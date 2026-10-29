"""``classify_job`` (04 §0, §4.1, §4.3) on Postgres: the queue telegram_monitor
leaves, one transaction and one ``costs.ask(operation='classify')`` per
message, what each outcome does to the row, the day's cap, the seams for the
signal writer, and the wake on ``NOTIFY cs_interaction`` with the 15-minute
sweep as the fallback. Model answers are SYNTHETIC
(tests/fixtures/model/README.md).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import classify, companies, costs
from clientscrapper.services.classify import Classified
from clientscrapper.userbot import ingest, prefilter
from clientscrapper.worker import main as worker
from tests import classify_fixtures as cf
from tests.classify_fixtures import (
    ASK,
    CHANNEL_ID,
    NOW,
    TZ,
    chat,
    interaction,
    link,
    model_jobs,
    store,
    usage,
)
from tests.fakes import api_error, message, model_fixture, overloaded

enabled = cf.enabled
offline = cf.offline
BUYER = model_fixture("classify_buyer_stated.json")
NEVER = "0 0 1 1 *"


class Listener:
    """``LISTEN cs_interaction`` on its own autocommit connection."""

    async def __aenter__(self) -> Listener:
        dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self.conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        await self.conn.execute("LISTEN cs_interaction")
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.conn.close()

    async def heard(self, timeout: float = 1.0) -> list[str]:
        return [n.payload async for n in self.conn.notifies(timeout=timeout)]


async def queued(session, offline, tmp_path, *texts: str) -> list[int]:
    """Messages stored and linked: the classifier's queue."""
    await chat(session)
    ids = [await store(session, text, message_id=100 + n) for n, text in enumerate(texts)]
    await link(offline, tmp_path)
    return ids


async def test_the_worked_example_is_classified_and_announced(
    session, enabled, offline, tmp_path, model_api
):
    [row_id] = await queued(session, offline, tmp_path, ASK)
    model_api.queue(message(BUYER))
    async with Listener() as listener:
        report = await classify.classify_batch(now=NOW)
        heard = await listener.heard()

    assert report.as_dict() == {
        "claimed": 1,
        "applied": 1,
        "failed": 0,
        "retried": 0,
        "waiting": 0,
        "given_up": 0,
        "skipped": 0,
        "refused": 0,
        "capped": 0,
    }
    # The scorer is told at once; the payload names classify, so the worker
    # never wakes classify on its own announcement.
    assert heard == [f"classify:{row_id}"]
    assert worker.origin_of(heard[0]) == "classify"
    row = await interaction(session, row_id)
    assert row.processed_at == NOW
    [job] = await model_jobs(session)
    assert (job.kind, job.status, job.evidence_ids, job.company_id) == (
        "classify",
        "applied",
        [row.evidence_id],
        None,  # the resolver has not run yet
    )
    assert job.result["volume_quote"] == "2 kub mato"
    assert job.result["volume_basis"] == "stated"
    [paid] = await usage(session)
    assert (paid.operation, paid.model_job_id, paid.cost_usd) == (
        "classify",
        job.id,
        Decimal("0.000114"),
    )
    # The request names the source, the date, the bio and the text.
    content = model_api.requests[0]["messages"][0]["content"]
    assert content.splitlines() == [
        "SOURCE: telegram group «Namuna Optom»",
        "DATE: 2026-10-24T11:58:00+05:00",
        "SENDER_BIO: Namuna Tekstil Savdo MChJ direktori",
        f"TEXT: {ASK}",
    ]

    # The resolver's hook finds the result by the message's evidence.
    found = await classify.classification_for(session, row.evidence_id)
    assert found == Classified(
        interaction_id=row_id,
        evidence_id=row.evidence_id,
        observation_id=row.observation_id,
        company_id=None,
        model_job_id=job.id,
        model=settings.extract_model,
        signal=job.result,
        quote="2 kub mato",
    )
    assert await classify.classification_for(session, row.evidence_id + 999) is None

    # Nothing is left: a second run claims nothing and calls nobody.
    again = await classify.classify_batch(now=NOW + timedelta(minutes=1))
    assert again.claimed == 0 and len(model_api.requests) == 1


