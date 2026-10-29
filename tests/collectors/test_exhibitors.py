"""The exhibitors collector: its calendar, its window, its crawl (03 §2.8;
07 T-32).

The calendar half is pure. The crawl half is Postgres-backed and goes
through the runner: the network is ``tests/fakes.Site`` answering with the
recorded bodies of ``tests/fixtures/exhibitors`` (the README says what each
proves); time is ``FakeClock``. The ``exhibitors`` row is seeded
configuration: each test snapshots it and puts it back (07 §1.0).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from clientscrapper.collectors.exhibitors import (
    DEFAULT_CALENDAR,
    SHORT_PROBLEM,
    Edition,
    Exhibitors,
    ListResult,
    ShowEntry,
    _excerpt,
    calendar_from,
    entry_config,
    in_window,
    match_entry,
    pick_targets,
    plan_visits,
    rows_url,
)
from clientscrapper.collectors.runner import run_collector
from clientscrapper.db.models import CollectorRun, Evidence, Source
from clientscrapper.db.models import Observation as ObservationRow
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import health
from tests.fakes import TASHKENT, FakeClock, Site, fetcher_for

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "exhibitors"
HTML = {"content-type": "text/html; charset=utf-8"}
KEPT = ("enabled", "config", "daily_page_budget", "paused_until", "last_run_at")
MONDAY = datetime(2026, 11, 9, 9, 0, tzinfo=TASHKENT)
TODAY = datetime(2026, 10, 29, 9, 0, tzinfo=TASHKENT)
CATALOGUE = "https://catalog.automechanika.uz/en/expositions/exposition/7012"
PAGES = f"{CATALOGUE}-automechanika-tashkent-comtrux-tashkent-bautrux-tashkent-2026.html"
ENTRY = {e.show + str(e.end_date.year): e for e in DEFAULT_CALENDAR}


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, headers=HTML, content=body)


# --- the calendar (pure) ----------------------------------------------------------------


def test_the_default_calendar_is_the_config_shape_read_back():
    config = {"calendar": [entry_config(e) for e in DEFAULT_CALENDAR]}
    assert calendar_from(config) == (DEFAULT_CALENDAR, [])
    assert calendar_from({}) == (DEFAULT_CALENDAR, [])
    assert calendar_from(None) == (DEFAULT_CALENDAR, [])
    # An empty list is the owner's «no show», not the default.
    assert calendar_from({"calendar": []}) == ((), [])


def test_the_default_calendar_names_six_shows_two_editions_each():
    assert sorted({e.show for e in DEFAULT_CALENDAR}) == [
        "automechanika",
        "caitme",
        "mebelexpo",
        "power",
        "textileexpo",
        "uzbuild",
    ]
    assert {e.platform for e in DEFAULT_CALENDAR if e.show == "automechanika"} == {
        "expodat"
    }
    assert {e.platform for e in DEFAULT_CALENDAR if e.show != "automechanika"} == {"era"}
    # The dates the sites print (README item 18), not the spec's guesses.
    assert ENTRY["textileexpo2027"].end_date == date(2027, 4, 29)
    assert ENTRY["caitme2028"].end_date == date(2028, 9, 8)
    assert ENTRY["automechanika2026"].exclude == ("BMCA",)


def test_an_entry_the_collector_cannot_read_is_skipped_and_named():
    good = entry_config(ENTRY["uzbuild2027"])
    config = {
        "calendar": [
            good,
            {**good, "end_date": "18.03.2027"},
            {**good, "url_patterns": ["https://uzbuild.uz/en/exhibitors-list"]},
            {
                **good,
                "url_patterns": ["https://a.uz/x", "https://a.uz/spisok.php?PAGEN_2=1"],
            },
            {
                **good,
                "url_patterns": [
                    "https://uzbuild.uz/en/exhibitors-list",
                    "https://uzbuild.uz/ERAForms/companies_list.php?l=en&exhibition=461",
                ],
            },
            {**good, "expected": "450"},
            {**good, "show": "Uz Build"},
            "uzbuild",
        ]
    }
    entries, problems = calendar_from(config)
    assert entries == (ENTRY["uzbuild2027"],)
    assert problems == [
        "uzbuild: end_date",
        "uzbuild: url_patterns",
        "uzbuild: url_patterns[1] is no list address this collector reads",
        "uzbuild: url_patterns[1] is no list address this collector reads",
        "uzbuild: expected",
        "show",
        "not an object",
    ]
    assert calendar_from({"calendar": {"show": "uzbuild"}}) == (
        (),
        ["calendar is not a list"],
    )


@pytest.mark.parametrize(
    ("days", "inside"),
    [(9, False), (10, True), (30, True), (59, True), (60, False), (-1, False)],
)
def test_the_window_is_ten_to_sixty_days_after_the_show(days, inside):
    end = date(2026, 10, 23)
    assert in_window(end, end + timedelta(days=days)) is inside


def test_on_2026_10_29_caitme_is_in_its_window_and_every_archive_is_due():
    visits = plan_visits(DEFAULT_CALENDAR, TODAY.date(), archived=())
    assert [(v.show, [e.end_date for e in v.due], v.archive) for v in visits] == [
        ("caitme", [date(2026, 9, 10)], True),
        ("automechanika", [], True),
        ("uzbuild", [], True),
        ("power", [], True),
        ("mebelexpo", [], True),
        ("textileexpo", [], True),
    ]


def test_out_of_its_window_a_swept_expodat_show_is_not_visited_an_era_one_is():
    """An ERA index is one request a week and prints its editions' dates;
    the expodat catalogue prints none, so its index waits for its window."""
    swept = ("automechanika", "uzbuild", "power", "mebelexpo", "textileexpo")
    visits = plan_visits(DEFAULT_CALENDAR, date(2026, 10, 30), archived=swept)
    assert [(v.show, len(v.due), v.archive) for v in visits] == [
        ("caitme", 1, True),
        ("uzbuild", 0, False),
        ("power", 0, False),
        ("mebelexpo", 0, False),
        ("textileexpo", 0, False),
    ]
    # Automechanika 2026 (21–23 Oct) opens on 2 Nov.
    visits = plan_visits(DEFAULT_CALENDAR, date(2026, 11, 2), archived=swept)
    assert [(v.show, len(v.due), v.archive) for v in visits][:2] == [
        ("automechanika", 1, False),
        ("caitme", 1, True),
    ]


def test_an_era_edition_is_the_entry_of_its_printed_date():
    entries = [ENTRY["textileexpo2026"], ENTRY["textileexpo2027"]]
    assert match_entry(Edition("504", 2027, date(2027, 4, 29)), entries) is entries[1]
    # A date the calendar has slightly wrong (the spec said May 2027).
    may = replace_end(ENTRY["textileexpo2027"], date(2027, 5, 20))
    assert match_entry(Edition("504", 2027, date(2027, 4, 29)), [may]) is may
    # The autumn edition is no calendar entry.
    assert match_entry(Edition("442", 2024, date(2024, 10, 17)), entries) is None
    # expodat prints no dates: the entry of the year.
    am = [ENTRY["automechanika2026"], ENTRY["automechanika2027"]]
    assert match_entry(Edition("7012", 2026), am) is am[0]


def replace_end(entry: ShowEntry, end: date) -> ShowEntry:
    from dataclasses import replace

    return replace(entry, end_date=end)


def era_editions(show: str) -> list[Edition]:
    return Exhibitors.parse_era_editions(
        raw(f"{show}_list_en.html"), url="https://x/en/exhibitors-list", fetched_at=TODAY
    )


def visit_of(show: str, today: date, *, archived: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    [visit] = [
        v for v in plan_visits(DEFAULT_CALENDAR, today, archived) if v.show == show
    ]
    return visit


def test_the_targets_of_a_sweep_are_the_editions_whose_window_is_over():
    visit = visit_of("caitme", TODAY.date())
    targets, missing = pick_targets(visit, era_editions("caitme"), TODAY.date())
    # This week's list first, then the archive (a budget stop takes the
    # archive, never the show in its window).
    assert [(t.edition.id, t.why, t.entry) for t in targets] == [
        ("480", "window", ENTRY["caitme2026"]),
        ("362", "archive", None),
        ("446", "archive", None),
    ]
    assert missing == []


def test_an_edition_the_site_prints_inside_its_window_is_read_without_an_entry():
    """TextileExpo's September 2025 edition: no calendar entry, its dates on
    the site (README item 16)."""
    today = date(2025, 9, 29)
    visit = visit_of("textileexpo", today, archived=("textileexpo",))
    assert visit.due == ()
    targets, _ = pick_targets(visit, era_editions("textileexpo"), today)
    assert [(t.edition.id, t.why) for t in targets] == [("472", "window")]


def test_an_in_window_entry_the_index_does_not_list_is_missing():
    visit = visit_of("automechanika", MONDAY.date(), archived=("automechanika",))
    targets, missing = pick_targets(visit, [], MONDAY.date())
    assert (targets, missing) == ([], [ENTRY["automechanika2026"]])
    linked = [Edition("7012", 2026, url=CATALOGUE)]
    targets, missing = pick_targets(visit, linked, MONDAY.date())
    assert [(t.edition.id, t.why) for t in targets] == [("7012", "window")]
    assert missing == []


def test_an_owner_s_pattern_takes_the_edition_and_nothing_else():
    pattern = (
        "https://uzbuild.uz/ERAForms/companies_list.php?l=en&exhibition={edition}&x={y}"
    )
    assert rows_url(pattern, "461") == (
        "https://uzbuild.uz/ERAForms/companies_list.php?l=en&exhibition=461&x={y}"
    )


def test_judging_a_list_against_its_organiser():
    entry = ENTRY["caitme2026"]  # expected 210
    result = ListResult("caitme", 2026, "480", "window", "complete", entry, rows=104)
    assert result.short() and result.incomplete() == "caitme:2026:short:104/210"
    result.rows = 105
    assert not result.short() and result.incomplete() is None
    gone = ListResult("caitme", 2026, None, "window", "missing", entry)
    assert gone.short() and gone.incomplete() == "caitme:2026:missing"
    # A walk the site cut short is incomplete, never judged short.
    cut = ListResult("caitme", 2026, "480", "window", "truncated", entry, rows=3)
    assert not cut.short() and cut.incomplete() == "caitme:2026:truncated"
    # An archive has no organiser count to fall under.
    archive = ListResult("caitme", 2024, "362", "archive", "complete", None, rows=1)
    assert not archive.short() and not archive.judged()


def test_an_excerpt_is_cut_at_a_word_under_600():
    text = _excerpt(["NAMUNA", "A 1", "Uzbekistan", "soʻz " * 200])
    assert len(text) <= 600 and text.endswith("soʻz")
    assert _excerpt(["NAMUNA", None, "", "Uzbekistan"]) == "NAMUNA · Uzbekistan"


# --- the crawl (Postgres, through the runner) -------------------------------------------


@pytest.fixture
async def exhibitors_row(session):  # type: ignore[no-untyped-def]
    """Enable the seeded row with the calendar a test gives, and put back
    every column the run or the test moves."""
    async with SessionLocal() as s:
        row = await s.get(Source, "exhibitors")
        assert row is not None
        snapshot = {name: getattr(row, name) for name in KEPT}

    async def setup(calendar: list[ShowEntry], **values: Any) -> None:
        config = {"calendar": [entry_config(e) for e in calendar]}
        async with SessionLocal() as s:
            await s.execute(
                sa.update(Source)
                .where(Source.key == "exhibitors")
                .values(enabled=True, config=config, **values)
            )
            await s.commit()

    yield setup
    async with SessionLocal() as s:
        await s.execute(
            sa.update(Source).where(Source.key == "exhibitors").values(**snapshot)
        )
        await s.commit()


def era_answer(bodies: Mapping[str, bytes]) -> Callable[[httpx.Request], httpx.Response]:
    """``companies_list.php`` by ``exhibition``; anything else is the bare
    endpoint's HTTP 500 with an empty body (README «Refused»)."""

    def answer(request: httpx.Request) -> httpx.Response:
        body = bodies.get(request.url.params.get("exhibition", ""))
        return ok(body) if body is not None else httpx.Response(500, content=b"")

    return answer


