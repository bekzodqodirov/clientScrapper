# VENDORED_FROM dailyJournal@6b1f67f miya/services/batch.py
"""The ``model_jobs`` lifecycle and the batch lane (04 §4.8, 02 §2.6).

Adapted from MIYA ``services/batch.py`` (memo §1: ADAPT): the submit, the
collect that drains the stream before it touches the database and keys every
result by ``custom_id``, and the retry-or-fallback ladder (an errored or
expired request back to pending, the instant lane after the third attempt)
are MIYA's; generalised from conversation windows to ``model_jobs``, with
every submit one ``costs.ask`` and every result priced in place, one
transaction per job, and the effects a registered kind's.

A model job is one model call and its record. The **instant lane** is
``extraction.call`` (T-15): ``pending`` → the call → ``applied`` or ``failed``
in the caller's transaction with its effects, the money claimed first by
``costs.ask`` in a transaction of its own. The **batch lane** — the Message
Batches API at half price, for the operations ``extraction.ROUTES`` sends
there (``company_verdict``) — is this module:

1. ``enqueue`` — a ``pending`` job: the masked user text and its reverse map
   (T-30's nightly selection calls it);
2. ``submit_batch`` (``model_batch_submit``, 23:30) — the oldest pending jobs
   of a batch operation, at most ``BATCH_MAX``, as many as the operation's
   budget holds (``costs.fits``, a hint); one ``costs.ask`` for the whole
   batch at the sum of the per-job estimates, which writes one
   **provisional** ``usage_log`` row per job at its own estimate; then
   ``messages.batches.create`` with one ``{custom_id, params}`` per job, built
   by the instant lane's own ``extraction.build_params`` (04 §4.4: one
   builder, comparable answers); each job stamped ``batch_id``,
   ``submitted``, ``submitted_at`` and ``attempts + 1``;
3. ``collect`` (``model_batch_collect``, every 30 minutes 00:30–08:00) — each
   batch holding submitted jobs: ``batches.retrieve`` until
   ``processing_status == 'ended'``, the result stream drained before the
   database is touched, then each job in a transaction of its own, found by
   its ``custom_id`` — never by position: a ``succeeded`` result settles the
   job's provisional row **in place** with the real tokens and price
   (``costs.settle_provisional``) and goes through the kind's validators —
   ``applied``, with its effects; the model's refusal is ``failed`` /
   ``refusal`` (no retry, no other model: cost stays predictable, 04 §4.1);
   a validator's bug ``failed`` / ``validator``; the rest takes the ladder;
4. **the ladder** — an ``errored``, ``expired`` or ``canceled`` result, a
   request the stream left out, an answer that does not validate or ran out
   of tokens, a batch the API refused to take, a batch given up as stuck:
   fewer than ``ATTEMPTS_MAX`` submissions so far → ``pending`` for the next
   night; ``ATTEMPTS_MAX`` → ``fallback``, the batch lane's last word, and the
   instant lane takes the job;
5. ``take_fallbacks`` — each fallback job not yet taken goes through
   ``extraction.call`` on the instant operation of ``FALLBACK_OP`` (the
   verdict model, within its own cap), its request naming the batch job
   (``fallback_of``). A budget refusal sends and keeps nothing: the job is
   taken once the cap has room. An API failure keeps its record and stops
   the run; the job is given up after ``FALLBACK_TRIES`` of them. A final
   answer — applied with its effects, or a final failure — marks the
   fallback job taken: its ``applied_at``, the moment it was done with
   (retention reads it, 02 §4 R6);
6. a batch whose jobs still wait ``submitted`` ``health.BATCH_STUCK_AFTER``
   (26 h) after it was sent is ``batch_stuck`` (06 §4.2 — the health sweep
   says it from the table, ``health.stuck_batches``). The next night's submit
   reads such a batch once more and collects it if it has ended; otherwise
   it gives it up: each job one failed attempt (``stuck``) on the ladder, and
   the batch cancelled (best effort; a cancel costs nothing).

**Money.** The provisional rows count the estimate until a result settles
them; ``costs.ask`` never undercounts (04 §8). A result other than
``succeeded`` carries no usage, so its row keeps the estimate — UNVERIFIED
whether Anthropic bills such a request at all (a day-1 check, 07 §5.5);
keeping it overcounts, the safe direction. A job's ``cost_usd`` is what its
rows hold, over every attempt.

**Kinds.** A batch operation is asked and applied by what its task
registers (``register``): the schema, the frozen system prompt, the checker
that holds an answer against the job's evidence, and the effects — T-30
registers ``company_verdict`` with ``RelevanceVerdict``. Nothing is submitted
for an operation nobody registered, and both lanes of a job use the same
checker and effects.

Every function takes ``now``; the jobs read the wall clock. The claims are
``FOR NO KEY UPDATE SKIP LOCKED``: the provisional rows' foreign key on the
job (``FOR KEY SHARE``) does not wait on it, so ``costs.ask``'s own
transaction attributes each row while the batch's jobs are held.
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Literal, cast

import anthropic
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import ModelJob, UsageLog
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import costs, extraction, health
from clientscrapper.services.pseudonyms import (
    Pseudonymised,
    UnmaskedPayload,
    assert_masked,
)
from clientscrapper.services.usage import Operation, Priced, priced_anthropic
from clientscrapper.services.validators import PII_LEAK, Validated

log = logging.getLogger(__name__)

Lane = Literal["instant", "batch"]

# The operations the batch lane carries (04 §4.1: company_verdict).
BATCH_ROUTES: Final[Mapping[Operation, extraction.Route]] = {
    op: route for op, route in extraction.ROUTES.items() if route.lane == "batch"
}
# 04 §4.8: «the instant lane (Sonnet) takes it within its own cap» — the
# verdict operation, whose model is VERDICT_MODEL and whose cap is its own.
FALLBACK_OP: Final[Mapping[Operation, Operation]] = {"company_verdict": "verdict"}
# 04 §4.1 / §4.8: ≤ 100 company verdicts a night.
BATCH_MAX: Final = 100
# 04 §4.8: three submissions, then the instant lane — the bound of
# model_jobs.attempts' CHECK (02 §2.6).
ATTEMPTS_MAX: Final = extraction.ATTEMPTS_MAX
# An instant fallback whose call the API failed this many times is given up:
# the company's next nightly selection asks again (T-30).
FALLBACK_TRIES: Final = 3
# How many fallback jobs one run hands to the instant lane.
FALLBACK_MAX: Final = BATCH_MAX
ENDED: Final = "ended"
# The key of an instant job's request that names the batch job it took.
FALLBACK_OF: Final = "fallback_of"

# model_jobs.error of the batch lane (the instant lane's are extraction's).
ERRORED: Final = "errored"  # + ":<the API's error type>"
EXPIRED: Final = "expired"
CANCELED: Final = "canceled"
MISSING: Final = "missing"  # the stream had no line for the custom_id
UNAVAILABLE: Final = "unavailable"  # the batch ended and has no results
STUCK: Final = "stuck"
SUBMIT_PREFIX: Final = "submit:"  # + the status the create was refused with
UNMASKED: Final = "unmasked"
UNREGISTERED: Final = "unregistered"
APPLY: Final = "apply"
# A batch create answered with one of these refused the request itself (its
# body, its size): sent again unchanged it is refused again, so it costs each
# job an attempt. Any other failure (the network, a 5xx, 429, the key) leaves
# the jobs pending for the next night as they were.
REQUEST_REFUSED: Final = frozenset({400, 413, 422})


# --- kinds ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Landed:
    """One validated answer, for its kind's effects. ``job_id`` is the job
    that answered (the batch job, or the instant job that took a fallback);
    ``batch_job_id`` the batch-lane job the answer is for."""

    job_id: int
    batch_job_id: int
    lane: Lane
    model: str
    company_id: uuid.UUID | None
    evidence_ids: tuple[int, ...]
    validated: Validated


Checker = Callable[[Any], Validated]
MakeChecker = Callable[[AsyncSession, ModelJob], Awaitable[Checker]]
Effects = Callable[[AsyncSession, Landed, datetime], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class BatchKind:
    """How a batch operation is asked and applied. ``checker(session, job)``
    reads what the validators need (the evidence excerpts) and returns the
    answer → ``Validated`` function both lanes use; None stores the answer as
    it validated against the schema. ``apply(session, landed, now)`` writes
    the effects in the job's transaction."""

    schema: type[BaseModel]
    system: str
    prompt_version: int | None = None
    checker: MakeChecker | None = None
    apply: Effects | None = None


