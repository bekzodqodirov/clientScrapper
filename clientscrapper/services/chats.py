# VENDORED_FROM dailyJournal@6b1f67f miya/services/chats.py
"""The chat monitor registry (02 §2.12, 03 §2.3, 06 §2).

``chat_monitors`` decides what the reader account may read, and is the
joiner's queue (``status='queued'``, ``priority``). MIYA's registry with its
default flipped: a group or channel is read (``rule:default``), a private chat
never is (``rule:private_off``) and GSR's own chat (``role='calibration'``) is
never read either — the account may sit in it, its members are clients (03
§2.3). ``default_monitor_enabled`` is the one home of that rule. A row is
created with the defaults once; its switch is the owner's afterwards and a
dialog re-sync never overwrites it.

Two queues, because 02's ``tg_chat_id`` is ``NOT NULL``: a chat whose numeric
id is known (a channel's linked discussion group, 03 §2.3) is queued as a row;
a bare username (a seed, a companion group named in an ``about``, a channel
another collector found) waits in ``runtime_flags('tg_pending')`` until the
reader resolves it (≤ 20 a day) into a queued row. The seeds are 03 §2.3's,
``sources.config.seeds`` overrides them.

Telethon-free: the worker and the owner bot use this module too (06 §5.2 — no
``telethon`` outside ``userbot/``), and the reader hands it plain values.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.db.enums import CHAT_ROLES
from clientscrapper.db.models import ChatMonitor, Interaction, RuntimeFlag, Source
from clientscrapper.services.keys import username_key

log = logging.getLogger(__name__)

SOURCE_KEY: Final = "telegram_monitor"
# The reader's insert transaction ends with ``NOTIFY cs_interaction, '<id>'``
# (02 §2.12); the worker's telegram_monitor, classify and score jobs wake on
# it (04 §0). The one home of the channel's name: the reader, the collector
# and the worker all read it here.
INTERACTION_CHANNEL: Final = "cs_interaction"
# ``evidence.meta.kind`` of a chat's weekly about (03 §2.3): the reader stores
# it (userbot/about.py), the telegram_monitor collector makes the profile
# observation of it.
ABOUT_EVIDENCE_KIND: Final = "telegram_about"

# 02 §2.12 priority column: 0 owner /chats qoʻsh · 1 linked discussion group ·
# 2 companion group · 3 resolved username · 5 seed.
PRIORITY_OWNER: Final = 0
PRIORITY_LINKED: Final = 1
PRIORITY_COMPANION: Final = 2
PRIORITY_RESOLVED: Final = 3
PRIORITY_SEED: Final = 5

# Telegram marks a channel's (and a supergroup's) peer id as -(10**12 + id):
# a message's ``chat_id`` is the marked id, ``full_chat.linked_chat_id`` the
# bare one. Every ``tg_chat_id`` here is the marked id.
_CHANNEL_MARK: Final = 10**12


def channel_peer_id(channel_id: int) -> int:
    """The marked peer id of a channel or supergroup given its bare id."""
    return -(_CHANNEL_MARK + channel_id) if channel_id > 0 else channel_id


def is_channel_peer(peer_id: int) -> bool:
    """A marked peer id that names a channel or a supergroup (not a user, whose
    id is positive, nor a basic group, ``-<id>`` under the mark)."""
    return peer_id < -_CHANNEL_MARK


def bare_channel_id(peer_id: int) -> int:
    """The bare id of a marked channel or supergroup id (``t.me/c/<bare>/…``);
    any other id as its absolute value."""
    return -peer_id - _CHANNEL_MARK if is_channel_peer(peer_id) else abs(peer_id)


@dataclass(frozen=True, slots=True)
class Seed:
    username: str
    role: str = "market"


# ``chat_monitors.role='buyers'``: a chat whose posts are its audience's, not
# its own — a companion «xaridorlar» group an about names, or a classifieds
# channel such as @Optom_bozor (03 §2.3: «the lead is the poster, not the
# channel»). What such a chat publishes as itself is a poster's ad, never the
# chat speaking for itself (collectors/telegram_monitor.speaker_of).
BUYERS_ROLE: Final = "buyers"


# 03 §2.3. Buyer-side groups first, then wholesaler channels, then the
# competitor channels whose linked discussion groups are the surface. The
# handles are 03's: «CHINNiLAR OPTOM TOSHKENT» (handle UNVERIFIED), Abu Sahiy
# Cargo, Meest China and CARGO GREEN (handles to confirm inside Telegram) wait
# for the day-1 check and join through sources.config.seeds or /chats qoʻsh.
# @cargo5566 and the religion/B2C clones of «Optom bozor» are excluded.
SEEDS: Final[tuple[Seed, ...]] = (
    Seed("optom_tavarlar_uz"),
    Seed("optom_bozor", role=BUYERS_ROLE),  # classifieds: the lead is the poster
    Seed("optomelonuz"),
    Seed("pekinoptom_uz"),
    Seed("abusaxiyoptomm"),
    Seed("patnoss"),  # Chinni Bozor Bekzod — probable (03 §2.3)
    Seed("mk_express", role="competitor"),
    Seed("fx_express", role="competitor"),
    Seed("halol_cargo", role="competitor"),
)
# GSR's own chat: the account may sit in it, nothing is ever read or stored
# from it (03 §2.3, REVIEW #38). Never joined by the agent either.
CALIBRATION_USERNAMES: Final = frozenset({"gsrgroupchat"})

# A backfill that keeps failing (lost access, flood limits) is left alone
# after this many nights (MIYA BACKFILL_MAX_ATTEMPTS).
BACKFILL_MAX_ATTEMPTS: Final = 3
# 03 §2.3: chats joined ≥ 24 h ago are backfilled.
BACKFILL_AFTER: Final = timedelta(hours=24)

PENDING_FLAG: Final = "tg_pending"
# How to find a linked discussion group with no public username again (see
# remember_linked): {"<tg_chat_id>": {"access_hash", "parent", "at"}}.
LINKED_FLAG: Final = "tg_linked"
UPDATED_BY: Final = "userbot"
# A name that resolved to nothing is not tried again for this long; then once
# more (a seed may be registered later, a channel may come back).
UNRESOLVABLE_KEEP: Final = timedelta(days=30)
# The resolve's reason for a name that is a person, a bot or a private chat:
# remembered as a hash only — a person's handle has no place in the reader's
# queue (06 §5.1).
NOT_A_CHAT: Final = "not_a_public_chat"
# 03 §2.3: a channel silent this long is not queued.
SILENT_AFTER: Final = timedelta(days=90)
# 03 §2.3: a poster's bio (``interactions.sender_bio``, which only
# userbot/about.py reads) is good for 30 days — the reader copies it to the
# sender's newer rows instead of asking again, and the telegram_monitor
# collector reads it from any of them when a message's own row has none yet.
BIO_CACHE: Final = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class DialogInfo:
    """What the reader knows about a chat, independent of Telethon types.
    ``tg_chat_id`` is the marked peer id."""

    tg_chat_id: int
    chat_type: str
    title: str | None = None
    username: str | None = None
    member_count: int | None = None


@dataclass(frozen=True, slots=True)
class Pending:
    """A username waiting to be resolved into a queued chat."""

    username: str
    priority: int
    role: str


# --- the rule ------------------------------------------------------------------


def role_for(username: str | None, role: str = "market") -> str:
    """``calibration`` for GSR's own chat, whatever the caller thought."""
    return "calibration" if username in CALIBRATION_USERNAMES else role


