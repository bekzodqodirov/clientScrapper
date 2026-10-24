"""Health (06 §4.1–4.3): the problem list, one trigger per key, the alert
memory's repeat and recovery timing, quiet hours, the claims, the Anthropic
probe, and the fence that keeps ``PROBLEM_KEYS`` the one list of keys.

The pure half builds ``Status`` values by hand; the database half writes
synthetic rows and reads them back through ``gather``. Every function under
test is handed ``now``.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import costs, health
from clientscrapper.services.health import (
    AnthropicFacts,
    BackupInfo,
    Reported,
    SourceState,
    Status,
    SuppressionHealth,
)
from tests.ast_tools import PACKAGE, modules, rel, tree_of
from tests.test_uz_text import problems as uz_problems

TZ = ZoneInfo("Asia/Tashkent")
# A Saturday noon: outside QUIET_HOURS (21:00-08:00).
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)
NIGHT = datetime(2026, 10, 24, 23, 30, tzinfo=TZ)
GB = 1000**3

# 06 §4.2, copied from the spec table (incl. erp_redirect, T-07 verification).
SPEC_KEYS = (
    "db_down",
    "disk_low",
    "worker_silent",
    "userbot_unauthorized",
    "push_404",
    "erp_redirect",
    "backup_failed",
    "restore_drill_failed",
    "suppression_stale",
    "suppression_selftest_failed",
    "suppression_stale_hit",
    "userbot_silent",
    "flood_breaker",
    "collector_blocked",
    "collector_silent",
    "cost_cap",
    "spend_high",
    "no_signals_24h",
    "push_failing",
    "push_unconfirmed",
    "feed_contract",
    "suppression_shrunk",
    "review_backlog",
    "anthropic_failing",
    "anthropic_silence",
    "batch_stuck",
    "pii_leak",
    "backup_unconfigured",
    "backup_stale",
    "ig_token_expired",
    "source_low_precision",
    "evidence_over_cap",
)
SPEC_CRITICAL = {
    "db_down",
    "disk_low",
    "worker_silent",
    "userbot_unauthorized",
    "push_404",
    "erp_redirect",
    "backup_failed",
    "restore_drill_failed",
}
# 07 §4.1 T-08's acceptance list, by name.
T08_NAMED = (
    "collector_blocked",
    "push_404",
    "erp_redirect",
    "cost_cap",
    "userbot_unauthorized",
    "flood_breaker",
    "no_signals_24h",
    "suppression_stale",
    "collector_silent",
    "batch_stuck",
    "push_unconfirmed",
    "push_failing",
    "suppression_shrunk",
    "feed_contract",
    "pii_leak",
    "ig_token_expired",
    "spend_high",
    "anthropic_silence",
    "suppression_selftest_failed",
    "suppression_stale_hit",
)


# --- the list ------------------------------------------------------------------


def test_problem_keys_are_06_s_table_in_its_order():
    assert health.PROBLEM_KEYS == SPEC_KEYS
    assert set(T08_NAMED) <= set(health.PROBLEM_KEYS)
    # 05's feed codes fold into feed_contract; 04's cost_80 is spend_high.
    for code in ("feed_bad_request", "feed_rule_changed", "feed_runaway", "cost_80"):
        assert code not in health.PROBLEM_KEYS


def test_the_critical_keys_and_the_ones_told_first_elsewhere():
    assert health.CRITICAL_KEYS == SPEC_CRITICAL
    assert {
        "cost_cap",
        "push_404",
        "erp_redirect",
        "flood_breaker",
    } == health.TOLD_FIRST_ELSEWHERE
    assert {"cost_cap", "spend_high"} == health.DAILY_KEYS
    assert set(health.PROBLEM_KEYS) >= health.REPORTED_KEYS


def test_every_key_has_its_own_recovery_line():
    for key in health.PROBLEM_KEYS:
        line = health.recovery_text(key)
        assert line.startswith("✅ ") and key not in line, key
        assert uz_problems(line, rendered=True) == set(), key


# --- a healthy status and one trigger per key ------------------------------------


def _beat(name: str, age: timedelta, **detail: object) -> health.Component:
    return health.judge(name, NOW - age, detail, now=NOW)


def healthy_status() -> Status:
    return Status(
        now=NOW,
        db_ok=True,
        disk_free_bytes=100 * GB,
        disk_total_bytes=200 * GB,
        disk_low=False,
        backup=BackupInfo(
            configured=True,
            path=Path("/data/backups/clientscrapper-20261024-034500.dump.age"),
            created_at=NOW - timedelta(hours=8),
            size=4096,
            ok=True,
            sent=True,
        ),
        components={
            "bot": _beat("bot", timedelta(minutes=1)),
            "worker": _beat("worker", timedelta(minutes=1), jobs=4),
            "userbot": _beat("userbot", timedelta(minutes=1), enabled=True),
        },
        restore_drill=_beat("restore_drill", timedelta(days=2), ok=True),
        suppression=SuppressionHealth(
            fresh=True,
            full_at=NOW - timedelta(hours=9),
            delta_at=NOW - timedelta(minutes=40),
            full_max_h=26,
            delta_max_h=3,
        ),
    )


def _reported(key: str, **detail: object) -> Callable[[Status], None]:
    def apply(status: Status) -> None:
        status.reported = {key: Reported(key, NOW - timedelta(hours=1), detail)}

    return apply


def _set(**fields: object) -> Callable[[Status], None]:
    def apply(status: Status) -> None:
        for name, value in fields.items():
            setattr(status, name, value)

    return apply


def _component(name: str, age: timedelta, **detail: object) -> Callable[[Status], None]:
    def apply(status: Status) -> None:
        status.components[name] = _beat(name, age, **detail)

    return apply


def _source(**kw: object) -> SourceState:
    fields: dict[str, object] = {
        "key": "olx_business",
        "title_uz": "OLX sotuvchilari",
        "enabled": True,
        "cron": "0 7 * * *",
        "paused_until": None,
        "breaker_until": None,
        "breaker_reason": None,
        "last_run_at": NOW - timedelta(hours=5),
        "last_skipped_at": None,
    }
    fields.update(kw)
    return SourceState(**fields)  # type: ignore[arg-type]


def _spend(day: str) -> costs.SpendSnapshot:
    return costs.SpendSnapshot(Decimal(day), Decimal(day), {})


# key → (mutation that makes exactly that problem hold, a fragment of its text)
TRIGGERS: dict[str, tuple[Callable[[Status], None], str]] = {
    "db_down": (_set(db_ok=False), "Baza javob bermayapti"),
    "disk_low": (_set(disk_free_bytes=2 * GB, disk_low=True), "Diskda 2.0 GB qoldi"),
    "worker_silent": (
        _component("worker", timedelta(minutes=25)),
        "Rejalashtiruvchi 25 daqiqa jim",
    ),
    "userbot_unauthorized": (
        _component(
            "userbot",
            timedelta(minutes=1),
            enabled=True,
            authorized=False,
            error="SessionRevokedError",
        ),
        "cheklangan (SessionRevokedError)",
    ),
    "push_404": (
        _set(erp_breaker_until=NOW + timedelta(hours=5), erp_breaker_reason="push_404"),
        "ERP kaliti yoki manba notoʻgʻri: 404",
    ),
    "erp_redirect": (
        _set(
            erp_breaker_until=NOW + timedelta(hours=5),
            erp_breaker_reason="erp_redirect:308",
        ),
        "yoʻnaltiryapti (HTTP 308)",
    ),
    "backup_failed": (
        lambda s: setattr(s, "backup", replace(s.backup, ok=False, error="empty_dump")),
        "Tungi zaxira yozilmadi: <code>empty_dump</code>",
    ),
    "restore_drill_failed": (
        lambda s: setattr(
            s,
            "restore_drill",
            _beat("restore_drill", timedelta(hours=1), ok=False, error="counts differ"),
        ),
        "tiklash sinovi oʻtmadi",
    ),
    "suppression_stale": (
        lambda s: setattr(
            s,
            "suppression",
            SuppressionHealth(
                fresh=False,
                full_at=NOW - timedelta(hours=27),
                delta_at=NOW - timedelta(minutes=30),
                full_max_h=26,
                delta_max_h=3,
                stale_since=NOW - timedelta(hours=1),
            ),
        ),
        "toʻliq 1 kun oldin (chegara 26 soat) · farq 30 daqiqa oldin (chegara 3 soat)",
    ),
    "suppression_selftest_failed": (
        _reported("suppression_selftest_failed"),
        "oʻz-oʻzini tekshirishdan oʻtmadi",
    ),
    "suppression_stale_hit": (_reported("suppression_stale_hit", n=2), "ERP 2 ta pushni"),
    "userbot_silent": (
        _component("userbot", timedelta(minutes=12), enabled=True),
        "Telegram oʻquvchi 12 daqiqa jim",
    ),
    "flood_breaker": (
        _set(flood_until=NOW + timedelta(hours=20), flood_seconds=420),
        "FloodWait (420 s) — oʻquvchi 25.10 08:00 gacha",
    ),
    "collector_blocked": (
        _set(
            sources=(
                _source(
                    breaker_until=NOW + timedelta(hours=3),
                    breaker_reason="host_blocked:www.olx.uz",
                    last_run_at=NOW - timedelta(minutes=10),
                ),
            )
        ),
        "OLX sotuvchilari: sayt bloklayapti (403/429) — 24.10 15:00 gacha",
    ),
    "collector_silent": (
        _set(sources=(_source(cron="15 * * * *"),)),
        "OLX sotuvchilari 5 soat ishlamadi",
    ),
    "cost_cap": (_set(spend=_spend("10.00")), "Kunlik xarajat chegarasi ($10)"),
    "spend_high": (_set(spend=_spend("8.50")), "Kunlik xarajat 85% ga yetdi"),
    "no_signals_24h": (
        _set(chats_on=3, monitoring_since=NOW - timedelta(days=3), interactions_24h=0),
        "24 soatda bitta ham Telegram signal yoʻq",
    ),
    "push_failing": (_set(push_failing_streak=3), "(3 ta push muvaffaqiyatsiz)"),
    "push_unconfirmed": (_reported("push_unconfirmed", n=4), "4 ta push 48 soatdan"),
    "feed_contract": (
        _reported("feed_contract", code="runaway"),
        "feed shakli oʻzgardi (runaway)",
    ),
    "suppression_shrunk": (_reported("suppression_shrunk", pct=72), "72% ga qisqardi"),
    "review_backlog": (_set(review_backlog=31), "31 ta karta 3 kundan beri"),
    "anthropic_failing": (
        _reported("anthropic_failing", code="overloaded"),
        "Anthropic javob bermayapti (overloaded)",
    ),
    "anthropic_silence": (
        _set(
            anthropic=AnthropicFacts(
                NOW - timedelta(hours=4), NOW - timedelta(minutes=10), 7
            )
        ),
        "Anthropic 4 soat jim — tasnif navbatda (7 ta)",
    ),
    "batch_stuck": (
        _set(stuck_batches=(("msgbatch_namuna01", NOW - timedelta(hours=27)),)),
        "Tungi batch 1 kun tugamadi (msgbatch_namuna01)",
    ),
    "pii_leak": (_reported("pii_leak", n=1), "shaxsiy maʼlumot chiqdi (1 ta"),
    "backup_unconfigured": (
        _set(backup=BackupInfo(configured=False)),
        "Zaxira sozlanmagan",
    ),
    "backup_stale": (
        lambda s: setattr(
            s,
            "backup",
            replace(s.backup, created_at=NOW - timedelta(hours=31), stale=True),
        ),
        "Oxirgi zaxira 1 kun oldin",
    ),
    "ig_token_expired": (_reported("ig_token_expired"), "Instagram tokeni eskirdi"),
    "source_low_precision": (
        _reported(
            "source_low_precision",
            sources=[{"key": "olx_business", "title": "OLX sotuvchilari", "pct": 14}],
        ),
        "OLX sotuvchilari aniqligi 14% — bir haftaga toʻxtatildi",
    ),
    "evidence_over_cap": (
        _reported("evidence_over_cap", gb=31.2),
        "Dalil papkasi 31.2 GB",
    ),
}


def test_a_healthy_status_has_no_problem():
    assert health.problems(healthy_status()) == []


def test_every_key_has_a_trigger():
    assert set(TRIGGERS) == set(health.PROBLEM_KEYS)


@pytest.mark.parametrize("key", SPEC_KEYS)
def test_each_trigger_raises_its_key_with_the_06_severity(key):
    mutate, fragment = TRIGGERS[key]
    status = healthy_status()
    mutate(status)
    found = health.problems(status)
    keys = [p.key for p in found]
    # Reaching a cap is past 80 % of it too (04 §8): both lines hold.
    assert keys == (["cost_cap", "spend_high"] if key == "cost_cap" else [key])
    [problem] = [p for p in found if p.key == key]
    assert problem.severity == ("critical" if key in SPEC_CRITICAL else "warning")
    assert fragment in problem.text
    assert problem.text.startswith(("⛔", "⚠️", "💸")), problem.text
    assert uz_problems(problem.text, rendered=True) == set()


def test_with_the_database_down_only_the_database_and_the_disk_are_judged():
    status = healthy_status()
    TRIGGERS["db_down"][0](status)
    TRIGGERS["disk_low"][0](status)
    TRIGGERS["worker_silent"][0](status)
    assert [p.key for p in health.problems(status)] == ["db_down", "disk_low"]
    assert health.unjudged(status) == frozenset(health.PROBLEM_KEYS) - {
        "db_down",
        "disk_low",
    }


def test_the_suppression_line_turns_critical_after_six_hours():
    status = healthy_status()
    TRIGGERS["suppression_stale"][0](status)
    assert status.suppression is not None
    status.suppression = replace(status.suppression, stale_since=NOW - timedelta(hours=6))
    [problem] = health.problems(status)
    assert (problem.key, problem.severity) == ("suppression_stale", "critical")
    assert problem.text.startswith("⛔ ERP bostirish")
    status.suppression = replace(
        status.suppression, stale_since=None, salt_ok=False, full_at=None
    )
    [problem] = health.problems(status)
    assert problem.severity == "warning"
    assert "toʻliq hech qachon" in problem.text and "kalit almashgan" in problem.text


def test_suppression_without_its_probe_is_unjudged():
    status = healthy_status()
    status.suppression = None
    assert health.problems(status) == []
    assert "suppression_stale" in health.unjudged(status)


def test_a_breaker_for_another_reason_or_one_that_ran_out_is_not_push_404():
    status = healthy_status()
    status.erp_breaker_reason = "push_404"
    status.erp_breaker_until = NOW - timedelta(seconds=1)
    assert health.problems(status) == []
    status.erp_breaker_until = NOW + timedelta(hours=1)
    status.erp_breaker_reason = "push_4040"
    assert health.problems(status) == []
    status.erp_breaker_reason = "erp_redirect"
    [problem] = health.problems(status)
    assert problem.key == "erp_redirect" and "(HTTP 3xx)" in problem.text


def test_an_erp_breaker_is_not_a_blocked_collector_and_vice_versa():
    status = healthy_status()
    status.sources = (
        _source(
            key="erp_sync",
            title_uz="ERP (fullprompt)",
            cron="15 * * * *",
            breaker_until=NOW + timedelta(hours=6),
            breaker_reason="push_404",
            last_run_at=NOW - timedelta(minutes=20),
        ),
    )
    status.erp_breaker_until = NOW + timedelta(hours=6)
    status.erp_breaker_reason = "push_404"
    assert [p.key for p in health.problems(status)] == ["push_404"]


def test_userbot_silence_and_dead_chats_are_not_said_twice_for_one_cause():
    status = healthy_status()
    TRIGGERS["userbot_unauthorized"][0](status)
    status.components["userbot"] = _beat(
        "userbot",
        timedelta(minutes=14),
        enabled=True,
        authorized=False,
        error="AuthKeyUnregisteredError",
    )
    status.chats_on = 3
    status.monitoring_since = NOW - timedelta(days=3)
    assert [p.key for p in health.problems(status)] == ["userbot_unauthorized"]


async def test_a_userbot_telegram_could_not_ask_is_unknown_not_lost_nor_back(session):
    """T-13 beats ``authorized: null`` when Telegram did not answer the check:
    no userbot_unauthorized, no «qayta ulandi» for an earlier one either —
    the state is unknown, and /holat can say so (``userbot_auth``)."""
    lost = healthy_status()
    TRIGGERS["userbot_unauthorized"][0](lost)
    assert health.userbot_auth(lost) == "unauthorized"
    notify = Notify()
    found = health.problems(lost)
    await health.tell(session, found, notify, now=NOW, unjudged=health.unjudged(lost))
    assert [p.key for p in found] == ["userbot_unauthorized"]
    unknown = healthy_status()
    unknown.components["userbot"] = _beat(
        "userbot",
        timedelta(minutes=1),
        enabled=True,
        authorized=None,
        connected=False,
        error="TimeoutError",
    )
    assert health.userbot_auth(unknown) == "unknown"
    assert health.userbot_unauthorized(unknown) is None
    assert health.problems(unknown) == []
    assert "userbot_unauthorized" in health.unjudged(unknown)
    later = NOW + timedelta(minutes=5)
    report = await health.tell(
        session, [], notify, now=later, unjudged=health.unjudged(unknown)
    )
    assert "userbot_unauthorized" not in report.recovered
    # Signed in again: now it is back.
    back = healthy_status()
    back.components["userbot"] = _beat(
        "userbot", timedelta(minutes=1), enabled=True, authorized=True
    )
    assert health.userbot_auth(back) == "authorized"
    assert "userbot_unauthorized" not in health.unjudged(back)
    report = await health.tell(
        session, [], notify, now=later, unjudged=health.unjudged(back)
    )
    assert "userbot_unauthorized" in report.recovered


def test_userbot_auth_reads_only_the_three_states():
    status = healthy_status()  # an enabled beat that names no state
    assert health.userbot_auth(status) is None
    for value in ("false", 0, "no"):
        status.components["userbot"] = _beat(
            "userbot", timedelta(minutes=1), enabled=True, authorized=value
        )
        assert health.userbot_auth(status) is None
        assert health.userbot_unauthorized(status) is None
    off = healthy_status()
    off.components["userbot"] = _beat(
        "userbot", timedelta(minutes=1), enabled=False, authorized=False
    )
    assert health.userbot_auth(off) is None


def test_the_erp_breaker_line_has_one_home():
    """problems() renders push_404 / erp_redirect through erp_breaker_text,
    the line the feed sync tells first (T-09): /holat and the alert agree."""
    status = healthy_status()
    status.erp_breaker_until = NOW + timedelta(hours=5)
    status.erp_breaker_reason = "push_404:feed"
    [problem] = health.problems(status)
    assert problem.key == "push_404"
    assert problem.text == health.erp_breaker_text("push_404")
    status.erp_breaker_reason = "erp_redirect:308"
    [problem] = health.problems(status)
    assert problem.text == health.erp_breaker_text("erp_redirect", "308")
    with pytest.raises(ValueError):
        health.erp_breaker_text("collector_blocked")


def test_a_userbot_switched_off_is_neither_silent_nor_judged():
    status = healthy_status()
    status.components["userbot"] = _beat("userbot", timedelta(days=9), enabled=False)
    status.chats_on = 3
    status.monitoring_since = NOW - timedelta(days=3)
    assert health.problems(status) == []
    assert {"userbot_silent", "userbot_unauthorized", "no_signals_24h"} <= (
        health.unjudged(status)
    )


def test_no_signals_waits_for_a_whole_day_of_monitoring():
    status = healthy_status()
    _set(chats_on=2, monitoring_since=NOW - timedelta(hours=23))(status)
    assert health.problems(status) == []
    status.monitoring_since = NOW - timedelta(hours=24)
    assert [p.key for p in health.problems(status)] == ["no_signals_24h"]
    status.interactions_24h = 1
    assert health.problems(status) == []


# --- collector_silent: what it counts and what it never counts ------------------


@pytest.mark.parametrize(
    ("change", "silent"),
    [
        ({}, True),
        ({"enabled": False}, False),
        ({"cron": None}, False),
        ({"paused_until": NOW + timedelta(hours=1)}, False),
        ({"paused_until": NOW - timedelta(hours=9)}, True),
        (
            {
                "breaker_until": NOW + timedelta(hours=1),
                "breaker_reason": "host_blocked:x.uz",
            },
            False,
        ),
        ({"last_run_at": None}, False),  # never ran, never fired: no clock yet
        # Never ran and never fired since it was enabled (its row's last
        # write): a built job that fails before it claims a run is silent too.
        ({"last_run_at": None, "updated_at": NOW - timedelta(hours=3)}, True),
        ({"last_run_at": None, "updated_at": NOW - timedelta(minutes=50)}, False),
        # At :45, last run 10:40: 10:45 and 11:45 unrun — but 11:45 is only
        # 15 minutes old, inside COLLECTOR_GRACE.
        (
            {"cron": "45 * * * *", "last_run_at": NOW - timedelta(hours=1, minutes=20)},
            False,
        ),
        (
            {"cron": "45 * * * *", "last_run_at": NOW - timedelta(hours=2, minutes=20)},
            True,
        ),
        ({"last_run_at": NOW - timedelta(hours=1, minutes=40)}, False),
        ({"last_run_at": NOW - timedelta(hours=2, minutes=50)}, True),
        # The scheduler fired it 40 minutes ago while it could not run.
        ({"last_skipped_at": NOW - timedelta(minutes=40)}, False),
    ],
)
def test_collector_silence(change, silent):
    status = healthy_status()
    # Hourly at :15, last run 5 h ago → fired 07:15 … 11:15 unrun.
    status.sources = (_source(**{"cron": "15 * * * *", **change}),)
    assert bool(health.silent_sources(status)) is silent


def test_a_sources_cron_the_scheduler_cannot_read_is_named_not_judged():
    """The worker falls back to the seeded cron (worker.slot_trigger); the
    owner is told the row is wrong instead of a silence nobody can judge."""
    status = healthy_status()
    status.sources = (_source(cron="0 23 1-7 * 4"),)
    assert health.silent_sources(status) == []
    [problem] = health.problems(status)
    assert problem.key == "collector_silent"
    assert problem.text == (
        "⚠️ OLX sotuvchilari: jadval («0 23 1-7 * 4») notoʻgʻri — oʻrniga dastlabki "
        "jadval ishlatilmoqda; /manbalar"
    )
    assert uz_problems(problem.text, rendered=True) == set()


def test_a_global_pause_or_crawling_off_silences_nothing_and_recovers_nothing(
    monkeypatch,
):
    status = healthy_status()
    status.sources = (_source(cron="15 * * * *"),)
    status.paused = True
    assert health.silent_sources(status) == []
    assert "collector_silent" in health.unjudged(status)
    status.paused = False
    monkeypatch.setattr(settings, "collectors_enabled", False)
    assert health.silent_sources(status) == []
    assert "collector_silent" in health.unjudged(status)


def test_a_weekly_cron_is_read_as_crontab_not_as_apscheduler():
    # «0 9 * * 1» is Monday 09:00 (exhibitors, 03 §3). Read by APScheduler's
    # own numbering it would be Tuesday and the silence a day late.
    monday = datetime(2026, 10, 26, 9, 0, tzinfo=TZ)
    status = healthy_status()
    status.now = monday + timedelta(days=7, hours=1)  # two Mondays went by
    status.sources = (_source(cron="0 9 * * 1", last_run_at=monday - timedelta(days=7)),)
    [(source, age)] = health.silent_sources(status)
    assert (source.key, age) == ("olx_business", timedelta(days=14, hours=1))
    status.now = monday + timedelta(minutes=31)  # one Monday only
    assert health.silent_sources(status) == []


# --- anthropic_silence: no false start, no latch ----------------------------------


@pytest.mark.parametrize(
    ("facts", "silent"),
    [
        # Never called: nothing to judge (and costs.ask lets the first call by).
        (AnthropicFacts(None, None, 5), False),
        # A day at the cost cap: only refusals, the last attempt succeeded.
        (AnthropicFacts(NOW - timedelta(hours=9), NOW - timedelta(hours=9), 5), False),
        # Tried and failed for 3 h.
        (AnthropicFacts(NOW - timedelta(hours=3), NOW - timedelta(minutes=5), 5), True),
        (AnthropicFacts(None, NOW - timedelta(minutes=5), 5), True),
        # Failing, but nothing waits.
        (AnthropicFacts(NOW - timedelta(hours=4), NOW - timedelta(minutes=5), 0), False),
        # Failing for less than 3 h.
        (
            AnthropicFacts(NOW - timedelta(hours=2, minutes=59), NOW, 5),
            False,
        ),
    ],
)
def test_anthropic_silence(facts, silent):
    assert health.anthropic_silent(facts, NOW) is silent


# --- database half ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No backup directory of the host, no probe and no cached Anthropic
    verdict leaking between tests."""
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "backup_age_recipient", "age1namuna")
    health.set_suppression_probe(None)
    health.reset_anthropic_cache()
    yield
    health.set_suppression_probe(None)
    health.reset_anthropic_cache()