KINDS: dict[Operation, BatchKind] = {}


def register(op: Operation, kind: BatchKind) -> None:
    """``op``'s schema, prompt, checker and effects (its task registers
    them). Only a batch operation with an instant fallback can be
    registered."""
    if op not in BATCH_ROUTES or op not in FALLBACK_OP:
        raise ValueError(f"{op} is not a batch operation")
    KINDS[op] = kind


def _op_of(job: ModelJob) -> Operation:
    """The batch operation of a batch-lane job (its kind names it)."""
    for op, route in BATCH_ROUTES.items():
        if route.kind == job.kind:
            return op
    raise ValueError(f"{job.kind} is not a batch kind")


def _default_checker(output: BaseModel) -> Validated:
    return Validated(result=output.model_dump(mode="json"))


async def _checker(session: AsyncSession, job: ModelJob, kind: BatchKind) -> Checker:
    if kind.checker is None:
        return _default_checker
    return await kind.checker(session, job)


def _user(job: ModelJob) -> Pseudonymised:
    """The job's masked user text and its reverse map, as enqueued."""
    request = job.request or {}
    return Pseudonymised(str(request.get("user") or ""), dict(job.pseudonyms or {}))


def _record(
    op: Operation, kind: BatchKind, *, model: str, user: Pseudonymised
) -> dict[str, Any]:
    route = BATCH_ROUTES[op]
    return extraction.request_record(
        op,
        model=model,
        system=kind.system,
        schema=kind.schema,
        user=user,
        max_tokens=route.max_tokens,
        effort=route.effort,
        prompt_version=kind.prompt_version,
        tools=None,
    )


