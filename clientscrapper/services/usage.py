# VENDORED_FROM dailyJournal@6b1f67f miya/services/usage.py
"""Prices of every paid call and the daily budget tables (04 §8, 03 §1.3).

Pure on purpose: nothing from ``clientscrapper`` is imported at module level.
``tools/doctor.py`` reads this module to say whether the configured model ids
are priced and to print the allocations table, and the doctor must run on a
`.env` that the settings module refuses to load; the ``clientscrapper.db``
package imports the settings, so even the ORM models are out of reach here.
That is why the tables live here and ``costs.py`` re-exports them, and why
writing a ``usage_log`` row (MIYA's ``record_anthropic_usage``) moved to
``costs.py``: the row belongs to ``costs.ask``'s own transaction.

Every price is a ``Decimal``; a cost is rounded *up* to the column's six
places, because the cap must never undercount. ``None`` means unpriced, and
``costs.ask`` refuses an unpriced call before it is made.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from decimal import ROUND_UP, Decimal, InvalidOperation
from typing import Final, Literal, NamedTuple, get_args
from uuid import UUID

D = Decimal
ZERO: Final = D(0)

# 04 §8 ``Operation``, verbatim; 02 §2.13's CHECK is the same tuple
# (``tests/test_costs.py`` holds the two equal).
Operation = Literal[
    "classify",
    "verdict",
    "company_verdict",
    "draft",
    "enrich_search",
    "serp_brave",
    "serp_yandex_night",
    "serp_searxng",
    "serp_ddg",
    "places_text_ids",
    "places_details_contact",
    "twogis_api",
    "apify_olx",
    "apify_2gis",
    "apify_instagram",
    "apify_uzum",
    "proxy_uzum",
    "model_reserve",
    "serp_reserve",
    "other",
]
OPERATIONS: Final[tuple[Operation, ...]] = get_args(Operation)

Family = Literal["anthropic", "serp", "places", "apify", "proxy"]
FAMILIES: Final[tuple[Family, ...]] = get_args(Family)

Provider = Literal[
    "anthropic",
    "brave",
    "yandex_search",
    "google_places",
    "twogis",
    "apify",
    "proxy",
    "searxng",
    "hh",
    "other",
]

# USD per Tashkent day; Σ = 10.00 = DAILY_COST_CAP_USD's default.
ALLOCATIONS: Final[Mapping[Operation, Decimal]] = {
    # Claude 5.00
    "classify": D("0.30"),
    "verdict": D("1.50"),
    "company_verdict": D("1.50"),
    "draft": D("0.20"),
    "enrich_search": D("0.75"),
    "model_reserve": D("0.75"),
    # SERP 1.00. Brave 0.165 (07 §1.9 pins it) keeps it under the $5 monthly
    # credit; reaching an allocation is over it (04 §8 rung 5, the same ≥ as
    # the daily cap), so 32 queries a day pass: 0.160 + 0.005 is refused. The
    # «33 queries» beside this number in 03 §1.3 / 07 §1.9 is 0.165 / 0.005,
    # which the ≥ rule never reaches.
    "serp_brave": D("0.165"),
    "serp_yandex_night": D("0"),
    "serp_searxng": D("0"),
    "serp_ddg": D("0"),
    "serp_reserve": D("0.835"),
    # Places 1.00
    "places_text_ids": D("0"),
    "places_details_contact": D("1.00"),
    "twogis_api": D("0"),
    # Apify 2.00
    "apify_olx": D("0.60"),
    "apify_2gis": D("0.40"),
    "apify_instagram": D("0.40"),
    "apify_uzum": D("0.60"),
    # proxy 1.00
    "proxy_uzum": D("1.00"),
}

FAMILY: Final[Mapping[Operation, Family]] = {
    "classify": "anthropic",
    "verdict": "anthropic",
    "company_verdict": "anthropic",
    "draft": "anthropic",
    "enrich_search": "anthropic",
    "model_reserve": "anthropic",
    "serp_brave": "serp",
    "serp_yandex_night": "serp",
    "serp_searxng": "serp",
    "serp_ddg": "serp",
    "serp_reserve": "serp",
    "places_text_ids": "places",
    "places_details_contact": "places",
    "twogis_api": "places",
    "apify_olx": "apify",
    "apify_2gis": "apify",
    "apify_instagram": "apify",
    "apify_uzum": "apify",
    "proxy_uzum": "proxy",
}

FAMILY_CAPS: Final[Mapping[Family, Decimal]] = {
    "anthropic": D("5.00"),
    "serp": D("1.00"),
    "places": D("1.00"),
    "apify": D("2.00"),
    "proxy": D("1.00"),
}

# The pool an op may borrow from once its own allocation is spent. Places,
# Apify and the proxy never borrow; a SERP op borrows only while the owner's
# switch ``sources.config.serp_paid_overflow`` is on (``costs.ask``).
RESERVES: Final[Mapping[Family, Operation]] = {
    "anthropic": "model_reserve",
    "serp": "serp_reserve",
}

# ``usage_log.provider`` of each op: a refusal row names the provider the
# call would have written (04 §8 step 9). DDG has no provider of its own.
PROVIDER: Final[Mapping[Operation, Provider]] = {
    "classify": "anthropic",
    "verdict": "anthropic",
    "company_verdict": "anthropic",
    "draft": "anthropic",
    "enrich_search": "anthropic",
    "model_reserve": "anthropic",
    "serp_brave": "brave",
    "serp_yandex_night": "yandex_search",
    "serp_searxng": "searxng",
    "serp_ddg": "other",
    "serp_reserve": "other",
    "places_text_ids": "google_places",
    "places_details_contact": "google_places",
    "twogis_api": "twogis",
    "apify_olx": "apify",
    "apify_2gis": "apify",
    "apify_instagram": "apify",
    "apify_uzum": "apify",
    "proxy_uzum": "proxy",
    "other": "other",
}

# The ops ``costs.ask`` may be asked for. The two reserves are pools, not
# calls, and ``other`` has no budget of its own: asking for one is a bug.
CALLABLE: Final[frozenset[Operation]] = frozenset(
    op for op in OPERATIONS if op not in RESERVES.values() and op != "other"
)

# Query counts per call for the ops whose cap is a count (03 §1.3): one call
# is one query, so ``costs.ask`` reserves one unit before the call and keeps
# at least that one when the answer is priced.
UNITS_PER_CALL: Final = D(1)
# The free ops' daily caps of 03 §1.3 — free calls skip the money rungs, so
# their cap is a count.
DAILY_UNIT_CAPS: Final[Mapping[Operation, Decimal]] = {
    "serp_yandex_night": D(35),
    "serp_searxng": D(300),
    "serp_ddg": D(40),
    "places_text_ids": D(200),
    "twogis_api": D(40),
}
# Calendar-month counts: Brave's $5 credit (past it the card is charged
# silently) and Yandex's free night quota (paid Yandex is never used in v1).
MONTHLY_UNIT_CAPS: Final[Mapping[Operation, Decimal]] = {
    "serp_brave": D(1000),
    "serp_yandex_night": D(1000),
}
# Local (TIMEZONE) hours an op may run in, [start, end): the Yandex free
# quota holds only 00:00–07:59 Moscow = 02:00–09:59 Tashkent (03 §2.1).
OP_WINDOWS: Final[Mapping[Operation, tuple[time, time]]] = {
    "serp_yandex_night": (time(2, 0), time(10, 0)),
}

# --- model prices -------------------------------------------------------------


class ModelPrice(NamedTuple):
    """USD per million tokens; the cache prices are optional in an override."""

    input: Decimal
    output: Decimal
    cache_read: Decimal | None = None
    cache_write: Decimal | None = None


def parse_price(text: str) -> ModelPrice | None:
    """'0.10/0.50' or '0.10/0.50/0.01/0.125' → ModelPrice; None when unreadable.

    Its home is here (04 §8 «Pricing»), not in the settings, because the doctor
    reads the overrides from a raw environment; ``config`` imports it."""
    parts = [p.strip() for p in (text or "").split("/")]
    if len(parts) not in (2, 4):
        return None
    try:
        values = [Decimal(p) for p in parts]
    except InvalidOperation:
        return None
    if any(not v.is_finite() or v < 0 for v in values):
        return None
    return ModelPrice(*values)


# Code, keyed by the configured ids (04 §8). There is no MODEL_PRICES in
# `.env`; a price change for a configured role is a *_MODEL_PRICE edit.
MODEL_PRICES: Final[Mapping[str, ModelPrice]] = {
    "claude-haiku-5-5": ModelPrice(D("0.10"), D("0.50"), D("0.01"), D("0.125")),
    "claude-sonnet-5-5": ModelPrice(D("2"), D("10"), D("0.20"), D("2.50")),
    "claude-opus-5-5": ModelPrice(D("4"), D("20"), D("0.20"), D("5.00")),
}


@dataclass(frozen=True, slots=True)
class ModelRole:
    """One configured model: its env key, its price override key, the shipped
    default (``config`` reads it from here) and what it does, for the doctor."""

    key: str
    price_key: str
    default: str
    operations: tuple[Operation, ...]
    label_uz: str


MODEL_ROLES: Final[tuple[ModelRole, ...]] = (
    ModelRole(
        "EXTRACT_MODEL",
        "EXTRACT_MODEL_PRICE",
        "claude-haiku-5-5",
        ("classify", "draft", "enrich_search"),
        "tasnif, qoralama, kontakt qidiruvi",
    ),
    ModelRole(
        "VERDICT_MODEL",
        "VERDICT_MODEL_PRICE",
        "claude-sonnet-5-5",
        ("verdict",),
        "xulosa",
    ),
    ModelRole(
        "BATCH_MODEL",
        "BATCH_MODEL_PRICE",
        "claude-opus-5-5",
        ("company_verdict",),
        "tungi batch xulosasi",
    ),
)
DEFAULT_MODELS: Final[Mapping[str, str]] = {r.key: r.default for r in MODEL_ROLES}

# An override that names only in/out takes MIYA's cache ratios.
CACHE_READ_RATIO: Final = D("0.1")
CACHE_WRITE_RATIO: Final = D("1.25")
# Anthropic web search: $10 per 1,000 searches (checked 2026-10-23).
WEB_SEARCH_USD: Final = D("0.010")
_MILLION: Final = D(1_000_000)
USD_QUANTUM: Final = D("0.000001")


def resolve_price(
    model: str, roles: tuple[tuple[str, str], ...] = ()
) -> ModelPrice | None:
    """``roles`` = ((configured model id, its *_MODEL_PRICE text), …): an
    override applies to its own role's model only, then the code table.
    An unreadable override is ignored here (the settings refuse it at start)."""
    for role_model, configured in roles:
        if model == role_model and configured.strip():
            price = parse_price(configured)
            if price is not None:
                return price
    return MODEL_PRICES.get(model)


def price_for(model: str) -> ModelPrice | None:
    """The configured role's override first, then the table; None = unpriced."""
    from clientscrapper.config import settings  # late: see the module docstring

    return resolve_price(
        model,
        (
            (settings.extract_model, settings.extract_model_price),
            (settings.verdict_model, settings.verdict_model_price),
            (settings.batch_model, settings.batch_model_price),
        ),
    )


