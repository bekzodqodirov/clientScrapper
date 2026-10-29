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
* TG_RE's link is read in any case and on telegram.me / telegram.dog too
  («T.me/namuna_optom» opens a sentence on a phone keyboard);
* PINFL_RE (a 14-digit run) is new: the normaliser, the pseudonymiser and
  the output hygiene ask the one pattern;
* LEGAL_FORM_RE gained МЧЖ, ХЧК and СП, the Cyrillic forms of MChJ, XChK, SP;
* QUESTION_RE is new, and so are ``buyer_question`` and ``cargo_offer``: the
  tout filter's text rule (03 §2.3) reads a message for the cargo core only
  when it is not a buyer's ask — «kim kargo qiladi?» is a buyer (04 §6);
* TONNES_RE, VACANCY_IMPORT_RE, NON_CN_ORIGIN_RE and STATE_BODY_RE are not
  03 §1.4's: they are 04 §5.1's detections of ``vol_stated_*`` (tonnes),
  ``vol_vacancy_import``, ``neg_non_cn_origin`` and ``neg_state_body``, kept
  here because this is where every regex lives (the rule signals of
  ``services/signals.py`` and the gates read them); each gained the Uzbek
  Cyrillic spellings of its Latin words and the endings people write.

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
# An origin other than China (04 §5.1 ``neg_non_cn_origin``: «Rossiya|Россия|
# Turkiya|Турция|Koreya|Корея|Yevropa|Европа|Eron|Иран»), plus the Uzbek
# Cyrillic «Туркия», «Эрон» and the genitive «Ирана», which a four-letter word
# does not grow by itself (INFLECT_MIN / RU_INFLECT_MIN below). Not 03 §1.4's.
# These are the country WORDS (NON_CN_COUNTRY_RE, the quote of a model's
# ``origin_cn='other_origin'``); the rule itself reads only a country named as
# the origin (NON_CN_ORIGIN_RE below).
NON_CN_ORIGIN_WORDS = (
    "Rossiya",
    "Россия",
    "Turkiya",
    "Туркия",
    "Турция",
    "Koreya",
    "Корея",
    "Yevropa",
    "Европа",
    "Eron",
    "Эрон",
    "Иран",
    "Ирана",
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
# «фуру», «партии»): 03 §1.4's bare words missed the inflected forms. A half
# truck is written in all three scripts — «yarim fura», «ярим фура», «полфуры»
# or «пол фуры» — and is matched whole, so its «fura» is never read as a full
# one (04 §5.3 rule 1 counts it as ≈ 40 m³). «full truck» and «FTL» are 04
# §5.1's container/truck words (not in 03 §1.4).
CONTAINER_RE = re.compile(
    rf"(?<!\w)(?:(?:konteyner|kontayner|yarim\s*fura|fura|tonna|partiya){_UZ_LATN}"
    rf"|(?:контейнер|ярим\s*фура|фура|тонна|партия){_UZ_CYRL}"
    rf"|(?:контейнер|фур|тонн){_RU_NOUN}|парти(?:ями|ях|ям|ей|я|и|ю|й)"
    rf"|пол\s*-?\s*фур(?:ы|у|ой|е)"
    r"|full\s*truck(?:load)?s?|FTL"
    rf"|20\s*(?:ft|fut|фут|{_A})|40\s*(?:ft|fut|фут|{_A}))(?!\w)",
    re.I,
)
# A weight next to a tonne word (04 §5.1 ``vol_stated_3plus``: «(\d+)\s*(tonna|
# тонн)»; 1 t of mixed goods ≈ 3 m³ at the low end, 04 §4.3). Group 1 = the
# number, a decimal allowed («1,5 tonna»); the unit takes its endings
# («tonnadan», «тоннали», «3 тонны», «10 тонн»). A tonne word with no number
# is CONTAINER_RE's (and counts as nothing there, 04 §5.3 rule 2).
TONNES_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[.,]\d{1,2})?)\s*"
    rf"(?:tonna{_UNIT_LATN}|тонна{_UNIT_CYRL}|тонн{_RU_NOUN})"
    rf"{_NO_LETTER}",
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
# A question (the tout filter's «ask-shaped» half, 03 §2.3 / 04 §6): a
# question mark, or an interrogative a buyer opens with when he leaves it out
# — «kim kargo qiladi», «qaysi kargo yaxshi», «кто доставит», «посоветуйте
# карго». Not 03 §1.4's: a buyer who names the service he looks for writes
# its competitor word in a question, and the filter must not read that as an
# offer.
QUESTION_RE = re.compile(
    r"\?|(?<!\w)(?:kim|qaysi|qancha|qanday|qanaqa|qayer\w*|necha\w*|nechpul|"
    r"ким|[қк]айси|[қк]анча|[қк]андай|[қк]аер\w*|неча\w*|"
    r"кто|как(?:ой|ая|ие|ую|ого|ом)|сколько|где|посоветуйте|подскажите)(?!\w)",
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
# Gate 2 on a self-description (04 §6; T-19 review): what a forwarder says of
# itself, without the words an importer also writes about its own trade.
# COMPETITOR_RE stays 03's — the tout filter's on a sender, the collectors' —
# but on a wholesaler's own listing its delivery words read «Доставка по
# Ташкенту бесплатно», «Bepul dostavka», «Toshkent boʻylab yetkazib beramiz»,
# «своя логистика и склад»: every importer delivers, and gate 2's exclusion
# never reopens. So the gate reads the cargo core: kargo/карго (with -xona and
# the endings), ekspeditor, broker, vikup, rastamojka as a service (not «…
# rastamojka qilingan / oʻtgan», «растаможенный»: goods cleared, an importer's
# selling point), bojxona xizmati/brokeri, таможенное оформление,
# грузоперевозки, yuk tashish, yuk yetkazib berish, «доставка грузов | из
# Китая» and «Xitoydan dostavka» (03 §2.6's OLX words «карго, выкуп, доставка
# из Китая, 1688 cargo»; 04 §4.3's tout «dostavka Xitoydan»), 1688 kargo, and
# logistics offered as a service. «dostavka», «yetkazib beramiz», «express»
# and «logistika» alone are no exclusion.
_CARGO_CORE = (
    rf"(?:avia\s*)?(?:kargo|cargo){_KARGO_LATN}|karga|"
    rf"(?:авиа\s*)?карго{_KARGO_CYRL}|"
    rf"(?:ekspeditor|broker|vikup){_UZ_LATN}|"
    rf"rastamojka{_UZ_LATN}(?!\w)(?!\s+(?:qilin\w*|o{_A}?t(?:gan|kaz)\w*))|"
    r"экспедитор\w*|экспедиров\w*|брокер\w*|растаможк\w*|растаможива\w*|"
    r"выкуп(?:ом|а|у)?|"
    r"yuk\s+tashi(?:sh|ymiz|sh\s+xizmati)|юк\s+таши(?:ш|ймиз|ш\s+хизмати)|"
    r"yuk(?:lar)?(?:ingiz(?:ni)?|ni)?\s+yetkazib\s+ber(?:amiz|ish\w*)|"
    r"юк(?:лар)?(?:ингиз(?:ни)?|ни)?\s+етказиб\s+бер(?:амиз|иш\w*)|"
    r"доставк\w*\s+(?:груз\w*|из\s+Кита[яе])|"
    r"(?:dostavka\s+Xitoydan|Xitoydan\s+dostavka|доставка\s+Хитойдан|"
    r"Хитойдан\s+доставка)|"
    r"божхона\s+(?:хизмат|брокер)\w*|bojxona\s+(?:xizmat|broker)\w*|"
    r"грузоперевозк\w*|таможенн(?:ое|ый)\s+оформлен\w*|"
    r"1688\s*(?:kargo|cargo|карго|zakupka)|"
    r"logistika\s+(?:xizmat|kompaniya)\w*|логистика\s+(?:хизмат|компания)\w*|"
    r"логистическ\w+\s+(?:услуг|компани|сервис)\w*|"
    r"logistics?\s+(?:services?|company)"
)
COMPETITOR_SELF_RE = re.compile(rf"(?<!\w)(?:{_CARGO_CORE})(?!\w)", re.I)
# Gate 2 on a NAME: the core, and the logistics words — a firm named for
# logistics is a forwarder («Namuna Logistika MChJ», «Namuna Logistics»). Not
# «Express» (a furniture shop is «Mebel Express»; the cargo channels named
# «… Express» are seeds, gate 2 reads them as ``chat_monitors`` competitors).
COMPETITOR_NAME_RE = re.compile(
    rf"(?<!\w)(?:{_CARGO_CORE}|logistika{_UZ_LATN}|logistik|logistics?|"
    rf"логистика{_UZ_CYRL}|логистик(?:ами|ах|ам|ой|а|е|и|у)?|логистическ\w*)(?!\w)",
    re.I,
)
# The same said by a Telegram username glued into one word («halolcargo»,
# «chinacargouz», «mkexpress»), which COMPETITOR_RE's word bounds cannot see:
# a username is Latin letters, digits and «_» only, so these are its Latin
# stems, looked for inside the name (``competitor_handle``).
COMPETITOR_HANDLE_STEMS = (
    "cargo",
    "kargo",
    "express",
    "logist",
    "ekspeditor",
    "dostavka",
    "broker",
    "rastamoj",
    "vikup",
)


def competitor_handle(username: str | None) -> bool:
    """A username that names a forwarder inside itself (COMPETITOR_HANDLE_STEMS)."""
    name = (username or "").lower()
    return any(stem in name for stem in COMPETITOR_HANDLE_STEMS)


# B2C / personal use.
B2C_RE = re.compile(
    rf"(?<!\w)(?:o{_A}?zim\s+uchun|ўзим\s+учун|для\s+себя|1\s*(?:dona|шт\.?|штук[аи]?)|"
    r"bitta\s+(?:dona|narsa)|битта|shaxsiy|шахсий|лично\s+для|taobao\s*zakaz|"
    r"zakaz\s+qilib\s+ber|в\s+розницу|доставка\s+по\s+городу)(?!\w)",
    re.I,
)
# An import duty in a vacancy (04 §5.1 ``vol_vacancy_import``: «ВЭД|деклар|
# закуп\w*\s.{0,20}Кита|tashqi savdo|xitoy tili|китайск\w+ язык»). The Uzbek
# Cyrillic spellings of the Latin words and the Latin «VED» join; «деклар» and
# the phrases keep their endings («деклараций», «xitoy tilini»). Read on a
# vacancy's text only (04 §9.3): a vacancy's other duties are not volume.
VACANCY_IMPORT_RE = re.compile(
    r"(?<!\w)(?:ВЭД|VED(?!\w)|деклар\w*|закуп\w*\s.{0,20}?Кита[йяею]|"
    r"tashqi\s+savdo|ташқи\s+савдо|xitoy\s+til\w*|хитой\s+тил\w*|"
    r"китайск\w+\s+язык\w*)",
    re.I,
)
# A state body (04 §5.1 ``neg_state_body``: «davlat|ГУП|ДУК|министер|vazirlig|
# hokimiyat|hokimligi|agentlig|maktab|shifoxona|поликлиник|МВД|IIB»), the
# stems with their endings («hokimligi», «Министерство», «поликлиника») and the
# Uzbek Cyrillic spellings (with ҳ and with х). Gate 7 reads it on the name; a
# word in the text only is the −20 negative, never an exclusion (04 §6). Two
# readings are not a state body: «davlat roʻyxatidan oʻtkazilgan» (state
# registration — every registry page says it of a private firm) and a state
# body named as the customer («maktablar uchun», «shifoxonalar uchun»: for
# schools, for hospitals — a supplier, not the body).
_NOT_STATE_BODY = (
    rf"(?!\s+(?:ro{_A}?y?xat\w*|рўйхат\w*|руйхат\w*)|\s+(?:uchun|учун)(?!\w))"
)
STATE_BODY_RE = re.compile(
    r"(?<!\w)(?:davlat\w*|давлат\w*|ГУП|ДУК|министер\w*|vazirlig\w*|вазирлиг\w*|"
    r"hokimiyat\w*|hokimlig\w*|[ҳх]окимият\w*|[ҳх]окимлиг\w*|agentlig\w*|"
    r"агентлиг\w*|maktab\w*|мактаб\w*|shifoxona\w*|шифохона\w*|поликлиник\w*|"
    rf"МВД|IIB|ИИБ)(?!\w){_NOT_STATE_BODY}",
    re.I,
)
# Gate 7 on a NAME (04 §6; T-19 review): a state body by its shape, not by
# STATE_BODY_RE's stems — «Davlat», «Davlatbek», «Davlatov» are given names
# and surnames firms carry («Davlatbek Mebel», «IP Davlatov»), and school and
# hospital equipment traders are classic China importers («Maktab Mebel
# Import», «Shifoxona Jihozlari Savdo»). A state body's name: an office's own
# word (hokimligi/hokimiyati, vazirligi, министерство, МВД, IIB/ИИБ, ГУП/ДУК
# and the Latin DUK, поликлиника); «Davlat … qoʻmitasi | inspeksiyasi |
# xizmati | korxonasi | muassasasi | boshqarmasi | agentligi» — the
# institution in the possessive, so «Davlat Xizmat Servis» is a person's firm (and
# «государственн… комитет | инспекция | предприятие | учреждение | служба |
# агентство»); a numbered school («5-maktab», «12-son maktab», «школа № 5»);
# a hospital in the possessive or numbered («… shifoxonasi», «3-son
# shifoxona»). A word that is no name of a body stays the text negative.
_WORD = rf"(?:\w|{_A})+"
STATE_BODY_NAME_RE = re.compile(
    r"(?<!\w)(?:ГУП|ДУК|DUK|МВД|IIB|ИИБ|министер\w*|vazirlig\w*|вазирлиг\w*|"
    r"hokimiyat\w*|hokimlig\w*|[ҳх]окимият\w*|[ҳх]окимлиг\w*|поликлиник\w*|"
    r"poliklinika\w*|"
    rf"davlat\s+(?:{_WORD}\s+){{0,4}}?(?:qo{_A}?mitasi|inspeksiyasi|xizmat(?:i|lari)|"
    r"korxonasi|muassasasi|boshqarmasi|agentligi|unitar)\w*|"
    r"давлат\s+(?:\w+\s+){0,4}?(?:[қк][ўу]митаси|инспекцияси|[ҳх]измат(?:и|лари)|"
    r"корхонаси|муассасаси|бош[қк]армаси|агентлиги|унитар)\w*|"
    r"государственн\w+\s+(?:\w+\s+){0,3}?(?:комитет|инспекц|предприят|учрежден|"
    r"служб|агентств)\w*|"
    rf"\d+\s*(?:-\s*|\s+(?=son))(?:son(?:li)?\s+)?(?:{_WORD}\s+){{0,2}}?"
    r"(?:maktab|shifoxona)\w*|"
    r"\d+\s*(?:-\s*|\s+(?=сон))(?:сон(?:ли)?\s+)?(?:\w+\s+){0,2}?(?:мактаб|шифохона)\w*|"
    r"школ\w*\s*№\s*\d+|shifoxonasi\w*|шифохонаси\w*)(?!\w)"
    rf"{_NOT_STATE_BODY}",
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


def buyer_question(text: str) -> bool:
    """A buyer's ask (03 §2.3, 04 §6: «kim kargo qiladi?» is a buyer): buyer
    intent (``INTENT_RE``) or a question form (``QUESTION_RE``), and no price
    figure (``PRICE_RE``) — a forwarder states its tariff («1 kub 250$, 3 kub
    kerakmi?»), a buyer asks for one («narxi qancha?»). A volume alone is no
    ask: a tout's offer states one too («1 kub 250$»)."""
    asks = INTENT_RE.search(text) is not None or QUESTION_RE.search(text) is not None
    return asks and PRICE_RE.search(text) is None


def cargo_offer(text: str, *, may_ask: bool = True) -> bool:
    """A text that offers the cargo service — read as 04 §6 reads a
    self-description: ``COMPETITOR_SELF_RE`` (the cargo core: «Kargo
    Xitoy-Toshkent», «Доставка из Китая», never «Доставка по Ташкенту»), in a
    text that is not a buyer's ask (``buyer_question``). ``may_ask=False``:
    the speaker never asks (a chat posting as itself), so its words are its
    offer. The tout filter's text rule (03 §2.3) and the model's tout check
    (04 §4.7) are this one rule."""
    if may_ask and buyer_question(text):
        return False
    return COMPETITOR_SELF_RE.search(text) is not None


# The normaliser validates operator codes (services/keys.phone_e164 / phone_kind).
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?998[\s\-]?)?\(?(\d{2})\)?[\s\-]?(\d{3})[\s\-]?(\d{2})[\s\-]?(\d{2})(?!\d)"
)
# Names ending in "bot" and reserved t.me paths are rejected by
# services/normalise.telegram_key, not here. The link is matched in any case —
# a phone keyboard writes «T.me/…» at a sentence start, and a pasted link may
# be «TELEGRAM.ME/…» — and on Telegram's two other link hosts, telegram.me and
# telegram.dog; the pseudonymiser masks with this pattern (04 §4.2), so a
# spelling it does not read reaches the model.
TG_RE = re.compile(
    r"(?:(?i:(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(?!joinchat|\+|s/))"
    r"|(?<![\w@])@)([A-Za-z][A-Za-z0-9_]{4,31})"
)
# A PINFL (the personal identification number of a passport) is 14 digits: a
# maximal run of exactly 14, so a 17-digit 2GIS firm id or Graph user id holds
# none (02 §2.5). Never stored, never sent (06 §5.2): the normaliser drops a
# field carrying one, the pseudonymiser masks it as ``<ID>`` and the output
# hygiene of 04 §4.7 drops a model field that carries one.
PINFL_RE = re.compile(r"(?<!\d)\d{14}(?!\d)")
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
# The personal-data shapes beyond PHONE_RE, TG_RE, IG_RE and EMAIL_RE (06
# §5.2, CLAUDE.md: phones and handles never reach a model; 05 §B.1: a note
# carries none beyond the body's). One home for the pseudonymiser, its
# ``assert_masked``, the push note's quote filter and the scrubber (E2E review
# 2026-10-29: each kept its own copy, and the pseudonymiser had none).
#
# A number with a country code other than Uzbekistan's (+7, +86, +996 …):
# «+», 9 to 15 digits, a separator or two between them; +998 is PHONE_RE's.
# ``intl_phones`` cuts a match back to its numbering plan.
INTL_PHONE_RE = re.compile(r"(?<![\w+])\+(?!998)\d(?:[\s\-.()]{0,2}\d){8,14}(?!\d)")
# A foreign or trunk number written without «+» («8 916 000 00 77», «0086 138
# …», «79161234567»): ten digits or more in one run, a space, hyphen or
# bracket (or two) between them — no dot, so a date beside a volume («25.11.2026 10
# kub») is no number. ``long_numbers`` cuts it back the same way.
LONG_NUMBER_RE = re.compile(r"(?<![\d+])\d(?:[\s\-()]{0,2}\d){9,}(?!\d)")
# A messenger id after its label: «WeChat: rustam_cn88», «viber rustam_88»,
# «Telegram: namuna_tekstil», «tg namuna_tekstil» — a colon or dash after the
# label, or, after a space, an id with a digit or «_» («whatsapp orqali» is a
# sentence, «tg kanal» a word). ``label`` names the messenger, ``id`` the
# handle (``TELEGRAM_LABELS``: a Telegram username written without «@»).
_MESSENGER_LABELS = (
    r"we\s?chat|weixin|微信|vx|wx|whats\s?app|ватсап|вотсап|viber|вайбер|skype|скайп|"
    r"telegramm?|телеграмм?|tg|тг|line|qq|imo"
)
# A messenger's label alone, ``fullmatch``ed on a word (a display name's).
MESSENGER_LABEL_RE = re.compile(rf"(?:{_MESSENGER_LABELS})(?:\s*(?:id|ид))?", re.I)
MESSENGER_ID_RE = re.compile(
    rf"(?<!\w)(?P<label>{_MESSENGER_LABELS})(?:\s*(?:id|ид))?"
    r"(?:\s*[:：\-–—]\s*@?|\s+@?(?=[\w.\-]*[\d_]))(?P<id>[\w.+\-]{3,})",
    re.I,
)
TELEGRAM_LABELS = frozenset(
    {"telegram", "telegramm", "телеграм", "телеграмм", "tg", "тг"}
)
# The longest number of a numbering plan, in digits with its country code:
# China's +86 1xx xxxx xxxx is 13; +7 (Kazakhstan, Russia) and +1 are 11. A
# trunk number without «+» is at most 12 («8 701 …», «0 532 …»), one written
# with «00» for «+» 15.
INTL_DIGITS_MAX = 13
INTL_PLAN_DIGITS = {"7": 11, "1": 11}
LONG_DIGITS_MAX = 12
LONG_ZERO_ZERO_MAX = 15
_PHONE_GROUP = re.compile(r"[\s\-.()]+\(?(\d+)\)?\Z")
_PHONE_DIGITS_MIN = 9


