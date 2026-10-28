"""The do-not-contact lookup and the agent's own DNC events (02 §2.8, 04 §7,
06 §5.5).

**The lookup.** ``build_keys`` turns what a company is known by into the
``(key_type, key)`` pairs the ``suppression`` rows are keyed by, over the
company's **lineage** (a merged source's rows count, 04 §2.5) and exactly as
the writers store them (seam review 2026-10-24): every live contact tail —
and the tail about to be pushed — as a clear ``phone_tail`` *and* as the
feed's ``phone_hash`` under the current ``CRM_AGENT_KEY`` (``erp_feeds.
hash_tail``; a hash row made under an older key carries another ``salt_id``
and cannot match, which is why ``erp_feeds.suppression_fresh`` holds pushes
until a full pull ran); each lineage company's ``website_domain`` as stored
(the host ``services/outcomes`` writes — never ``keys.site_key``, which would
miss ``shop.namuna.uz``); its STIR; its channel and every live contact's
username as ``tg_username``; every live contact's Telegram id; every
Instagram handle; and every lineage id as ``company_id``.

``is_suppressed`` is 02 §2.8's single query: the rows on any of those keys,
a ``cooldown`` only while ``expires_at > now``, ordered by ``KIND_RANK`` —
rendered into ``array_position(ARRAY[…], kind)``, never ``ORDER BY kind``,
which would put ``b2c``/``bounced`` first — so a permanent kind always beats
a cooldown and the gate and ``/lidlar`` report the lasting reason. One hit
on any key suppresses the whole company, not just that contact.

**The agent's own events** (04 §7 «local events», 06 §1.3): the owner's 🔒
on a card and the manager card's «🙅 Yozmang dedi» (``dnc_for_candidate``:
the lead contact's phone tail, username and Instagram plus the company's
channel, Instagram and domain, then the candidate → ``suppressed``);
``/dnc <phone|@user>`` (``dnc_command``); a company-wide kind — gate 2's
``competitor``, the owner's 🙍 ``b2c``, a manager's ``manager_veto`` —
(``suppress_company``). ``client``/``lost`` belong to the ERP feed, the
outcomes recompute and the XLSX import (``ck_suppression_erp_kinds``) and
are never written here.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db import enums as e
from clientscrapper.db.models import Company, Contact, Suppression
from clientscrapper.services import companies, erp_feeds, keys, normalise

if TYPE_CHECKING:
    from clientscrapper.db.models import LeadCandidate

# 06 §5.5: legal 0 · dnc 1 · client 2 · lost 3 · bounced 4 · competitor 5 ·
# manager_veto 6 · b2c 7 · cooldown 8 — the one constant; the lookup's SQL is
# rendered from it and tests/test_suppression.py pins the two equal.
KIND_RANK: Final[Mapping[str, int]] = MappingProxyType(
    {
        "legal": 0,
        "dnc": 1,
        "client": 2,
        "lost": 3,
        "bounced": 4,
        "competitor": 5,
        "manager_veto": 6,
        "b2c": 7,
        "cooldown": 8,
    }
)
KIND_ORDER: Final = tuple(sorted(KIND_RANK, key=KIND_RANK.__getitem__))
KIND_ORDER_SQL: Final = "ARRAY[" + ", ".join(f"'{k}'" for k in KIND_ORDER) + "]"
COOLDOWN: Final = "cooldown"
# The kind each gate reads (04 §6): gate 4 a client, gate 5 the agent's own
# and the ERP's «never again», gate 6 the expiring one.
CLIENT_KINDS: Final = frozenset({"client"})
DNC_KINDS: Final = frozenset(
    {"dnc", "lost", "legal", "bounced", "manager_veto", "b2c", "competitor"}
)
# ck_suppression_key.
KEY_MIN, KEY_MAX = 3, 200
# What this module may write (the ERP's kinds are the feed's, the outcomes'
# and the import's: ck_suppression_erp_kinds).
LOCAL_KINDS: Final = frozenset(
    {"dnc", "bounced", "legal", "competitor", "manager_veto", "b2c", "cooldown"}
)
COMPANY_WIDE_KINDS: Final = frozenset({"competitor", "manager_veto", "b2c"})
OWNER_SOURCE: Final = "owner_bot"

Key = tuple[str, str]  # (key_type, key)


@dataclass(frozen=True, slots=True)
class Hit:
    """The suppression row that decided."""

    id: int
    kind: str
    key_type: str
    key: str
    source: str
    reason: str | None = None
    expires_at: datetime | None = None

    @property
    def permanent(self) -> bool:
        return self.kind != COOLDOWN


@dataclass(frozen=True, slots=True)
class CompanyKeys:
    """What one lineage company is known by (``companies`` columns)."""

    id: uuid.UUID
    stir: str | None = None
    website_domain: str | None = None
    telegram_channel: str | None = None
    instagram: str | None = None


@dataclass(frozen=True, slots=True)
class ContactKeys:
    """What one contact is known by; an anonymised one is known by nothing."""

    phone_tail: str | None = None
    telegram_username: str | None = None
    telegram_id: int | None = None
    instagram: str | None = None
    anonymised: bool = False


def _fits(key: str) -> bool:
    return KEY_MIN <= len(key) <= KEY_MAX


def _tail(raw: str | None) -> str | None:
    """A stored nine-digit tail (``char(9)`` comes back as is; a pushed one
    may carry padding)."""
    if raw is None:
        return None
    tail = raw.strip()
    return tail if len(tail) == 9 and tail.isascii() and tail.isdigit() else None


def build_keys(
    lineage: Sequence[CompanyKeys],
    contacts: Sequence[ContactKeys],
    secret: str,
    *,
    tails: Iterable[str] = (),
) -> list[Key]:
    """Every ``(key_type, key)`` the company is suppressed by (02 §2.8): pure,
    sorted, each pair once. ``tails`` are phones about to be pushed; a blank
    ``secret`` (no ERP key yet) adds no ``phone_hash``."""
    found: set[Key] = set()
    phone_tails = {t for t in (_tail(x) for x in tails) if t}
    for c in contacts:
        if c.anonymised:
            continue
        if (tail := _tail(c.phone_tail)) is not None:
            phone_tails.add(tail)
        if c.telegram_username:
            found.add(("tg_username", c.telegram_username))
        if c.telegram_id is not None:
            found.add(("tg_user_id", str(c.telegram_id)))
        if c.instagram:
            found.add(("instagram", c.instagram))
    for company in lineage:
        found.add(("company_id", str(company.id)))
        if company.stir:
            found.add(("stir", company.stir.strip()))
        if company.website_domain:
            found.add(("domain", company.website_domain))
        if company.telegram_channel:
            found.add(("tg_username", company.telegram_channel))
        if company.instagram:
            found.add(("instagram", company.instagram))
    hashing = bool(secret.strip())
    for tail in phone_tails:
        found.add(("phone_tail", tail))
        if hashing:
            found.add(("phone_hash", erp_feeds.hash_tail(secret, tail)))
    return sorted(k for k in found if _fits(k[1]))


def rank_order() -> sa.ColumnElement[int]:
    """``array_position(ARRAY['legal', …, 'cooldown'], kind)`` — KIND_RANK."""
    return sa.func.array_position(sa.literal_column(KIND_ORDER_SQL), Suppression.kind)


def lookup_statement(
    keys_: Sequence[Key], *, now: datetime
) -> sa.Select[tuple[Suppression]]:
    """02 §2.8's one query."""
    return (
        sa.select(Suppression)
        .where(
            sa.tuple_(Suppression.key_type, Suppression.key).in_(list(keys_)),
            sa.or_(Suppression.expires_at.is_(None), Suppression.expires_at > now),
        )
        .order_by(rank_order(), Suppression.id)
        .limit(1)
    )


