"""The ``model_jobs`` lifecycle and the batch lane (04 §4.8, 07 T-16), on
Postgres, over the fake Messages and Message Batches API
(``tests/fakes.FakeAnthropic``; every answer SYNTHETIC,
``tests/fixtures/model/README.md``).

pending → submitted → applied / failed / fallback; the provisional
``usage_log`` row settled in place; results keyed by ``custom_id``; the
ladder (errored / expired / canceled / missing / invalid → pending, the third
attempt → fallback → the instant lane within the verdict cap); ``batch_stuck``
after 26 h and the next night's give-up; the batch sized to its budget; and
no raw personal data reaching the batch API.

The kind the lane carries is a stand-in registered for these tests: the real
``company_verdict`` schema, prompt and effects are T-30's (``RelevanceVerdict``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.services import costs, extraction, health, model_jobs, validators
from clientscrapper.services.model_jobs import BatchKind, Landed
from clientscrapper.services.pseudonyms import (
    Pseudonymised,
    UnmaskedPayload,
    pseudonymise,
)
from clientscrapper.services.validators import Validated
from tests.fakes import (
    api_error,
    batch_pii_in,
    canceled,
    errored,
    expired,
    message,
    model_fixture,
    overloaded,
    pii_in,
    succeeded,
)

TZ = ZoneInfo("Asia/Tashkent")
NIGHT = datetime(2026, 10, 24, 23, 30, tzinfo=TZ)  # model_batch_submit (04 §0)
COLLECT = datetime(2026, 10, 25, 0, 30, tzinfo=TZ)  # the first collect
BATCH = settings.batch_model
VERDICT = settings.verdict_model
CONFIRMED = model_fixture("batch_verdict_confirmed.json")
LIKELY = model_fixture("batch_verdict_likely.json")
UNLIKELY = model_fixture("batch_verdict_unlikely.json")
LEAK = model_fixture("batch_verdict_leak.json")
# A succeeded batch request of 4,000 input, 600 output and 900 cache-read
# tokens on the batch model's table price ($4 / $20 / $0.20 per million),
# halved by the Message Batches discount: (0.016 + 0.012 + 0.00018) / 2.
SETTLED = Decimal("0.014090")
# The instant fallback's answer (fakes.message's 300 in, 150 out, 900 cache
# reads) on the verdict model ($2 / $10 / $0.20 per million), full price.
INSTANT = Decimal("0.002280")
SYSTEM = (
    "You read the evidence about one company in Uzbekistan and decide whether "
    "it brings goods from China. Return one Verdict object."
)


class Verdict(BaseModel):
    """Whether one company imports from China (a test stand-in)."""

    model_config = ConfigDict(extra="forbid")

    imports_from_china: Literal["confirmed", "likely", "unlikely", "no"]
    reasons: list[str]
    confidence: float


class Kind:
    """The registered stand-in kind and what its checker and effects saw."""

    def __init__(self) -> None:
        self.checked: list[int] = []
        self.landed: list[Landed] = []
        self.fail_apply = False

    async def checker(self, session: AsyncSession, job: m.ModelJob) -> Any:
        self.checked.append(job.id)

        def check(answer: Verdict) -> Validated:
            kept = [r for r in answer.reasons if not validators.leaks(r)]
            dropped = ("reasons",) if len(kept) != len(answer.reasons) else ()
            result = answer.model_dump(mode="json") | {"reasons": kept}
            return Validated(result=result, leaks=dropped)

        return check

    async def apply(self, session: AsyncSession, landed: Landed, now: datetime) -> None:
        self.landed.append(landed)
        if landed.company_id is not None:
            await session.execute(
                sa.update(m.Company)
                .where(m.Company.id == landed.company_id)
                .values(verdict=landed.validated.result, verdict_at=now)
            )
        if self.fail_apply:
            raise RuntimeError("synthetic effects failure")

    def batch_kind(self) -> BatchKind:
        return BatchKind(
            schema=Verdict,
            system=SYSTEM,
            prompt_version=1,
            checker=self.checker,
            apply=self.apply,
        )


@pytest.fixture(autouse=True)
def kind(monkeypatch: pytest.MonkeyPatch) -> Any:
    registered = Kind()
    monkeypatch.setitem(model_jobs.KINDS, "company_verdict", registered.batch_kind())
    costs.clear_refusals()
    yield registered
    costs.clear_refusals()


# --- helpers ------------------------------------------------------------------


async def company(session: AsyncSession, name: str = "Namuna Savdo") -> uuid.UUID:
    row = m.Company(display_name=name, name_key=name.lower())
    session.add(row)
    await session.flush()
    return row.id


async def enqueue(
    session: AsyncSession,
    text: str = "Namuna Savdo MChJ: Yiwudan konteyner keldi",
    *,
    company_id: uuid.UUID | None = None,
    at: datetime = NIGHT - timedelta(hours=2),
) -> int:
    job = await model_jobs.enqueue(
        session, user=pseudonymise(text), now=at, company_id=company_id
    )
    await session.commit()
    return job.id


async def jobs(session: AsyncSession) -> list[m.ModelJob]:
    return list(
        await session.scalars(
            sa.select(m.ModelJob)
            .order_by(m.ModelJob.id)
            .execution_options(populate_existing=True)
        )
    )


async def job(session: AsyncSession, job_id: int) -> m.ModelJob:
    [found] = [j for j in await jobs(session) if j.id == job_id]
    return found


async def ledger(session: AsyncSession) -> list[m.UsageLog]:
    return list(
        await session.scalars(
            sa.select(m.UsageLog)
            .order_by(m.UsageLog.id)
            .execution_options(populate_existing=True)
        )
    )


def estimate(params: dict[str, Any]) -> Decimal | None:
    """What the lane claims for one sent request (extraction.estimate_usd)."""
    return extraction.estimate_usd(
        params["model"],
        prompt_bytes=extraction.request_bytes(params),
        max_tokens=params["max_tokens"],
        batch=True,
    )


def sent_ids(fake: Any, index: int = -1) -> list[str]:
    return [r["custom_id"] for r in fake.batch_bodies[index]["requests"]]


async def night(
    session: AsyncSession, fake: Any, *, at: datetime, results: dict[str, Any]
) -> None:
    """One night: the batch sent at ``at``, ended with ``results`` (custom_id →
    result line), collected an hour later."""
    fake.accept_batches()
    submitted = await model_jobs.submit_batch(now=at)
    assert submitted.submitted == len(results)
    fake.end(fake.batch().id, results)
    await model_jobs.collect(now=at + timedelta(hours=1))


# --- pending → submitted → applied --------------------------------------------


async def test_a_job_goes_pending_submitted_applied_with_its_usage_settled_in_place(
    session, model_api, kind
):
    firm = await company(session)
    first = await enqueue(session, company_id=firm)
    second = await enqueue(session, "Namuna Kafel: optom kafel va santexnika")
    a, b = await jobs(session)
    assert (a.status, a.lane, a.kind, a.model, a.attempts, a.batch_id) == (
        "pending",
        "batch",
        "company_verdict",
        BATCH,
        0,
        None,
    )
    assert a.custom_id.startswith("cs-") and len(a.custom_id) == 39
    assert a.request is not None
    assert a.request["op"] == "company_verdict" and a.request["prompt_version"] == 1

    model_api.accept_batches()
    report = await model_jobs.submit_batch(now=NIGHT)
    assert (report.claimed, report.submitted, report.held) == (2, 2, 0)
    [body] = model_api.batch_bodies
    assert sent_ids(model_api) == [a.custom_id, b.custom_id]
    params = body["requests"][0]["params"]
    assert params["model"] == BATCH and params["max_tokens"] == 1500
    assert params["output_config"]["effort"] == "low"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["system"] == [
        {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]
    assert params["messages"] == [
        {"role": "user", "content": "Namuna Savdo MChJ: Yiwudan konteyner keldi"}
    ]
    claims = [estimate(r["params"]) for r in body["requests"]]

    a, b = await jobs(session)
    for row in (a, b):
        assert (row.status, row.batch_id, row.submitted_at, row.attempts) == (
            "submitted",
            "msgbatch_namuna01",
            NIGHT,
            1,
        )
    provisional = await ledger(session)
    assert [
        (r.model_job_id, r.operation, r.model, r.batch, r.input_tokens, r.cost_usd)
        for r in provisional
    ] == [
        (first, "company_verdict", BATCH, True, None, claims[0]),
        (second, "company_verdict", BATCH, True, None, claims[1]),
    ]
    assert provisional[0].company_id == firm
    assert (a.cost_usd, b.cost_usd) == (claims[0], claims[1])

    # Still running at the first collect: nothing moves.
    running = await model_jobs.collect(now=COLLECT)
    assert (running.running, running.batches, running.applied) == (1, 0, 0)
    assert [j.status for j in await jobs(session)] == ["submitted", "submitted"]
    assert model_api.retrieved == ["msgbatch_namuna01"]

    model_api.end(
        "msgbatch_namuna01",
        [
            (b.custom_id, succeeded(LIKELY, input_tokens=4000, output_tokens=600)),
            (a.custom_id, succeeded(CONFIRMED, input_tokens=4000, output_tokens=600)),
        ],
    )
    collected = COLLECT + timedelta(minutes=30)
    done = await model_jobs.collect(now=collected)
    assert (done.batches, done.applied, done.failed, done.retried) == (1, 2, 0, 0)

    a, b = await jobs(session)
    assert (a.status, a.applied_at, a.error, a.attempts) == (
        "applied",
        collected,
        None,
        1,
    )
    assert a.result == CONFIRMED
    assert b.result == LIKELY
    settled = await ledger(session)
    assert [r.id for r in settled] == [r.id for r in provisional]  # in place
    assert [
        (r.model_job_id, r.input_tokens, r.output_tokens, r.cost_usd, r.batch)
        for r in settled
    ] == [(first, 4000, 600, SETTLED, True), (second, 4000, 600, SETTLED, True)]
    assert {r.created_at for r in settled} == {NIGHT}  # the claim's day
    assert (a.cost_usd, b.cost_usd) == (SETTLED, SETTLED)
    # The effects ran in the job's transaction, with the batch job's answer.
    assert [(x.job_id, x.batch_job_id, x.lane, x.model) for x in kind.landed] == [
        (first, first, "batch", BATCH),
        (second, second, "batch", BATCH),
    ]
    firm_row = await session.get(m.Company, firm, populate_existing=True)
    assert firm_row is not None
    assert (firm_row.verdict, firm_row.verdict_at) == (CONFIRMED, collected)
    # Collected once: a later poll finds nothing to read.
    again = await model_jobs.collect(now=collected + timedelta(minutes=30))
    assert (again.batches, again.running) == (0, 0)


async def test_results_are_keyed_by_custom_id_never_by_position(session, model_api):
    ids = [
        await enqueue(session, text)
        for text in ("Namuna Bir: optom", "Namuna Ikki: sklad", "Namuna Uch: zavod")
    ]
    model_api.accept_batches()
    await model_jobs.submit_batch(now=NIGHT)
    one, two, three = await jobs(session)
    model_api.end(
        "msgbatch_namuna01",
        [
            ("cs-not-ours", succeeded(CONFIRMED)),
            (three.custom_id, succeeded(UNLIKELY)),
            (one.custom_id, succeeded(CONFIRMED)),
            (two.custom_id, succeeded(LIKELY)),
            (one.custom_id, succeeded(UNLIKELY)),  # a second line: the first wins
        ],
    )
    report = await model_jobs.collect(now=COLLECT)
    assert report.applied == 3
    one, two, three = await jobs(session)
    assert [j.id for j in (one, two, three)] == ids
    assert [j.result["imports_from_china"] for j in (one, two, three)] == [
        "confirmed",
        "likely",
        "unlikely",
    ]


# --- failed -------------------------------------------------------------------


async def test_a_refusal_is_failed_never_retried_and_its_tokens_are_priced(
    session, model_api
):
    first = await enqueue(session)
    await night(
        session,
        model_api,
        at=NIGHT,
        results={
            (await job(session, first)).custom_id: succeeded(
                "I can't help with that.",
                stop_reason="refusal",
                input_tokens=4000,
                output_tokens=600,
            )
        },
    )
    row = await job(session, first)
    assert (row.status, row.error, row.attempts, row.result) == (
        "failed",
        "refusal",
        1,
        None,
    )
    [usage] = await ledger(session)
    assert (usage.input_tokens, usage.cost_usd) == (4000, SETTLED)
    # Never asked again, by either lane.
    assert (await model_jobs.take_fallbacks(now=COLLECT)).claimed == 0
    model_api.accept_batches()
    assert (await model_jobs.submit_batch(now=NIGHT + timedelta(days=1))).claimed == 0
    assert model_api.requests == []


async def test_a_validator_s_bug_is_a_final_failure(session, model_api, kind):
    async def broken(session: AsyncSession, job: m.ModelJob) -> Any:
        def check(answer: Verdict) -> Validated:
            raise KeyError("synthetic bug")

        return check

    model_jobs.KINDS["company_verdict"] = BatchKind(
        schema=Verdict, system=SYSTEM, checker=broken, apply=kind.apply
    )
    first = await enqueue(session)
    custom = (await job(session, first)).custom_id
    await night(session, model_api, at=NIGHT, results={custom: succeeded(CONFIRMED)})
    row = await job(session, first)
    assert (row.status, row.error) == ("failed", "validator")
    assert kind.landed == []


async def test_effects_that_raise_fail_the_job_and_keep_the_answer(
    session, model_api, kind
):
    kind.fail_apply = True
    firm = await company(session)
    first = await enqueue(session, company_id=firm)
    custom = (await job(session, first)).custom_id
    await night(session, model_api, at=NIGHT, results={custom: succeeded(CONFIRMED)})
    row = await job(session, first)
    assert (row.status, row.error, row.result) == ("failed", "apply", CONFIRMED)
    # The savepoint took the effects with it: the company is untouched.
    firm_row = await session.get(m.Company, firm, populate_existing=True)
    assert firm_row is not None and firm_row.verdict is None


async def test_a_leaking_answer_is_applied_without_the_field_and_flagged(
    session, model_api
):
    first = await enqueue(session)
    custom = (await job(session, first)).custom_id
    await night(session, model_api, at=NIGHT, results={custom: succeeded(LEAK)})
    row = await job(session, first)
    assert (row.status, row.error) == ("applied", "pii_leak")
    assert row.result == LEAK | {"reasons": ["optom mebel"]}
    problems = await health.reported_problems(session, COLLECT + timedelta(hours=2))
    assert problems["pii_leak"].detail == {"n": 1, "op": "company_verdict"}
    assert problems["pii_leak"].by == "model_jobs"


# --- the ladder and the fallback ----------------------------------------------


async def test_errored_expired_canceled_back_to_pending_then_the_instant_lane(
    session, model_api, kind
):
    firm = await company(session)
    first = await enqueue(session, company_id=firm)
    custom = (await job(session, first)).custom_id

    # Night 1: errored → pending for the next night.
    await night(
        session,
        model_api,
        at=NIGHT,
        results={custom: errored("overloaded_error", "Overloaded")},
    )
    row = await job(session, first)
    assert (row.status, row.attempts, row.error, row.batch_id, row.submitted_at) == (
        "pending",
        1,
        "errored:overloaded_error",
        None,
        None,
    )
    # Night 2: expired.
    await night(
        session, model_api, at=NIGHT + timedelta(days=1), results={custom: expired()}
    )
    row = await job(session, first)
    assert (row.status, row.attempts, row.error) == ("pending", 2, "expired")
    # Night 3: canceled — the third attempt: the batch lane gives up.
    await night(
        session, model_api, at=NIGHT + timedelta(days=2), results={custom: canceled()}
    )
    row = await job(session, first)
    assert (row.status, row.attempts, row.error, row.batch_id) == (
        "fallback",
        3,
        "canceled",
        "msgbatch_namuna03",
    )
    assert row.applied_at is None  # not taken yet
    # One custom_id, three batches; three provisional rows kept at the estimate
    # (no usage came back for them).
    assert [sent_ids(model_api, i) for i in range(3)] == [[custom]] * 3
    claims = [estimate(b["requests"][0]["params"]) for b in model_api.batch_bodies]
    rows = await ledger(session)
    assert [(r.model_job_id, r.input_tokens, r.cost_usd) for r in rows] == [
        (first, None, claims[0]),
        (first, None, claims[1]),
        (first, None, claims[2]),
    ]
    assert row.cost_usd == sum(claims, Decimal(0))

    # The instant lane takes it: the verdict model, within the verdict cap.
    model_api.queue(message(CONFIRMED, model=VERDICT))
    taken_at = NIGHT + timedelta(days=3, minutes=-30)
    report = await model_jobs.take_fallbacks(now=taken_at)
    assert (report.taken, report.applied, report.failed) == (1, 1, 0)
    [body] = model_api.requests
    assert body["model"] == VERDICT and body["max_tokens"] == 1200
    assert body["messages"] == [
        {"role": "user", "content": "Namuna Savdo MChJ: Yiwudan konteyner keldi"}
    ]
    assert body["system"][0]["text"] == SYSTEM
    fallback, instant = await jobs(session)
    assert (fallback.status, fallback.applied_at, fallback.attempts) == (
        "fallback",
        taken_at,
        3,
    )
    assert (instant.kind, instant.lane, instant.model, instant.status) == (
        "verdict",
        "instant",
        VERDICT,
        "applied",
    )
    assert instant.request is not None and instant.request["fallback_of"] == first
    assert instant.result == CONFIRMED and instant.cost_usd == INSTANT
    assert [j.id for j in await model_jobs.fallback_jobs(session, first)] == [instant.id]
    [x] = kind.landed
    assert (x.job_id, x.batch_job_id, x.lane, x.model, x.company_id) == (
        instant.id,
        first,
        "instant",
        VERDICT,
        firm,
    )
    *_, last = await ledger(session)
    assert (last.operation, last.model, last.batch, last.cost_usd) == (
        "verdict",
        VERDICT,
        False,
        INSTANT,
    )
    # Taken once: never handed over again.
    assert (await model_jobs.take_fallbacks(now=taken_at)).claimed == 0


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("this is not json", "invalid"),
        (CONFIRMED, "max_tokens"),
    ],
)
async def test_an_answer_that_does_not_land_takes_the_ladder(
    session, model_api, answer, reason
):
    first = await enqueue(session)
    custom = (await job(session, first)).custom_id
    stop = "max_tokens" if reason == "max_tokens" else "end_turn"
    await night(
        session,
        model_api,
        at=NIGHT,
        results={
            custom: succeeded(
                answer, stop_reason=stop, input_tokens=4000, output_tokens=600
            )
        },
    )
    row = await job(session, first)
    assert (row.status, row.attempts, row.error) == ("pending", 1, reason)
    # The answer was paid for: its row is settled all the same.
    [usage] = await ledger(session)
    assert (usage.input_tokens, usage.cost_usd) == (4000, SETTLED)


async def test_a_result_the_stream_left_out_takes_the_ladder(session, model_api):
    first = await enqueue(session)
    second = await enqueue(session, "Namuna Ikki: optom")
    one = await job(session, first)
    model_api.accept_batches()
    await model_jobs.submit_batch(now=NIGHT)
    model_api.end(model_api.batch().id, {one.custom_id: succeeded(CONFIRMED)})
    report = await model_jobs.collect(now=COLLECT)
    assert (report.applied, report.retried) == (1, 1)
    a, b = await jobs(session)
    assert (a.status, b.status, b.error, b.attempts) == (
        "applied",
        "pending",
        "missing",
        1,
    )
    assert b.id == second


async def test_an_ended_batch_with_no_results_puts_every_job_on_the_ladder(
    session, model_api
):
    first = await enqueue(session)
    model_api.accept_batches()
    await model_jobs.submit_batch(now=NIGHT)
    model_api.batch().status = "ended"  # ended, and no results_url (archived)
    report = await model_jobs.collect(now=COLLECT)
    assert report.batches == 1
    row = await job(session, first)
    assert (row.status, row.error) == ("pending", "unavailable")


async def test_a_results_stream_that_fails_is_read_again_later(session, model_api):
    first = await enqueue(session)
    model_api.accept_batches()
    await model_jobs.submit_batch(now=NIGHT)
    custom = (await job(session, first)).custom_id
    model_api.end(model_api.batch().id, {custom: succeeded(CONFIRMED)})
    model_api.results_failures[model_api.batch().id] = [overloaded()]
    failed = await model_jobs.collect(now=COLLECT)
    assert (failed.unreadable, failed.applied) == (1, 0)
    assert (await job(session, first)).status == "submitted"
    read = await model_jobs.collect(now=COLLECT + timedelta(minutes=30))
    assert read.applied == 1
    assert (await job(session, first)).status == "applied"


async def test_a_fallback_the_verdict_cap_refuses_keeps_nothing_and_waits(
    session, model_api, monkeypatch
):
    first = await enqueue(session)
    row = await job(session, first)
    for day in range(3):
        await night(
            session,
            model_api,
            at=NIGHT + timedelta(days=day),
            results={row.custom_id: expired()},
        )
    assert (await job(session, first)).status == "fallback"
    at = NIGHT + timedelta(days=3, hours=1)
    # The verdict op is out of room today: nothing is sent, nothing kept.
    room = {op: costs.ALLOCATIONS[op] for op in ("verdict", "model_reserve")}
    monkeypatch.setitem(costs.ALLOCATIONS, "verdict", Decimal("0"))
    monkeypatch.setitem(costs.ALLOCATIONS, "model_reserve", Decimal("0"))
    refused = await model_jobs.take_fallbacks(now=at)
    assert (refused.refused, refused.taken) == (1, 0)
    assert model_api.requests == []
    assert [j.lane for j in await jobs(session)] == ["batch"]
    assert (await job(session, first)).applied_at is None
    refusal = (await ledger(session))[-1]
    assert (refusal.operation, refusal.model) == (
        "verdict",
        "refused:cost_cap_op:verdict",
    )
    # Room again (the next day, the memory cleared): taken.
    for op, allocation in room.items():
        monkeypatch.setitem(costs.ALLOCATIONS, op, allocation)
    costs.clear_refusals()
    model_api.queue(message(LIKELY, model=VERDICT))
    later = at + timedelta(days=1)
    taken = await model_jobs.take_fallbacks(now=later)
    assert (taken.taken, taken.applied) == (1, 1)
    assert (await job(session, first)).applied_at == later


async def test_a_fallback_the_api_keeps_failing_is_given_up_after_three(
    session, model_api
):
    first = await enqueue(session)
    row = await job(session, first)
    for day in range(3):
        await night(
            session,
            model_api,
            at=NIGHT + timedelta(days=day),
            results={row.custom_id: expired()},
        )
    at = NIGHT + timedelta(days=3, hours=1)
    for n in range(3):
        model_api.queue(overloaded())
        report = await model_jobs.take_fallbacks(now=at + timedelta(minutes=30 * n))
        assert report.retried == 1
        expected = at + timedelta(minutes=60) if n == 2 else None
        assert (await job(session, first)).applied_at == expected
    assert [j.error for j in await jobs(session)][1:] == ["api:OverloadedError"] * 3
    # Given up: the run claims it no more.
    assert (await model_jobs.take_fallbacks(now=at + timedelta(hours=3))).claimed == 0


# --- the submit ---------------------------------------------------------------


async def test_the_batch_holds_only_what_its_budget_fits(session, model_api, monkeypatch):
    # Five jobs of one length: one claim each of the same size.
    ids = [await enqueue(session, f"Namuna {n}: optom tovar") for n in range(5)]
    model_api.accept_batches(2)
    first = await model_jobs.submit_batch(now=NIGHT, limit=1)
    assert first.submitted == 1
    [claim] = [r.cost_usd for r in await ledger(session)]
    # The op's own allocation holds today's claim and 2½ more, no reserve to
    # borrow: two of the four still pending fit (spend + est < allocation).
    monkeypatch.setitem(costs.ALLOCATIONS, "company_verdict", claim * Decimal("3.5"))
    monkeypatch.setitem(costs.ALLOCATIONS, "model_reserve", Decimal("0"))
    report = await model_jobs.submit_batch(now=NIGHT + timedelta(minutes=1))
    assert (report.claimed, report.submitted, report.held) == (4, 2, 2)
    assert [estimate(r["params"]) for r in model_api.batch_bodies[1]["requests"]] == [
        claim,
        claim,
    ]
    assert [(j.id, j.status, j.batch_id) for j in await jobs(session)] == [
        (ids[0], "submitted", "msgbatch_namuna01"),
        (ids[1], "submitted", "msgbatch_namuna02"),
        (ids[2], "submitted", "msgbatch_namuna02"),
        (ids[3], "pending", None),
        (ids[4], "pending", None),
    ]
    # One claim row per submitted job; nothing claimed for the held ones.
    assert [r.model_job_id for r in await ledger(session)] == ids[:3]


async def test_a_batch_nothing_fits_is_refused_and_sends_nothing(
    session, model_api, monkeypatch
):
    first = await enqueue(session)
    monkeypatch.setitem(costs.ALLOCATIONS, "company_verdict", Decimal("0.0001"))
    monkeypatch.setitem(costs.ALLOCATIONS, "model_reserve", Decimal("0"))
    model_api.accept_batches()
    report = await model_jobs.submit_batch(now=NIGHT)
    assert (report.refused, report.submitted) == (1, 0)
    assert model_api.batch_bodies == []
    row = await job(session, first)
    assert (row.status, row.attempts) == ("pending", 0)
    [refusal] = await ledger(session)
    assert refusal.model == "refused:cost_cap_op:company_verdict"
    model_api.batch_answers.clear()  # the accepted create nobody made
    # Remembered for the hour: the next submit claims nothing.
    assert (await model_jobs.submit_batch(now=NIGHT + timedelta(minutes=5))).claimed == 0


async def test_a_create_the_api_refuses_costs_an_attempt_and_an_outage_does_not(
    session, model_api
):
    first = await enqueue(session)
    model_api.refuse_batch(api_error(400, "invalid_request_error", "requests: bad"))
    refused = await model_jobs.submit_batch(now=NIGHT)
    assert refused.retried == 1
    row = await job(session, first)
    assert (row.status, row.attempts, row.error) == ("pending", 1, "submit:400")
    costs.clear_refusals()
    model_api.refuse_batch(overloaded())
    outage = await model_jobs.submit_batch(now=NIGHT + timedelta(days=1))
    assert (outage.not_sent, outage.retried) == (1, 0)
    row = await job(session, first)
    assert (row.status, row.attempts, row.error) == ("pending", 1, "submit:400")
    # Both creates claimed their estimate and the failed ones say so.
    assert [r.model for r in await ledger(session)] == [
        "failed:BadRequestError",
        "failed:OverloadedError",
    ]


async def test_no_raw_personal_data_reaches_the_batch_api(session, model_api):
    text = (
        "Namuna Savdo, Rustam aka: Yiwudan 2 konteyner. Tel +998 90 000 00 01, "
        "@namuna_savdo, pochta namuna_01@example.uz"
    )
    masked = pseudonymise(text, names=["Rustam"])
    stored = await model_jobs.enqueue(
        session, user=masked, now=NIGHT - timedelta(hours=1)
    )
    await session.commit()
    assert stored.pseudonyms == masked.reverse
    model_api.accept_batches()
    await model_jobs.submit_batch(now=NIGHT)
    [body] = model_api.batch_bodies
    assert batch_pii_in(body) == []
    assert pii_in(body["requests"][0]["params"]) == []
    sent = body["requests"][0]["params"]["messages"][0]["content"]
    assert sent == (
        "Namuna Savdo, <NAME_1> aka: Yiwudan 2 konteyner. Tel <PHONE_1>, "
        "@<TG_1>, pochta <EMAIL_1>"
    )
    for secret in ("+998 90 000 00 01", "namuna_savdo", "namuna_01@example.uz", "Rustam"):
        assert secret not in str(body)
    # A text that is not masked is refused before anything is written.
    with pytest.raises(UnmaskedPayload):
        await model_jobs.enqueue(
            session, user=Pseudonymised("Tel +998 90 000 00 02"), now=NIGHT
        )
    with pytest.raises(TypeError):
        await model_jobs.enqueue(session, user="Namuna", now=NIGHT)  # type: ignore[arg-type]


async def test_a_stored_request_that_is_not_masked_is_never_sent(session, model_api):
    tampered = await enqueue(session, "Namuna Bir: optom")
    clean = await enqueue(session, "Namuna Ikki: optom")
    row = await job(session, tampered)
    row.request = {**(row.request or {}), "user": "Tel +998 90 000 00 03"}
    await session.commit()
    model_api.accept_batches()
    report = await model_jobs.submit_batch(now=NIGHT)
    assert (report.unmasked, report.submitted) == (1, 1)
    bad, good = await jobs(session)
    assert (bad.status, bad.error, bad.attempts) == ("failed", "unmasked", 0)
    assert sent_ids(model_api) == [good.custom_id] and good.id == clean


# --- batch_stuck --------------------------------------------------------------


async def test_a_batch_not_ended_after_26_hours_is_stuck_and_given_up_next_night(
    session, model_api
):
    first = await enqueue(session)
    model_api.accept_batches()
    await model_jobs.submit_batch(now=NIGHT)
    for minutes in (60, 8 * 60 + 30, 25 * 60, 26 * 60):  # 00:30, 08:00, 00:30, 01:30
        polled = await model_jobs.collect(now=NIGHT + timedelta(minutes=minutes))
        assert polled.running == 1
    assert await health.stuck_batches(session, NIGHT + timedelta(hours=26)) == ()
    status = await health.gather(
        session, now=NIGHT + timedelta(hours=26, minutes=1), disk=(10**12, 10**12)
    )
    assert status.stuck_batches == (("msgbatch_namuna01", NIGHT),)
    [stuck] = [p for p in health.problems(status) if p.key == "batch_stuck"]
    assert stuck.severity == "warning"
    assert stuck.text.startswith("⚠️ Tungi batch ")
    assert "(msgbatch_namuna01)" in stuck.text

    # The next night's submit: read once more (still running), given up, and
    # the job — attempt 1 of 3 — goes out again in tonight's batch.
    model_api.accept_batches()
    report = await model_jobs.model_batch_submit_job(now=NIGHT + timedelta(days=2))
    assert (report["stuck"], report["retried"], report["submitted"]) == (1, 1, 1)
    assert model_api.cancelled == ["msgbatch_namuna01"]
    row = await job(session, first)
    assert (row.status, row.batch_id, row.attempts, row.error) == (
        "submitted",
        "msgbatch_namuna02",
        2,
        "stuck",
    )
    assert sent_ids(model_api) == [row.custom_id]
    assert await health.stuck_batches(session, NIGHT + timedelta(days=2, hours=1)) == ()


async def test_a_stuck_batch_that_ended_meanwhile_is_collected_not_given_up(
    session, model_api
):
    first = await enqueue(session)
    model_api.accept_batches()
    await model_jobs.submit_batch(now=NIGHT)
    custom = (await job(session, first)).custom_id
    model_api.end("msgbatch_namuna01", {custom: succeeded(CONFIRMED)})
    report = await model_jobs.give_up_stuck(now=NIGHT + timedelta(days=2))
    assert (report.stuck, report.applied) == (0, 1)
    assert model_api.cancelled == []
    assert (await job(session, first)).status == "applied"


# --- the jobs -----------------------------------------------------------------


async def test_the_scheduled_jobs_run_the_lane_end_to_end(session, model_api):
    first = await enqueue(session)
    model_api.accept_batches()
    submitted = await model_jobs.model_batch_submit_job(now=NIGHT)
    assert (submitted["claimed"], submitted["submitted"]) == (1, 1)
    custom = (await job(session, first)).custom_id
    model_api.end("msgbatch_namuna01", {custom: succeeded(UNLIKELY)})
    collected = await model_jobs.model_batch_collect_job(now=COLLECT)
    assert (collected["batches"], collected["applied"]) == (1, 1)
    row = await job(session, first)
    assert (row.status, row.result) == ("applied", UNLIKELY)


async def test_nothing_is_submitted_for_an_operation_nobody_registered(
    session, model_api, monkeypatch
):
    with pytest.raises(ValueError):
        model_jobs.register("verdict", Kind().batch_kind())
    monkeypatch.delitem(model_jobs.KINDS, "company_verdict")
    with pytest.raises(ValueError):
        await model_jobs.enqueue(session, user=pseudonymise("Namuna"), now=NIGHT)
    report = await model_jobs.submit_batch(now=NIGHT)
    assert report.claimed == 0 and model_api.batch_bodies == []