def default_monitor_enabled(chat_type: str, *, role: str = "market") -> tuple[bool, str]:
    """(``monitor_enabled``, ``decided_by``) for a new row (06 §2): groups and
    channels on by default, private chats off by rule, the calibration chat
    off — the one home of the reader's default."""
    if chat_type == "private":
        return False, "rule:private_off"
    if role == "calibration":
        return False, "rule:seed"
    return True, "rule:default"


def readable(monitor: ChatMonitor) -> bool:
    """May the reader read this chat now? Switched on, joined, a group or a
    channel, and not the calibration chat — the switch alone is not enough:
    a calibration or private row is never read whatever it says."""
    return (
        bool(monitor.monitor_enabled)
        and monitor.status == "joined"
        and monitor.chat_type in ("group", "channel")
        and monitor.role != "calibration"
    )


def seeds_from(config: Mapping[str, Any] | None) -> tuple[Seed, ...]:
    """``sources.config.seeds`` (a list of usernames or of {username, role})
    when the owner set it, else 03's list. Unusable names are dropped."""
    raw = (config or {}).get("seeds")
    if not isinstance(raw, list):
        return SEEDS
    seeds: list[Seed] = []
    for item in raw:
        name, role = (item, "market") if isinstance(item, str) else (None, "market")
        if isinstance(item, Mapping):
            name, role = item.get("username"), str(item.get("role") or "market")
        key = username_key(name) if isinstance(name, str) else None
        if key is not None and role in CHAT_ROLES:
            seeds.append(Seed(key, role))
    return tuple(seeds)


