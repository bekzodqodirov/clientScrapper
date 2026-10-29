"""The owner's morning digest (06 §1.8): one message at ``DIGEST_TIME``.

``render_digest`` is pure — a ``DigestView`` in, the Uzbek text out — and
golden-tested from fixtures (``tests/test_digest.py``); ``load`` reads the
view through the services that own each number; ``send`` claims the day's
``reminder_log(kind='digest', ref=<date>)`` before it tells the owner and
takes the claim back unless the message was delivered, so a restart never
sends a second digest and an unreachable Telegram leaves the day's digest
due (the worker's start-up catch-up sends it). The notifier is the owner
seam (``bot/deliver.owner_notifier``): no recipient argument, the owner only
(06 §4.3).

The layout is 06 §1.8's, «lines with nothing to say are omitted», plus the
lines other sections hand the digest:

* «Kecha» counts the Tashkent day before ``now``: companies found
  (``companies.created_at``), candidates scored, owner cards shown
  (``reviews.asked_today``'s count), approved, pushed (delivered push_log
  ids; ``make push-test``'s synthetic companies never count, 05 §B.1) and
  rejected; past ``REVIEW_DAILY_CAP`` the cards waiting are said (06 §1.6).
* «Navbat» is ``health.gather``'s queues and the live manager cards.
* «ERP (30 kun)» counts the pushes first delivered in the last 30 days by
  their outcome label (``outcomes.derive_label``'s, never re-derived):
  contacted of created, won — shown as the first-shipment proxy and labelled
  so (01 §7 decision 3a) — lost with «Qayta qilinmasin» and wrong numbers,
  and the ERP's drops (05 §B.2/§B.6 «digest»). Every lost-reason label no
  ``outcomes.LOST_REASON_PATTERNS`` class matched is listed (decision 1a): a
  ``lost_other`` row that carries a reason is exactly such a label, since
  the ERP sends labels only.
* «Manbalar» is ``owner.source_lines`` (the 30-day precision and pushes, the
  ✅/⏸/⛔ state) with the companies the source's runs found yesterday; a
  flip of ``auto_push`` since the last digest is its own line (05 §B.2), told
  by comparing with what the previous digest saw (``runtime_flags``
  ``digest_auto_push``) — whoever flipped it.
* The managers' day (06 §1.7): «📞 Bogʻlandim» counted, «🙅 Yozmang dedi»
  listed by company, «Menejer javob bermadi: {n}».
* «Tekshiruv» is the last ``make smoke`` run (``services/smoke``): a skipped
  live test is listed as skipped, never counted green (07 §1.12).
* «Tizim» is ``health.problems``' count; the dead-man line says when the
  outside watcher is not set or did not take the last ping (06 §4.2).
* With the feeds off and an XLSX import standing: «yoʻqotilgan lidlar
  roʻyxati yoʻq» (07 §1.5).
"""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db import enums as e
from clientscrapper.db.models import (
    ChatMonitor,
    CollectorRun,
    Company,
    Interaction,
    LeadCandidate,
    LeadReview,
    Outcome,
    PushLog,
    ReminderLog,
    RuntimeFlag,
    Suppression,
)
from clientscrapper.db.session import session_scope
from clientscrapper.services import (
    costs,
    erp_feeds,
    health,
    outcomes,
    owner,
    reviews,
    smoke,
)

log = logging.getLogger(__name__)

DIGEST_KIND: Final = "digest"
# What the previous digest saw of each source's auto_push (05 §B.2: a flip
# is one digest line).
AUTO_PUSH_FLAG: Final = "digest_auto_push"
ERP_WINDOW: Final = timedelta(days=30)
# Telegram's message limit (bot/deliver clips to it too; the digest must
# never need the clip).
MESSAGE_MAX: Final = 4096
# Lines of one list inside the digest (sources, labels, names).
MAX_SOURCES: Final = 10
MAX_FLIPS: Final = 5
MAX_NAMES: Final = 5
MAX_LABELS: Final = 6
NAME_MAX: Final = 40
LABEL_MAX: Final = 60
NOT_CLASSED: Final = "lost_other"

SourceState = Literal["on", "paused", "off"]
STATE_ICON: Final[Mapping[SourceState, str]] = {"on": "✅", "paused": "⏸", "off": "⛔"}

