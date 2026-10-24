# VENDORED_FROM dailyJournal@6b1f67f tests/test_logs.py
"""One logging setup for every process, and its two redaction filters (06 §4.5).

No database. The handler is installed on a StringIO and taken off again, so
pytest's own capture is never disturbed. Each pattern of the 06 §4.5 table is
fed through a real handler and the line is read back.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest

from clientscrapper import logs


@pytest.fixture
def install():
    """setup_logging onto a StringIO; the root logger is restored afterwards."""
    root = logging.getLogger()
    level = root.level

    def _install(component: str = "worker", fmt: logs.LogFormat = "json") -> io.StringIO:
        stream = io.StringIO()
        handler = logs.setup_logging(component, fmt=fmt, stream=stream, force=True)
        assert handler is not None
        return stream

    yield _install
    for handler in root.handlers[:]:
        if isinstance(handler, logs.CsHandler):
            root.removeHandler(handler)
    root.setLevel(level)


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_line_per_record(install):
    stream = install("worker")
    log = logging.getLogger("clientscrapper.worker.main")

    log.info("swept %d chats", 3, extra={"chat_id": 42, "count": 3})
    log.warning("second line")

    first, second = _lines(stream)
    assert first["level"] == "INFO"
    assert first["logger"] == "clientscrapper.worker.main"
    assert first["component"] == "worker"
    assert first["msg"] == "swept 3 chats"
    assert first["chat_id"] == 42 and first["count"] == 3
    assert first["ts"].endswith("+00:00")
    assert {"ts", "level", "logger", "component", "msg"} <= set(second)


def test_json_keeps_an_exception_on_one_line(install):
    stream = install("bot")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("clientscrapper.bot").exception("failed", extra={"push_id": 7})

    (line,) = _lines(stream)
    assert line["push_id"] == 7
    assert "RuntimeError: boom" in line["exc"]
    assert stream.getvalue().count("\n") == 1


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_body_shaped_extras_are_dropped(install, fmt):
    stream = install("userbot", fmt)
    bodies = {name: f"secret-{name}" for name in logs.REDACTED_EXTRAS}

    logging.getLogger("clientscrapper.userbot").warning(
        "stored", extra={**bodies, "interaction_id": 9}
    )

    assert "secret-" not in stream.getvalue()
    if fmt == "json":
        (line,) = _lines(stream)
        assert line["interaction_id"] == 9
        assert not set(line) & logs.REDACTED_EXTRAS


def test_the_body_list_is_the_spec_list():
    spec = {
        "text", "body", "raw_text", "transcript", "draft", "phone", "phones", "note",
        "payload", "headers", "key", "secret", "token", "session", "html", "quote", "bio",
    }  # fmt: skip
    assert spec == logs.REDACTED_EXTRAS


# Synthetic values only (07 §1.0): +998 90 000 00 NN, namuna_<n>.
BOT_TOKEN = "1234567890:" + "A" * 35
ANTHROPIC_KEY = "sk-ant-" + "x" * 24


@pytest.mark.parametrize(
    ("message", "leaked", "kept"),
    [
        ("call +998 90 000 00 01 now", "000 00 01", "<phone>"),
        ("call 998900000002 now", "998900000002", "<phone>"),
        ("call +998-90-000-00-03", "000-00-03", "<phone>"),
        ("sent X-ERP-Key: abc123secret", "abc123secret", "X-ERP-Key=<key>"),
        ("header x-gsr-key=zzzsecret", "zzzsecret", "x-gsr-key=<key>"),
        ("Authorization: Bearer-tokenvalue", "Bearer-tokenvalue", "Authorization=<key>"),
        # The scheme word is not the credential: the token after it goes too.
        (
            "Authorization: Bearer apify_api_namuna1",
            "apify_api_namuna1",
            "Authorization=<key>",
        ),
        ("authorization: Api-Key AQVNnamunakey", "AQVNnamunakey", "authorization=<key>"),
        (
            "Proxy-Authorization: Basic bmFtdW5hOm5hbXVuYQ==",
            "bmFtdW5hOm5hbXVuYQ",
            "Proxy-Authorization=<key>",
        ),
        (
            "headers {'Authorization': 'Bearer namuna.jwt.sig', 'n': 1}",
            "namuna.jwt.sig",
            "Authorization=<key>",
        ),
        ('sent {"X-GSR-Key": "namunasecret"}', "namunasecret", "X-GSR-Key=<key>"),
        (f"model key {ANTHROPIC_KEY}", ANTHROPIC_KEY, "<secret>"),
        (f"bot {BOT_TOKEN} failed", BOT_TOKEN, "<secret>"),
        ("TELETHON_SESSION=1BVtsOKABu0abc", "1BVtsOKABu0abc", "<secret>"),
        (
            "db postgresql+psycopg://cs:hunter2pass@db:5432/cs down",
            "hunter2pass",
            "postgresql+psycopg://cs:***@db:5432/cs",
        ),
        ("poster @namuna_01 wrote", "namuna_01", "@…"),
    ],
)
@pytest.mark.parametrize("fmt", ["json", "text"])
def test_patterns_are_rewritten_in_the_message(install, fmt, message, leaked, kept):
    stream = install("worker", fmt)
    logging.getLogger("clientscrapper.x").info(message)
    out = stream.getvalue()
    assert leaked not in out
    assert kept in out


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_the_values_of_other_extras_are_redacted(install, fmt):
    stream = install("worker", fmt)
    dsn = "postgresql+psycopg://cs:hunter2namuna@db:5432/cs"
    logging.getLogger("clientscrapper.x").warning(
        "db down",
        extra={
            "error": f"OperationalError: {dsn}",
            "sender": "+998 90 000 00 07",
            "context": {"where": "@namuna_04", "tries": [3, "+998900000008"]},
            "request": {"Authorization": "Bearer namunatoken9", "text": "namuna matn"},
            "attempt": 3,
        },
    )
    out = stream.getvalue()
    leaks = ("hunter2namuna", "000 00 07", "namuna_04", "900000008", "namunatoken9",
             "namuna matn")  # fmt: skip
    for leaked in leaks:
        assert leaked not in out
    if fmt == "json":
        (line,) = _lines(stream)
        assert line["error"] == (
            "OperationalError: postgresql+psycopg://cs:***@db:5432/cs"
        )
        assert line["sender"] == "<phone>"
        assert line["context"] == {"where": "@…", "tries": [3, "<phone>"]}
        assert line["request"] == {"Authorization": "<key>", "text": "<redacted>"}
        assert line["attempt"] == 3


def test_an_extra_that_is_not_json_is_redacted_as_printed(install):
    class Raw:
        def __str__(self) -> str:
            return "dsn postgresql://cs:hunter3namuna@db/cs"

    stream = install()
    logging.getLogger("clientscrapper.x").info("x", extra={"thing": Raw()})
    (line,) = _lines(stream)
    assert line["thing"] == "dsn postgresql://cs:***@db/cs"


def test_redacting_an_extra_leaves_the_callers_object_alone():
    context = {"phone_like": "+998 90 000 00 09"}
    assert logs.redact_value(context) == {"phone_like": "<phone>"}
    assert context == {"phone_like": "+998 90 000 00 09"}


def test_arguments_are_redacted_too(install):
    stream = install()
    logging.getLogger("clientscrapper.x").info("phone %s", "+998 90 000 00 04")
    (line,) = _lines(stream)
    assert line["msg"] == "phone <phone>"


def test_a_handle_inside_a_url_is_left_alone(install):
    stream = install()
    logging.getLogger("clientscrapper.x").info(
        "fetched https://t.me/s/namuna_02 for @namuna_03"
    )
    (line,) = _lines(stream)
    assert line["msg"] == "fetched https://t.me/s/namuna_02 for @…"


def test_a_traceback_is_redacted(install):
    stream = install()
    try:
        raise ValueError("bad number +998 90 000 00 05")
    except ValueError:
        logging.getLogger("clientscrapper.x").exception("failed")
    (line,) = _lines(stream)
    assert "000 00 05" not in line["exc"]
    assert "<phone>" in line["exc"]


def test_the_installed_handler_carries_both_filters(install):
    install("userbot")
    handlers = logging.getLogger().handlers
    handler = next(h for h in handlers if isinstance(h, logs.CsHandler))
    kinds = {type(f) for f in handler.filters}
    assert {logs.RedactBodies, logs.RedactPatterns} <= kinds
