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
    expected = re.sub(r"(?<!\d)\d{14}(?!\d)", "<ID>", text)  # never reversible
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
    result = pseudonymise(
        "Bekzodga ayting, Бекзоднинг ombori; Bekzodjon boshqa odam", names=["Bekzod"]
    )
    assert result.text == "<NAME_1>ga ayting, <NAME_1>нинг ombori; Bekzodjon boshqa odam"
    assert result.reverse == {"<NAME_1>": "Bekzod"}


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
    assert dict(p.GUARDED) == {
        "PHONE_RE": keywords.PHONE_RE,
        "TG_RE": keywords.TG_RE,
        "IG_RE": keywords.IG_RE,
        "EMAIL_RE": keywords.EMAIL_RE,
    }
    assert p.PHONE_RE is keywords.PHONE_RE and p.TG_RE is keywords.TG_RE
    assert p.IG_RE is keywords.IG_RE and p.EMAIL_RE is keywords.EMAIL_RE


def test_the_names_04_and_06_give_are_the_only_ones():
    assert {"Pseudonymised", "pseudonymise", "restore", "assert_masked"} <= set(p.__all__)
    for gone in ("Masked", "mask", "unmask"):
        assert not hasattr(p, gone)
    assert Pseudonymised("x").reverse == {}
