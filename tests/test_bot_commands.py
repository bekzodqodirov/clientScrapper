"""The owner bot's commands and buttons, end to end through the dispatcher
(06 §1.1, §1.2; 07 §4.1 T-20, F-13).

Every update is fed to the real dispatcher (``bot/main.build_dispatcher``)
over ``tests/fake_bot``'s Bot API, so the routers' filters, the command
parsing, ``HANDLERS`` and the replies are the production ones. One test per
command; the owner-only guard (an unset ``OWNER_TELEGRAM_ID`` locks everyone
out); and the four writes of 06 §1.2 pinned: ``/dnc`` → a DNC row,
``/vazn`` → a new ``scoring_weights`` version, ``/push tekshir`` → the
delivered row superseded and re-pushed as ``attempt + 1``, ``/stir`` → the
STIR, its evidence and E4 due again.

The handlers' database work runs in a connection whose transaction is rolled
back after the test (``sources`` and ``scoring_weights`` are seeded
configuration), except ``/push tekshir``, which pushes through the fake ERP
on committed rows like ``test_crm_push``. Every text the bot sends is held
to the Uzbek text rules (07 §1.11).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clientscrapper.bot import callbacks as cb
from clientscrapper.bot import deliver, handlers
from clientscrapper.bot import main as bot_main
from clientscrapper.bot.handlers.common import Deps, Plans
from clientscrapper.bot.texts import COMMAND_MENU, MANAGER_MENU, t
from clientscrapper.config import settings
from clientscrapper.db.models import (
    ChatMonitor,
    Company,
    Contact,
    Evidence,
    LeadCandidate,
    LeadReview,
    PurgeLog,
    RuntimeFlag,
    ScoringWeight,
    Signal,
    Source,
    Suppression,
)
from clientscrapper.db.session import engine, session_scope
from clientscrapper.services import cards, chats, crm_push, health, owner, purge, scoring
from tests import gate_fixtures
from tests.fake_bot import FakeSession, added_to_group, document, fake_bot, message, tap
from tests.fixtures.candidate_views import EXCERPT
from tests.gate_fixtures import NOW, phone
from tests.test_cards import BUBBLE_PX, width_px
from tests.test_uz_text import problems

OWNER = 1001
MANAGER = 2002
OTHER_MANAGER = 2003
STRANGER = 3003
TOKEN = "42:TEST"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "assistant_bot_token", TOKEN)
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER)
    monkeypatch.setattr(settings, "manager_telegram_ids", (MANAGER, OTHER_MANAGER))


Factory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@pytest.fixture
async def db(session: AsyncSession) -> AsyncIterator[Factory]:
    """Sessions on one connection whose transaction is rolled back at the
    end: a handler's commit releases a savepoint, nothing outlives the test."""
    async with engine.connect() as conn:
        outer = await conn.begin()
        maker = async_sessionmaker(
            bind=conn,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )

        @asynccontextmanager
        async def factory() -> AsyncIterator[AsyncSession]:
            async with maker() as s:
                try:
                    yield s
                    await s.commit()
                except Exception:
                    await s.rollback()
                    raise

        yield factory
        await outer.rollback()


@dataclass
class World:
    bot: Bot
    fake: FakeSession
    dp: Dispatcher
    deps: Deps
    db: Factory

    async def say(self, text: str | None, *, user: int = OWNER, **kw: Any) -> None:
        self.fake.clear()
        await self.dp.feed_update(self.bot, message(self.bot, text, user_id=user, **kw))
        self.check()

    async def press(self, data: str, *, user: int = OWNER, **kw: Any) -> None:
        self.fake.clear()
        await self.dp.feed_update(self.bot, tap(self.bot, data, user_id=user, **kw))
        self.check()

    def check(self) -> None:
        """No handler failed into the error reply, every word is clean, and
        nothing went to a chat outside the allowlist."""
        said = [*self.fake.texts(), *(a for a in self.fake.answers() if a)]
        assert t("error") not in said
        assert [s for s in said if problems(s, rendered=True)] == []
        assert self.fake.chats() <= {OWNER, MANAGER, OTHER_MANAGER}

    def last(self) -> str:
        return self.fake.texts()[-1]

    def buttons(self) -> list[str]:
        sent = self.fake.named("SendMessage", "EditMessageText", "EditMessageReplyMarkup")
        markup = sent[-1].get("reply_markup") or {"inline_keyboard": []}
        return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


def world(factory: Factory, **deps: Any) -> World:
    bot, fake = fake_bot()
    built = Deps(session_factory=factory, clock=lambda: NOW, **deps)
    return World(bot, fake, bot_main.build_dispatcher(built), built, factory)


@pytest.fixture
def w(db: Factory, configured: None) -> World:
    return world(db)


async def lead(
    s: AsyncSession, nn: int = 61, *, state: str = "review", **firm: Any
) -> LeadCandidate:
    """A company with a phone, a cited quote and a candidate (synthetic)."""
    owner_ = await gate_fixtures.firm(s, **firm)
    contact = await gate_fixtures.contact(s, owner_, phone(nn))
    ev = Evidence(
        source_key="telegram_monitor",
        url=f"https://t.me/namuna_optom/{nn}",
        fetched_at=NOW - timedelta(days=2),
        published_at=NOW - timedelta(days=2),
        sha256=f"{nn:064d}",
        bytes=0,
        excerpt=EXCERPT,
        expires_at=NOW + timedelta(days=180),
    )
    s.add(ev)
    await s.flush()
    for key, quote, value in (
        ("vol_stated_3plus", "5 kub mato kerak", Decimal(5)),
        ("cn_origin", "Xitoydan", None),
    ):
        s.add(
            Signal(
                company_id=owner_.id,
                evidence_id=ev.id,
                key=key,
                value=value,
                quote=quote,
                observed_at=NOW - timedelta(days=2),
                source_key="telegram_monitor",
            )
        )
    cand = await gate_fixtures.candidate(s, owner_, state=state, lead=contact, score=72)
    await s.flush()
    return cand


async def pending_card(s: AsyncSession, cand: LeadCandidate, **fields: Any) -> LeadReview:
    fields.setdefault("kind", "owner")
    fields.setdefault("state", "pending")
    if fields["state"] == "asked":
        fields.setdefault("asked_at", NOW)
        fields.setdefault("card_chat_id", fields.get("by_telegram_id") or OWNER)
        fields.setdefault("card_message_id", 77)
        fields.setdefault("card_hash", "0" * 64)
        fields.setdefault("expires_at", NOW + timedelta(hours=72))
    if fields["state"] in ("approved", "declined", "merged"):
        fields.setdefault("answered_at", NOW)
    review = LeadReview(candidate_id=cand.id, **fields)
    s.add(review)
    await s.flush()
    return review


# --- who may talk to the bot (06 §1.1) ---------------------------------------------


async def test_an_unset_owner_locks_everyone_out(db, configured, monkeypatch):
    monkeypatch.setattr(settings, "owner_telegram_id", None)
    w = world(db)
    for user in (OWNER, MANAGER, STRANGER):
        await w.say("/yordam", user=user)
        await w.say("/lidlar", user=user)
        assert w.fake.calls == []
    assert bot_main.refusal() == t("start.no_owner")
    with pytest.raises(SystemExit) as stop:
        await bot_main.run(bot=w.bot)
    assert stop.value.code == t("start.no_owner")


async def test_without_a_token_the_bot_does_not_start(configured, monkeypatch):
    monkeypatch.setattr(settings, "assistant_bot_token", "")
    with pytest.raises(SystemExit) as stop:
        await bot_main.run()
    assert stop.value.code == t("start.no_token")


async def test_a_stranger_is_answered_nothing_and_logged_once_an_hour(w, caplog):
    handlers._rejected.clear()
    await w.say("/lidlar", user=STRANGER)
    await w.press("ps:y", user=STRANGER)
    await w.say("salom", user=STRANGER)
    assert w.fake.calls == []
    logged = [r for r in caplog.records if r.getMessage() == "rejected_update"]
    assert len(logged) == 1


async def test_the_owner_in_a_group_is_not_served(w):
    await w.say("/holat", chat_type="supergroup", chat_id=-100500)
    assert w.fake.calls == []


async def test_added_to_a_group_the_bot_leaves(w):
    w.fake.clear()
    await w.dp.feed_update(w.bot, added_to_group(w.bot, by=OWNER))
    assert w.fake.named("LeaveChat") == [{"chat_id": -100500}]
    assert w.fake.named("SendMessage") == []


async def test_a_button_nobody_built_is_answered_stale(w):
    await w.press("rv:1:z")
    assert w.fake.answers() == [t("stale_button")]


