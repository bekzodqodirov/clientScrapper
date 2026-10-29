"""The pre-push checklist (04 §7) and the ``push_log`` claim (02 §2.10 rules
1–3 and 6).

``check`` evaluates the fourteen items under the candidate's ``FOR UPDATE``
lock and answers a list of ``Refusal``s — empty means the push may go. Each
refusal names the item it belongs to (``Refusal.check``), so one broken item
is one refusal:

1. ``approved`` with ``approved_by`` (a rule-5 re-push of the candidate's own
   ``external_id`` also reads ``pushed``);
2. ``CRM_PUSH_ENABLED``, no ``/toxtat``, the ``erp_sync`` breaker clear,
   ``erp_feeds.suppression_probe`` fresh (the one home of freshness);
3. gates 2–7 on live data (``services/gates``) — the parts later items own
   (the suppression hit, the push windows) are theirs, so a DNC is item 5's
   and a recent push items 7/8's, never two refusals;
4. the lead contact: a phone, not anonymised, ``+998``, mobile or landline
   (``scoring.usable_phone``), not shared unless the owner approved it with
   the note ``shared_ok``;
5. ``suppression.is_suppressed(build_keys(…))`` with the lead tail — a
   cooldown is ``cooldown`` with its end;
6. the ``external_id``'s own rows: nothing delivered or in flight, at most
   ``MAX_ATTEMPTS``, the next attempt only after ``ATTEMPT_DELAYS`` and with
   the identical body (rule 6);
7. no push of the lead tail in 24 h;
8. no open push of the company's lineage in 90 d (02 §2.10 rule 2);
9. ``PUSH_DAILY_CAP`` for the Tashkent day (up to ``now``) and the ERP's
   rolling 24-hour source cap of ``SOURCE_CAP`` (05 §A.2) on the door pushed
   to — counting, as 7 and 8 do, a delivered row and one in flight
   (``gates.PUSH_COUNTED_STATUSES``), only rows nobody superseded and never
   the push's own ``external_id``;
10. Mon–Sat 09:30–17:30 Tashkent (``PUSH_WINDOW``/``PUSH_DAYS``);
11. the payload's invariants (``payload_problems``): the identity fields,
    ``name``, ``phone``, ``note`` ≤ ``NOTE_MAX``, the stated ``kub``, no
    pseudonym token, no NUL or lone surrogate. A body this ``external_id``
    already claimed (its sha is a row's — a further attempt, a rule-5
    re-push, ``/push tekshir``) is re-sent as it was: today's display name
    and stated volume are not its measure, the facts it was claimed on are
    (rule 6: the identical body); its shape, and the lead phone it is sent
    to, still are. The body is
    ``crm_push.build_payload``'s (T-28), and so are the invariants only its
    builder can state: the landed note ≤ 1,000 (``landed_note``), the first
    159 code points standing alone, the NEGA TOPILDI … BALL blocks, every
    quote verified and no second person's name (05 §B.1's
    ``test_payload_sections_and_limits``);
12. ``external_id = cs:<root company id>:<yyyymm>`` (Tashkent month at claim
    time) — a merged company is no root;
13. score ≥ 60 — the live score: ``crm_push.push_one`` re-scores an
    approved candidate under its locks before it claims (score_job never
    touches one), so a drop after the owner's tap is read here and sends it
    back to review («Ball tushdi»);
14. every refusal the owner should see is written to ``lead_reviews.note``
    (``record_refusals``) — on the owner's own answer row, after the
    owner's word when the note holds one (``shared_ok · …``), never as a row
    the owner did not tap; ``cap``/``window`` and the claim's own mechanics
    (in flight, delivered, waiting for an attempt) are silent.

``claim`` locks the candidate, checks, and either records the refusals —
and what they mean for the candidate: a permanent suppression → ``suppressed``,
a refusal only the owner can resolve → back to ``review`` with the note — or
inserts the ``sending`` row. The caller commits **before** the HTTP call (02
§2.10 rule 3). Two workers claiming one candidate serialise on the lock: the
second sees the first's ``sending`` row and is refused ``in_flight``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors.runner import global_pause
from clientscrapper.config import settings
from clientscrapper.db import enums as e
from clientscrapper.db.models import (
    Company,
    Contact,
    LeadCandidate,
    LeadReview,
    PushLog,
    Signal,
    Source,
)
from clientscrapper.services import (
    candidates,
    companies,
    erp_feeds,
    gates,
    scoring,
)
from clientscrapper.services.pseudonyms import TOKEN_TRACE_RE, unmasked
from clientscrapper.services.suppression import CLIENT_KINDS, Hit

log = logging.getLogger(__name__)

MIN_SCORE: Final = scoring.HOT_MIN
MAX_ATTEMPTS: Final = 3
# Between push_log attempts (05 §B.1's two-level scheme; the HTTP tries inside
# one attempt are with_backoff's, T-28).
ATTEMPT_DELAYS: Final[Mapping[int, timedelta]] = {
    2: timedelta(minutes=5),
    3: timedelta(minutes=30),
}
# 05 §A.2: the ERP's per-source cap over its arrivals ledger, a rolling 24 h.
SOURCE_CAP: Final = 200
SOURCE_CAP_WINDOW: Final = timedelta(hours=24)
# 05 §B.1 NOTE_MAX: the note's own budget, in UTF-16 units (text_len).
NOTE_MAX: Final = 700
SHARED_OK: Final = "shared_ok"
# A refusal written on the owner's approval row keeps the owner's own word
# first (``shared_ok``, 04 §7 item 4): «shared_ok · <refusal>».
OWNER_WORD_SEP: Final = " · "
UZ_MOBILE_OR_LANDLINE: Final = scoring.USABLE_PHONE_KINDS
# E.164 (02 §2.4's ck_contacts_phone); that it is an Uzbek number is item 4's.
_E164: Final = re.compile(r"^\+[1-9][0-9]{6,14}$")
_KUB: Final = re.compile(r"^[1-9][0-9]*(?:\.[0-9]+)?$")
# 04 §7 item 11: no pseudonym token left (<PHONE_n>, <TG_n>, <NAME_n> …, and
# 04 §4.2's unnumbered <ID> for a 14-digit run) — pseudonyms' own pattern.
_PSEUDONYM: Final = TOKEN_TRACE_RE
# The note's person line (05 §B.1: «  Masʼul: <ism> (<rol>)»), which may name
# the decision maker and nothing more: no phone, handle or messenger id
# (crm_push.masul_line drops a line that would carry one; this item refuses
# a body that carries one anyway).
MASUL_PREFIX: Final = "  Masʼul: "
_ILL_FORMED: Final = re.compile("[\x00\ud800-\udfff]")
PAYLOAD_IDENTITY: Final = ("external_id", "name", "phone", "note")
STATED_KEYS: Final = frozenset({scoring.STATED_3PLUS, scoring.STATED_1_3})
REVIEW_NOTE_MAX: Final = 500

# The owner's words (06 §1.3, 04 §7 item 14).
REFUSAL_UZ: Final[Mapping[str, str]] = {
    "not_approved": "Nomzod tasdiqlanmagan",
    "disabled": "CRMʼga yuborish oʻchirilgan (CRM_PUSH_ENABLED)",
    "paused": "Agent toʻxtatilgan (/toxtat)",
    "breaker": "ERP bilan aloqa vaqtincha toʻxtatilgan",
    "stale": "ERP bostirish roʻyxati eskirgan — push toʻxtatildi",
    "competitor": "Raqobatchi (kargo) deb topildi",
    "supplier_cn": "Xitoy yetkazib beruvchisi deb topildi",
    "client_state": "Bu kompaniya allaqachon mijoz",
    "liquidated": "Kompaniya tugatilgan",
    "state_body": "Davlat tashkiloti",
    "inactive": "Kompaniya reestrda faol emas",
    "rejected": "180 kun ichida rad etilgan",
    "no_phone": "Telefon yoʻq",
    "anonymised": "Aloqa maʼlumoti oʻchirilgan",
    "not_uz": "Telefon Oʻzbekiston raqami emas",
    "not_usable": "Telefon mobil yoki shahar raqami emas",
    "shared_phone": "Bu raqam bir nechta kompaniyada (umumiy raqam)",
    "suppressed": "🚫 Bu raqam endi bostirilgan",
    "cooldown": "Kutish muddati tugamagan",
    "delivered": "Bu oy allaqachon yuborilgan",
    "in_flight": "Yuborilmoqda",
    "payload_changed": "Lid matni oldingi urinishdan farq qiladi",
    # 02 §2.10 rule 4: «CRM javob bermadi». The id is the month's: a
    # re-approval this month meets the same three spent attempts.
    "attempts_spent": "CRM javob bermadi — bu oyning uch urinishi tugadi",
    "attempt_wait": "Keyingi urinishni kutmoqda",
    "phone_recent": "Bu raqamga 24 soat ichida yuborilgan",
    "company_recent": "Bu kompaniyaga 90 kun ichida yuborilgan",
    "cap": "Bugungi push chegarasi toʻldi",
    "source_cap": "ERPning 24 soatlik chegarasi toʻldi",
    "window": "Push vaqti emas (Du–Sh 09:30–17:30)",
    "no_payload": "Lid matni tayyor emas",
    "payload": "Lid matni tekshiruvdan oʻtmadi",
    "not_root": "Kompaniya boshqasiga birlashtirilgan",
    "external_id": "Lid identifikatori mos emas",
    "score_low": "Ball tushdi",
    # What the push itself learned (crm_push, 05 §B.1 / 02 §2.10 rule 5): a
    # 4xx other than 404 — «ERP rad etdi (HTTP n)»; a re-push the outcomes
    # feed never showed either; a re-push asked twice; an id the ERP has
    # already reported (/push tekshir, 06 §1.2).
    "erp_refused": "ERP rad etdi",
    "unconfirmed": "CRM tasdiqlamadi",
    "repushed": "Bu push allaqachon bir marta qayta yuborilgan",
    "reported": "ERP bu pushni allaqachon koʻrsatgan",
    # make push-test's company (score_breakdown.gates.test): only the tool
    # pushes it, never a job, a re-push or /push tekshir (05 §B.1).
    "test_company": "Sinov kompaniyasi (make push-test) — qayta yuborilmaydi",
}
# The job runs again: nothing for the owner to read (05 §B.1).
SILENT: Final = frozenset(
    {
        "not_approved",
        "cap",
        "source_cap",
        "window",
        "in_flight",
        "delivered",
        "attempt_wait",
        "test_company",
    }
)
# Only the owner can resolve these: the candidate goes back to a card.
TO_REVIEW: Final = frozenset(
    {
        "competitor",
        "supplier_cn",
        "client_state",
        "liquidated",
        "state_body",
        "inactive",
        "rejected",
        "no_phone",
        "anonymised",
        "not_uz",
        "not_usable",
        "shared_phone",
        "payload",
        "payload_changed",
        "attempts_spent",
        "not_root",
        "external_id",
        "score_low",
        "erp_refused",
        "unconfirmed",
    }
)


@dataclass(frozen=True, slots=True)
class Refusal:
    check: int
    code: str
    until: datetime | None = None
    hit: Hit | None = None
    detail: str | None = None

    @property
    def silent(self) -> bool:
        return self.code in SILENT

    @property
    def text(self) -> str:
        words = REFUSAL_UZ[self.code]
        if self.code == "erp_refused" and self.detail:
            words += f" (HTTP {self.detail})"
        if self.until is not None and self.code != "breaker":
            local = self.until.astimezone(settings.tz)
            words += f" ({local.strftime('%d.%m.%Y %H:%M')} gacha)"
        return words


@dataclass(slots=True)
class Plan:
    refusals: list[Refusal] = field(default_factory=list)
    external_id: str | None = None
    attempt: int = 1
    contact: Contact | None = None
    tail: str | None = None
    payload_sha256: str | None = None
    # The body is one this external_id already claimed (item 11, rule 6).
    stored: bool = False


@dataclass(frozen=True, slots=True)
class ClaimResult:
    refusals: tuple[Refusal, ...] = ()
    push_log_id: int | None = None
    external_id: str | None = None
    attempt: int | None = None

    @property
    def claimed(self) -> bool:
        return self.push_log_id is not None


# --- pure helpers ---------------------------------------------------------------------


def external_id_for(company_id: uuid.UUID, now: datetime) -> str:
    """02 §2.10 rule 1: ``cs:<company uuid>:<yyyymm>``, the Tashkent month."""
    local = now.astimezone(settings.tz)
    return f"cs:{company_id}:{local.year:04d}{local.month:02d}"


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    """Rule 6: sorted keys, no spaces, a None dropped (``exclude_none``)."""
    body = {k: v for k, v in payload.items() if v is not None}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def text_len(s: str) -> int:
    """UTF-16 units — the ERP's measure (05 §B.1)."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def window_opens(now: datetime) -> datetime | None:
    """None while the push window is open (Mon–Sat 09:30–17:30 Tashkent by
    default); else the moment it next opens."""
    tz = settings.tz
    start, end = settings.push_window_parsed
    days = settings.push_days_parsed
    local = now.astimezone(tz)
    if local.isoweekday() in days and start <= local.time() < end:
        return None
    day = local.date()
    if local.time() >= start:
        day += timedelta(days=1)
    for _ in range(8):
        if day.isoweekday() in days:
            return datetime.combine(day, start, tzinfo=tz)
        day += timedelta(days=1)
    return None  # parse_days refuses an empty set; unreachable