DISK = (100 * GB, 200 * GB)


async def _gather(session, now=NOW):
    return await health.gather(session, now=now, disk=DISK)


async def _fresh_beats(session, now=NOW):
    for name in health.COMPONENTS:
        await health.beat(session, name, detail={"enabled": True}, now=now)


async def test_gather_on_an_empty_database(session):
    status = await _gather(session)
    assert status.db_ok
    # No process ever beat and no backup exists yet.
    assert [p.key for p in health.problems(status)] == [
        "worker_silent",
        "userbot_silent",
        "backup_stale",
    ]
    assert "hali bir marta ham xabar bermagan" in health.problems(status)[0].text
    assert "suppression_stale" in health.unjudged(status)


async def _company_and_candidate(session, state="approved"):
    company = await session.scalar(
        sa.text(
            "INSERT INTO companies (display_name, name_key) "
            "VALUES ('Namuna Tekstil Savdo', 'namuna tekstil savdo') RETURNING id"
        )
    )
    candidate = await session.scalar(
        sa.text(
            "INSERT INTO lead_candidates (company_id, state, score, approved_at, "
            "approved_by) VALUES (:c, :s, 70, now(), 'owner:1') RETURNING id"
        ),
        {"c": company, "s": state},
    )
    return company, candidate


async def test_review_backlog_counts_from_the_card_not_the_last_rescore(session):
    """A rescore or an enrichment touches lead_candidates.updated_at; the
    card the owner has not answered for four days still waits four days."""
    _, waiting = await _company_and_candidate(session, state="review")
    await session.execute(
        sa.text(
            "INSERT INTO lead_reviews (candidate_id, kind, asked_at) "
            "VALUES (:c, 'owner', :at)"
        ),
        {"c": waiting, "at": NOW - timedelta(days=4)},
    )
    # A candidate in review that was never shown and changed an hour ago.
    await _company_and_candidate(session, state="review")
    await session.execute(
        sa.text("UPDATE lead_candidates SET updated_at = :at"),
        {"at": NOW - timedelta(hours=1)},
    )
    await session.commit()
    status = await _gather(session)
    assert status.review_backlog == 1


