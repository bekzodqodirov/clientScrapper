"""Every card the owner bot shows (06 §1.3, §1.4, §1.7; 07 §1.11/T-20).

The renderers are pure — a view in, ``Card(text, keyboard, hash)`` out — and
golden-tested (``tests/test_cards.py``); ``bot/`` only delivers what they
return and builds the keyboard ``Card.keyboard`` names (the grammar and
aiogram are the bot's). Text is HTML (the bot's default parse mode): every
value from the database is escaped here.

* ``render_review`` — the owner's review card. Its words are the services'
  own, never restated: the quote is the one the ERP note cites
  (``crm_push.cited``: verbatim, verified, contact-free), its URL line and
  the 🧮 breakdown are the note's (``url_line``, ``ball_parts``), China's
  «ha/ehtimol» is ``crm_push.china_signal``'s, the band the note's
  ``band_of``, the registry word the gates' and the last line the three
  gates' own lines (4 ``not_client``, 5 ``not_dnc``, 6 ``not_recent`` —
  «90 kunda yuborilmagan» is gate 6: no open push for the company in 90
  days and none for its phone in 24 hours). A card exists only because every
  gate passed, so a failing gate is a refusal (``CardRefused('gate')``), as
  is a quote that cannot be cited and a ``review`` row with neither a usable
  phone nor ``score_breakdown.gates.no_phone`` (06 §1.3). The phone-less
  row renders the handoff variant: «📨 Faqat Telegram: @…» and
  ``[👤 Menejerga] [✖️ Yoʻq]``. A shared lead phone (04 §2.3) adds
  «⚠️ Raqam 3+ firmada uchraydi».
* ``render_manager`` — the manager handoff card (06 §1.7), with the owner's
  decision 2a line «Yozishdan oldin ERPda nomi boʻyicha qidiring» (01 §7).
* ``render_holat`` / ``render_xarajat`` — 06 §1.4's two layouts over
  ``health.gather``'s ``Status`` and ``costs.report``.
* ``render_company`` — ``/qidir``'s company card (06 §1.2).

The loaders (``review_view``, ``search``, …) read what a renderer needs
through the services that own it; ``claim_owner_review`` / ``mark_asked``
are ``/lidlar``'s half of the card's life (``pending → asked``), and
``set_aside`` takes a card that cannot be shown out of the queue.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import (
    Company,
    Contact,
    LeadCandidate,
    LeadReview,
    Outcome,
    PushLog,
)
from clientscrapper.services import (
    candidates,
    companies,
    costs,
    crm_push,
    gates,
    health,
    keys,
    normalise,
    purge,
    push_gate,
    redlines,
    schemas,
    suppression,
    validators,
)
from clientscrapper.services import verdict as verdict_service
from clientscrapper.services.candidates import CandidateView, SignalEvidence
from clientscrapper.services.gates import GateResult
from clientscrapper.services.suppression import Hit

log = logging.getLogger(__name__)

# 06 §1.3: «whole card ≤ 3,500 chars»; the quote ≤ 140 (the note's own cut).
CARD_MAX: Final = 3500
QUOTE_MAX: Final = crm_push.QUOTE_MAX
CardLayout = Literal["review", "handoff", "manager"]

# The card's first line must fit a 360 px phone on one line (07 §3.1;
# tests/test_cards.py measures it at 100/100): short segment words (01 §2's
# segments A–E; X competitor/supplier, Z unclear — 04 §4.4).
SEGMENT_UZ: Final[Mapping[str, str]] = {
    "A": "Bozor optom",
    "B": "Marketpleys",
    "C": "Fabrika",
    "D": "Qurilish/mebel",
    "E": "Elektronika",
    "X": "Raqobatchi",
    "Z": "Aniqlanmagan",
}
NO_SEGMENT_UZ: Final = "Segment yoʻq"
PHONE_KIND_UZ: Final[Mapping[str, str]] = {
    "mobile": "mobil",
    "landline": "shahar",
    "short": "qisqa",
    "unknown": "nomaʼlum",
}
# 06 §1.3 «📐 Hajm: … — {oʻzi yozgan | eʼlon | vakansiya | katalog | koʻrgazma}»:
# where the volume was said, by the source family of its signal.
VOLUME_SAID_UZ: Final[Mapping[str, str]] = {
    "telegram": "oʻzi yozgan",
    "instagram": "oʻzi yozgan",
    "olx": "eʼlon",
    "marketplace": "eʼlon",
    "vacancy": "vakansiya",
    "directory": "katalog",
    "maps": "katalog",
    "brand": "katalog",
    "exhibitor": "koʻrgazma",
    "search": "sayt",
    "registry": "reestr",
    "erp": "ERP",
    "manual": "qoʻlda",
}
# The bands of ``crm_push.BAND_HAJM`` in the card's short words.
BAND_UZ: Final[Mapping[str, str]] = {
    "1_3": "1–3 kub",
    "3_10": "3–10 kub",
    "truck": "fura",
    "container": "konteyner",
}
# Gates 4–6 (04 §6): the card's last line.
CARD_GATES: Final = ("not_client", "not_dnc", "not_recent")
PASSED_MARK: Final = " ✓"
NO_GOODS_UZ: Final = "tovar nomaʼlum"
NAMELESS_UZ: Final = "firma nomaʼlum"
SHARED_PHONE_UZ: Final = "⚠️ Raqam 3+ firmada uchraydi"

# The manager card's fixed lines (06 §1.7, 01 §7 decision 2a).
MANAGER_HOURS_UZ: Final = "oʻz akkauntingizdan, 10:00–17:00 (Du–Sha)"
# The last sentence is the opt-out line of the playbook (§2.6) the redline
# gate holds every opener to (services/redlines.OPTOUT_UZ — one home).
NO_PRICE_UZ: Final = f"⚠️ Birinchi xabarda narx yoʻq. Oxirgi jumla: «{redlines.OPTOUT_UZ}»"
ERP_FIRST_UZ: Final = "🔍 Yozishdan oldin ERPda nomi boʻyicha qidiring"
# The manager card shows the whole opener — the message the manager sends is
# this card's to show (docs/sotuv-playbook.md §3.0): up to the longest a draft
# may be (04 §4.5 ``DraftOpener``; every template is within it), under the
# card's 3,500. The ERP note has less room (``crm_push.OPENER_MAX``).
OPENER_MAX: Final = schemas.OPENER_CHARS[1]
# With no opener stored yet: the agent's own templates of the playbook — §3.0,
# the ones with no price and no day count (§3.1 is the managers' full script).
OPENER_FALLBACK_UZ: Final = "sotuv qoʻllanmasidagi «{segment}» shabloni (§3.0)"
# 04 §4.4: the verdict's reasons on the owner card; 04 §8: the line when a
# cap refused the verdict; the lt1 flag (validators.verdict_small), no points.
VERDICT_LINE_UZ: Final = "🤖 AI xulosasi: "
VERDICT_REFUSED_UZ: Final = "🤖 AI xulosasi: byudjet tugadi"
VERDICT_SMALL_UZ: Final = "⚠️ AI: yuk 1 kubdan kam koʻrinadi (ball qoʻshilmadi)"
VERDICT_REASON_SEP: Final = "; "


class CardRefused(Exception):
    """A card that must not be shown: ``no_phone`` (a review row with neither
    a usable phone nor the handoff flag), ``no_quote`` (no citable quote),
    ``gate`` (a gate failed since the row was routed), ``no_username`` (a
    handoff with nobody to write to), ``too_long``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Card:
    text: str
    keyboard: CardLayout
    hash: str


