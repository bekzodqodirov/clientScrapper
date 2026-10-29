"""Pseudonymisation (04 §4.2, 06 §5.2): golden Uzbek Latin, Uzbek Cyrillic
and Russian cases, the round trip, idempotence, stable numbering across a
bundle, and the fence ``assert_masked``.

All data is synthetic (07 §1.0): «Namuna …», ``namuna_<n>``,
``+998 90 000 00 NN``, ``*.example.uz``.
"""

from __future__ import annotations

import re

import pytest

from clientscrapper.collectors import keywords
from clientscrapper.services import pseudonyms as p
from clientscrapper.services.pseudonyms import (
    Pseudonymised,
    UnmaskedPayload,
    assert_masked,
    pseudonymise,
    restore,
)

# 02 §2.6: what a stored request must never hold.
RAW = re.compile(r"\+998\d{9}|@[a-z0-9_]{5,}")

GOLDEN = [
    # (text, names, masked, reverse)
    (
        "Guangzhoudan 2 kub mato kerak, kim olib keladi?",
        (),
        "Guangzhoudan 2 kub mato kerak, kim olib keladi?",
        {},
    ),
    (
        "Tel: +998 90 000 00 01, yozing @namuna_tekstil",
        (),
        "Tel: <PHONE_1>, yozing @<TG_1>",
        {"<PHONE_1>": "+998 90 000 00 01", "<TG_1>": "namuna_tekstil"},
    ),
    (
        "Звоните 90 000-00-02, почта info@namuna.example.uz",
        (),
        "Звоните <PHONE_1>, почта <EMAIL_1>",
        {"<PHONE_1>": "90 000-00-02", "<EMAIL_1>": "info@namuna.example.uz"},
    ),
    (
        "Рустамга ёзинг: t.me/namuna_optom, 3 куб керак",
        ("Rustam",),
        "<NAME_1>га ёзинг: t.me/<TG_1>, 3 куб керак",
        {"<NAME_1>": "Рустам", "<TG_1>": "namuna_optom"},
    ),
    (
        "Rustam aka va Karimovning ombori, (90) 000-00-04",
        ("Рустам Каримов",),
        "<NAME_1> aka va <NAME_2>ning ombori, <PHONE_1>",
        {"<NAME_1>": "Rustam", "<NAME_2>": "Karimov", "<PHONE_1>": "(90) 000-00-04"},
    ),
    (
        # Phones before IG_RE: its label branch would read «90» as a handle.
        "IG 90 000 00 03, Instagram: namuna_shop, instagram.com/namuna_mebel",
        (),
        "IG <PHONE_1>, Instagram: <IG_1>, instagram.com/<IG_2>",
        {
            "<PHONE_1>": "90 000 00 03",
            "<IG_1>": "namuna_shop",
            "<IG_2>": "namuna_mebel",
        },
    ),
    (
        "Insta @namuna_shop: optom narx",
        (),
        "Insta @<TG_1>: optom narx",
        {"<TG_1>": "namuna_shop"},
    ),
    (
        "Pochta namuna@mail.example.uz, telegram yoʻq",
        (),
        "Pochta <EMAIL_1>, telegram yoʻq",
        {"<EMAIL_1>": "namuna@mail.example.uz"},
    ),
    (
        # A 14-digit run first: never reversible, never in the map — and never
        # IG_RE's «handle» after «ig».
        "JSHSHIR 31234567890123, ig 31234567890124",
        (),
        "JSHSHIR <ID>, ig <ID>",
        {},
    ),
    (
        # A 17-digit 2GIS id is no PINFL and no phone.
        "2GIS: 70000001012345678",
        (),
        "2GIS: 70000001012345678",
        {},
    ),
    (
        "STIR 301234567, Namuna Tekstil Savdo MChJ, Toshkent, 25 kub",
        (),
        "STIR 301234567, Namuna Tekstil Savdo MChJ, Toshkent, 25 kub",
        {},
    ),
    # A name is matched whatever apostrophe the text types for oʻ/gʻ/ʼ, in
    # both directions; the token stands for the text's own spelling.
    (
        "Oʻtkir aka keldi, Gʻayratga ayting",
        ("O'tkir", "G’ayrat"),
        "<NAME_1> aka keldi, <NAME_2>ga ayting",
        {"<NAME_1>": "Oʻtkir", "<NAME_2>": "Gʻayrat"},
    ),
    (
        "O’tkir va G‘ayrat, Toʻlqinning ombori, Maʼrufdan",
        ("Oʻtkir", "Gʻayrat", "Toʻlqin", "Maʼruf"),
        "<NAME_1> va <NAME_2>, <NAME_3>ning ombori, <NAME_4>dan",
        {
            "<NAME_1>": "O’tkir",
            "<NAME_2>": "G‘ayrat",
            "<NAME_3>": "Toʻlqin",
            "<NAME_4>": "Maʼruf",
        },
    ),
    (
        # Russian endings on a Cyrillic spelling; the vowel a Russian ending
        # replaces is outside the token too («Дилноз-ы»).
        "Звоните Рустаму или Ивановой, у Дилнозы склад, с Рустамом",
        ("Rustam", "Ivanova", "Dilnoza"),
        "Звоните <NAME_1>у или <NAME_2>ой, у <NAME_3>ы склад, с <NAME_1>ом",
        {"<NAME_1>": "Рустам", "<NAME_2>": "Иванов", "<NAME_3>": "Дилноз"},
    ),
    (
        # The affectionate «-jon» is the same person.
        "Rustamjon aka, Ёқубжонга айтинг",
        ("Rustam", "Yoqubjon"),
        "<NAME_1>jon aka, <NAME_2>жонга айтинг",
        {"<NAME_1>": "Rustam", "<NAME_2>": "Ёқуб"},
    ),
    (
        # A phone keyboard capitalises «T.me» at a sentence start; Telegram's
        # other link hosts.
        "T.me/namuna_tekstil ga yozing. Kanal: TELEGRAM.ME/namuna_optom, "
        "telegram.dog/namuna_savdo",
        (),
        "T.me/<TG_1> ga yozing. Kanal: TELEGRAM.ME/<TG_2>, telegram.dog/<TG_3>",
        {"<TG_1>": "namuna_tekstil", "<TG_2>": "namuna_optom", "<TG_3>": "namuna_savdo"},
    ),
]


