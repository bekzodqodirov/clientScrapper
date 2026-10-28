"""Candidate views for the push tests and the owner-text rules (T-28).

``view()`` is one approved candidate as ``candidates.load_view`` would read
it — a stated 5 m³ of fabric from a Telegram group, a cited China origin,
a B2B company with every optional key — and ``replace(view(), …)`` changes
one fact. Synthetic only (07 §1.0): «Namuna …», +998 90 000 00 NN,
STIR 3000000NN, *.example.uz, namuna_<n>.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from clientscrapper.services.candidates import CandidateView, SignalEvidence
from tests.gate_fixtures import NOW

EXCERPT = (
    "Assalomu alaykum. Yiwu dan 5 kub mato kerak, optom olamiz, Toshkentga "
    "yetkazish kerak. Xitoydan har oy keltiramiz."
)


def sig(
    key: str = "vol_stated_3plus",
    *,
    quote: str | None = "5 kub mato kerak",
    excerpt: str = EXCERPT,
    url: str = "https://t.me/namuna_optom/101",
    family: str = "telegram",
    group: str = "volume",
    points: int = 30,
    value: Decimal | None = Decimal(5),
    at: datetime = NOW - timedelta(days=2),
    evidence_id: int = 7,
) -> SignalEvidence:
    return SignalEvidence(
        signal_id=evidence_id * 10,
        key=key,
        group=group,
        points=points,
        value=value,
        quote=quote,
        observed_at=at,
        source_key="telegram_monitor",
        source_family=family,
        evidence_id=evidence_id,
        url=url,
        published_at=at,
        fetched_at=at,
        excerpt=excerpt,
    )


BREAKDOWN: dict[str, Any] = {
    "groups": {
        "volume": {"used": 30},
        "origin": {"used": 20},
        "solidity": {"used": 8},
        "recency": {"used": 10},
        "contact": {"used": 6},
        "corroboration": {"used": 0},
    },
    "negatives": [],
}


def view(**changes: Any) -> CandidateView:
    base = CandidateView(
        candidate_id=uuid.UUID(int=1),
        company_id=uuid.UUID(int=2),
        external_id=f"cs:{uuid.UUID(int=2)}:202610",
        display_name="Namuna Tekstil Savdo",
        legal_form="MChJ",
        stir="300000011",
        city="Toshkent",
        address="Abu Sahiy bozori, 12-qator, 45-doʻkon",
        website_domain="namuna-tekstil.example.uz",
        telegram="namuna_tekstil",
        instagram="namuna_tekstil",
        registry_status="active",
        sectors=("tekstil",),
        goods=("mato", "aksessuar"),
        volume_band="3_10",
        phone="+998900000061",
        person=None,
        signals=(
            sig(),
            sig(
                "cn_origin",
                quote="Xitoydan har oy keltiramiz",
                group="origin",
                points=20,
                value=None,
                evidence_id=8,
            ),
        ),
        stated_m3=Decimal(5),
        score=74,
        breakdown=BREAKDOWN,
    )
    return replace(base, **changes)
