"""A purge racing the agent's own writers (seam review wave 4).

``purge.execute`` read its scope with no lock and locked the lineage only
in group 1: a push that committed in between made a candidate the stale
scope still deleted (``push_log.candidate_id`` RESTRICT — the purge failed
part-way), and a verdict or a draft whose model call was in flight wrote
its job (the request, the reverse map, the result) on the purged firm. The
lineage is now locked before the scope is read, and group 2 also clears the
company's jobs written since; an opener drafted across a purge writes
nothing (``openers.ensure_opener``)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import costs, model_jobs, openers, purge, push_gate, verdict
from tests import gate_fixtures
from tests import test_verdict as tv
from tests.fakes import message
from tests.fixtures import uz_corpus
from tests.gate_fixtures import NOW
from tests.test_crm_push import BASE, World


async def lock_waiters() -> int:
    async with SessionLocal() as probe:
        found = await probe.scalar(
            sa.text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        )
    return int(found or 0)


async def wait_waiters(n: int, *tasks: asyncio.Future[Any]) -> int:
    for _ in range(200):
        got = await lock_waiters()
        if got >= n or any(t.done() for t in tasks):
            return got
        await asyncio.sleep(0.05)
    return await lock_waiters()


@pytest.fixture
async def world(session, monkeypatch) -> AsyncIterator[World]:
    monkeypatch.setattr(settings, "crm_base_url", BASE)
    async with gate_fixtures.erp_ready(session, monkeypatch):
        yield World(session)


async def _execute(plan: purge.PurgePlan, root) -> Any:
    async with SessionLocal() as s:
        try:
            return await purge.execute(
                s, plan, requested_by="owner:1001", now=NOW, root=root
            )
        except Exception as exc:  # the test reports it
            return exc


async def test_a_push_that_commits_first_is_kept_never_deleted(world, tmp_path):
    """The push waits on the company, the purge behind it: the push lands,
    then the purge reads a candidate with push history and keeps it (02
    §2.10: the ledger keeps its candidate)."""
    cand = await world.lead()
    async with SessionLocal() as s:
        plan = await purge.plan_company(s, cand.company_id)
        await purge.record(s, plan, requested_by="owner:1001")
        await s.commit()
    assert plan.counts["candidates"] == 1  # planned as one to delete
    async with SessionLocal() as holder, holder.begin():
        await holder.execute(
            sa.select(m.Company.id)
            .where(m.Company.id == cand.company_id)
            .with_for_update(key_share=True)
        )
        push = asyncio.ensure_future(world.push(cand))
        assert await wait_waiters(1, push) >= 1 and not push.done()
        execute = asyncio.ensure_future(_execute(plan, tmp_path))
        assert await wait_waiters(2, push, execute) >= 2
    pushed = await asyncio.wait_for(push, 10)
    outcome = await asyncio.wait_for(execute, 10)
    assert pushed.status == "sent"
    assert isinstance(outcome, purge.PurgeResult), outcome
    assert outcome.executed["candidates"] == 0  # kept: it has a push now
    async with SessionLocal() as s:
        found = await s.get(m.LeadCandidate, cand.id)
        company = await s.get(m.Company, cand.company_id)
        row = await s.get(m.PurgeLog, plan.purge_log_id)
    assert found is not None and found.state == "pushed"
    assert company.state == "purged" and row.error is None


def _held_answer(gate: asyncio.Event, body: Any):
    async def held(request: Any) -> Any:
        await gate.wait()
        return message(body)

    return held


async def test_a_verdict_in_flight_leaves_no_job_on_the_purged_firm(
    session, model_api, monkeypatch, tmp_path
):
    costs.clear_refusals()
    monkeypatch.setitem(model_jobs.KINDS, "company_verdict", verdict.KIND)
    owner, _ = await tv.lead_firm(session)
    gate = asyncio.Event()
    model_api.queue(_held_answer(gate, tv.answer()))
    run = asyncio.ensure_future(verdict.verdict_batch(now=tv.NOW))
    for _ in range(200):
        if model_api.requests:
            break
        await asyncio.sleep(0.02)
    assert model_api.requests, "the verdict never asked"
    async with SessionLocal() as s:
        plan = await purge.plan_company(s, owner.id)
        await purge.record(s, plan, requested_by="owner:1001")
        await s.commit()
    executing = asyncio.ensure_future(_execute(plan, tmp_path))
    await wait_waiters(1, executing)
    gate.set()
    await asyncio.wait_for(run, 20)
    outcome = await asyncio.wait_for(executing, 20)
    assert isinstance(outcome, purge.PurgeResult), outcome
    async with SessionLocal() as s:
        company = await s.get(m.Company, owner.id)
        held = (
            await s.execute(
                sa.select(m.ModelJob.id).where(
                    m.ModelJob.company_id == owner.id,
                    sa.or_(
                        m.ModelJob.request.is_not(None),
                        m.ModelJob.pseudonyms.is_not(None),
                        m.ModelJob.result.is_not(None),
                    ),
                )
            )
        ).all()
    assert company.state == "purged"
    assert held == []


async def test_a_job_written_between_the_groups_goes_with_the_company(
    session, monkeypatch, tmp_path
):
    """A draft whose model call held no lock writes its job after group 1
    committed: group 2 takes the company's jobs as they stand then."""
    owner, _ = await tv.lead_firm(session)
    real = purge._people

    async def meanwhile(s: Any, scope: Any, *, now: Any) -> Any:
        async with SessionLocal() as other:
            other.add(
                m.ModelJob(
                    kind="draft",
                    lane="instant",
                    model="m",
                    custom_id="cs-late-opener",
                    status="applied",
                    company_id=owner.id,
                    request={"messages": "<PHONE_1>"},
                    pseudonyms={"<PHONE_1>": "+998900000061"},
                    result={"text": "Assalomu alaykum"},
                    applied_at=NOW,
                )
            )
            await other.commit()
        return await real(s, scope, now=now)

    monkeypatch.setattr(purge, "_people", meanwhile)
    async with SessionLocal() as s:
        plan = await purge.plan_company(s, owner.id)
    outcome = await _execute(plan, tmp_path)
    assert isinstance(outcome, purge.PurgeResult), outcome
    async with SessionLocal() as s:
        late = await s.scalar(
            sa.select(m.ModelJob).where(m.ModelJob.custom_id == "cs-late-opener")
        )
    assert (late.request, late.pseudonyms, late.result) == (None, None, None)


