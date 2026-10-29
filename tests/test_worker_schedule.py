"""The scheduler (06 §4, 03 §3, 04 §0, 05): the table equals the spec, the
built jobs are the ones registered with the crons the spec names, crontab is
read as crontab, and the T-08 jobs do what 06 §4 says.

``SPEC`` below is copied from the spec text, not from ``worker/main.py``: a
cadence changed in one place and not the other turns this red.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa
from apscheduler.events import JobExecutionEvent

from clientscrapper.collectors import erp_sync
from clientscrapper.collectors.base import Collector, Observation
from clientscrapper.collectors.runner import run_collector
from clientscrapper.collectors.telegram_monitor import TelegramMonitor
from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.services import (
    candidates,
    classify,
    companies,
    costs,
    crm_push,
    crontab,
    erp_feeds,
    health,
    model_jobs,
    retention,
)
from clientscrapper.worker import main as worker

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)  # a Saturday

# (id, triggers, task): 03 §3, 04 §0, 05 §B, 06 §1.6/§1.8/§4.4, 02 §4.
SPEC: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("heartbeat", ("every 1m",), "T-08"),
    ("health", ("every 5m",), "T-08"),
    ("backup", ("45 3 * * *",), "T-08"),  # BACKUP_TIME 03:45
    ("restore_drill", ("0 5 * * sun",), "T-08"),  # RESTORE_DRILL_DAY 05:00
    ("erp_sync_delta", ("15 * * * *",), "T-09"),
    ("erp_sync_full", ("30 2 * * *",), "T-09"),
    ("erp_sync_outcomes", ("0 3 * * *",), "T-10"),
    ("erp_sync_quarter", ("20 3 * * 0",), "T-10"),
    ("erp_sync_year", ("40 3 1 * *",), "T-10"),
    ("telegram_monitor", ("*/15 * * * *",), "T-14"),
    ("serp_layer", ("0 10,14 * * *",), "T-25"),
    ("serp_layer_night", ("10 2 * * *",), "T-25"),
    ("instagram_discovery", ("0 11 * * *",), "T-26"),
    ("instagram_hashtags", ("0 12 * * *",), "T-26"),
    ("olx_business", ("0 7 * * *",), "T-27"),
    ("olx_business_full", ("0 7 * * 4",), "T-27"),
    ("directories", ("0 23 * * 4",), "T-22"),
    ("directories_full", ("0 23 1-7 * *",), "T-22"),
    ("hh_vacancies", ("20 */4 * * *",), "T-23"),
    ("exhibitors", ("0 9 * * 1",), "T-32"),
    ("maps", ("0 10 * * 2",), "T-33"),
    ("maps_details", ("30 10 * * *",), "T-33"),
    ("maps_2gis", ("40 10 * * *",), "T-33"),
    ("brand_dealers", ("0 13 1-10 * *",), "T-34"),
    ("uzum_sellers", ("0 9 * * 5",), "T-37"),
    ("uzum_shops", ("0 4 15 * *",), "T-37"),
    ("tenders", ("0 4 * * 6",), "T-38"),
    ("classify", ("every 15m",), "T-15"),
    ("model_batch_submit", ("30 23 * * *",), "T-16"),
    ("model_batch_collect", ("30 0 * * *", "0,30 1-7 * * *", "0 8 * * *"), "T-16"),
    ("resolve", ("every 2m",), "T-17"),
    ("score", ("every 15m",), "T-18"),
    ("review_cards", ("every 2m",), "T-21"),
    ("enrich", ("*/10 7-21 * * *",), "T-24"),
    ("verdict", ("every 10m",), "T-30"),
    ("crm_push", ("every 1m",), "T-28"),
    ("reconcile", ("every 10m",), "T-28"),
    ("source_precision", ("0 4 * * 0",), "T-36"),
    ("digest", ("0 9 * * *",), "T-35"),  # DIGEST_TIME 09:00
    ("retention", ("15 4 * * *",), "T-31"),
)
BUILT = {
    "heartbeat",
    "health",
    "backup",
    "restore_drill",
    # T-09 / T-10: the ERP feeds, through run_collector(ErpSync, variant=…).
    "erp_sync_delta",
    "erp_sync_full",
    "erp_sync_outcomes",
    "erp_sync_quarter",
    "erp_sync_year",
    # T-17: the resolver.
    "resolve",
    # T-14: the Telegram collector, woken by NOTIFY cs_interaction too.
    "telegram_monitor",
    # T-15: the classifier, woken by the collector's NOTIFY.
    "classify",
    # T-16: the batch lane (services/model_jobs).
    "model_batch_submit",
    "model_batch_collect",
    # T-18: the scorer.
    "score",
    # T-21: the review cards (and their expiry sweep).
    "review_cards",
    # T-28: the push and its reconcile.
    "crm_push",
    "reconcile",
    # T-35: the owner's morning digest.
    "digest",
    # T-30: the instant verdicts and the openers (services/verdict).
    "verdict",
    # T-31: retention (services/retention, 02 §4).
    "retention",
    # T-36: the weekly precision job (05 §B.2).
    "source_precision",
}
# The ERP slots and the ErpSync variant each runs (03 §3, 05 §B.2).
ERP_VARIANTS = {
    "erp_sync_delta": "delta",
    "erp_sync_full": "full",
    "erp_sync_outcomes": "outcomes",
    "erp_sync_quarter": "quarter",
    "erp_sync_year": "year",
}
WAKES = {"telegram_monitor", "classify", "score"}  # NOTIFY cs_interaction (04 §0)


def test_the_table_is_the_spec():
    table = worker.job_table()
    assert [(s.id, s.triggers, s.task) for s in table] == list(SPEC)
    assert {s.id for s in table if s.wakes_on == "cs_interaction"} == WAKES


def test_only_built_jobs_are_registered_and_slots_import_nothing():
    table = worker.job_table()
    assert {s.id for s in table if s.job is not None} == BUILT
    scheduler = worker.build_scheduler()
    assert sorted(j.id for j in scheduler.get_jobs()) == sorted(BUILT)
    for job in scheduler.get_jobs():
        assert (job.max_instances, job.coalesce, job.misfire_grace_time) == (
            1,
            True,
            3600,
        )


def test_the_erp_slots_are_erp_sync_jobs_and_every_variant_is_erpsync_s():
    """One statement of the ERP cadences (collectors/erp_sync.JOBS); the table
    reads it, and every variant it schedules is one ErpSync runs."""
    table = {s.id: s for s in worker.job_table()}
    assert {j.id: (j.cron, j.variant, j.task) for j in erp_sync.JOBS} == {
        slot_id: (table[slot_id].triggers[0], variant, table[slot_id].task)
        for slot_id, variant in ERP_VARIANTS.items()
    }
    for slot_id, variant in ERP_VARIANTS.items():
        slot = table[slot_id]
        assert (slot.source_key, slot.variant) == ("erp_sync", variant)
        assert slot.variant in erp_sync.VARIANTS
        assert slot.primary == (variant == "delta")


async def test_each_erp_slot_runs_erpsync_with_its_variant(monkeypatch):
    ran: list[tuple[type, str | None]] = []

    async def record(cls, *, fetcher, now, variant=None, **kw):
        ran.append((cls, variant))

    monkeypatch.setattr(worker, "run_collector", record)
    table = {s.id: s for s in worker.job_table()}
    try:
        for slot_id in ERP_VARIANTS:
            job = table[slot_id].job
            assert job is not None
            await job()
    finally:
        await worker.shared_fetcher().aclose()
        worker._shared.fetcher = None
    assert ran == [(erp_sync.ErpSync, v) for v in ERP_VARIANTS.values()]


async def test_the_telegram_slot_runs_the_telegram_collector(monkeypatch):
    """T-14 fills the slot T-08 left: run_collector(TelegramMonitor), no
    variant, primary on sources.telegram_monitor.cron, woken by the reader's
    NOTIFY (04 §0)."""
    ran: list[tuple[type, str | None]] = []

    async def record(cls, *, fetcher, now, variant=None, **kw):
        ran.append((cls, variant))

    monkeypatch.setattr(worker, "run_collector", record)
    [slot] = [s for s in worker.job_table() if s.id == "telegram_monitor"]
    assert (slot.source_key, slot.variant, slot.primary, slot.wakes_on) == (
        "telegram_monitor",
        None,
        True,
        "cs_interaction",
    )
    assert slot.job is not None
    try:
        await slot.job()
    finally:
        await worker.shared_fetcher().aclose()
        worker._shared.fetcher = None
    assert ran == [(TelegramMonitor, None)]


def test_the_resolver_is_the_resolve_slot_s_job():
    [slot] = [s for s in worker.job_table() if s.id == "resolve"]
    assert slot.job is companies.resolve_job
    scheduler = worker.build_scheduler()
    job = scheduler.get_job("resolve")
    assert job is not None and job.func is companies.resolve_job
    assert (job.max_instances, job.coalesce) == (1, True)


def test_the_scorer_is_the_score_slot_s_job_and_wakes_on_cs_interaction():
    [slot] = [s for s in worker.job_table() if s.id == "score"]
    assert (slot.job, slot.wakes_on) == (candidates.score_job, "cs_interaction")
    scheduler = worker.build_scheduler()
    job = scheduler.get_job("score")
    assert job is not None and job.func.__name__ == "woken_score"
    # The channel's slots upstream first: each stage wakes the next (04 §0).
    assert worker.waking()["cs_interaction"] == ("telegram_monitor", "classify", "score")


def test_the_pipeline_hooks_are_registered_where_the_jobs_run():
    """The worker that runs ``classify`` and ``resolve`` also loads the signal
    writer's hooks (services/candidates, imported for the ``score`` slot):
    a classification and a resolution land as signals whichever comes second
    (04 §0; tests/test_telegram_pipeline.py drives both orders)."""
    assert candidates.after_classify in classify.AFTER_CLASSIFY
    assert candidates.after_resolve in companies.AFTER_RESOLVE
    table = {s.id: s for s in worker.job_table()}
    assert table["classify"].job is classify.classify_job
    # The NOTIFY chain of 04 §0, upstream first on the one channel.
    assert [s.id for s in worker.job_table() if s.wakes_on] == [
        "telegram_monitor",
        "classify",
        "score",
    ]


def test_the_push_and_reconcile_slots_are_crm_push_s_jobs():
    table = {s.id: s for s in worker.job_table()}
    assert table["crm_push"].job is crm_push.crm_push_slot
    assert table["reconcile"].job is crm_push.reconcile_slot
    assert (table["crm_push"].triggers, table["reconcile"].triggers) == (
        ("every 1m",),
        ("every 10m",),
    )
    scheduler = worker.build_scheduler()
    for job_id, func in (
        ("crm_push", crm_push.crm_push_slot),
        ("reconcile", crm_push.reconcile_slot),
    ):
        job = scheduler.get_job(job_id)
        assert job is not None and job.func is func


def test_the_retention_slot_is_retention_s_job_at_04_15_tashkent():
    """T-31 fills the slot T-08 left (02 §4: daily 04:15 Tashkent)."""
    [slot] = [s for s in worker.job_table() if s.id == "retention"]
    assert (slot.job, slot.triggers, slot.wakes_on) == (
        retention.retention_job,
        ("15 4 * * *",),
        None,
    )
    job = worker.build_scheduler().get_job("retention")
    assert job is not None and job.func is retention.retention_job
    fire = job.trigger.get_next_fire_time(None, NOW)
    assert (fire.hour, fire.minute, fire.utcoffset()) == (4, 15, timedelta(hours=5))


class _Moved:
    """A scheduler stand-in: records each job moved to now."""

    def __init__(self, ids: set[str]) -> None:
        self.ids = ids
        self.moved: list[tuple[str, datetime]] = []

    def get_job(self, job_id: str):
        if job_id not in self.ids:
            return None
        moved = self.moved

        class _Job:
            def modify(self, next_run_time: datetime) -> None:
                moved.append((job_id, next_run_time))

        return _Job()


async def test_a_notify_on_cs_interaction_runs_score_at_once(session):
    """01 N-06's event path (04 §0): the userbot's or the resolver's
    ``NOTIFY cs_interaction`` moves score_job's next run to now — the card
    does not wait for the 15-minute sweep."""
    scheduler = _Moved({"score"})
    stop = asyncio.Event()
    connected = asyncio.Event()
    listener = asyncio.create_task(
        worker.listen_for_wakes(scheduler, stop, connected=connected.set)
    )
    try:
        await asyncio.wait_for(connected.wait(), 10)
        # The connect wake (what was said while nobody listened) moved it once.
        assert [job_id for job_id, _ in scheduler.moved] == ["score"]
        scheduler.moved.clear()
        worker._wakes.pending.clear()
        before = datetime.now(TZ)
        await session.execute(sa.text("SELECT pg_notify('cs_interaction', '42')"))
        await session.commit()
        for _ in range(100):
            if scheduler.moved:
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(listener, timeout=worker.WAKE_POLL_SECONDS + 5)
    [(job_id, at)] = scheduler.moved
    assert job_id == "score"
    assert before - timedelta(seconds=1) <= at <= datetime.now(TZ)
    worker._wakes.pending.clear()


async def test_a_wake_while_the_job_runs_makes_it_run_once_more():
    runs: list[int] = []
    scheduler = _Moved({"score"})

    async def job() -> None:
        runs.append(len(runs))
        if len(runs) == 1:
            # a notification arrives in the middle of the first run
            assert worker.wake(scheduler, ("score",), now=NOW) == ["score"]

    await worker.wakeable("score", job)()
    assert runs == [0, 1]
    assert scheduler.moved == []  # a running job is not moved, it goes round again
    assert worker._wakes.pending == set() and worker._wakes.running == set()
    assert worker.wake(scheduler, ("score",), now=NOW) == ["score"]
    assert scheduler.moved == [("score", NOW)]
    worker._wakes.pending.clear()


def test_the_batch_lane_slots_run_model_jobs():
    """04 §0: model_batch_submit 23:30, model_batch_collect every 30 min
    00:30–08:00 — the two jobs of services/model_jobs (T-16)."""
    table = {s.id: s for s in worker.job_table()}
    assert table["model_batch_submit"].job is model_jobs.model_batch_submit_job
    assert table["model_batch_collect"].job is model_jobs.model_batch_collect_job
    scheduler = worker.build_scheduler()
    for slot_id, func in (
        ("model_batch_submit", model_jobs.model_batch_submit_job),
        ("model_batch_collect", model_jobs.model_batch_collect_job),
    ):
        job = scheduler.get_job(slot_id)
        assert job is not None and job.func is func
    [submit] = _next("model_batch_submit", NOW, 1)
    assert submit == datetime(2026, 10, 24, 23, 30, tzinfo=TZ)


def test_wire_registers_the_suppression_probe():
    """suppression_stale is unjudged until the feed sync's probe is
    registered (06 §4.2); the worker registers it at start (T-09)."""
    health.set_suppression_probe(None)
    worker.wire()
    assert health._probes.suppression is erp_feeds.suppression_probe


def test_every_trigger_of_the_table_parses():
    for slot in worker.job_table():
        assert worker.build_trigger(slot.triggers, TZ) is not None, slot.id


def _next(slot_id: str, after: datetime, n: int = 2) -> list[datetime]:
    [slot] = [s for s in worker.job_table() if s.id == slot_id]
    trigger = worker.build_trigger(slot.triggers, TZ)
    fires: list[datetime] = []
    previous = None
    moment = after
    for _ in range(n):
        nxt = trigger.get_next_fire_time(previous, moment)
        fires.append(nxt)
        previous, moment = nxt, nxt + timedelta(seconds=1)
    return fires


def test_the_registered_crons_fire_when_the_spec_says():
    assert _next("backup", NOW) == [
        datetime(2026, 10, 25, 3, 45, tzinfo=TZ),
        datetime(2026, 10, 26, 3, 45, tzinfo=TZ),
    ]
    sunday = datetime(2026, 10, 25, 5, 0, tzinfo=TZ)
    assert _next("restore_drill", NOW) == [sunday, sunday + timedelta(days=7)]
    gaps = _next("health", NOW, 3)
    assert gaps[1] - gaps[0] == gaps[2] - gaps[1] == timedelta(minutes=5)
    beats = _next("heartbeat", NOW, 2)
    assert beats[1] - beats[0] == timedelta(minutes=1)


def test_weekdays_are_crontab_s_not_apscheduler_s():
    # 05 §B.2: the quarter walk is «Sunday 20 3 * * 0»; exhibitors run Monday.
    [quarter] = _next("erp_sync_quarter", NOW, 1)
    assert (quarter.strftime("%a"), quarter.hour, quarter.minute) == ("Sun", 3, 20)
    [exhibitors] = _next("exhibitors", NOW, 1)
    assert exhibitors.strftime("%a %H:%M") == "Mon 09:00"
    [olx_full] = _next("olx_business_full", NOW, 1)
    assert olx_full.strftime("%a") == "Thu"
    [tenders] = _next("tenders", NOW + timedelta(days=1), 1)
    assert tenders.strftime("%a") == "Sat"
    # The batch collect fires every 30 min from 00:30 to 08:00 and not at 00:00.
    night = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
    fires = _next("model_batch_collect", night - timedelta(minutes=1), 17)
    # 00:30, fourteen half-hours 01:00…07:30, 08:00 — then the next night.
    assert fires[0] == datetime(2026, 10, 25, 0, 30, tzinfo=TZ)
    assert fires[15] == datetime(2026, 10, 25, 8, 0, tzinfo=TZ)
    assert fires[16] == datetime(2026, 10, 26, 0, 30, tzinfo=TZ)
    assert {b - a for a, b in itertools.pairwise(fires[:16])} == {timedelta(minutes=30)}


def test_the_crontab_translator_refuses_what_it_cannot_say_exactly():
    for bad in ("0 7 * *", "0 7 1 * 1", "0 7 * * 8", "0 7 * * fun", "every 0m"):
        with pytest.raises(crontab.CrontabError):
            worker.build_trigger((bad,), TZ)
    assert crontab.day_of_week("0,7") == "sun"
    assert crontab.day_of_week("1-5/2") == "mon,wed,fri"


def test_the_settings_move_the_backup_drill_and_digest(monkeypatch):
    monkeypatch.setattr(settings, "backup_time", "02:50")
    monkeypatch.setattr(settings, "restore_drill_day", "wed")
    monkeypatch.setattr(settings, "digest_time", "09:30")
    table = {s.id: s.triggers for s in worker.job_table()}
    assert table["backup"] == ("50 2 * * *",)
    assert table["restore_drill"] == ("0 5 * * wed",)
    assert table["digest"] == ("30 9 * * *",)


async def test_a_primary_collector_cron_is_the_sources_row(session):
    """The table carries the seeded sources.cron; the owner may change it from
    /manbalar and the scheduler then follows the row (03 §3)."""
    seeded = dict((await session.execute(sa.select(m.Source.key, m.Source.cron))).all())
    for slot in worker.job_table():
        if slot.primary:
            assert slot.source_key is not None, slot.id
            assert seeded[slot.source_key] == slot.triggers[0], slot.id
    built = tuple(s if s.id != "exhibitors" else _with_job(s) for s in worker.job_table())
    scheduler = worker.build_scheduler(built, source_crons={"exhibitors": "30 8 * * 2"})
    job = scheduler.get_job("exhibitors")
    first = job.trigger.get_next_fire_time(None, NOW)
    assert first.strftime("%a %H:%M") == "Tue 08:30"


def _with_job(slot: worker.JobSlot) -> worker.JobSlot:
    async def job() -> None:
        return None

    from dataclasses import replace

    return replace(slot, job=job)


# --- the jobs ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(worker._deadman, "last", None)
    monkeypatch.setattr(worker._deadman, "seen", None)
    worker._offline_alerted_at.clear()
    yield
    worker.set_owner_notifier(None)
    worker.set_backup_sender(None)
    # worker.wire() (backup_now) registers process-wide hooks.
    costs.set_anthropic_health(None)
    costs.set_cap_notifier(None)
    health.set_suppression_probe(None)


class Notify:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return True


async def test_heartbeat_beats_the_worker_and_pings_the_dead_man(session, monkeypatch):
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    transport = httpx.MockTransport(answer)
    monkeypatch.setattr(settings, "deadman_ping_url", "https://hc.example.uz/ping/namuna")
    scheduler = worker.build_scheduler()
    await worker.heartbeat_job(scheduler, transport=transport)
    row = (await health.beats(session))["worker"]
    # The first beat goes out before any ping: nothing to say about it yet.
    assert row.detail == {"jobs": len(BUILT)}
    await worker.heartbeat_job(scheduler, transport=transport)  # within 5 minutes
    row = (await health.beats(session))["worker"]
    # The ping the monitor observed is carried in the next beat (the digest
    # and /holat read it there).
    assert row.detail["jobs"] == len(BUILT)
    assert row.detail["deadman"]["ok"] is True
    assert row.detail["deadman"]["status"] == 200
    assert seen == ["https://hc.example.uz/ping/namuna"]
    monkeypatch.setattr(worker._deadman, "last", worker._deadman.last - 301)
    await worker.heartbeat_job(scheduler, transport=transport)
    assert len(seen) == 2


async def test_no_dead_man_url_means_no_request(monkeypatch):
    monkeypatch.setattr(settings, "deadman_ping_url", "")
    assert await worker.deadman_ping(transport=httpx.MockTransport(_refuse)) is False


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no request may be made")


async def test_a_dead_man_that_does_not_answer_is_not_an_error(monkeypatch):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(settings, "deadman_ping_url", "https://hc.example.uz/ping/x")
    assert await worker.deadman_ping(transport=httpx.MockTransport(down)) is False


async def test_a_job_outcome_is_its_job_heartbeat(session):
    loop = asyncio.get_running_loop()
    ok = JobExecutionEvent(4096, "health", "default", NOW)
    failed = JobExecutionEvent(8192, "backup", "default", NOW, exception=OSError("x"))
    for event in (ok, failed):
        future = await asyncio.to_thread(worker.on_job_event, event, loop=loop)
        await asyncio.wrap_future(future)
    rows = await health.beats(session)
    assert rows["job:health"].detail == {"ok": True, "error": None, "failures": 0}
    # A raise carries the job's period: the nightly backup's is a day, so
    # health says it on this first raise (health.job_failing_streak).
    assert rows["job:backup"].detail == {
        "ok": False,
        "error": "OSError",
        "failures": 1,
        "every": 86400,
    }
    await health.beat(session, "job:retired_job", now=NOW)
    await session.commit()
    assert await worker.prune_job_heartbeats(["job:health", "job:backup"]) == 1
    assert set(await health.beats(session)) == {"job:health", "job:backup"}


async def test_health_job_tells_the_owner_through_the_seam_only(session, monkeypatch):
    notify = Notify()
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    report = await worker.health_job(now=NOW, notify=notify)
    assert report is not None
    # Nothing ever beat: the worker's own silence is critical and said alone;
    # the warnings are one message.
    assert report.said == ("worker_silent", "userbot_silent", "backup_unconfigured")
    assert len(notify.sent) == 2
    assert notify.sent[0].startswith("⛔ Rejalashtiruvchi")
    again = await worker.health_job(now=NOW + timedelta(minutes=5), notify=notify)
    assert again is not None and again.said == () and len(notify.sent) == 2


async def test_health_job_without_a_wired_notifier_records_nothing(session):
    report = await worker.health_job(now=NOW)
    assert report is not None and report.said == ()
    count = await session.scalar(sa.select(sa.func.count()).select_from(m.ReminderLog))
    assert count == 0


async def test_health_job_with_the_database_down_says_the_critical_once(monkeypatch):
    notify = Notify()

    async def unreachable(session, *, now, **kw):
        return health.Status.unreachable(now, (100 * 1000**3, 200 * 1000**3))

    monkeypatch.setattr(health, "gather", unreachable)
    assert await worker.health_job(now=NOW, notify=notify) is None
    assert notify.sent == [
        "⛔ Baza javob bermayapti — <code>make ps</code>, <code>make logs</code>"
    ]
    await worker.health_job(now=NOW + timedelta(minutes=5), notify=notify)
    assert len(notify.sent) == 1


async def test_a_job_s_beat_counts_the_raises_in_a_row(session):
    for _ in range(3):
        await worker.record_job_event("resolve", ok=False, error="ProgrammingError")
    beat = (await health.beats(session))["job:resolve"]
    assert beat.detail == {"ok": False, "error": "ProgrammingError", "failures": 3}
    await worker.record_job_event("resolve", ok=True, error=None)
    beat = (await health.beats(session))["job:resolve"]
    assert beat.detail == {"ok": True, "error": None, "failures": 0}
    await worker.record_job_event("resolve", ok=False, error="OSError")
    beat = (await health.beats(session))["job:resolve"]
    assert beat.detail["failures"] == 1


# Wave 4's jobs (T-21, T-30, T-35, T-36, T-31): each is a built slot whose
# runs beat job:<id>, so 06 §4.2's job_failing names it by its own subject
# and the digest's «Tizim» line counts it — the same as the jobs before them.
WAVE4_JOBS = ("review_cards", "verdict", "source_precision", "digest", "retention")


def test_wave_4_s_jobs_are_built_slots_of_their_tasks():
    table = {s.id: s for s in worker.job_table()}
    assert {job: table[job].task for job in WAVE4_JOBS} == {
        "review_cards": "T-21",
        "verdict": "T-30",
        "source_precision": "T-36",
        "digest": "T-35",
        "retention": "T-31",
    }
    assert all(table[job].job is not None for job in WAVE4_JOBS)
    registered = {j.id for j in worker.build_scheduler().get_jobs()}
    assert set(WAVE4_JOBS) <= registered


@pytest.mark.parametrize("job_id", WAVE4_JOBS)
async def test_a_wave_4_job_that_raises_every_run_is_its_own_job_failing_line(
    session, monkeypatch, job_id
):
    from clientscrapper.services import digest

    monkeypatch.setattr(settings, "backup_age_recipient", "")
    await _fresh(session)
    for _ in range(health.JOB_FAILING_STREAK):
        await worker.record_job_event(job_id, ok=False, error="RuntimeError")
    status = await health.gather(session, now=NOW)
    [failing] = [p for p in health.problems(status) if p.key == "job_failing"]
    assert [subject for subject, _ in failing.subjects] == [health.job_subject(job_id)]
    assert f"<code>{job_id}</code> ishi oxirgi 3 marta xato bilan tugadi" in (
        failing.text
    )
    view = (await digest.load(session, now=NOW)).view
    assert view.problems >= 1
    assert "Tizim: hammasi joyida" not in digest.render_digest(view)


async def _fresh(session, now=NOW):
    for name in health.COMPONENTS:
        await health.beat(session, name, detail={"enabled": True}, now=now)
    await session.commit()


async def test_a_resolver_that_raises_every_run_is_told(session, monkeypatch):
    notify = Notify()
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    await _fresh(session)
    for _ in range(3):
        await worker.record_job_event("resolve", ok=False, error="ProgrammingError")
    report = await worker.health_job(now=NOW, notify=notify)
    assert report is not None and "job_failing" in report.said
    assert any(
        "<code>resolve</code> ishi oxirgi 3 marta xato bilan tugadi "
        "(ProgrammingError)" in text
        for text in notify.sent
    )
    await worker.record_job_event("resolve", ok=True, error=None)
    await _fresh(session, NOW + timedelta(minutes=5))
    back = await worker.health_job(now=NOW + timedelta(minutes=5), notify=notify)
    assert back is not None and back.recovered == ("job_failing",)
    assert notify.sent[-1] == health.recovery_text("job_failing")


async def test_a_collector_that_fails_every_run_is_told(session, monkeypatch):
    class Broken(Collector):
        key = "hh_vacancies"
        family = "vacancy"

        async def collect(self, session, budget) -> list[Observation]:
            raise KeyError("items")

    notify = Notify()
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    monkeypatch.setattr(settings, "collectors_enabled", True)
    source = await session.get(m.Source, "hh_vacancies")
    assert source is not None
    # The run moves last_run_at too: put back with the rest (07 §1.0).
    was = (source.enabled, source.cron, source.last_run_at)
    source.enabled, source.cron = True, "20 */4 * * *"
    await session.commit()
    try:
        runs = [NOW - timedelta(hours=h) for h in (8, 4, 0)]
        for at in runs:
            report = await run_collector(
                Broken,
                fetcher=worker.shared_fetcher(),
                now=at,
                cost_gate_for=None,
            )
            assert (report.status, report.error) == ("failed", "KeyError")
        await _fresh(session)
        told = await worker.health_job(now=NOW + timedelta(minutes=1), notify=notify)
        assert told is not None and "job_failing" in told.said
        assert any(
            "oxirgi 3 ta ishga tushirish xato bilan tugadi (KeyError)" in text
            for text in notify.sent
        )
    finally:
        source = await session.get(m.Source, "hh_vacancies", populate_existing=True)
        assert source is not None
        source.enabled, source.cron, source.last_run_at = was
        await session.commit()
        if worker._shared.fetcher is not None:
            await worker._shared.fetcher.aclose()
            worker._shared.fetcher = None


async def test_the_flood_breaker_is_said_by_the_sweep_and_its_end_too(
    session, monkeypatch
):
    """06 §4.3: the reader writes runtime_flags.flood_until and says nothing;
    the worker's sweep is what tells the owner, and tells the end."""
    from clientscrapper.userbot import limits

    notify = Notify()
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    await _fresh(session)
    until = await limits.trip_breaker(session, seconds=900, now=NOW, where="join")
    await session.commit()
    assert until is not None
    report = await worker.health_job(now=NOW, notify=notify)
    assert report is not None and "flood_breaker" in report.said
    assert any("⚠️ Telegram FloodWait (900 s)" in text for text in notify.sent)
    after = until + timedelta(minutes=1)
    if health.in_quiet_hours(after):  # the end is said outside the quiet hours
        after = after.replace(hour=12, minute=0)
    await _fresh(session, after)
    back = await worker.health_job(now=after, notify=notify)
    assert back is not None and "flood_breaker" in back.recovered
    assert health.recovery_text("flood_breaker") in notify.sent[-1]


