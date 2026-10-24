# VENDORED_FROM dailyJournal@6b1f67f miya/worker/main.py
"""The scheduler process (APScheduler, Asia/Tashkent).

``JOB_TABLE`` is every scheduled job of the spec — 03 §3 (collectors), 04 §0
(the pipeline), 05 (push, reconcile, precision) and 06 (health, backups,
digest, retention) — in one place. A row whose ``job`` is set is registered;
a row whose ``job`` is None is a **slot**: the cadence is written down, the
task that builds it is named, and nothing is imported that does not exist
yet. The task fills its slot (``job=collector_job(ErpSync, "delta")`` or its
own coroutine) and ``tests/test_worker_schedule.py`` holds the table to the
spec.

Built here (T-08):

* ``heartbeat`` — every minute: the worker's own liveness row, then the
  dead-man GET of ``DEADMAN_PING_URL`` at most every ``DEADMAN_PING_MINUTES``
  (06 §4.2: the only alarm that does not depend on the bot);
* ``health`` — every 5 minutes: one status, its problems, and the alert
  ledger decides what is said (``health.tell``); with the database gone only
  the critical problems, deduped in this process;
* ``backup`` — at ``BACKUP_TIME``: ``pg_dump -Fc | age``, pruned, the
  ``backup`` heartbeat ``{ok, size, sent, path, error}`` and the
  ``backup_counts`` the drill compares with;
* ``restore_drill`` — ``RESTORE_DRILL_DAY`` 05:00.

Built by later tasks and registered here: the five ERP feed cadences, read
from ``collectors/erp_sync.JOBS`` (T-09's suppression delta/full, T-10's
outcomes/quarter/year walks), each ``run_collector(ErpSync, variant=…)``;
and ``resolve`` (T-17, ``services/companies.resolve_job``, every 2 minutes).

Every job's outcome is also recorded as a ``job:<id>`` heartbeat by an
APScheduler listener (04 §0: «every job … beats job:<id>»). Crons are
standard crontab read by ``services/crontab`` (day-of-week 0 = Sunday — never
APScheduler's own numbering). A collector's primary cadence is its
``sources.cron`` (editable from ``/manbalar``, 03 §3); the table carries the
seeded value and a test keeps the two equal.

Alerts reach the owner through one seam, :func:`set_owner_notifier`: a
callable ``(text) -> delivered`` with no recipient argument. The owner bot
wires it (T-20, ``bot/deliver.to_person`` with ``OWNER_TELEGRAM_ID``); until
then nothing is delivered, nothing is marked said, and every problem stays
due. Nothing in this process sends a message to anyone else, ever.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo
from pathlib import Path
from time import monotonic
from typing import Any, Final

import httpx
import sqlalchemy as sa
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.interval import IntervalTrigger

from clientscrapper.collectors import erp_sync
from clientscrapper.collectors.base import Collector
from clientscrapper.collectors.runner import run_collector
from clientscrapper.config import settings
from clientscrapper.db.models import Source
from clientscrapper.db.session import engine, session_scope
from clientscrapper.logs import setup_logging
from clientscrapper.services import (
    backup,
    companies,
    costs,
    crontab,
    erp_feeds,
    health,
    restore_drill,
)
from clientscrapper.services import http as http_layer
from clientscrapper.services.lifecycle import idle_until_stopped, run_main

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[Any]]

# --- the owner seam ------------------------------------------------------------


async def _not_wired(text: str) -> bool:
    log.warning("owner notifier not wired: nothing delivered (the owner bot, T-20)")
    return False


@dataclass(slots=True)
class _Seams:
    notify: health.OwnerNotify = _not_wired
    # (piece, caption) -> delivered: the nightly file to the owner's Telegram.
    send_document: Callable[[Path, str], Awaitable[bool]] | None = None


_seams = _Seams()


def set_owner_notifier(notify: health.OwnerNotify | None) -> None:
    """Who tells the owner: ``(text) -> delivered``. The recipient is fixed
    inside it (``OWNER_TELEGRAM_ID``) — there is no argument to point it at
    anybody else (06 §4.3). ``costs.ask`` tells cost_cap through it too, held
    inside QUIET_HOURS like every warning (06 §4.2)."""
    _seams.notify = notify or _not_wired
    costs.set_cap_notifier(notify, hold=health.in_quiet_hours)


def set_backup_sender(send: Callable[[Path, str], Awaitable[bool]] | None) -> None:
    """``(file, caption) -> delivered``: one document to the owner (T-20)."""
    _seams.send_document = send


async def notify(text: str) -> bool:
    return await _seams.notify(text)


# --- the table -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobSlot:
    """One scheduled job. ``triggers`` are standard crontab expressions or
    ``every <n>m`` / ``every <n>s``; several fire as one job (an OrTrigger).
    ``wakes_on`` names the ``NOTIFY`` channel the job also listens on (04 §0,
    the event-driven Telegram path) — the cron is then the fallback sweep.
    ``source_key``/``variant`` name a collector run; ``primary`` says its
    cron is ``sources.cron``."""

    id: str
    triggers: tuple[str, ...]
    task: str
    job: Job | None = None
    source_key: str | None = None
    variant: str | None = None
    primary: bool = False
    wakes_on: str | None = None
    note: str = ""
    # Handed the scheduler (the heartbeat counts its jobs).
    pass_scheduler: bool = field(default=False, compare=False)


def _at(moment: time) -> str:
    return f"{moment.minute} {moment.hour} * * *"


def _collector(
    id: str,
    cron: str,
    task: str,
    source_key: str | None,
    variant: str | None = None,
    *,
    primary: bool = False,
    note: str = "",
) -> JobSlot:
    return JobSlot(
        id=id,
        triggers=(cron,),
        task=task,
        source_key=source_key,
        variant=variant,
        primary=primary,
        note=note,
    )


# The notes the table carries for the ERP cadences (03 §3, 05 §B.2).
_ERP_NOTES: Final[Mapping[str, str]] = {"outcomes": "cursor + recent walks"}


def _erp_sync_slot(erp: erp_sync.ErpJob) -> JobSlot:
    """One ERP cadence of ``erp_sync.JOBS`` as a built slot: the delta is the
    row's own cron (``sources.erp_sync.cron``), the others are variants."""
    return JobSlot(
        id=erp.id,
        triggers=(erp.cron,),
        task=erp.task,
        job=collector_job(erp_sync.ErpSync, erp.variant),
        source_key=erp_sync.ErpSync.key,
        variant=erp.variant,
        primary=erp.variant == "delta",
        note=_ERP_NOTES.get(erp.variant, ""),
    )


