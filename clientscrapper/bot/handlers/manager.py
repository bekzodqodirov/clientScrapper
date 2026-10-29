# VENDORED_FROM dailyJournal@6b1f67f miya/bot/handlers.py
"""What a manager reaches (06 §1.1, §1.7) and the commands the owner shares
with them: ``/start``, ``/yordam``, ``/qidir``, ``/tel``, and the buttons
``mh:*`` of the manager's OWN cards (``lead_reviews.by_telegram_id`` must be
the presser's — another manager's card answers «not yours»).

The manager card's answers (📞 🙅 ❌ ⏭) are the review flow's
(``reviews.answer``, the owner card's own path); ``/tel <karta> +998…`` is
``reviews.tel``: the phone the prospect gave, stored with its legal basis,
the candidate re-scored at once. A manager never sees ``/lidlar``,
``/manbalar``, ``/unut`` or a health alert: those are on the owner's router
alone.
"""

from __future__ import annotations

from aiogram.types import Message

from clientscrapper.bot import callbacks as cb
from clientscrapper.bot.handlers.common import (
    Deps,
    Tap,
    args_of,
    is_owner,
    reply,
    toast,
)
from clientscrapper.bot.handlers.review import apply_answer
from clientscrapper.bot.texts import UZ, t
from clientscrapper.db.enums import CANDIDATE_STATES
from clientscrapper.services import cards, reviews


async def start(message: Message) -> None:
    user = message.from_user.id if message.from_user else None
    await reply(message, t("start_owner" if is_owner(user) else "start_manager"))


async def yordam(message: Message) -> None:
    user = message.from_user.id if message.from_user else None
    await reply(message, t("help_owner" if is_owner(user) else "help_manager"))


def _state_words() -> dict[str, str]:
    return {state: t(f"state.{state}") for state in CANDIDATE_STATES}


async def qidir(message: Message, deps: Deps) -> None:
    """Read-only: a phone or tail-9, @username, STIR, domain or a name
    (≤ 5 trigram matches) to company cards (06 §1.2). Creates nothing."""
    query = args_of(message)
    if not query:
        await reply(message, t("qidir.how"))
        return
    async with deps.session_factory() as session:
        found = await cards.search(session, query, now=deps.clock())
    if not found:
        await reply(message, t("qidir.none", query=cards.esc(query)))
        return
    words = _state_words()
    for card in found:
        await reply(message, cards.render_company(card, state_words=words))


async def on_answer(tap: Tap) -> None:
    """📞 🙅 ❌ ⏭ on the manager's own card. Whose card it is, is
    ``reviews.answer``'s question (``Refused("not_yours")``), asked under the
    candidate's lock; this only turns away a card that no longer waits."""
    review_id = tap.data.ref
    assert review_id is not None
    async with tap.deps.session_factory() as session:
        review = await cards.live_review(session, review_id)
    if review is None or review.kind != "manager":
        await toast(tap.query, t("stale_card"), alert=True)
        return
    action = tap.data.action
    if action is None or action not in cb.MANAGER_ANSWERS:
        await toast(tap.query, t("stale_button"))
        return
    await apply_answer(tap, cb.MANAGER_ANSWERS[action])


async def tel(message: Message, deps: Deps) -> None:
    """``/tel <karta> +998…`` (06 §1.7): the manager's own card only."""
    head, _, raw = args_of(message).partition(" ")
    if not (head.isascii() and head.isdigit()) or len(head) > 19 or not raw.strip():
        await reply(message, t("tel.how"))
        return
    review_id = int(head)
    if review_id > cb.BIGINT_MAX:
        await reply(message, t("tel.how"))
        return
    user = message.from_user.id if message.from_user else 0
    async with deps.session_factory() as session:
        try:
            done = await reviews.tel(session, review_id, raw, by=user, now=deps.clock())
        except reviews.Refused as refused:
            await session.rollback()
            key = f"tel.{refused.code}"
            await reply(message, UZ[key] if key in UZ else t("tel.stale"))
            return
        await session.commit()
    await reply(message, t("tel.done" if done.approved else "tel.held", score=done.score))


__all__ = ["on_answer", "qidir", "start", "tel", "yordam"]