async def test_the_database_back_is_said_once_it_answers(session, monkeypatch):
    notify = Notify()
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    real = health.gather

    async def unreachable(session, *, now, **kw):
        return health.Status.unreachable(now, (100 * 1000**3, 200 * 1000**3))

    monkeypatch.setattr(health, "gather", unreachable)
    assert await worker.health_job(now=NOW, notify=notify) is None
    assert notify.sent == [
        "⛔ Baza javob bermayapti — <code>make ps</code>, <code>make logs</code>"
    ]
    monkeypatch.setattr(health, "gather", real)
    later = NOW + timedelta(minutes=10)
    await _fresh(session, later)
    report = await worker.health_job(now=later, notify=notify)
    assert report is not None and report.recovered == ("db_down",)
    assert notify.sent[-1] == "✅ Baza qayta javob beryapti — tiklandi"
    assert worker._offline_alerted_at == {}
    # The ledger keeps the moment it was said, not the moment it was written.
    rows = await session.execute(
        sa.select(m.ReminderLog.kind, m.ReminderLog.sent_at).order_by(m.ReminderLog.id)
    )
    assert [(k, at) for k, at in rows.all() if k.endswith(":db_down")] == [
        ("alert:db_down", NOW),
        ("recovered:db_down", later),
    ]


