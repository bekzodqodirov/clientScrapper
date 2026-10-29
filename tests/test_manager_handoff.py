"""The manager handoff — Telegram-only prospects (06 §1.7; 07 §4.1 T-21).

A hot candidate without a phone reaches the owner as the handoff card
(``[👤 Menejerga]``); the owner's 👤 puts it in ``hold`` for 30 days and
hands a ``lead_reviews(kind='manager')`` card to the manager with the
fewest such cards today (ties: list order). The manager writes from their
own account; ``/tel <karta> +998…`` stores the phone the prospect gave —
person-tier, ``legal_basis='pre_contract_request'``, ``source_url`` the
cited message, ``collected_at`` now — and re-scores at once: ``hold →
scored → approved(approved_by='manager:<tg_id>')``. «🙅 Yozmang dedi» writes
``suppression(dnc)``. Only the listed managers reach a manager card, each
their own; ``bot/`` holds no Telethon.

End to end through the real dispatcher over the fake Bot API, on committed
rows (``conftest.session`` wipes them). Synthetic data only (07 §1.0).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from clientscrapper.bot import callbacks as cb
from clientscrapper.bot.texts import t
from clientscrapper.config import settings
from clientscrapper.db.models import LeadReview, Suppression
from clientscrapper.db.session import session_scope
from clientscrapper.services import candidates, cards, owner, reviews
from tests.ast_tools import PACKAGE, dotted_name, imported_modules, is_under, tree_of
from tests.gate_fixtures import NOW
from tests.review_fixtures import (
    MANAGER,
    OTHER_MANAGER,
    OWNER,
    STRANGER,
    FakeSender,
    card,
    configure,
    contacts_of,
    fresh,
    lead,
    reviews_of,
    world,
)

USERNAME = "namuna_tg"
NO_PHONE: dict[str, Any] = {"score_breakdown": {"gates": {"no_phone": True}}}


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, managers=(MANAGER, OTHER_MANAGER))


async def handoff_lead(session, nn: int = 61):
    """The phone-less Telegram candidate in ``review`` (02 §2.9: ≥ 60, a
    username, ``gates.no_phone``) with its asked owner card."""
    cand = await lead(
        session, nn, person=True, username=USERNAME, phone_number=None, fields=NO_PHONE
    )
    review = await card(session, cand, state="asked")
    await session.commit()
    return cand, review


async def held_lead(session):
    """A candidate already handed off: in ``hold``, its manager card asked
    in MANAGER's chat (not yet committed)."""
    cand = await lead(
        session, person=True, username=USERNAME, phone_number=None, state="hold"
    )
    manager = await card(
        session, cand, kind="manager", state="asked", by_telegram_id=MANAGER
    )
    return cand, manager


async def manager_card(session, cand) -> LeadReview:
    [row] = [r for r in await reviews_of(session, cand) if r.kind == "manager"]
    return row


# --- 👤: hold + a manager row -----------------------------------------------------------


async def test_handoff_holds_30_days_and_makes_a_manager_card(session, setup):
    cand, review = await handoff_lead(session)
    done = await reviews.answer(session, review.id, "handoff", by=OWNER, now=NOW)
    await session.commit()
    assert done.manager_review_id is not None
    row = await fresh(session, review)
    assert (row.state, row.answer, row.answered_at) == ("approved", "handoff", NOW)
    found = await fresh(session, cand)
    assert (found.state, found.hold_until) == ("hold", NOW + timedelta(days=30))
    manager = await manager_card(session, cand)
    assert (manager.id, manager.state, manager.by_telegram_id, manager.created_at) == (
        done.manager_review_id,
        "pending",
        MANAGER,
        NOW,
    )


async def test_the_least_loaded_manager_gets_the_card_round_robin(session, setup):
    taken = []
    for nn in (61, 62, 63):
        cand, review = await handoff_lead(session, nn)
        await reviews.answer(session, review.id, "handoff", by=OWNER, now=NOW)
        await session.commit()
        taken.append((await manager_card(session, cand)).by_telegram_id)
    assert taken == [MANAGER, OTHER_MANAGER, MANAGER]


