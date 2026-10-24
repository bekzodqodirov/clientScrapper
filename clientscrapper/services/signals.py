"""Signals from rules and from the classifier (04 §5.3, §5.1) — the ≥ 1 m³
rule in code.

``from_rules(evidence, parsed, company)`` turns one resolved observation
(``observations.parsed``, 02 §2.5) into signal drafts, purely:

1. **Stated volume** — VOLUME_RE over the excerpt and ``volume_text``, and
   TONNES_RE (1 t ≈ 3 m³, the low end of 04 §4.3's conversion) and «yarim
   fura / полфуры» (≈ 40 m³, 04 §4.3): the largest number wins per evidence;
   ≥ 3 → ``vol_stated_3plus``, 1 ≤ n < 3 → ``vol_stated_1_3``, < 1 → no volume
   signal, and the small amount feeds the B2C negative.
2. **Containers and trucks** — CONTAINER_RE with a container/truck word →
   ``vol_container``; «partiya», «tonna» alone count as nothing here (tonnes
   are rule 1's), «yarim fura» is rule 1's.
3. **The model** — ``from_model``: an ImportSignal's stated volume with a
   verified quote (the quote validator is T-15's, ``services/validators``;
   this module trusts the quote it is handed and nothing else), a container
   or truck basis, or the tonnes of a ``weight`` quote through rule 1.
4. **Optom + a bulky category** — B2B_RE in the text and a bulky category:
   from the text (``bulky_categories``), the company's sectors, the model's
   ``goods_category`` or the OLX/Uzum category → ``vol_wholesale_bulky``. A
   marker without a bulky category (food, cosmetics, stationery) is nothing.
5. **Marketplace counts** — ≥ 10 active OLX ads in one bulky category, or
   ≥ 15 Uzum SKUs in one with Δorders ≥ 300 → ``vol_marketplace_velocity``
   with the count as ``value``.
6. **The collector's own types** — ``hints.collector.signal_types`` through
   the 03 → 02 map (``SIGNAL_TYPE_KEYS``); ``brand_distributor`` is decided
   here (``official`` and the brand group).

**A vacancy's duties are not volume** (04 §9.3): for ``kind='vacancy'`` every
volume key except ``vol_vacancy_import`` is dropped, whichever rule produced
it — «контроль отгрузок контейнеров» is a job, not the employer's trading
text. ``vol_vacancy_import`` is read from a vacancy's text only.

Besides the volume rules, the 04 §5.1 detections of own text: ``cn_origin``
(CHINA_RE), ``neg_b2c_words`` (B2C_RE or a sub-1 m³ amount, only while no
volume signal stands for the company), ``neg_non_cn_origin`` (another origin
named and no China word) and ``neg_state_body`` (a state word in a
self-description; never in a buyer's post or a vacancy, and never an
exclusion — gate 7 reads the name). A quote is the matched fragment of the
observation's own text (``parsed.text`` / ``company.goods_text``), clipped to
the column's 300 characters.

``emit`` upserts drafts on ``(company_id, key, evidence_id)`` (a later
writer's value and quote replace the earlier ones where it has them), sets
``expires_at`` from the active weights' decay, moves
``companies.last_signal_at``, and marks the company's open candidate for
re-scoring (``last_scored_at = NULL``, in the same transaction, so a score
computed concurrently cannot hide the new signal).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors.keywords import (
    B2B_RE,
    B2C_RE,
    BULKY_CATEGORIES,
    CHINA_RE,
    CONTAINER_RE,
    NON_CN_ORIGIN_RE,
    STATE_BODY_RE,
    TONNES_RE,
    VACANCY_IMPORT_RE,
    VOLUME_RE,
    bulky_categories,
)
from clientscrapper.db import enums as e
from clientscrapper.db.models import Company, LeadCandidate, Signal
from clientscrapper.services.scoring import WeightSet

VOLUME_KEYS: Final = frozenset(w.key for w in e.WEIGHTS_V1 if w.group_key == "volume")
VACANCY_KEY: Final = "vol_vacancy_import"
STATED_3PLUS: Final = "vol_stated_3plus"
STATED_1_3: Final = "vol_stated_1_3"
# 04 §5.1/§5.3: BULKY is the China-dominant set — the 17 BULKY_CATEGORIES keys
# but stationery and cosmetics; the same set answers «a bulky category» for
# rule 4 («food, cosmetics, stationery give nothing») and ``cn_sector_prior``.
BULKY: Final = frozenset(BULKY_CATEGORIES) - {"stationery", "cosmetics"}
# 04 §5.3 rule 6: 03's SIGNAL_TYPES → 02's keys. ``entity_solid`` is shape,
# never a registry confirmation (ent_legal_confirmed is E4's alone, §1.5).
SIGNAL_TYPE_KEYS: Final[Mapping[str, str]] = {
    "container_truck": "vol_container",
    "volume_m3_3plus": STATED_3PLUS,
    "volume_m3_1to3": STATED_1_3,
    "wholesale_bulky": "vol_wholesale_bulky",
    "directory_wholesale": "vol_directory_wholesale",
    "ved_vacancy": VACANCY_KEY,
    "exhibitor": "vol_exhibitor",
    "marketplace_velocity": "vol_marketplace_velocity",
    "china_origin": "cn_origin",
    "entity_solid": "ent_not_micro",
    "b2c_marker": "neg_b2c_words",
    "non_china_origin": "neg_non_cn_origin",
    "state_body": "neg_state_body",
    "liquidated": "neg_liquidated",
    "competitor": "flag_competitor",
}
# Collector types that are not signals (04 §5.3): a contact is a contact, an
# ask is the model's to read, a suppression hit is the gate's.
NOT_SIGNALS: Final = frozenset(
    {"contact_phone", "contact_handle", "intent_ask", "suppressed"}
)
BRAND_DISTRIBUTOR: Final = "brand_distributor"
# 04 §5.1: an official distributor of a bulky brand group is container scale,
# a dealer of one is wholesale; electronics brands are China origin only.
# Every brand of 03 §2.10 is Chinese, so a dealership is also cn_origin. The
# group names are the keys T-34's collectors/brands.py must use.
BULKY_BRAND_GROUPS: Final = frozenset(
    {
        "hvac_appliances",
        "solar_power",
        "vehicles",
        "machinery",
        "tyres",
        "plumbing_lighting",
    }
)
# Self-descriptions — the only kinds a state word in the text is read on: a
# buyer's post is the buyer's question, and a vacancy's «davlat tili» is a
# language (gate 7 reads the name of every kind).
SELF_DESCRIPTION_KINDS: Final = frozenset(
    {
        "profile",
        "listing",
        "directory_entry",
        "exhibitor",
        "place",
        "dealer_page",
        "registry",
        "seller",
    }
)
VACANCY_KIND: Final = "vacancy"
STATED_MIN: Final = Decimal(1)
STATED_3PLUS_MIN: Final = Decimal(3)
# 04 §4.3's conversions, the low end: 1 t of mixed goods ≈ 3 m³; yarim fura ≈ 40.
TONNE_M3: Final = Decimal(3)
HALF_TRUCK_M3: Final = Decimal(40)
_HALF_TRUCK: Final = re.compile(r"^(?:yarim|пол)", re.I)
# CONTAINER_RE matches that are a container or truck word (rule 2); the rest
# («partiya», «tonna», «полфуры») are not.
_CONTAINER_WORD: Final = re.compile(r"^(?:konte|konta|конте|fura|фур|20|40)", re.I)
# Rule 5 (03 §2.5, §2.11 thresholds).
OLX_SOURCE: Final = "olx_business"
UZUM_SOURCE: Final = "uzum_sellers"
OLX_VELOCITY_ADS: Final = 10
UZUM_VELOCITY_SKUS: Final = 15
UZUM_VELOCITY_ORDERS: Final = 300
QUOTE_MAX: Final = 300
# The states score_job re-scores (services/candidates reads this constant): a
# new signal marks such a candidate stale. ``approved`` is a push in flight.
RESCORE_STATES: Final = ("found", "enriching", "scored", "hold", "review")


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """What a draft needs of the evidence and the observation it came from."""

    id: int
    source_key: str
    fetched_at: datetime
    observation_id: int | None = None
    contact_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class CompanyFacts:
    """What a rule needs of the company: its sectors (rule 4's «companies.sectors
    ∩ BULKY») and whether a volume signal already stands for it (the B2C
    negative is emitted only without one)."""

    sectors: frozenset[str] = frozenset()
    holds_volume: bool = False


@dataclass(frozen=True, slots=True)
class SignalDraft:
    key: str
    evidence_id: int
    observed_at: datetime
    source_key: str
    value: Decimal | None = None
    quote: str | None = None
    observation_id: int | None = None
    contact_id: uuid.UUID | None = None
    model: str | None = None
    model_job_id: int | None = None


@dataclass(frozen=True, slots=True)
class VolumeReading:
    m3: Decimal
    quote: str


@dataclass(slots=True)
class _Drafts:
    """One draft per key: a second rule naming the same key fills what the
    first left empty (a value, a quote) and changes nothing else."""

    base: SignalDraft
    found: dict[str, SignalDraft] = field(default_factory=dict)

    def add(
        self, key: str, *, value: Decimal | None = None, quote: str | None = None
    ) -> None:
        clipped = _clip(quote)
        held = self.found.get(key)
        if held is None:
            self.found[key] = replace(self.base, key=key, value=value, quote=clipped)
            return
        self.found[key] = replace(
            held,
            value=held.value if held.value is not None else value,
            quote=held.quote if held.quote is not None else clipped,
        )

    def drop(self, keys: Iterable[str]) -> None:
        for key in keys:
            self.found.pop(key, None)

    def volume(self) -> bool:
        return any(k in VOLUME_KEYS for k in self.found)

    def result(self) -> list[SignalDraft]:
        return [self.found[k] for k in sorted(self.found)]


def _clip(quote: str | None) -> str | None:
    if quote is None:
        return None
    text = quote.strip()
    return text[:QUOTE_MAX] if text else None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _section(parsed: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = parsed.get(name)
    return value if isinstance(value, Mapping) else {}


def _join(*parts: str) -> str:
    return "\n".join(dict.fromkeys(p for p in parts if p))


def _texts(parsed: Mapping[str, Any]) -> tuple[str, str]:
    """(trading text, own text): the excerpt and the volume fragment, which the
    volume rules read; and that plus the origin fragment and the goods text,
    which the word rules read."""
    text = _section(parsed, "text")
    company = _section(parsed, "company")
    excerpt, volume = _str(text.get("excerpt")), _str(text.get("volume_text"))
    trading = _join(excerpt, volume)
    own = _join(
        excerpt, volume, _str(text.get("origin_text")), _str(company.get("goods_text"))
    )
    return trading, own


def _number(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None


def volume_reading(text: str) -> VolumeReading | None:
    """Rule 1: the largest amount ``text`` states, in m³ — a VOLUME_RE
    number, a TONNES_RE weight × 3, a half truck as 40 — with its fragment."""
    readings: list[VolumeReading] = []
    for match in VOLUME_RE.finditer(text):
        n = _number(match.group(1))
        if n is not None:
            readings.append(VolumeReading(n, match.group(0)))
    for match in TONNES_RE.finditer(text):
        t = _number(match.group(1))
        if t is not None:
            readings.append(VolumeReading(t * TONNE_M3, match.group(0)))
    for match in CONTAINER_RE.finditer(text):
        if _HALF_TRUCK.match(match.group(0)):
            readings.append(VolumeReading(HALF_TRUCK_M3, match.group(0)))
    # The largest wins; of equal amounts the first written.
    return max(readings, key=lambda r: r.m3, default=None)


def stated_key(m3: Decimal) -> str | None:
    """≥ 3 → vol_stated_3plus, 1 ≤ n < 3 → vol_stated_1_3, < 1 → None."""
    if m3 >= STATED_3PLUS_MIN:
        return STATED_3PLUS
    if m3 >= STATED_MIN:
        return STATED_1_3
    return None


def container_quote(text: str) -> str | None:
    """Rule 2: the first CONTAINER_RE match that is a container or truck word."""
    for match in CONTAINER_RE.finditer(text):
        word = match.group(0)
        if _CONTAINER_WORD.match(word) and not _HALF_TRUCK.match(word):
            return word
    return None


def _bulky_label(label: Any) -> bool:
    """A marketplace category, given as one of the BULKY keys or by its name
    («Бытовая техника»). An OLX numeric id cannot be judged here; the
    collector's own ``marketplace_velocity`` type (rule 6) carries it."""
    if not isinstance(label, str):
        return False
    return label in BULKY or bool(bulky_categories(label) & BULKY)


