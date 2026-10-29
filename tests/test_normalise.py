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
from clientscrapper.services import keys
from clientscrapper.services.keys import (
    IG_RESERVED,
    LANDLINE_CODES,
    MOBILE_CODES,
    MOBILE_CODES_UNCONFIRMED,
    NAME_KEY_MAX,
    TENANT_HOSTS,
    instagram_key,
    loose_key,
    name_key,
    phone_e164,
    phone_kind,
    site_key,
    stir_key,
)
from clientscrapper.services.normalise import (
    LINK_HUB_HOSTS,
    SHARED_HOSTS,
    TASHKENT_REGION,
    TG_RESERVED,
    LegalForm,
    Phone,
    canonical_city,
    find_legal_form,
    find_stir,
    given_name_shaped,
    instagram_handles,
    is_person_shaped,
    legal_form_code,
    legal_form_in_name,
    make_phone,
    parse_phones,
    person_names,
    telegram_handles,
    telegram_key,
    trade_word,
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


# 04 §1.1, written out: the two digits after +998.
SPEC_MOBILE = ("33", "77", "88", "90", "91", "93", "94", "95", "97", "98", "99")
SPEC_MOBILE_UNCONFIRMED = ("20", "50", "55")  # mobile only after 04 Q4
SPEC_LANDLINE = (
    "61",
    "62",
    "65",
    "66",
    "67",
    "69",
    "70",
    "71",
    "72",
    "73",
    "74",
    "75",
    "76",
    "78",
    "79",
)
CODE_KINDS = (
    [(c, "mobile") for c in SPEC_MOBILE]
    + [(c, "unknown") for c in SPEC_MOBILE_UNCONFIRMED]
    + [(c, "landline") for c in SPEC_LANDLINE]
)


def test_the_operator_code_sets_are_the_spec_lists() -> None:
    assert frozenset(SPEC_MOBILE) == MOBILE_CODES
    assert frozenset(SPEC_MOBILE_UNCONFIRMED) == MOBILE_CODES_UNCONFIRMED
    assert frozenset(SPEC_LANDLINE) == LANDLINE_CODES


@pytest.mark.parametrize(("code", "kind"), CODE_KINDS)
def test_every_listed_operator_code(code: str, kind: str) -> None:
    raw = f"+998 {code} 200 00 01"
    assert make_phone(raw) == Phone(
        e164=f"+998{code}2000001",
        tail=f"{code}2000001",
        kind=kind,  # type: ignore[arg-type]
        country="UZ",
        raw=raw,
    )


@pytest.mark.parametrize(
    "code",
    [f"{n:02d}" for n in range(100) if f"{n:02d}" not in {c for c, _ in CODE_KINDS}],
)
def test_every_unlisted_operator_code_is_unknown(code: str) -> None:
    assert phone_kind(f"+998{code}2000001") == "unknown"


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
    # Latin diacritics fold to the plain letter (Turkish suppliers, old Uzbek
    # Latin): one key with the spelling that drops them
    ("Öztürk Tekstil", "ozturk tekstil"),
    ("Ozturk Tekstil", "ozturk tekstil"),
    ("İstanbul Tekstil", "istanbul tekstil"),
    ("Kırmızı Mebel", "kirmizi mebel"),
    ("Şahin Çelik Group", "sahin celik group"),
    ("Ğalla Savdo", "galla savdo"),
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


# 04 §1.4's LEGAL_FORMS, written out, plus the spellings keys.LEGAL_FORMS adds
# (to_latin's щ → sh, the Uzbek possessive -i/-si after a name).
SPEC_LEGAL_FORMS = (
    "masuliyati cheklangan jamiyat",
    "obshchestvo s ogranichennoy otvetstvennostyu",
    "xususiy korxona",
    "qoshma korxona",
    "oilaviy korxona",
    "chastnoe predpriyatie",
    "individualnyy predprinimatel",
    "yakka tartibdagi tadbirkor",
    "aksiyadorlik jamiyati",
    "aktsionernoe obshchestvo",
    "fermer xojaligi",
    "mchj",
    "ooo",
    "llc",
    "ltd",
    "qk",
    "xk",
    "aj",
    "ao",
    "oao",
    "zao",
    "chp",
    "xchk",
    "ip",
    "yatt",
    "sp",
    "too",
    "jv",
    "ok",
    "fx",
    "uk",
    "dp",
    "gup",
)
ADDED_LEGAL_FORMS = (
    "masuliyati cheklangan jamiyati",
    "obshestvo s ogranichennoy otvetstvennostyu",
    "xususiy korxonasi",
    "qoshma korxonasi",
    "oilaviy korxonasi",
    "individualniy predprinimatel",
    "aktsiyadorlik jamiyati",
    "aktsionernoe obshestvo",
)
ALL_LEGAL_FORMS = SPEC_LEGAL_FORMS + ADDED_LEGAL_FORMS


def test_legal_forms_are_the_spec_list_and_its_spellings() -> None:
    assert set(keys.LEGAL_FORMS) == set(ALL_LEGAL_FORMS)


@pytest.mark.parametrize("form", ALL_LEGAL_FORMS)
def test_every_legal_form_is_stripped_as_a_whole_token(form: str) -> None:
    assert name_key(f"Namuna Savdo {form}") == "namuna savdo"
    assert name_key(f"{form.upper()} «Namuna Savdo»") == "namuna savdo"
    # glued to a letter it is part of a word of the name, not a form
    assert name_key(f"Namuna {form}x") == f"namuna {form}x"
    assert name_key(f"Namuna x{form}") == f"namuna x{form}"


def test_short_forms_cost_what_the_spec_says() -> None:
    # 04 AC-1: «Sklad Plus» keeps «plus» — «sp» is a form only as a token
    # of its own, never the initials of two words.
    assert name_key("Sklad Plus") == "sklad plus"
    assert name_key("SP Namuna") == "namuna"
    # «IP Telecom» loses «ip»: a lone «ip» token is the Russian ИП (an
    # individual entrepreneur), so a firm named after the internet protocol
    # keys as «telecom». Accepted: the alias table keeps the name as written.
    assert name_key("IP Telecom") == "telecom"
    assert name_key("VoIP Telecom") == "voip telecom"


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


# 04 §1.2, written out.
SPEC_TG_RESERVED = frozenset(
    {
        "joinchat",
        "addstickers",
        "addtheme",
        "share",
        "proxy",
        "socks",
        "iv",
        "s",
        "c",
        "login",
        "setlanguage",
        "confirmphone",
        "telegram",
        "durov",
    }
)
SPEC_IG_RESERVED = frozenset(
    {
        "p",
        "reel",
        "reels",
        "explore",
        "stories",
        "tv",
        "accounts",
        "tags",
        "direct",
        "about",
        "developer",
        "legal",
    }
)


def test_tg_reserved_is_the_spec_list() -> None:
    assert SPEC_TG_RESERVED == TG_RESERVED


@pytest.mark.parametrize("path", sorted(SPEC_TG_RESERVED))
def test_a_reserved_telegram_path_names_nobody(path: str) -> None:
    assert telegram_key(f"https://t.me/{path}") is None
    assert telegram_key(f"@{path}") is None
    if path != "s":  # t.me/s/<name> is that channel's public page
        assert telegram_key(f"t.me/{path}/namuna_optom") is None


def test_ig_reserved_is_the_spec_list() -> None:
    assert SPEC_IG_RESERVED == IG_RESERVED


@pytest.mark.parametrize("path", sorted(SPEC_IG_RESERVED))
def test_a_reserved_instagram_path_names_nobody(path: str) -> None:
    assert instagram_key(f"https://www.instagram.com/{path}/") is None
    assert instagram_key(f"instagram.com/{path}?hl=ru") is None
    assert instagram_key(path) is None


def test_a_link_is_read_in_any_case_and_on_every_telegram_host() -> None:
    """TG_RE reads «T.me/…» and telegram.me / telegram.dog; the key follows."""
    text = "T.me/namuna_optom, TELEGRAM.ME/namuna_savdo, https://telegram.dog/namuna_uz"
    assert telegram_handles(text) == ["namuna_optom", "namuna_savdo", "namuna_uz"]
    assert telegram_key("https://telegram.dog/joinchat/AAAA") is None


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


@pytest.mark.parametrize(
    ("text", "handles"),
    [
        ("Instagram namuna_shop", ["namuna_shop"]),
        ("Insta namuna.uz", ["namuna.uz"]),
        ("Инстаграм namuna_uz", ["namuna_uz"]),
        ("IG namuna2", ["namuna2"]),
        # after a bare space only a handle-shaped token is a handle: these are
        # words and a year, and a handle is an exact identity key
        ("Instagram sahifamizga obuna boʻling", []),
        ("Instagram va Telegram", []),
        ("Instagram 2024", []),
        ("IG namuna", []),
        # a colon, a dash or an «@» says a handle follows, whatever its shape
        ("IG: namuna", ["namuna"]),
        ("Instagram @namuna", ["namuna"]),
    ],
)
def test_instagram_handles_after_a_bare_label(text: str, handles: list[str]) -> None:
    assert instagram_handles(text) == handles


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
        # shared hosts: a card or a page on them is never the firm's own site
        ("https://2gis.uz/tashkent/firm/7000000000000001", None),
        ("https://yandex.uz/maps/org/namuna/1", None),
        ("https://sites.google.com/view/namuna", None),
        ("https://maps.app.goo.gl/AbCdEf", None),
        ("https://www.tiktok.com/@namuna", None),
        ("vk.com/namuna", None),
        ("https://yellowpages.uz/company/namuna", None),
        ("orginfo.uz/organization/1", None),
        ("https://kun.uz/news/1", None),
        ("namuna.en.alibaba.com", None),
        # site builders: the tenant is a website, the platform is not
        ("https://namuna.blogspot.com/", "namuna.blogspot.com"),
        ("namuna.wixsite.com/savdo", "namuna.wixsite.com"),
        ("blogspot.com", None),
        ("www.wixsite.com", None),
        # a dot keeps a host from matching a longer name
        ("box.example.uz", "box.example.uz"),
        ("namunax.com", "namunax.com"),
    ],
)
def test_website_key(raw: str | None, key: str | None) -> None:
    assert website_key(raw) == key


