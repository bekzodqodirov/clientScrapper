"""The daily join (03 §2.3, 06 §2): ≤ 3 chats a day, one function that joins.

Once a day, at a random minute between 10:00 and 20:00 Tashkent, the reader's
scheduler runs ``join_daily``:

1. ``resolve_daily`` turns up to ``RESOLVES_PER_DAY`` (20) waiting usernames
   — the seeds, the companion groups an ``about`` named, the channels other
   collectors found — into queued ``chat_monitors`` rows (public usernames
   only: no invite links in v1, and ``ImportChatInvite`` never appears);
2. then, while the day's ``USERBOT_JOINS_PER_DAY`` (3) lasts and the account
   sits in fewer than ``USERBOT_MAX_CHATS`` (60) chats, the best queued row is
   joined by ``join_one`` — ``JoinChannelRequest`` then
   ``GetFullChannelRequest`` and nothing else — with ≥ 90 s between joins.
   The full channel names the linked discussion group, queued at priority 1
   (the surface of a competitor's channel, 03 §2.3), and its ``about``, kept as
   weekly evidence whose companion-group mentions wait at priority 2.

Each join is counted in ``runtime_flags('userbot_quota')`` before the request
goes out, so neither a crash nor a restart can make a fourth. Nothing is
joined while the flood breaker or the owner's pause holds. The agent never
leaves a chat: the owner leaves by hand, and ``/chats`` off only stops reading.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import ChatMonitor
from clientscrapper.services import chats
from clientscrapper.userbot import about, limits, tg

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]

JOIN_GAP_SECONDS: Final = 90
JOIN_WINDOW: Final = (time(10, 0), time(20, 0))


def join_minute(day: date, *, rng: random.Random | None = None) -> time:
    """The day's join time: a random minute in [10:00, 20:00) (06 §2)."""
    start, end = JOIN_WINDOW
    span = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    picker = rng or random.Random()
    offset = picker.randrange(span)
    minutes = start.hour * 60 + start.minute + offset
    return time(minutes // 60, minutes % 60)


def join_due(now: datetime, *, at: time, last_day: date | None) -> bool:
    """Is today's join due: past today's minute, inside the window, not run
    yet today (Tashkent)."""
    local = now.astimezone(settings.tz)
    if last_day == local.date():
        return False
    return at <= local.time() < JOIN_WINDOW[1]


@dataclass(slots=True)
class JoinReport:
    resolved: int = 0
    joined: list[int] = field(default_factory=list)
    gone: int = 0
    queued_linked: int = 0
    skipped: str | None = None


@dataclass(frozen=True, slots=True)
class Joined:
    """What joining one chat taught: the chat, its linked discussion group
    and its about."""

    dialog: chats.DialogInfo
    linked: chats.DialogInfo | None
    about: about.About


async def join_one(client: Any, entity: Any) -> Joined:
    """Join one public chat and read its full info. The only
    ``JoinChannelRequest`` in the package (07 §1.2)."""
    from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest

    await client(JoinChannelRequest(channel=entity))
    full = await client(GetFullChannelRequest(channel=entity))
    info = full.full_chat
    linked_id = getattr(info, "linked_chat_id", None)
    linked = None
    if linked_id:
        peer = chats.channel_peer_id(int(linked_id))
        for chat in getattr(full, "chats", None) or ():
            if tg.peer_id(chat) == peer:
                linked = tg.dialog_of(chat)
                break
    dialog = tg.dialog_of(entity)
    members = getattr(info, "participants_count", None)
    return Joined(
        dialog=chats.DialogInfo(
            tg_chat_id=dialog.tg_chat_id,
            chat_type=dialog.chat_type,
            title=dialog.title,
            username=dialog.username,
            member_count=members if isinstance(members, int) else dialog.member_count,
        ),
        linked=linked,
        about=about.About(
            text=getattr(info, "about", None) or "",
            member_count=members if isinstance(members, int) else None,
            linked_chat_id=linked_id,
        ),
    )


async def _flood(
    session_factory: SessionFactory, exc: BaseException, *, now: datetime, where: str
) -> None:
    seconds = tg.flood_seconds(exc) or 0
    async with session_factory() as session:
        await limits.trip_breaker(session, seconds=seconds, now=now, where=where)
    log.warning("the join job stops on a flood wait", extra={"seconds": seconds})


async def resolve_daily(
    client: Any, session_factory: SessionFactory, *, now: datetime
) -> int:
    """Waiting usernames → queued rows, ≤ RESOLVES_PER_DAY a day. A name that
    is not a public group or channel is remembered as such and never tried
    again. Returns how many were queued."""
    async with session_factory() as session:
        if await limits.blocked(session, now=now):
            return 0
        seeds = chats.seeds_from(await chats.source_config(session))
        waiting = await chats.pending_usernames(
            session, seeds=seeds, limit=limits.RESOLVES_PER_DAY
        )
    queued = 0
    for pending in waiting:
        async with session_factory() as session:
            if not await limits.take(session, "resolves", now=now):
                break
        reason: str | None = None
        try:
            entity = await client.get_entity(pending.username)
        except tg.FLOOD_ERRORS as exc:
            await _flood(session_factory, exc, now=now, where="resolve")
            return queued
        except tg.GONE_ERRORS as exc:
            reason = tg.error_code(exc)
        except ValueError:
            reason = "unknown"
        else:
            dialog = tg.dialog_of(entity)
            if dialog.chat_type == "private" or not dialog.username:
                reason = "not_a_public_chat"
        async with session_factory() as session:
            if reason is None:
                await chats.enqueue_chat(
                    session, dialog, priority=pending.priority, role=pending.role, now=now
                )
                queued += 1
            await chats.forget_pending(
                session, pending.username, now=now, unresolvable=reason
            )
    return queued


async def _queued_target(
    session_factory: SessionFactory, *, tried: set[int]
) -> tuple[int, str | int] | None:
    async with session_factory() as session:
        rows = await chats.queued(session, limit=len(tried) + 1)
        for row in rows:
            if row.id not in tried:
                return row.id, row.username or row.tg_chat_id
    return None


async def _apply(
    session_factory: SessionFactory, monitor_id: int, joined: Joined, *, now: datetime
) -> bool:
    async with session_factory() as session:
        monitor = await session.get(ChatMonitor, monitor_id)
        if monitor is None:
            return False
        chats.mark_joined(monitor, now=now, dialog=joined.dialog)
        linked = False
        if joined.linked is not None:
            linked = (
                await chats.enqueue_chat(
                    session, joined.linked, priority=chats.PRIORITY_LINKED, now=now
                )
                is not None
            )
        await about.store_about(session, monitor, joined.about, now=now)
        return linked


async def join_daily(
    client: Any,
    session_factory: SessionFactory,
    *,
    clock: Clock,
    sleep: Sleep = asyncio.sleep,
) -> JoinReport:
    """The day's join run (see the module docstring). ``clock`` is read again
    after every pause, so the 90 s gaps and the day's cap use the real time."""
    report = JoinReport()
    now = clock()
    report.resolved = await resolve_daily(client, session_factory, now=now)
    tried: set[int] = set()
    first = True
    while True:
        now = clock()
        async with session_factory() as session:
            report.skipped = await limits.blocked(session, now=now)
            if report.skipped:
                return report
            if await chats.joined_count(session) >= settings.userbot_max_chats:
                report.skipped = "max_chats"
                return report
            if await limits.used(session, "joins", now=now) >= limits.quota_limit(
                "joins"
            ):
                return report
        target = await _queued_target(session_factory, tried=tried)
        if target is None:
            return report
        monitor_id, name = target
        tried.add(monitor_id)
        try:
            # A read: it resolves a username or finds a cached id; a linked
            # group the session no longer knows stays queued, no join spent.
            entity = await client.get_entity(name)
        except tg.FLOOD_ERRORS as exc:
            await _flood(session_factory, exc, now=now, where="join")
            return report
        except tg.GONE_ERRORS:
            await _mark_gone(session_factory, monitor_id)
            report.gone += 1
            continue
        except ValueError:
            continue
        if tg.chat_type_of(entity) == "private":
            await _mark_gone(session_factory, monitor_id)
            report.gone += 1
            continue
        if not first:
            await sleep(JOIN_GAP_SECONDS)
            now = clock()
        async with session_factory() as session:
            if not await limits.take(session, "joins", now=now):
                return report
        first = False
        try:
            joined = await join_one(client, entity)
        except tg.FLOOD_ERRORS as exc:
            await _flood(session_factory, exc, now=now, where="join")
            return report
        except tg.TOO_MANY_CHATS_ERRORS:
            report.skipped = "too_many_chats"
            log.error("Telegram says the account is in too many chats; no joins")
            return report
        except (*tg.GONE_ERRORS, *tg.JOIN_REFUSED_ERRORS):
            await _mark_gone(session_factory, monitor_id)
            report.gone += 1
            continue
        if await _apply(session_factory, monitor_id, joined, now=now):
            report.queued_linked += 1
        report.joined.append(monitor_id)
        log.info("joined a chat", extra={"chat_monitor_id": monitor_id})


async def _mark_gone(session_factory: SessionFactory, monitor_id: int) -> None:
    async with session_factory() as session:
        monitor = await session.get(ChatMonitor, monitor_id)
        if monitor is not None:
            chats.mark_gone(monitor)
