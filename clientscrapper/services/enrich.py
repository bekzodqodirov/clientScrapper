"""Enrichment: the chain E0–E6 over a candidate in ``enriching`` (04 §3).

``enrich_job`` (the worker's ``enrich`` slot: every 10 minutes 07:00–22:00,
≤ ``ENRICH_LIMIT`` candidates) walks three queues, cheapest first:

1. **warm re-asks** (``reask_handoffs``, T-21 as built «a warm re-ask is
   T-24's»): the phone-less handoff card (``score_breakdown.gates.no_phone``)
   whose owner card expired holds its candidate 7 days, and ``route`` has no
   way back for a warm hold with no attempts left — here it goes ``hold →
   scored → review`` again when its hold ran out, it still scores ≥ 60, has
   no usable phone and has a contact's Telegram username, and the owner never
   answered it ``handoff`` (the manager owns it then). The third silence
   holds it with no timer (``reviews.expire_due``), so it is asked at most
   twice again (02 §2.9);
2. **``/stir`` re-runs**: the owner's ``/stir`` sets ``next_enrich_at = now``
   on a candidate that is usually in ``review`` or ``hold`` (06 §1.2 «E4
   re-runs»); an open candidate outside ``enriching`` with ``next_enrich_at
   ≤ now`` gets step E4 alone, its state left to ``score_job`` (the signals
   mark it stale), and ``next_enrich_at`` cleared. The clock alone is no
   ``/stir`` — a warm hold keeps its enrichment clock (``candidates``): the
   re-run is the candidate whose owner ``/stir`` evidence
   (``owner:/stir:<company_id>``) was written at that very ``next_enrich_at``
   (``_stir_asked``);
3. **attempts**: ``state='enriching' AND next_enrich_at ≤ now``, oldest due
   first.

**One attempt** walks E0 → E6 and stops at the first stop condition (04
§3): **S1** a usable phone and a score ≥ 60 (the walk ``enriching → scored →
review``, through the gates, ``candidates.apply_score``); **S2** the
company became excluded or suppressed, or a gate would exclude or suppress
it now; **S3** the company's paid spend (``SUM(usage_log.cost_usd)`` by
``company_id``) would pass ``COMPANY_CAP`` ($0.10) — the paid steps are
skipped (``company_cap``), the free ones run on; **S4** after the third
attempt still no usable phone: ``score ≥ 60`` and a contact's Telegram
username → ``review`` with ``tier='warm'`` and ``gates.no_phone=true``
(``candidates.phone_less_review``: the one producer of the phone-less card,
02 §2.9), otherwise ``hold`` with no timer (a new signal re-opens it). The
attempts are 1 h, 24 h and 7 days apart (``SCHEDULE``). An attempt whose
step a budget refused — the day's pages, the day's registry lookups, a
``costs.ask`` cap — is not counted and is tried again tomorrow at 07:00 (04
§8 «next_enrich_at moves to tomorrow 07:00»), keeping what its free steps
found. Every step leaves ``{step, at, cost_usd, result, attempt}`` (and what
it found) in ``score_breakdown.enrichment[]``.

The steps:

* **E0** — the rules over the company's own evidence again
  (``signals.from_rules``, only drafts not stored yet), after
  ``companies.sectors`` is filled from its self-descriptions' bulky
  categories and its classifications' ``goods_category`` (T-24 carried:
  nothing else writes ``sectors``, so rule 4's «companies.sectors ∩ BULKY»
  could not fire);
* **E1** — the cross-source join: the lineage's sources, recorded (the
  resolver and the scorer did the joining);
* **E2** — the company's website (landing page + one about/contacts page,
  ``SITE_PAGES``) and its link hubs (a ``wa.me`` link is read off the link,
  nothing fetched), not fetched in 30 days; ``js_only`` for an empty shell;
* **E3** — its Telegram channel's public page ``https://t.me/s/<username>``
  (``companies.telegram_channel`` or a company-tier contact's username)
  through the Fetcher — never Telethon. A page that is no channel preview (a
  private channel, a user account) is not read and the name is queued for
  the reader (``chats.enqueue_username`` at priority 3; ``userbot/about.py``
  drains it). A person's username is never fetched (04 §9.1: «E3 not
  applicable (user)»);
* **E4/E4′** — the registry by STIR, else by name (``services/registry``),
  ≤ ``registry.LOOKUPS_PER_DAY`` a day, cached 90 days by the card page the
  evidence holds; a hit writes ``ent_legal_confirmed`` — the only writer of
  it — and the registry's columns;
* **E5** — directory/maps finders (T-25's ``SerpClient``, T-33's Places):
  each call through ``costs.ask`` with the company named; none is configured
  until those tasks build one (``not_configured``);
* **E6** — the Instagram Business Discovery reader (T-26), free; none
  configured yet.

Every fetched page is evidence (``enrich_site`` for E2/E3, ``registry_lookup``
for E4, the finder's own source for E5/E6) and its observation is resolved
into the candidate's company (``companies.resolve_into``: the pages were
reached through the company's own keys), then the resolver's hooks write the
rule signals and mark the candidate stale. A step never holds a row lock
across a fetch: each application is its own short transaction, ``companies``
before ``lead_candidates`` (04 §0). No step makes a model call (E7, the
paid ``enrich_search``, is T-40's), so nothing here reaches the model; what
the pages say is pseudonymised by whoever later asks the model about it.

The run's pages are counted in a ``collector_runs`` row per source
(``enrich_site``, ``registry_lookup``) written when the run ends, which is
what the next run's page budget reads (``runner.build_budget``).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors import company_site, telegram_page
from clientscrapper.collectors.base import Budget
from clientscrapper.collectors.base import Observation as Seen
from clientscrapper.collectors.keywords import BULKY_CATEGORIES, bulky_categories
from clientscrapper.collectors.runner import RunLedger, build_budget
from clientscrapper.config import settings
from clientscrapper.db.models import (
    CollectorRun,
    Company,
    Contact,
    Evidence,
    LeadCandidate,
    LeadReview,
    ModelJob,
    Observation,
    Signal,
    Source,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import (
    candidates,
    chats,
    companies,
    costs,
    gates,
    owner,
    registry,
    scoring,
    signals,
)
from clientscrapper.services import evidence as evidence_store
from clientscrapper.services.http import (
    BudgetExhausted,
    Fetcher,
    FetchResult,
    HostBlocked,
    HostPaused,
    PageBudget,
    RobotsDisallowed,
)
from clientscrapper.services.keys import domain_key
from clientscrapper.services.normalise import canonical_city, to_observation_json
from clientscrapper.services.urlsafe import strip_secrets
from clientscrapper.services.usage import Operation, Priced

log = logging.getLogger(__name__)

SITE_SOURCE: Final = company_site.SITE_SOURCE
REGISTRY_SOURCE: Final = registry.SOURCE_KEY
COLLECTOR_VERSION: Final = "enrich@1"
ENRICH_LIMIT: Final = 30
# 04 §3: next_enrich_at = now + {1 h, 24 h, 7 d}: before the first attempt
# (candidates.FIRST_ENRICH_DELAY), after the first, after the second.
SCHEDULE: Final = (timedelta(hours=1), timedelta(hours=24), timedelta(days=7))
MAX_ATTEMPTS: Final = candidates.MAX_ENRICH_ATTEMPTS
COMPANY_CAP: Final = Decimal("0.10")
REFETCH_AFTER: Final = timedelta(days=30)
SITE_PAGES: Final = 2
CHANNEL_PAGES: Final = 1
# A run's wall clock (the slot runs every 10 minutes, max_instances=1).
RUN_DEADLINE: Final = timedelta(minutes=9)
# While an attempt runs its candidate is not due again (a crashed run's
# candidate comes back after this).
LEASE: Final = timedelta(minutes=30)
# A refused budget retries tomorrow at this hour (04 §8).
RETRY_HOUR: Final = 7
E0_OBSERVATIONS: Final = 200
SPEND_SINCE: Final = datetime(2000, 1, 1, tzinfo=settings.tz)
STEPS: Final = ("E0", "E1", "E2", "E3", "E4", "E5", "E6")
SELF_KINDS: Final = signals.SELF_DESCRIPTION_KINDS
CHANNEL_SOURCES: Final = (SITE_SOURCE, chats.SOURCE_KEY)

SessionFactory = Callable[[], AsyncSession]


# --- the pluggable steps (E5, E6) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompanyQuery:
    """What a finder is told about the company (no person, no phone)."""

    company_id: uuid.UUID
    name: str
    name_key: str
    city: str | None
    instagram: str | None = None


@dataclass(frozen=True, slots=True)
class Page:
    """One page a finder or a reader fetched, with what it says: stored as
    evidence under ``source_key``, each observation resolved into the
    company when its name (and city) agree."""

    url: str
    body: bytes
    content_type: str
    fetched_at: datetime
    observations: tuple[Seen, ...] = ()
    source_key: str = SITE_SOURCE
    status: int = 200


class PaidFinder(Protocol):
    """E5 — a directory/maps lookup by name and city (04 §3: ``serp_brave``
    2 queries, Places text + details). T-25 and T-33 build them; each call
    goes through ``costs.ask`` with the company named (S3)."""

    key: str
    operation: Operation

    def estimate(self, query: CompanyQuery) -> Decimal: ...

    async def find(
        self, query: CompanyQuery, *, fetcher: Fetcher, budget: PageBudget
    ) -> list[Page]: ...

    def price(self, pages: list[Page]) -> Priced: ...


class ProfileReader(Protocol):
    """E6 — Instagram Business Discovery (T-26); free. None = the account
    is not a business account (``not_business``)."""

    key: str

    async def read(
        self, handle: str, *, fetcher: Fetcher, budget: PageBudget
    ) -> list[Page] | None: ...


@dataclass(slots=True)
class Tools:
    """What a run fetches with. ``fetcher`` is the worker's shared one
    (one breaker, one robots cache)."""

    fetcher: Fetcher
    hosts: Sequence[registry.RegistryHost] = registry.HOSTS
    finders: Sequence[PaidFinder] = ()
    profiles: ProfileReader | None = None
    evidence_root: Path | None = None


# --- the trail ------------------------------------------------------------------------


def entry(
    step: str,
    result: str,
    *,
    now: datetime,
    attempt: int | None,
    cost_usd: Decimal = Decimal("0"),
    found: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One ``score_breakdown.enrichment[]`` item (04 §3 ``{step, at,
    cost_usd, result}``)."""
    item: dict[str, Any] = {
        "step": step,
        "at": now.isoformat(),
        "cost_usd": float(cost_usd),
        "result": result,
        "attempt": attempt,
    }
    if found:
        item["found"] = dict(found)
    return item


