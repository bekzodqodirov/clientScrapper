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

A collector whose bytes are not a fetch — ``telegram_monitor`` reads the rows
the Telegram reader stored (03 §2.3) — hands them to the runner with
``supply`` instead (``Supplied``), and is told where each observation landed
through ``after_write`` (``Placed``), inside the run's transaction.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
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


@dataclass(frozen=True, slots=True)
class Supplied:
    """Bytes a collector read without the Fetcher, for the runner to keep as
    the evidence of the observations that carry ``raw_sha256 == sha256``: a
    message the Telegram reader stored (``body`` its text), or an evidence
    row another process already wrote (``body`` None or its bytes) — the
    reader's weekly channel about (03 §2.3). Such a row, stored outside any
    run (``collector_run_id`` NULL) and holding no observation yet, is
    adopted: the run writes its observations against it."""

    url: str
    sha256: str
    body: bytes | None
    content_type: str
    fetched_at: datetime
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Placed:
    """Where the runner put one observation of the run: its evidence row, and
    its observation row — None when it was not written now (a duplicate of
    the last 24 h, bytes an earlier run stored)."""

    observation: Observation
    evidence_id: int
    observation_id: int | None


class Collector(ABC):
    """Subclasses set the class variables. ``heartbeat`` is derived, never
    set: ``health.collector_component(key)`` — the name the runner beats
    (with ``.variant`` for a cadence variant, 03 §1.2) — so a subclass that
    sets one of its own is refused at class creation rather than ignored.
    ``found`` is what the runner keeps when ``collect`` stops on a budget or a
    breaker: a collector that appends to it as it goes loses nothing it
    already parsed. ``started_at`` is the run's ``now``. ``run_meta`` is
    merged into the run's ``collector_runs.meta`` (a key the runner writes
    itself wins) — for a collector whose work is not observations
    (``erp_sync``'s feed report, 05 §B.2). ``unavailable()`` names a reason
    the collector cannot run now whatever its ``sources`` row says (an ERP
    feed that is not configured, 05 §B.3): the runner skips the run with it,
    as it skips a disabled row. ``supplied`` holds the bytes ``supply`` was
    given; ``evidence_root`` is where the run keeps evidence files (the
    runner sets it), for a collector that reads stored ones back."""

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
        self.run_meta: dict[str, Any] = {}
        # The run's ``now`` (the runner sets it before ``collect``): the clock
        # of a collector whose work is not a fetch (the ERP feeds' ``now``).
        self.started_at: datetime | None = None
        self.supplied: dict[str, Supplied] = {}
        self.evidence_root: Path | None = None

    @classmethod
    def unavailable(cls) -> str | None:
        """A reason this collector cannot run now (None = it can)."""
        return None

    @abstractmethod
    async def collect(self, session: AsyncSession, budget: Budget) -> list[Observation]:
        """Fetch, parse, return every observation of this run."""

    def supply(
        self,
        url: str,
        body: bytes | None,
        *,
        content_type: str,
        fetched_at: datetime,
        sha256: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Hand the runner bytes this run did not fetch; the sha256 its
        observations carry as ``raw_sha256``. Without ``body`` the bytes are
        an evidence row already, named by ``sha256``."""
        if body is not None:
            sha = hashlib.sha256(body).hexdigest()
            if sha256 is not None and sha256 != sha:
                raise ValueError("supplied bytes do not hash to the sha256 given")
        elif sha256 is None:
            raise ValueError("supply needs the body or the stored row's sha256")
        else:
            sha = sha256
        self.supplied[sha] = Supplied(
            url, sha, body, content_type, fetched_at, dict(meta or {})
        )
        return sha

    async def after_write(self, session: AsyncSession, placed: list[Placed]) -> None:
        """Called by the runner once it wrote the run's evidence and
        observations, before its commit, inside a savepoint of its own: what
        a collector records about where its findings landed commits with them
        or not at all. A collector that needs nothing leaves this alone."""
        return None