async def _spent(session: AsyncSession, job_id: int) -> Decimal:
    """What the job's ``usage_log`` rows hold, every attempt together."""
    total = await session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(UsageLog.cost_usd), 0)).where(
            UsageLog.model_job_id == job_id
        )
    )
    return Decimal(str(total or 0))


# --- 1. enqueue ------------------------------------------------------------------


async def enqueue(
    session: AsyncSession,
    *,
    user: Pseudonymised,
    now: datetime,
    op: Operation = "company_verdict",
    company_id: uuid.UUID | None = None,
    evidence_ids: Sequence[int] = (),
) -> ModelJob:
    """A batch-lane job, ``pending`` until the night's submit, in the
    caller's transaction. ``user`` is masked text (``pseudonymise``): raises
    ``UnmaskedPayload`` before anything is written, as the instant lane does,
    and ``ValueError`` for an operation nobody registered."""
    if not isinstance(user, Pseudonymised):
        raise TypeError("user must be a Pseudonymised text (06 §5.2)")
    kind = KINDS.get(op)
    if op not in BATCH_ROUTES or kind is None:
        raise ValueError(f"{op} is not a registered batch operation")
    assert_masked(kind.system)
    assert_masked(user.text)
    model = extraction.model_for(op)
    job = ModelJob(
        kind=BATCH_ROUTES[op].kind,
        lane="batch",
        model=model,
        custom_id=extraction.new_custom_id(),
        status="pending",
        attempts=0,
        company_id=company_id,
        evidence_ids=list(evidence_ids),
        request=_record(op, kind, model=model, user=user),
        pseudonyms=dict(user.reverse) or None,
        created_at=now,
    )
    session.add(job)
    await session.flush()
    return job


# --- 4. the ladder ----------------------------------------------------------------


@dataclass(slots=True)
class LaneReport:
    """Counts of one run of the lane (logged; no text, no ids)."""

    claimed: int = 0
    submitted: int = 0
    held: int = 0
    refused: int = 0
    not_sent: int = 0
    unmasked: int = 0
    batches: int = 0
    running: int = 0
    unreadable: int = 0
    applied: int = 0
    failed: int = 0
    retried: int = 0
    fallback: int = 0
    stuck: int = 0
    taken: int = 0
    given_up: int = 0
    waiting: int = 0
    batch_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            f.name: getattr(self, f.name) for f in fields(self) if f.name != "batch_ids"
        }


async def _ladder(
    session: AsyncSession, job: ModelJob, reason: str, report: LaneReport
) -> None:
    """One submission that brought no answer: ``pending`` for the next night
    while ``attempts`` < ``ATTEMPTS_MAX``, else ``fallback`` (the instant lane
    takes it). The fallback keeps the last batch it was in."""
    job.error = reason
    if job.attempts < ATTEMPTS_MAX:
        job.status = "pending"
        job.batch_id = None
        job.submitted_at = None
        report.retried += 1
    else:
        job.status = "fallback"
        report.fallback += 1
    await session.flush()
    log.info(
        "batch job back on the ladder",
        extra={
            "model_job_id": job.id,
            "reason": reason,
            "attempts": job.attempts,
            "status": job.status,
        },
    )


