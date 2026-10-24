# VENDORED_FROM dailyJournal@6b1f67f miya/userbot/main.py
"""The read-only Telegram reader process (03 §2.3, 06 §2).

This process **never writes to Telegram**: no message, reaction, typing,
read receipt, invite or leave — ``tests/test_no_telegram_writes.py`` holds it
to that, and the only raw requests anywhere are the joiner's and the about
reader's (07 ``RAW_ALLOWED``). It connects with the dedicated reader account's
session string, follows new and edited messages in the chats it may read, and
stores the ones the prefilter keeps (``userbot/ingest.py``). Around that, one
loop beats the heartbeat every minute and starts the reader's own jobs at
their times, as a task of their own so a long one (a join day's 90 s gaps, a
night's backfill) never holds the beat back past ``USERBOT_STALE_MINUTES``:
the dialog sync (titles only), the daily join (``joiner.py``), the nightly
backfill (``backfill.py``) and the hourly about reads (``about.py``). Each of
those beats its own component.

Safety properties, all deliberate (MIYA's, adapted):

* ``USERBOT_ENABLED=false`` beats ``{"enabled": false}`` once and idles
  without building a client — idle, not exit: the container restarts
  ``unless-stopped``, and a clean exit would be restarted in a tight loop.
* Missing credentials end the process with the keys named; a session string
  that does not parse says so without echoing any of it.
* ``connect()``, never ``start()``: an unauthorised session would make
  ``start()`` prompt for a phone number on stdin, which in a container never
  returns. Not authorised, or restricted later (banned, revoked, a second
  client on the same session) → beat ``{"enabled": true, "authorized": false,
  "error": <code>}`` every minute and re-check every 15 minutes, no reconnect
  storm (06 §2). ``health`` reads that beat as ``userbot_unauthorized``.
  The check is ``get_me()``, asked anew each time (``authorization``) — never
  ``is_user_authorized()``, whose first answer Telethon keeps for the
  client's life: one timeout on it would read «unauthorised» for ever. A
  check Telegram could not answer (a timeout, a 500, a long flood wait) is
  not «unauthorised»: it beats ``authorized: null`` and asks again.
* ``flood_sleep_threshold=60``: Telethon sleeps through a short flood wait;
  a longer one reaches the job that met it, and past 300 s trips the 24 h
  breaker (``limits.trip_breaker``). Live messages keep flowing meanwhile.
* No media is downloaded and no member list is read, ever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession

from clientscrapper.config import settings
from clientscrapper.db.session import engine, session_scope
from clientscrapper.logs import setup_logging
from clientscrapper.services import chats, health
from clientscrapper.services.lifecycle import (
    idle_until_stopped,
    install_stop_signals,
    remove_stop_signals,
    run_main,
)
from clientscrapper.userbot import about, backfill, ingest, joiner, limits, tg

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
Clock = Callable[[], datetime]
Pause = Callable[[float], Awaitable[None]]

COMPONENT: Final = "userbot"
JOIN_COMPONENT: Final = health.collector_component(chats.SOURCE_KEY, "join")
BACKFILL_COMPONENT: Final = health.collector_component(chats.SOURCE_KEY, "backfill")
# Telethon's own default, kept: a flood wait up to this long is slept through.
READER_FLOOD_SLEEP_SECONDS: Final = 60
BEAT_SECONDS: Final = 60
RECHECK_UNAUTHORIZED: Final = timedelta(minutes=15)
CONNECT_ATTEMPTS: Final = 5
# On a stop the running job ends at its next check; past this it is cancelled
# (docker gives a container 10 s before SIGKILL).
STOP_GRACE_SECONDS: Final = 5.0
ABOUT_EVERY: Final = timedelta(hours=1)
DIALOG_SYNC_EVERY: Final = timedelta(hours=24)
# 03 §2.3 «backfill nightly 01:00»: once a night, inside this window.
BACKFILL_WINDOW: Final = (time(1, 0), time(6, 0))
UNAUTHORIZED: Final = "UNAUTHORIZED"


def _now() -> datetime:
    return datetime.now(settings.tz)


@dataclass(slots=True)
class Deps:
    """What the reader reaches outside Telegram, swapped for fakes in tests."""

    session_factory: SessionFactory = session_scope
    clock: Clock = _now
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    # Waits ``seconds`` or until ``stop`` is set; None = the real wait.
    pause: Pause | None = None
    rng: random.Random = field(default_factory=random.Random)

    async def wait(self, seconds: float) -> None:
        if self.pause is not None:
            await self.pause(seconds)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)


async def beat_userbot(deps: Deps, detail: dict[str, Any]) -> bool:
    """The reader's heartbeat. Never raises: a database hiccup is a log line,
    and the reader must not stop over a missed beat."""
    return await _beat(deps, COMPONENT, detail)


async def _beat(deps: Deps, component: str, detail: dict[str, Any]) -> bool:
    try:
        async with deps.session_factory() as session:
            await health.beat(session, component, detail=detail, now=deps.clock())
        return True
    except Exception as exc:
        log.warning(
            "heartbeat not written",
            extra={"component": component, "error": type(exc).__name__},
        )
        return False


async def _idle_forever() -> None:
    """The switched-off reader's whole life: wait for SIGTERM."""
    await idle_until_stopped()