# 04 §1.3, written out.
SPEC_LINK_HUB_HOSTS = frozenset(
    {
        "taplink.cc",
        "taplink.ws",
        "linktr.ee",
        "lnk.bio",
        "instabio.cc",
        "bio.site",
        "clck.ru",
        "bit.ly",
        "cutt.ly",
        "wa.me",
        "api.whatsapp.com",
    }
)


def test_link_hub_hosts_are_the_spec_list() -> None:
    assert SPEC_LINK_HUB_HOSTS == LINK_HUB_HOSTS


@pytest.mark.parametrize("host", sorted(SPEC_LINK_HUB_HOSTS | SHARED_HOSTS))
def test_a_link_hub_or_shared_host_is_no_website(host: str) -> None:
    assert website_key(f"https://{host}/namuna") is None
    assert website_key(f"https://www.{host}/namuna") is None
    assert website_key(f"https://m.{host}/namuna") is None


@pytest.mark.parametrize("host", sorted(TENANT_HOSTS))
def test_a_site_builder_keeps_its_tenants_apart(host: str) -> None:
    assert website_key(f"https://{host}/") is None
    assert website_key(f"https://namuna.{host}/aloqa") == f"namuna.{host}"
    assert site_key(f"namuna.{host}") == f"namuna.{host}"
    assert site_key(f"shop.namuna.{host}") == f"namuna.{host}"
    assert site_key(f"namuna.{host}") != site_key(f"boshqa.{host}")


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
        # a site builder's tenants stay apart: never one «blogspot.com»
        ("namuna.blogspot.com", "namuna.blogspot.com"),
        ("boshqa-firma.blogspot.com", "boshqa-firma.blogspot.com"),
        ("shop.namuna.blogspot.com", "namuna.blogspot.com"),
        ("namuna.wixsite.com", "namuna.wixsite.com"),
        ("namuna.ucoz.ru", "namuna.ucoz.ru"),
        ("namuna.narod.ru", "namuna.narod.ru"),
        ("namuna.tilda.ws", "namuna.tilda.ws"),
        ("namuna.business.site", "namuna.business.site"),
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
        # the Tashkent region is not the city (landlines +998 70, not 71)
        ("Toshkent viloyati, Chirchiq sh.", TASHKENT_REGION),
        ("Тошкент вилояти, Чирчиқ", TASHKENT_REGION),
        ("Ташкентская область, г. Чирчик", TASHKENT_REGION),
        ("Ташкентская обл.", TASHKENT_REGION),
        ("Tashkent region", TASHKENT_REGION),
        ("Toshkent sh., Toshkent viloyati yoʻli", "Toshkent"),
        # a city named anywhere beats a Tashkent bazaar or district
        ("Chorsu bozori Samarqand", "Samarqand"),
        ("Malika Karimova, Samarqand", "Samarqand"),
        ("Sergeli, Toshkent", "Toshkent"),
        ("Abu Saxiy savdo markazi", "Toshkent"),
        ("Moskva", None),
        ("", None),
        (None, None),
    ],
)
def test_canonical_city(text: str | None, city: str | None) -> None:
    assert canonical_city(text) == city


