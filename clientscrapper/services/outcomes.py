"""What the ERP did with each push, and what that means here (02 §2.11, 05 §B.2).

One feed row of ``GET /api/agent/outcomes`` (``FeedRow``, parsed by
``erp_feeds.parse_outcome``) becomes one ``outcomes`` row, keyed by its
``external_id``, written only for an id the agent pushed (``push_log``): the
«Boshqa» clause is permanent and anybody holding that key can post ``cs:`` ids
(05 §A.13), so every other row is ``foreign`` and dropped.

``derive_label`` is THE label rule (pure; the table of 05 §B.2 is its
picture): ``no_contact`` is the ERP's own answer, ``won`` is the stage (never
``client_id``, which a lead revived from won keeps), ``stale_open`` ages on
the push's ``sent_at`` (never the row's ``at``, which any touch moves), and
the lost-reason classes match LABELS only — the ERP sends a dictionary or
setting label, a seller's free text never leaves it (``LOST_REASON_PATTERNS``).

``apply_effects`` recomputes, per company, the local suppression rows and the
company flags its CURRENT labels justify — so a label that changes takes back
what the old one wrote (the retractions): a lead revived out of lost, moved
from won back to open, or given another reason. Kept stricter on purpose and
never taken back here: ``bounced`` (a wrong number is a fact about the
number), ``companies.is_competitor`` (the owner clears it by hand), every row
a person wrote and every ``erp_feed`` / ``manual_import`` row (their own syncs
own them). A row this module writes is ``source='outcomes'`` with
``reason='outcome:<external_id>'`` or ``'erp_belt:<external_id>'`` — that is
how it finds its own rows again.

``carry_lost_reason`` is the one place a stored value outranks the feed: a
renamed dictionary label comes back as ``lost_reason: null`` on every old
lead (the ERP compares with the CURRENT labels, verify:final), while the
lead, its stage and its decision moved nowhere; the text chosen at the move
is kept so the label and its cooldown do not flip (05 §B.6 «old leads keep
their spelling»).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db import enums as e
from clientscrapper.db.models import (
    Company,
    Contact,
    LeadCandidate,
    Outcome,
    PushLog,
    Suppression,
)
from clientscrapper.services import keys

# --- the feed row (05 §A.9) ------------------------------------------------------------

Landing = Literal["created", "joined", "client", "dropped"]
StageKind = Literal["open", "won", "lost"]
ContactKind = Literal["call", "telegram", "note", "stage", "followup"]
Label = Literal[
    "won",
    "contacted_open",
    "open_untouched",
    "stale_open",
    "lost_no_contact",
    "lost_price",
    "lost_not_importer",
    "lost_small",
    "lost_wrong_number",
    "lost_competitor",
    "lost_other",
    "landed_client",
    "landed_joined",
    "dropped_capped",
    "dropped_suppressed",
    "dropped_other",
]
PrecisionClass = Literal["positive", "negative", "pending", "neutral"]

# Exactly the 15 fields of 05 §A.9, in the feed's order: all present in a row
# (absent → a shape error), and all ``raw`` keeps (an extra key the ERP adds
# later is ignored, not stored).
FIELDS: Final = (
    "external_id",
    "intake_id",
    "outcome",
    "reason",
    "lead_id",
    "client_id",
    "stage_kind",
    "stage_name",
    "lost_reason",
    "no_contact",
    "closed_at",
    "contacted_at",
    "contact_kind",
    "quoted_volume_m3",
    "at",
)

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
Uuid = Annotated[str, Field(pattern=_UUID)]


class FeedRow(BaseModel):
    """One outcomes row, validated (``erp_feeds.parse_outcome`` builds it:
    the instants as aware datetimes, the kub as a ``Decimal``)."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    # Any cs: id the ERP holds — a foreign one too, which is skipped, not refused.
    external_id: Annotated[str, Field(min_length=1, max_length=2048)]
    intake_id: Uuid
    outcome: Landing
    # suppressed|capped|no_phone|no_contact; a reason added later is dropped_other.
    reason: Annotated[str, Field(max_length=200)] | None
    lead_id: Uuid | None
    client_id: Uuid | None
    stage_kind: StageKind | None
    stage_name: str | None
    lost_reason: str | None
    no_contact: bool
    closed_at: datetime | None
    contacted_at: datetime | None
    contact_kind: ContactKind | None
    quoted_volume_m3: Decimal | None
    at: datetime


# --- the label (02 §2.11) -------------------------------------------------------------

CONTACT_RIPE: Final = timedelta(days=14)
STALE_AFTER: Final = timedelta(days=30)

