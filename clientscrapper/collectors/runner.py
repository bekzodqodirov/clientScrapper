"""The collector runner (03 §1.2): one call per scheduled job.

``run_collector`` reads the collector's ``sources`` row and skips (a
``collector_runs`` row with ``status='skipped'`` and the reason in
``meta.note``) when ``COLLECTORS_ENABLED=false``, the row is disabled, its
``paused_until`` is ahead, or ``/toxtat`` holds ``runtime_flags('paused')``.
Otherwise it claims the run (``ux_collector_runs_one_running``; a ``running``
row older than ``STALE_RUN`` is closed as ``failed``/``abandoned`` first),
builds the ``Budget``, beats ``collector:<key>[.variant]``, binds the shared
``Fetcher`` to the run and calls ``collect``.

Then, for each observation: the evidence row (and file) for the bytes it was
parsed from — the body the run's Fetcher handed out, or for an API row
(``raw_sha256=""``) the canonical JSON of the observation itself — unless the
same bytes at the same URL were stored by an earlier run (``unchanged``:
nothing new is written); one ``observations(status='new')`` row per
observation, deduplicated on (source, source_ref, sha256) within 24 h, its
``parsed`` from ``normalise.to_observation_json``. The ``collector_runs`` row
carries the 02 §2.2 counters; ``unchanged``, ``skipped_robots``, ``note`` and
the rest go into ``meta``.

Endings: ``ok``; ``partial`` when a page/dollar budget, a cost cap or the
deadline stopped the run; ``failed`` + ``error='host_blocked'`` when a host
breaker tripped — the source is paused 6 h (``paused_until`` for the runner,
``breaker_until`` for the health problem ``collector_blocked``, both with
reason ``host_blocked:<host>``); ``failed`` + the error class for anything
else. What a stopped collector put in ``Collector.found`` is still written.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors.base import Budget, Collector, Observation
from clientscrapper.config import settings
from clientscrapper.db.models import CollectorRun, Evidence, RuntimeFlag, Source, UsageLog
from clientscrapper.db.models import Observation as ObservationRow
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import evidence as evidence_store
from clientscrapper.services import health
from clientscrapper.services.http import (
    BLOCKED_ERROR,
    BLOCKED_PROBLEM,
    BREAKER_PAUSE,
    BudgetExhausted,
    CostGate,
    Fetcher,
    FetchResult,
    HostPaused,
    StoredCopy,
)
from clientscrapper.services.normalise import NOT_OBSERVATIONS, to_observation_json

log = logging.getLogger(__name__)

DEFAULT_DEADLINE: Final = timedelta(hours=2)
# A 'running' row older than this belongs to a process that died mid-run.
STALE_RUN: Final = timedelta(hours=6)
DEDUPE_WINDOW: Final = timedelta(hours=24)
# Attribution-only rows whose spend counts against their parent's dollar
# budget (02 §2.1): costs.ask records the sub-source a call was made for.
SUBSOURCE_PREFIXES: Final = {
    "serp_layer": ("search_",),
    "directories": ("directories_",),
    "maps": ("maps_",),
}
ABANDONED: Final = "abandoned"

SessionFactory = Callable[[], AsyncSession]
GateFactory = Callable[[AsyncSession, int, datetime], CostGate]


@dataclass(slots=True)
class RunReport:
    source_key: str
    component: str
    status: str
    run_id: int | None = None
    error: str | None = None
    counters: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


class RunLedger:
    """The run's ``FetchHooks``: the stored copy for a conditional GET, and
    every body the Fetcher handed out, by sha256, for the evidence step.

    Lookups open their own short session: a collector may fetch concurrently
    while it holds the run's session."""

    def __init__(self, session_factory: SessionFactory, root: Path | None) -> None:
        self._factory = session_factory
        self._root = root
        self.bodies: dict[str, FetchResult] = {}

    async def stored_copy(self, url: str) -> StoredCopy | None:
        async with self._factory() as session:
            row = await evidence_store.latest_stored(session, url)
        if row is None:
            return None
        body = evidence_store.open_raw(row, root=self._root)
        if body is None:
            return None
        meta = row.meta or {}
        return StoredCopy(
            sha256=row.sha256,
            body=body,
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
        )

    def fetched(self, result: FetchResult) -> None:
        self.bodies.setdefault(result.sha256, result)


@dataclass(slots=True)
class _Written:
    new_evidence: int = 0
    new_observations: int = 0
    unchanged_observations: int = 0
    duplicates: int = 0
    serp_hits: int = 0
    truncated: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    by_source: dict[str, dict[str, int]] = field(default_factory=dict)

    def bump(self, source: str, what: str) -> None:
        counts = self.by_source.setdefault(source, {"evidence": 0, "observations": 0})
        counts[what] += 1


