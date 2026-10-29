"""The preflight (06 §3.3): what it calls an error, and what it lets pass.

No database: the probe's answer and the server facts are handed in.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
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


def test_the_digest_must_fall_outside_quiet_hours():
    """06 §1.6 «inside REVIEW_HOURS and outside QUIET_HOURS» (seam review
    wave 4: a 07:30 digest with REVIEW_HOURS 07:00-18:00 passed every check
    and went out in the owner's night); the Settings validator says the same
    at start."""
    env = _filled()
    env.update(DIGEST_TIME="07:30", REVIEW_HOURS="07:00-18:00", QUIET_HOURS="21:00-08:00")
    assert any("QUIET_HOURS" in e for e in _errors(_check(env)))
    env.update(QUIET_HOURS="21:00-07:00")
    assert not any("DIGEST_TIME" in e for e in _errors(_check(env)))
    with pytest.raises(ValueError, match="QUIET_HOURS"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            digest_time="07:30",
            review_hours="07:00-18:00",
            quiet_hours="21:00-08:00",
        )
    assert (
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            digest_time="07:30",
            review_hours="07:00-18:00",
            quiet_hours="21:00-07:00",
        ).digest_time_parsed.hour
        == 7
    )


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


# --- T-08: Telegram, ERP and salt_id (06 §3.3, 04 AC 13) --------------------------
#
# check() takes each probe's answer; the probes themselves run against fakes —
# a recording Bot stand-in and httpx.MockTransport. No test reaches a network.

KEY = "test-secret"  # 07 §1.0's agent-side vector: salt_id("test-secret") == "9caf06bb"
KEY_SALT = "9caf06bb"
OWNER = "111111111"
MANAGERS = ("222222222", "333333333")


def _telegram_env() -> dict[str, str]:
    env = _filled()
    env.update(MANAGER_TELEGRAM_IDS=",".join(MANAGERS))
    return env


def _crm_env(**change: str) -> dict[str, str]:
    env = _filled()
    env.update(
        CRM_AGENT_KEY=KEY,
        CRM_PUSH_SOURCE="agent",
        CRM_BASE_URL="https://gsr-namuna.example.uz",
    )
    env.update(change)
    return env


def _levels(findings: list[doctor.Finding], needle: str) -> list[tuple[str, str]]:
    return [(f.level, f.text) for f in findings if needle in f.text]


def test_nothing_is_reported_as_not_checked_yet_any_more():
    findings = _check(_filled())
    assert [f.text for f in findings if "hali tekshirilmaydi" in f.text] == []


def test_the_ids_the_bot_must_reach_are_the_owner_then_each_manager():
    env = _telegram_env()
    env["MANAGER_TELEGRAM_IDS"] = f"{MANAGERS[0]}, namuna_01 ,{OWNER},{MANAGERS[1]}"
    assert doctor.telegram_ids(env) == (OWNER, MANAGERS[0], MANAGERS[1])
    env["OWNER_TELEGRAM_ID"] = ""
    assert doctor.telegram_ids(env) == (MANAGERS[0], OWNER, MANAGERS[1])


def test_a_bot_that_reaches_everybody_is_green():
    state = doctor.TelegramState(
        "ok",
        username="namuna_bot",
        chats={OWNER: "ok", MANAGERS[0]: "ok", MANAGERS[1]: "ok"},
    )
    findings = _check(_telegram_env(), telegram=state)
    assert _errors(findings) == []
    infos = _infos(findings)
    assert "✅ Bot: @namuna_bot (get_me javob berdi)." in infos
    assert f"✅ Egasi {OWNER}: bot unga yoza oladi (get_chat)." in infos
    assert f"✅ Menejer {MANAGERS[1]}: bot unga yoza oladi (get_chat)." in infos


def test_a_refused_token_is_an_error_and_an_unreachable_telegram_a_warning():
    refused = doctor.TelegramState("refused", error="TelegramUnauthorizedError")
    findings = _check(_telegram_env(), telegram=refused)
    assert _errors(findings) == [
        "❌ ASSISTANT_BOT_TOKEN ni Telegram rad etdi (TelegramUnauthorizedError) — "
        "@BotFather → /mybots → API Token; .env ga qaytadan koʻchir."
    ]
    assert doctor.exit_code(findings) == 1
    away = doctor.TelegramState("unreachable", error="TelegramNetworkError")
    findings = _check(_telegram_env(), telegram=away)
    assert _errors(findings) == []
    assert _levels(findings, "TelegramNetworkError")[0][0] == "warn"


def test_an_owner_the_bot_cannot_see_is_an_error_a_manager_a_warning():
    state = doctor.TelegramState(
        "ok",
        username="namuna_bot",
        chats={OWNER: "not_found", MANAGERS[0]: "not_found", MANAGERS[1]: "unreachable"},
        chat_errors={
            OWNER: "TelegramBadRequest",
            MANAGERS[0]: "TelegramForbiddenError",
            MANAGERS[1]: "TimeoutError",
        },
    )
    findings = _check(_telegram_env(), telegram=state)
    [error] = _errors(findings)
    assert error.startswith(
        f"❌ OWNER_TELEGRAM_ID={OWNER}: bot bu chatni topmadi (TelegramBadRequest) — "
        "botga /start yoz"
    )
    assert _levels(findings, MANAGERS[0]) == [
        (
            "warn",
            f"⚠️ Menejer {MANAGERS[0]}: bot bu chatni topmadi (TelegramForbiddenError) — "
            "menejer botga /start yozsin, aks holda unga karta yetib bormaydi.",
        )
    ]
    assert _levels(findings, MANAGERS[1]) == [
        ("warn", f"⚠️ Menejer {MANAGERS[1]}: tekshirib boʻlmadi (TimeoutError).")
    ]


# The probe against a stand-in Bot: aiogram's exception classes by name.


class TelegramUnauthorizedError(Exception):
    pass


class TelegramBadRequest(Exception):
    pass


class TelegramNetworkError(Exception):
    pass


class TokenValidationError(Exception):
    pass


class _Me:
    username = "namuna_bot"


class FakeBot:
    """Records every call; get_chat answers per id (an exception class or ok)."""

    def __init__(self, token: str, *, me: object = None, chats: dict | None = None):
        self.token = token
        self.calls: list[tuple[str, object]] = []
        self.me = me if me is not None else _Me()
        self.chats = chats or {}
        self.closed = False

    async def get_me(self):
        self.calls.append(("get_me", None))
        if isinstance(self.me, BaseException):
            raise self.me
        if self.me == "hang":
            await asyncio.sleep(10)
        return self.me

    async def get_chat(self, chat_id):
        self.calls.append(("get_chat", chat_id))
        answer = self.chats.get(chat_id)
        if isinstance(answer, BaseException):
            raise answer
        return object()

    async def close(self):
        self.closed = True


def _probe(bot: FakeBot, ids=(OWNER, *MANAGERS), **kw):
    made: list[str] = []

    def make(token: str) -> FakeBot:
        made.append(token)
        return bot

    state = asyncio.run(
        doctor.probe_telegram("placeholder-not-a-token", ids, make_bot=make, **kw)
    )
    return state, made


def test_the_probe_asks_get_me_then_get_chat_per_id_and_closes():
    bot = FakeBot("x", chats={int(MANAGERS[0]): TelegramBadRequest("chat not found")})
    state, made = _probe(bot)
    assert made == ["placeholder-not-a-token"]
    assert bot.calls == [
        ("get_me", None),
        ("get_chat", int(OWNER)),
        ("get_chat", int(MANAGERS[0])),
        ("get_chat", int(MANAGERS[1])),
    ]
    assert state == doctor.TelegramState(
        "ok",
        username="namuna_bot",
        chats={OWNER: "ok", MANAGERS[0]: "not_found", MANAGERS[1]: "ok"},
        chat_errors={MANAGERS[0]: "TelegramBadRequest"},
    )
    assert bot.closed


def test_the_probe_tells_a_refused_token_from_an_unreachable_telegram():
    refused, _ = _probe(FakeBot("x", me=TelegramUnauthorizedError("Unauthorized")))
    assert (refused.token, refused.error, refused.chats) == (
        "refused",
        "TelegramUnauthorizedError",
        {},
    )
    away, _ = _probe(FakeBot("x", me=TelegramNetworkError("no route")))
    assert (away.token, away.error) == ("unreachable", "TelegramNetworkError")
    hung = FakeBot("x", me="hang")
    late, _ = _probe(hung, timeout=0.01)
    assert (late.token, late.error) == ("unreachable", "TimeoutError")
    assert hung.closed

    def bad_shape(token: str) -> FakeBot:
        raise TokenValidationError("Token is invalid!")

    state = asyncio.run(doctor.probe_telegram("x", (OWNER,), make_bot=bad_shape))
    assert (state.token, state.error) == ("refused", "TokenValidationError")


def test_a_malformed_token_is_refused_by_aiogram_itself_without_a_request():
    # The real Bot construction validates the shape before any session opens.
    state = asyncio.run(doctor.probe_telegram("not-a-token", (OWNER,)))
    assert (state.token, state.error) == ("refused", "TokenValidationError")


# The ERP probe against httpx.MockTransport.


def _erp(handler) -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return seen, httpx.MockTransport(record)


def _page(salt: str = KEY_SALT) -> dict[str, object]:
    return {
        "mode": "full",
        "generated_at": "2026-10-24T08:56:38.538Z",
        "salt_id": salt,
        "rule": "hmac-sha256(secret, last9digits) hex[0:20]",
        "entries": [],
        "next": None,
        "resume": None,
    }


def test_the_erp_probe_is_one_keyed_get_of_one_entry():
    seen, transport = _erp(lambda r: httpx.Response(200, json=_page()))
    state = asyncio.run(
        doctor.probe_crm("https://gsr-namuna.example.uz/", KEY, transport=transport)
    )
    assert state == doctor.CrmState(200, salt_id=KEY_SALT)
    [request] = seen
    assert request.method == "GET"
    assert (
        str(request.url) == "https://gsr-namuna.example.uz/api/agent/suppression?limit=1"
    )
    assert request.headers["X-GSR-Key"] == KEY
    assert request.headers["X-ERP-Key"] == KEY
    assert request.headers["User-Agent"] == doctor.DOCTOR_USER_AGENT
    assert KEY not in str(request.url)


def test_the_erp_probe_never_follows_a_redirect_and_names_each_answer():
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            308, headers={"Location": "https://gsr-namuna.example.uz/login"}
        )

    seen, transport = _erp(redirect)
    state = asyncio.run(
        doctor.probe_crm("https://gsr-namuna.example.uz", KEY, transport=transport)
    )
    assert (state, len(seen)) == (doctor.CrmState(308), 1)
    for response, expected in (
        (httpx.Response(404, text="Not found"), doctor.CrmState(404)),
        (httpx.Response(503, text=""), doctor.CrmState(503)),
        (
            httpx.Response(200, text="<html>Kirish</html>"),
            doctor.CrmState(200, shape="not_json"),
        ),
        (httpx.Response(200, json={"rows": []}), doctor.CrmState(200, shape="no_salt")),
        (httpx.Response(200, json=_page("XYZ")), doctor.CrmState(200, shape="no_salt")),
    ):
        _, transport = _erp(lambda r, response=response: response)
        got = asyncio.run(
            doctor.probe_crm("https://gsr-namuna.example.uz", KEY, transport=transport)
        )
        assert got == expected, response


def test_the_erp_probe_survives_a_dead_host_and_refuses_plain_http():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _, transport = _erp(down)
    state = asyncio.run(
        doctor.probe_crm("https://gsr-namuna.example.uz", KEY, transport=transport)
    )
    assert state == doctor.CrmState(None, error="ConnectError")
    # The key never travels in clear: no request at all to an http:// base.
    seen, transport = _erp(lambda r: httpx.Response(200, json=_page()))
    state = asyncio.run(
        doctor.probe_crm("http://gsr-namuna.example.uz", KEY, transport=transport)
    )
    assert (state, seen) == (doctor.CrmState(None, error="not_https"), [])


def test_crm_base_url_must_be_https_whatever_the_feed_says():
    for env in (
        _crm_env(CRM_BASE_URL="http://gsr-namuna.example.uz"),
        _filled() | {"CRM_BASE_URL": "http://gsr-namuna.example.uz"},
    ):
        assert _errors(_check(env)) == [
            "❌ CRM_BASE_URL https:// bilan boshlanishi kerak — http:// soʻroviga "
            "ERP 308 qaytaradi, agent unga ergashmaydi (erp_redirect)."
        ]


def test_during_the_fallback_no_feed_is_read_and_the_doctor_says_why():
    env = _crm_env(CRM_PUSH_SOURCE="other")
    assert not doctor.feeds_configured(env)
    findings = _check(env)
    assert _errors(findings) == []
    assert any("«Boshqa» kaliti" in line and "lid" in line for line in _infos(findings))
    assert [line for line in _infos(findings) if "salt_id" in line] == []
    blank = _crm_env(CRM_AGENT_KEY="")
    assert not doctor.feeds_configured(blank)
    assert "ℹ️ ERP: CRM_AGENT_KEY boʻsh — ERP bilan bogʻlanish tekshirilmadi." in _infos(
        _check(blank)
    )


def test_each_erp_answer_has_its_level():
    def level(state: doctor.CrmState, **change: str) -> list[str]:
        findings = _check(
            _crm_env(**change),
            crm=state,
            db=DbState(True, "0001_initial", salt_id=KEY_SALT),
        )
        return [f.level for f in findings if "ERP" in f.text and "salt_id" not in f.text]

    assert level(doctor.CrmState(200, salt_id=KEY_SALT)) == ["info"]
    assert level(doctor.CrmState(404)) == ["warn"]
    assert level(doctor.CrmState(404), CRM_PUSH_ENABLED="true") == ["error"]
    assert level(doctor.CrmState(308)) == ["error"]
    assert level(doctor.CrmState(503)) == ["warn"]
    assert level(doctor.CrmState(None, error="ConnectError")) == ["warn"]
    assert level(doctor.CrmState(200, shape="not_json")) == ["error"]
    findings = _check(_crm_env(), crm=doctor.CrmState(404))
    assert _levels(findings, "404")[0][1] == (
        "⚠️ ERP 404: kalit yoki manba yoʻq — Kelgan arizalar → Reklama havolalari → "
        "Agent → kalit; .env CRM_AGENT_KEY; make reload."
    )


def test_the_salt_the_key_hashes_with_is_compared_with_the_feed_and_the_store():
    assert keys_salt(KEY) == KEY_SALT

    def salt_lines(db: DbState | None, crm: doctor.CrmState | None = None):
        return [
            (f.level, f.text)
            for f in _check(_crm_env(), db=db, crm=crm)
            if "salt_id" in f.text
        ]

    migrated = DbState(True, "0001_initial", salt_id=None)
    assert salt_lines(migrated) == [
        (
            "info",
            f"ℹ️ salt_id: kalitniki {KEY_SALT}; bazada hali yoʻq — birinchi toʻliq "
            "yuklash (02:30) uni yozadi.",
        )
    ]
    assert salt_lines(DbState(True, "0001_initial", salt_id=KEY_SALT)) == [
        ("info", f"✅ salt_id {KEY_SALT}: bazadagi va kalitniki bir xil.")
    ]
    assert salt_lines(DbState(True, "0001_initial", salt_id="7a659490")) == [
        (
            "warn",
            f"⚠️ Bazadagi salt_id 7a659490, kalitniki {KEY_SALT} — kalit yangilangan: "
            "keyingi toʻliq yuklashgacha push toʻxtaydi (suppression_stale).",
        )
    ]
    assert salt_lines(DbState(False, error="OperationalError")) == [
        ("info", f"ℹ️ salt_id: kalitniki {KEY_SALT} (baza tekshirilmadi).")
    ]
    # The feed answering another salt for this very key is a contract break.
    lines = salt_lines(
        DbState(True, "0001_initial", salt_id=KEY_SALT),
        doctor.CrmState(200, salt_id="8254c329"),
    )
    assert lines[0] == (
        "error",
        f"❌ ERP feed salt_id 8254c329, kalitniki {KEY_SALT} — ERP xesh qoidasi "
        "oʻzgargan; bostirish roʻyxatini oʻqib boʻlmaydi, ERP ni tekshir.",
    )


def test_feeds_configured_with_the_erp_sync_row_off_is_a_warning():
    """T-09's deferred note: the seed ships erp_sync off, and then no pull
    ever runs — pushes are refused stale with no limit run out, so health's
    suppression_stale never turns critical. The doctor says it."""

    def erp_lines(env: dict[str, str], db: DbState) -> list[tuple[str, str]]:
        return _levels(_check(env, db=db), "«erp_sync» manbasi")

    off = DbState(True, "0001_initial", salt_id=None, erp_sync_enabled=False)
    assert erp_lines(_crm_env(), off) == [
        (
            "warn",
            "⚠️ ERP kaliti bor, lekin «erp_sync» manbasi oʻchiq: bostirish roʻyxati "
            "yuklanmaydi va har bir push «stale» bilan toʻxtaydi — botda /manbalar "
            "orqali yoqing.",
        )
    ]
    on = DbState(True, "0001_initial", salt_id=None, erp_sync_enabled=True)
    assert erp_lines(_crm_env(), on) == []
    # Not read, or the «Boshqa» fallback (no feed to pull): nothing to say.
    assert erp_lines(_crm_env(), DbState(True, "0001_initial")) == []
    assert erp_lines(_crm_env(CRM_PUSH_SOURCE="other"), off) == []


async def test_probe_database_reads_whether_erp_sync_is_on(session):
    import os

    import sqlalchemy as sa

    url = os.environ.get("DATABASE_URL", "")
    row = sa.text("SELECT enabled FROM sources WHERE key = 'erp_sync'")
    before = (await session.execute(row)).scalar_one()
    try:
        for enabled in (False, True):
            await session.execute(
                sa.text("UPDATE sources SET enabled = :e WHERE key = 'erp_sync'"),
                {"e": enabled},
            )
            await session.commit()
            assert doctor.probe_database(url).erp_sync_enabled is enabled
    finally:
        await session.execute(
            sa.text("UPDATE sources SET enabled = :e WHERE key = 'erp_sync'"),
            {"e": before},
        )
        await session.commit()


def keys_salt(secret: str) -> str:
    from clientscrapper.services import keys

    return keys.erp_salt_id(secret)


def test_probe_network_asks_only_what_it_has_something_to_ask():
    made: list[str] = []

    def make(token: str) -> FakeBot:
        made.append(token)
        return FakeBot(token)

    seen, transport = _erp(lambda r: httpx.Response(200, json=_page()))
    env = _crm_env(MANAGER_TELEGRAM_IDS=MANAGERS[0])
    telegram, crm = asyncio.run(
        doctor.probe_network(env, make_bot=make, transport=transport)
    )
    assert telegram is not None and telegram.chats == {OWNER: "ok", MANAGERS[0]: "ok"}
    assert crm == doctor.CrmState(200, salt_id=KEY_SALT) and len(seen) == 1
    # No token, the fallback key, an http:// base: nothing is asked.
    made.clear()
    seen.clear()
    for change in (
        {"ASSISTANT_BOT_TOKEN": "", "CRM_PUSH_SOURCE": "other"},
        {
            "ASSISTANT_BOT_TOKEN": "# @BotFather",
            "CRM_BASE_URL": "http://gsr-namuna.example.uz",
        },
        {"ASSISTANT_BOT_TOKEN": "", "CRM_AGENT_KEY": ""},
    ):
        got = asyncio.run(
            doctor.probe_network(_crm_env(**change), make_bot=make, transport=transport)
        )
        assert got == (None, None), change
    assert (made, seen) == ([], [])


def test_the_doctor_never_sends_anything_to_telegram():
    # Read-only by construction: the stand-in has no send method, and the
    # wrapper around aiogram's Bot exposes only the three calls.
    assert {name for name in vars(doctor._AiogramBot) if not name.startswith("__")} == {
        "get_me",
        "get_chat",
        "close",
    }


async def test_the_database_probe_reads_the_stored_salt(session):
    import os

    import sqlalchemy as sa

    url = os.environ.get("DATABASE_URL", "")
    before = (
        await session.execute(
            sa.text("SELECT config FROM sources WHERE key = 'erp_sync'")
        )
    ).scalar_one()
    try:
        assert doctor.probe_database(url).salt_id is None
        await session.execute(
            sa.text(
                "UPDATE sources SET config = jsonb_set(config, '{suppression,salt_id}', "
                "'\"9caf06bb\"') WHERE key = 'erp_sync'"
            )
        )
        await session.commit()
        state = doctor.probe_database(url)
        assert (state.reachable, state.salt_id) == (True, KEY_SALT)
        assert state.version is not None
    finally:
        await session.execute(
            sa.text(
                "UPDATE sources SET config = CAST(:c AS jsonb) WHERE key = 'erp_sync'"
            ),
            {"c": json.dumps(before)},
        )
        await session.commit()


def test_the_example_host_never_receives_the_key():
    """A real CRM_AGENT_KEY with CRM_BASE_URL still the .env.example host (or
    blank, which falls back to it): example.uz is not reserved, so the probe
    must not send the key there, and the doctor names the cause."""
    seen, transport = _erp(lambda r: httpx.Response(404))
    for base in ("https://erp.example.uz", "https://ERP.example.uz/", ""):
        env = _crm_env(CRM_BASE_URL=base)
        telegram, crm = asyncio.run(
            doctor.probe_network(env | {"ASSISTANT_BOT_TOKEN": ""}, transport=transport)
        )
        assert (telegram, crm) == (None, None), base
        assert _errors(_check(env)) == [
            "❌ CRM_BASE_URL hali namuna manzili (erp.example.uz) — .env ga ERP ning "
            "haqiqiy https:// manzilini yoz; kalit u yerga yuborilmadi."
        ], base
    direct = asyncio.run(
        doctor.probe_crm("https://erp.example.uz", KEY, transport=transport)
    )
    assert (direct, seen) == (doctor.CrmState(None, error="placeholder"), [])
    # No key: nothing would be sent, so the example host is not an error.
    assert _errors(_check(_crm_env(CRM_BASE_URL="", CRM_AGENT_KEY=""))) == []