def test_a_region_address_does_not_make_a_city_landline() -> None:
    # The pipeline hands canonical_city's answer to make_phone (04 §1.1 rule 6):
    # a bare 7-digit number in the region is not +998 71.
    region = canonical_city("Toshkent viloyati, Chirchiq sh., 200-00-01")
    assert region == TASHKENT_REGION
    assert make_phone("200-00-01", city=region) is None
    city = canonical_city("г. Ташкент, ул. Намуна 1")
    phone = make_phone("200-00-01", city=city)
    assert phone is not None and (phone.e164, phone.kind) == ("+998712000001", "landline")


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
        # MIYA's other honorifics name shops as often as people; 04 §1.6 names
        # aka/opa only
        "ustoz mebel",
        "bro shop",
        "bobo baraka",
        "domla kitob",
        "xola savdo",
        "amaki market",
    ],
)
def test_not_person_shaped(key: str) -> None:
    assert not is_person_shaped(key)


def test_the_olx_seller_of_the_worked_example_is_a_person() -> None:
    # 04 §9.2: user.name «Sardor» is person-shaped and never a company name.
    assert is_person_shaped(name_key("Sardor"))
    assert not is_person_shaped(name_key("Namuna Tekstil Savdo MChJ"))


# --- Person names in free text (04 §4.2) -------------------------------------------


