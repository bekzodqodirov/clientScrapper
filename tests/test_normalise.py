"""The normaliser and the keys that have no SQL twin (04 §1; 07 T-03).

Golden tables: each row is an input as a collector would hand it over and the
exact value stored. Phones and names are the bulk (07 asks for ≥ 40 / ≥ 20);
handles, websites, STIR, legal forms, the city and the person-shaped-name
predicate follow. Pure: no database, no clock. Synthetic data only — phones
+998 90 000 00 NN, STIR 3000000NN, handles namuna_<n>, *.example.uz (07 §1.0).
"""

from __future__ import annotations

from typing import get_args

import pytest

from clientscrapper.db.enums import LEGAL_FORMS
from clientscrapper.services.keys import (
    NAME_KEY_MAX,
    instagram_key,
    loose_key,
    name_key,
    phone_e164,
    phone_kind,
    site_key,
    stir_key,
)
from clientscrapper.services.normalise import (
    LegalForm,
    Phone,
    canonical_city,
    find_legal_form,
    find_stir,
    instagram_handles,
    is_person_shaped,
    legal_form_code,
    legal_form_in_name,
    make_phone,
    parse_phones,
    telegram_handles,
    telegram_key,
    website_key,
)

# --- Phones (04 §1.1) ---------------------------------------------------------

# (raw, city, e164, tail, kind, country); e164 None = refused.
PHONES: list[tuple[str, str | None, str | None, str | None, str | None, str | None]] = [
    # 04 §1.1's own table
    ("+998 90 123 45 67", None, "+998901234567", "901234567", "mobile", "UZ"),
    ("90 123-45-67", None, "+998901234567", "901234567", "mobile", "UZ"),
    ("8 (71) 200-00-00", None, "+998712000000", "712000000", "landline", "UZ"),
    ("998 33 000 00 01", None, "+998330000001", "330000001", "mobile", "UZ"),
    ("+7 926 000 00 00", None, "+79260000000", "260000000", "unknown", "other"),
    ("1234", None, None, None, None, None),
    ("+998 90 000 00 00", None, None, None, None, None),
    # the prefixes of rule (2)–(5)
    ("+998900000001", None, "+998900000001", "900000001", "mobile", "UZ"),
    ("998900000002", None, "+998900000002", "900000002", "mobile", "UZ"),
    ("900000003", None, "+998900000003", "900000003", "mobile", "UZ"),
    ("0900000004", None, "+998900000004", "900000004", "mobile", "UZ"),
    ("8900000005", None, "+998900000005", "900000005", "mobile", "UZ"),
    ("00998900000006", None, "+998900000006", "900000006", "mobile", "UZ"),
    ("+998 (90) 000-00-07", None, "+998900000007", "900000007", "mobile", "UZ"),
    ("+998-91-000-00-08", None, "+998910000008", "910000008", "mobile", "UZ"),
    ("tel: 90 000 00 24", None, "+998900000024", "900000024", "mobile", "UZ"),
    (
        " +998 90 000 00 25 (Telegram) ",
        None,
        "+998900000025",
        "900000025",
        "mobile",
        "UZ",
    ),
    # every confirmed mobile code
    ("+998 93 000 00 09", None, "+998930000009", "930000009", "mobile", "UZ"),
    ("+998 94 000 00 10", None, "+998940000010", "940000010", "mobile", "UZ"),
    ("+998 95 000 00 11", None, "+998950000011", "950000011", "mobile", "UZ"),
    ("+998 97 000 00 12", None, "+998970000012", "970000012", "mobile", "UZ"),
    ("+998 98 000 00 13", None, "+998980000013", "980000013", "mobile", "UZ"),
    ("+998 99 000 00 14", None, "+998990000014", "990000014", "mobile", "UZ"),
    ("+998 88 000 00 15", None, "+998880000015", "880000015", "mobile", "UZ"),
    ("+998 77 000 00 16", None, "+998770000016", "770000016", "mobile", "UZ"),
    # area codes
    ("+998 71 200 00 17", None, "+998712000017", "712000017", "landline", "UZ"),
    ("+998 66 200 00 18", None, "+998662000018", "662000018", "landline", "UZ"),
    ("+998 62 200 00 19", None, "+998622000019", "622000019", "landline", "UZ"),
    ("+998 70 200 00 26", None, "+998702000026", "702000026", "landline", "UZ"),
    # 20/50/55 wait for the numbering-plan check (04 Q4): stored, not usable
    ("+998 20 000 00 20", None, "+998200000020", "200000020", "unknown", "UZ"),
    ("+998 50 000 00 21", None, "+998500000021", "500000021", "unknown", "UZ"),
    ("+998 55 000 00 22", None, "+998550000022", "550000022", "unknown", "UZ"),
    ("+998 12 000 00 23", None, "+998120000023", "120000023", "unknown", "UZ"),
    # placeholders and impossible national numbers
    ("+998 90 111 11 11", None, None, None, None, None),
    ("999999999", None, None, None, None, None),
    ("123456789", None, None, None, None, None),
    ("987654321", None, None, None, None, None),
    ("+998 012 345 678", None, None, None, None, None),
    ("+998 90 000 00 0", None, None, None, None, None),
    ("+998 90 000 00 001", None, None, None, None, None),
    ("", None, None, None, None, None),
    ("abc", None, None, None, None, None),
    # rule (6): a bare 7-digit number is a Tashkent landline only in Tashkent
    ("2000000", "Toshkent", "+998712000000", "712000000", "landline", "UZ"),
    ("200-00-01", "г. Ташкент", "+998712000001", "712000001", "landline", "UZ"),
    ("2000002", "Tashkent city", "+998712000002", "712000002", "landline", "UZ"),
    ("2000003", None, None, None, None, None),
    ("2000004", "Samarqand", None, None, None, None),
    ("2000005", "Toshkent viloyati", None, None, None, None),
    # rule (7): another country keeps its E.164
    ("+86 138 0000 0001", None, "+8613800000001", "800000001", "unknown", "other"),
    ("0086 138 0000 0002", None, "+8613800000002", "800000002", "unknown", "other"),
    ("+0 123 4567", None, None, None, None, None),
    ("+12 345", None, None, None, None, None),
    ("+1 234 567 890 123 456", None, None, None, None, None),
]


