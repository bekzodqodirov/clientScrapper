"""The event-driven path (04 §0, 01 N-06): a ``NOTIFY cs_interaction`` wakes
the jobs that wait on it, through a ``LISTEN`` connection of the worker's own.

The scheduler is APScheduler's own, started on the test's loop, with crons
that would not fire for a year: every run counted here is a wake. The last
test is the whole chain on Postgres — the reader's insert and notify, the
listener, the telegram_monitor collector run that links the message — with
no Telegram and no network.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import chats
from clientscrapper.userbot import ingest, prefilter
from clientscrapper.worker import main as worker

TZ = ZoneInfo("Asia/Tashkent")
NEVER = "0 0 1 1 *"  # once a year: no run in a test is the cron's


def _recording(ran: list[str], slot_id: str, *, hold: asyncio.Event | None = None):
    async def job() -> None:
        ran.append(slot_id)
        if hold is not None:
            await hold.wait()

    return job


def _slot(slot_id: str, ran: list[str], **fields) -> worker.JobSlot:
    return worker.JobSlot(
        slot_id,
        (NEVER,),
        "T-14",
        fields.pop("job", None) or _recording(ran, slot_id),
        wakes_on=fields.pop("wakes_on", chats.INTERACTION_CHANNEL),
        **fields,
    )


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


@pytest.fixture(autouse=True)
def _no_pending_wakes():
    worker._wakes.pending.clear()
    yield
    worker._wakes.pending.clear()


def test_origin_is_the_slot_a_payload_names():
    assert worker.origin_of("telegram_monitor:12") == "telegram_monitor"
    assert worker.origin_of("12") is None  # the reader's bare id
    assert worker.origin_of(":12") is None
    assert worker.origin_of("") is None


def test_only_built_slots_wake_and_by_their_channel():
    ran: list[str] = []
    slots = (
        _slot("a", ran),
        _slot("b", ran, wakes_on="other_channel"),
        worker.JobSlot("c", (NEVER,), "T-15", wakes_on=chats.INTERACTION_CHANNEL),
        _slot("d", ran, wakes_on=None),
    )
    assert worker.waking(slots) == {"cs_interaction": ("a",), "other_channel": ("b",)}
    # The table: the reader's channel wakes the collector (classify and score
    # join it once T-15 and T-18 build them).
    assert worker.waking()["cs_interaction"] == ("telegram_monitor",)


async def test_a_wake_during_a_run_runs_the_job_once_more_after_it():
    """max_instances=1 refuses the scheduler's second instance; the wake is
    kept and the job runs again when it ends — never twice at once."""
    ran: list[str] = []
    hold = asyncio.Event()
    job = worker.wakeable("a", _recording(ran, "a", hold=hold))
    running = asyncio.ensure_future(job())
    await _until(lambda: ran == ["a"])
    worker._wakes.pending.add("a")  # what wake() records while it runs
    worker._wakes.pending.add("a")
    hold.set()
    await asyncio.wait_for(running, 5)
    assert ran == ["a", "a"]
    again = asyncio.ensure_future(job())
    await asyncio.wait_for(again, 5)
    assert ran == ["a", "a", "a"]  # no wake pending: one run


async def test_a_notification_wakes_every_slot_but_the_one_that_said_it(session):
    ran: list[str] = []
    slots = (_slot("a", ran), _slot("b", ran))
    scheduler = worker.build_scheduler(slots)
    stop = asyncio.Event()
    connected = asyncio.Event()
    scheduler.start()
    listener = asyncio.ensure_future(
        worker.listen_for_wakes(scheduler, stop, slots=slots, connected=connected.set)
    )
    try:
        await asyncio.wait_for(connected.wait(), 10)
        # On connect every waking job runs once: what was said while nobody
        # listened is not heard again.
        await _until(lambda: sorted(ran) == ["a", "b"])
        async with SessionLocal() as session:
            await session.execute(sa.select(sa.func.pg_notify("cs_interaction", "7")))
            await session.commit()
        await _until(lambda: sorted(ran) == ["a", "a", "b", "b"])
        async with SessionLocal() as session:
            await session.execute(sa.select(sa.func.pg_notify("cs_interaction", "a:7")))
            await session.commit()
        await _until(lambda: ran.count("b") == 3)
        await asyncio.sleep(0.5)
        assert ran.count("a") == 2  # a's own announcement does not wake a
        async with SessionLocal() as session:
            # Inside a transaction that rolls back: never delivered.
            await session.execute(sa.select(sa.func.pg_notify("cs_interaction", "8")))
            await session.rollback()
        await asyncio.sleep(0.5)
        assert (ran.count("a"), ran.count("b")) == (2, 3)
    finally:
        stop.set()
        await asyncio.wait_for(listener, 10)
        scheduler.shutdown(wait=False)


async def test_a_listener_that_cannot_connect_keeps_trying_until_stopped(monkeypatch):
    ran: list[str] = []
    slots = (_slot("a", ran),)
    scheduler = worker.build_scheduler(slots)
    monkeypatch.setattr(worker, "WAKE_RETRY", timedelta(seconds=0.05))
    stop = asyncio.Event()
    listener = asyncio.ensure_future(
        worker.listen_for_wakes(
            scheduler,
            stop,
            slots=slots,
            database_url="postgresql+psycopg://none:none@127.0.0.1:1/none",
        )
    )
    await asyncio.sleep(0.3)
    assert not listener.done()
    stop.set()
    await asyncio.wait_for(listener, 10)
    assert ran == []


# --- the whole chain (Postgres) --------------------------------------------------------


@pytest.fixture
async def enabled(session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "evidence_dir", str(tmp_path))
    async with SessionLocal() as s:
        row = await s.get(m.Source, "telegram_monitor")
        assert row is not None
        before = row.enabled
        row.enabled = True
        await s.commit()
    yield
    async with SessionLocal() as s:
        await s.execute(
            sa.update(m.Source)
            .where(m.Source.key == "telegram_monitor")
            .values(enabled=before)
        )
        await s.commit()


async def test_the_readers_notify_wakes_the_collector_which_links_the_message(
    session, enabled
):
    """N-06's first hop: the reader commits a hit with its NOTIFY, the worker
    hears it and runs telegram_monitor at once — not at the next */15 sweep —
    and the collector's own NOTIFY for the classifier does not wake it again."""
    now = datetime.now(TZ)
    await chats.ensure_monitor(
        session,
        chats.DialogInfo(-1001000000001, "group", "Namuna Optom", "namuna_optom"),
        now=now - timedelta(days=1),
    )
    await session.commit()
    slots = tuple(s for s in worker.job_table() if s.id == "telegram_monitor")
    scheduler = worker.build_scheduler(slots, source_crons={"telegram_monitor": NEVER})
    stop = asyncio.Event()
    connected = asyncio.Event()

    async def runs() -> list[m.CollectorRun]:
        async with SessionLocal() as s:
            return list(
                await s.scalars(
                    sa.select(m.CollectorRun).where(
                        m.CollectorRun.source_key == "telegram_monitor"
                    )
                )
            )

    scheduler.start()
    listener = asyncio.ensure_future(
        worker.listen_for_wakes(scheduler, stop, slots=slots, connected=connected.set)
    )
    try:
        await asyncio.wait_for(connected.wait(), 10)
        finished: list[m.CollectorRun] = []
        for _ in range(200):  # the connect wake's run, with nothing to do
            finished = [r for r in await runs() if r.status != "running"]
            if finished:
                break
            await asyncio.sleep(0.05)
        assert [r.status for r in finished] == ["ok"]

        text = "Guangzhoudan 25 kub mato olib kelish kerak, kim qiladi?"
        async with SessionLocal() as s:
            row_id = await ingest.store_hit(
                s,
                ingest.Incoming(
                    tg_chat_id=-1001000000001,
                    tg_message_id=18234,
                    text=text,
                    occurred_at=now,
                    sender_id=5100000001,
                    sender_username="namuna_tekstil",
                    sender_name="Rustam",
                    media_kind="none",
                    reply_to_message_id=None,
                    fwd_from_peer_id=None,
                ),
                prefilter.hits(text, chat_type="group"),
            )
            await s.commit()
        assert row_id is not None

        linked: int | None = None
        for _ in range(200):
            async with SessionLocal() as s:
                linked = await s.scalar(
                    sa.select(m.Interaction.observation_id).where(
                        m.Interaction.id == row_id
                    )
                )
            if linked is not None:
                break
            await asyncio.sleep(0.05)
        assert linked is not None
        await asyncio.sleep(1.0)
        assert len(await runs()) == 2  # the connect wake and the reader's
    finally:
        stop.set()
        await asyncio.wait_for(listener, 10)
        scheduler.shutdown(wait=False)
        await worker.shared_fetcher().aclose()
        worker._shared.fetcher = None