def _categories(attrs: Mapping[str, Any]) -> dict[str, int]:
    """``attrs.categories`` ({category: count}) plus a single ``category`` with
    ``ads_count``, as integers; anything else is ignored."""
    found: dict[str, int] = {}
    raw = attrs.get("categories")
    if isinstance(raw, Mapping):
        for label, count in raw.items():
            if isinstance(count, int) and not isinstance(count, bool):
                found[str(label)] = count
    single, count = attrs.get("category"), attrs.get("ads_count")
    if isinstance(single, str) and isinstance(count, int) and not isinstance(count, bool):
        found[single] = max(found.get(single, 0), count)
    return found


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def marketplace_velocity(source_key: str, attrs: Mapping[str, Any]) -> int | None:
    """Rule 5: the count that makes ``vol_marketplace_velocity``, or None."""
    bulky = [n for label, n in _categories(attrs).items() if _bulky_label(label)]
    best = max(bulky, default=0)
    if source_key == OLX_SOURCE and best >= OLX_VELOCITY_ADS:
        return best
    if source_key == UZUM_SOURCE:
        orders = _int(attrs.get("orders_30d"))
        if (
            best >= UZUM_VELOCITY_SKUS
            and orders is not None
            and orders >= UZUM_VELOCITY_ORDERS
        ):
            return best
    return None


