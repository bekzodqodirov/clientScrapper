"""Prices of every paid call (04 §4.1 / §8, 03 §1.3). Pure: no database.

The golden figures are the per-call estimates of 04 §4.1's table, computed
from the token counts written there; a price change makes them red on
purpose.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from clientscrapper import config
from clientscrapper.config import Settings, settings
from clientscrapper.services import usage

ROOT = Path(__file__).resolve().parents[1]
EXTRACT = usage.DEFAULT_MODELS["EXTRACT_MODEL"]
VERDICT = usage.DEFAULT_MODELS["VERDICT_MODEL"]
BATCH = usage.DEFAULT_MODELS["BATCH_MODEL"]


def test_the_price_table_is_the_specs():
    assert {
        EXTRACT: usage.ModelPrice(D("0.10"), D("0.50"), D("0.01"), D("0.125")),
        VERDICT: usage.ModelPrice(D("2"), D("10"), D("0.20"), D("2.50")),
        BATCH: usage.ModelPrice(D("4"), D("20"), D("0.20"), D("5.00")),
    } == usage.MODEL_PRICES


def test_the_shipped_model_ids_are_the_settings_defaults_and_are_priced():
    fields = Settings.model_fields
    assert fields["extract_model"].default == EXTRACT
    assert fields["verdict_model"].default == VERDICT
    assert fields["batch_model"].default == BATCH
    assert all(usage.resolve_price(m) is not None for m in usage.DEFAULT_MODELS.values())


def test_parse_price_has_one_home():
    assert config.parse_price is usage.parse_price


@pytest.mark.parametrize(
    ("model", "tokens", "expected"),
    [
        # classify: 0.9k cached system + 0.3k in + 0.15k out ≈ $0.00012
        (
            EXTRACT,
            {"cache_read_tokens": 900, "input_tokens": 300, "output_tokens": 150},
            "0.000114",
        ),
        # verdict: 1.5k cached + 2.5k in + 0.6k out ≈ $0.011
        (
            VERDICT,
            {"cache_read_tokens": 1500, "input_tokens": 2500, "output_tokens": 600},
            "0.011300",
        ),
        # company_verdict on the batch lane (−50 %): 4k in + 0.7k out ≈ $0.015
        (BATCH, {"input_tokens": 4000, "output_tokens": 700, "batch": True}, "0.015000"),
        # enrich_search: 3 searches × $0.01 + 6k in + 0.3k out ≈ $0.031
        (
            EXTRACT,
            {"input_tokens": 6000, "output_tokens": 300, "web_searches": 3},
            "0.030750",
        ),
        # cache writes at the table's own price
        (EXTRACT, {"cache_write_tokens": 1000}, "0.000125"),
    ],
)
def test_token_costs_reproduce_the_specs_estimates(model, tokens, expected):
    assert usage.anthropic_cost_usd(model, **tokens) == D(expected)


def test_a_cost_is_rounded_up_never_away():
    # 1 input token on the extract model is $0.0000001: the column keeps 6 places.
    assert usage.anthropic_cost_usd(EXTRACT, input_tokens=1) == D("0.000001")
    assert usage.round_usd(D("0.0000010001")) == D("0.000002")


def test_the_batch_discount_halves_tokens_but_not_searches():
    assert usage.anthropic_cost_usd(
        BATCH, input_tokens=4000, output_tokens=700, batch=True, web_searches=1
    ) == D("0.025000")


def test_an_unknown_model_is_unpriced():
    assert usage.price_for("model-nobody-priced") is None
    assert usage.anthropic_cost_usd("model-nobody-priced", input_tokens=100) is None


def test_a_role_override_wins_for_that_roles_model_only(monkeypatch):
    monkeypatch.setattr(settings, "extract_model_price", "1/5")
    assert usage.price_for(settings.extract_model) == usage.ModelPrice(D("1"), D("5"))
    # Without cache prices in the override the ratios apply: read 0.1×, write 1.25×.
    assert usage.anthropic_cost_usd(
        settings.extract_model, cache_read_tokens=1000, cache_write_tokens=1000
    ) == D("0.001350")
    assert usage.price_for(settings.verdict_model) == usage.MODEL_PRICES[VERDICT]


def test_an_override_prices_an_id_the_table_does_not_know(monkeypatch):
    monkeypatch.setattr(settings, "batch_model", "model-nobody-priced")
    monkeypatch.setattr(settings, "batch_model_price", "3/15/0.30/3.75")
    assert usage.price_for("model-nobody-priced") == usage.ModelPrice(
        D("3"), D("15"), D("0.30"), D("3.75")
    )


@pytest.mark.parametrize(
    ("operation", "units", "kwargs", "expected"),
    [
        ("serp_brave", 1, {}, "0.005"),
        ("serp_brave", 33, {}, "0.165"),
        ("serp_yandex_night", 1, {}, "0"),
        ("serp_searxng", 5, {}, "0"),
        ("serp_ddg", 5, {}, "0"),
        ("places_text_ids", 1, {}, "0"),
        ("twogis_api", 1, {}, "0"),
        # Places Details: the first 1,000 a month are free, then $0.020 each.
        ("places_details_contact", 1, {"month_units_before": 0}, "0"),
        ("places_details_contact", 2, {"month_units_before": 999}, "0.020"),
        ("places_details_contact", 1, {"month_units_before": 1000}, "0.020"),
        ("places_details_contact", 3, {"month_units_before": 1500}, "0.060"),
        ("apify_olx", 100, {}, "0.199"),
        ("apify_2gis", 1, {}, "0.00199"),
        ("apify_instagram", 10, {}, "0.023"),
        ("apify_uzum", 3, {}, "0.0099"),
        # proxy: MB × (sources.config.proxy_usd_per_gb / 1,000)
        ("proxy_uzum", 500, {"proxy_usd_per_gb": D("5")}, "2.5"),
        ("proxy_uzum", 1, {"proxy_usd_per_gb": D("3")}, "0.003"),
    ],
)
def test_unit_costs_follow_the_price_table(operation, units, kwargs, expected):
    assert usage.unit_cost(operation, units, **kwargs) == D(expected)


def test_a_proxy_without_a_configured_price_is_unpriced():
    assert usage.unit_cost("proxy_uzum", 10) is None
    assert usage.unit_cost("proxy_uzum", 10, proxy_usd_per_gb=D("NaN")) is None
    assert usage.unit_cost("proxy_uzum", 10, proxy_usd_per_gb=D("-1")) is None


def test_token_ops_reserves_and_negative_units_are_not_unit_priced():
    for operation in ("classify", "verdict", "model_reserve", "serp_reserve", "other"):
        with pytest.raises(ValueError):
            usage.unit_cost(operation, 1)
    with pytest.raises(ValueError):
        usage.unit_cost("serp_brave", -1)


def test_priced_anthropic_reads_the_sdk_usage_shape():
    sdk_usage = SimpleNamespace(
        input_tokens=6000,
        output_tokens=300,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=3),
    )
    priced = usage.priced_anthropic(EXTRACT, sdk_usage, model_job_id=7)
    assert priced == usage.Priced(
        cost_usd=D("0.030750"),
        units=D(3),
        model=EXTRACT,
        input_tokens=6000,
        output_tokens=300,
        cache_read_tokens=0,
        cache_write_tokens=0,
        batch=False,
        model_job_id=7,
    )


def test_priced_anthropic_treats_missing_counts_as_zero_and_unknown_models_as_unpriced():
    priced = usage.priced_anthropic(
        "model-nobody-priced", SimpleNamespace(input_tokens=10)
    )
    assert priced.cost_usd is None
    assert (priced.input_tokens, priced.output_tokens, priced.units) == (10, 0, D(0))


def test_the_pricing_module_loads_without_the_settings():
    # The doctor diagnoses a .env the settings refuse to load, and it reads the
    # price table and the allocations from here: neither may pull them in.
    code = (
        "import sys, clientscrapper.tools.doctor, clientscrapper.services.usage; "
        "print(sorted(m for m in ('clientscrapper.config', 'clientscrapper.db') "
        "if m in sys.modules))"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_only_the_free_ops_and_the_places_free_tier_are_free_by_definition():
    paid = sorted(usage.CALLABLE - usage.FREE_OPERATIONS - {"places_details_contact"})
    assert [op for op in paid if usage.free_call(op, month_units_before=0)] == []
    assert all(
        usage.free_call(op, month_units_before=10**6) for op in usage.FREE_OPERATIONS
    )
    # Places Details: the first 1,000 a calendar month are free (03 §1.3).
    assert usage.free_call("places_details_contact", month_units_before=999)
    assert not usage.free_call("places_details_contact", month_units_before=1000)
    assert usage.unit_cost("places_details_contact", 1, month_units_before=999) == 0
    assert usage.unit_cost("places_details_contact", 1, month_units_before=1000) == (
        D("0.020")
    )


def test_every_counted_cap_and_the_places_tier_are_counted_per_call():
    counted = set(usage.COUNTED_OPERATIONS)
    assert counted == {
        "serp_brave",
        "serp_yandex_night",
        "serp_searxng",
        "serp_ddg",
        "places_text_ids",
        "places_details_contact",
        "twogis_api",
    }
    assert set(usage.DAILY_UNIT_CAPS) | set(usage.MONTHLY_UNIT_CAPS) <= (
        usage.COUNTED_OPERATIONS
    )


def test_the_settings_strip_a_model_id_as_the_doctor_reads_it():
    loaded = Settings(
        _env_file=None,  # type: ignore[call-arg]
        extract_model=f" {EXTRACT} \t",
        verdict_model=f"{VERDICT} ",
        batch_model=f"\t{BATCH}",
    )
    assert (loaded.extract_model, loaded.verdict_model, loaded.batch_model) == (
        EXTRACT,
        VERDICT,
        BATCH,
    )
