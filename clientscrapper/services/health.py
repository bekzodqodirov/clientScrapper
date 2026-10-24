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
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import Heartbeat

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
    the stored dict whole (an absent detail stores ``{}``)."""
    stmt = pg_insert(Heartbeat).values(
        component=component, last_seen_at=_now(now), detail=dict(detail or {})
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Heartbeat.component],
        set_={
            "last_seen_at": stmt.excluded.last_seen_at,
            "detail": stmt.excluded.detail,
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