def _bulky(own: str, attrs: Mapping[str, Any], company: CompanyFacts, goods: Any) -> bool:
    """Rule 4's second half: a bulky category from the text, the company's
    sectors, the model's goods_category, or a marketplace category."""
    return (
        bool(bulky_categories(own) & BULKY)
        or bool(company.sectors & BULKY)
        or (isinstance(goods, str) and goods in BULKY)
        or any(_bulky_label(label) for label in _categories(attrs))
    )


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


def _observed_at(parsed: Mapping[str, Any], fallback: datetime) -> datetime:
    raw = parsed.get("observed_at")
    if isinstance(raw, str):
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            return fallback
        if moment.tzinfo is not None:
            return moment
    return fallback


def _base(evidence: EvidenceRef, parsed: Mapping[str, Any], **extra: Any) -> SignalDraft:
    return SignalDraft(
        key="",
        evidence_id=evidence.id,
        observed_at=_observed_at(parsed, evidence.fetched_at),
        source_key=evidence.source_key,
        observation_id=evidence.observation_id,
        contact_id=evidence.contact_id,
        **extra,
    )


def _hints(
    parsed: Mapping[str, Any],
) -> tuple[list[str], Mapping[str, Any], Decimal | None]:
    hints = _section(parsed, "hints")
    collector = hints.get("collector")
    collector = collector if isinstance(collector, Mapping) else {}
    types = collector.get("signal_types")
    attrs = collector.get("attrs")
    raw_volume = hints.get("volume_m3")
    volume = (
        Decimal(str(raw_volume))
        if isinstance(raw_volume, int | float) and not isinstance(raw_volume, bool)
        else None
    )
    return (
        [t for t in types if isinstance(t, str)] if isinstance(types, list) else [],
        attrs if isinstance(attrs, Mapping) else {},
        volume,
    )


