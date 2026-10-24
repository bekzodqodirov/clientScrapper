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
* «оптовые цен» (SUPPLY_RE) and «bojxona xizmat» (COMPETITOR_RE) take their
  word ending — cut before ``(?!\\w)`` they matched no text anyone writes;
* EMAIL_RE is new (06 §5.2 puts it beside the others);
* IG_RE's label branch («instagram:», «insta @…», «ig:») needs a word start
  and a separator, else «big shop» read as the handle «shop»;
* STIR_RE's label needs a word start («Martin 301234567» is not a STIR);
* LEGAL_FORM_RE gained МЧЖ, ХЧК and СП, the Cyrillic forms of MChJ, XChK, SP.

The word lists are matched through ``terms_re``: whole words, case-blind,
apostrophe-blind, and inflection-aware — «Guangzhoudan» is Guangzhou + -dan,
«в Китае» is Китай in the prepositional case (04 §9.1 and §9.3 quote both as
China evidence) — while «оптика» stays out of «опт».

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
# GEO by the canonical Latin city it places a company in (04 §1.6: «Ташкент» →
# «Toshkent»); the Tashkent bazaars and districts are Toshkent.
# tests/collectors/test_keywords.py holds this partition equal to GEO.
GEO_CITY: dict[str, tuple[str, ...]] = {
    "Toshkent": (
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
    ),
    "Andijon": ("Andijon", "Андижан"),
    "Samarqand": ("Samarqand", "Самарканд"),
    "Fargʻona": ("Farg'ona", "Фергана"),
    "Namangan": ("Namangan", "Наманган"),
    "Buxoro": ("Buxoro", "Бухара"),
    "Nukus": ("Nukus", "Нукус"),
}
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