async def test_without_a_manager_the_prospect_waits_in_hold(session, monkeypatch):
    """06 §3.4: «MANAGER_TELEGRAM_IDS empty = Telegram-only prospects wait in
    hold». 👤 is taken: the candidate holds 30 days and its manager card waits
    ``pending`` with nobody yet; the first manager listed later receives it."""
    configure(monkeypatch, managers=())
    cand, review = await handoff_lead(session)
    w = world(session_scope)
    await w.press(cb.review(review.id, "h"))
    [edited] = w.fake.named("EditMessageText")
    assert edited["text"].splitlines()[-1] == (
        "👤 Kutishda — menejer qoʻshilgach yuboriladi"
    )
    assert w.fake.named("SendMessage") == []
    assert (await fresh(session, review)).state == "approved"
    found = await fresh(session, cand)
    assert (found.state, found.hold_until) == ("hold", NOW + timedelta(days=30))
    waiting = await manager_card(session, cand)
    assert (waiting.state, waiting.by_telegram_id) == ("pending", None)
    # Nobody to send to: the job sends nothing and keeps it.
    sender = FakeSender()
    assert (await reviews.run_cards(sender, now=NOW + timedelta(minutes=2))).managers == 0
    assert sender.tried == []
    # A manager is listed: the next run hands it over.
    monkeypatch.setattr(settings, "manager_telegram_ids", (MANAGER,))
    report = await reviews.run_cards(sender, now=NOW + timedelta(minutes=4))
    assert report.managers == 1
    assert [to for to, _, _, _ in sender.sent] == [MANAGER]
    row = await fresh(session, waiting)
    assert (row.state, row.by_telegram_id) == ("asked", MANAGER)


# --- the whole road: 👤 → the manager's card → 📞 → /tel → approved -------------------


async def test_the_handoff_reaches_the_manager_and_tel_approves_as_the_manager(
    session, setup, monkeypatch
):
    cand, review = await handoff_lead(session)
    w = world(session_scope)
    w.fake.names[MANAGER] = "Aziz"
    await w.press(cb.review(review.id, "h"))
    # The manager's card, in the manager's chat, with decision 2a's line.
    [sent] = [m for m in w.fake.named("SendMessage") if m["chat_id"] == MANAGER]
    lines = sent["text"].splitlines()
    assert lines[0].startswith("📨 Nomzod · 72/100")
    assert f"👤 Yozish: t.me/{USERNAME} — oʻz akkauntingizdan, 10:00–17:00 (Du–Sha)" in (
        lines
    )
    assert lines[-1] == cards.ERP_FIRST_UZ
    manager = await manager_card(session, cand)
    rows = sent["reply_markup"]["inline_keyboard"]
    buttons = [b["callback_data"] for row in rows for b in row]
    assert buttons == [cb.manager(manager.id, a) for a in ("c", "x", "w", "l")]
    assert (manager.state, manager.card_chat_id) == ("asked", MANAGER)
    # The owner's card: edited in place, the footer naming the manager by
    # the first name Telegram gave — never the numeric id.
    [edited] = w.fake.named("EditMessageText")
    assert (edited["chat_id"], edited["text"].splitlines()[-1]) == (
        OWNER,
        "👤 Menejerga yuborildi — Aziz",
    )
    # 📞 Bogʻlandim: the card is answered, the candidate stays in hold.
    await w.press(cb.manager(manager.id, "c"), user=MANAGER)
    row = await fresh(session, manager)
    assert (row.state, row.answer) == ("approved", "contacted")
    assert (await fresh(session, cand)).state == "hold"
    # /tel: the prospect's phone, re-scored at once to approved by the manager.
    walk: list[tuple[str, str]] = []
    real = candidates.transition

    async def recording(session_: Any, candidate: Any, target: str, **kw: Any) -> None:
        walk.append((candidate.state, target))
        await real(session_, candidate, target, **kw)

    monkeypatch.setattr(candidates, "transition", recording)
    await w.say(f"/tel {manager.id} +998 90 000 00 77", user=MANAGER)
    assert w.fake.texts()[-1].startswith("✅ Raqam yozildi — lid CRMʼga yuboriladi")
    assert walk == [("hold", "scored"), ("scored", "approved")]
    found = await fresh(session, cand)
    assert (found.state, found.approved_by, found.approved_at) == (
        "approved",
        f"manager:{MANAGER}",
        NOW,
    )
    [person] = await contacts_of(session, found)
    assert (person.tier, person.phone, person.telegram_username) == (
        "person",
        "+998900000077",
        USERNAME,
    )
    # All three legal columns: the request, the cited message, now.
    assert (person.legal_basis, person.source_url, person.collected_at) == (
        "pre_contract_request",
        "https://t.me/namuna_optom/61",
        NOW,
    )


