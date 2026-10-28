"""The pre-push checklist and the claim (04 §7, 02 §2.10; 04 AC 10).

One fully passing approved candidate (``pushable``), then each item broken
alone answers exactly one refusal, of that item: ``[(check, code)]``. Item 14
is the recording: an owner-visible refusal reaches ``lead_reviews.note`` (and
what it means reaches the candidate), a ``cap``/``window`` refusal writes
nothing. Two workers claiming one candidate produce one ``push_log`` row.

No HTTP is made here — the gate decides before the POST; the fake ERP is
``tests/test_crm_push.py``'s (T-28). Fake clock: ``NOW`` (a Tuesday 11:00
Tashkent) and explicit moments. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db.models import (
    Company,
    Contact,
    LeadCandidate,
    LeadReview,
    PushLog,
    RuntimeFlag,
    Source,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import erp_feeds, push_gate
from tests import gate_fixtures
from tests.gate_fixtures import (
    NOW,
    TZ,
    candidate,
    contact,
    firm,
    fresh_config,
    phone,
    push,
    row,
    set_config,
    signal,
    tail,
)
from tests.test_uz_text import problems as uz_problems

ENDPOINT = "/api/leads/" + "in/agent"  # the claim stores what crm_push hands it


@pytest.fixture
async def erp_ready(session, monkeypatch):
    async with gate_fixtures.erp_ready(session, monkeypatch):
        yield


async def pushable(
    session, nn: int = 61, *, number: str | None = None, lead_fields=None, **firm_fields
) -> tuple[Company, Contact, LeadCandidate, dict[str, Any]]:
    owner = await firm(session, **firm_fields)
    lead = await contact(session, owner, number or phone(nn), **(lead_fields or {}))
    cand = await candidate(session, owner, state="approved", lead=lead, score=72)
    body = {
        "external_id": push_gate.external_id_for(owner.id, NOW),
        "name": owner.display_name,
        "phone": lead.phone,
        "note": "NEGA TOPILDI: Telegram guruh · 27.10.2026 · «2 kub mato kerak»",
        "utm_source": "clientscrapper",
        "score": "72",
    }
    return owner, lead, cand, body


async def tapped(session, cand, *, note=None, minutes=5, state="approved") -> LeadReview:
    """The owner's answer on the card (``push``, an answered row)."""
    answered = state in ("approved", "declined", "merged")
    found = LeadReview(
        candidate_id=cand.id,
        kind="owner",
        state=state,
        answer={"approved": "push", "declined": "decline"}.get(state),
        answered_at=NOW - timedelta(minutes=minutes) if answered else None,
        note=note,
    )
    session.add(found)
    await session.flush()
    return found


async def refusals(
    session, cand, body, *, now=NOW, repush_of=None
) -> list[tuple[int, str]]:
    found = await push_gate.check(
        session, cand, now=now, payload=body, repush_of=repush_of
    )
    return [(r.check, r.code) for r in found]


async def test_a_clean_candidate_passes_every_item(session, erp_ready):
    _, _, cand, body = await pushable(session)
    assert await refusals(session, cand, body) == []


# --- 1 --------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["review", "hold", "scored", "pushed", "rejected"])
async def test_1_only_an_approved_candidate(session, erp_ready, state):
    _, _, cand, body = await pushable(session)
    cand.state = state
    assert await refusals(session, cand, body) == [(1, "not_approved")]


async def test_1_an_approval_names_who(session, erp_ready):
    _, _, cand, body = await pushable(session)
    cand.approved_by = None  # not flushed: the CHECK would refuse the row
    assert await refusals(session, cand, body) == [(1, "not_approved")]


async def test_1_a_repush_of_its_own_id_reads_pushed(session, erp_ready):
    owner, lead, cand, body = await pushable(session)
    eid = body["external_id"]
    cand.state, cand.external_id, cand.pushed_at = "pushed", eid, NOW - timedelta(days=3)
    sent = await push(
        session,
        cand,
        sent_at=NOW - timedelta(days=3),
        phone_tail=tail(61),
        external_id=eid,
        payload_sha256=push_gate.canonical_sha256(body),
        superseded_at=NOW,
    )
    plan = await push_gate.evaluate(session, cand, now=NOW, payload=body, repush_of=sent)
    assert (plan.refusals, plan.attempt, plan.external_id) == ([], 2, eid)
    # Another candidate's row is never this one's re-push.
    other = await candidate(session, await firm(session), state="pushed", score=72)
    assert (12, "external_id") in await refusals(session, other, body, repush_of=sent)


async def test_1_a_repush_is_refused_for_another_candidate_of_the_company(
    session, erp_ready
):
    """The row's candidate is checked, not only its company: an older
    candidate's push of this month is no re-push of the new one."""
    owner, lead, cand, body = await pushable(session)
    old = await candidate(
        session, owner, state="rejected", score=72, rejected_at=NOW - timedelta(days=200)
    )
    sent = await push(
        session,
        old,
        sent_at=NOW - timedelta(days=3),
        phone_tail=tail(61),
        external_id=body["external_id"],
        payload_sha256=push_gate.canonical_sha256(body),
        superseded_at=NOW,
    )
    assert await refusals(session, cand, body, repush_of=sent) == [(12, "external_id")]


