"""Retention (02 §4; 06 §3.5, §5.4): ``retention_job``, daily 04:15 Tashkent.

Each step runs in its own transaction and writes its own ``purge_log`` row —
``subject_kind='retention'``, the sha256 of ``retention:<step>:<date>``
(02 §2.14), ``reason='retention'``, ``requested_by='job:retention'``, what
was due in ``plan`` and what was done in ``executed`` (counts only, never a
name or a phone). A step that fails is rolled back alone and leaves its row
with the error's class; the job goes on with the next step and raises
``RetentionFailed`` at the end, so ``job:retention`` records the failure
(06 §4.2 ``job_failing``). Database ages are the database's clock
(``now()``, as 02 §4 writes them); a file's age is the job's clock.

* **R1** raw evidence 180 d (30 d for Places): the file goes, the row stays —
  ``evidence.purge_raw``, which unlinks only when no other row holds the same
  file. Then the orphan ``.tmp`` a crash mid-write left (02 §3), the stored
  files no row names any more (``evidence.sweep_orphans``: a purge or R2a
  stopped between its commit and its unlink), and the cap of 06 §3.5: while
  the directory is over ``EVIDENCE_MAX_GB``, the oldest files go early (rows
  stay) and ``evidence_over_cap`` is raised; it clears on the first run that
  did not have to (a directory over the cap with no file left to take stays
  reported).
* **R2a** unconverted Telegram posts 30 d: deleted — and the post's own
  evidence row with them, whose excerpt is the post's text: an unconverted or
  rejected post is gone at 30 days, not only its ``interactions`` row
  (docs/huquqiy-memo.md: «Intent posts not converted … delete in 30 days»).
  An evidence row another observation, a signal or a kept message still
  cites stays.
* **R2b** converted posts 90 d: ids kept, text, bio and name dropped.
* **R3** observation scrub 30 d after resolution: ``phones``/``person`` out of
  ``parsed`` — and an intent post's poster's own ``handles.telegram`` and
  ``tg_user_id``; a directory entry's company handle stays.
* **R4b** Places phones 30 d (Maps terms): a ``maps_google`` contact's phone
  is cleared unless a delivered push (``sent``/``unknown``) of the company's
  lineage exists — the ERP already holds that lead. Over the lineage, not
  the row's company alone: ``push_log`` is never re-pointed by a merge
  (04 §2.5), so a push made before the merge is the root's too.
* **R4** unanswered leads 90 d: the company's latest push older than 90 days
  whose outcome is untouched (or none came) and no open candidate →
  ``contacts.anonymise`` on its person-tier contacts; company-tier rows stay.
* **R5** excluded persons 30 d: a company whose latest candidate was
  excluded or rejected 30 days ago (a merge's ``excluded(merged)`` is no
  decision about the firm and is left alone), or a company ``excluded`` for
  30 days → its person-tier contacts anonymised.
* **R6** ledgers: push payloads 90 d, model requests 30 d after their end,
  runs 180 d, reminders 90 d, the 365-day hold sweep (``NOT EXISTS`` push
  history: ``push_log`` keeps its candidate), cooldowns 30 d after expiry.
* **R6b** client exports 1 d: files in ``data/imports/`` (the ERP's client
  export is every client's name and phone).
* **R7** never: ``suppression`` (non-cooldown), ``outcomes``, ``purge_log``,
  ``merge_log``, ``usage_log`` — no statement here touches them.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db import enums as e
from clientscrapper.db.models import Contact, Evidence, Outcome, PurgeLog
from clientscrapper.db.models import LeadCandidate as Candidate
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import companies, evidence, health
from clientscrapper.services.contacts import anonymise
from clientscrapper.tools.import_clients import IMPORTS_DIR

log = logging.getLogger(__name__)

REQUESTED_BY: Final = "job:retention"
REASON: Final = "retention"
SUBJECT: Final = "retention"
# Who raises the evidence cap problem (runtime_flags.updated_by).
WRITER: Final = "worker:retention"
CAP_PROBLEM: Final = "evidence_over_cap"
# R1: «LIMIT 5000» per run (02 §4); the cap walks the oldest files in pages.
R1_LIMIT: Final = 5000
CAP_PAGE: Final = 200
# health reads sizes in decimal gigabytes (its _GB): the cap is the same unit.
GB: Final = 1000**3
UNANSWERED_AFTER: Final = timedelta(days=90)
EXCLUDED_AFTER: Final = timedelta(days=30)
# R4: «outcome label IN (…) or no outcome row» (02 §4).
UNTOUCHED_LABELS: Final = frozenset(
    {"open_untouched", "stale_open", "dropped_capped", "dropped_other"}
)
# R6b: the ERP's client export is not kept past a day (02 §4). The folder is
# tools/import_clients' (where make puts the export).
IMPORT_KEPT: Final = timedelta(days=1)

# --- 02 §4's statements ------------------------------------------------------------

# R2a: the delete, as 02 §4 writes it; the RETURNING names the posts'
# evidence for the second half (the post's own evidence row).
R2A_DELETE: Final = (
    "DELETE FROM interactions WHERE occurred_at < now() - interval '30 days' AND "
    "(observation_id IS NULL OR observation_id IN (SELECT id FROM observations "
    "WHERE status = 'rejected'))"
)
# The deleted posts' evidence that nothing else cites any more: no observation
# that was not rejected, no signal, no message still kept.
R2A_EVIDENCE: Final = (
    "SELECT e.id FROM evidence e WHERE e.id = ANY(:ids) "
    "AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.evidence_id = e.id "
    "AND o.status <> 'rejected') "
    "AND NOT EXISTS (SELECT 1 FROM signals s WHERE s.evidence_id = e.id) "
    "AND NOT EXISTS (SELECT 1 FROM interactions i WHERE i.evidence_id = e.id)"
)
R2B_UPDATE: Final = (
    "UPDATE interactions SET text = NULL, sender_bio = NULL, sender_name = NULL, "
    "text_purged_at = now() WHERE occurred_at < now() - interval '90 days' "
    "AND text IS NOT NULL"
)
R3_SCRUB: Final = (
    "UPDATE observations SET parsed = (parsed - 'phones' - 'person') || CASE WHEN "
    "parsed->>'kind' = 'intent_post' THEN jsonb_build_object('handles', "
    "(parsed->'handles') - 'tg_user_id' - 'telegram') ELSE '{}'::jsonb END || "
    "jsonb_build_object('scrubbed', true), scrubbed_at = now() WHERE scrubbed_at IS NULL "
    "AND coalesce(resolved_at, created_at) < now() - interval '30 days'"
)
# R4b's due rows; whether a push of the lineage holds the phone is decided over
# companies.lineages (the module docstring).
R4B_DUE: Final = (
    "SELECT c.id, c.company_id FROM contacts c WHERE c.source_key = 'maps_google' "
    "AND c.phone IS NOT NULL AND c.collected_at < now() - interval '30 days'"
)
# R5: the companies whose people go — their latest candidate excluded or
# rejected 30 days ago, or the company excluded for 30 days.
R5_COMPANIES: Final = (
    "WITH last AS (SELECT DISTINCT ON (company_id) company_id, state, exclude_reason, "
    "coalesce(rejected_at, updated_at) AS since FROM lead_candidates "
    "ORDER BY company_id, created_at DESC, id DESC) "
    "SELECT company_id FROM last WHERE state IN ('excluded', 'rejected') "
    "AND exclude_reason IS DISTINCT FROM 'merged' "
    "AND since < now() - interval '30 days' "
    "UNION SELECT id FROM companies WHERE state = 'excluded' "
    "AND updated_at < now() - interval '30 days'"
)
# R6, statement by statement (02 §4). The payload and the request lines only
# touch rows that still hold something, so the counts say what changed. An
# untaken fallback (status 'fallback', applied_at NULL) whose request goes can
# never be taken (model_jobs claims only a request that is there): it is given
# up in the same statement, as model_jobs does after its last try, or the
# verdict lane would read it as in flight for ever.
R6_PUSH_LOG: Final = (
    "UPDATE push_log SET payload = NULL WHERE sent_at < now() - interval '90 days' "
    "AND payload IS NOT NULL"
)
R6_MODEL_JOBS: Final = (
    "UPDATE model_jobs SET request = NULL, pseudonyms = NULL, applied_at = CASE WHEN "
    "status = 'fallback' THEN coalesce(applied_at, now()) ELSE applied_at END "
    "WHERE status IN ('applied', 'failed', 'fallback') "
    "AND coalesce(applied_at, created_at) < now() - interval '30 days' "
    "AND (request IS NOT NULL OR pseudonyms IS NOT NULL)"
)
R6_RUNS: Final = (
    "DELETE FROM collector_runs WHERE started_at < now() - interval '180 days'"
)
R6_REMINDERS: Final = (
    "DELETE FROM reminder_log WHERE sent_at < now() - interval '90 days'"
)
# tests/test_constraints.py::R6_HOLD_SWEEP pins this text.
R6_HOLD_SWEEP: Final = (
    "DELETE FROM lead_candidates WHERE state = 'hold' "
    "AND updated_at < now() - interval '365 days' "
    "AND NOT EXISTS (SELECT 1 FROM push_log p WHERE p.candidate_id = lead_candidates.id)"
)
R6_COOLDOWNS: Final = (
    "DELETE FROM suppression WHERE kind = 'cooldown' "
    "AND expires_at < now() - interval '30 days'"
)
R6_STATEMENTS: Final = (
    ("push_log", R6_PUSH_LOG),
    ("model_jobs", R6_MODEL_JOBS),
    ("collector_runs", R6_RUNS),
    ("reminder_log", R6_REMINDERS),
    ("lead_candidates", R6_HOLD_SWEEP),
    ("suppression", R6_COOLDOWNS),
)


class RetentionFailed(Exception):
    """One or more steps failed (each rolled back alone, each logged)."""

    def __init__(self, steps: list[str]) -> None:
        super().__init__(f"retention steps failed: {', '.join(steps)}")
        self.steps = steps


@dataclass(frozen=True, slots=True)
class Ctx:
    """What a step needs besides its session."""

    now: datetime
    root: Path
    imports_dir: Path


@dataclass(slots=True)
class Done:
    """A step's counts: ``plan`` what was due, ``executed`` what changed;
    ``files`` the evidence files to unlink once its transaction commits."""

    plan: dict[str, int] = field(default_factory=dict)
    executed: dict[str, int] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)


Step = Callable[[AsyncSession, Ctx], Awaitable[Done]]


def subject_hash(step: str, now: datetime) -> str:
    """sha256 of ``retention:<step>:<date>`` (02 §2.14), the Tashkent date."""
    day = now.astimezone(settings.tz).date().isoformat()
    return hashlib.sha256(f"retention:{step}:{day}".encode()).hexdigest()


async def _rowcount(session: AsyncSession, sql: str, **params: Any) -> int:
    result = await session.execute(sa.text(sql), params)
    return int(getattr(result, "rowcount", 0) or 0)


async def _db_now(session: AsyncSession) -> datetime:
    """The database's clock, which every age in 02 §4 is measured by."""
    found = await session.scalar(sa.select(sa.func.now()))
    assert isinstance(found, datetime)
    return found


