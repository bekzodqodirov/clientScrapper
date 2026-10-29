# VENDORED_FROM dailyJournal@6b1f67f miya/bot/handlers.py
"""``/lidlar`` and the review card's buttons (06 §1.2, §1.3).

``/lidlar`` sends the next ≤ ``REVIEW_BURST_MAX`` pending owner cards now —
the owner asked, so ``REVIEW_HOURS``, ``QUIET_HOURS`` and the daily cap do
not apply; the global pause does (``/toxtat``: no card until ``/yoq``) —
through the review flow's own loop (``reviews.send_owner_cards``, which the
worker's ``review_cards_job`` runs too): each card claimed, rendered with
the gates evaluated at that moment, sent, marked ``asked``; a card Telegram
did not take stays ``pending``; a card that cannot be shown leaves the
queue.

The buttons: ⋯ / ◀️ swap the keyboard in place, 🔀 lists ≤ 5 merge targets
(the resolver's own ``suggest_merge_targets``). The answers — ✅ ✖️ ⏭ 🔒 🙍
🚚 👤 and a chosen merge target — are ``reviews.answer``'s: the card is then
edited in place, its keyboard removed, one footer line appended (06 §1.3);
a refusal (no usable phone, no manager, a merge two STIRs forbid) is an
alert and the card stays. ✏️ waits 5 minutes for the owner's next message
(``owner_text``: the phone, ``reviews.edit_phone``) and re-renders the card.
A letter the card's layout does not carry (✅ on the handoff card, 👤 on the
normal one) is «Bu tugma eskirgan». A tap on a card that no longer waits
(answered, expired, its candidate moved on) is «Bu karta eskirgan —
/lidlar», and its buttons go.
"""

from __future__ import annotations

import html
import logging
import uuid
from typing import Final

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from clientscrapper.bot import callbacks as cb
from clientscrapper.bot import keyboards, pages
from clientscrapper.bot.cards_out import BotCards
from clientscrapper.bot.handlers.common import (
    Deps,
    Tap,
    edit,
    edit_keyboard,
    reply,
    toast,
)
from clientscrapper.bot.texts import UZ, t
from clientscrapper.config import settings
from clientscrapper.services import cards, owner, reviews

log = logging.getLogger(__name__)

COMMAND: Final = Command("lidlar")


async def lidlar(message: Message, command: CommandObject, deps: Deps) -> None:
    if (command.args or "").strip().lower() == "holat":
        await lidlar_holat(message, deps)
        return
    now = deps.clock()
    bot = message.bot
    assert bot is not None
    async with deps.session_factory() as session:
        if await owner.paused(session):
            # 06 §1.2 /toxtat: «… no push, no card …» until /yoq.
            await reply(message, t("lidlar.paused"))
            return
        # A candidate that reached review since the card job last ran has no
        # card row yet: /lidlar makes it (the job's own step), so it is not
        # invisible here — nor for good while the worker is down.
        await reviews.ensure_pending(session, now=now)
        await session.commit()
        burst = await reviews.send_owner_cards(
            session, BotCards(bot), now=now, limit=settings.review_burst_max
        )
        waiting = await cards.pending_owner_count(session)
    if burst.failed:
        await reply(message, t("lidlar.send_failed"))
    elif burst.sent == 0 and burst.skipped == 0:
        await reply(message, t("lidlar.none"))
        return
    if burst.skipped:
        await reply(message, t("lidlar.skipped", n=burst.skipped))
    if waiting:
        await reply(message, t("lidlar.more", n=waiting))


async def lidlar_holat(message: Message, deps: Deps) -> None:
    async with deps.session_factory() as session:
        holat = await cards.lidlar_holat(session)
    await reply(message, pages.lidlar_holat_text(holat, deps.clock()))


# --- the answers ---------------------------------------------------------------------


def refusal_text(code: str, *, manager: bool = False) -> str:
    """A ``reviews.Refused`` code in the owner's (or the manager's) words; a
    manager is never pointed at ``/lidlar`` (06 §1.7: the owner's alone)."""
    if code.startswith("merge:"):
        reason = code.split(":", 1)[1]
        key = f"review.merge.{reason}"
        why = UZ.get(key, reason)
        return t("review.refused.merge", why=why)
    if manager and code == "stale":
        return t("stale_card_manager")
    key = f"review.refused.{code}"
    return UZ[key] if key in UZ else t("stale_button")


def stale_text(tap: Tap) -> str:
    """«Bu karta eskirgan»: with ``/lidlar`` for the owner, without it for a
    manager's card (``mh:``)."""
    return t("stale_card_manager" if tap.data.kind == "mh" else "stale_card")


def _plain(line: str) -> str:
    """A footer as a button's answer: a toast shows no HTML."""
    return html.unescape(line)


async def _send_handoff(tap: Tap, manager_review_id: int) -> str | None:
    """👤: the manager card goes out at once inside the hours (06 §1.6) and
    not while ``/toxtat`` holds (``send_manager_card``), else the card job
    sends it and edits the owner's card then; the footer names the manager
    by first name (``reviews.handoff_line``), never by id."""
    now = tap.deps.clock()
    bot = tap.query.bot
    if bot is None or not reviews.sending_hours(now):
        return None
    async with tap.deps.session_factory() as session:
        out = await reviews.send_manager_card(
            session, manager_review_id, BotCards(bot), now=now
        )
        await session.commit()
        return await reviews.handoff_line(session, manager_review_id, out)