async def _push(session, company, candidate, *, status, error, at, http=None):
    await session.execute(
        sa.text(
            "INSERT INTO push_log (external_id, attempt, candidate_id, company_id, "
            "phone_tail, endpoint, source_key_sent, payload_sha256, status, error, "
            "http_status, sent_at) VALUES (:e, 1, :cand, :c, '900000001', "
            "'/api/leads/in/agent', 'agent', :sha, :s, :err, :h, :at)"
        ),
        {
            "e": f"cs:{company}:202610",
            "cand": candidate,
            "c": company,
            "sha": "0" * 64,
            "s": status,
            "err": error,
            "h": http,
            "at": at,
        },
    )


async def test_gather_reads_each_table_fact(session):
    await _fresh_beats(session)
    await health.beat(session, "backup", detail={"ok": False, "error": "x"}, now=NOW)
    await session.execute(
        sa.update(m.Source)
        .where(m.Source.key == "erp_sync")
        .values(breaker_until=NOW + timedelta(hours=6), breaker_reason="push_404")
    )
    await session.execute(
        sa.update(m.Source)
        .where(m.Source.key == "hh_vacancies")
        .values(
            breaker_until=NOW + timedelta(hours=2),
            breaker_reason="host_blocked:api.hh.ru",
        )
    )
    session.add(
        m.RuntimeFlag(
            key="flood_until",
            value={"until": (NOW + timedelta(hours=3)).isoformat(), "seconds": 900},
            updated_by="userbot",
        )
    )
    for i in range(3):
        company, candidate = await _company_and_candidate(session)
        await _push(
            session,
            company,
            candidate,
            status="failed",
            error="http_5xx",
            http=503,
            at=NOW - timedelta(minutes=30 - i),
        )
    session.add(
        m.ModelJob(
            kind="company_verdict",
            lane="batch",
            model=settings.batch_model,
            custom_id="cs-namuna-1",
            batch_id="msgbatch_namuna01",
            status="submitted",
            submitted_at=NOW - timedelta(hours=27),
        )
    )
    await session.commit()
    try:
        status = await _gather(session)
        keys = [p.key for p in health.problems(status)]
        assert keys == [
            "push_404",
            "backup_failed",
            "flood_breaker",
            "collector_blocked",
            "push_failing",
            "batch_stuck",
            "backup_stale",  # a recipient is set and no file was written
        ]
        assert status.flood_seconds == 900 and status.push_failing_streak == 3
        assert status.stuck_batches[0][0] == "msgbatch_namuna01"
    finally:
        await session.execute(
            sa.update(m.Source)
            .where(m.Source.key.in_(["erp_sync", "hh_vacancies"]))
            .values(breaker_until=None, breaker_reason=None)
        )
        await session.commit()