async def test_a_manager_reaches_only_their_commands(w):
    await w.say("/lidlar", user=MANAGER)
    assert w.fake.texts() == [t("help_manager")]
    await w.press("ps:y", user=MANAGER)
    assert w.fake.answers() == [t("stale_button")]
    await w.say("/yordam", user=MANAGER)
    assert w.last() == t("help_manager")


async def test_start_and_yordam(w):
    await w.say("/start")
    assert w.last() == t("start_owner")
    await w.say("/yordam")
    assert w.last() == t("help_owner")
    await w.say("/start", user=MANAGER)
    assert w.last() == t("start_manager")


# --- /lidlar ----------------------------------------------------------------------------


async def test_lidlar_sends_the_card_and_marks_it_asked(w):
    async with w.db() as s:
        cand = await lead(s)
        review = await pending_card(s, cand)
    await w.say("/lidlar")
    [card_message] = w.fake.named("SendMessage")
    assert card_message["chat_id"] == OWNER
    text = card_message["text"]
    assert text.splitlines()[0] == "🆕 Nomzod · 72/100 · Segment yoʻq"
    assert text.splitlines()[-1] == "✔️ mijoz emas · DNC yoʻq · 90 kunda yuborilmagan"
    assert w.buttons() == [
        cb.review(review.id, a) for a in ("p", "d", "m", "x", "l", "o")
    ]
    async with w.db() as s:
        row = await s.get(LeadReview, review.id, populate_existing=True)
        assert row is not None
        assert (row.state, row.card_chat_id, row.asked_at) == ("asked", OWNER, NOW)
        assert row.card_hash == cards.card_hash(text, "review")
        assert row.expires_at == NOW + timedelta(hours=settings.review_expiry_hours)
    await w.say("/lidlar")
    assert w.fake.texts() == [t("lidlar.none")]


async def test_lidlar_shows_no_card_while_the_agent_is_paused(w):
    async with w.db() as s:
        cand = await lead(s)
        review = await pending_card(s, cand)
    await w.say("/toxtat")
    await w.say("/lidlar")
    assert w.fake.texts() == [t("lidlar.paused")]
    async with w.db() as s:
        row = await s.get(LeadReview, review.id, populate_existing=True)
        assert row is not None and row.state == "pending"


async def test_lidlar_does_not_show_a_card_its_gate_now_refuses(w):
    async with w.db() as s:
        cand = await lead(s)
        await pending_card(s, cand)
        # A push to the company 10 days ago: gate 6 holds it 90 days.
        await gate_fixtures.push(
            s, cand, sent_at=NOW - timedelta(days=10), phone_tail="900000061"
        )
    await w.say("/lidlar")
    assert w.fake.texts() == [t("lidlar.skipped", n=1)]
    async with w.db() as s:
        found = await s.get(LeadCandidate, cand.id, populate_existing=True)
        assert found is not None
        # The gate's own hold (gate 6: 90 days from the push), not the
        # refused card's 7 days.
        assert (found.state, found.hold_until) == ("hold", NOW + timedelta(days=80))


async def test_a_card_whose_company_turned_dnc_is_suppressed_not_held(w):
    async with w.db() as s:
        cand = await lead(s)
        await pending_card(s, cand)
        s.add(
            Suppression(
                kind="dnc",
                key_type="phone_tail",
                key="900000061",
                source="owner_bot",
                reason="manager_report",
            )
        )
        await s.flush()
    await w.say("/lidlar")
    assert w.fake.texts() == [t("lidlar.skipped", n=1)]
    async with w.db() as s:
        found = await s.get(LeadCandidate, cand.id, populate_existing=True)
        assert found is not None and found.state == "suppressed"


async def _quoteless(s: AsyncSession, nn: int, *, score: int) -> LeadCandidate:
    """A review candidate with a phone and no signal: no quote to cite."""
    firm = await gate_fixtures.firm(s)
    contact = await gate_fixtures.contact(s, firm, phone(nn))
    return await gate_fixtures.candidate(
        s, firm, state="review", lead=contact, score=score
    )


def _owner_cards(w: World) -> list[str]:
    return [x for x in w.fake.texts() if x.startswith("🆕 ")]


async def test_a_card_that_cannot_be_shown_leaves_the_queue(w, monkeypatch):
    """Two top-scored cards with no citable quote must not hold the burst for
    ever: they are set aside (held 7 days) and the good card goes out."""
    monkeypatch.setattr(settings, "review_burst_max", 2)
    async with w.db() as s:
        bad = [await _quoteless(s, nn, score=95) for nn in (11, 12)]
        for b in bad:
            await pending_card(s, b)
        good = await lead(s, 61)
        good_card = await pending_card(s, good)
    await w.say("/lidlar")
    [card] = _owner_cards(w)
    assert "📞 +998 90 000 00 61 (mobil)" in card
    assert w.fake.texts()[-1] == t("lidlar.skipped", n=2)
    async with w.db() as s:
        row = await s.get(LeadReview, good_card.id, populate_existing=True)
        assert row is not None and row.state == "asked"
        for b in bad:
            held = await s.get(LeadCandidate, b.id, populate_existing=True)
            assert held is not None
            assert (held.state, held.hold_until) == ("hold", NOW + timedelta(days=7))
    await w.say("/lidlar")
    assert w.fake.texts() == [t("lidlar.none")]


async def test_a_card_whose_signals_expired_is_set_aside(w):
    async with w.db() as s:
        cand = await lead(s)
        review = await pending_card(s, cand)
        await s.execute(sa.update(Signal).values(expires_at=NOW - timedelta(days=1)))
    await w.say("/lidlar")
    assert w.fake.texts() == [t("lidlar.skipped", n=1)]
    async with w.db() as s:
        found = await s.get(LeadCandidate, cand.id, populate_existing=True)
        row = await s.get(LeadReview, review.id, populate_existing=True)
        assert found is not None and row is not None
        assert (found.state, row.state) == ("hold", "pending")
    await w.say("/lidlar")
    assert w.fake.texts() == [t("lidlar.none")]


async def test_lidlar_sends_the_best_owner_cards_first_within_the_burst(w, monkeypatch):
    monkeypatch.setattr(settings, "review_burst_max", 2)
    async with w.db() as s:
        for nn, score in ((51, 50), (52, 90), (53, 70), (55, 70)):
            cand = await lead(s, nn)
            cand.score = score
            await pending_card(s, cand)
        # A manager's card on a review candidate is never the owner's.
        other = await lead(s, 54)
        other.score = 99
        manager_card = await pending_card(
            s, other, kind="manager", by_telegram_id=MANAGER
        )
        await s.flush()
    await w.say("/lidlar")
    sent = _owner_cards(w)
    assert [line for x in sent for line in x.splitlines() if line.startswith("📞")] == [
        "📞 +998 90 000 00 52 (mobil)",
        "📞 +998 90 000 00 53 (mobil)",
    ]
    assert w.fake.texts()[-1] == t("lidlar.more", n=2)
    assert w.fake.chats() == {OWNER}
    async with w.db() as s:
        row = await s.get(LeadReview, manager_card.id, populate_existing=True)
        assert row is not None and row.state == "pending"
    await w.say("/lidlar")
    assert [x.splitlines()[0] for x in _owner_cards(w)] == [
        "🆕 Nomzod · 70/100 · Segment yoʻq",
        "🆕 Nomzod · 50/100 · Segment yoʻq",
    ]
    assert "📞 +998 90 000 00 55 (mobil)" in _owner_cards(w)[0]


async def test_a_card_telegram_refused_waits_and_the_cards_out_stay_asked(w):
    async with w.db() as s:
        first = await lead(s, 61)
        first.score = 90
        first_card = await pending_card(s, first)
        second = await lead(s, 62)
        second_card = await pending_card(s, second)
    refused = TelegramBadRequest(
        method=SendMessage(chat_id=OWNER, text="x"), message="Bad Request"
    )
    w.fake.fail("SendMessage", refused, times=2, after=1)
    await w.say("/lidlar")
    assert w.fake.texts()[-2:] == [t("lidlar.send_failed"), t("lidlar.more", n=1)]
    async with w.db() as s:
        out = await s.get(LeadReview, first_card.id, populate_existing=True)
        waits = await s.get(LeadReview, second_card.id, populate_existing=True)
        assert out is not None and waits is not None
        assert (out.state, waits.state) == ("asked", "pending")


