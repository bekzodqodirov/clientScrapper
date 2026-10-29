"""``telegram_monitor`` — what the reader stored, as observations (03 §2.3).

No network and no Telethon (07 ``test_collector_imports``): the reader
(``userbot/``) stores the prefilter's hits in ``interactions`` and, once a
week, a channel's about as an ``evidence`` row; this collector reads both
back. The worker wakes it on ``NOTIFY cs_interaction`` (04 §0, 01 N-06) with
the ``*/15`` sweep as the fallback (03 §3).

A run takes, oldest first, at most ``BATCH`` stored messages no run has
handled yet (``evidence_id`` and ``processed_at`` NULL, the text not purged,
the chat still readable — ``chats.readable_ids``) and holds them (``FOR
UPDATE SKIP LOCKED``, 04 §0) until it commits, so a reader's edit waits and
then finds the row handled (``ingest.handle_edit``). Of each:

* **the tout filter**, before any model call (03 §2.3): ``COMPETITOR_RE`` on
  the speaker's username (its words: ``fx_express`` is «fx express»; a glued
  one by ``keywords.competitor_handle``: «halolcargo»), name (camel case
  split: «HalolCargo») and bio; on the message only when it is offer-shaped,
  and then with ``COMPETITOR_SELF_RE``, the cargo core gate 2 reads in a
  self-description (``offers_cargo``, 04 §6): a person's or a poster's ask —
  buyer intent or a question form, «kim kargo qiladi?» — is a buyer's,
  whatever service it names, and «Доставка по Ташкенту» is no offer of
  cargo. The bio is
  ``interactions.sender_bio``, which only ``userbot/about.py`` fills — some
  hours after the message, for a poster seen for the first time — so a row
  that has none yet takes the sender's newest bio from any of their rows
  (``chats.known_bios``, the reader's own 30-day cache). For a person posting
  in a group also ``TOUT_PRICELISTS`` price-list-shaped messages
  (``keywords.pricelist_shaped``) of the sender in the ``TOUT_WINDOW`` up to
  and including this one, or a forward from a channel that is not the
  sender; for a chat speaking for itself also a ``chat_monitors`` row of role
  ``competitor`` (the seeds' cargo channels). A tout is emitted with
  ``competitor`` alone — excluded as a lead, kept for the weekly
  competitor-offer diff. A person's or a poster's tout carries no person
  data and no identity (06 §5.1: a competitor's person has no lead purpose):
  the resolver rejects it as ``competitor`` (``companies.tout_refusal``), so
  no company or contact is made of it and retention R2a takes the row at
  30 d. A chat's tout keeps the chat (company-tier: the competitor's
  channel, which the gate excludes);
* **the observation**: a group message by a person → ``kind="post"``
  (``parsed.kind='intent_post'``): the poster's username, first + last name
  and, from the bio, the firm a legal form anchors («Namuna Tekstil Savdo
  MChJ direktori» → the firm and the role, 04 §9.1); ``intent_ask``, the
  volume (``VOLUME_RE``, the largest stated: ``volume_m3_3plus`` /
  ``volume_m3_1to3``), ``container_truck``, ``china_origin``, ``b2c_marker``;
  ``attrs.repeat_count_60d`` for a repeat poster (``REPEAT_MIN`` intent posts
  in ``REPEAT_WINDOW``: a trader, not B2C). A channel's own post, or a chat
  posting in a group as itself → ``kind="post"`` with
  ``attrs.chat_type='channel'`` (``parsed.kind='profile'``) for that chat:
  its username, title, the contacts the post names, ``wholesale_bulky``
  (B2B or supply words with a bulky category), ``container_truck``,
  ``china_origin`` and the volume. What a ``buyers``-role chat publishes as
  itself — a classifieds feed such as @Optom_bozor (``chats.BUYERS_ROLE``) —
  is a poster's ad, never the chat speaking (03 §2.3: «the lead is the
  poster, not the channel»): ``attrs.chat_type='classifieds'``
  (``parsed.kind='intent_post'``, so its phones and handle stay person-tier
  and R3 scrubs them), the firm, phones, Telegram handle, site, Instagram and
  STIR the ad itself names, and the signals of an ask and of an offer;
* **the evidence**: the message text (``text/plain``) at its t.me URL,
  supplied to the runner (``Collector.supply``), which stores it — one row
  per message, so two messages with the same words are each cited where they
  were said.

A person's message that carries no buyer trigger — kept by the prefilter only
because it names prices, so the tout rule can count it (02 §2.12) — becomes
no observation: it is marked processed and goes at 30 days like every
unconverted row (02 §4 R2a).

Then every channel about the reader stored (``evidence.meta.kind =
chats.ABOUT_EVIDENCE_KIND``) that no observation cites yet, of a chat whose
about the reader would read now (``chats.about_readable``: not one the owner
switched off) and that is not a classifieds feed: ``kind="company"``
(``parsed.kind='profile'``, 02 §2.5) with the phones, website, Instagram,
address, city and STIR its description names; the runner adopts the reader's
row as its evidence. An about is never an ``interactions`` row.

Once the runner has written the run (``after_write``, inside its
transaction): each message row is linked to its evidence and observation
(one an earlier run wrote for the same message and bytes, when this run's
was a repeat); a tout's and a counted-only row are marked processed —
nothing more is due for them — and so is a row whose observation the
database refused (its evidence is linked; no later run could write the
observation on that evidence, so trying again would only hold the queue). A
standing row stays ``processed_at IS NULL``, the classifier's
queue (04 §4.1, T-15), announced with ``NOTIFY cs_interaction,
'telegram_monitor:<id>'`` so classify and score wake at once (04 §0); the
worker does not wake this collector on its own notification.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

import sqlalchemy as sa

from clientscrapper.collectors.base import Budget, Collector, Observation, Placed
from clientscrapper.collectors.keywords import (
    B2B_RE,
    B2C_RE,
    CHINA_RE,
    COMPETITOR_RE,
    CONTAINER_RE,
    INTENT_RE,
    LEGAL_FORM_RE,
    SUPPLY_RE,
    VOLUME_RE,
    bulky_categories,
    cargo_offer,
    competitor_handle,
    pricelist_shaped,
)
from clientscrapper.config import settings
from clientscrapper.db.models import ChatMonitor, Evidence, Interaction
from clientscrapper.db.models import Observation as ObservationRow
from clientscrapper.services import chats
from clientscrapper.services import evidence as evidence_store
from clientscrapper.services.companies import role_rank
from clientscrapper.services.normalise import (
    canonical_city,
    find_stir,
    instagram_handles,
    parse_phones,
    telegram_handles,
    website_key,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from clientscrapper.services.http import Fetcher

SOURCE_KEY: Final = chats.SOURCE_KEY
# Stored messages per run, and stored abouts (the resolver's own cap is 200).
BATCH: Final = 200
ABOUT_BATCH: Final = 50
# 03 §2.3: ≥ 3 price-list-shaped messages per sender in 7 days is a tout.
TOUT_PRICELISTS: Final = 3
TOUT_WINDOW: Final = timedelta(days=7)
# 03 §2.3: ≥ 3 intent posts in 60 days is a repeat poster (a trader).
REPEAT_MIN: Final = 3
REPEAT_WINDOW: Final = timedelta(days=60)
EXCERPT_MAX: Final = 600
ADDRESS_MAX: Final = 200
# A name a legal form anchors: at most this many words beside it.
NAME_WORDS_MAX: Final = 4
CONTENT_TYPE: Final = "text/plain"
MESSAGE_URL: Final = "https://t.me/{username}/{message_id}"
PRIVATE_MESSAGE_URL: Final = "https://t.me/c/{bare}/{message_id}"

# Why a message is a tout (``attrs.tout``).
Tout = Literal[
    "competitor_sender",
    "competitor_text",
    "competitor_chat",
    "pricelists_7d",
    "forwarded_channel",
]

# A post's words, read once per message (``signals_of``).
_SEPARATORS = re.compile(r"[|,;:!?\n•·/\\()\[\]{}<>]+|\s[-–—]\s")
_HANDLE_JOINERS = re.compile(r"[_.\d]+")
# «HalolCargo» → «Halol Cargo»: a display name glued in camel case.
_CAMEL = re.compile(r"(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])")
_ADDRESS_RE = re.compile(
    r"(?:📍|(?<!\w)(?:manzil(?:imiz)?|манзил(?:имиз)?|адрес|address)\s*[:\-–—]?)"
    r"[ \t]*(?P<address>[^\n]{3,})",
    re.I,
)
# A website as written: a scheme, a «www.» or one of these endings — a bare
# «Toshkent.Chorsu» is no site — then services/normalise.website_key keeps only
# a firm's own host (no social, marketplace, link hub or directory).
_SITE_RE = re.compile(
    r"(?<![\w@./-])(?P<url>(?P<scheme>https?://)?(?P<host>(?:[a-z0-9-]+\.)+"
    r"(?P<tld>[a-z]{2,24}))(?:/[^\s<>\"'«»()]*)?)",
    re.I,
)
_SITE_TLDS: Final = frozenset(
    {"uz", "com", "ru", "net", "org", "info", "biz", "shop", "store", "online", "site"}
    | {"kz", "cn", "io", "me", "pro", "co", "group", "trade"}
)


# --- what was stored ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Chat:
    """The chat a message was stored from (its ``chat_monitors`` row)."""

    tg_chat_id: int
    chat_type: str  # group | channel
    title: str | None = None
    username: str | None = None
    role: str = "market"


@dataclass(frozen=True, slots=True)
class Post:
    """One stored message (an ``interactions`` row) with the facts the tout
    filter and the repeat count need about its sender's other messages.
    ``pricelists_7d`` counts the sender's price-list-shaped messages in the
    ``TOUT_WINDOW`` up to and including this one, ``intents_60d`` their
    intent posts in the ``REPEAT_WINDOW``; ``competitor_sender`` says the
    sender is a chat whose ``chat_monitors`` role is ``competitor``."""

    interaction_id: int
    chat: Chat
    tg_message_id: int
    text: str
    occurred_at: datetime
    stored_at: datetime
    sender_id: int | None = None
    sender_username: str | None = None
    sender_name: str | None = None
    sender_bio: str | None = None
    fwd_from_peer_id: int | None = None
    pricelists_7d: int = 0
    intents_60d: int = 0
    competitor_sender: bool = False


SpeakerKind = Literal["person", "chat", "poster"]


@dataclass(frozen=True, slots=True)
class Speaker:
    """Who a message speaks for: a person (a user in a group), a chat — a
    channel's own post, or a channel or group posting in a group as itself —
    or a poster the text alone names: an ad a ``buyers``-role chat (a
    classifieds feed, ``chats.BUYERS_ROLE``) publishes as itself."""

    kind: SpeakerKind
    tg_id: int | None
    username: str | None
    name: str | None


def speaker_of(post: Post) -> Speaker:
    chat = post.chat
    sender = post.sender_id
    if chat.chat_type == "channel" or sender == chat.tg_chat_id:
        if chat.role == chats.BUYERS_ROLE:
            return Speaker("poster", None, None, None)
        return Speaker("chat", chat.tg_chat_id, chat.username, chat.title)
    if sender is not None and sender < 0:
        return Speaker("chat", sender, post.sender_username, post.sender_name)
    return Speaker("person", sender, post.sender_username, post.sender_name)


# --- the rules (pure) -----------------------------------------------------------------


def handle_words(username: str | None) -> str:
    """A username as words for the word-bounded patterns: «fx_express» →
    «fx express», «halol.cargo777» → «halol cargo»."""
    return " ".join(_HANDLE_JOINERS.sub(" ", username or "").split())


def name_words(name: str | None) -> str:
    """A display name with its camel case split: «HalolCargo» → «Halol Cargo»."""
    return _CAMEL.sub(" ", name or "")


def is_channel_forward(post: Post) -> bool:
    """A forward from a channel other than the sender's own (03 §2.3)."""
    peer = post.fwd_from_peer_id
    return peer is not None and chats.is_channel_peer(peer) and peer != post.sender_id