# Lower-cased ``re.search`` over the normalised label, in this order: the
# first class that matches decides (02 §2.11, 05 §B.2). Labels only — the ERP
# never sends a seller's free text (05 §A.9).
LOST_REASON_PATTERNS: Final[Mapping[Label, re.Pattern[str]]] = {
    "lost_price": re.compile(r"narx|цена|qimmat|дорог"),
    "lost_not_importer": re.compile(r"import qilmaydi|не импорт|xitoy emas"),
    "lost_small": re.compile(r"hajm|kichik|мал"),
    "lost_wrong_number": re.compile(r"raqam|номер|noto.g.ri"),
    "lost_competitor": re.compile(r"kargo|cargo|konkurent|конкурент"),
}
PRECISION: Final[Mapping[Label, PrecisionClass]] = {
    "lost_price": "neutral",
    "lost_not_importer": "negative",
    "lost_small": "negative",
    "lost_wrong_number": "negative",
    "lost_competitor": "negative",
}


def normalise_label(text: str | None) -> str:
    """Every whitespace run one space, trimmed, lower case — the ERP's own
    normalisation of a lost reason (05 §A.7)."""
    return " ".join((text or "").split()).lower()


def derive_label(
    row: FeedRow, *, pushed_at: datetime, now: datetime
) -> tuple[Label, PrecisionClass]:
    """The label and its precision class. ``pushed_at`` is the ``sent_at`` of
    the push_log row that delivered this ``external_id`` — never ``row.at``:
    ``at`` moves on any touch of the lead (a board re-order, an owner change,
    a joined landing)."""
    if row.outcome == "client":
        return "landed_client", "neutral"  # the fallback door only (05 §A.3)
    if row.outcome == "dropped":
        if row.reason == "capped":
            return "dropped_capped", "neutral"  # eligible again next month
        if row.reason == "suppressed":
            return "dropped_suppressed", "neutral"  # the ERP's belt refused it
        return "dropped_other", "neutral"  # no_phone | no_contact | anything new
    if row.stage_kind == "won":
        return "won", "positive"  # NOT client_id: a lead revived from won keeps it
    if row.stage_kind == "lost":
        if row.no_contact:
            return "lost_no_contact", "negative"
        reason = normalise_label(row.lost_reason)
        for label, pattern in LOST_REASON_PATTERNS.items():
            if pattern.search(reason):
                return label, PRECISION[label]
        return "lost_other", "neutral"
    # Open, or no stage (a lead the ERP removed): the contact decides.
    if row.contacted_at is not None:
        ripe = now - row.contacted_at >= CONTACT_RIPE
        return "contacted_open", ("positive" if ripe else "pending")
    if row.outcome == "joined":
        return "landed_joined", "neutral"
    if now - pushed_at >= STALE_AFTER:
        return "stale_open", "neutral"
    return "open_untouched", "pending"


def carry_lost_reason(
    row: FeedRow,
    *,
    stored_stage_kind: str | None,
    stored_closed_at: datetime | None,
    stored_lost_reason: str | None,
) -> FeedRow:
    """The stored label, when the feed now sends ``null`` for the reason of a
    lead that is still lost with the same decision — a dictionary label was
    renamed (or the setting no longer lists it), and the ERP compares with
    the current labels only. A revival and a new loss re-stamp ``closed_at``,
    so they are never carried."""
    if (
        row.stage_kind == "lost"
        and row.lost_reason is None
        and stored_stage_kind == "lost"
        and stored_lost_reason is not None
        and stored_closed_at is not None
        and row.closed_at == stored_closed_at
    ):
        return row.model_copy(update={"lost_reason": stored_lost_reason})
    return row


# --- one page recorded (05 §B.2 per-row steps 1–4) -------------------------------------


@dataclass(frozen=True, slots=True)
class Delivery:
    """The push_log row an outcome is about: the delivered one nobody
    superseded, else the first delivered, else the first attempt."""

    id: int
    external_id: str
    candidate_id: uuid.UUID
    company_id: uuid.UUID
    phone_tail: str
    sent_at: datetime
    status: str
    source_key: str | None


@dataclass(slots=True)
class PageResult:
    """What one page did; ``erp_feeds.OutcomesReport`` adds them up."""

    rows: int = 0
    foreign: int = 0
    stored: int = 0
    changed: int = 0
    promoted: int = 0
    labels: dict[str, int] = field(default_factory=dict)
    companies: set[uuid.UUID] = field(default_factory=set)
    # external_ids whose label became dropped_suppressed on this page.
    stale_hits: int = 0
    effects: Effects | None = None


