"""The review cards' way out (06 §1.3, §1.7): ``services/reviews``'
``CardSender`` over ``deliver.to_person`` — the owner's and the allow-listed
managers' chats only (anything else is ``NotAllowed`` before a byte leaves),
each card with the keyboard of its layout (``keyboards.review_keyboard``).

The bot's ``/lidlar`` and buttons and the worker's ``review_cards_job`` send
through the same object, so a card looks the same whoever sent it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import Message

from clientscrapper.bot import deliver, keyboards
from clientscrapper.services.cards import CardLayout
from clientscrapper.services.reviews import SentCard

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BotCards:
    bot: Bot

    async def send(
        self, telegram_id: int, text: str, layout: CardLayout, review_id: int
    ) -> SentCard | None:
        try:
            out = await deliver.to_person(
                self.bot,
                telegram_id,
                text,
                keyboard=keyboards.review_keyboard(review_id, layout),
            )
        except deliver.NotAllowed:
            log.error("a card for a chat outside the allowlist")
            return None
        if not isinstance(out, Message):
            return None
        chat = out.chat
        return SentCard(chat.id, out.message_id, chat.first_name or chat.username)

    async def edit(
        self,
        telegram_id: int,
        message_id: int,
        text: str,
        layout: CardLayout | None,
        review_id: int,
    ) -> bool:
        keyboard = (
            keyboards.review_keyboard(review_id, layout) if layout is not None else None
        )
        try:
            out = await deliver.to_person(
                self.bot, telegram_id, text, keyboard=keyboard, edit_message_id=message_id
            )
        except deliver.NotAllowed:
            return False
        return out is not None

    async def strip(self, telegram_id: int, message_id: int) -> bool:
        try:
            out = await deliver.to_person(
                self.bot, telegram_id, edit_message_id=message_id, markup_only=True
            )
        except deliver.NotAllowed:
            return False
        return out is not None


__all__ = ["BotCards"]
