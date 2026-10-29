"""The review flow (02 §2.9's review state machine; 06 §1.3, §1.6; 07 §4.1
T-21): each answer of both ``lead_reviews.kind``\\ s and the 02 §2.9
transition it makes, the expiry (``hold`` 7 days, re-asked at most twice),
⏭ (+24 h, asked again the next day), ``card_hash`` (a card is edited only
when it changed), and ``review_cards_job`` — every 2 minutes, sending only
inside ``REVIEW_HOURS`` with the burst and daily caps of 06 §1.6, and an
intent message reaching the owner's card in under 5 minutes (01 N-06).

On Postgres, with committed rows (``conftest.session`` wipes them); cards go
to a ``FakeSender`` that records them. Synthetic data only (07 §1.0).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db.models import (
    Company,
    Interaction,
    LeadReview,
    ReminderLog,
    Suppression,
)
from clientscrapper.services import (
    candidates,
    cards,
    classify,
    companies,
    owner,
    push_gate,
    reviews,
)
from clientscrapper.services.push_gate import Refusal
from clientscrapper.worker import main as worker
from tests import classify_fixtures as cf
from tests import gate_fixtures
from tests.fakes import message, model_fixture
from tests.gate_fixtures import NOW
from tests.review_fixtures import (
    MANAGER,
    OTHER_MANAGER,
    OWNER,
    FakeSender,
    card,
    company_of,
    configure,
    contacts_of,
    fresh,
    lead,
    reviews_of,
)

QUEUED = "✅ Navbatda — CRMʼga yuboriladi"
CONTAINER = model_fixture("classify_container_no_number.json")
enabled = cf.enabled
offline = cf.offline


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, managers=(MANAGER, OTHER_MANAGER))


async def tapped(session, review, code, *, by=OWNER, now=NOW, target=None):
    done = await reviews.answer(session, review.id, code, by=by, now=now, target=target)
    await session.commit()
    return done


# --- each answer → the 02 §2.9 transition ------------------------------------------


def test_the_review_row_states_are_02s():
    """02 §2.9: «Owner answers push/handoff → approved; decline/dnc/not_b2b/
    competitor → declined; merge → merged. Manager answers contacted →
    approved; said_no/wrong_or_b2c/no_reply → declined»; later and
    edit_phone keep ``asked``."""
    assert dict(reviews.ROW_STATE) == {
        "push": "approved",
        "handoff": "approved",
        "contacted": "approved",
        "decline": "declined",
        "dnc": "declined",
        "not_b2b": "declined",
        "competitor": "declined",
        "said_no": "declined",
        "wrong_or_b2c": "declined",
        "no_reply": "declined",
        "merge": "merged",
        "later": "asked",
        "edit_phone": "asked",
    }
    assert set(reviews.ROW_STATE) == reviews.OWNER_ANSWERS | reviews.MANAGER_ANSWERS


@pytest.mark.parametrize(
    ("code", "row_state", "candidate_state", "reason"),
    [
        ("push", "approved", "approved", None),
        ("decline", "declined", "rejected", None),
        ("later", "asked", "review", None),
        ("dnc", "declined", "suppressed", None),
        ("not_b2b", "declined", "excluded", "b2c"),
        ("competitor", "declined", "excluded", "competitor"),
        ("handoff", "approved", "hold", None),
    ],
)
async def test_each_owner_answer_makes_its_transition(
    session, setup, code, row_state, candidate_state, reason
):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    done = await tapped(session, review, code)
    assert (done.answer, done.stale) == (code, False)
    row = await fresh(session, review)
    found = await fresh(session, cand)
    assert (row.state, row.answer, row.by_telegram_id) == (row_state, code, OWNER)
    assert row.answered_at == (None if row_state == "asked" else NOW)
    assert (found.state, found.exclude_reason) == (candidate_state, reason)


@pytest.mark.parametrize(
    ("code", "row_state", "candidate_state", "reason"),
    [
        ("contacted", "approved", "hold", None),
        ("said_no", "declined", "suppressed", None),
        ("wrong_or_b2c", "declined", "excluded", "b2c"),
        ("no_reply", "declined", "hold", None),
        ("later", "asked", "hold", None),
    ],
)
async def test_each_manager_answer_makes_its_transition(
    session, setup, code, row_state, candidate_state, reason
):
    cand = await lead(session, person=True, username="namuna_tg", state="hold")
    review = await card(
        session, cand, kind="manager", state="asked", by_telegram_id=MANAGER
    )
    await session.commit()
    done = await tapped(session, review, code, by=MANAGER)
    assert done.answer == code
    row = await fresh(session, review)
    found = await fresh(session, cand)
    assert (row.state, row.answer, row.by_telegram_id) == (row_state, code, MANAGER)
    assert (found.state, found.exclude_reason) == (candidate_state, reason)


async def test_push_approves_as_the_owner_and_says_when_it_goes(session, setup):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    done = await tapped(session, review, "push")
    assert done.footer == QUEUED
    found = await fresh(session, cand)
    assert (found.approved_by, found.approved_at) == (f"owner:{OWNER}", NOW)
    assert (await fresh(session, review)).note is None


@pytest.mark.parametrize(
    ("now", "footer"),
    [
        # Tuesday 18:00: the window opens tomorrow at 09:30.
        (NOW.replace(hour=18), "⏳ Ertaga 09:30 da yuboriladi"),
        # Tuesday 08:00: today.
        (NOW.replace(hour=8), "⏳ Bugun 09:30 da yuboriladi"),
        # Saturday 18:00 (31.10): no window on Sunday — Monday.
        (NOW.replace(day=31, hour=18), "⏳ 02.11.2026 09:30 da yuboriladi"),
    ],
)
async def test_push_outside_the_window_names_when_it_goes(session, setup, now, footer):
    cand = await lead(session)
    review = await card(session, cand, state="asked", expires_at=now + timedelta(days=1))
    await session.commit()
    done = await tapped(session, review, "push", now=now)
    assert done.footer == footer


async def test_push_without_a_usable_phone_is_refused_and_the_card_stays(session, setup):
    cand = await lead(session, person=True, username="namuna_tg", phone_number=None)
    review = await card(session, cand, state="asked")
    await session.commit()
    with pytest.raises(reviews.Refused) as refused:
        await reviews.answer(session, review.id, "push", by=OWNER, now=NOW)
    await session.rollback()
    assert refused.value.code == "no_phone"
    assert (await fresh(session, review)).state == "asked"
    assert (await fresh(session, cand)).state == "review"


async def test_later_adds_24_hours_and_asks_again_tomorrow(session, setup):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    done = await tapped(session, review, "later")
    assert done.footer == "⏭ Keyinroq — ertaga qayta soʻrayman"
    row = await fresh(session, review)
    assert (row.state, row.answer) == ("asked", "later")
    assert row.expires_at == NOW + timedelta(hours=72 + 24)
    assert row.remind_at == NOW + timedelta(hours=24)
    first_message = row.card_message_id
    # The next day, inside REVIEW_HOURS: the card goes out again as a fresh
    # message — still the same row, still asked, its keyboard the card's.
    sender = FakeSender()
    report = await reviews.run_cards(sender, now=NOW + timedelta(hours=24))
    assert report.resent == 1
    [(to, text, layout, review_id)] = sender.sent
    assert (to, layout, review_id) == (OWNER, "review", review.id)
    row = await fresh(session, review)
    assert (row.state, row.remind_at) == ("asked", None)
    assert row.card_message_id != first_message
    assert row.card_hash == cards.card_hash(text, "review")
    # Once only.
    again = FakeSender()
    await reviews.run_cards(again, now=NOW + timedelta(hours=24, minutes=2))
    assert again.sent == []


async def test_a_later_card_is_not_asked_again_before_its_day(session, setup):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    await tapped(session, review, "later")
    sender = FakeSender()
    await reviews.run_cards(sender, now=NOW + timedelta(hours=23, minutes=59))
    assert sender.sent == []


async def test_dnc_writes_the_lead_phone_and_suppresses(session, setup):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    done = await tapped(session, review, "dnc")
    assert done.footer == "🔒 DNC — boshqa koʻrsatilmaydi"
    [row] = list(await session.scalars(sa.select(Suppression)))
    assert (row.kind, row.key_type, row.key, row.source, row.reason) == (
        "dnc",
        "phone_tail",
        "900000061",
        "owner_bot",
        "owner_card",
    )
    assert (await fresh(session, cand)).suppressed_by == row.id


async def test_not_b2b_excludes_and_suppresses_the_company(session, setup):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    await tapped(session, review, "not_b2b")
    [row] = list(await session.scalars(sa.select(Suppression)))
    assert (row.kind, row.key_type, row.key, row.source) == (
        "b2c",
        "company_id",
        str(cand.company_id),
        "owner_bot",
    )


async def test_competitor_flags_the_company(session, setup):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    done = await tapped(session, review, "competitor")
    assert done.footer == "🚚 Kargo — chiqarildi; kanali manba sifatida qoladi"
    assert (await company_of(session, cand)).is_competitor is True
    [row] = list(await session.scalars(sa.select(Suppression)))
    assert (row.kind, row.key, row.reason) == (
        "competitor",
        str(cand.company_id),
        "owner_card",
    )


async def test_merge_beside_the_targets_open_candidate_excludes_merged(session, setup):
    cand = await lead(session, 61)
    target = await lead(session, 62, state="review")
    review = await card(session, cand, state="asked")
    target_company = await company_of(session, target)
    await session.commit()
    done = await tapped(session, review, "merge", target=target.company_id)
    assert done.footer == f"🔀 Birlashtirildi → {target_company.display_name}"
    row = await fresh(session, review)
    found = await fresh(session, cand)
    assert (row.state, row.answer, row.answered_at) == ("merged", "merge", NOW)
    assert (found.state, found.exclude_reason) == ("excluded", "merged")
    source = await company_of(session, cand)
    assert (source.state, source.merged_into) == ("merged", target.company_id)


async def test_merge_into_a_company_without_a_candidate_moves_it(session, setup):
    """``merge_into``'s own rule: the open candidate moves onto the target
    and stays in review — the next card asks about the merged company."""
    cand = await lead(session, 61)
    target = await gate_fixtures.firm(session, "Namuna Tekstil MChJ")
    review = await card(session, cand, state="asked")
    await session.commit()
    await tapped(session, review, "merge", target=target.id)
    found = await fresh(session, cand)
    assert (found.state, found.company_id) == ("review", target.id)
    assert (await fresh(session, review)).state == "merged"
    assert await reviews.ensure_pending(session, now=NOW) == 1
    await session.commit()
    assert [r.state for r in await reviews_of(session, cand)] == ["merged", "pending"]


async def test_a_merge_two_stirs_forbid_keeps_the_card(session, setup):
    cand = await lead(session, 61, stir="300000011")
    target = await gate_fixtures.firm(session, stir="300000022")
    review = await card(session, cand, state="asked")
    await session.commit()
    with pytest.raises(reviews.Refused) as refused:
        await reviews.answer(
            session, review.id, "merge", by=OWNER, now=NOW, target=target.id
        )
    await session.rollback()
    assert refused.value.code == "merge:stir"
    assert (await fresh(session, review)).state == "asked"


async def test_edit_phone_replaces_the_digits_on_the_same_row(session, setup):
    cand = await lead(session, person=True, username="namuna_tg")
    review = await card(session, cand, state="asked")
    await session.commit()
    [before] = await contacts_of(session, cand)
    basis = (before.legal_basis, before.source_url, before.collected_at)
    await reviews.edit_phone(session, review.id, "+998 90 000 00 71", by=OWNER, now=NOW)
    await session.commit()
    [after] = await contacts_of(session, cand)
    assert (after.id, after.phone, after.phone_kind) == (
        before.id,
        "+998900000071",
        "mobile",
    )
    assert (after.legal_basis, after.source_url, after.collected_at) == basis
    row = await fresh(session, review)
    assert (row.state, row.answer) == ("asked", "edit_phone")
    # Re-scored on the corrected number.
    assert (await fresh(session, cand)).last_scored_at is None


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("+7 999 123 45 67", "bad_phone"),
        ("salom", "bad_phone"),
        ("+998 90 000 00 61", "same_phone"),
    ],
)
async def test_edit_phone_refuses(session, setup, raw, code):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    with pytest.raises(reviews.Refused) as refused:
        await reviews.edit_phone(session, review.id, raw, by=OWNER, now=NOW)
    await session.rollback()
    assert refused.value.code == code
    [contact] = await contacts_of(session, await fresh(session, cand))
    assert contact.phone == "+998900000061"


async def test_a_second_tap_of_the_same_card_is_stale(session, setup):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    assert (await tapped(session, review, "decline")).answer == "decline"
    assert (await tapped(session, review, "push")).stale
    assert (await fresh(session, cand)).state == "rejected"


async def test_a_tap_after_the_candidate_left_review_closes_the_card_only(session, setup):
    """06 §1.3: a card whose candidate left ``review`` (score_job's ``review
    → hold`` when the phone went) is stale — closed, never transitioned."""
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    cand.state = "hold"
    cand.hold_until = None
    await session.commit()
    assert (await tapped(session, review, "push")).stale
    row = await fresh(session, review)
    assert (row.state, row.note, row.answer) == ("expired", "stale:hold", None)
    found = await fresh(session, cand)
    assert (found.state, found.hold_until, found.approved_by) == ("hold", None, None)


async def test_an_owner_answer_on_a_manager_card_is_refused(session, setup):
    cand = await lead(session, state="hold")
    review = await card(
        session, cand, kind="manager", state="asked", by_telegram_id=MANAGER
    )
    await session.commit()
    review_id = review.id
    for code, by in (("push", OWNER), ("contacted", OTHER_MANAGER)):
        with pytest.raises(reviews.Refused):
            await reviews.answer(session, review_id, code, by=by, now=NOW)
        await session.rollback()
    assert (await fresh(session, review)).state == "asked"


# --- expiry (02 §2.9) ---------------------------------------------------------------


async def test_an_owner_card_nobody_answered_holds_its_candidate_7_days(session, setup):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    assert await reviews.expire_due(session, now=NOW + timedelta(hours=71)) == []
    due = NOW + timedelta(hours=72)
    [closed] = await reviews.expire_due(session, now=due)
    await session.commit()
    assert closed.review_id == review.id
    row = await fresh(session, review)
    assert (row.state, row.answer, row.answered_at) == ("expired", None, None)
    found = await fresh(session, cand)
    assert (found.state, found.hold_until) == ("hold", due + timedelta(days=7))
    [reminder] = list(await session.scalars(sa.select(ReminderLog)))
    assert (reminder.kind, reminder.ref, reminder.sent_at) == (
        "review_expired",
        str(cand.id),
        due,
    )


async def test_an_expired_card_is_re_asked_at_most_twice(session, setup):
    """Three asks, three silences: after each of the first two the candidate
    holds 7 days and the score job brings it back to a new card; the third
    holds it with no timer — only a new signal could bring it back."""
    cand = await lead(session)
    await session.commit()
    sender = FakeSender()
    now = NOW
    holds = []
    for _ in range(3):
        report = await reviews.run_cards(sender, now=now)
        assert report.sent == 1
        now += timedelta(hours=72)
        await reviews.run_cards(sender, now=now)
        found = await fresh(session, cand)
        assert found.state == "hold"
        holds.append(found.hold_until)
        if found.hold_until is None:
            break
        now = found.hold_until + timedelta(minutes=1)
        # Fresh words keep it hot across the weeks (recency); not a new
        # signal: last_signal_at does not move.
        await session.execute(
            sa.text("UPDATE signals SET observed_at = :at"), {"at": now}
        )
        await session.commit()
        assert (await candidates.score_batch(now=now)).scored == 1
        assert (await fresh(session, cand)).state == "review"
    assert holds[:2] == [
        NOW + timedelta(hours=72, days=7),
        NOW + timedelta(hours=144, days=14, minutes=1),
    ]
    assert holds[2] is None
    assert len(sender.sent) == 3
    assert await reviews.expired_count(session, cand.id) == 3
    # Weeks later: nothing brings it back on its own.
    later = now + timedelta(days=30)
    assert (await candidates.score_batch(now=later)).claimed == 0
    await reviews.run_cards(sender, now=later.replace(hour=11))
    assert len(sender.sent) == 3


async def test_a_manager_card_nobody_answered_keeps_the_handoff_hold(session, setup):
    held = NOW + timedelta(days=30)
    cand = await lead(session, state="hold", fields={"hold_until": held})
    review = await card(
        session, cand, kind="manager", state="asked", by_telegram_id=MANAGER
    )
    await session.commit()
    await reviews.expire_due(session, now=NOW + timedelta(hours=72))
    await session.commit()
    assert (await fresh(session, review)).state == "expired"
    found = await fresh(session, cand)
    assert (found.state, found.hold_until) == ("hold", held)
    assert await reviews.expired_count(session, cand.id) == 0


async def test_the_sweep_runs_outside_the_hours_and_strips_the_buttons(session, setup):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    sender = FakeSender()
    night = NOW.replace(hour=23) + timedelta(hours=72)
    report = await reviews.run_cards(sender, now=night)
    assert (report.expired, report.held, report.sent) == (1, "hours", 0)
    assert sender.strips == [(OWNER, review.card_message_id)]


async def test_a_card_whose_candidate_left_review_is_closed_by_the_job(session, setup):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    cand.state = "hold"
    await session.commit()
    sender = FakeSender()
    report = await reviews.run_cards(sender, now=NOW)
    assert report.stale == 1
    assert sender.strips == [(OWNER, review.card_message_id)]
    row = await fresh(session, review)
    assert (row.state, row.note) == ("expired", "stale:hold")
    assert (await fresh(session, cand)).state == "hold"


# --- card_hash: edited only when it changed --------------------------------------------


async def test_a_live_card_is_edited_only_when_it_changed(session, setup):
    cand = await lead(session)
    await session.commit()
    sender = FakeSender()
    await reviews.run_cards(sender, now=NOW)
    [(_, text, _, review_id)] = sender.sent
    row = await session.get(LeadReview, review_id, populate_existing=True)
    assert row is not None and row.card_hash == cards.card_hash(text, "review")
    assert await reviews.refresh_live(reviews.SessionLocal, sender, now=NOW) == 0
    company = await company_of(session, cand)
    company.display_name = "Namuna Mato Savdo"
    await session.commit()
    assert await reviews.refresh_live(reviews.SessionLocal, sender, now=NOW) == 1
    [(chat, message, edited, layout, edited_id)] = sender.edits
    assert (chat, message, layout, edited_id) == (
        OWNER,
        row.card_message_id,
        "review",
        review_id,
    )
    assert edited.splitlines()[1].startswith("🏢 Namuna Mato Savdo")
    assert (await fresh(session, row)).card_hash == cards.card_hash(edited, "review")
    assert await reviews.refresh_live(reviews.SessionLocal, sender, now=NOW) == 0
    assert len(sender.edits) == 1


async def test_a_live_card_a_gate_now_refuses_leaves_review(session, setup):
    """``gates.evaluate`` before every card (04 §6): a DNC that arrived while
    the card waited sets the candidate aside; the next run closes the card."""
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await gate_fixtures.row(session, "dnc", "phone_tail", "900000061")
    await session.commit()
    sender = FakeSender()
    await reviews.refresh_live(reviews.SessionLocal, sender, now=NOW)
    assert sender.edits == []
    assert (await fresh(session, cand)).state == "suppressed"
    report = await reviews.run_cards(sender, now=NOW)
    assert report.stale == 1
    assert (await fresh(session, review)).note == "stale:suppressed"


async def test_a_pushed_cards_footer_follows_the_push(session, setup):
    cand = await lead(session, state="pushed")
    review = await card(
        session, cand, state="approved", answer="push", card_message_id=811
    )
    review.card_chat_id = OWNER
    await gate_fixtures.push(
        session,
        cand,
        sent_at=NOW - timedelta(minutes=3),
        finished_at=NOW - timedelta(minutes=2),
        phone_tail="900000061",
    )
    await session.commit()
    sender = FakeSender()
    assert await reviews.refresh_push_footers(reviews.SessionLocal, sender, now=NOW) == 1
    [(chat, message, text, layout, _)] = sender.edits
    assert (chat, message, layout) == (OWNER, 811, None)
    assert text.splitlines()[-1] == "✅ CRMʼga yuborildi 10:58"
    assert "✔️" not in text  # an answered card carries no gate line
    assert await reviews.refresh_push_footers(reviews.SessionLocal, sender, now=NOW) == 0
    # Outside the window of a fresh push: not read again.
    later = NOW + reviews.PUSH_FOOTER_WINDOW + timedelta(minutes=3)
    assert (
        await reviews.refresh_push_footers(reviews.SessionLocal, sender, now=later) == 0
    )


async def test_an_approved_candidates_refusal_comes_back_as_a_new_card(session, setup):
    """04 Q23 / push_gate.TO_REVIEW: the refusal sends it back to review with
    the note on a new card, which says it on line 2."""
    cand = await lead(session, state="approved")
    await card(session, cand, state="approved", answer="push", card_message_id=812)
    await session.commit()
    locked = await push_gate.lock_for_push(session, cand.id)
    assert locked is not None
    await push_gate.record_refusals(session, locked, [Refusal(13, "score_low")], now=NOW)
    await session.commit()
    sender = FakeSender()
    report = await reviews.run_cards(sender, now=NOW)
    assert report.sent == 1
    [(_, text, _, _)] = sender.sent
    assert text.splitlines()[1] == "⚠️ Ball tushdi"


async def test_an_auto_approved_candidates_refusal_has_a_home(session, setup):
    """No owner card to carry it: ``lead_candidates.push_note`` (0004), and
    ``/lidlar holat`` lists it."""
    cand = await lead(
        session, state="approved", fields={"approved_by": "auto:telegram_monitor"}
    )
    await session.commit()
    locked = await push_gate.lock_for_push(session, cand.id)
    assert locked is not None
    await push_gate.record_refusals(session, locked, [Refusal(2, "paused")], now=NOW)
    await session.commit()
    found = await fresh(session, cand)
    assert (found.state, found.push_note, found.push_noted_at) == (
        "approved",
        "Agent toʻxtatilgan (/toxtat)",
        NOW,
    )
    assert await reviews_of(session, cand) == []
    holat = await cards.lidlar_holat(session)
    company = await company_of(session, cand)
    assert holat.auto_waiting_total == 1
    assert [(w.display_name, w.note) for w in holat.auto_waiting] == [
        (company.display_name, "Agent toʻxtatilgan (/toxtat)")
    ]


# --- review_cards_job (06 §1.6) -------------------------------------------------------


def test_review_cards_is_the_two_minute_slot():
    [slot] = [s for s in worker.job_table() if s.id == "review_cards"]
    assert slot.triggers == ("every 2m",)
    assert slot.job is reviews.review_cards_job


async def test_the_job_makes_a_pending_card_per_review_candidate(session, setup):
    first = await lead(session, 61)
    await lead(session, 62, state="enriching")
    await session.commit()
    report = await reviews.run_cards(None, now=NOW)
    assert (report.pending, report.held) == (1, "no_bot")
    [row] = await reviews_of(session, first)
    assert (row.kind, row.state, row.created_at) == ("owner", "pending", NOW)
    # Once: a live card stays the one card (ux_lead_reviews_live).
    assert (await reviews.run_cards(None, now=NOW)).pending == 0


async def test_a_burst_is_review_burst_max_and_the_rest_waits(session, setup):
    for nn in range(51, 63):
        await lead(session, nn)
    await session.commit()
    sender = FakeSender()
    report = await reviews.run_cards(sender, now=NOW)
    assert (report.pending, report.sent) == (12, settings.review_burst_max) == (12, 10)
    report = await reviews.run_cards(sender, now=NOW + timedelta(minutes=2))
    assert report.sent == 2
    assert {to for to, _, _, _ in sender.sent} == {OWNER}


async def test_the_daily_cap_counts_asked_cards_per_tashkent_day(
    session, setup, monkeypatch
):
    monkeypatch.setattr(settings, "review_daily_cap", 3)
    for nn in range(51, 56):
        await lead(session, nn)
    await session.commit()
    sender = FakeSender()
    assert (await reviews.run_cards(sender, now=NOW)).sent == 3
    assert (await reviews.run_cards(sender, now=NOW + timedelta(hours=2))).sent == 0
    tomorrow = NOW + timedelta(days=1)
    assert (await reviews.run_cards(sender, now=tomorrow)).sent == 2


@pytest.mark.parametrize(
    "now",
    [
        NOW.replace(hour=8, minute=59),  # before REVIEW_HOURS
        NOW.replace(hour=18),  # after
        NOW.replace(day=25),  # a Sunday
    ],
)
async def test_cards_go_out_only_inside_review_hours(session, setup, now):
    await lead(session)
    await session.commit()
    sender = FakeSender()
    report = await reviews.run_cards(sender, now=now)
    assert (report.pending, report.sent, report.held) == (1, 0, "hours")
    assert sender.sent == []


async def test_never_in_quiet_hours(session, setup, monkeypatch):
    monkeypatch.setattr(settings, "review_hours", "07:00-23:00")
    await lead(session)
    await session.commit()
    sender = FakeSender()
    report = await reviews.run_cards(sender, now=NOW.replace(hour=22))
    assert (report.sent, report.held) == (0, "hours")
    assert (await reviews.run_cards(sender, now=NOW.replace(hour=20))).sent == 1


async def test_no_card_while_paused_but_the_books_are_kept(session, setup):
    cand = await lead(session)
    asked = await lead(session, 62)
    review = await card(session, asked, state="asked")
    await owner.pause(session, by=f"owner:{OWNER}", reason=None, now=NOW)
    await session.commit()
    sender = FakeSender()
    report = await reviews.run_cards(sender, now=NOW + timedelta(hours=72))
    assert (report.held, report.sent, report.expired, report.pending) == (
        "paused",
        0,
        1,
        1,
    )
    assert sender.sent == sender.strips == []
    assert (await fresh(session, review)).state == "expired"
    assert [r.state for r in await reviews_of(session, cand)] == ["pending"]


async def test_a_card_telegram_refused_stays_pending(session, setup):
    cand = await lead(session)
    await session.commit()
    report = await reviews.run_cards(FakeSender(fail=True), now=NOW)
    assert (report.sent, report.failed) == (0, True)
    [row] = await reviews_of(session, cand)
    assert row.state == "pending"


async def test_an_intent_message_reaches_the_owners_card_in_under_5_minutes(
    session, setup, enabled, offline, tmp_path, model_api
):
    """01 N-06 on a fake clock, at the cadences of the worker's table: the
    reader stores the message (T0) and notifies; the collector and the
    classifier wake at once; the resolver runs at most one interval later
    (here: the worst case), waking the scorer; ``review_cards`` at most one
    interval after that. The owner's card is asked within 5 minutes."""
    table = {s.id: s for s in worker.job_table()}
    every = {
        name: timedelta(minutes=int(table[name].triggers[0].split()[1][:-1]))
        for name in ("resolve", "review_cards")
    }
    assert table["classify"].wakes_on == table["score"].wakes_on is not None
    text = (
        "Yiwudan 40 futlik konteyner kafel kerak, kim olib keladi? "
        "Tel +998 90 000 00 01"
    )
    await cf.chat(session)
    row_id = await cf.store(session, text, message_id=101, bio=None)
    stored = await session.get(Interaction, row_id)
    assert stored is not None
    t0 = stored.occurred_at
    await cf.link(offline, tmp_path, now=t0)
    model_api.queue(message(CONTAINER))
    assert (await classify.classify_batch(now=t0)).applied == 1
    resolved_at = t0 + every["resolve"]
    assert (await companies.resolve_batch(now=resolved_at)).resolved == 1
    assert (await candidates.score_batch(now=resolved_at)).scored == 1
    sender = FakeSender()
    asked_at = resolved_at + every["review_cards"]
    report = await reviews.run_cards(sender, now=asked_at)
    assert report.sent == 1
    [review] = list(
        await session.scalars(
            sa.select(LeadReview).execution_options(populate_existing=True)
        )
    )
    assert (review.state, review.asked_at) == ("asked", asked_at)
    assert review.asked_at - t0 < timedelta(minutes=5)
    [(to, card_text, layout, _)] = sender.sent
    assert (to, layout) == (OWNER, "review")
    lines = card_text.splitlines()
    # The author is the message's own (interactions.sender_*, 04 §2).
    assert next(x for x in lines if x.startswith("🔎")).startswith(
        "🔎 «40 futlik konteyner» — Rustam Karimov · "
    )


