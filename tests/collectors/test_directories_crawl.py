"""The directories run end to end over the recorded pages (03 §2.6; T-22):
the runner, the shared Fetcher with the sites' own robots.txt, the
evidence and observation rows, the heartbeat.

A fake web (``tests/fakes.Site``) answers each site's recorded pages; any
other URL is a 404, so a plan that asked for a page nobody recorded is
seen. Postgres-backed: ``sources`` is seeded configuration, so each test
snapshots the rows it switches and puts them back (07 §1.0).
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import datetime, timedelta
from itertools import pairwise
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa

from clientscrapper.collectors import directories
from clientscrapper.collectors.directories import Directories
from clientscrapper.collectors.directories import goldenpages as gp
from clientscrapper.collectors.runner import run_collector
from clientscrapper.db.models import CollectorRun, Evidence, Heartbeat, Source
from clientscrapper.db.models import Observation as ObservationRow
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import evidence as evidence_store
from tests.collectors.directories_pages import page, robots
from tests.fakes import FakeClock, Site, fetcher_for

TASHKENT = ZoneInfo("Asia/Tashkent")
THURSDAY = datetime(2026, 10, 29, 23, 0, tzinfo=TASHKENT)
HTML = {"content-type": "text/html; charset=utf-8"}
ROWS = (
    "directories",
    "directories_goldenpages",
    "directories_yellowpages",
    "directories_glotr",
    "directories_prom",
    "directories_topuz",
)
KEPT = ("enabled", "paused_until", "paused_reason", "daily_page_budget", "last_run_at")
TRADING_HOUSE = (
    "/company/international-business-and-travel-sp-ooo-uzbeksko-kitayskiy-"
    "torgovyy-dom-predstavitelstvo"
)


def html(name: str) -> httpx.Response:
    return httpx.Response(200, headers=HTML, content=page(name).body)


def missing() -> httpx.Response:
    return httpx.Response(404, headers=HTML, text="not found")


class Web:
    """The four directories as recorded. ``third`` is goldenpages'
    ``&Page=3`` answer (a redirect back to page 1, or page 1 in place)."""

    def __init__(self, clock: FakeClock, *, third: str = "redirect") -> None:
        self.third = third
        self.site = Site(clock)
        for host, name in (
            ("www.goldenpages.uz", "www.goldenpages.uz"),
            ("www.yellowpages.uz", "www.yellowpages.uz"),
            ("top.uz", "top.uz"),
            ("glotr.uz", "glotr.uz"),
        ):
            self.site.add(
                f"https://{host}/robots.txt", httpx.Response(200, text=robots(name))
            )
        self.site.add("https://www.goldenpages.uz/rubrics/", self.rubrics)
        self.site.add("https://www.goldenpages.uz/company/", self.company)
        yp = "https://www.yellowpages.uz"
        self.site.add(
            f"{yp}/rubrika/optovaya-torgovlya-kanctovarami-kancelyarskimi-tovarami/tashkent",
            html("yellowpages_rubrika_kanctovary_tashkent_p1"),
        )
        self.site.add(
            f"{yp}/teg/tovary-iz-kitaya", html("yellowpages_teg_tovary_iz_kitaya")
        )
        self.site.add(
            f"{yp}/kompaniya/alta-trade-ooo", html("yellowpages_kompaniya_alta_trade_ooo")
        )
        self.site.add("https://top.uz/section/stroymaterialy-optom", self.stroy)
        self.site.add("https://top.uz/section/optovaya-torgovlya", self.optovaya)
        self.site.add(
            f"https://top.uz{TRADING_HOUSE}",
            html("topuz_company_uzbek_chinese_trading_house"),
        )
        self.site.add("https://glotr.uz/oborudovanie/", html("glotr_oborudovanie_p1"))
        self.site.add("https://glotr.uz/showcase/jack/", html("glotr_showcase_jack"))

    @staticmethod
    def query(request: httpx.Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}

    def rubrics(self, request: httpx.Request) -> httpx.Response:
        q = self.query(request)
        if q == {"Id": "1424"}:
            return html("goldenpages_rubric_1424_p1")
        if q == {"Id": "1424", "Page": "2"}:
            return html("goldenpages_rubric_1424_p2")
        if q == {"Id": "1424", "Page": "3"}:
            if self.third == "redirect":
                return httpx.Response(302, headers={"location": "/rubrics/?Id=1424"})
            return html("goldenpages_rubric_1424_p1")
        if q == {"Id": "106080"}:
            return html("goldenpages_rubric_106080_p1")
        return missing()

    def company(self, request: httpx.Request) -> httpx.Response:
        if self.query(request) == {"Id": "85641"}:
            return html("goldenpages_company_85641")
        return missing()

    def stroy(self, request: httpx.Request) -> httpx.Response:
        return {
            None: html("topuz_stroymaterialy_optom_p1"),
            "2": html("topuz_stroymaterialy_optom_p2"),
        }.get(
            self.query(request).get("PAGEN_1"),
            html("topuz_stroymaterialy_optom_p99_negative"),
        )

    def optovaya(self, request: httpx.Request) -> httpx.Response:
        if not self.query(request):
            return html("topuz_optovaya_torgovlya_p1")
        return html("topuz_stroymaterialy_optom_p99_negative")

    def urls(self, host: str) -> list[str]:
        return [s.url for s in self.site.seen if s.request.url.host == host]


@pytest.fixture
async def rows(session):
    """Switch the parent and its sub-source rows on for the test; put back
    every column a test moves."""
    async with SessionLocal() as s:
        found = {
            r.key: r
            for r in await s.scalars(sa.select(Source).where(Source.key.in_(ROWS)))
        }
        snapshot = {key: {c: getattr(r, c) for c in KEPT} for key, r in found.items()}
        await s.execute(
            sa.update(Source)
            .where(Source.key.in_(ROWS))
            .values(enabled=True, paused_until=None)
        )
        await s.commit()
    yield
    async with SessionLocal() as s:
        for key, values in snapshot.items():
            await s.execute(sa.update(Source).where(Source.key == key).values(**values))
        await s.commit()


async def switch(key: str, **values: object) -> None:
    async with SessionLocal() as s:
        await s.execute(sa.update(Source).where(Source.key == key).values(**values))
        await s.commit()


async def run(web: Web, clock: FakeClock, tmp_path, variant: str | None = None):
    return await run_collector(
        Directories,
        fetcher=fetcher_for(web.site),
        now=clock.now(),
        variant=variant,
        evidence_root=tmp_path,
        clock=clock.now,
    )


async def observations(session) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for row in await session.scalars(
        sa.select(ObservationRow).order_by(ObservationRow.id)
    ):
        found.setdefault(row.source_key, []).append(row.parsed)
    return found


async def test_the_weekly_diff_runs_every_switched_on_site_and_names_the_others(
    session, rows, tmp_path
):
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    report = await run(web, clock, tmp_path)
    assert report.status == "ok", report
    meta = report.meta
    assert meta["mode"] == "diff"
    subs = meta["subsources"]
    assert {site: subs[site] for site in ("prom", "uzts", "openinfo", "uzpharm")} == {
        site: {"skipped": "not_built"} for site in ("prom", "uzts", "openinfo", "uzpharm")
    }
    gold = subs["goldenpages"]
    assert (gold["seeds"], gold["pages"], gold["cards"], gold["card_missing"]) == (
        7,
        2,
        1,
        27,
    )
    assert gold["list_status"] == {"404": 5}
    yellow = subs["yellowpages"]
    # 9 held stationery rows and the competitor need no card, nor a sole
    # trader's (its slug may be the person's name, the wave-5 seam review);
    # 5 cards were asked.
    assert (
        yellow["pages"],
        yellow["rows"],
        yellow["held_rows"],
        yellow["card_missing"],
    ) == (
        2,
        16,
        9,
        5,
    )
    # Pagination owed: the rubrika says 2 pages, the plan reads 1.
    assert yellow["pages_owed"] == 1
    glotr = subs["glotr"]
    assert (glotr["pages"], glotr["cards"], glotr["card_missing"], glotr["rows"]) == (
        1,
        1,
        53,
        0,
    )

    by_source = await observations(session)
    assert len(by_source["directories_goldenpages"]) == 28
    assert len(by_source["directories_yellowpages"]) == 16
    assert [o["source_ref"] for o in by_source["directories_glotr"]] == ["glotr:jack"]
    topuz = by_source["directories_topuz"]
    assert all(o["source_ref"].startswith("topuz:") for o in topuz)
    # The two sections share four companies: each is handled once.
    assert len({o["source_ref"] for o in topuz}) == len(topuz) == 36

    card = next(
        o for o in by_source["directories_goldenpages"] if o["url"].endswith("85641")
    )
    assert [p["e164"] for p in card["phones"]] == ["+998900000001", "+998900000002"]
    assert card["handles"]["goldenpages"] == "85641"
    evidence = await session.scalar(
        sa.select(Evidence).where(
            Evidence.url == "https://www.goldenpages.uz/company/?Id=85641"
        )
    )
    assert evidence is not None
    assert (evidence.source_key, evidence.source_ref) == (
        "directories_goldenpages",
        "goldenpages:85641",
    )
    house = next(o for o in topuz if o["source_ref"] == "topuz:6010")
    assert house["url"] == f"https://top.uz{TRADING_HOUSE}"

    beat = await session.get(Heartbeat, "collector:directories")
    assert beat is not None
    assert beat.detail["mode"] == "diff"
    assert beat.detail["subsources"] == {
        "goldenpages": "ran",
        "yellowpages": "ran",
        "topuz": "ran",
        "glotr": "ran",
        "prom": "not_built",
        "uzts": "not_built",
        "openinfo": "not_built",
        "uzpharm": "not_built",
    }
    assert beat.detail["status"] == "ok"


async def test_the_plan_never_follows_the_bot_trap_nor_asks_a_refused_page(
    session, rows, tmp_path
):
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    report = await run(web, clock, tmp_path)
    assert report.meta["skipped_robots"] == 0
    paths = [s.path for s in web.site.seen]
    assert not [p for p in paths if p.startswith("/gp/")]
    # glotr: page 1 of each seed, its sellers' showcases — never ?page=.
    for url in web.urls("glotr.uz"):
        assert "page=" not in url, url
    assert all(
        u == "https://glotr.uz/robots.txt"
        or u
        in {
            f"https://glotr.uz{p}"
            for p in (
                "/oborudovanie/",
                "/oborudovanie-v-tashkente/",
                "/mebel/",
                "/sanoat-uskunalari/",
            )
        }
        or u.startswith("https://glotr.uz/showcase/")
        for u in web.urls("glotr.uz")
    )
    # The diff reads page 1 only: no goldenpages Page=, no top.uz PAGEN_1=.
    assert not [u for u in web.urls("www.goldenpages.uz") if "Page=" in u]
    assert not [u for u in web.urls("top.uz") if "PAGEN_1" in u]
    assert not [u for u in web.urls("www.yellowpages.uz") if "page=" in u]
    # Every goldenpages request is robots.txt, a rubric seed or a card.
    for url in web.urls("www.goldenpages.uz"):
        path = url.removeprefix("https://www.goldenpages.uz")
        assert path == "/robots.txt" or path.startswith(
            ("/rubrics/?Id=", "/company/?Id=")
        ), url


async def test_a_paused_or_switched_off_sub_source_is_not_asked_at_all(
    session, rows, tmp_path
):
    """Carried from wave 4: the precision job pauses a weak source on its
    attribution row; the fan-out reads that row."""
    clock = FakeClock(THURSDAY)
    await switch(
        "directories_glotr",
        paused_until=THURSDAY + timedelta(days=7),
        paused_reason="low_precision",
    )
    await switch("directories_yellowpages", enabled=False)
    web = Web(clock)
    report = await run(web, clock, tmp_path)
    assert report.status == "ok"
    assert report.meta["subsources"]["glotr"] == {"skipped": "paused"}
    assert report.meta["subsources"]["yellowpages"] == {"skipped": "disabled"}
    assert web.urls("glotr.uz") == [] and web.urls("www.yellowpages.uz") == []
    assert web.urls("www.goldenpages.uz") and web.urls("top.uz")
    by_source = await observations(session)
    assert (
        "directories_glotr" not in by_source
        and "directories_yellowpages" not in by_source
    )
    beat = await session.get(Heartbeat, "collector:directories")
    assert beat is not None and beat.detail["subsources"]["glotr"] == "paused"


@pytest.mark.parametrize(
    ("third", "stop"), [("redirect", "redirected"), ("repeat", "repeat")]
)
async def test_the_full_crawl_follows_capital_p_and_stops_on_the_paginator(
    session, rows, tmp_path, third, stop
):
    """1 November, night 1: goldenpages alone, every page its pager links,
    until page 3 sends the plan back to page 1 (a redirect without Page, or
    page 1 served in place: its first card id repeats)."""
    clock = FakeClock(datetime(2026, 11, 1, 23, 0, tzinfo=TASHKENT))
    web = Web(clock, third=third)
    report = await run(web, clock, tmp_path, variant="full")
    assert report.meta["mode"] == "full" and report.meta["full_night"] == ["goldenpages"]
    assert list(report.meta["subsources"]) == ["goldenpages"]
    rubric_1424 = [
        u
        for u in web.urls("www.goldenpages.uz")
        if u.startswith("https://www.goldenpages.uz/rubrics/?Id=1424")
    ]
    assert rubric_1424 == [
        "https://www.goldenpages.uz/rubrics/?Id=1424",
        "https://www.goldenpages.uz/rubrics/?Id=1424&Page=2",
        "https://www.goldenpages.uz/rubrics/?Id=1424&Page=3",
        *(["https://www.goldenpages.uz/rubrics/?Id=1424"] if third == "redirect" else []),
    ]
    gold = report.meta["subsources"]["goldenpages"]
    assert gold["stops"] == {stop: 1, "last": 1}
    # Pages 1 and 2 of 1424 and page 1 of 106080: 48 companies, one card each.
    assert gold["pages"] == 3
    assert gold["cards"] + gold["card_missing"] == 48
    assert not web.urls("top.uz") and not web.urls("glotr.uz")


async def test_the_top_uz_full_crawl_takes_pagen_as_printed_until_the_empty_page(
    session, rows, tmp_path
):
    clock = FakeClock(datetime(2026, 11, 3, 23, 0, tzinfo=TASHKENT))
    web = Web(clock)
    report = await run(web, clock, tmp_path, variant="full")
    assert report.meta["full_night"] == ["topuz"]
    lists = [u for u in web.urls("top.uz") if "/section/" in u]
    assert lists == [
        "https://top.uz/section/optovaya-torgovlya",
        "https://top.uz/section/optovaya-torgovlya?PAGEN_1=2",
        "https://top.uz/section/stroymaterialy-optom",
        "https://top.uz/section/stroymaterialy-optom?PAGEN_1=2",
        "https://top.uz/section/stroymaterialy-optom?PAGEN_1=3",
    ]
    assert report.meta["subsources"]["topuz"]["stops"] == {"empty": 2}


@pytest.mark.parametrize("variant", [None, "full"])
async def test_the_thursday_of_the_1st_to_7th_runs_tonight_in_full_and_the_rest_by_diff(
    session, rows, tmp_path, variant
):
    """5 November is a Thursday and night 5 (prom, not built): the four built
    sites still get their weekly diff, whichever of the two jobs that fire at
    23:00 claims the run."""
    clock = FakeClock(datetime(2026, 11, 5, 23, 0, tzinfo=TASHKENT))
    report = await run(Web(clock), clock, tmp_path, variant=variant)
    assert report.status == "ok", report
    assert report.meta["full_night"] == ["prom"]
    subs = report.meta["subsources"]
    assert subs["prom"] == {"skipped": "not_built"}
    for site in ("goldenpages", "yellowpages", "topuz", "glotr"):
        assert subs[site]["mode"] == "diff" and subs[site]["seeds"], site
    assert report.counters["requests"] > 0


async def test_a_first_thursday_that_is_a_built_site_s_night_crawls_it_in_full(
    session, rows, tmp_path
):
    """1 October 2026 is a Thursday and night 1: goldenpages in full (every
    page its pager links), the other sites by their diff (page 1 alone)."""
    clock = FakeClock(datetime(2026, 10, 1, 23, 0, tzinfo=TASHKENT))
    web = Web(clock)
    report = await run(web, clock, tmp_path)
    subs = report.meta["subsources"]
    assert (subs["goldenpages"]["mode"], subs["topuz"]["mode"]) == ("full", "diff")
    assert "https://www.goldenpages.uz/rubrics/?Id=1424&Page=2" in web.urls(
        "www.goldenpages.uz"
    )
    assert not [u for u in web.urls("top.uz") if "PAGEN_1" in u]


async def test_a_second_diff_asks_no_card_for_a_company_already_observed(
    session, rows, tmp_path
):
    for key in ("directories_yellowpages", "directories_glotr"):
        await switch(key, enabled=False)
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    first = await run(web, clock, tmp_path)
    assert first.meta["subsources"]["goldenpages"]["known"] == 0
    asked = Counter(s.path for s in web.site.seen)
    second = await run(web, clock, tmp_path)
    gold = second.meta["subsources"]["goldenpages"]
    assert (gold["known"], gold["cards"], gold["card_missing"]) == (28, 0, 0)
    again = Counter(s.path for s in web.site.seen) - asked
    assert again["/company/"] == 0 and again["/rubrics/"] == 7
    assert second.counters["new_observations"] == 0


async def test_a_page_budget_stop_keeps_the_rows_of_the_cards_not_reached(
    session, rows, tmp_path
):
    for key in ("directories_yellowpages", "directories_glotr"):
        await switch(key, enabled=False)
    await switch("directories", daily_page_budget=4)
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    report = await run(web, clock, tmp_path)
    assert report.status == "partial"
    assert report.meta["exhausted"] == "pages"
    # Rubric page 1, then three cards (404s): the fourth card stops the run.
    assert [s.path for s in web.site.seen if s.path != "/robots.txt"] == [
        "/rubrics/",
        "/company/",
        "/company/",
        "/company/",
    ]
    by_source = await observations(session)
    rows_kept = by_source["directories_goldenpages"]
    assert len(rows_kept) == 20
    assert all(o["phones"] == [] for o in rows_kept)
    assert "directories_topuz" not in by_source  # top.uz was never reached
    run_row = await session.get(CollectorRun, report.run_id)
    assert run_row is not None and run_row.pages == 4


async def test_glotr_s_full_crawl_reads_page_one_and_never_meets_robots(
    session, rows, tmp_path
):
    """4 November, night 4: glotr alone. Its pager prints ?page=2, which
    robots.txt refuses; the plan never builds the request, so the Fetcher has
    nothing to refuse."""
    clock = FakeClock(datetime(2026, 11, 4, 23, 0, tzinfo=TASHKENT))
    web = Web(clock)
    report = await run(web, clock, tmp_path, variant="full")
    assert report.meta["full_night"] == ["glotr"]
    assert report.meta["skipped_robots"] == 0
    lists = [u for u in web.urls("glotr.uz") if "/showcase/" not in u]
    assert lists == [
        "https://glotr.uz/robots.txt",
        "https://glotr.uz/oborudovanie/",
        "https://glotr.uz/oborudovanie-v-tashkente/",
        "https://glotr.uz/mebel/",
        "https://glotr.uz/sanoat-uskunalari/",
    ]
    # Crawl-delay: 10 is honoured: no two glotr requests closer than 10 s.
    times = [s.at for s in web.site.seen if s.request.url.host == "glotr.uz"]
    assert all(b - a >= 10 for a, b in pairwise(times[1:]))


async def test_an_unchanged_list_page_is_parsed_from_the_stored_copy(
    session, rows, tmp_path
):
    """A conditional GET answered 304 hands back the stored body: the plan
    reads it as the page, never as a refusal. The crawl itself files no list
    page (a kept row is its own evidence, ``own_evidence``), so the stored
    copy here is put in place by hand — another process's (an enrichment
    fetch, a SERP hit's page) is what a live 304 would answer against."""
    for key in ("directories_yellowpages", "directories_glotr"):
        await switch(key, enabled=False)
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    body = page("goldenpages_rubric_106080_p1").body
    tag = '"namuna-106080"'
    url = "https://www.goldenpages.uz/rubrics/?Id=106080"
    async with SessionLocal() as own:
        await evidence_store.store(
            own,
            source_key="directories_goldenpages",
            url=url,
            body=body,
            content_type="text/html",
            fetched_at=THURSDAY - timedelta(days=7),
            meta={"etag": tag},
            root=tmp_path,
        )
        await own.commit()

    def china_rubric(request: httpx.Request) -> httpx.Response:
        if Web.query(request) != {"Id": "106080"}:
            return web.rubrics(request)
        if request.headers.get("if-none-match") == tag:
            return httpx.Response(304, headers={"etag": tag})
        return httpx.Response(200, headers={**HTML, "etag": tag}, content=body)

    web.site.add("https://www.goldenpages.uz/rubrics/", china_rubric)
    report = await run(web, clock, tmp_path)
    gold = report.meta["subsources"]["goldenpages"]
    assert any(s.request.headers.get("if-none-match") == tag for s in web.site.seen)
    assert "304" not in gold["list_status"]
    assert gold["pages"] == 2 and report.meta["unchanged"] == 1
    # The eight companies of the 304 page were read off the stored copy.
    by_source = await observations(session)
    assert len(by_source["directories_goldenpages"]) == 28


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("no answer", request=request)


