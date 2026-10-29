"""glotr.uz parsers on the recorded pages (03 §1.5, §2.6; T-22): category
page 1 → its sellers, the showcase's JSON-LD, «Проверено … Soliq.uz», the
soft 404. Page 2 is refused by robots.txt and never planned."""

from __future__ import annotations

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.directories import glotr
from clientscrapper.services.robots import parse_robots
from tests.collectors.directories_pages import page, robots

SELLERS = [
    "chemistry-invest-39618",
    "all-printuz-40078",
    "kcepokcuz-40077",
    "kans-profil-36855",
    "forte-plast-16588",
    "rich-health-18803",
    "shifoshopuz-20195",
    "energotex-39718",
    "viva-online-15833",
    "wst",
    "maxenergo-mchj-40795",
    "world-electronics-group-8115",
    "ooo-furniglass-9791",
    "daesung-celtic-7454",
    "franko-39915",
    "ooo-max-san-elektro-37075",
    "texnosaruz-28535",
    "coral-club-v-tashkente-8486",
    "lilshop-17990",
    "ooo-elso-trade-5799",
    "domashniy-ugolok-xk-18821",
    "fora-group-11901",
    "ooo-metal-gaz-butlash-1343",
    "mchj-energotaminot-38673",
    "dorixona-rasmiy-uzb-15876",
    "jack",
    "shifonetuz-7678",
    "ooo-mkm-metal-38766",
    "ledart-35654",
    "karvon-trade-11471",
    "gc-resanta-34913",
    "ooo-idealkitchen135",
    "barakatexnika",
    "hometech-16284",
    "man-markazi-toshkentda-1628",
    "mzshop-mchj-17714",
    "barso-metal-10367",
    "ooo-rumatin-9033",
    "ooo-hitech-ltd-8556",
    "ayvaz-29327",
    "evrotexservis-mchj-16234",
    "ooo-sayina-stroy-11387",
    "bio-composite-35448",
    "medicshopuz-24461",
    "ooo-leonis-20222",
    "cartivo-mchj-39003",
    "lechebnaya-kosmetika-8402",
    "birjaservistaminot-4649",
    "technohouse-17164",
    "cp-elektro-profi-plyus-292",
    "ooo-mezon-plast-11871",
    "ooo-imagine-7309",
    "ooo-kans-art-sales-8109",
    "joymax-35067",
]
ABOUT = (
    "Наша компания занимается продажей, поставкой и сервисным обслуживанием "
    "промышленных швейных машин в Узбекистане. Мы обеспечиваем профессиональных "
    "швей, швейные цеха и крупные производственные предприятия современным, "
    "надёжным и высококачественным швейным оборудованием. Наша компания является "
    "официальным дистрибьютором JACK TECHNOLOGY CO., LTD и осуществляет продажу и "
    "техническое обслуживание промышленных швейных машин JACK на территории..."
)


def test_the_category_page_lists_each_seller_once_by_its_path():
    p = page("glotr_oborudovanie_p1")
    # /ru/oborudovanie/ redirected to the unprefixed path (the sidecar).
    assert (p.url, p.final_url) == (
        "https://glotr.uz/ru/oborudovanie/",
        "https://glotr.uz/oborudovanie/",
    )
    listed = glotr.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at)
    assert [e.ref.removeprefix("glotr:") for e in listed.entries] == SELLERS
    # 56 seller links, two sellers twice; «?s=1» or no query: keyed on the path.
    assert p.body.count(b"class='product_company_link") == 56
    assert all(
        e.card_url == f"https://glotr.uz/showcase/{e.ref.removeprefix('glotr:')}/"
        for e in listed.entries
    )
    # A seller link alone is no company record.
    assert all(e.observation is None and not e.complete for e in listed.entries)
    assert listed.total == 39162


def test_page_two_is_printed_refused_by_robots_and_never_planned():
    p = page("glotr_oborudovanie_p1")
    assert b'href="/oborudovanie/?page=2"' in p.body
    rules = parse_robots(robots("glotr.uz"), "clientScrapperBot")
    assert not rules.allows("https://glotr.uz/oborudovanie/?page=2")
    assert rules.allows("https://glotr.uz/oborudovanie/")
    assert rules.allows("https://glotr.uz/showcase/jack/")
    assert rules.crawl_delay == 10
    listed = glotr.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at)
    assert listed.next_url is None
    assert glotr.SPEC.paginated is False