def tomorrow_morning(now: datetime) -> datetime:
    """07:00 Tashkent the next business day (04 §8)."""
    return costs.day_start(now) + timedelta(days=1, hours=RETRY_HOUR)


def next_attempt_at(attempts_done: int, now: datetime) -> datetime:
    """When the next attempt is due after ``attempts_done`` (1 or 2)."""
    return now + SCHEDULE[min(attempts_done, len(SCHEDULE) - 1)]


# --- the run's state --------------------------------------------------------------------


@dataclass(slots=True)
class SourceRun:
    """One source's part of a run: its page budget, the Fetcher view whose
    counters become its ``collector_runs`` row, and what it may do."""

    key: str
    budget: Budget
    fetcher: Fetcher
    usable: bool
    why_not: str | None = None
    new_evidence: int = 0
    new_observations: int = 0
    lookups: int = 0
    lookups_left: int = 0
    config: dict[str, Any] = field(default_factory=dict)
    rest: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class EnrichReport:
    reasked: int = 0
    reruns: int = 0
    attempts: int = 0
    deferred: int = 0
    failed: int = 0
    moved: dict[str, int] = field(default_factory=dict)
    stopped: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "reasked": self.reasked,
            "reruns": self.reruns,
            "attempts": self.attempts,
            "deferred": self.deferred,
            "failed": self.failed,
        }
        out.update({f"to_{s}": n for s, n in sorted(self.moved.items())})
        if self.stopped:
            out["stopped"] = self.stopped
        return out


@dataclass(slots=True)
class Attempt:
    """One candidate's walk: the trail, and what the finish needs to know."""

    candidate_id: uuid.UUID
    company_id: uuid.UUID
    mode: str  # attempt | stir
    attempt: int | None
    steps: tuple[str, ...]
    entries: list[dict[str, Any]] = field(default_factory=list)
    deferred: bool = False
    stopped: str | None = None
    planned_state: str = "enriching"


class _Stop(Exception):
    """The run's wall clock ran out."""


# --- reading the company ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Facts:
    """What the chain reads of the company before it fetches anything."""

    company_id: uuid.UUID
    lineage: tuple[uuid.UUID, ...]
    display_name: str
    name_key: str
    city: str | None
    stir: str | None
    website_domain: str | None
    telegram_channel: str | None
    instagram: str | None
    state: str
    company_usernames: tuple[str, ...]
    hub_links: tuple[str, ...]
    spent_usd: Decimal


async def read_facts(session: AsyncSession, company: Company) -> Facts:
    lineage = sorted(await companies.company_lineage(session, company.id))
    usernames = [
        u
        for u in await session.scalars(
            sa.select(Contact.telegram_username)
            .where(
                Contact.company_id.in_(lineage),
                Contact.tier == "company",
                Contact.anonymised_at.is_(None),
                Contact.telegram_username.is_not(None),
            )
            .order_by(Contact.created_at, Contact.id)
        )
        if u
    ]
    links: list[str] = []
    for site in await session.scalars(
        sa.select(Observation.parsed["company"]["website"].astext).where(
            Observation.company_id.in_(lineage), Observation.status == "resolved"
        )
    ):
        if site and (company_site.is_link_hub(site) or company_site.whatsapp_phone(site)):
            links.append(site.strip())
    spent = await costs.spend(session, since=SPEND_SINCE, company_id=company.id)
    instagram = company.instagram or await session.scalar(
        sa.select(Contact.instagram)
        .where(
            Contact.company_id.in_(lineage),
            Contact.tier == "company",
            Contact.anonymised_at.is_(None),
            Contact.instagram.is_not(None),
        )
        .limit(1)
    )
    return Facts(
        company_id=company.id,
        lineage=tuple(lineage),
        display_name=company.display_name,
        name_key=company.name_key,
        city=company.city,
        stir=company.stir.strip() if company.stir else None,
        website_domain=company.website_domain,
        telegram_channel=company.telegram_channel,
        instagram=instagram,
        state=company.state,
        company_usernames=tuple(dict.fromkeys(usernames)),
        hub_links=tuple(dict.fromkeys(links)),
        spent_usd=spent.usd,
    )


async def fetched_recently(
    session: AsyncSession, url: str, *, sources: Sequence[str], now: datetime
) -> bool:
    """Whether ``url`` is evidence of one of ``sources`` younger than 30 days
    (04 §3: «not fetched in 30 d»)."""
    found = await session.scalar(
        sa.select(Evidence.id)
        .where(
            Evidence.url == strip_secrets(url),
            Evidence.source_key.in_(list(sources)),
            Evidence.fetched_at > now - REFETCH_AFTER,
        )
        .limit(1)
    )
    return found is not None