async def test_a_handoff_row_with_an_unusable_phone_gets_the_manager_card(w):
    """gates.no_phone and a lead phone that is not usable (here: shared on
    3+ firms) → «📨 Faqat Telegram» and [👤 Menejerga], never ✅."""
    async with w.db() as s:
        cand = await lead(s)
        await s.execute(
            sa.update(Contact)
            .where(Contact.id == cand.contact_id)
            .values(shared_phone=True, telegram_username="namuna_tg")
        )
        cand.score_breakdown = {"gates": {"no_phone": True}}
        review = await pending_card(s, cand)
    await w.say("/lidlar")
    [card] = _owner_cards(w)
    lines = card.splitlines()
    assert lines[4] == "📨 Faqat Telegram: @namuna_tg"
    assert not any(line.startswith(("📞", "⚠️")) for line in lines)
    assert w.buttons()[:2] == [cb.review(review.id, "h"), cb.review(review.id, "d")]
    async with w.db() as s:
        row = await s.get(LeadReview, review.id, populate_existing=True)
        assert row is not None
        assert row.card_hash == cards.card_hash(card, "handoff")
    # The card's own layout answers its buttons: ⋯ keeps 👤, never ✅.
    await w.press(cb.review(review.id, "o"))
    assert cb.review(review.id, "h") in w.buttons()
    assert cb.review(review.id, "p") not in w.buttons()


async def test_two_lidlar_at_once_send_a_card_once(session, configured):
    """A double tap (or review_cards_job beside /lidlar): each card is
    claimed, so the second run takes the next one or nothing."""
    cand = await lead(session, 61)
    review = await pending_card(session, cand)
    await session.commit()
    w = world(session_scope)
    w.fake.clear()
    await asyncio.gather(
        w.dp.feed_update(w.bot, message(w.bot, "/lidlar", user_id=OWNER)),
        w.dp.feed_update(w.bot, message(w.bot, "/lidlar", user_id=OWNER)),
    )
    w.check()
    assert len(_owner_cards(w)) == 1
    row = await session.get(LeadReview, review.id, populate_existing=True)
    assert row is not None and row.state == "asked"
    [card] = [m for m in w.fake.named("SendMessage") if m["text"].startswith("🆕 ")]
    assert row.card_hash == cards.card_hash(card["text"], "review")


async def test_lidlar_answers_the_owner_at_any_hour(db, configured):
    """The owner asked: /lidlar ignores REVIEW_HOURS and QUIET_HOURS (and
    the daily cap — those bound the unsolicited cards of review_cards_job)."""
    w = world(db)
    night = NOW.replace(hour=23, minute=30)
    w.deps.clock = lambda: night
    async with w.db() as s:
        await pending_card(s, await lead(s))
    await w.say("/lidlar")
    assert len(_owner_cards(w)) == 1


async def test_a_button_past_its_column_is_stale_not_a_database_error(w):
    await w.press("ch:t:9999999999")
    assert w.fake.answers() == [t("stale_button")]


async def test_lidlar_holat_counts_the_states(w):
    async with w.db() as s:
        cand = await lead(s)
        await pending_card(s, cand)
    await w.say("/lidlar holat")
    lines = w.last().splitlines()
    assert lines[0] == "📋 Nomzodlar holati"
    assert "koʻrib chiqishda: 1" in lines
    assert "topildi: 0" in lines


async def test_the_card_buttons_menus_and_the_answer_seam(w):
    async with w.db() as s:
        cand = await lead(s)
        review = await pending_card(s, cand, state="asked")
    await w.press(cb.review(review.id, "o"))
    assert w.buttons()[-4:] == [cb.review(review.id, a) for a in ("b", "c", "e", "k")]
    await w.press(cb.review(review.id, "k"))
    assert w.buttons()[-1] == cb.review(review.id, "o")
    # The answers are the review flow's (T-21): not wired, nothing changes.
    await w.press(cb.review(review.id, "p"))
    assert w.fake.answers() == [t("review.not_ready")]
    await w.press(cb.review(review.id, "m"))
    assert w.fake.answers() == [t("review.no_targets")]
    async with w.db() as s:
        row = await s.get(LeadReview, review.id, populate_existing=True)
        assert row is not None and (row.state, row.answer) == ("asked", None)


async def test_a_tap_on_a_card_that_no_longer_waits_is_stale(w):
    async with w.db() as s:
        cand = await lead(s)
        review = await pending_card(s, cand, state="declined", answer="decline")
    await w.press(cb.review(review.id, "p"))
    assert w.fake.answers() == [t("stale_card")]


async def test_a_manager_presses_only_their_own_card(w):
    async with w.db() as s:
        cand = await lead(s, state="hold")
        review = await pending_card(
            s, cand, kind="manager", state="asked", by_telegram_id=MANAGER
        )
    await w.press(cb.manager(review.id, "c"), user=OTHER_MANAGER)
    assert w.fake.answers() == [t("not_yours")]
    await w.press(cb.manager(review.id, "c"), user=MANAGER)
    assert w.fake.answers() == [t("review.not_ready")]


# --- /manbalar --------------------------------------------------------------------------


async def test_manbalar_lists_toggles_informs_and_forbids(w):
    await w.say("/manbalar")
    text = w.last()
    assert text.splitlines()[0].startswith("🛰 Manbalar (1/")
    async with w.db() as s:
        row = await s.get(Source, "telegram_monitor", populate_existing=True)
        assert row is not None
        before = row.enabled
        title = row.title_uz
    assert cb.source("telegram_monitor", "t") in w.buttons() or any(
        b.startswith("src:p:") for b in w.buttons()
    )
    await w.press(cb.source("telegram_monitor", "t"))
    word = "manbalar.off" if before else "manbalar.on"
    assert w.fake.answers() == [t(word, title=title)]
    async with w.db() as s:
        row = await s.get(Source, "telegram_monitor", populate_existing=True)
        assert row is not None and row.enabled is (not before)
    await w.press(cb.source("telegram_monitor", "i"))
    assert w.last().splitlines()[0] == f"ℹ️ {title} (telegram_monitor)"
    assert cb.source("telegram_monitor", "a") in w.buttons()
    await w.press(cb.source("telegram_monitor", "a"))
    async with w.db() as s:
        row = await s.get(Source, "telegram_monitor", populate_existing=True)
        assert row is not None
        assert (row.config.get("auto_push_forbidden"), row.auto_push) == (True, False)
    assert cb.source("telegram_monitor", "a") not in w.buttons()
    await w.press(cb.sources_page(0))
    assert w.last().splitlines()[0].startswith("🛰 Manbalar (1/")


# --- /chats -----------------------------------------------------------------------------


async def test_chats_lists_toggles_and_queues(w):
    async with w.db() as s:
        group = ChatMonitor(
            tg_chat_id=-1001234,
            chat_type="group",
            title="Namuna optom guruh",
            status="joined",
            joined_at=NOW - timedelta(days=9),
            monitor_enabled=True,
            seen_count=40,
        )
        private = ChatMonitor(
            tg_chat_id=555,
            chat_type="private",
            title="Namuna",
            status="joined",
            joined_at=NOW - timedelta(days=9),
            monitor_enabled=False,
        )
        s.add_all([group, private])
        await s.flush()
        group_id, private_id = group.id, private.id
    await w.say("/chats")
    assert "✅ 👥 Namuna optom guruh · 0 signal (7 kun) / jami 40 xabar" in w.last()
    await w.press(cb.chat_toggle(group_id))
    assert w.fake.answers() == [t("chats.off", title="Namuna optom guruh")]
    async with w.db() as s:
        row = await s.get(ChatMonitor, group_id, populate_existing=True)
        assert row is not None and (row.monitor_enabled, row.decided_by) == (
            False,
            "owner",
        )
    await w.press(cb.chat_toggle(private_id))
    assert w.fake.answers() == [t("chats.never_read")]
    await w.say("/chats qoʻsh @namuna_yangi_chat")
    assert w.last() == t("chats.added", username="namuna_yangi_chat")
    await w.say("/chats qo'sh @namuna_yangi_chat")
    assert w.last() == t("chats.not_added", username="namuna_yangi_chat")
    async with w.db() as s:
        flag = await s.get(RuntimeFlag, chats.PENDING_FLAG, populate_existing=True)
        assert flag is not None
        queued = flag.value["names"]["namuna_yangi_chat"]
        assert (queued["priority"], queued["by"]) == (0, f"owner:{OWNER}")


# --- /holat, /xarajat -----------------------------------------------------------------


async def test_holat_shows_the_layout_and_the_problems(w):
    await w.say("/holat")
    lines = w.last().splitlines()
    assert lines[0].startswith("🩺 Agent holati — ")
    assert any(line.startswith("Muammolar: ") for line in lines)


async def test_xarajat_shows_the_day_and_the_month(w):
    await w.say("/xarajat")
    assert w.last().splitlines()[0].startswith("💸 Xarajat — bugun $0.00 (0% / $")


# --- /toxtat, /yoq ----------------------------------------------------------------------


