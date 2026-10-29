# VENDORED_FROM dailyJournal@6b1f67f miya/services/health.py
"""Self-monitoring (06 §4.1–4.3): heartbeats, one status, problems, alerts
with memory. MIYA's core, kept verbatim where it fits:

* **Heartbeats** — every process, every scheduler job and every collector run
  upserts one row in ``heartbeats`` through :func:`beat`; :func:`beats` reads
  them back. One vocabulary across 02 §2.13, 03 §1.2, 05 and 06: ``bot``,
  ``worker``, ``userbot``, ``backup``, ``backup_counts``, ``restore_drill``,
  ``job:<id>`` and ``collector:<key>[.variant]`` (:func:`collector_component`).
* **Status** — :func:`gather` collects the heartbeats, the database, the disk,
  the newest backup, the sources, the queues, the spend and the Anthropic
  facts into one :class:`Status` (a bounded set of indexed reads), which
  ``/holat`` renders and the sweep judges. It tolerates an empty database and
  a missing backup directory.
* **Problems** — :func:`problems` is pure: Status in, the 06 §4.2 problems out
  in ``PROBLEM_KEYS`` order, each with its Uzbek text and remedy; every key
  has its recovery line (:func:`recovery_text`). ``PROBLEM_KEYS`` is the one
  list of keys: :func:`raise_problem` refuses any other, and
  ``tests/test_health.py`` refuses a key raised anywhere in the package that
  is not in it.
* **Alert memory** — :func:`alerts_due` and the ``mark_*`` helpers keep a
  problem from being said more often than ``ALERT_REPEAT_HOURS`` and produce
  one recovery notice when it clears. The memory is ``reminder_log``
  (``alert:<key>`` / ``recovered:<key>``), shared by every process, so the
  worker's health job and the bot's watchdog never repeat each other; and
  :func:`tell` *claims* each line under the same advisory lock ``costs.ask``
  takes before it tells ``cost_cap`` (``costs.ALERT_LOCK``), so two speakers at
  once say a line once.

Three ways a condition reaches the status:

1. read from the tables the spec names (breakers, heartbeats, ``push_log``,
   ``model_jobs``, ``usage_log``, ``runtime_flags``, ``chat_monitors`` …);
2. **raised** by the module that sees it happen (``REPORTED_KEYS``: a feed
   contract break, a PII leak in a model answer, a shrunk feed …) through
   :func:`raise_problem` / :func:`clear_problem` — a ``runtime_flags`` row
   ``problem:<key>`` that survives restarts and that any process can clear;
3. a **probe** the owning module registers (``set_suppression_probe``: the
   freshness rule's home is ``services/erp_feeds.suppression_fresh``, T-09).
   Until it is registered the key is *unjudged*: neither raised nor
   announced as recovered.

``erp_breaker_text`` (T-09) is the one home of the ERP breaker's owner line
(06 §4.2 ``push_404`` / ``erp_redirect``): the feed sync says it at once
(``TOLD_FIRST_ELSEWHERE``) and :func:`problems` renders both keys through it,
so the line the owner was told first and the one ``/holat`` shows are one.

Nothing here sends anything: :func:`tell` is handed the owner notifier — the
one seam, whose only recipient is ``OWNER_TELEGRAM_ID`` (06 §4.3) — and
records only what was delivered.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings, within
from clientscrapper.db.models import (
    ChatMonitor,
    CollectorRun,
    Evidence,
    Heartbeat,
    Interaction,
    LeadCandidate,
    LeadReview,
    ModelJob,
    Observation,
    PushLog,
    ReminderLog,
    RuntimeFlag,
    Source,
    UsageLog,
)
from clientscrapper.db.session import session_scope
from clientscrapper.services import backup, costs, crontab, smoke

log = logging.getLogger(__name__)

COMPONENTS: Final = ("bot", "worker", "userbot")
JOB_PREFIX: Final = "job:"
COLLECTOR_PREFIX: Final = "collector:"
# The heartbeat the backup job writes: {ok, size, sent, path, error}.
BACKUP_COMPONENT: Final = "backup"
# What the backup job counted at dump time, for the drill: {path, counts}.
BACKUP_COUNTS_COMPONENT: Final = "backup_counts"
RESTORE_DRILL_COMPONENT: Final = "restore_drill"

# A sources.key (02 §2.1) and a cadence variant: «erp_sync.full»,
# «serp_layer.night», «telegram_monitor.backfill».
_KEY = re.compile(r"[a-z][a-z0-9_]{2,40}")
_VARIANT = re.compile(r"[a-z][a-z0-9_]{0,30}")

# The alert ledger's kinds (MIYA constants; 02 §2.13). costs.py tells
# cost_cap with the same prefix, so the two share one memory.
ALERT_KIND: Final = costs.ALERT_KIND
RECOVERED_KIND: Final = "recovered:"
# A told-first key its own job could not deliver (costs.UNDELIVERED_KIND).
UNDELIVERED_KIND: Final = costs.UNDELIVERED_KIND

Severity = Literal["critical", "warning"]

# 06 §4.2, in its order: the one list of problem keys.
PROBLEM_KEYS: Final = (
    "db_down",
    "disk_low",
    "worker_silent",
    "userbot_unauthorized",
    "push_404",
    "erp_redirect",
    "backup_failed",
    "restore_drill_failed",
    "suppression_stale",
    "suppression_selftest_failed",
    "suppression_stale_hit",
    "userbot_silent",
    "flood_breaker",
    "collector_blocked",
    "collector_silent",
    "job_failing",
    costs.COST_CAP_KEY,
    costs.SPEND_HIGH_KEY,
    "no_signals_24h",
    "push_failing",
    "push_rejected",
    "push_unconfirmed",
    "feed_contract",
    "suppression_shrunk",
    "review_backlog",
    "anthropic_failing",
    "anthropic_silence",
    "batch_stuck",
    "pii_leak",
    "backup_unconfigured",
    "backup_stale",
    "ig_token_expired",
    "source_low_precision",
    "evidence_over_cap",
)
CRITICAL_KEYS: Final = frozenset(
    {
        "db_down",
        "disk_low",
        "worker_silent",
        "userbot_unauthorized",
        "push_404",
        "erp_redirect",
        "backup_failed",
        "restore_drill_failed",
    }
)
# «warning → critical after 6 h» (06 §4.2).
SUPPRESSION_STALE_CRITICAL_AFTER: Final = timedelta(hours=6)
# Said first by their own job at once; the sweep only repeats them after the
# repeat window and announces their recovery (06 §4.2). Not flood_breaker:
# the reader that trips it must not be the one to report it (06 §4.3 — the
# worker watches the userbot), so the sweep says it first.
# push_rejected (an ERP 4xx other than 404 on a push, 05 §B.1 «owner alert»)
# is told at once by crm_push the same way.
TOLD_FIRST_ELSEWHERE: Final = frozenset(
    {costs.COST_CAP_KEY, "push_404", "erp_redirect", "push_rejected"}
)
# 04 §8: the spend alerts are said once per Tashkent business day, not every
# ALERT_REPEAT_HOURS — cost_cap holds all day once it is reached, and the
# monthly cap all month.
DAILY_KEYS: Final = frozenset({costs.COST_CAP_KEY, costs.SPEND_HIGH_KEY})
# 06 §4.2: «once per source per week».
REPEAT_OVERRIDES: Final[Mapping[str, timedelta]] = {
    "source_low_precision": timedelta(days=7),
}
# Keys whose repeat window runs per subject, not per key: the ledger row's
# ``ref`` is ``<key>:<subject>``. job_failing's subjects are its items — a
# scheduler job (``job:<id>``), a collector cadence (``run:<key>[.<variant>]``)
# and the resolver backlog — so one failing item never hides another for
# ALERT_REPEAT_HOURS: above all the health sweep's own streak, which only
# the bot's watchdog can say (``sweep_failing``).
PER_SUBJECT_KEYS: Final = frozenset({"source_low_precision", "job_failing"})
# Raised and cleared by the module that sees them (raise_problem): the event
# is that module's, so is the moment it is over.
REPORTED_KEYS: Final = frozenset(
    {
        "suppression_selftest_failed",  # full pull (T-09)
        "suppression_stale_hit",  # outcomes walk raises, next full pull clears (T-10)
        "suppression_shrunk",  # full pull (T-09)
        "feed_contract",  # any feed call: detail.code (T-09/T-10)
        "push_unconfirmed",  # reconcile_unconfirmed after healthy walks (T-10)
        "push_rejected",  # crm_push on a 4xx; the next push the ERP takes clears it
        "anthropic_failing",  # the instant lane's failure streak (T-15)
        "pii_leak",  # validators (T-15)
        "ig_token_expired",  # Graph 190 (T-26)
        "source_low_precision",  # source_precision_job (T-36)
        "evidence_over_cap",  # retention R1 (T-31)
    }
)
PROBLEM_FLAG_PREFIX: Final = "problem:"
# Codes the push and feed breakers write into sources.erp_sync.breaker_reason
# (05 §B.1): the reason is the key, optionally «:<detail>».
ERP_BREAKER_KEYS: Final = ("push_404", "erp_redirect")
ERP_SOURCE: Final = "erp_sync"
# What the Fetcher's breaker writes (runner: «host_blocked:<host>»): a 403/429
# streak, i.e. collector_blocked.
BLOCKED_REASON_PREFIX: Final = "host_blocked"
# A push that ended on one of these after its in-attempt tries is a failing
# ERP, not a refusal (05 §B.1 5xx row).
PUSH_FAILING_ERRORS: Final = frozenset({"http_5xx", "conn", "timeout"})
PUSH_FAILING_STREAK: Final = 3
REVIEW_BACKLOG_THRESHOLD: Final = 30
REVIEW_BACKLOG_AGE: Final = timedelta(hours=72)
NO_SIGNALS_WINDOW: Final = timedelta(hours=24)
# MIYA's ANTHROPIC_SILENCE.
ANTHROPIC_SILENCE: Final = timedelta(hours=3)
# While anthropic_silence holds, costs.ask refuses Anthropic calls; one call
# per this interval is let through as a probe, or the problem would hold for
# ever (no call, no success, still silent).
ANTHROPIC_PROBE_INTERVAL: Final = timedelta(minutes=30)
ANTHROPIC_PROBE_FLAG: Final = "anthropic_probe"
# The hook costs.ask calls re-reads the facts at most this often per process.
ANTHROPIC_CACHE: Final = timedelta(seconds=60)
BATCH_STUCK_AFTER: Final = timedelta(hours=26)
# A collector is silent when two of its scheduled runs went by unrun; a run
# gets this long past its fire time before it counts as missed.
COLLECTOR_MISSED_RUNS: Final = 2
COLLECTOR_GRACE: Final = timedelta(minutes=30)
# job_failing (06 §4.2): a scheduler job whose last runs all raised, or a
# collector cadence whose last finished runs all failed — a run that fails
# every time still moves sources.last_run_at, so collector_silent never
# sees it. A blocked host is collector_blocked's, a deploy's cancel is not a
# fault.
JOB_FAILING_STREAK: Final = 3
FAILING_NOT_COUNTED: Final = frozenset({BLOCKED_REASON_PREFIX, "cancelled"})
# Far enough back to hold three runs of the monthly cadence (erp_sync.year).
FAILING_LOOKBACK: Final = timedelta(days=100)
# The resolver takes ≤ 200 observations every 2 minutes (04 §0, 6,000 an
# hour): one still 'new' after this long means it is not working them off.
RESOLVE_BACKLOG_AGE: Final = timedelta(hours=6)
# job_failing's subject for that backlog line (PER_SUBJECT_KEYS).
RESOLVE_BACKLOG_SUBJECT: Final = "resolve_backlog"
# Lines of a list inside one problem (collector_blocked, collector_silent …).
MAX_LINES: Final = 10

BUNDLE_HEADER: Final = "🩺 <b>Agent holati</b> — {n} ta muammo:"
_GB: Final = 1000**3
_MB: Final = 1000**2


# --- heartbeats -------------------------------------------------------------


def collector_component(key: str, variant: str | None = None) -> str:
    """``collector:<key>`` or ``collector:<key>.<variant>`` (03 §1.2). Refuses a
    name the heartbeat table would carry but no reader could ever match."""
    if not _KEY.fullmatch(key):
        raise ValueError(f"not a sources.key: {key!r}")
    if variant is None:
        return f"{COLLECTOR_PREFIX}{key}"
    if not _VARIANT.fullmatch(variant):
        raise ValueError(f"not a collector variant: {variant!r}")
    return f"{COLLECTOR_PREFIX}{key}.{variant}"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(settings.tz)


async def beat(
    session: AsyncSession,
    component: str,
    *,
    detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Record that ``component`` is alive now. One upsert; ``detail`` replaces
    the stored dict whole (an absent detail stores ``{}``). A beat never moves
    the clock back: of two beats committed out of order (two runs, two
    processes), the later ``now`` and its detail stay — the stored row would
    otherwise say a live component was last seen minutes ago."""
    stmt = pg_insert(Heartbeat).values(
        component=component, last_seen_at=_now(now), detail=dict(detail or {})
    )
    newer = stmt.excluded.last_seen_at >= Heartbeat.last_seen_at
    stmt = stmt.on_conflict_do_update(
        index_elements=[Heartbeat.component],
        set_={
            "last_seen_at": sa.func.greatest(
                Heartbeat.last_seen_at, stmt.excluded.last_seen_at
            ),
            "detail": sa.case((newer, stmt.excluded.detail), else_=Heartbeat.detail),
        },
    )
    await session.execute(stmt)


