"""services/enrich (04 §3, 07 T-24): the chain E0–E6 over fakes.

Every page comes from ``tests/fakes.Site`` (a 404 for anything it was not
given — no request leaves the test): the registry pages are the recorded
orginfo files, the company sites and the Telegram public page are the
SYNTHETIC ones of ``tests/fixtures/company_sites/``. E5 and E6 have no
built client (T-25, T-26, T-33): fakes stand for them behind the same
protocols, and E5's calls go through the real ``costs.ask``.

The stops S1–S4, the per-company $0.10 cap, the 1 h / 24 h / 7 d schedule,
a budget refusal moving the attempt to tomorrow 07:00, the trail in
``score_breakdown.enrichment[]``, E3 through the Fetcher and the reader's
queue, E4's registry hit (``ent_legal_confirmed`` and one decision maker
with its legal basis), the ``/stir`` re-run, the warm re-ask of an expired
handoff card and ``companies.sectors``. ``sources`` is seeded
configuration: every row a test changes is put back.
"""

from __future__ import annotations

import ast
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from clientscrapper.collectors.base import Observation as Seen
from clientscrapper.config import settings
from clientscrapper.db.models import (
    CollectorRun,
    Company,
    Contact,
    Evidence,
    LeadCandidate,
    LeadReview,
    ModelJob,
    Observation,
    RuntimeFlag,
    Signal,
    Source,
    UsageLog,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import candidates, chats, costs, enrich, owner, registry
from clientscrapper.services.http import Fetcher, PageBudget, PaidCall, UnmeteredCall
from clientscrapper.services.normalise import to_observation_json
from clientscrapper.services.usage import Priced
from tests.ast_tools import PACKAGE, imported_modules, tree_of
from tests.fakes import ALLOW_ALL, FakeClock, Site, fetcher_for
from tests.gate_fixtures import NOW, candidate, contact, firm, phone, seq, signal

FIXTURES = Path(__file__).parent / "fixtures"
SITES = FIXTURES / "company_sites"
REGISTRIES = FIXTURES / "registries"
SITE = "https://namuna-tekstil.example.uz"
CARD = "https://orginfo.uz/ru/organization/cf68f132a2f4/"
SEARCH = "https://orginfo.uz/ru/search/organizations/"
SOURCE_FIELDS = ("enabled", "paused_until", "config", "daily_page_budget")


def site_page(name: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=(SITES / f"{name}_synthetic.html").read_bytes(),
        headers={"content-type": "text/html; charset=utf-8"},
    )


def registry_page(name: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=(REGISTRIES / f"{name}.html").read_bytes(),
        headers={"content-type": "text/html; charset=utf-8"},
    )


@pytest.fixture(autouse=True)
async def kept_sources() -> AsyncIterator[None]:
    """The rows enrichment reads (its two and the fake finder's
    ``serp_layer``), snapshotted and restored (07 §1.0)."""
    keys = (enrich.SITE_SOURCE, enrich.REGISTRY_SOURCE, "serp_layer")
    costs.clear_refusals()
    saved: dict[str, dict[str, Any]] = {}
    with contextlib.suppress(Exception):
        async with SessionLocal() as s:
            for key in keys:
                row = await s.get(Source, key)
                if row is not None:
                    saved[key] = {f: getattr(row, f) for f in SOURCE_FIELDS}
    yield
    costs.clear_refusals()
    if saved:
        async with SessionLocal() as s, s.begin():
            for key, values in saved.items():
                await s.execute(
                    sa.update(Source).where(Source.key == key).values(**values)
                )


async def set_source(session, key: str, **values: Any) -> None:
    await session.execute(sa.update(Source).where(Source.key == key).values(**values))
    await session.commit()


@dataclass
class World:
    site: Site
    fetcher: Fetcher
    tools: enrich.Tools

    async def run(self, now: datetime = NOW, **kwargs: Any) -> enrich.EnrichReport:
        self.site.clock.start = now
        self.site.clock.t = 0.0
        return await enrich.enrich_batch(self.tools, now=now, clock=lambda: now, **kwargs)

    def urls(self, host: str | None = None) -> list[str]:
        return [
            s.url for s in self.site.seen if host is None or s.request.url.host == host
        ]


@contextlib.asynccontextmanager
async def world(
    tmp_path: Path, routes: dict[str, Any] | None = None, **tools: Any
) -> AsyncIterator[World]:
    clock = FakeClock(NOW)
    site = Site(
        clock,
        {
            f"{SITE}/robots.txt": ALLOW_ALL,
            "https://t.me/robots.txt": ALLOW_ALL,
            "https://orginfo.uz/robots.txt": registry_page("orginfo_robots"),
            **(routes or {}),
        },
    )
    async with fetcher_for(site) as fetcher:
        yield World(
            site,
            fetcher,
            enrich.Tools(fetcher=fetcher, evidence_root=tmp_path, **tools),
        )


async def enriching(
    session,
    company: Company,
    *,
    attempts: int = 0,
    due: datetime = NOW,
    score: int = 50,
    **fields: Any,
) -> LeadCandidate:
    row = LeadCandidate(
        company_id=company.id,
        state=fields.pop("state", "enriching"),
        score=score,
        tier="hot" if score >= 60 else "warm",
        enrich_attempts=attempts,
        next_enrich_at=due,
        last_scored_at=NOW - timedelta(hours=1),
        **fields,
    )
    session.add(row)
    await session.flush()
    return row


async def reload(session, row):
    found = await session.get(type(row), row.id, populate_existing=True)
    assert found is not None
    return found


def steps(cand: LeadCandidate) -> list[tuple[str, str]]:
    return [(e["step"], e["result"]) for e in cand.score_breakdown["enrichment"]]


# --- S1: a phone and ≥ 60 --------------------------------------------------------


async def test_s1_a_site_phone_makes_it_hot_and_the_chain_stops(
    session, tmp_path, model_api
):
    company = await firm(
        session, "Namuna Tekstil Savdo", website_domain="namuna-tekstil.example.uz"
    )
    await signal(session, company)  # vol_container 40 + recency 10
    await signal(session, company, "cn_origin")  # + 20: 70 with no phone → warm
    cand = await enriching(session, company, score=70)
    await session.commit()
    routes = {
        f"{SITE}/": site_page("site_landing"),
        f"{SITE}/kontakty": site_page("site_contacts"),
    }
    async with world(tmp_path, routes) as w:
        report = await w.run()
    cand = await reload(session, cand)
    assert (cand.state, cand.tier, cand.enrich_attempts) == ("review", "hot", 1)
    assert report.as_dict() == {
        "reasked": 0,
        "reruns": 0,
        "attempts": 1,
        "deferred": 0,
        "failed": 0,
        "to_review": 1,
    }
    # E0 → E2 (landing + contacts) and then S1: no E3, no E4.
    assert steps(cand) == [("E0", "ok"), ("E1", "ok"), ("E2", "ok"), ("E2", "ok")]
    trail = cand.score_breakdown["enrichment"]
    assert {e["attempt"] for e in trail} == {1}
    assert all(set(e) >= {"step", "at", "cost_usd", "result"} for e in trail)
    assert trail[2]["found"]["phones"] == 1
    assert w.urls() == [f"{SITE}/robots.txt", f"{SITE}/", f"{SITE}/kontakty"]
    phones = (
        await session.execute(
            sa.select(
                Contact.phone, Contact.tier, Contact.source_key, Contact.legal_basis
            )
            .where(Contact.company_id == company.id)
            .order_by(Contact.phone)
        )
    ).all()
    assert phones == [
        ("+998712000032", "company", "enrich_site", "public_source"),
        ("+998900000031", "company", "enrich_site", "public_source"),
    ]
    stored = (
        await session.scalars(
            sa.select(Evidence.url)
            .where(Evidence.source_key == "enrich_site")
            .order_by(Evidence.id)
        )
    ).all()
    assert stored == [f"{SITE}/", f"{SITE}/kontakty"]
    company = await reload(session, company)
    assert company.stir == "300000031"  # the footer's STIR, carried
    run = await session.scalar(
        sa.select(CollectorRun).where(CollectorRun.source_key == "enrich_site")
    )
    assert run is not None and (run.pages, run.new_evidence, run.status) == (2, 2, "ok")
    # No model step in E0–E6: nothing reached the model (and so nothing
    # unpseudonymised could).
    assert model_api.requests == []


async def test_a_hot_candidate_gate_1_sent_here_walks_on_until_the_site_makes_it_b2b(
    session, tmp_path
):
    """Gate 1 routes a hot candidate with no B2B evidence to enrichment (T-24
    carried): it has its phone and its score, so S1 must not end the walk
    while gate 1's hold is what decides the card — the site's legal form is
    what the walk is for."""
    company = await firm(
        session, "Namuna Uch", legal_form=None, website_domain="namuna-tekstil.example.uz"
    )
    lead = await contact(session, company, phone(60))
    await signal(session, company)
    await signal(session, company, "cn_origin")
    cand = await candidate(
        session, company, state="scored", lead=lead, last_scored_at=None
    )
    await session.commit()
    await candidates.score_batch(now=NOW)
    cand = await reload(session, cand)
    assert (cand.state, cand.tier) == ("enriching", "hot")
    assert cand.score_breakdown["gates"]["is_b2b"] == "fail:not_b2b"
    routes = {
        f"{SITE}/": site_page("site_landing"),
        f"{SITE}/kontakty": site_page("site_contacts"),
    }
    async with world(tmp_path, routes) as w:
        await w.run(cand.next_enrich_at)
    cand = await reload(session, cand)
    company = await reload(session, company)
    assert company.legal_form == "MChJ"
    assert ("E2", "ok") in steps(cand)
    assert cand.state == "review"
    assert cand.score_breakdown["gates"]["is_b2b"] == "pass:legal_form"
    assert w.urls()[:2] == [f"{SITE}/robots.txt", f"{SITE}/"]


# --- the schedule and S4 ---------------------------------------------------------------


async def test_attempts_are_1h_24h_7d_apart_and_the_third_holds_with_no_timer(
    session, tmp_path
):
    company = await firm(session)
    await signal(session, company)  # 50: warm, nothing to find
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path) as w:
        await w.run(NOW)
        cand = await reload(session, cand)
        assert (cand.state, cand.enrich_attempts) == ("enriching", 1)
        assert cand.next_enrich_at == NOW + timedelta(hours=24)
        assert steps(cand) == [
            ("E0", "ok"),
            ("E1", "ok"),
            ("E2", "skipped:no_site"),
            ("E3", "skipped:no_channel"),
            ("E4", "skipped:source_disabled"),
            ("E5", "not_configured"),
            ("E6", "not_configured"),
        ]
        # Not due yet: nothing happens.
        await w.run(NOW + timedelta(hours=23))
        assert (await reload(session, cand)).enrich_attempts == 1
        second = NOW + timedelta(hours=24)
        await w.run(second)
        cand = await reload(session, cand)
        assert (cand.state, cand.enrich_attempts) == ("enriching", 2)
        assert cand.next_enrich_at == second + timedelta(days=7)
        third = second + timedelta(days=7)
        await w.run(third)
    cand = await reload(session, cand)
    assert (cand.state, cand.enrich_attempts, cand.hold_until) == ("hold", 3, None)
    assert [e["attempt"] for e in cand.score_breakdown["enrichment"]].count(3) == 7
    assert w.urls() == []  # nothing to fetch for a company with no site/channel
    # And the first attempt was an hour after entering enrichment (02 §2.9).
    assert enrich.SCHEDULE[0] == candidates.FIRST_ENRICH_DELAY == timedelta(hours=1)


