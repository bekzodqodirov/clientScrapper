"""Shared keywords and regexes (03 §1.4) — the one home.

Every collector imports from here and keeps no copy of its own; so does the
pseudonymiser (``services/pseudonyms.py`` imports ``PHONE_RE``, ``TG_RE``,
``IG_RE`` and ``EMAIL_RE``, 04 §4.2 / 06 §5.2). The word lists are 03 §1.4
verbatim. The regexes are too, except where 03's own acceptance (≥ 3 positive
cases per regex in Latin Uzbek, Cyrillic Uzbek and Russian) or a plain defect
forced a change — each is named in a comment beside it:

* every literal apostrophe accepts all the variants ``services/text.py``
  folds (ʻ ʼ ‘ ’ ` ʹ '), because these patterns run on text as written;
* SUPPLY_RE and COMPETITOR_RE gained the Uzbek Cyrillic spellings of their
  Uzbek Latin alternatives, and INTENT_RE the Cyrillic endings of «olib kel…»;
* the words of VOLUME_RE, CONTAINER_RE, SUPPLY_RE, INTENT_RE and COMPETITOR_RE
  take their Uzbek suffixes and Russian endings («20 кубов», «5 kubdan»,
  «фуру», «konteynerda», «Yangi tovarlar keldi», «Ищем», «kargoxonasi»,
  «логистикой») — cut before ``(?!\\w)``, 03's bare words missed the forms
  people write (04 §9.3 itself reads «контейнеров» with CONTAINER_RE);
* EMAIL_RE is new (06 §5.2 puts it beside the others);
* IG_RE's label branch («instagram:», «insta @…», «Instagram namuna_shop»)
  needs a word start and a separator, else «big shop» read as the handle
  «shop» and «Instagramda» as «da»;
* STIR_RE's label needs a word start («Martin 301234567» is not a STIR);
* LEGAL_FORM_RE gained МЧЖ, ХЧК and СП, the Cyrillic forms of MChJ, XChK, SP.

The word lists are matched through ``terms_re``: whole words, case-blind,
apostrophe-blind, е/ё-blind, and inflection-aware — «Guangzhoudan» is
Guangzhou + -dan, «в Китае» is Китай in the prepositional case (04 §9.1 and
§9.3 quote both as China evidence), «импортер» is «импортёр» — while «оптика»
stays out of «опт».

Python's ``re`` is Unicode-aware for ``str`` patterns, so ``(?<!\\w)`` works on
Cyrillic; the JS ``\\b`` trap does not apply here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from itertools import chain

from clientscrapper.services.text import APOSTROPHE_CLASS, transliterate

_A = APOSTROPHE_CLASS

B2B_MARKERS: dict[str, tuple[str, ...]] = {
    "uz-Latn": (
        "optom",
        "ulgurji",
        "ulgurji narx",
        "optom baza",
        "zavod narxida",
        "zavoddan",
        "ishlab chiqaruvchidan",
        "dilerlik",
        "distribyutor",
        "diler",
        "yetkazib beruvchi",
        "do'konlar uchun",
        "minimal buyurtma",
        "sklad",
    ),
    "uz-Cyrl": (
        "оптом",
        "улгуржи",
        "улгуржи нарх",
        "заводдан",
        "дилер",
        "дистрибьютор",
        "дўконлар учун",
        "склад",
    ),
    "ru": (
        "оптом",
        "опт",
        "мелкий опт",
        "крупный опт",
        "оптовая база",
        "оптовые цены",
        "от производителя",
        "с фабрики",
        "прямые поставки",
        "дилер",
        "дистрибьютор",
        "поставщик",
        "импортёр",
        "официальный импортер",
        "для магазинов",
        "склад",
        "мин. заказ",
        "MOQ",
    ),
}
VOLUME_WORDS = (
    "kub",
    "куб",
    "m3",
    "m³",
    "м3",
    "м³",
    "konteyner",
    "kontayner",
    "контейнер",
    "20 fut",
    "40 fut",
    "20ft",
    "40ft",
    "fura",
    "фура",
    "tonna",
    "тонна",
    "partiya",
    "партия",
    "konteyner keldi",
    "yuk keldi",
    "фура пришла",
    "новое поступление",
)
CHINA_WORDS = (
    "Xitoydan",
    "Xitoy",
    "Хитойдан",
    "Хитой",
    "из Китая",
    "Китай",
    "КНР",
    "China",
    "1688",
    "Taobao",
    "Alibaba",
    "Pinduoduo",
    "Yiwu",
    "Iwu",
    "Иу",
    "Guangzhou",
    "Guanchjou",
    "Гуанчжоу",
    "Urumchi",
    "Урумчи",
    "Xorgos",
    "Хоргос",
    "Qashqar",
    "Кашгар",
    "Shenzhen",
)
# A channel's about names its companion «xaridorlar/buyers» group (03 §2.3) the
# way it names its admin: «Xaridorlar guruhi: @…» beside «Murojaat uchun: @…».
# Only a handle these words introduce is a companion group; any other is
# usually a person's account and is not queued (06 §5.1). Not 03 §1.4's.
COMPANION_WORDS = (
    "xaridor",
    "haridor",
    "guruh",
    "gurux",
    "chat",
    "muhokama",
    "харидор",
    "гуруҳ",
    "гурух",
    "муҳокама",
    "покупатель",
    "группа",
    "чат",
    "чата",
    "чате",
    "обсуждение",
    "buyer",
    "buyers",
    "group",
    "groups",
    "discussion",
)
# The same said by a handle's own name («@namuna_xaridorlar», «@optom_chat»).
COMPANION_HANDLE_STEMS = (
    "xaridor",
    "haridor",
    "buyer",
    "guruh",
    "gurux",
    "group",
    "chat",
)
GEO = (
    "Toshkent",
    "Тошкент",
    "Ташкент",
    "Tashkent",
    "Abu Saxiy",
    "Abu Sahiy",
    "Абу Сахий",
    "Bek Baraka",
    "Chorsu",
    "Чорсу",
    "Urikzor",
    "Урикзор",
    "Yangiobod",
    "Янгиобод",
    "Malika",
    "Малика",
    "Qo'yliq",
    "Куйлюк",
    "Sergeli",
    "Сергели",
    "Chinni bozor",
    "Andijon",
    "Андижан",
    "Samarqand",
    "Самарканд",
    "Farg'ona",
    "Фергана",
    "Namangan",
    "Наманган",
    "Buxoro",
    "Бухара",
    "Nukus",
    "Нукус",
)
# GEO by the canonical Latin city it names (04 §1.6: «Ташкент» → «Toshkent»),
# and the Tashkent bazaars and districts apart: a place is Toshkent only when
# no city is named, so «Chorsu bozori Samarqand» and «Malika Karimova,
# Samarqand» are Samarqand. tests/collectors/test_keywords.py holds the two
# equal to GEO.
GEO_CITY: dict[str, tuple[str, ...]] = {
    "Toshkent": ("Toshkent", "Тошкент", "Ташкент", "Tashkent"),
    "Andijon": ("Andijon", "Андижан"),
    "Samarqand": ("Samarqand", "Самарканд"),
    "Fargʻona": ("Farg'ona", "Фергана"),
    "Namangan": ("Namangan", "Наманган"),
    "Buxoro": ("Buxoro", "Бухара"),
    "Nukus": ("Nukus", "Нукус"),
}
GEO_TASHKENT_PLACES = (
    "Abu Saxiy",
    "Abu Sahiy",
    "Абу Сахий",
    "Bek Baraka",
    "Chorsu",
    "Чорсу",
    "Urikzor",
    "Урикзор",
    "Yangiobod",
    "Янгиобод",
    "Malika",
    "Малика",
    "Qo'yliq",
    "Куйлюк",
    "Sergeli",
    "Сергели",
    "Chinni bozor",
)
BULKY_CATEGORIES: dict[str, tuple[str, ...]] = {
    # canonical key → terms in the three scripts (also the SERP <category> axis, 17 keys)
    "textile": ("mato", "gazlama", "мато", "газлама", "ткани", "текстиль"),
    "footwear": ("poyabzal", "пойабзал", "обувь"),
    "apparel": ("kiyim", "trikotaj", "кийим", "одежда", "трикотаж"),
    "household": (
        "uy-ro'zg'or",
        "idish-tovoq",
        "xoztovar",
        "идиш-товоқ",
        "хозтовары",
        "посуда",
    ),
    "auto_parts": (
        "avto ehtiyot qismlar",
        "zapchast",
        "shina",
        "авто эҳтиёт қисмлар",
        "автозапчасти",
        "шины",
    ),
    "building": (
        "qurilish mollari",
        "santexnika",
        "kafel",
        "қурилиш моллари",
        "стройматериалы",
        "сантехника",
        "плитка",
    ),
    "furniture": ("mebel", "mebel furnitura", "мебель", "мебельная фурнитура"),
    "toys": ("o'yinchoq", "ўйинчоқ", "игрушки"),
    "cosmetics": ("kosmetika", "parfyumeriya", "косметика", "бытовая химия"),
    "electronics": (
        "telefon aksessuar",
        "elektronika",
        "maishiy texnika",
        "электроника",
        "бытовая техника",
        "аксессуары для телефонов",
    ),
    "lighting": (
        "yoritgich",
        "LED",
        "elektr mollari",
        "ёритгич",
        "светильники",
        "электротовары",
    ),
    "equipment": (
        "uskuna",
        "dastgoh",
        "stanok",
        "ускуна",
        "дастгоҳ",
        "оборудование",
        "станки",
        "линия",
    ),
    "raw": ("xomashyo", "granula", "plastik", "хомашё", "сырьё", "гранулы", "полимеры"),
    "packaging": (
        "qadoqlash",
        "paket",
        "karobka",
        "қадоқлаш",
        "упаковка",
        "пакеты",
        "гофрокороб",
    ),
    "stationery": ("kanselyariya", "канцелярия", "канцтовары"),
    "bags": ("sumka", "chamadon", "сумка", "сумки", "чемоданы"),
    "sport": ("sport", "trenajyor", "спорт", "тренажер"),
}

# --- Inflection tables ----------------------------------------------------------

# Uzbek case/plural/possessive endings a word may carry, up to two in a row:
# «Xitoydan», «dilerlarga», «optomchilar», «skladimizdan». The Cyrillic set is
# the same endings spelled by services/text.transliterate.
UZ_SUFFIXES_LATN = (
    "larining",
    "laridan",
    "lariga",
    "larini",
    "larida",
    "lardan",
    "larga",
    "larni",
    "larda",
    "lari",
    "lar",
    "ning",
    "dagi",
    "imiz",
    "ingiz",
    "dan",
    "miz",
    "ga",
    "ka",
    "qa",
    "da",
    "ni",
    "si",
    "chi",
    "cha",
    "lik",
    "i",
)
UZ_SUFFIXES_CYRL = tuple(transliterate(s) for s in UZ_SUFFIXES_LATN)
UZ_SUFFIX_CHAIN = 2
# Russian noun endings on the stem of a Cyrillic word: «Китае», «дилеров»,
# «сантехники», «оборудования». A word ending in one of RU_STEM_DROP loses that
# letter before the ending is added («Китай» → «Кита-» + «е», «сырьё» →
# «сырь-» + «я»).
RU_ENDINGS = (
    "ами",
    "ями",
    "ой",
    "ей",
    "ом",
    "ем",
    "ам",
    "ям",
    "ах",
    "ях",
    "ов",
    "ев",
    "ью",
    "а",
    "я",
    "у",
    "ю",
    "ы",
    "и",
    "е",
    "о",
    "ь",
)
RU_STEM_DROP = frozenset("аяйьоеёыи")
# The endings of a measure word: «10 kubli», «5 kubdan ortiq», «20 kubga». Not
# the diminutive «cha» or a bare «k», so «kubcha», «kubik» and «kubok» (a cube,
# a toy cube, a cup) stay out.
UNIT_SUFFIXES_LATN = (
    "gacha",
    "dagi",
    "ning",
    "lik",
    "lar",
    "dan",
    "ga",
    "da",
    "li",
    "ni",
    "si",
    "i",
)
UNIT_SUFFIXES_CYRL = tuple(transliterate(s) for s in UNIT_SUFFIXES_LATN)
# A trade noun's Russian case endings: «кубов», «контейнера», «фуру», «тонн».
RU_NOUN_ENDINGS = ("ами", "ах", "ам", "ов", "ой", "ом", "а", "у", "ы", "е")

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_E_OR_YO = re.compile("[ЕЁеё]")


def _alternation(options: Iterable[str]) -> str:
    return "|".join(sorted(set(options), key=len, reverse=True))


def _literal(word: str) -> str:
    # re.escape leaves "'" alone (Python ≥ 3.7); widen it to every variant. A
    # Russian е and ё are one letter as people type them («импортер» for
    # «импортёр», «сырье» for «сырьё»); case is the pattern's re.I.
    return _E_OR_YO.sub("[её]", re.escape(word).replace("'", _A))


def _chain(suffixes: Iterable[str]) -> str:
    """Up to UZ_SUFFIX_CHAIN of ``suffixes`` in a row, or none."""
    return f"(?:{_alternation(map(_literal, suffixes))}){{0,{UZ_SUFFIX_CHAIN}}}"


def _ending(endings: Iterable[str]) -> str:
    """One of ``endings``, or none."""
    return f"(?:{_alternation(map(_literal, endings))})?"


_UZ_LATN = _chain(UZ_SUFFIXES_LATN)
_UZ_CYRL = _chain(UZ_SUFFIXES_CYRL)
_UNIT_LATN = _chain(UNIT_SUFFIXES_LATN)
_UNIT_CYRL = _chain(UNIT_SUFFIXES_CYRL)
_RU_NOUN = _ending(RU_NOUN_ENDINGS)
# «kargoxona» / «каргохона» is a cargo office: kargo + -xona, then the usual
# endings («kargoxonasi»).
_KARGO_LATN = _chain((*UZ_SUFFIXES_LATN, "xona"))
_KARGO_CYRL = _chain((*UZ_SUFFIXES_CYRL, "хона"))
# Not followed by a letter of any script, nor by an apostrophe (oʻ, gʻ).
_NO_LETTER = rf"(?![^\W\d_]|{_A})"


# --- Regexes ----------------------------------------------------------------------

# Volume: a number next to a cubic-metre word. Group 1 = the number. The unit
# takes its ending — «20 кубов», «30 кубометров», «3 куба», «5 kubdan ortiq»,
# «10 kubli yuk» are how volumes are written; 03 §1.4's bare unit before a
# no-letter lookahead read none of them. «кубик» (a toy cube, a cc) is not a
# unit here: «5 кубик ўйинчоқ» must not score as five cubic metres.
VOLUME_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[.,]\d)?)\s*"
    r"(?:куб\.?\s*м"
    rf"|(?:kubometr|kub|m3|m³){_UNIT_LATN}"
    rf"|(?:кубометр|куб|м3|м³)(?:{_alternation(map(_literal, UNIT_SUFFIXES_CYRL))}"
    rf"|{_alternation(map(_literal, RU_NOUN_ENDINGS))}){{0,{UZ_SUFFIX_CHAIN}}})"
    rf"{_NO_LETTER}",
    re.I,
)
# Container / truck / batch words (no number needed), with their Uzbek suffixes
# («konteynerda», «furaga», «партияси») and Russian case endings («контейнера»,
# «фуру», «партии»): 03 §1.4's bare words missed the inflected forms.
CONTAINER_RE = re.compile(
    rf"(?<!\w)(?:(?:konteyner|kontayner|yarim\s*fura|fura|tonna|partiya){_UZ_LATN}"
    rf"|(?:контейнер|фура|тонна|партия){_UZ_CYRL}"
    rf"|(?:контейнер|фур|тонн){_RU_NOUN}|парти(?:ями|ях|ям|ей|я|и|ю|й)|полфуры"
    rf"|20\s*(?:ft|fut|фут|{_A})|40\s*(?:ft|fut|фут|{_A}))(?!\w)",
    re.I,
)
# Buyer intent (groups): Latin, Cyrillic Uzbek, Russian. «олиб кел(иш|ади|…)»
# mirrors the Latin endings (03 §1.4 has only the bare «олиб кел», so «ким олиб
# келади?» — the commonest Cyrillic ask — never matched). The question forms
# «kerakmi?», «ищем», «нужны» and the plurals of «поставщик» / «закуп…» join
# the 03 §1.4 words; «keraksiz», «kerakmas» (not needed) stay out.
INTENT_RE = re.compile(
    rf"(?<!\w)(?:kim\s+olib\s+kel|olib\s+kel(?:ish|adi|asiz|ib)|kerak(?:mi|dir)?|kere|"
    rf"kimda\s+bor|kim\s+sotadi|ta{_A}?minotchi(?:lar)?|"
    r"ким\s+олиб\s+кел|олиб\s+кел(?:иш|ади|асиз|иб)?|керак(?:ми|дир)?|кимда\s+бор|"
    r"таъминотчи(?:лар)?|"
    r"ищу|ищем|нужен|нужна|нужно|нужны|требуется|требуются|"
    r"кто\s+(?:возит|доставит|привез[её]т)|поставщик(?:ами|ах|ам|ов|ом|а|и|у)?|"
    r"закуп(?:аю|аем|ает|ают|ить|ками|кой|ка|ки|ку|ок))(?!\w)",
    re.I,
)
# Supply-side posts by a wholesaler (channel posts): arrival of stock. The
# Uzbek Cyrillic line spells the Latin one (not in 03 §1.4); plurals and endings
# are 03's words as written («Yangi tovarlar keldi», «Konteynerlar keldi»,
# «Новые поступления», «optom narxlarda»); «оптовые цен\w*» takes its ending.
SUPPLY_RE = re.compile(
    rf"(?<!\w)(?:konteyner(?:lar)?\s+keldi|yangi\s+(?:tovar|partiya|konteyner){_UZ_LATN}|"
    rf"(?:yuk|tovar)(?:lar)?\s+keldi|zavod\s+narx(?:lar)?ida|optom\s+narx{_UZ_LATN}|"
    rf"контейнер(?:лар)?\s+келди|янги\s+(?:товар|партия|контейнер){_UZ_CYRL}|"
    rf"(?:юк|товар)(?:лар)?\s+келди|завод\s+нарх(?:лар)?ида|оптом\s+нарх{_UZ_CYRL}|"
    r"фура\s+пришла|фуры\s+пришли|контейнер\s+приш[её]л|контейнеры\s+пришли|"
    r"нов(?:ое|ые|ых)\s+поступлени[еяй]|нов(?:ая|ые|ую)\s+парти[яюи]|"
    r"оптов(?:ые|ая)\s+цен\w*)(?!\w)",
    re.I,
)
# Competitors / touts (forwarders, buying agents, brokers) — exclusion, their
# channels are seeds. The Uzbek words take their suffixes («kargoxonasi»,
# «kargomiz», «kargoga»), the Russian ones their endings («логистикой»,
# «экспедиторская»), and the Uzbek Cyrillic spellings of the Latin alternatives
# join (not in 03 §1.4); «bojxona xizmat\w*» takes its ending, as «Bojxona
# xizmati» is how it is written.
COMPETITOR_RE = re.compile(
    rf"(?<!\w)(?:(?:avia\s*)?(?:kargo|cargo){_KARGO_LATN}|karga|"
    rf"(?:авиа\s*)?карго{_KARGO_CYRL}|"
    rf"logistika{_UZ_LATN}|logistik|logistics?|логистика{_UZ_CYRL}|"
    r"логистик(?:ами|ах|ам|ой|а|е|и|у)|логистическ\w*|"
    rf"(?:ekspeditor|broker|rastamojka|vikup){_UZ_LATN}|экспедитор\w*|экспедиров\w*|"
    r"брокер\w*|растамож\w*|выкуп(?:ом|а|у)?|"
    r"dostavka|доставка|yetkazib\s+beramiz|yuk\s+tashi(?:sh|ymiz|sh\s+xizmati)|"
    r"етказиб\s+берамиз|юк\s+таши(?:ш|ймиз|ш\s+хизмати)|божхона\s+(?:хизмат|брокер)\w*|"
    r"грузоперевозк\w*|bojxona\s+(?:xizmat|broker)\w*|"
    r"таможенн(?:ое|ый)\s+оформлен\w*|express|"
    r"экспресс|1688\s*(?:kargo|cargo|карго|zakupka))(?!\w)",
    re.I,
)
# B2C / personal use.
B2C_RE = re.compile(
    rf"(?<!\w)(?:o{_A}?zim\s+uchun|ўзим\s+учун|для\s+себя|1\s*(?:dona|шт\.?|штук[аи]?)|"
    r"bitta\s+(?:dona|narsa)|битта|shaxsiy|шахсий|лично\s+для|taobao\s*zakaz|"
    r"zakaz\s+qilib\s+ber|в\s+розницу|доставка\s+по\s+городу)(?!\w)",
    re.I,
)
# A price in a message (03 §2.3 tout filter): 03's ``\d{2,}\s*(?:so'm|сум|\$|usd|
# y\.e)`` with every apostrophe (and none: «25000 som»), the Uzbek Cyrillic
# «сўм» and the Cyrillic «у.е.» beside the Latin «y.e.», the Uzbek unit endings
# on so'm/сўм («25000 so'mdan», «120 сўмдан» — how a price list is written),
# and no letter after the unit («сумка» is a bag, not a price). A price list is
# PRICELIST_MIN_PRICES of them, or a message longer than PRICELIST_MIN_CHARS
# (03 §2.3); the reader's prefilter stores the first shape and T-14's tout rule
# counts both — one home for the two (userbot/ and collectors/ may not import
# each other).
PRICE_RE = re.compile(
    rf"\d{{2,}}\s*(?:so{_A}?m{_UNIT_LATN}|сўм{_UNIT_CYRL}|сум|\$|usd|y\.e\.?|у\.е\.?)"
    r"(?![^\W\d_])",
    re.I,
)
PRICELIST_MIN_PRICES = 3
PRICELIST_MIN_CHARS = 1500


def price_count(text: str) -> int:
    """How many PRICE_RE prices ``text`` names."""
    return sum(1 for _ in PRICE_RE.finditer(text))


def pricelist_shaped(text: str) -> bool:
    """03 §2.3's price-list shape, which the tout rule counts per sender:
    PRICELIST_MIN_PRICES prices or more, or longer than PRICELIST_MIN_CHARS."""
    return price_count(text) >= PRICELIST_MIN_PRICES or len(text) > PRICELIST_MIN_CHARS


# The normaliser validates operator codes (services/keys.phone_e164 / phone_kind).
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?998[\s\-]?)?\(?(\d{2})\)?[\s\-]?(\d{3})[\s\-]?(\d{2})[\s\-]?(\d{2})(?!\d)"
)
# Names ending in "bot" and reserved t.me paths are rejected by
# services/normalise.telegram_key, not here.
TG_RE = re.compile(
    r"(?:(?:https?://)?t\.me/(?!joinchat|\+|s/)|(?<![\w@])@)([A-Za-z][A-Za-z0-9_]{4,31})"
)
# The label branch needs a word start («big shop» is not the handle «shop») and
# a separator: a colon or dash, an «@», or at least one space — «Instagram
# namuna_shop» is a handle and the pseudonymiser must mask it (04 §4.2), while
# «Instagramda» is a word. The space-only form also catches ordinary words
# («Instagram sahifamiz»); masking one is harmless, and
# services/normalise.instagram_handles keeps only handle-shaped ones of it.
IG_RE = re.compile(
    r"(?:instagram\.com/|(?<![\w.@])(?:инстаграм|instagram|insta|ig)"
    r"(?:\s*[:\-–—]\s*@?|\s*@|\s+))"
    r"([A-Za-z0-9._]{2,30})(?![\w.])",
    re.I,
)
EMAIL_RE = re.compile(
    r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
)
STIR_RE = re.compile(r"(?<!\w)(?:STIR|СТИР|ИНН|INN|TIN)\s*[:№#]?\s*(\d{9})(?!\d)", re.I)
LEGAL_FORM_RE = re.compile(
    r"(?<!\w)(MChJ|MCHJ|МЧЖ|OOO|ООО|LLC|XK|ХК|QK|ҚК|AJ|АЖ|AO|АО|ЧП|XChK|ХЧК|YaTT|ЯТТ|ИП|SP|СП|"
    rf"ТОО|JV|QO{_A}?SHMA)(?!\w)",
    re.I,
)
CAPTCHA_MARKERS = (
    "captcha",
    "anomaly.js",
    "Are you a robot",
    "Доступ ограничен",
    "cf-chl",
)


# --- Word lists as patterns ---------------------------------------------------

# A word shorter than this is matched only as written: «опт» must not grow into
# «оптика», nor «Иу» into anything.
INFLECT_MIN = 4
# Russian endings need one letter more: the Uzbek «мато» would otherwise stem to
# «мат-» and read «мать» as a textile word.
RU_INFLECT_MIN = 5


def _word_pattern(word: str, *, inflect: bool) -> str:
    if not inflect or len(word) < INFLECT_MIN:
        return _literal(word)
    if not _CYRILLIC.search(word):
        return f"{_literal(word)}{_UZ_LATN}"
    forms = [f"{_literal(word)}{_UZ_CYRL}"]
    if len(word) >= RU_INFLECT_MIN:
        stem = word[:-1] if word[-1].lower() in RU_STEM_DROP else word
        forms.append(f"{_literal(stem)}(?:{_alternation(map(_literal, RU_ENDINGS))})")
    return "(?:" + "|".join(forms) + ")"


def _term_pattern(term: str, *, inflect: bool) -> str:
    # Only the last word of a phrase inflects: «из Китая», «оптовые цены».
    *head, last = term.split()
    parts = [_literal(w) for w in head] + [_word_pattern(last, inflect=inflect)]
    return r"\s+".join(parts)


def terms_re(terms: Iterable[str], *, inflect: bool = True) -> re.Pattern[str]:
    """Any of ``terms`` as a whole word or phrase, any case, any apostrophe, е
    or ё alike, with an Uzbek or Russian ending on its last word when ``inflect``."""
    ordered = sorted(set(terms), key=len, reverse=True)
    body = "|".join(_term_pattern(t, inflect=inflect) for t in ordered)
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.I)


B2B_RE = terms_re(chain.from_iterable(B2B_MARKERS.values()))
CHINA_RE = terms_re(CHINA_WORDS)
COMPANION_RE = terms_re(COMPANION_WORDS)
GEO_RE = terms_re(GEO)
CITY_RES: dict[str, re.Pattern[str]] = {city: terms_re(t) for city, t in GEO_CITY.items()}
TASHKENT_PLACES_RE = terms_re(GEO_TASHKENT_PLACES)
BULKY_RES: dict[str, re.Pattern[str]] = {
    category: terms_re(t) for category, t in BULKY_CATEGORIES.items()
}


def bulky_categories(text: str) -> frozenset[str]:
    """The BULKY_CATEGORIES keys whose terms occur in ``text``."""
    return frozenset(c for c, pattern in BULKY_RES.items() if pattern.search(text))
