"""The seam between the Fetcher (T-05) and the budget controller (T-06).

``services/http`` refuses a paid request without a cost gate and calls the
gate it was given; ``collectors/runner.ask_gate`` binds the real
``costs.ask`` to a run. These tests run a Fetcher paid call through that real
gate on Postgres: under the caps the call is made once and its row carries
the run, the sub-source and the answer's price; at a cap nothing is sent, the
refusal is one zero row, and the Fetcher reports a budget stop. The fakes in
test_http.py pin the Fetcher's own half; this file pins that the halves fit.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal as D
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors.runner import ask_gate
from clientscrapper.db.models import CollectorRun, UsageLog
from clientscrapper.services import costs
from clientscrapper.services.http import BudgetExhausted, FetchResult, PaidCall
from clientscrapper.services.usage import Priced
from tests.fakes import FakeClock, Site, budget, fetcher_for

BRAVE = "https://api.search.brave.com/res/v1/web/search"


@pytest.fixture(autouse=True)
def _fresh_process_memory() -> Iterator[None]:
    # costs.ask remembers a refusal until the next hour, per process.
    costs.clear_refusals()
    costs.set_cap_notifier(None)
    yield
    costs.clear_refusals()
    costs.set_cap_notifier(None)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def brave(clock: FakeClock) -> Site:
    return Site(clock, {BRAVE: httpx.Response(200, json={"web": {"results": []}})})


def priced(result: FetchResult) -> Priced:
    # One Brave query answered: one unit at the list price (03 §1.3).
    return Priced(cost_usd=D("0.005"), units=D(1))


async def run_row(session: AsyncSession, now: datetime) -> int:
    run = CollectorRun(source_key="serp_layer", started_at=now, status="running")
    session.add(run)
    await session.commit()
    return run.id


async def usage_rows(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.scalars(
        sa.select(UsageLog)
        .order_by(UsageLog.id)
        .execution_options(populate_existing=True)
    )
    return [
        {
            "operation": r.operation,
            "provider": r.provider,
            "model": r.model,
            "units": r.units,
            "cost_usd": r.cost_usd,
            "source_key": r.source_key,
            "collector_run_id": r.collector_run_id,
            "created_at": r.created_at,
        }
        for r in rows
    ]


async def test_a_paid_fetch_through_the_real_ask_is_made_once_and_recorded(
    session, clock
):
    now = clock.now()
    run_id = await run_row(session, now)
    site = brave(clock)
    fetcher = fetcher_for(site, cost_gate=ask_gate(session, run_id))
    b = budget(usd="1")

    result = await fetcher.get(
        BRAVE,
        source="search_brave",
        budget=b,
        obey_robots=False,
        paid=PaidCall("serp_brave", D("0.005"), priced),
    )

    assert result.status == 200 and len(site.seen) == 1
    assert await usage_rows(session) == [
        {
            "operation": "serp_brave",
            "provider": "brave",
            "model": None,
            "units": D("1.000"),
            "cost_usd": D("0.005000"),
            "source_key": "search_brave",
            "collector_run_id": run_id,
            "created_at": now,
        }
    ]
    assert (b.spent_usd, fetcher.stats.cost_usd) == (D("0.005"), D("0.005"))


@pytest.mark.parametrize(
    ("operation", "spent", "code"),
    [
        # The day's $10 (04 §8 rung 2), spent by anything.
        ("other", "10.00", "cost_cap_daily"),
        # Brave's own allocation (rung 4): 0.165 reached.
        ("serp_brave", "0.165", "cost_cap_op:serp_brave"),
    ],
)
async def test_at_a_cap_the_real_ask_refuses_and_nothing_is_sent(
    session, clock, operation, spent, code
):
    now = clock.now()
    run_id = await run_row(session, now)
    session.add(
        UsageLog(
            provider="other" if operation == "other" else "brave",
            operation=operation,
            cost_usd=D(spent),
            created_at=now,
        )
    )
    await session.commit()
    site = brave(clock)
    fetcher = fetcher_for(site, cost_gate=ask_gate(session, run_id))
    b = budget(usd="1")

    with pytest.raises(BudgetExhausted) as caught:
        await fetcher.get(
            BRAVE,
            source="search_brave",
            budget=b,
            obey_robots=False,
            paid=PaidCall("serp_brave", D("0.005"), priced),
        )

    assert (caught.value.reason, caught.value.detail) == ("cost_cap", code)
    assert fetcher.stats.exhausted == "cost_cap"
    assert site.seen == []
    assert (b.spent_usd, fetcher.stats.cost_usd) == (D("0"), D("0"))
    refusal = (await usage_rows(session))[-1]
    assert refusal == {
        "operation": "serp_brave",
        "provider": "brave",
        "model": f"refused:{code}",
        "units": D("0.000"),
        "cost_usd": D("0.000000"),
        "source_key": "search_brave",
        "collector_run_id": run_id,
        "created_at": now,
    }


async def test_a_request_ask_cannot_take_fails_the_fetch_and_writes_nothing(
    session, clock
):
    # model_reserve is a ledger row, never a call: ask raises ValueError
    # before it reads anything, and the Fetcher must not call that a cap.
    now = clock.now()
    run_id = await run_row(session, now)
    site = brave(clock)
    fetcher = fetcher_for(site, cost_gate=ask_gate(session, run_id))
    with pytest.raises(ValueError):
        await fetcher.get(
            BRAVE,
            source="search_brave",
            budget=budget(usd="1"),
            obey_robots=False,
            paid=PaidCall("model_reserve", D("0.005"), priced),
        )
    assert fetcher.stats.exhausted is None
    assert site.seen == []
    assert await usage_rows(session) == []
