"""The RelevanceVerdict of a company, in both lanes (04 §4.1, §4.4, §4.8).

**The bundle** (``bundle_for``) — what both lanes ask with, one builder so
their answers are comparable (04 §4.4): the header ``COMPANY: <name> |
<legal form> | <city> | STIR: <9 digits or ->``, then at most
``BLOCKS_MAX`` blocks of the company's evidence (its lineage's: the
observations resolved to it and the evidence its signals stand on), newest
first, each ``[E<n>] <source family> <date> <url host>`` and the
pseudonymised excerpt (≤ 600 characters). The bundle is held to
``BUNDLE_TOKENS_MAX`` tokens (two bytes a token, under what Latin or
Cyrillic text averages; ``extraction`` claims the money by its own
byte-per-token bound): older blocks are dropped and the header says how
many. The people's names and handles are masked with one numbering
(``pseudonyms.pseudonymise``, ``prior``). ``model_jobs.evidence_ids`` holds
the blocks in order — ``E<n>`` is its n-th id — so the checker of either
lane rebuilds the blocks from the job.

**Checked** (``validators.relevance_verdict``): each quote against its own
block, the reasons and the role made owner text, a weak exclusion flag
dropped without its evidence, hygiene.

**Applied** (``apply_verdict``, the batch kind's effects and the instant
lane's, in the job's transaction): ``companies.verdict`` / ``verdict_at`` /
``segment`` / ``volume_band`` (a known band; the band is for the card and
routing and **never adds points** — points come from quoted evidence) /
``goods`` (merged, ≤ 6); ``confirmed`` with its verified quote →
``cn_origin`` (on the quote's evidence, ``model`` = the answering model),
``likely`` → ``cn_sector_prior``; ``is_competitor`` → ``flag_competitor``
and ``companies.is_competitor``, ``is_supplier_cn`` → ``flag_supplier_cn``
and ``companies.is_supplier_cn``, ``is_state_body`` →
``companies.is_state_body`` (a verdict sets a flag, never clears one);
``decision_maker_role`` → the role of the person-tier contact that has none
(the decision maker first); the company's open candidate is re-scored
(``signals.emit`` / ``signals.mark_stale``). ``lt1`` at ``confidence ≥
0.8`` is ``validators.verdict_small``: the card says so, no points.

**The instant lane** (``verdict_job``, every 10 minutes, ``VERDICT_BATCH`` a
run): a candidate in ``review`` whose company has no verdict younger than
``VERDICT_FRESH`` (30 days), and — while the night's batch is more than
``BATCH_AWAY`` (12 hours) off — one in ``enriching``/``scored``/``review``
scored 40–74 with ≥ 2 evidence rows and evidence newer than its verdict
(04 §4.1). Each company once at a time (an advisory lock, no row lock held
during the call); a budget refusal before anything was sent keeps nothing
and stops the run; a final failure (the model refused, an answer malformed
twice) is not asked again until new evidence arrives; an API failure waits
``RETRY_AFTER`` × 2ⁿ⁻¹ and is given up for the evidence at ``TRIES_MAX``.
An instant verdict supersedes the company's batch job still waiting for its
night. The job never hands over the batch lane's fallbacks: those belong to
the 23:30 run alone (``model_jobs.take_fallbacks``, ``FALLBACK_UNTIL``).
Then each ``review``/``approved`` candidate without an opener gets one
(``openers.ensure_opener``: the draft for a hot one, the template else).

**The night** (``select_company_verdicts``, the kind's ``select``, run by
``model_batch_submit`` right before the submit): companies whose candidate
is ``enriching``/``scored`` at 40–59 with evidence newer than their verdict,
and on Sunday nights ``hold`` companies with new evidence; at most
``model_jobs.BATCH_MAX`` waiting a night. A company whose last
``company_verdict`` ended ``failed`` or as a taken ``fallback`` is asked
again (its evidence is still newer than its verdict) — only a model refusal
on the same evidence is not; one with a job in flight (submitted, or a
fallback not yet taken) is left to it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import (
    Company,
    Contact,
    Evidence,
    LeadCandidate,
    ModelJob,
    Signal,
    Source,
    UsageLog,
)
from clientscrapper.db.models import Observation as ObservationRow
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import (
    companies,
    costs,
    extraction,
    model_jobs,
    openers,
    scoring,
    signals,
    validators,
)
from clientscrapper.services.classify import clip_masked
from clientscrapper.services.evidence import EXCERPT_MAX
from clientscrapper.services.prompts import VERDICT_PROMPT_VERSION, VERDICT_SYSTEM
from clientscrapper.services.pseudonyms import TOKEN_RE, Pseudonymised, pseudonymise
from clientscrapper.services.schemas import GOODS_MAX, RelevanceVerdict
from clientscrapper.services.signals import SignalDraft

log = logging.getLogger(__name__)

OP: Final = "verdict"
BATCH_OP: Final = "company_verdict"
VERDICT_KINDS: Final = (OP, BATCH_OP)
BLOCKS_MAX: Final = 12
BUNDLE_TOKENS_MAX: Final = 6000
BYTES_PER_TOKEN: Final = 2
VERDICT_BATCH: Final = 20
VERDICT_FRESH: Final = timedelta(days=30)
# 04 §4.1: the instant lane's second rule, and the night's selection.
INSTANT_SCORES: Final = (40, 74)
NIGHT_SCORES: Final = (40, 59)
EVIDENCE_MIN: Final = 2
BATCH_AWAY: Final = timedelta(hours=12)
BATCH_AT: Final = time(23, 30)  # the worker's model_batch_submit slot
# The weekly pass over held companies: Sunday night (Monday is 0).
HOLD_WEEKDAY: Final = 6
REVIEW_STATES: Final = ("review",)
INSTANT_STATES: Final = ("enriching", "scored", "review")
NIGHT_STATES: Final = ("enriching", "scored")
HOLD_STATES: Final = ("hold",)
LIVE_COMPANY_STATES: Final = ("new", "active")
OPENER_STATES: Final = ("review", "approved")
# An instant verdict the API failed: waits RETRY_AFTER × 2ⁿ⁻¹, given up for
# the company's evidence after TRIES_MAX (classify's rule).
RETRY_AFTER: Final = timedelta(minutes=15)
TRIES_MAX: Final = 3
CANDIDATES_SCANNED: Final = 200
NO_VALUE: Final = "-"
# One verdict caller per company at a time (the instant lane, a fallback).
VERDICT_LOCK: Final = 0x5645_5244  # "VERD"
SUPERSEDED: Final = "superseded"
# A verdict job that ended so is not asked again on the same evidence: the
# model's refusal, an answer malformed twice, a validator or the effects
# failing (asked again they would fail the same way, at the same price).
FINAL_ERRORS: Final = extraction.FINAL_ERRORS | {model_jobs.APPLY, model_jobs.UNMASKED}
COMPETITOR_FLAG: Final = "flag_competitor"
SUPPLIER_FLAG: Final = "flag_supplier_cn"


# --- the bundle -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Block:
    ref: str
    evidence_id: int
    excerpt: str
    source_key: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class Bundle:
    """One company's question: the masked user text and its reverse map, the
    blocks it shows (``E1`` first), and how many older ones did not fit."""

    user: Pseudonymised
    blocks: tuple[Block, ...]
    dropped: int = 0

    @property
    def evidence_ids(self) -> list[int]:
        return [b.evidence_id for b in self.blocks]

    @property
    def excerpts(self) -> dict[str, str]:
        return {b.ref: b.excerpt for b in self.blocks}


@dataclass(frozen=True, slots=True)
class Found:
    """One evidence row of a company, as a block needs it."""

    evidence_id: int
    source_key: str
    family: str
    url: str
    excerpt: str
    observed_at: datetime
    fetched_at: datetime


def _host(url: str) -> str:
    try:
        host = urlsplit(url).hostname or NO_VALUE
    except ValueError:
        return NO_VALUE
    return host.removeprefix("www.")


def build_bundle(
    *,
    display_name: str,
    legal_form: str | None,
    city: str | None,
    stir: str | None,
    found: Sequence[Found],
    names: Iterable[str] = (),
    handles: Iterable[str] = (),
) -> Bundle:
    """04 §4.4's bundle (the module docstring). Pure: ``found`` is the
    company's evidence, newest first."""
    names, handles = list(names), list(handles)
    head = pseudonymise(
        f"COMPANY: {display_name} | {legal_form or NO_VALUE} | {city or NO_VALUE} | "
        f"STIR: {stir.strip() if stir else NO_VALUE}",
        names=names,
        handles=handles,
    )
    budget = BUNDLE_TOKENS_MAX * BYTES_PER_TOKEN - len(head.text.encode("utf-8"))
    prior = head
    texts: list[str] = []
    blocks: list[Block] = []
    for item in found[:BLOCKS_MAX]:
        ref = f"E{len(blocks) + 1}"
        masked = pseudonymise(item.excerpt, names=names, handles=handles, prior=prior)
        when = item.observed_at.astimezone(settings.tz).date().isoformat()
        text = (
            f"[{ref}] {item.family} {when} {_host(item.url)}\n"
            f"{clip_masked(masked.text, EXCERPT_MAX)}"
        )
        size = len(text.encode("utf-8")) + 2
        if size > budget:
            break
        budget -= size
        prior = masked
        texts.append(text)
        blocks.append(
            Block(ref, item.evidence_id, item.excerpt, item.source_key, item.observed_at)
        )
    dropped = len(found) - len(blocks)
    header = head.text + (f"\nDROPPED: {dropped} older blocks" if dropped else "")
    content = "\n\n".join([header, *texts])
    used = {m.group(0) for m in TOKEN_RE.finditer(content)}
    reverse = {t: v for t, v in prior.reverse.items() if t in used}
    return Bundle(Pseudonymised(content, reverse), tuple(blocks), dropped)