async def deliveries(
    session: AsyncSession, external_ids: Iterable[str]
) -> dict[str, Delivery]:
    """The push_log row each pushed ``external_id`` is about; an id with no
    push_log row is absent (foreign)."""
    ids = sorted(set(external_ids))
    if not ids:
        return {}
    delivered = PushLog.status.in_(e.PUSH_DELIVERED_STATUSES)
    live = sa.and_(delivered, PushLog.superseded_at.is_(None))
    rows = await session.execute(
        sa.select(
            PushLog.id,
            PushLog.external_id,
            PushLog.candidate_id,
            PushLog.company_id,
            PushLog.phone_tail,
            PushLog.sent_at,
            PushLog.status,
            LeadCandidate.primary_source_key,
        )
        .join(LeadCandidate, LeadCandidate.id == PushLog.candidate_id)
        .where(PushLog.external_id.in_(ids))
        .distinct(PushLog.external_id)
        .order_by(
            PushLog.external_id,
            sa.case((live, 0), (delivered, 1), else_=2),
            PushLog.attempt,
        )
    )
    return {
        r.external_id: Delivery(
            id=r.id,
            external_id=r.external_id,
            candidate_id=r.candidate_id,
            company_id=r.company_id,
            phone_tail=r.phone_tail.strip(),
            sent_at=r.sent_at,
            status=r.status,
            source_key=r.primary_source_key,
        )
        for r in rows
    }


async def record_page(
    session: AsyncSession,
    rows: Sequence[FeedRow],
    raw: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    hasher: Hasher | None = None,
) -> PageResult:
    """Steps 1–4 of 05 §B.2 for one page, in the caller's transaction:
    foreign rows skipped, every other row upserted with its label, the
    touched companies' effects recomputed, an ``unknown`` push promoted to
    ``sent`` and its candidate to ``pushed``. ``raw[i]`` is ``rows[i]`` as the
    feed sent it (``outcomes.raw``)."""
    result = PageResult(rows=len(rows))
    await _lock(session)
    found = await deliveries(session, (r.external_id for r in rows))
    ours = [
        (row, dict(src))
        for row, src in zip(rows, raw, strict=True)
        if row.external_id in found
    ]
    result.foreign = len(rows) - len(ours)
    if not ours:
        return result
    stored = {
        o.external_id: o
        for o in await session.scalars(
            sa.select(Outcome)
            .where(Outcome.external_id.in_([r.external_id for r, _ in ours]))
            .execution_options(populate_existing=True)
        )
    }
    values: dict[str, dict[str, Any]] = {}
    for row, src in ours:
        push = found[row.external_id]
        old = stored.get(row.external_id)
        if old is not None:
            row = carry_lost_reason(
                row,
                stored_stage_kind=old.stage_kind,
                stored_closed_at=old.closed_at,
                stored_lost_reason=old.lost_reason,
            )
        label, precision = derive_label(row, pushed_at=push.sent_at, now=now)
        if old is None or old.label != label:
            result.changed += 1
            result.labels[label] = result.labels.get(label, 0) + 1
            if label == "dropped_suppressed":
                result.stale_hits += 1
        result.companies.add(push.company_id)
        # A page may carry one id twice only if the ERP broke its own key;
        # the later row stands.
        values[row.external_id] = _outcome_values(row, src, push, label, precision, now)
    await _upsert_outcomes(session, list(values.values()))
    result.stored = len(values)
    result.promoted = await _promote(session, [found[eid] for eid in values])
    result.effects = await apply_effects(
        session, result.companies, now=now, hasher=hasher
    )
    return result


def _outcome_values(
    row: FeedRow,
    src: Mapping[str, Any],
    push: Delivery,
    label: Label,
    precision: PrecisionClass,
    now: datetime,
) -> dict[str, Any]:
    return {
        "external_id": row.external_id,
        "intake_id": uuid.UUID(row.intake_id),
        "candidate_id": push.candidate_id,
        "company_id": push.company_id,
        "source_key": push.source_key,
        "landing": row.outcome,
        "landing_reason": row.reason,
        "lead_id": uuid.UUID(row.lead_id) if row.lead_id else None,
        "client_id": uuid.UUID(row.client_id) if row.client_id else None,
        "stage_kind": row.stage_kind,
        "stage_name": row.stage_name,
        "lost_reason": row.lost_reason,
        "no_contact": row.no_contact,
        "closed_at": row.closed_at,
        "contacted_at": row.contacted_at,
        "contact_kind": row.contact_kind,
        "quoted_volume_m3": row.quoted_volume_m3,
        "erp_at": row.at,
        "label": label,
        "precision_class": precision,
        "label_changed_at": now,
        "first_read_at": now,
        "read_at": now,
        # The 15 fields as the feed sent them, and nothing else.
        "raw": {name: src.get(name) for name in FIELDS},
    }


