"""A Bot API that never leaves the process (07 §1.0: no real Telegram).

``FakeSession`` is an aiogram ``BaseSession``: every method the bot calls is
recorded as ``(name, fields)`` and answered with the object Telegram would
return — a ``Message`` for a send or an edit, ``True`` for an answer, the bot
user for ``getMe``, a ``File`` and its bytes for a download. ``fail`` queues
an exception for the next call of a method (``TelegramBadRequest`` to prove
the plain-text retry, a network error for ``getMe``'s backoff) — or for the
calls after the next ``after`` ones went through.

``owner_message`` / ``tap`` / ``added_to_group`` build the updates a test
feeds the dispatcher (``dp.feed_update``), bound to the bot like the ones
polling hands over.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import File, Message, Update, User

TOKEN = "42:TEST"
BOT_USER = {"id": 42, "is_bot": True, "first_name": "Agent", "username": "cs_agent_bot"}
_IDS = itertools.count(1000)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # None = a call that goes through (``fail(…, after=n)``).
        self.failures: dict[str, list[Exception | None]] = {}
        self.files: dict[str, bytes] = {}

    def fail(
        self, method: str, error: Exception, times: int = 1, *, after: int = 0
    ) -> None:
        queued = self.failures.setdefault(method, [])
        queued.extend([None] * after)
        queued.extend([error] * times)

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        name = type(method).__name__
        fields = method.model_dump(exclude_none=True)
        self.calls.append((name, fields))
        queued = self.failures.get(name)
        if queued:
            failure = queued.pop(0)
            if failure is not None:
                raise failure
        if name in {
            "SendMessage",
            "SendDocument",
            "EditMessageText",
            "EditMessageReplyMarkup",
        }:
            chat_id = fields.get("chat_id", 1)
            message_id = fields.get("message_id") or next(_IDS)
            return Message.model_validate(
                {
                    "message_id": message_id,
                    "date": datetime.now(UTC),
                    "chat": {"id": chat_id, "type": "private"},
                    "from": BOT_USER,
                    "text": fields.get("text"),
                },
                context={"bot": bot},
            )
        if name == "GetMe":
            return User.model_validate(BOT_USER, context={"bot": bot})
        if name == "GetFile":
            file_id = fields["file_id"]
            return File.model_validate(
                {
                    "file_id": file_id,
                    "file_unique_id": f"u{file_id}",
                    "file_size": len(self.files.get(file_id, b"")),
                    "file_path": f"documents/{file_id}",
                },
                context={"bot": bot},
            )
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        file_id = url.rsplit("/", 1)[-1]
        yield self.files[file_id]

    # --- what the tests read ------------------------------------------------------

    def named(self, *names: str) -> list[dict[str, Any]]:
        return [fields for name, fields in self.calls if name in names]

    def texts(self) -> list[str]:
        """Every text the bot sent or edited to, in order."""
        return [f["text"] for f in self.named("SendMessage", "EditMessageText")]

    def chats(self) -> set[int]:
        """Every chat anything was put into."""
        return {
            int(f["chat_id"])
            for f in self.named(
                "SendMessage", "SendDocument", "EditMessageText", "EditMessageReplyMarkup"
            )
        }

    def answers(self) -> list[str | None]:
        return [f.get("text") for f in self.named("AnswerCallbackQuery")]

    def clear(self) -> None:
        self.calls.clear()


def fake_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    bot = Bot(
        token=TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    return bot, session


_UPDATES = itertools.count(1)


def _user(user_id: int) -> dict[str, Any]:
    return {"id": user_id, "is_bot": False, "first_name": "Namuna"}


def message(
    bot: Bot,
    text: str | None,
    *,
    user_id: int,
    chat_type: str = "private",
    chat_id: int | None = None,
    document: dict[str, Any] | None = None,
    caption: str | None = None,
    reply_to: dict[str, Any] | None = None,
) -> Update:
    data: dict[str, Any] = {
        "message_id": next(_IDS),
        "date": datetime.now(UTC),
        "chat": {"id": chat_id if chat_id is not None else user_id, "type": chat_type},
        "from": _user(user_id),
    }
    if text is not None:
        data["text"] = text
        if text.startswith("/"):
            data["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
    if caption is not None:
        data["caption"] = caption
        if caption.startswith("/"):
            data["caption_entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(caption.split()[0])}
            ]
    if document is not None:
        data["document"] = document
    if reply_to is not None:
        data["reply_to_message"] = reply_to
    return Update.model_validate(
        {"update_id": next(_UPDATES), "message": data}, context={"bot": bot}
    )


def tap(
    bot: Bot,
    data: str,
    *,
    user_id: int,
    message_id: int = 77,
    chat_type: str = "private",
    text: str = "karta",
) -> Update:
    return Update.model_validate(
        {
            "update_id": next(_UPDATES),
            "callback_query": {
                "id": str(next(_IDS)),
                "from": _user(user_id),
                "chat_instance": "ci",
                "data": data,
                "message": {
                    "message_id": message_id,
                    "date": datetime.now(UTC),
                    "chat": {"id": user_id, "type": chat_type},
                    "from": BOT_USER,
                    "text": text,
                },
            },
        },
        context={"bot": bot},
    )


def added_to_group(bot: Bot, *, by: int, chat_id: int = -100500) -> Update:
    member = {"user": BOT_USER, "status": "member"}
    return Update.model_validate(
        {
            "update_id": next(_UPDATES),
            "my_chat_member": {
                "chat": {"id": chat_id, "type": "supergroup", "title": "Guruh"},
                "from": _user(by),
                "date": datetime.now(UTC),
                "old_chat_member": {"user": BOT_USER, "status": "left"},
                "new_chat_member": member,
            },
        },
        context={"bot": bot},
    )


def document(file_id: str, name: str, size: int) -> dict[str, Any]:
    return {
        "file_id": file_id,
        "file_unique_id": f"u{file_id}",
        "file_name": name,
        "file_size": size,
    }
