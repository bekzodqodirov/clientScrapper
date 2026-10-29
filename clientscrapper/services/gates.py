"""The seven gates (04 §6): may this company be carded, and pushed?

``run_gates`` is pure. It judges every gate — so the breakdown and the card
can show each one — and the **first failing gate decides**, in the table's
order:

1. ``is_b2b`` — a legal form in a name, a STIR, a business directory's id
   (``B2B_ID_TYPES``), B2B words in the company's own text, the classifier's
   B2B buyer (``confidence ≥ 0.7``), the verdict, two volume signals of two
   evidences within 90 days, or a repeat poster (``repeat_count_60d ≥ 3``).
   Else: a B2C hit with no volume signal → ``excluded(b2c)``; otherwise
   ``hold`` — it may become B2B after enrichment, so ``GateResult.decide``
   defers this one hold until the candidate is bound for a card or a push.
2. ``not_competitor`` → ``excluded(competitor)``, ``is_competitor`` and a
   ``suppression(competitor, company_id, source='resolver')`` row. A
   Telegram poster is a tout's company too when ``telegram_monitor`` judged
   one of the poster's later messages a tout's within ``TOUT_WINDOW`` of a
   post the company stands on (``tout_poster``): the collector counts price
   lists up to each message, so a live tout's first two lists stand as asks
   and only the third is caught (03 §2.3) — the gate, at card and push
   time, reads the windows that end after the post.
3. ``not_supplier`` → ``excluded(supplier_cn)``, ``is_supplier_cn``.
4. ``not_client`` → ``suppressed`` by the client row, ``companies.state =
   'client'``.
5. ``not_dnc`` → ``suppressed`` by the row.
6. ``not_recent`` → ``hold`` until the earliest window ends: a live
   cooldown, an open push to the company's lineage in 90 days, a push to one
   of its phones in 24 hours, the last candidate rejected in 180 days.
7. ``active_entity`` → liquidated (the registry, or Places' «closed»
   signal) or a state body in a **name** → ``excluded``; ``inactive`` →
   ``hold``. A state word only in the text is scoring's −20, never this.

**Self-description only.** The competitor and B2C regexes read the entity's
own words — its names, and the observations it wrote about itself
(``signals.SELF_DESCRIPTION_KINDS``: a profile, a channel's own post, a
listing, a site, a directory card …) — never a buyer's question: «kim kargo
qiladi?» is a buyer (``intent_post``). Gate 2 reads a name with
``COMPETITOR_NAME_RE`` (the cargo core and the logistics words) and a
self-description with ``COMPETITOR_SELF_RE`` (the cargo core only): every
importer writes «Доставка по Ташкенту» or «yetkazib beramiz». Gate 7 reads a
name with ``STATE_BODY_NAME_RE``, a state body by its shape («… hokimligi»,
«Davlat … qoʻmitasi», «5-maktab»), never by a stem a founder's name or an
equipment trader carries («Davlatbek», «Maktab Mebel Import»). Gate 3's UZ
address is one ``normalise.canonical_city`` reads an Uzbek city or Tashkent
place in.

The DB half (``inputs_for``/``evaluate``) reads all of it over the company's
lineage; ``apply_effects`` writes what the deciding gate implies of the
company; ``services/candidates`` moves the candidate (score_job), the push
gate re-runs gates 2–7 inside its transaction (``services/push_gate``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors.keywords import (
    B2B_RE,
    B2C_RE,
    CJK_RE,
    CN_COUNTRY_NAMES,
    COMPETITOR_NAME_RE,
    COMPETITOR_SELF_RE,
    LEGAL_FORM_RE,
    STATE_BODY_NAME_RE,
    SUPPLIER_NAME_RE,
)
from clientscrapper.collectors.telegram_monitor import TOUT_WINDOW
from clientscrapper.config import settings
from clientscrapper.db import enums as e
from clientscrapper.db.models import (
    ChatMonitor,
    Company,
    CompanyAlias,
    CompanyId,
    Contact,
    Interaction,
    LeadCandidate,
    ModelJob,
    Observation,
    Outcome,
    PushLog,
    Signal,
)
from clientscrapper.services import companies, normalise, signals, suppression
from clientscrapper.services.suppression import Hit

Decision = Literal["pass", "hold", "excluded", "suppressed"]

GATES: Final = (
    "is_b2b",
    "not_competitor",
    "not_supplier",
    "not_client",
    "not_dnc",
    "not_recent",
    "active_entity",
)
# Gate 1: the platforms whose card is a business (04 §6).
B2B_ID_TYPES: Final = frozenset(
    {
        "hh_employer",
        "gis2_firm",
        "google_place",
        "uzum_shop",
        "goldenpages",
        "yellowpages",
        "glotr",
        "prom",
    }
)
INTENT_B2B_CONFIDENCE: Final = 0.7
VOLUME_SIGNALS_MIN: Final = 2
VOLUME_WINDOW: Final = timedelta(days=90)
REPEAT_POSTS_MIN: Final = 3
# Gate 6 and the push gate's checks 7/8 (02 §2.10 rule 2): one home.
OPEN_PUSH_WINDOW: Final = timedelta(days=90)
PHONE_PUSH_WINDOW: Final = timedelta(hours=24)
# What counts as a push for those windows and the push gate's caps: a
# delivered row (``sent|unknown``, 02 §2.10 rule 2) and one in flight — a
# committed ``sending`` row whose POST has not answered may land (rule 5 (a)
# turns it ``unknown`` after 10 minutes), so a second push beside it would
# be one more for the same phone, company or day (T-19 review).
PUSH_COUNTED_STATUSES: Final = (*e.PUSH_DELIVERED_STATUSES, "sending")
# The model's job kind whose result is an ImportSignal (04 §4.3).
CLASSIFY_KIND: Final = "classify"
CLASSIFY_DONE: Final = ("applied", "fallback")
# The site enrichment fetches (04 §3 E2): its title is the «website title».
SITE_SOURCE: Final = "enrich_site"
SUPPLIER_HOSTS: Final = ("1688.com", "alibaba.com")
# An exhibitor row's country, upper-cased (the spellings are keywords.py's).
CN_COUNTRIES: Final = CN_COUNTRY_NAMES
LIQUIDATED_KEY: Final = "neg_liquidated"
COMPETITOR_FLAG: Final = "flag_competitor"
SUPPLIER_FLAG: Final = "flag_supplier_cn"
COMPETITOR_SOURCE: Final = "resolver"

# The card's gate words (04 §6 «Darvozalar: …», 06 §1.3).
LINES_PREFIX: Final = "Darvozalar: "
B2B_WHY_UZ: Final[Mapping[str, str]] = {
    "stir": "STIR",
    "id": "katalog",
    "b2b_words": "optom",
    "intent_b2b": "xaridor",
    "verdict": "tahlil",
    "volume": "hajm",
    "repeat": "takroriy",
}
REGISTRY_UZ: Final[Mapping[str, str]] = {
    "active": "faol",
    "inactive": "faol emas",
    "liquidated": "tugatilgan",
    "unknown": "nomaʼlum",
}


# --- the inputs -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnText:
    """One self-description: what the company wrote about itself."""

    text: str
    kind: str
    title: str | None = None
    source_key: str | None = None


@dataclass(frozen=True, slots=True)
class ImportView:
    """What the gate reads of an ImportSignal (04 §4.3)."""

    speaker_role: str = "unknown"
    b2b: bool = False
    b2c_markers: bool = False
    confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class GateSignal:
    key: str
    evidence_id: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class GateContact:
    phone: str | None = None
    anonymised: bool = False


@dataclass(frozen=True, slots=True)
class GateCompany:
    """The company as the gates read it, over its lineage."""

    id: uuid.UUID
    display_name: str
    names: tuple[str, ...] = ()
    legal_form: str | None = None
    stir: str | None = None
    address: str | None = None
    city: str | None = None
    registry_status: str = "unknown"
    state: str = "active"
    is_competitor: bool = False
    is_supplier_cn: bool = False
    id_types: frozenset[str] = frozenset()
    own: tuple[OwnText, ...] = ()
    urls: tuple[str, ...] = ()
    import_signals: tuple[ImportView, ...] = ()
    competitor_channel: bool = False
    tout_poster: bool = False
    repeat_count_60d: int = 0
    exhibitor_countries: frozenset[str] = frozenset()

    def all_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.display_name, *self.names)))


@dataclass(frozen=True, slots=True)
class Recent:
    """When gate 6's windows end (None: no such window open)."""

    company_push_until: datetime | None = None
    phone_push_until: datetime | None = None
    rejected_until: datetime | None = None


