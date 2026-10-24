"""The chat monitor registry (02 §2.12, 06 §2; 07 T-13).

The defaults (groups and channels read, private chats and GSR's own chat
never), the seeds, a dialog re-sync that never overrides the owner, the
owner's /chats switch, the joiner's queue and the waiting usernames.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from clientscrapper.db import models as m
from clientscrapper.services import chats

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent"))


@pytest.mark.parametrize(
    ("chat_type", "role", "expected"),
    [
        ("group", "market", (True, "rule:default")),
        ("channel", "competitor", (True, "rule:default")),
        ("private", "market", (False, "rule:private_off")),
        ("group", "calibration", (False, "rule:seed")),
    ],
)
def test_the_default_is_one_rule(chat_type, role, expected):
    assert chats.default_monitor_enabled(chat_type, role=role) == expected


def test_the_calibration_chat_is_calibration_whatever_it_is_called_by():
    assert chats.role_for("gsrgroupchat", "market") == "calibration"
    assert chats.role_for("namuna_optom", "competitor") == "competitor"


def test_channel_peer_ids_are_the_marked_ones():
    assert chats.channel_peer_id(1234) == -1000000001234
    assert chats.channel_peer_id(-1000000001234) == -1000000001234


def test_seeds_are_03s_unless_the_source_config_says_otherwise():
    assert chats.seeds_from({}) == chats.SEEDS
    assert {s.username for s in chats.SEEDS} >= {"optom_tavarlar_uz", "mk_express"}
    assert "gsrgroupchat" not in {s.username for s in chats.SEEDS}
    assert all(s.username == s.username.lower() for s in chats.SEEDS)
    got = chats.seeds_from(
        {
            "seeds": [
                "@Namuna_Seed",
                {"username": "t.me/namuna_two", "role": "buyers"},
                "x",
                5,
            ]
        }
    )
    assert got == (chats.Seed("namuna_seed"), chats.Seed("namuna_two", "buyers"))
    assert chats.seeds_from({"seeds": []}) == ()


async def test_a_dialog_resync_keeps_the_owners_switch_and_renames(session):
    group = chats.DialogInfo(
        -1000000007001, "group", "Namuna Bozor", "Namuna_Bozor7", 100
    )
    created, updated = await chats.sync_dialogs(session, [group], now=NOW)
    assert (created, updated) == (1, 0)
    row = await chats.get_monitor(session, group.tg_chat_id)
    assert row is not None
    assert (row.username, row.status, row.joined_at, row.monitor_enabled) == (
        "namuna_bozor7",
        "joined",
        NOW,
        True,
    )
    await chats.set_monitor(session, row.id, False, now=NOW)
    renamed = chats.DialogInfo(
        -1000000007001, "group", "Namuna Bozor 2", "namuna_bozor7", 120
    )
    created, updated = await chats.sync_dialogs(
        session, [renamed], now=NOW + timedelta(days=1)
    )
    assert (created, updated) == (0, 1)
    row = await chats.get_monitor(session, group.tg_chat_id)
    assert row is not None
    assert (row.title, row.member_count, row.monitor_enabled, row.decided_by) == (
        "Namuna Bozor 2",
        120,
        False,
        "owner",
    )
    assert row.joined_at == NOW  # the first join, for the 24 h backfill wait


async def test_the_owner_can_not_switch_on_a_private_or_calibration_chat(session):
    dm = await chats.ensure_monitor(
        session,
        chats.DialogInfo(5500000003, "private", "Namuna Mijoz", "namuna_mijoz"),
        now=NOW,
    )
    gsr = await chats.ensure_monitor(
        session, chats.DialogInfo(-1000000007002, "group", "GSR", "gsrgroupchat"), now=NOW
    )
    assert (dm.title, dm.username) == (None, None)
    for row in (dm, gsr):
        with pytest.raises(ValueError):
            await chats.set_monitor(session, row.id, True, now=NOW)
        assert row.monitor_enabled is False
    assert await chats.set_monitor(session, 999999, True, now=NOW) is None


async def test_the_queue_is_best_priority_then_oldest(session):
    seed = chats.DialogInfo(-1000000007010, "channel", "Seed", "namuna_seed10")
    linked = chats.DialogInfo(-1000000007011, "group", "Linked", None)
    await chats.enqueue_chat(session, seed, priority=chats.PRIORITY_SEED, now=NOW)
    await session.commit()
    await chats.enqueue_chat(session, linked, priority=chats.PRIORITY_LINKED, now=NOW)
    await session.commit()
    assert [r.tg_chat_id for r in await chats.queued(session, limit=5)] == [
        linked.tg_chat_id,
        seed.tg_chat_id,
    ]
    await session.commit()
    # Queued again at a better priority, it moves up; a known chat is not new.
    assert (
        await chats.enqueue_chat(session, seed, priority=chats.PRIORITY_OWNER, now=NOW)
        is None
    )
    row = await chats.get_monitor(session, seed.tg_chat_id)
    assert row is not None and row.priority == 0


async def test_waiting_usernames_are_deduplicated_and_ordered(session):
    by = "test"
    assert await chats.enqueue_username(session, "@Namuna_A1", priority=3, now=NOW, by=by)
    assert await chats.enqueue_username(session, "namuna_b2", priority=2, now=NOW, by=by)
    assert not await chats.enqueue_username(
        session, "namuna_a1", priority=3, now=NOW, by=by
    )
    assert await chats.enqueue_username(session, "namuna_a1", priority=1, now=NOW, by=by)
    assert not await chats.enqueue_username(
        session, "gsrgroupchat", priority=0, now=NOW, by=by
    )
    assert not await chats.enqueue_username(session, "no", priority=0, now=NOW, by=by)
    await chats.ensure_monitor(
        session, chats.DialogInfo(-1000000007020, "channel", "C", "namuna_known"), now=NOW
    )
    assert not await chats.enqueue_username(
        session, "namuna_known", priority=0, now=NOW, by=by
    )
    seeds = (chats.Seed("namuna_seed1"), chats.Seed("namuna_known"))
    got = await chats.pending_usernames(session, seeds=seeds, limit=10)
    assert [(p.username, p.priority) for p in got] == [
        ("namuna_a1", 1),
        ("namuna_b2", 2),
        ("namuna_seed1", 5),
    ]
    await chats.forget_pending(session, "namuna_b2", now=NOW, unresolvable="unknown")
    got = await chats.pending_usernames(session, seeds=seeds, limit=10)
    assert [p.username for p in got] == ["namuna_a1", "namuna_seed1"]
    assert not await chats.enqueue_username(
        session, "namuna_b2", priority=0, now=NOW, by=by
    )


async def test_seen_counts_add_up_in_one_statement(session):
    row = await chats.ensure_monitor(
        session, chats.DialogInfo(-1000000007030, "group", "G", None), now=NOW
    )
    await chats.count_seen(session, row.tg_chat_id, message_id=10, now=NOW)
    await chats.count_seen(
        session, row.tg_chat_id, message_id=7, now=NOW, seen=3, stored=2
    )
    fresh = await session.scalar(
        sa.select(m.ChatMonitor)
        .where(m.ChatMonitor.id == row.id)
        .execution_options(populate_existing=True)
    )
    assert fresh is not None
    assert (fresh.seen_count, fresh.hit_count, fresh.last_seen_message_id) == (4, 2, 10)


async def test_the_calibration_chat_is_never_queued_for_a_join(session):
    gsr = chats.DialogInfo(-1000000007040, "group", "GSR", "GsrGroupChat")
    assert (
        await chats.enqueue_chat(session, gsr, priority=chats.PRIORITY_OWNER, now=NOW)
        is None
    )
    assert await chats.get_monitor(session, gsr.tg_chat_id) is None
    # Even a row that says queued (written by hand) is not offered to the joiner.
    row = await chats.ensure_monitor(session, gsr, now=NOW, status="queued")
    assert row.role == "calibration"
    assert await chats.queued(session, limit=5) == []