async def _families(session: AsyncSession) -> dict[str, str]:
    rows = await session.execute(sa.select(Source.key, Source.family))
    return {str(k): str(f) for k, f in rows.all()}


async def evidence_of(session: AsyncSession, lineage: Iterable[uuid.UUID]) -> list[Found]:
    """The lineage's evidence — what its observations were parsed from and
    what its live signals stand on — newest first."""
    ids = list(lineage)
    if not ids:
        return []
    via_observations = sa.select(ObservationRow.evidence_id).where(
        ObservationRow.company_id.in_(ids)
    )
    via_signals = sa.select(Signal.evidence_id).where(Signal.company_id.in_(ids))
    observed = sa.func.coalesce(Evidence.published_at, Evidence.fetched_at)
    rows = (
        await session.scalars(
            sa.select(Evidence)
            .where(Evidence.id.in_(via_observations.union(via_signals)))
            .order_by(observed.desc(), Evidence.id.desc())
        )
    ).all()
    families = await _families(session)
    return [
        Found(
            evidence_id=ev.id,
            source_key=ev.source_key,
            family=families.get(ev.source_key, ev.source_key),
            url=ev.url,
            excerpt=ev.excerpt,
            observed_at=ev.published_at or ev.fetched_at,
            fetched_at=ev.fetched_at,
        )
        for ev in rows
        if ev.excerpt and ev.excerpt.strip()
    ]


