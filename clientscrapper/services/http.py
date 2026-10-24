"""The only way a collector touches the network (03 §1.1).

``Fetcher`` wraps one ``httpx.AsyncClient`` and adds, per request: our
``USER_AGENT`` (never replaced), robots.txt (``clientScrapperBot``, then
``*``), a per-host token bucket (``HOST_RPS``; a slower ``Crawl-delay`` wins),
the page budget and the run deadline, the conditional GET against the stored
evidence copy (``If-None-Match`` / ``If-Modified-Since`` → 304), ``Retry-After``
and ``with_backoff`` for transient answers, a 5 MB body cap, same-site
redirects only, and the per-host breaker: **5 consecutive 403/429** (a
CAPTCHA page counts as a 403, a 2xx resets) pause the host for 6 h in this
process and raise ``HostBlocked``; the runner then pauses the source row and
ends the run ``failed`` + ``error='host_blocked'``.

Shared, process-wide state (buckets, robots cache, breaker counters) lives in
``HostState``; per-run state (the counters a ``collector_runs`` row carries,
the fetched bodies the runner turns into evidence) lives on the Fetcher a run
gets from ``bind``. Paid hosts (``METERED_HOSTS``) refuse a request that does
not declare a ``PaidCall``, and a ``PaidCall`` without a cost gate: the gate is
``costs.ask`` (04 §8), bound by the runner — the order is ``Budget.can_spend``
first, then the gate, then the request.

robots.txt is read by ``services/robots`` (RFC 9309: the longest rule wins,
``*`` and ``$``), fetched from the URL's own scheme, host and port.

Logs carry host, status, elapsed, bytes, source, the cache verdict and a
``url_id`` (the first 12 hex digits of the sha256 of the stored form of the
URL, ``urlsafe.strip_secrets``) — never the URL itself, whose path names a
person on t.me or instagram.com and whose query may carry a phone (06 §4.5),
and never a body.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import html
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from clientscrapper.collectors.keywords import CAPTCHA_MARKERS, EMAIL_RE
from clientscrapper.config import settings
from clientscrapper.services.retry import is_transient_http, with_backoff
from clientscrapper.services.robots import RobotsRules, parse_robots
from clientscrapper.services.urlsafe import scrub_url as scrub_url  # re-exported
from clientscrapper.services.urlsafe import strip_secrets

log = logging.getLogger(__name__)

VERSION: Final = "0.1.0"
ROBOTS_AGENT: Final = "clientScrapperBot"


def contact_email_from(text: str) -> str | None:
    """The e-mail inside ``HH_USER_AGENT`` («clientScrapper/1.0 (x@y.uz)»)."""
    match = EMAIL_RE.search(text or "")
    return match.group(0) if match else None


def user_agent(contact: str | None) -> str:
    # HH_USER_AGENT is "clientScrapper/1.0 (email)" as hh.ru requires; the same
    # address names the company's crawler everywhere, so one env var serves both.
    who = f" (+mailto:{contact})" if contact else ""
    return f"{ROBOTS_AGENT}/{VERSION}{who} python-httpx/{httpx.__version__}"


USER_AGENT: Final = user_agent(contact_email_from(settings.hh_user_agent))

HOST_RPS: Final[Mapping[str, float]] = {
    "api.hh.ru": 1.0,
    "api.search.brave.com": 1.0,
    "places.googleapis.com": 2.0,
    "searchapi.api.cloud.yandex.net": 1.0,
    "operation.api.cloud.yandex.net": 2.0,
    "catalog.api.2gis.com": 1.0,
    "www.olx.uz": 0.33,
    "2gis.uz": 0.2,
    "html.duckduckgo.com": 0.1,
    "graph.facebook.com": 1.0,
    "api.apify.com": 1.0,
}
# Documented JSON APIs with a key: the only hosts a request may skip robots.txt
# for (``obey_robots=False``, and every ``post_json``). The call sites are
# fenced by tests/collectors/test_http.py.
ROBOTS_OPTOUT_HOSTS: Final = frozenset(
    {
        "api.search.brave.com",
        "searchapi.api.cloud.yandex.net",
        "operation.api.cloud.yandex.net",
        "places.googleapis.com",
        "catalog.api.2gis.com",
        "api.hh.ru",
        "graph.facebook.com",
        "api.apify.com",
    }
)
# Hosts whose calls are metered by costs.ask (03 §1.3 operations serp_brave,
# serp_yandex_night, places_*, twogis_api, apify_*): a request there without a
# PaidCall is refused before anything is sent.
METERED_HOSTS: Final = frozenset(
    {
        "api.search.brave.com",
        "searchapi.api.cloud.yandex.net",
        "places.googleapis.com",
        "catalog.api.2gis.com",
        "api.apify.com",
    }
)

BREAKER_THRESHOLD: Final = 5
BREAKER_PAUSE: Final = timedelta(hours=6)
BLOCKED_ERROR: Final = "host_blocked"
BLOCKED_PROBLEM: Final = "collector_blocked"
RETRY_AFTER_MAX: Final = 900.0
ROBOTS_TTL: Final = timedelta(hours=24)
ROBOTS_ERROR_TTL: Final = timedelta(hours=1)
ROBOTS_BODY_CAP: Final = 512 * 1024
BODY_CAP: Final = 5 * 1024 * 1024
MAX_REDIRECTS: Final = 5
CONNECT_TIMEOUT: Final = 10.0
BACKOFF_ATTEMPTS: Final = 3
BACKOFF_BASE: Final = 2.0
BACKOFF_CAP: Final = 60.0
REDIRECT_CODES: Final = frozenset({301, 302, 303, 307, 308})

ExhaustReason = Literal["pages", "usd", "deadline", "cost_cap"]


# --- errors ---------------------------------------------------------------------


class FetchError(Exception):
    """Base of every refusal this layer raises on its own."""


class RobotsDisallowed(FetchError):
    def __init__(self, url: str) -> None:
        super().__init__("robots.txt disallows this url")
        self.url = url


class HostPaused(FetchError):
    """The breaker is open for this host: nothing is sent until ``until``."""

    def __init__(self, host: str, until: datetime) -> None:
        super().__init__(f"{BLOCKED_ERROR}:{host}")
        self.host = host
        self.until = until


class HostBlocked(HostPaused):
    """Raised by the request that tripped the breaker."""


class BudgetExhausted(FetchError):
    """Budget.pages, Budget.usd, the run deadline, or a cost-gate refusal."""

    def __init__(self, reason: ExhaustReason, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}:{detail}")
        self.reason: ExhaustReason = reason
        self.detail = detail


class UnmeteredCall(FetchError):
    """A metered host without a PaidCall, or a PaidCall with no cost gate."""


class RobotsOptOutRefused(ValueError):
    """``obey_robots=False`` (or a POST) to a host outside ROBOTS_OPTOUT_HOSTS."""


class ProxyUnavailable(FetchError):
    """``proxy=True`` with PROXY_URL blank."""


# --- the shapes the layer exchanges ------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    body: bytes
    headers: Mapping[str, str]
    fetched_at: datetime
    sha256: str
    from_cache: bool
    not_modified: bool
    truncated: bool = False
    captcha: bool = False
    source: str = ""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


@dataclass(frozen=True, slots=True)
class StoredCopy:
    """The latest stored evidence for a URL: what a conditional GET sends and
    what a 304 answers with."""

    sha256: str
    body: bytes
    etag: str | None = None
    last_modified: str | None = None


class FetchHooks(Protocol):
    """What a run gives its Fetcher (``collectors/runner.RunLedger``)."""

    async def stored_copy(self, url: str) -> StoredCopy | None: ...

    def fetched(self, result: FetchResult) -> None: ...


class PageBudget(Protocol):
    """``collectors/base.Budget`` as this layer sees it."""

    @property
    def deadline(self) -> datetime: ...

    def take_page(self) -> None: ...

    def can_spend(self, est: Decimal) -> bool: ...

    def charge(self, usd: Decimal) -> None: ...


class Priced(Protocol):
    """``usage.Priced`` (T-06) as this layer sees it."""

    @property
    def cost_usd(self) -> Decimal | None: ...


@dataclass(frozen=True, slots=True)
class PaidCall:
    """A request costs.ask must clear first (operation as 04 §8 names it)."""

    operation: str
    est_usd: Decimal
    price: Callable[[FetchResult], Priced]


class CostGate(Protocol):
    """``functools.partial(costs.ask, session, now=…, collector_run_id=…)``."""

    def __call__(
        self,
        *,
        operation: Any,
        est_usd: Decimal,
        call: Callable[[], Awaitable[FetchResult]],
        price: Callable[[FetchResult], Any],
        source_key: str,
    ) -> Awaitable[FetchResult]: ...


@dataclass(slots=True)
class FetchStats:
    """One run's counters: the collector_runs columns and its meta extras."""

    requests: int = 0
    pages: int = 0
    http_403: int = 0
    http_429: int = 0
    skipped_robots: int = 0
    unchanged: int = 0
    truncated: int = 0
    captcha: int = 0
    cost_usd: Decimal = Decimal("0")
    blocked_hosts: list[str] = field(default_factory=list)
    exhausted: ExhaustReason | None = None


