"""The redline gate and the playbook templates (04 §4.5; 07 §4.1 T-30).

``redlines.check_opener`` decides whether 04's draft may stand on the card
and in the ERP note. The corpus (``tests/fixtures/uz_corpus``) pins it: every
bad draft raises exactly the flags it is listed with, every good one passes.
The playbook templates A–E — the runtime copy in ``services/playbook`` and
the reference copy in the corpus, kept equal — pass the same gate filled
with any source and any goods, and the playbook's own messages (with their
day counts, digits and tariff words) do not. A flagged draft is never
rendered: ``tests/test_verdict.py`` shows the template taking its place on
the card.

Synthetic data only (07 §1.0).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clientscrapper.services import cards, playbook, prompts, redlines, validators
from clientscrapper.services.schemas import DraftOpener
from tests.fixtures import uz_corpus
from tests.test_uz_text import problems

GOODS_SAMPLES = (
    ("mato", "furnitura"),
    (),
    ("soat", "kafel"),  # «soat» is the time word: never filled in
    ("мебель", "oʻyinchoq"),  # Cyrillic goods are transliterated
    ("100 dona", "narxi arzon", "kafolatli mato", "chinni"),
)


def check(text: str, lang: str) -> redlines.GuardResult:
    return redlines.check_opener(
        text,
        lang=lang,  # type: ignore[arg-type]
        hooks=uz_corpus.HOOKS,
        competitors=uz_corpus.COMPETITORS,
    )


# --- the corpus -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "lang", "flags"), uz_corpus.REDLINE_BAD, ids=lambda x: str(x)[:40]
)
def test_a_bad_draft_raises_exactly_its_flags(text, lang, flags):
    found = check(text, lang)
    assert not found.ok
    assert set(found.flags) == flags


@pytest.mark.parametrize(
    ("text", "lang"), uz_corpus.REDLINE_GOOD, ids=lambda x: str(x)[:40]
)
def test_a_good_draft_passes(text, lang):
    assert check(text, lang) == redlines.GuardResult(ok=True, flags=())


def test_the_corpus_is_large_enough_and_covers_every_flag():
    assert len(uz_corpus.REDLINE_BAD) >= 30
    assert len(uz_corpus.REDLINE_GOOD) >= 10
    raised = set().union(*(flags for _, _, flags in uz_corpus.REDLINE_BAD))
    assert raised == set(redlines.FLAGS)
    # Uzbek Latin, Uzbek Cyrillic and Russian among the good ones.
    scripts = {redlines.script_of(text) for text, _ in uz_corpus.REDLINE_GOOD}
    assert scripts == {"latin", "cyrillic", "russian"}


def test_the_flags_come_in_their_order():
    text = uz_corpus.REDLINE_BAD[0][0].replace("150", "150 kun")
    assert check(text, "uz").flags == ("digit", "money", "time")


def test_only_the_allowed_token_is_a_digit_the_gate_lets_through():
    base = uz_corpus.REDLINE_GOOD[0][0]
    assert "2018" in base and check(base, "uz").ok
    assert check(base.replace("2018", "2019"), "uz").flags == ("digit",)
    # «20185» is not «2018».
    assert "digit" in check(base.replace("2018", "20185"), "uz").flags
    no_token = redlines.check_opener(base, lang="uz", allowed_tokens=())
    assert no_token.flags == ("digit",)


def test_our_own_name_is_no_competitor_but_a_forwarder_is():
    base = uz_corpus.REDLINE_GOOD[0][0]
    assert "GSR Logistics" in base and check(base, "uz").ok
    named = base.replace("GSR Logistics", "Namuna Logistics")
    assert check(named, "uz").flags == ("competitor",)
    # A seed's name, given by the caller: only then.
    seeded = base.replace("OLXda", "OLXda (MK Express emas)")
    assert redlines.check_opener(seeded, lang="uz", hooks=uz_corpus.HOOKS).ok
    assert check(seeded, "uz").flags == ("competitor",)


def test_the_opt_out_reads_in_any_spelling_but_only_at_the_end():
    head = uz_corpus.REDLINE_GOOD[1][0].rsplit("Agar", 1)[0]
    for ending in (
        uz_corpus.OPTOUT_UZ,
        "Agar qiziq bo'lmasa, \"yo'q\" deb yozing - boshqa yozmayman",
        "Agar qiziq boʼlmasa, “yoʼq” deb yozing – boshqa yozmayman.",
        "AGAR QIZIQ BOʻLMASA, «YOʻQ» DEB YOZING — BOSHQA YOZMAYMAN!",
        uz_corpus.OPTOUT_UZ_CYRL,
    ):
        assert redlines.ends_with_optout(head + ending, "uz"), ending
    assert not redlines.ends_with_optout(head + uz_corpus.OPTOUT_UZ + " Rahmat.", "uz")
    assert not redlines.ends_with_optout(head + uz_corpus.OPTOUT_RU, "uz")
    assert redlines.ends_with_optout(head + uz_corpus.OPTOUT_RU, "ru")
    # 04 §4.5's other wording is not the playbook's line.
    other = "Agar qiziq boʻlmasa, bir soʻz yozing — boshqa bezovta qilmayman."
    assert not redlines.ends_with_optout(head + other, "uz")


def test_the_opt_out_has_one_home():
    assert redlines.OPTOUT_UZ == uz_corpus.OPTOUT_UZ
    assert redlines.OPTOUT_UZ_CYRL == uz_corpus.OPTOUT_UZ_CYRL
    assert redlines.OPTOUT_RU == uz_corpus.OPTOUT_RU
    # The manager card's last-sentence line, and the draft prompt.
    assert f"«{redlines.OPTOUT_UZ}»" in cards.NO_PRICE_UZ
    assert redlines.OPTOUT_UZ in prompts.DRAFT_SYSTEM
    assert redlines.OPTOUT_RU in prompts.DRAFT_SYSTEM


def test_the_opt_out_and_the_uzbek_templates_are_spelled_with_okina():
    """04 Q9 (a): respelled — 07 §1.11's lint passes them."""
    assert problems(redlines.OPTOUT_UZ, rendered=True) == set()
    assert "'" not in redlines.OPTOUT_UZ
    for text in uz_corpus.PLAYBOOK_TEMPLATES.values():
        assert "'" not in text
        assert problems(text) == set()


