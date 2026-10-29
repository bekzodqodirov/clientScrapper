"""The opener a manager sends: 04's draft, or the playbook template (04 §4.5).

``ensure_opener`` gives a candidate its first message once, in the caller's
transaction, and stores it where the card and the ERP note read it —
``score_breakdown.opener`` (``candidates.load_view`` → ``CandidateView.opener``
→ the manager card's «✍️ Taklif» and the note's «NIMA DEYISH»), with
``score_breakdown.opener_meta`` saying how it was made; a re-score carries
both (``candidates._carried``).

1. A ``hot`` candidate (``paid``) is drafted: ``extraction.call(op='draft')``
   — masked, under ``costs.ask``, a ``model_jobs`` row — with the company's
   name, segment, goods (≤ 3), the cited evidence's source and date, whether
   a contact person is known (``[Ism]``: no name is ever sent), the language
   of the evidence, and the segment's playbook skeleton
   (``playbook.skeleton``). The answer goes through
   ``validators.draft_opener``: hygiene, the redline gate, the length.
2. Any flag, a refusal by the model, a budget refusal, an API failure, no
   client — and every candidate that is not ``hot`` — gets the playbook
   template instead (``playbook.opener``, which passes the same gate by
   construction). A flagged draft is never stored, so it is never rendered.

A stored opener is made again only when ``due`` says so: a template filled
for another segment than the company's now (the verdict that sets the
segment may land after the opener was made), or made for a candidate that
was not hot and now is. A stored draft stands: it was paid for.

Who asks: ``verdict_job`` (``ensure_openers``, every 10 minutes), the manager
card before its claim (``reviews.ensure_manager_opener``), and ``crm_push_job``
before each push (``crm_push.opener_first``: the template only, never a paid
draft, so a lead pushed before the next verdict run still carries one).

The model call runs with no row lock held; the write takes the company and
then the candidate (the lock order every writer of the two keeps,
``services/candidates``) and keeps an opener another run stored meanwhile.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import ChatMonitor, Company, LeadCandidate, ModelJob
from clientscrapper.services import candidates, crm_push, extraction, playbook, validators
from clientscrapper.services.candidates import CandidateView, SignalEvidence
from clientscrapper.services.prompts import DRAFT_PROMPT_VERSION, DRAFT_SYSTEM
from clientscrapper.services.pseudonyms import Pseudonymised, pseudonymise
from clientscrapper.services.redlines import Lang
from clientscrapper.services.schemas import DraftOpener

log = logging.getLogger(__name__)

OP: Final = "draft"
OPENER_KEY: Final = "opener"
META_KEY: Final = "opener_meta"
NO_VALUE: Final = "-"
NOT_HOT: Final = "not_hot"
# The forwarders' names the gate refuses beside its regex (the seeds').
COMPETITOR_NAMES_MAX: Final = 500
Source = Literal["draft", "template"]


@dataclass(frozen=True, slots=True)
class Made:
    """An opener and how it was made: ``source`` draft or template; ``why``
    the template's reason (``not_hot``, ``refused:<code>``, ``refusal``,
    ``flagged``, ``api:<class>``, ``unavailable``…); ``flags`` the draft's
    redlines when it was refused for them."""

    text: str
    lang: Lang
    segment: str
    source: Source
    why: str | None = None
    flags: tuple[str, ...] = ()
    model_job_id: int | None = None

    def meta(self, now: datetime) -> dict[str, Any]:
        return {
            "source": self.source,
            "segment": self.segment,
            "lang": self.lang,
            "why": self.why,
            "flags": list(self.flags),
            "model_job_id": self.model_job_id,
            "at": now.isoformat(timespec="seconds"),
        }


def stored(breakdown: Mapping[str, Any] | None) -> str | None:
    """The opener a candidate already has."""
    value = (breakdown or {}).get(OPENER_KEY)
    return value if isinstance(value, str) and value.strip() else None


def _template_meta(breakdown: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    meta = (breakdown or {}).get(META_KEY)
    if isinstance(meta, Mapping) and meta.get("source") == "template":
        return meta
    return None


def due(breakdown: Mapping[str, Any] | None, *, segment: str | None, hot: bool) -> bool:
    """Is an opener to be made — or made again? None stored: yes. A stored
    template is remade when it was filled for another segment than the
    company's now (the verdict that sets the segment may land after it:
    «A» bazaar facts on a «D» construction importer), or made because the
    candidate was not hot and now it is (a hot one is drafted, 04 §4.1). A
    template costs nothing to remake; a stored draft was paid for and
    stands."""
    if stored(breakdown) is None:
        return True
    meta = _template_meta(breakdown)
    if meta is None:
        return False
    if meta.get("segment") != playbook.template_segment(segment):
        return True
    return hot and meta.get("why") == NOT_HOT


def _cited(view: CandidateView) -> SignalEvidence | None:
    try:
        signal, _ = crm_push.cited(view)
    except crm_push.PayloadError:
        return next((s for s in view.signals if s.points > 0), None)
    return signal


def user_content(
    *,
    company: str,
    segment: str | None,
    goods: Sequence[str],
    source: str,
    seen_on: datetime | None,
    person: bool,
    lang: Lang,
    names: Sequence[str] = (),
) -> Pseudonymised:
    """04 §4.5's input. The company's name is the one field that may carry
    a person's (or a handle), and it alone is pseudonymised — the fixed
    lines are the code's own words, and run through the pseudonymiser a
    «в Telegram-группе» would be read as a messenger id. No contact name is
    sent — ``CONTACT: [Ism]`` says one is known, and the manager writes it.
    ``extraction`` still refuses the whole text unmasked."""
    seg = playbook.template_segment(segment)
    when = seen_on.astimezone(settings.tz).date().isoformat() if seen_on else NO_VALUE
    name = pseudonymise(company, names=list(names))
    lines = (
        f"COMPANY: {name.text}",
        f"SEGMENT: {seg}",
        f"GOODS: {', '.join(goods) or NO_VALUE}",
        f"SOURCE: {source}, {when}",
        f"CONTACT: {validators.NAME_PLACEHOLDER if person else NO_VALUE}",
        f"LANGUAGE: {lang}",
        "SKELETON:",
        playbook.skeleton(seg, lang),
    )
    return Pseudonymised("\n".join(lines), dict(name.reverse))


async def competitor_names(session: AsyncSession) -> list[str]:
    """The seeds' forwarder channels, by title and username."""
    rows = await session.execute(
        sa.select(ChatMonitor.title, ChatMonitor.username)
        .where(ChatMonitor.role == "competitor")
        .order_by(ChatMonitor.id)
        .limit(COMPETITOR_NAMES_MAX)
    )
    return [str(v) for row in rows.all() for v in row if v]


