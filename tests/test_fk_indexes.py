"""Every foreign key that cascades, nulls or restricts has an index (02 §0, MIYA
WP-23).

Without one, deleting a parent row scans the child table once per row — the
cost lands on retention and purge, which delete in bulk. RESTRICT is checked
on every parent delete too, so it counts (companies.merged_into turned
RESTRICT in the T-01/T-02 review and must keep its index). No database: walks
``Base.metadata``; an index, a unique constraint or the primary key counts
when the FK columns are its leading columns.
"""

from __future__ import annotations

import sqlalchemy as sa

from clientscrapper.db import models as _models  # noqa: F401 - populates Base.metadata
from clientscrapper.db.base import Base


def _leading_column_sets(table: sa.Table) -> list[tuple[str, ...]]:
    sets = [tuple(c.name for c in index.columns) for index in table.indexes]
    sets += [
        tuple(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint | sa.PrimaryKeyConstraint)
    ]
    return [s for s in sets if s]


def _cascading_fks() -> list[tuple[str, tuple[str, ...]]]:
    return [
        (table.name, tuple(c.name for c in fk.columns))
        for table in Base.metadata.tables.values()
        for fk in table.foreign_key_constraints
        if (fk.ondelete or "").upper() in {"CASCADE", "SET NULL", "RESTRICT"}
    ]


def test_every_cascading_or_nulling_fk_is_indexed():
    missing = [
        f"{table}({', '.join(cols)})"
        for table, cols in _cascading_fks()
        if not any(
            s[: len(cols)] == cols
            for s in _leading_column_sets(Base.metadata.tables[table])
        )
    ]
    assert missing == []


def test_the_walk_sees_the_foreign_keys_it_guards():
    # A walk that found no FKs would pass for the wrong reason.
    fks = _cascading_fks()
    assert len(fks) >= 29  # 0001 has 25 cascading or nulling, 4 restricting
    assert ("companies", ("merged_into",)) in fks
    assert ("signals", ("model_job_id",)) in fks
    assert ("company_aliases", ("company_id",)) in fks