async def test_a_push_that_got_through_ends_the_failing_streak(session):
    await _fresh_beats(session)
    companies = [await _company_and_candidate(session) for _ in range(3)]
    for i, (company, candidate) in enumerate(companies):
        await _push(
            session,
            company,
            candidate,
            status="failed",
            error="conn",
            at=NOW - timedelta(minutes=20 - i),
        )
    company, candidate = companies[-1]
    await session.execute(
        sa.text(
            "INSERT INTO push_log (external_id, attempt, candidate_id, company_id, "
            "phone_tail, endpoint, source_key_sent, payload_sha256, status, "
            "http_status, sent_at) VALUES (:e, 2, :cand, :c, '900000001', "
            "'/api/leads/in/agent', 'agent', :sha, 'sent', 200, :at)"
        ),
        {
            "e": f"cs:{company}:202610",
            "cand": candidate,
            "c": company,
            "sha": "0" * 64,
            "at": NOW - timedelta(minutes=1),
        },
    )
    await session.commit()
    status = await _gather(session)
    assert status.push_failing_streak == 0


async def test_anthropic_facts_skip_refusals_claims_in_flight_and_batch_rows(session):
    def row(model, at, *, tokens=None, batch=False):
        return m.UsageLog(
            provider="anthropic",
            operation="classify",
            model=model,
            cost_usd=Decimal("0.001"),
            input_tokens=tokens,
            batch=batch,
            created_at=at,
        )

    session.add_all(
        [
            row(settings.extract_model, NOW - timedelta(hours=5), tokens=900),
            # costs.ask marked this claim: its call raised.
            row("failed:APITimeoutError", NOW - timedelta(hours=1)),
            row("refused:anthropic_silence", NOW - timedelta(minutes=5)),
            row(settings.batch_model, NOW - timedelta(minutes=2), batch=True),
            # A claim whose call is still waiting for its answer.
            row(None, NOW - timedelta(minutes=1)),
        ]
    )
    session.add(
        m.ModelJob(kind="classify", lane="instant", model="m", custom_id="cs-x-1")
    )
    await session.commit()
    facts = await health.anthropic_facts(session)
    assert facts == AnthropicFacts(NOW - timedelta(hours=5), NOW - timedelta(hours=1), 1)
    assert health.anthropic_silent(facts, NOW)


