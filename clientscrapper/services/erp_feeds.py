"""The two ERP feeds the agent reads back (05 §B.2): the client and lost-lead
phone hashes (``GET /api/agent/suppression``) and, from T-10, what became of
each push (``GET /api/agent/outcomes``).

One HTTP client for the ERP (``ErpClient``): both key headers with one value
(the ERP reads ``X-GSR-Key`` only, 05 §A.2), gzip asked for, redirects never
followed — a 308 from production's Caddy to an ``http://`` base URL would hand
the key to wherever it points (05 §B.1). The key travels in a header and
nowhere else: never a URL, never a log line.

``fetch_pages`` walks a feed by its opaque cursors exactly as the ERP hands
them back (the agent never decodes one; the fake ERP does). ``sync_suppression``
applies a walk to ``suppression`` (05 §B.2 steps 1–5): a FULL pull — ``limit``
and nothing else, the only walk that may delete — and a DELTA — ``since`` the
previous read's ERP ``generated_at`` minus ten minutes, additive only, aborted
for a full pull when the key was re-minted (a new ``salt_id``).

``suppression_fresh`` is THE home of feed freshness (``FULL_MAX_AGE_H``,
``DELTA_MAX_AGE_H``): the push gate, 04's gate, 06 §4.2's
``suppression_stale`` and ``/holat`` all ask it and none restates the hours.

What a failure writes is what ``services/health`` reads (06 §4.2): a 404 (or a
proxy's 401/403) is the push's misconfiguration — ``sources.erp_sync`` breaker
``push_404`` for 6 h, the owner told once per 6 h; a 3xx is ``erp_redirect``
the same way; a 400, a changed hash rule, a runaway walk and a body of the
wrong shape are ``feed_contract`` with their code in the detail; a 503 or any
5xx is retried (``with_backoff``, 4 tries) and then only fails the run — the
freshness probe reports it, never ``push_404``. A full pull that shrank past
10 % deletes nothing and raises ``suppression_shrunk``.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import re
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal

import httpx
import sqlalchemy as sa
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import Settings, settings
from clientscrapper.db.models import Outcome, PushLog, RuntimeFlag, Source, Suppression
from clientscrapper.services import costs, health
from clientscrapper.services.http import VERSION
from clientscrapper.services.retry import Sleep, is_transient_http, with_backoff

log = logging.getLogger(__name__)

# --- the contract (05 §A.5–§A.7) ------------------------------------------------------

FEED_RULE: Final = "hmac-sha256(secret, last9digits) hex[0:20]"
SUPPRESSION_PATH: Final = "/api/agent/suppression"
OUTCOMES_PATH: Final = "/api/agent/outcomes"
# Every suppression page recomputes the whole set on the ERP (A.7): few big pages.
SUPPRESSION_LIMIT: Final = 5000
OUTCOMES_LIMIT: Final = 1000
# 1,000,000 entries / 200,000 rows; past it the walk is a runaway.
MAX_PAGES: Final = 200
# A feed row that carries one of these is a contract breach (06 §5).
PII_KEYS: Final = frozenset(
    {
        "phone",
        "telefon",
        "name",
        "ism",
        "note",
        "izoh",
        "owner",
        "owner_id",
        "quoted_amount",
        "price",
        "amount",
        "digits",
        "tail",
    }
)
# One value under both names: the ERP reads X-GSR-Key only (A.2); X-ERP-Key is
# the shared contract's old name, sent and ignored. crm_push sends through
# this client too.
ERP_KEY_HEADERS: Final = ("X-GSR-Key", "X-ERP-Key")
USER_AGENT: Final = f"clientscrapper/{VERSION}"
FEED_TIMEOUT: Final = 30.0
POST_TIMEOUT: Final = httpx.Timeout(20.0, connect=5.0)
# 503 and other 5xx, timeouts, a dropped connection (05 §B.2).
FEED_ATTEMPTS: Final = 4
FEED_BACKOFF_BASE: Final = 2.0
FEED_BACKOFF_CAP: Final = 60.0

# --- sync rules (05 §B.2) -------------------------------------------------------------

# Re-read what committed after the previous read: harmless upserts.
DELTA_OVERLAP: Final = timedelta(minutes=10)
# A full pull may delete only when it saw at least this share of the distinct
# (kind, h) of the previous COMPLETED full pull.
SHRINK_FLOOR: Final = 0.9
UPSERT_CHUNK: Final = 1000
# One nightly full pull (30 2 * * *) plus slack; one hourly delta plus slack.
FULL_MAX_AGE_H: Final = 26
DELTA_MAX_AGE_H: Final = 3
ERP_SOURCE: Final = health.ERP_SOURCE
# The ERP breaker (05 §B.1): sources.erp_sync.breaker_until; /yoq crm clears it.
BREAKER_FOR: Final = timedelta(hours=6)
FEED_SUPPRESSION: Final = "suppression"
FEED_OUTCOMES: Final = "outcomes"
WRITER: Final = "worker:erp_sync"

# 06 §4.2 keys this module raises (health.PROBLEM_KEYS is the one list).
PUSH_404_PROBLEM: Final = "push_404"
ERP_REDIRECT_PROBLEM: Final = "erp_redirect"
FEED_CONTRACT_PROBLEM: Final = "feed_contract"
SHRUNK_PROBLEM: Final = "suppression_shrunk"
SELFTEST_PROBLEM: Final = "suppression_selftest_failed"
STALE_HIT_PROBLEM: Final = "suppression_stale_hit"

# What the owner is told at once when a feed trips the breaker; the health
# sweep repeats it (06 §4.2 TOLD_FIRST_ELSEWHERE). Same words as its line.
PUSH_404_TEXT: Final = (
    "⛔ ERP kaliti yoki manba notoʻgʻri: 404. Kelgan arizalar → Reklama "
    "havolalari → Agent → kalit; <code>.env</code> CRM_AGENT_KEY; "
    "<code>make reload</code>; /yoq crm"
)
ERP_REDIRECT_TEXT: Final = (
    "⛔ ERP manzili yoʻnaltiryapti (HTTP {code}): <code>.env</code> dagi "
    "CRM_BASE_URL <code>https://</code> bilan boshlanishi kerak; "
    "<code>make reload</code>; /yoq crm"
)

FeedName = Literal["suppression", "outcomes"]
SyncMode = Literal["full", "delta"]
ContractCode = Literal["bad_request", "rule_changed", "runaway", "shape"]
BreakerKey = Literal["push_404", "erp_redirect"]


# --- errors ---------------------------------------------------------------------------


class FeedError(Exception):
    """A feed call that went wrong in a way the sync records (05 §B.6)."""


class ErpHttpError(FeedError):
    """A non-200 answer. ``code`` is the ERP's own ``error`` (``bad_cursor``,
    ``bad_since``, ``unavailable``) when the body was JSON."""

    def __init__(self, status: int, code: str | None = None) -> None:
        super().__init__(f"ERP answered HTTP {status}" + (f" ({code})" if code else ""))
        self.status = status
        self.code = code


class FeedShapeError(FeedError):
    """A 200 that is not the documented body: not JSON, a missing key, a
    non-list, a field of the wrong form, a row carrying a PII key."""


class FeedContract(FeedError):
    """A contract break with a code of its own: ``rule_changed`` (the hash
    rule moved), ``runaway`` (past ``MAX_PAGES``, or a ``next`` equal to the
    cursor just sent)."""

    def __init__(self, code: ContractCode, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _retryable(exc: BaseException) -> bool:
    """A 5xx or a proxy's 429, a timeout, a dropped connection: worth another
    try. A 4xx, a 3xx and a body of the wrong shape are not."""
    if isinstance(exc, ErpHttpError):
        return exc.status >= 500 or exc.status == 429
    return isinstance(exc, httpx.HTTPError) and is_transient_http(exc)


# --- the client -----------------------------------------------------------------------


class ErpClient:
    """One ``httpx.AsyncClient`` on ``CRM_BASE_URL`` with ``CRM_AGENT_KEY``.

    ``sleep`` is the backoff's wait (tests pass one that does not)."""

    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        if not key.strip():
            raise ValueError("CRM_AGENT_KEY is empty")
        self._key = key
        self.sleep = sleep
        headers = {name: key for name in ERP_KEY_HEADERS}
        headers.update({"Accept-Encoding": "gzip", "User-Agent": USER_AGENT})
        self.client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            headers=headers,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        # Never the key.
        return f"ErpClient({str(self.client.base_url)!r})"

    @property
    def salt_id(self) -> str:
        """The ``salt_id`` the ERP derives from this client's key."""
        return salt_id(self._key)

    def hash_tail(self, tail9: str) -> str:
        """``hash_tail`` with this client's key — the gate's hash."""
        return hash_tail(self._key, tail9)

    async def get_feed(
        self, path: str, params: Mapping[str, str | int]
    ) -> dict[str, Any]:
        """One GET of a feed page: the JSON object of a 200, else
        ``ErpHttpError``; a 200 that is not a JSON object is
        ``FeedShapeError``."""
        response = await self.client.get(path, params=dict(params), timeout=FEED_TIMEOUT)
        if response.status_code != httpx.codes.OK:
            raise ErpHttpError(response.status_code, _error_code(response))
        try:
            body = response.json()
        except ValueError:
            raise FeedShapeError(f"{path}: the body is not JSON") from None
        if not isinstance(body, dict):
            raise FeedShapeError(f"{path}: the body is not a JSON object")
        return body

    async def post_lead(self, path: str, body: Mapping[str, Any]) -> httpx.Response:
        """The lead door — ``crm_push._post``'s alone (07 §1.2); ``path`` is the
        push path it formats. 20 s, 5 s to connect."""
        return await self.client.post(path, json=dict(body), timeout=POST_TIMEOUT)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> ErpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def _error_code(response: httpx.Response) -> str | None:
    """The ERP's ``{"error": "<code>"}``; None for any other body."""
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) and len(code) <= 40 else None