async def _fail(
    session: AsyncSession, job: ModelJob, error: str, report: LaneReport
) -> None:
    """A final answer that is no answer: never asked again."""
    job.status = "failed"
    job.error = error
    await session.flush()
    report.failed += 1


async def _effects(
    session: AsyncSession, kind: BatchKind, landed: Landed, now: datetime
) -> bool:
    """The kind's effects in a savepoint of the job's transaction; False when
    they raised (the savepoint is gone, the answer stays recorded)."""
    if kind.apply is None:
        return True
    try:
        async with session.begin_nested():
            await kind.apply(session, landed, now)
    except Exception as exc:
        log.error(
            "model job effects failed",
            extra={"model_job_id": landed.job_id, "error": type(exc).__name__},
        )
        return False
    return True


# --- 2. submit -------------------------------------------------------------------


async def _create_batch(
    client: anthropic.AsyncAnthropic, requests: list[dict[str, Any]]
) -> Any:
    """The batch create itself: reached only through ``costs.ask``'s
    ``call=``."""
    return await client.messages.batches.create(requests=cast(Any, requests))


async def _claim_pending(
    session: AsyncSession, op: Operation, limit: int
) -> list[ModelJob]:
    rows = await session.scalars(
        sa.select(ModelJob)
        .where(
            ModelJob.lane == "batch",
            ModelJob.kind == BATCH_ROUTES[op].kind,
            ModelJob.status == "pending",
        )
        .order_by(ModelJob.created_at, ModelJob.id)
        .limit(limit)
        .with_for_update(key_share=True, skip_locked=True)
    )
    return list(rows)


@dataclass(frozen=True, slots=True)
class _Planned:
    job: ModelJob
    params: dict[str, Any]
    user: Pseudonymised
    est: Decimal | None


async def _submit_op(
    session: AsyncSession,
    op: Operation,
    kind: BatchKind,
    *,
    now: datetime,
    limit: int,
    report: LaneReport,
) -> None:
    """One batch of ``op``'s pending jobs, in the caller's transaction."""
    jobs = await _claim_pending(session, op, limit)
    report.claimed += len(jobs)
    if not jobs:
        return
    route = BATCH_ROUTES[op]
    model = extraction.model_for(op)
    structured = extraction.structured_output_available(kind.schema)
    planned: list[_Planned] = []
    for job in jobs:
        if job.attempts >= ATTEMPTS_MAX:
            # Never submitted again (the CHECK caps attempts at three).
            await _ladder(session, job, job.error or STUCK, report)
            continue
        user = _user(job)
        try:
            assert_masked(kind.system)
            assert_masked(user.text)
        except UnmaskedPayload as exc:
            log.error(
                "a batch job's request is not masked; never sent",
                extra={"model_job_id": job.id, "pattern": exc.pattern},
            )
            job.status, job.error = "failed", UNMASKED
            await session.flush()
            report.unmasked += 1
            continue
        params = extraction.build_params(
            model=model,
            system=kind.system,
            schema=kind.schema,
            messages=[{"role": "user", "content": user.text}],
            max_tokens=route.max_tokens,
            effort=route.effort,
            structured=structured,
        )
        est = extraction.estimate_usd(
            model,
            prompt_bytes=extraction.request_bytes(params),
            max_tokens=route.max_tokens,
            batch=True,
        )
        planned.append(_Planned(job, params, user, est))
    if not planned:
        return
    fitting = await costs.fits(
        session, operation=op, estimates=[p.est for p in planned], now=now
    )
    # Nothing fits: ask for the first anyway, so the refusal is ask's own —
    # recorded, and the cap told (04 §8).
    chosen, held = planned[: max(fitting, 1)], planned[max(fitting, 1) :]
    report.held += len(held)
    estimates = [p.est for p in chosen]
    total = (
        None
        if any(e is None for e in estimates)
        else sum((e for e in estimates if e is not None), Decimal(0))
    )
    requests = [{"custom_id": p.job.custom_id, "params": p.params} for p in chosen]

    def provisional(_: Any) -> list[Priced]:
        return [
            Priced(
                p.est,
                model=model,
                batch=True,
                model_job_id=p.job.id,
                company_id=p.job.company_id,
            )
            for p in chosen
        ]

    client = extraction.get_client()
    try:
        batch = await costs.ask(
            session,
            operation=op,
            est_usd=total,
            call=functools.partial(_create_batch, client, requests),
            price=provisional,
            now=now,
        )
    except costs.CostCapReached as exc:
        report.refused += 1
        log.info("batch not submitted: refused", extra={"op": op, "code": exc.code})
        return
    except extraction.API_FAILURES as exc:
        status = getattr(exc, "status_code", None)
        log.warning(
            "batch submit failed",
            extra={"op": op, "error": type(exc).__name__, "status": status},
        )
        if extraction.is_grammar_failure(exc):
            extraction.note_grammar_failure(kind.schema, exc)
        if status not in REQUEST_REFUSED:
            report.not_sent += len(chosen)
            return
        for p in chosen:
            p.job.attempts += 1
            p.job.cost_usd = await _spent(session, p.job.id)
            await _ladder(session, p.job, f"{SUBMIT_PREFIX}{status}", report)
        return
    batch_id = str(batch.id)
    for p in chosen:
        job = p.job
        job.model = model
        job.request = _record(op, kind, model=model, user=p.user)
        job.batch_id = batch_id
        job.status = "submitted"
        job.submitted_at = now
        job.attempts += 1
        job.cost_usd = await _spent(session, job.id)
    await session.flush()
    report.submitted += len(chosen)
    report.batch_ids.append(batch_id)
    log.info(
        "batch submitted",
        extra={"op": op, "batch_id": batch_id, "jobs": len(chosen), "held": len(held)},
    )