# --- R1 ----------------------------------------------------------------------------


def _cap() -> int:
    """EVIDENCE_MAX_GB in bytes; 0 is no cap."""
    return int(settings.evidence_max_gb * GB)


def _over(size: int) -> bool:
    cap = _cap()
    return cap > 0 and size > cap


async def _over_cap(session: AsyncSession, ctx: Ctx) -> tuple[int, int, int]:
    """06 §3.5: while the directory exceeds EVIDENCE_MAX_GB the oldest files go
    (rows stay). Returns (size before, rows taken, files removed); a cap of 0
    is no cap."""
    cap = _cap()
    size = evidence.stored_bytes(root=ctx.root)
    before = size
    rows = files = 0
    if not _over(size):
        return before, rows, files
    while size > cap:
        page = list(
            await session.scalars(
                sa.select(Evidence)
                .where(Evidence.raw_path.is_not(None))
                .order_by(Evidence.fetched_at, Evidence.id)
                .limit(CAP_PAGE)
            )
        )
        if not page:
            break
        for row in page:
            weight = evidence.raw_size(row, root=ctx.root)
            rows += 1
            if await evidence.purge_raw(session, row, now=ctx.now, root=ctx.root):
                files += 1
                size -= weight
            if size <= cap:
                break
    return before, rows, files


