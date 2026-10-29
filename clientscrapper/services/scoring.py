"""Scoring (04 §5): the weights, the pure score function, and the check a new
weights version passes before it is approved (02 §2.7).

``score`` is pure and deterministic: the same signals, contacts, families,
weights and ``now`` give the same ``ScoreResult`` byte for byte (07 §1.3).
Per key the best live signal counts once (the most points after decay; for a
negative the heaviest), a group counts at most its cap, and the total is
clamped to 0–100. The derived keys — recency, contact, corroboration — are
computed here from the signals' ages and the contacts, and never read from a
``signals`` row (02 §2.7: they are not stored; a row that names one is
ignored).

Three conditions of 04 §5.1/§5.3 are about the company's whole set of
signals, so they are decided here and not when a signal is written — a rule
sees one evidence, and the order two observations arrive in must not change a
score:

* ``neg_b2c_words`` counts only while no volume signal stands («and no volume
  signal ≥ 1 m³»);
* ``neg_non_cn_origin`` counts only while no ``cn_origin`` stands («and no
  China word»);
* one stated amount per evidence (rule 1: «the largest number wins per
  evidence»): a ``vol_stated_1_3`` on an evidence that also carries
  ``vol_stated_3plus`` is not counted, whichever writers stored the two (the
  rules and the model can read one text differently).

The DB half is small and at the bottom: ``load_weights`` (the active version
= the newest approved), ``views_for`` (a company's signals and contacts as the
score function's inputs) and ``activate_version`` (approval after
``validate_version``). The job that applies a score to a candidate is
``services/candidates.score_job``: the state machine is that module's.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.db import enums as e
from clientscrapper.db.models import Company, Contact, ScoringWeight, Signal

Tier = Literal["hot", "warm", "cold"]

POSITIVE_GROUPS: Final = (
    "volume",
    "origin",
    "solidity",
    "recency",
    "contact",
    "corroboration",
)
# The groups whose keys are observed (stored in `signals`); the other three
# positive groups are the scorer's own.
OBSERVED_GROUPS: Final = ("volume", "origin", "solidity")
# 02 §2.9 tiers: hot ≥ 60 with a usable phone; warm 40–59, or ≥ 60 without one.
HOT_MIN: Final = 60
WARM_MIN: Final = 40
# derived_fresh_7d / 30d / 90d: the age of the newest volume or cn_origin
# signal (04 §5.1), an age of exactly 7 days still being «7d».
FRESH_BUCKETS: Final = (
    (timedelta(days=7), "derived_fresh_7d"),
    (timedelta(days=30), "derived_fresh_30d"),
    (timedelta(days=90), "derived_fresh_90d"),
)
RECENCY_KEYS: Final = frozenset(k for _, k in FRESH_BUCKETS)
# Recency reads the volume group and this one origin key (04 §5.1).
RECENCY_ORIGIN_KEY: Final = "cn_origin"
# derived_corroboration: volume/origin signals from ≥ 2 source families.
CORROBORATION_GROUPS: Final = ("volume", "origin")
CORROBORATION_FAMILIES: Final = 2
# 04 §1.1: a usable lead phone is a UZ number of kind mobile or landline on a
# contact that is not anonymised and not shared.
UZ_PREFIX: Final = "+998"
USABLE_PHONE_KINDS: Final = frozenset({"mobile", "landline"})
# The observed keys the score function reads by name (services/signals uses
# the same constants): the two stated amounts of rule 1 and the two negatives
# whose condition is about the company's other signals (04 §5.1).
STATED_3PLUS: Final = "vol_stated_3plus"
STATED_1_3: Final = "vol_stated_1_3"
CN_ORIGIN: Final = RECENCY_ORIGIN_KEY
NEG_B2C: Final = "neg_b2c_words"
NEG_NON_CN: Final = "neg_non_cn_origin"
# 02 §2.7 versioning: each weight within ±30 % of the previous version unless
# proposed by the owner.
MAX_JUMP: Final = Decimal("0.30")
OWNER_PREFIX: Final = "owner:"
_ONE: Final = Decimal(1)


@dataclass(frozen=True, slots=True)
class Weight:
    points: int
    group: str
    cap: int
    decay_days: int | None
    decayed_factor: Decimal = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class WeightSet:
    version: int
    rows: Mapping[str, Weight]

    def points(self, key: str) -> int:
        return self.rows[key].points

    def cap(self, group: str) -> int:
        """A group's cap, as its rows carry it (0 for a group with no row)."""
        return next((w.cap for w in self.rows.values() if w.group == group), 0)


