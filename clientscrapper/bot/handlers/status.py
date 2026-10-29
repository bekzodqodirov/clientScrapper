# VENDORED_FROM dailyJournal@6b1f67f miya/bot/handlers.py
"""``/holat`` and ``/xarajat`` (06 §1.4): read-only, over ``health.gather``
(with the stored evidence size), ``metrics.holat`` (the pilot's numbers, 07
§3.2) and ``costs.report``; the layouts are ``services/cards``'. The
problems are ``health.problems`` — the same texts, remedy included, the
sweep tells. Numbers that cannot be read leave their block out; the status
still answers.
"""

from __future__ import annotations

import logging

from aiogram.types import Message

from clientscrapper.bot.handlers.common import Deps, reply
from clientscrapper.bot.texts import t
from clientscrapper.services import cards, costs, health, metrics

log = logging.getLogger(__name__)


async def holat(message: Message, deps: Deps) -> None:
    now = deps.clock()
    try:
        async with deps.session_factory() as session:
            status = await health.gather(session, now=now, evidence_size=True)
    except Exception as exc:
        log.warning("holat not gathered", extra={"error": type(exc).__name__})
        await reply(message, t("holat.failed"))
        return
    results = None
    try:
        async with deps.session_factory() as session:
            results = await metrics.holat(session, now=now)
    except Exception as exc:
        log.warning("holat numbers not read", extra={"error": type(exc).__name__})
    await reply(message, cards.render_holat(status, health.problems(status), results))


async def xarajat(message: Message, deps: Deps) -> None:
    async with deps.session_factory() as session:
        report = await costs.report(session, now=deps.clock())
    await reply(message, cards.render_xarajat(report))


__all__ = ["holat", "xarajat"]