@dataclass(frozen=True, slots=True)
class People:
    """What masks a company's people: their names and their handles."""

    names: tuple[str, ...] = ()
    handles: tuple[str, ...] = ()


async def people_of(session: AsyncSession, lineage: Iterable[uuid.UUID]) -> People:
    rows = (
        await session.scalars(
            sa.select(Contact).where(
                Contact.company_id.in_(list(lineage)), Contact.anonymised_at.is_(None)
            )
        )
    ).all()
    persons = [c for c in rows if c.tier == "person"]
    names = tuple(c.full_name for c in persons if c.full_name)
    handles = tuple(
        h
        for c in persons
        for h in (c.telegram_username, str(c.telegram_id) if c.telegram_id else None)
        if h
    )
    return People(names, handles)


async def bundle_for(session: AsyncSession, company: Company) -> Bundle | None:
    """The bundle of ``company`` (its lineage's evidence), or None when it has
    none to show."""
    lineage = (await companies.lineages(session, [company.id]))[company.id]
    found = await evidence_of(session, lineage)
    if not found:
        return None
    people = await people_of(session, lineage)
    return build_bundle(
        display_name=company.display_name,
        legal_form=company.legal_form,
        city=company.city,
        stir=company.stir,
        found=found,
        names=people.names,
        handles=people.handles,
    )


# --- the checker ----------------------------------------------------------------------


def checker_for(
    excerpts: Mapping[str, str], reverse: Mapping[str, str], names: Iterable[str]
) -> Callable[[RelevanceVerdict], validators.Validated]:
    names = tuple(names)

    def check(answer: RelevanceVerdict) -> validators.Validated:
        return validators.relevance_verdict(
            answer, blocks=excerpts, reverse=reverse, names=names
        )

    return check


async def _job_excerpts(session: AsyncSession, job: ModelJob) -> dict[str, str]:
    """The blocks a job was asked with, rebuilt from its ``evidence_ids``
    (``E<n>`` = the n-th); a block whose evidence is gone has no excerpt."""
    ids = list(job.evidence_ids or ())
    rows = await session.execute(
        sa.select(Evidence.id, Evidence.excerpt).where(Evidence.id.in_(ids))
    )
    found = {int(i): str(x or "") for i, x in rows.all()}
    return {f"E{n}": found[i] for n, i in enumerate(ids, start=1) if i in found}


