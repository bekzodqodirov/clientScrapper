"""The playbook's first messages, as the code fills them (04 §4.5). Pure.

``docs/sotuv-playbook.md`` §3.1 writes one first message per segment A–E for
a manager to send. The playbook's own text carries what a first message
written by a machine may not (§2.5 and 04 §4.5's redlines): day counts
(«15–25 kun»), the commission («3%»), digits («1688», «20/40 fut»,
«PKM-352»), tariff and price words («narx zichlik boʻyicha», «tarif»),
customs and declaration words («GTD», «TN VED»). The templates here are the
same messages with those taken out — the facts kept are the playbook's
(receiving points in Yiwu and Guangzhou, measurement and photo report,
consolidation, containers, the qualifier question) — and they end with the
opt-out sentence (``redlines.OPTOUT``). They are spelled with ʻ/ʼ (04 Q9 a)
and use the playbook's placeholders for the people: ``[Men]`` the manager,
``[Ism]`` the contact (never a stored name: the manager reads it on the card).

``tests/fixtures/uz_corpus.PLAYBOOK_TEMPLATES`` holds the reference copy
and ``tests/test_redlines.py`` pins the two equal and every template,
filled, through ``redlines.check_opener``.

``fill`` substitutes the source, the goods and the greeting; a value that
would trip the gate alone (``redlines.trips``: a goods word «soat», a digit)
is left out, so a filled template passes the same gate by construction —
``opener`` checks it once more and falls back to the generic values. With
no goods left an Uzbek template says «tovar» and a Russian one drops the
words around them (``RU_GOODS``: «ваш опт.», not «по товару «товары»»); a
Cyrillic goods word only Russian writes («холодильники») stays out of an
Uzbek text rather than standing there transliterated.

Every template ends with its qualifier question and the opt-out sentence:
the two sentences a shortened opener keeps (``crm_push.fit_opener``, the
ERP note's «NIMA DEYISH»).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from clientscrapper.services import redlines
from clientscrapper.services.redlines import Lang
from clientscrapper.services.text import to_latin
from clientscrapper.services.validators import uz_apostrophes

SEGMENTS: Final = ("A", "B", "C", "D", "E")
# A segment without its own template (X competitor never reaches a card; Z
# unclear, none yet): the bazaar wholesaler's, the commonest.
DEFAULT_SEGMENT: Final = "A"
GOODS_MAX: Final = 3

_OPTOUT_UZ: Final = redlines.OPTOUT_UZ
_OPTOUT_RU: Final = redlines.OPTOUT_RU

# {salom}: «, [Ism]» or nothing; {manba}: where the company was seen;
# {tovar}: what it trades.
TEMPLATES: Final[Mapping[tuple[str, Lang], str]] = {
    ("A", "uz"): (
        "Assalomu alaykum{salom}. Men [Men], GSR Logistics (Toshkent), 2018-yildan "
        "Xitoydan yuk olib kelamiz. {manba}da {tovar} optom savdongizni koʻrib "
        "yozyapman. Yukni Yiwu va Guangzhoudagi oʻz punktlarimizda qabul qilib "
        "oʻlchaymiz va fotohisobot yuboramiz. Oyiga taxminan necha kub olasiz? "
        + _OPTOUT_UZ
    ),
    ("B", "uz"): (
        "Assalomu alaykum{salom}. Men [Men], GSR Logistics. {manba}dagi "
        "doʻkoningizni koʻrdim — {tovar} Xitoydan keladi shekilli. Xitoydagi "
        "saytlardan xarid qilib beramiz, tovarni Xitoyda tekshirib foto yuboramiz, "
        "hujjatlarni sertifikat va buxgalteriyaga mos qilib tayyorlaymiz. Keyingi "
        "partiya qachon va taxminan necha kub yoki kilogramm? " + _OPTOUT_UZ
    ),
    ("C", "uz"): (
        "Assalomu alaykum{salom}. Men [Men], GSR Logistics (Toshkent). {manba}da "
        "{tovar} boʻyicha korxonangizni koʻrdim. Xitoydan uskuna va xomashyoni "
        "konteynerda olib kelamiz, zavodda yukni tekshirib foto beramiz, "
        "hujjatlarni oʻzimiz tayyorlaymiz. Yaqin oylarda rejalashtirilgan xarid "
        "bormi? " + _OPTOUT_UZ
    ),
    ("D", "uz"): (
        "Assalomu alaykum{salom}. Men [Men], GSR Logistics. {manba}da {tovar} "
        "boʻyicha optom savdongizni koʻrdim. Ogʻir yuk uchun toʻliq fura yoki "
        "konteyner hisobini yonma-yon koʻrsatamiz, Yiwu va Guangzhouda qabul qilib "
        "foto yuboramiz. Mavsumda Xorgosda navbat boʻladi — joyni oldindan band "
        "qilamiz. Odatda toʻliq furami yoki yigʻma? " + _OPTOUT_UZ
    ),
    ("E", "uz"): (
        "Assalomu alaykum{salom}. Men [Men], GSR Logistics. {manba}da {tovar} "
        "boʻyicha faoliyatingizni koʻrdim. Guangzhouda oʻz qabul punktimiz bor: "
        "har bir joyni sanab, nozik tovarni tekshirib foto yuboramiz, hujjatlarni "
        "oʻzimiz rasmiylashtiramiz. Avtomobil va elektromobil importi ham bor. "
        "Oyiga taxminan necha kub yoki kilogramm olasiz? " + _OPTOUT_UZ
    ),
    ("A", "ru"): (
        "Здравствуйте{salom}. Я [Men], GSR Logistics (Ташкент), с 2018 года возим "
        "грузы из Китая. Пишу, потому что увидел {manba} ваш опт{tovar}. "
        "Принимаем груз на своих пунктах в Иу и Гуанчжоу, замеряем и "
        "присылаем фотоотчёт. Сколько кубов в месяц обычно везёте? " + _OPTOUT_RU
    ),
    ("B", "ru"): (
        "Здравствуйте{salom}. Я [Men], GSR Logistics. Увидел ваш магазин {manba} — "
        "похоже, {tovar} идёт из Китая. Закупаем товар на китайских площадках, "
        "проверяем его в Китае и присылаем фото, документы готовим так, чтобы они "
        "подходили для сертификации и бухгалтерии. Когда следующая партия и "
        "сколько примерно кубов или килограммов? " + _OPTOUT_RU
    ),
    ("C", "ru"): (
        "Здравствуйте{salom}. Я [Men], GSR Logistics (Ташкент). Увидел {manba} "
        "ваше предприятие{tovar}. Везём оборудование и сырьё из Китая в "
        "контейнерах, принимаем груз на заводе с фотоотчётом, документы оформляем "
        "сами. Есть ли закупка в ближайшие месяцы? " + _OPTOUT_RU
    ),
    ("D", "ru"): (
        "Здравствуйте{salom}. Я [Men], GSR Logistics. Увидел {manba} ваш "
        "опт{tovar}. Для тяжёлого груза считаем рядом два варианта — полная "
        "фура и контейнер, приёмка и фото в Иу и Гуанчжоу. В сезон на Хоргосе "
        "очереди — бронируем место заранее. Вы обычно везёте полной фурой или "
        "сборным? " + _OPTOUT_RU
    ),
    ("E", "ru"): (
        "Здравствуйте{salom}. Я [Men], GSR Logistics. Увидел {manba} вашу "
        "работу{tovar}. У нас свой пункт приёма в Гуанчжоу: пересчитываем "
        "места, проверяем хрупкое и присылаем фото, документы делаем сами. Есть и "
        "импорт авто и электромобилей. Сколько кубов или килограммов в месяц "
        "обычно везёте? " + _OPTOUT_RU
    ),
}

# Where the company was seen, by the source family of the cited evidence: the
# Uzbek word takes «-da» / «-dagi» in the template, the Russian one carries
# its preposition.
SOURCE_UZ: Final[Mapping[str, str]] = {
    "telegram": "Telegram guruh",
    "olx": "OLX",
    "instagram": "Instagram",
    "directory": "katalog",
    "vacancy": "vakansiyalar sayti",
    "exhibitor": "koʻrgazma",
    "maps": "xarita",
    "brand": "dilerlar roʻyxati",
    "marketplace": "Uzum",
    "search": "internet",
    "registry": "reestr",
}
SOURCE_RU: Final[Mapping[str, str]] = {
    # Not «в Telegram-группе»: a messenger's label before a word is a
    # messenger id to services/pseudonyms («Telegram-группе» would be masked).
    "telegram": "в группе Telegram",
    "olx": "на OLX",
    "instagram": "в Instagram",
    "directory": "в каталоге",
    "vacancy": "на сайте вакансий",
    "exhibitor": "в списке участников выставки",
    "maps": "на карте",
    "brand": "в списке дилеров",
    "marketplace": "на Uzum",
    "search": "в интернете",
    "registry": "в реестре",
}
DEFAULT_SOURCE: Final = "search"
# The goods when none can be named (Uzbek: the word «tovar» in their place).
NO_GOODS_UZ: Final = "tovar"
# How a Russian template carries the goods, by segment: the words around
# them, and what stands when none can be named («ваш опт.», never «ваш опт по
# товару «товары»»).
RU_GOODS: Final[Mapping[str, tuple[str, str]]] = {
    "A": (" по товару «{goods}»", ""),
    "B": ("товар «{goods}»", "товар"),
    "C": (": «{goods}»", ""),
    "D": (" по товару «{goods}»", ""),
    "E": (" по товару «{goods}»", ""),
}
# A Cyrillic goods word that only Russian writes — a letter Uzbek has not
# («ы», «щ») or a Russian plural or adjective ending («холодильники»,
# «стиральные») — stays out of an Uzbek text, where it would stand
# transliterated («xolodilniki»); «мато», «мебель», «кунгабоқар ёғи» are
# Uzbek and are written in Latin.
_RU_ONLY_LETTERS: Final = frozenset("ыщЫЩ")
_RU_ENDING: Final = re.compile(
    r"(?:ы|и|ые|ие|ая|яя|ой|ый|ий|ое|ее|ую|юю|ов|ев|ей)$", re.IGNORECASE
)
GREETING: Final = ", [Ism]"
# The playbook's placeholders, as a skeleton shows them to the model.
SKELETON_FIELDS: Final[Mapping[Lang, Mapping[str, str]]] = {
    "uz": {"salom": GREETING, "manba": "[manba]", "tovar": "[tovar]"},
    "ru": {"salom": GREETING, "manba": "[источник]", "tovar": "[товар]"},
}


def _tovar(segment: str, lang: Lang, words: Sequence[str]) -> str:
    """The goods as segment ``segment``'s template in ``lang`` writes them."""
    if lang == "uz":
        return ", ".join(words) if words else NO_GOODS_UZ
    with_goods, without = RU_GOODS[segment]
    return with_goods.format(goods=", ".join(words)) if words else without