def _day_start(now: datetime) -> datetime:
    local = now.astimezone(settings.tz)
    return datetime.combine(local.date(), time(0), tzinfo=settings.tz)


def payload_problems(
    payload: Mapping[str, Any],
    *,
    display_name: str,
    phone: str | None,
    stated_m3: Decimal | None,
    stored: bool = False,
) -> list[str]:
    """Item 11's invariants, whoever built the body (04 §7, 05 §B.1): the
    identity fields present; ``name`` the company's display name only;
    ``phone`` E.164 and the lead contact's (``phone=None``: item 4 refuses
    the missing contact, not this); a note within ``NOTE_MAX``
    UTF-16 units whose «Masʼul» line names the person and carries no phone,
    handle or messenger id (``pseudonyms.unmasked``); ``kub`` only the stated
    volume, as a plain number; no pseudonym token, no NUL, no lone surrogate
    in any value.

    ``stored``: the body is one this ``external_id`` already claimed and is
    re-sent identical (rule 6) — its name and ``kub`` were measured against
    the facts of that claim, so a rename or a larger stated volume since is
    no refusal; the shape and the lead phone still are."""
    problems: list[str] = []
    missing = [k for k in PAYLOAD_IDENTITY if not isinstance(payload.get(k), str)]
    if missing:
        problems.append("missing:" + ",".join(missing))
    if not stored and payload.get("name") != display_name:
        problems.append("name")
    sent_phone = payload.get("phone")
    if not (isinstance(sent_phone, str) and _E164.match(sent_phone)) or (
        phone is not None and sent_phone != phone
    ):
        problems.append("phone")
    note = payload.get("note")
    if isinstance(note, str) and not (0 < text_len(note) <= NOTE_MAX):
        problems.append("note")
    if isinstance(note, str) and any(
        unmasked(line)
        for line in note.splitlines()
        if line.strip().startswith(MASUL_PREFIX.strip())
    ):
        problems.append("masul")
    kub = payload.get("kub")
    if kub is not None:
        try:
            ok = (
                isinstance(kub, str)
                and bool(_KUB.match(kub))
                and (stored or (stated_m3 is not None and Decimal(kub) == stated_m3))
            )
        except InvalidOperation:
            ok = False
        if not ok:
            problems.append("kub")
    for key, value in payload.items():
        if value is None:
            continue
        if not isinstance(value, str):
            problems.append(f"type:{key}")
        elif _PSEUDONYM.search(value):
            problems.append(f"pseudonym:{key}")
        elif _ILL_FORMED.search(value):
            problems.append(f"ill_formed:{key}")
    return problems


