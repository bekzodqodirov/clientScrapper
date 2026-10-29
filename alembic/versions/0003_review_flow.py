"""The review flow's two homes (06 §1.3; 07 §4.1 T-21): T-21.

* ``lead_reviews.remind_at`` — the moment a card the owner (or a manager)
  put off with ⏭ is asked again. 06 §1.3 says «⏭ Keyinroq — ertaga qayta
  soʻrayman»: the row stays ``asked`` with ``expires_at += 24 h`` (02 §2.9),
  its message loses its buttons, and ``review_cards_job`` sends the card
  again as a fresh message once ``remind_at`` has passed. Neither
  ``expires_at`` (the deadline) nor ``asked_at`` (when the owner was first
  shown the card — the backlog's age, 06 §4.2) can say when that is.
* ``lead_candidates.push_note`` / ``push_noted_at`` — the home of an
  auto-approved candidate's push refusals (04 §7 item 14, 07 §4.1 T-19/T-21's
  carried note): such a candidate has no owner review row to write the note
  on, and a row nobody tapped would read as the owner's decision. ``/lidlar
  holat`` lists them.

Additive only: 0001 and 0002 are not edited. Built as 0004 in its lane (0003
was held for the T-20 lane, which needed none) and renumbered 0003 at the
wave-4 integration, before any database had applied it.

Revision ID: 0003_review_flow
Revises: 0002_erp_contract
Create Date: T-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_review_flow"
down_revision: str | None = "0002_erp_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen: lead_reviews.note's own bound (0001), the same for the push note.
_NOTE_MAX = 500


def upgrade() -> None:
    op.add_column(
        "lead_reviews",
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_lead_reviews_remind",
        "lead_reviews",
        ["remind_at"],
        postgresql_where=sa.text("state = 'asked' AND remind_at IS NOT NULL"),
    )
    op.add_column("lead_candidates", sa.Column("push_note", sa.Text(), nullable=True))
    op.add_column(
        "lead_candidates",
        sa.Column("push_noted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_lead_candidates_push_note",
        "lead_candidates",
        f"length(push_note) <= {_NOTE_MAX}",
    )


def downgrade() -> None:
    op.drop_constraint("ck_lead_candidates_push_note", "lead_candidates", type_="check")
    op.drop_column("lead_candidates", "push_noted_at")
    op.drop_column("lead_candidates", "push_note")
    op.drop_index("ix_lead_reviews_remind", table_name="lead_reviews")
    op.drop_column("lead_reviews", "remind_at")