@pytest.mark.parametrize(("raw", "city", "e164", "tail", "kind", "country"), PHONES)
def test_make_phone(
    raw: str,
    city: str | None,
    e164: str | None,
    tail: str | None,
    kind: str | None,
    country: str | None,
) -> None:
    assert phone_e164(raw, city=city) == e164
    phone = make_phone(raw, city=city)
    if e164 is None:
        assert phone is None
    else:
        assert phone == Phone(e164=e164, tail=tail, kind=kind, country=country, raw=raw)  # type: ignore[arg-type]


def test_the_phone_table_is_big_enough() -> None:
    assert len(PHONES) >= 40


def test_a_short_foreign_number_has_e164_but_no_erp_identity() -> None:
    assert phone_e164("+1234567") == "+1234567"
    assert make_phone("+1234567") is None


@pytest.mark.parametrize(
    ("e164", "kind"),
    [
        ("+9981050", "short"),
        ("+998", "short"),
        ("+99890000", "short"),
        ("+99890000000", "unknown"),
        ("+9989000000011", "unknown"),
        ("+79260000000", "unknown"),
        ("+998x00000001", "unknown"),
    ],
)
def test_phone_kind_edges(e164: str, kind: str) -> None:
    assert phone_kind(e164) == kind


def test_parse_phones_finds_every_number_once_in_order() -> None:
    text = (
        "Tel: +998 90 000 00 01, 90 000-00-02; (71) 200-00-03 va yana +998 90 000 00 01"
    )
    assert [(p.e164, p.kind, p.raw) for p in parse_phones(text)] == [
        ("+998900000001", "mobile", "+998 90 000 00 01"),
        ("+998900000002", "mobile", "90 000-00-02"),
        ("+998712000003", "landline", "(71) 200-00-03"),
    ]


def test_parse_phones_limit() -> None:
    text = "90 000 00 01, 90 000 00 02, 90 000 00 03"
    assert [p.tail for p in parse_phones(text, limit=2)] == ["900000001", "900000002"]


@pytest.mark.parametrize(
    ("text", "tails"),
    [
        ("ИНН 301234567, тел 90 000 00 04", ["900000004"]),
        ("STIR: 300000011 · +998 71 200 00 05", ["712000005"]),
        ("Narxi 1250000 so'm, 7 kun", []),
        ("PINFL 31234567890123", []),
        ("Raqam: +998 90 000 00 00", []),
        ("Звоните: 8 (71) 200-00-06", ["712000006"]),
        ("Хабар учун: 90-000-00-07", ["900000007"]),
        ("", []),
    ],
)
def test_parse_phones_cases(text: str, tails: list[str]) -> None:
    assert [p.tail for p in parse_phones(text)] == tails