async def test_the_sender_is_masked_and_a_chat_speaks_for_itself(
    session, enabled, offline, tmp_path, model_api
):
    await chat(session)
    await chat(
        session,
        CHANNEL_ID,
        "channel",
        title="Namuna Mebel Optom",
        username="namuna_mebel",
    )
    await store(session, "Rustam: 3 kub mebel kerak, kim olib keladi?", message_id=1)
    await store(
        session,
        "Yangi tovar keldi: oshxona mebeli optom, Xitoydan konteyner",
        message_id=2,
        chat_id=CHANNEL_ID,
        chat_type="channel",
        sender_id=CHANNEL_ID,
        username=None,
        name=None,
        bio=None,
    )
    await link(offline, tmp_path)
    model_api.queue(message(BUYER), message(BUYER))
    report = await classify.classify_batch(now=NOW)
    assert report.applied == 2
    person, channel = (r["messages"][0]["content"] for r in model_api.requests)
    assert person.splitlines()[3] == "TEXT: <NAME_1>: 3 kub mebel kerak, kim olib keladi?"
    assert channel.splitlines()[:3] == [
        "SOURCE: telegram channel «Namuna Mebel Optom»",
        "DATE: 2026-10-24T11:58:00+05:00",
        "SENDER_BIO: -",
    ]


def test_a_display_name_masks_its_name_words_not_its_business_words():
    assert classify.name_words("Rustam Karimov") == ["Rustam", "Karimov"]
    assert classify.name_words("Рустам Иванов") == ["Рустам", "Иванов"]
    # A word nothing reads as business is a name (a name not in FIRST_NAMES).
    assert classify.name_words("Shahzodbek") == ["Shahzodbek"]
    assert classify.name_words("Oʻtkir Gʻaniyev") == ["Oʻtkir", "Gʻaniyev"]
    # A given name is masked inside a business label too; «aka» is no name.
    assert classify.name_words("Sardor Savdo") == ["Sardor"]
    assert classify.name_words("Dilnoza opa") == ["Dilnoza"]
    # A business label's other words stay: they are the goods, the origin,
    # the place the model is asked about.
    for label in (
        "Namuna Optom",
        "Kafel Savdo",
        "Xitoy Mollari",
        "Mato Optom",
        "Oyinchoq Uz",
        "R. K. Toshkent Mebel",
    ):
        assert classify.name_words(label) == [], label
    assert classify.name_words(None) == []


async def test_a_business_label_leaves_the_goods_and_the_origin_in_the_text(
    session, enabled, offline, tmp_path, model_api
):
    await chat(session)
    for n, (name, text) in enumerate(
        (
            ("Kafel Savdo", "Xitoydan 2 fura kafel kerak, kim olib keladi?"),
            ("Xitoy Mollari", "Xitoydan 3 kub mato kerak, kim olib keladi?"),
            ("Mato Optom", "Guangzhoudan 5 kub mato kerak"),
        ),
        start=1,
    ):
        await store(session, text, message_id=n, sender_id=5100000100 + n, name=name)
    await link(offline, tmp_path)
    model_api.queue(message(BUYER), message(BUYER), message(BUYER))
    assert (await classify.classify_batch(now=NOW)).applied == 3
    sent = [r["messages"][0]["content"].splitlines()[3] for r in model_api.requests]
    assert sent == [
        "TEXT: Xitoydan 2 fura kafel kerak, kim olib keladi?",
        "TEXT: Xitoydan 3 kub mato kerak, kim olib keladi?",
        "TEXT: Guangzhoudan 5 kub mato kerak",
    ]


async def test_a_chat_speaking_in_a_group_keeps_its_name(
    session, enabled, offline, tmp_path, model_api
):
    """A channel posting into a group (a negative sender id) speaks for
    itself: its title is company-tier and is not masked as a person's."""
    await chat(session)
    await store(
        session,
        "Sardor Mebel Optom: Xitoydan 3 kub mebel kerak, kim olib keladi?",
        message_id=1,
        sender_id=-1001000000555,
        username="namuna_mebel",
        name="Sardor Mebel Optom",
        bio=None,
    )
    await link(offline, tmp_path)
    model_api.queue(message(BUYER))
    assert (await classify.classify_batch(now=NOW)).applied == 1
    [body] = model_api.requests
    assert body["messages"][0]["content"].splitlines()[2:] == [
        "SENDER_BIO: -",
        "TEXT: Sardor Mebel Optom: Xitoydan 3 kub mebel kerak, kim olib keladi?",
    ]


