"""The reader process (06 §2; 07 T-13): kill switch, credentials, the
unauthorised idle, restriction, serving, the jobs' clock.

``supervise`` runs against ``FakeTelegramClient`` with a fake clock whose
``pause`` advances time and can stop the loop, so a 15-minute re-check takes
no time and every heartbeat it writes is read back from Postgres.
"""

from __future__ import annotations

import asyncio
import itertools
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from telethon import errors

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.db.session import session_scope
from clientscrapper.services import chats, health
from clientscrapper.userbot import main as userbot
from clientscrapper.userbot import tg
from tests.fake_telegram import FakeTelegramClient, channel, user

TASHKENT = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=TASHKENT)


class Ticker:
    """The reader's clock and its pauses: every pause advances the clock by
    what was asked; ``stop_after`` pauses set the stop event."""

    def __init__(
        self, deps: userbot.Deps, *, stop_after: int, start: datetime = NOW
    ) -> None:
        self.deps = deps
        self.t = start
        self.pauses: list[float] = []
        self.stop_after = stop_after
        self.on_pause: Callable[[int], None] | None = None

    def now(self) -> datetime:
        return self.t

    async def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.t += timedelta(seconds=seconds)
        if self.on_pause is not None:
            self.on_pause(len(self.pauses))
        if len(self.pauses) >= self.stop_after:
            self.deps.stop.set()
        await asyncio.sleep(0)


def _deps(*, stop_after: int, start: datetime = NOW) -> tuple[userbot.Deps, Ticker]:
    deps = userbot.Deps(session_factory=session_scope)
    ticker = Ticker(deps, stop_after=stop_after, start=start)
    deps.clock = ticker.now
    deps.pause = ticker.pause
    return deps, ticker


async def _beat(session, component: str = "userbot") -> m.Heartbeat:
    rows = await health.beats(session)
    return rows[component]


# --- unauthorised and restricted ------------------------------------------------------


async def test_an_unauthorised_session_beats_and_idles_rechecking_every_15_minutes(
    session,
):
    client = FakeTelegramClient(authorized=(False,))
    deps, ticker = _deps(stop_after=31)

    await userbot.supervise(client, deps)

    beat = await _beat(session)
    assert beat.detail == {"enabled": True, "authorized": False, "error": "UNAUTHORIZED"}
    assert beat.last_seen_at == NOW + timedelta(minutes=30)
    # Asked once on connect, then once per 15 minutes — no reconnect storm —
    # and each time really asked (get_me), never Telethon's kept answer.
    assert len(client.named("get_me")) == 3
    assert client.named("is_user_authorized") == []
    assert len(client.named("connect")) == 1
    assert ticker.pauses == [60] * 31
    # Never served: no handler, no dialog read, no request.
    assert client.handlers == []
    assert client.named("iter_dialogs") == []


async def test_a_restricted_account_beats_its_code_and_idles(session):
    client = FakeTelegramClient()
    # A 400 get_me raises (the 401 family it answers as None).
    client.fail[("get_me", None)] = errors.PhoneNumberBannedError(request=None)
    deps, _ = _deps(stop_after=3)

    await userbot.supervise(client, deps)

    beat = await _beat(session)
    assert beat.detail == {
        "enabled": True,
        "authorized": False,
        "error": "PHONE_NUMBER_BANNED",
    }
    assert client.handlers == []


async def test_a_restriction_a_job_meets_reaches_the_supervisor_and_keeps_its_code(
    session,
):
    # Signed in at the start; the dialog sync (the first job) meets a ban.
    # The re-check 15 minutes later only hears «no» (get_me hides a 401's
    # code), so the code the job met stays on the beat.
    client = FakeTelegramClient(authorized=(True, False))
    client.fail[("iter_dialogs", None)] = errors.UserDeactivatedBanError(request=None)
    deps, ticker = _deps(stop_after=18)

    await userbot.supervise(client, deps)

    beat = await _beat(session)
    assert beat.detail == {
        "enabled": True,
        "authorized": False,
        "error": "USER_DEACTIVATED_BAN",
    }
    assert len(client.named("iter_dialogs")) == 1
    assert len(client.named("get_me")) == 2  # the start, the 15-minute re-check
    assert client.handlers == []