async def is_suppressed(
    session: AsyncSession, keys_: Sequence[Key], *, now: datetime
) -> Hit | None:
    """The highest-ranked live row on any of ``keys_``, or None."""
    if not keys_:
        return None
    row = await session.scalar(
        lookup_statement(keys_, now=now).execution_options(populate_existing=True)
    )
    if row is None:
        return None
    return Hit(
        id=row.id,
        kind=row.kind,
        key_type=row.key_type,
        key=row.key,
        source=row.source,
        reason=row.reason,
        expires_at=row.expires_at,
    )


async def keys_for(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tails: Iterable[str] = (),
    secret: str | None = None,
) -> list[Key]:
    """``build_keys`` over ``company_id``'s lineage as stored now; ``secret``
    defaults to ``CRM_AGENT_KEY`` (the key the feed's hashes are made with)."""
    lineage = sorted((await companies.lineages(session, [company_id]))[company_id])
    rows = (
        await session.execute(
            sa.select(
                Company.id,
                Company.stir,
                Company.website_domain,
                Company.telegram_channel,
                Company.instagram,
            )
            .where(Company.id.in_(lineage))
            .order_by(Company.id)
        )
    ).all()
    people = (
        await session.execute(
            sa.select(
                Contact.phone_tail,
                Contact.telegram_username,
                Contact.telegram_id,
                Contact.instagram,
            )
            .where(Contact.company_id.in_(lineage), Contact.anonymised_at.is_(None))
            .order_by(Contact.id)
        )
    ).all()
    return build_keys(
        [
            CompanyKeys(
                id=r.id,
                stir=r.stir,
                website_domain=r.website_domain,
                telegram_channel=r.telegram_channel,
                instagram=r.instagram,
            )
            for r in rows
        ],
        [
            ContactKeys(
                phone_tail=p.phone_tail,
                telegram_username=p.telegram_username,
                telegram_id=p.telegram_id,
                instagram=p.instagram,
            )
            for p in people
        ],
        settings.crm_agent_key if secret is None else secret,
        tails=tails,
    )


