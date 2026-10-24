"""The budget controller (04 §8 / AC 11, 07 §1.9).

Two halves. ``decide`` is pure, so every rung and their order are pinned on
constructed ledgers. The rest runs ``costs.ask`` against Postgres: the rows it
writes, the Tashkent day, the claim that makes two concurrent calls unable to
both pass, the batch's provisional rows, and the alerts once per day.

Times are fixed Tashkent datetimes handed to the code as ``now``; usage rows
are stamped with that ``now``, never with the server clock.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import typing
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db import enums
from clientscrapper.db.models import UsageLog
from clientscrapper.db.session import SessionLocal, session_scope
from clientscrapper.services import costs, usage
from clientscrapper.services.costs import CostCapReached, Ledger, OpTotals, decide
from clientscrapper.services.usage import Priced

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 23, 14, 23, tzinfo=TZ)
NOON = time(12, 0)
EXTRACT = usage.DEFAULT_MODELS["EXTRACT_MODEL"]
VERDICT = usage.DEFAULT_MODELS["VERDICT_MODEL"]
BATCH = usage.DEFAULT_MODELS["BATCH_MODEL"]
CAP = D("10")
MONTH_CAP = D("250")


@pytest.fixture(autouse=True)
def _fresh_process_memory() -> Iterator[None]:
    """The refusal memory and the hooks are process state: each test starts
    and ends with none."""
    costs.clear_refusals()
    costs.set_anthropic_health(None)
    costs.set_cap_notifier(None)
    yield
    costs.clear_refusals()
    costs.set_anthropic_health(None)
    costs.set_cap_notifier(None)


# --- the tables ------------------------------------------------------------------


def test_allocations_and_family_caps_both_sum_to_ten_dollars():
    assert sum(usage.ALLOCATIONS.values()) == D("10.00")
    assert sum(usage.FAMILY_CAPS.values()) == D("10.00")
    assert usage.ALLOCATIONS["serp_brave"] == D("0.165")  # 33 × $0.005


def test_each_familys_allocations_add_up_to_its_cap():
    for family, cap in usage.FAMILY_CAPS.items():
        ops = [op for op, f in usage.FAMILY.items() if f == family]
        assert sum(usage.ALLOCATIONS[op] for op in ops) == cap, family


def test_the_operation_literal_is_the_usage_log_check():
    assert usage.OPERATIONS == enums.OPERATIONS
    assert typing.get_args(usage.Operation) == enums.OPERATIONS
    assert set(usage.PROVIDER.values()) <= set(enums.PROVIDERS)
    assert set(usage.PROVIDER) == set(enums.OPERATIONS)
    budgeted = set(enums.OPERATIONS) - {"other"}
    assert set(usage.ALLOCATIONS) == set(usage.FAMILY) == budgeted
    assert budgeted - {"model_reserve", "serp_reserve"} == usage.CALLABLE


def test_costs_serves_the_tables_from_their_one_home():
    assert costs.ALLOCATIONS is usage.ALLOCATIONS
    assert costs.FAMILY_CAPS is usage.FAMILY_CAPS


def test_ask_has_the_one_signature_of_04_section_8():
    params = inspect.signature(costs.ask).parameters
    assert list(params) == [
        "session",
        "operation",
        "est_usd",
        "call",
        "price",
        "now",
        "source_key",
        "company_id",
        "model_job_id",
        "collector_run_id",
    ]
    keyword = [p for p in params.values() if p.kind is inspect.Parameter.KEYWORD_ONLY]
    assert [p.name for p in keyword] == list(params)[1:]
    required = [p.name for p in keyword if p.default is inspect.Parameter.empty]
    assert required == ["operation", "est_usd", "call", "price", "now"]


# --- the business calendar -----------------------------------------------------------


def test_the_day_turns_at_tashkent_midnight_not_utc():
    last = datetime(2026, 10, 23, 18, 59, 59, tzinfo=UTC)  # 23:59:59 in Tashkent
    first = datetime(2026, 10, 23, 19, 0, 0, tzinfo=UTC)  # 00:00:00 next day
    assert costs.day_start(last) == datetime(2026, 10, 23, tzinfo=TZ)
    assert costs.day_start(first) == datetime(2026, 10, 24, tzinfo=TZ)
    assert costs.day_end(last) == datetime(2026, 10, 24, tzinfo=TZ)


def test_the_month_is_the_tashkent_calendar_month():
    new_years_eve = datetime(2026, 12, 31, 19, 30, tzinfo=UTC)  # 00:30 on 1 January
    assert costs.month_start(new_years_eve) == datetime(2027, 1, 1, tzinfo=TZ)
    assert costs.month_end(datetime(2026, 12, 15, tzinfo=TZ)) == datetime(
        2027, 1, 1, tzinfo=TZ
    )
    assert costs.month_start(datetime(2026, 10, 31, 18, 59, tzinfo=UTC)) == datetime(
        2026, 10, 1, tzinfo=TZ
    )


def test_a_refusal_is_remembered_until_the_top_of_the_next_hour():
    assert costs.next_hour(NOW) == datetime(2026, 10, 23, 15, 0, tzinfo=TZ)
    assert costs.next_hour(datetime(2026, 10, 23, 23, 40, tzinfo=TZ)) == datetime(
        2026, 10, 24, 0, 0, tzinfo=TZ
    )


def test_a_naive_now_is_refused():
    with pytest.raises(ValueError):
        costs.day_start(datetime(2026, 10, 23, 14, 0))


# --- decide: every rung, in order ------------------------------------------------------


def ledger(month: str = "0", **day: str | OpTotals) -> Ledger:
    """A ledger whose day spend per op is given; ``month`` defaults to the day."""
    ops = {
        op: v if isinstance(v, OpTotals) else OpTotals(day_usd=D(v))
        for op, v in day.items()
    }
    total = sum((t.day_usd for t in ops.values()), D(0))
    return Ledger(month_usd=max(D(month), total), ops=ops)


def code(operation: str, est: str | None, led: Ledger, **kw: Any) -> str | None:
    kw.setdefault("local_time", NOON)
    kw.setdefault("daily_cap", CAP)
    kw.setdefault("monthly_cap", MONTH_CAP)
    return decide(operation, None if est is None else D(est), led, **kw)  # type: ignore[arg-type]


def test_an_empty_ledger_lets_every_callable_op_through():
    night = time(3, 0)  # inside Yandex's window, where every op may run
    for op in usage.CALLABLE:
        est = "0" if op in usage.FREE_OPERATIONS else "0.001"
        assert code(op, est, ledger(), local_time=night) is None, op


@pytest.mark.parametrize("est", [None, "NaN", "Infinity", "sNaN"])
def test_an_unpriced_estimate_is_refused_first(est):
    everything_breached = ledger(month="300", other="20")
    assert code("classify", est, everything_breached) == "unpriced"


def test_the_order_is_month_then_day_then_family_then_operation():
    # Each state breaches its rung AND every rung after it, so only the order
    # decides the answer; each next state lifts the breach just reported.
    anthropic_full = {
        "classify": "0.30",  # at its allocation
        "verdict": "2.25",  # 0.75 over: the whole model_reserve borrowed
        "draft": "0.20",
        "company_verdict": "1.50",
        "enrich_search": "0.75",
    }  # Σ 5.00 = FAMILY_CAPS["anthropic"]
    ladder = [
        (ledger(month="249.995", other="5.00", **anthropic_full), "cost_cap_monthly"),
        (ledger(other="5.00", **anthropic_full), "cost_cap_daily"),
        (ledger(**anthropic_full), "cost_cap_family:anthropic"),
        (ledger(classify="0.30", verdict="2.25"), "cost_cap_op:classify"),
        (ledger(classify="0.29"), None),
    ]
    assert [code("classify", "0.01", led) for led, _ in ladder] == [c for _, c in ladder]


def test_the_daily_boundary_is_reaching_the_cap():
    assert code("classify", "0.01", ledger(other="9.98")) is None
    assert code("classify", "0.01", ledger(other="9.99")) == "cost_cap_daily"


def test_the_monthly_rung_counts_the_whole_month():
    assert code("classify", "0.01", ledger(month="249.98")) is None
    assert code("classify", "0.01", ledger(month="249.99")) == "cost_cap_monthly"


def test_a_claude_op_borrows_the_unspent_model_reserve():
    # classify is at its own 0.30; the reserve (0.75) still has room.
    assert code("classify", "0.01", ledger(classify="0.30")) is None
    # verdict already borrowed 0.70 of it: 0.05 is left.
    assert code("classify", "0.04", ledger(classify="0.30", verdict="2.20")) is None
    assert code("classify", "0.05", ledger(classify="0.30", verdict="2.20")) == (
        "cost_cap_op:classify"
    )


def test_a_serp_op_borrows_serp_reserve_only_with_the_owners_switch():
    spent = ledger(serp_brave="0.165")
    assert code("serp_brave", "0.005", spent) == "cost_cap_op:serp_brave"
    assert code("serp_brave", "0.005", spent, serp_overflow=True) is None


def test_brave_stops_where_its_allocation_is_reached():
    # 07 §1.9 pins both 0.165 and «reaching a cap is over it», so 32 queries a
    # day pass and the 33rd (spend 0.160 + 0.005 = 0.165) is refused — not the
    # «33 queries/day» the spec writes beside the number (a contradiction in
    # the spec; 32 × 31 days still stays under the $5 / 1,000-query credit).
    assert code("serp_brave", "0.005", ledger(serp_brave="0.155")) is None
    assert (
        code("serp_brave", "0.005", ledger(serp_brave="0.160"))
        == "cost_cap_op:serp_brave"
    )


def test_apify_and_places_never_borrow():
    # The Apify family has room (2.00), apify_olx's own 0.60 has not.
    assert code("apify_olx", "0.02", ledger(apify_olx="0.59")) == "cost_cap_op:apify_olx"
    # Places: its one paid op's allocation is the family cap.
    assert code(
        "places_details_contact", "0.02", ledger(places_details_contact="0.99")
    ) == ("cost_cap_family:places")


def test_a_free_call_skips_the_money_rungs():
    capped = ledger(month="250", other="10")
    assert code("serp_searxng", "0", capped) is None
    assert code("places_text_ids", "0", capped) is None
    assert code("serp_searxng", "0.01", capped) == "cost_cap_monthly"


PAID = sorted(usage.CALLABLE - usage.FREE_OPERATIONS - {"places_details_contact"})


@pytest.mark.parametrize("operation", PAID)
def test_a_paid_call_estimated_at_zero_is_unpriced(operation):
    # $0 is no price for a call that costs money; past a cap it would skip
    # every money rung (04 §8 (1)–(5) have no exception for a zero estimate).
    assert code(operation, "0", ledger(), local_time=time(3)) == "unpriced"
    assert code(operation, "0", ledger(month="300", other="20")) == "unpriced"
    assert code(operation, "0.001", ledger(month="300", other="20")) == (
        "cost_cap_monthly"
    )


def test_places_details_are_free_only_inside_the_months_free_tier():
    def places(month_units: int, day: str = "0") -> Ledger:
        details = OpTotals(month_units=D(month_units))
        return ledger(month=day, other=day, places_details_contact=details)

    assert code("places_details_contact", "0", places(999)) is None  # the 1,000th
    assert code("places_details_contact", "0", places(1000)) == "unpriced"
    assert code("places_details_contact", "0.02", places(1000)) is None
    # Inside the tier a $0 request runs even at the daily cap, like a free op.
    assert code("places_details_contact", "0", places(5, day="10")) is None


def test_brave_is_refused_at_the_1001st_query_of_the_month():
    month = OpTotals(month_units=D(999))
    assert code("serp_brave", "0.005", ledger(serp_brave=month)) is None
    full = OpTotals(month_units=D(1000))
    assert code("serp_brave", "0.005", ledger(serp_brave=full)) == (
        "cost_cap_month_op:serp_brave"
    )


@pytest.mark.parametrize(
    ("operation", "cap"),
    [
        ("serp_searxng", 300),
        ("serp_ddg", 40),
        ("places_text_ids", 200),
        ("twogis_api", 40),
        ("serp_yandex_night", 35),
    ],
)
def test_free_ops_are_capped_by_their_daily_query_count(operation, cap):
    local = time(3, 0)  # inside Yandex's night window
    under = ledger(**{operation: OpTotals(day_units=D(cap - 1))})
    full = ledger(**{operation: OpTotals(day_units=D(cap))})
    assert code(operation, "0", under, local_time=local) is None
    assert code(operation, "0", full, local_time=local) == f"cost_cap_day_op:{operation}"


def test_yandex_runs_only_in_its_night_window_and_free_quota():
    for t, expected in [
        (time(1, 59), "op_window:serp_yandex_night"),
        (time(2, 0), None),
        (time(9, 59), None),
        (time(10, 0), "op_window:serp_yandex_night"),
    ]:
        assert code("serp_yandex_night", "0", ledger(), local_time=t) == expected
    used = ledger(serp_yandex_night=OpTotals(month_units=D(1000)))
    assert code("serp_yandex_night", "0", used, local_time=time(3)) == (
        "cost_cap_month_op:serp_yandex_night"
    )


def test_anthropic_silence_refuses_claude_ops_only():
    assert code("classify", "0.001", ledger(), anthropic_down=True) == "anthropic_silence"
    assert code("serp_brave", "0.005", ledger(), anthropic_down=True) is None


# --- alerts (pure) ---------------------------------------------------------------------


def alerts(
    day: str = "0",
    month: str = "0",
    capped: frozenset[str] = frozenset(),
    **families: str,
) -> dict[str, str]:
    snap = costs.SpendSnapshot(
        day_usd=D(day),
        month_usd=max(D(month), D(day)),
        family_day_usd={f: D(v) for f, v in families.items()},  # type: ignore[misc]
        capped=capped,
    )
    found = costs.spend_alerts(snap, daily_cap=CAP, monthly_cap=MONTH_CAP, alert_pct=80)
    return {a.key: a.text for a in found}


def test_spend_high_is_one_key_whose_text_names_each_scope():
    assert alerts(day="7.99") == {}
    assert alerts(day="8.50") == {
        "spend_high": "⚠️ Kunlik xarajat 85% ga yetdi ($8.50/$10)"
    }
    both = alerts(day="4.10", anthropic="4.10")
    assert both == {"spend_high": "⚠️ Anthropic xarajati 82% ga yetdi ($4.10/$5)"}
    month = alerts(day="1", month="202.50")
    assert month == {"spend_high": "⚠️ Oylik xarajat 81% ga yetdi ($202.50/$250)"}


def test_cost_cap_holds_at_either_cap():
    day = alerts(day="10")
    assert day["cost_cap"].startswith("💸 Kunlik xarajat chegarasi ($10)")
    assert "Kunlik xarajat 100%" in day["spend_high"]
    month = alerts(day="2", month="250")
    assert month["cost_cap"].startswith("💸 Oylik xarajat chegarasi ($250)")


def test_cost_cap_holds_while_ask_refused_at_a_cap_below_it():
    """ask refuses at spend + est ≥ cap: $9.95 spent and a $0.10 call refused
    is the cap in force though the spend never reached $10."""
    assert "cost_cap" not in alerts(day="9.95")
    daily = alerts(day="9.95", capped=frozenset({costs.CAP_DAILY}))
    assert daily["cost_cap"].startswith("💸 Kunlik xarajat chegarasi ($10)")
    # The monthly cap names itself first, as decide() orders the rungs.
    both = alerts(day="1", capped=frozenset({costs.CAP_DAILY, costs.CAP_MONTHLY}))
    assert both["cost_cap"].startswith("💸 Oylik xarajat chegarasi ($250)")


# === Postgres ==========================================================================


async def seed(
    session: AsyncSession,
    usd: str,
    *,
    at: datetime = NOW,
    operation: str = "other",
    units: str | None = None,
    source_key: str | None = None,
) -> None:
    """A row as an earlier call left it (committed, so ask's own transaction sees it)."""
    session.add(
        UsageLog(
            provider=usage.PROVIDER[operation],  # type: ignore[index]
            operation=operation,
            model="seed",
            cost_usd=D(usd),
            units=None if units is None else D(units),
            source_key=source_key,
            created_at=at,
        )
    )
    await session.commit()


async def rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Every usage_log row as stored now, oldest first."""
    result = await session.execute(sa.select(UsageLog.__table__).order_by(UsageLog.id))
    return [dict(r) for r in result.mappings().all()]


def sdk_usage(**counts: int) -> SimpleNamespace:
    return SimpleNamespace(**counts)


class Calls:
    """A fake paid API: counts calls, answers or raises."""

    def __init__(self, answer: Any = "ok", raises: BaseException | None = None) -> None:
        self.n = 0
        self.answer = answer
        self.raises = raises

    async def __call__(self) -> Any:
        self.n += 1
        if self.raises is not None:
            raise self.raises
        return self.answer


async def classify(
    session: AsyncSession,
    est: str | None = "0.001",
    *,
    now: datetime = NOW,
    calls: Calls | None = None,
    **kw: Any,
) -> Any:
    calls = calls or Calls(
        SimpleNamespace(
            usage=sdk_usage(
                input_tokens=300, output_tokens=150, cache_read_input_tokens=900
            )
        )
    )
    return await costs.ask(
        session,
        operation="classify",
        est_usd=None if est is None else D(est),
        call=calls,
        price=lambda r: usage.priced_anthropic(EXTRACT, r.usage),
        now=now,
        **kw,
    )


async def brave(
    session: AsyncSession, *, now: datetime = NOW, calls: Calls | None = None
) -> Any:
    return await costs.ask(
        session,
        operation="serp_brave",
        est_usd=usage.unit_cost("serp_brave", 1),
        call=calls or Calls(),
        price=lambda _: Priced(usage.unit_cost("serp_brave", 1), units=D(1), model="web"),
        now=now,
    )


async def free(session: AsyncSession, operation: str, *, now: datetime = NOW) -> Any:
    return await costs.ask(
        session,
        operation=operation,  # type: ignore[arg-type]
        est_usd=D(0),
        call=Calls(),
        price=lambda _: Priced(D(0), units=D(1)),
        now=now,
    )


async def test_a_call_under_the_caps_is_made_once_and_its_row_is_the_answers_price(
    session,
):
    calls = Calls(
        SimpleNamespace(
            usage=sdk_usage(
                input_tokens=300, output_tokens=150, cache_read_input_tokens=900
            )
        )
    )
    result = await classify(session, "0.0005", calls=calls, source_key="telegram_monitor")
    assert calls.n == 1 and result is calls.answer
    [row] = await rows(session)
    assert row["provider"] == "anthropic" and row["operation"] == "classify"
    assert row["model"] == EXTRACT and row["batch"] is False
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (
        300,
        150,
        900,
    )
    assert row["cost_usd"] == D("0.000114") and row["units"] == D(0)
    assert row["source_key"] == "telegram_monitor"
    assert row["created_at"] == NOW


async def test_a_refusal_is_one_zero_row_and_nothing_else(session):
    await seed(session, "10.00")
    calls = Calls()
    with pytest.raises(CostCapReached) as refused:
        await classify(session, calls=calls)
    assert (
        refused.value.code == "cost_cap_daily" and str(refused.value) == "cost_cap_daily"
    )
    assert calls.n == 0
    seeded, refusal = await rows(session)
    assert refusal | {"id": None} == {
        "id": None,
        "provider": "anthropic",
        "model": "refused:cost_cap_daily",
        "operation": "classify",
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "units": D(0),
        "batch": False,
        "cost_usd": D(0),
        "source_key": None,
        "company_id": None,
        "model_job_id": None,
        "collector_run_id": None,
        "created_at": NOW,
    }
    assert costs.refused_until("classify", NOW) == datetime(2026, 10, 23, 15, tzinfo=TZ)


async def test_a_refused_op_is_not_asked_again_before_the_next_hour(session):
    await seed(session, "10.00")
    with pytest.raises(CostCapReached):
        await classify(session)
    with pytest.raises(CostCapReached) as again:
        await classify(session, now=NOW.replace(minute=59))
    assert again.value.code == "cost_cap_daily"
    assert len(await rows(session)) == 2  # the seed and one refusal
    with pytest.raises(CostCapReached):
        await classify(session, now=NOW.replace(hour=15, minute=0))
    assert [r["model"] for r in await rows(session)] == [
        "seed",
        "refused:cost_cap_daily",
        "refused:cost_cap_daily",
    ]


async def test_spend_counts_refusals_apart_from_the_money(session):
    await classify(session, "0.001")
    await seed(session, "10.00")
    with pytest.raises(CostCapReached):
        await classify(session, "0.001", now=NOW.replace(minute=30))
    today = await costs.spend(
        session, since=costs.day_start(NOW), operations=["classify"]
    )
    assert today == costs.Spend(
        usd=D("0.000114"), unpriced=0, calls=1, refused=1, units=D(0)
    )


async def test_the_day_is_tashkent_s_and_reaching_the_cap_is_over_it(session):
    late = datetime(2026, 10, 23, 23, 59, tzinfo=TZ)
    await seed(session, "9.98", at=late)
    await classify(session, "0.01", now=late.replace(second=10))  # 9.98 + 0.01 passes
    # That call cost 0.000114; top the day up to exactly 9.99.
    await seed(session, "0.009886", at=late.replace(second=20))
    with pytest.raises(CostCapReached) as refused:
        await classify(session, "0.01", now=late.replace(second=30))
    assert refused.value.code == "cost_cap_daily"
    # 00:00:30 in Tashkent is still 23 October in UTC: a UTC day would refuse.
    after_midnight = datetime(2026, 10, 23, 19, 0, 30, tzinfo=UTC)
    costs.clear_refusals()
    await classify(session, "0.01", now=after_midnight)
    assert [r["model"] for r in await rows(session)] == [
        "seed",
        EXTRACT,
        "seed",
        "refused:cost_cap_daily",
        EXTRACT,
    ]


async def test_the_month_counts_earlier_days_but_not_last_month(session):
    # 23:00 on 30 September in Tashkent: still September, whatever UTC says.
    await seed(session, "100", at=datetime(2026, 9, 30, 23, 0, tzinfo=TZ))
    await seed(session, "249.98", at=datetime(2026, 10, 2, 9, 0, tzinfo=TZ))
    await classify(session, "0.01")  # 249.98 + 0.01: September is not counted
    await seed(session, "0.01", at=datetime(2026, 10, 3, 9, 0, tzinfo=TZ))
    with pytest.raises(CostCapReached) as refused:
        await classify(session, "0.01", now=NOW.replace(hour=16))
    assert refused.value.code == "cost_cap_monthly"


async def test_a_free_call_still_runs_at_the_daily_cap(session):
    await seed(session, "10.00")
    assert await free(session, "serp_searxng") == "ok"
    [_, row] = await rows(session)
    assert (row["operation"], row["cost_usd"], row["units"]) == (
        "serp_searxng",
        D(0),
        D(1),
    )


async def test_a_free_op_is_refused_past_its_daily_count(session):
    await seed(session, "0", operation="serp_ddg", units="40")
    with pytest.raises(CostCapReached) as refused:
        await free(session, "serp_ddg")
    assert refused.value.code == "cost_cap_day_op:serp_ddg"


async def test_brave_is_refused_at_the_1001st_unit_of_the_calendar_month(session):
    sept = datetime(2026, 9, 28, 10, 0, tzinfo=TZ)
    await seed(session, "0", operation="serp_brave", units="1000", at=sept)
    await seed(
        session,
        "0",
        operation="serp_brave",
        units="999",
        at=datetime(2026, 10, 2, 10, 0, tzinfo=TZ),
    )
    assert await brave(session) == "ok"  # the 1,000th of October
    with pytest.raises(CostCapReached) as refused:
        await brave(session, now=NOW.replace(hour=16))
    assert refused.value.code == "cost_cap_month_op:serp_brave"
    units = await costs.month_units(session, operation="serp_brave", now=NOW)
    assert units == D(1000)


async def test_a_serp_op_borrows_only_with_the_owners_switch(session):
    await seed(session, "0.165", operation="serp_brave", units="33")
    with pytest.raises(CostCapReached) as refused:
        await brave(session)
    assert refused.value.code == "cost_cap_op:serp_brave"

    row = (
        await session.execute(
            sa.text("SELECT config FROM sources WHERE key = 'serp_layer'")
        )
    ).scalar_one()
    try:
        await session.execute(
            sa.text(
                'UPDATE sources SET config = config || \'{"serp_paid_overflow": "true"}\''
                " WHERE key = 'serp_layer'"
            )
        )
        await session.commit()
        assert await costs.serp_overflow(session) is False  # a string is not the switch
        await session.execute(
            sa.text(
                "UPDATE sources SET config = config || '{\"serp_paid_overflow\": true}'"
                " WHERE key = 'serp_layer'"
            )
        )
        await session.commit()
        assert await costs.serp_overflow(session) is True
        costs.clear_refusals()
        assert await brave(session, now=NOW.replace(hour=15)) == "ok"
    finally:
        await session.execute(
            sa.text(
                "UPDATE sources SET config = CAST(:c AS jsonb) WHERE key = 'serp_layer'"
            ),
            {"c": json.dumps(row)},
        )
        await session.commit()


async def test_a_call_that_raised_after_dispatch_keeps_the_estimate_with_no_units(
    session,
):
    with pytest.raises(TimeoutError):
        await classify(session, "0.004", calls=Calls(raises=TimeoutError()))
    with pytest.raises(ConnectionResetError):
        await brave(session, calls=Calls(raises=ConnectionResetError()))
    first, second = await rows(session)
    # The estimate stays counted and the row says the call failed — not a call
    # still in flight (whose claim carries no model): 06 §4.2 anthropic_silence
    # counts only failed attempts.
    assert (first["operation"], first["cost_usd"], first["units"], first["model"]) == (
        "classify",
        D("0.004"),
        D(0),
        "failed:TimeoutError",
    )
    # Brave's query was reserved as one unit before the call and given back.
    assert (second["operation"], second["cost_usd"], second["units"]) == (
        "serp_brave",
        D("0.005"),
        D(0),
    )
    assert second["model"] == "failed:ConnectionResetError"
    # Both still count against the caps: a failed call never undercounts.
    assert (await costs.read_ledger(session, NOW)).day_usd == D("0.009")


async def test_an_unknown_extract_model_refuses_every_classify(session, monkeypatch):
    monkeypatch.setattr(settings, "extract_model", "model-nobody-priced")
    est = usage.anthropic_cost_usd(
        settings.extract_model, input_tokens=300, output_tokens=400
    )
    assert est is None
    calls = Calls()
    with pytest.raises(CostCapReached) as refused:
        await costs.ask(
            session,
            operation="classify",
            est_usd=est,
            call=calls,
            price=lambda r: usage.priced_anthropic(settings.extract_model, r),
            now=NOW,
        )
    assert refused.value.code == "unpriced" and calls.n == 0
    today = await costs.spend(session, since=costs.day_start(NOW))
    assert (today.unpriced, today.refused, today.usd) == (1, 1, D(0))


async def test_programming_errors_are_not_budget_refusals(session):
    async def ask(**kw: Any) -> Any:
        params = {
            "operation": "classify",
            "est_usd": D("0.001"),
            "call": Calls(),
            "price": lambda _: Priced(D(0)),
            "now": NOW,
        } | kw
        return await costs.ask(session, **params)

    for operation in ("model_reserve", "serp_reserve", "other"):
        with pytest.raises(ValueError):
            await ask(operation=operation)
    with pytest.raises(TypeError):
        await ask(est_usd=0.001)  # a float is not a price
    with pytest.raises(ValueError):
        await ask(est_usd=D("-0.01"))
    with pytest.raises(ValueError):
        await ask(now=datetime(2026, 10, 23, 14, 0))
    assert await rows(session) == []


async def test_batch_provisional_rows_are_updated_in_place(session):
    jobs = [
        (
            await session.execute(
                sa.text(
                    "INSERT INTO model_jobs (kind, lane, model, custom_id) "
                    "VALUES ('company_verdict', 'batch', :m, :c) RETURNING id"
                ),
                {"m": BATCH, "c": f"cs-{uuid.uuid4()}"},
            )
        ).scalar_one()
        for _ in range(3)
    ]
    await session.commit()
    result = await costs.ask(
        session,
        operation="company_verdict",
        est_usd=D("0.045"),
        call=Calls("msgbatch_1"),
        price=lambda _: [
            Priced(D("0.015"), model=BATCH, batch=True, model_job_id=j) for j in jobs
        ],
        now=NOW.replace(hour=23, minute=30),
    )
    assert result == "msgbatch_1"
    provisional = await rows(session)
    assert [
        (r["model_job_id"], r["cost_usd"], r["input_tokens"]) for r in provisional
    ] == [(j, D("0.015"), None) for j in jobs]

    collected = NOW + timedelta(hours=12)
    for j in jobs:
        real = usage.priced_anthropic(
            BATCH,
            sdk_usage(input_tokens=4000, output_tokens=600),
            batch=True,
            model_job_id=j,
        )
        assert await costs.settle_provisional(
            session, model_job_id=j, priced=real, now=collected
        )
    await session.commit()
    settled = await rows(session)
    assert len(settled) == 3
    assert [r["id"] for r in settled] == [r["id"] for r in provisional]
    for r in settled:
        assert (r["cost_usd"], r["input_tokens"], r["output_tokens"], r["batch"]) == (
            D("0.014000"),
            4000,
            600,
            True,
        )
        assert r["created_at"] == NOW.replace(hour=23, minute=30)

    # A settled job has no provisional row left: the spend is inserted, never lost.
    again = usage.priced_anthropic(BATCH, sdk_usage(input_tokens=10), batch=True)
    assert not await costs.settle_provisional(
        session, model_job_id=jobs[0], priced=again, now=collected
    )
    await session.commit()
    assert len(await rows(session)) == 4


async def test_two_concurrent_calls_near_the_cap_cannot_both_pass(session, monkeypatch):
    await seed(session, "9.98")
    # Hold each ask right after its ledger read until the other has read too
    # (or 0.3 s passed): without the ledger lock both then decide on the same
    # stale total; with it the second read waits for the first claim.
    real_read, reads = costs.read_ledger, []

    async def read_then_wait(s: AsyncSession, now: datetime) -> Ledger:
        totals = await real_read(s, now)
        reads.append(now)
        for _ in range(30):
            if len(reads) >= 2:
                break
            await asyncio.sleep(0.01)
        return totals

    monkeypatch.setattr(costs, "read_ledger", read_then_wait)

    async def one() -> str:
        # The answer costs its estimate, so whichever order the two run in,
        # the first one's row keeps the day at 9.99.
        async with SessionLocal() as own:
            try:
                await costs.ask(
                    own,
                    operation="classify",
                    est_usd=D("0.01"),
                    call=Calls(),
                    price=lambda _: Priced(D("0.01"), model=EXTRACT),
                    now=NOW,
                )
            except CostCapReached as exc:
                return exc.code
            finally:
                await own.rollback()
            return "passed"

    outcomes = await asyncio.gather(one(), one())
    assert len(reads) == 2
    assert sorted(outcomes) == ["cost_cap_daily", "passed"]
    day = await costs.spend(session, since=costs.day_start(NOW), operations=["classify"])
    assert (day.calls, day.refused) == (1, 1)


async def test_the_per_source_budget_is_the_column_minus_todays_spend(session):
    # olx_business is seeded with daily_cost_cap_usd = 0.60 (03 §3).
    await seed(session, "0.25", operation="apify_olx", source_key="olx_business")
    await seed(session, "0.30", operation="apify_olx", source_key="instagram_discovery")
    await seed(
        session,
        "0.50",
        operation="apify_olx",
        source_key="olx_business",
        at=NOW - timedelta(days=1),
    )
    left = await costs.source_budget_usd(session, source_key="olx_business", now=NOW)
    assert left == D("0.35")
    assert (
        await costs.source_budget_usd(session, source_key="no_such_source", now=NOW) == 0
    )

    # The per-source budget is the collector's layer, checked before ask;
    # ask itself does not read it.
    await seed(session, "0.35", operation="other", source_key="olx_business")
    assert await costs.source_budget_usd(session, source_key="olx_business", now=NOW) == 0
    await costs.ask(
        session,
        operation="apify_olx",
        est_usd=D("0.00199"),
        call=Calls(),
        price=lambda _: Priced(D("0.00199"), units=D(1), model="actor"),
        now=NOW,
        source_key="olx_business",
    )


async def test_a_parent_rows_budget_counts_its_sub_sources(session):
    # serp_layer (cap 1.00, 03 §3) is budgeted for the run while ask records
    # the sub-source the call was made for (search_brave); maps likewise. A
    # sibling parent's sub-source does not count, nor does the parent's
    # spend count against a sub-source.
    await seed(session, "0.20", operation="serp_brave", source_key="search_brave")
    await seed(session, "0.05", operation="serp_brave", source_key="serp_layer")
    await seed(session, "0.30", operation="places_text_ids", source_key="maps_google")
    left = await costs.source_budget_usd(session, source_key="serp_layer", now=NOW)
    assert left == D("0.75")
    left = await costs.source_budget_usd(session, source_key="maps", now=NOW)
    assert left == D("1.10")
    assert await costs.source_budget_usd(
        session, source_key="search_brave", now=NOW
    ) == D("0")  # its own column is 0: a sub-source is never the budgeted row
    exact = await costs.spend(
        session, since=costs.day_start(NOW), source_key="serp_layer"
    )
    assert exact.usd == D("0.05")


async def test_the_answer_is_committed_even_when_its_job_row_is_not(session):
    # An instant job inserts its model_jobs row and calls inside one
    # transaction (04 §4.8): the money must not wait for that commit.
    job = (
        await session.execute(
            sa.text(
                "INSERT INTO model_jobs (kind, lane, model, custom_id) "
                "VALUES ('classify', 'instant', :m, 'cs-uncommitted') RETURNING id"
            ),
            {"m": EXTRACT},
        )
    ).scalar_one()
    await classify(session, "0.001", model_job_id=job)
    async with SessionLocal() as other:
        [seen] = await rows(other)
        assert seen["cost_usd"] == D("0.000114") and seen["model_job_id"] is None
    await session.commit()
    async with SessionLocal() as other:
        [seen] = await rows(other)
        assert seen["model_job_id"] == job


async def test_the_answer_stays_counted_when_the_callers_transaction_rolls_back(session):
    await session.execute(
        sa.text(
            "INSERT INTO model_jobs (kind, lane, model, custom_id) "
            "VALUES ('classify', 'instant', 'm', 'cs-rolled-back')"
        )
    )
    job = (
        await session.execute(
            sa.text("SELECT id FROM model_jobs WHERE custom_id = 'cs-rolled-back'")
        )
    ).scalar_one()
    await classify(session, "0.001", model_job_id=job)
    await session.rollback()
    [row] = await rows(session)
    assert row["cost_usd"] == D("0.000114") and row["model_job_id"] is None


async def test_without_an_own_transaction_the_callers_savepoint_takes_the_row(
    session, monkeypatch
):
    @asynccontextmanager
    async def unavailable() -> AsyncIterator[AsyncSession]:
        raise sa.exc.OperationalError("connect", {}, Exception("pool exhausted"))
        yield  # pragma: no cover

    monkeypatch.setattr(costs, "_own_session", unavailable)
    await classify(session, "0.001")
    async with SessionLocal() as other:
        assert await rows(other) == []  # not committed yet: it is the caller's
    [row] = await rows(session)
    assert row["cost_usd"] == D("0.000114") and row["model"] == EXTRACT
    held = await session.scalar(
        sa.text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
    )
    assert held == 0  # the session lock was given back


async def test_anthropic_silence_refuses_claude_calls_while_health_says_so(session):
    async def down(_: AsyncSession, now: datetime) -> datetime | None:
        return now + timedelta(minutes=30)

    costs.set_anthropic_health(down)
    with pytest.raises(CostCapReached) as refused:
        await classify(session)
    assert refused.value.code == "anthropic_silence"
    assert await free(session, "serp_searxng") == "ok"
    assert [r["model"] for r in await rows(session)] == [
        "refused:anthropic_silence",
        None,
    ]


class Owner:
    """The bot's notify(): records what it was told, says whether it arrived."""

    def __init__(self, delivered: bool = True) -> None:
        self.told: list[str] = []
        self.delivered = delivered

    async def __call__(self, text: str) -> bool:
        self.told.append(text)
        return self.delivered


async def alert_rows(session: AsyncSession) -> list[tuple[str, str, datetime]]:
    result = await session.execute(
        sa.text("SELECT kind, ref, sent_at FROM reminder_log ORDER BY id")
    )
    return [tuple(r) for r in result.all()]  # type: ignore[misc]


async def test_cost_cap_is_told_first_by_ask_once_per_tashkent_day(session):
    owner = Owner()
    costs.set_cap_notifier(owner)
    await seed(session, "10.00", at=NOW.replace(hour=9))
    with pytest.raises(CostCapReached):
        await classify(session)
    with pytest.raises(CostCapReached):
        await classify(session, now=NOW.replace(hour=16, minute=5))
    assert owner.told == [
        "💸 Kunlik xarajat chegarasi ($10) — ertagacha pullik chaqiruvlar toʻxtadi; "
        "kollektorlar bepul qismini davom ettiradi"
    ]
    assert await alert_rows(session) == [("alert:cost_cap", "cost_cap", NOW)]

    tomorrow = NOW + timedelta(days=1)
    await seed(session, "10.00", at=tomorrow.replace(hour=9))
    with pytest.raises(CostCapReached):
        await classify(session, now=tomorrow)
    assert len(owner.told) == 2 and len(await alert_rows(session)) == 2


async def test_an_undelivered_cost_cap_is_not_marked(session):
    owner = Owner(delivered=False)
    costs.set_cap_notifier(owner)
    await seed(session, "10.00", at=NOW.replace(hour=9))
    with pytest.raises(CostCapReached):
        await classify(session)
    with pytest.raises(CostCapReached):
        await classify(session, now=NOW.replace(hour=15))
    assert len(owner.told) == 2 and await alert_rows(session) == []


class SlowOwner(Owner):
    """A notify() that takes a while, as a Telegram send does: the window in
    which every other refusal at the cap could decide it is due as well."""

    async def __call__(self, text: str) -> bool:
        self.told.append(text)
        await asyncio.sleep(0.05)
        return self.delivered


async def test_refusals_at_the_cap_that_land_together_tell_the_owner_once(session):
    # 07 §1.9: cost_cap fires once per Tashkent day, however many processes
    # (or ops in one process: the refusal memory is per op) hit it at once.
    owner = SlowOwner()
    costs.set_cap_notifier(owner)
    await seed(session, "10.00", at=NOW.replace(hour=9))

    async def refused(operation: str, est: str) -> str:
        async with SessionLocal() as own:
            try:
                await costs.ask(
                    own,
                    operation=operation,  # type: ignore[arg-type]
                    est_usd=D(est),
                    call=Calls(),
                    price=lambda _: Priced(D(est), units=D(1)),
                    now=NOW,
                )
            except CostCapReached as exc:
                return exc.code
            finally:
                await own.rollback()
            return "passed"

    codes = await asyncio.gather(
        refused("classify", "0.001"),
        refused("serp_brave", "0.005"),
        refused("places_details_contact", "0.020"),
        refused("verdict", "0.01"),
    )
    assert codes == ["cost_cap_daily"] * 4
    assert len(owner.told) == 1
    assert await alert_rows(session) == [("alert:cost_cap", "cost_cap", NOW)]


async def test_two_claims_of_one_alert_at_once_cannot_both_win(session, monkeypatch):
    # Hold each claim right after its «is it due» read until the other has
    # read too (or 0.3 s passed): without the claim lock both then write a
    # claim; with it the second read waits for the first claim's commit.
    real_due, reads = costs.alert_due, []

    async def due_then_wait(s: AsyncSession, key: str, now: datetime) -> bool:
        due = await real_due(s, key, now)
        reads.append(key)
        for _ in range(30):
            if len(reads) >= 2:
                break
            await asyncio.sleep(0.01)
        return due

    monkeypatch.setattr(costs, "alert_due", due_then_wait)

    async def claim() -> int | None:
        async with session_scope() as own:
            return await costs.claim_alert(own, "cost_cap", NOW)

    won = await asyncio.gather(claim(), claim())
    assert sorted(w is not None for w in won) == [False, True]
    assert len(await alert_rows(session)) == 1


async def test_a_cost_cap_health_already_told_is_not_told_again_by_ask(session):
    # fire_spend_alerts (health_job) and ask share the claim of alert:cost_cap.
    owner = Owner()
    costs.set_cap_notifier(owner)
    await seed(session, "10.00", at=NOW.replace(hour=9))
    fired = await costs.fire_spend_alerts(session, now=NOW, notify=owner)
    assert "cost_cap" in fired
    with pytest.raises(CostCapReached):
        await classify(session, now=NOW.replace(hour=15))
    told_cap = [t for t in owner.told if t.startswith("💸")]
    assert len(told_cap) == 1


async def test_spend_high_fires_once_per_tashkent_day(session):
    owner = Owner()
    await seed(session, "8.50", at=NOW.replace(hour=9))
    assert await costs.fire_spend_alerts(session, now=NOW, notify=owner) == ["spend_high"]
    await session.commit()
    assert (
        await costs.fire_spend_alerts(session, now=NOW.replace(hour=20), notify=owner)
        == []
    )
    assert owner.told == ["⚠️ Kunlik xarajat 85% ga yetdi ($8.50/$10)"]

    tomorrow = NOW + timedelta(days=1)
    assert await costs.fire_spend_alerts(session, now=tomorrow, notify=owner) == []
    await seed(session, "9.00", at=tomorrow.replace(hour=9))
    fired = await costs.fire_spend_alerts(session, now=tomorrow, notify=owner)
    assert fired == ["spend_high"]
    await session.commit()
    assert [k for k, _, _ in await alert_rows(session)] == ["alert:spend_high"] * 2


async def new_company(session: AsyncSession, name: str) -> uuid.UUID:
    company = (
        await session.execute(
            sa.text(
                "INSERT INTO companies (display_name, name_key) "
                "VALUES (:n, lower(:n)) RETURNING id"
            ),
            {"n": name},
        )
    ).scalar_one()
    await session.commit()
    return company  # type: ignore[no-any-return]


async def spent_on(session: AsyncSession, company: uuid.UUID, usd: str) -> None:
    await costs.ask(
        session,
        operation="classify",
        est_usd=D(usd),
        call=Calls(),
        price=lambda _: Priced(D(usd), model=EXTRACT),
        now=NOW,
        company_id=company,
    )


async def test_the_report_has_what_xarajat_prints(session):
    company = await new_company(session, "Namuna Tekstil Savdo")
    await classify(session, "0.001", company_id=company)
    await brave(session)
    costs.set_anthropic_health(_always_down)
    with pytest.raises(CostCapReached):
        await costs.ask(
            session,
            operation="verdict",
            est_usd=D("0.011"),
            call=Calls(),
            price=lambda _: Priced(D("0.011")),
            now=NOW.replace(hour=16),
        )
    costs.set_anthropic_health(None)
    await seed(session, "1.00", operation="apify_olx", at=NOW - timedelta(days=3))
    await seed(session, "10.00")
    with pytest.raises(CostCapReached):
        await classify(session, now=NOW.replace(hour=17))

    report = await costs.report(session, now=NOW)
    assert report.day["classify"] == costs.OpLine(
        calls=1,
        units=D(0),
        usd=D("0.000114"),
        refused=1,
        refused_by_code={"cost_cap_daily": 1},
    )
    assert report.day["serp_brave"] == costs.OpLine(calls=1, units=D(1), usd=D("0.005"))
    assert "apify_olx" not in report.day
    assert report.month["apify_olx"].usd == D("1.00")
    assert report.total(report.day, "anthropic") == D("0.000114")
    assert report.total(report.day) == D("10.005114")
    assert report.total(report.month) == D("11.005114")
    assert report.refusals(report.day) == 2
    # 06 §1.2: refusals counted per code, and the lines by provider too.
    assert report.refusals_by_code(report.day) == {
        "cost_cap_daily": 1,
        "anthropic_silence": 1,
    }
    assert report.day_by_provider == {
        "anthropic": costs.OpLine(
            calls=1,
            units=D(0),
            usd=D("0.000114"),
            refused=2,
            refused_by_code={"cost_cap_daily": 1, "anthropic_silence": 1},
        ),
        "brave": costs.OpLine(calls=1, units=D(1), usd=D("0.005")),
        "other": costs.OpLine(calls=1, units=D(0), usd=D("10.00")),
    }
    assert report.month_by_provider["apify"] == costs.OpLine(
        calls=1, units=D(0), usd=D("1.00")
    )
    assert report.top_companies == (
        costs.TopCompany(company, "Namuna Tekstil Savdo", D("0.000114")),
    )
    assert (report.daily_cap, report.monthly_cap) == (CAP, MONTH_CAP)


async def _always_down(_: AsyncSession, now: datetime) -> datetime | None:
    return now + timedelta(minutes=30)


async def test_the_report_names_the_top_five_companies_by_spend(session):
    spenders = [await new_company(session, f"Namuna Savdo {n}") for n in range(1, 7)]
    for n, company in enumerate(spenders, start=1):
        await spent_on(session, company, f"0.00{n}")  # 1 spends least, 6 most
    refused_only = await new_company(session, "Namuna Faqat Rad")
    await seed(session, "10.00")
    with pytest.raises(CostCapReached):
        await classify(session, company_id=refused_only)

    report = await costs.report(session, now=NOW)
    assert costs.TOP_COMPANIES == 5
    assert [(c.name, c.usd) for c in report.top_companies] == [
        (f"Namuna Savdo {n}", D(f"0.00{n}")) for n in (6, 5, 4, 3, 2)
    ]
    assert [c.company_id for c in report.top_companies] == spenders[:0:-1]
    # A company that only has a refusal row spent nothing: it is no top spender.
    every = await costs.report(session, now=NOW, top=10)
    assert refused_only not in [c.company_id for c in every.top_companies]
    assert len(every.top_companies) == 6


# --- ask never waits on its caller (04 §0: jobs claim rows FOR UPDATE) ------------


async def new_job(
    session: AsyncSession, kind: str = "verdict", lane: str = "instant"
) -> int:
    job = (
        await session.execute(
            sa.text(
                "INSERT INTO model_jobs (kind, lane, model, custom_id) "
                "VALUES (:k, :l, :m, :c) RETURNING id"
            ),
            {"k": kind, "l": lane, "m": BATCH, "c": f"cs-{uuid.uuid4()}"},
        )
    ).scalar_one()
    await session.commit()
    return job  # type: ignore[no-any-return]


async def committed_rows() -> list[dict[str, Any]]:
    """What another process sees right now."""
    async with SessionLocal() as other:
        return await rows(other)


async def searxng_elsewhere() -> str:
    """An unrelated ask in another session: it needs the ledger lock too."""
    async with SessionLocal() as other:
        return await free(other, "serp_searxng")  # type: ignore[no-any-return]


async def test_rows_the_caller_holds_for_update_never_stall_ask(session):
    company = await new_company(session, "Namuna Mebel Import")
    job = await new_job(session)
    run = (
        await session.execute(
            sa.text(
                "INSERT INTO collector_runs (source_key) "
                "VALUES ('olx_business') RETURNING id"
            )
        )
    ).scalar_one()
    await session.commit()
    held = {
        "model_jobs": ("id", job),
        "companies": ("id", company),
        "collector_runs": ("id", run),
        "sources": ("key", "olx_business"),
    }
    async with SessionLocal() as caller:
        # The job claims its rows, as every job does (04 §0).
        for table, (key, value) in held.items():
            claimed = await caller.scalar(
                sa.text(f"SELECT 1 FROM {table} WHERE {key} = :v FOR UPDATE SKIP LOCKED"),
                {"v": value},
            )
            assert claimed == 1, table
        verdict = asyncio.create_task(
            costs.ask(
                caller,
                operation="verdict",
                est_usd=D("0.030"),
                call=Calls(),
                price=lambda _: Priced(D("0.011"), model=VERDICT, input_tokens=2500),
                now=NOW,
                source_key="olx_business",
                company_id=company,
                model_job_id=job,
                collector_run_id=run,
            )
        )
        bystander = asyncio.create_task(searxng_elsewhere())
        done, _ = await asyncio.wait({verdict, bystander}, timeout=5)
        seen_before_commit = await committed_rows()
        await caller.commit()  # lets a stalled ask go, so a red run ends
        await asyncio.wait({verdict, bystander}, timeout=10)
        assert done == {verdict, bystander}
        assert verdict.result() == "ok" and bystander.result() == "ok"
    # The money was committed by ask's own transaction, not the caller's.
    assert sorted(
        (r["operation"], r["cost_usd"], r["model_job_id"]) for r in seen_before_commit
    ) == [("serp_searxng", D(0), None), ("verdict", D("0.011"), None)]
    [first] = [r for r in await committed_rows() if r["operation"] == "verdict"]
    assert (first["source_key"], first["company_id"], first["model_job_id"]) == (
        "olx_business",
        company,
        job,
    )
    assert first["collector_run_id"] == run


async def test_the_night_batch_returns_while_its_jobs_are_claimed(session):
    for _ in range(3):
        await new_job(session, "company_verdict", "batch")
    async with SessionLocal() as caller:
        jobs = (
            (
                await caller.execute(
                    sa.text(
                        "SELECT id FROM model_jobs WHERE status = 'pending' "
                        "AND kind = 'company_verdict' ORDER BY id LIMIT 100 "
                        "FOR UPDATE SKIP LOCKED"
                    )
                )
            )
            .scalars()
            .all()
        )
        submit = asyncio.create_task(
            costs.ask(
                caller,
                operation="company_verdict",
                est_usd=D("0.045"),
                call=Calls("msgbatch_1"),
                price=lambda _: [
                    Priced(D("0.015"), model=BATCH, batch=True, model_job_id=j)
                    for j in jobs
                ],
                now=NOW.replace(hour=23, minute=30),
            )
        )
        done, _ = await asyncio.wait({submit}, timeout=5)
        seen_before_commit = await committed_rows()
        await caller.commit()
        await asyncio.wait({submit}, timeout=10)
        assert done == {submit} and submit.result() == "msgbatch_1"
    assert [r["cost_usd"] for r in seen_before_commit] == [D("0.015")] * 3
    assert [r["model_job_id"] for r in await committed_rows()] == list(jobs)


async def test_a_lock_nobody_foresaw_costs_seconds_not_every_ask(session, monkeypatch):
    # Should a referenced row ever be written without its NOWAIT probe, the
    # own transaction gives up after OWN_LOCK_TIMEOUT and the caller's
    # savepoint takes the row: slow, never a hang of every ask.
    async def unprobed(_: AsyncSession, wanted: Any) -> dict[str, Any]:
        return dict(wanted)

    monkeypatch.setattr(costs, "_key_shared", unprobed)
    job = await new_job(session)
    async with SessionLocal() as caller:
        await caller.execute(
            sa.text("SELECT 1 FROM model_jobs WHERE id = :i FOR UPDATE"), {"i": job}
        )
        verdict = asyncio.create_task(
            costs.ask(
                caller,
                operation="verdict",
                est_usd=D("0.011"),
                call=Calls(),
                price=lambda _: Priced(D("0.011"), model=VERDICT),
                now=NOW,
                model_job_id=job,
            )
        )
        done, _ = await asyncio.wait({verdict}, timeout=8)
        await caller.commit()
        await asyncio.wait({verdict}, timeout=10)
        assert done == {verdict} and verdict.result() == "ok"
    [row] = await committed_rows()
    assert (row["cost_usd"], row["model_job_id"]) == (D("0.011"), job)


async def test_a_claim_in_the_callers_savepoint_is_settled_there(session, monkeypatch):
    entries = 0

    @asynccontextmanager
    async def once_unavailable() -> AsyncIterator[AsyncSession]:
        nonlocal entries
        entries += 1
        if entries == 1:  # the claim's attempt only
            raise sa.exc.OperationalError("connect", {}, Exception("pool timeout"))
        async with session_scope() as s:
            yield s

    monkeypatch.setattr(costs, "_own_session", once_unavailable)
    await costs.ask(
        session,
        operation="verdict",
        est_usd=D("0.030"),
        call=Calls(),
        price=lambda _: Priced(D("0.011"), model=VERDICT, input_tokens=2500),
        now=NOW,
    )
    await session.commit()
    [row] = await committed_rows()
    assert (row["model"], row["cost_usd"], row["input_tokens"]) == (
        VERDICT,
        D("0.011"),
        2500,
    )


async def test_a_unit_claimed_in_the_callers_savepoint_is_given_back_there(
    session, monkeypatch
):
    entries = 0

    @asynccontextmanager
    async def once_unavailable() -> AsyncIterator[AsyncSession]:
        nonlocal entries
        entries += 1
        if entries == 1:
            raise sa.exc.OperationalError("connect", {}, Exception("pool timeout"))
        async with session_scope() as s:
            yield s

    monkeypatch.setattr(costs, "_own_session", once_unavailable)
    with pytest.raises(ConnectionResetError):
        await brave(session, calls=Calls(raises=ConnectionResetError()))
    await session.commit()
    [row] = await committed_rows()
    assert (row["operation"], row["cost_usd"], row["units"]) == (
        "serp_brave",
        D("0.005"),
        D(0),
    )


# --- the money rungs and the counters cannot be talked past -------------------------


async def test_a_paid_call_estimated_at_zero_never_passes_a_cap(session):
    await seed(session, "12.00")
    for operation, real in [
        ("classify", "0.05"),
        ("verdict", "0.20"),
        ("serp_brave", "0.005"),
        ("apify_olx", "0.10"),
    ]:
        calls = Calls()
        with pytest.raises(CostCapReached) as refused:
            await costs.ask(
                session,
                operation=operation,  # type: ignore[arg-type]
                est_usd=D(0),
                call=calls,
                price=lambda _, r=real: Priced(D(r), units=D(1)),  # type: ignore[misc]
                now=NOW,
            )
        assert (refused.value.code, calls.n) == ("unpriced", 0), operation
    day = await costs.spend(session, since=costs.day_start(NOW))
    assert (day.usd, day.calls, day.refused, day.unpriced) == (D("12.00"), 1, 4, 4)


async def test_the_count_caps_do_not_wait_for_the_price_callback(session):
    # A natural callback prices the answer and leaves Priced.units at its
    # default 0: the claim's one unit stays, so the counters still count.
    for i in range(40):
        await costs.ask(
            session,
            operation="serp_ddg",
            est_usd=D(0),
            call=Calls(),
            price=lambda _: Priced(D(0)),
            now=NOW.replace(second=i % 60),
        )
    with pytest.raises(CostCapReached) as refused:
        await free(session, "serp_ddg")
    assert refused.value.code == "cost_cap_day_op:serp_ddg"
    one = usage.unit_cost("serp_brave", 1)
    for _ in range(3):
        await costs.ask(
            session,
            operation="serp_brave",
            est_usd=one,
            call=Calls(),
            price=lambda _: Priced(one, model="web"),
            now=NOW,
        )
    assert await costs.month_units(session, operation="serp_brave", now=NOW) == D(3)


async def test_places_details_count_their_free_tier_in_ask(session):
    await seed(session, "0", operation="places_details_contact", units="998")

    async def details(est: str) -> Any:
        return await costs.ask(
            session,
            operation="places_details_contact",
            est_usd=D(est),
            call=Calls(),
            price=lambda _: Priced(D(est), model="places.details"),  # units forgotten
            now=NOW,
        )

    await details("0")  # the 999th
    await details("0")  # the 1,000th: the last free one
    units = await costs.month_units(session, operation="places_details_contact", now=NOW)
    assert units == D(1000)
    with pytest.raises(CostCapReached) as refused:
        await details("0")
    assert refused.value.code == "unpriced"
    costs.clear_refusals()
    assert await details("0.020") == "ok"


@pytest.mark.parametrize("cost", [None, D("NaN"), D("Infinity"), D("-0.01")])
async def test_an_answer_that_cannot_be_priced_keeps_the_estimate(session, cost):
    await costs.ask(
        session,
        operation="verdict",
        est_usd=D("0.030"),
        call=Calls(),
        price=lambda _: Priced(cost, model=VERDICT, input_tokens=2500, output_tokens=600),
        now=NOW,
    )
    [row] = await rows(session)
    assert (row["cost_usd"], row["model"], row["input_tokens"]) == (
        D("0.030"),
        VERDICT,
        2500,
    )


async def test_a_batch_row_that_cannot_be_priced_takes_what_the_estimate_left(session):
    jobs = [await new_job(session, "company_verdict", "batch") for _ in range(3)]
    await costs.ask(
        session,
        operation="company_verdict",
        est_usd=D("0.045"),
        call=Calls("msgbatch_1"),
        price=lambda _: [
            Priced(D("0.015"), model=BATCH, batch=True, model_job_id=jobs[0]),
            Priced(None, model=BATCH, batch=True, model_job_id=jobs[1]),
            Priced(D("NaN"), model=BATCH, batch=True, model_job_id=jobs[2]),
        ],
        now=NOW,
    )
    stored = await rows(session)
    assert [(r["model_job_id"], r["cost_usd"]) for r in stored] == [
        (jobs[0], D("0.015")),
        (jobs[1], D("0.015")),
        (jobs[2], D("0.015")),
    ]


async def test_a_batch_result_that_cannot_be_priced_keeps_its_provisional_cost(session):
    job = await new_job(session, "company_verdict", "batch")
    await costs.ask(
        session,
        operation="company_verdict",
        est_usd=D("0.015"),
        call=Calls("msgbatch_1"),
        price=lambda _: [Priced(D("0.015"), model=BATCH, batch=True, model_job_id=job)],
        now=NOW,
    )
    unpriced = Priced(None, model=BATCH, batch=True, input_tokens=4000, output_tokens=600)
    assert await costs.settle_provisional(
        session, model_job_id=job, priced=unpriced, now=NOW + timedelta(hours=8)
    )
    [row] = await rows(session)
    assert (row["cost_usd"], row["input_tokens"]) == (D("0.015"), 4000)


async def test_the_monthly_cap_tells_the_owner_the_monthly_cost_cap(session):
    owner = Owner()
    costs.set_cap_notifier(owner)
    await seed(session, "250.00", at=datetime(2026, 10, 2, 9, 0, tzinfo=TZ))
    with pytest.raises(CostCapReached) as refused:
        await classify(session)
    assert refused.value.code == "cost_cap_monthly"
    assert owner.told == [
        "💸 Oylik xarajat chegarasi ($250) — oy oxirigacha pullik chaqiruvlar "
        "toʻxtadi; kollektorlar bepul qismini davom ettiradi"
    ]
    assert await alert_rows(session) == [("alert:cost_cap", "cost_cap", NOW)]


async def test_the_yandex_window_is_read_on_the_tashkent_clock(session):
    # 23:30 UTC is 04:30 in Tashkent: inside the night window, 23:30 is not.
    assert (
        await free(
            session, "serp_yandex_night", now=datetime(2026, 10, 22, 23, 30, tzinfo=UTC)
        )
        == "ok"
    )
    # 06:00 UTC is 11:00 in Tashkent: outside it, though 06:00 is inside.
    with pytest.raises(CostCapReached) as refused:
        await free(
            session, "serp_yandex_night", now=datetime(2026, 10, 23, 6, 0, tzinfo=UTC)
        )
    assert refused.value.code == "op_window:serp_yandex_night"


async def test_a_broken_health_probe_does_not_stop_the_classifier(session):
    async def broken(_: AsyncSession, __: datetime) -> datetime | None:
        raise RuntimeError("health probe")

    costs.set_anthropic_health(broken)
    await classify(session)
    assert [r["model"] for r in await rows(session)] == [EXTRACT]


async def test_spend_narrows_by_family_company_and_source(session):
    company = await new_company(session, "Namuna Oyna Savdo")
    await seed(session, "0.40", operation="apify_olx", source_key="olx_business")
    await classify(session, "0.001", company_id=company)
    await seed(session, "0.005", operation="serp_brave")
    since = costs.day_start(NOW)
    assert (await costs.spend(session, since=since)).usd == D("0.405114")
    assert (await costs.spend(session, since=since, family="apify")).usd == D("0.40")
    assert (await costs.spend(session, since=since, family="anthropic")).usd == D(
        "0.000114"
    )
    assert (await costs.spend(session, since=since, company_id=company)).usd == D(
        "0.000114"
    )
    assert (await costs.spend(session, since=since, source_key="olx_business")).usd == D(
        "0.40"
    )


async def test_attribution_to_rows_nobody_holds_survives_the_callers_rollback(session):
    # The per-source budget (03 §1.2) and the per-company enrichment cap read
    # this attribution: when nothing holds the referenced rows, ask's own
    # transaction writes it, so a caller that later fails cannot erase it.
    company = await new_company(session, "Namuna Plitka Savdo")
    await costs.ask(
        session,
        operation="apify_olx",
        est_usd=D("0.00199"),
        call=Calls(),
        price=lambda _: Priced(D("0.00199"), units=D(1), model="actor"),
        now=NOW,
        source_key="olx_business",
        company_id=company,
    )
    await session.rollback()
    [row] = await committed_rows()
    assert (row["source_key"], row["company_id"]) == ("olx_business", company)
    left = await costs.source_budget_usd(session, source_key="olx_business", now=NOW)
    assert left == D("0.60") - D("0.00199")