def _brand(drafts: _Drafts, attrs: Mapping[str, Any], excerpt: str) -> None:
    """04 §5.1's ``brand_distributor`` decision."""
    brand = attrs.get("brand")
    quote = None
    if isinstance(brand, str) and brand.strip():
        quote = _first(re.compile(re.escape(brand.strip()), re.I), excerpt)
    group = attrs.get("brand_group")
    if group in BULKY_BRAND_GROUPS:
        key = "vol_container" if attrs.get("official") is True else "vol_wholesale_bulky"
        drafts.add(key, quote=quote)
    drafts.add("cn_origin", quote=quote)


def _collector_types(
    drafts: _Drafts,
    types: Sequence[str],
    *,
    attrs: Mapping[str, Any],
    volume: Decimal | None,
    trading: str,
    own: str,
    excerpt: str,
    vacancy: bool,
) -> None:
    """Rule 6, with the fragment that carries each key as its quote where one
    is in the text."""
    finders: Mapping[str, re.Pattern[str]] = {
        "cn_origin": CHINA_RE,
        "vol_wholesale_bulky": B2B_RE,
        "vol_directory_wholesale": B2B_RE,
        "neg_b2c_words": B2C_RE,
        "neg_non_cn_origin": NON_CN_ORIGIN_RE,
        "neg_state_body": STATE_BODY_RE,
    }
    reading = volume_reading(trading)
    for kind in types:
        if kind in NOT_SIGNALS:
            continue
        if kind == BRAND_DISTRIBUTOR:
            _brand(drafts, attrs, excerpt)
            continue
        key = SIGNAL_TYPE_KEYS.get(kind)
        if key is None:
            continue
        value: Decimal | None = None
        quote: str | None = None
        if key in (STATED_3PLUS, STATED_1_3):
            value = volume if volume is not None else (reading.m3 if reading else None)
            quote = reading.quote if reading else None
        elif key == "vol_container":
            quote = container_quote(trading)
        elif key == VACANCY_KEY:
            quote = _first(VACANCY_IMPORT_RE, own) if vacancy else None
        elif key == "vol_marketplace_velocity":
            count = _int(attrs.get("ads_count")) or _int(attrs.get("sku_count"))
            value = Decimal(count) if count is not None else None
        elif key in finders:
            quote = _first(finders[key], own)
        drafts.add(key, value=value, quote=quote)


