"""The structured outputs a model call must return (04 §4.3).

Each schema is the contract of one operation: ``services/extraction.call``
sends it as the request's JSON schema (the structured path) or as text in the
system block (the prompted fallback), and validates every answer against it
either way — what does not validate is no answer. ``services/validators``
then holds the answer against the evidence (04 §4.7).

Only ``ImportSignal`` (``classify``) is built here (T-15); ``RelevanceVerdict``
and ``DraftOpener`` join with T-30, ``ContactFind`` with T-24.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class ImportSignal(BaseModel):
    """Who speaks in one text and what they move (04 §4.3). 03's field names
    map onto it: ``is_buyer_intent = speaker_role == "buyer"``,
    ``competitor = speaker_role == "tout"``, ``b2c = b2c_markers``."""

    model_config = ConfigDict(extra="forbid")

    # tout = a cargo company, a forwarder or a buying agent.
    speaker_role: SpeakerRole
    # A business, not a private person.
    b2b: bool
    # «oʻzim uchun», a single item, retail.
    b2c_markers: bool
    # Uzbek Latin, one to three words each.
    goods: list[str] = Field(max_length=GOODS_MAX)
    goods_category: GoodsCategory
    origin_cn: OriginCn
    # Per shipment, m³; None when nothing states or implies one.
    volume_m3_est: float | None
    volume_basis: VolumeBasis
    # The verbatim fragment that carries the volume.
    volume_quote: str | None
    frequency: Frequency
    confidence: float = Field(ge=0, le=1)


__all__ = [
    "GOODS_MAX",
    "Frequency",
    "GoodsCategory",
    "ImportSignal",
    "OriginCn",
    "SpeakerRole",
    "VolumeBasis",
]