async def beats(session: AsyncSession) -> dict[str, Heartbeat]:
    """Every heartbeat row, keyed by component — fresh from the database,
    not from the identity map, so a beat in the same session is visible."""
    rows = await session.scalars(
        sa.select(Heartbeat).execution_options(populate_existing=True)
    )
    return {row.component: row for row in rows}


async def keep_beating(
    component: str,
    *,
    every: timedelta,
    detail: Callable[[], dict[str, Any]],
    stop: asyncio.Event,
) -> None:
    """Beat ``component`` now and every ``every`` until ``stop`` is set — a
    process's liveness loop. A failed beat is a log line, never a crash."""
    while True:
        try:
            async with session_scope() as session:
                await beat(session, component, detail=detail())
        except Exception as exc:
            log.warning(
                "heartbeat not written",
                extra={"component": component, "error": type(exc).__name__},
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=every.total_seconds())
            return
        except TimeoutError:
            continue


@dataclass(frozen=True, slots=True)
class Component:
    """One process or job as seen through its heartbeat.

    ``stale`` is «older than the threshold» — or never seen at all. The
    userbot's threshold is USERBOT_STALE_MINUTES, everything else's
    HEARTBEAT_STALE_MINUTES. ``disabled`` is a userbot switched off: it beats
    ``{"enabled": false}`` once and then goes quiet on purpose, which is not a
    fault and is never alerted.
    """

    name: str
    last_seen_at: datetime | None
    age: timedelta | None
    detail: Mapping[str, Any]
    stale: bool
    disabled: bool


def stale_after(name: str) -> timedelta:
    minutes = (
        settings.userbot_stale_minutes
        if name == "userbot"
        else settings.heartbeat_stale_minutes
    )
    return timedelta(minutes=minutes)


def judge(
    name: str,
    last_seen_at: datetime | None,
    detail: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> Component:
    """One heartbeat (or its absence) against its threshold."""
    if last_seen_at is None:
        return Component(name, None, None, {}, stale=True, disabled=False)
    age = max(now - last_seen_at, timedelta(0))
    facts = dict(detail or {})
    return Component(
        name,
        last_seen_at,
        age,
        facts,
        stale=age > stale_after(name),
        disabled=facts.get("enabled") is False,
    )


def component_of(name: str, row: Heartbeat | None, *, now: datetime) -> Component:
    if row is None:
        return judge(name, None, None, now=now)
    return judge(name, row.last_seen_at, row.detail, now=now)


def healthy(component: Component) -> bool:
    """The container healthcheck's verdict (tools/healthcheck.py): a fresh
    beat, or a userbot that is switched off on purpose."""
    return component.disabled or not component.stale


# --- formatting (owner text) --------------------------------------------------


def escape(text: object) -> str:
    """Neutralise HTML: a title or an error code cannot break parse_mode=HTML."""
    return html.escape(str(text), quote=False)


def age_label(delta: timedelta) -> str:
    """«25 daqiqa» / «3 soat» / «2 kun» — MIYA's ``formatting.age_label``."""
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 3600:
        return f"{seconds // 60} daqiqa"
    if seconds < 86400:
        return f"{seconds // 3600} soat"
    return f"{seconds // 86400} kun"


def clock_label(moment: datetime) -> str:
    """«24.10 15:30» in Tashkent time."""
    return moment.astimezone(settings.tz).strftime("%d.%m %H:%M")


def size_label(size: int | None) -> str:
    """«512 MB» / «3.4 GB» — the owner's units, not bytes."""
    if size is None:
        return "?"
    if size >= _GB:
        return f"{size / _GB:.1f} GB"
    return f"{size // _MB} MB"


def in_quiet_hours(now: datetime) -> bool:
    return within(now.astimezone(settings.tz).time(), *settings.quiet_hours_parsed)


# --- status ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """The newest ``*.dump.age`` on disk and what the backup job said."""

    configured: bool
    path: Path | None = None
    created_at: datetime | None = None
    size: int | None = None
    # Older than BACKUP_MAX_AGE_HOURS, or missing, while a recipient is set.
    stale: bool = False
    # The ``backup`` heartbeat: the last run's outcome.
    ok: bool | None = None
    error: str | None = None
    # Did *this* file reach Telegram (06 §1.4 «Telegramga yetdi»).
    sent: bool = False


@dataclass(frozen=True, slots=True)
class SourceState:
    """One ``sources`` row as the checks read it; ``last_skipped_at`` is the
    newest ``collector_runs(status='skipped')`` — the scheduler fired the
    source while it could not run, so silence is counted from there."""

    key: str
    title_uz: str
    enabled: bool
    cron: str | None
    paused_until: datetime | None
    breaker_until: datetime | None
    breaker_reason: str | None
    last_run_at: datetime | None
    last_skipped_at: datetime | None = None
    # The row's last write: the clock of a source that has neither run nor
    # been skipped since it was enabled (enabling it is a write).
    updated_at: datetime | None = None
    # /holat's per-source numbers (06 §1.4, 05 §B.2): the precision job's
    # 30-day precision and pushes, its auto_push, and the companies minted
    # from the evidence of the source's runs started since Tashkent midnight
    # (the resolver counts each on its run, companies.count_minted).
    precision_30d: Decimal | None = None
    pushes_30d: int = 0
    auto_push: bool = False
    new_companies_today: int = 0


@dataclass(frozen=True, slots=True)
class SuppressionHealth:
    """What the registered freshness probe answered (services/erp_feeds,
    T-09): the limits travel with it, so the text never restates them."""

    fresh: bool
    full_at: datetime | None
    delta_at: datetime | None
    full_max_h: int
    delta_max_h: int
    salt_ok: bool = True
    # When freshness was lost (the first limit that ran out), if known.
    stale_since: datetime | None = None
    # The «Boshqa» fallback (05 §B.3): no feed, the owner's XLSX import is
    # the gate — fresh while the last import is at most import_max_d days
    # old and newer than the last completed full pull (a full pull retires
    # the imported tails it carries; back on the fallback door they are gone).
    fallback: bool = False
    imported_at: datetime | None = None
    import_max_d: int = 0
    import_before_feed: bool = False
    # The feeds are configured but sources.erp_sync is switched off: no pull
    # ever runs (the seed ships it off), so the line says what to switch on.
    source_off: bool = False


@dataclass(frozen=True, slots=True)
class Reported:
    """A problem raised by the module that saw it (``raise_problem``)."""

    key: str
    since: datetime
    detail: Mapping[str, Any]
    until: datetime | None = None
    by: str = ""


@dataclass(frozen=True, slots=True)
class AnthropicFacts:
    """``usage_log``'s Anthropic rows: the newest success (a row with real
    tokens) and the newest failed attempt (a claim ``costs.ask`` marked
    ``failed:<error>`` — a call still in flight is neither), and the jobs
    waiting for the model."""

    last_ok_at: datetime | None
    last_failed_at: datetime | None
    pending_jobs: int


@dataclass(frozen=True, slots=True)
class Queues:
    """``/holat``'s queue line (06 §1.4) and today's counts."""

    review: int = 0
    review_oldest_at: datetime | None = None
    enriching: int = 0
    hold: int = 0
    approved: int = 0
    pushes_today: int = 0
    cards_today: int = 0
    last_push_at: datetime | None = None
    # observations(status='new'): what the resolver has still to take.
    observations_new: int = 0
    observations_new_oldest_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FailingRun:
    """A collector cadence (``source_key`` and its variant, ``""`` for the
    primary) whose last ``JOB_FAILING_STREAK`` finished runs all failed with
    an error that counts (``FAILING_NOT_COUNTED`` does not)."""

    source_key: str
    variant: str
    runs: int
    error: str
    last_at: datetime


@dataclass(slots=True)
class Status:
    now: datetime
    db_ok: bool
    disk_free_bytes: int | None
    disk_total_bytes: int | None
    disk_low: bool
    backup: BackupInfo
    components: dict[str, Component] = field(default_factory=dict)
    # The ``job:*`` rows, keyed by the bare scheduler job id.
    jobs: dict[str, Component] = field(default_factory=dict)
    # The ``collector:*`` rows, keyed by the name after the prefix.
    collectors: dict[str, Component] = field(default_factory=dict)
    restore_drill: Component | None = None
    sources: tuple[SourceState, ...] = ()
    paused: bool = False
    flood_until: datetime | None = None
    flood_seconds: int | None = None
    chats_on: int = 0
    monitoring_since: datetime | None = None
    interactions_24h: int = 0
    last_interaction_at: datetime | None = None
    spend: costs.SpendSnapshot = field(
        default_factory=lambda: costs.SpendSnapshot(Decimal(0), Decimal(0), {})
    )
    anthropic: AnthropicFacts = field(
        default_factory=lambda: AnthropicFacts(None, None, 0)
    )
    # (batch_id, submitted_at of its oldest job), past BATCH_STUCK_AFTER.
    stuck_batches: tuple[tuple[str, datetime], ...] = ()
    review_backlog: int = 0
    push_failing_streak: int = 0
    queues: Queues = field(default_factory=Queues)
    failing_runs: tuple[FailingRun, ...] = ()
    erp_breaker_until: datetime | None = None
    erp_breaker_reason: str | None = None
    # sources.erp_sync.config.suppression, for /holat (never judged here).
    erp_suppression: Mapping[str, Any] = field(default_factory=dict)
    # None = no probe registered: suppression_stale is unjudged.
    suppression: SuppressionHealth | None = None
    reported: dict[str, Reported] = field(default_factory=dict)
    evidence_bytes: int | None = None

    @classmethod
    def unreachable(cls, now: datetime, disk: tuple[int | None, int | None]) -> Status:
        """What can still be said when the database does not answer."""
        free, total = disk
        return cls(
            now=now,
            db_ok=False,
            disk_free_bytes=free,
            disk_total_bytes=total,
            disk_low=is_disk_low(free),
            backup=BackupInfo(configured=bool(settings.backup_age_recipient)),
        )


def is_disk_low(free: int | None) -> bool:
    return free is not None and free < settings.disk_min_free_gb * _GB


def disk_usage() -> tuple[int | None, int | None]:
    """Free and total bytes of the data volume, else of «/», else unknown."""
    for candidate in (Path(settings.backup_dir).parent, Path("/")):
        try:
            usage = shutil.disk_usage(candidate)
        except OSError:
            continue
        return usage.free, usage.total
    return None, None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=settings.tz)


