# VENDORED_FROM dailyJournal@6b1f67f miya/bot/keyboards.py
"""Inline keyboards of the owner bot (06 §1.3, §1.7, §1.2).

Every ``callback_data`` is made by a builder of ``bot/callbacks.py`` — the
grammar's one home — and never spelt here. ``KEYBOARD_SAMPLES`` builds each
keyboard with synthetic arguments (the longest legal source key, a 19-digit
review id, a uuid target): ``tests/test_bot_callbacks.py`` demands that each
button parses, fits 64 bytes and reaches a handler, and ``test_uz_text`` that
each label is Uzbek Latin within 40 characters.

The builders take plain values, not rows: what a button needs is the id it
carries and the word it shows.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from clientscrapper.bot import callbacks as cb
from clientscrapper.services.cards import CardLayout

PAGE_SIZE: Final = 8
# A label cut to fit a 360 px phone (MIYA's TITLE_WIDTH).
TITLE_WIDTH: Final = 22
MERGE_WIDTH: Final = 30

CHAT_ICON: Final = {"group": "👥", "channel": "📢", "private": "👤"}
SOURCE_ICON: Final = {"on": "✅", "paused": "⏸", "off": "⛔"}


def short(text: str | None, width: int = TITLE_WIDTH) -> str:
    """A label within ``width`` characters, cut with «…»."""
    value = " ".join((text or "").split()) or "—"
    return value if len(value) <= width else value[: width - 1] + "…"


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- the review card (06 §1.3) and the manager card (06 §1.7) -------------------------


def review_keyboard(
    review_id: int, layout: CardLayout = "review", *, expanded: bool = False
) -> InlineKeyboardMarkup:
    """Row 1 ✅/✖️ (the handoff variant: 👤 Menejerga/✖️), row 2 🔀/🔒/⏭,
    row 3 ⋯ Boshqa — which opens 🙍 B2C, 🚚 Kargo/raqobatchi, ✏️ Telefon and
    ◀️ Orqaga in its place."""
    if layout == "manager":
        return manager_keyboard(review_id)
    first = (
        _button("👤 Menejerga", cb.review(review_id, "h"))
        if layout == "handoff"
        else _button("✅ CRMʼga yubor", cb.review(review_id, "p"))
    )
    rows = [
        [first, _button("✖️ Yoʻq", cb.review(review_id, "d"))],
        [
            _button("🔀 Birlashtir", cb.review(review_id, "m")),
            _button("🔒 DNC", cb.review(review_id, "x")),
            _button("⏭ Keyinroq", cb.review(review_id, "l")),
        ],
    ]
    if expanded:
        rows += [
            [
                _button("🙍 B2C", cb.review(review_id, "b")),
                _button("🚚 Kargo/raqobatchi", cb.review(review_id, "c")),
            ],
            [
                _button("✏️ Telefon", cb.review(review_id, "e")),
                _button("◀️ Orqaga", cb.review(review_id, "k")),
            ],
        ]
    else:
        rows.append([_button("⋯ Boshqa", cb.review(review_id, "o"))])
    return _markup(rows)


def merge_keyboard(
    review_id: int, targets: Sequence[tuple[uuid.UUID, str]]
) -> InlineKeyboardMarkup:
    """🔀's menu: one row per target (≤ 5, the resolver's own suggestion),
    then ◀️ back to the card."""
    rows = [
        [_button(f"🔀 {short(name, MERGE_WIDTH)}", cb.merge(review_id, target))]
        for target, name in targets
    ]
    rows.append([_button("◀️ Orqaga", cb.review(review_id, "k"))])
    return _markup(rows)


def manager_keyboard(review_id: int) -> InlineKeyboardMarkup:
    return _markup(
        [
            [
                _button("📞 Bogʻlandim", cb.manager(review_id, "c")),
                _button("🙅 Yozmang dedi", cb.manager(review_id, "x")),
            ],
            [
                _button("❌ Notoʻgʻri / B2C", cb.manager(review_id, "w")),
                _button("⏭ Keyinroq", cb.manager(review_id, "l")),
            ],
        ]
    )


# --- /manbalar --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceButton:
    key: str
    title: str
    # ``SourceLine.state(now)``: on · paused · off.
    state: str


def pages_of(total: int, size: int = PAGE_SIZE) -> int:
    return max(1, -(-total // size))


def sources_keyboard(
    rows: Sequence[SourceButton], *, page: int, pages: int
) -> InlineKeyboardMarkup:
    """One row per source: the on/off switch with its title, and ℹ️; then
    the pages (the middle button re-reads the page it shows)."""
    keyboard = [
        [
            _button(
                f"{SOURCE_ICON.get(row.state, '⛔')} {short(row.title)}",
                cb.source(row.key, "t"),
            ),
            _button("ℹ️", cb.source(row.key, "i")),
        ]
        for row in rows
    ]
    if pages > 1:
        keyboard.append(
            [
                _button("◀️", cb.sources_page((page - 1) % pages)),
                _button(f"{page + 1}/{pages}", cb.sources_page(page)),
                _button("▶️", cb.sources_page((page + 1) % pages)),
            ]
        )
    else:
        keyboard.append([_button("🔄 Yangilash", cb.sources_page(page))])
    return _markup(keyboard)


def source_info_keyboard(
    key: str, *, page: int, can_forbid: bool
) -> InlineKeyboardMarkup:
    """``src:<key>:i``'s view: forbid auto-push (while it is not forbidden —
    the owner forbids, never grants) and back to the list's page."""
    rows = []
    if can_forbid:
        rows.append([_button("🚫 Avto-pushni taqiqlash", cb.source(key, "a"))])
    rows.append([_button("◀️ Orqaga", cb.sources_page(page))])
    return _markup(rows)


