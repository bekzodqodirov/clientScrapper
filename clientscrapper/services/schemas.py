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

Only ``ImportSignal`` (``classify``) is built here (T-15); ``RelevanceVerdict``
and ``DraftOpener`` join with T-30, ``ContactFind`` with T-24.
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


__all__ = [
    "GOODS_MAX",
    "Frequency",
    "GoodsCategory",
    "ImportSignal",
    "OriginCn",
    "SpeakerRole",
    "VolumeBasis",
]
