"""The candidate state machine (02 §2.9) and the job that scores (04 §0, §5).

``TRANSITIONS`` is 02 §2.9's table and the only statement of it: every state
change of a ``lead_candidates`` row goes through :func:`transition`, which
refuses a step the table does not have (``IllegalTransition``) and a step that
lacks what its target state means — an exclusion without a reason, a
suppression without its row, an approval without a well-formed
``approved_by`` or without a usable phone («it is never pushed without a
usable phone», 04 §5.2). The database checks values and invariants only.

Producers built here:

* the resolver's hook (``after_resolve``, registered with
  ``companies.register_after_resolve`` when this module is imported): the
  observation's rule signals (``services/signals``) and, when the company has
  no open candidate and 02 §2.9's re-opening rule allows one, a ``found``
  candidate — attributed to the observation's source when the observation
  brought a volume or origin signal; else ``primary_source_key`` stays NULL
  until the first score names the source whose signal it scored on
  (``_attribute``: 02 §2.9 «the source whose signal created the candidate»);
* ``score_job`` (04 §0: on ``NOTIFY cs_interaction``, sweep every 15 min):
  each open candidate marked stale (``last_scored_at`` NULL — set by every
  signal written, every resolution and every merge — or older than the
  company's newest signal, or a hold whose ``hold_until`` ran out since its
  last score) is scored and routed by its tier: ``found``/``scored`` →
  ``review`` (hot; ``approved(auto:<source>)`` when the source pushes
  itself), ``enriching`` (warm, ``next_enrich_at`` in one hour) or ``hold``
  (cold). A ``found`` candidate waits for a volume signal (02 §2.9). A
  ``review`` row stays the owner's — unless it lost its usable phone and is
  not the phone-less handoff (06 §1.3: such a card is refused), then it steps
  back to ``hold`` until a phone or a signal re-scores it. The gates
  (``services/gates``, 04 §6) step in before the routing: the first failing
  gate decides — ``excluded``/``suppressed`` at once, a ``hold`` (with the
  gate's ``hold_until``) instead of the route — except gate 1's «not yet
  B2B», which waits until the candidate is bound for a card (``review``,
  ``approved``) or sits in one, because enrichment is what may make it B2B
  (``gated_walk``). Every gate's mark lands in ``score_breakdown.gates``;
* ``phone_less_review`` — 02 §2.9's ``enriching → review`` for a candidate
  that kept no phone after the third enrichment attempt but scores ≥ 60 and
  has a Telegram username (``tier='warm'``, ``gates.no_phone=true``; T-24's
  ``enrich_job`` calls it);
* ``manager_phone_rescore`` — a manager's ``/tel`` phone: ``hold → scored →
  approved(approved_by='manager:<tg_id>')`` (06 §1.7; the owner's earlier
  ``handoff`` answer was the owner's approval).

**Lock order: ``companies`` before ``lead_candidates``.** The resolver (its
updates of the company, then ``signals.emit`` → ``mark_stale``), ``emit`` and
``companies.merge_into`` all take them in that order. ``score_job`` claims the
candidate and its company in one ``FOR NO KEY UPDATE … SKIP LOCKED`` (a
company a writer holds is skipped, and re-marked stale by that writer), so it
never waits on either; ``rescore`` takes the company before it writes, so a
caller that has not locked the candidate first (T-24, the bot) keeps the
order too.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db import enums as e
from clientscrapper.db.enums import sql_list
from clientscrapper.db.models import (
    Company,
    Contact,
    Evidence,
    LeadCandidate,
    Observation,
    Outcome,
    Signal,
    Source,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import companies, gates, scoring, signals
from clientscrapper.services.scoring import ScoreResult, Tier

log = logging.getLogger(__name__)

# 02 §2.9, row by row.
TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    "found": frozenset({"enriching", "scored", "excluded", "suppressed"}),
    "enriching": frozenset({"scored", "review", "hold", "excluded", "suppressed"}),
    "scored": frozenset(
        {"review", "enriching", "hold", "approved", "excluded", "suppressed"}
    ),
    "review": frozenset({"approved", "rejected", "hold", "suppressed", "excluded"}),
    "approved": frozenset({"pushed", "review", "suppressed"}),
    "hold": frozenset({"scored", "excluded", "suppressed"}),
    "pushed": frozenset(),
    "rejected": frozenset(),
    "excluded": frozenset(),
    "suppressed": frozenset(),
}
TERMINAL_STATES: Final = frozenset(s for s, to in TRANSITIONS.items() if not to)
# 02 §2.9: 'owner:<id>' | 'auto:<source_key>' | 'manager:<tg_id>', exactly.
APPROVED_BY_RE: Final = re.compile(r"^(?:owner:\d+|manager:\d+|auto:[a-z][a-z0-9_]*)$")
# 9.1: a warm candidate's first enrichment attempt is an hour away.
FIRST_ENRICH_DELAY: Final = timedelta(hours=1)
# enrich_attempts' CHECK; the phone-less review is the third attempt's verdict.
MAX_ENRICH_ATTEMPTS: Final = 3
# 02 §2.9 re-opening windows.
REOPEN_AFTER_PUSH: Final = timedelta(days=90)
REOPEN_AFTER_REJECT: Final = timedelta(days=180)
# An exclusion that holds while the company flag behind it stands (02 §2.9:
# «never (owner can clear the company flag by hand)»).
FLAGGED_EXCLUSIONS: Final = frozenset({"competitor", "supplier_cn", "liquidated"})
SCORE_LIMIT: Final = 200
RESCORE_STATES: Final = signals.RESCORE_STATES
# The groups whose signal attributes a candidate to its source (02 §2.9
# ``primary_source_key``): what makes a lead, not a negative or a solidity row.
ATTRIBUTING_KEYS: Final = frozenset(
    w.key for w in e.WEIGHTS_V1 if w.group_key in scoring.CORROBORATION_GROUPS
)
# Where a card or a push is next (04 §6: the gates run «before a card»).
CARD_BOUND_STATES: Final = frozenset({"review", "approved"})


class IllegalTransition(Exception):
    def __init__(self, source: str, target: str, why: str = "not in TRANSITIONS") -> None:
        super().__init__(f"{source} → {target}: {why}")
        self.source = source
        self.target = target
        self.why = why


def check_transition(source: str, target: str) -> None:
    """Raise unless 02 §2.9's table has the step ``source → target``."""
    if target not in TRANSITIONS.get(source, frozenset()):
        raise IllegalTransition(source, target)