async def test_a_card_that_never_answers_costs_that_card_not_the_night(
    session, rows, tmp_path
):
    """The Fetcher's retries spent, a transport error on one card: its list
    row is kept and the crawl goes on — the other cards, the other sites."""
    clock = FakeClock(THURSDAY)
    web = Web(clock)

    def company(request: httpx.Request) -> httpx.Response:
        if Web.query(request) == {"Id": "85641"}:
            return timeout(request)
        return web.company(request)

    web.site.add("https://www.goldenpages.uz/company/", company)
    report = await run(web, clock, tmp_path)
    assert report.status == "ok", report
    gold = report.meta["subsources"]["goldenpages"]
    assert (gold["card_errors"], gold["cards"]) == (1, 0)
    by_source = await observations(session)
    gold_rows = {o["source_ref"] for o in by_source["directories_goldenpages"]}
    assert "goldenpages:85641" in gold_rows and len(gold_rows) == 28
    # The crawl went on past the dead card: top.uz and glotr ran too.
    assert by_source["directories_topuz"] and by_source["directories_glotr"]


async def test_a_list_page_that_never_answers_costs_that_seed(session, rows, tmp_path):
    clock = FakeClock(THURSDAY)
    web = Web(clock)

    def rubrics(request: httpx.Request) -> httpx.Response:
        if Web.query(request) == {"Id": "1424"}:
            return timeout(request)
        return web.rubrics(request)

    web.site.add("https://www.goldenpages.uz/rubrics/", rubrics)
    report = await run(web, clock, tmp_path)
    assert report.status == "ok", report
    gold = report.meta["subsources"]["goldenpages"]
    assert gold["list_status"]["transport"] == 1
    # The next seed (106080, the China rubric) was read.
    assert gold["pages"] == 1
    by_source = await observations(session)
    assert len(by_source["directories_goldenpages"]) == 8