# --- Name key (04 §1.4) -------------------------------------------------------

NAMES: list[tuple[str, str]] = [
    # 04 §1.4's own table
    ('"NAMUNA TEKSTIL SAVDO" MChJ', "namuna tekstil savdo"),
    ("ООО «Намуна Текстиль Савдо»", "namuna tekstil savdo"),
    ("Go'zal Mebel XK", "gozal mebel"),
    ("Гўзал Мебель ХК", "gozal mebel"),
    ("ИП Каримов Р.Р.", "karimov r r"),
    ("OOO", "ooo"),
    # every apostrophe a keyboard produces
    ("Goʻzal Mebel XK", "gozal mebel"),
    ("Goʼzal Mebel XK", "gozal mebel"),
    ("Go`zal Mebel XK", "gozal mebel"),
    ("Go‘zal Mebel", "gozal mebel"),
    ("Go’zal Mebel", "gozal mebel"),
    # Cyrillic Uzbek ↔ Latin Uzbek
    ("Ўзбекистон Намуна Савдо", "ozbekiston namuna savdo"),
    ("Oʻzbekiston Namuna Savdo", "ozbekiston namuna savdo"),
    ("Хоразм Ғалла", "xorazm galla"),
    ("Шаҳзода Қурилиш", "shahzoda qurilish"),
    # short legal forms, both scripts, either end
    ("Namuna Savdo МЧЖ", "namuna savdo"),
    ("Namuna Plast ЯТТ", "namuna plast"),
    ("YaTT Namuna", "namuna"),
    ("ЧП «Намуна»", "namuna"),
    ("ТОО Namuna", "namuna"),
    ("AJ «Namuna Sement»", "namuna sement"),
    ("Namuna-Trade LLC", "namuna trade"),
    ("NAMUNA & Co. Ltd", "namuna co"),
    # spelled-out forms, including the Uzbek possessive after a name
    ("Общество с ограниченной ответственностью «Намуна Импорт»", "namuna import"),
    ("Namuna mas'uliyati cheklangan jamiyati", "namuna"),
    ("«Namuna Qurilish» xususiy korxonasi", "namuna qurilish"),
    ("Қўшма корхона «Намуна»", "namuna"),
    ("Акционерное общество Намуна", "namuna"),
    ("Индивидуальный предприниматель Каримов", "karimov"),
    # quotes, punctuation, width, spacing
    ("„Namuna“ ‹Savdo›", "namuna savdo"),
    ("Namuna   Savdo,  MChJ", "namuna savdo"),
    ("Ｎａｍｕｎａ Savdo", "namuna savdo"),
    ("Namuna Mebel №2", "namuna mebel no2"),
    # a name that is only a legal form keeps the form
    ("«ООО»", "ooo"),
    ("МЧЖ", "mchj"),
    ("", ""),
]


@pytest.mark.parametrize(("raw", "key"), NAMES)
def test_name_key(raw: str, key: str) -> None:
    assert name_key(raw) == key


def test_the_name_table_is_big_enough() -> None:
    assert len(NAMES) >= 20


def test_name_key_is_cut_at_160_without_a_trailing_space() -> None:
    # 8-character words put a space at index 159, the last one the cut keeps.
    key = name_key("Namunaa " * 40)
    assert len(key) == NAME_KEY_MAX - 1
    assert key == ("namunaa " * 20)[:159]


@pytest.mark.parametrize(
    ("key", "loose"),
    [
        ("toshkent tekstil", "toshkent tekstil"),
        ("namuna qurilish", "namuna kurilish"),
        ("xorazm mebell", "horazm mebel"),
        ("tshirt savdo", "chirt savdo"),
        ("namuna 2000", "namuna 2000"),
    ],
)
def test_loose_key(key: str, loose: str) -> None:
    assert loose_key(key) == loose