def weights_v1() -> WeightSet:
    """Version 1 from the seed tuple (``enums.WEIGHTS_V1``, 04 §5.1)."""
    return WeightSet(
        version=1,
        rows={
            w.key: Weight(w.points, w.group_key, w.group_cap, w.decay_days)
            for w in e.WEIGHTS_V1
        },
    )


@dataclass(frozen=True, slots=True)
class SignalView:
    """What the score function reads of a ``signals`` row."""

    id: int
    key: str
    observed_at: datetime
    evidence_id: int
    source_key: str
    value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ContactView:
    """What the score function reads of a contact. The company's own channel
    and Instagram account (``companies.telegram_channel`` / ``instagram``) are
    handed in as one more company-tier view: the dm-channel key reads «on the
    company or a contact» (04 §5.1)."""

    tier: str = "company"
    phone: str | None = None
    phone_kind: str | None = None
    shared_phone: bool = False
    anonymised: bool = False
    is_decision_maker: bool = False
    telegram_username: str | None = None
    instagram: str | None = None


def usable_phone(contact: ContactView) -> bool:
    """04 §1.1: UZ, mobile or landline, not anonymised, not shared."""
    return (
        contact.phone is not None
        and contact.phone.startswith(UZ_PREFIX)
        and contact.phone_kind in USABLE_PHONE_KINDS
        and not contact.anonymised
        and not contact.shared_phone
    )


def _mobile(contact: ContactView) -> bool:
    return usable_phone(contact) and contact.phone_kind == "mobile"


def _dm_channel(contact: ContactView) -> bool:
    return not contact.anonymised and bool(contact.telegram_username or contact.instagram)


def _decision_maker(contact: ContactView) -> bool:
    return (
        contact.tier == "person" and contact.is_decision_maker and not contact.anonymised
    )


@dataclass(frozen=True, slots=True)
class ScoreResult:
    total: int
    tier: Tier
    breakdown: dict[str, Any]
    # The newest volume or cn_origin signal (the one recency measured), or None.
    newest_signal_at: datetime | None
    usable_phone: bool


def tier_of(total: int, *, phone: bool) -> Tier:
    """02 §2.9: hot = ≥ 60 and a usable phone; warm = 40–59, or ≥ 60 without
    one; cold = < 40."""
    if total >= HOT_MIN:
        return "hot" if phone else "warm"
    return "warm" if total >= WARM_MIN else "cold"


def _num(value: Decimal) -> int | float:
    """A JSON number: whole points stay integers (the breakdown is compared
    byte for byte, so 20 must not become 20.0)."""
    return int(value) if value == value.to_integral_value() else float(value)


def _factor(weight: Weight, observed_at: datetime, now: datetime) -> Decimal:
    """``observed_at + decay_days < now`` → the decayed factor (04 §5.2)."""
    if (
        weight.decay_days is not None
        and observed_at + timedelta(days=weight.decay_days) < now
    ):
        return weight.decayed_factor
    return _ONE


@dataclass(frozen=True, slots=True)
class _Counted:
    points: Decimal
    factor: Decimal
    signal: SignalView


def _better(new: _Counted, held: _Counted | None, *, negative: bool) -> bool:
    """The one signal a key counts: the most points after decay (for a
    negative, the heaviest penalty — a fresh «o'zim uchun» outweighs an old
    one), then the newest, then the lowest id, so the choice never depends on
    the order the rows arrived in."""
    if held is None:
        return True
    a = -new.points if negative else new.points
    b = -held.points if negative else held.points
    if a != b:
        return a > b
    if new.signal.observed_at != held.signal.observed_at:
        return new.signal.observed_at > held.signal.observed_at
    return new.signal.id < held.signal.id