async def test_backup_job_without_a_recipient_writes_and_beats_nothing(
    session, monkeypatch
):
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    result = await worker.backup_job(now=NOW)
    assert result.error == "no_recipient"
    assert "backup" not in await health.beats(session)


async def test_backup_job_beats_its_outcome_and_the_counts_for_the_drill(
    session, monkeypatch, tmp_path
):
    import shutil
    import subprocess

    if shutil.which("age-keygen") is None or shutil.which("pg_dump") is None:
        from tests.conftest import require_db

        if require_db():
            pytest.fail("CS_REQUIRE_DB=1 but age/pg_dump are not on PATH")
        pytest.skip("age/pg_dump not on PATH")
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True
    ).stdout.strip()
    monkeypatch.setattr(settings, "backup_age_recipient", recipient)
    sent: list[tuple[str, str]] = []

    async def send(path: Path, caption: str) -> bool:
        sent.append((path.name, caption))
        return True

    worker.set_backup_sender(send)
    when = datetime(2026, 10, 24, 3, 45, tzinfo=TZ)
    result = await worker.backup_job(now=when)
    assert result.ok and result.path is not None
    rows = await health.beats(session)
    detail = rows["backup"].detail
    assert detail["ok"] is True and detail["sent"] is True
    assert detail["path"] == str(result.path) and detail["size"] == result.size > 0
    assert rows["backup_counts"].detail == {
        "path": str(result.path),
        "counts": {
            "companies": 0,
            "contacts": 0,
            "lead_candidates": 0,
            "push_log": 0,
            "suppression": 0,
            "outcomes": 0,
        },
    }
    assert sent == [(result.path.name, sent[0][1])]
    assert sent[0][1].startswith("🗄 Zaxira 24.10 03:45 · ")
    assert sent[0][1].endswith(" · 1/1")
    # The file is today's: no catch-up backup at 10:00.
    assert worker.backup_is_missing(when + timedelta(hours=6)) is False
    assert worker.backup_is_missing(when + timedelta(days=1, minutes=1)) is True

    worker.set_backup_sender(None)
    later = when + timedelta(days=1)
    await worker.backup_job(now=later)
    detail = (await health.beats(session))["backup"].detail
    assert (detail["sent"], detail["send_error"]) == (False, "not_wired")