async def _has_usable_phone(session: AsyncSession, company_id: uuid.UUID) -> bool:
    company = await session.get(Company, company_id)
    if company is None:
        return False
    _, contacts = await scoring.views_for(session, company)
    return any(scoring.usable_phone(c) for c in contacts)


async def transition(
    session: AsyncSession,
    candidate: LeadCandidate,
    target: str,
    *,
    now: datetime,
    exclude_reason: str | None = None,
    suppressed_by: int | None = None,
    approved_by: str | None = None,
    hold_until: datetime | None = None,
    next_enrich_at: datetime | None = None,
    external_id: str | None = None,
) -> None:
    """Move ``candidate`` to ``target`` (02 §2.9) and set what the target
    state carries; flushes. Raises IllegalTransition for a step the table
    does not have or a target whose meaning is missing."""
    source = candidate.state
    check_transition(source, target)
    if target in ("scored", "review", "approved") and candidate.score is None:
        raise IllegalTransition(source, target, "not scored")
    if target == "excluded":
        if exclude_reason not in e.EXCLUDE_REASONS:
            raise IllegalTransition(source, target, "exclude_reason")
        candidate.exclude_reason = exclude_reason
    elif target == "suppressed":
        if suppressed_by is None:
            raise IllegalTransition(source, target, "suppressed_by")
        candidate.suppressed_by = suppressed_by
    elif target == "approved":
        if approved_by is None or not APPROVED_BY_RE.match(approved_by):
            raise IllegalTransition(source, target, "approved_by")
        if not await _has_usable_phone(session, candidate.company_id):
            raise IllegalTransition(source, target, "no usable phone")
        candidate.approved_by = approved_by
        candidate.approved_at = now
    elif target == "rejected":
        candidate.rejected_at = now
    elif target == "pushed":
        if external_id is None:
            raise IllegalTransition(source, target, "external_id")
        candidate.external_id = external_id
        candidate.pushed_at = now
    elif target == "hold":
        candidate.hold_until = hold_until
    elif target == "enriching":
        candidate.next_enrich_at = (
            next_enrich_at or candidate.next_enrich_at or (now + FIRST_ENRICH_DELAY)
        )
    if source == "hold" and target != "hold":
        candidate.hold_until = None
    candidate.state = target
    await session.flush()


# --- routing (pure) ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteFacts:
    auto_push: bool = False
    hold_until: datetime | None = None
    enrich_attempts: int = 0
    # The fresh score's usable phone, and the phone-less handoff flag
    # (``score_breakdown.gates.no_phone``) the row carries.
    usable_phone: bool = True
    no_phone: bool = False


