# VENDORED_FROM dailyJournal@6b1f67f miya/db/base.py
"""Declarative base and shared column helpers.

The naming convention is what keeps an autogenerate diff empty: an index,
unique constraint, foreign key or primary key that is not named explicitly
gets the same name in the ORM and in the migration (02 §0). A CHECK must
always be named — ``ck_<table>_<rule>`` written out in full — and the ``ck``
template refuses one that is not.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, MappedColumn, mapped_column

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


def pg_enum(py_enum: type[enum.Enum], name: str) -> sa.Enum:
    """A native PostgreSQL enum whose labels are the member *values*.

    Kept from MIYA but unused: this schema uses CHECK constraints over the
    frozen tuples of ``db/enums.py``, never native enums (02 §0).
    """
    return sa.Enum(
        py_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        native_enum=True,
    )


def created_at_column(**kwargs: Any) -> MappedColumn[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
        **kwargs,
    )


def updated_at_column() -> MappedColumn[datetime]:
    """``onupdate`` covers ORM updates only; the bot, userbot and worker also
    write raw SQL, so the ``cs_touch_updated_at`` trigger is the real writer."""
    return created_at_column(onupdate=sa.func.now())


def money_column(**kwargs: Any) -> MappedColumn[Any]:
    """Money is NUMERIC — never float."""
    return mapped_column(sa.Numeric(14, 2), **kwargs)


__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "created_at_column",
    "money_column",
    "pg_enum",
    "updated_at_column",
]