Notify = Callable[[str], Awaitable[bool]]


# --- the view -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Yesterday:
    found: int = 0
    scored: int = 0
    asked: int = 0
    approved: int = 0
    pushed: int = 0
    rejected: int = 0


@dataclass(frozen=True, slots=True)
class QueueLine:
    review: int = 0
    review_oldest_at: datetime | None = None
    enriching: int = 0
    hold: int = 0
    handoff_open: int = 0


@dataclass(frozen=True, slots=True)
class Unclassed:
    """A dictionary label no ``LOST_REASON_PATTERNS`` class matched, as the
    ERP spelt it last, and how many outcomes carry it."""

    label: str
    n: int


@dataclass(frozen=True, slots=True)
class ErpLine:
    """The pushes first delivered in the last 30 days, by outcome."""

    pushes: int = 0
    created: int = 0
    contacted: int = 0
    won: int = 0
    lost: int = 0
    lost_no_contact: int = 0
    lost_wrong_number: int = 0
    dropped_capped: int = 0
    dropped_suppressed: int = 0
    dropped_other: int = 0

    @property
    def contacted_pct(self) -> int | None:
        return None if self.created == 0 else self.contacted * 100 // self.created


@dataclass(frozen=True, slots=True)
class SourceRow:
    title: str
    state: SourceState
    new: int = 0
    precision: Decimal | None = None
    pushes_30d: int = 0


@dataclass(frozen=True, slots=True)
class Flip:
    title: str
    on: bool


@dataclass(frozen=True, slots=True)
class ReaderLine:
    chats_on: int = 0
    hits: int = 0
    joined: int = 0
    flood_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class ManagerLine:
    contacted: int = 0
    said_no: tuple[str, ...] = ()
    wrong: int = 0
    expired: int = 0


@dataclass(frozen=True, slots=True)
class SpendLine:
    day_usd: Decimal = Decimal(0)
    month_usd: Decimal = Decimal(0)
    monthly_cap: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class DeadmanLine:
    """The outside watcher (06 §4.2): not configured, or its last answer."""

    configured: bool = True
    ok: bool | None = None
    status: int | None = None


@dataclass(frozen=True, slots=True)
class DigestView:
    day: date
    yesterday: Yesterday = field(default_factory=Yesterday)
    push_cap: int = 0
    review_cap: int = 0
    queue: QueueLine = field(default_factory=QueueLine)
    erp: ErpLine = field(default_factory=ErpLine)
    unclassed: tuple[Unclassed, ...] = ()
    sources: tuple[SourceRow, ...] = ()
    flips: tuple[Flip, ...] = ()
    reader: ReaderLine | None = None
    managers: ManagerLine = field(default_factory=ManagerLine)
    spend: SpendLine = field(default_factory=SpendLine)
    smoke: smoke.SmokeRun | None = None
    problems: int = 0
    deadman: DeadmanLine = field(default_factory=DeadmanLine)
    no_lost_list: bool = False
    waiting_cards: int = 0
    now: datetime | None = None


# --- rendering (pure) ------------------------------------------------------------


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _names(names: Sequence[str]) -> str:
    shown = [esc(_clip(n, NAME_MAX)) for n in names[:MAX_NAMES]]
    more = len(names) - len(shown)
    return ", ".join(shown) + (f" va yana {more} ta" if more > 0 else "")


def _money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01'))}"


def _pct(part: Decimal, whole: Decimal) -> int:
    return int(part * 100 / whole) if whole > 0 else 0


def _age(moment: datetime | None, now: datetime | None) -> str:
    if moment is None or now is None:
        return "—"
    return health.age_label(now - moment)


def _precision(value: Decimal | None) -> str:
    return "—" if value is None else f"{int(value * 100)}%"