def round_usd(value: Decimal) -> Decimal:
    """Up to the column's six places: a cost is never rounded away."""
    return value.quantize(USD_QUANTUM, rounding=ROUND_UP)


def token_cost(
    price: ModelPrice,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    batch: bool = False,
    web_searches: int = 0,
) -> Decimal:
    cache_read = (
        price.cache_read
        if price.cache_read is not None
        else price.input * CACHE_READ_RATIO
    )
    cache_write = (
        price.cache_write
        if price.cache_write is not None
        else price.input * CACHE_WRITE_RATIO
    )
    tokens = (
        D(input_tokens) * price.input
        + D(output_tokens) * price.output
        + D(cache_read_tokens) * cache_read
        + D(cache_write_tokens) * cache_write
    ) / _MILLION
    if batch:
        tokens /= 2  # the Message Batches API is half price
    # Searches are billed per search, outside the token discount.
    return round_usd(tokens + D(web_searches) * WEB_SEARCH_USD)


def anthropic_cost_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    batch: bool = False,
    web_searches: int = 0,
) -> Decimal | None:
    """The cost of one call (or one estimate) on ``model``; None = unpriced,
    which ``costs.ask`` refuses before anything is sent."""
    price = price_for(model)
    if price is None:
        return None
    return token_cost(
        price,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        batch=batch,
        web_searches=web_searches,
    )


