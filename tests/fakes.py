"""Test doubles shared across files (07 §1.0).

``FakeClock`` drives a ``Fetcher``'s monotonic clock, wall clock and sleep:
sleeping advances time instantly and is recorded. ``Site`` is an
``httpx.MockTransport`` router: a path (or a full ``https://host/path``)
answers from a queue of responses or a callable, every request is kept with
the fake time it arrived at, and an unknown URL is a 404 — so no test can
reach the network. ``fetcher_for`` builds a ``Fetcher`` over both.

``FakeAnthropic`` is the Messages API as an ``httpx.MockTransport`` (06 §5.2,
T-15): it answers from a queue of scripted responses — hand-written and
SYNTHETIC (``tests/fixtures/model/README.md``; real recordings are a day-1
task on the VPS, 07 §5.5) — keeps every request body, and notes any request
whose strings still match ``PHONE_RE``/``TG_RE``/``IG_RE``/``EMAIL_RE`` or
carry a 14-digit run, and any request nobody scripted an answer for.
``tests/conftest.py`` installs one on ``services/extraction`` for every test
and fails the test that leaked or called unscripted.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx

from clientscrapper.collectors.base import Budget
from clientscrapper.collectors.keywords import EMAIL_RE, IG_RE, PHONE_RE, TG_RE
from clientscrapper.config import settings
from clientscrapper.services.http import Fetcher, HostState

TASHKENT = ZoneInfo("Asia/Tashkent")
T0 = datetime(2026, 10, 24, 10, 0, tzinfo=TASHKENT)

Answer = httpx.Response | Callable[[httpx.Request], httpx.Response]


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.start = start
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.t)

    def advance(self, seconds: float) -> None:
        self.t += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(seconds, 0.0)
        await asyncio.sleep(0)


@dataclass(slots=True)
class Seen:
    at: float
    request: httpx.Request

    @property
    def url(self) -> str:
        return str(self.request.url)

    @property
    def path(self) -> str:
        return self.request.url.path


class Site:
    """Routes keyed by ``https://host[:port]/path`` (query ignored; a default
    port is not written). A list answers in order and repeats its last item;
    a callable answers every time."""

    def __init__(self, clock: FakeClock, routes: dict[str, Any] | None = None) -> None:
        self.clock = clock
        self.routes: dict[str, Any] = {}
        self.seen: list[Seen] = []
        for key, answer in (routes or {}).items():
            self.add(key, answer)

    def add(self, key: str, answer: Any) -> None:
        if isinstance(answer, list):
            self.routes[key] = list(answer)
        else:
            self.routes[key] = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(Seen(self.clock.t, request))
        port = f":{request.url.port}" if request.url.port is not None else ""
        key = f"{request.url.scheme}://{request.url.host}{port}{request.url.path}"
        answer = self.routes.get(key)
        if answer is None:
            return httpx.Response(404, text="not found")
        if isinstance(answer, list):
            current = answer.pop(0) if len(answer) > 1 else answer[0]
        else:
            current = answer
        if callable(current) and not isinstance(current, httpx.Response):
            response: httpx.Response = current(request)
            return response
        assert isinstance(current, httpx.Response)
        # A Response can be read once; hand out a copy each time.
        return httpx.Response(
            current.status_code, headers=current.headers, content=current.content
        )

    def paths(self, host: str | None = None) -> list[str]:
        return [s.path for s in self.seen if host is None or s.request.url.host == host]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def fetcher_for(site: Site, **kwargs: Any) -> Fetcher:
    clock = site.clock
    kwargs.setdefault("hosts", HostState())
    return Fetcher(
        transport=site.transport(),
        monotonic=clock.monotonic,
        wall=clock.now,
        sleep=clock.sleep,
        **kwargs,
    )


def budget(
    pages: int = 100, usd: str = "0", *, deadline: datetime | None = None
) -> Budget:
    return Budget(
        pages=pages,
        usd=Decimal(usd),
        deadline=deadline or T0 + timedelta(days=30),
    )


ALLOW_ALL = httpx.Response(200, text="User-agent: *\nDisallow:\n")


# --- the Messages API (T-15) -----------------------------------------------------

MODEL_FIXTURES = Path(__file__).parent / "fixtures" / "model"
PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PHONE_RE", PHONE_RE),
    ("TG_RE", TG_RE),
    ("IG_RE", IG_RE),
    ("EMAIL_RE", EMAIL_RE),
    ("14 digits", re.compile(r"(?<!\d)\d{14}(?!\d)")),
)
GRAMMAR_MESSAGE = "Grammar compilation timed out for the provided schema"


def strings_of(value: Any) -> Iterator[str]:
    """Every string inside a decoded JSON body, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_of(item)


def pii_in(body: Any) -> list[str]:
    """The patterns a request body still matches — the pattern's name, never
    the value (a failing test must not print what it protects)."""
    return sorted(
        {
            name
            for text in strings_of(body)
            for name, pattern in PII_PATTERNS
            if pattern.search(text)
        }
    )


def model_fixture(name: str) -> Any:
    """A SYNTHETIC structured answer from tests/fixtures/model/."""
    return json.loads((MODEL_FIXTURES / name).read_text(encoding="utf-8"))


def message(
    answer: Any = None,
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 300,
    output_tokens: int = 150,
    cache_read: int = 900,
    cache_write: int = 0,
    model: str | None = None,
) -> httpx.Response:
    """A Messages API 200: ``answer`` as the text block (a dict or list is
    written as JSON, a str as it is, None = no content at all)."""
    if answer is None:
        content: list[dict[str, Any]] = []
    else:
        text = answer if isinstance(answer, str) else json.dumps(answer)
        content = [{"type": "text", "text": text}]
    body = {
        "id": "msg_synthetic",
        "type": "message",
        "role": "assistant",
        "model": model or settings.extract_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
        },
    }
    return httpx.Response(200, json=body)


def api_error(status: int, kind: str, text: str) -> httpx.Response:
    return httpx.Response(
        status, json={"type": "error", "error": {"type": kind, "message": text}}
    )


def grammar_failure() -> httpx.Response:
    """The 400 a schema too big for the grammar compiler gets (MIYA)."""
    return api_error(400, "invalid_request_error", GRAMMAR_MESSAGE)


def overloaded() -> httpx.Response:
    return api_error(529, "overloaded_error", "Overloaded")


ModelAnswer = httpx.Response | Callable[[httpx.Request], httpx.Response]


class FakeAnthropic:
    """The Messages API: scripted answers in order, every request kept."""

    def __init__(self) -> None:
        self.answers: list[ModelAnswer] = []
        self.requests: list[dict[str, Any]] = []
        self.leaks: list[list[str]] = []
        self.unexpected = 0

    def queue(self, *answers: ModelAnswer) -> FakeAnthropic:
        self.answers.extend(answers)
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        found = pii_in(body)
        if found:
            self.leaks.append(found)
        if not self.answers:
            self.unexpected += 1
            return api_error(500, "api_error", "no scripted answer")
        answer = self.answers.pop(0)
        if callable(answer) and not isinstance(answer, httpx.Response):
            return answer(request)
        return httpx.Response(
            answer.status_code, headers=answer.headers, content=answer.content
        )

    def client(self) -> anthropic.AsyncAnthropic:
        """An SDK client over this transport. No retries of the SDK's own: a
        scripted 5xx is one request, so a test counts what it planted."""
        return anthropic.AsyncAnthropic(
            api_key="test-key-not-a-credential",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self)),
        )