# Is a ``runtime_flags`` switch on? (one home: ``costs.flag_set``, which the
# pause every paid call reads lives beside.)
flag_set = costs.flag_set


def flood_of(value: Any) -> tuple[datetime | None, int | None]:
    """``runtime_flags('flood_until')``: an ISO moment, or
    ``{"until": iso, "seconds": n}`` with the FloodWait that tripped it."""
    if isinstance(value, Mapping):
        seconds = value.get("seconds")
        until = _parse_iso(value.get("until"))
        return until, seconds if isinstance(seconds, int) else None
    return _parse_iso(value), None


# Probes registered by the module that owns the rule.
SuppressionProbe = Callable[[AsyncSession, datetime], Awaitable[SuppressionHealth | None]]


@dataclass(slots=True)
class _Probes:
    suppression: SuppressionProbe | None = None


_probes = _Probes()


def set_suppression_probe(probe: SuppressionProbe | None) -> None:
    """services/erp_feeds registers ``suppression_fresh`` here (T-09)."""
    _probes.suppression = probe


async def _backup_info(rows: Mapping[str, Heartbeat], now: datetime) -> BackupInfo:
    configured = bool(settings.backup_age_recipient)
    path = await backup.newest_backup()
    created_at = backup.backup_stamp(path) if path is not None else None
    size: int | None = None
    if path is not None:
        try:
            size = path.stat().st_size
        except OSError:
            size = None
    row = rows.get(BACKUP_COMPONENT)
    detail = dict(row.detail or {}) if row is not None else {}
    ok = detail.get("ok") if isinstance(detail.get("ok"), bool) else None
    about_this_file = path is not None and detail.get("path") == str(path)
    max_age = timedelta(hours=settings.backup_max_age_hours)
    return BackupInfo(
        configured=configured,
        path=path,
        created_at=created_at,
        size=size,
        stale=configured and (created_at is None or now - created_at > max_age),
        ok=ok,
        error=str(detail["error"])[:200] if ok is False and detail.get("error") else None,
        sent=about_this_file and detail.get("sent") is True,
    )


async def anthropic_facts(session: AsyncSession) -> AnthropicFacts:
    base = (
        sa.select(UsageLog.created_at)
        .where(UsageLog.provider == "anthropic")
        .order_by(UsageLog.created_at.desc())
        .limit(1)
    )
    last_ok = await session.scalar(base.where(UsageLog.input_tokens.is_not(None)))
    # Only a claim whose call raised is a failed attempt. A claim without a
    # model is a call in flight (or one cancelled, or one whose answer could
    # not be settled), a batch submit's provisional rows wait for their
    # results, and a refusal sent nothing: none of them says the API is silent.
    last_failed = await session.scalar(
        base.where(UsageLog.model.startswith(costs.FAILED_PREFIX, autoescape=True))
    )
    # The work that waits for an answer now: the Telegram messages classify
    # has still to ask about (its queue — an instant-lane job is never left
    # pending once its transaction commits, so the queue is where the
    # waiting work is) and any instant job left pending. The batch lane's
    # pending jobs wait for 23:30 by design; batch_stuck is their alarm.
    from clientscrapper.services import classify  # it imports this module

    pending = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ModelJob)
        .where(ModelJob.status == "pending", ModelJob.lane == "instant")
    )
    waiting = await classify.waiting(session)
    return AnthropicFacts(last_ok, last_failed, int(pending or 0) + waiting)


async def stuck_batches(
    session: AsyncSession, now: datetime
) -> tuple[tuple[str, datetime], ...]:
    """06 §4.2 batch_stuck: each Message Batch whose jobs still wait
    ``submitted`` more than ``BATCH_STUCK_AFTER`` after it was sent — (batch
    id, submitted_at of its oldest job), oldest first. The one statement of
    «stuck»: the sweep says it, the night's submit gives up on it
    (``model_jobs``)."""
    rows = await session.execute(
        sa.select(ModelJob.batch_id, sa.func.min(ModelJob.submitted_at))
        .where(
            ModelJob.lane == "batch",
            ModelJob.status == "submitted",
            ModelJob.batch_id.is_not(None),
        )
        .group_by(ModelJob.batch_id)
        .having(sa.func.min(ModelJob.submitted_at) < now - BATCH_STUCK_AFTER)
        .order_by(sa.func.min(ModelJob.submitted_at))
    )
    return tuple((str(batch_id), at) for batch_id, at in rows.all())


async def reported_problems(session: AsyncSession, now: datetime) -> dict[str, Reported]:
    """The raised problems in force (an ``until`` in the past has expired)."""
    rows = await session.scalars(
        sa.select(RuntimeFlag)
        .where(RuntimeFlag.key.startswith(PROBLEM_FLAG_PREFIX))
        .execution_options(populate_existing=True)
    )
    found: dict[str, Reported] = {}
    for row in rows:
        key = row.key[len(PROBLEM_FLAG_PREFIX) :]
        value = row.value if isinstance(row.value, Mapping) else {}
        until = _parse_iso(value.get("until"))
        if key not in REPORTED_KEYS or (until is not None and until <= now):
            continue
        detail = value.get("detail")
        found[key] = Reported(
            key=key,
            since=_parse_iso(value.get("since")) or row.updated_at,
            detail=dict(detail) if isinstance(detail, Mapping) else {},
            until=until,
            by=row.updated_by,
        )
    return found


async def _sources(session: AsyncSession, now: datetime) -> tuple[SourceState, ...]:
    # A smoke test's row is ``skipped`` too, but the scheduler never fired the
    # source: it must not restart a silent collector's clock (services/smoke).
    found = await session.execute(
        sa.select(CollectorRun.source_key, sa.func.max(CollectorRun.started_at))
        .where(CollectorRun.status == "skipped", smoke.not_smoke())
        .group_by(CollectorRun.source_key)
    )
    skipped: dict[str, datetime] = {str(key): at for key, at in found.all()}
    today = await session.execute(
        sa.select(CollectorRun.source_key, sa.func.sum(CollectorRun.new_companies))
        .where(CollectorRun.started_at >= costs.day_start(now))
        .group_by(CollectorRun.source_key)
    )
    new_today: dict[str, int] = {str(key): int(n or 0) for key, n in today.all()}
    rows = await session.scalars(
        sa.select(Source).order_by(Source.key).execution_options(populate_existing=True)
    )
    return tuple(
        SourceState(
            key=row.key,
            title_uz=row.title_uz,
            enabled=row.enabled,
            cron=row.cron,
            paused_until=row.paused_until,
            breaker_until=row.breaker_until,
            breaker_reason=row.breaker_reason,
            last_run_at=row.last_run_at,
            last_skipped_at=skipped.get(row.key),
            updated_at=row.updated_at,
            precision_30d=row.precision_30d,
            pushes_30d=row.pushes_30d,
            auto_push=row.auto_push,
            new_companies_today=new_today.get(row.key, 0),
        )
        for row in rows
    )


async def _push_failing_streak(session: AsyncSession) -> int:
    """How many of the newest candidates' last finished pushes failed on the
    ERP (5xx / connection / connect timeout), counted from the newest."""
    latest = (
        sa.select(PushLog.candidate_id, PushLog.status, PushLog.error, PushLog.sent_at)
        .where(PushLog.status != "sending")
        .distinct(PushLog.candidate_id)
        .order_by(PushLog.candidate_id, PushLog.sent_at.desc(), PushLog.attempt.desc())
        .subquery()
    )
    rows = (
        await session.execute(
            sa.select(latest.c.status, latest.c.error)
            .order_by(latest.c.sent_at.desc())
            .limit(20)
        )
    ).all()
    streak = 0
    for status, error in rows:
        if status != "failed" or error not in PUSH_FAILING_ERRORS:
            break
        streak += 1
    return streak


async def _queues(session: AsyncSession, now: datetime) -> Queues:
    by_state = {
        state: (int(n), oldest)
        for state, n, oldest in (
            await session.execute(
                sa.select(
                    LeadCandidate.state,
                    sa.func.count(),
                    sa.func.min(LeadCandidate.updated_at),
                )
                .where(
                    LeadCandidate.state.in_(["review", "enriching", "hold", "approved"])
                )
                .group_by(LeadCandidate.state)
            )
        ).all()
    }
    day = costs.day_start(now)
    delivered = PushLog.status.in_(["sent", "unknown"])
    pushes = await session.scalar(
        sa.select(sa.func.count())
        .select_from(PushLog)
        .where(delivered, PushLog.sent_at >= day)
    )
    cards = await session.scalar(
        sa.select(sa.func.count())
        .select_from(LeadReview)
        .where(LeadReview.asked_at >= day)
    )
    last_push = await session.scalar(
        sa.select(sa.func.max(PushLog.sent_at)).where(PushLog.status == "sent")
    )
    # Read through ix_observations_queue (partial on the queue's statuses).
    waiting, oldest_new = (
        await session.execute(
            sa.select(sa.func.count(), sa.func.min(Observation.created_at)).where(
                Observation.status == "new"
            )
        )
    ).one()
    review = by_state.get("review", (0, None))
    return Queues(
        review=review[0],
        review_oldest_at=review[1],
        enriching=by_state.get("enriching", (0, None))[0],
        hold=by_state.get("hold", (0, None))[0],
        approved=by_state.get("approved", (0, None))[0],
        pushes_today=int(pushes or 0),
        cards_today=int(cards or 0),
        last_push_at=last_push,
        observations_new=int(waiting or 0),
        observations_new_oldest_at=oldest_new,
    )


