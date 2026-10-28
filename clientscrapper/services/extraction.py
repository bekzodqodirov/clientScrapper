# VENDORED_FROM dailyJournal@6b1f67f miya/services/extraction.py
"""The instant lane: one model call, masked, budgeted, validated (04 §4.1).

Kept from MIYA: the shared client with its own retries, ``AnthropicUnavailable``
for a missing key, ``API_FAILURES``, the structured path with the prompted-JSON
fallback when the server cannot compile the schema into a grammar (remembered
for the rest of the process, per schema), ``json_payload`` (unwrapping, never
repair), one retry with the JSON nudge, refusal and ``max_tokens`` handling,
and every billed attempt counted.

Changed for this repository:

* every request is checked by ``pseudonyms.assert_masked`` before it is
  dispatched — the system block, the user text and any tool definition — and
  the user text arrives only as a ``Pseudonymised`` (06 §5.2);
* every request is one ``costs.ask`` (the claim before the call, the price
  after it, 04 §8): a refusal before dispatch sends nothing;
* the structured path is ``messages.create`` with the ``output_config`` that
  ``messages.parse`` would build (the SDK's own ``transform_schema``), and the
  answer is validated here against the same schema: ``parse`` validates
  inside the SDK and raises on a refusal's non-JSON text, which loses the
  ``stop_reason`` and the usage — the refusal would read as a failure and its
  tokens would go unpriced;
* ``output_config.effort`` is sent and thinking stays adaptive (no
  ``thinking``, no ``temperature``, no prefill, no forced ``tool_choice``); the
  system block is cached (``cache_control: ephemeral``);
* ``max_tokens`` → one retry with twice the budget; a schema failure → one
  retry with the nudge; at most one retry in all; a refusal → ``failed``,
  ``error='refusal'``, no retry and no other model;
* the job is a ``model_jobs`` row (``pending`` → ``applied``/``failed``) in the
  caller's transaction, with the masked request, the reverse map and the
  validated result; the failure streak raises ``anthropic_failing`` and a
  dropped field ``pii_leak`` (06 §4.2).

Which model an operation uses is configuration: the role of
``usage.MODEL_ROLES`` that lists the operation names its settings key
(``EXTRACT_MODEL`` …). ``ROUTES`` holds the rest of 04 §4.1's table.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

import anthropic
import sqlalchemy as sa
from anthropic.lib._parse._transform import transform_schema
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import ModelJob, UsageLog
from clientscrapper.services import costs, health
from clientscrapper.services.prompts import JSON_NUDGE
from clientscrapper.services.pseudonyms import Pseudonymised, assert_masked
from clientscrapper.services.usage import (
    MODEL_ROLES,
    Operation,
    Priced,
    anthropic_cost_usd,
    priced_anthropic,
)
from clientscrapper.services.validators import PII_LEAK, Validated

log = logging.getLogger(__name__)

Effort = Literal["low", "medium", "high", "xhigh", "max"]
Lane = Literal["instant", "batch"]
JobKind = Literal["classify", "verdict", "company_verdict", "draft"]


@dataclass(frozen=True, slots=True)
class Route:
    """One row of 04 §4.1's table: the job kind, the lane, the effort and the
    output budget of an operation (its model is its role's setting)."""

    kind: JobKind
    lane: Lane
    effort: Effort
    max_tokens: int


ROUTES: Final[Mapping[Operation, Route]] = {
    "classify": Route("classify", "instant", "low", 400),
    "verdict": Route("verdict", "instant", "low", 1200),
    "company_verdict": Route("company_verdict", "batch", "low", 1500),
    "draft": Route("draft", "instant", "low", 600),
}

# The shared client's own exponential backoff on connection errors, 408, 409,
# 429 and 5xx (MIYA JV-02): never wrapped in another retry on top.
MODEL_CLIENT_MAX_RETRIES: Final = 3
MODEL_CLIENT_TIMEOUT_S: Final = 60.0
# 06 §4.2 anthropic_failing: this many failed attempts in the window.
FAILING_MIN: Final = 5
FAILING_WINDOW: Final = timedelta(hours=1)
PII_LEAK_HOLD: Final = timedelta(hours=24)
# A characters-per-token ratio no Uzbek, Russian or English text goes below:
# the claim is never under what the call can cost (04 §8, costs.ask).
CHARS_PER_TOKEN: Final = 2
# model_jobs.attempts' CHECK (02 §2.6): at most three paid requests are made
# (a grammar refusal, the prompted turn, one retry).
ATTEMPTS_MAX: Final = 3
REFUSAL: Final = "refusal"
MAX_TOKENS: Final = "max_tokens"
INVALID: Final = "invalid"
UNAVAILABLE: Final = "unavailable"
VALIDATOR: Final = "validator"
API_PREFIX: Final = "api:"
# A job error is a final answer (no try again), or a failure of the way to it.
FINAL_ERRORS: Final = frozenset({REFUSAL, MAX_TOKENS, INVALID, VALIDATOR})


class AnthropicUnavailable(RuntimeError):
    """Anthropic cannot be called at all — no API key configured."""


# What every caller treats as «the model is not reachable right now».
API_FAILURES: Final = (anthropic.AnthropicError, AnthropicUnavailable)


@dataclass(slots=True)
class _Client:
    client: anthropic.AsyncAnthropic | None = None
    injected: bool = False


_shared = _Client()


def set_client(client: anthropic.AsyncAnthropic | None) -> None:
    """Use ``client`` for every call (tests install a fake transport here);
    None goes back to the configured one."""
    _shared.client = client
    _shared.injected = client is not None


def available() -> bool:
    """Is there a client to call (a key, or an injected client)?"""
    return _shared.injected or bool(settings.anthropic_api_key.strip())


def get_client() -> anthropic.AsyncAnthropic:
    """The shared client; ``AnthropicUnavailable`` when no key is set — the
    SDK's own failure is a bare TypeError deep in header building (MIYA)."""
    if _shared.client is not None:
        return _shared.client
    if not settings.anthropic_api_key.strip():
        raise AnthropicUnavailable("ANTHROPIC_API_KEY is not set")
    _shared.client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        max_retries=MODEL_CLIENT_MAX_RETRIES,
        timeout=MODEL_CLIENT_TIMEOUT_S,
    )
    return _shared.client


