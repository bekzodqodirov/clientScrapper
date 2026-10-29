"""yellowpages.uz parsers on the recorded pages (03 §1.5, §2.6; T-22).

The data is the Nuxt payload (``__NUXT_DATA__``), not the HTML: the HTML's
phone is a mask, ``contacts.phone[]`` the number as served. The fixture's
phones and STIR-shaped ``inn`` values are synthetic (README «Scrubbing»): the
tests assert the field and its position, never a value as a real STIR.
"""

from __future__ import annotations

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.directories import nuxt
from clientscrapper.collectors.directories import yellowpages as yp
from clientscrapper.collectors.directories.markup import parse
from tests.collectors.directories_pages import page

RUBRIKA = "yellowpages_rubrika_kanctovary_tashkent_p1"
ALTA_RUBRICS = [
    "Канцелярские товары - Производство, Продажа",
    "Бумага офисная - Продажа",
    "Бумага, Бумажная продукция",
    "Акварельные краски - продажа, производство",
    "Бумага Снегурочка - продажа",
    "Ежедневники и органайзеры - продажа",
    "Канцелярские ножи - продажа",
    "Канцелярский клей - продажа",
    "Перманентные маркеры - продажа",
    "Ручки гелевые - продажа",
    "Оптовая торговля канцтоварами, канцелярскими товарами",
    "Папки-скоросшиватели (пластиковые, бумажные, картонные) - продажа",
    "ПВА клей - продажа, производство",
    "Пеналы школьные - продажа, производство",
    "Художественные краски - продажа, производство",
]
# (ref, name, legal form, stir, phones, website, signal types, held)
ROWS = [
    (
        "26219",
        "ALTA TRADE ООО",
        "ООО",
        None,
        ["90 0000009", "90 0000001"],
        "deskform.uz",
        [],
        True,
    ),
    (
        "24981",
        "AZIYA BESH SAVDO ООО",
        "ООО",
        None,
        ["90 0000010", "90 0000002"],
        None,
        ["directory_wholesale"],
        False,
    ),
    (
        "11808",
        "BETA ООО",
        "ООО",
        "900000011",
        ["90 0000012", "90 0000013", "90 0000003"],
        None,
        ["directory_wholesale"],
        False,
    ),
    (
        "22628",
        "PREMIER OFIS ЧП",
        "ЧП",
        "900000014",
        ["90 0000015", "90 0000004", "90 0000016"],
        "https://centrum-post.com/",
        [],
        True,
    ),
    (
        "20325",
        "CHILONZOR SAVDO UYI ООО",
        "ООО",
        "900000017",
        ["90 0000005", "90 0000018"],
        "torgstroy.uz",
        ["directory_wholesale"],
        False,
    ),
    (
        "4853",
        "DELI TORG ЧП",
        "ЧП",
        "900000019",
        ["90 0000006"],
        "deli.uz",
        ["directory_wholesale"],
        False,
    ),
    (
        "35270",
        "GRAND TRADING СЕМЕЙНОЕ ПРЕДПРИЯТИЕ",
        None,
        None,
        ["90 0000020", "90 0000021"],
        "gtr.uz",
        [],
        True,
    ),
    (
        "46086",
        "KANS ART SALES ООО",
        "ООО",
        None,
        ["90 0000022", "90 0000023"],
        None,
        ["directory_wholesale"],
        False,
    ),
    (
        "22535",
        "KANS-SARBON ООО",
        "ООО",
        None,
        ["90 0000007", "90 0000024"],
        None,
        [],
        True,
    ),
    ("46092", "LIDER-PRESS ООО", "ООО", None, ["90 0000025"], None, [], True),
    (
        "5777",
        "MAL-GUZ-GLOBAL ООО",
        "ООО",
        None,
        ["90 0000026", "90 0000027"],
        None,
        [],
        True,
    ),
    (
        "11785",
        "MARKER-MARKET МАГАЗИН КАНЦТОВАРОВ",
        "ИП",
        None,
        ["90 0000028", "90 0000029", "90 0000008"],
        "markermarket.uz",
        ["directory_wholesale"],
        False,
    ),
    ("29647", "MIREL GROUP ООО", "ООО", None, ["90 0000030"], "mirelgroup.com", [], True),
    ("253955", "Nuqta Stationery", None, "313064467", ["90 0000031"], None, [], True),
    (
        "253053",
        "Alfa Monet Group",
        None,
        "900000032",
        ["90 0000033"],
        "alfamonetgroup.uz",
        [],
        True,
    ),
]


