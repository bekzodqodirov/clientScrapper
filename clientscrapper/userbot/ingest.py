"""From a Telegram message to an ``interactions`` row, or to a count (03 §2.3).

The live handler, the edit handler and the backfill all go through here, so
there is one rule for what is kept:

* a message from a chat the reader may read (``chats.readable``) is counted
  in ``chat_monitors.seen_count``;
* it is stored only when the prefilter answers a hit, and then only the
  columns 02 §2.12 has — chat and message ids, the sender's id, username and
  first+last name (never a phone, never a photo), the text, the date, what was
  attached (never downloaded), the reply and forward ids and the hits;
* every inserted row is followed, in the same transaction, by
  ``NOTIFY cs_interaction, '<id>'`` — the event-driven path of 04 §0 that the
  worker's classify and score jobs wake on (01 N-06);
* an edit re-runs the prefilter at most ``MAX_EDITS`` times per message;
* the backfill counts into a ``Tally`` and writes it once per chat: a message
  the live handler already counted is not counted again, and the backfill's
  long transaction never holds the chat's row while it inserts (an edit that
  inserts and then counts would deadlock with it).

Nothing here makes a Telegram request: every value comes from the update.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.db.models import ChatMonitor, Interaction
from clientscrapper.services import chats
from clientscrapper.services.text import storable
from clientscrapper.userbot import prefilter, tg

log = logging.getLogger(__name__)

NOTIFY_CHANNEL: Final = chats.INTERACTION_CHANNEL
TEXT_MAX: Final = 4000
# 03 §2.3: an edit re-runs the prefilter, ten times per message at most.
MAX_EDITS: Final = 10
# How many messages' edit counts are remembered (the oldest are forgotten).
EDIT_MEMORY: Final = 10_000

Outcome = Literal[
    "ignored", "seen", "stored", "duplicate", "capped", "updated", "unchanged"
]


@dataclass(frozen=True, slots=True)
class Incoming:
    """The parts of a message 02 §2.12 lets the reader keep."""

    tg_chat_id: int
    tg_message_id: int
    text: str
    occurred_at: datetime
    sender_id: int | None
    sender_username: str | None
    sender_name: str | None
    media_kind: str
    reply_to_message_id: int | None
    fwd_from_peer_id: int | None


def incoming_of(message: object) -> Incoming | None:
    """A Telethon message as plain values; None for what is never stored: a
    service message (joins, pins — ``action`` is set), the account's own
    message, a message with no ids or no date."""
    if getattr(message, "action", None) is not None or getattr(message, "out", False):
        return None
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "id", None)
    occurred_at = getattr(message, "date", None)
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return None
    if not isinstance(occurred_at, datetime):
        return None
    sender = getattr(message, "sender", None)
    sender_id = getattr(message, "sender_id", None)
    name = tg.display_name_of(sender) if sender is not None else None
    return Incoming(
        tg_chat_id=chat_id,
        tg_message_id=message_id,
        text=storable(tg.text_of(message))[:TEXT_MAX],
        occurred_at=occurred_at,
        sender_id=sender_id if isinstance(sender_id, int) else None,
        sender_username=tg.sender_username(sender) if sender is not None else None,
        sender_name=storable(name) if name else None,
        media_kind=tg.media_kind_of(message),
        reply_to_message_id=tg.reply_to_id(message),
        fwd_from_peer_id=tg.fwd_from_peer(message),
    )


@dataclass(slots=True)
class Tally:
    """A backfill's counts for one chat, written by one ``count_seen`` at the
    end. ``live_since`` is the chat's ``monitoring_since``: a message from then
    on reached the live handler, which counted it in ``seen_count`` (02:
    «every message seen» — once)."""

    live_since: datetime | None = None
    seen: int = 0
    stored: int = 0
    newest: int | None = None

    def add(self, item: Incoming, *, stored: bool) -> None:
        if self.live_since is None or item.occurred_at < self.live_since:
            self.seen += 1
        if stored:
            self.stored += 1
        if self.newest is None or item.tg_message_id > self.newest:
            self.newest = item.tg_message_id


async def store_hit(
    session: AsyncSession, item: Incoming, hits: tuple[str, ...]
) -> int | None:
    """Insert one prefilter hit and notify; the new row's id, or None when the
    message was stored before (live and backfill meet on the unique key)."""
    if not hits:
        raise ValueError("only a prefilter hit is stored (02 §2.12)")
    row_id = await session.scalar(
        pg_insert(Interaction)
        .values(
            tg_chat_id=item.tg_chat_id,
            tg_message_id=item.tg_message_id,
            sender_id=item.sender_id,
            sender_username=item.sender_username,
            sender_name=item.sender_name,
            text=item.text,
            occurred_at=item.occurred_at,
            media_kind=item.media_kind,
            reply_to_message_id=item.reply_to_message_id,
            fwd_from_peer_id=item.fwd_from_peer_id,
            prefilter_hit=list(hits),
        )
        .on_conflict_do_nothing(constraint="uq_interactions_tg_message")
        .returning(Interaction.id)
    )
    if row_id is not None:
        # Delivered when this transaction commits, and only then (04 §0).
        await session.execute(sa.select(sa.func.pg_notify(NOTIFY_CHANNEL, str(row_id))))
    return row_id


async def monitor_for(
    session: AsyncSession, message: object, item: Incoming, *, now: datetime
) -> ChatMonitor | None:
    """The chat's row; a chat the reader has no row for yet (joined by hand
    since the last dialog sync) gets one with the defaults, from the entity
    the update carried — no request."""
    monitor = await chats.get_monitor(session, item.tg_chat_id)
    if monitor is not None:
        return monitor
    chat = getattr(message, "chat", None)
    if chat is None:
        return None
    return await chats.ensure_monitor(session, tg.dialog_of(chat), now=now)


async def handle_message(
    session: AsyncSession,
    message: object,
    *,
    now: datetime,
    tally: Tally | None = None,
) -> Outcome:
    """One new message (live or backfilled). The caller commits. With a
    ``tally`` (the backfill) the counts go there instead of to the row."""
    item = incoming_of(message)
    if item is None:
        return "ignored"
    monitor = await monitor_for(session, message, item, now=now)
    if monitor is None or not chats.readable(monitor):
        return "ignored"
    hits = prefilter.hits(item.text, chat_type=monitor.chat_type)
    if not hits:
        if tally is not None:
            tally.add(item, stored=False)
            return "seen"
        await chats.count_seen(
            session, item.tg_chat_id, message_id=item.tg_message_id, now=now
        )
        return "seen"
    row_id = await store_hit(session, item, hits)
    if tally is not None:
        tally.add(item, stored=row_id is not None)
        return "stored" if row_id is not None else "duplicate"
    await chats.count_seen(
        session,
        item.tg_chat_id,
        message_id=item.tg_message_id,
        now=now,
        stored=1 if row_id is not None else 0,
    )
    return "stored" if row_id is not None else "duplicate"


class EditCounter:
    """How many times each message's edits were run through the prefilter,
    for the newest EDIT_MEMORY messages. In memory on purpose: it bounds work,
    and a restart that forgets it costs at most ten more prefilter runs."""

    def __init__(self, limit: int = MAX_EDITS, memory: int = EDIT_MEMORY) -> None:
        self.limit = limit
        self.memory = memory
        self._seen: OrderedDict[tuple[int, int], int] = OrderedDict()

    def allow(self, tg_chat_id: int, tg_message_id: int) -> bool:
        """Count one edit; False once the message has used its ``limit``."""
        key = (tg_chat_id, tg_message_id)
        count = self._seen.pop(key, 0)
        self._seen[key] = min(count + 1, self.limit + 1)
        while len(self._seen) > self.memory:
            self._seen.popitem(last=False)
        return count < self.limit


async def handle_edit(
    session: AsyncSession, message: object, *, now: datetime, edits: EditCounter
) -> Outcome:
    """A message was edited: the prefilter runs again (≤ MAX_EDITS times).

    * not stored, now a hit → stored and notified, as a new message would be;
    * stored, not yet processed nor observed, still a hit → its text and hits
      follow the edit (neither the collector nor the classifier has read it);
    * stored and only counted by the collector (``chats.counted_only``: a
      price list with no buyer trigger, no evidence of it was kept), still a
      hit → its text follows the edit and it is read again, announced as a
      new message is — the edit may have made it an ask;
    * anything else stays as it was: a processed row — or one the
      telegram_monitor collector already turned into evidence and an
      observation (``evidence_id``), which quote the text as it was — is
      evidence of what was said, and an edit that is no hit is not kept (no
      full chat logs). The collector holds the rows it reads (``FOR UPDATE``)
      until its run commits, so an edit waits for it and then finds
      ``evidence_id`` set.
    An edit is not a new message: ``seen_count`` does not move."""
    item = incoming_of(message)
    if item is None:
        return "ignored"
    monitor = await chats.get_monitor(session, item.tg_chat_id)
    if monitor is None or not chats.readable(monitor):
        return "ignored"
    if not edits.allow(item.tg_chat_id, item.tg_message_id):
        return "capped"
    hits = prefilter.hits(item.text, chat_type=monitor.chat_type)
    stored = await session.scalar(
        sa.select(Interaction)
        .where(
            Interaction.tg_chat_id == item.tg_chat_id,
            Interaction.tg_message_id == item.tg_message_id,
        )
        .with_for_update()
    )
    if stored is None:
        if not hits:
            return "unchanged"
        row_id = await store_hit(session, item, hits)
        if row_id is None:
            return "unchanged"
        await chats.note_hit(session, item.tg_chat_id)
        return "stored"
    counted = chats.counted_only(stored)
    if (
        not hits
        or (stored.processed_at is not None and not counted)
        or stored.evidence_id is not None
        or stored.text_purged_at is not None
    ):
        return "unchanged"
    if stored.text == item.text and list(stored.prefilter_hit) == list(hits):
        return "unchanged"
    stored.text = item.text
    stored.prefilter_hit = list(hits)
    if counted:
        stored.processed_at = None
    await session.flush()
    if counted:
        # Delivered when this transaction commits (04 §0): the collector reads
        # the row again at once instead of at the next sweep.
        notify = sa.func.pg_notify(NOTIFY_CHANNEL, str(stored.id))
        await session.execute(sa.select(notify))
    return "updated"