def model_for(op: Operation) -> str:
    """The configured model of ``op``: its role's setting (``EXTRACT_MODEL``
    for classify, draft and enrich_search, 04 §4.1)."""
    for role in MODEL_ROLES:
        if op in role.operations:
            model: str = getattr(settings, role.key.lower())
            return model
    raise ValueError(f"{op} is not a model operation")


# --- the grammar fallback (MIYA) -----------------------------------------------

# Schemas the server could not compile into a sampling grammar («Grammar
# compilation timed out»): decided before the model runs, so the identical
# request cannot succeed and every later one would pay the latency to fail the
# same way. Remembered per schema until the process restarts — the right
# cadence for a platform-side limit.
_grammar_refused: set[str] = set()


def structured_output_available(schema: type[BaseModel]) -> bool:
    return schema.__name__ not in _grammar_refused


def note_grammar_failure(schema: type[BaseModel], exc: Exception) -> None:
    if schema.__name__ not in _grammar_refused:
        log.warning(
            "structured outputs unavailable for this schema; prompted JSON from now on",
            extra={"schema": schema.__name__, "error": type(exc).__name__},
        )
    _grammar_refused.add(schema.__name__)


def reset_grammar_memory() -> None:
    _grammar_refused.clear()


def is_grammar_failure(exc: Exception) -> bool:
    """Did the server refuse the *schema* rather than the request?"""
    return isinstance(exc, anthropic.BadRequestError) and "grammar" in str(exc).lower()


