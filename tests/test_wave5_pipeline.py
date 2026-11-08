"""Wave 5 end to end: the directories and the exhibitor lists mint companies
that carry no phone, and enrichment (04 §3) picks them up exactly as its
section says (the wave-5 integration, 07 §4.1 T-22 / T-32 / T-24).

Recorded pages all the way: the directory and exhibitor fixtures through
the real collectors and the runner (``tests/collectors``' fakes), then the
real resolver, scorer and enrichment chain, the registry answered by the
recorded orginfo.uz pages (``tests/fixtures/registries``). Where a recorded
page names another firm than the one a test follows, the test says so and
substitutes the name, the STIR and the city in its text — the page's shape
is what is recorded, the firm is not. No request leaves the test (any URL a
fake was not given is a 404) and no model is asked.

What is pinned:

* a goldenpages row whose phone the list masks (``+998 (NN) NNN-XX-XX``)
  and whose card is not answered keeps no phone; it scores warm and enters
  ``enriching`` one hour on (04 §3 schedule), is not attempted before, and
  at the first attempt walks E0 → E6 in order: E4′ by name and city finds
  its registry card, which writes the STIR, the registry's columns,
  ``ent_legal_confirmed`` and the one decision maker, whose phone is the
  lead's — so the walk stops at S1 and the candidate reaches ``review``
  with a citable quote;
* a firm the registry does not know spends its attempt and is due again in
  24 h; the registry pages are kept without their bytes (founders, 06 §5.1);
* an exhibitor-only firm scores cold and is held — enrichment runs on warm
  candidates only — and has no city, so E4′ would have nothing to agree
  with; the owner's ``/stir`` (06 §1.2) is how the registry reaches it: E4
  by STIR attaches the registry's data and phone, and the row's own quote
  («CAITME 2026 · … · Uzbekistan · …») is what a card and the ERP note cite;
* ``/unut`` of an enriched directory firm leaves none of its rows behind —
  not the registry search by its name, which no observation cites.

``sources`` is seeded configuration: every row a test switches is put back
(07 §1.0).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa

from clientscrapper.collectors.directories import Directories
from clientscrapper.collectors.exhibitors import (
    DEFAULT_CALENDAR,
    Exhibitors,
    entry_config,
)
from clientscrapper.collectors.runner import run_collector
from clientscrapper.db.models import (
    CollectorRun,
    Company,
    CompanyId,
    Contact,
    Evidence,
    LeadCandidate,
    Observation,
    RuntimeFlag,
    Signal,
    Source,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import (
    candidates,
    cards,
    chats,
    companies,
    crm_push,
    enrich,
    gates,
    owner,
    purge,
    registry,
)
from clientscrapper.services import evidence as evidence_store
from tests.collectors.test_directories_crawl import ROWS, THURSDAY, Web
from tests.collectors.test_exhibitors import caitme_site
from tests.fakes import FakeClock, Site, fetcher_for

TZ = ZoneInfo("Asia/Tashkent")
# The exhibitors' Monday-window run (CAITME 2026 ended 10 Sep: in its window).
EXHIBITORS_AT = datetime(2026, 10, 29, 9, 0, tzinfo=TZ)
# The resolver and the scorer run the next morning, office hours.
NOW = datetime(2026, 10, 30, 10, 0, tzinfo=TZ)
REGISTRIES = Path(__file__).parent / "fixtures" / "registries"
SEARCH = "https://orginfo.uz/ru/search/organizations/"
STIR_CARD = "https://orginfo.uz/ru/organization/cf68f132a2f4/"
# The recorded name search (``q=xitoy``) lists «"XITOY TOSHKENT" MChJ» in
# Toshkent at this card; the tests put the firm they follow in its place.
NAME_CARD = "https://orginfo.uz/ru/organization/e9beed368894/"
RECORDED_NAME = "XITOY TOSHKENT"
RECORDED_STIR = "310514454"
SUBSTITUTED_STIR = "313378012"
SWITCHED = (*ROWS, "exhibitors", enrich.REGISTRY_SOURCE, enrich.SITE_SOURCE)
KEPT = ("enabled", "paused_until", "paused_reason", "config", "last_run_at")


def recorded(name: str) -> str:
    return (REGISTRIES / f"{name}.html").read_text(encoding="utf-8")


def html(text: str) -> httpx.Response:
    return httpx.Response(
        200, text=text, headers={"content-type": "text/html; charset=utf-8"}
    )


def registry_routes(firm: str) -> dict[str, Any]:
    """orginfo.uz as recorded, answering for ``firm``: the name search lists
    it (the recorded «XITOY TOSHKENT» card, renamed) at a Toshkent address,
    and its card is the recorded company page with ``firm``'s name, a STIR of
    its own and Toshkent for Qoʻqon; a STIR search is the recorded one."""
    by_name = recorded("orginfo_search_name_xitoy_newest_p1").replace(RECORDED_NAME, firm)
    card = (
        recorded("orginfo_organization_cf68f132a2f4")
        .replace(RECORDED_STIR, SUBSTITUTED_STIR)
        .replace("ORGINFO", firm)
        .replace("Farg&#x27;ona viloyati, Qo&#x27;qon sh.", "Toshkent sh.")
        .replace("Farg'ona viloyati, Qo'qon sh.", "Toshkent sh.")
    )

    def search(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("q", "")
        if query.isdigit():
            return html(recorded(f"orginfo_search_stir_{query}"))
        return html(by_name)

    return {
        "https://orginfo.uz/robots.txt": html(recorded("orginfo_robots")),
        SEARCH: search,
        NAME_CARD: html(card),
        STIR_CARD: html(recorded("orginfo_organization_cf68f132a2f4")),
    }


@pytest.fixture
async def wave5_sources(session) -> AsyncIterator[None]:  # type: ignore[no-untyped-def]
    """The directories and their sub-sources, the exhibitors (CAITME 2026 as
    its calendar) and the registry switched on; every column put back."""
    caitme = next(
        e for e in DEFAULT_CALENDAR if e.show == "caitme" and e.end_date.year == 2026
    )
    async with SessionLocal() as s:
        rows = await s.scalars(sa.select(Source).where(Source.key.in_(SWITCHED)))
        snapshot = {r.key: {c: getattr(r, c) for c in KEPT} for r in rows}
        await s.execute(
            sa.update(Source)
            .where(Source.key.in_(SWITCHED))
            .values(enabled=True, paused_until=None)
        )
        await s.execute(
            sa.update(Source)
            .where(Source.key == "exhibitors")
            .values(config={"calendar": [entry_config(caitme)]})
        )
        await s.commit()
    yield
    async with SessionLocal() as s:
        for key, values in snapshot.items():
            await s.execute(sa.update(Source).where(Source.key == key).values(**values))
        await s.commit()


def own_number(index: int) -> tuple[str, str]:
    """A visibly synthetic number of its own (07 §1.0's ``… 20000 NN`` shape
    under codes 93–99), and its raw form."""
    code, nn = 93 + index // 100, index % 100
    digits = f"{code}20000{nn:02d}"
    return f"+998{digits}", f"+998 {digits[:2]} {digits[2:5]} {digits[5:7]} {digits[7:]}"


async def renumber_directory_phones(session) -> None:  # type: ignore[no-untyped-def]
    """tests/fixtures/directories/README.md «Not for a resolver end-to-end»:
    the scrubber numbered each file's synthetic phones afresh, so unrelated
    firms on different pages share numbers. Before anything is resolved,
    each (source_ref, number) gets a number of its own — one firm keeps one
    number however many pages print it, two firms never share one (the
    wave-5 seam review: the E2E ran in a world where they did)."""
    given: dict[tuple[str, str], tuple[str, str]] = {}
    rows = await session.scalars(
        sa.select(Observation).where(Observation.source_key.like("directories%"))
    )
    for row in rows:
        parsed = dict(row.parsed)
        phones = parsed.get("phones") or []
        if not phones:
            continue
        ref = parsed["source_ref"]
        fresh = []
        for phone in phones:
            mark = (ref, phone["e164"])
            if mark not in given:
                given[mark] = own_number(len(given))
            e164, raw = given[mark]
            fresh.append({**phone, "e164": e164, "raw": raw})
        parsed["phones"] = fresh
        row.parsed = parsed
    await session.commit()


async def collect(session, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The two collectors over their recordings, through the runner, then
    each directory firm's numbers made its own (``renumber_directory_phones``)."""
    clock = FakeClock(THURSDAY)
    report = await run_collector(
        Directories,
        fetcher=fetcher_for(Web(clock).site),
        now=clock.now(),
        evidence_root=tmp_path,
        clock=clock.now,
    )
    assert report.status == "ok", report
    # CAITME's archive was swept a month ago: this Monday reads its window.
    session.add(
        CollectorRun(
            source_key="exhibitors",
            started_at=EXHIBITORS_AT - timedelta(days=30),
            finished_at=EXHIBITORS_AT - timedelta(days=30),
            status="ok",
            meta={"archived": ["caitme"]},
        )
    )
    await session.commit()
    clock = FakeClock(EXHIBITORS_AT)
    report = await run_collector(
        Exhibitors,
        fetcher=fetcher_for(caitme_site(clock)),
        now=clock.now(),
        evidence_root=tmp_path,
        clock=clock.now,
    )
    assert report.status == "ok", report
    await renumber_directory_phones(session)


