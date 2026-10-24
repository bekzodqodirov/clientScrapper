"""The preflight (06 §3.3): what it calls an error, and what it lets pass.

No database: the probe's answer and the server facts are handed in.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

from clientscrapper.tools import doctor
from clientscrapper.tools.doctor import DbState

ROOT = Path(__file__).resolve().parents[1]
MEMINFO_8GB = "MemTotal:        8000000 kB\nSwapTotal:       2097148 kB\n"
DISK_100GB = 100 * 1000**3


def _example() -> dict[str, str]:
    return {k: v or "" for k, v in dotenv_values(ROOT / ".env.example").items()}


def _filled() -> dict[str, str]:
    """The example with the owner's required values typed in (synthetic)."""
    env = _example()
    env.update(
        POSTGRES_PASSWORD="0f1e2d3c4b5a",
        DATABASE_URL="postgresql+psycopg://clientscrapper:0f1e2d3c4b5a@db:5432/clientscrapper",
        ANTHROPIC_API_KEY="placeholder-not-a-key",
        ASSISTANT_BOT_TOKEN="placeholder-not-a-token",
        OWNER_TELEGRAM_ID="111111111",
        TELETHON_API_ID="1000001",
        TELETHON_API_HASH="placeholder-not-a-hash",
        TELETHON_SESSION="placeholder-not-a-session",
        BACKUP_AGE_RECIPIENT="age1placeholder",
    )
    return env


def _check(env: dict[str, str], **kw: object) -> list[doctor.Finding]:
    defaults: dict[str, object] = {"db": DbState(reachable=True, version="0001_initial")}
    defaults.update(kw)
    return doctor.check(env, MEMINFO_8GB, DISK_100GB, True, **defaults)  # type: ignore[arg-type]


def _errors(findings: list[doctor.Finding]) -> list[str]:
    return [f.text for f in findings if f.level == "error"]


def test_a_fresh_copy_of_the_example_is_red_on_exactly_the_required_values():
    errors = " ".join(_errors(_check(_example())))
    for key in (
        "POSTGRES_PASSWORD",
        "ANTHROPIC_API_KEY",
        "ASSISTANT_BOT_TOKEN",
        "OWNER_TELEGRAM_ID",
        "TELETHON_API_ID",
        "TELETHON_API_HASH",
    ):
        assert key in errors
    assert "CHANGE_ME" in errors


def test_filled_required_values_with_blank_optional_credentials_are_green():
    findings = _check(_filled())
    assert _errors(findings) == []
    assert doctor.exit_code(findings) == 0
    text = " ".join(f.text for f in findings)
    optional = (
        "BRAVE_API_KEY",
        "APIFY_TOKEN",
        "SEARXNG_URL",
        "PROXY_URL",
        "HH_APP_TOKEN",
    )
    assert [key for key in optional if key in text] == []
    assert doctor.summary(findings) == "✅ Hammasi joyida — endi: make up"


def test_the_reader_credentials_are_not_required_with_the_reader_off():
    env = _filled()
    env.update(USERBOT_ENABLED="false", TELETHON_API_ID="", TELETHON_API_HASH="")
    assert _errors(_check(env)) == []


def test_the_erp_key_is_required_once_pushing_is_on():
    env = _filled()
    env.update(CRM_PUSH_ENABLED="true")
    assert any("CRM_AGENT_KEY" in e for e in _errors(_check(env)))


def test_a_password_that_differs_from_the_url_is_an_error():
    env = _filled()
    env.update(POSTGRES_PASSWORD="another")
    assert any("DATABASE_URL" in e for e in _errors(_check(env)))


def test_a_comment_shaped_value_is_an_error():
    env = _filled()
    env.update(OWNER_TELEGRAM_ID="# @userinfobot")
    assert any("OWNER_TELEGRAM_ID" in e and "izoh" in e for e in _errors(_check(env)))


def test_manager_ids_must_be_numbers():
    env = _filled()
    env.update(MANAGER_TELEGRAM_IDS="111111111,namuna_01")
    assert any("namuna_01" in e for e in _errors(_check(env)))


def test_the_digest_must_fall_inside_review_hours():
    env = _filled()
    env.update(DIGEST_TIME="20:00")
    assert any("DIGEST_TIME" in e for e in _errors(_check(env)))


def test_a_database_that_is_not_up_yet_only_warns():
    # make up runs the doctor before it starts the database.
    findings = _check(_filled(), db=DbState(reachable=False, error="OperationalError"))
    assert _errors(findings) == []
    assert any(f.level == "warn" and "OperationalError" in f.text for f in findings)


def test_small_ram_is_an_error_and_missing_tools_warn():
    meminfo = "MemTotal:        2000000 kB\nSwapTotal:       0 kB\n"
    findings = doctor.check(
        _filled(), meminfo, DISK_100GB, True, binaries={"age": False, "pg_dump": True}
    )
    assert any("RAM" in e for e in _errors(findings))
    warnings = [f.text for f in findings if f.level == "warn"]
    assert any("Swap" in w for w in warnings)
    assert any(w.startswith("⚠️ age ") for w in warnings)
    assert not any("pg_dump" in w for w in warnings)


def test_missing_keys_after_an_update_are_one_warning_line():
    env = _filled()
    for key in ("PUSH_DAYS", "PUSH_WINDOW", "REVIEW_BURST_MAX", "DEADMAN_PING_URL"):
        del env[key]
    warnings = [f.text for f in _check(env) if f.level == "warn"]
    assert len([w for w in warnings if "yangi sozlama" in w]) == 1


def test_errors_come_first():
    levels = [f.level for f in _check(_example())]
    assert levels == sorted(levels, key={"error": 0, "warn": 1, "info": 2}.__getitem__)