def _erp_lines(erp: ErpLine, unclassed: Sequence[Unclassed]) -> list[str]:
    lines: list[str] = []
    if erp.pushes:
        pct = erp.contacted_pct
        lines.append(
            f"ERP (30 kun): bogʻlanildi {'—' if pct is None else f'{pct}%'} · "
            f"yutuq {erp.won} (birinchi yuk oʻrniga, taxminiy) · yoʻqotildi "
            f"{erp.lost} (shundan «Qayta qilinmasin» {erp.lost_no_contact}, "
            f"notoʻgʻri raqam {erp.lost_wrong_number})"
        )
        dropped = [
            (erp.dropped_capped, "chegara"),
            (erp.dropped_suppressed, "bostirilgan"),
            (erp.dropped_other, "boshqa"),
        ]
        said = [f"{word} {n}" for n, word in dropped if n]
        if said:
            lines.append("ERP qabul qilmadi: " + " · ".join(said))
    if unclassed:
        shown = " · ".join(
            f"«{esc(_clip(u.label, LABEL_MAX))}» {u.n} ta" for u in unclassed
        )
        lines.append(
            f"⚠️ Sinflanmagan yoʻqotish sabablari: {shown} — dasturchiga ayting, "
            "aniqlik hisobiga qoʻshilsin"
        )
    return lines


def _source_lines(sources: Sequence[SourceRow], flips: Sequence[Flip]) -> list[str]:
    lines: list[str] = []
    if sources:
        lines.append("Manbalar:")
        for row in sources[:MAX_SOURCES]:
            lines.append(
                f"  {esc(_clip(row.title, NAME_MAX))} {row.new} yangi · "
                f"{_precision(row.precision)} ({row.pushes_30d}) {STATE_ICON[row.state]}"
            )
        if len(sources) > MAX_SOURCES:
            lines.append(f"  … yana {len(sources) - MAX_SOURCES} ta")
    for flip in flips[:MAX_FLIPS]:
        word = "yoqildi" if flip.on else "oʻchirildi"
        lines.append(f"🔁 Avtomatik push {word}: {esc(_clip(flip.title, NAME_MAX))}")
    if len(flips) > MAX_FLIPS:
        lines.append(f"🔁 … yana {len(flips) - MAX_FLIPS} ta oʻzgarish — /manbalar")
    return lines


def _smoke_lines(run: smoke.SmokeRun | None) -> list[str]:
    if run is None:
        return []
    passed, failed, skipped = run.counts
    head = "Choraklik tekshiruv" if run.full else "Tekshiruv"
    lines = [f"{head}: ✓ {passed} / ✗ {failed} / oʻtkazildi {skipped}"]
    if run.failed:
        lines.append(
            f"  ✗ oʻzgargan: {_names(run.failed)} — fixture yangilansin, /manbalar"
        )
    if run.skipped:
        lines.append(f"  oʻtkazildi: {_names(run.skipped)}")
    return lines


def _manager_lines(m: ManagerLine) -> list[str]:
    lines: list[str] = []
    parts = []
    if m.contacted:
        parts.append(f"bogʻlandi {m.contacted}")
    if m.said_no:
        parts.append(f"«Yozmang» dedi {len(m.said_no)} ({_names(m.said_no)})")
    if m.wrong:
        parts.append(f"notoʻgʻri/B2C {m.wrong}")
    if parts:
        lines.append("Menejerlar kecha: " + " · ".join(parts))
    if m.expired:
        lines.append(f"Menejer javob bermadi: {m.expired}")
    return lines


def _deadman_line(d: DeadmanLine) -> list[str]:
    if not d.configured:
        return ["⚠️ Tashqi kuzatuvchi sozlanmagan — <code>.env</code> DEADMAN_PING_URL"]
    if d.ok is False:
        code = "javob yoʻq" if d.status is None else f"HTTP {d.status}"
        return [
            f"⚠️ Tashqi kuzatuvchi oxirgi signalni olmadi ({code}) — manzilni tekshiring"
        ]
    return []


