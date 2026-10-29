"""What ``make smoke`` found (07 §1.12, 03 §1.4): the live tests' record.

A live test makes one real request per collector, run only with ``LIVE=1``
from the VPS (``make smoke``: weekly, Sunday 05:00; ``FULL=1`` quarterly).
Live tests never write anywhere but ``collector_runs``: the recorder
(``tests/live/conftest.py``) writes one row per test with
``status='skipped'`` and ``meta={"note": "smoke", "variant": "smoke",
"smoke": passed|failed|skipped, "test": <name>, "run": <the session's id>,
"full": bool}`` — ``note`` is a ``meta`` key, not a column (02 §2.2) — and a
host breaker during the run ends the row ``status='failed',
error='host_blocked'``, never a ``blocked`` status. The cadence is its own
(``variant='smoke'``): a smoke row neither breaks nor makes a real cadence's
``job_failing`` streak, and health counts a ``skipped`` row's moment as «the
scheduler fired it» only when it is not a smoke row (``not_smoke``) — a
smoke run must never hide a silent collector.

:func:`latest` is the digest's reading: the newest run of the last day,
counted per outcome — a skipped live test is reported as skipped, never
counted green (07 §1.12, 03 §1.4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.db.models import CollectorRun, Source
from clientscrapper.services import http

SMOKE_NOTE: Final = "smoke"
SMOKE_VARIANT: Final = "smoke"
BLOCKED_ERROR: Final = http.BLOCKED_ERROR
# The digest reports the newest run that started within this window (the
# weekly run is at Sunday 05:00, the digest at 09:00).
SMOKE_WINDOW: Final = timedelta(hours=24)

Outcome = Literal["passed", "failed", "skipped"]
OUTCOMES: Final[tuple[Outcome, ...]] = ("passed", "failed", "skipped")


@dataclass(frozen=True, slots=True)
class TestResult:
    """One live test as pytest reported it. ``blocked``: it failed on the
    Fetcher's host breaker (``http.HostPaused``), the 03 §1.1 «stop on a
    403/429 streak» — a failed row with ``error='host_blocked'``."""

    __test__ = False  # not a pytest test class, whatever its name says

    source_key: str
    test: str
    outcome: Outcome
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class SmokeRun:
    """One ``make smoke`` session: the sources' titles per outcome, in test
    order (a source with two live tests is named once per outcome)."""

    run: str
    started_at: datetime
    full: bool
    passed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    # How many tests, not sources, each outcome counts.
    counts: tuple[int, int, int] = (0, 0, 0)


def not_smoke() -> sa.ColumnElement[bool]:
    """A ``collector_runs`` row that is not a smoke test's (``meta.note``)."""
    found: sa.ColumnElement[bool] = CollectorRun.meta["note"].astext.is_distinct_from(
        SMOKE_NOTE
    )
    return found


def is_smoke() -> sa.ColumnElement[bool]:
    found: sa.ColumnElement[bool] = CollectorRun.meta["note"].astext == SMOKE_NOTE
    return found


async def record(
    session: AsyncSession,
    results: Sequence[TestResult],
    *,
    started_at: datetime,
    now: datetime,
    full: bool,
) -> str:
    """One ``collector_runs`` row per live test of the session that began at
    ``started_at``; the run's id (its start, ISO). The caller commits."""
    run = started_at.isoformat()
    for result in results:
        if result.outcome not in OUTCOMES:
            raise ValueError(f"not a smoke outcome: {result.outcome!r}")
        blocked = result.outcome == "failed" and result.blocked
        session.add(
            CollectorRun(
                source_key=result.source_key,
                started_at=started_at,
                finished_at=max(now, started_at),
                status="failed" if blocked else "skipped",
                error=BLOCKED_ERROR if blocked else None,
                meta={
                    "note": SMOKE_NOTE,
                    "variant": SMOKE_VARIANT,
                    "smoke": result.outcome,
                    "test": result.test[:200],
                    "run": run,
                    "full": full,
                },
            )
        )
    await session.flush()
    return run


async def last_run_at(session: AsyncSession, *, now: datetime) -> datetime | None:
    """When the newest smoke run up to ``now`` started, or None (never)."""
    found: datetime | None = await session.scalar(
        sa.select(sa.func.max(CollectorRun.started_at)).where(
            is_smoke(), CollectorRun.started_at <= now
        )
    )
    return found


async def latest(
    session: AsyncSession, *, now: datetime, window: timedelta = SMOKE_WINDOW
) -> SmokeRun | None:
    """The newest smoke run that started within ``window`` before ``now``,
    or None (no run, or nothing recorded)."""
    run_id = CollectorRun.meta["run"].astext
    newest = await session.scalar(
        sa.select(run_id)
        .where(
            is_smoke(),
            CollectorRun.started_at >= now - window,
            CollectorRun.started_at <= now,
        )
        .order_by(CollectorRun.started_at.desc(), CollectorRun.id.desc())
        .limit(1)
    )
    if newest is None:
        return None
    rows = (
        await session.execute(
            sa.select(
                Source.title_uz,
                CollectorRun.meta["smoke"].astext,
                CollectorRun.meta["full"].as_boolean(),
                CollectorRun.started_at,
            )
            .join(Source, Source.key == CollectorRun.source_key)
            .where(is_smoke(), run_id == newest)
            .order_by(CollectorRun.id)
        )
    ).all()
    named: dict[str, list[str]] = {outcome: [] for outcome in OUTCOMES}
    counts = dict.fromkeys(OUTCOMES, 0)
    full = False
    started = now
    for title, outcome, is_full, at in rows:
        if outcome not in named:
            continue
        counts[outcome] += 1
        if title not in named[outcome]:
            named[outcome].append(title)
        full = full or bool(is_full)
        started = min(started, at)
    return SmokeRun(
        run=str(newest),
        started_at=started,
        full=full,
        passed=tuple(named["passed"]),
        failed=tuple(named["failed"]),
        skipped=tuple(named["skipped"]),
        counts=(counts["passed"], counts["failed"], counts["skipped"]),
    )


__all__ = [
    "BLOCKED_ERROR",
    "OUTCOMES",
    "SMOKE_NOTE",
    "SMOKE_VARIANT",
    "SMOKE_WINDOW",
    "SmokeRun",
    "TestResult",
    "is_smoke",
    "last_run_at",
    "latest",
    "not_smoke",
    "record",
]
