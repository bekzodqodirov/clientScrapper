"""exhibitors against the live sites (07 §1.12, 03 §1.5): ``make smoke`` from
the VPS, ``LIVE=1`` only. Each test makes one real request through the
Fetcher (robots.txt, the UA, the host's pace) and asserts the fixture-era
parser still reads rows; the recorder writes its outcome into
``collector_runs`` for the digest. No database is touched."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from clientscrapper.collectors.base import Budget
from clientscrapper.collectors.exhibitors import Edition, Exhibitors
from clientscrapper.config import settings
from clientscrapper.services.http import Fetcher

pytestmark = pytest.mark.live(source="exhibitors")

# UzBuild 2026 (exhibition 461) and the Automechanika 2026 catalogue (7012),
# as recorded on 2026-10-29 (tests/fixtures/exhibitors/README.md).
ERA = "https://uzbuild.uz/ERAForms/companies_list.php?l=en&exhibition=461"
CATALOGUE = "https://catalog.automechanika.uz/en/expositions/exposition/7012"


def budget() -> Budget:
    now = datetime.now(settings.tz)
    return Budget(pages=2, usd=Decimal("0"), deadline=now + timedelta(minutes=5))


async def test_the_era_endpoint_still_answers_datatables_rows():
    fetcher = Fetcher()
    try:
        answer = await fetcher.get(
            ERA,
            source="exhibitors",
            budget=budget(),
            params={"draw": 1, "start": 0, "length": 10},
        )
    finally:
        await fetcher.aclose()
    assert answer.status == 200
    page = Exhibitors.parse_era_rows(
        answer.body,
        url=answer.url,
        fetched_at=answer.fetched_at,
        show="uzbuild",
        edition=Edition("461", 2026),
    )
    assert page.seen >= 1 and (page.total or 0) >= 1


async def test_the_expodat_catalogue_still_prints_rows():
    fetcher = Fetcher()
    try:
        answer = await fetcher.get(CATALOGUE, source="exhibitors", budget=budget())
    finally:
        await fetcher.aclose()
    assert answer.status == 200
    page = Exhibitors.parse_expodat_page(
        answer.body,
        url=answer.final_url,
        fetched_at=answer.fetched_at,
        show="automechanika",
        edition=Edition("7012", 2026, url=CATALOGUE),
    )
    assert not page.removed and page.seen >= 1
