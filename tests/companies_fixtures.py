"""Builders for the resolver tests (tests/test_resolver.py, test_shared_phone.py).

``parsed`` writes the 02 §2.5 ``observations.parsed`` shape by hand, so a test
can hand the resolver exactly the keys it means — a person-shaped name_key the
normaliser would have moved, a phone of a given owner. ``observe`` stores it
the way the runner does: an evidence row and an ``observations(status='new')``
row. Synthetic data only (07 §1.0): «Namuna …», +998 90 000 00 NN, STIR
3000000NN, *.example.uz.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.db.models import Company, Contact, Evidence, Observation
from clientscrapper.services.keys import name_key, phone_kind
from clientscrapper.services.normalise import legal_form_in_name

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 20, 7, 46, tzinfo=TZ)
_SEQ = iter(range(1, 1_000_000))


def parsed(
    *,
    kind: str = "directory_entry",
    source_key: str = "directories_goldenpages",
    name: str | None = None,
    key: str | None = "auto",
    legal_form: str | None = None,
    stir: str | None = None,
    city: str | None = None,
    website: str | None = None,
    phones: tuple[tuple[str, str], ...] = (),
    telegram: str | None = None,
    tg_user_id: int | None = None,
    instagram: str | None = None,
    ids: dict[str, Any] | None = None,
    person: dict[str, str | None] | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """One observation's ``parsed`` JSON. ``key='auto'`` is ``name_key(name)``;
    pass a value (or None) to set it directly. ``phones`` are (e164, owner)."""
    n = next(_SEQ)
    handles: dict[str, Any] = {
        "telegram": telegram,
        "tg_user_id": tg_user_id,
        "instagram": instagram,
        "olx_user_id": None,
        "hh_employer_id": None,
        "gis2_firm_id": None,
        "google_place_id": None,
        "uzum_shop": None,
    }
    handles.update(ids or {})
    return {
        "v": 1,
        "kind": kind,
        "source_key": source_key,
        "source_ref": f"test:{n}",
        "url": url or f"https://namuna.example.uz/card/{n}",
        "observed_at": NOW.isoformat(),
        "name_key": (name_key(name) if name else None) if key == "auto" else key,
        "company": {
            "name_raw": name,
            "legal_form": legal_form or (legal_form_in_name(name) if name else None),
            "stir": stir,
            "oked": None,
            "city": city,
            "address": None,
            "website": website,
            "goods_text": None,
            "sector_hints": [],
        },
        "handles": handles,
        "phones": [
            {"raw": e164, "e164": e164, "kind": phone_kind(e164), "owner": owner}
            for e164, owner in phones
        ],
        "person": person,
        "text": {
            "excerpt": "",
            "volume_text": None,
            "origin_text": None,
            "intent_words": [],
        },
        "hints": {"volume_m3": None, "volume_basis": "none"},
        "legal_basis": "public_source",
        "collector_version": f"{source_key}@1",
    }


async def observe(session: AsyncSession, body: dict[str, Any]) -> Observation:
    """Store ``body`` as the runner would: evidence + a new observation."""
    url = body["url"]
    evidence = Evidence(
        source_key=body["source_key"],
        url=url,
        source_ref=body["source_ref"],
        fetched_at=NOW,
        sha256=hashlib.sha256(url.encode()).hexdigest(),
        content_type="text/html",
        bytes=0,
        excerpt="",
        expires_at=NOW + timedelta(days=180),
    )
    session.add(evidence)
    await session.flush()
    row = Observation(
        evidence_id=evidence.id,
        source_key=body["source_key"],
        status="new",
        parsed=body,
    )
    session.add(row)
    await session.flush()
    return row


async def company(session: AsyncSession, name: str, **fields: Any) -> Company:
    """A company row as an earlier resolution would have left it."""
    fields.setdefault("state", "active")
    fields.setdefault("first_seen_at", NOW - timedelta(days=next(_SEQ) % 300 + 1))
    row = Company(display_name=name, name_key=name_key(name), **fields)
    session.add(row)
    await session.flush()
    return row


async def contact(session: AsyncSession, owner: Company, **fields: Any) -> Contact:
    fields.setdefault("tier", "company")
    if fields.get("phone"):
        fields.setdefault("phone_kind", phone_kind(fields["phone"]))
    if fields["tier"] == "person":
        fields.setdefault("legal_basis", "public_source")
        fields.setdefault("source_url", "https://t.me/namuna_optom/1")
        fields.setdefault("collected_at", NOW)
    row = Contact(company_id=owner.id, **fields)
    session.add(row)
    await session.flush()
    return row


async def count(
    session: AsyncSession, table: str, where: str = "true", **params: Any
) -> int:
    result = await session.execute(
        sa.text(f"SELECT count(*) FROM {table} WHERE {where}"), params
    )
    return int(result.scalar_one())


async def ids_of(session: AsyncSession, sql: str, **params: Any) -> list[Any]:
    return list((await session.execute(sa.text(sql), params)).scalars().all())


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
