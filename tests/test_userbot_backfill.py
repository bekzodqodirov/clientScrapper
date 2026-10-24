"""The nightly backfill and the about reads (03 §2.3, 06 §2; 07 T-13).

Backfill: only chats joined ≥ 24 h ago and not yet backfilled; the window is
the newest 500 messages and nothing older than 30 days; only hits are
stored (and notified, as live ones are); ``backfilled_at`` is stamped once;
a flood wait ends the night. About: a channel's about becomes weekly
evidence and never an interaction; a poster's bio fills ``sender_bio``,
cached for 30 days, within the day's caps.
"""

from __future__ import annotations

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
    assert (group.backfilled_at, group.seen_count, group.hit_count) == (T0, 4, 2)
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
