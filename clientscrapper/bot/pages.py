"""The bot's list pages that are not cards (06 §1.2): ``/manbalar`` and a
source's ℹ️, ``/chats``, ``/lidlar holat``, ``/vazn``'s list, ``/dnc``'s
preview. Pure: plain values in, HTML text out; every value from the database
is escaped here, the words are ``bot/texts``'.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from clientscrapper.bot.keyboards import CHAT_ICON, SOURCE_ICON
from clientscrapper.bot.texts import t
from clientscrapper.config import settings
from clientscrapper.db.enums import CANDIDATE_STATES
from clientscrapper.services import cards, health
from clientscrapper.services.cards import LidlarHolat
from clientscrapper.services.owner import SourceInfo, SourceLine
from clientscrapper.services.scoring import WeightSet

esc = health.escape
# /chats' signal window (06 §1.2 «{hit_7d} signal»).
HITS_WINDOW: Final = timedelta(days=7)


def age(moment: datetime | None, now: datetime) -> str:
    return health.age_label(now - moment) if moment is not None else t("manbalar.never")


def at(moment: datetime) -> str:
    return f"{moment.astimezone(settings.tz):%d.%m %H:%M}"


def precision(value: Decimal | None) -> str:
    return f"{int(value * 100)}%" if value is not None else "—"


# --- /manbalar ------------------------------------------------------------------------


def source_line(line: SourceLine, now: datetime) -> str:
    """06 §1.2: ``{✅/⏸/⛔} {title} · oxirgi {age} · bugun {pages}/{budget}
    sahifa · ${cost} · aniqlik {p}% ({pushes_30d}) · {avto/qoʻlda}``."""
    return t(
        "manbalar.line",
        icon=SOURCE_ICON[line.state(now)],
        title=esc(line.title),
        age=age(line.last_run_at, now),
        pages=line.pages_today,
        budget=line.page_budget,
        cost=cards.money(line.cost_today),
        precision=precision(line.precision),
        pushes=line.pushes_30d,
        mode=t("manbalar.mode_auto" if line.auto_push else "manbalar.mode_manual"),
    )


def sources_text(
    lines: Sequence[SourceLine], *, page: int, pages: int, now: datetime
) -> str:
    if not lines:
        return t("manbalar.empty")
    head = t("manbalar.title", page=page + 1, pages=pages)
    return "\n".join([head, *(source_line(line, now) for line in lines)])


def source_info_text(info: SourceInfo, now: datetime) -> str:
    line = info.line
    out = [t("manbalar.info_title", title=esc(line.title), key=esc(line.key))]
    out.append(source_line(line, now))
    if line.breaker_until is not None and line.breaker_until > now:
        out.append(
            t(
                "manbalar.info_breaker",
                until=at(line.breaker_until),
                reason=esc(info.breaker_reason or "—"),
            )
        )
    if line.paused_until is not None and line.paused_until > now:
        out.append(
            t(
                "manbalar.info_paused",
                until=at(line.paused_until),
                reason=esc(info.paused_reason or "—"),
            )
        )
    state = (
        "manbalar.auto_forbidden"
        if line.forbidden
        else "manbalar.auto_on"
        if line.auto_push
        else "manbalar.auto_off"
    )
    out.append(t("manbalar.info_auto", state=t(state)))
    if info.runs:
        out.append(t("manbalar.info_runs"))
        out += [
            t(
                "manbalar.info_run",
                at=at(run.started_at),
                status=esc(run.status),
                pages=run.pages,
                new=run.new_companies,
                error=f" · {esc(run.error)}" if run.error else "",
            )
            for run in info.runs
        ]
    else:
        out.append(t("manbalar.info_no_runs"))
    return "\n".join(out)


# --- /chats -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChatLine:
    monitor_id: int
    title: str
    chat_type: str
    enabled: bool
    hits: int
    seen: int


def chats_text(rows: Sequence[ChatLine], *, page: int, pages: int, total: int) -> str:
    if not rows:
        return t("chats.empty")
    head = t("chats.title", page=page + 1, pages=pages, total=total)
    return "\n".join(
        [
            head,
            *(
                t(
                    "chats.line",
                    state="✅" if row.enabled else "❌",
                    icon=CHAT_ICON.get(row.chat_type, "💬"),
                    title=esc(row.title),
                    hits=row.hits,
                    seen=row.seen,
                )
                for row in rows
            ),
        ]
    )


# --- /lidlar holat --------------------------------------------------------------------


def lidlar_holat_text(holat: LidlarHolat, now: datetime) -> str:
    out = [t("lidlar.holat_title")]
    out += [
        t("lidlar.holat_line", state=t(f"state.{state}"), n=holat.counts.get(state, 0))
        for state in CANDIDATE_STATES
    ]
    if holat.review_oldest_at is not None:
        out.append(t("lidlar.holat_oldest", age=age(holat.review_oldest_at, now)))
    else:
        out.append(t("lidlar.holat_no_review"))
    if holat.auto_waiting_total:
        # The auto-approved candidates a push refused: their note's home
        # (lead_candidates.push_note, 04 §7 item 14).
        out.append(t("lidlar.holat_auto", n=holat.auto_waiting_total))
        out += [
            t("lidlar.holat_auto_line", name=esc(w.display_name), note=esc(w.note))
            for w in holat.auto_waiting
        ]
    return "\n".join(out)


# --- /vazn ----------------------------------------------------------------------------


def weights_text(weights: WeightSet) -> str:
    out = [t("vazn.title", version=weights.version)]
    out += [
        t(
            "vazn.line",
            key=esc(key),
            points=w.points,
            group=esc(w.group),
            cap=w.cap,
        )
        for key, w in sorted(weights.rows.items(), key=lambda kv: (kv[1].group, kv[0]))
    ]
    out.append(t("vazn.how"))
    return "\n".join(out)


def points_range(group: str, cap: int) -> str:
    """The range ``scoring.points_allowed`` admits, in the owner's words."""
    if group == "negative":
        return t("vazn.range_negative")
    if group == "gate":
        return t("vazn.range_gate")
    return t("vazn.range_positive", cap=cap)


# --- /dnc -----------------------------------------------------------------------------


def dnc_preview_text(key: str, names: Sequence[str]) -> str:
    companies = (
        "\n".join(t("dnc.company", name=esc(name)) for name in names)
        if names
        else t("dnc.no_company")
    )
    return t("dnc.preview", key=esc(key), companies=companies)


__all__ = [
    "HITS_WINDOW",
    "ChatLine",
    "age",
    "chats_text",
    "dnc_preview_text",
    "lidlar_holat_text",
    "points_range",
    "source_info_text",
    "source_line",
    "sources_text",
    "weights_text",
]