async def step_r1(session: AsyncSession, ctx: Ctx) -> Done:
    due = list(
        await session.scalars(
            sa.select(Evidence)
            .where(Evidence.expires_at < sa.func.now(), Evidence.raw_path.is_not(None))
            .order_by(Evidence.expires_at, Evidence.id)
            .limit(R1_LIMIT)
        )
    )
    removed = 0
    for row in due:
        if await evidence.purge_raw(session, row, now=ctx.now, root=ctx.root):
            removed += 1
    tmp = evidence.sweep_tmp(now=ctx.now, root=ctx.root)
    orphans = await evidence.sweep_orphans(session, now=ctx.now, root=ctx.root)
    size, early_rows, early_files = await _over_cap(session, ctx)
    if early_rows:
        await health.raise_problem(
            session,
            CAP_PROBLEM,
            now=ctx.now,
            by=WRITER,
            detail={"gb": round(size / GB, 1)},
        )
    elif not _over(size):
        # Cleared only by a run that did not have to: a directory still over
        # the cap with no row left to take a file from stays reported.
        await health.clear_problem(session, CAP_PROBLEM)
    done = {
        "rows": len(due),
        "files": removed,
        "tmp": tmp,
        "orphans": orphans,
        "over_cap_rows": early_rows,
        "over_cap_files": early_files,
    }
    return Done(plan={"rows": len(due), "tmp": tmp, "orphans": orphans}, executed=done)


