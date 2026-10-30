"""services/registry (04 §3 E4/E4′, 07 T-24): orginfo.uz over its recorded
pages, the plan robots.txt allows, and the host chain.

The orginfo parsers read the scrubbed recordings of
``tests/fixtures/registries/`` (their README is the record of what each file
proves): the director there is «Seller 04», the phones ``9000000NN``. The
three other hosts have no recorded page: they are configured-off adapters,
and the chain is driven here with a fake host. No request leaves the test
(``tests/fakes.Site`` answers 404 to anything it was not given).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import httpx
import pytest

from clientscrapper.config import settings
from clientscrapper.services import registry
from clientscrapper.services.http import ROBOTS_AGENT, Fetcher, PageBudget
from clientscrapper.services.normalise import to_observation_json
from clientscrapper.services.robots import parse_robots
from clientscrapper.services.signals import EvidenceRef, from_rules
from tests.fakes import FakeClock, Site, budget, fetcher_for

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=TZ)
FIXTURES = Path(__file__).parent / "fixtures" / "registries"
CARD = "https://orginfo.uz/ru/organization/cf68f132a2f4/"
SEARCH = "https://orginfo.uz/ru/search/organizations/"
# The recorded card's legal address (Qo'qon, Fargʻona), in its two spellings
# on the page: a test that dresses the card as another firm moves it too.
FERGANA_ENTITY = "Farg&#x27;ona viloyati, Qo&#x27;qon sh."
FERGANA = "Farg'ona viloyati, Qo'qon sh."


def page(name: str) -> bytes:
    return (FIXTURES / f"{name}.html").read_bytes()


def robots() -> str:
    return page("orginfo_robots").decode()


def html(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=page(name), headers={"content-type": "text/html; charset=utf-8"}
    )


# --- the company page -----------------------------------------------------------------


def test_the_company_page_gives_every_registry_fact_exactly():
    record = registry.parse_orginfo_company(
        page("orginfo_organization_cf68f132a2f4"), url=CARD
    )
    assert record == registry.RegistryRecord(
        host="orginfo.uz",
        url=CARD,
        stir="310514454",
        official_name='"ORGINFO" mas`uliyati cheklangan jamiyati',
        short_name='ООО "ORGINFO"',
        status="active",
        status_text="Активный",
        registered_on=date(2023, 5, 27),
        oked="63110",
        oked_text="Услуги по размещению и обработке данных",
        opf="152 - Общество с ограниченной или дополнительной ответственностью",
        address="Farg'ona viloyati, Qo'qon sh. Guzar ko'chasi, 2-uy",
        phones=("900000001",),
        director_name="Seller 04",
        director_role="Руководитель",
        data_as_of=date(2026, 6, 15),
        ref="cf68f132a2f4",
    )
    assert (record.legal_form, record.city) == ("MChJ", "Fargʻona")


def test_the_stir_is_never_read_from_the_similar_organisations_badges():
    """README «Scrubbing»: the cards at the foot carry 9-digit badges
    (``900000002``…) that are STIRs of other firms. Without the ИНН row and
    the JSON-LD ``taxID`` the page has no STIR — the badges are not one."""
    body = page("orginfo_organization_cf68f132a2f4").decode()
    assert "900000002" in body
    no_tin = body.replace('id="organizationTinValue"', 'id="x"').replace(
        '"taxID": "310514454"', '"taxID": ""'
    )
    assert registry.parse_orginfo_company(no_tin.encode(), url=CARD) is None
    only_json = body.replace('id="organizationTinValue"', 'id="x"')
    found = registry.parse_orginfo_company(only_json.encode(), url=CARD)
    assert found is not None and found.stir == "310514454"


def test_the_missing_page_is_no_record():
    assert (
        registry.parse_orginfo_company(
            page("orginfo_organization_missing_negative"),
            url="https://orginfo.uz/ru/organization/000000000000/",
        )
        is None
    )


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Активный", "active"),
        ("Ликвидирована", "liquidated"),
        ("Ликвидирован", "liquidated"),
        ("В процессе ликвидации", "inactive"),
        ("Неактивный", "inactive"),
        ("Faol", "active"),
        ("Faol emas", "inactive"),
        ("Tugatilgan", "liquidated"),
        ("", "unknown"),
        ("—", "unknown"),
    ],
)
def test_status_words(text, status):
    assert registry.status_of(text) == status


# --- the search pages -------------------------------------------------------------------


def test_the_stir_search_is_one_card():
    cards = registry.parse_orginfo_search(
        page("orginfo_search_stir_310514454"), url=f"{SEARCH}?q=310514454"
    )
    assert cards == [
        registry.RegistryCard(
            url=CARD,
            stir="310514454",
            name='"ORGINFO" mas`uliyati cheklangan jamiyati',
            registered_on=date(2023, 5, 27),
            liquidated=False,
            address="Farg'ona viloyati, Qo'qon sh. Guzar ko'chasi, 2-uy",
        )
    ]


def test_the_name_search_pages_give_ten_cards_and_the_liquidated_badge():
    p1 = registry.parse_orginfo_search(
        page("orginfo_search_name_xitoy_newest_p1"), url=f"{SEARCH}?q=xitoy"
    )
    p2 = registry.parse_orginfo_search(
        page("orginfo_search_name_xitoy_newest_p2"), url=f"{SEARCH}?q=xitoy&page=2"
    )
    assert [c.stir for c in p1] == [
        "313393367",
        "313378012",
        "313343483",
        "313101404",
        "313094525",
        "313085149",
        "313064507",
        "312951685",
        "312872665",
        "312852745",
    ]
    assert {c.stir for c in p1 if c.liquidated} == {"313101404", "312951685", "312872665"}
    assert len(p2) == 10 and not {c.stir for c in p1} & {c.stir for c in p2}
    first = p1[1]
    assert (first.name, first.registered_on, first.city) == (
        '"XITOY TOSHKENT" mas`uliyati cheklangan jamiyati',
        date(2026, 10, 2),
        "Toshkent",
    )
    assert all(registry.is_card_url(c.url) for c in p1 + p2)


def test_e4_prime_takes_one_card_whose_name_and_city_agree():
    cards = registry.parse_orginfo_search(
        page("orginfo_search_name_xitoy_newest_p1"), url=SEARCH
    )
    result, card = registry.pick_by_name(cards, "XITOY TOSHKENT MChJ", city="Toshkent")
    assert (result, card.stir if card else None) == ("hit", "313378012")
    # The company knows another city: none. It knows none: none either —
    # 04 §3 «name + city must agree, else none»: a same-named firm anywhere
    # in the country would give ours its STIR and its director.
    assert registry.pick_by_name(cards, "Xitoy Toshkent", city="Buxoro") == ("none", None)
    assert registry.pick_by_name(cards, "Xitoy Toshkent", city=None) == ("none", None)
    # orginfo's fuzzy match («xitoy» finds «XIDOY») is no agreement.
    assert registry.pick_by_name(cards, "Qodirbobo Xitoy Zamini", city="Toshkent") == (
        "none",
        None,
    )
    # A card with no STIR, or with no address, agrees with nothing.
    stirless = replace(cards[1], stir=None)
    assert registry.pick_by_name([stirless], "Xitoy Toshkent", city="Toshkent") == (
        "none",
        None,
    )
    homeless = replace(cards[1], address=None)
    assert registry.pick_by_name([homeless], "Xitoy Toshkent", city="Toshkent") == (
        "none",
        None,
    )
    # A card the search list marks liquidated is no candidate (an older firm
    # under the same name): alone it is none, beside a live one the live one.
    dead = replace(cards[1], stir="313378013", liquidated=True)
    assert registry.pick_by_name([dead], "Xitoy Toshkent", city="Toshkent") == (
        "none",
        None,
    )
    assert registry.pick_by_name([dead, cards[1]], "Xitoy Toshkent", city="Toshkent") == (
        "hit",
        cards[1],
    )
    # Two firms of one name: ambiguous, nothing (04 §3 «≥ 2 hits → none»).
    twin = [cards[1], replace(cards[1], stir="313378013")]
    assert registry.pick_by_name(twin, "Xitoy Toshkent", city="Toshkent") == (
        "ambiguous",
        None,
    )


# --- the plan robots.txt allows ----------------------------------------------------


def test_every_url_the_plan_builds_is_allowed_and_the_refused_routes_never_are():
    rules = parse_robots(robots(), ROBOTS_AGENT)
    cards = [
        c
        for name in (
            "orginfo_search_stir_310514454",
            "orginfo_search_name_xitoy_newest_p1",
            "orginfo_search_name_xitoy_newest_p2",
        )
        for c in registry.parse_orginfo_search(page(name), url=SEARCH)
    ]
    planned = [
        registry.search_url("310514454"),
        registry.search_url("namuna tekstil savdo"),
        *(c.url for c in cards),
        registry.card_url("cf68f132a2f4"),
    ]
    assert all(rules.allows(u) for u in planned)
    refused = [
        "https://orginfo.uz/api/organizations/cf68f132a2f4/update-request/",
        "https://orginfo.uz/ru/organization/cf68f132a2f4/pdf/",
        "https://orginfo.uz/ru/search/managers/?q=Seller%2004",
        "https://orginfo.uz/ru/search/founders/?q=Seller%2004",
        "https://orginfo.uz/ru/search/phones/?q=900000001",
        "https://orginfo.uz/ru/organization/cf68f132a2f4/?tab=x",
    ]
    assert [u for u in refused if registry.is_card_url(u)] == []
    assert not rules.allows(refused[0]) and not rules.allows(refused[1])


def test_a_card_link_that_is_not_the_company_page_shape_is_not_followed():
    body = (
        page("orginfo_search_stir_310514454")
        .decode()
        .replace(
            'href="/ru/organization/cf68f132a2f4/" class',
            'href="/ru/organization/cf68f132a2f4/pdf/" class',
        )
    )
    assert registry.parse_orginfo_search(body.encode(), url=SEARCH) == []


# --- what a hit says -----------------------------------------------------------------


def _record(**changes) -> registry.RegistryRecord:
    found = registry.parse_orginfo_company(
        page("orginfo_organization_cf68f132a2f4"), url=CARD
    )
    assert found is not None
    return replace(found, **changes)


def test_the_hit_as_an_observation_carries_the_director_with_his_phone():
    record = _record()
    seen = registry.observation_of(record, fetched_at=NOW, sha256="0" * 64)
    parsed = registry.phones_to_person(to_observation_json(seen))
    assert parsed["kind"] == "registry"
    assert parsed["company"]["stir"] == "310514454"
    assert parsed["company"]["legal_form"] == "MChJ"
    assert parsed["person"] == {"name": "Seller 04", "role": "Руководитель"}
    assert [p["owner"] for p in parsed["phones"]] == ["person"]
    assert parsed["phones"][0]["e164"] == "+998900000001"
    # The excerpt carries no name and no OKED words: no rule signal of its own.
    assert (
        parsed["text"]["excerpt"] == "ИНН 310514454 · Активный · 27.05.2023 · ОКЭД 63110"
    )
    ref = EvidenceRef(id=1, source_key=registry.SOURCE_KEY, fetched_at=NOW)
    assert from_rules(ref, parsed, registry_facts()) == []


def registry_facts():
    from clientscrapper.services.signals import CompanyFacts

    return CompanyFacts()


def test_without_a_director_the_phone_is_the_company_s():
    seen = registry.observation_of(
        _record(director_name=None, director_role=None), fetched_at=NOW, sha256="0" * 64
    )
    parsed = registry.phones_to_person(to_observation_json(seen))
    assert parsed["person"] is None
    assert [p["owner"] for p in parsed["phones"]] == ["company"]


def test_a_hit_writes_legal_confirmed_and_the_age_only_when_active_and_old():
    ref = EvidenceRef(id=7, source_key=registry.SOURCE_KEY, fetched_at=NOW)
    old = registry.hit_drafts(_record(), ref, now=NOW)
    assert [d.key for d in old] == ["ent_legal_confirmed", "ent_age_2y_active"]
    assert old[0].quote == "ИНН 310514454" and old[1].quote == "27.05.2023"
    young = _record(registered_on=(NOW - timedelta(days=729)).date())
    assert [d.key for d in registry.hit_drafts(young, ref, now=NOW)] == [
        "ent_legal_confirmed"
    ]
    gone = _record(status="liquidated", status_text="Ликвидирована")
    assert [d.key for d in registry.hit_drafts(gone, ref, now=NOW)] == [
        "ent_legal_confirmed"
    ]


def test_a_liquidated_hit_gives_neg_liquidated_through_its_type():
    gone = _record(status="liquidated", status_text="Ликвидирована")
    parsed = to_observation_json(
        registry.observation_of(gone, fetched_at=NOW, sha256="0" * 64)
    )
    ref = EvidenceRef(id=1, source_key=registry.SOURCE_KEY, fetched_at=NOW)
    assert [d.key for d in from_rules(ref, parsed, registry_facts())] == [
        "neg_liquidated"
    ]


def test_company_values_take_the_registry_s_word_and_fill_only_the_empty():
    blank = registry.CompanyNow("unknown", None, None, None, None, None)
    assert registry.company_values(_record(), blank) == {
        "registry_status": "active",
        "registered_on": date(2023, 5, 27),
        "oked": "63110",
        "legal_form": "MChJ",
        "address": "Farg'ona viloyati, Qo'qon sh. Guzar ko'chasi, 2-uy",
        "city": "Fargʻona",
    }
    held = registry.CompanyNow(
        "active", date(2023, 5, 27), "63110", "XK", "Toshkent, X koʻchasi", "Toshkent"
    )
    assert registry.company_values(_record(), held) == {}
    other = registry.CompanyNow("unknown", None, None, "other", None, "Toshkent")
    assert registry.company_values(_record(), other)["legal_form"] == "MChJ"


# --- the hosts ----------------------------------------------------------------------


def orginfo_site(clock: FakeClock, **routes) -> Site:
    return Site(
        clock,
        {
            "https://orginfo.uz/robots.txt": httpx.Response(200, text=robots()),
            SEARCH: html("orginfo_search_stir_310514454"),
            CARD: html("orginfo_organization_cf68f132a2f4"),
            **routes,
        },
    )


async def test_e4_is_a_search_then_the_card_and_nothing_else():
    clock = FakeClock(NOW)
    site = orginfo_site(clock)
    async with fetcher_for(site) as fetcher:
        found = await registry.lookup(
            registry.hosts_on({}),
            stir="310514454",
            name=None,
            city=None,
            fetcher=fetcher,
            budget=budget(pages=10, deadline=NOW + timedelta(hours=1)),
        )
    assert (found.result, found.host) == ("hit", "orginfo")
    assert found.record is not None and found.record.stir == "310514454"
    assert [s.url for s in site.seen] == [
        "https://orginfo.uz/robots.txt",
        f"{SEARCH}?q=310514454",
        CARD,
    ]
    assert [p.url for p in found.pages] == [f"{SEARCH}?q=310514454", CARD]
    # 1 request every 3 s or slower (04 §3 «1 req/3 s»).
    gaps = [b.at - a.at for a, b in zip(site.seen[1:], site.seen[2:], strict=False)]
    assert all(g >= 3.0 for g in gaps)


async def test_e4_another_stir_is_none_and_the_card_is_not_fetched():
    clock = FakeClock(NOW)
    site = orginfo_site(clock)
    async with fetcher_for(site) as fetcher:
        found = await registry.lookup(
            registry.hosts_on({}),
            stir="310514455",
            name=None,
            city=None,
            fetcher=fetcher,
            budget=budget(pages=10, deadline=NOW + timedelta(hours=1)),
        )
    assert (found.result, found.record) == ("none", None)
    assert CARD not in [s.url for s in site.seen]


async def test_e4_prime_by_name_reads_one_search_and_the_agreeing_card():
    clock = FakeClock(NOW)
    site = orginfo_site(
        clock,
        **{
            SEARCH: html("orginfo_search_name_xitoy_newest_p1"),
            "https://orginfo.uz/ru/organization/e9beed368894/": httpx.Response(
                200,
                content=page("orginfo_organization_cf68f132a2f4")
                .decode()
                .replace("310514454", "313378012")
                .replace("ORGINFO", "XITOY TOSHKENT")
                .replace(FERGANA_ENTITY, "Toshkent sh.")
                .replace(FERGANA, "Toshkent sh.")
                .encode(),
            ),
        },
    )
    async with fetcher_for(site) as fetcher:
        found = await registry.lookup(
            registry.hosts_on({}),
            stir=None,
            name="Xitoy Toshkent MChJ",
            city="Toshkent",
            fetcher=fetcher,
            budget=budget(pages=10, deadline=NOW + timedelta(hours=1)),
        )
    assert (found.result, found.record.stir if found.record else None) == (
        "hit",
        "313378012",
    )
    assert site.seen[1].url == f"{SEARCH}?q=xitoy%20toshkent"


class FakeHost(registry.RegistryHost):
    """A built host answering from a script (stat/soliq/myorg have no
    recorded page: the chain is shown on this one)."""

    key = "fake"
    host = "registry.example.uz"
    answers: ClassVar[list[str]] = []

    async def by_stir(
        self, stir: str, *, fetcher: Fetcher, budget: PageBudget
    ) -> registry.Answer:
        self.answers.append(stir)
        record = registry.RegistryRecord(
            host=self.host, url="https://registry.example.uz/x", stir=stir
        )
        return registry.Answer("hit", record, host=self.host)

    async def by_name(
        self, name: str, *, city: str | None, fetcher: Fetcher, budget: PageBudget
    ) -> registry.Answer:
        return registry.Answer("none", host=self.host)


async def test_a_captcha_rests_orginfo_and_the_next_host_answers():
    clock = FakeClock(NOW)
    captcha = httpx.Response(
        200, text="<html><body>captcha: Are you a robot?</body></html>"
    )
    site = orginfo_site(clock, **{SEARCH: captcha})
    FakeHost.answers = []
    async with fetcher_for(site) as fetcher:
        found = await registry.lookup(
            [registry.Orginfo(), registry.StatUz(), FakeHost()],
            stir="310514454",
            name=None,
            city=None,
            fetcher=fetcher,
            budget=budget(pages=10, deadline=NOW + timedelta(hours=1)),
        )
    assert (found.result, found.host, found.rest) == ("hit", "fake", ["orginfo"])
    assert found.tried == [("orginfo", "captcha"), ("stat", "not_built"), ("fake", "hit")]
    assert FakeHost.answers == ["310514454"]
    assert found.pages == []  # a captcha page is no evidence of the firm


async def test_all_hosts_down_is_registry_unavailable_and_a_resting_host_is_skipped():
    clock = FakeClock(NOW)
    site = orginfo_site(clock, **{SEARCH: httpx.Response(403, text="no")})
    async with fetcher_for(site) as fetcher:
        found = await registry.lookup(
            [registry.Orginfo(), registry.MySoliq(), registry.MyOrg()],
            stir="310514454",
            name=None,
            city=None,
            fetcher=fetcher,
            budget=budget(pages=10, deadline=NOW + timedelta(hours=1)),
        )
        assert (found.result, found.rest) == (registry.UNAVAILABLE, ["orginfo"])
        before = len(site.seen)
        rested = await registry.lookup(
            [registry.Orginfo()],
            stir="310514454",
            name=None,
            city=None,
            fetcher=fetcher,
            budget=budget(pages=10, deadline=NOW + timedelta(hours=1)),
            paused=frozenset({"orginfo"}),
        )
    assert (rested.result, rested.tried) == (
        registry.UNAVAILABLE,
        [("orginfo", "paused")],
    )
    assert len(site.seen) == before


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("no answer", request=request)


async def test_a_host_that_does_not_answer_is_no_answer_and_rests_nothing():
    """A timeout, DNS or a reset after the Fetcher's retries is the host
    unable to say (``unreachable``): the chain asks the next host and ends
    ``registry_unavailable`` (04 §3, retried at the next attempt) — never an
    exception out of the lookup, and no 24 h rest (that is a captcha's or a
    403's)."""
    clock = FakeClock(NOW)
    site = orginfo_site(clock, **{SEARCH: _timeout})
    async with fetcher_for(site) as fetcher:
        found = await registry.lookup(
            [registry.Orginfo(), registry.MyOrg()],
            stir="310514454",
            name=None,
            city=None,
            fetcher=fetcher,
            budget=budget(pages=10, deadline=NOW + timedelta(hours=1)),
        )
    assert (found.result, found.rest, found.pages) == (registry.UNAVAILABLE, [], [])
    assert found.tried == [("orginfo", "unreachable"), ("myorg", "not_built")]


async def test_a_programming_error_in_a_host_is_not_swallowed():
    class Broken(registry.Orginfo):
        async def by_stir(self, stir, *, fetcher, budget):
            answer = registry.Answer("none", host=self.host)
            await self._get("not a url", answer, fetcher=fetcher, budget=budget)
            return answer

    async with fetcher_for(Site(FakeClock(NOW))) as fetcher:
        with pytest.raises(ValueError):
            await registry.lookup(
                [Broken()],
                stir="310514454",
                name=None,
                city=None,
                fetcher=fetcher,
                budget=budget(pages=10, deadline=NOW + timedelta(hours=1)),
            )


async def test_the_registry_pace_is_its_own_under_a_raised_default(monkeypatch):
    """04 §3 «1 req/3 s» is orginfo's own rate (``HOST_RPS``), not the
    global default's, which the owner may raise to 5."""
    monkeypatch.setattr(settings, "collector_rps_default", 2.0)
    clock = FakeClock(NOW)
    site = orginfo_site(clock)
    async with fetcher_for(site) as fetcher:
        found = await registry.lookup(
            registry.hosts_on({}),
            stir="310514454",
            name=None,
            city=None,
            fetcher=fetcher,
            budget=budget(pages=10, deadline=NOW + timedelta(hours=1)),
        )
    assert found.result == "hit"
    gaps = [b.at - a.at for a, b in zip(site.seen[1:], site.seen[2:], strict=False)]
    assert gaps and all(g >= 3.0 for g in gaps)


async def test_e4_prime_asks_nothing_for_a_company_with_no_city():
    clock = FakeClock(NOW)
    site = orginfo_site(clock)
    async with fetcher_for(site) as fetcher:
        found = await registry.lookup(
            registry.hosts_on({}),
            stir=None,
            name="Xitoy Toshkent MChJ",
            city=None,
            fetcher=fetcher,
            budget=budget(pages=10, deadline=NOW + timedelta(hours=1)),
        )
    assert (found.result, found.pages) == ("none", [])
    assert site.seen == []


async def test_e4_prime_a_card_page_in_another_city_is_a_mismatch():
    """The search card agreed (Toshkent); the company page it leads to names
    another city: not the firm the rule agreed to."""
    clock = FakeClock(NOW)
    site = orginfo_site(
        clock,
        **{
            SEARCH: html("orginfo_search_name_xitoy_newest_p1"),
            "https://orginfo.uz/ru/organization/e9beed368894/": httpx.Response(
                200,
                content=page("orginfo_organization_cf68f132a2f4")
                .decode()
                .replace("310514454", "313378012")
                .replace("ORGINFO", "XITOY TOSHKENT")
                .encode(),
            ),
        },
    )
    async with fetcher_for(site) as fetcher:
        found = await registry.lookup(
            registry.hosts_on({}),
            stir=None,
            name="Xitoy Toshkent MChJ",
            city="Toshkent",
            fetcher=fetcher,
            budget=budget(pages=10, deadline=NOW + timedelta(hours=1)),
        )
    assert (found.result, found.record) == ("mismatch", None)


async def test_the_unbuilt_hosts_fetch_nothing():
    clock = FakeClock(NOW)
    site = Site(clock)
    async with fetcher_for(site) as fetcher:
        for host in (registry.StatUz(), registry.MySoliq(), registry.MyOrg()):
            assert not host.built
            got = await host.by_stir("310514454", fetcher=fetcher, budget=budget())
            assert got.result == "not_built"
            named = await host.by_name(
                "Namuna", city=None, fetcher=fetcher, budget=budget()
            )
            assert named.result in ("not_built", "not_supported")
    assert site.seen == []


def test_hosts_on_and_paused_hosts_read_the_source_config():
    keys = [h.key for h in registry.hosts_on({})]
    assert keys == ["orginfo"]
    assert [
        h.key for h in registry.hosts_on({"hosts": {"orginfo": False, "stat": True}})
    ] == ["stat"]
    later = (NOW + timedelta(hours=1)).isoformat()
    earlier = (NOW - timedelta(hours=1)).isoformat()
    config = {"paused_hosts": {"orginfo": later, "stat": earlier, "x": "garbage"}}
    assert registry.paused_hosts(config, now=NOW) == frozenset({"orginfo"})