async def settle(now: datetime) -> None:
    """The resolver, then the scorer, as their slots would run them (04 §0):
    a few rounds each — a ``found`` candidate waiting for a volume signal is
    claimed again on every round and never moves."""
    for _ in range(3):
        await companies.resolve_job(now=now)
    for _ in range(3):
        await candidates.score_job(now=now)


async def company_named(session, name: str) -> Company:  # type: ignore[no-untyped-def]
    found = await session.scalar(
        sa.select(Company)
        .where(Company.display_name == name)
        .execution_options(populate_existing=True)
    )
    assert found is not None, name
    return found


async def candidate_of(session, company: Company) -> LeadCandidate:  # type: ignore[no-untyped-def]
    found = await session.scalar(
        sa.select(LeadCandidate)
        .where(LeadCandidate.company_id == company.id)
        .execution_options(populate_existing=True)
    )
    assert found is not None, company.display_name
    return found


async def usable_phones(session, company: Company) -> list[Contact]:  # type: ignore[no-untyped-def]
    return list(
        await session.scalars(
            sa.select(Contact).where(
                Contact.company_id == company.id, Contact.phone.is_not(None)
            )
        )
    )


def trail(cand: LeadCandidate) -> list[tuple[str, str]]:
    return [(e["step"], e["result"]) for e in cand.score_breakdown["enrichment"]]


