"""Every button the bot draws has a parser and a handler (07 §1.10, 06 §1.5).

An unparsed ``callback_data`` is answered by nobody: the button spins for
~15 s with no error anywhere (fullprompt #937). So the fence is derived:
build every keyboard of ``bot/keyboards.KEYBOARD_SAMPLES`` with synthetic
ids, collect every ``callback_data`` and require that ``bot/callbacks.parse``
returns a typed ``Callback`` (never None) for each, that each fits
Telegram's 64 bytes, that every ``(kind, action)`` the parser can return has
a handler in ``HANDLERS`` and every handler is reachable from some button;
and that garbage, a wrong prefix, a non-integer id or an unknown answer
parse to None.

The parser, the keyboards and the handlers arrive with T-20. Until then the
tree is held to its only safe state — no button, no ``callback_data``
anywhere — and the fence itself is proven on a stand-in built from 06 §1.5's
grammar, so it is not vacuous the day the real modules land.
"""

from __future__ import annotations

import ast
import importlib
import random
import re
import string
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from tests.ast_tools import PACKAGE, modules, owners, rel, tree_of

CALLBACKS = PACKAGE / "bot" / "callbacks.py"
KEYBOARDS = PACKAGE / "bot" / "keyboards.py"
CALLBACK_DATA_MAX = 64
# Where 06 §1.5's HANDLERS dict (the one the routers are built from) may live.
HANDLER_HOMES = (
    "clientscrapper.bot.callbacks",
    "clientscrapper.bot.handlers",
    "clientscrapper.bot.main",
)
UUID = "0f0e0d0c-0b0a-4908-8706-050403020100"
NEAR_MISSES = (
    "rv:1:P",  # an answer letter in the wrong case
    "rv::p",  # no id
    "rv:a:p",  # a non-integer id
    "rv:1:z",  # an unknown answer
    "rv:1:pp",
    "rv:1:p:",
    " rv:1:p",
    "rv:1:p\n",  # `$` matches before a trailing newline: parse must fullmatch
    "rv:12345678901234567890:p",  # 20 digits, past a bigint
    "xx:1:p",  # a wrong prefix
    "mg:1:not-a-uuid",
    "mg:1:" + UUID.upper(),
    "mh:1:p",
    "src:p:x",
    "src:Ab:t",
    "ch:t:",
    "ps:yes",
    "im:1:maybe",
    "rv:\uff11:p",  # a fullwidth digit: \\d would take it, [0-9] does not
    "rv:\u0661\u0662:p",  # Arabic-Indic digits
    "pt:\u0663:y",
    "ch:t:\uff19",
    "ch:t:2147483648",  # past chat_monitors.id's integer
    "",
    ":",
)


def _random_garbage(n: int = 200) -> list[str]:
    rng = random.Random(937)
    alphabet = string.ascii_letters + string.digits + string.punctuation + " "
    return ["".join(rng.choices(alphabet, k=rng.randint(1, 64))) for _ in range(n)]


@dataclass(frozen=True, slots=True)
class Button:
    """The two fields of aiogram's InlineKeyboardButton the fence reads
    (importing aiogram costs ~5 s, too much for a pre-push guard)."""

    text: str
    callback_data: str | None = None


@dataclass(frozen=True, slots=True)
class Markup:
    inline_keyboard: list[list[Button]]


def callback_data_of(markup: Any) -> list[str]:
    """Every callback_data of an InlineKeyboardMarkup (or anything shaped
    like one)."""
    return [
        b.callback_data
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data is not None
    ]


def fence_problems(
    parse: Callable[[str], Any],
    samples: list[tuple[Callable[..., Any], dict[str, Any]]],
    handlers: Mapping[str, Mapping[str | None, Any]],
) -> list[str]:
    """What 06 §1.5 (a), (b) and (c) refuse; empty when the fence holds."""
    found: list[str] = []
    datas = [d for build, kwargs in samples for d in callback_data_of(build(**kwargs))]
    if not datas:
        found.append("the samples build no button")
    produced: set[tuple[str, str | None]] = set()
    for data in datas:
        if not data.isascii() or len(data.encode()) > CALLBACK_DATA_MAX:
            found.append(f"{data!r}: not ASCII within {CALLBACK_DATA_MAX} bytes")
        parsed = parse(data)
        if parsed is None:
            found.append(f"{data!r}: unparsed — nobody answers this button")
            continue
        produced.add((parsed.kind, parsed.action))
    handled = {(kind, action) for kind, actions in handlers.items() for action in actions}
    found += [
        f"{key}: built and parsed, but no handler"
        for key in sorted(produced - handled, key=str)
    ]
    found += [
        f"{key}: a handler no button reaches"
        for key in sorted(handled - produced, key=str)
    ]
    for junk in (*NEAR_MISSES, *_random_garbage()):
        if parse(junk) is not None:
            found.append(f"{junk!r}: garbage parsed")
    return found


