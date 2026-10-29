# VENDORED_FROM dailyJournal@6b1f67f miya/services/people.py
"""Company resolution (04 §2) — MIYA's person resolution, adapted to firms.

An observation (02 §2.5) names a firm the way the page wrote it: «"NAMUNA
TEKSTIL SAVDO" MChJ», «ООО «Намуна Текстиль Савдо»», a phone, a channel, an
hh.uz employer id. ``resolve_company`` turns it into one ``companies`` row:

1. **Exact keys first** (§2.1), in precedence order, first hit wins: the
   poster's Telegram id (MIYA's ``telegram_id`` shortcut, §2.6) → STIR → phone
   tail (not shared, not anonymised; a landline only where the cities agree)
   → Telegram username → website (folded by ``site_key``) → Instagram →
   platform ids. A hit on a merged company follows ``merged_into`` to the
   root. A later key that names a *different* company is a conflict: logged,
   never merged, never carried, and the other company gets ``dup_of`` when
   the names are close (the owner's 🔀). The name is the last key: under an
   exact hit, a name that is another company's is a conflict too.
2. **Then the name** (§2.2): an exact ``name_key`` or alias wins, else
   rapidfuzz over ``loose_key`` on pg_trgm candidates — ≥ 90 attaches, 80–89
   creates a new company with ``dup_of``, < 80 creates. Generic-only names,
   short keys and pairs of person-shaped keys never fuzzy-match.
3. **The winner absorbs the observation's identity** (``_carry_identity``):
   empty fields filled, phones and handles as contacts, platform ids as
   ``company_ids``, the spelling as an alias — after an exact hit only when
   it reads as the winner's own name (``_name_agrees``, MIYA's rule).

Every key the observation carries is held under a transaction-scoped
advisory lock before the lookup (MIYA's pattern, widened from the name to
every key), so two workers resolving the same new phone, STIR or name cannot
both find nothing and both create.

Merging (§2.5) re-points the source's rows onto the target and records every
moved id in ``merge_log``, which is what makes ``unmerge`` (24 h) exact.

What is not here: signals and the ``found`` candidate (T-18 plugs them in
with ``register_after_resolve``), enrichment, the scheduler (``resolve_job``
is the ``job`` of T-08's ``resolve`` slot), and the bot's 🔀 button (it calls
``merge_into(by='owner:<id>')``).
"""

from __future__ import annotations