# --- is the account signed in -------------------------------------------------------

# What a check that reached no answer raises: no network, a timeout
# (TimeoutError is an OSError), Telethon's «request was unsuccessful» after its
# own retries (ValueError), a 500 or a flood wait past the sleep threshold.
_UNANSWERED: Final = (OSError, ConnectionError, ValueError, errors.RPCError)


@dataclass(frozen=True, slots=True)
class Authorization:
    """What Telegram said about the reader account just now. ``authorized``
    is None when it could not be asked; ``code`` names why not."""

    authorized: bool | None
    me: Any = None
    code: str | None = None


async def authorization(client: Any) -> Authorization:
    """Ask Telegram whether the account is signed in, every time.

    ``get_me()`` sends ``users.getUsers([self])`` on each call and answers
    None for the 401 family (signed out, revoked, deactivated, banned). Not
    ``is_user_authorized()``: Telethon keeps its first answer in
    ``client._authorized`` and ``connect()`` never clears it, so after one
    transient error the 15-minute re-check would never reach Telegram again."""
    try:
        me = await client.get_me()
    except tg.RESTRICTION_ERRORS as exc:
        return Authorization(False, code=tg.error_code(exc))
    except _UNANSWERED as exc:
        return Authorization(None, code=tg.error_code(exc))
    if me is None:
        return Authorization(False)
    return Authorization(True, me=me)


# --- the jobs' clock -------------------------------------------------------------


@dataclass(slots=True)
class Schedule:
    """When each of the reader's jobs last ran (in memory: every job keeps its
    own restart-proof caps in the database, so a restart costs nothing)."""

    join_day: date | None = None
    join_at: dict[date, time] = field(default_factory=dict)
    backfill_day: date | None = None
    about_at: datetime | None = None
    dialogs_at: datetime | None = None

    def join_time(self, day: date, rng: random.Random) -> time:
        if day not in self.join_at:
            self.join_at = {day: joiner.join_minute(day, rng=rng)}
        return self.join_at[day]


def backfill_due(now: datetime, *, last_day: date | None) -> bool:
    local = now.astimezone(settings.tz)
    start, end = BACKFILL_WINDOW
    return last_day != local.date() and start <= local.time() < end


def every_due(now: datetime, *, last: datetime | None, every: timedelta) -> bool:
    return last is None or now - last >= every


# --- the dialog list (titles only, no history) -------------------------------------