async def test_the_drill_job_beats_its_outcome(session, monkeypatch):
    from clientscrapper.services import restore_drill

    async def fake_run(**kw):
        assert kw == {"expected": {"companies": 2}}
        return restore_drill.DrillResult(ok=False, error="counts differ: companies 2≠0")

    await health.beat(
        session,
        "backup_counts",
        detail={"path": str(Path(settings.backup_dir) / "x.dump.age"), "counts": {}},
        now=NOW,
    )
    folder = Path(settings.backup_dir)
    folder.mkdir(parents=True)
    newest = folder / "clientscrapper-20261024-034500.dump.age"
    newest.write_bytes(b"age")
    await health.beat(
        session,
        "backup_counts",
        detail={"path": str(newest), "counts": {"companies": 2}},
        now=NOW + timedelta(seconds=1),
    )
    await session.commit()
    monkeypatch.setattr(restore_drill, "run", fake_run)
    result = await worker.restore_drill_job()
    assert result is not None and not result.ok
    status = await health.gather(session, now=NOW, disk=(10**12, 10**12))
    assert "restore_drill_failed" in [p.key for p in health.problems(status)]
    monkeypatch.setattr(settings, "restore_drill_enabled", False)
    assert await worker.restore_drill_job() is None


async def test_catch_up_runs_a_missed_backup(monkeypatch):
    calls: list[datetime] = []

    async def fake_backup(*, now):
        calls.append(now)

    monkeypatch.setattr(worker, "backup_job", fake_backup)
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    await worker.catch_up(now=NOW)
    assert calls == []  # nothing is backed up without a recipient
    monkeypatch.setattr(settings, "backup_age_recipient", "age1namuna")
    await worker.catch_up(now=NOW)
    assert calls == [NOW]


