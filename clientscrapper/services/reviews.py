# VENDORED_FROM dailyJournal@6b1f67f miya/services/approvals.py
"""The review flow: 02 §2.9's review state machine, the card job, the expiry
sweep and the manager handoff (06 §1.3, §1.6, §1.7; 07 §4.1 T-21).

One machine for both ``lead_reviews.kind``\\ s (MIYA ``approvals.py``'s shape
plus ``merged``): ``pending`` → ``asked`` (the card is out, ``expires_at =
asked_at + REVIEW_EXPIRY_HOURS``) → ``approved | declined | merged`` (one tap)
or ``expired`` (the sweep, or a card whose candidate left the state it waits
in). The tap's answer moves the candidate through ``candidates.transition``
(02 §2.9, the one statement of the table):

=============  ==========  ===============================================
answer          row         candidate
=============  ==========  ===============================================
push            approved    review → approved(``owner:<tg_id>``)
decline         declined    review → rejected
later           asked       stays; ``expires_at += 24 h``, asked again in
                            24 h (``remind_at``, 0004) inside the hours
dnc             declined    review → suppressed + ``suppression(dnc)`` rows
not_b2b         declined    review → excluded(b2c) + ``suppression(b2c)``
competitor      declined    review → excluded(competitor), the company flag
merge           merged      review → excluded(merged): beside the target's
                            open candidate (``merge_into``), or moved onto the
                            target by ``merge_into`` and excluded there
edit_phone      asked       stays; the lead contact's digits replaced
handoff         approved    review → hold 30 d + a ``kind='manager'`` row
                            (with nobody yet when no manager is listed)
contacted       approved    stays hold (``/tel`` re-scores it)
said_no         declined    hold → suppressed + ``suppression(dnc)``
wrong_or_b2c    declined    hold → excluded(b2c)
=============  ==========  ===============================================

An owner card waits on a candidate in ``review``, a manager card on one in
``hold``; a tap, the sweep or the card job that finds the candidate moved on
closes the card (``expired``, ``note='stale:<state>'``, its keyboard removed)
and never transitions the candidate (06 §1.3).

**⏭** (06 §1.3): the tap adds 24 hours to ``expires_at`` and sets
``remind_at`` a day ahead; the card is asked again — a fresh message — on the
first run inside the hours from then on, and never has less than a fresh
card's ``REVIEW_EXPIRY_HOURS`` after that resend. A card put off and not yet
asked again is never expired by the sweep (its old message has no buttons:
nobody could answer it). An owner resend counts against ``REVIEW_DAILY_CAP``
(``reminder_log(kind='review_resent', ref=review_id)``, ``asked_today``).

**Expiry** (02 §2.9): an owner card nobody answered puts its candidate in
``hold`` for 7 days, ``reminder_log(kind='review_expired',
ref=candidate_id)`` counting the silences; the score job brings a hot hold
back to ``review`` when the hold runs out, and this module asks again — at
most twice: the third silence holds the candidate with no timer, so only a
new signal (``score_job``'s stale mark) brings it back. A manager card nobody
answered leaves the candidate in its handoff hold (the digest says
«Menejer javob bermadi»).

**Locks.** A tap takes the company, then the candidate
(``push_gate.lock_for_push``: companies before lead_candidates, 04 §0), then
claims its review row with ``UPDATE … WHERE state='asked'`` — the row count
decides, so a second tap of the same button answers «Bu karta eskirgan»
(06 §1.3). ``record_refusals`` writes a review row after the candidate, so
the review row is always the last of the three. The job's edits of a live
card (``refresh_live``, the ⏭ resend) hold the card's row (``FOR UPDATE …
SKIP LOCKED``) across the Telegram call, so a tap landing meanwhile waits for
that edit and its own edit comes last; they take no company or candidate
lock while they hold it (a card that cannot be shown is set aside after the
row is let go). The sweep locks its pairs in company order (the order
``merge_into`` locks two companies in).

**The card job** (``review_cards_job``, every 2 minutes, 06 §1.6): the
sweep, the stale cards, a ``pending`` owner row for every ``review``
candidate that has none, then — not while ``/toxtat`` holds, and only when a
bot is wired — the keyboards of the closed cards (every closed card whose
message may still show buttons, ``card_message_id`` cleared once stripped, so
a card closed while paused loses them after ``/yoq``), the ✅ cards whose push
just finished (their footer follows it), the live cards whose content changed
(``card_hash``: edited only on a change; the newest and a rotating window of
the rest each run), and inside ``REVIEW_HOURS`` (Mon–Sat, never in
``QUIET_HOURS``) the sending: the cards put off with ⏭, then the next
``pending`` owner cards (``send_owner_cards``, ``/lidlar``'s own loop) within
``REVIEW_BURST_MAX`` per run and ``REVIEW_DAILY_CAP`` owner cards per
Tashkent day, then the manager cards waiting (the owner's handoff card then
says who received it). A recipient Telegram refused is skipped for the rest
of the run; the others still get theirs. Every card is rendered with the
gates evaluated at that moment (``cards.review_view``, 04 §6); a card that
cannot be shown leaves the queue (``cards.set_aside``).

The cards go out through a ``CardSender`` (``bot/cards_out.BotCards`` over
``bot/deliver.to_person``: the owner and the allow-listed managers only).
Nothing here imports aiogram.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings, within
from clientscrapper.db.models import (
    Company,
    Contact,
    LeadCandidate,
    LeadReview,
    PushLog,
    ReminderLog,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import (
    candidates,
    cards,
    companies,
    costs,
    crm_push,
    gates,
    health,
    normalise,
    owner,
    push_gate,
    scoring,
    signals,
    suppression,
)
from clientscrapper.services.cards import CardLayout, CardRefused

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# 06 §1.6: REVIEW_HOURS «09:00-18:00 Mon–Sat» (ISO weekdays).
REVIEW_DAYS: Final = frozenset(range(1, 7))
# 02 §2.9: an expired owner card holds its candidate 7 days, re-asked at most
# twice; a handoff holds it 30 days; ⏭ adds 24 hours.
EXPIRED_HOLD: Final = timedelta(days=7)
MAX_REASKS: Final = 2
HANDOFF_HOLD: Final = timedelta(days=30)
LATER: Final = timedelta(hours=24)
# 06 §1.3 ✏️: «the owner's next message within 5 min must be a phone».
EDIT_PHONE_WINDOW: Final = timedelta(minutes=5)
EXPIRED_REMINDER: Final = "review_expired"
# An owner card put off with ⏭ and asked again (ref = the review id): the
# resend is a card the owner sees, so REVIEW_DAILY_CAP counts it.
RESENT_REMINDER: Final = "review_resent"
# suppression.reason of the card's own writes (the source is the owner bot's).
OWNER_CARD_REASON: Final = "owner_card"
MANAGER_REASON: Final = "manager_report"
# 06 §1.7: a manager's /tel phone is a pre-contract request.
TEL_BASIS: Final = "pre_contract_request"
# A card closed because its candidate left the state it waits in.
STALE_NOTE: Final = "stale:"
# A manager card that could not be shown at all.
REFUSED_NOTE: Final = "refused:"
# Live cards re-rendered per run — the newest half of them, then a window of
# the rest that moves on every run (REFRESH_SLOT, the job's cadence), so an
# older card is re-judged too; edited only on a change.
REFRESH_MAX: Final = 20
REFRESH_SLOT: Final = timedelta(minutes=2)
LIVE_SCAN_MAX: Final = 1000
# A closed card's buttons are taken off its message while it is this young
# (a message Telegram no longer lets the bot edit is not retried for ever).
STRIP_WINDOW: Final = timedelta(days=14)
# A ✅ card whose push finished this recently gets the push's footer (the job
# runs every 2 minutes: a few chances, and an edit only when the text moved).
PUSH_FOOTER_WINDOW: Final = timedelta(minutes=15)
SWEEP_LIMIT: Final = 200

# The candidate state a live card waits on (06 §1.3/§1.7).
WAITING_STATE: Final[Mapping[str, str]] = {"owner": "review", "manager": "hold"}
OWNER_ANSWERS: Final = frozenset(
    {
        "push",
        "decline",
        "later",
        "dnc",
        "not_b2b",
        "competitor",
        "merge",
        "edit_phone",
        "handoff",
    }
)
MANAGER_ANSWERS: Final = frozenset(
    {"contacted", "said_no", "wrong_or_b2c", "no_reply", "later"}
)
# 02 §2.9: the review row's state after each answer ('asked': it stays).
ROW_STATE: Final[Mapping[str, str]] = {
    "push": "approved",
    "handoff": "approved",
    "contacted": "approved",
    "decline": "declined",
    "dnc": "declined",
    "not_b2b": "declined",
    "competitor": "declined",
    "said_no": "declined",
    "wrong_or_b2c": "declined",
    "no_reply": "declined",
    "merge": "merged",
    "later": "asked",
    "edit_phone": "asked",
}


class Refused(Exception):
    """A tap or a command that changes nothing (the caller rolls back):
    ``wrong_card``, ``not_yours``, ``no_phone`` (✅ without a usable phone),
    ``no_target``, ``same_company``, ``merge:<reason>``,
    ``bad_phone``, ``same_phone``, ``phone_taken``, ``no_card``, ``stale``,
    ``not_waiting``, ``no_source``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Tapped:
    """What a tap did: ``answer`` (its code), or ``stale`` when the card no
    longer waited — closed now if it was still open. ``footer`` is the line
    under the card; a handoff names the manager card it made."""

    review_id: int
    answer: str
    footer: str | None = None
    manager_review_id: int | None = None
    # The candidate the card asked about (``merge`` may move it).
    candidate_id: uuid.UUID | None = None

    @property
    def stale(self) -> bool:
        return self.answer == "stale"


