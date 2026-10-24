# VENDORED_FROM dailyJournal@6b1f67f miya/services/health.py
"""Self-monitoring (06 §4.1) — the heartbeat core, MIYA verbatim.

Every process, every scheduler job and every collector run upserts one row in
``heartbeats`` through :func:`beat`; :func:`beats` reads them back. The
component names are one vocabulary across 02 §2.13, 03 §1.2, 05 and 06:
``bot``, ``worker``, ``userbot``, ``backup``, ``restore_drill``, ``job:<id>``
and ``collector:<key>[.variant]`` (:func:`collector_component`).

The status, the problem list and the alert memory (``gather``, ``problems``,
``alerts_due``, ``mark_*``) are T-08's and join this module there; nothing
here sends anything.

The **raised problems** (``REPORTED_KEYS``, ``raise_problem``,
``clear_problem``) and the **suppression probe** (``SuppressionHealth``,
``set_suppression_probe``) at the end of this file are T-08's code, copied
verbatim from its branch (wt/t08 423d97b) so that T-09's feed sync writes
exactly the ``runtime_flags`` rows T-08's ``gather`` reads; at the merge
T-08's copy is the one kept.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import Heartbeat, RuntimeFlag

COMPONENTS: Final = ("bot", "worker", "userbot")
JOB_PREFIX: Final = "job:"
COLLECTOR_PREFIX: Final = "collector:"
# The heartbeat the worker writes after the nightly file went (or failed to go).
BACKUP_COMPONENT: Final = "backup"
RESTORE_DRILL_COMPONENT: Final = "restore_drill"

# A sources.key (02 §2.1) and a cadence variant: «erp_sync.full»,
# «serp_layer.night», «telegram_monitor.backfill».
_KEY = re.compile(r"[a-z][a-z0-9_]{2,40}")
_VARIANT = re.compile(r"[a-z][a-z0-9_]{0,30}")


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


# --- raised problems (T-08, verbatim) ------------------------------------------

# Raised and cleared by the module that sees them (raise_problem): the event
# is that module's, so is the moment it is over.
REPORTED_KEYS: Final = frozenset(
    {
        "suppression_selftest_failed",  # full pull (T-09)
        "suppression_stale_hit",  # outcomes walk raises, next full pull clears (T-10)
        "suppression_shrunk",  # full pull (T-09)
        "feed_contract",  # any feed call: detail.code (T-09/T-10)
        "push_unconfirmed",  # reconcile_unconfirmed after healthy walks (T-10)
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


# Probes registered by the module that owns the rule.
SuppressionProbe = Callable[[AsyncSession, datetime], Awaitable[SuppressionHealth | None]]


@dataclass(slots=True)
class _Probes:
    suppression: SuppressionProbe | None = None


_probes = _Probes()


def set_suppression_probe(probe: SuppressionProbe | None) -> None:
    """services/erp_feeds registers ``suppression_fresh`` here (T-09)."""
    _probes.suppression = probe


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