def render_digest(view: DigestView) -> str:
    """06 §1.8 over a ``DigestView``; HTML (the bot's parse mode), every
    value escaped. Lines with nothing to say are omitted."""
    y = view.yesterday
    q = view.queue
    oldest = (
        f" (eng eskisi {_age(q.review_oldest_at, view.now)})"
        if q.review and q.review_oldest_at is not None
        else ""
    )
    lines = [
        f"☀️ {view.day:%d.%m.%Y} — Qidiruv agenti",
        f"Kecha: topildi {y.found} · ball olgan {y.scored} · karta {y.asked} · "
        f"tasdiq {y.approved} · CRMʼga {y.pushed}/{view.push_cap} · rad {y.rejected}",
        f"Navbat: koʻrib chiqish {q.review}{oldest} · boyitish {q.enriching} · "
        f"kutish {q.hold} · menejerda {q.handoff_open}",
    ]
    if view.review_cap and y.asked >= view.review_cap and view.waiting_cards:
        lines.append(
            f"Kunlik karta chegarasi ({view.review_cap}) toʻldi — navbatda "
            f"{view.waiting_cards} ta"
        )
    lines += _erp_lines(view.erp, view.unclassed)
    if view.no_lost_list:
        lines.append(
            "ERP: yoʻqotilgan lidlar roʻyxati yoʻq — faqat mijozlar XLSX importi "
            "tekshiriladi («Boshqa» eshigi)"
        )
    lines += _source_lines(view.sources, view.flips)
    lines += _manager_lines(view.managers)
    reader = view.reader
    if reader is not None:
        flood = (
            "yoʻq"
            if reader.flood_until is None
            else f"{health.clock_label(reader.flood_until)} gacha"
        )
        lines.append(
            f"Telegram oʻquvchi: {reader.chats_on} chat · kecha {reader.hits} signal · "
            f"{reader.joined} yangi chat · flood {flood}"
        )
    s = view.spend
    lines.append(
        f"Xarajat: kecha {_money(s.day_usd)} · oy {_money(s.month_usd)}/"
        f"{_money(s.monthly_cap)} ({_pct(s.month_usd, s.monthly_cap)}%)"
    )
    lines += _smoke_lines(view.smoke)
    lines.append(
        "Tizim: hammasi joyida"
        if view.problems == 0
        else f"Tizim: {view.problems} ta muammo — /holat"
    )
    lines += _deadman_line(view.deadman)
    if view.waiting_cards:
        lines.append(f"Sizni {view.waiting_cards} ta karta kutmoqda → /lidlar")
    return fit(lines)


def fit(lines: Sequence[str], limit: int = MESSAGE_MAX) -> str:
    """The lines joined, within ``limit``: the lists are capped so a digest
    fits, and should one not, whole lines from the end of the middle give way
    (each line is its own balanced HTML, so a cut never breaks a tag) — the
    first and the last line («Sizni … kutmoqda») stay."""
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    kept = list(lines)
    gap = "  …"
    while len(kept) > 2 and len("\n".join([*kept[:-1], gap, kept[-1]])) > limit:
        del kept[-2]
    return "\n".join([*kept[:-1], gap, kept[-1]])[:limit]


# --- loading ----------------------------------------------------------------------


def yesterday_window(now: datetime) -> tuple[datetime, datetime]:
    """The Tashkent day before ``now``'s: [start, end)."""
    today = costs.day_start(now)
    return costs.day_start(today - timedelta(hours=12)), today


def _not_synthetic() -> sa.ColumnElement[bool]:
    """Not ``make push-test``'s synthetic company (05 §B.1)."""
    test = LeadCandidate.score_breakdown["gates"]["test"].as_boolean()
    return sa.func.coalesce(test, sa.false()).is_(False)


async def _count(session: AsyncSession, stmt: sa.Select[Any]) -> int:
    return int(await session.scalar(stmt) or 0)


async def _yesterday(session: AsyncSession, now: datetime) -> Yesterday:
    start, end = yesterday_window(now)

    def between(column: Any) -> sa.ColumnElement[bool]:
        return sa.and_(column >= start, column < end)

    count = sa.select(sa.func.count())
    first = _first_deliveries()
    pushed = sa.select(sa.func.count()).select_from(first).where(between(first.c.sent_at))
    return Yesterday(
        found=await _count(
            session, count.select_from(Company).where(between(Company.created_at))
        ),
        scored=await _count(
            session,
            count.select_from(LeadCandidate).where(between(LeadCandidate.last_scored_at)),
        ),
        asked=await reviews.asked_today(session, start),
        approved=await _count(
            session,
            count.select_from(LeadCandidate).where(
                between(LeadCandidate.approved_at), _not_synthetic()
            ),
        ),
        pushed=await _count(session, pushed),
        rejected=await _count(
            session,
            count.select_from(LeadCandidate).where(between(LeadCandidate.rejected_at)),
        ),
    )


async def _handoff_open(session: AsyncSession) -> int:
    return await _count(
        session,
        sa.select(sa.func.count())
        .select_from(LeadReview)
        .where(LeadReview.kind == "manager", LeadReview.state.in_(e.REVIEW_LIVE_STATES)),
    )


