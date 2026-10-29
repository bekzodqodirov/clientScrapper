# VENDORED_FROM dailyJournal@6b1f67f miya/bot/handlers.py
"""``/chats`` (06 §1.2): MIYA's chat list with one toggle — read a chat or
stop reading it (the account never leaves it, 06 §2) — and ``/chats qoʻsh
@username``, which queues a chat for the joiner (``priority=0``, still ≤ 3
joins a day). The registry is ``services/chats``'.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from aiogram.types import InlineKeyboardMarkup, Message

from clientscrapper.bot import keyboards, pages
from clientscrapper.bot.handlers.common import (
    Deps,
    Tap,
    args_of,
    edit,
    is_owner,
    reply,
    toast,
)
from clientscrapper.bot.texts import t
from clientscrapper.services import chats, keys
from clientscrapper.services.owner import owner_ref

# «qoʻsh» as a phone keyboard types it (any apostrophe, or none), and «add».
ADD_WORDS: Final = frozenset({"qoʻsh", "qo'sh", "qo‘sh", "qo’sh", "qosh", "add"})


async def _page(deps: Deps, page: int) -> tuple[str, InlineKeyboardMarkup]:
    now = deps.clock()
    async with deps.session_factory() as session:
        monitors, total = await chats.list_monitors(
            session, offset=page * keyboards.PAGE_SIZE, limit=keyboards.PAGE_SIZE
        )
        total_pages = keyboards.pages_of(total)
        if page >= total_pages and total:
            page = total_pages - 1
            monitors, total = await chats.list_monitors(
                session, offset=page * keyboards.PAGE_SIZE, limit=keyboards.PAGE_SIZE
            )
        hits = await chats.hits_since(
            session, [m.tg_chat_id for m in monitors], since=now - pages.HITS_WINDOW
        )
    rows = [
        pages.ChatLine(
            monitor_id=m.id,
            title=m.title or (f"@{m.username}" if m.username else str(m.tg_chat_id)),
            chat_type=m.chat_type,
            enabled=bool(m.monitor_enabled),
            hits=hits.get(m.tg_chat_id, 0),
            seen=m.seen_count,
        )
        for m in monitors
    ]
    text = pages.chats_text(rows, page=page, pages=total_pages, total=total)
    keyboard = keyboards.chats_keyboard(
        [
            keyboards.ChatButton(r.monitor_id, r.title, r.chat_type, r.enabled)
            for r in rows
        ],
        page=page,
        pages=total_pages,
    )
    return text, keyboard


async def chats_command(message: Message, deps: Deps) -> None:
    args = args_of(message).split()
    if args and args[0].lower() in ADD_WORDS:
        await _add(message, deps, args[1] if len(args) > 1 else "", deps.clock())
        return
    text, keyboard = await _page(deps, 0)
    await reply(message, text, keyboard=keyboard)


async def _add(message: Message, deps: Deps, raw: str, now: datetime) -> None:
    username = keys.username_key(raw)
    if username is None:
        await reply(message, t("chats.bad_username"))
        return
    user = message.from_user.id if message.from_user else None
    by = owner_ref(user) if user is not None and is_owner(user) else "owner"
    async with deps.session_factory() as session:
        added = await chats.enqueue_username(
            session, raw, priority=chats.PRIORITY_OWNER, now=now, by=by
        )
    word = "chats.added" if added else "chats.not_added"
    await reply(message, t(word, username=pages.esc(username)))


async def on_page(tap: Tap) -> None:
    text, keyboard = await _page(tap.deps, tap.data.page or 0)
    await edit(tap.query, text, keyboard=keyboard)
    await toast(tap.query)


async def on_noop(tap: Tap) -> None:
    await toast(tap.query)


async def on_toggle(tap: Tap) -> None:
    monitor_id = tap.data.ref
    assert monitor_id is not None
    now = tap.deps.clock()
    async with tap.deps.session_factory() as session:
        current = await chats.get_monitor_by_id(session, monitor_id)
        if current is None:
            await toast(tap.query, t("chats.gone"), alert=True)
            return
        try:
            monitor = await chats.set_monitor(
                session, monitor_id, not current.monitor_enabled, now=now
            )
        except ValueError:
            await toast(tap.query, t("chats.never_read"), alert=True)
            return
        if monitor is None:
            await toast(tap.query, t("chats.gone"), alert=True)
            return
        title = monitor.title or monitor.username or str(monitor.tg_chat_id)
        enabled = bool(monitor.monitor_enabled)
        offset = await chats.position_of(session, monitor_id)
    text, keyboard = await _page(tap.deps, offset // keyboards.PAGE_SIZE)
    await edit(tap.query, text, keyboard=keyboard)
    await toast(tap.query, t("chats.on" if enabled else "chats.off", title=title))


__all__ = ["ADD_WORDS", "chats_command", "on_noop", "on_page", "on_toggle"]