def client_from_settings(config: Settings = settings) -> ErpClient:
    return ErpClient(config.crm_base_url, config.crm_agent_key)


def feeds_configured(config: Settings = settings) -> bool:
    """The feeds are read only through the ``agent`` door with its key.
    During the «Boshqa» fallback (``CRM_PUSH_SOURCE=other``) ``CRM_AGENT_KEY``
    is that door's key, which reads nothing: every GET would be a 404 and
    trip the breaker, so no feed call is made at all (05 §B.3)."""
    return config.crm_push_source == "agent" and bool(config.crm_agent_key.strip())


# --- the hash rule (A.7) --------------------------------------------------------------


def hash_tail(secret: str, tail9: str) -> str:
    """``hmac_sha256(secret, tail9).hexdigest()[:20]`` — the feed's ``h``."""
    return hmac.new(secret.encode(), tail9.encode(), hashlib.sha256).hexdigest()[:20]


def salt_id(secret: str) -> str:
    """``sha256(secret)[:8]``: changes when the owner re-mints the key."""
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def fmt_since(dt: datetime) -> str:
    """An aware instant as a ``since``: UTC, six fraction digits, always
    ``Z`` (a ``+`` would arrive as a space and be refused, A.6)."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("since must be an aware datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- the pages (A.7, A.9) -------------------------------------------------------------

_INSTANT_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


def _instant_z(value: str) -> datetime:
    """An ISO instant in UTC with ``Z`` (the feeds print milliseconds)."""
    if not _INSTANT_Z.match(value):
        raise ValueError("not an ISO instant ending in Z")
    head, _, frac = value[:-1].partition(".")
    stamp = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    return stamp + timedelta(microseconds=int(frac.ljust(6, "0")) if frac else 0)


def _checked_instant_z(value: str) -> str:
    _instant_z(value)
    return value


InstantZ = Annotated[str, AfterValidator(_checked_instant_z)]
Cursor = Annotated[str, Field(min_length=1, max_length=400)]
_PAGE = ConfigDict(frozen=True, extra="ignore", strict=True)


class FeedEntry(BaseModel):
    """One suppression entry: the hash, its kind, when its record last moved."""

    model_config = _PAGE

    h: Annotated[str, Field(pattern=r"^[0-9a-f]{20}$")]
    kind: Literal["client", "lost"]
    at: datetime

    @classmethod
    def from_row(cls, d: Mapping[str, Any]) -> FeedEntry:
        at = d.get("at")
        if not isinstance(at, str):
            raise ValueError("at must be a string")
        return cls.model_validate({**d, "at": _instant_z(at)})


class SuppressionPage(BaseModel):
    """A page of ``GET /api/agent/suppression``: every key present (``next``
    and ``resume`` may be null), ``entries`` raw until ``parse_entry``."""

    model_config = _PAGE

    mode: Literal["full", "delta"]
    generated_at: InstantZ
    salt_id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}$")]
    rule: str
    entries: list[dict[str, Any]]
    next: Cursor | None
    resume: Cursor | None

    @property
    def generated(self) -> datetime:
        return _instant_z(self.generated_at)


class OutcomesPage(BaseModel):
    """A page of ``GET /api/agent/outcomes``; each row is ``parse_outcome``'s
    (T-10)."""

    model_config = _PAGE

    generated_at: InstantZ
    rows: list[dict[str, Any]]
    next: Cursor | None
    resume: Cursor | None


def _pii_keys(d: Mapping[str, Any]) -> list[str]:
    return sorted(k for k in d if isinstance(k, str) and k.lower() in PII_KEYS)


def parse_entry(d: Any) -> FeedEntry:
    """One suppression entry, validated; unknown keys ignored, a PII key or a
    field of the wrong form → ``FeedShapeError``."""
    if not isinstance(d, dict):
        raise FeedShapeError("a suppression entry is not an object")
    leaked = _pii_keys(d)
    if leaked:
        raise FeedShapeError(f"a suppression entry carries {leaked}")
    try:
        return FeedEntry.from_row(d)
    except (ValidationError, ValueError) as exc:
        raise FeedShapeError(
            f"a suppression entry is malformed: {_fields(exc)}"
        ) from None


def parse_page[P: (SuppressionPage, OutcomesPage)](
    model: type[P], body: Mapping[str, Any]
) -> P:
    """A page body as ``model``; a missing key, a non-list or a field of the
    wrong form → ``FeedShapeError``."""
    try:
        return model.model_validate(dict(body))
    except ValidationError as exc:
        raise FeedShapeError(f"{model.__name__} is malformed: {_fields(exc)}") from None


def _fields(exc: Exception) -> str:
    """Which fields failed — never their values (a body may carry anything)."""
    if isinstance(exc, ValidationError):
        return ", ".join(
            sorted({".".join(str(p) for p in err["loc"]) or "?" for err in exc.errors()})
        )
    return type(exc).__name__


async def fetch_pages[P: (SuppressionPage, OutcomesPage)](
    erp: ErpClient,
    path: str,
    *,
    params: Mapping[str, str | int],
    model: type[P],
    max_pages: int = MAX_PAGES,
) -> AsyncGenerator[P, None]:
    """Walk a feed: ``params`` exactly as given (never a ``since`` of its own,
    never ``If-None-Match``), then ``next`` with the SAME params plus
    ``cursor=<next>`` until ``next`` is null. Each page is validated before it
    is yielded. A 5xx or a transport error is retried (4 tries); more than
    ``max_pages`` pages, or a ``next`` equal to the cursor just sent, is
    ``FeedContract('runaway')``."""
    query: dict[str, str | int] = dict(params)
    pages = 0
    while True:

        async def get(q: Mapping[str, str | int] = query) -> dict[str, Any]:
            return await erp.get_feed(path, q)

        body = await with_backoff(
            get,
            attempts=FEED_ATTEMPTS,
            base=FEED_BACKOFF_BASE,
            cap=FEED_BACKOFF_CAP,
            retry_on=_retryable,
            sleep=erp.sleep,
        )
        page = parse_page(model, body)
        pages += 1
        yield page
        if page.next is None:
            return
        if page.next == query.get("cursor"):
            raise FeedContract("runaway", f"{path}: next is the cursor just sent")
        if pages >= max_pages:
            raise FeedContract("runaway", f"{path}: more than {max_pages} pages")
        query = {**params, "cursor": page.next}


# --- the stored state (sources.erp_sync.config.suppression) ---------------------------


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


@dataclass(frozen=True, slots=True)
class SuppressionConfig:
    """What the last pulls left behind. ``last_*_at`` are the agent's clock
    (freshness), ``*_generated_at`` the ERP's (the next delta's anchor);
    ``count`` the distinct (kind, h) of the last completed, unshrunk full
    pull (the shrink rule's yardstick — never the table's row count, which
    deltas only grow)."""

    salt_id: str | None = None
    last_full_at: datetime | None = None
    last_delta_at: datetime | None = None
    full_generated_at: datetime | None = None
    delta_generated_at: datetime | None = None
    count: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> SuppressionConfig:
        if not isinstance(raw, Mapping):
            return cls()
        salt = raw.get("salt_id")
        count = raw.get("count")
        return cls(
            salt_id=salt if isinstance(salt, str) and salt else None,
            last_full_at=_stamp(raw.get("last_full_at")),
            last_delta_at=_stamp(raw.get("last_delta_at")),
            full_generated_at=_stamp(raw.get("full_generated_at")),
            delta_generated_at=_stamp(raw.get("delta_generated_at")),
            count=count if isinstance(count, int) and not isinstance(count, bool) else 0,
        )

    def to_json(self) -> dict[str, Any]:
        def iso(at: datetime | None) -> str | None:
            return at.isoformat() if at is not None else None

        return {
            "salt_id": self.salt_id,
            "last_full_at": iso(self.last_full_at),
            "last_delta_at": iso(self.last_delta_at),
            "full_generated_at": iso(self.full_generated_at),
            "delta_generated_at": iso(self.delta_generated_at),
            "count": self.count,
        }


async def suppression_config(session: AsyncSession) -> SuppressionConfig:
    raw = await session.scalar(
        sa.select(Source.config["suppression"])
        .where(Source.key == ERP_SOURCE)
        .execution_options(populate_existing=True)
    )
    return SuppressionConfig.from_json(raw)


async def _store_config(session: AsyncSession, cfg: SuppressionConfig) -> None:
    """``config.suppression`` replaced, the rest of ``config`` (the outcomes
    cursor, T-10) left as it stands."""
    await session.execute(
        sa.update(Source)
        .where(Source.key == ERP_SOURCE)
        .values(
            config=sa.func.jsonb_set(
                Source.config,
                sa.text("'{suppression}'"),
                sa.cast(sa.bindparam("suppression", cfg.to_json(), type_=JSONB), JSONB),
                True,
            )
        )
    )


# --- freshness: the one home ----------------------------------------------------------


def freshness(
    cfg: SuppressionConfig | None,
    *,
    key: str,
    now: datetime,
    configured: bool | None = None,
) -> health.SuppressionHealth:
    """Fresh when the feed is configured and the last completed full pull is
    at most ``FULL_MAX_AGE_H`` old, the last completed delta at most
    ``DELTA_MAX_AGE_H`` (a full pull counts as one), and the stored
    ``salt_id`` is the current key's. Feed not configured ⇒ fresh: the XLSX
    fallback carries the gate (05 §B.3). ``stale_since`` is the moment the
    first limit ran out, when it can be known."""
    on = feeds_configured() if configured is None else configured
    full_at = cfg.last_full_at if cfg is not None else None
    delta_at = cfg.last_delta_at if cfg is not None else None
    full_limit = timedelta(hours=FULL_MAX_AGE_H)
    delta_limit = timedelta(hours=DELTA_MAX_AGE_H)
    if not on:
        return health.SuppressionHealth(
            True, full_at, delta_at, FULL_MAX_AGE_H, DELTA_MAX_AGE_H
        )
    salt_ok = cfg is not None and cfg.salt_id is not None and cfg.salt_id == salt_id(key)
    full_ok = full_at is not None and now - full_at <= full_limit
    delta_ok = delta_at is not None and now - delta_at <= delta_limit
    ran_out = [
        at + limit
        for at, limit, ok in (
            (full_at, full_limit, full_ok),
            (delta_at, delta_limit, delta_ok),
        )
        if at is not None and not ok
    ]
    return health.SuppressionHealth(
        fresh=full_ok and delta_ok and salt_ok,
        full_at=full_at,
        delta_at=delta_at,
        full_max_h=FULL_MAX_AGE_H,
        delta_max_h=DELTA_MAX_AGE_H,
        salt_ok=salt_ok,
        stale_since=min(ran_out) if ran_out else None,
    )


def suppression_fresh(
    cfg: SuppressionConfig | None,
    *,
    key: str,
    now: datetime,
    configured: bool | None = None,
) -> bool:
    """The gate's question (02 §2.8, 02 §2.10 rule 2, 03 §2.2, 04's gate,
    06 §4.2): false → every push is refused ``stale``."""
    return freshness(cfg, key=key, now=now, configured=configured).fresh


async def suppression_probe(
    session: AsyncSession, now: datetime
) -> health.SuppressionHealth:
    """``health``'s probe for ``suppression_stale`` (``register_health_probe``)."""
    cfg = await suppression_config(session)
    return freshness(cfg, key=settings.crm_agent_key, now=now)


