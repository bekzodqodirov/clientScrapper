"""top.uz parsers on the recorded pages (03 §1.5, §2.6; T-22): the Nuxt
payload rows, ``?PAGEN_1=N`` taken as the pager prints it, the empty page
past the last, the company card."""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.directories import nuxt, stop_reason
from clientscrapper.collectors.directories import topuz as tz
from clientscrapper.collectors.directories.markup import parse
from tests.collectors.directories_pages import page

P1_IDS = [
    "8359",
    "7104",
    "5585",
    "6010",
    "104670",
    "67188",
    "68459",
    "88797",
    "5320",
    "5309",
    "5304",
    "7103",
    "64586",
    "9699",
    "4947",
    "5462",
    "5275",
    "5463",
    "5484",
    "5362",
]
P2_IDS = [
    "5363",
    "5366",
    "5503",
    "4412",
    "7102",
    "71562",
    "67164",
    "5432",
    "67163",
    "67200",
    "5292",
    "7125",
    "7126",
    "7127",
    "8451",
    "9681",
    "5311",
    "5313",
    "5315",
    "5318",
]
TRADING_HOUSE = (
    "https://top.uz/company/international-business-and-travel-sp-ooo-uzbeksko-"
    "kitayskiy-torgovyy-dom-predstavitelstvo"
)


def rows_of(name: str):
    p = page(name)
    return tz.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at)


def test_the_section_page_gives_its_twenty_rows_from_the_payload():
    p = page("topuz_stroymaterialy_optom_p1")
    listed = tz.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at)
    assert [e.ref for e in listed.entries] == [f"topuz:{i}" for i in P1_IDS]
    assert all(e.complete for e in listed.entries)
    assert (listed.total, listed.page, listed.last_page) == (231, 1, 12)
    # The pager's own link to page 2, as printed.
    assert listed.next_url == "https://top.uz/section/stroymaterialy-optom?PAGEN_1=2"
    # The list HTML masks the number («+998...» and a «Показать» button); the
    # payload carries it in full for 20 of 20 rows.
    assert b"+998...</a><button" in p.body
    assert all(e.observation and e.observation.phones for e in listed.entries)
    first = listed.entries[0].observation
    assert first == Observation(
        source="directories_topuz",
        source_ref="topuz:8359",
        url="https://top.uz/section/stroymaterialy-optom",
        fetched_at=p.fetched_at,
        raw_sha256=p.sha256,
        kind="company",
        company_name_raw='"Birjaservistexno" ООО',
        legal_form="ООО",
        phones=["+998(90)000-00-01"],
        website="elevor.uz",
        address=(
            "Узбекистан, 100057, Ташкент, АЛМАЗАРСКИЙ РАЙОН, ул. УСТА ШИРИН, 125 "
            "маг. №39"
        ),
        city="Ташкент",
        goods_text=(
            "Оптовая торговля · Прочая оптовая торговля · Стройматериалы оптом · "
            "Мебельная фурнитура, электрохозтовары - оптовая продажа."
        ),
        signal_types=["directory_wholesale"],
        excerpt=(
            '"Birjaservistexno" ООО · Мебельная фурнитура, электрохозтовары - '
            "оптовая продажа. · Оптовая торговля · Прочая оптовая торговля · "
            "Стройматериалы оптом"
        ),
        attrs={
            "site": "topuz",
            "sections": [
                "optovaya-torgovlya",
                "prochaya-optovaya-torgovlya",
                "stroymaterialy-optom",
            ],
            "inside_source": "prom.uz",
            "prom_id": "17729",
            "card_url": "https://top.uz/company/birjaservistexno-ooo",
            "bulky": ["building", "furniture"],
        },
        # The payload's naive updated_at, read as Tashkent time.
        published_at=datetime(2024, 10, 15, 6, 54, 41, tzinfo=ZoneInfo("Asia/Tashkent")),
        lang="ru",
    )
    assert tz.parse_section(p.body, url=p.final_url, fetched_at=p.fetched_at) == [
        e.observation for e in listed.entries
    ]


