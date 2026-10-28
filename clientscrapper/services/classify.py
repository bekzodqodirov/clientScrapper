"""``classify_job`` — the ImportSignal of every stored Telegram message that
stands after the tout filter (04 §0, §4.1, §4.3).

The queue is what ``telegram_monitor`` leaves for it (T-14): a message row
linked to its evidence and observation and still ``processed_at IS NULL``.
The job wakes on ``NOTIFY cs_interaction`` (the collector's
``telegram_monitor:<id>`` after it links a row, 01 N-06) with the ``every
15m`` sweep as the fallback for a lost notification. One transaction per
message, the oldest first, each row held ``FOR UPDATE SKIP LOCKED`` (04 §0),
at most ``CLASSIFY_BATCH`` a run and ``CLASSIFY_DAILY_MAX`` a Tashkent day
(03 §2.3: ≤ 300 a day).

For one message:

1. the request: ``SOURCE`` (the chat's kind and title), ``DATE``,
   ``SENDER_BIO`` and ``TEXT`` (≤ 1,500 characters), each pseudonymised with
   one numbering (``prior``) and the sender's own name words as ``names``
   (a person's; a chat speaking for itself is company-tier);
2. ``extraction.call(op='classify')`` — masked, under ``costs.ask``,
   recorded as a ``model_jobs`` row — and ``validators.import_signal``
   against the evidence's excerpt;
3. applied → the row is processed, ``NOTIFY cs_interaction,
   'classify:<id>'`` wakes the scorer (04 §0), and when the observation is
   resolved already the ``AFTER_CLASSIFY`` hooks run with the result;
4. refused before anything was sent (the budget, no API key) → nothing is
   kept: the transaction rolls back, the row stays in the queue for the next
   sweep (04 §8: tomorrow, at a daily cap) and the run stops;
5. a final failure (the model refused, an answer malformed twice, out of
   tokens twice) → processed, never asked again (cost stays predictable,
   04 §4.1); a transient one (the API failed) → asked again by a later run,
   at most ``TRIES_MAX`` times in all.

**Where the result goes.** A signal needs a company (02 §2.7), and the
resolver may not have made one yet: classify runs in seconds, the resolver
every two minutes. The validated result stays in ``model_jobs.result``
(``evidence_ids`` = the message's evidence); ``AFTER_CLASSIFY`` hooks turn it
into signals when the company is known now, and ``classification_for`` hands
it to the resolver's hook when it is known later. Both are seams for the
signal writer (``services/signals``, T-18: ``from_model`` + ``emit``), whose
upsert on ``(company, key, evidence)`` makes running both harmless.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors import telegram_monitor as tm
from clientscrapper.config import settings
from clientscrapper.db.models import ChatMonitor, Evidence, Interaction, ModelJob
from clientscrapper.db.models import Observation as ObservationRow
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import chats, costs, extraction, validators
from clientscrapper.services import evidence as evidence_store
from clientscrapper.services.keys import name_key
from clientscrapper.services.normalise import GENERIC_TOKENS
from clientscrapper.services.prompts import CLASSIFY_PROMPT_VERSION, CLASSIFY_SYSTEM
from clientscrapper.services.pseudonyms import Pseudonymised, pseudonymise
from clientscrapper.services.schemas import ImportSignal

log = logging.getLogger(__name__)

OP: Final = "classify"
CLASSIFY_BATCH: Final = 100
# 03 §2.3: ≤ 300 classify calls a day (the money cap alone allows ≈ 2,000).
CLASSIFY_DAILY_MAX: Final = 300
# A message whose call failed this many times (the API, not the answer) is
# given up: R2a takes it at 30 days like every unconverted row.
TRIES_MAX: Final = 3
TEXT_MAX: Final = 1500
NO_VALUE: Final = "-"
# The payload this job announces itself with (worker.origin_of): it wakes the
# scorer and the collector, never classify again.
NOTIFY_PREFIX: Final = "classify:"
# A sender's name word shorter than this is an initial (it would mask «R» in
# every «R.»).
NAME_WORD_MIN: Final = 3


@dataclass(frozen=True, slots=True)
class Classified:
    """One validated ImportSignal and where it belongs. ``signal`` is the
    stored result (``model_jobs.result``); ``quote`` the verified volume quote
    — the evidence excerpt's own fragment — or None (no volume signal may be
    written from it, 04 §4.7)."""

    interaction_id: int
    evidence_id: int
    observation_id: int | None
    company_id: uuid.UUID | None
    model_job_id: int
    model: str
    signal: Mapping[str, Any]
    quote: str | None


AfterClassify = Callable[[AsyncSession, Classified, datetime], Awaitable[None]]
# Run inside the job's transaction (a savepoint each) when the observation is
# resolved already: the signal writer (T-18) registers here. Read when a run
# starts, so a hook registered at import time of its module reaches it.
AFTER_CLASSIFY: list[AfterClassify] = []


def register_after_classify(hook: AfterClassify) -> None:
    """Run ``hook(session, classified, now)`` after every applied result whose
    company is known. A hook that raises costs only its own savepoint: the
    result stays applied. Idempotent."""
    if hook not in AFTER_CLASSIFY:
        AFTER_CLASSIFY.append(hook)


@dataclass(slots=True)
class ClassifyReport:
    claimed: int = 0
    applied: int = 0
    failed: int = 0
    retried: int = 0
    given_up: int = 0
    skipped: int = 0
    refused: int = 0
    capped: int = 0

    @property
    def calls(self) -> int:
        """Messages a model call was made for in this run."""
        return self.applied + self.failed + self.retried

    def as_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "applied": self.applied,
            "failed": self.failed,
            "retried": self.retried,
            "given_up": self.given_up,
            "skipped": self.skipped,
            "refused": self.refused,
            "capped": self.capped,
        }


# --- the request ----------------------------------------------------------------


def name_words(name: str | None) -> list[str]:
    """The words of a sender's display name worth masking: not an initial,
    not a business word («Namuna Optom» keeps «Optom», 04 §2.2's
    ``GENERIC_TOKENS``)."""
    words = []
    for word in re.split(r"[\s\-_.,]+", name or ""):
        if sum(c.isalpha() for c in word) < NAME_WORD_MIN:
            continue
        if name_key(word) in GENERIC_TOKENS:
            continue
        words.append(word)
    return words


def user_content(
    *,
    chat_type: str,
    title: str | None,
    occurred_at: datetime,
    bio: str | None,
    text: str,
    names: Iterable[str] = (),
) -> Pseudonymised:
    """04 §4.3's user message, pseudonymised part by part with one numbering:
    ``SOURCE: telegram group «<title>»``, ``DATE``, ``SENDER_BIO`` (or ``-``)
    and ``TEXT`` (≤ 1,500 characters)."""
    names = list(names)
    head = pseudonymise(title or "", names=names)
    told = pseudonymise(bio or "", names=names, prior=head)
    said = pseudonymise(evidence_store.clip(text, TEXT_MAX), names=names, prior=told)
    source = f"telegram {chat_type}" + (f" «{head.text}»" if head.text else "")
    date = occurred_at.astimezone(settings.tz).isoformat(timespec="seconds")
    lines = (
        f"SOURCE: {source}",
        f"DATE: {date}",
        f"SENDER_BIO: {told.text or NO_VALUE}",
        f"TEXT: {said.text}",
    )
    return Pseudonymised("\n".join(lines), said.reverse)


def _speaker(row: Interaction, monitor: ChatMonitor) -> tm.Speaker:
    """Who the message speaks for — the collector's rule (``speaker_of``)."""
    post = tm.Post(
        interaction_id=row.id,
        chat=tm.Chat(
            tg_chat_id=monitor.tg_chat_id,
            chat_type=monitor.chat_type,
            title=monitor.title,
            username=monitor.username,
            role=monitor.role,
        ),
        tg_message_id=row.tg_message_id,
        text=row.text or "",
        occurred_at=row.occurred_at,
        stored_at=row.created_at,
        sender_id=row.sender_id,
        sender_username=row.sender_username,
        sender_name=row.sender_name,
    )
    return tm.speaker_of(post)


