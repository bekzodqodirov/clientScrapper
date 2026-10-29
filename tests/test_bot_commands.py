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
from clientscrapper.bot.texts import t
from clientscrapper.config import settings
from clientscrapper.db.models import (
    ChatMonitor,
    Company,
    Evidence,
    LeadCandidate,
    LeadReview,
    RuntimeFlag,
    ScoringWeight,
    Signal,
    Source,
    Suppression,
)
from clientscrapper.db.session import engine, session_scope
from clientscrapper.services import cards, chats, crm_push, health, owner
from tests import gate_fixtures
from tests.fake_bot import FakeSession, added_to_group, document, fake_bot, message, tap
from tests.fixtures.candidate_views import EXCERPT
from tests.gate_fixtures import NOW, phone
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
        assert found is not None and found.state == "hold"


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
    assert "✅ 👥 Namuna optom guruh · 0 signal (7 kun) / 40 xabar" in w.last()
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


async def test_unut_is_not_ready(w):
    await w.say("/unut 300000011")
    assert w.last() == t("unut.not_ready")


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
    w = world(session_scope, erp=lambda: erp.erp)
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