async def test_yozmang_dedi_writes_a_dnc_and_suppresses(session, setup):
    cand, manager = await held_lead(session)
    await session.commit()
    w = world(session_scope)
    await w.press(cb.manager(manager.id, "x"), user=MANAGER)
    assert w.fake.answers() == ["🙅 Yozmang dedi — boshqa yozilmaydi"]
    [row] = list(await session.scalars(sa.select(Suppression)))
    assert (row.kind, row.key_type, row.key, row.source, row.reason) == (
        "dnc",
        "tg_username",
        USERNAME,
        "owner_bot",
        "manager_report",
    )
    found = await fresh(session, cand)
    assert (found.state, found.suppressed_by) == ("suppressed", row.id)
    assert (await fresh(session, manager)).answer == "said_no"


async def test_wrong_or_b2c_excludes(session, setup):
    cand, manager = await held_lead(session)
    await session.commit()
    w = world(session_scope)
    await w.press(cb.manager(manager.id, "w"), user=MANAGER)
    found = await fresh(session, cand)
    assert (found.state, found.exclude_reason) == ("excluded", "b2c")


# --- who may answer -----------------------------------------------------------------


async def test_only_the_cards_manager_answers_it(session, setup):
    cand, manager = await held_lead(session)
    await session.commit()
    w = world(session_scope)
    # Another listed manager: «not yours»; nothing changes.
    await w.press(cb.manager(manager.id, "c"), user=OTHER_MANAGER)
    assert w.fake.answers() == [t("not_yours")]
    # A non-listed id gets nothing at all — not even the button answered.
    await w.press(cb.manager(manager.id, "c"), user=STRANGER)
    assert w.fake.calls == []
    await w.say(f"/tel {manager.id} +998 90 000 00 77", user=STRANGER)
    assert w.fake.calls == []
    # /tel by the other manager is refused.
    await w.say(f"/tel {manager.id} +998 90 000 00 77", user=OTHER_MANAGER)
    assert w.fake.texts() == [t("tel.not_yours")]
    assert (await fresh(session, manager)).state == "asked"
    assert (await fresh(session, cand)).state == "hold"


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("/tel", "tel.how"),
        ("/tel 5", "tel.how"),
        ("/tel abc +998900000077", "tel.how"),
        ("/tel 99999999999999999999 +998900000077", "tel.how"),
        ("/tel {id} +7 999 123 45 67", "tel.bad_phone"),
        ("/tel 987654 +998900000077", "tel.no_card"),
    ],
)
async def test_tel_refuses(session, setup, text, said):
    cand, manager = await held_lead(session)
    await session.commit()
    w = world(session_scope)
    await w.say(text.format(id=manager.id), user=MANAGER)
    assert w.fake.texts() == [t(said)]
    assert (await fresh(session, cand)).state == "hold"


async def test_tel_on_a_candidate_that_left_hold_writes_nothing(session, setup):
    cand, manager = await held_lead(session)
    cand.state = "excluded"
    cand.exclude_reason = "b2c"
    await session.commit()
    w = world(session_scope)
    await w.say(f"/tel {manager.id} +998 90 000 00 77", user=MANAGER)
    assert w.fake.texts() == [t("tel.not_waiting")]
    [person] = await contacts_of(session, cand)
    assert person.phone is None


# --- the hours (06 §1.6): a manager card outside them waits for the job ----------------


