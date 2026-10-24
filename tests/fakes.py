"""Test doubles shared across files (07 §1.0).

``FakeClock`` drives a ``Fetcher``'s monotonic clock, wall clock and sleep:
sleeping advances time instantly and is recorded. ``Site`` is an
``httpx.MockTransport`` router: a path (or a full ``https://host/path``)
answers from a queue of responses or a callable, every request is kept with
the fake time it arrived at, and an unknown URL is a 404 — so no test can
reach the network. ``fetcher_for`` builds a ``Fetcher`` over both.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from clientscrapper.collectors.base import Budget
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
    """Routes keyed by ``https://host/path`` (query ignored). A list answers in
    order and repeats its last item; a callable answers every time."""

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
        key = f"{request.url.scheme}://{request.url.host}{request.url.path}"
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
