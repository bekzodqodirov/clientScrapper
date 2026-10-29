# VENDORED_FROM dailyJournal@6b1f67f miya/services/purge.py
"""Purge (06 §5.4, 02 §2.14): forget a company, a phone or a Telegram user.

MIYA's plan → execute shape, adapted from people to companies. Deleting is
the one thing the agent cannot undo, so it is two steps: ``plan_company`` /
``plan_contact`` count what would go (nothing changes), the owner sees the
counts — never a name — and only ``execute`` changes anything; ``/unut`` and
``tools/purge.py`` share these functions (``pg:<purge_log_id>:y`` or
``--apply``).

**The subject** is a company's whole lineage (the root and every company
merged into it, 04 §2.5): a merged source is the same firm. A phone or a
Telegram id resolves to the companies whose live contacts carry it first;
a Telegram id also reaches the messages its sender wrote where no company
holds them.

**What goes** (06 §5.4): ``contacts``; ``observations`` of the lineage, of
its contacts and of the subject's messages; ``interactions`` the subject's
Telegram ids sent or that the observations came from; ``signals`` (a quote
is a slice of the page); ``model_jobs.request``/``pseudonyms`` and the result
(an unsent or in-flight job ends ``failed`` with ``error='purged'``); the
evidence rows only the subject's rows cite, their files unlinked last when
no other row holds them; ``company_aliases``, ``company_ids``; the
candidates and their review cards (a ⏭ card's ``remind_at`` with them, so
nothing is asked again) — **except a candidate with ``push_log`` rows**,
which ``push_log.candidate_id`` RESTRICT keeps on purpose: an open one ends
``suppressed`` (pointing at the ``legal``/``dnc`` row this purge wrote) or,
for any other reason, ``excluded(owner)``; a terminal one stays as it is;
either loses its opener (``score_breakdown.opener``/``opener_meta``, the one
free text there — ``push_note`` holds only the gate's coded refusal words).
``sheet_rows`` are tombstoned and a monitored chat of the company's
channel is no longer read. A message of the subject's person that resolved to
another firm is the person's and goes with its observation and signals; that
firm is not purged, its candidate is scored again (``signals.mark_stale``).

**What is anonymised**: ``push_log.payload := NULL``; every lineage company
becomes ``state='purged'`` with ``display_name='[oʻchirilgan]'`` and a name
key no name match reaches, ``merged_into`` cleared in the same UPDATE (02
§2.4). The root keeps its STIR, domain, channel and Instagram: the resolver
answers an observation carrying them «purged» and never makes the firm
again (``companies.resolve_company``); a merged source gives up a STIR or a
domain another company holds (its root's, mostly — the two would collide
under the unique indexes once the source is no longer ``merged``) and keeps
one only it had, so that key answers «purged» too.

**What stays**: ``outcomes``, ``usage_log``, ``merge_log``, ``purge_log``
(the hash, the counts) and the suppression rows. With ``legal_request`` or
``dnc`` a ``suppression(legal|dnc)`` row is written for every key the
subject had (``suppression.keys_for``: phones as tails, usernames, Telegram
ids, Instagram, domain, STIR, every lineage id), so no card or push reaches
it again.

**Transactions** (06 §5.4: «one transaction per table group; files unlinked
last»): the claim on the ``purge_log`` row alone (a second tap then finds it
``running``); the suppression rows, the candidates and the companies — once
that commits, nothing is carded, pushed or resolved to the subject even if a
later group fails — then the people data, then identity and evidence, then
the files, then the log row's ``executed``. A failure leaves
``error='failed:<class>'`` on the row; a new plan finishes the job: a purged
company that still holds rows a purge removes is ``unfinished`` and ``/unut``
plans it again. A file the unlink missed (a stop after the evidence commit, a
refusing disk) is no row's any more: retention R1's ``evidence.sweep_orphans``
takes it.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from clientscrapper.db import enums as e
from clientscrapper.db.models import (
    ChatMonitor,
    Company,
    CompanyAlias,
    CompanyId,
    Contact,
    Interaction,
    LeadCandidate,
    LeadReview,
    MergeLog,
    ModelJob,
    Observation,
    PurgeLog,
    PushLog,
    SheetRow,
    Signal,
)
from clientscrapper.db.models import Evidence as EvidenceRow
from clientscrapper.services import candidates, companies, evidence, normalise, push_gate
from clientscrapper.services import keys as keys_service
from clientscrapper.services import signals as signal_service
from clientscrapper.services import suppression as suppression_service
from clientscrapper.services.suppression import Entry, Key

if TYPE_CHECKING:
    from clientscrapper.services.reviews import CardSender

log = logging.getLogger(__name__)

PURGED_NAME: Final = "[oʻchirilgan]"
# A stored name key no name_key() produces (companies.LABEL_PREFIX): no exact
# or fuzzy match reaches a purged company by its old name.
PURGED_NAME_KEY: Final = companies.LABEL_PREFIX + "purged"
# The reasons an owner gives (purge_log.reason minus the job's own).
OWNER_REASONS: Final = tuple(r for r in e.PURGE_REASONS if r != "retention")
DEFAULT_REASON: Final = "legal_request"
# The reasons that write a suppression row per key, and its kind (06 §5.4).
SUPPRESSING: Final = {"legal_request": "legal", "dnc": "dnc"}
SUPPRESSION_SOURCE: Final = suppression_service.OWNER_SOURCE
# What an open candidate with push history becomes for the other reasons.
EXCLUDE_REASON: Final = "owner"
# purge_log.error while an execute holds the row, and after a failure.
RUNNING: Final = "running"
FAILED_PREFIX: Final = "failed:"
JOB_PURGED: Final = "purged"
LIVE_JOB_STATUSES: Final = ("pending", "submitted")
SHEET_TOMBSTONE: Final = "tombstone"
# A purged lead's cards (seam review wave 4): the messages the owner and the
# managers were sent named the company, its phone, its person and its quotes,
# and the purge deleted their rows. Group 1 writes where they are into the
# log row (``executed.cards``: [chat id, message id] — our own chats, no
# subject data) in the same transaction as the delete; ``close_purged_cards``
# edits each to this text with no buttons, at once from the bot and from the
# card job after, and forgets each one it closed. Telegram is asked for
# ``CARDS_WINDOW`` after the plan, then the pointers are dropped.
PURGED_CARD_UZ: Final = "🗑 Bu lid maʼlumotlari oʻchirildi."
CARDS_KEY: Final = "cards"
CARDS_WINDOW: Final = timedelta(days=14)
CARDS_LIMIT: Final = 50

# The order of the counts on the plan card and in the log.
COUNT_KEYS: Final = (
    "companies",
    "contacts",
    "observations",
    "interactions",
    "signals",
    "evidence",
    "files",
    "model_jobs",
    "aliases",
    "company_ids",
    "candidates",
    "candidates_ended",
    "reviews",
    "push_payloads",
    "sheet_rows",
    "chats",
    "suppression",
    "erp_leads",
)
# What groups 2 and 3 remove (group 1's candidates, cards, sheet rows and chats
# are gone once the company reads 'purged'): a purged lineage that still holds
# any of these is a purge that stopped part-way (``unfinished``).
LEFTOVER_KEYS: Final = (
    "contacts",
    "observations",
    "interactions",
    "signals",
    "evidence",
    "files",
    "model_jobs",
    "aliases",
    "company_ids",
    "push_payloads",
)
# How far ``subject_root`` follows a purged source to its old root.
ROOT_HOPS: Final = 8
# ``row_state``'s answers: a plan that may still run, one an execute holds or
# held when it stopped, one that failed, one executed, one cancelled or
# expired, no such row.
ROW_OPEN: Final = "open"
ROW_RUNNING: Final = "running"
ROW_FAILED: Final = "failed"
ROW_DONE: Final = "done"
ROW_CLOSED: Final = "closed"
ROW_UNKNOWN: Final = "unknown"


class PurgeRefused(Exception):
    """The plan cannot run: its log row was executed, abandoned or is
    being executed (``code`` says which), or the subject is unknown."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def hash_key(key: str) -> str:
    """``purge_log.subject_hash``: sha256 of the canonical key (02 §2.14)."""
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass(slots=True)
class PurgePlan:
    """What a purge would do. The subject's ids are kept here, in memory,
    and never in the log row (which holds the hash and the counts)."""

    subject_kind: str
    subject_hash: str
    reason: str
    company_ids: tuple[uuid.UUID, ...]
    tg_user_ids: tuple[int, ...] = ()
    own_keys: tuple[Key, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    purge_log_id: int | None = None

    @property
    def empty(self) -> bool:
        return not any(self.counts.values())


@dataclass(slots=True)
class PurgeResult:
    purge_log_id: int
    executed: dict[str, int]
    files_removed: int


@dataclass(slots=True)
class Scope:
    """Every row a purge touches, read in one place for the plan and again
    for the execution."""

    members: tuple[uuid.UUID, ...] = ()
    roots: tuple[uuid.UUID, ...] = ()
    contacts: tuple[uuid.UUID, ...] = ()
    tg_user_ids: tuple[int, ...] = ()
    observations: tuple[int, ...] = ()
    interactions: tuple[int, ...] = ()
    signals: tuple[int, ...] = ()
    evidence: tuple[int, ...] = ()
    files: tuple[str, ...] = ()
    model_jobs: tuple[int, ...] = ()
    aliases: int = 0
    company_ids: int = 0
    deleted: tuple[uuid.UUID, ...] = ()
    kept: tuple[uuid.UUID, ...] = ()
    ended: tuple[uuid.UUID, ...] = ()
    reviews: int = 0
    payloads: tuple[int, ...] = ()
    pushes: tuple[int, ...] = ()
    sheet_rows: int = 0
    chats: tuple[int, ...] = ()
    keys: tuple[Key, ...] = ()
    # Leads the ERP received (a sent or unknown push): what the agent sent
    # stays there — the owner is told to erase it in the ERP (seam review
    # wave 4; 05 has no erasure call).
    erp_leads: int = 0
    # Other companies whose observations or signals go with the subject's
    # messages (a post its person wrote that resolved elsewhere): scored again.
    bystanders: tuple[uuid.UUID, ...] = ()

    def counts(self) -> dict[str, int]:
        return {
            "companies": len(self.members),
            "contacts": len(self.contacts),
            "observations": len(self.observations),
            "interactions": len(self.interactions),
            "signals": len(self.signals),
            "evidence": len(self.evidence),
            "files": len(self.files),
            "model_jobs": len(self.model_jobs),
            "aliases": self.aliases,
            "company_ids": self.company_ids,
            "candidates": len(self.deleted),
            "candidates_ended": len(self.ended),
            "reviews": self.reviews,
            "push_payloads": len(self.payloads),
            "sheet_rows": self.sheet_rows,
            "chats": len(self.chats),
            "suppression": len(self.keys),
            "erp_leads": self.erp_leads,
        }


async def _ids(session: AsyncSession, stmt: sa.Select[Any]) -> list[Any]:
    return list(await session.scalars(stmt))


async def _count(session: AsyncSession, stmt: sa.Select[Any]) -> int:
    found = await session.scalar(sa.select(sa.func.count()).select_from(stmt.subquery()))
    return int(found or 0)


def _key_fits(key: Key) -> bool:
    return suppression_service.KEY_MIN <= len(key[1]) <= suppression_service.KEY_MAX


def _pseudonyms_name(
    tails: Collection[str], usernames: Collection[str], ids: Collection[str]
) -> sa.ColumnElement[bool]:
    """A job whose reverse map (``model_jobs.pseudonyms``, token → value as
    first written) holds one of the subject's phones (by tail, under a
    ``<PHONE_n>`` token), usernames or Telegram ids (under ``<TG_n>``)."""
    pairs = sa.func.jsonb_each_text(ModelJob.pseudonyms).table_valued("key", "value")
    phone = sa.and_(
        pairs.c.key.like("<PHONE\\_%"),
        sa.func.cs_phone_tail(pairs.c.value).in_(sorted(tails)),
    )
    handle = sa.and_(
        pairs.c.key.like("<TG\\_%"),
        sa.or_(
            sa.func.cs_username_key(pairs.c.value).in_(sorted(usernames)),
            pairs.c.value.in_(sorted(ids)),
        ),
    )
    return sa.exists(
        sa.select(sa.literal(1)).select_from(pairs).where(sa.or_(phone, handle))
    )


async def collect(
    session: AsyncSession,
    company_ids: Collection[uuid.UUID],
    *,
    tg_user_ids: Collection[int] = (),
    own_keys: Collection[Key] = (),
    reason: str,
) -> Scope:
    """The rows the subject is made of, as they stand now."""
    groups = await companies.lineages(session, sorted(set(company_ids)))
    members = sorted({m for group in groups.values() for m in group})
    roots = (
        await _ids(
            session,
            sa.select(Company.id)
            .where(
                Company.id.in_(members),
                sa.or_(Company.state != "merged", Company.merged_into.is_(None)),
            )
            .order_by(Company.id),
        )
        if members
        else []
    )
    people = (
        await session.execute(
            sa.select(Contact.id, Contact.telegram_id)
            .where(Contact.company_id.in_(members))
            .order_by(Contact.id)
        )
    ).all()
    contact_ids = [p.id for p in people]
    tg = sorted({*tg_user_ids, *(p.telegram_id for p in people if p.telegram_id)})
    first = set(
        await _ids(
            session,
            sa.select(Observation.id).where(
                sa.or_(
                    Observation.company_id.in_(members),
                    Observation.contact_id.in_(contact_ids),
                )
            ),
        )
    )
    # An intent post that names the subject by its handles (until R3 strips
    # them) is theirs wherever it resolved, or if it resolved nowhere.
    handles = Observation.parsed["handles"]
    named = sorted({key for kind, key in own_keys if kind == "tg_username"})
    naming: list[sa.ColumnElement[bool]] = []
    if tg:
        naming.append(handles["tg_user_id"].astext.in_([str(t) for t in tg]))
    if named:
        naming.append(sa.func.lower(handles["telegram"].astext).in_(named))
    if naming:
        first |= set(
            await _ids(session, sa.select(Observation.id).where(sa.or_(*naming)))
        )
    sent = (
        await session.execute(
            sa.select(Interaction.observation_id).where(
                sa.or_(
                    Interaction.sender_id.in_(tg),
                    Interaction.observation_id.in_(sorted(first)),
                )
            )
        )
    ).all()
    observations = sorted(first | {r[0] for r in sent if r[0] is not None})
    messages = (
        await session.execute(
            sa.select(
                Interaction.id,
                Interaction.evidence_id,
                Interaction.sender_id,
                Interaction.sender_username,
            )
            .where(
                sa.or_(
                    Interaction.sender_id.in_(tg),
                    Interaction.observation_id.in_(observations),
                )
            )
            .order_by(Interaction.id)
        )
    ).all()
    interactions = [m.id for m in messages]
    signal_rows = (
        await session.execute(
            sa.select(Signal.id, Signal.evidence_id, Signal.company_id)
            .where(
                sa.or_(
                    Signal.company_id.in_(members),
                    Signal.observation_id.in_(observations),
                    Signal.contact_id.in_(contact_ids),
                )
            )
            .order_by(Signal.id)
        )
    ).all()
    signal_ids = [s.id for s in signal_rows]
    touched = {s.company_id for s in signal_rows}
    touched |= set(
        await _ids(
            session,
            sa.select(Observation.company_id).where(
                Observation.id.in_(observations), Observation.company_id.is_not(None)
            ),
        )
    )
    cited = {s.evidence_id for s in signal_rows}
    cited |= {m.evidence_id for m in messages if m.evidence_id is not None}
    cited |= set(
        await _ids(
            session,
            sa.select(Observation.evidence_id).where(Observation.id.in_(observations)),
        )
    )
    # Only the subject's rows cite it: no other observation, signal or message.
    alone = (
        await _ids(
            session,
            sa.select(EvidenceRow.id)
            .where(
                EvidenceRow.id.in_(sorted(cited)),
                ~sa.exists().where(
                    Observation.evidence_id == EvidenceRow.id,
                    Observation.id.not_in(observations),
                ),
                ~sa.exists().where(
                    Signal.evidence_id == EvidenceRow.id, Signal.id.not_in(signal_ids)
                ),
                ~sa.exists().where(
                    Interaction.evidence_id == EvidenceRow.id,
                    Interaction.id.not_in(interactions),
                ),
            )
            .order_by(EvidenceRow.id),
        )
        if cited
        else []
    )
    files = await _files_of(session, alone)
    job_match: list[sa.ColumnElement[bool]] = [ModelJob.company_id.in_(members)]
    if alone:
        job_match.append(ModelJob.evidence_ids.overlap(alone))
    # A request about another company that held the subject's phone or
    # handle (a reply quoting it, a dealer list): its reverse map holds them
    # raw (seam review wave 4), so it goes with them.
    held = (
        await session.execute(
            sa.select(Contact.phone, Contact.telegram_username).where(
                Contact.company_id.in_(members)
            )
        )
    ).all()
    tails = {t for phone, _ in held if phone and (t := keys_service.phone_tail(phone))}
    tails |= {key for kind, key in own_keys if kind == "phone_tail"}
    usernames = {u for _, u in held if u}
    usernames |= {m.sender_username for m in messages if m.sender_username}
    usernames |= {key for kind, key in own_keys if kind == "tg_username"}
    usernames |= set(
        await _ids(
            session,
            sa.select(Company.telegram_channel).where(
                Company.id.in_(members), Company.telegram_channel.is_not(None)
            ),
        )
    )
    ids = {str(t) for t in tg} | {str(m.sender_id) for m in messages if m.sender_id}
    if tails or usernames or ids:
        job_match.append(_pseudonyms_name(tails, usernames, ids))
    jobs = await _ids(
        session,
        sa.select(ModelJob.id)
        .where(
            sa.or_(*job_match),
            sa.or_(
                ModelJob.request.is_not(None),
                ModelJob.pseudonyms.is_not(None),
                ModelJob.result.is_not(None),
                ModelJob.status.in_(LIVE_JOB_STATUSES),
            ),
        )
        .order_by(ModelJob.id),
    )
    cands = (
        await session.execute(
            sa.select(
                LeadCandidate.id,
                LeadCandidate.state,
                sa.exists()
                .where(PushLog.candidate_id == LeadCandidate.id)
                .label("pushed"),
            )
            .where(LeadCandidate.company_id.in_(members))
            .order_by(LeadCandidate.id)
        )
    ).all()
    every = [c.id for c in cands]
    kept = [c.id for c in cands if c.pushed]
    ended = [
        c.id for c in cands if c.pushed and c.state not in candidates.TERMINAL_STATES
    ]
    pushes = (
        await session.execute(
            sa.select(
                PushLog.id,
                PushLog.payload.is_not(None).label("held"),
                PushLog.status.in_(("sent", "unknown")).label("landed"),
            )
            .where(PushLog.company_id.in_(members))
            .order_by(PushLog.id)
        )
    ).all()
    sheet_match = sa.or_(
        sa.and_(
            SheetRow.entity_kind == "company",
            SheetRow.entity_id.in_([str(m) for m in members]),
        ),
        sa.and_(
            SheetRow.entity_kind == "candidate",
            SheetRow.entity_id.in_([str(c) for c in every]),
        ),
        sa.and_(
            SheetRow.entity_kind == "push",
            SheetRow.entity_id.in_([str(p.id) for p in pushes]),
        ),
    )
    channels = [
        c
        for c in await _ids(
            session, sa.select(Company.telegram_channel).where(Company.id.in_(members))
        )
        if c
    ]
    keys: set[Key] = set()
    if reason in SUPPRESSING:
        for root in roots:
            keys |= set(await suppression_service.keys_for(session, root, secret=""))
        # The senders of the messages that go: the subject's own, and the
        # poster of every message its observations came from (a next post by
        # that poster must not card the firm again, 06 §5.4). A channel's
        # negative peer id is no user id.
        senders = {*tg, *(m.sender_id for m in messages if m.sender_id)}
        keys |= {("tg_user_id", str(t)) for t in senders if t > 0}
        keys |= {
            ("tg_username", m.sender_username) for m in messages if m.sender_username
        }
        keys |= set(own_keys)
    return Scope(
        members=tuple(members),
        roots=tuple(roots),
        contacts=tuple(contact_ids),
        tg_user_ids=tuple(tg),
        observations=tuple(observations),
        interactions=tuple(interactions),
        signals=tuple(signal_ids),
        evidence=tuple(alone),
        files=tuple(files),
        model_jobs=tuple(jobs),
        aliases=await _count(
            session,
            sa.select(CompanyAlias.id).where(CompanyAlias.company_id.in_(members)),
        ),
        company_ids=await _count(
            session,
            sa.select(CompanyId.id_type).where(CompanyId.company_id.in_(members)),
        ),
        deleted=tuple(c for c in every if c not in set(kept)),
        kept=tuple(kept),
        ended=tuple(ended),
        reviews=await _count(
            session, sa.select(LeadReview.id).where(LeadReview.candidate_id.in_(every))
        ),
        payloads=tuple(p.id for p in pushes if p.held),
        pushes=tuple(p.id for p in pushes),
        erp_leads=sum(1 for p in pushes if p.landed),
        sheet_rows=await _count(
            session,
            sa.select(SheetRow.id).where(sheet_match, SheetRow.state != SHEET_TOMBSTONE),
        ),
        chats=tuple(
            await _ids(
                session,
                sa.select(ChatMonitor.id)
                .where(ChatMonitor.username.in_(channels), ChatMonitor.monitor_enabled)
                .order_by(ChatMonitor.id),
            )
        ),
        keys=tuple(sorted(k for k in keys if _key_fits(k))),
        bystanders=tuple(sorted(touched - set(members))),
    )


async def _files_of(session: AsyncSession, ids: Sequence[int]) -> list[str]:
    """The files of these evidence rows that no other row holds (02 §3)."""
    if not ids:
        return []
    paths = sorted(
        {
            p
            for p in await _ids(
                session, sa.select(EvidenceRow.raw_path).where(EvidenceRow.id.in_(ids))
            )
            if p is not None
        }
    )
    if not paths:
        return []
    shared = set(
        await _ids(
            session,
            sa.select(EvidenceRow.raw_path).where(
                EvidenceRow.raw_path.in_(paths), EvidenceRow.id.not_in(ids)
            ),
        )
    )
    return [p for p in paths if p not in shared]


# --- the plan ---------------------------------------------------------------------------


def _reason(reason: str) -> str:
    if reason not in OWNER_REASONS:
        raise ValueError(f"not a purge reason: {reason}")
    return reason


async def subject_root(session: AsyncSession, company: Company) -> Company:
    """The root a purge of ``company`` names: ``companies.root_company``, and
    for a purged merged source — whose ``merged_into`` the purge cleared —
    the target of its merge (``merge_log``, undone merges excluded), so a
    second purge through a source's id still takes the whole firm."""
    found = await companies.root_company(session, company)
    for _ in range(ROOT_HOPS):
        if found.state != "purged":
            return found
        target = await session.scalar(
            sa.select(MergeLog.target_company_id)
            .where(MergeLog.source_company_id == found.id, MergeLog.undone_at.is_(None))
            .order_by(MergeLog.created_at.desc(), MergeLog.id.desc())
            .limit(1)
        )
        nxt = await session.get(Company, target) if target is not None else None
        if nxt is None:
            return found
        found = await companies.root_company(session, nxt)
    return found


async def unfinished(session: AsyncSession, company_id: uuid.UUID) -> bool:
    """A purged lineage that still holds rows a purge removes: a purge of it
    stopped after its first group committed (06 §5.4's groups commit one by
    one). ``/unut`` plans such a company again and the new plan finishes the
    job; a lineage with nothing left is forgotten (the resolver refuses its
    observations, so nothing comes back)."""
    counts = (await collect(session, (company_id,), reason=EXCLUDE_REASON)).counts()
    return any(counts[key] for key in LEFTOVER_KEYS)


async def plan_company(
    session: AsyncSession, company_id: uuid.UUID, *, reason: str = DEFAULT_REASON
) -> PurgePlan:
    """What purging ``company_id``'s lineage would do; nothing changes. The
    subject's hash is the root's uuid (a merged source is its root's firm).
    Raises ``PurgeRefused('unknown')`` for an id that names no company."""
    found = await session.get(Company, company_id)
    if found is None:
        raise PurgeRefused("unknown")
    root = await subject_root(session, found)
    plan = PurgePlan(
        subject_kind="company",
        subject_hash=hash_key(str(root.id)),
        reason=_reason(reason),
        company_ids=(root.id,),
    )
    plan.counts = (await collect(session, plan.company_ids, reason=plan.reason)).counts()
    return plan


async def plan_contact(
    session: AsyncSession,
    *,
    phone_tail: str | None = None,
    tg_user_id: int | None = None,
    reason: str = DEFAULT_REASON,
) -> PurgePlan:
    """What purging a phone tail or a Telegram id would do: the companies
    whose live contacts carry it (each with its lineage), and for a Telegram
    id its own messages; nothing changes."""
    if (phone_tail is None) == (tg_user_id is None):
        raise ValueError("a phone tail or a Telegram id, exactly one")
    tg: tuple[int, ...] = ()
    own: tuple[Key, ...]
    if phone_tail is not None:
        tail = phone_tail.strip()
        if not (len(tail) == 9 and tail.isascii() and tail.isdigit()):
            raise ValueError("a phone tail is nine digits")
        holders = await _ids(
            session,
            sa.select(Contact.company_id).where(
                Contact.phone_tail == tail, Contact.anonymised_at.is_(None)
            ),
        )
        kind, key, own = "contact", tail, (("phone_tail", tail),)
    else:
        assert tg_user_id is not None
        holders = await _ids(
            session,
            sa.select(Contact.company_id).where(Contact.telegram_id == tg_user_id),
        )
        kind, key = "tg_user", str(tg_user_id)
        own, tg = (("tg_user_id", key),), (tg_user_id,)
    plan = PurgePlan(
        subject_kind=kind,
        subject_hash=hash_key(key),
        reason=_reason(reason),
        company_ids=tuple(sorted(set(holders))),
        tg_user_ids=tg,
        own_keys=own,
    )
    plan.counts = (await _scope_of(session, plan)).counts()
    return plan


async def senders_of(session: AsyncSession, handle: str) -> list[int]:
    """The Telegram user ids a ``@username`` was seen on (seam review wave
    4): the messages its holder wrote (``interactions.sender_username``) and
    the intent posts that name it (``observations.parsed.handles``, until R3
    strips them). A channel's negative peer id is no user."""
    key = normalise.telegram_key(handle)
    if key is None:
        return []
    handles = Observation.parsed["handles"]
    found = await session.scalars(
        sa.union(
            sa.select(Interaction.sender_id).where(
                Interaction.sender_username == key, Interaction.sender_id.is_not(None)
            ),
            sa.select(sa.cast(handles["tg_user_id"].astext, sa.BigInteger)).where(
                sa.func.lower(handles["telegram"].astext) == key,
                handles["tg_user_id"].astext.op("~")(r"^[0-9]{1,19}$"),
            ),
        )
    )
    return sorted({int(i) for i in found if i is not None and int(i) > 0})


async def plan_handle(
    session: AsyncSession, handle: str, *, reason: str = DEFAULT_REASON
) -> PurgePlan | None:
    """``/unut @username`` for a person the agent keeps no contact under that
    name (seam review wave 4: the lookup answered «nothing found» while their
    messages were held): the ``tg_user`` plan over every id the username was
    seen on, with the username as a key of its own. None when nothing carries
    it — no message, no post naming it; nothing changes."""
    key = normalise.telegram_key(handle)
    if key is None:
        return None
    ids = await senders_of(session, handle)
    named = await session.scalar(
        sa.select(
            sa.exists().where(
                sa.func.lower(Observation.parsed["handles"]["telegram"].astext) == key
            )
        )
    )
    if not ids and not named:
        return None
    holders = await _ids(
        session,
        sa.select(Contact.company_id).where(Contact.telegram_id.in_(ids)),
    )
    own: tuple[Key, ...] = (
        ("tg_username", key),
        *(("tg_user_id", str(i)) for i in ids),
    )
    plan = PurgePlan(
        subject_kind="tg_user",
        subject_hash=hash_key(",".join(str(i) for i in ids) or key),
        reason=_reason(reason),
        company_ids=tuple(sorted(set(holders))),
        tg_user_ids=tuple(ids),
        own_keys=tuple(k for k in own if _key_fits(k)),
    )
    plan.counts = (await _scope_of(session, plan)).counts()
    return plan


async def _scope_of(session: AsyncSession, plan: PurgePlan) -> Scope:
    return await collect(
        session,
        plan.company_ids,
        tg_user_ids=plan.tg_user_ids,
        own_keys=plan.own_keys,
        reason=plan.reason,
    )


async def record(session: AsyncSession, plan: PurgePlan, *, requested_by: str) -> int:
    """The plan's ``purge_log`` row (the hash and the counts, never a name);
    its id is what the confirm button carries. The caller commits."""
    row = PurgeLog(
        subject_kind=plan.subject_kind,
        subject_hash=plan.subject_hash,
        reason=plan.reason,
        requested_by=requested_by,
        plan=dict(plan.counts),
    )
    session.add(row)
    await session.flush()
    plan.purge_log_id = row.id
    return row.id


async def abandon(session: AsyncSession, purge_log_id: int, *, why: str) -> bool:
    """A plan that will not run (cancelled, or its confirm came after the plan
    was forgotten): the row says so and can never be executed."""
    result = await session.execute(
        sa.update(PurgeLog)
        .where(
            PurgeLog.id == purge_log_id,
            PurgeLog.executed_at.is_(None),
            PurgeLog.error.is_(None),
        )
        .values(error=why)
        .returning(PurgeLog.id)
    )
    return result.first() is not None


# --- the execution ----------------------------------------------------------------------


async def row_state(session: AsyncSession, purge_log_id: int) -> str:
    """Where a plan's log row stands: ``open`` (it may still run), ``running``
    (an execute holds it — or held it when its process stopped), ``failed``
    (a group failed; the company may hold the rest, ``unfinished``), ``done``,
    ``closed`` (cancelled or expired) or ``unknown`` (no such row)."""
    found = (
        await session.execute(
            sa.select(PurgeLog.executed_at, PurgeLog.error).where(
                PurgeLog.id == purge_log_id
            )
        )
    ).first()
    if found is None:
        return ROW_UNKNOWN
    executed_at, error = found
    if executed_at is not None:
        return ROW_DONE
    if error is None:
        return ROW_OPEN
    if error == RUNNING:
        return ROW_RUNNING
    if error.startswith(FAILED_PREFIX):
        return ROW_FAILED
    return ROW_CLOSED


async def _claim(session: AsyncSession, purge_log_id: int) -> None:
    """The row is this execute's — committed before anything is read, so a
    second tap, a cancel or an expiry that comes later finds it ``running``
    and never closes a plan another tap is executing."""
    claimed = await session.scalar(
        sa.update(PurgeLog)
        .where(
            PurgeLog.id == purge_log_id,
            PurgeLog.executed_at.is_(None),
            PurgeLog.error.is_(None),
        )
        .values(error=RUNNING)
        .returning(PurgeLog.id)
    )
    if claimed is None:
        raise PurgeRefused(await row_state(session, purge_log_id))
    await session.commit()


async def _gone(session: AsyncSession, stmt: Any) -> int:
    result = await session.execute(stmt.execution_options(synchronize_session=False))
    return int(getattr(result, "rowcount", 0) or 0)


async def _merge_executed(
    session: AsyncSession, purge_log_id: int, part: dict[str, Any], **values: Any
) -> None:
    """``executed || part``: the counts and the card pointers are written by
    different steps, and neither may drop the other's."""
    merged = sa.func.coalesce(PurgeLog.executed, sa.cast({}, JSONB)).op("||")(
        sa.cast(part, JSONB)
    )
    await session.execute(
        sa.update(PurgeLog)
        .where(PurgeLog.id == purge_log_id)
        .values(executed=merged, **values)
    )


async def close_purged_cards(
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    sender: CardSender,
    *,
    now: datetime,
) -> int:
    """Edit every card a purge left in a chat to ``PURGED_CARD_UZ`` with no
    buttons and forget it; a card Telegram would not edit stays listed and
    is asked again next time, until ``CARDS_WINDOW`` after the plan (then
    the pointers go). Returns the cards closed."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.select(PurgeLog.id, PurgeLog.planned_at, PurgeLog.executed[CARDS_KEY])
                .where(PurgeLog.executed.has_key(CARDS_KEY))
                .order_by(PurgeLog.id)
                .limit(CARDS_LIMIT)
            )
        ).all()
    closed = 0
    for purge_log_id, planned_at, pointers in rows:
        left: list[list[int]] = []
        if planned_at >= now - CARDS_WINDOW:
            for chat_id, message_id in pointers or []:
                if await sender.edit(
                    int(chat_id), int(message_id), PURGED_CARD_UZ, None, 0
                ):
                    closed += 1
                else:
                    left.append([int(chat_id), int(message_id)])
        kept = (
            PurgeLog.executed.op("-")(CARDS_KEY)
            if not left
            else sa.func.jsonb_set(
                PurgeLog.executed,
                sa.literal(["cards"], ARRAY(sa.Text)),
                sa.cast(left, JSONB),
            )
        )
        async with session_factory() as session:
            await session.execute(
                sa.update(PurgeLog)
                .where(PurgeLog.id == purge_log_id)
                .values(executed=kept)
            )
            await session.commit()
    return closed


async def _end_candidate(
    session: AsyncSession, candidate_id: uuid.UUID, *, row_id: int | None, now: datetime
) -> bool:
    """An open candidate with push history ends terminal: ``suppressed`` by
    the purge's row, else ``excluded(owner)`` — through ``review`` from
    ``approved``, the one open state the table does not let into
    ``excluded`` directly (02 §2.9)."""
    locked = await push_gate.lock_candidate(session, candidate_id)
    if locked is None or locked.state in candidates.TERMINAL_STATES:
        return False
    if row_id is not None:
        await candidates.transition(
            session, locked, "suppressed", now=now, suppressed_by=row_id
        )
        return True
    if "excluded" not in candidates.TRANSITIONS[locked.state]:
        await candidates.transition(session, locked, "review", now=now)
    await candidates.transition(
        session, locked, "excluded", now=now, exclude_reason=EXCLUDE_REASON
    )
    return True


def _released(name: str) -> sa.ColumnElement[Any]:
    """A purged source's STIR or domain: NULL when another company holds the
    same value under ``ux_companies_stir``/``ux_companies_domain`` (whose
    predicate is ``state <> 'merged'``: the source leaving ``merged`` would
    collide — with its root's, mostly), else kept, so the resolver answers
    «purged» for a key only the source had (the root's are kept anyway)."""
    other = aliased(Company)
    mine, theirs = getattr(Company, name), getattr(other, name)
    held = (
        sa.select(sa.literal(1))
        .where(theirs == mine, other.id != Company.id, other.state != "merged")
        .exists()
    )
    return sa.case((held, sa.null()), else_=mine)


async def _stop(
    session: AsyncSession, plan: PurgePlan, scope: Scope, *, now: datetime
) -> dict[str, int]:
    """Group 1: the suppression rows, the candidates, the companies."""
    members = list(scope.members)
    if members:
        await session.execute(
            sa.select(Company.id)
            .where(Company.id.in_(members))
            .order_by(Company.id)
            .with_for_update()
        )
    row_id: int | None = None
    written = 0
    kind = SUPPRESSING.get(plan.reason)
    if kind is not None and scope.keys:
        ids = await suppression_service.add(
            session,
            [Entry(kind, key_type, key) for key_type, key in scope.keys],
            source=SUPPRESSION_SOURCE,
            reason=f"purge:{plan.reason}",
        )
        row_id, written = ids[0], len(ids)
    ended = 0
    for candidate_id in scope.ended:
        if await _end_candidate(session, candidate_id, row_id=row_id, now=now):
            ended += 1
    every = [*scope.deleted, *scope.kept]
    shown = (
        await session.execute(
            sa.select(LeadReview.card_chat_id, LeadReview.card_message_id)
            .where(
                LeadReview.candidate_id.in_(every),
                LeadReview.card_chat_id.is_not(None),
                LeadReview.card_message_id.is_not(None),
            )
            .order_by(LeadReview.id)
        )
    ).all()
    if shown and plan.purge_log_id is not None:
        await _merge_executed(
            session, plan.purge_log_id, {CARDS_KEY: [[c, m] for c, m in shown]}
        )
    reviews = await _gone(
        session, sa.delete(LeadReview).where(LeadReview.candidate_id.in_(every))
    )
    deleted = await _gone(
        session, sa.delete(LeadCandidate).where(LeadCandidate.id.in_(scope.deleted))
    )
    # A kept candidate's opener is free text a manager would send — a draft
    # was asked with the company's name (04 §4.5) and may say it — so it goes
    # with the name; what stays of score_breakdown is keys, ids and points.
    if scope.kept:
        await _gone(
            session,
            sa.update(LeadCandidate)
            .where(LeadCandidate.id.in_(scope.kept))
            .values(
                score_breakdown=LeadCandidate.score_breakdown.op("-")(
                    sa.cast(list(candidates.CARRIED_KEYS), ARRAY(sa.Text))
                )
            ),
        )
    anonymous: dict[str, Any] = {
        "display_name": PURGED_NAME,
        "name_key": PURGED_NAME_KEY,
        "state": "purged",
        "merged_into": None,
        "exclude_reason": None,
        "dup_of": None,
        "dup_score": None,
        # SQL NULL, not JSON 'null' (a JSONB column takes None as the latter).
        "verdict": sa.null(),
        "verdict_at": None,
        # A sole trader's (YaTT) address may be a home (06 §5.1 Forbidden
        # tier, seam review wave 4); nothing reads a purged firm's address,
        # and the STIR, domain and suppression keys still answer «purged».
        "address": None,
    }
    roots = list(scope.roots)
    sources = [m for m in members if m not in set(roots)]
    purged = await _gone(
        session, sa.update(Company).where(Company.id.in_(roots)).values(**anonymous)
    )
    # One source at a time: each sees the sources before it already purged.
    for source in sources:
        purged += await _gone(
            session,
            sa.update(Company)
            .where(Company.id == source)
            .values(
                **anonymous,
                stir=_released("stir"),
                website_domain=_released("website_domain"),
            ),
        )
    await _gone(
        session,
        sa.update(Company)
        .where(Company.dup_of.in_(members))
        .values(dup_of=None, dup_score=None),
    )
    chats = await _gone(
        session,
        sa.update(ChatMonitor)
        .where(ChatMonitor.id.in_(scope.chats))
        .values(monitor_enabled=False, decided_by="owner"),
    )
    sheet = await _gone(
        session,
        sa.update(SheetRow)
        .where(
            sa.or_(
                sa.and_(
                    SheetRow.entity_kind == "company",
                    SheetRow.entity_id.in_([str(m) for m in members]),
                ),
                sa.and_(
                    SheetRow.entity_kind == "candidate",
                    SheetRow.entity_id.in_([str(c) for c in every]),
                ),
                sa.and_(
                    SheetRow.entity_kind == "push",
                    SheetRow.entity_id.in_([str(p) for p in scope.pushes]),
                ),
            ),
            SheetRow.state != SHEET_TOMBSTONE,
        )
        .values(state=SHEET_TOMBSTONE),
    )
    return {
        "companies": purged,
        "candidates": deleted,
        "candidates_ended": ended,
        "reviews": reviews,
        "chats": chats,
        "sheet_rows": sheet,
        "suppression": written,
        "erp_leads": scope.erp_leads,
    }


async def _people(
    session: AsyncSession, scope: Scope, *, now: datetime
) -> dict[str, int]:
    """Group 2: messages, observations, signals, contacts, model requests,
    push payloads."""
    members = list(scope.members)
    interactions = await _gone(
        session,
        sa.delete(Interaction).where(
            sa.or_(
                Interaction.id.in_(scope.interactions),
                Interaction.sender_id.in_(scope.tg_user_ids),
            )
        ),
    )
    observations = await _gone(
        session,
        sa.delete(Observation).where(
            sa.or_(
                Observation.id.in_(scope.observations),
                Observation.company_id.in_(members),
            )
        ),
    )
    signals = await _gone(
        session,
        sa.delete(Signal).where(
            sa.or_(Signal.id.in_(scope.signals), Signal.company_id.in_(members))
        ),
    )
    contacts = await _gone(
        session, sa.delete(Contact).where(Contact.company_id.in_(members))
    )
    live = ModelJob.status.in_(LIVE_JOB_STATUSES)
    # And a request of the subject's company written since group 1 (a draft
    # or a verdict whose model call was in flight, seam review wave 4).
    late = sa.and_(
        ModelJob.company_id.in_(members),
        sa.or_(
            ModelJob.request.is_not(None),
            ModelJob.pseudonyms.is_not(None),
            ModelJob.result.is_not(None),
            live,
        ),
    )
    jobs = await _gone(
        session,
        sa.update(ModelJob)
        .where(sa.or_(ModelJob.id.in_(scope.model_jobs), late))
        .values(
            request=sa.null(),
            pseudonyms=sa.null(),
            result=sa.null(),
            status=sa.case((live, "failed"), else_=ModelJob.status),
            error=sa.case((live, JOB_PURGED), else_=ModelJob.error),
            # An untaken fallback whose request is gone can never be taken:
            # given up, as model_jobs does after its last try.
            applied_at=sa.case(
                (
                    ModelJob.status == "fallback",
                    sa.func.coalesce(ModelJob.applied_at, now),
                ),
                else_=ModelJob.applied_at,
            ),
        ),
    )
    payloads = await _gone(
        session,
        sa.update(PushLog)
        .where(PushLog.company_id.in_(members), PushLog.payload.is_not(None))
        .values(payload=sa.null()),
    )
    # A company that lost signals with the subject's messages is scored again
    # (its company first: candidates' lock order, signals.mark_stale).
    for company_id in scope.bystanders:
        await session.execute(
            sa.select(Company.id)
            .where(Company.id == company_id)
            .with_for_update(key_share=True)
        )
        await signal_service.mark_stale(session, company_id)
    return {
        "interactions": interactions,
        "observations": observations,
        "signals": signals,
        "contacts": contacts,
        "model_jobs": jobs,
        "push_payloads": payloads,
    }


async def _identity(
    session: AsyncSession, scope: Scope
) -> tuple[dict[str, int], list[str]]:
    """Group 3: aliases, platform ids, the subject's own evidence rows."""
    members = list(scope.members)
    aliases = await _gone(
        session, sa.delete(CompanyAlias).where(CompanyAlias.company_id.in_(members))
    )
    ids = await _gone(
        session, sa.delete(CompanyId).where(CompanyId.company_id.in_(members))
    )
    files = await evidence.drop_rows(session, scope.evidence)
    counts = {
        "aliases": aliases,
        "company_ids": ids,
        "evidence": len(scope.evidence),
        "files": len(files),
    }
    return counts, files


async def _lock_lineage(session: AsyncSession, company_ids: Sequence[uuid.UUID]) -> None:
    """``FOR UPDATE`` on every company of the plan's lineages, in id order
    (companies → lead_candidates → lead_reviews)."""
    if not company_ids:
        return
    groups = await companies.lineages(session, sorted(set(company_ids)))
    members = sorted({m for group in groups.values() for m in group})
    await session.execute(
        sa.select(Company.id)
        .where(Company.id.in_(members))
        .order_by(Company.id)
        .with_for_update()
    )


async def execute(
    session: AsyncSession,
    plan: PurgePlan,
    *,
    requested_by: str,
    now: datetime,
    root: Path | None = None,
) -> PurgeResult:
    """Run ``plan`` (06 §5.4), one transaction per table group, files last;
    the plan's log row (written now when the plan has none) gets the counts.
    Raises ``PurgeRefused`` when the row was executed or closed already; any
    other failure leaves ``error='failed:<class>'`` on it and is raised."""
    if plan.purge_log_id is None:
        await record(session, plan, requested_by=requested_by)
        await session.commit()
    purge_log_id = plan.purge_log_id
    assert purge_log_id is not None
    try:
        await _claim(session, purge_log_id)
        # The lineage is locked before its scope is read (seam review wave
        # 4): a push that commits between the read and group 1 made a
        # candidate the stale scope still deleted (push_log RESTRICT), and a
        # job written meanwhile was missed. A push or a verdict holding the
        # company finishes first; the next one waits for the purge.
        await _lock_lineage(session, plan.company_ids)
        scope = await _scope_of(session, plan)
        done = await _stop(session, plan, scope, now=now)
        await session.commit()
        done |= await _people(session, scope, now=now)
        await session.commit()
        identity, files = await _identity(session, scope)
        done |= identity
        await session.commit()
    except PurgeRefused:
        await session.rollback()
        raise
    except Exception as exc:
        await session.rollback()
        await session.execute(
            sa.update(PurgeLog)
            .where(PurgeLog.id == purge_log_id)
            .values(error=f"{FAILED_PREFIX}{type(exc).__name__}"[:200])
        )
        await session.commit()
        log.warning("purge failed", extra={"purge_log_id": purge_log_id})
        raise
    removed = evidence.unlink_raw(files, root=root)
    executed = {key: int(done.get(key, 0)) for key in COUNT_KEYS}
    await _merge_executed(
        session,
        purge_log_id,
        {**executed, "files_removed": removed},
        executed_at=now,
        error=None,
    )
    await session.commit()
    log.info("purge executed", extra={"purge_log_id": purge_log_id, "counts": executed})
    return PurgeResult(purge_log_id, executed, removed)


# --- what the owner reads (06 §5.4: counts, never names) ----------------------------

REASON_UZ: Final = {
    "legal_request": "huquqiy soʻrov",
    "dnc": "yozilmasin",
    "b2c": "B2C",
    "owner": "egasining qarori",
    "merge_cleanup": "birlashtirishdan qolgan",
}
PLAN_TITLE: Final = "🗑 Oʻchirish rejasi · {reason}"
EMPTY_TEXT: Final = "Oʻchiriladigan maʼlumot qolmagan."
COUNT_LINES: Final = (
    "🏢 Kompaniya: {companies} · kontakt: {contacts}",
    "🔎 Kuzatuv: {observations} · Telegram xabar: {interactions} · signal: {signals}",
    "📄 Dalil: {evidence} ({files} fayl) · AI soʻrovi: {model_jobs}",
    "🏷 Nom: {aliases} · platforma ID: {company_ids}",
    "📋 Nomzod: {candidates} oʻchadi, {candidates_ended} yopiladi · karta: {reviews}",
    "📤 Push matni: {push_payloads} · jadval qatori: {sheet_rows} · chat: {chats}",
)
SUPPRESSION_LINE: Final = "🔒 Abadiy bostirish kaliti: {suppression} ta"
NO_SUPPRESSION_LINE: Final = "Bostirish kaliti yozilmaydi (sabab: {reason})"
KEPT_LINE: Final = "Qoladi: ERP natijalari, push jurnali (matnsiz), xarajat jurnali."
ERP_LINE: Final = (
    "⚠️ ERPʼga yuborilgan lid: {erp_leads} ta — ism va telefoni ERPda qoladi: "
    "uni ERPning oʻzida oʻchiring."
)
DONE_TITLE: Final = "✅ Oʻchirildi · {reason}"
FILES_LINE: Final = "Diskdan oʻchirilgan fayl: {removed}"


def _filled(counts: dict[str, int]) -> dict[str, int]:
    return {key: int(counts.get(key, 0)) for key in COUNT_KEYS}


def count_lines(counts: dict[str, int], *, reason: str) -> list[str]:
    values = _filled(counts)
    lines = [line.format(**values) for line in COUNT_LINES]
    if reason in SUPPRESSING:
        lines.append(SUPPRESSION_LINE.format(**values))
    else:
        lines.append(NO_SUPPRESSION_LINE.format(reason=REASON_UZ[reason]))
    if values.get("erp_leads"):
        lines.append(ERP_LINE.format(**values))
    return lines


def plan_lines(plan: PurgePlan) -> list[str]:
    """The plan for the owner, one line each (the tool prints them, the bot
    sends them joined and escaped): counts only."""
    head = PLAN_TITLE.format(reason=REASON_UZ[plan.reason])
    if plan.empty:
        return [head, EMPTY_TEXT]
    return [head, *count_lines(plan.counts, reason=plan.reason), KEPT_LINE]


def result_lines(result: PurgeResult, *, reason: str) -> list[str]:
    return [
        DONE_TITLE.format(reason=REASON_UZ[reason]),
        *count_lines(result.executed, reason=reason),
        FILES_LINE.format(removed=result.files_removed),
    ]


__all__ = [
    "CARDS_KEY",
    "CARDS_WINDOW",
    "COUNT_KEYS",
    "DEFAULT_REASON",
    "LEFTOVER_KEYS",
    "OWNER_REASONS",
    "PURGED_CARD_UZ",
    "PURGED_NAME",
    "PurgePlan",
    "PurgeRefused",
    "PurgeResult",
    "Scope",
    "abandon",
    "close_purged_cards",
    "collect",
    "count_lines",
    "execute",
    "hash_key",
    "plan_company",
    "plan_contact",
    "plan_handle",
    "plan_lines",
    "record",
    "result_lines",
    "row_state",
    "senders_of",
    "subject_root",
    "unfinished",
]