def tout_reasons(post: Post) -> tuple[Tout, ...]:
    """Why ``post`` is a competitor's (03 §2.3), in a fixed order; () when it
    is not. The price-list and forward rules are about posters: a chat posting
    as itself (a wholesaler channel's posts copied into its discussion group)
    is judged by its words and its role alone; a classifieds ad's poster has
    no id to count price lists by, and is judged by its words and its
    forward."""
    speaker = speaker_of(post)
    reasons: list[Tout] = []
    said_by = " \n ".join(
        part
        for part in (
            handle_words(speaker.username),
            name_words(speaker.name),
            post.sender_bio,
        )
        if part
    )
    if (said_by and COMPETITOR_RE.search(said_by)) or competitor_handle(speaker.username):
        reasons.append("competitor_sender")
    if offers_cargo(post.text, speaker=speaker.kind):
        reasons.append("competitor_text")
    if speaker.kind == "chat":
        if post.competitor_sender or (
            post.chat.role == "competitor" and speaker.tg_id == post.chat.tg_chat_id
        ):
            reasons.append("competitor_chat")
        return tuple(reasons)
    if speaker.kind == "person" and post.pricelists_7d >= TOUT_PRICELISTS:
        reasons.append("pricelists_7d")
    if is_channel_forward(post):
        reasons.append("forwarded_channel")
    return tuple(reasons)