def card_hash(text: str, keyboard: CardLayout) -> str:
    """``lead_reviews.card_hash``: the rendered card, keyboard layout
    included (a card is edited only when this changed)."""
    return hashlib.sha256(f"{keyboard}\n{text}".encode()).hexdigest()


def _card(lines: Sequence[str], keyboard: CardLayout) -> Card:
    text = "\n".join(line for line in lines if line)
    if len(text) > CARD_MAX:
        raise CardRefused("too_long")
    return Card(text, keyboard, card_hash(text, keyboard))


def esc(value: object) -> str:
    return health.escape(value)


def day(at: datetime) -> str:
    return at.astimezone(settings.tz).strftime("%d.%m.%Y")


def phone_shown(phone: str) -> str:
    """«+998 90 000 00 61» for an Uzbek number; any other as stored."""
    digits = phone[1:] if phone.startswith("+") else phone
    if phone.startswith("+998") and len(digits) == 12 and digits.isdigit():
        d = digits[3:]
        return f"+998 {d[:2]} {d[2:5]} {d[5:7]} {d[7:]}"
    return phone


def segment_label(segment: str | None) -> str:
    return SEGMENT_UZ.get(segment or "", NO_SEGMENT_UZ)


def _company_line(view: CandidateView, *, details: bool = True) -> str:
    name = view.display_name
    if companies.is_nameless_label(name):
        head = esc(NAMELESS_UZ)
    else:
        head = esc(name)
        form = view.legal_form
        if details and form and form.lower() not in name.lower().split():
            head += f" ({esc(form)})"
    parts = [head]
    if view.city:
        parts.append(esc(view.city))
    if details:
        if view.stir:
            parts.append(f"STIR {esc(view.stir)}")
        if view.registry_status != "unknown":
            parts.append(esc(gates.REGISTRY_UZ[view.registry_status]))
    return "🏢 " + " · ".join(parts)


def _goods(view: CandidateView) -> str:
    goods = [
        g for g in (crm_push.clean(x) for x in view.goods[: crm_push.GOODS_MAX]) if g
    ]
    return esc(", ".join(goods)) if goods else esc(NO_GOODS_UZ)


def volume_label(view: CandidateView) -> str | None:
    """«2 kub» (stated, as the poster said it) | 1–3 kub | 3–10 kub | fura |
    konteyner; None when there is neither (the line is left out)."""
    stated = crm_push.stated_m3(view)
    if stated is not None:
        return f"«{crm_push.fmt_number(stated)} kub»"
    band = crm_push.band_of(view)
    return BAND_UZ[band] if band is not None else None


def _volume_said(view: CandidateView) -> str | None:
    best = next((s for s in view.signals if s.group == "volume" and s.points > 0), None)
    if best is None:
        return None
    return VOLUME_SAID_UZ.get(best.source_family)


def _quote(view: CandidateView) -> tuple[SignalEvidence, str]:
    try:
        cited, quote = crm_push.cited(view)
    except crm_push.PayloadError:
        raise CardRefused("no_quote") from None
    return cited, crm_push.clip_words(quote, QUOTE_MAX)


def _quote_line(quote: str, title: str, cited: SignalEvidence) -> str:
    when = day(cited.published_at or cited.fetched_at)
    return f"🔎 «{esc(quote)}» — {esc(title)}, {when}"


def _gate_line(gate: GateResult) -> str:
    if gate.decide() is not None:
        raise CardRefused("gate")
    lines = dict(zip(gates.GATES, gate.lines, strict=True))
    words = [lines[g].removesuffix(PASSED_MARK) for g in CARD_GATES]
    return "✔️ " + " · ".join(esc(w) for w in words)