async def test_toxtat_pauses_at_once_and_yoq_resumes_after_its_confirm(w):
    await w.say("/toxtat bozor yopiq")
    assert w.last() == "⏸ Toʻxtatildi. Davom ettirish: /yoq"
    async with w.db() as s:
        flag = await s.get(RuntimeFlag, owner.PAUSE_FLAG, populate_existing=True)
        assert flag is not None
        assert flag.value["reason"] == "bozor yopiq"
        assert flag.updated_by == f"owner:{OWNER}"
    await w.say("/yoq")
    assert w.last() == "Davom ettiraymi?"
    assert w.buttons() == [cb.resume("y"), cb.resume("n")]
    await w.press(cb.resume("n"))
    assert w.last() == t("yoq.kept")
    await w.press(cb.resume("y"))
    assert w.last() == t("yoq.resumed")
    async with w.db() as s:
        assert await s.get(RuntimeFlag, owner.PAUSE_FLAG, populate_existing=True) is None
    await w.say("/yoq")
    assert w.last() == t("yoq.not_paused")


async def test_yoq_crm_clears_the_breaker_after_its_confirm(w):
    async with w.db() as s:
        await s.execute(
            sa.update(Source)
            .where(Source.key == "erp_sync")
            .values(breaker_until=NOW + timedelta(hours=1), breaker_reason="push_404")
        )
    await w.say("/yoq crm")
    assert w.buttons() == [cb.resume("c"), cb.resume("n")]
    await w.press(cb.resume("c"))
    assert w.last() == t("yoq.breaker_cleared")
    async with w.db() as s:
        row = await s.get(Source, "erp_sync", populate_existing=True)
        assert row is not None and (row.breaker_until, row.breaker_reason) == (None, None)


# --- /import ----------------------------------------------------------------------------


CSV = (
    "Code;Name;Phones;Active\n"
    "NMN01;Namuna Bir;+998 90 000 00 71;✓\n"
    "NMN02;Namuna Ikki;+998 90 000 00 72;✓\n"
).encode()


async def test_import_plans_then_writes_after_the_confirm(w):
    await w.say("/import")
    assert w.last() == t("import.how")
    w.fake.files["f1"] = CSV
    await w.say(None, caption="/import", document=document("f1", "clients.csv", len(CSV)))
    plan_text = w.last()
    assert plan_text.endswith(t("import.ask"))
    [yes, no] = w.buttons()
    assert cb.parse(yes) is not None and yes.endswith(":y") and no.endswith(":n")
    await w.press(yes)
    async with w.db() as s:
        rows = (
            await s.scalars(
                sa.select(Suppression.key).where(
                    Suppression.kind == "client", Suppression.source == "manual_import"
                )
            )
        ).all()
    assert sorted(rows) == ["900000071", "900000072"]
    await w.press(yes)
    assert w.last() == t("import.expired")


async def test_import_refuses_a_file_it_cannot_read(w):
    w.fake.files["f2"] = b"Name;Kod\nNamuna;1\n"
    await w.say(None, caption="/import", document=document("f2", "clients.csv", 20))
    assert "Phones" in w.last() or "telefon" in w.last().lower()
    assert w.buttons() == []


# --- /unut, /qidir ----------------------------------------------------------------------


async def test_unut_plans_by_counts_then_purges_after_its_confirm(w):
    async with w.db() as s:
        cand = await lead(s, name="Namuna Unut Savdo", stir="300000061")
        company_id = cand.company_id
    await w.say("/unut +998 90 000 00 61")
    plan_text = w.last()
    lines = plan_text.splitlines()
    assert lines[0] == "🗑 Oʻchirish rejasi · huquqiy soʻrov"
    assert width_px(lines[0]) <= BUBBLE_PX
    assert "🏢 Kompaniya: 1 · kontakt: 1" in lines
    assert t("unut.subject", id=str(company_id)) in lines
    assert lines[-1] == t("unut.ask")
    # Counts and the uuid only: never the name or the phone (06 §5.4).
    assert "Namuna Unut" not in plan_text and "900000061" not in plan_text
    [yes, no] = w.buttons()
    parsed = cb.parse(yes)
    assert parsed is not None and (parsed.kind, parsed.action) == ("pg", "y")
    assert no == yes[:-1] + "n"
    async with w.db() as s:
        row = await s.scalar(sa.select(PurgeLog))
        assert row is not None and (row.id, row.executed_at, row.error) == (
            parsed.ref,
            None,
            None,
        )
        assert (row.requested_by, row.reason) == (f"owner:{OWNER}", "legal_request")
        assert (await s.get(Company, company_id)).state == "active"
    await w.press(yes)
    done = w.last()
    assert done.splitlines()[0] == "✅ Oʻchirildi · huquqiy soʻrov"
    assert "Namuna Unut" not in done
    async with w.db() as s:
        found = await s.get(Company, company_id, populate_existing=True)
        assert found is not None and found.state == "purged"
        assert await s.get(LeadCandidate, cand.id, populate_existing=True) is None
        held = await s.scalar(
            sa.select(Suppression).where(
                Suppression.key_type == "phone_tail", Suppression.key == "900000061"
            )
        )
        assert held is not None and held.kind == "legal"
        row = await s.scalar(
            sa.select(PurgeLog).execution_options(populate_existing=True)
        )
        assert row is not None and row.executed_at is not None and row.error is None
    await w.press(yes)
    assert w.last() == t("unut.done")
    # A purged company is no longer found — by the STIR it keeps either.
    for query in ("+998 90 000 00 61", "300000061", str(company_id)):
        await w.say(f"/unut {query}")
        assert w.last() == t("unut.none", query=query)


async def test_unut_cancel_and_a_forgotten_plan_delete_nothing(w):
    async with w.db() as s:
        cand = await lead(s, name="Namuna Bekor Savdo")
        company_id = cand.company_id
    await w.say(f"/unut {company_id}")
    [yes, no] = w.buttons()
    await w.press(no)
    assert w.last() == t("unut.cancelled")
    await w.press(yes)
    assert w.last() == t("unut.closed")
    # The bot restarted between the plan and the confirm: the plan is gone.
    await w.say(f"/unut {cand.id}")
    [yes, _no] = w.buttons()
    w.deps.plans = Plans()
    await w.press(yes)
    assert w.last() == t("unut.expired")
    async with w.db() as s:
        rows = (await s.scalars(sa.select(PurgeLog).order_by(PurgeLog.id))).all()
        assert [(r.error, r.executed_at) for r in rows] == [
            ("cancelled", None),
            ("expired", None),
        ]
        found = await s.get(Company, company_id, populate_existing=True)
        assert found is not None and found.state != "purged"
        assert await s.get(LeadCandidate, cand.id) is not None
        assert await s.scalar(sa.select(sa.func.count(Suppression.id))) == 0


async def test_unut_finishes_a_purge_that_stopped_part_way(w, monkeypatch):
    """Group 1 committed, group 2 failed: the owner is told how to finish, a
    re-tap says the plan did not finish, and /unut plans the firm again by
    the keys it still has — until nothing is left."""
    async with w.db() as s:
        cand = await lead(s, nn=63, name="Namuna Yarim Savdo", stir="300000063")
        company_id = cand.company_id
    await w.say("/unut 300000063")
    [yes, _no] = w.buttons()
    real = purge._people

    async def broken(*args: Any, **kw: Any) -> dict[str, int]:
        raise RuntimeError("db blip in group 2")

    monkeypatch.setattr(purge, "_people", broken)
    await w.press(yes)
    assert w.last() == t("unut.failed", id=str(company_id))
    monkeypatch.setattr(purge, "_people", real)
    async with w.db() as s:
        row = await s.scalar(sa.select(PurgeLog))
        assert row is not None and row.error == "failed:RuntimeError"
        firm = await s.get(Company, company_id, populate_existing=True)
        assert firm is not None and firm.state == "purged"
        left = await s.scalar(
            sa.select(sa.func.count())
            .select_from(Contact)
            .where(Contact.company_id == company_id)
        )
        assert left == 1
    await w.press(yes)
    assert w.last() == t("unut.unfinished")
    for query in ("300000063", "+998 90 000 00 63", str(company_id)):
        await w.say(f"/unut {query}")
        assert t("unut.subject", id=str(company_id)) in w.last().splitlines()
        assert len(w.buttons()) == 2
    [yes, _no] = w.buttons()
    await w.press(yes)
    assert w.last().splitlines()[0] == "✅ Oʻchirildi · huquqiy soʻrov"
    await w.say("/unut 300000063")
    assert w.last() == t("unut.none", query="300000063")


