"""Alert routing (06 §4.2–4.3, 07 §4.1 T-35): every problem key, triggered,
reaches the owner — and nobody else — through the worker's health sweep and
the owner bot's one door (``bot/deliver.owner_notifier`` → ``to_person``).

* Each key of ``PROBLEM_KEYS`` has a trigger (``test_health.TRIGGERS``, the
  status that makes exactly that problem hold); here the sweep (``worker.
  health_job``) judges it and the fake Bot API records one ``SendMessage``
  to the owner carrying the key's line.
* ``TOLD_FIRST_ELSEWHERE`` (``push_404``, ``erp_redirect``, ``cost_cap``)
  is said first by its owning job — and when that job could not deliver it
  (no notifier in its process, Telegram down), by the next sweep: otherwise
  nobody ever would.
* A sweep that keeps crashing cannot report itself: its ``job:health``
  heartbeat reaches ``job_failing`` through the bot's watchdog, from the
  heartbeats alone when the shared ``gather`` is what raises.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from clientscrapper.bot import deliver
from clientscrapper.bot import main as bot_main
from clientscrapper.config import settings
from clientscrapper.db.models import ReminderLog
from clientscrapper.services import costs, erp_feeds, health
from clientscrapper.worker import main as worker
from tests.fake_bot import fake_bot
from tests.test_health import NOW, TRIGGERS, healthy_status

OWNER = 1001
MANAGER = 2002


@pytest.fixture
def routed(monkeypatch):
    """The worker wired to a fake owner bot, as ``connect_owner_bot`` wires it
    in production; the scheduler's offline memo emptied."""
    monkeypatch.setattr(settings, "assistant_bot_token", "42:TEST")
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER)
    monkeypatch.setattr(settings, "manager_telegram_ids", (MANAGER,))
    bot, fake = fake_bot()
    monkeypatch.setattr(deliver, "make_bot", lambda token=None: bot)
    worker._offline_alerted_at.clear()
    connected = worker.connect_owner_bot()
    assert connected is bot
    yield fake
    worker.set_owner_notifier(None)
    worker.set_backup_sender(None)
    costs.set_cap_notifier(None)
    worker._offline_alerted_at.clear()


def status_with(key):
    mutate, fragment = TRIGGERS[key]
    status = healthy_status()
    mutate(status)
    return status, fragment


def gathering(monkeypatch, status):
    async def gather(session, *, now, disk=None, evidence_size=False):
        return status

    monkeypatch.setattr(health, "gather", gather)


def owner_texts(fake):
    return [f["text"] for f in fake.named("SendMessage") if f["chat_id"] == OWNER]


SAID_BY_THE_SWEEP = [
    k for k in health.PROBLEM_KEYS if k not in health.TOLD_FIRST_ELSEWHERE
]


def test_every_key_is_routed_by_one_of_the_two_paths():
    assert set(SAID_BY_THE_SWEEP) | health.TOLD_FIRST_ELSEWHERE == set(
        health.PROBLEM_KEYS
    )
    assert set(TRIGGERS) == set(health.PROBLEM_KEYS)


@pytest.mark.parametrize("key", SAID_BY_THE_SWEEP)
async def test_each_problem_reaches_the_owner_through_the_sweep(
    session, routed, monkeypatch, key
):
    status, fragment = status_with(key)
    gathering(monkeypatch, status)
    await worker.health_job(now=NOW)
    [text] = owner_texts(routed)
    assert fragment in text
    # Owner only: a manager never receives a health alert (06 §1.7, §4.3).
    assert routed.chats() == {OWNER}
    if key != "db_down":
        # Delivered, then marked: the ledger holds what reached the owner.
        kinds = (await session.scalars(sa.select(ReminderLog.kind))).all()
        assert health.ALERT_KIND + key in kinds
    # The next sweep within the repeat window says nothing new.
    await worker.health_job(now=NOW + timedelta(minutes=5))
    assert len(owner_texts(routed)) == 1