async def registry_card(
    session: AsyncSession, lineage: Sequence[uuid.UUID]
) -> tuple[str, datetime] | None:
    """The newest registry company page resolved into the lineage — the 90
    day cache of 04 §3 is the evidence row itself (02's ``company_ids`` has
    no ``id_type`` for orginfo's id, and ``myorg`` is another host's)."""
    row = (
        await session.execute(
            sa.select(Evidence.url, Evidence.fetched_at)
            .join(Observation, Observation.evidence_id == Evidence.id)
            .where(
                Evidence.source_key == REGISTRY_SOURCE,
                Observation.company_id.in_(list(lineage)),
                Observation.status == "resolved",
            )
            .order_by(Evidence.fetched_at.desc(), Evidence.id.desc())
            .limit(1)
        )
    ).first()
    if row is None or not registry.is_card_url(row.url):
        return None
    return str(row.url), row.fetched_at


# --- sectors (T-24 carried: companies.sectors) -----------------------------------------


def sectors_of_text(text: str) -> frozenset[str]:
    """The BULKY_CATEGORIES keys a self-description names."""
    return bulky_categories(text)


def merged_sectors(held: Sequence[str], found: frozenset[str]) -> list[str]:
    """``companies.sectors`` with ``found`` added: BULKY_CATEGORIES keys
    only, sorted."""
    keys = {s for s in held if s in BULKY_CATEGORIES} | {
        s for s in found if s in BULKY_CATEGORIES
    }
    return sorted(keys)


def _own_text(parsed: Mapping[str, Any]) -> str:
    company = parsed.get("company") if isinstance(parsed.get("company"), Mapping) else {}
    text = parsed.get("text") if isinstance(parsed.get("text"), Mapping) else {}
    assert isinstance(company, Mapping) and isinstance(text, Mapping)
    parts = [company.get("name_raw"), company.get("goods_text"), text.get("excerpt")]
    return "\n".join(p for p in parts if isinstance(p, str) and p)


async def company_sectors(session: AsyncSession, company: Company) -> list[str]:
    """What ``sectors`` should hold: the bulky categories the lineage's
    self-descriptions name, and its classifications' ``goods_category``
    (a tout's never)."""
    lineage = sorted(await companies.company_lineage(session, company.id))
    found: set[str] = set()
    for kind, parsed in (
        await session.execute(
            sa.select(Observation.parsed["kind"].astext, Observation.parsed).where(
                Observation.company_id.in_(lineage), Observation.status == "resolved"
            )
        )
    ).all():
        if kind in SELF_KINDS and isinstance(parsed, Mapping):
            found |= sectors_of_text(_own_text(parsed))
    for result in await session.scalars(
        sa.select(ModelJob.result).where(
            ModelJob.company_id.in_(lineage),
            ModelJob.kind == gates.CLASSIFY_KIND,
            ModelJob.status.in_(gates.CLASSIFY_DONE),
        )
    ):
        if (
            isinstance(result, Mapping)
            and result.get("speaker_role") != "tout"
            and isinstance(result.get("goods_category"), str)
        ):
            found.add(result["goods_category"])
    return merged_sectors(list(company.sectors or ()), frozenset(found))


# --- applying a page -------------------------------------------------------------------


@dataclass(slots=True)
class Applied:
    evidence_id: int | None = None
    observations: int = 0
    new: bool = False
    rejected: str | None = None
    found: dict[str, Any] = field(default_factory=dict)


def _fetch_meta(result: FetchResult) -> dict[str, Any]:
    meta: dict[str, Any] = {"status": result.status, "final_url": result.final_url}
    if result.etag:
        meta["etag"] = result.etag
    if result.last_modified:
        meta["last_modified"] = result.last_modified
    if result.truncated:
        meta["truncated"] = True
    return meta


async def _locked_company(session: AsyncSession, company_id: uuid.UUID) -> Company | None:
    company = await session.get(
        Company, company_id, with_for_update={"key_share": True}, populate_existing=True
    )
    if company is None:
        return None
    root = await companies.root_company(session, company)
    if root.id != company.id:
        root = (
            await session.get(
                Company,
                root.id,
                with_for_update={"key_share": True},
                populate_existing=True,
            )
            or root
        )
    return root


async def apply_page(
    session_factory: SessionFactory,
    company_id: uuid.UUID,
    *,
    source_key: str,
    url: str,
    body: bytes,
    content_type: str,
    fetched_at: datetime,
    observations: Sequence[Seen],
    meta: Mapping[str, Any] | None,
    now: datetime,
    root: Path | None,
    registry_record: registry.RegistryRecord | None = None,
    source: SourceRun | None = None,
) -> Applied:
    """Store one page as evidence and resolve its observations into the
    company, in one transaction that holds the company first (04 §0); the
    resolver's hooks write the rule signals and the stale mark. A registry
    hit also writes its own keys and columns (``registry.hit_drafts``,
    ``company_values``)."""
    out = Applied()
    async with session_factory() as session, session.begin():
        company = await _locked_company(session, company_id)
        if company is None or company.state in ("merged", "purged"):
            out.rejected = "company_gone"
            return out
        first = observations[0] if observations else None
        stored_url = strip_secrets(url)
        known = await evidence_store.find(
            session, stored_url, hashlib.sha256(body).hexdigest()
        )
        row = await evidence_store.store(
            session,
            source_key=source_key,
            url=stored_url,
            body=body,
            content_type=content_type,
            fetched_at=fetched_at,
            source_ref=first.source_ref if first is not None else None,
            excerpt=(first.excerpt or None) if first is not None else None,
            lang=first.lang if first is not None else None,
            published_at=first.published_at if first is not None else None,
            meta=dict(meta or {}),
            root=root,
        )
        out.evidence_id = row.id
        out.new = known is None
        if source is not None and out.new:
            source.new_evidence += 1
        held = set(
            await session.scalars(
                sa.select(Observation.ordinal).where(Observation.evidence_id == row.id)
            )
        )
        weights = await scoring.load_weights(session)
        for ordinal, seen in enumerate(observations):
            if ordinal in held:
                continue
            parsed = to_observation_json(seen, collector_version=COLLECTOR_VERSION)
            if registry_record is not None:
                parsed = registry.phones_to_person(parsed)
            kind = parsed.get("kind")
            if kind in SELF_KINDS:
                sectors = merged_sectors(
                    list(company.sectors or ()), sectors_of_text(_own_text(parsed))
                )
                if sectors != list(company.sectors or ()):
                    company.sectors = sectors
                    await session.flush()
            obs = Observation(
                evidence_id=row.id,
                ordinal=ordinal,
                source_key=seen.source,
                status="new",
                parsed=parsed,
            )
            session.add(obs)
            await session.flush()
            try:
                async with session.begin_nested():
                    resolution = await companies.resolve_into(
                        session, obs, company, now=now
                    )
                    for hook in companies.AFTER_RESOLVE:
                        await hook(session, obs, resolution, now)
            except companies.ObservationRejected as refused:
                obs.status, obs.reject_reason, obs.resolved_at = (
                    "rejected",
                    refused.reason,
                    now,
                )
                out.rejected = refused.reason
                await session.flush()
                continue
            contact = resolution.contact
            obs.status = "resolved"
            obs.company_id = company.id
            obs.contact_id = contact.id if contact is not None else None
            obs.resolved_at = now
            await session.flush()
            out.observations += 1
            if source is not None:
                source.new_observations += 1
            if registry_record is not None:
                ref = signals.EvidenceRef(
                    id=row.id,
                    source_key=REGISTRY_SOURCE,
                    fetched_at=fetched_at,
                    observation_id=obs.id,
                )
                await signals.emit(
                    session,
                    company.id,
                    registry.hit_drafts(registry_record, ref, now=now),
                    weights=weights,
                )
                values = registry.company_values(
                    registry_record,
                    registry.CompanyNow(
                        registry_status=company.registry_status,
                        registered_on=company.registered_on,
                        oked=company.oked,
                        legal_form=company.legal_form,
                        address=company.address,
                        city=company.city,
                    ),
                )
                for column, value in values.items():
                    setattr(company, column, value)
                await session.flush()
                out.found["registry"] = registry_record.status
            phones = [
                p["e164"] for p in parsed.get("phones") or () if isinstance(p, dict)
            ]
            if phones:
                out.found["phones"] = out.found.get("phones", 0) + len(phones)
            handles = parsed.get("handles") or {}
            if isinstance(handles, Mapping):
                for name in ("telegram", "instagram"):
                    if handles.get(name):
                        out.found[name] = True
    return out