async def test_a_host_that_stops_answering_stops_its_own_site_only(
    session, rows, tmp_path
):
    """Five transport failures in a row on one site end that site's crawl
    (its list rows kept); the other sites still run."""
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    web.site.add("https://www.goldenpages.uz/company/", timeout)
    report = await run(web, clock, tmp_path)
    assert report.status == "ok", report
    gold = report.meta["subsources"]["goldenpages"]
    assert gold["card_errors"] == 5 and gold["stops"]["transport"] == 1
    cards = [s for s in web.site.seen if s.path == "/company/"]
    assert len(cards) == 5 * 3  # five cards, the Fetcher's three attempts each
    by_source = await observations(session)
    assert len(by_source["directories_goldenpages"]) == 20  # rubric 1424's rows
    assert by_source["directories_topuz"] and by_source["directories_glotr"]


async def test_a_page_the_parser_cannot_read_costs_that_page(session, rows, tmp_path):
    for key in ("directories_yellowpages", "directories_glotr"):
        await switch(key, enabled=False)
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    real = gp.SPEC.parse_card

    def broken(body: bytes, *, url: str, fetched_at: datetime) -> list:
        if url.endswith("85641"):
            raise ValueError("a layout nobody recorded")
        return real(body, url=url, fetched_at=fetched_at)

    def broken_list(body: bytes, *, url: str, fetched_at: datetime):
        if "106080" in url:
            raise ValueError("a layout nobody recorded")
        return gp.SPEC.parse_list(body, url=url, fetched_at=fetched_at)

    spec = dataclasses.replace(gp.SPEC, parse_card=broken, parse_list=broken_list)
    sub = next(s for s in directories.SUBSOURCES if s.site == "goldenpages")
    patched = tuple(
        dataclasses.replace(s, spec=spec) if s is sub else s
        for s in directories.SUBSOURCES
    )
    original = directories.SUBSOURCES
    directories.SUBSOURCES = patched
    try:
        report = await run(web, clock, tmp_path)
    finally:
        directories.SUBSOURCES = original
    assert report.status == "ok", report
    gold = report.meta["subsources"]["goldenpages"]
    assert gold["parse_errors"] == 2
    by_source = await observations(session)
    refs = {o["source_ref"] for o in by_source["directories_goldenpages"]}
    assert "goldenpages:85641" in refs and len(refs) == 20