def test_the_showcase_gives_the_json_ld_contacts_and_the_soliq_check():
    p = page("glotr_showcase_jack")
    [obs] = glotr.parse_showcase(p.body, url=p.final_url, fetched_at=p.fetched_at)
    titles = obs.goods_text.split(" · ")[1:] if obs.goods_text else []
    assert len(titles) == 19
    assert obs == Observation(
        source="directories_glotr",
        source_ref="glotr:jack",
        url="https://glotr.uz/showcase/jack/",
        fetched_at=p.fetched_at,
        raw_sha256=p.sha256,
        kind="company",
        company_name_raw='"JACK" (ООО "SEWING MACHINES TRADING")',
        legal_form="ООО",
        phones=["+998 90 000 00 01"],
        telegram="namuna_02",
        instagram="namuna_04",
        website="jack.uz",
        address="Яккасарайский район, улица Кушбеги, 26",
        city="Ташкент",
        goods_text=" · ".join([ABOUT, *titles]),
        signal_types=["entity_solid"],
        excerpt=f'"JACK" (ООО "SEWING MACHINES TRADING") · {ABOUT}',
        external_ids={"glotr": "jack"},
        attrs={
            "site": "glotr",
            "sku_count": 19,
            "checked": "soliq",
            "bulky": ["equipment"],
        },
        lang="ru",
    )
    assert titles[0] == (
        "Промышленная швейная машина JACK A2C. Прямая строчка для производства и дома"
    )
    # The visible phone is a mask; the site's own Organization JSON-LD (its
    # help desk) is not the seller's.
    assert "+998 (показать номер)".encode() in p.body


def test_skus_are_products_not_cards():
    """27 product cards, 19 products: a second block repeats eight. 03 §2.6's
    «≥ 20 SKUs» is not met by the recorded seller (the README counted cards)."""
    p = page("glotr_showcase_jack")
    assert p.body.count(b"product-card__parent") == 27
    [obs] = glotr.parse_showcase(p.body, url=p.final_url, fetched_at=p.fetched_at)
    assert obs.attrs["sku_count"] == 19
    assert "marketplace_velocity" not in obs.signal_types
    # One product card fewer is one SKU fewer only when it is not a repeat.
    fewer = p.body.replace(b"product-card__parent", b"product-card__gone", 1)
    [less] = glotr.parse_showcase(fewer, url=p.final_url, fetched_at=p.fetched_at)
    assert less.attrs["sku_count"] == 18


def test_a_showcase_without_the_soliq_check_is_not_solid():
    p = page("glotr_showcase_jack")
    unchecked = p.body.replace(b"seller-info__checked", b"seller-info__other")
    [obs] = glotr.parse_showcase(unchecked, url=p.final_url, fetched_at=p.fetched_at)
    assert obs.signal_types == [] and "checked" not in obs.attrs


def test_the_soft_404_gives_nothing_whatever_its_status():
    p = page("glotr_showcase_missing_negative")
    assert (p.status, p.final_url) == (200, "https://glotr.uz/showcases/")
    assert glotr.parse_showcase(p.body, url=p.final_url, fetched_at=p.fetched_at) == []
    # Read at the URL that was asked for, it is still no seller's page.
    assert glotr.parse_showcase(p.body, url=p.url, fetched_at=p.fetched_at) == []
    # The partners index is no category page either: its showcase links are
    # not product sellers.
    assert (
        glotr.parse_list(p.body, url=p.final_url, fetched_at=p.fetched_at).entries == ()
    )


def test_an_individual_entrepreneur_s_name_is_no_company_name():
    p = page("glotr_showcase_jack")
    person = "Намунов Намуна Намунович ИндП"
    body = p.body.replace(
        b'<h1 class=\'h1\'>"JACK" (\xd0\x9e\xd0\x9e\xd0\x9e "SEWING MACHINES TRADING")',
        f"<h1 class='h1'>{person}".encode(),
    )
    assert person.encode() in body
    [obs] = glotr.parse_showcase(body, url=p.final_url, fetched_at=p.fetched_at)
    assert (obs.company_name_raw, obs.legal_form) == (None, "ИП")
    assert "Намунов" not in obs.excerpt
    assert obs.phones == ["+998 90 000 00 01"]
