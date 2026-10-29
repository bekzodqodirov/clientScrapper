"""Candidates, reviews, pushes and what the ERP did with them (02 §2.9–2.11).

The state machines are ``services/candidates.py`` and ``services/reviews.py``;
the database checks values and invariants only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from clientscrapper.db import enums as e
from clientscrapper.db.base import Base, created_at_column, updated_at_column
from clientscrapper.db.enums import sql_list
from clientscrapper.db.models._common import UUID_DEFAULT, bigint_pk, check_in

EXTERNAL_ID_RE = "^cs:[0-9a-f-]{36}:[0-9]{6}$"


class LeadCandidate(Base):
    """A company on its way to the ERP: found → … → pushed, or a terminal
    refusal. At most one open candidate per company (``ux_lead_candidates_open``);
    ``weights_version`` + ``score_breakdown`` make every score reproducible."""

    __tablename__ = "lead_candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="SET NULL")
    )
    state: Mapped[str] = mapped_column(sa.Text, server_default="found")
    tier: Mapped[str | None] = mapped_column(sa.Text)
    score: Mapped[int | None] = mapped_column(sa.SmallInteger)
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    weights_version: Mapped[int | None] = mapped_column(sa.Integer)
    # The source whose signal created the candidate (precision attribution).
    primary_source_key: Mapped[str | None] = mapped_column(
        sa.Text, sa.ForeignKey("sources.key")
    )
    exclude_reason: Mapped[str | None] = mapped_column(sa.Text)
    # NULL on a 'suppressed' candidate: its suppression row was retired (R6,
    # a full ERP pull, an outcomes retraction) — the «row is gone» of 02 §2.9.
    suppressed_by: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("suppression.id", ondelete="SET NULL")
    )
    hold_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    enrich_attempts: Mapped[int] = mapped_column(
        sa.SmallInteger, server_default=sa.text("0")
    )
    next_enrich_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    external_id: Mapped[str | None] = mapped_column(sa.Text)
    # 'owner:<tg_id>' | 'auto:<source_key>' | 'manager:<tg_id>'
    approved_by: Mapped[str | None] = mapped_column(sa.Text)
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    pushed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_scored_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # An auto-approved candidate's push refusals (04 §7 item 14): it has no
    # owner review row to carry them (0004, T-21; /lidlar holat lists them).
    push_note: Mapped[str | None] = mapped_column(sa.Text)
    push_noted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        check_in("lead_candidates", "state", e.CANDIDATE_STATES),
        check_in("lead_candidates", "tier", e.CANDIDATE_TIERS),
        sa.CheckConstraint("score BETWEEN 0 AND 100", name="ck_lead_candidates_score"),
        check_in("lead_candidates", "exclude_reason", e.EXCLUDE_REASONS),
        sa.CheckConstraint(
            "enrich_attempts BETWEEN 0 AND 3", name="ck_lead_candidates_enrich_attempts"
        ),
        sa.CheckConstraint(
            f"external_id ~ '{EXTERNAL_ID_RE}'", name="ck_lead_candidates_external_id"
        ),
        sa.CheckConstraint(
            "state <> 'excluded' OR exclude_reason IS NOT NULL",
            name="ck_lead_candidates_excluded",
        ),
        sa.CheckConstraint(
            "state <> 'pushed' OR (external_id IS NOT NULL AND pushed_at IS NOT NULL)",
            name="ck_lead_candidates_pushed",
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'pushed') "
            "OR (approved_at IS NOT NULL AND approved_by IS NOT NULL)",
            name="ck_lead_candidates_approved",
        ),
        sa.CheckConstraint(
            "state NOT IN ('scored', 'review', 'approved', 'pushed') "
            "OR score IS NOT NULL",
            name="ck_lead_candidates_scored",
        ),
        sa.CheckConstraint(
            "length(push_note) <= 500", name="ck_lead_candidates_push_note"
        ),
        # No ck_lead_candidates_suppressed: a CHECK spanning suppressed_by, an
        # ON DELETE SET NULL column, made every retirement of a suppression row
        # a candidate points at fail (fullprompt #809). The pointer may go
        # («the suppression row is gone», 02 §2.9); entering 'suppressed'
        # without one is refused by trg_lead_candidates_suppressed (0001).
        sa.Index(
            "ux_lead_candidates_open",
            "company_id",
            unique=True,
            postgresql_where=sa.text(f"state IN ({sql_list(e.CANDIDATE_OPEN_STATES)})"),
        ),
        sa.Index(
            "ux_lead_candidates_external",
            "external_id",
            unique=True,
            postgresql_where=sa.text("external_id IS NOT NULL"),
        ),
        sa.Index("ix_lead_candidates_state_score", "state", sa.text("score DESC")),
        sa.Index(
            "ix_lead_candidates_enrich_due",
            "next_enrich_at",
            postgresql_where=sa.text("state = 'enriching'"),
        ),
        sa.Index(
            "ix_lead_candidates_hold_until",
            "hold_until",
            postgresql_where=sa.text("state = 'hold'"),
        ),
        sa.Index("ix_lead_candidates_company", "company_id"),
        sa.Index("ix_lead_candidates_contact", "contact_id"),
        sa.Index("ix_lead_candidates_suppressed", "suppressed_by"),
    )


class LeadReview(Base):
    """A review card: the owner's (``kind='owner'``) or, after a handoff, a
    manager's (``kind='manager'``) — one machine for both, one live card of
    each kind per candidate."""

    __tablename__ = "lead_reviews"

    id: Mapped[int] = bigint_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("lead_candidates.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(sa.Text, server_default="owner")
    state: Mapped[str] = mapped_column(sa.Text, server_default="pending")
    card_chat_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    card_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    # sha256 of the rendered card: the message is edited only when it changed.
    card_hash: Mapped[str | None] = mapped_column(sa.CHAR(64))
    asked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    answer: Mapped[str | None] = mapped_column(sa.Text)
    by_telegram_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    note: Mapped[str | None] = mapped_column(sa.Text)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # ⏭ Keyinroq: when the card is asked again as a fresh message (0004,
    # T-21; 06 §1.3 «ertaga qayta soʻrayman»). NULL = not put off.
    remind_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        check_in("lead_reviews", "kind", e.REVIEW_KINDS),
        check_in("lead_reviews", "state", e.REVIEW_STATES),
        check_in("lead_reviews", "answer", e.REVIEW_ANSWERS),
        sa.CheckConstraint("length(note) <= 500", name="ck_lead_reviews_note"),
        sa.CheckConstraint(
            "(state IN ('approved', 'declined', 'merged')) = (answered_at IS NOT NULL)",
            name="ck_lead_reviews_answered",
        ),
        sa.CheckConstraint(
            "state <> 'asked' OR (asked_at IS NOT NULL AND card_message_id IS NOT NULL "
            "AND expires_at IS NOT NULL)",
            name="ck_lead_reviews_asked",
        ),
        sa.Index(
            "ux_lead_reviews_live",
            "candidate_id",
            "kind",
            unique=True,
            postgresql_where=sa.text(f"state IN ({sql_list(e.REVIEW_LIVE_STATES)})"),
        ),
        sa.Index(
            "ux_lead_reviews_card",
            "card_chat_id",
            "card_message_id",
            unique=True,
            postgresql_where=sa.text("card_message_id IS NOT NULL"),
        ),
        sa.Index(
            "ix_lead_reviews_expiry",
            "expires_at",
            postgresql_where=sa.text("state = 'asked'"),
        ),
        sa.Index("ix_lead_reviews_candidate", "candidate_id"),
        sa.Index(
            "ix_lead_reviews_remind",
            "remind_at",
            postgresql_where=sa.text("state = 'asked' AND remind_at IS NOT NULL"),
        ),
    )


class PushLog(Base):
    """Every attempt to hand a lead to the ERP. The claim row is committed
    before the HTTP call; ``ux_push_log_delivered`` is the real fence against
    a second delivery of one ``external_id``. Append-only (``cs_append_only``)."""

    __tablename__ = "push_log"

    id: Mapped[int] = bigint_pk()
    external_id: Mapped[str] = mapped_column(sa.Text)
    attempt: Mapped[int] = mapped_column(sa.SmallInteger)
    # RESTRICT on purpose: a candidate with push history is never deleted, so
    # 02 §4 R6's hold sweep must skip it (tests/test_constraints.py).
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("lead_candidates.id", ondelete="RESTRICT")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="RESTRICT")
    )
    phone_tail: Mapped[str] = mapped_column(sa.CHAR(9))
    # '/api/leads/in/agent' | '/api/leads/in/other'
    endpoint: Mapped[str] = mapped_column(sa.Text)
    source_key_sent: Mapped[str] = mapped_column(sa.Text)
    # The body as sent; NULL after 90 d (retention R6).
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    payload_sha256: Mapped[str] = mapped_column(sa.CHAR(64))
    status: Mapped[str] = mapped_column(sa.Text, server_default="sending")
    http_status: Mapped[int | None] = mapped_column(sa.SmallInteger)
    # timeout|conn|http_5xx|http_404|http_3xx|http_4xx|http_2xx_badbody|breaker
    error: Mapped[str | None] = mapped_column(sa.Text)
    sent_at: Mapped[datetime] = created_at_column()
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # 0002: set by rule 5's re-push or by /push tekshir on a delivered row — a
    # 2xx row can never become 'failed' (ck_push_log_sent), so this is what
    # frees ux_push_log_delivered for the attempt that re-sends it.
    superseded_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint(
            f"external_id ~ '{EXTERNAL_ID_RE}'", name="ck_push_log_external_id"
        ),
        sa.CheckConstraint("attempt BETWEEN 1 AND 3", name="ck_push_log_attempt"),
        sa.CheckConstraint("phone_tail ~ '^[0-9]{9}$'", name="ck_push_log_phone_tail"),
        check_in("push_log", "source_key_sent", e.PUSH_SOURCE_KEYS),
        check_in("push_log", "status", e.PUSH_STATUSES),
        sa.UniqueConstraint(
            "external_id", "attempt", name="uq_push_log_external_attempt"
        ),
        # sent ⇒ 2xx or no answer at all, and 2xx ⇒ sent | unknown (an
        # unparseable 2xx body is 'unknown' with error='http_2xx_badbody'). The
        # NULL passes on purpose: an 'unknown' row the outcomes walk confirms
        # becomes 'sent' with no HTTP status ever received (02 §2.10 rule 5b).
        sa.CheckConstraint(
            "(status <> 'sent' OR http_status BETWEEN 200 AND 299) "
            "AND (http_status IS NULL OR http_status NOT BETWEEN 200 AND 299 "
            "OR status IN ('sent', 'unknown'))",
            name="ck_push_log_sent",
        ),
        sa.Index(
            "ux_push_log_delivered",
            "external_id",
            unique=True,
            # 0002 narrowed it to the rows nobody superseded.
            postgresql_where=sa.text(
                f"status IN ({sql_list(e.PUSH_DELIVERED_STATUSES)}) "
                "AND superseded_at IS NULL"
            ),
        ),
        sa.Index("ix_push_log_phone_sent", "phone_tail", sa.text("sent_at DESC")),
        sa.Index("ix_push_log_company_sent", "company_id", sa.text("sent_at DESC")),
        sa.Index(
            "ix_push_log_day",
            "sent_at",
            postgresql_where=sa.text(
                f"status IN ({sql_list(e.PUSH_DELIVERED_STATUSES)})"
            ),
        ),
        sa.Index("ix_push_log_candidate", "candidate_id"),
    )


class Outcome(Base):
    """What the ERP did with one push: a snapshot upserted nightly from the
    outcomes feed. ``raw`` is the feed row verbatim — by contract it carries no
    name, phone or note. Append-only (``cs_append_only``)."""

    __tablename__ = "outcomes"

    external_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    # The ERP's lead_intakes.id.
    intake_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("lead_candidates.id", ondelete="SET NULL")
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL")
    )
    # Copied from the candidate for per-source precision.
    source_key: Mapped[str | None] = mapped_column(sa.Text, sa.ForeignKey("sources.key"))
    landing: Mapped[str] = mapped_column(sa.Text)
    # The feed's ``reason``: suppressed|capped|no_phone|no_contact for a
    # dropped landing, NULL otherwise (a replay writes no ERP row). No CHECK:
    # a reason the ERP adds later is ``dropped_other``, not a refused page.
    landing_reason: Mapped[str | None] = mapped_column(sa.Text)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    stage_kind: Mapped[str | None] = mapped_column(sa.Text)
    stage_name: Mapped[str | None] = mapped_column(sa.Text)
    # Only a LABEL (the owner's dictionary or agent_no_contact_reasons); a
    # seller's free text arrives as NULL (05 §A.9).
    lost_reason: Mapped[str | None] = mapped_column(sa.Text)
    # 0002: the ERP's own «lost with a reason agent_no_contact_reasons lists».
    no_contact: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    contacted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    contact_kind: Mapped[str | None] = mapped_column(sa.Text)
    quoted_volume_m3: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 3))
    # The feed row's `at`.
    erp_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    label: Mapped[str] = mapped_column(sa.Text)
    precision_class: Mapped[str] = mapped_column(sa.Text)
    label_changed_at: Mapped[datetime] = created_at_column()
    first_read_at: Mapped[datetime] = created_at_column()
    read_at: Mapped[datetime] = created_at_column()
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__ = (
        check_in("outcomes", "landing", e.LANDINGS),
        check_in("outcomes", "stage_kind", e.STAGE_KINDS),
        check_in("outcomes", "contact_kind", e.CONTACT_KINDS),
        check_in("outcomes", "label", e.OUTCOME_LABELS),
        check_in("outcomes", "precision_class", e.PRECISION_CLASSES),
        sa.Index("ix_outcomes_source_first_read", "source_key", "first_read_at"),
        sa.Index("ix_outcomes_company", "company_id"),
        sa.Index("ix_outcomes_label", "label"),
        sa.Index("ix_outcomes_candidate", "candidate_id"),
    )