# --- rows ------------------------------------------------------------------------


async def get_monitor(session: AsyncSession, tg_chat_id: int) -> ChatMonitor | None:
    monitor: ChatMonitor | None = await session.scalar(
        sa.select(ChatMonitor)
        .where(ChatMonitor.tg_chat_id == tg_chat_id)
        .execution_options(populate_existing=True)
    )
    return monitor


async def by_username(session: AsyncSession, username: str) -> ChatMonitor | None:
    monitor: ChatMonitor | None = await session.scalar(
        sa.select(ChatMonitor)
        .where(ChatMonitor.username == username)
        .order_by(ChatMonitor.id)
        .limit(1)
    )
    return monitor


def _person_safe(dialog: DialogInfo) -> tuple[str | None, str | None]:
    """A private chat's title is a person's name and is never read: the row
    keeps neither title nor username (06 §5.1 — nothing person-tier here)."""
    if dialog.chat_type == "private":
        return None, None
    return (dialog.title or None), username_key(dialog.username)


async def ensure_monitor(
    session: AsyncSession,
    dialog: DialogInfo,
    *,
    now: datetime,
    status: str = "joined",
    priority: int = PRIORITY_SEED,
    role: str = "market",
) -> ChatMonitor:
    """The row for this chat, created with the defaults when it is new.

    An upsert that does nothing on conflict, then a read: two messages from a
    chat the reader has never seen can be handled at once, and a plain
    check-then-insert would lose one to the unique ``tg_chat_id`` (MIYA)."""
    title, username = _person_safe(dialog)
    role = role_for(username, role)
    enabled, decided_by = default_monitor_enabled(dialog.chat_type, role=role)
    joined = status == "joined"
    await session.execute(
        pg_insert(ChatMonitor)
        .values(
            tg_chat_id=dialog.tg_chat_id,
            chat_type=dialog.chat_type,
            title=title,
            username=username,
            role=role,
            status=status,
            priority=priority,
            monitor_enabled=enabled,
            decided_by=decided_by,
            joined_at=now if joined else None,
            monitoring_since=now if joined and enabled else None,
            member_count=dialog.member_count,
        )
        .on_conflict_do_nothing(index_elements=[ChatMonitor.tg_chat_id])
    )
    monitor = await get_monitor(session, dialog.tg_chat_id)
    if monitor is None:  # pragma: no cover - the upsert just guaranteed a row
        raise RuntimeError(f"chat monitor for {dialog.tg_chat_id} vanished")
    return monitor


def _refresh(monitor: ChatMonitor, dialog: DialogInfo) -> bool:
    """Titles drift (groups get renamed); the switch is the owner's and stays."""
    title, username = _person_safe(dialog)
    changed = False
    for attr, value in (
        ("title", title),
        ("username", username),
        ("member_count", dialog.member_count),
        ("chat_type", dialog.chat_type),
    ):
        if value is not None and getattr(monitor, attr) != value:
            setattr(monitor, attr, value)
            changed = True
    return changed