async def test_s4_phone_less_with_a_username_is_the_handoff_card(session, tmp_path):
    company = await firm(session)
    await signal(session, company)
    await signal(session, company, "cn_origin")  # 70, no phone
    await contact(
        session,
        company,
        tier="person",
        telegram_username="namuna_rustam",
        full_name="Rustam",
        role="direktor",
    )
    cand = await enriching(session, company, attempts=2, score=70)
    await session.commit()
    async with world(tmp_path) as w:
        await w.run()
    cand = await reload(session, cand)
    assert (cand.state, cand.tier, cand.enrich_attempts) == ("review", "warm", 3)
    assert cand.score_breakdown["gates"]["no_phone"] is True
    assert cand.score is not None and cand.score >= 60


async def test_s4_without_a_username_holds(session, tmp_path):
    company = await firm(session)
    await signal(session, company)
    await signal(session, company, "cn_origin")
    cand = await enriching(session, company, attempts=2, score=70)
    await session.commit()
    async with world(tmp_path) as w:
        await w.run()
    cand = await reload(session, cand)
    assert (cand.state, cand.hold_until) == ("hold", None)
    assert "no_phone" not in cand.score_breakdown["gates"]


async def test_s4_a_phone_but_under_60_after_the_third_holds(session, tmp_path):
    company = await firm(session)
    await signal(session, company)
    await contact(session, company, phone(41))
    cand = await enriching(session, company, attempts=2, score=56)
    await session.commit()
    async with world(tmp_path) as w:
        await w.run()
    cand = await reload(session, cand)
    assert (cand.state, cand.hold_until, cand.enrich_attempts) == ("hold", None, 3)


# --- S2: excluded on the way ------------------------------------------------------------


async def test_s2_a_cargo_site_excludes_and_the_chain_stops(session, tmp_path):
    # A neutral name: only the site's own words make it a competitor.
    company = await firm(
        session, "Namuna Savdo", website_domain="namuna-tekstil.example.uz"
    )
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path, {f"{SITE}/": site_page("site_cargo")}) as w:
        await w.run()
    cand = await reload(session, cand)
    assert (cand.state, cand.exclude_reason) == ("excluded", "competitor")
    assert [s for s, _ in steps(cand)] == ["E0", "E1", "E2"]
    assert (await reload(session, company)).is_competitor is True


async def test_s2_a_chinese_site_title_is_gate_3_s_supplier(session, tmp_path):
    # enrich_site's name_raw is the title as written: gate 3 reads its CJK.
    yiwu = "https://namuna-yiwu.example.cn"
    company = await firm(session, "Namuna Yiwu", website_domain="namuna-yiwu.example.cn")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    routes = {
        f"{yiwu}/robots.txt": ALLOW_ALL,
        f"{yiwu}/": site_page("site_cn_title"),
    }
    async with world(tmp_path, routes) as w:
        await w.run()
    cand = await reload(session, cand)
    assert (cand.state, cand.exclude_reason) == ("excluded", "supplier_cn")
    assert cand.score_breakdown["gates"]["not_supplier"] == "fail:site_title"


# --- S3: the company cap, and the budget ----------------------------------------------


BRAVE = "https://api.search.brave.com/res/v1/web/search"


