"""The pilot's numbers — one home for ``/holat`` and ``tools/mvp_report.py``
(07 §3.2, 01 §5; the owner's decisions of 2026-10-29, 01 §7).

What is counted is a **push**: an ``external_id`` with a delivered
``push_log`` row (``sent`` or ``unknown``), once — a re-push supersedes a
row and writes another with the same id — and dated by the row its outcome
is about (``outcomes.delivery_order``: the live one). Rule 5 re-pushes an
id the ERP never recorded (02 §2.10), so its lead was born at the re-push
and the managers' day runs from there. ``make push-test``'s synthetic lead
(``candidates.synthetic_sql``) is no push of the pilot's. The outcome is the
``outcomes`` row of that id (05 §B.2); its ``label`` and ``precision_class``
are ``outcomes.derive_label``'s and are never re-derived here.

* **hot** — the candidate's score ≥ 60 (07 §3.2's first row; every push has
  a phone, ``push_log.phone_tail`` is NOT NULL).
* **real importer rate** — over pushes whose outcome is terminal (won,
  lost, a client or dropped landing) or whose push is ≥ 14 days old:
  ``1 − not_importer / judged``, ``judged`` = labels that are not
  ``dropped_*`` (07 §3.2, the owner's wording; 07 open question 7). The
  stricter ``precision`` (positives / (positives + negatives)) of the same
  pushes is reported beside it.
* **contacted within one business day** — ``contacted_at`` no later than
  ``next_business_day_end(sent_at)`` (Mon–Sat, Asia/Tashkent), over the
  pushes the ERP ``created`` a lead for whose deadline has passed or that
  were contacted in time. ``contacted_at`` is what the ERP can see (calls
  app, a connected Telegram account, a note, a stage move, «📞 Bogʻlandim»),
  so it under-counts (01 §7 decision 6a: stated beside the number).
* **quotes sent / contacted** (decision 5a) — APPROXIMATE: the lead's
  CURRENT stage name holds one of ``QUOTE_STAGE_NAMES`` (default «narx»), or
  the stage is ``won``; a lead that passed «Narx aytildi» and moved on to an
  open stage without the word is not seen. Over the pushes with an outcome
  that were contacted or quoted.
* **first shipment** (decision 3a) — PROXY: ``won`` (and the fallback
  door's ``landed_client``) over every push; the ERP sends no receipt, no
  kub and no shipment (05 open question 14). Labelled a proxy everywhere.
* **unclassed lost reasons** (decision 1a) — a ``lost_other`` row with a
  ``lost_reason`` is a dictionary label no ``outcomes.LOST_REASON_PATTERNS``
  class matched (the ERP sends labels only, a seller's free text arrives as
  NULL): listed so a new label is mapped, never silently neutral.
* **spend** — per Tashkent day, Anthropic and all providers, from
  ``usage_log`` (refusals cost 0).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db import enums
from clientscrapper.db.models import LeadCandidate, Outcome, PushLog, UsageLog
from clientscrapper.services.candidates import synthetic_sql
from clientscrapper.services.outcomes import (
    CLIENT_LABELS,
    delivery_order,
    normalise_label,
)

# 07 §3.2: «Mon–Sat Tashkent calendar».
BUSINESS_DAYS: Final = frozenset({1, 2, 3, 4, 5, 6})
HOT_SCORE: Final = 60
# 07 §3.2: an outcome is judged once terminal or once the push is this old.
JUDGED_AFTER: Final = timedelta(days=14)
NOT_IMPORTER_LABELS: Final = frozenset(
    {"lost_not_importer", "lost_wrong_number", "lost_competitor", "lost_small"}
)
TERMINAL_STAGES: Final = frozenset({"won", "lost"})
TERMINAL_LANDINGS: Final = frozenset({"client", "dropped"})
DROPPED_PREFIX: Final = "dropped_"
# /holat's window (01 §5's month-3 targets are read over the same 30 days
# the precision job uses).
HOLAT_WINDOW: Final = timedelta(days=30)
# 07 §3.2: the MVP is read at the end of week 2 of the pilot.
WEEK: Final = timedelta(days=7)
# 07 §3.2 targets (and 01 §5's for the two approximate rows).
TARGET_PUSHED_W2: Final = 20
TARGET_IMPORTER: Final = 60
TARGET_CONTACTED: Final = 40
TARGET_QUOTED: Final = 25
TARGET_FIRST_SHIPMENT: Final = 3
TARGET_CLAUDE_DAY: Final = Decimal("5.00")


def next_business_day_end(at: datetime, tz: tzinfo | None = None) -> datetime:
    """The end (the next local midnight) of the first business day after
    ``at``'s own Tashkent day: a push on Friday has until Saturday 24:00, one
    on Saturday or Sunday until Monday 24:00 (07 §3.2)."""
    zone = tz or settings.tz
    day = at.astimezone(zone).date() + timedelta(days=1)
    while day.isoweekday() not in BUSINESS_DAYS:
        day += timedelta(days=1)
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)


def quote_stage(
    stage_name: str | None, stage_kind: str | None, names: Iterable[str]
) -> bool:
    """Decision 5a: a won stage, or a stage whose name holds one of the
    «price given» fragments (normalised as the ERP normalises a label)."""
    if stage_kind == "won":
        return True
    current = normalise_label(stage_name)
    return bool(current) and any(fragment in current for fragment in names)


@dataclass(frozen=True, slots=True)
class Ratio:
    """``n`` of ``of``; no percent while ``of`` is 0."""

    n: int = 0
    of: int = 0

    @property
    def pct(self) -> int | None:
        return None if self.of == 0 else self.n * 100 // self.of


@dataclass(frozen=True, slots=True)
class Unclassed:
    """A lost-reason label no pattern classed: as the ERP spelt it (the
    newest spelling), how many outcomes carry it, and the newest one."""

    label: str
    n: int
    last_at: datetime


@dataclass(frozen=True, slots=True)
class PushRow:
    """One push as the metrics read it."""

    external_id: str
    sent_at: datetime
    score: int | None
    landing: str | None = None
    stage_kind: str | None = None
    stage_name: str | None = None
    label: str | None = None
    precision_class: str | None = None
    contacted_at: datetime | None = None

    @property
    def hot(self) -> bool:
        return self.score is not None and self.score >= HOT_SCORE

    @property
    def judged(self) -> bool:
        return self.label is not None


@dataclass(frozen=True, slots=True)
class Results:
    """The numbers over the pushes dated in ``[since, until)``."""

    since: datetime
    until: datetime
    pushed: int = 0
    hot: int = 0
    importer: Ratio = field(default_factory=Ratio)
    precision: Ratio = field(default_factory=Ratio)
    contacted: Ratio = field(default_factory=Ratio)
    quoted: Ratio = field(default_factory=Ratio)
    first_shipment: Ratio = field(default_factory=Ratio)
    unclassed: tuple[Unclassed, ...] = ()


async def pushes(
    session: AsyncSession, *, since: datetime, until: datetime
) -> list[PushRow]:
    """Every push dated in ``[since, until)``, with its outcome."""
    dated = (
        sa.select(PushLog.external_id, PushLog.candidate_id, PushLog.sent_at)
        .where(PushLog.status.in_(enums.PUSH_DELIVERED_STATUSES))
        .distinct(PushLog.external_id)
        .order_by(PushLog.external_id, *delivery_order())
        .subquery()
    )
    rows = await session.execute(
        sa.select(
            dated.c.external_id,
            dated.c.sent_at,
            LeadCandidate.score,
            Outcome.landing,
            Outcome.stage_kind,
            Outcome.stage_name,
            Outcome.label,
            Outcome.precision_class,
            Outcome.contacted_at,
        )
        .join(LeadCandidate, LeadCandidate.id == dated.c.candidate_id)
        .outerjoin(Outcome, Outcome.external_id == dated.c.external_id)
        .where(dated.c.sent_at >= since, dated.c.sent_at < until, ~synthetic_sql())
        .order_by(dated.c.sent_at, dated.c.external_id)
    )
    return [
        PushRow(
            external_id=r.external_id,
            sent_at=r.sent_at,
            score=r.score,
            landing=r.landing,
            stage_kind=r.stage_kind,
            stage_name=r.stage_name,
            label=r.label,
            precision_class=r.precision_class,
            contacted_at=r.contacted_at,
        )
        for r in rows
    ]


def importer_rate(rows: Sequence[PushRow], *, now: datetime) -> Ratio:
    """07 §3.2's real importer rate: ``n`` real importers of ``of`` judged."""
    judged = [
        r
        for r in rows
        if r.label is not None
        and not r.label.startswith(DROPPED_PREFIX)
        and (
            r.stage_kind in TERMINAL_STAGES
            or r.landing in TERMINAL_LANDINGS
            or now - r.sent_at >= JUDGED_AFTER
        )
    ]
    bad = sum(1 for r in judged if r.label in NOT_IMPORTER_LABELS)
    return Ratio(len(judged) - bad, len(judged))