def _entry(key: str, counted: _Counted) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "key": key,
        "points": _num(counted.points),
        "factor": _num(counted.factor),
        "signal_id": counted.signal.id,
        "evidence_id": counted.signal.evidence_id,
    }
    if counted.signal.value is not None:
        entry["value"] = _num(Decimal(counted.signal.value))
    return entry


def _group(cap: int, entries: list[dict[str, Any]], raw: Decimal) -> dict[str, Any]:
    used = min(Decimal(cap), raw)
    return {"cap": cap, "raw": _num(raw), "used": _num(used), "signals": entries}


def counted_signals(
    signals: Sequence[SignalView], weights: WeightSet
) -> list[SignalView]:
    """The signals ``score`` reads: keys of ``weights`` that are observed (a
    row naming a derived key is ignored), less the three the company's other
    signals overrule (module docstring): a B2C negative beside a volume
    signal, a non-China origin beside ``cn_origin``, and a 1–3 m³ amount on an
    evidence that states ≥ 3 m³."""
    observed = [
        s for s in signals if s.key in weights.rows and s.key not in e.DERIVED_KEYS
    ]
    volume = any(weights.rows[s.key].group == "volume" for s in observed)
    china = any(s.key == CN_ORIGIN for s in observed)
    large = {s.evidence_id for s in observed if s.key == STATED_3PLUS}
    return [
        s
        for s in observed
        if not (s.key == NEG_B2C and volume)
        and not (s.key == NEG_NON_CN and china)
        and not (s.key == STATED_1_3 and s.evidence_id in large)
    ]