@functools.lru_cache(maxsize=16)
def json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The schema both request paths are built from."""
    built: dict[str, Any] = TypeAdapter(schema).json_schema()
    return built


@functools.lru_cache(maxsize=16)
def schema_instructions(schema: type[BaseModel]) -> str:
    """The schema as prompt text, for the prompted path — generated from the
    same model the structured path sends, so the two never drift."""
    return (
        "\n\nReturn a JSON object valid against this JSON Schema. "
        "Emit the object only - no prose, no markdown fences.\n"
        + json.dumps(json_schema(schema), ensure_ascii=False, separators=(",", ":"))
    )


def system_block(
    system: str, schema: type[BaseModel], *, with_schema: bool
) -> list[dict[str, Any]]:
    """The cached system block; ``with_schema`` appends the schema for the
    prompted path. Both variants are byte-stable: one cache entry each."""
    text = system + (schema_instructions(schema) if with_schema else "")
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def output_config(
    schema: type[BaseModel], *, effort: Effort, structured: bool
) -> dict[str, Any]:
    """``{"effort": …}``, plus the format ``messages.parse`` builds from the
    type on the structured path."""
    config: dict[str, Any] = {"effort": effort}
    if structured:
        config["format"] = {
            "type": "json_schema",
            "schema": transform_schema(json_schema(schema)),
        }
    return config


# A fenced block, with or without a language tag (MIYA).
_FENCED: Final = re.compile(r"\A\s*```[^\n]*\n(?P<body>.*?)\n?```\s*\Z", re.DOTALL)


def json_payload(text: str) -> str:
    """The JSON object inside whatever the model wrapped it in — unwrapping
    only, never repair (MIYA)."""
    fenced = _FENCED.match(text)
    if fenced:
        return fenced.group("body")
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def parse_answer[S: BaseModel](schema: type[S], text: str) -> S | None:
    """The answer as ``schema``, or None when it does not validate."""
    if not text.strip():
        return None
    try:
        return schema.model_validate_json(json_payload(text))
    except ValidationError as exc:
        log.warning(
            "the model's answer does not validate",
            extra={"schema": schema.__name__, "errors": exc.error_count()},
        )
        return None


def text_of(response: Any) -> str:
    return "".join(
        getattr(block, "text", "")
        for block in getattr(response, "content", None) or ()
        if getattr(block, "type", "") == "text"
    )


def estimate_usd(model: str, *, chars: int, max_tokens: int) -> Decimal | None:
    """A claim no answer can exceed: every prompt character uncached at
    ``CHARS_PER_TOKEN``, the output at ``max_tokens``. None = unpriced."""
    return anthropic_cost_usd(
        model,
        input_tokens=math.ceil(chars / CHARS_PER_TOKEN),
        output_tokens=max_tokens,
    )


# --- one call ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelResult[S: BaseModel]:
    """What ``call`` hands back. ``refused`` = nothing was sent (a budget
    refusal before the first dispatch, or no client): the caller may simply
    try again later. ``error``: ``refusal`` / ``max_tokens`` / ``invalid`` /
    ``validator`` (final), ``api:<class>``, ``refused:<code>``,
    ``unavailable``; an applied job flagged by hygiene says ``pii_leak``."""

    status: Literal["applied", "failed"]
    model: str
    job_id: int | None = None
    output: S | None = None
    validated: Validated | None = None
    error: str | None = None
    refused: bool = False
    calls: int = 0
    cost_usd: Decimal = Decimal(0)
    stop_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "applied"

    @property
    def final(self) -> bool:
        """An answer that would not change if asked again (applied, refused
        by the model, malformed twice): the caller does not retry."""
        return self.ok or self.error in FINAL_ERRORS


@dataclass(slots=True)
class _Call:
    """The state of one ``call``: what it asks, and what it has spent."""

    op: Operation
    schema: type[BaseModel]
    system: str
    model: str
    effort: Effort
    tools: Sequence[Mapping[str, Any]] | None
    company_id: uuid.UUID | None
    job_id: int
    now: datetime
    calls: int = 0
    spent: list[Decimal] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Answer:
    stop_reason: str | None
    parsed: BaseModel | None


async def _send(client: anthropic.AsyncAnthropic, params: dict[str, Any]) -> Any:
    """The request itself: reached only through ``costs.ask``'s ``call=``."""
    return await client.messages.create(**params)


async def _request(
    session: AsyncSession,
    state: _Call,
    client: anthropic.AsyncAnthropic,
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    structured: bool,
) -> _Answer:
    """One paid request, claimed and priced by ``costs.ask``."""
    system = system_block(state.system, state.schema, with_schema=not structured)
    params: dict[str, Any] = {
        "model": state.model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "output_config": output_config(
            state.schema, effort=state.effort, structured=structured
        ),
    }
    if state.tools:
        params["tools"] = [dict(t) for t in state.tools]
    chars = len(system[0]["text"]) + sum(len(str(m["content"])) for m in messages)
    est = estimate_usd(state.model, chars=chars, max_tokens=max_tokens)

    def price(response: Any) -> Priced:
        priced = priced_anthropic(
            state.model,
            getattr(response, "usage", None),
            model_job_id=state.job_id,
            company_id=state.company_id,
        )
        state.spent.append(
            priced.cost_usd if priced.cost_usd is not None else est or Decimal(0)
        )
        return priced

    state.calls += 1
    try:
        response = await costs.ask(
            session,
            operation=state.op,
            est_usd=est,
            call=functools.partial(_send, client, params),
            price=price,
            now=state.now,
            company_id=state.company_id,
            model_job_id=state.job_id,
        )
    except costs.CostCapReached:
        state.calls -= 1  # refused before dispatch: nothing was sent
        raise
    except Exception:
        state.spent.append(est or Decimal(0))  # the claim keeps the estimate
        raise
    return _Answer(
        getattr(response, "stop_reason", None),
        parse_answer(state.schema, text_of(response)),
    )


