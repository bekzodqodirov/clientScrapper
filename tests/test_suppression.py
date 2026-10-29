"""Suppression (07 §1.5): the hash rule shared with the ERP, the freshness
gate's one home, and the lookup (T-19, ``services/suppression.py``) — the
keys over a company's lineage as the writers store them, the any-key rule,
cooldown expiry and the agent's own DNC events. The precedence of kinds is
``tests/test_suppression_precedence.py`` (06 §5.5 names it), the push gate's
freshness refusal ``tests/test_push_gate.py``, the sync itself
``tests/test_erp_feeds.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from clientscrapper.config import Settings, settings
from clientscrapper.db.models import LeadCandidate, Suppression
from clientscrapper.services import candidates, companies, erp_feeds, health, suppression
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
from tests.gate_fixtures import candidate, contact, firm, phone, row, signal
from tests.gate_fixtures import tail as tail_of

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


# --- the lookup (T-19): keys over the lineage, as the writers store them -----------


def test_build_keys_covers_every_contact_tail_and_the_lineage():
    """02 §2.8 (seam review): every live tail clear and hashed under the
    current key, the tail to be pushed, the stored domain (not its site_key),
    the STIR, the channel and the usernames, the Telegram id, the Instagram
    handles and every lineage id; an anonymised contact adds nothing."""
    root, merged = uuid.UUID(int=1), uuid.UUID(int=2)
    lineage = [
        suppression.CompanyKeys(
            id=root,
            stir="300000011",
            website_domain="shop.namuna.example.uz",
            telegram_channel="namuna_kanal",
            instagram="namuna.shop",
        ),
        suppression.CompanyKeys(id=merged, website_domain="namuna.example.uz"),
    ]
    contacts = [
        suppression.ContactKeys(
            phone_tail="900000001", telegram_username="namuna_bir", telegram_id=5100000001
        ),
        suppression.ContactKeys(phone_tail="900000002", instagram="namuna_ikki"),
        suppression.ContactKeys(
            phone_tail="900000009", telegram_username="namuna_old", anonymised=True
        ),
    ]
    keys = suppression.build_keys(lineage, contacts, KEY, tails=["900000003 "])
    assert keys == sorted(
        [
            ("company_id", "00000000-0000-0000-0000-000000000001"),
            ("company_id", "00000000-0000-0000-0000-000000000002"),
            ("domain", "namuna.example.uz"),
            ("domain", "shop.namuna.example.uz"),
            ("instagram", "namuna.shop"),
            ("instagram", "namuna_ikki"),
            ("phone_hash", "d9d4ae601e164f812f1d"),
            ("phone_hash", hash_tail(KEY, "900000002")),
            ("phone_hash", hash_tail(KEY, "900000003")),
            ("phone_tail", "900000001"),
            ("phone_tail", "900000002"),
            ("phone_tail", "900000003"),
            ("stir", "300000011"),
            ("tg_user_id", "5100000001"),
            ("tg_username", "namuna_bir"),
            ("tg_username", "namuna_kanal"),
        ]
    )
    # No ERP key yet: the clear tails only.
    bare = suppression.build_keys(lineage, contacts, "  ", tails=["900000003"])
    assert [k for k in bare if k[0] == "phone_hash"] == []
    assert ("phone_tail", "900000003") in bare


async def test_keys_for_reads_the_merged_lineage(session):
    target = await firm(session, website_domain="namuna.example.uz")
    source = await firm(session, stir="300000012")
    await contact(session, source, phone(21), telegram_username="namuna_manba")
    await contact(session, target, phone(22), anonymised_at=NOW)
    await companies.merge_into(session, source, target, by="test", now=NOW)
    keys = await suppression.keys_for(session, target.id, secret=KEY)
    assert ("company_id", str(source.id)) in keys and (
        "company_id",
        str(target.id),
    ) in keys
    assert ("stir", "300000012") in keys  # the merged source's own column
    assert ("tg_username", "namuna_manba") in keys  # its contact, moved to the target
    assert ("phone_tail", tail_of(21)) in keys
    assert ("phone_hash", hash_tail(KEY, tail_of(21))) in keys
    assert ("phone_tail", tail_of(22)) not in keys  # anonymised
    # The merged source asks for the same lineage.
    assert await suppression.keys_for(session, source.id, secret=KEY) == keys


async def test_a_dnc_on_any_key_suppresses_the_company(session):
    """07 §1.5: a DNC on a Telegram username suppresses a candidate whose lead
    contact has a clean phone."""
    owner = await firm(session)
    lead = await contact(session, owner, phone(23))
    await contact(session, owner, tier="company", telegram_username="namuna_yozmang")
    dnc = await row(session, "dnc", "tg_username", "namuna_yozmang")
    hit = await suppression.check_company(session, owner.id, now=NOW)
    assert hit is not None and (hit.id, hit.kind) == (dnc.id, "dnc")
    cand = await candidate(session, owner, state="scored", lead=lead, last_scored_at=None)
    await signal(session, owner)
    await session.commit()
    await candidates.score_batch(now=NOW)
    got = await session.get(LeadCandidate, cand.id, populate_existing=True)
    assert got is not None and (got.state, got.suppressed_by) == ("suppressed", dnc.id)


async def test_the_feed_hash_of_a_live_tail_is_a_client(session, monkeypatch):
    monkeypatch.setattr(settings, "crm_agent_key", KEY)
    owner = await firm(session)
    await contact(session, owner, phone(24))
    feed = await row(
        session,
        "client",
        "phone_hash",
        hash_tail(KEY, tail_of(24)),
        source="erp_feed",
        salt_id=salt_id(KEY),
    )
    hit = await suppression.check_company(session, owner.id, now=NOW)
    assert hit is not None and (hit.id, hit.kind, hit.key_type) == (
        feed.id,
        "client",
        "phone_hash",
    )
    # A hash made under another key matches nothing (its salt_id differs too).
    monkeypatch.setattr(settings, "crm_agent_key", "the-new-key")
    assert await suppression.check_company(session, owner.id, now=NOW) is None


async def test_the_pushed_tail_is_looked_up_too(session):
    owner = await firm(session)
    await row(session, "client", "phone_tail", tail_of(25), source="manual_import")
    assert await suppression.check_company(session, owner.id, now=NOW) is None
    hit = await suppression.check_company(session, owner.id, now=NOW, tails=[tail_of(25)])
    assert hit is not None and hit.kind == "client"


async def test_the_domain_is_the_stored_host_not_its_site_key(session):
    """An outcome writes the host the company holds (``shop.namuna…``); the
    lookup asks that host, so the row matches — a ``site_key`` fold would ask
    ``namuna.example.uz`` and miss it."""
    owner = await firm(session, website_domain="shop.namuna.example.uz")
    lost = await row(session, "lost", "domain", "shop.namuna.example.uz")
    other = await firm(session, website_domain="namuna.example.uz")
    hit = await suppression.check_company(session, owner.id, now=NOW)
    assert hit is not None and hit.id == lost.id
    assert await suppression.check_company(session, other.id, now=NOW) is None


async def test_a_cooldown_counts_only_until_it_expires(session):
    owner = await firm(session)
    until = NOW + timedelta(hours=1)
    cool = await row(session, "cooldown", "company_id", str(owner.id), expires_at=until)
    hit = await suppression.check_company(session, owner.id, now=NOW)
    assert hit is not None and (hit.id, hit.permanent, hit.expires_at) == (
        cool.id,
        False,
        until,
    )
    assert await suppression.check_company(session, owner.id, now=until) is None


# --- the agent's own DNC events -----------------------------------------------------


async def test_the_owner_s_dnc_writes_every_key_and_suppresses(session):
    owner = await firm(
        session,
        telegram_channel="namuna_kanal",
        instagram="namuna.shop",
        website_domain="namuna.example.uz",
        stir="300000031",
    )
    lead = await contact(
        session, owner, phone(31), telegram_username="namuna_bir", instagram="namuna_ig"
    )
    cand = await candidate(session, owner, state="review", lead=lead)
    first = await suppression.dnc_for_candidate(session, cand, now=NOW, reason="🔒")
    rows = (
        await session.execute(
            sa.select(
                Suppression.id,
                Suppression.kind,
                Suppression.key_type,
                Suppression.key,
                Suppression.source,
            ).order_by(Suppression.id)
        )
    ).all()
    assert [tuple(r)[1:] for r in rows] == [
        ("dnc", "phone_tail", tail_of(31), "owner_bot"),
        ("dnc", "tg_username", "namuna_bir", "owner_bot"),
        ("dnc", "instagram", "namuna_ig", "owner_bot"),
        ("dnc", "tg_username", "namuna_kanal", "owner_bot"),
        ("dnc", "instagram", "namuna.shop", "owner_bot"),
        ("dnc", "domain", "namuna.example.uz", "owner_bot"),
        # The STIR is a DNC key too (seam review wave 4).
        ("dnc", "stir", "300000031", "owner_bot"),
    ]
    assert first == rows[0].id
    assert (cand.state, cand.suppressed_by) == ("suppressed", first)


async def test_the_owner_s_dnc_writes_the_phone_the_card_pushes(session):
    """The review's probe (9.1 shape): the card's contact is the poster with
    a username and no phone, the phone came from enrichment on a
    company-tier contact. 🔒 writes that phone (06 §1.3), so the same mobile
    at the director's second firm is suppressed too."""
    from clientscrapper.services import push_gate

    owner = await firm(session, "Namuna Tekstil Savdo")
    person = await contact(
        session,
        owner,
        None,
        tier="person",
        telegram_username="namuna_tekstil",
        role="direktor",
    )
    await contact(session, owner, phone(32))
    cand = await candidate(session, owner, state="review", lead=person, score=70)
    lead = await push_gate.lead_contact(session, cand)
    assert lead is not None and lead.phone == phone(32)
    first = await suppression.dnc_for_candidate(session, cand, now=NOW)
    rows = (
        await session.execute(
            sa.select(Suppression.kind, Suppression.key_type, Suppression.key).order_by(
                Suppression.id
            )
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        ("dnc", "phone_tail", tail_of(32)),
        ("dnc", "tg_username", "namuna_tekstil"),
    ]
    assert (cand.state, cand.suppressed_by) == ("suppressed", first)
    other = await firm(session, "Namuna Mato Import")
    await contact(session, other, phone(32))
    hit = await suppression.check_company(session, other.id, now=NOW)
    assert hit is not None and (hit.kind, hit.key_type) == ("dnc", "phone_tail")


async def test_an_anonymised_contact_s_kept_number_is_no_dnc_key(session):
    owner = await firm(session)
    gone = await contact(
        session, owner, "+998712000033", anonymised_at=NOW - timedelta(days=1)
    )
    cand = await candidate(session, owner, state="review", lead=gone, score=70)
    await suppression.dnc_for_candidate(session, cand, now=NOW)
    rows = (await session.execute(sa.select(Suppression.key_type))).scalars().all()
    assert rows == ["company_id"]


async def test_the_owner_s_dnc_reads_the_candidate_as_stored(session):
    """The candidate is locked and re-read: one a push ended meanwhile keeps
    ``pushed`` — the rows are written, the person said «yozmang»."""
    owner = await firm(session)
    lead = await contact(session, owner, phone(34))
    cand = await candidate(session, owner, state="approved", lead=lead, score=70)
    # Another session's push: the row moves, the caller's object does not.
    await session.execute(
        sa.text(
            "UPDATE lead_candidates SET state = 'pushed', external_id = :eid,"
            " pushed_at = :at WHERE id = :id"
        ),
        {"eid": f"cs:{owner.id}:202610", "at": NOW, "id": cand.id},
    )
    assert cand.state == "approved"
    first = await suppression.dnc_for_candidate(session, cand, now=NOW)
    got = await session.get(LeadCandidate, cand.id)
    assert got is not None and (got.state, got.suppressed_by) == ("pushed", None)
    held = await session.get(Suppression, first)
    assert held is not None and (held.kind, held.key) == ("dnc", tail_of(34))


async def test_a_dnc_on_a_keyless_company_names_the_company(session):
    owner = await firm(session)
    cand = await candidate(session, owner, state="hold", score=40)
    first = await suppression.dnc_for_candidate(session, cand, now=NOW)
    held = await session.get(Suppression, first)
    assert held is not None
    assert (held.kind, held.key_type, held.key) == ("dnc", "company_id", str(owner.id))


@pytest.mark.parametrize(
    ("arg", "want"),
    [
        ("+998 90 000 00 41", ("phone_tail", "900000041")),
        ("90 000-00-42", ("phone_tail", "900000042")),
        ("@namuna_user", ("tg_username", "namuna_user")),
        ("https://t.me/Namuna_User", ("tg_username", "namuna_user")),
    ],
)
async def test_the_dnc_command_writes_one_row(session, arg, want):
    done = await suppression.dnc_command(session, arg, now=NOW)
    assert done.row_id is not None and done.existing is None
    stored = await session.get(Suppression, done.row_id)
    assert stored is not None
    assert (done.entry.key_type, done.entry.key) == want
    assert (stored.kind, stored.key_type, stored.key, stored.source) == (
        "dnc",
        *want,
        "owner_bot",
    )
    # A second /dnc of the same key writes nothing and names the row that
    # holds it: «Allaqachon bor ({kind}, {dd.mm.yyyy})» (06 §1.2).
    again = await suppression.dnc_command(session, arg, now=NOW)
    assert again.row_id is None and again.existing is not None
    assert (again.existing.id, again.existing.kind) == (done.row_id, "dnc")
    assert again.existing.created_at == stored.created_at
    assert await session.scalar(sa.select(sa.func.count(Suppression.id))) == 1


async def test_a_dnc_command_on_a_key_a_client_row_holds_writes_nothing(session):
    held = await row(session, "client", "phone_tail", tail_of(44), source="outcomes")
    done = await suppression.dnc_command(session, phone(44), now=NOW)
    assert done.existing is not None and (done.existing.id, done.existing.kind) == (
        held.id,
        "client",
    )
    assert (done.row_id, done.suppressed) == (None, ())


async def test_a_dnc_command_over_a_cooldown_writes_the_lasting_row(session):
    await row(
        session,
        "cooldown",
        "phone_tail",
        tail_of(45),
        expires_at=NOW + timedelta(days=30),
    )
    done = await suppression.dnc_command(session, phone(45), now=NOW)
    assert done.existing is None and done.row_id is not None


async def test_a_dnc_command_suppresses_the_open_candidates_its_key_names(session):
    """06 §1.2: «open lead_candidates of that company → suppressed» — the
    company holding the tail on a live contact, the one whose channel is the
    handle, and a merged source's key on its root; a pushed candidate keeps
    its state, an anonymised contact names no company."""
    by_phone = await firm(session)
    await contact(session, by_phone, phone(46))
    open_one = await candidate(session, by_phone, state="review", score=70)
    by_channel = await firm(session, telegram_channel="namuna_kanal")
    channel_one = await candidate(session, by_channel, state="hold", score=40)
    root = await firm(session)
    source = await firm(session)
    await contact(session, source, phone(46))
    root_one = await candidate(session, root, state="enriching", score=50)
    await companies.merge_into(session, source, root, by="owner:111111111", now=NOW)
    gone = await firm(session)
    await contact(session, gone, phone(46), anonymised_at=NOW)
    gone_one = await candidate(session, gone, state="review", score=70)
    done_firm = await firm(session)
    await contact(session, done_firm, phone(46))
    pushed = await candidate(session, done_firm, state="pushed", score=70)

    done = await suppression.dnc_command(session, phone(46), now=NOW)
    assert done.row_id is not None
    assert set(done.suppressed) == {open_one.id, root_one.id}
    for cand, state in (
        (open_one, "suppressed"),
        (root_one, "suppressed"),
        (gone_one, "review"),
        (pushed, "pushed"),
    ):
        got = await session.get(LeadCandidate, cand.id, populate_existing=True)
        assert got is not None and got.state == state
        if state == "suppressed":
            assert got.suppressed_by == done.row_id
    by_handle = await suppression.dnc_command(session, "@namuna_kanal", now=NOW)
    assert by_handle.suppressed == (channel_one.id,)


async def test_a_dnc_command_on_a_merged_source_s_own_key_reaches_the_root(session):
    """A key only the merged source still holds — its decision maker, kept
    on the source because the root has one (one person per company, 04
    §2.5) — names the source; its lineage's open candidate, the root's, is
    the one to end."""
    root = await firm(session)
    source = await firm(session)
    await contact(session, root, None, tier="person", telegram_username="namuna_bosh")
    await contact(session, source, None, tier="person", telegram_username="namuna_eski")
    root_one = await candidate(session, root, state="review", score=70)
    await companies.merge_into(session, source, root, by="owner:111111111", now=NOW)
    held = await suppression.holders(
        session, suppression.Entry("dnc", "tg_username", "namuna_eski")
    )
    assert held == [source.id]
    done = await suppression.dnc_command(session, "@namuna_eski", now=NOW)
    assert done.suppressed == (root_one.id,)


@pytest.mark.parametrize("arg", ["hello", "1234", "@ab", "t.me/joinchat/xyz", ""])
def test_the_dnc_command_refuses_what_is_no_key(arg):
    with pytest.raises(ValueError):
        suppression.dnc_command_entry(arg)


async def test_a_company_wide_kind_and_the_cooldown_upsert(session):
    owner = await firm(session)
    veto = await suppression.suppress_company(
        session, owner.id, "manager_veto", source="owner_bot"
    )
    assert (
        await suppression.suppress_company(
            session, owner.id, "manager_veto", source="owner_bot"
        )
        == veto
    )
    with pytest.raises(ValueError):
        await suppression.suppress_company(session, owner.id, "dnc", source="owner_bot")
    cool = suppression.Entry("cooldown", "phone_tail", "900000043")
    later = NOW + timedelta(days=30)
    [first] = await suppression.add(session, [cool], source="owner_bot", expires_at=later)
    [again] = await suppression.add(
        session, [cool], source="owner_bot", expires_at=NOW + timedelta(days=1)
    )
    held = await session.get(Suppression, first, populate_existing=True)
    assert held is not None and (again, held.expires_at) == (first, later)
    with pytest.raises(ValueError):
        await suppression.add(session, [cool], source="owner_bot")  # a cooldown expires


@pytest.mark.parametrize(
    ("kind", "key_type", "raw"),
    [
        ("client", "phone_tail", "900000044"),  # the ERP's kinds are not written here
        ("lost", "domain", "namuna.example.uz"),
        ("dnc", "phone_hash", "d9d4ae601e164f812f1d"),
        ("dnc", "phone_tail", "12345"),
        ("dnc", "stir", "123456789"),
        ("dnc", "domain", "instagram.com"),
    ],
)
def test_an_entry_refuses_what_it_may_not_write(kind, key_type, raw):
    with pytest.raises(ValueError):
        suppression.entry(kind, key_type, raw)


@pytest.mark.parametrize(
    ("key_type", "raw", "key"),
    [
        ("phone_tail", "+998 90 000 00 45", "900000045"),
        ("tg_username", "@Namuna_User", "namuna_user"),
        ("instagram", "https://instagram.com/Namuna.Shop/", "namuna.shop"),
        ("domain", "https://www.Namuna.example.uz/about", "namuna.example.uz"),
        ("stir", "STIR 300000013", "300000013"),
        ("tg_user_id", "5100000001", "5100000001"),
    ],
)
def test_an_entry_stores_the_key_shape(key_type, raw, key):
    assert suppression.entry("dnc", key_type, raw) == suppression.Entry(
        "dnc", key_type, key
    )
