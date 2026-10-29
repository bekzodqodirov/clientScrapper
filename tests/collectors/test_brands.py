"""collectors/brands.py: 03 §2.10's brand list and its reader (T-32 reads it
on exhibitor rows; T-34 queries by it)."""

from __future__ import annotations

import pytest

from clientscrapper.collectors.brands import BRANDS, brand_hits, brands_in, own_office
from clientscrapper.services.signals import BULKY_BRAND_GROUPS


def names(text: str) -> list[str]:
    return [b.name for b in brands_in(text)]


def test_the_groups_are_the_ones_the_signal_rule_reads():
    """04 §5.1: every bulky group is signals.BULKY_BRAND_GROUPS' key, and
    electronics (China origin only) is the one group outside it."""
    groups = {b.group for b in BRANDS}
    assert groups - BULKY_BRAND_GROUPS == {"electronics"}
    assert groups >= BULKY_BRAND_GROUPS
    assert len(BRANDS) == len({b.name for b in BRANDS}) >= 60


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("DAHUA TECHNOLOGY", ["Dahua"]),
        ("HUAWEI DIGITAL POWER", ["Huawei FusionSolar"]),
        ("Gree va Midea konditsionerlari", ["Gree", "Midea"]),
        ("BYD Battery omborlari", ["BYD Battery"]),
        ("BYD avtomobillari", ["BYD"]),
        ("Sinotruk HOWO, Shacman", ["Sinotruk HOWO", "Shacman"]),
        ("HOWO yuk mashinalari", ["Sinotruk HOWO"]),
        ("Vivo smartfonlari", ["vivo"]),
        ("TP-LINK routerlari", ["TP-Link"]),
        ("Great  Wall avtomobillari", ["Great Wall"]),
        ("Honor smartfonlari", ["Honor"]),
        ("HONOR SMARTFONLARI", ["Honor"]),
        ("Jack tikuv mashinalari", ["Jack"]),
        ("TRIANGLE shinalari", ["Triangle"]),
        ("AUX konditsionerlari", ["AUX"]),
    ],
)
def test_a_brand_written_as_brands_write_themselves(text, found):
    assert names(text) == found


@pytest.mark.parametrize(
    "text",
    [
        "we honor our clients",  # «Honor» is a word too: never case-blind
        "a golden triangle of trade",
        "jack of all trades",
        "Greenland",  # Gree inside a word
        "TP-Linked",
        "",
    ],
)
def test_an_ordinary_word_is_no_brand(text):
    assert brands_in(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "GREAT WALL MEBEL, LLC",
        "TRIANGLE DESIGN, LLC",
        "HONOR TEXTILE, LLC",
        "JACK SERVICE",
        "AUX GROUP",
        "FRAP",
        "VIVO MEBEL",
        "Honor Textile",
        "Great Wall restorani",
        "HOFMANN GMBH",
    ],
)
def test_a_common_word_brand_needs_its_goods_beside_it(text):
    """An exhibitor list prints every name in capitals, so case says nothing
    there: a brand that is an ordinary word or a firm's own name too is read
    only beside a word of its goods (a furniture maker called «Great Wall» is
    no car dealer)."""
    assert brands_in(text) == []


def test_a_hit_is_the_spelling_the_text_has():
    """The quote of a brand signal is a fragment of the text: «HOWO», not the
    list's «Sinotruk HOWO»."""
    [howo, shacman] = brand_hits("HOWO va Shacman yuk mashinalari")
    assert (howo.brand.name, howo.written, howo.start, howo.end) == (
        "Sinotruk HOWO",
        "HOWO",
        0,
        4,
    )
    assert (shacman.brand.name, shacman.written) == ("Shacman", "Shacman")
    [huawei] = brand_hits("HUAWEI DIGITAL POWER")
    assert huawei.written == "HUAWEI DIGITAL POWER"


@pytest.mark.parametrize(
    ("name", "brand"),
    [
        ("DAHUA TECHNOLOGY", "Dahua"),
        ("HUAWEI DIGITAL POWER", "Huawei FusionSolar"),
        ("MIDEA UZBEKISTAN, LLC", "Midea"),
        ("Representative office of Haier in Uzbekistan", "Haier"),
        ("GREE ELECTRIC APPLIANCES", "Gree"),
        ("CHERY", "Chery"),
    ],
)
def test_a_brand_s_own_office_is_the_brand_and_its_corporate_words(name, brand):
    """README item 12: «local offices of foreign brands (DAHUA TECHNOLOGY,
    HUAWEI DIGITAL POWER at Power)» — the supply side, no dealer."""
    found = own_office(name)
    assert found is not None and found.name == brand


@pytest.mark.parametrize(
    "name",
    [
        "AKFA GREE, LLC",
        "GREE KONDITSIONER SAVDO",
        "CHERY TASHKENT AVTO",
        "HAIER VA MIDEA",
        "4D OPALUBKA",
        "DATANG LIGHTING TECHNOLOGY CO., LTD",
    ],
)
def test_a_dealer_names_itself_beside_the_brand(name):
    assert own_office(name) is None


def test_each_brand_once_in_the_order_named():
    assert names("Chery, Geely, Chery") == ["Chery", "Geely"]
