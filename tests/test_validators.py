"""The validators (04 §4.7): the quote, volume sanity, the tout downgrade,
goods, output hygiene — pure, over SYNTHETIC answers
(tests/fixtures/model/README.md).

A quote that is not in the evidence leaves the result with no quote and a
basis that claims nothing (``inferred``): ``services/signals.from_model``
(T-18) writes ``vol_stated_*`` and ``vol_container`` only from a verified
quote, so such an answer yields no volume signal; and the model's origin
never writes ``cn_origin`` (only the text's own China word does).
"""

from __future__ import annotations

import math

import pytest

from clientscrapper.services import validators as v
from clientscrapper.services.schemas import ImportSignal
from tests.fakes import model_fixture

ASK = "Guangzhoudan 2 kub mato kerak, kim olib keladi?"


def signal(name: str, **changes) -> ImportSignal:
    return ImportSignal.model_validate({**model_fixture(name), **changes})


# --- the quote ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "excerpt", "kept"),
    [
        ("2 kub mato", ASK, "2 kub mato"),
        # Case, spacing and apostrophes fold; what is kept is the excerpt's.
        ("2 KUB  Mato", ASK, "2 kub mato"),
        ("o'yinchoq 5 kub", "Oʻyinchoq 5 kub kerak", "Oʻyinchoq 5 kub"),
        ("5 куб ўйинчоқ", "Ассалому алайкум, 5 куб ўйинчоқ керак", "5 куб ўйинчоқ"),
        # NFKC: full-width digits are the same digits.
        ("２ kub", "Bizga ２ kub kerak", "２ kub"),
        ("kub", ASK, "kub"),
    ],
)
def test_a_quote_in_the_excerpt_is_kept_as_the_excerpt_writes_it(quote, excerpt, kept):
    assert v.verify_quote(quote, excerpt=excerpt, reverse={}) == kept


@pytest.mark.parametrize(
    "quote",
    [
        "3 kub mato",  # a number the text does not say
        "2 кубометра ткани",  # a translation
        "ikki kub mato",  # a paraphrase
        "kb",  # under 3 characters
        "2 kub mato " + "x" * 200,  # over 200
        "   ",
        "<PHONE_1> 2 kub",  # a token the map does not know stays a token
    ],
)
def test_a_quote_not_in_the_excerpt_is_no_quote(quote):
    assert v.verify_quote(quote, excerpt=ASK, reverse={}) is None


def test_a_quote_has_three_to_two_hundred_characters():
    """The bounds themselves, on fragments the excerpt does hold."""
    excerpt = "Guangzhoudan " + "mato " * 60 + "kerak"
    assert v.verify_quote("ub", excerpt="2 kub", reverse={}) is None
    assert v.verify_quote("kub", excerpt="2 kub", reverse={}) == "kub"
    fits = excerpt[:200]
    assert len(fits) == 200 and v.verify_quote(fits, excerpt=excerpt, reverse={}) == fits
    over = excerpt[:201]
    assert v.verify_quote(over, excerpt=excerpt, reverse={}) is None


def test_the_quote_is_restored_before_it_is_looked_for():
    excerpt = "Rustamga 3 kub mebel kerak, Xitoydan"
    quote = model_fixture("classify_quote_with_token.json")["volume_quote"]
    assert v.verify_quote(quote, excerpt=excerpt, reverse={"<NAME_1>": "Rustam"}) == (
        "Rustamga 3 kub mebel"
    )
    assert v.verify_quote(quote, excerpt=excerpt, reverse={}) is None
    assert v.verify_quote(None, excerpt=excerpt, reverse={}) is None