def offers_cargo(text: str, *, speaker: SpeakerKind) -> bool:
    """The text rule of the tout filter (03 §2.3, read as 04 §6 reads a
    self-description; ``keywords.cargo_offer``): a person's or a classifieds
    poster's ask is a buyer's whatever service it names («Guangzhoudan 2 kub
    mato bor, kim kargo qiladi?» is the customer GSR looks for); a chat
    speaking as itself never asks — its post is its self-description."""
    return cargo_offer(text, may_ask=speaker != "chat")


def buyer_trigger(text: str) -> bool:
    """A group message's own reason to be read as demand: a volume, a
    container or truck, or buyer intent (the prefilter's group triggers less
    the price list, which is kept only to be counted)."""
    return bool(
        VOLUME_RE.search(text) or CONTAINER_RE.search(text) or INTENT_RE.search(text)
    )


def stated_volume(text: str) -> tuple[float, str] | None:
    """The largest volume ``VOLUME_RE`` reads in ``text`` (m³) and its
    fragment as written."""
    best: tuple[float, str] | None = None
    for match in VOLUME_RE.finditer(text):
        value = float(match.group(1).replace(",", "."))
        if best is None or value > best[0]:
            best = (value, match.group(0).strip())
    return best


@dataclass(slots=True)
class Reading:
    """What a text says, as the collector contract wants it."""

    signal_types: list[str] = field(default_factory=list)
    volume_m3: float | None = None
    basis: str = "none"
    volume_text: str | None = None
    origin_text: str | None = None