async def enrich_at(
    tmp_path: Path, at: datetime, routes: dict[str, Any]
) -> enrich.EnrichReport:
    site = Site(FakeClock(at), routes)
    async with fetcher_for(site) as fetcher:
        return await enrich.enrich_batch(
            enrich.Tools(fetcher=fetcher, evidence_root=tmp_path),
            now=at,
            clock=lambda: at,
        )


ASM = '"ASM ALLIANCE" ООО'
EMA = '"EMA LEASING" ООО'
CHINT = '"CHINT DISTRIBUTION" ООО'
JACK = '"JACK" (ООО "SEWING MACHINES TRADING")'


async def test_a_masked_directory_firm_is_enriched_registered_and_carded(
    session, wave5_sources, tmp_path
):
    await collect(session, tmp_path)
    await settle(NOW)

    # The list masks the phone and no card answered: no phone, a city.
    asm, ema = await company_named(session, ASM), await company_named(session, EMA)
    for firm in (asm, ema):
        assert await usable_phones(session, firm) == []
        assert firm.city == "Toshkent" and firm.stir is None
        cand = await candidate_of(session, firm)
        # 04 §3 / 02 §2.9: warm (40–59) → enriching, the first attempt 1 h on.
        assert (cand.state, cand.tier, cand.enrich_attempts) == ("enriching", "warm", 0)
        assert cand.next_enrich_at == NOW + enrich.SCHEDULE[0]
        assert cand.primary_source_key == "directories_goldenpages"

    # Not due before its hour.
    early = await enrich_at(tmp_path, NOW + timedelta(minutes=30), registry_routes(ASM))
    assert early.attempts == 0

    due = NOW + enrich.SCHEDULE[0]
    report = await enrich_at(tmp_path, due, registry_routes(ASM))
    # ASM, EMA and the CHINT dealer (warm on its brand and its two numbers).
    assert report.attempts == 3

    # E0 → E4 in the chain's order; E4′ (no STIR, a name and a city) hits;
    # the director's phone is a usable phone and the score passes 60: S1.
    cand = await candidate_of(session, asm)
    steps = trail(cand)
    # No website, no channel; E5 and E6 never run: S1 ended the walk.
    assert steps == [
        ("E0", "ok"),
        ("E1", "ok"),
        ("E2", "skipped:no_site"),
        ("E3", "skipped:no_channel"),
        ("E4", "hit"),
    ]
    assert (cand.state, cand.score) == ("review", 70)
    asm = await company_named(session, ASM)
    assert asm.stir == SUBSTITUTED_STIR
    assert asm.registry_status == "active"
    keys = set(
        await session.scalars(sa.select(Signal.key).where(Signal.company_id == asm.id))
    )
    assert registry.LEGAL_CONFIRMED in keys
    [director] = [
        c for c in await usable_phones(session, asm) if c.legal_basis == "public_registry"
    ]
    assert director.is_decision_maker and director.source_url == NAME_CARD

    # The registry pages are kept as their rows alone (founders, 06 §5.1),
    # each naming the company it was fetched for — one of the three walked.
    walked = [asm, ema, await company_named(session, CHINT)]
    pages = list(
        await session.scalars(
            sa.select(Evidence).where(Evidence.source_key == enrich.REGISTRY_SOURCE)
        )
    )
    assert {p.url.split("?")[0] for p in pages} >= {SEARCH, NAME_CARD}
    for page in pages:
        assert page.raw_path is None
        assert page.meta["file"] == evidence_store.FILE_NOT_KEPT
        assert page.meta[evidence_store.FOR_COMPANY] in {str(f.id) for f in walked}

    await settle(due)
    cand = await candidate_of(session, asm)
    assert cand.state == "review", (cand.state, cand.score, steps)
    view = await candidates.load_view(session, cand.id, now=due)
    cited, quote = crm_push.cited(view)
    assert quote in cited.excerpt
    assert cited.source_key == "directories_goldenpages"

    # The firm the registry does not know spends its attempt: due in 24 h.
    cand = await candidate_of(session, ema)
    assert ("E4", "none") in trail(cand)
    assert (cand.state, cand.enrich_attempts) == ("enriching", 1)
    assert cand.next_enrich_at == due + enrich.SCHEDULE[1]


