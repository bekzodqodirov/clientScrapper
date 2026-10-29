"""The Chinese brands of 03 §2.10, grouped — the one list.

``BRANDS`` is 03 §2.10's ~60 names under the group keys
``services/signals.BULKY_BRAND_GROUPS`` reads (04 §5.1: an official
distributor of a bulky group is container scale, a dealer of one is
wholesale; ``electronics`` is China origin only), so a collector that finds a
brand writes ``attrs.brand`` and ``attrs.brand_group`` and the signal rule
does the rest. ``brand_dealers`` (T-34) adds the per-brand domains and the
queries; ``exhibitors`` (T-32, 03 §2.8) reads the names on an exhibitor's
row — «brand names in the profile → ``brand_distributor(official=False)``».

``brands_in`` reads a brand as a whole word, written the way a brand writes
itself or in capitals (exhibitor lists print names upper-cased): never
case-blind, because «Honor», «Triangle», «Jack» and «Great Wall» are ordinary
words too, and «we honor our clients» is no dealership. The longest spelling
wins where two overlap («BYD Battery» before «BYD»).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

BrandGroup = Literal[
    "hvac_appliances",
    "solar_power",
    "vehicles",
    "machinery",
    "tyres",
    "electronics",
    "plumbing_lighting",
]


@dataclass(frozen=True, slots=True)
class Brand:
    """One brand: its name as 03 §2.10 writes it, its group, and the other
    names it trades under (``aliases``)."""

    name: str
    group: BrandGroup
    aliases: tuple[str, ...] = ()


def _group(group: BrandGroup, *names: str | tuple[str, ...]) -> tuple[Brand, ...]:
    found: list[Brand] = []
    for entry in names:
        if isinstance(entry, tuple):
            found.append(Brand(entry[0], group, entry[1:]))
        else:
            found.append(Brand(entry, group))
    return tuple(found)


# 03 §2.10, in its order. «Jack (sewing)» is the sewing-machine maker Jack;
# Huawei's solar inverters are sold by its Digital Power arm (the name the
# Power Uzbekistan list prints, tests/fixtures/exhibitors).
BRANDS: Final[tuple[Brand, ...]] = (
    *_group(
        "hvac_appliances",
        "Gree",
        "Haier",
        "Midea",
        "Hisense",
        "TCL",
        "AUX",
        "Chigo",
        "Hofmann",
        "Welkin",
    ),
    *_group(
        "solar_power",
        ("Huawei FusionSolar", "FusionSolar", "Huawei Digital Power"),
        "Jinko",
        "LONGi",
        "JA Solar",
        "Trina",
        "Sungrow",
        "Growatt",
        "Deye",
        "BYD Battery",
        "CATL",
        "Chint",
        "Delixi",
    ),
    *_group(
        "vehicles",
        "Chery",
        "BYD",
        "Changan",
        "Geely",
        "Haval",
        "Great Wall",
        "JAC",
        "Jetour",
        "Exeed",
        "Omoda",
        "Dongfeng",
        "FAW",
        "Foton",
        ("Sinotruk HOWO", "Sinotruk", "HOWO"),
        "Shacman",
        "Yutong",
        "King Long",
    ),
    *_group(
        "machinery",
        "Sany",
        "XCMG",
        "Zoomlion",
        "LiuGong",
        "Lonking",
        "SDLG",
        "Shantui",
        "Weichai",
        "Haitian",
        "Yizumi",
        "Bodor",
        "Jack",
        "Zoje",
    ),
    *_group("tyres", "Linglong", "Sailun", "Triangle", "Doublestar", "Aeolus"),
    *_group(
        "electronics",
        "Xiaomi",
        "OPPO",
        "vivo",
        "realme",
        "Honor",
        "ZTE",
        "Lenovo",
        "Hikvision",
        "Dahua",
        "TP-Link",
        "Tenda",
    ),
    *_group("plumbing_lighting", "Frap", "Ledeme", "Zerix", "Opple", "Yeelight"),
)


def _spellings(written: str) -> set[str]:
    """As written, in capitals, and with a capital first letter («vivo»
    opens a sentence as «Vivo»)."""
    return {written, written.upper(), written[:1].upper() + written[1:]}


_BY_SPELLING: Final[dict[str, Brand]] = {
    spelling: brand
    for brand in BRANDS
    for written in (brand.name, *brand.aliases)
    for spelling in _spellings(written)
}
_BRAND_RE: Final = re.compile(
    r"(?<![\w-])(?:"
    + "|".join(
        re.escape(s).replace(r"\ ", r"\s+")
        for s in sorted(_BY_SPELLING, key=len, reverse=True)
    )
    + r")(?![\w-])"
)
_SPACES: Final = re.compile(r"\s+")


def brands_in(text: str) -> list[Brand]:
    """The brands ``text`` names, each once, in the order it names them."""
    found: list[Brand] = []
    for match in _BRAND_RE.finditer(text or ""):
        brand = _BY_SPELLING.get(_SPACES.sub(" ", match.group(0)))
        if brand is not None and brand not in found:
            found.append(brand)
    return found
