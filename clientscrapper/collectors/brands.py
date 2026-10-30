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
case-blind, and «we honor our clients» is no dealership. The longest spelling
wins where two overlap («BYD Battery» before «BYD»). A brand whose name is
also an ordinary word or a firm's own name (``Brand.common``: «Honor»,
«Triangle», «Jack», «Great Wall», «AUX» …) is read only beside a word of its
own goods (``GOODS_WORDS``, ``GOODS_NEAR`` characters either way): in a list
that prints every name in capitals «GREAT WALL MEBEL, LLC» and «HONOR
TEXTILE, LLC» are a furniture maker and a textile one, «Honor smartfonlari»
is the phone. ``brand_hits`` says where each brand stands and how the text
spells it — a quote is that spelling, a verbatim fragment («HOWO», not
«Sinotruk HOWO»).

``own_office`` reads the brand's own office in a name: the brand and nothing
but its corporate words, a legal form or a place («DAHUA TECHNOLOGY»,
«HUAWEI DIGITAL POWER», «Representative office of Haier in Uzbekistan») —
the supply side, no dealer (``tests/fixtures/exhibitors/README.md`` item 12:
«local offices of foreign brands» are no lead). A dealer names itself beside
the brand («AKFA GREE»).
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
    """One brand: its name as 03 §2.10 writes it, its group, the other names
    it trades under (``aliases``), and whether its name is an ordinary word
    or a firm's own name too (``common``: read only beside its goods)."""

    name: str
    group: BrandGroup
    aliases: tuple[str, ...] = ()
    common: bool = False


@dataclass(frozen=True, slots=True)
class BrandHit:
    """Where a text names a brand and how it spells it (``written``, a
    verbatim fragment of the text)."""

    brand: Brand
    written: str
    start: int
    end: int


# The brands whose name is an English word, a given name or a surname, or a
# word Uzbek firms take for themselves: «Honor Textile», «Triangle Design»,
# «Jack Service», «Great Wall Mebel», «AUX Group», «Vivo Mebel», «Tenda» (a
# curtain maker), «Haitian» (of Haiti), «Aeolus», «Welkin», «Hofmann» (a
# German surname: «HOFMANN GMBH» is on UzBuild 2026's list), «Frap».
_COMMON: Final = frozenset(
    {
        "AUX",
        "Welkin",
        "Hofmann",
        "Great Wall",
        "Jack",
        "Haitian",
        "Triangle",
        "Aeolus",
        "vivo",
        "Honor",
        "Tenda",
        "Frap",
    }
)


def _group(group: BrandGroup, *names: str | tuple[str, ...]) -> tuple[Brand, ...]:
    found: list[Brand] = []
    for entry in names:
        if isinstance(entry, tuple):
            found.append(Brand(entry[0], group, entry[1:], entry[0] in _COMMON))
        else:
            found.append(Brand(entry, group, common=entry in _COMMON))
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

# The goods of each group, the words that make a common-word brand a brand
# (Uzbek Latin and Cyrillic, Russian, English; stems, any case).
GOODS_WORDS: Final[dict[BrandGroup, tuple[str, ...]]] = {
    "hvac_appliances": (
        "konditsioner",
        "kondicioner",
        "кондиционер",
        "air condition",
        "air-condition",
        "appliance",
        "maishiy texnika",
        "бытов",
        "holodilnik",
        "xolodilnik",
        "холодильник",
        "isitish",
        "отоплен",
        "hvac",
        "heat pump",
    ),
    "solar_power": ("solar", "quyosh", "солнечн", "inverter", "инвертор"),
    "vehicles": (
        "avto",
        "авто",
        "automobile",
        "mashina",
        "машин",
        "truck",
        "грузов",
        "vehicle",
        "pickup",
    ),
    "machinery": (
        "tikuv",
        "тикув",
        "sewing",
        "швейн",
        "stanok",
        "станок",
        "станк",
        "machine",
        "mashina",
        "машин",
        "uskuna",
        "ускуна",
        "оборудован",
        "equipment",
        "injection",
        "термопласт",
    ),
    "tyres": ("shina", "шина", "шины", "шин", "tyre", "tire", "balon", "баллон"),
    "electronics": (
        "smartfon",
        "smartphone",
        "смартфон",
        "telefon",
        "телефон",
        "phone",
        "router",
        "роутер",
        "planshet",
        "планшет",
        "gadget",
        "wi-fi",
        "wifi",
    ),
    "plumbing_lighting": (
        "santexnika",
        "сантехник",
        "smesitel",
        "смесител",
        "faucet",
        "mixer",
        "kran",
        "кран",
        "chiroq",
        "светильник",
        "lighting",
        "lamp",
        "лампа",
    ),
}
GOODS_NEAR: Final = 60
_GOODS_RE: Final = {
    group: re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + ")", re.I)
    for group, words in GOODS_WORDS.items()
}