async def test_an_exhibitor_firm_is_held_and_the_owner_s_stir_lets_the_registry_in(
    session, wave5_sources, tmp_path
):
    await collect(session, tmp_path)
    await settle(NOW)

    exhibitors = list(
        await session.scalars(
            sa.select(LeadCandidate).where(
                LeadCandidate.primary_source_key == "exhibitors"
            )
        )
    )
    # Every exhibitor-only firm scores cold and is held: enrichment runs on
    # warm candidates only (04 §3), and none has a city for E4′ to agree on.
    assert exhibitors and {(c.state, c.tier) for c in exhibitors} == {("hold", "cold")}
    assert all(c.next_enrich_at is None for c in exhibitors)

    advantex_id = await session.scalar(
        sa.select(CompanyId.company_id).where(
            CompanyId.id_type == "exhibitor_slug", CompanyId.id_value == "era:25848"
        )
    )
    advantex = await session.get(Company, advantex_id, populate_existing=True)
    assert advantex is not None and advantex.city is None
    cand = await candidate_of(session, advantex)

    # Its own row is citable now: the show, the year and the row as printed,
    # at the show's list page, dated by the show (the wave-5 integration).
    view = await candidates.load_view(session, cand.id, now=NOW)
    cited, quote = crm_push.cited(view)
    assert (cited.key, cited.source_key) == ("vol_exhibitor", "exhibitors")
    assert quote.startswith("CAITME 2026 · ADVANTEX")
    assert cited.url == "https://caitme.uz/en/exhibitors-list#caitme:2026:advantex-ooo"
    nega = crm_push.nega_topildi(view)
    assert nega.startswith("NEGA TOPILDI: Koʻrgazma · 10.09.2026 · «CAITME 2026 · ")

    # The owner's /stir (06 §1.2): E4 by STIR re-runs on the held candidate.
    result = await owner.set_stir(session, cand.id, RECORDED_STIR, now=NOW)
    await session.commit()
    assert result.outcome == "written"
    report = await enrich_at(tmp_path, NOW + timedelta(minutes=10), registry_routes("-"))
    assert (report.reruns, report.attempts) == (1, 0)
    cand = await candidate_of(session, advantex)
    assert ("E4", "hit") in trail(cand)
    advantex = await session.get(Company, advantex_id, populate_existing=True)
    assert advantex is not None
    assert (advantex.stir, advantex.registry_status) == (RECORDED_STIR, "active")
    assert await usable_phones(session, advantex)
    keys = set(
        await session.scalars(
            sa.select(Signal.key).where(Signal.company_id == advantex.id)
        )
    )
    assert {"vol_exhibitor", registry.LEGAL_CONFIRMED} <= keys