async def test_the_drill_beat_carries_its_last_proof_across_skips(session, monkeypatch):
    """Seam review wave 4: a drill skipped for want of the key beat ok=True
    every Sunday and health never looked past the last word — the proof's
    moment rides on every beat now, and the no-key skip is said."""
    from clientscrapper.services import restore_drill

    results = iter(
        [
            restore_drill.DrillResult(ok=True, counts={"companies": 2}),
            restore_drill.DrillResult(ok=True, skipped="no_key"),
        ]
    )

    async def fake_run(**kw):
        return next(results)

    monkeypatch.setattr(restore_drill, "run", fake_run)
    await worker.restore_drill_job(now=NOW)
    week = NOW + timedelta(days=7)
    await worker.restore_drill_job(now=week)
    detail = (await health.beats(session))["restore_drill"].detail
    assert (detail["skipped"], detail["proved_at"]) == ("no_key", NOW.isoformat())
    status = await health.gather(session, now=week, disk=(10**12, 10**12))
    [problem] = [p for p in health.problems(status) if p.key == "restore_drill_failed"]
    assert problem.text == health.DRILL_NO_KEY_UZ


async def test_catch_up_hands_an_overdue_drill_to_its_scheduler_job(session, monkeypatch):
    """A restart across Sunday 05:00 is past the misfire grace: backups
    unproved for over DRILL_STALE get the drill at the next start."""
    monkeypatch.setattr(settings, "backup_age_recipient", "age1namuna")
    scheduler, _ = _scheduler_with("restore_drill")
    scheduler.start(paused=True)
    moment = datetime.now(TZ)
    try:
        for proved, moved in ((timedelta(days=2), False), (timedelta(days=9), True)):
            await health.beat(
                session,
                "restore_drill",
                detail={
                    "ok": True,
                    "skipped": None,
                    "proved_at": (moment - proved).isoformat(),
                },
                now=moment,
            )
            await session.commit()
            job = scheduler.get_job("restore_drill")
            assert job is not None
            job.modify(next_run_time=moment + timedelta(days=3))
            await worker.catch_up(now=moment, scheduler=scheduler)
            job = scheduler.get_job("restore_drill")
            assert job is not None
            assert (job.next_run_time <= moment + timedelta(seconds=1)) is moved
    finally:
        scheduler.shutdown(wait=False)