# --- the owner's review card (06 §1.3) ---------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewView:
    """What the owner card shows beside the push's ``CandidateView``."""

    review_id: int
    candidate: CandidateView
    gate: GateResult
    segment: str | None = None
    phone_kind: str | None = None
    shared_phone: bool = False
    # score_breakdown.gates.no_phone: the phone-less handoff variant.
    handoff: bool = False
    # The contact a manager would write to (the handoff variant's line).
    handoff_username: str | None = None
    # The lead phone is usable (04 §1.1, ``push_gate.usable_lead_phone``);
    # None = judged by its presence alone. A handoff row whose only phone is
    # not usable (short, foreign) is still the handoff card: ✅ would be
    # refused by the push gate's item 4.
    phone_usable: bool | None = None
    # The company's RelevanceVerdict (04 §4.4): its Uzbek reasons, its lt1
    # flag; ``verdict_refused`` when a cap refused it today and none stands.
    verdict_reasons: tuple[str, ...] = ()
    verdict_small: bool = False
    verdict_refused: bool = False

    @property
    def handoff_card(self) -> bool:
        usable = (
            self.phone_usable
            if self.phone_usable is not None
            else self.candidate.phone is not None
        )
        return self.handoff and not usable


def _contact_line(rv: ReviewView) -> str:
    c = rv.candidate
    parts: list[str] = []
    if c.phone is not None:
        kind = PHONE_KIND_UZ.get(rv.phone_kind or "unknown", PHONE_KIND_UZ["unknown"])
        parts.append(f"📞 {esc(phone_shown(c.phone))} ({esc(kind)})")
    if crm_push.masul_line(c) is not None and c.person is not None:
        person = esc(crm_push.clean(c.person.full_name) or "")
        role = crm_push.clean(c.person.role)
        parts.append(f"👤 {person}" + (f", {esc(role)}" if role else ""))
    if c.telegram:
        parts.append(f"@{esc(c.telegram)}")
    if c.instagram:
        parts.append(f"IG {esc(c.instagram)}")
    if c.website_domain:
        parts.append(esc(c.website_domain))
    return " · ".join(parts)


def verdict_lines(rv: ReviewView) -> list[str]:
    """The card's AI lines (04 §4.4, §8): the reasons, else the budget's
    word; the lt1 flag beside either."""
    reasons = [r for r in (crm_push.clean(x) for x in rv.verdict_reasons) if r]
    lines: list[str] = []
    if reasons:
        lines.append(VERDICT_LINE_UZ + esc(VERDICT_REASON_SEP.join(reasons)))
    elif rv.verdict_refused:
        lines.append(VERDICT_REFUSED_UZ)
    if rv.verdict_small:
        lines.append(VERDICT_SMALL_UZ)
    return lines


def render_review(rv: ReviewView) -> Card:
    """06 §1.3's card. Raises ``CardRefused``."""
    c = rv.candidate
    if rv.handoff_card:
        if not rv.handoff_username:
            raise CardRefused("no_username")
        contact = f"📨 Faqat Telegram: @{esc(rv.handoff_username)}"
        layout: CardLayout = "handoff"
    elif c.phone is not None:
        contact = _contact_line(rv)
        layout = "review"
    else:
        raise CardRefused("no_phone")
    gate_line = _gate_line(rv.gate)
    cited, quote = _quote(c)
    china = crm_push.china_signal(c)
    goods = f"📦 {_goods(c)}" + (f" · Xitoy: {china[1]}" if china is not None else "")
    volume = volume_label(c)
    said = _volume_said(c)
    title = crm_push.SOURCE_TITLE_UZ.get(cited.source_family, cited.source_key)
    ball = crm_push.ball_parts(c)
    lines = [
        f"🆕 Nomzod · {c.score}/100 · {esc(segment_label(rv.segment))}",
        _company_line(c),
        goods,
        (f"📐 Hajm: {esc(volume)}" + (f" — {esc(said)}" if said else ""))
        if volume
        else "",
        contact,
        SHARED_PHONE_UZ if rv.shared_phone and layout == "review" else "",
        _quote_line(quote, title, cited),
        f"🔗 {esc(crm_push.url_line(cited))}",
        *verdict_lines(rv),
        ("🧮 " + " · ".join(esc(p) for p in ball)) if ball else "",
        gate_line,
    ]
    return _card(lines, layout)


# --- the manager handoff card (06 §1.7) --------------------------------------------


@dataclass(frozen=True, slots=True)
class ManagerView:
    review_id: int
    candidate: CandidateView
    username: str
    segment: str | None = None
    # The chat the quote was posted in (the evidence's), when known.
    chat_title: str | None = None


def render_manager(mv: ManagerView) -> Card:
    """06 §1.7's card, with decision 2a's ERP line. Raises ``CardRefused``."""
    c = mv.candidate
    if not mv.username:
        raise CardRefused("no_username")
    cited, quote = _quote(c)
    title = mv.chat_title or crm_push.SOURCE_TITLE_UZ.get(
        cited.source_family, cited.source_key
    )
    volume = volume_label(c)
    opener = crm_push.clean(c.opener)
    if opener:
        # The whole message the manager sends (docs/sotuv-playbook.md §3.0),
        # never cut before its question and its opt-out line.
        offer = esc(crm_push.fit_opener(opener, OPENER_MAX))
    else:
        offer = esc(OPENER_FALLBACK_UZ.format(segment=segment_label(mv.segment)))
    lines = [
        # 06 §1.7's «Telegram orqali» left line 1 for the 360 px phone: the
        # 📨 and the «Yozish: t.me/…» line say it.
        f"📨 Nomzod · {c.score}/100 · {esc(segment_label(mv.segment))}",
        _company_line(c, details=False),
        f"📦 {_goods(c)}" + (f" · 📐 {esc(volume)}" if volume else ""),
        _quote_line(quote, title, cited),
        f"🔗 {esc(crm_push.url_line(cited))}",
        f"👤 Yozish: t.me/{esc(mv.username)} — {MANAGER_HOURS_UZ}",
        f"✍️ Taklif: {offer}",
        esc(NO_PRICE_UZ),
        ERP_FIRST_UZ,
    ]
    return _card(lines, "manager")