async def test_a_row_with_no_bio_takes_the_senders_known_one(
    session, enabled, offline, tmp_path, model_api
):
    await chat(session)
    await store(session, ASK, message_id=1)
    await store(
        session, "Yiwudan 4 kub mebel kerak, kim olib keladi?", message_id=2, bio=None
    )
    await link(offline, tmp_path)
    model_api.queue(message(BUYER), message(BUYER))
    assert (await classify.classify_batch(now=NOW)).applied == 2
    bios = [r["messages"][0]["content"].splitlines()[2] for r in model_api.requests]
    assert bios == [f"SENDER_BIO: {cf.BIO}"] * 2


async def test_only_the_queue_is_classified(
    session, enabled, offline, tmp_path, model_api
):
    await chat(session)
    off = await chat(
        session, -1001000000077, "group", title="Namuna Off", username="namuna_off"
    )
    ask = await store(session, ASK, message_id=1)
    tout = await store(session, "Kargo xizmati: Xitoydan 1 kub 250$", message_id=2)
    prices = "Narxlar: 10 dona 50$, 20 dona 90$, 30 dona 120$"
    price = await store(session, prices, message_id=3, sender_id=None)
    switched_off = await store(session, ASK, message_id=4, chat_id=off.tg_chat_id)
    purged = await store(session, "Yiwudan 4 kub o'yinchoq kerak", message_id=5)
    await link(offline, tmp_path)
    async with SessionLocal() as s:
        await s.execute(
            sa.update(m.ChatMonitor)
            .where(m.ChatMonitor.tg_chat_id == off.tg_chat_id)
            .values(monitor_enabled=False)
        )
        await s.execute(
            sa.update(m.Interaction)
            .where(m.Interaction.id == purged)
            .values(text_purged_at=NOW)
        )
        await s.commit()
    unlinked = await store(session, "Guangzhoudan 6 kub mato kerak", message_id=6)

    model_api.queue(message(BUYER))
    report = await classify.classify_batch(now=NOW)
    assert (report.claimed, report.applied) == (1, 1)
    assert (await interaction(session, ask)).processed_at == NOW
    # A tout and a counted price list are the collector's to close; a row
    # of a switched-off chat, a purged text and an unlinked row wait.
    assert (await interaction(session, tout)).processed_at is not None
    assert (await interaction(session, price)).processed_at is not None
    for row_id in (switched_off, purged, unlinked):
        assert (await interaction(session, row_id)).processed_at is None
    assert len(model_api.requests) == 1


async def test_a_budget_refusal_keeps_the_message_queued(
    session, enabled, offline, tmp_path, model_api, monkeypatch
):
    [first, second] = await queued(session, offline, tmp_path, ASK, ASK + " Tez")
    monkeypatch.setattr(settings, "daily_cost_cap_usd", Decimal("0"))
    report = await classify.classify_batch(now=NOW)
    # Nothing was sent: no job row, the row stays queued, the run stops.
    assert (report.claimed, report.refused, report.applied) == (1, 1, 0)
    assert model_api.requests == []
    assert await model_jobs(session) == []
    for row_id in (first, second):
        assert (await interaction(session, row_id)).processed_at is None
    [refusal] = await usage(session)
    assert (refusal.operation, refusal.model) == ("classify", "refused:cost_cap_daily")

    # While ask remembers the refusal, a run claims nothing at all.
    held = await classify.classify_batch(now=NOW + timedelta(minutes=5))
    assert (held.claimed, held.refused) == (0, 1)

    # Next hour, with room again: both are classified.
    monkeypatch.setattr(settings, "daily_cost_cap_usd", Decimal("10"))
    model_api.queue(message(BUYER), message(BUYER))
    later = await classify.classify_batch(now=NOW + timedelta(hours=1))
    assert later.applied == 2