import logging
import re
import traceback
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import sqlalchemy as sa
from rapidfuzz import fuzz
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db import enums as e
from clientscrapper.db.models import (
    Company,
    CompanyAlias,
    CompanyId,
    Contact,
    LeadCandidate,
    MergeLog,
    ModelJob,
    Observation,
    Signal,
    UsageLog,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services.contacts import anonymise
from clientscrapper.services.keys import (
    instagram_key,
    loose_key,
    name_key,
    phone_e164,
    phone_kind,
    phone_tail,
    site_key,
    stir_key,
    username_key,
)
from clientscrapper.services.normalise import (
    GENERIC_TOKENS,
    is_person_shaped,
    legal_form_code,
    website_key,
)
from clientscrapper.services.signals import mark_stale
from clientscrapper.services.text import fold_apostrophes, to_latin

log = logging.getLogger(__name__)
# The package's own files, to name where a failure came through (_failure_facts).
_PACKAGE_DIR: Final = Path(__file__).resolve().parents[1]

# --- Bands (04 §2.2, re-tuned from MIYA's 85/70/10) ----------------------------

AUTO_MERGE: Final = 90
REVIEW_LOW: Final = 80
AMBIGUITY_MARGIN: Final = 5
# /qidir and the merge card answer a question; a wrong guess costs a wrong
# answer, not a wrong merge (MIYA's QUESTION_THRESHOLD, kept).
QUESTION_THRESHOLD: Final = 70
# A match found only by gluing tokens («Tex Snab» = «TexSnab») is a proposal
# for the owner, never an attach: it stops one point under AUTO_MERGE.
GLUED_CEILING: Final = AUTO_MERGE - 1
MIN_NAME_KEY: Final = 4
FUZZY_CANDIDATES: Final = 50
TRGM_THRESHOLD: Final = "0.3"

# A phone on this many unrelated companies is an accountant, a broker or a
# business centre (04 §2.3).
SHARED_PHONE_MIN: Final = 3
UNMERGE_WINDOW: Final = timedelta(hours=24)
# Advisory lock namespace (MIYA: hashtext('miya:person')).
LOCK_CLASS: Final = "cs:company"
# A label stands where a firm's name was unknown or a person's («OLX sotuvchi
# AujTE», 04 §1.6). Its stored name_key starts with a character name_key()
# never produces, so no exact or fuzzy name match can reach it: two labels
# share their words and would fuzzy-match each other into one company.
LABEL_PREFIX: Final = "#"
_ROOT_HOPS: Final = 16
RESOLVE_LIMIT: Final = 200
TELEGRAM_SOURCE: Final = "telegram_monitor"
# A candidate the merge must leave alone: a push may be claiming it.
_OPEN_STATES: Final = ("found", "enriching", "scored", "review", "approved", "hold")

# --- GSR's own identity never becomes a company (04 §2.6 _self_guard) ----------

SELF_NAME_KEYS: Final = frozenset(
    {"gsr logistics", "gsr logistika", "imex services", "imex servis"}
)
SELF_DOMAINS: Final = frozenset({"gsrlogistics.uz", "gsrwms.uz"})
SELF_CHANNELS: Final = frozenset({"gsrlogistics", "gsrgroupchat"})

How = Literal[
    "stir", "phone", "telegram", "domain", "instagram", "external_id", "fuzzy", "new"
]

# observations.parsed.handles key → company_ids.id_type (04 §1.6 renames). A
# key that already is an id type (``myorg``, ``exhibitor_slug``) maps to itself;
# ``instagram`` is the handle, not an id, and ``tg_user_id`` is person-tier.
_HANDLE_ID_TYPES: Final = {
    "olx_user_id": "olx_user",
    "hh_employer_id": "hh_employer",
    "gis2_firm_id": "gis2_firm",
    "google_place_id": "google_place",
    "uzum_shop": "uzum_shop",
    "tg_chat": "tg_chat",
}
_NOT_ID_HANDLES: Final = frozenset({"telegram", "tg_user_id", "instagram"})
_ID_VALUE_MAX: Final = 200


class ObservationRejected(Exception):
    """The observation names nothing the agent may keep: GSR itself
    (``competitor``), nobody at all (``no_company``), or a company that was
    purged on request (``purged`` — its data is not collected again)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MergeRefused(Exception):
    """Two companies that cannot be one (different STIRs, channels,
    registry-confirmed sites), or a merge that would break a running push."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnmergeRefused(Exception):
    """The merge cannot be undone by the machine: too late (24 h), already
    undone, or the rows moved on since. The owner splits by hand."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class KeyConflict(Exception):
    """MIYA's IdentityConflict: two exact keys of one observation name two
    companies. Logged, never raised past the resolver (04 §2.6)."""

    def __init__(self, key_type: str, winner: uuid.UUID, other: uuid.UUID) -> None:
        super().__init__(f"{key_type}: {winner} vs {other}")
        self.key_type = key_type
        self.winner = winner
        self.other = other


# --- The observation's identity (pure) -------------------------------------------


@dataclass(frozen=True, slots=True)
class PhoneKey:
    tail: str
    e164: str
    kind: str  # mobile | landline
    owner: str  # person | company | unknown


@dataclass(frozen=True, slots=True)
class IdentityKeys:
    """What an observation says about who it is (04 §2.1, plus what
    ``_carry_identity`` stores: the person, the display name, the URL)."""

    stir: str | None
    phone_tails: tuple[str, ...]  # UZ mobile+landline, owner known or a poster's
    tg_username: str | None  # company channel OR poster username
    site_key: str | None
    instagram: str | None
    external_ids: tuple[tuple[str, str], ...]  # (id_type, id_value) as in company_ids
    name_key: str | None  # None: a label or no name (never name-matched)
    city: str | None
    kind: str = ""
    tg_user_id: int | None = None  # a poster's Telegram id: person-tier
    domain: str | None = None  # the stored website_domain (site_key folds it)
    phones: tuple[PhoneKey, ...] = ()
    landline_tails: frozenset[str] = frozenset()
    display_name: str | None = None
    legal_form: str | None = None
    person_name: str | None = None
    person_role: str | None = None
    url: str | None = None

    @property
    def person_side_handles(self) -> bool:
        """The Telegram/Instagram handle is the poster's, not the firm's: a
        group post's sender, or an OLX seller who is a person (04 §9.2)."""
        return self.kind == "intent_post" or (
            self.kind == "seller" and self.person_name is not None
        )

    def any_exact(self) -> bool:
        return bool(
            self.stir
            or self.phone_tails
            or self.tg_username
            or self.site_key
            or self.instagram
            or self.external_ids
            or self.tg_user_id is not None
        )


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = " ".join(value.split())
    return stripped or None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _id_type(handle_key: str) -> str | None:
    if handle_key in _NOT_ID_HANDLES:
        return None
    mapped = _HANDLE_ID_TYPES.get(handle_key, handle_key)
    if mapped in e.ID_TYPES:
        return mapped
    stem = handle_key.removesuffix("_id")
    return stem if stem != handle_key and stem in e.ID_TYPES else None


def _id_value(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value).strip()
    return text if 1 <= len(text) <= _ID_VALUE_MAX else None


def identity_keys(parsed: Mapping[str, Any]) -> IdentityKeys:
    """The 02 §2.5 ``parsed`` JSON → the keys the ladder looks up. Defensive
    about shapes (a field of the wrong type is absent) and re-keys every
    handle, so a hand-edited row cannot smuggle a raw value into a CHECKed
    column."""
    kind = _text(parsed.get("kind")) or ""
    company = _mapping(parsed.get("company"))
    handles = _mapping(parsed.get("handles"))
    person = _mapping(parsed.get("person"))

    phones: list[PhoneKey] = []
    seen: set[str] = set()
    raw_phones = parsed.get("phones")
    for item in raw_phones if isinstance(raw_phones, list) else []:
        entry = _mapping(item)
        e164 = phone_e164(_text(entry.get("e164")))
        if e164 is None or not e164.startswith("+998"):
            continue
        tail = phone_tail(e164)
        kind_of = phone_kind(e164)
        owner = _text(entry.get("owner")) or "unknown"
        if tail is None or tail in seen or kind_of not in ("mobile", "landline"):
            continue
        if owner == "unknown" and kind != "intent_post":
            continue
        seen.add(tail)
        phones.append(PhoneKey(tail=tail, e164=e164, kind=kind_of, owner=owner))

    domain = website_key(_text(company.get("website")))
    external: list[tuple[str, str]] = []
    for raw_key, value in handles.items():
        id_type = _id_type(str(raw_key))
        id_value = _id_value(value)
        # A group post's chat is the group's, shared by every poster in it.
        if (
            id_type is None
            or id_value is None
            or (id_type == "tg_chat" and kind == "intent_post")
        ):
            continue
        if (id_type, id_value) not in external:
            external.append((id_type, id_value))

    tg_user_raw = handles.get("tg_user_id")
    tg_user_id: int | None = None
    if isinstance(tg_user_raw, int) and not isinstance(tg_user_raw, bool):
        tg_user_id = tg_user_raw
    elif isinstance(tg_user_raw, str) and tg_user_raw.isdigit():
        tg_user_id = int(tg_user_raw)

    key = _text(parsed.get("name_key"))
    display = _text(company.get("name_raw"))
    if display is not None and len(display) < 2:
        display = None
    # A vacancy names the employer by its hh id; a handle on it is the HR
    # person's, and HR people are never kept (03 §2.7, 04 §2.4) — not as a
    # contact, not as the firm's channel, not as a key.
    hr_handles = kind == "vacancy"
    return IdentityKeys(
        stir=stir_key(_text(company.get("stir"))),
        phone_tails=tuple(p.tail for p in phones),
        tg_username=None if hr_handles else username_key(_text(handles.get("telegram"))),
        site_key=site_key(domain) if domain else None,
        instagram=None if hr_handles else instagram_key(_text(handles.get("instagram"))),
        external_ids=tuple(external),
        name_key=key if key and not key.startswith(LABEL_PREFIX) else None,
        city=_text(company.get("city")),
        kind=kind,
        tg_user_id=tg_user_id,
        domain=domain,
        phones=tuple(phones),
        landline_tails=frozenset(p.tail for p in phones if p.kind == "landline"),
        display_name=display[:160].rstrip() if display else None,
        legal_form=legal_form_code(_text(company.get("legal_form"))),
        person_name=_text(person.get("name")),
        person_role=_text(person.get("role")),
        url=_text(parsed.get("url")),
    )


def self_refusal(keys: IdentityKeys) -> str | None:
    """'competitor' when the observation is GSR itself (its names, its sites,
    its channels) — the agent's own firm is never a lead."""
    if keys.name_key and (
        keys.name_key in SELF_NAME_KEYS
        or loose_key(keys.name_key) in {loose_key(k) for k in SELF_NAME_KEYS}
    ):
        return "competitor"
    if keys.site_key in SELF_DOMAINS or keys.tg_username in SELF_CHANNELS:
        return "competitor"
    return None


# --- Names (pure) -----------------------------------------------------------------


def usable_name(key: str | None) -> bool:
    """A key the name ladder may use at all (04 §2.2 guards): not a label, at
    least four characters, at least one token that is not a generic business
    word. «Optom Savdo» names thousands of shops; «ooo» is no name."""
    if not key or key.startswith(LABEL_PREFIX) or len(key) < MIN_NAME_KEY:
        return False
    return any(token not in GENERIC_TOKENS for token in key.split())


def _glue(tokens: list[str], others: set[str]) -> list[str]:
    """Join runs of adjacent tokens whose concatenation is one of ``others``'
    tokens: ["teh", "snab", "import"] against {"tehsnab"} → ["tehsnab",
    "import"]."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        for j in range(len(tokens), i + 1, -1):
            joined = "".join(tokens[i:j])
            if joined in others:
                out.append(joined)
                i = j
                break
        else:
            out.append(tokens[i])
            i += 1
    return out


def _score(a: str, b: str) -> float:
    """rapidfuzz ``token_set_ratio`` over ``loose_key`` (04 §2.2), plus one
    reading 04 does not have: a name written glued on one side and spaced on
    the other («TexSnab» / «Tex Snab Import»), which token_set_ratio scores 45
    because no token is shared. The glued comparison counts, but never above
    GLUED_CEILING — 07 §1.4 puts that pair in the review band, not the attach
    band."""
    la, lb = loose_key(a), loose_key(b)
    plain = float(fuzz.token_set_ratio(la, lb))
    ta, tb = la.split(), lb.split()
    ga = " ".join(_glue(ta, set(tb)))
    gb = " ".join(_glue(tb, set(ta)))
    if ga == la and gb == lb:
        return plain
    glued = float(max(fuzz.token_set_ratio(ga, lb), fuzz.token_set_ratio(la, gb)))
    return max(plain, min(glued, float(GLUED_CEILING))) if glued > plain else plain


def _pair_score(query: str, key: str, *, guarded: bool = True) -> float:
    """The score of one candidate key, or 0 when a guard refuses the pair."""
    if query == key:
        return 100.0
    if guarded and not usable_name(key):
        return 0.0
    if key.startswith(LABEL_PREFIX):
        return 0.0
    score = _score(query, key)
    # Two person-shaped keys never fuzzy-match below 100 («Karimov R R» and
    # «Karimova R» are two people, whatever rapidfuzz says).
    if guarded and score < 100 and is_person_shaped(query) and is_person_shaped(key):
        return 0.0
    return score


@dataclass(frozen=True, slots=True)
class NameCandidate:
    company_id: uuid.UUID
    keys: tuple[str, ...]  # name_key and the alias keys that came back
    city: str | None
    first_seen_at: datetime


@dataclass(frozen=True, slots=True)
class NameMatch:
    """MIYA's ``Match``: the best company for a name and the best other one."""

    company_id: uuid.UUID | None
    score: float
    runner_up_id: uuid.UUID | None = None
    runner_up_score: float = 0.0
    exact: bool = False

    @property
    def ambiguous(self) -> bool:
        """Two companies both in the attach band and within the margin: the
        owner decides, the machine does not (04 §2.2)."""
        return (
            not self.exact
            and self.company_id is not None
            and self.runner_up_id is not None
            and self.runner_up_score >= AUTO_MERGE
            and self.score - self.runner_up_score <= AMBIGUITY_MARGIN
        )


FuzzyDecision = Literal["attach", "review", "new"]


def _city_ok(a: str | None, b: str | None) -> bool:
    return a is None or b is None or a == b


def best_name_match(
    query: str | None,
    candidates: Sequence[NameCandidate],
    *,
    city: str | None = None,
    guarded: bool = True,
) -> NameMatch:
    """The best candidate for ``query`` (MIYA ``best_match``): an exact key or
    alias wins before any scoring — the same city first, then the oldest —
    else the highest score per company over its keys."""
    if not query or (guarded and not usable_name(query)):
        return NameMatch(company_id=None, score=0.0)
    pool = [c for c in candidates if _city_ok(city, c.city)]
    exact = sorted(
        (c for c in pool if query in c.keys),
        key=lambda c: (
            city is None or c.city != city,
            c.first_seen_at,
            str(c.company_id),
        ),
    )
    if exact:
        runner = next((c for c in exact[1:] if c.company_id != exact[0].company_id), None)
        return NameMatch(
            company_id=exact[0].company_id,
            score=100.0,
            runner_up_id=runner.company_id if runner else None,
            runner_up_score=100.0 if runner else 0.0,
            exact=True,
        )
    best: dict[uuid.UUID, tuple[float, NameCandidate]] = {}
    for candidate in pool:
        score = max(
            (_pair_score(query, k, guarded=guarded) for k in candidate.keys), default=0.0
        )
        held = best.get(candidate.company_id)
        if held is None or score > held[0]:
            best[candidate.company_id] = (score, candidate)
    ranked = sorted(
        best.values(),
        key=lambda pair: (-pair[0], pair[1].first_seen_at, str(pair[1].company_id)),
    )
    if not ranked or ranked[0][0] <= 0:
        return NameMatch(company_id=None, score=0.0)
    (score, top), rest = ranked[0], ranked[1:]
    return NameMatch(
        company_id=top.company_id,
        score=score,
        runner_up_id=rest[0][1].company_id if rest else None,
        runner_up_score=rest[0][0] if rest else 0.0,
    )


def fuzzy_decision(match: NameMatch) -> FuzzyDecision:
    """≥ 90 attach, 80–89 (or an ambiguous ≥ 90) review, else new (04 §2.2)."""
    if match.company_id is None:
        return "new"
    if match.exact:
        return "attach"
    if match.score >= AUTO_MERGE and not match.ambiguous:
        return "attach"
    if match.score >= REVIEW_LOW:
        return "review"
    return "new"


# --- Decision makers (04 §2.4) ------------------------------------------------------

# Matched on the role folded to lower-case Latin (to_latin: ж → j, so
# «менеджер» reads «menedjer»), so «Директор», «direktori» and «Director» are
# one rank. Rank 3 is the firm's head and owner only (04 §2.4): «bosh»
# («chief») is not a head word — «bosh hisobchi», «bosh muhandis» are a chief
# accountant and a chief engineer — and an assistant or secretary to the head
# (``_NOT_THE_HEAD``) is not the head, whatever word follows.
_HEAD: Final = re.compile(
    r"direkt|director|\brahbar|\brakhbar|rukovod|owner|egasi|asoschi|uchredit|"
    r"founder|\bceo\b|boshliq|prezident|vladel"
)
_NOT_THE_HEAD: Final = re.compile(
    r"yordamchi|kotib|sekretar|secretar|pomosh|assist|referent"
)
_ROLE_RANKS: Final = (
    (3, _HEAD),
    (
        2,
        re.compile(
            r"ta'?minot|zakup|snabzh|snabj|\bved\b|vneshne|xarid|import|"
            r"logist|supply|procure"
        ),
    ),
    (
        1,
        re.compile(
            r"menejer|menedjer|menedzher|manager|sotuv|prodaj|prodazh|sales|savdo"
        ),
    ),
)


def role_rank(role: str | None) -> int:
    """ROLE_RANK: direktor/rahbar/owner/учредитель 3 > taʼminot/закупки/ВЭД 2
    > menejer/sotuv 1 > unknown 0."""
    if not role:
        return 0
    folded = to_latin(fold_apostrophes(role)).lower()
    for rank, pattern in _ROLE_RANKS:
        if rank == 3 and _NOT_THE_HEAD.search(folded):
            continue
        if pattern.search(folded):
            return rank
    return 0


# A procurement or VED person chooses the forwarder as much as a director does.
DECISION_MAKER_RANK: Final = 2


# --- Resolution -----------------------------------------------------------------------


@dataclass(slots=True)
class Resolution:
    company: Company
    how: How
    score: float  # 100 for exact, the fuzzy score, 0 for new
    created: bool
    duplicate_of: Company | None  # the 80–89 review band
    conflict: tuple[str, uuid.UUID] | None  # a lower-precedence key pointed elsewhere
    contact: Contact | None = None  # the contact this observation is about
    conflicts: tuple[tuple[str, uuid.UUID], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class _Hit:
    how: How
    key: str
    company: Company


def _lock_keys(keys: IdentityKeys) -> list[str]:
    """The advisory lock arguments, sorted (one global order, no deadlock).
    The bare name_key is 04 §2.2's own lock; the other keys are prefixed."""
    found: set[str] = set()
    if keys.tg_user_id is not None:
        found.add(f"tgid:{keys.tg_user_id}")
    if keys.stir:
        found.add(f"stir:{keys.stir}")
    found.update(f"phone:{t}" for t in keys.phone_tails)
    if keys.tg_username:
        found.add(f"tg:{keys.tg_username}")
    if keys.site_key:
        found.add(f"site:{keys.site_key}")
    if keys.instagram:
        found.add(f"ig:{keys.instagram}")
    found.update(f"ext:{t}:{v}" for t, v in keys.external_ids)
    if keys.name_key:
        found.add(keys.name_key)
    elif keys.display_name:
        found.add(_label_key(keys.display_name))
    return sorted(found)


async def _lock(session: AsyncSession, keys: IdentityKeys) -> None:
    for argument in _lock_keys(keys):
        await session.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(
                    sa.func.hashtext(LOCK_CLASS), sa.func.hashtext(argument)
                )
            )
        )