@dataclass(frozen=True, slots=True)
class SentCard:
    """A card Telegram took: where it is, and the recipient's first name
    (a manager's, for the handoff footer)."""

    chat_id: int
    message_id: int
    name: str | None = None


class CardSender(Protocol):
    """The bot's half (``bot/cards_out.BotCards``): sends to the owner and
    the allow-listed managers only (``deliver.to_person``)."""

    async def send(
        self, telegram_id: int, text: str, layout: CardLayout, review_id: int
    ) -> SentCard | None: ...

    async def edit(
        self,
        telegram_id: int,
        message_id: int,
        text: str,
        layout: CardLayout | None,
        review_id: int,
    ) -> bool: ...

    async def strip(self, telegram_id: int, message_id: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class Closed:
    """A card the sweep or the stale step closed: its message loses its
    buttons (a later tap answers «Bu karta eskirgan»)."""

    review_id: int
    chat_id: int | None
    message_id: int | None


# --- when cards may go out (06 §1.6) --------------------------------------------------


def sending_hours(now: datetime) -> bool:
    """Inside ``REVIEW_HOURS`` (Mon–Sat) and outside ``QUIET_HOURS``."""
    local = now.astimezone(settings.tz)
    start, end = settings.review_hours_parsed
    return (
        local.isoweekday() in REVIEW_DAYS
        and within(local.time(), start, end)
        and not health.in_quiet_hours(now)
    )


async def asked_today(session: AsyncSession, now: datetime) -> int:
    """Owner cards shown this Tashkent day (``REVIEW_DAILY_CAP``'s count, 06
    §1.6): the cards first asked today and the ⏭ cards asked again today
    (``RESENT_REMINDER``) — a resend does not move ``asked_at``. A manager's
    card is not the owner's budget."""
    start, end = costs.day_start(now), costs.day_end(now)
    asked = await session.scalar(
        sa.select(sa.func.count())
        .select_from(LeadReview)
        .where(
            LeadReview.kind == "owner",
            LeadReview.asked_at >= start,
            LeadReview.asked_at < end,
        )
    )
    resent = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ReminderLog)
        .where(
            ReminderLog.kind == RESENT_REMINDER,
            ReminderLog.sent_at >= start,
            ReminderLog.sent_at < end,
        )
    )
    return int(asked or 0) + int(resent or 0)


def owner_budget(asked: int) -> int:
    """This run's owner cards: ``REVIEW_BURST_MAX``, within what the day's
    ``REVIEW_DAILY_CAP`` leaves."""
    return max(0, min(settings.review_burst_max, settings.review_daily_cap - asked))


# --- one tap ---------------------------------------------------------------------------


async def _claim(
    session: AsyncSession,
    review: LeadReview,
    code: str,
    *,
    by: int,
    now: datetime,
    only: tuple[sa.ColumnElement[bool], ...] = (),
    values: Mapping[str, Any] | None = None,
) -> bool:
    """The row takes the answer while it is still ``asked`` (and ``only``
    holds): the row count decides (06 §1.3 — a second tap of the same card
    is stale)."""
    state = ROW_STATE[code]
    row: dict[str, Any] = {"answer": code, **(values or {})}
    if review.kind == "owner":
        row["by_telegram_id"] = by
    if state != "asked":
        row.update(state=state, answered_at=now, remind_at=None)
    result = await session.execute(
        sa.update(LeadReview)
        .where(LeadReview.id == review.id, LeadReview.state == "asked", *only)
        .values(**row)
        .returning(LeadReview.id)
    )
    return result.first() is not None


async def close_card(
    session: AsyncSession, review_id: int, why: str, *, states: tuple[str, ...]
) -> bool:
    """A live card that can no longer be answered: ``expired`` with the
    reason in ``note`` (``stale:<state>`` / ``refused:<code>``); its
    candidate is not touched."""
    result = await session.execute(
        sa.update(LeadReview)
        .where(LeadReview.id == review_id, LeadReview.state.in_(states))
        .values(state="expired", note=why[: push_gate.REVIEW_NOTE_MAX], remind_at=None)
        .returning(LeadReview.id)
    )
    return result.first() is not None


async def answer(
    session: AsyncSession,
    review_id: int,
    code: str,
    *,
    by: int,
    now: datetime,
    target: uuid.UUID | None = None,
) -> Tapped:
    """Apply one tap of card ``review_id`` in the caller's transaction (the
    caller commits; on ``Refused`` it rolls back). ``by`` is the presser's
    Telegram id; ``target`` the company 🔀 merges into."""
    review = await session.get(LeadReview, review_id, populate_existing=True)
    if review is None or review.state != "asked":
        return Tapped(review_id, "stale")
    allowed = OWNER_ANSWERS if review.kind == "owner" else MANAGER_ANSWERS
    if code not in allowed or code == "edit_phone":
        raise Refused("wrong_card")
    if review.kind == "manager" and review.by_telegram_id != by:
        raise Refused("not_yours")
    if code == "merge":
        return await _merge(session, review, by=by, now=now, target=target)
    candidate = await push_gate.lock_for_push(session, review.candidate_id)
    waiting = WAITING_STATE[review.kind]
    if candidate is None or candidate.state != waiting:
        gone = candidate.state if candidate is not None else "gone"
        await close_card(session, review.id, STALE_NOTE + gone, states=("asked",))
        return Tapped(review_id, "stale")
    handler = _EFFECTS[code]
    footer, made = await handler(session, review, candidate, by, now)
    return Tapped(
        review_id,
        code,
        footer=footer,
        manager_review_id=made,
        candidate_id=candidate.id,
    )


_Effect = Any  # (session, review, candidate, by, now) -> (footer, manager id)