async def test_no_client_claims_nothing(
    session, enabled, offline, tmp_path, model_api, monkeypatch
):
    [row_id] = await queued(session, offline, tmp_path, ASK)
    from clientscrapper.services import extraction

    extraction.set_client(None)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    report = await classify.classify_batch(now=NOW)
    assert (report.claimed, report.refused) == (0, 1)
    assert (await interaction(session, row_id)).processed_at is None


@pytest.mark.parametrize(
    "answers",
    [
        [message(None, stop_reason="refusal")],
        [message("not json"), message("still not json")],
        [message("{", stop_reason="max_tokens"), message("{", stop_reason="max_tokens")],
    ],
    ids=["refusal", "invalid", "max_tokens"],
)
async def test_a_final_failure_is_never_asked_again(
    session, enabled, offline, tmp_path, model_api, answers
):
    [row_id] = await queued(session, offline, tmp_path, ASK)
    model_api.queue(*answers)
    async with Listener() as listener:
        report = await classify.classify_batch(now=NOW)
        heard = await listener.heard(timeout=0.3)
    assert (report.applied, report.failed) == (0, 1)
    assert heard == []  # nothing to score
    assert (await interaction(session, row_id)).processed_at == NOW
    [job] = await model_jobs(session)
    assert job.status == "failed"
    again = await classify.classify_batch(now=NOW + timedelta(hours=2))
    assert again.claimed == 0


async def test_an_api_failure_stops_the_run_and_the_message_waits_its_turn(
    session, enabled, offline, tmp_path, model_api
):
    [failing, fine] = await queued(session, offline, tmp_path, ASK, ASK + " Tez")
    model_api.queue(api_error(500, "api_error", "Internal server error"))
    first = await classify.classify_batch(now=NOW)
    # The run stops at the failure: the next message would meet the outage.
    assert (first.claimed, first.retried, first.applied) == (1, 1, 0)
    assert len(model_api.requests) == 1
    for row_id in (failing, fine):
        assert (await interaction(session, row_id)).processed_at is None

    # A wake 20 s later: the failed message waits, the next one goes.
    model_api.queue(message(BUYER))
    second = await classify.classify_batch(now=NOW + timedelta(seconds=20))
    assert (second.claimed, second.waiting, second.applied) == (2, 1, 1)
    assert (await interaction(session, fine)).processed_at == NOW + timedelta(seconds=20)

    # 15 minutes after the first failure it is asked again; after the second,
    # it waits 30 minutes.
    model_api.queue(overloaded())
    third = await classify.classify_batch(now=NOW + timedelta(minutes=15))
    assert (third.claimed, third.retried) == (1, 1)
    early = await classify.classify_batch(now=NOW + timedelta(minutes=44))
    assert (early.claimed, early.waiting, len(model_api.requests)) == (1, 1, 3)
    model_api.queue(overloaded())
    fourth = await classify.classify_batch(now=NOW + timedelta(minutes=45))
    assert (fourth.claimed, fourth.retried, len(model_api.requests)) == (1, 1, 4)

    # Three failed calls, 45 minutes apart in all: given up, asking nobody.
    last = await classify.classify_batch(now=NOW + timedelta(minutes=45, seconds=1))
    assert (last.claimed, last.given_up, len(model_api.requests)) == (1, 1, 4)
    row = await interaction(session, failing)
    assert row.processed_at == NOW + timedelta(minutes=45, seconds=1)
    assert await classify.tries(session, row.evidence_id) == 3


async def test_an_outage_under_a_burst_of_wakes_gives_up_no_message(
    session, enabled, offline, tmp_path, model_api
):
    """Wakes 20 s apart during a one-minute 529 burst: each message pays one
    failed attempt, never three, and every one is classified once the API is
    back."""
    ids = await queued(
        session, offline, tmp_path, ASK, ASK + " 1", ASK + " 2", ASK + " 3"
    )
    model_api.queue(*(overloaded() for _ in range(3)))
    for n in range(3):
        run = await classify.classify_batch(now=NOW + timedelta(seconds=20 * n))
        assert (run.retried, run.applied, run.given_up) == (1, 0, 0)
    assert len(model_api.requests) == 3
    model_api.queue(message(BUYER))
    back = await classify.classify_batch(now=NOW + timedelta(minutes=1))
    assert (back.waiting, back.applied) == (3, 1)
    model_api.queue(message(BUYER), message(BUYER), message(BUYER))
    later = await classify.classify_batch(now=NOW + timedelta(minutes=16))
    assert (later.applied, later.given_up) == (3, 0)
    for row_id in ids:
        assert (await interaction(session, row_id)).processed_at is not None
    applied = [j for j in await model_jobs(session) if j.status == "applied"]
    assert len(applied) == 4


