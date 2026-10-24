"""What a channel says about itself, and what a poster's bio says (06 §2).

``read_about`` is the one function besides the joiner allowed a raw request
(07 ``RAW_ALLOWED``): ``GetFullChannelRequest`` for a channel's full ``about``
(and its pinned post, the one history read this module makes) and
``GetFullUserRequest`` for a poster's bio. Everything else here decides what
to read, within the day's caps, and keeps what was read:

* a channel's ``about`` is **not** an interaction (it has no prefilter hit,
  02 §2.12): once a week it becomes an ``evidence`` row at
  ``https://t.me/s/<username>`` under ``telegram_monitor`` (03 §2.3), from
  which T-14's collector makes the ``profile`` observation. Read for the
  queued rows E3 found (priority 3) first, then for the chats the reader sits
  in; ≤ ABOUT_CHANNELS_PER_DAY (20) a day;
* a poster's bio goes into ``interactions.sender_bio`` (≤ 300 characters,
  person-tier, cleared by retention at 90 d) for the tout filter; ≤ 50 a day,
  and a bio read in the last 30 days is copied rather than read again. An
  empty bio is stored as ``''`` so it is not asked for again.

Nothing is read while the flood breaker or the owner's pause holds.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.db.models import ChatMonitor, Evidence, Interaction
from clientscrapper.services import chats, evidence
from clientscrapper.services.normalise import telegram_handles
from clientscrapper.services.text import storable
from clientscrapper.userbot import limits, tg

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

ABOUT_EVERY: Final = timedelta(days=7)
BIO_CACHE: Final = timedelta(days=30)
# Bios are read for senders of recent hits only: an older row is past the
# tout filter's 7-day window.
BIO_WINDOW: Final = timedelta(days=7)
BIO_MAX: Final = 300
# Companion groups named in an about (03 §2.3), per chat.
COMPANIONS_PER_CHAT: Final = 3
PUBLIC_PAGE: Final = "https://t.me/s/{username}"


@dataclass(frozen=True, slots=True)
class About:
    """What Telegram answered about a channel or a user."""

    text: str
    pinned: str | None = None
    member_count: int | None = None
    linked_chat_id: int | None = None
    chats: tuple[Any, ...] = field(default=())


async def read_about(client: Any, target: Any, *, kind: str) -> About:
    """One read: a channel's full info and pinned post, or a user's bio."""
    if kind == "channel":
        from telethon.tl.functions.channels import GetFullChannelRequest

        full = await client(GetFullChannelRequest(channel=target))
        info = full.full_chat
        pinned = None
        if getattr(info, "pinned_msg_id", None):
            found = await client.get_messages(target, ids=info.pinned_msg_id)
            pinned = tg.text_of(found) if found is not None else None
        return About(
            text=getattr(info, "about", None) or "",
            pinned=pinned or None,
            member_count=getattr(info, "participants_count", None),
            linked_chat_id=getattr(info, "linked_chat_id", None),
            chats=tuple(getattr(full, "chats", None) or ()),
        )
    if kind == "user":
        from telethon.tl.functions.users import GetFullUserRequest

        full = await client(GetFullUserRequest(id=target))
        return About(text=getattr(full.full_user, "about", None) or "")
    raise ValueError(f"unknown about kind: {kind!r}")


def about_body(monitor: ChatMonitor, about: About, *, read_at: datetime) -> bytes:
    """The evidence bytes: what the channel says, as JSON for T-14's parser."""
    payload = {
        "tg_chat_id": monitor.tg_chat_id,
        "username": monitor.username,
        "title": monitor.title,
        "chat_type": monitor.chat_type,
        "about": storable(about.text),
        "pinned": storable(about.pinned) if about.pinned else None,
        "member_count": about.member_count,
        "read_at": read_at.isoformat(),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()


async def store_about(
    session: AsyncSession, monitor: ChatMonitor, about: About, *, now: datetime
) -> Evidence | None:
    """The weekly ``evidence`` row for a chat's about (03 §2.3); None for a chat
    with no public username (it has no t.me/s/ page to cite). The companion
    groups it names are queued for resolution (priority 2)."""
    if not monitor.username:
        return None
    if about.member_count is not None:
        monitor.member_count = about.member_count
    row = await evidence.store(
        session,
        source_key=chats.SOURCE_KEY,
        url=PUBLIC_PAGE.format(username=monitor.username),
        body=about_body(monitor, about, read_at=now),
        content_type="application/json",
        fetched_at=now,
        source_ref=f"tg:{monitor.tg_chat_id}",
        excerpt=about.text or about.pinned or monitor.title or monitor.username,
        title=monitor.title,
        meta={"kind": "telegram_about"},
    )
    mentioned = telegram_handles("\n".join(t for t in (about.text, about.pinned) if t))
    companions = [name for name in mentioned if name != monitor.username]
    for name in companions[:COMPANIONS_PER_CHAT]:
        await chats.enqueue_username(
            session,
            name,
            priority=chats.PRIORITY_COMPANION,
            role="buyers",
            now=now,
            by=f"about:{monitor.username}",
        )
    return row


async def channels_due(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[ChatMonitor]:
    """Chats whose about is due: queued priority-3 rows (E3's finds) first,
    then readable joined chats; each with a username and no about evidence in
    the last ABOUT_EVERY."""
    recent = sa.exists().where(
        Evidence.source_key == chats.SOURCE_KEY,
        Evidence.url == sa.func.concat("https://t.me/s/", ChatMonitor.username),
        Evidence.fetched_at > now - ABOUT_EVERY,
    )
    queued_first = sa.case((ChatMonitor.status == "queued", 0), else_=1)
    return list(
        await session.scalars(
            sa.select(ChatMonitor)
            .where(
                ChatMonitor.username.is_not(None),
                ChatMonitor.chat_type.in_(("group", "channel")),
                ChatMonitor.role != "calibration",
                sa.or_(
                    sa.and_(
                        ChatMonitor.status == "queued",
                        ChatMonitor.priority == chats.PRIORITY_RESOLVED,
                    ),
                    sa.and_(
                        ChatMonitor.status == "joined",
                        ChatMonitor.monitor_enabled.is_(True),
                    ),
                ),
                ~recent,
            )
            .order_by(queued_first, ChatMonitor.priority, ChatMonitor.id)
            .limit(limit)
        )
    )


@dataclass(frozen=True, slots=True)
class BioTarget:
    sender_id: int
    sender_username: str | None


async def bios_due(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[BioTarget]:
    """Senders of the last week's hits whose bio is unknown, newest first.
    Users only: a channel's own post has a negative (channel) sender id."""
    newest = sa.func.max(Interaction.occurred_at)
    rows = await session.execute(
        sa.select(Interaction.sender_id, sa.func.max(Interaction.sender_username), newest)
        .where(
            Interaction.sender_id > 0,
            Interaction.sender_bio.is_(None),
            Interaction.occurred_at > now - BIO_WINDOW,
        )
        .group_by(Interaction.sender_id)
        .order_by(newest.desc())
        .limit(limit)
    )
    return [BioTarget(int(r[0]), r[1]) for r in rows]


async def cached_bio(
    session: AsyncSession, sender_id: int, *, now: datetime
) -> str | None:
    """A bio of this sender read within BIO_CACHE, else None."""
    bio: str | None = await session.scalar(
        sa.select(Interaction.sender_bio)
        .where(
            Interaction.sender_id == sender_id,
            Interaction.sender_bio.is_not(None),
            Interaction.occurred_at > now - BIO_CACHE,
        )
        .order_by(Interaction.occurred_at.desc())
        .limit(1)
    )
    return bio


async def fill_bio(
    session: AsyncSession, sender_id: int, bio: str, *, now: datetime
) -> int:
    """Write ``bio`` into this sender's recent rows that have none."""
    clipped = storable(bio).strip()[:BIO_MAX]
    result = await session.execute(
        sa.update(Interaction)
        .where(
            Interaction.sender_id == sender_id,
            Interaction.sender_bio.is_(None),
            Interaction.occurred_at > now - BIO_CACHE,
        )
        .values(sender_bio=clipped)
    )
    return int(getattr(result, "rowcount", 0) or 0)


@dataclass(slots=True)
class AboutReport:
    channels: int = 0
    bios: int = 0
    bios_cached: int = 0
    skipped: str | None = None


async def _flood(
    session_factory: SessionFactory, exc: BaseException, *, now: datetime
) -> None:
    seconds = tg.flood_seconds(exc) or 0
    async with session_factory() as session:
        await limits.trip_breaker(session, seconds=seconds, now=now, where="about")
    log.warning("about reads stop on a flood wait", extra={"seconds": seconds})


async def drain_about(
    client: Any,
    session_factory: SessionFactory,
    *,
    now: datetime,
    batch: int = 5,
) -> AboutReport:
    """One pass: up to ``batch`` channel abouts and ``batch`` bios, inside the
    day's caps. A flood wait ends the pass (and past 300 s trips the
    breaker); a chat that is gone is marked so."""
    report = AboutReport()
    async with session_factory() as session:
        report.skipped = await limits.blocked(session, now=now)
        if report.skipped:
            return report
        due = await channels_due(session, now=now, limit=batch)
        due_ids = [m.id for m in due]
    for monitor_id in due_ids:
        async with session_factory() as session:
            monitor = await session.get(ChatMonitor, monitor_id)
            if monitor is None or not await limits.take(
                session, "about_channels", now=now
            ):
                break
            target = monitor.username
        try:
            about = await read_about(client, target, kind="channel")
        except tg.FLOOD_ERRORS as exc:
            await _flood(session_factory, exc, now=now)
            return report
        except tg.GONE_ERRORS:
            async with session_factory() as session:
                gone = await session.get(ChatMonitor, monitor_id)
                if gone is not None:
                    chats.mark_gone(gone)
            continue
        async with session_factory() as session:
            monitor = await session.get(ChatMonitor, monitor_id)
            if monitor is not None:
                await store_about(session, monitor, about, now=now)
                report.channels += 1
    async with session_factory() as session:
        senders = await bios_due(session, now=now, limit=batch)
    for sender in senders:
        async with session_factory() as session:
            known = await cached_bio(session, sender.sender_id, now=now)
            if known is not None:
                await fill_bio(session, sender.sender_id, known, now=now)
                report.bios_cached += 1
                continue
            if not await limits.take(session, "about_users", now=now):
                break
        try:
            # The username finds a sender the session has not cached (after a
            # restart); the id alone works only for an entity seen since.
            who = sender.sender_username or sender.sender_id
            bio = await read_about(client, who, kind="user")
        except tg.FLOOD_ERRORS as exc:
            await _flood(session_factory, exc, now=now)
            return report
        except (ValueError, *tg.GONE_ERRORS):
            # Not in the session's cache and no username to find it by, or
            # the account is gone: tomorrow's pass may know it; the week's
            # window ends the retries.
            continue
        async with session_factory() as session:
            await fill_bio(session, sender.sender_id, bio.text, now=now)
            report.bios += 1
    return report