async def _root(session: AsyncSession, company: Company) -> Company:
    """Follow ``merged_into`` to the company that holds the rows now."""
    current = company
    for _ in range(_ROOT_HOPS):
        if current.state != "merged" or current.merged_into is None:
            return current
        nxt = await session.get(Company, current.merged_into)
        if nxt is None:
            return current
        current = nxt
    return current


async def _companies(
    session: AsyncSession, stmt: sa.Select[tuple[Company]]
) -> list[Company]:
    rows = (await session.scalars(stmt)).unique().all()
    return list(rows)


def _by_age(stmt: sa.Select[tuple[Company]]) -> sa.Select[tuple[Company]]:
    return stmt.order_by(Company.first_seen_at, Company.id)


def _via_contacts(*where: sa.ColumnElement[bool]) -> sa.Select[tuple[Company]]:
    return _by_age(
        sa.select(Company)
        .join(Contact, Contact.company_id == Company.id)
        .where(Contact.anonymised_at.is_(None), *where)
        .distinct()
    )


async def _exact_hits(session: AsyncSession, keys: IdentityKeys) -> list[_Hit]:
    """Every company an exact key names, in the ladder's precedence order,
    roots only. The first is the winner; any other is a conflict."""
    raw: list[tuple[How, str, Company]] = []
    if keys.tg_user_id is not None:
        for c in await _companies(
            session, _via_contacts(Contact.telegram_id == keys.tg_user_id)
        ):
            raw.append(("telegram", f"tgid:{keys.tg_user_id}", c))
    if keys.stir:
        for c in await _companies(
            session, _by_age(sa.select(Company).where(Company.stir == keys.stir))
        ):
            raw.append(("stir", keys.stir, c))
    for tail in keys.phone_tails:
        where = [Contact.phone_tail == tail, Contact.shared_phone.is_(False)]
        if tail in keys.landline_tails and keys.city is not None:
            # A landline tail is a city's number: the same digits in another
            # city are another firm.
            where.append(sa.or_(Company.city.is_(None), Company.city == keys.city))
        for c in await _companies(session, _via_contacts(*where)):
            raw.append(("phone", tail, c))
    if keys.tg_username:
        u = keys.tg_username
        for c in await _companies(
            session, _by_age(sa.select(Company).where(Company.telegram_channel == u))
        ):
            raw.append(("telegram", f"tg:{u}", c))
        for c in await _companies(session, _via_contacts(Contact.telegram_username == u)):
            raw.append(("telegram", f"tg:{u}", c))
    if keys.site_key:
        site = keys.site_key
        stmt = sa.select(Company).where(
            sa.or_(
                Company.website_domain == site, Company.website_domain.like(f"%.{site}")
            )
        )
        for c in await _companies(session, _by_age(stmt)):
            if c.website_domain and site_key(c.website_domain) == site:
                raw.append(("domain", site, c))
    if keys.instagram:
        ig = keys.instagram
        for c in await _companies(
            session, _by_age(sa.select(Company).where(Company.instagram == ig))
        ):
            raw.append(("instagram", f"ig:{ig}", c))
        for c in await _companies(session, _via_contacts(Contact.instagram == ig)):
            raw.append(("instagram", f"ig:{ig}", c))
    if keys.external_ids:
        by_id = (
            sa.select(Company, CompanyId.id_type, CompanyId.id_value)
            .join(CompanyId, CompanyId.company_id == Company.id)
            .where(
                sa.tuple_(CompanyId.id_type, CompanyId.id_value).in_(keys.external_ids)
            )
        )
        found = {(t, v): c for c, t, v in (await session.execute(by_id)).all()}
        for pair in keys.external_ids:
            if pair in found:
                raw.append(("external_id", f"ext:{pair[0]}:{pair[1]}", found[pair]))
    hits: list[_Hit] = []
    for how, key, company in raw:
        hits.append(_Hit(how=how, key=key, company=await _root(session, company)))
    return hits


def _label_key(display: str) -> str:
    return (LABEL_PREFIX + name_key(display))[:160]


def _nameless_label(keys: IdentityKeys, observation_id: int) -> str:
    """The display name of a firm nobody named: never the poster's handle or
    number (person-tier), only where it was seen."""
    if keys.kind == "intent_post":
        return f"Telegram mijoz #{observation_id}"
    return f"Nomaʼlum firma #{observation_id}"


async def _name_candidates(
    session: AsyncSession, key: str, *, city: str | None
) -> list[NameCandidate]:
    """pg_trgm candidates (04 §2.2): the ``%`` operator reads the GIN indexes
    of both tables; live companies only, cities compatible, labels never."""
    await session.execute(
        sa.select(
            sa.func.set_config("pg_trgm.similarity_threshold", TRGM_THRESHOLD, True)
        )
    )
    union = sa.union(
        sa.select(
            CompanyAlias.company_id.label("id"), CompanyAlias.alias_key.label("key")
        ).where(CompanyAlias.alias_key.op("%")(key)),
        sa.select(Company.id.label("id"), Company.name_key.label("key")).where(
            Company.name_key.op("%")(key)
        ),
    ).subquery()
    stmt = (
        sa.select(Company.id, union.c.key, Company.city, Company.first_seen_at)
        .join(union, union.c.id == Company.id)
        .where(
            Company.state.not_in(("merged", "purged")),
            ~Company.name_key.startswith(LABEL_PREFIX),
        )
        .order_by(sa.func.similarity(union.c.key, key).desc(), Company.id)
        .limit(FUZZY_CANDIDATES)
    )
    if city is not None:
        stmt = stmt.where(sa.or_(Company.city.is_(None), Company.city == city))
    return _group_candidates((await session.execute(stmt)).all())