async def _failing_runs(session: AsyncSession, now: datetime) -> tuple[FailingRun, ...]:
    """Each collector cadence whose last ``JOB_FAILING_STREAK`` finished runs
    (skipped and running ones are not runs here) all failed with an error that
    counts — one window over ``ix_collector_runs_source_started``, bounded by
    ``FAILING_LOOKBACK``."""
    variant = sa.func.coalesce(CollectorRun.meta["variant"].astext, "")
    ranked = (
        sa.select(
            CollectorRun.source_key.label("key"),
            variant.label("variant"),
            CollectorRun.status.label("status"),
            CollectorRun.error.label("error"),
            CollectorRun.started_at.label("at"),
            sa.func.row_number()
            .over(
                partition_by=(CollectorRun.source_key, variant),
                order_by=(CollectorRun.started_at.desc(), CollectorRun.id.desc()),
            )
            .label("rn"),
        )
        .where(
            CollectorRun.status.in_(["ok", "partial", "failed"]),
            CollectorRun.started_at >= now - FAILING_LOOKBACK,
        )
        .subquery()
    )
    rows = (
        await session.execute(
            sa.select(
                ranked.c.key,
                ranked.c.variant,
                ranked.c.status,
                ranked.c.error,
                ranked.c.at,
            )
            .where(ranked.c.rn <= JOB_FAILING_STREAK)
            .order_by(ranked.c.key, ranked.c.variant, ranked.c.rn)
        )
    ).all()
    runs: dict[tuple[str, str], list[tuple[str, str | None, datetime]]] = {}
    for key, var, status, error, at in rows:
        runs.setdefault((str(key), str(var)), []).append((status, error, at))
    found: list[FailingRun] = []
    for (key, var), latest in runs.items():
        if len(latest) < JOB_FAILING_STREAK:
            continue
        if all(
            status == "failed" and (error or "") not in FAILING_NOT_COUNTED
            for status, error, _ in latest
        ):
            _, error, at = latest[0]
            found.append(FailingRun(key, var, len(latest), str(error or "?"), at))
    return tuple(found)


async def gather(
    session: AsyncSession,
    *,
    now: datetime,
    disk: tuple[int | None, int | None] | None = None,
    evidence_size: bool = False,
) -> Status:
    """Everything ``/holat`` shows and the sweep judges, in one pass of
    bounded queries. ``disk`` is (free, total) bytes (default: measured).
    ``evidence_size`` adds the stored evidence bytes (``/holat`` only — a sum
    over the evidence table). If the database itself does not answer,
    ``db_ok`` is False and only the disk and the backup file are read."""
    measured = disk if disk is not None else disk_usage()
    try:
        await session.execute(sa.text("SELECT 1"))
    except Exception as exc:
        log.warning(
            "health: the database did not answer", extra={"error": type(exc).__name__}
        )
        down = Status.unreachable(now, measured)
        down.backup = await _backup_info({}, now)
        return down

    rows = await beats(session)
    flags = {
        row.key: row
        for row in await session.scalars(
            sa.select(RuntimeFlag).execution_options(populate_existing=True)
        )
    }
    paused_row = flags.get("paused")
    flood_row = flags.get("flood_until")
    flood_until, flood_seconds = (
        flood_of(flood_row.value) if flood_row is not None else (None, None)
    )
    chats_on, monitoring_since = (
        await session.execute(
            sa.select(sa.func.count(), sa.func.min(ChatMonitor.monitoring_since)).where(
                ChatMonitor.monitor_enabled.is_(True), ChatMonitor.status == "joined"
            )
        )
    ).one()
    signals, last_signal = (
        await session.execute(
            sa.select(
                sa.func.count().filter(
                    Interaction.occurred_at >= now - NO_SIGNALS_WINDOW
                ),
                sa.func.max(Interaction.occurred_at),
            )
        )
    ).one()
    stuck = await stuck_batches(session, now)
    # How long a card has waited: since the owner was first shown it (an
    # unanswered owner card's asked_at), else since the row last changed —
    # updated_at alone moves on every rescore and enrichment, and would
    # undercount old cards (06 §4.2 review_backlog).
    shown = (
        sa.select(
            LeadReview.candidate_id, sa.func.min(LeadReview.asked_at).label("asked_at")
        )
        .where(
            LeadReview.kind == "owner",
            LeadReview.answered_at.is_(None),
            LeadReview.asked_at.is_not(None),
        )
        .group_by(LeadReview.candidate_id)
        .subquery()
    )
    backlog = await session.scalar(
        sa.select(sa.func.count())
        .select_from(LeadCandidate)
        .outerjoin(shown, shown.c.candidate_id == LeadCandidate.id)
        .where(
            LeadCandidate.state == "review",
            sa.func.coalesce(shown.c.asked_at, LeadCandidate.updated_at)
            < now - REVIEW_BACKLOG_AGE,
        )
    )
    sources = await _sources(session, now)
    erp = next((s for s in sources if s.key == ERP_SOURCE), None)
    erp_config = await session.scalar(
        sa.select(Source.config["suppression"]).where(Source.key == ERP_SOURCE)
    )
    suppression: SuppressionHealth | None = None
    probe = _probes.suppression
    if probe is not None:
        try:
            suppression = await probe(session, now)
        except Exception as exc:
            log.warning(
                "health: the suppression probe failed",
                extra={"error": type(exc).__name__},
            )
    evidence_bytes: int | None = None
    if evidence_size:
        evidence_bytes = int(
            await session.scalar(
                sa.select(sa.func.coalesce(sa.func.sum(Evidence.bytes), 0)).where(
                    Evidence.raw_path.is_not(None)
                )
            )
            or 0
        )
    free, total = measured
    return Status(
        now=now,
        db_ok=True,
        disk_free_bytes=free,
        disk_total_bytes=total,
        disk_low=is_disk_low(free),
        backup=await _backup_info(rows, now),
        components={
            name: component_of(name, rows.get(name), now=now) for name in COMPONENTS
        },
        jobs={
            key[len(JOB_PREFIX) :]: component_of(key[len(JOB_PREFIX) :], row, now=now)
            for key, row in rows.items()
            if key.startswith(JOB_PREFIX)
        },
        collectors={
            key[len(COLLECTOR_PREFIX) :]: component_of(key, row, now=now)
            for key, row in rows.items()
            if key.startswith(COLLECTOR_PREFIX)
        },
        restore_drill=(
            component_of(RESTORE_DRILL_COMPONENT, rows[RESTORE_DRILL_COMPONENT], now=now)
            if RESTORE_DRILL_COMPONENT in rows
            else None
        ),
        sources=sources,
        paused=paused_row is not None and flag_set(paused_row.value),
        flood_until=flood_until,
        flood_seconds=flood_seconds,
        chats_on=int(chats_on or 0),
        monitoring_since=monitoring_since,
        interactions_24h=int(signals or 0),
        last_interaction_at=last_signal,
        spend=await costs.snapshot(session, now),
        anthropic=await anthropic_facts(session),
        stuck_batches=stuck,
        review_backlog=int(backlog or 0),
        push_failing_streak=await _push_failing_streak(session),
        queues=await _queues(session, now),
        failing_runs=await _failing_runs(session, now),
        erp_breaker_until=erp.breaker_until if erp is not None else None,
        erp_breaker_reason=erp.breaker_reason if erp is not None else None,
        erp_suppression=dict(erp_config) if isinstance(erp_config, Mapping) else {},
        suppression=suppression,
        reported=await reported_problems(session, now),
        evidence_bytes=evidence_bytes,
    )


# --- judgements (pure) -------------------------------------------------------


def anthropic_silent(facts: AnthropicFacts, now: datetime) -> bool:
    """06 §4.2 anthropic_silence: no successful call for ≥ 3 h while jobs
    wait. Counted only once a call *failed* after the last success: a day
    whose calls were all refused at a cost cap is not a silent API, a call
    still waiting for its answer is not one, and a fresh install that never
    called is not one either."""
    if facts.pending_jobs <= 0 or facts.last_failed_at is None:
        return False
    if facts.last_ok_at is not None and facts.last_failed_at <= facts.last_ok_at:
        return False
    since = facts.last_ok_at
    return since is None or now - since >= ANTHROPIC_SILENCE


def erp_breaker(status: Status, key: str) -> str | None:
    """The detail after ``<key>`` when the ERP breaker is in force for that
    key (``push_404`` / ``erp_redirect``, optionally «:<detail>»), else None."""
    reason = status.erp_breaker_reason or ""
    until = status.erp_breaker_until
    if until is None or until <= status.now:
        return None
    if reason == key:
        return ""
    if reason.startswith(key + ":"):
        return reason[len(key) + 1 :]
    return None


def erp_breaker_text(key: str, detail: str = "") -> str:
    """The owner's line for an ERP breaker in force (06 §4.2): ``key`` is
    ``push_404`` or ``erp_redirect``, ``detail`` what follows «<key>:» in
    ``breaker_reason`` (the redirect's HTTP status)."""
    if key == "push_404":
        return (
            "⛔ ERP kaliti yoki manba notoʻgʻri: 404. Kelgan arizalar → Reklama "
            "havolalari → Agent → kalit; <code>.env</code> CRM_AGENT_KEY; "
            "<code>make reload</code>; /yoq crm"
        )
    if key == "erp_redirect":
        return (
            f"⛔ ERP manzili yoʻnaltiryapti (HTTP {escape(detail or '3xx')}): "
            "<code>.env</code> dagi CRM_BASE_URL <code>https://</code> bilan "
            "boshlanishi kerak; <code>make reload</code>; /yoq crm"
        )
    raise ValueError(f"{key!r} is not an ERP breaker key")


def push_rejected_text(status: int | None) -> str:
    """push_rejected's line (05 §B.1): a 4xx other than 404 on a push — the
    ERP door answers the agent only 200 / 404 / 503, so it is a proxy or a
    wrong ``CRM_BASE_URL``. crm_push says it first, the sweep repeats it."""
    return (
        f"⚠️ ERP pushni rad etdi (HTTP {status or '4xx'}): <code>.env</code> dagi "
        "CRM_BASE_URL va ERP oldidagi proksini tekshiring; lid koʻrib chiqishga qaytdi"
    )


def blocked_sources(status: Status) -> list[SourceState]:
    return [
        s
        for s in status.sources
        if s.breaker_until is not None
        and s.breaker_until > status.now
        and (s.breaker_reason or "").startswith(BLOCKED_REASON_PREFIX)
    ]


def collectors_judged(status: Status) -> bool:
    """With crawling switched off or ``/toxtat`` in force no collector runs on
    purpose: silence is neither a problem nor a recovery."""
    return settings.collectors_enabled and not status.paused


