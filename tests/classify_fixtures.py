"""The Telegram path up to the classifier's queue, on Postgres (T-15 tests).

A message goes where production puts it: the reader's ``ingest.store_hit``
(its prefilter, its insert, its notify), then a ``telegram_monitor`` run that
turns it into evidence and an observation and links the row — the queue
``classify_job`` reads. No Telegram, no network: the collector's Fetcher is
offline and must not be asked.

All data is synthetic (07 §1.0): «Namuna …», ``namuna_<n>``,
``+998 90 000 00 NN``, ``*.example.uz``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from clientscrapper.collectors import telegram_monitor as tm
from clientscrapper.collectors.runner import run_collector
from clientscrapper.db import models as m
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import chats, costs
from clientscrapper.services.http import Fetcher
from clientscrapper.userbot import ingest, prefilter
from tests.fakes import FakeClock, Site, fetcher_for

TZ = ZoneInfo("Asia/Tashkent")
T0 = datetime(2026, 10, 24, 11, 58, tzinfo=TZ)
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)
GROUP_ID = -1001000000001
CHANNEL_ID = -1001000000002
POSTER = 5100000001
ASK = "Guangzhoudan 2 kub mato kerak, kim olib keladi?"
BIO = "Namuna Tekstil Savdo MChJ direktori"
KEPT = (
    "enabled",
    "paused_until",
    "paused_reason",
    "breaker_until",
    "breaker_reason",
    "last_run_at",
)


@pytest.fixture
async def enabled(session) -> AsyncIterator[None]:
    """telegram_monitor switched on for the test, put back after."""
    async with SessionLocal() as s:
        row = await s.get(m.Source, tm.SOURCE_KEY)
        assert row is not None
        snapshot = {name: getattr(row, name) for name in KEPT}
        row.enabled = True
        await s.commit()
    costs.clear_refusals()
    yield
    costs.clear_refusals()
    async with SessionLocal() as s:
        await s.execute(
            sa.update(m.Source).where(m.Source.key == tm.SOURCE_KEY).values(**snapshot)
        )
        await s.commit()


@pytest.fixture
async def offline() -> AsyncIterator[Fetcher]:
    site = Site(FakeClock(NOW))
    fetcher = fetcher_for(site)
    yield fetcher
    await fetcher.aclose()
    assert site.seen == []


async def chat(
    session,
    tg_chat_id: int = GROUP_ID,
    chat_type: str = "group",
    *,
    title: str = "Namuna Optom",
    username: str | None = "namuna_optom",
    **fields,
) -> m.ChatMonitor:
    dialog = chats.DialogInfo(tg_chat_id, chat_type, title, username)
    row = await chats.ensure_monitor(
        session, dialog, now=T0 - timedelta(days=90), **fields
    )
    await session.commit()
    return row


async def store(
    session,
    text: str = ASK,
    *,
    message_id: int,
    at: datetime = T0,
    chat_id: int = GROUP_ID,
    chat_type: str = "group",
    sender_id: int | None = POSTER,
    username: str | None = "namuna_tekstil",
    name: str | None = "Rustam Karimov",
    bio: str | None = BIO,
) -> int:
    """One row as the reader stores it."""
    hits = prefilter.hits(text, chat_type=chat_type)
    assert hits, text
    row_id = await ingest.store_hit(
        session,
        ingest.Incoming(
            tg_chat_id=chat_id,
            tg_message_id=message_id,
            text=text,
            occurred_at=at,
            sender_id=sender_id,
            sender_username=username,
            sender_name=name,
            media_kind="none",
            reply_to_message_id=None,
            fwd_from_peer_id=None,
        ),
        hits,
    )
    assert row_id is not None
    await session.execute(
        sa.update(m.Interaction)
        .where(m.Interaction.id == row_id)
        .values(created_at=at + timedelta(seconds=20), sender_bio=bio)
    )
    await session.commit()
    return row_id


async def link(fetcher: Fetcher, tmp_path, *, now: datetime = NOW) -> None:
    """The collector run that links the stored rows to their evidence."""
    report = await run_collector(
        tm.TelegramMonitor,
        fetcher=fetcher,
        now=now,
        clock=lambda: now,
        evidence_root=tmp_path,
    )
    assert report.status == "ok", report


async def interaction(session, row_id: int) -> m.Interaction:
    row = await session.get(m.Interaction, row_id, populate_existing=True)
    assert row is not None
    return row


async def model_jobs(session) -> list[m.ModelJob]:
    return list(
        await session.scalars(
            sa.select(m.ModelJob)
            .order_by(m.ModelJob.id)
            .execution_options(populate_existing=True)
        )
    )


async def usage(session) -> list[m.UsageLog]:
    return list(
        await session.scalars(
            sa.select(m.UsageLog)
            .order_by(m.UsageLog.id)
            .execution_options(populate_existing=True)
        )
    )