def score(
    signals: Sequence[SignalView],
    contacts: Sequence[ContactView],
    *,
    source_families: Mapping[str, str],
    weights: WeightSet,
    now: datetime,
) -> ScoreResult:
    """Pure and deterministic (04 §5.2). Per key: the best live signal counts
    once. Group total = min(cap, Σ keys). total = clamp(0, 100, Σ positive
    groups + Σ negatives). Decay: observed_at + decay_days < now → points ×
    decayed_factor; negatives decay the same way. Derived keys are computed
    here from contacts and signal ages, never read from ``signals``. The
    signals read are ``counted_signals``'s."""
    observed = counted_signals(signals, weights)
    best: dict[str, _Counted] = {}
    for s in observed:
        weight = weights.rows[s.key]
        if weight.group == "gate":
            continue
        factor = _factor(weight, s.observed_at, now)
        counted = _Counted(Decimal(weight.points) * factor, factor, s)
        if _better(counted, best.get(s.key), negative=weight.group == "negative"):
            best[s.key] = counted

    groups: dict[str, dict[str, Any]] = {}
    used_total = Decimal(0)
    for group in OBSERVED_GROUPS:
        keys = sorted(k for k, c in best.items() if weights.rows[k].group == group)
        raw = sum((best[k].points for k in keys), Decimal(0))
        groups[group] = _group(
            weights.cap(group), [_entry(k, best[k]) for k in keys], raw
        )
        used_total += min(Decimal(weights.cap(group)), raw)

    # Recency: the newest volume or cn_origin signal, whatever its points.
    fresh = [
        s.observed_at
        for s in observed
        if weights.rows[s.key].group == "volume" or s.key == RECENCY_ORIGIN_KEY
    ]
    newest = max(fresh, default=None)
    recency: list[dict[str, Any]] = []
    recency_raw = Decimal(0)
    if newest is not None:
        age = now - newest
        for limit, key in FRESH_BUCKETS:
            if age <= limit:
                points = Decimal(weights.points(key))
                recency = [
                    {"key": key, "points": _num(points), "age_days": max(0, age.days)}
                ]
                recency_raw = points
                break
    groups["recency"] = _group(weights.cap("recency"), recency, recency_raw)
    used_total += min(Decimal(weights.cap("recency")), recency_raw)

    phone = any(usable_phone(c) for c in contacts)
    contact_entries: list[dict[str, Any]] = []
    for key, present in (
        ("derived_contact_decision_maker", any(_decision_maker(c) for c in contacts)),
        ("derived_contact_dm_channel", any(_dm_channel(c) for c in contacts)),
        ("derived_contact_mobile", any(_mobile(c) for c in contacts)),
    ):
        if present:
            contact_entries.append({"key": key, "points": weights.points(key)})
    contact_raw = sum((Decimal(x["points"]) for x in contact_entries), Decimal(0))
    groups["contact"] = _group(weights.cap("contact"), contact_entries, contact_raw)
    used_total += min(Decimal(weights.cap("contact")), contact_raw)

    families = sorted(
        {
            source_families[s.source_key]
            for s in observed
            if weights.rows[s.key].group in CORROBORATION_GROUPS
            and s.source_key in source_families
        }
    )
    corroboration_raw = (
        Decimal(weights.points("derived_corroboration"))
        if len(families) >= CORROBORATION_FAMILIES
        else Decimal(0)
    )
    cap = weights.cap("corroboration")
    groups["corroboration"] = {
        "cap": cap,
        "raw": _num(corroboration_raw),
        "used": _num(min(Decimal(cap), corroboration_raw)),
        "families": families,
    }
    used_total += min(Decimal(cap), corroboration_raw)

    negative_keys = sorted(k for k in best if weights.rows[k].group == "negative")
    negatives = [_entry(k, best[k]) for k in negative_keys]
    negative_total = sum((best[k].points for k in negative_keys), Decimal(0))
    flags = sorted(
        (
            {"key": s.key, "signal_id": s.id, "evidence_id": s.evidence_id}
            for s in observed
            if weights.rows[s.key].group == "gate"
        ),
        key=lambda f: (f["key"], f["signal_id"]),
    )

    exact = (used_total + negative_total).quantize(_ONE, rounding=ROUND_HALF_UP)
    total = int(max(Decimal(0), min(Decimal(100), exact)))
    tier = tier_of(total, phone=phone)
    breakdown: dict[str, Any] = {
        "version": weights.version,
        "computed_at": now.isoformat(),
        "groups": groups,
        "negatives": negatives,
        "flags": flags,
        "total": total,
        "tier": tier,
        "usable_phone": phone,
        "gates": {},
        "enrichment": [],
    }
    return ScoreResult(
        total=total,
        tier=tier,
        breakdown=breakdown,
        newest_signal_at=newest,
        usable_phone=phone,
    )


# --- versions (02 §2.7) ------------------------------------------------------------


def validate_version(
    candidate: WeightSet, *, previous: WeightSet | None, proposed_by: str
) -> list[str]:
    """Why ``candidate`` may not be approved; empty when it may (02 §2.7).

    Completeness: one row per key of SIGNAL_KEYS ∪ DERIVED_KEYS. Caps: every
    row of a group carries the group's one cap, the positive groups' caps sum
    to 100, ``negative`` and ``gate`` carry 0, each positive key is worth more
    than 0 and at most its cap, a negative less than 0, a gate key 0. Against
    ``previous``: no key moves to another group (the scorer's groups are
    code — a derived key in the wrong group is never counted) and each key's
    points move by at most ±30 % unless ``proposed_by`` starts with
    ``owner:`` (a key worth 0 stays 0)."""
    problems: list[str] = []
    expected = set(e.SIGNAL_KEYS) | set(e.DERIVED_KEYS)
    keys = set(candidate.rows)
    problems += [f"missing:{k}" for k in sorted(expected - keys)]
    problems += [f"unknown:{k}" for k in sorted(keys - expected)]
    caps: dict[str, set[int]] = {}
    for key in sorted(keys & expected):
        weight = candidate.rows[key]
        if weight.group not in e.WEIGHT_GROUPS:
            problems.append(f"group:{key}")
            continue
        caps.setdefault(weight.group, set()).add(weight.cap)
        if weight.group == "negative":
            ok = weight.points < 0
        elif weight.group == "gate":
            ok = weight.points == 0
        else:
            ok = 0 < weight.points <= weight.cap
        if not ok:
            problems.append(f"points:{key}")
        if weight.decay_days is not None and weight.decay_days <= 0:
            problems.append(f"decay:{key}")
        if not Decimal(0) <= weight.decayed_factor <= _ONE:
            problems.append(f"factor:{key}")
    problems += [f"caps:{g}" for g in sorted(caps) if len(caps[g]) != 1]
    for group in ("negative", "gate"):
        if caps.get(group, {0}) != {0}:
            problems.append(f"cap_not_zero:{group}")
    positive = sum(min(caps.get(g, {0})) for g in POSITIVE_GROUPS)
    if positive != 100:
        problems.append(f"caps_sum:{positive}")
    if previous is not None:
        owner = proposed_by.startswith(OWNER_PREFIX)
        for key in sorted(keys & set(previous.rows)):
            new, old = candidate.rows[key], previous.rows[key]
            if new.group != old.group:
                problems.append(f"group_changed:{key}")
            if owner:
                continue
            jump = abs(Decimal(new.points - old.points))
            if jump > MAX_JUMP * abs(Decimal(old.points)):
                problems.append(f"jump:{key}")
    return problems


