"""The owner's own writes from the bot (06 §1.2) that no other service holds.

Each is one short statement under the row it touches, in the caller's
transaction (the bot commits):

* **the global pause** — ``/toxtat`` sets ``runtime_flags('paused')``
  ``{"since", "reason"}``, read by the runner (``runner.global_pause``), the
  push gate (item 2), the reader's joiner and ``/holat``; ``/yoq`` deletes
  it after its confirm;
* **sources** — ``/manbalar``'s page (``source_lines``: the row, today's
  pages and spend, the 30-day precision), ``src:<key>:t`` (``toggle_source``),
  ``src:<key>:i`` (``source_info``: the last runs, the breaker, the pause) and
  ``src:<key>:a`` (``forbid_auto_push``: ``config.auto_push_forbidden=true`` —
  the owner can forbid auto-push, never grant it: ``auto_push`` itself is the
  weekly precision job's, 05 §B.2);
* **the ERP breaker by hand** — ``/yoq crm`` clears ``sources.erp_sync``'s
  breaker whatever tripped it (02 §2.10 rule 2: «/yoq crm clears it by
  hand»); ``erp_feeds.clear_breaker`` is the feed's narrower rule;
* **/stir** — the registry queue's manual answer (04 §3 E4, captcha path):
  the STIR on the candidate's company (refused when another company holds
  it — that company is answered, nothing is merged), an evidence row that
  says the owner gave it, and the candidate's ``next_enrich_at = now`` so
  E4 re-runs.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors.runner import global_pause
from clientscrapper.db.models import (
    CollectorRun,
    Company,
    Evidence,
    LeadCandidate,
    RuntimeFlag,
    Source,
    UsageLog,
)
from clientscrapper.services import companies, costs, erp_feeds, keys

PAUSE_FLAG: Final = "paused"
REASON_MAX: Final = 200
AUTO_PUSH_FORBIDDEN: Final = "auto_push_forbidden"
# 06 §1.2: «last 5 collector_runs».
INFO_RUNS: Final = 5
# 04 §3 E4: the owner's STIR is a registry fact the agent did not fetch.
STIR_SOURCE: Final = "registry_lookup"
STIR_EXCERPT: Final = "owner:/stir"
STIR_EVIDENCE_DAYS: Final = 180


def owner_ref(telegram_id: int) -> str:
    """``owner:<tg_id>`` — the owner's name in every ``*_by`` column."""
    return f"owner:{telegram_id}"


# --- the global pause ------------------------------------------------------------


async def paused(session: AsyncSession) -> bool:
    """The runner's own reading of the flag (one home)."""
    return await global_pause(session)


async def pause(
    session: AsyncSession, *, by: str, reason: str | None, now: datetime
) -> None:
    """``/toxtat [sabab]``: at once, no confirmation (a safety action)."""
    value = {"since": now.isoformat(), "reason": (reason or "")[:REASON_MAX] or None}
    row = await session.get(RuntimeFlag, PAUSE_FLAG, populate_existing=True)
    if row is None:
        session.add(RuntimeFlag(key=PAUSE_FLAG, value=value, updated_by=by))
    else:
        row.value = value
        row.updated_by = by
    await session.flush()


async def resume(session: AsyncSession) -> bool:
    """``/yoq`` after its confirm: the flag goes. True when it was set."""
    result = await session.execute(
        sa.delete(RuntimeFlag)
        .where(RuntimeFlag.key == PAUSE_FLAG)
        .returning(RuntimeFlag.key)
    )
    return result.first() is not None


async def clear_erp_breaker(session: AsyncSession) -> bool:
    """``/yoq crm`` after its confirm: ``sources.erp_sync.breaker_until`` and
    its reason cleared, whatever tripped it. True when one was set."""
    result = await session.execute(
        sa.update(Source)
        .where(Source.key == erp_feeds.ERP_SOURCE, Source.breaker_until.is_not(None))
        .values(breaker_until=None, breaker_reason=None)
        .returning(Source.key)
    )
    return result.first() is not None


async def erp_breaker_until(session: AsyncSession) -> datetime | None:
    found = await session.scalar(
        sa.select(Source.breaker_until).where(Source.key == erp_feeds.ERP_SOURCE)
    )
    return found


