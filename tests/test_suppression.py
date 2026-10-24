"""Suppression — the feed half (07 §1.5): the hash rule shared with the ERP and
the freshness gate's one home. The DNC precedence, the any-key rule and the
gate's own checks are T-19's (``services/suppression.py``) and join this file
there; the sync itself is ``tests/test_erp_feeds.py``.
"""

from __future__ import annotations

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


def test_the_feed_not_configured_is_fresh():
    # The «Boshqa» fallback: the XLSX import carries the gate (05 §B.3).
    assert suppression_fresh(None, key=KEY, now=NOW, configured=False)
    assert suppression_fresh(cfg(100, 100), key=KEY, now=NOW, configured=False)


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
    assert SuppressionConfig.from_json({"last_full_at": "garbage", "count": True}) == (
        SuppressionConfig()
    )
    # A naive stamp is not a stamp (the gate fails closed on it).
    assert SuppressionConfig.from_json({"last_full_at": "2026-10-24T10:00:00"}) == (
        SuppressionConfig()
    )


async def test_the_probe_reads_the_stored_config(session, monkeypatch):
    monkeypatch.setattr(settings, "crm_push_source", "agent")
    monkeypatch.setattr(settings, "crm_agent_key", KEY)
    report = await erp_feeds.suppression_probe(session, NOW)
    # The seed: never pulled.
    assert (report.fresh, report.full_at, report.salt_ok) == (False, None, False)
    erp_feeds.register_health_probe()
    try:
        assert health._probes.suppression is erp_feeds.suppression_probe
    finally:
        health.set_suppression_probe(None)
