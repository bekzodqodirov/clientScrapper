"""The daily join (03 §2.3, 06 §2; 07 T-13), on Postgres with a fake clock.

≤ 3 joins a day whatever the queue holds, counted before each request so a
second run the same day joins nothing and the next day three more; ≥ 90 s
between joins; the linked discussion group queued at priority 1 and joined
next; the about kept as evidence, never as an interaction; the 60-chat cap;
a flood wait past 300 s trips the 24 h breaker and nothing is joined while
it holds; a username that is not a chat is never tried again. The account's
own resolves are capped at 20 a day.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
import sqlalchemy as sa
from telethon import errors

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.db.session import session_scope
from clientscrapper.services import chats
from clientscrapper.userbot import joiner, limits, tg
from tests.fake_telegram import FakeTelegramClient, channel, full_channel, user
from tests.fakes import T0, FakeClock

NAMES = [f"namuna_kanal{n}" for n in range(1, 6)]
CHANNELS = {
    name: channel(3000 + n, title=f"Namuna Kanal {n}", username=name)
    for n, name in enumerate(NAMES, 1)
}
LINKED = channel(3900, title="Namuna Kanal 1 muhokama", megagroup=True)


def _running() -> bool:
    """The process is not stopping (join_daily asks; see the stop test)."""
    return False


@pytest.fixture
async def reader(session, tmp_path, monkeypatch):
    """A clean database, no seeds (the 03 list holds real handles; the source
    row is configuration, so it is put back), evidence files in tmp_path."""
    monkeypatch.setattr(settings, "evidence_dir", str(tmp_path))
    source = await session.get(m.Source, chats.SOURCE_KEY)
    assert source is not None
    before = dict(source.config or {})
    source.config = {**before, "seeds": []}
    await session.commit()
    yield session
    await session.rollback()
    source = await session.get(m.Source, chats.SOURCE_KEY, populate_existing=True)
    assert source is not None
    source.config = before
    await session.commit()


async def _queue(session, *names: str) -> list[int]:
    ids = []
    for name in names:
        row = await chats.enqueue_chat(
            session, tg.dialog_of(CHANNELS[name]), priority=chats.PRIORITY_SEED, now=T0
        )
        assert row is not None
        ids.append(row.id)
        # created_at orders the queue; one transaction would give all one stamp.
        await session.commit()
    return ids


def _client() -> FakeTelegramClient:
    entities: dict[object, object] = dict(CHANNELS)
    entities[tg.peer_id(LINKED)] = LINKED
    full = {
        NAMES[0]: full_channel(
            about=(
                "Xitoydan optom. Xaridorlar guruhi: @namuna_xaridorlar, "
                "admin @namuna_kanal1"
            ),
            linked=LINKED,
            participants=1500,
        )
    }
    return FakeTelegramClient(entities=entities, full=full)


async def _monitors(session) -> dict[int, m.ChatMonitor]:
    rows = await session.scalars(
        sa.select(m.ChatMonitor).execution_options(populate_existing=True)
    )
    return {row.tg_chat_id: row for row in rows}


async def test_three_joins_a_day_with_the_fake_clock(reader):
    session = reader
    await _queue(session, *NAMES)
    client = _client()
    clock = FakeClock(T0)

    first = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )

    joins = client.named("JoinChannelRequest")
    # The linked group of the first channel (priority 1) jumps the queue.
    assert [c.target for c in joins] == [NAMES[0], tg.peer_id(LINKED), NAMES[1]]
    assert clock.sleeps == [joiner.JOIN_GAP_SECONDS, joiner.JOIN_GAP_SECONDS] == [90, 90]
    assert len(first.joined) == 3 and first.queued_linked == 1
    assert await limits.used(session, "joins", now=clock.now()) == 3

    # The same day again: no join and no lookup of a queued chat — the cap is
    # read first. Only the companion group the first about named is resolved
    # (resolves have their own cap; the fake knows no such chat).
    calls = len(client.calls)
    again = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert again.joined == []
    assert [(c.name, c.target) for c in client.calls[calls:]] == [
        ("get_entity", "namuna_xaridorlar")
    ]

    # The next Tashkent day: three more, and the queue is empty.
    clock.advance(timedelta(days=1).total_seconds())
    later = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert [c.target for c in client.named("JoinChannelRequest")[3:]] == NAMES[2:5]
    assert len(later.joined) == 3
    rows = await _monitors(session)
    joined = sorted(
        r.username or str(r.tg_chat_id) for r in rows.values() if r.status == "joined"
    )
    assert joined == sorted([*NAMES, str(tg.peer_id(LINKED))])


async def test_a_join_reads_the_full_channel_and_keeps_its_about_as_evidence(reader):
    session = reader
    await _queue(session, NAMES[0])
    client = _client()
    clock = FakeClock(T0)
    await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )

    rows = await _monitors(session)
    parent = rows[tg.peer_id(CHANNELS[NAMES[0]])]
    assert (parent.status, parent.joined_at, parent.member_count) == ("joined", T0, 1500)
    assert (parent.monitor_enabled, parent.monitoring_since) == (True, T0)
    linked = rows[tg.peer_id(LINKED)]
    # Queued at priority 1 and joined in the same run (the quota allowed it).
    assert (linked.chat_type, linked.priority, linked.status) == ("group", 1, "joined")
    [proof] = list(await session.scalars(sa.select(m.Evidence)))
    assert proof.url == "https://t.me/s/namuna_kanal1"
    assert proof.source_key == "telegram_monitor"
    assert proof.content_type == "application/json"
    assert proof.excerpt.startswith("Xitoydan optom. Xaridorlar guruhi")
    assert proof.meta == {"kind": "telegram_about"}
    # The about is never an interaction (02 §2.12: it has no prefilter hit).
    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(m.Interaction)) == 0
    )
    # The companion group waits for resolution at priority 2; the channel's
    # own name is not a companion.
    flag = await session.get(m.RuntimeFlag, chats.PENDING_FLAG, populate_existing=True)
    assert flag is not None
    assert {k: (v["priority"], v["role"]) for k, v in flag.value["names"].items()} == {
        "namuna_xaridorlar": (2, "buyers")
    }
    # Only the two allowed raw requests went out, in that order, per join.
    raw = [c.name for c in client.calls if c.name.endswith("Request")]
    assert raw == ["JoinChannelRequest", "GetFullChannelRequest"] * 2


async def test_the_sixty_chat_cap_stops_joining(reader, monkeypatch):
    session = reader
    monkeypatch.setattr(settings, "userbot_max_chats", 2)
    await _queue(session, *NAMES)
    client = _client()
    clock = FakeClock(T0)
    report = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert report.skipped == "max_chats"
    assert len(client.named("JoinChannelRequest")) == 2


async def test_a_long_flood_wait_trips_the_breaker_and_nothing_joins_while_it_holds(
    reader,
):
    session = reader
    await _queue(session, *NAMES)
    client = _client()
    client.fail[("JoinChannelRequest", None)] = errors.FloodWaitError(
        request=None, capture=400
    )
    clock = FakeClock(T0)

    report = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )

    assert report.joined == []
    flag = await session.get(m.RuntimeFlag, limits.FLOOD_FLAG, populate_existing=True)
    assert flag is not None
    assert flag.value == {
        "until": (T0 + timedelta(hours=24)).isoformat(),
        "seconds": 400,
        "since": T0.isoformat(),
        "where": "join",
    }
    assert flag.updated_by == "userbot"
    assert await limits.flood_until(session, now=T0) == T0 + timedelta(hours=24)
    # The attempt was counted before the request went out.
    assert await limits.used(session, "joins", now=T0) == 1

    del client.fail[("JoinChannelRequest", None)]
    calls = len(client.calls)
    clock.advance(timedelta(hours=23).total_seconds())
    blocked = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert blocked.skipped == "flood"
    assert len(client.calls) == calls  # not even a resolve

    clock.advance(timedelta(hours=2).total_seconds())
    after = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert after.skipped is None and len(after.joined) == 3


async def test_a_short_flood_wait_ends_the_run_without_a_breaker(reader):
    session = reader
    await _queue(session, *NAMES)
    client = _client()
    client.fail[("JoinChannelRequest", None)] = errors.FloodWaitError(
        request=None, capture=200
    )
    clock = FakeClock(T0)
    report = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert report.joined == [] and report.skipped is None
    assert len(client.named("JoinChannelRequest")) == 1
    assert await session.get(m.RuntimeFlag, limits.FLOOD_FLAG) is None
    assert await limits.flood_until(session, now=T0) is None


async def test_the_owners_pause_stops_the_join(reader):
    session = reader
    await _queue(session, *NAMES)
    session.add(
        m.RuntimeFlag(key="paused", value={"since": T0.isoformat()}, updated_by="owner:1")
    )
    await session.commit()
    client = _client()
    clock = FakeClock(T0)
    report = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert report.skipped == "paused"
    assert client.calls == []


async def test_a_vanished_username_is_gone_and_costs_no_join(reader):
    session = reader
    ids = await _queue(session, *NAMES[:2])
    client = _client()
    client.fail[("get_entity", NAMES[0])] = errors.UsernameNotOccupiedError(request=None)
    clock = FakeClock(T0)
    report = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert report.gone == 1
    assert [c.target for c in client.named("JoinChannelRequest")] == [NAMES[1]]
    gone = await session.get(m.ChatMonitor, ids[0], populate_existing=True)
    assert gone is not None and gone.status == "gone"
    assert await limits.used(session, "joins", now=T0) == 1


async def test_a_refused_join_marks_the_chat_gone(reader):
    session = reader
    ids = await _queue(session, NAMES[0])
    client = _client()
    client.fail[("JoinChannelRequest", None)] = errors.InviteRequestSentError(
        request=None
    )
    clock = FakeClock(T0)
    report = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert report.gone == 1
    gone = await session.get(m.ChatMonitor, ids[0], populate_existing=True)
    assert gone is not None and gone.status == "gone"


# --- resolving usernames --------------------------------------------------------------


async def test_resolves_are_capped_at_twenty_a_day_and_non_chats_never_retried(reader):
    session = reader
    names = [f"namuna_nom{n:02d}" for n in range(22)]
    for name in names:
        assert await chats.enqueue_username(
            session, name, priority=chats.PRIORITY_RESOLVED, now=T0, by="test"
        )
    await session.commit()
    entities = {
        name: channel(4000 + i, title=f"Namuna {i}", username=name)
        for i, name in enumerate(names)
    }
    entities[names[1]] = user(5300000001, username=names[1])  # a person, not a chat
    del entities[names[2]]  # resolves to nothing
    client = FakeTelegramClient(entities=entities)

    queued = await joiner.resolve_daily(client, session_scope, now=T0)

    assert [c.target for c in client.named("get_entity")] == names[:20]
    assert queued == 18
    assert await limits.used(session, "resolves", now=T0) == 20
    flag = await session.get(m.RuntimeFlag, chats.PENDING_FLAG, populate_existing=True)
    assert flag is not None
    assert sorted(flag.value["names"]) == names[20:]
    gone = flag.value["unresolvable"]
    assert sorted(v["reason"] for v in gone.values()) == ["not_a_public_chat", "unknown"]
    assert gone[names[2]] == {"reason": "unknown", "at": T0.isoformat()}
    # The person's handle is remembered only as a hash (06 §5.1).
    assert names[1] not in json.dumps(flag.value)
    rows = await _monitors(session)
    assert len(rows) == 18
    assert {r.priority for r in rows.values()} == {3}
    assert {r.status for r in rows.values()} == {"queued"}

    # The same day: the cap holds. The next day: the last two, and the two
    # that are not chats are not asked about again.
    assert await joiner.resolve_daily(client, session_scope, now=T0) == 0
    tomorrow = T0 + timedelta(days=1)
    assert await joiner.resolve_daily(client, session_scope, now=tomorrow) == 2
    assert [c.target for c in client.named("get_entity")][20:] == names[20:]
    assert not await chats.enqueue_username(
        session, names[1], priority=chats.PRIORITY_RESOLVED, now=tomorrow, by="test"
    )


async def test_seeds_come_from_the_source_config_and_resolve_once(reader):
    session = reader
    source = await session.get(m.Source, chats.SOURCE_KEY, populate_existing=True)
    assert source is not None
    source.config = {
        **source.config,
        "seeds": [
            "@Namuna_Seed1",
            {"username": "namuna_seed2", "role": "competitor"},
            "gsrgroupchat",
        ],
    }
    await session.commit()
    entities = {
        "namuna_seed1": channel(4100, title="Namuna Seed 1", username="namuna_seed1"),
        "namuna_seed2": channel(4101, title="Namuna Seed 2", username="namuna_seed2"),
    }
    client = FakeTelegramClient(entities=entities)
    assert await joiner.resolve_daily(client, session_scope, now=T0) == 2
    # The calibration chat is never resolved, let alone joined.
    assert [c.target for c in client.named("get_entity")] == [
        "namuna_seed1",
        "namuna_seed2",
    ]
    rows = {r.username: r for r in (await _monitors(session)).values()}
    assert (rows["namuna_seed1"].priority, rows["namuna_seed1"].role) == (5, "market")
    assert (rows["namuna_seed2"].priority, rows["namuna_seed2"].role) == (5, "competitor")
    # Known now: tomorrow's resolve asks nothing.
    assert (
        await joiner.resolve_daily(client, session_scope, now=T0 + timedelta(days=1)) == 0
    )
    assert len(client.named("get_entity")) == 2


def test_the_join_minute_is_inside_ten_to_twenty():
    import random

    rng = random.Random(7)
    minutes = {joiner.join_minute(T0.date(), rng=rng) for _ in range(500)}
    assert min(minutes) >= joiner.JOIN_WINDOW[0]
    assert max(minutes) < joiner.JOIN_WINDOW[1]
    assert len(minutes) > 100  # spread over the window, not a fixed time


def test_the_join_is_due_once_a_day_after_its_minute():
    at = joiner.JOIN_WINDOW[0].replace(hour=14, minute=30)
    day = T0.replace(hour=14, minute=29)
    assert not joiner.join_due(day, at=at, last_day=None)
    assert joiner.join_due(day.replace(minute=30), at=at, last_day=None)
    assert not joiner.join_due(day.replace(minute=31), at=at, last_day=day.date())
    # After 20:00 the day's join is skipped, not run late.
    assert not joiner.join_due(day.replace(hour=20, minute=5), at=at, last_day=None)


# --- review fixes -----------------------------------------------------------------------


async def test_a_stop_during_the_gap_ends_the_run_without_another_join(reader):
    session = reader
    await _queue(session, *NAMES[1:4])
    client = _client()
    clock = FakeClock(T0)
    stopping = asyncio.Event()

    async def cut_short(seconds: float) -> None:
        # SIGTERM arrives in the 90 s gap; the reader's wait returns at once.
        clock.sleeps.append(seconds)
        stopping.set()

    report = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=cut_short, stopped=stopping.is_set
    )

    assert [c.target for c in client.named("JoinChannelRequest")] == [NAMES[1]]
    assert (len(report.joined), report.skipped) == (1, "stopped")
    assert clock.sleeps == [90]
    assert await limits.used(session, "joins", now=T0) == 1


LONELY = channel(3950, title="Namuna Kanal 2 muhokama", megagroup=True)  # no username


async def test_a_usernameless_linked_group_is_joined_after_a_restart(reader, monkeypatch):
    session = reader
    monkeypatch.setattr(settings, "userbot_joins_per_day", 1)
    await _queue(session, NAMES[1])
    full = {NAMES[1]: full_channel(about="Xitoydan optom", linked=LONELY)}
    first = FakeTelegramClient(entities=dict(CHANNELS), full=full)
    clock = FakeClock(T0)
    day1 = await joiner.join_daily(
        first, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert (len(day1.joined), day1.queued_linked) == (1, 1)
    linked = await chats.get_monitor(session, tg.peer_id(LONELY))
    assert linked is not None and (linked.status, linked.username) == ("queued", None)
    assert (
        await chats.linked_access_hash(session, tg.peer_id(LONELY)) == LONELY.access_hash
    )

    # The next day, after a restart: the new client has cached nothing (no
    # peer-id key), but Telegram finds the group by its id and access hash.
    restarted = FakeTelegramClient(entities={}, reachable=[LONELY])
    clock.advance(timedelta(days=1).total_seconds())
    day2 = await joiner.join_daily(
        restarted, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )

    assert day2.joined == [linked.id]
    [lookup] = restarted.named("get_entity")
    assert (lookup.target.channel_id, lookup.target.access_hash) == (
        LONELY.id,
        LONELY.access_hash,
    )
    assert [c.target for c in restarted.named("JoinChannelRequest")] == [
        tg.peer_id(LONELY)
    ]
    row = await chats.get_monitor(session, tg.peer_id(LONELY))
    assert row is not None and row.status == "joined"
    assert await chats.linked_access_hash(session, tg.peer_id(LONELY)) is None


async def test_a_queued_chat_nobody_can_find_is_gone_not_retried_daily(reader):
    session = reader
    # A usernameless row with nothing remembered to find it by (queued before
    # a restart, say): the bare id finds nothing in a fresh session.
    row = await chats.enqueue_chat(
        session, tg.dialog_of(LONELY), priority=chats.PRIORITY_LINKED, now=T0
    )
    assert row is not None
    await session.commit()
    client = FakeTelegramClient()
    clock = FakeClock(T0)
    report = await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert (report.gone, report.joined) == (1, [])
    gone = await session.get(m.ChatMonitor, row.id, populate_existing=True)
    assert gone is not None and gone.status == "gone"
    clock.advance(timedelta(days=1).total_seconds())
    await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    assert [c.target for c in client.named("get_entity")] == [tg.peer_id(LONELY)]


async def test_an_admins_handle_in_an_about_is_not_queued(reader):
    session = reader
    await _queue(session, NAMES[1])
    full = {
        NAMES[1]: full_channel(
            about=(
                "Optom kiyim. Murojaat uchun: @namuna_admin9\n"
                "Xaridorlar guruhi: @namuna_guruh2, admin @namuna_admin8"
            )
        )
    }
    client = FakeTelegramClient(entities=dict(CHANNELS), full=full)
    clock = FakeClock(T0)
    await joiner.join_daily(
        client, session_scope, clock=clock.now, sleep=clock.sleep, stopped=_running
    )
    flag = await session.get(m.RuntimeFlag, chats.PENDING_FLAG, populate_existing=True)
    assert flag is not None
    assert list(flag.value["names"]) == ["namuna_guruh2"]


async def test_nothing_is_resolved_while_the_owner_has_paused(reader):
    session = reader
    assert await chats.enqueue_username(
        session, "namuna_nom99", priority=chats.PRIORITY_RESOLVED, now=T0, by="test"
    )
    session.add(
        m.RuntimeFlag(key="paused", value={"since": T0.isoformat()}, updated_by="owner:1")
    )
    await session.commit()
    client = FakeTelegramClient(
        entities={"namuna_nom99": channel(4999, title="Namuna", username="namuna_nom99")}
    )
    assert await joiner.resolve_daily(client, session_scope, now=T0) == 0
    assert client.calls == []
    assert await limits.used(session, "resolves", now=T0) == 0


async def test_a_resolve_that_is_a_person_keeps_no_handle_and_is_forgotten_in_a_month(
    reader,
):
    session = reader
    assert await chats.enqueue_username(
        session, "namuna_shaxs1", priority=chats.PRIORITY_COMPANION, now=T0, by="test"
    )
    await session.commit()
    client = FakeTelegramClient(
        entities={"namuna_shaxs1": user(5300000009, username="namuna_shaxs1")}
    )
    assert await joiner.resolve_daily(client, session_scope, now=T0) == 0
    flag = await session.get(m.RuntimeFlag, chats.PENDING_FLAG, populate_existing=True)
    assert flag is not None
    assert "namuna_shaxs1" not in json.dumps(flag.value)
    [(key, item)] = flag.value["unresolvable"].items()
    assert key.startswith("#") and item == {
        "reason": "not_a_public_chat",
        "at": T0.isoformat(),
    }
    # Not asked again within the month; a month on, the memory has expired.
    later = T0 + timedelta(days=29)
    assert not await chats.enqueue_username(
        session, "namuna_shaxs1", priority=chats.PRIORITY_COMPANION, now=later, by="t"
    )
    month = T0 + timedelta(days=31)
    assert await chats.enqueue_username(
        session, "namuna_shaxs1", priority=chats.PRIORITY_COMPANION, now=month, by="t"
    )
    await session.commit()
    flag = await session.get(m.RuntimeFlag, chats.PENDING_FLAG, populate_existing=True)
    assert flag is not None and flag.value["unresolvable"] == {}