async def make(
    session: AsyncSession,
    view: CandidateView,
    *,
    segment: str | None,
    paid: bool,
    now: datetime,
) -> Made:
    """The opener ``view`` gets (the module docstring), not yet stored."""
    cited = _cited(view)
    family = cited.source_family if cited is not None else None
    lang = playbook.lang_of(cited.excerpt if cited is not None else None)
    person = view.person is not None
    goods = playbook.goods_words(view.goods, lang)
    template = playbook.opener(
        segment, lang, goods=view.goods, family=family, person=person
    )

    def fallback(why: str, *, flags: Sequence[str] = (), job: int | None = None) -> Made:
        return Made(
            template.text,
            lang,
            template.segment,
            "template",
            why=why,
            flags=tuple(flags),
            model_job_id=job,
        )

    if not paid:
        return fallback(NOT_HOT)
    hooks = playbook.hooks(goods, family)
    competitors = await competitor_names(session)
    user = user_content(
        company=view.display_name,
        segment=segment,
        goods=goods,
        source=playbook.source_word(family, lang),
        seen_on=(cited.published_at or cited.fetched_at) if cited else None,
        person=person,
        lang=lang,
        names=[view.person.full_name] if view.person and view.person.full_name else (),
    )

    def validate(draft: DraftOpener) -> validators.Validated:
        return validators.draft_opener(
            draft, lang=lang, hooks=hooks, competitors=competitors
        )

    result = await extraction.call(
        session,
        op=OP,
        schema=DraftOpener,
        system=DRAFT_SYSTEM,
        user=user,
        now=now,
        company_id=view.company_id,
        evidence_ids=[cited.evidence_id] if cited is not None else (),
        prompt_version=DRAFT_PROMPT_VERSION,
        validate=validate,
    )
    if not result.ok or result.validated is None:
        return fallback(result.error or "failed", job=result.job_id)
    answer = result.validated.result
    flags = [str(f) for f in answer.get("flags") or ()]
    text = answer.get("text")
    if flags or not isinstance(text, str):
        log.info(
            "draft opener refused by the redlines",
            extra={"model_job_id": result.job_id, "flags": flags},
        )
        return fallback("flagged", flags=flags, job=result.job_id)
    return Made(text, lang, template.segment, "draft", model_job_id=result.job_id)