# Volume: a number next to a cubic-metre word. Group 1 = the number.
VOLUME_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[.,]\d)?)\s*"
    r"(?:kub|куб(?:\.?\s*м)?|m3|м3|m³|м³|kubometr|кубометр)"
    rf"(?![a-zа-яё]|{_A})",
    re.I,
)
# Container / truck / batch words (no number needed).
CONTAINER_RE = re.compile(
    r"(?<!\w)(?:konteyner|kontayner|контейнер|fura|фура|yarim\s*fura|полфуры|"
    rf"20\s*(?:ft|fut|фут|{_A})|40\s*(?:ft|fut|фут|{_A})|"
    r"tonna|тонн[аы]?|partiya|партия|партиями)(?!\w)",
    re.I,
)
# Buyer intent (groups): Latin, Cyrillic Uzbek, Russian. «олиб кел(иш|ади|…)»
# mirrors the Latin endings (03 §1.4 has only the bare «олиб кел», so «ким олиб
# келади?» — the commonest Cyrillic ask — never matched).
INTENT_RE = re.compile(
    rf"(?<!\w)(?:kim\s+olib\s+kel|olib\s+kel(?:ish|adi|asiz|ib)|kerak|kere|kimda\s+bor|"
    rf"kim\s+sotadi|ta{_A}?minotchi|"
    r"ким\s+олиб\s+кел|олиб\s+кел(?:иш|ади|асиз|иб)?|керак|кимда\s+бор|таъминотчи|"
    r"ищу|нужен|нужна|нужно|требуется|кто\s+(?:возит|доставит|привез[её]т)|поставщик|"
    r"закуп(?:аю|ить|ка))(?!\w)",
    re.I,
)
# Supply-side posts by a wholesaler (channel posts): arrival of stock.
# The second line is the Uzbek Cyrillic of the first (not in 03 §1.4); «оптовые
# цен\w*» takes its ending (03 §1.4's «цен» before (?!\w) matched no real text).
SUPPLY_RE = re.compile(
    r"(?<!\w)(?:konteyner\s+keldi|yangi\s+(?:tovar|partiya|konteyner)|yuk\s+keldi|"
    r"контейнер\s+келди|янги\s+(?:товар|партия|контейнер)|юк\s+келди|"
    r"фура\s+пришла|контейнер\s+(?:пришёл|пришел)|"
    r"новое\s+поступление|новая\s+партия|zavod\s+narxida|завод\s+нархида|optom\s+narx|"
    r"оптом\s+нарх|оптов(?:ые|ая)\s+цен\w*)(?!\w)",
    re.I,
)
# Competitors / touts (forwarders, buying agents, brokers) — exclusion, their
# channels are seeds. The third line is the Uzbek Cyrillic of the Latin
# alternatives (not in 03 §1.4); «bojxona xizmat\w*» takes its ending, as
# «Bojxona xizmati» is how it is written.
COMPETITOR_RE = re.compile(
    r"(?<!\w)(?:kargo|karg[oa]|cargo|карго|logistik[a]?|логистик[аи]|ekspeditor|экспедитор|"
    r"dostavka|доставка|yetkazib\s+beramiz|yuk\s+tashi(?:sh|ymiz|sh\s+xizmati)|"
    r"етказиб\s+берамиз|юк\s+таши(?:ш|ймиз|ш\s+хизмати)|божхона\s+(?:хизмат|брокер)\w*|"
    r"грузоперевозк\w*|broker|брокер|rastamojka|растамож\w*|bojxona\s+(?:xizmat|broker)\w*|"
    r"таможенн(?:ое|ый)\s+оформлен\w*|vikup|выкуп|avia\s*kargo|авиа\s*карго|express|"
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
# The normaliser validates operator codes (services/keys.phone_e164 / phone_kind).
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?998[\s\-]?)?\(?(\d{2})\)?[\s\-]?(\d{3})[\s\-]?(\d{2})[\s\-]?(\d{2})(?!\d)"
)
# Names ending in "bot" and reserved t.me paths are rejected by
# services/normalise.telegram_key, not here.
TG_RE = re.compile(
    r"(?:(?:https?://)?t\.me/(?!joinchat|\+|s/)|(?<![\w@])@)([A-Za-z][A-Za-z0-9_]{4,31})"
)
IG_RE = re.compile(
    r"(?:instagram\.com/|(?<![\w.@])(?:инстаграм|instagram|insta|ig)(?:\s*[:\-]\s*@?|\s*@))"
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

# Uzbek case/plural/possessive endings a list word may carry, up to two in a
# row: «Xitoydan», «dilerlarga», «optomchilar», «skladimizdan». The Cyrillic set
# is the same endings spelled by services/text.transliterate.
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
# letter before the ending is added («Китай» → «Кита-» + «е»).
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
RU_STEM_DROP = frozenset("аяйьоеыи")
# A word shorter than this is matched only as written: «опт» must not grow into
# «оптика», nor «Иу» into anything.
INFLECT_MIN = 4
# Russian endings need one letter more: the Uzbek «мато» would otherwise stem to
# «мат-» and read «мать» as a textile word.
RU_INFLECT_MIN = 5

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def _literal(word: str) -> str:
    # re.escape leaves "'" alone (Python ≥ 3.7); widen it to every variant.
    return re.escape(word).replace("'", _A)


def _alternation(options: Iterable[str]) -> str:
    return "|".join(sorted(set(options), key=len, reverse=True))


def _word_pattern(word: str, *, inflect: bool) -> str:
    if not inflect or len(word) < INFLECT_MIN:
        return _literal(word)
    if not _CYRILLIC.search(word):
        return (
            f"{_literal(word)}(?:{_alternation(UZ_SUFFIXES_LATN)}){{0,{UZ_SUFFIX_CHAIN}}}"
        )
    uz_cyrl = f"(?:{_alternation(UZ_SUFFIXES_CYRL)}){{0,{UZ_SUFFIX_CHAIN}}}"
    forms = [f"{_literal(word)}{uz_cyrl}"]
    if len(word) >= RU_INFLECT_MIN:
        stem = word[:-1] if word[-1].lower() in RU_STEM_DROP else word
        forms.append(f"{_literal(stem)}(?:{_alternation(RU_ENDINGS)})")
    return "(?:" + "|".join(forms) + ")"


def _term_pattern(term: str, *, inflect: bool) -> str:
    # Only the last word of a phrase inflects: «из Китая», «оптовые цены».
    *head, last = term.split()
    parts = [_literal(w) for w in head] + [_word_pattern(last, inflect=inflect)]
    return r"\s+".join(parts)


def terms_re(terms: Iterable[str], *, inflect: bool = True) -> re.Pattern[str]:
    """Any of ``terms`` as a whole word or phrase, any case, any apostrophe,
    with an Uzbek or Russian ending on its last word when ``inflect``."""
    ordered = sorted(set(terms), key=len, reverse=True)
    body = "|".join(_term_pattern(t, inflect=inflect) for t in ordered)
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.I)


B2B_RE = terms_re(chain.from_iterable(B2B_MARKERS.values()))
CHINA_RE = terms_re(CHINA_WORDS)
GEO_RE = terms_re(GEO)
CITY_RES: dict[str, re.Pattern[str]] = {city: terms_re(t) for city, t in GEO_CITY.items()}
BULKY_RES: dict[str, re.Pattern[str]] = {
    category: terms_re(t) for category, t in BULKY_CATEGORIES.items()
}


def bulky_categories(text: str) -> frozenset[str]:
    """The BULKY_CATEGORIES keys whose terms occur in ``text``."""
    return frozenset(c for c, pattern in BULKY_RES.items() if pattern.search(text))