async def _turn(
    session: AsyncSession,
    state: _Call,
    client: anthropic.AsyncAnthropic,
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> _Answer:
    """Ask once, by whichever route is usable: the structured path, or — once
    the server refused this schema's grammar — the prompted one (MIYA's
    ``_one_attempt``: the refusal costs one call per process, not one each)."""
    if structured_output_available(state.schema):
        try:
            return await _request(
                session, state, client, messages, max_tokens, structured=True
            )
        except API_FAILURES as exc:
            if not is_grammar_failure(exc):
                raise
            note_grammar_failure(state.schema, exc)
    return await _request(session, state, client, messages, max_tokens, structured=False)


def _request_record(
    op: Operation,
    *,
    model: str,
    system: str,
    user: Pseudonymised,
    max_tokens: int,
    effort: Effort,
    prompt_version: int | None,
    tools: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """``model_jobs.request``: the masked user text and what it was asked
    with — the frozen system block by version and hash, not copied into
    every row."""
    return {
        "op": op,
        "model": model,
        "prompt_version": prompt_version,
        "system_sha256": hashlib.sha256(system.encode()).hexdigest()[:16],
        "max_tokens": max_tokens,
        "effort": effort,
        "user": user.text,
        "tools": [dict(t) for t in tools] if tools else None,
    }


async def call[S: BaseModel](
    session: AsyncSession,
    *,
    op: Operation,
    schema: type[S],
    system: str,
    user: Pseudonymised,
    now: datetime,
    model: str | None = None,
    max_tokens: int | None = None,
    effort: Effort | None = None,
    tools: Sequence[Mapping[str, Any]] | None = None,
    company_id: uuid.UUID | None = None,
    evidence_ids: Iterable[int] = (),
    prompt_version: int | None = None,
    validate: Callable[[S], Validated] | None = None,
) -> ModelResult[S]:
    """One instant-lane model call (04 §4.1), recorded as a ``model_jobs`` row
    in the caller's transaction. ``user`` is masked text (``pseudonymise``);
    ``validate`` turns a schema-valid answer into what is stored
    (``validators``). Never raises for the model's sake: a refusal, a budget
    refusal or an API failure comes back as ``error``. Raises
    ``UnmaskedPayload`` — before anything is written or sent — when the
    request still carries a phone, a handle or an e-mail."""
    if not isinstance(user, Pseudonymised):
        raise TypeError("user must be a Pseudonymised text (06 §5.2)")
    route = ROUTES[op]
    model = model or model_for(op)
    tokens = max_tokens or route.max_tokens
    effort = effort or route.effort
    assert_masked(system)
    assert_masked(user.text)
    if tools:
        assert_masked(json.dumps(list(tools), ensure_ascii=False))
    try:
        client = get_client()
    except AnthropicUnavailable:
        log.warning("model call skipped: no client", extra={"op": op})
        return ModelResult("failed", model, error=UNAVAILABLE, refused=True)
    job = ModelJob(
        kind=route.kind,
        lane="instant",
        model=model,
        custom_id=f"cs-{uuid.uuid4()}",
        status="pending",
        attempts=0,
        company_id=company_id,
        evidence_ids=list(evidence_ids),
        request=_request_record(
            op,
            model=model,
            system=system,
            user=user,
            max_tokens=tokens,
            effort=effort,
            prompt_version=prompt_version,
            tools=tools,
        ),
        pseudonyms=dict(user.reverse) or None,
        created_at=now,
    )
    session.add(job)
    await session.flush()
    state = _Call(op, schema, system, model, effort, tools, company_id, job.id, now)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user.text}]
    retried = False
    while True:
        try:
            answer = await _turn(session, state, client, messages, tokens)
        except costs.CostCapReached as exc:
            return await _finish(
                session,
                job,
                state,
                error=costs.REFUSED_PREFIX + exc.code,
                refused=state.calls == 0,
            )
        except API_FAILURES as exc:
            code = API_PREFIX + type(exc).__name__
            await _note_api_failure(session, now, code)
            return await _finish(session, job, state, error=code)
        if answer.stop_reason == REFUSAL:
            return await _finish(session, job, state, error=REFUSAL, stop=REFUSAL)
        if answer.stop_reason == MAX_TOKENS or answer.parsed is None:
            if retried:
                error = MAX_TOKENS if answer.stop_reason == MAX_TOKENS else INVALID
                return await _finish(
                    session, job, state, error=error, stop=answer.stop_reason
                )
            retried = True
            if answer.stop_reason == MAX_TOKENS:
                tokens *= 2
            else:
                messages = [*messages, {"role": "user", "content": JSON_NUDGE}]
            continue
        output: S = answer.parsed  # type: ignore[assignment]
        await health.clear_problem(session, "anthropic_failing")
        try:
            checked = (
                validate(output)
                if validate is not None
                else Validated(result=output.model_dump(mode="json"))
            )
        except Exception as exc:
            # A validator's bug, not the model's: asked again, the same answer
            # would fail the same way at the same price — a final failure.
            log.error(
                "validator failed",
                extra={"op": op, "model_job_id": job.id, "error": type(exc).__name__},
            )
            return await _finish(
                session, job, state, error=VALIDATOR, stop=answer.stop_reason
            )
        return await _finish(
            session,
            job,
            state,
            output=output,
            validated=checked,
            stop=answer.stop_reason,
        )


