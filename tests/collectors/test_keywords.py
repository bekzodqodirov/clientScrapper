"""Shared keyword regexes (03 §1.4; 07 T-03).

For every pattern and every script — Latin Uzbek, Cyrillic Uzbek, Russian — at
least three texts that must match, each pinned to the exact fragment the
pattern returns (the capture a caller reads: the number for VOLUME_RE, the
handle for TG_RE/IG_RE, the digits for STIR_RE …), and at least three that must
not. 03's named cases are here verbatim: «3 kub kerak Yiwudan» → VOLUME 3 and
INTENT; «1688 kargo Toshkentga» → COMPETITOR; «o'zim uchun 1 dona» → B2C;
«оптом» matches and «оптика» does not. Synthetic data only (07 §1.0).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from itertools import chain

import pytest

from clientscrapper.collectors import keywords as kw

Extract = Callable[[re.Match[str]], str]
SCRIPTS = ("uz-Latn", "uz-Cyrl", "ru")


def _whole(m: re.Match[str]) -> str:
    return m.group(0)


def _first(m: re.Match[str]) -> str:
    return m.group(1)


def _digits(m: re.Match[str]) -> str:
    return "".join(m.groups())


Cases = dict[str, tuple[list[tuple[str, str]], list[str]]]

CASES: dict[str, tuple[re.Pattern[str], Extract, Cases]] = {
    "VOLUME_RE": (
        kw.VOLUME_RE,
        _first,
        {
            "uz-Latn": (
                [
                    ("3 kub kerak Yiwudan", "3"),
                    ("Guangzhoudan 2 kub mato kerak", "2"),
                    ("Jami 12.5 m3 yuk bor", "12.5"),
                    ("hajmi 2,5 kubometr", "2,5"),
                ],
                ["5 kubok yutdik", "kub shaklidagi quti", "Narxi 3 mln so'm", "1250 kub"],
            ),
            "uz-Cyrl": (
                [
                    ("Хитойдан 5 куб юк керак", "5"),
                    ("20 м3 мато бор", "20"),
                    ("3 кубометр мебель", "3"),
                ],
                ["куб шаклидаги қути", "5 кубик ўйинчоқ", "Нархи 3 млн сўм"],
            ),
            "ru": (
                [
                    ("Нужно 5 куб.м мебели", "5"),
                    ("груз 15 м³ из Китая", "15"),
                    ("10 куб м ткани", "10"),
                    ("объём 2.5 м3", "2.5"),
                ],
                ["кубик Рубика", "цена 3 млн сум", "объём не указан, звоните"],
            ),
        },
    ),
    "CONTAINER_RE": (
        kw.CONTAINER_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("1 konteyner mebel keladi", "konteyner"),
                    ("yarim fura yuk", "yarim fura"),
                    ("40 fut konteyner kerak", "40 fut"),
                    ("2 tonna mato", "tonna"),
                    ("20ʼ konteyner", "20ʼ"),
                ],
                ["kontakt raqam", "furgon xizmati", "tonnel qurilishi"],
            ),
            "uz-Cyrl": (
                [
                    ("1 контейнер мато", "контейнер"),
                    ("фура юк керак", "фура"),
                    ("2 тонна шакар", "тонна"),
                    ("янги партия", "партия"),
                ],
                ["фургон хизмати", "тоннел", "контакт рақам"],
            ),
            "ru": (
                [
                    ("полфуры мебели", "полфуры"),
                    ("контейнер 40 фут", "контейнер"),
                    ("3 тонны груза", "тонны"),
                    ("поставки партиями", "партиями"),
                ],
                ["фургон", "тоннель", "партийный билет"],
            ),
        },
    ),
    "INTENT_RE": (
        kw.INTENT_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("3 kub kerak Yiwudan", "kerak"),
                    ("Guangzhoudan mato kim olib keladi?", "olib keladi"),
                    ("Yiwudan tovar kimda bor?", "kimda bor"),
                    ("taʼminotchi qidiryapmiz", "taʼminotchi"),
                ],
                ["keraksiz narsalar", "olib ketish xizmati", "Biz sotamiz"],
            ),
            "uz-Cyrl": (
                [
                    ("Хитойдан мато керак", "керак"),
                    ("ким олиб келади?", "олиб келади"),
                    ("кимда бор?", "кимда бор"),
                    ("таъминотчи қидиряпмиз", "таъминотчи"),
                ],
                ["кераксиз нарсалар", "олиб кетиш", "сотилади"],
            ),
            "ru": (
                [
                    ("Ищу поставщика из Китая", "Ищу"),
                    ("нужен контейнер", "нужен"),
                    ("кто доставит из Китая?", "кто доставит"),
                    ("требуется поставщик", "требуется"),
                ],
                ["ищущий", "ненужное убрать", "закупочная цена"],
            ),
        },
    ),
    "SUPPLY_RE": (
        kw.SUPPLY_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Yangi konteyner keldi!", "Yangi konteyner"),
                    ("Yuk keldi, marhamat", "Yuk keldi"),
                    ("zavod narxida sotamiz", "zavod narxida"),
                    ("optom narx", "optom narx"),
                ],
                ["yangi yil", "yuk kerak", "zavod manzili"],
            ),
            "uz-Cyrl": (
                [
                    ("Янги партия келди", "Янги партия"),
                    ("Контейнер келди!", "Контейнер келди"),
                    ("завод нархида", "завод нархида"),
                ],
                ["янги йил", "юк керак", "завод манзили"],
            ),
            "ru": (
                [
                    ("Новое поступление!", "Новое поступление"),
                    ("фура пришла", "фура пришла"),
                    ("оптовые цены", "оптовые цены"),
                    ("новая партия обуви", "новая партия"),
                ],
                ["новый год", "фура нужна", "оптика"],
            ),
        },
    ),
    "COMPETITOR_RE": (
        kw.COMPETITOR_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("1688 kargo Toshkentga", "1688 kargo"),
                    ("Xitoydan yuk tashish xizmati", "yuk tashish"),
                    ("Bojxona xizmati", "Bojxona xizmati"),
                    ("Rastamojka va vikup", "Rastamojka"),
                ],
                ["Mato optom savdo", "Tikuv fabrikasi Toshkentda", "Narxlar kelishilgan"],
            ),
            "uz-Cyrl": (
                [
                    ("Хитойдан карго хизмати", "карго"),
                    ("Хитойдан юк ташиш хизмати", "юк ташиш"),
                    ("Божхона брокер хизмати", "Божхона брокер"),
                    ("Растаможка хизмати", "Растаможка"),
                ],
                ["Мато улгуржи савдо", "Тикув фабрикаси", "Нархлар келишилган"],
            ),
            "ru": (
                [
                    ("Карго из Китая", "Карго"),
                    ("таможенное оформление грузов", "таможенное оформление"),
                    ("грузоперевозки по Узбекистану", "грузоперевозки"),
                    ("доставка из Китая", "доставка"),
                ],
                ["Ткани оптом", "Мебельная фабрика", "Цены договорные"],
            ),
        },
    ),
    "B2C_RE": (
        kw.B2C_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("o'zim uchun 1 dona", "o'zim uchun"),
                    ("oʼzim uchun olaman", "oʼzim uchun"),
                    ("bitta dona kerak", "bitta dona"),
                    ("shaxsiy foydalanish", "shaxsiy"),
                    ("Faqat 1 dona", "1 dona"),
                ],
                ["100 dona", "optom 1000 dona", "o'zimiz ishlab chiqaramiz"],
            ),
            "uz-Cyrl": (
                [
                    ("ўзим учун", "ўзим учун"),
                    ("битта керак", "битта"),
                    ("шахсий фойдаланиш", "шахсий"),
                ],
                ["100 дона", "ўзимиз ишлаб чиқарамиз", "биттадан"],
            ),
            "ru": (
                [
                    ("для себя", "для себя"),
                    ("нужен 1 шт", "1 шт"),
                    ("только в розницу", "в розницу"),
                    ("лично для мамы", "лично для"),
                ],
                ["от 100 шт", "11 штук", "Работаем лично с фабриками"],
            ),
        },
    ),
    "PHONE_RE": (
        kw.PHONE_RE,
        _digits,
        {
            "uz-Latn": (
                [
                    ("Tel: +998 90 000 00 01", "900000001"),
                    ("Murojaat uchun 90-000-00-02", "900000002"),
                    ("(71) 200-00-03 ga qo'ng'iroq qiling", "712000003"),
                    ("998900000004", "900000004"),
                ],
                [
                    "Narxi 1250000 so'm",
                    "PINFL 31234567890123",
                    "Buyurtma №12345",
                    "+998 90 000 00",
                ],
            ),
            "uz-Cyrl": (
                [
                    ("Тел: +998 90 000 00 05", "900000005"),
                    ("Мурожаат: 90 000 00 06", "900000006"),
                    ("+998(90)0000007", "900000007"),
                ],
                ["Нархи 1 250 000 сўм", "ПИНФЛ 31234567890123", "Буюртма №12345"],
            ),
            "ru": (
                [
                    ("Звоните: +998 90 000-00-08", "900000008"),
                    ("т. 8 (71) 200-00-09", "712000009"),
                    ("WhatsApp 90 000 00 10", "900000010"),
                ],
                ["Цена 1 250 000 сум", "ИНН 3012345678", "Заказ №12345"],
            ),
        },
    ),
    "TG_RE": (
        kw.TG_RE,
        _first,
        {
            "uz-Latn": (
                [
                    ("Kanalimiz: t.me/namuna_optom", "namuna_optom"),
                    ("Admin @namuna_admin ga yozing", "namuna_admin"),
                    ("https://t.me/namuna_tekstil_uz", "namuna_tekstil_uz"),
                ],
                [
                    "Pochta: info@namuna.example.uz",
                    "t.me/joinchat/AAAAAE",
                    "@abc qisqa",
                    "t.me/+AbCdEf",
                ],
            ),
            "uz-Cyrl": (
                [
                    ("Канал: t.me/namuna_optom", "namuna_optom"),
                    ("Админ: @namuna_admin", "namuna_admin"),
                    ("Телеграм @Namuna_Savdo", "Namuna_Savdo"),
                ],
                ["Почта: info@namuna.example.uz", "@кирилл_номи", "t.me/s/namuna_optom"],
            ),
            "ru": (
                [
                    ("Наш канал t.me/namuna_optom", "namuna_optom"),
                    ("Пишите @namuna_admin", "namuna_admin"),
                    ("telegram: @namuna_shop", "namuna_shop"),
                ],
                [
                    "email: sales@namuna.example.uz",
                    "@12345abc",
                    "Подписка t.me/joinchat/AAA",
                ],
            ),
        },
    ),
    "IG_RE": (
        kw.IG_RE,
        _first,
        {
            "uz-Latn": (
                [
                    ("Instagram: @namuna.tekstil", "namuna.tekstil"),
                    ("instagram.com/namuna_shop", "namuna_shop"),
                    ("Insta @namuna_uz", "namuna_uz"),
                    ("IG: namuna_mebel", "namuna_mebel"),
                ],
                [
                    "Instagramda kuzating",
                    "big shop",
                    "LED lighting",
                    "digital marketing",
                    # «ig» inside a word followed by a separator: only the word
                    # start keeps «zag» from reading as a handle.
                    "Zig-zag mebel",
                ],
            ),
            "uz-Cyrl": (
                [
                    ("Инстаграм: @namuna.tekstil", "namuna.tekstil"),
                    ("Инстаграм @namuna_uz", "namuna_uz"),
                    ("instagram.com/namuna_shop саҳифамиз", "namuna_shop"),
                ],
                [
                    "Инстаграмда кузатинг",
                    "Инстаграм саҳифамиз",
                    "Телеграм: @namuna_admin",
                    "Zig-zag шаклидаги",
                ],
            ),
            "ru": (
                [
                    ("Инстаграм: @namuna_shop", "namuna_shop"),
                    ("наш instagram.com/namuna.tekstil/", "namuna.tekstil"),
                    ("insta: namuna_uz", "namuna_uz"),
                ],
                ["Инстаграм страница", "в инстаграме", "big sale", "Диван Zig-zag"],
            ),
        },
    ),
    "EMAIL_RE": (
        kw.EMAIL_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Pochta: info@namuna.example.uz", "info@namuna.example.uz"),
                    (
                        "sales+opt@namuna.example.uz manziliga",
                        "sales+opt@namuna.example.uz",
                    ),
                    ("E-mail: namuna_1@example.uz.", "namuna_1@example.uz"),
                ],
                ["@namuna_admin", "info@namuna", "namuna.example.uz"],
            ),
            "uz-Cyrl": (
                [
                    ("Почта: info@namuna.example.uz", "info@namuna.example.uz"),
                    ("Электрон манзил: savdo@example.uz", "savdo@example.uz"),
                    ("E-mail — opt.uz@namuna.example.uz", "opt.uz@namuna.example.uz"),
                ],
                ["Телеграм @namuna_admin", "почта@намуна.уз", "info @ namuna.example.uz"],
            ),
            "ru": (
                [
                    ("Пишите на sales@namuna.example.uz", "sales@namuna.example.uz"),
                    ("e-mail: Namuna.Opt@example.uz", "Namuna.Opt@example.uz"),
                    ("(zakaz@namuna.example.uz)", "zakaz@namuna.example.uz"),
                ],
                ["Пишите @namuna_admin", "почта не указана", "sales@localhost"],
            ),
        },
    ),
    "STIR_RE": (
        kw.STIR_RE,
        _first,
        {
            "uz-Latn": (
                [
                    ("STIR: 300000011", "300000011"),
                    ("Korxona STIR 301234567", "301234567"),
                    ("INN 400000013", "400000013"),
                ],
                ["300000011", "STIR: 30000001", "Martin 300000018", "STIR 3000000111"],
            ),
            "uz-Cyrl": (
                [
                    ("СТИР: 300000011", "300000011"),
                    ("СТИР №301234567", "301234567"),
                    ("ИНН 300000012", "300000012"),
                ],
                ["СТИР рақами йўқ", "Тел 300000011", "СТИР 3000000111"],
            ),
            "ru": (
                [
                    ("ИНН 301234567", "301234567"),
                    ("ИНН: 300000014", "300000014"),
                    ("инн#300000015", "300000015"),
                ],
                ["ИНН не указан", "ИННОВАЦИИ 300000016", "БИНН 300000017"],
            ),
        },
    ),
    "LEGAL_FORM_RE": (
        kw.LEGAL_FORM_RE,
        _first,
        {
            "uz-Latn": (
                [
                    ("Namuna Tekstil Savdo MChJ", "MChJ"),
                    ("Namuna XK", "XK"),
                    ("YaTT Karimov", "YaTT"),
                    ("Namuna Qoʻshma korxonasi", "Qoʻshma"),
                ],
                ["Namuna Savdo", "SPORT DO'KONI", "AJOYIB MEBEL"],
            ),
            "uz-Cyrl": (
                [
                    ("«Намуна» МЧЖ", "МЧЖ"),
                    ("Намуна ХК", "ХК"),
                    ("ЯТТ Каримов", "ЯТТ"),
                ],
                ["Намуна савдо", "АЖОЙИБ МЕБЕЛ", "СПОРТ ДЎКОНИ"],
            ),
            "ru": (
                [
                    ("ООО «Намуна»", "ООО"),
                    ("ИП Каримов", "ИП"),
                    ("ЧП Намуна", "ЧП"),
                    ("СП ООО «Намуна»", "СП"),
                ],
                ["Намуна Трейд", "ИПОТЕКА", "ЧПУ станки"],
            ),
        },
    ),
    "B2B_RE": (
        kw.B2B_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Mato optom", "optom"),
                    ("Ulgurji narxlarda", "Ulgurji narxlarda"),
                    ("dilerlarga chegirma", "dilerlarga"),
                    ("Doʻkonlar uchun narx", "Doʻkonlar uchun"),
                    ("Skladimizdan olib keting", "Skladimizdan"),
                ],
                ["Optika do'koni", "Chakana savdo", "Dilshod aka"],
            ),
            "uz-Cyrl": (
                [
                    ("Мато оптом", "оптом"),
                    ("Улгуржи нархда", "Улгуржи нархда"),
                    ("Дилерлар учун", "Дилерлар"),
                    ("Дўконлар учун", "Дўконлар учун"),
                ],
                ["Оптика дўкони", "Чакана савдо", "Дилшод ака"],
            ),
            "ru": (
                [
                    ("Продаём оптом и в розницу", "оптом"),
                    ("мелким оптом", "оптом"),
                    ("Ищем дилеров", "дилеров"),
                    ("Цены от производителя", "от производителя"),
                    ("Склад в Ташкенте", "Склад"),
                ],
                ["Салон оптика", "Оптический центр", "Только в розницу"],
            ),
        },
    ),
    "CHINA_RE": (
        kw.CHINA_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Guangzhoudan 2 kub mato kerak", "Guangzhoudan"),
                    ("Xitoydan olib kelamiz", "Xitoydan"),
                    ("Yiwudan tovar", "Yiwudan"),
                    ("1688 dan buyurtma", "1688"),
                ],
                ["Turkiyadan olib kelamiz", "Mahalliy ishlab chiqarish", "Chinni bozor"],
            ),
            "uz-Cyrl": (
                [
                    ("Хитойдан юк", "Хитойдан"),
                    ("Хитой товарлари", "Хитой"),
                    ("Гуанчжоу шаҳридан", "Гуанчжоу"),
                    ("Иу бозоридан", "Иу"),
                ],
                ["Туркиядан", "Маҳаллий ишлаб чиқариш", "Россиядан"],
            ),
            "ru": (
                [
                    ("закупки оборудования в Китае", "Китае"),
                    ("товары из Китая", "из Китая"),
                    ("доставка из КНР", "КНР"),
                    ("Кашгар — Ташкент", "Кашгар"),
                ],
                ["из Турции", "местное производство", "Казахстан"],
            ),
        },
    ),
    "GEO_RE": (
        kw.GEO_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Toshkentga yetkazamiz", "Toshkentga"),
                    ("Chorsu bozori", "Chorsu"),
                    ("Qoʻyliq bozorida", "Qoʻyliq"),
                    ("Abu Saxiy savdo markazi", "Abu Saxiy"),
                ],
                ["Moskva", "Toshloq tumani", "Chorvoq"],
            ),
            "uz-Cyrl": (
                [
                    ("Тошкентда", "Тошкентда"),
                    ("Чорсу бозори", "Чорсу"),
                    ("Сергели тумани", "Сергели"),
                ],
                ["Москва", "Тошлоқ", "Чорвоқ"],
            ),
            "ru": (
                [
                    ("в Ташкенте", "Ташкенте"),
                    ("рынок Абу Сахий", "Абу Сахий"),
                    ("Самарканд", "Самарканд"),
                    ("из Ферганы", "Ферганы"),
                ],
                ["Москва", "Санкт-Петербург", "Алматы"],
            ),
        },
    ),
}

PARAMS = [
    pytest.param(name, script, text, want, id=f"{name}-{script}-{text}")
    for name, (_, _, by_script) in CASES.items()
    for script, (positives, _) in by_script.items()
    for text, want in positives
]
NEGATIVES = [
    pytest.param(name, script, text, id=f"{name}-{script}-{text}")
    for name, (_, _, by_script) in CASES.items()
    for script, (_, negatives) in by_script.items()
    for text in negatives
]


@pytest.mark.parametrize(("name", "script", "text", "want"), PARAMS)
def test_matches(name: str, script: str, text: str, want: str) -> None:
    pattern, extract, _ = CASES[name]
    match = pattern.search(text)
    assert match is not None, f"{name} found nothing in {text!r}"
    assert extract(match) == want


@pytest.mark.parametrize(("name", "script", "text"), NEGATIVES)
def test_does_not_match(name: str, script: str, text: str) -> None:
    pattern, _, _ = CASES[name]
    assert pattern.search(text) is None


@pytest.mark.parametrize("name", list(CASES))
def test_every_script_has_three_of_each(name: str) -> None:
    _, _, by_script = CASES[name]
    assert set(by_script) == set(SCRIPTS)
    for positives, negatives in by_script.values():
        assert len(positives) >= 3 and len(negatives) >= 3


def test_every_regex_of_the_module_is_covered() -> None:
    patterns = {
        n for n, v in vars(kw).items() if n.endswith("_RE") and isinstance(v, re.Pattern)
    }
    assert patterns == set(CASES)


# --- 03's named cases --------------------------------------------------------


def test_the_spec_examples() -> None:
    volume = kw.VOLUME_RE.search("3 kub kerak Yiwudan")
    assert volume is not None and volume.group(1) == "3"
    intent = kw.INTENT_RE.search("3 kub kerak Yiwudan")
    assert intent is not None and intent.group(0) == "kerak"
    competitor = kw.COMPETITOR_RE.search("1688 kargo Toshkentga")
    assert competitor is not None and competitor.group(0) == "1688 kargo"
    b2c = kw.B2C_RE.search("o'zim uchun 1 dona")
    assert b2c is not None and b2c.group(0) == "o'zim uchun"


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("оптом", "оптом"),
        ("Ткани ОПТОМ", "ОПТОМ"),
        ("крупный опт", "крупный опт"),
        ("оптика", None),
        ("Салон «Оптика»", None),
        ("оптический", None),
    ],
)
def test_optom_is_wholesale_and_optika_is_not(text: str, hit: str | None) -> None:
    match = kw.B2B_RE.search(text)
    assert (match.group(0) if match else None) == hit


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("kargo", "kargo"),
        ("Xitoydan kargo xizmati", "kargo"),
        ("KARGO 1688", "KARGO"),
        ("карго Гуанчжоу — Ташкент", "карго"),
        ("Avia kargo", "Avia kargo"),
        ("cargo service", "cargo"),
        ("kargoxona", None),
        ("Kargopolov", None),
    ],
)
def test_kargo_is_a_competitor_word(text: str, hit: str | None) -> None:
    match = kw.COMPETITOR_RE.search(text)
    assert (match.group(0) if match else None) == hit


# --- Word lists ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "categories"),
    [
        ("Mato va gazlama optom", {"textile"}),
        ("Mebel furnitura", {"furniture"}),
        ("Kafel, santexnika", {"building"}),
        ("Bolalar oʻyinchoqlari", {"toys"}),
        ("Мато ва газлама", {"textile"}),
        ("Мебель", {"furniture"}),
        ("Қурилиш моллари", {"building"}),
        ("Ткани оптом", {"textile"}),
        ("Мебельная фурнитура", {"furniture"}),
        ("Сантехника и плитка", {"building"}),
        ("Бытовая техника и посуда", {"electronics", "household"}),
        ("Non va sut mahsulotlari", set()),
        ("Restoran va kafe", set()),
        ("Avto xizmat", set()),
        ("Нон ва сут", set()),
        ("Ресторан", set()),
        ("Туризм", set()),
        ("Продукты питания", set()),
        ("Услуги юриста", set()),
        ("мать и дитя", set()),
    ],
)
def test_bulky_categories(text: str, categories: set[str]) -> None:
    assert kw.bulky_categories(text) == categories


def test_bulky_categories_are_the_seventeen_serp_keys() -> None:
    assert len(kw.BULKY_CATEGORIES) == 17


def test_geo_city_partitions_geo() -> None:
    listed = list(chain.from_iterable(kw.GEO_CITY.values()))
    assert sorted(listed) == sorted(kw.GEO)
    assert len(listed) == len(set(listed))


def test_short_words_do_not_inflect() -> None:
    # «опт» and «Иу» are three letters or fewer: matched only as written.
    assert kw.terms_re(["опт"]).search("оптика") is None
    assert kw.terms_re(["Иу"]).search("Иуда") is None
    assert kw.terms_re(["Иу"]).search("Иу бозори") is not None