async def test_raised_problems_round_trip_keep_since_and_expire(session):
    await health.raise_problem(
        session, "feed_contract", now=NOW, by="worker:erp_sync", detail={"code": "shape"}
    )
    later = NOW + timedelta(hours=2)
    await health.raise_problem(
        session, "feed_contract", now=later, by="worker:erp_sync", detail={"code": "x"}
    )
    await health.raise_problem(
        session, "pii_leak", now=NOW, by="worker:classify", until=NOW + timedelta(hours=1)
    )
    await session.commit()
    found = await health.reported_problems(session, later)
    assert set(found) == {"feed_contract"}  # pii_leak expired at 13:00
    assert found["feed_contract"].since == NOW
    assert found["feed_contract"].detail == {"code": "x"}
    assert found["feed_contract"].by == "worker:erp_sync"
    assert await health.clear_problem(session, "feed_contract") is True
    assert await health.clear_problem(session, "feed_contract") is False
    await session.commit()
    assert await health.reported_problems(session, later) == {}


async def test_only_reported_keys_may_be_raised(session):
    with pytest.raises(ValueError):
        await health.raise_problem(session, "push_404", now=NOW, by="x")
    with pytest.raises(ValueError):
        await health.raise_problem(session, "feed_bad_request", now=NOW, by="x")
    with pytest.raises(ValueError):
        await health.clear_problem(session, "disk_low")


async def test_the_suppression_probe_is_read_and_a_broken_one_is_unjudged(session):
    async def stale(_session, now):
        return SuppressionHealth(False, None, None, 26, 3)

    health.set_suppression_probe(stale)
    await _fresh_beats(session)
    status = await _gather(session)
    assert "suppression_stale" in [p.key for p in health.problems(status)]

    async def broken(_session, now):
        raise RuntimeError("probe")

    health.set_suppression_probe(broken)
    status = await _gather(session)
    assert status.suppression is None
    assert "suppression_stale" in health.unjudged(status)


# --- the alert memory --------------------------------------------------------------


class Notify:
    """The owner seam, recorded: no recipient argument exists to get wrong."""

    def __init__(self, delivered: bool = True) -> None:
        self.delivered = delivered
        self.sent: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return self.delivered


def _problem(key: str) -> health.Problem:
    status = healthy_status()
    TRIGGERS[key][0](status)
    [problem] = [p for p in health.problems(status) if p.key == key]
    return problem


async def _kinds(session) -> list[tuple[str, datetime]]:
    rows = await session.execute(
        sa.select(m.ReminderLog.kind, m.ReminderLog.sent_at).order_by(m.ReminderLog.id)
    )
    return [(k, at) for k, at in rows.all()]


async def test_a_warning_repeats_after_alert_repeat_hours_only_and_recovers_once(
    session,
):
    notify = Notify()
    warn = _problem("review_backlog")
    report = await health.tell(session, [warn], notify, now=NOW)
    assert report.said == ("review_backlog",)
    assert notify.sent == [warn.text]

    hours = settings.alert_repeat_hours
    for at in (NOW + timedelta(minutes=5), NOW + timedelta(hours=hours)):
        assert (await health.tell(session, [warn], notify, now=at)).said == ()
    again = NOW + timedelta(hours=hours, minutes=5)
    assert (await health.tell(session, [warn], notify, now=again)).said == (
        "review_backlog",
    )
    back = again + timedelta(minutes=5)
    assert (await health.tell(session, [], notify, now=back)).recovered == (
        "review_backlog",
    )
    assert notify.sent[-1] == health.recovery_text("review_backlog")
    # Once: the next sweeps say nothing.
    assert await health.tell(session, [], notify, now=back + timedelta(minutes=5)) == (
        health.TellReport()
    )
    assert [k for k, _ in await _kinds(session)] == [
        "alert:review_backlog",
        "alert:review_backlog",
        "recovered:review_backlog",
    ]


