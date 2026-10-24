"""Signals and their weights (02 §2.7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from clientscrapper.db import enums as e
from clientscrapper.db.base import Base, created_at_column
from clientscrapper.db.models._common import bigint_pk, check_in


class Signal(Base):
    """One piece of evidence for one scoring key, with the verbatim quote that
    carries it. Derived keys (recency, contact, corroboration) are computed
    by the scorer and never stored here."""

    __tablename__ = "signals"

    id: Mapped[int] = bigint_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="SET NULL")
    )
    evidence_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("evidence.id", ondelete="CASCADE")
    )
    observation_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("observations.id", ondelete="SET NULL")
    )
    key: Mapped[str] = mapped_column(sa.Text)
    # The measured quantity: m³, an order count, years.
    value: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 3))
    # A verbatim substring of evidence.excerpt (validated in Python).
    quote: Mapped[str | None] = mapped_column(sa.Text)
    observed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    # observed_at + the key's decay_days; NULL = no decay.
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    source_key: Mapped[str] = mapped_column(sa.Text, sa.ForeignKey("sources.key"))
    # NULL = a deterministic rule; else the model that said so.
    model: Mapped[str | None] = mapped_column(sa.Text)
    model_job_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("model_jobs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        check_in("signals", "key", e.SIGNAL_KEYS),
        sa.CheckConstraint("length(quote) <= 300", name="ck_signals_quote"),
        sa.UniqueConstraint(
            "company_id", "key", "evidence_id", name="uq_signals_company_key_evidence"
        ),
        sa.Index("ix_signals_company_key", "company_id", "key"),
        sa.Index("ix_signals_observed", sa.text("observed_at DESC")),
        sa.Index("ix_signals_evidence", "evidence_id"),
        sa.Index("ix_signals_contact", "contact_id"),
        sa.Index("ix_signals_observation", "observation_id"),
        sa.Index("ix_signals_model_job", "model_job_id"),
    )


class ScoringWeight(Base):
    """One key of one weights version. A version is a complete set; the
    active one is the newest approved. ``cs_scoring_weights_frozen`` refuses
    any change to an approved row but the approval itself, and every delete."""

    __tablename__ = "scoring_weights"

    version: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    # SIGNAL_KEYS ∪ DERIVED_KEYS.
    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    group_key: Mapped[str] = mapped_column(sa.Text)
    points: Mapped[int] = mapped_column(sa.SmallInteger)
    group_cap: Mapped[int] = mapped_column(sa.SmallInteger)
    decay_days: Mapped[int | None] = mapped_column(sa.Integer)
    decayed_factor: Mapped[Decimal] = mapped_column(
        sa.Numeric(3, 2), server_default=sa.text("0.5")
    )
    note: Mapped[str | None] = mapped_column(sa.Text)
    # 'seed' | 'owner:<tg_id>' | 'fit_weights@<date>'
    proposed_by: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at_column()
    approved_by: Mapped[str | None] = mapped_column(sa.Text)
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint("version >= 1", name="ck_scoring_weights_version"),
        check_in("scoring_weights", "group_key", e.WEIGHT_GROUPS),
        sa.CheckConstraint(
            "points BETWEEN -100 AND 100", name="ck_scoring_weights_points"
        ),
        sa.CheckConstraint(
            "group_cap BETWEEN 0 AND 100", name="ck_scoring_weights_group_cap"
        ),
        sa.CheckConstraint("decay_days > 0", name="ck_scoring_weights_decay_days"),
        sa.CheckConstraint(
            "decayed_factor BETWEEN 0 AND 1", name="ck_scoring_weights_decayed_factor"
        ),
        check_in("scoring_weights", "key", e.SIGNAL_KEYS + e.DERIVED_KEYS),
        sa.Index(
            "ix_scoring_weights_approved",
            "version",
            postgresql_where=sa.text("approved_at IS NOT NULL"),
        ),
    )