def test_the_language_flag_reads_the_script_and_english():
    ru_text = uz_corpus.REDLINE_GOOD[5][0]
    assert check(ru_text, "ru").ok
    # The Russian draft offered as Uzbek: its script is not Uzbek's.
    assert "language" in check(ru_text, "uz").flags
    assert redlines.script_of("Ойига неча куб оласиз?") == "cyrillic"
    assert redlines.script_of("Ўзимиз оламиз") == "cyrillic"
    assert redlines.script_of("Сколько кубов?") == "russian"
    assert redlines.script_of("Oyiga necha kub?") == "latin"
    assert redlines.script_of("2018 —") is None
    # Yiwu, GSR Logistics and the like are names, not English.
    assert redlines.language_ok("Men GSR Logistics, Yiwu va Guangzhou.", "uz")
    assert not redlines.language_ok("We will ship it with the best price.", "uz")


def test_trips_is_the_gate_on_one_value():
    assert redlines.trips("soat")
    assert redlines.trips("100 dona")
    assert redlines.trips("narxi arzon")
    assert redlines.trips("Namuna Kargo")
    assert not redlines.trips("mato")
    assert not redlines.trips("oʻyinchoq")


# --- the templates --------------------------------------------------------------------


def test_the_runtime_templates_are_the_reference_copy():
    assert {k: v for (k, lang), v in playbook.TEMPLATES.items() if lang == "uz"} == (
        uz_corpus.PLAYBOOK_TEMPLATES
    )
    assert {k: v for (k, lang), v in playbook.TEMPLATES.items() if lang == "ru"} == (
        uz_corpus.PLAYBOOK_TEMPLATES_RU
    )
    assert set(uz_corpus.PLAYBOOK_TEMPLATES) == set(playbook.SEGMENTS) == set("ABCDE")


PLAYBOOK_DOC = Path(__file__).resolve().parents[1] / "docs" / "sotuv-playbook.md"


def test_the_sales_playbook_prints_the_code_templates_and_opt_out_lines():
    """docs/sotuv-playbook.md §3.0 and §2.6 show managers what the card
    suggests: the code's own text, so a change to one is a change to both."""
    doc = PLAYBOOK_DOC.read_text(encoding="utf-8")
    blocks = doc.split("```")[1::2]
    for (segment, lang), template in playbook.TEMPLATES.items():
        assert f"\n{template}\n" in blocks, (segment, lang)
    opt_out = doc.split("### 2.6 ", 1)[1].split("\n### ", 1)[0]
    assert f"- UZ Latin: `{redlines.OPTOUT_UZ}`" in opt_out
    assert f"- UZ Cyrillic: `{redlines.OPTOUT_UZ_CYRL}`" in opt_out
    assert f"- RU: `{redlines.OPTOUT_RU}`" in opt_out
    assert "**No price before density.**" in doc


@pytest.mark.parametrize("segment", [*playbook.SEGMENTS, "X", "Z", None])
@pytest.mark.parametrize("lang", ["uz", "ru"])
def test_every_template_filled_passes_the_gate(segment, lang):
    families = [*playbook.SOURCE_UZ, None]
    for family in families:
        for person in (True, False):
            for goods in GOODS_SAMPLES:
                text = playbook.fill(
                    segment, lang, goods=goods, family=family, person=person
                )
                kept = playbook.goods_words(goods, lang)
                found = redlines.check_opener(
                    text,
                    lang=lang,
                    hooks=playbook.hooks(kept, family),
                    competitors=uz_corpus.COMPETITORS,
                )
                assert found.ok, (segment, lang, family, goods, found.flags)
                low, high = validators.OPENER_CHARS
                assert low <= len(text) <= high, (segment, lang, family, len(text))
                assert "{" not in text and "}" not in text
                if lang == "uz":
                    assert problems(text, rendered=True) == set()