def _phone_span(text: str, start: int, end: int, cap: int) -> tuple[int, int]:
    """``text[start:end]`` cut back, a whole digit group at a time, until it
    holds at most ``cap`` digits and does not end in a one-digit group — a
    number never does, the volume after it may («+7 701 000 00 00 30 kub»)."""
    body = text[start:end]
    while True:
        last = _PHONE_GROUP.search(body)
        digits = sum(c.isdigit() for c in body)
        if last is None or not (digits > cap or len(last.group(1)) == 1):
            break
        cut = body[: last.start()]
        if sum(c.isdigit() for c in cut) < _PHONE_DIGITS_MIN:
            break
        body = cut
    return start, start + len(body)


def intl_phones(text: str) -> list[tuple[int, int]]:
    """The spans of the foreign numbers ``INTL_PHONE_RE`` finds in ``text``,
    each cut back to its numbering plan."""
    spans = []
    for match in INTL_PHONE_RE.finditer(text):
        cap = INTL_PLAN_DIGITS.get(match.group(0)[1], INTL_DIGITS_MAX)
        spans.append(_phone_span(text, match.start(), match.end(), cap))
    return spans


def long_numbers(text: str) -> list[tuple[int, int]]:
    """The spans of the digit runs ``LONG_NUMBER_RE`` finds in ``text``, each
    cut back to the longest trunk number. A run with no separator is one
    number, kept whole — except a 14-digit one (``PINFL_RE``'s: masked as
    ``<ID>`` first, never a reversible token) and one longer than any number
    (a 17-digit 2GIS firm id is no phone) — and the tail of a foreign number
    (``intl_phones``' own)."""
    spans = []
    foreign = intl_phones(text)
    for match in LONG_NUMBER_RE.finditer(text):
        if any(s < match.end() and match.start() < e for s, e in foreign):
            continue
        run = match.group(0)
        if run.isdigit() and (PINFL_RE.fullmatch(run) or len(run) > LONG_ZERO_ZERO_MAX):
            continue
        cap = LONG_ZERO_ZERO_MAX if run.startswith("00") else LONG_DIGITS_MAX
        start, end = _phone_span(text, match.start(), match.end(), cap)
        if sum(c.isdigit() for c in text[start:end]) >= 10:
            spans.append((start, end))
    return spans