async def _exact_name_candidates(session: AsyncSession, key: str) -> list[NameCandidate]:
    """Every live company holding ``key`` as its name_key or an alias. The
    city rule is ``best_name_match``'s alone (one home); the trigram query
    filters cities in SQL only so other cities cannot crowd its 50 rows."""
    alias = sa.select(CompanyAlias.company_id).where(CompanyAlias.alias_key == key)
    stmt = sa.select(
        Company.id, Company.name_key, Company.city, Company.first_seen_at
    ).where(
        sa.or_(Company.name_key == key, Company.id.in_(alias)),
        Company.state.not_in(("merged", "purged")),
    )
    rows = (await session.execute(stmt)).all()
    # The query matched on name_key or an alias: either way the key is held.
    return [NameCandidate(r[0], (key,), r[2], r[3]) for r in rows]


def _group_candidates(rows: Iterable[Sequence[Any]]) -> list[NameCandidate]:
    grouped: dict[uuid.UUID, tuple[list[str], str | None, datetime]] = {}
    for company_id, key, city, seen in rows:
        held = grouped.setdefault(company_id, ([], city, seen))
        if key not in held[0]:
            held[0].append(key)
    return [NameCandidate(cid, tuple(k), c, s) for cid, (k, c, s) in grouped.items()]


async def _name_match(session: AsyncSession, keys: IdentityKeys) -> NameMatch:
    key = keys.name_key
    if key is None or not usable_name(key):
        return NameMatch(company_id=None, score=0.0)
    exact = await _exact_name_candidates(session, key)
    if exact:
        match = best_name_match(key, exact, city=keys.city)
        if match.company_id is not None:
            return match
        # Every holder of the exact name is in another city: other firms. This
        # city's close names still get their trigram look.
    candidates = await _name_candidates(session, key, city=keys.city)
    return best_name_match(key, candidates, city=keys.city)


async def resolve_company(
    session: AsyncSession, obs: Observation, *, source_key: str, now: datetime
) -> Resolution:
    """Find or create the company ``obs`` is about and give it the
    observation's identity (04 §2.1–2.4). Runs in the caller's transaction;
    raises ``ObservationRejected`` for GSR itself, a nameless keyless
    observation, or a purged company."""
    keys = identity_keys(obs.parsed)
    if (reason := self_refusal(keys)) is not None:
        raise ObservationRejected(reason)
    if not keys.any_exact() and keys.name_key is None and keys.display_name is None:
        raise ObservationRejected("no_company")
    await _lock(session, keys)

    hits = await _exact_hits(session, keys)
    winner: Company | None = None
    how: How = "new"
    score = 0.0
    duplicate_of: Company | None = None
    dup_score = 0
    created = False
    if hits:
        first = hits[0]
        if first.company.state == "purged":
            raise ObservationRejected("purged")
        winner, how, score = first.company, first.how, 100.0
    else:
        match = await _name_match(session, keys)
        decision = fuzzy_decision(match)
        if decision == "attach" and match.company_id is not None:
            winner = await session.get(Company, match.company_id)
            how, score = "fuzzy", match.score
        elif decision == "review" and match.company_id is not None:
            duplicate_of = await session.get(Company, match.company_id)
            dup_score = round(match.score)
        if winner is None:
            winner = _new_company(keys, obs.id, now=now)
            if duplicate_of is not None:
                winner.dup_of = duplicate_of.id
                winner.dup_score = dup_score
            session.add(winner)
            await session.flush()
            created = True

    conflicts: list[tuple[str, uuid.UUID]] = []
    foreign: set[str] = set()
    for hit in hits:
        if hit.company.id == winner.id:
            continue
        # The key is another company's: never carried. A purged company keeps
        # its STIR and domain under the unique indexes, and its data is not
        # collected again (06 §5.4) — but it is nobody's conflict or 🔀.
        foreign.add(hit.key)
        if hit.company.state == "purged":
            continue
        if (hit.how, hit.company.id) not in conflicts:
            conflicts.append((hit.how, hit.company.id))
            await _note_conflict(session, winner, hit)

    learn_name = how in ("fuzzy", "new") or await _name_agrees(
        session, winner, keys.name_key
    )
    if not learn_name and keys.name_key is not None:
        # An exact key under another firm's name (a broker's phone, a misread
        # number): the name is the ladder's last key, and when it names a
        # different company that is a conflict like any other (04 §2.1).
        other = await _name_match(session, keys)
        if (
            fuzzy_decision(other) == "attach"
            and other.company_id is not None
            and other.company_id != winner.id
            and ("fuzzy", other.company_id) not in conflicts
        ):
            named = await session.get(Company, other.company_id)
            if named is not None:
                conflicts.append(("fuzzy", named.id))
                await _note_conflict(
                    session, winner, _Hit(how="fuzzy", key=keys.name_key, company=named)
                )

    contact = await _carry_identity(
        session,
        winner,
        keys,
        foreign=foreign,
        learn_name=learn_name,
        source_key=source_key,
        now=now,
    )
    return Resolution(
        company=winner,
        how=how,
        score=score,
        created=created,
        duplicate_of=duplicate_of,
        conflict=conflicts[0] if conflicts else None,
        contact=contact,
        conflicts=tuple(conflicts),
    )


def _new_company(keys: IdentityKeys, observation_id: int, *, now: datetime) -> Company:
    display = keys.display_name or _nameless_label(keys, observation_id)
    key = keys.name_key or _label_key(display)
    return Company(
        display_name=display,
        name_key=key,
        legal_form=keys.legal_form,
        city=keys.city,
        state="active",
        first_seen_at=now,
        dup_of=None,
        dup_score=None,
    )


async def _name_agrees(session: AsyncSession, company: Company, key: str | None) -> bool:
    """Whether the observation's name reads as ``company``'s own (its name or
    a spelling it holds, in the attach band). An exact key does not make a
    name the company's: MIYA learns a spelling on a phone hit only when it
    matches (``resolve_person``: ``best_match >= MATCH_THRESHOLD``), or one
    wrong phone teaches a firm another firm's name for good. A company known
    only by a label has no name to disagree with."""
    if key is None:
        return False
    if company.name_key.startswith(LABEL_PREFIX):
        return True
    held = [company.name_key]
    held += await _ids(
        session,
        sa.select(CompanyAlias.alias_key).where(CompanyAlias.company_id == company.id),
    )
    return any(_pair_score(key, k) >= AUTO_MERGE for k in held)


async def _note_conflict(session: AsyncSession, winner: Company, hit: _Hit) -> None:
    """A lower-precedence key named another company: log it, and when the two
    names are close mark the other one for the owner's 🔀 (04 §2.1)."""
    other = hit.company
    conflict = KeyConflict(hit.how, winner.id, other.id)
    log.info(
        "key conflict",
        extra={
            "key_type": conflict.key_type,
            "winner": str(winner.id),
            "other": str(other.id),
        },
    )
    if not (usable_name(winner.name_key) and usable_name(other.name_key)):
        return
    score = _pair_score(winner.name_key, other.name_key)
    if score >= REVIEW_LOW and other.dup_of is None and other.state != "merged":
        other.dup_of = winner.id
        other.dup_score = round(score)
        await session.flush()


# --- Carrying identity onto the winner (04 §2.1, §2.4) -------------------------------


async def learn_alias(
    session: AsyncSession, company: Company, spelling: str | None, source_key: str | None
) -> bool:
    """Keep a spelling the company was seen under, once per ``alias_key``
    (``uq_company_aliases_company_key``): the first spelling of a key stays.
    The display name itself is not an alias — ``companies.name_key`` already
    carries it into every lookup."""
    text = _text(spelling)
    if text is None or len(text) < 2 or text == company.display_name:
        return False
    key = name_key(text)
    if not key or key.startswith(LABEL_PREFIX):
        return False
    stmt = (
        pg_insert(CompanyAlias)
        .values(
            company_id=company.id,
            alias=text[:160].rstrip(),
            alias_key=key,
            source_key=source_key,
        )
        .on_conflict_do_nothing(constraint="uq_company_aliases_company_key")
        .returning(CompanyAlias.id)
    )
    return (await session.execute(stmt)).scalar() is not None


async def _add_external_id(
    session: AsyncSession, company: Company, id_type: str, id_value: str, source_key: str
) -> None:
    await session.execute(
        pg_insert(CompanyId)
        .values(
            company_id=company.id,
            id_type=id_type,
            id_value=id_value,
            source_key=source_key,
        )
        .on_conflict_do_nothing(index_elements=["id_type", "id_value"])
    )