async def test_quiet_hours_hold_warnings_and_recoveries_never_critical(session):
    notify = Notify()
    crit, warn = _problem("disk_low"), _problem("push_failing")
    report = await health.tell(session, [crit, warn], notify, now=NIGHT)
    assert (report.said, report.held) == (("disk_low",), ("push_failing",))
    # 08:05 is past QUIET_HOURS and 8.5 h after the critical line.
    morning = datetime(2026, 10, 25, 8, 5, tzinfo=TZ)
    report = await health.tell(session, [crit, warn], notify, now=morning)
    assert report.said == ("disk_low", "push_failing")
    # Cleared in the night: the recovery waits for the morning too.
    night2 = datetime(2026, 10, 25, 22, 0, tzinfo=TZ)
    report = await health.tell(session, [], notify, now=night2)
    assert set(report.held) == {"disk_low", "push_failing"} and report.recovered == ()


async def test_the_warnings_of_one_sweep_are_one_message(session):
    notify = Notify()
    found = [_problem("push_failing"), _problem("review_backlog")]
    await health.tell(session, found, notify, now=NOW)
    assert len(notify.sent) == 1
    assert notify.sent[0].startswith("🩺 <b>Agent holati</b> — 2 ta muammo:")
    assert uz_problems(notify.sent[0], rendered=True) == set()


async def test_an_undelivered_alert_is_not_recorded_and_is_tried_again(session):
    down = Notify(delivered=False)
    crit = _problem("worker_silent")
    assert (await health.tell(session, [crit], down, now=NOW)).said == ()
    assert await _kinds(session) == []
    up = Notify()
    later = NOW + timedelta(minutes=5)
    assert (await health.tell(session, [crit], up, now=later)).said == ("worker_silent",)
    assert await _kinds(session) == [("alert:worker_silent", later)]


async def test_told_first_elsewhere_is_never_said_first_by_the_sweep(session):
    notify = Notify()
    p404 = _problem("push_404")
    assert (await health.tell(session, [p404], notify, now=NOW)).said == ()
    # crm_push_job told it at 11:00 (its own ledger row).
    health.mark_alerted(session, ["push_404"], now=NOW - timedelta(hours=1))
    await session.commit()
    assert (await health.tell(session, [p404], notify, now=NOW)).said == ()
    later = NOW + timedelta(hours=settings.alert_repeat_hours)
    assert (await health.tell(session, [p404], notify, now=later)).said == ("push_404",)
    assert notify.sent == [p404.text]


async def test_spend_alerts_are_said_once_per_tashkent_day(session):
    notify = Notify()
    high = _problem("spend_high")
    assert (await health.tell(session, [high], notify, now=NOW)).said == ("spend_high",)
    # Seven hours later is past ALERT_REPEAT_HOURS but the same business day.
    evening = NOW + timedelta(hours=7, minutes=30)
    assert evening.date() == NOW.date()
    assert (await health.tell(session, [high], notify, now=evening)).said == ()
    tomorrow = datetime(2026, 10, 25, 9, 0, tzinfo=TZ)
    assert (await health.tell(session, [high], notify, now=tomorrow)).said == (
        "spend_high",
    )


async def test_cost_cap_told_by_ask_is_not_repeated_by_the_sweep_the_same_day(
    session,
):
    """costs.ask tells cost_cap at once through the same claim; the sweep
    then repeats it once a day, never twice in one day."""
    notify = Notify()
    told = await costs.fire_spend_alerts(session, now=NOW, notify=notify)
    assert told == []  # nothing spent yet
    # ask's own path: the first refusal at the cap.
    costs.set_cap_notifier(notify)
    try:
        await costs._tell_cost_cap(costs.CAP_DAILY, NOW)
    finally:
        costs.set_cap_notifier(None)
    assert len(notify.sent) == 1
    cap = _problem("cost_cap")
    assert (await health.tell(session, [cap], notify, now=NOW)).said == ()
    tomorrow = datetime(2026, 10, 25, 10, 0, tzinfo=TZ)
    assert (await health.tell(session, [cap], notify, now=tomorrow)).said == ("cost_cap",)
    assert len(notify.sent) == 2


class Undispatched:
    """A call costs.ask must refuse before sending anything."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> int:
        self.calls += 1
        return 1


async def _ask_classify(session, now, call=None) -> None:
    await costs.ask(
        session,
        operation="classify",
        est_usd=Decimal("0.10"),
        call=call or Undispatched(),
        price=lambda _: costs.Priced(cost_usd=Decimal("0.10"), input_tokens=400),
        now=now,
    )


async def test_a_cap_ask_refuses_at_holds_until_its_day_is_over(session):
    """costs.ask refuses at spend + est ≥ cap — the spend itself stays below
    the cap — and tells cost_cap. The sweep must neither announce it
    recovered while ask still refuses, nor say it twice; the daily cap's
    recovery is the next morning's."""
    session.add(
        m.UsageLog(
            provider="anthropic",
            operation="verdict",
            model=settings.verdict_model,
            cost_usd=Decimal("9.95"),
            input_tokens=900,
            created_at=NOW - timedelta(hours=2),
        )
    )
    await session.commit()
    notify = Notify()
    costs.set_cap_notifier(notify)
    costs.clear_refusals()
    try:
        with pytest.raises(costs.CostCapReached) as refused:
            await _ask_classify(session, NOW)
        assert refused.value.code == costs.CAP_DAILY
        assert notify.sent == [
            "💸 Kunlik xarajat chegarasi ($10) — ertagacha pullik chaqiruvlar "
            "toʻxtadi; kollektorlar bepul qismini davom ettiradi"
        ]
        later = NOW + timedelta(minutes=5)
        status = await _gather(session, now=later)
        assert status.spend.day_usd == Decimal("9.95")  # below the cap …
        assert status.spend.capped == frozenset({costs.CAP_DAILY})  # … yet in force
        found = health.problems(status)
        [cap] = [p for p in found if p.key == "cost_cap"]
        assert cap.text == notify.sent[0]
        report = await health.tell(
            session, found, notify, now=later, unjudged=health.unjudged(status)
        )
        assert "cost_cap" not in report.said + report.recovered + report.held
        costs.clear_refusals()
        with pytest.raises(costs.CostCapReached):  # still refused
            await _ask_classify(session, later)
        # The next Tashkent day the cap is over: one recovery, at 08:05.
        morning = datetime(2026, 10, 25, 8, 5, tzinfo=TZ)
        status = await _gather(session, now=morning)
        assert status.spend.capped == frozenset()
        found = health.problems(status)
        assert "cost_cap" not in [p.key for p in found]
        report = await health.tell(
            session, found, notify, now=morning, unjudged=health.unjudged(status)
        )
        assert report.recovered == ("cost_cap", "spend_high")
        lines = [line for text in notify.sent for line in text.splitlines()]
        assert lines.count(health.recovery_text("cost_cap")) == 1
    finally:
        costs.set_cap_notifier(None)
        costs.clear_refusals()


async def test_a_monthly_cap_refusal_holds_all_month(session):
    session.add(
        m.UsageLog(
            provider="anthropic",
            operation="verdict",
            model=settings.verdict_model,
            cost_usd=Decimal("249.95"),
            input_tokens=900,
            created_at=datetime(2026, 10, 10, 12, 0, tzinfo=TZ),
        )
    )
    await session.commit()
    costs.clear_refusals()
    try:
        with pytest.raises(costs.CostCapReached) as refused:
            await _ask_classify(session, NOW)
        assert refused.value.code == costs.CAP_MONTHLY
        for moment in (
            NOW + timedelta(minutes=5),
            datetime(2026, 10, 31, 23, 55, tzinfo=TZ),
        ):
            status = await _gather(session, now=moment)
            [cap] = [p for p in health.problems(status) if p.key == "cost_cap"]
            assert cap.text.startswith("💸 Oylik xarajat chegarasi ($250)")
        november = datetime(2026, 11, 1, 9, 0, tzinfo=TZ)
        status = await _gather(session, now=november)
        assert "cost_cap" not in [p.key for p in health.problems(status)]
    finally:
        costs.clear_refusals()