async def test_unut_a_second_tap_closes_nothing_the_first_one_runs(w):
    """✅ twice (or ✅ then ❌): the first tap holds the plan; the second, which
    finds none in memory, must not close the row as expired under it."""
    async with w.db() as s:
        cand = await lead(s, nn=64, name="Namuna Ikki Bosish")
        company_id = cand.company_id
    await w.say(f"/unut {company_id}")
    [yes, no] = w.buttons()
    ref = cb.parse(yes)
    assert ref is not None and ref.ref is not None
    # The first tap took the plan and has not claimed its row yet.
    planned = w.deps.plans.take("purge", ref.ref, now=NOW)
    assert planned is not None
    for second in (yes, no):
        await w.press(second)
        assert w.last() == t("unut.running")
    async with w.db() as s:
        row = await s.scalar(sa.select(PurgeLog))
        assert row is not None and (row.error, row.executed_at) == (None, None)
        result = await purge.execute(s, planned, requested_by=f"owner:{OWNER}", now=NOW)
    assert result.executed["companies"] == 1
    await w.press(yes)
    assert w.last() == t("unut.done")


async def test_unut_asks_for_one_company_when_a_key_names_several(w):
    async with w.db() as s:
        first = await gate_fixtures.firm(s, "Namuna Bir Savdo")
        second = await gate_fixtures.firm(s, "Namuna Ikki Savdo")
        for company in (first, second):
            await gate_fixtures.contact(s, company, phone(62))
    await w.say("/unut +998 90 000 00 62")
    text = w.last()
    assert text.startswith("2 ta kompaniya topildi")
    assert f"<code>{first.id}</code>" in text and f"<code>{second.id}</code>" in text
    assert "Namuna" not in text
    assert w.buttons() == []
    await w.say("/unut Yoʻq Kompaniya Xyz")
    assert w.last() == t("unut.none", query="Yoʻq Kompaniya Xyz")
    await w.say("/unut")
    assert w.last() == t("unut.how")
    async with w.db() as s:
        assert await s.scalar(sa.select(sa.func.count(PurgeLog.id))) == 0


async def test_qidir_finds_a_company_by_its_phone_and_creates_nothing(w):
    async with w.db() as s:
        cand = await lead(s, name="Namuna Qidiruv Savdo")
        before = await s.scalar(sa.select(sa.func.count()).select_from(Company))
    await w.say("/qidir +998 90 000 00 61")
    text = w.last()
    assert text.splitlines()[0].startswith("🏢 Namuna Qidiruv Savdo (MChJ)")
    assert f"<code>{cand.id}</code>" in text
    await w.say("/qidir Yoʻq Kompaniya Xyz")
    assert w.last() == t("qidir.none", query="Yoʻq Kompaniya Xyz")
    await w.say("/qidir")
    assert w.last() == t("qidir.how")
    async with w.db() as s:
        after = await s.scalar(sa.select(sa.func.count()).select_from(Company))
    assert after == before


# --- the four writes (07 §4.1 T-20) ----------------------------------------------------


async def test_dnc_writes_a_dnc_row_after_its_confirm(w):
    async with w.db() as s:
        cand = await lead(s, name="Namuna DNC Savdo")
    await w.say("/dnc +998 90 000 00 61")
    preview = w.last()
    assert preview.splitlines()[:2] == [
        "🔒 DNC: 900000061",
        "Kompaniya: Namuna DNC Savdo",
    ]
    [yes, _no] = w.buttons()
    await w.press(yes)
    assert w.last() == "🔒 DNC yozildi — 900000061\n" + t("dnc.ended", n=1)
    async with w.db() as s:
        [row] = (
            await s.scalars(sa.select(Suppression).where(Suppression.kind == "dnc"))
        ).all()
        assert (row.key_type, row.key, row.source, row.reason) == (
            "phone_tail",
            "900000061",
            "owner_bot",
            "owner_command",
        )
        found = await s.get(LeadCandidate, cand.id, populate_existing=True)
        assert found is not None and found.state == "suppressed"
    await w.say("/dnc +998900000061")
    assert row.created_at is not None
    assert w.last() == t(
        "dnc.exists", kind="yozilmasin (DNC)", day=cards.day(row.created_at)
    )
    await w.say("/dnc Namuna")
    assert w.last() == t("dnc.how")


async def test_dnc_by_username(w):
    await w.say("/dnc @Namuna_Yozmang")
    assert w.last().splitlines()[0] == "🔒 DNC: @namuna_yozmang"
    await w.press(w.buttons()[0])
    async with w.db() as s:
        row = await s.scalar(sa.select(Suppression).where(Suppression.kind == "dnc"))
        assert row is not None
        assert (row.key_type, row.key) == ("tg_username", "namuna_yozmang")


async def test_vazn_copies_the_active_version_with_the_one_change(w):
    await w.say("/vazn")
    assert w.last().splitlines()[0] == "⚖️ Ball vaznlari — 1-versiya"
    await w.say("/vazn ent_not_micro 5")
    assert w.last() == "⚖️ ent_not_micro: 3 → 5 · 2-versiya"
    async with w.db() as s:
        rows = {
            (r.version, r.key): r
            for r in (await s.scalars(sa.select(ScoringWeight))).all()
        }
    v1 = {k: r for (v, k), r in rows.items() if v == 1}
    v2 = {k: r for (v, k), r in rows.items() if v == 2}
    assert set(v1) == set(v2)
    assert v2["ent_not_micro"].points == 5 and v1["ent_not_micro"].points == 3
    assert all(v2[k].points == v1[k].points for k in v1 if k != "ent_not_micro")
    assert {r.approved_by for r in v2.values()} == {f"owner:{OWNER}"}
    assert {r.approved_at for r in v2.values()} == {NOW}


async def test_vazn_beside_a_waiting_proposal_takes_the_next_free_version(w):
    """T-40's fit_weights proposes version 2 (unapproved); the owner's edit
    is version 3 and supersedes it."""
    async with w.db() as s:
        rows = (
            await s.scalars(sa.select(ScoringWeight).where(ScoringWeight.version == 1))
        ).all()
        s.add_all(
            ScoringWeight(
                version=2,
                key=r.key,
                group_key=r.group_key,
                points=r.points,
                group_cap=r.group_cap,
                decay_days=r.decay_days,
                decayed_factor=r.decayed_factor,
                note=r.note,
                proposed_by="fit_weights@2026-11-01",
            )
            for r in rows
        )
        await s.flush()
    await w.say("/vazn ent_not_micro 5")
    assert w.last() == "⚖️ ent_not_micro: 3 → 5 · 3-versiya"
    async with w.db() as s:
        assert await scoring.active_version(s) == 3
        proposal = await s.scalar(
            sa.select(sa.func.count())
            .select_from(ScoringWeight)
            .where(ScoringWeight.version == 2, ScoringWeight.approved_at.is_(None))
        )
    assert proposal == len(rows)


async def test_vazn_refuses_points_outside_the_group_and_unknown_keys(w):
    await w.say("/vazn ent_not_micro 16")
    assert w.last() == t("vazn.refused", key="ent_not_micro", range="1–15")
    await w.say("/vazn neg_b2c_words 5")
    assert w.last() == t("vazn.refused", key="neg_b2c_words", range="−100…−1")
    await w.say("/vazn yoq_kalit 3")
    assert w.last() == t("vazn.unknown", key="yoq_kalit")
    await w.say("/vazn ent_not_micro besh")
    assert w.last() == t("vazn.bad_points")
    async with w.db() as s:
        top = await s.scalar(sa.select(sa.func.max(ScoringWeight.version)))
    assert top == 1


async def test_stir_writes_the_stir_and_makes_e4_due(w):
    async with w.db() as s:
        cand = await lead(s, name="Namuna STIR Savdo")
        cand_id, company_id = cand.id, cand.company_id
        await s.execute(
            sa.update(LeadCandidate)
            .where(LeadCandidate.id == cand_id)
            .values(next_enrich_at=NOW + timedelta(days=3))
        )
        other = await gate_fixtures.firm(s, "Namuna Boshqa Savdo", stir="300000099")
        other_name = other.display_name
    await w.say(f"/stir {cand_id} 300000077")
    assert w.last() == t("stir.written", stir="300000077", name="Namuna STIR Savdo")
    async with w.db() as s:
        company = await s.get(Company, company_id, populate_existing=True)
        assert company is not None and company.stir == "300000077"
        found = await s.get(LeadCandidate, cand_id, populate_existing=True)
        assert found is not None and found.next_enrich_at == NOW
        ev = await s.scalar(
            sa.select(Evidence).where(Evidence.excerpt == owner.STIR_EXCERPT)
        )
        assert ev is not None and ev.source_key == "registry_lookup"
    await w.say(f"/stir {cand_id} 300000088")
    assert w.last() == t("stir.has_other", name="Namuna STIR Savdo", other="300000077")
    async with w.db() as s:
        second = await lead(s, 62, name="Namuna Ikkinchi")
    await w.say(f"/stir {second.id} 300000099")
    assert w.last() == t("stir.held", other=other_name)
    await w.say(f"/stir {second.id} 12345")
    assert w.last() == t("stir.bad_stir")
    await w.say(f"/stir {uuid.uuid4()} 300000066")
    assert w.last() == t("stir.no_candidate")
    await w.say("/stir nomzod 300000088")
    assert w.last() == t("stir.bad_id")