def catalogue_answer(
    pages: Mapping[int, bytes],
) -> Callable[[httpx.Request], httpx.Response]:
    """The catalogue's later pages by ``start``; any other offset is the
    recorded page past the end."""

    def answer(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", "0"))
        return ok(pages.get(start, raw("automechanika_catalog_2026_past_end.html")))

    return answer


def caitme_site(clock: FakeClock, **bodies: bytes) -> Site:
    return Site(
        clock,
        {
            "https://caitme.uz/en/exhibitors-list": ok(raw("caitme_list_en.html")),
            "https://caitme.uz/ERAForms/companies_list.php": era_answer(
                {"480": raw("caitme_2026_companies_all.json"), **bodies}
            ),
        },
    )


def automechanika_site(
    clock: FakeClock, pages: Mapping[int, bytes], **routes: Any
) -> Site:
    return Site(
        clock,
        {
            "https://automechanika.uz/eng/participants": ok(
                raw("automechanika_participants_en.html")
            ),
            CATALOGUE: ok(raw("automechanika_catalog_2026_p1.html")),
            PAGES: catalogue_answer(pages),
            **routes,
        },
    )


async def run(site: Site, clock: FakeClock, tmp_path: Path):  # type: ignore[no-untyped-def]
    return await run_collector(
        Exhibitors,
        fetcher=fetcher_for(site),
        now=clock.now(),
        clock=clock.now,
        evidence_root=tmp_path,
    )


async def swept(session, *shows: str, at: datetime) -> None:  # type: ignore[no-untyped-def]
    """A run that swept these shows' archives (``meta.archived``)."""
    session.add(
        CollectorRun(
            source_key="exhibitors",
            started_at=at,
            finished_at=at,
            status="ok",
            meta={"archived": list(shows)},
        )
    )
    await session.commit()


async def last_run(session) -> CollectorRun:  # type: ignore[no-untyped-def]
    row = await session.scalar(
        sa.select(CollectorRun)
        .order_by(CollectorRun.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def stored(session) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    rows = await session.scalars(
        sa.select(ObservationRow.parsed)
        .order_by(ObservationRow.id)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def problem(session, at: datetime) -> health.Reported | None:  # type: ignore[no-untyped-def]
    return (await health.reported_problems(session, at)).get(SHORT_PROBLEM)


async def test_a_show_in_its_window_stores_its_uzbek_rows_only(
    session, exhibitors_row, tmp_path
):
    await exhibitors_row([ENTRY["caitme2026"]])
    clock = FakeClock(TODAY)
    await swept(session, "caitme", at=TODAY - timedelta(days=30))
    site = caitme_site(clock)
    report = await run(site, clock, tmp_path)

    assert report.status == "ok"
    row = await last_run(session)
    assert (row.status, row.pages, row.new_evidence, row.new_observations) == (
        "ok",
        2,
        1,
        40,
    )
    assert row.meta["lists"] == [
        {
            "show": "caitme",
            "year": 2026,
            "edition": "480",
            "why": "window",
            "status": "complete",
            "rows": 170,
            "kept": 40,
            "foreign": 129,
            "skipped": 1,
            "pages": 1,
            "total": 170,
            "expected": 210,
        }
    ]
    assert row.meta["archived"] == [] and "incomplete" not in row.meta
    parsed = await stored(session)
    assert {p["kind"] for p in parsed} == {"exhibitor"}
    assert {p["hints"]["collector"]["attrs"]["country"] for p in parsed} == {"Uzbekistan"}
    assert "GOLDEN PAGES" not in {p["company"]["name_raw"] for p in parsed}
    [evidence] = list(await session.scalars(sa.select(Evidence)))
    assert evidence.url == (
        "https://caitme.uz/ERAForms/companies_list.php?l=en&exhibition=480"
        "&draw=1&start=0&length=500"
    )
    # 1 request per 3 s per host at most (03 §2.8): robots.txt, index, list.
    at = [s.at for s in site.seen if s.request.url.host == "caitme.uz"]
    assert len(at) == 3 and all(b - a >= 3.0 for a, b in pairwise(at))
    assert await problem(session, clock.now()) is None


async def test_a_list_under_half_its_organiser_s_count_is_partial_and_alerts(
    session, exhibitors_row, tmp_path
):
    entry = replace_expected(ENTRY["caitme2026"], 400)
    await exhibitors_row([entry])
    clock = FakeClock(TODAY)
    await swept(session, "caitme", at=TODAY - timedelta(days=30))
    report = await run(caitme_site(clock), clock, tmp_path)

    assert report.status == "partial"
    row = await last_run(session)
    assert row.meta["incomplete"] == ["caitme:2026:short:170/400"]
    found = await problem(session, clock.now())
    assert found is not None
    assert found.detail == {
        "lists": [
            {
                "show": "caitme",
                "title": "CAITME",
                "year": 2026,
                "rows": 170,
                "expected": 400,
                "status": "complete",
                "until": "2026-11-09",
            }
        ]
    }
    assert found.until == datetime(2026, 11, 9, tzinfo=TASHKENT)
    [line] = health.exhibitor_list_lines(found)
    assert line.startswith("⚠️ CAITME 2026: koʻrgazma roʻyxatida 170 ta ishtirokchi")
    assert "tashkilotchi 400 ta deydi (42%)" in line

    # The next Monday the list reads whole against the right figure: cleared.
    await exhibitors_row([ENTRY["caitme2026"]])
    clock = FakeClock(TODAY + timedelta(days=7))
    report = await run(caitme_site(clock), clock, tmp_path)
    assert report.status == "ok"
    assert await problem(session, clock.now()) is None


def replace_expected(entry: ShowEntry, expected: int) -> ShowEntry:
    from dataclasses import replace

    return replace(entry, expected=expected)


async def test_a_removed_in_window_catalogue_is_partial_and_alerts(
    session, exhibitors_row, tmp_path
):
    """The 2025 catalogue's fate (README «Files»): the exposition redirects to
    the login page, «The event was not found»."""
    await exhibitors_row([ENTRY["automechanika2026"]])
    clock = FakeClock(MONDAY)
    await swept(session, "automechanika", at=MONDAY - timedelta(days=30))
    site = automechanika_site(
        clock,
        {},
        **{
            CATALOGUE: httpx.Response(
                302,
                headers={
                    "location": "https://catalog.automechanika.uz/automechanika-main.html"
                },
            ),
            "https://catalog.automechanika.uz/automechanika-main.html": ok(
                raw("automechanika_catalog_2025_p1.html")
            ),
        },
    )
    report = await run(site, clock, tmp_path)

    assert report.status == "partial"
    row = await last_run(session)
    assert row.meta["incomplete"] == ["automechanika:2026:removed"]
    assert row.new_observations == 0
    found = await problem(session, clock.now())
    assert found is not None and found.detail["lists"][0]["status"] == "removed"
    [line] = health.exhibitor_list_lines(found)
    assert "roʻyxati saytda topilmadi" in line and "Automechanika Tashkent 2026" in line


async def test_the_catalogue_is_read_page_by_page_until_a_short_page(
    session, exhibitors_row, tmp_path
):
    """Step 48 from the page's own pager (never the widget's 8s); a page of
    fewer rows ends the walk. Its «End» link says 457 rows at least, so the
    three pages served here are a truncated walk: partial, never judged
    short."""
    await exhibitors_row([ENTRY["automechanika2026"]])
    clock = FakeClock(MONDAY)
    await swept(session, "automechanika", at=MONDAY - timedelta(days=30))
    site = automechanika_site(
        clock,
        {
            48: raw("automechanika_catalog_2026_p2.html"),
            96: raw("automechanika_catalog_2026_last.html"),
        },
    )
    report = await run(site, clock, tmp_path)

    assert report.status == "partial"
    asked = [s.url for s in site.seen if s.request.url.host == "catalog.automechanika.uz"]
    assert asked == [
        "https://catalog.automechanika.uz/robots.txt",
        CATALOGUE,
        f"{PAGES}?start=48",
        f"{PAGES}?start=96",
    ]
    row = await last_run(session)
    assert row.meta["incomplete"] == ["automechanika:2026:truncated"]
    [listed] = row.meta["lists"]
    assert (listed["rows"], listed["kept"], listed["pages"], listed["total"]) == (
        102,
        10,
        3,
        457,
    )
    names = [p["company"]["name_raw"] for p in await stored(session)]
    assert names[:4] == ["Autoport", "AutoZIP", "BRZ", "Carville"]
    assert "BMCA" not in names and len(names) == 10
    assert await problem(session, clock.now()) is None


async def test_an_empty_page_stops_the_walk(session, exhibitors_row, tmp_path):
    """The paginator-stop fixture: start=48 is past this walk's end."""
    await exhibitors_row([ENTRY["automechanika2026"]])
    clock = FakeClock(MONDAY)
    await swept(session, "automechanika", at=MONDAY - timedelta(days=30))
    site = automechanika_site(clock, {})
    await run(site, clock, tmp_path)

    asked = [s.url for s in site.seen if s.request.url.host == "catalog.automechanika.uz"]
    assert asked[-2:] == [CATALOGUE, f"{PAGES}?start=48"]
    row = await last_run(session)
    assert (row.meta["lists"][0]["rows"], row.meta["lists"][0]["pages"]) == (48, 1)


async def test_a_page_that_repeats_one_already_read_stops_the_walk(
    session, exhibitors_row, tmp_path
):
    """A server that ignores ``start`` would hand page 1 back for ever."""
    await exhibitors_row([ENTRY["automechanika2026"]])
    clock = FakeClock(MONDAY)
    await swept(session, "automechanika", at=MONDAY - timedelta(days=30))
    p1 = raw("automechanika_catalog_2026_p1.html")
    site = automechanika_site(clock, dict.fromkeys(range(48, 48 * 40, 48), p1))
    await run(site, clock, tmp_path)

    asked = [s.url for s in site.seen if s.request.url.host == "catalog.automechanika.uz"]
    assert asked[-2:] == [CATALOGUE, f"{PAGES}?start=48"]
    row = await last_run(session)
    assert row.meta["lists"][0]["status"] == "truncated"
    assert row.meta["lists"][0]["rows"] == 48


async def test_an_era_answer_that_repeats_stops_the_walk(
    session, exhibitors_row, tmp_path
):
    """The endpoint answering the same 100 rows (of 341) whatever ``start``."""
    await exhibitors_row([ENTRY["uzbuild2026"]])
    clock = FakeClock(datetime(2026, 2, 23, 9, 0, tzinfo=TASHKENT))
    await swept(session, "uzbuild", at=clock.now() - timedelta(days=30))
    site = Site(
        clock,
        {
            "https://uzbuild.uz/en/exhibitors-list": ok(raw("uzbuild_list_en.html")),
            "https://uzbuild.uz/ERAForms/companies_list.php": ok(
                raw("uzbuild_2026_companies_p2.json")
            ),
        },
    )
    report = await run(site, clock, tmp_path)

    assert report.status == "partial"
    starts = [
        s.request.url.params.get("start")
        for s in site.seen
        if s.request.url.path.endswith("companies_list.php")
    ]
    assert starts == ["0", "100"]
    row = await last_run(session)
    [listed] = row.meta["lists"]
    assert (listed["status"], listed["rows"], listed["total"]) == ("truncated", 100, 341)
    assert row.meta["incomplete"] == ["uzbuild:2026:truncated"]


async def test_the_archive_is_swept_once_a_year(session, exhibitors_row, tmp_path):
    """The first run reads every edition whose window is over; the next
    Monday reads nothing; a year on, the sweep comes round again."""
    await exhibitors_row([ENTRY["uzbuild2027"]])
    whole = raw("uzbuild_2026_companies_all.json")
    foreign = raw("power_2027_companies_foreign_only.json")

    def site(clock: FakeClock) -> Site:
        return Site(
            clock,
            {
                "https://uzbuild.uz/en/exhibitors-list": ok(raw("uzbuild_list_en.html")),
                "https://uzbuild.uz/ERAForms/companies_list.php": era_answer(
                    {"401": foreign, "430": foreign, "461": whole, "490": whole}
                ),
            },
        )

    clock = FakeClock(TODAY)
    assert (await run(site(clock), clock, tmp_path)).status == "ok"
    first = await last_run(session)
    assert first.meta["archived"] == ["uzbuild"]
    assert [(x["edition"], x["why"], x["status"]) for x in first.meta["lists"]] == [
        ("401", "archive", "complete"),
        ("430", "archive", "complete"),
        ("461", "archive", "complete"),
    ]
    assert first.new_observations == 77  # UzBuild 2026's Uzbek rows less GOLDEN PAGES

    # The next Monday only the index is read (its printed dates), no list.
    clock = FakeClock(TODAY + timedelta(days=4))
    again = site(clock)
    assert (await run(again, clock, tmp_path)).status == "ok"
    assert [s.path for s in again.seen] == ["/robots.txt", "/en/exhibitors-list"]
    assert (await last_run(session)).meta["lists"] == []

    clock = FakeClock(TODAY + timedelta(days=366))
    year_on = site(clock)
    await run(year_on, clock, tmp_path)
    latest = await last_run(session)
    assert latest.meta["archived"] == ["uzbuild"]
    # 2027 (16–18 Mar) is over by then too.
    assert [x["edition"] for x in latest.meta["lists"]] == ["401", "430", "461", "490"]


async def test_the_page_budget_stops_the_run_and_keeps_what_it_read(
    session, exhibitors_row, tmp_path
):
    """≤ 100 pages a run (03 §2.8, sources.daily_page_budget): out of pages,
    the run ends partial with the list it finished stored; the archive sweep
    it could not finish is not marked done."""
    await exhibitors_row([ENTRY["caitme2026"]], daily_page_budget=2)
    clock = FakeClock(TODAY)
    report = await run(caitme_site(clock), clock, tmp_path)

    assert report.status == "partial"
    row = await last_run(session)
    assert (row.pages, row.new_observations) == (2, 40)
    assert row.meta["exhausted"] == "pages"
    assert row.meta["archived"] == []
    assert [x["edition"] for x in row.meta["lists"]] == ["480"]


async def test_a_list_the_site_will_not_serve_is_failed_and_the_run_goes_on(
    session, exhibitors_row, tmp_path
):
    """CAITME's archives answer the bare endpoint's 500: each is failed, the
    in-window list is still read and stored, the sweep is asked again."""
    await exhibitors_row([ENTRY["caitme2026"]])
    clock = FakeClock(TODAY)
    report = await run(caitme_site(clock), clock, tmp_path)

    assert report.status == "partial"
    row = await last_run(session)
    assert [(x["edition"], x["status"], x.get("error")) for x in row.meta["lists"]] == [
        ("480", "complete", None),
        ("362", "failed", "http_500"),
        ("446", "failed", "http_500"),
    ]
    assert row.meta["incomplete"] == ["caitme:2024:failed", "caitme:2025:failed"]
    assert row.meta["archived"] == []
    assert row.new_observations == 40


async def test_a_disabled_row_reads_nothing(session, tmp_path):
    clock = FakeClock(TODAY)
    site = caitme_site(clock)
    report = await run(site, clock, tmp_path)
    assert (report.status, report.meta["note"]) == ("skipped", "source_disabled")
    assert site.seen == []


async def test_a_short_line_from_an_earlier_run_survives_a_run_of_another_show(
    session, exhibitors_row, tmp_path
):
    """The alert holds one line per show: a run that judges another show
    leaves CAITME's line standing; CAITME's own window end retires it."""
    flag_at = TODAY - timedelta(days=1)
    await health.raise_problem(
        session,
        SHORT_PROBLEM,
        now=flag_at,
        by="collector:exhibitors",
        detail={
            "lists": [
                {
                    "show": "caitme",
                    "title": "CAITME",
                    "year": 2026,
                    "rows": 3,
                    "expected": 210,
                    "status": "complete",
                    "until": "2026-11-09",
                }
            ]
        },
        until=datetime(2026, 11, 9, tzinfo=TASHKENT),
    )
    await session.commit()
    await exhibitors_row([ENTRY["automechanika2026"]])
    clock = FakeClock(MONDAY - timedelta(days=7))
    await swept(session, "automechanika", at=clock.now() - timedelta(days=30))
    await run(automechanika_site(clock, {}), clock, tmp_path)
    found = await problem(session, clock.now())
    assert found is not None
    assert [x["show"] for x in found.detail["lists"]] == ["caitme"]
    # Past CAITME's window the line expires by itself.
    assert await problem(session, datetime(2026, 11, 10, tzinfo=TASHKENT)) is None


async def test_a_stored_row_resolves_to_a_company_gate_3_reads_the_country_of(
    session, exhibitors_row, tmp_path
):
    """The carried item (07 T-32): every kept row stores ``attrs.country``
    and gate 3 reads it off the company's resolved exhibitor observations —
    an Uzbek one passes «not a supplier»; the ERA id becomes the company's
    ``exhibitor_slug``."""
    from clientscrapper.db.models import CompanyId
    from clientscrapper.services import companies, gates

    await exhibitors_row([ENTRY["caitme2026"]])
    clock = FakeClock(TODAY)
    await swept(session, "caitme", at=TODAY - timedelta(days=30))
    await run(caitme_site(clock), clock, tmp_path)
    counts = await companies.resolve_job(now=clock.now())
    assert counts["claimed"] == 40

    company_id = await session.scalar(
        sa.select(CompanyId.company_id).where(
            CompanyId.id_type == "exhibitor_slug", CompanyId.id_value == "era:25848"
        )
    )
    assert company_id is not None  # ADVANTEX, OOO
    got = await gates.inputs_for(session, company_id, now=clock.now())
    assert got.company.exhibitor_countries == frozenset({"UZBEKISTAN"})
    result = gates.run_gates(
        got.company,
        got.contacts,
        got.signals,
        got.verdict,
        got.hit,
        got.recent,
        clock.now(),
    )
    assert not result.marks["not_supplier"].startswith("fail")


async def test_a_page_the_site_answers_304_is_read_from_the_stored_copy(
    session, exhibitors_row, tmp_path
):
    """The Fetcher's conditional GET (03 §1.1): the catalogue sends
    ``last-modified``; a week on, a 304 is the page as stored, read again and
    counted, with nothing new written."""
    await exhibitors_row([ENTRY["automechanika2026"]])
    stamp = "Mon, 09 Nov 2026 04:00:00 GMT"

    def conditional(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
        def answer(request: httpx.Request) -> httpx.Response:
            if request.headers.get("if-modified-since") == stamp:
                return httpx.Response(304, headers={"last-modified": stamp})
            return httpx.Response(
                200, headers={**HTML, "last-modified": stamp}, content=body
            )

        return answer

    def site(clock: FakeClock) -> Site:
        return automechanika_site(
            clock,
            {},
            **{CATALOGUE: conditional(raw("automechanika_catalog_2026_p1.html"))},
        )

    clock = FakeClock(MONDAY)
    await swept(session, "automechanika", at=MONDAY - timedelta(days=30))
    await run(site(clock), clock, tmp_path)
    first = await last_run(session)
    assert first.new_observations == 4

    clock = FakeClock(MONDAY + timedelta(days=7))
    again = site(clock)
    await run(again, clock, tmp_path)
    second = await last_run(session)
    conditional_asks = [
        s
        for s in again.seen
        if s.url == CATALOGUE and "if-modified-since" in s.request.headers
    ]
    assert len(conditional_asks) == 1
    assert second.meta["lists"][0]["rows"] == 48
    assert (second.new_observations, second.meta["unchanged"]) == (0, 1)


async def test_an_era_index_that_fails_its_weekly_read_is_said(
    session, exhibitors_row, tmp_path
):
    """Nothing due, the sweep done: the weekly index read alone, failed —
    the run is partial and names it."""
    await exhibitors_row([ENTRY["uzbuild2027"]])
    clock = FakeClock(TODAY)
    await swept(session, "uzbuild", at=TODAY - timedelta(days=30))
    site = Site(clock, {"https://uzbuild.uz/en/exhibitors-list": httpx.Response(404)})
    report = await run(site, clock, tmp_path)
    assert report.status == "partial"
    row = await last_run(session)
    assert row.meta["lists"] == [
        {
            "show": "uzbuild",
            "year": None,
            "edition": None,
            "why": "index",
            "status": "failed",
            "rows": 0,
            "kept": 0,
            "foreign": 0,
            "skipped": 0,
            "pages": 0,
            "error": "http_404",
        }
    ]
    assert row.meta["incomplete"] == ["uzbuild:failed"]