async def test_ask_holds_its_cost_cap_line_inside_quiet_hours(session):
    """cost_cap is a warning: only critical problems ignore QUIET_HOURS. The
    notifier the worker wires holds it at 23:30 (the batch submit's minute)
    without claiming it, and says it at noon."""
    from clientscrapper.worker import main as worker

    notify = Notify()
    worker.set_owner_notifier(notify)
    try:
        await costs._tell_cost_cap(costs.CAP_DAILY, NIGHT)
        assert notify.sent == []
        assert await _kinds(session) == []
        await costs._tell_cost_cap(costs.CAP_DAILY, NOW)
        assert len(notify.sent) == 1 and notify.sent[0].startswith("💸 Kunlik")
        assert [k for k, _ in await _kinds(session)] == ["alert:cost_cap"]
    finally:
        worker.set_owner_notifier(None)


def _low_precision(*sources: tuple[str, str, int]) -> health.Problem:
    status = healthy_status()
    _reported(
        "source_low_precision",
        sources=[{"key": k, "title": t, "pct": pct} for k, t, pct in sources],
    )(status)
    [problem] = health.problems(status)
    return problem


async def test_low_precision_is_said_once_per_source_per_week(session):
    """06 §4.2: a second source paused two days after the first is said at
    once, and only it; each source is repeated after its own week."""
    notify = Notify()
    olx = ("olx_business", "OLX sotuvchilari", 14)
    hh = ("hh_vacancies", "hh.uz vakansiyalari", 11)
    monday = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)
    assert (
        await health.tell(session, [_low_precision(olx)], notify, now=monday)
    ).said == ("source_low_precision",)
    wednesday = monday + timedelta(days=2)
    both = _low_precision(olx, hh)
    assert both.text.count("\n") == 1  # /holat shows both
    report = await health.tell(session, [both], notify, now=wednesday)
    assert report.said == ("source_low_precision",)
    assert notify.sent[-1] == (
        "⚠️ hh.uz vakansiyalari aniqligi 11% — bir haftaga toʻxtatildi "
        "(/manbalar bilan yoqiladi)"
    )
    thursday = wednesday + timedelta(days=1)
    assert (await health.tell(session, [both], notify, now=thursday)).said == ()
    next_tuesday = monday + timedelta(days=8)
    await health.tell(session, [both], notify, now=next_tuesday)
    assert notify.sent[-1].startswith("⚠️ OLX sotuvchilari aniqligi 14%")
    assert "hh.uz" not in notify.sent[-1]
    assert len(notify.sent) == 3
    refs = await session.scalars(sa.select(m.ReminderLog.ref).order_by(m.ReminderLog.id))
    assert list(refs) == [
        "source_low_precision:olx_business",
        "source_low_precision:hh_vacancies",
        "source_low_precision:olx_business",
    ]
    # Over for both: one recovery.
    report = await health.tell(session, [], notify, now=next_tuesday + timedelta(hours=1))
    assert report.recovered == ("source_low_precision",)


async def test_two_speakers_at_once_say_a_line_once(session):
    """The worker's sweep and the bot's watchdog run the same tell at the
    same moment: the claim lets one of them speak."""
    notify = Notify()
    crit = _problem("disk_low")

    async def speaker() -> tuple[str, ...]:
        async with SessionLocal() as own:
            return (await health.tell(own, [crit], notify, now=NOW)).said

    results = await asyncio.gather(*(speaker() for _ in range(6)))
    assert sorted(results) == [(), (), (), (), (), ("disk_low",)]
    assert notify.sent == [crit.text]


async def test_unjudged_keys_are_not_announced_as_recovered(session):
    notify = Notify()
    await health.tell(session, [_problem("collector_silent")], notify, now=NOW)
    later = NOW + timedelta(minutes=5)
    report = await health.tell(
        session, [], notify, now=later, unjudged={"collector_silent"}
    )
    assert report.recovered == ()
    report = await health.tell(session, [], notify, now=later)
    assert report.recovered == ("collector_silent",)


async def test_offline_alerts_are_critical_only_and_deduped_in_memory():
    notify = Notify()
    memo: dict[str, datetime] = {}
    found = [_problem("db_down"), _problem("review_backlog")]
    assert await health.tell_offline(found, notify, now=NOW, memo=memo) == ["db_down"]
    soon = NOW + timedelta(hours=1)
    assert await health.tell_offline(found, notify, now=soon, memo=memo) == []
    later = NOW + timedelta(hours=settings.alert_repeat_hours, minutes=1)
    assert await health.tell_offline(found, notify, now=later, memo=memo) == ["db_down"]


# --- the Anthropic switch costs.ask reads ------------------------------------------


async def _silent_anthropic(session):
    session.add_all(
        [
            m.UsageLog(
                provider="anthropic",
                operation="classify",
                model=settings.extract_model,
                cost_usd=Decimal("0.001"),
                input_tokens=500,
                created_at=NOW - timedelta(hours=4),
            ),
            m.UsageLog(
                provider="anthropic",
                operation="classify",
                model="failed:ConnectionError",
                cost_usd=Decimal("0.001"),
                created_at=NOW - timedelta(minutes=20),
            ),
            m.ModelJob(kind="classify", lane="instant", model="m", custom_id="cs-p-1"),
        ]
    )
    await session.commit()


async def test_the_switch_lets_one_probe_through_per_interval(session):
    await _silent_anthropic(session)
    assert await health.anthropic_down(session, NOW) is False  # the probe
    for seconds in (1, 61, 600):
        moment = NOW + timedelta(seconds=seconds)
        assert await health.anthropic_down(session, moment) is True
    after = NOW + health.ANTHROPIC_PROBE_INTERVAL
    assert await health.anthropic_down(session, after) is False  # the next probe
    assert await health.anthropic_down(session, after + timedelta(seconds=1)) is True


async def test_a_successful_probe_ends_the_silence(session):
    await _silent_anthropic(session)
    assert await health.anthropic_down(session, NOW) is False
    session.add(
        m.UsageLog(
            provider="anthropic",
            operation="classify",
            model=settings.extract_model,
            cost_usd=Decimal("0.001"),
            input_tokens=480,
            created_at=NOW + timedelta(seconds=2),
        )
    )
    await session.commit()
    later = NOW + timedelta(seconds=30)
    assert await health.anthropic_down(session, later) is False
    status = await _gather(session, now=later)
    assert "anthropic_silence" not in [p.key for p in health.problems(status)]


async def test_concurrent_askers_share_one_probe(session):
    await _silent_anthropic(session)

    async def ask_switch() -> bool:
        async with SessionLocal() as own:
            return await health.anthropic_down(own, NOW)

    health.reset_anthropic_cache()
    answers = await asyncio.gather(*(ask_switch() for _ in range(8)))
    assert sorted(answers) == [False] + [True] * 7