# --- 2 --------------------------------------------------------------------------------


async def test_2_pushing_disabled(session, erp_ready, monkeypatch):
    _, _, cand, body = await pushable(session)
    monkeypatch.setattr(settings, "crm_push_enabled", False)
    assert await refusals(session, cand, body) == [(2, "disabled")]


async def test_2_the_owner_s_pause(session, erp_ready):
    _, _, cand, body = await pushable(session)
    session.add(
        RuntimeFlag(key="paused", value={"since": NOW.isoformat()}, updated_by="owner:1")
    )
    await session.flush()
    assert await refusals(session, cand, body) == [(2, "paused")]


async def test_2_the_erp_breaker(session, erp_ready):
    _, _, cand, body = await pushable(session)
    await session.execute(
        sa.update(Source)
        .where(Source.key == erp_feeds.ERP_SOURCE)
        .values(breaker_until=NOW + timedelta(hours=6), breaker_reason="push_404")
    )
    assert await refusals(session, cand, body) == [(2, "breaker")]
    await session.execute(
        sa.update(Source)
        .where(Source.key == erp_feeds.ERP_SOURCE)
        .values(breaker_until=NOW - timedelta(minutes=1))
    )
    assert await refusals(session, cand, body) == []


@pytest.mark.parametrize(
    ("full_h", "delta_h", "salt"),
    [(27, 1, None), (2, 4, None), (2, 1, "0badc0de")],
)
async def test_2_a_stale_list_refuses_every_push(
    session, erp_ready, full_h, delta_h, salt
):
    """07 §1.5 «gate freshness»: the one home's answer (26 h full, 3 h delta,
    the key's salt_id)."""
    _, _, cand, body = await pushable(session)
    cfg = erp_feeds.SuppressionConfig(
        salt_id=salt or erp_feeds.salt_id(settings.crm_agent_key),
        last_full_at=NOW - timedelta(hours=full_h),
        last_delta_at=NOW - timedelta(hours=delta_h),
        count=10,
    )
    await set_config(session, "suppression", cfg.to_json())
    assert await refusals(session, cand, body) == [(2, "stale")]


async def test_2_the_fallback_needs_a_recent_import(session, erp_ready, monkeypatch):
    _, _, cand, body = await pushable(session)
    monkeypatch.setattr(settings, "crm_push_source", "other")
    assert await refusals(session, cand, body) == [(2, "stale")]
    # An import older than the last completed full pull carries nothing (05 §B.3).
    made = {"rows": 10, "tails": 12, "sha256": "0" * 64}
    old = {**made, "at": (NOW - timedelta(hours=3)).isoformat()}
    await set_config(session, "client_import", old)
    assert await refusals(session, cand, body) == [(2, "stale")]
    new = {**made, "at": (NOW - timedelta(hours=1)).isoformat()}
    await set_config(session, "client_import", new)
    assert await refusals(session, cand, body) == []


# --- 3 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"is_competitor": True}, "competitor"),
        ({"name": "Namuna Kargo MChJ"}, "competitor"),
        ({"is_supplier_cn": True}, "supplier_cn"),
        ({"registry_status": "liquidated"}, "liquidated"),
        ({"name": "Namuna hokimligi MChJ"}, "state_body"),
        ({"registry_status": "inactive"}, "inactive"),
        ({"state": "client"}, "client_state"),
    ],
)
async def test_3_the_gates_on_live_data(session, erp_ready, fields, code):
    """A competitor flag or a state that arrived after the approval wins."""
    _, _, cand, body = await pushable(session, **fields)
    assert await refusals(session, cand, body) == [(3, code)]


async def test_3_a_recent_rejection(session, erp_ready):
    owner, _, cand, body = await pushable(session)
    await candidate(
        session, owner, state="rejected", score=50, rejected_at=NOW - timedelta(days=30)
    )
    assert await refusals(session, cand, body) == [(3, "rejected")]


async def test_3_reads_gates_2_to_7_not_gate_1(session, erp_ready):
    """A company the owner approved without B2B evidence: gate 1 judges a
    card, not a push (04 §7 item 3 names gates 2–7)."""
    _, _, cand, body = await pushable(session, legal_form=None)
    assert await refusals(session, cand, body) == []


# --- 4 --------------------------------------------------------------------------------


async def test_4_no_phone(session, erp_ready):
    owner = await firm(session)
    cand = await candidate(session, owner, state="approved", score=72)
    body = {
        "external_id": push_gate.external_id_for(owner.id, NOW),
        "name": owner.display_name,
        "phone": phone(62),
        "note": "NEGA TOPILDI: …",
    }
    assert await refusals(session, cand, body) == [(4, "no_phone")]


@pytest.mark.parametrize(
    ("number", "lead_fields", "code"),
    [
        (None, {"anonymised_at": NOW - timedelta(days=1)}, "anonymised"),
        ("+79260000001", {}, "not_uz"),
        ("+998200000020", {}, "not_usable"),  # code 20: unconfirmed (04 Q4)
        (None, {"shared_phone": True}, "shared_phone"),
    ],
)
async def test_4_the_lead_contact(session, erp_ready, number, lead_fields, code):
    _, _, cand, body = await pushable(session, number=number, lead_fields=lead_fields)
    assert await refusals(session, cand, body) == [(4, code)]


