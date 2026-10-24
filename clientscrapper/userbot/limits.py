"""The reader's brakes, kept where the three processes can see them (06 §2).

Telethon-free and pure SQL over ``runtime_flags`` (02 §2.13), so a restart
forgets nothing:

* ``flood_until`` — the FloodWait breaker. A wait longer than
  ``USERBOT_FLOOD_BREAKER_SECONDS`` (300) stops joins, backfill, ``about``
  reads, resolves and dialog sync for ``USERBOT_FLOOD_BREAKER_HOURS`` (24);
  live messages keep flowing (a push from Telegram, not a request). The value
  is ``{"until", "seconds", "since", "where"}``, which ``health`` reads as
  the ``flood_breaker`` problem — writing it is how the problem is raised,
  and the worker's health sweep is what tells the owner (06 §4.3: the reader
  never reports its own problem).
* ``userbot_quota`` — the day's counts of what Telegram is asked: ``joins``
  (≤ USERBOT_JOINS_PER_DAY, 3), ``resolves`` (≤ 20), ``about_channels``
  (≤ 20), ``about_users`` (≤ 50). One row, reset at the Tashkent midnight.
  An attempt is counted before the request goes out, so a crash between the
  two can only make the reader more careful, never less.
* ``paused`` — the owner's ``/toxtat``: no joins and no ``about`` reads while
  it is set (06 §2); the rule's home is ``collectors.runner.global_pause``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors.runner import global_pause
from clientscrapper.config import settings
from clientscrapper.db.models import RuntimeFlag

FLOOD_FLAG: Final = "flood_until"
QUOTA_FLAG: Final = "userbot_quota"
UPDATED_BY: Final = "userbot"

RESOLVES_PER_DAY: Final = 20
ABOUT_CHANNELS_PER_DAY: Final = 20
ABOUT_USERS_PER_DAY: Final = 50
QUOTA_KINDS: Final = ("joins", "resolves", "about_channels", "about_users")


def quota_limit(kind: str) -> int:
    """The day's cap for ``kind`` (03 §2.3, 06 §2)."""
    limits = {
        "joins": settings.userbot_joins_per_day,
        "resolves": RESOLVES_PER_DAY,
        "about_channels": ABOUT_CHANNELS_PER_DAY,
        "about_users": ABOUT_USERS_PER_DAY,
    }
    if kind not in limits:
        raise ValueError(f"unknown quota: {kind!r}")
    return limits[kind]


def day_of(now: datetime) -> str:
    """The Tashkent calendar day a count belongs to."""
    return now.astimezone(settings.tz).date().isoformat()


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


async def flood_until(session: AsyncSession, *, now: datetime) -> datetime | None:
    """The breaker's end while it is set, else None."""
    row = await session.get(RuntimeFlag, FLOOD_FLAG, populate_existing=True)
    if row is None:
        return None
    value = row.value
    until = _parse(value.get("until") if isinstance(value, Mapping) else value)
    return until if until is not None and until > now else None


async def trip_breaker(
    session: AsyncSession, *, seconds: int, now: datetime, where: str
) -> datetime | None:
    """A FloodWait of ``seconds`` met in ``where``: past the threshold it sets
    the breaker for USERBOT_FLOOD_BREAKER_HOURS and returns its end; a shorter
    one returns None and changes nothing. A breaker already set is extended,
    never shortened."""
    if seconds <= settings.userbot_flood_breaker_seconds:
        return None
    until = now + timedelta(hours=settings.userbot_flood_breaker_hours)
    current = await flood_until(session, now=now)
    if current is not None and current >= until:
        return current
    value = {
        "until": until.isoformat(),
        "seconds": int(seconds),
        "since": now.isoformat(),
        "where": where,
    }
    stmt = pg_insert(RuntimeFlag).values(
        key=FLOOD_FLAG, value=value, updated_by=UPDATED_BY
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[RuntimeFlag.key],
            set_={"value": stmt.excluded.value, "updated_by": stmt.excluded.updated_by},
        )
    )
    return until


async def blocked(session: AsyncSession, *, now: datetime) -> str | None:
    """Why the reader may not ask Telegram for anything now: ``flood`` while
    the breaker holds, ``paused`` while the owner's /toxtat does; else None."""
    if await flood_until(session, now=now) is not None:
        return "flood"
    if await global_pause(session):
        return "paused"
    return None


async def _quota_row(session: AsyncSession) -> RuntimeFlag:
    await session.execute(
        pg_insert(RuntimeFlag)
        .values(key=QUOTA_FLAG, value={}, updated_by=UPDATED_BY)
        .on_conflict_do_nothing(index_elements=[RuntimeFlag.key])
    )
    row = await session.scalar(
        sa.select(RuntimeFlag)
        .where(RuntimeFlag.key == QUOTA_FLAG)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert row is not None  # the upsert just made sure
    return row


def _counts(value: Any, day: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or value.get("day") != day:
        return {}
    return {
        k: int(v) for k, v in value.items() if k in QUOTA_KINDS and isinstance(v, int)
    }


async def used(session: AsyncSession, kind: str, *, now: datetime) -> int:
    """How many ``kind`` requests went out today."""
    row = await session.get(RuntimeFlag, QUOTA_FLAG, populate_existing=True)
    return _counts(row.value if row is not None else None, day_of(now)).get(kind, 0)


async def take(session: AsyncSession, kind: str, *, now: datetime) -> bool:
    """Count one ``kind`` request today, if the day's cap allows it. False
    (nothing counted) once the cap is reached. Under the row's lock, so two
    callers can never both take the last one."""
    limit = quota_limit(kind)
    row = await _quota_row(session)
    day = day_of(now)
    counts = _counts(row.value, day)
    if counts.get(kind, 0) >= limit:
        return False
    counts[kind] = counts.get(kind, 0) + 1
    row.value = {"day": day, **counts}
    row.updated_by = UPDATED_BY
    await session.flush()
    return True
