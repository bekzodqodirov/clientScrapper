"""Operational ledgers: liveness, spend, alerts, shared switches, model calls,
merges, purges and the Sheets mirror (02 §2.6, §2.13–2.15).

Several of these are written with raw SQL by the bot, the userbot and the
worker (``health.beat``, ``costs._record``, the ``runtime_flags`` upserts);
they rely on the 0001 triggers, not on ORM hooks.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from clientscrapper.db import enums as e
from clientscrapper.db.base import Base, created_at_column, updated_at_column
from clientscrapper.db.models._common import EMPTY_ARRAY, EMPTY_JSONB, bigint_pk, check_in


class Heartbeat(Base):
    """Liveness ledger: one row per process, scheduler job or collector run
    name (bot|worker|userbot|backup|job:<id>|collector:<key>[.variant]).
    ``detail`` is replaced whole on every beat."""

    __tablename__ = "heartbeats"

    component: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSONB)


class ModelJob(Base):
    """One Claude call, instant or in a Message Batch: pending → submitted →
    applied | failed, ``fallback`` when the batch gave up after 3 attempts and
    the instant lane took it. ``request`` is pseudonymised; ``pseudonyms`` is
    the reverse map and never leaves the process."""

    __tablename__ = "model_jobs"

    id: Mapped[int] = bigint_pk()
    kind: Mapped[str] = mapped_column(sa.Text)
    lane: Mapped[str] = mapped_column(sa.Text)
    # The configured id (EXTRACT_MODEL / VERDICT_MODEL / BATCH_MODEL).
    model: Mapped[str] = mapped_column(sa.Text)
    # 'cs-' || uuid4, minted in Python before the insert.
    custom_id: Mapped[str] = mapped_column(sa.Text, unique=True)
    batch_id: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(sa.Text, server_default="pending")
    attempts: Mapped[int] = mapped_column(sa.SmallInteger, server_default=sa.text("0"))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE")
    )
    evidence_ids: Mapped[list[int]] = mapped_column(
        ARRAY(sa.BigInteger), server_default=EMPTY_ARRAY
    )
    # Pseudonymised payload; NULL 30 d after a terminal status (R6).
    request: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # {"PHONE_1": "+998…", "TG_1": "…"}; NULL with request.
    pseudonyms: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The validated structured output.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(sa.Text)
    cost_usd: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 6))
    submitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        check_in("model_jobs", "kind", e.JOB_KINDS),
        check_in("model_jobs", "lane", e.JOB_LANES),
        check_in("model_jobs", "status", e.JOB_STATUSES),
        sa.CheckConstraint("attempts BETWEEN 0 AND 3", name="ck_model_jobs_attempts"),
        sa.Index("ix_model_jobs_status_created", "status", "created_at"),
        sa.Index(
            "ix_model_jobs_batch",
            "batch_id",
            postgresql_where=sa.text("batch_id IS NOT NULL"),
        ),
        sa.Index("ix_model_jobs_company", "company_id"),
    )


class UsageLog(Base):
    """Every paid call, priced (04 §8). A refusal by ``costs.ask`` is a row
    too — ``cost_usd=0, units=0, model='refused:<code>'`` — because the bot is
    another process and counts refusals from here. Append-only."""

    __tablename__ = "usage_log"

    id: Mapped[int] = bigint_pk()
    provider: Mapped[str] = mapped_column(sa.Text)
    # model id | apify actor | places SKU | 'refused:<code>'
    model: Mapped[str | None] = mapped_column(sa.Text)
    operation: Mapped[str] = mapped_column(sa.Text)
    input_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    output_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    cache_write_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    # Queries, results, MB for providers that do not bill tokens.
    units: Mapped[Decimal | None] = mapped_column(sa.Numeric(14, 3))
    batch: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    cost_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 6))
    source_key: Mapped[str | None] = mapped_column(sa.Text, sa.ForeignKey("sources.key"))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL")
    )
    model_job_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("model_jobs.id", ondelete="SET NULL")
    )
    collector_run_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("collector_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        check_in("usage_log", "provider", e.PROVIDERS),
        check_in("usage_log", "operation", e.OPERATIONS),
        sa.CheckConstraint("cost_usd >= 0", name="ck_usage_log_cost_usd"),
        sa.Index("ix_usage_log_created_at", "created_at"),
        sa.Index("ix_usage_log_provider_created", "provider", "created_at"),
        sa.Index("ix_usage_log_company", "company_id"),
        sa.Index("ix_usage_log_model_job", "model_job_id"),
        sa.Index("ix_usage_log_collector_run", "collector_run_id"),
    )


class ReminderLog(Base):
    """One row per reminder or alert actually sent, so nothing is said twice:
    alert:<key> | recovered:<key> | review_expired | push_unconfirmed | digest
    | source_paused — and undelivered:<key>, one row per key moved to the
    newest moment its own teller could not deliver it (costs.note_undelivered:
    the health sweep says such a told-first key then)."""

    __tablename__ = "reminder_log"

    id: Mapped[int] = bigint_pk()
    kind: Mapped[str] = mapped_column(sa.Text)
    ref: Mapped[str] = mapped_column(sa.Text)
    sent_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.Index("ix_reminder_log_kind_ref_sent", "kind", "ref", "sent_at"),
    )


class RuntimeFlag(Base):
    """A switch that survives a restart and is read by bot, worker and userbot:
    'paused' {"since", "reason"} | 'flood_until' | 'review_hours_override'.

    T-08 adds two kinds of row, no schema change: 'problem:<key>'
    {"since", "until", "detail"} — a health problem raised by the module that
    owns it (``health.raise_problem``, the REPORTED_KEYS of 06 §4.2), deleted
    when it clears — and 'anthropic_probe' {"at"} — the half-open probe slot
    that keeps ``anthropic_silence`` from latching (``health.anthropic_down``)."""

    __tablename__ = "runtime_flags"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = updated_at_column()
    # 'owner:<tg_id>' | 'userbot' | 'worker:<job>'
    updated_by: Mapped[str] = mapped_column(sa.Text)


class MergeLog(Base):
    """One merge of a company into another, with every re-pointed id, so an
    undo within 24 h can put them back. Append-only."""

    __tablename__ = "merge_log"

    id: Mapped[int] = bigint_pk()
    # No FK: the source row stays (state='merged') but may be purged later.
    source_company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    target_company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="RESTRICT")
    )
    # resolver:stir | resolver:phone | resolver:domain | resolver:fuzzy:<n> | owner:<id>
    decided_by: Mapped[str] = mapped_column(sa.Text)
    match_score: Mapped[int | None] = mapped_column(sa.SmallInteger)
    moved: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Identity fields copied onto the target because it lacked them.
    carried: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSONB)
    created_at: Mapped[datetime] = created_at_column()
    undone_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    undone_by: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.CheckConstraint(
            "match_score BETWEEN 0 AND 100", name="ck_merge_log_match_score"
        ),
        sa.Index("ix_merge_log_source", "source_company_id"),
        sa.Index("ix_merge_log_target", "target_company_id"),
    )


class PurgeLog(Base):
    """One purge, planned (counts) then executed. Holds the subject's hash,
    never its name or phone. Append-only."""

    __tablename__ = "purge_log"

    id: Mapped[int] = bigint_pk()
    subject_kind: Mapped[str] = mapped_column(sa.Text)
    # sha256 of the canonical key: company uuid | phone_tail | tg_user_id |
    # 'range:<from>:<to>' | 'retention:<step>:<date>'
    subject_hash: Mapped[str] = mapped_column(sa.CHAR(64))
    reason: Mapped[str] = mapped_column(sa.Text)
    # owner:<tg_id> | job:retention
    requested_by: Mapped[str] = mapped_column(sa.Text)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB)
    executed: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    planned_at: Mapped[datetime] = created_at_column()
    executed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        check_in("purge_log", "subject_kind", e.PURGE_SUBJECTS),
        check_in("purge_log", "reason", e.PURGE_REASONS),
        sa.Index("ix_purge_log_subject", "subject_kind", "subject_hash"),
    )


class SheetRow(Base):
    """Where one entity sits in the optional Sheets mirror and whether the
    sheet has caught up. Company-tier columns only reach the sheet."""

    __tablename__ = "sheet_rows"

    id: Mapped[int] = bigint_pk()
    tab: Mapped[str] = mapped_column(sa.String(32))
    entity_kind: Mapped[str] = mapped_column(sa.String(16))
    entity_id: Mapped[str] = mapped_column(sa.Text)
    row_number: Mapped[int | None] = mapped_column(sa.Integer)
    content_hash: Mapped[str] = mapped_column(sa.CHAR(64))
    state: Mapped[str] = mapped_column(sa.String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(sa.SmallInteger, server_default=sa.text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # An error code, never a message.
    last_error: Mapped[str | None] = mapped_column(sa.String(64))
    synced_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        check_in("sheet_rows", "tab", e.SHEET_TABS),
        check_in("sheet_rows", "entity_kind", e.SHEET_ENTITY_KINDS),
        check_in("sheet_rows", "state", e.SHEET_ROW_STATES),
        sa.CheckConstraint(
            "row_number IS NULL OR row_number >= 2", name="ck_sheet_rows_row_number"
        ),
        sa.UniqueConstraint(
            "tab", "entity_kind", "entity_id", name="uq_sheet_rows_entity"
        ),
        sa.Index(
            "ux_sheet_rows_row_number",
            "tab",
            "row_number",
            unique=True,
            postgresql_where=sa.text("row_number IS NOT NULL"),
        ),
        sa.Index(
            "ix_sheet_rows_due",
            "next_attempt_at",
            postgresql_where=sa.text("state IN ('pending', 'failed', 'tombstone')"),
        ),
    )