def test_a_given_name_keeps_its_jon_and_bek() -> None:
    assert given_name_shaped("akmaljon") and given_name_shaped("sardorbek")
    assert given_name_shaped("sardor") and not given_name_shaped("jon")
    assert not given_name_shaped("namunajon")  # «namuna» is no given name
    assert is_person_shaped("dilshodjon")


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("Menejer: Dilshod Karimov", ["Dilshod", "Karimov"]),
        ("Oʻtkir Gʻaniyev, Chilonzor", ["Oʻtkir", "Gʻaniyev"]),
        ("Каримов Рустам Алиевич", ["Каримов", "Рустам", "Алиевич"]),
        ("Ivanov Sergey, Moskva", ["Ivanov", "Sergey"]),
        ("Malika Karimova, Samarqand", ["Malika", "Karimova"]),
        ("Murojaat: Akmal aka", ["Akmal"]),
        ("Shahzod aka, 3 kub", ["Shahzod"]),  # «aka» marks a name nobody listed
        ("Рустам", ["Рустам"]),
        ("Sardor Savdo MChJ direktori Akmal Karimov", ["Akmal", "Karimov"]),
        # A company's name stays (company-tier, 04 §4.2) — a given name
        # before a business word is the firm's, not a person's.
        ("Namuna Tekstil Savdo MChJ direktori", []),
        ("Sardor Savdo MChJ", []),
        # A given name that is also a place counts only beside a surname.
        ("Malika bozori, Toshkent", []),
        # A Russian plural is no surname: only a capitalised word is.
        ("Продажа 20 кубов товаров", []),
        ("Director of Namuna Group", []),
        ("", []),
        (None, []),
    ],
)
def test_the_people_a_text_names(text, names) -> None:
    assert person_names(text) == names


@pytest.mark.parametrize(
    ("word", "trade"),
    [
        ("Optom", True),
        ("Xitoy", True),
        ("Xitoydan", True),
        ("Kafel", True),
        ("Mato", True),
        ("MChJ", True),
        ("Chorsu", True),
        ("Uz", True),
        ("Mollari", False),
        ("Rustam", False),
        ("Namuna", False),
    ],
)
def test_a_trade_word(word, trade) -> None:
    assert trade_word(word) is trade