async def test_4_a_shared_phone_the_owner_approved(session, erp_ready):
    _, _, cand, body = await pushable(session, lead_fields={"shared_phone": True})
    session.add(
        LeadReview(
            candidate_id=cand.id,
            kind="owner",
            state="approved",
            answer="push",
            answered_at=NOW - timedelta(minutes=5),
            note=push_gate.SHARED_OK,
        )
    )
    await session.flush()
    assert await refusals(session, cand, body) == []


async def test_4_the_newest_owner_approval_decides_the_shared_phone(session, erp_ready):
    """The owner's newest approval carries the word; an older ``shared_ok``
    is not the answer to the card the owner tapped since."""
    _, _, cand, body = await pushable(session, lead_fields={"shared_phone": True})
    await tapped(session, cand, note=push_gate.SHARED_OK, minutes=60)
    await tapped(session, cand, note=None, minutes=5)
    assert await refusals(session, cand, body) == [(4, "shared_phone")]
    await tapped(session, cand, note=push_gate.SHARED_OK, minutes=1)
    assert await refusals(session, cand, body) == []


@pytest.mark.parametrize("state", ["declined", "pending"])
async def test_4_shared_ok_on_a_row_that_is_no_approval(session, erp_ready, state):
    _, _, cand, body = await pushable(session, lead_fields={"shared_phone": True})
    await tapped(session, cand, note=push_gate.SHARED_OK, state=state)
    assert await refusals(session, cand, body) == [(4, "shared_phone")]


@pytest.mark.parametrize(
    "transient",
    ["crm_push_enabled", "stale", "breaker"],
)
async def test_4_shared_ok_survives_a_transient_refusal(
    session, erp_ready, monkeypatch, transient
):
    """The review's probe: a refusal that keeps the candidate approved is
    written after the owner's word, never over it — or the next claim would
    refuse ``shared_phone`` and send the owner's approval back to a card."""
    _, _, cand, body = await pushable(session, lead_fields={"shared_phone": True})
    approval = await tapped(session, cand, note=push_gate.SHARED_OK)
    if transient == "crm_push_enabled":
        monkeypatch.setattr(settings, "crm_push_enabled", False)
        code = "disabled"
    elif transient == "stale":
        await set_config(session, "suppression", fresh_config(NOW - timedelta(days=3)))
        code = "stale"
    else:
        await session.execute(
            sa.update(Source)
            .where(Source.key == erp_feeds.ERP_SOURCE)
            .values(breaker_until=NOW + timedelta(hours=1), breaker_reason="push_404")
        )
        code = "breaker"
    first = await push_gate.claim(
        session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT
    )
    assert [(r.check, r.code) for r in first.refusals] == [(2, code)]
    await session.refresh(approval)
    assert approval.note == "shared_ok · " + push_gate.REFUSAL_UZ[code]
    assert push_gate.owner_word(approval.note) == push_gate.SHARED_OK
    monkeypatch.setattr(settings, "crm_push_enabled", True)
    await set_config(session, "suppression", fresh_config())
    await session.execute(
        sa.update(Source)
        .where(Source.key == erp_feeds.ERP_SOURCE)
        .values(breaker_until=None, breaker_reason=None)
    )
    second = await push_gate.claim(
        session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT
    )
    assert second.claimed, second.refusals
    assert (await session.get(LeadCandidate, cand.id)).state == "approved"


def test_4_the_owner_s_word_is_kept_within_the_column():
    long = "x" * 600
    assert push_gate.noted(push_gate.SHARED_OK, long) == ("shared_ok · " + long)[:500]
    assert push_gate.noted("eski izoh", "yangi") == "yangi"
    assert push_gate.noted(None, long) == long[:500]
    assert push_gate.owner_word("shared_ok · Ball tushdi") == "shared_ok"
    assert push_gate.owner_word(None) is None


async def test_4_the_usable_phone_is_preferred(session, erp_ready):
    """No contact named on the candidate: the lineage's usable mobile wins
    over a shared one and a landline."""
    owner = await firm(session)
    await contact(session, owner, phone(63), shared_phone=True)
    await contact(session, owner, "+998712000063")
    mobile = await contact(session, owner, phone(64))
    cand = await candidate(session, owner, state="approved", score=72)
    picked = await push_gate.lead_contact(session, cand)
    assert picked is not None and picked.id == mobile.id


async def test_4_a_mobile_beats_an_older_landline(session, erp_ready):
    owner = await firm(session)
    landline = await contact(
        session, owner, "+998712000064", created_at=NOW - timedelta(days=30)
    )
    mobile = await contact(session, owner, phone(64), created_at=NOW)
    cand = await candidate(session, owner, state="approved", score=72)
    picked = await push_gate.lead_contact(session, cand)
    assert picked is not None and picked.id == mobile.id
    # Without a mobile the oldest usable phone is the one.
    await session.delete(mobile)
    await session.flush()
    picked = await push_gate.lead_contact(session, cand)
    assert picked is not None and picked.id == landline.id