async def sync_from_client(client: Any, deps: Deps, *, now: datetime) -> int:
    """Register every chat the account is in, and mark ``left`` the ones it
    no longer is (the owner left them by hand) — only from a list read to the
    end. Skipped while the flood breaker holds (06 §2). Returns how many rows
    were created."""
    async with deps.session_factory() as session:
        if await limits.flood_until(session, now=now) is not None:
            return 0
    found: list[chats.DialogInfo] = []
    try:
        async for dialog in client.iter_dialogs():
            entity = getattr(dialog, "entity", None)
            if entity is not None:
                found.append(tg.dialog_of(entity))
    except tg.FLOOD_ERRORS as exc:
        seconds = tg.flood_seconds(exc) or 0
        async with deps.session_factory() as session:
            await limits.trip_breaker(session, seconds=seconds, now=now, where="dialogs")
        log.warning("dialog sync stops on a flood wait", extra={"seconds": seconds})
        return 0
    async with deps.session_factory() as session:
        created, _ = await chats.sync_dialogs(session, found, now=now)
        left = await chats.mark_left(
            session, (d.tg_chat_id for d in found), listed_at=now
        )
    if left:
        log.info("chats left by hand", extra={"left": left})
    return created


# --- the minute: one beat, the due jobs ------------------------------------------------


async def _flood_detail(deps: Deps, now: datetime) -> str | None:
    try:
        async with deps.session_factory() as session:
            until = await limits.flood_until(session, now=now)
    except Exception:
        return None
    return until.isoformat() if until is not None else None


async def beat_serving(client: Any, deps: Deps, *, user_id: int | None) -> bool:
    """The authorised reader's minute beat."""
    now = deps.clock()
    return await beat_userbot(
        deps,
        {
            "enabled": True,
            "authorized": True,
            "connected": bool(client.is_connected()),
            "user": user_id,
            "flood_until": await _flood_detail(deps, now),
        },
    )


async def run_jobs(client: Any, deps: Deps, schedule: Schedule) -> None:
    """Run whatever job is due, one after the other, until a stop. A job that
    fails is logged and the reader goes on; a restriction (the account banned
    or signed out) is not a job's failure and is raised to the supervisor."""
    now = deps.clock()
    local = now.astimezone(settings.tz)
    stopped = deps.stop.is_set
    try:
        if every_due(now, last=schedule.dialogs_at, every=DIALOG_SYNC_EVERY):
            schedule.dialogs_at = now
            await sync_from_client(client, deps, now=now)
        at = schedule.join_time(local.date(), deps.rng)
        if not stopped() and joiner.join_due(now, at=at, last_day=schedule.join_day):
            schedule.join_day = local.date()
            report = await joiner.join_daily(
                client,
                deps.session_factory,
                clock=deps.clock,
                sleep=deps.wait,
                stopped=stopped,
            )
            await _beat(
                deps,
                JOIN_COMPONENT,
                {
                    "resolved": report.resolved,
                    "joined": len(report.joined),
                    "gone": report.gone,
                    "queued_linked": report.queued_linked,
                    "skipped": report.skipped,
                },
            )
        # A join day's run takes minutes: the later jobs ask the clock again.
        now = deps.clock()
        local = now.astimezone(settings.tz)
        if not stopped() and backfill_due(now, last_day=schedule.backfill_day):
            schedule.backfill_day = local.date()
            night = await backfill.backfill_nightly(
                client, deps.session_factory, now=now, stopped=stopped
            )
            await _beat(
                deps,
                BACKFILL_COMPONENT,
                {
                    "chats": night.chats,
                    "seen": night.seen,
                    "stored": night.stored,
                    "failed": night.failed,
                    "skipped": night.skipped,
                },
            )
        now = deps.clock()
        if not stopped() and every_due(now, last=schedule.about_at, every=ABOUT_EVERY):
            schedule.about_at = now
            await about.drain_about(client, deps.session_factory, now=now)
    except tg.RESTRICTION_ERRORS:
        raise
    except Exception as exc:
        log.warning("a reader job failed", extra={"error": type(exc).__name__})


# --- the live handlers ----------------------------------------------------------------