# --- R2a, R2b, R3 ---------------------------------------------------------------------


async def step_r2a(session: AsyncSession, ctx: Ctx) -> Done:
    gone = (await session.execute(sa.text(R2A_DELETE + " RETURNING evidence_id"))).all()
    cited = sorted({int(r[0]) for r in gone if r[0] is not None})
    alone = (
        list(await session.scalars(sa.text(R2A_EVIDENCE), {"ids": cited}))
        if cited
        else []
    )
    files = await evidence.drop_rows(session, alone)
    counts = {"interactions": len(gone), "evidence": len(alone), "files": len(files)}
    return Done(plan=dict(counts), executed=counts, files=files)


async def step_r2b(session: AsyncSession, ctx: Ctx) -> Done:
    changed = await _rowcount(session, R2B_UPDATE)
    return Done(plan={"interactions": changed}, executed={"interactions": changed})


async def step_r3(session: AsyncSession, ctx: Ctx) -> Done:
    changed = await _rowcount(session, R3_SCRUB)
    return Done(plan={"observations": changed}, executed={"observations": changed})


# --- R4b, R4, R5 --------------------------------------------------------------------


async def _delivered(session: AsyncSession, members: set[uuid.UUID]) -> set[uuid.UUID]:
    """The companies among ``members`` a delivered push was made for."""
    if not members:
        return set()
    return set(
        await session.scalars(
            sa.text(
                "SELECT DISTINCT company_id FROM push_log "
                "WHERE company_id = ANY(:ids) AND status = ANY(:delivered)"
            ),
            {"ids": sorted(members), "delivered": list(e.PUSH_DELIVERED_STATUSES)},
        )
    )