def _judged_sources(status: Status) -> list[SourceState]:
    """The sources collector_silent judges: enabled, with a cron, not paused,
    not blocked (collector_blocked says that one)."""
    if not collectors_judged(status):
        return []
    return [
        source
        for source in status.sources
        if source.enabled
        and source.cron
        and not (source.paused_until is not None and source.paused_until > status.now)
        and not (source.breaker_until is not None and source.breaker_until > status.now)
    ]


def unreadable_crons(status: Status) -> list[SourceState]:
    """Judged sources whose ``sources.cron`` the translator refuses (an owner
    edit): the scheduler runs the seeded cron instead and says so in its
    log; collector_silent names them, since their silence cannot be judged."""
    found: list[SourceState] = []
    for source in _judged_sources(status):
        try:
            crontab.trigger(source.cron or "", settings.tz)
        except crontab.CrontabError:
            found.append(source)
    return found


def silent_sources(status: Status) -> list[tuple[SourceState, timedelta]]:
    """06 §4.2 collector_silent: an enabled source with a cron, not paused,
    whose cron fired at least twice (plus a grace) since it last ran — or
    since the scheduler last fired it while it could not run (a ``skipped``
    run: disabled, paused or globally paused) — or, for a source that has
    done neither, since its row was last written (it was enabled then, and
    a built job that fails before it claims a run leaves no other trace).
    Returns (source, silent for)."""
    found: list[tuple[SourceState, timedelta]] = []
    for source in _judged_sources(status):
        marks = [m for m in (source.last_run_at, source.last_skipped_at) if m is not None]
        if not marks and source.updated_at is not None:
            marks = [source.updated_at]
        if not marks:
            continue
        reference = max(marks)
        try:
            missed = list(
                crontab.fires(
                    source.cron or "",
                    after=reference,
                    until=status.now - COLLECTOR_GRACE,
                    tz=settings.tz,
                    limit=COLLECTOR_MISSED_RUNS,
                )
            )
        except crontab.CrontabError:
            continue  # unreadable_crons names it
        if len(missed) >= COLLECTOR_MISSED_RUNS:
            found.append((source, status.now - reference))
    return found


UserbotAuth = Literal["authorized", "unauthorized", "unknown"]


def userbot_auth(status: Status) -> UserbotAuth | None:
    """What the reader's last beat says about its account (T-13, 06 §2):
    ``authorized: true`` / ``false``, or ``null`` — «unknown»: Telegram could
    not be asked (a timeout, a 500, a long flood wait) and the reader asks
    again each minute. Only «unauthorized» raises ``userbot_unauthorized``;
    «unknown» neither raises it nor counts as its recovery (:func:`unjudged`),
    and ``/holat`` shows it as such. None: the reader is off, unseen, or its
    beat names no state."""
    userbot = status.components.get("userbot")
    if userbot is None or userbot.disabled or userbot.last_seen_at is None:
        return None
    if "authorized" not in userbot.detail:
        return None
    authorized = userbot.detail["authorized"]
    if authorized is None:
        return "unknown"
    if authorized is True:
        return "authorized"
    if authorized is False:
        return "unauthorized"
    return None


def userbot_unauthorized(status: Status) -> str | None:
    """The userbot's error code when it says it is not authorized, else None."""
    if userbot_auth(status) != "unauthorized":
        return None
    userbot = status.components["userbot"]
    return str(userbot.detail.get("error") or "?")


def userbot_on(status: Status) -> bool:
    userbot = status.components.get("userbot")
    return userbot is not None and not userbot.disabled and settings.userbot_enabled


def unjudged(status: Status) -> frozenset[str]:
    """Keys this status cannot clear: a state that was not read, or a feature
    switched off, is not a recovery — such a key is neither said nor
    announced as recovered until a status judges it again."""
    keys: set[str] = set()
    if not status.db_ok:
        keys.update(k for k in PROBLEM_KEYS if k not in {"db_down", "disk_low"})
    if status.suppression is None:
        keys.add("suppression_stale")
    if not userbot_on(status):
        keys.update({"userbot_silent", "userbot_unauthorized", "no_signals_24h"})
    elif userbot_auth(status) == "unknown":
        # Telegram was not asked: the account is neither lost nor back.
        keys.add("userbot_unauthorized")
    if not collectors_judged(status):
        keys.update({"collector_silent", "job_failing"})
    if not worker_fresh(status):
        # The job beats and the runs are the worker's: worker_silent says it.
        keys.add("job_failing")
    return frozenset(keys)


def worker_fresh(status: Status) -> bool:
    worker = status.components.get("worker")
    return worker is not None and not worker.stale


def _streak(component: Component) -> int:
    failures = component.detail.get("failures")
    if component.detail.get("ok") is not False or isinstance(failures, bool):
        return 0
    return failures if isinstance(failures, int) else 0


# The sweep's own scheduler id: when it is the job that keeps raising, it
# cannot say so itself (06 §4.3), so the bot's watchdog reads its beat.
HEALTH_JOB: Final = "health"
# The error a ``job:health`` beat carries for a run the scheduler skipped
# because the previous one never ended (APScheduler's max_instances=1): a
# hung sweep raises nothing, so the worker counts each skip as a failed run
# (``worker.on_job_skipped``) and the watchdog sees the streak.
STILL_RUNNING: Final = "still_running"


def job_subject(job_id: str) -> str:
    """job_failing's subject for one scheduler job (``PER_SUBJECT_KEYS``)."""
    return JOB_PREFIX + job_id


def failing_job_line(job: Component, runs: int) -> str:
    """job_failing's line for one scheduler job (the sweep and the watchdog
    say it in these words)."""
    if job.detail.get("error") == STILL_RUNNING:
        return (
            f"⚠️ <code>{escape(job.name)}</code> ishi tugamayapti: oxirgi {runs} ta "
            "navbati oʻtkazib yuborildi — <code>make worker</code> loglari"
        )
    return (
        f"⚠️ <code>{escape(job.name)}</code> ishi oxirgi {runs} marta xato bilan "
        f"tugadi ({escape(job.detail.get('error') or '?')}) — "
        "<code>make worker</code> loglari"
    )


def sweep_failing(status: Status) -> Problem | None:
    """job_failing for the health sweep alone: its ``job:health`` beat says it
    raised — or was skipped while a hung run held its place — ``JOB_FAILING_
    STREAK`` times or more in a row (the worker is up — a dead worker is
    worker_silent). The bot's watchdog says it, since a sweep that crashes
    never reaches its own judgement; its subject is the sweep's own, so an
    unrelated failing job said earlier never holds it back."""
    job = status.jobs.get(HEALTH_JOB)
    if job is None or not worker_fresh(status):
        return None
    runs = _streak(job)
    if runs < JOB_FAILING_STREAK:
        return None
    line = failing_job_line(job, runs)
    return Problem("job_failing", "warning", line, ((job_subject(HEALTH_JOB), line),))


def status_from_beats(rows: Mapping[str, Heartbeat], now: datetime) -> Status:
    """What the heartbeats alone say — the processes and the jobs — for a
    reader whose ``gather`` failed with the database up (the watchdog): its
    worker_silent and :func:`sweep_failing` hold on it; nothing else is
    judged from it."""
    return Status(
        now=now,
        db_ok=True,
        disk_free_bytes=None,
        disk_total_bytes=None,
        disk_low=False,
        backup=BackupInfo(configured=True),
        components={
            name: component_of(name, rows.get(name), now=now) for name in COMPONENTS
        },
        jobs={
            key[len(JOB_PREFIX) :]: component_of(key[len(JOB_PREFIX) :], row, now=now)
            for key, row in rows.items()
            if key.startswith(JOB_PREFIX)
        },
    )


def failing_jobs(status: Status) -> list[tuple[Component, int]]:
    """06 §4.2 job_failing, the scheduler half: a ``job:<id>`` whose last
    ``JOB_FAILING_STREAK`` runs or more raised (``failures`` in its beat)."""
    if not worker_fresh(status):
        return []
    return [
        (job, n)
        for _, job in sorted(status.jobs.items())
        if (n := _streak(job)) >= JOB_FAILING_STREAK
    ]


def failing_collectors(status: Status) -> list[tuple[SourceState, FailingRun]]:
    """job_failing, the collector half: a cadence of a judged source
    (collector_silent's set: enabled, scheduled, not paused, not blocked)
    whose last runs all failed."""
    if not worker_fresh(status):
        return []
    judged = {s.key: s for s in _judged_sources(status)}
    return [
        (judged[run.source_key], run)
        for run in status.failing_runs
        if run.source_key in judged
    ]


def resolve_backlog(status: Status) -> timedelta | None:
    """How long the oldest observation has waited for the resolver, when
    that is past ``RESOLVE_BACKLOG_AGE`` (and the worker is up — a dead
    worker is worker_silent), else None."""
    oldest = status.queues.observations_new_oldest_at
    if oldest is None or not worker_fresh(status):
        return None
    age = status.now - oldest
    return age if age > RESOLVE_BACKLOG_AGE else None


@dataclass(frozen=True, slots=True)
class Problem:
    key: str
    severity: Severity
    # Uzbek, HTML-escaped, and it names the remedy.
    text: str
    # A key said per subject (``PER_SUBJECT_KEYS``): (subject, its line), and
    # ``text`` is their lines. The alert memory keeps one row per subject, so
    # a new subject is said at once while the others keep their window.
    subjects: tuple[tuple[str, str], ...] = ()


def _since(component: Component) -> str:
    if component.age is None:
        return "hali bir marta ham xabar bermagan"
    return f"{age_label(component.age)} jim"


def _detail(reported: Reported, name: str, default: object = "?") -> str:
    value = reported.detail.get(name, default)
    return escape(value if value is not None else default)


def _lines(lines: list[str]) -> str:
    if len(lines) > MAX_LINES:
        lines = [*lines[:MAX_LINES], f"… yana {len(lines) - MAX_LINES} ta"]
    return "\n".join(lines)


def _suppression_text(status: Status, health: SuppressionHealth) -> str:
    def age(at: datetime | None) -> str:
        return "hech qachon" if at is None else f"{age_label(status.now - at)} oldin"

    head = "⛔" if _suppression_critical(status, health) else "⚠️"
    if health.fallback:
        remedy = (
            "ERP → Mijozlar → XLSX, <code>make import-clients</code> — push toʻxtatildi"
        )
        if health.imported_at is None:
            return f"{head} Mijozlar roʻyxati hali import qilinmagan: {remedy}"
        if health.import_before_feed:
            return (
                f"{head} Mijozlar roʻyxati {age(health.imported_at)} — ERP "
                "roʻyxatidan oldin import qilingan, «Boshqa» eshigida qayta import "
                f"kerak: {remedy}"
            )
        return (
            f"{head} Mijozlar roʻyxati {age(health.imported_at)} import qilingan "
            f"(chegara {health.import_max_d} kun): {remedy}"
        )
    salt = "" if health.salt_ok else " · kalit almashgan (salt_id)"
    off = (
        " · «erp_sync» manbasi oʻchiq — /manbalar orqali yoqing"
        if health.source_off
        else ""
    )
    return (
        f"{head} ERP bostirish roʻyxati yangilanmadi: toʻliq {age(health.full_at)} "
        f"(chegara {health.full_max_h} soat) · farq {age(health.delta_at)} "
        f"(chegara {health.delta_max_h} soat){salt}{off} — push toʻxtatildi"
    )