def handlers(
    deps: Deps,
) -> tuple[Callable[[Any], Awaitable[None]], Callable[[Any], Awaitable[None]]]:
    """The NewMessage and MessageEdited callbacks. Each message gets its own
    transaction, and one bad message never takes the reader down."""
    edits = ingest.EditCounter()

    async def on_new_message(event: Any) -> None:
        message = getattr(event, "message", None)
        try:
            async with deps.session_factory() as session:
                await ingest.handle_message(session, message, now=deps.clock())
        except Exception as exc:
            log.warning(
                "message not ingested",
                extra={
                    "tg_chat_id": getattr(message, "chat_id", None),
                    "tg_message_id": getattr(message, "id", None),
                    "error": type(exc).__name__,
                },
            )

    async def on_message_edited(event: Any) -> None:
        message = getattr(event, "message", None)
        try:
            async with deps.session_factory() as session:
                await ingest.handle_edit(session, message, now=deps.clock(), edits=edits)
        except Exception as exc:
            log.warning(
                "edit not handled",
                extra={
                    "tg_chat_id": getattr(message, "chat_id", None),
                    "tg_message_id": getattr(message, "id", None),
                    "error": type(exc).__name__,
                },
            )

    return on_new_message, on_message_edited


async def _settle(task: asyncio.Task[None]) -> BaseException | None:
    """Wait (≤ STOP_GRACE_SECONDS) for the jobs task, then cancel it; the
    restriction it raised, if any (``run_jobs`` keeps every other failure)."""
    done, _ = await asyncio.wait({task}, timeout=STOP_GRACE_SECONDS)
    if not done:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return None
    if task.cancelled():
        return None
    return task.exception()


async def serve(client: Any, deps: Deps, *, me: Any) -> None:
    """The authorised reader: handlers in, then the minute loop — beat, start
    the due jobs when the last run has ended — until stopped or disconnected
    for good. A restriction a job met is raised here, on the next minute."""
    user_id = getattr(me, "id", None)
    on_new_message, on_message_edited = handlers(deps)
    # Every chat's messages arrive here; ingest decides what may be read
    # (chats.readable): the chat list changes with every join and an owner
    # toggle, and re-registering events.NewMessage(chats=…) would race them.
    client.add_event_handler(on_new_message, events.NewMessage())
    client.add_event_handler(on_message_edited, events.MessageEdited())
    log.info("userbot connected (read-only)", extra={"user": user_id})
    try:
        try:
            # Whatever arrived while the process was down, through the same
            # handlers, now that they are in place.
            await client.catch_up()
        except tg.RESTRICTION_ERRORS:
            raise
        except Exception as exc:
            log.warning("catch-up failed", extra={"error": type(exc).__name__})
        schedule = Schedule()
        jobs: asyncio.Task[None] | None = None
        try:
            while not deps.stop.is_set():
                await beat_serving(client, deps, user_id=user_id)
                if jobs is not None and jobs.done():
                    failed = await _settle(jobs)
                    jobs = None
                    if failed is not None:
                        raise failed
                if jobs is None:
                    jobs = asyncio.create_task(run_jobs(client, deps, schedule))
                if not client.is_connected():
                    # auto_reconnect gave up; the supervisor connects again.
                    break
                await deps.wait(BEAT_SECONDS)
        finally:
            failed = await _settle(jobs) if jobs is not None else None
        if failed is not None and not deps.stop.is_set():
            raise failed
    finally:
        client.remove_event_handler(on_new_message)
        client.remove_event_handler(on_message_edited)


async def idle_unauthorized(
    client: Any, deps: Deps, *, code: str
) -> Authorization | None:
    """Beat ``authorized=false`` every minute, ask again every 15 minutes, no
    reconnect storm (06 §2). The answer once the account is authorised again;
    None when stopped. A re-check that names a new code shows it; one that
    only says «no» (``get_me`` hides the 401's code) keeps the code the
    reader met; one Telegram did not answer changes nothing."""
    log.error("the reader account is not authorised", extra={"code": code})
    deadline = deps.clock() + RECHECK_UNAUTHORIZED
    while not deps.stop.is_set():
        await beat_userbot(deps, {"enabled": True, "authorized": False, "error": code})
        await deps.wait(BEAT_SECONDS)
        if deps.stop.is_set():
            return None
        if deps.clock() < deadline:
            continue
        deadline = deps.clock() + RECHECK_UNAUTHORIZED
        try:
            if not client.is_connected():
                await client.connect()
        except (OSError, ConnectionError) as exc:
            log.warning("re-check could not connect", extra={"error": type(exc).__name__})
            continue
        answer = await authorization(client)
        if answer.authorized:
            return answer
        if answer.authorized is None:
            log.warning("re-check went unanswered", extra={"error": answer.code})
        elif answer.code is not None:
            code = answer.code
    return None


