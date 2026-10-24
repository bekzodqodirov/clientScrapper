"""The do-not-contact set (02 §2.8).

One row per (kind, key_type, key). ``KIND_RANK`` — which kind wins when a
company hits several — lives in ``services/suppression.py``, not here.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from clientscrapper.db import enums as e
from clientscrapper.db.base import Base, created_at_column
from clientscrapper.db.enums import sql_list
from clientscrapper.db.models._common import bigint_pk, check_in


class Suppression(Base):
    """A key that must never be carded or pushed: an ERP client or
    «Qayta qilinmasin» phone (hashed by the feed, clear from outcomes and the
    XLSX fallback), the agent's own DNC, a competitor, a cooldown."""

    __tablename__ = "suppression"

    id: Mapped[int] = bigint_pk()
    kind: Mapped[str] = mapped_column(sa.Text)
    key_type: Mapped[str] = mapped_column(sa.Text)
    key: Mapped[str] = mapped_column(sa.Text)
    # phone_hash only: the feed's salt_id the hash was made with.
    salt_id: Mapped[str | None] = mapped_column(sa.CHAR(8))
    reason: Mapped[str | None] = mapped_column(sa.Text)
    source: Mapped[str] = mapped_column(sa.Text)
    # The feed entry's `at`.
    erp_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    # Stamped by every full pull; unseen erp_feed rows are deleted after one.
    last_seen_at: Mapped[datetime] = created_at_column()
    # Cooldown only.
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        check_in("suppression", "kind", e.SUPPRESSION_KINDS),
        check_in("suppression", "key_type", e.SUPPRESSION_KEY_TYPES),
        sa.CheckConstraint("length(key) BETWEEN 3 AND 200", name="ck_suppression_key"),
        sa.CheckConstraint("length(reason) <= 200", name="ck_suppression_reason"),
        check_in("suppression", "source", e.SUPPRESSION_SOURCES),
        sa.CheckConstraint(
            "key_type <> 'phone_tail' OR key ~ '^[0-9]{9}$'",
            name="ck_suppression_phone_tail",
        ),
        sa.CheckConstraint(
            "key_type <> 'phone_hash' "
            "OR (key ~ '^[0-9a-f]{20}$' AND salt_id IS NOT NULL)",
            name="ck_suppression_phone_hash",
        ),
        sa.CheckConstraint(
            "key_type <> 'stir' OR key ~ '^[0-9]{9}$'", name="ck_suppression_stir"
        ),
        sa.CheckConstraint(
            "key_type <> 'tg_username' OR key IS NOT DISTINCT FROM cs_username_key(key)",
            name="ck_suppression_username",
        ),
        sa.CheckConstraint(
            "key_type <> 'domain' OR key IS NOT DISTINCT FROM cs_domain_key(key)",
            name="ck_suppression_domain",
        ),
        sa.CheckConstraint(
            "kind NOT IN ('client', 'lost') "
            f"OR source IN ({sql_list(e.SUPPRESSION_ERP_SOURCES)})",
            name="ck_suppression_erp_kinds",
        ),
        sa.CheckConstraint(
            "(kind = 'cooldown') = (expires_at IS NOT NULL)",
            name="ck_suppression_cooldown",
        ),
        sa.UniqueConstraint("kind", "key_type", "key", name="uq_suppression_key"),
        sa.Index("ix_suppression_lookup", "key_type", "key"),
        sa.Index(
            "ix_suppression_feed_seen",
            "last_seen_at",
            postgresql_where=sa.text("source = 'erp_feed'"),
        ),
        sa.Index(
            "ix_suppression_expiry",
            "expires_at",
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        ),
    )