def job_table() -> tuple[JobSlot, ...]:
    """Every job of the spec, built or a slot (see the module docstring)."""
    drill_day = settings.restore_drill_day
    return (
        # --- 06: health, backups (T-08) -----------------------------------------
        JobSlot("heartbeat", ("every 1m",), "T-08", heartbeat_job, pass_scheduler=True),
        JobSlot("health", ("every 5m",), "T-08", health_job),
        JobSlot("backup", (_at(settings.backup_time_parsed),), "T-08", backup_job),
        JobSlot("restore_drill", (f"0 5 * * {drill_day}",), "T-08", restore_drill_job),
        # --- 03 §3: collectors --------------------------------------------------
        # The ERP feeds (T-09 suppression, T-10 outcomes): erp_sync.JOBS is the
        # one statement of their ids, crons and variants.
        *(_erp_sync_slot(erp) for erp in erp_sync.JOBS),
        JobSlot(
            "telegram_monitor",
            ("*/15 * * * *",),
            "T-14",
            source_key="telegram_monitor",
            primary=True,
            wakes_on="cs_interaction",
            note="join (10–20 h) and backfill (01:00) run in the userbot process",
        ),
        _collector("serp_layer", "0 10,14 * * *", "T-25", "serp_layer", primary=True),
        _collector("serp_layer_night", "10 2 * * *", "T-25", "serp_layer", "night"),
        _collector(
            "instagram_discovery",
            "0 11 * * *",
            "T-26",
            "instagram_discovery",
            primary=True,
        ),
        _collector(
            "instagram_hashtags", "0 12 * * *", "T-26", "instagram_discovery", "hashtags"
        ),
        _collector("olx_business", "0 7 * * *", "T-27", "olx_business", primary=True),
        _collector("olx_business_full", "0 7 * * 4", "T-27", "olx_business", "full"),
        _collector("directories", "0 23 * * 4", "T-22", "directories", primary=True),
        _collector("directories_full", "0 23 1-7 * *", "T-22", "directories", "full"),
        _collector("hh_vacancies", "20 */4 * * *", "T-23", "hh_vacancies", primary=True),
        _collector("exhibitors", "0 9 * * 1", "T-32", "exhibitors", primary=True),
        _collector("maps", "0 10 * * 2", "T-33", "maps", primary=True, note="text"),
        _collector("maps_details", "30 10 * * *", "T-33", "maps", "details"),
        _collector("maps_2gis", "40 10 * * *", "T-33", "maps", "twogis"),
        _collector(
            "brand_dealers", "0 13 1-10 * *", "T-34", "brand_dealers", primary=True
        ),
        _collector("uzum_sellers", "0 9 * * 5", "T-37", "uzum_sellers", primary=True),
        _collector("uzum_shops", "0 4 15 * *", "T-37", "uzum_sellers", "shops"),
        _collector("tenders", "0 4 * * 6", "T-38", None, note="phase 2; no sources row"),
        # --- 04 §0: the pipeline -----------------------------------------------
        JobSlot("classify", ("every 15m",), "T-15", wakes_on="cs_interaction"),
        JobSlot("model_batch_submit", ("30 23 * * *",), "T-16"),
        JobSlot(
            "model_batch_collect",
            ("30 0 * * *", "0,30 1-7 * * *", "0 8 * * *"),
            "T-16",
            note="every 30 min 00:30–08:00",
        ),
        JobSlot("resolve", ("every 2m",), "T-17", companies.resolve_job),
        JobSlot("score", ("every 15m",), "T-18", wakes_on="cs_interaction"),
        JobSlot("review_cards", ("every 2m",), "T-21", note="sends inside REVIEW_HOURS"),
        JobSlot("enrich", ("*/10 7-21 * * *",), "T-24", note="every 10 min 07:00–22:00"),
        JobSlot("verdict", ("every 10m",), "T-30"),
        # --- 05: push, reconcile, precision ---------------------------------------
        JobSlot("crm_push", ("every 1m",), "T-28", note="sends inside PUSH_WINDOW"),
        JobSlot("reconcile", ("every 10m",), "T-28"),
        JobSlot("source_precision", ("0 4 * * 0",), "T-36"),
        # --- 06: digest, retention --------------------------------------------------
        JobSlot("digest", (_at(settings.digest_time_parsed),), "T-35"),
        JobSlot("retention", ("15 4 * * *",), "T-31"),
    )