@dataclass
class FakeFinder:
    """An E5 finder (T-25/T-33 build the real ones), written the way every
    metered collector is: one Brave request declaring its ``PaidCall`` on
    the Fetcher it is handed, under ``serp_layer``'s budget. Finds nothing
    unless given ``pages``."""

    est: Decimal
    key: str = "fake_serp"
    source_key: str = "serp_layer"
    calls: int = 0
    pages: list[enrich.Page] = field(default_factory=list)
    # The Fetcher it was handed is gated for the company it was asked about.
    gated: list[bool] = field(default_factory=list)

    def estimate(self, query: enrich.CompanyQuery) -> Decimal:
        return self.est

    async def find(
        self, query: enrich.CompanyQuery, *, fetcher: Fetcher, budget: PageBudget
    ) -> list[enrich.Page]:
        self.calls += 1
        gate = fetcher._cost_gate
        self.gated.append(
            isinstance(gate, enrich.CompanyGate) and gate.company_id == query.company_id
        )
        await fetcher.get(
            BRAVE,
            params={"q": query.name},
            source="search_brave",
            budget=budget,
            obey_robots=False,
            paid=PaidCall(
                operation="serp_brave",
                est_usd=self.est,
                price=lambda result: Priced(cost_usd=self.est, units=Decimal(1)),
            ),
        )
        return list(self.pages)


def brave_ok() -> dict[str, httpx.Response]:
    return {BRAVE: httpx.Response(200, json={"web": {"results": []}})}


@dataclass
class FakeReader:
    """E6 (T-26 builds Business Discovery): free, records its calls."""

    key: str = "fake_ig"
    handles: list[str] = field(default_factory=list)
    business: bool = True

    async def read(
        self, handle: str, *, fetcher: Fetcher, budget: PageBudget
    ) -> list[enrich.Page] | None:
        self.handles.append(handle)
        return [] if self.business else None


async def test_s3_paid_steps_stop_at_ten_cents_and_the_free_ones_run_on(
    session, tmp_path
):
    await set_source(session, "serp_layer", enabled=True)
    company = await firm(session, "Namuna Mebel Savdo", instagram="namuna.mebel")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    finders = [FakeFinder(Decimal("0.04")) for _ in range(3)]
    reader = FakeReader()
    async with world(tmp_path, brave_ok(), finders=finders, profiles=reader) as w:
        await w.run()
    cand = await reload(session, cand)
    e5 = [e for e in cand.score_breakdown["enrichment"] if e["step"] == "E5"]
    assert [e["result"] for e in e5] == ["ok", "ok", "company_cap"]
    assert [e["cost_usd"] for e in e5] == [0.04, 0.04, 0.0]
    # Every paid request went through costs.ask (the Fetcher's PaidCall
    # through the company's gate), and the ledger names the company and the
    # sub-source (04 §3 S3's SUM by company_id).
    assert [f.calls for f in finders] == [1, 1, 0]
    assert [f.gated for f in finders] == [[True], [True], []]
    assert len(w.urls("api.search.brave.com")) == 2
    rows = (
        await session.execute(
            sa.select(UsageLog.operation, UsageLog.source_key, UsageLog.cost_usd).where(
                UsageLog.company_id == company.id
            )
        )
    ).all()
    assert sorted(rows) == [("serp_brave", "search_brave", Decimal("0.04"))] * 2
    spent = await costs.spend(session, since=enrich.SPEND_SINCE, company_id=company.id)
    assert spent.usd == Decimal("0.08")
    assert reader.handles == ["namuna.mebel"]  # E6, free, still asked
    assert cand.enrich_attempts == 1  # the cap is no deferral
    # The finder's pages and dollars are serp_layer's: its run row says so.
    run = await session.scalar(
        sa.select(CollectorRun).where(CollectorRun.source_key == "serp_layer")
    )
    assert run is not None and (run.pages, run.cost_usd) == (2, Decimal("0.08"))


async def test_e5_a_finder_whose_source_is_off_or_resting_is_not_asked(
    session, tmp_path
):
    """A finder spends its own row's budget (``serp_layer``): the owner's
    /manba off, or the row resting, keeps it from spending at all."""
    company = await firm(session, "Namuna Mebel Savdo")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    finder = FakeFinder(Decimal("0.01"))
    async with world(tmp_path, brave_ok(), finders=[finder]) as w:
        await w.run()  # serp_layer is seeded off
        await set_source(
            session,
            "serp_layer",
            enabled=True,
            paused_until=NOW + timedelta(days=2),
        )
        cand = await reload(session, cand)
        await w.run(cand.next_enrich_at)
    cand = await reload(session, cand)
    e5 = [r for step, r in steps(cand) if step == "E5"]
    assert e5 == ["skipped:source_disabled", "skipped:paused"]
    assert finder.calls == 0 and w.urls("api.search.brave.com") == []


async def test_s3_a_company_at_ten_cents_gets_no_paid_call_whatever_the_estimate(
    session, tmp_path
):
    """S3 is checked per request, by the company's own gate: an estimate of
    $0 does not let a call past a company already at $0.10, and a request
    that would cross it is refused before it is sent."""
    await set_source(session, "serp_layer", enabled=True)
    at_cap = await firm(session, "Namuna Mebel Savdo")
    near = await firm(session, "Namuna Mebel Bozor")
    for company, spent in ((at_cap, "0.10"), (near, "0.08")):
        await signal(session, company)
        session.add(
            UsageLog(
                provider="brave",
                operation="serp_brave",
                cost_usd=Decimal(spent),
                company_id=company.id,
                units=Decimal(1),
            )
        )
    first = await enriching(session, at_cap)
    second = await enriching(session, near, due=NOW + timedelta(seconds=1))
    await session.commit()

    @dataclass
    class Understated(FakeFinder):
        def estimate(self, query: enrich.CompanyQuery) -> Decimal:
            return Decimal("0")

    finder = Understated(Decimal("0.05"))
    async with world(tmp_path, brave_ok(), finders=[finder]) as w:
        await w.run(NOW + timedelta(seconds=1))
    assert [r for st, r in steps(await reload(session, first)) if st == "E5"] == [
        "company_cap"
    ]
    assert [r for st, r in steps(await reload(session, second)) if st == "E5"] == [
        "company_cap"
    ]
    assert finder.calls == 1  # asked for the second, refused at its request
    assert w.urls("api.search.brave.com") == []
    for company, spent in ((at_cap, "0.10"), (near, "0.08")):
        total = await costs.spend(session, since=enrich.SPEND_SINCE, company_id=company.id)
        assert total.usd == Decimal(spent)


async def test_the_company_gate_refuses_a_paid_call_that_names_no_company():
    gate = enrich.CompanyGate(SessionLocal)

    async def never() -> Any:
        raise AssertionError("sent")

    with pytest.raises(UnmeteredCall):
        await gate(
            operation="serp_brave",
            est_usd=Decimal("0.01"),
            call=never,
            price=lambda result: Priced(cost_usd=Decimal("0.01"), units=Decimal(1)),
            source_key="search_brave",
            now=NOW,
        )