# --- the result -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Failure:
    gate: str
    decision: Decision
    reason: str
    until: datetime | None = None
    hit: Hit | None = None


@dataclass(frozen=True, slots=True)
class GateResult:
    """Every gate's mark (``score_breakdown.gates``), the failures in gate
    order, the card's words, and the suppression row that was read."""

    marks: Mapping[str, str]
    failures: tuple[Failure, ...]
    lines: tuple[str, ...]
    hit: Hit | None = None

    def decide(self, *, card: bool = True) -> Failure | None:
        """The first failing gate. Gate 1's hold (not yet B2B, no B2C) waits
        while the candidate is not bound for a card or a push (``card``):
        enrichment is what may make it B2B."""
        for failure in self.failures:
            if not card and failure.gate == "is_b2b" and failure.decision == "hold":
                continue
            return failure
        return None

    @property
    def decision(self) -> Decision:
        first = self.decide()
        return "pass" if first is None else first.decision

    def summary(self) -> str:
        return LINES_PREFIX + " · ".join(self.lines)


@dataclass(frozen=True, slots=True)
class _Verdict:
    mark: str
    line: str
    failure: Failure | None = None


# --- the gates (pure) -----------------------------------------------------------------


def _own_texts(company: GateCompany) -> list[str]:
    """Names and self-descriptions — what the B2C/B2B words are read on."""
    return [*company.all_names(), *(o.text for o in company.own if o.text)]