async def _carry_identity(
    session: AsyncSession,
    company: Company,
    keys: IdentityKeys,
    *,
    foreign: set[str],
    learn_name: bool,
    source_key: str,
    now: datetime,
) -> Contact | None:
    """Give ``company`` what the observation knows that it lacks (MIYA
    ``_backfill``): fields only where NULL, never a key that named another
    company (``foreign``), phones and handles as contacts, platform ids, and
    the spelling when ``learn_name`` (``_name_agrees``). Returns the contact
    the observation is about."""
    if keys.stir and company.stir is None and f"{keys.stir}" not in foreign:
        company.stir = keys.stir
    if keys.domain and company.website_domain is None and keys.site_key not in foreign:
        company.website_domain = keys.domain
    company_tg = keys.tg_username if not keys.person_side_handles else None
    company_ig = keys.instagram if not keys.person_side_handles else None
    extra_handles: list[tuple[str | None, str | None]] = []
    if company_tg and f"tg:{company_tg}" not in foreign:
        if company.telegram_channel is None:
            company.telegram_channel = company_tg
        elif company.telegram_channel != company_tg:
            extra_handles.append((company_tg, None))
    if company_ig and f"ig:{company_ig}" not in foreign:
        if company.instagram is None:
            company.instagram = company_ig
        elif company.instagram != company_ig:
            extra_handles.append((None, company_ig))
    if keys.city and company.city is None:
        company.city = keys.city
    if keys.legal_form and company.legal_form is None:
        company.legal_form = keys.legal_form
    if company.state == "new":
        company.state = "active"
    await session.flush()

    for id_type, id_value in keys.external_ids:
        if f"ext:{id_type}:{id_value}" not in foreign:
            await _add_external_id(session, company, id_type, id_value, source_key)
    if keys.name_key and learn_name:
        await learn_alias(session, company, keys.display_name, source_key)

    url = keys.url
    basis = "public_registry" if keys.kind == "registry" else "public_source"
    contact: Contact | None = None
    # HR people from vacancies are never contacts (03 §2.7, 04 §2.4).
    if keys.kind != "vacancy" and url is not None:
        person_phone = next((p for p in keys.phones if p.owner == "person"), None)
        username = keys.tg_username if keys.person_side_handles else None
        if username and f"tg:{username}" in foreign:
            username = None
        instagram = keys.instagram if keys.person_side_handles else None
        if instagram and f"ig:{instagram}" in foreign:
            instagram = None
        telegram_id = keys.tg_user_id
        if telegram_id is not None and f"tgid:{telegram_id}" in foreign:
            telegram_id = None
        if keys.person_name or person_phone or username or instagram or telegram_id:
            contact = await upsert_contact(
                session,
                company,
                tier="person",
                phone=person_phone.e164 if person_phone else None,
                username=username,
                instagram=instagram,
                telegram_id=telegram_id,
                full_name=keys.person_name,
                role=keys.person_role,
                legal_basis=basis,
                source_url=url,
                source_key=source_key,
                now=now,
            )
    # A person's second and third numbers are forbidden data (06 §5.1): only
    # company-owned phones become company-tier rows.
    for phone in keys.phones:
        if phone.owner != "company":
            continue
        row = await upsert_contact(
            session,
            company,
            tier="company",
            phone=phone.e164,
            legal_basis=basis,
            source_url=url,
            source_key=source_key,
            now=now,
        )
        contact = contact or row
    for tg, ig in extra_handles:
        row = await upsert_contact(
            session,
            company,
            tier="company",
            username=tg,
            instagram=ig,
            legal_basis=basis,
            source_url=url,
            source_key=source_key,
            now=now,
        )
        contact = contact or row
    return contact


# --- Contacts inside a company (04 §2.4) --------------------------------------------


def _match_contact(
    rows: Sequence[Contact],
    *,
    telegram_id: int | None,
    tail: str | None,
    username: str | None,
    instagram: str | None,
) -> Contact | None:
    """Inside one company: the Telegram id, then the phone tail, then the
    username, then Instagram (04 §2.4 order, the id first — it never changes)."""
    if telegram_id is not None:
        for row in rows:
            if row.telegram_id == telegram_id:
                return row
    if tail is not None:
        for row in rows:
            if row.phone is not None and phone_tail(row.phone) == tail:
                return row
    if username is not None:
        for row in rows:
            if row.telegram_username == username:
                return row
    if instagram is not None:
        for row in rows:
            if row.instagram == instagram:
                return row
    return None


def _clean_person_text(text: str | None, limit: int) -> str | None:
    value = _text(text)
    # A 14-digit run is a PINFL (ck_contacts_no_pinfl): never stored.
    if value is None or re.search(r"[0-9]{14}", value):
        return None
    return value[:limit].rstrip()


async def upsert_contact(
    session: AsyncSession,
    company: Company,
    *,
    tier: Literal["company", "person"],
    phone: str | None = None,
    username: str | None = None,
    instagram: str | None = None,
    full_name: str | None = None,
    role: str | None = None,
    telegram_id: int | None = None,
    legal_basis: str | None = None,
    source_url: str | None = None,
    source_key: str | None = None,
    now: datetime,
) -> Contact | None:
    """Add or complete a contact of ``company`` (04 §2.4).

    One live person per company: a new person while one exists replaces it
    (the old row anonymised, never deleted) only when the newcomer ranks
    higher by ``role_rank``; otherwise the newcomer's phone is kept as a
    company-tier row without the name, and the rest of the person is dropped.
    A person-tier row always carries its legal basis, source and time.
    Returns the row the data landed in, or None when nothing was storable.
    """
    e164 = phone_e164(phone) if phone else None
    tail = phone_tail(e164) if e164 else None
    kind = phone_kind(e164) if e164 else None
    username = username_key(username)
    instagram = instagram_key(instagram)
    if tier == "person" and source_url is None:
        raise ValueError("a person-tier contact needs its source_url")
    url = source_url[:2048] if source_url else None
    every = list(
        await session.scalars(
            sa.select(Contact)
            .where(Contact.company_id == company.id)
            .order_by(Contact.created_at, Contact.id)
        )
    )
    rows = [r for r in every if r.anonymised_at is None]
    # The row already holding this number (ux_contacts_company_phone, which
    # covers anonymised rows too — anonymise keeps a landline, 02 §4 R4): the
    # number can go nowhere else in the company.
    holder = next(
        (
            r
            for r in every
            if e164 is not None
            and r.phone is not None
            and (r.phone == e164 or phone_tail(r.phone) == tail)
        ),
        None,
    )
    match = _match_contact(
        rows,
        telegram_id=telegram_id if tier == "person" else None,
        tail=tail,
        username=username,
        instagram=instagram,
    )

    def company_row(*, phone_only: bool) -> Contact | None:
        phone = e164 if holder is None else None
        handles = not phone_only and (username is not None or instagram is not None)
        if phone is None and not handles:
            return None
        row = Contact(
            company_id=company.id,
            tier="company",
            phone=phone,
            phone_kind=kind if phone is not None else None,
            telegram_username=None if phone_only else username,
            instagram=None if phone_only else instagram,
            legal_basis=legal_basis,
            source_url=url,
            source_key=source_key,
            collected_at=now,
        )
        session.add(row)
        return row

    row: Contact | None
    if tier == "company":
        if match is None:
            row = company_row(phone_only=False)
        else:
            row = match  # a person's own number listed as the firm's stays theirs
            if row.tier == "company":
                if row.phone is None and e164 is not None and holder is None:
                    row.phone, row.phone_kind = e164, kind
                row.telegram_username = row.telegram_username or username
                row.instagram = row.instagram or instagram
    else:
        role_text = _clean_person_text(role, 80)
        person = next((r for r in rows if r.tier == "person"), None)
        if (
            person is not None
            and match is not person
            and (role_rank(role_text) <= role_rank(person.role))
        ):
            # The company keeps its decision maker; the newcomer's number is
            # kept as the firm's, the rest of them is not stored.
            row = match if match is not None else company_row(phone_only=True)
        else:
            if person is not None and match is not person:
                anonymise(person, now=now)
                await session.flush()  # free ux_contacts_one_person first
            # match: the person themself, or a company-tier row that turns
            # out to be this person's (upgraded, never duplicated).
            target = match
            if target is None:
                target = Contact(company_id=company.id, tier="person")
                session.add(target)
            _fill_person(
                target,
                e164=e164 if holder is None or holder is target else None,
                kind=kind,
                username=username,
                instagram=instagram,
                telegram_id=telegram_id,
                name=_clean_person_text(full_name, 120),
                role=role_text,
                legal_basis=legal_basis or "public_source",
                url=url,
                source_key=source_key,
                now=now,
            )
            row = target
    await session.flush()
    if row is not None and row.phone is not None:
        held_tail = phone_tail(row.phone)
        if held_tail is not None:
            await check_shared_phone(session, held_tail)
    return row


def _fill_person(
    row: Contact,
    *,
    e164: str | None,
    kind: str | None,
    username: str | None,
    instagram: str | None,
    telegram_id: int | None,
    name: str | None,
    role: str | None,
    legal_basis: str,
    url: str | None,
    source_key: str | None,
    now: datetime,
) -> None:
    """Make ``row`` the company's person, filling only what it lacks; the
    first legal basis, source and collection time stay (02 §2.4)."""
    if row.tier != "person":
        row.tier = "person"
        row.legal_basis = None
        row.source_url = None
        row.collected_at = None
    if row.phone is None and e164 is not None:
        row.phone, row.phone_kind = e164, kind
    row.telegram_username = row.telegram_username or username
    row.instagram = row.instagram or instagram
    if row.telegram_id is None:
        row.telegram_id = telegram_id
    row.full_name = row.full_name or name
    row.role = row.role or role
    row.is_decision_maker = bool(row.is_decision_maker) or (
        role_rank(row.role) >= DECISION_MAKER_RANK
    )
    if row.legal_basis is None or row.source_url is None or row.collected_at is None:
        row.legal_basis = legal_basis
        row.source_url = url
        row.collected_at = now
        row.source_key = row.source_key or source_key