async def test_e5_keeps_only_the_cards_whose_name_agrees(session, tmp_path):
    """A directory answer lists several firms: only the one whose name (and
    city, when both say one) is the company's is resolved into it."""
    await set_source(session, "serp_layer", enabled=True)
    company = await firm(session, "Namuna Mebel Savdo", city="Toshkent")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    url = "https://catalog.example.uz/q?namuna"

    def card(name: str, number: str, city: str | None) -> Seen:
        return Seen(
            source="serp_layer",
            source_ref=f"fake:{number}",
            url=url,
            fetched_at=NOW,
            raw_sha256="",
            kind="company",
            company_name_raw=name,
            phones=[number],
            city=city,
        )

    finder = FakeFinder(
        Decimal("0.01"),
        pages=[
            enrich.Page(
                url=url,
                body=b"namuna",
                content_type="text/plain",
                fetched_at=NOW,
                observations=(
                    card("Namuna Mebel Savdo", "+998901110001", "Toshkent"),
                    card("Boshqa Mebel", "+998901110002", "Toshkent"),
                    card("Namuna Mebel Savdo", "+998901110003", "Buxoro"),
                ),
                source_key="serp_layer",
            )
        ],
    )
    async with world(tmp_path, brave_ok(), finders=[finder]) as w:
        await w.run()
    phones = set(
        await session.scalars(
            sa.select(Contact.phone).where(Contact.company_id == company.id)
        )
    )
    assert phones == {"+998901110001"}
    assert ("E5", "ok") in steps(await reload(session, cand))


async def test_a_refused_paid_call_retries_tomorrow_at_seven_and_is_not_counted(
    session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "daily_cost_cap_usd", Decimal("0.01"))
    await set_source(session, "serp_layer", enabled=True)
    company = await firm(session, "Namuna Mebel Savdo")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    finder = FakeFinder(Decimal("0.04"))
    async with world(tmp_path, brave_ok(), finders=[finder]) as w:
        report = await w.run()
    cand = await reload(session, cand)
    assert w.urls("api.search.brave.com") == []  # refused before it was sent
    assert ("E5", "budget:cost_cap_daily") in steps(cand)
    assert (cand.state, cand.enrich_attempts) == ("enriching", 0)
    assert cand.next_enrich_at == datetime(2026, 10, 28, 7, 0, tzinfo=NOW.tzinfo)
    assert report.deferred == 1


async def test_the_day_s_pages_gone_defers_the_attempt(session, tmp_path):
    await set_source(session, enrich.SITE_SOURCE, daily_page_budget=0)
    company = await firm(session, website_domain="namuna-tekstil.example.uz")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path, {f"{SITE}/": site_page("site_landing")}) as w:
        await w.run()
    cand = await reload(session, cand)
    assert ("E2", "budget:pages") in steps(cand)
    assert (cand.enrich_attempts, cand.next_enrich_at) == (
        0,
        enrich.tomorrow_morning(NOW),
    )
    # The Fetcher may read robots.txt first (no page); no page is asked for.
    assert [u for u in w.urls() if not u.endswith("/robots.txt")] == []


async def test_a_js_only_site_is_evidence_and_no_observation(session, tmp_path):
    company = await firm(session, website_domain="namuna-tekstil.example.uz")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path, {f"{SITE}/": site_page("site_js_only")}) as w:
        await w.run()
    cand = await reload(session, cand)
    assert ("E2", "js_only") in steps(cand)
    ev = await session.scalar(sa.select(Evidence).where(Evidence.url == f"{SITE}/"))
    assert ev is not None
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Observation)
            .where(Observation.evidence_id == ev.id)
        )
    ) == 0


async def test_a_site_fetched_within_30_days_is_not_fetched_again(session, tmp_path):
    company = await firm(session, website_domain="namuna-tekstil.example.uz")
    await signal(session, company)
    session.add(
        Evidence(
            source_key="enrich_site",
            url=f"{SITE}/",
            fetched_at=NOW - timedelta(days=29),
            sha256=f"{seq():064x}",
            bytes=0,
            excerpt="",
            expires_at=NOW + timedelta(days=150),
        )
    )
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path, {f"{SITE}/": site_page("site_landing")}) as w:
        await w.run()
    assert ("E2", "fresh") in steps(await reload(session, cand))
    assert w.urls() == []


def site_pages(w: World) -> list[str]:
    """The company site's pages the run asked for (robots.txt left out)."""
    return [
        u
        for u in w.urls()
        if "namuna-tekstil.example.uz" in u and not u.endswith("/robots.txt")
    ]


async def back_to_enriching(session, cand: LeadCandidate, at: datetime) -> None:
    """The candidate put back in enrichment at ``at`` with the phones the
    first walk found taken away (so S1 does not end the next walk at E1)."""
    await session.execute(sa.delete(Contact).where(Contact.company_id == cand.company_id))
    await session.execute(
        sa.update(LeadCandidate)
        .where(LeadCandidate.id == cand.id)
        .values(state="enriching", next_enrich_at=at, enrich_attempts=0)
    )
    await session.commit()


async def test_a_site_that_redirects_to_www_is_fresh_for_30_days(session, tmp_path):
    """The evidence is stored under the page a same-site redirect led to
    (apex → www); the next attempt's 30-day check asks with the URL it would
    request, and finds it: the site is not fetched again (04 §3)."""
    company = await firm(session, website_domain="namuna-tekstil.example.uz")
    await signal(session, company)  # 50; the site's mobile adds 6: 56 < 60
    cand = await enriching(session, company)
    await session.commit()
    www = "https://www.namuna-tekstil.example.uz"
    routes = {
        f"{SITE}/": httpx.Response(301, headers={"location": f"{www}/"}),
        f"{www}/robots.txt": ALLOW_ALL,
        f"{www}/": site_page("site_landing"),
        f"{www}/kontakty": site_page("site_contacts"),
    }
    async with world(tmp_path, routes) as w:
        await w.run()
        first = site_pages(w)
        assert first == [f"{SITE}/", f"{www}/", f"{www}/kontakty"]
        await back_to_enriching(session, cand, NOW + timedelta(days=1))
        await w.run(NOW + timedelta(days=1))
        second = site_pages(w)
    cand = await reload(session, cand)
    assert second == first  # nothing fetched again
    assert ("E2", "fresh") in steps(cand)


async def test_an_about_page_fetched_within_30_days_is_not_fetched_again(
    session, tmp_path
):
    company = await firm(session, website_domain="namuna-tekstil.example.uz")
    await signal(session, company)
    session.add(
        Evidence(
            source_key="enrich_site",
            url=f"{SITE}/kontakty",
            fetched_at=NOW - timedelta(days=10),
            sha256=f"{seq():064x}",
            bytes=0,
            excerpt="",
            expires_at=NOW + timedelta(days=150),
        )
    )
    cand = await enriching(session, company)
    await session.commit()
    routes = {
        f"{SITE}/": site_page("site_landing"),
        f"{SITE}/kontakty": site_page("site_contacts"),
    }
    async with world(tmp_path, routes) as w:
        await w.run()
    cand = await reload(session, cand)
    assert [s for s in steps(cand) if s[0] == "E2"] == [("E2", "ok"), ("E2", "fresh")]
    assert f"{SITE}/kontakty" not in w.urls()


# --- E3: the public page, never Telethon ------------------------------------------------


def test_enrichment_imports_no_telethon_and_no_http_client():
    # No model client either: E0-E6 make no model call, so nothing reaches a
    # model unpseudonymised (E5/E6's own clients carry that rule, T-25/T-26).
    for name in (
        "services/enrich.py",
        "services/registry.py",
        "services/markup.py",
        "collectors/company_site.py",
        "collectors/telegram_page.py",
    ):
        path = PACKAGE / name
        module = "clientscrapper." + name.removesuffix(".py").replace("/", ".")
        found = imported_modules(tree_of(path), module=module)
        assert not [
            m for m in found if m.split(".")[0] in ("telethon", "httpx", "anthropic")
        ]
    # The fetch goes through the Fetcher it is handed.
    tree = ast.parse((PACKAGE / "services/enrich.py").read_text(encoding="utf-8"))
    assert not [
        n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "TelegramClient"
    ]