async def test_a_busy_drill_writes_no_beat(session, monkeypatch):
    from clientscrapper.services import restore_drill

    async def busy(**kw):
        return restore_drill.DrillResult(ok=True, skipped="busy")

    monkeypatch.setattr(restore_drill, "run", busy)
    result = await worker.restore_drill_job()
    assert result is not None and result.skipped == "busy"
    assert "restore_drill" not in await health.beats(session)


def _scheduler_with(*job_ids: str) -> tuple[object, list[str]]:
    """A scheduler (started, paused) holding stand-ins for ``job_ids``."""
    ran: list[str] = []
    scheduler = worker.AsyncIOScheduler(timezone=TZ)
    for job_id in job_ids:

        async def job(job_id: str = job_id) -> None:
            ran.append(job_id)

        scheduler.add_job(job, "cron", hour=3, minute=45, id=job_id, max_instances=1)
    return scheduler, ran


async def test_catch_up_hands_the_backup_to_its_scheduler_job(monkeypatch):
    """The backup goes through the scheduler's own job, so max_instances=1
    keeps it from running beside the 03:45 cron."""
    called: list[datetime] = []

    async def direct(*, now):
        called.append(now)

    monkeypatch.setattr(worker, "backup_job", direct)
    monkeypatch.setattr(settings, "backup_age_recipient", "age1namuna")
    scheduler, ran = _scheduler_with("backup")
    scheduler.start(paused=True)
    try:
        moment = datetime.now(TZ)
        await worker.catch_up(now=moment, scheduler=scheduler)
        assert called == []
        job = scheduler.get_job("backup")
        assert job is not None and job.next_run_time <= moment + timedelta(seconds=1)
        scheduler.resume()
        await asyncio.sleep(0.2)
        assert ran == ["backup"]
    finally:
        scheduler.shutdown(wait=False)


async def test_catch_up_runs_the_nightly_outcomes_walks_it_missed(session, monkeypatch):
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    monkeypatch.setattr(settings, "crm_push_source", "agent")
    monkeypatch.setattr(settings, "crm_agent_key", "namuna-agent-key")
    scheduler, ran = _scheduler_with(worker.OUTCOMES_JOB)
    # sources is seeded configuration (tests/conftest.py KEPT_TABLES): what
    # this test changes it puts back as it found it.
    row = await session.get(m.Source, "erp_sync")
    assert row is not None
    seeded = dict(row.config)
    scheduler.start(paused=True)
    try:
        moment = datetime.now(TZ)
        await worker.catch_up(now=moment, scheduler=scheduler)  # never walked
        job = scheduler.get_job(worker.OUTCOMES_JOB)
        assert job is not None and job.next_run_time <= moment + timedelta(seconds=1)
        # Walked last night: nothing to catch up.
        done = erp_feeds.OutcomesConfig(
            last_cursor_at=moment - timedelta(hours=9),
            last_recent_at=moment - timedelta(hours=9),
        )
        await erp_feeds._store_outcomes_config(session, done)
        await session.commit()
        job.modify(next_run_time=moment + timedelta(days=1))
        await worker.catch_up(now=moment, scheduler=scheduler)
        job = scheduler.get_job(worker.OUTCOMES_JOB)
        assert job is not None and job.next_run_time > moment + timedelta(hours=1)
        # The feeds off (the «Boshqa» fallback): never.
        monkeypatch.setattr(settings, "crm_push_source", "other")
        await erp_feeds._store_outcomes_config(session, erp_feeds.OutcomesConfig())
        await session.commit()
        await worker.catch_up(now=moment, scheduler=scheduler)
        job = scheduler.get_job(worker.OUTCOMES_JOB)
        assert job is not None and job.next_run_time > moment + timedelta(hours=1)
    finally:
        scheduler.shutdown(wait=False)
        await session.execute(
            sa.update(m.Source).where(m.Source.key == "erp_sync").values(config=seeded)
        )
        await session.commit()