def test_a_fax_field_with_two_numbers_is_two_and_a_repeat_is_one():
    [row] = [
        e.observation
        for e in rows_of("topuz_stroymaterialy_optom_p1").entries
        if e.ref == "topuz:5275"
    ]
    # phones[] «+998(90)000-00-33», fax «(998) 90-000-00-33, 90-000-00-34».
    assert row is not None and row.phones == ["+998(90)000-00-33", "90-000-00-34"]


def test_wholesale_sections_mark_directory_wholesale_and_the_rest_do_not():
    rows = {
        e.ref: e.observation for e in rows_of("topuz_stroymaterialy_optom_p1").entries
    }
    wholesale = sorted(
        r for r, o in rows.items() if o and "directory_wholesale" in o.signal_types
    )
    assert len(wholesale) == 13
    # «Grand Rate»: khoztovary / instrumenty — bulky household goods, no
    # wholesale section: kept, without the directory signal.
    grand = rows["topuz:5320"]
    assert grand is not None
    assert grand.attrs["sections"] == ["khoztovary", "instrumenty", "instrument"]
    assert grand.signal_types == [] and "hold" not in grand.attrs
    # «Global Trade»: no bulky goods and no China cue — held.
    held = rows["topuz:5484"]
    assert held is not None and held.attrs["hold"] == "category"


def test_page_two_follows_pagen_and_brings_twenty_new_slugs():
    two = rows_of("topuz_stroymaterialy_optom_p2")
    assert [e.ref.removeprefix("topuz:") for e in two.entries] == P2_IDS
    assert not set(P1_IDS) & set(P2_IDS)
    assert (two.page, two.next_url) == (
        2,
        "https://top.uz/section/stroymaterialy-optom?PAGEN_1=3",
    )


def test_the_page_past_the_last_is_empty_and_stops_the_seed():
    p = page("topuz_stroymaterialy_optom_p99_negative")
    past = tz.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at)
    assert (past.entries, past.page, past.last_page, past.next_url) == ((), 99, 12, None)
    assert stop_reason(
        past, spec=tz.SPEC, asked=p.url, final=p.final_url, firsts=set()
    ) == ("empty")


def test_the_big_section_reports_the_organiser_s_figure():
    listed = rows_of("topuz_optovaya_torgovlya_p1")
    assert (listed.total, listed.last_page, len(listed.entries)) == (815, 41, 20)
    assert listed.next_url == "https://top.uz/section/optovaya-torgovlya?PAGEN_1=2"


def test_the_plan_never_appends_a_parameter_robots_refuses():
    """Only ``?PAGEN_1=N`` links of the section's own path are followed:
    the page prints sort/filter links too, which robots.txt refuses."""
    p = page("topuz_stroymaterialy_optom_p1")
    nxt = rows_of("topuz_stroymaterialy_optom_p1").next_url
    assert nxt is not None and re.fullmatch(
        r"https://top\.uz/section/stroymaterialy-optom\?PAGEN_1=2", nxt
    )
    assert tz.PAGE_HREF.match("/section/stroymaterialy-optom?PAGEN_1=2&SORT=asc") is None
    assert tz.PAGE_HREF.match("/section/stroymaterialy-optom?p=2") is None
    assert b"PAGEN_1=" in p.body


def test_the_card_is_the_payload_organisation_with_its_legal_name():
    p = page("topuz_company_uzbek_chinese_trading_house")
    [obs] = tz.parse_company(p.body, url=p.final_url, fetched_at=p.fetched_at)
    assert (obs.source, obs.source_ref, obs.url) == (
        "directories_topuz",
        "topuz:6010",
        TRADING_HOUSE,
    )
    assert obs.company_name_raw == "Техноплаза савдо уйи"
    # The card's tel: link carries the first payload number (note the space).
    assert b'href="tel: +998(90)000-00-02"' in p.body
    assert obs.phones == ["+998(90)000-00-02", "+998(90)000-00-03", "(998) 90-000-00-01"]
    assert obs.website == "uzchinatrade.com"
    assert obs.attrs["legal_name"] == "УЗБЕКСКО-КИТАЙСКИЙ ТОРГОВЫЙ ДОМ"
    assert obs.signal_types == ["directory_wholesale"]
    assert obs.attrs["bulky"] == ["building", "equipment", "raw"]
    assert "китайских производителей" in (obs.goods_text or "")
    assert obs.excerpt.startswith(
        "Техноплаза савдо уйи · УЗБЕКСКО-КИТАЙСКИЙ ТОРГОВЫЙ ДОМ"
    )


