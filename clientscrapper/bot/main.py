# VENDORED_FROM dailyJournal@6b1f67f miya/bot/main.py
"""The owner bot process (06 §1.1): aiogram 3 long polling.

It refuses to start without ``ASSISTANT_BOT_TOKEN`` and ``OWNER_TELEGRAM_ID``
(never an unrestricted bot), reaches Telegram with ``get_me`` (×5, a
container often starts before the network), sets the command menus (the
owner's and each manager's, nobody else's), and then runs three things until
stopped:

* polling with ``drop_pending_updates=False`` (MIYA: what the owner sent
  while the bot restarted is not thrown away; Telegram keeps a day);
* the ``bot`` heartbeat every minute (06 §4.1), ``{"telegram": true,
  "username": …}``;
* the 5-minute watchdog: the worker is the only process that speaks
  unprompted, so its silence is the one failure nothing else would report.
  The watchdog judges ``worker_silent`` and ``db_down`` alone (every other
  key is the worker's health sweep's), through the same alert ledger — so
  neither process repeats the other — and, with the database gone, an
  in-memory memo the next reachable tick writes into the ledger.

The process wires the seams every process that may refuse a paid call or
judge health registers (``costs.set_anthropic_health``,
``costs.set_cap_notifier`` with the quiet hours, the suppression probe).
``@dp.errors()`` answers «⚠️ Xatolik yuz berdi …» so a pressed button never
spins.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any, Final

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from aiogram.types.error_event import ErrorEvent
from sqlalchemy import exc as sa_exc

from clientscrapper.bot import deliver, handlers
from clientscrapper.bot.handlers.common import Deps
from clientscrapper.bot.texts import COMMAND_MENU, MANAGER_MENU, t
from clientscrapper.config import settings
from clientscrapper.db.session import engine, session_scope
from clientscrapper.logs import setup_logging
from clientscrapper.services import costs, erp_feeds, health
from clientscrapper.services.lifecycle import (
    install_stop_signals,
    remove_stop_signals,
    run_main,
)

log = logging.getLogger(__name__)

BEAT_EVERY: Final = timedelta(minutes=1)
WATCHDOG_EVERY: Final = timedelta(minutes=5)
GET_ME_ATTEMPTS: Final = 5
# What the watchdog speaks for; every other key is the worker's sweep's.
WATCHED: Final = frozenset({"worker_silent", "db_down"})
UNWATCHED: Final = frozenset(health.PROBLEM_KEYS) - WATCHED

Sleep = Callable[[float], Awaitable[None]]
Polling = Callable[[Dispatcher, Bot], Coroutine[Any, Any, None]]


# --- the dispatcher -------------------------------------------------------------------


def build_dispatcher(deps: Deps | None = None) -> Dispatcher:
    dp = Dispatcher(deps=deps or Deps())
    for router in handlers.build_routers():
        dp.include_router(router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> None:
        # Without this an unhandled exception (the database, foremost) means
        # the message disappears and a pressed button spins for ever.
        log.error(
            "unhandled error in an update",
            extra={"error": type(event.exception).__name__},
        )
        down = isinstance(event.exception, sa_exc.DBAPIError | OSError | TimeoutError)
        text = t("db_down" if down else "error")
        update = event.update
        try:
            if update.callback_query is not None:
                await update.callback_query.answer(text, show_alert=True)
            elif update.message is not None:
                await update.message.answer(text, parse_mode=None)
        except Exception:
            log.warning("could not even report the error")

    return dp


# --- the seams ------------------------------------------------------------------------


def wire(bot: Bot) -> health.OwnerNotify:
    """The hooks this process registers; the owner notifier it speaks with."""
    notify = deliver.owner_notifier(bot)
    costs.set_anthropic_health(health.anthropic_down_until)
    costs.set_cap_notifier(notify, hold=health.in_quiet_hours)
    erp_feeds.register_health_probe()
    return notify


# --- the watchdog ---------------------------------------------------------------------


_offline: dict[str, datetime] = {}


async def watchdog_tick(
    notify: health.OwnerNotify,
    *,
    now: datetime | None = None,
    memo: dict[str, datetime] | None = None,
) -> health.TellReport | None:
    """One pass over ``worker_silent`` and ``db_down``. Never raises."""
    now = now or datetime.now(settings.tz)
    memo = _offline if memo is None else memo
    status: health.Status | None = None
    try:
        async with session_scope() as session:
            status = await health.gather(session, now=now)
            if status.db_ok:
                await health.adopt_offline(memo)
                found = [p for p in health.problems(status) if p.key in WATCHED]
                return await health.tell(
                    session, found, notify, now=now, unjudged=UNWATCHED
                )
    except Exception as exc:
        if status is not None and status.db_ok:
            log.warning("watchdog tick failed", extra={"error": type(exc).__name__})
            return None
    if status is None:
        return None
    found = [p for p in health.problems(status) if p.key in WATCHED]
    with contextlib.suppress(Exception):
        await health.tell_offline(found, notify, now=now, memo=memo)
    return None


async def watchdog_loop(notify: health.OwnerNotify, stop: asyncio.Event) -> None:
    """Sleep first: the start-up beat covered this minute."""
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=WATCHDOG_EVERY.total_seconds())
            return
        await watchdog_tick(notify)


# --- start ----------------------------------------------------------------------------


async def reach(bot: Bot, *, sleep: Sleep = asyncio.sleep) -> Any:
    """``get_me`` with MIYA's backoff (5, 10, 20, 40 s); SystemExit after 5."""
    for attempt in range(GET_ME_ATTEMPTS):
        try:
            return await bot.get_me()
        except Exception as exc:
            if attempt == GET_ME_ATTEMPTS - 1:
                break
            wait = 2**attempt * 5
            log.warning(
                "get_me failed; retrying",
                extra={"error": type(exc).__name__, "wait_s": wait},
            )
            await sleep(wait)
    raise SystemExit(t("start.no_telegram"))


