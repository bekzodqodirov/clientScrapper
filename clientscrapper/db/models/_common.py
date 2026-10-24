"""Column and constraint helpers shared by the model modules."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import MappedColumn, mapped_column

from clientscrapper.db.enums import sql_list

EMPTY_JSONB = sa.text("'{}'::jsonb")
EMPTY_ARRAY = sa.text("'{}'")
UUID_DEFAULT = sa.text("gen_random_uuid()")


def check_in(table: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    """``ck_<table>_<column>``: the column takes one of the frozen values."""
    return sa.CheckConstraint(
        f"{column} IN ({sql_list(values)})", name=f"ck_{table}_{column}"
    )


def bigint_pk() -> MappedColumn[int]:
    """``bigint GENERATED ALWAYS AS IDENTITY`` — the ledgers' key (02 §0)."""
    return mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