async def connect(client: Any, deps: Deps) -> None:
    """``connect()``, retried with backoff: a container often comes up before
    the network does, and one failed connect must not burn a restart."""
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            await client.connect()
            return
        except (OSError, ConnectionError) as exc:
            wait = 2**attempt * 5
            log.warning(
                "connect failed; retrying",
                extra={"error": type(exc).__name__, "wait_s": wait},
            )
            await deps.wait(wait)
    raise SystemExit(f"could not reach Telegram after {CONNECT_ATTEMPTS} attempts")


async def supervise(client: Any, deps: Deps) -> None:
    """Connect, then serve while authorised; idle (beating) while not; and
    while Telegram cannot say, beat that and ask again each minute."""
    await connect(client, deps)
    answer = await authorization(client)
    while not deps.stop.is_set():
        try:
            if answer.authorized is None:
                await beat_userbot(
                    deps,
                    {
                        "enabled": True,
                        "authorized": None,
                        "connected": bool(client.is_connected()),
                        "error": answer.code,
                    },
                )
                log.warning(
                    "authorisation check unanswered", extra={"error": answer.code}
                )
                await deps.wait(BEAT_SECONDS)
                if deps.stop.is_set():
                    return
                if not client.is_connected():
                    await connect(client, deps)
                answer = await authorization(client)
                continue
            if not answer.authorized:
                again = await idle_unauthorized(
                    client, deps, code=answer.code or UNAUTHORIZED
                )
                if again is None:
                    return
                answer = again
                continue
            await serve(client, deps, me=answer.me)
            if not deps.stop.is_set():
                await deps.wait(BEAT_SECONDS)
                if not client.is_connected():
                    await connect(client, deps)
                answer = await authorization(client)
        except tg.RESTRICTION_ERRORS as exc:
            answer = Authorization(False, code=tg.error_code(exc))


# --- process ---------------------------------------------------------------------------


def _credentials_missing() -> list[str]:
    named = (
        ("TELETHON_API_ID", settings.telethon_api_id),
        ("TELETHON_API_HASH", settings.telethon_api_hash),
        ("TELETHON_SESSION", settings.telethon_session),
    )
    return [key for key, value in named if not value]


async def run() -> None:
    setup_logging("userbot")
    if not settings.userbot_enabled:
        log.warning("USERBOT_ENABLED=false — the reader stays off")
        # Said once, so /holat shows the reader as switched off, not silent;
        # then the pool goes: an idle process holds no connection for weeks.
        await beat_userbot(Deps(), {"enabled": False})
        await engine.dispose()
        await _idle_forever()
        return
    missing = _credentials_missing()
    if missing:
        raise SystemExit(
            f"{', '.join(missing)} must be set when USERBOT_ENABLED=true "
            "(make userbot-login prints TELETHON_SESSION)"
        )
    # Never echo any part of the string: it is a full credential (06 §2).
    try:
        session = StringSession(settings.telethon_session)
    except Exception as exc:
        raise SystemExit(
            "TELETHON_SESSION is not a valid session string "
            f"({type(exc).__name__}, {len(settings.telethon_session)} characters): "
            "check .env for a repeated TELETHON_SESSION= prefix, quotes or a "
            "truncated paste, then make userbot-login"
        ) from None
    client = TelegramClient(
        session,
        settings.telethon_api_id,
        settings.telethon_api_hash,
        flood_sleep_threshold=READER_FLOOD_SLEEP_SECONDS,
    )
    deps = Deps()
    installed = install_stop_signals(deps.stop)
    try:
        await supervise(client, deps)
    finally:
        remove_stop_signals(installed)
        await client.disconnect()
        await engine.dispose()
        log.info("userbot stopped")


def main() -> int:
    return run_main(run)


if __name__ == "__main__":
    raise SystemExit(main())