def test_a_quote_that_fails_leaves_no_quote_and_no_claimed_basis():
    answer = signal("classify_buyer_stated.json", volume_quote="2 kubometr mato")
    checked = v.import_signal(answer, excerpt=ASK, text=ASK, reverse={})
    assert checked.quote is None
    assert checked.result["volume_quote"] is None
    # «volume_m3_est without a verified quote is treated as inferred».
    assert (checked.result["volume_basis"], checked.result["volume_m3_est"]) == (
        "inferred",
        2.0,
    )
    container = signal("classify_container.json", volume_quote="bir konteyner")
    excerpt = "40 fut konteyner kerak"
    held = v.import_signal(container, excerpt=excerpt, text=excerpt, reverse={})
    assert (held.quote, held.result["volume_basis"], held.result["volume_m3_est"]) == (
        None,
        "inferred",
        60.0,
    )


def test_a_failed_quote_with_no_estimate_claims_nothing():
    answer = signal(
        "classify_buyer_stated.json", volume_quote="5 kub", volume_m3_est=None
    )
    checked = v.import_signal(answer, excerpt=ASK, text=ASK, reverse={})
    assert (checked.result["volume_basis"], checked.result["volume_m3_est"]) == (
        "none",
        None,
    )


def test_the_worked_example_passes_whole():
    checked = v.import_signal(
        signal("classify_buyer_stated.json"), excerpt=ASK, text=ASK, reverse={}
    )
    assert checked.quote == "2 kub mato"
    assert checked.leaks == ()
    assert checked.result == {
        "speaker_role": "buyer",
        "b2b": True,
        "b2c_markers": False,
        "goods": ["mato"],
        "goods_category": "textile",
        "origin_cn": "explicit",
        "volume_m3_est": 2.0,
        "volume_basis": "stated",
        "volume_quote": "2 kub mato",
        "frequency": "unknown",
        "confidence": 0.86,
    }


# --- volume sanity ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "basis", "kept"),
    [
        (0, "inferred", None),
        (-3, "inferred", None),
        (0.5, "inferred", 0.5),
        (200, "inferred", 200.0),
        (200.5, "inferred", None),
        (math.nan, "inferred", None),
        (math.inf, "inferred", None),
        (True, "inferred", None),
        (None, "inferred", None),
        (27.9, "container", None),
        (28, "container", 28.0),
        (70, "container", 70.0),
        (70.1, "container", None),
        (39.9, "truck", None),
        (40, "truck", 40.0),
        (95, "truck", 95.0),
        (95.1, "truck", None),
        (12, "weight", 12.0),
    ],
)
def test_the_volume_bounds(value, basis, kept):
    assert v.sane_volume(value, basis, None) == kept


@pytest.mark.parametrize(
    ("value", "quote", "kept"),
    [
        (25, "25 kub", 25.0),
        (27.5, "25 kub", 27.5),  # |27.5 − 25| = 2.5: within 10 %
        (27.6, "25 kub", 25.0),  # outside: the regex wins
        (22.4, "25 kub", 25.0),
        (None, "25 kub", 25.0),
        (10, "2 kub mato", 2.0),
        (3, "2,5 kub", 2.5),
        (2, "5 куб ўйинчоқ", 5.0),
        # The text says it: capped at 200, not dropped (every signal reads ≥ 3
        # the same; a 250 m³ ask must not score below a 5 m³ one).
        (300, "300 kub", 200.0),
        (250, "Xitoydan 250 kub kafel", 200.0),
        (40, "yarim fura", 40.0),  # no number the regex reads: the estimate
    ],
)
def test_a_stated_volume_agrees_with_its_quote(value, quote, kept):
    assert v.sane_volume(value, "stated", quote) == kept


@pytest.mark.parametrize(
    ("quote", "count"),
    [
        ("2 ta 40 fut konteyner", 2),
        ("3 фуры", 3),
        ("2 fura", 2),
        ("10 ta fura", 10),
        ("40ft konteyner", 1),  # a size is no count
        ("20 fut konteyner", 1),
        ("bir konteyner", 1),
        ("yarim fura", 1),
        ("5 tonna yuk", 1),  # tonnes are no unit
        ("99 ta fura", 1),  # no count the rule reads (1–19)
        (None, 1),
    ],
)
def test_the_count_of_units_a_quote_names(quote, count):
    assert v.unit_count(quote) == count