# --- the tree ---------------------------------------------------------------------------


def _builds_buttons(tree: ast.Module) -> list[str]:
    """Where a module makes an inline button or names callback_data."""
    owner = owners(tree)
    hits = []
    for node in ast.walk(tree):
        named = (
            isinstance(node, ast.Name)
            and node.id in {"InlineKeyboardButton", "InlineKeyboardBuilder"}
        ) or (
            isinstance(node, ast.Attribute)
            and node.attr in {"InlineKeyboardButton", "button"}
        )
        keyword = isinstance(node, ast.keyword) and node.arg == "callback_data"
        if named or keyword:
            function = owner.get(id(node))
            hits.append(function.name if function else "<module>")
    return hits


def test_no_button_exists_before_its_parser():
    if CALLBACKS.exists():
        pytest.skip("bot/callbacks.py exists: the fence below runs on it")
    assert not KEYBOARDS.exists(), "bot/keyboards.py without bot/callbacks.py (#937)"
    building = {rel(p): hits for p in modules() if (hits := _builds_buttons(tree_of(p)))}
    assert building == {}, "a callback_data with no parser is a button nobody answers"


def _real_fence() -> tuple[Callable[[str], Any], list[Any], Mapping[str, Any]]:
    callbacks = importlib.import_module("clientscrapper.bot.callbacks")
    keyboards = importlib.import_module("clientscrapper.bot.keyboards")
    for home in HANDLER_HOMES:
        handlers = getattr(importlib.import_module(home), "HANDLERS", None)
        if handlers is not None:
            return callbacks.parse, list(keyboards.KEYBOARD_SAMPLES), handlers
    pytest.fail(f"no HANDLERS dict in {HANDLER_HOMES} (06 §1.5)")


def test_every_button_parses_fits_and_has_a_handler():
    if not CALLBACKS.exists():
        pytest.skip("bot/callbacks.py and bot/keyboards.py arrive with T-20 (06 §1.5)")
    assert fence_problems(*_real_fence()) == []


# --- the fence, on a stand-in built from 06 §1.5 ------------------------------------


@dataclass(frozen=True, slots=True)
class _Callback:
    kind: str
    action: str | None = None
    ref: int | None = None
    target: str | None = None
    page: int | None = None


# 06 §1.5's patterns, read as the ASCII it says the payloads are (``\d``
# alone also takes «１», a fullwidth digit).
_GRAMMAR: dict[str, re.Pattern[str]] = {
    "rv": re.compile(r"rv:(?P<ref>\d{1,19}):(?P<action>[pdlxmbcehok])", re.ASCII),
    "mg": re.compile(
        r"mg:(?P<ref>\d{1,19}):(?P<target>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        re.ASCII,
    ),
    "mh": re.compile(r"mh:(?P<ref>\d{1,19}):(?P<action>[cnxwl])", re.ASCII),
    "src": re.compile(
        r"src:(?P<target>[a-z][a-z0-9_]{2,40}):(?P<action>[tia])", re.ASCII
    ),
    "srcp": re.compile(r"src:p:(?P<page>\d{1,3})", re.ASCII),
    "ch": re.compile(r"ch:t:(?P<ref>\d{1,10})", re.ASCII),
    "chp": re.compile(r"ch:p:(?P<page>\d{1,3})", re.ASCII),
    "chnoop": re.compile(r"ch:noop", re.ASCII),
    "ps": re.compile(r"ps:(?P<action>[ync])", re.ASCII),
    "im": re.compile(r"im:(?P<ref>\d{1,10}):(?P<action>[yn])", re.ASCII),
    "pg": re.compile(r"pg:(?P<ref>\d{1,19}):(?P<action>[yn])", re.ASCII),
    "dn": re.compile(r"dn:(?P<ref>\d{1,10}):(?P<action>[yn])", re.ASCII),
    "pt": re.compile(r"pt:(?P<ref>\d{1,19}):(?P<action>[yn])", re.ASCII),
}


