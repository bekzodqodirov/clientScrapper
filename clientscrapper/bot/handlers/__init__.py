"""The owner bot's routers (06 §1.1) and ``HANDLERS``, the dict they are
built from (06 §1.5).

Routers, in registration order:

* ``owner`` — ``OWNER_TELEGRAM_ID``'s private chat: every command and button;
* ``manager`` — an id of ``MANAGER_TELEGRAM_IDS``, private chat: ``/start``,
  ``/yordam``, ``/qidir`` and ``mh:*`` on the manager's own cards;
* ``reject`` — everything else: answers nothing, logs ``rejected_update``
  with the user id at most once per id per hour, and leaves any group or
  channel the bot is added to.

A button's ``callback_data`` is parsed once (``bot/callbacks.parse``) and its
``(kind, action)`` looked up here; ``tests/test_bot_callbacks.py`` holds this
dict and the keyboards to each other.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from clientscrapper.bot import callbacks as cb
from clientscrapper.bot.handlers import admin, chats, manager, review, sources, status
from clientscrapper.bot.handlers.common import (
    Deps,
    Tap,
    TapHandler,
    is_manager,
    is_owner,
    reply,
    toast,
)
from clientscrapper.bot.texts import t

log = logging.getLogger(__name__)

HANDLERS: Final[Mapping[str, Mapping[str | None, TapHandler]]] = MappingProxyType(
    {
        "rv": {
            **{a: review.on_answer for a in cb.REVIEW_ANSWERS},
            "m": review.on_merge_menu,
            "o": review.on_more,
            "k": review.on_back,
        },
        "mg": {None: review.on_answer},
        "mh": {a: manager.on_answer for a in ("c", "x", "w", "l")},
        "src": {"t": sources.on_toggle, "i": sources.on_info, "a": sources.on_forbid},
        "srcp": {None: sources.on_page},
        "ch": {None: chats.on_toggle},
        "chp": {None: chats.on_page},
        "chnoop": {None: chats.on_noop},
        "ps": {"y": admin.on_resume, "n": admin.on_keep, "c": admin.on_clear_crm},
        "im": {"y": admin.on_import, "n": admin.on_import},
        "dn": {"y": admin.on_dnc, "n": admin.on_dnc},
        "pt": {"y": admin.on_push_check, "n": admin.on_push_check},
    }
)
# The buttons a manager may press (06 §1.1).
MANAGER_KINDS: Final = frozenset({"mh"})

Handler = Callable[..., Awaitable[Any]]
OWNER_COMMANDS: Final[Mapping[str, Handler]] = MappingProxyType(
    {
        "start": manager.start,
        "yordam": manager.yordam,
        "help": manager.yordam,
        "lidlar": review.lidlar,
        "manbalar": sources.manbalar,
        "chats": chats.chats_command,
        "holat": status.holat,
        "xarajat": status.xarajat,
        "toxtat": admin.toxtat,
        "yoq": admin.yoq,
        "import": admin.import_command,
        "unut": admin.unut,
        "qidir": manager.qidir,
        "dnc": admin.dnc,
        "vazn": admin.vazn,
        "push": admin.push,
        "stir": admin.stir,
    }
)
MANAGER_COMMANDS: Final[Mapping[str, Handler]] = MappingProxyType(
    {
        "start": manager.start,
        "yordam": manager.yordam,
        "help": manager.yordam,
        "qidir": manager.qidir,
    }
)


async def dispatch(
    query: CallbackQuery, deps: Deps, *, kinds: frozenset[str] | None = None
) -> None:
    """One button: parsed, looked up in HANDLERS, run. A payload the grammar
    refuses — or a kind this router does not serve — is «Bu tugma eskirgan»,
    never silence (#937)."""
    data = cb.parse(query.data or "")
    handler = None
    if data is not None and (kinds is None or data.kind in kinds):
        handler = HANDLERS.get(data.kind, {}).get(data.action)
    if data is None or handler is None:
        await toast(query, t("stale_button"))
        return
    await handler(Tap(query=query, data=data, deps=deps, user_id=query.from_user.id))


async def owner_tap(query: CallbackQuery, deps: Deps) -> None:
    await dispatch(query, deps)


async def manager_tap(query: CallbackQuery, deps: Deps) -> None:
    await dispatch(query, deps, kinds=MANAGER_KINDS)


async def owner_text(message: Message) -> None:
    """Anything that is not a command: the list of commands."""
    await reply(message, t("help_owner"))


async def manager_text(message: Message) -> None:
    await reply(message, t("help_manager"))


def _user_filter(check: Callable[[int | None], bool]) -> Any:
    return F.from_user.id.func(check)


def build_owner_router() -> Router:
    router = Router(name="owner")
    router.message.filter(_user_filter(is_owner), F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(
        _user_filter(is_owner), F.message.chat.type == ChatType.PRIVATE
    )
    for name, handler in OWNER_COMMANDS.items():
        router.message.register(handler, Command(name))
    router.message.register(owner_text)
    router.callback_query.register(owner_tap)
    return router


def build_manager_router() -> Router:
    router = Router(name="manager")
    router.message.filter(_user_filter(is_manager), F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(
        _user_filter(is_manager), F.message.chat.type == ChatType.PRIVATE
    )
    for name, handler in MANAGER_COMMANDS.items():
        router.message.register(handler, Command(name))
    router.message.register(manager_text)
    router.callback_query.register(manager_tap)
    return router


# 06 §1.1: «logs rejected_update with the user id at most once per id per hour».
REJECT_LOG_EVERY: Final = timedelta(hours=1)
_rejected: dict[int, datetime] = {}


def note_rejected(user_id: int | None, *, now: datetime, what: str) -> bool:
    """Log a refused update; True when it was logged (once per id per hour)."""
    key = user_id if user_id is not None else 0
    last = _rejected.get(key)
    if last is not None and now - last < REJECT_LOG_EVERY:
        return False
    _rejected[key] = now
    log.warning("rejected_update", extra={"user_id": user_id, "update": what})
    return True


# A chat the bot stands in as one of these is one it was added to.
_PRESENT: Final = frozenset(
    {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.RESTRICTED}
)


def build_reject_router() -> Router:
    """Registered last: reached only when the routers above refused."""
    router = Router(name="reject")

    @router.message()
    async def _message(message: Message, deps: Deps) -> None:
        user = message.from_user.id if message.from_user else None
        note_rejected(user, now=deps.clock(), what="message")

    @router.callback_query()
    async def _tap(query: CallbackQuery, deps: Deps) -> None:
        note_rejected(query.from_user.id, now=deps.clock(), what="callback_query")

    @router.my_chat_member()
    async def _added(event: ChatMemberUpdated, bot: Bot, deps: Deps) -> None:
        """The bot never posts into a group or a channel: added to one, it
        leaves."""
        if event.chat.type == ChatType.PRIVATE:
            return
        if event.new_chat_member.status not in _PRESENT:
            return
        note_rejected(event.from_user.id, now=deps.clock(), what="added_to_chat")
        try:
            await bot.leave_chat(event.chat.id)
        except Exception as exc:
            log.warning("could not leave a chat", extra={"error": type(exc).__name__})

    return router


def build_routers() -> list[Router]:
    """Fresh routers (one router has one parent): owner, manager, reject."""
    return [build_owner_router(), build_manager_router(), build_reject_router()]


__all__ = [
    "HANDLERS",
    "MANAGER_COMMANDS",
    "MANAGER_KINDS",
    "OWNER_COMMANDS",
    "build_routers",
    "dispatch",
    "note_rejected",
]
