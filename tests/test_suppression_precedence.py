"""Suppression precedence (06 §5.5, 07 §1.5 «DNC precedence»).

``KIND_RANK`` is one constant in ``services/suppression.py`` and the lookup's
SQL is rendered from it — ``array_position(ARRAY[…], kind)``, never ``ORDER
BY kind`` (which would put ``b2c``/``bounced`` first). These tests pin the
rendered array against the constant, not a copy, and walk the precedence on
real rows: a permanent kind beside a live cooldown is what the gate reports,
the candidate becomes ``suppressed`` and not ``hold``, the permanent hit
still holds after the cooldown expired, and ``client`` beats ``lost`` for
``companies.state``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from clientscrapper.db import enums as e
from clientscrapper.db.models import Company, LeadCandidate
from clientscrapper.services import candidates, suppression
from clientscrapper.services.suppression import KIND_ORDER, KIND_ORDER_SQL, KIND_RANK
from tests.gate_fixtures import NOW, candidate, contact, firm, phone, row, signal, tail

SPEC_ORDER = (
    "legal",
    "dnc",
    "client",
    "lost",
    "bounced",
    "competitor",
    "manager_veto",
    "b2c",
    "cooldown",
)


def test_kind_rank_is_06_section_5_5():
    assert KIND_ORDER == SPEC_ORDER
    assert [KIND_RANK[k] for k in SPEC_ORDER] == list(range(9))
    assert set(KIND_RANK) == set(e.SUPPRESSION_KINDS)


def test_the_lookup_is_rendered_from_the_constant():
    sql = str(
        suppression.lookup_statement([("phone_tail", "900000001")], now=NOW).compile(
            dialect=postgresql.dialect()
        )
    )
    rendered = "ARRAY[" + ", ".join(f"'{k}'" for k in KIND_RANK) + "]"
    assert rendered == KIND_ORDER_SQL
    assert f"ORDER BY array_position({rendered}, suppression.kind), suppression.id" in sql
    assert "LIMIT" in sql
    # 02 §2.8: a cooldown counts only while it has not expired.
    assert "suppression.expires_at IS NULL OR suppression.expires_at >" in sql


@pytest.mark.parametrize("permanent", [k for k in SPEC_ORDER if k != "cooldown"])
async def test_a_permanent_kind_beats_a_live_cooldown(session, permanent):
    owner = await firm(session)
    lead = await contact(session, owner, phone(11), telegram_username="namuna_bir")
    await row(
        session, "cooldown", "phone_tail", tail(11), expires_at=NOW + timedelta(days=30)
    )
    held = await row(
        session,
        permanent,
        "tg_username",
        "namuna_bir",
        source="outcomes" if permanent in ("client", "lost") else None,
    )
    keys = await suppression.keys_for(session, owner.id)
    hit = await suppression.is_suppressed(session, keys, now=NOW)
    assert hit is not None and (hit.id, hit.kind, hit.permanent) == (
        held.id,
        permanent,
        True,
    )
    # The cooldown gone, the permanent row still decides.
    later = await suppression.is_suppressed(session, keys, now=NOW + timedelta(days=60))
    assert later is not None and later.id == held.id
    assert lead.phone_tail == tail(11)


async def test_every_pair_reports_the_lower_rank(session):
    owner = await firm(session)
    await contact(session, owner, phone(12))
    keys = await suppression.keys_for(session, owner.id)
    # Written worst-last, so neither insertion order nor id decides.
    for kind in reversed(SPEC_ORDER):
        await row(
            session,
            kind,
            "company_id",
            str(owner.id),
            source="outcomes" if kind in ("client", "lost") else None,
            expires_at=NOW + timedelta(days=1) if kind == "cooldown" else None,
        )
        hit = await suppression.is_suppressed(session, keys, now=NOW)
        assert hit is not None and hit.kind == kind


async def test_cooldown_and_dnc_suppress_the_candidate_never_hold_it(session):
    """07 §1.5: the candidate becomes ``suppressed`` (by the permanent row),
    not ``hold`` (the cooldown), through score_job's gates."""
    owner = await firm(session)
    lead = await contact(session, owner, phone(13))
    await row(
        session,
        "cooldown",
        "company_id",
        str(owner.id),
        expires_at=NOW + timedelta(days=90),
    )
    dnc = await row(session, "dnc", "phone_tail", tail(13))
    cand = await candidate(session, owner, state="scored", lead=lead, last_scored_at=None)
    await signal(session, owner)
    await session.commit()
    await candidates.score_batch(now=NOW)
    got = await session.get(LeadCandidate, cand.id, populate_existing=True)
    assert got is not None
    assert (got.state, got.suppressed_by, got.hold_until) == ("suppressed", dnc.id, None)
    assert got.score_breakdown is not None
    assert got.score_breakdown["gates"]["not_dnc"] == "fail:dnc"


async def test_a_lone_cooldown_holds_until_it_expires(session):
    owner = await firm(session)
    lead = await contact(session, owner, phone(14))
    until = NOW + timedelta(days=180)
    await row(session, "cooldown", "company_id", str(owner.id), expires_at=until)
    cand = await candidate(session, owner, state="scored", lead=lead, last_scored_at=None)
    await signal(session, owner)
    await session.commit()
    await candidates.score_batch(now=NOW)
    got = await session.get(LeadCandidate, cand.id, populate_existing=True)
    assert got is not None
    assert (got.state, got.hold_until, got.suppressed_by) == ("hold", until, None)
    assert got.score_breakdown is not None
    assert got.score_breakdown["gates"]["not_recent"] == "fail:cooldown"


async def test_client_beats_lost_for_the_company_state(session):
    owner = await firm(session)
    lead = await contact(session, owner, phone(15))
    await row(session, "lost", "phone_tail", tail(15))
    client = await row(session, "client", "company_id", str(owner.id))
    cand = await candidate(session, owner, state="scored", lead=lead, last_scored_at=None)
    await signal(session, owner)
    await session.commit()
    await candidates.score_batch(now=NOW)
    got = await session.get(LeadCandidate, cand.id, populate_existing=True)
    state = await session.scalar(
        sa.select(Company.state)
        .where(Company.id == owner.id)
        .execution_options(populate_existing=True)
    )
    assert got is not None
    assert (got.state, got.suppressed_by, state) == ("suppressed", client.id, "client")