def register_health_probe() -> None:
    """Called by the processes that judge health (the worker, the bot): the
    probe is not registered at import, so a test that imports this module
    leaves ``health`` as it found it."""
    health.set_suppression_probe(suppression_probe)


# --- the breaker and the owner -------------------------------------------------------


def breaker_reason(key: BreakerKey, status: int) -> str:
    """``push_404`` / ``erp_redirect:<status>`` (health reads «<key>[:<detail>]»)."""
    return key if key == PUSH_404_PROBLEM else f"{key}:{status}"


async def trip_breaker(
    session: AsyncSession, key: BreakerKey, *, status: int, now: datetime
) -> None:
    """``sources.erp_sync.breaker_until = now + 6 h`` with the reason (05 §B.1).
    The caller's transaction writes it."""
    await session.execute(
        sa.update(Source)
        .where(Source.key == ERP_SOURCE)
        .values(
            breaker_until=now + BREAKER_FOR, breaker_reason=breaker_reason(key, status)
        )
    )


async def clear_breaker(session: AsyncSession) -> bool:
    """A feed that answered 200 proves the key, the source and the address:
    an ERP breaker in force is over (B.5: «until a 2xx»). Another breaker
    reason on the row is left alone. True when one was cleared."""
    result = await session.execute(
        sa.update(Source)
        .where(
            Source.key == ERP_SOURCE,
            Source.breaker_until.is_not(None),
            sa.or_(
                *(
                    sa.or_(
                        Source.breaker_reason == k,
                        Source.breaker_reason.startswith(k + ":"),
                    )
                    for k in health.ERP_BREAKER_KEYS
                )
            ),
        )
        .values(breaker_until=None, breaker_reason=None)
        .returning(Source.key)
    )
    return result.first() is not None