async def set_commands(bot: Bot) -> None:
    """The owner's menu in the owner's chat, the managers' in theirs, and
    none by default (a stranger is shown nothing). A manager who has not
    /start-ed the bot is a warning, not a stop."""
    calls: list[tuple[list[BotCommand], Any]] = [([], BotCommandScopeDefault())]
    owner = settings.owner_telegram_id
    if owner is not None:
        menu = [BotCommand(command=c, description=d) for c, d in COMMAND_MENU]
        calls.append((menu, BotCommandScopeChat(chat_id=owner)))
    for manager in settings.manager_telegram_ids:
        if manager == owner:
            continue
        menu = [BotCommand(command=c, description=d) for c, d in MANAGER_MENU]
        calls.append((menu, BotCommandScopeChat(chat_id=manager)))
    for commands, scope in calls:
        try:
            if commands:
                await bot.set_my_commands(commands, scope=scope)
            else:
                await bot.delete_my_commands(scope=scope)
        except TelegramAPIError as exc:
            log.warning("command menu not set", extra={"error": type(exc).__name__})


async def _poll(dp: Dispatcher, bot: Bot) -> None:
    # Deliberately NOT drop_pending_updates (MIYA): a restart must not lose
    # what the owner sent meanwhile.
    await dp.start_polling(bot, drop_pending_updates=False, handle_signals=False)


def refusal() -> str | None:
    """Why the bot must not start (06 §1.1), in the owner's words."""
    if not settings.assistant_bot_token:
        return t("start.no_token")
    if settings.owner_telegram_id is None:
        return t("start.no_owner")
    return None


async def run(
    *,
    bot: Bot | None = None,
    polling: Polling = _poll,
    sleep: Sleep = asyncio.sleep,
    deps: Deps | None = None,
) -> None:
    setup_logging("bot")
    why = refusal()
    if why is not None:
        raise SystemExit(why)
    bot = bot or deliver.make_bot()
    stop = asyncio.Event()
    installed = install_stop_signals(stop)
    tasks: list[asyncio.Task[Any]] = []
    try:
        me = await reach(bot, sleep=sleep)
        identity: dict[str, Any] = {"telegram": True, "username": me.username}
        log.info("bot ready", extra={"username": me.username})
        await set_commands(bot)
        notify = wire(bot)
        dp = build_dispatcher(deps)
        tasks = [
            asyncio.create_task(
                health.keep_beating(
                    "bot", every=BEAT_EVERY, detail=lambda: dict(identity), stop=stop
                ),
                name="beat",
            ),
            asyncio.create_task(watchdog_loop(notify, stop), name="watchdog"),
        ]
        poll = asyncio.create_task(polling(dp, bot), name="polling")
        stopper = asyncio.create_task(stop.wait(), name="stop")
        await asyncio.wait({poll, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stop.set()
        with contextlib.suppress(Exception):
            await dp.stop_polling()
        for task in (poll, stopper, *tasks):
            if not task.done():
                task.cancel()
        for task in (poll, stopper, *tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    finally:
        remove_stop_signals(installed)
        costs.set_cap_notifier(None)
        with contextlib.suppress(Exception):
            await bot.session.close()
        await engine.dispose()
    log.info("bot stopped")


def main() -> int:
    return run_main(run)


if __name__ == "__main__":
    raise SystemExit(main())