# --- the push gate's view of a review: the owner's word ------------------------------


async def test_a_shared_phone_approval_stores_the_owners_word(session, setup):
    """04 §7 item 4: ✅ on a shared lead phone writes ``shared_ok`` exactly —
    when the company has a usable phone too (the transition demands one)."""
    cand = await lead(session)
    company = await company_of(session, cand)
    [contact] = await contacts_of(session, cand)
    contact.shared_phone = True
    second = await gate_fixtures.contact(session, company, gate_fixtures.phone(62))
    cand.contact_id = contact.id
    review = await card(session, cand, state="asked")
    await session.commit()
    assert second.phone is not None
    await tapped(session, review, "push")
    assert (await fresh(session, review)).note == push_gate.SHARED_OK


async def test_pick_manager_is_the_least_loaded_today(session, setup):
    for nn in (61, 62):
        cand = await lead(session, nn, person=True, username="namuna_tg", state="hold")
        await card(
            session,
            cand,
            kind="manager",
            by_telegram_id=MANAGER,
            created_at=NOW - timedelta(hours=1),
        )
    # Yesterday's cards are not today's load.
    old = await lead(session, 70, state="hold")
    await card(
        session,
        old,
        kind="manager",
        state="expired",
        by_telegram_id=OTHER_MANAGER,
        created_at=NOW - timedelta(days=1),
    )
    await session.commit()
    assert await reviews.pick_manager(session, NOW) == OTHER_MANAGER