async def batch_checker(
    session: AsyncSession, job: ModelJob
) -> Callable[[Any], validators.Validated]:
    """The ``company_verdict`` kind's checker (``model_jobs.BatchKind``)."""
    excerpts = await _job_excerpts(session, job)
    names: tuple[str, ...] = ()
    if job.company_id is not None:
        lineage = (await companies.lineages(session, [job.company_id]))[job.company_id]
        names = (await people_of(session, lineage)).names
    return checker_for(excerpts, dict(job.pseudonyms or {}), names)


# --- applied --------------------------------------------------------------------------


def merged_goods(held: Sequence[str], new: Sequence[str]) -> list[str]:
    """The company's goods first, the verdict's new ones after, ≤ 6."""
    out = list(dict.fromkeys(g for g in held if g))
    for g in new:
        if g and g not in out:
            out.append(g)
    return out[:GOODS_MAX]


async def _evidence(session: AsyncSession, ids: Sequence[int]) -> dict[int, Evidence]:
    rows = (await session.scalars(sa.select(Evidence).where(Evidence.id.in_(ids)))).all()
    return {ev.id: ev for ev in rows}


def _ref_index(ref: str) -> int | None:
    try:
        n = int(ref.removeprefix("E"))
    except ValueError:
        return None
    return n - 1 if n >= 1 else None


def drafts_of(
    result: Mapping[str, Any],
    *,
    evidence_ids: Sequence[int],
    evidence: Mapping[int, Evidence],
    model: str,
    model_job_id: int | None,
) -> list[SignalDraft]:
    """The signals a validated verdict writes (the module docstring). Pure.
    No volume key, ever: the band adds no points."""

    def draft(key: str, evidence_id: int, quote: str | None = None) -> SignalDraft | None:
        ev = evidence.get(evidence_id)
        if ev is None:
            return None
        return SignalDraft(
            key=key,
            evidence_id=ev.id,
            observed_at=ev.published_at or ev.fetched_at,
            source_key=ev.source_key,
            quote=quote,
            model=model,
            model_job_id=model_job_id,
        )

    out: list[SignalDraft | None] = []
    quotes = [q for q in result.get("evidence_quotes") or () if isinstance(q, Mapping)]
    newest = evidence_ids[0] if evidence_ids else None
    china = result.get("imports_from_china")
    if china == "confirmed":
        found = validators.china_quote(quotes)
        index = _ref_index(str(found["ref"])) if found is not None else None
        if found is not None and index is not None and index < len(evidence_ids):
            out.append(draft("cn_origin", evidence_ids[index], str(found["quote"])))
    elif china == "likely" and newest is not None:
        out.append(draft("cn_sector_prior", newest))
    quoted = _ref_index(str(quotes[0]["ref"])) if quotes else None
    anchor = (
        evidence_ids[quoted]
        if quoted is not None and quoted < len(evidence_ids)
        else newest
    )
    if anchor is not None:
        if result.get("is_competitor") is True:
            out.append(draft(COMPETITOR_FLAG, anchor))
        if result.get("is_supplier_cn") is True:
            out.append(draft(SUPPLIER_FLAG, anchor))
    return [d for d in out if d is not None]


async def _set_role(
    session: AsyncSession, lineage: Iterable[uuid.UUID], role: str | None
) -> None:
    """04 §4.4: the verdict's ``decision_maker_role`` on the person-tier
    contact that has none — the decision maker first, then the oldest."""
    if not role:
        return
    contact = await session.scalar(
        sa.select(Contact)
        .where(
            Contact.company_id.in_(list(lineage)),
            Contact.tier == "person",
            Contact.anonymised_at.is_(None),
            Contact.role.is_(None),
        )
        .order_by(Contact.is_decision_maker.desc(), Contact.created_at, Contact.id)
        .limit(1)
        .with_for_update(key_share=True)
    )
    if contact is not None:
        contact.role = role


