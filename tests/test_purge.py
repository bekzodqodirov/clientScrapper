"""Purge (06 §5.4, 02 §2.14; 07 §4.1 T-31): plan, then execute.

One rich subject is built on committed rows: a root company and a source
merged into it, a company contact and a person contact with a Telegram id,
observations, messages, signals, evidence (one row the subject alone cites,
one shared with another firm, one whose file another row holds), model jobs,
aliases, platform ids, three candidates (pushed, open with a failed push,
rejected) and a merged source's excluded one, review cards, push payloads,
an outcome, sheet rows and the firm's monitored channel — and beside it a
control firm whose rows nothing may touch. Synthetic data only (07 §1.0).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import cards, evidence, purge, retention
from clientscrapper.tools import purge as purge_tool
from tests import gate_fixtures
from tests.gate_fixtures import phone, tail

SUBJECT_TG = 5100000042
POSTER_TG = 5100000043
CONTROL_TG = 5100000049
APPEND_ONLY = (m.PushLog, m.Outcome, m.MergeLog, m.UsageLog)


def now() -> datetime:
    return datetime.now(settings.tz)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    found = tmp_path / "evidence"
    found.mkdir()
    return found


async def stored(session, root: Path, body: bytes, url: str) -> m.Evidence:
    row = await evidence.store(
        session,
        source_key="telegram_monitor",
        url=url,
        body=body,
        content_type="text/plain",
        fetched_at=now(),
        excerpt=body.decode(),
        root=root,
    )
    await session.commit()
    return row


def observation(ev: m.Evidence, company: m.Company | None, ordinal: int = 0):
    return m.Observation(
        evidence_id=ev.id,
        ordinal=ordinal,
        source_key="telegram_monitor",
        status="resolved",
        parsed={"kind": "intent_post"},
        company_id=company.id if company is not None else None,
        resolved_at=now(),
    )


_MESSAGES = iter(range(1, 10_000))


def message(
    ev: m.Evidence, sender: int, username: str | None, obs: m.Observation | None = None
) -> m.Interaction:
    return m.Interaction(
        tg_chat_id=-100700,
        tg_message_id=next(_MESSAGES),
        sender_id=sender,
        sender_username=username,
        sender_name="Rustam",
        text="5 kub mato kerak",
        occurred_at=now(),
        prefilter_hit=["volume_kub"],
        evidence_id=ev.id,
        observation_id=obs.id if obs is not None else None,
    )


def job(status: str, company: m.Company | None, **kw: Any) -> m.ModelJob:
    return m.ModelJob(
        kind="classify",
        lane="batch" if status == "fallback" else "instant",
        model="m",
        custom_id=f"cs-{uuid.uuid4()}",
        status=status,
        company_id=company.id if company is not None else None,
        request={"messages": "<PHONE_1>"},
        pseudonyms=kw.pop("pseudonyms", {"<PHONE_1>": phone(42)}),
        result={"kind": "intent_post"} if status == "applied" else None,
        **kw,
    )


def sheet(kind: str, entity: Any) -> m.SheetRow:
    return m.SheetRow(
        tab="Kompaniyalar" if kind == "company" else "Yuborilganlar",
        entity_kind=kind,
        entity_id=str(entity),
        content_hash="0" * 64,
        state="synced",
    )


@dataclass
class World:
    root: m.Company
    source: m.Company
    control: m.Company
    twin: m.Company
    rows: dict[str, Any]


async def subject(session, root: Path) -> World:
    firm = await gate_fixtures.firm(
        session,
        stir="300000041",
        website_domain="namuna41.example.uz",
        telegram_channel="namuna_kanal41",
        instagram="namuna_insta41",
    )
    source = await gate_fixtures.firm(
        session,
        state="merged",
        merged_into=firm.id,
        stir="300000042",
        website_domain="namuna42.example.uz",
    )
    session.add(
        m.MergeLog(
            source_company_id=source.id,
            target_company_id=firm.id,
            decided_by="owner:1",
            moved={},
        )
    )
    control = await gate_fixtures.firm(session, stir="300000049")
    # A firm the resolver thought a duplicate of the subject's.
    twin = await gate_fixtures.firm(session, dup_of=firm.id, dup_score=80)
    await gate_fixtures.contact(session, firm, phone(41))
    await gate_fixtures.contact(
        session,
        firm,
        phone(42),
        tier="person",
        telegram_id=SUBJECT_TG,
        telegram_username="namuna_boss",
    )
    await gate_fixtures.contact(session, control, phone(49))
    await session.commit()
    r: dict[str, Any] = {}
    r["ev_own"] = await stored(session, root, b"namuna own post", "https://t.me/n/1")
    r["ev_shared"] = await stored(session, root, b"namuna shared", "https://t.me/n/2")
    r["ev_twin_a"] = await stored(session, root, b"namuna twin", "https://t.me/n/3")
    r["ev_twin_b"] = await stored(session, root, b"namuna twin", "https://t.me/n/4")
    r["ev_msg"] = await stored(session, root, b"namuna message", "https://t.me/n/5")
    assert r["ev_twin_a"].raw_path == r["ev_twin_b"].raw_path
    r["obs_own"] = observation(r["ev_own"], firm)
    r["obs_shared"] = observation(r["ev_shared"], firm)
    r["obs_control"] = observation(r["ev_shared"], control, ordinal=1)
    r["obs_twin"] = observation(r["ev_twin_a"], firm)
    r["obs_twin_control"] = observation(r["ev_twin_b"], control)
    session.add_all([r[k] for k in r if k.startswith("obs_")])
    await session.flush()
    # The subject's own message; the post its observation came from (written
    # by its poster); a control message in the same chat.
    r["msg_own"] = message(r["ev_msg"], SUBJECT_TG, "namuna_boss")
    r["msg_post"] = message(r["ev_own"], POSTER_TG, "namuna_poster", r["obs_own"])
    r["msg_control"] = message(
        r["ev_shared"], CONTROL_TG, "namuna_boshqa", r["obs_control"]
    )
    session.add_all([r["msg_own"], r["msg_post"], r["msg_control"]])
    session.add(
        m.Signal(
            company_id=firm.id,
            evidence_id=r["ev_own"].id,
            observation_id=r["obs_own"].id,
            key="vol_stated_3plus",
            observed_at=now(),
            source_key="telegram_monitor",
        )
    )
    r["sig_source"] = await gate_fixtures.signal(session, source)
    r["sig_control"] = await gate_fixtures.signal(session, control)
    r["jobs"] = {
        "applied": job("applied", firm, applied_at=now()),
        "pending": job("pending", firm),
        "fallback": job("fallback", firm),
        "by_evidence": job("applied", None, evidence_ids=[r["ev_msg"].id]),
        "control": job(
            "applied",
            control,
            applied_at=now(),
            pseudonyms={"<PHONE_1>": phone(49), "<TG_1>": "namuna_boshqa"},
        ),
        # Another company's request that quoted the subject: its reverse map
        # holds their number as written and their handle (seam review wave 4).
        "naming_phone": job(
            "applied", control, applied_at=now(), pseudonyms={"<PHONE_1>": "90 000 00 41"}
        ),
        "naming_handle": job(
            "applied", control, applied_at=now(), pseudonyms={"<TG_1>": "@Namuna_Boss"}
        ),
        "naming_id": job(
            "applied", control, applied_at=now(), pseudonyms={"<TG_2>": str(SUBJECT_TG)}
        ),
    }
    session.add_all(r["jobs"].values())
    session.add(
        m.CompanyAlias(company_id=firm.id, alias="Namuna Eski", alias_key="namuna eski")
    )
    session.add(m.CompanyId(company_id=firm.id, id_type="olx_user", id_value="n41"))
    session.add(m.CompanyId(company_id=control.id, id_type="olx_user", id_value="n49"))
    pushed = await gate_fixtures.candidate(session, firm, state="pushed")
    open_ = await gate_fixtures.candidate(session, firm, state="approved")
    rejected = await gate_fixtures.candidate(
        session, firm, state="rejected", score=40, rejected_at=now()
    )
    merged_away = await gate_fixtures.candidate(
        session, source, state="excluded", score=None, exclude_reason="merged"
    )
    kept_control = await gate_fixtures.candidate(session, control, state="approved")
    r["candidates"] = {
        "pushed": pushed,
        "open": open_,
        "rejected": rejected,
        "merged_away": merged_away,
        "control": kept_control,
    }
    sent = await gate_fixtures.push(
        session,
        pushed,
        sent_at=now(),
        phone_tail=tail(41),
        external_id=pushed.external_id,
    )
    failed = await gate_fixtures.push(
        session,
        open_,
        sent_at=now(),
        phone_tail=tail(41),
        status="failed",
        external_id=f"cs:{firm.id}:202609",
    )
    control_push = await gate_fixtures.push(
        session, kept_control, sent_at=now(), phone_tail=tail(49), status="failed"
    )
    for row in (sent, failed, control_push):
        row.payload = {"lead": {"name": "Namuna Savdo", "phone": phone(41)}}
    r["pushes"] = {"sent": sent, "failed": failed, "control": control_push}
    for cand, state in (
        (open_, "pending"),
        (rejected, "declined"),
        (kept_control, "pending"),
    ):
        session.add(
            m.LeadReview(
                candidate_id=cand.id,
                state=state,
                answered_at=now() if state == "declined" else None,
            )
        )
    session.add(
        m.Outcome(
            external_id=pushed.external_id,
            intake_id=uuid.uuid4(),
            candidate_id=pushed.id,
            company_id=firm.id,
            landing="created",
            erp_at=now(),
            label="contacted_open",
            precision_class="pending",
            raw={},
        )
    )
    session.add(m.UsageLog(provider="anthropic", operation="classify", cost_usd=0))
    session.add_all(
        [sheet("company", firm.id), sheet("push", sent.id), sheet("company", control.id)]
    )
    session.add_all(
        [
            m.ChatMonitor(
                tg_chat_id=-1007001, chat_type="channel", username="namuna_kanal41"
            ),
            m.ChatMonitor(
                tg_chat_id=-1007002, chat_type="channel", username="namuna_boshqa"
            ),
        ]
    )
    await session.commit()
    return World(firm, source, control, twin, r)


async def count(session, model, *where: Any) -> int:
    stmt = sa.select(sa.func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    return int(await session.scalar(stmt) or 0)


async def fresh(session, model, key):
    return await session.get(model, key, populate_existing=True)


async def snapshot(session) -> dict[str, int]:
    return {model.__tablename__: await count(session, model) for model in APPEND_ONLY}


EXPECTED = {
    "companies": 2,
    "contacts": 2,
    "observations": 3,
    "interactions": 2,
    "signals": 2,
    "evidence": 4,
    "files": 2,
    "model_jobs": 7,
    "aliases": 1,
    "company_ids": 1,
    "candidates": 2,
    "candidates_ended": 1,
    "reviews": 2,
    "push_payloads": 2,
    "sheet_rows": 2,
    "chats": 1,
    "erp_leads": 1,
}


# --- plan, then execute (06 §5.4) ------------------------------------------------------


async def test_a_purge_executes_what_its_plan_counted_and_the_log_holds_a_hash(
    session, root
):
    w = await subject(session, root)
    before = await snapshot(session)
    files = {
        name: root / w.rows[name].raw_path
        for name in ("ev_own", "ev_shared", "ev_twin_a", "ev_msg")
    }
    assert all(path.exists() for path in files.values())
    # A merged source names its root's lineage.
    plan = await purge.plan_company(session, w.source.id)
    assert plan.subject_kind == "company"
    assert plan.subject_hash == hashlib.sha256(str(w.root.id).encode()).hexdigest()
    assert plan.company_ids == (w.root.id,)
    assert {k: v for k, v in plan.counts.items() if k != "suppression"} == EXPECTED
    assert plan.counts["suppression"] >= 12
    # Planning changed nothing.
    assert (await fresh(session, m.Company, w.root.id)).state == "active"
    assert await count(session, m.PurgeLog) == 0
    purge_log_id = await purge.record(session, plan, requested_by="owner:1001")
    await session.commit()

    result = await purge.execute(
        session, plan, requested_by="owner:1001", now=now(), root=root
    )

    assert result.purge_log_id == purge_log_id
    assert result.executed == plan.counts
    assert result.files_removed == 2
    row = await fresh(session, m.PurgeLog, purge_log_id)
    assert (row.subject_kind, row.reason, row.requested_by, row.error) == (
        "company",
        "legal_request",
        "owner:1001",
        None,
    )
    assert row.subject_hash == plan.subject_hash
    assert row.plan == plan.counts
    assert row.executed == {**plan.counts, "files_removed": 2}
    assert row.executed_at is not None
    logged = json.dumps([row.plan, row.executed, row.subject_hash, row.requested_by])
    for secret in (
        str(w.root.id),
        str(w.source.id),
        "Namuna",
        tail(41),
        tail(42),
        "300000041",
        "namuna_boss",
        str(SUBJECT_TG),
    ):
        assert secret not in logged
    # Append-only tables keep every row; the subject's push payloads are gone,
    # the control firm's stays.
    assert await snapshot(session) == before
    sent = await fresh(session, m.PushLog, w.rows["pushes"]["sent"].id)
    failed = await fresh(session, m.PushLog, w.rows["pushes"]["failed"].id)
    control_push = await fresh(session, m.PushLog, w.rows["pushes"]["control"].id)
    assert sent.payload is None and failed.payload is None
    assert control_push.payload is not None
    # SQL NULL, not JSON 'null': a later IS NOT NULL never finds them again.
    for table, column in (
        ("push_log", "payload"),
        ("model_jobs", "request"),
        ("model_jobs", "pseudonyms"),
        ("model_jobs", "result"),
        ("companies", "verdict"),
    ):
        nulls = await session.scalar(
            sa.text(f"SELECT count(*) FROM {table} WHERE {column} = 'null'::jsonb")
        )
        assert nulls == 0, (table, column)
    assert not await purge.unfinished(session, w.root.id)
    # Files: the subject's own two unlinked; the shared row's file and the
    # file the control's twin row holds stay.
    assert not files["ev_own"].exists() and not files["ev_msg"].exists()
    assert files["ev_shared"].exists() and files["ev_twin_a"].exists()


# Wave 4's homes meet the purge: T-30's opener on score_breakdown, T-21's ⏭
# card (lead_reviews.remind_at) and push note (lead_candidates.push_note).
OPENER = "Assalomu alaykum! Namuna Savdo uchun Xitoydan yuk tashishni taklif qilamiz."


async def test_a_purge_takes_the_opener_the_put_off_card_and_the_push_note(session, root):
    w = await subject(session, root)
    cands = w.rows["candidates"]
    opened = ("pushed", "open", "control")
    for key in opened:
        row = await fresh(session, m.LeadCandidate, cands[key].id)
        row.score_breakdown = {
            "total": 70,
            "opener": OPENER,
            "opener_meta": {"source": "draft", "segment": "A", "lang": "uz"},
        }
    refused = await fresh(session, m.LeadCandidate, cands["rejected"].id)
    refused.push_note = "Telefon yaroqsiz"
    refused.push_noted_at = now()
    for cand, chat in ((cands["open"], 7001), (cands["control"], 7002)):
        session.add(
            m.LeadReview(
                candidate_id=cand.id,
                kind="manager",
                state="asked",
                card_chat_id=chat,
                card_message_id=1,
                asked_at=now(),
                expires_at=now(),
                remind_at=now(),
            )
        )
    await session.commit()
    plan = await purge.plan_company(session, w.root.id)
    await purge.record(session, plan, requested_by="owner:1001")
    await session.commit()

    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)

    # The kept candidates (push history) lose the opener, the one free text
    # of the breakdown; the points stay. The control firm's is untouched.
    for key in ("pushed", "open"):
        breakdown = (await fresh(session, m.LeadCandidate, cands[key].id)).score_breakdown
        assert breakdown == {"total": 70}, key
    control = await fresh(session, m.LeadCandidate, cands["control"].id)
    assert control.score_breakdown["opener"] == OPENER
    # The refused candidate had no push: it goes, and its note with it.
    assert await fresh(session, m.LeadCandidate, cands["rejected"].id) is None
    # The ⏭ card is gone with its row: review_cards_job's resend finds only
    # the control's.
    due = await session.scalars(
        sa.select(m.LeadReview.candidate_id).where(
            m.LeadReview.state == "asked", m.LeadReview.remind_at <= now()
        )
    )
    assert list(due) == [cands["control"].id]


async def test_the_companies_are_tombstoned_and_a_merged_source_keeps_its_own_keys(
    session, root
):
    w = await subject(session, root)
    for company in (w.root, w.source, w.control):
        company.address = "Toshkent, Namuna koʻchasi 1-uy"
        company.city = "Toshkent"
    await session.commit()
    plan = await purge.plan_company(session, w.root.id)
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    firm = await fresh(session, m.Company, w.root.id)
    source = await fresh(session, m.Company, w.source.id)
    for company in (firm, source):
        assert (company.state, company.display_name, company.name_key) == (
            "purged",
            purge.PURGED_NAME,
            purge.PURGED_NAME_KEY,
        )
        assert company.merged_into is None and company.verdict is None
        # A sole trader's address may be a home: it goes; the city stays.
        assert (company.address, company.city) == (None, "Toshkent")
    # The root keeps what the resolver answers «purged» by; so does the source,
    # for a STIR and a domain nobody else holds.
    assert (firm.stir, firm.website_domain, firm.telegram_channel, firm.instagram) == (
        "300000041",
        "namuna41.example.uz",
        "namuna_kanal41",
        "namuna_insta41",
    )
    assert (source.stir, source.website_domain) == ("300000042", "namuna42.example.uz")
    twin = await fresh(session, m.Company, w.twin.id)
    assert (twin.dup_of, twin.dup_score) == (None, None)
    control = await fresh(session, m.Company, w.control.id)
    assert (control.state, control.stir) == ("active", "300000049")
    assert control.address == "Toshkent, Namuna koʻchasi 1-uy"


async def test_a_candidate_with_push_history_is_ended_never_deleted(session, root):
    w = await subject(session, root)
    c = w.rows["candidates"]
    plan = await purge.plan_company(session, w.root.id)
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    pushed = await fresh(session, m.LeadCandidate, c["pushed"].id)
    ended = await fresh(session, m.LeadCandidate, c["open"].id)
    assert pushed is not None and pushed.state == "pushed"
    assert ended is not None and ended.state == "suppressed"
    held = await fresh(session, m.Suppression, ended.suppressed_by)
    assert (held.kind, held.source) == ("legal", "owner_bot")
    assert await fresh(session, m.LeadCandidate, c["rejected"].id) is None
    assert await fresh(session, m.LeadCandidate, c["merged_away"].id) is None
    control = await fresh(session, m.LeadCandidate, c["control"].id)
    assert control is not None and control.state == "approved"
    # Every review card of the subject goes; the control's stays.
    reviews = list(await session.scalars(sa.select(m.LeadReview.candidate_id)))
    assert reviews == [c["control"].id]
    # The outcome of the kept push still names its candidate.
    outcome = await fresh(session, m.Outcome, c["pushed"].external_id)
    assert outcome.candidate_id == c["pushed"].id


async def test_the_people_data_goes_and_the_control_firm_s_stays(session, root):
    w = await subject(session, root)
    r = w.rows
    plan = await purge.plan_company(session, w.root.id)
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    lineage = (w.root.id, w.source.id)
    assert await count(session, m.Contact, m.Contact.company_id.in_(lineage)) == 0
    assert await count(session, m.Contact, m.Contact.company_id == w.control.id) == 1
    assert await count(session, m.Observation, m.Observation.company_id.in_(lineage)) == 0
    assert await count(session, m.Signal, m.Signal.company_id.in_(lineage)) == 0
    assert await count(session, m.Signal, m.Signal.company_id == w.control.id) == 1
    senders = set(await session.scalars(sa.select(m.Interaction.sender_id)))
    assert senders == {CONTROL_TG}
    observed = set(await session.scalars(sa.select(m.Observation.id)))
    assert observed == {r["obs_control"].id, r["obs_twin_control"].id}
    kept = set(await session.scalars(sa.select(m.Evidence.id)))
    assert r["ev_shared"].id in kept and r["ev_twin_b"].id in kept
    assert r["sig_control"].evidence_id in kept
    for name in ("ev_own", "ev_twin_a", "ev_msg"):
        assert r[name].id not in kept
    assert r["sig_source"].evidence_id not in kept
    assert await count(session, m.CompanyAlias) == 0
    assert set(await session.scalars(sa.select(m.CompanyId.id_value))) == {"n49"}
    jobs = {k: await fresh(session, m.ModelJob, v.id) for k, v in r["jobs"].items()}
    naming = ("naming_phone", "naming_handle", "naming_id")
    for name in ("applied", "pending", "fallback", "by_evidence", *naming):
        found = jobs[name]
        assert (found.request, found.pseudonyms, found.result) == (None, None, None)
    assert (jobs["applied"].status, jobs["applied"].error) == ("applied", None)
    assert (jobs["pending"].status, jobs["pending"].error) == ("failed", "purged")
    assert jobs["fallback"].status == "fallback"
    assert jobs["fallback"].applied_at is not None
    assert jobs["control"].request is not None
    states = dict(
        (await session.execute(sa.select(m.SheetRow.entity_id, m.SheetRow.state))).all()
    )
    assert states == {
        str(w.root.id): "tombstone",
        str(r["pushes"]["sent"].id): "tombstone",
        str(w.control.id): "synced",
    }
    chats = dict(
        (
            await session.execute(
                sa.select(m.ChatMonitor.username, m.ChatMonitor.monitor_enabled)
            )
        ).all()
    )
    assert chats == {"namuna_kanal41": False, "namuna_boshqa": True}


async def test_every_key_the_subject_had_becomes_a_legal_suppression_row(session, root):
    w = await subject(session, root)
    plan = await purge.plan_company(session, w.root.id)
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    rows = list(await session.scalars(sa.select(m.Suppression)))
    assert {(r.kind, r.source, r.reason) for r in rows} == {
        ("legal", "owner_bot", "purge:legal_request")
    }
    keys = {(r.key_type, r.key) for r in rows}
    assert len(keys) == plan.counts["suppression"] == len(rows)
    assert keys == {
        ("company_id", str(w.root.id)),
        ("company_id", str(w.source.id)),
        ("stir", "300000041"),
        ("stir", "300000042"),
        ("domain", "namuna41.example.uz"),
        ("domain", "namuna42.example.uz"),
        ("tg_username", "namuna_kanal41"),
        ("instagram", "namuna_insta41"),
        ("phone_tail", tail(41)),
        ("phone_tail", tail(42)),
        ("tg_username", "namuna_boss"),
        ("tg_user_id", str(SUBJECT_TG)),
        # The poster of the message the subject's observation came from.
        ("tg_username", "namuna_poster"),
        ("tg_user_id", str(POSTER_TG)),
    }


async def test_a_plan_runs_once(session, root):
    w = await subject(session, root)
    firm_id = w.root.id  # a refused execute rolls the session back
    plan = await purge.plan_company(session, firm_id)
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    with pytest.raises(purge.PurgeRefused) as refused:
        await purge.execute(
            session, plan, requested_by="owner:1001", now=now(), root=root
        )
    assert refused.value.code == "done"
    # A purged company can be planned again (a failed run is finished so).
    again = await purge.plan_company(session, firm_id)
    assert again.counts["contacts"] == 0 and again.counts["companies"] == 2


async def test_an_abandoned_plan_never_runs(session, root):
    w = await subject(session, root)
    firm_id = w.root.id
    plan = await purge.plan_company(session, firm_id)
    purge_log_id = await purge.record(session, plan, requested_by="owner:1001")
    assert await purge.abandon(session, purge_log_id, why="cancelled")
    await session.commit()
    assert not await purge.abandon(session, purge_log_id, why="expired")
    with pytest.raises(purge.PurgeRefused) as refused:
        await purge.execute(
            session, plan, requested_by="owner:1001", now=now(), root=root
        )
    assert refused.value.code == "closed"
    row = await fresh(session, m.PurgeLog, purge_log_id)
    assert (row.error, row.executed_at) == ("cancelled", None)
    assert (await fresh(session, m.Company, firm_id)).state == "active"


async def test_a_failed_group_is_logged_and_a_new_plan_finishes_the_job(
    session, root, monkeypatch
):
    w = await subject(session, root)
    firm_id, own = w.root.id, root / w.rows["ev_own"].raw_path
    plan = await purge.plan_company(session, firm_id)

    async def broken(*args: Any, **kw: Any) -> dict[str, int]:
        raise RuntimeError("disk")

    monkeypatch.setattr(purge, "_people", broken)
    with pytest.raises(RuntimeError):
        await purge.execute(
            session, plan, requested_by="owner:1001", now=now(), root=root
        )
    row = await fresh(session, m.PurgeLog, plan.purge_log_id)
    assert (row.error, row.executed_at) == ("failed:RuntimeError", None)
    # Group 1 committed: nothing cards, pushes or resolves to the firm now.
    assert (await fresh(session, m.Company, firm_id)).state == "purged"
    assert await count(session, m.Suppression, m.Suppression.kind == "legal") > 0
    assert await count(session, m.Contact, m.Contact.company_id == firm_id) == 2
    with pytest.raises(purge.PurgeRefused) as refused:
        await purge.execute(
            session, plan, requested_by="owner:1001", now=now(), root=root
        )
    assert refused.value.code == "failed"
    monkeypatch.undo()
    again = await purge.plan_company(session, firm_id)
    await purge.execute(session, again, requested_by="owner:1001", now=now(), root=root)
    assert await count(session, m.Contact, m.Contact.company_id == firm_id) == 0
    assert not own.exists()


async def test_another_reason_ends_a_pushed_candidate_excluded_and_writes_no_key(
    session, root
):
    w = await subject(session, root)
    plan = await purge.plan_company(session, w.root.id, reason="owner")
    assert plan.counts["suppression"] == 0
    result = await purge.execute(
        session, plan, requested_by="owner:1001", now=now(), root=root
    )
    assert result.executed["candidates_ended"] == 1
    ended = await fresh(session, m.LeadCandidate, w.rows["candidates"]["open"].id)
    # approved → review → excluded: the one open state excluded is not reached
    # from directly (02 §2.9).
    assert (ended.state, ended.exclude_reason) == ("excluded", "owner")
    assert await count(session, m.Suppression) == 0
    lines = purge.plan_lines(plan)
    assert "Bostirish kaliti yozilmaydi" in lines[-3]
    # The lead the ERP received stays there: the owner is told to erase it
    # in the ERP (seam review wave 4).
    assert lines[-2] == purge.ERP_LINE.format(erp_leads=1)


# --- by phone, by Telegram id --------------------------------------------------------


async def test_a_phone_tail_names_the_companies_whose_contacts_carry_it(session, root):
    w = await subject(session, root)
    plan = await purge.plan_contact(session, phone_tail=tail(42))
    assert plan.subject_kind == "contact"
    assert plan.subject_hash == hashlib.sha256(tail(42).encode()).hexdigest()
    assert plan.company_ids == (w.root.id,)
    by_company = await purge.plan_company(session, w.root.id)
    assert plan.counts == by_company.counts
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    assert (await fresh(session, m.Company, w.root.id)).state == "purged"
    assert (await fresh(session, m.Company, w.control.id)).state == "active"


async def test_a_telegram_id_alone_takes_its_messages_and_its_keys(session, root):
    stranger = 5100000061
    ev = await stored(session, root, b"namuna begona", "https://t.me/n/61")
    other = await stored(session, root, b"namuna boshqa", "https://t.me/n/62")
    session.add_all(
        [
            message(ev, stranger, "namuna_begona"),
            message(ev, stranger, "namuna_begona"),
            message(other, CONTROL_TG, "namuna_boshqa"),
        ]
    )
    await session.commit()
    path = root / ev.raw_path
    plan = await purge.plan_contact(session, tg_user_id=stranger)
    assert (plan.subject_kind, plan.company_ids) == ("tg_user", ())
    assert plan.subject_hash == hashlib.sha256(str(stranger).encode()).hexdigest()
    assert plan.counts["interactions"] == 2
    assert (plan.counts["evidence"], plan.counts["files"]) == (1, 1)
    result = await purge.execute(
        session, plan, requested_by="owner:1001", now=now(), root=root
    )
    assert result.files_removed == 1 and not path.exists()
    assert set(await session.scalars(sa.select(m.Interaction.sender_id))) == {CONTROL_TG}
    keys = set(
        (
            await session.execute(sa.select(m.Suppression.key_type, m.Suppression.key))
        ).all()
    )
    assert keys == {("tg_user_id", str(stranger)), ("tg_username", "namuna_begona")}


async def test_a_username_names_the_ids_it_was_seen_on(session, root):
    """/unut @username for a person kept under no contact (seam review wave
    4): their messages by the username's sender ids, an intent post's id
    too, both keys — and another sender's message stays."""
    ev = await stored(session, root, b"namuna begona 2", "https://t.me/n/71")
    other = await stored(session, root, b"namuna boshqa 2", "https://t.me/n/72")
    obs = observation(other, None)
    obs.parsed = {
        "kind": "intent_post",
        "handles": {"telegram": "Namuna_Begona", "tg_user_id": 5100000072},
    }
    session.add(obs)
    session.add_all(
        [
            message(ev, 5100000071, "namuna_begona"),
            message(other, CONTROL_TG, "boshqa_xx"),
        ]
    )
    await session.commit()
    assert await purge.senders_of(session, "@Namuna_Begona") == [5100000071, 5100000072]
    assert await purge.senders_of(session, "t.me/namuna_begona") == [
        5100000071,
        5100000072,
    ]
    assert await purge.plan_handle(session, "@hech_kim_yoq") is None
    plan = await purge.plan_handle(session, "@namuna_begona")
    assert plan is not None
    assert (plan.subject_kind, plan.company_ids) == ("tg_user", ())
    assert plan.tg_user_ids == (5100000071, 5100000072)
    assert plan.counts["interactions"] == 1
    await purge.record(session, plan, requested_by="owner:1001")
    await session.commit()
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    assert set(await session.scalars(sa.select(m.Interaction.sender_id))) == {CONTROL_TG}
    # The intent post that named them, resolved nowhere, goes too.
    assert await fresh(session, m.Observation, obs.id) is None
    keys = set(
        (
            await session.execute(sa.select(m.Suppression.key_type, m.Suppression.key))
        ).all()
    )
    assert {
        ("tg_username", "namuna_begona"),
        ("tg_user_id", "5100000071"),
        ("tg_user_id", "5100000072"),
    } <= keys
    assert ("tg_username", "boshqa_xx") not in keys