async def test_a_budget_stop_mid_call_keeps_the_paid_job(
    session, enabled, offline, tmp_path, model_api, monkeypatch
):
    """The first answer is malformed and the cap is spent before the nudge:
    the paid attempt is a failed job (it counts toward the day), the message
    waits for room, and nothing counts it as the message's failure."""
    [row_id] = await queued(session, offline, tmp_path, ASK)

    def spends_the_cap(request):
        monkeypatch.setattr(settings, "daily_cost_cap_usd", Decimal("0"))
        return message("not json")

    model_api.queue(spends_the_cap)
    report = await classify.classify_batch(now=NOW)
    assert (report.claimed, report.retried, report.applied) == (1, 1, 0)
    assert len(model_api.requests) == 1
    [job] = await model_jobs(session)
    assert (job.status, job.error, job.attempts) == (
        "failed",
        "refused:cost_cap_daily",
        1,
    )
    assert await classify.jobs_today(session, NOW) == 1
    row = await interaction(session, row_id)
    assert row.processed_at is None
    assert await classify.tries(session, row.evidence_id) == 0


async def test_a_failed_job_counts_toward_the_day(
    session, enabled, offline, tmp_path, model_api, monkeypatch
):
    monkeypatch.setattr(classify, "CLASSIFY_DAILY_MAX", 2)
    [first, second] = await queued(session, offline, tmp_path, ASK, ASK + " Tez")
    model_api.queue(overloaded())
    await classify.classify_batch(now=NOW)
    model_api.queue(message(BUYER))
    run = await classify.classify_batch(now=NOW + timedelta(minutes=16))
    assert (run.applied, run.capped) == (1, 1)
    assert (await interaction(session, first)).processed_at is not None
    assert (await interaction(session, second)).processed_at is None


async def test_each_message_is_classified_at_its_own_clock(
    session, enabled, offline, tmp_path, model_api, monkeypatch
):
    """A run that starts before midnight and pays after it books the new day
    — the claim, the job, the cap and the row (runner.ask_gate's rule)."""
    monkeypatch.setattr(classify, "CLASSIFY_DAILY_MAX", 1)
    await queued(session, offline, tmp_path, ASK, ASK + " Tez")
    start = datetime(2026, 10, 24, 23, 59, 58, tzinfo=TZ)
    ticks = iter(
        [
            datetime(2026, 10, 24, 23, 59, 59, tzinfo=TZ),
            datetime(2026, 10, 25, 0, 0, 30, tzinfo=TZ),
            datetime(2026, 10, 25, 0, 0, 31, tzinfo=TZ),
        ]
    )
    model_api.queue(message(BUYER), message(BUYER))
    report = await classify.classify_batch(now=start, clock=lambda: next(ticks))
    # One a day: the cap of the 24th does not stop the 25th's.
    assert report.applied == 2
    jobs = await model_jobs(session)
    assert [j.created_at for j in jobs] == [
        datetime(2026, 10, 24, 23, 59, 59, tzinfo=TZ),
        datetime(2026, 10, 25, 0, 0, 30, tzinfo=TZ),
    ]
    assert [u.created_at for u in await usage(session)] == [j.created_at for j in jobs]
    assert [j.applied_at for j in jobs] == [j.created_at for j in jobs]


async def test_the_day_has_a_cap(
    session, enabled, offline, tmp_path, model_api, monkeypatch
):
    monkeypatch.setattr(classify, "CLASSIFY_DAILY_MAX", 2)
    ids = await queued(session, offline, tmp_path, ASK, ASK + " 1", ASK + " 2")
    model_api.queue(message(BUYER), message(BUYER), message(BUYER))
    report = await classify.classify_batch(now=NOW)
    assert (report.applied, report.capped) == (2, 1)
    assert (await interaction(session, ids[2])).processed_at is None
    # The cap is per Tashkent day: after midnight the third goes.
    tomorrow = datetime(2026, 10, 25, 0, 5, tzinfo=TZ)
    assert await classify.jobs_today(session, tomorrow) == 0
    later = await classify.classify_batch(now=tomorrow)
    assert later.applied == 1


