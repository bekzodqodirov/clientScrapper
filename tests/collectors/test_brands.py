"""collectors/brands.py: 03 §2.10's brand list and its reader (T-32 reads it
on exhibitor rows; T-34 queries by it)."""

from __future__ import annotations

import pytest

from clientscrapper.collectors.brands import BRANDS, brands_in
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
        ("Great  Wall", ["Great Wall"]),
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


def test_each_brand_once_in_the_order_named():
    assert names("Chery, Geely, Chery") == ["Chery", "Geely"]