async def test_4_among_unusable_phones_a_live_contact_beats_an_anonymised_one(
    session, erp_ready
):
    """Neither is pushable; the refusal names the live one's reason (shared)
    rather than the stripped one's, whatever their age."""
    owner = await firm(session)
    await contact(
        session,
        owner,
        "+998712000065",
        anonymised_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=30),
    )
    shared = await contact(session, owner, phone(65), shared_phone=True, created_at=NOW)
    cand = await candidate(session, owner, state="approved", score=72)
    picked = await push_gate.lead_contact(session, cand)
    assert picked is not None and picked.id == shared.id


# --- 5 --------------------------------------------------------------------------------


async def test_5_a_suppressed_key(session, erp_ready):
    owner, _, cand, body = await pushable(session)
    await row(session, "dnc", "phone_tail", tail(61))
    assert await refusals(session, cand, body) == [(5, "suppressed")]


async def test_5_a_cooldown_names_its_end(session, erp_ready):
    owner, _, cand, body = await pushable(session)
    until = NOW + timedelta(days=30)
    await row(session, "cooldown", "company_id", str(owner.id), expires_at=until)
    found = await push_gate.check(session, cand, now=NOW, payload=body)
    assert [(r.check, r.code, r.until) for r in found] == [(5, "cooldown", until)]


async def test_5_the_feed_s_hash_of_the_lead_tail(session, erp_ready):
    _, _, cand, body = await pushable(session)
    await row(
        session,
        "client",
        "phone_hash",
        erp_feeds.hash_tail(settings.crm_agent_key, tail(61)),
        source="erp_feed",
        salt_id=erp_feeds.salt_id(settings.crm_agent_key),
    )
    assert await refusals(session, cand, body) == [(5, "suppressed")]


# --- 6 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [("sent", "delivered"), ("unknown", "delivered"), ("sending", "in_flight")],
)
async def test_6_the_id_s_own_rows(session, erp_ready, monkeypatch, status, code):
    """The id's own row is item 6's fact alone: the windows (7, 8) and the
    caps (9) never count it, so a cap of one is not reached by it."""
    _, _, cand, body = await pushable(session)
    monkeypatch.setattr(settings, "push_daily_cap", 1)
    await push(
        session,
        cand,
        sent_at=NOW - timedelta(minutes=2),
        phone_tail=tail(61),
        external_id=body["external_id"],
        status=status,
    )
    assert await refusals(session, cand, body) == [(6, code)]


async def test_6_the_next_attempt_waits_and_carries_the_same_body(session, erp_ready):
    _, _, cand, body = await pushable(session)
    sha = push_gate.canonical_sha256(body)
    failed = NOW - timedelta(minutes=1)
    await push(
        session,
        cand,
        sent_at=failed,
        finished_at=failed,
        phone_tail=tail(61),
        external_id=body["external_id"],
        status="failed",
        payload_sha256=sha,
    )
    found = await push_gate.check(session, cand, now=NOW, payload=body)
    assert [(r.check, r.code, r.until) for r in found] == [
        (6, "attempt_wait", failed + push_gate.ATTEMPT_DELAYS[2])
    ]
    # Waiting for the next attempt is the job's own clock: no note.
    approval = await tapped(session, cand)
    await push_gate.claim(session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT)
    await session.refresh(approval)
    assert approval.note is None
    later = NOW + timedelta(minutes=5)
    plan = await push_gate.evaluate(session, cand, now=later, payload=body)
    assert (plan.refusals, plan.attempt) == ([], 2)
    changed = {**body, "note": body["note"] + " (yangi)"}
    assert await refusals(session, cand, changed, now=later) == [(6, "payload_changed")]


async def test_6_three_attempts_then_none(session, erp_ready):
    _, _, cand, body = await pushable(session)
    sha = push_gate.canonical_sha256(body)
    for attempt in (1, 2, 3):
        at = NOW - timedelta(hours=4 - attempt)
        await push(
            session,
            cand,
            sent_at=at,
            finished_at=at,
            attempt=attempt,
            phone_tail=tail(61),
            external_id=body["external_id"],
            status="failed",
            payload_sha256=sha,
        )
    assert await refusals(session, cand, body) == [(6, "attempts_spent")]
    assert push_gate.REFUSAL_UZ["attempts_spent"].startswith("CRM javob bermadi")
    assert push_gate.MAX_ATTEMPTS == 3
    assert dict(push_gate.ATTEMPT_DELAYS) == {
        2: timedelta(minutes=5),
        3: timedelta(minutes=30),
    }


# --- 7, 8 -----------------------------------------------------------------------------


async def test_7_the_lead_tail_pushed_in_24_hours(session, erp_ready):
    _, _, cand, body = await pushable(session)
    other = await firm(session)
    stranger = await candidate(
        session, other, state="pushed", score=72, external_id=f"cs:{other.id}:202610"
    )
    await push(
        session,
        stranger,
        sent_at=NOW - timedelta(hours=23),
        phone_tail=tail(61),
        external_id=f"cs:{other.id}:202610",
    )
    found = await push_gate.check(session, cand, now=NOW, payload=body)
    assert [(r.check, r.code) for r in found] == [(7, "phone_recent")]
    assert found[0].until == NOW + timedelta(hours=1)
    assert (
        await refusals(session, cand, body, now=NOW + timedelta(hours=1, minutes=1)) == []
    )