async def test_unut_leaves_nothing_of_an_enriched_directory_firm(
    session, wave5_sources, tmp_path
):
    """06 §5.4: the whole firm — its directory row's own evidence, the
    registry search by its name (which no observation cites), its card, the
    director, the signals and the candidate."""
    await collect(session, tmp_path)
    await settle(NOW)
    await enrich_at(tmp_path, NOW + enrich.SCHEDULE[0], registry_routes(ASM))
    asm = await company_named(session, ASM)
    search_pages = list(
        await session.scalars(
            sa.select(Evidence.id).where(
                Evidence.source_key == enrich.REGISTRY_SOURCE,
                Evidence.meta[evidence_store.FOR_COMPANY].astext == str(asm.id),
                ~sa.exists().where(Observation.evidence_id == Evidence.id),
            )
        )
    )
    assert search_pages, "the name search is evidence no observation cites"
    by_name = Evidence.url.ilike("%/search/organizations/%q=asm%alliance%")
    assert await session.scalar(sa.select(sa.func.count()).where(by_name)) == 1

    plan = await purge.plan_company(session, asm.id)
    await session.commit()
    await purge.execute(session, plan, requested_by="owner:1", now=NOW, root=tmp_path)
    await session.commit()

    left = {
        "observations": Observation.company_id == asm.id,
        "signals": Signal.company_id == asm.id,
        "contacts": Contact.company_id == asm.id,
        "candidates": LeadCandidate.company_id == asm.id,
        "evidence": Evidence.meta[evidence_store.FOR_COMPANY].astext == str(asm.id),
    }
    for what, where in left.items():
        table = {
            "observations": Observation,
            "signals": Signal,
            "contacts": Contact,
            "candidates": LeadCandidate,
            "evidence": Evidence,
        }[what]
        count = await session.scalar(
            sa.select(sa.func.count()).select_from(table).where(where)
        )
        assert count == 0, what
    # No page names it by its address any more.
    assert await session.scalar(sa.select(sa.func.count()).where(by_name)) == 0
    gone = await session.get(Company, asm.id, populate_existing=True)
    assert gone is not None and gone.state == "purged"


