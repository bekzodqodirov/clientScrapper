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
from datetime import datetime, timedelta
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
    "enabled",
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


async def repush(session: AsyncSession, log: PushLog, *, sent_at: datetime) -> PushLog:
    """``/push tekshir``'s re-push: the delivered row superseded, the same
    ``external_id`` sent again as ``attempt + 1``."""
    log.superseded_at = sent_at
    await session.flush()
    again = PushLog(
        external_id=log.external_id,
        attempt=log.attempt + 1,
        candidate_id=log.candidate_id,
        company_id=log.company_id,
        phone_tail=log.phone_tail,
        endpoint=log.endpoint,
        source_key_sent=log.source_key_sent,
        payload=None,
        payload_sha256=log.payload_sha256,
        status="sent",
        http_status=200,
        sent_at=sent_at,
    )
    session.add(again)
    await session.flush()
    return again


# --- one pilot (tests/test_mvp_report.py, /holat) -----------------------------------

PILOT_START = datetime(2026, 11, 2, tzinfo=TZ)  # a Monday
PILOT_NOW = datetime(2026, 11, 16, 12, 0, tzinfo=TZ)  # day 15, a Monday
UNCLASSED = "Mijoz bilan kelishmadi"


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


async def pilot(session: AsyncSession, source: str = "telegram_monitor") -> None:
    """Two weeks of pushes, read on day 15 (``PILOT_NOW``):

    week 1 — eight pushes: won (contacted in time); lost «Import qilmaydi»,
    re-pushed two days later and contacted only then (late against the
    first delivery); lost «Raqam notoʻgʻri», never contacted; open at «Narx
    aytildi», contacted in time; a Saturday push nobody touched (its
    deadline was Monday 24:00); lost with a label no class matches; a capped
    drop; a Saturday push contacted Monday 10:00 (in time) at «Muzokara».
    week 2 — 21 pushes at score 72, two at 50, one on Sunday 23:30 (still
    week 2); then one on day 15 at 00:30 (after week 2, inside the pilot).
    Spend: Claude $5.20 at most (10.11), all providers $7.20 at most."""
    read = PILOT_NOW - timedelta(hours=9)
    tue = _at(3, 11)
    a1 = await push(session, source, sent_at=tue)
    await judged(session, a1, read_at=read, stage_kind="won", stage_name="Yutildi",
                 contacted_at=_at(3, 16))  # fmt: skip
    a2 = await push(session, source, sent_at=tue, status="unknown")
    a2 = await repush(session, a2, sent_at=_at(5, 10))
    await judged(session, a2, read_at=read, stage_kind="lost", stage_name="Yoʻqotildi",
                 lost_reason="Import qilmaydi", contacted_at=_at(5, 11))  # fmt: skip
    a3 = await push(session, source, sent_at=tue)
    await judged(session, a3, read_at=read, stage_kind="lost", stage_name="Yoʻqotildi",
                 lost_reason="Raqam notoʻgʻri")  # fmt: skip
    a4 = await push(session, source, sent_at=tue)
    await judged(session, a4, read_at=read, stage_name="Narx aytildi",
                 contacted_at=_at(3, 15))  # fmt: skip
    a5 = await push(session, source, sent_at=_at(7, 11))
    await judged(session, a5, read_at=read)
    a6 = await push(session, source, sent_at=tue)
    await judged(session, a6, read_at=read, stage_kind="lost", stage_name="Yoʻqotildi",
                 lost_reason=UNCLASSED, contacted_at=_at(4, 10))  # fmt: skip
    a7 = await push(session, source, sent_at=tue)
    await judged(session, a7, read_at=read, landing="dropped", reason="capped")
    a8 = await push(session, source, sent_at=_at(7, 12))
    await judged(session, a8, read_at=read, stage_name="Muzokara",
                 contacted_at=_at(9, 10))  # fmt: skip
    for _ in range(21):
        await push(session, source, sent_at=_at(10, 11))
    for _ in range(2):
        await push(session, source, sent_at=_at(10, 11), score=50)
    await push(session, source, sent_at=_at(15, 23, 30))
    await push(session, source, sent_at=_at(16, 0, 30))
    await spend(session, _at(3, 12), "3.50")
    await spend(session, _at(3, 13), "1.00", provider="brave", operation="serp_brave")
    await spend(session, _at(4, 12), "2.40")
    await spend(session, _at(4, 23, 30), "2.40")
    await spend(session, _at(5, 0, 30), "0.10")
    await spend(session, _at(10, 12), "5.20")
    await spend(
        session,
        _at(10, 13),
        "2.00",
        provider="google_places",
        operation="places_text_ids",
    )
    await session.commit()
