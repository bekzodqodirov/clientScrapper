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
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.services import evidence, purge
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
        pseudonyms={"<PHONE_1>": phone(42)},
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
        "control": job("applied", control, applied_at=now()),
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
    "model_jobs": 4,
    "aliases": 1,
    "company_ids": 1,
    "candidates": 2,
    "candidates_ended": 1,
    "reviews": 2,
    "push_payloads": 2,
    "sheet_rows": 2,
    "chats": 1,
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
    # Files: the subject's own two unlinked; the shared row's file and the
    # file the control's twin row holds stay.
    assert not files["ev_own"].exists() and not files["ev_msg"].exists()
    assert files["ev_shared"].exists() and files["ev_twin_a"].exists()


async def test_the_companies_are_tombstoned_and_a_merged_source_lets_go_of_its_keys(
    session, root
):
    w = await subject(session, root)
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
    # The root keeps what the resolver answers «purged» by; the source gives
    # its STIR and domain up (they would collide with the root's once it is
    # no longer 'merged').
    assert (firm.stir, firm.website_domain, firm.telegram_channel, firm.instagram) == (
        "300000041",
        "namuna41.example.uz",
        "namuna_kanal41",
        "namuna_insta41",
    )
    assert (source.stir, source.website_domain) == (None, None)
    twin = await fresh(session, m.Company, w.twin.id)
    assert (twin.dup_of, twin.dup_score) == (None, None)
    control = await fresh(session, m.Company, w.control.id)
    assert (control.state, control.stir) == ("active", "300000049")


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
    for name in ("applied", "pending", "fallback", "by_evidence"):
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
    assert refused.value.code == "closed"
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
    assert "Bostirish kaliti yozilmaydi" in purge.plan_lines(plan)[-2]


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
