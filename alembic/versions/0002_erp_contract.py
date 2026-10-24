"""The ERP contract as built (02 §5, 05 §B.2, 2026-10-24 «T-07 as built»): T-10.

* ``outcomes.no_contact`` — the ERP's own «lost with a reason
  ``agent_no_contact_reasons`` lists» (05 §A.9); the agent keeps no copy of
  that setting, so the flag is stored as the feed sent it.
* ``push_log.superseded_at`` — rule 5's one re-push and ``/push tekshir`` set
  it on a delivered row instead of rewriting its status: ``ck_push_log_sent``
  forbids ``failed`` on a 2xx row (02 §2.10 rule 5).
* ``ux_push_log_delivered`` narrowed to the rows nobody superseded, so the
  re-push's ``attempt + 1`` can be delivered under the same ``external_id``.

Additive only: 0001 is not edited (T-02 shipped it). The value list of the
index predicate is frozen here like 0001's (``_DELIVERED``).

``downgrade`` puts 0001's index back; it fails — on purpose, nothing is
deleted to make it pass — once a superseded row and its re-push both stand
delivered under one ``external_id``.

Revision ID: 0002_erp_contract
Revises: 0001_initial
Create Date: T-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_erp_contract"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen: push_log statuses that block a second delivery (enums
# PUSH_DELIVERED_STATUSES the day this ran).
_DELIVERED = ("sent", "unknown")
_DELIVERED_SQL = "status IN (" + ", ".join(f"'{v}'" for v in _DELIVERED) + ")"
_INDEX = "ux_push_log_delivered"


def upgrade() -> None:
    op.add_column(
        "outcomes",
        sa.Column("no_contact", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "push_log", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_index(_INDEX, table_name="push_log")
    op.create_index(
        _INDEX,
        "push_log",
        ["external_id"],
        unique=True,
        postgresql_where=sa.text(f"{_DELIVERED_SQL} AND superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="push_log")
    op.create_index(
        _INDEX,
        "push_log",
        ["external_id"],
        unique=True,
        postgresql_where=sa.text(_DELIVERED_SQL),
    )
    op.drop_column("push_log", "superseded_at")
    op.drop_column("outcomes", "no_contact")