def _stand_in_parse(data: str) -> _Callback | None:
    for kind, pattern in _GRAMMAR.items():
        if m := pattern.fullmatch(data):
            g = m.groupdict()
            ref, page = g.get("ref"), g.get("page")
            if ref is not None and int(ref) > (2**31 - 1 if kind == "ch" else 2**63 - 1):
                return None
            return _Callback(
                kind,
                g.get("action"),
                int(ref) if ref is not None else None,
                g.get("target"),
                int(page) if page is not None else None,
            )
    return None


def _markup(*datas: str) -> Markup:
    return Markup(inline_keyboard=[[Button(text=d[:10], callback_data=d)] for d in datas])


def _review_keyboard(review_id: int) -> Markup:
    return _markup(*(f"rv:{review_id}:{a}" for a in "pdlxmbcehok"))


def _merge_keyboard(review_id: int, target: str) -> Markup:
    return _markup(f"mg:{review_id}:{target}", f"rv:{review_id}:k")


def _sources_keyboard(key: str, page: int) -> Markup:
    return _markup(f"src:{key}:t", f"src:{key}:i", f"src:{key}:a", f"src:p:{page}")


STAND_IN_SAMPLES: list[tuple[Callable[..., Markup], dict[str, Any]]] = [
    (_review_keyboard, {"review_id": 9_223_372_036_854_775_807}),
    (_merge_keyboard, {"review_id": 9_223_372_036_854_775_807, "target": UUID}),
    # The longest legal source key: 41 characters.
    (_sources_keyboard, {"key": "d" + "x" * 40, "page": 999}),
]
STAND_IN_HANDLERS: dict[str, dict[str | None, str]] = {
    "rv": {a: f"review_{a}" for a in "pdlxmbcehok"},
    "mg": {None: "merge"},
    "src": {a: f"source_{a}" for a in "tia"},
    "srcp": {None: "sources_page"},
}


def test_the_fence_holds_on_a_stand_in_of_the_spec_grammar():
    assert fence_problems(_stand_in_parse, STAND_IN_SAMPLES, STAND_IN_HANDLERS) == []
    assert len(f"mg:{2**63 - 1}:{UUID}".encode()) <= CALLBACK_DATA_MAX


def test_the_fence_catches_each_failure():
    def problems(
        parse=_stand_in_parse, samples=STAND_IN_SAMPLES, handlers=STAND_IN_HANDLERS
    ):
        return fence_problems(parse, samples, handlers)

    # A button the parser does not know (#937).
    unknown = [*STAND_IN_SAMPLES, (lambda: _markup("rv:1:q"), {})]
    assert problems(samples=unknown) == [
        "'rv:1:q': unparsed — nobody answers this button"
    ]
    # A key over 64 bytes.
    long_key = [(_sources_keyboard, {"key": "a" + "b" * 63, "page": 1})]
    found = problems(
        samples=long_key, handlers={"src": {"t": 1, "i": 1, "a": 1}, "srcp": {None: 1}}
    )
    assert found[0].endswith(f"not ASCII within {CALLBACK_DATA_MAX} bytes")
    # A built kind with no handler, and a handler no button reaches.
    no_merge = {k: v for k, v in STAND_IN_HANDLERS.items() if k != "mg"}
    assert problems(handlers=no_merge) == [
        "('mg', None): built and parsed, but no handler"
    ]
    dead = {**STAND_IN_HANDLERS, "ps": {"y": "resume"}}
    assert problems(handlers=dead) == ["('ps', 'y'): a handler no button reaches"]
    # A parser that guesses: `match` with `$` takes a trailing newline.
    lenient = {k: re.compile("^" + p.pattern + "$") for k, p in _GRAMMAR.items()}

    def guessing(data: str) -> _Callback | None:
        for kind, pattern in lenient.items():
            if pattern.match(data):
                return _Callback(kind)
        return None

    found = problems(parse=guessing)
    assert "'rv:1:p\\n': garbage parsed" in found
    # A parser that answers everything.
    assert any(
        f.endswith("garbage parsed")
        for f in problems(parse=lambda d: _Callback("rv", "p"))
    )


def test_the_button_detector_sees_each_shape():
    source = (
        "from aiogram.types import InlineKeyboardButton\n"
        "def review(rid):\n"
        "    return InlineKeyboardButton(text='✅', callback_data=f'rv:{rid}:p')\n"
        "def builder(kb):\n"
        "    kb.button(text='x', callback_data='ps:y')\n"
    )
    assert sorted(_builds_buttons(ast.parse(source))) == [
        "builder",
        "builder",
        "review",
        "review",
    ]
    assert _builds_buttons(ast.parse("x = {'callback': 1}\n")) == []
    assert uuid.UUID(UUID).version == 4