def route(state: str, tier: Tier, facts: RouteFacts, *, now: datetime) -> tuple[str, ...]:
    """The states a fresh score walks ``state`` through (02 §2.9), in order;
    empty = the candidate stays where it is (its score is still updated).

    * ``found``/``scored``: hot → review (approved when the source pushes
      itself), warm → enriching, cold → hold — through ``scored``;
    * ``enriching``: hot → review, cold → hold; warm stays (enrichment's own
      clock runs on);
    * ``hold``: a hold that has not run out stays (a cooldown, a handoff);
      else hot → review, warm → enriching while attempts are left; cold stays;
    * ``review``: stays — the owner answers it — unless it has no usable phone
      and is not the phone-less handoff: then ``hold`` (06 §1.3 refuses a
      review card with neither)."""
    hot_to = "approved" if facts.auto_push else "review"
    if state in ("found", "scored"):
        to = {"hot": hot_to, "warm": "enriching", "cold": "hold"}[tier]
        return ("scored", to) if state == "found" else (to,)
    if state == "enriching":
        return {"hot": ("scored", hot_to), "warm": (), "cold": ("scored", "hold")}[tier]
    if state == "hold":
        if facts.hold_until is not None and facts.hold_until > now:
            return ()
        if tier == "hot":
            return ("scored", hot_to)
        if tier == "warm" and facts.enrich_attempts < MAX_ENRICH_ATTEMPTS:
            return ("scored", "enriching")
        return ()
    if state == "review" and not facts.usable_phone and not facts.no_phone:
        return ("hold",)
    return ()


def card_bound(state: str, walk: tuple[str, ...]) -> bool:
    """The candidate sits in a card or the walk takes it to a card or a
    push: gate 1's hold is judged (``GateResult.decide(card=…)``)."""
    target = walk[-1] if walk else state
    return state == "review" or target in CARD_BOUND_STATES


def gated_walk(
    state: str, walk: tuple[str, ...], failure: gates.Failure | None
) -> tuple[str, ...]:
    """The walk once the deciding gate has spoken (04 §6): an exclusion or
    a suppression at once; a hold instead of the route (``found`` passes
    through ``scored``; a held candidate stays held)."""
    if failure is None:
        return walk
    if failure.decision in ("excluded", "suppressed"):
        return (failure.decision,)
    if state == "hold":
        return ()
    return ("scored", "hold") if state == "found" else ("hold",)


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    return max((x for x in (a, b) if x is not None), default=None)


# --- the 'found' candidate (02 §2.9) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class LastCandidate:
    state: str
    pushed_at: datetime | None = None
    rejected_at: datetime | None = None
    suppressed_by: int | None = None
    exclude_reason: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReopenFacts:
    newest_signal_at: datetime | None = None
    # The pushed lead's ERP outcome: True terminal, False still open, None absent.
    outcome_terminal: bool | None = None
    is_competitor: bool = False
    is_supplier_cn: bool = False
    liquidated: bool = False


def _month(moment: datetime) -> tuple[int, int]:
    local = moment.astimezone(settings.tz)
    return local.year, local.month


def _newer(facts: ReopenFacts, than: datetime | None) -> bool:
    return (
        facts.newest_signal_at is not None
        and than is not None
        and facts.newest_signal_at > than
    )


def may_reopen(last: LastCandidate, facts: ReopenFacts, *, now: datetime) -> bool:
    """02 §2.9: may a new candidate follow ``last``, the company's newest one?
    After ``pushed``: the outcome is terminal, or absent for 90 days; a signal
    is newer than ``pushed_at``; and the month differs (the external id is the
    month's). After ``rejected``: 180 days and a newer signal. After
    ``suppressed``: only when the suppression row is gone. After ``excluded``
    as a competitor, a Chinese supplier or liquidated: only once the company
    flag is cleared; another exclusion: on a signal newer than it."""
    if last.state == "pushed":
        pushed = last.pushed_at
        if pushed is None:
            return False
        settled = facts.outcome_terminal is True or (
            facts.outcome_terminal is None and now - pushed >= REOPEN_AFTER_PUSH
        )
        return settled and _newer(facts, pushed) and _month(now) != _month(pushed)
    if last.state == "rejected":
        rejected = last.rejected_at
        return (
            rejected is not None
            and now - rejected >= REOPEN_AFTER_REJECT
            and _newer(facts, rejected)
        )
    if last.state == "suppressed":
        return last.suppressed_by is None
    if last.state == "excluded":
        if last.exclude_reason == "competitor":
            return not facts.is_competitor
        if last.exclude_reason == "supplier_cn":
            return not facts.is_supplier_cn
        if last.exclude_reason == "liquidated":
            return not facts.liquidated
        return _newer(facts, last.updated_at)
    return False


def outcome_terminal(outcome: Outcome | None) -> bool | None:
    """A pushed lead's outcome is terminal when the ERP closed it (won/lost)
    or never kept it as a lead of its own (landed on a client, dropped)."""
    if outcome is None:
        return None
    return outcome.landing in ("client", "dropped") or outcome.stage_kind in (
        "won",
        "lost",
    )