async def _push(
    session: AsyncSession,
    review: LeadReview,
    candidate: LeadCandidate,
    by: int,
    now: datetime,
) -> tuple[str, int | None]:
    lead = await push_gate.lead_contact(session, candidate)
    try:
        await candidates.transition(
            session, candidate, "approved", now=now, approved_by=owner.owner_ref(by)
        )
    except candidates.IllegalTransition:
        raise Refused("no_phone") from None
    # 04 §7 item 4: approving a shared lead phone is the owner's word, stored
    # exactly (push_gate.SHARED_OK, read by owner_word).
    note = push_gate.SHARED_OK if lead is not None and lead.shared_phone else None
    extra = {"note": note} if note is not None else {}
    if not await _claim(session, review, "push", by=by, now=now, values=extra):
        raise Refused("stale")
    return await push_footer(session, candidate, now=now), None


async def _decline(
    session: AsyncSession,
    review: LeadReview,
    candidate: LeadCandidate,
    by: int,
    now: datetime,
) -> tuple[str, int | None]:
    if not await _claim(session, review, "decline", by=by, now=now):
        raise Refused("stale")
    await candidates.transition(session, candidate, "rejected", now=now)
    return cards.footer("decline"), None


async def _later(
    session: AsyncSession,
    review: LeadReview,
    candidate: LeadCandidate,
    by: int,
    now: datetime,
) -> tuple[str, int | None]:
    # A card already put off (and not asked again yet) is stale: a second
    # ⏭ of the same message in flight adds nothing (06 §1.3).
    expires = (review.expires_at or now) + LATER
    if not await _claim(
        session,
        review,
        "later",
        by=by,
        now=now,
        only=(LeadReview.remind_at.is_(None),),
        values={"expires_at": expires, "remind_at": now + LATER},
    ):
        raise Refused("stale")
    return cards.footer("later" if review.kind == "owner" else "manager_later"), None


async def _dnc(
    session: AsyncSession,
    review: LeadReview,
    candidate: LeadCandidate,
    by: int,
    now: datetime,
) -> tuple[str, int | None]:
    code = "dnc" if review.kind == "owner" else "said_no"
    if not await _claim(session, review, code, by=by, now=now):
        raise Refused("stale")
    await suppression.dnc_for_candidate(
        session,
        candidate,
        now=now,
        reason=OWNER_CARD_REASON if review.kind == "owner" else MANAGER_REASON,
    )
    return cards.footer(code), None


async def _not_b2b(
    session: AsyncSession,
    review: LeadReview,
    candidate: LeadCandidate,
    by: int,
    now: datetime,
) -> tuple[str, int | None]:
    if not await _claim(session, review, "not_b2b", by=by, now=now):
        raise Refused("stale")
    await suppression.suppress_company(
        session,
        candidate.company_id,
        "b2c",
        source=suppression.OWNER_SOURCE,
        reason=OWNER_CARD_REASON,
    )
    await candidates.transition(
        session, candidate, "excluded", now=now, exclude_reason="b2c"
    )
    return cards.footer("not_b2b"), None


async def _competitor(
    session: AsyncSession,
    review: LeadReview,
    candidate: LeadCandidate,
    by: int,
    now: datetime,
) -> tuple[str, int | None]:
    if not await _claim(session, review, "competitor", by=by, now=now):
        raise Refused("stale")
    # The flag keeps the company out for good (02 §2.9: re-opened only when
    # the owner clears it); its channel stays a seed (06 §1.3).
    await session.execute(
        sa.update(Company)
        .where(Company.id == candidate.company_id)
        .values(is_competitor=True)
    )
    await suppression.suppress_company(
        session,
        candidate.company_id,
        "competitor",
        source=suppression.OWNER_SOURCE,
        reason=OWNER_CARD_REASON,
    )
    await candidates.transition(
        session, candidate, "excluded", now=now, exclude_reason="competitor"
    )
    return cards.footer("competitor"), None


async def pick_manager(session: AsyncSession, now: datetime) -> int | None:
    """06 §1.7: the manager with the fewest ``kind='manager'`` cards today
    (ties: ``MANAGER_TELEGRAM_IDS`` order); None when none is configured."""
    managers = list(dict.fromkeys(settings.manager_telegram_ids))
    if not managers:
        return None
    rows = await session.execute(
        sa.select(LeadReview.by_telegram_id, sa.func.count())
        .where(
            LeadReview.kind == "manager",
            LeadReview.by_telegram_id.in_(managers),
            LeadReview.created_at >= costs.day_start(now),
            LeadReview.created_at < costs.day_end(now),
        )
        .group_by(LeadReview.by_telegram_id)
    )
    counts = {int(m): int(n) for m, n in rows.all() if m is not None}
    return min(managers, key=lambda m: (counts.get(m, 0), managers.index(m)))


async def _handoff(
    session: AsyncSession,
    review: LeadReview,
    candidate: LeadCandidate,
    by: int,
    now: datetime,
) -> tuple[str, int | None]:
    # 06 §3.4: with no manager listed the prospect waits in hold — its card
    # is made with nobody yet, and the first manager listed receives it.
    manager = await pick_manager(session, now)
    if not await _claim(session, review, "handoff", by=by, now=now):
        raise Refused("stale")
    await candidates.transition(
        session, candidate, "hold", now=now, hold_until=now + HANDOFF_HOLD
    )
    made = await session.scalar(
        pg_insert(LeadReview)
        .values(
            candidate_id=candidate.id,
            kind="manager",
            state="pending",
            by_telegram_id=manager,
            created_at=now,
        )
        .on_conflict_do_nothing()
        .returning(LeadReview.id)
    )
    if made is None:
        # A live manager card of this candidate exists (ux_lead_reviews_live).
        made = await session.scalar(
            sa.select(LeadReview.id).where(
                LeadReview.candidate_id == candidate.id,
                LeadReview.kind == "manager",
                LeadReview.state.in_(("pending", "asked")),
            )
        )
    return handoff_footer(None, configured=manager is not None), made


def handoff_footer(out: SentCard | None, *, configured: bool = True) -> str:
    """The owner's handoff footer (06 §1.3 «👤 Menejerga yuborildi —
    {manager}»): the manager's first name once the card is out (a card out
    to a chat Telegram named nobody for says only that it went); before
    that, that it waits — or that no manager is listed yet. Never an id."""
    if out is not None:
        if out.name:
            return cards.footer("handoff", manager=out.name)
        return cards.footer("handoff_unnamed")
    return cards.footer("handoff_waiting" if configured else "handoff_no_manager")


async def handoff_line(
    session: AsyncSession, manager_review_id: int, out: SentCard | None
) -> str:
    """``handoff_footer`` for the tap's own send: when this send got nothing
    because the card job sent the card meanwhile, it is out — unnamed."""
    if out is None:
        review = await session.get(LeadReview, manager_review_id, populate_existing=True)
        if review is not None and review.state == "asked":
            return cards.footer("handoff_unnamed")
    return handoff_footer(out, configured=bool(settings.manager_telegram_ids))


async def _contacted(
    session: AsyncSession,
    review: LeadReview,
    candidate: LeadCandidate,
    by: int,
    now: datetime,
) -> tuple[str, int | None]:
    if not await _claim(session, review, "contacted", by=by, now=now):
        raise Refused("stale")
    return cards.footer("contacted", review_id=review.id), None


async def _wrong(
    session: AsyncSession,
    review: LeadReview,
    candidate: LeadCandidate,
    by: int,
    now: datetime,
) -> tuple[str, int | None]:
    if not await _claim(session, review, "wrong_or_b2c", by=by, now=now):
        raise Refused("stale")
    await candidates.transition(
        session, candidate, "excluded", now=now, exclude_reason="b2c"
    )
    return cards.footer("wrong_or_b2c"), None


