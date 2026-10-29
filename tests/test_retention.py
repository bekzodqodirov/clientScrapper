"""Retention (02 §4, 02 §10 AC 8; 07 §4.1 T-31): ``retention_job``, step by
step.

Fixtures are committed rows aged by string-edited timestamps
(``now() - interval 'N days'``): 02 §4's rules are measured by the database's
clock, so a row is put 31 days back, never a fixed calendar date. Evidence
files go to a tmp ``EVIDENCE_DIR`` (the store writes them when the
transaction commits). Synthetic data only (07 §1.0).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import evidence, health, retention
from tests import gate_fixtures
from tests.collectors.test_telegram_monitor import R3_SCRUB
from tests.gate_fixtures import phone, tail
from tests.test_constraints import R6_HOLD_SWEEP

STEPS = ("R1", "R2a", "R2b", "R3", "R4b", "R4", "R5", "R6", "R6b")


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    root, imports = tmp_path / "evidence", tmp_path / "imports"
    root.mkdir()
    imports.mkdir()
    return root, imports


async def run(dirs: tuple[Path, Path], **kw: Any) -> dict[str, dict[str, int]]:
    root, imports = dirs
    return await retention.retention_job(root=root, imports_dir=imports, **kw)


async def age(session, table: str, column: str, row_id: Any, days: int) -> None:
    """The string-edited timestamp: ``column`` set ``days`` back. Not for an
    ``updated_at`` (``cs_touch_updated_at`` sets it on every UPDATE): that one
    is given at the INSERT (``ago``)."""
    await session.execute(
        sa.text(
            f"UPDATE {table} SET {column} = now() - interval '{days} days' WHERE id = :i"
        ),
        {"i": row_id},
    )


def now() -> datetime:
    return datetime.now(settings.tz)


def ago(days: int) -> datetime:
    return now() - timedelta(days=days)


async def stored(
    session,
    root: Path,
    body: bytes,
    url: str,
    source: str = "directories",
    excerpt: str | None = None,
):
    row = await evidence.store(
        session,
        source_key=source,
        url=url,
        body=body,
        content_type="text/plain",
        fetched_at=now(),
        excerpt=excerpt if excerpt is not None else body.decode(),
        root=root,
    )
    await session.commit()
    return row


async def fresh(session, model, row_id):
    return await session.get(model, row_id, populate_existing=True)


async def count(session, model) -> int:
    return int(await session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


# --- the statements are 02 §4's ---------------------------------------------------------


def test_the_statements_are_the_spec_s():
    assert retention.R3_SCRUB == R3_SCRUB
    assert retention.R6_HOLD_SWEEP == R6_HOLD_SWEEP
    assert dict(retention.R6_STATEMENTS)["lead_candidates"] == R6_HOLD_SWEEP
    assert retention.R2A_DELETE == (
        "DELETE FROM interactions WHERE occurred_at < now() - interval '30 days' AND "
        "(observation_id IS NULL OR observation_id IN (SELECT id FROM observations "
        "WHERE status = 'rejected'))"
    )
    assert retention.R2B_UPDATE == (
        "UPDATE interactions SET text = NULL, sender_bio = NULL, sender_name = NULL, "
        "text_purged_at = now() WHERE occurred_at < now() - interval '90 days' "
        "AND text IS NOT NULL"
    )
    assert [name for name, _ in retention.STEPS] == list(STEPS)


# --- every step writes its purge_log row ----------------------------------------------


async def test_every_step_writes_its_own_purge_log_row(session, dirs):
    report = await run(dirs)
    assert list(report) == list(STEPS)
    rows = list(await session.scalars(sa.select(m.PurgeLog).order_by(m.PurgeLog.id)))
    assert [r.plan["step"] for r in rows] == list(STEPS)
    day = now().date().isoformat()
    for row in rows:
        step = row.plan["step"]
        expected = hashlib.sha256(f"retention:{step}:{day}".encode()).hexdigest()
        assert (row.subject_kind, row.subject_hash, row.reason, row.requested_by) == (
            "retention",
            expected,
            "retention",
            "job:retention",
        )
        assert row.executed == report[step]
        assert row.executed_at is not None and row.error is None


async def test_a_failing_step_rolls_back_alone_and_the_job_says_so(
    session, dirs, monkeypatch
):
    async def broken(s, ctx):
        await s.execute(sa.text("UPDATE push_log SET payload = NULL"))
        raise RuntimeError("synthetic")

    company = await gate_fixtures.firm(session)
    cand = await gate_fixtures.candidate(
        session, company, state="hold", updated_at=ago(366)
    )
    await session.commit()
    steps = dict(retention.STEPS)
    monkeypatch.setattr(retention, "STEPS", (("R2a", broken), ("R6", steps["R6"])))
    with pytest.raises(retention.RetentionFailed) as failed:
        await run(dirs)
    assert failed.value.steps == ["R2a"]
    rows = list(await session.scalars(sa.select(m.PurgeLog).order_by(m.PurgeLog.id)))
    assert [(r.plan["step"], r.error, r.executed) for r in rows] == [
        ("R2a", "RuntimeError", None),
        ("R6", None, rows[1].executed),
    ]
    # R6 ran after R2a failed: the hold candidate is gone.
    assert rows[1].executed["lead_candidates"] == 1
    assert await fresh(session, m.LeadCandidate, cand.id) is None


# --- R1 raw evidence ------------------------------------------------------------------


async def test_r1_unlinks_a_file_only_when_no_other_row_shares_the_sha(session, dirs):
    root, _ = dirs
    shared = b"Namuna Savdo: 5 kub mato kerak"
    a = await stored(session, root, shared, "https://namuna.example.uz/a")
    b = await stored(session, root, shared, "https://namuna.example.uz/b")
    c = await stored(session, root, b"Boshqa sahifa", "https://namuna.example.uz/c")
    assert a.raw_path == b.raw_path and a.sha256 == b.sha256
    file_ab, file_c = root / str(a.raw_path), root / str(c.raw_path)
    assert file_ab.is_file() and file_c.is_file()
    for row in (a, c):
        await age(session, "evidence", "expires_at", row.id, 1)
    await session.commit()
    report = await run(dirs)
    assert report["R1"] == {
        "rows": 2,
        "files": 1,
        "tmp": 0,
        "orphans": 0,
        "over_cap_rows": 0,
        "over_cap_files": 0,
    }
    a, b, c = [await fresh(session, m.Evidence, r.id) for r in (a, b, c)]
    # The rows stay (url, excerpt are what cards quote); only the file goes.
    assert (a.raw_path, b.raw_path, c.raw_path) == (None, b.raw_path, None)
    assert a.raw_deleted_at is not None and c.raw_deleted_at is not None
    assert b.raw_deleted_at is None and a.excerpt == shared.decode()
    assert file_ab.is_file()  # row b still holds it
    assert not file_c.exists()
    await age(session, "evidence", "expires_at", b.id, 1)
    await session.commit()
    assert (await run(dirs))["R1"]["files"] == 1
    assert not file_ab.exists()


async def test_r1_sweeps_a_crash_s_orphan_tmp_and_leaves_a_write_in_flight(session, dirs):
    root, _ = dirs
    (root / "26").mkdir()
    orphan = root / "26" / f"{'a' * 64}.gz.123.deadbeef.tmp"
    flight = root / "26" / f"{'b' * 64}.gz.124.cafebabe.tmp"
    orphan.write_bytes(b"half")
    flight.write_bytes(b"half")
    old = (now() - timedelta(hours=2)).timestamp()
    os.utime(orphan, (old, old))
    report = await run(dirs)
    assert report["R1"]["tmp"] == 1
    assert not orphan.exists() and flight.exists()


def backdate(path: Path, hours: float = 2) -> None:
    stamp = (now() - timedelta(hours=hours)).timestamp()
    os.utime(path, (stamp, stamp), follow_symlinks=False)


async def test_r1_sweeps_a_stored_file_no_row_names_and_keeps_the_rest(session, dirs):
    """A purge or R2a deletes its rows, commits, then unlinks; a stop in
    between leaves files no row names (02 §3). R1 finds them on disk."""
    root, _ = dirs
    named = await stored(session, root, b"Namuna nomli sahifa", "https://n.example.uz/1")
    gone = await stored(session, root, b"Namuna yetim sahifa", "https://n.example.uz/2")
    purged = await stored(session, root, b"Namuna eski sahifa", "https://n.example.uz/3")
    files = {
        "named": root / str(named.raw_path),
        "gone": root / str(gone.raw_path),
        "purged": root / str(purged.raw_path),
    }
    # The row of «gone» was deleted after its file was written; «purged»'s row
    # stays without a raw_path (R1 took it; its file was left behind).
    await session.execute(sa.delete(m.Evidence).where(m.Evidence.id == gone.id))
    await session.execute(
        sa.update(m.Evidence).where(m.Evidence.id == purged.id).values(raw_path=None)
    )
    await session.commit()
    # The same bytes under another year's folder: a row names 26/<sha>, not
    # 25/<sha> — the file a row names is its raw_path, not its sha.
    other_year = root / "25" / Path(str(named.raw_path)).name
    if str(named.raw_path).startswith("25/"):
        other_year = root / "24" / Path(str(named.raw_path)).name
    other_year.parent.mkdir(exist_ok=True)
    other_year.write_bytes(files["named"].read_bytes())
    young = root / "26" / f"{'c' * 64}.gz"  # an unnamed file younger than the grace
    young.parent.mkdir(exist_ok=True)
    young.write_bytes(b"young")
    foreign = root / "26" / "README.gz"  # not raw_path's shape: never touched
    foreign.write_bytes(b"x")
    outside = root.parent / f"{'d' * 64}.gz"
    outside.write_bytes(b"x")
    for path in (*files.values(), other_year, foreign, outside):
        backdate(path)
    report = await run(dirs)
    assert report["R1"]["orphans"] == 3
    assert files["named"].is_file()
    assert not files["gone"].exists() and not files["purged"].exists()
    assert not other_year.exists()
    assert young.is_file() and foreign.is_file() and outside.is_file()
    # The rows are as they were.
    assert (await fresh(session, m.Evidence, named.id)).raw_path == named.raw_path
    assert (await run(dirs))["R1"]["orphans"] == 0


async def test_r1_keeps_reporting_a_directory_over_the_cap_it_cannot_free(
    session, dirs, monkeypatch
):
    """«It clears on the first run that did not have to»: a run over the cap
    with no row left to take a file from had to, and could not."""
    root, _ = dirs
    (root / "26").mkdir()
    big = root / "26" / f"{'e' * 64}.gz"  # an unnamed file still in its grace
    big.write_bytes(os.urandom(5000))
    await health.raise_problem(
        session, retention.CAP_PROBLEM, now=now(), by="test", detail={"gb": 0.0}
    )
    await session.commit()
    monkeypatch.setattr(settings, "evidence_max_gb", 1000 / retention.GB)
    report = await run(dirs)
    assert (report["R1"]["over_cap_rows"], report["R1"]["orphans"]) == (0, 0)
    assert await fresh(session, m.RuntimeFlag, "problem:evidence_over_cap") is not None
    big.unlink()
    await run(dirs)
    assert await fresh(session, m.RuntimeFlag, "problem:evidence_over_cap") is None


def test_unlink_never_leaves_the_evidence_root(tmp_path: Path):
    root = tmp_path / "evidence"
    (root / "26").mkdir(parents=True)
    outside = tmp_path / f"{'f' * 64}.gz"
    outside.write_bytes(b"x")
    inside = root / "26" / f"{'f' * 64}.gz"
    inside.write_bytes(b"x")
    rel = f"../{outside.name}"
    assert evidence.unlink_raw([rel, f"26/../../{outside.name}"], root=root) == 0
    assert outside.is_file()
    assert evidence.raw_size(m.Evidence(raw_path=rel), root=root) == 0
    assert evidence.unlink_raw([f"26/{inside.name}"], root=root) == 1
    assert not inside.exists()


async def test_r1_tmp_sweep_leaves_a_symlink_alone(session, dirs, tmp_path):
    root, _ = dirs
    (root / "26").mkdir()
    target = tmp_path / "kept.txt"
    target.write_bytes(b"x")
    link = root / "26" / f"{'a' * 64}.gz.1.ab.tmp"
    link.symlink_to(target)
    backdate(link)
    assert (await run(dirs))["R1"]["tmp"] == 0
    assert link.is_symlink() and target.is_file()


async def test_r1_keeps_the_directory_under_evidence_max_gb(session, dirs, monkeypatch):
    root, _ = dirs
    bodies = [os.urandom(4000) for _ in range(3)]
    rows = [
        await stored(
            session, root, body, f"https://namuna.example.uz/big/{n}", excerpt="katta"
        )
        for n, body in enumerate(bodies)
    ]
    sizes = [(root / str(r.raw_path)).stat().st_size for r in rows]
    for n, row in enumerate(rows):  # fetched oldest first
        await session.execute(
            sa.text(
                f"UPDATE evidence SET fetched_at = now() - interval '{10 - n} days' "
                "WHERE id = :i"
            ),
            {"i": row.id},
        )
    await session.commit()
    cap = sum(sizes) - 1  # one file too many
    monkeypatch.setattr(settings, "evidence_max_gb", cap / retention.GB)
    report = await run(dirs)
    assert (report["R1"]["over_cap_rows"], report["R1"]["over_cap_files"]) == (1, 1)
    kept = [await fresh(session, m.Evidence, r.id) for r in rows]
    assert [k.raw_path is None for k in kept] == [True, False, False]
    flag = await fresh(session, m.RuntimeFlag, "problem:evidence_over_cap")
    assert flag is not None
    assert flag.value["detail"] == {"gb": round(sum(sizes) / retention.GB, 1)}
    # Within the cap the next run clears it.
    await run(dirs)
    assert await fresh(session, m.RuntimeFlag, "problem:evidence_over_cap") is None
    # A cap of 0 is no cap.
    monkeypatch.setattr(settings, "evidence_max_gb", 0.0)
    assert (await run(dirs))["R1"]["over_cap_rows"] == 0


# --- R2a / R2b Telegram posts ---------------------------------------------------------

_MSG = iter(range(1000, 10_000))


async def post(
    session,
    root: Path,
    *,
    days: int,
    text: str = "Xitoydan 5 kub mato kerak, +998 90 000 00 41",
    observation: str | None = None,
    signal: bool = False,
) -> tuple[m.Interaction, m.Evidence, m.Observation | None]:
    """A stored prefilter hit, its evidence (the post's text) and, when asked,
    its observation in ``observation`` state."""
    n = next(_MSG)
    ev = await stored(
        session,
        root,
        f"{text} #{n}".encode(),
        f"https://t.me/namuna_optom/{n}",
        source="telegram_monitor",
    )
    obs = None
    if observation is not None:
        obs = m.Observation(
            evidence_id=ev.id,
            source_key="telegram_monitor",
            status=observation,
            parsed={"kind": "intent_post", "phones": [{"e164": phone(41)}]},
        )
        session.add(obs)
        await session.flush()
    if signal:
        company = await gate_fixtures.firm(session)
        session.add(
            m.Signal(
                company_id=company.id,
                evidence_id=ev.id,
                key="vol_stated_3plus",
                observed_at=now(),
                source_key="telegram_monitor",
            )
        )
    row = m.Interaction(
        tg_chat_id=-100500,
        tg_message_id=n,
        sender_id=5100000041,
        sender_username="namuna_poster",
        sender_name="Rustam",
        sender_bio="Namuna Tekstil direktori",
        text=text,
        occurred_at=now(),
        prefilter_hit=["volume_kub"],
        evidence_id=ev.id,
        observation_id=obs.id if obs is not None else None,
    )
    session.add(row)
    await session.commit()
    await age(session, "interactions", "occurred_at", row.id, days)
    await session.commit()
    return row, ev, obs


async def test_r2a_deletes_an_unconverted_post_and_its_evidence_at_31_days(session, dirs):
    root, _ = dirs
    plain, plain_ev, _ = await post(session, root, days=31)
    rejected, rejected_ev, rejected_obs = await post(
        session, root, days=31, observation="rejected"
    )
    converted, converted_ev, _ = await post(
        session, root, days=31, observation="resolved"
    )
    young, young_ev, _ = await post(session, root, days=29)
    cited, cited_ev, _ = await post(session, root, days=31, signal=True)
    # A rejected post whose page another, live observation still reads.
    read, read_ev, _ = await post(session, root, days=31, observation="rejected")
    live = m.Observation(
        evidence_id=read_ev.id,
        ordinal=1,
        source_key="telegram_monitor",
        status="resolved",
        parsed={"kind": "intent_post"},
    )
    session.add(live)
    await session.commit()
    files = {
        r.id: root / str(r.raw_path) for r in (plain_ev, rejected_ev, cited_ev, read_ev)
    }
    report = await run(dirs)
    assert report["R2a"] == {"interactions": 4, "evidence": 2, "files": 2}
    assert await fresh(session, m.Evidence, read_ev.id) is not None
    assert await fresh(session, m.Observation, live.id) is not None
    assert files[read_ev.id].is_file()
    for row in (plain, rejected, cited, read):
        assert await fresh(session, m.Interaction, row.id) is None
    for row in (converted, young):
        assert await fresh(session, m.Interaction, row.id) is not None
    # The post's own evidence goes with it — its excerpt is the post's text —
    # and the rejected observation with that.
    assert await fresh(session, m.Evidence, plain_ev.id) is None
    assert await fresh(session, m.Evidence, rejected_ev.id) is None
    assert rejected_obs is not None
    assert await fresh(session, m.Observation, rejected_obs.id) is None
    assert not files[plain_ev.id].exists() and not files[rejected_ev.id].exists()
    # A signal still cites this one: row and file stay.
    assert await fresh(session, m.Evidence, cited_ev.id) is not None
    assert files[cited_ev.id].is_file()
    for kept in (converted_ev, young_ev):
        assert await fresh(session, m.Evidence, kept.id) is not None


async def test_r2a_keeps_evidence_a_younger_message_still_cites(session, dirs):
    """The same post read twice (a forward, a re-scan): the old row goes at 31
    days, the page stays while the young one still cites it."""
    root, _ = dirs
    old, ev, _ = await post(session, root, days=31)
    again = m.Interaction(
        tg_chat_id=-100501,
        tg_message_id=next(_MSG),
        sender_id=5100000041,
        text=old.text,
        occurred_at=now(),
        prefilter_hit=["volume_kub"],
        evidence_id=ev.id,
    )
    session.add(again)
    await session.commit()
    report = await run(dirs)
    assert report["R2a"] == {"interactions": 1, "evidence": 0, "files": 0}
    assert await fresh(session, m.Interaction, old.id) is None
    assert await fresh(session, m.Interaction, again.id) is not None
    assert await fresh(session, m.Evidence, ev.id) is not None
    assert (root / str(ev.raw_path)).is_file()


async def test_r2b_drops_a_converted_post_s_text_at_91_days(session, dirs):
    root, _ = dirs
    old, _, _ = await post(session, root, days=91, observation="resolved")
    recent, _, _ = await post(session, root, days=89, observation="resolved")
    report = await run(dirs)
    assert report["R2b"] == {"interactions": 1}
    old, recent = (
        await fresh(session, m.Interaction, old.id),
        await fresh(session, m.Interaction, recent.id),
    )
    assert (old.text, old.sender_bio, old.sender_name) == (None, None, None)
    assert old.text_purged_at is not None
    # The ids stay.
    assert (old.sender_id, old.sender_username, old.tg_message_id) == (
        5100000041,
        "namuna_poster",
        old.tg_message_id,
    )
    assert recent.text is not None and recent.text_purged_at is None


# --- R3 observation scrub -------------------------------------------------------------


async def test_r3_strips_an_intent_post_s_handles_and_keeps_a_directory_entry_s(
    session, dirs
):
    root, _ = dirs
    ev = await stored(session, root, b"sahifa", "https://namuna.example.uz/r3")
    shapes = {
        "intent_post": {
            "kind": "intent_post",
            "phones": [{"e164": phone(42)}],
            "person": {"name": "Rustam"},
            "handles": {"telegram": "namuna_poster", "tg_user_id": 5100000042},
            "company": {"name_raw": "Namuna Tekstil Savdo MChJ"},
        },
        "directory_entry": {
            "kind": "directory_entry",
            "phones": [{"e164": phone(43)}],
            "person": {"name": "Sardor"},
            "handles": {"telegram": "namuna_kanal", "instagram": "namuna.uz"},
        },
    }
    rows = {}
    for n, (kind, parsed) in enumerate(shapes.items()):
        rows[kind] = m.Observation(
            evidence_id=ev.id,
            ordinal=n,
            source_key="directories",
            status="resolved",
            parsed=parsed,
            resolved_at=now(),
        )
        session.add(rows[kind])
    young = m.Observation(
        evidence_id=ev.id,
        ordinal=5,
        source_key="directories",
        status="resolved",
        parsed=dict(shapes["intent_post"]),
        resolved_at=now(),
    )
    session.add(young)
    await session.commit()
    for row in rows.values():
        await age(session, "observations", "resolved_at", row.id, 31)
    await age(session, "observations", "resolved_at", young.id, 29)
    await session.commit()
    report = await run(dirs)
    assert report["R3"] == {"observations": 2}
    post_ = await fresh(session, m.Observation, rows["intent_post"].id)
    entry = await fresh(session, m.Observation, rows["directory_entry"].id)
    assert post_.parsed == {
        "kind": "intent_post",
        "handles": {},
        "company": {"name_raw": "Namuna Tekstil Savdo MChJ"},
        "scrubbed": True,
    }
    assert entry.parsed == {
        "kind": "directory_entry",
        "handles": {"telegram": "namuna_kanal", "instagram": "namuna.uz"},
        "scrubbed": True,
    }
    assert post_.scrubbed_at is not None and entry.scrubbed_at is not None
    left = await fresh(session, m.Observation, young.id)
    assert left.scrubbed_at is None and "phones" in left.parsed


# --- R4b Places phones ----------------------------------------------------------------


async def places_contact(session, company: m.Company, nn: int, *, days: int) -> m.Contact:
    row = await gate_fixtures.contact(
        session,
        company,
        phone(nn),
        source_key="maps_google",
        source_url="https://www.google.com/maps/place/?q=place_id:ChIJnamuna",
        collected_at=now(),
    )
    await session.commit()
    await age(session, "contacts", "collected_at", row.id, days)
    await session.commit()
    return row


async def test_r4b_nulls_a_places_phone_at_31_days_unless_a_push_used_it(session, dirs):
    plain = await gate_fixtures.firm(session)
    pushed = await gate_fixtures.firm(session)
    failed = await gate_fixtures.firm(session)
    vague = await gate_fixtures.firm(session)
    young = await gate_fixtures.firm(session)
    source = await gate_fixtures.firm(session)
    # 'unknown' counts as used: the ERP may hold the lead (02 §2.10).
    for company, status in ((pushed, "sent"), (failed, "failed"), (vague, "unknown")):
        cand = await gate_fixtures.candidate(session, company, state="pushed")
        await gate_fixtures.push(
            session, cand, sent_at=now(), phone_tail=tail(51), status=status
        )
    # A push made for a company before it was merged into its root: the
    # root's lineage was pushed (push_log is never re-pointed).
    merged = await gate_fixtures.firm(session, state="merged", merged_into=source.id)
    session.add(
        m.MergeLog(
            source_company_id=merged.id,
            target_company_id=source.id,
            decided_by="owner:1",
            moved={},
        )
    )
    cand = await gate_fixtures.candidate(session, merged, state="pushed")
    await gate_fixtures.push(session, cand, sent_at=now(), phone_tail=tail(55))
    await session.commit()
    rows = {
        "plain": await places_contact(session, plain, 51, days=31),
        "pushed": await places_contact(session, pushed, 52, days=31),
        "failed": await places_contact(session, failed, 53, days=31),
        "unknown": await places_contact(session, vague, 57, days=31),
        "young": await places_contact(session, young, 54, days=29),
        "lineage": await places_contact(session, source, 55, days=31),
    }
    # Another source's phone is not Places': it stays.
    other = await gate_fixtures.contact(session, plain, phone(56), collected_at=now())
    await session.commit()
    await age(session, "contacts", "collected_at", other.id, 31)
    await session.commit()
    report = await run(dirs)
    assert report["R4b"] == {"contacts": 2, "kept": 3}
    after = {k: await fresh(session, m.Contact, r.id) for k, r in rows.items()}
    assert {k: (c.phone, c.phone_kind) for k, c in after.items()} == {
        "plain": (None, None),
        "pushed": (phone(52), "mobile"),
        "failed": (None, None),
        "unknown": (phone(57), "mobile"),
        "young": (phone(54), "mobile"),
        "lineage": (phone(55), "mobile"),
    }
    assert (await fresh(session, m.Contact, other.id)).phone == phone(56)


# --- R4 unanswered leads, R5 excluded persons ------------------------------------------


async def person(session, company: m.Company, nn: int) -> tuple[m.Contact, m.Contact]:
    board = await gate_fixtures.contact(session, company, f"+9987112345{nn:02d}")
    someone = await gate_fixtures.contact(
        session,
        company,
        phone(nn),
        tier="person",
        full_name="Rustam Namunaev",
        role="direktor",
        telegram_username=f"namuna_{nn}",
        telegram_id=5100000000 + nn,
    )
    return board, someone


async def pushed_lead(session, *, days: int, label: str | None, nn: int):
    company = await gate_fixtures.firm(session)
    board, someone = await person(session, company, nn)
    cand = await gate_fixtures.candidate(session, company, state="pushed", lead=someone)
    if label is not None:
        session.add(
            m.Outcome(
                external_id=cand.external_id,
                intake_id=uuid.uuid4(),
                candidate_id=cand.id,
                company_id=company.id,
                landing="created",
                erp_at=now(),
                label=label,
                precision_class="pending",
                raw={},
            )
        )
    await session.commit()
    await age(session, "lead_candidates", "pushed_at", cand.id, days)
    await session.commit()
    return company, board, someone


async def test_r4_anonymises_the_person_at_91_days_after_an_untouched_push(session, dirs):
    untouched = await pushed_lead(session, days=91, label="open_untouched", nn=61)
    silent = await pushed_lead(session, days=91, label=None, nn=62)
    contacted = await pushed_lead(session, days=91, label="contacted_open", nn=63)
    recent = await pushed_lead(session, days=89, label="open_untouched", nn=64)
    reopened = await pushed_lead(session, days=91, label="stale_open", nn=65)
    await gate_fixtures.candidate(session, reopened[0], state="review", score=70)
    await session.commit()
    report = await run(dirs)
    assert report["R4"] == {"companies": 2, "contacts": 2}
    for _, board, someone in (untouched, silent):
        gone = await fresh(session, m.Contact, someone.id)
        assert (gone.full_name, gone.role, gone.telegram_id, gone.telegram_username) == (
            None,
            None,
            None,
            None,
        )
        assert (gone.phone, gone.anonymised_at is not None) == (None, True)
        # The company-tier row stays as it was.
        assert (await fresh(session, m.Contact, board.id)).phone == board.phone
    for _, _, someone in (contacted, recent, reopened):
        kept = await fresh(session, m.Contact, someone.id)
        assert (kept.full_name, kept.anonymised_at) == ("Rustam Namunaev", None)


async def test_r4_the_company_s_latest_push_decides(session, dirs):
    """An untouched push 100 days old is not the company's last word when a
    later one was answered, or is still young."""
    answered = await pushed_lead(session, days=100, label="open_untouched", nn=66)
    young = await pushed_lead(session, days=100, label=None, nn=67)
    for (company, _, _), days, label in (
        (answered, 95, "contacted_open"),
        (young, 10, "open_untouched"),
    ):
        later = await gate_fixtures.candidate(
            session, company, state="pushed", external_id=f"cs:{company.id}:202601"
        )
        session.add(
            m.Outcome(
                external_id=str(later.external_id),
                intake_id=uuid.uuid4(),
                candidate_id=later.id,
                company_id=company.id,
                landing="created",
                erp_at=now(),
                label=label,
                precision_class="pending",
                raw={},
            )
        )
        await session.commit()
        await age(session, "lead_candidates", "pushed_at", later.id, days)
        await session.commit()
    report = await run(dirs)
    assert report["R4"] == {"companies": 0, "contacts": 0}
    for _, _, someone in (answered, young):
        kept = await fresh(session, m.Contact, someone.id)
        assert (kept.full_name, kept.anonymised_at) == ("Rustam Namunaev", None)


async def ended_lead(session, *, state: str, days: int, nn: int, **fields: Any):
    company = await gate_fixtures.firm(session)
    _, someone = await person(session, company, nn)
    if state == "rejected":
        fields["rejected_at"] = ago(days)
    await gate_fixtures.candidate(
        session, company, state=state, updated_at=ago(days), **fields
    )
    await session.commit()
    return company, someone


async def test_r5_anonymises_an_excluded_company_s_person_at_30_days(session, dirs):
    rejected = await ended_lead(session, state="rejected", days=31, nn=71)
    excluded = await ended_lead(
        session, state="excluded", days=31, nn=72, exclude_reason="b2c"
    )
    merged = await ended_lead(
        session, state="excluded", days=31, nn=73, exclude_reason="merged"
    )
    young = await ended_lead(session, state="rejected", days=29, nn=74)
    flagged = await gate_fixtures.firm(
        session, state="excluded", exclude_reason="competitor", updated_at=ago(31)
    )
    _, flagged_person = await person(session, flagged, 75)
    fresh_flag = await gate_fixtures.firm(
        session, state="excluded", exclude_reason="competitor", updated_at=ago(29)
    )
    _, fresh_flag_person = await person(session, fresh_flag, 76)
    await session.commit()
    report = await run(dirs)
    assert report["R5"] == {"companies": 3, "contacts": 3}
    for _, someone in (rejected, excluded, (flagged, flagged_person)):
        assert (await fresh(session, m.Contact, someone.id)).anonymised_at is not None
    for _, someone in (merged, young, (fresh_flag, fresh_flag_person)):
        assert (await fresh(session, m.Contact, someone.id)).anonymised_at is None


# --- R6 ledgers, R6b, R7 --------------------------------------------------------------


async def test_r6_ledgers_and_the_hold_sweep_that_keeps_push_history(session, dirs):
    company = await gate_fixtures.firm(session)
    pushed = await gate_fixtures.candidate(session, company, state="pushed")
    old_push = await gate_fixtures.push(
        session, pushed, sent_at=now(), phone_tail=tail(81)
    )
    new_push = await gate_fixtures.push(
        session,
        pushed,
        sent_at=now(),
        phone_tail=tail(81),
        attempt=2,
        status="failed",
        external_id=old_push.external_id,
    )
    for row in (old_push, new_push):
        row.payload = {"phone": phone(81)}
    hold_free = await gate_fixtures.candidate(
        session, await gate_fixtures.firm(session), state="hold", updated_at=ago(366)
    )
    hold_pushed = await gate_fixtures.candidate(
        session, await gate_fixtures.firm(session), state="hold", updated_at=ago(366)
    )
    hold_young = await gate_fixtures.candidate(
        session, await gate_fixtures.firm(session), state="hold", updated_at=ago(364)
    )
    await gate_fixtures.push(
        session,
        hold_pushed,
        sent_at=now(),
        phone_tail=tail(82),
        status="failed",
        attempt=3,
    )

    def job(status: str, **kw: Any) -> m.ModelJob:
        return m.ModelJob(
            kind="classify",
            lane=kw.pop("lane", "instant"),
            model="m",
            custom_id=f"cs-{uuid.uuid4()}",
            status=status,
            request={"messages": "<PHONE_1>"},
            pseudonyms={"<PHONE_1>": phone(83)},
            **kw,
        )

    jobs = {
        "applied": job("applied", applied_at=now()),
        "pending": job("pending"),
        "fallback": job("fallback", lane="batch"),
        "young": job("applied", applied_at=now()),
    }
    session.add_all(jobs.values())
    runs = [m.CollectorRun(source_key="directories", status="ok") for _ in range(2)]
    session.add_all(runs)
    reminders = [m.ReminderLog(kind="digest", ref="d") for _ in range(2)]
    session.add_all(reminders)
    cooldowns = [
        await gate_fixtures.row(
            session, "cooldown", "phone_tail", tail(n), expires_at=now()
        )
        for n in (84, 85)
    ]
    lasting = await gate_fixtures.row(session, "dnc", "phone_tail", tail(86))
    await session.commit()
    await age(session, "push_log", "sent_at", old_push.id, 91)
    await age(session, "push_log", "sent_at", new_push.id, 89)
    await age(session, "model_jobs", "applied_at", jobs["applied"].id, 31)
    await age(session, "model_jobs", "created_at", jobs["pending"].id, 31)
    await age(session, "model_jobs", "created_at", jobs["fallback"].id, 31)
    await age(session, "model_jobs", "applied_at", jobs["young"].id, 29)
    await age(session, "collector_runs", "started_at", runs[0].id, 181)
    await age(session, "reminder_log", "sent_at", reminders[0].id, 91)
    await age(session, "suppression", "expires_at", cooldowns[0].id, 31)
    await age(session, "suppression", "expires_at", cooldowns[1].id, 29)
    await session.commit()
    report = await run(dirs)
    assert report["R6"] == {
        "push_log": 1,
        "model_jobs": 2,
        "collector_runs": 1,
        "reminder_log": 1,
        "lead_candidates": 1,
        "suppression": 1,
    }
    assert (await fresh(session, m.PushLog, old_push.id)).payload is None
    assert (await fresh(session, m.PushLog, new_push.id)).payload is not None
    after = {k: await fresh(session, m.ModelJob, j.id) for k, j in jobs.items()}
    assert {k: (j.request is None, j.pseudonyms is None) for k, j in after.items()} == {
        "applied": (True, True),
        "pending": (False, False),
        "fallback": (True, True),
        "young": (False, False),
    }
    # An untaken fallback whose request went is given up, not left in flight.
    assert after["fallback"].applied_at is not None
    assert after["pending"].status == "pending"
    assert await fresh(session, m.CollectorRun, runs[0].id) is None
    assert await fresh(session, m.CollectorRun, runs[1].id) is not None
    assert await fresh(session, m.ReminderLog, reminders[0].id) is None
    assert await fresh(session, m.LeadCandidate, hold_free.id) is None
    assert await fresh(session, m.LeadCandidate, hold_pushed.id) is not None
    assert await fresh(session, m.LeadCandidate, hold_young.id) is not None
    assert await fresh(session, m.Suppression, cooldowns[0].id) is None
    assert await fresh(session, m.Suppression, cooldowns[1].id) is not None
    assert await fresh(session, m.Suppression, lasting.id) is not None


async def test_r6b_sweeps_client_exports_older_than_a_day(session, dirs):
    _, imports = dirs
    old, today = imports / "clients-old.xlsx", imports / "clients.xlsx"
    old.write_bytes(b"PK")
    today.write_bytes(b"PK")
    (imports / "nested").mkdir()
    stamp = (now() - timedelta(days=2)).timestamp()
    os.utime(old, (stamp, stamp))
    report = await run(dirs)
    assert report["R6b"] == {"files": 1}
    assert not old.exists() and today.exists() and (imports / "nested").is_dir()


async def test_r6b_leaves_a_symlink_alone(session, dirs, tmp_path):
    _, imports = dirs
    target = tmp_path / "elsewhere.xlsx"
    target.write_bytes(b"PK")
    link = imports / "clients-link.xlsx"
    link.symlink_to(target)
    for path in (target, link):
        backdate(path, hours=72)
    assert (await run(dirs))["R6b"] == {"files": 0}
    assert link.is_symlink() and target.is_file()


def test_the_subject_hash_is_the_tashkent_day():
    """04:15 Tashkent is 23:15 UTC the day before: the hash names the
    Tashkent date whatever zone the clock is given in."""
    at = datetime(2026, 10, 28, 23, 15, tzinfo=ZoneInfo("UTC"))
    expected = hashlib.sha256(b"retention:R1:2026-10-29").hexdigest()
    assert retention.subject_hash("R1", at) == expected


def test_r6b_reads_tools_import_clients_folder():
    from clientscrapper.tools import import_clients

    assert retention.IMPORTS_DIR is import_clients.IMPORTS_DIR


async def test_r7_tables_are_never_touched(session, dirs):
    """02 §4 R7: suppression (non-cooldown), outcomes, purge_log, merge_log,
    usage_log — however old."""
    company = await gate_fixtures.firm(session)
    target = await gate_fixtures.firm(session)
    cand = await gate_fixtures.candidate(session, company, state="pushed")
    session.add(
        m.Outcome(
            external_id=cand.external_id,
            intake_id=uuid.uuid4(),
            landing="created",
            erp_at=now(),
            label="won",
            precision_class="positive",
            raw={},
        )
    )
    session.add(
        m.MergeLog(
            source_company_id=uuid.uuid4(),
            target_company_id=target.id,
            decided_by="owner:1",
            moved={},
        )
    )
    session.add(m.UsageLog(provider="anthropic", operation="classify", cost_usd=0))
    session.add(
        m.PurgeLog(
            subject_kind="company",
            subject_hash="0" * 64,
            reason="owner",
            requested_by="owner:1",
            plan={},
        )
    )
    for kind, key_type, key in (
        ("legal", "phone_tail", tail(91)),
        ("dnc", "tg_username", "namuna_91"),
        ("b2c", "company_id", str(company.id)),
    ):
        await gate_fixtures.row(session, kind, key_type, key)
    await session.commit()
    for table, column in (
        ("suppression", "created_at"),
        ("merge_log", "created_at"),
        ("usage_log", "created_at"),
        ("purge_log", "planned_at"),
    ):
        await session.execute(
            sa.text(f"UPDATE {table} SET {column} = now() - interval '4000 days'")
        )
    await session.execute(
        sa.text("UPDATE outcomes SET first_read_at = now() - interval '4000 days'")
    )
    await session.commit()
    before = [
        await count(session, t)
        for t in (m.Suppression, m.Outcome, m.MergeLog, m.UsageLog)
    ]
    purge_rows = await count(session, m.PurgeLog)
    await run(dirs)
    assert (
        [
            await count(session, t)
            for t in (m.Suppression, m.Outcome, m.MergeLog, m.UsageLog)
        ]
        == before
        == [3, 1, 1, 1]
    )
    assert await count(session, m.PurgeLog) == purge_rows + len(STEPS)


def test_the_cap_problem_is_one_health_reports():
    assert retention.CAP_PROBLEM in health.REPORTED_KEYS


async def test_the_job_runs_on_its_own_sessions(session, dirs):
    """Each step opens its own session (its own transaction): the default is
    the engine's SessionLocal."""
    report = await retention.retention_job(
        root=dirs[0], imports_dir=dirs[1], session_factory=SessionLocal
    )
    assert set(report) == set(STEPS)
