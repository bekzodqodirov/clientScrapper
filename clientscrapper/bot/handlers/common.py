"""What every handler shares: the dependencies (swapped for fakes in tests),
who may talk to the bot, the confirm plans kept in memory, and the reply
helper (MIYA's ``_safe_answer``: HTML, then plain text).

**Who.** ``is_owner`` fails closed — with ``OWNER_TELEGRAM_ID`` unset nobody
is the owner (06 §1.1), so an unconfigured bot answers nobody. A manager is
an id of ``MANAGER_TELEGRAM_IDS``; only private chats are served at all.

**Plans.** ``/import`` and ``/dnc`` keep what they would write for 10
minutes under a random id the confirm button carries (06 §1.2: «plan kept
in memory 10 min (restart = send again)»). A random id, not a counter: after
a restart an old button must not name a new plan. ``/unut``'s plan is kept
the same way under its ``purge_log`` id (the button is ``pg:<purge_log_id>``,
06 §1.5): the log row holds only a hash, so the subject lives here.
"""

from __future__ import annotations

import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.bot.callbacks import Callback
from clientscrapper.bot.deliver import clip
from clientscrapper.config import settings
from clientscrapper.db.session import session_scope
from clientscrapper.services import erp_feeds
from clientscrapper.services.erp_feeds import ErpClient

log = logging.getLogger(__name__)

PLAN_TTL: Final = timedelta(minutes=10)
PLAN_ID_MAX: Final = 9_999_999_999

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def _now() -> datetime:
    return datetime.now(settings.tz)


ErpFactory = Callable[[], AbstractAsyncContextManager[ErpClient | None]]


@asynccontextmanager
async def _erp() -> AsyncIterator[ErpClient | None]:
    """The ERP client for ``/push tekshir``, closed after the tap; None
    without the agent key or with a ``CRM_BASE_URL`` the client refuses
    (``keys.erp_base_refusal``: the key is never sent in clear)."""
    if not settings.crm_agent_key:
        yield None
        return
    try:
        client = erp_feeds.client_from_settings()
    except ValueError as exc:
        log.warning("no ERP client", extra={"error": str(exc)})
        yield None
        return
    try:
        yield client
    finally:
        await client.aclose()


@dataclass(slots=True)
class Plans:
    """Confirm plans in memory: ``put`` → an id, ``take`` → the plan once,
    while it is fresh."""

    ttl: timedelta = PLAN_TTL
    rng: random.Random = field(default_factory=random.SystemRandom)
    _items: dict[int, tuple[str, datetime, Any]] = field(default_factory=dict)

    def put(
        self, kind: str, value: Any, *, now: datetime, plan_id: int | None = None
    ) -> int:
        """Keep ``value`` under a fresh random id — or under ``plan_id``, an id
        the database already gave the plan (``/unut``'s ``purge_log`` row)."""
        self._sweep(now)
        while plan_id is None:
            drawn = self.rng.randint(1, PLAN_ID_MAX)
            if drawn not in self._items:
                plan_id = drawn
        self._items[plan_id] = (kind, now, value)
        return plan_id

    def take(self, kind: str, plan_id: int, *, now: datetime) -> Any | None:
        self._sweep(now)
        found = self._items.get(plan_id)
        if found is None or found[0] != kind:
            return None
        del self._items[plan_id]
        return found[2]

    def _sweep(self, now: datetime) -> None:
        for key in [k for k, (_, at, _) in self._items.items() if now - at > self.ttl]:
            del self._items[key]


@dataclass(slots=True)
class Deps:
    """What the handlers reach outside Telegram."""

    session_factory: SessionFactory = session_scope
    clock: Callable[[], datetime] = _now
    erp: ErpFactory = _erp
    plans: Plans = field(default_factory=Plans)
    # The answer of a review button (T-21's services/reviews): None = not
    # wired, the tap is answered «not ready» and nothing changes.
    review_answer: Callable[[Tap, Any], Awaitable[str | None]] | None = None