# --- the stop checks ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Standing:
    total: int
    usable_phone: bool
    stop: str | None  # S1 | S2 | None


async def standing(
    session_factory: SessionFactory, candidate_id: uuid.UUID, *, now: datetime
) -> Standing:
    """The live score and the gates now, read without a lock: S1 (a usable
    phone and ≥ 60) or S2 (the company excluded or suppressed, or a gate
    that would exclude or suppress it)."""
    async with session_factory() as session:
        candidate = await session.get(LeadCandidate, candidate_id)
        if candidate is None:
            return Standing(0, False, "S2")
        company = await session.get(Company, candidate.company_id)
        if company is None or company.state in (
            "excluded",
            "suppressed",
            "merged",
            "purged",
        ):
            return Standing(0, False, "S2")
        result = await candidates.live_score(session, candidate, now=now)
        if result is None:
            return Standing(0, False, "S2")
        failure = (await gates.evaluate(session, candidate.company_id, now=now)).decide(
            card=False
        )
        if failure is not None and failure.decision in ("excluded", "suppressed"):
            return Standing(result.total, result.usable_phone, "S2")
        if result.usable_phone and result.total >= scoring.HOT_MIN:
            return Standing(result.total, result.usable_phone, "S1")
        return Standing(result.total, result.usable_phone, None)


# --- the steps ----------------------------------------------------------------------


@dataclass(slots=True)
class Context:
    session_factory: SessionFactory
    tools: Tools
    site: SourceRun
    registry: SourceRun
    now: datetime
    deadline: datetime
    clock: Callable[[], datetime]


def _clock_check(ctx: Context) -> None:
    if ctx.clock() >= ctx.deadline:
        raise _Stop("deadline")


async def _get(
    fetcher: Fetcher, url: str, *, source: str, budget: PageBudget
) -> FetchResult | str:
    """A page, or the word for why there is none."""
    try:
        return await fetcher.get(url, source=source, budget=budget)
    except HostBlocked:
        return "blocked"
    except HostPaused:
        return "paused"
    except RobotsDisallowed:
        return "robots"
    except BudgetExhausted as exc:
        if exc.reason == "deadline":
            raise _Stop("deadline") from exc
        return f"budget:{exc.reason}"
    except Exception as exc:  # a transport failure costs this page only
        return f"error:{type(exc).__name__}"


async def step_e0(ctx: Context, run: Attempt) -> dict[str, Any]:
    """E0: sectors, then the rules over the company's own evidence."""
    found: dict[str, Any] = {}
    async with ctx.session_factory() as session, session.begin():
        company = await _locked_company(session, run.company_id)
        if company is None:
            return entry("E0", "skipped:company_gone", now=ctx.now, attempt=run.attempt)
        sectors = await company_sectors(session, company)
        if sectors != list(company.sectors or ()):
            company.sectors = sectors
            await session.flush()
            found["sectors"] = sectors
        lineage = sorted(await companies.company_lineage(session, company.id))
        facts = await signals.company_facts(session, company)
        held = {
            (str(k), int(e))
            for k, e in (
                await session.execute(
                    sa.select(Signal.key, Signal.evidence_id).where(
                        Signal.company_id == company.id
                    )
                )
            ).all()
        }
        drafts: list[signals.SignalDraft] = []
        for obs in await session.scalars(
            sa.select(Observation)
            .where(Observation.company_id.in_(lineage), Observation.status == "resolved")
            .order_by(Observation.id.desc())
            .limit(E0_OBSERVATIONS)
        ):
            ref = signals.EvidenceRef(
                id=obs.evidence_id,
                source_key=obs.source_key,
                fetched_at=obs.created_at or ctx.now,
                observation_id=obs.id,
                contact_id=obs.contact_id,
            )
            drafts += [
                d
                for d in signals.from_rules(ref, obs.parsed, facts)
                if (d.key, d.evidence_id) not in held
            ]
        if drafts:
            weights = await scoring.load_weights(session)
            await signals.emit(session, company.id, drafts, weights=weights)
        found["signals"] = len({(d.key, d.evidence_id) for d in drafts})
    return entry("E0", "ok", now=ctx.now, attempt=run.attempt, found=found)


async def step_e1(ctx: Context, run: Attempt) -> dict[str, Any]:
    """E1: the cross-source join, as the resolver and the scorer made it."""
    async with ctx.session_factory() as session:
        lineage = sorted(await companies.company_lineage(session, run.company_id))
        rows = (
            await session.execute(
                sa.select(Source.family, sa.func.count())
                .join(Observation, Observation.source_key == Source.key)
                .where(
                    Observation.company_id.in_(lineage),
                    Observation.status == "resolved",
                )
                .group_by(Source.family)
                .order_by(Source.family)
            )
        ).all()
    families = {str(f): int(n) for f, n in rows}
    return entry(
        "E1",
        "ok",
        now=ctx.now,
        attempt=run.attempt,
        found={"families": families, "lineage": len(lineage)},
    )


async def _site_page(
    ctx: Context, run: Attempt, url: str, *, hub: bool
) -> tuple[str, dict[str, Any], FetchResult | None]:
    got = await _get(ctx.site.fetcher, url, source=SITE_SOURCE, budget=ctx.site.budget)
    if isinstance(got, str):
        return got, {}, None
    if got.status in (301, 302, 303, 307, 308):
        target = got.headers.get("location")
        return "redirect", {"to": domain_key(target) if target else None}, None
    if got.status != 200:
        return f"http_{got.status}", {}, None
    parse = company_site.parse_link_hub if hub else company_site.parse_company_site
    seen = parse(
        got.body,
        url=got.final_url,
        fetched_at=got.fetched_at,
        raw_sha256=got.sha256,
        content_type=got.headers.get("content-type", "text/html"),
    )
    if not seen and not hub:
        # An empty shell is evidence that the page says nothing without JS.
        await apply_page(
            ctx.session_factory,
            run.company_id,
            source_key=SITE_SOURCE,
            url=got.final_url,
            body=got.body,
            content_type=got.content_type or "text/html",
            fetched_at=got.fetched_at,
            observations=(),
            meta=_fetch_meta(got),
            now=ctx.now,
            root=ctx.tools.evidence_root,
            source=ctx.site,
        )
        return "js_only", {}, got
    applied = await apply_page(
        ctx.session_factory,
        run.company_id,
        source_key=SITE_SOURCE,
        url=got.final_url,
        body=got.body,
        content_type=got.content_type or "text/html",
        fetched_at=got.fetched_at,
        observations=seen,
        meta=_fetch_meta(got),
        now=ctx.now,
        root=ctx.tools.evidence_root,
        source=ctx.site,
    )
    return ("ok" if seen else "empty"), applied.found, got