SessionFactory = Callable[[], AsyncSession]
Clock = Callable[[], datetime]


async def submit_batch(
    session_factory: SessionFactory = SessionLocal,
    *,
    now: datetime,
    limit: int = BATCH_MAX,
    report: LaneReport | None = None,
) -> LaneReport:
    """The night's batch of each registered batch operation (step 2): one
    transaction each, committed after the create — or after a refusal, which
    leaves the jobs pending. Nothing is claimed while ``costs.ask`` still
    remembers refusing the operation, or with no client to call."""
    report = report or LaneReport()
    for op, kind in list(KINDS.items()):
        if costs.refused_until(op, now) is not None or not extraction.available():
            report.refused += 1
            continue
        async with session_factory() as session, session.begin():
            await _submit_op(session, op, kind, now=now, limit=limit, report=report)
    return report


# --- 3. collect -------------------------------------------------------------------


async def _open_batches(session_factory: SessionFactory) -> list[str]:
    """Every batch holding a submitted job, the oldest first."""
    async with session_factory() as session:
        rows = await session.execute(
            sa.select(ModelJob.batch_id)
            .where(
                ModelJob.lane == "batch",
                ModelJob.status == "submitted",
                ModelJob.batch_id.is_not(None),
            )
            .group_by(ModelJob.batch_id)
            .order_by(sa.func.min(ModelJob.submitted_at))
        )
        return [str(b) for (b,) in rows.all()]


async def _waiting(session_factory: SessionFactory, batch_id: str) -> list[str]:
    """The custom_ids of a batch's jobs still waiting for their result."""
    async with session_factory() as session:
        rows = await session.scalars(
            sa.select(ModelJob.custom_id)
            .where(
                ModelJob.lane == "batch",
                ModelJob.batch_id == batch_id,
                ModelJob.status == "submitted",
            )
            .order_by(ModelJob.id)
        )
        return list(rows)


async def _claim_submitted(
    session: AsyncSession, batch_id: str, custom_id: str
) -> ModelJob | None:
    job: ModelJob | None = await session.scalar(
        sa.select(ModelJob)
        .where(
            ModelJob.lane == "batch",
            ModelJob.batch_id == batch_id,
            ModelJob.custom_id == custom_id,
            ModelJob.status == "submitted",
        )
        .with_for_update(key_share=True, skip_locked=True)
    )
    return job