def _suppression_critical(status: Status, health: SuppressionHealth) -> bool:
    since = health.stale_since
    return since is not None and status.now - since >= SUPPRESSION_STALE_CRITICAL_AFTER


def problems(status: Status) -> list[Problem]:
    """What is wrong, in ``PROBLEM_KEYS`` order. Pure — Status in, list out.

    When the database is down only that and the disk are judged: every other
    judgement rests on rows that could not be read."""
    found: dict[str, Problem] = {}

    def add(key: str, text: str, severity: Severity | None = None) -> None:
        sev: Severity = severity or ("critical" if key in CRITICAL_KEYS else "warning")
        found[key] = Problem(key, sev, text)

    if not status.db_ok:
        add(
            "db_down",
            "⛔ Baza javob bermayapti — <code>make ps</code>, <code>make logs</code>",
        )
    if status.disk_low:
        free = (status.disk_free_bytes or 0) / _GB
        add(
            "disk_low",
            f"⛔ Diskda {free:.1f} GB qoldi — <code>make backup</code> tekshir, "
            "<code>EVIDENCE_MAX_GB</code> kamaytir",
        )
    if not status.db_ok:
        return [found[k] for k in PROBLEM_KEYS if k in found]

    worker = status.components.get("worker")
    if worker is not None and worker.stale:
        add(
            "worker_silent",
            f"⛔ Rejalashtiruvchi {escape(_since(worker))} — <code>make worker</code>",
        )
    code = userbot_unauthorized(status)
    if code is not None:
        add(
            "userbot_unauthorized",
            "⛔ Telegram oʻquvchi akkaunti chiqib ketgan/cheklangan "
            f"({escape(code)}). @SpamBot tekshiring; <code>make userbot-login</code>",
        )
    for key in ERP_BREAKER_KEYS:
        detail = erp_breaker(status, key)
        if detail is not None:
            add(key, erp_breaker_text(key, detail))
    info = status.backup
    if info.configured and info.ok is False:
        add(
            "backup_failed",
            "⛔ Tungi zaxira yozilmadi: "
            f"<code>{escape(info.error or 'nomaʼlum xato')}</code>",
        )
    drill = status.restore_drill
    if drill is not None and drill.detail.get("ok") is False:
        error = str(drill.detail.get("error") or "?")[:200]
        add(
            "restore_drill_failed",
            "⛔ Zaxirani tiklash sinovi oʻtmadi — zaxiralar ishonchsiz "
            f"(<code>{escape(error)}</code>)",
        )
    sup = status.suppression
    if sup is not None and not sup.fresh:
        add(
            "suppression_stale",
            _suppression_text(status, sup),
            "critical" if _suppression_critical(status, sup) else "warning",
        )
    rep = status.reported
    if "suppression_selftest_failed" in rep:
        add(
            "suppression_selftest_failed",
            "⚠️ ERP roʻyxati oʻz-oʻzini tekshirishdan oʻtmadi — maʼlum mijoz raqami "
            "roʻyxatda yoʻq; kalit, salt va ERP feedini tekshiring",
        )
    if "suppression_stale_hit" in rep:
        add(
            "suppression_stale_hit",
            f"⚠️ ERP {_detail(rep['suppression_stale_hit'], 'n')} ta pushni "
            "«bostirilgan» deb qaytardi — bu raqamlar endi bizda ham yopiq; tungi "
            "toʻliq yuklashdan keyin bu ogohlantirish oʻchadi",
        )
    userbot = status.components.get("userbot")
    if userbot_on(status) and userbot is not None and userbot.stale and code is None:
        add(
            "userbot_silent",
            f"⚠️ Telegram oʻquvchi {escape(_since(userbot))} — <code>make userbot</code>",
        )
    if status.flood_until is not None and status.flood_until > status.now:
        seconds = status.flood_seconds if status.flood_seconds is not None else "?"
        add(
            "flood_breaker",
            f"⚠️ Telegram FloodWait ({seconds} s) — oʻquvchi "
            f"{clock_label(status.flood_until)} gacha qoʻshilmaydi/oʻqimaydi "
            "(jonli xabarlar davom etadi)",
        )
    blocked = blocked_sources(status)
    if blocked:
        add(
            "collector_blocked",
            _lines(
                [
                    f"⚠️ {escape(s.title_uz)}: sayt bloklayapti (403/429) — "
                    f"{clock_label(s.breaker_until)} gacha toʻxtadi"
                    for s in blocked
                    if s.breaker_until is not None
                ]
            ),
        )
    silent = [
        f"⚠️ {escape(s.title_uz)} {age_label(age)} ishlamadi — /manbalar"
        for s, age in silent_sources(status)
    ]
    silent += [
        f"⚠️ {escape(s.title_uz)}: jadval («{escape(s.cron)}») notoʻgʻri — oʻrniga "
        "dastlabki jadval ishlatilmoqda; /manbalar"
        for s in unreadable_crons(status)
    ]
    if silent:
        add("collector_silent", _lines(silent))
    # One subject per item (PER_SUBJECT_KEYS): each is said on its own clock.
    failing: list[tuple[str, str]] = [
        (job_subject(job.name), failing_job_line(job, n))
        for job, n in failing_jobs(status)
    ]
    failing += [
        (
            f"run:{run.source_key}{'.' + run.variant if run.variant else ''}",
            f"⚠️ {escape(source.title_uz)}"
            f"{' · ' + escape(run.variant) if run.variant else ''}: oxirgi {run.runs} ta "
            f"ishga tushirish xato bilan tugadi ({escape(run.error)}) — "
            "<code>make worker</code> loglari; /manbalar",
        )
        for source, run in failing_collectors(status)
    ]
    waited = resolve_backlog(status)
    if waited is not None:
        failing.append(
            (
                RESOLVE_BACKLOG_SUBJECT,
                f"⚠️ {status.queues.observations_new} ta topilma navbatda kutmoqda "
                f"(eng eskisi {age_label(waited)}) — saralash (resolve) ishlamayapti; "
                "<code>make worker</code> loglari",
            )
        )
    if failing:
        found["job_failing"] = Problem(
            "job_failing",
            "warning",
            _lines([line for _, line in failing]),
            tuple(failing),
        )
    for alert in costs.spend_alerts(
        status.spend,
        daily_cap=settings.daily_cost_cap_usd,
        monthly_cap=settings.monthly_cost_cap_usd,
        alert_pct=settings.spend_alert_pct,
    ):
        add(alert.key, alert.text)
    if (
        userbot_on(status)
        and code is None
        and not (userbot is not None and userbot.stale)
        and status.chats_on > 0
        and status.monitoring_since is not None
        and status.now - status.monitoring_since >= NO_SIGNALS_WINDOW
        and status.interactions_24h == 0
    ):
        add(
            "no_signals_24h",
            "⚠️ 24 soatda bitta ham Telegram signal yoʻq — chatlar oʻlikmi? /chats",
        )
    if status.push_failing_streak >= PUSH_FAILING_STREAK:
        add(
            "push_failing",
            f"⚠️ ERP javob bermayapti ({status.push_failing_streak} ta push "
            "muvaffaqiyatsiz)",
        )
    if "push_rejected" in rep:
        status_code = rep["push_rejected"].detail.get("status")
        add(
            "push_rejected",
            push_rejected_text(
                status_code
                if isinstance(status_code, int) and not isinstance(status_code, bool)
                else None
            ),
        )
    if "push_unconfirmed" in rep:
        add(
            "push_unconfirmed",
            f"⚠️ {_detail(rep['push_unconfirmed'], 'n')} ta push 48 soatdan beri ERPda "
            "koʻrinmadi — bir marta qayta yuborildi; qolganini /push tekshir bilan "
            "koʻring",
        )
    if "feed_contract" in rep:
        add(
            "feed_contract",
            f"⚠️ ERP feed shakli oʻzgardi ({_detail(rep['feed_contract'], 'code')}) — "
            "dasturchi kerak",
        )
    if "suppression_shrunk" in rep:
        add(
            "suppression_shrunk",
            f"⚠️ ERP roʻyxati {_detail(rep['suppression_shrunk'], 'pct')}% ga qisqardi — "
            "oʻchirish toʻxtatildi",
        )
    if status.review_backlog >= REVIEW_BACKLOG_THRESHOLD:
        add(
            "review_backlog",
            f"⚠️ {status.review_backlog} ta karta 3 kundan beri kutmoqda — /lidlar",
        )
    if "anthropic_failing" in rep:
        add(
            "anthropic_failing",
            f"⚠️ Anthropic javob bermayapti ({_detail(rep['anthropic_failing'], 'code')})",
        )
    facts = status.anthropic
    if anthropic_silent(facts, status.now):
        silence = (
            "hech qachon javob bermagan"
            if facts.last_ok_at is None
            else f"{age_label(status.now - facts.last_ok_at)} jim"
        )
        add(
            "anthropic_silence",
            f"⚠️ Anthropic {silence} — tasnif navbatda ({facts.pending_jobs} ta); "
            "kalit va status.anthropic.com ni tekshiring",
        )
    if status.stuck_batches:
        batch_id, submitted_at = status.stuck_batches[0]
        more = len(status.stuck_batches) - 1
        extra = f" va yana {more} ta" if more else ""
        add(
            "batch_stuck",
            f"⚠️ Tungi batch {age_label(status.now - submitted_at)} tugamadi "
            f"({escape(batch_id)}{extra}) — <code>make worker</code> loglari; "
            "ishlar keyingi tunda qayta yuboriladi, 3-urinishdan keyin tezkor yoʻl "
            "oʻzi oladi",
        )
    if "pii_leak" in rep:
        add(
            "pii_leak",
            f"⚠️ Model javobida shaxsiy maʼlumot chiqdi ({_detail(rep['pii_leak'], 'n')} "
            "ta, maydon tashlandi) — dasturchi kerak",
        )
    if not info.configured:
        add("backup_unconfigured", "⚠️ Zaxira sozlanmagan — <code>make backup-key</code>")
    elif info.stale:
        last = (
            "yoʻq"
            if info.created_at is None
            else f"{age_label(status.now - info.created_at)} oldin"
        )
        add("backup_stale", f"⚠️ Oxirgi zaxira {last} — <code>make backup</code>")
    if "ig_token_expired" in rep:
        add("ig_token_expired", "⚠️ Instagram tokeni eskirdi — yangilang")
    if "source_low_precision" in rep:
        subjects = _low_precision_lines(rep["source_low_precision"])
        found["source_low_precision"] = Problem(
            "source_low_precision",
            "warning",
            _lines([line for _, line in subjects]),
            subjects,
        )
    if "evidence_over_cap" in rep:
        add(
            "evidence_over_cap",
            f"⚠️ Dalil papkasi {_detail(rep['evidence_over_cap'], 'gb')} GB — eski "
            "fayllar muddatidan oldin oʻchirildi",
        )
    return [found[k] for k in PROBLEM_KEYS if k in found]