# --- /push tekshir (committed rows, the fake ERP) ---------------------------------------


@pytest.fixture
async def erp(session, monkeypatch, configured) -> AsyncIterator[Any]:
    from tests.test_crm_push import BASE, World

    monkeypatch.setattr(settings, "crm_base_url", BASE)
    async with gate_fixtures.erp_ready(session, monkeypatch):
        yield World(session)


async def test_push_tekshir_supersedes_and_repushes_as_the_next_attempt(erp):
    cand = await erp.lead()
    first = await erp.push(cand)
    assert first.status == "sent"
    w = world(session_scope, erp=lambda: contextlib.nullcontext(erp.erp))
    await w.say(f"/push tekshir {first.external_id}")
    view = w.last()
    assert "Urinish: 1 · holat: yuborildi · HTTP 200" in view
    assert view.endswith("Qayta yuboraymi?")
    [yes, _no] = w.buttons()
    assert yes == cb.push_check(first.push_log_id, "y")
    await w.press(yes)
    rows = await erp.rows()
    assert [(r.attempt, r.status, r.http_status) for r in rows] == [
        (1, "sent", 200),
        (2, "sent", 200),
    ]
    assert rows[0].superseded_at is not None and rows[1].superseded_at is None
    assert rows[1].payload_sha256 == rows[0].payload_sha256
    assert w.last() == t("push.sent", attempt=2)
    await w.say("/push tekshir")
    assert w.last() == t("push.how")


async def test_push_tekshir_refuses_an_unknown_id(w):
    await w.say("/push tekshir cs:00000000-0000-0000-0000-000000000000:202610")
    assert w.last() == crm_push.PUSH_CHECK_REFUSED_UZ["not_found"]
    assert w.buttons() == []


# --- the one send door (06 §5.3) --------------------------------------------------------


async def test_to_person_refuses_every_chat_outside_the_allowlist(configured):
    bot, fake = fake_bot()
    for chat in (STRANGER, -100500, 0):
        with pytest.raises(deliver.NotAllowed):
            await deliver.to_person(bot, chat, "salom")
    with pytest.raises(deliver.NotAllowed):
        await deliver.to_person(bot, MANAGER, document=__import__("pathlib").Path("x"))
    assert fake.calls == []
    assert await deliver.to_person(bot, MANAGER, "salom") is not None
    notify = deliver.owner_notifier(bot)
    assert await notify("⛔ sinov") is True
    assert fake.chats() == {MANAGER, OWNER}


async def test_to_person_retries_a_refused_html_as_plain_text(configured):
    bot, fake = fake_bot()
    method = SendMessage(chat_id=OWNER, text="x")
    fake.fail("SendMessage", TelegramBadRequest(method=method, message="can't parse"))
    assert await deliver.to_person(bot, OWNER, "<b>buzuq") is not None
    first, second = fake.named("SendMessage")
    assert "parse_mode" not in second or second["parse_mode"] is None
    assert first["text"] == second["text"] == "<b>buzuq"


async def test_nothing_is_sent_when_no_owner_is_set(monkeypatch):
    monkeypatch.setattr(settings, "owner_telegram_id", None)
    bot, fake = fake_bot()
    assert await deliver.owner_notifier(bot)("x") is False
    assert fake.calls == []


# --- the process (06 §1.1) ------------------------------------------------------------


async def test_the_bot_reaches_telegram_sets_the_menus_and_beats(session, configured):
    bot, fake = fake_bot()
    fake.fail("GetMe", OSError("network"), times=2)
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    async def polling(dp: Dispatcher, b: Bot) -> None:
        return None

    await bot_main.run(bot=bot, polling=polling, sleep=sleep)
    assert waits == [5, 10]
    scopes = {
        (f["scope"]["type"], f["scope"].get("chat_id"))
        for f in fake.named("SetMyCommands")
    }
    assert scopes == {("chat", OWNER), ("chat", MANAGER), ("chat", OTHER_MANAGER)}
    menus = {
        f["scope"]["chat_id"]: [c["command"] for c in f["commands"]]
        for f in fake.named("SetMyCommands")
    }
    assert menus[OWNER] == [c for c, _ in COMMAND_MENU]
    assert menus[MANAGER] == menus[OTHER_MANAGER] == [c for c, _ in MANAGER_MENU]
    assert [f["scope"]["type"] for f in fake.named("DeleteMyCommands")] == ["default"]
    beat = (await health.beats(session))["bot"]
    assert beat.detail == {"telegram": True, "username": "cs_agent_bot"}


async def test_the_bot_stops_after_five_failed_get_me(configured):
    bot, fake = fake_bot()
    fake.fail("GetMe", OSError("network"), times=5)

    async def sleep(seconds: float) -> None:
        return None

    with pytest.raises(SystemExit) as stop:
        await bot_main.run(bot=bot, sleep=sleep)
    assert stop.value.code == t("start.no_telegram")


async def test_the_watchdog_says_a_silent_worker_once(session, configured):
    said: list[str] = []

    async def notify(text: str) -> bool:
        said.append(text)
        return True

    await health.beat(session, "worker", now=NOW - timedelta(hours=1))
    await session.commit()
    noon = NOW.replace(hour=12)
    await bot_main.watchdog_tick(notify, now=noon, memo={})
    await bot_main.watchdog_tick(notify, now=noon + timedelta(minutes=5), memo={})
    assert len(said) == 1 and said[0].startswith("⛔ Rejalashtiruvchi")


def test_plans_are_taken_once_and_expire():
    plans = Plans()
    first = plans.put("dnc", "a", now=NOW)
    assert plans.take("import", first, now=NOW) is None
    assert plans.take("dnc", first, now=NOW) == "a"
    assert plans.take("dnc", first, now=NOW) is None
    late = plans.put("dnc", "b", now=NOW)
    assert plans.take("dnc", late, now=NOW + timedelta(minutes=11)) is None


# --- the worker's owner seam (T-20 wires it) ---------------------------------------


async def test_the_worker_tells_the_owner_through_the_bot(configured, monkeypatch):
    from clientscrapper.worker import main as worker

    bot, fake = fake_bot()
    monkeypatch.setattr(deliver, "make_bot", lambda token=None: bot)
    connected = worker.connect_owner_bot()
    try:
        assert connected is bot
        assert await worker.notify("⛔ sinov") is True
        sent = [(f["chat_id"], f["text"]) for f in fake.named("SendMessage")]
        assert sent == [(OWNER, "⛔ sinov")]
    finally:
        await worker.disconnect_owner_bot(connected)
    assert await worker.notify("⛔ yana") is False
    assert len(fake.named("SendMessage")) == 1


async def test_the_worker_stays_silent_without_a_bot(monkeypatch):
    from clientscrapper.worker import main as worker

    monkeypatch.setattr(settings, "assistant_bot_token", "")
    assert worker.connect_owner_bot() is None
    assert await worker.notify("⛔ sinov") is False


async def test_the_merge_menu_lists_the_resolvers_suspicion(w):
    async with w.db() as s:
        cand = await lead(s, name="Namuna Birlashma Savdo")
        target = await gate_fixtures.firm(s, "Zarafshon Elektr Texnika")
        company = await s.get(Company, cand.company_id)
        assert company is not None
        company.dup_of = target.id
        review = await pending_card(s, cand, state="asked")
    await w.press(cb.review(review.id, "m"))
    assert w.buttons() == [cb.merge(review.id, target.id), cb.review(review.id, "k")]
    assert w.fake.answers() == [t("review.merge_title")]


async def test_qidir_finds_a_company_by_its_name(w):
    async with w.db() as s:
        await lead(s, name="Namuna Qidiruv Savdo")
    await w.say("/qidir Namuna Qidiruv Savdo")
    assert w.last().splitlines()[0].startswith("🏢 Namuna Qidiruv Savdo (MChJ)")


# --- the review's pins (each rule a string edit could break, 07 §4.1 T-20) ------------


async def test_buttons_and_commands_outside_a_private_chat_are_not_served(w):
    """06 §1.1: the bot never posts into a group — not for the owner's tap,
    not for a manager's tap or command."""
    await w.press(cb.resume("y"), chat_type="supergroup")
    assert w.fake.calls == []
    await w.press(cb.manager(1, "c"), user=MANAGER, chat_type="supergroup")
    assert w.fake.calls == []
    await w.say("/qidir Namuna", user=MANAGER, chat_type="group", chat_id=-100600)
    assert w.fake.calls == []
    await w.say("/yordam", user=MANAGER, chat_type="group", chat_id=-100600)
    assert w.fake.calls == []


