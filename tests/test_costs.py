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
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import costs, usage
from clientscrapper.services.costs import CostCapReached, Ledger, OpTotals, decide
from clientscrapper.services.usage import Priced

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 23, 14, 23, tzinfo=TZ)
NOON = time(12, 0)
EXTRACT = usage.DEFAULT_MODELS["EXTRACT_MODEL"]
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
    # Each step removes the breach the previous step reported; the next rung
    # must then be the one that answers.
    ladder = [
        (
            ledger(month="249.995", classify="0.30", verdict="2.25", other="5.00"),
            "cost_cap_monthly",
        ),
        (ledger(classify="0.30", verdict="2.25", other="7.50"), "cost_cap_daily"),
        (
            ledger(
                classify="0.30",
                verdict="2.25",
                draft="0.20",
                company_verdict="1.50",
                enrich_search="0.75",
            ),
            "cost_cap_family:anthropic",
        ),
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
    # 0.165 = 33 × $0.005, and reaching an allocation is over it: the 32nd
    # query passes, the 33rd (spend 0.160 + 0.005 = 0.165) is refused.
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


def alerts(day: str = "0", month: str = "0", **families: str) -> dict[str, str]:
    snap = costs.SpendSnapshot(
        day_usd=D(day),
        month_usd=max(D(month), D(day)),
        family_day_usd={f: D(v) for f, v in families.items()},  # type: ignore[misc]
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
    assert (first["operation"], first["cost_usd"], first["units"], first["model"]) == (
        "classify",
        D("0.004"),
        D(0),
        None,
    )
    # Brave's query was reserved as one unit before the call and given back.
    assert (second["operation"], second["cost_usd"], second["units"]) == (
        "serp_brave",
        D("0.005"),
        D(0),
    )


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


async def test_two_concurrent_calls_near_the_cap_cannot_both_pass(session):
    await seed(session, "9.98")

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
    async def down(_: AsyncSession, __: datetime) -> bool:
        return True

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


async def test_the_report_has_what_xarajat_prints(session):
    company = (
        await session.execute(
            sa.text(
                "INSERT INTO companies (display_name, name_key) "
                "VALUES ('Namuna Tekstil Savdo', 'namuna tekstil savdo') RETURNING id"
            )
        )
    ).scalar_one()
    await session.commit()
    await classify(session, "0.001", company_id=company)
    await brave(session)
    await seed(session, "1.00", operation="apify_olx", at=NOW - timedelta(days=3))
    await seed(session, "10.00")
    with pytest.raises(CostCapReached):
        await classify(session, now=NOW.replace(hour=17))

    report = await costs.report(session, now=NOW)
    assert report.day["classify"] == costs.OpLine(
        calls=1, units=D(0), usd=D("0.000114"), refused=1
    )
    assert report.day["serp_brave"] == costs.OpLine(calls=1, units=D(1), usd=D("0.005"))
    assert "apify_olx" not in report.day
    assert report.month["apify_olx"].usd == D("1.00")
    assert report.total(report.day, "anthropic") == D("0.000114")
    assert report.total(report.day) == D("10.005114")
    assert report.total(report.month) == D("11.005114")
    assert report.refusals(report.day) == 1
    assert report.top_companies == ((company, D("0.000114")),)
    assert (report.daily_cap, report.monthly_cap) == (CAP, MONTH_CAP)