async def _upsert_outcomes(session: AsyncSession, values: list[dict[str, Any]]) -> None:
    """Upsert by ``external_id``: every field moves but ``first_read_at``,
    and ``label_changed_at`` only when the label did."""
    stmt = pg_insert(Outcome).values(values)
    kept = {"external_id", "first_read_at", "label_changed_at"}
    moved: dict[str, sa.ColumnElement[Any]] = {
        c: stmt.excluded[c] for c in values[0] if c not in kept
    }
    moved["label_changed_at"] = sa.case(
        (
            Outcome.label.is_distinct_from(stmt.excluded.label),
            stmt.excluded.label_changed_at,
        ),
        else_=Outcome.label_changed_at,
    )
    await session.execute(
        stmt.on_conflict_do_update(index_elements=[Outcome.external_id], set_=moved)
    )


async def _promote(session: AsyncSession, pushes: Sequence[Delivery]) -> int:
    """Step 4: the ERP shows the id, so an ``unknown`` push landed — it
    becomes ``sent`` (``http_status`` stays as it was; ck_push_log_sent lets
    a NULL through) and its candidate ``approved → pushed``. A superseded
    row is history and stays as it is."""
    ids = [p.id for p in pushes if p.status == "unknown"]
    if not ids:
        return 0
    promoted = (
        await session.execute(
            sa.update(PushLog)
            .where(
                PushLog.id.in_(ids),
                PushLog.status == "unknown",
                PushLog.superseded_at.is_(None),
                sa.or_(
                    PushLog.http_status.is_(None),
                    PushLog.http_status.between(200, 299),
                ),
            )
            .values(status="sent")
            .returning(
                PushLog.id, PushLog.candidate_id, PushLog.external_id, PushLog.sent_at
            )
        )
    ).all()
    for _, candidate_id, external_id, sent_at in promoted:
        await session.execute(
            sa.update(LeadCandidate)
            .where(LeadCandidate.id == candidate_id, LeadCandidate.state == "approved")
            .values(state="pushed", pushed_at=sent_at, external_id=external_id)
        )
    return len(promoted)


# --- the effects (02 §2.11): derived, not appended ----------------------------------

OUTCOME_REASON: Final = "outcome:"
BELT_REASON: Final = "erp_belt:"
PRICE_COOLDOWN: Final = timedelta(days=180)
NOT_IMPORTER_COOLDOWN: Final = timedelta(days=365)
# 02 §2.8: a wrong number's first time is a 30-day cooldown on the phone, the
# second (a second push the ERP lost as a wrong number) makes it bounced.
WRONG_NUMBER_COOLDOWN: Final = timedelta(days=30)
WRONG_NUMBER_TIMES: Final = 2
CLIENT_LABELS: Final[frozenset[str]] = frozenset({"won", "landed_client"})
# The rows an outcome never takes back (02 §2.11 «kept stricter on purpose»).
NEVER_RETRACTED: Final[frozenset[str]] = frozenset({"bounced"})
# Serialises the recompute across processes (the nightly walk, reconcile's
# 10-minute walk): two of them over one company would each delete what the
# other is inserting.
EFFECTS_LOCK: Final = "cs:outcomes:effects"

Triple = tuple[str, str, str]  # (kind, key_type, key)
# A nine-digit tail → the feed's hash of it (ErpClient.hash_tail).
Hasher = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class Want:
    reason: str
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Labelled:
    """One of a company's outcomes as the recompute reads it."""

    external_id: str
    label: str
    label_changed_at: datetime
    phone_tail: str


@dataclass(frozen=True, slots=True)
class CompanyKeys:
    """What a company is known by, beyond the pushed phone (02 §2.11
    ``lost_no_contact``: the ERP suppresses by phone only)."""

    id: uuid.UUID
    tg_usernames: frozenset[str] = frozenset()
    instagrams: frozenset[str] = frozenset()
    domain: str | None = None
    stir: str | None = None
    phone_tails: frozenset[str] = frozenset()


@dataclass(slots=True)
class Effects:
    companies: int = 0
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    to_client: int = 0
    from_client: int = 0
    competitors: int = 0


def owned_reasons(external_ids: Iterable[str]) -> frozenset[str]:
    """The reasons that mark a suppression row as written for these pushes."""
    return frozenset(
        f"{prefix}{eid}"
        for eid in external_ids
        for prefix in (OUTCOME_REASON, BELT_REASON)
    )


def _key_ok(key: str | None) -> bool:
    # ck_suppression_key: 3–200 characters.
    return key is not None and 3 <= len(key) <= 200