def breaker_text(key: BreakerKey, status: int) -> str:
    return (
        PUSH_404_TEXT
        if key == PUSH_404_PROBLEM
        else ERP_REDIRECT_TEXT.format(code=status)
    )


async def tell_breaker(key: BreakerKey, *, status: int, now: datetime) -> bool:
    """The owner, at once and at most once per ``BREAKER_FOR`` (05 §B.1:
    ``reminder_log(kind='alert:<key>')``), through the owner notifier the
    worker registers (``costs.set_cap_notifier``). True when delivered."""
    return await costs.tell_once(
        key, breaker_text(key, status), now=now, since=now - BREAKER_FOR
    )


# --- one walk applied -----------------------------------------------------------------


@dataclass(slots=True)
class SyncReport:
    """What one ``sync_suppression`` did; it lands in ``collector_runs.meta``."""

    mode: SyncMode
    walk: SyncMode
    status: Literal["ok", "shrunk", "salt_changed"] = "ok"
    pages: int = 0
    entries: int = 0
    seen: int = 0
    previous: int = 0
    deleted: int = 0
    retired_manual: int = 0
    salt_changed: bool = False
    selftest: bool | None = None
    since: str | None = None
    generated_at: str | None = None

    def as_meta(self) -> dict[str, Any]:
        """Every field that holds an answer (a ``selftest`` of False is one)."""
        return {k: v for k, v in asdict(self).items() if v is not None}