# --- one scheduler at a time ------------------------------------------------------------


async def test_only_one_worker_holds_the_scheduler_lock(session):
    first, second = worker.Leadership(), worker.Leadership()
    try:
        assert await first.acquire() is True
        assert await first.acquire() is True  # held: still ours
        assert await second.acquire() is False
        await first.release()
        assert await first.held() is False
        assert await second.acquire() is True
        assert await first.acquire() is False
    finally:
        await first.release()
        await second.release()


async def test_a_standby_waits_and_takes_over_when_the_leader_goes(session, monkeypatch):
    monkeypatch.setattr(worker, "LEADER_RETRY", timedelta(milliseconds=50))
    leader, standby = worker.Leadership(), worker.Leadership()
    stop = asyncio.Event()
    try:
        assert await leader.acquire()
        waiting = asyncio.ensure_future(worker.wait_for_leadership(standby, stop))
        await asyncio.sleep(0.2)
        assert not waiting.done()  # stands by while the leader lives
        await leader.release()  # the leader's process is gone
        assert await asyncio.wait_for(waiting, 5) is True
        # Stopped while standing by: False, and nothing scheduled.
        other = worker.Leadership()
        stop.set()
        assert await worker.wait_for_leadership(other, stop) is False
    finally:
        await leader.release()
        await standby.release()


async def test_a_worker_that_loses_the_lock_pauses_its_scheduler(session, monkeypatch):
    monkeypatch.setattr(worker, "LEADER_CHECK", timedelta(milliseconds=50))
    monkeypatch.setattr(worker, "LEADER_RETRY", timedelta(milliseconds=50))

    class Scheduler:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def pause(self) -> None:
            self.calls.append("pause")

        def resume(self) -> None:
            self.calls.append("resume")

    mine, theirs = worker.Leadership(), worker.Leadership()
    scheduler = Scheduler()
    stop = asyncio.Event()
    try:
        assert await mine.acquire()
        keeping = asyncio.ensure_future(worker.keep_leadership(mine, scheduler, stop))
        await asyncio.sleep(0.2)
        assert scheduler.calls == []  # still ours: nothing changes
        stop.set()
        await asyncio.wait_for(keeping, 5)
        # A database restart dropped this connection and the other worker took
        # the lock before this one asked again (no loop runs between the two).
        await mine.release()
        assert await theirs.acquire()
        stop.clear()
        keeping = asyncio.ensure_future(worker.keep_leadership(mine, scheduler, stop))
        await asyncio.sleep(0.3)
        assert scheduler.calls == ["pause"]
        await theirs.release()
        await asyncio.sleep(0.3)
        assert scheduler.calls == ["pause", "resume"]
        stop.set()
        await asyncio.wait_for(keeping, 5)
    finally:
        stop.set()
        await mine.release()
        await theirs.release()


class _Paused:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")


async def test_a_leader_that_takes_the_lock_back_does_a_new_leaders_work(
    session, monkeypatch
):
    """Seam review wave 4: A paused for B (a database blip), B is killed.
    A's resume closes B's ``running`` runs (they held the source's slot
    ~2 h 30, every ERP cadence ``already_running``) before the scheduler
    runs, and hands B's missed jobs to the scheduler (``catch_up``)."""
    monkeypatch.setattr(worker, "LEADER_CHECK", timedelta(milliseconds=50))
    monkeypatch.setattr(worker, "LEADER_RETRY", timedelta(milliseconds=50))
    order: list[str] = []
    real_reclaim = worker.reclaim_runs

    async def reclaim(*args, **kw):
        order.append("reclaim")
        return await real_reclaim(*args, **kw)

    async def catch_up(**kw):
        order.append("catch_up")

    monkeypatch.setattr(worker, "reclaim_runs", reclaim)
    monkeypatch.setattr(worker, "catch_up", catch_up)
    scheduler = _Paused()
    real_resume = scheduler.resume
    scheduler.resume = lambda: (order.append("resume"), real_resume())[1]  # type: ignore[method-assign]
    mine, theirs = worker.Leadership(), worker.Leadership()
    stop = asyncio.Event()
    try:
        assert await theirs.acquire()
        keeping = asyncio.ensure_future(worker.keep_leadership(mine, scheduler, stop))
        await asyncio.sleep(0.3)
        assert scheduler.calls == ["pause"]
        session.add(
            m.CollectorRun(
                source_key="erp_sync",
                status="running",
                started_at=NOW,
                meta={"variant": "delta"},
            )
        )
        await session.commit()
        await theirs.release()  # B is gone, its run still «running»
        await asyncio.sleep(0.4)
        assert order == ["reclaim", "resume", "catch_up"]
        [run] = await session.scalars(
            sa.select(m.CollectorRun).execution_options(populate_existing=True)
        )
        assert (run.status, run.error) == ("failed", "abandoned")
    finally:
        stop.set()
        await asyncio.wait_for(keeping, 5)
        await mine.release()
        await theirs.release()


async def test_a_paused_worker_does_not_resume_on_a_failed_lock_check(
    session, monkeypatch
):
    """One connect error while standing by is no proof the other worker is
    gone (the «cannot be asked → run» rule is the process start's): the
    paused worker asks again, and stays paused while the other holds it."""
    monkeypatch.setattr(worker, "LEADER_CHECK", timedelta(milliseconds=50))
    monkeypatch.setattr(worker, "LEADER_RETRY", timedelta(milliseconds=50))

    async def nothing(**kw):
        return None

    monkeypatch.setattr(worker, "catch_up", nothing)
    mine, theirs = worker.Leadership(), worker.Leadership()
    scheduler = _Paused()
    stop = asyncio.Event()
    real_acquire = worker.Leadership.acquire
    blips = {"left": 0}

    async def acquire(self):
        if self is mine and blips["left"]:
            blips["left"] -= 1
            raise OSError("connection refused")
        return await real_acquire(self)

    monkeypatch.setattr(worker.Leadership, "acquire", acquire)
    try:
        assert await theirs.acquire()
        keeping = asyncio.ensure_future(worker.keep_leadership(mine, scheduler, stop))
        await asyncio.sleep(0.3)
        assert scheduler.calls == ["pause"]
        blips["left"] = 1
        await asyncio.sleep(0.4)
        assert blips["left"] == 0  # the blip was met
        assert scheduler.calls == ["pause"]  # and nothing resumed
        assert await theirs.held()
    finally:
        stop.set()
        await asyncio.wait_for(keeping, 5)
        await mine.release()
        await theirs.release()


async def test_an_app_connection_waits_on_a_lock_at_most_lock_timeout(session):
    """A job blocked on a row lock raises after LOCK_TIMEOUT instead of
    hanging while max_instances=1 refuses every later run (seam review wave
    4); the skips meanwhile are health's (job_failing_streak)."""
    from clientscrapper.db import session as db_session

    assert db_session.LOCK_TIMEOUT == "300s"
    assert await session.scalar(sa.text("SHOW lock_timeout")) == "5min"


