# VENDORED_FROM dailyJournal@6b1f67f miya/jarvis/costs.py
"""The budget controller (04 §8): every paid call goes through ``ask``.

Kept from MIYA ``jarvis/costs.py``: the spend summed in SQL from
``usage_log``, the row written in a transaction of its own (falling back to the
caller's savepoint), «an unknown price counts as over the cap», and
``CostCapReached(ModelUnavailable)`` whose message is a code, never text.
Generalised to every provider and to the refusal order month → day → family →
operation, then the unit counters, Yandex's window and the Anthropic switch.

One change of mechanism, because ≤ $10/day is a cap *enforced in code*: the
check and the record are one step. Under a transaction-scoped advisory lock
``ask`` reads the ledger, decides, and writes either the refusal row or a
*claim* row at ``est_usd`` — then commits, releasing the lock, and only then
calls. Two concurrent calls near a cap cannot both pass (each sees the other's
claim), a process killed mid-call leaves its estimate counted, and the answer's
real price later updates that same row in place (``usage_log`` refuses DELETE,
not UPDATE). The lock covers the ledger read only, never the call.

ask's own transactions never wait on the caller. The attribution columns are
foreign keys, and their check waits on a referenced row that the caller holds
``FOR UPDATE`` (every job claims its rows that way, 04 §0) while the caller
waits on ask: a deadlock Postgres cannot see. So the own transaction first
takes ``FOR KEY SHARE NOWAIT`` on each referenced row and writes only the
columns it got; the rest is written in the caller's transaction once the
money is final, and lands when the caller commits.

The two budget layers run in a fixed order: a collector's ``Budget.can_spend``
first (per source: ``source_budget_usd`` = ``sources.daily_cost_cap_usd`` minus
the source's spend today), then ``ask``. ``ask`` never reads the per-source cap.

Only a call that is free by definition (``usage.free_call``) skips the money
rungs: at the daily cap paid calls stop while the collectors carry on with the
free part (06 §4.2 ``cost_cap``), and the free ops are capped by their query
counts instead. A paid call estimated at $0 is refused as unpriced.

Days and months are the business calendar in ``TIMEZONE`` (Asia/Tashkent):
every function takes ``now`` and rows are stamped with it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings, within
from clientscrapper.db.models import Company, ReminderLog, Source, UsageLog
from clientscrapper.db.session import session_scope
from clientscrapper.services.usage import (
    ALLOCATIONS,
    CALLABLE,
    COUNTED_OPERATIONS,
    DAILY_UNIT_CAPS,
    FAMILIES,
    FAMILY,
    FAMILY_CAPS,
    MONTHLY_UNIT_CAPS,
    OP_WINDOWS,
    OPERATIONS,
    PROVIDER,
    RESERVES,
    UNITS_PER_CALL,
    Family,
    Operation,
    Priced,
    free_call,
    round_usd,
)

log = logging.getLogger(__name__)

D0: Final = Decimal(0)

# Refusal codes (the exception's message and ``usage_log.model`` after
# ``refused:``). The prefixed ones end with the family or the operation.
UNPRICED: Final = "unpriced"
CAP_MONTHLY: Final = "cost_cap_monthly"
CAP_DAILY: Final = "cost_cap_daily"
CAP_FAMILY: Final = "cost_cap_family:"
CAP_OP: Final = "cost_cap_op:"
CAP_DAY_OP: Final = "cost_cap_day_op:"
CAP_MONTH_OP: Final = "cost_cap_month_op:"
OP_WINDOW: Final = "op_window:"
ANTHROPIC_SILENCE: Final = "anthropic_silence"
REFUSED_PREFIX: Final = "refused:"
# The refusals that are the 06 §4.2 problem ``cost_cap``, told first by ``ask``.
CAP_ALERT_CODES: Final = frozenset({CAP_DAILY, CAP_MONTHLY})
# Every rung that is a cap (money or count) starts so; the others —
# unpriced, op_window, anthropic_silence — refuse a call for another reason.
CAP_PREFIX: Final = "cost_cap"


def is_cap(code: str) -> bool:
    """Is this refusal code a budget cap (as opposed to an unpriced call, an op
    outside its window, or the Anthropic API reported down)?"""
    return code.startswith(CAP_PREFIX)


# Health keys this module raises (rows of 06 §4.2 PROBLEM_KEYS) and the alert
# ledger's kind prefix (MIYA ``health.ALERT_KIND``; health.py reuses it).
COST_CAP_KEY: Final = "cost_cap"
SPEND_HIGH_KEY: Final = "spend_high"
ALERT_KIND: Final = "alert:"

# Parent rows whose dollar budget also counts their sub-sources' spend
# (02 §2.1, 03 §1.2): a call is recorded with the sub-source it was made for
# (search_brave), and the run is budgeted on the parent row (serp_layer).
SUBSOURCE_PREFIXES: Final[Mapping[str, tuple[str, ...]]] = {
    "serp_layer": ("search_",),
    "directories": ("directories_",),
    "maps": ("maps_",),
}

# The owner's switch that lets a SERP op borrow serp_reserve (default off).
SERP_OVERFLOW_SOURCE: Final = "serp_layer"
SERP_OVERFLOW_FLAG: Final = "serp_paid_overflow"

# pg_advisory_xact_lock key naming the spend ledger: every ask's read-decide-
# claim step is serialised on it (milliseconds; never held across a call).
LEDGER_LOCK: Final = 0x63730006
# The class key of an alert's claim lock, ``pg_advisory_xact_lock(ALERT_LOCK,
# hashtext('alert:<key>'))``: «is it due, then claim it» is one step, so
# refusals at a cap that land together tell the owner once (07 §1.9).
ALERT_LOCK: Final = 0x63730008

# Ops whose cap or price is a count: their claim reserves one unit before the
# call and the settled row keeps at least it.
UNIT_COUNTED: Final[frozenset[Operation]] = COUNTED_OPERATIONS
# How long ask's own transaction may wait on any lock once it holds the
# ledger lock. Nothing it does should wait at all (the foreign keys are taken
# NOWAIT); this bounds what nobody foresaw instead of hanging every ask.
OWN_LOCK_TIMEOUT: Final = "2s"
# FAMILY keyed by plain strings: ledger rows carry the operation as text.
_FAMILY_OF: Final[Mapping[str, Family]] = {str(op): f for op, f in FAMILY.items()}

FAMILY_LABEL_UZ: Final[Mapping[Family, str]] = {
    "anthropic": "Anthropic",
    "serp": "Qidiruv",
    "places": "Xaritalar",
    "apify": "Apify",
    "proxy": "Proksi",
}


class ModelUnavailable(Exception):
    """No answer from a paid API: the call was not made, failed or was
    refused. The message is a code, never text."""


class CostCapReached(ModelUnavailable):
    """``ask`` refused before anything was sent; ``code`` says which rung."""

    def __init__(self, code: str, operation: str) -> None:
        super().__init__(code)
        self.code = code
        self.operation = operation


# --- the business calendar -----------------------------------------------------


def _local(now: datetime, tz: tzinfo | None) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(tz or settings.tz)


def day_start(now: datetime, tz: tzinfo | None = None) -> datetime:
    """Midnight of ``now``'s business day (TIMEZONE, Asia/Tashkent)."""
    zone = tz or settings.tz
    return datetime.combine(_local(now, zone).date(), time.min, tzinfo=zone)


