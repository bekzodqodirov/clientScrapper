"""The nightly backfill and the about reads (03 §2.3, 06 §2; 07 T-13).

Backfill: only chats joined ≥ 24 h ago and not yet backfilled; the window is
the newest 500 messages and nothing older than 30 days; only hits are
stored (and notified, as live ones are); ``backfilled_at`` is stamped once;
a flood wait ends the night. About: a channel's about becomes weekly
evidence and never an interaction; a poster's bio fills ``sender_bio``,
cached for 30 days, within the day's caps.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

import pytest
import sqlalchemy as sa
from telethon import errors

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.db.session import session_scope
from clientscrapper.services import chats
from clientscrapper.userbot import about, backfill, ingest, limits, tg
from tests.fake_telegram import FakeTelegramClient, channel, full_channel, message, user
from tests.fakes import T0

GROUP = channel(5100, title="Namuna Bozor", username="namuna_bozor", megagroup=True)
FRESH = channel(5200, title="Namuna Yangi", username="namuna_yangi2", megagroup=True)
POSTER = user(5400000003, first_name="Namuna", username="namuna_3")
HIT = "Yiwudan konteyner olib keladigan bormi? 20 kub"
MISS = "Assalomu alaykum, bugun havo yaxshi ekan, hammaga omad"


async def _joined(session, entity, *, joined_at):
    row = await chats.ensure_monitor(session, tg.dialog_of(entity), now=joined_at)
    await session.commit()
    return row


def _history() -> list:
    """Newest first, as iter_messages yields: two hits, two misses, then a
    hit older than 30 days that must never be read."""
    return [
        message(GROUP, 105, HIT, date=T0 - timedelta(hours=2), sender=POSTER),
        message(GROUP, 104, MISS, date=T0 - timedelta(days=1), sender=POSTER),
        message(
            GROUP,
            103,
            "Срочно нужна фура до Ташкента",
            date=T0 - timedelta(days=3),
            sender=POSTER,
        ),
        message(GROUP, 102, MISS, date=T0 - timedelta(days=29), sender=POSTER),
        message(GROUP, 101, HIT, date=T0 - timedelta(days=31), sender=POSTER),
        message(GROUP, 100, HIT, date=T0 - timedelta(days=40), sender=POSTER),
    ]


async def test_the_backfill_reads_the_window_stores_hits_and_stamps_the_chat(session):
    await _joined(session, GROUP, joined_at=T0 - timedelta(hours=25))
    await _joined(session, FRESH, joined_at=T0 - timedelta(hours=23))  # not yet due
    client = FakeTelegramClient(history={tg.peer_id(GROUP): _history()})

    night = await backfill.backfill_nightly(client, session_scope, now=T0)

    reads = client.named("iter_messages")
    assert [(c.target, c.kwargs["limit"]) for c in reads] == [(tg.peer_id(GROUP), 500)]
    assert (night.chats, night.seen, night.stored, night.failed) == (1, 4, 2, 0)
    rows = list(
        await session.scalars(
            sa.select(m.Interaction).order_by(m.Interaction.tg_message_id)
        )
    )
    assert [r.tg_message_id for r in rows] == [103, 105]
    group = await chats.get_monitor(session, tg.peer_id(GROUP))
    fresh = await chats.get_monitor(session, tg.peer_id(FRESH))
    assert group is not None and fresh is not None
    # seen_count takes only what is older than monitoring_since (joined_at,
    # T0 - 25 h): 103 and 102; 105 and 104 were the live handler's to count.
    assert (group.backfilled_at, group.seen_count, group.hit_count) == (T0, 2, 2)
    assert group.last_seen_message_id == 105
    assert fresh.backfilled_at is None

    # Once only: the next night reads nothing for this chat.
    again = await backfill.backfill_nightly(
        client, session_scope, now=T0 + timedelta(days=1)
    )
    assert [c.target for c in client.named("iter_messages")] == [
        tg.peer_id(GROUP),
        tg.peer_id(FRESH),
    ]
    assert again.chats == 1


async def test_a_backfilled_hit_is_notified_like_a_live_one(session):
    from tests.test_userbot_ingest import Listener

    await _joined(session, GROUP, joined_at=T0 - timedelta(days=2))
    client = FakeTelegramClient(history={tg.peer_id(GROUP): _history()[:3]})
    async with Listener() as listener:
        await backfill.backfill_nightly(client, session_scope, now=T0)
        heard = await listener.heard(expect=2)
    ids = list(
        await session.scalars(sa.select(m.Interaction.id).order_by(m.Interaction.id))
    )
    assert sorted(heard) == sorted(str(i) for i in ids)


async def test_a_live_message_and_the_backfill_meet_on_one_row(session):
    await _joined(session, GROUP, joined_at=T0 - timedelta(days=2))
    live = message(GROUP, 105, HIT, date=T0 - timedelta(hours=2), sender=POSTER)
    assert await ingest.handle_message(session, live, now=T0) == "stored"
    await session.commit()
    client = FakeTelegramClient(history={tg.peer_id(GROUP): _history()})
    night = await backfill.backfill_nightly(client, session_scope, now=T0)
    assert night.stored == 1  # 103 only; 105 was there
    count = await session.scalar(sa.select(sa.func.count()).select_from(m.Interaction))
    assert count == 2


async def test_a_flood_wait_ends_the_night_trips_the_breaker_and_counts_an_attempt(
    session,
):
    await _joined(session, GROUP, joined_at=T0 - timedelta(days=2))
    history = [_history()[0], errors.FloodWaitError(request=None, capture=900)]
    client = FakeTelegramClient(history={tg.peer_id(GROUP): history})
    night = await backfill.backfill_nightly(client, session_scope, now=T0)
    assert night.failed == 1
    assert await limits.flood_until(session, now=T0) == T0 + timedelta(hours=24)
    row = await chats.get_monitor(session, tg.peer_id(GROUP))
    assert row is not None and (row.backfilled_at, row.backfill_attempts) == (None, 1)
    # The partial read rolled back with its chat: no half-backfilled chat.
    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(m.Interaction)) == 0
    )
    # While the breaker holds there is no backfill at all.
    blocked = await backfill.backfill_nightly(
        client, session_scope, now=T0 + timedelta(hours=1)
    )
    assert blocked.skipped == "flood"
    assert len(client.named("iter_messages")) == 1


async def test_a_chat_that_keeps_failing_is_left_alone_after_three_nights(session):
    await _joined(session, GROUP, joined_at=T0 - timedelta(days=2))
    client = FakeTelegramClient(history={tg.peer_id(GROUP): [RuntimeError("broken")]})
    for night in range(4):
        await backfill.backfill_nightly(
            client, session_scope, now=T0 + timedelta(days=night)
        )
    assert len(client.named("iter_messages")) == chats.BACKFILL_MAX_ATTEMPTS == 3


# --- about ----------------------------------------------------------------------------


@pytest.fixture
def evidence_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "evidence_dir", str(tmp_path))
    return tmp_path


async def test_a_queued_channels_about_is_weekly_evidence_never_an_interaction(
    session, evidence_dir
):
    found = channel(5300, title="Namuna Diler", username="namuna_diler")
    await chats.enqueue_chat(
        session, tg.dialog_of(found), priority=chats.PRIORITY_RESOLVED, now=T0
    )
    await session.commit()
    pinned = message(found, 9, "Manzil: Toshkent, Chorsu. Tel +998 90 000 00 11", date=T0)
    client = FakeTelegramClient(
        full={
            "namuna_diler": full_channel(
                about="Optom mebel furnitura, @namuna_xaridor_guruh",
                participants=870,
                pinned_msg_id=9,
            )
        },
        pinned={"namuna_diler": pinned},
    )
    report = await about.drain_about(client, session_scope, now=T0)
    assert report.channels == 1
    assert [c.name for c in client.calls] == ["GetFullChannelRequest", "get_messages"]
    [proof] = list(await session.scalars(sa.select(m.Evidence)))
    assert (proof.url, proof.source_key, proof.source_ref) == (
        "https://t.me/s/namuna_diler",
        "telegram_monitor",
        f"tg:{tg.peer_id(found)}",
    )
    assert proof.excerpt == "Optom mebel furnitura, @namuna_xaridor_guruh"
    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(m.Interaction)) == 0
    )
    row = await chats.get_monitor(session, tg.peer_id(found))
    assert row is not None and (row.member_count, row.status) == (870, "queued")
    assert await limits.used(session, "about_channels", now=T0) == 1

    # Within the week it is not read again.
    await about.drain_about(client, session_scope, now=T0 + timedelta(days=6))
    assert len(client.named("GetFullChannelRequest")) == 1
    await about.drain_about(client, session_scope, now=T0 + timedelta(days=8))
    assert len(client.named("GetFullChannelRequest")) == 2


async def test_bios_fill_sender_bio_within_the_cap_and_are_cached(session, evidence_dir):
    await _joined(session, GROUP, joined_at=T0 - timedelta(days=2))
    for n in (1, 2):
        live = message(GROUP, n, HIT, date=T0 - timedelta(hours=n), sender=POSTER)
        assert await ingest.handle_message(session, live, now=T0) == "stored"
    await session.commit()
    bio = "Xitoydan yuk tashish xizmati " + "a" * 400
    client = FakeTelegramClient(bios={"namuna_3": bio})

    report = await about.drain_about(client, session_scope, now=T0)

    assert (report.bios, report.bios_cached) == (1, 0)
    assert [c.target for c in client.named("GetFullUserRequest")] == ["namuna_3"]
    bios = list(await session.scalars(sa.select(m.Interaction.sender_bio)))
    assert bios == [bio[:300]] * 2
    # A later post by the same sender takes the cached bio, no request.
    later = message(GROUP, 3, HIT, date=T0 + timedelta(hours=1), sender=POSTER)
    assert await ingest.handle_message(session, later, now=T0) == "stored"
    await session.commit()
    again = await about.drain_about(client, session_scope, now=T0 + timedelta(hours=2))
    assert (again.bios, again.bios_cached) == (0, 1)
    assert len(client.named("GetFullUserRequest")) == 1


async def test_the_about_caps_hold_for_the_day(session, evidence_dir, monkeypatch):
    monkeypatch.setattr(limits, "ABOUT_CHANNELS_PER_DAY", 2)
    for n in range(4):
        found = channel(5500 + n, title=f"Namuna {n}", username=f"namuna_diler{n}")
        await chats.enqueue_chat(
            session, tg.dialog_of(found), priority=chats.PRIORITY_RESOLVED, now=T0
        )
        await session.commit()
    client = FakeTelegramClient()
    first = await about.drain_about(client, session_scope, now=T0)
    second = await about.drain_about(client, session_scope, now=T0 + timedelta(hours=1))
    assert (first.channels, second.channels) == (2, 0)
    assert len(client.named("GetFullChannelRequest")) == 2


async def test_nothing_is_read_while_the_breaker_holds(session, evidence_dir):
    await limits.trip_breaker(session, seconds=301, now=T0, where="test")
    await session.commit()
    found = channel(5600, title="Namuna", username="namuna_diler9")
    await chats.enqueue_chat(
        session, tg.dialog_of(found), priority=chats.PRIORITY_RESOLVED, now=T0
    )
    await session.commit()
    client = FakeTelegramClient()
    report = await about.drain_about(client, session_scope, now=T0 + timedelta(hours=1))
    assert report.skipped == "flood"
    assert client.calls == []


# --- review fixes -----------------------------------------------------------------------


async def test_a_message_seen_live_is_not_counted_again_by_the_backfill(session):
    # Joined two days ago; three misses arrived live since. The backfill reads
    # them again and one older message from before the join.
    await _joined(session, GROUP, joined_at=T0 - timedelta(days=2))
    live = [
        message(GROUP, n, MISS, date=T0 - timedelta(hours=n), sender=POSTER)
        for n in (3, 2, 1)
    ]
    for msg in live:
        assert await ingest.handle_message(session, msg, now=T0) == "seen"
    await session.commit()
    before = message(GROUP, 0, MISS, date=T0 - timedelta(days=3), sender=POSTER)
    client = FakeTelegramClient(history={tg.peer_id(GROUP): [*reversed(live), before]})

    night = await backfill.backfill_nightly(client, session_scope, now=T0)

    assert night.seen == 4  # what the night read
    row = await chats.get_monitor(session, tg.peer_id(GROUP))
    assert row is not None
    assert (row.seen_count, row.hit_count, row.last_seen_message_id) == (4, 0, 3)


async def test_a_backfill_and_an_edit_in_the_same_chat_do_not_deadlock(session):
    # The backfill's transaction is open for the whole read. An edit that
    # turns an older message into a hit inserts it and counts the hit while
    # the backfill is still reading; then the backfill reaches that message.
    await _joined(session, GROUP, joined_at=T0 - timedelta(days=2))
    newest = message(GROUP, 200, MISS, date=T0 - timedelta(minutes=5), sender=POSTER)
    edited = message(GROUP, 150, HIT, date=T0 - timedelta(days=3), sender=POSTER)
    edit_done = asyncio.Event()
    outcomes: list[object] = []

    async def edit() -> None:
        try:
            async with session_scope() as other:
                outcomes.append(
                    await ingest.handle_edit(
                        other, edited, now=T0, edits=ingest.EditCounter()
                    )
                )
        except Exception as exc:  # what a deadlock victim raises
            outcomes.append(exc)
        finally:
            edit_done.set()

    class Reading(FakeTelegramClient):
        def iter_messages(self, chat, limit=None, **kwargs):
            self._record("iter_messages", chat, limit=limit, **kwargs)
            return self._read()

        async def _read(self):
            yield newest
            task = asyncio.ensure_future(edit())
            # The edit runs while the backfill's transaction is open; give it
            # a second (Postgres's deadlock_timeout) before reading on.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(edit_done.wait(), timeout=3)
            yield edited
            await task

    night = await backfill.backfill_nightly(Reading(), session_scope, now=T0)

    assert outcomes == ["stored"]
    assert (night.chats, night.failed, night.stored) == (1, 0, 0)
    row = await chats.get_monitor(session, tg.peer_id(GROUP))
    assert row is not None
    assert row.backfilled_at == T0
    # The edit counted its hit; the backfill met it stored, and counted only
    # 150 as seen — 200 is newer than monitoring_since, the live handler's.
    assert (row.seen_count, row.hit_count) == (1, 1)


async def test_a_restriction_in_the_backfill_goes_up_and_costs_no_attempt(session):
    await _joined(session, GROUP, joined_at=T0 - timedelta(days=2))
    client = FakeTelegramClient(
        history={tg.peer_id(GROUP): [errors.UserDeactivatedBanError(request=None)]}
    )
    with pytest.raises(errors.UserDeactivatedBanError):
        await backfill.backfill_nightly(client, session_scope, now=T0)
    row = await chats.get_monitor(session, tg.peer_id(GROUP))
    assert row is not None and (row.backfill_attempts, row.backfilled_at) == (0, None)


async def test_a_stop_ends_the_night_between_chats(session):
    await _joined(session, GROUP, joined_at=T0 - timedelta(days=3))
    await _joined(session, FRESH, joined_at=T0 - timedelta(days=2))
    client = FakeTelegramClient(history={tg.peer_id(GROUP): _history()[:2]})
    asked: list[int] = []

    def stopped() -> bool:
        asked.append(len(client.named("iter_messages")))
        return len(client.named("iter_messages")) >= 1

    night = await backfill.backfill_nightly(
        client, session_scope, now=T0, stopped=stopped
    )
    assert (night.chats, night.skipped) == (1, "stopped")
    assert [c.target for c in client.named("iter_messages")] == [tg.peer_id(GROUP)]


async def test_the_fifty_first_bio_of_the_day_is_not_asked(session, evidence_dir):
    await _joined(session, GROUP, joined_at=T0 - timedelta(days=2))
    for n in range(52):
        poster = user(5410000000 + n, username=f"namuna_b{n:02d}")
        msg = message(GROUP, 1000 + n, HIT, date=T0 - timedelta(minutes=n), sender=poster)
        assert await ingest.handle_message(session, msg, now=T0) == "stored"
    await session.commit()
    client = FakeTelegramClient()
    report = await about.drain_about(client, session_scope, now=T0, batch=60)
    assert report.bios == 50
    assert len(client.named("GetFullUserRequest")) == 50
    assert await limits.used(session, "about_users", now=T0) == 50
    again = await about.drain_about(client, session_scope, now=T0, batch=60)
    assert again.bios == 0 and len(client.named("GetFullUserRequest")) == 50


async def test_the_calibration_chats_about_is_never_read(session, evidence_dir):
    # Even a row that says queued at priority 3 (written by hand): GSR's own
    # chat is never read (03 §2.3).
    gsr = channel(5700, title="GSR guruhi", username="gsrgroupchat", megagroup=True)
    row = await chats.ensure_monitor(
        session,
        tg.dialog_of(gsr),
        now=T0,
        status="queued",
        priority=chats.PRIORITY_RESOLVED,
    )
    assert row.role == "calibration"
    await session.commit()
    assert await about.channels_due(session, now=T0, limit=10) == []
    client = FakeTelegramClient()
    report = await about.drain_about(client, session_scope, now=T0)
    assert report.channels == 0 and client.calls == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Optom kiyim. Murojaat uchun: @namuna_admin9", []),
        ("Xaridorlar guruhi: @namuna_guruh2, admin @namuna_admin8", ["namuna_guruh2"]),
        ("Buyers chat — t.me/namuna_buy3\nAdmin: @namuna_admin7", ["namuna_buy3"]),
        ("Покупатели: @namuna_pok4 · менеджер @namuna_men5", ["namuna_pok4"]),
        ("Савол бўлса: @namuna_xaridorlar", ["namuna_xaridorlar"]),
        ("Гуруҳимиз: @namuna_one6\n@namuna_admin6", ["namuna_one6"]),
        ("Kanal: @namuna_kanal7", []),
    ],
)
def test_companion_groups_are_the_handles_named_as_groups(text, expected):
    assert about.companion_handles(text) == expected