async def test_the_new_leader_closes_a_dead_workers_runs_only(session):
    """An OOM-killed worker's 'running' erp_sync row held the source's one
    running slot for 2 h 30 min: every ERP cadence was skipped
    already_running and suppression went stale. The next leader closes it;
    a run the reader makes (telegram_monitor.backfill) is not its to close."""
    started = NOW - timedelta(minutes=10)
    for key, variant in (("erp_sync", "delta"), ("telegram_monitor", "backfill")):
        session.add(
            m.CollectorRun(
                source_key=key,
                status="running",
                started_at=started,
                meta={
                    "variant": variant,
                    "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
                },
            )
        )
    await session.commit()
    assert await worker.reclaim_runs(now=NOW) == 1
    rows = {
        r.source_key: (r.status, r.error)
        for r in await session.scalars(
            sa.select(m.CollectorRun).execution_options(populate_existing=True)
        )
    }
    assert rows == {
        "erp_sync": ("failed", "abandoned"),
        "telegram_monitor": ("running", None),
    }
    # The source's one running slot is free again (ux_collector_runs_one_running).
    session.add(m.CollectorRun(source_key="erp_sync", status="running", started_at=NOW))
    await session.commit()


async def test_a_database_that_cannot_be_asked_is_no_other_leader(monkeypatch):
    """The lock lives in the database: unreachable, nobody else can hold it,
    so the worker runs — or health_job could not say «Baza javob bermayapti»."""
    nowhere = worker.Leadership("postgresql+psycopg://none:none@127.0.0.1:1/none")
    assert await worker.wait_for_leadership(nowhere, asyncio.Event()) is True


class _Exhibitors(Collector):
    key = "exhibitors"
    family = "exhibitor"

    async def collect(self, session, budget) -> list[Observation]:
        raise AssertionError("a disabled source is never collected")


async def test_a_collector_job_goes_through_the_runner(session):
    job = worker.collector_job(_Exhibitors)
    try:
        await job()
    finally:
        await worker.shared_fetcher().aclose()
        worker._shared.fetcher = None
    run = await session.scalar(
        sa.select(m.CollectorRun).where(m.CollectorRun.source_key == "exhibitors")
    )
    assert run is not None
    assert (run.status, run.meta["note"]) == ("skipped", "source_disabled")


def test_the_owner_seam_has_no_recipient_argument():
    import inspect

    params = list(inspect.signature(worker.notify).parameters)
    assert params == ["text"]
    assert list(inspect.signature(worker._not_wired).parameters) == ["text"]


# --- review fixes -------------------------------------------------------------------


async def test_the_dead_man_is_pinged_only_after_the_beat_is_durable(monkeypatch):
    """A dead database must stop the ping too, or the outside watcher never
    notices that the worker cannot write."""
    from contextlib import asynccontextmanager

    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    @asynccontextmanager
    async def unreachable():
        raise OSError("the database is gone")
        yield  # pragma: no cover

    monkeypatch.setattr(settings, "deadman_ping_url", "https://hc.example.uz/ping/namuna")
    monkeypatch.setattr(worker, "session_scope", unreachable)
    with pytest.raises(OSError):
        await worker.heartbeat_job(None, transport=httpx.MockTransport(answer))
    assert seen == []


def _primary(source_key: str, cron: str) -> worker.JobSlot:
    async def job() -> None:
        return None

    return worker.JobSlot(
        source_key, (cron,), "T-22", job, source_key=source_key, primary=True
    )


@pytest.mark.parametrize("bad", ["0 23 1-7 * 4", "0 23 * * 4 *", "0 25 * * *"])
def test_a_sources_cron_the_translator_refuses_falls_back_to_the_seeded_one(bad):
    """An owner edit through /manbalar must not crash-loop the worker."""
    slots = (_primary("directories", "0 23 * * 4"),)
    scheduler = worker.build_scheduler(slots, source_crons={"directories": bad})
    [job] = scheduler.get_jobs()
    seeded = crontab.trigger("0 23 * * 4", TZ)
    assert str(job.trigger) == str(seeded)


def test_a_null_sources_cron_is_not_scheduled_and_an_unread_table_keeps_the_seed():
    slots = (_primary("directories", "0 23 * * 4"), _primary("exhibitors", "0 9 * * 1"))
    scheduler = worker.build_scheduler(slots, source_crons={"directories": None})
    assert [job.id for job in scheduler.get_jobs()] == ["exhibitors"]
    scheduler = worker.build_scheduler(slots, source_crons={})
    assert sorted(job.id for job in scheduler.get_jobs()) == ["directories", "exhibitors"]
    scheduler = worker.build_scheduler(slots, source_crons={"directories": "30 22 * * 3"})
    [job] = [j for j in scheduler.get_jobs() if j.id == "directories"]
    assert str(job.trigger) == str(crontab.trigger("30 22 * * 3", TZ))


def _keys(tmp_path: Path) -> str:
    import shutil
    import subprocess

    if shutil.which("age-keygen") is None or shutil.which("pg_dump") is None:
        from tests.conftest import require_db

        if require_db():
            pytest.fail("CS_REQUIRE_DB=1 but age/pg_dump are not on PATH")
        pytest.skip("age/pg_dump not on PATH")
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    return subprocess.run(
        ["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True
    ).stdout.strip()


async def test_make_backup_clears_a_failed_night(session, monkeypatch, tmp_path):
    """`make backup` is the runbook's cure for backup_failed (06 §4.6): it runs
    the nightly job, so its heartbeat clears the critical alarm at once."""
    monkeypatch.setattr(settings, "backup_age_recipient", _keys(tmp_path))
    night = datetime(2026, 10, 24, 3, 45, tzinfo=TZ)
    await health.beat(
        session, "backup", detail={"ok": False, "error": "empty_dump"}, now=night
    )
    await session.commit()
    status = await health.gather(session, now=night, disk=(10**12, 10**12))
    assert "backup_failed" in [p.key for p in health.problems(status)]
    morning = datetime(2026, 10, 24, 9, 0, tzinfo=TZ)
    assert await worker.backup_now(now=morning) == 0
    status = await health.gather(
        session, now=morning + timedelta(minutes=5), disk=(10**12, 10**12)
    )
    assert "backup_failed" not in [p.key for p in health.problems(status)]
    rows = await health.beats(session)
    assert rows["backup"].detail["ok"] is True
    assert rows["backup_counts"].detail["path"] == rows["backup"].detail["path"]
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    assert await worker.backup_now(now=morning) == 1


def test_the_worker_command_line_knows_one_command(monkeypatch):
    calls: list[str] = []

    async def fake_backup_now(*, now=None) -> int:
        calls.append("backup")
        return 1

    monkeypatch.setattr(worker, "backup_now", fake_backup_now)
    monkeypatch.setattr(worker, "setup_logging", lambda name: None)
    assert worker.main(["backup"]) == 1
    assert calls == ["backup"]
    with pytest.raises(SystemExit):
        worker.main(["restore"])