async def test_a_rejected_observation_is_closed_without_a_call(
    session, enabled, offline, tmp_path, model_api
):
    [row_id] = await queued(session, offline, tmp_path, ASK)
    row = await interaction(session, row_id)
    async with SessionLocal() as s:
        await s.execute(
            sa.update(m.Observation)
            .where(m.Observation.id == row.observation_id)
            .values(status="rejected", reject_reason="competitor")
        )
        await s.commit()
    report = await classify.classify_batch(now=NOW)
    assert (report.claimed, report.skipped) == (1, 1)
    assert model_api.requests == []
    assert (await interaction(session, row_id)).processed_at == NOW


async def test_the_hooks_run_when_the_company_is_known(
    session, enabled, offline, tmp_path, model_api
):
    [row_id] = await queued(session, offline, tmp_path, ASK)
    await companies.resolve_batch(now=NOW)
    row = await interaction(session, row_id)
    obs = await session.get(m.Observation, row.observation_id, populate_existing=True)
    assert obs is not None and obs.company_id is not None
    seen: list[Classified] = []

    async def hook(s, classified: Classified, now: datetime) -> None:
        seen.append(classified)

    async def broken(s, classified: Classified, now: datetime) -> None:
        raise RuntimeError("a signal writer that fails")

    model_api.queue(message(BUYER))
    report = await classify.classify_batch(now=NOW, hooks=[broken, hook])
    assert report.applied == 1
    [job] = await model_jobs(session)
    assert seen == [
        Classified(
            interaction_id=row_id,
            evidence_id=row.evidence_id,
            observation_id=obs.id,
            company_id=obs.company_id,
            model_job_id=job.id,
            model=settings.extract_model,
            signal=job.result,
            quote="2 kub mato",
        )
    ]
    # The failing hook cost only itself; the spend is the company's.
    assert (job.status, job.company_id) == ("applied", obs.company_id)
    [paid] = await usage(session)
    assert paid.company_id == obs.company_id
    assert (await interaction(session, row_id)).processed_at == NOW


async def test_no_hook_runs_before_the_company_exists(
    session, enabled, offline, tmp_path, model_api
):
    await queued(session, offline, tmp_path, ASK)
    seen: list[Classified] = []

    async def hook(s, classified: Classified, now: datetime) -> None:
        seen.append(classified)

    classify.register_after_classify(hook)
    try:
        model_api.queue(message(BUYER))
        await classify.classify_batch(now=NOW)
    finally:
        classify.AFTER_CLASSIFY.remove(hook)
    assert seen == []


def test_the_slot_is_built_and_wakes_on_cs_interaction():
    [slot] = [s for s in worker.job_table() if s.id == "classify"]
    assert (slot.triggers, slot.task, slot.wakes_on) == (
        ("every 15m",),
        "T-15",
        "cs_interaction",
    )
    assert slot.job is classify.classify_job
    assert worker.waking()["cs_interaction"] == ("telegram_monitor", "classify", "score")
    scheduler = worker.build_scheduler((slot,))
    [job] = scheduler.get_jobs()
    assert job.trigger.interval == timedelta(minutes=15)  # the fallback sweep