async def sync_suppression(
    session: AsyncSession,
    erp: ErpClient,
    *,
    mode: SyncMode,
    now: datetime,
    accept_shrink: bool = False,
) -> SyncReport:
    """Bring ``suppression``'s ``erp_feed`` rows to the ERP's set (05 §B.2).

    Pages are committed as they arrive, so a pull that fails midway keeps
    what it upserted and deletes nothing. A delta whose first page carries a
    new ``salt_id`` applies nothing and comes back ``status='salt_changed'``:
    the caller runs a full pull now. A failure is recorded (``health``'s
    rows, the breaker, the owner) and raised."""
    cfg = await suppression_config(session)
    params: dict[str, str | int] = {"limit": SUPPRESSION_LIMIT}
    walk: SyncMode = "full"
    if mode == "delta":
        anchor = cfg.delta_generated_at or cfg.full_generated_at
        if anchor is not None:
            walk = "delta"
            params["since"] = fmt_since(anchor - DELTA_OVERLAP)
    report = SyncReport(mode=mode, walk=walk, since=_as_str(params.get("since")))
    try:
        return await _apply_walk(session, erp, cfg, params, report, now, accept_shrink)
    except FeedError as exc:
        await record_failure(session, exc, feed=FEED_SUPPRESSION, now=now)
        raise