def _finish(
    drafts: _Drafts,
    *,
    own: str,
    kind: str,
    company: CompanyFacts,
    small: str | None,
    b2c: bool,
) -> list[SignalDraft]:
    """What every path ends with: the vacancy fence, one stated-volume key per
    evidence (the largest wins), the B2C negative only without volume, and no
    non-China origin beside a China word."""
    if kind == VACANCY_KIND:
        drafts.drop(k for k in VOLUME_KEYS if k != VACANCY_KEY)
    if STATED_3PLUS in drafts.found:
        drafts.drop([STATED_1_3])
    b2c_quote = _first(B2C_RE, own) if kind != VACANCY_KIND else None
    if (b2c or b2c_quote or small) and not drafts.volume() and not company.holds_volume:
        drafts.add("neg_b2c_words", quote=b2c_quote or small)
    elif drafts.volume() or company.holds_volume:
        drafts.drop(["neg_b2c_words"])
    if "cn_origin" in drafts.found:
        drafts.drop(["neg_non_cn_origin"])
    return drafts.result()


def from_rules(
    evidence: EvidenceRef, parsed: Mapping[str, Any], company: CompanyFacts
) -> list[SignalDraft]:
    """Rule signals of one observation (04 §5.3 rules 1, 2, 4, 5, 6 and the
    own-text detections of 04 §5.1). Pure; drafts sorted by key."""
    kind = _str(parsed.get("kind"))
    vacancy = kind == VACANCY_KIND
    trading, own = _texts(parsed)
    excerpt = _str(_section(parsed, "text").get("excerpt"))
    types, attrs, volume = _hints(parsed)
    drafts = _Drafts(_base(evidence, parsed))
    small: str | None = None

    if not vacancy:
        reading = volume_reading(trading)
        if reading is not None:
            key = stated_key(reading.m3)
            if key is None:
                small = reading.quote
            else:
                drafts.add(key, value=reading.m3, quote=reading.quote)
        if (quote := container_quote(trading)) is not None:
            drafts.add("vol_container", quote=quote)
        marker = _first(B2B_RE, own)
        if marker is not None and _bulky(own, attrs, company, None):
            drafts.add("vol_wholesale_bulky", quote=marker)
        count = marketplace_velocity(evidence.source_key, attrs)
        if count is not None:
            drafts.add("vol_marketplace_velocity", value=Decimal(count))
    elif (duty := _first(VACANCY_IMPORT_RE, own)) is not None:
        drafts.add(VACANCY_KEY, quote=duty)

    if (china := _first(CHINA_RE, own)) is not None:
        drafts.add("cn_origin", quote=china)
    elif (other := _first(NON_CN_ORIGIN_RE, own)) is not None:
        drafts.add("neg_non_cn_origin", quote=other)
    if (
        kind in SELF_DESCRIPTION_KINDS
        and (state := _first(STATE_BODY_RE, own)) is not None
    ):
        drafts.add("neg_state_body", quote=state)

    _collector_types(
        drafts,
        types,
        attrs=attrs,
        volume=volume,
        trading=trading,
        own=own,
        excerpt=excerpt,
        vacancy=vacancy,
    )
    return _finish(drafts, own=own, kind=kind, company=company, small=small, b2c=False)