async def test_a_handoff_after_hours_is_sent_by_the_job_next_morning(session, setup):
    """The owner's footer says the card waits, with no id in it; when the job
    hands the card over the next morning, the owner's card is edited to say
    who received it (06 §1.3 «👤 Menejerga yuborildi — {manager}»)."""
    cand, review = await handoff_lead(session)
    evening = NOW.replace(hour=19)
    w = world(session_scope, clock=evening)
    await w.press(cb.review(review.id, "h"))
    assert [m["chat_id"] for m in w.fake.named("SendMessage")] == []
    [edited] = w.fake.named("EditMessageText")
    waiting = edited["text"].splitlines()[-1]
    assert waiting == "👤 Menejerga navbatda — hali yetkazilmadi"
    assert str(MANAGER) not in edited["text"]
    assert (await manager_card(session, cand)).state == "pending"
    sender = FakeSender(names={MANAGER: "Aziz"})
    morning = NOW.replace(hour=9, minute=5) + timedelta(days=1)
    report = await reviews.run_cards(sender, now=morning)
    assert report.managers == 1
    [(to, text, layout, _)] = sender.sent
    assert (to, layout) == (MANAGER, "manager")
    assert text.splitlines()[-1] == cards.ERP_FIRST_UZ
    row = await manager_card(session, cand)
    assert (row.state, row.asked_at) == ("asked", morning)
    # The owner's card follows the delivery, once.
    owner_row = await fresh(session, review)
    [(chat, message, body, layout, rid)] = sender.edits
    assert (chat, message, layout, rid) == (
        OWNER,
        owner_row.card_message_id,
        None,
        review.id,
    )
    assert body.splitlines()[-1] == "👤 Menejerga yuborildi — Aziz"
    assert body.splitlines()[0] == edited["text"].splitlines()[0]
    assert owner_row.card_hash == cards.card_hash(body, None)
    for minutes in (2, 4):
        await reviews.run_cards(sender, now=morning + timedelta(minutes=minutes))
    assert len(sender.edits) == 1


async def test_a_handoff_while_paused_sends_no_card_until_yoq(session, setup):
    """/toxtat: «no card» — the manager's card too (06 §1.2)."""
    cand, review = await handoff_lead(session)
    await owner.pause(session, by=f"owner:{OWNER}", reason=None, now=NOW)
    await session.commit()
    w = world(session_scope)
    await w.press(cb.review(review.id, "h"))
    assert w.fake.named("SendMessage") == []
    [edited] = w.fake.named("EditMessageText")
    assert edited["text"].splitlines()[-1] == "👤 Menejerga navbatda — hali yetkazilmadi"
    assert (await manager_card(session, cand)).state == "pending"
    sender = FakeSender(names={MANAGER: "Aziz"})
    assert (await reviews.run_cards(sender, now=NOW)).held == "paused"
    assert sender.sent == []
    await owner.resume(session)
    await session.commit()
    report = await reviews.run_cards(sender, now=NOW + timedelta(minutes=2))
    assert report.managers == 1
    assert [to for to, _, _, _ in sender.sent] == [MANAGER]


async def test_a_manager_dropped_from_the_list_hands_the_card_on(
    session, setup, monkeypatch
):
    cand = await lead(
        session, person=True, username=USERNAME, phone_number=None, state="hold"
    )
    pending = await card(session, cand, kind="manager", by_telegram_id=OTHER_MANAGER)
    await session.commit()
    monkeypatch.setattr(settings, "manager_telegram_ids", (MANAGER,))
    sender = FakeSender()
    await reviews.run_cards(sender, now=NOW)
    assert [to for to, _, _, _ in sender.sent] == [MANAGER]
    assert (await fresh(session, pending)).by_telegram_id == MANAGER


# --- no Telethon in the bot (06 §5.3) ---------------------------------------------


def test_no_module_of_the_bot_imports_telethon():
    bot = PACKAGE / "bot"
    found = {
        str(path.relative_to(PACKAGE)): sorted(
            name
            for name in imported_modules(tree_of(path), module=dotted_name(path))
            if is_under(name, ["telethon"])
        )
        for path in sorted(bot.rglob("*.py"))
    }
    assert len(found) >= 10  # the scan reads the package
    assert {k: v for k, v in found.items() if v} == {}