def test_a_page_that_is_not_the_named_company_gives_nothing():
    card = page("topuz_company_uzbek_chinese_trading_house")
    other = "https://top.uz/company/birjaservistexno-ooo"
    assert tz.parse_company(card.body, url=other, fetched_at=card.fetched_at) == []
    listing = page("topuz_stroymaterialy_optom_p1")
    assert (
        tz.parse_company(listing.body, url=listing.url, fetched_at=listing.fetched_at)
        == []
    )


# An individual entrepreneur's name, the way the directories print it (README
# «Scrubbing» item 2): synthetic words, a real shape — surname, given name,
# patronymic, «ИндП».
PERSON = "Намунов Намуна Намунович ИндП"


def _org(name: str, *, legal: str | None = None) -> Observation | None:
    p = page("topuz_stroymaterialy_optom_p1")
    org = dict(nuxt.list_block(nuxt.payload(parse(p.body)))["data"][0])
    org.update(name_ru=name, name_uz=name, legal_name=legal)
    return tz.organisation(org, url=p.final_url, fetched_at=p.fetched_at, sha=p.sha256)


def test_an_individual_entrepreneur_s_name_is_no_company_name():
    """The words before «ИндП» are a person's: not the company name, not the
    excerpt, not ``attrs`` (the README scrubbed them as person data)."""
    obs = _org(PERSON, legal=PERSON)
    assert obs is not None
    assert (obs.company_name_raw, obs.legal_form) == (None, "ИП")
    assert "Намунов" not in obs.excerpt and "legal_name" not in obs.attrs
    assert obs.phones  # the shop's own number is company-tier and kept


def test_a_shop_name_beside_a_person_s_legal_name_names_the_company():
    obs = _org("Namuna Qurilish", legal=PERSON)
    assert obs is not None
    assert (obs.company_name_raw, obs.legal_form) == ("Namuna Qurilish", "ИП")
    assert "Намунов" not in obs.excerpt and "legal_name" not in obs.attrs
    # A person's name in name_ru, the firm's own in legal_name: the firm's.
    named = _org(PERSON, legal="Namuna Qurilish ООО")
    assert named is not None and named.company_name_raw == "Namuna Qurilish ООО"
    assert "Намунов" not in named.excerpt


def test_the_channel_is_the_firm_s_telegram_and_a_contact_handle_is_not():
    """``tg_channel`` is the firm's own channel; ``tg_contact`` is, by its
    name, a contact person's account — person-tier, never filed as the
    company's channel (the T-22 review). No recorded row carries either."""
    p = page("topuz_stroymaterialy_optom_p1")
    org = nuxt.list_block(nuxt.payload(parse(p.body)))["data"][0]

    def with_(**changes: object) -> Observation:
        obs = tz.organisation(
            {**org, **changes}, url=p.final_url, fetched_at=p.fetched_at, sha=p.sha256
        )
        assert obs is not None
        return obs

    assert with_(tg_channel="https://t.me/namuna_06").telegram == "namuna_06"
    assert with_(tg_contact="namuna_07").telegram is None
    both = with_(tg_channel="namuna_06", tg_contact="namuna_07")
    assert both.telegram == "namuna_06" and "namuna_07" not in repr(both)


