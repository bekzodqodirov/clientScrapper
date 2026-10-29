"""The owner bot's cards (06 §1.3, §1.7; 07 §4.1 T-20).

``services/cards`` renders a view into ``Card(text, keyboard, hash)``; these
are its goldens. The gate line is the gates' own words for gates 4–6 — a
card exists only because every gate passed, so a failing one (a push to the
company inside 90 days, to the phone inside 24 hours) is a refusal, never a
card. The handoff variant (``score_breakdown.gates.no_phone``) says «📨 Faqat
Telegram» and carries ``[👤 Menejerga]``; the manager card ends with the
owner's decision 2a line (01 §7). Line 1 is measured against a 360 px phone.

Synthetic data only (07 §1.0).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from clientscrapper.bot import keyboards
from clientscrapper.services import cards, gates
from clientscrapper.services.cards import CardRefused, ManagerView, ReviewView
from clientscrapper.services.suppression import Hit
from tests.fixtures.candidate_views import BREAKDOWN, sig, view
from tests.gate_fixtures import NOW

COMPANY = gates.GateCompany(
    id=uuid.UUID(int=2),
    display_name="Namuna Tekstil Savdo",
    legal_form="MChJ",
    stir="300000011",
)


def gate(recent: gates.Recent | None = None, hit: Hit | None = None) -> gates.GateResult:
    """The real gates over a passing company (04 §6)."""
    return gates.run_gates(COMPANY, (), (), None, hit, recent or gates.Recent(), NOW)


def review(**changes: object) -> ReviewView:
    base = ReviewView(
        review_id=5, candidate=view(), gate=gate(), segment="A", phone_kind="mobile"
    )
    return replace(base, **changes)  # type: ignore[arg-type]


OWNER_CARD = (
    "🆕 Nomzod · 74/100 · Bozor optom\n"
    "🏢 Namuna Tekstil Savdo (MChJ) · Toshkent · STIR 300000011 · faol\n"
    "📦 mato, aksessuar · Xitoy: ha\n"
    "📐 Hajm: «5 kub» — oʻzi yozgan\n"
    "📞 +998 90 000 00 61 (mobil) · @namuna_tekstil · IG namuna_tekstil · "
    "namuna-tekstil.example.uz\n"
    "🔎 «5 kub mato kerak» — Telegram guruh, 25.10.2026\n"
    "🔗 https://t.me/namuna_optom/101\n"
    "🧮 hajm 30 · Xitoy 20 · firma 8 · yangilik 10 · aloqa 6 · tasdiq 0\n"
    "✔️ mijoz emas · DNC yoʻq · 90 kunda yuborilmagan"
)


def test_the_owner_card_is_rendered_line_by_line():
    card = cards.render_review(review())
    assert card.text == OWNER_CARD
    assert card.keyboard == "review"
    assert card.hash == cards.card_hash(OWNER_CARD, "review")
    # The hash covers the keyboard too: a layout change re-edits the card.
    assert cards.card_hash(OWNER_CARD, "handoff") != card.hash


def test_the_gate_line_is_gates_4_to_6_in_their_own_words():
    lines = dict(zip(gates.GATES, gate().lines, strict=True))
    assert lines["not_recent"] == "90 kunda yuborilmagan ✓"
    last = cards.render_review(review()).text.splitlines()[-1]
    assert last == "✔️ mijoz emas · DNC yoʻq · 90 kunda yuborilmagan"


@pytest.mark.parametrize(
    "recent",
    [
        # Gate 6: an open push for the company within 90 days …
        gates.Recent(company_push_until=NOW + timedelta(days=89)),
        # … or for this phone within 24 hours.
        gates.Recent(phone_push_until=NOW + timedelta(hours=23)),
    ],
)
def test_a_card_whose_gate_6_fails_is_refused(recent):
    with pytest.raises(CardRefused) as refused:
        cards.render_review(review(gate=gate(recent)))
    assert refused.value.code == "gate"


def test_a_dnc_hit_refuses_the_card():
    hit = Hit(
        id=1, kind="dnc", key_type="phone_tail", key="900000061", source="owner_bot"
    )
    with pytest.raises(CardRefused) as refused:
        cards.render_review(review(gate=gate(hit=hit)))
    assert refused.value.code == "gate"


def test_the_breakdown_line_names_every_group_and_the_negatives():
    negative = {**BREAKDOWN, "negatives": [{"key": "neg_b2c_words", "points": -10}]}
    card = cards.render_review(review(candidate=view(breakdown=negative)))
    assert card.text.splitlines()[-2] == (
        "🧮 hajm 30 · Xitoy 20 · firma 8 · yangilik 10 · aloqa 6 · tasdiq 0 · minus 10"
    )


def test_the_quote_is_verbatim_from_the_evidence_and_cut_at_140():
    card = cards.render_review(review())
    assert "«5 kub mato kerak»" in card.text
    assert "5 kub mato kerak" in view().signals[0].excerpt
    long_text = "Yiwu dan 5 kub mato kerak " + "va boshqa tovarlar ham kerak " * 10
    long = sig(quote=long_text.strip(), excerpt=long_text)
    shown = cards.render_review(review(candidate=view(signals=(long,))))
    quote = next(x for x in shown.text.splitlines() if x.startswith("🔎"))
    inner = quote.split("«", 1)[1].split("»", 1)[0]
    assert len(inner) <= cards.QUOTE_MAX
    assert long_text.startswith(inner.rstrip("…").rstrip())


def test_a_quote_not_in_its_evidence_refuses_the_card():
    invented = sig(quote="10 kub kerak", excerpt="Salom, narxi qancha?")
    with pytest.raises(CardRefused) as refused:
        cards.render_review(review(candidate=view(signals=(invented,))))
    assert refused.value.code == "no_quote"


HANDOFF_CARD = OWNER_CARD.replace(
    "📞 +998 90 000 00 61 (mobil) · @namuna_tekstil · IG namuna_tekstil · "
    "namuna-tekstil.example.uz",
    "📨 Faqat Telegram: @namuna_tekstil",
)


def test_the_handoff_variant_for_a_no_phone_row():
    rv = review(
        candidate=view(phone=None), handoff=True, handoff_username="namuna_tekstil"
    )
    card = cards.render_review(rv)
    assert card.text == HANDOFF_CARD
    assert card.keyboard == "handoff"
    first_row = keyboards.review_keyboard(5, card.keyboard).inline_keyboard[0]
    assert [b.text for b in first_row] == ["👤 Menejerga", "✖️ Yoʻq"]
    assert [b.callback_data for b in first_row] == ["rv:5:h", "rv:5:d"]


def test_the_review_keyboard_rows():
    rows = keyboards.review_keyboard(5, "review").inline_keyboard
    assert [[b.text for b in row] for row in rows] == [
        ["✅ CRMʼga yubor", "✖️ Yoʻq"],
        ["🔀 Birlashtir", "🔒 DNC", "⏭ Keyinroq"],
        ["⋯ Boshqa"],
    ]
    more = keyboards.review_keyboard(5, "review", expanded=True).inline_keyboard
    assert [[b.text for b in row] for row in more[2:]] == [
        ["🙍 B2C", "🚚 Kargo/raqobatchi"],
        ["✏️ Telefon", "◀️ Orqaga"],
    ]


def test_a_review_row_with_no_phone_and_no_handoff_flag_is_refused():
    with pytest.raises(CardRefused) as refused:
        cards.render_review(review(candidate=view(phone=None)))
    assert refused.value.code == "no_phone"
    with pytest.raises(CardRefused) as nobody:
        cards.render_review(review(candidate=view(phone=None), handoff=True))
    assert nobody.value.code == "no_username"


def test_a_handoff_flag_with_a_phone_is_the_normal_card():
    card = cards.render_review(review(handoff=True, handoff_username="x_user"))
    assert card.keyboard == "review"
    assert card.text == OWNER_CARD


def test_a_shared_phone_adds_its_warning_line():
    lines = cards.render_review(review(shared_phone=True)).text.splitlines()
    assert lines[5] == "⚠️ Raqam 3+ firmada uchraydi"
    assert lines[4].startswith("📞 +998 90 000 00 61")


MANAGER_CARD = (
    "📨 Nomzod · 74/100 · Bozor optom\n"
    "🏢 Namuna Tekstil Savdo · Toshkent\n"
    "📦 mato, aksessuar · 📐 «5 kub»\n"
    "🔎 «5 kub mato kerak» — Optom bozor chat, 25.10.2026\n"
    "🔗 https://t.me/namuna_optom/101\n"
    "👤 Yozish: t.me/namuna_tekstil — oʻz akkauntingizdan, 10:00–17:00 (Du–Sha)\n"
    "✍️ Taklif: sotuv qoʻllanmasidagi «Bozor optom» shabloni (§3.1)\n"
    "⚠️ Birinchi xabarda narx yoʻq. Oxirgi jumla: «Agar qiziq boʻlmasa, "
    '"yoʻq" deb yozing — boshqa yozmayman.»\n'
    "🔍 Yozishdan oldin ERPda nomi boʻyicha qidiring"
)


def test_the_manager_card_carries_the_erp_first_line():
    mv = ManagerView(
        review_id=6,
        candidate=view(phone=None),
        username="namuna_tekstil",
        segment="A",
        chat_title="Optom bozor chat",
    )
    card = cards.render_manager(mv)
    assert card.text == MANAGER_CARD
    assert card.text.splitlines()[-1] == "🔍 Yozishdan oldin ERPda nomi boʻyicha qidiring"
    assert card.keyboard == "manager"
    rows = keyboards.review_keyboard(6, card.keyboard).inline_keyboard
    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [("📞 Bogʻlandim", "mh:6:c"), ("🙅 Yozmang dedi", "mh:6:x")],
        [("❌ Notoʻgʻri / B2C", "mh:6:w"), ("⏭ Keyinroq", "mh:6:l")],
    ]


def test_the_manager_card_uses_04s_opener_when_there_is_one():
    opener = "Assalomu alaykum! Xitoydan yuk olib kelishda yordam beramiz."
    mv = ManagerView(review_id=6, candidate=view(opener=opener), username="u_x")
    assert f"✍️ Taklif: {opener}" in cards.render_manager(mv).text


def test_values_from_the_database_are_escaped():
    rv = review(candidate=view(display_name="Namuna <b>&</b> Savdo", legal_form=None))
    line = cards.render_review(rv).text.splitlines()[1]
    assert line.startswith("🏢 Namuna &lt;b&gt;&amp;&lt;/b&gt; Savdo · Toshkent")


def test_a_company_card_names_the_suppression_that_holds_it():
    hit = Hit(
        id=3,
        kind="lost",
        key_type="phone_tail",
        key="900000061",
        source="outcomes",
        created_at=NOW,
    )
    card = cards.CompanyCard(
        company_id=uuid.UUID(int=2),
        display_name="Namuna Tekstil Savdo",
        legal_form="MChJ",
        city="Toshkent",
        stir="300000011",
        registry_status="active",
        website_domain=None,
        telegram="namuna_tekstil",
        instagram=None,
        state="suppressed",
        best_score=74,
        candidate_state="suppressed",
        candidate_id=uuid.UUID(int=1),
        last_push=None,
        hit=hit,
    )
    text = cards.render_company(card, state_words={"suppressed": "bostirilgan"})
    assert text.splitlines() == [
        "🏢 Namuna Tekstil Savdo (MChJ) · Toshkent · STIR 300000011 · faol",
        "🔗 @namuna_tekstil",
        "Holat: bostirilgan · eng yaxshi ball 74 · nomzod: bostirilgan",
        f"<code>{uuid.UUID(int=1)}</code>",
        "🚫 Bostirilgan: «Qayta qilinmasin» (outcomes, 27.10.2026)",
    ]


# --- line 1 on a 360 px phone (07 §3.1) ----------------------------------------------
#
# Roboto's advance widths (per 1000 em) at Telegram's 16 sp; an emoji is
# 1.2 em. A 360 px screen leaves a private chat's text bubble ~280 px once
# Telegram's margins and the bubble's padding are taken (an estimate: the
# real phone is 07 §3.1's manual check).
_ROBOTO = {
    **dict(
        zip(
            "abcdefghijklmnopqrstuvwxyz",
            (
                *(544, 561, 523, 564, 530, 347, 561, 551, 243, 239, 507, 243, 876),
                *(552, 570, 561, 568, 338, 516, 327, 551, 484, 751, 496, 473, 496),
            ),
            strict=True,
        )
    ),
    **dict(
        zip(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            (
                *(652, 623, 651, 656, 568, 553, 681, 713, 272, 552, 627, 538, 873),
                *(713, 688, 631, 688, 616, 593, 597, 648, 636, 887, 627, 601, 599),
            ),
            strict=True,
        )
    ),
    **dict.fromkeys("0123456789", 561),
    " ": 248,
    "·": 263,
    "/": 412,
    "ʻ": 200,
}
BUBBLE_PX = 280


def width_px(text: str, sp: float = 16) -> float:
    units = sum(_ROBOTO.get(ch, 1200 if ord(ch) > 0x2000 else 600) for ch in text)
    return units / 1000 * sp


def test_the_width_model_is_not_vacuous():
    assert width_px("🆕 Nomzod · 100/100 · Bozor optomchisi") > BUBBLE_PX
    assert width_px("📨 Telegram orqali nomzod · 74/100 · Fabrika") > BUBBLE_PX


@pytest.mark.parametrize("segment", [*cards.SEGMENT_UZ, None])
def test_line_one_fits_a_360px_phone_at_the_widest_score(segment):
    full = view(score=100)
    owner = cards.render_review(review(candidate=full, segment=segment))
    manager = cards.render_manager(
        ManagerView(review_id=6, candidate=full, username="u_x", segment=segment)
    )
    for card in (owner, manager):
        first = card.text.splitlines()[0]
        assert width_px(first) <= BUBBLE_PX, (first, width_px(first))


def test_a_card_stays_under_3500_characters_at_its_longest():
    long_goods = tuple(f"tovar nomi {n} juda uzun nom bilan" for n in range(20))
    big = view(goods=long_goods, display_name="Namuna " * 40, stated_m3=Decimal(999))
    assert len(cards.render_review(review(candidate=big)).text) <= cards.CARD_MAX