# --- loading a review card ----------------------------------------------------------


async def _handoff_username(
    session: AsyncSession, candidate: LeadCandidate
) -> str | None:
    """The username a manager writes to: the candidate's own contact's, else
    the lineage's oldest live contact with one (02 §2.9's «contacts.
    telegram_username IS NOT NULL»)."""
    lineage = sorted(
        (await companies.lineages(session, [candidate.company_id]))[candidate.company_id]
    )
    rows = (
        await session.execute(
            sa.select(Contact.id, Contact.telegram_username)
            .where(
                Contact.company_id.in_(lineage),
                Contact.telegram_username.is_not(None),
                Contact.anonymised_at.is_(None),
            )
            .order_by(Contact.created_at, Contact.id)
        )
    ).all()
    own = next((u for cid, u in rows if cid == candidate.contact_id), None)
    return own or next((u for _, u in rows), None)


async def review_view(
    session: AsyncSession, review_id: int, *, now: datetime
) -> ReviewView | None:
    """The owner card's view of review ``review_id``, the gates evaluated now
    over the company and the phone the card shows; None when the row, its
    candidate or its company is gone."""
    review = await session.get(LeadReview, review_id, populate_existing=True)
    if review is None:
        return None
    candidate = await session.get(
        LeadCandidate, review.candidate_id, populate_existing=True
    )
    if candidate is None:
        return None
    try:
        view = await candidates.load_view(session, candidate.id, now=now)
    except LookupError:
        return None
    company = await session.get(Company, candidate.company_id)
    lead = await push_gate.lead_contact(session, candidate)
    tails = [lead.phone_tail.strip()] if lead is not None and lead.phone_tail else []
    gate = await gates.evaluate(session, candidate.company_id, now=now, tails=tails)
    handoff = candidates.is_handoff(candidate.score_breakdown)
    verdict = company.verdict if company is not None else None
    reasons = verdict.get("reasons") if isinstance(verdict, dict) else None
    refused = verdict is None and await verdict_service.refused_today(session, now=now)
    return ReviewView(
        review_id=review.id,
        candidate=view,
        gate=gate,
        segment=company.segment if company is not None else None,
        phone_kind=lead.phone_kind if lead is not None else None,
        shared_phone=bool(lead is not None and lead.shared_phone),
        handoff=handoff,
        handoff_username=(
            await _handoff_username(session, candidate) if handoff else None
        ),
        phone_usable=push_gate.usable_lead_phone(lead),
        verdict_reasons=tuple(str(r) for r in reasons or () if isinstance(r, str)),
        verdict_small=validators.verdict_small(verdict),
        verdict_refused=refused,
    )


async def card_layout(session: AsyncSession, review: LeadReview) -> CardLayout:
    """The keyboard a card was drawn with: a manager card's; the handoff
    variant's when the candidate carries ``gates.no_phone`` and no usable
    lead phone (``ReviewView.handoff_card``, ``render_review``'s own rule);
    else the review's."""
    if review.kind == "manager":
        return "manager"
    candidate = await session.get(LeadCandidate, review.candidate_id)
    if candidate is None:
        return "review"
    lead = await push_gate.lead_contact(session, candidate)
    if candidates.is_handoff(candidate.score_breakdown) and not (
        push_gate.usable_lead_phone(lead)
    ):
        return "handoff"
    return "review"


# 02 §2.9: an owner card nobody answered holds its candidate 7 days; a card
# that cannot be shown at all is held as long, then re-scored like any hold.
CARD_REFUSED_HOLD: Final = timedelta(days=7)


async def set_aside(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    code: str,
    gate: GateResult | None,
    *,
    now: datetime,
) -> tuple[str, ...]:
    """A card ``render_review`` refused leaves the queue — left in
    ``review`` it would head every ``/lidlar`` (score desc) and keep the good
    cards behind it for ever. First the score job's own walk
    (``candidates.rescore`` + ``apply_score`` with the gate the card was
    judged by): a failed gate, or a ``review`` row without a usable phone,
    leaves ``review`` the way it would have entered it. A refusal that walk
    does not move — a quote no longer citable (its signals expired), a
    handoff with nobody to write to, a card too long — holds the candidate
    ``CARD_REFUSED_HOLD``. The company and the candidate are locked in the
    push gate's order (companies before lead_candidates, ``lock_for_push``);
    the caller holds no review row (the claim ended before). The walk, or
    ``()`` when the candidate is gone or has left ``review`` meanwhile."""
    candidate = await push_gate.lock_for_push(session, candidate_id)
    if candidate is None or candidate.state != "review":
        return ()
    result = await candidates.rescore(session, candidate, now=now)
    walk = await candidates.apply_score(
        session, candidate, result, now=now, auto_push=False, gate=gate
    )
    if candidate.state == "review":
        await candidates.transition(
            session, candidate, "hold", now=now, hold_until=now + CARD_REFUSED_HOLD
        )
        walk = (*walk, "hold")
    log.info(
        "card set aside",
        extra={"candidate_id": str(candidate_id), "code": code, "walk": list(walk)},
    )
    return walk


def _owner_pending() -> sa.ColumnElement[bool]:
    return sa.and_(
        LeadReview.kind == "owner",
        LeadReview.state == "pending",
        LeadCandidate.state == "review",
    )