def _as_str(value: str | int | None) -> str | None:
    return None if value is None else str(value)


async def _apply_walk(
    session: AsyncSession,
    erp: ErpClient,
    cfg: SuppressionConfig,
    params: Mapping[str, str | int],
    report: SyncReport,
    now: datetime,
    accept_shrink: bool,
) -> SyncReport:
    seen: set[tuple[str, str]] = set()
    salt: str | None = None
    generated: datetime | None = None
    walk = fetch_pages(erp, SUPPRESSION_PATH, params=params, model=SuppressionPage)
    async with contextlib.aclosing(walk):
        async for page in walk:
            if salt is None:
                if page.rule != FEED_RULE:
                    raise FeedContract("rule_changed", "the hash rule moved")
                salt, generated = page.salt_id, page.generated
                report.generated_at = page.generated_at
                # The key was re-minted (a first pull has nothing to differ from).
                report.salt_changed = cfg.salt_id is not None and salt != cfg.salt_id
                if report.walk == "delta" and salt != cfg.salt_id:
                    report.status = "salt_changed"
                    log.warning(
                        "erp feed: a new salt_id on a delta — a full pull runs now",
                        extra={"feed": FEED_SUPPRESSION},
                    )
                    return report
                await clear_breaker(session)
            elif page.salt_id != salt or page.rule != FEED_RULE:
                raise FeedShapeError("the salt_id or the rule moved inside one walk")
            entries = [parse_entry(e) for e in page.entries]
            await _upsert(session, entries, salt=salt, seen_at=now)
            await session.commit()
            report.pages += 1
            report.entries += len(entries)
            seen.update((e.kind, e.h) for e in entries)
    assert salt is not None and generated is not None
    report.seen = len(seen)
    if report.walk == "full":
        await _finish_full(session, erp, cfg, report, salt, generated, now, accept_shrink)
    else:
        await _store_config(
            session,
            replace(cfg, salt_id=salt, last_delta_at=now, delta_generated_at=generated),
        )
    await _clear_feed_contract(session, FEED_SUPPRESSION)
    await session.commit()
    log.info(
        "erp suppression %s: %d pages, %d entries, %d deleted",
        report.walk,
        report.pages,
        report.entries,
        report.deleted,
        extra={"feed": FEED_SUPPRESSION, "status": report.status},
    )
    return report


def _dedup(entries: Sequence[FeedEntry]) -> list[FeedEntry]:
    """One row per (kind, h) for one INSERT — the ERP does not deduplicate
    (two cards, one phone, A.7); the latest ``at`` stands."""
    best: dict[tuple[str, str], FeedEntry] = {}
    for e in entries:
        known = best.get((e.kind, e.h))
        if known is None or e.at > known.at:
            best[(e.kind, e.h)] = e
    return list(best.values())