# A company whose candidate gets no opener: purged (its data is gone) or
# merged (the root's candidate is the one a manager reads).
NO_OPENER_STATES: Final = frozenset({"purged", "merged"})
# The advisory-lock namespace of a candidate's paid draft.
DRAFT_LOCK: Final = "opener_draft"


async def _gone(session: AsyncSession, candidate: LeadCandidate) -> bool:
    """The candidate left the funnel, or its company was purged or merged."""
    if candidate.state in candidates.TERMINAL_STATES:
        return True
    state = await session.scalar(
        sa.select(Company.state)
        .where(Company.id == candidate.company_id)
        .execution_options(populate_existing=True)
    )
    return state is None or state in NO_OPENER_STATES


async def _drafting(session: AsyncSession, candidate_id: uuid.UUID) -> bool:
    """This transaction's claim on the candidate's paid draft
    (``pg_try_advisory_xact_lock``, released at its end); False when another
    transaction holds it."""
    key = sa.func.hashtextextended(f"{DRAFT_LOCK}:{candidate_id}", 0)
    return bool(await session.scalar(sa.select(sa.func.pg_try_advisory_xact_lock(key))))


async def forget_job(session: AsyncSession, job_id: int | None) -> None:
    """A draft's request, reverse map and answer dropped: the opener it was
    for will not be stored."""
    if job_id is None:
        return
    await session.execute(
        sa.update(ModelJob)
        .where(ModelJob.id == job_id)
        .values(request=sa.null(), pseudonyms=sa.null(), result=sa.null())
    )


async def ensure_opener(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    now: datetime,
    paid: bool | None = None,
) -> str | None:
    """The candidate's opener — made once and stored (the module docstring)
    in the caller's transaction, and made again only when ``due`` says so (a
    template for another segment, or for a candidate that was not hot).
    ``paid`` defaults to «the candidate is hot». None when the candidate or
    its company is gone."""
    candidate = await session.get(LeadCandidate, candidate_id, populate_existing=True)
    if candidate is None:
        return None
    if await _gone(session, candidate):
        # A purged firm, or a candidate that left the funnel: no draft is
        # paid for and nothing is written (seam review wave 4).
        return None
    have = stored(candidate.score_breakdown)
    if have is not None and _template_meta(candidate.score_breakdown) is None:
        return have
    try:
        view = await candidates.load_view(session, candidate_id, now=now)
    except LookupError:
        return have
    company = await session.get(Company, view.company_id)
    segment = company.segment if company is not None else None
    if paid is None:
        paid = candidate.tier == "hot"
    if not due(candidate.score_breakdown, segment=segment, hot=paid):
        return have
    if paid and not await _drafting(session, candidate_id):
        # Another run is paying for this candidate's draft right now (the
        # card job beside /lidlar, two cards of one lead): its draft will
        # stand; this one is not bought twice (seam review wave 4).
        return have
    made = await make(session, view, segment=segment, paid=paid, now=now)
    # The write: the company, then the candidate (services/candidates' order).
    await session.get(
        Company,
        candidate.company_id,
        with_for_update={"key_share": True},
        populate_existing=True,
    )
    locked = await session.get(
        LeadCandidate,
        candidate_id,
        with_for_update={"key_share": True},
        populate_existing=True,
    )
    if locked is None:
        return None
    if await _gone(session, locked):
        # Purged or closed while the draft was being made: the company's
        # row now reads so (the purge held it; this lock waited). The draft's
        # own request goes too — the purge could not see it uncommitted.
        await forget_job(session, made.model_job_id)
        return None
    if not due(locked.score_breakdown, segment=segment, hot=paid):
        # Another run stored one meanwhile: it stands.
        return stored(locked.score_breakdown)
    locked.score_breakdown = {
        **dict(locked.score_breakdown or {}),
        OPENER_KEY: made.text,
        META_KEY: made.meta(now),
    }
    await session.flush()
    log.info(
        "opener stored",
        extra={
            "candidate_id": str(candidate_id),
            "source": made.source,
            "why": made.why,
        },
    )
    return made.text


__all__ = [
    "META_KEY",
    "NOT_HOT",
    "NO_OPENER_STATES",
    "OPENER_KEY",
    "Made",
    "competitor_names",
    "due",
    "ensure_opener",
    "forget_job",
    "make",
    "stored",
    "user_content",
]
