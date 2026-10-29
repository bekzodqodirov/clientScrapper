# VENDORED_FROM dailyJournal@6b1f67f miya/bot/handlers.py
"""What a manager reaches (06 §1.1, §1.7) and the commands the owner shares
with them: ``/start``, ``/yordam``, ``/qidir``, and the buttons ``mh:*`` of
the manager's OWN cards (``lead_reviews.by_telegram_id`` must be the
presser's — another manager's card answers «not yours»).

The manager card's answers (📞 🙅 ❌ ⏭) are the review flow's (T-21,
``Deps.review_answer``); unwired, a tap is answered «not ready». ``/tel`` is
T-21's too. A manager never sees ``/lidlar``, ``/manbalar``, ``/unut`` or a
health alert: those are on the owner's router alone.
"""

from __future__ import annotations

from aiogram.types import Message

from clientscrapper.bot.handlers.common import (
    Deps,
    Tap,
    args_of,
    is_owner,
    reply,
    toast,
)
from clientscrapper.bot.texts import t
from clientscrapper.db.enums import CANDIDATE_STATES
from clientscrapper.services import cards


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
    """📞 🙅 ❌ ⏭ on the manager's own card."""
    review_id = tap.data.ref
    assert review_id is not None
    async with tap.deps.session_factory() as session:
        review = await cards.live_review(session, review_id)
    if review is None or review.kind != "manager":
        await toast(tap.query, t("stale_card"), alert=True)
        return
    if review.by_telegram_id != tap.user_id:
        await toast(tap.query, t("not_yours"), alert=True)
        return
    answer = tap.deps.review_answer
    if answer is None:
        await toast(tap.query, t("review.not_ready"), alert=True)
        return
    said = await answer(tap, None)
    await toast(tap.query, said)


__all__ = ["on_answer", "qidir", "start", "yordam"]