# --- the DB half ----------------------------------------------------------------------


async def lead_contact(session: AsyncSession, candidate: LeadCandidate) -> Contact | None:
    """The contact whose phone is pushed: the candidate's own contact when it
    carries a phone; else the company lineage's phone contacts — a usable one
    first (``scoring.usable_phone``), a mobile before a landline, the oldest
    first."""
    if candidate.contact_id is not None:
        own = await session.get(Contact, candidate.contact_id, populate_existing=True)
        if own is not None and own.phone is not None:
            return own
    lineage = sorted(
        (await companies.lineages(session, [candidate.company_id]))[candidate.company_id]
    )
    rows = (
        await session.scalars(
            sa.select(Contact)
            .where(Contact.company_id.in_(lineage), Contact.phone.is_not(None))
            .order_by(Contact.created_at, Contact.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    if not rows:
        return None

    def rank(c: Contact) -> tuple[bool, bool, bool]:
        return (
            not usable_lead_phone(c),
            c.anonymised_at is not None,
            c.phone_kind != "mobile",
        )

    return sorted(rows, key=rank)[0]


def usable_lead_phone(contact: Contact | None) -> bool:
    """04 §1.1's usable phone (``scoring.usable_phone``) on a stored
    contact: what ``lead_contact`` ranks first, and what tells the owner
    card's handoff variant from the ✅ one (``services/cards``)."""
    return contact is not None and scoring.usable_phone(_view(contact))


def _view(c: Contact) -> scoring.ContactView:
    return scoring.ContactView(
        tier=c.tier,
        phone=c.phone,
        phone_kind=c.phone_kind,
        shared_phone=c.shared_phone,
        anonymised=c.anonymised_at is not None,
    )


def owner_word(note: str | None) -> str | None:
    """The owner's own word on a review row: the note before a refusal the
    push gate wrote after it (``OWNER_WORD_SEP``)."""
    if note is None:
        return None
    return note.split(OWNER_WORD_SEP, 1)[0]


def noted(previous: str | None, text: str) -> str:
    """The note a refusal leaves on an owner row: the refusal, after the
    owner's ``shared_ok`` when the row carries it, within the column."""
    if owner_word(previous) == SHARED_OK:
        return (SHARED_OK + OWNER_WORD_SEP + text)[:REVIEW_NOTE_MAX]
    return text[:REVIEW_NOTE_MAX]


async def _shared_ok(session: AsyncSession, candidate: LeadCandidate) -> bool:
    """The owner's newest approval carries ``shared_ok`` (04 §7 item 4)."""
    note = await session.scalar(
        sa.select(LeadReview.note)
        .where(
            LeadReview.candidate_id == candidate.id,
            LeadReview.kind == "owner",
            LeadReview.state == "approved",
        )
        .order_by(LeadReview.answered_at.desc().nulls_last(), LeadReview.id.desc())
        .limit(1)
    )
    return owner_word(note) == SHARED_OK


def _contact_refusals(contact: Contact | None, *, shared_ok: bool) -> list[Refusal]:
    if contact is None or contact.phone is None:
        return [Refusal(4, "no_phone")]
    if contact.anonymised_at is not None:
        return [Refusal(4, "anonymised")]
    if not contact.phone.startswith(scoring.UZ_PREFIX):
        return [Refusal(4, "not_uz")]
    if contact.phone_kind not in UZ_MOBILE_OR_LANDLINE:
        return [Refusal(4, "not_usable")]
    if contact.shared_phone and not shared_ok:
        return [Refusal(4, "shared_phone")]
    return []


async def stated_m3(
    session: AsyncSession, lineage: Sequence[uuid.UUID]
) -> Decimal | None:
    """The stated volume a ``kub`` may carry (item 11): the largest stated
    amount over the lineage — the one home ``crm_push`` reads too."""
    value = await session.scalar(
        sa.select(sa.func.max(Signal.value)).where(
            Signal.company_id.in_(list(lineage)),
            Signal.key.in_(sorted(STATED_KEYS)),
        )
    )
    return Decimal(value) if value is not None else None


async def _attempt_refusals(
    session: AsyncSession,
    external_id: str,
    *,
    sha: str | None,
    now: datetime,
    plan: Plan,
) -> None:
    rows = (
        await session.scalars(
            sa.select(PushLog)
            .where(PushLog.external_id == external_id)
            .order_by(PushLog.attempt)
            .execution_options(populate_existing=True)
        )
    ).all()
    if not rows:
        plan.attempt = 1
        return
    live = [r for r in rows if r.superseded_at is None]
    if any(r.status in e.PUSH_DELIVERED_STATUSES for r in live):
        plan.refusals.append(Refusal(6, "delivered"))
        return
    if any(r.status == "sending" for r in live):
        plan.refusals.append(Refusal(6, "in_flight"))
        return
    last = rows[-1]
    plan.attempt = last.attempt + 1
    plan.stored = sha is not None and any(r.payload_sha256 == sha for r in rows)
    if sha is not None and any(r.payload_sha256 != sha for r in rows):
        plan.refusals.append(Refusal(6, "payload_changed"))
    if plan.attempt > MAX_ATTEMPTS:
        plan.refusals.append(Refusal(6, "attempts_spent"))
        return
    if last.status == "failed" and last.superseded_at is None:
        ready = (last.finished_at or last.sent_at) + ATTEMPT_DELAYS[plan.attempt]
        if now < ready:
            plan.refusals.append(Refusal(6, "attempt_wait", until=ready))


async def _caps(
    session: AsyncSession, external_id: str, now: datetime, plan: Plan
) -> None:
    counted = sa.and_(
        PushLog.status.in_(gates.PUSH_COUNTED_STATUSES),
        PushLog.superseded_at.is_(None),
        PushLog.external_id != external_id,
    )
    today = await session.scalar(
        sa.select(sa.func.count(PushLog.id)).where(
            counted, PushLog.sent_at >= _day_start(now), PushLog.sent_at <= now
        )
    )
    if (today or 0) >= settings.push_daily_cap:
        plan.refusals.append(Refusal(9, "cap"))
    rolling = await session.scalar(
        sa.select(sa.func.count(PushLog.id)).where(
            counted,
            PushLog.sent_at > now - SOURCE_CAP_WINDOW,
            PushLog.source_key_sent == settings.crm_push_source,
        )
    )
    if (rolling or 0) >= SOURCE_CAP:
        plan.refusals.append(Refusal(9, "source_cap"))


async def evaluate(
    session: AsyncSession,
    candidate: LeadCandidate,
    *,
    now: datetime,
    payload: Mapping[str, Any] | None,
    repush_of: PushLog | None = None,
) -> Plan:
    """The fourteen items over ``candidate`` (locked by the caller)."""
    plan = Plan()
    refuse = plan.refusals.append
    company = await session.get(Company, candidate.company_id, populate_existing=True)
    if company is None:
        raise LookupError(f"company {candidate.company_id} is gone")
    # 12. the id: the root company's, this month — or the re-pushed row's.
    if repush_of is not None:
        external_id = repush_of.external_id
        if repush_of.candidate_id != candidate.id or not external_id.startswith(
            f"cs:{company.id}:"
        ):
            refuse(Refusal(12, "external_id"))
    else:
        external_id = external_id_for(company.id, now)
    if company.state == "merged" or company.merged_into is not None:
        refuse(Refusal(12, "not_root"))
    if payload is not None and payload.get("external_id") != external_id:
        refuse(Refusal(12, "external_id"))
    plan.external_id = external_id
    # 1. approved (a re-push of its own id: pushed too).
    allowed = ("approved", "pushed") if repush_of is not None else ("approved",)
    if candidate.state not in allowed or not candidate.approved_by:
        refuse(Refusal(1, "not_approved"))
    # 2. the switches and the list's freshness.
    if not settings.crm_push_enabled:
        refuse(Refusal(2, "disabled"))
    if await global_pause(session):
        refuse(Refusal(2, "paused"))
    breaker = await session.scalar(
        sa.select(Source.breaker_until).where(Source.key == erp_feeds.ERP_SOURCE)
    )
    if breaker is not None and breaker > now:
        refuse(Refusal(2, "breaker", until=breaker))
    if not (await erp_feeds.suppression_probe(session, now)).fresh:
        refuse(Refusal(2, "stale"))
    # 4. the lead contact.
    contact = await lead_contact(session, candidate)
    plan.contact = contact
    for r in _contact_refusals(contact, shared_ok=await _shared_ok(session, candidate)):
        refuse(r)
    tail = (
        contact.phone_tail.strip()
        if contact is not None and contact.phone_tail is not None
        else None
    )
    plan.tail = tail
    tails = [tail] if tail else []
    # 3 and 5. the gates on live data; the suppression hit is item 5's.
    got = await gates.inputs_for(session, company.id, now=now, tails=tails)
    judged = gates.run_gates(
        got.company,
        got.contacts,
        got.signals,
        got.verdict,
        None,
        gates.Recent(rejected_until=got.recent.rejected_until),
        now,
    )
    for failure in judged.failures:
        if failure.gate != "is_b2b":
            refuse(Refusal(3, failure.reason, until=failure.until, detail=failure.gate))
    hit = got.hit
    if hit is not None:
        if hit.permanent:
            refuse(Refusal(5, "suppressed", hit=hit, detail=hit.kind))
        else:
            refuse(Refusal(5, "cooldown", until=hit.expires_at, hit=hit))
    # 6. the id's own attempts.
    sha = canonical_sha256(payload) if payload is not None else None
    plan.payload_sha256 = sha
    await _attempt_refusals(session, external_id, sha=sha, now=now, plan=plan)
    # 7 and 8. the windows of 02 §2.10 rule 2 (never this id's own rows).
    windows = await gates.recent_pushes(
        session,
        lineage=got.lineage,
        tails=tails,
        now=now,
        exclude_external_id=external_id,
    )
    if windows.phone_push_until is not None and windows.phone_push_until > now:
        refuse(Refusal(7, "phone_recent", until=windows.phone_push_until))
    if windows.company_push_until is not None and windows.company_push_until > now:
        refuse(Refusal(8, "company_recent", until=windows.company_push_until))
    # 9. the caps.
    await _caps(session, external_id, now, plan)
    # 10. the window.
    opens = window_opens(now)
    if opens is not None:
        refuse(Refusal(10, "window", until=opens))
    # 11. the payload.
    if payload is None:
        refuse(Refusal(11, "no_payload"))
    else:
        problems = payload_problems(
            {k: v for k, v in payload.items() if k != "external_id"}
            | {"external_id": external_id},
            display_name=company.display_name,
            phone=contact.phone if contact is not None else None,
            stated_m3=await stated_m3(session, got.lineage),
            stored=plan.stored,
        )
        if problems:
            refuse(Refusal(11, "payload", detail=";".join(problems)))
    # 13. the score at push time.
    if candidate.score is None or candidate.score < MIN_SCORE:
        refuse(Refusal(13, "score_low"))
    plan.refusals.sort(key=lambda r: r.check)
    return plan


async def check(
    session: AsyncSession,
    candidate: LeadCandidate,
    *,
    now: datetime,
    payload: Mapping[str, Any] | None,
    repush_of: PushLog | None = None,
) -> list[Refusal]:
    """04 §7's ``push_gate.check``: empty = the push may go."""
    plan = await evaluate(
        session, candidate, now=now, payload=payload, repush_of=repush_of
    )
    return plan.refusals


async def lock_candidate(
    session: AsyncSession, candidate_id: uuid.UUID
) -> LeadCandidate | None:
    """``SELECT … FOR UPDATE`` on the candidate (02 §2.10 rule 2)."""
    found: LeadCandidate | None = await session.scalar(
        sa.select(LeadCandidate)
        .where(LeadCandidate.id == candidate_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return found


# How often a candidate a merge moved between the read and the locks is read
# again before the push gives up for this run.
LOCK_TRIES: Final = 3


async def lock_for_push(
    session: AsyncSession, candidate_id: uuid.UUID
) -> LeadCandidate | None:
    """The candidate ``FOR UPDATE`` after its company ``FOR NO KEY UPDATE`` —
    the lock order of ``services/candidates`` (companies before
    lead_candidates). The push transaction may write the company (gate 4's
    ``state='client'`` on a client hit, ``record_refusals``), and a writer
    that holds the company (the resolver, ``signals.emit``) marks its
    candidate stale next: taken the other way round, the two deadlock. A
    candidate a merge moved between the read and the locks is read again;
    None = no candidate, or one that kept moving (the next run asks)."""
    for _ in range(LOCK_TRIES):
        company_id = await lock_company_of(session, candidate_id)
        if company_id is None:
            return None
        candidate = await lock_candidate(session, candidate_id)
        if candidate is None or candidate.company_id == company_id:
            return candidate
    return None


async def lock_company_of(
    session: AsyncSession, candidate_id: uuid.UUID
) -> uuid.UUID | None:
    """The candidate's company ``FOR NO KEY UPDATE`` (``lock_for_push``'s
    first step), and its id; None = no such candidate. The candidate itself is
    read without a lock: the caller takes it next and compares."""
    company_id: uuid.UUID | None = await session.scalar(
        sa.select(LeadCandidate.company_id).where(LeadCandidate.id == candidate_id)
    )
    if company_id is not None:
        await session.execute(
            sa.select(Company.id)
            .where(Company.id == company_id)
            .with_for_update(key_share=True)
        )
    return company_id


async def _note_row(session: AsyncSession, candidate: LeadCandidate) -> LeadReview | None:
    """The owner review row a refusal is written on: the live card, else the
    newest answered one. An auto-approved candidate never had a card, and no
    row is made for it — a row with no tap would read as the owner's
    decision (``/lidlar``, the reports); its refusals go to
    ``lead_candidates.push_note`` (0004, T-21), which ``/lidlar holat``
    lists."""
    row: LeadReview | None = await session.scalar(
        sa.select(LeadReview)
        .where(LeadReview.candidate_id == candidate.id, LeadReview.kind == "owner")
        .order_by(
            LeadReview.state.in_(e.REVIEW_LIVE_STATES).desc(),
            LeadReview.created_at.desc(),
            LeadReview.id.desc(),
        )
        .limit(1)
    )
    return row


def _joined(refusals: Sequence[Refusal]) -> str:
    return "; ".join(dict.fromkeys(r.text for r in refusals))[:REVIEW_NOTE_MAX]


async def _write_note(
    session: AsyncSession, candidate: LeadCandidate, note: str, *, now: datetime
) -> None:
    row = await _note_row(session, candidate)
    if row is None:
        log.info(
            "push refused",
            extra={"candidate_id": str(candidate.id), "refusal": note[:80]},
        )
        candidate.push_note = note[:REVIEW_NOTE_MAX]
        candidate.push_noted_at = now
        return
    row.note = noted(row.note, note)


async def record_refusals(
    session: AsyncSession,
    candidate: LeadCandidate,
    refusals: Sequence[Refusal],
    *,
    now: datetime,
) -> None:
    """Item 14: the owner-visible refusals as ``lead_reviews.note``, and what
    they mean for an ``approved`` candidate — a permanent suppression makes
    it ``suppressed`` (a ``client`` hit also writes gate 4's
    ``companies.state='client'``); a refusal only the owner can resolve sends
    it back to ``review`` with the note on its new card («Ball tushdi», 04 §7
    item 13). Everything else leaves it approved for the job's next run."""
    visible = [r for r in refusals if not r.silent]
    if not visible:
        return
    note = _joined(visible)
    suppressed = next((r for r in visible if r.code == "suppressed" and r.hit), None)
    if candidate.state == "approved" and suppressed is not None and suppressed.hit:
        await _write_note(session, candidate, note, now=now)
        if suppressed.hit.kind in CLIENT_KINDS:
            await gates.apply_effects(
                session,
                candidate.company_id,
                gates.Failure("not_client", "suppressed", "client", hit=suppressed.hit),
            )
        await candidates.transition(
            session, candidate, "suppressed", now=now, suppressed_by=suppressed.hit.id
        )
        return
    if candidate.state == "approved" and any(r.code in TO_REVIEW for r in visible):
        await candidates.transition(session, candidate, "review", now=now)
        live: LeadReview | None = await session.scalar(
            sa.select(LeadReview).where(
                LeadReview.candidate_id == candidate.id,
                LeadReview.kind == "owner",
                LeadReview.state.in_(e.REVIEW_LIVE_STATES),
            )
        )
        if live is None:
            live = LeadReview(candidate_id=candidate.id, kind="owner", state="pending")
            session.add(live)
        live.note = note
        await session.flush()
        return
    await _write_note(session, candidate, note, now=now)
    await session.flush()


async def claim(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    now: datetime,
    payload: Mapping[str, Any] | None,
    endpoint: str,
    repush_of: int | None = None,
    skip_checks: frozenset[int] = frozenset(),
    record: bool = True,
) -> ClaimResult:
    """Lock, check and claim one ``push_log`` row (``status='sending'``) in
    the caller's transaction — or record the refusals. The caller commits
    before it sends (02 §2.10 rule 3).

    ``skip_checks`` drops the refusals of those items — only ``make
    push-test``'s synthetic company, which has no score (item 13, 05 §B.1);
    ``record=False`` returns the refusals without writing them (the same
    tool rolls its synthetic company back instead)."""
    candidate = await lock_for_push(session, candidate_id)
    if candidate is None:
        return ClaimResult(refusals=(Refusal(1, "not_approved"),))
    prior = await session.get(PushLog, repush_of) if repush_of is not None else None
    if repush_of is not None and prior is None:
        return ClaimResult(refusals=(Refusal(12, "external_id"),))
    plan = await evaluate(session, candidate, now=now, payload=payload, repush_of=prior)
    plan.refusals = [r for r in plan.refusals if r.check not in skip_checks]
    if plan.refusals:
        if record:
            await record_refusals(session, candidate, plan.refusals, now=now)
        return ClaimResult(refusals=tuple(plan.refusals))
    assert payload is not None and plan.tail is not None and plan.external_id
    assert plan.payload_sha256 is not None
    row = PushLog(
        external_id=plan.external_id,
        attempt=plan.attempt,
        candidate_id=candidate.id,
        company_id=candidate.company_id,
        phone_tail=plan.tail,
        endpoint=endpoint,
        source_key_sent=settings.crm_push_source,
        payload=dict(payload),
        payload_sha256=plan.payload_sha256,
        status="sending",
        sent_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # uq_push_log_external_attempt / ux_push_log_delivered: another
        # claim got there (the lock makes this a belt, not the fence).
        return ClaimResult(refusals=(Refusal(6, "in_flight"),))
    return ClaimResult(
        push_log_id=row.id, external_id=plan.external_id, attempt=plan.attempt
    )


__all__ = [
    "ATTEMPT_DELAYS",
    "LOCK_TRIES",
    "MASUL_PREFIX",
    "MAX_ATTEMPTS",
    "MIN_SCORE",
    "NOTE_MAX",
    "REFUSAL_UZ",
    "SOURCE_CAP",
    "ClaimResult",
    "Plan",
    "Refusal",
    "canonical_sha256",
    "check",
    "claim",
    "evaluate",
    "external_id_for",
    "lead_contact",
    "lock_candidate",
    "lock_company_of",
    "lock_for_push",
    "noted",
    "owner_word",
    "payload_problems",
    "record_refusals",
    "stated_m3",
    "text_len",
    "usable_lead_phone",
    "window_opens",
]
