"""Sources and their runs (02 §2.1–2.2)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from clientscrapper.db import enums as e
from clientscrapper.db.base import Base, created_at_column, updated_at_column
from clientscrapper.db.models._common import EMPTY_JSONB, bigint_pk, check_in


class Source(Base):
    """One row per collector or feed: the switches, budgets, breaker and
    precision the runner, the gate and the bot read. Schedulable units and
    attribution-only sub-sources (``search_brave``, ``maps_google`` …) share
    the table so every ``source_key`` foreign key can name the engine or site
    that produced a row. Seeded by 0001."""

    __tablename__ = "sources"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    family: Mapped[str] = mapped_column(sa.Text)
    title_uz: Mapped[str] = mapped_column(sa.Text)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    # APScheduler cron in Asia/Tashkent; NULL = event-driven or never scheduled.
    cron: Mapped[str | None] = mapped_column(sa.Text)
    rps: Mapped[Decimal] = mapped_column(sa.Numeric(6, 3), server_default=sa.text("0.3"))
    daily_page_budget: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("500")
    )
    # The per-source budget Budget.can_spend reads before costs.ask (04 §8).
    daily_cost_cap_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(8, 4), server_default=sa.text("0")
    )
    # True only when pushes_30d >= 15 and precision_30d >= 0.60, flipped by
    # source_precision_job — never on while config.auto_push_forbidden.
    auto_push: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    precision_30d: Mapped[Decimal | None] = mapped_column(sa.Numeric(5, 4))
    pushes_30d: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    positives_30d: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    paused_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    paused_reason: Mapped[str | None] = mapped_column(sa.Text)
    breaker_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    breaker_reason: Mapped[str | None] = mapped_column(sa.Text)
    last_run_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # Per-source state: keywords, seeds, cursors, the feed's salt_id. Never a key.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSONB)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        sa.CheckConstraint("key ~ '^[a-z][a-z0-9_]{2,40}$'", name="ck_sources_key"),
        check_in("sources", "family", e.SOURCE_FAMILIES),
        sa.CheckConstraint("rps > 0 AND rps <= 5", name="ck_sources_rps"),
        sa.CheckConstraint("daily_page_budget >= 0", name="ck_sources_daily_page_budget"),
        sa.CheckConstraint(
            "daily_cost_cap_usd >= 0", name="ck_sources_daily_cost_cap_usd"
        ),
        sa.CheckConstraint(
            "precision_30d BETWEEN 0 AND 1", name="ck_sources_precision_30d"
        ),
    )


class CollectorRun(Base):
    """One row per collector execution. Counters only; extras such as
    ``unchanged``, ``skipped_robots`` and ``note`` go into ``meta``."""

    __tablename__ = "collector_runs"

    id: Mapped[int] = bigint_pk()
    source_key: Mapped[str] = mapped_column(sa.Text, sa.ForeignKey("sources.key"))
    started_at: Mapped[datetime] = created_at_column()
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    status: Mapped[str] = mapped_column(sa.Text, server_default="running")
    requests: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    pages: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    http_403: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    http_429: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    new_evidence: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    new_observations: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    new_companies: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    new_signals: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 6), server_default=sa.text("0")
    )
    # An error code (timeout|host_blocked|budget|…), never a body.
    error: Mapped[str | None] = mapped_column(sa.Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSONB)

    __table_args__ = (
        check_in("collector_runs", "status", e.RUN_STATUSES),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_collector_runs_finished",
        ),
        sa.Index(
            "ix_collector_runs_source_started", "source_key", sa.text("started_at DESC")
        ),
        sa.Index(
            "ux_collector_runs_one_running",
            "source_key",
            unique=True,
            postgresql_where=sa.text("status = 'running'"),
        ),
    )
