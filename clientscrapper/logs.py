# VENDORED_FROM dailyJournal@6b1f67f miya/logs.py
"""Log lines for every process (06 §4.5).

``setup_logging(component)`` is the one place a process configures logging.
With ``LOG_FORMAT=json`` each record becomes one JSON object on one line,
carrying ``ts``, ``level``, ``logger``, ``component``, ``msg`` and the
record's extras (ids and counts); ``LOG_FORMAT=text`` keeps the plain
``time level logger: message`` lines.

Logs carry ids and counts, never bodies, names, phones, handles, quotes,
payloads, headers or any `.env` value. Two filters are the backstop for a
careless call: ``RedactBodies`` drops an extra named like a body before any
formatter sees it, and ``RedactPatterns`` rewrites what still slipped into the
message, the traceback or the value of any other extra (a phone, a key header,
a token, a DSN password, a handle). Both run in either format.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

from clientscrapper.config import settings

LogFormat = Literal["json", "text"]

TEXT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Extras that would carry what someone wrote or a credential; never logged.
REDACTED_EXTRAS = frozenset(
    {
        "text",
        "body",
        "raw_text",
        "transcript",
        "draft",
        "phone",
        "phones",
        "note",
        "payload",
        "headers",
        "key",
        "secret",
        "token",
        "session",
        "html",
        "quote",
        "bio",
    }
)

_KEY_NAMES = r"x-gsr-key|x-erp-key|proxy-authorization|authorization|x-api-key"
_AUTH_SCHEME = r"(?:bearer|basic|digest|token|api-key|bot)\s+"
_KEY_HEADER = re.compile(
    rf"(?i)({_KEY_NAMES})[\"']?\s*[=:]\s*"
    rf"(?:([\"'])(?:{_AUTH_SCHEME})?[^\"']*\2|(?:{_AUTH_SCHEME})?\S+)"
)

# Applied in this order: credentials first, so a token is never half-eaten by
# the phone rule; handles last, and only outside URLs (a DSN's «@host» or a
# link's «user@» is not a Telegram handle).
_SECRET_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "<secret>"),
    (re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}"), "<secret>"),
    (re.compile(r"(?i)telethon_session=\S+"), "<secret>"),
    # A key header with its whole value: the auth scheme too («Authorization:
    # Bearer <t>», Apify; «Api-Key <k>», Yandex), and a quoted value as a dict's
    # repr prints it. 06 §4.5's «[=:]\s*\S+» stopped at the scheme word and
    # logged the credential after it.
    (_KEY_HEADER, r"\1=<key>"),
    (re.compile(r"postgresql(\+\w+)?://([^:/]+):([^@]+)@"), r"postgresql\1://\2:***@"),
    (re.compile(r"\+?998[\s-]?\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}"), "<phone>"),
)
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_HANDLE_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{4,}")

# Attributes every LogRecord has; anything else on a record came from extra=.
_RECORD_ATTRS = frozenset(
    {
        *logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__,
        "message",
        "asctime",
    }
)


def redact(text: str) -> str:
    """The 06 §4.5 pattern table applied to one string."""
    for pattern, replacement in _SECRET_RULES:
        text = pattern.sub(replacement, text)
    out: list[str] = []
    last = 0
    for url in _URL_RE.finditer(text):
        out.append(_HANDLE_RE.sub("@…", text[last : url.start()]))
        out.append(url.group(0))
        last = url.end()
    out.append(_HANDLE_RE.sub("@…", text[last:]))
    return "".join(out)


class RedactBodies(logging.Filter):
    """Drop body-shaped extras from the record before it is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name in REDACTED_EXTRAS:
            record.__dict__.pop(name, None)
        return True


_KEY_HEADER_NAMES = frozenset(_KEY_NAMES.split("|"))


def _redact_item(key: Any, value: Any) -> Any:
    name = key.lower() if isinstance(key, str) else None
    if name in _KEY_HEADER_NAMES:
        return "<key>"
    if name in REDACTED_EXTRAS:
        return "<redacted>"
    return redact_value(value)


def redact_value(value: Any) -> Any:
    """``redact`` through an extra's value: strings, the strings inside lists,
    tuples and dicts (keys too; under a key-header or body-shaped key the whole
    value goes), and the ``str()`` of anything JSON cannot carry, since that is
    what the formatter would print. Numbers, booleans and None are ids and
    counts and stay as they are. Never mutates its input."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact_value(k): _redact_item(k, v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_value(v) for v in value]
    return redact(str(value))


class RedactPatterns(logging.Filter):
    """Rewrite the message, the traceback and every extra through ``redact``.

    The message is rendered once here (``msg % args``) and stored back with no
    args, so no formatter can re-insert an argument that carried a phone. An
    extra not on the body list is kept, its value redacted (``error=str(exc)``
    can carry a DSN, ``sender=…`` a phone).
    """

    def __init__(self) -> None:
        super().__init__()
        self._exc_formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._exc_formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        for key, value in list(record.__dict__.items()):
            if key not in _RECORD_ATTRS and not key.startswith("_"):
                record.__dict__[key] = redact_value(value)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record; extras that are not JSON become strings."""

    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "component": self.component,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if (
                key in _RECORD_ATTRS
                or key in REDACTED_EXTRAS
                or key in line
                or key.startswith("_")
            ):
                continue
            line[key] = value
        if record.exc_text:
            line["exc"] = record.exc_text
        elif record.exc_info:
            line["exc"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            line["stack"] = self.formatStack(record.stack_info)
        return json.dumps(line, ensure_ascii=False, default=str)


class CsHandler(logging.StreamHandler[TextIO]):
    """The handler ``setup_logging`` installs; a second call replaces it."""


def setup_logging(
    component: str,
    *,
    fmt: LogFormat | None = None,
    stream: TextIO | None = None,
    force: bool = False,
) -> CsHandler | None:
    """Configure the root logger for one process.

    Like ``logging.basicConfig`` it leaves logging alone when something else
    already put a handler on the root logger (pytest's capture), unless
    ``force`` is set; an earlier handler of ours is replaced, so calling it
    twice is harmless. Other handlers are never removed.
    Returns the installed handler, or None when nothing was changed.
    """
    root = logging.getLogger()
    if not force and any(not isinstance(h, CsHandler) for h in root.handlers):
        return None
    for old in [h for h in root.handlers if isinstance(h, CsHandler)]:
        root.removeHandler(old)
        old.close()
    handler = CsHandler(stream if stream is not None else sys.stderr)
    handler.addFilter(RedactBodies())
    handler.addFilter(RedactPatterns())
    chosen = fmt if fmt is not None else settings.log_format
    if chosen == "json":
        handler.setFormatter(JsonFormatter(component))
    else:
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(settings.log_level)
    return handler