@pytest.mark.parametrize(("text", "names", "masked", "reverse"), GOLDEN)
def test_golden_masks(text, names, masked, reverse):
    result = pseudonymise(text, names=names)
    assert (result.text, result.reverse) == (masked, reverse)
    assert_masked(result.text)
    assert RAW.search(result.text) is None


@pytest.mark.parametrize(("text", "names", "masked", "reverse"), GOLDEN)
def test_the_round_trip_restores_exactly(text, names, masked, reverse):
    result = pseudonymise(text, names=names)
    expected = keywords.PINFL_RE.sub("<ID>", text)  # never reversible
    assert restore(result.text, result.reverse) == expected


@pytest.mark.parametrize(("text", "names", "masked", "reverse"), GOLDEN)
def test_masking_twice_changes_nothing(text, names, masked, reverse):
    once = pseudonymise(text, names=names)
    twice = pseudonymise(once.text, names=names, prior=once)
    assert twice == once


def test_one_value_is_one_token_however_written():
    text = "+998 90 000 00 01 yoki 90 000 00 01; @Namuna_Tekstil, t.me/namuna_tekstil"
    result = pseudonymise(text)
    assert result.text == "<PHONE_1> yoki <PHONE_1>; @<TG_1>, t.me/<TG_1>"
    # The token stands for the first spelling; restore writes it everywhere.
    assert result.reverse == {
        "<PHONE_1>": "+998 90 000 00 01",
        "<TG_1>": "Namuna_Tekstil",
    }
    assert restore(result.text, result.reverse) == (
        "+998 90 000 00 01 yoki +998 90 000 00 01; @Namuna_Tekstil, t.me/Namuna_Tekstil"
    )


def test_numbering_is_stable_across_a_bundle():
    first = pseudonymise(
        "E1: +998 90 000 00 01, @namuna_tekstil, Rustam", names=["Rustam"]
    )
    second = pseudonymise("E2: @namuna_optom, +998900000001, Rustamdan", prior=first)
    assert first.text == "E1: <PHONE_1>, @<TG_1>, <NAME_1>"
    # A value seen in an earlier block keeps its token; a new one is next;
    # the earlier block's names are masked again with no names passed.
    assert second.text == "E2: @<TG_2>, <PHONE_1>, <NAME_1>dan"
    assert second.reverse == {
        "<PHONE_1>": "+998 90 000 00 01",
        "<TG_1>": "namuna_tekstil",
        "<NAME_1>": "Rustam",
        "<TG_2>": "namuna_optom",
    }
    third = pseudonymise("E3: 90 000 00 05", prior=second)
    assert third.text == "E3: <PHONE_2>"


def test_a_name_is_masked_in_both_scripts_with_its_ending_kept():
    """Any form of a name word is masked: a «Bekzodjon» who is another
    person is a person's name too; a longer word is not one («Bekzodova» is
    a surname of its own, «Alisher» a name of its own)."""
    result = pseudonymise(
        "Bekzodga ayting, Бекзоднинг ombori; Bekzodjon boshqa odam, Bekzodova, Alisher",
        names=["Bekzod", "Ali"],
    )
    assert result.text == (
        "<NAME_1>ga ayting, <NAME_1>нинг ombori; <NAME_1>jon boshqa odam, "
        "Bekzodova, Alisher"
    )
    assert result.reverse == {"<NAME_1>": "Bekzod"}