async def _finish[S: BaseModel](
    session: AsyncSession,
    job: ModelJob,
    state: _Call,
    *,
    error: str | None = None,
    refused: bool = False,
    output: S | None = None,
    validated: Validated | None = None,
    stop: str | None = None,
) -> ModelResult[S]:
    applied = validated is not None
    spent = sum(state.spent, Decimal(0))
    job.status = "applied" if applied else "failed"
    job.attempts = min(state.calls, ATTEMPTS_MAX)
    job.cost_usd = spent
    if applied:
        assert validated is not None
        job.result = validated.result
        job.applied_at = state.now
        if validated.leaks:
            job.error = PII_LEAK
    else:
        job.error = error
    await session.flush()
    if applied and validated is not None and validated.leaks:
        await _note_pii_leak(session, state, validated.leaks)
    log.info(
        "model job",
        extra={
            "op": state.op,
            "model_job_id": job.id,
            "status": job.status,
            "error": job.error,
            "calls": state.calls,
        },
    )
    return ModelResult(
        status="applied" if applied else "failed",
        model=state.model,
        job_id=job.id,
        output=output,
        validated=validated,
        error=job.error,
        refused=refused,
        calls=state.calls,
        cost_usd=spent,
        stop_reason=stop,
    )


# --- 06 §4.2: what the lane reports ---------------------------------------------


async def failed_attempts(session: AsyncSession, now: datetime) -> int:
    """Anthropic attempts that failed after dispatch in the last hour (the
    claims ``costs.ask`` marked ``failed:<error>``)."""
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(UsageLog)
        .where(
            UsageLog.provider == "anthropic",
            UsageLog.model.startswith(costs.FAILED_PREFIX, autoescape=True),
            UsageLog.created_at > now - FAILING_WINDOW,
            UsageLog.created_at <= now,
        )
    )
    return int(count or 0)


async def _note_api_failure(session: AsyncSession, now: datetime, code: str) -> None:
    """``anthropic_failing`` once ``FAILING_MIN`` attempts failed within the
    hour; it lapses an hour after the last one, and a success clears it."""
    failed = await failed_attempts(session, now)
    if failed >= FAILING_MIN:
        await health.raise_problem(
            session,
            "anthropic_failing",
            now=now,
            by="extraction",
            detail={"code": code, "n": failed},
            until=now + FAILING_WINDOW,
        )


async def _note_pii_leak(
    session: AsyncSession, state: _Call, fields: Sequence[str]
) -> None:
    """``pii_leak``: the jobs of the last day a validator flagged, this one
    included (the field was dropped; 04 §4.7)."""
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ModelJob)
        .where(
            ModelJob.error == PII_LEAK,
            ModelJob.created_at > state.now - PII_LEAK_HOLD,
        )
    )
    log.error(
        "a model answer carried personal data; the field was dropped",
        extra={"op": state.op, "model_job_id": state.job_id, "fields": list(fields)},
    )
    await health.raise_problem(
        session,
        PII_LEAK,
        now=state.now,
        by="extraction",
        detail={"n": int(count or 0), "op": state.op},
        until=state.now + PII_LEAK_HOLD,
    )


__all__ = [
    "API_FAILURES",
    "FINAL_ERRORS",
    "ROUTES",
    "AnthropicUnavailable",
    "ModelResult",
    "Route",
    "available",
    "call",
    "estimate_usd",
    "get_client",
    "is_grammar_failure",
    "json_payload",
    "model_for",
    "note_grammar_failure",
    "output_config",
    "parse_answer",
    "reset_grammar_memory",
    "set_client",
    "structured_output_available",
    "system_block",
]