async def step_e2(ctx: Context, run: Attempt, facts: Facts) -> list[dict[str, Any]]:
    """E2: the website (landing + one about page) and the link hubs."""
    out: list[dict[str, Any]] = []
    if not ctx.site.usable:
        return [
            entry("E2", f"skipped:{ctx.site.why_not}", now=ctx.now, attempt=run.attempt)
        ]
    targets: list[tuple[str, bool]] = []
    if facts.website_domain:
        targets.append((f"https://{facts.website_domain}/", False))
    for link in facts.hub_links:
        if company_site.whatsapp_phone(link):
            seen = company_site.whatsapp_observation(link, fetched_at=ctx.now)
            applied = await apply_page(
                ctx.session_factory,
                run.company_id,
                source_key=SITE_SOURCE,
                url=link,
                body=link.encode(),
                content_type="text/plain",
                fetched_at=ctx.now,
                observations=seen,
                meta={"kind": "link", "fetched": False},
                now=ctx.now,
                root=ctx.tools.evidence_root,
                source=ctx.site,
            )
            out.append(
                entry("E2", "link", now=ctx.now, attempt=run.attempt, found=applied.found)
            )
        else:
            url = link if "://" in link else f"https://{link}"
            targets.append((url, True))
    if not targets and not out:
        return [entry("E2", "skipped:no_site", now=ctx.now, attempt=run.attempt)]
    pages_left = SITE_PAGES
    for url, hub in targets:
        _clock_check(ctx)
        async with ctx.session_factory() as session:
            fresh = await fetched_recently(
                session, url, sources=(SITE_SOURCE,), now=ctx.now
            )
        if fresh:
            out.append(entry("E2", "fresh", now=ctx.now, attempt=run.attempt))
            continue
        if pages_left <= 0:
            out.append(entry("E2", "skipped:pages", now=ctx.now, attempt=run.attempt))
            break
        result, found, got = await _site_page(ctx, run, url, hub=hub)
        pages_left -= 1
        if result.startswith("budget:"):
            run.deferred = True
        out.append(entry("E2", result, now=ctx.now, attempt=run.attempt, found=found))
        if result.startswith("budget:"):
            break
        if got is not None and not hub and result == "ok" and pages_left > 0:
            about = company_site.about_link(got.body, url=got.final_url)
            if about is not None:
                _clock_check(ctx)
                result, found, _ = await _site_page(ctx, run, about, hub=False)
                pages_left -= 1
                if result.startswith("budget:"):
                    run.deferred = True
                out.append(
                    entry("E2", result, now=ctx.now, attempt=run.attempt, found=found)
                )
        if await _stopped(ctx, run):
            break
    return out


async def step_e3(ctx: Context, run: Attempt, facts: Facts) -> dict[str, Any]:
    """E3: the channel's public page through the Fetcher, else the reader's
    queue."""
    names = [n for n in (facts.telegram_channel, *facts.company_usernames) if n]
    if not names:
        return entry("E3", "skipped:no_channel", now=ctx.now, attempt=run.attempt)
    if not ctx.site.usable:
        return entry(
            "E3", f"skipped:{ctx.site.why_not}", now=ctx.now, attempt=run.attempt
        )
    username = names[0]
    url = telegram_page.public_url(username)
    async with ctx.session_factory() as session:
        if await fetched_recently(session, url, sources=CHANNEL_SOURCES, now=ctx.now):
            return entry("E3", "fresh", now=ctx.now, attempt=run.attempt)
    _clock_check(ctx)
    got = await _get(ctx.site.fetcher, url, source=SITE_SOURCE, budget=ctx.site.budget)
    if isinstance(got, str) and got.startswith("budget:"):
        run.deferred = True
        return entry("E3", got, now=ctx.now, attempt=run.attempt)
    page = (
        telegram_page.parse_channel_page(got.body, url=got.final_url)
        if isinstance(got, FetchResult) and got.status == 200
        else None
    )
    if page is None:
        # A private channel, a user account, a name nobody holds: nothing is
        # kept of the page (a person's is person-tier); the reader may ask.
        async with ctx.session_factory() as session, session.begin():
            queued = await chats.enqueue_username(
                session,
                username,
                priority=chats.PRIORITY_RESOLVED,
                now=ctx.now,
                by=f"enrich:{run.candidate_id}",
            )
        why = got if isinstance(got, str) else f"http_{got.status}"
        result = "queued" if queued else "not_channel"
        return entry("E3", result, now=ctx.now, attempt=run.attempt, found={"page": why})
    assert isinstance(got, FetchResult)
    seen = telegram_page.channel_observation(
        page, url=url, fetched_at=got.fetched_at, raw_sha256=got.sha256
    )
    applied = await apply_page(
        ctx.session_factory,
        run.company_id,
        source_key=SITE_SOURCE,
        url=url,
        body=got.body,
        content_type=got.content_type or "text/html",
        fetched_at=got.fetched_at,
        observations=(seen,),
        meta=_fetch_meta(got),
        now=ctx.now,
        root=ctx.tools.evidence_root,
        source=ctx.site,
    )
    return entry("E3", "ok", now=ctx.now, attempt=run.attempt, found=applied.found)


async def _rest_hosts(ctx: Context, keys: Sequence[str]) -> None:
    if not keys:
        return
    until = (ctx.now + registry.HOST_PAUSE).isoformat()
    for key in keys:
        ctx.registry.rest[key] = until
    async with ctx.session_factory() as session, session.begin():
        source = await session.get(Source, REGISTRY_SOURCE, with_for_update=True)
        if source is None:
            return
        config = dict(source.config or {})
        paused = dict(config.get("paused_hosts") or {})
        paused.update(dict.fromkeys(keys, until))
        config["paused_hosts"] = paused
        source.config = config


async def step_e4(ctx: Context, run: Attempt, facts: Facts) -> dict[str, Any]:
    """E4 by STIR, else E4′ by name (``services/registry``)."""
    if not ctx.registry.usable:
        return entry(
            "E4", f"skipped:{ctx.registry.why_not}", now=ctx.now, attempt=run.attempt
        )
    name_ok = companies.usable_name(facts.name_key)
    if facts.stir is None and not name_ok:
        return entry("E4", "skipped:no_key", now=ctx.now, attempt=run.attempt)
    async with ctx.session_factory() as session:
        cached = await registry_card(session, facts.lineage)
    refresh: str | None = None
    if cached is not None:
        url, fetched = cached
        if ctx.now - fetched < timedelta(days=registry.CACHE_DAYS):
            return entry("E4", "cached", now=ctx.now, attempt=run.attempt)
        refresh = url
    if ctx.registry.lookups_left <= 0:
        run.deferred = True
        return entry("E4", "budget:lookups", now=ctx.now, attempt=run.attempt)
    hosts = registry.hosts_on(ctx.registry.config, ctx.tools.hosts)
    paused = registry.paused_hosts(ctx.registry.config, now=ctx.now) | frozenset(
        ctx.registry.rest
    )
    _clock_check(ctx)
    ctx.registry.lookups += 1
    ctx.registry.lookups_left -= 1
    try:
        answer = await registry.lookup(
            hosts,
            stir=facts.stir,
            name=None if facts.stir else facts.display_name,
            city=facts.city,
            fetcher=ctx.registry.fetcher,
            budget=ctx.registry.budget,
            paused=paused,
            refresh_url=refresh,
        )
    except BudgetExhausted as exc:
        if exc.reason == "deadline":
            raise _Stop("deadline") from exc
        run.deferred = True
        return entry("E4", f"budget:{exc.reason}", now=ctx.now, attempt=run.attempt)
    await _rest_hosts(ctx, answer.rest)
    found: dict[str, Any] = {"host": answer.host} if answer.host else {}
    card = answer.record.url if answer.record is not None else None
    for page in answer.pages:
        hit = answer.record if page.final_url == card or page.url == card else None
        seen = (
            (
                registry.observation_of(
                    hit, fetched_at=page.fetched_at, sha256=page.sha256
                ),
            )
            if hit is not None
            else ()
        )
        applied = await apply_page(
            ctx.session_factory,
            run.company_id,
            source_key=REGISTRY_SOURCE,
            url=page.final_url,
            body=page.body,
            content_type=page.content_type or "text/html",
            fetched_at=page.fetched_at,
            observations=seen,
            meta={**_fetch_meta(page), "registry_host": answer.host},
            now=ctx.now,
            root=ctx.tools.evidence_root,
            registry_record=hit,
            source=ctx.registry,
        )
        found.update(applied.found)
    result = answer.result
    if result == registry.UNAVAILABLE:
        found["tried"] = [f"{k}:{r}" for k, r in answer.tried]
    return entry("E4", result, now=ctx.now, attempt=run.attempt, found=found)