async def open_candidate(
    session: AsyncSession, company_id: uuid.UUID
) -> LeadCandidate | None:
    found: LeadCandidate | None = await session.scalar(
        sa.select(LeadCandidate).where(
            LeadCandidate.company_id == company_id,
            LeadCandidate.state.in_(e.CANDIDATE_OPEN_STATES),
        )
    )
    return found


async def ensure_found(
    session: AsyncSession,
    company: Company,
    *,
    source_key: str | None,
    contact_id: uuid.UUID | None,
    now: datetime,
) -> LeadCandidate | None:
    """The company's open candidate, or a new ``found`` one attributed to
    ``source_key`` (``primary_source_key``, the precision attribution; None
    = not yet known, ``_attribute`` names it at the first score) when 02 §2.9
    allows one; None when it does not. A concurrent resolution that made the
    same candidate first wins (ON CONFLICT on the open index)."""
    held = await open_candidate(session, company.id)
    if held is not None:
        return held
    if company.state in ("excluded", "merged", "purged"):
        return None
    last = await session.scalar(
        sa.select(LeadCandidate)
        .where(LeadCandidate.company_id == company.id)
        .order_by(LeadCandidate.created_at.desc(), LeadCandidate.id.desc())
        .limit(1)
    )
    if last is not None:
        outcome = (
            await session.get(Outcome, last.external_id) if last.external_id else None
        )
        facts = ReopenFacts(
            newest_signal_at=company.last_signal_at,
            outcome_terminal=outcome_terminal(outcome),
            is_competitor=company.is_competitor,
            is_supplier_cn=company.is_supplier_cn,
            liquidated=company.registry_status == "liquidated",
        )
        previous = LastCandidate(
            state=last.state,
            pushed_at=last.pushed_at,
            rejected_at=last.rejected_at,
            suppressed_by=last.suppressed_by,
            exclude_reason=last.exclude_reason,
            updated_at=last.updated_at,
        )
        if not may_reopen(previous, facts, now=now):
            return None
    await session.execute(
        pg_insert(LeadCandidate)
        .values(
            company_id=company.id,
            contact_id=contact_id,
            state="found",
            primary_source_key=source_key,
        )
        .on_conflict_do_nothing(
            index_elements=["company_id"],
            index_where=sa.text(f"state IN ({sql_list(e.CANDIDATE_OPEN_STATES)})"),
        )
    )
    return await open_candidate(session, company.id)


async def after_resolve(
    session: AsyncSession,
    obs: Observation,
    resolution: companies.Resolution,
    now: datetime,
) -> None:
    """The resolver's hook (04 §0 «→ signals, lead_candidates(found)»): the
    observation's rule signals, the ``found`` candidate, and the open
    candidate marked for re-scoring — a resolution may have added a phone or
    a handle even when it brought no signal."""
    company = resolution.company
    if company.state in ("merged", "purged"):
        return
    contact_id = resolution.contact.id if resolution.contact is not None else None
    evidence = signals.EvidenceRef(
        id=obs.evidence_id,
        source_key=obs.source_key,
        fetched_at=obs.created_at or now,
        observation_id=obs.id,
        contact_id=contact_id,
    )
    facts = await signals.company_facts(session, company)
    drafts = signals.from_rules(evidence, obs.parsed, facts)
    weights = await scoring.load_weights(session)
    await signals.emit(session, company.id, drafts, weights=weights)
    await session.refresh(company, ["last_signal_at"])
    attributing = any(d.key in ATTRIBUTING_KEYS for d in drafts)
    await ensure_found(
        session,
        company,
        source_key=obs.source_key if attributing else None,
        contact_id=contact_id,
        now=now,
    )
    await signals.mark_stale(session, company.id)


companies.register_after_resolve(after_resolve)


# --- scoring a candidate ------------------------------------------------------------


def _carried(
    previous: Mapping[str, Any] | None, breakdown: dict[str, Any]
) -> dict[str, Any]:
    """A re-score keeps what other writers put in the breakdown: the gates'
    verdicts (T-19, incl. ``gates.no_phone``) and the enrichment trail (T-24).
    ``gates.no_phone`` is the phone-less handoff's mark and holds only while
    there is no usable phone: a score that finds one drops it (the card is
    the ordinary one again, 06 §1.3; a manager's approval is recorded by
    ``approved_by='manager:<tg_id>'``)."""
    merged = dict(breakdown)
    if previous:
        gates = previous.get("gates")
        enrichment = previous.get("enrichment")
        if isinstance(gates, Mapping):
            merged["gates"] = {**dict(gates), **merged["gates"]}
        if isinstance(enrichment, list):
            merged["enrichment"] = list(enrichment)
    if merged.get("usable_phone") is True and "no_phone" in merged["gates"]:
        merged["gates"] = {k: v for k, v in merged["gates"].items() if k != "no_phone"}
    return merged