# --- /chats (MIYA's chats_keyboard, one toggle) ---------------------------------------


@dataclass(frozen=True, slots=True)
class ChatButton:
    monitor_id: int
    title: str
    chat_type: str
    enabled: bool


def chats_keyboard(
    rows: Sequence[ChatButton], *, page: int, pages: int
) -> InlineKeyboardMarkup:
    keyboard = [
        [
            _button(
                f"{'✅' if row.enabled else '❌'} "
                f"{CHAT_ICON.get(row.chat_type, '💬')} {short(row.title)}",
                cb.chat_toggle(row.monitor_id),
            )
        ]
        for row in rows
    ]
    if pages > 1:
        keyboard.append(
            [
                _button("◀️", cb.chats_page((page - 1) % pages)),
                _button(f"{page + 1}/{pages}", cb.CHATS_NOOP),
                _button("▶️", cb.chats_page((page + 1) % pages)),
            ]
        )
    return _markup(keyboard)


# --- confirms ---------------------------------------------------------------------------


def resume_keyboard(*, crm: bool = False) -> InlineKeyboardMarkup:
    """``/yoq``: resume (ps:y) or keep paused (ps:n); ``/yoq crm``: clear
    the CRM breaker (ps:c) or leave it (ps:n)."""
    yes = (
        _button("✅ Ha, toʻsiqni ol", cb.resume("c"))
        if crm
        else _button("▶️ Ha, davom et", cb.resume("y"))
    )
    return _markup([[yes, _button("✖️ Yoʻq", cb.resume("n"))]])


def _confirm(
    build: Callable[[int, str], str], ref: int, yes: str
) -> InlineKeyboardMarkup:
    return _markup([[_button(yes, build(ref, "y")), _button("✖️ Bekor", build(ref, "n"))]])


def import_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return _confirm(cb.import_plan, plan_id, "✅ Yoz")


def dnc_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return _confirm(cb.dnc_plan, plan_id, "🔒 Yoz")


def push_check_keyboard(push_log_id: int) -> InlineKeyboardMarkup:
    return _confirm(cb.push_check, push_log_id, "🔁 Qayta yubor")


# --- the fence's samples (06 §1.5) ------------------------------------------------------

_BIG: Final = 2**63 - 1
_PLAN: Final = 9_999_999_999
_UUID: Final = uuid.UUID("0f0e0d0c-0b0a-4908-8706-050403020100")
# The longest legal ``sources.key``: 41 characters.
_LONG_KEY: Final = "d" + "x" * 40

KEYBOARD_SAMPLES: Final[
    list[tuple[Callable[..., InlineKeyboardMarkup], dict[str, Any]]]
] = [
    (review_keyboard, {"review_id": _BIG}),
    (review_keyboard, {"review_id": _BIG, "expanded": True}),
    (review_keyboard, {"review_id": _BIG, "layout": "handoff"}),
    (review_keyboard, {"review_id": _BIG, "layout": "handoff", "expanded": True}),
    (merge_keyboard, {"review_id": _BIG, "targets": [(_UUID, "Optom " * 12)]}),
    (manager_keyboard, {"review_id": _BIG}),
    (
        sources_keyboard,
        {
            "rows": [SourceButton(_LONG_KEY, "Qidiruv tizimlari " * 3, "paused")],
            "page": 998,
            "pages": 999,
        },
    ),
    (sources_keyboard, {"rows": [], "page": 0, "pages": 1}),
    (source_info_keyboard, {"key": _LONG_KEY, "page": 998, "can_forbid": True}),
    (
        chats_keyboard,
        {
            "rows": [ChatButton(9_999_999_999, "Optom bozor " * 4, "group", True)],
            "page": 998,
            "pages": 999,
        },
    ),
    (resume_keyboard, {}),
    (resume_keyboard, {"crm": True}),
    (import_keyboard, {"plan_id": _PLAN}),
    (dnc_keyboard, {"plan_id": _PLAN}),
    (push_check_keyboard, {"push_log_id": _BIG}),
]


__all__ = [
    "KEYBOARD_SAMPLES",
    "PAGE_SIZE",
    "ChatButton",
    "SourceButton",
    "chats_keyboard",
    "dnc_keyboard",
    "import_keyboard",
    "manager_keyboard",
    "merge_keyboard",
    "pages_of",
    "push_check_keyboard",
    "resume_keyboard",
    "review_keyboard",
    "short",
    "source_info_keyboard",
    "sources_keyboard",
]