def _agrees(seen: Seen, facts: Facts) -> bool:
    """A finder's card is the company's when its name agrees (and the city,
    when both say one) — the registry's E4′ rule."""
    if not seen.company_name_raw:
        return False
    city = canonical_city(seen.city or seen.address)
    return registry.name_agrees(
        seen.company_name_raw,
        facts.display_name,
        card_city=city,
        city=facts.city if city is not None else None,
    )


async def step_e5(ctx: Context, run: Attempt, facts: Facts) -> list[dict[str, Any]]:
    """E5: each configured finder, every call through ``costs.ask``."""
    if not ctx.tools.finders:
        return [entry("E5", "not_configured", now=ctx.now, attempt=run.attempt)]
    if not companies.usable_name(facts.name_key):
        return [entry("E5", "skipped:no_name", now=ctx.now, attempt=run.attempt)]
    query = CompanyQuery(
        company_id=facts.company_id,
        name=facts.display_name,
        name_key=facts.name_key,
        city=facts.city,
        instagram=facts.instagram,
    )
    out: list[dict[str, Any]] = []
    for finder in ctx.tools.finders:
        _clock_check(ctx)
        estimate = finder.estimate(query)
        async with ctx.session_factory() as session:
            spent = (
                await costs.spend(session, since=SPEND_SINCE, company_id=facts.company_id)
            ).usd
        if spent + estimate > COMPANY_CAP:
            out.append(
                entry(
                    "E5",
                    "company_cap",
                    now=ctx.now,
                    attempt=run.attempt,
                    found={"finder": finder.key, "spent_usd": float(spent)},
                )
            )
            continue
        try:
            async with ctx.session_factory() as session, session.begin():
                pages = await costs.ask(
                    session,
                    operation=finder.operation,
                    est_usd=estimate,
                    call=partial(
                        finder.find,
                        query,
                        fetcher=ctx.site.fetcher,
                        budget=ctx.site.budget,
                    ),
                    price=finder.price,
                    now=ctx.now,
                    company_id=facts.company_id,
                )
        except costs.CostCapReached as refused:
            run.deferred = run.deferred or costs.is_cap(refused.code)
            out.append(
                entry(
                    "E5",
                    f"budget:{refused.code}",
                    now=ctx.now,
                    attempt=run.attempt,
                    found={"finder": finder.key},
                )
            )
            continue
        except BudgetExhausted as exc:
            if exc.reason == "deadline":
                raise _Stop("deadline") from exc
            run.deferred = True
            out.append(
                entry("E5", f"budget:{exc.reason}", now=ctx.now, attempt=run.attempt)
            )
            continue
        found: dict[str, Any] = {"finder": finder.key, "pages": len(pages)}
        for page in pages:
            kept = tuple(s for s in page.observations if _agrees(s, facts))
            applied = await apply_page(
                ctx.session_factory,
                run.company_id,
                source_key=page.source_key,
                url=page.url,
                body=page.body,
                content_type=page.content_type,
                fetched_at=page.fetched_at,
                observations=kept,
                meta={"status": page.status},
                now=ctx.now,
                root=ctx.tools.evidence_root,
            )
            for key, value in applied.found.items():
                found[key] = value
        out.append(
            entry(
                "E5",
                "ok",
                now=ctx.now,
                attempt=run.attempt,
                cost_usd=estimate,
                found=found,
            )
        )
        if await _stopped(ctx, run):
            break
    return out


async def step_e6(ctx: Context, run: Attempt, facts: Facts) -> dict[str, Any]:
    """E6: Instagram Business Discovery for the company's own account."""
    reader = ctx.tools.profiles
    if reader is None:
        return entry("E6", "not_configured", now=ctx.now, attempt=run.attempt)
    if not facts.instagram:
        return entry("E6", "skipped:no_instagram", now=ctx.now, attempt=run.attempt)
    _clock_check(ctx)
    try:
        pages = await reader.read(
            facts.instagram, fetcher=ctx.site.fetcher, budget=ctx.site.budget
        )
    except BudgetExhausted as exc:
        if exc.reason == "deadline":
            raise _Stop("deadline") from exc
        run.deferred = True
        return entry("E6", f"budget:{exc.reason}", now=ctx.now, attempt=run.attempt)
    if pages is None:
        return entry("E6", "not_business", now=ctx.now, attempt=run.attempt)
    found: dict[str, Any] = {}
    for page in pages:
        applied = await apply_page(
            ctx.session_factory,
            run.company_id,
            source_key=page.source_key,
            url=page.url,
            body=page.body,
            content_type=page.content_type,
            fetched_at=page.fetched_at,
            observations=page.observations,
            meta={"status": page.status},
            now=ctx.now,
            root=ctx.tools.evidence_root,
        )
        found.update(applied.found)
    return entry("E6", "ok", now=ctx.now, attempt=run.attempt, found=found)


async def _stopped(ctx: Context, run: Attempt) -> bool:
    """S1/S2 after a step that wrote something."""
    if run.stopped is not None:
        return True
    now_standing = await standing(ctx.session_factory, run.candidate_id, now=ctx.now)
    if now_standing.stop is not None:
        run.stopped = now_standing.stop
        return True
    return False


async def walk(ctx: Context, run: Attempt) -> None:
    """The chain for one candidate, stopping at S1/S2 (04 §3)."""
    async with ctx.session_factory() as session:
        company = await session.get(Company, run.company_id)
        if company is None:
            run.stopped = "S2"
            return
        company = await companies.root_company(session, company)
        run.company_id = company.id
        facts = await read_facts(session, company)
    for step in run.steps:
        _clock_check(ctx)
        if step == "E0":
            run.entries.append(await step_e0(ctx, run))
        elif step == "E1":
            run.entries.append(await step_e1(ctx, run))
            if await _stopped(ctx, run):
                return
        elif step == "E2":
            run.entries += await step_e2(ctx, run, facts)
        elif step == "E3":
            run.entries.append(await step_e3(ctx, run, facts))
        elif step == "E4":
            run.entries.append(await step_e4(ctx, run, facts))
        elif step == "E5":
            run.entries += await step_e5(ctx, run, facts)
        elif step == "E6":
            run.entries.append(await step_e6(ctx, run, facts))
        if step in ("E2", "E3", "E4", "E5", "E6") and await _stopped(ctx, run):
            return
        if step in ("E2", "E3", "E4"):
            # A page may have taught the company a site, a channel, a STIR.
            async with ctx.session_factory() as session:
                fresh = await session.get(Company, run.company_id)
                if fresh is not None:
                    facts = await read_facts(session, fresh)


# --- the finish ---------------------------------------------------------------------


def _trail(candidate: LeadCandidate, entries: Sequence[Mapping[str, Any]]) -> None:
    breakdown = dict(candidate.score_breakdown or {})
    held = breakdown.get("enrichment")
    breakdown["enrichment"] = [*(held if isinstance(held, list) else []), *entries]
    candidate.score_breakdown = breakdown


async def _writable_username(session: AsyncSession, company_id: uuid.UUID) -> bool:
    found = await session.scalar(
        sa.select(
            sa.exists().where(
                Contact.company_id == company_id,
                Contact.telegram_username.is_not(None),
                Contact.anonymised_at.is_(None),
            )
        )
    )
    return bool(found)