def read_text(text: str, *, speaker: SpeakerKind) -> Reading:
    """The signal types of a standing (non-tout) text (03 §1.2, §2.3): a
    person's ask carries ``intent_ask`` and may carry ``b2c_marker``; a
    chat's post carries ``wholesale_bulky`` when B2B or supply words name a
    bulky category; a classifieds poster's ad either (a buyer's ask or a
    seller's offer), ``b2c_marker`` only beside an ask; all carry the volume,
    ``container_truck`` and ``china_origin``."""
    reading = Reading()
    signals: list[str] = []
    asks = speaker != "chat" and INTENT_RE.search(text) is not None
    if asks:
        signals.append("intent_ask")
    if (
        speaker != "person"
        and (B2B_RE.search(text) or SUPPLY_RE.search(text))
        and bulky_categories(text)
    ):
        signals.append("wholesale_bulky")
    volume = stated_volume(text)
    container = CONTAINER_RE.search(text)
    if volume is not None:
        reading.volume_m3, reading.volume_text = volume
        reading.basis = "stated"
        if volume[0] >= 3:
            signals.append("volume_m3_3plus")
        elif volume[0] >= 1:
            signals.append("volume_m3_1to3")
    elif container is not None:
        reading.basis = "container_words"
        reading.volume_text = container.group(0)
    if container is not None:
        signals.append("container_truck")
    china = CHINA_RE.search(text)
    if china is not None:
        signals.append("china_origin")
        reading.origin_text = china.group(0)
    if (speaker == "person" or asks) and B2C_RE.search(text):
        signals.append("b2c_marker")
    reading.signal_types = signals
    return reading


@dataclass(frozen=True, slots=True)
class Firm:
    """A firm a text names by its legal form, as written, and a role word
    outside the name («direktori»)."""

    name: str
    role: str | None


def _words_before(segment: str) -> tuple[int, int] | None:
    """The span of the capitalised words that end ``segment`` (≤ NAME_WORDS_MAX)."""
    words = list(re.finditer(r"\S+", segment))
    kept: list[re.Match[str]] = []
    for word in reversed(words):
        if len(kept) == NAME_WORDS_MAX or not _name_word(word.group(0)):
            break
        kept.append(word)
    if not kept:
        return None
    return kept[-1].start(), kept[0].end()


def _words_after(segment: str) -> tuple[int, int] | None:
    """The span of the capitalised words that start ``segment``."""
    kept: list[re.Match[str]] = []
    for word in re.finditer(r"\S+", segment):
        if len(kept) == NAME_WORDS_MAX or not _name_word(word.group(0)):
            break
        kept.append(word)
    if not kept:
        return None
    return kept[0].start(), kept[-1].end()


def _name_word(word: str) -> bool:
    """A word of a firm's name: it starts with a capital letter, a digit or an
    opening quote («Namuna», «"Namuna», «777»)."""
    first = word.lstrip("\"'«“„")[:1]
    return bool(first) and (first.isupper() or first.isdigit())


def firm_in(text: str | None) -> Firm | None:
    """The firm a bio names by its legal form, as written (04 §9.1: «Namuna
    Tekstil Savdo MChJ direktori» → «Namuna Tekstil Savdo MChJ», role
    «direktori»): the capitalised words before the form, else after it
    («ООО Namuna Savdo»). The role is the highest-ranked word outside the
    name (``companies.role_rank``, the resolver's own ladder)."""
    if not text:
        return None
    form = LEGAL_FORM_RE.search(text)
    if form is None:
        return None
    head_start = max(
        (m.end() for m in _SEPARATORS.finditer(text, 0, form.start())), default=0
    )
    before = _words_before(text[head_start : form.start()])
    if before is not None:
        start, end = head_start + before[0], form.end()
    else:
        tail = _SEPARATORS.search(text, form.end())
        tail_end = tail.start() if tail else len(text)
        after = _words_after(text[form.end() : tail_end])
        if after is None:
            return None
        start, end = form.start(), form.end() + after[1]
    name = text[start:end].strip().rstrip(".,;:")
    outside = f"{text[:start]} {text[end:]}"
    ranked = [
        (role_rank(word), index, word)
        for index, word in enumerate(re.findall(r"[^\W\d_][\w'ʻʼ‘’`-]*", outside))
    ]
    best = max((r for r in ranked if r[0] > 0), key=lambda r: (r[0], -r[1]), default=None)
    return Firm(name=name, role=best[2] if best else None)


def phones_in(*texts: str | None) -> list[str]:
    """The valid phones ``texts`` name, as written, in order, each once —
    ``normalise.parse_phones``'s reading (a labelled STIR is no phone)."""
    found: list[str] = []
    for text in texts:
        for phone in parse_phones(text or ""):
            if phone.raw.strip() not in found:
                found.append(phone.raw.strip())
    return found