@dataclass(slots=True)
class _Group:
    url: str
    sha256: str
    body: bytes
    content_type: str
    meta: dict[str, Any]
    fetched_at: datetime
    members: list[Observation] = field(default_factory=list)


def _tashkent_day_start(now: datetime) -> datetime:
    local = now.astimezone(settings.tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


async def global_pause(session: AsyncSession) -> bool:
    """``policy.paused()``: ``/toxtat`` set ``runtime_flags('paused')``."""
    row = await session.get(RuntimeFlag, "paused", populate_existing=True)
    return row is not None and row.value not in (None, False)


async def skip_reason(session: AsyncSession, source: Source, now: datetime) -> str | None:
    if not settings.collectors_enabled:
        return "collectors_disabled"
    if await global_pause(session):
        return "global_pause"
    if not source.enabled:
        return "source_disabled"
    if source.paused_until is not None and source.paused_until > now:
        return "paused"
    return None


async def build_budget(
    session: AsyncSession, source: Source, *, now: datetime, deadline: timedelta
) -> Budget:
    """03 §1.2: pages = the row's daily budget minus pages used today (and the
    global COLLECTOR_DAILY_PAGE_BUDGET minus every source's pages); usd = the
    row's daily cap minus today's spend of the row and its sub-sources."""
    day = _tashkent_day_start(now)
    pages_today = sa.func.coalesce(sa.func.sum(CollectorRun.pages), 0)
    used_here = await session.scalar(
        sa.select(pages_today).where(
            CollectorRun.source_key == source.key, CollectorRun.started_at >= day
        )
    )
    used_all = await session.scalar(
        sa.select(pages_today).where(CollectorRun.started_at >= day)
    )
    pages = min(
        source.daily_page_budget - int(used_here or 0),
        settings.collector_daily_page_budget - int(used_all or 0),
    )
    keys = [UsageLog.source_key == source.key]
    keys += [
        UsageLog.source_key.like(f"{prefix}%")
        for prefix in SUBSOURCE_PREFIXES.get(source.key, ())
    ]
    spent = await session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(UsageLog.cost_usd), 0)).where(
            sa.or_(*keys),
            UsageLog.created_at >= day,
            sa.or_(UsageLog.model.is_(None), UsageLog.model.not_like("refused:%")),
        )
    )
    usd = Decimal(source.daily_cost_cap_usd) - Decimal(spent or 0)
    return Budget(
        pages=max(pages, 0),
        usd=max(usd, Decimal("0")),
        deadline=now + deadline,
    )


def _canonical_body(obs: Observation) -> bytes:
    """The evidence body of an API row: the observation itself, without the
    fetch time, so the same row seen twice hashes the same."""
    data = dataclasses.asdict(obs)
    data.pop("fetched_at", None)
    data.pop("raw_sha256", None)
    return json.dumps(
        data, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")
    ).encode("utf-8")


def _fetch_meta(result: FetchResult) -> dict[str, Any]:
    meta: dict[str, Any] = {"final_url": result.final_url, "status": result.status}
    if result.etag:
        meta["etag"] = result.etag
    if result.last_modified:
        meta["last_modified"] = result.last_modified
    if result.truncated:
        meta["truncated"] = True
    return meta


async def _recently_seen(
    session: AsyncSession, obs: Observation, sha256: str, *, now: datetime
) -> bool:
    stmt = (
        sa.select(ObservationRow.id)
        .join(Evidence, Evidence.id == ObservationRow.evidence_id)
        .where(
            ObservationRow.source_key == obs.source,
            ObservationRow.parsed["source_ref"].astext == obs.source_ref,
            Evidence.sha256 == sha256,
            ObservationRow.created_at > now - DEDUPE_WINDOW,
        )
        .limit(1)
    )
    return (await session.scalar(stmt)) is not None