async def test_a_kept_list_row_cites_its_own_evidence_with_its_own_excerpt(
    session, rows, tmp_path
):
    """A list page is one body for twenty companies, and an evidence row has
    one excerpt: the quote a lead card verifies (02 §2.6) must be found in the
    row's OWN words. Every kept list row is its own evidence (the runner's
    canonical body), at the page's URL with the row named in the fragment."""
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    report = await run(web, clock, tmp_path)
    assert report.status == "ok", report
    kept = (
        await session.execute(
            sa.select(ObservationRow, Evidence)
            .join(Evidence, Evidence.id == ObservationRow.evidence_id)
            .where(ObservationRow.parsed["source_ref"].astext.like("%:%"))
        )
    ).all()
    list_rows = [
        (o, e)
        for o, e in kept
        if not e.url.startswith(
            (
                "https://www.goldenpages.uz/company/",
                "https://top.uz/company/",
                "https://www.yellowpages.uz/kompaniya/",
                "https://glotr.uz/showcase/",
            )
        )
    ]
    assert len(list_rows) >= 60
    for obs, evidence in list_rows:
        ref = obs.parsed["source_ref"]
        assert evidence.excerpt == (obs.parsed["text"]["excerpt"] or None), ref
        assert evidence.source_ref == ref
        assert evidence.url.endswith(f"#{ref}") and obs.parsed["url"] == evidence.url
    # One evidence row per company: none is shared between two list rows.
    assert len({e.id for _, e in list_rows}) == len(list_rows)
    # A card is still cited at the card's own URL.
    card = next(e for o, e in kept if o.parsed["source_ref"] == "goldenpages:85641")
    assert card.url == "https://www.goldenpages.uz/company/?Id=85641"


