"""collectors/runner (03 §1.2, 07 T-05): a fake collector run writes
``collector_runs``, ``evidence``, ``observations`` and the heartbeat
``collector:<key>[.variant]``; a planted 403 streak pauses the source, ends the
run ``failed`` + ``error='host_blocked'`` and records ``collector_blocked``.

Postgres-backed. The network is ``tests/fakes.Site``; time is ``FakeClock``.
The ``directories`` row is seeded configuration: each test snapshots it and
puts it back (07 §1.0 — a restore written after the fact restores what it
found).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from clientscrapper.collectors import runner as runner_module
from clientscrapper.collectors.base import Budget, Collector, Observation
from clientscrapper.collectors.runner import build_budget, run_collector
from clientscrapper.db.models import (
    CollectorRun,
    Evidence,
    Heartbeat,
    RuntimeFlag,
    Source,
    UsageLog,
)
from clientscrapper.db.models import Observation as ObservationRow
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import costs, http, usage
from clientscrapper.services.http import BREAKER_PAUSE, PaidCall, RobotsDisallowed
from tests.fakes import ALLOW_ALL, TASHKENT, FakeClock, Site, fetcher_for

EX = "https://www.goldenpages.example.uz"
HTML = {"content-type": "text/html; charset=utf-8"}
KEPT = (
    "enabled",
    "paused_until",
    "paused_reason",
    "breaker_until",
    "breaker_reason",
    "last_run_at",
    "daily_page_budget",
    "daily_cost_cap_usd",
)
CARD = re.compile(rb'<li data-id="(\d+)">([^|<]+)\|([^<]+)</li>')


def listing(*cards: tuple[int, str, str]) -> bytes:
    items = "".join(f'<li data-id="{n}">{name}|{phone}</li>' for n, name, phone in cards)
    return f"<html><title>Katalog</title><ul>{items}</ul></html>".encode()


PAGE_1 = listing(
    (1, "Namuna Mebel MChJ", "+998 90 000 00 01"), (2, "Namuna Savdo", "90 000 00 02")
)
PAGE_2 = listing((3, "Namuna Tekstil XK", "+998 90 000 00 03"))


class Directory(Collector):
    """Two catalogue pages on a directory site, plus one API-shaped row."""

    key = "directories"
    family = "directory"
    default_cron = "0 23 * * 4"
    paths: tuple[str, ...] = ("/katalog/1", "/katalog/2")
    api_row = True

    @staticmethod
    def parse_listing(
        body: bytes, *, url: str, fetched_at: datetime
    ) -> list[Observation]:
        sha = hashlib.sha256(body).hexdigest()
        return [
            Observation(
                source="directories_goldenpages",
                source_ref=f"goldenpages:{m.group(1).decode()}",
                url=f"{url}#card-{m.group(1).decode()}",
                fetched_at=fetched_at,
                raw_sha256=sha,
                kind="company",
                company_name_raw=m.group(2).decode(),
                phones=[m.group(3).decode()],
                signal_types=["directory_wholesale"],
                excerpt=f"{m.group(2).decode()} — optom",
            )
            for m in CARD.finditer(body)
        ]

    async def collect(self, session, budget: Budget) -> list[Observation]:
        for path in self.paths:
            result = await self.fetcher.get(
                EX + path, source="directories_goldenpages", budget=budget
            )
            self.found += self.parse_listing(
                result.body, url=result.url, fetched_at=result.fetched_at
            )
        if self.api_row:
            self.found.append(
                Observation(
                    source="directories",
                    source_ref="uzts:300000004",
                    url="https://api.namuna.example.uz/members/300000004",
                    fetched_at=self.fetcher._wall(),
                    raw_sha256="",
                    kind="company",
                    company_name_raw="Namuna Import QK",
                    stir="300000004",
                    excerpt="Namuna Import QK, a'zo",
                )
            )
        return self.found


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def directories(session):
    """Enable the seeded row for the test and restore every column it may move."""
    async with SessionLocal() as s:
        row = await s.get(Source, "directories")
        assert row is not None
        snapshot = {name: getattr(row, name) for name in KEPT}
        await s.execute(
            sa.update(Source).where(Source.key == "directories").values(enabled=True)
        )
        await s.commit()
    yield
    async with SessionLocal() as s:
        await s.execute(
            sa.update(Source).where(Source.key == "directories").values(**snapshot)
        )
        await s.execute(sa.delete(RuntimeFlag).where(RuntimeFlag.key == "paused"))
        await s.commit()


def directory_site(clock: FakeClock, **extra) -> Site:
    def etagged(body: bytes, tag: str):
        def answer(request: httpx.Request) -> httpx.Response:
            if request.headers.get("if-none-match") == tag:
                return httpx.Response(304, headers={"etag": tag})
            return httpx.Response(200, headers={**HTML, "etag": tag}, content=body)

        return answer

    return Site(
        clock,
        {
            f"{EX}/robots.txt": ALLOW_ALL,
            f"{EX}/katalog/1": etagged(PAGE_1, '"p1"'),
            f"{EX}/katalog/2": etagged(PAGE_2, '"p2"'),
            **extra,
        },
    )


async def run(collector, fetcher, clock, tmp_path, **kwargs):
    return await run_collector(
        collector,
        fetcher=fetcher,
        now=clock.now(),
        clock=clock.now,
        evidence_root=tmp_path,
        **kwargs,
    )


async def runs(session) -> list[CollectorRun]:
    rows = await session.scalars(
        sa.select(CollectorRun)
        .order_by(CollectorRun.id)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def source_row(session) -> Source:
    row = await session.get(Source, "directories", populate_existing=True)
    assert row is not None
    return row


async def heartbeat(session, component: str) -> Heartbeat:
    row = await session.get(Heartbeat, component, populate_existing=True)
    assert row is not None
    return row


# --- the happy path -----------------------------------------------------------------


async def test_a_run_writes_the_three_tables_and_a_heartbeat(
    session, directories, clock, tmp_path
):
    site = directory_site(clock)
    start = clock.now()
    report = await run(Directory, fetcher_for(site), clock, tmp_path)

    assert report.status == "ok"
    [row] = await runs(session)
    assert (row.status, row.error, row.source_key) == ("ok", None, "directories")
    assert row.started_at == start and row.finished_at == clock.now()
    assert (row.requests, row.pages, row.http_403, row.http_429) == (3, 2, 0, 0)
    assert (row.new_evidence, row.new_observations) == (3, 4)
    assert (row.new_companies, row.new_signals, row.cost_usd) == (0, 0, Decimal("0"))
    assert row.meta == {
        "unchanged": 0,
        "skipped_robots": 0,
        "truncated": 0,
        "sources": {
            "directories_goldenpages": {"evidence": 2, "observations": 3},
            "directories": {"evidence": 1, "observations": 1},
        },
    }

    evidence = list(await session.scalars(sa.select(Evidence).order_by(Evidence.id)))
    assert [(e.url, e.source_key, e.collector_run_id) for e in evidence] == [
        (f"{EX}/katalog/1", "directories_goldenpages", row.id),
        (f"{EX}/katalog/2", "directories_goldenpages", row.id),
        ("https://api.namuna.example.uz/members/300000004", "directories", row.id),
    ]
    page1, page2, api = evidence
    assert page1.sha256 == hashlib.sha256(PAGE_1).hexdigest()
    assert (page1.source_ref, page2.source_ref, api.source_ref) == (
        None,  # a page with two cards names none of them
        "goldenpages:3",
        "uzts:300000004",
    )
    assert page1.meta == {"etag": '"p1"', "final_url": f"{EX}/katalog/1", "status": 200}
    assert (page1.title, page1.excerpt) == ("Katalog", "Namuna Mebel MChJ — optom")
    assert api.content_type == "application/json"
    assert (tmp_path / page1.raw_path).is_file()

    found = list(
        await session.scalars(sa.select(ObservationRow).order_by(ObservationRow.id))
    )
    assert [(o.evidence_id, o.ordinal, o.status, o.source_key) for o in found] == [
        (page1.id, 0, "new", "directories_goldenpages"),
        (page1.id, 1, "new", "directories_goldenpages"),
        (page2.id, 0, "new", "directories_goldenpages"),
        (api.id, 0, "new", "directories"),
    ]
    first = found[0].parsed
    assert (first["kind"], first["name_key"], first["source_ref"]) == (
        "directory_entry",
        "namuna mebel",
        "goldenpages:1",
    )
    assert first["phones"][0]["e164"] == "+998900000001"
    assert first["collector_version"] == "directories@1"
    assert found[3].parsed["company"]["stir"] == "300000004"

    beat = await heartbeat(session, "collector:directories")
    assert beat.last_seen_at == clock.now()
    assert beat.detail == {
        "phase": "end",
        "run_id": row.id,
        "status": "ok",
        "error": None,
        "requests": 3,
        "pages": 2,
        "http_403": 0,
        "http_429": 0,
        "new_evidence": 3,
        "new_observations": 4,
        "new_companies": 0,
        "new_signals": 0,
        "cost_usd": "0",
        "unchanged": 0,
        "skipped_robots": 0,
    }
    assert (await source_row(session)).last_run_at == start


async def test_a_rerun_over_unchanged_pages_writes_nothing_new(
    session, directories, clock, tmp_path
):
    site = directory_site(clock)
    fetcher = fetcher_for(site)
    await run(Directory, fetcher, clock, tmp_path)
    clock.advance(3600)
    report = await run(Directory, fetcher, clock, tmp_path)

    assert report.status == "ok"
    second = (await runs(session))[1]
    assert (second.new_evidence, second.new_observations) == (0, 0)
    assert second.meta["unchanged"] == 2  # both pages answered 304
    assert second.meta["unchanged_observations"] == 4
    conditional = [s for s in site.seen if "if-none-match" in s.request.headers]
    assert [s.path for s in conditional] == ["/katalog/1", "/katalog/2"]
    assert await session.scalar(sa.select(sa.func.count()).select_from(Evidence)) == 3


async def test_a_variant_beats_its_own_component(session, directories, clock, tmp_path):
    report = await run(
        Directory, fetcher_for(directory_site(clock)), clock, tmp_path, variant="full"
    )
    assert report.component == "collector:directories.full"
    [row] = await runs(session)
    assert row.meta["variant"] == "full"
    assert (await heartbeat(session, "collector:directories.full")).detail[
        "status"
    ] == "ok"
    assert await session.get(Heartbeat, "collector:directories") is None


# --- robots, duplicates, orphans ------------------------------------------------------


class WithDisallowed(Directory):
    async def collect(self, session, budget):
        with contextlib.suppress(RobotsDisallowed):
            await self.fetcher.get(EX + "/private/x", source="directories", budget=budget)
        return await super().collect(session, budget)


async def test_a_robots_refusal_is_counted_in_meta(session, directories, clock, tmp_path):
    site = directory_site(clock)
    site.add(
        f"{EX}/robots.txt",
        httpx.Response(200, text="User-agent: *\nDisallow: /private\n"),
    )
    await run(WithDisallowed, fetcher_for(site), clock, tmp_path)
    [row] = await runs(session)
    assert row.meta["skipped_robots"] == 1
    assert "/private/x" not in site.paths()
    assert row.status == "ok"


class Twice(Directory):
    paths = ("/katalog/2",)
    api_row = False

    async def collect(self, session, budget):
        found = await super().collect(session, budget)
        return [*found, found[0]]


async def test_the_same_observation_twice_in_a_run_is_written_once(
    session, directories, clock, tmp_path
):
    await run(Twice, fetcher_for(directory_site(clock)), clock, tmp_path)
    [row] = await runs(session)
    assert (row.new_observations, row.meta["duplicates"]) == (1, 1)


class Mirror(Directory):
    """The same bytes at a second URL with the same source_ref."""

    paths = ("/katalog/mirror",)
    api_row = False


class Mirror2(Mirror):
    paths = ("/katalog/mirror2",)


async def test_a_source_ref_and_sha_seen_within_24_hours_is_a_duplicate(
    session, directories, clock, tmp_path
):
    site = directory_site(
        clock,
        **{f"{EX}/katalog/mirror": httpx.Response(200, headers=HTML, content=PAGE_2)},
    )
    fetcher = fetcher_for(site)
    await run(Twice, fetcher, clock, tmp_path)
    clock.advance(3600)
    await run(Mirror, fetcher, clock, tmp_path)
    mirror = (await runs(session))[1]
    assert (mirror.new_evidence, mirror.new_observations) == (1, 0)
    assert mirror.meta["duplicates"] == 1

    # Older than 24 hours, the same finding (same source_ref, same bytes, a
    # third URL) is written again.
    await session.execute(
        sa.update(ObservationRow).values(created_at=clock.now() - timedelta(hours=25))
    )
    await session.commit()
    site.add(f"{EX}/katalog/mirror2", httpx.Response(200, headers=HTML, content=PAGE_2))
    clock.advance(3600)
    await run(Mirror2, fetcher, clock, tmp_path)
    third = (await runs(session))[2]
    assert (third.new_evidence, third.new_observations) == (1, 1)
    assert "duplicates" not in third.meta


class Orphans(Directory):
    paths = ()
    api_row = False

    async def collect(self, session, budget):
        base = {
            "url": "https://x.example.uz/a",
            "fetched_at": self.fetcher._wall(),
            "kind": "company",
        }
        return [
            Observation(
                source="directories", source_ref="a", raw_sha256="0" * 64, **base
            ),
            Observation(source="nowhere_known", source_ref="b", raw_sha256="", **base),
            Observation(
                source="erp_sync",
                source_ref="c",
                raw_sha256="",
                **{**base, "kind": "outcome"},
            ),
        ]


async def test_what_cannot_be_written_is_counted_not_written(
    session, directories, clock, tmp_path
):
    report = await run(Orphans, fetcher_for(directory_site(clock)), clock, tmp_path)
    assert report.status == "ok"
    [row] = await runs(session)
    assert row.meta["dropped"] == {
        "no_evidence": 1,
        "unknown_source": 1,
        "not_an_observation": 1,
    }
    assert (row.new_evidence, row.new_observations) == (0, 0)


# --- the breaker ----------------------------------------------------------------------


class Hammering(Directory):
    """Page 1 parses, then the site turns every request away."""

    async def collect(self, session, budget):
        result = await self.fetcher.get(
            EX + "/katalog/1", source="directories_goldenpages", budget=budget
        )
        self.found += self.parse_listing(
            result.body, url=result.url, fetched_at=result.fetched_at
        )
        while True:
            await self.fetcher.get(EX + "/blocked", source="directories", budget=budget)


async def test_a_planted_403_streak_pauses_the_source_and_fails_the_run(
    session, directories, clock, tmp_path
):
    site = directory_site(clock, **{f"{EX}/blocked": httpx.Response(403)})
    fetcher = fetcher_for(site)
    report = await run(Hammering, fetcher, clock, tmp_path)

    assert (report.status, report.error) == ("failed", "host_blocked")
    [row] = await runs(session)
    assert (row.status, row.error, row.http_403) == ("failed", "host_blocked", 5)
    assert row.meta["problem"] == "collector_blocked"
    assert row.meta["blocked_hosts"] == ["www.goldenpages.example.uz"]
    assert row.new_observations == 2  # what page 1 gave is kept

    source = await source_row(session)
    until = clock.now() + BREAKER_PAUSE
    reason = "host_blocked:www.goldenpages.example.uz"
    assert (source.paused_until, source.paused_reason) == (until, reason)
    assert (source.breaker_until, source.breaker_reason) == (until, reason)

    beat = await heartbeat(session, "collector:directories")
    assert (beat.detail["status"], beat.detail["error"], beat.detail["problem"]) == (
        "failed",
        "host_blocked",
        "collector_blocked",
    )

    # The next scheduled run is skipped while the pause holds.
    clock.advance(60)
    before = len(site.seen)
    again = await run(Directory, fetcher, clock, tmp_path)
    assert again.status == "skipped"
    assert (await runs(session))[1].meta == {"note": "paused"}
    assert len(site.seen) == before


class Swallowing(Hammering):
    """A collector that catches the breaker itself still ends the run failed."""

    async def collect(self, session, budget):
        with contextlib.suppress(http.HostPaused):
            await super().collect(session, budget)
        return self.found


async def test_a_breaker_the_collector_swallowed_still_fails_the_run(
    session, directories, clock, tmp_path
):
    site = directory_site(clock, **{f"{EX}/blocked": httpx.Response(403)})
    report = await run(Swallowing, fetcher_for(site), clock, tmp_path)
    assert (report.status, report.error) == ("failed", "host_blocked")
    assert (await source_row(session)).paused_reason == (
        "host_blocked:www.goldenpages.example.uz"
    )


# --- budgets ----------------------------------------------------------------------------


async def test_an_exhausted_page_budget_ends_partial_and_keeps_what_was_found(
    session, directories, clock, tmp_path
):
    await session.execute(
        sa.update(Source).where(Source.key == "directories").values(daily_page_budget=1)
    )
    await session.commit()
    site = directory_site(clock)
    report = await run(Directory, fetcher_for(site), clock, tmp_path)

    assert report.status == "partial"
    [row] = await runs(session)
    assert (row.status, row.pages, row.new_observations) == ("partial", 1, 2)
    assert row.meta["exhausted"] == "pages"
    assert "/katalog/2" not in site.paths()


async def test_the_budget_subtracts_todays_pages_and_spend(session, directories, clock):
    now = clock.now()
    await session.execute(
        sa.update(Source)
        .where(Source.key == "directories")
        .values(daily_page_budget=600, daily_cost_cap_usd=Decimal("1.0"))
    )
    session.add_all(
        [
            CollectorRun(
                source_key="directories",
                started_at=now - timedelta(hours=2),
                finished_at=now - timedelta(hours=1),
                status="ok",
                pages=150,
            ),
            CollectorRun(
                source_key="directories",
                started_at=now - timedelta(days=1),
                finished_at=now - timedelta(days=1),
                status="ok",
                pages=400,
            ),
            UsageLog(
                provider="other",
                operation="other",
                cost_usd=Decimal("0.30"),
                source_key="directories_goldenpages",
                created_at=now - timedelta(hours=1),
            ),
            UsageLog(
                provider="other",
                operation="other",
                cost_usd=Decimal("0.05"),
                source_key="directories",
                created_at=now - timedelta(minutes=5),
            ),
            UsageLog(
                provider="other",
                operation="other",
                cost_usd=Decimal("0.50"),
                source_key="directories",
                created_at=now - timedelta(days=1),
            ),
            UsageLog(
                provider="other",
                operation="other",
                cost_usd=Decimal("0"),
                model="refused:cost_cap_daily",
                source_key="directories",
                created_at=now - timedelta(minutes=1),
            ),
            UsageLog(
                provider="other",
                operation="other",
                cost_usd=Decimal("0.20"),
                source_key="maps_google",
                created_at=now - timedelta(minutes=1),
            ),
        ]
    )
    await session.commit()
    source = await source_row(session)
    budget = await build_budget(session, source, now=now, deadline=timedelta(hours=1))
    assert budget.pages == 450
    assert budget.usd == Decimal("0.65")
    assert budget.deadline == now + timedelta(hours=1)


class Paying(Directory):
    paths = ()
    api_row = False

    async def collect(self, session, budget):
        paid = PaidCall("serp_brave", Decimal("0.005"), lambda r: _Priced("0.004"))
        await self.fetcher.get(
            "https://api.search.brave.com/res/v1/web/search",
            source="search_brave",
            budget=budget,
            obey_robots=False,
            paid=paid,
        )
        return []


def _Priced(cost: str) -> usage.Priced:
    # The real type: costs.ask (the default gate) refuses a look-alike.
    return usage.Priced(cost_usd=Decimal(cost))


async def test_the_cost_gate_is_bound_to_the_run_and_its_cost_recorded(
    session, directories, clock, tmp_path
):
    await session.execute(
        sa.update(Source)
        .where(Source.key == "directories")
        .values(daily_cost_cap_usd=Decimal("0.1"))
    )
    await session.commit()
    bound: list[int] = []
    asked_at: list[datetime] = []

    def gate_for(gate_session, run_id):
        bound.append(run_id)

        async def gate(*, operation, est_usd, call, price, source_key, now):
            asked_at.append(now)
            result = await call()
            price(result)
            return result

        return gate

    site = Site(
        clock,
        {"https://api.search.brave.com/res/v1/web/search": httpx.Response(200, json={})},
    )
    report = await run(Paying, fetcher_for(site), clock, tmp_path, cost_gate_for=gate_for)
    [row] = await runs(session)
    assert (bound, asked_at) == ([row.id], [clock.now()])
    assert (report.status, row.cost_usd) == ("ok", Decimal("0.004000"))


async def test_by_default_a_run_pays_through_the_real_costs_ask(
    session, directories, clock, tmp_path
):
    # No cost_gate_for: the runner binds costs.ask itself (runner.ask_gate),
    # so the usage row names this run and its sub-source.
    await session.execute(
        sa.update(Source)
        .where(Source.key == "directories")
        .values(daily_cost_cap_usd=Decimal("0.1"))
    )
    await session.commit()
    site = Site(
        clock,
        {"https://api.search.brave.com/res/v1/web/search": httpx.Response(200, json={})},
    )
    report = await run(Paying, fetcher_for(site), clock, tmp_path)
    [row] = await runs(session)
    assert (report.status, row.cost_usd) == ("ok", Decimal("0.004000"))
    [usage] = await session.scalars(
        sa.select(UsageLog).where(UsageLog.operation != "other")
    )
    assert (
        usage.operation,
        usage.source_key,
        usage.collector_run_id,
        usage.cost_usd,
    ) == (
        "serp_brave",
        "search_brave",
        row.id,
        Decimal("0.004000"),
    )


async def test_by_default_a_run_at_the_daily_cap_ends_partial_and_sends_nothing(
    session, directories, clock, tmp_path
):
    await session.execute(
        sa.update(Source)
        .where(Source.key == "directories")
        .values(daily_cost_cap_usd=Decimal("0.1"))
    )
    session.add(
        UsageLog(
            provider="other",
            operation="other",
            cost_usd=Decimal("10"),
            created_at=clock.now(),
        )
    )
    await session.commit()
    costs.clear_refusals()
    site = Site(
        clock,
        {"https://api.search.brave.com/res/v1/web/search": httpx.Response(200, json={})},
    )
    try:
        report = await run(Paying, fetcher_for(site), clock, tmp_path)
    finally:
        costs.clear_refusals()
    [row] = await runs(session)
    assert (report.status, row.status, row.meta["exhausted"]) == (
        "partial",
        "partial",
        "cost_cap",
    )
    assert row.meta["refused"] == "cost_cap_daily"
    assert site.seen == []


class LateBrave(Directory):
    """Waits an hour on its own clock (pacing, a slow page), then pays."""

    paths = ()
    api_row = False
    wait = 3600.0
    url = "https://api.search.brave.com/res/v1/web/search"
    paid = PaidCall("serp_brave", Decimal("0.005"), lambda r: _Priced("0.004"))
    sub_source = "search_brave"

    async def collect(self, session, budget):
        await self.fetcher._sleep(self.wait)  # FakeClock.sleep: time moves on
        await self.fetcher.get(
            self.url,
            source=self.sub_source,
            budget=budget,
            obey_robots=False,
            paid=self.paid,
        )
        return []


class LateYandex(LateBrave):
    wait = 30 * 60.0
    url = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
    paid = PaidCall(
        "serp_yandex_night",
        Decimal("0"),
        lambda r: usage.Priced(cost_usd=Decimal("0"), units=Decimal(1)),
    )
    sub_source = "search_yandex"


async def _cap_directories(cap: str) -> None:
    async with SessionLocal() as s:
        await s.execute(
            sa.update(Source)
            .where(Source.key == "directories")
            .values(daily_cost_cap_usd=Decimal(cap))
        )
        await s.commit()


async def test_a_run_that_crosses_midnight_pays_from_the_new_days_cap(
    session, directories, tmp_path
):
    # Started 23:50 on a day whose $10 is spent; it pays at 00:50. The new
    # Tashkent day has its whole cap, and the row is that day's (04 §8).
    late = FakeClock(datetime(2026, 10, 24, 23, 50, tzinfo=TASHKENT))
    await _cap_directories("0.1")
    session.add(
        UsageLog(
            provider="other",
            operation="other",
            cost_usd=Decimal("10"),
            created_at=datetime(2026, 10, 24, 23, 0, tzinfo=TASHKENT),
        )
    )
    await session.commit()
    site = Site(late, {LateBrave.url: httpx.Response(200, json={})})
    costs.clear_refusals()
    try:
        report = await run(LateBrave, fetcher_for(site), late, tmp_path)
    finally:
        costs.clear_refusals()
    paid_at = datetime(2026, 10, 25, 0, 50, tzinfo=TASHKENT)
    assert late.now() == paid_at
    assert report.status == "ok" and len(site.seen) == 1
    [row] = await session.scalars(
        sa.select(UsageLog).where(UsageLog.operation == "serp_brave")
    )
    assert (row.created_at, row.model, row.cost_usd) == (
        paid_at,
        None,
        Decimal("0.004000"),
    )
    assert (await costs.read_ledger(session, paid_at)).day_usd == Decimal("0.004")


async def test_a_run_that_outlives_the_yandex_window_is_refused_at_the_call(
    session, directories, tmp_path
):
    # The window is [02:00, 10:00) Tashkent; a run started 09:59 that asks at
    # 10:29 asks outside it, however early the run began.
    late = FakeClock(datetime(2026, 10, 24, 9, 59, tzinfo=TASHKENT))
    site = Site(late, {LateYandex.url: httpx.Response(200, json={})})
    costs.clear_refusals()
    try:
        report = await run(LateYandex, fetcher_for(site), late, tmp_path)
    finally:
        costs.clear_refusals()
    assert report.status == "partial" and site.seen == []
    [row] = await runs(session)
    assert (row.meta["exhausted"], row.meta["refused"]) == (
        "refused",
        "op_window:serp_yandex_night",
    )
    [refusal] = await session.scalars(
        sa.select(UsageLog).where(UsageLog.operation == "serp_yandex_night")
    )
    assert (refusal.model, refusal.created_at) == (
        "refused:op_window:serp_yandex_night",
        datetime(2026, 10, 24, 10, 29, tzinfo=TASHKENT),
    )


class Classifying(Directory):
    """Page 1, then a classify through costs.ask itself (T-27's shape: one
    model call per seller, services/extraction), not through the Fetcher."""

    paths = ("/katalog/1",)
    api_row = False

    async def collect(self, session, budget):
        await super().collect(session, budget)
        await costs.ask(
            session,
            operation="classify",
            est_usd=Decimal("0.001"),
            call=self._never,
            price=lambda r: usage.Priced(cost_usd=Decimal("0.001")),
            now=self.fetcher._wall(),
        )
        return self.found

    @staticmethod
    async def _never() -> None:
        raise AssertionError("a refused call must not be made")


async def test_a_cap_met_inside_collect_ends_partial_and_keeps_what_was_found(
    session, directories, clock, tmp_path
):
    session.add(
        UsageLog(
            provider="anthropic",
            operation="verdict",
            cost_usd=Decimal("5.00"),
            created_at=clock.now(),
        )
    )
    await session.commit()
    costs.clear_refusals()
    try:
        report = await run(
            Classifying, fetcher_for(directory_site(clock)), clock, tmp_path
        )
    finally:
        costs.clear_refusals()
    assert (report.status, report.error) == ("partial", None)
    [row] = await runs(session)
    assert (row.status, row.error, row.new_observations) == ("partial", None, 2)
    assert (row.meta["exhausted"], row.meta["refused"]) == (
        "cost_cap",
        "cost_cap_family:anthropic",
    )


# --- skips and failures ----------------------------------------------------------------


async def test_a_disabled_source_is_skipped_without_a_request(
    session, directories, clock, tmp_path
):
    await session.execute(
        sa.update(Source).where(Source.key == "directories").values(enabled=False)
    )
    await session.commit()
    site = directory_site(clock)
    report = await run(Directory, fetcher_for(site), clock, tmp_path)
    assert report.status == "skipped"
    [row] = await runs(session)
    assert (row.status, row.meta, row.finished_at) == (
        "skipped",
        {"note": "source_disabled"},
        clock.now(),
    )
    assert site.seen == []
    assert await session.get(Heartbeat, "collector:directories") is None


async def test_the_global_pause_skips(session, directories, clock, tmp_path):
    session.add(
        RuntimeFlag(
            key="paused",
            value={"since": "2026-10-24", "reason": "test"},
            updated_by="owner:1",
        )
    )
    await session.commit()
    report = await run(Directory, fetcher_for(directory_site(clock)), clock, tmp_path)
    assert report.status == "skipped"
    assert (await runs(session))[0].meta == {"note": "global_pause"}


async def test_collectors_enabled_false_skips(
    session, directories, clock, tmp_path, monkeypatch
):
    from clientscrapper.collectors import runner

    monkeypatch.setattr(runner.settings, "collectors_enabled", False)
    report = await run(Directory, fetcher_for(directory_site(clock)), clock, tmp_path)
    assert (report.status, report.meta) == ("skipped", {"note": "collectors_disabled"})


class Broken(Directory):
    async def collect(self, session, budget):
        await super().collect(session, budget)
        raise ValueError("Namuna sahifa tanasi +998 90 000 00 01")


async def test_an_exception_fails_the_run_with_the_error_class_only(
    session, directories, clock, tmp_path
):
    report = await run(Broken, fetcher_for(directory_site(clock)), clock, tmp_path)
    assert (report.status, report.error) == ("failed", "ValueError")
    [row] = await runs(session)
    assert (row.status, row.error, row.new_observations, row.requests) == (
        "failed",
        "ValueError",
        0,
        3,
    )
    beat = await heartbeat(session, "collector:directories")
    assert (beat.detail["status"], beat.detail["error"]) == ("failed", "ValueError")


async def test_a_stale_running_row_is_closed_and_a_fresh_one_blocks(
    session, directories, clock, tmp_path
):
    now = clock.now()
    session.add(
        CollectorRun(
            source_key="directories",
            started_at=now - timedelta(hours=7),
            status="running",
        )
    )
    await session.commit()
    report = await run(Directory, fetcher_for(directory_site(clock)), clock, tmp_path)
    assert report.status == "ok"
    stale, done = await runs(session)
    assert (stale.status, stale.error, stale.finished_at) == ("failed", "abandoned", now)

    session.add(CollectorRun(source_key="directories", started_at=now, status="running"))
    await session.commit()
    blocked = await run(Directory, fetcher_for(directory_site(clock)), clock, tmp_path)
    assert (blocked.status, blocked.meta) == ("skipped", {"note": "already_running"})


# --- what reaches a row: no credential, no all-or-nothing -------------------------------

GRAPH = "https://graph.facebook.com/v21.0/17841400000000001"


class GraphApi(Directory):
    paths = ()
    api_row = False

    async def collect(self, session, budget):
        result = await self.fetcher.get(
            GRAPH,
            source="directories",
            budget=budget,
            obey_robots=False,
            params={"fields": "biography,website", "access_token": "EAAB-SECRET-1"},
        )
        return [
            Observation(
                source="directories",
                source_ref="ig:namuna_01",
                url=result.url,
                fetched_at=result.fetched_at,
                raw_sha256=result.sha256,
                kind="company",
                company_name_raw="Namuna Savdo MChJ",
                excerpt="Namuna Savdo MChJ optom",
            )
        ]


async def test_an_api_key_in_the_query_never_reaches_a_row(
    session, directories, clock, tmp_path
):
    answer = httpx.Response(200, json={"biography": "Namuna Savdo MChJ optom"})
    site = directory_site(clock, **{GRAPH: answer})
    report = await run(GraphApi, fetcher_for(site), clock, tmp_path)
    assert report.status == "ok"
    clean = f"{GRAPH}?fields=biography%2Cwebsite"
    [ev] = (await session.scalars(sa.select(Evidence))).all()
    [obs] = (await session.scalars(sa.select(ObservationRow))).all()
    assert (ev.url, ev.meta["final_url"], obs.parsed["url"]) == (clean, clean, clean)
    dumped = await session.scalar(
        sa.select(sa.func.string_agg(sa.cast(Evidence.meta, sa.Text), "")).select_from(
            Evidence
        )
    )
    assert "SECRET" not in ev.url + dumped + str(obs.parsed)


LONG = "/katalog/long?" + "&".join(f"utm_{i}=x" for i in range(400))


class LongUrl(Directory):
    paths = ("/katalog/1", LONG)
    api_row = False


async def test_a_url_no_row_can_hold_is_dropped_and_the_rest_kept(
    session, directories, clock, tmp_path
):
    page = listing((9, "Namuna Long MChJ", "+998 90 000 00 09"))
    long_page = httpx.Response(200, headers=HTML, content=page)
    site = directory_site(clock, **{f"{EX}/katalog/long": long_page})
    report = await run(LongUrl, fetcher_for(site), clock, tmp_path)
    assert (report.status, report.error) == ("ok", None)
    [row] = await runs(session)
    assert (row.new_evidence, row.new_observations) == (1, 2)
    assert row.meta["dropped"] == {"url_too_long": 1}
    [ev] = (await session.scalars(sa.select(Evidence))).all()
    assert [p.name for p in tmp_path.rglob("*.gz")] == [ev.raw_path.split("/")[1]]


class NulGoods(Directory):
    """Page 2 yields rows whose goods text, source_ref and excerpt carry a NUL
    character (a page with binary junk in it), and a volume of «nan». Neither
    text nor jsonb holds a NUL, and NaN is not JSON: before they were made
    storable, one such row cost the page all its observations."""

    api_row = False

    @staticmethod
    def parse_listing(body, *, url, fetched_at):
        found = Directory.parse_listing(body, url=url, fetched_at=fetched_at)
        if url.endswith("/katalog/2"):
            for obs in found:
                obs.goods_text = "mato \x00 furnitura"
                obs.excerpt = "Namuna\x00 Tekstil"
                obs.attrs = {"volume_m3": "nan", "ratio": float("inf")}
        return found


async def test_nul_characters_and_nan_are_made_storable_and_the_rows_kept(
    session, directories, clock, tmp_path
):
    report = await run(NulGoods, fetcher_for(directory_site(clock)), clock, tmp_path)
    assert (report.status, report.error) == ("ok", None)
    [row] = await runs(session)
    assert (row.new_evidence, row.new_observations) == (2, 3)
    assert "dropped" not in row.meta
    page2 = await session.scalar(
        sa.select(Evidence).where(Evidence.url == f"{EX}/katalog/2")
    )
    assert page2 is not None and page2.excerpt == "Namuna Tekstil"
    parsed = (
        await session.scalars(
            sa.select(ObservationRow.parsed).where(ObservationRow.evidence_id == page2.id)
        )
    ).one()
    assert parsed["company"]["goods_text"] == "mato  furnitura"
    assert parsed["hints"]["volume_m3"] is None
    assert parsed["hints"]["volume_basis"] == "none"
    assert parsed["hints"]["collector"]["attrs"] == {"volume_m3": "nan", "ratio": None}


async def test_a_nul_in_the_page_body_reaches_no_column(
    session, directories, clock, tmp_path
):
    junk = PAGE_2.replace(b"<ul>", b"<p>\x00\x00 binary</p><ul>")
    site = directory_site(
        clock, **{f"{EX}/katalog/2": httpx.Response(200, headers=HTML, content=junk)}
    )

    class NoExcerpt(Directory):
        api_row = False

        @staticmethod
        def parse_listing(body, *, url, fetched_at):
            found = Directory.parse_listing(body, url=url, fetched_at=fetched_at)
            for obs in found:
                obs.excerpt = ""  # the store quotes the page itself
            return found

    report = await run(NoExcerpt, fetcher_for(site), clock, tmp_path)
    assert report.status == "ok"
    [row] = await runs(session)
    assert (row.new_evidence, row.new_observations) == (2, 3)
    page2 = await session.scalar(
        sa.select(Evidence).where(Evidence.url == f"{EX}/katalog/2")
    )
    assert page2 is not None and "\x00" not in page2.excerpt
    assert page2.excerpt.startswith("Katalog binary")


async def test_a_row_the_database_refuses_costs_that_row_not_its_page(
    session, directories, clock, tmp_path, monkeypatch
):
    # Whatever the normaliser lets through that the database still refuses:
    # one observation of page 1's two, here an unstorable tree.
    real = runner_module.to_observation_json

    def one_bad(obs, **kwargs):
        parsed = real(obs, **kwargs)
        if obs.source_ref == "goldenpages:2":
            parsed["text"]["excerpt"] = "\x00"
        return parsed

    monkeypatch.setattr(runner_module, "to_observation_json", one_bad)
    report = await run(Directory, fetcher_for(directory_site(clock)), clock, tmp_path)
    assert (report.status, report.error) == ("partial", None)
    [row] = await runs(session)
    assert (row.status, row.new_evidence, row.new_observations) == ("partial", 3, 3)
    assert row.meta["dropped"] == {"bad_observation": 1}
    refs = (await session.scalars(sa.select(ObservationRow.parsed))).all()
    assert sorted(p["source_ref"] for p in refs) == [
        "goldenpages:1",
        "goldenpages:3",
        "uzts:300000004",
    ]
    # Every page kept its evidence file: one per page, the API row's too.
    assert len(list(tmp_path.rglob("*.gz"))) == 3


# --- a run that does not end by itself --------------------------------------------------


class Slow(Directory):
    paths = ()
    api_row = False
    started: asyncio.Event | None = None

    async def collect(self, session, budget):
        assert self.started is not None
        self.started.set()
        await asyncio.sleep(3600)
        return []


async def test_a_cancelled_run_closes_its_row_and_the_next_run_goes(
    session, directories, clock, tmp_path
):
    Slow.started = asyncio.Event()
    task = asyncio.ensure_future(
        run(Slow, fetcher_for(directory_site(clock)), clock, tmp_path)
    )
    await Slow.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [cancelled] = await runs(session)
    assert (cancelled.status, cancelled.error) == ("failed", "cancelled")
    assert cancelled.finished_at is not None

    clock.advance(3 * 3600)  # the next hourly job, well inside the old stale window
    report = await run(Directory, fetcher_for(directory_site(clock)), clock, tmp_path)
    assert report.status == "ok"


async def test_a_running_row_is_stale_by_its_own_deadline(
    session, directories, clock, tmp_path
):
    now = clock.now()
    long_job = CollectorRun(
        source_key="directories",
        started_at=now - timedelta(hours=3),
        status="running",
        meta={"deadline_at": (now + timedelta(hours=1)).isoformat()},
    )
    session.add(long_job)
    await session.commit()
    blocked = await run(Directory, fetcher_for(directory_site(clock)), clock, tmp_path)
    assert (blocked.status, blocked.meta) == ("skipped", {"note": "already_running"})

    clock.advance(timedelta(hours=1, minutes=31).total_seconds())
    report = await run(Directory, fetcher_for(directory_site(clock)), clock, tmp_path)
    assert report.status == "ok"
    first = (await runs(session))[0]
    assert (first.status, first.error) == ("failed", "abandoned")