async def _waiting_cards(session: AsyncSession) -> int:
    """The owner's cards not answered yet (shown or still to show)."""
    return await _count(
        session,
        sa.select(sa.func.count())
        .select_from(LeadReview)
        .join(LeadCandidate, LeadCandidate.id == LeadReview.candidate_id)
        .where(
            LeadReview.kind == "owner",
            LeadReview.state.in_(e.REVIEW_LIVE_STATES),
            LeadCandidate.state == "review",
        ),
    )


def _first_deliveries() -> sa.Subquery:
    """Each pushed ``external_id`` once, dated by its first delivered row (a
    re-push supersedes a row and writes another with the same id, 02 §2.10);
    none of ``make push-test``'s."""
    return (
        sa.select(
            PushLog.external_id.label("external_id"),
            sa.func.min(PushLog.sent_at).label("sent_at"),
        )
        .join(LeadCandidate, LeadCandidate.id == PushLog.candidate_id)
        .where(PushLog.status.in_(e.PUSH_DELIVERED_STATUSES), _not_synthetic())
        .group_by(PushLog.external_id)
        .subquery()
    )


async def erp_line(session: AsyncSession, now: datetime) -> ErpLine:
    """The pushes first delivered in the last 30 days, by their outcome row."""
    first = _first_deliveries()
    rows = (
        await session.execute(
            sa.select(Outcome.landing, Outcome.label, Outcome.contacted_at)
            .select_from(first)
            .outerjoin(Outcome, Outcome.external_id == first.c.external_id)
            .where(first.c.sent_at >= now - ERP_WINDOW, first.c.sent_at <= now)
        )
    ).all()
    labels = [str(label) for _, label, _ in rows if label is not None]
    return ErpLine(
        pushes=len(rows),
        created=sum(1 for landing, _, _ in rows if landing == "created"),
        contacted=sum(
            1 for landing, _, at in rows if landing == "created" and at is not None
        ),
        won=sum(1 for label in labels if label in outcomes.CLIENT_LABELS),
        lost=sum(1 for label in labels if label.startswith("lost_")),
        lost_no_contact=labels.count("lost_no_contact"),
        lost_wrong_number=labels.count("lost_wrong_number"),
        dropped_capped=labels.count("dropped_capped"),
        dropped_suppressed=labels.count("dropped_suppressed"),
        dropped_other=labels.count("dropped_other"),
    )


async def unclassed_labels(
    session: AsyncSession, *, limit: int = MAX_LABELS
) -> tuple[Unclassed, ...]:
    """Decision 1a: every lost-reason label no class matched — a
    ``lost_other`` outcome with a reason — most used first, one entry per
    normalised label (``outcomes.normalise_label``, the ERP's own)."""
    rows = await session.execute(
        sa.select(Outcome.lost_reason, Outcome.erp_at)
        .where(Outcome.label == NOT_CLASSED, Outcome.lost_reason.is_not(None))
        .order_by(Outcome.erp_at.desc(), Outcome.external_id)
    )
    found: dict[str, list[Any]] = {}
    for reason, at in rows:
        key = outcomes.normalise_label(reason)
        if not key:
            continue
        entry = found.setdefault(key, [" ".join(str(reason).split()), 0, at])
        entry[1] += 1
    ranked = sorted(found.values(), key=lambda x: (-x[1], -x[2].timestamp(), x[0]))
    return tuple(Unclassed(str(label), int(n)) for label, n, _ in ranked[:limit])


async def _new_by_source(session: AsyncSession, now: datetime) -> dict[str, int]:
    start, end = yesterday_window(now)
    rows = await session.execute(
        sa.select(CollectorRun.source_key, sa.func.sum(CollectorRun.new_companies))
        .where(
            CollectorRun.started_at >= start,
            CollectorRun.started_at < end,
            smoke.not_smoke(),
        )
        .group_by(CollectorRun.source_key)
    )
    return {str(key): int(n or 0) for key, n in rows.all()}