STIR_RE = re.compile(r"(?<!\w)(?:STIR|СТИР|ИНН|INN|TIN)\s*[:№#]?\s*(\d{9})(?!\d)", re.I)
LEGAL_FORM_RE = re.compile(
    r"(?<!\w)(MChJ|MCHJ|МЧЖ|OOO|ООО|LLC|XK|ХК|QK|ҚК|AJ|АЖ|AO|АО|ЧП|XChK|ХЧК|YaTT|ЯТТ|ИП|SP|СП|"
    rf"ТОО|JV|QO{_A}?SHMA)(?!\w)",
    re.I,
)
# A Chinese-side supplier's name (04 §6 gate 3: «Co\.?,?\s*Ltd|有限公司|Trading
# Co\.|Imp(ort)?\s*&\s*Exp(ort)?» without a UZ legal form or address): the
# English company endings of a Chinese exporter's card, and 有限公司 («limited
# company»). Read on a name only; «& Exp.» is the shortened form exhibitor
# lists print; «Co» and «Ltd» need a separator («Coltd» is a word). «Co.,
# Limited» and «Company Limited» are the same ending written out (T-19
# review: common on Chinese exporters' cards).
SUPPLIER_NAME_RE = re.compile(
    r"(?<!\w)(?:Co[.,\s][.,\s]*(?:Ltd\.?|Limited)|Company\s+Limited|Trading\s+Co\.|"
    r"Imp(?:ort)?\.?\s*&\s*Exp(?:ort)?\.?)(?!\w)"
    r"|有限公司",
    re.I,
)
# China as an exhibitor table prints it (04 §6 gate 3 «exhibitor country CN»;
# 03 §2.8's tables are in the show's languages), upper-cased.
CN_COUNTRY_NAMES = frozenset(
    {
        "CN",
        "CHN",
        "PRC",
        "CHINA",
        "CHINA (MAINLAND)",
        "XITOY",
        "ХИТОЙ",
        "КИТАЙ",
        "КНР",
        "中国",
    }
)
# A CJK ideograph (04 §6 gate 3: «website title in Chinese»): one is enough —
# an Uzbek importer's site is never titled in Chinese characters.
CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
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
NON_CN_COUNTRY_RE = terms_re(NON_CN_ORIGIN_WORDS)
# 04 §5.1 «named as origin»: a country is an origin only in an origin form —
# the Uzbek ablative («Turkiyadan», «Кореядан»), «X ishlab chiqarish / X
# mahsuloti» (made in X), the Russian «из …», «производства …» /
# «производство: …». A destination («Rossiyaga ham yetkazib beramiz», «по
# России») and a mention («работа с Россией») are not an origin.
_ORIGIN_LATN = "(?:Rossiya|Turkiya|Koreya|Yevropa|Eron)"
_ORIGIN_CYRL = "(?:Россия|Туркия|Корея|Европа|Эрон)"
_ORIGIN_RU_GEN = r"(?:Южной\s+)?(?:России|Турции|Кореи|Европы|Ирана)"
_ORIGIN_RU_NOM = r"(?:Южная\s+)?(?:Россия|Турция|Корея|Европа|Иран)"
NON_CN_ORIGIN_RE = re.compile(
    rf"(?<!\w)(?:{_ORIGIN_LATN}dan{_UZ_LATN}"
    rf"|{_ORIGIN_LATN}(?:da)?\s+(?:ishlab\s+chiqar\w*|mahsulot\w*)"
    rf"|{_ORIGIN_CYRL}дан{_UZ_CYRL}"
    rf"|{_ORIGIN_CYRL}(?:да)?\s+(?:ишлаб\s+чи[қк]ар\w*|ма[ҳх]сулот\w*)"
    rf"|(?:из|производств[аоу]?|производител[ья])\s+{_ORIGIN_RU_GEN}"
    rf"|производств[оа]\s*[:\-–—]\s*{_ORIGIN_RU_NOM})(?!\w)",
    re.I,
)
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
