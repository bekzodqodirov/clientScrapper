"""The container healthcheck and the beats it reads (06 §4.1, 06 §3.1).

«Healthy» is «a fresh heartbeat»: ``tools/healthcheck.py`` asks
``health.judge``/``health.healthy`` — the rule the health job alerts on — and
answers Docker with 0 or 1. The bot and userbot skeletons beat so that their
containers are healthy before T-13/T-20 build what runs inside them.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from clientscrapper.bot import main as bot
from clientscrapper.config import settings
from clientscrapper.services import health
from clientscrapper.tools import healthcheck
from clientscrapper.userbot import main as userbot

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 24, 10, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    # The shipped defaults (.env.example), pinned: the lines below name them.
    monkeypatch.setattr(settings, "heartbeat_stale_minutes", 10)
    monkeypatch.setattr(settings, "userbot_stale_minutes", 5)


def test_a_fresh_beat_is_healthy_a_stale_or_missing_one_is_not():
    fresh = healthcheck.verdict("worker", NOW - timedelta(minutes=3), {}, now=NOW)
    assert fresh == (True, "worker: beat 3 min ago (limit 10 min)")
    stale = healthcheck.verdict("worker", NOW - timedelta(minutes=11), {}, now=NOW)
    assert stale == (False, "worker: last beat 11 min ago (limit 10 min)")
    # On the threshold is still fresh: stale is «older than».
    edge = healthcheck.verdict("bot", NOW - timedelta(minutes=10), {}, now=NOW)
    assert edge == (True, "bot: beat 10 min ago (limit 10 min)")
    assert healthcheck.verdict("bot", None, None, now=NOW) == (
        False,
        "bot: no heartbeat yet",
    )


def test_the_reader_has_its_own_threshold_and_may_be_off_on_purpose():
    six = NOW - timedelta(minutes=6)
    assert healthcheck.verdict("userbot", six, {"enabled": True}, now=NOW) == (
        False,
        "userbot: last beat 6 min ago (limit 5 min)",
    )
    off = {"enabled": False, "built": False}
    week = NOW - timedelta(days=7)
    assert healthcheck.verdict("userbot", week, off, now=NOW) == (
        True,
        "userbot: switched off on purpose (beat 10080 min ago)",
    )


def test_the_healthcheck_and_the_health_job_agree_on_every_age():
    # One rule: for each age the container verdict equals «not stale» as the
    # health job's worker_silent reads it.
    for minutes in range(0, 30):
        seen = NOW - timedelta(minutes=minutes, seconds=30)
        ok, _ = healthcheck.verdict("worker", seen, {}, now=NOW)
        assert ok is not health.judge("worker", seen, {}, now=NOW).stale, minutes


async def test_main_reads_the_row_and_answers_docker(session, capsys):
    assert healthcheck.main(["worker"], now=NOW) == healthcheck.EXIT_UNHEALTHY
    assert capsys.readouterr().out == "worker: no heartbeat yet\n"
    await health.beat(
        session, "worker", detail={"jobs": 4}, now=NOW - timedelta(minutes=2)
    )
    await health.beat(session, "bot", now=NOW - timedelta(minutes=12))
    await session.commit()
    assert healthcheck.main(["worker"], now=NOW) == healthcheck.EXIT_HEALTHY
    assert capsys.readouterr().out == "worker: beat 2 min ago (limit 10 min)\n"
    assert healthcheck.main(["bot"], now=NOW) == healthcheck.EXIT_UNHEALTHY
    assert capsys.readouterr().out == "bot: last beat 12 min ago (limit 10 min)\n"


def test_bad_arguments_and_an_unreadable_database(capsys):
    for argv in ([], ["backup"], ["worker", "bot"]):
        assert healthcheck.main(argv, now=NOW) == healthcheck.EXIT_USAGE, argv
    capsys.readouterr()
    dead = "postgresql+psycopg://none:none@127.0.0.1:1/none"
    assert healthcheck.main(["worker"], now=NOW, database_url=dead) == 1
    out = capsys.readouterr().out
    assert out == "worker: heartbeat unreadable (OperationalError)\n"
    assert "none:none" not in out


async def test_the_module_runs_as_docker_calls_it(session):
    def run() -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return subprocess.run(
            [sys.executable, "-m", "clientscrapper.tools.healthcheck", "worker"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    missing = run()
    assert (missing.returncode, missing.stdout) == (1, "worker: no heartbeat yet\n")
    await health.beat(session, "worker", detail={"jobs": 4})
    await session.commit()
    fresh = run()
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert fresh.stdout.startswith("worker: beat 0 min ago")


async def test_the_userbot_skeleton_beats_once_as_switched_off(session, monkeypatch):
    idled: list[bool] = []

    async def _idle() -> None:
        idled.append(True)

    monkeypatch.setattr(settings, "userbot_enabled", True)
    monkeypatch.setattr(userbot, "idle_until_stopped", _idle)
    monkeypatch.setattr(userbot, "setup_logging", lambda *a, **k: None)
    await userbot.run()
    assert idled == [True]
    row = (await health.beats(session))["userbot"]
    assert row.detail == {"enabled": False, "built": False}
    # Whatever USERBOT_ENABLED says, nothing reads Telegram before T-13: the
    # container is healthy and the health job says nothing about the reader.
    later = row.last_seen_at + timedelta(days=3)
    assert healthcheck.verdict("userbot", row.last_seen_at, row.detail, now=later)[0]
    status = await health.gather(session, now=later)
    keys = [p.key for p in health.problems(status)]
    assert "userbot_silent" not in keys
    assert {"userbot_silent", "no_signals_24h"} <= health.unjudged(status)


async def test_the_bot_skeleton_beats_every_minute_until_stopped(session, monkeypatch):
    def _stop_soon(stop: asyncio.Event) -> tuple[int, ...]:
        asyncio.get_running_loop().call_later(0.3, stop.set)
        return ()

    monkeypatch.setattr(bot, "install_stop_signals", _stop_soon)
    monkeypatch.setattr(bot, "setup_logging", lambda *a, **k: None)
    assert timedelta(minutes=1) == bot.BEAT_EVERY
    await asyncio.wait_for(bot.run(), timeout=10)
    row = (await health.beats(session))["bot"]
    assert row.detail == {"telegram": False}
    assert healthcheck.verdict("bot", row.last_seen_at, row.detail, now=row.last_seen_at)[
        0
    ]
