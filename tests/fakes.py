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

Its Message Batches half (T-16) keeps batches by id: ``create`` mints one only
when the test allowed it (``accept_batches``, or a scripted refusal with
``refuse_batch``) — an unplanned submit is an unscripted request like any
other — ``retrieve`` reads a batch as the test left it (``in_progress`` until
``end``), ``results`` streams the JSONL the test wrote (``succeeded`` /
``errored`` / ``expired`` / ``canceled`` lines, in whatever order it gave), and
``cancel`` is noted. Every request inside a batch passes the same PII fence
(``batch_pii_in``: what the model reads, not the lane's own ``custom_id``).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx

from clientscrapper.collectors.base import Budget
from clientscrapper.collectors.keywords import EMAIL_RE, IG_RE, PHONE_RE, PINFL_RE, TG_RE
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
    ("14 digits", PINFL_RE),
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


def batch_pii_in(body: Any) -> list[str]:
    """``pii_in`` over a batch create: everything the model is asked — each
    request's ``params`` and the rest of the body — but not the ``custom_id``,
    the lane's own key (``'cs-' || uuid4``, 02 §2.6) that only matches a
    result to its job and is never shown to the model. A uuid's hyphens make
    a phone-shaped digit run (``65555-2015``) about one time in forty."""
    requests = body.get("requests") if isinstance(body, dict) else None
    if not isinstance(requests, list):
        return pii_in(body)
    rest = {k: v for k, v in body.items() if k != "requests"}
    shown = [r.get("params") if isinstance(r, dict) else r for r in requests]
    return pii_in([rest, shown])


def model_fixture(name: str) -> Any:
    """A SYNTHETIC structured answer from tests/fixtures/model/."""
    return json.loads((MODEL_FIXTURES / name).read_text(encoding="utf-8"))


def message_body(
    answer: Any = None,
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 300,
    output_tokens: int = 150,
    cache_read: int = 900,
    cache_write: int = 0,
    model: str | None = None,
) -> dict[str, Any]:
    """A Messages API message: ``answer`` as the text block (a dict or list is
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
    return body


def message(answer: Any = None, **fields: Any) -> httpx.Response:
    """A Messages API 200 carrying ``message_body(answer, **fields)``."""
    return httpx.Response(200, json=message_body(answer, **fields))


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


# --- the Message Batches API (T-16) ---------------------------------------------

BATCHES_PATH = "/v1/messages/batches"
_BATCH_PATH = re.compile(
    r"\A/v1/messages/batches/(?P<id>[A-Za-z0-9_]+)(?P<tail>/results|/cancel)?\Z"
)


def succeeded(answer: Any = None, **fields: Any) -> dict[str, Any]:
    """A batch result line's ``result``: the request succeeded with this
    message (``message_body``'s fields; the default model is the batch one)."""
    fields.setdefault("model", settings.batch_model)
    return {"type": "succeeded", "message": message_body(answer, **fields)}


def errored(
    kind: str = "api_error", text: str = "Internal server error"
) -> dict[str, Any]:
    return {
        "type": "errored",
        "error": {"type": "error", "error": {"type": kind, "message": text}},
    }


def expired() -> dict[str, Any]:
    return {"type": "expired"}


def canceled() -> dict[str, Any]:
    return {"type": "canceled"}


@dataclass(slots=True)
class FakeBatch:
    """One Message Batch as the fake API holds it."""

    id: str
    requests: list[dict[str, Any]]
    created_at: datetime
    status: str = "in_progress"
    # (custom_id, result) lines, in the order the test wrote them.
    results: list[tuple[str, dict[str, Any]]] | None = None
    cancelled: bool = False

    @property
    def custom_ids(self) -> list[str]:
        return [str(r["custom_id"]) for r in self.requests]

    def json(self) -> dict[str, Any]:
        ended = self.status == "ended"
        counts = {"processing": 0 if ended else len(self.requests), "succeeded": 0}
        counts |= {"errored": 0, "canceled": 0, "expired": 0}
        for _, result in self.results or ():
            counts[str(result["type"])] += 1
        return {
            "id": self.id,
            "type": "message_batch",
            "processing_status": self.status,
            "request_counts": counts,
            "created_at": self.created_at.isoformat(),
            "expires_at": (self.created_at + timedelta(hours=24)).isoformat(),
            "ended_at": self.created_at.isoformat() if ended else None,
            "results_url": (
                f"https://api.anthropic.com{BATCHES_PATH}/{self.id}/results"
                if ended and self.results is not None
                else None
            ),
        }


class FakeAnthropic:
    """The Messages API: scripted answers in order, every request kept; and
    the Message Batches API (the module docstring)."""

    def __init__(self) -> None:
        self.answers: list[ModelAnswer] = []
        self.requests: list[dict[str, Any]] = []
        self.leaks: list[list[str]] = []
        self.unexpected = 0
        self.batches: dict[str, FakeBatch] = {}
        # Every batch create's body (its requests), refused ones included.
        self.batch_bodies: list[dict[str, Any]] = []
        # Scripted answers to batch creates: None accepts and mints a batch.
        self.batch_answers: list[httpx.Response | None] = []
        # Scripted failures of retrieve / results, per batch id, used first.
        self.retrieve_failures: dict[str, list[httpx.Response]] = {}
        self.results_failures: dict[str, list[httpx.Response]] = {}
        self.retrieved: list[str] = []
        self.cancelled: list[str] = []

    def queue(self, *answers: ModelAnswer) -> FakeAnthropic:
        self.answers.extend(answers)
        return self

    def accept_batches(self, n: int = 1) -> FakeAnthropic:
        self.batch_answers.extend([None] * n)
        return self

    def refuse_batch(self, response: httpx.Response) -> FakeAnthropic:
        self.batch_answers.append(response)
        return self

    def batch(self, index: int = -1) -> FakeBatch:
        """A batch by creation order (default: the newest)."""
        return list(self.batches.values())[index]

    def end(
        self,
        batch_id: str,
        results: Mapping[str, dict[str, Any]] | Sequence[tuple[str, dict[str, Any]]],
    ) -> None:
        """The batch ended with these result lines (``custom_id`` → result,
        streamed in the order given; a custom_id left out has no line)."""
        lines = list(results.items()) if isinstance(results, Mapping) else list(results)
        found = self.batches[batch_id]
        found.status, found.results = "ended", lines

    def _batches(self, request: httpx.Request) -> httpx.Response | None:
        path = request.url.path
        if path == BATCHES_PATH and request.method == "POST":
            body = json.loads(request.content or b"{}")
            self.batch_bodies.append(body)
            found = batch_pii_in(body)
            if found:
                self.leaks.append(found)
            if not self.batch_answers:
                self.unexpected += 1
                return api_error(500, "api_error", "no scripted batch")
            answer = self.batch_answers.pop(0)
            if answer is not None:
                return httpx.Response(
                    answer.status_code, headers=answer.headers, content=answer.content
                )
            batch_id = f"msgbatch_namuna{len(self.batches) + 1:02d}"
            self.batches[batch_id] = FakeBatch(
                batch_id, list(body.get("requests") or []), T0
            )
            return httpx.Response(200, json=self.batches[batch_id].json())
        matched = _BATCH_PATH.match(path)
        if matched is None:
            return None
        batch_id, tail = matched.group("id"), matched.group("tail")
        found_batch = self.batches.get(batch_id)
        if found_batch is None:
            return api_error(404, "not_found_error", "batch not found")
        failures = (
            self.results_failures if tail == "/results" else self.retrieve_failures
        ).get(batch_id)
        if failures and tail != "/cancel":
            failure = failures.pop(0)
            return httpx.Response(
                failure.status_code, headers=failure.headers, content=failure.content
            )
        if tail == "/cancel":
            self.cancelled.append(batch_id)
            found_batch.cancelled = True
            if found_batch.status != "ended":
                found_batch.status = "canceling"
            return httpx.Response(200, json=found_batch.json())
        if tail == "/results":
            lines = [
                json.dumps({"custom_id": custom_id, "result": result})
                for custom_id, result in found_batch.results or ()
            ]
            return httpx.Response(
                200, content=("\n".join(lines) + "\n").encode() if lines else b""
            )
        self.retrieved.append(batch_id)
        return httpx.Response(200, json=found_batch.json())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        batches = self._batches(request)
        if batches is not None:
            return batches
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