def _beside_its_goods(text: str, brand: Brand, start: int, end: int) -> bool:
    near = text[max(0, start - GOODS_NEAR) : end + GOODS_NEAR]
    return _GOODS_RE[brand.group].search(near) is not None


def brand_hits(text: str) -> list[BrandHit]:
    """Each brand ``text`` names, once, where it first names it and how it
    spells it there, in the order it names them."""
    text = text or ""
    found: list[BrandHit] = []
    for match in _BRAND_RE.finditer(text):
        brand = _BY_SPELLING.get(_SPACES.sub(" ", match.group(0)))
        if brand is None or any(hit.brand == brand for hit in found):
            continue
        if brand.common and not _beside_its_goods(
            text, brand, match.start(), match.end()
        ):
            continue
        found.append(BrandHit(brand, match.group(0), match.start(), match.end()))
    return found


def brands_in(text: str) -> list[Brand]:
    """The brands ``text`` names, each once, in the order it names them."""
    return [hit.brand for hit in brand_hits(text)]


# A brand's own corporate words (and the words of a legal form, a place, an
# office) — what a name may hold beside the brand and still be the brand.
_OFFICE_WORDS: Final = frozenset(
    {
        "TECHNOLOGY",
        "TECHNOLOGIES",
        "TECH",
        "DIGITAL",
        "POWER",
        "ENERGY",
        "ELECTRIC",
        "ELECTRICAL",
        "ELECTRONICS",
        "APPLIANCES",
        "LIGHTING",
        "SOLAR",
        "GROUP",
        "HOLDING",
        "INTERNATIONAL",
        "GLOBAL",
        "CORPORATION",
        "CORP",
        "COMPANY",
        "CO",
        "LTD",
        "LIMITED",
        "INC",
        "LLC",
        "OOO",
        "ООО",
        "MCHJ",
        "МЧЖ",
        "XK",
        "ХК",
        "JV",
        "СП",
        "AO",
        "АО",
        "UZBEKISTAN",
        "UZB",
        "UZ",
        "OZBEKISTON",
        "УЗБЕКИСТАН",
        "ЎЗБЕКИСТОН",
        "CENTRAL",
        "ASIA",
        "REPRESENTATIVE",
        "OFFICE",
        "BRANCH",
        "ПРЕДСТАВИТЕЛЬСТВО",
        "VAKOLATXONASI",
        "ВАКОЛАТХОНАСИ",
        "FILIALI",
        "ФИЛИАЛ",
        "OF",
        "IN",
        "THE",
        "AND",
    }
)
_NAME_WORD: Final = re.compile(r"[^\W_]+(?:['ʻʼ’`][^\W_]+)*")
_APOSTROPHES: Final = re.compile(r"['ʻʼ’`]")


def own_office(name: str) -> Brand | None:
    """The brand whose own office ``name`` is, or None: one brand, and every
    other word of the name a corporate word, a legal form or a place
    (``_OFFICE_WORDS``)."""
    hits = brand_hits(name)
    if len(hits) != 1:
        return None
    hit = hits[0]
    rest = f"{name[: hit.start]} {name[hit.end :]}"
    words = [_APOSTROPHES.sub("", w).upper() for w in _NAME_WORD.findall(rest)]
    return hit.brand if all(w in _OFFICE_WORDS for w in words) else None