@dataclass(slots=True)
class _Robots:
    rules: RobotsRules | None  # None = allow everything
    expires_at: datetime

    def allows(self, url: str) -> bool:
        return self.rules is None or self.rules.allows(url)

    def crawl_delay(self) -> float | None:
        return None if self.rules is None else self.rules.crawl_delay


class HostState:
    """Process-wide per-host state, shared by every Fetcher ``bind`` makes.
    Pacing and the breaker are keyed by host name; robots.txt by authority
    (host and a non-default port), since each port serves its own file."""

    def __init__(self) -> None:
        self.last_at: dict[str, float] = {}
        self.streak: dict[str, int] = {}
        self.paused_until: dict[str, datetime] = {}
        self.robots: dict[str, _Robots] = {}
        self._pace_locks: dict[str, asyncio.Lock] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    def pace_lock(self, host: str) -> asyncio.Lock:
        return self._pace_locks.setdefault(host, asyncio.Lock())

    def robots_lock(self, host: str) -> asyncio.Lock:
        return self._robots_locks.setdefault(host, asyncio.Lock())


# --- small pure helpers ------------------------------------------------------------

_ROBOTS_HEADER = re.compile(
    r"\A# clientScrapper robots fetched_at=(?P<at>\S+) status=(?P<status>\d+)\n"
)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