async def test_8_an_open_push_to_the_company_in_90_days(session, erp_ready):
    owner, _, cand, body = await pushable(session)
    eid = f"cs:{owner.id}:202609"
    old = await candidate(session, owner, state="pushed", score=72, external_id=eid)
    await push(
        session,
        old,
        sent_at=NOW - timedelta(days=30),
        phone_tail=tail(65),
        external_id=eid,
    )
    assert await refusals(session, cand, body) == [(8, "company_recent")]
    # Superseded rows count for nothing.
    await session.execute(
        sa.update(PushLog).where(PushLog.external_id == eid).values(superseded_at=NOW)
    )
    assert await refusals(session, cand, body) == []


# --- 9 --------------------------------------------------------------------------------


async def test_9_the_daily_cap(session, erp_ready, monkeypatch):
    _, _, cand, body = await pushable(session)
    monkeypatch.setattr(settings, "push_daily_cap", 1)
    other = await firm(session)
    stranger = await candidate(
        session, other, state="pushed", score=72, external_id=f"cs:{other.id}:202610"
    )
    await push(
        session,
        stranger,
        sent_at=NOW - timedelta(hours=1),
        phone_tail=tail(66),
        external_id=f"cs:{other.id}:202610",
    )
    assert await refusals(session, cand, body) == [(9, "cap")]
    # 00:30 in Tashkent is still yesterday in UTC: the Tashkent day counts it.
    await session.execute(
        sa.update(PushLog).values(sent_at=datetime(2026, 10, 27, 0, 30, tzinfo=TZ))
    )
    assert await refusals(session, cand, body) == [(9, "cap")]
    # Yesterday's push is not today's (the Tashkent day).
    await session.execute(
        sa.update(PushLog).values(sent_at=datetime(2026, 10, 26, 23, 59, tzinfo=TZ))
    )
    assert await refusals(session, cand, body) == []


async def test_9_the_erp_s_rolling_source_cap(session, erp_ready):
    _, _, cand, body = await pushable(session)
    other = await firm(session)
    stranger = await candidate(session, other, state="pushed", score=72)
    # 200 pushes 22 h ago — yesterday afternoon, inside the rolling 24 h.
    await session.execute(
        sa.text(
            "INSERT INTO push_log (external_id, attempt, candidate_id, company_id,"
            " phone_tail, endpoint, source_key_sent, payload_sha256, status, http_status,"
            " sent_at)"
            " SELECT 'cs' || chr(58) || gen_random_uuid() || :month, 1, :cand, :company,"
            " lpad((910000000 + i)::text, 9, '0'), :endpoint, 'agent', repeat('0', 64),"
            " 'sent', 200, :at FROM generate_series(1, :n) AS i"
        ),
        {
            "cand": stranger.id,
            "company": other.id,
            "endpoint": ENDPOINT,
            "at": NOW - timedelta(hours=22),
            "n": push_gate.SOURCE_CAP,
            "month": ":202610",
        },
    )
    assert await refusals(session, cand, body) == [(9, "source_cap")]
    assert (
        await refusals(session, cand, body, now=NOW + timedelta(hours=2, minutes=1)) == []
    )


async def test_9_the_source_cap_counts_the_door_pushed_to(session, erp_ready):
    """05 §A.2: the ERP caps per source — 200 «Boshqa» arrivals do not fill
    the agent door's cap."""
    _, _, cand, body = await pushable(session)
    other = await firm(session)
    stranger = await candidate(session, other, state="pushed", score=72)
    await session.execute(
        sa.text(
            "INSERT INTO push_log (external_id, attempt, candidate_id, company_id,"
            " phone_tail, endpoint, source_key_sent, payload_sha256, status, http_status,"
            " sent_at)"
            " SELECT 'cs' || chr(58) || gen_random_uuid() || :month, 1, :cand, :company,"
            " lpad((920000000 + i)::text, 9, '0'), :endpoint, 'other', repeat('0', 64),"
            " 'sent', 200, :at FROM generate_series(1, :n) AS i"
        ),
        {
            "cand": stranger.id,
            "company": other.id,
            "endpoint": "/api/leads/in/other",
            "at": NOW - timedelta(hours=22),
            "n": push_gate.SOURCE_CAP,
            "month": ":202610",
        },
    )
    assert await refusals(session, cand, body) == []


async def test_9_and_7_a_push_in_flight_counts(session, erp_ready, monkeypatch):
    """The review's probe: a committed ``sending`` row whose POST has not
    answered is a push for the day's cap and the phone's 24 hours (it may
    land, 02 §2.10 rule 5 (a))."""
    monkeypatch.setattr(settings, "push_daily_cap", 1)
    a = await firm(session)
    la = await contact(session, a, phone(67))
    ca = await candidate(session, a, state="approved", lead=la, score=72)
    b = await firm(session)
    lb = await contact(session, b, phone(67), shared_phone=True)
    cb = await candidate(session, b, state="approved", lead=lb, score=72)
    await tapped(session, cb, note=push_gate.SHARED_OK)

    def body(owner, lead):
        return {
            "external_id": push_gate.external_id_for(owner.id, NOW),
            "name": owner.display_name,
            "phone": lead.phone,
            "note": "NEGA TOPILDI: x",
        }

    first = await push_gate.claim(
        session, ca.id, now=NOW, payload=body(a, la), endpoint=ENDPOINT
    )
    assert first.claimed
    await session.commit()
    found = await push_gate.check(session, cb, now=NOW, payload=body(b, lb))
    assert [(r.check, r.code) for r in found] == [(7, "phone_recent"), (9, "cap")]
    assert found[0].until == NOW + timedelta(hours=24)