async def _no_reply(
    session: AsyncSession,
    review: LeadReview,
    candidate: LeadCandidate,
    by: int,
    now: datetime,
) -> tuple[str, int | None]:
    if not await _claim(session, review, "no_reply", by=by, now=now):
        raise Refused("stale")
    return cards.footer("no_reply"), None


_EFFECTS: Final[Mapping[str, _Effect]] = {
    "push": _push,
    "decline": _decline,
    "later": _later,
    "dnc": _dnc,
    "said_no": _dnc,
    "not_b2b": _not_b2b,
    "competitor": _competitor,
    "handoff": _handoff,
    "contacted": _contacted,
    "wrong_or_b2c": _wrong,
    "no_reply": _no_reply,
}


async def _merge(
    session: AsyncSession,
    review: LeadReview,
    *,
    by: int,
    now: datetime,
    target: uuid.UUID | None,
) -> Tapped:
    """🔀 into ``target``: the resolver's own ``merge_into`` (it locks both
    companies in id order, then their open candidates — no lock is taken
    before it). A refused merge (two STIRs, …) keeps the card (06 §1.3)."""
    if target is None:
        raise Refused("no_target")
    candidate = await session.get(
        LeadCandidate, review.candidate_id, populate_existing=True
    )
    if candidate is None or candidate.state != "review":
        gone = candidate.state if candidate is not None else "gone"
        await close_card(session, review.id, STALE_NOTE + gone, states=("asked",))
        return Tapped(review.id, "stale")
    source = await session.get(Company, candidate.company_id)
    into = await session.get(Company, target)
    if source is None or into is None:
        raise Refused("no_target")
    source = await companies.root_company(session, source)
    root = await companies.root_company(session, into)
    if root.id == source.id:
        raise Refused("same_company")
    try:
        merged = await companies.merge_into(
            session, source, root, by=owner.owner_ref(by), now=now
        )
    except companies.MergeRefused as exc:
        raise Refused(f"merge:{exc.reason}") from None
    # 06 §1.3 / 02 §2.9: 🔀 → excluded(merged). Beside the target's open
    # candidate merge_into did it; alone it moved the candidate onto the
    # target, which is excluded there (merge_into holds its row lock) — the
    # owner is not asked again about the lead he merged; a newer signal
    # reopens the company (candidates.may_reopen).
    moved = await session.get(LeadCandidate, candidate.id, populate_existing=True)
    if moved is not None and moved.state == "review":
        await candidates.transition(
            session, moved, "excluded", now=now, exclude_reason="merged"
        )
    if not await _claim(session, review, "merge", by=by, now=now):
        raise Refused("stale")
    return Tapped(
        review.id,
        "merge",
        footer=cards.footer("merge", target=merged.display_name),
        candidate_id=candidate.id,
    )


# --- ✏️ and /tel ---------------------------------------------------------------------


def usable_phone(raw: str) -> normalise.Phone | None:
    """An Uzbek mobile or landline as written (04 §1.1), or None."""
    phone = normalise.make_phone(raw.strip())
    if phone is None or phone.country != "UZ":
        return None
    if phone.kind not in scoring.USABLE_PHONE_KINDS:
        return None
    return phone


async def edit_phone(
    session: AsyncSession, review_id: int, raw: str, *, by: int, now: datetime
) -> None:
    """✏️'s second step (06 §1.3): the owner's message is the lead phone,
    corrected on the same contact row — a typo fix, ``legal_basis`` and
    ``source_url`` unchanged; the card stays ``asked`` and its candidate is
    re-scored. Raises ``Refused``."""
    review = await session.get(LeadReview, review_id, populate_existing=True)
    if review is None or review.kind != "owner" or review.state != "asked":
        raise Refused("stale")
    phone = usable_phone(raw)
    if phone is None:
        raise Refused("bad_phone")
    candidate = await push_gate.lock_for_push(session, review.candidate_id)
    if candidate is None or candidate.state != "review":
        raise Refused("stale")
    lead = await push_gate.lead_contact(session, candidate)
    if lead is None or lead.phone is None:
        raise Refused("no_phone")
    if lead.phone == phone.e164:
        raise Refused("same_phone")
    taken = await session.scalar(
        sa.select(
            sa.exists().where(
                Contact.company_id == lead.company_id,
                Contact.id != lead.id,
                Contact.phone_tail == phone.tail,
            )
        )
    )
    if taken:
        raise Refused("phone_taken")
    lead.phone = phone.e164
    lead.phone_kind = phone.kind
    # The flag described the old number; the new one is judged afresh.
    lead.shared_phone = False
    await session.flush()
    await companies.check_shared_phone(session, phone.tail)
    await signals.mark_stale(session, candidate.company_id)
    if not await _claim(session, review, "edit_phone", by=by, now=now):
        raise Refused("stale")


@dataclass(frozen=True, slots=True)
class TelResult:
    """``/tel``: the score the new phone gave, whether the candidate is now
    approved (``manager:<tg_id>``) on its way to the push, and — when a gate
    refuses the company with this phone (an ERP client's number, a DNC) —
    that gate, the candidate having taken the gate's walk instead."""

    approved: bool
    score: int
    gated: str | None = None


async def _source_url(
    session: AsyncSession, candidate: LeadCandidate, now: datetime
) -> str | None:
    """The message the manager answered: the URL the cards cite."""
    try:
        view = await candidates.load_view(session, candidate.id, now=now)
        cited, _ = crm_push.cited(view)
    except (LookupError, crm_push.PayloadError):
        return None
    return cited.url


async def tel(
    session: AsyncSession, review_id: int, raw: str, *, by: int, now: datetime
) -> TelResult:
    """``/tel <review_id> +998…`` (06 §1.7): the phone the prospect gave a
    manager, stored as the company's person-tier phone with
    ``legal_basis='pre_contract_request'``, ``source_url`` = the message the
    card cited and ``collected_at=now`` — then the candidate re-scored at
    once: ``hold → scored → approved(manager:<tg_id>)`` with a usable phone
    (the owner's handoff was the owner's approval). Only the manager the
    card went to may answer it. Raises ``Refused``."""
    review = await session.get(LeadReview, review_id, populate_existing=True)
    if review is None or review.kind != "manager":
        raise Refused("no_card")
    if review.by_telegram_id != by:
        raise Refused("not_yours")
    open_or_contacted = review.state in ("asked", "expired") or (
        review.state == "approved" and review.answer == "contacted"
    )
    if not open_or_contacted:
        raise Refused("stale")
    phone = usable_phone(raw)
    if phone is None:
        raise Refused("bad_phone")
    candidate = await push_gate.lock_for_push(session, review.candidate_id)
    if candidate is None or candidate.state != "hold":
        raise Refused("not_waiting")
    company = await session.get(Company, candidate.company_id)
    if company is None:
        raise Refused("not_waiting")
    url = await _source_url(session, candidate, now)
    if url is None:
        raise Refused("no_source")
    username = await cards.handoff_username(session, candidate)
    row = await companies.upsert_contact(
        session,
        company,
        tier="person",
        phone=phone.e164,
        username=username,
        legal_basis=TEL_BASIS,
        source_url=url,
        now=now,
    )
    if row is not None and row.tier == "person" and row.phone == phone.e164:
        # The phone is this request's: its basis is the row's now (the first
        # basis of 02 §2.4 was the public post the number was not in).
        row.legal_basis = TEL_BASIS
        row.source_url = url[:2048]
        row.collected_at = now
    held = await session.scalar(
        sa.select(
            sa.exists().where(
                Contact.company_id == company.id, Contact.phone_tail == phone.tail
            )
        )
    )
    if not held:
        # The person already holds another number: the new one is the firm's.
        await companies.upsert_contact(
            session,
            company,
            tier="company",
            phone=phone.e164,
            legal_basis=TEL_BASIS,
            source_url=url,
            now=now,
        )
    await session.flush()
    if review.state == "asked":
        await _claim(session, review, "contacted", by=by, now=now)
    # The gates before the approval (04 §6), over the phone just given: a
    # refused company walks the gate's way (excluded, suppressed, or it
    # stays held) and the manager is told it will not be pushed.
    gate = await gates.evaluate(session, company.id, now=now, tails=[phone.tail])
    failure = gate.decide(card=True)
    if failure is not None:
        result = await candidates.rescore(session, candidate, now=now)
        await candidates.apply_score(
            session, candidate, result, now=now, auto_push=False, gate=gate
        )
        return TelResult(approved=False, score=result.total, gated=failure.gate)
    result = await candidates.manager_phone_rescore(session, candidate, tg_id=by, now=now)
    return TelResult(approved=candidate.state == "approved", score=result.total)