async def check_company(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    now: datetime,
    tails: Iterable[str] = (),
) -> Hit | None:
    """``is_suppressed(keys_for(company))`` — before a card and inside the
    push transaction (02 §2.8)."""
    return await is_suppressed(
        session, await keys_for(session, company_id, tails=tails), now=now
    )


# --- the agent's own events ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    """One row to write, its key already in the stored shape."""

    kind: str
    key_type: str
    key: str


def entry(kind: str, key_type: str, raw: str) -> Entry:
    """A row for ``raw``, normalised as the stored keys are (02 §1; the CHECKs
    of ``suppression`` hold the same shapes). Raises ValueError for a kind
    this module may not write, an unknown key type or a raw value that is no
    key."""
    if kind not in LOCAL_KINDS:
        raise ValueError(f"{kind} rows are not written here")
    if key_type not in e.SUPPRESSION_KEY_TYPES or key_type == "phone_hash":
        raise ValueError(f"key type {key_type} is not written here")
    key: str | None
    if key_type == "phone_tail":
        key = _tail(raw) or keys.phone_tail(raw)
    elif key_type == "tg_username":
        key = normalise.telegram_key(raw)
    elif key_type == "instagram":
        key = keys.instagram_key(raw)
    elif key_type == "domain":
        key = keys.domain_key(raw)
    elif key_type == "stir":
        key = keys.stir_key(raw)
    elif key_type == "tg_user_id":
        key = raw.strip() if raw.strip().isascii() and raw.strip().isdigit() else None
    else:  # company_id
        key = str(uuid.UUID(raw))
    if key is None or not _fits(key):
        raise ValueError(f"not a {key_type}")
    return Entry(kind, key_type, key)


async def add(
    session: AsyncSession,
    entries: Iterable[Entry],
    *,
    source: str,
    reason: str | None = None,
    expires_at: datetime | None = None,
) -> list[int]:
    """Write the rows (``uq_suppression_key`` is the dedup: a key already
    held keeps its row, whoever wrote it — it is suppressed either way; a
    cooldown already held keeps the later expiry). Returns the ids in the
    order given."""
    if source not in e.SUPPRESSION_SOURCES:
        raise ValueError(f"unknown suppression source {source}")
    ids: list[int] = []
    for item in entries:
        cooldown = item.kind == COOLDOWN
        if cooldown != (expires_at is not None):
            raise ValueError("a cooldown, and only a cooldown, expires")
        stmt = pg_insert(Suppression).values(
            kind=item.kind,
            key_type=item.key_type,
            key=item.key,
            source=source,
            reason=reason[:200] if reason else None,
            expires_at=expires_at,
        )
        if cooldown:
            stmt = stmt.on_conflict_do_update(
                constraint="uq_suppression_key",
                set_={
                    "expires_at": sa.func.greatest(
                        Suppression.expires_at, stmt.excluded.expires_at
                    )
                },
            )
        else:
            stmt = stmt.on_conflict_do_nothing(constraint="uq_suppression_key")
        written = await session.scalar(stmt.returning(Suppression.id))
        if written is None:
            written = await session.scalar(
                sa.select(Suppression.id).where(
                    Suppression.kind == item.kind,
                    Suppression.key_type == item.key_type,
                    Suppression.key == item.key,
                )
            )
        assert written is not None  # the conflicting row exists
        ids.append(int(written))
    return ids