def _names_and_titles(company: GateCompany) -> list[str]:
    """What the company is called: its names over the lineage and the names
    its own pages carry (a profile's name, a site's title)."""
    return [*company.all_names(), *(o.title for o in company.own if o.title)]


def _search(pattern: Any, texts: Iterable[str]) -> str | None:
    for text in texts:
        match = pattern.search(text)
        if match is not None:
            return str(match.group(0))
    return None


def _volume_signals(signals_: Sequence[GateSignal]) -> list[GateSignal]:
    return [s for s in signals_ if s.key in signals.VOLUME_KEYS]


def gate_b2b(
    company: GateCompany,
    signals_: Sequence[GateSignal],
    verdict: Mapping[str, Any] | None,
    now: datetime,
) -> _Verdict:
    form = _search(LEGAL_FORM_RE, company.all_names()) or company.legal_form
    why: str | None = None
    label: str | None = None
    if form:
        why, label = "legal_form", form
    elif company.stir:
        why = "stir"
    elif company.id_types & B2B_ID_TYPES:
        why = "id"
    elif _search(B2B_RE, _own_texts(company)):
        why = "b2b_words"
    elif any(
        s.b2b and s.speaker_role == "buyer" and s.confidence >= INTENT_B2B_CONFIDENCE
        for s in company.import_signals
    ):
        why = "intent_b2b"
    elif verdict is not None and verdict.get("is_b2b") is True:
        why = "verdict"
    elif (
        len(
            {
                s.evidence_id
                for s in _volume_signals(signals_)
                if now - s.observed_at <= VOLUME_WINDOW
            }
        )
        >= VOLUME_SIGNALS_MIN
    ):
        why = "volume"
    elif company.repeat_count_60d >= REPEAT_POSTS_MIN:
        why = "repeat"
    if why is not None:
        shown = label or B2B_WHY_UZ[why]
        return _Verdict(f"pass:{why}", f"B2B ✓ ({shown})")
    b2c = _search(B2C_RE, _own_texts(company)) is not None or any(
        s.b2c_markers for s in company.import_signals
    )
    if b2c and not _volume_signals(signals_):
        return _Verdict(
            "fail:b2c",
            "B2B ✗ (shaxsiy xarid)",
            Failure("is_b2b", "excluded", "b2c"),
        )
    return _Verdict(
        "fail:not_b2b",
        "B2B ? (tasdiqlanmagan)",
        Failure("is_b2b", "hold", "not_b2b"),
    )