def test_the_next_link_is_the_next_page_of_the_same_section_only():
    """A pager whose links come in another order — the previous page first, a
    link to another section's page 3 before ours — still gives page 3 of this
    section (a hand-edited copy of recorded page 2)."""
    p = page("topuz_stroymaterialy_optom_p2")
    edited = p.body.replace(
        b'<ul class="articles-pagination" data-v-fe36f899 data-v-a1f05fe5>',
        b'<ul class="articles-pagination" data-v-fe36f899 data-v-a1f05fe5>'
        b'<li><a href="/section/stroymaterialy-optom?PAGEN_1=1">1</a></li>'
        b'<li><a href="/section/optovaya-torgovlya?PAGEN_1=3">3</a></li>'
        b'<li><a href="/section/stroymaterialy-optom?PAGEN_1=4">4</a></li>',
        1,
    )
    assert edited != p.body
    listed = tz.parse_list(edited, url=p.final_url, fetched_at=p.fetched_at)
    assert listed.next_url == "https://top.uz/section/stroymaterialy-optom?PAGEN_1=3"
    # Without our page 3 in the pager, there is no next page.
    gone = edited.replace(b"stroymaterialy-optom?PAGEN_1=3", b"stroymaterialy-optom?x=3")
    assert tz.parse_list(gone, url=p.final_url, fetched_at=p.fetched_at).next_url is None


PERSON_SLUG = "namunov-namuna-namunovich-indp"


def test_an_individual_entrepreneur_s_card_is_never_asked_or_filed():
    """The slug of a person's card is the person's name (README «Scrubbing»
    item 2: ``<surname>-<i>-<i>-indp``): the list row names no card to ask,
    ``attrs`` holds no card URL, and a card asked for anyway files nothing at
    its own URL (the wave-5 seam review; 06 §5.1)."""
    p = page("topuz_stroymaterialy_optom_p1")
    org = dict(nuxt.list_block(nuxt.payload(parse(p.body)))["data"][0])
    org.update(name_ru=PERSON, name_uz=PERSON, legal_name=None, slug=PERSON_SLUG)
    obs = tz.organisation(org, url=p.final_url, fetched_at=p.fetched_at, sha=p.sha256)
    assert obs is not None and obs.phones
    assert "card_url" not in obs.attrs
    assert "namunov" not in repr(obs).lower()
    # The person's name in the legal name alone is enough.
    org.update(name_ru="Namuna Qurilish", name_uz=None, legal_name=PERSON)
    shop = tz.organisation(org, url=p.final_url, fetched_at=p.fetched_at, sha=p.sha256)
    assert shop is not None and "card_url" not in shop.attrs

    # The list page as served, its first row made a person's (the payload
    # holds the name JSON-escaped).
    body = p.body.decode("utf-8")
    row = nuxt.list_block(nuxt.payload(parse(p.body)))["data"][0]
    escaped = json.dumps(row["name_ru"], ensure_ascii=False)[1:-1]
    assert escaped in body
    served = body.replace(f'"{row["slug"]}"', f'"{PERSON_SLUG}"').replace(escaped, PERSON)
    listed = tz.parse_list(served.encode(), url=p.final_url, fetched_at=p.fetched_at)
    entry = next(e for e in listed.entries if e.ref == f"topuz:{row['id']}")
    assert entry.card_url is None and entry.complete
    others = [e for e in listed.entries if e.ref != entry.ref]
    assert others and all(e.card_url for e in others)

    card = page("topuz_company_uzbek_chinese_trading_house")
    slug = TRADING_HOUSE.rsplit("/", 1)[1]
    text = (
        card.body.decode("utf-8")
        .replace(slug, PERSON_SLUG)
        .replace("Техноплаза савдо уйи", PERSON)
        .replace("УЗБЕКСКО-КИТАЙСКИЙ ТОРГОВЫЙ ДОМ", PERSON)
    )
    url = f"{tz.BASE}/company/{PERSON_SLUG}"
    assert tz.parse_company(text.encode(), url=url, fetched_at=card.fetched_at) == []
    # The same card under a firm's name is filed (the control).
    named = card.body.decode("utf-8").replace(slug, PERSON_SLUG)
    assert tz.parse_company(named.encode(), url=url, fetched_at=card.fetched_at)