def desired(
    company: CompanyKeys,
    labelled: Sequence[Labelled],
    *,
    wrong_numbers: Mapping[str, int],
    now: datetime,
) -> dict[Triple, Want]:
    """The local suppression rows a company's current labels justify (02
    §2.11's table). ``wrong_numbers[tail]`` = how many pushes of that phone
    (any company) the ERP lost as a wrong number. A cooldown already past is
    not wanted. Of two pushes wanting one row, a won/landed push names it
    before a belt refusal, the longer cooldown before the shorter, else the
    first id."""
    want: dict[Triple, Want] = {}

    def add(triple: Triple, new: Want) -> None:
        old = want.get(triple)
        if old is None or _better(new, old):
            want[triple] = new

    for item in sorted(labelled, key=lambda x: x.external_id):
        tail = item.phone_tail
        because = Want(OUTCOME_REASON + item.external_id)
        if item.label in CLIENT_LABELS:
            add(("client", "phone_tail", tail), because)
        elif item.label == "dropped_suppressed":
            add(("client", "phone_tail", tail), Want(BELT_REASON + item.external_id))
        elif item.label == "lost_no_contact":
            add(("lost", "phone_tail", tail), because)
            for name in sorted(company.tg_usernames):
                add(("lost", "tg_username", name), because)
            for handle in sorted(company.instagrams):
                if _key_ok(handle):
                    add(("lost", "instagram", handle), because)
            if company.domain and _key_ok(company.domain):
                add(("lost", "domain", company.domain), because)
            if company.stir:
                add(("lost", "stir", company.stir), because)
        elif item.label in ("lost_price", "lost_not_importer"):
            span = PRICE_COOLDOWN if item.label == "lost_price" else NOT_IMPORTER_COOLDOWN
            until = item.label_changed_at + span
            if until > now:
                add(
                    ("cooldown", "company_id", str(company.id)),
                    Want(because.reason, until),
                )
        elif item.label == "lost_wrong_number":
            if wrong_numbers.get(tail, 0) >= WRONG_NUMBER_TIMES:
                add(("bounced", "phone_tail", tail), because)
            else:
                until = item.label_changed_at + WRONG_NUMBER_COOLDOWN
                if until > now:
                    add(("cooldown", "phone_tail", tail), Want(because.reason, until))
    return want


def _better(new: Want, old: Want) -> bool:
    """Of two pushes wanting one row, which names it — the longer cooldown, a
    landing before a belt refusal, then the smaller reason (any order of
    reading gives the same row)."""
    if new.expires_at != old.expires_at:
        if new.expires_at is None or old.expires_at is None:
            return new.expires_at is None
        return new.expires_at > old.expires_at
    rank_new = new.reason.startswith(BELT_REASON)
    rank_old = old.reason.startswith(BELT_REASON)
    if rank_new != rank_old:
        return rank_old
    return new.reason < old.reason


async def apply_effects(
    session: AsyncSession,
    company_ids: Iterable[uuid.UUID],
    *,
    now: datetime,
    hasher: Hasher | None = None,
) -> Effects:
    """Recompute the companies' ``source='outcomes'`` suppression rows,
    ``companies.state`` and ``is_competitor`` from their current labels, in
    the caller's transaction (02 §2.11). A row a company no longer wants is
    deleted — unless another company's labels want it too, which then names
    it; a missing one is inserted; a row another source wrote is never
    touched. ``hasher`` hashes a tail as the feed does
    (``ErpClient.hash_tail``; default: under ``CRM_AGENT_KEY``) — the client
    check reads the feed's hashed rows too."""
    ids = sorted(set(company_ids))
    if not ids:
        return Effects()
    await _lock(session)
    hash_tail = hasher or _configured_hasher()
    loaded = await _load(session, ids)
    # The wrong-number count is the one input one company's label gives
    # another's: the earlier company's 30-day cooldown is not wanted once the
    # phone is bounced.
    sharing = await _sharing_wrong_numbers(session, loaded, exclude=set(ids))
    if sharing:
        loaded.merge(await _load(session, sorted(sharing)))
        ids = sorted({*ids, *sharing})
    effects = Effects(companies=len(ids))
    wants = {c: desired(k, loaded.labelled[c], wrong_numbers=loaded.wrong, now=now)
             for c, k in loaded.companies.items()}  # fmt: skip
    gone = await _delete_unwanted(session, loaded, wants)
    effects.deleted = len(gone)
    # A row one company let go may still be another's (a shared phone).
    others = await _companies_wanting(session, gone, exclude=set(ids))
    if others:
        more = await _load(session, sorted(others))
        loaded.merge(more)
        wants.update(
            {
                c: desired(k, more.labelled[c], wrong_numbers=loaded.wrong, now=now)
                for c, k in more.companies.items()
            }  # fmt: skip
        )
    effects.inserted, effects.updated = await _write_wanted(session, loaded, wants)
    await _flags(session, loaded, ids, hash_tail, effects)
    return effects