# --- the footer a ✅ card follows (06 §1.3) --------------------------------------------


async def push_footer(
    session: AsyncSession, candidate: LeadCandidate, *, now: datetime
) -> str:
    """✅'s footer: sent at hh:mm; the ERP's answer unknown; refused for the
    month (attempt 3 failed, or a 4xx); else queued — now, or when the push
    window opens next («⏳ Ertaga 09:30 da yuboriladi»)."""
    latest = await session.scalar(
        sa.select(PushLog)
        .where(PushLog.candidate_id == candidate.id, PushLog.superseded_at.is_(None))
        .order_by(PushLog.sent_at.desc(), PushLog.attempt.desc())
        .limit(1)
    )
    if candidate.state == "pushed" and latest is not None and latest.status == "sent":
        at = (latest.finished_at or latest.sent_at).astimezone(settings.tz)
        return cards.footer("push_sent", time=at.strftime("%H:%M"))
    if latest is not None and latest.status == "unknown":
        return cards.footer("push_unknown")
    if latest is not None and latest.status == "failed":
        spent = latest.attempt >= push_gate.MAX_ATTEMPTS
        if spent or latest.error == "http_4xx":
            return cards.footer("push_failed")
    opens = push_gate.window_opens(now)
    if opens is None:
        return cards.footer("push_queued")
    return cards.footer("push_at", when=cards.push_opens_word(opens, now))


async def answered_text(
    session: AsyncSession, review_id: int, line: str, *, now: datetime
) -> str | None:
    """An answered card's message: the card's body (``render_review(…,
    answered=True)``) under the tap's footer; None when the body cannot be
    rendered any more."""
    view = await cards.review_view(session, review_id, now=now)
    if view is None:
        return None
    try:
        body = cards.render_review(view, answered=True).text
    except CardRefused:
        return None
    return cards.with_footer(body, line)


async def manager_answered_text(
    session: AsyncSession, review_id: int, line: str, *, now: datetime
) -> str | None:
    view = await cards.manager_view(session, review_id, now=now)
    if view is None:
        return None
    try:
        body = cards.render_manager(view).text
    except CardRefused:
        return None
    return cards.with_footer(body, line)


async def store_hash(session: AsyncSession, review_id: int, value: str) -> None:
    await session.execute(
        sa.update(LeadReview).where(LeadReview.id == review_id).values(card_hash=value)
    )


async def tapped_text(
    session: AsyncSession,
    review_id: int,
    line: str,
    *,
    now: datetime,
    shown: str | None,
) -> str:
    """The text a tapped card's message becomes (keyboard removed): the
    answered body under ``line``, else the text it showed under it; its
    hash stored (the job edits it again only when it changed)."""
    review = await session.get(LeadReview, review_id, populate_existing=True)
    render = (
        manager_answered_text
        if review is not None and review.kind == "manager"
        else answered_text
    )
    text = await render(session, review_id, line, now=now)
    if text is None:
        text = cards.with_footer(shown, line) if shown else line
    await store_hash(session, review_id, cards.card_hash(text, None))
    return text


# --- the sweep (02 §2.9) ----------------------------------------------------------------


async def expired_count(session: AsyncSession, candidate_id: uuid.UUID) -> int:
    """How many owner cards of the candidate expired unanswered."""
    found = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ReminderLog)
        .where(ReminderLog.kind == EXPIRED_REMINDER, ReminderLog.ref == str(candidate_id))
    )
    return int(found or 0)


async def expire_due(
    session: AsyncSession, *, now: datetime, limit: int = SWEEP_LIMIT
) -> list[Closed]:
    """Every ``asked`` card past ``expires_at`` → ``expired`` — but not a
    card put off with ⏭ and not asked again yet (``remind_at`` set: its old
    message has no buttons, nobody could have answered it; the resend gives
    it a fresh deadline). An owner card's candidate (still in ``review``) →
    ``hold`` 7 days and a ``reminder_log`` row; the third silence holds it
    with no timer (re-asked at most twice). A manager card's candidate keeps
    its handoff hold. The company and the candidate are locked before the
    row (the module's order), the pairs in company order — the order
    ``merge_into`` locks two companies in; the caller commits."""
    due = (
        await session.execute(
            sa.select(
                LeadReview.id,
                LeadReview.candidate_id,
                LeadReview.kind,
                LeadReview.card_chat_id,
                LeadReview.card_message_id,
                LeadCandidate.company_id,
            )
            .join(LeadCandidate, LeadCandidate.id == LeadReview.candidate_id)
            .where(
                LeadReview.state == "asked",
                LeadReview.expires_at <= now,
                LeadReview.remind_at.is_(None),
            )
            .order_by(LeadReview.expires_at, LeadReview.id)
            .limit(limit)
        )
    ).all()
    closed: list[Closed] = []
    ordered = sorted(due, key=lambda r: (r.company_id, r.id))
    for review_id, candidate_id, kind, chat_id, message_id, _ in ordered:
        candidate = await push_gate.lock_for_push(session, candidate_id)
        claimed = await session.execute(
            sa.update(LeadReview)
            .where(
                LeadReview.id == review_id,
                LeadReview.state == "asked",
                LeadReview.expires_at <= now,
                LeadReview.remind_at.is_(None),
            )
            .values(state="expired", remind_at=None)
            .returning(LeadReview.id)
        )
        if claimed.first() is None:
            continue
        closed.append(Closed(review_id, chat_id, message_id))
        if kind != "owner" or candidate is None or candidate.state != "review":
            continue
        silences = await expired_count(session, candidate.id)
        session.add(
            ReminderLog(kind=EXPIRED_REMINDER, ref=str(candidate.id), sent_at=now)
        )
        hold_until = now + EXPIRED_HOLD if silences < MAX_REASKS else None
        await candidates.transition(
            session, candidate, "hold", now=now, hold_until=hold_until
        )
        log.info(
            "review expired",
            extra={
                "candidate_id": str(candidate.id),
                "silences": silences + 1,
                "held_until": hold_until.isoformat() if hold_until else None,
            },
        )
    await session.flush()
    return closed


async def close_stale(session: AsyncSession, *, limit: int = SWEEP_LIMIT) -> list[Closed]:
    """Every ``asked`` card whose candidate left the state it waits on
    (06 §1.3: «edit it, never transition»): ``expired``,
    ``note='stale:<state>'``. A ``pending`` card stays: it was never shown,
    and ``claim_owner_review`` takes only a candidate in ``review``."""
    rows = (
        await session.execute(
            sa.select(
                LeadReview.id,
                LeadReview.card_chat_id,
                LeadReview.card_message_id,
                LeadCandidate.state,
            )
            .join(LeadCandidate, LeadCandidate.id == LeadReview.candidate_id)
            .where(
                LeadReview.state == "asked",
                sa.or_(
                    sa.and_(LeadReview.kind == "owner", LeadCandidate.state != "review"),
                    sa.and_(LeadReview.kind == "manager", LeadCandidate.state != "hold"),
                ),
            )
            .order_by(LeadReview.id)
            .limit(limit)
        )
    ).all()
    closed: list[Closed] = []
    for review_id, chat_id, message_id, state in rows:
        if await close_card(session, review_id, STALE_NOTE + state, states=("asked",)):
            closed.append(Closed(review_id, chat_id, message_id))
    return closed