async def write_observations(
    session: AsyncSession,
    observations: list[Observation],
    *,
    ledger: RunLedger,
    run_id: int,
    collector_version: str,
    now: datetime,
    root: Path | None,
) -> _Written:
    """Evidence + observation rows for one run's findings (no commit)."""
    written = _Written()
    known = set(await session.scalars(sa.select(Source.key)))
    groups: dict[tuple[str, str], _Group] = {}
    for obs in observations:
        if obs.kind in NOT_OBSERVATIONS - {"serp_hit"}:
            written.dropped["not_an_observation"] += 1
            continue
        if obs.source not in known:
            written.dropped["unknown_source"] += 1
            continue
        if obs.raw_sha256:
            result = ledger.bodies.get(obs.raw_sha256)
            if result is None:
                written.dropped["no_evidence"] += 1
                continue
            key = (result.url, result.sha256)
            if key not in groups:
                groups[key] = _Group(
                    url=result.url,
                    sha256=result.sha256,
                    body=result.body,
                    content_type=result.content_type or "text/html",
                    meta=_fetch_meta(result),
                    fetched_at=result.fetched_at,
                )
        else:
            body = _canonical_body(obs)
            sha = hashlib.sha256(body).hexdigest()
            key = (obs.url, sha)
            if key not in groups:
                groups[key] = _Group(
                    url=obs.url,
                    sha256=sha,
                    body=body,
                    content_type="application/json",
                    meta={},
                    fetched_at=obs.fetched_at,
                )
        groups[key].members.append(obs)

    seen: set[tuple[str, str, str]] = set()
    for group in groups.values():
        existing = await evidence_store.find(session, group.url, group.sha256)
        if existing is not None and existing.collector_run_id != run_id:
            # Same bytes at the same URL as an earlier run: nothing new to say.
            written.unchanged_observations += len(group.members)
            continue
        first = group.members[0]
        single = len(group.members) == 1
        row = existing or await evidence_store.store(
            session,
            source_key=first.source,
            url=group.url,
            body=group.body,
            content_type=group.content_type,
            fetched_at=group.fetched_at,
            source_ref=first.source_ref if single else None,
            excerpt=first.excerpt or None,
            lang=first.lang,
            published_at=first.published_at if single else None,
            collector_run_id=run_id,
            meta=group.meta,
            root=root,
        )
        if existing is None:
            written.new_evidence += 1
            written.bump(first.source, "evidence")
            if row.meta.get("truncated"):
                written.truncated += 1
        for ordinal, obs in enumerate(group.members):
            if obs.kind == "serp_hit":
                written.serp_hits += 1
                continue
            mark = (obs.source, obs.source_ref, group.sha256)
            if mark in seen or await _recently_seen(session, obs, group.sha256, now=now):
                written.duplicates += 1
                continue
            seen.add(mark)
            session.add(
                ObservationRow(
                    evidence_id=row.id,
                    ordinal=ordinal,
                    source_key=obs.source,
                    status="new",
                    parsed=to_observation_json(obs, collector_version=collector_version),
                )
            )
            written.new_observations += 1
            written.bump(obs.source, "observations")
    await session.flush()
    return written


def _meta(
    *,
    variant: str | None,
    fetcher: Fetcher,
    written: _Written,
    note: str | None = None,
) -> dict[str, Any]:
    stats = fetcher.stats
    meta: dict[str, Any] = {
        "unchanged": stats.unchanged,
        "skipped_robots": stats.skipped_robots,
        # Evidence rows stored without their file (02 §3: over 2 MB, or cut at
        # the 5 MB fetch cap); fetch_truncated counts cut answers of any kind.
        "truncated": written.truncated,
    }
    extras: dict[str, Any] = {
        "variant": variant,
        "fetch_truncated": stats.truncated,
        "unchanged_observations": written.unchanged_observations,
        "note": note,
        "captcha": stats.captcha,
        "duplicates": written.duplicates,
        "serp_hits": written.serp_hits,
        "dropped": dict(written.dropped),
        "exhausted": stats.exhausted,
        "blocked_hosts": list(stats.blocked_hosts),
        "problem": BLOCKED_PROBLEM if stats.blocked_hosts else None,
        "sources": written.by_source,
    }
    meta.update({k: v for k, v in extras.items() if v})
    return meta


async def _skip(
    session: AsyncSession,
    key: str,
    component: str,
    *,
    now: datetime,
    note: str,
    variant: str | None,
) -> RunReport:
    meta: dict[str, Any] = {"note": note}
    if variant:
        meta["variant"] = variant
    run = CollectorRun(
        source_key=key, started_at=now, finished_at=now, status="skipped", meta=meta
    )
    session.add(run)
    await session.commit()
    log.info("collector %s skipped: %s", key, note, extra={"source": key, "reason": note})
    return RunReport(key, component, "skipped", run_id=run.id, meta=meta)