async def step_r4b(session: AsyncSession, ctx: Ctx) -> Done:
    due = (await session.execute(sa.text(R4B_DUE))).all()
    groups = await companies.lineages(session, sorted({r.company_id for r in due}))
    members = {m for group in groups.values() for m in group}
    delivered = await _delivered(session, members)
    clear = sorted(r.id for r in due if not (groups[r.company_id] & delivered))
    if clear:
        await session.execute(
            sa.update(Contact)
            .where(Contact.id.in_(clear))
            .values(phone=None, phone_kind=None)
            .execution_options(synchronize_session=False)
        )
    return Done(
        plan={"contacts": len(due)},
        executed={"contacts": len(clear), "kept": len(due) - len(clear)},
    )


async def _anonymise_people(
    session: AsyncSession, company_ids: set[uuid.UUID], *, now: datetime
) -> int:
    if not company_ids:
        return 0
    people = list(
        await session.scalars(
            sa.select(Contact)
            .where(
                Contact.company_id.in_(sorted(company_ids)),
                Contact.tier == "person",
                Contact.anonymised_at.is_(None),
            )
            .order_by(Contact.id)
            .with_for_update()
        )
    )
    for person in people:
        anonymise(person, now=now)
    await session.flush()
    return len(people)


async def step_r4(session: AsyncSession, ctx: Ctx) -> Done:
    cutoff = await _db_now(session) - UNANSWERED_AFTER
    pushed = (
        await session.execute(
            sa.select(Candidate.company_id, Candidate.pushed_at, Outcome.label)
            .outerjoin(Outcome, Outcome.external_id == Candidate.external_id)
            .where(Candidate.state == "pushed", Candidate.pushed_at.is_not(None))
        )
    ).all()
    due = {
        r.company_id
        for r in pushed
        if r.pushed_at < cutoff and (r.label is None or r.label in UNTOUCHED_LABELS)
    }
    groups = await companies.lineages(session, sorted(due))
    members = {m for group in groups.values() for m in group}
    open_ = set(
        await session.scalars(
            sa.select(Candidate.company_id).where(
                Candidate.company_id.in_(sorted(members)),
                Candidate.state.in_(e.CANDIDATE_OPEN_STATES),
            )
        )
    )
    targets: set[uuid.UUID] = set()
    for company_id in sorted(due):
        group = groups[company_id]
        if group & open_:
            continue
        latest = max(
            (r for r in pushed if r.company_id in group), key=lambda r: r.pushed_at
        )
        untouched = latest.label is None or latest.label in UNTOUCHED_LABELS
        if latest.pushed_at < cutoff and untouched:
            targets |= group
    people = await _anonymise_people(session, targets, now=ctx.now)
    return Done(
        plan={"companies": len(due)},
        executed={"companies": len(targets), "contacts": people},
    )


async def step_r5(session: AsyncSession, ctx: Ctx) -> Done:
    found = {uuid.UUID(str(c)) for c in await session.scalars(sa.text(R5_COMPANIES))}
    people = await _anonymise_people(session, found, now=ctx.now)
    return Done(
        plan={"companies": len(found)},
        executed={"companies": len(found), "contacts": people},
    )


# --- R6, R6b ------------------------------------------------------------------------


async def step_r6(session: AsyncSession, ctx: Ctx) -> Done:
    counts = {table: await _rowcount(session, sql) for table, sql in R6_STATEMENTS}
    return Done(plan=dict(counts), executed=counts)