async def ensure_pending(session: AsyncSession, *, now: datetime) -> int:
    """A ``pending`` owner card for every candidate in ``review`` that has no
    live one (02 §2.9: created by the card job). Returns how many."""
    live = (
        sa.select(LeadReview.id)
        .where(
            LeadReview.candidate_id == LeadCandidate.id,
            LeadReview.kind == "owner",
            LeadReview.state.in_(("pending", "asked")),
        )
        .exists()
    )
    stmt = (
        pg_insert(LeadReview)
        .from_select(
            ["candidate_id", "kind", "state", "created_at"],
            sa.select(
                LeadCandidate.id,
                sa.literal("owner"),
                sa.literal("pending"),
                sa.literal(now, sa.DateTime(timezone=True)),
            ).where(LeadCandidate.state == "review", ~live),
        )
        .on_conflict_do_nothing()
        .returning(LeadReview.id)
    )
    return len((await session.execute(stmt)).all())


# --- sending ------------------------------------------------------------------------


@dataclass(slots=True)
class Burst:
    """One run of owner cards: sent, set aside (could not be shown), and
    whether Telegram stopped taking them."""

    sent: int = 0
    skipped: int = 0
    failed: bool = False


async def send_owner_cards(
    session: AsyncSession, sender: CardSender, *, now: datetime, limit: int
) -> Burst:
    """The next ≤ ``limit`` ``pending`` owner cards, in 06 §1.6's order —
    ``/lidlar``'s loop and the card job's. Each is claimed (``FOR UPDATE …
    SKIP LOCKED``: a double tap or the job beside ``/lidlar`` never sends a
    card twice), rendered with the gates evaluated now, sent, and marked
    ``asked`` with its hash; a card Telegram did not take stays ``pending``
    and the burst stops. A card that cannot be shown leaves the queue
    (``cards.set_aside``). Commits per card."""
    burst = Burst()
    owner_id = settings.owner_telegram_id
    if owner_id is None:
        return burst
    handled: set[int] = set()
    while burst.sent < limit:
        review_id = await cards.claim_owner_review(session, skip=handled)
        if review_id is None:
            break
        handled.add(review_id)
        view = await cards.review_view(session, review_id, now=now)
        if view is None:
            await session.rollback()
            burst.skipped += 1
            continue
        try:
            card = cards.render_review(view)
        except CardRefused as refused:
            # The claim ends before the candidate is locked: the push gate
            # takes the company and the candidate first, then may write this
            # review row (its refusal note).
            await session.rollback()
            burst.skipped += 1
            await cards.set_aside(
                session, view.candidate.candidate_id, refused.code, view.gate, now=now
            )
            await session.commit()
            continue
        out = await sender.send(owner_id, card.text, card.keyboard, review_id)
        if out is None:
            await session.rollback()
            burst.failed = True
            break
        marked = await cards.mark_asked(
            session,
            review_id,
            chat_id=out.chat_id,
            message_id=out.message_id,
            card_hash=card.hash,
            now=now,
        )
        await session.commit()
        if not marked:
            # The claim makes this impossible; said, never hidden.
            log.error("claimed card not pending", extra={"review_id": review_id})
        burst.sent += 1
    return burst


async def _manager_for(
    session: AsyncSession, review: LeadReview, now: datetime
) -> int | None:
    """The manager a manager card goes to: its own while listed, else the
    least-loaded listed one (written on the row); None when nobody is."""
    if review.by_telegram_id in settings.manager_telegram_ids:
        return review.by_telegram_id
    chosen = await pick_manager(session, now)
    if chosen is None:
        return None
    review.by_telegram_id = chosen
    await session.flush()
    return chosen