async def run_collector(
    collector_cls: type[Collector],
    *,
    fetcher: Fetcher,
    now: datetime,
    variant: str | None = None,
    session_factory: SessionFactory = SessionLocal,
    cost_gate_for: GateFactory | None = None,
    serp: object | None = None,
    evidence_root: Path | None = None,
    deadline: timedelta = DEFAULT_DEADLINE,
    clock: Callable[[], datetime] | None = None,
) -> RunReport:
    """One run of ``collector_cls`` at ``now``; see the module docstring.
    ``clock`` gives the finish time (default: the real clock, never before
    ``now``). ``cost_gate_for(session, run_id, now)`` binds ``costs.ask``."""
    key = collector_cls.key
    component = health.collector_component(key, variant)
    version = f"{key}@{collector_cls.version}"

    def finished() -> datetime:
        stamp = clock() if clock is not None else datetime.now(settings.tz)
        return max(stamp, now)

    async with session_factory() as session:
        source = await session.get(Source, key)
        if source is None:
            raise LookupError(f"no sources row {key!r}")
        reason = await skip_reason(session, source, now)
        if reason is not None:
            return await _skip(
                session, key, component, now=now, note=reason, variant=variant
            )

        await session.execute(
            sa.update(CollectorRun)
            .where(
                CollectorRun.source_key == key,
                CollectorRun.status == "running",
                CollectorRun.started_at < now - STALE_RUN,
            )
            .values(status="failed", error=ABANDONED, finished_at=now)
        )
        run = CollectorRun(
            source_key=key,
            started_at=now,
            status="running",
            meta={"variant": variant} if variant else {},
        )
        session.add(run)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return await _skip(
                session, key, component, now=now, note="already_running", variant=variant
            )
        run_id = run.id
        budget = await build_budget(session, source, now=now, deadline=deadline)
        await health.beat(
            session, component, detail={"phase": "start", "run_id": run_id}, now=now
        )
        await session.commit()

        ledger = RunLedger(session_factory, evidence_root)
        gate = cost_gate_for(session, run_id, now) if cost_gate_for is not None else None
        run_fetcher = fetcher.bind(hooks=ledger, cost_gate=gate)
        collector = collector_cls(run_fetcher, serp)
        collector.variant = variant

        status, error = "ok", None
        try:
            observations = await collector.collect(session, budget)
        except HostPaused:
            observations, status, error = list(collector.found), "failed", BLOCKED_ERROR
        except BudgetExhausted:
            observations, status = list(collector.found), "partial"
        except Exception as exc:
            await session.rollback()
            observations, status, error = [], "failed", type(exc).__name__
            log.warning(
                "collector %s failed: %s",
                key,
                error,
                extra={"source": key, "run_id": run_id, "error": error},
            )
        blocked = list(run_fetcher.stats.blocked_hosts)
        if blocked:
            status, error = "failed", BLOCKED_ERROR
        elif status == "ok" and run_fetcher.stats.exhausted is not None:
            status = "partial"

        try:
            written = await write_observations(
                session,
                observations,
                ledger=ledger,
                run_id=run_id,
                collector_version=version,
                now=now,
                root=evidence_root,
            )
        except Exception as exc:
            await session.rollback()
            written = _Written()
            status, error = "failed", error or type(exc).__name__
            log.warning(
                "collector %s: writing its findings failed: %s",
                key,
                type(exc).__name__,
                extra={"source": key, "run_id": run_id},
            )

        stats = run_fetcher.stats
        counters: dict[str, Any] = {
            "requests": stats.requests,
            "pages": stats.pages,
            "http_403": stats.http_403,
            "http_429": stats.http_429,
            "new_evidence": written.new_evidence,
            "new_observations": written.new_observations,
            "new_companies": 0,
            "new_signals": 0,
            "cost_usd": stats.cost_usd,
        }
        meta = _meta(variant=variant, fetcher=run_fetcher, written=written)
        end = finished()
        await session.execute(
            sa.update(CollectorRun)
            .where(CollectorRun.id == run_id)
            .values(finished_at=end, status=status, error=error, meta=meta, **counters)
        )
        source_values: dict[str, Any] = {"last_run_at": now}
        if blocked:
            until = end + BREAKER_PAUSE
            reason = f"{BLOCKED_ERROR}:{blocked[0]}"
            source_values.update(
                paused_until=until,
                paused_reason=reason,
                breaker_until=until,
                breaker_reason=reason,
            )
        await session.execute(
            sa.update(Source).where(Source.key == key).values(**source_values)
        )
        detail: dict[str, Any] = {
            "phase": "end",
            "run_id": run_id,
            "status": status,
            "error": error,
            **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in counters.items()},
            "unchanged": meta["unchanged"],
            "skipped_robots": meta["skipped_robots"],
        }
        if blocked:
            detail["problem"] = BLOCKED_PROBLEM
            detail["blocked_hosts"] = blocked
        await health.beat(session, component, detail=detail, now=end)
        await session.commit()

    log.info(
        "collector %s %s",
        key,
        status,
        extra={"source": key, "run_id": run_id, "status": status, "error": error},
    )
    return RunReport(key, component, status, run_id, error, counters, meta)