async def _supersede_pending(
    session: AsyncSession, company_ids: Iterable[uuid.UUID], *, keep: int
) -> int:
    """The company's ``company_verdict`` still waiting for its night is no
    longer asked: an instant verdict answered it (``failed`` /
    ``superseded`` — never sent, nothing billed)."""
    result = await session.execute(
        sa.update(ModelJob)
        .where(
            ModelJob.lane == "batch",
            ModelJob.kind == BATCH_OP,
            ModelJob.status == "pending",
            ModelJob.company_id.in_(list(company_ids)),
            ModelJob.id != keep,
        )
        .values(status="failed", error=SUPERSEDED)
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def apply_verdict(
    session: AsyncSession, landed: model_jobs.Landed, now: datetime
) -> None:
    """A validated verdict's effects (the module docstring) in the caller's
    transaction: the company that holds the asked company's rows now (a merge
    since the question was asked moves the answer to the root)."""
    if landed.company_id is None:
        return
    asked = await session.get(Company, landed.company_id)
    if asked is None:
        return
    root = await companies.root_company(session, asked)
    company = await session.get(
        Company, root.id, with_for_update={"key_share": True}, populate_existing=True
    )
    if company is None or company.state == "purged":
        return
    result = dict(landed.validated.result)
    company.verdict = result
    company.verdict_at = now
    segment = result.get("segment")
    if isinstance(segment, str) and len(segment) == 1:
        company.segment = segment
    band = result.get("est_volume_band")
    if isinstance(band, str) and band != "unknown":
        company.volume_band = band
    company.goods = merged_goods(company.goods or (), result.get("goods") or ())
    if result.get("is_competitor") is True:
        company.is_competitor = True
    if result.get("is_supplier_cn") is True:
        company.is_supplier_cn = True
    if result.get("is_state_body") is True:
        company.is_state_body = True
    await session.flush()
    lineage = (await companies.lineages(session, [company.id]))[company.id]
    await _set_role(session, lineage, result.get("decision_maker_role"))
    ids = list(landed.evidence_ids)
    drafts = drafts_of(
        result,
        evidence_ids=ids,
        evidence=await _evidence(session, ids),
        model=landed.model,
        model_job_id=landed.job_id,
    )
    try:
        weights = await scoring.load_weights(session)
    except LookupError:
        weights = scoring.weights_v1()
    if drafts:
        await signals.emit(session, company.id, drafts, weights=weights)
    else:
        await signals.mark_stale(session, company.id)
    if landed.lane == "instant":
        await _supersede_pending(session, lineage, keep=landed.batch_job_id)
    log.info(
        "verdict applied",
        extra={
            "model_job_id": landed.job_id,
            "lane": landed.lane,
            "signals": [d.key for d in drafts],
        },
    )


# --- when a company is due ------------------------------------------------------------


def next_batch_at(now: datetime) -> datetime:
    """The next 23:30 Tashkent (the night's batch, 04 §0)."""
    local = now.astimezone(settings.tz)
    at = local.replace(
        hour=BATCH_AT.hour, minute=BATCH_AT.minute, second=0, microsecond=0
    )
    return at if at > local else at + timedelta(days=1)


def batch_far(now: datetime) -> bool:
    """Is the night's batch more than ``BATCH_AWAY`` off (04 §4.1)?"""
    return next_batch_at(now) - now > BATCH_AWAY


@dataclass(frozen=True, slots=True)
class History:
    """A company's verdict jobs since its newest evidence: a final failure
    (the model refused, an answer malformed twice), the API failures and
    the last of them, a job in flight."""

    final: bool = False
    refused: bool = False
    api_failures: int = 0
    last_failure: datetime | None = None
    in_flight: bool = False

    def waits_until(self) -> datetime | None:
        if not self.api_failures or self.last_failure is None:
            return None
        return self.last_failure + RETRY_AFTER * (1 << (self.api_failures - 1))


async def history(
    session: AsyncSession, lineage: Iterable[uuid.UUID], since: datetime | None
) -> History:
    ids = list(lineage)
    where: list[sa.ColumnElement[bool]] = [
        ModelJob.company_id.in_(ids),
        ModelJob.kind.in_(VERDICT_KINDS),
    ]
    if since is not None:
        where.append(ModelJob.created_at >= since)
    rows = (await session.scalars(sa.select(ModelJob).where(*where))).all()
    final = refused = False
    api = 0
    last: datetime | None = None
    flight = False
    for job in rows:
        error = job.error or ""
        if job.status == "failed":
            if error == extraction.REFUSAL:
                refused = final = True
            elif error in FINAL_ERRORS:
                final = True
            elif error.startswith(extraction.API_PREFIX):
                api += 1
                last = max(last, job.created_at) if last else job.created_at
        elif job.status == "submitted" or (
            job.status == "fallback" and job.applied_at is None
        ):
            flight = True
    return History(final, refused, api, last, flight)


@dataclass(frozen=True, slots=True)
class Due:
    candidate_id: uuid.UUID
    company_id: uuid.UUID


async def _newest_evidence(
    session: AsyncSession, lineage: Iterable[uuid.UUID]
) -> tuple[datetime | None, int]:
    found = await evidence_of(session, lineage)
    if not found:
        return None, 0
    return max(f.fetched_at for f in found), len(found)


def _stale(verdict_at: datetime | None, newest: datetime | None) -> bool:
    return newest is not None and (verdict_at is None or verdict_at < newest)


async def due_instant(
    session: AsyncSession,
    *,
    now: datetime,
    skip: Collection[uuid.UUID] = (),
    limit: int = VERDICT_BATCH,
) -> list[Due]:
    """The instant lane's queue (04 §4.1), best score first, at most
    ``limit``."""
    fresh_before = now - VERDICT_FRESH
    review = sa.and_(
        LeadCandidate.state.in_(REVIEW_STATES),
        sa.or_(Company.verdict_at.is_(None), Company.verdict_at < fresh_before),
    )
    rules: list[sa.ColumnElement[bool]] = [review]
    far = batch_far(now)
    if far:
        rules.append(
            sa.and_(
                LeadCandidate.state.in_(INSTANT_STATES),
                LeadCandidate.score.between(*INSTANT_SCORES),
            )
        )
    rows = (
        await session.execute(
            sa.select(LeadCandidate, Company)
            .join(Company, Company.id == LeadCandidate.company_id)
            .where(Company.state.in_(LIVE_COMPANY_STATES), sa.or_(*rules))
            .order_by(LeadCandidate.score.desc().nulls_last(), LeadCandidate.id)
            .limit(CANDIDATES_SCANNED)
        )
    ).all()
    out: list[Due] = []
    for candidate, company in rows:
        if company.id in skip:
            continue
        lineage = (await companies.lineages(session, [company.id]))[company.id]
        newest, count = await _newest_evidence(session, lineage)
        if newest is None:
            continue
        in_review = candidate.state in REVIEW_STATES and (
            company.verdict_at is None or company.verdict_at < fresh_before
        )
        second = (
            far
            and candidate.state in INSTANT_STATES
            and candidate.score is not None
            and INSTANT_SCORES[0] <= candidate.score <= INSTANT_SCORES[1]
            and count >= EVIDENCE_MIN
            and _stale(company.verdict_at, newest)
        )
        if not (in_review or second):
            continue
        past = await history(session, lineage, since=newest)
        if past.final or past.api_failures >= TRIES_MAX or past.in_flight:
            continue
        due = past.waits_until()
        if due is not None and now < due:
            continue
        out.append(Due(candidate.id, company.id))
        if len(out) >= limit:
            break
    return out


# --- the instant lane -----------------------------------------------------------------


@dataclass(slots=True)
class VerdictReport:
    claimed: int = 0
    applied: int = 0
    failed: int = 0
    retried: int = 0
    refused: int = 0
    skipped: int = 0
    openers: int = 0
    opener_errors: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "applied": self.applied,
            "failed": self.failed,
            "retried": self.retried,
            "refused": self.refused,
            "skipped": self.skipped,
            "openers": self.openers,
            "opener_errors": self.opener_errors,
        }


