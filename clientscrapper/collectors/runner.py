"""The collector runner (03 §1.2): one call per scheduled job.

``run_collector`` reads the collector's ``sources`` row and skips (a
``collector_runs`` row with ``status='skipped'`` and the reason in
``meta.note``) when ``COLLECTORS_ENABLED=false``, the row is disabled, its
``paused_until`` is ahead, or ``/toxtat`` holds ``runtime_flags('paused')``.
Otherwise it claims the run (``ux_collector_runs_one_running``; a ``running``
row whose deadline (``meta.deadline_at``) passed more than ``STALE_MARGIN``
ago belongs to a process that died, and is closed as ``failed``/``abandoned``
first), builds the ``Budget``, beats ``collector:<key>[.variant]``, binds the
shared ``Fetcher`` to the run and calls ``collect``. A run that is cancelled
(worker shutdown, a deploy) closes its own row ``failed``/``cancelled`` from a
fresh session before the cancellation goes on, so the next run is not
refused as ``already_running``.

Then, for each observation: the evidence row (and file) for the bytes it was
parsed from — the body the run's Fetcher handed out, or for an API row
(``raw_sha256=""``) the canonical JSON of the observation itself — unless the
same bytes at the same URL were stored by an earlier run (``unchanged``:
nothing new is written); one ``observations(status='new')`` row per
observation, deduplicated on (source, source_ref, sha256) within 24 h, its
``parsed`` from ``normalise.to_observation_json`` (storable: no NUL, no
NaN). Every stored URL is ``urlsafe.strip_secrets``'d (an API key in a query
never reaches a row or a backup). Each evidence group is written in its own
savepoint, and each observation in one inside it: a group the database
refuses is dropped and counted (``meta.dropped.bad_evidence``), an
observation it refuses likewise (``bad_observation``), the rest of the page
and of the run is kept, and the run ends ``partial``. The ``collector_runs`` row
carries the 02 §2.2 counters; ``unchanged``, ``skipped_robots``, ``note`` and
the rest go into ``meta``.

Bytes a collector read without the Fetcher (``Collector.supply``: the rows the
Telegram reader stored, 03 §2.3) are kept the same way; an evidence row such
a collector names that another process wrote outside any run and that holds
no observation yet (the reader's weekly channel about) is adopted — its
observations are this run's to write. Then ``Collector.after_write`` hears
where every observation landed (``Placed``), in a savepoint before the commit:
a failure there costs its own writes only, is counted
(``meta.dropped.after_write``) and ends the run ``partial``.

Endings: ``ok``; ``partial`` when the database refused a row, or when a
page/dollar budget, a cost cap (or any
``costs.ask`` refusal, ``meta.refused`` naming its code — through the Fetcher
or from a paid call the collector made itself, ``costs.ModelUnavailable``) or
the deadline stopped the run; ``failed`` + ``error='host_blocked'`` when a host
breaker tripped — the source is paused 6 h (``paused_until`` for the runner,
``breaker_until`` for the health problem ``collector_blocked``, both with
reason ``host_blocked:<host>``); ``failed`` + the error class for anything
else. What a stopped collector put in ``Collector.found`` is still written.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors.base import (
    Budget,
    Collector,
    Observation,
    Placed,
    Supplied,
)
from clientscrapper.config import settings
from clientscrapper.db.models import CollectorRun, Evidence, Source
from clientscrapper.db.models import Observation as ObservationRow
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import costs, health
from clientscrapper.services import evidence as evidence_store
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
from clientscrapper.services.urlsafe import strip_secrets
from clientscrapper.services.usage import Operation, Priced

log = logging.getLogger(__name__)

DEFAULT_DEADLINE: Final = timedelta(hours=2)
# A 'running' row this long past its deadline belongs to a process that died
# (the Fetcher refuses every request after the deadline, so a live run ends
# soon after it). Rows without meta.deadline_at count from started_at + 2 h.
STALE_MARGIN: Final = timedelta(minutes=30)
DEDUPE_WINDOW: Final = timedelta(hours=24)
# How often a run in progress is looked after (``_watch_run``): its
# ``pages``/``requests`` are written onto its ``running`` row, so a run killed
# mid-crawl (OOM, kill -9, a VPS reboot — nothing runs its close) still counts
# what it fetched (build_budget sums ``collector_runs.pages`` and would
# otherwise hand the next run today's whole page budget again); and
# ``/toxtat`` (``runtime_flags('paused')``) is re-read, so a run already going
# fetches nothing more once the owner paused (06 §1.2 «collectors fetch
# nothing»). 07 §4.1 T-08, both carried from T-05.
RUN_WATCH: Final = timedelta(seconds=10)
GLOBAL_PAUSE: Final = "global_pause"
ABANDONED: Final = "abandoned"
CANCELLED: Final = "cancelled"
# The database refused this row: skip it (or its group), keep the run.
_REFUSED: Final = (IntegrityError, DataError, ValueError)
# meta.dropped keys for rows the database refused; either ends the run
# 'partial' — what it found is not all stored.
BAD_EVIDENCE: Final = "bad_evidence"
BAD_OBSERVATION: Final = "bad_observation"
# meta.dropped key for a Collector.after_write that raised: its writes are
# undone, the run's rows are kept, the run ends 'partial'.
AFTER_WRITE: Final = "after_write"

SessionFactory = Callable[[], AsyncSession]
GateFactory = Callable[[AsyncSession, int], CostGate]


class UnattributedCall(ValueError):
    """A paid call attributed to a ``source_key`` that is neither the run's
    sources row nor one of its sub-sources: ``usage_log.source_key`` would be
    NULL or another source's, and the run's own dollar budget
    (``costs.source_budget_usd``) would never see the spend."""


async def _attributable(run_id: int) -> frozenset[str]:
    """The keys a run may attribute a paid call to: its own sources row and
    the existing rows under its sub-source prefixes (``costs.SUBSOURCE_
    PREFIXES``). Read in a short session of its own: the run's session may
    be in use by the collector."""
    async with SessionLocal() as own:
        key = await own.scalar(
            sa.select(CollectorRun.source_key).where(CollectorRun.id == run_id)
        )
        known = set(await own.scalars(sa.select(Source.key)))
    if key is None:
        return frozenset()
    prefixes = costs.SUBSOURCE_PREFIXES.get(key, ())
    return frozenset({key, *(k for k in known if prefixes and k.startswith(prefixes))})


def ask_gate(session: AsyncSession, run_id: int) -> CostGate:
    """The run's cost gate: ``costs.ask`` with the run's session and
    ``collector_run_id`` bound (04 §8); the Fetcher supplies the rest per call
    (operation, estimate, call, price, the sub-source it was made for, and
    ``now`` — its wall clock at that request, never the run's start: a run
    started at 23:50 that pays at 00:50 spends the new day's cap, and a
    Yandex call at 10:29 is outside its window however early the run began).

    ``functools.partial(costs.ask, session, collector_run_id=…)`` in effect,
    spelled out because mypy does not hold a partial to a Protocol: written
    as a call, both signatures are checked, so a change to either side of the
    seam is a type error (tests/collectors/test_cost_gate.py runs it against
    the database). A call attributed to anything but the run's row or one of
    its sub-sources is refused (``UnattributedCall``) before ask is asked."""

    allowed: frozenset[str] | None = None

    async def gate(
        *,
        operation: Operation,
        est_usd: Decimal,
        call: Callable[[], Awaitable[FetchResult]],
        price: Callable[[FetchResult], Priced],
        source_key: str,
        now: datetime,
    ) -> FetchResult:
        nonlocal allowed
        if allowed is None:
            allowed = await _attributable(run_id)
        if source_key not in allowed:
            # Before ask: nothing is claimed or sent, and the run fails on it
            # (a collector's bug, not a budget stop).
            raise UnattributedCall(f"{source_key!r} is not this run's source")
        return await costs.ask(
            session,
            operation=operation,
            est_usd=est_usd,
            call=call,
            price=price,
            now=now,
            source_key=source_key,
            collector_run_id=run_id,
        )

    return gate


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
            row = await evidence_store.latest_stored(session, strip_secrets(url))
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
    placed: list[Placed] = field(default_factory=list)

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
    # Bytes the collector supplied (Collector.supply), not a fetch: an
    # evidence row stored outside any run may be adopted (_write_group).
    supplied: bool = False


async def global_pause(session: AsyncSession) -> bool:
    """``policy.paused()``: ``/toxtat`` set ``runtime_flags('paused')``
    (``costs.global_pause``, the flag's one reading)."""
    return await costs.global_pause(session)


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
    row's daily cap minus today's spend of the row and its sub-sources
    (``costs.source_budget_usd``, the rule's one home)."""
    day = costs.day_start(now)  # the Tashkent business day, as costs counts it
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
    usd = await costs.source_budget_usd(session, source_key=source.key, now=now)
    return Budget(pages=max(pages, 0), usd=usd, deadline=now + deadline)


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
    meta: dict[str, Any] = {
        "final_url": strip_secrets(result.final_url),
        "status": result.status,
    }
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


def _merge(into: _Written, part: _Written) -> None:
    into.new_evidence += part.new_evidence
    into.new_observations += part.new_observations
    into.unchanged_observations += part.unchanged_observations
    into.duplicates += part.duplicates
    into.serp_hits += part.serp_hits
    into.truncated += part.truncated
    into.dropped.update(part.dropped)
    into.placed.extend(part.placed)
    for source, counts in part.by_source.items():
        mine = into.by_source.setdefault(source, {"evidence": 0, "observations": 0})
        for what, n in counts.items():
            mine[what] += n


async def _adoptable(session: AsyncSession, evidence: Evidence) -> bool:
    """An evidence row another process wrote outside any run (the Telegram
    reader's weekly about, 03 §2.3) that no observation cites yet."""
    if evidence.collector_run_id is not None:
        return False
    cited = await session.scalar(
        sa.select(ObservationRow.id)
        .where(ObservationRow.evidence_id == evidence.id)
        .limit(1)
    )
    return cited is None


async def _write_group(
    session: AsyncSession,
    group: _Group,
    *,
    seen: set[tuple[str, str, str]],
    run_id: int,
    collector_version: str,
    now: datetime,
    root: Path | None,
) -> _Written:
    written = _Written()
    existing = await evidence_store.find(session, group.url, group.sha256)
    if (
        existing is not None
        and existing.collector_run_id != run_id
        and not (group.supplied and await _adoptable(session, existing))
    ):
        # Same bytes at the same URL as an earlier run: nothing new to say.
        if group.body:
            evidence_store.keep_raw(session, existing, group.body, root=root)
        written.unchanged_observations += len(group.members)
        written.placed.extend(
            Placed(obs, existing.id, None)
            for obs in group.members
            if obs.kind != "serp_hit"
        )
        return written
    if existing is None and not group.body and group.supplied:
        # A stored row the collector named is gone: nothing to cite.
        written.dropped["no_evidence"] += len(group.members)
        return written
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
            written.placed.append(Placed(obs, row.id, None))
            continue
        try:
            # Its own savepoint: a row the database refuses costs that row,
            # not the page's other observations.
            async with session.begin_nested():
                observation = ObservationRow(
                    evidence_id=row.id,
                    ordinal=ordinal,
                    source_key=obs.source,
                    status="new",
                    parsed=to_observation_json(obs, collector_version=collector_version),
                )
                session.add(observation)
                await session.flush()
        except _REFUSED as exc:
            written.dropped[BAD_OBSERVATION] += 1
            log.warning(
                "observation refused: %s",
                type(exc).__name__,
                extra={"source": obs.source, "run_id": run_id},
            )
            continue
        seen.add(mark)
        written.new_observations += 1
        written.bump(obs.source, "observations")
        written.placed.append(Placed(obs, row.id, observation.id))
    await session.flush()
    return written


async def write_observations(
    session: AsyncSession,
    observations: list[Observation],
    *,
    ledger: RunLedger,
    run_id: int,
    collector_version: str,
    now: datetime,
    root: Path | None,
    supplied: Mapping[tuple[str, str], Supplied] | None = None,
) -> _Written:
    """Evidence + observation rows for one run's findings (no commit). Each
    evidence group is one savepoint: a group the database refuses is dropped
    and counted, never the whole run. ``supplied`` are the bytes the collector
    read without the Fetcher (``Collector.supply``), by ``(url, sha256)``: an
    observation finds its own bytes at its own URL, so two messages with the
    same words are two evidence rows, each at its own link."""
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
        given = (
            (supplied or {}).get((obs.url, obs.raw_sha256)) if obs.raw_sha256 else None
        )
        if given is not None and obs.raw_sha256 not in ledger.bodies:
            url, sha, body = strip_secrets(given.url), given.sha256, given.body or b""
            content_type = given.content_type or "text/plain"
            meta, fetched_at = dict(given.meta), given.fetched_at
        elif obs.raw_sha256:
            result = ledger.bodies.get(obs.raw_sha256)
            if result is None:
                written.dropped["no_evidence"] += 1
                continue
            url, sha, body = strip_secrets(result.url), result.sha256, result.body
            content_type = result.content_type or "text/html"
            meta, fetched_at = _fetch_meta(result), result.fetched_at
        else:
            body = _canonical_body(obs)
            url, sha = strip_secrets(obs.url), hashlib.sha256(body).hexdigest()
            content_type, meta, fetched_at = "application/json", {}, obs.fetched_at
        if len(url) > evidence_store.URL_MAX:
            # evidence.url holds 2048 characters; such a page cannot be cited.
            written.dropped["url_too_long"] += 1
            continue
        key = (url, sha)
        if key not in groups:
            groups[key] = _Group(
                url, sha, body, content_type, meta, fetched_at, supplied=given is not None
            )
        groups[key].members.append(obs)

    seen: set[tuple[str, str, str]] = set()
    for group in groups.values():
        marks = set(seen)
        try:
            async with session.begin_nested():
                part = await _write_group(
                    session,
                    group,
                    seen=marks,
                    run_id=run_id,
                    collector_version=collector_version,
                    now=now,
                    root=root,
                )
        except _REFUSED as exc:
            written.dropped[BAD_EVIDENCE] += len(group.members)
            log.warning(
                "evidence group refused: %s",
                type(exc).__name__,
                extra={"source": group.members[0].source, "run_id": run_id},
            )
            continue
        seen = marks
        _merge(written, part)
    return written


def _stopped_by(exc: costs.ModelUnavailable) -> tuple[str, str]:
    """(meta.exhausted, meta.refused) for a refusal the collector met outside
    the Fetcher: the same words the Fetcher's own refusals carry."""
    if isinstance(exc, costs.CostCapReached):
        return ("cost_cap" if costs.is_cap(exc.code) else "refused"), exc.code
    # ModelUnavailable's message is a code, never text (costs.py).
    return "model_unavailable", str(exc) or type(exc).__name__


def _meta(
    *,
    variant: str | None,
    fetcher: Fetcher,
    written: _Written,
    note: str | None = None,
    stopped: tuple[str, str] | None = None,
) -> dict[str, Any]:
    stats = fetcher.stats
    exhausted, refused = stopped or (stats.exhausted, stats.refused)
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
        "exhausted": exhausted,
        "refused": refused,
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


@dataclass(slots=True)
class _Watch:
    paused: bool = False


async def _watch_run(
    session_factory: SessionFactory,
    run_id: int,
    fetcher: Fetcher,
    budget: Budget,
    *,
    started: datetime,
    watch: _Watch,
) -> None:
    """Every RUN_WATCH, in a short session of its own: the run's counters
    onto its ``running`` row, and the global pause re-read — once it holds,
    the budget's deadline moves to the run's start, so the Fetcher refuses
    the next request (``BudgetExhausted``) and the run ends ``partial``.
    Cancelled when the run ends; a failed step is a log line and the next
    tick tries again."""
    written = (-1, -1)
    while True:
        await asyncio.sleep(RUN_WATCH.total_seconds())
        stats = fetcher.stats
        counters = (stats.pages, stats.requests)
        try:
            async with session_factory() as session:
                if counters != written:
                    await session.execute(
                        sa.update(CollectorRun)
                        .where(
                            CollectorRun.id == run_id, CollectorRun.status == "running"
                        )
                        .values(pages=stats.pages, requests=stats.requests)
                    )
                    await session.commit()
                    written = counters
                if not watch.paused and await global_pause(session):
                    watch.paused = True
                    budget.deadline = min(budget.deadline, started)
                    log.info("collector run stopped by /toxtat", extra={"run_id": run_id})
        except Exception as exc:
            log.warning(
                "run not looked after",
                extra={"run_id": run_id, "error": type(exc).__name__},
            )


async def _stop(task: asyncio.Future[None]) -> None:
    """Cancel the checkpoint and wait for it to let go of its session, so the
    run's own final write is the last one on the row."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _close_interrupted(
    session_factory: SessionFactory,
    run_id: int,
    fetcher: Fetcher,
    *,
    error: str,
    variant: str | None,
    finished_at: datetime,
) -> None:
    stats = fetcher.stats
    meta: dict[str, Any] = {
        "unchanged": stats.unchanged,
        "skipped_robots": stats.skipped_robots,
        "note": error,
    }
    if variant:
        meta["variant"] = variant
    async with session_factory() as session:
        await session.execute(
            sa.update(CollectorRun)
            .where(CollectorRun.id == run_id, CollectorRun.status == "running")
            .values(
                finished_at=finished_at,
                status="failed",
                error=error,
                meta=meta,
                requests=stats.requests,
                pages=stats.pages,
                http_403=stats.http_403,
                http_429=stats.http_429,
                cost_usd=stats.cost_usd,
            )
        )
        await session.commit()
    log.warning(
        "collector run %s interrupted: %s",
        run_id,
        error,
        extra={"run_id": run_id, "error": error},
    )


async def run_collector(
    collector_cls: type[Collector],
    *,
    fetcher: Fetcher,
    now: datetime,
    variant: str | None = None,
    session_factory: SessionFactory = SessionLocal,
    cost_gate_for: GateFactory | None = ask_gate,
    serp: object | None = None,
    evidence_root: Path | None = None,
    deadline: timedelta = DEFAULT_DEADLINE,
    clock: Callable[[], datetime] | None = None,
) -> RunReport:
    """One run of ``collector_cls`` at ``now``; see the module docstring.
    ``clock`` gives the finish time (default: the real clock, never before
    ``now``). ``cost_gate_for(session, run_id)`` binds ``costs.ask``
    (``ask_gate``; the Fetcher passes each request's own ``now``); ``None``
    leaves the run without a gate, so a paid call is refused
    (``UnmeteredCall``) before anything is sent."""
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
        reason = await skip_reason(session, source, now) or collector_cls.unavailable()
        if reason is not None:
            return await _skip(
                session, key, component, now=now, note=reason, variant=variant
            )

        deadline_at = CollectorRun.meta["deadline_at"].astext.cast(
            sa.DateTime(timezone=True)
        )
        due = sa.func.coalesce(deadline_at, CollectorRun.started_at + DEFAULT_DEADLINE)
        await session.execute(
            sa.update(CollectorRun)
            .where(
                CollectorRun.source_key == key,
                CollectorRun.status == "running",
                due < now - STALE_MARGIN,
            )
            .values(status="failed", error=ABANDONED, finished_at=now)
        )
        claim: dict[str, Any] = {"deadline_at": (now + deadline).isoformat()}
        if variant:
            claim["variant"] = variant
        run = CollectorRun(source_key=key, started_at=now, status="running", meta=claim)
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
        gate = cost_gate_for(session, run_id) if cost_gate_for is not None else None
        run_fetcher = fetcher.bind(hooks=ledger, cost_gate=gate)
        watch = _Watch()
        checkpoint = asyncio.ensure_future(
            _watch_run(
                session_factory, run_id, run_fetcher, budget, started=now, watch=watch
            )
        )
        try:
            collector = collector_cls(run_fetcher, serp)
            collector.variant = variant
            collector.started_at = now
            collector.evidence_root = evidence_root

            status, error = "ok", None
            # A paid call the collector made itself (services/extraction's
            # classify, T-27) rather than through the Fetcher: what stopped it.
            stopped: tuple[str, str] | None = None
            try:
                try:
                    observations = await collector.collect(session, budget)
                finally:
                    await _stop(checkpoint)
            except HostPaused:
                observations, status, error = (
                    list(collector.found),
                    "failed",
                    BLOCKED_ERROR,
                )
            except BudgetExhausted:
                observations, status = list(collector.found), "partial"
            except costs.ModelUnavailable as exc:
                # A cap (or a model that did not answer) is a budget stop, not
                # a failure: the collector keeps its free part (04 §8, 06 §4.2
                # cost_cap). CostCapReached is one; its code names the rung.
                observations, status = list(collector.found), "partial"
                stopped = _stopped_by(exc)
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
            if watch.paused and stopped is None:
                stopped = (GLOBAL_PAUSE, "")
                if status == "ok":
                    status = "partial"
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
                    supplied=collector.supplied,
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
            else:
                try:
                    # Its own savepoint: a hook that fails costs its writes,
                    # never the run's evidence and observations.
                    async with session.begin_nested():
                        await collector.after_write(session, list(written.placed))
                except Exception as exc:
                    written.dropped[AFTER_WRITE] += 1
                    log.warning(
                        "collector %s: after_write failed: %s",
                        key,
                        type(exc).__name__,
                        extra={"source": key, "run_id": run_id},
                    )

            if status == "ok" and (
                written.dropped[BAD_EVIDENCE]
                or written.dropped[BAD_OBSERVATION]
                or written.dropped[AFTER_WRITE]
            ):
                status = "partial"
            stats = run_fetcher.stats
            counters: dict[str, Any] = {
                "requests": stats.requests,
                "pages": stats.pages,
                "http_403": stats.http_403,
                "http_429": stats.http_429,
                "new_evidence": written.new_evidence,
                "new_observations": written.new_observations,
                # None resolved yet: the resolver adds each company it mints
                # from this run's evidence (companies.count_minted).
                "new_companies": 0,
                "new_signals": 0,
                "cost_usd": stats.cost_usd,
            }
            meta = _meta(
                variant=variant, fetcher=run_fetcher, written=written, stopped=stopped
            )
            meta = {**collector.run_meta, **meta}
            end = finished()
            await session.execute(
                sa.update(CollectorRun)
                .where(CollectorRun.id == run_id)
                .values(
                    finished_at=end, status=status, error=error, meta=meta, **counters
                )
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
                **{
                    k: (str(v) if isinstance(v, Decimal) else v)
                    for k, v in counters.items()
                },
                "unchanged": meta["unchanged"],
                "skipped_robots": meta["skipped_robots"],
            }
            if blocked:
                detail["problem"] = BLOCKED_PROBLEM
                detail["blocked_hosts"] = blocked
            await health.beat(session, component, detail=detail, now=end)
            await session.commit()
        except BaseException as exc:
            # Cancelled (worker shutdown, a deploy) or the bookkeeping failed:
            # the row must not stay 'running', or ux_collector_runs_one_running
            # refuses every later run until the stale margin. A fresh session,
            # shielded: this one may be mid-statement, and a second cancel must
            # not interrupt the close.
            checkpoint.cancel()
            cancelled = isinstance(exc, asyncio.CancelledError)
            error = CANCELLED if cancelled else type(exc).__name__
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    _close_interrupted(
                        session_factory,
                        run_id,
                        run_fetcher,
                        error=error,
                        variant=variant,
                        finished_at=finished(),
                    )
                )
            raise

    log.info(
        "collector %s %s",
        key,
        status,
        extra={"source": key, "run_id": run_id, "status": status, "error": error},
    )
    return RunReport(key, component, status, run_id, error, counters, meta)