async def claim_owner_review(
    session: AsyncSession, *, skip: Collection[int] = ()
) -> int | None:
    """The next ``pending`` owner card of a candidate in ``review``, in 06
    §1.6's burst order (score desc, then the oldest card first), claimed for
    the caller's transaction: ``FOR UPDATE OF lead_reviews SKIP LOCKED``. A
    second ``/lidlar`` (a double tap) or ``review_cards_job`` running
    meanwhile takes the next card, never this one, so a card goes out once;
    the claim ends with the transaction (the commit after ``mark_asked``, or
    a rollback when Telegram did not take the card). ``skip``: the cards
    this run has handled already. None when nothing waits."""
    stmt = (
        sa.select(LeadReview.id)
        .join(LeadCandidate, LeadCandidate.id == LeadReview.candidate_id)
        .where(_owner_pending())
    )
    if skip:
        stmt = stmt.where(LeadReview.id.not_in(sorted(skip)))
    found: int | None = await session.scalar(
        stmt.order_by(
            LeadCandidate.score.desc().nulls_last(),
            LeadReview.created_at,
            LeadReview.id,
        )
        .limit(1)
        .with_for_update(of=LeadReview, skip_locked=True)
    )
    return found


async def pending_owner_count(session: AsyncSession) -> int:
    found = await session.scalar(
        sa.select(sa.func.count())
        .select_from(LeadReview)
        .join(LeadCandidate, LeadCandidate.id == LeadReview.candidate_id)
        .where(_owner_pending())
    )
    return int(found or 0)


async def mark_asked(
    session: AsyncSession,
    review_id: int,
    *,
    chat_id: int,
    message_id: int,
    card_hash: str,
    now: datetime,
) -> bool:
    """``pending → asked`` once the card is out (06 §1.2 /lidlar): the
    message it went out as, its hash and ``expires_at = now +
    REVIEW_EXPIRY_HOURS``. True when the row was still pending."""
    result = await session.execute(
        sa.update(LeadReview)
        .where(LeadReview.id == review_id, LeadReview.state == "pending")
        .values(
            state="asked",
            card_chat_id=chat_id,
            card_message_id=message_id,
            card_hash=card_hash,
            asked_at=now,
            expires_at=now + timedelta(hours=settings.review_expiry_hours),
        )
        .returning(LeadReview.id)
    )
    return result.first() is not None


async def live_review(session: AsyncSession, review_id: int) -> LeadReview | None:
    """The card a tap is on, while it still waits for an answer (``asked``)."""
    review = await session.get(LeadReview, review_id, populate_existing=True)
    if review is None or review.state != "asked":
        return None
    return review


@dataclass(frozen=True, slots=True)
class LidlarHolat:
    """``/lidlar holat``: candidates per state and the oldest review."""

    counts: Mapping[str, int]
    review_oldest_at: datetime | None


async def lidlar_holat(session: AsyncSession) -> LidlarHolat:
    counts = {
        str(state): int(n)
        for state, n in (
            await session.execute(
                sa.select(LeadCandidate.state, sa.func.count()).group_by(
                    LeadCandidate.state
                )
            )
        ).all()
    }
    shown = await session.scalar(
        sa.select(
            sa.func.min(sa.func.coalesce(LeadReview.asked_at, LeadReview.created_at))
        )
        .join(LeadCandidate, LeadCandidate.id == LeadReview.candidate_id)
        .where(
            LeadCandidate.state == "review",
            LeadReview.kind == "owner",
            LeadReview.state.in_(("pending", "asked")),
        )
    )
    oldest = shown
    if oldest is None:
        oldest = await session.scalar(
            sa.select(sa.func.min(LeadCandidate.updated_at)).where(
                LeadCandidate.state == "review"
            )
        )
    return LidlarHolat(counts=counts, review_oldest_at=oldest)


async def merge_targets(
    session: AsyncSession, review: LeadReview
) -> list[tuple[uuid.UUID, str]]:
    """The 🔀 menu: ≤ 5 targets of the resolver's own function."""
    candidate = await session.get(LeadCandidate, review.candidate_id)
    if candidate is None:
        return []
    found = await companies.suggest_merge_targets(session, candidate.company_id)
    return [(c.id, c.display_name) for c in found]


# --- /qidir (06 §1.2) ----------------------------------------------------------------

OUTCOME_UZ: Final[Mapping[str, str]] = {
    "won": "yutildi (mijoz)",
    "contacted_open": "bogʻlanildi, ochiq",
    "open_untouched": "ochiq, tegilmagan",
    "stale_open": "ochiq, eskirgan",
    "lost_no_contact": "yoʻqotildi — «Qayta qilinmasin»",
    "lost_price": "yoʻqotildi — narx",
    "lost_not_importer": "yoʻqotildi — import qilmaydi",
    "lost_small": "yoʻqotildi — kichik yuk",
    "lost_wrong_number": "yoʻqotildi — notoʻgʻri raqam",
    "lost_competitor": "yoʻqotildi — kargo/raqobatchi",
    "lost_other": "yoʻqotildi — boshqa sabab",
    "landed_client": "ERPda mijoz",
    "landed_joined": "ERPda bor lidga qoʻshildi",
    "dropped_capped": "ERP qabul qilmadi — chegara",
    "dropped_suppressed": "ERP qabul qilmadi — bostirilgan",
    "dropped_other": "ERP qabul qilmadi",
}
SUPPRESSION_KIND_UZ: Final[Mapping[str, str]] = {
    "legal": "huquqiy soʻrov",
    "dnc": "yozilmasin (DNC)",
    "client": "mijoz",
    "lost": "«Qayta qilinmasin»",
    "bounced": "notoʻgʻri raqam",
    "competitor": "raqobatchi",
    "manager_veto": "menejer rad etdi",
    "b2c": "B2C",
    "cooldown": "kutish",
}
STATE_UZ: Final[Mapping[str, str]] = {
    "new": "yangi",
    "active": "faol",
    "excluded": "chiqarilgan",
    "client": "mijoz",
    "suppressed": "bostirilgan",
    "merged": "birlashtirilgan",
    "purged": "oʻchirilgan",
}


