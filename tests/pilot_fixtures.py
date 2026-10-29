"""A pilot's worth of pushes and what the ERP did with them (T-36).

Synthetic only (07 §1.0): «Namuna …» companies, nine-digit tails
``90000NNNN``. A push is a pushed candidate and its delivered ``push_log``
row, as ``crm_push`` writes them; its outcome goes through the real
``outcomes.record_page`` with a raw feed row (05 §A.9's 15 fields), so the
label and precision class are ``derive_label``'s — never typed here.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.db.models import (
    Company,
    LeadCandidate,
    Outcome,
    PushLog,
    Source,
    UsageLog,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import erp_feeds, outcomes
from clientscrapper.services.keys import name_key

TZ = ZoneInfo("Asia/Tashkent")
_SEQ = itertools.count(1)
# The sources columns the precision job writes: snapshotted and put back.
SOURCE_FIELDS = (
    "auto_push",
    "precision_30d",
    "positives_30d",
    "pushes_30d",
    "paused_until",
    "paused_reason",
    "config",
)


@pytest.fixture
async def sources_kept(session: AsyncSession) -> AsyncIterator[None]:
    """``sources`` is migration-seeded configuration (07 §1.0): every row's
    precision columns, pause and config as they were, put back after."""
    kept = {
        r.key: {f: getattr(r, f) for f in SOURCE_FIELDS}
        for r in await session.scalars(
            sa.select(Source).execution_options(populate_existing=True)
        )
    }
    yield
    async with SessionLocal() as own:
        for key, values in kept.items():
            await own.execute(sa.update(Source).where(Source.key == key).values(**values))
        await own.commit()


def tail(n: int) -> str:
    return f"90000{n:04d}"


async def push(
    session: AsyncSession,
    source: str,
    *,
    sent_at: datetime,
    score: int = 72,
    status: str = "sent",
    attempt: int = 1,
    superseded_at: datetime | None = None,
) -> PushLog:
    """One company pushed from ``source`` at ``sent_at``: its candidate
    (``pushed``) and its delivered push_log row."""
    n = next(_SEQ)
    display = f"Namuna Savdo {n}"
    firm = Company(display_name=display, name_key=name_key(display), state="active")
    session.add(firm)
    await session.flush()
    local = sent_at.astimezone(TZ)
    eid = f"cs:{firm.id}:{local:%Y%m}"
    cand = LeadCandidate(
        company_id=firm.id,
        state="pushed",
        score=score,
        tier="hot" if score >= 60 else "warm",
        primary_source_key=source,
        approved_by="owner:1",
        approved_at=sent_at,
        external_id=eid,
        pushed_at=sent_at,
        last_scored_at=sent_at,
    )
    session.add(cand)
    await session.flush()
    log = PushLog(
        external_id=eid,
        attempt=attempt,
        candidate_id=cand.id,
        company_id=firm.id,
        phone_tail=tail(n),
        endpoint="/api/leads/in/agent",
        source_key_sent="agent",
        payload=None,
        payload_sha256="0" * 64,
        status=status,
        http_status=200 if status == "sent" else None,
        sent_at=sent_at,
        superseded_at=superseded_at,
    )
    session.add(log)
    await session.flush()
    return log


def feed_row(
    log: PushLog,
    *,
    landing: str = "created",
    reason: str | None = None,
    stage_kind: str | None = "open",
    stage_name: str | None = "Yangi",
    lost_reason: str | None = None,
    no_contact: bool = False,
    contacted_at: datetime | None = None,
    closed_at: datetime | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """The raw outcomes row the ERP would send for ``log`` (05 §A.9)."""
    closed = closed_at
    if closed is None and stage_kind in ("won", "lost"):
        closed = log.sent_at
    return {
        "external_id": log.external_id,
        "intake_id": str(uuid.uuid4()),
        "outcome": landing,
        "reason": reason,
        "lead_id": None if landing == "dropped" else str(uuid.uuid4()),
        "client_id": None,
        "stage_kind": None if landing == "dropped" else stage_kind,
        "stage_name": None if landing == "dropped" else stage_name,
        "lost_reason": lost_reason,
        "no_contact": no_contact,
        "closed_at": erp_feeds.fmt_since(closed) if closed is not None else None,
        "contacted_at": (
            erp_feeds.fmt_since(contacted_at) if contacted_at is not None else None
        ),
        "contact_kind": "call" if contacted_at is not None else None,
        "quoted_volume_m3": None,
        "at": erp_feeds.fmt_since(at or log.sent_at),
    }


async def judged(
    session: AsyncSession, log: PushLog, *, read_at: datetime, **row: Any
) -> Outcome:
    """The ERP's row for ``log`` read at ``read_at`` through the real
    ``record_page`` (label and precision class are ``derive_label``'s)."""
    await outcomes.record_page(
        session, [feed_row(log, **row)], parse=erp_feeds.parse_outcome, now=read_at
    )
    await session.flush()
    found = await session.get(Outcome, log.external_id, populate_existing=True)
    assert found is not None
    return found


async def spend(
    session: AsyncSession,
    at: datetime,
    usd: str,
    *,
    provider: str = "anthropic",
    operation: str = "classify",
) -> None:
    """One paid call's ``usage_log`` row at ``at``."""
    session.add(
        UsageLog(
            provider=provider,
            operation=operation,
            model="test-model",
            units=1,
            cost_usd=Decimal(usd),
            created_at=at,
        )
    )
    await session.flush()
