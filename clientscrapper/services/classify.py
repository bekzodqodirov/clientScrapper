"""``classify_job`` — the ImportSignal of every stored Telegram message that
stands after the tout filter (04 §0, §4.1, §4.3).

The queue is what ``telegram_monitor`` leaves for it (T-14): a message row
linked to its evidence and observation and still ``processed_at IS NULL``.
The job wakes on ``NOTIFY cs_interaction`` (the collector's
``telegram_monitor:<id>`` after it links a row, 01 N-06) with the ``every
15m`` sweep as the fallback for a lost notification. One transaction per
message, the oldest first, the message row and its observation's each held
``FOR UPDATE SKIP LOCKED`` (04 §0), at most ``CLASSIFY_BATCH`` a run and
``CLASSIFY_DAILY_MAX`` model jobs a Tashkent day (03 §2.3: ≤ 300 a day).
Each message is classified at the wall clock of its own turn (``clock``):
a run started at 23:58 that pays at 00:01 books the new day, as the
collectors' runner does (``runner.ask_gate``).

For one message:

1. the request: ``SOURCE`` (the chat's kind and title), ``DATE``,
   ``SENDER_BIO`` and ``TEXT``, each pseudonymised with one numbering
   (``prior``). ``names`` = the person-shaped words of the sender's display
   name (``name_words``) and the person names its bio states
   (``normalise.person_names``) — a person's; a chat speaking for itself is
   company-tier. ``TEXT`` is the whole message masked, then cut to
   ``TEXT_MAX`` between words: cut first, a phone across the cut would leave
   digits no pattern reads;
2. ``extraction.call(op='classify')`` — masked, under ``costs.ask``,
   recorded as a ``model_jobs`` row — and ``validators.import_signal``
   against the evidence's excerpt;
3. applied → the row is processed, ``NOTIFY cs_interaction,
   'classify:<id>'`` wakes the scorer (04 §0), and when the observation is
   resolved already the ``AFTER_CLASSIFY`` hooks run with the result;
4. refused before anything was sent (the budget, Anthropic declared down,
   no API key) → nothing is kept: the transaction rolls back, the row stays
   in the queue for the next sweep (04 §8: tomorrow, at a daily cap) and the
   run stops;
5. a final failure (the model refused, an answer malformed twice, out of
   tokens twice) → processed, never asked again (cost stays predictable,
   04 §4.1);
6. the API failed → the job row stays ``failed``, the message waits, and
   **the run stops**: the next message would meet the same outage. A
   message that failed waits ``RETRY_AFTER`` × 2ⁿ⁻¹ after its n-th failure
   before it is asked again, and is given up — processed, unclassified; R2a
   takes it at 30 days — once ``TRIES_MAX`` such failures were met. A burst
   of wakes inside one outage costs each message one attempt, never three;
   a long outage is ``costs.ask``'s ``anthropic_silence`` refusal (4.), which
   counts nothing.

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
from datetime import datetime, timedelta
from typing import Any, Final, Literal

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
from clientscrapper.services.normalise import (
    PERSON_HONORIFICS,
    given_name_shaped,
    person_names,
    surname_shaped,
    trade_word,
)
from clientscrapper.services.prompts import CLASSIFY_PROMPT_VERSION, CLASSIFY_SYSTEM
from clientscrapper.services.pseudonyms import TOKEN_RE, Pseudonymised, pseudonymise
from clientscrapper.services.schemas import ImportSignal

log = logging.getLogger(__name__)

OP: Final = "classify"
CLASSIFY_BATCH: Final = 100
# 03 §2.3: ≤ 300 classify calls a day (the money cap alone allows ≈ 2,000).
CLASSIFY_DAILY_MAX: Final = 300
# A message whose call failed this many times (the API, not the answer) is
# given up: R2a takes it at 30 days like every unconverted row.
TRIES_MAX: Final = 3
# After its n-th failure a message waits RETRY_AFTER × 2ⁿ⁻¹ (15, 30 minutes):
# three failures span at least 45 minutes of an API that kept failing.
RETRY_AFTER: Final = timedelta(minutes=15)
# 04 §4.3 allows 1,500 characters; a quote must be the evidence excerpt's own
# fragment (02 §2.7, ≤ 600), so what is sent is what can be verified. The
# masked text is cut here, which covers a little more than the excerpt: a
# token is shorter than the phone it stands for.
TEXT_MAX: Final = evidence_store.EXCERPT_MAX
NO_VALUE: Final = "-"
# A held candidate is passed by at most this many times in one claim.
CLAIM_PASSES: Final = 25
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
    waiting: int = 0
    given_up: int = 0
    skipped: int = 0
    refused: int = 0
    capped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "applied": self.applied,
            "failed": self.failed,
            "retried": self.retried,
            "waiting": self.waiting,
            "given_up": self.given_up,
            "skipped": self.skipped,
            "refused": self.refused,
            "capped": self.capped,
        }


# --- the request ----------------------------------------------------------------


def name_words(name: str | None) -> list[str]:
    """The words of a sender's display name to mask (04 §4.2: «sender first/
    last name»): every given name and surname-shaped word, and every other
    word of three letters or more — unless the display name carries a
    business word (``normalise.trade_word``: «Kafel Savdo», «Xitoy Mollari»,
    «Oyinchoq Uz»), when those others are the label's and stay, so the goods
    and the origin a text names reach the model. An initial and «aka»/«opa»
    are no name."""
    split = [
        w for w in re.split(r"[\s\-_.,]+", name or "") if any(c.isalpha() for c in w)
    ]
    business = any(trade_word(w) for w in split)
    words: list[str] = []
    for word in split:
        key = name_key(word)
        if sum(c.isalpha() for c in word) < NAME_WORD_MIN or key in PERSON_HONORIFICS:
            continue
        if (
            given_name_shaped(key)
            or surname_shaped(key)
            or (not business and not trade_word(word))
        ):
            words.append(word)
    return words


_PARTIAL_TOKEN: Final = re.compile(r"<[A-Z]*(?:_\d*)?\Z")


def clip_masked(text: str, limit: int) -> str:
    """A masked text cut to ``limit`` characters between words
    (``evidence.clip``), never inside a token: a token the cut would split
    is dropped whole."""
    cut = evidence_store.clip(text, limit)
    return _PARTIAL_TOKEN.sub("", cut).rstrip()


def user_content(
    *,
    chat_type: str,
    title: str | None,
    occurred_at: datetime,
    bio: str | None,
    text: str,
    names: Iterable[str] = (),
    handles: Iterable[str] = (),
) -> Pseudonymised:
    """04 §4.3's user message, pseudonymised part by part with one numbering:
    ``SOURCE: telegram group «<title>»``, ``DATE``, ``SENDER_BIO`` (or ``-``)
    and ``TEXT`` (masked whole, then cut to ``TEXT_MAX``). ``handles`` are
    the sender's own username and Telegram id, masked verbatim wherever the
    message writes them (a bare «namuna_tekstil» no pattern reads). The
    reverse map holds only the tokens the message carries."""
    names, handles = list(names), list(handles)
    head = pseudonymise(title or "", names=names, handles=handles)
    told = pseudonymise(bio or "", names=names, handles=handles, prior=head)
    said = pseudonymise(text, names=names, handles=handles, prior=told)
    source = f"telegram {chat_type}" + (f" «{head.text}»" if head.text else "")
    date = occurred_at.astimezone(settings.tz).isoformat(timespec="seconds")
    lines = (
        f"SOURCE: {source}",
        f"DATE: {date}",
        f"SENDER_BIO: {told.text or NO_VALUE}",
        f"TEXT: {clip_masked(said.text, TEXT_MAX)}",
    )
    content = "\n".join(lines)
    used = {m.group(0) for m in TOKEN_RE.finditer(content)}
    return Pseudonymised(content, {t: v for t, v in said.reverse.items() if t in used})


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
        sa.select(Interaction, ChatMonitor, Evidence)
        .join(ChatMonitor, ChatMonitor.tg_chat_id == Interaction.tg_chat_id)
        .join(Evidence, Evidence.id == Interaction.evidence_id)
        .where(
            Interaction.processed_at.is_(None),
            Interaction.text.is_not(None),
            Interaction.text_purged_at.is_(None),
            Interaction.observation_id.is_not(None),
            Interaction.tg_chat_id.in_(readable),
        )
        .order_by(Interaction.created_at, Interaction.id)
    )


async def waiting(session: AsyncSession) -> int:
    """How many messages wait for the classifier (the queue ``_claim``
    takes from): 06 §4.2 anthropic_silence's «tasnif navbatda (n ta)»."""
    readable = await chats.readable_ids(session)
    if not readable:
        return 0
    queue = _queue(readable).with_only_columns(sa.func.count()).order_by(None)
    return int(await session.scalar(queue) or 0)


