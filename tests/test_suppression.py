"""Suppression — the feed half (07 §1.5): the hash rule shared with the ERP and
the freshness gate's one home. The DNC precedence, the any-key rule and the
gate's own checks are T-19's (``services/suppression.py``) and join this file
there; the sync itself is ``tests/test_erp_feeds.py``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from clientscrapper.config import Settings, settings
from clientscrapper.services import erp_feeds, health
from clientscrapper.services.erp_feeds import (
    DELTA_MAX_AGE_H,
    FULL_MAX_AGE_H,
    SuppressionConfig,
    feeds_configured,
    freshness,
    hash_tail,
    salt_id,
    suppression_fresh,
)
from tests import fake_erp

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
KEY = "test-secret"


# --- the hash rule ------------------------------------------------------------------


def test_the_vectors_both_repos_pin():
    # fullprompt tests/unit/agent-feed.test.ts, computed there with openssl.
    assert hash_tail("k", "901234567") == "2ea6869e504cebca50f0"
    assert salt_id("k") == "8254c329"


def test_the_agent_side_vectors():
    assert hash_tail("test-secret", "900000001") == "d9d4ae601e164f812f1d"
    assert salt_id("test-secret") == "9caf06bb"


@pytest.mark.parametrize(
    "raw", ["+998 90 123 45 67", "1234567", "90123456", "9012345678"]
)
def test_the_hash_takes_the_nine_digit_tail_only(raw):
    # One rule, one guard (services/keys.erp_phone_hash): a raw phone or a
    # short tail would hash, match nothing and fail open without a sound.
    with pytest.raises(ValueError):
        hash_tail("k", raw)


def test_the_fake_erp_hashes_as_the_agent_does():
    for tail in ("901234567", "900000001", "000000000"):
        assert fake_erp.hash_tail(KEY, tail) == hash_tail(KEY, tail)
    assert fake_erp.salt_id_for(KEY) == salt_id(KEY)
    assert erp_feeds.FEED_RULE == fake_erp.HASH_RULE


# --- freshness: the one home ------------------------------------------------------------


def cfg(
    full_h: float | None, delta_h: float | None, salt: str | None = None
) -> SuppressionConfig:
    return SuppressionConfig(
        salt_id=salt_id(KEY) if salt is None else salt,
        last_full_at=None if full_h is None else NOW - timedelta(hours=full_h),
        last_delta_at=None if delta_h is None else NOW - timedelta(hours=delta_h),
        count=10,
    )


def test_the_limits_are_26_and_3_hours():
    assert (FULL_MAX_AGE_H, DELTA_MAX_AGE_H) == (26, 3)


@pytest.mark.parametrize(
    ("full_h", "delta_h", "fresh"),
    [
        (1, 0.5, True),
        (26, 3, True),
        (26 + 1 / 3600, 1, False),
        (25, 3 + 1 / 3600, False),
        (None, 1, False),
        (1, None, False),
        (None, None, False),
    ],
)
def test_suppression_fresh_at_its_edges(full_h, delta_h, fresh):
    assert (
        suppression_fresh(cfg(full_h, delta_h), key=KEY, now=NOW, configured=True)
        is fresh
    )


def test_a_salt_mismatch_is_stale_however_new_the_pulls():
    stale = cfg(0, 0, salt=salt_id("the-old-key"))
    assert not suppression_fresh(stale, key=KEY, now=NOW, configured=True)
    assert not suppression_fresh(SuppressionConfig(), key=KEY, now=NOW, configured=True)
    assert not suppression_fresh(None, key=KEY, now=NOW, configured=True)
    report = freshness(stale, key=KEY, now=NOW, configured=True)
    assert (report.fresh, report.salt_ok, report.stale_since) == (False, False, None)


def test_stale_since_counts_from_the_first_try_of_the_key():
    tried = NOW - timedelta(hours=7)
    never = SuppressionConfig(awaiting_salt_id=salt_id(KEY), awaiting_since=tried)
    assert freshness(never, key=KEY, now=NOW, configured=True).stale_since == tried
    # A marker another key left says nothing about this one.
    other = SuppressionConfig(awaiting_salt_id="0badc0de", awaiting_since=tried)
    assert freshness(other, key=KEY, now=NOW, configured=True).stale_since is None
    # Aged stamps and a marker: the earlier moment.
    both = SuppressionConfig(
        salt_id=salt_id("the-old-key"),
        last_full_at=NOW - timedelta(hours=40),
        last_delta_at=NOW - timedelta(hours=1),
        count=10,
        awaiting_salt_id=salt_id(KEY),
        awaiting_since=NOW - timedelta(hours=2),
    )
    assert freshness(both, key=KEY, now=NOW, configured=True).stale_since == (
        NOW - timedelta(hours=14)
    )
    # A completed pull under this key: the marker is history, not staleness.
    done = replace(cfg(1, 0.5), awaiting_salt_id=salt_id(KEY), awaiting_since=tried)
    report = freshness(done, key=KEY, now=NOW, configured=True)
    assert (report.fresh, report.stale_since) == (True, None)


def test_the_fallback_is_fresh_only_with_a_recent_import():
    """The «Boshqa» fallback (05 §B.3): the XLSX import is the only client
    guard, so the gate passes only while one exists and is at most
    IMPORT_MAX_AGE_D days old — no import, no push (seam review: it used to
    pass with an empty suppression table)."""
    assert erp_feeds.IMPORT_MAX_AGE_D == 8  # the weekly re-import plus a day
    never = freshness(None, key=KEY, now=NOW, configured=False)
    assert (never.fresh, never.fallback, never.imported_at, never.stale_since) == (
        False,
        True,
        None,
        None,
    )
    for days in (0, 7, 8):
        at = NOW - timedelta(days=days)
        assert suppression_fresh(None, key=KEY, now=NOW, configured=False, imported_at=at)
    old = NOW - timedelta(days=8, seconds=1)
    report = freshness(None, key=KEY, now=NOW, configured=False, imported_at=old)
    assert (report.fresh, report.stale_since, report.import_max_d) == (
        False,
        old + timedelta(days=8),
        8,
    )


def test_an_import_older_than_the_last_full_pull_does_not_carry_the_fallback():
    """Back on «Boshqa» after the agent door: the full pulls retired the
    imported tails their hashes carried, and those hashes (the agent key's)
    match nothing now. Only an import made after the last full pull counts."""
    pulled = cfg(30, 30)  # the last full pull 30 h ago
    before = freshness(
        pulled, key=KEY, now=NOW, configured=False, imported_at=NOW - timedelta(days=2)
    )
    assert (before.fresh, before.import_before_feed, before.stale_since) == (
        False,
        True,
        None,
    )
    after = freshness(
        pulled, key=KEY, now=NOW, configured=False, imported_at=NOW - timedelta(hours=1)
    )
    assert (after.fresh, after.import_before_feed) == (True, False)
    # The feeds on, the import says nothing.
    assert not suppression_fresh(
        pulled, key=KEY, now=NOW, configured=True, imported_at=NOW
    )


@pytest.mark.parametrize(
    ("imported", "before", "fragment"),
    [
        (None, False, "Mijozlar roʻyxati hali import qilinmagan"),
        (timedelta(days=9), False, "9 kun oldin import qilingan (chegara 8 kun)"),
        (timedelta(days=2), True, "ERP roʻyxatidan oldin import qilingan"),
    ],
)
def test_the_fallback_line_names_the_import(imported, before, fragment):
    from tests.test_uz_text import problems as uz_problems

    sup = health.SuppressionHealth(
        fresh=False,
        full_at=None,
        delta_at=None,
        full_max_h=26,
        delta_max_h=3,
        fallback=True,
        imported_at=None if imported is None else NOW - imported,
        import_max_d=8,
        import_before_feed=before,
    )
    status = health.Status(
        now=NOW,
        db_ok=True,
        disk_free_bytes=None,
        disk_total_bytes=None,
        disk_low=False,
        backup=health.BackupInfo(configured=True),
        suppression=sup,
    )
    [problem] = [p for p in health.problems(status) if p.key == "suppression_stale"]
    assert fragment in problem.text
    assert "<code>make import-clients</code>" in problem.text
    assert uz_problems(problem.text, rendered=True) == set()


def test_freshness_says_what_health_prints():
    report = freshness(cfg(30, 1), key=KEY, now=NOW, configured=True)
    assert report == health.SuppressionHealth(
        fresh=False,
        full_at=NOW - timedelta(hours=30),
        delta_at=NOW - timedelta(hours=1),
        full_max_h=26,
        delta_max_h=3,
        salt_ok=True,
        stale_since=NOW - timedelta(hours=4),
    )
    both = freshness(cfg(27, 5), key=KEY, now=NOW, configured=True)
    # The first limit that ran out: the delta's, three hours after it.
    assert both.stale_since == NOW - timedelta(hours=2)


def test_feeds_configured_needs_the_agent_door_and_its_key():
    base = settings.model_dump()
    assert feeds_configured(Settings(**{**base, "crm_push_source": "agent",
                                        "crm_agent_key": "x"}))  # fmt: skip
    assert not feeds_configured(Settings(**{**base, "crm_push_source": "other",
                                            "crm_agent_key": "x"}))  # fmt: skip
    assert not feeds_configured(Settings(**{**base, "crm_push_source": "agent",
                                            "crm_agent_key": "  "}))  # fmt: skip


def test_the_config_round_trips_and_reads_the_seed():
    seeded = {"salt_id": None, "last_full_at": None, "last_delta_at": None, "count": 0}
    assert SuppressionConfig.from_json(seeded) == SuppressionConfig()
    full = cfg(1, 0.5)
    assert SuppressionConfig.from_json(full.to_json()) == full
    waiting = SuppressionConfig(awaiting_salt_id="9caf06bb", awaiting_since=NOW)
    assert SuppressionConfig.from_json(waiting.to_json()) == waiting
    assert SuppressionConfig.from_json({"last_full_at": "garbage", "count": True}) == (
        SuppressionConfig()
    )
    # A naive stamp is not a stamp (the gate fails closed on it).
    assert SuppressionConfig.from_json({"last_full_at": "2026-10-24T10:00:00"}) == (
        SuppressionConfig()
    )


async def test_the_probe_reads_the_last_import_during_the_fallback(session, monkeypatch):
    from tests.test_client_import import XLSX, imported

    monkeypatch.setattr(settings, "crm_push_source", "other")
    monkeypatch.setattr(settings, "crm_agent_key", KEY)
    row = await session.get(erp_feeds.Source, erp_feeds.ERP_SOURCE)
    assert row is not None
    seeded = dict(row.config)
    try:
        report = await erp_feeds.suppression_probe(session, NOW)
        assert (report.fresh, report.fallback, report.imported_at) == (False, True, None)
        from clientscrapper.services import client_import as ci

        await imported(session, ci.read_rows(XLSX))
        last = await ci.last_import(session)
        assert last is not None
        report = await erp_feeds.suppression_probe(session, last.at)
        assert (report.fresh, report.imported_at) == (True, last.at)
        later = last.at + timedelta(days=9)
        assert not (await erp_feeds.suppression_probe(session, later)).fresh
    finally:
        row = await session.get(erp_feeds.Source, erp_feeds.ERP_SOURCE)
        assert row is not None
        row.config = seeded
        await session.commit()


async def test_the_probe_reads_the_stored_config(session, monkeypatch):
    monkeypatch.setattr(settings, "crm_push_source", "agent")
    monkeypatch.setattr(settings, "crm_agent_key", KEY)
    report = await erp_feeds.suppression_probe(session, NOW)
    # The seed: never pulled, and the source shipped off — the line says so.
    assert (report.fresh, report.full_at, report.salt_ok) == (False, None, False)
    assert report.source_off is True
    status = health.Status(
        now=NOW,
        db_ok=True,
        disk_free_bytes=None,
        disk_total_bytes=None,
        disk_low=False,
        backup=health.BackupInfo(configured=True),
        suppression=report,
    )
    [line] = [p.text for p in health.problems(status) if p.key == "suppression_stale"]
    assert "«erp_sync» manbasi oʻchiq — /manbalar orqali yoqing" in line
    row = await session.get(erp_feeds.Source, erp_feeds.ERP_SOURCE)
    assert row is not None and row.enabled is False
    row.enabled = True
    await session.commit()
    try:
        assert (await erp_feeds.suppression_probe(session, NOW)).source_off is False
    finally:
        row.enabled = False
        await session.commit()
    erp_feeds.register_health_probe()
    try:
        assert health._probes.suppression is erp_feeds.suppression_probe
    finally:
        health.set_suppression_probe(None)