async def _lock(session: AsyncSession) -> None:
    """``EFFECTS_LOCK`` for the rest of the transaction (re-entrant: a page
    takes it before its upserts, its recompute again)."""
    await session.execute(
        sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtext(EFFECTS_LOCK)))
    )


@dataclass(slots=True)
class _Loaded:
    companies: dict[uuid.UUID, CompanyKeys] = field(default_factory=dict)
    labelled: dict[uuid.UUID, list[Labelled]] = field(default_factory=dict)
    state: dict[uuid.UUID, tuple[str, str | None, bool]] = field(default_factory=dict)
    wrong: dict[str, int] = field(default_factory=dict)

    def merge(self, other: _Loaded) -> None:
        self.companies.update(other.companies)
        self.labelled.update(other.labelled)
        self.state.update(other.state)
        self.wrong.update(other.wrong)

    def owned(self, company_id: uuid.UUID) -> frozenset[str]:
        return owned_reasons(x.external_id for x in self.labelled.get(company_id, ()))


def _first_tail(where: sa.ColumnElement[bool]) -> sa.Subquery:
    """(external_id, phone_tail) of the pushes ``where`` selects: one phone
    per id (every attempt re-sends the identical body)."""
    return (
        sa.select(PushLog.external_id, PushLog.phone_tail)
        .where(where)
        .distinct(PushLog.external_id)
        .order_by(PushLog.external_id, PushLog.attempt)
        .subquery()
    )


async def _load(session: AsyncSession, ids: Sequence[uuid.UUID]) -> _Loaded:
    """Everything the recompute reads, in four statements for any number of
    companies."""
    loaded = _Loaded()
    first_tail = _first_tail(PushLog.company_id.in_(ids))
    rows = await session.execute(
        sa.select(
            Outcome.company_id,
            Outcome.external_id,
            Outcome.label,
            Outcome.label_changed_at,
            first_tail.c.phone_tail,
        )
        .join(first_tail, first_tail.c.external_id == Outcome.external_id)
        .where(Outcome.company_id.in_(ids))
    )
    for r in rows:
        loaded.labelled.setdefault(r.company_id, []).append(
            Labelled(r.external_id, r.label, r.label_changed_at, r.phone_tail.strip())
        )
    handles: dict[uuid.UUID, tuple[set[str], set[str], set[str]]] = {
        c: (set(), set(), set()) for c in ids
    }
    for c in await session.execute(
        sa.select(
            Contact.company_id,
            Contact.telegram_username,
            Contact.instagram,
            Contact.phone_tail,
        ).where(Contact.company_id.in_(ids), Contact.anonymised_at.is_(None))  # fmt: skip
    ):
        tg, ig, tails = handles[c.company_id]
        if c.telegram_username:
            tg.add(c.telegram_username)
        if c.instagram:
            ig.add(c.instagram)
        if c.phone_tail:
            tails.add(c.phone_tail.strip())
    for co in await session.execute(
        sa.select(
            Company.id,
            Company.telegram_channel,
            Company.instagram,
            Company.website_domain,
            Company.stir,
            Company.state,
            Company.exclude_reason,
            Company.is_competitor,
        ).where(Company.id.in_(ids))
    ):
        tg, ig, tails = handles[co.id]
        if co.telegram_channel:
            tg.add(co.telegram_channel)
        if co.instagram:
            ig.add(co.instagram)
        tails.update(x.phone_tail for x in loaded.labelled.get(co.id, ()))
        loaded.companies[co.id] = CompanyKeys(
            id=co.id,
            tg_usernames=frozenset(tg),
            instagrams=frozenset(ig),
            domain=co.website_domain,
            stir=co.stir.strip() if co.stir else None,
            phone_tails=frozenset(tails),
        )
        loaded.state[co.id] = (co.state, co.exclude_reason, co.is_competitor)
        loaded.labelled.setdefault(co.id, [])
    wrong_tails = sorted(
        {
            x.phone_tail
            for xs in loaded.labelled.values()
            for x in xs
            if x.label == "lost_wrong_number"
        }  # fmt: skip
    )
    if wrong_tails:
        first_tail = _first_tail(PushLog.phone_tail.in_(wrong_tails))
        counted = await session.execute(
            sa.select(
                first_tail.c.phone_tail, sa.func.count(sa.distinct(Outcome.external_id))
            )
            .join(first_tail, first_tail.c.external_id == Outcome.external_id)
            .where(
                Outcome.label == "lost_wrong_number",
                first_tail.c.phone_tail.in_(wrong_tails),
            )
            .group_by(first_tail.c.phone_tail)
        )
        loaded.wrong.update({t.strip(): n for t, n in counted})
    return loaded


