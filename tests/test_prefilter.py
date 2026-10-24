"""The reader's prefilter (03 §2.3; 07 T-13): which messages are stored.

The 25 synthetic messages of ``tests/fixtures/telegram/prefilter_cases.json``
— Latin Uzbek, Cyrillic Uzbek and Russian, in groups, channels and a private
chat — each with the exact ``prefilter_hit`` the reader stores, ``[]`` for a
message that only counts. The rules are 03's: a group keeps a volume, a
container or an ask of at least 12 characters; a channel keeps stock news; a
private chat is never read. Pure; no database.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clientscrapper.collectors import keywords as kw
from clientscrapper.userbot import prefilter

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "telegram" / "prefilter_cases.json").read_text(
        encoding="utf-8"
    )
)


def test_the_fixture_covers_three_scripts_and_three_chat_types():
    assert len(CASES) >= 20
    assert {c["script"] for c in CASES} == {"uz-Latn", "uz-Cyrl", "ru"}
    assert {c["chat_type"] for c in CASES} == {"group", "channel", "private"}
    # Both verdicts in every script, so the cases below prove each script in
    # both directions: a prefilter that stored everything, or nothing, in one
    # of them could not pass.
    for script in ("uz-Latn", "uz-Cyrl", "ru"):
        verdicts = {bool(c["hits"]) for c in CASES if c["script"] == script}
        assert verdicts == {True, False}, script


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["text"][:40])
def test_each_synthetic_message_gets_its_verdict(case):
    got = prefilter.hits(case["text"], chat_type=case["chat_type"])
    assert list(got) == case["hits"]


def test_03_day_one_cyrillic_asks_are_caught():
    # 03 §2.3 Day 1: «verify the prefilter catches Cyrillic Uzbek («ким олиб
    # келади», «керак»)».
    assert prefilter.hits("Ким олиб келади Иудан юкни?", chat_type="group") == ("intent",)
    assert prefilter.hits("Бизга 2 куб мато керак", chat_type="group") == (
        "volume_kub",
        "intent",
    )


def test_the_twelve_character_floor_is_for_groups():
    assert prefilter.hits("3 kub kerak", chat_type="group") == ()  # 11 characters
    assert prefilter.hits("3 kub kerak!", chat_type="group") == ("volume_kub", "intent")
    # A channel's short stock post is kept.
    assert prefilter.hits("Yuk keldi", chat_type="channel") == ("wholesale",)


def test_china_alone_never_stores_a_message():
    assert prefilter.hits("Yiwu va Guangzhou haqida yangiliklar", chat_type="group") == ()
    assert prefilter.hits("Xitoydan salom!", chat_type="channel") == ()


def test_a_long_message_is_a_price_list_only_beside_another_hit():
    long_tail = " ".join(["matn"] * 400)  # > 1,500 characters, no hit
    assert len(long_tail) > kw.PRICELIST_MIN_CHARS
    assert prefilter.hits(long_tail, chat_type="group") == ()
    assert prefilter.hits("2 kub kerak. " + long_tail, chat_type="group") == (
        "volume_kub",
        "intent",
        "pricelist",
    )


def test_empty_and_missing_text_are_never_stored():
    assert prefilter.hits(None, chat_type="group") == ()
    assert prefilter.hits("   ", chat_type="channel") == ()


@pytest.mark.parametrize(
    ("text", "prices"),
    [
        ("futbolka 25000 so'm, shim 45000 soʻm, kurtka 120000 som", 3),
        ("Цена 150 000 сум, доставка 25$", 2),
        ("Нархи 45000 сўм ва 30 у.е.", 2),
        ("40 usd yoki 30 y.e.", 2),
        ("сумка 25 сумка", 0),
        ("25000 sumka", 0),
    ],
)
def test_price_re_counts_prices_not_bags(text, prices):
    assert kw.price_count(text) == prices
