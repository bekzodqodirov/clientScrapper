# VENDORED_FROM dailyJournal@6b1f67f tests/test_retry.py
"""The backoff helper (03 §1.1 Backoff), MIYA's tests for it as-is.

No database, no network and no real waiting: the sleep is a recorder. MIYA's
call-site tests (transcription, embeddings, gcal, the model client) stay
behind with the code they test; the Fetcher's own use is pinned in
``tests/collectors/test_http.py``.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from clientscrapper.services import retry

REQUEST = httpx.Request("POST", "https://example.invalid/")


class Sleeps:
    """Stands in for asyncio.sleep and remembers each wait."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class Flaky:
    """Fails with each of `errors` in turn, then returns `result`."""

    def __init__(self, errors: list[Exception], result: str = "ok") -> None:
        self.errors = list(errors)
        self.result = result
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


async def test_backoff_retries_transient_then_succeeds(monkeypatch):
    # The jitter is pinned to its upper bound, so the waits show the curve.
    monkeypatch.setattr(retry.random, "uniform", lambda low, high: high)
    sleeps = Sleeps()
    fn = Flaky([httpx.ConnectError("down", request=REQUEST), httpx.ReadTimeout("slow")])

    result = await retry.with_backoff(
        fn,
        attempts=4,
        base=1.0,
        cap=30.0,
        retry_on=retry.is_transient_http,
        sleep=sleeps,
    )

    assert result == "ok"
    assert fn.calls == 3
    assert sleeps.waits == [1.0, 2.0]


async def test_backoff_gives_up_on_permanent():
    sleeps = Sleeps()
    fn = Flaky([ValueError("bad request")])

    with pytest.raises(ValueError):
        await retry.with_backoff(
            fn,
            attempts=5,
            base=1.0,
            cap=30.0,
            retry_on=retry.is_transient_http,
            sleep=sleeps,
        )

    assert fn.calls == 1
    assert sleeps.waits == []


async def test_backoff_stops_after_the_last_attempt():
    sleeps = Sleeps()
    errors: list[Exception] = [httpx.ConnectError("down") for _ in range(5)]
    fn = Flaky(errors)

    with pytest.raises(httpx.ConnectError):
        await retry.with_backoff(
            fn,
            attempts=3,
            base=1.0,
            cap=30.0,
            retry_on=(httpx.ConnectError,),
            sleep=sleeps,
        )

    assert fn.calls == 3
    assert len(sleeps.waits) == 2


async def test_backoff_never_retries_cancellation():
    async def cancelled() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retry.with_backoff(
            cancelled,
            attempts=3,
            base=1.0,
            cap=1.0,
            retry_on=lambda exc: True,
            sleep=Sleeps(),
        )


def test_backoff_delay_is_full_jitter_and_capped(monkeypatch):
    bounds: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return high

    monkeypatch.setattr(retry.random, "uniform", uniform)
    delays = [retry.backoff_delay(n, base=0.5, cap=3.0) for n in range(1, 6)]

    assert delays == [0.5, 1.0, 2.0, 3.0, 3.0]
    assert all(low == 0.0 for low, _ in bounds)


def test_backoff_delay_survives_a_huge_attempt_number():
    assert 0.0 <= retry.backoff_delay(10_000, base=1.0, cap=5.0) <= 5.0


async def test_backoff_refuses_zero_attempts():
    with pytest.raises(ValueError):
        await retry.with_backoff(
            Flaky([]), attempts=0, base=1.0, cap=1.0, retry_on=(OSError,)
        )


async def test_backoff_logs_only_the_attempt_and_the_error_class(caplog):
    fn = Flaky([httpx.ConnectError("secret body 998877"), httpx.ConnectError("again")])

    with (
        caplog.at_level(logging.INFO, logger="clientscrapper.services.retry"),
        pytest.raises(httpx.ConnectError),
    ):
        await retry.with_backoff(
            fn,
            attempts=2,
            base=1.0,
            cap=1.0,
            retry_on=retry.is_transient_http,
            sleep=Sleeps(),
        )

    records = [r for r in caplog.records if r.name == "clientscrapper.services.retry"]
    assert [r.attempt for r in records] == [1, 2]
    assert {r.error for r in records} == {"ConnectError"}
    assert "secret body" not in caplog.text
    assert "998877" not in caplog.text


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (httpx.ConnectError("x"), True),
        (httpx.ReadTimeout("x"), True),
        (httpx.RemoteProtocolError("x"), True),
        (retry.TransientStatus(httpx.Response(503)), True),
        (
            httpx.HTTPStatusError("x", request=REQUEST, response=httpx.Response(429)),
            True,
        ),
        (
            httpx.HTTPStatusError("x", request=REQUEST, response=httpx.Response(500)),
            True,
        ),
        (
            httpx.HTTPStatusError("x", request=REQUEST, response=httpx.Response(400)),
            False,
        ),
        (
            httpx.HTTPStatusError("x", request=REQUEST, response=httpx.Response(404)),
            False,
        ),
        (httpx.UnsupportedProtocol("x"), False),
        (ValueError("x"), False),
    ],
)
def test_is_transient_http(exc, transient):
    assert retry.is_transient_http(exc) is transient


@pytest.mark.parametrize(("code", "raises"), [(200, False), (400, False), (429, True)])
def test_raise_for_transient_status(code, raises):
    response = httpx.Response(code)
    if raises:
        with pytest.raises(retry.TransientStatus) as caught:
            retry.raise_for_transient_status(response)
        assert caught.value.response is response
    else:
        assert retry.raise_for_transient_status(response) is response