def gate_competitor(
    company: GateCompany,
    signals_: Sequence[GateSignal],
    verdict: Mapping[str, Any] | None,
) -> _Verdict:
    why: str | None = None
    if company.is_competitor:
        why = "flag"
    elif (
        _search(COMPETITOR_NAME_RE, _names_and_titles(company)) is not None
        or _search(COMPETITOR_SELF_RE, [o.text for o in company.own if o.text])
        is not None
    ):
        # A name read for logistics too, a self-description only for the
        # cargo core (keywords.COMPETITOR_SELF_RE: every importer delivers).
        why = "words"
    elif verdict is not None and verdict.get("is_competitor") is True:
        why = "verdict"
    elif any(s.key == COMPETITOR_FLAG for s in signals_):
        why = "signal"
    elif company.competitor_channel:
        why = "channel"
    elif company.tout_poster:
        why = "tout"
    if why is None:
        return _Verdict("pass", "raqobatchi emas ✓")
    return _Verdict(
        f"fail:{why}",
        "raqobatchi (kargo) ✗",
        Failure("not_competitor", "excluded", "competitor"),
    )


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def gate_supplier(
    company: GateCompany,
    contacts: Sequence[GateContact],
    signals_: Sequence[GateSignal],
    verdict: Mapping[str, Any] | None,
) -> _Verdict:
    uz_form = company.legal_form is not None or (
        _search(LEGAL_FORM_RE, company.all_names()) is not None
    )
    # A UZ address names an Uzbek city or Tashkent place (normalise's one
    # reading of an address); a Chinese exporter's card carries an address
    # too.
    uz_address = (
        normalise.canonical_city(company.city) is not None
        or normalise.canonical_city(company.address) is not None
    )
    why: str | None = None
    if company.is_supplier_cn:
        why = "flag"
    elif (
        _search(SUPPLIER_NAME_RE, company.all_names()) is not None
        and not uz_form
        and not uz_address
    ):
        why = "name"
    elif company.exhibitor_countries & CN_COUNTRIES:
        why = "exhibitor"
    elif any(
        c.phone is not None and c.phone.startswith("+86") and not c.anonymised
        for c in contacts
    ):
        why = "phone"
    elif any(
        o.source_key == SITE_SOURCE and o.title and CJK_RE.search(o.title)
        for o in company.own
    ):
        why = "site_title"
    elif any(
        _host(u) == h or _host(u).endswith("." + h)
        for u in company.urls
        for h in SUPPLIER_HOSTS
    ):
        why = "seller_url"
    elif verdict is not None and verdict.get("is_supplier_cn") is True:
        why = "verdict"
    elif any(s.key == SUPPLIER_FLAG for s in signals_):
        why = "signal"
    if why is None:
        return _Verdict("pass", "Xitoy yetkazib beruvchisi emas ✓")
    return _Verdict(
        f"fail:{why}",
        "Xitoy yetkazib beruvchisi ✗",
        Failure("not_supplier", "excluded", "supplier_cn"),
    )


def gate_client(company: GateCompany, hit: Hit | None) -> _Verdict:
    if hit is not None and hit.kind in suppression.CLIENT_KINDS:
        return _Verdict(
            "fail:client",
            "mijoz ✗",
            Failure("not_client", "suppressed", "client", hit=hit),
        )
    if company.state == "client":
        # A client company whose phone this lookup does not hold (an older
        # pushed number): not a lead, but no row to point a suppression at.
        return _Verdict(
            "fail:client_state", "mijoz ✗", Failure("not_client", "hold", "client_state")
        )
    return _Verdict("pass", "mijoz emas ✓")