async def test_a_bot_removed_from_a_group_does_not_try_to_leave_it(w):
    for status in ("left", "kicked"):
        w.fake.clear()
        await w.dp.feed_update(w.bot, added_to_group(w.bot, by=OWNER, status=status))
        assert w.fake.calls == []


async def test_a_forwarded_command_runs_nothing(w):
    """Someone else's «/toxtat» or «/vazn …», forwarded by the owner, is
    never the owner's command."""
    await w.say("/toxtat hujum", forwarded_from=STRANGER)
    assert w.fake.texts() == [t("forwarded")]
    await w.say("/vazn ent_not_micro 5", forwarded_from=STRANGER)
    assert w.fake.texts() == [t("forwarded")]
    await w.say("/qidir Namuna", user=MANAGER, forwarded_from=STRANGER)
    assert w.fake.texts() == [t("forwarded")]
    async with w.db() as s:
        assert not await owner.paused(s)
        top = await s.scalar(sa.select(sa.func.max(ScoringWeight.version)))
    assert top == 1


async def test_each_card_answers_only_its_own_kind_of_button(w):
    async with w.db() as s:
        owner_card = await pending_card(
            s, await lead(s, 61), state="asked", by_telegram_id=MANAGER
        )
        manager_card = await pending_card(
            s,
            await lead(s, 62, state="hold"),
            kind="manager",
            state="asked",
            by_telegram_id=MANAGER,
            card_message_id=78,
        )
    # A manager's mh: on an owner card, the owner's rv:/mg: on a manager card.
    await w.press(cb.manager(owner_card.id, "c"), user=MANAGER)
    assert w.fake.answers() == [t("stale_card")]
    for data in (
        cb.review(manager_card.id, "p"),
        cb.review(manager_card.id, "o"),
        cb.review(manager_card.id, "m"),
        cb.merge(manager_card.id, uuid.UUID(int=7)),
    ):
        await w.press(data)
        assert w.fake.answers() == [t("stale_card")], data


async def test_a_letter_the_cards_layout_lacks_never_reaches_the_review_flow(
    db, configured
):
    seen: list[tuple[str, str | None]] = []

    async def answer(tap_: Any, _: Any) -> str:
        seen.append((tap_.data.kind, tap_.data.action))
        return "qabul"

    w = world(db, review_answer=answer)
    async with w.db() as s:
        normal = await pending_card(s, await lead(s, 61), state="asked")
        cand = await lead(s, 62)
        await s.execute(
            sa.update(Contact)
            .where(Contact.id == cand.contact_id)
            .values(shared_phone=True, telegram_username="namuna_tg")
        )
        cand.score_breakdown = {"gates": {"no_phone": True}}
        handoff = await pending_card(s, cand, state="asked", card_message_id=78)
    for data in (
        cb.review(normal.id, "h"),
        cb.review(handoff.id, "p"),
        cb.review(handoff.id, "e"),
    ):
        await w.press(data)
        assert w.fake.answers() == [t("stale_button")], data
    for data in (
        cb.review(normal.id, "p"),
        cb.review(normal.id, "e"),
        cb.review(handoff.id, "h"),
        cb.merge(normal.id, uuid.UUID(int=7)),
    ):
        await w.press(data)
        assert w.fake.answers() == ["qabul"], data
    assert seen == [("rv", "p"), ("rv", "e"), ("rv", "h"), ("mg", None)]


async def test_forbidding_auto_push_switches_it_off(w):
    async with w.db() as s:
        await s.execute(
            sa.update(Source)
            .where(Source.key == "telegram_monitor")
            .values(auto_push=True)
        )
    await w.press(cb.source("telegram_monitor", "a"))
    async with w.db() as s:
        row = await s.get(Source, "telegram_monitor", populate_existing=True)
        assert row is not None
        assert (row.config.get("auto_push_forbidden"), row.auto_push) == (True, False)


async def test_stir_the_company_holds_already_and_a_merged_holder(w):
    async with w.db() as s:
        own = await lead(s, 61, name="Namuna STIR Bir", stir="300000044")
        root = await gate_fixtures.firm(s, "Namuna Ildiz Savdo")
        await gate_fixtures.firm(
            s, "Namuna Eski Savdo", stir="300000055", state="merged", merged_into=root.id
        )
        other = await lead(s, 62, name="Namuna STIR Ikki")
        other_company = other.company_id
    await w.say(f"/stir {own.id} 300000044")
    assert w.last() == t("stir.same", name="Namuna STIR Bir", stir="300000044")
    # A merged company's old STIR is not a live holder.
    await w.say(f"/stir {other.id} 300000055")
    assert w.last() == t("stir.written", stir="300000055", name="Namuna STIR Ikki")
    async with w.db() as s:
        company = await s.get(Company, other_company, populate_existing=True)
        assert company is not None and company.stir == "300000055"


async def test_the_cancel_buttons_write_nothing(w):
    async with w.db() as s:
        cand = await lead(s, name="Namuna Bekor Savdo")
    await w.say("/dnc +998 90 000 00 61")
    [_yes, no] = w.buttons()
    await w.press(no)
    assert w.last() == t("dnc.cancelled")
    w.fake.files["f3"] = CSV
    await w.say(None, caption="/import", document=document("f3", "clients.csv", len(CSV)))
    [_yes, no] = w.buttons()
    await w.press(no)
    assert w.last() == t("import.cancelled")
    async with w.db() as s:
        written = await s.scalar(sa.select(sa.func.count()).select_from(Suppression))
        found = await s.get(LeadCandidate, cand.id, populate_existing=True)
    assert written == 0
    assert found is not None and found.state == "review"


async def test_push_tekshir_cancel_sends_nothing(erp):
    cand = await erp.lead()
    first = await erp.push(cand)
    w = world(session_scope, erp=lambda: contextlib.nullcontext(erp.erp))
    await w.say(f"/push tekshir {first.external_id}")
    [_yes, no] = w.buttons()
    await w.press(no)
    assert w.last() == t("push.cancelled")
    rows = await erp.rows()
    assert [(r.attempt, r.superseded_at) for r in rows] == [(1, None)]


async def test_push_tekshir_without_a_usable_erp_client(db, configured):
    @asynccontextmanager
    async def none() -> AsyncIterator[None]:
        yield None

    w = world(db, erp=none)
    await w.press(cb.push_check(7, "y"))
    assert w.fake.texts() == [t("push.no_erp")]
    # The tap is answered before the work, not after it.
    assert [name for name, _ in w.fake.calls] == [
        "AnswerCallbackQuery",
        "EditMessageText",
    ]


async def test_dnc_refuses_another_countrys_number(w):
    await w.say("/dnc +7 999 123 45 67")
    assert w.last() == t("dnc.foreign")
    assert w.buttons() == []


async def test_dnc_names_a_merged_holder_by_its_root(w):
    async with w.db() as s:
        root = await gate_fixtures.firm(s, "Namuna Ildiz Savdo")
        old = await gate_fixtures.firm(
            s, "Namuna Eski Nom", state="merged", merged_into=root.id
        )
        await gate_fixtures.contact(s, old, phone(61))
    await w.say("/dnc +998 90 000 00 61")
    assert w.last().splitlines()[:2] == [
        "🔒 DNC: 900000061",
        "Kompaniya: Namuna Ildiz Savdo",
    ]


async def test_qidir_names_the_suppression_that_holds_the_company(w):
    async with w.db() as s:
        await lead(s, name="Namuna Bostirilgan Savdo")
    await w.say("/dnc +998 90 000 00 61")
    await w.press(w.buttons()[0])
    async with w.db() as s:
        row = await s.scalar(sa.select(Suppression).where(Suppression.kind == "dnc"))
        assert row is not None and row.created_at is not None
        when = cards.day(row.created_at)
    await w.say("/qidir +998 90 000 00 61")
    assert w.last().splitlines()[-1] == (
        f"🚫 Bostirilgan: yozilmasin (DNC) (owner_bot, {when})"
    )


async def test_the_merge_menu_offers_at_most_five_targets(w):
    async with w.db() as s:
        cand = await lead(s, name="Namuna Birlashma Savdo")
        for n in range(6):
            twin = await gate_fixtures.firm(s, f"Zarafshon Texnika {n} Xyz")
            twin.dup_of = cand.company_id
            twin.dup_score = 90 - n
        review = await pending_card(s, cand, state="asked")
        await s.flush()
    await w.press(cb.review(review.id, "m"))
    buttons = w.buttons()
    assert len(buttons) == 6 and buttons[-1] == cb.review(review.id, "k")
    assert all(b.startswith(f"mg:{review.id}:") for b in buttons[:5])