# --- Shared phones (04 §2.3) -----------------------------------------------------------


def unrelated_count(name_keys: Sequence[str]) -> int:
    """How many unrelated firms a set of names is: names that score ≥ 80
    against each other are one firm seen twice (single-link clusters)."""
    clusters: list[list[str]] = []
    for key in name_keys:
        joined = [
            c
            for c in clusters
            if any(
                usable_name(key) and usable_name(k) and _pair_score(key, k) >= REVIEW_LOW
                for k in c
            )
        ]
        merged = [key]
        for c in joined:
            merged.extend(c)
            clusters.remove(c)
        clusters.append(merged)
    return len(clusters)


async def check_shared_phone(session: AsyncSession, tail: str) -> bool:
    """After a contact insert: a tail on ≥ 3 unrelated live companies flips
    ``shared_phone`` on every row that carries it. Shared phones never resolve,
    never merge and never score ``derived_contact_mobile``."""
    rows = (
        await session.execute(
            sa.select(Company.id, Company.name_key)
            .join(Contact, Contact.company_id == Company.id)
            .where(Contact.phone_tail == tail, Company.state != "merged")
            .distinct()
        )
    ).all()
    if len(rows) < SHARED_PHONE_MIN:
        return False
    if unrelated_count([r[1] for r in rows]) < SHARED_PHONE_MIN:
        return False
    await session.execute(
        sa.update(Contact)
        .where(Contact.phone_tail == tail, Contact.shared_phone.is_(False))
        .values(shared_phone=True)
        .execution_options(synchronize_session="fetch")
    )
    return True


# --- Lookups for the bot and other services -------------------------------------------


async def find_company(
    session: AsyncSession,
    name: str,
    *,
    city: str | None = None,
    threshold: float = QUESTION_THRESHOLD,
) -> NameMatch:
    """Best and second-best company for a typed name (``/qidir``, the merge
    card). Loads trigram candidates, never the table; never creates, never
    learns an alias (a question is not an observation)."""
    key = name_key(name or "")
    if not key:
        return NameMatch(company_id=None, score=0.0)
    found = await _exact_name_candidates(session, key)
    found += await _name_candidates(session, key, city=city)
    rows = [(c.company_id, k, c.city, c.first_seen_at) for c in found for k in c.keys]
    match = best_name_match(key, _group_candidates(rows), city=city, guarded=False)
    if match.score < threshold:
        return NameMatch(company_id=None, score=match.score)
    return match


async def find_by_phone_tail(session: AsyncSession, tail: str) -> list[Company]:
    """Every live company a phone tail is on, roots, oldest first (MIYA
    ``find_by_phone``, all of them: a shared phone has several)."""
    found: list[Company] = []
    for company in await _companies(session, _via_contacts(Contact.phone_tail == tail)):
        root = await _root(session, company)
        if root.state != "purged" and root.id not in {c.id for c in found}:
            found.append(root)
    return found


async def company_lineage(session: AsyncSession, company_id: uuid.UUID) -> set[uuid.UUID]:
    """The id plus every company merged into it, directly or through a chain
    (undone merges excluded). ``push_log`` and ``outcomes`` are never
    re-pointed, so their readers ask for the lineage (04 §2.5)."""
    groups = await _lineages(session, [company_id], from_root=False)
    return set(groups[company_id])


async def lineages(
    session: AsyncSession, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, frozenset[uuid.UUID]]:
    """Each company's whole lineage, in one statement for any number of
    companies: ``merged_into`` followed to the company that holds its rows now
    (:func:`_root`'s rule, at most ``_ROOT_HOPS`` hops), then that root's
    :func:`company_lineage`. A merged source's contacts and carried keys sit
    on its root (04 §2.5), so a reader of what a company is known by — the
    outcomes recompute (02 §2.11, T-10) — asks for this. A company with no
    merge, and an id that names no company, is its own lineage."""
    return await _lineages(session, ids, from_root=True)


async def _lineages(
    session: AsyncSession, ids: Sequence[uuid.UUID], *, from_root: bool
) -> dict[uuid.UUID, frozenset[uuid.UUID]]:
    """The one statement behind :func:`company_lineage` and :func:`lineages`:
    (start, top) where top is the start itself or, ``from_root``, its root;
    then down ``merge_log`` from the top (undone merges excluded)."""
    groups: dict[uuid.UUID, set[uuid.UUID]] = {c: {c} for c in ids}
    if not ids:
        return {}
    named = Company.id.in_(list(ids))
    top: sa.Select[tuple[uuid.UUID, uuid.UUID]]
    if from_root:
        up = (
            sa.select(
                Company.id.label("start"),
                Company.id.label("id"),
                sa.literal(0).label("hops"),
            )
            .where(named)
            .cte("up", recursive=True)
        )
        up = up.union_all(
            sa.select(up.c.start, Company.merged_into, up.c.hops + 1)
            .join(Company, Company.id == up.c.id)
            .where(
                Company.state == "merged",
                Company.merged_into.is_not(None),
                up.c.hops < _ROOT_HOPS,
            )
        )
        root = (
            sa.select(up.c.start, up.c.id)
            .distinct(up.c.start)
            .order_by(up.c.start, up.c.hops.desc())
            .cte("root")
        )
        top = sa.select(root.c.start, root.c.id)
    else:
        top = sa.select(Company.id.label("start"), Company.id.label("id")).where(named)
    down = top.cte("down", recursive=True)
    down = down.union(
        sa.select(down.c.start, MergeLog.source_company_id)
        .join(MergeLog, MergeLog.target_company_id == down.c.id)
        .where(MergeLog.undone_at.is_(None))
    )
    for start, member in await session.execute(sa.select(down.c.start, down.c.id)):
        groups.setdefault(start, {start}).add(member)
    return {c: frozenset(g) for c, g in groups.items()}


# --- Merge and unmerge (04 §2.5, 02 §2.14) --------------------------------------

_CARRIED: Final = (
    "stir",
    "website_domain",
    "telegram_channel",
    "instagram",
    "city",
    "region",
    "legal_form",
    "oked",
    "address",
    "registered_on",
)


def _registry_confirmed(company: Company) -> bool:
    # A registry hit (E4) set the status; nothing else does.
    return company.registry_status != "unknown"


def merge_refusal(source: Company, target: Company) -> str | None:
    """Why ``source`` cannot become ``target``, or None (04 §2.5). Two
    different websites refuse only when both firms are registry-confirmed:
    a site is otherwise a weaker identity than the owner's own judgement."""
    if "purged" in (source.state, target.state):
        return "purged"
    if source.state == "merged":
        return "source_merged"
    if target.state == "merged":
        return "target_merged"
    if source.stir and target.stir and source.stir != target.stir:
        return "stir"
    if (
        source.telegram_channel
        and target.telegram_channel
        and source.telegram_channel != target.telegram_channel
    ):
        return "telegram"
    if (
        source.website_domain
        and target.website_domain
        and site_key(source.website_domain) != site_key(target.website_domain)
        and _registry_confirmed(source)
        and _registry_confirmed(target)
    ):
        return "domain"
    return None


def _json_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


async def _ids(session: AsyncSession, stmt: sa.Select[Any]) -> list[Any]:
    return list((await session.execute(stmt)).scalars().all())