async def _claim(session: AsyncSession, skip: set[int]) -> _Claim | None:
    """The oldest message waiting for the classifier, held until commit —
    the message row and its observation's. Holding the observation is what
    lets the result meet its company: the resolver claims observations
    ``FOR UPDATE SKIP LOCKED``, so while a message is being classified the
    resolver passes its observation by, and resolves it once the result is
    committed (its hook then finds it, ``classification_for``); one the
    resolver holds, classify passes by in turn, and reads its company when it
    comes back. Without it a resolution landing during the model call would
    see no result, and the result would see no company.

    Two steps, each ``SKIP LOCKED``, the message first and its observation
    in a savepoint: a message whose observation is held is let go again (the
    savepoint's rollback releases its row lock) — one statement locking both
    would keep the message it passed by locked until this transaction ends,
    and the reader's edit and bio writes would wait on it for a model call."""
    readable = await chats.readable_ids(session)
    if not readable:
        return None
    passed: set[int] = set()
    for _ in range(CLAIM_PASSES):
        stmt = _queue(readable)
        if skip or passed:
            stmt = stmt.where(Interaction.id.not_in(skip | passed))
        held = stmt.limit(1).with_for_update(of=Interaction, skip_locked=True)
        savepoint = await session.begin_nested()
        found = (await session.execute(held)).first()
        if found is None:
            await savepoint.commit()
            return None
        row, monitor, proof = found
        obs = await session.scalar(
            sa.select(ObservationRow)
            .where(ObservationRow.id == row.observation_id)
            .with_for_update(skip_locked=True)
        )
        if obs is None:
            await savepoint.rollback()
            passed.add(row.id)
            continue
        await savepoint.commit()
        return _Claim(row, monitor, proof, obs)
    return None


