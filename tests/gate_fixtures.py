"""Builders for the gate, suppression and push-gate tests (T-19).

Synthetic data only (07 §1.0): «Namuna …», +998 90 000 00 NN, STIR 3000000NN,
*.example.uz, namuna_<n>. ``NOW`` is a Tuesday inside the push window
(Mon–Sat 09:30–17:30 Tashkent), so a candidate built here passes item 10
unless a test moves the clock.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import (
    Company,
    Contact,
    Evidence,
    LeadCandidate,
    PushLog,
    Signal,
    Source,
    Suppression,
)
from clientscrapper.services import erp_feeds, keys
from clientscrapper.services.keys import name_key, phone_kind

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 27, 11, 0, tzinfo=TZ)  # a Tuesday
KEY = "test-secret"
_SEQ = itertools.count(1)


def seq() -> int:
    return next(_SEQ)


def phone(nn: int) -> str:
    """+998 90 000 00 NN — the synthetic mobile of 07 §1.0."""
    assert 1 <= nn <= 99
    return f"+9989000000{nn:02d}"


def tail(nn: int) -> str:
    found = keys.phone_tail(phone(nn))
    assert found is not None
    return found


async def firm(
    session: AsyncSession,
    name: str | None = None,
    *,
    legal_form: str | None = "MChJ",
    **fields: Any,
) -> Company:
    """A company that passes every gate unless a test says otherwise: a
    legal form makes it B2B (gate 1)."""
    display = name or f"Namuna Savdo {seq()}"
    fields.setdefault("state", "active")
    row = Company(
        display_name=display, name_key=name_key(display), legal_form=legal_form, **fields
    )
    session.add(row)
    await session.flush()
    return row


async def contact(
    session: AsyncSession, owner: Company, number: str | None = None, **fields: Any
) -> Contact:
    fields.setdefault("tier", "company")
    if number is not None:
        fields["phone"] = number
        fields.setdefault("phone_kind", phone_kind(number))
    if fields["tier"] == "person":
        fields.setdefault("legal_basis", "public_source")
        fields.setdefault("source_url", "https://t.me/namuna_optom/1")
        fields.setdefault("collected_at", NOW)
    row = Contact(company_id=owner.id, **fields)
    session.add(row)
    await session.flush()
    return row


async def candidate(
    session: AsyncSession,
    owner: Company,
    *,
    state: str = "approved",
    lead: Contact | None = None,
    score: int | None = 72,
    **fields: Any,
) -> LeadCandidate:
    if state in ("approved", "pushed"):
        fields.setdefault("approved_by", "owner:1")
        fields.setdefault("approved_at", NOW - timedelta(minutes=5))
    if state == "pushed":
        fields.setdefault("pushed_at", NOW - timedelta(minutes=1))
        fields.setdefault("external_id", f"cs:{owner.id}:{NOW:%Y%m}")
    fields.setdefault("last_scored_at", NOW)
    row = LeadCandidate(
        company_id=owner.id,
        contact_id=lead.id if lead is not None else None,
        state=state,
        score=score,
        tier="hot" if score is not None and score >= 60 else "warm",
        **fields,
    )
    session.add(row)
    await session.flush()
    return row


async def evidence(
    session: AsyncSession, source: str = "olx_business", url: str | None = None
) -> Evidence:
    n = seq()
    found = Evidence(
        source_key=source,
        url=url or f"https://namuna.example.uz/e/{n}",
        fetched_at=NOW,
        sha256=hashlib.sha256(f"gate:{n}".encode()).hexdigest(),
        bytes=0,
        excerpt="",
        expires_at=NOW + timedelta(days=180),
    )
    session.add(found)
    await session.flush()
    return found


async def signal(
    session: AsyncSession,
    owner: Company,
    key: str = "vol_container",
    *,
    observed_at: datetime | None = None,
    source: str = "olx_business",
    value: Decimal | None = None,
) -> Signal:
    """One signal on its own evidence (a container: the candidate is hot
    enough to be routed)."""
    ev = await evidence(session, source)
    found = Signal(
        company_id=owner.id,
        evidence_id=ev.id,
        key=key,
        observed_at=observed_at or NOW - timedelta(days=3),
        source_key=source,
        value=value,
    )
    session.add(found)
    await session.flush()
    return found


async def row(
    session: AsyncSession,
    kind: str,
    key_type: str,
    key: str,
    *,
    source: str | None = None,
    expires_at: datetime | None = None,
    salt_id: str | None = None,
    reason: str | None = None,
) -> Suppression:
    if source is None:
        source = "outcomes" if kind in ("client", "lost") else "owner_bot"
    found = Suppression(
        kind=kind,
        key_type=key_type,
        key=key,
        source=source,
        expires_at=expires_at,
        salt_id=salt_id,
        reason=reason,
    )
    session.add(found)
    await session.flush()
    return found


async def push(
    session: AsyncSession,
    cand: LeadCandidate,
    *,
    sent_at: datetime,
    phone_tail: str,
    external_id: str | None = None,
    attempt: int = 1,
    status: str = "sent",
    http_status: int | None = 200,
    payload_sha256: str = "0" * 64,
    finished_at: datetime | None = None,
    superseded_at: datetime | None = None,
    company_id: Any = None,
    source_key_sent: str = "agent",
) -> PushLog:
    month = sent_at.astimezone(TZ)
    log = PushLog(
        external_id=external_id
        or f"cs:{company_id or cand.company_id}:{month.year:04d}{month.month:02d}",
        attempt=attempt,
        candidate_id=cand.id,
        company_id=company_id or cand.company_id,
        phone_tail=phone_tail,
        endpoint="/api/leads/in/agent",
        source_key_sent=source_key_sent,
        payload=None,
        payload_sha256=payload_sha256,
        status=status,
        http_status=http_status if status in ("sent", "unknown") else None,
        sent_at=sent_at,
        finished_at=finished_at,
        superseded_at=superseded_at,
    )
    session.add(log)
    await session.flush()
    return log


def fresh_config(now: datetime = NOW) -> dict[str, Any]:
    """``config.suppression`` of a full pull 2 h and a delta 30 min before ``now``."""
    return erp_feeds.SuppressionConfig(
        salt_id=erp_feeds.salt_id(KEY),
        last_full_at=now - timedelta(hours=2),
        last_delta_at=now - timedelta(minutes=30),
        count=10,
    ).to_json()


async def set_config(session: AsyncSession, key: str, value: dict[str, Any]) -> None:
    """One key of ``sources.erp_sync.config`` (``erp_ready`` restores it)."""
    source = await session.get(Source, erp_feeds.ERP_SOURCE, populate_existing=True)
    assert source is not None
    source.config = {**source.config, key: value}
    await session.flush()


@contextlib.asynccontextmanager
async def erp_ready(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """The feeds configured and fresh at NOW, pushing enabled; ``sources`` is
    seeded configuration (07 §1.0): snapshot, change, restore. A test module
    wraps it in a fixture of its own."""
    monkeypatch.setattr(settings, "crm_push_enabled", True)
    monkeypatch.setattr(settings, "crm_push_source", "agent")
    monkeypatch.setattr(settings, "crm_agent_key", KEY)
    source = await session.get(Source, erp_feeds.ERP_SOURCE)
    assert source is not None
    before = (
        dict(source.config),
        source.enabled,
        source.breaker_until,
        source.breaker_reason,
    )
    source.config = {**source.config, "suppression": fresh_config()}
    source.breaker_until = None
    source.breaker_reason = None
    await session.commit()
    try:
        yield
    finally:
        await session.rollback()
        await session.execute(
            sa.update(Source)
            .where(Source.key == erp_feeds.ERP_SOURCE)
            .values(
                config=before[0],
                enabled=before[1],
                breaker_until=before[2],
                breaker_reason=before[3],
            )
        )
        await session.commit()