def sweep_imports(*, now: datetime, directory: Path) -> int:
    """R6b: every file in ``directory`` whose mtime is older than a day."""
    if not directory.is_dir():
        return 0
    cutoff = now.timestamp() - IMPORT_KEPT.total_seconds()
    removed = 0
    for path in sorted(directory.iterdir()):
        try:
            found = path.lstat()
            if path.is_symlink() or not path.is_file() or found.st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            log.warning("client export not removed", extra={"file": path.name})
    return removed


async def step_r6b(session: AsyncSession, ctx: Ctx) -> Done:
    removed = sweep_imports(now=ctx.now, directory=ctx.imports_dir)
    return Done(plan={"files": removed}, executed={"files": removed})


# 02 §4's order. R7 is a rule about what no step touches, not a step.
STEPS: Final[tuple[tuple[str, Step], ...]] = (
    ("R1", step_r1),
    ("R2a", step_r2a),
    ("R2b", step_r2b),
    ("R3", step_r3),
    ("R4b", step_r4b),
    ("R4", step_r4),
    ("R5", step_r5),
    ("R6", step_r6),
    ("R6b", step_r6b),
)


def _row(step: str, now: datetime, **values: Any) -> PurgeLog:
    return PurgeLog(
        subject_kind=SUBJECT,
        subject_hash=subject_hash(step, now),
        reason=REASON,
        requested_by=REQUESTED_BY,
        **values,
    )


SessionFactory = Callable[[], AsyncSession]


async def run_step(
    name: str, step: Step, ctx: Ctx, *, session_factory: SessionFactory = SessionLocal
) -> Done | None:
    """One step in its own transaction with its own ``purge_log`` row; None
    when it failed (rolled back, the failure logged in a row of its own)."""
    try:
        async with session_factory() as session:
            done = await step(session, ctx)
            session.add(
                _row(
                    name,
                    ctx.now,
                    plan={"step": name, **done.plan},
                    executed=dict(done.executed),
                    executed_at=ctx.now,
                )
            )
            await session.commit()
    except Exception as exc:
        log.warning(
            "retention step failed", extra={"step": name, "error": type(exc).__name__}
        )
        try:
            async with session_factory() as session:
                session.add(
                    _row(name, ctx.now, plan={"step": name}, error=type(exc).__name__)
                )
                await session.commit()
        except Exception as again:
            log.warning(
                "retention failure not logged",
                extra={"step": name, "error": type(again).__name__},
            )
        return None
    evidence.unlink_raw(done.files, root=ctx.root)
    return done


async def retention_job(
    *,
    now: datetime | None = None,
    root: Path | None = None,
    imports_dir: Path | None = None,
    session_factory: SessionFactory = SessionLocal,
) -> dict[str, dict[str, int]]:
    """The worker's ``retention`` slot (02 §4, 04:15 Tashkent): every step in
    02 §4's order, each in its own transaction; the executed counts per step.
    Raises ``RetentionFailed`` after the last step when any failed."""
    ctx = Ctx(
        now=now or datetime.now(settings.tz),
        root=root or evidence.evidence_root(),
        imports_dir=imports_dir or IMPORTS_DIR,
    )
    report: dict[str, dict[str, int]] = {}
    failed: list[str] = []
    for name, step in STEPS:
        done = await run_step(name, step, ctx, session_factory=session_factory)
        if done is None:
            failed.append(name)
        else:
            report[name] = done.executed
    log.info("retention_job", extra={"counts": report, "failed": failed})
    if failed:
        raise RetentionFailed(failed)
    return report


__all__ = [
    "CAP_PROBLEM",
    "IMPORTS_DIR",
    "R2A_DELETE",
    "R2B_UPDATE",
    "R3_SCRUB",
    "R6_HOLD_SWEEP",
    "R6_STATEMENTS",
    "STEPS",
    "Ctx",
    "Done",
    "RetentionFailed",
    "retention_job",
    "run_step",
    "subject_hash",
    "sweep_imports",
]