@pytest.mark.parametrize("by", ["id", "username"])
async def test_an_intent_post_naming_the_subject_goes_with_them(session, root, by):
    """Resolved to no company and sent by nobody the agent kept: the post's
    own handles name the subject (a channel poster has a username only)."""
    ev = await stored(
        session, root, f"namuna post {by}".encode(), f"https://t.me/n/9{by}"
    )
    obs = observation(ev, None)
    held = {"tg_user_id": 5100000091} if by == "id" else {"telegram": "namuna_kanal"}
    obs.parsed = {"kind": "intent_post", "handles": held}
    session.add(obs)
    await session.commit()
    if by == "id":
        plan = await purge.plan_contact(session, tg_user_id=5100000091)
    else:
        found = await purge.plan_handle(session, "@namuna_kanal")
        assert found is not None
        plan = found
    assert plan.counts["observations"] == 1
    await purge.record(session, plan, requested_by="owner:1001")
    await session.commit()
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    assert await fresh(session, m.Observation, obs.id) is None


async def test_make_purge_takes_a_username(session, root, monkeypatch):
    monkeypatch.setattr(settings, "evidence_dir", str(root))
    ev = await stored(session, root, b"namuna begona 3", "https://t.me/n/73")
    session.add(message(ev, 5100000073, "namuna_uchinchi"))
    await session.commit()
    code, lines = await purge_tool.run(args(tg_handle="@namuna_uchinchi"))
    assert code == 0 and lines[-1] == purge_tool.DRY_RUN_TEXT
    code, lines = await purge_tool.run(args(tg_handle="@namuna_uchinchi", apply=True))
    assert code == 0
    assert await count(session, m.Interaction, m.Interaction.sender_id == 5100000073) == 0
    code, lines = await purge_tool.run(args(tg_handle="@hech_kim_yoq"))
    assert (code, lines) == (
        1,
        [purge_tool.UNKNOWN_HANDLE_TEXT.format(subject="@hech_kim_yoq")],
    )
    parsed = purge_tool.parse_args(["--tg-handle", "@namuna_uchinchi"])
    assert parsed.tg_handle == "@namuna_uchinchi"


