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

from clientscrapper.collectors.base import Collector, Observation
from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.services import crontab, health
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
BUILT = {"heartbeat", "health", "backup", "restore_drill"}
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
    worker._offline_alerted_at.clear()
    yield
    worker.set_owner_notifier(None)
    worker.set_backup_sender(None)


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
    await worker.heartbeat_job(scheduler, transport=transport)  # within 5 minutes
    row = (await health.beats(session))["worker"]
    assert row.detail == {"jobs": 4}
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
    assert rows["job:health"].detail == {"ok": True, "error": None}
    assert rows["job:backup"].detail == {"ok": False, "error": "OSError"}
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