async def finish(
    session_factory: SessionFactory, run: Attempt, *, now: datetime
) -> tuple[str, ...]:
    """Re-score, decide, and write the trail — the company locked before the
    candidate (04 §0). Returns the states walked."""
    async with session_factory() as session, session.begin():
        company = await _locked_company(session, run.company_id)
        candidate = await session.scalar(
            sa.select(LeadCandidate)
            .where(LeadCandidate.id == run.candidate_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if company is None or candidate is None:
            return ()
        if candidate.state != run.planned_state:
            # Another writer moved it meanwhile (score_job routed it on the
            # signals this attempt wrote): the trail is kept, nothing else.
            if (
                candidate.state in candidates.RESCORE_STATES
                or candidate.state == "approved"
            ):
                _trail(candidate, run.entries)
            return ()
        if run.mode == "stir":
            # The registry's signals marked the candidate stale: score_job
            # re-scores and routes it (its state is not enrichment's).
            candidate.next_enrich_at = None
            _trail(candidate, run.entries)
            return ()
        result = await candidates.rescore(session, candidate, now=now)
        if not run.deferred:
            candidate.enrich_attempts = min(candidate.enrich_attempts + 1, MAX_ATTEMPTS)
        gate = await gates.evaluate(session, candidate.company_id, now=now)
        walked = await candidates.apply_score(
            session,
            candidate,
            result,
            now=now,
            auto_push=await candidates._auto_push(session, candidate.primary_source_key),
            gate=gate,
        )
        if candidate.state == "enriching":
            if run.deferred:
                candidate.next_enrich_at = tomorrow_morning(now)
            elif candidate.enrich_attempts < MAX_ATTEMPTS:
                candidate.next_enrich_at = next_attempt_at(candidate.enrich_attempts, now)
            else:
                walked += await _after_last(session, candidate, result, gate, now=now)
        _trail(candidate, run.entries)
        await session.flush()
        return walked


async def _after_last(
    session: AsyncSession,
    candidate: LeadCandidate,
    result: scoring.ScoreResult,
    gate: gates.GateResult,
    *,
    now: datetime,
) -> tuple[str, ...]:
    """S4 (04 §3): no usable phone after the third attempt — the phone-less
    card when it scores ≥ 60 and a contact has a Telegram username (its gates
    judged as for any card), else ``hold`` with no timer."""
    if (
        not result.usable_phone
        and result.total >= scoring.HOT_MIN
        and await _writable_username(session, candidate.company_id)
    ):
        failure = gate.decide(card=True)
        if failure is None:
            await candidates.phone_less_review(session, candidate, now=now)
            return ("review",)
        if failure.decision in ("excluded", "suppressed"):
            await candidates.transition(
                session,
                candidate,
                failure.decision,
                now=now,
                exclude_reason=failure.reason if failure.decision == "excluded" else None,
                suppressed_by=failure.hit.id if failure.hit else None,
            )
            return (failure.decision,)
        await candidates.transition(
            session, candidate, "hold", now=now, hold_until=failure.until
        )
        return ("hold",)
    await candidates.transition(session, candidate, "hold", now=now, hold_until=None)
    return ("hold",)


# --- the warm re-ask (T-21 carried) ---------------------------------------------------


async def _handed_off(session: AsyncSession, candidate_id: uuid.UUID) -> bool:
    found = await session.scalar(
        sa.select(
            sa.exists().where(
                LeadReview.candidate_id == candidate_id,
                LeadReview.kind == "owner",
                LeadReview.answer == "handoff",
            )
        )
    )
    return bool(found)


async def reask_handoffs(
    session_factory: SessionFactory, *, now: datetime, limit: int
) -> int:
    """The phone-less handoff card asked again once its hold ran out (module
    docstring, queue 1). Returns how many went back to ``review``."""
    async with session_factory() as session:
        due = list(
            await session.scalars(
                sa.select(LeadCandidate.id)
                .where(
                    LeadCandidate.state == "hold",
                    LeadCandidate.hold_until.is_not(None),
                    LeadCandidate.hold_until <= now,
                    LeadCandidate.score >= scoring.HOT_MIN,
                    LeadCandidate.score_breakdown.contains({"gates": {"no_phone": True}}),
                )
                .order_by(LeadCandidate.hold_until, LeadCandidate.id)
                .limit(limit)
            )
        )
    moved = 0
    for candidate_id in due:
        async with session_factory() as session, session.begin():
            company_id = await session.scalar(
                sa.select(LeadCandidate.company_id).where(
                    LeadCandidate.id == candidate_id
                )
            )
            if company_id is None or await _locked_company(session, company_id) is None:
                continue
            candidate = await session.scalar(
                sa.select(LeadCandidate)
                .where(LeadCandidate.id == candidate_id, LeadCandidate.state == "hold")
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if (
                candidate is None
                or candidate.hold_until is None
                or candidate.hold_until > now
                or not candidates.is_handoff(candidate.score_breakdown)
                or await _handed_off(session, candidate.id)
            ):
                continue
            result = await candidates.rescore(session, candidate, now=now)
            if (
                result.usable_phone
                or result.total < scoring.HOT_MIN
                or not await _writable_username(session, candidate.company_id)
            ):
                continue
            walked = await candidates.apply_score(
                session,
                candidate,
                result,
                now=now,
                auto_push=False,
                gate=await gates.evaluate(session, candidate.company_id, now=now),
                walk=("scored", "review"),
            )
            if walked and walked[-1] == "review":
                candidate.tier = "warm"
                _trail(
                    candidate,
                    [
                        entry(
                            "reask", "review", now=now, attempt=candidate.enrich_attempts
                        )
                    ],
                )
                moved += 1
    return moved


# --- the queues -------------------------------------------------------------------------


async def _claim(
    session_factory: SessionFactory, candidate_id: uuid.UUID, *, now: datetime, state: str
) -> tuple[uuid.UUID, int] | None:
    """Take one due candidate: its company, then the candidate (``SKIP
    LOCKED``), still due, still in ``state``; lease it (``next_enrich_at``
    moved ``LEASE`` on) so no other run takes it meanwhile."""
    async with session_factory() as session, session.begin():
        company_id = await session.scalar(
            sa.select(LeadCandidate.company_id).where(LeadCandidate.id == candidate_id)
        )
        if company_id is None:
            return None
        company = await session.scalar(
            sa.select(Company)
            .where(Company.id == company_id)
            .with_for_update(key_share=True, skip_locked=True)
        )
        if company is None:
            return None
        candidate = await session.scalar(
            sa.select(LeadCandidate)
            .where(
                LeadCandidate.id == candidate_id,
                LeadCandidate.state == state,
                LeadCandidate.next_enrich_at <= now,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if candidate is None:
            return None
        candidate.next_enrich_at = now + LEASE
        return company_id, candidate.enrich_attempts


# Where /stir leaves its mark (services/owner.set_stir): an evidence row at
# ``owner:/stir:<company_id>`` fetched at the very moment it set
# ``next_enrich_at`` — so a clock an earlier enrichment left on a candidate
# that has since moved on (02 §2.9 keeps it across a hold) is not a re-run.
STIR_MARK: Final = f"{owner.STIR_EXCERPT}:"
STIR_STATES: Final = ("found", "scored", "review", "hold")


def _stir_asked() -> sa.ColumnElement[bool]:
    return (
        sa.select(Evidence.id)
        .where(
            Evidence.source_key == REGISTRY_SOURCE,
            Evidence.url
            == sa.func.concat(STIR_MARK, sa.cast(LeadCandidate.company_id, sa.Text)),
            Evidence.fetched_at == LeadCandidate.next_enrich_at,
        )
        .exists()
    )


async def _due(
    session_factory: SessionFactory, *, now: datetime, limit: int, attempts: bool
) -> list[tuple[uuid.UUID, str]]:
    """The attempts due (``enriching``), or the ``/stir`` re-runs due."""
    if attempts:
        where = [LeadCandidate.state == "enriching"]
    else:
        where = [LeadCandidate.state.in_(STIR_STATES), _stir_asked()]
    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.select(LeadCandidate.id, LeadCandidate.state)
                .where(
                    *where,
                    LeadCandidate.next_enrich_at.is_not(None),
                    LeadCandidate.next_enrich_at <= now,
                )
                .order_by(LeadCandidate.next_enrich_at, LeadCandidate.id)
                .limit(limit)
            )
        ).all()
    return [(r.id, r.state) for r in rows]


async def _source_run(
    session: AsyncSession, key: str, *, now: datetime, fetcher: Fetcher, hooks: Any
) -> SourceRun:
    source = await session.get(Source, key)
    view = fetcher.bind(hooks=hooks)
    if source is None:
        empty = Budget(pages=0, usd=Decimal("0"), deadline=now)
        return SourceRun(key, empty, view, usable=False, why_not="no_source")
    budget = await build_budget(session, source, now=now, deadline=RUN_DEADLINE)
    why: str | None = None
    if not settings.collectors_enabled:
        why = "collectors_disabled"
    elif not source.enabled:
        why = "source_disabled"
    elif source.paused_until is not None and source.paused_until > now:
        why = "paused"
    run = SourceRun(
        key,
        budget,
        view,
        usable=why is None,
        why_not=why,
        config=dict(source.config or {}),
    )
    if key == REGISTRY_SOURCE:
        day = costs.day_start(now)
        done = await session.scalar(
            sa.select(
                sa.func.coalesce(
                    sa.func.sum(CollectorRun.meta["lookups"].astext.cast(sa.Integer)), 0
                )
            ).where(
                CollectorRun.source_key == REGISTRY_SOURCE,
                CollectorRun.started_at >= day,
            )
        )
        run.lookups_left = max(registry.LOOKUPS_PER_DAY - int(done or 0), 0)
    return run


async def _record_run(
    session_factory: SessionFactory, run: SourceRun, *, started: datetime, ended: datetime
) -> None:
    """The source's ``collector_runs`` row for this run (its pages are what
    the next run's budget subtracts)."""
    stats = run.fetcher.stats
    if not stats.requests and not run.lookups and not run.new_evidence:
        return
    status = "partial" if stats.blocked_hosts or stats.exhausted is not None else "ok"
    meta: dict[str, Any] = {
        "note": "enrich",
        "unchanged": stats.unchanged,
        "skipped_robots": stats.skipped_robots,
    }
    if run.key == REGISTRY_SOURCE:
        meta["lookups"] = run.lookups
    if stats.blocked_hosts:
        meta["blocked_hosts"] = list(stats.blocked_hosts)
    if stats.exhausted is not None:
        meta["exhausted"] = stats.exhausted
    async with session_factory() as session, session.begin():
        session.add(
            CollectorRun(
                source_key=run.key,
                started_at=started,
                finished_at=max(ended, started),
                status=status,
                requests=stats.requests,
                pages=stats.pages,
                http_403=stats.http_403,
                http_429=stats.http_429,
                new_evidence=run.new_evidence,
                new_observations=run.new_observations,
                cost_usd=stats.cost_usd,
                meta=meta,
            )
        )


async def enrich_batch(
    tools: Tools,
    *,
    now: datetime,
    session_factory: SessionFactory = SessionLocal,
    limit: int = ENRICH_LIMIT,
    clock: Callable[[], datetime] | None = None,
) -> EnrichReport:
    """One run of the three queues (module docstring)."""
    report = EnrichReport()
    clock_of = clock or (lambda: datetime.now(settings.tz))
    async with session_factory() as session:
        if await costs.global_pause(session):
            report.stopped = "global_pause"
            return report
        ledger = RunLedger(session_factory, tools.evidence_root)
        site = await _source_run(
            session, SITE_SOURCE, now=now, fetcher=tools.fetcher, hooks=ledger
        )
        reg = await _source_run(
            session, REGISTRY_SOURCE, now=now, fetcher=tools.fetcher, hooks=None
        )
    ctx = Context(
        session_factory=session_factory,
        tools=tools,
        site=site,
        registry=reg,
        now=now,
        deadline=now + RUN_DEADLINE,
        clock=lambda: max(clock_of(), now),
    )
    budget_left = limit
    report.reasked = await reask_handoffs(session_factory, now=now, limit=budget_left)
    budget_left -= report.reasked
    try:
        for attempts in (False, True):
            if budget_left <= 0:
                break
            for candidate_id, state in await _due(
                session_factory, now=now, limit=budget_left, attempts=attempts
            ):
                if budget_left <= 0:
                    break
                claimed = await _claim(
                    session_factory, candidate_id, now=now, state=state
                )
                if claimed is None:
                    continue
                company_id, done = claimed
                budget_left -= 1
                run = Attempt(
                    candidate_id=candidate_id,
                    company_id=company_id,
                    mode="attempt" if attempts else "stir",
                    attempt=done + 1 if attempts else None,
                    steps=STEPS if attempts else ("E4",),
                    planned_state=state,
                )
                try:
                    if attempts and done >= MAX_ATTEMPTS:
                        run.steps = ()  # a stray row: S4 at once
                    await walk(ctx, run)
                except _Stop:
                    report.stopped = "deadline"
                    await _release(session_factory, candidate_id, now=now)
                    raise
                except Exception as exc:
                    log.error(
                        "enrichment failed",
                        extra={
                            "candidate_id": str(candidate_id),
                            "error": type(exc).__name__,
                        },
                    )
                    report.failed += 1
                    await _release(
                        session_factory, candidate_id, now=now, after=SCHEDULE[0]
                    )
                    continue
                walked = await finish(session_factory, run, now=now)
                if attempts:
                    report.attempts += 1
                    report.deferred += int(run.deferred)
                else:
                    report.reruns += 1
                if walked:
                    report.moved[walked[-1]] = report.moved.get(walked[-1], 0) + 1
    except _Stop:
        pass
    finally:
        ended = ctx.clock()
        await _record_run(session_factory, site, started=now, ended=ended)
        await _record_run(session_factory, reg, started=now, ended=ended)
    return report


async def _release(
    session_factory: SessionFactory,
    candidate_id: uuid.UUID,
    *,
    now: datetime,
    after: timedelta = timedelta(0),
) -> None:
    """A walk that did not finish gives its lease back: due again ``after``
    (a walk that failed waits an hour — it would fail the same way now)."""
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.update(LeadCandidate)
            .where(
                LeadCandidate.id == candidate_id,
                LeadCandidate.next_enrich_at > now,
            )
            .values(next_enrich_at=now + after)
        )


async def enrich_job(
    *, fetcher: Fetcher, now: datetime | None = None, limit: int = ENRICH_LIMIT
) -> dict[str, Any]:
    """The worker's ``enrich`` slot (04 §0: every 10 min 07:00–22:00, ≤ 30
    candidates); counts logged and returned (ids and counts only)."""
    report = await enrich_batch(
        Tools(fetcher=fetcher), now=now or datetime.now(settings.tz), limit=limit
    )
    counts = report.as_dict()
    log.info("enrich_job", extra={"counts": counts})
    return counts


__all__ = [
    "COMPANY_CAP",
    "ENRICH_LIMIT",
    "SCHEDULE",
    "STEPS",
    "CompanyQuery",
    "EnrichReport",
    "Page",
    "PaidFinder",
    "ProfileReader",
    "Tools",
    "apply_page",
    "enrich_batch",
    "enrich_job",
    "entry",
    "merged_sectors",
    "next_attempt_at",
    "reask_handoffs",
    "tomorrow_morning",
]