# --- Handles (04 §1.2) --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("@namuna_optom", "namuna_optom"),
        ("T.ME/Namuna_Optom", "namuna_optom"),
        ("https://telegram.me/namuna_optom/77", "namuna_optom"),
        ("t.me/s/namuna_optom", "namuna_optom"),
        ("https://t.me/s/namuna_optom/120?before=121", "namuna_optom"),
        ("t.me/joinchat/AAAAAEnamuna", None),
        ("https://t.me/+AbCdEfGh12", None),
        ("t.me/share/url?url=x", None),
        ("t.me/c/1000000001/5", None),
        ("t.me/addstickers/namuna_pack", None),
        ("t.me/iv?url=https://namuna.example.uz", None),
        ("@telegram", None),
        ("@durov", None),
        ("t.me/namuna_bot", None),
        ("t.me/s", None),
        (None, None),
    ],
)
def test_telegram_key(raw: str | None, key: str | None) -> None:
    assert telegram_key(raw) == key


def test_telegram_handles_in_text() -> None:
    text = (
        "Kanal: t.me/namuna_optom, admin @namuna_admin, t.me/share/url, "
        "info@namuna.example.uz, @namuna_bot, @namuna_optom, t.me/joinchat/AAAAAE"
    )
    assert telegram_handles(text) == ["namuna_optom", "namuna_admin"]


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("https://www.instagram.com/Namuna.Tekstil/?hl=ru", "namuna.tekstil"),
        ("instagram.com/namuna_tekstil", "namuna_tekstil"),
        ("m.instagram.com/namuna_shop", "namuna_shop"),
        ("@namuna_tekstil.", "namuna_tekstil"),
        ("namuna_1", "namuna_1"),
        ("instagram.com/p/ABC123/", None),
        ("https://instagram.com/reel/XYZ", None),
        ("instagram.com/explore/tags/mato", None),
        ("explore", None),
        ("namuna tekstil", None),
        ("a" * 31, None),
        ("", None),
        (None, None),
    ],
)
def test_instagram_key(raw: str | None, key: str | None) -> None:
    assert instagram_key(raw) == key


def test_instagram_handles_in_text() -> None:
    text = (
        "Insta: @namuna.tekstil | instagram.com/namuna_shop/ | big shop | "
        "instagram.com/p/AbC/ | Instagramda kuzating | IG - namuna_uz"
    )
    assert instagram_handles(text) == ["namuna.tekstil", "namuna_shop", "namuna_uz"]


# --- Website (04 §1.3) --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("https://www.namuna.example.uz/aloqa", "namuna.example.uz"),
        ("namuna.example.uz", "namuna.example.uz"),
        ("notfacebook.example.uz", "notfacebook.example.uz"),
        ("m.facebook.com/namuna", None),
        ("l.instagram.com/?u=x", None),
        ("https://taplink.cc/namuna", None),
        ("linktr.ee/namuna", None),
        ("https://api.whatsapp.com/send?phone=998900000001", None),
        ("bit.ly/namuna", None),
        ("https://wa.me/998900000001", None),
        ("t.me/namuna_optom", None),
        (None, None),
    ],
)
def test_website_key(raw: str | None, key: str | None) -> None:
    assert website_key(raw) == key


@pytest.mark.parametrize(
    ("domain", "site"),
    [
        ("shop.namuna.uz", "namuna.uz"),
        ("namuna.uz", "namuna.uz"),
        ("namuna.co.uz", "namuna.co.uz"),
        ("a.b.namuna.com.uz", "namuna.com.uz"),
        ("www2.namuna.com", "namuna.com"),
        ("namuna.example.de", "namuna.example.de"),
        ("co.uz", "co.uz"),
    ],
)
def test_site_key(domain: str, site: str) -> None:
    assert site_key(domain) == site


# --- STIR (04 §1.5) -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stir"),
    [
        ("300000011", "300000011"),
        ("300 000 011", "300000011"),
        ("200000001", "200000001"),
        ("400000001", "400000001"),
        ("600000001", "600000001"),
        ("100000001", None),
        ("700000001", None),
        ("333333333", None),
        ("123456789", None),
        ("30000001", None),
        ("3000000111", None),
        ("", None),
        (None, None),
    ],
)
def test_stir_key(raw: str | None, stir: str | None) -> None:
    assert stir_key(raw) == stir


@pytest.mark.parametrize(
    ("text", "stir"),
    [
        ("STIR: 300000011", "300000011"),
        ("ИНН 301234567, тел 90 000 00 04", "301234567"),
        ("СТИР №300000012", "300000012"),
        ("INN#400000013", "400000013"),
        ("tin 600000014", "600000014"),
        ("ИНН 123456789, СТИР 300000015", "300000015"),
        ("ИНН 700000016", None),
        ("STIR 333333333", None),
        ("300000017", None),
        ("Martin 300000018", None),
        ("ИНН 3000000190", None),
    ],
)
def test_find_stir(text: str, stir: str | None) -> None:
    assert find_stir(text) == stir