# --- the queue -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Claim:
    row: Interaction
    monitor: ChatMonitor
    evidence: Evidence
    observation: ObservationRow


def _queue(readable: list[int]) -> sa.Select[Any]:
    return (
        sa.select(Interaction, ChatMonitor, Evidence, ObservationRow)
        .join(ChatMonitor, ChatMonitor.tg_chat_id == Interaction.tg_chat_id)
        .join(Evidence, Evidence.id == Interaction.evidence_id)
        .join(ObservationRow, ObservationRow.id == Interaction.observation_id)
        .where(
            Interaction.processed_at.is_(None),
            Interaction.text.is_not(None),
            Interaction.text_purged_at.is_(None),
            Interaction.tg_chat_id.in_(readable),
        )
        .order_by(Interaction.created_at, Interaction.id)
    )


async def _claim(session: AsyncSession, skip: set[int]) -> _Claim | None:
    """The oldest message waiting for the classifier, held until commit —
    the message row and its observation's. Holding the observation is what
    lets the result meet its company: the resolver claims observations
    ``FOR UPDATE SKIP LOCKED``, so while a message is being classified the
    resolver passes its observation by, and resolves it once the result is
    committed (its hook then finds it, ``classification_for``); one the
    resolver holds, classify passes by in turn, and reads its company when it
    comes back. Without it a resolution landing during the model call would
    see no result, and the result would see no company."""
    readable = await chats.readable_ids(session)
    if not readable:
        return None
    stmt = _queue(readable)
    if skip:
        stmt = stmt.where(Interaction.id.not_in(skip))
    held = stmt.limit(1).with_for_update(
        # A column names its table here: FOR UPDATE OF interactions, observations.
        of=[Interaction.id, ObservationRow.id],
        skip_locked=True,
    )
    found = (await session.execute(held)).first()
    if found is None:
        return None
    row, monitor, proof, obs = found
    return _Claim(row, monitor, proof, obs)