async def _apply_batch_answer(
    session: AsyncSession,
    job: ModelJob,
    kind: BatchKind,
    message: Any,
    *,
    now: datetime,
    report: LaneReport,
) -> None:
    """A ``succeeded`` result: settled in place, then validated and applied."""
    op = _op_of(job)
    await costs.settle_provisional(
        session,
        model_job_id=job.id,
        priced=priced_anthropic(
            job.model,
            getattr(message, "usage", None),
            batch=True,
            model_job_id=job.id,
            company_id=job.company_id,
        ),
        operation=op,
        now=now,
    )
    await session.flush()
    job.cost_usd = await _spent(session, job.id)
    stop = getattr(message, "stop_reason", None)
    if stop == extraction.REFUSAL:
        await _fail(session, job, extraction.REFUSAL, report)
        return
    parsed = extraction.parse_answer(kind.schema, extraction.text_of(message))
    if stop == extraction.MAX_TOKENS or parsed is None:
        reason = (
            extraction.MAX_TOKENS if stop == extraction.MAX_TOKENS else extraction.INVALID
        )
        await _ladder(session, job, reason, report)
        return
    try:
        checked = (await _checker(session, job, kind))(parsed)
    except Exception as exc:
        # A validator's bug: asked again, the same answer would fail the same
        # way — a final failure, as in the instant lane.
        log.error(
            "validator failed",
            extra={"model_job_id": job.id, "error": type(exc).__name__},
        )
        await _fail(session, job, extraction.VALIDATOR, report)
        return
    job.status = "applied"
    job.result = checked.result
    job.applied_at = now
    job.error = PII_LEAK if checked.leaks else None
    await session.flush()
    if checked.leaks:
        await extraction.note_pii_leak(
            session,
            now=now,
            op=op,
            model_job_id=job.id,
            fields=checked.leaks,
            by="model_jobs",
        )
    landed = Landed(
        job_id=job.id,
        batch_job_id=job.id,
        lane="batch",
        model=job.model,
        company_id=job.company_id,
        evidence_ids=tuple(job.evidence_ids or ()),
        validated=checked,
    )
    if not await _effects(session, kind, landed, now):
        job.status = "failed"
        job.error = APPLY
        await session.flush()
        report.failed += 1
        return
    report.applied += 1


def _reason(result: Any) -> str:
    """The ladder's reason for a result that is not ``succeeded``."""
    kind = str(getattr(result, "type", "") or "")
    if kind == ERRORED:
        detail = getattr(getattr(result, "error", None), "error", None)
        code = str(getattr(detail, "type", "") or "")
        return f"{ERRORED}:{code}" if code else ERRORED
    return kind or MISSING


async def _settle_one(
    session: AsyncSession,
    job: ModelJob,
    result: Any,
    *,
    now: datetime,
    report: LaneReport,
) -> None:
    kind = KINDS.get(_op_of(job))
    if result is None:
        await _ladder(session, job, MISSING, report)
        return
    if getattr(result, "type", None) != "succeeded":
        if kind is not None:
            detail = getattr(getattr(result, "error", None), "error", None)
            if "grammar" in str(getattr(detail, "message", "") or "").lower():
                # The schema the server cannot compile: prompted next time.
                extraction.note_grammar_failure(kind.schema, RuntimeError("grammar"))
        await _ladder(session, job, _reason(result), report)
        return
    if kind is None:
        log.error("no kind registered for a batch answer", extra={"model_job_id": job.id})
        await _fail(session, job, UNREGISTERED, report)
        return
    await _apply_batch_answer(session, job, kind, result.message, now=now, report=report)


async def collect_batch(
    session_factory: SessionFactory,
    client: anthropic.AsyncAnthropic,
    batch_id: str,
    *,
    now: datetime,
    report: LaneReport,
) -> bool:
    """Collect one batch (step 3). False when it has not ended or could not
    be read — its jobs stay submitted, and a later poll reads it again (the
    results are paid for)."""
    try:
        batch = await client.messages.batches.retrieve(batch_id)
    except extraction.API_FAILURES as exc:
        log.warning(
            "batch not read",
            extra={"batch_id": batch_id, "error": type(exc).__name__},
        )
        report.unreadable += 1
        return False
    if batch.processing_status != ENDED:
        report.running += 1
        return False
    entries: list[Any] | None
    try:
        # Drained before the database is touched (MIYA): nothing is held
        # while the network dribbles the stream in.
        stream = await client.messages.batches.results(batch_id)
        entries = [entry async for entry in stream]
    except anthropic.APIError as exc:
        log.warning(
            "batch results not read",
            extra={"batch_id": batch_id, "error": type(exc).__name__},
        )
        report.unreadable += 1
        return False
    except anthropic.AnthropicError as exc:
        # Raised bare when an ended batch has no results_url (archived,
        # cancelled): it never recovers, so every job takes the ladder.
        log.warning(
            "batch results unavailable",
            extra={"batch_id": batch_id, "error": type(exc).__name__},
        )
        entries = None
    report.batches += 1
    results: dict[str, Any] = {}
    for entry in entries or ():
        results.setdefault(str(entry.custom_id), entry.result)
    for custom_id in await _waiting(session_factory, batch_id):
        async with session_factory() as session, session.begin():
            job = await _claim_submitted(session, batch_id, custom_id)
            if job is None:
                continue
            if entries is None:
                await _ladder(session, job, UNAVAILABLE, report)
                continue
            await _settle_one(
                session, job, results.get(custom_id), now=now, report=report
            )
    return True


