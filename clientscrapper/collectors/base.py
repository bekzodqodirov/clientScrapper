"""The collector contract (03 §1.2): ``Observation``, ``Budget``, ``Collector``.

A collector is a set of pure, synchronous parsers —
``parse_<page>(body: bytes, *, url: str, fetched_at: datetime) ->
list[Observation]``, tested on recorded fixtures — plus a thin ``collect``
that fetches through the ``Fetcher`` it was given and calls them. The runner
(``collectors/runner.py``), not the collector, writes evidence, observations,
``collector_runs`` and the heartbeat.

``Collector.key`` **is** a ``sources.key``: the schedulable row whose
``enabled``, ``paused_until``, ``daily_page_budget`` and ``daily_cost_cap_usd``
the runner reads. ``Observation.source`` may name an attribution-only
sub-source (``search_brave``, ``directories_goldenpages``, ``maps_google``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal

from clientscrapper.services.health import collector_component
from clientscrapper.services.http import BudgetExhausted, Fetcher

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

Kind = Literal[
    "company",
    "listing",
    "post",
    "vacancy",
    "exhibitor",
    "place",
    "serp_hit",
    "suppression",
    "outcome",
]
Lang = Literal["uz-Latn", "uz-Cyrl", "ru", "en"]

# What collectors emit; 04 scores them (03 §1.2).
SIGNAL_TYPES: Final = (
    "container_truck",
    "volume_m3_3plus",
    "volume_m3_1to3",
    "wholesale_bulky",
    "directory_wholesale",
    "ved_vacancy",
    "exhibitor",
    "marketplace_velocity",
    "brand_distributor",
    "china_origin",
    "entity_solid",
    "contact_phone",
    "contact_handle",
    "intent_ask",
    "b2c_marker",
    "non_china_origin",
    "state_body",
    "liquidated",
    "competitor",
    "suppressed",
)


@dataclass(slots=True)
class Observation:
    source: str  # sources.key, e.g. "olx_business"
    source_ref: str  # stable id inside the source
    url: str
    fetched_at: datetime
    raw_sha256: str  # evidence sha256; "" for API rows the runner hashes itself
    kind: Kind
    company_name_raw: str | None = None
    legal_form: str | None = None  # MChJ/OOO/ООО/XK/YaTT/ИП/AJ/QK… as written
    stir: str | None = None
    phones: list[str] = field(default_factory=list)  # as written
    telegram: str | None = None  # company/channel handle, no "@"
    instagram: str | None = None
    website: str | None = None  # full URL as found
    address: str | None = None
    city: str | None = None
    goods_text: str | None = None
    volume_text: str | None = None  # the verbatim fragment that carried volume
    origin_text: str | None = None  # the verbatim fragment that carried China origin
    signal_types: list[str] = field(default_factory=list)
    excerpt: str = ""  # ≤600 chars, verbatim, what the card will quote
    person_name: str | None = None  # person-tier: at most one, only self-published
    person_role: str | None = None
    person_telegram: str | None = None  # a group poster's username (person-tier)
    external_ids: dict[str, str] = field(default_factory=dict)
    attrs: dict[str, Any] = field(default_factory=dict)
    published_at: datetime | None = None
    lang: Lang | None = None


@dataclass(slots=True)
class Budget:
    """One run's allowance. ``pages`` = ``sources.daily_page_budget`` minus the
    pages already used today; ``usd`` = ``sources.daily_cost_cap_usd`` minus
    today's spend of the source (04 §8); ``deadline`` the wall-clock stop the
    Fetcher checks before every request."""

    pages: int
    usd: Decimal
    deadline: datetime
    pages_taken: int = 0
    spent_usd: Decimal = Decimal("0")

    def take_page(self) -> None:
        if self.pages <= 0:
            raise BudgetExhausted("pages")
        self.pages -= 1
        self.pages_taken += 1

    def can_spend(self, est: Decimal) -> bool:
        return est <= self.usd

    def charge(self, usd: Decimal) -> None:
        self.usd -= usd
        self.spent_usd += usd


class Collector(ABC):
    """Subclasses set the class variables. ``heartbeat`` is derived, never
    set: ``health.collector_component(key)`` — the name the runner beats
    (with ``.variant`` for a cadence variant, 03 §1.2) — so a subclass that
    sets one of its own is refused at class creation rather than ignored.
    ``found`` is what the runner keeps when ``collect`` stops on a budget or a
    breaker: a collector that appends to it as it goes loses nothing it
    already parsed."""

    key: ClassVar[str]
    family: ClassVar[str]
    phase: ClassVar[int] = 1
    default_cron: ClassVar[str | None] = None
    default_rps: ClassVar[float] = 0.3
    default_page_budget: ClassVar[int] = 500
    # Seeds sources.daily_cost_cap_usd.
    default_usd_cap: ClassVar[Decimal] = Decimal("0")
    heartbeat: ClassVar[str]
    # Bumped when a parser's output shape changes; parsed.collector_version.
    version: ClassVar[int] = 1

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "heartbeat" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__}.heartbeat is derived from its key "
                "(health.collector_component); the runner beats that name"
            )
        key = cls.__dict__.get("key")
        if key is not None:
            cls.heartbeat = collector_component(key)

    def __init__(self, fetcher: Fetcher, serp: object | None = None) -> None:
        # ``serp`` is services/serp.SerpClient once T-25 builds it.
        self.fetcher = fetcher
        self.serp = serp
        self.variant: str | None = None
        self.found: list[Observation] = []

    @abstractmethod
    async def collect(self, session: AsyncSession, budget: Budget) -> list[Observation]:
        """Fetch, parse, return every observation of this run."""