@pytest.mark.parametrize("key", sorted(health.TOLD_FIRST_ELSEWHERE))
async def test_a_told_first_key_is_said_by_its_job_and_never_first_by_the_sweep(
    session, routed, monkeypatch, key
):
    status, fragment = status_with(key)
    gathering(monkeypatch, status)
    await worker.health_job(now=NOW)
    # (cost_cap's status is past 80 % too: spend_high is the sweep's to say.)
    assert [t for t in owner_texts(routed) if fragment in t] == []
    routed.clear()
    # The owning job tells it (the ERP breaker's tell_breaker, ask's cap line
    # through the same claim-then-tell) through the notifier the worker wired.
    [problem] = [p for p in health.problems(status) if p.key == key]
    assert await costs.tell_once(key, problem.text, now=NOW) is True
    assert [fragment in t for t in owner_texts(routed)] == [True]
    await worker.health_job(now=NOW + timedelta(minutes=5))
    assert len(owner_texts(routed)) == 1
    assert routed.chats() == {OWNER}


@pytest.mark.parametrize("key", sorted(health.TOLD_FIRST_ELSEWHERE))
async def test_a_told_first_key_its_job_could_not_deliver_is_said_by_the_sweep(
    session, routed, monkeypatch, key
):
    status, fragment = status_with(key)
    [problem] = [p for p in health.problems(status) if p.key == key]
    # Telegram is down when the owning job tells it: not delivered, noted.
    routed.fail("SendMessage", ConnectionError("telegram down"))
    assert await costs.tell_once(key, problem.text, now=NOW) is False
    kinds = (await session.scalars(sa.select(ReminderLog.kind))).all()
    assert kinds == [costs.UNDELIVERED_KIND + key]
    # Telegram is back: the next sweep says it, once.
    gathering(monkeypatch, status)
    routed.clear()
    await worker.health_job(now=NOW + timedelta(minutes=5))
    [text] = owner_texts(routed)
    assert fragment in text
    await worker.health_job(now=NOW + timedelta(minutes=10))
    assert len(owner_texts(routed)) == 1


async def test_a_breaker_tripped_in_a_process_with_no_notifier_reaches_the_owner(
    session, routed, monkeypatch
):
    """``make erp-sync`` runs in a one-off container with no owner bot: the
    feed trips the breaker and its tell has nobody to tell. Before T-35 the
    sweep waited for a teller that had given up; now it says it."""
    costs.set_cap_notifier(None)  # the one-off process
    assert await erp_feeds.tell_breaker("push_404", status=404, now=NOW) is False
    costs.set_cap_notifier(worker.notify)  # back in the worker
    status, fragment = status_with("push_404")
    gathering(monkeypatch, status)
    await worker.health_job(now=NOW + timedelta(minutes=1))
    assert [fragment in t for t in owner_texts(routed)] == [True]


async def test_an_old_undelivered_note_is_another_episode_s(session, routed, monkeypatch):
    """A note older than the key's repeat window (or than its last recovery)
    does not let the sweep say a new episode first."""
    costs.mark_undelivered(session, "push_404", NOW - timedelta(hours=7))
    session.add(
        ReminderLog(
            kind="undelivered:erp_redirect",
            ref="erp_redirect",
            sent_at=NOW - timedelta(hours=2),
        )
    )
    health.mark_recovered(session, ["erp_redirect"], now=NOW - timedelta(hours=1))
    await session.commit()
    for key in ("push_404", "erp_redirect"):
        status, _ = status_with(key)
        gathering(monkeypatch, status)
        await worker.health_job(now=NOW)
    assert owner_texts(routed) == []


async def test_a_quiet_hours_cost_cap_held_by_ask_is_no_undelivered_note(
    session, monkeypatch
):
    """Held for the end of the quiet hours is not a failed delivery: nothing
    is noted, the refusal rows keep the cap in force for the morning."""
    told: list[str] = []

    async def notify(text: str) -> bool:
        told.append(text)
        return True

    costs.set_cap_notifier(notify, hold=lambda now: True)
    try:
        await costs._tell_cost_cap(costs.CAP_DAILY, NOW)
    finally:
        costs.set_cap_notifier(None)
    assert told == []
    assert (await session.scalars(sa.select(ReminderLog.kind))).all() == []


