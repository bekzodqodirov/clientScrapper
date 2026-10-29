"""The ``callback_data`` grammar (06 §1.5, its one home), its parser and its
builders.

Telegram caps ``callback_data`` at 64 bytes; every payload here is ASCII,
colon-separated, prefix first, with one-letter answer codes:

    rv:<review_id>:<a>        p push · d decline · l later · x dnc · m merge-menu ·
                              b not_b2b · c competitor · e edit_phone · h handoff ·
                              o more · k back
    mg:<review_id>:<uuid>     merge into the target company
    mh:<review_id>:<a>        manager card: c contacted · n no_reply · x said_no ·
                              w wrong_or_b2c · l later
    src:<key>:<a>             t toggle · i info · a forbid auto-push
    src:p:<page>              a page of /manbalar
    ch:t:<monitor_id> · ch:p:<page> · ch:noop
    ps:<a>                    y resume · n keep paused · c clear the CRM breaker
    im:<plan_id>:<y|n>        /import's plan
    pg:<purge_log_id>:<y|n>   /unut's plan
    dn:<plan_id>:<y|n>        /dnc's confirm
    pt:<push_log_id>:<y|n>    /push tekshir's confirm

``parse`` matches each pattern with ``re.fullmatch`` — ``re.match`` with
``^…$`` would also take a trailing newline («rv:1:p\\n») — and never
guesses: None is «Bu tugma eskirgan». ``tests/test_bot_callbacks.py`` builds
every keyboard of ``bot/keyboards.KEYBOARD_SAMPLES`` and demands each button
parse, fit 64 bytes and reach a handler (fullprompt #937: an unparsed
button is answered by nobody and spins for 15 seconds).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

Kind = Literal[
    "rv", "mg", "mh", "src", "srcp", "ch", "chp", "chnoop", "ps", "im", "pg", "dn", "pt"
]

CALLBACK_DATA_MAX: Final = 64
# A bigint (review, purge log, push log ids are bigint identities).
BIGINT_MAX: Final = 2**63 - 1

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_GRAMMAR: Final[Mapping[Kind, re.Pattern[str]]] = MappingProxyType(
    {
        "rv": re.compile(r"rv:(?P<ref>\d{1,19}):(?P<action>[pdlxmbcehok])"),
        "mg": re.compile(rf"mg:(?P<ref>\d{{1,19}}):(?P<target>{_UUID})"),
        "mh": re.compile(r"mh:(?P<ref>\d{1,19}):(?P<action>[cnxwl])"),
        "src": re.compile(r"src:(?P<target>[a-z][a-z0-9_]{2,40}):(?P<action>[tia])"),
        "srcp": re.compile(r"src:p:(?P<page>\d{1,3})"),
        "ch": re.compile(r"ch:t:(?P<ref>\d{1,10})"),
        "chp": re.compile(r"ch:p:(?P<page>\d{1,3})"),
        "chnoop": re.compile(r"ch:noop"),
        "ps": re.compile(r"ps:(?P<action>[ync])"),
        "im": re.compile(r"im:(?P<ref>\d{1,10}):(?P<action>[yn])"),
        "pg": re.compile(r"pg:(?P<ref>\d{1,19}):(?P<action>[yn])"),
        "dn": re.compile(r"dn:(?P<ref>\d{1,10}):(?P<action>[yn])"),
        "pt": re.compile(r"pt:(?P<ref>\d{1,19}):(?P<action>[yn])"),
    }
)

# The owner card's letters and the answers they are (02 §2.9 lead_reviews.answer);
# o/k/m are the card's own menus, not answers.
REVIEW_ANSWERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "p": "push",
        "d": "decline",
        "l": "later",
        "x": "dnc",
        "b": "not_b2b",
        "c": "competitor",
        "e": "edit_phone",
        "h": "handoff",
    }
)
REVIEW_MENUS: Final = frozenset({"m", "o", "k"})
MANAGER_ANSWERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "c": "contacted",
        "n": "no_reply",
        "x": "said_no",
        "w": "wrong_or_b2c",
        "l": "later",
    }
)


@dataclass(frozen=True, slots=True)
class Callback:
    kind: Kind
    action: str | None = None
    # review_id | monitor_id | plan_id | purge_log_id | push_log_id
    ref: int | None = None
    # uuid | source key
    target: str | None = None
    page: int | None = None


def parse(data: str) -> Callback | None:
    """The first pattern that matches ``data`` whole, or None."""
    for kind, pattern in _GRAMMAR.items():
        found = pattern.fullmatch(data)
        if found is None:
            continue
        groups = found.groupdict()
        ref = int(groups["ref"]) if groups.get("ref") is not None else None
        if ref is not None and ref > BIGINT_MAX:
            return None
        page = int(groups["page"]) if groups.get("page") is not None else None
        return Callback(
            kind=kind,
            action=groups.get("action"),
            ref=ref,
            target=groups.get("target"),
            page=page,
        )
    return None


# --- the builders (the only way a keyboard writes callback_data) -----------------------


def review(review_id: int, action: str) -> str:
    return _checked(f"rv:{review_id}:{action}")


def merge(review_id: int, target: uuid.UUID) -> str:
    return _checked(f"mg:{review_id}:{target}")


def manager(review_id: int, action: str) -> str:
    return _checked(f"mh:{review_id}:{action}")


def source(key: str, action: str) -> str:
    return _checked(f"src:{key}:{action}")


def sources_page(page: int) -> str:
    return _checked(f"src:p:{page}")


def chat_toggle(monitor_id: int) -> str:
    return _checked(f"ch:t:{monitor_id}")


def chats_page(page: int) -> str:
    return _checked(f"ch:p:{page}")


CHATS_NOOP: Final = "ch:noop"


def resume(action: str) -> str:
    return _checked(f"ps:{action}")


def import_plan(plan_id: int, answer: str) -> str:
    return _checked(f"im:{plan_id}:{answer}")


def purge_plan(purge_log_id: int, answer: str) -> str:
    return _checked(f"pg:{purge_log_id}:{answer}")


def dnc_plan(plan_id: int, answer: str) -> str:
    return _checked(f"dn:{plan_id}:{answer}")


def push_check(push_log_id: int, answer: str) -> str:
    return _checked(f"pt:{push_log_id}:{answer}")


def _checked(data: str) -> str:
    """A builder never hands out a payload its own parser refuses."""
    if len(data.encode()) > CALLBACK_DATA_MAX or parse(data) is None:
        raise ValueError(f"callback_data outside the grammar: {data!r}")
    return data


__all__ = [
    "CALLBACK_DATA_MAX",
    "CHATS_NOOP",
    "MANAGER_ANSWERS",
    "REVIEW_ANSWERS",
    "REVIEW_MENUS",
    "Callback",
    "Kind",
    "chat_toggle",
    "chats_page",
    "dnc_plan",
    "import_plan",
    "manager",
    "merge",
    "parse",
    "purge_plan",
    "push_check",
    "resume",
    "review",
    "source",
    "sources_page",
]