def build_trigger(specs: tuple[str, ...], tz: tzinfo) -> BaseTrigger:
    """One trigger for a slot's expressions (crontab or ``every <n>m|s``)."""
    parts: list[BaseTrigger] = []
    for spec in specs:
        if spec.startswith("every "):
            amount, unit = spec[len("every ") : -1], spec[-1]
            if not amount.isdigit() or unit not in {"m", "s"} or int(amount) < 1:
                raise crontab.CrontabError(f"interval {spec!r}")
            seconds = int(amount) * (60 if unit == "m" else 1)
            parts.append(IntervalTrigger(seconds=seconds, timezone=tz))
        else:
            parts.append(crontab.trigger(spec, tz))
    if not parts:
        raise crontab.CrontabError("a job needs a trigger")
    return parts[0] if len(parts) == 1 else OrTrigger(parts)


def slot_trigger(
    slot: JobSlot, source_crons: Mapping[str, str | None], tz: tzinfo
) -> BaseTrigger | None:
    """The trigger a built slot runs on, or None when it is not scheduled.

    A primary collector runs on its ``sources.cron`` (03 §3); a row whose
    cron is NULL is not scheduled (02 §2.1: NULL = never scheduled — health
    stops judging it too); a key absent from ``source_crons`` (the table
    could not be read) keeps the seeded cron. A ``sources.cron`` the
    translator refuses — an owner edit — falls back to the seeded cron with
    an error line, and ``health`` names it under collector_silent: one bad
    row must not keep the whole worker from starting (a crash loop under
    ``restart: unless-stopped``)."""
    if slot.primary and slot.source_key and slot.source_key in source_crons:
        override = source_crons[slot.source_key]
        if override is None:
            log.info("sources.cron is NULL: not scheduled", extra={"job": slot.id})
            return None
        try:
            return build_trigger((override,), tz)
        except crontab.CrontabError:
            log.error(
                "sources.cron refused; the seeded cron is used",
                extra={"job": slot.id, "source": slot.source_key},
            )
    return build_trigger(slot.triggers, tz)


