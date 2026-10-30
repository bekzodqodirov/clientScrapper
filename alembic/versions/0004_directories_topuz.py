"""top.uz's own attribution row (03 §2.6; the T-22 review): T-22.

top.uz had no ``sources`` row: its evidence and observations credited the
parent ``directories``, the row the runner schedules for every directory
site. So the weekly precision job's pause — which lands on the row a lead is
credited to (05 §B.2, 06 Open question 23 (a): «pause that sub-source alone,
the fan-out collectors skip it») — stopped goldenpages, yellowpages and glotr
for a week whenever top.uz alone was weak; and the owner had no switch for
top.uz alone, which the fan-out reads on a site's own row (seeded off, turned
on after the site's day-1 check). 02 §5: «new source = a data migration
inserting the ``sources`` row». One attribution-only row, the shape of
``directories_goldenpages``: family ``directory``, ``enabled=false``, no
cron, no budget, rps 0.3.

Data only: 0001–0003 are not edited. ``downgrade`` files back under the
parent what the row holds (every ``source_key`` that references it, and the
copy inside ``observations.parsed``), then deletes the row — nothing a run
wrote is lost.

Revision ID: 0004_directories_topuz
Revises: 0003_review_flow
Create Date: T-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_directories_topuz"
down_revision: str | None = "0003_review_flow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen: the row as inserted, and the parent its rows return to.
_KEY = "directories_topuz"
_PARENT = "directories"
# Every column that references sources.key (02 §2), as of 0003.
_REFERENCES = (
    ("evidence", "source_key"),
    ("observations", "source_key"),
    ("signals", "source_key"),
    ("company_aliases", "source_key"),
    ("company_ids", "source_key"),
    ("contacts", "source_key"),
    ("lead_candidates", "primary_source_key"),
    ("outcomes", "source_key"),
    ("usage_log", "source_key"),
    ("collector_runs", "source_key"),
)


def upgrade() -> None:
    sources = sa.table(
        "sources",
        sa.column("key", sa.Text()),
        sa.column("family", sa.Text()),
        sa.column("title_uz", sa.Text()),
        sa.column("enabled", sa.Boolean()),
        sa.column("cron", sa.Text()),
        sa.column("rps", sa.Numeric(6, 3)),
        sa.column("daily_page_budget", sa.Integer()),
        sa.column("daily_cost_cap_usd", sa.Numeric(8, 4)),
    )
    op.bulk_insert(
        sources,
        [
            {
                "key": _KEY,
                "family": "directory",
                "title_uz": "top.uz",
                "enabled": False,
                "cron": None,
                "rps": "0.3",
                "daily_page_budget": 0,
                "daily_cost_cap_usd": "0",
            }
        ],
    )


def downgrade() -> None:
    for table, column in _REFERENCES:
        op.execute(
            f"UPDATE {table} SET {column} = '{_PARENT}' WHERE {column} = '{_KEY}'"
        )
    op.execute(
        "UPDATE observations SET parsed = jsonb_set(parsed, '{source_key}', "
        f"'\"{_PARENT}\"') WHERE parsed->>'source_key' = '{_KEY}'"
    )
    op.execute(f"DELETE FROM sources WHERE key = '{_KEY}'")