class VersionRefused(Exception):
    def __init__(self, version: int, problems: Sequence[str]) -> None:
        super().__init__(f"weights version {version}: {', '.join(problems)}")
        self.version = version
        self.problems = tuple(problems)


def _weight_set(version: int, rows: Iterable[ScoringWeight]) -> WeightSet:
    return WeightSet(
        version=version,
        rows={
            r.key: Weight(
                r.points,
                r.group_key,
                r.group_cap,
                r.decay_days,
                Decimal(r.decayed_factor),
            )
            for r in rows
        },
    )


async def active_version(session: AsyncSession) -> int | None:
    """02 §2.7: ``max(version) WHERE approved_at IS NOT NULL``."""
    found = await session.scalar(
        sa.select(sa.func.max(ScoringWeight.version)).where(
            ScoringWeight.approved_at.is_not(None)
        )
    )
    return int(found) if found is not None else None


async def load_weights(session: AsyncSession, version: int | None = None) -> WeightSet:
    """A version's rows (default: the active one). Raises LookupError when
    there is none — the seed approves version 1, so that is a broken database."""
    chosen = version if version is not None else await active_version(session)
    if chosen is None:
        raise LookupError("no approved scoring_weights version")
    rows = (
        await session.scalars(
            sa.select(ScoringWeight)
            .where(ScoringWeight.version == chosen)
            .order_by(ScoringWeight.key)
            .execution_options(populate_existing=True)
        )
    ).all()
    if not rows:
        raise LookupError(f"scoring_weights version {chosen} has no rows")
    return _weight_set(chosen, rows)


async def activate_version(
    session: AsyncSession, version: int, *, by: str, now: datetime
) -> None:
    """Approve ``version`` after ``validate_version`` against the active one
    (02 §2.7: one UPDATE of ``approved_by``/``approved_at``). Does not
    commit. Raises VersionRefused with the problems."""
    rows = (
        await session.scalars(
            sa.select(ScoringWeight)
            .where(ScoringWeight.version == version)
            .execution_options(populate_existing=True)
        )
    ).all()
    if not rows:
        raise VersionRefused(version, ["no_rows"])
    if any(r.approved_at is not None for r in rows):
        raise VersionRefused(version, ["already_approved"])
    proposers = {r.proposed_by for r in rows}
    current = await active_version(session)
    previous = await load_weights(session, current) if current is not None else None
    problems = [] if len(proposers) == 1 else ["mixed_proposed_by"]
    if current is not None and version <= current:
        problems.append(f"not_newer_than:{current}")
    problems += validate_version(
        _weight_set(version, rows), previous=previous, proposed_by=min(proposers)
    )
    if problems:
        raise VersionRefused(version, problems)
    await session.execute(
        sa.update(ScoringWeight)
        .where(ScoringWeight.version == version, ScoringWeight.approved_at.is_(None))
        .values(approved_by=by, approved_at=now)
    )