def dnc_entries(company: Company, contact: Contact | None) -> list[Entry]:
    """06 §1.3 🔒: the lead contact's phone tail, username and Instagram,
    and the company's channel, Instagram and domain — each the stored key."""
    found: list[Entry] = []
    if contact is not None and contact.anonymised_at is None:
        if contact.phone_tail:
            found.append(Entry("dnc", "phone_tail", contact.phone_tail.strip()))
        if contact.telegram_username:
            found.append(Entry("dnc", "tg_username", contact.telegram_username))
        if contact.instagram:
            found.append(Entry("dnc", "instagram", contact.instagram))
    if company.telegram_channel:
        found.append(Entry("dnc", "tg_username", company.telegram_channel))
    if company.instagram:
        found.append(Entry("dnc", "instagram", company.instagram))
    if company.website_domain:
        found.append(Entry("dnc", "domain", company.website_domain))
    unique = list(dict.fromkeys(f for f in found if _fits(f.key)))
    return unique


async def dnc_for_candidate(
    session: AsyncSession,
    candidate: LeadCandidate,
    *,
    now: datetime,
    reason: str | None = None,
    source: str = OWNER_SOURCE,
) -> int:
    """The owner's 🔒 (``lead_reviews.answer='dnc'``) or a manager's «🙅
    Yozmang dedi»: the rows of ``dnc_entries`` — or, when the company has
    none of those keys, its id — and the candidate → ``suppressed``,
    pointing at the first row. Returns that row's id."""
    from clientscrapper.services import candidates  # it imports gates → here

    company = await session.get(Company, candidate.company_id)
    if company is None:
        raise LookupError(f"company {candidate.company_id} is gone")
    contact = (
        await session.get(Contact, candidate.contact_id)
        if candidate.contact_id is not None
        else None
    )
    found = dnc_entries(company, contact)
    if not found:
        found = [Entry("dnc", "company_id", str(company.id))]
    ids = await add(session, found, source=source, reason=reason)
    await candidates.transition(
        session, candidate, "suppressed", now=now, suppressed_by=ids[0]
    )
    return ids[0]


# A handle written as one: «@name» or a t.me link (a bare word is neither).
_HANDLE_FORM: Final = re.compile(
    r"\A(?:@|(?:https?://)?(?:www\.)?(?:t|telegram)\.me/)", re.I
)


def dnc_command_entry(arg: str) -> Entry:
    """``/dnc <phone|@user>`` (06 §1.2): a phone → its tail, else a Telegram
    handle written as one (``@name``, ``t.me/name``) — a bare word is
    neither. ValueError for anything else."""
    text = arg.strip()
    phone = normalise.phone_e164(text)
    if phone is not None:
        tail = keys.phone_tail(phone)
        if tail is not None:
            return Entry("dnc", "phone_tail", tail)
    if _HANDLE_FORM.match(text):
        handle = normalise.telegram_key(text)
        if handle is not None:
            return Entry("dnc", "tg_username", handle)
    raise ValueError("neither a phone nor a Telegram handle")


async def dnc_command(
    session: AsyncSession, arg: str, *, reason: str | None = None
) -> tuple[Entry, int]:
    """Write ``/dnc``'s row (``source='owner_bot'``); the bot confirms first."""
    item = dnc_command_entry(arg)
    [row_id] = await add(session, [item], source=OWNER_SOURCE, reason=reason)
    return item, row_id


async def suppress_company(
    session: AsyncSession,
    company_id: uuid.UUID,
    kind: str,
    *,
    source: str,
    reason: str | None = None,
) -> int:
    """A company-wide row keyed by ``company_id`` — gate 2's ``competitor``
    (``source='resolver'``), the owner's 🙍 ``b2c``, a ``manager_veto``."""
    if kind not in COMPANY_WIDE_KINDS:
        raise ValueError(f"{kind} is not a company-wide kind")
    [row_id] = await add(
        session,
        [Entry(kind, "company_id", str(company_id))],
        source=source,
        reason=reason,
    )
    return row_id


__all__ = [
    "CLIENT_KINDS",
    "COOLDOWN",
    "DNC_KINDS",
    "KIND_ORDER",
    "KIND_ORDER_SQL",
    "KIND_RANK",
    "CompanyKeys",
    "ContactKeys",
    "Entry",
    "Hit",
    "Key",
    "add",
    "build_keys",
    "check_company",
    "dnc_command",
    "dnc_command_entry",
    "dnc_entries",
    "dnc_for_candidate",
    "entry",
    "is_suppressed",
    "keys_for",
    "lookup_statement",
    "rank_order",
    "suppress_company",
]