async def test_top_uz_has_its_own_row_and_its_own_pause(session, rows, tmp_path):
    """06 Open question 23 (a): the precision job pauses the row a lead is
    credited to. top.uz is credited to its own row (``directories_topuz``,
    0004), so a week's pause of a weak top.uz stops top.uz alone — the parent
    row, which schedules every site, is never the one paused."""
    clock = FakeClock(THURSDAY)
    await switch(
        "directories_topuz",
        paused_until=THURSDAY + timedelta(days=7),
        paused_reason="low_precision",
    )
    web = Web(clock)
    report = await run(web, clock, tmp_path)
    assert report.status == "ok", report
    assert report.meta["subsources"]["topuz"] == {"skipped": "paused"}
    assert web.urls("top.uz") == []
    assert web.urls("www.goldenpages.uz") and web.urls("glotr.uz")
    await switch("directories_topuz", paused_until=None, paused_reason=None)
    again = await run(Web(clock), clock, tmp_path)
    assert again.meta["subsources"]["topuz"]["seeds"] == 2
    by_source = await observations(session)
    assert by_source["directories_topuz"]
    assert all(
        o["source_ref"].startswith("topuz:") for o in by_source["directories_topuz"]
    )
    assert "directories" not in by_source  # nothing is credited to the parent


