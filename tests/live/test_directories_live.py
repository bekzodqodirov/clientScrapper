"""``make smoke``: the directories' selectors against the live sites (03 §1.5,
§2.6; 07 §1.12). Skipped unless ``LIVE=1`` (the VPS); each test makes one real
request through ``services/http.Fetcher`` (robots.txt, the per-host pace, the
breaker) and asserts the recorded-era selector still yields a row. A skip is
reported as a skip, never as green.

The goldenpages card test is the day-1 check of 03 §2.6 «phones … server-side?»
on the VPS: the JSON-LD answer must still carry a number.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from clientscrapper.collectors.base import Budget
from clientscrapper.collectors.directories import glotr, goldenpages, topuz, yellowpages
from clientscrapper.config import settings
from clientscrapper.services.http import Fetcher, FetchResult
from tests.live.conftest import HOSTS

pytestmark = pytest.mark.live(source="directories")


async def fetch(url: str, *, source: str) -> FetchResult:
    fetcher = Fetcher(hosts=HOSTS)
    budget = Budget(
        pages=1,
        usd=Decimal("0"),
        deadline=datetime.now(settings.tz) + timedelta(minutes=5),
    )
    try:
        return await fetcher.get(url, source=source, budget=budget)
    finally:
        await fetcher.aclose()


async def test_a_goldenpages_rubric_still_lists_companies():
    result = await fetch(goldenpages.SEEDS[0], source=goldenpages.SOURCE_KEY)
    assert result.status == 200
    listed = goldenpages.parse_list(
        result.body, url=result.final_url, fetched_at=result.fetched_at
    )
    assert listed.entries and listed.next_url is not None


async def test_a_goldenpages_card_still_carries_its_phones_in_json_ld():
    url = "https://www.goldenpages.uz/company/?Id=85641"
    result = await fetch(url, source=goldenpages.SOURCE_KEY)
    assert result.status == 200
    [obs] = goldenpages.parse_company(
        result.body, url=result.final_url, fetched_at=result.fetched_at
    )
    assert obs.phones


async def test_a_yellowpages_rubrika_still_carries_its_payload_rows():
    result = await fetch(yellowpages.SEEDS[0], source=yellowpages.SOURCE_KEY)
    assert result.status == 200
    listed = yellowpages.parse_list(
        result.body, url=result.final_url, fetched_at=result.fetched_at
    )
    assert listed.entries and all(e.observation for e in listed.entries)


async def test_a_top_uz_section_still_carries_its_payload_rows():
    result = await fetch(topuz.SEEDS[1], source=topuz.SOURCE_KEY)
    assert result.status == 200
    listed = topuz.parse_list(
        result.body, url=result.final_url, fetched_at=result.fetched_at
    )
    assert listed.entries and listed.next_url is not None


async def test_a_glotr_category_still_names_its_sellers():
    result = await fetch(glotr.SEEDS[0], source=glotr.SOURCE_KEY)
    assert result.status == 200
    listed = glotr.parse_list(
        result.body, url=result.final_url, fetched_at=result.fetched_at
    )
    assert listed.entries
