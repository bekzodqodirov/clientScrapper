"""goldenpages.uz parsers on the recorded pages (03 §1.5, §2.6; T-22).

Every expected value below is read off the recorded files
(tests/fixtures/directories/README.md «goldenpages.uz»): phones are the
scrubbed synthetic ``(+99890) 00000NN``; list phones are masks and must never
come out as phones.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.directories import goldenpages as gp
from clientscrapper.collectors.directories import stop_reason
from clientscrapper.services.normalise import to_observation_json
from tests.collectors.directories_pages import GOLDENPAGES, page

TASHKENT = ZoneInfo("Asia/Tashkent")
P1_IDS = [
    "73659",
    "113536",
    "89002",
    "109496",
    "100445",
    "45214",
    "53976",
    "107742",
    "110076",
    "73054",
    "89178",
    "91647",
    "79648",
    "98489",
    "89481",
    "109719",
    "108810",
    "63826",
    "48003",
    "85641",
]
P2_IDS = [
    "40840",
    "65765",
    "115282",
    "116694",
    "113583",
    "43099",
    "46128",
    "122938",
    "116663",
    "76683",
    "121285",
    "61971",
    "76710",
    "115978",
    "70570",
    "120549",
    "84436",
    "40169",
    "46990",
    "98135",
]
BADGES = ["Производитель", "Экспортёр"]
CHINA_IDS = ["76768", "78697", "62755", "108133", "96147", "6264", "88370", "73256"]


def listing(name: str):
    p = page(name)
    return gp.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at)


def test_the_card_gives_the_json_ld_phones_and_the_card_s_own_fields():
    p = page("goldenpages_company_85641")
    [obs] = gp.parse_company(p.body, url=p.final_url, fetched_at=p.fetched_at)
    rubrics = [
        "Низковольтное оборудование",
        "Оптово-розничная торговля",
        "Продукция Chint Electric - продажа",
        "Электрощиты, электрошкафы",
    ]
    about = (
        'ООО "CHINT DISTRIBUTION" занимается производством и продажей '
        "низковольтного оборудования, включая электрощиты и электрошкафы, а также "
        "оптово-розничной торговлей электротехнической продукцией."
    )
    assert obs == Observation(
        source="directories_goldenpages",
        source_ref="goldenpages:85641",
        url="https://www.goldenpages.uz/company/?Id=85641",
        fetched_at=p.fetched_at,
        raw_sha256=p.sha256,
        kind="company",
        company_name_raw='"CHINT DISTRIBUTION" ООО',
        legal_form="ООО",
        phones=["(+99890) 0000001", "(+99890) 0000002"],
        address="100057, Ташкент, Алмазарский район, ул. Уста Ширин, 136",
        city="Ташкент",
        goods_text=" · ".join([*rubrics, about]),
        signal_types=["directory_wholesale"],
        excerpt=" · ".join(['"CHINT DISTRIBUTION" ООО', *BADGES, *rubrics, about]),
        external_ids={"goldenpages": "85641"},
        attrs={
            "site": "goldenpages",
            "rubrics": rubrics,
            "badges": BADGES,
            "updated_on": "2025-12-11",
            "bulky": ["equipment"],
        },
        published_at=datetime(2025, 12, 11, tzinfo=TASHKENT),
        lang="ru",
    )
    # The normaliser reads the numbers as written; the masked «+998 (90)
    # 000-XX-XX» and the site's help-desk «90 000 00 03» are not among them.
    parsed = to_observation_json(obs)
    assert [ph["e164"] for ph in parsed["phones"]] == ["+998900000001", "+998900000002"]
    assert not any("XX" in phone for phone in obs.phones)


def test_the_card_s_help_desk_numbers_are_on_the_page_and_never_read():
    p = page("goldenpages_company_85641")
    assert b'href="tel:900000003"' in p.body  # the header's help desk
    [obs] = gp.parse_company(p.body, url=p.final_url, fetched_at=p.fetched_at)
    assert all("0000003" not in phone for phone in obs.phones)


def test_a_card_updated_before_2024_is_dropped():
    p = page("goldenpages_company_85641")
    stale = p.body.replace(
        "Обновлено: 11.12.2025".encode(), "Обновлено: 11.12.2023".encode()
    )
    assert stale != p.body
    assert gp.parse_company(stale, url=p.final_url, fetched_at=p.fetched_at) == []


def test_the_rubric_page_lists_its_companies_without_phones():
    p = page("goldenpages_rubric_1424_p1")
    listed = gp.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at)
    assert [e.ref for e in listed.entries] == [f"goldenpages:{i}" for i in P1_IDS]
    assert [e.card_url for e in listed.entries] == [
        f"https://www.goldenpages.uz/company/?Id={i}" for i in P1_IDS
    ]
    assert (listed.next_url, listed.total, listed.page) == (
        "https://www.goldenpages.uz/rubrics/?Id=1424&Page=2",
        74,
        1,
    )
    # The list prints masks («+998 (90) 000-XX-XX»): no row carries a phone.
    assert b"000-XX-XX" in p.body
    assert all(
        e.observation is not None and e.observation.phones == [] for e in listed.entries
    )
    assert not any(e.complete for e in listed.entries)
    first = listed.entries[0].observation
    assert first == Observation(
        source="directories_goldenpages",
        source_ref="goldenpages:73659",
        url="https://www.goldenpages.uz/rubrics/?Id=1424",
        fetched_at=p.fetched_at,
        raw_sha256=p.sha256,
        kind="company",
        company_name_raw='"ENERGOPRIBOR" ООО',
        legal_form="ООО",
        address="100069, Ташкент, Алмазарский район, ул. Шимолий Олмазор, 4/1",
        goods_text=(
            "Оптово-розничная торговля · Контрольно-измерительные приборы и "
            "автоматика оптом и в розницу"
        ),
        excerpt=(
            '"ENERGOPRIBOR" ООО · Оптово-розничная торговля · Контрольно-'
            "измерительные приборы и автоматика оптом и в розницу"
        ),
        external_ids={"goldenpages": "73659"},
        attrs={
            "site": "goldenpages",
            "card_url": "https://www.goldenpages.uz/company/?Id=73659",
            "rubrics": ["Оптово-розничная торговля"],
            "badges": [],
            "list_row": True,
            "hold": "category",
        },
        lang="ru",
    )
    assert gp.parse_rubric(p.body, url=p.final_url, fetched_at=p.fetched_at) == [
        e.observation for e in listed.entries
    ]


def test_the_china_rubric_gives_china_origin_and_the_importer_badge_a_distributor():
    listed = listing("goldenpages_rubric_106080_p1")
    assert [e.ref.split(":")[1] for e in listed.entries] == CHINA_IDS
    assert (listed.next_url, listed.total) == (None, 8)
    rows = {e.ref: e.observation for e in listed.entries}
    ema = rows["goldenpages:76768"]
    assert ema is not None
    assert (ema.company_name_raw, ema.signal_types, ema.origin_text) == (
        '"EMA LEASING" ООО',
        ["china_origin", "brand_distributor"],
        "Оборудование из Китая - продажа, монтаж, установка",
    )
    assert ema.attrs["badges"] == ["Импортер", "Дистрибьютор"]
    assert ema.attrs["official"] is False
    # Every row of the rubric carries the China cue; only importers distribute.
    assert all(o and "china_origin" in o.signal_types for o in rows.values())
    distributors = [
        r for r, o in rows.items() if o and "brand_distributor" in o.signal_types
    ]
    assert distributors == ["goldenpages:76768", "goldenpages:96147"]
    # Not a wholesale rubric: no directory_wholesale on any row.
    assert not any(o and "directory_wholesale" in o.signal_types for o in rows.values())


def test_page_two_is_capital_p_and_holds_twenty_other_companies():
    listed = listing("goldenpages_rubric_1424_p2")
    assert [e.ref.split(":")[1] for e in listed.entries] == P2_IDS
    assert not set(P1_IDS) & set(P2_IDS)
    assert listed.next_url == "https://www.goldenpages.uz/rubrics/?Id=1424&Page=3"
    assert listed.page == 2


def test_a_page_past_the_last_stops_the_paginator_both_ways():
    """Page=99 redirects to /rubrics/?Id=1424 (the sidecar's final URL) and
    repeats page 1's first card id: either rule stops the seed."""
    p = page("goldenpages_rubric_1424_p99_negative")
    assert (p.url, p.final_url) == (
        "https://www.goldenpages.uz/rubrics/?Id=1424&Page=99",
        "https://www.goldenpages.uz/rubrics/?Id=1424",
    )
    past = gp.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at)
    assert past.entries[0].ref == "goldenpages:73659"
    # The final URL dropped Page.
    assert gp.redirect_stop(p.url, p.final_url) == "redirected"
    assert stop_reason(
        past, spec=gp.SPEC, asked=p.url, final=p.final_url, firsts=set()
    ) == ("redirected")
    # The first card id repeats (as if the site had answered page 99 in place).
    seen = {listing("goldenpages_rubric_1424_p1").entries[0].ref}
    assert (
        stop_reason(past, spec=gp.SPEC, asked=p.url, final=p.url, firsts=seen) == "repeat"
    )
    # Page 2 after page 1 is neither.
    two = page("goldenpages_rubric_1424_p2")
    following = listing("goldenpages_rubric_1424_p2")
    assert (
        stop_reason(
            following, spec=gp.SPEC, asked=two.url, final=two.final_url, firsts=seen
        )
        is None
    )