async def _lock_pair(
    session: AsyncSession, a: uuid.UUID, b: uuid.UUID
) -> dict[uuid.UUID, Company]:
    rows = await session.scalars(
        sa.select(Company)
        .where(Company.id.in_([a, b]))
        .order_by(Company.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return {c.id: c for c in rows}


async def merge_into(
    session: AsyncSession,
    source: Company,
    target: Company,
    *,
    by: str,
    score: int | None = None,
    now: datetime | None = None,
) -> Company:
    """Move every row of ``source`` onto ``target`` (04 §2.5, MIYA
    ``merge_into``) and record each moved id in ``merge_log`` so ``unmerge``
    can put it back. The source row stays (``state='merged'``); push_log and
    outcomes stay where they are (``company_lineage`` reads them).

    A row that cannot move without losing something stays on the source
    instead of being deleted: a contact whose phone the target already has,
    a second live person, an alias or a signal the target already holds.
    ``unmerge`` then has nothing to recreate.
    """
    moment = now or datetime.now(settings.tz)
    target = await _root(session, target)
    if source.id == target.id:
        return target
    locked = await _lock_pair(session, source.id, target.id)
    src, tgt = locked[source.id], locked[target.id]
    if (reason := merge_refusal(src, tgt)) is not None:
        raise MergeRefused(reason)
    open_of = {
        cid: (cand_id, state)
        for cand_id, cid, state in (
            await session.execute(
                sa.select(LeadCandidate.id, LeadCandidate.company_id, LeadCandidate.state)
                .where(
                    LeadCandidate.company_id.in_([src.id, tgt.id]),
                    LeadCandidate.state.in_(_OPEN_STATES),
                )
                .with_for_update()
            )
        ).all()
    }
    src_open, tgt_open = open_of.get(src.id), open_of.get(tgt.id)
    if src_open is not None and tgt_open is not None and src_open[1] == "approved":
        # approved → excluded is no transition (02 §2.9), and a push may be
        # claiming it this very moment.
        raise MergeRefused("candidate_approved")

    moved: dict[str, Any] = {
        "source_state": src.state,
        "source_exclude_reason": src.exclude_reason,
    }

    # Contacts.
    target_contacts = (
        await session.execute(
            sa.select(Contact.phone, Contact.tier, Contact.anonymised_at).where(
                Contact.company_id == tgt.id
            )
        )
    ).all()
    target_tails = {phone_tail(p) for p, _, _ in target_contacts if p}
    target_person = any(t == "person" and a is None for _, t, a in target_contacts)
    moving: list[uuid.UUID] = []
    stayed: list[uuid.UUID] = []
    for cid, phone, tier, anonymised_at in (
        await session.execute(
            sa.select(
                Contact.id, Contact.phone, Contact.tier, Contact.anonymised_at
            ).where(Contact.company_id == src.id)
        )
    ).all():
        duplicate = phone is not None and phone_tail(phone) in target_tails
        second_person = tier == "person" and anonymised_at is None and target_person
        (stayed if duplicate or second_person else moving).append(cid)
    await _set_company(session, Contact, moving, tgt.id)
    moved["contacts"] = [str(c) for c in moving]
    moved["stayed_contacts"] = [str(c) for c in stayed]

    # Aliases (dedupe on alias_key).
    target_alias_keys = set(
        await _ids(
            session,
            sa.select(CompanyAlias.alias_key).where(CompanyAlias.company_id == tgt.id),
        )
    )
    alias_rows = (
        await session.execute(
            sa.select(CompanyAlias.id, CompanyAlias.alias_key).where(
                CompanyAlias.company_id == src.id
            )
        )
    ).all()
    aliases = [aid for aid, key in alias_rows if key not in target_alias_keys]
    await _set_company(session, CompanyAlias, aliases, tgt.id)
    moved["aliases"] = aliases

    # Platform ids: the primary key is global, so they always move.
    pairs = [
        [t, v]
        for t, v in (
            await session.execute(
                sa.select(CompanyId.id_type, CompanyId.id_value).where(
                    CompanyId.company_id == src.id
                )
            )
        ).all()
    ]
    await session.execute(
        sa.update(CompanyId)
        .where(CompanyId.company_id == src.id)
        .values(company_id=tgt.id)
    )
    moved["company_ids"] = pairs

    # Observations, model jobs, usage rows: plain re-points.
    plain: tuple[tuple[str, type[Any]], ...] = (
        ("observations", Observation),
        ("model_jobs", ModelJob),
        ("usage_log", UsageLog),
    )
    for name, model in plain:
        ids = await _ids(session, sa.select(model.id).where(model.company_id == src.id))
        await _set_company(session, model, ids, tgt.id)
        moved[name] = ids

    # Signals (dedupe on (key, evidence_id)).
    target_signals = set(
        (
            await session.execute(
                sa.select(Signal.key, Signal.evidence_id).where(
                    Signal.company_id == tgt.id
                )
            )
        ).all()
    )
    signal_rows = (
        await session.execute(
            sa.select(Signal.id, Signal.key, Signal.evidence_id).where(
                Signal.company_id == src.id
            )
        )
    ).all()
    signals = [sid for sid, key, ev in signal_rows if (key, ev) not in target_signals]
    await _set_company(session, Signal, signals, tgt.id)
    moved["signals"] = signals

    # The open candidate: excluded(merged) beside the target's, else moved.
    moved["candidates"] = []
    moved["excluded_candidates"] = []
    if src_open is not None:
        cand_id, state = src_open
        if tgt_open is not None:
            await session.execute(
                sa.update(LeadCandidate)
                .where(LeadCandidate.id == cand_id)
                .values(state="excluded", exclude_reason="merged")
            )
            moved["excluded_candidates"] = [{"id": str(cand_id), "state": state}]
        else:
            await _set_company(session, LeadCandidate, [cand_id], tgt.id)
            moved["candidates"] = [str(cand_id)]

    # 🔀 proposals: whoever pointed at the source now points at the target;
    # the pair's own proposal about each other is settled by the merge.
    pointing = await _ids(
        session,
        sa.select(Company.id).where(Company.dup_of == src.id, Company.id != tgt.id),
    )
    if pointing:
        await session.execute(
            sa.update(Company).where(Company.id.in_(pointing)).values(dup_of=tgt.id)
        )
    moved["dup_of"] = [str(c) for c in pointing]
    cleared = []
    for row, other in ((tgt, src), (src, tgt)):
        if row.dup_of == other.id:
            cleared.append(
                {"id": str(row.id), "dup_of": str(other.id), "dup_score": row.dup_score}
            )
    moved["dup_cleared"] = cleared

    # The source steps out of the unique indexes before its keys are carried.
    await session.execute(
        sa.update(Company)
        .where(Company.id == src.id)
        .values(
            state="merged",
            merged_into=tgt.id,
            dup_of=sa.case((Company.dup_of == tgt.id, None), else_=Company.dup_of),
        )
    )
    carried: dict[str, Any] = {}
    for name in _CARRIED:
        value = getattr(src, name)
        if value is not None and getattr(tgt, name) is None:
            carried[name] = _json_value(value)
    if tgt.registry_status == "unknown" and src.registry_status != "unknown":
        carried["registry_status"] = src.registry_status
    values: dict[str, Any] = {name: getattr(src, name) for name in carried}
    if tgt.dup_of == src.id:
        values["dup_of"] = None
    if signals and src.last_signal_at is not None:
        # The target now holds the source's signals: its newest is the newer
        # of the two, so the stale sweep (04 §0) sees them too.
        values["last_signal_at"] = sa.func.greatest(
            sa.func.coalesce(Company.last_signal_at, src.last_signal_at),
            src.last_signal_at,
        )
    if values:
        await session.execute(
            sa.update(Company).where(Company.id == tgt.id).values(**values)
        )
    # The target's open candidate (its own, or the one that moved) is scored
    # again on what it holds now: moved signals and contacts change its score.
    await mark_stale(session, tgt.id)
    session.add(
        MergeLog(
            source_company_id=src.id,
            target_company_id=tgt.id,
            decided_by=by,
            match_score=score,
            moved=moved,
            carried=carried,
            created_at=moment,
        )
    )
    await session.flush()
    log.warning("merged company %s into %s (by %s)", src.id, tgt.id, by)
    await _reload(session, src.id)
    merged = await _reload(session, tgt.id)
    return merged


async def _set_company(
    session: AsyncSession,
    model: type[Any],
    ids: Sequence[Any],
    company_id: uuid.UUID,
    *,
    only_from: uuid.UUID | None = None,
) -> None:
    """Re-point the rows ``ids`` of ``model`` to ``company_id`` — only those
    still on ``only_from`` when it is given (an unmerge leaves alone what
    moved on since)."""
    if not ids:
        return
    stmt = sa.update(model).where(model.id.in_(ids))
    if only_from is not None:
        stmt = stmt.where(model.company_id == only_from)
    await session.execute(
        stmt.values(company_id=company_id).execution_options(synchronize_session="fetch")
    )


async def _reload(session: AsyncSession, company_id: uuid.UUID) -> Company:
    row = await session.get(Company, company_id, populate_existing=True)
    if row is None:
        raise LookupError(company_id)
    return row


async def unmerge(
    session: AsyncSession, merge_log_id: int, *, by: str, now: datetime | None = None
) -> Company:
    """Undo one merge within 24 h (04 §2.5): every id recorded in ``moved``
    goes back to the source if it is still on the target, the ``carried``
    fields leave the target if they are still what was carried, the source
    gets its state back, and the merge_log row is stamped (it is append-only:
    updated, never deleted)."""
    moment = now or datetime.now(settings.tz)
    entry = await session.get(MergeLog, merge_log_id, with_for_update=True)
    if entry is None:
        raise UnmergeRefused("not_found")
    if entry.undone_at is not None:
        raise UnmergeRefused("already_undone")
    if moment - entry.created_at > UNMERGE_WINDOW:
        raise UnmergeRefused("too_late")
    locked = await _lock_pair(session, entry.source_company_id, entry.target_company_id)
    src = locked.get(entry.source_company_id)
    tgt = locked.get(entry.target_company_id)
    if src is None or tgt is None or src.state != "merged" or src.merged_into != tgt.id:
        raise UnmergeRefused("source_moved")
    if tgt.state == "merged":
        # Undo the later merge first: these rows sit on its target now.
        raise UnmergeRefused("target_merged")
    if tgt.state == "purged":
        raise UnmergeRefused("purged")
    moved: Mapping[str, Any] = entry.moved
    carried: Mapping[str, Any] = entry.carried or {}

    # The target gives back what it was given (only while it still holds
    # exactly that) and gets back the proposal the merge settled.
    give_back: dict[str, Any] = {}
    for name, value in carried.items():
        if _json_value(getattr(tgt, name)) == value:
            give_back[name] = "unknown" if name == "registry_status" else None
    for item in moved.get("dup_cleared", []):
        if item["id"] == str(tgt.id) and tgt.dup_of is None:
            give_back["dup_of"] = uuid.UUID(item["dup_of"])
            give_back["dup_score"] = item["dup_score"]
    if give_back:
        await session.execute(
            sa.update(Company).where(Company.id == tgt.id).values(**give_back)
        )
    restore: dict[str, Any] = {
        "state": moved.get("source_state", "active"),
        "exclude_reason": moved.get("source_exclude_reason"),
        "merged_into": None,
    }
    for item in moved.get("dup_cleared", []):
        if item["id"] == str(src.id):
            restore["dup_of"] = uuid.UUID(item["dup_of"])
            restore["dup_score"] = item["dup_score"]
    if restore["state"] == "merged":
        restore["state"] = "active"
    await session.execute(
        sa.update(Company).where(Company.id == src.id).values(**restore)
    )

    by_uuid: tuple[tuple[str, type[Any]], ...] = (
        ("contacts", Contact),
        ("candidates", LeadCandidate),
    )
    for name, model in by_uuid:
        ids = [uuid.UUID(c) for c in moved.get(name, [])]
        await _set_company(session, model, ids, src.id, only_from=tgt.id)
    by_int: tuple[tuple[str, type[Any]], ...] = (
        ("aliases", CompanyAlias),
        ("observations", Observation),
        ("model_jobs", ModelJob),
        ("usage_log", UsageLog),
        ("signals", Signal),
    )
    for name, model in by_int:
        await _set_company(session, model, moved.get(name, []), src.id, only_from=tgt.id)
    pairs = [tuple(p) for p in moved.get("company_ids", [])]
    if pairs:
        await session.execute(
            sa.update(CompanyId)
            .where(
                sa.tuple_(CompanyId.id_type, CompanyId.id_value).in_(pairs),
                CompanyId.company_id == tgt.id,
            )
            .values(company_id=src.id)
        )
    for item in moved.get("excluded_candidates", []):
        await session.execute(
            sa.update(LeadCandidate)
            .where(
                LeadCandidate.id == uuid.UUID(item["id"]),
                LeadCandidate.state == "excluded",
                LeadCandidate.exclude_reason == "merged",
            )
            .values(state=item["state"], exclude_reason=None)
        )
    pointing = [uuid.UUID(c) for c in moved.get("dup_of", [])]
    if pointing:
        await session.execute(
            sa.update(Company)
            .where(Company.id.in_(pointing), Company.dup_of == tgt.id)
            .values(dup_of=src.id)
        )
    entry.undone_at = moment
    entry.undone_by = by
    # Both companies hold a different set of signals and contacts than their
    # open candidates were scored on.
    await mark_stale(session, src.id)
    await mark_stale(session, tgt.id)
    await session.flush()
    log.warning("unmerged company %s from %s (by %s)", src.id, tgt.id, by)
    await _reload(session, tgt.id)
    return await _reload(session, src.id)


# --- resolve_job (04 §0: every 2 min, ≤ 200 rows) -------------------------------

AfterResolve = Callable[
    [AsyncSession, Observation, Resolution, datetime], Awaitable[None]
]
# What runs inside the same transaction after each resolution: T-18 adds the
# rule signals and the `found` candidate here (04 §0 «→ signals,
# lead_candidates(found)») through ``register_after_resolve``. Read when a
# batch starts, never bound as a default, so a hook added at import time of
# its module reaches every later ``resolve_job``.
AFTER_RESOLVE: list[AfterResolve] = []


def register_after_resolve(hook: AfterResolve) -> None:
    """Run ``hook(session, observation, resolution, now)`` after every
    resolution, inside its savepoint: a hook that raises costs that one
    observation (``needs_review``), never the batch. Idempotent."""
    if hook not in AFTER_RESOLVE:
        AFTER_RESOLVE.append(hook)


# Lock trouble, not data trouble: the row stays 'new' for the next run.
_RETRYABLE_SQLSTATES: Final = frozenset({"40P01", "40001", "55P03"})

SessionFactory = Callable[[], AsyncSession]


@dataclass(slots=True)
class ResolveReport:
    claimed: int = 0
    resolved: int = 0
    created: int = 0
    rejected: int = 0
    needs_review: int = 0
    deferred: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "resolved": self.resolved,
            "created": self.created,
            "rejected": self.rejected,
            "needs_review": self.needs_review,
            "deferred": self.deferred,
        }


def _sqlstate(exc: BaseException) -> str | None:
    state = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return state if isinstance(state, str) else None


def _failure_facts(exc: BaseException) -> dict[str, str | None]:
    """What a failed resolution may say in a log line: the exception's class,
    the SQLSTATE and constraint, and the line of this package it came through
    — never its message or traceback text. A database error's message carries
    ``[parameters: …]``, the row being written: a person's name, role and
    handle, which the redactor does not recognise (06 §5.1: the person tier
    is never in logs; T-08's job events log the class alone for the same
    reason)."""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint = getattr(diag, "constraint_name", None)
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if f"{_PACKAGE_DIR}" in f.filename]
    frame = ours[-1] if ours else (frames[-1] if frames else None)
    where = f"{Path(frame.filename).name}:{frame.lineno}" if frame else None
    return {
        "error": type(exc).__name__,
        "sqlstate": _sqlstate(exc),
        "constraint": constraint if isinstance(constraint, str) else None,
        "where": where,
    }