def _handoff(breakdown: Mapping[str, Any] | None) -> bool:
    """``score_breakdown.gates.no_phone`` is set (the phone-less handoff)."""
    gates = (breakdown or {}).get("gates")
    return isinstance(gates, Mapping) and gates.get("no_phone") is True


async def _attribute(session: AsyncSession, candidate: LeadCandidate) -> None:
    """Name the candidate's source when its creation could not (02 §2.9
    «the source whose signal created the candidate»): the source of the
    company's first volume or origin signal written since the candidate was
    made — the signal a ``found`` row is first scored on — or, for a row
    older than every signal, the first one at all."""
    if candidate.primary_source_key is not None:
        return
    base = (
        sa.select(Signal.source_key)
        .where(
            Signal.company_id == candidate.company_id,
            Signal.key.in_(ATTRIBUTING_KEYS),
        )
        .order_by(Signal.observed_at, Signal.id)
        .limit(1)
    )
    made = (
        sa.select(LeadCandidate.created_at)
        .where(LeadCandidate.id == candidate.id)
        .scalar_subquery()
    )
    found = await session.scalar(
        base.where(Signal.created_at >= made)
    ) or await session.scalar(base)
    if found is not None:
        candidate.primary_source_key = found


async def rescore(
    session: AsyncSession,
    candidate: LeadCandidate,
    *,
    now: datetime,
    weights: scoring.WeightSet | None = None,
    families: Mapping[str, str] | None = None,
) -> ScoreResult:
    """Score ``candidate``'s company now and write the score (not the state):
    ``score``, ``tier``, ``score_breakdown`` (other writers' keys carried),
    ``weights_version``, ``last_scored_at``, ``primary_source_key`` when it
    is not yet known (``_attribute``), and ``companies.best_score``. The
    company row is locked first (``FOR NO KEY UPDATE``; the module's lock
    order)."""
    company = await session.get(
        Company,
        candidate.company_id,
        with_for_update={"key_share": True},
        populate_existing=True,
    )
    if company is None:
        raise LookupError(f"company {candidate.company_id} is gone")
    weights = weights or await scoring.load_weights(session)
    if families is None:
        families = await source_families(session)
    signal_views, contact_views = await scoring.views_for(session, company)
    result = scoring.score(
        signal_views, contact_views, source_families=families, weights=weights, now=now
    )
    candidate.score = result.total
    candidate.tier = result.tier
    candidate.score_breakdown = _carried(candidate.score_breakdown, result.breakdown)
    candidate.weights_version = weights.version
    candidate.last_scored_at = now
    await _attribute(session, candidate)
    company.best_score = max(company.best_score or 0, result.total)
    await session.flush()
    return result


async def source_families(session: AsyncSession) -> dict[str, str]:
    rows = await session.execute(sa.select(Source.key, Source.family))
    return {str(k): str(f) for k, f in rows.all()}


async def _auto_push(session: AsyncSession, source_key: str | None) -> bool:
    if source_key is None:
        return False
    found = await session.scalar(
        sa.select(Source.auto_push).where(Source.key == source_key)
    )
    return bool(found)


async def apply_score(
    session: AsyncSession,
    candidate: LeadCandidate,
    result: ScoreResult,
    *,
    now: datetime,
    auto_push: bool,
    gate: gates.GateResult | None = None,
) -> tuple[str, ...]:
    """Walk ``candidate`` through ``route``'s states — or, when ``gate``
    has a deciding failure, through ``gated_walk``'s — and record every
    gate's mark in ``score_breakdown.gates``; returns the walk."""
    facts = RouteFacts(
        auto_push=auto_push,
        hold_until=candidate.hold_until,
        enrich_attempts=candidate.enrich_attempts,
        usable_phone=result.usable_phone,
        no_phone=_handoff(candidate.score_breakdown),
    )
    state = candidate.state
    walk = route(state, result.tier, facts, now=now)
    failure: gates.Failure | None = None
    if gate is not None:
        failure = gate.decide(card=card_bound(state, walk))
        walk = gated_walk(state, walk, failure)
        breakdown = dict(candidate.score_breakdown or {})
        breakdown["gates"] = {**dict(breakdown.get("gates") or {}), **gate.marks}
        candidate.score_breakdown = breakdown
        await gates.apply_effects(session, candidate.company_id, failure)
    held_until = failure.until if failure is not None else None
    for target in walk:
        await transition(
            session,
            candidate,
            target,
            now=now,
            approved_by=(
                f"auto:{candidate.primary_source_key}" if target == "approved" else None
            ),
            exclude_reason=failure.reason if target == "excluded" and failure else None,
            suppressed_by=(
                failure.hit.id
                if target == "suppressed" and failure and failure.hit
                else None
            ),
            hold_until=held_until if target == "hold" else None,
        )
    if failure is not None and failure.decision == "hold" and not walk:
        # Already held: the gate's window extends the hold, never shortens it.
        candidate.hold_until = _later(candidate.hold_until, held_until)
        await session.flush()
    return walk