async def test_an_account_authorised_again_is_served_again(session):
    client = FakeTelegramClient(authorized=(False, True))
    deps, ticker = _deps(stop_after=17)
    served: list[str] = []
    ticker.on_pause = lambda n: served.extend(
        str(c.target) for c in client.named("add_event_handler")[len(served) :]
    )

    await userbot.supervise(client, deps)

    assert len(client.named("get_me")) == 2
    assert served == ["NewMessage", "MessageEdited"]
    beat = await _beat(session)
    assert beat.detail["authorized"] is True


async def test_an_unanswered_check_is_not_unauthorised_and_is_asked_again(session):
    # Telegram does not answer the first question (a timeout). Telethon would
    # keep that as «not authorised» for the client's life; the reader says it
    # could not tell, asks again a minute later and serves.
    client = FakeTelegramClient(authorized=(True,))
    client.unanswered = [errors.TimedOutError(request=None, message="Timeout")]
    deps, ticker = _deps(stop_after=2)
    first: list[dict[str, object]] = []

    async def read_beat() -> None:
        async with session_scope() as other:
            first.append(dict((await _beat(other)).detail))

    pending: list[asyncio.Task[None]] = []
    ticker.on_pause = lambda n: (
        pending.append(asyncio.ensure_future(read_beat())) if n == 1 else None
    )

    await userbot.supervise(client, deps)
    await asyncio.gather(*pending)

    assert first == [
        {"enabled": True, "authorized": None, "connected": True, "error": "Timeout"}
    ]
    beat = await _beat(session)
    assert (beat.detail["authorized"], beat.detail["user"]) == (True, 5100000001)
    assert len(client.named("get_me")) == 2
    assert client.named("is_user_authorized") == []


# --- serving --------------------------------------------------------------------------


async def test_serving_registers_the_two_read_handlers_syncs_dialogs_and_beats(session):
    group = channel(6100, title="Namuna Optom", username="namuna_optom6", megagroup=True)
    private = user(
        5500000001, first_name="Namuna", last_name="Mijoz", username="namuna_mijoz"
    )
    client = FakeTelegramClient(dialogs=[group, private])
    deps, ticker = _deps(stop_after=1)
    registered: list[list[str]] = []

    def watch(count: int) -> None:
        registered.append([str(c.target) for c in client.named("add_event_handler")])

    ticker.on_pause = watch

    await userbot.supervise(client, deps)

    assert registered == [["NewMessage", "MessageEdited"]]
    # Removed again on the way out.
    assert client.handlers == []
    assert len(client.named("catch_up")) == 1
    beat = await _beat(session)
    assert beat.detail == {
        "enabled": True,
        "authorized": True,
        "connected": True,
        "user": 5100000001,
        "flood_until": None,
    }
    rows = {r.tg_chat_id: r for r in (await chats.list_monitors(session, limit=10))[0]}
    gr = rows[tg.peer_id(group)]
    assert (gr.status, gr.chat_type, gr.monitor_enabled, gr.decided_by) == (
        "joined",
        "group",
        True,
        "rule:default",
    )
    pr = rows[tg.peer_id(private)]
    # A private dialog: registered off, with no name or handle kept.
    assert (pr.chat_type, pr.monitor_enabled, pr.decided_by, pr.title, pr.username) == (
        "private",
        False,
        "rule:private_off",
        None,
        None,
    )


async def test_the_live_handler_stores_through_the_registered_callback(session):
    group = channel(6200, title="Namuna Bozor", username="namuna_bozor6", megagroup=True)
    client = FakeTelegramClient(dialogs=[group])
    deps, ticker = _deps(stop_after=1)
    from tests.fake_telegram import message

    async def deliver(count: int) -> None:
        on_new = client.handlers[0][0]
        msg = message(
            group, 1, "Guangzhoudan 2 kub mato kerak, kim olib keladi?", date=NOW
        )
        await on_new(type("Event", (), {"message": msg})())

    pending: list[asyncio.Task[None]] = []
    ticker.on_pause = lambda n: pending.append(asyncio.ensure_future(deliver(n)))

    await userbot.supervise(client, deps)
    await asyncio.gather(*pending)

    import sqlalchemy as sa

    texts = list(await session.scalars(sa.select(m.Interaction.text)))
    assert texts == ["Guangzhoudan 2 kub mato kerak, kim olib keladi?"]