# A challenge interstitial's own script (services/http MARKUP_MARKERS «cf-chl»).
CHALLENGE = b'<script src="/cdn-cgi/challenge-platform/h/b/cf-chl/v1"></script>'


async def test_a_challenge_page_is_never_read_as_data(session, rows, tmp_path):
    """03 §1.1: never around a CAPTCHA. A list page or a card answered with a
    challenge — even one that carries the page's own markup — is no page:
    counted, never parsed; the card's list row is kept instead."""
    for key in ("directories_yellowpages", "directories_glotr", "directories_topuz"):
        await switch(key, enabled=False)
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    card = page("goldenpages_company_85641").body.replace(
        b"</head>", CHALLENGE + b"</head>", 1
    )
    china = page("goldenpages_rubric_106080_p1").body.replace(
        b"</head>", CHALLENGE + b"</head>", 1
    )

    def company(request: httpx.Request) -> httpx.Response:
        if Web.query(request) == {"Id": "85641"}:
            return httpx.Response(200, headers=HTML, content=card)
        return web.company(request)

    def rubrics(request: httpx.Request) -> httpx.Response:
        if Web.query(request) == {"Id": "106080"}:
            return httpx.Response(200, headers=HTML, content=china)
        return web.rubrics(request)

    web.site.add("https://www.goldenpages.uz/company/", company)
    web.site.add("https://www.goldenpages.uz/rubrics/", rubrics)
    report = await run(web, clock, tmp_path)
    gold = report.meta["subsources"]["goldenpages"]
    assert gold["list_status"]["captcha"] == 1 and "empty" not in gold["stops"]
    assert gold["cards"] == 0 and gold["pages"] == 1
    by_source = await observations(session)
    rows_kept = {o["source_ref"]: o for o in by_source["directories_goldenpages"]}
    # The card's row is the list row: no phone, none of the card's own fields.
    assert len(rows_kept) == 20 and rows_kept["goldenpages:85641"]["phones"] == []


