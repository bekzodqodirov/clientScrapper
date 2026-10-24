# VENDORED_FROM dailyJournal@6b1f67f tests/test_env_example.py
"""`.env.example` must be copyable to `.env` verbatim and start cleanly (07 §1.2).

No database. Docker Compose and python-dotenv both read ``KEY=   # note`` as
the value "# note", which crashed every container on MIYA's first `make up`.
The file and ``Settings`` are held equal in both directions, and the doctor's
constant is held equal to the file, so a key added in one place and not the
others is red here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from clientscrapper import config
from clientscrapper.config import Settings, parse_price
from clientscrapper.tools import doctor

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
ASSIGNMENT = re.compile(r"^([A-Z0-9_]+)=(.*)$")

# Keys 03 adds to the shared env list (03 Open question 3, 07 §1.2).
COLLECTOR_ADDITIONS = (
    "IG_GRAPH_TOKEN",
    "IG_BUSINESS_ACCOUNT_ID",
    "TWOGIS_API_KEY",
    "HH_APP_TOKEN",
    "EVIDENCE_DIR",
    "TEST_PHONE",
)

# What a real credential looks like; none may sit in the example (06 §3.4).
SECRET_SHAPES = (
    re.compile(r"sk-ant-"),
    re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}"),
    re.compile(r"age1[a-z0-9]{58}"),
    re.compile(r"^1[A-Za-z0-9+/=_-]{300,}"),
)


def _lines() -> list[str]:
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def _keys() -> list[str]:
    return [m.group(1) for line in _lines() if (m := ASSIGNMENT.match(line))]


def _values() -> dict[str, str]:
    return {m.group(1): m.group(2) for line in _lines() if (m := ASSIGNMENT.match(line))}


def test_no_value_in_env_example_is_a_comment():
    values = dotenv_values(ENV_EXAMPLE)
    bad = [k for k, v in values.items() if v and v.lstrip().startswith("#")]
    assert bad == []


def test_no_assignment_line_carries_an_inline_comment():
    pattern = re.compile(r"^[A-Z0-9_]+=.*\s#")
    assert [line for line in _lines() if pattern.match(line)] == []


def test_every_line_is_a_comment_an_assignment_or_blank():
    odd = [
        line
        for line in _lines()
        if line.strip() and not line.startswith("#") and not ASSIGNMENT.match(line)
    ]
    assert odd == []


def test_keys_are_unique():
    keys = _keys()
    assert len(keys) == len(set(keys))


def test_the_database_password_is_the_placeholder_never_blank():
    # Compose's ${POSTGRES_PASSWORD:?required} treats an empty value as unset,
    # which would fail `docker compose config` in CI; the doctor refuses the
    # placeholder before a real make up.
    values = _values()
    assert values["POSTGRES_PASSWORD"] == doctor.PASSWORD_PLACEHOLDER
    assert f":{doctor.PASSWORD_PLACEHOLDER}@" in values["DATABASE_URL"]


def test_optional_switches_ship_off_or_blank():
    values = _values()
    assert values["SEARXNG_URL"] == ""  # searxng is an optional compose profile
    assert values["TEST_PHONE"] == ""
    assert values["CRM_PUSH_ENABLED"] == "false"


def test_prices_and_the_claude_cap_have_no_env_line():
    # Their home is code: MODEL_PRICES in usage.py, FAMILY_CAPS in costs.py.
    keys = set(_keys())
    assert "MODEL_PRICES" not in keys
    assert "CLAUDE_DAILY_CAP_USD" not in keys


def test_no_value_looks_like_a_real_secret():
    for key, value in _values().items():
        for shape in SECRET_SHAPES:
            assert not shape.search(value), key


def test_every_settings_field_has_a_line_and_every_line_is_a_field():
    fields = {name.upper() for name in Settings.model_fields}
    keys = set(_keys())
    assert fields - keys == set(), "fields with no .env.example line"
    assert keys - fields == set(), ".env.example lines with no Settings field"


def test_the_collector_additions_are_in_the_list():
    assert set(COLLECTOR_ADDITIONS) <= set(_keys())


def test_the_doctor_expects_exactly_the_example_keys():
    assert list(doctor.EXPECTED_ENV_KEYS) == _keys()


def test_settings_build_from_env_example_with_blank_credentials(monkeypatch):
    for key in ("TELETHON_API_ID", "OWNER_TELEGRAM_ID", "MANAGER_TELEGRAM_IDS"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=str(ENV_EXAMPLE))
    assert s.telethon_api_id is None
    assert s.owner_telegram_id is None
    assert s.manager_telegram_ids == ()
    assert s.anthropic_api_key == ""
    assert s.push_days_parsed == frozenset(range(1, 7))


def test_a_comment_shaped_value_is_refused_with_the_key_named():
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, assistant_bot_token="# @BotFather → /newbot")
    assert "ASSISTANT_BOT_TOKEN" in str(caught.value)


@pytest.mark.parametrize(
    ("value", "ok"),
    [("age1abc", True), ("", True), ("ssh-ed25519 AAAA", True), ("xyz", False)],
)
def test_backup_recipient_must_look_like_an_age_key(value, ok):
    if ok:
        Settings(_env_file=None, backup_age_recipient=value)
    else:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, backup_age_recipient=value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.10/0.50", ("0.10", "0.50", None, None)),
        ("0.10/0.50/0.01/0.125", ("0.10", "0.50", "0.01", "0.125")),
        ("0.10,0.50", None),
        ("0.10/0.50/0.01", None),
        ("-1/2", None),
        ("a/b", None),
        ("", None),
    ],
)
def test_parse_price_reads_in_out_and_the_optional_cache_prices(text, expected):
    price = parse_price(text)
    if expected is None:
        assert price is None
    else:
        assert price is not None
        rates = (price.input, price.output, price.cache_read, price.cache_write)
        assert tuple(None if v is None else str(v) for v in rates) == expected
        # An override names no long-context tier; resolve_price lends the
        # table's to the override's own model (tests/test_usage.py).
        assert (price.long_above, price.long_input, price.long_output) == (None,) * 3


def test_an_unreadable_price_override_is_refused():
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, verdict_model_price="2,10")
    assert "verdict_model_price" in str(caught.value)


def test_manager_ids_are_a_comma_separated_list(monkeypatch):
    monkeypatch.setenv("MANAGER_TELEGRAM_IDS", "111111111, 222222222")
    assert Settings(_env_file=None).manager_telegram_ids == (111111111, 222222222)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PUSH_WINDOW", "9-17"),
        ("PUSH_DAYS", "0-6"),
        ("DIGEST_TIME", "25:00"),
        ("RESTORE_DRILL_DAY", "sunday"),
        ("TIMEZONE", "Asia/Nowhere"),
    ],
)
def test_bad_schedule_values_are_refused(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("key", "value"),
    [("OWNER_TELEGRAM_ID", "not-a-number"), ("MANAGER_TELEGRAM_IDS", "111,abc")],
)
def test_get_settings_exits_with_one_line_naming_the_key(monkeypatch, key, value):
    config.get_settings.cache_clear()
    monkeypatch.setenv(key, value)
    try:
        with pytest.raises(SystemExit) as caught:
            config.get_settings()
        text = str(caught.value)
        assert key in text
        assert "Traceback" not in text
        assert len(text.splitlines()) == 1
    finally:
        monkeypatch.delenv(key)
        config.get_settings.cache_clear()
