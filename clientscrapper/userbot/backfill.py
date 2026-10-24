"""The nightly backfill (03 §2.3): what was said before the reader joined.

Once per chat, for a readable chat joined at least 24 hours ago and not yet
backfilled: the newest ``BACKFILL_LIMIT`` (500) messages through
``iter_messages`` — the one place besides the about reader that reads
history (07 ``HISTORY_ALLOWED``) — filtered **client-side** by the prefilter
(a server ``search=`` is one keyword at a time and misses the Latin/Cyrillic
variants). Only the hits are stored, through ``ingest.handle_message``, so a
backfilled hit is stored and notified exactly as a live one; the rest only
count in ``seen_count``. Then ``chat_monitors.backfilled_at``.

The window is also bounded in time: a message older than ``BACKFILL_MAX_AGE``
(30 days) ends the read. Retention deletes an unconverted post at 30 days
(02 §4 R2a), so an older one would be stored only to be deleted that night.

No backfill while the flood breaker or the owner's pause holds; a flood wait
ends the night (past 300 s it trips the breaker); a chat that keeps failing
is left alone after ``chats.BACKFILL_MAX_ATTEMPTS`` nights.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.db.models import ChatMonitor
from clientscrapper.services import chats
from clientscrapper.userbot import ingest, limits, tg

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

BACKFILL_LIMIT: Final = 500
BACKFILL_MAX_AGE: Final = timedelta(days=30)
CHATS_PER_NIGHT: Final = 10


@dataclass(slots=True)
class ChatBackfill:
    seen: int = 0
    stored: int = 0
    oldest_read: datetime | None = None


async def backfill_chat(
    client: Any, session: AsyncSession, monitor: ChatMonitor, *, now: datetime
) -> ChatBackfill:
    """Read one chat back and store its hits; stamps ``backfilled_at``. The
    caller commits (one transaction per chat)."""
    result = ChatBackfill()
    floor = now - BACKFILL_MAX_AGE
    async for message in client.iter_messages(monitor.tg_chat_id, limit=BACKFILL_LIMIT):
        date = getattr(message, "date", None)
        if isinstance(date, datetime) and date < floor:
            break
        outcome = await ingest.handle_message(session, message, now=now)
        if outcome == "ignored":
            continue
        result.seen += 1
        result.stored += 1 if outcome == "stored" else 0
        result.oldest_read = date if isinstance(date, datetime) else result.oldest_read
    chats.mark_backfilled(monitor, now=now)
    return result


@dataclass(slots=True)
class NightReport:
    chats: int = 0
    seen: int = 0
    stored: int = 0
    failed: int = 0
    skipped: str | None = None


async def backfill_nightly(
    client: Any, session_factory: SessionFactory, *, now: datetime
) -> NightReport:
    """The night's pass over every chat that is due (≤ CHATS_PER_NIGHT)."""
    report = NightReport()
    async with session_factory() as session:
        report.skipped = await limits.blocked(session, now=now)
        if report.skipped:
            return report
        due = [
            m.id
            for m in await chats.backfill_due(session, now=now, limit=CHATS_PER_NIGHT)
        ]
    for monitor_id in due:
        try:
            async with session_factory() as session:
                monitor = await session.get(ChatMonitor, monitor_id)
                if monitor is None or not chats.readable(monitor):
                    continue
                done = await backfill_chat(client, session, monitor, now=now)
        except tg.FLOOD_ERRORS as exc:
            seconds = tg.flood_seconds(exc) or 0
            async with session_factory() as session:
                await limits.trip_breaker(
                    session, seconds=seconds, now=now, where="backfill"
                )
                await _failed(session, monitor_id)
            report.failed += 1
            log.warning("backfill stops on a flood wait", extra={"seconds": seconds})
            return report
        except tg.GONE_ERRORS:
            async with session_factory() as session:
                gone = await session.get(ChatMonitor, monitor_id)
                if gone is not None:
                    chats.mark_gone(gone)
            report.failed += 1
            continue
        except tg.RESTRICTION_ERRORS:
            raise
        except Exception as exc:
            log.warning(
                "backfill of one chat failed",
                extra={"chat_monitor_id": monitor_id, "error": type(exc).__name__},
            )
            async with session_factory() as session:
                await _failed(session, monitor_id)
            report.failed += 1
            continue
        report.chats += 1
        report.seen += done.seen
        report.stored += done.stored
    return report


async def _failed(session: AsyncSession, monitor_id: int) -> None:
    monitor = await session.get(ChatMonitor, monitor_id)
    if monitor is not None:
        chats.mark_backfill_failed(monitor)
