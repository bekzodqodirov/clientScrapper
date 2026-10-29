"""The structured outputs a model call must return (04 §4.3).

Each schema is the contract of one operation: ``services/extraction.call``
sends it as the request's JSON schema (the structured path) or as text in the
system block (the prompted fallback), and validates every answer against it
either way — what does not validate is no answer. ``services/validators``
then holds the answer against the evidence (04 §4.7).

What the model reads of a schema is the schema itself — the class docstring
is its description and goes out with every request — so a docstring here is
written for the model and a developer's note is a comment.

A bound structured outputs cannot hold is applied here instead of refused:
the SDK's ``transform_schema`` moves ``maxItems``, ``minimum`` and
``maximum`` into the description (the grammar does not enforce them), so a
grammar-valid answer listing seven goods or a confidence of 1.2 is ordinary,
and refusing it would cost the retry and then the message. The list is cut
to its bound and the number clamped into its range; the schema still states
both bounds.

``ImportSignal`` (``classify``, T-15), ``RelevanceVerdict`` (``verdict`` and
``company_verdict``, 04 §4.4) and ``DraftOpener`` (``draft``, 04 §4.5) are
built here (T-30); ``ContactFind`` joins with T-24. A string bound (a quote's
200 characters, a reason's 120, an opener's 200–520) is stated in the schema
and held by ``services/validators``, which drops, clips or refuses what
overruns it — the grammar does not enforce it either.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Final, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

GOODS_MAX: Final = 6

SpeakerRole = Literal["buyer", "seller", "tout", "unknown"]
GoodsCategory = Literal[
    "textile",
    "footwear",
    "apparel",
    "household",
    "auto_parts",
    "building",
    "furniture",
    "toys",
    "cosmetics",
    "electronics",
    "lighting",
    "equipment",
    "raw",
    "packaging",
    "stationery",
    "bags",
    "sport",
    "other",
    "none",
]
OriginCn = Literal["explicit", "implied", "other_origin", "unknown"]
VolumeBasis = Literal["stated", "container", "truck", "weight", "inferred", "none"]
Frequency = Literal["one_off", "repeat", "unknown"]


def _clamp_unit(value: Any) -> Any:
    """A number clamped into [0, 1] (a non-finite one is 0); anything else as
    it is, for the float validation to refuse."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value
    return min(max(float(value), 0.0), 1.0) if math.isfinite(value) else 0.0


def _first_goods(goods: list[str]) -> list[str]:
    return goods[:GOODS_MAX]


Goods = Annotated[
    list[str],
    AfterValidator(_first_goods),
    Field(json_schema_extra={"maxItems": GOODS_MAX}),
]
Confidence = Annotated[
    float,
    BeforeValidator(_clamp_unit),
    Field(json_schema_extra={"minimum": 0, "maximum": 1}),
]


# 04 §4.3. 03's field names map onto it: is_buyer_intent = speaker_role ==
# "buyer", competitor = speaker_role == "tout", b2c = b2c_markers. Bounds the
# grammar cannot hold (goods ≤ 6, confidence in [0, 1]) are applied, not
# refused (the module docstring).
class ImportSignal(BaseModel):
    """Who speaks in one text from Uzbekistan's wholesale market, and what
    they move."""

    model_config = ConfigDict(extra="forbid")

    # tout = a cargo company, a forwarder or a buying agent.
    speaker_role: SpeakerRole
    # A business, not a private person.
    b2b: bool
    # «oʻzim uchun», a single item, retail.
    b2c_markers: bool
    # Uzbek Latin, one to three words each.
    goods: Goods
    goods_category: GoodsCategory
    origin_cn: OriginCn
    # Per shipment, m³; None when nothing states or implies one.
    volume_m3_est: float | None
    volume_basis: VolumeBasis
    # The verbatim fragment that carries the volume.
    volume_quote: str | None
    frequency: Frequency
    confidence: Confidence