async def test_e3_reads_the_channel_page_through_the_fetcher(session, tmp_path):
    company = await firm(
        session, "Namuna Optom Uy-roʻzgʻor", telegram_channel="namuna_optom"
    )
    await signal(session, company)
    await signal(session, company, "cn_origin")
    cand = await enriching(session, company, score=70)
    await session.commit()
    async with world(
        tmp_path, {"https://t.me/s/namuna_optom": site_page("tme_channel")}
    ) as w:
        await w.run()
    cand = await reload(session, cand)
    assert ("E3", "ok") in steps(cand)
    assert cand.state == "review"  # the channel's phone: S1
    assert w.urls() == ["https://t.me/robots.txt", "https://t.me/s/namuna_optom"]
    ev = await session.scalar(
        sa.select(Evidence).where(Evidence.url == "https://t.me/s/namuna_optom")
    )
    assert ev is not None and ev.source_key == "enrich_site"
    company = await reload(session, company)
    assert company.website_domain == "namuna-optom.example.uz"


async def test_e3_a_user_page_is_not_kept_and_the_name_goes_to_the_reader(
    session, tmp_path
):
    company = await firm(session, telegram_channel="namuna_person")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    routes = {
        "https://t.me/s/namuna_person": httpx.Response(
            302, headers={"location": "https://t.me/namuna_person"}
        ),
        "https://t.me/namuna_person": site_page("tme_user"),
    }
    async with world(tmp_path, routes) as w:
        await w.run()
    cand = await reload(session, cand)
    assert ("E3", "queued") in steps(cand)
    # The redirect to the person's page is not followed: the page is never
    # read at all, not only never stored.
    assert "https://t.me/namuna_person" not in w.urls()
    e3 = next(e for e in cand.score_breakdown["enrichment"] if e["step"] == "E3")
    assert e3["found"] == {"page": "http_302"}
    flag = await session.get(RuntimeFlag, chats.PENDING_FLAG, populate_existing=True)
    assert flag is not None
    queued = flag.value["names"]["namuna_person"]
    assert (queued["priority"], queued["by"]) == (
        chats.PRIORITY_RESOLVED,
        f"enrich:{cand.id}",
    )
    assert chats.PRIORITY_RESOLVED == 3
    # A person's page is never stored (06 §5.1).
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Evidence)
            .where(Evidence.url.like("https://t.me/%"))
        )
    ) == 0


async def test_e3_a_304_on_a_stored_channel_page_is_unchanged_not_queued(
    session, tmp_path
):
    """A month on, the conditional GET of a channel page stored before
    answers 304: it is the channel, unchanged — not a name for the reader."""
    company = await firm(session, telegram_channel="namuna_optom")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    page = site_page("tme_channel")

    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"etag": '"v1"'})
        return httpx.Response(
            200,
            content=page.content,
            headers={"content-type": "text/html; charset=utf-8", "etag": '"v1"'},
        )

    later = NOW + timedelta(days=31)
    async with world(tmp_path, {"https://t.me/s/namuna_optom": answer}) as w:
        await w.run()
        assert ("E3", "ok") in steps(await reload(session, cand))
        await back_to_enriching(session, cand, later)
        await w.run(later)
    cand = await reload(session, cand)
    assert steps(cand)[-1] != ("E3", "queued")
    assert ("E3", "unchanged") in steps(cand)
    flag = await session.get(RuntimeFlag, chats.PENDING_FLAG, populate_existing=True)
    assert flag is None or "namuna_optom" not in (flag.value.get("names") or {})


async def test_e3_never_fetches_a_person_s_username(session, tmp_path):
    company = await firm(session)
    await signal(session, company)
    await contact(
        session,
        company,
        tier="person",
        telegram_username="namuna_rustam",
        full_name="Rustam",
        role="direktor",
    )
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path) as w:
        await w.run()
    assert ("E3", "skipped:no_channel") in steps(await reload(session, cand))
    assert w.urls("t.me") == []


# --- E4: the registry ---------------------------------------------------------------


ORGINFO_ROUTES = {
    SEARCH: registry_page("orginfo_search_stir_310514454"),
    CARD: registry_page("orginfo_organization_cf68f132a2f4"),
}


async def test_e4_is_off_until_the_owner_enables_the_registry_row(session, tmp_path):
    company = await firm(session, stir="310514454")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path, ORGINFO_ROUTES) as w:
        await w.run()
    assert ("E4", "skipped:source_disabled") in steps(await reload(session, cand))
    assert w.urls("orginfo.uz") == []


async def test_e4_hit_confirms_the_firm_and_keeps_one_decision_maker(session, tmp_path):
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, "Namuna Orginfo", legal_form=None, stir="310514454")
    await signal(session, company)  # 50
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path, ORGINFO_ROUTES) as w:
        await w.run()
    cand = await reload(session, cand)
    assert ("E4", "hit") in steps(cand)
    keys = set(
        await session.scalars(
            sa.select(Signal.key).where(
                Signal.company_id == company.id, Signal.source_key == "registry_lookup"
            )
        )
    )
    assert keys == {"ent_legal_confirmed", "ent_age_2y_active"}
    company = await reload(session, company)
    assert (company.registry_status, company.oked, str(company.registered_on)) == (
        "active",
        "63110",
        "2023-05-27",
    )
    assert company.legal_form == "MChJ"
    people = (
        await session.scalars(
            sa.select(Contact).where(
                Contact.company_id == company.id,
                Contact.tier == "person",
                Contact.anonymised_at.is_(None),
            )
        )
    ).all()
    assert len(people) == 1
    head = people[0]
    assert (head.full_name, head.role, head.is_decision_maker) == (
        "Seller 04",
        "Руководитель",
        True,
    )
    assert (head.legal_basis, head.source_url, head.collected_at) == (
        "public_registry",
        CARD,
        NOW,
    )
    assert head.phone == "+998900000001"
    # 50 + solidity 12 + a mobile 6 + a decision maker 2 = 70 with a phone: S1.
    assert (cand.state, cand.score) == ("review", 70)
    assert w.urls("orginfo.uz") == [
        "https://orginfo.uz/robots.txt",
        f"{SEARCH}?q=310514454",
        CARD,
    ]
    run = await session.scalar(
        sa.select(CollectorRun).where(CollectorRun.source_key == "registry_lookup")
    )
    assert run is not None and (run.pages, run.meta["lookups"]) == (2, 1)
    assert set(
        await session.scalars(
            sa.select(Evidence.url).where(Evidence.source_key == "registry_lookup")
        )
    ) == {f"{SEARCH}?q=310514454", CARD}


async def test_e4_a_second_person_never_joins_the_decision_maker(session, tmp_path):
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, "Namuna Orginfo", stir="310514454")
    await signal(session, company)
    await contact(
        session,
        company,
        tier="person",
        full_name="Rustam",
        role="direktor",
        telegram_username="namuna_rustam",
    )
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path, ORGINFO_ROUTES) as w:
        await w.run()
    assert ("E4", "hit") in steps(await reload(session, cand))
    people = (
        await session.execute(
            sa.select(Contact.full_name, Contact.tier, Contact.phone)
            .where(Contact.company_id == company.id, Contact.anonymised_at.is_(None))
            .order_by(Contact.tier)
        )
    ).all()
    # The firm keeps its decision maker; the registry's phone is the firm's.
    assert people == [
        (None, "company", "+998900000001"),
        ("Rustam", "person", None),
    ]