# --- a sweep that crashes cannot say it: the watchdog does ------------------------------


async def failing_sweep(session, runs: int = 3) -> None:
    await health.beat(
        session, "worker", detail={"jobs": 20}, now=NOW - timedelta(minutes=1)
    )
    await session.commit()
    for _ in range(runs):
        await worker.record_job_event("health", ok=False, error="ProgrammingError")


class Told:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.texts.append(text)
        return True


async def test_a_crashed_sweep_s_heartbeat_reaches_job_failing_through_the_watchdog(
    session,
):
    await failing_sweep(session)
    told = Told()
    report = await bot_main.watchdog_tick(told, now=NOW, memo={})
    assert report is not None and report.said == ("job_failing",)
    assert told.texts == [
        "⚠️ <code>health</code> ishi oxirgi 3 marta xato bilan tugadi "
        "(ProgrammingError) — <code>make worker</code> loglari"
    ]
    # The worker's own sweep, once back, says the same key: the shared ledger
    # keeps it from saying it again inside the window.
    found = health.problems(await health.gather(session, now=NOW))
    mine = [p for p in found if p.key == "job_failing"]
    assert len(mine) == 1 and mine[0].text == told.texts[0]
    assert (await health.tell(session, mine, told, now=NOW)).said == ()


async def test_two_raises_are_not_yet_a_failing_sweep(session):
    await failing_sweep(session, runs=2)
    told = Told()
    await bot_main.watchdog_tick(told, now=NOW, memo={})
    assert told.texts == []


async def test_the_watchdog_never_announces_job_failing_s_recovery(session):
    await failing_sweep(session)
    told = Told()
    await bot_main.watchdog_tick(told, now=NOW, memo={})
    await worker.record_job_event("health", ok=True, error=None)
    later = NOW + timedelta(minutes=10)
    await health.beat(session, "worker", detail={"jobs": 20}, now=later)
    await session.commit()
    report = await bot_main.watchdog_tick(told, now=later, memo={})
    assert report is not None and report == health.TellReport()
    # The sweep judges the whole key, and says its recovery.
    status = await health.gather(session, now=later)
    found = [p for p in health.problems(status) if p.key == "job_failing"]
    back = await health.tell(
        session, found, told, now=later, unjudged=health.unjudged(status)
    )
    assert back.recovered == ("job_failing",)


async def test_with_gather_broken_the_watchdog_reads_the_heartbeats_alone(
    session, monkeypatch
):
    """The sweep's crash is in the code both processes share: the watchdog's
    own gather raises too, and it still says the sweep and a silent worker."""
    await failing_sweep(session)

    async def broken(session, *, now, disk=None, evidence_size=False):
        raise RuntimeError("a column the code reads is gone")

    monkeypatch.setattr(health, "gather", broken)
    told = Told()
    report = await bot_main.watchdog_tick(told, now=NOW, memo={})
    assert report is not None and report.said == ("job_failing",)
    # Twenty-five minutes on, the worker has stopped beating as well.
    silent = NOW + timedelta(minutes=25)
    report = await bot_main.watchdog_tick(told, now=silent, memo={})
    assert report is not None and report.said == ("worker_silent",)
    assert told.texts[-1].startswith("⛔ Rejalashtiruvchi")


def test_the_sweep_and_the_watchdog_say_a_failing_job_in_one_set_of_words():
    status = healthy_status()
    status.jobs["health"] = health.judge(
        "health", NOW, {"ok": False, "error": "OSError", "failures": 4}, now=NOW
    )
    sweep = health.sweep_failing(status)
    assert sweep is not None and sweep.key == "job_failing"
    [problem] = [p for p in health.problems(status) if p.key == "job_failing"]
    assert problem.text == sweep.text
    status.components["worker"] = health.judge(
        "worker", NOW - timedelta(hours=1), {}, now=NOW
    )
    assert health.sweep_failing(status) is None  # a dead worker is worker_silent's
