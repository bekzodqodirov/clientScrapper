"""What a model said, held against the evidence (04 §4.7). Pure.

A schema-valid answer is still only a claim. Before anything is stored:

* **output hygiene** first, over the answer as the model wrote it (it saw
  only tokens): a ``+998`` number, an ``@handle`` or a 14-digit run in a text
  field — or a phone, a Telegram link or an e-mail as ``collectors/keywords``
  reads them — came from nowhere the evidence put it: the field is dropped
  and the job is flagged ``pii_leak`` (06 §4.2);
* **the quote** (``verify_quote``): restored from its tokens, it must be a
  substring of the evidence's excerpt — compared after NFKC, folded
  apostrophes, collapsed whitespace and case-folding — and 3–200 characters
  long; what is kept is the excerpt's own fragment, so a stored quote is
  verbatim (02 §2.7). A quote that fails is no quote: the signals that need
  one (``vol_stated_*``, ``vol_container``) are not written, and a volume
  without a verified quote counts as ``inferred``;
* **volume sanity** (``sane_volume``): (0, 200] m³; a container 28–70, a
  truck 40–95 — each times the count its quote states («2 ta 40 fut
  konteyner» is 56–140, «3 fura» 120–285: 04 §4.7's bounds are one unit's,
  the volume is a shipment's); a stated volume must agree with ``VOLUME_RE``
  on its quote within ±10 %, else the regex wins. A volume over 200 with a
  verified quote is **capped** at 200, not dropped: the text says it, and
  every signal reads ≥ 3 the same (a 250 m³ ask must not score below a 5 m³
  one); an estimate over 200 (``inferred``, or a quote that failed) is no
  volume. The basis follows the volume: a ``stated`` or ``inferred`` basis
  left with no volume is ``none`` (with no quote); a quoted container,
  truck or weight keeps its basis and quote with no number — the unit is
  what the text says;
* **role consistency** (``role_consistent``): a weak ``tout`` whose text
  offers no cargo service (``keywords.cargo_offer``, the tout filter's own
  text rule: a buyer's «kim kargo qiladi?» names the service, it does not
  offer it) is ``unknown`` — the collector's tout filter already caught the
  plain ones, and a doubtful model flag must not exclude a buyer (a tout
  that stands excludes the company, ``gates.gate_competitor``);
* **goods** (``clean_goods``): lower-cased, deduplicated, at most three words
  and 30 characters each, Latin or Cyrillic letters only.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from clientscrapper.collectors.keywords import (
    CHINA_RE,
    CONTAINER_RE,
    EMAIL_RE,
    PHONE_RE,
    PINFL_RE,
    TG_RE,
    cargo_offer,
)
from clientscrapper.collectors.telegram_monitor import stated_volume
from clientscrapper.services import redlines
from clientscrapper.services.pseudonyms import (
    TOKEN_RE,
    TOKEN_TRACE_RE,
    pseudonymise,
    restore,
)
from clientscrapper.services.schemas import (
    GOODS_MAX,
    HOOK_CHARS_MAX,
    OPENER_CHARS,
    QUOTE_REF_PATTERN,
    REASON_CHARS_MAX,
    ROLE_CHARS_MAX,
    DraftOpener,
    ImportSignal,
    RelevanceVerdict,
    SpeakerRole,
)
from clientscrapper.services.text import APOSTROPHE_CLASS, fold_apostrophes, to_latin

# 04 §4.7 output hygiene: what a model output field may never carry — 04's
# three shapes (the 14-digit run is keywords' PINFL_RE), and what the request
# fence itself refuses (a phone written with spaces is a phone too; IG_RE is
# left out: its label branch reads an ordinary word after «Instagram», and
# dropping a field is not free).
LEAK_RE: Final = re.compile(r"\+998\d{9}|@\w{5,}")
LEAK_PATTERNS: Final = (LEAK_RE, PINFL_RE, PHONE_RE, TG_RE, EMAIL_RE)
PII_LEAK: Final = "pii_leak"
QUOTE_MIN: Final = 3
QUOTE_MAX: Final = 200
VOLUME_MAX: Final = 200.0
CONTAINER_M3: Final = (28.0, 70.0)
TRUCK_M3: Final = (40.0, 95.0)
STATED_TOLERANCE: Final = 0.10
TOUT_CONFIDENCE: Final = 0.8
GOODS_WORDS_MAX: Final = 3
GOODS_CHARS_MAX: Final = 30
# A basis that claims the text says the volume: without a verified quote it
# is only an estimate.
QUOTED_BASES: Final = frozenset({"stated", "container", "truck", "weight"})
# A quoted unit that stands with no number (the signal writer reads the unit,
# or the quoted tonnes, from the quote itself).
UNIT_BASES: Final = frozenset({"container", "truck", "weight"})
# How many containers or trucks a quote names: a count of 1–19 right before
# the unit, a «ta»/«x»/«шт» after it and a container size between («2 ta 40
# fut konteyner», «3 фуры»). A size alone («40ft konteyner») is one unit.
COUNT_MAX: Final = 19
_COUNT_RE: Final = re.compile(
    r"(?<![\d.,])(1[0-9]|[1-9])\s*(?:ta|та|x|х|шт\.?)?\s*"
    rf"(?:(?:20|40|45)\s*(?:ft|fut|фут\w*|{APOSTROPHE_CLASS})\s*)?\Z",
    re.I,
)
# CONTAINER_RE's words that are no container or truck.
_NOT_UNITS: Final = ("tonn", "тонн", "parti", "парти")
_SCRIPTS: Final = ("LATIN", "CYRILLIC")
_JOINERS: Final = frozenset("'ʻʼ-")


@dataclass(frozen=True, slots=True)
class Validated:
    """An answer after the validators: ``result`` is what ``model_jobs.result``
    stores; ``quote`` the verified volume quote (the excerpt's own fragment)
    or None; ``leaks`` the fields hygiene dropped (non-empty = ``pii_leak``)."""

    result: dict[str, Any]
    quote: str | None = None
    leaks: tuple[str, ...] = field(default_factory=tuple)


# --- hygiene ------------------------------------------------------------------


def leaks(text: str | None) -> bool:
    """Does a model output field carry a raw phone, handle or 14-digit run?"""
    return bool(text) and any(p.search(text or "") for p in LEAK_PATTERNS)


# --- the quote ----------------------------------------------------------------


def _fold(text: str) -> tuple[str, list[int]]:
    """``text`` in the comparison form (NFKC, apostrophes folded, case-folded,
    whitespace runs as one space, no leading or trailing space), with the
    index in ``text`` each folded character came from."""
    out: list[str] = []
    origin: list[int] = []
    gap = False
    for index, char in enumerate(text):
        for folded in fold_apostrophes(unicodedata.normalize("NFKC", char)).casefold():
            if folded.isspace():
                gap = bool(out)
                continue
            if gap:
                out.append(" ")
                origin.append(index)
                gap = False
            out.append(folded)
            origin.append(index)
    return "".join(out), origin


def verify_quote(
    quote: str | None, *, excerpt: str, reverse: Mapping[str, str]
) -> str | None:
    """The excerpt's own fragment the model quoted, or None when the quote,
    restored from its tokens, is not in it (or is shorter than 3 or longer
    than 200 characters)."""
    if quote is None:
        return None
    restored = restore(quote, reverse).strip()
    if not QUOTE_MIN <= len(restored) <= QUOTE_MAX:
        return None
    needle, _ = _fold(restored)
    haystack, origin = _fold(excerpt)
    at = haystack.find(needle) if needle else -1
    if at < 0:
        return None
    return excerpt[origin[at] : origin[at + len(needle) - 1] + 1]


# --- volume ------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def unit_count(quote: str | None) -> int:
    """How many containers or trucks ``quote`` names (1 when it says no
    count)."""
    count = 1
    for match in CONTAINER_RE.finditer(quote or ""):
        if match.group(0).lower().startswith(_NOT_UNITS):
            continue
        said = _COUNT_RE.search((quote or "")[: match.start()])
        if said is not None:
            count = max(count, min(int(said.group(1)), COUNT_MAX))
    return count


def sane_volume(value: Any, basis: str, quote: str | None) -> float | None:
    """The volume that survives 04 §4.7's sanity rules (the module
    docstring), or None. ``quote`` is the verified quote or None."""
    volume = _number(value)
    if basis == "stated" and quote is not None:
        reading = stated_volume(quote)
        if reading is not None:
            said = reading[0]
            if volume is None or abs(volume - said) > STATED_TOLERANCE * said:
                volume = said  # the regex wins
    if volume is None or volume <= 0:
        return None
    bounds = {"container": CONTAINER_M3, "truck": TRUCK_M3}.get(basis)
    if bounds is not None:
        units = unit_count(quote)
        if not bounds[0] * units <= volume <= bounds[1] * units:
            return None
    if volume > VOLUME_MAX:
        quoted = quote is not None and basis in QUOTED_BASES
        return VOLUME_MAX if quoted else None
    return volume


# --- role -----------------------------------------------------------------------


def role_consistent(role: SpeakerRole, *, text: str, confidence: float) -> SpeakerRole:
    """A ``tout`` the text does not support (no cargo offer: ``cargo_offer``)
    and the model is not sure of (confidence < 0.8) is ``unknown``."""
    if role == "tout" and confidence < TOUT_CONFIDENCE and not cargo_offer(text):
        return "unknown"
    return role


# --- goods ---------------------------------------------------------------------


def uz_apostrophes(text: str) -> str:
    """oʻ/gʻ with U+02BB (either case), any other apostrophe U+02BC (07 §1.11:
    goods and reasons reach the owner's cards)."""
    folded = fold_apostrophes(text)
    folded = re.sub(r"(?<=[oOgG])'", "ʻ", folded)
    return folded.replace("'", "ʼ")


def _letters_only(item: str) -> bool:
    for char in item:
        if char == " " or char in _JOINERS:
            continue
        if not char.isalpha() or not unicodedata.name(char, "").startswith(_SCRIPTS):
            return False
    return any(char.isalpha() for char in item)


def clean_goods(goods: Iterable[str]) -> list[str]:
    """Lower-cased, deduplicated, ≤ 3 words and ≤ 30 characters each, Latin
    or Cyrillic letters only (a space, a hyphen and an apostrophe may join
    them); at most ``GOODS_MAX``. An item that breaks a rule is dropped."""
    kept: list[str] = []
    for raw in goods:
        item = uz_apostrophes(" ".join(raw.casefold().split()))
        if (
            not item
            or len(item) > GOODS_CHARS_MAX
            or len(item.split()) > GOODS_WORDS_MAX
            or not _letters_only(item)
            or item in kept
        ):
            continue
        kept.append(item)
    return kept[:GOODS_MAX]


# --- ImportSignal ------------------------------------------------------------------


def import_signal(
    signal: ImportSignal, *, excerpt: str, text: str, reverse: Mapping[str, str]
) -> Validated:
    """One ``classify`` answer after every rule of 04 §4.7. ``excerpt`` is the
    evidence's (the quote must be in it), ``text`` the message as written
    (the tout check reads it), ``reverse`` the request's pseudonym map."""
    dropped: list[str] = []
    goods = [g for g in signal.goods if not leaks(g)]
    if len(goods) != len(signal.goods):
        dropped.append("goods")
    raw_quote = signal.volume_quote
    if leaks(raw_quote):
        dropped.append("volume_quote")
        raw_quote = None
    quote = verify_quote(raw_quote, excerpt=excerpt, reverse=reverse)
    basis = signal.volume_basis
    if quote is None and basis in QUOTED_BASES:
        basis = "inferred"
    volume = sane_volume(signal.volume_m3_est, basis, quote)
    if volume is None and basis not in UNIT_BASES:
        # A basis that is a number («stated», «inferred») with no number left.
        basis, quote = "none", None
    result = signal.model_dump(mode="json")
    result.update(
        speaker_role=role_consistent(
            signal.speaker_role, text=text, confidence=signal.confidence
        ),
        goods=clean_goods(goods),
        volume_m3_est=volume,
        volume_basis=basis,
        volume_quote=quote,
    )
    return Validated(result=result, quote=quote, leaks=tuple(dropped))


# --- RelevanceVerdict (04 §4.4) ------------------------------------------------------

# A weak exclusion flag must not exclude a buyer (as ``role_consistent``): a
# competitor below this confidence stands only when a block offers a cargo
# service (``keywords.cargo_offer``), a supplier or a state body only with a
# verified quote.
FLAG_CONFIDENCE: Final = TOUT_CONFIDENCE
_REF_RE: Final = re.compile(QUOTE_REF_PATTERN)
_CYRILLIC_RE: Final = re.compile(r"[Ѐ-ӿ]")


def clip_text(text: str, limit: int) -> str:
    """``text`` within ``limit`` characters, cut between words («…» marks a
    cut)."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def names_one(text: str, names: Iterable[str]) -> bool:
    """Does ``text`` carry one of ``names`` (the pseudonymiser's own name
    rule, any case, either script, with the endings people write)?"""
    known = [n for n in names if n]
    if not known:
        return False
    masked = pseudonymise(text, names=known).text
    return any(m.group(1) == "NAME" for m in TOKEN_RE.finditer(masked))


def _owner_text(raw: str, limit: int, names: Iterable[str]) -> str | None:
    """A model's sentence as an owner card may show it (07 §1.11): Uzbek
    Latin with ʻ/ʼ, within ``limit``; None for one that carries Cyrillic, a
    pseudonym token or a person's name."""
    text = " ".join(raw.split())
    if not text or _CYRILLIC_RE.search(text) or TOKEN_TRACE_RE.search(text):
        return None
    if names_one(text, names):
        return None
    return clip_text(uz_apostrophes(text), limit)


def china_quote(
    quotes: Iterable[Mapping[str, Any]], *, dealer_refs: Collection[str] = ()
) -> Mapping[str, Any] | None:
    """The verified quote a ``confirmed`` China origin rests on (04 §4.4: an
    explicit China/1688/Taobao/Yiwu/Guangzhou mention or a Chinese brand's
    dealership quoted): the first that names China (``CHINA_RE``), else the
    first from a block of a brand dealer's page (``dealer_refs`` — every
    brand 03 §2.10 lists is Chinese, so the dealership names no country).
    A quote that is neither — «Toshkent, Abu Sahiy bozori» — carries no
    China origin: None."""
    kept = [q for q in quotes if isinstance(q, Mapping) and q.get("quote")]
    named = [q for q in kept if CHINA_RE.search(str(q["quote"]))]
    if named:
        return named[0]
    dealer = [q for q in kept if str(q.get("ref")) in dealer_refs]
    return dealer[0] if dealer else None


def relevance_verdict(
    verdict: RelevanceVerdict,
    *,
    blocks: Mapping[str, str],
    reverse: Mapping[str, str],
    names: Iterable[str] = (),
    dealer_refs: Collection[str] = (),
) -> Validated:
    """One verdict after 04 §4.4/§4.7: ``blocks`` is the bundle's
    ``{"E1": excerpt, …}``, ``reverse`` its pseudonym map, ``names`` the
    company's people (no output field may name one), ``dealer_refs`` the
    blocks that are a brand dealer's page (``china_quote``).

    * every evidence quote is held against its own block (``verify_quote``):
      what is kept is the block's own fragment, a quote of a block the bundle
      does not have, or not in its block, is dropped;
    * ``confirmed`` without a verified quote that names China or comes from
      a dealer's page (``china_quote``) is ``likely`` (a sector prior);
    * the reasons and the role are owner text: Uzbek Latin with ʻ/ʼ, cut to
      120 and 40 characters (a Cyrillic role is transliterated), none naming a
      person, none carrying a token;
    * a weak exclusion flag (``FLAG_CONFIDENCE``) without its evidence is
      dropped: a competitor needs a block offering a cargo service, a
      supplier or a state body a verified quote;
    * hygiene first (``leaks``): a reason, the role, a goods word or a quote
      carrying a phone, a handle or a 14-digit run is dropped and the job is
      flagged ``pii_leak``."""
    names = list(names)
    dropped: list[str] = []
    reasons: list[str] = []
    for raw in verdict.reasons:
        if leaks(raw):
            if "reasons" not in dropped:
                dropped.append("reasons")
            continue
        kept = _owner_text(raw, REASON_CHARS_MAX, names)
        if kept and kept not in reasons:
            reasons.append(kept)
    role: str | None = None
    if verdict.decision_maker_role is not None:
        if leaks(verdict.decision_maker_role):
            dropped.append("decision_maker_role")
        else:
            role = _owner_text(
                to_latin(verdict.decision_maker_role)
                if _CYRILLIC_RE.search(verdict.decision_maker_role)
                else verdict.decision_maker_role,
                ROLE_CHARS_MAX,
                names,
            )
    goods = [g for g in verdict.goods if not leaks(g)]
    if len(goods) != len(verdict.goods):
        dropped.append("goods")
    quotes: list[dict[str, str]] = []
    for item in verdict.evidence_quotes:
        if leaks(item.quote):
            if "evidence_quotes" not in dropped:
                dropped.append("evidence_quotes")
            continue
        ref = item.ref.strip().upper()
        if not _REF_RE.match(ref) or ref not in blocks:
            continue
        verified = verify_quote(item.quote, excerpt=blocks[ref], reverse=reverse)
        if verified is not None and not any(
            q["ref"] == ref and q["quote"] == verified for q in quotes
        ):
            quotes.append({"ref": ref, "quote": verified})
    china = verdict.imports_from_china
    if china == "confirmed" and china_quote(quotes, dealer_refs=dealer_refs) is None:
        china = "likely"
    weak = verdict.confidence < FLAG_CONFIDENCE
    competitor = verdict.is_competitor and not (
        weak and not any(cargo_offer(text) for text in blocks.values())
    )
    supplier = verdict.is_supplier_cn and not (weak and not quotes)
    state_body = verdict.is_state_body and not (weak and not quotes)
    result = verdict.model_dump(mode="json")
    result.update(
        imports_from_china=china,
        is_competitor=competitor,
        is_supplier_cn=supplier,
        is_state_body=state_body,
        decision_maker_role=role,
        goods=clean_goods(goods),
        reasons=reasons,
        evidence_quotes=quotes,
    )
    found = china_quote(quotes, dealer_refs=dealer_refs) if china == "confirmed" else None
    return Validated(
        result=result,
        quote=str(found["quote"]) if found is not None else None,
        leaks=tuple(dropped),
    )


def verdict_small(verdict: Mapping[str, Any] | None) -> bool:
    """04 §4.4: a verdict sure (≥ 0.8) the shipments are under one cubic
    metre — a flag the card shows, never points."""
    if not verdict or verdict.get("est_volume_band") != "lt1":
        return False
    confidence = verdict.get("confidence")
    return isinstance(confidence, int | float) and confidence >= SMALL_CONFIDENCE


SMALL_CONFIDENCE: Final = 0.8


# --- DraftOpener (04 §4.5) ------------------------------------------------------------

# The contact's name never stands in a stored opener: the manager writes it
# (``[Ism]``, the playbook's placeholder), and reads it on the card.
NAME_PLACEHOLDER: Final = "[Ism]"
_NAME_TOKEN: Final = re.compile(r"<NAME_\d+>")
LENGTH: Final = "length"


def draft_opener(
    draft: DraftOpener,
    *,
    lang: redlines.Lang,
    hooks: Iterable[str] = (),
    competitors: Iterable[str] = (),
) -> Validated:
    """One draft after 04 §4.5/§4.7: hygiene on the text and the hook (a
    leak drops the field and flags ``pii_leak``), a ``<NAME_n>`` token
    written as ``[Ism]``, then the redline gate (``redlines.check_opener``)
    and the schema's length (``length``); a draft in another language than
    the one asked for is ``language``. ``result.flags`` empty = the draft may
    stand; any flag = the template takes its place (``services/openers``)."""
    dropped: list[str] = []
    flags: list[str] = []
    text: str | None = draft.text
    if leaks(text):
        dropped.append("text")
        text = None
    hook: str | None = draft.hook
    if leaks(hook):
        dropped.append("hook")
        hook = None
    if text is not None:
        text = _NAME_TOKEN.sub(NAME_PLACEHOLDER, text).strip()
        guard = redlines.check_opener(
            text, lang=lang, hooks=list(hooks), competitors=list(competitors)
        )
        flags.extend(guard.flags)
        if draft.lang != lang and "language" not in flags:
            flags.append("language")
        if not OPENER_CHARS[0] <= len(text) <= OPENER_CHARS[1]:
            flags.append(LENGTH)
    else:
        flags.append(PII_LEAK)
    result = {
        "lang": draft.lang,
        "text": text,
        "hook": clip_text(hook, HOOK_CHARS_MAX) if hook else None,
        "flags": flags,
    }
    return Validated(result=result, leaks=tuple(dropped))


__all__ = [
    "CONTAINER_M3",
    "FLAG_CONFIDENCE",
    "LEAK_PATTERNS",
    "LEAK_RE",
    "LENGTH",
    "NAME_PLACEHOLDER",
    "PII_LEAK",
    "QUOTE_MAX",
    "QUOTE_MIN",
    "SMALL_CONFIDENCE",
    "STATED_TOLERANCE",
    "TOUT_CONFIDENCE",
    "TRUCK_M3",
    "VOLUME_MAX",
    "Validated",
    "china_quote",
    "clean_goods",
    "clip_text",
    "draft_opener",
    "import_signal",
    "leaks",
    "names_one",
    "relevance_verdict",
    "role_consistent",
    "sane_volume",
    "unit_count",
    "uz_apostrophes",
    "verdict_small",
    "verify_quote",
]