def gate_dnc(hit: Hit | None) -> _Verdict:
    if hit is not None and hit.kind in suppression.DNC_KINDS:
        return _Verdict(
            f"fail:{hit.kind}",
            f"DNC ✗ ({hit.kind})",
            Failure("not_dnc", "suppressed", hit.kind, hit=hit),
        )
    return _Verdict("pass", "DNC yoʻq ✓")


def gate_recent(hit: Hit | None, recent: Recent, now: datetime) -> _Verdict:
    windows: list[tuple[str, datetime]] = []
    if hit is not None and hit.kind == suppression.COOLDOWN and hit.expires_at:
        windows.append(("cooldown", hit.expires_at))
    for why, until in (
        ("company_push", recent.company_push_until),
        ("phone_push", recent.phone_push_until),
        ("rejected", recent.rejected_until),
    ):
        if until is not None and until > now:
            windows.append((why, until))
    if not windows:
        return _Verdict("pass", "90 kunda yuborilmagan ✓")
    why, until = min(windows, key=lambda w: w[1])
    return _Verdict(
        f"fail:{why}",
        f"kutish: {until.astimezone(settings.tz).strftime('%d.%m.%Y')} gacha",
        Failure("not_recent", "hold", why, until=until, hit=hit),
    )


def gate_active(company: GateCompany, signals_: Sequence[GateSignal]) -> _Verdict:
    status = company.registry_status
    if status == "liquidated" or any(s.key == LIQUIDATED_KEY for s in signals_):
        return _Verdict(
            "fail:liquidated",
            "reestr: tugatilgan ✗",
            Failure("active_entity", "excluded", "liquidated"),
        )
    if _search(STATE_BODY_NAME_RE, company.all_names()) is not None:
        return _Verdict(
            "fail:state_body",
            "davlat tashkiloti ✗",
            Failure("active_entity", "excluded", "state_body"),
        )
    if status == "inactive":
        return _Verdict(
            "fail:inactive",
            "reestr: faol emas",
            Failure("active_entity", "hold", "inactive"),
        )
    word = REGISTRY_UZ.get(status, REGISTRY_UZ["unknown"])
    return _Verdict("pass:active" if status == "active" else "unknown", f"reestr: {word}")


def run_gates(
    company: GateCompany,
    contacts: Sequence[GateContact],
    signals_: Sequence[GateSignal],
    verdict: Mapping[str, Any] | None,
    suppression_hit: Hit | None,
    recent: Recent,
    now: datetime,
) -> GateResult:
    """Judge the seven gates of 04 §6 (pure)."""
    judged = {
        "is_b2b": gate_b2b(company, signals_, verdict, now),
        "not_competitor": gate_competitor(company, signals_, verdict),
        "not_supplier": gate_supplier(company, contacts, signals_, verdict),
        "not_client": gate_client(company, suppression_hit),
        "not_dnc": gate_dnc(suppression_hit),
        "not_recent": gate_recent(suppression_hit, recent, now),
        "active_entity": gate_active(company, signals_),
    }
    return GateResult(
        marks={g: judged[g].mark for g in GATES},
        failures=tuple(v.failure for g in GATES if (v := judged[g]).failure is not None),
        lines=tuple(judged[g].line for g in GATES),
        hit=suppression_hit,
    )


# --- the DB half ----------------------------------------------------------------------


