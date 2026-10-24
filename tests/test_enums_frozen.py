"""The frozen value lists: enums.py, the migration's copy, the models and the
live CHECKs say the same thing (02 §0, §10 AC 2).

The pure half needs no database. The live half reads every ``… IN (…)`` CHECK
back from ``pg_constraint`` on the test database.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa

from clientscrapper.db import enums as e
from clientscrapper.db import models as _models  # noqa: F401 - populates Base.metadata
from clientscrapper.db.base import Base

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0001_initial.py"
IN_LIST = re.compile(r"^(\w+) IN \((.*)\)$")


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0001", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _str_tuples(module: ModuleType) -> dict[str, tuple[str, ...]]:
    """Public UPPER_CASE tuples of strings: the frozen value lists."""
    return {
        name: value
        for name, value in vars(module).items()
        if name.isupper()
        and not name.startswith("_")
        and isinstance(value, tuple)
        and value
        and all(isinstance(v, str) for v in value)
    }


def _in_checks() -> dict[str, tuple[str, tuple[str, ...]]]:
    """Every model CHECK of the shape ``col IN ('a', 'b')``: name → (col, values)."""
    found = {}
    for table in Base.metadata.tables.values():
        for ck in table.constraints:
            if not isinstance(ck, sa.CheckConstraint):
                continue
            match = IN_LIST.match(str(ck.sqltext))
            if match:
                values = tuple(re.findall(r"'([^']*)'", match.group(2)))
                found[str(ck.name)] = (match.group(1), values)
    return found


def test_every_enum_list_has_its_frozen_copy_in_0001_and_back():
    ours = _str_tuples(e)
    theirs = _str_tuples(_migration())
    assert set(ours) == set(theirs)
    assert {name: ours[name] for name in ours if ours[name] != theirs[name]} == {}


def test_the_values_within_a_list_are_unique():
    assert {n: v for n, v in _str_tuples(e).items() if len(set(v)) != len(v)} == {}


def test_every_in_list_check_of_the_models_is_an_enums_tuple():
    # No ad-hoc literal list in a model: each IN (…) is one of the frozen tuples.
    lists = set(_str_tuples(e).values()) | {e.SIGNAL_KEYS + e.DERIVED_KEYS}
    stray = {
        name: values for name, (_, values) in _in_checks().items() if values not in lists
    }
    assert stray == {}


def test_the_weights_key_check_is_signal_and_derived_keys():
    column, values = _in_checks()["ck_scoring_weights_key"]
    assert column == "key"
    assert set(values) == set(e.SIGNAL_KEYS) | set(e.DERIVED_KEYS)
    assert not set(e.SIGNAL_KEYS) & set(e.DERIVED_KEYS)


async def test_the_live_checks_hold_the_same_values(session):
    rows = (
        await session.execute(
            sa.text(
                "SELECT conname, pg_get_constraintdef(oid) AS definition FROM "
                "pg_constraint "
                "WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
            )
        )
    ).all()
    live = {r.conname: r.definition for r in rows}
    checks = _in_checks()
    assert len(checks) >= 40  # the guard must have something to guard
    differs = {}
    for name, (column, values) in checks.items():
        definition = live.get(name, "")
        if column not in definition or set(re.findall(r"'([^']*)'", definition)) != set(
            values
        ):
            differs[name] = definition or "missing"
    assert differs == {}