# --- sources (/manbalar) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceLine:
    """One ``sources`` row as ``/manbalar`` prints it."""

    key: str
    title: str
    enabled: bool
    schedulable: bool
    last_run_at: datetime | None
    pages_today: int
    page_budget: int
    cost_today: Decimal
    precision: Decimal | None
    pushes_30d: int
    auto_push: bool
    forbidden: bool
    paused_until: datetime | None
    breaker_until: datetime | None

    def state(self, now: datetime) -> Literal["on", "paused", "off"]:
        """✅ on · ⏸ paused or breaker-held · ⛔ off."""
        if not self.enabled:
            return "off"
        held = [t for t in (self.paused_until, self.breaker_until) if t is not None]
        if any(t > now for t in held):
            return "paused"
        return "on"


def forbidden_of(config: Any) -> bool:
    return isinstance(config, dict) and config.get(AUTO_PUSH_FORBIDDEN) is True


async def source_lines(session: AsyncSession, *, now: datetime) -> list[SourceLine]:
    """Every source, schedulable rows (a cron) first, then the
    attribution-only sub-sources (03 §2.1), each by key; today's pages are
    its runs' and today's spend its ``usage_log`` rows' (refusals cost 0)."""
    start, end = costs.day_start(now), costs.day_end(now)
    page_rows = (
        await session.execute(
            sa.select(CollectorRun.source_key, sa.func.sum(CollectorRun.pages))
            .where(CollectorRun.started_at >= start, CollectorRun.started_at < end)
            .group_by(CollectorRun.source_key)
        )
    ).all()
    pages: dict[str, int] = {key: int(total or 0) for key, total in page_rows}
    spend_rows = (
        await session.execute(
            sa.select(UsageLog.source_key, sa.func.sum(UsageLog.cost_usd))
            .where(
                UsageLog.created_at >= start,
                UsageLog.created_at < end,
                UsageLog.source_key.is_not(None),
            )
            .group_by(UsageLog.source_key)
        )
    ).all()
    spent: dict[str | None, Decimal] = {
        key: Decimal(total or 0) for key, total in spend_rows
    }
    rows = (
        await session.scalars(
            sa.select(Source)
            .order_by(Source.cron.is_(None), Source.key)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [
        SourceLine(
            key=row.key,
            title=row.title_uz,
            enabled=row.enabled,
            schedulable=row.cron is not None,
            last_run_at=row.last_run_at,
            pages_today=int(pages.get(row.key) or 0),
            page_budget=row.daily_page_budget,
            cost_today=Decimal(str(spent.get(row.key) or 0)),
            precision=row.precision_30d,
            pushes_30d=row.pushes_30d,
            auto_push=row.auto_push,
            forbidden=forbidden_of(row.config),
            paused_until=row.paused_until,
            breaker_until=row.breaker_until,
        )
        for row in rows
    ]


async def _locked_source(session: AsyncSession, key: str) -> Source | None:
    found: Source | None = await session.scalar(
        sa.select(Source)
        .where(Source.key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return found


async def toggle_source(session: AsyncSession, key: str) -> bool | None:
    """``src:<key>:t``: the switch flipped under the row's lock (a tap that
    arrives twice flips it twice, as two taps would). The new value, or None
    when the row is gone."""
    row = await _locked_source(session, key)
    if row is None:
        return None
    row.enabled = not row.enabled
    await session.flush()
    return row.enabled


async def forbid_auto_push(session: AsyncSession, key: str) -> bool | None:
    """``src:<key>:a``: ``config.auto_push_forbidden=true`` and ``auto_push``
    off at once — the owner forbids, never grants. True when it was not
    forbidden before, False when it already was, None when the row is gone."""
    row = await _locked_source(session, key)
    if row is None:
        return None
    if forbidden_of(row.config) and not row.auto_push:
        return False
    row.config = {**dict(row.config or {}), AUTO_PUSH_FORBIDDEN: True}
    row.auto_push = False
    await session.flush()
    return True


@dataclass(frozen=True, slots=True)
class RunLine:
    started_at: datetime
    status: str
    pages: int
    new_companies: int
    error: str | None


@dataclass(frozen=True, slots=True)
class SourceInfo:
    line: SourceLine
    runs: tuple[RunLine, ...]
    breaker_reason: str | None
    paused_reason: str | None


async def source_info(
    session: AsyncSession, key: str, *, now: datetime
) -> SourceInfo | None:
    """``src:<key>:i``: the row, its last runs, the breaker and the pause."""
    row = await session.get(Source, key, populate_existing=True)
    if row is None:
        return None
    line = next(x for x in await source_lines(session, now=now) if x.key == key)
    runs = (
        await session.scalars(
            sa.select(CollectorRun)
            .where(CollectorRun.source_key == key)
            .order_by(CollectorRun.started_at.desc(), CollectorRun.id.desc())
            .limit(INFO_RUNS)
        )
    ).all()
    return SourceInfo(
        line=line,
        runs=tuple(
            RunLine(r.started_at, r.status, r.pages, r.new_companies, r.error)
            for r in runs
        ),
        breaker_reason=row.breaker_reason,
        paused_reason=row.paused_reason,
    )


# --- /stir (04 §3 E4, captcha path) ------------------------------------------------


StirOutcome = Literal["written", "same", "held", "has_other", "bad_stir", "no_candidate"]


@dataclass(frozen=True, slots=True)
class StirResult:
    outcome: StirOutcome
    stir: str | None = None
    company_id: uuid.UUID | None = None
    company_name: str | None = None
    # «held»: the company that holds the STIR; «has_other»: the STIR the
    # candidate's company already carries.
    other_id: uuid.UUID | None = None
    other_name: str | None = None
    other_stir: str | None = None


async def set_stir(
    session: AsyncSession, candidate_id: uuid.UUID, raw: str, *, now: datetime
) -> StirResult:
    """The owner's ``/stir <candidate_id> <9 digits>``: the STIR checked for
    its shape (``keys.stir_key``, the one home), refused when another live
    company holds it (that company is named — a merge is the owner's 🔀, not
    this) or when the company already carries another; else written on the
    candidate's company with an evidence row (``source_key='registry_lookup'``,
    ``excerpt='owner:/stir'``) and ``next_enrich_at = now`` (E4 re-runs)."""
    stir = keys.stir_key(raw) if raw.strip().isdigit() and len(raw.strip()) == 9 else None
    if stir is None:
        return StirResult("bad_stir")
    company_id = await session.scalar(
        sa.select(LeadCandidate.company_id).where(LeadCandidate.id == candidate_id)
    )
    if company_id is None:
        return StirResult("no_candidate", stir=stir)
    # candidates' lock order: the company before the candidate.
    found = await session.get(
        Company, company_id, with_for_update=True, populate_existing=True
    )
    candidate = await session.scalar(
        sa.select(LeadCandidate)
        .where(LeadCandidate.id == candidate_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if found is None or candidate is None:
        return StirResult("no_candidate", stir=stir)
    company = await companies.root_company(session, found)
    lineage = await companies.company_lineage(session, company.id)
    holder = await session.scalar(
        sa.select(Company).where(
            Company.stir == stir,
            Company.state.not_in(("merged", "purged")),
            Company.id.not_in(lineage),
        )
    )

    def result(outcome: StirOutcome, **other: Any) -> StirResult:
        return StirResult(
            outcome,
            stir=stir,
            company_id=company.id,
            company_name=company.display_name,
            **other,
        )

    if holder is not None:
        return result("held", other_id=holder.id, other_name=holder.display_name)
    if company.stir is not None and company.stir.strip() != stir:
        return result("has_other", other_stir=company.stir.strip())
    if company.stir is not None:
        return result("same")
    company.stir = stir
    session.add(
        Evidence(
            source_key=STIR_SOURCE,
            url=f"{STIR_EXCERPT}:{company.id}",
            fetched_at=now,
            sha256=hashlib.sha256(
                f"{STIR_EXCERPT}:{company.id}:{stir}".encode()
            ).hexdigest(),
            content_type="text/plain",
            bytes=0,
            excerpt=STIR_EXCERPT,
            expires_at=now + timedelta(days=STIR_EVIDENCE_DAYS),
        )
    )
    candidate.next_enrich_at = now
    await session.flush()
    return result("written")


__all__ = [
    "AUTO_PUSH_FORBIDDEN",
    "INFO_RUNS",
    "PAUSE_FLAG",
    "RunLine",
    "SourceInfo",
    "SourceLine",
    "StirResult",
    "clear_erp_breaker",
    "erp_breaker_until",
    "forbid_auto_push",
    "forbidden_of",
    "owner_ref",
    "pause",
    "paused",
    "resume",
    "set_stir",
    "source_info",
    "source_lines",
    "toggle_source",
]