async def send_manager_card(
    session: AsyncSession,
    review_id: int,
    sender: CardSender,
    *,
    now: datetime,
    refused: set[int] | None = None,
) -> SentCard | None:
    """A ``pending`` manager card to its manager (06 §1.7): a manager no
    longer in ``MANAGER_TELEGRAM_IDS`` is replaced by the least-loaded one;
    a card that cannot be shown is closed (``refused:<code>``). Nothing goes
    out while ``/toxtat`` holds («no card», 06 §1.2): the card waits
    ``pending`` for the job after ``/yoq``. ``refused``: the managers
    Telegram refused this run — skipped, and one refusing now is added. The
    caller commits. The sent card, or None."""
    if await owner.paused(session):
        return None
    review = await session.scalar(
        sa.select(LeadReview)
        .where(LeadReview.id == review_id, LeadReview.state == "pending")
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if review is None or review.kind != "manager":
        return None
    manager = await _manager_for(session, review, now)
    if manager is None or (refused is not None and manager in refused):
        return None
    view = await cards.manager_view(session, review_id, now=now)
    if view is None:
        await close_card(
            session, review_id, REFUSED_NOTE + "no_view", states=("pending",)
        )
        return None
    try:
        card = cards.render_manager(view)
    except CardRefused as why:
        await close_card(session, review_id, REFUSED_NOTE + why.code, states=("pending",))
        return None
    out = await sender.send(manager, card.text, card.keyboard, review_id)
    if out is None:
        if refused is not None:
            refused.add(manager)
        return None
    await cards.mark_asked(
        session,
        review_id,
        chat_id=out.chat_id,
        message_id=out.message_id,
        card_hash=card.hash,
        now=now,
    )
    return out


@dataclass(frozen=True, slots=True)
class _Drawn:
    """A live card as it reads now."""

    text: str
    layout: CardLayout


@dataclass(frozen=True, slots=True)
class _Unshowable:
    """A live card that cannot be shown any more: an owner card's candidate
    is to be set aside (``cards.set_aside``) once the card's row is let go."""

    candidate_id: uuid.UUID | None = None
    code: str = ""
    gate: gates.GateResult | None = None


async def _draw(
    session: AsyncSession, review: LeadReview, *, now: datetime
) -> _Drawn | _Unshowable:
    """A live card as it reads now (the gates evaluated): owner or manager.
    Reads only — the caller may hold the card's row."""
    if review.kind == "manager":
        mv = await cards.manager_view(session, review.id, now=now)
        if mv is None:
            return _Unshowable()
        try:
            card = cards.render_manager(mv)
        except CardRefused:
            return _Unshowable()
        return _Drawn(card.text, card.keyboard)
    view = await cards.review_view(session, review.id, now=now)
    if view is None:
        return _Unshowable()
    try:
        card = cards.render_review(view)
    except CardRefused as refused:
        return _Unshowable(view.candidate.candidate_id, refused.code, view.gate)
    return _Drawn(card.text, card.keyboard)


async def _set_aside_later(
    session_factory: SessionFactory, shown: _Unshowable, *, now: datetime
) -> None:
    """The set-aside of a card the job could not show, in its own
    transaction: the company and the candidate are locked there, after the
    card's row was let go (the tap's order is company → candidate → row)."""
    if shown.candidate_id is None:
        return
    async with session_factory() as session:
        await cards.set_aside(
            session, shown.candidate_id, shown.code, shown.gate, now=now
        )
        await session.commit()


async def _lock_live(
    session: AsyncSession, review_id: int, *, now: datetime, due: bool
) -> LeadReview | None:
    """The card's row for an edit, held to the end of the caller's
    transaction (``FOR UPDATE … SKIP LOCKED``): ``asked``, with a message;
    a ⏭ card whose resend is due (``due``) or a card not put off. A tap
    holding the row meanwhile makes the job skip it this run."""
    stmt = sa.select(LeadReview).where(
        LeadReview.id == review_id,
        LeadReview.state == "asked",
    )
    if due:
        stmt = stmt.where(LeadReview.remind_at <= now)
    else:
        stmt = stmt.where(
            LeadReview.remind_at.is_(None), LeadReview.card_message_id.is_not(None)
        )
    found: LeadReview | None = await session.scalar(
        stmt.with_for_update(skip_locked=True).execution_options(populate_existing=True)
    )
    return found


async def resend_later(
    session_factory: SessionFactory,
    sender: CardSender,
    *,
    now: datetime,
    owner_limit: int,
) -> tuple[int, int]:
    """The cards put off with ⏭ whose day has come: sent again as a fresh
    message (the old one lost its buttons at the tap), still ``asked``, with
    at least a fresh card's ``REVIEW_EXPIRY_HOURS`` from now (``expires_at``
    never moves earlier). Owner cards within ``owner_limit``, each counted
    against the day's cap (``RESENT_REMINDER``); manager cards to their
    manager, a manager no longer listed replaced by the least-loaded one. A
    recipient Telegram did not take is skipped for the rest of the run — the
    others still get theirs. Returns (owner, manager) cards sent."""
    async with session_factory() as session:
        due = list(
            await session.scalars(
                sa.select(LeadReview.id)
                .where(LeadReview.state == "asked", LeadReview.remind_at <= now)
                .order_by(LeadReview.remind_at, LeadReview.id)
                .limit(SWEEP_LIMIT)
            )
        )
    sent_owner = sent_manager = 0
    refused: set[int] = set()
    for review_id in due:
        async with session_factory() as session:
            review = await _lock_live(session, review_id, now=now, due=True)
            if review is None:
                continue
            if review.kind == "owner":
                if sent_owner >= owner_limit:
                    continue
                to = settings.owner_telegram_id
            else:
                to = await _manager_for(session, review, now)
            if to is None or to in refused:
                await session.rollback()
                continue
            drawn = await _draw(session, review, now=now)
            if isinstance(drawn, _Unshowable):
                await session.rollback()
                await _set_aside_later(session_factory, drawn, now=now)
                continue
            out = await sender.send(to, drawn.text, drawn.layout, review_id)
            if out is None:
                await session.rollback()
                refused.add(to)
                continue
            fresh_deadline = now + timedelta(hours=settings.review_expiry_hours)
            await session.execute(
                sa.update(LeadReview)
                .where(LeadReview.id == review_id)
                .values(
                    card_chat_id=out.chat_id,
                    card_message_id=out.message_id,
                    card_hash=cards.card_hash(drawn.text, drawn.layout),
                    remind_at=None,
                    expires_at=sa.func.greatest(LeadReview.expires_at, fresh_deadline),
                )
            )
            if review.kind == "owner":
                session.add(
                    ReminderLog(kind=RESENT_REMINDER, ref=str(review_id), sent_at=now)
                )
            await session.commit()
            if review.kind == "owner":
                sent_owner += 1
            else:
                sent_manager += 1
    return sent_owner, sent_manager


async def follow_handoff(
    session: AsyncSession,
    manager_review_id: int,
    out: SentCard,
    sender: CardSender,
    *,
    now: datetime,
) -> bool:
    """The owner's handoff card, once the job delivered its manager card:
    the footer says who received it (06 §1.3 «👤 Menejerga yuborildi —
    {manager}»), edited only when it changed. True when edited."""
    manager_card = await session.get(LeadReview, manager_review_id)
    if manager_card is None:
        return False
    review = await session.scalar(
        sa.select(LeadReview)
        .where(
            LeadReview.candidate_id == manager_card.candidate_id,
            LeadReview.kind == "owner",
            LeadReview.state == "approved",
            LeadReview.answer == "handoff",
            LeadReview.card_message_id.is_not(None),
        )
        .order_by(LeadReview.answered_at.desc(), LeadReview.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if review is None:
        return False
    text = await answered_text(session, review.id, handoff_footer(out), now=now)
    if text is None:
        return False
    digest = cards.card_hash(text, None)
    if digest == review.card_hash:
        return False
    chat, message = review.card_chat_id, review.card_message_id
    assert chat is not None and message is not None
    if not await sender.edit(chat, message, text, None, review.id):
        return False
    await store_hash(session, review.id, digest)
    return True


async def send_pending_managers(
    session_factory: SessionFactory, sender: CardSender, *, now: datetime
) -> int:
    """Manager cards a handoff made that are not out yet (sent outside the
    hours, while paused, with no manager listed, or Telegram did not take
    them): ≤ ``REVIEW_BURST_MAX`` sent per run, oldest first; a manager
    Telegram refused is skipped for the rest of the run, so the cards of a
    manager who blocked the bot never hold the others back. Each delivery
    edits the owner's handoff card to name the manager."""
    async with session_factory() as session:
        waiting = list(
            await session.scalars(
                sa.select(LeadReview.id)
                .join(LeadCandidate, LeadCandidate.id == LeadReview.candidate_id)
                .where(
                    LeadReview.kind == "manager",
                    LeadReview.state == "pending",
                    LeadCandidate.state == "hold",
                )
                .order_by(LeadReview.created_at, LeadReview.id)
                .limit(SWEEP_LIMIT)
            )
        )
    sent = 0
    refused: set[int] = set()
    for review_id in waiting:
        if sent >= settings.review_burst_max:
            break
        async with session_factory() as session:
            out = await send_manager_card(
                session, review_id, sender, now=now, refused=refused
            )
            await session.commit()
        if out is None:
            continue
        sent += 1
        async with session_factory() as session:
            if await follow_handoff(session, review_id, out, sender, now=now):
                await session.commit()
    return sent


def _rotation(ids: list[int], limit: int, now: datetime) -> list[int]:
    """The live cards a refresh re-judges this run: the newest half of
    ``limit``, then a window of the rest that moves on every run (the slot
    of ``REFRESH_SLOT`` ``now`` falls in), so every card is re-judged within
    a few runs however many are live."""
    if len(ids) <= limit:
        return ids
    head, rest = ids[: limit // 2], ids[limit // 2 :]
    window = limit - len(head)
    slot = int(now.timestamp() // REFRESH_SLOT.total_seconds())
    start = (slot * window) % len(rest)
    return head + [rest[(start + i) % len(rest)] for i in range(window)]


async def refresh_live(
    session_factory: SessionFactory,
    sender: CardSender,
    *,
    now: datetime,
    limit: int = REFRESH_MAX,
) -> int:
    """Live owner cards re-rendered (the gates evaluated now): edited only
    when ``card_hash`` changed, the card's row held across the edit (a tap
    landing meanwhile waits for it — its own edit comes last); a card that
    can no longer be shown sets its candidate aside, and the next run closes
    the card. ⏭ cards wait for their resend. Returns the edits."""
    async with session_factory() as session:
        live = list(
            await session.scalars(
                sa.select(LeadReview.id)
                .join(LeadCandidate, LeadCandidate.id == LeadReview.candidate_id)
                .where(
                    LeadReview.kind == "owner",
                    LeadReview.state == "asked",
                    LeadReview.remind_at.is_(None),
                    LeadReview.card_message_id.is_not(None),
                    LeadCandidate.state == "review",
                )
                .order_by(LeadReview.asked_at.desc(), LeadReview.id)
                .limit(LIVE_SCAN_MAX)
            )
        )
    edits = 0
    for review_id in _rotation(live, limit, now):
        async with session_factory() as session:
            review = await _lock_live(session, review_id, now=now, due=False)
            if review is None:
                continue
            drawn = await _draw(session, review, now=now)
            if isinstance(drawn, _Unshowable):
                await session.rollback()
                await _set_aside_later(session_factory, drawn, now=now)
                continue
            digest = cards.card_hash(drawn.text, drawn.layout)
            if digest == review.card_hash:
                continue
            chat, message = review.card_chat_id, review.card_message_id
            assert chat is not None and message is not None
            if await sender.edit(chat, message, drawn.text, drawn.layout, review_id):
                await store_hash(session, review_id, digest)
                await session.commit()
                edits += 1
    return edits


async def refresh_push_footers(
    session_factory: SessionFactory, sender: CardSender, *, now: datetime
) -> int:
    """✅ cards whose push finished within ``PUSH_FOOTER_WINDOW``: the
    footer follows it (06 §1.3 «✅ CRMʼga yuborildi {hh:mm}»), edited only on
    a change, the card's row held across the edit. A tap's own edit that
    lands after this one (the push was faster than the tap's edit) is put
    right by the next run inside the window: its hash differs. Returns the
    edits."""
    async with session_factory() as session:
        rows = list(
            await session.scalars(
                sa.select(LeadReview.id)
                .join(PushLog, PushLog.candidate_id == LeadReview.candidate_id)
                .where(
                    LeadReview.kind == "owner",
                    LeadReview.state == "approved",
                    LeadReview.answer == "push",
                    LeadReview.card_message_id.is_not(None),
                    PushLog.finished_at >= now - PUSH_FOOTER_WINDOW,
                    PushLog.finished_at <= now,
                )
                .distinct()
            )
        )
    edits = 0
    for review_id in sorted(rows):
        async with session_factory() as session:
            review = await session.scalar(
                sa.select(LeadReview)
                .where(LeadReview.id == review_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            candidate = (
                await session.get(LeadCandidate, review.candidate_id)
                if review is not None
                else None
            )
            if review is None or candidate is None:
                continue
            line = await push_footer(session, candidate, now=now)
            text = await answered_text(session, review_id, line, now=now)
            if text is None:
                continue
            digest = cards.card_hash(text, None)
            if digest == review.card_hash:
                continue
            chat, message = review.card_chat_id, review.card_message_id
            assert chat is not None and message is not None
            if await sender.edit(chat, message, text, None, review_id):
                await store_hash(session, review_id, digest)
                await session.commit()
                edits += 1
    return edits


async def strip_closed(
    session_factory: SessionFactory, sender: CardSender, *, now: datetime
) -> int:
    """Every closed card whose message may still show its buttons (an
    ``expired`` row that still names its message — closed by the sweep, as
    stale, or while ``/toxtat`` held or no bot was wired): the buttons go
    (a later tap would answer «Bu karta eskirgan» anyway), then the row
    forgets the message. Only cards asked within ``STRIP_WINDOW``: a message
    Telegram will not edit any more is not retried for ever. Returns the
    cards stripped."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.select(
                    LeadReview.id, LeadReview.card_chat_id, LeadReview.card_message_id
                )
                .where(
                    LeadReview.state == "expired",
                    LeadReview.card_message_id.is_not(None),
                    LeadReview.card_chat_id.is_not(None),
                    LeadReview.asked_at >= now - STRIP_WINDOW,
                )
                .order_by(LeadReview.id)
                .limit(SWEEP_LIMIT)
            )
        ).all()
    stripped = 0
    for review_id, chat_id, message_id in rows:
        if not await sender.strip(chat_id, message_id):
            continue
        async with session_factory() as session:
            await session.execute(
                sa.update(LeadReview)
                .where(LeadReview.id == review_id, LeadReview.state == "expired")
                .values(card_message_id=None)
            )
            await session.commit()
        stripped += 1
    return stripped


# --- the job (06 §1.6) --------------------------------------------------------------


@dataclass(slots=True)
class CardsReport:
    expired: int = 0
    stale: int = 0
    pending: int = 0
    stripped: int = 0
    footers: int = 0
    refreshed: int = 0
    resent: int = 0
    sent: int = 0
    skipped: int = 0
    managers: int = 0
    failed: bool = False
    # Why nothing went out: 'paused' | 'no_bot' | 'hours' | None.
    held: str | None = None
    closed: list[Closed] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "expired": self.expired,
            "stale": self.stale,
            "pending": self.pending,
            "stripped": self.stripped,
            "footers": self.footers,
            "refreshed": self.refreshed,
            "resent": self.resent,
            "sent": self.sent,
            "skipped": self.skipped,
            "managers": self.managers,
            "failed": self.failed,
            "held": self.held,
        }


async def run_cards(
    sender: CardSender | None,
    *,
    now: datetime,
    session_factory: SessionFactory = SessionLocal,
) -> CardsReport:
    """One run of the card job (see the module docstring): the bookkeeping
    always, Telegram only with a sender and no global pause, new cards only
    inside the hours."""
    report = CardsReport()
    async with session_factory() as session:
        expired = await expire_due(session, now=now)
        stale = await close_stale(session)
        report.pending = await ensure_pending(session, now=now)
        paused = await owner.paused(session)
        await session.commit()
    report.expired, report.stale = len(expired), len(stale)
    report.closed = [*expired, *stale]
    if paused:
        report.held = "paused"
        return report
    if sender is None:
        report.held = "no_bot"
        return report
    report.stripped = await strip_closed(session_factory, sender, now=now)
    report.footers = await refresh_push_footers(session_factory, sender, now=now)
    report.refreshed = await refresh_live(session_factory, sender, now=now)
    if not sending_hours(now):
        report.held = "hours"
        return report
    async with session_factory() as session:
        budget = owner_budget(await asked_today(session, now))
    owner_resent, report.managers = await resend_later(
        session_factory, sender, now=now, owner_limit=budget
    )
    report.resent = owner_resent
    budget -= owner_resent
    if budget > 0:
        async with session_factory() as session:
            burst = await send_owner_cards(session, sender, now=now, limit=budget)
        report.sent, report.skipped, report.failed = (
            burst.sent,
            burst.skipped,
            burst.failed,
        )
    report.managers += await send_pending_managers(session_factory, sender, now=now)
    return report


@dataclass(slots=True)
class _Seams:
    sender: CardSender | None = None


_seams = _Seams()


def set_card_sender(sender: CardSender | None) -> None:
    """The bot the worker sends cards with (``worker.connect_owner_bot``);
    None: the job keeps its books and sends nothing."""
    _seams.sender = sender


async def review_cards_job(*, now: datetime | None = None) -> dict[str, Any]:
    """The worker's ``review_cards`` slot (every 2 minutes, 06 §1.6)."""
    report = await run_cards(_seams.sender, now=now or datetime.now(settings.tz))
    counts = report.as_dict()
    if any(v for k, v in counts.items() if k not in ("held", "failed")):
        log.info("review_cards_job", extra={"counts": counts})
    return counts


__all__ = [
    "EDIT_PHONE_WINDOW",
    "EXPIRED_HOLD",
    "EXPIRED_REMINDER",
    "HANDOFF_HOLD",
    "LATER",
    "MANAGER_ANSWERS",
    "MAX_REASKS",
    "OWNER_ANSWERS",
    "RESENT_REMINDER",
    "REVIEW_DAYS",
    "ROW_STATE",
    "STALE_NOTE",
    "TEL_BASIS",
    "Burst",
    "CardSender",
    "CardsReport",
    "Closed",
    "Refused",
    "SentCard",
    "Tapped",
    "TelResult",
    "answer",
    "answered_text",
    "asked_today",
    "close_card",
    "close_stale",
    "edit_phone",
    "ensure_pending",
    "expire_due",
    "expired_count",
    "follow_handoff",
    "handoff_footer",
    "handoff_line",
    "owner_budget",
    "pick_manager",
    "push_footer",
    "refresh_live",
    "refresh_push_footers",
    "resend_later",
    "review_cards_job",
    "run_cards",
    "send_manager_card",
    "send_owner_cards",
    "send_pending_managers",
    "sending_hours",
    "set_card_sender",
    "strip_closed",
    "tapped_text",
    "tel",
    "usable_phone",
]