# The owner's /vazn (06 §1.2): one version at a time across processes.
WEIGHTS_LOCK: Final = "cs:scoring_weights"


@dataclass(frozen=True, slots=True)
class WeightEdit:
    """What ``/vazn <key> <points>`` did: the key's points before and after
    and the version that now holds them."""

    key: str
    old: int
    new: int
    version: int
    group: str
    cap: int


def points_allowed(weight: Weight, points: int) -> bool:
    """A key's points within its group (``validate_version``'s rule for
    one row): a positive group's key 1…cap, a negative one below 0 (and at
    least −100, the column), a gate key 0."""
    if weight.group == "negative":
        return -100 <= points < 0
    if weight.group == "gate":
        return points == 0
    return 0 < points <= weight.cap


async def owner_edit(
    session: AsyncSession, key: str, points: int, *, by: str, now: datetime
) -> WeightEdit:
    """02 §2.7's ``/vazn key points``: copy the active version into
    ``version + 1`` with the one change, ``proposed_by`` and ``approved_by``
    the owner, and approve it (``activate_version`` validates it: an owner's
    edit may move a key more than ±30 %). Raises KeyError for a key the
    active version does not hold and VersionRefused when the points do not
    fit the group (``points_allowed``) or the version fails validation.
    Does not commit."""
    if not by.startswith(OWNER_PREFIX):
        raise ValueError("an owner edit is proposed by owner:<id>")
    await session.execute(
        sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtext(WEIGHTS_LOCK)))
    )
    current = await active_version(session)
    if current is None:
        raise LookupError("no approved scoring_weights version")
    active = await load_weights(session, current)
    if key not in active.rows:
        raise KeyError(key)
    weight = active.rows[key]
    target = current + 1
    if not points_allowed(weight, points):
        raise VersionRefused(target, [f"points:{key}"])
    rows = (
        await session.scalars(
            sa.select(ScoringWeight)
            .where(ScoringWeight.version == current)
            .order_by(ScoringWeight.key)
        )
    ).all()
    session.add_all(
        ScoringWeight(
            version=target,
            key=row.key,
            group_key=row.group_key,
            points=points if row.key == key else row.points,
            group_cap=row.group_cap,
            decay_days=row.decay_days,
            decayed_factor=row.decayed_factor,
            note=row.note,
            proposed_by=by,
        )
        for row in rows
    )
    await session.flush()
    await activate_version(session, target, by=by, now=now)
    return WeightEdit(
        key=key,
        old=weight.points,
        new=points,
        version=target,
        group=weight.group,
        cap=weight.cap,
    )


# --- a company as the score function's input ---------------------------------------


async def views_for(
    session: AsyncSession, company: Company
) -> tuple[list[SignalView], list[ContactView]]:
    """A company's signals and contacts as ``score`` reads them; the company's
    own channel and Instagram account become one more company-tier view."""
    signal_rows = (
        await session.scalars(
            sa.select(Signal).where(Signal.company_id == company.id).order_by(Signal.id)
        )
    ).all()
    contact_rows = (
        await session.scalars(
            sa.select(Contact)
            .where(Contact.company_id == company.id)
            .order_by(Contact.id)
        )
    ).all()
    signals = [
        SignalView(
            id=s.id,
            key=s.key,
            observed_at=s.observed_at,
            evidence_id=s.evidence_id,
            source_key=s.source_key,
            value=s.value,
        )
        for s in signal_rows
    ]
    contacts = [
        ContactView(
            tier=c.tier,
            phone=c.phone,
            phone_kind=c.phone_kind,
            shared_phone=c.shared_phone,
            anonymised=c.anonymised_at is not None,
            is_decision_maker=c.is_decision_maker,
            telegram_username=c.telegram_username,
            instagram=c.instagram,
        )
        for c in contact_rows
    ]
    if company.telegram_channel or company.instagram:
        contacts.append(
            ContactView(
                tier="company",
                telegram_username=company.telegram_channel,
                instagram=company.instagram,
            )
        )
    return signals, contacts