def precision(rows: Sequence[PushRow]) -> Ratio:
    """Positives of positives + negatives (``sources.precision_30d``'s rule)."""
    positives = sum(1 for r in rows if r.precision_class == "positive")
    negatives = sum(1 for r in rows if r.precision_class == "negative")
    return Ratio(positives, positives + negatives)


def contacted_in_time(rows: Sequence[PushRow], *, now: datetime) -> Ratio:
    """Contacted by the end of the next business day, of the created leads
    that had that day (or were contacted already)."""
    hit = due = 0
    for r in rows:
        if r.landing != "created":
            continue
        deadline = next_business_day_end(r.sent_at)
        in_time = r.contacted_at is not None and r.contacted_at <= deadline
        if in_time or deadline <= now:
            due += 1
            hit += in_time
    return Ratio(hit, due)


def quoted(rows: Sequence[PushRow], names: Iterable[str]) -> Ratio:
    """Decision 5a, approximate: quoted of contacted-or-quoted."""
    fragments = tuple(names)
    asked = told = 0
    for r in rows:
        if not r.judged:
            continue
        price = quote_stage(r.stage_name, r.stage_kind, fragments)
        if price or r.contacted_at is not None:
            asked += 1
            told += price
    return Ratio(told, asked)


def first_shipment(rows: Sequence[PushRow]) -> Ratio:
    """Decision 3a, a proxy: won (or landed as a client) of every push."""
    return Ratio(sum(1 for r in rows if r.label in CLIENT_LABELS), len(rows))