async def test_e4_never_lets_the_registry_s_director_erase_the_telegram_poster(
    session, tmp_path
):
    """The poster whose intent made the lead (a person-tier row with a
    Telegram account) is whom a manager writes to: a registry hit's director
    — rank 3, reached by nobody on Telegram — does not anonymise him; the
    director's number stays as the firm's, without the name."""
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, "Namuna Orginfo", stir="310514454")
    await signal(session, company)
    await signal(session, company, "cn_origin")
    poster = await contact(
        session,
        company,
        tier="person",
        full_name="Bobur",
        telegram_username="namuna_poster",
        telegram_id=777000111,
        legal_basis="public_source",
        source_url="https://t.me/namuna_chat/1",
        collected_at=NOW - timedelta(days=1),
    )
    cand = await enriching(session, company, attempts=2, score=70)
    await session.commit()
    async with world(tmp_path, ORGINFO_ROUTES) as w:
        await w.run()
    poster = await reload(session, poster)
    assert (poster.full_name, poster.telegram_username, poster.anonymised_at) == (
        "Bobur",
        "namuna_poster",
        None,
    )
    people = (
        await session.execute(
            sa.select(Contact.full_name, Contact.tier, Contact.phone)
            .where(Contact.company_id == company.id, Contact.anonymised_at.is_(None))
            .order_by(Contact.tier)
        )
    ).all()
    assert people == [
        (None, "company", "+998900000001"),
        ("Bobur", "person", None),
    ]
    assert ("E4", "hit") in steps(await reload(session, cand))


async def test_e4_prime_spends_no_lookup_on_a_company_with_no_city(session, tmp_path):
    """04 §3 «name + city must agree, else none»: with no city a same-named
    firm anywhere in the country would give ours its STIR and its director."""
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, "Orginfo", city=None)
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path, ORGINFO_ROUTES) as w:
        await w.run()
    cand = await reload(session, cand)
    company = await reload(session, company)
    assert ("E4", "skipped:no_city") in steps(cand)
    assert w.urls("orginfo.uz") == [] and company.stir is None
    run = await session.scalar(
        sa.select(CollectorRun).where(CollectorRun.source_key == "registry_lookup")
    )
    assert run is None  # no lookup counted


async def test_e4_prime_a_card_another_known_firm_holds_is_none_of_ours(
    session, tmp_path
):
    """E4′'s card carries a STIR another company holds: the STIR outranks
    the name (04 §2.1) — the page is evidence, but no registry fact, column
    or director is written on ours; ours is marked ``dup_of`` the holder for
    the owner's 🔀."""
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    other = await firm(session, "Orginfo", stir="310514454", city="Fargʻona")
    ours = await firm(session, "Orginfo", city="Fargʻona")
    await signal(session, ours)
    cand = await enriching(session, ours)
    await session.commit()
    routes = {
        SEARCH: registry_page("orginfo_search_stir_310514454"),
        CARD: registry_page("orginfo_organization_cf68f132a2f4"),
    }
    async with world(tmp_path, routes) as w:
        await w.run()
    cand = await reload(session, cand)
    ours = await reload(session, ours)
    assert ("E4", "foreign_stir") in steps(cand)
    assert (ours.stir, ours.registry_status, ours.oked, ours.dup_of) == (
        None,
        "unknown",
        None,
        other.id,
    )
    keys = set(
        await session.scalars(
            sa.select(Signal.key).where(
                Signal.company_id == ours.id, Signal.source_key == "registry_lookup"
            )
        )
    )
    assert keys == set()
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Contact)
            .where(Contact.company_id == ours.id)
        )
    ) == 0
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Evidence)
            .where(Evidence.url == CARD, Evidence.source_key == "registry_lookup")
        )
    ) == 1


async def test_e4_prime_never_follows_a_card_the_search_marks_liquidated(
    session, tmp_path
):
    """A same-named, same-city firm the search list marks «Ликвидирована» is
    an older firm, not ours: following it would exclude ours as liquidated."""
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, "Orginfo", city="Fargʻona")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    search = (
        (REGISTRIES / "orginfo_search_stir_310514454.html")
        .read_text(encoding="utf-8")
        .replace(
            '<span class="text-body-tertiary ms-1 text-nowrap">',
            '<span class="bg-danger bg-opacity-25 px-2 py-1 rounded-5 text-danger">'
            "Ликвидирована</span>"
            '<span class="text-body-tertiary ms-1 text-nowrap">',
            1,
        )
    )
    assert registry.parse_orginfo_search(search.encode(), url=SEARCH)[0].liquidated
    async with world(tmp_path, {SEARCH: httpx.Response(200, text=search)}) as w:
        await w.run()
    cand = await reload(session, cand)
    assert ("E4", "none") in steps(cand) and cand.state == "enriching"
    assert CARD not in w.urls()


async def test_e4_is_cached_90_days_by_its_card(session, tmp_path):
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, "Namuna Orginfo", stir="310514454")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()

    async def again(at: datetime) -> list[str]:
        # Without the registry's phone S1 cannot end the chain before E4.
        await session.execute(sa.delete(Contact).where(Contact.company_id == company.id))
        await session.execute(
            sa.update(LeadCandidate)
            .where(LeadCandidate.id == cand.id)
            .values(state="enriching", next_enrich_at=at, enrich_attempts=0)
        )
        await session.commit()
        await w.run(at)
        return [r for s, r in steps(await reload(session, cand)) if s == "E4"]

    def pages() -> list[str]:
        return [u for u in w.urls("orginfo.uz") if not u.endswith("/robots.txt")]

    async with world(tmp_path, ORGINFO_ROUTES) as w:
        await w.run()
        assert pages() == [f"{SEARCH}?q=310514454", CARD]
        assert await again(NOW + timedelta(days=89)) == ["hit", "cached"]
        assert pages() == [f"{SEARCH}?q=310514454", CARD]
        # Past 90 days the card itself is read again, with no search.
        assert await again(NOW + timedelta(days=91)) == ["hit", "cached", "hit"]
        assert pages() == [f"{SEARCH}?q=310514454", CARD, CARD]


async def test_e4_a_captcha_rests_the_host_a_day_and_is_retried_next_attempt(
    session, tmp_path
):
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, stir="310514454")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    captcha = httpx.Response(200, text="<html><body>captcha</body></html>")
    async with world(tmp_path, {SEARCH: captcha}) as w:
        await w.run()
    cand = await reload(session, cand)
    assert ("E4", registry.UNAVAILABLE) in steps(cand)
    source = await session.get(Source, enrich.REGISTRY_SOURCE, populate_existing=True)
    assert source is not None
    assert (
        source.config["paused_hosts"]["orginfo"]
        == (NOW + timedelta(hours=24)).isoformat()
    )
    assert cand.enrich_attempts == 1  # retried at the next attempt, not deferred


async def test_e4_by_name_for_a_firm_with_no_stir(session, tmp_path):
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, "XITOY TOSHKENT", city="Toshkent")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    card = "https://orginfo.uz/ru/organization/e9beed368894/"
    body = (
        (REGISTRIES / "orginfo_organization_cf68f132a2f4.html")
        .read_text(encoding="utf-8")
        .replace("310514454", "313378012")
        .replace("ORGINFO", "XITOY TOSHKENT")
        .replace("Farg&#x27;ona viloyati, Qo&#x27;qon sh.", "Toshkent sh.")
        .replace("Farg'ona viloyati, Qo'qon sh.", "Toshkent sh.")
    )
    routes = {
        SEARCH: registry_page("orginfo_search_name_xitoy_newest_p1"),
        card: httpx.Response(200, text=body),
    }
    async with world(tmp_path, routes) as w:
        await w.run()
    assert ("E4", "hit") in steps(await reload(session, cand))
    assert (await reload(session, company)).stir == "313378012"