async def collect(
    session_factory: SessionFactory = SessionLocal,
    *,
    now: datetime,
    report: LaneReport | None = None,
) -> LaneReport:
    """Every batch holding submitted jobs, the oldest first (step 3)."""
    report = report or LaneReport()
    if not extraction.available():
        return report
    client = extraction.get_client()
    for batch_id in await _open_batches(session_factory):
        await collect_batch(session_factory, client, batch_id, now=now, report=report)
    return report


# --- 6. stuck batches -------------------------------------------------------------


async def give_up_stuck(
    session_factory: SessionFactory = SessionLocal,
    *,
    now: datetime,
    report: LaneReport | None = None,
) -> LaneReport:
    """Each batch ``health.stuck_batches`` names: read once more and
    collected if it ended, else given up — each job one failed attempt
    (``stuck``) on the ladder, the batch cancelled (best effort)."""
    report = report or LaneReport()
    if not extraction.available():
        return report
    async with session_factory() as session:
        stuck = await health.stuck_batches(session, now)
    client = extraction.get_client()
    for batch_id, _ in stuck:
        if await collect_batch(session_factory, client, batch_id, now=now, report=report):
            continue
        for custom_id in await _waiting(session_factory, batch_id):
            async with session_factory() as session, session.begin():
                job = await _claim_submitted(session, batch_id, custom_id)
                if job is not None:
                    await _ladder(session, job, STUCK, report)
        report.stuck += 1
        try:
            await client.messages.batches.cancel(batch_id)
        except extraction.API_FAILURES as exc:
            log.warning(
                "stuck batch not cancelled",
                extra={"batch_id": batch_id, "error": type(exc).__name__},
            )
        log.warning("stuck batch given up", extra={"batch_id": batch_id})
    return report


# --- 5. the instant lane takes the fallbacks -----------------------------------------


def _taken_by(job_id: int) -> sa.ColumnElement[bool]:
    return sa.and_(
        ModelJob.lane == "instant",
        ModelJob.request[FALLBACK_OF].as_string() == str(job_id),
    )


async def fallback_jobs(session: AsyncSession, job_id: int) -> list[ModelJob]:
    """The instant jobs that took (or tried to take) a fallback job, oldest
    first."""
    rows = await session.scalars(
        sa.select(ModelJob).where(_taken_by(job_id)).order_by(ModelJob.id)
    )
    return list(rows)


async def _fallback_failures(session: AsyncSession, job_id: int) -> int:
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ModelJob)
        .where(
            _taken_by(job_id),
            ModelJob.status == "failed",
            ModelJob.error.startswith(extraction.API_PREFIX, autoescape=True),
        )
    )
    return int(count or 0)


async def _claim_fallback(session: AsyncSession, skip: set[int]) -> ModelJob | None:
    stmt = (
        sa.select(ModelJob)
        .where(
            ModelJob.lane == "batch",
            ModelJob.status == "fallback",
            ModelJob.applied_at.is_(None),
            ModelJob.request.is_not(None),
        )
        .order_by(ModelJob.created_at, ModelJob.id)
        .limit(1)
        .with_for_update(key_share=True, skip_locked=True)
    )
    if skip:
        stmt = stmt.where(ModelJob.id.not_in(skip))
    job: ModelJob | None = await session.scalar(stmt)
    return job


class _Refused(Exception):
    """Nothing was sent: undo the instant job's row, keep the fallback."""


Outcome = Literal["done", "waits", "stop"]