async def test_a_call_in_flight_is_not_a_silent_api(session):
    """ask commits its claim before it calls; while the answer is on its way
    another process's sweep must not count that claim as a failed attempt."""
    session.add_all(
        [
            m.UsageLog(
                provider="anthropic",
                operation="classify",
                model=settings.extract_model,
                cost_usd=Decimal("0.001"),
                input_tokens=500,
                created_at=NOW - timedelta(hours=4),
            ),
            m.ModelJob(kind="classify", lane="instant", model="m", custom_id="cs-f-1"),
        ]
    )
    await session.commit()
    seen: dict[str, object] = {}

    async def answering() -> int:
        async with SessionLocal() as other:
            status = await _gather(other, now=NOW + timedelta(seconds=2))
            seen["facts"] = status.anthropic
            seen["keys"] = [p.key for p in health.problems(status)]
        return 1

    assert (
        await costs.ask(
            session,
            operation="classify",
            est_usd=Decimal("0.001"),
            call=answering,
            price=lambda _: costs.Priced(cost_usd=Decimal("0.001"), input_tokens=480),
            now=NOW,
        )
        == 1
    )
    assert seen["facts"] == AnthropicFacts(NOW - timedelta(hours=4), None, 1)
    assert "anthropic_silence" not in seen["keys"]  # type: ignore[operator]


async def test_a_call_that_raised_is_the_failed_attempt_silence_counts(session):
    session.add_all(
        [
            m.UsageLog(
                provider="anthropic",
                operation="classify",
                model=settings.extract_model,
                cost_usd=Decimal("0.001"),
                input_tokens=500,
                created_at=NOW - timedelta(hours=4),
            ),
            m.ModelJob(kind="classify", lane="instant", model="m", custom_id="cs-f-2"),
        ]
    )
    await session.commit()

    async def failing() -> int:
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await costs.ask(
            session,
            operation="classify",
            est_usd=Decimal("0.001"),
            call=failing,
            price=lambda _: costs.Priced(cost_usd=Decimal("0.001"), input_tokens=480),
            now=NOW,
        )
    status = await _gather(session, now=NOW + timedelta(minutes=5))
    assert status.anthropic == AnthropicFacts(NOW - timedelta(hours=4), NOW, 1)
    assert "anthropic_silence" in [p.key for p in health.problems(status)]


async def test_a_refused_op_takes_the_next_probe_slot(session):
    """A silence refusal is remembered until the next probe slot, not to the
    hour: an op refused at 12:02 gets the slot that frees at 12:31."""
    await _silent_anthropic(session)
    costs.set_anthropic_health(health.anthropic_down_until)
    costs.clear_refusals()
    at = NOW + timedelta(minutes=1)
    calls: list[str] = []

    async def failing() -> int:
        calls.append("fail")
        raise ConnectionError("down")

    async def answering() -> int:
        calls.append("ok")
        return 1

    def price(_: int) -> costs.Priced:
        return costs.Priced(cost_usd=Decimal("0.0005"), input_tokens=400)

    async def ask(call, moment):
        return await costs.ask(
            session,
            operation="classify",
            est_usd=Decimal("0.0005"),
            call=call,
            price=price,
            now=moment,
        )

    try:
        with pytest.raises(ConnectionError):
            await ask(failing, at)  # the 12:01 probe
        with pytest.raises(costs.CostCapReached) as refused:
            await ask(answering, at + timedelta(minutes=1))
        assert refused.value.code == costs.ANTHROPIC_SILENCE
        frees = at + health.ANTHROPIC_PROBE_INTERVAL  # 12:31
        assert costs.refused_until("classify", at + timedelta(minutes=2)) == frees
        assert await ask(answering, at + timedelta(minutes=34)) == 1  # 12:35
        assert calls == ["fail", "ok"]
    finally:
        costs.set_anthropic_health(None)
        costs.clear_refusals()


async def test_the_switch_is_the_one_costs_ask_reads(session):
    """With the hook registered, the probe call goes through; when it fails
    too, the next Anthropic call in the silence is refused with the
    anthropic_silence code and never dispatched."""
    await _silent_anthropic(session)
    costs.set_anthropic_health(health.anthropic_down_until)
    costs.clear_refusals()
    calls: list[int] = []

    async def failing() -> int:
        calls.append(1)
        raise ConnectionError("down")

    def price(_: int) -> costs.Priced:
        return costs.Priced(cost_usd=Decimal("0.0005"), input_tokens=400)

    try:
        with pytest.raises(ConnectionError):
            await costs.ask(
                session,
                operation="classify",
                est_usd=Decimal("0.0005"),
                call=failing,
                price=price,
                now=NOW,
            )
        assert calls == [1]  # the probe went through
        costs.clear_refusals()
        with pytest.raises(costs.CostCapReached) as refused:
            await costs.ask(
                session,
                operation="classify",
                est_usd=Decimal("0.0005"),
                call=failing,
                price=price,
                now=NOW + timedelta(minutes=1),
            )
        assert refused.value.code == costs.ANTHROPIC_SILENCE
        assert calls == [1]
    finally:
        costs.set_anthropic_health(None)
        costs.clear_refusals()


# --- the fence: PROBLEM_KEYS is the one list ---------------------------------------

RAISERS = {"raise_problem", "clear_problem"}


def raised_keys(tree: ast.Module) -> set[str]:
    """Problem keys a module raises: the literal key of raise_problem /
    clear_problem, a module constant named ``*_PROBLEM`` or ``*_PROBLEM_KEY``
    (and costs' ``*_KEY`` alert constants), and a ``"problem": "<key>"``
    entry of a dict literal (a heartbeat detail naming its problem)."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for target in targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = value.value
    found: set[str] = set()
    for name, value in consts.items():
        if name.endswith(("_PROBLEM", "_PROBLEM_KEY")) or name in {
            "COST_CAP_KEY",
            "SPEND_HIGH_KEY",
        }:
            found.add(value)

    def literal(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            )
            args = [*node.args[1:2], *(k.value for k in node.keywords if k.arg == "key")]
            if name in RAISERS and args and (key := literal(args[0])) is not None:
                found.add(key)
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(k, ast.Constant)
                    and k.value == "problem"
                    and v is not None
                    and (key := literal(v)) is not None
                ):
                    found.add(key)
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].slice, ast.Constant)
            and node.targets[0].slice.value == "problem"
            and (key := literal(node.value)) is not None
        ):
            found.add(key)
    return found


def test_every_key_raised_in_the_package_is_a_row_of_06():
    raised = {rel(p): raised_keys(tree_of(p)) for p in modules()}
    unknown = {
        path: sorted(keys - set(health.PROBLEM_KEYS))
        for path, keys in raised.items()
        if keys - set(health.PROBLEM_KEYS)
    }
    assert unknown == {}
    # The scan sees today's raisers: the Fetcher's breaker and the spend alerts.
    assert "collector_blocked" in raised["services/http.py"]
    assert {"cost_cap", "spend_high"} <= raised["services/costs.py"]


def test_the_raised_key_scan_catches_each_shape():
    source = (
        "FEED_PROBLEM = 'feed_kaput'\n"
        "async def f(s, now):\n"
        "    await health.raise_problem(s, 'pii_leakk', now=now, by='x')\n"
        "    await clear_problem(s, FEED_PROBLEM)\n"
        "    detail = {'problem': 'blocked_hosts'}\n"
        "    detail['problem'] = 'not_a_key'\n"
    )
    assert raised_keys(ast.parse(source)) == {
        "feed_kaput",
        "pii_leakk",
        "blocked_hosts",
        "not_a_key",
    }
    assert (PACKAGE / "services" / "health.py").is_file()