# --- unit prices (03 §1.3) ----------------------------------------------------

BRAVE_USD_PER_QUERY: Final = D("0.005")
# Places Details (contact fields): free for the first 1,000 per month, then
# $0.020 — UNVERIFIED SKU price (03 §2.9), checked on day 1.
PLACES_DETAILS_FREE_PER_MONTH: Final = D(1000)
PLACES_DETAILS_USD: Final = D("0.020")
APIFY_USD_PER_UNIT: Final[Mapping[Operation, Decimal]] = {
    "apify_olx": D("0.00199"),  # per listing
    "apify_2gis": D("0.00199"),  # per place (searchapi)
    "apify_instagram": D("0.0023"),  # per profile
    "apify_uzum": D("0.0033"),  # per product
}
FREE_OPERATIONS: Final[frozenset[Operation]] = frozenset(
    {"serp_yandex_night", "serp_searxng", "serp_ddg", "places_text_ids", "twogis_api"}
)
# Every op whose cap or price is a count of calls: the count caps above and
# the Places Details free tier. ``costs.ask`` claims one unit per call for
# these, so the count never waits for a price callback to remember it.
COUNTED_OPERATIONS: Final[frozenset[Operation]] = (
    frozenset(DAILY_UNIT_CAPS)
    | frozenset(MONTHLY_UNIT_CAPS)
    | frozenset({"places_details_contact"})
)