async def tries(session: AsyncSession, evidence_id: int) -> int:
    """How many classify calls for this evidence failed on the way (the API,
    a budget refusal mid-call) rather than with an answer."""
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ModelJob)
        .where(
            ModelJob.kind == OP,
            ModelJob.status == "failed",
            ModelJob.evidence_ids.contains([evidence_id]),
            sa.or_(
                ModelJob.error.is_(None),
                ModelJob.error.not_in(sorted(extraction.FINAL_ERRORS)),
            ),
        )
    )
    return int(count or 0)


async def jobs_today(session: AsyncSession, now: datetime) -> int:
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ModelJob)
        .where(ModelJob.kind == OP, ModelJob.created_at >= costs.day_start(now))
    )
    return int(count or 0)


async def classification_for(
    session: AsyncSession, evidence_id: int
) -> Classified | None:
    """The newest applied ImportSignal of the message whose evidence this is
    — for the resolver's hook, which knows the company classify did not."""
    job = await session.scalar(
        sa.select(ModelJob)
        .where(
            ModelJob.kind == OP,
            ModelJob.status == "applied",
            ModelJob.evidence_ids.contains([evidence_id]),
        )
        .order_by(ModelJob.applied_at.desc(), ModelJob.id.desc())
        .limit(1)
    )
    if job is None or job.result is None:
        return None
    row = await session.scalar(
        sa.select(Interaction)
        .where(Interaction.evidence_id == evidence_id)
        .order_by(Interaction.id)
        .limit(1)
    )
    if row is None:
        return None
    quote = job.result.get("volume_quote")
    return Classified(
        interaction_id=row.id,
        evidence_id=evidence_id,
        observation_id=row.observation_id,
        company_id=job.company_id,
        model_job_id=job.id,
        model=job.model,
        signal=dict(job.result),
        quote=quote if isinstance(quote, str) else None,
    )


# --- one message -----------------------------------------------------------------


class _Refused(Exception):
    """Nothing was sent: undo the job row, keep the message queued."""


async def _done(session: AsyncSession, row: Interaction, now: datetime) -> None:
    await session.execute(
        sa.update(Interaction).where(Interaction.id == row.id).values(processed_at=now)
    )


async def _run_hooks(
    session: AsyncSession,
    hooks: tuple[AfterClassify, ...],
    classified: Classified,
    now: datetime,
) -> None:
    for hook in hooks:
        try:
            async with session.begin_nested():
                await hook(session, classified, now)
        except Exception as exc:
            log.error(
                "after-classify hook failed",
                extra={
                    "model_job_id": classified.model_job_id,
                    "error": type(exc).__name__,
                },
            )