@pytest.mark.parametrize("name", GOLDENPAGES)
def test_the_bot_trap_is_on_every_page_and_no_gp_link_is_ever_queued(name):
    """The hidden /gp/ links (tag and style change on every response) are in
    every recorded file; the plan queues only the selector-matched hrefs."""
    p = page(name)
    assert len(re.findall(rb'href="/gp/', p.body)) == 4
    listed = gp.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at)
    queued = [e.card_url for e in listed.entries] + [listed.next_url]
    for url in filter(None, queued):
        assert "/gp/" not in url
        path = url.removeprefix(gp.BASE)
        assert gp.CARD_HREF.match(path) or gp.PAGE_HREF.match(path), url
    assert gp.card_url("/gp/item/company/") is None
    assert gp.page_url("/gp/list/company/") is None
    assert gp.card_url("/gp/company/?Id=1") is None


def test_a_page_that_is_no_card_gives_nothing():
    """Negative: a rubric page read as a card (no top-level LocalBusiness),
    and a card body at a URL that names no company."""
    p = page("goldenpages_rubric_1424_p1")
    url = "https://www.goldenpages.uz/company/?Id=73659"
    assert gp.parse_company(p.body, url=url, fetched_at=p.fetched_at) == []
    card = page("goldenpages_company_85641")
    rubric = "https://www.goldenpages.uz/rubrics/?Id=1424"
    assert gp.parse_company(card.body, url=rubric, fetched_at=card.fetched_at) == []