def mark_joined(
    monitor: ChatMonitor, *, now: datetime, dialog: DialogInfo | None = None
) -> None:
    """The account is in this chat. ``joined_at`` keeps the first join: the
    24-hour backfill wait (03 §2.3) counts from it."""
    if dialog is not None:
        _refresh(monitor, dialog)
    monitor.status = "joined"
    monitor.joined_at = monitor.joined_at or now
    if monitor.monitor_enabled and readable(monitor):
        monitor.monitoring_since = monitor.monitoring_since or now


def mark_gone(monitor: ChatMonitor) -> None:
    """The chat was removed, made private or banned the account
    (``ChannelPrivateError``, ``UsernameNotOccupied``): never queued again."""
    monitor.status = "gone"


async def sync_dialogs(
    session: AsyncSession, dialogs: Iterable[DialogInfo], *, now: datetime
) -> tuple[int, int]:
    """Register the chats the account is in (the dialog list: titles only, no
    history). Returns (created, updated)."""
    created = updated = 0
    for dialog in dialogs:
        existing = await get_monitor(session, dialog.tg_chat_id)
        if existing is None:
            await ensure_monitor(session, dialog, now=now)
            created += 1
            continue
        before = (existing.status, existing.title, existing.username)
        changed = _refresh(existing, dialog)
        if existing.status != "joined":
            mark_joined(existing, now=now)
        if changed or before != (existing.status, existing.title, existing.username):
            updated += 1
    await session.flush()
    return created, updated