def test_the_jobs_clock():
    day = NOW.date()
    at_1 = NOW.replace(hour=1, minute=0)
    assert userbot.backfill_due(at_1, last_day=None)
    assert not userbot.backfill_due(at_1, last_day=day)
    assert not userbot.backfill_due(NOW.replace(hour=0, minute=59), last_day=None)
    assert not userbot.backfill_due(NOW.replace(hour=6, minute=0), last_day=None)
    assert userbot.every_due(NOW, last=None, every=timedelta(hours=1))
    assert not userbot.every_due(
        NOW, last=NOW - timedelta(minutes=59), every=timedelta(hours=1)
    )
    assert userbot.every_due(NOW, last=NOW - timedelta(hours=1), every=timedelta(hours=1))
    schedule = userbot.Schedule()
    import random

    first = schedule.join_time(day, random.Random(1))
    assert schedule.join_time(day, random.Random(2)) == first  # fixed for the day


# --- the process ----------------------------------------------------------------------


async def test_the_kill_switch_beats_switched_off_and_never_builds_a_client(
    session, monkeypatch
):
    monkeypatch.setattr(settings, "userbot_enabled", False)
    idled: list[bool] = []

    async def _idle() -> None:
        idled.append(True)

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("TelegramClient was constructed with the switch off")

    monkeypatch.setattr(userbot, "TelegramClient", _explode)
    monkeypatch.setattr(userbot, "_idle_forever", _idle)
    monkeypatch.setattr(userbot, "setup_logging", lambda *a, **k: None)

    await userbot.run()

    assert idled == [True]
    beat = await _beat(session)
    assert beat.detail == {"enabled": False}