def rows_of(name: str):
    p = page(name)
    return yp.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at)


def test_the_rubrika_page_gives_every_row_from_the_payload():
    p = page(RUBRIKA)
    listed = yp.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at)
    got = [
        (
            e.ref.removeprefix("yellowpages:"),
            o.company_name_raw,
            o.legal_form,
            o.stir,
            o.phones,
            o.website,
            o.signal_types,
            o.attrs.get("hold") == "category",
        )
        for e in listed.entries
        if (o := e.observation) is not None
    ]
    assert got == ROWS
    assert all(e.complete for e in listed.entries)
    assert [e.card_url for e in listed.entries][:2] == [
        "https://www.yellowpages.uz/kompaniya/alta-trade-ooo",
        "https://www.yellowpages.uz/kompaniya/aziya-besh-savdo-ooo",
    ]
    # The HTML masks the number; the payload carries it (03 §2.6, README).
    assert b"+99871 ..." in p.body
    assert (listed.total, listed.page, listed.last_page, listed.next_url) == (
        25,
        1,
        2,
        None,
    )
    first = listed.entries[0].observation
    assert first == Observation(
        source="directories_yellowpages",
        source_ref="yellowpages:26219",
        url=p.final_url,
        fetched_at=p.fetched_at,
        raw_sha256=p.sha256,
        kind="company",
        company_name_raw="ALTA TRADE ООО",
        legal_form="ООО",
        phones=["90 0000009", "90 0000001"],
        website="deskform.uz",
        address=(
            "Узбекистан, 100060, Ташкентская область, Ташкент, Яшнабадский район, "
            "ул. КИЧИК ХАЛКА ЙУЛИ, 32"
        ),
        city="Ташкент",
        goods_text=" · ".join([*ALTA_RUBRICS, "бумага", "канцтовары"]),
        excerpt=" · ".join(["ALTA TRADE ООО", *ALTA_RUBRICS]),
        external_ids={"yellowpages": "26219"},
        attrs={
            "site": "yellowpages",
            "rubrics": ALTA_RUBRICS,
            "tags": ["bumaga", "kanctovary"],
            "updated_at": "2021-02-03T11:51:59.000000Z",
            "card_url": "https://www.yellowpages.uz/kompaniya/alta-trade-ooo",
            "hold": "category",
        },
        lang="ru",
    )
    assert yp.parse_rubrika(p.body, url=p.final_url, fetched_at=p.fetched_at) == [
        e.observation for e in listed.entries
    ]


def test_stationery_wholesale_without_a_china_cue_is_held():
    """03 §2.6's category filter on the recorded stationery rubric: a row with
    no bulky goods keeps its contacts and carries no directory_wholesale."""
    held = [
        o
        for e in rows_of(RUBRIKA).entries
        if (o := e.observation) and o.attrs.get("hold")
    ]
    assert len(held) == 9
    assert all(o.signal_types == [] and o.phones for o in held)
    assert "bulky" not in held[0].attrs


def test_an_individual_entrepreneur_is_named_by_the_shop_and_keeps_no_stir():
    p = page(RUBRIKA)
    org = next(
        o
        for o in nuxt.list_block(nuxt.payload(parse(p.body)))["data"]
        if o["id"] == 11785
    )
    # The payload holds the person's name before «ИндП» and a taxpayer number.
    assert (org["name"], org["inn"]) == ("Seller 04 ИндП", "400000001")
    [obs] = [
        e.observation for e in rows_of(RUBRIKA).entries if e.ref == "yellowpages:11785"
    ]
    assert obs is not None
    assert (obs.company_name_raw, obs.legal_form, obs.stir) == (
        "MARKER-MARKET МАГАЗИН КАНЦТОВАРОВ",
        "ИП",
        None,
    )
    assert "Seller 04" not in obs.excerpt
    assert "brand_name" not in obs.attrs