def test_a_name_without_its_apostrophe_is_the_same_name():
    result = pseudonymise("Otkir keldi, Oʻtkirdan so'rang", names=["Oʻtkir"])
    assert result.text == "<NAME_1> keldi, <NAME_1>dan so'rang"
    assert result.reverse == {"<NAME_1>": "Otkir"}


def test_one_name_is_one_token_in_both_scripts_however_declined():
    """«Dilnoza» and «Дилнозы» are matched by one name word: one token, though
    the stems differ («Dilnoza», «Дилноз»)."""
    result = pseudonymise("Dilnoza keldi, Дилнозы нет", names=["Dilnoza"])
    assert result.text == "<NAME_1> keldi, <NAME_1>ы нет"
    assert result.reverse == {"<NAME_1>": "Dilnoza"}


def test_a_declined_name_keeps_its_token_across_a_bundle():
    first = pseudonymise("Дилнозе скажите, Сергея нет", names=["Dilnoza", "Сергей"])
    assert first.text == "<NAME_1>е скажите, <NAME_2>я нет"
    # No names passed: the stems the first block left mask the later forms.
    second = pseudonymise("Дилноза, Дилнозага, Сергеем", prior=first)
    assert second.text == "<NAME_1>а, <NAME_1>ага, <NAME_2>ем"
    assert restore(second.text, second.reverse) == "Дилноза, Дилнозага, Сергеем"


def test_initials_and_empty_names_mask_nothing():
    result = pseudonymise("R. Karimov: 2 kub", names=["R.", "", "  ", "Karimov"])
    assert result.text == "R. <NAME_1>: 2 kub"


def test_company_tier_facts_stay():
    text = "OOO «Namuna Savdo», STIR 301234567, Chorsu bozori, 1 kub — 300$"
    assert pseudonymise(text).text == text


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("tel +998 90 000 00 01", "PHONE_RE"),
        ("tel 900000001", "PHONE_RE"),
        ("@namuna_tekstil yozing", "TG_RE"),
        ("https://t.me/namuna_optom", "TG_RE"),
        ("Instagram: namuna_shop", "IG_RE"),
        ("info@namuna.example.uz", "EMAIL_RE"),
        ("T.me/namuna_optom", "TG_RE"),
        ("telegram.me/namuna_optom", "TG_RE"),
        ("JSHSHIR 31234567890123", "PINFL_RE"),
        # The E2E review's shapes (2026-10-29): foreign numbers, a long run,
        # a messenger id and a Telegram id written without «@» after a label.
        ("Qozogʻiston nomer +7 701 000 00 00", "INTL_PHONE_RE"),
        ("agent +86 138 0000 0000", "INTL_PHONE_RE"),
        ("tel 8 916 000 00 77", "LONG_NUMBER_RE"),
        ("WeChat: rustam_cn88", "MESSENGER_ID_RE"),
        ("Telegram: namuna_tekstil", "MESSENGER_ID_RE"),
        ("tg namuna_tekstil", "MESSENGER_ID_RE"),
    ],
)
def test_assert_masked_refuses_raw_personal_data(text, pattern):
    with pytest.raises(UnmaskedPayload) as refused:
        assert_masked(text)
    assert refused.value.pattern == pattern
    assert str(refused.value) == pattern  # the pattern, never the value


def test_assert_masked_passes_tokens():
    assert_masked("@<TG_1>, t.me/<TG_2>, <PHONE_1>, <EMAIL_1>, Instagram: <IG_1>, <ID>")


def test_restore_leaves_unknown_tokens_and_ids():
    restored = restore("<PHONE_9> <ID> <NAME_1>", {"<NAME_1>": "Aziz"})
    assert restored == "<PHONE_9> <ID> Aziz"


def test_the_patterns_are_the_keywords_own():
    """One pattern, one home (06 §5.2): the objects are collectors/keywords'
    (tests/test_imports_fence.py holds that none is redefined here)."""
    assert set(p.GUARDED) == {
        "PHONE_RE",
        "INTL_PHONE_RE",
        "LONG_NUMBER_RE",
        "TG_RE",
        "MESSENGER_ID_RE",
        "IG_RE",
        "EMAIL_RE",
        "PINFL_RE",
    }
    assert all(isinstance(getattr(keywords, name), re.Pattern) for name in p.GUARDED)
    assert p.MESSENGER_ID_RE is keywords.MESSENGER_ID_RE
    assert p.intl_phones is keywords.intl_phones
    assert p.long_numbers is keywords.long_numbers
    # PHONE_RE through normalise.phone_matches (the labelled-STIR rule).
    assert p.phone_matches.__module__ == "clientscrapper.services.normalise"
    assert p.TG_RE is keywords.TG_RE
    assert p.IG_RE is keywords.IG_RE and p.EMAIL_RE is keywords.EMAIL_RE
    assert p.PINFL_RE is keywords.PINFL_RE