# --- RelevanceVerdict (04 §4.4) ----------------------------------------------------

REASONS_MAX: Final = 5
REASON_CHARS_MAX: Final = 120
QUOTES_MAX: Final = 6
QUOTE_CHARS_MAX: Final = 200
ROLE_CHARS_MAX: Final = 40
QUOTE_REF_PATTERN: Final = r"^E\d{1,2}$"

ChinaImport = Literal["confirmed", "likely", "unlikely", "no"]
Segment = Literal["A", "B", "C", "D", "E", "X", "Z"]
VolumeBand = Literal["unknown", "lt1", "1_3", "3_10", "truck", "container"]


def _first_n(n: int) -> Any:
    def cut(items: list[Any]) -> list[Any]:
        return items[:n]

    return AfterValidator(cut)


class EvidenceQuote(BaseModel):
    """A fragment copied character for character from one evidence block."""

    model_config = ConfigDict(extra="forbid")

    # The block the quote comes from: "E1", "E2", ...
    ref: Annotated[str, Field(json_schema_extra={"pattern": QUOTE_REF_PATTERN})]
    quote: Annotated[str, Field(json_schema_extra={"maxLength": QUOTE_CHARS_MAX})]


# 04 §4.4. Both lanes (instant, batch) ask for it with the same request.
class RelevanceVerdict(BaseModel):
    """Is this Uzbek company a GSR Logistics prospect: a business that imports
    from China at a volume worth a truck share or more."""

    model_config = ConfigDict(extra="forbid")

    imports_from_china: ChinaImport
    # A bazaar · B marketplace · C factory · D construction/furniture ·
    # E electronics/auto/appliances · X competitor/supplier · Z unclear.
    segment: Segment
    est_volume_band: VolumeBand
    is_b2b: bool
    is_competitor: bool
    is_supplier_cn: bool
    is_state_body: bool
    # A role, never a person's name.
    decision_maker_role: Annotated[
        str | None, Field(json_schema_extra={"maxLength": ROLE_CHARS_MAX})
    ]
    goods: Goods
    # Uzbek Latin, for a sales manager; at most 120 characters each.
    reasons: Annotated[
        list[str],
        _first_n(REASONS_MAX),
        Field(json_schema_extra={"maxItems": REASONS_MAX}),
    ]
    evidence_quotes: Annotated[
        list[EvidenceQuote],
        _first_n(QUOTES_MAX),
        Field(json_schema_extra={"maxItems": QUOTES_MAX}),
    ]
    confidence: Confidence


# --- DraftOpener (04 §4.5) ----------------------------------------------------------

OPENER_CHARS: Final = (200, 520)
HOOK_CHARS_MAX: Final = 120


class DraftOpener(BaseModel):
    """One first message a sales manager sends from their own account."""

    model_config = ConfigDict(extra="forbid")

    lang: Literal["uz", "ru"]
    text: Annotated[
        str,
        Field(
            json_schema_extra={"minLength": OPENER_CHARS[0], "maxLength": OPENER_CHARS[1]}
        ),
    ]
    # The one public fact the message leans on.
    hook: Annotated[str, Field(json_schema_extra={"maxLength": HOOK_CHARS_MAX})]


__all__ = [
    "GOODS_MAX",
    "HOOK_CHARS_MAX",
    "OPENER_CHARS",
    "QUOTES_MAX",
    "QUOTE_CHARS_MAX",
    "QUOTE_REF_PATTERN",
    "REASONS_MAX",
    "REASON_CHARS_MAX",
    "ROLE_CHARS_MAX",
    "ChinaImport",
    "DraftOpener",
    "EvidenceQuote",
    "Frequency",
    "GoodsCategory",
    "ImportSignal",
    "OriginCn",
    "RelevanceVerdict",
    "Segment",
    "SpeakerRole",
    "VolumeBand",
    "VolumeBasis",
]