# --- Legal forms (04 §1.6) -----------------------------------------------------


def test_legal_form_values_are_the_enum() -> None:
    assert get_args(LegalForm) == LEGAL_FORMS


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("ООО", "OOO"),
        ("OOO", "OOO"),
        ("ХК", "XK"),
        ("ЧП", "ChP"),
        ("ИП", "IP"),
        ("ЯТТ", "YaTT"),
        ("ТОО", "TOO"),
        ("AO", "AJ"),
        ("АО", "AJ"),
        ("АЖ", "AJ"),
        ("MChJ", "MChJ"),
        ("МЧЖ", "MChJ"),
        ("Mas'uliyati cheklangan jamiyati", "MChJ"),
        ("ҚК", "QK"),
        ("Qoʻshma korxona", "QK"),
        ("СП", "SP"),
        ("LLC", "LLC"),
        ("Ltd", "other"),
        ("JV", "other"),
        ("XChK", "other"),
        ("  ", None),
        (None, None),
    ],
)
def test_legal_form_code(raw: str | None, code: str | None) -> None:
    assert legal_form_code(raw) == code


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("«Namuna Savdo» MChJ, Toshkent", "MChJ"),
        ("ООО «Намуна»", "OOO"),
        ("Ташкент, ИП Каримов", "IP"),
        ("Namuna Savdo", None),
    ],
)
def test_find_legal_form(text: str, code: str | None) -> None:
    assert find_legal_form(text) == code


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("Namuna mas'uliyati cheklangan jamiyati", "MChJ"),
        ("Намуна МЧЖ", "MChJ"),
        ("Общество с ограниченной ответственностью «Намуна»", "OOO"),
        ("Namuna xususiy korxonasi", "XK"),
        ("Namuna Savdo", None),
    ],
)
def test_legal_form_in_name(name: str, code: str | None) -> None:
    assert legal_form_in_name(name) == code


# --- City (04 §1.6) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "city"),
    [
        ("г. Ташкент, ул. Намуна 1", "Toshkent"),
        ("Toshkent sh., Chilonzor", "Toshkent"),
        ("Chorsu bozori, 3-qator", "Toshkent"),
        ("Qoʻyliq dehqon bozori", "Toshkent"),
        ("Toshkentga yetkazamiz", "Toshkent"),
        ("в Ташкенте", "Toshkent"),
        ("Самарканд, ул. Намуна", "Samarqand"),
        ("Фергана", "Fargʻona"),
        ("Fargʻona viloyati", "Fargʻona"),
        ("Андижан", "Andijon"),
        ("Buxoro", "Buxoro"),
        ("Нукус", "Nukus"),
        ("Andijon viloyati, Toshkent koʻchasi", "Andijon"),
        ("Moskva", None),
        ("", None),
        (None, None),
    ],
)
def test_canonical_city(text: str | None, city: str | None) -> None:
    assert canonical_city(text) == city


# --- Person-shaped names (04 §1.6, §2.2) -----------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "sardor",
        "aziz aka",
        "malika opa",
        "karimov r r",
        "karimov r",
        "aliyev",
        "rustamova dilnoza",
        "akmal karimov",
        "akmal karimov rustamovich",
        "ivan petrov",
        "mohira",
        "mixail",
        "mikhail",
        "xurshid",
        "hurshid",
        "ulugbek",
    ],
)
def test_person_shaped(key: str) -> None:
    assert is_person_shaped(key)


@pytest.mark.parametrize(
    "key",
    [
        "namuna tekstil savdo",
        "sardor savdo",
        "aziz optom",
        "nova savdo",
        "kiev mebel",
        "namuna",
        "gold star",
        "ooo",
        "akmal karimov rustam savdo",
        "",
    ],
)
def test_not_person_shaped(key: str) -> None:
    assert not is_person_shaped(key)


def test_the_olx_seller_of_the_worked_example_is_a_person() -> None:
    # 04 §9.2: user.name «Sardor» is person-shaped and never a company name.
    assert is_person_shaped(name_key("Sardor"))
    assert not is_person_shaped(name_key("Namuna Tekstil Savdo MChJ"))