def authority_of(url: str) -> str:
    """``host`` or ``host:port`` (a non-default port): where robots.txt lives."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is None or port == _DEFAULT_PORTS.get(parts.scheme.lower()):
        return host
    return f"{host}:{port}"


def url_id(url: str) -> str:
    """A log line's handle on a URL it must not print (see the module doc)."""
    return hashlib.sha256(strip_secrets(url).encode("utf-8")).hexdigest()[:12]


def same_site(a: str, b: str) -> bool:
    """A redirect we follow: the same host, or the same host with/without www."""
    return a == b or b == f"www.{a}" or a == f"www.{b}"


def retry_after_seconds(value: str | None, *, now: datetime) -> float | None:
    """``Retry-After`` as seconds: an integer, or an HTTP date relative to now."""
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    return max((when - now).total_seconds(), 0.0)


# A marker that names challenge machinery (a script, a challenge id) is found in
# the markup; the others are words a challenge page shows a reader. «captcha»
# is also in the markup of every page that embeds a reCAPTCHA in its contact
# form (recaptcha/api.js, g-recaptcha), so words count only in visible text.
MARKUP_MARKERS: Final = frozenset({"anomaly.js", "cf-chl"})
# A challenge page shows a reader a sentence or two; a directory page with a
# captcha-labelled form field shows the directory. Beyond this many visible
# characters a word marker counts only in the title or a heading.
CHALLENGE_TEXT_MAX: Final = 2000
_HIDDEN = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>|<!--.*?-->", re.I | re.S
)
_TAG = re.compile(r"<[^>]*>")
_HEADING = re.compile(r"<(title|h1|h2)\b[^>]*>(.*?)</\1\s*>", re.I | re.S)
_SPACES = re.compile(r"\s+")


