# VENDORED_FROM dailyJournal@6b1f67f miya/bot/handlers.py
"""``/manbalar`` and its buttons (06 §1.2): one line per ``sources`` row,
paged by 8 — schedulable rows first, then the attribution-only sub-sources;
``src:<key>:t`` switches a source on or off, ``src:<key>:i`` shows its last
five runs, breaker and pause, ``src:<key>:a`` forbids auto-push (the owner
can only forbid it — ``auto_push`` itself is the precision job's, 05 §B.2).
The writes are ``services/owner``'s.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.bot import keyboards, pages
from clientscrapper.bot.handlers.common import Deps, Tap, edit, reply, toast
from clientscrapper.bot.texts import t
from clientscrapper.db.models import Source
from clientscrapper.services import owner


async def _page(deps: Deps, page: int) -> tuple[str, InlineKeyboardMarkup, int]:
    now = deps.clock()
    async with deps.session_factory() as session:
        lines = await owner.source_lines(session, now=now)
    total_pages = keyboards.pages_of(len(lines))
    page = min(max(page, 0), total_pages - 1)
    shown = lines[page * keyboards.PAGE_SIZE : (page + 1) * keyboards.PAGE_SIZE]
    text = pages.sources_text(shown, page=page, pages=total_pages, now=now)
    keyboard = keyboards.sources_keyboard(
        [keyboards.SourceButton(x.key, x.title, x.state(now)) for x in shown],
        page=page,
        pages=total_pages,
    )
    return text, keyboard, page


async def manbalar(message: Message, deps: Deps) -> None:
    text, keyboard, _ = await _page(deps, 0)
    await reply(message, text, keyboard=keyboard)


async def on_page(tap: Tap) -> None:
    text, keyboard, _ = await _page(tap.deps, tap.data.page or 0)
    await edit(tap.query, text, keyboard=keyboard)
    await toast(tap.query)


async def _page_of(deps: Deps, key: str) -> int:
    now = deps.clock()
    async with deps.session_factory() as session:
        keys = [x.key for x in await owner.source_lines(session, now=now)]
    return keys.index(key) // keyboards.PAGE_SIZE if key in keys else 0


async def on_toggle(tap: Tap) -> None:
    key = tap.data.target
    assert key is not None
    async with tap.deps.session_factory() as session:
        enabled = await owner.toggle_source(session, key)
        title = await _title(session, key)
    if enabled is None:
        await toast(tap.query, t("manbalar.gone"), alert=True)
        return
    text, keyboard, _ = await _page(tap.deps, await _page_of(tap.deps, key))
    await edit(tap.query, text, keyboard=keyboard)
    word = "manbalar.on" if enabled else "manbalar.off"
    await toast(tap.query, t(word, title=title))


async def _title(session: AsyncSession, key: str) -> str:
    """A toast is plain text: the title unescaped."""
    row = await session.get(Source, key)
    return row.title_uz if row is not None else key


async def on_info(tap: Tap) -> None:
    key = tap.data.target
    assert key is not None
    now = tap.deps.clock()
    async with tap.deps.session_factory() as session:
        info = await owner.source_info(session, key, now=now)
    if info is None:
        await toast(tap.query, t("manbalar.gone"), alert=True)
        return
    page = await _page_of(tap.deps, key)
    await edit(
        tap.query,
        pages.source_info_text(info, now),
        keyboard=keyboards.source_info_keyboard(
            key, page=page, can_forbid=not info.line.forbidden
        ),
    )
    await toast(tap.query)


async def on_forbid(tap: Tap) -> None:
    key = tap.data.target
    assert key is not None
    now = tap.deps.clock()
    async with tap.deps.session_factory() as session:
        done = await owner.forbid_auto_push(session, key)
        info = await owner.source_info(session, key, now=now)
    if done is None or info is None:
        await toast(tap.query, t("manbalar.gone"), alert=True)
        return
    title = info.line.title
    page = await _page_of(tap.deps, key)
    await edit(
        tap.query,
        pages.source_info_text(info, now),
        keyboard=keyboards.source_info_keyboard(key, page=page, can_forbid=False),
    )
    word = "manbalar.forbidden" if done else "manbalar.already_forbidden"
    await toast(tap.query, t(word, title=title))


__all__ = ["manbalar", "on_forbid", "on_info", "on_page", "on_toggle"]