@dataclass(frozen=True, slots=True)
class LastPush:
    external_id: str
    sent_at: datetime
    status: str
    label: str | None


@dataclass(frozen=True, slots=True)
class CompanyCard:
    """``/qidir``'s company-tier view — no person, no phone."""

    company_id: uuid.UUID
    display_name: str
    legal_form: str | None
    city: str | None
    stir: str | None
    registry_status: str
    website_domain: str | None
    telegram: str | None
    instagram: str | None
    state: str
    best_score: int | None
    candidate_state: str | None
    candidate_id: uuid.UUID | None
    last_push: LastPush | None
    hit: Hit | None


def render_company(card: CompanyCard, *, state_words: Mapping[str, str]) -> str:
    """One ``/qidir`` answer (06 §1.2): company-tier fields, state, best
    score, the candidate, the last push with its outcome label, and the
    suppression that holds it. ``state_words`` are the bot's candidate
    state words."""
    head = esc(card.display_name)
    if (
        card.legal_form
        and card.legal_form.lower() not in card.display_name.lower().split()
    ):
        head += f" ({esc(card.legal_form)})"
    facts = [head]
    if card.city:
        facts.append(esc(card.city))
    if card.stir:
        facts.append(f"STIR {esc(card.stir)}")
    if card.registry_status != "unknown":
        facts.append(esc(gates.REGISTRY_UZ[card.registry_status]))
    lines = ["🏢 " + " · ".join(facts)]
    handles = [
        f"@{esc(card.telegram)}" if card.telegram else "",
        f"IG {esc(card.instagram)}" if card.instagram else "",
        esc(card.website_domain) if card.website_domain else "",
    ]
    if any(handles):
        lines.append("🔗 " + " · ".join(h for h in handles if h))
    state = [f"Holat: {esc(STATE_UZ.get(card.state, card.state))}"]
    if card.best_score is not None:
        state.append(f"eng yaxshi ball {card.best_score}")
    if card.candidate_state is not None:
        word = state_words.get(card.candidate_state, card.candidate_state)
        state.append(f"nomzod: {esc(word)}")
    lines.append(" · ".join(state))
    if card.candidate_id is not None:
        lines.append(f"<code>{card.candidate_id}</code>")
    if card.last_push is not None:
        push = card.last_push
        label = OUTCOME_UZ.get(push.label or "", "ERP javobi hali yoʻq")
        lines.append(
            f"📤 Oxirgi push: {day(push.sent_at)} · "
            f"{esc(crm_push.PUSH_STATUS_UZ.get(push.status, push.status))} · {esc(label)}"
        )
    if card.hit is not None:
        at = card.hit.created_at
        when = f", {day(at)}" if at is not None else ""
        kind = SUPPRESSION_KIND_UZ.get(card.hit.kind, card.hit.kind)
        lines.append(f"🚫 Bostirilgan: {esc(kind)} ({esc(card.hit.source)}{when})")
    return "\n".join(lines)


async def live_roots(session: AsyncSession, query: str) -> list[uuid.UUID]:
    """``find_company_ids``' hits as the roots a purge names now
    (``purge.subject_root``), each once — ``/unut``'s lookup. A purged company
    is left out unless a purge of it stopped part-way (``purge.unfinished``:
    its rows are still there), so ``/unut`` can finish that job."""
    roots, _ = await purge_roots(session, query)
    return roots


async def purge_roots(session: AsyncSession, query: str) -> tuple[list[uuid.UUID], bool]:
    """``live_roots`` and whether the name match alone found them (``lookup``)."""
    hits, by_name = await lookup(session, query)
    roots: list[uuid.UUID] = []
    for company_id in hits:
        found = await session.get(Company, company_id)
        if found is None:
            continue
        root = await purge.subject_root(session, found)
        if root.id in roots:
            continue
        if root.state == "purged" and not await purge.unfinished(session, root.id):
            continue
        roots.append(root.id)
    return roots, by_name


async def company_card(
    session: AsyncSession, company_id: uuid.UUID, *, now: datetime
) -> CompanyCard | None:
    company = await session.get(Company, company_id, populate_existing=True)
    if company is None:
        return None
    company = await companies.root_company(session, company)
    lineage = sorted(await companies.company_lineage(session, company.id))
    candidate = await session.scalar(
        sa.select(LeadCandidate)
        .where(LeadCandidate.company_id.in_(lineage))
        .order_by(LeadCandidate.created_at.desc(), LeadCandidate.id)
        .limit(1)
    )
    push = await session.scalar(
        sa.select(PushLog)
        .where(PushLog.company_id.in_(lineage))
        .order_by(PushLog.sent_at.desc(), PushLog.id.desc())
        .limit(1)
    )
    last: LastPush | None = None
    if push is not None:
        outcome = await session.get(Outcome, push.external_id)
        last = LastPush(
            push.external_id,
            push.sent_at,
            push.status,
            outcome.label if outcome is not None else None,
        )
    return CompanyCard(
        company_id=company.id,
        display_name=company.display_name,
        legal_form=company.legal_form,
        city=company.city,
        stir=company.stir.strip() if company.stir else None,
        registry_status=company.registry_status,
        website_domain=company.website_domain,
        telegram=company.telegram_channel,
        instagram=company.instagram,
        state=company.state,
        best_score=company.best_score,
        candidate_state=candidate.state if candidate is not None else None,
        candidate_id=candidate.id if candidate is not None else None,
        last_push=last,
        hit=await suppression.check_company(session, company.id, now=now),
    )