# --- the two other producers ----------------------------------------------------------


async def phone_less_review(
    session: AsyncSession, candidate: LeadCandidate, *, now: datetime
) -> None:
    """02 §2.9 / 04 §5.2 F-12: ``enriching → review`` after the third attempt
    for a candidate with no usable phone, ``score ≥ 60`` and a Telegram
    username on a contact: ``tier='warm'``, ``score_breakdown.gates.no_phone
    = true`` (the owner card's handoff variant, 06 §1.3). Raises
    IllegalTransition when any of that does not hold."""
    source = candidate.state
    if source != "enriching":
        raise IllegalTransition(source, "review", "not enriching")
    if candidate.enrich_attempts < MAX_ENRICH_ATTEMPTS:
        raise IllegalTransition(source, "review", "attempts left")
    if candidate.score is None or candidate.score < scoring.HOT_MIN:
        raise IllegalTransition(source, "review", "score below 60")
    company = await session.get(Company, candidate.company_id)
    if company is None:
        raise LookupError(f"company {candidate.company_id} is gone")
    _, contacts = await scoring.views_for(session, company)
    if any(scoring.usable_phone(c) for c in contacts):
        raise IllegalTransition(source, "review", "has a phone")
    # A contact row's username (02 §2.9 «contacts.telegram_username IS NOT
    # NULL») — the person a manager writes to; the company's own channel
    # (``companies.telegram_channel``) is a broadcast nobody can answer.
    writable = await session.scalar(
        sa.select(
            sa.exists().where(
                Contact.company_id == company.id,
                Contact.telegram_username.is_not(None),
                Contact.anonymised_at.is_(None),
            )
        )
    )
    if not writable:
        raise IllegalTransition(source, "review", "no telegram username")
    breakdown = dict(candidate.score_breakdown or {})
    breakdown["gates"] = {**dict(breakdown.get("gates") or {}), "no_phone": True}
    candidate.score_breakdown = breakdown
    candidate.tier = "warm"
    await transition(session, candidate, "review", now=now)


async def manager_phone_rescore(
    session: AsyncSession, candidate: LeadCandidate, *, tg_id: int, now: datetime
) -> ScoreResult:
    """A manager's ``/tel`` phone after a handoff (06 §1.7): re-score at once
    and, with a usable phone, ``hold → scored → approved`` with
    ``approved_by='manager:<tg_id>'``. Without one the candidate stays in
    ``hold`` with its new score. Raises IllegalTransition unless in hold."""
    if candidate.state != "hold":
        raise IllegalTransition(candidate.state, "scored", "not in hold")
    result = await rescore(session, candidate, now=now)
    if result.usable_phone:
        await transition(session, candidate, "scored", now=now)
        await transition(
            session, candidate, "approved", now=now, approved_by=f"manager:{tg_id}"
        )
    return result


# --- score_job (04 §0) --------------------------------------------------------------

SessionFactory = Callable[[], AsyncSession]


@dataclass(slots=True)
class ScoreReport:
    claimed: int = 0
    scored: int = 0
    failed: int = 0
    moved: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int]:
        counts = {"claimed": self.claimed, "scored": self.scored, "failed": self.failed}
        counts.update({f"to_{state}": n for state, n in sorted(self.moved.items())})
        return counts


def _stale(now: datetime) -> sa.ColumnElement[bool]:
    """A candidate score_job owes a score: open in a re-scored state, and
    marked stale, or older than its company's newest signal, or a hold whose
    ``hold_until`` ran out since its last score (a review that expired, a
    cooldown — 02 §2.9's timed holds come back without a new signal); and —
    for ``found`` — a volume signal to score on (02 §2.9)."""
    has_volume = (
        sa.select(Signal.id)
        .where(
            Signal.company_id == LeadCandidate.company_id,
            Signal.key.in_(signals.VOLUME_KEYS),
        )
        .exists()
    )
    hold_ran_out = sa.and_(
        LeadCandidate.state == "hold",
        LeadCandidate.hold_until <= now,
        LeadCandidate.last_scored_at < LeadCandidate.hold_until,
    )
    return sa.and_(
        LeadCandidate.state.in_(RESCORE_STATES),
        sa.or_(
            LeadCandidate.last_scored_at.is_(None),
            Company.last_signal_at > LeadCandidate.last_scored_at,
            hold_ran_out,
        ),
        sa.or_(LeadCandidate.state != "found", has_volume),
    )