@pytest.mark.parametrize(
    ("value", "basis", "quote", "kept"),
    [
        # 04 §4.7's bounds are one unit's; a quote's count scales them.
        (120, "container", "2 ta 40 fut konteyner", 120.0),
        (140, "container", "2 ta 40 fut konteyner", 140.0),
        (141, "container", "2 ta 40 fut konteyner", None),
        (55, "container", "2 ta 40 fut konteyner", None),
        (160, "truck", "2 fura", 160.0),
        (240, "truck", "3 фуры", 200.0),  # in 120–285, capped at 200
        (70, "container", "40ft konteyner", 70.0),
        (120, "container", "40ft konteyner", None),
        # An estimate over 200 is no volume, a quote or not.
        (250, "inferred", None, None),
        (250, "weight", None, None),
        (250, "weight", "50 tonna", 200.0),
    ],
)
def test_the_bounds_follow_the_count_and_a_quoted_volume_is_capped(
    value, basis, quote, kept
):
    assert v.sane_volume(value, basis, quote) == kept


@pytest.mark.parametrize(
    ("changes", "excerpt", "result"),
    [
        (
            {"volume_m3_est": 250, "volume_quote": "250 kub"},
            "Xitoydan 250 kub kafel kerak",
            (200.0, "stated", "250 kub"),
        ),
        (
            {
                "volume_m3_est": 120,
                "volume_basis": "container",
                "volume_quote": "2 ta 40 fut konteyner",
            },
            "Xitoydan 2 ta 40 fut konteyner kerak",
            (120.0, "container", "2 ta 40 fut konteyner"),
        ),
        (
            # A quoted unit with an implausible number keeps the unit.
            {"volume_m3_est": 10, "volume_basis": "truck", "volume_quote": "2 fura"},
            "Xitoydan 2 fura kerak",
            (None, "truck", "2 fura"),
        ),
        (
            # A basis that is a number keeps none it lost: no «stated» with
            # no volume, and no quote beside «none».
            {"volume_m3_est": 0, "volume_quote": "0 kub"},
            "Hozircha 0 kub, keyin aytaman",
            (None, "none", None),
        ),
    ],
)
def test_the_basis_follows_the_volume(changes, excerpt, result):
    answer = signal("classify_buyer_stated.json", **changes)
    checked = v.import_signal(answer, excerpt=excerpt, text=excerpt, reverse={})
    got = checked.result
    assert (got["volume_m3_est"], got["volume_basis"], got["volume_quote"]) == result
    assert checked.quote == result[2]


def test_a_bound_the_grammar_cannot_hold_is_applied_not_refused():
    """transform_schema moves maxItems/minimum/maximum into the description:
    seven goods and a confidence of 1.2 are answers (cut, clamped)."""
    answer = signal(
        "classify_buyer_stated.json", goods=[f"tovar{chr(97 + i)}" for i in range(7)]
    )
    assert len(answer.goods) == 6
    for raw, kept in ((1.2, 1.0), (-0.1, 0.0), (0.5, 0.5), (math.inf, 0.0)):
        assert signal("classify_buyer_stated.json", confidence=raw).confidence == kept
    with pytest.raises(ValueError):
        signal("classify_buyer_stated.json", confidence="high")