# --- 10 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "opens"),
    [
        (
            datetime(2026, 10, 25, 11, 0, tzinfo=TZ),
            datetime(2026, 10, 26, 9, 30, tzinfo=TZ),
        ),
        (
            datetime(2026, 10, 27, 17, 30, tzinfo=TZ),
            datetime(2026, 10, 28, 9, 30, tzinfo=TZ),
        ),
        (
            datetime(2026, 10, 27, 9, 29, tzinfo=TZ),
            datetime(2026, 10, 27, 9, 30, tzinfo=TZ),
        ),
        (
            datetime(2026, 10, 31, 18, 0, tzinfo=TZ),
            datetime(2026, 11, 2, 9, 30, tzinfo=TZ),
        ),
    ],
)
async def test_10_outside_the_window(session, erp_ready, now, opens):
    _, _, cand, body = await pushable(session)
    body["external_id"] = push_gate.external_id_for(cand.company_id, now)
    await set_config(session, "suppression", fresh_config(now))
    found = await push_gate.check(session, cand, now=now, payload=body)
    assert [(r.check, r.code, r.until) for r in found] == [(10, "window", opens)]


@pytest.mark.parametrize(
    ("now", "opens"),
    [
        (datetime(2026, 10, 27, 9, 30, tzinfo=TZ), None),
        (datetime(2026, 10, 31, 17, 29, tzinfo=TZ), None),  # Saturday
        # 12:29 UTC is 17:29 in Tashkent, still open; 12:30 UTC closes it.
        (datetime(2026, 10, 27, 12, 29, tzinfo=UTC), None),
        (
            datetime(2026, 10, 27, 12, 30, tzinfo=UTC),
            datetime(2026, 10, 28, 9, 30, tzinfo=TZ),
        ),
    ],
)
def test_the_window_is_tashkent_time(now, opens):
    assert push_gate.window_opens(now) == opens


# --- 11 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        ({"name": "Rustam (direktor) — Namuna"}, "name"),
        ({"phone": "+998900000099"}, "phone"),
        ({"phone": "900000061"}, "phone"),
        ({"note": "x" * 701}, "note"),
        ({"note": "😀" * 351}, "note"),  # 702 UTF-16 units
        ({"note": ""}, "note"),
        ({"note": None}, "missing:note"),
        ({"kub": "2"}, "kub"),  # no stated volume
        ({"note": "Qoʻngʻiroq: <PHONE_1>"}, "pseudonym:note"),
        ({"note": "Pasport: <ID>"}, "pseudonym:note"),
        ({"firma": "Namuna\x00 MChJ"}, "ill_formed:firma"),
        ({"evidence": "bad \ud83d half"}, "ill_formed:evidence"),
        ({"score": 72}, "type:score"),
    ],
)
async def test_11_the_payload(session, erp_ready, change, detail):
    _, _, cand, body = await pushable(session)
    found = await push_gate.check(session, cand, now=NOW, payload={**body, **change})
    assert [(r.check, r.code, r.detail) for r in found] == [(11, "payload", detail)]


async def test_11_a_note_of_700_units_and_the_stated_kub_pass(session, erp_ready):
    owner, _, cand, body = await pushable(session)
    await signal(session, owner, "vol_stated_1_3", value=Decimal("2.000"))
    good = {**body, "note": "😀" * 350, "kub": "2"}
    assert await refusals(session, cand, good) == []
    assert await refusals(session, cand, {**good, "kub": "3"}) == [(11, "payload")]


async def test_11_kub_is_a_stated_volume_never_another_signal_s_value(session, erp_ready):
    """14 ads in a category are a velocity, not 14 kub (05 §A.2: a mapped
    ``kub`` becomes the lead's quoted volume)."""
    owner, _, cand, body = await pushable(session)
    await signal(session, owner, "vol_marketplace_velocity", value=Decimal("14"))
    assert await refusals(session, cand, {**body, "kub": "14"}) == [(11, "payload")]


async def test_11_no_payload(session, erp_ready):
    _, _, cand, _ = await pushable(session)
    assert await refusals(session, cand, None) == [(11, "no_payload")]


# --- 12 -------------------------------------------------------------------------------


async def test_12_the_external_id(session, erp_ready):
    owner, _, cand, body = await pushable(session)
    for wrong in (f"cs:{owner.id}:202609", f"cs:{uuid.uuid4()}:202610"):
        assert await refusals(session, cand, {**body, "external_id": wrong}) == [
            (12, "external_id")
        ]


def test_12_the_month_is_tashkent_s_at_claim_time():
    company = uuid.UUID(int=5)
    late = datetime(2026, 10, 31, 23, 30, tzinfo=UTC)  # 04:30 on 1 November in Tashkent
    assert push_gate.external_id_for(company, late) == f"cs:{company}:202611"
    assert push_gate.external_id_for(
        company, datetime(2026, 10, 31, 18, 59, tzinfo=UTC)
    ) == (f"cs:{company}:202610")


