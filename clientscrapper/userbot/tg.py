"""Telethon → plain values, and which Telegram errors mean what (06 §2).

Read-only helpers: everything here looks at objects Telegram already sent
(an update's message, a resolved entity, a raised error) and never makes a
request. The reader's modules use them so the rest of the package sees
``DialogInfo`` and strings, never a Telethon type.
"""

from __future__ import annotations

import re
from typing import Any, Final

from telethon import errors
from telethon import utils as tg_utils
from telethon.tl.types import Channel, Chat, InputPeerChannel, User

from clientscrapper.services.chats import DialogInfo
from clientscrapper.services.keys import username_key

# The plain flood wait and the download-speed one Telegram gives an account
# without Premium: siblings, neither a subclass of the other (MIYA).
FLOOD_ERRORS: Final = (errors.FloodWaitError, errors.FloodPremiumWaitError)
# The account is gone or limited (06 §2 Restriction): beat authorized=false,
# idle with a 15-minute re-check, never a reconnect storm.
RESTRICTION_ERRORS: Final = (
    errors.UserDeactivatedBanError,
    errors.AuthKeyUnregisteredError,
    errors.SessionRevokedError,
    errors.PhoneNumberBannedError,
    errors.UserDeactivatedError,
    errors.AuthKeyDuplicatedError,
)
# The chat is no longer reachable: status 'gone', never queued again.
GONE_ERRORS: Final = (
    errors.ChannelPrivateError,
    errors.UsernameNotOccupiedError,
    errors.UsernameInvalidError,
    errors.ChannelInvalidError,
    errors.ChatInvalidError,
)

# A join Telegram refused for this chat (admin approval needed, the account
# banned there): the chat is never queued again.
JOIN_REFUSED_ERRORS: Final = (
    errors.InviteRequestSentError,
    errors.UserBannedInChannelError,
    errors.ChannelPublicGroupNaError,
)
# The account itself is in too many chats: no join today, whatever the queue.
TOO_MANY_CHATS_ERRORS: Final = (errors.ChannelsTooMuchError,)

SENDER_NAME_MAX: Final = 120

# The family classes an RPC error falls back to when Telethon has no class of
# its own for the code: their message, not their name, is the code.
_GENERIC_RPC: Final = frozenset(
    {
        errors.RPCError,
        errors.InvalidDCError,
        errors.BadRequestError,
        errors.UnauthorizedError,
        errors.ForbiddenError,
        errors.NotFoundError,
        errors.AuthKeyError,
        errors.FloodError,
        errors.ServerError,
        errors.TimedOutError,
    }
)


def flood_seconds(exc: BaseException) -> int | None:
    """The wait of a FloodWait error, None for any other error."""
    if isinstance(exc, FLOOD_ERRORS):
        return int(getattr(exc, "seconds", 0) or 0)
    return None


_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def error_code(exc: BaseException) -> str:
    """Telegram's code for a known error (``UserDeactivatedBanError`` →
    ``USER_DEACTIVATED_BAN``), the message of a generic RPC error, else the
    class name — what the heartbeat and ``userbot_unauthorized`` show. The
    class decides first: Telethon names each code's class after it, while an
    error built outside a response carries only its family's message."""
    name = type(exc).__name__
    if isinstance(exc, errors.RPCError) and type(exc) not in _GENERIC_RPC:
        return _CAMEL.sub("_", name.removesuffix("Error")).upper()
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message
    return name


def chat_type_of(entity: object) -> str:
    """02's ``chat_type``: Telethon models supergroups and broadcast channels
    with one type."""
    if isinstance(entity, User):
        return "private"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        return "group" if getattr(entity, "megagroup", False) else "channel"
    return "group"


def peer_id(entity: object) -> int:
    """The marked peer id (``-100…`` for channels), as a message's chat_id."""
    return int(tg_utils.get_peer_id(entity))


def access_hash_of(entity: object) -> int | None:
    """The access hash Telegram gave this account for a channel (or a user)."""
    value = getattr(entity, "access_hash", None)
    return value if isinstance(value, int) else None


def input_channel(tg_chat_id: int, access_hash: int) -> InputPeerChannel:
    """A channel or supergroup by its marked id and access hash: how a chat
    with no public username is found again after a restart, when the session
    string has cached nothing (StringSession keeps the auth key only)."""
    bare, _ = tg_utils.resolve_id(tg_chat_id)
    return InputPeerChannel(channel_id=bare, access_hash=access_hash)


def dialog_of(entity: object) -> DialogInfo:
    """A chat entity as the registry wants it."""
    return DialogInfo(
        tg_chat_id=peer_id(entity),
        chat_type=chat_type_of(entity),
        title=getattr(entity, "title", None),
        username=getattr(entity, "username", None),
        member_count=getattr(entity, "participants_count", None),
    )


def display_name_of(entity: object) -> str | None:
    """First + last name, or a channel's title; never a phone number (a user's
    phone, when Telegram shows one, is not read: 06 §5.1)."""
    first = (getattr(entity, "first_name", None) or "").strip()
    last = (getattr(entity, "last_name", None) or "").strip()
    full = " ".join(part for part in (first, last) if part)
    if not full:
        full = (getattr(entity, "title", None) or "").strip()
    return full[:SENDER_NAME_MAX] or None


def sender_username(entity: object) -> str | None:
    """The sender's username in the CHECK's form (``cs_username_key``)."""
    raw = getattr(entity, "username", None)
    return username_key(raw) if isinstance(raw, str) else None


def fwd_from_peer(message: object) -> int | None:
    """The forwarded-from channel or user id (03 §2.3 tout rule), else None."""
    header = getattr(message, "fwd_from", None)
    origin = getattr(header, "from_id", None) if header is not None else None
    if origin is None:
        return None
    try:
        return int(tg_utils.get_peer_id(origin))
    except (TypeError, ValueError):
        return None


def reply_to_id(message: object) -> int | None:
    header = getattr(message, "reply_to", None)
    value = getattr(header, "reply_to_msg_id", None) if header is not None else None
    return value if isinstance(value, int) else None


def media_kind_of(message: object) -> str:
    """02's ``media_kind``. Media is never downloaded in v1 (06 §2); this only
    names what was attached. A link preview is not an attachment."""
    if getattr(message, "photo", None) is not None:
        return "photo"
    if getattr(message, "video", None) is not None:
        return "video"
    # Named one by one: the write guard refuses an attribute read by a name
    # built at run time (tests/test_no_telegram_writes.py).
    sounds_or_stickers = (
        getattr(message, "sticker", None),
        getattr(message, "gif", None),
        getattr(message, "voice", None),
        getattr(message, "audio", None),
        getattr(message, "video_note", None),
    )
    if any(item is not None for item in sounds_or_stickers):
        return "other"
    if getattr(message, "document", None) is not None:
        return "document"
    media = getattr(message, "media", None)
    if media is None or type(media).__name__ == "MessageMediaWebPage":
        return "none"
    return "other"


def text_of(message: object) -> str:
    """The message's words (a caption is the message text in Telethon)."""
    value: Any = getattr(message, "message", None)
    return value if isinstance(value, str) else ""
