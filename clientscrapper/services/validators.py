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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from clientscrapper.collectors.keywords import (
    CONTAINER_RE,
    EMAIL_RE,
    PHONE_RE,
    PINFL_RE,
    TG_RE,
    cargo_offer,
)
from clientscrapper.collectors.telegram_monitor import stated_volume
from clientscrapper.services.pseudonyms import restore
from clientscrapper.services.schemas import GOODS_MAX, ImportSignal, SpeakerRole
from clientscrapper.services.text import APOSTROPHE_CLASS, fold_apostrophes

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


def _uz_apostrophes(word: str) -> str:
    """oʻ/gʻ with U+02BB, any other apostrophe between letters U+02BC
    (07 §1.11: goods reach the owner's cards)."""
    folded = fold_apostrophes(word)
    folded = re.sub(r"(?<=[og])'", "ʻ", folded)
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
        item = _uz_apostrophes(" ".join(raw.casefold().split()))
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


__all__ = [
    "CONTAINER_M3",
    "LEAK_PATTERNS",
    "LEAK_RE",
    "PII_LEAK",
    "QUOTE_MAX",
    "QUOTE_MIN",
    "STATED_TOLERANCE",
    "TOUT_CONFIDENCE",
    "TRUCK_M3",
    "VOLUME_MAX",
    "Validated",
    "clean_goods",
    "import_signal",
    "leaks",
    "role_consistent",
    "sane_volume",
    "unit_count",
    "verify_quote",
]