async def test_merge_needs_a_company_that_exists(session, setup):
    cand = await lead(session)
    review = await card(session, cand, state="asked")
    await session.commit()
    review_id = review.id
    for target, code in ((None, "no_target"), (cand.company_id, "same_company")):
        with pytest.raises(reviews.Refused) as refused:
            await reviews.answer(
                session, review_id, "merge", by=OWNER, now=NOW, target=target
            )
        await session.rollback()
        assert refused.value.code == code
    assert await session.scalar(sa.select(sa.func.count()).select_from(Company)) == 1


async def test_the_owner_budget_is_the_burst_within_the_days_cap(monkeypatch):
    monkeypatch.setattr(settings, "review_burst_max", 10)
    monkeypatch.setattr(settings, "review_daily_cap", 40)
    assert [reviews.owner_budget(n) for n in (0, 35, 40, 41)] == [10, 5, 0, 0]


def test_review_hours_are_monday_to_saturday():
    assert frozenset({1, 2, 3, 4, 5, 6}) == reviews.REVIEW_DAYS
    tuesday = datetime(2026, 10, 27, 9, 0, tzinfo=gate_fixtures.TZ)
    assert reviews.sending_hours(tuesday)
    assert not reviews.sending_hours(tuesday - timedelta(minutes=1))
    assert not reviews.sending_hours(tuesday.replace(hour=18))
    assert not reviews.sending_hours(tuesday.replace(day=25))
    assert reviews.sending_hours(tuesday.replace(day=31, hour=17, minute=59))