class _Refused(Exception):
    """Nothing was sent: undo the job row, stop the run."""


async def _try_lock(session: AsyncSession, company_id: uuid.UUID) -> bool:
    got = await session.scalar(
        sa.select(
            sa.func.pg_try_advisory_xact_lock(
                VERDICT_LOCK, sa.func.hashtext(str(company_id))
            )
        )
    )
    return bool(got)


async def ask_instant(
    session: AsyncSession, due: Due, *, now: datetime, report: VerdictReport
) -> str:
    """One instant verdict in the caller's transaction: ``done``, ``waits``
    (another caller holds the company, no bundle), or ``stop`` (the API
    failed: the next company would meet the same outage). Raises
    ``_Refused`` when nothing was sent."""
    if not await _try_lock(session, due.company_id):
        report.skipped += 1
        return "waits"
    company = await session.get(Company, due.company_id, populate_existing=True)
    if company is None:
        return "waits"
    bundle = await bundle_for(session, company)
    if bundle is None:
        report.skipped += 1
        return "waits"
    lineage = (await companies.lineages(session, [company.id]))[company.id]
    people = await people_of(session, lineage)
    result = await extraction.call(
        session,
        op=OP,
        schema=RelevanceVerdict,
        system=VERDICT_SYSTEM,
        user=bundle.user,
        now=now,
        company_id=company.id,
        evidence_ids=bundle.evidence_ids,
        prompt_version=VERDICT_PROMPT_VERSION,
        validate=checker_for(bundle.excerpts, bundle.user.reverse, people.names),
    )
    if result.refused:
        raise _Refused(result.error or "refused")
    if not result.final:
        report.retried += 1
        return (
            "stop" if (result.error or "").startswith(extraction.API_PREFIX) else "waits"
        )
    if not result.ok or result.validated is None or result.job_id is None:
        report.failed += 1
        return "done"
    landed = model_jobs.Landed(
        job_id=result.job_id,
        batch_job_id=result.job_id,
        lane="instant",
        model=result.model,
        company_id=company.id,
        evidence_ids=tuple(bundle.evidence_ids),
        validated=result.validated,
    )
    try:
        async with session.begin_nested():
            await apply_verdict(session, landed, now)
    except Exception as exc:
        log.error(
            "verdict effects failed",
            extra={"model_job_id": result.job_id, "error": type(exc).__name__},
        )
        await session.execute(
            sa.update(ModelJob)
            .where(ModelJob.id == result.job_id)
            .values(status="failed", error=model_jobs.APPLY)
        )
        report.failed += 1
        return "done"
    report.applied += 1
    return "done"