PERSON = "Намунов Намуна Намунович ИндП"


def test_an_individual_entrepreneur_s_name_is_no_company_name():
    """The card and its list row, with the firm's name rewritten the way the
    directories print an individual entrepreneur's (README «Scrubbing»)."""
    p = page("goldenpages_company_85641")
    card = p.body.replace('"CHINT DISTRIBUTION" ООО'.encode(), PERSON.encode()).replace(
        '\\"CHINT DISTRIBUTION\\" ООО'.encode(), PERSON.encode()
    )
    assert PERSON.encode() in card
    [obs] = gp.parse_company(card, url=p.final_url, fetched_at=p.fetched_at)
    assert (obs.company_name_raw, obs.legal_form) == (None, "ИП")
    assert "Намунов" not in obs.excerpt
    assert obs.phones == ["(+99890) 0000001", "(+99890) 0000002"]
    rubric = page("goldenpages_rubric_1424_p1")
    body = rubric.body.replace('"CHINT DISTRIBUTION" ООО'.encode(), PERSON.encode())
    listed = gp.parse_list(body, url=rubric.final_url, fetched_at=rubric.fetched_at)
    [row] = [e.observation for e in listed.entries if e.ref == "goldenpages:85641"]
    assert row is not None and row.company_name_raw is None
    assert "Намунов" not in row.excerpt


def test_a_firm_named_after_a_given_name_keeps_its_name_by_its_legal_form():
    """«"AKMAL KONSULT" ООО» (recorded row 53976) keys as «akmal konsult», which
    reads as a person (a given name first, two tokens); its ООО says it is a
    company, so the name stays the company's (04 §1.6 as built)."""
    [row] = [
        e.observation
        for e in listing("goldenpages_rubric_1424_p1").entries
        if e.ref == "goldenpages:53976"
    ]
    assert row is not None and row.company_name_raw == '"AKMAL KONSULT" ООО'
    parsed = to_observation_json(row)
    assert (parsed["company"]["name_raw"], parsed["name_key"]) == (
        '"AKMAL KONSULT" ООО',
        "akmal konsult",
    )
    assert parsed["company"]["legal_form"] == "OOO"


@pytest.mark.parametrize(
    ("href", "url"),
    [
        (
            "/rubrics/?Id=1424&Page=2",
            "https://www.goldenpages.uz/rubrics/?Id=1424&Page=2",
        ),
        ("/rubrics/?Id=1424&page=2", None),  # the site's own is capital P
        ("/rubrics/?Id=1424&Page=2&s=1", None),  # robots.txt refuses *&s=*
        ("/gp/rubrics/?Id=1424&Page=2", None),  # the bot trap
        ("https://evil.example.uz/rubrics/?Id=1424&Page=2", None),
        (None, None),
    ],
)
def test_only_the_pager_s_own_shape_is_followed(href, url):
    assert gp.page_url(href) == url
