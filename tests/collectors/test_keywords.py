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
                    ("5 kubdan ortiq yuk", "5"),
                    ("10 kubli yuk bor", "10"),
                    ("20 kubga yaqin", "20"),
                    ("3 kubometrdan kam", "3"),
                ],
                [
                    "5 kubok yutdik",
                    "kub shaklidagi quti",
                    "Narxi 3 mln so'm",
                    "1250 kub",
                    "5 kubcha shakar",
                    "3 kubik muz",
                ],
            ),
            "uz-Cyrl": (
                [
                    ("Хитойдан 5 куб юк керак", "5"),
                    ("20 м3 мато бор", "20"),
                    ("3 кубометр мебель", "3"),
                    ("5 кубдан ортиқ юк", "5"),
                    ("10 кубли юк", "10"),
                ],
                ["куб шаклидаги қути", "5 кубик ўйинчоқ", "Нархи 3 млн сўм"],
            ),
            "ru": (
                [
                    ("Нужно 5 куб.м мебели", "5"),
                    ("груз 15 м³ из Китая", "15"),
                    ("10 куб м ткани", "10"),
                    ("объём 2.5 м3", "2.5"),
                    ("Нужно привезти 20 кубов из Китая", "20"),
                    ("30 кубометров груза", "30"),
                    ("3 куба мебели", "3"),
                    ("в 1 кубе", "1"),
                ],
                [
                    "кубик Рубика",
                    "цена 3 млн сум",
                    "объём не указан, звоните",
                    # «кубик» is a toy cube or a cc as often as a cubic metre;
                    # the model reads «12 кубиков груза» (04 §4.3), the rule does not.
                    "12 кубиков",
                    "2 кубка чемпиона",
                ],
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
                    ("konteynerda keladi", "konteynerda"),
                    ("furaga yuk kerak", "furaga"),
                    ("2 ta konteynerlarimiz bor", "konteynerlarimiz"),
                    ("tonnadan sotamiz", "tonnadan"),
                    # 04 §5.1's truck words beyond 03 §1.4 (T-18 review)
                    ("full truck kerak", "full truck"),
                    ("FTL yuk Xitoydan", "FTL"),
                ],
                [
                    "kontakt raqam",
                    "furgon xizmati",
                    "tonnel qurilishi",
                    "partiyaviy ish",
                    "full stack dasturchi",
                ],
            ),
            "uz-Cyrl": (
                [
                    ("1 контейнер мато", "контейнер"),
                    ("фура юк керак", "фура"),
                    ("2 тонна шакар", "тонна"),
                    ("янги партия", "партия"),
                    ("контейнерда келади", "контейнерда"),
                    ("фурага юк", "фурага"),
                    ("партияси тайёр", "партияси"),
                    # the half truck whole, never its «фура» alone (T-18 review)
                    ("Хитойдан ярим фура мебел", "ярим фура"),
                    ("ярим фурага юк", "ярим фурага"),
                ],
                ["фургон хизмати", "тоннел", "контакт рақам"],
            ),
            "ru": (
                [
                    ("полфуры мебели", "полфуры"),
                    ("контейнер 40 фут", "контейнер"),
                    ("3 тонны груза", "тонны"),
                    ("поставки партиями", "партиями"),
                    ("Ищу фуру до Ташкента", "фуру"),
                    ("2 контейнера из Китая", "контейнера"),
                    ("отгрузка контейнеров", "контейнеров"),
                    ("5 фур в месяц", "фур"),
                    ("10 тонн груза", "тонн"),
                    ("новые партии", "партии"),
                    ("Нужно пол фуры мебели", "пол фуры"),
                    ("пол-фуры обоев", "пол-фуры"),
                ],
                ["фургон", "тоннель", "партийный билет", "фурнитура", "пол фурнитуры"],
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
                    ("Mato kerakmi?", "kerakmi"),
                    ("taʼminotchilar kerak", "taʼminotchilar"),
                ],
                ["keraksiz narsalar", "olib ketish xizmati", "Biz sotamiz", "kerakmas"],
            ),
            "uz-Cyrl": (
                [
                    ("Хитойдан мато керак", "керак"),
                    ("ким олиб келади?", "олиб келади"),
                    ("кимда бор?", "кимда бор"),
                    ("таъминотчи қидиряпмиз", "таъминотчи"),
                    ("Мато керакми?", "керакми"),
                ],
                ["кераксиз нарсалар", "олиб кетиш", "сотилади", "керакмас"],
            ),
            "ru": (
                [
                    ("Ищу поставщика из Китая", "Ищу"),
                    ("нужен контейнер", "нужен"),
                    ("кто доставит из Китая?", "кто доставит"),
                    ("требуется поставщик", "требуется"),
                    ("Ищем поставщика мебели", "Ищем"),
                    ("Срочно нужны поставщики", "нужны"),
                    ("закупаем оптом", "закупаем"),
                    ("Работаем с поставщиками из Китая", "поставщиками"),
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
                    ("Yangi tovarlar keldi!", "Yangi tovarlar"),
                    ("Konteynerlar keldi", "Konteynerlar keldi"),
                    ("Tovarlar keldi, marhamat", "Tovarlar keldi"),
                    ("optom narxlarda", "optom narxlarda"),
                ],
                ["yangi yil", "yuk kerak", "zavod manzili", "yangi tovarsiz"],
            ),
            "uz-Cyrl": (
                [
                    ("Янги партия келди", "Янги партия"),
                    ("Контейнер келди!", "Контейнер келди"),
                    ("завод нархида", "завод нархида"),
                    ("Янги товарлар келди", "Янги товарлар"),
                    ("Контейнерлар келди", "Контейнерлар келди"),
                ],
                ["янги йил", "юк керак", "завод манзили"],
            ),
            "ru": (
                [
                    ("Новое поступление!", "Новое поступление"),
                    ("фура пришла", "фура пришла"),
                    ("оптовые цены", "оптовые цены"),
                    ("новая партия обуви", "новая партия"),
                    ("Новые поступления каждую неделю", "Новые поступления"),
                    ("Контейнеры пришли!", "Контейнеры пришли"),
                    ("ждём новую партию", "новую партию"),
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
                    ("Namuna kargoxonasi", "kargoxonasi"),
                    ("Xitoydan kargomiz orqali", "kargomiz"),
                    ("yukni kargoga topshiring", "kargoga"),
                    ("Namuna kargosi", "kargosi"),
                    ("Namuna Logistics", "Logistics"),
                ],
                ["Mato optom savdo", "Tikuv fabrikasi Toshkentda", "Narxlar kelishilgan"],
            ),
            "uz-Cyrl": (
                [
                    ("Хитойдан карго хизмати", "карго"),
                    ("Хитойдан юк ташиш хизмати", "юк ташиш"),
                    ("Божхона брокер хизмати", "Божхона брокер"),
                    ("Растаможка хизмати", "Растаможка"),
                    ("Намуна каргохонаси", "каргохонаси"),
                    ("Хитойдан каргомиз орқали", "каргомиз"),
                ],
                ["Мато улгуржи савдо", "Тикув фабрикаси", "Нархлар келишилган"],
            ),
            "ru": (
                [
                    ("Карго из Китая", "Карго"),
                    ("таможенное оформление грузов", "таможенное оформление"),
                    ("грузоперевозки по Узбекистану", "грузоперевозки"),
                    ("доставка из Китая", "доставка"),
                    ("Занимаемся логистикой", "логистикой"),
                    ("экспедиторская компания", "экспедиторская"),
                    ("услуги выкупа", "выкупа"),
                ],
                ["Ткани оптом", "Мебельная фабрика", "Цены договорные"],
            ),
        },
    ),
    # Gate 2 on a self-description (T-19 review): the cargo core; the delivery
    # words an importer writes about its own trade are no match.
    "COMPETITOR_SELF_RE": (
        kw.COMPETITOR_SELF_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("1688 kargo Toshkentga", "1688 kargo"),
                    ("Xitoydan yuk tashish xizmati", "yuk tashish"),
                    ("Rastamojka qilamiz", "Rastamojka"),
                    ("Bojxona xizmati", "Bojxona xizmati"),
                    ("Namuna kargoxonasi", "kargoxonasi"),
                    ("yukingizni yetkazib beramiz", "yukingizni yetkazib beramiz"),
                    ("dostavka Xitoydan 3 kunda", "dostavka Xitoydan"),
                    ("logistika xizmatlari", "logistika xizmatlari"),
                ],
                [
                    "Oshxona mebeli optom. Toshkent bo'ylab yetkazib beramiz!",
                    "Bepul dostavka, optom narxlar",
                    "Barcha tovarlar rastamojka qilingan",
                    "Tovarlar rastamojkadan o'tgan",
                    "Mato optom, buyurtmalar yetkazib beriladi",
                    "Mebel Express",
                ],
            ),
            "uz-Cyrl": (
                [
                    ("Хитойдан карго хизмати", "карго"),
                    ("Хитойдан юк ташиш хизмати", "юк ташиш"),
                    ("Божхона брокер хизмати", "Божхона брокер"),
                    ("Растаможка хизмати", "Растаможка"),
                    ("юкингизни етказиб берамиз", "юкингизни етказиб берамиз"),
                ],
                [
                    "Тошкент бўйлаб етказиб берамиз",
                    "Бепул доставка",
                    "Мебель оптом, ўз логистикамиз",
                ],
            ),
            "ru": (
                [
                    ("Карго из Китая", "Карго"),
                    ("таможенное оформление грузов", "таможенное оформление"),
                    ("грузоперевозки по Узбекистану", "грузоперевозки"),
                    ("доставка из Китая", "доставка из Китая"),
                    ("Доставка грузов из Гуанчжоу", "Доставка грузов"),
                    ("экспедиторская компания", "экспедиторская"),
                    ("услуги выкупа", "выкупа"),
                    ("логистические услуги", "логистические услуги"),
                ],
                [
                    "Стиральные машины оптом со склада. Доставка по Ташкенту бесплатно.",
                    "Сантехника из Китая оптом, своя логистика и склад",
                    "растаможенный товар в наличии",
                    "Доставка по городу",
                ],
            ),
        },
    ),
    # Gate 2 on a name: the core and the logistics words, never «Express».
    "COMPETITOR_NAME_RE": (
        kw.COMPETITOR_NAME_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Namuna Logistika MChJ", "Logistika"),
                    ("Namuna Logistics", "Logistics"),
                    ("Namuna Kargo", "Kargo"),
                    ("Namuna Ekspeditor", "Ekspeditor"),
                ],
                ["Mebel Express", "Namuna Dostavka Optom", "Namuna Savdo MChJ"],
            ),
            "uz-Cyrl": (
                [
                    ("Намуна Логистика", "Логистика"),
                    ("Намуна Карго", "Карго"),
                    ("Намуна каргохонаси", "каргохонаси"),
                ],
                ["Намуна Экспресс Мебель", "Намуна Савдо", "Намуна Текстил"],
            ),
            "ru": (
                [
                    ("ООО «Намуна Логистик»", "Логистик"),
                    ("Намуна Грузоперевозки", "Грузоперевозки"),
                    ("Намуна Экспедиторская компания", "Экспедиторская"),
                ],
                ["Мебель Экспресс", "Намуна Доставка", "ООО «Намуна Трейд»"],
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
    # T-13: a price in a message (03 §2.3 tout filter's price-list shape).
    "PRICE_RE": (
        kw.PRICE_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Narxi 25000 so'm", "25000 so'm"),
                    ("futbolka 45000 soʻm dan", "45000 soʻm"),
                    ("kurtka 120000 som", "120000 som"),
                    ("narxi 25000 soʻmdan", "25000 soʻmdan"),
                    ("1 kub 250$", "250$"),
                    ("Yetkazish 30 usd", "30 usd"),
                ],
                ["Narxi kelishiladi", "25000 sumka", "3 som"],
            ),
            "uz-Cyrl": (
                [
                    ("Нархи 45000 сўм", "45000 сўм"),
                    ("Футболка 25000 сум", "25000 сум"),
                    ("Нарх 120 сўмдан", "120 сўмдан"),
                    ("Куртка 30 у.е.", "30 у.е."),
                ],
                ["Нархи келишилади", "сумка 25 сумка", "5 сўм"],
            ),
            "ru": (
                [
                    ("Цена 150 000 сум", "000 сум"),
                    ("Доставка 25$", "25$"),
                    ("Опт от 40 usd", "40 usd"),
                    ("Цена 30 у.е.", "30 у.е."),
                ],
                ["Цена договорная", "25 сумок", "Сумма заказа 100"],
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
                    # A phone keyboard capitalises a sentence's first letter.
                    ("T.me/namuna_tekstil ga yozing", "namuna_tekstil"),
                    ("Kanal: T.ME/namuna_tekstil", "namuna_tekstil"),
                    ("Https://T.me/namuna_optom", "namuna_optom"),
                    # Telegram's two other link hosts.
                    ("telegram.me/namuna_tekstil", "namuna_tekstil"),
                    ("https://telegram.dog/namuna_optom", "namuna_optom"),
                ],
                [
                    "Pochta: info@namuna.example.uz",
                    "t.me/joinchat/AAAAAE",
                    "@abc qisqa",
                    "t.me/+AbCdEf",
                    "T.ME/JOINCHAT/AAAAAE",
                    "T.me/S/namuna_optom",
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
    "PINFL_RE": (
        kw.PINFL_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("JSHSHIR 31234567890123", "31234567890123"),
                    ("pasport, JShShIR:31234567890124.", "31234567890124"),
                    ("31234567890125 raqamli", "31234567890125"),
                ],
                [
                    # a 2GIS firm id is 17 digits; a 13- or 15-digit run is none
                    "2GIS: 70000001012345678",
                    "hisob 3123456789012",
                    "kod 312345678901234",
                ],
            ),
            "uz-Cyrl": (
                [
                    ("ЖШШИР 31234567890126", "31234567890126"),
                    ("ЖШШИР: 31234567890127, паспорт", "31234567890127"),
                    ("(31234567890128)", "31234567890128"),
                ],
                ["СТИР 301234567", "тел 998900000001", "рақам 1234-5678-9012-34"],
            ),
            "ru": (
                [
                    ("ПИНФЛ 31234567890129", "31234567890129"),
                    ("ПИНФЛ:31234567890130 паспорт", "31234567890130"),
                    ("номер 31234567890131.", "31234567890131"),
                ],
                ["ИНН 301234567", "счёт 20208000123456789012", "ID 70000001012345678"],
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
                    # a space is a separator: the pseudonymiser masks with IG_RE
                    ("Instagram namuna_shop", "namuna_shop"),
                    ("IG namuna", "namuna"),
                    ("Insta — namuna.uz", "namuna.uz"),
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
                    ("Инстаграм namuna_uz", "namuna_uz"),
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
                    ("наш Instagram namuna.shop", "namuna.shop"),
                ],
                [
                    "Инстаграм страница",
                    "в инстаграме",
                    "big sale",
                    "Диван Zig-zag",
                    "подписывайтесь на Instagramm",
                ],
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
    # 04 §6 gate 3 (T-19): a Chinese exporter's name; read on names only.
    "SUPPLIER_NAME_RE": (
        kw.SUPPLIER_NAME_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Yiwu Namuna Trading Co., Ltd", "Trading Co."),
                    ("Guangzhou Namuna Co.,Ltd.", "Co.,Ltd."),
                    ("Namuna Import & Export", "Import & Export"),
                    ("Ningbo Namuna Imp. & Exp. Co", "Imp. & Exp."),
                    ("义乌市纳木纳有限公司", "有限公司"),
                    ("Yiwu Namuna Co., Limited", "Co., Limited"),
                    ("Guangzhou Namuna Company Limited", "Company Limited"),
                ],
                [
                    "Namuna Savdo MChJ",
                    "Namuna Import MChJ",
                    "Coltd mebel",
                    "Limited edition",
                ],
            ),
            "uz-Cyrl": (
                [
                    ("Хитой фирмаси Yiwu Namuna Co. Ltd", "Co. Ltd"),
                    ("Етказиб берувчи: Namuna Trading Co.", "Trading Co."),
                    ("Завод: Namuna Imp & Exp", "Imp & Exp"),
                ],
                ["Намуна савдо МЧЖ", "Намуна импорт", "Колтд мебел"],
            ),
            "ru": (
                [
                    ("Поставщик Shenzhen Namuna Co., Ltd", "Co., Ltd"),
                    ("Фабрика Namuna Import&Export", "Import&Export"),
                    ("Компания Namuna Trading Co. (Китай)", "Trading Co."),
                ],
                ["ООО «Намуна Трейдинг»", "Импорт и экспорт из Китая", "Ltd товары"],
            ),
        },
    ),
    # 04 §6 gate 3 (T-19): a website title in Chinese.
    "CJK_RE": (
        kw.CJK_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Sayt nomi: 义乌纳木纳", "义"),
                    ("Namuna 家具 fabrikasi", "家"),
                    ("灯具 optom", "灯"),
                ],
                ["Namuna Mebel", "Xitoydan mebel", "Ōzbekiston"],
            ),
            "uz-Cyrl": (
                [
                    ("Сайт: 纺织品 Намуна", "纺"),
                    ("Намуна 有限公司", "有"),
                    ("Хитой 工厂", "工"),
                ],
                ["Намуна мебел", "Хитойдан мато", "Ўзбекистон"],
            ),
            "ru": (
                [
                    ("Сайт фабрики 照明", "照"),
                    ("ООО 纳木纳", "纳"),
                    ("Каталог 鞋子", "鞋"),
                ],
                ["Мебель из Китая", "Каталог товаров", "Ёлки оптом"],
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
    # T-18: 04 §5.1's detections (the rule signals of services/signals.py).
    "NON_CN_ORIGIN_RE": (
        kw.NON_CN_ORIGIN_RE,
        _whole,
        {
            # only a country named as the origin (04 §5.1); a destination or
            # a mention is not one (T-18 review)
            "uz-Latn": (
                [
                    ("Turkiyadan mato keldi", "Turkiyadan"),
                    ("Yevropadan uskunalar", "Yevropadan"),
                    ("Erondan kafel", "Erondan"),
                    ("Koreya mahsuloti", "Koreya mahsuloti"),
                    ("Turkiya ishlab chiqarishi", "Turkiya ishlab chiqarishi"),
                ],
                [
                    "Xitoydan mato",
                    "evro remont",
                    "turkcha",
                    "Rossiyaga ham yetkazib beramiz",
                    "Turkiyada filialimiz bor",
                ],
            ),
            "uz-Cyrl": (
                [
                    ("Туркиядан мато", "Туркиядан"),
                    ("Россиядан юк", "Россиядан"),
                    ("Эрондан кафел", "Эрондан"),
                    ("Корея маҳсулоти", "Корея маҳсулоти"),
                ],
                ["Тошкентдан", "евро ремонт", "туркча", "Россияга етказамиз"],
            ),
            "ru": (
                [
                    ("ткани из Турции", "из Турции"),
                    ("товары из России", "из России"),
                    ("плитка из Ирана", "из Ирана"),
                    ("косметика из Южной Кореи", "из Южной Кореи"),
                    ("Производство: Турция", "Производство: Турция"),
                    ("мебель производства Европы", "производства Европы"),
                ],
                [
                    "из Китая",
                    "евроремонт",
                    "турецкий",
                    "доставка по Узбекистану и России",
                    "работа с Россией",
                ],
            ),
        },
    ),
    # The country words alone: the quote of a model's other_origin verdict.
    "NON_CN_COUNTRY_RE": (
        kw.NON_CN_COUNTRY_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Koreya kosmetikasi", "Koreya"),
                    ("Rossiyaga yetkazamiz", "Rossiyaga"),
                    ("Turkiyadan", "Turkiyadan"),
                ],
                ["Xitoydan mato", "evro remont", "turkcha"],
            ),
            "uz-Cyrl": (
                [
                    ("Туркиядан мато", "Туркиядан"),
                    ("Эрон гиламлари", "Эрон"),
                    ("Корея", "Корея"),
                ],
                ["Тошкентдан", "евро ремонт", "туркча"],
            ),
            "ru": (
                [
                    ("ткани из Турции", "Турции"),
                    ("доставка по России", "России"),
                    ("плитка из Ирана", "Ирана"),
                ],
                ["из Китая", "евроремонт", "турецкий"],
            ),
        },
    ),
    "TONNES_RE": (
        kw.TONNES_RE,
        _first,
        {
            "uz-Latn": (
                [
                    ("2 tonna mato", "2"),
                    ("1,5 tonnadan ortiq", "1,5"),
                    ("10 tonnalik yuk", "10"),
                ],
                ["tonnadan sotamiz", "tonnel qurilishi", "2 tonnelga"],
            ),
            "uz-Cyrl": (
                [
                    ("2 тонна шакар", "2"),
                    ("5 тоннадан", "5"),
                    ("3 тоннали юк", "3"),
                ],
                ["тоннадан сотамиз", "тоннел", "2 тоннелга"],
            ),
            "ru": (
                [
                    ("3 тонны груза", "3"),
                    ("10 тонн", "10"),
                    ("0,5 тонны", "0,5"),
                    ("по 2 тонне", "2"),
                ],
                ["тоннель", "тонна груза", "2 тоннеля"],
            ),
        },
    ),
    "VACANCY_IMPORT_RE": (
        kw.VACANCY_IMPORT_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("VED menejeri kerak", "VED"),
                    ("tashqi savdo bo'limiga", "tashqi savdo"),
                    ("xitoy tilini biladigan", "xitoy tilini"),
                ],
                ["savdo menejeri", "tashqi reklama", "xitoy taomlari", "vedomost"],
            ),
            "uz-Cyrl": (
                [
                    ("ВЭД менежери", "ВЭД"),
                    ("ташқи савдо бўлими", "ташқи савдо"),
                    ("хитой тилини билиш", "хитой тилини"),
                ],
                ["савдо менежери", "ташқи реклама", "хитой таомлари"],
            ),
            "ru": (
                [
                    ("Менеджер ВЭД", "ВЭД"),
                    ("оформление деклараций", "деклараций"),
                    ("закупки оборудования в Китае", "закупки оборудования в Китае"),
                    ("со знанием китайского языка", "китайского языка"),
                ],
                [
                    "менеджер по продажам",
                    "закупщик продуктов",
                    "китайская кухня",
                    "ведущий специалист",
                ],
            ),
        },
    ),
    "STATE_BODY_RE": (
        kw.STATE_BODY_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Toshkent shahar hokimligi", "hokimligi"),
                    ("Davlat soliq qo'mitasi", "Davlat"),
                    ("5-maktab", "maktab"),
                    ("Sog'liqni saqlash vazirligi", "vazirligi"),
                ],
                [
                    "savdo markazi",
                    "hokim aka",
                    "agent",
                    "davomat",
                    # state registration, a state body as the customer (T-18 review)
                    "Davlat ro'yxatidan o'tkazilgan sana",
                    "Maktablar uchun mebel",
                ],
            ),
            "uz-Cyrl": (
                [
                    ("Тошкент шаҳар ҳокимлиги", "ҳокимлиги"),
                    ("Давлат солиқ қўмитаси", "Давлат"),
                    ("12-мактаб", "мактаб"),
                    ("ИИБ ходими", "ИИБ"),
                ],
                [
                    "савдо маркази",
                    "агент",
                    "давомат",
                    "дукон",
                    "Давлат рўйхатидан ўтказилган",
                    "Шифохоналар учун ускуналар",
                ],
            ),
            "ru": (
                [
                    ("Министерство здравоохранения", "Министерство"),
                    ("ГУП «Намуна»", "ГУП"),
                    ("городская поликлиника", "поликлиника"),
                    ("МВД", "МВД"),
                ],
                ["министр туризма", "клиника", "магазин"],
            ),
        },
    ),
    # Gate 7 on a name (T-19 review): a state body by its shape; a founder's
    # name and an equipment trader's are no match.
    "STATE_BODY_NAME_RE": (
        kw.STATE_BODY_NAME_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Yunusobod tumani hokimligi", "hokimligi"),
                    ("Davlat soliq qoʻmitasi", "Davlat soliq qoʻmitasi"),
                    ("Davlat unitar korxonasi «Namuna»", "Davlat unitar"),
                    ("Sog'liqni saqlash vazirligi", "vazirligi"),
                    ("12-son maktab", "12-son maktab"),
                    ("5-maktab", "5-maktab"),
                    ("Toshkent shahar 3-son shifoxonasi", "3-son shifoxonasi"),
                    ("Respublika ko'z shifoxonasi", "shifoxonasi"),
                ],
                [
                    "Davlatbek Savdo",
                    "Davlat Mebel Plus",
                    "IP Davlatov",
                    "Davlat Xizmat Servis",
                    "Maktab Mebel Import",
                    "Maktab Jihozlari Savdo",
                    "Shifoxona Jihozlari Savdo",
                    "Namuna Reklama Agentligi",
                    "Maktablar uchun mebel",
                ],
            ),
            "uz-Cyrl": (
                [
                    ("Тошкент шаҳар ҳокимлиги", "ҳокимлиги"),
                    ("Давлат солиқ қўмитаси", "Давлат солиқ қўмитаси"),
                    ("12-мактаб", "12-мактаб"),
                    ("ИИБ", "ИИБ"),
                ],
                ["Давлатбек Савдо", "Мактаб Жиҳозлари", "Давлатов Мебел"],
            ),
            "ru": (
                [
                    ("Министерство здравоохранения", "Министерство"),
                    ("ГУП «Намуна»", "ГУП"),
                    ("городская поликлиника №5", "поликлиника"),
                    (
                        "Государственный налоговый комитет",
                        "Государственный налоговый комитет",
                    ),
                    ("школа № 5", "школа № 5"),
                ],
                ["ИП Давлатов", "Школьная мебель оптом", "Больничное оборудование"],
            ),
        },
    ),
    "COMPANION_RE": (
        kw.COMPANION_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Xaridorlar guruhi: @namuna", "Xaridorlar"),
                    ("Bizning guruhimiz", "guruhimiz"),
                    ("Muhokama chati", "Muhokama"),
                    ("Buyers chat", "Buyers"),
                ],
                ["Murojaat uchun", "Admin bilan bogʻlaning", "Optom narxlarda"],
            ),
            "uz-Cyrl": (
                [
                    ("Харидорлар гуруҳи", "Харидорлар"),
                    ("Гуруҳимизга қўшилинг", "Гуруҳимизга"),
                    ("Муҳокама учун", "Муҳокама"),
                ],
                ["Мурожаат учун", "Админ", "Оптом нархларда"],
            ),
            "ru": (
                [
                    ("Группа покупателей", "Группа"),
                    ("Для покупателей", "покупателей"),
                    ("Наш чат", "чат"),
                ],
                ["По вопросам пишите", "Менеджер", "Оптовые цены"],
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
    "TASHKENT_PLACES_RE": (
        kw.TASHKENT_PLACES_RE,
        _whole,
        {
            "uz-Latn": (
                [
                    ("Chorsu bozori", "Chorsu"),
                    ("Qoʻyliq bozorida", "Qoʻyliq"),
                    ("Abu Sahiy savdo majmuasi", "Abu Sahiy"),
                ],
                # the city names are GEO_CITY's, never a place
                ["Toshkent shahri", "Tashkent", "Samarqand"],
            ),
            "uz-Cyrl": (
                [
                    ("Чорсу бозори", "Чорсу"),
                    ("Сергели тумани", "Сергели"),
                    ("Урикзор бозорида", "Урикзор"),
                ],
                ["Тошкент шаҳри", "Самарқанд", "Москва"],
            ),
            "ru": (
                [
                    ("рынок Абу Сахий", "Абу Сахий"),
                    ("Куйлюк, 3-й ряд", "Куйлюк"),
                    ("Малика бозор", "Малика"),
                ],
                ["Ташкент", "Самарканд", "Алматы"],
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
        ("kargoxona", "kargoxona"),
        ("Kargoxonamiz Yiwuda", "Kargoxonamiz"),
        ("Kargopolov", None),
        ("Qarg'a", None),
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
        # Russian е for ё, as people type it
        ("Сырье и гранулы", {"raw"}),
        ("поставка сырья", {"raw"}),
        ("Тренажёры для зала", {"sport"}),
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


def test_geo_city_and_tashkent_places_partition_geo() -> None:
    listed = [*chain.from_iterable(kw.GEO_CITY.values()), *kw.GEO_TASHKENT_PLACES]
    assert sorted(listed) == sorted(kw.GEO)
    assert len(listed) == len(set(listed))


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("Мы прямой импортер из Китая", "импортер"),
        ("официальный импортёр", "официальный импортёр"),
        ("официальный импортер", "официальный импортер"),
        ("ИМПОРТЕРЫ", "ИМПОРТЕРЫ"),
    ],
)
def test_e_and_yo_are_one_letter(text: str, hit: str) -> None:
    # 03 §1.4 lists «импортёр» and «официальный импортер»: text drops the ё.
    match = kw.B2B_RE.search(text)
    assert match is not None and match.group(0) == hit


@pytest.mark.parametrize(
    ("word", "forms"),
    [
        ("сырьё", ("сырьё", "сырье", "сырья", "сырьем", "сырьём")),
        ("тренажер", ("тренажер", "тренажёр", "тренажёры", "тренажеров")),
    ],
)
def test_a_yo_word_inflects_like_its_e_spelling(
    word: str, forms: tuple[str, ...]
) -> None:
    pattern = kw.terms_re([word])
    assert [f for f in forms if pattern.fullmatch(f)] == list(forms)


def test_short_words_do_not_inflect() -> None:
    # «опт» and «Иу» are three letters or fewer: matched only as written.
    assert kw.terms_re(["опт"]).search("оптика") is None
    assert kw.terms_re(["Иу"]).search("Иуда") is None
    assert kw.terms_re(["Иу"]).search("Иу бозори") is not None


def test_a_price_list_is_three_prices_or_more_than_1500_characters() -> None:
    # 03 §2.3: «≥ 3 prices … or > 1,500 chars» — the tout rule's count (T-14).
    assert kw.PRICELIST_MIN_CHARS == 1500
    assert kw.pricelist_shaped("x" * 1501) is True
    assert kw.pricelist_shaped("x" * 1500) is False
    assert kw.pricelist_shaped("stul 250000 so'm, stol 900000 so'm") is False
    assert kw.pricelist_shaped("stul 250000 so'm, stol 900000 so'm, 30$") is True


@pytest.mark.parametrize(
    ("username", "competitor"),
    [
        ("halolcargo", True),
        ("chinacargouz", True),
        ("mkexpress", True),
        ("xitoy_logistika", True),
        ("namuna_dostavka24", True),
        ("Namuna_Kargo", True),
        ("namuna_tekstil", False),
        ("optom_bozor", False),
        (None, False),
    ],
)
def test_a_glued_username_names_a_forwarder(username, competitor) -> None:
    # COMPETITOR_RE's word bounds cannot see inside «halolcargo» (T-14).
    assert kw.competitor_handle(username) is competitor
