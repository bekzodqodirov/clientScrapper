# VENDORED_FROM dailyJournal@6b1f67f miya/services/retry.py
"""One backoff helper for calls that can fail for a moment (MIYA JV-02, as-is).

``with_backoff`` retries an async call with full-jitter exponential backoff:
before attempt ``n + 1`` it sleeps a uniform random time between zero and
``min(cap, base * 2 ** (n - 1))`` seconds. Only the failures ``retry_on``
accepts are retried; anything else, and the last failure, reaches the caller
unchanged, so each caller keeps its own fallback.

A log line names the attempt number and the error class, never the error's
message: an HTTP error can quote the body that was sent.

Users here: ``services/http.Fetcher`` (03 §1.1: attempts 3, base 2.0, cap
60.0, ``retry_on=is_transient_http``) and the ERP push (05 §B.1). Model calls
are not wrapped: the shared client retries by itself, and wrapping it again
would retry twice. The one exception is the batch create
(``model_jobs._create_batch``): it is not idempotent, so it runs with the
client's retries off and is wrapped here to repeat only a create the API
turned away before taking it.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

log = logging.getLogger(__name__)

T = TypeVar("T")

RetryOn = Callable[[BaseException], bool] | tuple[type[BaseException], ...]
Sleep = Callable[[float], Awaitable[None]]

# Status codes that mean "try again later" rather than "this request is wrong".
TOO_MANY_REQUESTS = 429
SERVER_ERROR_FROM = 500

# The exponent stops growing here: past it ``base * 2 ** n`` is far above any
# cap, and an unbounded power overflows a float once a supervisor has
# restarted a task about a thousand times.
MAX_DOUBLINGS = 62


def backoff_delay(attempt: int, *, base: float, cap: float) -> float:
    """The full-jitter wait after failed attempt ``attempt`` (1-based)."""
    doublings = min(attempt - 1, MAX_DOUBLINGS)
    return random.uniform(0.0, min(cap, base * 2**doublings))


def _accepts(retry_on: RetryOn, exc: BaseException) -> bool:
    if isinstance(retry_on, tuple):
        return isinstance(exc, retry_on)
    return retry_on(exc)


async def with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base: float,
    cap: float,
    retry_on: RetryOn,
    sleep: Sleep | None = None,
) -> T:
    """Await ``fn()`` up to ``attempts`` times; see the module docstring."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    wait = sleep if sleep is not None else asyncio.sleep
    attempt = 1
    while True:
        try:
            return await fn()
        except Exception as exc:
            if not _accepts(retry_on, exc):
                raise
            error = type(exc).__name__
            if attempt >= attempts:
                log.warning(
                    "giving up after attempt %d: %s",
                    attempt,
                    error,
                    extra={"attempt": attempt, "error": error},
                )
                raise
            log.info(
                "attempt %d failed (%s); retrying",
                attempt,
                error,
                extra={"attempt": attempt, "error": error},
            )
            await wait(backoff_delay(attempt, base=base, cap=cap))
            attempt += 1


class TransientStatus(Exception):
    """A 429 or 5xx response, raised so ``with_backoff`` can retry it."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"transient status {response.status_code}")
        self.response = response


def is_transient_status(code: int) -> bool:
    return code == TOO_MANY_REQUESTS or code >= SERVER_ERROR_FROM


def raise_for_transient_status(response: httpx.Response) -> httpx.Response:
    """Raise ``TransientStatus`` for a 429 or 5xx; return any other response."""
    if is_transient_status(response.status_code):
        raise TransientStatus(response)
    return response


def is_transient_http(exc: BaseException) -> bool:
    """A timeout, a dropped connection, a 429 or a 5xx: worth another try.

    A 4xx other than 429 is the request's own fault and is never retried.
    """
    if isinstance(exc, TransientStatus):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return is_transient_status(exc.response.status_code)
    return isinstance(
        exc, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError
    )