SessionFactory = Callable[[], AsyncSession]
Clock = Callable[[], datetime]


async def verdict_batch(
    session_factory: SessionFactory = SessionLocal,
    *,
    now: datetime,
    clock: Clock | None = None,
    limit: int = VERDICT_BATCH,
) -> VerdictReport:
    """The instant lane's run (the module docstring), each company in a
    transaction of its own at ``clock()`` (default ``now``)."""
    report = VerdictReport()
    if costs.refused_until(OP, now) is not None or not extraction.available():
        report.refused += 1
        return report
    async with session_factory() as session:
        queue = await due_instant(session, now=now, limit=limit)
    for due in queue:
        at = max(clock(), now) if clock is not None else now
        outcome = "done"
        try:
            async with session_factory() as session, session.begin():
                report.claimed += 1
                outcome = await ask_instant(session, due, now=at, report=report)
        except _Refused as refused:
            report.refused += 1
            log.info("verdict stopped: refused", extra={"code": str(refused)})
            break
        except Exception as exc:
            report.errors.append(type(exc).__name__)
            log.error(
                "verdict not asked",
                extra={"company_id": str(due.company_id), "error": type(exc).__name__},
            )
            continue
        if outcome == "stop":
            log.warning("verdict stopped: the API failed")
            break
    return report


async def ensure_openers(
    session_factory: SessionFactory = SessionLocal,
    *,
    now: datetime,
    limit: int = VERDICT_BATCH,
    report: VerdictReport | None = None,
) -> VerdictReport:
    """Each ``review``/``approved`` candidate without an opener gets one
    (``openers.ensure_opener``), one transaction each, best score first."""
    report = report or VerdictReport()
    async with session_factory() as session:
        ids = list(
            await session.scalars(
                sa.select(LeadCandidate.id)
                .where(
                    LeadCandidate.state.in_(OPENER_STATES),
                    sa.or_(
                        LeadCandidate.score_breakdown.is_(None),
                        sa.not_(
                            LeadCandidate.score_breakdown.has_key(openers.OPENER_KEY)
                        ),
                    ),
                )
                .order_by(LeadCandidate.score.desc().nulls_last(), LeadCandidate.id)
                .limit(limit)
            )
        )
    for candidate_id in ids:
        try:
            async with session_factory() as session, session.begin():
                if await openers.ensure_opener(session, candidate_id, now=now):
                    report.openers += 1
        except Exception as exc:
            report.opener_errors += 1
            log.error(
                "opener not made",
                extra={"candidate_id": str(candidate_id), "error": type(exc).__name__},
            )
    return report


async def verdict_job(*, now: datetime | None = None) -> dict[str, int]:
    """The worker's ``verdict`` slot (every 10 minutes, 04 §0): the instant
    verdicts due, each at the wall clock of its turn, then the openers.
    Never the batch lane's fallbacks (they are the 23:30 run's). Counts only
    are logged (06 §4.5)."""

    def wall() -> datetime:
        return datetime.now(settings.tz)

    start = now or wall()
    report = await verdict_batch(now=start, clock=None if now else wall)
    await ensure_openers(now=start, report=report)
    counts = report.as_dict()
    if any(counts.values()):
        log.info("verdict_job", extra={"counts": counts})
    return counts


# --- the night ------------------------------------------------------------------------


async def _waiting_jobs(session: AsyncSession) -> int:
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ModelJob)
        .where(
            ModelJob.lane == "batch",
            ModelJob.kind == BATCH_OP,
            ModelJob.status == "pending",
        )
    )
    return int(count or 0)