def build_scheduler(
    slots: tuple[JobSlot, ...] | None = None,
    *,
    source_crons: Mapping[str, str | None] | None = None,
) -> AsyncIOScheduler:
    """The scheduler with every built row of ``slots`` (default: the table)
    registered: one instance at a time, a missed run coalesced, a cron run up
    to an hour late still made. ``source_crons`` (``sources.key`` → cron)
    overrides a primary collector's seeded cron (``slot_trigger``)."""
    tz = settings.tz
    scheduler = AsyncIOScheduler(timezone=tz)
    for slot in slots if slots is not None else job_table():
        if slot.job is None:
            continue
        trigger = slot_trigger(slot, source_crons or {}, tz)
        if trigger is None:
            continue
        args = [scheduler] if slot.pass_scheduler else []
        scheduler.add_job(
            slot.job,
            trigger,
            args=args,
            id=slot.id,
            name=slot.id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
    return scheduler


# --- collector jobs --------------------------------------------------------------


@dataclass(slots=True)
class _Shared:
    # One Fetcher for every collector run: one breaker, one robots cache, one
    # set of host buckets (03 §1.1).
    fetcher: http_layer.Fetcher | None = None


_shared = _Shared()


def shared_fetcher() -> http_layer.Fetcher:
    if _shared.fetcher is None:
        _shared.fetcher = http_layer.Fetcher()
    return _shared.fetcher


def collector_job(cls: type[Collector], variant: str | None = None) -> Job:
    """The scheduler entry of one collector cadence: ``runner.run_collector``
    with the shared Fetcher, at the wall clock of the run (03 §1.2)."""

    async def job() -> None:
        await run_collector(
            cls, fetcher=shared_fetcher(), now=datetime.now(settings.tz), variant=variant
        )

    job.__name__ = f"collector_{cls.key}{'_' + variant if variant else ''}"
    return job


# --- self-monitoring -----------------------------------------------------------------


async def record_job_event(job_id: str, *, ok: bool, error: str | None) -> None:
    """Upsert the ``job:<id>`` heartbeat for one finished scheduler run. Its
    own session, and it never raises: a heartbeat that cannot be written is a
    log line, not a second failure on top of the job's own. The error is the
    exception's class: a message may carry what a row said."""
    try:
        async with session_scope() as session:
            await health.beat(
                session, health.JOB_PREFIX + job_id, detail={"ok": ok, "error": error}
            )
    except Exception as exc:
        log.error(
            "job outcome not recorded",
            extra={"job": job_id, "error": type(exc).__name__},
        )


def on_job_event(
    event: JobExecutionEvent, *, loop: asyncio.AbstractEventLoop
) -> asyncio.Future[None] | Any:
    """APScheduler listener (EVENT_JOB_EXECUTED | EVENT_JOB_ERROR). Listeners
    are plain callbacks: the upsert is handed to the loop; the future is for
    tests."""
    exc = getattr(event, "exception", None)
    error = None if exc is None else type(exc).__name__
    return asyncio.run_coroutine_threadsafe(
        record_job_event(event.job_id, ok=exc is None, error=error), loop
    )


async def prune_job_heartbeats(registered: list[str]) -> int:
    """Drop ``job:*`` rows of jobs this worker no longer runs, so /holat never
    names a retired job."""
    async with session_scope() as session:
        result = await session.execute(
            sa.text(
                "DELETE FROM heartbeats WHERE component LIKE 'job:%' "
                "AND component <> ALL(:registered)"
            ),
            {"registered": registered},
        )
        return int(getattr(result, "rowcount", 0) or 0)


@dataclass(slots=True)
class _Deadman:
    last: float | None = None


_deadman = _Deadman()


async def deadman_ping(*, transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """GET DEADMAN_PING_URL at most every DEADMAN_PING_MINUTES. Never raises."""
    url = settings.deadman_ping_url.strip()
    if not url:
        return False
    now = monotonic()
    every = settings.deadman_ping_minutes * 60
    if _deadman.last is not None and now - _deadman.last < every:
        return False
    _deadman.last = now
    status = await http_layer.ping(url, transport=transport)
    return status is not None and 200 <= status < 300


async def heartbeat_job(
    scheduler: AsyncIOScheduler | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """The worker's own liveness row, once a minute; then the dead-man ping —
    only after the beat is durable, so a dead database stops the ping too and
    the outside watcher notices."""
    jobs = len(scheduler.get_jobs()) if scheduler is not None else 0
    async with session_scope() as session:
        await health.beat(session, "worker", detail={"jobs": jobs})
    await deadman_ping(transport=transport)


# When the database is down the alert ledger is down with it; this memo keeps
# «Baza javob bermayapti» from being repeated every five minutes.
_offline_alerted_at: dict[str, datetime] = {}
# What was last logged per key, so a problem is one log line per repeat window.
_logged: dict[str, datetime] = {}


def _log_problems(found: list[health.Problem], now: datetime) -> None:
    repeat = timedelta(hours=settings.alert_repeat_hours)
    for problem in found:
        last = _logged.get(problem.key)
        if last is None or now - last > repeat:
            _logged[problem.key] = now
            log.warning(
                "health problem",
                extra={"problem": problem.key, "severity": problem.severity},
            )


async def health_job(
    *, now: datetime | None = None, notify: health.OwnerNotify | None = None
) -> health.TellReport | None:
    """Judge the system and tell the owner what needs a hand (every 5 min).

    One status, the problems it shows, then the ledger decides (``health.tell``):
    a problem is said once per ALERT_REPEAT_HOURS while it lasts (the spend
    alerts once per Tashkent day), and once more as «tiklandi» when it clears;
    critical ones ignore quiet hours. With the database unreachable only the
    critical problems are said, deduped in this process."""
    now = now or datetime.now(settings.tz)
    speak = notify or _seams.notify
    status: health.Status | None = None
    try:
        async with session_scope() as session:
            status = await health.gather(session, now=now)
            if status.db_ok:
                found = health.problems(status)
                _log_problems(found, now)
                return await health.tell(
                    session, found, speak, now=now, unjudged=health.unjudged(status)
                )
    except Exception:
        # Leaving the scope commits; with the database gone that can fail
        # too. A failure with the database *up* is a real one and stays loud.
        if status is None or status.db_ok:
            raise
        log.warning("health sweep: the database is unreachable")
    assert status is not None  # gather answered; only the commit failed
    found = health.problems(status)
    _log_problems(found, now)
    await health.tell_offline(found, speak, now=now, memo=_offline_alerted_at)
    return None


# --- backups -------------------------------------------------------------------------


def _caption(path: Path, size: int, index: int, total: int) -> str:
    stamp = backup.backup_stamp(path)
    when = health.clock_label(stamp) if stamp is not None else "?"
    return health.escape(f"🗄 Zaxira {when} · {health.size_label(size)} · {index}/{total}")


async def _send_backup(path: Path, size: int) -> tuple[bool, str | None]:
    """The nightly file to the owner's Telegram as documents (≤ 45 MB each,
    ciphertext). (delivered, error code)."""
    send = _seams.send_document
    if send is None:
        return False, "not_wired"
    parts: list[Path] = []
    try:
        parts = await asyncio.to_thread(backup.split_for_telegram, path)
        for index, part in enumerate(parts, 1):
            if not await send(part, _caption(path, size, index, len(parts))):
                return False, "not_delivered"
        return True, None
    except Exception as exc:
        log.warning("backup not sent to Telegram", extra={"error": type(exc).__name__})
        return False, type(exc).__name__
    finally:
        backup.remove_parts(parts)


async def backup_job(*, now: datetime | None = None) -> backup.BackupResult:
    """Nightly encrypted pg_dump (06 §4.4), then the copy to Telegram.

    No recipient → nothing is written and nothing beats: the health sweep
    says ``backup_unconfigured``. Otherwise the ``backup`` heartbeat says how
    tonight went — ``ok=false`` is the critical ``backup_failed`` — and a good
    file's row counts are kept for the weekly drill."""
    now = now or datetime.now(settings.tz)
    result = await backup.create_backup(
        now=now, count_tables=restore_drill.COUNTED_TABLES
    )
    if result.error == "no_recipient":
        log.warning("backup skipped: BACKUP_AGE_RECIPIENT is empty")
        return result
    try:
        folder = backup.backup_dir()
        # Pieces left by an upload the container died in the middle of.
        backup.remove_parts(sorted(folder.glob(f"*{backup.BACKUP_SUFFIX}.part[0-9]*")))
    except OSError:
        pass
    detail: dict[str, Any] = {
        "ok": result.ok,
        "path": str(result.path) if result.path is not None else None,
        "size": result.size,
        "sent": False,
        "error": result.error,
        "pruned": result.pruned,
    }
    if result.ok and result.path is not None:
        try:
            # Counted in the dump's own snapshot (backup.create_backup), so
            # the drill compares the file with exactly what it holds.
            async with session_scope() as session:
                await health.beat(
                    session,
                    health.BACKUP_COUNTS_COMPONENT,
                    detail={"path": str(result.path), "counts": result.counts},
                    now=now,
                )
        except Exception as exc:
            log.error(
                "backup row counts not recorded", extra={"error": type(exc).__name__}
            )
        if settings.backup_to_telegram:
            sent, error = await _send_backup(result.path, result.size)
            detail["sent"] = sent
            if not sent:
                detail["send_error"] = error
    try:
        async with session_scope() as session:
            await health.beat(session, health.BACKUP_COMPONENT, detail=detail, now=now)
    except Exception as exc:
        log.error("backup heartbeat not written", extra={"error": type(exc).__name__})
    return result


async def restore_drill_job() -> restore_drill.DrillResult | None:
    """Weekly: restore the newest backup into a scratch database (06 §4.4)."""
    if not settings.restore_drill_enabled:
        return None
    newest = await backup.newest_backup()
    expected: dict[str, int] | None = None
    async with session_scope() as session:
        rows = await health.beats(session)
        counted = rows.get(health.BACKUP_COUNTS_COMPONENT)
        detail = dict(counted.detail or {}) if counted is not None else {}
        if newest is not None and detail.get("path") == str(newest):
            counts = detail.get("counts")
            expected = dict(counts) if isinstance(counts, dict) else None
    result = await restore_drill.run(expected=expected)
    if result.skipped == "no_backup":
        return result
    async with session_scope() as session:
        await health.beat(session, health.RESTORE_DRILL_COMPONENT, detail=result.detail())
    log.info(
        "restore drill finished",
        extra={"ok": result.ok, "skipped": result.skipped, "seconds": result.seconds},
    )
    return result


def last_scheduled(at: time, now: datetime) -> datetime:
    """The most recent moment ``at`` has already passed (today, else yesterday)."""
    local = now.astimezone(settings.tz)
    todays = datetime.combine(local.date(), at, tzinfo=settings.tz)
    return todays if todays <= local else todays - timedelta(days=1)


def backup_is_missing(now: datetime, directory: Path | None = None) -> bool:
    """No backup written since the last scheduled BACKUP_TIME. An absolute
    «older than 25 hours» misses the common case: the VPS is down at 03:45
    and back at 04:10, when yesterday's file is 24.5 hours old — the run
    would be skipped and the gap would stretch to 48 hours (MIYA)."""
    due = last_scheduled(settings.backup_time_parsed, now)
    newest = max(
        (
            stamp
            for path in backup.current_backups(directory)
            if (stamp := backup.backup_stamp(path)) is not None
        ),
        default=None,
    )
    return newest is None or newest < due


async def catch_up(*, now: datetime | None = None) -> None:
    """Run what the scheduler missed while the worker was down. The jobstore
    is in memory, so a deploy spanning BACKUP_TIME loses that night's backup.
    Every branch is guarded: a startup path that can raise is a crash loop
    under ``restart: unless-stopped``. (The digest's catch-up is T-35's.)"""
    now = now or datetime.now(settings.tz)
    if not settings.backup_age_recipient:
        return
    try:
        missing = backup_is_missing(now)
    except OSError:
        log.error("catch-up could not read the backup directory")
        return
    if missing:
        log.info("catch-up: no backup since the last scheduled time; running one")
        try:
            await backup_job(now=now)
        except Exception as exc:
            log.error("catch-up backup failed", extra={"error": type(exc).__name__})


async def source_crons() -> dict[str, str | None]:
    """``sources.key`` → ``cron``: the owner's cadence, read at start."""
    async with session_scope() as session:
        rows = await session.execute(sa.select(Source.key, Source.cron))
        return {str(key): cron for key, cron in rows.all()}


# --- the process ---------------------------------------------------------------------


def wire() -> None:
    """The hooks every worker process registers — the scheduler and a one-off
    run alike (``make backup``): the Anthropic switch costs.ask reads, the
    cap notifier with health's quiet hours, and the suppression freshness
    probe health judges ``suppression_stale`` by. The owner bot's seams (the
    notifier, the backup sender, T-20) are wired through the same setters."""
    costs.set_anthropic_health(health.anthropic_down_until)
    costs.set_cap_notifier(
        _seams.notify if _seams.notify is not _not_wired else None,
        hold=health.in_quiet_hours,
    )
    # suppression_stale is judged through the feed sync's own freshness rule
    # (T-09); unregistered, the key stays unjudged (06 §4.2).
    erp_feeds.register_health_probe()


async def backup_now(*, now: datetime | None = None) -> int:
    """``make backup`` (06 §3.3, §4.6): the nightly job run once by hand — the
    same ``backup`` heartbeat, the drill's counts and the Telegram copy, so a
    good manual run clears ``backup_failed`` (the runbook's «verify: backup
    beat ok=true»). 0 when the file was written, else 1 (``make update``
    stops)."""
    wire()
    try:
        result = await backup_job(now=now)
    finally:
        await engine.dispose()
    # The JSON log line is what the operator reads (create_backup logs the
    # file and its size, or the failure).
    log.info(
        "manual backup finished",
        extra={"ok": result.ok, "bytes": result.size, "error": result.error},
    )
    return 0 if result.ok else 1


async def run() -> None:
    setup_logging("worker")
    wire()
    try:
        crons = await source_crons()
    except Exception as exc:
        log.error(
            "sources.cron not read; seeded crons used",
            extra={"error": type(exc).__name__},
        )
        crons = {}
    scheduler = build_scheduler(source_crons=crons)
    scheduler.add_listener(
        functools.partial(on_job_event, loop=asyncio.get_running_loop()),
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
    )
    scheduler.start()
    registered = [job.id for job in scheduler.get_jobs()]
    log.info(
        "worker started",
        extra={"timezone": settings.timezone, "jobs": len(registered)},
    )
    try:
        await prune_job_heartbeats([health.JOB_PREFIX + job_id for job_id in registered])
    except Exception as exc:
        log.warning(
            "stale job heartbeats not pruned", extra={"error": type(exc).__name__}
        )
    # The first beat goes out before the catch-up, which can take minutes (a
    # backup): the bot's watchdog must not read that as silence.
    try:
        await heartbeat_job(scheduler)
    except Exception as exc:
        log.error("startup heartbeat failed", extra={"error": type(exc).__name__})
    try:
        await catch_up()
    except Exception as exc:
        log.error("startup catch-up failed", extra={"error": type(exc).__name__})
    try:
        await idle_until_stopped()
    finally:
        scheduler.shutdown(wait=False)
        if _shared.fetcher is not None:
            await _shared.fetcher.aclose()
        await engine.dispose()
        log.info("worker stopped")


def main(argv: list[str] | None = None) -> int:
    """``python -m clientscrapper.worker.main`` runs the scheduler;
    ``… main backup`` runs one backup (``make backup``)."""
    args = sys.argv[1:] if argv is None else argv
    if args == ["backup"]:
        setup_logging("worker")
        return asyncio.run(backup_now())
    if args:
        raise SystemExit(f"unknown arguments: {args} (only «backup»)")
    return run_main(run)


if __name__ == "__main__":
    raise SystemExit(main())