async def _sources(
    session: AsyncSession, now: datetime
) -> tuple[tuple[SourceRow, ...], tuple[Flip, ...], dict[str, bool]]:
    """The enabled sources' rows, the auto_push flips since the last digest,
    and what this digest saw (written only when it is delivered)."""
    lines = await owner.source_lines(session, now=now)
    new = await _new_by_source(session, now)
    rows = tuple(
        SourceRow(
            title=line.title,
            state=line.state(now),
            new=new.get(line.key, 0),
            precision=line.precision,
            pushes_30d=line.pushes_30d,
        )
        for line in lines
        if line.enabled
    )
    seen = {line.key: line.auto_push for line in lines}
    flag = await session.get(RuntimeFlag, AUTO_PUSH_FLAG, populate_existing=True)
    before = flag.value if flag is not None and isinstance(flag.value, dict) else None
    flips: tuple[Flip, ...] = ()
    if before is not None:
        titles = {line.key: line.title for line in lines}
        flips = tuple(
            Flip(titles[key], on)
            for key, on in sorted(seen.items())
            if isinstance(before.get(key), bool) and before[key] != on
        )
    return rows, flips, seen


async def _managers(session: AsyncSession, now: datetime) -> ManagerLine:
    start, end = yesterday_window(now)
    answered = sa.and_(
        LeadReview.kind == "manager",
        LeadReview.answered_at >= start,
        LeadReview.answered_at < end,
    )
    counts: dict[str, int] = {
        str(answer): int(n)
        for answer, n in (
            await session.execute(
                sa.select(LeadReview.answer, sa.func.count())
                .where(answered)
                .group_by(LeadReview.answer)
            )
        ).all()
    }
    said_no = (
        await session.scalars(
            sa.select(Company.display_name)
            .join(LeadCandidate, LeadCandidate.company_id == Company.id)
            .join(LeadReview, LeadReview.candidate_id == LeadCandidate.id)
            .where(answered, LeadReview.answer == "said_no")
            .order_by(LeadReview.answered_at, LeadReview.id)
        )
    ).all()
    expired = await _count(
        session,
        sa.select(sa.func.count())
        .select_from(LeadReview)
        .where(
            LeadReview.kind == "manager",
            LeadReview.state == "expired",
            LeadReview.note.is_(None),
            LeadReview.expires_at >= start,
            LeadReview.expires_at < end,
        ),
    )
    return ManagerLine(
        contacted=int(counts.get("contacted", 0)),
        said_no=tuple(str(name) for name in said_no),
        wrong=int(counts.get("wrong_or_b2c", 0)),
        expired=expired,
    )


async def _reader(
    session: AsyncSession, status: health.Status, now: datetime
) -> ReaderLine | None:
    if not health.userbot_on(status):
        return None
    start, end = yesterday_window(now)
    hits = await _count(
        session,
        sa.select(sa.func.count())
        .select_from(Interaction)
        .where(Interaction.occurred_at >= start, Interaction.occurred_at < end),
    )
    joined = await _count(
        session,
        sa.select(sa.func.count())
        .select_from(ChatMonitor)
        .where(ChatMonitor.joined_at >= start, ChatMonitor.joined_at < end),
    )
    flood = (
        status.flood_until if status.flood_until and status.flood_until > now else None
    )
    return ReaderLine(status.chats_on, hits, joined, flood)


async def _no_lost_list(session: AsyncSession) -> bool:
    """07 §1.5: the feeds off and the owner's XLSX import standing — pushes
    proceed with no «Qayta qilinmasin» list behind them."""
    if erp_feeds.feeds_configured():
        return False
    found = await session.scalar(
        sa.select(Suppression.id).where(Suppression.source == "manual_import").limit(1)
    )
    return found is not None


def _deadman(status: health.Status) -> DeadmanLine:
    if not settings.deadman_ping_url.strip():
        return DeadmanLine(configured=False)
    worker = status.components.get("worker")
    ping = worker.detail.get("deadman") if worker is not None else None
    if not isinstance(ping, Mapping):
        return DeadmanLine()
    ok = ping.get("ok")
    code = ping.get("status")
    return DeadmanLine(
        ok=ok if isinstance(ok, bool) else None,
        status=code if isinstance(code, int) and not isinstance(code, bool) else None,
    )


@dataclass(frozen=True, slots=True)
class Loaded:
    view: DigestView
    # What this digest saw of auto_push, remembered once it is delivered.
    auto_push: Mapping[str, bool]