def _low_precision_lines(reported: Reported) -> tuple[tuple[str, str], ...]:
    """(source key, its line) per auto-paused source (T-36 raises
    ``detail={"sources": [{"key", "title", "pct"}, …]}``)."""
    sources = reported.detail.get("sources")
    items = sources if isinstance(sources, list) else [reported.detail]
    lines: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or item.get("title") or "?")
        title = escape(item.get("title") or item.get("key") or "?")
        pct = escape(item.get("pct", "?"))
        lines.append(
            (
                key,
                f"⚠️ {title} aniqligi {pct}% — bir haftaga toʻxtatildi "
                "(/manbalar bilan yoqiladi)",
            )
        )
    return tuple(lines) or (
        ("?", "⚠️ Manba aniqligi past — bir haftaga toʻxtatildi (/manbalar)"),
    )


_RECOVERY: Final[Mapping[str, str]] = {
    "db_down": "✅ Baza qayta javob beryapti — tiklandi",
    "disk_low": "✅ Diskda yana joy bor — tiklandi",
    "worker_silent": "✅ Rejalashtiruvchi qayta ishlayapti — tiklandi",
    "userbot_unauthorized": "✅ Telegram oʻquvchi akkaunti qayta ulandi — tiklandi",
    "push_404": "✅ ERP kaliti yana qabul qilinyapti — tiklandi",
    "erp_redirect": "✅ ERP manzili toʻgʻri — tiklandi",
    "backup_failed": "✅ Tungi zaxira yana yozildi — tiklandi",
    "restore_drill_failed": "✅ Zaxirani tiklash sinovi oʻtdi — tiklandi",
    "suppression_stale": "✅ Bostirish roʻyxati yangilandi — push davom etadi",
    "suppression_selftest_failed": (
        "✅ ERP roʻyxati oʻz-oʻzini tekshirishdan oʻtdi — tiklandi"
    ),
    "suppression_stale_hit": (
        "✅ Toʻliq yuklash bostirish roʻyxatini yangiladi — tiklandi"
    ),
    "userbot_silent": "✅ Telegram oʻquvchi qayta ishlayapti — tiklandi",
    "flood_breaker": "✅ Telegram FloodWait tugadi — oʻquvchi yana qoʻshiladi — tiklandi",
    "collector_blocked": "✅ Saytlar yana ochiq — kollektorlar davom etadi — tiklandi",
    "collector_silent": "✅ Kollektorlar yana ishlayapti — tiklandi",
    "job_failing": "✅ Rejalashtirilgan ishlar yana xatosiz tugayapti — tiklandi",
    costs.COST_CAP_KEY: (
        "✅ Xarajat chegarasi yangilandi — pullik chaqiruvlar davom etadi"
    ),
    costs.SPEND_HIGH_KEY: "✅ Xarajat yana chegara ichida — tiklandi",
    "no_signals_24h": "✅ Telegramdan yana signal kelyapti — tiklandi",
    "push_failing": "✅ ERP yana javob beryapti — tiklandi",
    "push_rejected": "✅ ERP pushlarni yana qabul qilyapti — tiklandi",
    "push_unconfirmed": "✅ Hamma push ERPda koʻrindi — tiklandi",
    "feed_contract": "✅ ERP feed yana kutilgan shaklda — tiklandi",
    "suppression_shrunk": "✅ ERP roʻyxati qisqarmadi — tiklandi",
    "review_backlog": "✅ Kartalar navbati kamaydi — tiklandi",
    "anthropic_failing": "✅ Anthropic yana javob beryapti — tiklandi",
    "anthropic_silence": "✅ Anthropic yana javob beryapti — tasnif davom etadi",
    "batch_stuck": "✅ Tungi batch tugadi — tiklandi",
    "pii_leak": "✅ Model javoblarida shaxsiy maʼlumot yoʻq — tiklandi",
    "backup_unconfigured": "✅ Zaxira sozlandi — tiklandi",
    "backup_stale": "✅ Zaxira yana yangi — tiklandi",
    "ig_token_expired": "✅ Instagram tokeni yangilandi — tiklandi",
    "source_low_precision": "✅ Past aniqlik pauzasi tugadi — tiklandi",
    "evidence_over_cap": "✅ Dalil papkasi chegara ichida — tiklandi",
}


def recovery_text(key: str) -> str:
    """The one-line «it is back» notice for a problem key."""
    return _RECOVERY.get(key) or f"✅ {escape(key)} — tiklandi"


# --- raised problems ----------------------------------------------------------


async def raise_problem(
    session: AsyncSession,
    key: str,
    *,
    now: datetime,
    by: str,
    detail: Mapping[str, Any] | None = None,
    until: datetime | None = None,
) -> None:
    """Put a problem the caller saw happen in force (the caller's transaction
    writes it). ``detail`` replaces the stored one; ``since`` stays the first
    raise of this episode. ``until`` expires it by itself (an event with no
    «it is over» moment). Only ``REPORTED_KEYS`` may be raised: a key the
    status reads from the tables has its home there."""
    if key not in REPORTED_KEYS:
        raise ValueError(f"{key!r} is not a reported problem key (06 §4.2)")
    value = {
        "since": now.isoformat(),
        "until": until.isoformat() if until is not None else None,
        "detail": dict(detail or {}),
    }
    stmt = pg_insert(RuntimeFlag).values(
        key=PROBLEM_FLAG_PREFIX + key, value=value, updated_by=by
    )
    kept = sa.func.jsonb_set(
        stmt.excluded.value,
        sa.text("'{since}'"),
        sa.func.coalesce(RuntimeFlag.value["since"], stmt.excluded.value["since"]),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[RuntimeFlag.key],
            set_={"value": kept, "updated_by": stmt.excluded.updated_by},
        )
    )


async def clear_problem(session: AsyncSession, key: str) -> bool:
    """The episode is over: True when a raised row was removed."""
    if key not in REPORTED_KEYS:
        raise ValueError(f"{key!r} is not a reported problem key (06 §4.2)")
    result = await session.execute(
        sa.delete(RuntimeFlag)
        .where(RuntimeFlag.key == PROBLEM_FLAG_PREFIX + key)
        .returning(RuntimeFlag.key)
    )
    return result.first() is not None


# --- alert memory ------------------------------------------------------------


def _told(last: Mapping[str, datetime], key: str) -> bool:
    """This episode was announced: an alert newer than its recovery."""
    alerted = last.get(ALERT_KIND + key)
    if alerted is None:
        return False
    cleared = last.get(RECOVERED_KIND + key)
    return cleared is None or cleared < alerted


def _repeat(key: str) -> timedelta:
    return REPEAT_OVERRIDES.get(key, timedelta(hours=settings.alert_repeat_hours))


def _undelivered(last: Mapping[str, datetime], key: str, now: datetime) -> bool:
    """The key's own teller gave up on this episode: an ``undelivered:<key>``
    row newer than the key's last recovery and inside its repeat window (a
    ``DAILY_KEYS`` key: this Tashkent day) — an older one is another
    episode's."""
    at = last.get(UNDELIVERED_KIND + key)
    if at is None:
        return False
    cleared = last.get(RECOVERED_KIND + key)
    if cleared is not None and cleared >= at:
        return False
    if key in DAILY_KEYS:
        return at >= costs.day_start(now)
    return now - at <= _repeat(key)


def _alert_due(last: Mapping[str, datetime], key: str, now: datetime) -> bool:
    # Said first by its own job — unless that job could not deliver it: then
    # the sweep says it, or nobody ever would (06 §4.2).
    if (
        key in TOLD_FIRST_ELSEWHERE
        and not _told(last, key)
        and not _undelivered(last, key, now)
    ):
        return False
    alerted = last.get(ALERT_KIND + key)
    if alerted is None:
        return True
    if key in DAILY_KEYS:
        return alerted < costs.day_start(now)
    return now - alerted > _repeat(key)


def _subject_ref(key: str, subject: str) -> str:
    return f"{key}:{subject}"


async def _subject_ledger(session: AsyncSession, key: str) -> dict[str, datetime]:
    """``ref`` → newest ``alert:<key>`` row, for a PER_SUBJECT_KEYS key."""
    rows = await session.execute(
        sa.select(ReminderLog.ref, sa.func.max(ReminderLog.sent_at))
        .where(ReminderLog.kind == ALERT_KIND + key)
        .group_by(ReminderLog.ref)
    )
    return {str(ref): at for ref, at in rows.all()}


def _due_subjects(
    problem: Problem, told: Mapping[str, datetime], now: datetime
) -> tuple[tuple[str, str], ...]:
    window = _repeat(problem.key)
    return tuple(
        (subject, line)
        for subject, line in problem.subjects
        if (at := told.get(_subject_ref(problem.key, subject))) is None
        or now - at > window
    )


def _narrowed(problem: Problem, subjects: tuple[tuple[str, str], ...]) -> Problem:
    """The problem as it is said now: only these subjects' lines."""
    return replace(
        problem, subjects=subjects, text=_lines([line for _, line in subjects])
    )


async def _ledger(session: AsyncSession, keys: Iterable[str]) -> dict[str, datetime]:
    wanted = list(keys)
    kinds = [ALERT_KIND + k for k in wanted] + [RECOVERED_KIND + k for k in wanted]
    kinds += [UNDELIVERED_KIND + k for k in wanted if k in TOLD_FIRST_ELSEWHERE]
    rows = await session.execute(
        sa.select(ReminderLog.kind, sa.func.max(ReminderLog.sent_at))
        .where(ReminderLog.kind.in_(kinds))
        .group_by(ReminderLog.kind)
    )
    return {str(kind): at for kind, at in rows.all()}


async def alerts_due(
    session: AsyncSession,
    found: list[Problem],
    *,
    now: datetime,
    unjudged: Collection[str] = (),
) -> tuple[list[Problem], list[str]]:
    """Which problems to say now, and which keys have recovered.

    A problem is due when no ``alert:<key>`` row is newer than
    ALERT_REPEAT_HOURS (a ``DAILY_KEYS`` key: none in this Tashkent day; a
    ``REPEAT_OVERRIDES`` key: none within its own window). A key in
    TOLD_FIRST_ELSEWHERE is due only once its own job recorded the alert of
    this episode, so the sweep repeats it but never says it first. A
    ``PER_SUBJECT_KEYS`` problem is due with the subjects not said within its
    window, and only those (06 §4.2 «once per source per week»: a second
    source paused two days after the first is said at once). A key has
    recovered when it is no longer a problem, is judged, and its last alert
    is newer than its last recovery."""
    last = await _ledger(session, PROBLEM_KEYS)
    due: list[Problem] = []
    for problem in found:
        if problem.key in PER_SUBJECT_KEYS and problem.subjects:
            fresh = _due_subjects(
                problem, await _subject_ledger(session, problem.key), now
            )
            if fresh:
                due.append(_narrowed(problem, fresh))
        elif _alert_due(last, problem.key, now):
            due.append(problem)
    current = {p.key for p in found}
    recovered = [
        key
        for key in PROBLEM_KEYS
        if key not in current and key not in unjudged and _told(last, key)
    ]
    return due, recovered


