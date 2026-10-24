"""The 23 seeded ``sources`` rows against the spec (02 §2.1, 03 §1.3, 03 §3).

03 §3 says the schedule table's cron, rps, pages and ``$/day`` columns are
seeded into the ``sources`` row of each collector, so the table is written out
here by hand as the oracle, and the migration's frozen copy and the seeded rows
must each equal it — a value changed in the migration alone is a difference
from the spec. 03 §1.3's invariant is held too: the collector caps add up to
at most $5/day, which with Claude's $5 stays inside the binding $10/day.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from clientscrapper.config import Settings
from clientscrapper.services.usage import FAMILY_CAPS

ROOT = Path(__file__).resolve().parents[1]

# key → (cron, rps, daily_page_budget, daily_cost_cap_usd). A row with several
# cadences seeds its first one; the other cadences live with the job.
SCHEDULE: dict[str, tuple[str | None, str, int, str]] = {
    # 03 §3, phase 1 and 2 rows. «—» rps is the column default 0.3 (rps > 0),
    # «—» pages 0, «Claude only» and «inside serp» $0, «per engine» rps 0.3,
    # «2.0 / 1.0» the first.
    "erp_sync": ("15 * * * *", "1.0", 50, "0"),
    "telegram_monitor": ("*/15 * * * *", "0.3", 0, "0"),
    "serp_layer": ("0 10,14 * * *", "0.3", 300, "1.00"),
    "instagram_discovery": ("0 11 * * *", "1.0", 100, "0.40"),
    "olx_business": ("0 7 * * *", "0.33", 400, "0.60"),
    "directories": ("0 23 * * 4", "0.5", 600, "0"),
    "hh_vacancies": ("20 */4 * * *", "1.0", 400, "0"),
    "exhibitors": ("0 9 * * 1", "0.33", 100, "0"),
    "maps": ("0 10 * * 2", "2.0", 150, "1.40"),
    "brand_dealers": ("0 13 1-10 * *", "0.3", 120, "0"),
    "uzum_sellers": ("0 9 * * 5", "0.2", 300, "1.60"),
    # Not in 03 §3: the registry is called by enrichment (04 §3 E4, 1 req/3 s;
    # E2's 150 pages/day are its budget), manual entry and enrich_site are
    # event-driven (02 §2.1).
    "registry_lookup": (None, "0.33", 150, "0"),
    "manual": (None, "0.3", 0, "0"),
    "enrich_site": (None, "0.3", 150, "0"),
}
# Attribution-only sub-sources (02 §2.1): never scheduled, no budget.
ATTRIBUTION = (
    "directories_goldenpages",
    "directories_yellowpages",
    "directories_glotr",
    "directories_prom",
    "maps_2gis",
    "maps_google",
    "search_brave",
    "search_yandex",
    "search_searxng",
)
EXPECTED = {
    **SCHEDULE,
    **dict.fromkeys(ATTRIBUTION, (None, "0.3", 0, "0")),
}
COLLECTOR_CAP_USD = Decimal("5")  # 03 §1.3


def _migration_sources() -> dict[str, tuple[str | None, str, int, str]]:
    spec = importlib.util.spec_from_file_location(
        "migration_0001_sources", ROOT / "alembic" / "versions" / "0001_initial.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        key: (cron, rps, pages, usd)
        for key, _family, _title, _enabled, cron, rps, pages, usd in module._SOURCES
    }


def _normal(row: tuple[str | None, str, int, str]) -> tuple[object, ...]:
    cron, rps, pages, usd = row
    return (cron, Decimal(rps), pages, Decimal(usd))


def test_the_migration_seeds_the_spec_schedule():
    assert len(EXPECTED) == 23
    seeded = {k: _normal(v) for k, v in _migration_sources().items()}
    assert seeded == {k: _normal(v) for k, v in EXPECTED.items()}


def test_the_collector_caps_stay_inside_the_daily_budget():
    total = sum((Decimal(usd) for *_, usd in _migration_sources().values()), Decimal(0))
    assert total <= COLLECTOR_CAP_USD
    daily = Settings.model_fields["daily_cost_cap_usd"].default
    assert total + FAMILY_CAPS["anthropic"] <= daily == Decimal("10")


def test_tenders_is_not_a_source_yet():
    # 03 §3 schedules `tenders` as phase 2 but 02 §2.1 seeds 23 rows without
    # it: until a data migration adds the row, nothing may be filed under it.
    assert "tenders" not in _migration_sources()


async def test_the_seeded_rows_are_the_spec_schedule(session):
    rows = (
        await session.execute(
            sa.text(
                "SELECT key, cron, rps, daily_page_budget, daily_cost_cap_usd "
                "FROM sources"
            )
        )
    ).all()
    seeded = {
        r.key: (r.cron, r.rps, r.daily_page_budget, r.daily_cost_cap_usd) for r in rows
    }
    assert seeded == {k: _normal(v) for k, v in EXPECTED.items()}
    total = sum(r.daily_cost_cap_usd for r in rows)
    assert total <= COLLECTOR_CAP_USD


async def test_nothing_can_be_filed_under_tenders(session):
    with pytest.raises(sa.exc.IntegrityError, match="fk_evidence_source_key"):
        await session.execute(
            sa.text(
                "INSERT INTO evidence (source_key, url, fetched_at, sha256, bytes, "
                "excerpt, expires_at) VALUES ('tenders', 'https://uztender.example.uz/', "
                "now(), :sha, 1, 'namuna', now())"
            ),
            {"sha": "b" * 64},
        )