async def test_unut_takes_a_channel_e3_queued_out_of_the_reader_s_line(session, tmp_path):
    """04 §3 E3 queues a firm's channel for the reader when its public page is
    no channel preview (``chats.enqueue_username``, ``by=enrich:<candidate>``);
    a purge of the firm takes the name out of that line, or the joiner would
    resolve and join a purged firm's channel (06 §5.4)."""
    from tests.gate_fixtures import firm, signal
    from tests.test_enrich import enriching, site_page, world

    company = await firm(session, telegram_channel="namuna_person")
    await signal(session, company)
    cand = await enriching(session, company)
    other = await firm(session, "Namuna Boshqa", telegram_channel="namuna_boshqa")
    await session.commit()
    await chats.enqueue_username(
        session, "namuna_boshqa", priority=5, now=NOW, by="seed:other"
    )
    await session.commit()
    routes = {
        "https://t.me/s/namuna_person": httpx.Response(
            302, headers={"location": "https://t.me/namuna_person"}
        ),
        "https://t.me/namuna_person": site_page("tme_user"),
    }
    async with world(tmp_path, routes) as w:
        await w.run()
    flag = await session.get(RuntimeFlag, chats.PENDING_FLAG, populate_existing=True)
    assert flag is not None
    assert flag.value["names"]["namuna_person"]["by"] == f"{chats.BY_ENRICH}{cand.id}"

    plan = await purge.plan_company(session, company.id)
    assert plan.counts["chats"] == 1
    await session.commit()
    result = await purge.execute(
        session, plan, requested_by="owner:1", now=NOW, root=tmp_path
    )
    await session.commit()
    assert result.executed["chats"] == 1
    flag = await session.get(RuntimeFlag, chats.PENDING_FLAG, populate_existing=True)
    assert flag is not None
    assert "namuna_person" not in flag.value["names"]
    # Another firm's waiting name is not the subject's.
    assert "namuna_boshqa" in flag.value["names"]
    assert other.id != company.id


async def live_holders(session, pairs: list[tuple[str, str]]) -> list[str]:  # type: ignore[no-untyped-def]
    """The live (not purged) companies holding one of the platform ids, or
    named as either followed firm."""
    rows = await session.scalars(
        sa.select(Company.display_name)
        .outerjoin(CompanyId, CompanyId.company_id == Company.id)
        .where(
            Company.state != "purged",
            sa.or_(
                sa.tuple_(CompanyId.id_type, CompanyId.id_value).in_(pairs),
                Company.display_name.in_((ADVANTEX, EMA)),
            ),
        )
        .execution_options(populate_existing=True)
    )
    return sorted(set(rows))


ADVANTEX = "ADVANTEX, OOO"


@pytest.mark.parametrize("reason", ["legal_request", "dnc", "owner"])
async def test_a_purged_wave5_firm_is_not_made_again_by_the_next_run(
    session, wave5_sources, tmp_path, reason
):
    """06 §5.4: «the same company cannot be re-created by the next
    observation». A directory or exhibitor firm has no STIR, domain or
    channel for the purged row to keep — its platform id is the key the next
    run carries, so the purged root keeps its ``company_ids`` (company-tier,
    06 §5.1) and the resolver answers «purged» (the wave-5 seam review)."""
    await collect(session, tmp_path)
    await settle(NOW)
    ema = await company_named(session, EMA)
    advantex = await company_named(session, ADVANTEX)
    pairs = [
        (t, v)
        for t, v in (
            await session.execute(
                sa.select(CompanyId.id_type, CompanyId.id_value).where(
                    CompanyId.company_id.in_((ema.id, advantex.id))
                )
            )
        ).all()
    ]
    assert ("exhibitor_slug", "era:25848") in pairs
    assert any(t == "goldenpages" for t, _ in pairs)
    for firm_id in (ema.id, advantex.id):
        plan = await purge.plan_company(session, firm_id, reason=reason)
        await session.commit()
        await purge.execute(session, plan, requested_by="owner:1", now=NOW, root=tmp_path)
        await session.commit()
        # The kept ids are not a purge that stopped part-way.
        assert not await purge.unfinished(session, firm_id)
    assert await live_holders(session, pairs) == []

    # A week on, both collectors read the same rows again.
    clock = FakeClock(THURSDAY + timedelta(days=7))
    report = await run_collector(
        Directories,
        fetcher=fetcher_for(Web(clock).site),
        now=clock.now(),
        evidence_root=tmp_path,
        clock=clock.now,
    )
    assert report.status == "ok", report
    clock = FakeClock(EXHIBITORS_AT + timedelta(days=7))
    report = await run_collector(
        Exhibitors,
        fetcher=fetcher_for(caitme_site(clock)),
        now=clock.now(),
        evidence_root=tmp_path,
        clock=clock.now,
    )
    assert report.status == "ok", report
    await settle(NOW + timedelta(days=8))

    assert await live_holders(session, pairs) == []
    for firm_id in (ema.id, advantex.id):
        gone = await session.get(Company, firm_id, populate_existing=True)
        assert gone is not None and gone.state == "purged"
    # The rows were read again and refused, not simply absent from the run.
    refused = await session.scalars(
        sa.select(Observation.parsed["handles"]).where(
            Observation.status == "rejected", Observation.reject_reason == "purged"
        )
    )
    seen = {(t, v) for handles in refused for t, v in (handles or {}).items()}
    assert ("exhibitor_slug", "era:25848") in seen
    assert any(t == "goldenpages" and (t, v) in pairs for t, v in seen)