def day_end(now: datetime, tz: tzinfo | None = None) -> datetime:
    zone = tz or settings.tz
    tomorrow = _local(now, zone).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, tzinfo=zone)


def month_start(now: datetime, tz: tzinfo | None = None) -> datetime:
    zone = tz or settings.tz
    local = _local(now, zone)
    return datetime(local.year, local.month, 1, tzinfo=zone)


def month_end(now: datetime, tz: tzinfo | None = None) -> datetime:
    zone = tz or settings.tz
    local = _local(now, zone)
    year, month = (
        (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    )
    return datetime(year, month, 1, tzinfo=zone)


def next_hour(now: datetime, tz: tzinfo | None = None) -> datetime:
    """The top of the next clock hour: how long a refusal is remembered."""
    local = _local(now, tz).replace(minute=0, second=0, microsecond=0)
    return local + timedelta(hours=1)


# --- the decision (pure) -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpTotals:
    """One operation's recorded spend: money and units today, units this month."""

    day_usd: Decimal = D0
    day_units: Decimal = D0
    month_units: Decimal = D0


@dataclass(frozen=True, slots=True)
class Ledger:
    """What ``usage_log`` says about the current day and month (refusal rows
    excluded; claims of calls in flight included)."""

    month_usd: Decimal = D0
    ops: Mapping[str, OpTotals] = field(default_factory=dict)

    def op(self, operation: str) -> OpTotals:
        return self.ops.get(operation, OpTotals())

    @property
    def day_usd(self) -> Decimal:
        return sum((t.day_usd for t in self.ops.values()), D0)

    def family_day_usd(self, family: Family) -> Decimal:
        return sum(
            (t.day_usd for op, t in self.ops.items() if _FAMILY_OF.get(op) == family),
            D0,
        )


def _op_fits(
    operation: Operation, est: Decimal, ledger: Ledger, *, serp_overflow: bool
) -> bool:
    """Rung (5): the op's own allocation, then the family's reserve — the
    reserve's unspent remainder after what the other ops already borrowed."""
    need = ledger.op(operation).day_usd + est
    allocation = ALLOCATIONS[operation]
    if need < allocation:
        return True
    family = FAMILY[operation]
    reserve = RESERVES.get(family)
    if reserve is None or (family == "serp" and not serp_overflow):
        return False
    borrowed = sum(
        (
            max(D0, ledger.op(other).day_usd - ALLOCATIONS[other])
            for other in ALLOCATIONS
            if FAMILY[other] == family and other not in (operation, reserve)
        ),
        D0,
    )
    left = ALLOCATIONS[reserve] - ledger.op(reserve).day_usd - borrowed
    return need < allocation + left


def decide(
    operation: Operation,
    est_usd: Decimal | None,
    ledger: Ledger,
    *,
    local_time: time,
    daily_cap: Decimal,
    monthly_cap: Decimal,
    serp_overflow: bool = False,
    anthropic_down: bool = False,
) -> str | None:
    """The refusal code for this call, or None to let it through — 04 §8's
    rungs in their order. Money rungs refuse at ``spend + est ≥ cap`` (reaching
    a cap exactly is over it) and are skipped only for a call that is free by
    definition at $0 (``usage.free_call``); unit rungs refuse at
    ``units + 1 > cap``. ``local_time`` is ``now`` in TIMEZONE."""
    if est_usd is None or not est_usd.is_finite():
        return UNPRICED
    family = FAMILY[operation]
    totals = ledger.op(operation)
    if est_usd == 0 and not free_call(operation, month_units_before=totals.month_units):
        # A paid call priced at nothing (an estimate from no tokens, a 0/0
        # price override, a Places request past the free tier) is not priced:
        # letting it past the money rungs would bypass the hard cap.
        return UNPRICED
    if est_usd > 0:
        if ledger.month_usd + est_usd >= monthly_cap:
            return CAP_MONTHLY
        if ledger.day_usd + est_usd >= daily_cap:
            return CAP_DAILY
        if ledger.family_day_usd(family) + est_usd >= FAMILY_CAPS[family]:
            return CAP_FAMILY + family
        if not _op_fits(operation, est_usd, ledger, serp_overflow=serp_overflow):
            return CAP_OP + operation
    day_units = DAILY_UNIT_CAPS.get(operation)
    if day_units is not None and totals.day_units + UNITS_PER_CALL > day_units:
        return CAP_DAY_OP + operation
    month_units = MONTHLY_UNIT_CAPS.get(operation)
    if month_units is not None and totals.month_units + UNITS_PER_CALL > month_units:
        return CAP_MONTH_OP + operation
    window = OP_WINDOWS.get(operation)
    if window is not None and not within(local_time, *window):
        return OP_WINDOW + operation
    if family == "anthropic" and anthropic_down:
        return ANTHROPIC_SILENCE
    return None


# --- reading the ledger --------------------------------------------------------

_NOT_REFUSED = sa.or_(
    UsageLog.model.is_(None), sa.not_(UsageLog.model.startswith(REFUSED_PREFIX))
)
_REFUSED = sa.and_(UsageLog.model.is_not(None), UsageLog.model.startswith(REFUSED_PREFIX))


def _dec(value: object) -> Decimal:
    return Decimal(str(value)) if value is not None else D0


async def read_ledger(session: AsyncSession, now: datetime) -> Ledger:
    """The day's and month's totals per operation, summed in SQL."""
    start, end = day_start(now), day_end(now)
    in_day = sa.and_(UsageLog.created_at >= start, UsageLog.created_at < end)
    rows = await session.execute(
        sa.select(
            UsageLog.operation,
            sa.func.sum(UsageLog.cost_usd),
            sa.func.sum(UsageLog.cost_usd).filter(in_day),
            sa.func.sum(UsageLog.units).filter(in_day),
            sa.func.sum(UsageLog.units),
        )
        .where(
            UsageLog.created_at >= month_start(now),
            UsageLog.created_at < month_end(now),
            _NOT_REFUSED,
        )
        .group_by(UsageLog.operation)
    )
    month = D0
    ops: dict[str, OpTotals] = {}
    for operation, month_usd, day_usd, day_units, month_units in rows.all():
        month += _dec(month_usd)
        ops[operation] = OpTotals(_dec(day_usd), _dec(day_units), _dec(month_units))
    return Ledger(month_usd=month, ops=ops)


@dataclass(frozen=True, slots=True)
class Spend:
    """``usd``/``units``/``calls`` over the rows of recorded calls; ``refused``
    counts the refusal rows and ``unpriced`` those refused as unpriced."""

    usd: Decimal
    unpriced: int
    calls: int = 0
    refused: int = 0
    units: Decimal = D0


async def spend(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime | None = None,
    operations: Sequence[Operation] | None = None,
    family: Family | None = None,
    company_id: UUID | None = None,
    source_key: str | None = None,
    with_subsources: bool = False,
) -> Spend:
    """The spend recorded in ``[since, until)``, optionally narrowed;
    ``with_subsources`` widens ``source_key`` to a parent row's sub-sources
    (``SUBSOURCE_PREFIXES``)."""
    where: list[sa.ColumnElement[bool]] = [UsageLog.created_at >= since]
    if until is not None:
        where.append(UsageLog.created_at < until)
    if operations is not None:
        where.append(UsageLog.operation.in_(list(operations)))
    if family is not None:
        where.append(
            UsageLog.operation.in_([op for op, f in FAMILY.items() if f == family])
        )
    if company_id is not None:
        where.append(UsageLog.company_id == company_id)
    if source_key is not None:
        keys = [UsageLog.source_key == source_key]
        if with_subsources:
            keys += [
                UsageLog.source_key.startswith(prefix, autoescape=True)
                for prefix in SUBSOURCE_PREFIXES.get(source_key, ())
            ]
        where.append(sa.or_(*keys))
    row = (
        await session.execute(
            sa.select(
                sa.func.sum(UsageLog.cost_usd).filter(_NOT_REFUSED),
                sa.func.sum(UsageLog.units).filter(_NOT_REFUSED),
                sa.func.count().filter(_NOT_REFUSED),
                sa.func.count().filter(_REFUSED),
                sa.func.count().filter(UsageLog.model == REFUSED_PREFIX + UNPRICED),
            ).where(*where)
        )
    ).one()
    return Spend(
        usd=_dec(row[0]),
        units=_dec(row[1]),
        calls=int(row[2] or 0),
        refused=int(row[3] or 0),
        unpriced=int(row[4] or 0),
    )


async def source_budget_usd(
    session: AsyncSession, *, source_key: str, now: datetime
) -> Decimal:
    """``Budget.usd`` of 03 §1.2: the column ``sources.daily_cost_cap_usd``
    minus this source's spend today — a parent row's includes its
    sub-sources' — never below zero; an unknown source has nothing to spend.
    The one home of the rule: the runner's ``build_budget`` asks it."""
    cap = await session.scalar(
        sa.select(Source.daily_cost_cap_usd).where(Source.key == source_key)
    )
    if cap is None:
        return D0
    used = await spend(
        session,
        since=day_start(now),
        until=day_end(now),
        source_key=source_key,
        with_subsources=True,
    )
    return max(D0, Decimal(cap) - used.usd)


async def month_units(
    session: AsyncSession, *, operation: Operation, now: datetime
) -> Decimal:
    """Units of ``operation`` used this calendar month (the Places free tier)."""
    used = await spend(
        session, since=month_start(now), until=month_end(now), operations=[operation]
    )
    return used.units


async def serp_overflow(session: AsyncSession) -> bool:
    """The owner's switch ``sources.config.serp_paid_overflow`` — strictly JSON
    ``true``; anything else is off."""
    value = await session.scalar(
        sa.select(Source.config.contains({SERP_OVERFLOW_FLAG: True})).where(
            Source.key == SERP_OVERFLOW_SOURCE
        )
    )
    return bool(value)


# --- process memory and hooks ---------------------------------------------------

# op → (until, code): a refused op is not asked again before the next hour —
# no ledger read and no further row (usage_log is never pruned, R7). The bot
# counts refusals from the rows, never from here.
_refused: dict[str, tuple[datetime, str]] = {}

AnthropicHealth = Callable[[AsyncSession, datetime], Awaitable[bool]]
CapNotifier = Callable[[str], Awaitable[bool]]


@dataclass(slots=True)
class _Hooks:
    # health.py (T-08) registers both: «is the Anthropic API down» (06 §4.2
    # anthropic_silence) and the owner notifier that tells cost_cap first.
    anthropic_down: AnthropicHealth | None = None
    notify_cap: CapNotifier | None = None


_hooks = _Hooks()


def set_anthropic_health(check: AnthropicHealth | None) -> None:
    _hooks.anthropic_down = check


def set_cap_notifier(notify: CapNotifier | None) -> None:
    """``notify(text) -> delivered``; a cost_cap is marked only once delivered."""
    _hooks.notify_cap = notify


def refused_until(operation: str, now: datetime) -> datetime | None:
    """Until when ``operation`` stays refused in this process (jobs skip it)."""
    held = _refused.get(operation)
    if held is None or now >= held[0]:
        return None
    return held[0]


def clear_refusals() -> None:
    _refused.clear()


async def _anthropic_is_down(session: AsyncSession, now: datetime) -> bool:
    check = _hooks.anthropic_down
    if check is None:
        return False
    try:
        return await check(session, now)
    except Exception as exc:
        # The money rungs are the hard limit; a broken health probe must not
        # stop the classifier.
        log.warning("anthropic health check failed", extra={"error": type(exc).__name__})
        return False


# --- writing the ledger --------------------------------------------------------

# Indirection for tests: the transaction of ask's own.
_own_session: Callable[[], AbstractAsyncContextManager[AsyncSession]] = session_scope

_LOCK_LEDGER: Final = sa.select(sa.func.pg_advisory_xact_lock(LEDGER_LOCK))
_UNSET: Final = object()
_LOCK_NOT_AVAILABLE: Final = "55P03"


class _ClaimNotVisible(Exception):
    """The settle found no claim row: it sits uncommitted in the caller's
    transaction, so the settle must be written there too."""


def _sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def _referenced(column: str) -> sa.ColumnElement[Any]:
    """The key ``usage_log.<column>`` references, read off the schema."""
    (fk,) = UsageLog.__table__.c[column].foreign_keys
    return fk.column


async def _key_shared(session: AsyncSession, wanted: Mapping[str, Any]) -> dict[str, Any]:
    """The attribution columns this transaction may write without waiting.

    Each referenced row is taken ``FOR KEY SHARE NOWAIT``; holding that lock,
    the write's foreign-key check cannot wait. A row another transaction holds
    ``FOR UPDATE`` (the model_jobs row a job claimed) or has not committed yet
    is left out, for the caller's transaction to write later."""
    got: dict[str, Any] = {}
    for column, value in wanted.items():
        key = _referenced(column)
        probe = (
            sa.select(key)
            .where(key == value)
            .with_for_update(read=True, key_share=True, nowait=True)
        )
        try:
            async with session.begin_nested():
                found = await session.scalar(probe)
        except sa.exc.OperationalError as exc:
            if _sqlstate(exc) != _LOCK_NOT_AVAILABLE:
                raise
            continue
        if found is not None:
            got[column] = value
    return got


async def _write[X](
    session: AsyncSession,
    body: Callable[[AsyncSession], Awaitable[X]],
    *,
    what: str,
    lock: bool,
    own: bool = True,
) -> tuple[X, bool]:
    """``body(s)`` in a transaction of its own, so a later failure of the
    caller cannot roll the spend back; returns (value, True). The own
    transaction takes the ledger lock first when ``lock`` and never waits on a
    row lock past ``OWN_LOCK_TIMEOUT``. With ``own=False`` (the claim already
    sits in the caller's transaction, where no other one can see it), or when
    no own transaction can be had, the caller's savepoint takes it (MIYA
    ``_record``): (value, False)."""
    if own:
        try:
            async with _own_session() as s:
                if lock:
                    await s.execute(_LOCK_LEDGER)
                await s.execute(
                    sa.select(sa.func.set_config("lock_timeout", OWN_LOCK_TIMEOUT, True))
                )
                return await body(s), True
        except Exception as exc:
            log.warning(
                "usage ledger written in the caller's transaction",
                extra={"what": what, "error": type(exc).__name__},
            )
    # The caller's transaction outlives this call, so an xact lock would be
    # held across the API call: take the session lock and give it back.
    if lock:
        await session.execute(sa.select(sa.func.pg_advisory_lock(LEDGER_LOCK)))
    try:
        async with session.begin_nested():
            value = await body(session)
            await session.flush()
    finally:
        if lock:
            await session.execute(sa.select(sa.func.pg_advisory_unlock(LEDGER_LOCK)))
    return value, False


def _attribution(
    *,
    source_key: str | None = None,
    company_id: UUID | None = None,
    model_job_id: int | None = None,
    collector_run_id: int | None = None,
) -> dict[str, Any]:
    values = {
        "source_key": source_key,
        "company_id": company_id,
        "model_job_id": model_job_id,
        "collector_run_id": collector_run_id,
    }
    return {k: v for k, v in values.items() if v is not None}


@dataclass(slots=True)
class _Attributions:
    """Per usage_log row: the attribution it should carry and what is stored."""

    wanted: dict[int, dict[str, Any]] = field(default_factory=dict)
    stored: dict[int, dict[str, Any]] = field(default_factory=dict)

    def note(
        self, row_id: int, wanted: Mapping[str, Any], stored: Mapping[str, Any]
    ) -> None:
        self.wanted[row_id] = dict(wanted)
        self.stored.setdefault(row_id, {}).update(stored)

    def missing(self, row_id: int) -> dict[str, Any]:
        have = self.stored.get(row_id, {})
        return {
            k: v
            for k, v in self.wanted.get(row_id, {}).items()
            if have.get(k, _UNSET) != v
        }


async def _attribute_in_caller(session: AsyncSession, rows: _Attributions) -> None:
    """The attribution ask's own transactions could not take: written in the
    caller's transaction once the money is final, so it lands when the caller
    commits; the committed money stands either way. One savepoint per column,
    so one bad value does not cost the others."""
    for row_id in list(rows.wanted):
        for column, value in rows.missing(row_id).items():
            try:
                async with session.begin_nested():
                    await session.execute(
                        sa.update(UsageLog)
                        .where(UsageLog.id == row_id)
                        .values({column: value})
                    )
            except Exception as exc:
                log.warning(
                    "usage attribution not recorded",
                    extra={"column": column, "error": type(exc).__name__},
                )


def _valid_cost(cost: Decimal | None) -> Decimal | None:
    if cost is None or not cost.is_finite() or cost < 0:
        return None
    return cost


def _answer_costs(answers: Sequence[Priced], estimate: Decimal) -> list[Decimal]:
    """Each row's cost: its own price, or — for an answer that could not be
    priced (None, NaN, negative) — an equal share of what the estimate has
    left, so the rows together never fall below the claim (04 §8: the cap
    never undercounts, an unknown price counts as over it)."""
    valid = [_valid_cost(p.cost_usd) for p in answers]
    unpriced = sum(1 for cost in valid if cost is None)
    share = D0
    if unpriced:
        log.warning("an answer could not be priced; the estimate stays")
        priced = sum((cost for cost in valid if cost is not None), D0)
        share = round_usd(max(D0, estimate - priced) / unpriced)
    return [share if cost is None else cost for cost in valid]


def _units(value: Decimal, floor: Decimal) -> Decimal:
    usable = value if value.is_finite() and value >= 0 else D0
    return max(usable, floor)


def _priced_values(
    p: Priced, cost: Decimal, *, min_units: Decimal = D0
) -> dict[str, Any]:
    return {
        "model": p.model,
        "input_tokens": p.input_tokens,
        "output_tokens": p.output_tokens,
        "cache_read_tokens": p.cache_read_tokens,
        "cache_write_tokens": p.cache_write_tokens,
        "units": _units(p.units, min_units),
        "batch": p.batch,
        "cost_usd": cost,
    }


# --- ask -----------------------------------------------------------------------


async def ask[T](
    session: AsyncSession,
    *,
    operation: Operation,
    est_usd: Decimal | None,
    call: Callable[[], Awaitable[T]],
    price: Callable[[T], Priced | Sequence[Priced]],
    now: datetime,
    source_key: str | None = None,
    company_id: UUID | None = None,
    model_job_id: int | None = None,
    collector_run_id: int | None = None,
) -> T:
    """One paid call, or ``CostCapReached`` before anything is sent.

    ``est_usd`` is the caller's priced estimate (``usage.anthropic_cost_usd`` /
    ``usage.unit_cost``; ``None`` = unpriced, refused, and so is $0 for a call
    that is not free by definition). It is claimed before the call, so it
    should not be below what the call can cost (estimate output at
    ``max_tokens``). ``price(result)`` turns the answer into its row — or one
    row per job for a batch submit (provisional, settled by
    ``settle_provisional``); an answer it cannot price keeps the estimate. If
    ``call()`` raises, the claim stays at ``est_usd`` with ``units=0`` and the
    exception propagates. A refusal is persisted as a ``usage_log`` row
    (``cost_usd=0, units=0, model='refused:<code>'``) and remembered in this
    process until the next hour."""
    if operation not in CALLABLE:
        raise ValueError(f"{operation} is not a callable operation")
    if est_usd is not None:
        if not isinstance(est_usd, Decimal):
            raise TypeError("est_usd must be a Decimal")
        if est_usd.is_finite() and est_usd < 0:
            raise ValueError("est_usd < 0")
    held = _refused.get(operation)
    if held is not None and _local(now, None) < held[0]:
        raise CostCapReached(held[1], operation)

    down = FAMILY[operation] == "anthropic" and await _anthropic_is_down(session, now)
    wanted = _attribution(
        source_key=source_key,
        company_id=company_id,
        model_job_id=model_job_id,
        collector_run_id=collector_run_id,
    )
    counted = operation in UNIT_COUNTED

    async def claim(s: AsyncSession) -> tuple[str | None, int, dict[str, Any]]:
        totals = await read_ledger(s, now)
        overflow = FAMILY[operation] == "serp" and await serp_overflow(s)
        code = decide(
            operation,
            est_usd,
            totals,
            local_time=_local(now, None).time(),
            daily_cap=settings.daily_cost_cap_usd,
            monthly_cap=settings.monthly_cost_cap_usd,
            serp_overflow=overflow,
            anthropic_down=down,
        )
        attach = await _key_shared(s, wanted)
        row = UsageLog(
            provider=PROVIDER[operation],
            operation=operation,
            model=None if code is None else REFUSED_PREFIX + code,
            units=UNITS_PER_CALL if code is None and counted else D0,
            batch=False,
            cost_usd=est_usd if code is None and est_usd is not None else D0,
            created_at=now,
            **attach,
        )
        s.add(row)
        await s.flush()
        return code, row.id, attach

    (code, claim_id, attached), claim_own = await _write(
        session, claim, what="claim", lock=True
    )
    rows = _Attributions()
    rows.note(claim_id, wanted, attached)
    if code is not None:
        _refused[operation] = (next_hour(now), code)
        log.info("paid call refused", extra={"op": operation, "code": code})
        await _attribute_in_caller(session, rows)
        if code in CAP_ALERT_CODES:
            await _tell_cost_cap(code, now)
        raise CostCapReached(code, operation)

    try:
        result = await call()
    except asyncio.CancelledError:
        # Dispatched or not, the estimate stays counted: never undercount.
        if counted:
            await _release_units(session, claim_id, own=claim_own)
        raise
    except Exception:
        if counted:
            await _release_units(session, claim_id, own=claim_own)
        await _attribute_in_caller(session, rows)
        raise

    priced = price(result)
    answers = [priced] if isinstance(priced, Priced) else list(priced)
    if not answers:
        log.warning(
            "price() returned no row; the estimate stays", extra={"op": operation}
        )
        await _attribute_in_caller(session, rows)
        return result
    estimate = est_usd if est_usd is not None else D0
    cost_of = _answer_costs(answers, estimate)
    wants = [
        wanted | _attribution(company_id=p.company_id, model_job_id=p.model_job_id)
        for p in answers
    ]
    rows.note(claim_id, wants[0], {})

    async def settle(s: AsyncSession) -> list[tuple[int, dict[str, Any]]]:
        first = _priced_values(
            answers[0], cost_of[0], min_units=UNITS_PER_CALL if counted else D0
        )
        attach = await _key_shared(s, rows.missing(claim_id))
        updated = await s.scalar(
            sa.update(UsageLog)
            .where(UsageLog.id == claim_id)
            .values(**first, **attach)
            .returning(UsageLog.id)
        )
        if updated is None:
            raise _ClaimNotVisible(claim_id)
        written = [(claim_id, attach)]
        for p, cost, want in zip(answers[1:], cost_of[1:], wants[1:], strict=True):
            more = await _key_shared(s, want)
            extra = UsageLog(
                provider=PROVIDER[operation],
                operation=operation,
                created_at=now,
                **_priced_values(p, cost),
                **more,
            )
            s.add(extra)
            await s.flush()
            written.append((extra.id, more))
        return written

    try:
        settled, _ = await _write(
            session, settle, what="settle", lock=False, own=claim_own
        )
    except Exception as exc:
        log.error(
            "usage not settled; the estimate stays",
            extra={"op": operation, "error": type(exc).__name__},
        )
        await _attribute_in_caller(session, rows)
        return result
    for (row_id, stored), want in zip(settled, wants, strict=True):
        rows.note(row_id, want, stored)
    await _attribute_in_caller(session, rows)
    return result


async def _release_units(session: AsyncSession, claim_id: int, *, own: bool) -> None:
    async def body(s: AsyncSession) -> None:
        await s.execute(
            sa.update(UsageLog).where(UsageLog.id == claim_id).values(units=D0)
        )

    try:
        await _write(session, body, what="release", lock=False, own=own)
    except Exception as exc:
        # The unit stays reserved: an overcount, the safe direction.
        log.warning("claimed unit not released", extra={"error": type(exc).__name__})


async def settle_provisional(
    session: AsyncSession,
    *,
    model_job_id: int,
    priced: Priced,
    operation: Operation = "company_verdict",
    now: datetime,
) -> bool:
    """A batch result's real tokens and price, written over the job's
    provisional row (04 §4.8) in the caller's transaction — the one that
    applies the result. True when a provisional row was updated; False when
    none was found and a row was inserted instead (the spend is never lost).
    A result that cannot be priced keeps the provisional estimate."""
    row_id = await session.scalar(
        sa.select(UsageLog.id)
        .where(
            UsageLog.model_job_id == model_job_id,
            UsageLog.operation == operation,
            UsageLog.batch.is_(True),
            UsageLog.input_tokens.is_(None),
            _NOT_REFUSED,
        )
        .order_by(UsageLog.id.desc())
        .limit(1)
    )
    if row_id is None:
        log.warning("no provisional usage row", extra={"model_job_id": model_job_id})
        [cost] = _answer_costs([priced], D0)
        session.add(
            UsageLog(
                provider=PROVIDER[operation],
                operation=operation,
                model_job_id=model_job_id,
                created_at=now,
                **_priced_values(priced, cost),
            )
        )
        await session.flush()
        return False
    keep = await session.scalar(sa.select(UsageLog.cost_usd).where(UsageLog.id == row_id))
    [cost] = _answer_costs([priced], _dec(keep))
    await session.execute(
        sa.update(UsageLog)
        .where(UsageLog.id == row_id)
        .values(**_priced_values(priced, cost))
    )
    return True


# --- alerts (06 §4.2 spend_high, cost_cap) --------------------------------------


def _money(value: Decimal) -> str:
    return f"{value:.0f}" if value == value.to_integral_value() else f"{value:.2f}"


def _pct(spent: Decimal, cap: Decimal) -> int:
    return int(spent * 100 // cap)


def cost_cap_text(code: str, *, daily_cap: Decimal, monthly_cap: Decimal) -> str:
    if code == CAP_MONTHLY:
        return (
            f"💸 Oylik xarajat chegarasi (${_money(monthly_cap)}) — oy oxirigacha "
            "pullik chaqiruvlar toʻxtadi; kollektorlar bepul qismini davom ettiradi"
        )
    return (
        f"💸 Kunlik xarajat chegarasi (${_money(daily_cap)}) — ertagacha pullik "
        "chaqiruvlar toʻxtadi; kollektorlar bepul qismini davom ettiradi"
    )


@dataclass(frozen=True, slots=True)
class SpendSnapshot:
    day_usd: Decimal
    month_usd: Decimal
    family_day_usd: Mapping[Family, Decimal]


@dataclass(frozen=True, slots=True)
class SpendAlert:
    key: str
    text: str


def spend_alerts(
    snap: SpendSnapshot,
    *,
    daily_cap: Decimal,
    monthly_cap: Decimal,
    alert_pct: int,
) -> list[SpendAlert]:
    """``cost_cap`` at a cap; ``spend_high`` — one key whose text names each
    scope (day, month, a family) at or above ``alert_pct`` of its cap."""
    alerts: list[SpendAlert] = []
    if snap.month_usd >= monthly_cap or snap.day_usd >= daily_cap:
        code = CAP_MONTHLY if snap.month_usd >= monthly_cap else CAP_DAILY
        text = cost_cap_text(code, daily_cap=daily_cap, monthly_cap=monthly_cap)
        alerts.append(SpendAlert(COST_CAP_KEY, text))

    def high(spent: Decimal, cap: Decimal) -> bool:
        return cap > 0 and spent * 100 >= cap * alert_pct

    lines: list[str] = []
    if high(snap.day_usd, daily_cap):
        lines.append(
            f"⚠️ Kunlik xarajat {_pct(snap.day_usd, daily_cap)}% ga yetdi "
            f"(${snap.day_usd:.2f}/${_money(daily_cap)})"
        )
    if high(snap.month_usd, monthly_cap):
        lines.append(
            f"⚠️ Oylik xarajat {_pct(snap.month_usd, monthly_cap)}% ga yetdi "
            f"(${snap.month_usd:.2f}/${_money(monthly_cap)})"
        )
    for family in FAMILIES:
        spent, cap = snap.family_day_usd.get(family, D0), FAMILY_CAPS[family]
        if high(spent, cap):
            lines.append(
                f"⚠️ {FAMILY_LABEL_UZ[family]} xarajati {_pct(spent, cap)}% ga yetdi "
                f"(${spent:.2f}/${_money(cap)})"
            )
    if lines:
        alerts.append(SpendAlert(SPEND_HIGH_KEY, "\n".join(lines)))
    return alerts


async def snapshot(session: AsyncSession, now: datetime) -> SpendSnapshot:
    totals = await read_ledger(session, now)
    return SpendSnapshot(
        day_usd=totals.day_usd,
        month_usd=totals.month_usd,
        family_day_usd={f: totals.family_day_usd(f) for f in FAMILIES},
    )


async def alert_due(session: AsyncSession, key: str, now: datetime) -> bool:
    """No ``alert:<key>`` row yet in this business day."""
    sent = await session.scalar(
        sa.select(sa.func.count(ReminderLog.id)).where(
            ReminderLog.kind == ALERT_KIND + key,
            ReminderLog.sent_at >= day_start(now),
            ReminderLog.sent_at < day_end(now),
        )
    )
    return not sent


def mark_alerted(session: AsyncSession, key: str, now: datetime) -> None:
    """Record a delivered alert (the caller's commit writes it)."""
    session.add(ReminderLog(kind=ALERT_KIND + key, ref=key, sent_at=now))


async def claim_alert(session: AsyncSession, key: str, now: datetime) -> int | None:
    """Claim ``alert:<key>`` for ``now``'s business day: the id of the
    ``reminder_log`` row written, or None when one already stands. The check
    and the write hold ``pg_advisory_xact_lock(ALERT_LOCK, hashtext(kind))``
    until the caller's transaction ends, so of two claims at once one wins —
    the alert is said once, not once per process that saw the cap. Commit
    before notifying: the lock must not be held across a network call."""
    kind = ALERT_KIND + key
    await session.execute(
        sa.select(sa.func.pg_advisory_xact_lock(ALERT_LOCK, sa.func.hashtext(kind)))
    )
    if not await alert_due(session, key, now):
        return None
    row = ReminderLog(kind=kind, ref=key, sent_at=now)
    session.add(row)
    await session.flush()
    return row.id


async def _say_once(key: str, text: str, now: datetime, notify: CapNotifier) -> bool:
    """Claim, then tell; a claim whose message did not arrive is taken back,
    so the next refusal (or health run) tries again. Each step is its own
    transaction: the claim is committed before ``notify`` is awaited."""
    async with _own_session() as own:
        claim = await claim_alert(own, key, now)
    if claim is None:
        return False
    delivered = False
    try:
        delivered = await notify(text)
    finally:
        if not delivered:
            try:
                async with _own_session() as own:
                    await own.execute(
                        sa.delete(ReminderLog).where(ReminderLog.id == claim)
                    )
            except Exception as exc:
                # The claim stays: the alert is not said again today — the
                # quiet direction, and /holat still shows the cap.
                log.warning(
                    "alert claim not released",
                    extra={"key": key, "error": type(exc).__name__},
                )
    return delivered


async def fire_spend_alerts(
    session: AsyncSession, *, now: datetime, notify: CapNotifier
) -> list[str]:
    """Say each holding spend alert at most once per business day; returns the
    keys delivered now (health_job calls this; ``ask`` tells cost_cap itself,
    and the two share the claim, so cost_cap is said once whichever is
    first). ``session`` reads the ledger; each claim is its own transaction."""
    alerts = spend_alerts(
        await snapshot(session, now),
        daily_cap=settings.daily_cost_cap_usd,
        monthly_cap=settings.monthly_cost_cap_usd,
        alert_pct=settings.spend_alert_pct,
    )
    fired: list[str] = []
    for alert in alerts:
        if await _say_once(alert.key, alert.text, now, notify):
            fired.append(alert.key)
    return fired


async def _tell_cost_cap(code: str, now: datetime) -> None:
    """The first refusal at a cap tells the owner at once (06 §4.2
    TOLD_FIRST_ELSEWHERE); once per business day — claimed before it is said
    (``claim_alert``), kept only if delivered."""
    notify = _hooks.notify_cap
    if notify is None:
        return
    text = cost_cap_text(
        code,
        daily_cap=settings.daily_cost_cap_usd,
        monthly_cap=settings.monthly_cost_cap_usd,
    )
    try:
        await _say_once(COST_CAP_KEY, text, now, notify)
    except Exception as exc:
        log.warning("cost_cap alert not told", extra={"error": type(exc).__name__})


# --- /xarajat (06 §1.2, §1.4) ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpLine:
    """One operation's (or provider's) recorded calls and money, and its
    refusal rows counted per code (``refused:<code>`` without the prefix)."""

    calls: int = 0
    units: Decimal = D0
    usd: Decimal = D0
    refused: int = 0
    refused_by_code: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UsageLines:
    """The same rows twice: by ``usage_log.operation`` and by ``provider``."""

    by_operation: Mapping[str, OpLine]
    by_provider: Mapping[str, OpLine]


def _fold(
    lines: dict[str, OpLine],
    key: str,
    n: int,
    units: Decimal,
    usd: Decimal,
    code: str | None,
) -> None:
    line = lines.get(key, OpLine())
    if code is None:
        lines[key] = OpLine(
            line.calls + n,
            line.units + units,
            line.usd + usd,
            line.refused,
            line.refused_by_code,
        )
        return
    codes = dict(line.refused_by_code)
    codes[code] = codes.get(code, 0) + n
    lines[key] = OpLine(line.calls, line.units, line.usd, line.refused + n, codes)


async def usage_lines(
    session: AsyncSession, *, since: datetime, until: datetime
) -> UsageLines:
    code = sa.case(
        (_REFUSED, sa.func.substr(UsageLog.model, len(REFUSED_PREFIX) + 1)), else_=None
    )
    rows = await session.execute(
        sa.select(
            UsageLog.provider,
            UsageLog.operation,
            code,
            sa.func.count(),
            sa.func.sum(UsageLog.units),
            sa.func.sum(UsageLog.cost_usd),
        )
        .where(UsageLog.created_at >= since, UsageLog.created_at < until)
        .group_by(UsageLog.provider, UsageLog.operation, code)
    )
    by_operation: dict[str, OpLine] = {}
    by_provider: dict[str, OpLine] = {}
    for provider, operation, refusal, n, units, usd in rows.all():
        for lines, key in ((by_operation, operation), (by_provider, provider)):
            _fold(lines, key, int(n), _dec(units), _dec(usd), refusal)
    return UsageLines(by_operation=by_operation, by_provider=by_provider)


async def operation_lines(
    session: AsyncSession, *, since: datetime, until: datetime
) -> dict[str, OpLine]:
    return dict((await usage_lines(session, since=since, until=until)).by_operation)


@dataclass(frozen=True, slots=True)
class TopCompany:
    company_id: UUID
    name: str
    usd: Decimal


@dataclass(frozen=True, slots=True)
class SpendReport:
    """Today and this month per operation and per provider, with the refusals
    counted per code and the most expensive companies of the day: what
    /xarajat and the digest print."""

    day: Mapping[str, OpLine]
    month: Mapping[str, OpLine]
    daily_cap: Decimal
    monthly_cap: Decimal
    day_by_provider: Mapping[str, OpLine] = field(default_factory=dict)
    month_by_provider: Mapping[str, OpLine] = field(default_factory=dict)
    top_companies: tuple[TopCompany, ...] = ()

    @staticmethod
    def total(lines: Mapping[str, OpLine], family: Family | None = None) -> Decimal:
        return sum(
            (
                line.usd
                for op, line in lines.items()
                if family is None or _FAMILY_OF.get(op) == family
            ),
            D0,
        )

    @staticmethod
    def refusals(lines: Mapping[str, OpLine]) -> int:
        return sum(line.refused for line in lines.values())

    @staticmethod
    def refusals_by_code(lines: Mapping[str, OpLine]) -> dict[str, int]:
        codes: dict[str, int] = {}
        for line in lines.values():
            for refusal, n in line.refused_by_code.items():
                codes[refusal] = codes.get(refusal, 0) + n
        return codes


# 06 §1.2: «top 5 companies by spend (ids and names only)».
TOP_COMPANIES: Final = 5


async def report(
    session: AsyncSession, *, now: datetime, top: int = TOP_COMPANIES
) -> SpendReport:
    start, end = day_start(now), day_end(now)
    usd = sa.func.sum(UsageLog.cost_usd).label("usd")
    companies = await session.execute(
        sa.select(UsageLog.company_id, Company.display_name, usd)
        .join(Company, Company.id == UsageLog.company_id)
        .where(UsageLog.created_at >= start, UsageLog.created_at < end, _NOT_REFUSED)
        .group_by(UsageLog.company_id, Company.display_name)
        .order_by(usd.desc(), UsageLog.company_id)
        .limit(top)
    )
    day = await usage_lines(session, since=start, until=end)
    month = await usage_lines(session, since=month_start(now), until=month_end(now))
    return SpendReport(
        day=day.by_operation,
        month=month.by_operation,
        daily_cap=settings.daily_cost_cap_usd,
        monthly_cap=settings.monthly_cost_cap_usd,
        day_by_provider=day.by_provider,
        month_by_provider=month.by_provider,
        top_companies=tuple(
            TopCompany(cid, name, _dec(total)) for cid, name, total in companies.all()
        ),
    )


__all__ = [
    "ALERT_KIND",
    "ALERT_LOCK",
    "ALLOCATIONS",
    "ANTHROPIC_SILENCE",
    "CALLABLE",
    "CAP_DAILY",
    "CAP_DAY_OP",
    "CAP_FAMILY",
    "CAP_MONTHLY",
    "CAP_MONTH_OP",
    "CAP_OP",
    "CAP_PREFIX",
    "COST_CAP_KEY",
    "DAILY_UNIT_CAPS",
    "FAMILY",
    "FAMILY_CAPS",
    "LEDGER_LOCK",
    "MONTHLY_UNIT_CAPS",
    "OPERATIONS",
    "OP_WINDOW",
    "OP_WINDOWS",
    "OWN_LOCK_TIMEOUT",
    "PROVIDER",
    "REFUSED_PREFIX",
    "RESERVES",
    "SERP_OVERFLOW_FLAG",
    "SERP_OVERFLOW_SOURCE",
    "SPEND_HIGH_KEY",
    "SUBSOURCE_PREFIXES",
    "TOP_COMPANIES",
    "UNITS_PER_CALL",
    "UNIT_COUNTED",
    "UNPRICED",
    "CostCapReached",
    "Ledger",
    "ModelUnavailable",
    "OpLine",
    "OpTotals",
    "Operation",
    "Priced",
    "Spend",
    "SpendAlert",
    "SpendReport",
    "SpendSnapshot",
    "TopCompany",
    "UsageLines",
    "alert_due",
    "ask",
    "claim_alert",
    "clear_refusals",
    "cost_cap_text",
    "day_end",
    "day_start",
    "decide",
    "fire_spend_alerts",
    "is_cap",
    "mark_alerted",
    "month_end",
    "month_start",
    "month_units",
    "next_hour",
    "operation_lines",
    "read_ledger",
    "refused_until",
    "report",
    "serp_overflow",
    "set_anthropic_health",
    "set_cap_notifier",
    "settle_provisional",
    "snapshot",
    "source_budget_usd",
    "spend",
    "spend_alerts",
    "usage_lines",
]
