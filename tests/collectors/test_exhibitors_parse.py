"""The exhibitors parsers on the recorded lists (03 §1.5, §2.8; 07 T-32).

Every fixture is ``tests/fixtures/exhibitors`` (README: what each file
proves, recorded 2026-10-29); the one hand-written file is
``era_no_country_synthetic.json`` (named so, and said so in the README): no
recorded list prints a row without its country, and 03 §2.8's legal-form
fallback needs one. Counts are the README's own («Row counts against the
organiser's figures»), so a parser that drifts from what the recorder saw is
red here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.exhibitors import (
    Edition,
    Exhibitors,
    Row,
    _observation,
    edition_dates,
    held_on,
    is_uzbek,
    slug,
    uz_form,
)
from clientscrapper.services import signals
from clientscrapper.services.normalise import to_observation_json
from tests.fakes import TASHKENT

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "exhibitors"
AT = datetime(2026, 10, 29, 19, 45, tzinfo=TASHKENT)
CATALOGUE = "https://catalog.automechanika.uz/en/expositions/exposition/7012"
PAGES = f"{CATALOGUE}-automechanika-tashkent-comtrux-tashkent-bautrux-tashkent-2026.html"
AM_2026 = Edition("7012", 2026, None, "Exhibitors Catalogue 2026", CATALOGUE)
# show → (host, the 2026 edition id) — README «Files».
ERA = {
    "uzbuild": ("uzbuild.uz", "461"),
    "power": ("power-uzbekistan.uz", "474"),
    "caitme": ("caitme.uz", "480"),
    "mebelexpo": ("mebelexpo.uz", "463"),
    "textileexpo": ("textileexpo.uz", "478"),
}


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def sha(name: str) -> str:
    return hashlib.sha256(raw(name)).hexdigest()


def endpoint(show: str, start: int = 0, length: int = 500, draw: int = 1) -> str:
    host, edition = ERA[show]
    return (
        f"https://{host}/ERAForms/companies_list.php?l=en&exhibition={edition}"
        f"&draw={draw}&start={start}&length={length}"
    )


def era_rows(show: str, name: str | None = None, **kw):  # type: ignore[no-untyped-def]
    """The 2026 answer of ``show``, read as the crawl reads it: the edition
    with the dates its index prints (``EDITIONS``)."""
    host, edition = ERA[show]
    [end] = [e for i, _, e in EDITIONS[show] if i == edition]
    fixture = name or f"{show}_2026_companies_all.json"
    return Exhibitors.parse_era_rows(
        raw(fixture),
        url=endpoint(show),
        fetched_at=AT,
        show=show,
        edition=Edition(edition, 2026, end),
        **kw,
    )


def expodat(name: str, url: str = CATALOGUE, **kw):  # type: ignore[no-untyped-def]
    return Exhibitors.parse_expodat_page(
        raw(name), url=url, fetched_at=AT, show="automechanika", edition=AM_2026, **kw
    )


# --- ERA: the editions ------------------------------------------------------------------

EDITIONS = {
    "uzbuild": [
        ("401", 2024, date(2024, 3, 1)),
        ("430", 2025, date(2025, 2, 27)),
        ("461", 2026, date(2026, 2, 12)),
        ("490", 2027, date(2027, 3, 18)),
    ],
    "power": [
        ("409", 2024, date(2024, 5, 16)),
        ("440", 2025, date(2025, 5, 15)),
        ("474", 2026, date(2026, 5, 14)),
        ("501", 2027, date(2027, 5, 14)),
    ],
    "caitme": [
        ("362", 2024, date(2024, 9, 14)),
        ("446", 2025, date(2025, 9, 11)),
        ("480", 2026, date(2026, 9, 10)),
        ("507", 2028, date(2028, 9, 8)),
    ],
    "mebelexpo": [
        ("407", 2024, date(2024, 5, 30)),
        ("443", 2025, date(2025, 2, 27)),
        ("463", 2026, date(2026, 4, 30)),
        ("499", 2027, date(2027, 4, 29)),
    ],
    # Two editions in 2024 and in 2025, one in 2026 (README item 16).
    "textileexpo": [
        ("410", 2024, date(2024, 5, 30)),
        ("442", 2024, date(2024, 10, 17)),
        ("452", 2025, date(2025, 5, 16)),
        ("472", 2025, date(2025, 9, 11)),
        ("478", 2026, date(2026, 5, 15)),
        ("504", 2027, date(2027, 4, 29)),
    ],
}


@pytest.mark.parametrize("show", sorted(EDITIONS))
def test_the_editions_are_the_changeyear_options_with_their_printed_dates(show):
    """The «Archive» placeholder (no value) and the filter selects (country,
    categories, location) are not editions."""
    editions = Exhibitors.parse_era_editions(
        raw(f"{show}_list_en.html"),
        url=f"https://{ERA[show][0]}/en/exhibitors-list",
        fetched_at=AT,
    )
    assert [(e.id, e.year, e.end) for e in editions] == EDITIONS[show]
    assert editions[0].label.startswith(str(EDITIONS[show][0][1]))


def test_an_unpublished_default_edition_still_lists_the_editions():
    """CAITME's page says «This information will be available soon» for its
    2028 default and carries no table: the select is still there."""
    editions = Exhibitors.parse_era_editions(
        raw("caitme_list_en.html"),
        url="https://caitme.uz/en/exhibitors-list",
        fetched_at=AT,
    )
    assert [e.id for e in editions] == ["362", "446", "480", "507"]


def test_edition_dates_read_both_shapes_and_nothing_else():
    assert edition_dates("2024 (27 February - 1 March 2024)") == (
        date(2024, 2, 27),
        date(2024, 3, 1),
    )
    assert edition_dates("2026 (10 - 12 February 2026)") == (
        date(2026, 2, 10),
        date(2026, 2, 12),
    )
    assert edition_dates("2027") is None
    assert edition_dates("2026 (10 - 12 Fevral 2026)") is None
    assert edition_dates("2026 (30 - 31 February 2026)") is None
    # A show over the new year starts in the year before the one printed.
    assert edition_dates("2027 (30 December - 2 January 2027)") == (
        date(2026, 12, 30),
        date(2027, 1, 2),
    )


# --- ERA: the rows ----------------------------------------------------------------------

# README: rows seen (every country) · Uzbek rows · the site's recordsTotal.
COUNTS = {
    "uzbuild": (341, 78, 263, 341),
    "power": (218, 19, 199, 218),
    "caitme": (170, 41, 129, 170),
    "mebelexpo": (80, 25, 55, 80),
    "textileexpo": (144, 57, 87, 144),
}
# The Uzbek rows that are a brand's own office (README item 12): skipped.
OFFICES = {"power": {"DAHUA TECHNOLOGY", "HUAWEI DIGITAL POWER"}}


@pytest.mark.parametrize("show", sorted(COUNTS))
def test_each_show_keeps_its_uzbek_rows_and_counts_the_foreign_ones(show):
    page = era_rows(show)
    offices = OFFICES.get(show, set())
    assert (
        page.seen,
        len(page.kept) + page.skipped,
        page.foreign,
        page.total,
    ) == COUNTS[show]
    assert page.skipped == len(offices)
    assert not offices & {o.company_name_raw for o in page.kept}
    assert all(o.attrs["country"] == "Uzbekistan" for o in page.kept)
    assert all(o.kind == "exhibitor" and o.source == "exhibitors" for o in page.kept)
    assert all(o.raw_sha256 == sha(f"{show}_2026_companies_all.json") for o in page.kept)


def test_an_uzbek_era_row_with_its_exact_fields():
    first = era_rows("uzbuild").kept[0]
    assert first == Observation(
        source="exhibitors",
        source_ref="uzbuild:2026:4d-opalubka",
        url="https://uzbuild.uz/en/exhibitor-info-461-1155",
        fetched_at=AT,
        raw_sha256=sha("uzbuild_2026_companies_all.json"),
        kind="exhibitor",
        company_name_raw="4D OPALUBKA",
        legal_form=None,
        signal_types=["exhibitor"],
        excerpt="4D OPALUBKA · Uzbekistan · C 106 · Uzexpocentre NEC",
        external_ids={"exhibitor_slug": "era:1155"},
        attrs={
            "show": "uzbuild",
            "year": 2026,
            "edition": "461",
            "stand": "C 106",
            "pavilion": None,
            "country": "Uzbekistan",
            "venue": "Uzexpocentre NEC",
        },
        # The show's last day, as its index prints it (10 - 12 February 2026).
        published_at=datetime(2026, 2, 12, tzinfo=TASHKENT),
    )


def test_the_legal_form_is_the_one_written_in_the_name():
    by_name = {o.company_name_raw: o for o in era_rows("uzbuild").kept}
    akfa = by_name["AKFA BUILDING MATERIALS, LLC / YTONG"]
    assert (akfa.legal_form, akfa.source_ref, akfa.external_ids) == (
        "LLC",
        "uzbuild:2026:akfa-building-materials-llc-ytong",
        {"exhibitor_slug": "era:1235"},
    )
    # «PE» is no form LEGAL_FORM_RE knows; the row is kept on its country.
    assert by_name["SAMA GROUP, PE"].legal_form is None


@pytest.mark.parametrize("show", sorted(ERA))
def test_a_page_of_the_endpoint_is_a_slice_of_the_whole_answer(show):
    """``start``/``length`` pagination (README «Files»: page 2 = rows 100–199,
    MebelExpo's 40–79)."""
    start, length = (40, 40) if show == "mebelexpo" else (100, 100)
    whole = era_rows(show)
    page = era_rows(show, f"{show}_2026_companies_p2.json")
    assert page.total == whole.total
    assert page.refs == whole.refs[start : start + length]
    kept = {o.source_ref for o in page.kept}
    assert [o.source_ref for o in whole.kept if o.source_ref in kept] == [
        o.source_ref for o in page.kept
    ]


def test_a_row_with_no_location_is_read():
    """Power's one null ``location`` is a Chinese row; CAITME's is GOLDEN
    PAGES, kept until the calendar's ``exclude`` names it."""
    power = era_rows("power")
    assert power.seen == 218
    golden = [o for o in era_rows("caitme").kept if o.company_name_raw == "GOLDEN PAGES"]
    assert len(golden) == 1 and "venue" not in golden[0].attrs
    assert golden[0].excerpt == "GOLDEN PAGES · Uzbekistan · F 30"


def test_the_names_the_calendar_excludes_are_skipped():
    page = era_rows("uzbuild", exclude=("Golden Pages",))
    assert (page.seen, len(page.kept), page.foreign, page.skipped) == (341, 77, 263, 1)
    assert "GOLDEN PAGES" not in {o.company_name_raw for o in page.kept}


def era_synthetic(data: list[object], **kw):  # type: ignore[no-untyped-def]
    """An ERA answer written here, not recorded: the shapes no recorded
    answer shows (a row that is no row, an excluded name abroad)."""
    body = json.dumps(
        {"draw": 1, "recordsTotal": len(data), "recordsFiltered": len(data), "data": data}
    ).encode()
    return Exhibitors.parse_era_rows(
        body,
        url="https://uzbuild.uz/ERAForms/companies_list.php?l=en&exhibition=999",
        fetched_at=AT,
        show="uzbuild",
        edition=Edition("999", 2026),
        **kw,
    )


def era_cell(company_id: int, name: str) -> str:
    return f'<a href="/en/exhibitor-info-999-{company_id}">{name}</a>'


def test_a_row_no_name_reads_in_is_counted_seen_and_skipped():
    page = era_synthetic(
        [
            [era_cell(1, "NAMUNA"), "Uzbekistan", "A 1", "Hall 1", ""],
            [12345],
            ["<a href='/en/exhibitor-info-999-3'> </a>", "Uzbekistan", "A 3", "", ""],
        ]
    )
    assert (page.seen, len(page.kept), page.skipped, page.foreign) == (3, 1, 2, 0)


def test_an_excluded_name_abroad_is_foreign_not_skipped():
    """The country is read first: «GOLDEN PAGES» of another country is the
    supply side, and only an Uzbek row of the calendar's names is skipped."""
    page = era_synthetic(
        [
            [era_cell(1, "GOLDEN PAGES"), "Kazakhstan", "A 1", "", ""],
            [era_cell(2, "GOLDEN PAGES"), "Uzbekistan", "A 2", "", ""],
        ],
        exclude=("GOLDEN PAGES",),
    )
    assert (page.seen, page.kept, page.foreign, page.skipped) == (2, (), 1, 1)


def test_a_foreign_only_edition_keeps_nothing():
    """NEGATIVE (README): Power 2027 has one Russian Federation row."""
    page = Exhibitors.parse_era_rows(
        raw("power_2027_companies_foreign_only.json"),
        url="https://power-uzbekistan.uz/ERAForms/companies_list.php?l=en&exhibition=501",
        fetched_at=AT,
        show="power",
        edition=Edition("501", 2027),
    )
    assert (page.kept, page.seen, page.foreign, page.total) == ((), 1, 1, 1)


def test_the_count_answer_names_the_site_s_total():
    """CAITME 2025 at length=1: recordsTotal 163 (organiser: 210); its one row
    is a Türkiye sponsor."""
    page = Exhibitors.parse_era_rows(
        raw("caitme_2025_count.json"),
        url="https://caitme.uz/ERAForms/companies_list.php?l=en&exhibition=446",
        fetched_at=AT,
        show="caitme",
        edition=Edition("446", 2025),
    )
    assert (page.total, page.seen, page.foreign, page.kept) == (163, 1, 1, ())


@pytest.mark.parametrize(
    "name", ["uzbuild_archive_p1.html", "uzbuild_list_en.html", "caitme_list_en.html"]
)
def test_an_answer_that_is_no_datatables_json_is_refused(name):
    """The dead Bitrix archive (a soft 404 with the home page), a list page:
    a ValueError, which the collector counts as a failed list."""
    with pytest.raises(ValueError):
        era_rows("uzbuild", name)


def uzbek_row(
    name: str, description: str | None = None, company_id: str | None = "777"
) -> Row:
    return Row(
        name=name,
        country="Uzbekistan",
        stand="A 1",
        company_id=company_id,
        card_url=None,
        description=description,
    )


def observe(row: Row, *, edition: Edition = AM_2026, held: date | None = None):  # type: ignore[no-untyped-def]
    return _observation(
        row,
        show="automechanika",
        edition=edition,
        platform="expodat",
        url=CATALOGUE,
        fetched_at=AT,
        sha="0" * 64,
        cells=(row.name, row.stand, row.country, row.description),
        held=held,
    )


def rule_signals(obs: Observation) -> dict[str, signals.SignalDraft]:
    """The signals the rules derive from ``obs`` as stored (04 §5.3)."""
    parsed = to_observation_json(obs, collector_version="exhibitors@1")
    evidence = signals.EvidenceRef(id=1, source_key="exhibitors", fetched_at=AT)
    return {
        d.key: d for d in signals.from_rules(evidence, parsed, signals.CompanyFacts())
    }


def test_a_brand_s_own_office_on_the_list_is_the_supply_side_not_a_dealer():
    """README item 12: DAHUA TECHNOLOGY and HUAWEI DIGITAL POWER at Power are
    local offices of foreign brands, no lead — not stored, counted skipped
    (03 §2.8: «foreign rows are not stored as companies (supply side)»)."""
    page = era_rows("power")
    names = {o.company_name_raw for o in page.kept}
    assert "DAHUA TECHNOLOGY" not in names and "HUAWEI DIGITAL POWER" not in names
    assert page.skipped == 2
    plain = next(o for o in page.kept if o.company_name_raw == "AKKUENERGO, LLC")
    assert plain.signal_types == ["exhibitor"] and "brand" not in plain.attrs


def test_brands_on_a_row_are_a_dealership_never_an_official_one():
    """03 §2.8: brand names in the profile → brand_distributor(official=False);
    ``attrs.brand`` is the brand as the row spells it, so the signal rule's
    quote is a fragment of the excerpt (a card cites it)."""
    obs = observe(uzbek_row("AKFA GREE, LLC"))
    assert obs.signal_types == ["exhibitor", "brand_distributor"]
    assert {k: obs.attrs[k] for k in ("brand", "brand_group", "official", "brands")} == {
        "brand": "GREE",
        "brand_group": "hvac_appliances",
        "official": False,
        "brands": ["Gree"],
    }
    found = rule_signals(obs)
    assert found["vol_wholesale_bulky"].quote == "GREE"
    assert found["cn_origin"].quote == "GREE"
    assert "GREE" in obs.excerpt


def test_a_brand_named_by_an_alias_is_quoted_as_the_row_writes_it():
    """«HOWO» is 03 §2.10's «Sinotruk HOWO»: the quote is «HOWO»."""
    obs = observe(
        uzbek_row("NAMUNA AVTO", "HOWO va Shacman yuk mashinalari, ehtiyot qismlar")
    )
    assert (obs.attrs["brand"], obs.attrs["brands"]) == (
        "HOWO",
        ["Sinotruk HOWO", "Shacman"],
    )
    assert rule_signals(obs)["cn_origin"].quote == "HOWO"


def test_a_brand_in_the_description_alone_is_read():
    obs = observe(uzbek_row("NAMUNA SAVDO", "Chery va Haval ehtiyot qismlari optom"))
    assert "brand_distributor" in obs.signal_types
    assert obs.attrs["brands"] == ["Chery", "Haval"]


def test_a_common_word_in_a_shouted_name_is_no_brand():
    """«GREAT WALL MEBEL, LLC» is a furniture maker: no dealership."""
    obs = observe(uzbek_row("GREAT WALL MEBEL, LLC", "Mebel ishlab chiqaramiz"))
    assert obs.signal_types == ["exhibitor"] and "brand" not in obs.attrs


def test_a_cargo_firm_by_its_own_words_is_marked_competitor():
    """COMPETITOR_SELF_RE on the description: a row whose profile offers the
    cargo service is a forwarder, whatever its name."""
    obs = observe(
        uzbek_row("NAMUNA TRADE", "Xitoydan yuk tashish xizmati: kargo va rastamojka")
    )
    assert obs.signal_types == ["exhibitor", "competitor"]
    # «reliable logistics» in a parts distributor's profile is no cargo offer.
    autozip = expodat("automechanika_catalog_2026_p1.html").kept[1]
    assert "competitor" not in autozip.signal_types


def test_a_name_with_no_latin_slug_is_keyed_by_its_company_id():
    obs = observe(uzbek_row("有限公司", company_id="150498"))
    assert obs.source_ref == "automechanika:2026:id150498"


def test_a_row_is_dated_by_its_show_not_by_the_crawl():
    """An archive read a year on is last year's evidence: ``published_at`` is
    the show's last day — the printed one, else the calendar's, else the
    year's last — never after the read."""
    archive = Edition("401", 2024, date(2024, 3, 1))
    assert observe(uzbek_row("NAMUNA"), edition=archive).published_at == datetime(
        2024, 3, 1, tzinfo=TASHKENT
    )
    assert observe(uzbek_row("NAMUNA"), held=date(2026, 10, 23)).published_at == (
        datetime(2026, 10, 23, tzinfo=TASHKENT)
    )
    # The date the site prints beats the calendar's guess.
    printed = Edition("504", 2027, date(2027, 4, 29))
    assert held_on(printed, datetime(2027, 6, 1, tzinfo=TASHKENT), date(2027, 5, 20)) == (
        datetime(2027, 4, 29, tzinfo=TASHKENT)
    )
    undated = Edition("6400", 2025)
    assert held_on(undated, AT) == datetime(2025, 12, 31, tzinfo=TASHKENT)
    assert held_on(AM_2026, AT) == datetime(2026, 10, 29, tzinfo=TASHKENT)
    parsed = to_observation_json(
        observe(uzbek_row("NAMUNA"), edition=archive), collector_version="exhibitors@1"
    )
    assert parsed["observed_at"] == "2024-03-01T00:00:00+05:00"


def test_a_cargo_firm_on_the_list_is_marked_competitor():
    """A forwarder's name (COMPETITOR_NAME_RE) — never a lead, kept so gate 2
    excludes it by name."""
    by_name = {o.company_name_raw: o for o in era_rows("uzbuild").kept}
    assert by_name["NUR LOGISTIC TASHCITY, LLC"].signal_types == [
        "exhibitor",
        "competitor",
    ]
    assert by_name["4D OPALUBKA"].signal_types == ["exhibitor"]


def test_the_turkish_shipping_firm_on_power_s_list_is_marked_competitor():
    """README item 12: «MARTRADE DENIZCILIK TASIMACILIK A.S.» (Power 2026) is a
    shipping and forwarding firm — competitor, never a lead."""
    by_name = {o.company_name_raw: o for o in era_rows("power").kept}
    assert by_name["MARTRADE DENIZCILIK TASIMACILIK A.S."].signal_types == [
        "exhibitor",
        "competitor",
    ]


@pytest.mark.parametrize("show", sorted(ERA))
def test_every_kept_row_stores_its_country_where_gate_3_reads_it(show):
    """04 §6 gate 3 reads ``parsed.hints.collector.attrs.country`` of an
    ``exhibitor`` observation (07 T-32, carried from wave 3)."""
    for obs in era_rows(show).kept:
        parsed = to_observation_json(obs, collector_version="exhibitors@1")
        assert parsed["kind"] == "exhibitor"
        assert parsed["hints"]["collector"]["attrs"]["country"] == "Uzbekistan"
        assert parsed["hints"]["collector"]["signal_types"][0] == "exhibitor"
        assert parsed["handles"]["exhibitor_slug"].startswith("era:")


def test_without_a_country_only_an_uzbek_legal_form_keeps_a_row():
    """SYNTHETIC (README «Synthetic»): MChJ and МЧЖ keep a row with no
    country, LLC does not; a printed Uzbek or Latin «Oʻzbekiston» is
    Uzbekistan."""
    page = Exhibitors.parse_era_rows(
        raw("era_no_country_synthetic.json"),
        url="https://uzbuild.uz/ERAForms/companies_list.php?l=en&exhibition=999",
        fetched_at=AT,
        show="uzbuild",
        edition=Edition("999", 2026),
    )
    assert [(o.company_name_raw, o.attrs["country"]) for o in page.kept] == [
        ("NAMUNA SAVDO, MChJ", None),
        ("НАМУНА ИМПОРТ МЧЖ", None),
        ("NAMUNA TEKSTIL, LLC", "Ўзбекистон"),
        ("NAMUNA METALL", "O'zbekiston"),
    ]
    assert (page.seen, page.foreign) == (5, 1)
    assert page.kept[1].source_ref == "uzbuild:2026:namuna-import-mchj"


# --- the rules the parsers share --------------------------------------------------------


@pytest.mark.parametrize(
    ("country", "name", "uzbek"),
    [
        ("Uzbekistan", "Namuna", True),
        ("Узбекистан", "Namuna", True),
        ("O'zbekiston", "Namuna", True),
        ("Oʻzbekiston", "Namuna", True),
        ("Ўзбекистон", "Namuna", True),
        ("UZBEKISTAN", "Namuna", True),
        ("China", "Namuna MChJ", False),
        ("Russian Federation", "Namuna, LLC", False),
        ("Kazakhstan", "Namuna ТОО", False),
        (None, "Namuna MChJ", True),
        (None, "«Namuna» XK", True),
        (None, "Namuna YaTT", True),
        (None, "Namuna, LLC", False),
        (None, "ООО Namuna", False),
        (None, "Namuna", False),
        ("", "Namuna QK", True),
    ],
)
def test_uzbek_by_the_country_column_else_by_an_uzbek_form(country, name, uzbek):
    assert is_uzbek(country, name) is uzbek


def test_the_uzbek_forms_are_only_the_uzbek_ones():
    assert uz_form("Namuna Savdo MChJ") == "MChJ"
    assert uz_form("Намуна ХЧК") == "ХЧК"
    assert uz_form("Namuna qo'shma korxonasi") == "qo'shma"
    assert uz_form("JV LLC Namuna") is None
    assert uz_form("Namuna AO") is None


def test_the_slug_is_latin_lower_case_words():
    assert slug('JV LLC "UZCHASYS"') == "jv-llc-uzchasys"
    assert slug("project «TAILEV»") == "project-tailev"
    assert slug("Тошкент Металлургия") == "toshkent-metallurgiya"
    assert slug("O'zbek Avto") == "ozbek-avto"
    assert slug("有限公司") == ""


# --- expodat: the links, the pages ------------------------------------------------------


def test_the_participants_page_links_the_2026_catalogue():
    """README item 1: the 2026 rows are behind «Exhibitors Catalogue 2026»."""
    links = Exhibitors.parse_expodat_links(
        raw("automechanika_participants_en.html"),
        url="https://automechanika.uz/eng/participants",
        fetched_at=AT,
    )
    assert links == [AM_2026]


def test_the_spec_s_list_page_links_only_the_removed_2025_catalogue_and_2024_has_none():
    links = Exhibitors.parse_expodat_links(
        raw("automechanika_2026_list_en.html"),
        url="https://automechanika.uz/exhibitorslisteng",
        fetched_at=AT,
    )
    assert [(e.id, e.year) for e in links] == [("6400", 2025)]
    # The 2024 list is a Tilda feed loaded by JS (README item 5): no link.
    assert (
        Exhibitors.parse_expodat_links(
            raw("automechanika_2024_list_en.html"),
            url="https://automechanika.uz/exhibitors-eng2024",
            fetched_at=AT,
        )
        == []
    )


def test_catalogue_page_1_with_its_pager():
    """48 rows: 41 China, 5 Uzbekistan (one is the organiser BMCA), 1 UAE, 1
    Saudi Arabia (README «Files»); 48 a page (``lim``), «End» at start=456."""
    page = expodat("automechanika_catalog_2026_p1.html", exclude=("BMCA",))
    assert (page.seen, len(page.kept), page.foreign, page.skipped) == (48, 4, 43, 1)
    assert [o.company_name_raw for o in page.kept] == [
        "Autoport",
        "AutoZIP",
        "BRZ",
        "Carville",
    ]
    assert (page.page_size, page.base, page.end_start, page.removed) == (
        48,
        PAGES.removeprefix("https://catalog.automechanika.uz"),
        456,
        False,
    )
    assert page.total is None
    assert len(set(page.refs)) == 48


def test_an_uzbek_catalogue_row_with_its_exact_fields():
    autozip = expodat("automechanika_catalog_2026_p1.html", held=date(2026, 10, 23)).kept[
        1
    ]
    assert autozip == Observation(
        source="exhibitors",
        source_ref="automechanika:2026:autozip",
        url="https://catalog.automechanika.uz/en/companies/company/150498-autozip.html",
        fetched_at=AT,
        raw_sha256=sha("automechanika_catalog_2026_p1.html"),
        kind="exhibitor",
        company_name_raw="AutoZIP",
        signal_types=["exhibitor"],
        excerpt=(
            "AutoZIP · AM272 · Uzbekistan · AUTOZIP is one of the leading "
            "distributors of automotive spare parts in Uzbekistan, supplying products "
            "from trusted international manufacturers. The company offers a wide "
            "range of components for passenger and commercial vehicles, ensuring "
            "consistent availability, reliable logistics, and professional customer "
            "support. AUTOZIP is the official distributor of brands including "
            "CHAMPION, ROTINGER, FEBEST, and JP Group, serving wholesalers, retailers, "
            "service centers, and automotive businesses across the country."
        ),
        external_ids={"exhibitor_slug": "expodat:150498"},
        attrs={
            "show": "automechanika",
            "year": 2026,
            "edition": "7012",
            "stand": "AM272",
            "pavilion": None,
            "country": "Uzbekistan",
        },
        # The calendar's last day of Automechanika 2026 (21–23 October).
        published_at=datetime(2026, 10, 23, tzinfo=TASHKENT),
    )


def test_a_row_without_a_stand_and_the_organiser_without_exclude():
    """«Autoport» and «BMCA» print no stand (README item 2); the card link
    is the row's, whatever its slug says («…/150621-bizol.html»)."""
    by_name = {
        o.company_name_raw: o for o in expodat("automechanika_catalog_2026_p1.html").kept
    }
    autoport = by_name["Autoport"]
    assert autoport.attrs["stand"] is None
    assert autoport.url.endswith("/en/companies/company/150621-bizol.html")
    assert autoport.excerpt.startswith("Autoport · Uzbekistan · - AVTOPORT — это")
    assert by_name["BMCA"].excerpt == "BMCA · Uzbekistan · World Brands Beyond Borders"


def test_page_2_continues_page_1_with_no_overlap():
    first = expodat("automechanika_catalog_2026_p1.html")
    second = expodat("automechanika_catalog_2026_p2.html", url=f"{PAGES}?start=48")
    assert second.seen == 48 and not set(first.refs) & set(second.refs)
    assert [o.company_name_raw for o in second.kept] == ["DAC", "EXZAP"]
    assert second.kept[1].url == (
        "https://catalog.automechanika.uz/en/companies/company/150345-exzap.html"
    )


def test_the_last_page_unescapes_the_names():
    """Six rows (4 Uzbekistan, 1 Russia, 1 Kazakhstan): 456 + 6 = 462; the
    «End» link is no link on the last page."""
    page = expodat("automechanika_catalog_2026_last.html", url=f"{PAGES}?start=456")
    assert (page.seen, page.foreign, page.end_start) == (6, 2, None)
    assert [(o.company_name_raw, o.attrs["stand"]) for o in page.kept] == [
        ("project «TAILEV»", "C061"),
        ('JV "World Trading Company" LLC', "AM260"),
        ('JV LLC "UZCHASYS"', "AM282"),
        ("JV UzEraeAlternator LLC", "AM288/2"),
    ]
    assert [o.legal_form for o in page.kept] == [None, "JV", "JV", "JV"]


def test_a_page_past_the_end_is_empty_not_removed():
    """NEGATIVE / paginator stop: «no results found», still HTTP 200."""
    page = expodat("automechanika_catalog_2026_past_end.html", url=f"{PAGES}?start=504")
    assert (page.kept, page.seen, page.removed) == ((), 0, False)


def test_a_removed_catalogue_is_told_apart_from_an_empty_page():
    """NEGATIVE: the 2025 catalogue lands on the login page, «The event was
    not found», no ``#scroll_list``."""
    page = expodat(
        "automechanika_catalog_2025_p1.html",
        url="https://catalog.automechanika.uz/automechanika-main.html",
    )
    assert (page.kept, page.seen, page.removed) == ((), 0, True)


def test_a_page_with_no_list_that_is_still_the_exposition_is_not_removed():
    """Only a page that left the exposition says the catalogue is gone; the
    same body at the exposition's own address is a page this parser cannot
    read (a redesign, an error page)."""
    page = expodat("automechanika_catalog_2025_p1.html", url=CATALOGUE)
    assert (page.kept, page.no_list, page.removed) == ((), True, False)
    later = expodat("automechanika_catalog_2025_p1.html", url=f"{PAGES}?start=48")
    assert (later.no_list, later.removed) == (True, False)


def test_only_the_rows_inside_the_list_are_rows():
    """A company link elsewhere on the page (a sponsor strip, a sidebar) is
    no row of the catalogue. Written here, not recorded."""
    body = (
        b'<div class="sponsors"><a href="/en/companies/company/1-x.html" '
        b'title="SPONSOR FIRM"><img class="img_country" title="Uzbekistan"></a></div>'
        b'<div id="scroll_list"><a href="/en/companies/company/2-y.html" '
        b'title="LIST FIRM"><span class="numb_stand"><b>A1</b></span>'
        b'<img class="img_country" title="Uzbekistan"></a></div>'
    )
    page = Exhibitors.parse_expodat_page(
        body, url=CATALOGUE, fetched_at=AT, show="automechanika", edition=AM_2026
    )
    assert [o.company_name_raw for o in page.kept] == ["LIST FIRM"]
    assert page.seen == 1


def test_no_catalogue_row_is_cut_mid_word_and_none_is_over_600():
    pages = ("p1", "p2", "last")
    for name in pages:
        for obs in expodat(f"automechanika_catalog_2026_{name}.html").kept:
            assert len(obs.excerpt) <= 600
            assert obs.excerpt.startswith(f"{obs.company_name_raw} · ")
