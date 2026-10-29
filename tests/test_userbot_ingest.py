"""The reader's live path (03 §2.3, 02 §2.12; 07 T-13), on Postgres.

What a message becomes: a stored ``interactions`` row and a ``NOTIFY
cs_interaction`` when the prefilter keeps it, a ``seen_count`` and nothing
else when it does not, nothing at all from a chat that is never read. An edit
re-runs the prefilter at most ten times. The NOTIFY is heard the way the
worker hears it — ``LISTEN`` on a second connection — so a payload sent before
the commit, or not at all, fails here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.services import chats
from clientscrapper.userbot import ingest
from tests.fake_telegram import channel, message, user

TASHKENT = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=TASHKENT)
GROUP = channel(
    1100, title="Namuna Optom Guruhi", username="namuna_optom", megagroup=True
)
CHANNEL = channel(1200, title="Namuna Kanal", username="namuna_kanal")
POSTER = user(
    5200000007,
    first_name="Namuna",
    last_name="Sotuvchi",
    username="Namuna_7",
    phone="998900000007",
)
HIT = "Guangzhoudan 2 kub mato kerak, kim olib keladi?"
MISS = "Assalomu alaykum, bugun havo yaxshi ekan, hammaga omad"

# 02 §2.12: what an interactions row may hold. Anything else non-empty on a
# row the live handler wrote is data the reader was never allowed to keep.
ALLOWED_COLUMNS = {
    "id",
    "tg_chat_id",
    "tg_message_id",
    "sender_id",
    "sender_username",
    "sender_name",
    "text",
    "occurred_at",
    "media_kind",
    "reply_to_message_id",
    "fwd_from_peer_id",
    "prefilter_hit",
    "created_at",
}


async def _monitor(session, entity, **overrides) -> m.ChatMonitor:
    from clientscrapper.userbot import tg

    row = await chats.ensure_monitor(session, tg.dialog_of(entity), now=NOW)
    for key, value in overrides.items():
        setattr(row, key, value)
    await session.flush()
    return row


def _conninfo() -> str:
    return settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


class Listener:
    """``LISTEN cs_interaction`` on its own autocommit connection."""

    def __init__(self) -> None:
        self.conn: psycopg.AsyncConnection | None = None

    async def __aenter__(self) -> Listener:
        self.conn = await psycopg.AsyncConnection.connect(_conninfo(), autocommit=True)
        await self.conn.execute("LISTEN cs_interaction")
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.conn is not None
        await self.conn.close()

    async def heard(self, *, expect: int, timeout: float = 2.0) -> list[str]:
        assert self.conn is not None
        got: list[str] = []
        if expect == 0:
            async for note in self.conn.notifies(timeout=0.3):
                got.append(note.payload)
            return got
        async for note in self.conn.notifies(timeout=timeout, stop_after=expect):
            assert note.channel == "cs_interaction"
            got.append(note.payload)
        return got


async def _rows(session) -> list[m.Interaction]:
    return list(
        await session.scalars(
            sa.select(m.Interaction)
            .order_by(m.Interaction.id)
            .execution_options(populate_existing=True)
        )
    )


async def _counts(session, entity) -> tuple[int, int, int | None]:
    from clientscrapper.userbot import tg

    row = await chats.get_monitor(session, tg.peer_id(entity))
    assert row is not None
    return row.seen_count, row.hit_count, row.last_seen_message_id


# --- the live handler --------------------------------------------------------------


async def test_a_hit_is_stored_with_only_02s_columns_and_notified_after_commit(session):
    await _monitor(session, GROUP)
    await session.commit()
    msg = message(
        GROUP,
        41,
        HIT,
        date=NOW - timedelta(minutes=1),
        sender=POSTER,
        fwd_from_channel=1200,
        reply_to=40,
        photo=object(),
    )
    async with Listener() as listener:
        assert await ingest.handle_message(session, msg, now=NOW) == "stored"
        # Nothing is heard before the commit: the worker must never wake on a
        # row it cannot read yet.
        assert await listener.heard(expect=0) == []
        await session.commit()
        heard = await listener.heard(expect=1)
    [row] = await _rows(session)
    assert heard == [str(row.id)]
    assert (row.tg_chat_id, row.tg_message_id) == (-1000000001100, 41)
    assert row.sender_id == 5200000007
    assert row.sender_username == "namuna_7"
    assert row.sender_name == "Namuna Sotuvchi"
    assert row.text == HIT
    assert row.occurred_at == NOW - timedelta(minutes=1)
    assert row.media_kind == "photo"
    assert row.reply_to_message_id == 40
    assert row.fwd_from_peer_id == -1000000001200
    assert row.prefilter_hit == ["volume_kub", "china", "intent"]
    # Nothing beyond 02 §2.12: no phone (the sender's is on the entity), no
    # bio until about.py reads one, no processing or purge stamps.
    filled = {
        col.name
        for col in m.Interaction.__table__.columns
        if getattr(row, col.key) is not None
    }
    assert filled <= ALLOWED_COLUMNS
    assert "900000007" not in repr(
        [getattr(row, c.key) for c in m.Interaction.__table__.columns]
    )
    assert await _counts(session, GROUP) == (1, 1, 41)


async def test_a_non_match_only_increments_seen_count(session):
    await _monitor(session, GROUP)
    await session.commit()
    async with Listener() as listener:
        for n, text in enumerate([MISS, "Kerak!", "Salom hammaga"], start=50):
            msg = message(GROUP, n, text, date=NOW, sender=POSTER)
            assert await ingest.handle_message(session, msg, now=NOW) == "seen"
        await session.commit()
        assert await listener.heard(expect=0) == []
    assert await _rows(session) == []
    assert await _counts(session, GROUP) == (3, 0, 52)
    # No full chat logs: nothing of the three messages exists anywhere.
    for model in (m.Evidence, m.Observation, m.Contact):
        assert await session.scalar(sa.select(sa.func.count()).select_from(model)) == 0


async def test_every_stored_row_is_followed_by_its_own_notify_and_a_repeat_by_none(
    session,
):
    await _monitor(session, GROUP)
    await _monitor(session, CHANNEL)
    await session.commit()
    async with Listener() as listener:
        texts = [(GROUP, 1, HIT), (CHANNEL, 2, "Yangi tovarlar keldi! Optom narxlarda")]
        texts.append((GROUP, 3, "Срочно нужна фура до Ташкента"))
        for chat, n, text in texts:
            outcome = await ingest.handle_message(
                session, message(chat, n, text, date=NOW, sender=POSTER), now=NOW
            )
            assert outcome == "stored"
            await session.commit()
        heard = await listener.heard(expect=3)
        # The same message again (a catch-up, the backfill meeting the live
        # path) is no new row and wakes nobody.
        again = message(GROUP, 1, HIT, date=NOW, sender=POSTER)
        assert await ingest.handle_message(session, again, now=NOW) == "duplicate"
        await session.commit()
        assert await listener.heard(expect=0) == []
    rows = await _rows(session)
    assert heard == [str(r.id) for r in rows]
    assert [r.prefilter_hit for r in rows] == [
        ["volume_kub", "china", "intent"],
        ["wholesale"],
        ["container", "intent"],
    ]
    assert await _counts(session, GROUP) == (3, 2, 3)


@pytest.mark.parametrize(
    ("overrides", "entity"),
    [
        ({"monitor_enabled": False}, GROUP),  # the owner switched it off
        ({"role": "calibration"}, GROUP),  # GSR's own chat: nothing is read
        ({"status": "gone"}, GROUP),
        ({}, user(5200000009, username="namuna_9")),  # a private chat
    ],
    ids=["off", "calibration", "gone", "private"],
)
async def test_a_chat_that_is_not_read_is_not_even_counted(session, overrides, entity):
    await _monitor(session, entity, **overrides)
    await session.commit()
    msg = message(entity, 7, HIT, date=NOW, sender=POSTER)
    assert await ingest.handle_message(session, msg, now=NOW) == "ignored"
    await session.commit()
    assert await _rows(session) == []
    assert (await _counts(session, entity))[:2] == (0, 0)


async def test_the_calibration_chat_is_off_from_its_first_row(session):
    gsr = channel(1300, title="GSR guruhi", username="gsrgroupchat", megagroup=True)
    msg = message(gsr, 1, HIT, date=NOW, sender=POSTER)
    assert await ingest.handle_message(session, msg, now=NOW) == "ignored"
    await session.commit()
    row = await chats.get_monitor(session, -1000000001300)
    assert row is not None
    assert (row.role, row.monitor_enabled, row.decided_by) == (
        "calibration",
        False,
        "rule:seed",
    )
    assert await _rows(session) == []


async def test_service_messages_own_messages_and_unknown_chats_are_ignored(session):
    await _monitor(session, GROUP)
    await session.commit()
    joined = message(GROUP, 1, HIT, date=NOW, action=object())
    mine = message(GROUP, 2, HIT, date=NOW, out=True)
    stranger = channel(1400, title="Namuna 2", megagroup=True)
    no_entity = message(stranger, 3, HIT, date=NOW, with_chat=False)
    for msg in (joined, mine, no_entity):
        assert await ingest.handle_message(session, msg, now=NOW) == "ignored"
    await session.commit()
    assert await _rows(session) == []
    assert await chats.get_monitor(session, -1000000001400) is None


async def test_a_chat_joined_by_hand_since_the_last_sync_gets_a_default_row(session):
    newcomer = channel(
        1500, title="Namuna Yangi Guruh", username="namuna_yangi", megagroup=True
    )
    msg = message(newcomer, 9, HIT, date=NOW, sender=POSTER)
    assert await ingest.handle_message(session, msg, now=NOW) == "stored"
    await session.commit()
    row = await chats.get_monitor(session, -1000000001500)
    assert row is not None
    assert (row.chat_type, row.status, row.monitor_enabled, row.decided_by) == (
        "group",
        "joined",
        True,
        "rule:default",
    )
    assert (row.seen_count, row.hit_count) == (1, 1)


async def test_a_channel_post_keeps_stock_news_and_drops_an_ask(session):
    await _monitor(session, CHANNEL)
    await session.commit()
    post = message(
        CHANNEL,
        1,
        "Konteyner keldi, 1688 dan toʻgʻridan-toʻgʻri",
        date=NOW,
        sender=CHANNEL,
    )
    ask = message(CHANNEL, 2, "Kimda 3 kub joy bor? Kerak", date=NOW, sender=CHANNEL)
    assert await ingest.handle_message(session, post, now=NOW) == "stored"
    assert await ingest.handle_message(session, ask, now=NOW) == "seen"
    await session.commit()
    [row] = await _rows(session)
    assert row.prefilter_hit == ["container", "china", "wholesale"]
    # A channel's own post: the sender is the channel, named by its title.
    assert (row.sender_id, row.sender_username, row.sender_name) == (
        -1000000001200,
        "namuna_kanal",
        "Namuna Kanal",
    )


async def test_a_long_message_is_cut_to_the_column_and_a_nul_never_reaches_it(session):
    await _monitor(session, GROUP)
    await session.commit()
    text = "2 kub mato kerak\x00 " + "x" * 5000
    msg = message(GROUP, 1, text, date=NOW, sender=POSTER)
    assert await ingest.handle_message(session, msg, now=NOW) == "stored"
    await session.commit()
    [row] = await _rows(session)
    assert len(row.text) == ingest.TEXT_MAX
    assert "\x00" not in row.text
    assert row.text.startswith("2 kub mato kerak x")


# --- edits -----------------------------------------------------------------------------


async def test_an_edit_that_makes_a_hit_stores_it_and_notifies(session):
    await _monitor(session, GROUP)
    await session.commit()
    edits = ingest.EditCounter()
    first = message(GROUP, 60, MISS, date=NOW, sender=POSTER)
    assert await ingest.handle_message(session, first, now=NOW) == "seen"
    await session.commit()
    async with Listener() as listener:
        edited = message(GROUP, 60, HIT, date=NOW, sender=POSTER)
        assert await ingest.handle_edit(session, edited, now=NOW, edits=edits) == "stored"
        await session.commit()
        heard = await listener.heard(expect=1)
    [row] = await _rows(session)
    assert heard == [str(row.id)]
    assert row.text == HIT
    # An edit is not a new message: seen stays 1, the hit is counted.
    assert await _counts(session, GROUP) == (1, 1, 60)


async def test_edits_re_run_the_prefilter_at_most_ten_times(session, monkeypatch):
    from clientscrapper.userbot import prefilter

    await _monitor(session, GROUP)
    await session.commit()
    edits = ingest.EditCounter()
    calls: list[str] = []
    real = prefilter.hits

    def spy(text, *, chat_type):
        calls.append(text)
        return real(text, chat_type=chat_type)

    monkeypatch.setattr(prefilter, "hits", spy)
    outcomes = []
    for n in range(1, 11):
        edited = message(GROUP, 70, f"{MISS} {n}", date=NOW, sender=POSTER)
        outcomes.append(await ingest.handle_edit(session, edited, now=NOW, edits=edits))
    # The eleventh edit would be a hit; the cap means it is never looked at.
    eleventh = message(GROUP, 70, HIT, date=NOW, sender=POSTER)
    outcomes.append(await ingest.handle_edit(session, eleventh, now=NOW, edits=edits))
    await session.commit()
    assert outcomes == ["unchanged"] * 10 + ["capped"]
    assert len(calls) == ingest.MAX_EDITS == 10
    assert await _rows(session) == []
    # Another message still has its own ten.
    other = message(GROUP, 71, HIT, date=NOW, sender=POSTER)
    assert await ingest.handle_edit(session, other, now=NOW, edits=edits) == "stored"


async def test_the_tenth_edit_still_counts(session):
    await _monitor(session, GROUP)
    await session.commit()
    edits = ingest.EditCounter()
    for n in range(1, 10):
        edited = message(GROUP, 80, f"{MISS} {n}", date=NOW, sender=POSTER)
        assert (
            await ingest.handle_edit(session, edited, now=NOW, edits=edits) == "unchanged"
        )
    tenth = message(GROUP, 80, HIT, date=NOW, sender=POSTER)
    assert await ingest.handle_edit(session, tenth, now=NOW, edits=edits) == "stored"


async def _proof(session, message_id: int) -> m.Evidence:
    """The evidence row the telegram_monitor collector keeps of a message."""
    proof = m.Evidence(
        source_key="telegram_monitor",
        url=f"https://t.me/namuna_optom/{message_id}",
        fetched_at=NOW,
        sha256="0" * 64,
        content_type="text/plain",
        bytes=len(HIT.encode()),
        excerpt=HIT,
        expires_at=NOW + timedelta(days=180),
    )
    session.add(proof)
    await session.flush()
    return proof


async def test_an_edit_updates_an_unprocessed_hit_and_leaves_a_processed_one(session):
    await _monitor(session, GROUP)
    await session.commit()
    edits = ingest.EditCounter()
    for n in (90, 91):
        msg = message(GROUP, n, HIT, date=NOW, sender=POSTER)
        assert await ingest.handle_message(session, msg, now=NOW) == "stored"
    await session.commit()
    # 91 is handled: the collector linked its evidence, the classifier read it
    # (a handled row always carries its evidence; one without is only counted,
    # see test_an_edit_reopens_a_price_list_the_collector_only_counted).
    proof = await _proof(session, 91)
    await session.execute(
        sa.update(m.Interaction)
        .where(m.Interaction.tg_message_id == 91)
        .values(processed_at=NOW, evidence_id=proof.id)
    )
    await session.commit()
    newer = "Хитойдан 5 куб товар керак, ким олиб келади?"
    first = message(GROUP, 90, newer, date=NOW, sender=POSTER)
    second = message(GROUP, 91, newer, date=NOW, sender=POSTER)
    assert await ingest.handle_edit(session, first, now=NOW, edits=edits) == "updated"
    assert await ingest.handle_edit(session, second, now=NOW, edits=edits) == "unchanged"
    # An edit that is no hit is not kept, and the row it would replace stays.
    gone = message(GROUP, 90, MISS, date=NOW, sender=POSTER)
    assert await ingest.handle_edit(session, gone, now=NOW, edits=edits) == "unchanged"
    await session.commit()
    rows = {r.tg_message_id: r for r in await _rows(session)}
    assert rows[90].text == newer
    assert rows[90].prefilter_hit == ["volume_kub", "china", "intent"]
    assert rows[91].text == HIT


async def test_an_edit_leaves_a_hit_the_collector_already_observed(session):
    """The telegram_monitor collector stored the text as evidence and quotes
    it in an observation: an edit landing after that would make the row say
    something its evidence does not (T-14)."""
    await _monitor(session, GROUP)
    await session.commit()
    msg = message(GROUP, 92, HIT, date=NOW, sender=POSTER)
    assert await ingest.handle_message(session, msg, now=NOW) == "stored"
    await session.commit()
    proof = await _proof(session, 92)
    await session.execute(
        sa.update(m.Interaction)
        .where(m.Interaction.tg_message_id == 92)
        .values(evidence_id=proof.id)
    )
    await session.commit()
    newer = "Хитойдан 5 куб товар керак, ким олиб келади?"
    edit = message(GROUP, 92, newer, date=NOW, sender=POSTER)
    assert (
        await ingest.handle_edit(session, edit, now=NOW, edits=ingest.EditCounter())
        == "unchanged"
    )
    await session.commit()
    [row] = [r for r in await _rows(session) if r.tg_message_id == 92]
    assert row.text == HIT


async def test_an_edit_reopens_a_price_list_the_collector_only_counted(session):
    """The collector counts a person's price list with no buyer trigger and
    keeps nothing of it (no evidence, no observation): an edit that makes it
    an ask is read again, and announced as a new message is (T-14)."""
    await _monitor(session, GROUP)
    await session.commit()
    narxlar = "Narxlar: stul 250000 so'm, stol 900000 so'm, shkaf 1500000 so'm"
    msg = message(GROUP, 93, narxlar, date=NOW, sender=POSTER)
    assert await ingest.handle_message(session, msg, now=NOW) == "stored"
    await session.commit()
    await session.execute(
        sa.update(m.Interaction)
        .where(m.Interaction.tg_message_id == 93)
        .values(processed_at=NOW)  # what the collector does with it
    )
    await session.commit()
    [counted] = [r for r in await _rows(session) if r.tg_message_id == 93]
    asked = narxlar + ". Xitoydan 3 kub mebel kerak, kim olib keladi?"
    edit = message(GROUP, 93, asked, date=NOW, sender=POSTER)
    async with Listener() as listener:
        assert (
            await ingest.handle_edit(session, edit, now=NOW, edits=ingest.EditCounter())
            == "updated"
        )
        await session.commit()
        heard = await listener.heard(expect=1)
    [row] = [r for r in await _rows(session) if r.tg_message_id == 93]
    assert (row.text, row.processed_at) == (asked, None)
    assert row.prefilter_hit == ["volume_kub", "china", "intent", "pricelist"]
    assert heard == [str(counted.id)]


def test_the_edit_memory_forgets_the_oldest_message():
    edits = ingest.EditCounter(limit=2, memory=2)
    assert edits.allow(1, 1) and edits.allow(1, 1)
    assert not edits.allow(1, 1)
    assert edits.allow(1, 2) and edits.allow(1, 3)
    # (1, 1) was pushed out by two newer messages: it starts again.
    assert edits.allow(1, 1)


@pytest.mark.parametrize(
    ("overrides", "entity"),
    [
        ({"monitor_enabled": False}, GROUP),
        ({"role": "calibration"}, GROUP),
        ({"status": "left"}, GROUP),
        ({}, user(5200000011, username="namuna_11")),
    ],
    ids=["off", "calibration", "left", "private"],
)
async def test_an_edit_in_a_chat_that_is_not_read_stores_nothing(
    session, overrides, entity
):
    await _monitor(session, entity, **overrides)
    await session.commit()
    edits = ingest.EditCounter()
    edited = message(entity, 70, HIT, date=NOW, sender=POSTER)
    assert await ingest.handle_edit(session, edited, now=NOW, edits=edits) == "ignored"
    await session.commit()
    assert await _rows(session) == []
    assert (await _counts(session, entity))[:2] == (0, 0)