def from_model(
    evidence: EvidenceRef,
    parsed: Mapping[str, Any],
    signal: Mapping[str, Any],
    *,
    quote: str | None,
    company: CompanyFacts,
    model: str,
    model_job_id: int | None = None,
) -> list[SignalDraft]:
    """Signals of one ImportSignal (04 §4.3, §5.3 rule 3). ``quote`` is the
    ``volume_quote`` as the validator returned it — verified, or None (then
    ``volume_m3_est`` counts as inferred: no volume signal). Every draft
    carries ``model`` and ``model_job_id``."""
    kind = _str(parsed.get("kind"))
    _, own = _texts(parsed)
    drafts = _Drafts(_base(evidence, parsed, model=model, model_job_id=model_job_id))
    small: str | None = None
    basis = signal.get("volume_basis")
    raw = signal.get("volume_m3_est")
    estimate = (
        Decimal(str(raw))
        if isinstance(raw, int | float) and not isinstance(raw, bool)
        else None
    )
    if quote is not None and kind != VACANCY_KIND:
        m3: Decimal | None = None
        if basis == "stated":
            m3 = estimate
        elif basis == "weight":
            reading = volume_reading(quote)
            m3 = reading.m3 if reading is not None else None
        elif basis in ("container", "truck"):
            drafts.add("vol_container", value=estimate, quote=quote)
        if m3 is not None:
            key = stated_key(m3)
            if key is None:
                small = quote
            else:
                drafts.add(key, value=m3, quote=quote)
    goods = signal.get("goods_category")
    if goods in BULKY:
        drafts.add("cn_sector_prior")
        marker = _first(B2B_RE, own)
        if signal.get("b2b") is True and marker is not None:
            drafts.add("vol_wholesale_bulky", quote=marker)
    if signal.get("origin_cn") == "other_origin" and _first(CHINA_RE, own) is None:
        drafts.add("neg_non_cn_origin", quote=_first(NON_CN_ORIGIN_RE, own))
    return _finish(
        drafts,
        own=own,
        kind=kind,
        company=company,
        small=small,
        b2c=signal.get("b2c_markers") is True,
    )