def test_null_contact_lists_are_empty_ones():
    p = page(RUBRIKA)
    org = next(
        o
        for o in nuxt.list_block(nuxt.payload(parse(p.body)))["data"]
        if o["id"] == 253955
    )
    assert org["contacts"]["telegram"] is None and org["contacts"]["emails"][1] is None
    [obs] = [
        e.observation for e in rows_of(RUBRIKA).entries if e.ref == "yellowpages:253955"
    ]
    assert obs is not None and (obs.telegram, obs.instagram, obs.phones) == (
        None,
        None,
        ["90 0000031"],
    )


def test_the_china_tag_s_one_firm_is_a_logistics_competitor():
    listed = rows_of("yellowpages_teg_tovary_iz_kitaya")
    [entry] = listed.entries
    obs = entry.observation
    assert obs is not None and entry.complete
    assert "tovary-iz-kitaya" in obs.attrs["tags"]
    # The competitor rule first: no china_origin for a cargo firm.
    assert (obs.company_name_raw, obs.signal_types, obs.origin_text) == (
        "Xinjiang transpor ООО",
        ["competitor"],
        None,
    )
    assert (obs.instagram, obs.attrs["brand_name"]) == ("namuna_01", "SAFlogistics")
    assert (listed.total, listed.last_page) == (1, 1)


def test_the_china_tag_gives_china_origin_to_an_importer():
    """The tag rule on a payload row with no cargo words (the recorded teg
    row with its transport rubrics and tags taken out)."""
    p = page("yellowpages_teg_tovary_iz_kitaya")
    org = nuxt.list_block(nuxt.payload(parse(p.body)))["data"][0]
    importer = {
        **org,
        "name": "Namuna Mebel ООО",
        "brand_name": "Namuna Mebel ООО",
        "rubrics": [{"id": 1, "slug": "mebel-optom", "name": "Мебель оптом"}],
        "tags": [t for t in org["tags"] if t["slug"] == "tovary-iz-kitaya"],
    }
    obs = yp.organisation(
        importer, url=p.final_url, fetched_at=p.fetched_at, sha=p.sha256
    )
    assert obs is not None
    assert (obs.signal_types, obs.origin_text) == (
        ["china_origin", "directory_wholesale"],
        "товары из китая",
    )
    assert "товары из китая" in obs.excerpt


def test_the_card_is_the_payload_organisation_of_its_slug():
    p = page("yellowpages_kompaniya_alta_trade_ooo")
    [obs] = yp.parse_kompaniya(p.body, url=p.final_url, fetched_at=p.fetched_at)
    assert (obs.source_ref, obs.url, obs.raw_sha256) == (
        "yellowpages:26219",
        "https://www.yellowpages.uz/kompaniya/alta-trade-ooo",
        p.sha256,
    )
    # The card's own synthetic numbers (the list page's differ: another file).
    assert obs.phones == ["90 0000002", "90 0000001"]
    assert (obs.company_name_raw, obs.website, obs.city) == (
        "ALTA TRADE ООО",
        "deskform.uz",
        "Ташкент",
    )
    assert obs.attrs["rubrics"] == ALTA_RUBRICS
    assert obs.signal_types == [] and obs.attrs["hold"] == "category"
    # The contact block prints a mask («+99871 ... Позвонить»).
    assert "+99871 ... Позвонить".encode() in p.body


def test_page_two_is_ignored_by_the_server_and_no_next_link_is_followed():
    """Pagination is STILL UNVERIFIED: ``?page=2`` answers page 1 again."""
    two = rows_of("yellowpages_rubrika_kanctovary_tashkent_p2")
    one = rows_of(RUBRIKA)
    assert two.page == 1 and two.next_url is None
    assert [e.ref for e in two.entries] == [e.ref for e in one.entries]
    assert yp.SPEC.paginated is False


def test_an_unknown_slug_gives_nothing():
    p = page("yellowpages_kompaniya_missing_negative")
    assert p.status == 404
    assert yp.parse_kompaniya(p.body, url=p.final_url, fetched_at=p.fetched_at) == []
    assert rows_of("yellowpages_kompaniya_missing_negative").entries == ()
    # A card body read at another company's URL is not that company.
    card = page("yellowpages_kompaniya_alta_trade_ooo")
    other = "https://www.yellowpages.uz/kompaniya/beta-ooo"
    assert yp.parse_kompaniya(card.body, url=other, fetched_at=card.fetched_at) == []