def test_a_value_that_would_trip_the_gate_is_left_out_of_the_fill():
    text = playbook.fill(
        "A", "uz", goods=("soat", "mato", "100 dona"), family="olx", person=False
    )
    assert "OLXda mato optom" in text
    assert "soat" not in text and "100" not in text
    bare = playbook.fill("A", "uz", goods=("soat",), family="olx", person=True)
    assert "OLXda tovar optom" in bare and "alaykum, [Ism]." in bare
    cyrillic = playbook.fill("D", "uz", goods=("мебель",), family="olx", person=False)
    assert "mebel boʻyicha" in cyrillic


def test_x_and_z_take_the_default_template():
    for segment in ("X", "Z", None, "Q"):
        assert playbook.template_segment(segment) == playbook.DEFAULT_SEGMENT == "A"
    assert playbook.skeleton("Z", "uz") == playbook.skeleton("A", "uz")
    assert "[manba]da [tovar] optom" in playbook.skeleton("A", "uz")
    assert "«[товар]»" in playbook.skeleton("A", "ru")


def test_the_playbooks_own_messages_trip_the_gate():
    """docs/sotuv-playbook.md §3.1 as written: day counts, digits, tariff and
    customs words — what the runtime templates leave out."""
    for segment, (text, flags) in uz_corpus.PLAYBOOK_RAW.items():
        found = redlines.check_opener(text, lang="uz")
        assert set(found.flags) == flags, segment


def test_the_template_language_follows_the_evidence():
    assert playbook.lang_of("Нужно 5 кубов ткани, кто привезёт?") == "ru"
    assert playbook.lang_of("Гуанчжоудан 2 куб мато керак") == "uz"
    assert playbook.lang_of("Guangzhoudan 2 kub mato kerak") == "uz"
    assert playbook.lang_of(None) == "uz"


def test_the_template_opener_is_the_fill():
    made = playbook.opener("C", "ru", goods=("mato",), family="telegram", person=True)
    assert made == playbook.Opener(
        text=playbook.fill("C", "ru", goods=("mato",), family="telegram", person=True),
        lang="ru",
        segment="C",
    )
    assert made.text.startswith("Здравствуйте, [Ism]. Я [Men], GSR Logistics (Ташкент).")
    assert "Увидел в Telegram-группе ваше предприятие: «mato»." in made.text


# --- the draft validator --------------------------------------------------------------


def draft(text: str, *, lang: str = "uz", hook: str = "OLX, mato") -> DraftOpener:
    return DraftOpener(lang=lang, text=text, hook=hook)  # type: ignore[arg-type]


def validate(answer: DraftOpener, lang: str = "uz") -> validators.Validated:
    return validators.draft_opener(
        answer,
        lang=lang,  # type: ignore[arg-type]
        hooks=uz_corpus.HOOKS,
        competitors=uz_corpus.COMPETITORS,
    )


def test_a_clean_draft_stands_with_no_flags():
    text = uz_corpus.REDLINE_GOOD[0][0]
    checked = validate(draft(text))
    assert checked.result == {
        "lang": "uz",
        "text": text,
        "hook": "OLX, mato",
        "flags": [],
    }
    assert checked.leaks == ()


def test_every_bad_draft_is_refused_by_the_validator_too():
    for text, lang, flags in uz_corpus.REDLINE_BAD:
        checked = validate(draft(text, lang=lang), lang)
        if checked.leaks:
            # A handle is personal data first: the text is dropped (hygiene).
            assert checked.result == {**checked.result, "text": None}
            assert checked.result["flags"] == ["pii_leak"]
            continue
        assert flags <= set(checked.result["flags"]), text


def test_a_name_token_becomes_the_playbook_placeholder():
    text = uz_corpus.REDLINE_GOOD[0][0].replace(
        "Assalomu alaykum.", "Assalomu alaykum, <NAME_1>."
    )
    checked = validate(draft(text))
    assert checked.result["flags"] == []
    assert checked.result["text"].startswith("Assalomu alaykum, [Ism]. Men [Men]")
    assert "<NAME_" not in checked.result["text"]


def test_a_draft_carrying_a_phone_is_dropped_and_flagged():
    text = uz_corpus.REDLINE_GOOD[0][0].replace("OLXda", "+998900000061 OLXda")
    checked = validate(draft(text))
    assert checked.result["text"] is None
    assert checked.result["flags"] == ["pii_leak"]
    assert checked.leaks == ("text",)


def test_a_draft_out_of_its_length_or_language_is_flagged():
    short = uz_corpus._HEAD + uz_corpus.OPTOUT_UZ
    assert len(short) < validators.OPENER_CHARS[0]
    assert validate(draft(short)).result["flags"] == ["length"]
    long = uz_corpus._HEAD + "Yiwu va Guangzhou. " * 30 + uz_corpus.OPTOUT_UZ
    assert validate(draft(long)).result["flags"] == ["length"]
    good = uz_corpus.REDLINE_GOOD[0][0]
    assert validate(draft(good, lang="ru")).result["flags"] == ["language"]