async def _score_one(
    session_factory: SessionFactory,
    *,
    now: datetime,
    done: set[uuid.UUID],
    weights: scoring.WeightSet,
    families: Mapping[str, str],
    report: ScoreReport,
) -> bool:
    """Claim the oldest stale candidate with its company (``FOR NO KEY UPDATE
    OF lead_candidates, companies SKIP LOCKED``: the module's lock order, and
    a company a writer holds is skipped — that writer marks it stale again)
    and score it in its own transaction. False when nothing is owed."""
    async with session_factory() as session, session.begin():
        stmt = (
            sa.select(LeadCandidate)
            .join(Company, Company.id == LeadCandidate.company_id)
            .where(_stale(now))
        )
        if done:
            stmt = stmt.where(LeadCandidate.id.not_in(done))
        candidate = await session.scalar(
            stmt.order_by(LeadCandidate.created_at, LeadCandidate.id)
            .limit(1)
            .with_for_update(
                # A column names its table: OF lead_candidates, companies.
                of=[LeadCandidate.id, Company.id],
                skip_locked=True,
                key_share=True,
            )
        )
        if candidate is None:
            return False
        report.claimed += 1
        # Read before the savepoint: its rollback expires the row, and an
        # expired attribute cannot be loaded in the handler.
        candidate_id = candidate.id
        done.add(candidate_id)
        try:
            async with session.begin_nested():
                result = await rescore(
                    session, candidate, now=now, weights=weights, families=families
                )
                walk = await apply_score(
                    session,
                    candidate,
                    result,
                    now=now,
                    auto_push=await _auto_push(session, candidate.primary_source_key),
                    gate=await gates.evaluate(session, candidate.company_id, now=now),
                )
        except Exception as exc:
            # A candidate that cannot be scored costs only itself; the error
            # class only (a message may carry a row's values, 06 §4.5).
            log.error(
                "scoring failed",
                extra={"candidate_id": str(candidate_id), "error": type(exc).__name__},
            )
            report.failed += 1
            return True
        report.scored += 1
        if walk:
            report.moved[walk[-1]] = report.moved.get(walk[-1], 0) + 1
    return True


async def score_batch(
    session_factory: SessionFactory = SessionLocal,
    *,
    now: datetime,
    limit: int = SCORE_LIMIT,
) -> ScoreReport:
    """Score up to ``limit`` stale candidates, one transaction each, with the
    weights and source families read once for the batch."""
    async with session_factory() as session:
        weights = await scoring.load_weights(session)
        families = await source_families(session)
    report = ScoreReport()
    done: set[uuid.UUID] = set()
    for _ in range(limit):
        if not await _score_one(
            session_factory,
            now=now,
            done=done,
            weights=weights,
            families=families,
            report=report,
        ):
            break
    return report


async def score_job(
    *, now: datetime | None = None, limit: int = SCORE_LIMIT
) -> dict[str, int]:
    """The scheduled job: the ``score`` slot of the worker table (every 15
    min, and at once on ``NOTIFY cs_interaction``, 04 §0); its listener
    records the ``job:score`` heartbeat. Counts are logged (ids and counts
    only) and returned."""
    report = await score_batch(now=now or datetime.now(settings.tz), limit=limit)
    counts = report.as_dict()
    if report.claimed:
        log.info("score_job", extra={"counts": counts})
    return counts


# --- what a push reads (05 §B.1 ``CandidateView``) -----------------------------------


@dataclass(frozen=True, slots=True)
class SignalEvidence:
    """One signal of the company's lineage with the evidence it stands on:
    what the ERP note cites (URL + date + verbatim quote)."""

    signal_id: int
    key: str
    group: str
    points: int
    value: Decimal | None
    quote: str | None
    observed_at: datetime
    source_key: str
    source_family: str
    evidence_id: int
    url: str
    published_at: datetime | None
    fetched_at: datetime
    excerpt: str


@dataclass(frozen=True, slots=True)
class PersonView:
    """The one decision maker the note may name — a person-tier contact with
    its ``legal_basis`` (CLAUDE.md: at most one per company)."""

    full_name: str
    role: str | None
    telegram_username: str | None


@dataclass(frozen=True, slots=True)
class CandidateView:
    """Read-only: everything ``crm_push.build_payload`` reads (05 §B.1). The
    signals are ranked best first — the volume group before origin before
    the rest, by points, then the newest."""

    candidate_id: uuid.UUID
    company_id: uuid.UUID
    external_id: str
    display_name: str
    legal_form: str | None
    stir: str | None
    city: str | None
    address: str | None
    website_domain: str | None
    telegram: str | None
    instagram: str | None
    registry_status: str
    sectors: tuple[str, ...]
    goods: tuple[str, ...]
    volume_band: str
    phone: str | None
    person: PersonView | None
    signals: tuple[SignalEvidence, ...]
    stated_m3: Decimal | None
    score: int
    breakdown: Mapping[str, Any]
    opener: str | None = None
    per_month: str | None = None


