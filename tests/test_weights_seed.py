"""Version-1 weights (02 §2.7, 04 §5.1, 07 §1.3).

One row per key of SIGNAL_KEYS ∪ DERIVED_KEYS, caps of the positive groups
summing to 100, approved by the seed; enums.WEIGHTS_V1 (what the scorer's
tests load) equal to the migration's frozen copy and to the seeded rows.
``validate_version``'s ±30 % rule belongs to services/scoring.py (T-18).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from clientscrapper.db import enums as e

ROOT = Path(__file__).resolve().parents[1]
POSITIVE_GROUPS = ("volume", "origin", "solidity", "recency", "contact", "corroboration")
# 04 §5.1, written out: the cap of each group.
CAPS = {
    "volume": 40,
    "origin": 20,
    "solidity": 15,
    "recency": 10,
    "contact": 10,
    "corroboration": 5,
    "negative": 0,
    "gate": 0,
}


def _migration_weights() -> tuple[tuple[object, ...], ...]:
    spec = importlib.util.spec_from_file_location(
        "migration_0001", ROOT / "alembic" / "versions" / "0001_initial.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    weights: tuple[tuple[object, ...], ...] = module.WEIGHTS_V1
    return weights


def test_one_row_per_key_of_both_tuples():
    keys = [w.key for w in e.WEIGHTS_V1]
    assert len(keys) == 26
    assert len(set(keys)) == len(keys)
    assert set(keys) == set(e.SIGNAL_KEYS) | set(e.DERIVED_KEYS)


def test_every_row_of_a_group_carries_the_groups_cap():
    caps = {(w.group_key, w.group_cap) for w in e.WEIGHTS_V1}
    assert caps == set(CAPS.items())
    assert sum(CAPS[g] for g in POSITIVE_GROUPS) == 100
    assert {w.group_key for w in e.WEIGHTS_V1} <= set(e.WEIGHT_GROUPS)


def test_signs_follow_the_groups():
    for w in e.WEIGHTS_V1:
        if w.group_key == "negative":
            assert w.points < 0, w
        elif w.group_key == "gate":
            assert w.points == 0, w
        else:
            assert 0 < w.points <= w.group_cap, w


def test_derived_keys_never_decay():
    # Recency is rolling and contact/corroboration are read at score time.
    assert [w.key for w in e.WEIGHTS_V1 if w.key in e.DERIVED_KEYS and w.decay_days] == []


def test_the_migration_seeds_exactly_the_enums_copy():
    assert _migration_weights() == tuple(tuple(w) for w in e.WEIGHTS_V1)


async def test_version_1_is_seeded_approved_and_equal_to_the_tuple(session):
    rows = (
        await session.execute(
            sa.text(
                "SELECT key, group_key, points, group_cap, decay_days, decayed_factor, "
                "proposed_by, approved_by, approved_at FROM scoring_weights WHERE "
                "version = 1"
            )
        )
    ).all()
    assert len(rows) == 26
    assert {(r.key, r.group_key, r.points, r.group_cap, r.decay_days) for r in rows} == {
        tuple(w) for w in e.WEIGHTS_V1
    }
    assert {(r.proposed_by, r.approved_by) for r in rows} == {("seed", "seed")}
    assert all(r.approved_at is not None for r in rows)
    assert {str(r.decayed_factor) for r in rows} == {"0.50"}
    caps = {r.group_key: r.group_cap for r in rows}
    assert sum(caps[g] for g in POSITIVE_GROUPS) == 100
    (versions,) = (
        await session.execute(
            sa.text("SELECT array_agg(DISTINCT version) FROM scoring_weights")
        )
    ).one()
    assert versions == [1]
