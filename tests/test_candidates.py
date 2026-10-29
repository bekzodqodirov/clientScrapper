"""The candidate state machine (02 §2.9) and score_job (04 §0, §5).

Every row of 02 §2.9's table is driven through ``candidates.transition`` on a
real row (so the CHECKs and ``trg_lead_candidates_suppressed`` agree with the
Python table), every step the table lacks is refused, and the two named
paths are walked end to end: the phone-less Telegram candidate's
``enriching → review`` and a manager's ``/tel`` ``hold → scored →
approved(manager:<tg_id>)``. score_job runs over observations the resolver
stored, through the hook this module registers. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from jsonschema import Draft202012Validator

from clientscrapper.collectors.base import Observation as Seen
from clientscrapper.config import settings
from clientscrapper.db import enums as e
from clientscrapper.db.models import (
    Company,
    Contact,
    Evidence,
    LeadCandidate,
    Observation,
    Outcome,
    Signal,
    Source,
    Suppression,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import candidates, companies, scoring, signals
from clientscrapper.services.candidates import (
    IllegalTransition,
    LastCandidate,
    ReopenFacts,
    RouteFacts,
)
from clientscrapper.services.normalise import to_observation_json

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 20, 7, 53, tzinfo=TZ)
POSTED = NOW - timedelta(minutes=12)
_SEQ = itertools.count(1)
SCHEMA = Draft202012Validator(
    json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "clientscrapper"
            / "schemas"
            / "score_breakdown.json"
        ).read_text(encoding="utf-8")
    )
)

# 02 §2.9's table, written out from the spec.
SPEC = {
    "found": {"enriching", "scored", "excluded", "suppressed"},
    "enriching": {"scored", "review", "hold", "excluded", "suppressed"},
    "scored": {"review", "enriching", "hold", "approved", "excluded", "suppressed"},
    "review": {"approved", "rejected", "hold", "suppressed", "excluded"},
    "approved": {"pushed", "review", "suppressed"},
    "hold": {"scored", "excluded", "suppressed"},
    "pushed": set(),
    "rejected": set(),
    "excluded": set(),
    "suppressed": set(),
}
ALLOWED = sorted((a, b) for a, tos in SPEC.items() for b in tos)
# The marks score_job writes for make_firm's firm (04 §6, services/gates).
PASSING_GATES = {
    "is_b2b": "pass:legal_form",
    "not_competitor": "pass",
    "not_supplier": "pass",
    "not_client": "pass",
    "not_dnc": "pass",
    "not_recent": "pass",
    "active_entity": "unknown",
}
REFUSED = sorted(
    (a, b) for a in e.CANDIDATE_STATES for b in e.CANDIDATE_STATES if b not in SPEC[a]
)


# --- builders ------------------------------------------------------------------------


async def make_firm(session, *, phone: str | None = "+998900000011", **fields) -> Company:
    """A firm that passes the gates (04 §6): a legal form makes it B2B (gate
    1), and nothing else about it fails one — the routing tests here are
    about the score, test_gates.py is about the gates."""
    n = next(_SEQ)
    firm = Company(
        display_name=f"Namuna Firma {n}",
        name_key=f"namuna firma {n}",
        state=fields.pop("state", "active"),
        legal_form=fields.pop("legal_form", "MChJ"),
        **fields,
    )
    session.add(firm)
    await session.flush()
    if phone is not None:
        session.add(
            Contact(company_id=firm.id, tier="company", phone=phone, phone_kind="mobile")
        )
        await session.flush()
    return firm


async def dnc_row(session) -> Suppression:
    row = Suppression(
        kind="dnc",
        key_type="phone_tail",
        key=f"9000000{next(_SEQ) % 100:02d}",
        source="owner_bot",
    )
    session.add(row)
    await session.flush()
    return row


async def candidate_in(session, state: str, firm: Company | None = None, **fields):
    firm = firm or await make_firm(session)
    values: dict[str, Any] = {
        "company_id": firm.id,
        "state": state,
        "score": 65,
        "tier": "hot",
        "primary_source_key": "telegram_monitor",
    }
    if state in ("approved", "pushed"):
        values.update(approved_by="owner:1001", approved_at=NOW)
    if state == "pushed":
        values.update(external_id=f"cs:{firm.id}:202610", pushed_at=NOW)
    if state == "excluded":
        values["exclude_reason"] = "owner"
    if state == "suppressed":
        values["suppressed_by"] = (await dnc_row(session)).id
    if state == "rejected":
        values["rejected_at"] = NOW
    values.update(fields)
    row = LeadCandidate(**values)
    session.add(row)
    await session.flush()
    return row


async def reload(session, row: LeadCandidate) -> LeadCandidate:
    fresh = await session.scalar(
        sa.select(LeadCandidate)
        .where(LeadCandidate.id == row.id)
        .execution_options(populate_existing=True)
    )
    assert fresh is not None
    return fresh


async def target_args(session, cand: LeadCandidate, target: str) -> dict[str, Any]:
    if target == "excluded":
        return {"exclude_reason": "b2c"}
    if target == "suppressed":
        return {"suppressed_by": (await dnc_row(session)).id}
    if target == "approved":
        return {"approved_by": "owner:1001"}
    if target == "pushed":
        return {"external_id": f"cs:{cand.company_id}:202610"}
    if target == "hold":
        return {"hold_until": NOW + timedelta(days=7)}
    return {}


# --- the table ------------------------------------------------------------------------


def test_transitions_are_02_section_2_9_s_table():
    assert {k: set(v) for k, v in candidates.TRANSITIONS.items()} == SPEC
    assert set(candidates.TRANSITIONS) == set(e.CANDIDATE_STATES)
    terminal = set(candidates.TERMINAL_STATES)
    assert terminal == {"pushed", "rejected", "excluded", "suppressed"}


@pytest.mark.parametrize(("source", "target"), ALLOWED)
async def test_every_step_of_the_table_moves_a_real_row(session, source, target):
    cand = await candidate_in(session, source)
    await candidates.transition(
        session, cand, target, now=NOW, **await target_args(session, cand, target)
    )
    row = await reload(session, cand)
    assert row.state == target
    if target == "approved":
        assert (row.approved_by, row.approved_at) == ("owner:1001", NOW)
    elif target == "pushed":
        assert (row.external_id, row.pushed_at) == (f"cs:{cand.company_id}:202610", NOW)
    elif target == "rejected":
        assert row.rejected_at == NOW
    elif target == "excluded":
        assert row.exclude_reason == "b2c"
    elif target == "suppressed":
        assert row.suppressed_by is not None
    elif target == "hold":
        assert row.hold_until == NOW + timedelta(days=7)
    elif target == "enriching":
        assert row.next_enrich_at == NOW + timedelta(hours=1)
    if source == "hold":
        assert row.hold_until is None


@pytest.mark.parametrize(("source", "target"), REFUSED)
def test_every_step_the_table_lacks_is_refused(source, target):
    with pytest.raises(IllegalTransition):
        candidates.check_transition(source, target)


@pytest.mark.parametrize("terminal", ["pushed", "rejected", "excluded", "suppressed"])
async def test_a_terminal_row_stays_where_it_is(session, terminal):
    cand = await candidate_in(session, terminal)
    for target in ("found", "scored", "review", "approved"):
        with pytest.raises(IllegalTransition):
            await candidates.transition(
                session, cand, target, now=NOW, approved_by="owner:1"
            )
    assert (await reload(session, cand)).state == terminal


async def test_a_found_row_cannot_jump_to_review_or_approved(session):
    cand = await candidate_in(session, "found")
    for target in ("review", "approved", "hold", "pushed"):
        with pytest.raises(IllegalTransition):
            await candidates.transition(
                session, cand, target, now=NOW, approved_by="owner:1"
            )
    assert (await reload(session, cand)).state == "found"


# --- what a target state means --------------------------------------------------------


async def test_an_exclusion_needs_its_reason(session):
    cand = await candidate_in(session, "review")
    for reason in (None, "not_a_reason"):
        with pytest.raises(IllegalTransition, match="exclude_reason"):
            await candidates.transition(
                session, cand, "excluded", now=NOW, exclude_reason=reason
            )


async def test_a_suppression_needs_its_row(session):
    cand = await candidate_in(session, "review")
    with pytest.raises(IllegalTransition, match="suppressed_by"):
        await candidates.transition(session, cand, "suppressed", now=NOW)


@pytest.mark.parametrize(
    "who",
    [None, "owner:abc", "auto:", "boss:1", "manager:", "owner:1 ", "auto:Telegram"],
)
async def test_an_approval_names_exactly_who(session, who):
    cand = await candidate_in(session, "review")
    with pytest.raises(IllegalTransition, match="approved_by"):
        await candidates.transition(session, cand, "approved", now=NOW, approved_by=who)


@pytest.mark.parametrize("who", ["owner:1001", "manager:5001", "auto:olx_business"])
async def test_the_three_approvals(session, who):
    cand = await candidate_in(session, "review")
    await candidates.transition(session, cand, "approved", now=NOW, approved_by=who)
    assert (await reload(session, cand)).approved_by == who


async def test_no_approval_without_a_usable_phone(session):
    shared = await make_firm(session, phone=None)
    session.add(
        Contact(
            company_id=shared.id,
            tier="company",
            phone="+998900000012",
            phone_kind="mobile",
            shared_phone=True,
        )
    )
    landline = await make_firm(session, phone=None)
    session.add(
        Contact(
            company_id=landline.id,
            tier="company",
            phone="+998712000012",
            phone_kind="landline",
        )
    )
    await session.flush()
    for firm in (shared, await make_firm(session, phone=None)):
        cand = await candidate_in(session, "review", firm)
        with pytest.raises(IllegalTransition, match="no usable phone"):
            await candidates.transition(
                session, cand, "approved", now=NOW, approved_by="owner:1"
            )
    ok = await candidate_in(session, "review", landline)
    await candidates.transition(session, ok, "approved", now=NOW, approved_by="owner:1")
    assert (await reload(session, ok)).state == "approved"


async def test_scored_review_and_approved_need_a_score(session):
    cand = await candidate_in(session, "found", score=None, tier=None)
    with pytest.raises(IllegalTransition, match="not scored"):
        await candidates.transition(session, cand, "scored", now=NOW)


async def test_a_push_needs_its_external_id(session):
    cand = await candidate_in(session, "approved")
    with pytest.raises(IllegalTransition, match="external_id"):
        await candidates.transition(session, cand, "pushed", now=NOW)


# --- the phone-less Telegram candidate (02 §2.9, 04 §5.2 F-12) ------------------------


async def phoneless(session, **fields) -> LeadCandidate:
    firm = await make_firm(session, phone=None)
    session.add(
        Contact(
            company_id=firm.id,
            tier="person",
            telegram_username="sardor_optom_tex",
            legal_basis="public_source",
            source_url="https://www.olx.uz/namuna",
            collected_at=NOW,
        )
    )
    await session.flush()
    values: dict[str, Any] = {
        "enrich_attempts": 3,
        "score": 65,
        # not yet 'warm': phone_less_review sets the tier itself (02 §2.9)
        "tier": None,
        "score_breakdown": {"total": 65, "gates": {"is_b2b": "pass"}, "enrichment": [1]},
    }
    values.update(fields)
    return await candidate_in(session, "enriching", firm, **values)


async def test_enriching_to_review_for_the_phone_less_telegram_candidate(session):
    cand = await phoneless(session)
    await candidates.phone_less_review(session, cand, now=NOW)
    row = await reload(session, cand)
    assert (row.state, row.tier) == ("review", "warm")
    assert row.score_breakdown["gates"] == {"is_b2b": "pass", "no_phone": True}
    assert row.score_breakdown["enrichment"] == [1]


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"enrich_attempts": 2}, "attempts left"),
        ({"score": 59}, "score below 60"),
        ({"score": None}, "score below 60"),
    ],
)
async def test_the_phone_less_review_needs_three_attempts_and_sixty(session, fields, why):
    cand = await phoneless(session, **fields)
    with pytest.raises(IllegalTransition, match=why):
        await candidates.phone_less_review(session, cand, now=NOW)
    assert (await reload(session, cand)).state == "enriching"


async def test_the_phone_less_review_needs_no_phone_and_a_username(session):
    with_phone = await phoneless(session)
    session.add(
        Contact(
            company_id=with_phone.company_id,
            tier="company",
            phone="+998900000013",
            phone_kind="mobile",
        )
    )
    await session.flush()
    with pytest.raises(IllegalTransition, match="has a phone"):
        await candidates.phone_less_review(session, with_phone, now=NOW)
    silent = await candidate_in(
        session, "enriching", await make_firm(session, phone=None), enrich_attempts=3
    )
    with pytest.raises(IllegalTransition, match="no telegram username"):
        await candidates.phone_less_review(session, silent, now=NOW)
    held = await phoneless(session)
    held.state = "hold"
    await session.flush()
    with pytest.raises(IllegalTransition, match="not enriching"):
        await candidates.phone_less_review(session, held, now=NOW)


# --- a manager's /tel (06 §1.7) -------------------------------------------------------


async def _evidence(session, source: str = "telegram_monitor") -> Evidence:
    n = next(_SEQ)
    row = Evidence(
        source_key=source,
        url=f"https://namuna.example.uz/e/{n}",
        fetched_at=NOW,
        sha256=hashlib.sha256(str(n).encode()).hexdigest(),
        bytes=0,
        excerpt="",
        expires_at=NOW + timedelta(days=180),
    )
    session.add(row)
    await session.flush()
    return row


async def _signal(
    session, firm: Company, key: str, *, source: str = "olx_business"
) -> None:
    evidence = await _evidence(session, source)
    session.add(
        Signal(
            company_id=firm.id,
            evidence_id=evidence.id,
            key=key,
            observed_at=NOW - timedelta(days=3),
            source_key=source,
        )
    )
    await session.flush()


async def test_hold_to_scored_to_approved_by_the_manager_s_phone(session):
    """04 §9.2's seller after the owner's handoff: a manager's /tel phone
    re-scores it at once and approves it as the manager."""
    cand = await phoneless(session)
    await candidates.phone_less_review(session, cand, now=NOW)
    await candidates.transition(
        session, cand, "hold", now=NOW, hold_until=NOW + timedelta(days=30)
    )
    firm = await session.get(Company, cand.company_id)
    for key in ("vol_wholesale_bulky", "vol_marketplace_velocity", "cn_sector_prior"):
        await _signal(session, firm, key)
    session.add(
        Contact(
            company_id=firm.id,
            tier="company",
            phone="+998900000014",
            phone_kind="mobile",
        )
    )
    await session.flush()
    later = NOW + timedelta(days=2)
    result = await candidates.manager_phone_rescore(session, cand, tg_id=5001, now=later)
    row = await reload(session, cand)
    assert (row.state, row.approved_by, row.approved_at) == (
        "approved",
        "manager:5001",
        later,
    )
    # volume 40 + origin 10 + recency 10 + contact (mobile 6 + dm 2)
    assert (result.total, row.score, row.tier) == (68, 68, "hot")
    assert row.hold_until is None
    # A usable phone ends the phone-less handoff's mark; the approval itself
    # records the manager (approved_by), 06 §1.7.
    assert row.score_breakdown["gates"] == {"is_b2b": "pass"}
    SCHEMA.validate(row.score_breakdown)


async def test_a_manager_phone_that_is_not_usable_leaves_the_hold(session):
    cand = await phoneless(session)
    cand.state = "hold"
    session.add(
        Contact(
            company_id=cand.company_id,
            tier="company",
            phone="+998200000020",  # an unconfirmed code reads «unknown» (04 Q4)
            phone_kind="unknown",
        )
    )
    await session.flush()
    result = await candidates.manager_phone_rescore(session, cand, tg_id=5001, now=NOW)
    row = await reload(session, cand)
    assert (row.state, row.score, row.last_scored_at) == ("hold", result.total, NOW)
    assert result.usable_phone is False


async def test_a_manager_phone_outside_hold_is_refused(session):
    cand = await candidate_in(session, "review")
    with pytest.raises(IllegalTransition, match="not in hold"):
        await candidates.manager_phone_rescore(session, cand, tg_id=5001, now=NOW)


# --- routing (pure) -------------------------------------------------------------------

NO_FACTS = RouteFacts()


@pytest.mark.parametrize(
    ("state", "tier", "facts", "walk"),
    [
        ("found", "hot", NO_FACTS, ("scored", "review")),
        ("found", "hot", RouteFacts(auto_push=True), ("scored", "approved")),
        ("found", "warm", NO_FACTS, ("scored", "enriching")),
        ("found", "cold", NO_FACTS, ("scored", "hold")),
        ("scored", "hot", NO_FACTS, ("review",)),
        ("scored", "warm", NO_FACTS, ("enriching",)),
        ("scored", "cold", NO_FACTS, ("hold",)),
        ("enriching", "hot", NO_FACTS, ("scored", "review")),
        ("enriching", "warm", NO_FACTS, ()),
        ("enriching", "cold", NO_FACTS, ("scored", "hold")),
        ("hold", "hot", NO_FACTS, ("scored", "review")),
        ("hold", "hot", RouteFacts(hold_until=NOW + timedelta(seconds=1)), ()),
        ("hold", "hot", RouteFacts(hold_until=NOW), ("scored", "review")),
        ("hold", "warm", NO_FACTS, ("scored", "enriching")),
        ("hold", "warm", RouteFacts(enrich_attempts=3), ()),
        ("hold", "cold", NO_FACTS, ()),
        ("review", "hot", NO_FACTS, ()),
        ("review", "cold", NO_FACTS, ()),
        # a review row that lost its phone and is not the handoff steps back
        ("review", "warm", RouteFacts(usable_phone=False), ("hold",)),
        ("review", "warm", RouteFacts(usable_phone=False, no_phone=True), ()),
    ],
)
def test_route(state, tier, facts, walk):
    assert candidates.route(state, tier, facts, now=NOW) == walk
    for a, b in itertools.pairwise((state, *walk)):
        candidates.check_transition(a, b)


# --- re-opening a company (02 §2.9) ---------------------------------------------------

LONG_AGO = NOW - timedelta(days=200)


@pytest.mark.parametrize(
    ("last", "facts", "ok"),
    [
        # after pushed: terminal outcome, a newer signal, another month
        (LastCandidate("pushed", pushed_at=LONG_AGO), ReopenFacts(NOW, True), True),
        (LastCandidate("pushed", pushed_at=LONG_AGO), ReopenFacts(NOW, False), False),
        (LastCandidate("pushed", pushed_at=LONG_AGO), ReopenFacts(NOW, None), True),
        (
            LastCandidate("pushed", pushed_at=NOW - timedelta(days=89)),
            ReopenFacts(NOW, None),
            False,
        ),
        (LastCandidate("pushed", pushed_at=LONG_AGO), ReopenFacts(LONG_AGO, True), False),
        # exactly 90 days is enough (≥)
        (
            LastCandidate("pushed", pushed_at=NOW - timedelta(days=90)),
            ReopenFacts(NOW, None),
            True,
        ),
        # 2026-09-30 19:30 UTC is 1 October in Tashkent: the month of NOW
        (
            LastCandidate(
                "pushed", pushed_at=datetime(2026, 9, 30, 19, 30, tzinfo=ZoneInfo("UTC"))
            ),
            ReopenFacts(NOW, True),
            False,
        ),
        (
            LastCandidate(
                "pushed", pushed_at=datetime(2026, 9, 30, 18, 30, tzinfo=ZoneInfo("UTC"))
            ),
            ReopenFacts(NOW, True),
            True,  # 23:30 on 30 September in Tashkent
        ),
        (
            LastCandidate("pushed", pushed_at=NOW - timedelta(days=5)),
            ReopenFacts(NOW, True),
            False,  # the same month: the external id would repeat
        ),
        # after rejected: 180 days and a newer signal
        (LastCandidate("rejected", rejected_at=LONG_AGO), ReopenFacts(NOW), True),
        (
            LastCandidate("rejected", rejected_at=NOW - timedelta(days=179)),
            ReopenFacts(NOW),
            False,
        ),
        (LastCandidate("rejected", rejected_at=LONG_AGO), ReopenFacts(LONG_AGO), False),
        (
            LastCandidate("rejected", rejected_at=NOW - timedelta(days=180)),
            ReopenFacts(NOW),
            True,  # exactly 180 days (≥)
        ),
        # after suppressed: only when the row is gone
        (LastCandidate("suppressed", suppressed_by=7), ReopenFacts(NOW), False),
        (LastCandidate("suppressed", suppressed_by=None), ReopenFacts(NOW), True),
        # after excluded: the flag behind it, or a newer signal
        (
            LastCandidate("excluded", exclude_reason="competitor"),
            ReopenFacts(NOW, is_competitor=True),
            False,
        ),
        (LastCandidate("excluded", exclude_reason="competitor"), ReopenFacts(NOW), True),
        (
            LastCandidate("excluded", exclude_reason="supplier_cn"),
            ReopenFacts(NOW, is_supplier_cn=True),
            False,
        ),
        (
            LastCandidate("excluded", exclude_reason="liquidated"),
            ReopenFacts(NOW, liquidated=True),
            False,
        ),
        (
            LastCandidate("excluded", exclude_reason="b2c", updated_at=LONG_AGO),
            ReopenFacts(NOW),
            True,
        ),
        (
            LastCandidate("excluded", exclude_reason="b2c", updated_at=NOW),
            ReopenFacts(LONG_AGO),
            False,
        ),
        (LastCandidate("review"), ReopenFacts(NOW), False),
    ],
)
def test_may_reopen(last, facts, ok):
    assert candidates.may_reopen(last, facts, now=NOW) is ok


async def test_ensure_found_makes_one_open_candidate(session):
    firm = await make_firm(session)
    first = await candidates.ensure_found(
        session, firm, source_key="olx_business", contact_id=None, now=NOW
    )
    again = await candidates.ensure_found(
        session, firm, source_key="hh_vacancies", contact_id=None, now=NOW
    )
    assert first is not None and again is not None and first.id == again.id
    assert (first.state, first.primary_source_key) == ("found", "olx_business")
    excluded = await make_firm(session, state="excluded", exclude_reason="competitor")
    assert (
        await candidates.ensure_found(
            session, excluded, source_key="olx_business", contact_id=None, now=NOW
        )
        is None
    )


async def test_ensure_found_reads_the_pushed_lead_s_outcome(session):
    firm = await make_firm(session, last_signal_at=NOW)
    pushed = await candidate_in(
        session,
        "pushed",
        firm,
        pushed_at=LONG_AGO,
        external_id=f"cs:{firm.id}:202604",
    )
    session.add(
        Outcome(
            external_id=pushed.external_id,
            intake_id=uuid.uuid4(),
            candidate_id=pushed.id,
            company_id=firm.id,
            landing="created",
            stage_kind="open",
            erp_at=NOW,
            label="contacted_open",
            precision_class="pending",
            raw={},
        )
    )
    await session.flush()
    args = {"source_key": "olx_business", "contact_id": None, "now": NOW}
    assert await candidates.ensure_found(session, firm, **args) is None  # still open
    await session.execute(
        sa.update(Outcome)
        .where(Outcome.external_id == pushed.external_id)
        .values(stage_kind="lost", label="lost_price", precision_class="negative")
    )
    found = await candidates.ensure_found(session, firm, **args)
    assert found is not None and found.state == "found" and found.id != pushed.id


# --- the resolver's hook and score_job ------------------------------------------------


def post(text: str, **fields: Any) -> Seen:
    return Seen(
        source=fields.pop("source", "telegram_monitor"),
        source_ref=f"tg:-1001000000001:{next(_SEQ)}",
        url="https://t.me/namuna_optom/18234",
        fetched_at=NOW,
        raw_sha256="",
        kind=fields.pop("kind", "post"),
        company_name_raw=fields.pop("name", "Namuna Tekstil Savdo MChJ"),
        excerpt=text,
        published_at=POSTED,
        **fields,
    )


async def store(session, seen: Seen) -> Observation:
    """As the runner stores it: evidence + observations(status='new')."""
    body = to_observation_json(seen)
    evidence = Evidence(
        source_key=seen.source,
        url=seen.url,
        source_ref=seen.source_ref,
        fetched_at=NOW,
        sha256=hashlib.sha256(seen.source_ref.encode()).hexdigest(),
        bytes=0,
        excerpt=body["text"]["excerpt"],
        expires_at=NOW + timedelta(days=180),
    )
    session.add(evidence)
    await session.flush()
    row = Observation(evidence_id=evidence.id, source_key=seen.source, parsed=body)
    session.add(row)
    await session.flush()
    return row


POST_91 = "Guangzhoudan 2 kub mato kerak, kim olib keladi?"


async def the_candidate(session, firm_id=None) -> LeadCandidate:
    stmt = sa.select(LeadCandidate).execution_options(populate_existing=True)
    if firm_id is not None:
        stmt = stmt.where(LeadCandidate.company_id == firm_id)
    [row] = (await session.scalars(stmt)).all()
    return row


def test_the_hook_is_registered_with_the_resolver():
    assert candidates.after_resolve in companies.AFTER_RESOLVE


async def test_the_resolver_writes_rule_signals_and_the_found_candidate(session):
    obs = await store(session, post(POST_91, person_telegram="namuna_tekstil"))
    await session.commit()
    report = await companies.resolve_batch(now=NOW)
    assert (report.resolved, report.needs_review) == (1, 0)
    rows = (
        await session.execute(
            sa.select(Signal.key, Signal.quote, Signal.observation_id, Signal.source_key)
            .order_by(Signal.key)
            .execution_options(populate_existing=True)
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        ("cn_origin", "Guangzhoudan", obs.id, "telegram_monitor"),
        ("vol_stated_1_3", "2 kub", obs.id, "telegram_monitor"),
    ]
    cand = await the_candidate(session)
    person = await session.scalar(sa.select(Contact))
    assert (cand.state, cand.primary_source_key, cand.contact_id, cand.score) == (
        "found",
        "telegram_monitor",
        person.id,
        None,
    )
    # a second post of the same poster is the same company and candidate
    await store(session, post("Yana 2 kub mato kerak", person_telegram="namuna_tekstil"))
    await session.commit()
    await companies.resolve_batch(now=NOW + timedelta(minutes=5))
    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(LeadCandidate))
    ) == 1


async def test_9_1_through_score_job_52_enriching_then_70_review(session):
    await store(session, post(POST_91, person_telegram="namuna_tekstil"))
    await session.commit()
    await companies.resolve_batch(now=NOW)
    report = await candidates.score_batch(now=NOW)
    assert report.as_dict() == {"claimed": 1, "scored": 1, "failed": 0, "to_enriching": 1}
    cand = await the_candidate(session)
    assert (cand.state, cand.score, cand.tier, cand.weights_version) == (
        "enriching",
        52,
        "warm",
        1,
    )
    assert (cand.next_enrich_at, cand.last_scored_at) == (NOW + timedelta(hours=1), NOW)
    SCHEMA.validate(cand.score_breakdown)
    firm = await session.scalar(
        sa.select(Company).execution_options(populate_existing=True)
    )
    assert firm.best_score == 52
    # nothing new: nothing to score
    assert (await candidates.score_batch(now=NOW + timedelta(minutes=1))).claimed == 0

    # enrichment's registry hit and a directory phone (04 §9.1 attempt 1)
    registry = await _evidence(session, "registry_lookup")
    weights = scoring.weights_v1()
    drafts = [
        signals.SignalDraft(
            key=key,
            evidence_id=registry.id,
            observed_at=NOW,
            source_key="registry_lookup",
        )
        for key in ("ent_legal_confirmed", "ent_age_2y_active")
    ]
    await signals.emit(session, firm.id, drafts, weights=weights)
    session.add(
        Contact(
            company_id=firm.id, tier="company", phone="+998900000001", phone_kind="mobile"
        )
    )
    await session.commit()
    later = NOW + timedelta(hours=1)
    report = await candidates.score_batch(now=later)
    assert report.as_dict() == {"claimed": 1, "scored": 1, "failed": 0, "to_review": 1}
    cand = await the_candidate(session)
    assert (cand.state, cand.score, cand.tier) == ("review", 70, "hot")
    SCHEMA.validate(cand.score_breakdown)


async def test_a_resolution_that_brings_only_a_phone_still_rescores(session):
    """A directory card naming the poster's handle and a phone adds no signal,
    but the phone changes the score: the hook marks the candidate stale."""
    await store(session, post(POST_91, person_telegram="namuna_tekstil"))
    await session.commit()
    await companies.resolve_batch(now=NOW)
    await candidates.score_batch(now=NOW)
    card = post(
        "",
        kind="company",
        source="directories_yellowpages",
        telegram="namuna_tekstil",
        phones=["+998 90 000 00 41"],
    )
    await store(session, card)
    await session.commit()
    later = NOW + timedelta(hours=2)
    await companies.resolve_batch(now=later)
    assert (await the_candidate(session)).last_scored_at is None
    assert (await session.scalar(sa.select(sa.func.count()).select_from(Signal))) == 2
    report = await candidates.score_batch(now=later)
    cand = await the_candidate(session)
    # 52 + a mobile 6: still warm, still enriching
    assert (report.scored, cand.state, cand.score, cand.last_scored_at) == (
        1,
        "enriching",
        58,
        later,
    )


async def test_a_stated_director_adds_the_decision_maker_points(session):
    """04 §9.1's poster is «Namuna Tekstil Savdo MChJ direktori», and the
    resolver marks a direktor a decision maker (04 §2.4): the score is then
    54, not the example's 52, which counts no decision-maker points — the
    spec note T-18 reports."""
    await store(
        session, post(POST_91, person_telegram="namuna_tekstil", person_role="direktor")
    )
    await session.commit()
    await companies.resolve_batch(now=NOW)
    await candidates.score_batch(now=NOW)
    cand = await the_candidate(session)
    assert (cand.score, cand.state) == (54, "enriching")


async def test_a_found_candidate_waits_for_a_volume_signal(session):
    await store(session, post("Xitoydan mato bor", person_telegram="namuna_mato"))
    await session.commit()
    await companies.resolve_batch(now=NOW)
    assert (await candidates.score_batch(now=NOW)).claimed == 0
    cand = await the_candidate(session)
    assert (cand.state, cand.score) == ("found", None)


@pytest.fixture
async def auto_push(session) -> AsyncIterator[None]:
    """sources is seeded configuration (07 §1.0): snapshot, flip, restore."""
    before = await session.scalar(
        sa.select(Source.auto_push).where(Source.key == "telegram_monitor")
    )
    await session.execute(
        sa.update(Source).where(Source.key == "telegram_monitor").values(auto_push=True)
    )
    await session.commit()
    try:
        yield
    finally:
        await session.rollback()
        await session.execute(
            sa.update(Source)
            .where(Source.key == "telegram_monitor")
            .values(auto_push=before)
        )
        await session.commit()


async def test_a_hot_candidate_of_an_auto_push_source_is_approved_as_the_source(
    session, auto_push, monkeypatch
):
    monkeypatch.setattr(settings, "auto_approve", True)
    seen = post(
        "Guangzhoudan 25 kub mato kerak",
        person_telegram="namuna_auto",
        phones=["+998900000031"],
    )
    await store(session, seen)
    await session.commit()
    await companies.resolve_batch(now=NOW)
    report = await candidates.score_batch(now=NOW)
    assert report.as_dict()["to_approved"] == 1
    cand = await the_candidate(session)
    # volume 30 + origin 20 + recency 10 + contact (mobile 6 + dm 2)
    assert (cand.state, cand.score, cand.approved_by, cand.approved_at) == (
        "approved",
        68,
        "auto:telegram_monitor",
        NOW,
    )


async def test_an_earned_auto_push_approves_nothing_until_the_owner_turns_it_on(
    session, auto_push, monkeypatch
):
    """07 T-39 / 06 open question 2: the precision job earns ``auto_push``;
    while ``AUTO_APPROVE`` is off the hot candidate still goes to the owner's
    review — no ``auto:`` approval, nothing due for the push job."""
    monkeypatch.setattr(settings, "auto_approve", False)
    seen = post(
        "Guangzhoudan 25 kub mato kerak",
        person_telegram="namuna_auto",
        phones=["+998900000031"],
    )
    await store(session, seen)
    await session.commit()
    await companies.resolve_batch(now=NOW)
    report = await candidates.score_batch(now=NOW)
    assert ("to_approved" in report.as_dict(), report.as_dict()["to_review"]) == (
        False,
        1,
    )
    cand = await the_candidate(session)
    assert (cand.state, cand.score, cand.approved_by) == ("review", 68, None)
    assert candidates.auto_approves(True) is False
    monkeypatch.setattr(settings, "auto_approve", True)
    assert (candidates.auto_approves(True), candidates.auto_approves(False)) == (
        True,
        False,
    )


async def test_a_hold_that_has_not_run_out_is_rescored_in_place(session):
    firm = await make_firm(session)
    await _signal(session, firm, "vol_container")
    await _signal(session, firm, "cn_origin")
    cand = await candidate_in(
        session, "hold", firm, hold_until=NOW + timedelta(days=30), last_scored_at=None
    )
    await session.commit()
    report = await candidates.score_batch(now=NOW)
    assert report.as_dict() == {"claimed": 1, "scored": 1, "failed": 0}
    row = await reload(session, cand)
    # 40 + 20 + recency 10 + a mobile 6
    assert (row.state, row.score, row.hold_until) == (
        "hold",
        76,
        NOW + timedelta(days=30),
    )


async def test_a_review_card_is_rescored_but_stays_the_owner_s(session):
    firm = await make_firm(session)
    await _signal(session, firm, "cn_origin")
    cand = await candidate_in(
        session,
        "review",
        firm,
        last_scored_at=None,
        score_breakdown={"gates": {"not_dnc": "pass"}, "enrichment": [{"step": "E2"}]},
    )
    await session.commit()
    await candidates.score_batch(now=NOW)
    row = await reload(session, cand)
    # origin 20 + recency 10 + a mobile 6
    assert (row.state, row.score, row.tier) == ("review", 36, "cold")
    # The gates' own marks (T-19) replace the stale ones, the trail is kept.
    assert row.score_breakdown["gates"] == PASSING_GATES
    assert row.score_breakdown["enrichment"] == [{"step": "E2"}]


async def test_an_approved_candidate_is_not_rescored(session):
    firm = await make_firm(session)
    await _signal(session, firm, "vol_container")
    cand = await candidate_in(session, "approved", firm, last_scored_at=None)
    await session.commit()
    assert (await candidates.score_batch(now=NOW)).claimed == 0
    assert (await reload(session, cand)).score == 65


async def test_a_candidate_that_cannot_be_scored_costs_only_itself(session, monkeypatch):
    broken = await make_firm(session)
    fine = await make_firm(session)
    for firm in (broken, fine):
        await _signal(session, firm, "vol_container")
        await candidate_in(session, "scored", firm, last_scored_at=None)
    await session.commit()
    real = candidates.rescore

    async def rescore(session, candidate, **kw):
        if candidate.company_id == broken.id:
            raise RuntimeError("boom")
        return await real(session, candidate, **kw)

    monkeypatch.setattr(candidates, "rescore", rescore)
    report = await candidates.score_batch(now=NOW)
    assert (report.claimed, report.scored, report.failed) == (2, 1, 1)
    assert (await the_candidate(session, broken.id)).last_scored_at is None
    assert (await the_candidate(session, fine.id)).last_scored_at == NOW


async def test_score_job_returns_the_counts(session):
    firm = await make_firm(session, phone=None)
    await _signal(session, firm, "vol_container")
    await candidate_in(session, "found", firm, score=None, tier=None)
    await session.commit()
    counts = await candidates.score_job(now=NOW)
    # 40 + recency 10 = 50: warm
    assert counts == {"claimed": 1, "scored": 1, "failed": 0, "to_enriching": 1}


# --- review findings (T-18 review): one candidate's failure, lock order -------------


async def test_a_failure_after_the_score_is_written_costs_only_itself(
    session, monkeypatch, caplog
):
    """A failure after ``rescore`` flushed (the savepoint is rolled back and
    the row expired): the batch goes on, and the log names the candidate."""
    broken = await make_firm(session)
    fine = await make_firm(session)
    ids = {}
    for firm in (broken, fine):
        await _signal(session, firm, "vol_container")
        ids[firm.id] = (
            await candidate_in(session, "scored", firm, last_scored_at=None)
        ).id
    await session.commit()
    real = candidates.apply_score

    async def apply_score(session, candidate, result, **kw):
        if candidate.company_id == broken.id:
            assert candidate.score == result.total  # rescore wrote it
            raise RuntimeError("boom")
        return await real(session, candidate, result, **kw)

    monkeypatch.setattr(candidates, "apply_score", apply_score)
    with caplog.at_level("ERROR", logger=candidates.log.name):
        report = await candidates.score_batch(now=NOW)
    # the fine one: 40 + recency 10 + a mobile 6 = 56, warm
    assert report.as_dict() == {
        "claimed": 2,
        "scored": 1,
        "failed": 1,
        "to_enriching": 1,
    }
    [record] = [r for r in caplog.records if r.getMessage() == "scoring failed"]
    assert (record.candidate_id, record.error) == (str(ids[broken.id]), "RuntimeError")
    row = await the_candidate(session, broken.id)
    assert (row.state, row.score, row.last_scored_at) == ("scored", 65, None)


async def _lock_waiters(watcher) -> int:
    """Backends of this database waiting on a lock (pg_stat_activity, read on
    its own connection and rolled back: its snapshot freezes in a transaction)."""
    n = await watcher.scalar(
        sa.text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND datname = current_database()"
        )
    )
    await watcher.rollback()
    return int(n or 0)


async def _second_post(session) -> None:
    await store(session, post(POST_91, person_telegram="namuna_tekstil"))
    await session.commit()
    await companies.resolve_batch(now=NOW)
    await store(
        session, post("Yana 30 kub mato kerak Xitoydan", person_telegram="namuna_tekstil")
    )
    await session.commit()


async def test_a_resolver_waits_for_a_score_in_progress_and_neither_dies(
    session, monkeypatch
):
    """score_job holds the candidate and its company; the resolver's next
    post of that poster waits on the company and then lands — no deadlock
    (02 §2.9's two writers, one lock order)."""
    await _second_post(session)
    claimed, go = asyncio.Event(), asyncio.Event()
    real = scoring.views_for
    first = True

    async def paused(s, company):
        nonlocal first
        if first:
            first = False
            claimed.set()
            await go.wait()
        return await real(s, company)

    monkeypatch.setattr(scoring, "views_for", paused)
    scoring_task = asyncio.create_task(candidates.score_batch(now=NOW, limit=1))
    await asyncio.wait_for(claimed.wait(), 10)
    later = NOW + timedelta(minutes=5)
    resolving = asyncio.create_task(companies.resolve_batch(now=later))
    async with SessionLocal() as watcher:
        for _ in range(250):
            if await _lock_waiters(watcher):
                break
            await asyncio.sleep(0.02)
        waited = await _lock_waiters(watcher)
    go.set()
    scored, resolved = await asyncio.wait_for(asyncio.gather(scoring_task, resolving), 30)
    assert waited == 1
    assert (scored.scored, scored.failed) == (1, 0)
    assert (resolved.resolved, resolved.deferred, resolved.needs_review) == (1, 0, 0)
    assert (await the_candidate(session)).last_scored_at is None  # owed again
    report = await candidates.score_batch(now=later)
    cand = await the_candidate(session)
    # the 30 kub post counts: volume 20 + 30, origin 20, recency 10, dm 2
    assert (report.scored, cand.score) == (1, 72)


async def test_a_score_skips_a_company_a_resolver_holds(session, monkeypatch):
    """The resolver holds the company (emit's update) and has not marked the
    candidate yet: score_job skips it instead of waiting into a deadlock, and
    scores it on the next run."""
    await _second_post(session)
    held, go = asyncio.Event(), asyncio.Event()
    real = signals.mark_stale
    first = True

    async def paused(s, company_id):
        nonlocal first
        if first:
            first = False
            held.set()
            await go.wait()
        return await real(s, company_id)

    monkeypatch.setattr(signals, "mark_stale", paused)
    later = NOW + timedelta(minutes=5)
    resolving = asyncio.create_task(companies.resolve_batch(now=later))
    await asyncio.wait_for(held.wait(), 10)
    skipped = await asyncio.wait_for(candidates.score_batch(now=later, limit=5), 10)
    go.set()
    resolved = await asyncio.wait_for(resolving, 30)
    assert skipped.as_dict() == {"claimed": 0, "scored": 0, "failed": 0}
    assert (resolved.resolved, resolved.deferred) == (1, 0)
    report = await candidates.score_batch(now=later)
    assert (report.scored, (await the_candidate(session)).score) == (1, 72)


# --- review findings: what a re-score must see ----------------------------------------


@pytest.mark.parametrize(
    "texts",
    [
        ("Xitoydan bitta telefon kerak, o'zim uchun", POST_91),
        (POST_91, "Xitoydan bitta telefon kerak, o'zim uchun"),
    ],
    ids=["b2c_first", "volume_first"],
)
async def test_the_order_two_posts_arrive_in_changes_no_score(session, texts):
    """04 §5.1: the B2C negative counts only without a volume signal — the
    same two posts score 52 whichever came first."""
    for minute, text in enumerate(texts):
        await store(session, post(text, person_telegram="namuna_tekstil"))
        await session.commit()
        await companies.resolve_batch(now=NOW + timedelta(minutes=minute))
    await candidates.score_batch(now=NOW + timedelta(minutes=5))
    cand = await the_candidate(session)
    assert (cand.state, cand.score, cand.tier) == ("enriching", 52, "warm")
    assert cand.score_breakdown["negatives"] == []


async def test_a_phone_ends_the_phone_less_handoff_mark(session):
    cand = await phoneless(session)
    await candidates.phone_less_review(session, cand, now=NOW)
    session.add(
        Contact(
            company_id=cand.company_id,
            tier="company",
            phone="+998900000015",
            phone_kind="mobile",
        )
    )
    cand.last_scored_at = None
    await session.commit()
    await candidates.score_batch(now=NOW)
    row = await reload(session, cand)
    assert row.state == "review"
    assert row.score_breakdown["usable_phone"] is True
    assert row.score_breakdown["gates"] == PASSING_GATES  # no_phone is gone


async def test_the_company_s_own_channel_is_no_username_to_write_to(session):
    """02 §2.9 reads ``contacts.telegram_username``: a broadcast channel
    (``companies.telegram_channel``) cannot be answered."""
    firm = await make_firm(session, phone=None, telegram_channel="namuna_optom_kanal")
    cand = await candidate_in(
        session, "enriching", firm, enrich_attempts=3, score=65, tier="warm"
    )
    with pytest.raises(IllegalTransition, match="no telegram username"):
        await candidates.phone_less_review(session, cand, now=NOW)
    anonymised = await phoneless(session)
    await session.execute(
        sa.update(Contact)
        .where(Contact.company_id == anonymised.company_id)
        .values(anonymised_at=NOW)
    )
    with pytest.raises(IllegalTransition, match="no telegram username"):
        await candidates.phone_less_review(session, anonymised, now=NOW)


async def test_a_review_row_that_loses_its_phone_steps_back_to_hold(session):
    """06 §1.3 refuses a review card with neither a phone nor the handoff
    mark: a row whose only phone turned shared goes back to hold."""
    firm = await make_firm(session)
    for key in ("vol_stated_3plus", "cn_origin"):
        await _signal(session, firm, key)
    cand = await candidate_in(session, "review", firm, last_scored_at=None)
    await session.execute(
        sa.update(Contact).where(Contact.company_id == firm.id).values(shared_phone=True)
    )
    await session.commit()
    report = await candidates.score_batch(now=NOW)
    row = await reload(session, cand)
    # 30 + 20 + recency 10: ≥ 60 without a usable phone is warm
    assert (report.as_dict()["to_hold"], row.state, row.tier, row.score) == (
        1,
        "hold",
        "warm",
        60,
    )


async def test_a_hold_that_runs_out_is_scored_without_a_new_signal(session):
    """A review that expired or a cooldown (02 §2.9 holds with a
    ``hold_until``) comes back when the hold runs out, once."""
    firm = await make_firm(session, last_signal_at=NOW - timedelta(days=3))
    for key in ("vol_stated_3plus", "cn_origin"):
        await _signal(session, firm, key)
    cand = await candidate_in(
        session,
        "hold",
        firm,
        hold_until=NOW + timedelta(days=7),
        last_scored_at=NOW - timedelta(days=1),
    )
    await session.commit()
    assert (await candidates.score_batch(now=NOW)).claimed == 0  # still held
    later = NOW + timedelta(days=8)
    report = await candidates.score_batch(now=later)
    row = await reload(session, cand)
    assert (report.as_dict()["to_review"], row.state, row.hold_until) == (
        1,
        "review",
        None,
    )
    # a cold one that runs out stays where it is and is not claimed again
    cold = await make_firm(session, last_signal_at=NOW - timedelta(days=3))
    await _signal(session, cold, "cn_sector_prior")
    held = await candidate_in(
        session,
        "hold",
        cold,
        hold_until=NOW + timedelta(days=7),
        last_scored_at=NOW - timedelta(days=1),
    )
    await session.commit()
    assert (await candidates.score_batch(now=later)).scored == 1
    assert (await reload(session, held)).state == "hold"
    assert (await candidates.score_batch(now=later + timedelta(hours=1))).claimed == 0


async def test_a_candidate_older_than_its_company_s_newest_signal_is_scored(session):
    """04 §0's sweep: ``last_signal_at > last_scored_at`` is owed a score
    even when nothing cleared ``last_scored_at``."""
    firm = await make_firm(session, last_signal_at=NOW)
    await _signal(session, firm, "vol_container")
    await candidate_in(
        session, "enriching", firm, last_scored_at=NOW - timedelta(hours=1)
    )
    await session.commit()
    assert (await candidates.score_batch(now=NOW)).claimed == 1
    assert (await candidates.score_batch(now=NOW)).claimed == 0


async def test_a_warm_hold_keeps_its_enrichment_clock(session):
    """hold → scored → enriching keeps a ``next_enrich_at`` already set (a
    past one is due now), and sets one hour only when there is none."""
    firm = await make_firm(session, phone=None)
    await _signal(session, firm, "vol_container")
    due = NOW - timedelta(days=1)
    cand = await candidate_in(
        session, "hold", firm, last_scored_at=None, next_enrich_at=due
    )
    await session.commit()
    await candidates.score_batch(now=NOW)
    row = await reload(session, cand)
    # 40 + recency 10: warm
    assert (row.state, row.score, row.next_enrich_at) == ("enriching", 50, due)


async def test_the_source_is_the_one_whose_signal_made_the_candidate(session):
    """02 §2.9 ``primary_source_key``: a directory card that only brought a
    phone creates the candidate, the Telegram post brings the signals — the
    candidate is the Telegram source's."""
    card = post(
        "",
        kind="company",
        source="directories_yellowpages",
        telegram="namuna_tekstil",
        phones=["+998 90 000 00 41"],
    )
    await store(session, card)
    await session.commit()
    await companies.resolve_batch(now=NOW)
    assert (await the_candidate(session)).primary_source_key is None
    await store(session, post(POST_91, person_telegram="namuna_tekstil"))
    await session.commit()
    await companies.resolve_batch(now=NOW + timedelta(minutes=5))
    await candidates.score_batch(now=NOW + timedelta(minutes=5))
    assert (await the_candidate(session)).primary_source_key == "telegram_monitor"


async def test_a_merge_rescores_the_target(session):
    """companies.merge_into moves the source's signals onto the target: the
    target's open candidate is owed a score on them (and on the source's
    again after an unmerge)."""
    target = await make_firm(session)
    source = await make_firm(session, phone=None, last_signal_at=NOW - timedelta(days=3))
    await _signal(session, target, "vol_stated_1_3", source="telegram_monitor")
    await _signal(session, target, "cn_origin", source="telegram_monitor")
    await _signal(session, source, "vol_container")
    cand = await candidate_in(session, "found", target, score=None, tier=None)
    await session.commit()
    await candidates.score_batch(now=NOW)
    # 20 + 20 + recency 10 + a mobile 6
    assert ((await reload(session, cand)).state, (await reload(session, cand)).score) == (
        "enriching",
        56,
    )
    entry = await companies.merge_into(
        session,
        await session.get(Company, source.id),
        await session.get(Company, target.id),
        by="owner:1",
        now=NOW,
    )
    await session.commit()
    assert entry.last_signal_at == NOW - timedelta(days=3)
    report = await candidates.score_batch(now=NOW + timedelta(minutes=1))
    row = await reload(session, cand)
    # volume min(40, 20 + 40) + 20 + recency 10 + a mobile 6 + corroboration 5
    assert (report.claimed, row.state, row.score) == (1, "review", 81)
    log_id = await session.scalar(sa.text("SELECT max(id) FROM merge_log"))
    await companies.unmerge(session, log_id, by="owner:1", now=NOW + timedelta(hours=1))
    await session.commit()
    assert (await reload(session, cand)).last_scored_at is None


async def test_the_hook_leaves_a_merged_company_alone(session):
    """The resolver hands a merged company to no hook in practice (it
    resolves to the root); the hook refuses one all the same."""
    root = await make_firm(session)
    firm = await make_firm(session, state="merged", merged_into=root.id)
    obs = await store(session, post(POST_91))
    resolution = companies.Resolution(
        company=firm,
        how="telegram",
        score=100,
        created=False,
        duplicate_of=None,
        conflict=None,
    )
    await candidates.after_resolve(session, obs, resolution, NOW)
    assert await session.scalar(sa.select(sa.func.count()).select_from(Signal)) == 0
    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(LeadCandidate))
    ) == 0
