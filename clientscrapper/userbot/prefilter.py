"""The reader's prefilter (03 §2.3): which messages are stored at all.

Pure. A message from a monitored chat is stored in ``interactions`` only when
this answers a non-empty tuple, which becomes ``prefilter_hit`` (02 §2.12:
``cardinality(prefilter_hit) > 0`` is a CHECK — "no full chat logs" is
structural, everything else only bumps ``chat_monitors.seen_count``).

The two rules are 03's:

* a **group** message is stored when ``VOLUME_RE``, ``CONTAINER_RE`` or
  ``INTENT_RE`` matches and the text is at least 12 characters long;
* a **channel** post is stored when ``SUPPLY_RE``, ``CONTAINER_RE`` or the
  ``B2B_MARKERS`` match — a channel is a seller, so what it announces is stock,
  never a buyer's ask.

One addition, from 02 §2.12: ``pricelist`` hits "are stored so the tout rule
(≥ 3 price-list-shaped messages per sender in 7 d, 03 §2.3) can be counted". A
group message naming ``PRICELIST_MIN_PRICES`` prices is therefore stored too —
a tout's price list rarely says «kub» or «kerak», and the rule could not count
what was never kept. The other price-list shape, a message over
``PRICELIST_MIN_CHARS``, only labels a message stored for another reason: on
its own it would keep every long post of a group, which is a chat log.

The labels are 02's vocabulary, in one fixed order: ``volume_kub``,
``container``, ``china``, ``intent``, ``wholesale``, ``pricelist``. ``china``
never stores a message by itself; it rides along for the classifier.
"""

from __future__ import annotations

from typing import Final

from clientscrapper.collectors.keywords import (
    B2B_RE,
    CHINA_RE,
    CONTAINER_RE,
    INTENT_RE,
    PRICELIST_MIN_CHARS,
    PRICELIST_MIN_PRICES,
    SUPPLY_RE,
    VOLUME_RE,
    price_count,
)

LABELS: Final = ("volume_kub", "container", "china", "intent", "wholesale", "pricelist")
# 03 §2.3: a group message shorter than this is not stored («Kerak!»).
GROUP_MIN_CHARS: Final = 12
GROUP_TRIGGERS: Final = frozenset({"volume_kub", "container", "intent", "pricelist"})
CHANNEL_TRIGGERS: Final = frozenset({"container", "wholesale"})


def labels(text: str) -> frozenset[str]:
    """Every label whose pattern occurs in ``text``, whatever the chat."""
    found: set[str] = set()
    if VOLUME_RE.search(text):
        found.add("volume_kub")
    if CONTAINER_RE.search(text):
        found.add("container")
    if CHINA_RE.search(text):
        found.add("china")
    if INTENT_RE.search(text):
        found.add("intent")
    if SUPPLY_RE.search(text) or B2B_RE.search(text):
        found.add("wholesale")
    if price_count(text) >= PRICELIST_MIN_PRICES:
        found.add("pricelist")
    return frozenset(found)


def hits(text: str | None, *, chat_type: str) -> tuple[str, ...]:
    """``prefilter_hit`` for a message of a ``chat_type`` chat — empty when the
    message is not to be stored. A private chat is never read (06 §2)."""
    body = (text or "").strip()
    if not body or chat_type == "private":
        return ()
    found = set(labels(body))
    if chat_type == "channel":
        stored = bool(found & CHANNEL_TRIGGERS)
    else:
        stored = len(body) >= GROUP_MIN_CHARS and bool(found & GROUP_TRIGGERS)
    if not stored:
        return ()
    if len(body) > PRICELIST_MIN_CHARS:
        found.add("pricelist")
    return tuple(label for label in LABELS if label in found)