async def test_holat_lists_the_enabled_sources_only(w):
    async with w.db() as s:
        await s.execute(sa.update(Source).values(enabled=False))
        await s.execute(
            sa.update(Source)
            .where(Source.key == "telegram_monitor")
            .values(enabled=True, last_run_at=None, paused_until=None, breaker_until=None)
        )
        await s.execute(
            sa.update(Source)
            .where(Source.key == "hh_vacancies")
            .values(enabled=True, last_run_at=None, paused_until=NOW + timedelta(hours=2))
        )
        titles = dict(
            (await s.execute(sa.select(Source.key, Source.title_uz))).tuples().all()
        )
    await w.say("/holat")
    lines = w.last().splitlines()
    start = lines.index("Manbalar:")
    end = next(i for i, line in enumerate(lines) if line.startswith("Muammolar:"))
    assert sorted(lines[start + 1 : end]) == sorted(
        [
            f"  {titles['telegram_monitor']} ✅ —",
            f"  {titles['hh_vacancies']} ⏸ —",
        ]
    )


async def test_xarajat_tells_the_day_from_the_month(w):
    from clientscrapper.db.models import UsageLog

    async with w.db() as s:
        for at, usd in ((NOW - timedelta(days=1), "1.50"), (NOW, "0.25")):
            s.add(
                UsageLog(
                    provider="anthropic",
                    operation="classify",
                    model="namuna",
                    cost_usd=Decimal(usd),
                    created_at=at,
                )
            )
        await s.flush()
    await w.say("/xarajat")
    day_cap, month_cap = settings.daily_cost_cap_usd, settings.monthly_cost_cap_usd
    assert w.last().splitlines()[0] == (
        f"💸 Xarajat — bugun $0.25 ({int(Decimal('0.25') * 100 / day_cap)}% / "
        f"{cards.money(day_cap)}) · oy $1.75 "
        f"({int(Decimal('1.75') * 100 / month_cap)}% / {cards.money(month_cap)}) · "
        "rad etilgan chaqiruvlar 0"
    )


async def test_to_person_refuses_a_group_even_when_it_is_configured(monkeypatch):
    """A negative id is a group or a channel: never a person, whatever the
    .env says."""
    monkeypatch.setattr(settings, "owner_telegram_id", -100500)
    monkeypatch.setattr(settings, "manager_telegram_ids", (-100600,))
    bot, fake = fake_bot()
    for chat in (-100500, -100600):
        with pytest.raises(deliver.NotAllowed):
            await deliver.to_person(bot, chat, "salom")
    assert fake.calls == []


def test_a_long_reply_is_clipped_as_valid_html():
    # The cut keeps 4,063 characters: inside <code> → the tag is closed …
    text = "<b>Sarlavha</b>\n" + "a" * 4000 + "<code>" + "b" * 100 + "</code> oxir"
    clipped = deliver.clip(text)
    assert len(clipped) <= deliver.TELEGRAM_MESSAGE_MAX
    assert clipped == text[:4063] + "…</code>"
    # … and never inside an entity or a tag.
    assert deliver.clip("x" * 4060 + "&amp;" + "y" * 100) == "x" * 4060 + "…"
    assert deliver.clip("x" * 4060 + "<code>" + "y" * 100) == "x" * 4060 + "…"
    assert deliver.clip("x" * 4058 + "&amp;" + "y" * 100) == "x" * 4058 + "&amp;…"
    assert deliver.clip("qisqa <b>matn</b>") == "qisqa <b>matn</b>"


async def test_a_reply_lost_on_the_network_is_not_sent_twice(configured):
    bot, fake = fake_bot()
    fake.fail("SendMessage", TimeoutError("network"))
    from clientscrapper.bot.handlers.common import reply

    update = message(bot, "/holat", user_id=OWNER)
    assert update.message is not None
    assert await reply(update.message, "<b>salom</b>") is None
    assert len(fake.named("SendMessage")) == 1


# --- the process wiring (06 §1.1) -------------------------------------------------------


def test_the_bot_wires_the_seams_it_judges_with(configured):
    from clientscrapper.services import costs, erp_feeds

    bot, _fake = fake_bot()
    before = (costs._hooks.notify_cap, costs._hooks.hold_cap, costs._hooks.anthropic_down)
    probe = health._probes.suppression
    try:
        notify = bot_main.wire(bot)
        assert costs._hooks.notify_cap is notify
        assert costs._hooks.hold_cap is health.in_quiet_hours
        assert costs._hooks.anthropic_down is health.anthropic_down_until
        assert health._probes.suppression is erp_feeds.suppression_probe
    finally:
        costs.set_cap_notifier(before[0], hold=before[1])
        costs.set_anthropic_health(before[2])
        health.set_suppression_probe(probe)


async def test_polling_keeps_what_the_owner_sent_meanwhile():
    seen: dict[str, Any] = {}

    class Polling:
        async def start_polling(self, bot: Any, **kw: Any) -> None:
            seen.update(kw)

    bot, _ = fake_bot()
    await bot_main._poll(Polling(), bot)  # type: ignore[arg-type]
    assert seen == {"drop_pending_updates": False, "handle_signals": False}


def _failing(exc: Exception) -> Callable[[], AbstractAsyncContextManager[Any]]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[Any]:
        raise exc
        yield  # pragma: no cover

    return factory


@pytest.mark.parametrize(
    ("exc", "word"),
    [
        (sa.exc.OperationalError("SELECT 1", {}, Exception("down")), "db_down"),
        (OSError("connection refused"), "db_down"),
        (sa.exc.IntegrityError("INSERT", {}, Exception("dup")), "error"),
        (RuntimeError("bug"), "error"),
    ],
)
async def test_a_failing_handler_is_answered_never_silent(configured, exc, word):
    bot, fake = fake_bot()
    dp = bot_main.build_dispatcher(Deps(session_factory=_failing(exc), clock=lambda: NOW))
    await dp.feed_update(bot, tap(bot, cb.resume("y"), user_id=OWNER))
    [answered] = fake.named("AnswerCallbackQuery")
    assert (answered["text"], answered["show_alert"]) == (t(word), True)
    fake.clear()
    await dp.feed_update(bot, message(bot, "/toxtat", user_id=OWNER))
    assert fake.texts() == [t(word)]


async def test_an_error_after_the_tap_was_answered_is_said_in_the_chat(configured):
    bot, fake = fake_bot()
    refused = TelegramBadRequest(
        method=SendMessage(chat_id=OWNER, text="x"), message="query is too old"
    )
    fake.fail("AnswerCallbackQuery", refused, after=1)
    dp = bot_main.build_dispatcher(
        Deps(clock=lambda: NOW, erp=_failing(RuntimeError("erp")))
    )
    await dp.feed_update(bot, tap(bot, cb.push_check(7, "y"), user_id=OWNER))
    assert fake.texts() == [t("error")]


async def test_the_worker_hands_the_backup_to_the_owner_through_the_bot(
    configured, monkeypatch, tmp_path
):
    from clientscrapper.worker import main as worker

    bot, fake = fake_bot()
    monkeypatch.setattr(deliver, "make_bot", lambda token=None: bot)
    connected = worker.connect_owner_bot()
    piece = tmp_path / "zaxira.age"
    piece.write_bytes(b"x")
    try:
        send = worker._seams.send_document
        assert send is not None
        assert await send(piece, "zaxira") is True
        [sent] = fake.named("SendDocument")
        assert (sent["chat_id"], sent["caption"]) == (OWNER, "zaxira")
    finally:
        await worker.disconnect_owner_bot(connected)
    assert worker._seams.send_document is None


async def test_mark_asked_takes_only_a_pending_card(db):
    """A card already out keeps the message it went out as."""
    async with db() as s:
        review = await pending_card(s, await lead(s), state="asked")
        args = {"chat_id": OWNER, "message_id": 99, "card_hash": "1" * 64}
        assert not await cards.mark_asked(s, review.id, now=NOW, **args)
        row = await s.get(LeadReview, review.id, populate_existing=True)
        assert row is not None and (row.card_message_id, row.card_hash) == (77, "0" * 64)


async def test_the_erp_client_of_a_tap_is_closed_and_a_refused_url_is_none(
    monkeypatch,
):
    from clientscrapper.bot.handlers import common

    monkeypatch.setattr(settings, "crm_agent_key", "namuna-kalit")
    monkeypatch.setattr(settings, "crm_base_url", "http://erp.test")
    async with common._erp() as refused:
        assert refused is None  # the key is never sent in clear
    monkeypatch.setattr(settings, "crm_base_url", "https://erp.test")
    async with common._erp() as client:
        assert client is not None and not client.client.is_closed
    assert client.client.is_closed
    monkeypatch.setattr(settings, "crm_agent_key", "")
    async with common._erp() as keyless:
        assert keyless is None