async def test_the_plan_refuses_what_it_cannot_name(session):
    with pytest.raises(purge.PurgeRefused) as refused:
        await purge.plan_company(session, uuid.uuid4())
    assert refused.value.code == "unknown"
    company = await gate_fixtures.firm(session)
    with pytest.raises(ValueError):
        await purge.plan_company(session, company.id, reason="retention")
    with pytest.raises(ValueError):
        await purge.plan_contact(session)
    with pytest.raises(ValueError):
        await purge.plan_contact(session, phone_tail=tail(41), tg_user_id=1)
    with pytest.raises(ValueError):
        await purge.plan_contact(session, phone_tail="90000")


def test_the_plan_text_is_counts_in_uzbek():
    plan = purge.PurgePlan(
        subject_kind="company",
        subject_hash="0" * 64,
        reason="legal_request",
        company_ids=(uuid.uuid4(),),
        counts={key: 3 for key in purge.COUNT_KEYS},
    )
    lines = purge.plan_lines(plan)
    assert lines[0] == "🗑 Oʻchirish rejasi · huquqiy soʻrov"
    assert "🔒 Abadiy bostirish kaliti: 3 ta" in lines
    assert lines[-1] == purge.KEPT_LINE
    empty = purge.PurgePlan("company", "0" * 64, "dnc", (), counts={})
    assert purge.plan_lines(empty) == [
        "🗑 Oʻchirish rejasi · yozilmasin",
        purge.EMPTY_TEXT,
    ]