def test_the_names_04_and_06_give_are_the_only_ones():
    assert {"Pseudonymised", "pseudonymise", "restore", "assert_masked"} <= set(p.__all__)
    for gone in ("Masked", "mask", "unmask"):
        assert not hasattr(p, gone)
    assert Pseudonymised("x").reverse == {}


# --- the E2E review's shapes (2026-10-29) ---------------------------------------


@pytest.mark.parametrize(
    ("text", "masked", "reverse"),
    [
        # Foreign numbers whole, before PHONE_RE could take «86 138 0000».
        ("+86 138 0000 0000", "<PHONE_1>", {"<PHONE_1>": "+86 138 0000 0000"}),
        (
            "Qozogʻiston nomer +7 701 000 00 00",
            "Qozogʻiston nomer <PHONE_1>",
            {"<PHONE_1>": "+7 701 000 00 00"},
        ),
        # The number ends where its plan does: the volume after it stays.
        (
            "+7 701 000 00 00 30 kub kerak",
            "<PHONE_1> 30 kub kerak",
            {"<PHONE_1>": "+7 701 000 00 00"},
        ),
        (
            "tel 8 916 000 00 77 3 kub",
            "tel <PHONE_1> 3 kub",
            {"<PHONE_1>": "8 916 000 00 77"},
        ),
        (
            "0086 138 0000 0002 ga yozing",
            "<PHONE_1> ga yozing",
            {"<PHONE_1>": "0086 138 0000 0002"},
        ),
        # A date beside a volume is no number (no dot in a long run).
        ("Muddat 25.11.2026 10 kub kerak", "Muddat 25.11.2026 10 kub kerak", {}),
        # A messenger id after its label: the label stays, the id is a token.
        ("WeChat: rustam_cn88", "WeChat: <MSG_1>", {"<MSG_1>": "rustam_cn88"}),
        ("viber rustam_88 orqali", "viber <MSG_1> orqali", {"<MSG_1>": "rustam_88"}),
        ("whatsapp orqali yozing", "whatsapp orqali yozing", {}),
        # A Telegram username written without «@» after its label is a handle.
        ("Telegram: namuna_tekstil", "Telegram: <TG_1>", {"<TG_1>": "namuna_tekstil"}),
        ("tg namuna_tekstil", "tg <TG_1>", {"<TG_1>": "namuna_tekstil"}),
        ("tg kanal", "tg kanal", {}),
        # A labelled STIR is company-tier and stays (04 §4.2).
        ("STIR 300000011", "STIR 300000011", {}),
    ],
)
def test_foreign_numbers_and_labelled_ids_are_masked(text, masked, reverse):
    result = pseudonymise(text)
    assert (result.text, result.reverse) == (masked, reverse)
    assert_masked(result.text)
    assert restore(result.text, result.reverse) == text


def test_the_reviews_message_reaches_no_model_raw():
    """The E2E probe: classify already knows the sender's username; with it
    and the label rules nothing personal is left."""
    text = (
        "Guangzhoudan 3 kub mato kerak. Telegram: namuna_tekstil, WeChat: "
        "rustam_cn88, Qozogʻiston nomer +7 701 000 00 00"
    )
    bio = "Namuna Tekstil Savdo MChJ direktori, wa: +7 701 000 00 09"
    told = pseudonymise(bio, handles=["namuna_tekstil", "5100000001"])
    said = pseudonymise(text, handles=["namuna_tekstil", "5100000001"], prior=told)
    for part in (told.text, said.text):
        assert p.unmasked(part) == []
        for raw in ("namuna_tekstil", "rustam_cn88", "701 000 00 00", "701 000 00 09"):
            assert raw not in part


def test_the_senders_own_handle_is_masked_wherever_it_is_written():
    result = pseudonymise(
        "namuna_tekstil ga yozing, NAMUNA_TEKSTIL yoki id 5100000001",
        handles=["@namuna_tekstil", "5100000001"],
    )
    assert result.text == "<TG_1> ga yozing, <TG_1> yoki id <PHONE_1>"
    # A username that is a business word is never masked as a handle.
    assert pseudonymise("optom narx", handles=["optom"]).text == "optom narx"
    assert pseudonymise("namuna_tekstilchi", handles=["namuna_tekstil"]).text == (
        "namuna_tekstilchi"
    )


def test_a_long_run_that_is_no_number_stays():
    # A 17-digit 2GIS firm id is longer than any number; a PINFL is <ID>.
    assert p.unmasked("2GIS: 70000001012345678") == []
    assert p.unmasked("JSHSHIR 31234567890123") == ["PINFL_RE"]