async def _upsert(
    session: AsyncSession, entries: Sequence[FeedEntry], *, salt: str, seen_at: datetime
) -> None:
    """Step 3: ``(kind, 'phone_hash', h, salt_id, 'erp_feed', erp_at, last_seen_at)``,
    on conflict ``(kind, key_type, key)`` the seen stamp, ``erp_at`` and
    ``salt_id``. The upsert is the deduplication."""
    rows = [
        {
            "kind": e.kind,
            "key_type": "phone_hash",
            "key": e.h,
            "salt_id": salt,
            "source": "erp_feed",
            "erp_at": e.at,
            "last_seen_at": seen_at,
        }
        for e in _dedup(entries)
    ]
    for start in range(0, len(rows), UPSERT_CHUNK):
        stmt = pg_insert(Suppression).values(rows[start : start + UPSERT_CHUNK])
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_suppression_key",
                set_={
                    "last_seen_at": stmt.excluded.last_seen_at,
                    "erp_at": stmt.excluded.erp_at,
                    "salt_id": stmt.excluded.salt_id,
                },
            )
        )


async def _finish_full(
    session: AsyncSession,
    erp: ErpClient,
    cfg: SuppressionConfig,
    report: SyncReport,
    salt: str,
    generated: datetime,
    now: datetime,
    accept_shrink: bool,
) -> None:
    """Step 4 (the only delete) and step 5 of a completed full pull."""
    report.previous = cfg.count
    shrunk = (
        cfg.count > 0 and report.seen < SHRINK_FLOOR * cfg.count and not accept_shrink
    )
    count = cfg.count
    if shrunk:
        report.status = "shrunk"
        pct = round(100 * (cfg.count - report.seen) / cfg.count)
        await health.raise_problem(
            session,
            SHRUNK_PROBLEM,
            now=now,
            by=WRITER,
            detail={"pct": pct, "seen": report.seen, "previous": cfg.count},
        )
        log.warning(
            "erp suppression: the full pull shrank %d%% — nothing deleted",
            pct,
            extra={"feed": FEED_SUPPRESSION},
        )
    else:
        deleted = await session.execute(
            sa.delete(Suppression)
            .where(Suppression.source == "erp_feed", Suppression.last_seen_at < now)
            .returning(Suppression.id)
        )
        report.deleted = len(deleted.all())
        count = report.seen
        await health.clear_problem(session, SHRUNK_PROBLEM)
    # A full pull is as fresh as a delta: both clocks move (freshness).
    await _store_config(
        session,
        replace(
            cfg,
            salt_id=salt,
            last_full_at=now,
            last_delta_at=now,
            full_generated_at=generated,
            count=count,
        ),
    )
    # (a) the stale hits are covered by this pull.
    await health.clear_problem(session, STALE_HIT_PROBLEM)
    # (b) the self-test.
    report.selftest = await _self_test(session, erp, salt, now)
    # (c) the XLSX fallback retires row by row.
    report.retired_manual = await _retire_manual_import(session, erp, salt)


async def _self_test(
    session: AsyncSession, erp: ErpClient, salt: str, now: datetime
) -> bool | None:
    """06 §4.2: the hash of the tail of the newest ``landed_client`` / ``won``
    outcome must be in the feed. None when there is no such outcome yet. A
    warning only: a client the ERP recognised by a 7–8-digit card fails it
    spuriously (A.8)."""
    tail = await session.scalar(
        sa.select(PushLog.phone_tail)
        .join(Outcome, Outcome.external_id == PushLog.external_id)
        .where(Outcome.label.in_(("landed_client", "won")))
        .order_by(Outcome.label_changed_at.desc(), PushLog.sent_at.desc())
        .limit(1)
    )
    if tail is None:
        await health.clear_problem(session, SELFTEST_PROBLEM)
        return None
    present = await session.scalar(
        sa.select(sa.func.count(Suppression.id)).where(
            Suppression.source == "erp_feed",
            Suppression.key_type == "phone_hash",
            Suppression.kind == "client",
            Suppression.key == erp.hash_tail(tail.strip()),
            Suppression.salt_id == salt,
        )
    )
    if present:
        await health.clear_problem(session, SELFTEST_PROBLEM)
        return True
    await health.raise_problem(
        session, SELFTEST_PROBLEM, now=now, by=WRITER, detail={"salt_id": salt}
    )
    log.warning("erp suppression: a known client is not in the feed (self-test)")
    return False


async def _retire_manual_import(session: AsyncSession, erp: ErpClient, salt: str) -> int:
    """Step 5(c): each ``manual_import`` tail whose hash this pull carries is
    retired; a tail the feed does not carry stays (fail closed)."""
    rows = (
        await session.execute(
            sa.select(Suppression.id, Suppression.key).where(
                Suppression.source == "manual_import",
                Suppression.key_type == "phone_tail",
            )
        )
    ).all()
    if not rows:
        return 0
    by_hash: dict[str, list[int]] = {}
    for row_id, tail in rows:
        by_hash.setdefault(erp.hash_tail(tail), []).append(row_id)
    covered: set[str] = set()
    hashes = list(by_hash)
    for start in range(0, len(hashes), UPSERT_CHUNK):
        covered.update(
            await session.scalars(
                sa.select(Suppression.key).where(
                    Suppression.source == "erp_feed",
                    Suppression.key_type == "phone_hash",
                    Suppression.salt_id == salt,
                    Suppression.key.in_(hashes[start : start + UPSERT_CHUNK]),
                )
            )
        )
    doomed = [row_id for h in covered for row_id in by_hash[h]]
    for start in range(0, len(doomed), UPSERT_CHUNK):
        await session.execute(
            sa.delete(Suppression).where(
                Suppression.id.in_(doomed[start : start + UPSERT_CHUNK])
            )
        )
    return len(doomed)