async def load(session: AsyncSession, *, now: datetime) -> Loaded:
    """Everything the digest says, read at ``now``."""
    status = await health.gather(session, now=now)
    start, _ = yesterday_window(now)
    spent_day = await costs.snapshot(session, start + timedelta(hours=12))
    spent_month = await costs.snapshot(session, now)
    sources, flips, seen = await _sources(session, now)
    q = status.queues
    view = DigestView(
        day=now.astimezone(settings.tz).date(),
        yesterday=await _yesterday(session, now),
        push_cap=settings.push_daily_cap,
        review_cap=settings.review_daily_cap,
        queue=QueueLine(
            review=q.review,
            review_oldest_at=q.review_oldest_at,
            enriching=q.enriching,
            hold=q.hold,
            handoff_open=await _handoff_open(session),
        ),
        erp=await erp_line(session, now),
        unclassed=await unclassed_labels(session),
        sources=sources,
        flips=flips,
        reader=await _reader(session, status, now),
        managers=await _managers(session, now),
        spend=SpendLine(
            spent_day.day_usd, spent_month.month_usd, settings.monthly_cost_cap_usd
        ),
        smoke=await smoke.latest(session, now=now),
        problems=len(health.problems(status)),
        deadman=_deadman(status),
        no_lost_list=await _no_lost_list(session),
        waiting_cards=await _waiting_cards(session),
        now=now,
    )
    return Loaded(view, seen)


# --- once a day ---------------------------------------------------------------------


def day_ref(now: datetime) -> str:
    return now.astimezone(settings.tz).date().isoformat()


async def sent_today(session: AsyncSession, now: datetime) -> bool:
    found = await session.scalar(
        sa.select(ReminderLog.id)
        .where(ReminderLog.kind == DIGEST_KIND, ReminderLog.ref == day_ref(now))
        .limit(1)
    )
    return found is not None


async def _claim(session: AsyncSession, now: datetime) -> int | None:
    """The day's digest row, under its advisory lock (two speakers, one
    digest), or None when one already stands."""
    ref = day_ref(now)
    await session.execute(
        sa.select(
            sa.func.pg_advisory_xact_lock(
                costs.ALERT_LOCK, sa.func.hashtext(f"{DIGEST_KIND}:{ref}")
            )
        )
    )
    if await sent_today(session, now):
        return None
    row = ReminderLog(kind=DIGEST_KIND, ref=ref, sent_at=now)
    session.add(row)
    await session.flush()
    return row.id


async def _remember(session: AsyncSession, seen: Mapping[str, bool]) -> None:
    stmt = pg_insert(RuntimeFlag).values(
        key=AUTO_PUSH_FLAG, value=dict(seen), updated_by="worker:digest"
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[RuntimeFlag.key],
            set_={"value": stmt.excluded.value, "updated_by": stmt.excluded.updated_by},
        )
    )


SendOutcome = Literal["sent", "already", "undelivered"]


async def send(notify: Notify, *, now: datetime) -> SendOutcome:
    """Today's digest, at most once: rendered, claimed (committed), told,
    the claim taken back unless delivered."""
    async with session_scope() as session:
        if await sent_today(session, now):
            return "already"
        loaded = await load(session, now=now)
    text = render_digest(loaded.view)
    async with session_scope() as own:
        claim = await _claim(own, now)
    if claim is None:
        return "already"
    delivered = False
    try:
        delivered = await notify(text)
    finally:
        async with session_scope() as own:
            if delivered:
                await _remember(own, loaded.auto_push)
            else:
                await own.execute(sa.delete(ReminderLog).where(ReminderLog.id == claim))
    if not delivered:
        log.warning("digest not delivered; due again at the next start-up")
        return "undelivered"
    log.info("digest sent", extra={"chars": len(text)})
    return "sent"


__all__ = [
    "AUTO_PUSH_FLAG",
    "DIGEST_KIND",
    "ERP_WINDOW",
    "MESSAGE_MAX",
    "DeadmanLine",
    "DigestView",
    "ErpLine",
    "Flip",
    "Loaded",
    "ManagerLine",
    "QueueLine",
    "ReaderLine",
    "SourceRow",
    "SpendLine",
    "Unclassed",
    "Yesterday",
    "day_ref",
    "erp_line",
    "fit",
    "load",
    "render_digest",
    "send",
    "sent_today",
    "unclassed_labels",
    "yesterday_window",
]