def websites_in(text: str) -> list[str]:
    """The firm websites ``text`` names, as written (see ``_SITE_RE``)."""
    found: list[str] = []
    for match in _SITE_RE.finditer(text):
        host = match["host"].lower()
        if not (
            match["scheme"]
            or host.startswith("www.")
            or match["tld"].lower() in _SITE_TLDS
        ):
            continue
        url = match["url"].rstrip(".,;:!?")
        if website_key(url) is not None and url not in found:
            found.append(url)
    return found


def address_in(text: str) -> str | None:
    """The line an address label («Manzil:», «Адрес:», «📍») introduces."""
    match = _ADDRESS_RE.search(text)
    if match is None:
        return None
    address = match["address"].strip()
    return evidence_store.clip(address, ADDRESS_MAX) or None


def message_url(chat: Chat, message_id: int) -> str:
    """The message's public link, or its ``t.me/c/`` link in a chat with no
    username (03 §2.3)."""
    if chat.username:
        return MESSAGE_URL.format(username=chat.username, message_id=message_id)
    bare = chats.bare_channel_id(chat.tg_chat_id)
    return PRIVATE_MESSAGE_URL.format(bare=bare, message_id=message_id)


def source_ref(tg_chat_id: int, message_id: int | None = None) -> str:
    """``tg:<chat>:<msg>`` — 02's evidence ``source_ref``; ``tg:<chat>`` for a
    chat's about (``userbot/about.py`` stores it so)."""
    return f"tg:{tg_chat_id}" if message_id is None else f"tg:{tg_chat_id}:{message_id}"


def body_of(post: Post) -> bytes:
    """The evidence bytes of a message: its text."""
    return post.text.encode("utf-8")


def parse_post(post: Post) -> Observation | None:
    """One stored message → its observation (03 §2.3), or None for a
    person's message the prefilter kept only so the tout rule could count it
    (no buyer trigger: 02 §2.12)."""
    speaker = speaker_of(post)
    reasons = tout_reasons(post)
    if speaker.kind == "person" and not reasons and not buyer_trigger(post.text):
        return None
    body = body_of(post)
    attrs: dict[str, Any] = {"chat_title": post.chat.title}
    obs = Observation(
        source=SOURCE_KEY,
        source_ref=source_ref(post.chat.tg_chat_id, post.tg_message_id),
        url=message_url(post.chat, post.tg_message_id),
        fetched_at=post.stored_at,
        raw_sha256=hashlib.sha256(body).hexdigest(),
        kind="post",
        excerpt=evidence_store.clip(post.text, EXCERPT_MAX),
        published_at=post.occurred_at,
        attrs=attrs,
    )
    attrs["chat_type"] = _CHAT_TYPES[speaker.kind]
    if speaker.kind == "chat":
        # The chat speaks for itself (company-tier), a tout's too: a
        # competitor's channel is a company the gate excludes.
        obs.company_name_raw = speaker.name
        obs.telegram = speaker.username
        obs.phones = phones_in(post.text)
        _company_contacts(obs, post.text)
        if speaker.tg_id is not None:
            obs.external_ids = {"tg_chat_id": str(speaker.tg_id)}
    if reasons:
        # A person's or a poster's tout: the offer, and nobody (06 §5.1).
        attrs["tout"] = list(reasons)
        obs.signal_types = ["competitor"]
        return obs
    reading = read_text(post.text, speaker=speaker.kind)
    obs.signal_types = reading.signal_types
    obs.volume_text = reading.volume_text
    obs.origin_text = reading.origin_text
    attrs.update(volume_m3=reading.volume_m3, basis=reading.basis)
    if speaker.kind == "person":
        firm = firm_in(post.sender_bio)
        obs.company_name_raw = firm.name if firm else None
        obs.person_name = speaker.name
        obs.person_role = firm.role if firm else None
        obs.person_telegram = speaker.username
        obs.phones = phones_in(post.text, post.sender_bio)
        repeat = post.intents_60d >= REPEAT_MIN
        attrs.update(
            frequency="repeat" if repeat else None,
            repeat_count_60d=post.intents_60d if repeat else None,
        )
        if speaker.tg_id is not None:
            attrs["tg_user_id"] = speaker.tg_id
    elif speaker.kind == "poster":
        # A classifieds ad: the poster the text names, never the feed. Its
        # phones and handle are the poster's (an intent post: person-tier,
        # scrubbed by R3); the firm a legal form anchors is the ad's.
        firm = firm_in(post.text)
        obs.company_name_raw = firm.name if firm else None
        obs.phones = phones_in(post.text)
        handles = [h for h in telegram_handles(post.text) if h != post.chat.username]
        obs.person_telegram = handles[0] if handles else None
        _company_contacts(obs, post.text)
    return obs


# ``attrs.chat_type`` by who speaks: 02 §2.5 maps 'channel' to a profile and
# anything else to an intent post.
_CHAT_TYPES: Final[Mapping[SpeakerKind, str]] = {
    "person": "group",
    "chat": "channel",
    "poster": "classifieds",
}