async def unclassed_labels(session: AsyncSession) -> tuple[Unclassed, ...]:
    """Decision 1a: every dictionary label no pattern classed, most used
    first (then newest), one entry per normalised label, shown as the ERP
    spelt it last — all of them (a cut is the renderer's, and it says how
    many it left out)."""
    rows = await session.execute(
        sa.select(Outcome.lost_reason, Outcome.erp_at)
        .where(Outcome.label == "lost_other", Outcome.lost_reason.is_not(None))
        .order_by(Outcome.erp_at.desc())
    )
    found: dict[str, tuple[str, int, datetime]] = {}
    for reason, at in rows:
        key = normalise_label(reason)
        if not key:
            continue
        shown, n, last = found.get(key, (" ".join(reason.split()), 0, at))
        found[key] = (shown, n + 1, max(last, at))
    ranked = sorted(found.values(), key=lambda x: (-x[1], -x[2].timestamp(), x[0]))
    return tuple(Unclassed(label, n, at) for label, n, at in ranked)


async def measure(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    now: datetime,
    quote_names: Sequence[str] | None = None,
) -> Results:
    """Every number over the pushes dated in ``[since, until)``."""
    rows = await pushes(session, since=since, until=until)
    names = settings.quote_stage_names_parsed if quote_names is None else quote_names
    return Results(
        since=since,
        until=until,
        pushed=len(rows),
        hot=sum(1 for r in rows if r.hot),
        importer=importer_rate(rows, now=now),
        precision=precision(rows),
        contacted=contacted_in_time(rows, now=now),
        quoted=quoted(rows, names),
        first_shipment=first_shipment(rows),
        unclassed=await unclassed_labels(session),
    )