async def test_a_phone_bearing_directory_firm_reaches_the_card_and_the_payload(
    session, wave5_sources, tmp_path
):
    """The other half of the wave (the wave-5 seam review): a directory firm
    with a phone of its own, in a world where each firm's numbers are its own
    (``renumber_directory_phones``), goes found → scored → review on its own
    signals — JACK, the official distributor of a Chinese brand on glotr —
    and its owner card and ERP payload carry its phone, its card's URL, the
    page's date and a verbatim quote of the card."""
    await collect(session, tmp_path)
    await settle(NOW)
    jack = await company_named(session, JACK)
    cand = await candidate_of(session, jack)
    assert (cand.state, cand.tier) == ("review", "hot")
    [phone] = await usable_phones(session, jack)
    # A number ``renumber_directory_phones`` gave (``… 20000 NN`` under 93–99).
    assert phone.phone is not None and phone.phone[6:11] == "20000"
    keys = set(
        await session.scalars(sa.select(Signal.key).where(Signal.company_id == jack.id))
    )
    assert {"cn_origin", "vol_container", "vol_marketplace_velocity"} <= keys

    view = await candidates.load_view(session, cand.id, now=NOW)
    cited, quote = crm_push.cited(view)
    assert cited.url == "https://glotr.uz/showcase/jack/"
    assert cited.source_key == "directories_glotr"
    assert quote and quote in cited.excerpt
    payload = crm_push.build_payload(view)
    assert payload.phone == phone.phone
    assert payload.firma == JACK  # the form once (the name carries ООО)
    first, url = payload.note.split("\n")[:2]
    day = cited.published_at or cited.fetched_at
    assert first.startswith("NEGA TOPILDI: ")
    assert day.astimezone(TZ).strftime("%d.%m.%Y") in first
    assert f"«{quote}»" in first
    assert url == cited.url

    gate = await gates.evaluate(session, jack.id, now=NOW, tails=[phone.phone_tail])
    card = cards.render_review(
        cards.ReviewView(review_id=1, candidate=view, gate=gate, phone_kind="mobile")
    ).text
    assert cards.esc(JACK) in card and phone.phone[-2:] in card
    assert cited.url in card


async def test_a_directory_card_with_a_chinese_number_is_excluded_as_a_supplier(
    session, wave5_sources
):
    """04 §6 gate 3 «phone +86», through the resolver and the scorer: a
    directory card that lists a Chinese number beside its Uzbek one ends
    ``excluded(supplier_cn)`` instead of going to enrichment (the wave-5
    seam review: the resolver keeps Uzbek numbers only, so the branch that
    read contacts never fired)."""
    from tests.companies_fixtures import observe, parsed

    body = parsed(
        name='"NAMUNA TEXNIKA" ООО',
        ids={"goldenpages": "990001"},
        city="Toshkent",
        phones=(("+8613800138000", "company"), ("+998900000089", "company")),
        url="https://www.goldenpages.uz/company/?Id=990001",
    )
    body["text"]["excerpt"] = '"NAMUNA TEXNIKA" ООО · Оптово-розничная торговля'
    body["hints"]["collector"] = {
        "signal_types": ["directory_wholesale"],
        "attrs": {"site": "goldenpages", "bulky": ["equipment"]},
    }
    await observe(session, body)
    await session.commit()
    await settle(NOW)
    firm = await company_named(session, '"NAMUNA TEXNIKA" ООО')
    cand = await candidate_of(session, firm)
    assert (cand.state, cand.exclude_reason) == ("excluded", "supplier_cn")
    assert firm.is_supplier_cn
    keys = set(
        await session.scalars(sa.select(Signal.key).where(Signal.company_id == firm.id))
    )
    assert "flag_supplier_cn" in keys
