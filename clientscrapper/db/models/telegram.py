"""The Telegram reader's tables (02 §2.12).

"No full chat logs" is structural: the userbot stores only a message whose
text matched a prefilter pattern (``cardinality(prefilter_hit) > 0``);
everything else only bumps ``chat_monitors.seen_count``.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from clientscrapper.db import enums as e
from clientscrapper.db.base import Base, created_at_column, updated_at_column
from clientscrapper.db.models._common import bigint_pk, check_in


class Interaction(Base):
    """One prefilter hit from a monitored group or channel. Person-tier only
    transiently: sender name and bio go at 90 days (R2b), an unconverted post
    goes whole at 30 (R2a)."""

    __tablename__ = "interactions"

    id: Mapped[int] = bigint_pk()
    tg_chat_id: Mapped[int] = mapped_column(sa.BigInteger)
    tg_message_id: Mapped[int] = mapped_column(sa.BigInteger)
    sender_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    sender_username: Mapped[str | None] = mapped_column(sa.Text)
    sender_name: Mapped[str | None] = mapped_column(sa.Text)
    sender_bio: Mapped[str | None] = mapped_column(sa.Text)
    text: Mapped[str | None] = mapped_column(sa.Text)
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    media_kind: Mapped[str] = mapped_column(sa.Text, server_default="none")
    reply_to_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    # Forwarded-from channel/user id: the tout rule counts a forward from a
    # channel other than the sender's own (03 §2.3). NULL = not a forward.
    fwd_from_peer_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    # {volume_kub, container, china, intent, wholesale, pricelist}
    prefilter_hit: Mapped[list[str]] = mapped_column(ARRAY(sa.Text))
    processed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    evidence_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("evidence.id", ondelete="SET NULL")
    )
    observation_id: Mapped[int | None] = mapped_column(
        sa.BigInteger, sa.ForeignKey("observations.id", ondelete="SET NULL")
    )
    text_purged_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.CheckConstraint(
            "sender_username IS NOT DISTINCT FROM cs_username_key(sender_username)",
            name="ck_interactions_sender_username",
        ),
        sa.CheckConstraint(
            "length(sender_name) <= 120", name="ck_interactions_sender_name"
        ),
        sa.CheckConstraint(
            "length(sender_bio) <= 300", name="ck_interactions_sender_bio"
        ),
        sa.CheckConstraint("length(text) <= 4000", name="ck_interactions_text"),
        check_in("interactions", "media_kind", e.MEDIA_KINDS),
        sa.CheckConstraint(
            "cardinality(prefilter_hit) > 0", name="ck_interactions_prefilter_nonempty"
        ),
        sa.UniqueConstraint(
            "tg_chat_id", "tg_message_id", name="uq_interactions_tg_message"
        ),
        sa.Index(
            "ix_interactions_unprocessed",
            "created_at",
            postgresql_where=sa.text("processed_at IS NULL"),
        ),
        sa.Index("ix_interactions_sender", "sender_id", sa.text("occurred_at DESC")),
        sa.Index(
            "ix_interactions_chat_occurred", "tg_chat_id", sa.text("occurred_at DESC")
        ),
        sa.Index(
            "ix_interactions_text_retention",
            "occurred_at",
            postgresql_where=sa.text("text IS NOT NULL"),
        ),
        sa.Index("ix_interactions_evidence", "evidence_id"),
        sa.Index("ix_interactions_observation", "observation_id"),
    )


class ChatMonitor(Base):
    """One chat the reader account knows: whether it is read, and the joiner's
    queue (``status``/``priority``). Groups and channels default on; private
    chats are off by rule and never read; ``role='calibration'`` (GSR's own
    chat) is off and nothing is read from it."""

    __tablename__ = "chat_monitors"

    id: Mapped[int] = mapped_column(
        sa.Integer, sa.Identity(always=True), primary_key=True
    )
    tg_chat_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True)
    chat_type: Mapped[str] = mapped_column(sa.Text)
    title: Mapped[str | None] = mapped_column(sa.Text)
    username: Mapped[str | None] = mapped_column(sa.Text)
    role: Mapped[str] = mapped_column(sa.Text, server_default="market")
    status: Mapped[str] = mapped_column(sa.Text, server_default="queued")
    # 0 owner /chats qoʻsh · 1 linked discussion group · 2 companion group ·
    # 3 resolved username · 5 seed
    priority: Mapped[int] = mapped_column(sa.SmallInteger, server_default=sa.text("5"))
    monitor_enabled: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())
    decided_by: Mapped[str | None] = mapped_column(sa.Text)
    asked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    joined_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    backfill_requested_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True)
    )
    backfilled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    backfill_attempts: Mapped[int] = mapped_column(
        sa.SmallInteger, server_default=sa.text("0")
    )
    monitoring_since: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_seen_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # Every message seen — counted, never stored.
    seen_count: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    # Prefilter hits stored in interactions.
    hit_count: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    member_count: Mapped[int | None] = mapped_column(sa.Integer)
    flood_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        check_in("chat_monitors", "chat_type", e.CHAT_TYPES),
        check_in("chat_monitors", "role", e.CHAT_ROLES),
        check_in("chat_monitors", "status", e.CHAT_STATUSES),
        sa.CheckConstraint("priority BETWEEN 0 AND 9", name="ck_chat_monitors_priority"),
        check_in("chat_monitors", "decided_by", e.CHAT_DECIDED_BY),
        sa.CheckConstraint(
            "status <> 'joined' OR joined_at IS NOT NULL", name="ck_chat_monitors_joined"
        ),
        sa.Index(
            "ix_chat_monitors_queue",
            "priority",
            "created_at",
            postgresql_where=sa.text("status = 'queued'"),
        ),
    )