def template_segment(segment: str | None) -> str:
    """The template a segment uses (``DEFAULT_SEGMENT`` for X, Z or none)."""
    return segment if segment in SEGMENTS else DEFAULT_SEGMENT


def lang_of(text: str | None) -> Lang:
    """Russian when the evidence is written in Russian (04 §4.5), else Uzbek."""
    return "ru" if redlines.script_of(text or "") == "russian" else "uz"


def skeleton(segment: str | None, lang: Lang) -> str:
    """The segment's template with the playbook's placeholders — what the
    draft request shows the model (04 §4.5)."""
    seg = template_segment(segment)
    fields = SKELETON_FIELDS[lang]
    return TEMPLATES[(seg, lang)].format(
        salom=fields["salom"],
        manba=fields["manba"],
        tovar=_tovar(seg, lang, [fields["tovar"]]),
    )


def source_word(family: str | None, lang: Lang) -> str:
    table = SOURCE_UZ if lang == "uz" else SOURCE_RU
    return table.get(family or "", table[DEFAULT_SOURCE])


def goods_words(goods: Sequence[str], lang: Lang) -> list[str]:
    """At most ``GOODS_MAX`` goods a template may carry: in Latin for an
    Uzbek text (Cyrillic goods transliterated, apostrophes ʻ/ʼ), none that
    would trip the gate alone."""
    kept: list[str] = []
    for raw in goods:
        item = " ".join(str(raw).split())
        if not item:
            continue
        if lang == "uz":
            if _russian(item):
                continue
            item = uz_apostrophes(to_latin(item))
        if redlines.trips(item) or item in kept:
            continue
        kept.append(item)
        if len(kept) == GOODS_MAX:
            break
    return kept