def _visible(markup: str) -> str:
    return _SPACES.sub(" ", html.unescape(_TAG.sub(" ", markup))).strip()


def looks_like_captcha(body: bytes) -> bool:
    """A challenge page, not a page that embeds a challenge widget: a markup
    marker anywhere, or a word marker in the title / a heading, or in the
    visible text of a page that shows little else (03 §1.1 CAPTCHA_MARKERS)."""
    text = body.decode("utf-8", errors="ignore")
    if text.lstrip()[:1] in ("{", "["):
        # A JSON answer is data: «captcha» in it is somebody's bio or title.
        # An API that wants a challenge answers 403/429, which count anyway.
        return False
    lowered = text.lower()
    words = [m.lower() for m in CAPTCHA_MARKERS if m not in MARKUP_MARKERS]
    if any(marker in lowered for marker in MARKUP_MARKERS):
        return True
    shown = _HIDDEN.sub(" ", text)
    headings = " ".join(_visible(m.group(2)) for m in _HEADING.finditer(shown)).lower()
    if any(marker in headings for marker in words):
        return True
    visible = _visible(shown).lower()
    return len(visible) <= CHALLENGE_TEXT_MAX and any(m in visible for m in words)


async def _read_capped(response: httpx.Response, cap: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        if size + len(chunk) > cap:
            chunks.append(chunk[: cap - size])
            return b"".join(chunks), True
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), False


class _TransientResult(Exception):
    """A 429/5xx answer, raised so with_backoff retries it; the last one is
    returned to the caller as an ordinary FetchResult."""

    def __init__(self, result: FetchResult) -> None:
        super().__init__(f"transient status {result.status}")
        self.result = result


def _retry_on(exc: BaseException) -> bool:
    return isinstance(exc, _TransientResult) or is_transient_http(exc)


def _default_wall() -> datetime:
    return datetime.now(settings.tz)


# --- the Fetcher -----------------------------------------------------------------