def parse_uuid(text: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


async def find_company_ids(session: AsyncSession, query: str) -> list[uuid.UUID]:
    """What ``/qidir`` and ``/unut`` look a typed key up by (06 §1.2),
    read-only, in the order found: a uuid (a company or a candidate), a phone
    or tail-9, an ``@username``/``t.me`` link, a STIR, a domain, else a
    trigram name match. A bare nine-digit number is tried as a STIR and as a
    tail. Not yet roots, not yet unique."""
    found, _ = await lookup(session, query)
    return found


async def lookup(session: AsyncSession, query: str) -> tuple[list[uuid.UUID], bool]:
    """``find_company_ids``' hits and whether they came from the name match
    alone (no exact key answered): a guess, which ``/unut`` says on its plan
    card before anything is deleted for good."""
    text = query.strip()
    found: list[uuid.UUID] = []
    if (as_id := parse_uuid(text)) is not None:
        owner = await session.scalar(
            sa.select(LeadCandidate.company_id).where(LeadCandidate.id == as_id)
        )
        found.append(owner or as_id)
    else:
        digits = "".join(ch for ch in text if ch.isascii() and ch.isdigit())
        tail: str | None = None
        if digits and len(digits) >= 9 and not any(ch.isalpha() for ch in text):
            phone = normalise.phone_e164(text)
            tail = (
                keys.phone_tail(phone) if phone is not None else keys.phone_tail(digits)
            )
        stir = keys.stir_key(digits) if len(digits) == 9 and digits == text else None
        handle = (
            normalise.telegram_key(text)
            if text.startswith("@") or "t.me/" in text.lower()
            else None
        )
        domain = keys.domain_key(text) if "." in text and " " not in text else None
        if stir is not None:
            found += await session.scalars(
                sa.select(Company.id).where(Company.stir == stir)
            )
        if tail is not None:
            found += [c.id for c in await companies.find_by_phone_tail(session, tail)]
            # A purged firm whose purge stopped before its contacts went
            # (find_by_phone_tail names live firms only): /unut finishes it.
            found += await session.scalars(
                sa.select(Contact.company_id)
                .join(Company, Company.id == Contact.company_id)
                .where(
                    Contact.phone_tail == tail,
                    Contact.anonymised_at.is_(None),
                    Company.state == "purged",
                )
                .order_by(Contact.company_id)
            )
        if handle is not None:
            found += await session.scalars(
                sa.union(
                    sa.select(Company.id).where(Company.telegram_channel == handle),
                    sa.select(Contact.company_id).where(
                        Contact.telegram_username == handle,
                        Contact.anonymised_at.is_(None),
                    ),
                )
            )
        if domain is not None and tail is None:
            found += await session.scalars(
                sa.select(Company.id).where(Company.website_domain == domain)
            )
        if not found and tail is None and stir is None and handle is None:
            found += [cid for cid, _ in await companies.find_companies(session, text)]
            return found, True
    return found, False


async def search(
    session: AsyncSession,
    query: str,
    *,
    now: datetime,
    limit: int = companies.NAME_MATCHES,
) -> list[CompanyCard]:
    """``/qidir``'s lookup (06 §1.2), read-only: ``find_company_ids``' hits
    as company cards (≤ ``limit``), roots only, each once."""
    cards: list[CompanyCard] = []
    for company_id in await find_company_ids(session, query):
        card = await company_card(session, company_id, now=now)
        if card is not None and card.company_id not in {c.company_id for c in cards}:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


# --- /holat and /xarajat (06 §1.4) ---------------------------------------------------


def money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01'))}"


def pct(spent: Decimal, cap: Decimal) -> int:
    return int(spent * 100 / cap) if cap > 0 else 0


def _age(moment: datetime | None, now: datetime) -> str:
    return health.age_label(now - moment) if moment is not None else "—"


def _process(component: health.Component | None, now: datetime) -> str:
    if component is None or component.last_seen_at is None:
        return "❌ yoʻq"
    if component.disabled:
        return "⏸ oʻchiq"
    if component.stale:
        return f"❌ {_age(component.last_seen_at, now)}"
    return f"✅ ({_age(component.last_seen_at, now)})"


def _source_icon(source: health.SourceState, now: datetime) -> str:
    if not source.enabled:
        return "⛔"
    held = [t for t in (source.paused_until, source.breaker_until) if t is not None]
    return "⏸" if any(t > now for t in held) else "✅"


def render_holat(status: health.Status, found: Sequence[health.Problem]) -> str:
    """06 §1.4's ``/holat``: processes, database, disk, backup, Anthropic,
    the ERP, the queues, today's pushes and cards against their caps,
    spend, the enabled sources and the open problems (each its own text,
    remedy included — ``health.problems``)."""
    now = status.now
    lines = [f"🩺 Agent holati — {now.astimezone(settings.tz):%d.%m.%Y %H:%M}"]
    if not status.db_ok:
        lines.append("Baza ❌")
    else:
        userbot = status.components.get("userbot")
        reader = _process(userbot, now)
        if userbot is not None and not userbot.stale and not userbot.disabled:
            reader = (
                f"✅ ({status.chats_on} chat, oxirgi xabar "
                f"{_age(status.last_interaction_at, now)})"
            )
        lines.append(
            "Jarayonlar: bot ✅ · worker "
            f"{_process(status.components.get('worker'), now)} · oʻquvchi {reader}"
        )
        free = (status.disk_free_bytes or 0) / 1000**3
        disk = f"Disk {free:.1f} GB boʻsh"
        if status.evidence_bytes is not None:
            disk += f" ({status.evidence_bytes / 1000**3:.1f} GB dalil)"
        info = status.backup
        if not info.configured:
            backup = "Zaxira sozlanmagan ⚠️"
        elif info.created_at is None:
            backup = "Zaxira yoʻq ⚠️"
        else:
            mark = "✅ Telegramga yetdi" if info.sent else "⚠️"
            backup = f"Zaxira {_age(info.created_at, now)} {mark}"
        lines.append(f"Baza ✅ · {disk} · {backup}")
        supp = status.suppression
        full = _age(supp.full_at, now) if supp is not None else "—"
        delta = _age(supp.delta_at, now) if supp is not None else "—"
        until = status.erp_breaker_until
        breaker = (
            until.astimezone(settings.tz).strftime("%d.%m %H:%M")
            if until is not None and until > now
            else "yoʻq"
        )
        lines.append(
            f"Anthropic: oxirgi muvaffaqiyat {_age(status.anthropic.last_ok_at, now)} · "
            f"ERP: oxirgi push {_age(status.queues.last_push_at, now)} · "
            f"roʻyxat toʻliq {full} / farq {delta} · breaker {breaker}"
        )
        q = status.queues
        lines.append(
            f"Navbat: koʻrib chiqish {q.review} (eng eskisi "
            f"{_age(q.review_oldest_at, now)}) · boyitish {q.enriching} · "
            f"kutish {q.hold} · push navbati {q.approved}"
        )
        spend = status.spend
        lines.append(
            f"Bugun: push {q.pushes_today}/{settings.push_daily_cap} · karta "
            f"{q.cards_today}/{settings.review_daily_cap} · xarajat "
            f"{money(spend.day_usd)}/{money(settings.daily_cost_cap_usd)} · oy "
            f"{money(spend.month_usd)}/{money(settings.monthly_cost_cap_usd)}"
        )
        if status.paused:
            lines.append("⏸ Agent toʻxtatilgan — /yoq")
        shown = [s for s in status.sources if s.enabled]
        if shown:
            lines.append("Manbalar:")
            lines += [
                f"  {esc(s.title_uz)} {_source_icon(s, now)} {_age(s.last_run_at, now)}"
                for s in shown
            ]
    if found:
        lines.append(f"Muammolar: {len(found)} ta:")
        lines += [f"  {p.text}" for p in found]
    else:
        lines.append("Muammolar: yoʻq")
    return "\n".join(lines)


# 06 §1.4's /xarajat words for each operation.
OPERATION_UZ: Final[Mapping[str, str]] = {
    "classify": "tasnif",
    "verdict": "xulosa",
    "company_verdict": "tungi batch",
    "draft": "qoralama",
    "enrich_search": "kontakt qidiruv",
}
SEARCH_UZ: Final[tuple[tuple[str, str], ...]] = (
    ("serp_brave", "Brave"),
    ("serp_yandex_night", "Yandex"),
    ("serp_searxng", "SearXNG"),
    ("serp_ddg", "DuckDuckGo"),
    ("places_text_ids", "Places"),
    ("places_details_contact", "Places kontakt"),
    ("twogis_api", "2GIS"),
)


def render_xarajat(report: costs.SpendReport) -> str:
    """06 §1.4's ``/xarajat`` over ``costs.report``: today and the month
    against their caps, the refusals (the ``refused:<code>`` rows, counted
    per code), each family by operation and the most expensive companies."""
    day_total = report.total(report.day)
    month_total = report.total(report.month)
    refused = report.refusals(report.day)
    lines = [
        f"💸 Xarajat — bugun {money(day_total)} ({pct(day_total, report.daily_cap)}% / "
        f"{money(report.daily_cap)}) · oy {money(month_total)} "
        f"({pct(month_total, report.monthly_cap)}% / {money(report.monthly_cap)}) · "
        f"rad etilgan chaqiruvlar {refused}"
    ]

    def op(name: str) -> costs.OpLine:
        return report.day.get(name, costs.OpLine())

    anthropic = report.total(report.day, "anthropic")
    lines.append(
        f"Anthropic {money(anthropic)}: "
        + " · ".join(
            f"{word} {op(name).calls} ta {money(op(name).usd)}"
            for name, word in OPERATION_UZ.items()
        )
    )
    search_usd = report.total(report.day, "serp") + report.total(report.day, "places")
    lines.append(
        f"Qidiruv {money(search_usd)}: "
        + " · ".join(
            f"{word} {op(name).calls}"
            + (" (bepul)" if name == "serp_yandex_night" else "")
            for name, word in SEARCH_UZ
        )
    )
    lines.append(
        f"Apify {money(report.total(report.day, 'apify'))} · "
        f"proksi {money(report.total(report.day, 'proxy'))}"
    )
    codes = report.refusals_by_code(report.day)
    if codes:
        lines.append(
            "Rad etilganlar: "
            + " · ".join(f"{esc(code)} {n}" for code, n in sorted(codes.items()))
        )
    if report.top_companies:
        lines.append(
            "Eng qimmat kompaniyalar: "
            + " · ".join(f"{esc(t.name)} {money(t.usd)}" for t in report.top_companies)
        )
    return "\n".join(lines)


__all__ = [
    "BAND_UZ",
    "CARD_GATES",
    "CARD_MAX",
    "CARD_REFUSED_HOLD",
    "ERP_FIRST_UZ",
    "NO_PRICE_UZ",
    "OUTCOME_UZ",
    "SEGMENT_UZ",
    "SHARED_PHONE_UZ",
    "SUPPRESSION_KIND_UZ",
    "Card",
    "CardLayout",
    "CardRefused",
    "CompanyCard",
    "LastPush",
    "LidlarHolat",
    "ManagerView",
    "ReviewView",
    "card_hash",
    "card_layout",
    "claim_owner_review",
    "company_card",
    "find_company_ids",
    "lidlar_holat",
    "live_review",
    "live_roots",
    "lookup",
    "mark_asked",
    "merge_targets",
    "money",
    "parse_uuid",
    "pending_owner_count",
    "phone_shown",
    "purge_roots",
    "render_company",
    "render_holat",
    "render_manager",
    "render_review",
    "render_xarajat",
    "review_view",
    "search",
    "segment_label",
    "set_aside",
    "volume_label",
]