async def _take(
    session: AsyncSession, job: ModelJob, *, now: datetime, report: LaneReport
) -> Outcome:
    """Hand one fallback job to the instant lane in the caller's transaction.
    Raises ``_Refused`` when nothing was sent."""
    op = _op_of(job)
    kind = KINDS.get(op)
    if kind is None:
        log.error("no kind registered for a fallback", extra={"model_job_id": job.id})
        return "waits"
    if await _fallback_failures(session, job.id) >= FALLBACK_TRIES:
        job.applied_at = now
        await session.flush()
        report.given_up += 1
        return "done"
    try:
        result = await extraction.call(
            session,
            op=FALLBACK_OP[op],
            schema=kind.schema,
            system=kind.system,
            user=_user(job),
            now=now,
            company_id=job.company_id,
            evidence_ids=job.evidence_ids or (),
            prompt_version=kind.prompt_version,
            validate=await _checker(session, job, kind),
            extra={FALLBACK_OF: job.id},
        )
    except UnmaskedPayload as exc:
        log.error(
            "a fallback job's request is not masked; never sent",
            extra={"model_job_id": job.id, "pattern": exc.pattern},
        )
        job.error = UNMASKED
        job.applied_at = now
        await session.flush()
        report.unmasked += 1
        return "done"
    if result.refused:
        raise _Refused(result.error or "refused")
    if not result.final:
        report.retried += 1
        if not (result.error or "").startswith(extraction.API_PREFIX):
            return "waits"  # a budget stop after a first dispatch: asked again
        # The API failed it: its record is kept, and the next fallback would
        # meet the same outage — the run stops.
        await session.flush()
        if await _fallback_failures(session, job.id) >= FALLBACK_TRIES:
            job.applied_at = now
            await session.flush()
            report.given_up += 1
        return "stop"
    job.applied_at = now
    await session.flush()
    report.taken += 1
    if not result.ok or result.validated is None or result.job_id is None:
        report.failed += 1
        return "done"
    landed = Landed(
        job_id=result.job_id,
        batch_job_id=job.id,
        lane="instant",
        model=result.model,
        company_id=job.company_id,
        evidence_ids=tuple(job.evidence_ids or ()),
        validated=result.validated,
    )
    if not await _effects(session, kind, landed, now):
        await session.execute(
            sa.update(ModelJob)
            .where(ModelJob.id == result.job_id)
            .values(status="failed", error=APPLY)
        )
        report.failed += 1
        return "done"
    report.applied += 1
    return "done"


async def take_fallbacks(
    session_factory: SessionFactory = SessionLocal,
    *,
    now: datetime,
    clock: Clock | None = None,
    limit: int = FALLBACK_MAX,
    report: LaneReport | None = None,
) -> LaneReport:
    """Hand the fallback jobs to the instant lane (step 5), one transaction
    each, the oldest first, each at ``clock()`` (default ``now``). The run
    stops at a budget refusal or an API failure."""
    report = report or LaneReport()
    if not extraction.available():
        return report
    skip: set[int] = set()
    for _ in range(limit):
        at = max(clock(), now) if clock is not None else now
        try:
            async with session_factory() as session, session.begin():
                job = await _claim_fallback(session, skip)
                if job is None:
                    break
                outcome = await _take(session, job, now=at, report=report)
                if outcome != "done":
                    skip.add(job.id)
        except _Refused as refused:
            report.refused += 1
            log.info("fallbacks stopped: refused", extra={"code": str(refused)})
            break
        if outcome == "stop":
            log.warning("fallbacks stopped: the API failed")
            break
        if outcome == "waits":
            report.waiting += 1
    return report


# --- the jobs ---------------------------------------------------------------------


def _wall() -> datetime:
    return datetime.now(settings.tz)


async def model_batch_submit_job(*, now: datetime | None = None) -> dict[str, int]:
    """The worker's ``model_batch_submit`` slot (23:30, 04 §0): the stuck
    batches given up, the night's batch sent, then the fallbacks handed to
    the instant lane. Counts only are logged (06 §4.5)."""
    start = now or _wall()
    report = LaneReport()
    await give_up_stuck(now=start, report=report)
    await submit_batch(now=start, report=report)
    await take_fallbacks(now=start, clock=None if now else _wall, report=report)
    counts = report.as_dict()
    log.info("model_batch_submit", extra={"counts": counts})
    return counts


async def model_batch_collect_job(*, now: datetime | None = None) -> dict[str, int]:
    """The worker's ``model_batch_collect`` slot (every 30 minutes
    00:30–08:00, 04 §0): every ended batch collected, then the fallbacks
    handed to the instant lane."""
    start = now or _wall()
    report = LaneReport()
    await collect(now=start, report=report)
    await take_fallbacks(now=start, clock=None if now else _wall, report=report)
    counts = report.as_dict()
    if report.batches or report.taken or report.refused or report.unreadable:
        log.info("model_batch_collect", extra={"counts": counts})
    return counts


__all__ = [
    "ATTEMPTS_MAX",
    "BATCH_MAX",
    "BATCH_ROUTES",
    "FALLBACK_OF",
    "FALLBACK_OP",
    "FALLBACK_TRIES",
    "KINDS",
    "BatchKind",
    "Landed",
    "LaneReport",
    "collect",
    "collect_batch",
    "enqueue",
    "fallback_jobs",
    "give_up_stuck",
    "model_batch_collect_job",
    "model_batch_submit_job",
    "register",
    "submit_batch",
    "take_fallbacks",
]