# --- make purge (tools/purge.py) -----------------------------------------------------


def args(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(
        company=kw.get("company"),
        phone=kw.get("phone"),
        tg_user=kw.get("tg_user"),
        tg_handle=kw.get("tg_handle"),
        reason=kw.get("reason", purge.DEFAULT_REASON),
        apply=kw.get("apply", False),
    )


async def test_make_purge_prints_the_plan_and_changes_nothing_without_apply(
    session, root, monkeypatch
):
    monkeypatch.setattr(settings, "evidence_dir", str(root))
    w = await subject(session, root)
    code, lines = await purge_tool.run(args(company=w.root.id))
    assert code == 0
    assert lines[0] == "🗑 Oʻchirish rejasi · huquqiy soʻrov"
    assert lines[-1] == purge_tool.DRY_RUN_TEXT
    assert await count(session, m.PurgeLog) == 0
    assert (await fresh(session, m.Company, w.root.id)).state == "active"
    code, lines = await purge_tool.run(args(company=w.root.id, apply=True), now=now())
    assert code == 0
    assert lines[-1] == purge.FILES_LINE.format(removed=2)
    row = await session.scalar(sa.select(m.PurgeLog))
    assert row.executed is not None and row.requested_by.startswith("owner:")
    code, lines = await purge_tool.run(args(company=uuid.uuid4()))
    assert code == 1 and lines[0].startswith("Bunday kompaniya topilmadi")
    code, lines = await purge_tool.run(args(phone="123"))
    assert code == 1


def test_make_purge_parses_one_subject():
    parsed = purge_tool.parse_args(["--phone", tail(41), "--reason", "dnc", "--apply"])
    assert (parsed.phone, parsed.reason, parsed.apply) == (tail(41), "dnc", True)
    with pytest.raises(SystemExit):
        purge_tool.parse_args(["--phone", tail(41), "--tg-user", "1"])
    with pytest.raises(SystemExit):
        purge_tool.parse_args(["--company", str(uuid.uuid4()), "--reason", "retention"])


# --- a purge that stopped part-way (06 §5.4: one transaction per group) -------------


def backdate(*paths: Path, hours: float = 2) -> None:
    stamp = now().timestamp() - hours * 3600
    for path in paths:
        os.utime(path, (stamp, stamp))


async def test_a_stop_after_the_evidence_commit_leaves_files_r1_sweeps(
    session, root, tmp_path, monkeypatch
):
    """Rows go first and commit, files after: a process stopped in between
    (a deploy, OOM) leaves the subject's raw pages with no row to name them.
    R1's orphan sweep is how they still go."""
    w = await subject(session, root)
    own = [root / w.rows[n].raw_path for n in ("ev_own", "ev_msg")]
    kept = [root / w.rows[n].raw_path for n in ("ev_shared", "ev_twin_a")]
    plan = await purge.plan_company(session, w.root.id)
    await purge.record(session, plan, requested_by="owner:1001")
    await session.commit()

    def killed(paths: Any, *, root: Path | None = None) -> int:
        raise asyncio.CancelledError  # the process is stopped here

    monkeypatch.setattr(purge.evidence, "unlink_raw", killed)
    with pytest.raises(asyncio.CancelledError):
        await purge.execute(
            session, plan, requested_by="owner:1001", now=now(), root=root
        )
    monkeypatch.undo()
    await session.rollback()
    assert all(path.is_file() for path in own)
    row = await fresh(session, m.PurgeLog, plan.purge_log_id)
    assert (row.error, row.executed_at) == ("running", None)
    names = [str(path.relative_to(root)) for path in own]
    assert await count(session, m.Evidence, m.Evidence.raw_path.in_(names)) == 0
    # Nothing is left in the database to plan again.
    assert not await purge.unfinished(session, w.root.id)
    backdate(*own, *kept)
    imports = tmp_path / "imports"
    imports.mkdir()
    report = await retention.retention_job(root=root, imports_dir=imports)
    assert report["R1"]["orphans"] == 2
    assert not any(path.exists() for path in own)
    assert all(path.is_file() for path in kept)


async def test_a_half_done_purge_is_found_again_and_finished(session, root, monkeypatch):
    w = await subject(session, root)
    firm_id, source_id = w.root.id, w.source.id
    own = root / w.rows["ev_own"].raw_path
    plan = await purge.plan_company(session, firm_id)

    async def broken(*args: Any, **kw: Any) -> dict[str, int]:
        raise RuntimeError("db blip in group 2")

    monkeypatch.setattr(purge, "_people", broken)
    with pytest.raises(RuntimeError):
        await purge.execute(
            session, plan, requested_by="owner:1001", now=now(), root=root
        )
    monkeypatch.undo()
    firm = await fresh(session, m.Company, firm_id)
    assert firm.state == "purged" and await purge.unfinished(session, firm_id)
    # Every key /unut takes still names the firm: by its kept STIR, its uuid,
    # the source's uuid, the person's phone and username — read as /unut
    # reads, in a session of its own (this one still holds the source as it
    # was loaded, «merged» into the root).
    async with SessionLocal() as other:
        for query in (
            "300000041",
            str(firm_id),
            str(source_id),
            phone(42),
            "@namuna_boss",
        ):
            assert await cards.live_roots(other, query) == [firm_id], query
    again = await purge.plan_company(session, source_id)
    assert again.company_ids == (firm_id,)
    assert (again.counts["contacts"], again.counts["evidence"]) == (2, 4)
    await purge.execute(session, again, requested_by="owner:1001", now=now(), root=root)
    assert await count(session, m.Contact, m.Contact.company_id == firm_id) == 0
    assert not own.exists()
    assert not await purge.unfinished(session, firm_id)
    for query in ("300000041", str(firm_id), str(source_id)):
        assert await cards.live_roots(session, query) == [], query


async def test_the_claim_commits_before_the_purge_reads(session, root, monkeypatch):
    """A second tap, a cancel or an expiry that comes while the purge runs
    finds the row ``running`` — never open, which it would close."""
    w = await subject(session, root)
    plan = await purge.plan_company(session, w.root.id)
    await purge.record(session, plan, requested_by="owner:1001")
    await session.commit()
    seen: list[str] = []
    real = purge._stop

    async def watched(*args: Any, **kw: Any) -> dict[str, int]:
        async with SessionLocal() as other:
            assert plan.purge_log_id is not None
            # An uncommitted claim would hold the row: wait 2 s, not for ever.
            await other.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
            seen.append(await purge.row_state(other, plan.purge_log_id))
            seen.append(str(await purge.abandon(other, plan.purge_log_id, why="expired")))
            await other.commit()
        return await real(*args, **kw)

    monkeypatch.setattr(purge, "_stop", watched)
    result = await purge.execute(
        session, plan, requested_by="owner:1001", now=now(), root=root
    )
    assert seen == ["running", "False"]
    assert result.executed["companies"] == 2
    assert await purge.row_state(session, result.purge_log_id) == "done"


async def test_the_log_row_says_where_a_plan_stands(session):
    plan = purge.PurgePlan("company", "0" * 64, "owner", (), counts={})
    states = {}
    for error, executed in (
        (None, False),
        ("running", False),
        ("failed:RuntimeError", False),
        ("cancelled", False),
        ("expired", False),
        (None, True),
    ):
        row_id = await purge.record(session, plan, requested_by="owner:1")
        await session.execute(
            sa.update(m.PurgeLog)
            .where(m.PurgeLog.id == row_id)
            .values(error=error, executed_at=now() if executed else None)
        )
        states[(error, executed)] = await purge.row_state(session, row_id)
    assert states == {
        (None, False): "open",
        ("running", False): "running",
        ("failed:RuntimeError", False): "failed",
        ("cancelled", False): "closed",
        ("expired", False): "closed",
        (None, True): "done",
    }
    assert await purge.row_state(session, 10**12) == "unknown"


# --- who the subject is ----------------------------------------------------------------


async def test_a_merged_source_gives_up_only_a_key_another_company_holds(session, root):
    firm = await gate_fixtures.firm(
        session, stir="300000051", website_domain="n51.example.uz"
    )
    # The STIR the merge carried (the root's own); a domain only this source had.
    carried = await gate_fixtures.firm(
        session,
        state="merged",
        merged_into=firm.id,
        stir="300000051",
        website_domain="n52.example.uz",
    )
    # Two sources with one domain nobody else holds: the first keeps it.
    twins = [
        await gate_fixtures.firm(
            session, state="merged", merged_into=firm.id, website_domain="n53.example.uz"
        )
        for _ in range(2)
    ]
    for source in (carried, *twins):
        session.add(
            m.MergeLog(
                source_company_id=source.id,
                target_company_id=firm.id,
                decided_by="owner:1",
                moved={},
            )
        )
    await session.commit()
    plan = await purge.plan_company(session, firm.id, reason="owner")
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    carried = await fresh(session, m.Company, carried.id)
    assert (carried.state, carried.stir, carried.website_domain) == (
        "purged",
        None,
        "n52.example.uz",
    )
    domains = []
    for twin in twins:
        domains.append(str((await fresh(session, m.Company, twin.id)).website_domain))
    assert sorted(domains) == ["None", "n53.example.uz"]
    # A key only the source had now names the purged firm, not a new one.
    assert await cards.find_company_ids(session, "n52.example.uz") == [carried.id]


async def test_a_firm_that_loses_the_subject_s_signals_is_scored_again(session, root):
    """A post the subject's person wrote that resolved to another firm is the
    person's: it goes, with its signals; that firm is scored again, not purged."""
    w = await subject(session, root)
    other = await gate_fixtures.firm(session)
    cand = await gate_fixtures.candidate(session, other, state="review", score=65)
    ev = await stored(session, root, b"namuna boshqa firma", "https://t.me/n/71")
    obs = observation(ev, other)
    session.add(obs)
    await session.flush()
    session.add(message(ev, SUBJECT_TG, "namuna_boss", obs))
    session.add(
        m.Signal(
            company_id=other.id,
            evidence_id=ev.id,
            observation_id=obs.id,
            key="vol_stated_3plus",
            observed_at=now(),
            source_key="telegram_monitor",
        )
    )
    await session.commit()
    assert (await fresh(session, m.LeadCandidate, cand.id)).last_scored_at is not None
    plan = await purge.plan_company(session, w.root.id)
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    assert await count(session, m.Signal, m.Signal.company_id == other.id) == 0
    assert await fresh(session, m.Observation, obs.id) is None
    assert await fresh(session, m.Evidence, ev.id) is None
    after = await fresh(session, m.LeadCandidate, cand.id)
    assert (after.state, after.last_scored_at) == ("review", None)
    assert (await fresh(session, m.Company, other.id)).state == "active"
    # The control firm, which lost nothing, was not touched.
    control = await fresh(session, m.LeadCandidate, w.rows["candidates"]["control"].id)
    assert control.last_scored_at is not None


async def test_an_observation_that_names_the_subject_s_person_goes(session, root):
    """Reached through the contact, whatever company it resolved to."""
    w = await subject(session, root)
    person = await session.scalar(
        sa.select(m.Contact).where(m.Contact.telegram_id == SUBJECT_TG)
    )
    ev = await stored(session, root, b"namuna shaxs", "https://t.me/n/72")
    obs = observation(ev, w.control)
    obs.contact_id = person.id
    session.add(obs)
    await session.commit()
    plan = await purge.plan_company(session, w.root.id)
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    assert await fresh(session, m.Observation, obs.id) is None
    assert await fresh(session, m.Evidence, ev.id) is None


async def test_a_tail_only_an_anonymised_contact_holds_names_no_company(session):
    company = await gate_fixtures.firm(session)
    # Anonymised, the number kept (as a landline would be): no holder.
    await gate_fixtures.contact(
        session, company, phone(44), tier="person", anonymised_at=now()
    )
    await session.commit()
    plan = await purge.plan_contact(session, phone_tail=tail(44))
    assert plan.company_ids == ()
    assert plan.counts["companies"] == 0 and plan.counts["suppression"] == 1


async def test_a_telegram_id_names_the_company_whose_contact_carries_it(session, root):
    w = await subject(session, root)
    plan = await purge.plan_contact(session, tg_user_id=SUBJECT_TG)
    assert (plan.subject_kind, plan.company_ids) == ("tg_user", (w.root.id,))
    assert plan.counts["contacts"] == 2
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    assert (await fresh(session, m.Company, w.root.id)).state == "purged"
    assert await count(session, m.Contact, m.Contact.telegram_id == SUBJECT_TG) == 0
    assert (await fresh(session, m.Company, w.control.id)).state == "active"


async def test_a_channel_poster_is_a_username_key_never_a_user_id(session, root):
    """A channel's post has a negative peer id: no user id key is written for
    it (the channel's username is)."""
    w = await subject(session, root)
    ev = await stored(session, root, b"namuna kanal posti", "https://t.me/n/73")
    obs = observation(ev, w.root)
    session.add(obs)
    await session.flush()
    session.add(message(ev, -1001234567890, "namuna_kanal_post", obs))
    await session.commit()
    plan = await purge.plan_company(session, w.root.id)
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    keys = set(
        (
            await session.execute(sa.select(m.Suppression.key_type, m.Suppression.key))
        ).all()
    )
    assert ("tg_username", "namuna_kanal_post") in keys
    assert not any(key.startswith("-") for kind, key in keys if kind == "tg_user_id")


async def test_an_excluded_company_is_purged_without_its_old_reason(session, root):
    company = await gate_fixtures.firm(session, state="excluded", exclude_reason="b2c")
    await session.commit()
    plan = await purge.plan_company(session, company.id, reason="b2c")
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    found = await fresh(session, m.Company, company.id)
    assert (found.state, found.exclude_reason) == ("purged", None)


async def test_make_purge_reads_a_phone_as_written_and_answers_in_uzbek(
    session, root, monkeypatch
):
    monkeypatch.setattr(settings, "evidence_dir", str(root))
    w = await subject(session, root)
    code, lines = await purge_tool.run(args(phone="+998 90 000 00 42"))
    assert code == 0 and lines[-1] == purge_tool.DRY_RUN_TEXT
    assert "🏢 Kompaniya: 2 · kontakt: 2" in lines
    for raw, text in (
        ("+7 999 000 00 42", purge_tool.FOREIGN_PHONE_TEXT),
        ("123", purge_tool.BAD_PHONE_TEXT),
        ("90000004x", purge_tool.BAD_PHONE_TEXT),
    ):
        assert await purge_tool.run(args(phone=raw)) == (1, [text]), raw

    async def broken(*a: Any, **kw: Any) -> dict[str, int]:
        raise RuntimeError("disk")

    monkeypatch.setattr(purge, "_people", broken)
    code, lines = await purge_tool.run(args(phone=tail(42), apply=True), now=now())
    assert (code, lines[-1]) == (1, purge_tool.FAILED_TEXT)
    monkeypatch.undo()
    monkeypatch.setattr(settings, "evidence_dir", str(root))
    # The same command again finishes it: the tail is still on a contact.
    code, lines = await purge_tool.run(args(phone=tail(42), apply=True), now=now())
    assert code == 0 and lines[-1] == purge.FILES_LINE.format(removed=2)
    assert await count(session, m.Contact, m.Contact.company_id == w.root.id) == 0
    for state, text in purge_tool.REFUSED_TEXTS.items():
        assert "Oʻchirilmadi" in text, state


# --- the cards a purged lead left in the chats (seam review wave 4) ---------------


@dataclass
class Editor:
    """``reviews.CardSender``'s edit, recorded; Telegram refuses ``refuse``."""

    refuse: set[int]
    edits: list[tuple[int, int, str, Any]]

    async def edit(self, chat: int, message: int, text: str, layout: Any, _: int) -> bool:
        if chat in self.refuse:
            return False
        self.edits.append((chat, message, text, layout))
        return True


async def _carded(session, root: Path) -> tuple[World, purge.PurgePlan]:
    w = await subject(session, root)
    cands = w.rows["candidates"]
    for cand, chat, msg in (
        (cands["open"], 7001, 11),
        (cands["pushed"], 7003, 12),
        (cands["control"], 7002, 13),
    ):
        session.add(
            m.LeadReview(
                candidate_id=cand.id,
                kind="manager",
                state="asked",
                card_chat_id=chat,
                card_message_id=msg,
                asked_at=now(),
                expires_at=now(),
            )
        )
    # An answered card shows its lead all the same.
    declined = await session.scalar(
        sa.select(m.LeadReview).where(m.LeadReview.candidate_id == cands["rejected"].id)
    )
    declined.card_chat_id, declined.card_message_id = 7004, 14
    await session.commit()
    plan = await purge.plan_company(session, w.root.id)
    await purge.record(session, plan, requested_by="owner:1001")
    await session.commit()
    await purge.execute(session, plan, requested_by="owner:1001", now=now(), root=root)
    return w, plan


async def _executed(session, plan: purge.PurgePlan) -> dict[str, Any]:
    row = await fresh(session, m.PurgeLog, plan.purge_log_id)
    return dict(row.executed or {})


async def test_a_purged_leads_cards_lose_its_data_in_the_chats(session, root):
    """The cards named the company, its phone and its quotes; the purge
    deleted their rows. Each is edited to the purged text with no buttons —
    the control firm's never — and one Telegram refused stays listed."""
    _, plan = await _carded(session, root)
    held = await _executed(session, plan)
    assert sorted(held[purge.CARDS_KEY]) == [[7001, 11], [7003, 12], [7004, 14]]
    assert held["companies"] > 0  # the counts are written beside them

    editor = Editor(refuse={7003}, edits=[])
    assert await purge.close_purged_cards(SessionLocal, editor, now=now()) == 2
    assert sorted(editor.edits) == [
        (7001, 11, purge.PURGED_CARD_UZ, None),
        (7004, 14, purge.PURGED_CARD_UZ, None),
    ]
    left = await _executed(session, plan)
    assert left[purge.CARDS_KEY] == [[7003, 12]]
    assert left["companies"] == held["companies"]

    again = Editor(refuse=set(), edits=[])
    assert await purge.close_purged_cards(SessionLocal, again, now=now()) == 1
    assert again.edits == [(7003, 12, purge.PURGED_CARD_UZ, None)]
    assert purge.CARDS_KEY not in await _executed(session, plan)


async def test_a_card_telegram_never_took_is_forgotten_after_the_window(session, root):
    _, plan = await _carded(session, root)
    editor = Editor(refuse=set(), edits=[])
    later = now() + purge.CARDS_WINDOW + timedelta(hours=1)
    assert await purge.close_purged_cards(SessionLocal, editor, now=later) == 0
    assert editor.edits == []
    assert purge.CARDS_KEY not in await _executed(session, plan)


async def test_the_card_job_closes_purged_cards_even_while_paused(session, root):
    from clientscrapper.services import owner, reviews

    _, plan = await _carded(session, root)
    await owner.pause(session, by="owner:1001", reason=None, now=now())
    await session.commit()
    editor = Editor(refuse=set(), edits=[])
    report = await reviews.run_cards(editor, now=now())  # type: ignore[arg-type]
    assert report.held == "paused"
    assert report.purged == 3
    assert {(c, msg) for c, msg, _, _ in editor.edits} == {
        (7001, 11),
        (7003, 12),
        (7004, 14),
    }
