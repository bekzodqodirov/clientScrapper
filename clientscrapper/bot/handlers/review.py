# VENDORED_FROM dailyJournal@6b1f67f miya/bot/handlers.py
"""``/lidlar`` and the review card's buttons (06 §1.2, §1.3).

``/lidlar`` sends the next ≤ ``REVIEW_BURST_MAX`` pending owner cards now —
the owner asked, so ``REVIEW_HOURS`` and ``QUIET_HOURS`` do not apply; the
global pause does (``/toxtat``: no card until ``/yoq``) — in
06 §1.6's order, each rendered by ``services/cards`` with the gates
evaluated at that moment. A card the gates now refuse is not shown: its
candidate takes the score job's own walk out of ``review``
(``cards.reroute``). A card goes out first and is marked ``asked`` after
(``mark_asked``: the message, its hash, ``expires_at``), so a card Telegram
did not take stays ``pending``.

The buttons: ⋯ / ◀️ swap the keyboard in place, 🔀 lists ≤ 5 merge targets
(the resolver's own ``suggest_merge_targets``). The answers themselves —
✅ ✖️ ⏭ 🔒 🙍 🚚 ✏️ 👤 and a chosen merge target — are the review flow's
(T-21, ``services/reviews``): until it is wired (``Deps.review_answer``) a
tap is answered «not ready» and nothing changes. A tap on a card that no
longer waits (answered, expired) is «Bu karta eskirgan — /lidlar».
"""

from __future__ import annotations

import logging
from typing import Final

from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from clientscrapper.bot import keyboards, pages
from clientscrapper.bot.handlers.common import Deps, Tap, edit_keyboard, reply, toast
from clientscrapper.bot.texts import t
from clientscrapper.config import settings
from clientscrapper.services import cards, owner

log = logging.getLogger(__name__)

COMMAND: Final = Command("lidlar")


async def lidlar(message: Message, command: CommandObject, deps: Deps) -> None:
    if (command.args or "").strip().lower() == "holat":
        await lidlar_holat(message, deps)
        return
    now = deps.clock()
    sent = skipped = 0
    async with deps.session_factory() as session:
        if await owner.paused(session):
            # 06 §1.2 /toxtat: «… no push, no card …» until /yoq.
            await reply(message, t("lidlar.paused"))
            return
        ids = await cards.next_owner_reviews(session, limit=settings.review_burst_max)
        for review_id in ids:
            view = await cards.review_view(session, review_id, now=now)
            if view is None:
                skipped += 1
                continue
            try:
                card = cards.render_review(view)
            except cards.CardRefused as refused:
                skipped += 1
                if refused.code == "gate":
                    await cards.reroute(
                        session, view.candidate.candidate_id, view.gate, now=now
                    )
                log.info(
                    "card not shown", extra={"review_id": review_id, "code": refused.code}
                )
                continue
            out = await reply(
                message,
                card.text,
                keyboard=keyboards.review_keyboard(review_id, card.keyboard),
            )
            if out is None:
                await reply(message, t("lidlar.send_failed"))
                break
            await cards.mark_asked(
                session,
                review_id,
                chat_id=out.chat.id,
                message_id=out.message_id,
                card_hash=card.hash,
                now=now,
            )
            await session.commit()
            sent += 1
        waiting = await cards.pending_owner_count(session)
    if sent == 0 and skipped == 0:
        await reply(message, t("lidlar.none"))
        return
    if skipped:
        await reply(message, t("lidlar.skipped", n=skipped))
    if waiting:
        await reply(message, t("lidlar.more", n=waiting))


async def lidlar_holat(message: Message, deps: Deps) -> None:
    async with deps.session_factory() as session:
        holat = await cards.lidlar_holat(session)
    await reply(message, pages.lidlar_holat_text(holat, deps.clock()))


# --- the card's buttons --------------------------------------------------------------


async def _live(tap: Tap) -> bool:
    """The card still waits for its answer and is the owner's."""
    review_id = tap.data.ref
    if review_id is None:
        return False
    async with tap.deps.session_factory() as session:
        review = await cards.live_review(session, review_id)
    return review is not None and review.kind == "owner"


async def on_answer(tap: Tap) -> None:
    """✅ ✖️ ⏭ 🔒 🙍 🚚 ✏️ 👤 and 🔀's target: the review flow's (T-21)."""
    if not await _live(tap):
        await toast(tap.query, t("stale_card"), alert=True)
        return
    answer = tap.deps.review_answer
    if answer is None:
        await toast(tap.query, t("review.not_ready"), alert=True)
        return
    said = await answer(tap, None)
    await toast(tap.query, said)


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
    "lidlar",
    "lidlar_holat",
    "on_answer",
    "on_back",
    "on_merge_menu",
    "on_more",
]