# --- writing ----------------------------------------------------------------------------


async def company_facts(session: AsyncSession, company: Company) -> CompanyFacts:
    """The rules' view of ``company``: its sectors and whether any volume
    signal stands for it."""
    holds = await session.scalar(
        sa.select(
            sa.exists().where(
                Signal.company_id == company.id, Signal.key.in_(VOLUME_KEYS)
            )
        )
    )
    return CompanyFacts(
        sectors=frozenset(company.sectors or ()), holds_volume=bool(holds)
    )


def expires_at(draft: SignalDraft, weights: WeightSet) -> datetime | None:
    """``observed_at + decay_days`` of the key; None = no decay (02 §2.7)."""
    weight = weights.rows.get(draft.key)
    if weight is None or weight.decay_days is None:
        return None
    return draft.observed_at + timedelta(days=weight.decay_days)


async def mark_stale(session: AsyncSession, company_id: uuid.UUID) -> None:
    """The company's open candidate is to be scored again (score_job picks a
    NULL ``last_scored_at``). In the writer's transaction: a score_job holding
    the row waits, and the mark lands after its score."""
    await session.execute(
        sa.update(LeadCandidate)
        .where(
            LeadCandidate.company_id == company_id,
            LeadCandidate.state.in_(RESCORE_STATES),
        )
        .values(last_scored_at=None)
    )


async def emit(
    session: AsyncSession,
    company_id: uuid.UUID,
    drafts: Sequence[SignalDraft],
    *,
    weights: WeightSet,
) -> int:
    """Upsert ``drafts`` on (company_id, key, evidence_id) — a second writer of
    one key on one evidence (the rules, then the model) replaces the value,
    the quote and the model columns where it brings one and keeps the first
    writer's where it brings none (04 §9.1: the rule's «2 kub» becomes the
    model's verified «2 kub mato») — then move ``companies.last_signal_at``
    and mark the open candidate stale. Returns how many drafts were written."""
    if not drafts:
        return 0
    rows = [
        {
            "company_id": company_id,
            "contact_id": d.contact_id,
            "evidence_id": d.evidence_id,
            "observation_id": d.observation_id,
            "key": d.key,
            "value": d.value,
            "quote": d.quote,
            "observed_at": d.observed_at,
            "expires_at": expires_at(d, weights),
            "source_key": d.source_key,
            "model": d.model,
            "model_job_id": d.model_job_id,
        }
        for d in drafts
    ]
    stmt = pg_insert(Signal).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_signals_company_key_evidence",
        set_={
            "value": sa.func.coalesce(stmt.excluded.value, Signal.value),
            "quote": sa.func.coalesce(stmt.excluded.quote, Signal.quote),
            "contact_id": sa.func.coalesce(stmt.excluded.contact_id, Signal.contact_id),
            "observation_id": sa.func.coalesce(
                stmt.excluded.observation_id, Signal.observation_id
            ),
            "model": sa.func.coalesce(stmt.excluded.model, Signal.model),
            "model_job_id": sa.func.coalesce(
                stmt.excluded.model_job_id, Signal.model_job_id
            ),
        },
    )
    await session.execute(stmt)
    newest = max(d.observed_at for d in drafts)
    await session.execute(
        sa.update(Company)
        .where(Company.id == company_id)
        .values(
            last_signal_at=sa.func.greatest(
                sa.func.coalesce(Company.last_signal_at, newest), newest
            )
        )
    )
    await mark_stale(session, company_id)
    return len(rows)