async def test_12_a_merged_company_is_no_root(session, erp_ready):
    owner, _, cand, body = await pushable(session)
    root = await firm(session)
    await session.execute(
        sa.update(Company)
        .where(Company.id == owner.id)
        .values(state="merged", merged_into=root.id)
    )
    assert (12, "not_root") in await refusals(session, cand, body)


# --- 13 -------------------------------------------------------------------------------


async def test_13_the_score_at_push_time(session, erp_ready):
    _, _, cand, body = await pushable(session)
    cand.score = 59
    assert await refusals(session, cand, body) == [(13, "score_low")]
    cand.score = 60
    assert await refusals(session, cand, body) == []


# --- 14: the claim and what a refusal writes ----------------------------------------


async def test_a_claim_writes_one_sending_row(session, erp_ready):
    owner, _, cand, body = await pushable(session)
    result = await push_gate.claim(
        session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT
    )
    assert result.claimed and (result.external_id, result.attempt) == (
        body["external_id"],
        1,
    )
    log = await session.get(PushLog, result.push_log_id)
    assert log is not None
    assert (
        log.status,
        log.attempt,
        log.candidate_id,
        log.company_id,
        log.phone_tail,
        log.endpoint,
        log.source_key_sent,
        log.payload,
        log.payload_sha256,
        log.sent_at,
    ) == (
        "sending",
        1,
        cand.id,
        owner.id,
        tail(61),
        ENDPOINT,
        "agent",
        body,
        push_gate.canonical_sha256(body),
        NOW,
    )
    await session.commit()
    again = await push_gate.claim(
        session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT
    )
    assert [(r.check, r.code) for r in again.refusals] == [(6, "in_flight")]
    assert await session.scalar(sa.select(sa.func.count(PushLog.id))) == 1


async def test_two_workers_claim_one_candidate_once(session, erp_ready):
    """07 §1.8: two sessions, ``asyncio.gather``; the FOR UPDATE waiter is
    seen in ``pg_stat_activity`` as a Lock wait before the first commits."""
    _, _, cand, body = await pushable(session)
    await session.commit()
    first_claimed = asyncio.Event()
    release = asyncio.Event()

    async def worker(hold: bool) -> push_gate.ClaimResult:
        async with SessionLocal() as s, s.begin():
            result = await push_gate.claim(
                s, cand.id, now=NOW, payload=body, endpoint=ENDPOINT
            )
            if hold:
                first_claimed.set()
                await release.wait()
            return result

    a = asyncio.create_task(worker(True))
    await asyncio.wait_for(first_claimed.wait(), 10)
    b = asyncio.create_task(worker(False))
    for _ in range(200):
        waiting = await session.scalar(
            sa.text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND wait_event_type = 'Lock'"
            )
        )
        await session.commit()
        if waiting:
            break
        await asyncio.sleep(0.05)
    assert waiting == 1  # B waits on A's lock
    release.set()
    one, two = await asyncio.wait_for(asyncio.gather(a, b), 15)
    assert one.claimed and not two.claimed
    assert [(r.check, r.code) for r in two.refusals] == [(6, "in_flight")]
    assert await session.scalar(sa.select(sa.func.count(PushLog.id))) == 1


async def test_14_a_refusal_reaches_the_note_and_the_candidate_stays(
    session, erp_ready, monkeypatch
):
    _, _, cand, body = await pushable(session)
    await tapped(session, cand)
    monkeypatch.setattr(settings, "crm_push_enabled", False)
    result = await push_gate.claim(
        session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT
    )
    assert not result.claimed
    reviews = (await session.scalars(sa.select(LeadReview))).all()
    assert [(r.kind, r.state, r.answer, r.note) for r in reviews] == [
        ("owner", "approved", "push", push_gate.REFUSAL_UZ["disabled"])
    ]
    assert (await session.get(LeadCandidate, cand.id)).state == "approved"
    assert await session.scalar(sa.select(sa.func.count(PushLog.id))) == 0


async def test_14_an_auto_approved_candidate_gets_no_invented_owner_answer(
    session, erp_ready, monkeypatch
):
    """No card was ever tapped: a row would read as the owner's decision in
    /lidlar and the reports. The refusal is the log's, the candidate stays."""
    _, _, cand, body = await pushable(session)
    cand.approved_by = "auto:olx_business"
    await session.flush()
    monkeypatch.setattr(settings, "crm_push_enabled", False)
    result = await push_gate.claim(
        session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT
    )
    assert [(r.check, r.code) for r in result.refusals] == [(2, "disabled")]
    assert await session.scalar(sa.select(sa.func.count(LeadReview.id))) == 0
    assert (await session.get(LeadCandidate, cand.id)).state == "approved"


