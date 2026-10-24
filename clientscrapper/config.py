# VENDORED_FROM dailyJournal@6b1f67f miya/config.py
"""Application settings, loaded from environment / .env.

Every field has a ``KEY=`` line in ``.env.example`` and every line there is a
field (``tests/test_env_example.py`` holds the two equal in both directions);
06 §3.4 is the list. Model prices and the Claude cap are not here: their home
is code (04 §8).

A bad value ends the process with one line per problem that names the key,
never a pydantic traceback (``get_settings``).
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BeforeValidator,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Prices are code (04 §8): parse_price, the price table and the shipped model
# ids live in usage.py, which the doctor can import without these settings.
from clientscrapper.services.usage import DEFAULT_MODELS, parse_price

COMMENT_VALUE_ERROR = (
    "{key} qiymati izohga oʻxshaydi ('#…'): .env faylida izohni kalitdan "
    "yuqoridagi alohida qatorga oʻtkaz."
)
BACKUP_RECIPIENT_ERROR = (
    "BACKUP_AGE_RECIPIENT «age1…» bilan boshlanishi kerak. "
    "Kalitni yarat: make backup-key."
)
PRICE_ERROR = (
    "narx «kirish/chiqish» yoki «kirish/chiqish/kesh_oʻqish/kesh_yozish» koʻrinishida "
    "boʻlsin — million token uchun dollar, masalan 0.10/0.50"
)
TIME_ERROR = "vaqt «SS:DD» koʻrinishida boʻlsin, masalan 09:00"
RANGE_ERROR = "oraliq «SS:DD-SS:DD» koʻrinishida boʻlsin, masalan 09:30-17:30"
DAYS_ERROR = "kunlar 1–7 (dushanba=1) boʻlsin: «1-6» yoki «1,2,3,4,5»"
IDS_ERROR = "Telegram id raqamlari vergul bilan: «111111111,222222222»"


def _parse_hhmm(value: str) -> time:
    hour, minute = value.strip().split(":")
    return time(hour=int(hour), minute=int(minute))


def _parse_range(value: str) -> tuple[time, time]:
    start, end = value.split("-")
    return _parse_hhmm(start), _parse_hhmm(end)


def within(current: time, start: time, end: time) -> bool:
    """Is ``current`` inside [start, end), a range that may wrap midnight?"""
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def parse_days(value: str) -> frozenset[int]:
    """ISO weekdays from «1-6» or «1,3,5» (a mix works: «1-3,5»)."""
    days: set[int] = set()
    for part in value.replace(" ", "").split(","):
        if not part:
            continue
        lo, sep, hi = part.partition("-")
        first, last = int(lo), int(hi) if sep else int(lo)
        if not 1 <= first <= last <= 7:
            raise ValueError(part)
        days.update(range(first, last + 1))
    if not days:
        raise ValueError(value)
    return frozenset(days)


def _blank_to_none(v: object) -> object:
    """`.env.example` ships credentials as empty keys; treat those as unset."""
    if isinstance(v, str) and not v.strip():
        return None
    return v


def _csv_ints(v: object) -> object:
    """«111,222» → (111, 222); blank → (). Anything else is pydantic's to refuse."""
    if isinstance(v, str):
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if any(not p.isdigit() for p in parts):
            raise ValueError(IDS_ERROR)
        return tuple(int(p) for p in parts)
    return v