async def classify_message(
    session: AsyncSession,
    claim: _Claim,
    *,
    now: datetime,
    hooks: tuple[AfterClassify, ...],
    report: ClassifyReport,
) -> bool:
    """Classify one claimed message in the caller's transaction. True when
    the row is done with (processed), False when it waits for a later run.
    Raises ``_Refused`` when nothing was sent."""
    row, monitor, proof, obs = claim.row, claim.monitor, claim.evidence, claim.observation
    if obs.status == "rejected":
        # The resolver turned it away already (a competitor, a supplier):
        # nothing a classification could add.
        await _done(session, row, now)
        report.skipped += 1
        return True
    if await tries(session, proof.id) >= TRIES_MAX:
        await _done(session, row, now)
        report.given_up += 1
        return True
    speaker = _speaker(row, monitor)
    names: list[str] = []
    bio: str | None = None
    if speaker.kind == "person":
        names = name_words(row.sender_name)
        bio = row.sender_bio
        if bio is None and row.sender_id is not None:
            bio = (await chats.known_bios(session, [row.sender_id], now=now)).get(
                row.sender_id
            )
    user = user_content(
        chat_type=monitor.chat_type,
        title=monitor.title,
        occurred_at=row.occurred_at,
        bio=bio,
        text=row.text or "",
        names=names,
    )
    text = row.text or ""

    def validate(signal: ImportSignal) -> validators.Validated:
        return validators.import_signal(
            signal, excerpt=proof.excerpt, text=text, reverse=user.reverse
        )

    result = await extraction.call(
        session,
        op=OP,
        schema=ImportSignal,
        system=CLASSIFY_SYSTEM,
        user=user,
        now=now,
        company_id=obs.company_id,
        evidence_ids=[proof.id],
        prompt_version=CLASSIFY_PROMPT_VERSION,
        validate=validate,
    )
    if result.refused:
        raise _Refused(result.error or "refused")
    if not result.final:
        report.retried += 1
        return False
    await _done(session, row, now)
    if not result.ok or result.validated is None or result.job_id is None:
        report.failed += 1
        return True
    report.applied += 1
    payload = f"{NOTIFY_PREFIX}{row.id}"
    await session.execute(
        sa.select(sa.func.pg_notify(chats.INTERACTION_CHANNEL, payload))
    )
    if obs.company_id is not None and hooks:
        classified = Classified(
            interaction_id=row.id,
            evidence_id=proof.id,
            observation_id=obs.id,
            company_id=obs.company_id,
            model_job_id=result.job_id,
            model=result.model,
            signal=result.validated.result,
            quote=result.validated.quote,
        )
        await _run_hooks(session, hooks, classified, now)
    return True


SessionFactory = Callable[[], AsyncSession]


async def _classify_next(
    session_factory: SessionFactory,
    *,
    now: datetime,
    skip: set[int],
    hooks: tuple[AfterClassify, ...],
    report: ClassifyReport,
) -> bool | None:
    """Claim and classify the oldest waiting message in its own transaction.
    False when the queue is empty, None when the run must stop (refused)."""
    try:
        async with session_factory() as session, session.begin():
            claim = await _claim(session, skip)
            if claim is None:
                return False
            report.claimed += 1
            if not await classify_message(
                session, claim, now=now, hooks=hooks, report=report
            ):
                skip.add(claim.row.id)
    except _Refused as refused:
        report.refused += 1
        log.info("classify stopped: refused", extra={"code": str(refused)})
        return None
    return True


async def classify_batch(
    session_factory: SessionFactory = SessionLocal,
    *,
    now: datetime,
    limit: int = CLASSIFY_BATCH,
    hooks: Iterable[AfterClassify] | None = None,
) -> ClassifyReport:
    """Classify up to ``limit`` waiting messages, one transaction each, within
    the day's ``CLASSIFY_DAILY_MAX``. Nothing is claimed while ``costs.ask``
    still remembers refusing classify, or with no client to call."""
    report = ClassifyReport()
    if costs.refused_until(OP, now) is not None or not extraction.available():
        report.refused += 1
        return report
    async with session_factory() as session:
        used = await jobs_today(session, now)
    chosen = tuple(AFTER_CLASSIFY if hooks is None else hooks)
    skip: set[int] = set()
    for _ in range(limit):
        if used + report.calls >= CLASSIFY_DAILY_MAX:
            report.capped += 1
            break
        went = await _classify_next(
            session_factory, now=now, skip=skip, hooks=chosen, report=report
        )
        if not went:
            break
    return report


async def classify_job(
    *, now: datetime | None = None, limit: int = CLASSIFY_BATCH
) -> dict[str, int]:
    """The scheduled job: the worker's ``classify`` slot (``every 15m`` and
    every ``NOTIFY cs_interaction``, 04 §0). Counts only are logged (06 §4.5)."""
    report = await classify_batch(now=now or datetime.now(settings.tz), limit=limit)
    counts = report.as_dict()
    if report.claimed or report.refused:
        log.info("classify_job", extra={"counts": counts})
    return counts


__all__ = [
    "AFTER_CLASSIFY",
    "CLASSIFY_BATCH",
    "CLASSIFY_DAILY_MAX",
    "TRIES_MAX",
    "Classified",
    "ClassifyReport",
    "classification_for",
    "classify_batch",
    "classify_job",
    "classify_message",
    "name_words",
    "register_after_classify",
    "user_content",
]