async def test_missing_credentials_name_every_missing_key(monkeypatch):
    monkeypatch.setattr(settings, "userbot_enabled", True)
    monkeypatch.setattr(settings, "telethon_api_id", None)
    monkeypatch.setattr(settings, "telethon_api_hash", "")
    monkeypatch.setattr(settings, "telethon_session", "")
    monkeypatch.setattr(userbot, "setup_logging", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exit_:
        await userbot.run()
    assert "TELETHON_API_ID, TELETHON_API_HASH, TELETHON_SESSION" in str(exit_.value.code)


async def test_a_broken_session_string_is_refused_without_echoing_it(monkeypatch):
    secret = "1" + "Q" * 40 + "notasession"
    monkeypatch.setattr(settings, "userbot_enabled", True)
    monkeypatch.setattr(settings, "telethon_api_id", 12345)
    monkeypatch.setattr(settings, "telethon_api_hash", "0" * 32)
    monkeypatch.setattr(settings, "telethon_session", secret)
    monkeypatch.setattr(userbot, "setup_logging", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exit_:
        await userbot.run()
    text = str(exit_.value.code)
    assert "TELETHON_SESSION is not a valid session string" in text
    assert secret not in text and secret[:6] not in text
    assert f"{len(secret)} characters" in text


async def test_connect_gives_up_after_five_tries(monkeypatch):
    client = FakeTelegramClient()
    client.fail[("connect", None)] = ConnectionError("no network")
    deps, ticker = _deps(stop_after=999)
    with pytest.raises(SystemExit):
        await userbot.connect(client, deps)
    assert len(client.named("connect")) == 5
    assert ticker.pauses == [5, 10, 20, 40, 80]


# --- review fixes -----------------------------------------------------------------------

JOIN_DAY = datetime(2026, 10, 24, 10, 0, tzinfo=TASHKENT)
QUEUED = [
    channel(6300 + n, title=f"Namuna Q{n}", username=f"namuna_navbat{n}")
    for n in range(3)
]


class FirstMinute(random.Random):
    """The day's join minute is 10:00."""

    def randrange(self, *args, **kwargs):  # type: ignore[no-untyped-def,override]
        return 0


@pytest.fixture
async def queued(session, tmp_path, monkeypatch):
    """Three queued channels, no seeds (the source row is put back)."""
    monkeypatch.setattr(settings, "evidence_dir", str(tmp_path))
    source = await session.get(m.Source, chats.SOURCE_KEY)
    assert source is not None
    before = dict(source.config or {})
    source.config = {**before, "seeds": []}
    await session.commit()
    for entity in QUEUED:
        await chats.enqueue_chat(
            session, tg.dialog_of(entity), priority=chats.PRIORITY_SEED, now=JOIN_DAY
        )
        await session.commit()
    yield session
    await session.rollback()
    source = await session.get(m.Source, chats.SOURCE_KEY, populate_existing=True)
    assert source is not None
    source.config = before
    await session.commit()


async def test_a_sigterm_in_the_join_gap_joins_nothing_more(queued):
    session = queued
    client = FakeTelegramClient(entities={e.username: e for e in QUEUED})
    deps, ticker = _deps(stop_after=999, start=JOIN_DAY)
    deps.rng = FirstMinute()
    real_pause = ticker.pause

    async def pause(seconds: float) -> None:
        if seconds == 90:  # the joiner's gap: SIGTERM arrives in it
            deps.stop.set()
        await real_pause(seconds)

    deps.pause = pause

    await asyncio.wait_for(userbot.supervise(client, deps), timeout=20)

    assert [c.target for c in client.named("JoinChannelRequest")] == ["namuna_navbat0"]
    beat = await _beat(session, userbot.JOIN_COMPONENT)
    assert (beat.detail["joined"], beat.detail["skipped"]) == (1, "stopped")


async def test_the_beat_keeps_coming_while_a_long_job_runs(queued, monkeypatch):
    session = queued
    release = asyncio.Event()
    blocked: list[int] = []

    class Slow(FakeTelegramClient):
        async def __call__(self, request):  # type: ignore[no-untyped-def]
            if type(request).__name__ == "JoinChannelRequest" and not blocked:
                blocked.append(len(beats))
                await release.wait()  # a join that takes minutes
            return await super().__call__(request)

    client = Slow(entities={e.username: e for e in QUEUED})
    deps, ticker = _deps(stop_after=999, start=JOIN_DAY)
    deps.rng = FirstMinute()
    beats: list[datetime] = []
    real_beat = userbot.beat_userbot

    async def recording(d: userbot.Deps, detail: dict[str, object]) -> bool:
        beats.append(ticker.now())
        return await real_beat(d, detail)

    monkeypatch.setattr(userbot, "beat_userbot", recording)

    def watch(count: int) -> None:
        if blocked and len(beats) - blocked[0] >= 3:
            release.set()
            deps.stop.set()

    ticker.on_pause = watch

    await asyncio.wait_for(userbot.supervise(client, deps), timeout=20)

    # Three minute beats while the join was still waiting on Telegram.
    assert blocked and len(beats) - blocked[0] >= 3
    gaps = {(b - a).total_seconds() for a, b in itertools.pairwise(beats)}
    assert gaps == {60.0}
    beat = await _beat(session)
    assert beat.detail["authorized"] is True


async def test_no_dialog_sync_while_the_flood_breaker_holds(session):
    from clientscrapper.userbot import limits

    await limits.trip_breaker(session, seconds=900, now=NOW, where="test")
    await session.commit()
    group = channel(6400, title="Namuna D", username="namuna_dialog6", megagroup=True)
    client = FakeTelegramClient(dialogs=[group])
    deps, _ = _deps(stop_after=1)
    assert await userbot.sync_from_client(client, deps, now=NOW + timedelta(hours=1)) == 0
    assert client.named("iter_dialogs") == []
    assert await chats.get_monitor(session, tg.peer_id(group)) is None


async def test_the_dialog_sync_marks_chats_left_only_from_a_whole_list(session):
    rows = [
        channel(
            6500 + n, title=f"Namuna L{n}", username=f"namuna_tark{n}", megagroup=True
        )
        for n in range(3)
    ]
    deps, _ = _deps(stop_after=1)
    assert (
        await userbot.sync_from_client(FakeTelegramClient(dialogs=rows), deps, now=NOW)
        == 3
    )
    # A list cut short by a flood wait says nothing about the rest.
    cut = FakeTelegramClient(
        dialogs=[rows[0], errors.FloodWaitError(request=None, capture=30)]
    )
    later = NOW + timedelta(days=1)
    assert await userbot.sync_from_client(cut, deps, now=later) == 0
    assert await chats.joined_count(session) == 3
    # The whole list, without two of them: the owner left those by hand.
    whole = FakeTelegramClient(dialogs=[rows[0]])
    await userbot.sync_from_client(whole, deps, now=later)
    statuses = {
        r.username: r.status for r in (await chats.list_monitors(session, limit=10))[0]
    }
    assert statuses == {
        "namuna_tark0": "joined",
        "namuna_tark1": "left",
        "namuna_tark2": "left",
    }
    assert await chats.joined_count(session) == 1
