"""A Telegram that never leaves the process (07 §1.0; T-13).

``FakeTelegramClient`` answers the read methods the reader uses —
``connect``, ``is_user_authorized``, ``get_me``, ``iter_dialogs``,
``get_entity``, ``iter_messages``, ``get_messages``, ``catch_up``, the event
handler registry — and the raw requests it may make (``JoinChannelRequest``,
``GetFullChannelRequest``, ``GetFullUserRequest``), recording every call. Any
other attribute raises, so a write method the reader grew would fail the test
that reached it rather than pass silently. Errors are planted per call name
and target (``fail``).

The entities are real Telethon types (``Channel``, ``User``) because the
reader computes peer ids with ``telethon.utils``; the messages are plain
namespaces with the attributes a Telethon message has. All data is synthetic
(07 §1.0): «Namuna …», ``namuna_<n>``, ``+998 90 000 00 NN``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from telethon import errors
from telethon import utils as tg_utils
from telethon.tl.types import Channel, ChatPhotoEmpty, InputPeerChannel, PeerChannel, User

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def channel(
    channel_id: int,
    *,
    title: str,
    username: str | None = None,
    megagroup: bool = False,
    participants: int | None = None,
) -> Channel:
    """A broadcast channel, or a supergroup with ``megagroup=True``."""
    return Channel(
        id=channel_id,
        title=title,
        photo=ChatPhotoEmpty(),
        date=EPOCH,
        megagroup=megagroup,
        broadcast=not megagroup,
        username=username,
        access_hash=channel_id * 7,
        participants_count=participants,
    )


def user(
    user_id: int,
    *,
    first_name: str = "Namuna",
    last_name: str | None = None,
    username: str | None = None,
    phone: str | None = None,
) -> User:
    return User(
        id=user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
        phone=phone,
        access_hash=user_id * 11,
    )


def peer(entity: Any) -> int:
    return int(tg_utils.get_peer_id(entity))


def message(
    chat: Any,
    message_id: int,
    text: str,
    *,
    date: datetime,
    sender: Any = None,
    out: bool = False,
    action: Any = None,
    fwd_from_channel: int | None = None,
    reply_to: int | None = None,
    photo: Any = None,
    document: Any = None,
    with_chat: bool = True,
) -> SimpleNamespace:
    """A message as an update carries it."""
    return SimpleNamespace(
        id=message_id,
        chat_id=peer(chat),
        chat=chat if with_chat else None,
        date=date,
        message=text,
        out=out,
        action=action,
        sender=sender,
        sender_id=peer(sender) if sender is not None else None,
        fwd_from=(
            SimpleNamespace(from_id=PeerChannel(channel_id=fwd_from_channel))
            if fwd_from_channel is not None
            else None
        ),
        reply_to=SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None,
        photo=photo,
        video=None,
        sticker=None,
        gif=None,
        voice=None,
        audio=None,
        video_note=None,
        document=document,
        media=photo or document,
    )


def full_channel(
    *,
    about: str = "",
    linked: Any = None,
    participants: int | None = None,
    pinned_msg_id: int | None = None,
) -> SimpleNamespace:
    """``messages.ChatFull`` as GetFullChannelRequest answers it."""
    return SimpleNamespace(
        full_chat=SimpleNamespace(
            about=about,
            linked_chat_id=linked.id if linked is not None else None,
            participants_count=participants,
            pinned_msg_id=pinned_msg_id,
        ),
        chats=[linked] if linked is not None else [],
    )


@dataclass
class Call:
    name: str
    target: Any
    kwargs: dict[str, Any] = field(default_factory=dict)


class FakeTelegramClient:
    """See the module docstring.

    ``entities`` maps a username or a peer id to an entity (a peer id stands
    for Telethon's in-memory cache, which a restarted client does not have);
    an ``InputPeerChannel`` finds a channel by id and access hash whatever is
    cached, as do the ``reachable`` channels. ``full`` maps a username or peer
    id to a GetFullChannel answer; ``bios`` a user id or username to an about
    text; ``history`` a peer id to the messages ``iter_messages`` yields
    newest first. ``authorized`` is Telegram's answer each time it is asked
    whether the account is signed in (the last one repeats): ``get_me`` asks
    on every call and answers None for «no»; ``is_user_authorized`` asks once
    and keeps the answer, as Telethon's ``client._authorized`` does."""

    def __init__(
        self,
        *,
        authorized: Iterable[bool] = (True,),
        me: Any = None,
        entities: dict[Any, Any] | None = None,
        full: dict[Any, Any] | None = None,
        bios: dict[Any, str] | None = None,
        history: dict[int, list[Any]] | None = None,
        dialogs: Iterable[Any] = (),
        pinned: dict[Any, Any] | None = None,
        reachable: Iterable[Any] = (),
    ) -> None:
        self._answers = list(authorized)
        self._authorized: bool | None = None
        # Questions Telegram leaves unanswered, in turn (a timeout, a 500):
        # get_me raises each; is_user_authorized swallows it and keeps False,
        # as Telethon does with any RPCError on its GetState.
        self.unanswered: list[BaseException] = []
        self.me = me if me is not None else user(5100000001, first_name="Namuna")
        self.entities = dict(entities or {})
        self.full = dict(full or {})
        self.bios = dict(bios or {})
        self.history = dict(history or {})
        self.dialogs = list(dialogs)
        self.pinned = dict(pinned or {})
        # Channels Telegram finds by id and access hash though nothing is
        # cached (the restarted client of a test).
        self.reachable = list(reachable)
        self.calls: list[Call] = []
        self.handlers: list[tuple[Any, Any]] = []
        self.connected = False
        # (call name, target or None for any) → exception to raise
        self.fail: dict[tuple[str, Any], BaseException] = {}

    def _record(self, name: str, target: Any = None, **kwargs: Any) -> None:
        self.calls.append(Call(name, target, kwargs))
        for key in ((name, target), (name, None)):
            try:
                planted = self.fail.get(key)
            except TypeError:  # an InputPeer target is not hashable
                continue
            if planted is not None:
                raise planted

    def named(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name]

    # --- connection ------------------------------------------------------------

    async def connect(self) -> None:
        self._record("connect")
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.calls.append(Call("disconnect", None))
        self.connected = False

    def _ask(self) -> bool:
        if self.unanswered:
            raise self.unanswered.pop(0)
        if len(self._answers) > 1:
            return self._answers.pop(0)
        return self._answers[0]

    async def is_user_authorized(self) -> bool:
        self._record("is_user_authorized")
        # Telethon asks Telegram only while it has no answer, and keeps it
        # (users.py: ``if self._authorized is None``); connect() never resets.
        if self._authorized is None:
            try:
                self._authorized = self._ask()
            except errors.RPCError:
                self._authorized = False
        return self._authorized

    async def get_me(self) -> Any:
        # users.getUsers([self]) on every call; Telethon answers None for the
        # 401 family (a planted UnauthorizedError included) and raises the rest.
        try:
            self._record("get_me")
        except errors.UnauthorizedError:
            return None
        return self.me if self._ask() else None

    async def catch_up(self) -> None:
        self._record("catch_up")

    # --- updates ------------------------------------------------------------------

    def add_event_handler(self, callback: Any, event: Any) -> None:
        self.calls.append(Call("add_event_handler", type(event).__name__))
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback: Any) -> None:
        self.calls.append(Call("remove_event_handler", None))
        self.handlers = [(cb, ev) for cb, ev in self.handlers if cb is not callback]

    # --- reads ----------------------------------------------------------------------

    async def _dialogs(self) -> AsyncIterator[Any]:
        for entity in self.dialogs:
            if isinstance(entity, BaseException):
                raise entity
            yield SimpleNamespace(entity=entity)

    def iter_dialogs(self) -> AsyncIterator[Any]:
        self._record("iter_dialogs")
        return self._dialogs()

    async def get_entity(self, target: Any) -> Any:
        self._record("get_entity", target)
        if isinstance(target, InputPeerChannel):
            for entity in [*self.entities.values(), *self.reachable]:
                if (
                    isinstance(entity, Channel)
                    and entity.id == target.channel_id
                    and entity.access_hash == target.access_hash
                ):
                    return entity
            raise errors.ChannelInvalidError(request=None)
        if target not in self.entities:
            raise ValueError(f"Could not find the input entity for {target!r}")
        return self.entities[target]

    async def _history(self, chat: Any, limit: int | None) -> AsyncIterator[Any]:
        messages = self.history.get(chat, [])
        for index, item in enumerate(messages):
            if limit is not None and index >= limit:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def iter_messages(self, chat: Any, limit: int | None = None, **kwargs: Any) -> Any:
        self._record("iter_messages", chat, limit=limit, **kwargs)
        return self._history(chat, limit)

    async def get_messages(self, target: Any, ids: Any = None) -> Any:
        self._record("get_messages", target, ids=ids)
        return self.pinned.get(target)

    # --- raw requests ------------------------------------------------------------------

    async def __call__(self, request: Any) -> Any:
        kind = type(request).__name__
        if kind == "JoinChannelRequest":
            target = request.channel
            self._record(kind, _key(target))
            return SimpleNamespace(updates=[])
        if kind == "GetFullChannelRequest":
            target = request.channel
            self._record(kind, _key(target))
            return self.full.get(_key(target), full_channel())
        if kind == "GetFullUserRequest":
            target = request.id
            self._record(kind, _key(target))
            about = self.bios.get(_key(target), "")
            return SimpleNamespace(full_user=SimpleNamespace(about=about))
        raise AssertionError(f"the reader sent a request the fake does not allow: {kind}")


def _key(target: Any) -> Any:
    """A request's target the way the fake's tables are keyed: the username
    of an entity that has one, else its peer id; a bare value as given."""
    if isinstance(target, Channel | User):
        return target.username or peer(target)
    return target
