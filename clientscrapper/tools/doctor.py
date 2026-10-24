# VENDORED_FROM dailyJournal@6b1f67f miya/tools/doctor.py
"""Preflight: check `.env`, the server and the database before `make up`.

    make doctor        (runs this inside the worker image, with .env injected)

It must NOT import ``clientscrapper.config``: a broken `.env` is exactly what
it diagnoses, and the settings module refuses to load one. Everything comes
from ``os.environ`` (Compose passes `.env` through ``env_file``),
``/proc/meminfo``, the disk and one database probe. Neither `.env` nor
`.env.example` exists inside the image, so the expected keys are a constant
here; ``tests/test_env_example.py`` keeps it equal to the keys of
`.env.example`.

Config, server and database checks (T-01); the configured model ids priced
and the daily allocations table (T-06) — read from ``services/usage.py``,
which imports no settings. T-08 adds the network (06 §3.3): the bot token
through ``get_me`` and the owner's and every manager's id through
``get_chat`` (read-only Bot API calls — nothing is ever sent), the ERP through
one keyed ``GET /api/agent/suppression?limit=1`` while the feed is
configured (never a POST: it would land a lead), and the ``salt_id`` the key
hashes with against the one the feed answers and the one stored by the last
full pull (04 AC 13).

``check`` is pure: every probe's answer is handed in (``DbState``,
``TelegramState``, ``CrmState``; None = not probed), so the tests use fakes
and no test reaches the network. ``main`` runs the probes.

Exit code 1 when anything is an error; warnings alone exit 0. A network that
cannot be reached is a warning — the doctor also runs on a laptop.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from clientscrapper.services import keys, usage

EXPECTED_ENV_KEYS: tuple[str, ...] = (
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "DATABASE_URL",
    "DB_PORT",
    "ANTHROPIC_API_KEY",
    "EXTRACT_MODEL",
    "VERDICT_MODEL",
    "BATCH_MODEL",
    "EXTRACT_MODEL_PRICE",
    "VERDICT_MODEL_PRICE",
    "BATCH_MODEL_PRICE",
    "DAILY_COST_CAP_USD",
    "MONTHLY_COST_CAP_USD",
    "SPEND_ALERT_PCT",
    "ASSISTANT_BOT_TOKEN",
    "OWNER_TELEGRAM_ID",
    "MANAGER_TELEGRAM_IDS",
    "USERBOT_ENABLED",
    "TELETHON_API_ID",
    "TELETHON_API_HASH",
    "TELETHON_SESSION",
    "USERBOT_JOINS_PER_DAY",
    "USERBOT_MAX_CHATS",
    "USERBOT_FLOOD_BREAKER_SECONDS",
    "USERBOT_FLOOD_BREAKER_HOURS",
    "COLLECTORS_ENABLED",
    "COLLECTOR_RPS_DEFAULT",
    "COLLECTOR_DAILY_PAGE_BUDGET",
    "HH_USER_AGENT",
    "HH_APP_TOKEN",
    "BRAVE_API_KEY",
    "YANDEX_SEARCH_API_KEY",
    "YANDEX_FOLDER_ID",
    "GOOGLE_PLACES_API_KEY",
    "APIFY_TOKEN",
    "SEARXNG_URL",
    "PROXY_URL",
    "IG_GRAPH_TOKEN",
    "IG_BUSINESS_ACCOUNT_ID",
    "TWOGIS_API_KEY",
    "CRM_PUSH_ENABLED",
    "CRM_BASE_URL",
    "CRM_PUSH_SOURCE",
    "CRM_AGENT_KEY",
    "PUSH_DAILY_CAP",
    "PUSH_WINDOW",
    "PUSH_DAYS",
    "TEST_PHONE",
    "REVIEW_HOURS",
    "REVIEW_BURST_MAX",
    "REVIEW_DAILY_CAP",
    "REVIEW_EXPIRY_HOURS",
    "DIGEST_TIME",
    "QUIET_HOURS",
    "ALERT_REPEAT_HOURS",
    "HEARTBEAT_STALE_MINUTES",
    "USERBOT_STALE_MINUTES",
    "DISK_MIN_FREE_GB",
    "DEADMAN_PING_URL",
    "DEADMAN_PING_MINUTES",
    "BACKUP_DIR",
    "BACKUP_TIME",
    "BACKUP_RETENTION_DAYS",
    "BACKUP_AGE_RECIPIENT",
    "BACKUP_TO_TELEGRAM",
    "BACKUP_MAX_AGE_HOURS",
    "RESTORE_DRILL_ENABLED",
    "RESTORE_DRILL_DAY",
    "EVIDENCE_DIR",
    "EVIDENCE_MAX_GB",
    "TIMEZONE",
    "LOG_LEVEL",
    "LOG_FORMAT",
)

# Where to get each required value, in the owner's words.
REQUIRED_HINTS: dict[str, str] = {
    "POSTGRES_PASSWORD": (
        "openssl rand -hex 24 (DATABASE_URL ichidagi parolni ham shu bilan almashtir)"
    ),
    "ANTHROPIC_API_KEY": (
        "console.anthropic.com → API Keys (clientScrapper uchun alohida kalit)"
    ),
    "ASSISTANT_BOT_TOKEN": "Telegramda @BotFather → /newbot (MIYA botidan alohida bot)",
    "OWNER_TELEGRAM_ID": "Telegramda @userinfobot ga yoz — raqamingni aytadi",
}
_MY_TELEGRAM = "my.telegram.org → API development tools (kompaniyaning oʻquvchi akkaunti)"
TELETHON_HINTS: dict[str, str] = {
    "TELETHON_API_ID": _MY_TELEGRAM,
    "TELETHON_API_HASH": _MY_TELEGRAM,
}
CRM_HINTS: dict[str, str] = {
    "CRM_AGENT_KEY": "ERP: Kelgan arizalar → Reklama havolalari → Agent → kalit",
}

PASSWORD_PLACEHOLDER = "CHANGE_ME"
# More missing keys than this are reported as one line.
MISSING_KEYS_LISTED = 3
# A "4 GB" VPS reports about 3.8 GiB of MemTotal (06 §3.5 recommends 8 GB).
MIN_RAM_GIB = 3.7
MIN_SWAP_GIB = 0.9
DEFAULT_DISK_MIN_FREE_GB = 5.0
DB_CONNECT_TIMEOUT_SECONDS = 3
BINARIES = ("age", "pg_dump")
_GIB = 1024**3
_GB = 1000**3
# The network probes (06 §3.3). Each call has its own deadline; the doctor
# never waits on a hung host for longer than this.
NETWORK_TIMEOUT_SECONDS = 10.0
# The feed read the doctor makes: one entry, keyed, never followed (05 §B.1).
CRM_FEED_PATH = "/api/agent/suppression"
CRM_DEFAULT_BASE_URL = "https://erp.example.uz"
# The host .env.example (and the settings default) ship; never probed. The
# rule's home is services/keys (the ERP client refuses the same base).
CRM_PLACEHOLDER_HOST = keys.ERP_PLACEHOLDER_HOST
DOCTOR_USER_AGENT = "clientscrapper-doctor/1"
SALT_ID = re.compile(r"[0-9a-f]{8}")
# aiogram's exception classes, by name (the probe classifies whatever the Bot
# raises without importing aiogram here; the fakes in tests carry the names).
TOKEN_REFUSED_ERRORS = frozenset({"TelegramUnauthorizedError", "TokenValidationError"})
CHAT_MISSING_ERRORS = frozenset(
    {"TelegramBadRequest", "TelegramForbiddenError", "TelegramNotFound"}
)


@dataclass(frozen=True)
class Finding:
    # info: printed for the owner (a passed check, a step not built yet),
    # never a problem.
    level: Literal["error", "warn", "info"]
    text: str


@dataclass(frozen=True)
class DbState:
    reachable: bool
    # alembic_version.version_num; None = reachable but never migrated.
    version: str | None = None
    # An error class name, never the message (it may carry the DSN).
    error: str | None = None
    # sources.erp_sync.config.suppression.salt_id: the salt the last
    # completed full pull stored (None = never pulled, 05 §B.2 step 5).
    salt_id: str | None = None
    # sources.erp_sync.enabled (None = not read): the seed ships it off.
    erp_sync_enabled: bool | None = None


TokenVerdict = Literal["ok", "refused", "unreachable"]
ChatVerdict = Literal["ok", "not_found", "unreachable"]


@dataclass(frozen=True)
class TelegramState:
    """What the Bot API said. ``token`` is ``get_me``'s verdict; ``chats``
    maps each id asked with ``get_chat`` to its verdict (asked only once the
    token answered)."""

    token: TokenVerdict
    username: str | None = None
    # An error class name, never the message (it may echo the token).
    error: str | None = None
    chats: Mapping[str, ChatVerdict] = field(default_factory=dict)
    chat_errors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CrmState:
    """The keyed feed GET's answer: the HTTP status (None = no answer, with
    the transport error's class name), and on a 200 the page's ``salt_id``
    — or why the body is not a feed page."""

    status: int | None
    error: str | None = None
    salt_id: str | None = None
    shape: Literal["ok", "not_json", "no_salt"] = "ok"


def _error(text: str) -> Finding:
    return Finding("error", text)


def _warn(text: str) -> Finding:
    return Finding("warn", text)


def _info(text: str) -> Finding:
    return Finding("info", text)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _meminfo_kib(meminfo: str, field: str) -> int | None:
    match = re.search(rf"^{field}:\s+(\d+)\s*kB", meminfo, re.MULTILINE)
    return int(match.group(1)) if match else None


def _hhmm(value: str) -> time | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _range(value: str) -> tuple[time, time] | None:
    start, sep, end = value.partition("-")
    first, last = _hhmm(start), _hhmm(end)
    if not sep or first is None or last is None:
        return None
    return first, last


def _within(current: time, start: time, end: time) -> bool:
    """config.within, restated: this module may not import the settings."""
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def _database(state: DbState) -> Finding:
    if not state.reachable:
        return _warn(
            f"⚠️ Baza hozircha javob bermayapti ({state.error or 'ulanish yoʻq'}) — "
            "make up uni ishga tushiradi; keyin make doctor ni qaytadan ishlat."
        )
    if state.version is None:
        return _info(
            "ℹ️ Baza ulanadi, lekin migratsiya hali qoʻllanmagan — make up "
            "(yoki make migrate) uni qoʻllaydi."
        )
    return _info(f"✅ Baza: ulanish bor, migratsiya {state.version}.")


def telegram_ids(env: Mapping[str, str]) -> tuple[str, ...]:
    """The ids the bot must reach: the owner's, then every manager's —
    numeric ones only (C5 reports the rest)."""
    owner = (env.get("OWNER_TELEGRAM_ID") or "").strip()
    managers = [
        m.strip() for m in (env.get("MANAGER_TELEGRAM_IDS") or "").split(",") if m.strip()
    ]
    found: list[str] = []
    for chat_id in (owner, *managers):
        if chat_id.isdigit() and chat_id not in found:
            found.append(chat_id)
    return tuple(found)


def _telegram(env: Mapping[str, str], state: TelegramState) -> list[Finding]:
    """get_me and get_chat (06 §3.3, §1.7): the owner must be reachable or
    no alert arrives; a manager who has not /start-ed the bot cannot receive
    a card, which is a warning — the agent runs without them."""
    if state.token == "refused":
        return [
            _error(
                f"❌ ASSISTANT_BOT_TOKEN ni Telegram rad etdi ({state.error or '?'}) — "
                "@BotFather → /mybots → API Token; .env ga qaytadan koʻchir."
            )
        ]
    if state.token == "unreachable":
        return [
            _warn(
                f"⚠️ Telegram bilan bogʻlanib boʻlmadi ({state.error or '?'}) — bot "
                "tokeni va egasi/menejerlar tekshirilmadi; internetni tekshir."
            )
        ]
    found = [_info(f"✅ Bot: @{state.username or '?'} (get_me javob berdi).")]
    owner = (env.get("OWNER_TELEGRAM_ID") or "").strip()
    for chat_id, verdict in state.chats.items():
        error = state.chat_errors.get(chat_id, "?")
        is_owner = chat_id == owner
        who = "Egasi" if is_owner else "Menejer"
        if verdict == "ok":
            found.append(_info(f"✅ {who} {chat_id}: bot unga yoza oladi (get_chat)."))
        elif verdict == "unreachable":
            found.append(_warn(f"⚠️ {who} {chat_id}: tekshirib boʻlmadi ({error})."))
        elif is_owner:
            found.append(
                _error(
                    f"❌ OWNER_TELEGRAM_ID={chat_id}: bot bu chatni topmadi ({error}) — "
                    "botga /start yoz; raqamni @userinfobot da tekshir. Aks holda "
                    "hech qanday ogohlantirish kelmaydi."
                )
            )
        else:
            found.append(
                _warn(
                    f"⚠️ Menejer {chat_id}: bot bu chatni topmadi ({error}) — menejer "
                    "botga /start yozsin, aks holda unga karta yetib bormaydi."
                )
            )
    return found


def feeds_configured(env: Mapping[str, str]) -> bool:
    """erp_feeds.feeds_configured (05 §B.2), restated on the raw env: this
    module may not import the settings. The «Boshqa» key reads nothing."""
    source = (env.get("CRM_PUSH_SOURCE") or "agent").strip().lower()
    return source == "agent" and bool((env.get("CRM_AGENT_KEY") or "").strip())


def crm_base_url(env: Mapping[str, str]) -> str:
    return (env.get("CRM_BASE_URL") or "").strip() or CRM_DEFAULT_BASE_URL


def is_placeholder(base_url: str) -> bool:
    """The .env.example host, still in place (or CRM_BASE_URL left blank)."""
    return keys.is_erp_placeholder(base_url)


def _crm(env: Mapping[str, str], state: CrmState | None) -> list[Finding]:
    """CRM_BASE_URL and the keyed feed GET (06 §3.3, 05 §A.5/§B.1)."""
    base = crm_base_url(env)
    if not base.lower().startswith("https://"):
        return [
            _error(
                "❌ CRM_BASE_URL https:// bilan boshlanishi kerak — http:// soʻroviga "
                "ERP 308 qaytaradi, agent unga ergashmaydi (erp_redirect)."
            )
        ]
    keyed = bool((env.get("CRM_AGENT_KEY") or "").strip())
    if keyed and is_placeholder(base):
        return [
            _error(
                f"❌ CRM_BASE_URL hali namuna manzili ({CRM_PLACEHOLDER_HOST}) — .env ga "
                "ERP ning haqiqiy https:// manzilini yoz; kalit u yerga yuborilmadi."
            )
        ]
    if not feeds_configured(env):
        source = (env.get("CRM_PUSH_SOURCE") or "agent").strip().lower()
        if source != "agent":
            return [
                _info(
                    "ℹ️ ERP: CRM_PUSH_SOURCE=other («Boshqa» kaliti) — feed oʻqilmaydi, "
                    "sinov push ham qilinmaydi (u lid qoʻshib qoʻyadi)."
                )
            ]
        return [_info("ℹ️ ERP: CRM_AGENT_KEY boʻsh — ERP bilan bogʻlanish tekshirilmadi.")]
    if state is None:
        return []
    pushing = _truthy(env.get("CRM_PUSH_ENABLED", "false"))
    status = state.status
    if status is None:
        return [
            _warn(
                f"⚠️ ERP ga ulanib boʻlmadi ({state.error or '?'}) — internetni va "
                "CRM_BASE_URL ni tekshir."
            )
        ]
    if status == 200 and state.shape != "ok":
        return [
            _error(
                "❌ ERP 200 qaytardi, lekin javob bostirish roʻyxati emas "
                f"({state.shape}) — CRM_BASE_URL ERP ning oʻzi ekanini tekshir."
            )
        ]
    if status == 200:
        return [_info("✅ ERP: /api/agent/suppression javob berdi (200).")]
    if status == 404:
        text = (
            "ERP 404: kalit yoki manba yoʻq — Kelgan arizalar → Reklama havolalari → "
            "Agent → kalit; .env CRM_AGENT_KEY; make reload."
        )
        return [_error(f"❌ {text}")] if pushing else [_warn(f"⚠️ {text}")]
    if 300 <= status < 400:
        return [
            _error(
                f"❌ ERP manzili yoʻnaltiryapti (HTTP {status}) — CRM_BASE_URL toʻgʻri "
                "manzil va https:// boʻlsin; agent yoʻnaltirishga ergashmaydi."
            )
        ]
    return [
        _warn(
            f"⚠️ ERP HTTP {status} qaytardi — keyinroq qaytadan tekshir "
            "(503 = ERP bazasi ishlamayapti)."
        )
    ]


def _erp_source(env: Mapping[str, str], db: DbState | None) -> list[Finding]:
    """The feeds configured while the ``erp_sync`` row is off (the seed ships
    it off): no pull ever runs, the suppression list is never fresh and every
    push is refused ``stale`` — with no limit run out, so suppression_stale
    never turns critical. A warning, not a stop: the row is switched on from
    the bot, which runs only after ``make up``."""
    if not feeds_configured(env) or db is None or db.erp_sync_enabled is not False:
        return []
    return [
        _warn(
            "⚠️ ERP kaliti bor, lekin «erp_sync» manbasi oʻchiq: bostirish roʻyxati "
            "yuklanmaydi va har bir push «stale» bilan toʻxtaydi — botda /manbalar "
            "orqali yoqing."
        )
    ]


def _salt(
    env: Mapping[str, str], db: DbState | None, crm: CrmState | None
) -> list[Finding]:
    """The salt the key hashes with (04 §7: sha256(secret)[:8]) against the
    feed's answer and the last full pull's stored one (04 AC 13, 05 §B.2)."""
    if not feeds_configured(env):
        return []
    own = keys.erp_salt_id((env.get("CRM_AGENT_KEY") or "").strip())
    found: list[Finding] = []
    if crm is not None and crm.salt_id is not None and crm.salt_id != own:
        found.append(
            _error(
                f"❌ ERP feed salt_id {crm.salt_id}, kalitniki {own} — ERP xesh qoidasi "
                "oʻzgargan; bostirish roʻyxatini oʻqib boʻlmaydi, ERP ni tekshir."
            )
        )
    stored = db.salt_id if db is not None and db.version is not None else None
    if db is None or not db.reachable or db.version is None:
        found.append(_info(f"ℹ️ salt_id: kalitniki {own} (baza tekshirilmadi)."))
    elif stored is None:
        found.append(
            _info(
                f"ℹ️ salt_id: kalitniki {own}; bazada hali yoʻq — birinchi toʻliq "
                "yuklash (02:30) uni yozadi."
            )
        )
    elif stored == own:
        found.append(_info(f"✅ salt_id {own}: bazadagi va kalitniki bir xil."))
    else:
        found.append(
            _warn(
                f"⚠️ Bazadagi salt_id {stored}, kalitniki {own} — kalit yangilangan: "
                "keyingi toʻliq yuklashgacha push toʻxtaydi (suppression_stale)."
            )
        )
    return found


def _usd(value: Decimal) -> str:
    """Money with at least two decimals and no trailing noise: 0.10, 0.165, 10.00."""
    whole, _, frac = f"{value.normalize():f}".partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def _model_prices(env: Mapping[str, str]) -> list[Finding]:
    """Each configured model id must be priced (04 §8): ``costs.ask`` refuses
    every call to an unpriced one, so a wrong id silently stops that work."""
    found: list[Finding] = []
    # A missing key runs on the shipped default; a blank one is read by the
    # settings as an empty id, which nothing prices.
    roles = tuple(
        (env.get(r.key, r.default).strip(), env.get(r.price_key) or "")
        for r in usage.MODEL_ROLES
    )
    for role, (model, override) in zip(usage.MODEL_ROLES, roles, strict=True):
        if override.strip() and usage.parse_price(override) is None:
            found.append(
                _error(
                    f"❌ {role.price_key} oʻqilmadi — «kirish/chiqish» yoki "
                    "«kirish/chiqish/kesh_oʻqish/kesh_yozish» boʻlsin, masalan 0.10/0.50."
                )
            )
            continue
        if not model:
            found.append(_error(f"❌ {role.key} boʻsh — model nomini yoz."))
            continue
        price = usage.resolve_price(model, roles)
        if price is None:
            found.append(
                _error(
                    f"❌ {role.key}={model}: narxi nomaʼlum — bu modelga har bir "
                    f"chaqiruv ({role.label_uz}) rad etiladi. Model nomini tekshir yoki "
                    f"{role.price_key}=kirish/chiqish yoz (1 mln token uchun $)."
                )
            )
            continue
        source = role.price_key if override.strip() else "usage.py"
        if price.input == 0 and price.output == 0:
            # costs.ask refuses a paid call estimated at $0 as unpriced.
            found.append(
                _error(
                    f"❌ {role.key}={model}: narxi $0 ({source}) — bunday narx bilan har "
                    f"bir chaqiruv ({role.label_uz}) rad etiladi. {role.price_key} ni "
                    "boʻshat yoki haqiqiy narxni yoz."
                )
            )
            continue
        found.append(
            _info(
                f"✅ {role.key}={model}: kirish ${_usd(price.input)} · chiqish "
                f"${_usd(price.output)} / 1 mln token ({source}; {role.label_uz})."
            )
        )
    return found


def allocations_table() -> str:
    """The daily budget split of 04 §8, one line per family."""
    total = sum(usage.ALLOCATIONS.values(), Decimal(0))
    lines = [f"ℹ️ Kunlik byudjet taqsimoti (USD, Toshkent kuni) — jami ${_usd(total)}:"]
    for family in usage.FAMILIES:
        ops = [op for op, f in usage.FAMILY.items() if f == family]
        parts = " · ".join(f"{op} {_usd(usage.ALLOCATIONS[op])}" for op in ops)
        lines.append(f"   {family} ${_usd(usage.FAMILY_CAPS[family])}: {parts}")
    return "\n".join(lines)


def check(
    env: Mapping[str, str],
    meminfo: str,
    disk_free: int,
    key_file_exists: bool,
    *,
    db: DbState | None = None,
    binaries: Mapping[str, bool] | None = None,
    telegram: TelegramState | None = None,
    crm: CrmState | None = None,
) -> list[Finding]:
    """Everything wrong with this `.env` and this server, errors first.
    ``db``, ``telegram`` and ``crm`` are the probes' answers (None = not
    probed); ``binaries`` maps a program name to whether it is on PATH (None =
    not checked)."""
    found: list[Finding] = []

    def value(key: str) -> str:
        return (env.get(key) or "").strip()

    # C1: keys that .env.example has and this .env lacks. After an update
    # many new keys are missing at once; they run on their defaults, so they
    # are one line, not one alarm each.
    missing = [key for key in EXPECTED_ENV_KEYS if key not in env]
    if len(missing) > MISSING_KEYS_LISTED:
        found.append(
            _warn(
                f"⚠️ .env faylida {len(missing)} ta yangi sozlama yoʻq — ular standart "
                "qiymat bilan ishlaydi; oʻzgartirmoqchi boʻlsang .env.example faylidan "
                f"koʻchir: {', '.join(missing)}"
            )
        )
    else:
        for key in missing:
            found.append(
                _warn(f"⚠️ {key} .env faylida yoʻq — .env.example faylidan koʻchir.")
            )

    # C2: a trailing comment read as the value.
    commented = {key for key, raw in env.items() if (raw or "").lstrip().startswith("#")}
    for key in sorted(commented):
        found.append(
            _error(
                f"❌ {key}: qiymat izohga oʻxshaydi ('#…'). "
                ".env faylida izohni alohida qatorga oʻtkaz."
            )
        )

    # C3: required values.
    required = dict(REQUIRED_HINTS)
    if _truthy(env.get("USERBOT_ENABLED", "true")):
        required.update(TELETHON_HINTS)
    if _truthy(env.get("CRM_PUSH_ENABLED", "false")):
        required.update(CRM_HINTS)
    for key, hint in required.items():
        if key not in commented and not value(key):
            found.append(_error(f"❌ {key} boʻsh — toʻldir: {hint}"))

    # C4: the database password: never the placeholder, and the same in the URL.
    password = value("POSTGRES_PASSWORD")
    if password == PASSWORD_PLACEHOLDER:
        found.append(
            _error(
                "❌ POSTGRES_PASSWORD hali «CHANGE_ME» — birinchi make up dan oldin "
                "almashtir (keyin oʻzgartirish qiyin), DATABASE_URL ichida ham."
            )
        )
    elif password and f":{password}@" not in value("DATABASE_URL"):
        found.append(
            _error("❌ DATABASE_URL dagi parol POSTGRES_PASSWORD bilan bir xil emas.")
        )

    # C5: the owner's and the managers' Telegram ids.
    owner_id = value("OWNER_TELEGRAM_ID")
    if owner_id and not owner_id.isdigit() and "OWNER_TELEGRAM_ID" not in commented:
        found.append(
            _error("❌ OWNER_TELEGRAM_ID faqat raqam boʻlishi kerak (masalan 123456789).")
        )
    managers = [m.strip() for m in value("MANAGER_TELEGRAM_IDS").split(",") if m.strip()]
    bad = [m for m in managers if not m.isdigit()]
    if bad and "MANAGER_TELEGRAM_IDS" not in commented:
        found.append(
            _error(
                f"❌ MANAGER_TELEGRAM_IDS: {', '.join(bad)} raqam emas — "
                "vergul bilan ajratilgan Telegram id lar boʻlsin."
            )
        )

    # C6: the backup key: optional, but then nothing is backed up.
    recipient = value("BACKUP_AGE_RECIPIENT")
    if "BACKUP_AGE_RECIPIENT" not in commented:
        if not recipient:
            found.append(
                _warn(
                    "⚠️ BACKUP_AGE_RECIPIENT boʻsh — tungi zaxira yozilmaydi: "
                    "make backup-key."
                )
            )
        elif not recipient.startswith(("age1", "ssh-")):
            found.append(
                _error(
                    "❌ BACKUP_AGE_RECIPIENT «age1…» bilan boshlanishi kerak — "
                    "make backup-key chiqargan qatorni koʻchir."
                )
            )
        elif not key_file_exists:
            found.append(
                _warn(
                    "⚠️ secrets/backup-key.txt topilmadi — tiklash uchun maxfiy kalit "
                    "kerak. make backup-key-show bilan nusxasini server tashqarisida "
                    "saqla."
                )
            )

    # C7: the reader account's session, made after the first make up.
    if _truthy(env.get("USERBOT_ENABLED", "true")) and not value("TELETHON_SESSION"):
        found.append(
            _warn("⚠️ TELETHON_SESSION boʻsh — make up dan keyin make userbot-login qil.")
        )

    # C8: times the bot and the scheduler read; the digest goes out while
    # cards are being sent, never outside REVIEW_HOURS.
    digest = _hhmm(value("DIGEST_TIME") or "09:00")
    review = _range(value("REVIEW_HOURS") or "09:00-18:00")
    if digest is None:
        found.append(_error("❌ DIGEST_TIME oʻqilmadi — «SS:DD» boʻlsin, masalan 09:00."))
    if review is None:
        found.append(
            _error(
                "❌ REVIEW_HOURS oʻqilmadi — «SS:DD-SS:DD» boʻlsin, masalan 09:00-18:00."
            )
        )
    if digest is not None and review is not None and not _within(digest, *review):
        found.append(
            _error(
                f"❌ DIGEST_TIME={value('DIGEST_TIME')} REVIEW_HOURS="
                f"{value('REVIEW_HOURS')} oraligʻidan tashqarida — vaqtni oraliq "
                "ichiga qoʻy."
            )
        )

    # C9: memory and swap.
    mem_kib = _meminfo_kib(meminfo, "MemTotal")
    if mem_kib is not None and mem_kib * 1024 / _GIB < MIN_RAM_GIB:
        found.append(
            _error(
                f"❌ Serverda {mem_kib * 1024 / _GIB:.1f} GB RAM bor; kamida 4 GB kerak "
                "(8 GB tavsiya). Kattaroq server ol yoki SKIP_DOCTOR=1 bilan oʻz "
                "xavfingga ishga tushir."
            )
        )
    swap_kib = _meminfo_kib(meminfo, "SwapTotal")
    if swap_kib is not None and swap_kib * 1024 / _GIB < MIN_SWAP_GIB:
        found.append(_warn("⚠️ Swap 1 GB dan kam — 2 GB swap qoʻsh."))

    # C10: disk.
    try:
        min_free = float(value("DISK_MIN_FREE_GB") or DEFAULT_DISK_MIN_FREE_GB)
    except ValueError:
        min_free = DEFAULT_DISK_MIN_FREE_GB
    if disk_free < min_free * _GB:
        found.append(
            _warn(
                f"⚠️ Diskda {disk_free / _GB:.0f} GB boʻsh — DISK_MIN_FREE_GB="
                f"{min_free:g} dan kam."
            )
        )

    # C11: the backup programs (present in the image; absent on a laptop).
    for name, present in sorted((binaries or {}).items()):
        if not present:
            found.append(
                _warn(f"⚠️ {name} topilmadi — zaxira ishlamaydi (Docker obrazida bor).")
            )

    # C12: the database.
    if db is not None:
        found.append(_database(db))

    # C13: the configured model ids are priced; the allocations table.
    found.extend(_model_prices(env))
    found.append(_info(allocations_table()))

    # C14: the bot token and the ids it must reach (T-08).
    if telegram is not None:
        found.extend(_telegram(env, telegram))

    # C15: the ERP address and the keyed feed GET (T-08).
    found.extend(_crm(env, crm))

    # C16: the salt_id (T-08; 04 §13).
    found.extend(_salt(env, db, crm))
    found.extend(_erp_source(env, db))

    rank = {"error": 0, "warn": 1, "info": 2}
    found.sort(key=lambda f: rank[f.level])
    return found


def summary(findings: list[Finding], *, in_update: bool = False) -> str:
    """``in_update``: run by ``make update`` (CS_IN_UPDATE=1), which goes on
    by itself, so the all-clear does not tell the owner to type make up."""
    errors = sum(1 for f in findings if f.level == "error")
    if errors:
        return f"❌ {errors} ta muammo. Tuzatib, qaytadan: make doctor"
    if in_update:
        return "✅ Hammasi joyida — make update davom etadi, hech narsa yozmang."
    return "✅ Hammasi joyida — endi: make up"


def exit_code(findings: list[Finding]) -> int:
    return 1 if any(f.level == "error" for f in findings) else 0


def probe_database(url: str) -> DbState:
    """One connection with a short timeout; the error is reported by class."""
    if not url:
        return DbState(reachable=False, error="DATABASE_URL boʻsh")
    dsn = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS) as conn:
            row = conn.execute("SELECT to_regclass('public.alembic_version')").fetchone()
            if row is None or row[0] is None:
                return DbState(reachable=True)
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            erp = conn.execute(
                "SELECT config #>> '{suppression,salt_id}', enabled FROM sources "
                "WHERE key = 'erp_sync'"
            ).fetchone()
            return DbState(
                reachable=True,
                version=version[0] if version else None,
                salt_id=erp[0] if erp else None,
                erp_sync_enabled=bool(erp[1]) if erp else None,
            )
    except Exception as exc:
        return DbState(reachable=False, error=type(exc).__name__)


# --- the network probes (T-08) ------------------------------------------------


class BotLike(Protocol):
    """The two read-only Bot API calls the doctor makes, and the close."""

    async def get_me(self) -> Any: ...

    async def get_chat(self, chat_id: int) -> Any: ...

    async def close(self) -> None: ...


def _names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


class _AiogramBot:
    """aiogram's Bot behind ``BotLike``: get_me, get_chat, and the session
    closed. Nothing else of the Bot is reachable from here (06 §5.3)."""

    def __init__(self, token: str) -> None:
        from aiogram import Bot

        self._bot = Bot(token=token)

    async def get_me(self) -> Any:
        return await self._bot.get_me()

    async def get_chat(self, chat_id: int) -> Any:
        return await self._bot.get_chat(chat_id)

    async def close(self) -> None:
        await self._bot.session.close()


async def probe_telegram(
    token: str,
    chat_ids: Sequence[str],
    *,
    make_bot: Callable[[str], BotLike] | None = None,
    timeout: float = NETWORK_TIMEOUT_SECONDS,
) -> TelegramState:
    """``get_me`` for the token, then ``get_chat`` for each id. Read-only: a
    chat the bot cannot see is reported, never written to. Never raises."""
    factory = make_bot or _AiogramBot
    try:
        bot = factory(token)
    except Exception as exc:
        refused = bool(_names(exc) & TOKEN_REFUSED_ERRORS)
        return TelegramState(
            "refused" if refused else "unreachable", error=type(exc).__name__
        )
    try:
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout)
        except Exception as exc:
            refused = bool(_names(exc) & TOKEN_REFUSED_ERRORS)
            return TelegramState(
                "refused" if refused else "unreachable", error=type(exc).__name__
            )
        chats: dict[str, ChatVerdict] = {}
        errors: dict[str, str] = {}
        for chat_id in chat_ids:
            try:
                await asyncio.wait_for(bot.get_chat(int(chat_id)), timeout)
                chats[chat_id] = "ok"
            except Exception as exc:
                missing = bool(_names(exc) & CHAT_MISSING_ERRORS)
                chats[chat_id] = "not_found" if missing else "unreachable"
                errors[chat_id] = type(exc).__name__
        username = getattr(me, "username", None)
        return TelegramState(
            "ok",
            username=username if isinstance(username, str) else None,
            chats=chats,
            chat_errors=errors,
        )
    finally:
        with contextlib.suppress(Exception):
            await bot.close()


async def probe_crm(
    base_url: str,
    key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = NETWORK_TIMEOUT_SECONDS,
) -> CrmState:
    """One keyed ``GET /api/agent/suppression?limit=1`` (05 §A.5): both key
    headers, never a redirect followed, never a POST. Only for an https://
    base — the key never travels in clear. Never raises; the key is never in
    a URL, a message or a log line — nor sent to the .env.example host."""
    refusal = keys.erp_base_refusal(base_url)
    if refusal is not None:
        return CrmState(None, error=refusal)
    headers = {
        "X-GSR-Key": key,
        "X-ERP-Key": key,
        "Accept": "application/json",
        "User-Agent": DOCTOR_USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=timeout, follow_redirects=False
        ) as client:
            response = await client.get(
                base_url.rstrip("/") + CRM_FEED_PATH,
                params={"limit": 1},
                headers=headers,
            )
    except Exception as exc:
        return CrmState(None, error=type(exc).__name__)
    if response.status_code != 200:
        return CrmState(response.status_code)
    try:
        body = response.json()
    except ValueError:
        return CrmState(200, shape="not_json")
    salt = body.get("salt_id") if isinstance(body, dict) else None
    if not isinstance(salt, str) or not SALT_ID.fullmatch(salt):
        return CrmState(200, shape="no_salt")
    return CrmState(200, salt_id=salt)


async def probe_network(
    env: Mapping[str, str],
    *,
    make_bot: Callable[[str], BotLike] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[TelegramState | None, CrmState | None]:
    """Both probes, concurrently, each only when it has something to ask:
    a bot token for Telegram, a configured feed on https:// for the ERP."""
    token = (env.get("ASSISTANT_BOT_TOKEN") or "").strip()
    base = crm_base_url(env)

    async def nothing() -> None:
        return None

    telegram: Awaitable[TelegramState | None] = (
        probe_telegram(token, telegram_ids(env), make_bot=make_bot)
        if token and not token.startswith("#")
        else nothing()
    )
    crm: Awaitable[CrmState | None] = (
        probe_crm(base, (env.get("CRM_AGENT_KEY") or "").strip(), transport=transport)
        if feeds_configured(env)
        and base.lower().startswith("https://")
        and not is_placeholder(base)
        else nothing()
    )
    found_telegram, found_crm = await asyncio.gather(telegram, crm)
    return found_telegram, found_crm


def _read_meminfo() -> str:
    try:
        return Path("/proc/meminfo").read_text()
    except OSError:
        return ""


def _disk_free() -> int:
    for path in ("/data", "/"):
        try:
            return shutil.disk_usage(path).free
        except OSError:
            continue
    return 0


def _key_file_exists() -> bool:
    paths = ("/app/secrets/backup-key.txt", "secrets/backup-key.txt")
    return any(Path(p).exists() for p in paths)


def main() -> int:
    telegram, crm = asyncio.run(probe_network(os.environ))
    findings = check(
        os.environ,
        _read_meminfo(),
        _disk_free(),
        _key_file_exists(),
        db=probe_database(os.environ.get("DATABASE_URL", "")),
        binaries={name: shutil.which(name) is not None for name in BINARIES},
        telegram=telegram,
        crm=crm,
    )
    for finding in findings:
        print(finding.text)
    print(summary(findings, in_update=os.environ.get("CS_IN_UPDATE") == "1"))
    return exit_code(findings)


if __name__ == "__main__":
    sys.exit(main())