async def apply_answer(tap: Tap, code: str, *, target: uuid.UUID | None = None) -> None:
    """One answer of the owner's or a manager's card: applied, then the card
    edited in place — keyboard removed, the footer appended (06 §1.3)."""
    review_id = tap.data.ref
    assert review_id is not None
    now = tap.deps.clock()
    message = tap.query.message
    shown = message.html_text if isinstance(message, Message) else None
    async with tap.deps.session_factory() as session:
        try:
            done = await reviews.answer(
                session, review_id, code, by=tap.user_id, now=now, target=target
            )
        except reviews.Refused as refused:
            await session.rollback()
            text = refusal_text(refused.code, manager=tap.data.kind == "mh")
            await toast(tap.query, text, alert=True)
            return
        await session.commit()
    if done.stale or done.footer is None:
        await edit_keyboard(tap.query, None)
        await toast(tap.query, stale_text(tap), alert=True)
        return
    line = done.footer
    if done.manager_review_id is not None:
        line = await _send_handoff(tap, done.manager_review_id) or line
    async with tap.deps.session_factory() as session:
        text = await reviews.tapped_text(session, review_id, line, now=now, shown=shown)
        await session.commit()
    await edit(tap.query, text)
    await toast(tap.query, _plain(line))


async def on_answer(tap: Tap) -> None:
    """✅ ✖️ ⏭ 🔒 🙍 🚚 ✏️ 👤 and 🔀's target — on a live owner card, and
    only a letter that card's layout carries."""
    review_id = tap.data.ref
    assert review_id is not None
    async with tap.deps.session_factory() as session:
        review = await cards.live_review(session, review_id)
        if review is None or review.kind != "owner":
            await toast(tap.query, t("stale_card"), alert=True)
            return
        layout = await cards.card_layout(session, review)
    letter = "m" if tap.data.kind == "mg" else tap.data.action
    if letter is None or letter not in keyboards.layout_actions(layout):
        await toast(tap.query, t("stale_button"))
        return
    if letter == "m":
        target = uuid.UUID(tap.data.target) if tap.data.target else None
        await apply_answer(tap, "merge", target=target)
        return
    code = cb.REVIEW_ANSWERS[letter]
    if code == "edit_phone":
        tap.deps.phones.put(tap.user_id, review_id, now=tap.deps.clock())
        await toast(tap.query, t("review.phone_ask"), alert=True)
        return
    await apply_answer(tap, code)


async def phone_answer(message: Message, deps: Deps, review_id: int) -> None:
    """✏️'s second step: the owner's message is the card's phone; the card
    is re-rendered in place (its buttons stay — it still waits)."""
    now = deps.clock()
    user = message.from_user.id if message.from_user else 0
    async with deps.session_factory() as session:
        try:
            await reviews.edit_phone(
                session, review_id, message.text or "", by=user, now=now
            )
        except reviews.Refused as refused:
            await session.rollback()
            await reply(message, refusal_text(refused.code))
            return
        await session.commit()
    bot = message.bot
    async with deps.session_factory() as session:
        view = await cards.review_view(session, review_id, now=now)
        review = await cards.live_review(session, review_id)
        card: cards.Card | None = None
        if view is not None:
            try:
                card = cards.render_review(view)
            except cards.CardRefused:
                card = None
        if card is not None and review is not None and bot is not None:
            chat, shown = review.card_chat_id, review.card_message_id
            text = cards.with_footer(card.text, cards.footer("edit_phone"))
            if chat is not None and shown is not None:
                sent = await BotCards(bot).edit(
                    chat, shown, text, card.keyboard, review_id
                )
                if sent:
                    # The card's own hash, footer aside: the next refresh
                    # edits it only when the card itself changed.
                    await reviews.store_hash(session, review_id, card.hash)
                    await session.commit()
    await reply(message, t("review.phone_done"))


# --- the card's menus --------------------------------------------------------------


async def on_more(tap: Tap) -> None:
    await _swap(tap, expanded=True)


async def on_back(tap: Tap) -> None:
    await _swap(tap, expanded=False)


async def _swap(tap: Tap, *, expanded: bool) -> None:
    review_id = tap.data.ref
    assert review_id is not None
    async with tap.deps.session_factory() as session:
        review = await cards.live_review(session, review_id)
        if review is None or review.kind != "owner":
            await toast(tap.query, t("stale_card"), alert=True)
            return
        layout = await cards.card_layout(session, review)
    await edit_keyboard(
        tap.query, keyboards.review_keyboard(review_id, layout, expanded=expanded)
    )
    await toast(tap.query)


async def on_merge_menu(tap: Tap) -> None:
    review_id = tap.data.ref
    assert review_id is not None
    async with tap.deps.session_factory() as session:
        review = await cards.live_review(session, review_id)
        if review is None or review.kind != "owner":
            await toast(tap.query, t("stale_card"), alert=True)
            return
        targets = await cards.merge_targets(session, review)
    if not targets:
        await toast(tap.query, t("review.no_targets"), alert=True)
        return
    await edit_keyboard(tap.query, keyboards.merge_keyboard(review_id, targets))
    await toast(tap.query, t("review.merge_title"))


__all__ = [
    "COMMAND",
    "apply_answer",
    "lidlar",
    "lidlar_holat",
    "on_answer",
    "on_back",
    "on_merge_menu",
    "on_more",
    "phone_answer",
    "refusal_text",
    "stale_text",
]