async def test_14_the_owner_s_approval_row_carries_the_note(session, erp_ready):
    owner, _, cand, body = await pushable(session)
    tapped = LeadReview(
        candidate_id=cand.id,
        kind="owner",
        state="approved",
        answer="push",
        answered_at=NOW - timedelta(minutes=5),
    )
    session.add(tapped)
    await row(
        session,
        "cooldown",
        "company_id",
        str(owner.id),
        expires_at=datetime(2027, 4, 25, 12, tzinfo=TZ),
    )
    await push_gate.claim(session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT)
    await session.refresh(tapped)
    assert tapped.note == "Kutish muddati tugamagan (25.04.2027 12:00 gacha)"
    assert uz_problems(tapped.note, rendered=True) == set()


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 25, 11, 0, tzinfo=TZ),
        NOW,
    ],  # Sunday: the window; Tuesday: the cap
)
async def test_14_cap_and_window_are_silent(session, erp_ready, monkeypatch, now):
    _, _, cand, body = await pushable(session)
    monkeypatch.setattr(settings, "push_daily_cap", 0)
    body["external_id"] = push_gate.external_id_for(cand.company_id, now)
    await set_config(session, "suppression", fresh_config(now))
    result = await push_gate.claim(
        session, cand.id, now=now, payload=body, endpoint=ENDPOINT
    )
    assert result.refusals and all(r.silent for r in result.refusals)
    assert await session.scalar(sa.select(sa.func.count(LeadReview.id))) == 0


async def test_14_a_fallen_score_goes_back_to_the_owner(session, erp_ready):
    """04 §7 item 13: «Ball tushdi» on the new card."""
    _, _, cand, body = await pushable(session)
    cand.score = 55
    await session.flush()
    await push_gate.claim(session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT)
    got = await session.get(LeadCandidate, cand.id, populate_existing=True)
    live = (await session.scalars(sa.select(LeadReview))).all()
    assert got is not None and got.state == "review"
    assert [(r.state, r.note) for r in live] == [("pending", "Ball tushdi")]


@pytest.mark.parametrize(
    ("fields", "lead_fields", "code"),
    [
        ({"is_competitor": True}, {}, "competitor"),
        ({"is_supplier_cn": True}, {}, "supplier_cn"),
        ({"state": "client"}, {}, "client_state"),
        ({}, {"shared_phone": True}, "shared_phone"),
    ],
)
async def test_14_what_only_the_owner_can_resolve_goes_back_to_review(
    session, erp_ready, fields, lead_fields, code
):
    """A fact that arrived after the approval and that no rerun of the job
    can clear: the candidate leaves ``approved`` for a new card that says
    why, instead of being refused again every run."""
    _, _, cand, body = await pushable(session, lead_fields=lead_fields, **fields)
    result = await push_gate.claim(
        session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT
    )
    assert not result.claimed
    got = await session.get(LeadCandidate, cand.id, populate_existing=True)
    live = (await session.scalars(sa.select(LeadReview))).all()
    assert got is not None and got.state == "review"
    assert [(r.state, r.note) for r in live] == [("pending", push_gate.REFUSAL_UZ[code])]


async def test_14_a_dnc_after_the_approval_suppresses(session, erp_ready):
    owner, _, cand, body = await pushable(session)
    await tapped(session, cand)
    dnc = await row(session, "dnc", "phone_tail", tail(61))
    await push_gate.claim(session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT)
    got = await session.get(LeadCandidate, cand.id, populate_existing=True)
    assert got is not None and (got.state, got.suppressed_by) == ("suppressed", dnc.id)
    [note] = (await session.scalars(sa.select(LeadReview.note))).all()
    assert note == "🚫 Bu raqam endi bostirilgan"


async def test_14_a_client_hit_at_push_time_makes_the_company_a_client(
    session, erp_ready
):
    """Gate 4 runs inside the push transaction too (04 §6): a client hit
    suppresses the candidate and writes ``companies.state='client'``."""
    owner, _, cand, body = await pushable(session)
    client = await row(session, "client", "phone_tail", tail(61), source="outcomes")
    await push_gate.claim(session, cand.id, now=NOW, payload=body, endpoint=ENDPOINT)
    got = await session.get(LeadCandidate, cand.id, populate_existing=True)
    state = await session.scalar(
        sa.select(Company.state)
        .where(Company.id == owner.id)
        .execution_options(populate_existing=True)
    )
    assert got is not None and (got.state, got.suppressed_by) == ("suppressed", client.id)
    assert state == "client"


def test_every_refusal_word_is_owner_text():
    for code, text in push_gate.REFUSAL_UZ.items():
        assert uz_problems(text, rendered=True) == set(), code
    assert set(push_gate.REFUSAL_UZ) >= push_gate.SILENT
    assert set(push_gate.REFUSAL_UZ) >= push_gate.TO_REVIEW


# --- the pure helpers -----------------------------------------------------------------


def test_the_canonical_body_hash():
    """Rule 6: sorted keys, no spaces, a None dropped, UTF-8 as written."""
    a = {"note": "Qoʻl", "kub": None, "name": "Namuna"}
    b = {"name": "Namuna", "note": "Qoʻl"}
    pinned = "406e65976a16ff5d1b5fdf0dc866f584b8e511bacf4d1291f6de9e044f7a8600"
    assert push_gate.canonical_sha256(a) == push_gate.canonical_sha256(b) == pinned
    assert push_gate.canonical_sha256({"a": "1"}) == (
        "9afeb0f2b203f254312ec8ded441d0318b7c34c57f8695ede42d2215a30c0960"
    )


def test_text_len_counts_utf16_units():
    assert push_gate.text_len("Qoʻl") == 4
    assert push_gate.text_len("😀") == 2
    assert push_gate.text_len("") == 0