# The groups a note's first line may cite, in the order it prefers them.
CITED_GROUPS: Final = ("volume", "origin", "solidity")


def _signal_rank(s: SignalEvidence) -> tuple[int, int, float]:
    group = CITED_GROUPS.index(s.group) if s.group in CITED_GROUPS else len(CITED_GROUPS)
    return (group, -s.points, -s.observed_at.timestamp())


async def load_view(
    session: AsyncSession, candidate_id: uuid.UUID, *, now: datetime
) -> CandidateView:
    """Assemble the ``CandidateView`` of ``candidate_id`` (05 §B.1): the
    company, the lead contact the push gate chooses (``push_gate.lead_contact``),
    the decision maker with a legal basis, the lineage's live signals with
    their evidence, the stated volume, the score's breakdown and — when 04's
    draft stored one — the opener (``score_breakdown.opener``; UNVERIFIED:
    T-30 decides where the draft lives). Raises LookupError for a candidate
    or a company that is gone."""
    from clientscrapper.services import push_gate  # it imports this module

    candidate = await session.get(LeadCandidate, candidate_id, populate_existing=True)
    if candidate is None:
        raise LookupError(f"candidate {candidate_id} is gone")
    company = await session.get(Company, candidate.company_id, populate_existing=True)
    if company is None:
        raise LookupError(f"company {candidate.company_id} is gone")
    lineage = sorted((await companies.lineages(session, [company.id]))[company.id])
    lead = await push_gate.lead_contact(session, candidate)
    contacts = (
        await session.scalars(
            sa.select(Contact)
            .where(Contact.company_id.in_(lineage), Contact.anonymised_at.is_(None))
            .order_by(Contact.created_at, Contact.id)
        )
    ).all()
    person = next(
        (
            PersonView(c.full_name, c.role, c.telegram_username)
            for c in sorted(contacts, key=lambda c: not c.is_decision_maker)
            if c.tier == "person" and c.legal_basis and c.full_name
        ),
        None,
    )
    company_tier = [c for c in contacts if c.tier == "company"]
    if lead is not None and lead.tier == "company":
        company_tier.sort(key=lambda c: c.id != lead.id)
    telegram = company.telegram_channel or next(
        (c.telegram_username for c in company_tier if c.telegram_username), None
    )
    if telegram is None and person is not None:
        telegram = person.telegram_username
    instagram = company.instagram or next(
        (c.instagram for c in company_tier if c.instagram), None
    )
    try:
        weights = await scoring.load_weights(session)
    except LookupError:
        weights = scoring.weights_v1()
    families = await source_families(session)
    rows = (
        await session.execute(
            sa.select(Signal, Evidence)
            .join(Evidence, Evidence.id == Signal.evidence_id)
            .where(
                Signal.company_id.in_(lineage),
                sa.or_(Signal.expires_at.is_(None), Signal.expires_at > now),
            )
        )
    ).all()
    found = [
        SignalEvidence(
            signal_id=sig.id,
            key=sig.key,
            group=weights.rows[sig.key].group if sig.key in weights.rows else "other",
            points=weights.rows[sig.key].points if sig.key in weights.rows else 0,
            value=sig.value,
            quote=sig.quote,
            observed_at=sig.observed_at,
            source_key=sig.source_key,
            source_family=families.get(sig.source_key, sig.source_key),
            evidence_id=ev.id,
            url=ev.url,
            published_at=ev.published_at,
            fetched_at=ev.fetched_at,
            excerpt=ev.excerpt,
        )
        for sig, ev in rows
    ]
    breakdown = candidate.score_breakdown or {}
    opener = breakdown.get("opener")
    return CandidateView(
        candidate_id=candidate.id,
        company_id=company.id,
        external_id=push_gate.external_id_for(company.id, now),
        display_name=company.display_name,
        legal_form=company.legal_form,
        stir=company.stir.strip() if company.stir else None,
        city=company.city,
        address=company.address,
        website_domain=company.website_domain,
        telegram=telegram,
        instagram=instagram,
        registry_status=company.registry_status,
        sectors=tuple(company.sectors or ()),
        goods=tuple(company.goods or ()),
        volume_band=company.volume_band,
        phone=lead.phone if lead is not None else None,
        person=person,
        signals=tuple(sorted(found, key=_signal_rank)),
        stated_m3=await push_gate.stated_m3(session, lineage),
        score=candidate.score or 0,
        breakdown=breakdown,
        opener=opener if isinstance(opener, str) and opener.strip() else None,
    )