async def mark_left(
    session: AsyncSession, listed: Iterable[int], *, listed_at: datetime
) -> int:
    """The chats the account sat in and a complete dialog list no longer
    shows: the owner left them by hand (06 §2 — the agent never leaves).
    They become ``left`` — not read, not backfilled, off the 60-chat cap —
    and a re-join puts them back (``sync_dialogs``). A chat with a message
    seen since the list was asked for is still the account's. Call it only
    after the whole list was read; an empty list changes nothing. Returns how
    many rows were marked."""
    ids = set(listed)
    if not ids:
        return 0
    result = await session.execute(
        sa.update(ChatMonitor)
        .where(
            ChatMonitor.status == "joined",
            ChatMonitor.tg_chat_id.not_in(ids),
            sa.or_(
                ChatMonitor.last_seen_at.is_(None),
                ChatMonitor.last_seen_at < listed_at,
            ),
        )
        .values(status="left")
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def enqueue_chat(
    session: AsyncSession,
    dialog: DialogInfo,
    *,
    priority: int,
    role: str = "market",
    now: datetime,
) -> ChatMonitor | None:
    """Queue a chat whose id is known for the joiner. A chat already known
    keeps its row; a queued one moves up to the better (lower) priority. The
    calibration chat is never queued: the agent does not join it (03 §2.3).
    Returns the row when it is new."""
    if role_for(username_key(dialog.username)) == "calibration":
        return None
    existing = await get_monitor(session, dialog.tg_chat_id)
    if existing is not None:
        if existing.status == "queued" and priority < existing.priority:
            existing.priority = priority
            # Sessions here do not autoflush; a later populate_existing read
            # would otherwise put the old priority back.
            await session.flush()
        return None
    return await ensure_monitor(
        session, dialog, now=now, status="queued", priority=priority, role=role
    )


async def queued(session: AsyncSession, *, limit: int) -> list[ChatMonitor]:
    """The joiner's queue, best priority first, then oldest (02 §2.12 index)."""
    return list(
        await session.scalars(
            sa.select(ChatMonitor)
            .where(ChatMonitor.status == "queued", ChatMonitor.role != "calibration")
            .order_by(ChatMonitor.priority, ChatMonitor.created_at, ChatMonitor.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )


async def joined_count(session: AsyncSession) -> int:
    """Chats the account sits in (the 60-chat cap, 06 §2): groups and
    channels, not private dialogs."""
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ChatMonitor)
        .where(ChatMonitor.status == "joined", ChatMonitor.chat_type != "private")
    )
    return int(count or 0)


def about_readable() -> sa.ColumnElement[bool]:
    """A chat whose about the reader reads (``userbot/about.channels_due``)
    and the telegram_monitor collector may turn into a profile: a queued
    channel E3 found (priority 3), or a joined chat the owner has not switched
    off — never the calibration chat. One predicate for both, so an about the
    reader would not read now is not used either."""
    return sa.and_(
        ChatMonitor.role != "calibration",
        sa.or_(
            sa.and_(
                ChatMonitor.status == "queued",
                ChatMonitor.priority == PRIORITY_RESOLVED,
            ),
            sa.and_(
                ChatMonitor.status == "joined",
                ChatMonitor.monitor_enabled.is_(True),
            ),
        ),
    )


def counted_only(row: Interaction) -> bool:
    """A stored message the telegram_monitor collector counted and nothing
    more (a person's price list with no buyer trigger, kept so the tout rule
    can count it, 02 §2.12): handled, with no evidence and no observation.
    Every other handled row carries its evidence."""
    return (
        row.processed_at is not None
        and row.evidence_id is None
        and row.observation_id is None
    )


async def readable_ids(session: AsyncSession) -> list[int]:
    rows = await session.scalars(
        sa.select(ChatMonitor.tg_chat_id).where(
            ChatMonitor.monitor_enabled.is_(True),
            ChatMonitor.status == "joined",
            ChatMonitor.chat_type.in_(("group", "channel")),
            ChatMonitor.role != "calibration",
        )
    )
    return list(rows)


async def count_seen(
    session: AsyncSession,
    tg_chat_id: int,
    *,
    message_id: int | None,
    now: datetime,
    seen: int = 1,
    stored: int = 0,
) -> None:
    """Messages seen in a chat: ``seen_count`` for every one (counted, never
    stored), ``hit_count`` for the stored hits among them; the newest id seen.
    One UPDATE, so two handlers at once never lose a count."""
    values: dict[str, Any] = {
        "seen_count": ChatMonitor.seen_count + seen,
        "last_seen_at": now,
    }
    if stored:
        values["hit_count"] = ChatMonitor.hit_count + stored
    if message_id is not None:
        values["last_seen_message_id"] = sa.func.greatest(
            sa.func.coalesce(ChatMonitor.last_seen_message_id, 0), message_id
        )
    await session.execute(
        sa.update(ChatMonitor)
        .where(ChatMonitor.tg_chat_id == tg_chat_id)
        .values(**values)
    )


async def note_hit(session: AsyncSession, tg_chat_id: int) -> None:
    """A stored hit that was not a new message (an edit made it one)."""
    await session.execute(
        sa.update(ChatMonitor)
        .where(ChatMonitor.tg_chat_id == tg_chat_id)
        .values(hit_count=ChatMonitor.hit_count + 1)
    )


# --- backfill (03 §2.3) ------------------------------------------------------------


async def backfill_due(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[ChatMonitor]:
    """Readable chats joined ≥ 24 h ago and not yet backfilled, oldest join
    first; a chat that failed BACKFILL_MAX_ATTEMPTS nights is left alone."""
    return list(
        await session.scalars(
            sa.select(ChatMonitor)
            .where(
                ChatMonitor.status == "joined",
                ChatMonitor.monitor_enabled.is_(True),
                ChatMonitor.chat_type.in_(("group", "channel")),
                ChatMonitor.role != "calibration",
                ChatMonitor.backfilled_at.is_(None),
                ChatMonitor.joined_at <= now - BACKFILL_AFTER,
                ChatMonitor.backfill_attempts < BACKFILL_MAX_ATTEMPTS,
            )
            .order_by(ChatMonitor.joined_at, ChatMonitor.id)
            .limit(limit)
        )
    )


def mark_backfilled(monitor: ChatMonitor, *, now: datetime) -> None:
    monitor.backfilled_at = now


def mark_backfill_failed(monitor: ChatMonitor) -> None:
    """One more failed night; at BACKFILL_MAX_ATTEMPTS it is left alone."""
    monitor.backfill_attempts += 1
    if monitor.backfill_attempts >= BACKFILL_MAX_ATTEMPTS:
        log.error(
            "backfill gave up",
            extra={
                "tg_chat_id": monitor.tg_chat_id,
                "attempts": monitor.backfill_attempts,
            },
        )


# --- the owner's /chats (06 §1.2) ----------------------------------------------------


def _list_order() -> tuple[Any, ...]:
    """/chats' order, one home for the page and a row's place in it."""
    return (
        ChatMonitor.monitor_enabled.desc(),
        ChatMonitor.status,
        ChatMonitor.title.nulls_last(),
        ChatMonitor.id,
    )


async def list_monitors(
    session: AsyncSession, *, offset: int = 0, limit: int = 8
) -> tuple[list[ChatMonitor], int]:
    """One page of chats for /chats, read ones first, plus the total."""
    total = await session.scalar(sa.select(sa.func.count()).select_from(ChatMonitor))
    rows = list(
        await session.scalars(
            sa.select(ChatMonitor)
            .order_by(*_list_order())
            .offset(offset)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
    )
    return rows, int(total or 0)


async def get_monitor_by_id(session: AsyncSession, monitor_id: int) -> ChatMonitor | None:
    found: ChatMonitor | None = await session.get(
        ChatMonitor, monitor_id, populate_existing=True
    )
    return found


async def position_of(session: AsyncSession, monitor_id: int) -> int:
    """A chat's index in /chats' order (0 when it is gone), so the page it
    now stands on can be shown after a toggle moved it."""
    ids = list(await session.scalars(sa.select(ChatMonitor.id).order_by(*_list_order())))
    return ids.index(monitor_id) if monitor_id in ids else 0


async def hits_since(
    session: AsyncSession, tg_chat_ids: Iterable[int], *, since: datetime
) -> dict[int, int]:
    """/chats' signal count: the stored prefilter hits (``interactions``)
    of each chat since ``since`` — one grouped query for the page. A chat
    with none is absent."""
    ids = sorted(set(tg_chat_ids))
    if not ids:
        return {}
    rows = await session.execute(
        sa.select(Interaction.tg_chat_id, sa.func.count())
        .where(Interaction.tg_chat_id.in_(ids), Interaction.occurred_at >= since)
        .group_by(Interaction.tg_chat_id)
    )
    return {int(chat): int(n) for chat, n in rows.all()}


async def set_monitor(
    session: AsyncSession, monitor_id: int, enabled: bool, *, now: datetime
) -> ChatMonitor | None:
    """The owner switches reading a chat on or off, under the row's lock so a
    tap that arrives twice never flips it back. A private chat or the
    calibration chat cannot be switched on: they are never read (06 §2).
    None when the row is gone. Off only stops reading — the account never
    leaves a chat (06 §2)."""
    monitor = await session.scalar(
        sa.select(ChatMonitor)
        .where(ChatMonitor.id == monitor_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if monitor is None:
        return None
    if enabled and (monitor.chat_type == "private" or monitor.role == "calibration"):
        raise ValueError("a private or calibration chat is never read")
    if bool(monitor.monitor_enabled) != enabled:
        monitor.monitor_enabled = enabled
        if enabled and monitor.status == "joined":
            monitor.monitoring_since = monitor.monitoring_since or now
    monitor.decided_by = "owner"
    monitor.asked_at = monitor.asked_at or now
    await session.flush()
    return monitor


# --- usernames waiting to be resolved ------------------------------------------------


async def _locked_flag(session: AsyncSession, key: str) -> RuntimeFlag:
    await session.execute(
        pg_insert(RuntimeFlag)
        .values(key=key, value={}, updated_by=UPDATED_BY)
        .on_conflict_do_nothing(index_elements=[RuntimeFlag.key])
    )
    row = await session.scalar(
        sa.select(RuntimeFlag)
        .where(RuntimeFlag.key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert row is not None  # the upsert just made sure
    return row


async def remember_linked(
    session: AsyncSession,
    tg_chat_id: int,
    *,
    access_hash: int,
    parent: str | None,
    now: datetime,
) -> None:
    """How to find a queued linked discussion group again. One with no public
    username is found only by its id and the access hash Telegram gave this
    account, which arrive with the parent's full channel and live in
    Telethon's memory: the session string keeps no entities, so a restart
    would leave the row queued for ever. Kept until the group is joined or
    gone (``forget_linked``)."""
    flag = await _locked_flag(session, LINKED_FLAG)
    value = dict(flag.value) if isinstance(flag.value, Mapping) else {}
    value[str(tg_chat_id)] = {
        "access_hash": int(access_hash),
        "parent": parent,
        "at": now.isoformat(),
    }
    flag.value = value
    flag.updated_by = UPDATED_BY
    await session.flush()


async def linked_access_hash(session: AsyncSession, tg_chat_id: int) -> int | None:
    """The access hash remembered for a queued linked group, else None."""
    flag = await session.get(RuntimeFlag, LINKED_FLAG, populate_existing=True)
    value = flag.value if flag is not None else None
    item = value.get(str(tg_chat_id)) if isinstance(value, Mapping) else None
    found = item.get("access_hash") if isinstance(item, Mapping) else None
    return found if isinstance(found, int) else None


async def forget_linked(session: AsyncSession, tg_chat_id: int) -> None:
    """The linked group was joined or is gone: nothing more to find."""
    flag = await session.get(RuntimeFlag, LINKED_FLAG, populate_existing=True)
    if flag is None or not isinstance(flag.value, Mapping):
        return
    if str(tg_chat_id) not in flag.value:
        return
    flag = await _locked_flag(session, LINKED_FLAG)
    value = dict(flag.value) if isinstance(flag.value, Mapping) else {}
    value.pop(str(tg_chat_id), None)
    flag.value = value
    flag.updated_by = UPDATED_BY
    await session.flush()


def _hashed(username: str) -> str:
    """How a name that is not a chat is remembered: never as itself."""
    return "#" + hashlib.sha256(f"{PENDING_FLAG}:{username}".encode()).hexdigest()[:20]


def _is_unresolvable(username: str, gone: Mapping[str, Any]) -> bool:
    return username in gone or _hashed(username) in gone


def _fresh_unresolvable(gone: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """The unresolvable names still inside UNRESOLVABLE_KEEP."""
    kept: dict[str, Any] = {}
    for key, item in gone.items():
        at = item.get("at") if isinstance(item, Mapping) else None
        try:
            when = datetime.fromisoformat(at) if isinstance(at, str) else None
        except ValueError:
            when = None
        if (
            when is not None
            and when.tzinfo is not None
            and now - when < UNRESOLVABLE_KEEP
        ):
            kept[key] = item
    return kept


def _pending_state(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    value = value if isinstance(value, Mapping) else {}
    names = value.get("names")
    gone = value.get("unresolvable")
    return (
        dict(names) if isinstance(names, Mapping) else {},
        dict(gone) if isinstance(gone, Mapping) else {},
    )


async def enqueue_username(
    session: AsyncSession,
    raw: str,
    *,
    priority: int,
    role: str = "market",
    now: datetime,
    by: str,
    last_post_at: datetime | None = None,
) -> bool:
    """Put a username in line for resolution (the joiner resolves ≤ 20 a day).
    True when it was added. A name already a chat, already waiting, or known
    to resolve to nothing is not added again; the calibration chat never is.

    ``last_post_at`` is the newest post date of the channel's public page
    ``https://t.me/s/<username>``, read by the caller through ``Fetcher``
    (03 §2.3 — the reader fetches no page): a channel silent more than
    SILENT_AFTER (90 days) is not queued. None = no page was read (E3 queues
    a name whose page is empty, 04 §3)."""
    username = username_key(raw)
    if username is None or username in CALIBRATION_USERNAMES or role not in CHAT_ROLES:
        return False
    if last_post_at is not None and now - last_post_at > SILENT_AFTER:
        return False
    if await by_username(session, username) is not None:
        return False
    flag = await _locked_flag(session, PENDING_FLAG)
    names, gone = _pending_state(flag.value)
    gone = _fresh_unresolvable(gone, now=now)
    if _is_unresolvable(username, gone):
        return False
    current = names.get(username)
    if isinstance(current, Mapping) and int(current.get("priority", 9)) <= priority:
        return False
    names[username] = {
        "priority": priority,
        "role": role,
        "added_at": now.isoformat(),
        "by": by,
    }
    flag.value = {"names": names, "unresolvable": gone}
    flag.updated_by = UPDATED_BY
    await session.flush()
    return True


async def pending_usernames(
    session: AsyncSession,
    *,
    seeds: Iterable[Seed] = SEEDS,
    limit: int,
    now: datetime,
) -> list[Pending]:
    """What waits to be resolved, best priority first: the queued usernames,
    then the seeds no row knows yet, in their order. Names already a chat or
    known (within UNRESOLVABLE_KEEP) to resolve to nothing are left out."""
    flag = await session.get(RuntimeFlag, PENDING_FLAG, populate_existing=True)
    names, gone = _pending_state(flag.value if flag is not None else None)
    gone = _fresh_unresolvable(gone, now=now)
    order: list[tuple[int, int, Pending]] = []
    for index, (username, item) in enumerate(names.items()):
        if isinstance(item, Mapping):
            pending = Pending(
                username,
                int(item.get("priority", PRIORITY_RESOLVED)),
                str(item.get("role")),
            )
            order.append((pending.priority, index, pending))
    for index, seed in enumerate(seeds):
        if seed.username not in names and seed.username not in CALIBRATION_USERNAMES:
            order.append(
                (
                    PRIORITY_SEED,
                    len(names) + index,
                    Pending(seed.username, PRIORITY_SEED, seed.role),
                )
            )
    order.sort(key=lambda item: (item[0], item[1]))
    known = set(
        await session.scalars(
            sa.select(ChatMonitor.username).where(
                ChatMonitor.username.in_([p.username for _, _, p in order])
            )
        )
    )
    found = [
        p
        for _, _, p in order
        if p.username not in known and not _is_unresolvable(p.username, gone)
    ]
    return found[:limit]


async def forget_pending(
    session: AsyncSession,
    username: str,
    *,
    now: datetime,
    unresolvable: str | None = None,
) -> None:
    """A waiting username is done: resolved into a row, or (``unresolvable``
    names why) not to be tried again for UNRESOLVABLE_KEEP — a seed that is
    not a chat is remembered here, or it would cost a resolve every day. A
    name that is a person (NOT_A_CHAT) is remembered by its hash only."""
    flag = await _locked_flag(session, PENDING_FLAG)
    names, gone = _pending_state(flag.value)
    gone = _fresh_unresolvable(gone, now=now)
    names.pop(username, None)
    if unresolvable is not None:
        key = _hashed(username) if unresolvable == NOT_A_CHAT else username
        gone[key] = {"reason": unresolvable, "at": now.isoformat()}
    flag.value = {"names": names, "unresolvable": gone}
    flag.updated_by = UPDATED_BY
    await session.flush()


async def known_bios(
    session: AsyncSession, sender_ids: Iterable[int], *, now: datetime
) -> dict[int, str]:
    """The newest bio read within BIO_CACHE of each of ``sender_ids``, from any
    of the sender's rows (``''``: read, and empty); a sender none of whose
    rows has one is absent. The one home of the cache rule: the reader's
    ``about.cached_bio`` and the collector both ask here."""
    ids = sorted({int(i) for i in sender_ids})
    if not ids:
        return {}
    rows = await session.execute(
        sa.select(Interaction.sender_id, Interaction.sender_bio)
        .where(
            Interaction.sender_id.in_(ids),
            Interaction.sender_bio.is_not(None),
            Interaction.occurred_at > now - BIO_CACHE,
        )
        .distinct(Interaction.sender_id)
        .order_by(Interaction.sender_id, Interaction.occurred_at.desc())
    )
    return {
        int(sender): str(bio)
        for sender, bio in rows.tuples().all()
        if sender is not None and bio is not None
    }


async def source_config(session: AsyncSession) -> dict[str, Any]:
    """``sources.config`` of telegram_monitor (the seeds live there)."""
    row = await session.get(Source, SOURCE_KEY)
    return dict(row.config or {}) if row is not None else {}