# --- the /stir re-run (T-20 carried) --------------------------------------------------


async def test_stir_makes_e4_run_on_a_review_candidate_and_nothing_else(
    session, tmp_path
):
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, "Namuna Orginfo")
    await signal(session, company)
    await contact(session, company, phone(43))
    cand = await enriching(session, company, state="review", score=65, due=None)
    # A clock an earlier enrichment left on a candidate that moved on is no
    # re-run, even on a firm the owner once gave a STIR (its /stir ran a month
    # ago and was answered): only a /stir the registry has not answered is.
    stale_company = await firm(session, "Namuna Eski")
    stale = await enriching(session, stale_company, state="review", score=65, due=None)
    await session.commit()
    month_ago = NOW - timedelta(days=30)
    assert (
        await owner.set_stir(session, stale.id, "300000044", now=month_ago)
    ).outcome == ("written")
    answered = {"stir_answered": (month_ago + timedelta(minutes=10)).isoformat()}
    await session.execute(
        sa.update(LeadCandidate)
        .where(LeadCandidate.id == stale.id)
        .values(next_enrich_at=NOW - timedelta(days=2), score_breakdown=answered)
    )
    await session.commit()
    result = await owner.set_stir(session, cand.id, "310514454", now=NOW)
    await session.commit()
    assert result.outcome == "written"
    async with world(tmp_path, ORGINFO_ROUTES) as w:
        report = await w.run(NOW + timedelta(minutes=10))
    cand = await reload(session, cand)
    assert report.reruns == 1 and report.attempts == 0
    assert (cand.state, cand.next_enrich_at, cand.last_scored_at) == (
        "review",
        None,
        None,
    )
    assert steps(cand) == [("E4", "hit")]
    assert cand.score_breakdown["enrichment"][0]["attempt"] is None
    assert cand.score_breakdown["stir_answered"] == (
        NOW + timedelta(minutes=10)
    ).isoformat()
    stale = await reload(session, stale)
    assert stale.score_breakdown == answered
    assert "q=300000044" not in " ".join(w.urls())


async def stir_review(session, name: str = "Namuna Orginfo") -> LeadCandidate:
    """A review candidate the owner just gave a STIR (06 §1.2 ``/stir``)."""
    company = await firm(session, name)
    await signal(session, company)
    await contact(session, company, phone(43))
    cand = await enriching(session, company, state="review", score=65, due=None)
    await session.commit()
    assert (await owner.set_stir(session, cand.id, "310514454", now=NOW)).outcome == (
        "written"
    )
    await session.commit()
    return cand


async def test_a_stir_waits_while_the_registry_is_off_and_runs_once_it_is_on(
    session, tmp_path
):
    """Day 1: ``registry_lookup`` is seeded off. The owner's /stir is not
    consumed by a run that could not ask — it stays due and runs at the
    first run after the owner turns the source on."""
    cand = await stir_review(session)
    async with world(tmp_path, ORGINFO_ROUTES) as w:
        off = await w.run(NOW + timedelta(minutes=10))
        held = await reload(session, cand)
        assert (off.reruns, held.score_breakdown, held.next_enrich_at) == (0, None, NOW)
        await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
        on = await w.run(NOW + timedelta(days=1))
    cand = await reload(session, cand)
    assert on.reruns == 1 and steps(cand) == [("E4", "hit")]
    assert cand.next_enrich_at is None
    assert f"{SEARCH}?q=310514454" in w.urls("orginfo.uz")


async def test_a_stir_with_the_day_s_lookups_gone_runs_tomorrow(session, tmp_path):
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    session.add(
        CollectorRun(
            source_key="registry_lookup",
            started_at=NOW - timedelta(hours=1),
            finished_at=NOW - timedelta(hours=1),
            status="ok",
            meta={"note": "enrich", "lookups": registry.LOOKUPS_PER_DAY},
        )
    )
    await session.commit()
    cand = await stir_review(session)
    async with world(tmp_path, ORGINFO_ROUTES) as w:
        today = await w.run(NOW + timedelta(minutes=10))
        assert today.reruns == 0 and w.urls("orginfo.uz") == []
        assert (await reload(session, cand)).next_enrich_at == NOW
        tomorrow = await w.run(enrich.tomorrow_morning(NOW))
    assert tomorrow.reruns == 1
    assert steps(await reload(session, cand)) == [("E4", "hit")]


async def test_a_stir_the_run_s_deadline_stopped_runs_at_the_next_run(session, tmp_path):
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    cand = await stir_review(session)
    at = NOW + timedelta(minutes=10)
    async with world(tmp_path, ORGINFO_ROUTES) as w:
        stopped = await enrich.enrich_batch(
            w.tools, now=at, clock=lambda: at + enrich.RUN_DEADLINE
        )
        assert stopped.stopped == "deadline" and w.urls("orginfo.uz") == []
        assert (await reload(session, cand)).next_enrich_at == at
        again = await w.run(at + timedelta(minutes=10))
    assert again.reruns == 1
    assert steps(await reload(session, cand)) == [("E4", "hit")]


def _no_answer(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("no answer", request=request)


async def test_a_stir_no_registry_host_answered_is_asked_again_tomorrow(
    session, tmp_path
):
    """Every host down (a timeout here): ``registry_unavailable`` — no
    exception, no failure — and the /stir stays pending for tomorrow 07:00."""
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    cand = await stir_review(session)
    at = NOW + timedelta(minutes=10)
    async with world(tmp_path, {SEARCH: _no_answer}) as w:
        down = await w.run(at)
    held = await reload(session, cand)
    assert (down.failed, down.reruns) == (0, 1)
    assert steps(held) == [("E4", registry.UNAVAILABLE)]
    assert held.score_breakdown["enrichment"][0]["found"]["tried"] == [
        "orginfo:unreachable"
    ]
    assert held.next_enrich_at == enrich.tomorrow_morning(at)
    async with world(tmp_path, ORGINFO_ROUTES) as w:
        up = await w.run(enrich.tomorrow_morning(at))
    cand = await reload(session, cand)
    assert up.reruns == 1 and steps(cand)[-1] == ("E4", "hit")
    assert cand.next_enrich_at is None


async def test_an_attempt_s_e4_answers_the_stir_too(session, tmp_path):
    """A /stir on a candidate that is enriching is answered by the attempt's
    own E4: once it has left for review, no re-run of it is due."""
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, "Namuna Orginfo")
    await signal(session, company)  # 50 + the mobile's 6: under 60, E4 runs
    await contact(session, company, phone(43))
    cand = await enriching(session, company, due=None)
    await session.commit()
    assert (await owner.set_stir(session, cand.id, "310514454", now=NOW)).outcome == (
        "written"
    )
    await session.commit()
    async with world(tmp_path, ORGINFO_ROUTES) as w:
        first = await w.run(NOW + timedelta(minutes=10))
        cand = await reload(session, cand)
        assert first.attempts == 1 and ("E4", "hit") in steps(cand)
        assert cand.score_breakdown["stir_answered"] == (
            NOW + timedelta(minutes=10)
        ).isoformat()
        await session.execute(
            sa.update(LeadCandidate)
            .where(LeadCandidate.id == cand.id)
            .values(state="review", next_enrich_at=NOW)
        )
        await session.commit()
        later = await w.run(NOW + timedelta(hours=2))
    assert later.reruns == 0


class Broken(registry.Orginfo):
    """A host whose lookup raises — a bug, not an unanswered host."""

    async def by_stir(self, stir, *, fetcher, budget):
        raise RuntimeError("boom")

    async def by_name(self, name, *, city, fetcher, budget):
        raise RuntimeError("boom")


