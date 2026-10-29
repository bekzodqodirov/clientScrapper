"""The RelevanceVerdict in both lanes, and the opener (04 §4.4, §4.5, §4.8;
07 §4.1 T-30).

What these pin: the bundle (blocks newest first, masked, capped); an
instant verdict applied — the company's verdict, segment, band and goods, a
``cn_origin`` on the quote's own evidence and nothing for the band (it adds
no points); a quote verified against its own block or dropped; the weak
exclusion flag dropped; a refusal and a final failure not asked again on the
same evidence, a budget refusal keeping nothing; the second instant rule
only while the night's batch is far; an instant verdict superseding the
night's waiting job; the verdict job never taking the batch lane's
fallbacks; the night's selection (stale evidence, failed and taken-fallback
companies asked again, refusals and jobs in flight not); the batch lane and
its fallback through the registered kind; the opener — 04's draft when it
passes the redlines, the playbook template on a flag, a refusal, a budget
miss or a candidate that is not hot — stored once and carried by a re-score;
the card's Uzbek reasons.

Synthetic data only (07 §1.0); the model is ``FakeAnthropic`` with synthetic
answers.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.services import (
    candidates,
    cards,
    costs,
    crm_push,
    gates,
    model_jobs,
    openers,
    playbook,
    pseudonyms,
    redlines,
    scoring,
    validators,
    verdict,
)
from clientscrapper.services.keys import name_key
from tests.fakes import message, succeeded
from tests.fixtures import uz_corpus
from tests.test_uz_text import problems

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 27, 11, 0, tzinfo=TZ)  # a Tuesday; the batch 12.5 h off
NEAR = datetime(2026, 10, 27, 15, 0, tzinfo=TZ)  # the batch 8.5 h off
SUNDAY_NIGHT = datetime(2026, 11, 1, 23, 30, tzinfo=TZ)
TUESDAY_NIGHT = datetime(2026, 10, 27, 23, 30, tzinfo=TZ)
E1 = "Guangzhoudan 2 ta konteyner mato olib kelamiz, optom. Tel +998 90 000 00 61"
E2 = "Namuna Tekstil Savdo: mato va furnitura optom, Toshkent, Abu Sahiy bozori."
CHINA_QUOTE = "Guangzhoudan 2 ta konteyner mato"
_SEQ = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_refusals() -> Any:
    costs.clear_refusals()
    yield
    costs.clear_refusals()


@pytest.fixture(autouse=True)
def the_real_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """``services/verdict`` registers ``company_verdict`` at import; another
    test may have put its stand-in there."""
    monkeypatch.setitem(model_jobs.KINDS, "company_verdict", verdict.KIND)


# --- builders -------------------------------------------------------------------------


async def firm(
    session: AsyncSession, name: str = "Namuna Tekstil Savdo", **fields: Any
) -> m.Company:
    fields.setdefault("state", "active")
    fields.setdefault("legal_form", "MChJ")
    row = m.Company(display_name=name, name_key=name_key(name), **fields)
    session.add(row)
    await session.flush()
    return row


async def evidence(
    session: AsyncSession,
    owner: m.Company,
    excerpt: str,
    *,
    days: float = 1,
    source: str = "telegram_monitor",
    key: str = "vol_stated_1_3",
    quote: str | None = None,
    at: datetime = NOW,
) -> m.Evidence:
    """One piece of evidence of ``owner``, linked by a signal on it."""
    n = next(_SEQ)
    when = at - timedelta(days=days)
    found = m.Evidence(
        source_key=source,
        url=f"https://t.me/namuna_optom/{n}",
        fetched_at=when,
        published_at=when,
        sha256=f"{n:064x}",
        bytes=len(excerpt),
        excerpt=excerpt,
        expires_at=when + timedelta(days=180),
    )
    session.add(found)
    await session.flush()
    session.add(
        m.Signal(
            company_id=owner.id,
            evidence_id=found.id,
            key=key,
            value=Decimal(2) if key == "vol_stated_1_3" else None,
            quote=quote,
            observed_at=when,
            source_key=source,
        )
    )
    await session.flush()
    return found


async def candidate(
    session: AsyncSession, owner: m.Company, *, state: str = "review", score: int = 70
) -> m.LeadCandidate:
    row = m.LeadCandidate(
        company_id=owner.id,
        state=state,
        score=score,
        tier="hot" if score >= 60 else "warm",
        last_scored_at=NOW,
        score_breakdown={},
    )
    session.add(row)
    await session.flush()
    return row


async def person(session: AsyncSession, owner: m.Company, **fields: Any) -> m.Contact:
    fields.setdefault("full_name", "Rustam Namunov")
    row = m.Contact(
        company_id=owner.id,
        tier="person",
        legal_basis="public_source",
        source_url="https://t.me/namuna_optom/1",
        collected_at=NOW,
        **fields,
    )
    session.add(row)
    await session.flush()
    return row


async def lead_firm(
    session: AsyncSession, **cand: Any
) -> tuple[m.Company, m.LeadCandidate]:
    """A firm with E1 (yesterday) and E2 (five days ago) and a candidate."""
    owner = await firm(session, goods=["mato"])
    await evidence(session, owner, E2, days=5, key="vol_wholesale_bulky")
    await evidence(session, owner, E1, days=1, quote="2 ta konteyner mato")
    found = await candidate(session, owner, **cand)
    await session.commit()
    return owner, found


def answer(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "imports_from_china": "confirmed",
        "segment": "A",
        "est_volume_band": "container",
        "is_b2b": True,
        "is_competitor": False,
        "is_supplier_cn": False,
        "is_state_body": False,
        "decision_maker_role": "direktor",
        "goods": ["Mato", "furnitura"],
        "reasons": [
            "Guangzhoudan mato olib keladi",
            "Abu Sahiy bozorida o'z do'koni bor",
        ],
        "evidence_quotes": [{"ref": "E1", "quote": CHINA_QUOTE}],
        "confidence": 0.85,
    }
    return {**base, **changes}


async def reload(session: AsyncSession, row: Any) -> Any:
    await session.refresh(row)
    return row


async def signals_of(session: AsyncSession, owner: m.Company) -> list[m.Signal]:
    return list(
        await session.scalars(
            sa.select(m.Signal)
            .where(m.Signal.company_id == owner.id)
            .order_by(m.Signal.id)
            .execution_options(populate_existing=True)
        )
    )


async def jobs(session: AsyncSession) -> list[m.ModelJob]:
    return list(
        await session.scalars(
            sa.select(m.ModelJob)
            .order_by(m.ModelJob.id)
            .execution_options(populate_existing=True)
        )
    )


# --- the bundle -----------------------------------------------------------------------


def found(n: int, text: str, *, days: int, family: str = "telegram") -> verdict.Found:
    at = NOW - timedelta(days=days)
    return verdict.Found(
        evidence_id=n,
        source_key="telegram_monitor",
        family=family,
        url=f"https://www.t.me/namuna_optom/{n}",
        excerpt=text,
        observed_at=at,
        fetched_at=at,
    )


def test_the_bundle_is_masked_newest_first_and_says_what_it_dropped():
    bundle = verdict.build_bundle(
        display_name="Namuna Tekstil Savdo",
        legal_form="MChJ",
        city="Toshkent",
        stir="300000011",
        found=[found(11, E1, days=1), found(12, E2 + " Rustam aka yozing", days=5)],
        names=["Rustam Namunov"],
    )
    text = bundle.user.text
    assert text.startswith(
        "COMPANY: Namuna Tekstil Savdo | MChJ | Toshkent | STIR: 300000011\n\n"
        "[E1] telegram | 2026-10-26 | t.me\n"
        "Guangzhoudan 2 ta konteyner mato olib kelamiz, optom. Tel <PHONE_1>\n\n"
        "[E2] telegram | 2026-10-22 | t.me\n"
    )
    assert "<NAME_1> aka yozing" in text
    assert "+998" not in text and "Rustam" not in text
    assert bundle.user.reverse == {
        "<PHONE_1>": "+998 90 000 00 61",
        "<NAME_1>": "Rustam",
    }
    # The block header is no messenger id («telegram 2026-…» would be one).
    assert pseudonyms.unmasked(text) == []
    assert bundle.evidence_ids == [11, 12]
    assert bundle.excerpts == {"E1": E1, "E2": E2 + " Rustam aka yozing"}
    assert bundle.dropped == 0


def test_the_bundle_holds_twelve_blocks_and_its_token_cap():
    many = [found(n, f"Namuna mato optom {n}. " * 3, days=n) for n in range(1, 20)]
    twelve = verdict.build_bundle(
        display_name="Namuna", legal_form=None, city=None, stir=None, found=many
    )
    assert len(twelve.blocks) == verdict.BLOCKS_MAX == 12
    assert twelve.user.text.startswith(
        "COMPANY: Namuna | - | - | STIR: -\nDROPPED: 7 older blocks\n\n[E1] "
    )
    assert [b.ref for b in twelve.blocks] == [f"E{n}" for n in range(1, 13)]
    long = [found(n, "мато оптом " * 54, days=n) for n in range(1, 13)]
    capped = verdict.build_bundle(
        display_name="Namuna", legal_form=None, city=None, stir=None, found=long
    )
    assert len(capped.user.text.encode()) <= (
        verdict.BUNDLE_TOKENS_MAX * verdict.BYTES_PER_TOKEN
    )
    assert 0 < len(capped.blocks) < 12
    assert f"DROPPED: {12 - len(capped.blocks)} older blocks" in capped.user.text


# --- an instant verdict applied -------------------------------------------------------


async def test_an_instant_verdict_is_applied_with_a_quote_verified_and_no_band_points(
    session, model_api
):
    owner, cand = await lead_firm(session)
    contact = await person(session, owner)
    await session.commit()
    before = {s.key for s in await signals_of(session, owner)}
    model_api.queue(message(answer()))

    report = await verdict.verdict_batch(now=NOW)

    assert (report.claimed, report.applied, report.failed) == (1, 1, 0)
    [request] = model_api.requests
    assert request["model"] == settings.verdict_model
    assert request["max_tokens"] == 1200
    content = request["messages"][0]["content"]
    assert content.startswith("COMPANY: Namuna Tekstil Savdo | MChJ | - | STIR: -")
    assert "<PHONE_1>" in content and "+998" not in content
    [job] = await jobs(session)
    assert (job.kind, job.lane, job.status) == ("verdict", "instant", "applied")
    assert job.pseudonyms == {"<PHONE_1>": "+998 90 000 00 61"}
    owner = await reload(session, owner)
    assert owner.verdict == {**answer(), **job.result}
    assert owner.verdict["evidence_quotes"] == [{"ref": "E1", "quote": CHINA_QUOTE}]
    assert owner.verdict["reasons"] == [
        "Guangzhoudan mato olib keladi",
        "Abu Sahiy bozorida oʻz doʻkoni bor",
    ]
    assert owner.verdict_at == NOW
    assert (owner.segment, owner.volume_band) == ("A", "container")
    assert owner.goods == ["mato", "furnitura"]
    added = [s for s in await signals_of(session, owner) if s.model_job_id == job.id]
    assert [(s.key, s.quote, s.model) for s in added] == [
        ("cn_origin", CHINA_QUOTE, settings.verdict_model)
    ]
    # The band is for the card and routing: no volume key came from it.
    assert {s.key for s in await signals_of(session, owner)} - before == {"cn_origin"}
    assert (await reload(session, contact)).role == "direktor"
    assert (await reload(session, cand)).last_scored_at is None  # re-scored next


async def test_the_band_adds_no_points_and_lt1_is_only_a_flag(session, model_api):
    owner, cand = await lead_firm(session)
    weights = await scoring.load_weights(session)
    before = await candidates.rescore(session, cand, now=NOW, weights=weights)
    await session.commit()
    model_api.queue(
        message(
            answer(
                imports_from_china="unlikely",
                est_volume_band="lt1",
                confidence=0.9,
                evidence_quotes=[],
            )
        )
    )
    await verdict.verdict_batch(now=NOW)
    owner = await reload(session, owner)
    assert owner.volume_band == "lt1"
    assert validators.verdict_small(owner.verdict)
    assert not validators.verdict_small({**owner.verdict, "confidence": 0.79})
    after = await candidates.rescore(session, cand, now=NOW, weights=weights)
    assert after.total == before.total
    assert after.breakdown["groups"] == before.breakdown["groups"]
    assert after.breakdown["negatives"] == before.breakdown["negatives"]

    # A container band on another firm: the same score with and without it.
    other, cand2 = await lead_firm(session)
    first = await candidates.rescore(session, cand2, now=NOW, weights=weights)
    await session.commit()
    model_api.queue(message(answer(imports_from_china="unlikely", evidence_quotes=[])))
    await verdict.verdict_batch(now=NOW)
    assert (await reload(session, other)).volume_band == "container"
    again = await candidates.rescore(session, cand2, now=NOW, weights=weights)
    assert again.total == first.total


async def test_a_quote_is_verified_against_its_own_block(session, model_api):
    owner, _ = await lead_firm(session)
    # The China quote is E1's, not E2's; and one quote is in no block at all.
    model_api.queue(
        message(
            answer(
                evidence_quotes=[
                    {"ref": "E2", "quote": CHINA_QUOTE},
                    {"ref": "E1", "quote": "Yiwudan 40 fut konteyner"},
                    {"ref": "E7", "quote": "mato"},
                ]
            )
        )
    )
    await verdict.verdict_batch(now=NOW)
    owner = await reload(session, owner)
    assert owner.verdict["evidence_quotes"] == []
    # «confirmed» without a verified quote is a sector prior.
    assert owner.verdict["imports_from_china"] == "likely"
    keys = [s.key for s in await signals_of(session, owner) if s.model is not None]
    assert keys == ["cn_sector_prior"]
    newest = (await verdict.bundle_for(session, owner)).evidence_ids[0]  # type: ignore[union-attr]
    prior = next(s for s in await signals_of(session, owner) if s.model is not None)
    assert (prior.evidence_id, prior.quote) == (newest, None)


async def test_a_weak_competitor_flag_without_a_cargo_offer_is_dropped(
    session, model_api
):
    owner, _ = await lead_firm(session)
    model_api.queue(message(answer(is_competitor=True, confidence=0.6)))
    await verdict.verdict_batch(now=NOW)
    owner = await reload(session, owner)
    assert owner.verdict["is_competitor"] is False and owner.is_competitor is False
    # A sure one stands: the flag signal and the company's column.
    other, _ = await lead_firm(session)
    model_api.queue(message(answer(is_competitor=True, confidence=0.9)))
    await verdict.verdict_batch(now=NOW)
    other = await reload(session, other)
    assert other.is_competitor is True
    keys = {s.key for s in await signals_of(session, other) if s.model is not None}
    assert keys == {"cn_origin", "flag_competitor"}
    result = await gates.evaluate(session, other.id, now=NOW)
    assert result.decision == "excluded"


# --- when it is asked, and when not ---------------------------------------------------


async def test_a_refusal_is_not_asked_again_until_new_evidence(session, model_api):
    owner, _ = await lead_firm(session)
    model_api.queue(message(None, stop_reason="refusal"))
    report = await verdict.verdict_batch(now=NOW)
    assert (report.claimed, report.failed) == (1, 1)
    [job] = await jobs(session)
    assert (job.status, job.error) == ("failed", "refusal")
    # The next run asks nothing (FakeAnthropic fails a test on an unscripted call).
    again = await verdict.verdict_batch(now=NOW + timedelta(minutes=10))
    assert again.claimed == 0
    # New evidence: asked again.
    await evidence(
        session, owner, "Yiwudan mebel ham olamiz.", days=0, at=NOW + timedelta(hours=1)
    )
    await session.commit()
    model_api.queue(message(answer()))
    later = await verdict.verdict_batch(now=NOW + timedelta(hours=2))
    assert (later.claimed, later.applied) == (1, 1)


async def test_an_api_failure_waits_and_is_given_up_after_three(session, model_api):
    from tests.fakes import overloaded

    await lead_firm(session)
    for minutes in (0, 15, 45):
        model_api.queue(overloaded())
        report = await verdict.verdict_batch(now=NOW + timedelta(minutes=minutes))
        assert (report.claimed, report.retried) == (1, 1)
        # Inside the wait: nothing asked.
        waiting = await verdict.verdict_batch(now=NOW + timedelta(minutes=minutes + 1))
        assert waiting.claimed == 0
    given_up = await verdict.verdict_batch(now=NOW + timedelta(hours=5))
    assert given_up.claimed == 0
    assert [j.error for j in await jobs(session)] == ["api:OverloadedError"] * 3


async def test_a_budget_refusal_keeps_nothing_and_the_card_says_so(
    session, model_api, monkeypatch
):
    owner, _ = await lead_firm(session)
    monkeypatch.setattr(settings, "daily_cost_cap_usd", Decimal("0"))
    report = await verdict.verdict_batch(now=NOW)
    assert (report.claimed, report.refused, report.applied) == (1, 1, 0)
    assert await jobs(session) == []
    refusal = (
        await session.scalars(
            sa.select(m.UsageLog).where(m.UsageLog.operation == "verdict")
        )
    ).one()
    assert (refusal.model, refusal.company_id) == ("refused:cost_cap_daily", owner.id)
    assert await verdict.refused_today(session, now=NOW)
    assert not await verdict.refused_today(session, now=NOW + timedelta(days=1))


async def test_every_card_the_cap_refused_says_so_not_only_the_first(
    session, model_api, monkeypatch
):
    """04 §8: a run stops at its first refusal, so the second company has no
    refused row of its own — the same cap refused it all the same."""
    first, cand_a = await lead_firm(session, score=72)
    second, cand_b = await lead_firm(session, score=71)
    monkeypatch.setattr(settings, "daily_cost_cap_usd", Decimal("0"))
    report = await verdict.verdict_batch(now=NOW)
    assert (report.claimed, report.refused) == (1, 1)
    rows = (
        await session.scalars(
            sa.select(m.UsageLog.company_id).where(m.UsageLog.operation == "verdict")
        )
    ).all()
    assert rows == [first.id]
    reviews = []
    for cand in (cand_a, cand_b):
        review = m.LeadReview(candidate_id=cand.id, kind="owner", state="pending")
        session.add(review)
        await session.commit()
        reviews.append(review.id)
        rv = await cards.review_view(session, review.id, now=NOW)
        assert rv is not None
        assert (rv.verdict_refused, cards.verdict_lines(rv)) == (
            True,
            [cards.VERDICT_REFUSED_UZ],
        )
    # A company that has its verdict shows its reasons, not the budget line.
    second.verdict, second.verdict_at = answer(), NOW
    await session.commit()
    rv = await cards.review_view(session, reviews[1], now=NOW)
    assert rv is not None and rv.verdict_refused is False


def _refused(owner: m.Company, op: str, code: str) -> m.UsageLog:
    return m.UsageLog(
        provider="anthropic",
        operation=op,
        model=costs.REFUSED_PREFIX + code,
        units=Decimal(0),
        batch=False,
        cost_usd=Decimal(0),
        company_id=owner.id,
        created_at=NOW - timedelta(hours=1),
    )


async def test_a_refusal_that_is_no_cap_is_not_the_budget(session):
    """Paused or unpriced is not «byudjet tugadi» (``costs.is_cap``)."""
    owner = await firm(session)
    session.add_all([_refused(owner, "verdict", c) for c in ("paused", "unpriced")])
    await session.commit()
    assert not await verdict.refused_today(session, now=NOW)
    session.add(_refused(owner, "company_verdict", "cost_cap_daily"))
    await session.commit()
    assert await verdict.refused_today(session, now=NOW)


async def test_the_second_rule_waits_for_a_far_batch_and_two_evidence_rows(session):
    owner, _ = await lead_firm(session, state="scored", score=50)
    single = await firm(session, "Namuna Kafel")
    await evidence(session, single, E1, days=1)
    await candidate(session, single, state="scored", score=50)
    high = await firm(session, "Namuna Mebel")
    await evidence(session, high, E1, days=1)
    await evidence(session, high, E2, days=2)
    await candidate(session, high, state="scored", score=80)
    await session.commit()
    assert verdict.batch_far(NOW) and not verdict.batch_far(NEAR)
    assert verdict.batch_far(TUESDAY_NIGHT + timedelta(minutes=1))
    far = await verdict.due_instant(session, now=NOW)
    assert [d.company_id for d in far] == [owner.id]
    assert await verdict.due_instant(session, now=NEAR) == []
    # A verdict newer than the evidence: not asked.
    owner.verdict, owner.verdict_at = answer(), NOW - timedelta(hours=1)
    await session.commit()
    assert await verdict.due_instant(session, now=NOW) == []


async def test_review_asks_again_after_thirty_days(session):
    # Scored above the second rule's band: only the review rule asks.
    owner, _ = await lead_firm(session, score=80)
    owner.verdict, owner.verdict_at = answer(), NOW - timedelta(days=29)
    await session.commit()
    assert await verdict.due_instant(session, now=NOW) == []
    owner.verdict_at = NOW - timedelta(days=31)
    await session.commit()
    assert [d.company_id for d in await verdict.due_instant(session, now=NOW)] == [
        owner.id
    ]


async def test_an_instant_verdict_supersedes_the_nights_waiting_job(session, model_api):
    owner, _ = await lead_firm(session)
    bundle = await verdict.bundle_for(session, owner)
    assert bundle is not None
    waiting = await model_jobs.enqueue(
        session, user=bundle.user, now=NOW, op="company_verdict", company_id=owner.id
    )
    await session.commit()
    model_api.queue(message(answer()))
    await verdict.verdict_batch(now=NOW)
    await session.refresh(waiting)
    assert (waiting.status, waiting.error) == ("failed", "superseded")


async def test_the_verdict_job_never_takes_the_batch_lanes_fallbacks(session, model_api):
    owner = await firm(session)
    session.add(
        m.ModelJob(
            kind="company_verdict",
            lane="batch",
            model=settings.batch_model,
            custom_id="cs-00000000-0000-0000-0000-000000000001",
            status="fallback",
            attempts=3,
            company_id=owner.id,
            request={"user": "COMPANY: Namuna"},
            created_at=NOW - timedelta(days=3),
        )
    )
    await session.commit()
    counts = await verdict.verdict_job(now=NOW)
    assert counts["claimed"] == 0
    [job] = await jobs(session)
    assert (job.status, job.applied_at) == ("fallback", None)
    assert model_api.requests == []


# --- the night ------------------------------------------------------------------------


async def test_the_night_selects_stale_companies_and_asks_failed_ones_again(
    session, model_api
):
    due, _ = await lead_firm(session, state="scored", score=45)
    fresh, _ = await lead_firm(session, state="enriching", score=50)
    fresh.verdict, fresh.verdict_at = answer(), NOW
    failed, _ = await lead_firm(session, state="scored", score=55)
    refused, _ = await lead_firm(session, state="scored", score=56)
    flight, _ = await lead_firm(session, state="scored", score=57)
    taken, _ = await lead_firm(session, state="scored", score=58)
    hot, _ = await lead_firm(session, state="scored", score=70)
    held, _ = await lead_firm(session, state="hold", score=30)
    for owner, status, error, applied in (
        (failed, "failed", "invalid", None),
        (refused, "failed", "refusal", None),
        (flight, "fallback", "errored", None),
        (taken, "fallback", "errored", NOW),
    ):
        session.add(
            m.ModelJob(
                kind="company_verdict",
                lane="batch",
                model=settings.batch_model,
                custom_id=f"cs-{uuid.uuid4()}",
                status=status,
                error=error,
                attempts=3,
                company_id=owner.id,
                applied_at=applied,
                request={"user": "COMPANY: Namuna"},
                created_at=NOW - timedelta(hours=1),
            )
        )
    await session.commit()

    chosen = await verdict.due_nightly(session, now=TUESDAY_NIGHT)
    assert chosen == [taken.id, failed.id, due.id]
    sunday = await verdict.due_nightly(session, now=SUNDAY_NIGHT)
    assert sunday == [taken.id, failed.id, due.id, held.id]
    assert hot.id not in sunday

    queued = await verdict.select_company_verdicts(now=TUESDAY_NIGHT)
    assert queued == 3
    pending = [j for j in await jobs(session) if j.status == "pending"]
    assert sorted(j.company_id for j in pending) == sorted([taken.id, failed.id, due.id])
    for job in pending:
        assert job.request["user"].startswith("COMPANY: Namuna Tekstil Savdo")
        assert "+998" not in job.request["user"]
        assert len(job.evidence_ids) == 2
    # Selected again the same night: the waiting jobs are asked anew, not doubled.
    assert await verdict.select_company_verdicts(now=TUESDAY_NIGHT) == 3
    assert len([j for j in await jobs(session) if j.status == "pending"]) == 3


async def test_the_batch_lane_answers_through_the_registered_kind(session, model_api):
    owner, _ = await lead_firm(session, state="scored", score=50)
    model_api.accept_batches()
    counts = await model_jobs.model_batch_submit_job(now=TUESDAY_NIGHT)
    assert (counts["selected"], counts["submitted"]) == (1, 1)
    [job] = await jobs(session)
    model_api.end(model_api.batch().id, {job.custom_id: succeeded(answer())})
    await model_jobs.collect(now=TUESDAY_NIGHT + timedelta(hours=1))
    owner = await reload(session, owner)
    [job] = await jobs(session)
    assert job.status == "applied"
    assert owner.verdict["evidence_quotes"] == [{"ref": "E1", "quote": CHINA_QUOTE}]
    origin = [s for s in await signals_of(session, owner) if s.key == "cn_origin"]
    assert [(s.quote, s.model, s.model_job_id) for s in origin] == [
        (CHINA_QUOTE, settings.batch_model, job.id)
    ]


async def test_a_fallback_is_answered_and_checked_like_the_batch(session, model_api):
    owner, _ = await lead_firm(session, state="scored", score=50)
    bundle = await verdict.bundle_for(session, owner)
    assert bundle is not None
    job = await model_jobs.enqueue(
        session,
        user=bundle.user,
        now=NOW,
        op="company_verdict",
        company_id=owner.id,
        evidence_ids=bundle.evidence_ids,
    )
    job.status, job.attempts = "fallback", 3
    await session.commit()
    model_api.queue(
        message(answer(evidence_quotes=[{"ref": "E2", "quote": "mato va furnitura"}]))
    )
    report = await model_jobs.take_fallbacks(now=TUESDAY_NIGHT)
    assert (report.taken, report.applied) == (1, 1)
    owner = await reload(session, owner)
    # The checker rebuilt the blocks from the job: E2 is E2's.
    assert owner.verdict["evidence_quotes"] == [
        {"ref": "E2", "quote": "mato va furnitura"}
    ]
    # A verified quote that names no China, from no dealer's page, is no
    # China origin: «confirmed» stands as «likely» (04 §4.4).
    assert owner.verdict["imports_from_china"] == "likely"
    keys = [s.key for s in await signals_of(session, owner) if s.model is not None]
    assert keys == ["cn_sector_prior"]


# --- the opener -----------------------------------------------------------------------

GOOD_DRAFT = uz_corpus.REDLINE_GOOD[8][0]  # a Telegram group, Yiwu/Guangzhou


async def opener_firm(session: AsyncSession, *, score: int = 70) -> m.LeadCandidate:
    owner, cand = await lead_firm(session, score=score)
    owner.segment = "A"
    await session.commit()
    return cand


async def breakdown(session: AsyncSession, cand: m.LeadCandidate) -> dict[str, Any]:
    await session.refresh(cand)
    return dict(cand.score_breakdown or {})


async def test_a_clean_draft_is_stored_and_carried_by_a_rescore(session, model_api):
    cand = await opener_firm(session)
    model_api.queue(message({"lang": "uz", "text": GOOD_DRAFT, "hook": "Telegram"}))
    text = await openers.ensure_opener(session, cand.id, now=NOW)
    await session.commit()
    assert text == GOOD_DRAFT
    [request] = model_api.requests
    assert request["model"] == settings.extract_model and request["max_tokens"] == 600
    content = request["messages"][0]["content"]
    assert "SEGMENT: A\nGOODS: mato\nSOURCE: Telegram guruh, 2026-10-26\n" in content
    assert "CONTACT: -\nLANGUAGE: uz\nSKELETON:\n" in content
    assert playbook.skeleton("A", "uz") in content
    stored = await breakdown(session, cand)
    assert stored["opener"] == GOOD_DRAFT
    assert stored["opener_meta"]["source"] == "draft"
    # Made once: a second call asks nothing.
    assert await openers.ensure_opener(session, cand.id, now=NOW) == GOOD_DRAFT
    # A re-score keeps it, and the note and the manager card read it.
    await candidates.rescore(session, cand, now=NOW)
    await session.commit()
    assert (await breakdown(session, cand))["opener"] == GOOD_DRAFT
    view = await candidates.load_view(session, cand.id, now=NOW)
    assert view.opener == GOOD_DRAFT
    assert "NIMA DEYISH: Assalomu alaykum. Men [Men], GSR Logistics." in (
        crm_push.render_note(view)
    )


@pytest.mark.parametrize(
    ("reply", "why", "flags"),
    [
        (
            {
                "lang": "uz",
                "text": GOOD_DRAFT.replace(
                    "Telegram guruhda", "Telegram guruhda (narx 150$)"
                ),
                "hook": "Telegram",
            },
            "flagged",
            ["digit", "money"],
        ),
        (
            {"lang": "uz", "text": "Salom!", "hook": "x"},
            "flagged",
            ["optout_missing", "hook_missing", "length"],
        ),
        (None, "refusal", []),
    ],
    ids=["price", "short", "refusal"],
)
async def test_a_flagged_or_refused_draft_is_never_rendered(
    session, model_api, reply, why, flags
):
    cand = await opener_firm(session)
    if reply is None:
        model_api.queue(message(None, stop_reason="refusal"))
    else:
        model_api.queue(message(reply))
    text = await openers.ensure_opener(session, cand.id, now=NOW)
    await session.commit()
    template = playbook.fill("A", "uz", goods=["mato"], family="telegram", person=False)
    assert text == template
    meta = (await breakdown(session, cand))["opener_meta"]
    assert (meta["source"], meta["why"], meta["flags"]) == ("template", why, flags)
    view = await candidates.load_view(session, cand.id, now=NOW)
    card = cards.render_manager(cards.ManagerView(9, replace(view, phone=None), "u_x"))
    assert f"✍️ Taklif: {crm_push.clip_words(template, cards.OPENER_MAX)}" in card.text
    assert "150" not in card.text
    assert problems(card.text, rendered=True) == set()


async def test_a_budget_miss_and_a_warm_candidate_get_the_template(
    session, model_api, monkeypatch
):
    warm = await opener_firm(session, score=50)
    assert await openers.ensure_opener(session, warm.id, now=NOW)
    await session.commit()
    assert (await breakdown(session, warm))["opener_meta"]["why"] == "not_hot"
    assert model_api.requests == []
    hot = await opener_firm(session)
    monkeypatch.setattr(settings, "daily_cost_cap_usd", Decimal("0"))
    await openers.ensure_opener(session, hot.id, now=NOW)
    await session.commit()
    meta = (await breakdown(session, hot))["opener_meta"]
    assert (meta["source"], meta["why"]) == ("template", "refused:cost_cap_daily")


async def test_the_verdict_job_gives_every_review_candidate_an_opener(session, model_api):
    cand = await opener_firm(session)
    model_api.queue(message(answer()))
    model_api.queue(message({"lang": "uz", "text": GOOD_DRAFT, "hook": "Telegram"}))
    counts = await verdict.verdict_job(now=NOW)
    assert (counts["applied"], counts["openers"]) == (1, 1)
    assert (await breakdown(session, cand))["opener"] == GOOD_DRAFT


# --- the card -------------------------------------------------------------------------


async def test_the_card_shows_the_uzbek_reasons(session, model_api):
    owner, cand = await lead_firm(session)
    model_api.queue(message(answer()))
    await verdict.verdict_batch(now=NOW)
    review = m.LeadReview(candidate_id=cand.id, kind="owner", state="pending")
    session.add(review)
    await session.commit()
    rv = await cards.review_view(session, review.id, now=NOW)
    assert rv is not None
    assert rv.verdict_reasons == (
        "Guangzhoudan mato olib keladi",
        "Abu Sahiy bozorida oʻz doʻkoni bor",
    )
    assert (rv.verdict_small, rv.verdict_refused) == (False, False)
    text = cards.verdict_lines(rv)
    assert text == [
        "🤖 AI xulosasi: Guangzhoudan mato olib keladi; "
        "Abu Sahiy bozorida oʻz doʻkoni bor"
    ]
    for line in text:
        assert problems(line, rendered=True) == set()
    del owner


async def test_the_validator_keeps_the_reasons_owner_text():
    raw = answer(
        reasons=[
            "Rustam aka bilan gaplashing",  # a person's name
            "Тел: <PHONE_1>",  # Cyrillic and a token
            "+998901234567 ga qo'ng'iroq",  # a phone: a leak
            "x" * 300,
        ],
        decision_maker_role="директор по закупкам",
    )
    from clientscrapper.services.schemas import RelevanceVerdict

    checked = validators.relevance_verdict(
        RelevanceVerdict.model_validate(raw),
        blocks={"E1": E1},
        reverse={},
        names=["Rustam Namunov"],
    )
    assert checked.result["reasons"] == ["x" * 119 + "…"]
    assert checked.leaks == ("reasons",)
    assert checked.result["decision_maker_role"] == "direktor po zakupkam"
    assert redlines.script_of(checked.result["decision_maker_role"]) == "latin"
