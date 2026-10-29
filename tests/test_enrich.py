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
from clientscrapper.services.http import Fetcher, PageBudget
from clientscrapper.services.normalise import to_observation_json
from clientscrapper.services.usage import Priced
from tests.ast_tools import PACKAGE, imported_modules, tree_of
from tests.fakes import ALLOW_ALL, FakeClock, Site, fetcher_for
from tests.gate_fixtures import NOW, contact, firm, phone, seq, signal

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
    """The two rows enrichment reads, snapshotted and restored (07 §1.0)."""
    keys = (enrich.SITE_SOURCE, enrich.REGISTRY_SOURCE)
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


@dataclass
class FakeFinder:
    """An E5 finder (T-25/T-33 build the real ones): costs ``est`` a call,
    finds nothing, and records whether its call was claimed first."""

    est: Decimal
    key: str = "fake_serp"
    operation: Any = "serp_brave"
    calls: list[bool] = field(default_factory=list)

    def estimate(self, query: enrich.CompanyQuery) -> Decimal:
        return self.est

    async def find(
        self, query: enrich.CompanyQuery, *, fetcher: Fetcher, budget: PageBudget
    ) -> list[enrich.Page]:
        async with SessionLocal() as s:
            claimed = await s.scalar(
                sa.select(sa.func.count())
                .select_from(UsageLog)
                .where(
                    UsageLog.company_id == query.company_id,
                    UsageLog.model.is_(None),
                    UsageLog.cost_usd == self.est,
                )
            )
        self.calls.append(bool(claimed))
        return []

    def price(self, pages: list[enrich.Page]) -> Priced:
        return Priced(cost_usd=self.est, units=Decimal(1))


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
    company = await firm(session, "Namuna Mebel Savdo", instagram="namuna.mebel")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    finders = [FakeFinder(Decimal("0.04")) for _ in range(3)]
    reader = FakeReader()
    async with world(tmp_path, finders=finders, profiles=reader) as w:
        await w.run()
    cand = await reload(session, cand)
    e5 = [e for e in cand.score_breakdown["enrichment"] if e["step"] == "E5"]
    assert [e["result"] for e in e5] == ["ok", "ok", "company_cap"]
    assert [e["cost_usd"] for e in e5] == [0.04, 0.04, 0.0]
    # Every paid call went through costs.ask: claimed before it ran, and the
    # ledger names the company (04 §3 S3's SUM by company_id).
    assert [f.calls for f in finders] == [[True], [True], []]
    spent = await costs.spend(session, since=enrich.SPEND_SINCE, company_id=company.id)
    assert spent.usd == Decimal("0.08")
    assert reader.handles == ["namuna.mebel"]  # E6, free, still asked
    assert cand.enrich_attempts == 1  # the cap is no deferral


async def test_a_refused_paid_call_retries_tomorrow_at_seven_and_is_not_counted(
    session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "daily_cost_cap_usd", Decimal("0.01"))
    company = await firm(session, "Namuna Mebel Savdo")
    await signal(session, company)
    cand = await enriching(session, company)
    await session.commit()
    finder = FakeFinder(Decimal("0.04"))
    async with world(tmp_path, finders=[finder]) as w:
        report = await w.run()
    cand = await reload(session, cand)
    assert finder.calls == []
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


# --- E3: the public page, never Telethon ------------------------------------------------


def test_enrichment_imports_no_telethon_and_no_http_client():
    for name in ("services/enrich.py", "services/registry.py"):
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
    # re-run: only /stir's own mark is.
    stale_company = await firm(session, "Namuna Eski", stir="300000044")
    stale = await enriching(
        session, stale_company, state="review", score=65, due=NOW - timedelta(days=2)
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
    stale = await reload(session, stale)
    assert stale.score_breakdown is None
    assert "q=300000044" not in " ".join(w.urls())


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