# --- an opener drafted across a purge (openers.ensure_opener) -------------------------

GOOD_DRAFT = uz_corpus.REDLINE_GOOD[8][0]


async def _ensure(cand_id: Any) -> Any:
    async with SessionLocal() as s:
        text = await openers.ensure_opener(s, cand_id, now=NOW)
        await s.commit()
        return text


async def _kept_lead(world: World) -> m.LeadCandidate:
    """A candidate with push history (a failed push): the purge keeps it."""
    cand = await world.lead()
    world.fake.modes[push_gate.external_id_for(cand.company_id, NOW)] = "503"
    assert (await world.push(cand)).status == "failed"
    assert (await world.candidate(cand)).state == "approved"
    return cand


async def test_no_draft_is_paid_for_a_purged_firm(world, tmp_path, model_api):
    cand = await _kept_lead(world)
    async with SessionLocal() as s:
        plan = await purge.plan_company(s, cand.company_id)
    assert isinstance(await _execute(plan, tmp_path), purge.PurgeResult)
    assert await _ensure(cand.id) is None
    assert model_api.requests == []


async def test_a_draft_made_across_the_purge_is_never_stored(
    world, tmp_path, monkeypatch, model_api
):
    cand = await _kept_lead(world)
    real_make = openers.make
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def held_make(*a: Any, **k: Any) -> Any:
        entered.set()
        await gate.wait()
        return await real_make(*a, **k)

    monkeypatch.setattr(openers, "make", held_make)
    model_api.queue(message({"lang": "uz", "text": GOOD_DRAFT, "hook": "Telegram"}))
    opener = asyncio.ensure_future(_ensure(cand.id))
    await asyncio.wait_for(entered.wait(), 5)
    async with SessionLocal() as s:
        plan = await purge.plan_company(s, cand.company_id)
    assert isinstance(await _execute(plan, tmp_path), purge.PurgeResult)
    gate.set()
    assert await asyncio.wait_for(opener, 10) is None
    async with SessionLocal() as s:
        after = await s.get(m.LeadCandidate, cand.id)
        jobs = (
            await s.execute(
                sa.select(
                    m.ModelJob.request, m.ModelJob.pseudonyms, m.ModelJob.result
                ).where(
                    m.ModelJob.company_id == cand.company_id, m.ModelJob.kind == "draft"
                )
            )
        ).all()
    assert openers.stored(after.score_breakdown) is None
    assert jobs and all(row == (None, None, None) for row in jobs)


async def test_two_runs_at_once_pay_for_one_draft(world, monkeypatch, model_api):
    """The card job beside /lidlar, two cards of one lead: the second run
    finds the draft being bought and does not buy it again."""
    cand = await world.lead()
    real_make = openers.make
    gate = asyncio.Event()
    entered: list[int] = []

    async def held_make(*a: Any, **k: Any) -> Any:
        entered.append(1)
        await gate.wait()
        return await real_make(*a, **k)

    monkeypatch.setattr(openers, "make", held_make)
    model_api.queue(message({"lang": "uz", "text": GOOD_DRAFT, "hook": "Telegram"}))
    first = asyncio.ensure_future(_ensure(cand.id))
    for _ in range(100):
        if entered:
            break
        await asyncio.sleep(0.02)
    second = await asyncio.wait_for(_ensure(cand.id), 5)
    gate.set()
    made = await asyncio.wait_for(first, 10)
    assert (len(entered), len(model_api.requests)) == (1, 1)
    assert second is None and made is not None
