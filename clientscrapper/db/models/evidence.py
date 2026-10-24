"""Fetched artefacts and the findings parsed from them (02 §2.3, §2.5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from clientscrapper.db import enums as e
from clientscrapper.db.base import Base, created_at_column
from clientscrapper.db.models._common import EMPTY_JSONB, bigint_pk, check_in


class Evidence(Base):
    """An immutable fetched artefact. The row outlives its file: ``raw_path``
    is cleared at ``expires_at`` (180 d; 30 d for Places) while ``url``,
    ``fetched_at`` and ``excerpt`` — all a card or an ERP note quotes — stay.
    A re-fetch with identical bytes is a no-op (``ux_evidence_url_sha``)."""

    __tablename__ = "evidence"

    id: Mapped[int] = bigint_pk()
    source_key: Mapped[str] = mapped_column(sa.Text, sa.ForeignKey("sources.key"))
    collector_run_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("collector_runs.id", ondelete="SET NULL")
    )
    url: Mapped[str] = mapped_column(sa.Text)
    # Platform-native id: 'tg:<chat>:<msg>', OLX ad id, hh vacancy id, place_id.
    source_ref: Mapped[str | None] = mapped_column(sa.Text)
    fetched_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    sha256: Mapped[str] = mapped_column(sa.CHAR(64))
    content_type: Mapped[str] = mapped_column(sa.Text, server_default="text/html")
    bytes: Mapped[int] = mapped_column(sa.Integer)
    # '<yy>/<sha256>.gz' under EVIDENCE_DIR; NULL = not stored or purged.
    raw_path: Mapped[str | None] = mapped_column(sa.Text)
    # The ≤600-char slice cards and ERP notes quote.
    excerpt: Mapped[str] = mapped_column(sa.Text)
    title: Mapped[str | None] = mapped_column(sa.Text)
    lang: Mapped[str | None] = mapped_column(sa.CHAR(2))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    raw_deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # {etag, last_modified, final_url, status, truncated}: conditional-fetch
    # inputs and fetch facts (03 §1.1).
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_JSONB)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.CheckConstraint("length(url) <= 2048", name="ck_evidence_url"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_evidence_sha256"),
        sa.CheckConstraint("bytes >= 0", name="ck_evidence_bytes"),
        sa.CheckConstraint("length(excerpt) <= 600", name="ck_evidence_excerpt"),
        sa.CheckConstraint("length(title) <= 300", name="ck_evidence_title"),
        sa.Index("ux_evidence_url_sha", sa.text("md5(url)"), "sha256", unique=True),
        sa.Index("ix_evidence_source_fetched", "source_key", sa.text("fetched_at DESC")),
        sa.Index("ix_evidence_sha", "sha256"),
        sa.Index(
            "ix_evidence_ref",
            "source_key",
            "source_ref",
            postgresql_where=sa.text("source_ref IS NOT NULL"),
        ),
        sa.Index(
            "ix_evidence_expiry",
            "expires_at",
            postgresql_where=sa.text("raw_path IS NOT NULL"),
        ),
        sa.Index("ix_evidence_collector_run", "collector_run_id"),
    )


class Observation(Base):
    """One parsed finding, before resolution. ``parsed`` is the 02 §2.5 JSON
    shape; its phones and person go at retention step R3."""

    __tablename__ = "observations"

    id: Mapped[int] = bigint_pk()
    evidence_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("evidence.id", ondelete="CASCADE")
    )
    # A directory page yields many.
    ordinal: Mapped[int] = mapped_column(sa.SmallInteger, server_default=sa.text("0"))
    source_key: Mapped[str] = mapped_column(sa.Text, sa.ForeignKey("sources.key"))
    status: Mapped[str] = mapped_column(sa.Text, server_default="new")
    parsed: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parsed_version: Mapped[int] = mapped_column(
        sa.SmallInteger, server_default=sa.text("1")
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="SET NULL")
    )
    # competitor|supplier_cn|b2c|no_company|duplicate|foreign
    reject_reason: Mapped[str | None] = mapped_column(sa.Text)
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # Phones and person removed from `parsed` (retention R3).
    scrubbed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.CheckConstraint("ordinal >= 0", name="ck_observations_ordinal"),
        check_in("observations", "status", e.OBSERVATION_STATUSES),
        sa.UniqueConstraint(
            "evidence_id", "ordinal", name="uq_observations_evidence_ordinal"
        ),
        sa.Index(
            "ix_observations_queue",
            "created_at",
            postgresql_where=sa.text("status IN ('new', 'needs_review')"),
        ),
        sa.Index("ix_observations_company", "company_id"),
        sa.Index("ix_observations_contact", "contact_id"),
        sa.Index("ix_observations_name_key", sa.text("(parsed ->> 'name_key')")),
    )