async def test_the_diff_never_asks_again_for_a_company_observed_long_ago(
    session, rows, tmp_path
):
    """The weekly diff asks a card only for a company the source has never
    observed — not «not in the last 45 days», which is the full crawl's rule."""
    for key in ("directories_yellowpages", "directories_glotr", "directories_topuz"):
        await switch(key, enabled=False)
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    await run(web, clock, tmp_path)
    asked = Counter(s.path for s in web.site.seen)
    later = FakeClock(THURSDAY + timedelta(days=7 * 8))  # eight weeks: > CARD_FRESH
    web.site.clock = later
    second = await run(web, later, tmp_path)
    assert second.meta["mode"] == "diff"
    gold = second.meta["subsources"]["goldenpages"]
    assert (gold["known"], gold["cards"], gold["card_missing"]) == (28, 0, 0)
    assert (Counter(s.path for s in web.site.seen) - asked)["/company/"] == 0


async def test_a_card_robots_txt_refuses_keeps_its_list_row(session, rows, tmp_path):
    for key in ("directories_yellowpages", "directories_glotr", "directories_topuz"):
        await switch(key, enabled=False)
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    refused = robots("www.goldenpages.uz") + "\nUser-agent: *\nDisallow: /company/\n"
    web.site.add(
        "https://www.goldenpages.uz/robots.txt", httpx.Response(200, text=refused)
    )
    report = await run(web, clock, tmp_path)
    gold = report.meta["subsources"]["goldenpages"]
    assert gold["robots"] == 28 and gold["rows"] == 28
    assert not [s for s in web.site.seen if s.path == "/company/"]
    by_source = await observations(session)
    assert len(by_source["directories_goldenpages"]) == 28