class Fetcher:
    def __init__(
        self,
        *,
        rps_default: float | None = None,
        host_rps: Mapping[str, float] = HOST_RPS,
        robots_ttl: timedelta = ROBOTS_TTL,
        robots_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        hosts: HostState | None = None,
        hooks: FetchHooks | None = None,
        cost_gate: CostGate | None = None,
        agent: str = USER_AGENT,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = _default_wall,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rps_default = (
            rps_default if rps_default is not None else settings.collector_rps_default
        )
        self.host_rps = dict(host_rps)
        self.robots_ttl = robots_ttl
        self.robots_dir = robots_dir
        self.agent = agent
        self.stats = FetchStats()
        self._transport = transport
        self._hosts = hosts if hosts is not None else HostState()
        self._hooks = hooks
        self._cost_gate = cost_gate
        self._monotonic = monotonic
        self._wall = wall
        self._sleep = sleep
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        self._proxy_client: httpx.AsyncClient | None = None
        self._owner = True

    # -- lifecycle ------------------------------------------------------------------

    def bind(
        self, *, hooks: FetchHooks | None = None, cost_gate: CostGate | None = None
    ) -> Fetcher:
        """A view for one run: same client, buckets, robots and breaker; its own
        counters, hooks and cost gate. Only the original closes the client."""
        view = copy.copy(self)
        view.stats = FetchStats()
        view._hooks = hooks
        view._cost_gate = cost_gate
        view._owner = False
        return view

    async def aclose(self) -> None:
        if not self._owner:
            return
        await self._client.aclose()
        if self._proxy_client is not None:
            await self._proxy_client.aclose()

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- public API -----------------------------------------------------------------

    async def get(
        self,
        url: str,
        *,
        source: str,
        budget: PageBudget,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        cache: bool = True,
        obey_robots: bool = True,
        paid: PaidCall | None = None,
        proxy: bool = False,
    ) -> FetchResult:
        # Merged, not replaced: httpx.URL(url, params=…) would drop the query the
        # caller already wrote (hh's text= and area= under a page=).
        full = str(httpx.URL(url).copy_merge_params(dict(params))) if params else url
        return await self._fetch(
            "GET",
            full,
            source=source,
            budget=budget,
            headers=headers,
            timeout=timeout,
            cache=cache,
            obey_robots=obey_robots,
            paid=paid,
            proxy=proxy,
            json_body=None,
        )

    async def post_json(
        self,
        url: str,
        *,
        source: str,
        budget: PageBudget,
        json: Any,
        headers: Mapping[str, str] | None = None,
        timeout: float = 60.0,
        paid: PaidCall | None = None,
    ) -> FetchResult:
        """APIs only (Places, Yandex): robots.txt does not apply, so the host must
        be a documented API in ROBOTS_OPTOUT_HOSTS."""
        return await self._fetch(
            "POST",
            url,
            source=source,
            budget=budget,
            headers=headers,
            timeout=timeout,
            cache=False,
            obey_robots=False,
            paid=paid,
            proxy=False,
            json_body=json,
        )

    # -- the pipeline ----------------------------------------------------------------

    async def _fetch(
        self,
        method: str,
        url: str,
        *,
        source: str,
        budget: PageBudget,
        headers: Mapping[str, str] | None,
        timeout: float,
        cache: bool,
        obey_robots: bool,
        paid: PaidCall | None,
        proxy: bool,
        json_body: Any,
    ) -> FetchResult:
        host = host_of(url)
        if not host:
            raise ValueError("a fetch needs an absolute http(s) url")
        extra = self._extra_headers(headers)
        if not obey_robots and host not in ROBOTS_OPTOUT_HOSTS:
            raise RobotsOptOutRefused(f"robots.txt applies to {host}")
        if host in METERED_HOSTS and paid is None:
            raise UnmeteredCall(f"{host} is metered: declare a PaidCall")
        if paid is not None and self._cost_gate is None:
            raise UnmeteredCall("a paid call needs the cost gate (costs.ask)")
        if proxy and not settings.proxy_url:
            raise ProxyUnavailable("PROXY_URL is blank")
        self._refuse_paused(host)
        if obey_robots:
            await self._check_robots(url, host)
        self._take_page(budget)

        stored: StoredCopy | None = None
        if cache and method == "GET" and self._hooks is not None:
            stored = await self._hooks.stored_copy(url)

        async def call() -> FetchResult:
            return await self._with_retries(
                method,
                url,
                host=host,
                source=source,
                headers=extra,
                timeout=timeout,
                stored=stored,
                json_body=json_body,
                proxy=proxy,
                obey_robots=obey_robots,
            )

        result = await (self._paid(paid, budget, call, source) if paid else call())
        if stored is not None and (
            result.not_modified
            or (result.status == 200 and result.sha256 == stored.sha256)
        ):
            self.stats.unchanged += 1
        if result.truncated:
            self.stats.truncated += 1
        if self._hooks is not None:
            self._hooks.fetched(result)
        return result

    def _extra_headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        extra = dict(headers or {})
        if any(name.lower() == "user-agent" for name in extra):
            raise ValueError("collectors add headers; the User-Agent is never replaced")
        return extra

    def _refuse_paused(self, host: str) -> None:
        until = self._hosts.paused_until.get(host)
        if until is not None and until > self._wall():
            raise HostPaused(host, until)

    def _take_page(self, budget: PageBudget) -> None:
        if self._wall() >= budget.deadline:
            self.stats.exhausted = "deadline"
            raise BudgetExhausted("deadline")
        try:
            budget.take_page()
        except BudgetExhausted as exc:
            self.stats.exhausted = exc.reason
            raise
        self.stats.pages += 1

    async def _paid(
        self,
        paid: PaidCall,
        budget: PageBudget,
        call: Callable[[], Awaitable[FetchResult]],
        source: str,
    ) -> FetchResult:
        if not budget.can_spend(paid.est_usd):
            self.stats.exhausted = "usd"
            raise BudgetExhausted("usd")
        gate = self._cost_gate
        assert gate is not None  # checked before any request (_fetch)
        dispatched = False
        charged: list[Decimal] = []

        async def guarded() -> FetchResult:
            nonlocal dispatched
            dispatched = True
            return await call()

        def price(result: FetchResult) -> Priced:
            priced = paid.price(result)
            cost = priced.cost_usd
            charged.append(cost if cost is not None else paid.est_usd)
            return priced

        try:
            result = await gate(
                operation=paid.operation,
                est_usd=paid.est_usd,
                call=guarded,
                price=price,
                source_key=source,
            )
        except Exception as exc:
            if not dispatched:
                # The gate refused before anything was sent (a cap): a budget
                # stop, not a failure — the run ends partial (04 §8).
                self.stats.exhausted = "cost_cap"
                raise BudgetExhausted("cost_cap", str(exc)) from exc
            # Sent and failed: the cap layer keeps the estimate, so do we.
            self._charge(budget, paid.est_usd)
            raise
        self._charge(budget, sum(charged, Decimal("0")) if charged else paid.est_usd)
        return result

    def _charge(self, budget: PageBudget, usd: Decimal) -> None:
        budget.charge(usd)
        self.stats.cost_usd += usd

    async def _with_retries(
        self,
        method: str,
        url: str,
        *,
        host: str,
        source: str,
        headers: dict[str, str],
        timeout: float,
        stored: StoredCopy | None,
        json_body: Any,
        proxy: bool,
        obey_robots: bool,
    ) -> FetchResult:
        async def attempt() -> FetchResult:
            result = await self._request(
                method,
                url,
                host=host,
                source=source,
                headers=headers,
                timeout=timeout,
                stored=stored,
                json_body=json_body,
                proxy=proxy,
                obey_robots=obey_robots,
            )
            if result.status == 429 or result.status >= 500:
                wait = retry_after_seconds(
                    result.headers.get("retry-after"), now=self._wall()
                )
                if (
                    wait is not None
                    and wait <= RETRY_AFTER_MAX
                    and result.status in (429, 503)
                ):
                    await self._sleep(wait)
                raise _TransientResult(result)
            return result

        try:
            return await with_backoff(
                attempt,
                attempts=BACKOFF_ATTEMPTS,
                base=BACKOFF_BASE,
                cap=BACKOFF_CAP,
                retry_on=_retry_on,
                sleep=self._sleep,
            )
        except _TransientResult as exc:
            return exc.result

    async def _request(
        self,
        method: str,
        url: str,
        *,
        host: str,
        source: str,
        headers: dict[str, str],
        timeout: float,
        stored: StoredCopy | None,
        json_body: Any,
        proxy: bool,
        obey_robots: bool,
    ) -> FetchResult:
        """One logical request: the hops of a same-site redirect chain, then the
        breaker's verdict on the final answer."""
        send_headers = {"User-Agent": self.agent, **headers}
        if stored is not None:
            if stored.etag:
                send_headers["If-None-Match"] = stored.etag
            if stored.last_modified:
                send_headers["If-Modified-Since"] = stored.last_modified
        current = url
        current_method = method
        started = self._monotonic()
        for hop in range(MAX_REDIRECTS + 1):
            status, response_headers, body, truncated = await self._send(
                current_method,
                current,
                host=host_of(current),
                headers=send_headers,
                timeout=timeout,
                json_body=json_body if current_method == "POST" else None,
                proxy=proxy,
            )
            location = response_headers.get("location")
            if status in REDIRECT_CODES and location and hop < MAX_REDIRECTS:
                target = urljoin(current, location)
                if same_site(host_of(current), host_of(target)):
                    if obey_robots:
                        await self._check_robots(target, host_of(target))
                    if status == 303:
                        current_method = "GET"
                    current = target
                    continue
            break
        fetched_at = self._wall()
        if status == 304 and stored is not None:
            result = FetchResult(
                url=url,
                final_url=current,
                status=304,
                body=stored.body,
                headers=response_headers,
                fetched_at=fetched_at,
                sha256=stored.sha256,
                from_cache=True,
                not_modified=True,
                source=source,
            )
        else:
            captcha = 200 <= status < 300 and looks_like_captcha(body)
            result = FetchResult(
                url=url,
                final_url=current,
                status=status,
                body=body,
                headers=response_headers,
                fetched_at=fetched_at,
                sha256=hashlib.sha256(body).hexdigest(),
                from_cache=False,
                not_modified=False,
                truncated=truncated,
                captcha=captcha,
                source=source,
            )
        self._log(method, result, host=host, started=started, cached=stored is not None)
        self._account(host, result)
        return result

    async def _send(
        self,
        method: str,
        url: str,
        *,
        host: str,
        headers: Mapping[str, str],
        timeout: float,
        json_body: Any,
        proxy: bool,
    ) -> tuple[int, dict[str, str], bytes, bool]:
        await self._pace(host, authority_of(url))
        self.stats.requests += 1
        client = self._client_for(proxy)
        request = client.build_request(
            method,
            url,
            headers=headers,
            json=json_body,
            timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT),
        )
        response = await client.send(request, stream=True)
        try:
            body, truncated = await _read_capped(response, BODY_CAP)
        finally:
            await response.aclose()
        lowered = {name.lower(): value for name, value in response.headers.items()}
        return response.status_code, lowered, body, truncated

    def _client_for(self, proxy: bool) -> httpx.AsyncClient:
        if not proxy:
            return self._client
        if self._proxy_client is None:
            self._proxy_client = httpx.AsyncClient(
                transport=self._transport,
                proxy=None if self._transport is not None else settings.proxy_url,
                follow_redirects=False,
            )
        return self._proxy_client

    def _log(
        self, method: str, result: FetchResult, *, host: str, started: float, cached: bool
    ) -> None:
        if result.not_modified:
            verdict = "not_modified"
        elif not cached:
            verdict = "off"
        else:
            verdict = "miss"
        elapsed_ms = int((self._monotonic() - started) * 1000)
        log.info(
            "%s %s %s -> %d",
            method,
            host,
            url_id(result.url),
            result.status,
            extra={
                "host": host,
                "url_id": url_id(result.url),
                "status": result.status,
                "elapsed_ms": elapsed_ms,
                "bytes": len(result.body),
                "source": result.source,
                "cache": verdict,
            },
        )

    # -- the breaker --------------------------------------------------------------

    def _account(self, host: str, result: FetchResult) -> None:
        status = result.status
        if status == 403 or result.captcha:
            self.stats.http_403 += 1
            if result.captcha:
                self.stats.captcha += 1
            self._strike(host)
            return
        retry_after = retry_after_seconds(
            result.headers.get("retry-after"), now=self._wall()
        )
        if status == 429 or (status == 503 and retry_after is not None):
            # A 503 that asks us to wait is a rate limit in other words.
            self.stats.http_429 += 1
            if retry_after is not None and retry_after > RETRY_AFTER_MAX:
                self._trip(host)
            self._strike(host)
            return
        if 200 <= status < 300:
            self._hosts.streak[host] = 0

    def _strike(self, host: str) -> None:
        streak = self._hosts.streak.get(host, 0) + 1
        self._hosts.streak[host] = streak
        if streak >= BREAKER_THRESHOLD:
            self._trip(host)

    def _trip(self, host: str) -> None:
        until = self._wall() + BREAKER_PAUSE
        self._hosts.paused_until[host] = until
        self._hosts.streak[host] = 0
        if host not in self.stats.blocked_hosts:
            self.stats.blocked_hosts.append(host)
        log.warning(
            "breaker open for %s until %s",
            host,
            until.isoformat(),
            extra={"host": host, "problem": BLOCKED_PROBLEM},
        )
        raise HostBlocked(host, until)

    # -- pacing ----------------------------------------------------------------------

    def _interval(self, host: str, authority: str) -> float:
        rps = self.host_rps.get(host, self.rps_default)
        interval = 1.0 / rps
        robots = self._hosts.robots.get(authority)
        delay = robots.crawl_delay() if robots is not None else None
        return max(interval, delay) if delay is not None else interval

    async def _pace(self, host: str, authority: str) -> None:
        # The interval is read at the next request, not fixed at the last one:
        # a Crawl-delay learnt from the robots.txt just fetched already counts.
        async with self._hosts.pace_lock(host):
            last = self._hosts.last_at.get(host)
            if last is not None:
                wait = last + self._interval(host, authority) - self._monotonic()
                if wait > 0:
                    await self._sleep(wait)
            self._hosts.last_at[host] = self._monotonic()

    # -- robots.txt ------------------------------------------------------------------

    async def _check_robots(self, url: str, host: str) -> None:
        robots = await self._robots_for(urlsplit(url).scheme or "https", url, host)
        if not robots.allows(url):
            self.stats.skipped_robots += 1
            raise RobotsDisallowed(url)

    async def _robots_for(self, scheme: str, url: str, host: str) -> _Robots:
        authority = authority_of(url)
        async with self._hosts.robots_lock(authority):
            now = self._wall()
            entry = self._hosts.robots.get(authority)
            if entry is not None and entry.expires_at > now:
                return entry
            entry = self._robots_from_disk(authority, now)
            if entry is None:
                entry = await self._fetch_robots(scheme, host, authority, now)
            self._hosts.robots[authority] = entry
            return entry

    async def _fetch_robots(
        self, scheme: str, host: str, authority: str, now: datetime
    ) -> _Robots:
        url = f"{scheme}://{authority}/robots.txt"
        await self._pace(host, authority)
        self.stats.requests += 1
        try:
            async with self._client.stream(
                "GET",
                url,
                headers={"User-Agent": self.agent},
                timeout=httpx.Timeout(30.0, connect=CONNECT_TIMEOUT),
                follow_redirects=True,
            ) as response:
                body, _ = await _read_capped(response, ROBOTS_BODY_CAP)
                status = response.status_code
        except httpx.HTTPError:
            # Unreachable: allow for an hour, then ask again (03 §1.1).
            return _Robots(rules=None, expires_at=now + ROBOTS_ERROR_TTL)
        if status >= 500 or status == 429:
            # A 429 is the server asking us to slow down, not saying it has no
            # robots.txt: the 5xx rule (an hour, then ask again), not 24 h of
            # "no rules".
            return _Robots(rules=None, expires_at=now + ROBOTS_ERROR_TTL)
        text = body.decode("utf-8", errors="replace") if status == 200 else ""
        self._robots_to_disk(authority, now, status, text)
        return self._robots_entry(text, status, now + self.robots_ttl)

    @staticmethod
    def _robots_entry(text: str, status: int, expires_at: datetime) -> _Robots:
        if status != 200:
            # A 4xx robots.txt means "no rules": everything is allowed (RFC 9309).
            return _Robots(rules=None, expires_at=expires_at)
        return _Robots(rules=parse_robots(text, ROBOTS_AGENT), expires_at=expires_at)

    def _robots_path(self, authority: str) -> Path | None:
        if self.robots_dir is None or "/" in authority or authority.startswith("."):
            return None
        return self.robots_dir / f"{authority}.txt"

    def _robots_to_disk(
        self, authority: str, now: datetime, status: int, text: str
    ) -> None:
        path = self._robots_path(authority)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            header = (
                f"# clientScrapper robots fetched_at={now.isoformat()} status={status}\n"
            )
            tmp.write_text(header + text, encoding="utf-8")
            tmp.replace(path)
        except OSError:
            log.warning("robots cache not writable", extra={"host": authority})

    def _robots_from_disk(self, authority: str, now: datetime) -> _Robots | None:
        path = self._robots_path(authority)
        if path is None or not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        head = _ROBOTS_HEADER.match(text)
        if head is None:
            return None
        try:
            fetched_at = datetime.fromisoformat(head["at"])
        except ValueError:
            return None
        if fetched_at.tzinfo is None or fetched_at + self.robots_ttl <= now:
            return None
        return self._robots_entry(
            text[head.end() :], int(head["status"]), fetched_at + self.robots_ttl
        )
