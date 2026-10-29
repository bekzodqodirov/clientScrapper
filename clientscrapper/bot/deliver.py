"""The one door through which the agent puts anything into a Telegram chat
(06 §1.1, §4.3, §5.3).

``to_person`` sends, edits or hands over a document — and refuses, before
any network call, every chat that is not the owner's (``OWNER_TELEGRAM_ID``)
or an allow-listed manager's (``MANAGER_TELEGRAM_IDS``): no group, no
channel, no prospect, ever. A document goes to the owner alone (the nightly
backup). ``tests/test_no_telegram_writes.py`` holds the shape: a Bot API
send is called only here, by ``to_person`` or a helper reached only from it.

The worker's and the bot's owner seams (``worker.set_owner_notifier``,
``costs.set_cap_notifier``, ``worker.set_backup_sender``) are
``owner_notifier`` / ``document_sender``: callables with no recipient
argument, the owner fixed inside.

HTML is the bot's parse mode; a text Telegram refuses to parse is sent again
as plain text (MIYA's ``_send``), so a broken tag costs formatting, never the
message. A message is clipped to Telegram's 4,096 characters.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message

from clientscrapper.config import settings

log = logging.getLogger(__name__)

TELEGRAM_MESSAGE_MAX: Final = 4096
# Telegram answers an edit that changes nothing with this; it is not a failure.
NOT_MODIFIED: Final = "message is not modified"


class NotAllowed(Exception):
    """A chat outside the owner/manager allowlist: nothing was sent."""


def clip(text: str) -> str:
    return (
        text
        if len(text) <= TELEGRAM_MESSAGE_MAX
        else text[: TELEGRAM_MESSAGE_MAX - 1] + "…"
    )


def make_bot(token: str | None = None) -> Bot:
    """The bot client with HTML as its parse mode (no network call)."""
    return Bot(
        token=token or settings.assistant_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def configured() -> bool:
    """A bot that may speak: a token and an owner (06 §1.1 — never an
    unrestricted bot)."""
    return bool(settings.assistant_bot_token) and settings.owner_telegram_id is not None


async def to_person(
    bot: Bot,
    telegram_id: int,
    text: str | None = None,
    *,
    keyboard: InlineKeyboardMarkup | None = None,
    edit_message_id: int | None = None,
    document: Path | None = None,
    caption: str | None = None,
) -> Message | bool | None:
    """Send ``text`` (or edit message ``edit_message_id`` to it, or send
    ``document``) to ``telegram_id`` — the owner or an allow-listed manager
    only, else ``NotAllowed`` before anything leaves. The sent message (an
    edit's: the message, or True when Telegram only says so), or None when
    Telegram did not take it."""
    owner = settings.owner_telegram_id
    managers = settings.manager_telegram_ids
    if owner is None or telegram_id <= 0:
        raise NotAllowed(telegram_id)
    if telegram_id != owner and telegram_id not in managers:
        raise NotAllowed(telegram_id)
    if document is not None:
        if telegram_id != owner:
            raise NotAllowed(telegram_id)
        return await _send_document(bot, telegram_id, document, caption)
    if text is None:
        raise ValueError("nothing to send")
    if edit_message_id is not None:
        return await _edit(bot, telegram_id, edit_message_id, clip(text), keyboard)
    return await _send(bot, telegram_id, clip(text), keyboard)


async def _send(
    bot: Bot, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None
) -> Message | None:
    try:
        return await bot.send_message(chat_id, text, reply_markup=keyboard)
    except TelegramBadRequest:
        log.warning("HTML send refused, retrying as plain text")
    except Exception as exc:
        log.warning("send failed", extra={"error": type(exc).__name__})
        return None
    try:
        return await bot.send_message(
            chat_id, text, reply_markup=keyboard, parse_mode=None
        )
    except Exception as exc:
        log.warning("plain-text send failed too", extra={"error": type(exc).__name__})
        return None


async def _edit(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> Message | bool | None:
    for parse_mode in (ParseMode.HTML, None):
        try:
            return await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode=parse_mode,
            )
        except TelegramBadRequest as exc:
            if NOT_MODIFIED in str(exc):
                return True
            log.warning("edit refused", extra={"plain": parse_mode is None})
        except Exception as exc:
            log.warning("edit failed", extra={"error": type(exc).__name__})
            return None
    return None


async def _send_document(
    bot: Bot, chat_id: int, path: Path, caption: str | None
) -> Message | None:
    try:
        return await bot.send_document(chat_id, FSInputFile(path), caption=caption)
    except Exception as exc:
        log.warning("document not sent", extra={"error": type(exc).__name__})
        return None


OwnerNotify = Callable[[str], Awaitable[bool]]


def owner_notifier(bot: Bot) -> OwnerNotify:
    """``(text) -> delivered`` to the owner: the worker's and the bot's
    alert seam. There is no recipient argument (06 §4.3)."""

    async def notify(text: str) -> bool:
        owner = settings.owner_telegram_id
        if owner is None:
            return False
        try:
            return await to_person(bot, owner, text) is not None
        except NotAllowed:
            return False

    return notify


def document_sender(bot: Bot) -> Callable[[Path, str], Awaitable[bool]]:
    """``(file, caption) -> delivered``: the nightly backup to the owner."""

    async def send(path: Path, caption: str) -> bool:
        owner = settings.owner_telegram_id
        if owner is None:
            return False
        try:
            return await to_person(bot, owner, document=path, caption=caption) is not None
        except NotAllowed:
            return False

    return send


__all__ = [
    "TELEGRAM_MESSAGE_MAX",
    "NotAllowed",
    "clip",
    "configured",
    "document_sender",
    "make_bot",
    "owner_notifier",
    "to_person",
]