@dataclass(frozen=True, slots=True)
class Tap:
    """One button press, parsed: what every callback handler receives."""

    query: CallbackQuery
    data: Callback
    deps: Deps
    user_id: int


TapHandler = Callable[[Tap], Awaitable[None]]


# --- who -------------------------------------------------------------------------------


def is_owner(user_id: int | None) -> bool:
    """Fail closed: with OWNER_TELEGRAM_ID unset, nobody is the owner."""
    owner = settings.owner_telegram_id
    return owner is not None and user_id is not None and user_id == owner


def is_manager(user_id: int | None) -> bool:
    return (
        settings.owner_telegram_id is not None
        and user_id is not None
        and user_id in settings.manager_telegram_ids
    )


def private_message(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE


def private_tap(query: CallbackQuery) -> bool:
    message = query.message
    return message is not None and message.chat.type == ChatType.PRIVATE


# --- replies --------------------------------------------------------------------------


async def reply(
    message: Message, text: str, *, keyboard: InlineKeyboardMarkup | None = None
) -> Message | None:
    """Answer the chat the command came from (the routers admitted only the
    owner or a manager), clipped to Telegram's 4,096 characters: HTML, then —
    only when Telegram refused the HTML — plain text; None when nothing went
    out. A network failure is not retried: Telegram may have taken the
    message, and a second copy is worse than none."""
    text = clip(text)
    for parse_mode in ("HTML", None):
        try:
            return await message.answer(
                text, reply_markup=keyboard, parse_mode=parse_mode
            )
        except TelegramBadRequest as exc:
            log.warning(
                "reply refused",
                extra={"error": type(exc).__name__, "plain": parse_mode is None},
            )
        except Exception as exc:
            log.warning("reply not sent", extra={"error": type(exc).__name__})
            return None
    return None


async def edit(
    query: CallbackQuery, text: str, *, keyboard: InlineKeyboardMarkup | None = None
) -> bool:
    """Edit the message the button sits on (same message, keyboard replaced
    or removed)."""
    message = query.message
    if not isinstance(message, Message):
        return False
    text = clip(text)
    for parse_mode in ("HTML", None):
        try:
            await message.edit_text(text, reply_markup=keyboard, parse_mode=parse_mode)
            return True
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return True
            log.warning(
                "edit refused",
                extra={"error": type(exc).__name__, "plain": parse_mode is None},
            )
        except Exception as exc:
            log.warning("edit not applied", extra={"error": type(exc).__name__})
            return False
    return False


async def edit_keyboard(
    query: CallbackQuery, keyboard: InlineKeyboardMarkup | None
) -> bool:
    message = query.message
    if not isinstance(message, Message):
        return False
    try:
        await message.edit_reply_markup(reply_markup=keyboard)
        return True
    except Exception as exc:
        if "message is not modified" in str(exc):
            return True
        log.warning("keyboard not replaced", extra={"error": type(exc).__name__})
        return False


async def toast(
    query: CallbackQuery, text: str | None = None, *, alert: bool = False
) -> None:
    """Answer the button (it stops spinning); never raises."""
    try:
        await query.answer(text, show_alert=alert)
    except Exception as exc:
        log.warning("callback not answered", extra={"error": type(exc).__name__})


def args_of(message: Message) -> str:
    """What follows the command word: «/vazn hajm 30» → «hajm 30»."""
    text = message.text or message.caption or ""
    head, _, rest = text.strip().partition(" ")
    return rest.strip() if head.startswith("/") else ""


__all__ = [
    "PLAN_TTL",
    "Deps",
    "ErpFactory",
    "Plans",
    "SessionFactory",
    "Tap",
    "TapHandler",
    "args_of",
    "edit",
    "edit_keyboard",
    "is_manager",
    "is_owner",
    "private_message",
    "private_tap",
    "reply",
    "toast",
]