async def test_a_stir_that_fails_every_time_is_given_up_after_three(session, tmp_path):
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    cand = await stir_review(session)
    at = NOW + timedelta(minutes=10)
    async with world(tmp_path, hosts=[Broken()]) as w:
        for n in range(1, 4):
            report = await w.run(at)
            held = await reload(session, cand)
            assert report.failed == 1
            assert [s for s in steps(held) if s[0] == "failed"] == [
                ("failed", "error:RuntimeError")
            ] * n
            at = held.next_enrich_at or at
            if n < 3:
                assert held.next_enrich_at is not None
        last = await w.run(at + timedelta(days=1))
    held = await reload(session, cand)
    assert held.next_enrich_at is None and "stir_answered" in held.score_breakdown
    assert last.failed == 0 and last.reruns == 0


async def test_an_attempt_that_raises_keeps_its_trail_and_spends_the_attempt(
    session, tmp_path
):
    """A walk that raises (a bug in a host) is a failed attempt, scheduled by
    the 1 h / 24 h / 7 d clock — never the same failure every hour for
    ever — and what it did before keeps its place in the trail."""
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, "Namuna Orginfo", stir="310514454")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path, hosts=[Broken()]) as w:
        report = await w.run()
    cand = await reload(session, cand)
    assert report.failed == 1 and report.attempts == 0
    assert (cand.state, cand.enrich_attempts) == ("enriching", 1)
    assert cand.next_enrich_at == NOW + enrich.SCHEDULE[1]
    assert steps(cand) == [
        ("E0", "ok"),
        ("E1", "ok"),
        ("E2", "skipped:no_site"),
        ("E3", "skipped:no_channel"),
        ("failed", "error:RuntimeError"),
    ]


async def test_an_attempt_no_registry_host_answered_is_counted_not_failed(
    session, tmp_path
):
    await set_source(session, enrich.REGISTRY_SOURCE, enabled=True)
    company = await firm(session, "Namuna Orginfo", stir="310514454")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path, {SEARCH: _no_answer}) as w:
        report = await w.run()
    cand = await reload(session, cand)
    assert (report.failed, report.attempts, report.deferred) == (0, 1, 0)
    assert ("E4", registry.UNAVAILABLE) in steps(cand)
    assert (cand.enrich_attempts, cand.next_enrich_at) == (1, NOW + enrich.SCHEDULE[1])


# --- the warm re-ask (T-21 carried) ---------------------------------------------------


async def handoff_hold(session, *, hold_until: datetime, answer: str | None = None):
    company = await firm(session)
    await signal(session, company)
    await signal(session, company, "cn_origin")
    await contact(
        session,
        company,
        tier="person",
        telegram_username="namuna_rustam",
        full_name="Rustam",
        role="direktor",
    )
    cand = await enriching(
        session,
        company,
        state="hold",
        attempts=3,
        score=70,
        due=None,
        hold_until=hold_until,
        score_breakdown={"gates": {"no_phone": True}, "enrichment": []},
    )
    review = LeadReview(
        candidate_id=cand.id,
        kind="owner",
        state="approved" if answer else "expired",
        answer=answer,
        answered_at=NOW - timedelta(days=8) if answer else None,
    )
    session.add(review)
    await session.flush()
    return cand


async def test_an_expired_handoff_card_is_asked_again_when_its_hold_runs_out(
    session, tmp_path
):
    due = await handoff_hold(session, hold_until=NOW - timedelta(minutes=1))
    waiting = await handoff_hold(session, hold_until=NOW + timedelta(days=1))
    handed = await handoff_hold(
        session, hold_until=NOW - timedelta(minutes=1), answer="handoff"
    )
    await session.commit()
    async with world(tmp_path) as w:
        report = await w.run()
    assert report.reasked == 1
    due = await reload(session, due)
    assert (due.state, due.tier) == ("review", "warm")
    assert due.score_breakdown["gates"]["no_phone"] is True
    assert steps(due) == [("reask", "review")]
    assert (await reload(session, waiting)).state == "hold"
    assert (await reload(session, handed)).state == "hold"


# --- E0: companies.sectors (T-18/T-22 carried) ----------------------------------------


async def resolved(session, company: Company, seen: Seen) -> Observation:
    ev = Evidence(
        source_key=seen.source,
        url=seen.url,
        fetched_at=NOW - timedelta(days=2),
        sha256=f"{seq():064x}",
        bytes=0,
        excerpt=seen.excerpt,
        expires_at=NOW + timedelta(days=150),
    )
    session.add(ev)
    await session.flush()
    row = Observation(
        evidence_id=ev.id,
        source_key=seen.source,
        status="resolved",
        parsed=to_observation_json(seen),
        company_id=company.id,
        resolved_at=NOW - timedelta(days=2),
    )
    session.add(row)
    await session.flush()
    return row


def profile(text: str, n: int) -> Seen:
    return Seen(
        source="instagram_discovery",
        source_ref=f"ig:{n}",
        url=f"https://www.instagram.com/namuna_{n}/",
        fetched_at=NOW - timedelta(days=2),
        raw_sha256="",
        kind="company",
        company_name_raw="Namuna Uy",
        excerpt=text,
    )


async def test_e0_fills_sectors_and_rule_4_then_fires_on_the_older_bio(session, tmp_path):
    company = await firm(session, "Namuna Uy")
    await signal(session, company)
    bio = await resolved(
        session, company, profile("Optom savdo, Toshkent. Buyurtma qabul qilamiz.", 1)
    )
    await resolved(session, company, profile("Mebel va mebel furnitura", 2))
    session.add(
        ModelJob(
            kind="classify",
            lane="instant",
            status="applied",
            company_id=company.id,
            model="test",
            custom_id=f"cs-{seq()}",
            result={"speaker_role": "buyer", "goods_category": "textile"},
        )
    )
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path) as w:
        await w.run()
    company = await reload(session, company)
    assert company.sectors == ["furniture", "textile"]
    bulky = await session.scalar(
        sa.select(Signal.evidence_id).where(
            Signal.company_id == company.id, Signal.key == "vol_wholesale_bulky"
        )
    )
    assert bulky == bio.evidence_id
    e0 = (await reload(session, cand)).score_breakdown["enrichment"][0]
    assert e0["found"]["sectors"] == ["furniture", "textile"]


def test_merged_sectors_keep_only_category_keys():
    assert enrich.merged_sectors(["textile", "nonsense"], frozenset({"toys", "x"})) == [
        "textile",
        "toys",
    ]


# --- the clock -----------------------------------------------------------------------


def test_tomorrow_morning_is_seven_tashkent_the_next_day():
    late = datetime(2026, 10, 27, 23, 30, tzinfo=NOW.tzinfo)
    assert enrich.tomorrow_morning(late) == datetime(
        2026, 10, 28, 7, 0, tzinfo=NOW.tzinfo
    )
    assert enrich.next_attempt_at(1, NOW) == NOW + timedelta(hours=24)
    assert enrich.next_attempt_at(2, NOW) == NOW + timedelta(days=7)


async def test_the_global_pause_stops_the_run(session, tmp_path):
    session.add(RuntimeFlag(key=costs.PAUSE_FLAG, value=True, updated_by="owner:1"))
    company = await firm(session, website_domain="namuna-tekstil.example.uz")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    async with world(tmp_path, {f"{SITE}/": site_page("site_landing")}) as w:
        report = await w.run()
    assert report.stopped == "global_pause"
    assert (await reload(session, cand)).enrich_attempts == 0
    assert w.urls() == []
