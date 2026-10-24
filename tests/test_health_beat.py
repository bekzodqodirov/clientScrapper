# VENDORED_FROM dailyJournal@6b1f67f tests/test_health.py
"""The heartbeat core (06 §4.1): MIYA's beat/beats tests, plus the collector
component names of 03 §1.2. The problem list is T-08's ``test_health.py``."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from clientscrapper.db import models as m
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import health

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=ZoneInfo("Asia/Tashkent"))


async def test_beat_upserts_one_row_and_replaces_the_detail(session):
    await health.beat(session, "worker", detail={"jobs": 17}, now=NOW)
    await health.beat(
        session, "worker", detail={"other": True}, now=NOW + timedelta(minutes=1)
    )

    rows = await health.beats(session)

    assert list(rows) == ["worker"]
    assert rows["worker"].last_seen_at == NOW + timedelta(minutes=1)
    assert rows["worker"].detail == {"other": True}  # replaced, not merged
    count = await session.scalar(sa.select(sa.func.count()).select_from(m.Heartbeat))
    assert count == 1


async def test_an_older_beat_committed_later_does_not_move_the_clock_back(session):
    later = NOW + timedelta(minutes=5)
    await health.beat(session, "collector:directories", detail={"run": 2}, now=later)
    await health.beat(session, "collector:directories", detail={"run": 1}, now=NOW)
    row = (await health.beats(session))["collector:directories"]
    assert (row.last_seen_at, row.detail) == (later, {"run": 2})


async def test_concurrent_beats_keep_the_latest(session):
    # The seam review's probe: 40 beats at once, T+i s each; the stored row
    # is the latest, whichever transaction commits last.
    async def one(i: int) -> None:
        async with SessionLocal() as own:
            await health.beat(
                own, "collector:maps", detail={"i": i}, now=NOW + timedelta(seconds=i)
            )
            await asyncio.sleep(0.001 * ((i * 7) % 5))
            await own.commit()

    await asyncio.gather(*(one(i) for i in range(40)))
    row = (await health.beats(session))["collector:maps"]
    assert (row.last_seen_at, row.detail) == (NOW + timedelta(seconds=39), {"i": 39})


async def test_beat_without_detail_stores_an_empty_dict(session):
    await health.beat(session, "bot", detail={"x": 1}, now=NOW)
    await health.beat(session, "bot", now=NOW)
    rows = await health.beats(session)
    assert rows["bot"].detail == {}


async def test_beats_keys_every_component_and_job(session):
    await health.beat(session, "bot", now=NOW)
    await health.beat(session, "job:backup", detail={"ok": True}, now=NOW)
    await health.beat(session, health.collector_component("erp_sync", "full"), now=NOW)

    rows = await health.beats(session)

    assert set(rows) == {"bot", "job:backup", "collector:erp_sync.full"}
    assert rows["job:backup"].detail == {"ok": True}


@pytest.mark.parametrize(
    ("key", "variant", "name"),
    [
        ("hh_vacancies", None, "collector:hh_vacancies"),
        ("erp_sync", "full", "collector:erp_sync.full"),
        ("erp_sync", "delta", "collector:erp_sync.delta"),
        ("telegram_monitor", "backfill", "collector:telegram_monitor.backfill"),
        ("serp_layer", "night", "collector:serp_layer.night"),
    ],
)
def test_collector_component_names(key, variant, name):
    assert health.collector_component(key, variant) == name


@pytest.mark.parametrize(
    ("key", "variant"),
    [("HH", None), ("x", None), ("erp_sync", "Full"), ("erp_sync", "a.b"), ("", None)],
)
def test_collector_component_refuses_names_no_reader_matches(key, variant):
    with pytest.raises(ValueError):
        health.collector_component(key, variant)