# --- the tout downgrade --------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "text", "confidence", "kept"),
    [
        ("tout", "Xitoydan mebel kerak, 5 kub", 0.79, "unknown"),
        ("tout", "Xitoydan mebel kerak, 5 kub", 0.8, "tout"),
        ("tout", "Kargo xizmati: Xitoydan yuk olib kelamiz", 0.3, "tout"),
        ("tout", "Доставка из Китая, недорого", 0.5, "tout"),
        # A buyer's ask names the service, it does not offer it (04 §6): the
        # tout filter's text rule, keywords.cargo_offer, decides here too.
        ("tout", "Guangzhoudan 2 kub mato bor, kim kargo qiladi?", 0.5, "unknown"),
        ("tout", "Kargo Xitoy-Toshkent: 1 kub 250$, 3 kub kerakmi?", 0.5, "tout"),
        ("buyer", "Kargo kerak", 0.2, "buyer"),
        ("seller", "Optom narx", 0.1, "seller"),
    ],
)
def test_a_weak_tout_without_a_competitor_word_is_unknown(role, text, confidence, kept):
    assert v.role_consistent(role, text=text, confidence=confidence) == kept


def test_the_tout_downgrade_reaches_the_result():
    text = "Xitoydan mebel kerak, narxi qancha?"
    checked = v.import_signal(
        signal("classify_tout_weak.json"), excerpt=text, text=text, reverse={}
    )
    assert checked.result["speaker_role"] == "unknown"


# --- goods -----------------------------------------------------------------------


def test_goods_are_cleaned():
    assert v.clean_goods(
        [
            "Mato",
            "mato",
            "  Mebel   furniturasi ",
            "kir yuvish mashinasi uchun",  # four words
            "iphone 15",  # a digit
            "<NAME_1>",  # a token
            "a" * 31,
            "o'yinchoq",
            "ЎЙИНЧОҚ",
            "Ткань",
            "中文 mato",  # a script that is neither
            "",
        ]
    ) == ["mato", "mebel furniturasi", "oʻyinchoq", "ўйинчоқ", "ткань"]


def test_at_most_six_goods_are_kept():
    kept = v.clean_goods([f"tovar{chr(97 + i)}" for i in range(9)])
    assert kept == ["tovara", "tovarb", "tovarc", "tovard", "tovare", "tovarf"]


def test_the_apostrophes_of_uzbek_goods():
    assert v.clean_goods(["g'isht", "qurilish ma'lumotnomasi", "O`yinchoq"]) == [
        "gʻisht",
        "qurilish maʼlumotnomasi",
        "oʻyinchoq",
    ]


# --- output hygiene ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("+998900000001", True),
        ("tel +998900000001 orqali", True),
        ("@namuna_tekstil", True),
        ("31234567890123", True),
        ("<PHONE_1>", False),
        ("@<TG_1>", False),
        ("+998 90 000 00 01", True),  # PHONE_RE's phone, beyond 04's compact form
        ("90 000-00-01", True),
        ("t.me/namuna_optom", True),
        ("info@namuna.example.uz", True),
        ("Instagram sahifamiz", False),  # IG_RE's label branch is left out
        ("25 kub mato", False),
        ("312345678901234", False),  # fifteen digits are no PINFL
        ("@abcd", False),
        (None, False),
        ("", False),
    ],
)
def test_what_hygiene_calls_a_leak(text, leak):
    assert v.leaks(text) is leak


def test_a_leaking_field_is_dropped_and_named():
    answer = signal(
        "classify_buyer_stated.json",
        goods=["mato", "+998900000001", "@namuna_tekstil"],
        volume_quote="2 kub mato 31234567890123",
    )
    checked = v.import_signal(answer, excerpt=ASK, text=ASK, reverse={})
    assert checked.leaks == ("goods", "volume_quote")
    assert checked.result["goods"] == ["mato"]
    assert checked.quote is None and checked.result["volume_quote"] is None
    assert checked.result["volume_basis"] == "inferred"


def test_the_container_answer_whole():
    excerpt = "Xitoydan 40 futlik konteyner kafel va santexnika olib kelish kerak"
    checked = v.import_signal(
        signal("classify_container.json"), excerpt=excerpt, text=excerpt, reverse={}
    )
    assert checked.quote == "40 futlik konteyner"
    assert (
        checked.result["goods"],
        checked.result["volume_basis"],
        checked.result["volume_m3_est"],
    ) == (["kafel", "santexnika"], "container", 60.0)
