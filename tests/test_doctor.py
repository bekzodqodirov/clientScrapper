"""The preflight (06 §3.3): what it calls an error, and what it lets pass.

No database: the probe's answer and the server facts are handed in.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

from clientscrapper.config import Settings
from clientscrapper.services import usage
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


# --- T-06: model prices and the allocations table (04 AC 13, 07 §1.9) -----------


def _infos(findings: list[doctor.Finding]) -> list[str]:
    return [f.text for f in findings if f.level == "info"]


def test_each_configured_model_id_is_printed_with_its_price():
    infos = _infos(_check(_filled()))
    priced = [line for line in infos if line.startswith("✅ ") and "MODEL=" in line]
    assert [line.split(":")[0] for line in priced] == [
        f"✅ EXTRACT_MODEL={_filled()['EXTRACT_MODEL']}",
        f"✅ VERDICT_MODEL={_filled()['VERDICT_MODEL']}",
        f"✅ BATCH_MODEL={_filled()['BATCH_MODEL']}",
    ]
    assert "kirish $0.10 · chiqish $0.50 / 1 mln token" in priced[0]
    assert "kirish $2.00 · chiqish $10.00" in priced[1]


def test_an_unpriced_extract_model_makes_the_doctor_red():
    env = _filled()
    env["EXTRACT_MODEL"] = "model-nobody-priced"
    findings = _check(env)
    [error] = _errors(findings)
    assert error.startswith("❌ EXTRACT_MODEL=model-nobody-priced: narxi nomaʼlum")
    assert "EXTRACT_MODEL_PRICE" in error
    assert doctor.exit_code(findings) == 1


def test_a_price_override_prices_an_unknown_model_and_says_so():
    env = _filled()
    env.update(EXTRACT_MODEL="model-nobody-priced", EXTRACT_MODEL_PRICE="0.20/1.00")
    findings = _check(env)
    assert _errors(findings) == []
    priced = "✅ EXTRACT_MODEL=model-nobody-priced: kirish $0.20 · chiqish $1.00"
    assert any(
        line.startswith(priced) and "EXTRACT_MODEL_PRICE" in line
        for line in _infos(findings)
    )


def test_an_unreadable_price_override_or_a_blank_model_is_an_error():
    env = _filled()
    env.update(VERDICT_MODEL_PRICE="2,10", BATCH_MODEL="")
    errors = _errors(_check(env))
    assert any(e.startswith("❌ VERDICT_MODEL_PRICE oʻqilmadi") for e in errors)
    assert any(e.startswith("❌ BATCH_MODEL boʻsh") for e in errors)


def test_a_missing_model_key_falls_back_to_the_shipped_default():
    env = _filled()
    del env["BATCH_MODEL"]
    assert not any("BATCH_MODEL=" in e for e in _errors(_check(env)))


def test_the_allocations_table_is_printed():
    assert doctor.allocations_table() in _infos(_check(_filled()))
    assert doctor.allocations_table().splitlines() == [
        "ℹ️ Kunlik byudjet taqsimoti (USD, Toshkent kuni) — jami $10.00:",
        "   anthropic $5.00: classify 0.30 · verdict 1.50 · company_verdict 1.50 · "
        "draft 0.20 · enrich_search 0.75 · model_reserve 0.75",
        "   serp $1.00: serp_brave 0.165 · serp_yandex_night 0.00 · serp_searxng 0.00 · "
        "serp_ddg 0.00 · serp_reserve 0.835",
        "   places $1.00: places_text_ids 0.00 · places_details_contact 1.00 · "
        "twogis_api 0.00",
        "   apify $2.00: apify_olx 0.60 · apify_2gis 0.40 · apify_instagram 0.40 · "
        "apify_uzum 0.60",
        "   proxy $1.00: proxy_uzum 1.00",
    ]


def test_a_zero_price_is_an_error_because_every_call_would_be_refused():
    env = _filled()
    env["EXTRACT_MODEL_PRICE"] = "0/0"
    [error] = _errors(_check(env))
    assert error.startswith(
        f"❌ EXTRACT_MODEL={env['EXTRACT_MODEL']}: narxi $0 (EXTRACT_MODEL_PRICE)"
    )
    env["EXTRACT_MODEL_PRICE"] = "0/0.50"  # only one half free: still priced
    assert _errors(_check(env)) == []


def test_a_stray_space_in_a_model_id_reads_alike_in_the_doctor_and_at_runtime(
    monkeypatch,
):
    env = _filled()
    env["EXTRACT_MODEL"] = f"{env['EXTRACT_MODEL']} "
    assert _errors(_check(env)) == []
    monkeypatch.setenv("EXTRACT_MODEL", env["EXTRACT_MODEL"])
    runtime = Settings(_env_file=None)  # type: ignore[call-arg]
    assert usage.resolve_price(runtime.extract_model) is not None
