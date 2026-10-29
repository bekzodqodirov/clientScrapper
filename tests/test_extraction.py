"""``extraction.call`` — the instant lane (04 §4.1), on Postgres, over the fake
Messages API (tests/fakes.FakeAnthropic; every answer SYNTHETIC,
tests/fixtures/model/README.md).

The structured path, the grammar fallback, the JSON nudge, a refusal, the
``max_tokens`` retry, a budget refusal that sends nothing, the masking fence,
an API failure streak (``anthropic_failing``) and a leak in an answer
(``pii_leak``). Every request is one ``costs.ask(operation='classify')``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from anthropic.lib._parse._transform import transform_schema

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.services import costs, extraction, health
from clientscrapper.services.prompts import (
    CLASSIFY_PROMPT_VERSION,
    CLASSIFY_SYSTEM,
    JSON_NUDGE,
)
from clientscrapper.services.pseudonyms import (
    Pseudonymised,
    UnmaskedPayload,
    pseudonymise,
)
from clientscrapper.services.schemas import ImportSignal
from clientscrapper.services.usage import anthropic_cost_usd
from clientscrapper.services.validators import Validated
from tests.fakes import (
    api_error,
    grammar_failure,
    message,
    model_fixture,
    overloaded,
)

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)
ASK = "Guangzhoudan 2 kub mato kerak, kim olib keladi?"
USER = pseudonymise(
    "SOURCE: telegram group «Namuna Optom»\nDATE: 2026-10-24T11:58:00+05:00\n"
    f"SENDER_BIO: -\nTEXT: {ASK}"
)
BUYER = model_fixture("classify_buyer_stated.json")
# 300 input × $0.10 + 150 output × $0.50 + 900 cache reads × $0.01, per
# million tokens: the default extract model's table price (usage.py).
PRICED = Decimal("0.000114")


@pytest.fixture(autouse=True)
def _no_refusal_memory():
    costs.clear_refusals()
    yield
    costs.clear_refusals()


async def classify(session, *, user: Pseudonymised = USER, **fields):
    return await extraction.call(
        session,
        op="classify",
        schema=ImportSignal,
        system=CLASSIFY_SYSTEM,
        user=user,
        now=fields.pop("now", NOW),
        prompt_version=CLASSIFY_PROMPT_VERSION,
        **fields,
    )


async def jobs(session) -> list[m.ModelJob]:
    return list(
        await session.scalars(
            sa.select(m.ModelJob)
            .order_by(m.ModelJob.id)
            .execution_options(populate_existing=True)
        )
    )


async def ledger(session) -> list[m.UsageLog]:
    return list(
        await session.scalars(
            sa.select(m.UsageLog)
            .order_by(m.UsageLog.id)
            .execution_options(populate_existing=True)
        )
    )


def structured_format() -> dict:
    return {
        "type": "json_schema",
        "schema": transform_schema(ImportSignal.model_json_schema()),
    }


async def test_the_structured_path_is_one_masked_priced_request(session, model_api):
    model_api.queue(message(BUYER))
    result = await classify(session, evidence_ids=[7])
    await session.commit()

    assert (result.status, result.error, result.calls, result.refused) == (
        "applied",
        None,
        1,
        False,
    )
    assert result.output == ImportSignal.model_validate(BUYER)
    assert result.cost_usd == PRICED
    [body] = model_api.requests
    assert body["model"] == settings.extract_model
    assert body["max_tokens"] == 400
    assert body["system"] == [
        {"type": "text", "text": CLASSIFY_SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]
    assert body["messages"] == [{"role": "user", "content": USER.text}]
    assert body["output_config"] == {"effort": "low", "format": structured_format()}
    # Thinking stays adaptive; no sampling knobs, no prefill, no forced tool.
    assert not {"thinking", "temperature", "tool_choice", "tools"} & body.keys()

    [job] = await jobs(session)
    assert (job.kind, job.lane, job.model, job.status, job.attempts) == (
        "classify",
        "instant",
        settings.extract_model,
        "applied",
        1,
    )
    assert job.custom_id.startswith("cs-") and len(job.custom_id) == 39
    assert (job.evidence_ids, job.error, job.cost_usd, job.applied_at) == (
        [7],
        None,
        PRICED,
        NOW,
    )
    assert job.request["user"] == USER.text
    assert (job.request["op"], job.request["prompt_version"], job.request["effort"]) == (
        "classify",
        CLASSIFY_PROMPT_VERSION,
        "low",
    )
    assert job.result == BUYER
    assert job.pseudonyms is None  # nothing was masked in this text

    [row] = await ledger(session)
    assert (row.provider, row.operation, row.model, row.model_job_id) == (
        "anthropic",
        "classify",
        settings.extract_model,
        job.id,
    )
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (300, 150, 900)
    assert row.cost_usd == PRICED


async def test_a_fenced_answer_is_unwrapped_not_repaired(session, model_api):
    model_api.queue(message("```json\n" + json.dumps(BUYER) + "\n```"))
    result = await classify(session)
    assert (result.status, result.calls) == ("applied", 1)
    assert extraction.json_payload('Mana: {"a": 1} tamom') == '{"a": 1}'
    assert extraction.json_payload("no object") == "no object"


async def test_a_grammar_failure_falls_back_to_prompted_json_and_is_remembered(
    session, model_api
):
    model_api.queue(grammar_failure(), message(BUYER), message(BUYER))
    first = await classify(session)
    assert (first.status, first.calls) == ("applied", 2)
    structured, prompted = model_api.requests
    assert structured["output_config"]["format"] == structured_format()
    # The same turn as prompted JSON: the schema in the system block, no
    # format, the effort kept.
    assert prompted["output_config"] == {"effort": "low"}
    assert prompted["system"][0][
        "text"
    ] == CLASSIFY_SYSTEM + extraction.schema_instructions(ImportSignal)
    assert prompted["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert prompted["messages"] == structured["messages"]
    assert not extraction.structured_output_available(ImportSignal)

    second = await classify(session)
    assert (second.status, second.calls) == ("applied", 1)
    assert "format" not in model_api.requests[2]["output_config"]
    await session.commit()
    first_job, _ = await jobs(session)
    assert first_job.attempts == 2
    rows = await ledger(session)
    # The refused grammar is a failed claim at its estimate; the answers are
    # priced. Three requests, three rows: none left uncounted.
    assert [r.model for r in rows] == [
        "failed:BadRequestError",
        settings.extract_model,
        settings.extract_model,
    ]
    assert rows[0].cost_usd > 0 and rows[0].input_tokens is None
    assert all(r.operation == "classify" for r in rows)


async def test_an_invalid_answer_gets_one_nudge(session, model_api):
    model_api.queue(message("Albatta, mana javob: xaridor"), message(BUYER))
    result = await classify(session)
    assert (result.status, result.calls) == ("applied", 2)
    first, nudged = model_api.requests
    assert nudged["messages"] == [
        *first["messages"],
        {"role": "user", "content": JSON_NUDGE},
    ]
    assert nudged["max_tokens"] == first["max_tokens"] == 400


async def test_an_answer_invalid_twice_fails_as_final(session, model_api):
    wrong = dict(BUYER, speaker_role="customer")  # not in the schema's enum
    model_api.queue(message(wrong), message({"speaker_role": "buyer"}))
    result = await classify(session)
    await session.commit()
    assert (result.status, result.error, result.calls, result.final) == (
        "failed",
        "invalid",
        2,
        True,
    )
    [job] = await jobs(session)
    assert (job.status, job.error, job.result, job.attempts) == (
        "failed",
        "invalid",
        None,
        2,
    )


@pytest.mark.parametrize("text", [None, "Kechirasiz, bu soʻrovga javob bera olmayman."])
async def test_a_refusal_is_failed_with_no_retry(session, model_api, text):
    # With text that is no JSON: messages.parse would raise validating it and
    # lose the stop_reason; the lane reads the stop_reason first.
    model_api.queue(message(text, stop_reason="refusal", output_tokens=12))
    result = await classify(session)
    await session.commit()
    assert (result.status, result.error, result.calls, result.final) == (
        "failed",
        "refusal",
        1,
        True,
    )
    assert result.stop_reason == "refusal"
    assert len(model_api.requests) == 1
    [job] = await jobs(session)
    assert (job.status, job.error) == ("failed", "refusal")
    [row] = await ledger(session)
    assert (row.output_tokens, row.model) == (12, settings.extract_model)


async def test_max_tokens_is_retried_once_with_twice_the_budget(session, model_api):
    model_api.queue(
        message('{"speaker_role": "buy', stop_reason="max_tokens", output_tokens=400),
        message(BUYER),
    )
    result = await classify(session)
    assert (result.status, result.calls) == ("applied", 2)
    assert [r["max_tokens"] for r in model_api.requests] == [400, 800]
    assert model_api.requests[1]["messages"] == model_api.requests[0]["messages"]


async def test_max_tokens_twice_fails_as_final(session, model_api):
    cut = message("{", stop_reason="max_tokens")
    model_api.queue(cut, cut)
    result = await classify(session)
    assert (result.status, result.error, result.final) == ("failed", "max_tokens", True)
    assert [r["max_tokens"] for r in model_api.requests] == [400, 800]


async def test_one_retry_in_all(session, model_api):
    """A max_tokens answer and then an invalid one: the second failure ends
    it — three requests would be a retry of the retry."""
    model_api.queue(message("{", stop_reason="max_tokens"), message("no json"))
    result = await classify(session)
    assert (result.status, result.error, result.calls) == ("failed", "invalid", 2)


async def test_a_budget_refusal_sends_nothing(session, model_api, monkeypatch):
    monkeypatch.setattr(settings, "daily_cost_cap_usd", Decimal("0"))
    result = await classify(session)
    await session.commit()
    assert (result.status, result.error, result.refused, result.calls) == (
        "failed",
        "refused:cost_cap_daily",
        True,
        0,
    )
    assert model_api.requests == []
    [row] = await ledger(session)
    assert (row.operation, row.model, row.cost_usd, row.units) == (
        "classify",
        "refused:cost_cap_daily",
        0,
        0,
    )
    # Remembered until the next hour: the next call is refused in-process.
    again = await classify(session)
    assert (again.refused, again.error) == (True, "refused:cost_cap_daily")
    assert model_api.requests == []


async def test_an_unpriced_model_is_refused_before_dispatch(session, model_api):
    result = await classify(session, model="not-a-priced-model")
    assert (result.refused, result.error) == (True, "refused:unpriced")
    assert model_api.requests == []


@pytest.mark.parametrize(
    ("system", "text", "pattern"),
    [
        (CLASSIFY_SYSTEM, "TEXT: tel +998 90 000 00 01", "PHONE_RE"),
        (CLASSIFY_SYSTEM, "TEXT: @namuna_tekstil ga yozing", "TG_RE"),
        (CLASSIFY_SYSTEM, "TEXT: info@namuna.example.uz", "EMAIL_RE"),
        (CLASSIFY_SYSTEM, "TEXT: Instagram: namuna_shop", "IG_RE"),
        (CLASSIFY_SYSTEM + " Example: t.me/namuna_optom", "TEXT: ok", "TG_RE"),
    ],
)
async def test_an_unmasked_request_never_leaves(
    session, model_api, system, text, pattern
):
    with pytest.raises(UnmaskedPayload) as refused:
        await extraction.call(
            session,
            op="classify",
            schema=ImportSignal,
            system=system,
            user=Pseudonymised(text),
            now=NOW,
        )
    assert refused.value.pattern == pattern
    assert pattern in str(refused.value) and "namuna" not in str(refused.value)
    assert model_api.requests == []
    assert await jobs(session) == [] and await ledger(session) == []


async def test_only_a_pseudonymised_text_is_accepted(session, model_api):
    with pytest.raises(TypeError):
        await extraction.call(
            session,
            op="classify",
            schema=ImportSignal,
            system=CLASSIFY_SYSTEM,
            user="TEXT: ok",  # type: ignore[arg-type]
            now=NOW,
        )
    assert model_api.requests == []


async def test_the_masked_text_and_its_map_are_stored(session, model_api):
    user = pseudonymise("TEXT: 2 kub mato kerak, tel +998 90 000 00 01, @namuna_tekstil")
    model_api.queue(message(BUYER))
    await classify(session, user=user)
    await session.commit()
    [job] = await jobs(session)
    assert job.request["user"] == "TEXT: 2 kub mato kerak, tel <PHONE_1>, @<TG_1>"
    assert job.pseudonyms == {
        "<PHONE_1>": "+998 90 000 00 01",
        "<TG_1>": "namuna_tekstil",
    }


async def test_an_api_failure_is_a_transient_failure(session, model_api):
    model_api.queue(api_error(500, "api_error", "Internal server error"))
    result = await classify(session)
    await session.commit()
    assert (result.status, result.error, result.final, result.refused) == (
        "failed",
        "api:InternalServerError",
        False,
        False,
    )
    [row] = await ledger(session)
    # The claim keeps its estimate: the cap never undercounts.
    assert row.model == "failed:InternalServerError"
    [body] = model_api.requests
    assert row.cost_usd == extraction.estimate_usd(
        settings.extract_model,
        prompt_bytes=extraction.request_bytes(body),
        max_tokens=400,
    )


async def flag(session, key: str) -> m.RuntimeFlag | None:
    return await session.scalar(
        sa.select(m.RuntimeFlag)
        .where(m.RuntimeFlag.key == health.PROBLEM_FLAG_PREFIX + key)
        .execution_options(populate_existing=True)
    )


async def test_five_failures_in_an_hour_raise_anthropic_failing(session, model_api):
    for minute in range(4):
        model_api.queue(overloaded())
        await classify(session, now=NOW + timedelta(minutes=minute))
    await session.commit()
    assert await flag(session, "anthropic_failing") is None
    model_api.queue(overloaded())
    await classify(session, now=NOW + timedelta(minutes=4))
    await session.commit()
    raised = await flag(session, "anthropic_failing")
    assert raised is not None
    assert raised.value["detail"] == {"code": "api:OverloadedError", "n": 5}
    assert raised.value["until"] == (NOW + timedelta(minutes=64)).isoformat()
    # A success is the end of the streak.
    model_api.queue(message(BUYER))
    await classify(session, now=NOW + timedelta(minutes=5))
    await session.commit()
    assert await flag(session, "anthropic_failing") is None


async def test_failures_outside_the_hour_do_not_count(session, model_api):
    for minute in (0, 1, 2, 3):
        model_api.queue(overloaded())
        await classify(session, now=NOW + timedelta(minutes=minute))
    model_api.queue(overloaded())
    await classify(session, now=NOW + timedelta(minutes=61))
    await session.commit()
    # (12:01, 13:01]: the failures at 12:02, 12:03 and 13:01 count; 12:00 is
    # out, and so is 12:01, exactly an hour back.
    assert await extraction.failed_attempts(session, NOW + timedelta(minutes=61)) == 3
    assert await flag(session, "anthropic_failing") is None


async def test_a_dropped_field_flags_the_job_and_raises_pii_leak(session, model_api):
    def validate(signal: ImportSignal) -> Validated:
        return Validated(result={"goods": []}, leaks=("goods",))

    model_api.queue(message(BUYER))
    result = await classify(session, validate=validate)
    await session.commit()
    assert (result.status, result.error) == ("applied", "pii_leak")
    [job] = await jobs(session)
    assert (job.status, job.error, job.result) == ("applied", "pii_leak", {"goods": []})
    raised = await flag(session, "pii_leak")
    assert raised is not None
    assert raised.value["detail"] == {"n": 1, "op": "classify"}
    assert raised.value["until"] == (NOW + timedelta(hours=24)).isoformat()


async def test_no_key_and_no_client_is_refused_without_a_job(
    session, model_api, monkeypatch
):
    extraction.set_client(None)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert not extraction.available()
    result = await classify(session)
    assert (result.status, result.error, result.refused, result.job_id) == (
        "failed",
        "unavailable",
        True,
        None,
    )
    assert await jobs(session) == []


def test_the_model_of_an_operation_is_its_roles_setting(monkeypatch):
    assert extraction.model_for("classify") == settings.extract_model
    assert extraction.model_for("draft") == settings.extract_model
    assert extraction.model_for("verdict") == settings.verdict_model
    assert extraction.model_for("company_verdict") == settings.batch_model
    monkeypatch.setattr(settings, "extract_model", settings.verdict_model)
    assert extraction.model_for("classify") == settings.verdict_model
    with pytest.raises(ValueError):
        extraction.model_for("serp_brave")


async def test_a_configured_model_is_the_one_asked(session, model_api, monkeypatch):
    monkeypatch.setattr(settings, "extract_model", settings.verdict_model)
    model_api.queue(message(BUYER, model=settings.verdict_model))
    result = await classify(session)
    assert result.model == settings.verdict_model
    assert model_api.requests[0]["model"] == settings.verdict_model


def test_04_4_1_routes():
    assert {
        op: (r.kind, r.lane, r.effort, r.max_tokens)
        for op, r in extraction.ROUTES.items()
    } == {
        "classify": ("classify", "instant", "low", 400),
        "verdict": ("verdict", "instant", "low", 1200),
        "company_verdict": ("company_verdict", "batch", "low", 1500),
        "draft": ("draft", "instant", "low", 600),
    }


def test_the_system_prompt_passes_the_mask_fence():
    """extraction.call checks the system block too: the frozen prompt must
    carry no «@word», no phone shape and no word after «Instagram»."""
    from clientscrapper.services.pseudonyms import assert_masked

    assert_masked(CLASSIFY_SYSTEM)
    assert_masked(JSON_NUDGE)
    assert_masked(extraction.schema_instructions(ImportSignal))


async def test_a_validator_bug_is_a_final_failure(session, model_api):
    def broken(signal: ImportSignal) -> Validated:
        raise KeyError("a validator's own bug")

    model_api.queue(message(BUYER))
    result = await classify(session, validate=broken)
    await session.commit()
    assert (result.status, result.error, result.final) == ("failed", "validator", True)
    [job] = await jobs(session)
    assert (job.status, job.error, job.result) == ("failed", "validator", None)


async def test_a_tool_definition_is_held_to_the_mask_too(session, model_api):
    tools = [
        {"name": "lookup", "description": "call +998 90 000 00 01", "input_schema": {}}
    ]
    with pytest.raises(UnmaskedPayload) as refused:
        await classify(session, tools=tools)
    assert refused.value.pattern == "PHONE_RE"
    assert model_api.requests == [] and await jobs(session) == []


def test_the_claim_is_never_under_what_the_call_can_cost():
    """04 §8: the claim bounds the bill. A Cyrillic-heavy request priced at
    a token per byte of what it sends (no token is shorter than a byte) and
    the output at max_tokens never exceeds the claim."""
    text = "Ассалому алайкум, Хитойдан 5 куб ўйинчоқ ва қўғирчоқ керак 😀 " * 8
    user = pseudonymise(f"TEXT: {text}")
    params = {
        "system": extraction.system_block(
            CLASSIFY_SYSTEM, ImportSignal, with_schema=False
        ),
        "messages": [{"role": "user", "content": user.text}],
        "output_config": extraction.output_config(
            ImportSignal, effort="low", structured=True
        ),
    }
    sent = extraction.request_bytes(params)
    claim = extraction.estimate_usd(
        settings.extract_model, prompt_bytes=sent, max_tokens=400
    )
    worst = anthropic_cost_usd(
        settings.extract_model, input_tokens=sent, output_tokens=400
    )
    assert claim is not None and worst is not None
    assert claim >= worst
    # A byte bound, not a character ratio: the Cyrillic and the emoji count
    # more than once.
    assert sent > len(json.dumps(params, ensure_ascii=False))


async def test_the_schema_a_request_sends_is_recorded_by_its_hash(session, model_api):
    """A field's description is prompt too: the job records the schema it
    was asked with beside the system prompt's hash."""

    class Narrower(ImportSignal):
        """Another description the model would read."""

    model_api.queue(message(BUYER))
    await classify(session)
    [job] = await jobs(session)
    assert job.request["schema_sha256"] == extraction.schema_sha256(ImportSignal)
    assert extraction.schema_sha256(Narrower) != extraction.schema_sha256(ImportSignal)
    assert len(job.request["system_sha256"]) == len(job.request["schema_sha256"]) == 16
    # The schema's description is written for the model.
    description = extraction.json_schema(ImportSignal)["description"]
    assert "§" not in description and "``" not in description