@dataclass(frozen=True, slots=True)
class Failures:
    """The API failures of one message's classify calls so far."""

    count: int
    last_at: datetime | None

    def waits_until(self) -> datetime | None:
        """When the message may be asked again (None: now)."""
        if not self.count or self.last_at is None:
            return None
        return self.last_at + RETRY_AFTER * (1 << (self.count - 1))


async def failures(session: AsyncSession, evidence_id: int) -> Failures:
    """The classify calls for this evidence the API failed (``api:<class>``)
    — not an answer, final or not, and not a budget stop mid-call, which is
    no fault of the message's and is asked again whenever room returns."""
    count, last = (
        await session.execute(
            sa.select(sa.func.count(), sa.func.max(ModelJob.created_at)).where(
                ModelJob.kind == OP,
                ModelJob.status == "failed",
                ModelJob.evidence_ids.contains([evidence_id]),
                ModelJob.error.startswith(extraction.API_PREFIX, autoescape=True),
            )
        )
    ).one()
    return Failures(int(count or 0), last)


async def tries(session: AsyncSession, evidence_id: int) -> int:
    """How many classify calls for this evidence the API failed."""
    return (await failures(session, evidence_id)).count


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


# What became of one claimed message: done with (processed), waiting for a
# later run, or waiting and the run stops — the API failed its call, and the
# next message would meet the same outage (its failed job is kept).
Outcome = Literal["done", "waits", "stop"]


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
) -> Outcome:
    """Classify one claimed message in the caller's transaction (``Outcome``).
    Raises ``_Refused`` when nothing was sent."""
    row, monitor, proof, obs = claim.row, claim.monitor, claim.evidence, claim.observation
    if obs.status == "rejected":
        # The resolver turned it away already (a competitor, a supplier):
        # nothing a classification could add.
        await _done(session, row, now)
        report.skipped += 1
        return "done"
    failed = await failures(session, proof.id)
    if failed.count >= TRIES_MAX:
        await _done(session, row, now)
        report.given_up += 1
        return "done"
    due = failed.waits_until()
    if due is not None and now < due:
        report.waiting += 1
        return "waits"
    speaker = _speaker(row, monitor)
    names: list[str] = []
    handles: list[str] = []
    bio: str | None = None
    if speaker.kind == "person":
        bio = row.sender_bio
        if bio is None and row.sender_id is not None:
            bio = (await chats.known_bios(session, [row.sender_id], now=now)).get(
                row.sender_id
            )
        names = [*name_words(row.sender_name), *person_names(bio)]
        handles = [row.sender_username] if row.sender_username else []
        if row.sender_id is not None:
            handles.append(str(row.sender_id))
    user = user_content(
        chat_type=monitor.chat_type,
        title=monitor.title,
        occurred_at=row.occurred_at,
        bio=bio,
        text=row.text or "",
        names=names,
        handles=handles,
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
        if (result.error or "").startswith(extraction.API_PREFIX):
            return "stop"
        return "waits"
    await _done(session, row, now)
    if not result.ok or result.validated is None or result.job_id is None:
        report.failed += 1
        return "done"
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
    return "done"


SessionFactory = Callable[[], AsyncSession]
Clock = Callable[[], datetime]


async def _classify_next(
    session_factory: SessionFactory,
    *,
    now: datetime,
    skip: set[int],
    hooks: tuple[AfterClassify, ...],
    report: ClassifyReport,
) -> bool | None:
    """Claim and classify the oldest waiting message in its own transaction
    at ``now``. False when the queue is empty or the day's cap is reached,
    None when the run must stop (refused, the API down)."""
    try:
        async with session_factory() as session, session.begin():
            if await jobs_today(session, now) >= CLASSIFY_DAILY_MAX:
                report.capped += 1
                return False
            claim = await _claim(session, skip)
            if claim is None:
                return False
            report.claimed += 1
            outcome = await classify_message(
                session, claim, now=now, hooks=hooks, report=report
            )
            if outcome != "done":
                skip.add(claim.row.id)
    except _Refused as refused:
        report.refused += 1
        log.info("classify stopped: refused", extra={"code": str(refused)})
        return None
    if outcome == "stop":
        # Committed: the failed job is the message's record of the failure.
        log.warning("classify stopped: the API failed")
        return None
    return True


async def classify_batch(
    session_factory: SessionFactory = SessionLocal,
    *,
    now: datetime,
    clock: Clock | None = None,
    limit: int = CLASSIFY_BATCH,
    hooks: Iterable[AfterClassify] | None = None,
) -> ClassifyReport:
    """Classify up to ``limit`` waiting messages, one transaction each, within
    the day's ``CLASSIFY_DAILY_MAX``. ``now`` is the run's start; each message
    is classified at ``clock()`` (default: ``now`` for every message — a
    test's fixed clock). Nothing is claimed while ``costs.ask`` still
    remembers refusing classify, or with no client to call."""
    report = ClassifyReport()
    if costs.refused_until(OP, now) is not None or not extraction.available():
        report.refused += 1
        return report
    chosen = tuple(AFTER_CLASSIFY if hooks is None else hooks)
    skip: set[int] = set()
    for _ in range(limit):
        at = max(clock(), now) if clock is not None else now
        went = await _classify_next(
            session_factory, now=at, skip=skip, hooks=chosen, report=report
        )
        if not went:
            break
    return report


async def classify_job(
    *, now: datetime | None = None, limit: int = CLASSIFY_BATCH
) -> dict[str, int]:
    """The scheduled job: the worker's ``classify`` slot (``every 15m`` and
    every ``NOTIFY cs_interaction``, 04 §0), each message at the wall clock of
    its turn. Counts only are logged (06 §4.5)."""

    def wall() -> datetime:
        return datetime.now(settings.tz)

    report = await classify_batch(now=now or wall(), clock=wall, limit=limit)
    counts = report.as_dict()
    if report.claimed or report.refused or report.capped:
        log.info("classify_job", extra={"counts": counts})
    return counts


__all__ = [
    "AFTER_CLASSIFY",
    "CLASSIFY_BATCH",
    "CLASSIFY_DAILY_MAX",
    "RETRY_AFTER",
    "TEXT_MAX",
    "TRIES_MAX",
    "Classified",
    "ClassifyReport",
    "classification_for",
    "classify_batch",
    "classify_job",
    "classify_message",
    "clip_masked",
    "failures",
    "name_words",
    "register_after_classify",
    "user_content",
    "waiting",
]