# An integer setting that may legitimately be blank until the owner fills it in.
OptionalInt = Annotated[int | None, BeforeValidator(_blank_to_none)]
# A comma-separated id list; NoDecode stops pydantic-settings reading it as JSON.
TelegramIds = Annotated[tuple[int, ...], NoDecode, BeforeValidator(_csv_ints)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database -----------------------------------------------------------
    # Compose reads the three POSTGRES_* keys for the db container; the app
    # reads only DATABASE_URL. They are fields so the doctor and the
    # .env.example test see one list.
    postgres_user: str = "clientscrapper"
    postgres_password: str = "CHANGE_ME"
    postgres_db: str = "clientscrapper"
    database_url: str = (
        "postgresql+psycopg://clientscrapper:CHANGE_ME@db:5432/clientscrapper"
    )
    db_port: int = Field(default=5433, ge=1, le=65535)

    # --- Claude -------------------------------------------------------------
    anthropic_api_key: str = ""
    extract_model: str = DEFAULT_MODELS["EXTRACT_MODEL"]
    verdict_model: str = DEFAULT_MODELS["VERDICT_MODEL"]
    batch_model: str = DEFAULT_MODELS["BATCH_MODEL"]
    # Blank = the code table in usage.py; a model it does not know is refused.
    extract_model_price: str = ""
    verdict_model_price: str = ""
    batch_model_price: str = ""
    daily_cost_cap_usd: Decimal = Field(default=Decimal("10"), ge=0)
    monthly_cost_cap_usd: Decimal = Field(default=Decimal("250"), ge=0)
    spend_alert_pct: int = Field(default=80, ge=1, le=100)

    # --- Telegram: owner bot and managers -----------------------------------
    assistant_bot_token: str = ""
    owner_telegram_id: OptionalInt = None
    # Empty = Telegram-only prospects wait in hold (06 §1.7).
    manager_telegram_ids: TelegramIds = ()

    # --- Telegram: reader account -------------------------------------------
    userbot_enabled: bool = True
    telethon_api_id: OptionalInt = None
    telethon_api_hash: str = ""
    telethon_session: str = ""
    userbot_joins_per_day: int = Field(default=3, ge=0, le=3)
    userbot_max_chats: int = Field(default=60, ge=1)
    userbot_flood_breaker_seconds: int = Field(default=300, ge=1)
    userbot_flood_breaker_hours: int = Field(default=24, ge=1)

    # --- Collectors ---------------------------------------------------------
    # One switch for all crawling; the per-source switch is sources.enabled.
    collectors_enabled: bool = True
    collector_rps_default: float = Field(default=0.3, gt=0, le=5)
    collector_daily_page_budget: int = Field(default=3000, ge=0)
    hh_user_agent: str = ""
    hh_app_token: str = ""
    brave_api_key: str = ""
    yandex_search_api_key: str = ""
    yandex_folder_id: str = ""
    google_places_api_key: str = ""
    apify_token: str = ""
    searxng_url: str = ""
    proxy_url: str = ""
    ig_graph_token: str = ""
    ig_business_account_id: str = ""
    twogis_api_key: str = ""

    # --- ERP (fullprompt) ---------------------------------------------------
    crm_push_enabled: bool = False
    crm_base_url: str = "https://erp.example.uz"
    crm_push_source: Literal["agent", "other"] = "agent"
    crm_agent_key: str = ""
    push_daily_cap: int = Field(default=25, ge=0)
    push_window: str = "09:30-17:30"
    push_days: str = "1-6"
    test_phone: str = ""

    # --- Owner bot cadence --------------------------------------------------
    review_hours: str = "09:00-18:00"
    review_burst_max: int = Field(default=10, ge=1)
    review_daily_cap: int = Field(default=40, ge=0)
    review_expiry_hours: int = Field(default=72, ge=1)
    digest_time: str = "09:00"
    quiet_hours: str = "21:00-08:00"
    alert_repeat_hours: int = Field(default=6, ge=1)

    # --- Health -------------------------------------------------------------
    heartbeat_stale_minutes: int = Field(default=10, ge=1)
    userbot_stale_minutes: int = Field(default=5, ge=1)
    disk_min_free_gb: float = Field(default=5.0, ge=0)
    # An outside dead-man's switch the worker pings; blank = off.
    deadman_ping_url: str = ""
    deadman_ping_minutes: int = Field(default=5, ge=1)

    # --- Backups ------------------------------------------------------------
    backup_dir: str = "/data/backups"
    backup_time: str = "03:45"
    backup_retention_days: int = Field(default=14, ge=1)
    # age public key (age1…). Without it nothing is written: an unencrypted
    # dump of a phone book is not a fallback.
    backup_age_recipient: str = ""
    backup_to_telegram: bool = True
    backup_max_age_hours: int = Field(default=30, ge=1)
    restore_drill_enabled: bool = True
    restore_drill_day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "sun"

    # --- Data on disk -------------------------------------------------------
    evidence_dir: str = "/data/evidence"
    evidence_max_gb: float = Field(default=30.0, ge=0)

    # --- Misc ---------------------------------------------------------------
    timezone: str = "Asia/Tashkent"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    @model_validator(mode="before")
    @classmethod
    def _no_comment_values(cls, data: object) -> object:
        """Docker Compose and python-dotenv both read ``KEY=   # note`` as the
        value "# note"; refuse it with a message that says how to fix .env."""
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and value.lstrip().startswith("#"):
                    raise ValueError(COMMENT_VALUE_ERROR.format(key=str(key).upper()))
        return data

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, v: object) -> object:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("extract_model_price", "verdict_model_price", "batch_model_price")
    @classmethod
    def _validate_price(cls, v: str) -> str:
        v = v.strip()
        if v and parse_price(v) is None:
            raise ValueError(PRICE_ERROR)
        return v

    @field_validator("backup_age_recipient")
    @classmethod
    def _validate_backup_recipient(cls, v: str) -> str:
        v = v.strip()
        if v and not v.startswith(("age1", "ssh-")):
            raise ValueError(BACKUP_RECIPIENT_ERROR)
        return v

    @field_validator("digest_time", "backup_time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        try:
            _parse_hhmm(v)
        except ValueError:
            raise ValueError(TIME_ERROR) from None
        return v.strip()

    @field_validator("review_hours", "quiet_hours", "push_window")
    @classmethod
    def _validate_range(cls, v: str) -> str:
        try:
            _parse_range(v)
        except ValueError:
            raise ValueError(RANGE_ERROR) from None
        return v.strip()

    @field_validator("push_days")
    @classmethod
    def _validate_days(cls, v: str) -> str:
        try:
            parse_days(v)
        except ValueError:
            raise ValueError(DAYS_ERROR) from None
        return v.strip()

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"nomaʼlum vaqt mintaqasi: {v}") from None
        return v.strip()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def digest_time_parsed(self) -> time:
        return _parse_hhmm(self.digest_time)

    @property
    def backup_time_parsed(self) -> time:
        return _parse_hhmm(self.backup_time)

    @property
    def review_hours_parsed(self) -> tuple[time, time]:
        return _parse_range(self.review_hours)

    @property
    def quiet_hours_parsed(self) -> tuple[time, time]:
        return _parse_range(self.quiet_hours)

    @property
    def push_window_parsed(self) -> tuple[time, time]:
        return _parse_range(self.push_window)

    @property
    def push_days_parsed(self) -> frozenset[int]:
        return parse_days(self.push_days)


def _config_error_text(exc: ValidationError) -> str:
    """One line per problem, each naming its key, without pydantic's framing."""
    lines = []
    for error in exc.errors():
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        loc = [str(part) for part in error.get("loc", ()) if isinstance(part, str)]
        key = loc[0].upper() if loc else ""
        lines.append(f"{key}: {message}" if key and key not in message else message)
    return "\n".join(lines)


@lru_cache
def get_settings() -> Settings:
    # Built at import time by every process: a bad .env must end the process
    # with the one-line reason, not a pydantic traceback.
    try:
        return Settings()
    except ValidationError as exc:
        raise SystemExit(_config_error_text(exc)) from None


settings = get_settings()
