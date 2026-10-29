"""Builders for the review-flow tests (T-21): a candidate the gates pass with a
cited quote, its cards, a card sender that records instead of sending, and
a bot world over the real dispatcher.

Synthetic data only (07 §1.0): «Namuna …», +998 90 000 00 NN, namuna_<n>.
``NOW`` is gate_fixtures' Tuesday 11:00 Tashkent — inside ``REVIEW_HOURS``
(Mon–Sat 09:00–18:00) and the push window.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.bot import main as bot_main
from clientscrapper.bot.handlers.common import Deps, SessionFactory
from clientscrapper.bot.texts import t
from clientscrapper.config import settings
from clientscrapper.db.models import (
    Company,
    Contact,
    Evidence,
    LeadCandidate,
    LeadReview,
    Signal,
)
from clientscrapper.services.cards import CardLayout
from clientscrapper.services.reviews import SentCard
from tests import gate_fixtures
from tests.fake_bot import FakeSession, fake_bot, message, tap
from tests.fixtures.candidate_views import EXCERPT
from tests.gate_fixtures import NOW, phone
from tests.test_uz_text import problems

OWNER = 1001
MANAGER = 2002
OTHER_MANAGER = 2003
STRANGER = 3003
TOKEN = "42:TEST"
_IDS = itertools.count(700)


def configure(monkeypatch: pytest.MonkeyPatch, *, managers: tuple[int, ...]) -> None:
    monkeypatch.setattr(settings, "assistant_bot_token", TOKEN)
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER)
    monkeypatch.setattr(settings, "manager_telegram_ids", managers)


async def lead(
    s: AsyncSession,
    nn: int = 61,
    *,
    state: str = "review",
    person: bool = False,
    username: str | None = None,
    phone_number: str | None = "",
    fields: dict[str, Any] | None = None,
    **firm: Any,
) -> LeadCandidate:
    """A company that passes every gate, with a cited «5 kub mato kerak» from
    Xitoy two days ago, its candidate (``state``, score 72) and its lead
    contact: a company-tier mobile, or — ``person`` — the poster, a person
    with ``username`` (and no phone unless ``phone_number`` gives one).
    ``fields`` go to the candidate, ``firm`` to the company."""
    owner = await gate_fixtures.firm(s, **firm)
    number = phone(nn) if phone_number == "" else phone_number
    if person:
        contact = await gate_fixtures.contact(
            s,
            owner,
            number,
            tier="person",
            full_name="Rustam Karimov",
            role="direktori",
            telegram_username=username,
            source_url=f"https://t.me/namuna_optom/{nn}",
        )
    else:
        contact = await gate_fixtures.contact(
            s, owner, number, telegram_username=username
        )
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
                company_id=owner.id,
                evidence_id=ev.id,
                key=key,
                value=value,
                quote=quote,
                observed_at=NOW - timedelta(days=2),
                source_key="telegram_monitor",
            )
        )
    cand = await gate_fixtures.candidate(
        s, owner, state=state, lead=contact, score=72, **(fields or {})
    )
    await s.flush()
    return cand


async def card(s: AsyncSession, cand: LeadCandidate, **fields: Any) -> LeadReview:
    """A review row of ``cand``: ``pending`` by default; ``asked`` with a
    message, a hash and the 72-hour expiry."""
    fields.setdefault("kind", "owner")
    fields.setdefault("state", "pending")
    if fields["state"] == "asked":
        fields.setdefault("asked_at", NOW)
        fields.setdefault("card_chat_id", fields.get("by_telegram_id") or OWNER)
        fields.setdefault("card_message_id", next(_IDS))
        fields.setdefault("card_hash", "0" * 64)
        fields.setdefault("expires_at", NOW + timedelta(hours=72))
    if fields["state"] in ("approved", "declined", "merged"):
        fields.setdefault("answered_at", NOW)
    review = LeadReview(candidate_id=cand.id, **fields)
    s.add(review)
    await s.flush()
    return review


async def fresh(s: AsyncSession, row: Any) -> Any:
    """``row`` read again — by its identity, which survives a rollback that
    expired every attribute (reading ``row.id`` then would load it)."""
    identity = sa.inspect(row).identity
    assert identity is not None
    found = await s.get(type(row), identity[0], populate_existing=True)
    assert found is not None
    return found


async def reviews_of(s: AsyncSession, cand: LeadCandidate) -> list[LeadReview]:
    return list(
        await s.scalars(
            sa.select(LeadReview)
            .where(LeadReview.candidate_id == cand.id)
            .order_by(LeadReview.id)
            .execution_options(populate_existing=True)
        )
    )


async def company_of(s: AsyncSession, cand: LeadCandidate) -> Company:
    found = await s.get(Company, cand.company_id, populate_existing=True)
    assert found is not None
    return found


async def contacts_of(s: AsyncSession, cand: LeadCandidate) -> list[Contact]:
    return list(
        await s.scalars(
            sa.select(Contact)
            .where(Contact.company_id == cand.company_id)
            .order_by(Contact.created_at, Contact.id)
            .execution_options(populate_existing=True)
        )
    )


@dataclass
class FakeSender:
    """``reviews.CardSender`` that records: every card sent, edited and
    stripped of its buttons; ``fail`` makes Telegram refuse the sends,
    ``refuse`` the sends to those chats only (a manager who blocked the
    bot), and — like ``deliver.to_person`` — a chat that is neither the
    owner's nor a listed manager's is never sent to."""

    sent: list[tuple[int, str, CardLayout, int]] = field(default_factory=list)
    edits: list[tuple[int, int, str, CardLayout | None, int]] = field(
        default_factory=list
    )
    strips: list[tuple[int, int]] = field(default_factory=list)
    fail: bool = False
    names: dict[int, str] = field(default_factory=dict)
    refuse: set[int] = field(default_factory=set)
    # Every send tried, taken or not: (chat, review id).
    tried: list[tuple[int, int]] = field(default_factory=list)

    async def send(
        self, telegram_id: int, text: str, layout: CardLayout, review_id: int
    ) -> SentCard | None:
        self.tried.append((telegram_id, review_id))
        listed = telegram_id == settings.owner_telegram_id or (
            telegram_id in settings.manager_telegram_ids
        )
        if self.fail or telegram_id in self.refuse or not listed:
            return None
        self.sent.append((telegram_id, text, layout, review_id))
        return SentCard(telegram_id, next(_IDS), self.names.get(telegram_id))

    async def edit(
        self,
        telegram_id: int,
        message_id: int,
        text: str,
        layout: CardLayout | None,
        review_id: int,
    ) -> bool:
        self.edits.append((telegram_id, message_id, text, layout, review_id))
        return True

    async def strip(self, telegram_id: int, message_id: int) -> bool:
        self.strips.append((telegram_id, message_id))
        return True

    def texts(self) -> list[str]:
        return [text for _, text, _, _ in self.sent]


@dataclass
class World:
    """The real dispatcher over the fake Bot API (``tests/fake_bot``)."""

    bot: Bot
    fake: FakeSession
    dp: Dispatcher
    deps: Deps

    async def say(self, text: str | None, *, user: int = OWNER) -> None:
        self.fake.clear()
        await self.dp.feed_update(self.bot, message(self.bot, text, user_id=user))
        self.check()

    async def press(self, data: str, *, user: int = OWNER, **kw: Any) -> None:
        self.fake.clear()
        await self.dp.feed_update(self.bot, tap(self.bot, data, user_id=user, **kw))
        self.check()

    def check(self) -> None:
        said = [*self.fake.texts(), *(a for a in self.fake.answers() if a)]
        assert t("error") not in said
        assert [x for x in said if problems(x, rendered=True)] == []
        assert self.fake.chats() <= {OWNER, MANAGER, OTHER_MANAGER}

    def buttons(self) -> list[str]:
        sent = self.fake.named("SendMessage", "EditMessageText", "EditMessageReplyMarkup")
        markup = sent[-1].get("reply_markup") or {"inline_keyboard": []}
        return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


def world(factory: SessionFactory, *, clock: datetime = NOW) -> World:
    bot, fake = fake_bot()
    deps = Deps(session_factory=factory, clock=lambda: clock)
    return World(bot, fake, bot_main.build_dispatcher(deps), deps)