def free_call(operation: Operation, *, month_units_before: Decimal | int) -> bool:
    """Is this call free by definition — not by the caller's estimate? A free
    op, or a Places Details request inside the month's free tier
    (``month_units_before`` = the op's units this calendar month, claims of
    calls in flight included). Only such a call may skip the money rungs."""
    if operation in FREE_OPERATIONS:
        return True
    if operation == "places_details_contact":
        return D(month_units_before) < PLACES_DETAILS_FREE_PER_MONTH
    return False


# A proxy GB is priced as 1,000 MB: if the provider bills 1,024 MB, this
# overcounts, which is the safe direction for a cap.
MB_PER_GB: Final = D(1000)


def unit_cost(
    operation: Operation,
    units: Decimal | int,
    *,
    month_units_before: Decimal | int = 0,
    proxy_usd_per_gb: Decimal | None = None,
) -> Decimal | None:
    """The cost of ``units`` of a non-token op; None = unpriced (refused).

    ``month_units_before`` — this op's units already used in the calendar month
    (``costs.month_units``): the Places free tier depends on it.
    ``proxy_usd_per_gb`` — ``sources.config.proxy_usd_per_gb`` of the caller's
    source; the proxy is unpriced without it. Yandex is free: its window and
    the 1,000/month quota are ``costs.ask`` rungs, not a price."""
    n = D(units)
    if n < 0:
        raise ValueError("units < 0")
    if operation in FREE_OPERATIONS:
        return D(0)
    if operation == "serp_brave":
        return round_usd(n * BRAVE_USD_PER_QUERY)
    if operation == "places_details_contact":
        free_left = max(D(0), PLACES_DETAILS_FREE_PER_MONTH - D(month_units_before))
        return round_usd(max(D(0), n - free_left) * PLACES_DETAILS_USD)
    if operation in APIFY_USD_PER_UNIT:
        return round_usd(n * APIFY_USD_PER_UNIT[operation])
    if operation == "proxy_uzum":
        if proxy_usd_per_gb is None or not proxy_usd_per_gb.is_finite():
            return None
        if proxy_usd_per_gb < 0:
            return None
        return round_usd(n * proxy_usd_per_gb / MB_PER_GB)
    raise ValueError(f"{operation} is not priced per unit")


# --- what a call cost ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Priced:
    """What ``costs.ask``'s ``price`` callback says one answer cost — one
    ``usage_log`` row. A batch submit returns one per job (provisional: the
    per-job estimate, no tokens), later settled in place.

    ``cost_usd=None``: the answer could not be priced; the row keeps the
    estimate (never undercounts). ``model_job_id``/``company_id`` override the
    ask's attribution for this row."""

    cost_usd: Decimal | None
    units: Decimal = ZERO
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    batch: bool = False
    model_job_id: int | None = None
    company_id: UUID | None = None


def _count(obj: object, name: str) -> int:
    value = getattr(obj, name, 0)
    return value if isinstance(value, int) else 0


def priced_anthropic(
    model: str,
    usage: object,
    *,
    batch: bool = False,
    model_job_id: int | None = None,
    company_id: UUID | None = None,
) -> Priced:
    """One SDK ``response.usage`` (or a batch result's) as a row. Web searches
    are read from ``usage.server_tool_use.web_search_requests`` and recorded
    as the row's units."""
    input_tokens = _count(usage, "input_tokens")
    output_tokens = _count(usage, "output_tokens")
    cache_read = _count(usage, "cache_read_input_tokens")
    cache_write = _count(usage, "cache_creation_input_tokens")
    searches = _count(getattr(usage, "server_tool_use", None), "web_search_requests")
    return Priced(
        cost_usd=anthropic_cost_usd(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            batch=batch,
            web_searches=searches,
        ),
        units=D(searches),
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        batch=batch,
        model_job_id=model_job_id,
        company_id=company_id,
    )


__all__ = [
    "ALLOCATIONS",
    "APIFY_USD_PER_UNIT",
    "BRAVE_USD_PER_QUERY",
    "CALLABLE",
    "COUNTED_OPERATIONS",
    "DAILY_UNIT_CAPS",
    "DEFAULT_MODELS",
    "FAMILIES",
    "FAMILY",
    "FAMILY_CAPS",
    "FREE_OPERATIONS",
    "MODEL_PRICES",
    "MODEL_ROLES",
    "MONTHLY_UNIT_CAPS",
    "OPERATIONS",
    "OP_WINDOWS",
    "PLACES_DETAILS_FREE_PER_MONTH",
    "PLACES_DETAILS_USD",
    "PROVIDER",
    "RESERVES",
    "UNITS_PER_CALL",
    "WEB_SEARCH_USD",
    "Family",
    "ModelPrice",
    "ModelRole",
    "Operation",
    "Priced",
    "Provider",
    "anthropic_cost_usd",
    "free_call",
    "parse_price",
    "price_for",
    "priced_anthropic",
    "resolve_price",
    "round_usd",
    "token_cost",
    "unit_cost",
]