async def test_a_seed_reads_at_most_max_list_pages(session, rows, tmp_path, monkeypatch):
    monkeypatch.setattr(directories, "MAX_LIST_PAGES", 2)
    clock = FakeClock(datetime(2026, 11, 1, 23, 0, tzinfo=TASHKENT))
    web = Web(clock)
    report = await run(web, clock, tmp_path, variant="full")
    gold = report.meta["subsources"]["goldenpages"]
    assert gold["stops"]["page_cap"] == 1
    assert [u for u in web.urls("www.goldenpages.uz") if "Id=1424" in u] == [
        "https://www.goldenpages.uz/rubrics/?Id=1424",
        "https://www.goldenpages.uz/rubrics/?Id=1424&Page=2",
    ]


async def test_the_directories_are_paced_under_03_s_half_a_request_a_second(
    session, rows, tmp_path
):
    """03 §2.6: «Rate 0.5 rps max». The directory hosts have their own pace
    (HOST_RPS), so a COLLECTOR_RPS_DEFAULT raised for other hosts does not
    raise theirs."""
    from clientscrapper.services import http

    for host in ("www.goldenpages.uz", "www.yellowpages.uz", "top.uz", "glotr.uz"):
        assert http.HOST_RPS[host] <= 0.5, host
    for key in (
        "directories_yellowpages",
        "directories_glotr",
        "directories_goldenpages",
    ):
        await switch(key, enabled=False)
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    report = await run_collector(
        Directories,
        fetcher=fetcher_for(web.site, rps_default=2.0),
        now=clock.now(),
        evidence_root=tmp_path,
        clock=clock.now,
    )
    assert report.status == "ok"
    times = [s.at for s in web.site.seen if s.request.url.host == "top.uz"]
    assert len(times) > 5 and all(b - a >= 2.0 for a, b in pairwise(times))