async def _resolve_one(
    session_factory: SessionFactory,
    *,
    now: datetime,
    skip: set[int],
    after: Sequence[AfterResolve],
    report: ResolveReport,
) -> bool:
    """Claim the oldest new observation (FOR UPDATE SKIP LOCKED) and resolve
    it in its own transaction. False when the queue is empty."""
    async with session_factory() as session, session.begin():
        stmt = sa.select(Observation).where(Observation.status == "new")
        if skip:
            stmt = stmt.where(Observation.id.not_in(skip))
        obs = await session.scalar(
            stmt.order_by(Observation.created_at, Observation.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if obs is None:
            return False
        report.claimed += 1
        obs_id, source_key = obs.id, obs.source_key
        values: dict[str, Any]
        try:
            async with session.begin_nested():
                resolution = await resolve_company(
                    session, obs, source_key=source_key, now=now
                )
                for hook in after:
                    await hook(session, obs, resolution, now)
        except ObservationRejected as refused:
            values = {
                "status": "rejected",
                "reject_reason": refused.reason,
                "resolved_at": now,
            }
            report.rejected += 1
        except sa.exc.DBAPIError as exc:
            if _sqlstate(exc) not in _RETRYABLE_SQLSTATES:
                log.error(
                    "resolution failed",
                    extra={"observation_id": obs_id, **_failure_facts(exc)},
                )
                values = {"status": "needs_review", "resolved_at": now}
                report.needs_review += 1
            else:
                skip.add(obs_id)
                report.deferred += 1
                return True
        except Exception as exc:
            log.error(
                "resolution failed",
                extra={"observation_id": obs_id, **_failure_facts(exc)},
            )
            values = {"status": "needs_review", "resolved_at": now}
            report.needs_review += 1
        else:
            contact = resolution.contact
            values = {
                "status": "resolved",
                "company_id": resolution.company.id,
                "contact_id": contact.id if contact is not None else None,
                "resolved_at": now,
            }
            report.resolved += 1
            report.created += int(resolution.created)
            if source_key == TELEGRAM_SOURCE:
                # Wake score_job (04 §0): the notify is delivered at commit.
                await session.execute(
                    sa.text(
                        "SELECT pg_notify('cs_interaction', id::text) "
                        "FROM interactions WHERE observation_id = :o"
                    ),
                    {"o": obs_id},
                )
        await session.execute(
            sa.update(Observation).where(Observation.id == obs_id).values(**values)
        )
    return True


async def resolve_batch(
    session_factory: SessionFactory = SessionLocal,
    *,
    now: datetime,
    limit: int = RESOLVE_LIMIT,
    after: Sequence[AfterResolve] | None = None,
) -> ResolveReport:
    """Resolve up to ``limit`` new observations, one transaction each, so one
    bad row costs only itself (it becomes ``needs_review``) and a backlog
    never starves the other jobs. ``after`` defaults to the registered
    ``AFTER_RESOLVE`` hooks as they stand now."""
    hooks = tuple(AFTER_RESOLVE if after is None else after)
    report = ResolveReport()
    skip: set[int] = set()
    for _ in range(limit):
        if not await _resolve_one(
            session_factory, now=now, skip=skip, after=hooks, report=report
        ):
            break
    return report


async def resolve_job(
    *, now: datetime | None = None, limit: int = RESOLVE_LIMIT
) -> dict[str, int]:
    """The scheduled job (04 §0: every 2 min, ≤ 200 rows). It takes no
    argument, so it is the ``job`` of the worker table's slot
    ``JobSlot("resolve", ("every 2m",), "T-17")`` (worker/main.py, T-08),
    whose listener records the ``job:resolve`` heartbeat ``{ok, error}``; the
    counts are logged here (ids and counts only, 06 §4.5) and returned for
    callers and tests."""
    report = await resolve_batch(now=now or datetime.now(settings.tz), limit=limit)
    counts = report.as_dict()
    if report.claimed:
        log.info("resolve_job", extra={"counts": counts})
    return counts