def _section(parsed: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = parsed.get(name)
    return value if isinstance(value, Mapping) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _attrs(parsed: Mapping[str, Any]) -> Mapping[str, Any]:
    collector = _section(_section(parsed, "hints"), "collector")
    return _section(collector, "attrs")


def _import_view(result: Any) -> ImportView | None:
    if not isinstance(result, Mapping):
        return None
    confidence = result.get("confidence")
    return ImportView(
        speaker_role=str(result.get("speaker_role") or "unknown"),
        b2b=result.get("b2b") is True,
        b2c_markers=result.get("b2c_markers") is True,
        confidence=float(confidence)
        if isinstance(confidence, int | float) and not isinstance(confidence, bool)
        else 0.0,
    )


@dataclass(slots=True)
class Inputs:
    """Everything ``run_gates`` reads, loaded for one company."""

    company: GateCompany
    contacts: list[GateContact]
    signals: list[GateSignal]
    verdict: Mapping[str, Any] | None
    hit: Hit | None
    recent: Recent
    lineage: list[uuid.UUID] = field(default_factory=list)
    tails: list[str] = field(default_factory=list)


async def lineage_tails(session: AsyncSession, lineage: Sequence[uuid.UUID]) -> list[str]:
    """The live contacts' phone tails over the lineage."""
    found = await session.scalars(
        sa.select(Contact.phone_tail)
        .where(
            Contact.company_id.in_(list(lineage)),
            Contact.anonymised_at.is_(None),
            Contact.phone_tail.is_not(None),
        )
        .distinct()
    )
    return sorted(t.strip() for t in found if t)


async def recent_pushes(
    session: AsyncSession,
    *,
    lineage: Sequence[uuid.UUID],
    tails: Sequence[str],
    now: datetime,
    exclude_external_id: str | None = None,
) -> Recent:
    """Gate 6's windows and the push gate's checks 7/8 (02 §2.10 rule 2):
    pushes counted only while ``PUSH_COUNTED_STATUSES`` (delivered or in
    flight) and not superseded, and — for a re-push — never the push's own
    ``external_id``."""
    from clientscrapper.services import candidates  # it imports this module

    delivered = sa.and_(
        PushLog.status.in_(PUSH_COUNTED_STATUSES),
        PushLog.superseded_at.is_(None),
    )
    if exclude_external_id is not None:
        delivered = sa.and_(delivered, PushLog.external_id != exclude_external_id)
    company_rows = (
        await session.execute(
            sa.select(PushLog.sent_at, Outcome)
            .outerjoin(Outcome, Outcome.external_id == PushLog.external_id)
            .where(
                delivered,
                PushLog.company_id.in_(list(lineage)),
                PushLog.sent_at > now - OPEN_PUSH_WINDOW,
            )
        )
    ).all()
    open_sent = [
        r.sent_at
        for r in company_rows
        if candidates.outcome_terminal(r.Outcome) is not True
    ]
    phone_sent = (
        await session.scalar(
            sa.select(sa.func.max(PushLog.sent_at)).where(
                delivered,
                PushLog.phone_tail.in_(list(tails)),
                PushLog.sent_at > now - PHONE_PUSH_WINDOW,
            )
        )
        if tails
        else None
    )
    rejected = await session.scalar(
        sa.select(sa.func.max(LeadCandidate.rejected_at)).where(
            LeadCandidate.company_id.in_(list(lineage)),
            LeadCandidate.state == "rejected",
            LeadCandidate.rejected_at > now - candidates.REOPEN_AFTER_REJECT,
        )
    )
    return Recent(
        company_push_until=max(open_sent) + OPEN_PUSH_WINDOW if open_sent else None,
        phone_push_until=phone_sent + PHONE_PUSH_WINDOW if phone_sent else None,
        rejected_until=rejected + candidates.REOPEN_AFTER_REJECT if rejected else None,
    )


async def _tout_poster(session: AsyncSession, lineage: Sequence[uuid.UUID]) -> bool:
    """Whether a Telegram person the lineage's posts come from had a later
    message ``telegram_monitor`` judged a tout's (``companies.tout_refusal``
    reads its mark) within ``TOUT_WINDOW`` after one of those posts: a
    window that holds the post, as 03 §2.3 counts them. The collector's
    verdict is the one home of the tout rule; the gate only reads it."""
    posts = (
        await session.execute(
            sa.select(Interaction.sender_id, Interaction.occurred_at)
            .join(Observation, Observation.id == Interaction.observation_id)
            .where(
                Observation.company_id.in_(lineage),
                Observation.status == "resolved",
                Interaction.sender_id > 0,
            )
        )
    ).all()
    if not posts:
        return False
    times: dict[int, list[datetime]] = {}
    for sender, at in posts:
        times.setdefault(int(sender), []).append(at)
    later = await session.execute(
        sa.select(Interaction.sender_id, Interaction.occurred_at, Observation.parsed)
        .join(Observation, Observation.id == Interaction.observation_id)
        .where(
            Interaction.sender_id.in_(sorted(times)),
            Interaction.occurred_at > min(at for _, at in posts),
            Interaction.occurred_at < max(at for _, at in posts) + TOUT_WINDOW,
        )
    )
    for sender, at, parsed in later.all():
        if not isinstance(parsed, Mapping) or companies.tout_refusal(parsed) is None:
            continue
        if any(t < at < t + TOUT_WINDOW for t in times.get(int(sender), ())):
            return True
    return False


async def inputs_for(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    now: datetime,
    tails: Iterable[str] = (),
) -> Inputs:
    """Load what the gates read of ``company_id`` over its lineage."""
    company = await session.get(Company, company_id, populate_existing=True)
    if company is None:
        raise LookupError(f"company {company_id} is gone")
    lineage = sorted((await companies.lineages(session, [company_id]))[company_id])
    members = Company.id.in_(lineage)
    rows = (await session.scalars(sa.select(Company).where(members))).all()
    names = list(
        await session.scalars(
            sa.select(CompanyAlias.alias)
            .where(CompanyAlias.company_id.in_(lineage))
            .order_by(CompanyAlias.id)
        )
    )
    names.extend(r.display_name for r in rows if r.id != company.id)
    ids = (
        await session.execute(
            sa.select(CompanyId.id_type, CompanyId.id_value).where(
                CompanyId.company_id.in_(lineage)
            )
        )
    ).all()
    tg_chats = [int(v) for t, v in ids if t == "tg_chat" and v.lstrip("-").isdigit()]
    channels = sorted({r.telegram_channel for r in rows if r.telegram_channel})
    competitor_channel = False
    if channels or tg_chats:
        on = []
        if channels:
            on.append(sa.func.lower(ChatMonitor.username).in_(channels))
        if tg_chats:
            on.append(ChatMonitor.tg_chat_id.in_(tg_chats))
        competitor_channel = bool(
            await session.scalar(
                sa.select(
                    sa.exists().where(ChatMonitor.role == "competitor", sa.or_(*on))
                )
            )
        )
    tout_poster = await _tout_poster(session, lineage)
    own: list[OwnText] = []
    urls: list[str] = []
    repeat = 0
    countries: set[str] = set()
    for obs_kind, source_key, parsed in (
        await session.execute(
            sa.select(
                Observation.parsed["kind"].astext,
                Observation.source_key,
                Observation.parsed,
            )
            .where(Observation.company_id.in_(lineage), Observation.status == "resolved")
            .order_by(Observation.id)
        )
    ).all():
        if not isinstance(parsed, Mapping):
            continue
        attrs = _attrs(parsed)
        if url := _str(parsed.get("url")):
            urls.append(url)
        count = attrs.get("repeat_count_60d")
        if isinstance(count, int) and not isinstance(count, bool):
            repeat = max(repeat, count)
        if obs_kind == "exhibitor":
            country = _str(attrs.get("country"))
            if country:
                countries.add(country.strip().upper())
        if obs_kind in signals.SELF_DESCRIPTION_KINDS:
            body = _section(parsed, "company")
            text = _section(parsed, "text")
            title = _str(body.get("name_raw"))
            words = "\n".join(
                t
                for t in (title, _str(text.get("excerpt")), _str(body.get("goods_text")))
                if t
            )
            own.append(OwnText(words, str(obs_kind), title=title, source_key=source_key))
    imports = [
        signal_view
        for result in await session.scalars(
            sa.select(ModelJob.result).where(
                ModelJob.company_id.in_(lineage),
                ModelJob.kind == CLASSIFY_KIND,
                ModelJob.status.in_(CLASSIFY_DONE),
            )
        )
        if (signal_view := _import_view(result)) is not None
    ]
    people = (
        await session.execute(
            sa.select(Contact.phone, Contact.anonymised_at).where(
                Contact.company_id.in_(lineage)
            )
        )
    ).all()
    signal_rows = (
        await session.execute(
            sa.select(Signal.key, Signal.evidence_id, Signal.observed_at)
            .where(Signal.company_id.in_(lineage))
            .order_by(Signal.id)
        )
    ).all()
    push_tails = sorted({t for t in tails if t})
    all_tails = sorted({*await lineage_tails(session, lineage), *push_tails})
    view = GateCompany(
        id=company.id,
        display_name=company.display_name,
        names=tuple(dict.fromkeys(n for n in names if n and n != company.display_name)),
        legal_form=company.legal_form,
        stir=company.stir,
        address=company.address,
        city=company.city,
        registry_status=company.registry_status,
        state=company.state,
        is_competitor=any(r.is_competitor for r in rows),
        is_supplier_cn=any(r.is_supplier_cn for r in rows),
        id_types=frozenset(t for t, _ in ids),
        own=tuple(own),
        urls=tuple(urls),
        import_signals=tuple(imports),
        competitor_channel=competitor_channel,
        tout_poster=tout_poster,
        repeat_count_60d=repeat,
        exhibitor_countries=frozenset(countries),
    )
    return Inputs(
        company=view,
        contacts=[GateContact(p.phone, p.anonymised_at is not None) for p in people],
        signals=[GateSignal(s.key, s.evidence_id, s.observed_at) for s in signal_rows],
        verdict=company.verdict,
        hit=await suppression.check_company(
            session, company.id, now=now, tails=push_tails
        ),
        recent=await recent_pushes(
            session,
            lineage=lineage,
            tails=all_tails,
            now=now,
        ),
        lineage=lineage,
        tails=all_tails,
    )


async def evaluate(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    now: datetime,
    tails: Iterable[str] = (),
) -> GateResult:
    """``run_gates`` over the company as stored now."""
    got = await inputs_for(session, company_id, now=now, tails=tails)
    return run_gates(
        got.company, got.contacts, got.signals, got.verdict, got.hit, got.recent, now
    )


async def apply_effects(
    session: AsyncSession, company_id: uuid.UUID, failure: Failure | None
) -> None:
    """What the deciding gate writes of the company (04 §6): a competitor's
    flag and its ``company_id`` row, a supplier's flag, a client's state."""
    if failure is None:
        return
    values: dict[str, Any] = {}
    if failure.gate == "not_competitor":
        values["is_competitor"] = True
        await suppression.suppress_company(
            session, company_id, "competitor", source=COMPETITOR_SOURCE, reason="gate"
        )
    elif failure.gate == "not_supplier":
        values["is_supplier_cn"] = True
    elif failure.gate == "not_client" and failure.decision == "suppressed":
        state = await session.scalar(
            sa.select(Company.state).where(Company.id == company_id)
        )
        if state not in ("merged", "purged", "client"):
            values["state"] = "client"
    if values:
        await session.execute(
            sa.update(Company).where(Company.id == company_id).values(**values)
        )


__all__ = [
    "B2B_ID_TYPES",
    "GATES",
    "OPEN_PUSH_WINDOW",
    "PHONE_PUSH_WINDOW",
    "PUSH_COUNTED_STATUSES",
    "Failure",
    "GateCompany",
    "GateContact",
    "GateResult",
    "GateSignal",
    "ImportView",
    "Inputs",
    "OwnText",
    "Recent",
    "apply_effects",
    "evaluate",
    "inputs_for",
    "recent_pushes",
    "run_gates",
]