# --- what a failure writes ------------------------------------------------------------


def contract_code(exc: FeedError) -> ContractCode | None:
    """The ``feed_contract`` code of a failure, None when it is not one."""
    if isinstance(exc, FeedContract):
        return exc.code
    if isinstance(exc, FeedShapeError):
        return "shape"
    if isinstance(exc, ErpHttpError) and (exc.status == 304 or 200 <= exc.status < 300):
        # Never asked for (no If-None-Match), or a 2xx that is not the 200
        # the feed answers: not the documented body.
        return "shape"
    if isinstance(exc, ErpHttpError) and 400 <= exc.status < 500:
        if exc.status in MISCONFIGURED or exc.status == 429:
            return None
        return "bad_request"
    return None


# A 404 is the door off, the key wrong or the X-ERP-Key alone (A.5); a proxy
# in front of the ERP says the same with 401/403 (03 §2.2).
MISCONFIGURED: Final = frozenset({401, 403, 404})


def breaker_key(exc: FeedError) -> BreakerKey | None:
    if not isinstance(exc, ErpHttpError):
        return None
    if exc.status in MISCONFIGURED:
        return PUSH_404_PROBLEM
    if 300 <= exc.status < 400 and exc.status != 304:
        return ERP_REDIRECT_PROBLEM
    return None


async def record_failure(
    session: AsyncSession, exc: FeedError, *, feed: FeedName, now: datetime
) -> None:
    """What 05 §B.6 says a failed feed call leaves behind, committed before
    the error goes on (the runner rolls the run's session back)."""
    await session.rollback()
    status = exc.status if isinstance(exc, ErpHttpError) else None
    key = breaker_key(exc)
    code = contract_code(exc)
    if key is not None and status is not None:
        await trip_breaker(session, key, status=status, now=now)
        await session.commit()
        log.warning(
            "erp feed: HTTP %d — breaker %s for %s",
            status,
            key,
            BREAKER_FOR,
            extra={"feed": feed, "problem": key},
        )
        try:
            await tell_breaker(key, status=status, now=now)
        except Exception as error:
            log.warning(
                "erp breaker alert not told", extra={"error": type(error).__name__}
            )
        return
    if code is not None:
        detail: dict[str, Any] = {"code": code, "feed": feed}
        if isinstance(exc, ErpHttpError):
            detail.update(status=exc.status, erp=exc.code)
        await health.raise_problem(
            session, FEED_CONTRACT_PROBLEM, now=now, by=WRITER, detail=detail
        )
        await session.commit()
        log.warning(
            "erp feed contract broken (%s)", code, extra={"feed": feed, "code": code}
        )
        return
    log.warning(
        "erp feed failed: %s", type(exc).__name__, extra={"feed": feed, "status": status}
    )


async def _clear_feed_contract(session: AsyncSession, feed: FeedName) -> None:
    """A walk that went through ends ITS feed's contract problem; one the
    other feed raised stays until that feed reads cleanly."""
    row = await session.get(
        RuntimeFlag,
        health.PROBLEM_FLAG_PREFIX + FEED_CONTRACT_PROBLEM,
        populate_existing=True,
    )
    if row is None:
        return
    value = row.value if isinstance(row.value, Mapping) else {}
    detail = value.get("detail")
    if isinstance(detail, Mapping) and detail.get("feed") not in (None, feed):
        return
    await health.clear_problem(session, FEED_CONTRACT_PROBLEM)


__all__ = [
    "DELTA_MAX_AGE_H",
    "ERP_KEY_HEADERS",
    "FEED_RULE",
    "FULL_MAX_AGE_H",
    "MAX_PAGES",
    "OUTCOMES_LIMIT",
    "OUTCOMES_PATH",
    "PII_KEYS",
    "SUPPRESSION_LIMIT",
    "SUPPRESSION_PATH",
    "ErpClient",
    "ErpHttpError",
    "FeedContract",
    "FeedEntry",
    "FeedError",
    "FeedShapeError",
    "OutcomesPage",
    "SuppressionConfig",
    "SuppressionPage",
    "SyncReport",
    "feeds_configured",
    "fetch_pages",
    "fmt_since",
    "freshness",
    "hash_tail",
    "parse_entry",
    "salt_id",
    "suppression_fresh",
    "sync_suppression",
]