async def test_the_collectors_notify_wakes_classify_which_classifies(
    session, enabled, model_api, tmp_path, monkeypatch
):
    """N-06's second hop: the reader's insert wakes the collector, whose
    ``telegram_monitor:<id>`` wakes classify at once — not at the 15-minute
    sweep — and classify's own ``classify:<id>`` does not wake it again."""
    monkeypatch.setattr(settings, "evidence_dir", str(tmp_path))
    now = datetime.now(TZ)
    await chat(session)
    slots = tuple(
        s for s in worker.job_table() if s.id in ("telegram_monitor", "classify")
    )
    scheduler = worker.build_scheduler(slots, source_crons={"telegram_monitor": NEVER})
    stop = asyncio.Event()
    connected = asyncio.Event()
    model_api.queue(message(BUYER))
    scheduler.start()
    listener = asyncio.ensure_future(
        worker.listen_for_wakes(scheduler, stop, slots=slots, connected=connected.set)
    )
    try:
        await asyncio.wait_for(connected.wait(), 10)
        await asyncio.sleep(0.5)  # the connect wake's runs, with nothing to do
        async with SessionLocal() as s:
            row_id = await ingest.store_hit(
                s,
                ingest.Incoming(
                    tg_chat_id=cf.GROUP_ID,
                    tg_message_id=18234,
                    text=ASK,
                    occurred_at=now,
                    sender_id=cf.POSTER,
                    sender_username="namuna_tekstil",
                    sender_name="Rustam",
                    media_kind="none",
                    reply_to_message_id=None,
                    fwd_from_peer_id=None,
                ),
                prefilter.hits(ASK, chat_type="group"),
            )
            await s.commit()
        assert row_id is not None
        processed = None
        for _ in range(200):
            async with SessionLocal() as s:
                processed = await s.scalar(
                    sa.select(m.Interaction.processed_at).where(
                        m.Interaction.id == row_id
                    )
                )
            if processed is not None:
                break
            await asyncio.sleep(0.05)
        assert processed is not None
        await asyncio.sleep(1.0)
        assert len(model_api.requests) == 1
        assert len(await model_jobs(session)) == 1
    finally:
        stop.set()
        await asyncio.wait_for(listener, 10)
        scheduler.shutdown(wait=False)
        await worker.shared_fetcher().aclose()
        worker._shared.fetcher = None
        costs.clear_refusals()


async def test_a_message_being_classified_holds_its_observation_from_the_resolver(
    session, enabled, offline, tmp_path, model_api
):
    """The result must meet its company: while classify holds a message, the
    resolver passes its observation by (and resolves it after the result is
    committed, where its hook finds it); one the resolver holds, classify
    passes by in turn."""
    [row_id] = await queued(session, offline, tmp_path, ASK)
    async with SessionLocal() as held, held.begin():
        claim = await classify._claim(held, set())
        assert claim is not None and claim.row.id == row_id
        passed = await companies.resolve_batch(now=NOW)
        assert passed.claimed == 0
    resolved = await companies.resolve_batch(now=NOW)
    assert resolved.claimed == 1
    model_api.queue(message(BUYER))
    first = await classify.classify_batch(now=NOW)
    assert first.applied == 1  # after the resolver: its hook finds the company

    other = await store(session, ASK + " Yiwudan", message_id=200)
    await link(offline, tmp_path, now=NOW + timedelta(minutes=1))
    row = await interaction(session, other)
    async with SessionLocal() as held, held.begin():
        await held.execute(
            sa.select(m.Observation.id)
            .where(m.Observation.id == row.observation_id)
            .with_for_update()
        )
        skipped = await classify.classify_batch(now=NOW)
        assert (skipped.claimed, len(model_api.requests)) == (0, 1)
    model_api.queue(message(BUYER))
    later = await classify.classify_batch(now=NOW)
    assert (later.claimed, later.applied) == (1, 1)  # it was only held
    assert (await interaction(session, other)).processed_at == NOW


async def test_the_text_is_masked_whole_then_cut_to_what_can_be_verified(
    session, enabled, offline, tmp_path, model_api
):
    """TEXT is the excerpt's bound (02 §2.7: a quote must be the excerpt's own
    fragment), masked before it is cut: a phone across the cut leaves no
    digits behind, whatever offset the cut falls at."""
    assert classify.TEXT_MAX == 600
    await chat(session)
    filler = "mato kerak sifatli bo'lsin " * 40  # no digit anywhere
    texts = []
    for n, k in enumerate(range(0, 24, 3)):
        head = filler[: classify.TEXT_MAX - 8 - k].rstrip()
        texts.append(f"Guangzhoudan 2 kub {head} tel +998 90 000 00 01 yozing")
        await store(session, texts[-1], message_id=300 + n)
    await link(offline, tmp_path)
    model_api.queue(*(message(BUYER) for _ in texts))
    assert (await classify.classify_batch(now=NOW)).applied == len(texts)
    for body in model_api.requests:
        said = body["messages"][0]["content"].splitlines()[3].removeprefix("TEXT: ")
        assert len(said) <= classify.TEXT_MAX
        assert said.startswith("Guangzhoudan 2 kub mato kerak")
        # Only the volume's own digit: no part of the phone.
        assert re.findall(r"\d+", said.replace("<PHONE_1>", "")) == ["2"]
    for job in await model_jobs(session):
        # The map keeps what the request carries, nothing more.
        assert set(job.pseudonyms or {}) <= {"<PHONE_1>"}
        assert ("<PHONE_1>" in job.request["user"]) == bool(job.pseudonyms)