def _russian(item: str) -> bool:
    """A goods word only Russian writes (``_RU_ENDING``)."""
    if redlines.script_of(item) != "russian":
        return False
    return any(ch in _RU_ONLY_LETTERS for ch in item) or any(
        _RU_ENDING.search(word) for word in item.split()
    )


def hooks(goods: Sequence[str], family: str | None) -> list[str]:
    """What an opener may hook on (04 §4.5 ``hook_missing``): the goods and
    the source's name in both languages."""
    return [
        *goods,
        source_word(family, "uz"),
        source_word(family, "ru"),
        *([family] if family else []),
    ]


@dataclass(frozen=True, slots=True)
class Opener:
    """A first message and how it was made."""

    text: str
    lang: Lang
    segment: str
    flags: tuple[redlines.Flag, ...] = ()


def fill(
    segment: str | None,
    lang: Lang,
    *,
    goods: Sequence[str],
    family: str | None,
    person: bool,
) -> str:
    """The segment's template filled by string substitution."""
    seg = template_segment(segment)
    return TEMPLATES[(seg, lang)].format(
        salom=GREETING if person else "",
        manba=source_word(family, lang),
        tovar=_tovar(seg, lang, goods_words(goods, lang)),
    )


def opener(
    segment: str | None,
    lang: Lang,
    *,
    goods: Sequence[str],
    family: str | None,
    person: bool,
) -> Opener:
    """The template opener: ``fill``, held to ``redlines.check_opener``; a
    fill the gate refuses (it cannot, by construction — the check is the
    proof) is the template with the generic goods and source."""
    seg = template_segment(segment)
    text = fill(seg, lang, goods=goods, family=family, person=person)
    guard = redlines.check_opener(text, lang=lang, hooks=hooks(goods, family))
    if not guard.ok:
        text = fill(seg, lang, goods=(), family=None, person=person)
    return Opener(text=text, lang=lang, segment=seg)


__all__ = [
    "DEFAULT_SEGMENT",
    "GOODS_MAX",
    "SEGMENTS",
    "SOURCE_RU",
    "SOURCE_UZ",
    "TEMPLATES",
    "Opener",
    "fill",
    "goods_words",
    "hooks",
    "lang_of",
    "opener",
    "skeleton",
    "source_word",
    "template_segment",
]