def _company_contacts(obs: Observation, text: str) -> None:
    """The site, Instagram account and STIR a post names (company-tier)."""
    sites = websites_in(text)
    obs.website = sites[0] if sites else None
    handles = instagram_handles(text)
    obs.instagram = handles[0] if handles else None
    obs.stir = find_stir(text)


def parse_about(
    body: bytes, *, url: str, fetched_at: datetime, competitor_chat: bool = False
) -> list[Observation]:
    """A chat's weekly about (the JSON ``userbot/about.py`` stores) → one
    ``kind="company"`` observation (``parsed.kind='profile'``, 02 §2.5) with
    the contacts its description and pinned post name (03 §2.3).
    ``competitor_chat``: the chat's ``chat_monitors`` role is competitor."""
    data = json.loads(body)
    if not isinstance(data, Mapping):
        return []
    chat_id = data.get("tg_chat_id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return []
    about, pinned = _text(data.get("about")), _text(data.get("pinned"))
    title, username = _text(data.get("title")), _text(data.get("username"))
    text = "\n".join(part for part in (about, pinned) if part)
    attrs: dict[str, Any] = {
        "chat_type": _text(data.get("chat_type")),
        "chat_title": title,
        "member_count": data.get("member_count"),
    }
    obs = Observation(
        source=SOURCE_KEY,
        source_ref=source_ref(chat_id),
        url=url,
        fetched_at=fetched_at,
        raw_sha256=hashlib.sha256(body).hexdigest(),
        kind="company",
        company_name_raw=title,
        telegram=username,
        phones=phones_in(text),
        address=address_in(text),
        city=canonical_city(text),
        stir=find_stir(text),
        excerpt=evidence_store.clip(
            about or pinned or title or username or "", EXCERPT_MAX
        ),
        external_ids={"tg_chat_id": str(chat_id)},
        attrs=attrs,
    )
    sites = websites_in(text)
    obs.website = sites[0] if sites else None
    handles = instagram_handles(text)
    obs.instagram = handles[0] if handles else None
    said_by = " \n ".join(p for p in (handle_words(username), name_words(title)) if p)
    reasons: list[Tout] = []
    if (said_by and COMPETITOR_RE.search(said_by)) or competitor_handle(username):
        reasons.append("competitor_sender")
    if offers_cargo(text, speaker="chat"):
        reasons.append("competitor_text")
    if competitor_chat:
        reasons.append("competitor_chat")
    if reasons:
        attrs["tout"] = reasons
        obs.signal_types = ["competitor"]
        return [obs]
    reading = read_text(text, speaker="chat")
    obs.signal_types = reading.signal_types
    obs.volume_text, obs.origin_text = reading.volume_text, reading.origin_text
    attrs.update(volume_m3=reading.volume_m3, basis=reading.basis)
    return [obs]


def _text(value: Any) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def window_count(times: Iterable[datetime], *, at: datetime, window: timedelta) -> int:
    """How many of ``times`` fall in ``(at - window, at]``."""
    return sum(1 for t in times if at - window < t <= at)


# --- the stored rows ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class History:
    """A sender's stored message: when, and what shape (for the counts)."""

    sender_id: int
    occurred_at: datetime
    pricelist: bool
    intent: bool


async def _histories(
    session: AsyncSession, rows: Sequence[Interaction]
) -> dict[int, list[History]]:
    """The stored messages of the batch's posters across every chat, inside
    the longest window a count needs."""
    senders = {r.sender_id for r in rows if r.sender_id is not None and r.sender_id > 0}
    if not senders:
        return {}
    since = min(r.occurred_at for r in rows) - max(TOUT_WINDOW, REPEAT_WINDOW)
    until = max(r.occurred_at for r in rows)
    found = await session.execute(
        sa.select(Interaction.sender_id, Interaction.occurred_at, Interaction.text).where(
            Interaction.sender_id.in_(sorted(senders)),
            Interaction.occurred_at > since,
            Interaction.occurred_at <= until,
            Interaction.text.is_not(None),
        )
    )
    out: dict[int, list[History]] = {}
    for sender_id, occurred_at, text in found.all():
        out.setdefault(int(sender_id), []).append(
            History(
                sender_id=int(sender_id),
                occurred_at=occurred_at,
                pricelist=pricelist_shaped(text),
                intent=INTENT_RE.search(text) is not None,
            )
        )
    return out


def _post_of(
    row: Interaction,
    monitor: ChatMonitor,
    history: Sequence[History],
    competitor_chats: frozenset[int],
    bios: Mapping[int, str],
) -> Post:
    at = row.occurred_at
    bio = row.sender_bio
    if bio is None and row.sender_id is not None:
        bio = bios.get(row.sender_id)
    return Post(
        interaction_id=row.id,
        chat=Chat(
            tg_chat_id=monitor.tg_chat_id,
            chat_type=monitor.chat_type,
            title=monitor.title,
            username=monitor.username,
            role=monitor.role,
        ),
        tg_message_id=row.tg_message_id,
        text=row.text or "",
        occurred_at=at,
        stored_at=row.created_at,
        sender_id=row.sender_id,
        sender_username=row.sender_username,
        sender_name=row.sender_name,
        sender_bio=bio,
        fwd_from_peer_id=row.fwd_from_peer_id,
        pricelists_7d=window_count(
            (h.occurred_at for h in history if h.pricelist), at=at, window=TOUT_WINDOW
        ),
        intents_60d=window_count(
            (h.occurred_at for h in history if h.intent), at=at, window=REPEAT_WINDOW
        ),
        competitor_sender=row.sender_id in competitor_chats,
    )


async def pending_posts(
    session: AsyncSession, *, now: datetime, limit: int = BATCH
) -> list[Post]:
    """The stored messages no run has handled yet, oldest first, from chats
    the reader may read (``chats.readable_ids`` — never the calibration chat,
    never one the owner switched off). The rows are held (``FOR UPDATE SKIP
    LOCKED``, 04 §0) until the caller's transaction ends: a reader's edit
    that lands meanwhile waits, then finds them handled, and never makes a
    row say what its evidence does not. A row with no bio yet takes the
    sender's newest one from any of their rows (``chats.known_bios``)."""
    readable = await chats.readable_ids(session)
    if not readable:
        return []
    result = await session.execute(
        sa.select(Interaction, ChatMonitor)
        .join(ChatMonitor, ChatMonitor.tg_chat_id == Interaction.tg_chat_id)
        .where(
            Interaction.evidence_id.is_(None),
            Interaction.processed_at.is_(None),
            Interaction.text.is_not(None),
            Interaction.text_purged_at.is_(None),
            Interaction.tg_chat_id.in_(readable),
        )
        .order_by(Interaction.created_at, Interaction.id)
        .limit(limit)
        .with_for_update(of=Interaction, skip_locked=True)
    )
    pairs = [(row, monitor) for row, monitor in result.tuples().all()]
    if not pairs:
        return []
    histories = await _histories(session, [row for row, _ in pairs])
    bios = await chats.known_bios(
        session,
        (r.sender_id for r, _ in pairs if r.sender_bio is None and r.sender_id),
        now=now,
    )
    competitor_chats = frozenset(
        await session.scalars(
            sa.select(ChatMonitor.tg_chat_id).where(ChatMonitor.role == "competitor")
        )
    )
    return [
        _post_of(
            row, monitor, histories.get(row.sender_id or 0, ()), competitor_chats, bios
        )
        for row, monitor in pairs
    ]


async def pending_abouts(
    session: AsyncSession, *, limit: int = ABOUT_BATCH
) -> list[tuple[Evidence, ChatMonitor]]:
    """Channel abouts the reader stored outside any run that no observation
    cites yet, oldest first, of chats whose about the reader would read now
    (``chats.about_readable``: one the owner switched off waits until it is
    switched on again). A group's about names a community, not a firm, and a
    classifieds feed's (``chats.BUYERS_ROLE``) names the board, not a lead:
    the reader keeps them for the companion groups they name, nothing more."""
    cited = sa.exists().where(ObservationRow.evidence_id == Evidence.id)
    result = await session.execute(
        sa.select(Evidence, ChatMonitor)
        .join(
            ChatMonitor,
            sa.func.concat("tg:", ChatMonitor.tg_chat_id) == Evidence.source_ref,
        )
        .where(
            Evidence.source_key == SOURCE_KEY,
            Evidence.meta["kind"].astext == chats.ABOUT_EVIDENCE_KIND,
            Evidence.collector_run_id.is_(None),
            ChatMonitor.chat_type == "channel",
            chats.about_readable(),
            ChatMonitor.role != chats.BUYERS_ROLE,
            ~cited,
        )
        .order_by(Evidence.fetched_at, Evidence.id)
        .limit(limit)
    )
    return [(row, monitor) for row, monitor in result.tuples().all()]


def _about_stand_in(row: Evidence, monitor: ChatMonitor) -> bytes:
    """The about as the row itself still tells it, when its file is gone: the
    excerpt is the description (cut at 600 characters)."""
    payload = {
        "tg_chat_id": monitor.tg_chat_id,
        "username": monitor.username,
        "title": row.title or monitor.title,
        "chat_type": monitor.chat_type,
        "about": row.excerpt,
        "pinned": None,
        "member_count": monitor.member_count,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()


# --- the collector --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Taken:
    interaction_id: int
    done: bool  # nothing more is due: a tout


class TelegramMonitor(Collector):
    key = "telegram_monitor"
    family = "telegram"
    default_cron = "*/15 * * * *"
    default_page_budget = 0

    def __init__(self, fetcher: Fetcher, serp: object | None = None) -> None:
        super().__init__(fetcher, serp)
        self._taken: dict[str, _Taken] = {}
        self._counted: list[int] = []

    async def collect(self, session: AsyncSession, budget: Budget) -> list[Observation]:
        now = self.started_at or datetime.now(settings.tz)
        posts = await pending_posts(session, now=now)
        touts = 0
        for post in posts:
            obs = self.parse_post(post)
            if obs is None:
                self._counted.append(post.interaction_id)
                continue
            tout = bool(obs.attrs.get("tout"))
            touts += tout
            self.supply(
                obs.url,
                body_of(post),
                content_type=CONTENT_TYPE,
                fetched_at=post.stored_at,
            )
            self._taken[obs.source_ref] = _Taken(post.interaction_id, done=tout)
            self.found.append(obs)
        abouts = 0
        for row, monitor in await pending_abouts(session):
            raw = evidence_store.open_raw(row, root=self.evidence_root)
            if raw is not None and hashlib.sha256(raw).hexdigest() != row.sha256:
                raw = None
            found = self.parse_about(
                raw if raw is not None else _about_stand_in(row, monitor),
                url=row.url,
                fetched_at=row.fetched_at,
                competitor_chat=monitor.role == "competitor",
            )
            self.supply(
                row.url,
                raw,
                sha256=row.sha256,
                content_type=row.content_type,
                fetched_at=row.fetched_at,
            )
            for obs in found:
                # The reader's row is the evidence: its bytes and its excerpt.
                self.found.append(
                    replace(obs, raw_sha256=row.sha256, excerpt=row.excerpt)
                )
                abouts += 1
        self.run_meta["telegram"] = {
            "messages": len(posts),
            "touts": touts,
            "counted_only": len(self._counted),
            "abouts": abouts,
        }
        return self.found

    parse_post = staticmethod(parse_post)
    parse_about = staticmethod(parse_about)

    async def after_write(self, session: AsyncSession, placed: list[Placed]) -> None:
        """Link each message row to where its observation landed; mark the
        touts, the counted-only rows and the rows whose observation the
        database refused processed; NOTIFY for each standing row, so the
        classifier and the scorer wake now (04 §0)."""
        now = self.started_at or datetime.now(settings.tz)
        linked = unobserved = 0
        for item in placed:
            taken = self._taken.get(item.observation.source_ref)
            if taken is None:
                continue
            values: dict[str, Any] = {"evidence_id": item.evidence_id}
            if item.observation_id is not None:
                values["observation_id"] = item.observation_id
            else:
                earlier = await _observation_of(
                    session, item.observation.source_ref, item.observation.raw_sha256
                )
                if earlier is not None:
                    # A repeat of a message an earlier run observed: the row
                    # cites that observation and its own evidence.
                    values["observation_id"], values["evidence_id"] = earlier
            done = taken.done or "observation_id" not in values
            if done:
                values["processed_at"] = now
            # ``evidence_id IS NULL``: a belt only — the run holds the row
            # since pending_posts read it, so nothing else can have linked it.
            result = await session.execute(
                sa.update(Interaction)
                .where(
                    Interaction.id == taken.interaction_id,
                    Interaction.evidence_id.is_(None),
                )
                .values(**values)
            )
            if not getattr(result, "rowcount", 0):
                continue
            linked += 1
            if "observation_id" not in values:
                # The database refused its observation (``bad_observation``):
                # the runner never writes one on evidence it already holds,
                # so the row is done, with its evidence (R2a takes it at 30 d).
                unobserved += 1
            elif not done:
                await session.execute(
                    sa.select(
                        sa.func.pg_notify(
                            chats.INTERACTION_CHANNEL,
                            f"{self.key}:{taken.interaction_id}",
                        )
                    )
                )
        if self._counted:
            await session.execute(
                sa.update(Interaction)
                .where(
                    Interaction.id.in_(self._counted),
                    Interaction.evidence_id.is_(None),
                    Interaction.processed_at.is_(None),
                )
                .values(processed_at=now)
            )
        report = self.run_meta.setdefault("telegram", {})
        report["linked"] = linked
        if unobserved:
            report["unobserved"] = unobserved
        # Rows whose evidence the database refused (``bad_evidence``, the run
        # is partial) have nothing to link to: they are read again next run.
        unplaced = len(self._taken.keys() - {i.observation.source_ref for i in placed})
        if unplaced:
            report["unplaced"] = unplaced


async def _observation_of(
    session: AsyncSession, ref: str, sha256: str
) -> tuple[int, int] | None:
    """The observation (and its evidence) an earlier run wrote for message
    ``ref`` with these bytes: the runner found them stored already, or saw the
    message in the last 24 h under another link (its chat changed username)."""
    found = (
        await session.execute(
            sa.select(ObservationRow.id, ObservationRow.evidence_id)
            .join(Evidence, Evidence.id == ObservationRow.evidence_id)
            .where(
                ObservationRow.source_key == SOURCE_KEY,
                ObservationRow.parsed["source_ref"].astext == ref,
                Evidence.sha256 == sha256,
            )
            .order_by(ObservationRow.id.desc())
            .limit(1)
        )
    ).first()
    return (int(found[0]), int(found[1])) if found is not None else None
