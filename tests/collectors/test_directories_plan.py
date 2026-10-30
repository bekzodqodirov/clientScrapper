"""The directories crawl plan and shared rule, pure (03 §2.6; T-22): the
markup tree, the Nuxt resolver, ``common.judge`` (competitor, China cue,
the BULKY_CATEGORIES hold filter), the cadences, the fan-out's skip rule,
the seeds, the paginator stop, the card decision."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from clientscrapper.collectors import directories as d
from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.directories import common, nuxt
from clientscrapper.collectors.directories import goldenpages as gp
from clientscrapper.collectors.directories.markup import parse
from clientscrapper.collectors.keywords import INDIVIDUAL_FORM_RE, WHOLESALE_RUBRIC_RE

TASHKENT = ZoneInfo("Asia/Tashkent")
THURSDAY = datetime(2026, 10, 29, 23, 0, tzinfo=TASHKENT)
BRAND = "brand_distributor"


# --- the markup tree and the payload ------------------------------------------------


def test_the_tree_closes_void_and_stray_tags_as_a_browser_would():
    root = parse(
        b'<div class="a b"><img src="x"><p>One &amp; <b>two</b></p></span>'
        b"<script>var x = '<p>no</p>';</script><ul><li>i</li></ul></div><p>after"
    )
    div = root.find("div", cls=("a", "b"))
    assert div is not None and div.has_class("a") and not div.has_class("c")
    assert div.text() == "One & two i"
    assert div.find("img") is not None and div.find("img").children == []
    assert div.find("script").raw() == "var x = '<p>no</p>';"
    assert [p.text() for p in root.find_all("p")] == ["One & two", "after"]
    assert root.find("a", prefix=("href", "/x")) is None


def test_the_devalue_payload_resolves_indices_wrappers_and_constants():
    data = [
        ["ShallowReactive", 1],
        {"data": 2, "meta": 6, "gone": -1},
        [3],
        {"id": 4, "slug": 5, "tags": 7, "phone": 8},
        42,
        "namuna",
        {"current_page": 4},
        ["Set", 5],
        ["null"],
    ]
    root = nuxt.resolve(data)
    assert root == {
        "data": [{"id": 42, "slug": "namuna", "tags": ["namuna"], "phone": None}],
        "meta": {"current_page": 42},
        "gone": None,
    }
    assert nuxt.list_block(root) is root
    assert nuxt.organisation(root, "namuna", needs="phone") == root["data"][0]
    assert nuxt.organisation(root, "other", needs="phone") is None
    page = f'<script id="__NUXT_DATA__">{json.dumps(data)}</script>'.encode()
    assert nuxt.payload(parse(page)) == root
    assert nuxt.payload(parse(b"<html>no payload</html>")) is None


# --- the one rule ---------------------------------------------------------------------


def test_a_cargo_firm_is_a_competitor_and_nothing_else():
    verdict = common.judge(
        name="Namuna Savdo ООО",
        own="Грузоперевозки из Китая · Оптовая торговля мебелью",
        goods="мебель",
        wholesale=True,
        china="товары из китая",
        importer=True,
    )
    assert (verdict.signal_types, verdict.competitor, verdict.held) == (
        ("competitor",),
        True,
        False,
    )
    named = common.judge(
        name="Namuna Cargo", own="", goods="", wholesale=False, china=None
    )
    assert named.signal_types == ("competitor",)


def test_an_importer_that_delivers_is_no_forwarder():
    """Carried from wave 3: the card's own text is read with the cargo core,
    so «доставка по Ташкенту» on a furniture wholesaler is no exclusion."""
    verdict = common.judge(
        name="Namuna Mebel ООО",
        own="Мебель оптом из Китая. Доставка по Ташкенту бесплатно · Bepul dostavka",
        goods="мебель",
        wholesale=True,
        china="из Китая",
    )
    assert verdict.signal_types == ("china_origin", "directory_wholesale")
    assert not verdict.competitor


@pytest.mark.parametrize(
    ("goods", "china", "held", "types"),
    [
        # A bulky category keeps the wholesale signal and the distributor.
        ("Мебель, мебельная фурнитура", None, False, ("directory_wholesale", BRAND)),
        # A China cue keeps them whatever the goods.
        (
            "Продукты питания",
            "из Китая",
            False,
            ("china_origin", "directory_wholesale", BRAND),
        ),
        # Food, stationery, metal, agri without a China cue: held, no volume type.
        ("Продукты питания, бакалея", None, True, ()),
        ("Канцтовары, бумага офисная", None, True, ()),
        ("Металлопрокат, арматура", None, True, ()),
    ],
)
def test_the_category_filter_holds_what_is_not_china_heavy(goods, china, held, types):
    verdict = common.judge(
        name="Namuna Savdo ООО",
        own=goods,
        goods=goods,
        wholesale=True,
        china=china,
        importer=True,
    )
    assert (verdict.held, verdict.signal_types) == (held, types)
    obs = common.apply(
        Observation("directories", "x:1", "u", THURSDAY, "", "company"), verdict
    )
    assert (obs.attrs.get("hold") == "category") is held
    assert ("official" in obs.attrs) is ("brand_distributor" in types)


def test_entity_solid_survives_the_hold_and_velocity_needs_twenty_skus():
    held = common.judge(
        name="X",
        own="",
        goods="лекарства",
        wholesale=False,
        china=None,
        solid=True,
        skus=40,
    )
    assert (held.held, held.signal_types) == (True, ("entity_solid",))
    kept = common.judge(
        name="X", own="", goods="оборудование", wholesale=False, china=None, skus=20
    )
    assert kept.signal_types == ("marketplace_velocity",)
    under = common.judge(
        name="X", own="", goods="оборудование", wholesale=False, china=None, skus=19
    )
    assert under.signal_types == ()


def test_the_wholesale_and_individual_patterns():
    for text in (
        "Оптовая торговля",
        "Оптово-розничная торговля",
        "Стройматериалы оптом",
        "Ulgurji savdo",
        "stroymaterialy-optom",
        "optovaya-torgovlya",
        "мелкий опт",
    ):
        assert WHOLESALE_RUBRIC_RE.search(text), text
    for text in ("Оптика", "оптический прицел", "Розничная торговля", "khoztovary"):
        assert not WHOLESALE_RUBRIC_RE.search(text), text
    for name in ("Seller 04 ИндП", "Namuna ИП", "YaTT Namuna", "IP Namuna", "Namuna ИнП"):
        assert INDIVIDUAL_FORM_RE.search(name) and common.individual(name), name
    for name in (
        '"XITOY UPAKOVSHIK" ИП ООО',
        "Ripley",
        "Ip man",
        "ИПОТЕКА",
        "Namuna ООО",
    ):
        assert not common.individual(name), name
    assert common.legal_form_in('"XITOY UPAKOVSHIK" ИП ООО') == "ООО"
    assert common.legal_form_in('"Global Trade" СП ООО') == "ООО"
    assert common.legal_form_in("Seller 04 ИндП") == "ИП"
    assert common.legal_form_in('"Trading Venture" ЧП') == "ЧП"


def test_phones_as_written_split_a_field_and_keep_one_number_once():
    assert common.phones_as_written(
        ["+998(90)000-00-33", "(998) 90-000-00-33, 90-000-00-34", "", "не номер"]
    ) == ["+998(90)000-00-33", "90-000-00-34"]
    assert common.phones_as_written(["+998 (90) 000-XX-XX"]) == []


# --- the cadences and the fan-out ------------------------------------------------


def test_the_weekly_diff_is_a_full_crawl_on_the_1st_to_7th():
    assert d.crawl_mode(None, THURSDAY) == "diff"
    assert d.crawl_mode("full", THURSDAY) == "full"
    first_thursday = datetime(2026, 11, 5, 23, 0, tzinfo=TASHKENT)
    assert d.crawl_mode(None, first_thursday) == "full"
    with pytest.raises(ValueError):
        d.crawl_mode("night", THURSDAY)
    # The day is Tashkent's: 19:00 UTC on the 31st is 00:00 on the 1st here.
    utc_eve = datetime(2026, 10, 31, 19, 0, tzinfo=ZoneInfo("UTC"))
    assert d.crawl_mode(None, utc_eve) == "full"


def test_one_site_a_night_and_the_unbuilt_ones_have_their_nights():
    def night(day: int) -> list[str]:
        return [
            s.site for s in d.tonight(datetime(2026, 11, day, 23, 0, tzinfo=TASHKENT))
        ]

    assert [night(day) for day in range(1, 9)] == [
        ["goldenpages"],
        ["yellowpages"],
        ["topuz"],
        ["glotr"],
        ["prom"],
        ["uzts"],
        ["openinfo", "uzpharm"],
        [],
    ]


def test_the_registry_builds_four_sites_and_owes_four():
    built = {s.site: s.source_key for s in d.SUBSOURCES if s.spec is not None}
    assert built == {
        "goldenpages": "directories_goldenpages",
        "yellowpages": "directories_yellowpages",
        "topuz": "directories_topuz",
        "glotr": "directories_glotr",
    }
    owed = {s.site: s.source_key for s in d.SUBSOURCES if s.spec is None}
    assert owed == {
        "prom": "directories_prom",
        "uzts": "directories",
        "openinfo": "directories",
        "uzpharm": "directories",
    }
    assert all(s.owed for s in d.SUBSOURCES if s.spec is None)


def test_a_sub_source_runs_only_when_built_switched_on_and_not_paused():
    by_site = {s.site: s for s in d.SUBSOURCES}
    on = d.RowState(enabled=True, paused_until=None)
    gold, uzts, prom = by_site["goldenpages"], by_site["uzts"], by_site["prom"]
    assert d.skip_reason(gold, on, THURSDAY) is None
    assert d.skip_reason(gold, d.RowState(False, None), THURSDAY) == "disabled"
    later = THURSDAY + timedelta(days=7)
    assert d.skip_reason(gold, d.RowState(True, later), THURSDAY) == "paused"
    assert d.skip_reason(gold, d.RowState(True, THURSDAY), THURSDAY) is None
    assert d.skip_reason(gold, None, THURSDAY) == "no_sources_row"
    # top.uz has its row (0004): with none it is not run.
    top = by_site["topuz"]
    assert d.skip_reason(top, None, THURSDAY) == "no_sources_row"
    assert d.skip_reason(top, d.RowState(True, later), THURSDAY) == "paused"
    # A site with no row of its own (uzts, owed) would run under the parent's
    # switch, which the runner already read — once it is built.
    assert d.skip_reason(uzts, None, THURSDAY) == "not_built"
    assert d.skip_reason(prom, on, THURSDAY) == "not_built"


def test_the_seeds_come_from_the_parent_s_config_on_the_site_s_own_host():
    gold = next(s for s in d.SUBSOURCES if s.site == "goldenpages")
    assert d.seeds_for(gold, {}) == gp.SEEDS
    assert d.seeds_for(gold, {"seeds": {"yellowpages": ["x"]}}) == gp.SEEDS
    config = {
        "seeds": {
            "goldenpages": [
                "https://www.goldenpages.uz/rubrics/?Id=1686",
                "https://evil.example.uz/rubrics/?Id=1",
                "http://www.goldenpages.uz/rubrics/?Id=1",
                7,
            ]
        }
    }
    assert d.seeds_for(gold, config) == ("https://www.goldenpages.uz/rubrics/?Id=1686",)


def entry(
    *, complete: bool, card: str | None = "https://x/c", **attrs: object
) -> common.Entry:
    obs = Observation(
        "directories", "x:1", "u", THURSDAY, "", "company", attrs=dict(attrs)
    )
    return common.Entry("x:1", card, obs, complete=complete)


def test_a_card_is_asked_for_unless_a_complete_row_settles_the_company():
    assert d.needs_card(entry(complete=False))
    assert d.needs_card(entry(complete=False, hold="category"))
    assert d.needs_card(entry(complete=True))
    assert not d.needs_card(entry(complete=True, hold="category"))
    competitor = entry(complete=True)
    assert competitor.observation is not None
    competitor.observation.signal_types = ["competitor"]
    assert not d.needs_card(competitor)
    assert not d.needs_card(entry(complete=False, card=None))
    assert d.needs_card(common.Entry("x:2", "https://x/c2"))


def test_the_paginator_stops_on_an_empty_page_and_past_the_last():
    spec = gp.SPEC
    one = common.Entry("x:1", None)
    empty = common.ListPage(entries=())
    assert d.stop_reason(empty, spec=spec, asked="a", final="a", firsts=set()) == "empty"
    past = common.ListPage(entries=(one,), page=13, last_page=12)
    assert (
        d.stop_reason(past, spec=spec, asked="a", final="a", firsts=set()) == "past_last"
    )
    fine = common.ListPage(entries=(one,), page=12, last_page=12)
    assert d.stop_reason(fine, spec=spec, asked="a", final="a", firsts=set()) is None
    assert (
        d.stop_reason(fine, spec=spec, asked="a", final="a", firsts={"x:1"}) == "repeat"
    )