async def holat(session: AsyncSession, *, now: datetime) -> Results:
    """``/holat``'s numbers: the last 30 days."""
    return await measure(session, since=now - HOLAT_WINDOW, until=now, now=now)


# --- the MVP report (07 §3.2) -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpendDay:
    day: date
    anthropic: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class MvpReport:
    """07 §3.2's four numbers and the two approximate rows of 01 §5."""

    start: date
    now: datetime
    week2_start: datetime
    week2_end: datetime
    week2_hot: int
    results: Results
    spend: tuple[SpendDay, ...] = field(default_factory=tuple)

    @property
    def anthropic_max(self) -> SpendDay | None:
        return max(self.spend, key=lambda d: (d.anthropic, d.day), default=None)

    @property
    def total_max(self) -> SpendDay | None:
        return max(self.spend, key=lambda d: (d.total, d.day), default=None)


def local_midnight(day: date, tz: tzinfo | None = None) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz or settings.tz)


async def pilot_start(session: AsyncSession) -> date | None:
    """The Tashkent day of the first delivered push, None before any;
    ``make push-test``'s synthetic lead — day 1's check, before the pilot
    (06 §7.1) — is not the pilot's first push."""
    first = await session.scalar(
        sa.select(sa.func.min(PushLog.sent_at))
        .join(LeadCandidate, LeadCandidate.id == PushLog.candidate_id)
        .where(PushLog.status.in_(enums.PUSH_DELIVERED_STATUSES), ~synthetic_sql())
    )
    return None if first is None else first.astimezone(settings.tz).date()


async def spend_by_day(
    session: AsyncSession, *, since: datetime, until: datetime
) -> tuple[SpendDay, ...]:
    """Each Tashkent day's spend in ``[since, until)``: Anthropic and all."""
    local_day = sa.cast(
        sa.func.timezone(settings.timezone, UsageLog.created_at), sa.Date
    ).label("day")
    rows = await session.execute(
        sa.select(
            local_day,
            sa.func.coalesce(
                sa.func.sum(UsageLog.cost_usd).filter(UsageLog.provider == "anthropic"),
                0,
            ),
            sa.func.sum(UsageLog.cost_usd),
        )
        .where(UsageLog.created_at >= since, UsageLog.created_at < until)
        .group_by(local_day)
        .order_by(local_day)
    )
    return tuple(
        SpendDay(day, Decimal(anthropic), Decimal(total))
        for day, anthropic, total in rows
    )


async def mvp_report(
    session: AsyncSession,
    *,
    now: datetime,
    start: date | None = None,
    quote_names: Sequence[str] | None = None,
) -> MvpReport | None:
    """07 §3.2 over the pilot that began on ``start`` (default: the day of
    the first push), read at ``now``: week 2 = days 8–14. None before any
    push and no ``start``."""
    begun = start or await pilot_start(session)
    if begun is None:
        return None
    since = local_midnight(begun)
    week2_start, week2_end = since + WEEK, since + 2 * WEEK
    week2 = await pushes(session, since=week2_start, until=min(week2_end, now))
    return MvpReport(
        start=begun,
        now=now,
        week2_start=week2_start,
        week2_end=week2_end,
        week2_hot=sum(1 for r in week2 if r.hot),
        results=await measure(
            session, since=since, until=now, now=now, quote_names=quote_names
        ),
        spend=await spend_by_day(session, since=since, until=now),
    )


__all__ = [
    "BUSINESS_DAYS",
    "HOLAT_WINDOW",
    "HOT_SCORE",
    "JUDGED_AFTER",
    "NOT_IMPORTER_LABELS",
    "MvpReport",
    "PushRow",
    "Ratio",
    "Results",
    "SpendDay",
    "Unclassed",
    "contacted_in_time",
    "first_shipment",
    "holat",
    "importer_rate",
    "measure",
    "mvp_report",
    "next_business_day_end",
    "pilot_start",
    "precision",
    "pushes",
    "quote_stage",
    "quoted",
    "spend_by_day",
    "unclassed_labels",
]