async def due_nightly(session: AsyncSession, *, now: datetime) -> list[uuid.UUID]:
    """The night's companies (the module docstring), best score first."""
    rules: list[sa.ColumnElement[bool]] = [
        sa.and_(
            LeadCandidate.state.in_(NIGHT_STATES),
            LeadCandidate.score.between(*NIGHT_SCORES),
        )
    ]
    if now.astimezone(settings.tz).weekday() == HOLD_WEEKDAY:
        rules.append(LeadCandidate.state.in_(HOLD_STATES))
    rows = (
        await session.execute(
            sa.select(LeadCandidate.company_id, Company.verdict_at)
            .join(Company, Company.id == LeadCandidate.company_id)
            .where(Company.state.in_(LIVE_COMPANY_STATES), sa.or_(*rules))
            .order_by(LeadCandidate.score.desc().nulls_last(), LeadCandidate.id)
        )
    ).all()
    out: list[uuid.UUID] = []
    for company_id, verdict_at in rows:
        if company_id in out:
            continue
        lineage = (await companies.lineages(session, [company_id]))[company_id]
        newest, _ = await _newest_evidence(session, lineage)
        if not _stale(verdict_at, newest):
            continue
        past = await history(session, lineage, since=newest)
        if past.refused or past.in_flight:
            continue
        out.append(company_id)
    return out


async def select_company_verdicts(
    session_factory: SessionFactory = SessionLocal, now: datetime | None = None
) -> int:
    """The ``company_verdict`` kind's night selection: each due company's
    bundle enqueued (``model_jobs.enqueue``: a company with a job waiting has
    it asked the newest bundle), one transaction each, while fewer than
    ``model_jobs.BATCH_MAX`` wait. Returns how many were enqueued."""
    at = now or datetime.now(settings.tz)
    async with session_factory() as session:
        room = model_jobs.BATCH_MAX - await _waiting_jobs(session)
        if room <= 0:
            return 0
        chosen = (await due_nightly(session, now=at))[:room]
    queued = 0
    for company_id in chosen:
        try:
            async with session_factory() as session, session.begin():
                company = await session.get(Company, company_id)
                if company is None:
                    continue
                bundle = await bundle_for(session, company)
                if bundle is None:
                    continue
                await model_jobs.enqueue(
                    session,
                    user=bundle.user,
                    now=at,
                    op=BATCH_OP,
                    company_id=company.id,
                    evidence_ids=bundle.evidence_ids,
                )
                queued += 1
        except Exception as exc:
            log.error(
                "company verdict not enqueued",
                extra={"company_id": str(company_id), "error": type(exc).__name__},
            )
    log.info("company verdicts selected", extra={"queued": queued})
    return queued


# --- the card -------------------------------------------------------------------------


async def refused_today(
    session: AsyncSession, company_ids: Iterable[uuid.UUID], *, now: datetime
) -> bool:
    """Was a verdict for these companies refused by a cap today (04 §8: the
    card says «AI xulosasi: byudjet tugadi»)?"""
    rows = await session.scalars(
        sa.select(UsageLog.model).where(
            UsageLog.operation.in_(VERDICT_KINDS),
            UsageLog.company_id.in_(list(company_ids)),
            UsageLog.created_at >= costs.day_start(now),
            UsageLog.model.startswith(costs.REFUSED_PREFIX, autoescape=True),
        )
    )
    return any(costs.is_cap(str(m)[len(costs.REFUSED_PREFIX) :]) for m in rows)


# --- registration ---------------------------------------------------------------------

KIND: Final = model_jobs.BatchKind(
    schema=RelevanceVerdict,
    system=VERDICT_SYSTEM,
    prompt_version=VERDICT_PROMPT_VERSION,
    checker=batch_checker,
    apply=apply_verdict,
    select=select_company_verdicts,
)
model_jobs.register(BATCH_OP, KIND)


__all__ = [
    "BATCH_OP",
    "BLOCKS_MAX",
    "BUNDLE_TOKENS_MAX",
    "KIND",
    "OP",
    "VERDICT_FRESH",
    "Block",
    "Bundle",
    "Found",
    "VerdictReport",
    "apply_verdict",
    "batch_checker",
    "batch_far",
    "build_bundle",
    "bundle_for",
    "checker_for",
    "drafts_of",
    "due_instant",
    "due_nightly",
    "ensure_openers",
    "evidence_of",
    "merged_goods",
    "next_batch_at",
    "refused_today",
    "select_company_verdicts",
    "verdict_batch",
    "verdict_job",
]