def _mark(session: AsyncSession, kind: str, keys: Iterable[str], now: datetime) -> None:
    for key in keys:
        session.add(ReminderLog(kind=kind + key, ref=key, sent_at=now))


def mark_alerted(session: AsyncSession, keys: Iterable[str], *, now: datetime) -> None:
    """Record that these problems were *actually delivered* (the caller's
    commit writes the rows). Call only after the send succeeded."""
    _mark(session, ALERT_KIND, keys, now)


def mark_recovered(session: AsyncSession, keys: Iterable[str], *, now: datetime) -> None:
    _mark(session, RECOVERED_KIND, keys, now)


# Indirection for tests: the short transactions of the claims.
_own_session: Callable[[], AbstractAsyncContextManager[AsyncSession]] = session_scope

OwnerNotify = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class _Line:
    key: str
    text: str
    # PER_SUBJECT_KEYS: what the text is made of, claimed one by one.
    subjects: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _Claimed:
    text: str
    ids: tuple[int, ...]


async def _claim(
    session: AsyncSession, kind: str, line: _Line, now: datetime
) -> _Claimed | None:
    """Re-check under the key's lock that ``kind`` is still due, then write
    its row (one per subject still due, for a PER_SUBJECT_KEYS line); what
    is to be said and the ids, or None when another speaker got there first.
    The lock is costs.ALERT_LOCK keyed on ``alert:<key>``, the one
    ``costs.ask``'s cost_cap claim takes, and it lasts until the caller
    commits."""
    key = line.key
    await session.execute(
        sa.select(
            sa.func.pg_advisory_xact_lock(
                costs.ALERT_LOCK, sa.func.hashtext(ALERT_KIND + key)
            )
        )
    )
    if kind == ALERT_KIND and line.subjects:
        probe = Problem(key, "warning", line.text, line.subjects)
        fresh = _due_subjects(probe, await _subject_ledger(session, key), now)
        if not fresh:
            return None
        rows = [
            ReminderLog(kind=kind + key, ref=_subject_ref(key, subject), sent_at=now)
            for subject, _ in fresh
        ]
        session.add_all(rows)
        await session.flush()
        text = _lines([text for _, text in fresh])
        return _Claimed(text, tuple(row.id for row in rows))
    last = await _ledger(session, [key])
    still = _alert_due(last, key, now) if kind == ALERT_KIND else _told(last, key)
    if not still:
        return None
    row = ReminderLog(kind=kind + key, ref=key, sent_at=now)
    session.add(row)
    await session.flush()
    return _Claimed(line.text, (row.id,))


async def _release(ids: list[int]) -> None:
    try:
        async with _own_session() as own:
            await own.execute(sa.delete(ReminderLog).where(ReminderLog.id.in_(ids)))
    except Exception as exc:
        # The claim stays: the line is not said again until its window ends —
        # the quiet direction, and /holat still shows the problem.
        log.warning("alert claim not released", extra={"error": type(exc).__name__})


async def _say(
    kind: str,
    lines: list[_Line],
    compose: Callable[[list[str]], str],
    *,
    now: datetime,
    notify: OwnerNotify,
) -> list[str]:
    """Claim each line, commit, tell what was claimed as one message
    (``compose`` joins the texts), and take the claims back unless it was
    delivered. Returns the keys said now; a key (a subject) another speaker
    claimed first is left out of the message."""
    claimed: list[tuple[str, _Claimed]] = []
    async with _own_session() as own:
        for line in lines:
            got = await _claim(own, kind, line, now)
            if got is not None:
                claimed.append((line.key, got))
    if not claimed:
        return []
    delivered = False
    try:
        delivered = await notify(compose([got.text for _, got in claimed]))
    finally:
        if not delivered:
            await _release([row_id for _, got in claimed for row_id in got.ids])
    return [key for key, _ in claimed] if delivered else []


def _line(problem: Problem) -> _Line:
    subjects = problem.subjects if problem.key in PER_SUBJECT_KEYS else ()
    return _Line(problem.key, problem.text, subjects)


def bundle(texts: list[str]) -> str:
    """The warnings of one sweep as one message (MIYA WP-80)."""
    if len(texts) == 1:
        return texts[0]
    return BUNDLE_HEADER.format(n=len(texts)) + "\n\n" + "\n\n".join(texts)


@dataclass(frozen=True, slots=True)
class TellReport:
    said: tuple[str, ...] = ()
    recovered: tuple[str, ...] = ()
    # Due but held for the end of QUIET_HOURS (warnings, recoveries).
    held: tuple[str, ...] = ()


async def tell(
    session: AsyncSession,
    found: list[Problem],
    notify: OwnerNotify,
    *,
    now: datetime,
    unjudged: Collection[str] = (),
) -> TellReport:
    """Say what is due, once (06 §4.2–4.3, MIYA ``health_job``).

    Critical problems go one message each, at any hour; the warnings of one
    sweep are one message; recoveries are one message. Inside QUIET_HOURS
    warnings and recoveries wait — the ledger still shows them due when the
    quiet hours end, nothing is lost. Each line is claimed in the ledger
    before it is said and the claim is taken back unless ``notify`` reported
    it delivered, so an unreachable Telegram means it is tried again next
    sweep and two speakers at once never say it twice. ``notify`` is the
    owner seam: it has no recipient argument."""
    quiet = in_quiet_hours(now)
    due, recovered = await alerts_due(session, found, now=now, unjudged=unjudged)
    said: list[str] = []
    held: list[str] = []
    for problem in due:
        if problem.severity == "critical":
            said += await _say(
                ALERT_KIND, [_line(problem)], bundle, now=now, notify=notify
            )
    warnings = [p for p in due if p.severity != "critical"]
    if warnings and quiet:
        held += [p.key for p in warnings]
    elif warnings:
        said += await _say(
            ALERT_KIND,
            [_line(p) for p in warnings],
            bundle,
            now=now,
            notify=notify,
        )
    back: list[str] = []
    if recovered and quiet:
        held += recovered
    elif recovered:
        back = await _say(
            RECOVERED_KIND,
            [_Line(key, recovery_text(key)) for key in recovered],
            "\n".join,
            now=now,
            notify=notify,
        )
    return TellReport(tuple(said), tuple(back), tuple(held))


async def tell_offline(
    found: list[Problem],
    notify: OwnerNotify,
    *,
    now: datetime,
    memo: dict[str, datetime],
) -> list[str]:
    """The critical problems only, deduped in ``memo`` (the caller's, per
    process): with the database gone there is nowhere to write the ledger."""
    repeat = timedelta(hours=settings.alert_repeat_hours)
    said: list[str] = []
    for problem in found:
        if problem.severity != "critical":
            continue
        last = memo.get(problem.key)
        if last is not None and now - last <= repeat:
            continue
        if await notify(problem.text):
            memo[problem.key] = now
            said.append(problem.key)
    return said


async def adopt_offline(memo: dict[str, datetime]) -> list[str]:
    """The database answers again: what :func:`tell_offline` said while it
    was gone goes into the ledger as ``alert:<key>`` at the moment it was
    said, then the memo is emptied. The next :func:`tell` then repeats such a
    key on the ledger's clock and — ``db_down`` above all — announces its
    recovery (06 §4.2: every key has its recovery line), which an in-memory
    memo alone never could. Its own transaction; if it fails the memo stays
    and the next sweep tries again. Returns the keys written."""
    if not memo:
        return []
    written = sorted(memo)
    async with _own_session() as own:
        for key in written:
            mark_alerted(own, [key], now=memo[key])
    for key in written:
        memo.pop(key, None)
    return written


# --- the Anthropic switch costs.ask reads (04 §8 step 6) ----------------------


@dataclass(slots=True)
class _AnthropicCache:
    at: datetime | None = None
    silent: bool = False


_anthropic_cache = _AnthropicCache()


async def _claim_probe(now: datetime) -> datetime | None:
    """Take the probe slot, across processes (a ``runtime_flags`` row updated
    only when its stamp is old enough): None for the one caller per
    ANTHROPIC_PROBE_INTERVAL that got it, else when the slot frees. Its own
    short transaction: the caller's may run for long."""
    stamp = {"at": now.isoformat()}
    insert = pg_insert(RuntimeFlag).values(
        key=ANTHROPIC_PROBE_FLAG, value=stamp, updated_by="worker:anthropic_probe"
    )
    stamped = RuntimeFlag.value["at"].astext.cast(sa.DateTime(timezone=True))
    stmt = insert.on_conflict_do_update(
        index_elements=[RuntimeFlag.key],
        set_={"value": insert.excluded.value, "updated_by": insert.excluded.updated_by},
        where=stamped <= now - ANTHROPIC_PROBE_INTERVAL,
    ).returning(RuntimeFlag.key)
    async with _own_session() as own:
        if (await own.execute(stmt)).first() is not None:
            return None
        taken = await own.scalar(
            sa.select(stamped).where(RuntimeFlag.key == ANTHROPIC_PROBE_FLAG)
        )
    frees = (taken or now) + ANTHROPIC_PROBE_INTERVAL
    # A stamp from a clock ahead of this one must not refuse for longer than
    # one interval; a free slot nobody took is asked again within the cache.
    return min(max(frees, now + ANTHROPIC_CACHE), now + ANTHROPIC_PROBE_INTERVAL)


async def anthropic_down_until(session: AsyncSession, now: datetime) -> datetime | None:
    """``costs.set_anthropic_health``'s check: while anthropic_silence holds,
    the moment the next probe slot frees (ask refuses Anthropic calls and
    remembers it until then) — except for one call per
    ANTHROPIC_PROBE_INTERVAL, let through (None) so a success can end it.
    Without the probe the switch would latch: ask refuses every Anthropic
    call, so no call ever succeeds again. None when the API is not silent.
    The facts are re-read at most every ANTHROPIC_CACHE in this process."""
    cache = _anthropic_cache
    if cache.at is None or not (timedelta(0) <= now - cache.at < ANTHROPIC_CACHE):
        cache.silent = anthropic_silent(await anthropic_facts(session), now)
        cache.at = now
    if not cache.silent:
        return None
    frees = await _claim_probe(now)
    if frees is None:
        log.info("anthropic_silence: one probe call let through")
        # The probe's outcome decides the next answer, not this cache.
        cache.at = None
    return frees


async def anthropic_down(session: AsyncSession, now: datetime) -> bool:
    """:func:`anthropic_down_until` as a yes/no (``/holat``, tests)."""
    return await anthropic_down_until(session, now) is not None


def reset_anthropic_cache() -> None:
    _anthropic_cache.at = None
    _anthropic_cache.silent = False