async def _sharing_wrong_numbers(
    session: AsyncSession, loaded: _Loaded, *, exclude: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Companies outside ``exclude`` the ERP lost as a wrong number on a phone
    one of ``loaded``'s companies was lost on the same way."""
    tails = sorted(
        {
            x.phone_tail
            for xs in loaded.labelled.values()
            for x in xs
            if x.label == "lost_wrong_number"
        }  # fmt: skip
    )
    if not tails:
        return set()
    found = await session.scalars(
        sa.select(Outcome.company_id)
        .join(PushLog, PushLog.external_id == Outcome.external_id)
        .where(Outcome.label == "lost_wrong_number", PushLog.phone_tail.in_(tails))
        .distinct()
    )
    return {c for c in found if c is not None and c not in exclude}


async def _delete_unwanted(
    session: AsyncSession, loaded: _Loaded, wants: Mapping[uuid.UUID, dict[Triple, Want]]
) -> list[Triple]:
    """The companies' own rows their labels no longer justify (never a
    ``bounced`` one)."""
    owner = {
        reason: company_id
        for company_id in loaded.companies
        for reason in loaded.owned(company_id)
    }
    if not owner:
        return []
    doomed: list[int] = []
    for start in range(0, len(owner), 500):
        chunk = sorted(owner)[start : start + 500]
        mine = await session.execute(
            sa.select(
                Suppression.id,
                Suppression.kind,
                Suppression.key_type,
                Suppression.key,
                Suppression.reason,
            ).where(
                Suppression.source == "outcomes",
                Suppression.reason.in_(chunk),
                Suppression.kind.not_in(sorted(NEVER_RETRACTED)),
            )
        )
        for r in mine:
            if (r.kind, r.key_type, r.key) not in wants.get(owner[r.reason], {}):
                doomed.append(r.id)
    if not doomed:
        return []
    gone = await session.execute(
        sa.delete(Suppression)
        .where(Suppression.id.in_(doomed))
        .returning(Suppression.kind, Suppression.key_type, Suppression.key)
    )
    return [(k, t, v) for k, t, v in gone]


async def _companies_wanting(
    session: AsyncSession, triples: Sequence[Triple], *, exclude: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Companies outside ``exclude`` whose labels may want one of these rows:
    a push on the same phone, or the same handle, domain or STIR."""
    found: set[uuid.UUID | None] = set()
    tails = sorted({k for _, t, k in triples if t == "phone_tail"})
    if tails:
        found.update(
            await session.scalars(
                sa.select(Outcome.company_id)
                .join(PushLog, PushLog.external_id == Outcome.external_id)
                .where(PushLog.phone_tail.in_(tails))
                .distinct()
            )
        )
    named = {
        "tg_username": (Company.telegram_channel, Contact.telegram_username),
        "instagram": (Company.instagram, Contact.instagram),
        "domain": (Company.website_domain, None),
        "stir": (Company.stir, None),
    }
    for _, key_type, key in triples:
        if key_type not in named:
            continue  # a phone (above) or a company_id (its own company only)
        on_company, on_contact = named[key_type]
        found.update(
            await session.scalars(sa.select(Company.id).where(on_company == key))
        )
        if on_contact is not None:
            found.update(
                await session.scalars(
                    sa.select(Contact.company_id).where(
                        on_contact == key, Contact.anonymised_at.is_(None)
                    )
                )
            )
    return {c for c in found if c is not None and c not in exclude}


async def _write_wanted(
    session: AsyncSession, loaded: _Loaded, wants: Mapping[uuid.UUID, dict[Triple, Want]]
) -> tuple[int, int]:
    """Insert the wanted rows that are missing; re-name or re-date the ones
    the same company already owns. A row another source (or another
    company's push) holds stays as it is."""
    triples = sorted({t for want in wants.values() for t in want})
    if not triples:
        return 0, 0
    standing: dict[Triple, tuple[int, str, str | None, datetime | None]] = {}
    for start in range(0, len(triples), 500):
        chunk = triples[start : start + 500]
        rows = await session.execute(
            sa.select(
                Suppression.id,
                Suppression.kind,
                Suppression.key_type,
                Suppression.key,
                Suppression.source,
                Suppression.reason,
                Suppression.expires_at,
            ).where(
                sa.tuple_(Suppression.kind, Suppression.key_type, Suppression.key).in_(
                    chunk
                )
            )
        )
        for r in rows:
            standing[(r.kind, r.key_type, r.key)] = (
                r.id,
                r.source,
                r.reason,
                r.expires_at,
            )
    inserts: dict[Triple, Want] = {}
    updated = 0
    for company_id, want in wants.items():
        owned = loaded.owned(company_id)
        for triple, w in want.items():
            row = standing.get(triple)
            if row is None:
                if triple not in inserts or _better(w, inserts[triple]):
                    inserts[triple] = w
                continue
            row_id, source, reason, expires_at = row
            if (
                source != "outcomes"
                or reason not in owned
                or triple[0] in NEVER_RETRACTED
            ):
                continue
            if (reason, expires_at) != (w.reason, w.expires_at):
                await session.execute(
                    sa.update(Suppression)
                    .where(Suppression.id == row_id)
                    .values(reason=w.reason, expires_at=w.expires_at)
                )
                standing[triple] = (row_id, source, w.reason, w.expires_at)
                updated += 1
    if not inserts:
        return 0, updated
    stmt = pg_insert(Suppression).values(
        [
            {
                "kind": kind,
                "key_type": key_type,
                "key": key,
                "reason": w.reason,
                "source": "outcomes",
                "expires_at": w.expires_at,
            }
            for (kind, key_type, key), w in sorted(inserts.items())
        ]
    )
    written = await session.execute(
        stmt.on_conflict_do_nothing(constraint="uq_suppression_key").returning(
            Suppression.id
        )
    )
    return len(written.all()), updated


async def _flags(
    session: AsyncSession,
    loaded: _Loaded,
    ids: Sequence[uuid.UUID],
    hash_tail: Hasher | None,
    effects: Effects,
) -> None:
    """``companies.state`` and ``is_competitor`` of the touched companies.
    ``client`` while a won/landed label stands; back to ``active`` (or
    ``excluded``, if it carries an exclusion) only when no ``client`` row
    from any source holds one of its phones. ``is_competitor`` is only ever
    set here, never cleared."""
    for company_id in ids:
        if company_id not in loaded.state:
            continue
        state, exclude_reason, competitor = loaded.state[company_id]
        labels = {x.label for x in loaded.labelled.get(company_id, ())}
        values: dict[str, Any] = {}
        if "lost_competitor" in labels and not competitor:
            values["is_competitor"] = True
            effects.competitors += 1
        if state not in ("merged", "purged"):
            if labels & CLIENT_LABELS:
                if state != "client":
                    values["state"] = "client"
                    effects.to_client += 1
            elif state == "client" and not await _client_elsewhere(
                session, loaded.companies[company_id], hash_tail
            ):
                values["state"] = "excluded" if exclude_reason else "active"
                effects.from_client += 1
        if values:
            await session.execute(
                sa.update(Company).where(Company.id == company_id).values(**values)
            )


async def _client_elsewhere(
    session: AsyncSession, company: CompanyKeys, hash_tail: Hasher | None
) -> bool:
    """A ``client`` row of any source holds one of the company's phones: a
    clear tail, or the feed's hash of it."""
    tails = sorted(company.phone_tails)
    if not tails:
        return False
    match = sa.and_(Suppression.key_type == "phone_tail", Suppression.key.in_(tails))
    if hash_tail is not None:
        hashes = [hash_tail(t) for t in tails]
        match = sa.or_(
            match,
            sa.and_(Suppression.key_type == "phone_hash", Suppression.key.in_(hashes)),
        )
    found = await session.scalar(
        sa.select(sa.func.count(Suppression.id)).where(
            Suppression.kind == "client", match
        )
    )
    return bool(found)


def _configured_hasher() -> Hasher | None:
    """The feed's hash under ``CRM_AGENT_KEY``; None while no key is set."""
    secret = settings.crm_agent_key
    if not secret.strip():
        return None
    return lambda tail: keys.erp_phone_hash(secret, tail)


__all__ = [
    "CONTACT_RIPE",
    "FIELDS",
    "LOST_REASON_PATTERNS",
    "PRECISION",
    "STALE_AFTER",
    "CompanyKeys",
    "Delivery",
    "Effects",
    "FeedRow",
    "Label",
    "Labelled",
    "PageResult",
    "PrecisionClass",
    "Want",
    "apply_effects",
    "carry_lost_reason",
    "deliveries",
    "derive_label",
    "desired",
    "normalise_label",
    "owned_reasons",
    "record_page",
]