def test_a_cut_never_splits_a_token():
    assert classify.clip_masked("mato kerak <PHONE_1>", 14) == "mato kerak"
    assert classify.clip_masked("mato <PHONE_12>", 12) == "mato"
    assert classify.clip_masked("ab<NAME_1>", 6) == "ab"
    assert classify.clip_masked("mato <NAME_1>ga", 40) == "mato <NAME_1>ga"


async def test_seven_goods_are_an_answer_not_a_failure(
    session, enabled, offline, tmp_path, model_api
):
    """Structured outputs cannot hold maxItems (the SDK moves it into the
    description): seven goods are cut to six, never refused."""
    await chat(session)
    text = (
        "Xitoydan optom kerak: mato, ip, tugma, zamok, rezina, lenta, kley. "
        "Jami 5 kub, kim olib keladi?"
    )
    await store(session, text, message_id=301)
    await link(offline, tmp_path)
    answer = {
        **BUYER,
        "goods": ["mato", "ip", "tugma", "zamok", "rezina", "lenta", "kley"],
        "volume_m3_est": 5,
        "volume_quote": "Jami 5 kub",
        "confidence": 1.3,
    }
    model_api.queue(message(answer))
    report = await classify.classify_batch(now=NOW)
    assert (report.applied, report.failed, len(model_api.requests)) == (1, 0, 1)
    [job] = await model_jobs(session)
    assert job.result["goods"] == ["mato", "ip", "tugma", "zamok", "rezina", "lenta"]
    assert job.result["confidence"] == 1.0
    assert job.attempts == 1


async def test_a_message_passed_by_is_not_left_locked(
    session, enabled, offline, tmp_path, model_api
):
    """The resolver holds the first message's observation: classify takes the
    second, and the first is free for the reader's edit while classify works
    — its lock went with the savepoint that took it."""
    await chat(session)
    held_id = await store(session, ASK, message_id=401)
    free_id = await store(session, ASK + " Tez", message_id=402)
    await link(offline, tmp_path)
    held = await interaction(session, held_id)
    async with SessionLocal() as resolver, resolver.begin():
        await resolver.execute(
            sa.select(m.Observation.id)
            .where(m.Observation.id == held.observation_id)
            .with_for_update()
        )
        async with SessionLocal() as worker_session, worker_session.begin():
            claim = await classify._claim(worker_session, set())
            assert claim is not None and claim.row.id == free_id
            async with SessionLocal() as reader, reader.begin():
                locked = await reader.execute(
                    sa.select(m.Interaction.id)
                    .where(m.Interaction.id == held_id)
                    .with_for_update(nowait=True)
                )
                assert locked.scalar_one() == held_id


async def test_a_hook_that_breaks_the_transaction_costs_only_itself(
    session, enabled, offline, tmp_path, model_api
):
    """A signal writer whose statement fails in the database: its savepoint
    rolls back and the result stays applied (without the savepoint the
    whole transaction would be aborted and the paid result lost)."""
    [row_id] = await queued(session, offline, tmp_path, ASK)
    await companies.resolve_batch(now=NOW)

    async def broken_sql(s, classified: Classified, now: datetime) -> None:
        await s.execute(sa.text("SELECT 1 / 0"))

    model_api.queue(message(BUYER))
    report = await classify.classify_batch(now=NOW, hooks=[broken_sql])
    assert report.applied == 1
    [job] = await model_jobs(session)
    assert job.status == "applied"
    assert (await interaction(session, row_id)).processed_at == NOW
