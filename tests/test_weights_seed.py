"""Version-1 weights (02 §2.7, 04 §5.1, 07 §1.3).

04 §5.1: «the table above is the seeded version 1 and tests/test_weights_seed.py
holds the two equal». So the table is written out here, by hand from the spec,
as the oracle: enums.WEIGHTS_V1 (what the scorer's tests load), the
migration's frozen copy and the seeded rows must each equal it — a typo made
in both enums.py and 0001 is still a difference from the spec. Plus: one row
per key of SIGNAL_KEYS ∪ DERIVED_KEYS, caps of the positive groups summing to
100, approved by the seed. And services/scoring.validate_version (T-18),
which a later version passes before it is approved: completeness, caps, and
each weight within ±30 % of the previous version unless the owner proposed it.
"""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from clientscrapper.db import enums as e
from clientscrapper.services import scoring

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


# 04 §5.1 (= 02 §2.7), row by row: key → (group, points, decay days). «—» and
# «rolling» are None. decayed_factor is 0.5 for every row («×0.5 after»).
SPEC_TABLE: dict[str, tuple[str, int, int | None]] = {
    "vol_container": ("volume", 40, 180),
    "vol_stated_3plus": ("volume", 30, 180),
    "vol_stated_1_3": ("volume", 20, 180),
    "vol_wholesale_bulky": ("volume", 20, 365),
    "vol_marketplace_velocity": ("volume", 20, 60),
    "vol_vacancy_import": ("volume", 15, 180),
    "vol_exhibitor": ("volume", 15, 365),
    "vol_directory_wholesale": ("volume", 10, 365),
    "cn_origin": ("origin", 20, 365),
    "cn_sector_prior": ("origin", 10, None),
    "ent_legal_confirmed": ("solidity", 8, None),
    "ent_age_2y_active": ("solidity", 4, None),
    "ent_not_micro": ("solidity", 3, None),
    "derived_fresh_7d": ("recency", 10, None),
    "derived_fresh_30d": ("recency", 7, None),
    "derived_fresh_90d": ("recency", 4, None),
    "derived_contact_mobile": ("contact", 6, None),
    "derived_contact_decision_maker": ("contact", 2, None),
    "derived_contact_dm_channel": ("contact", 2, None),
    "derived_corroboration": ("corroboration", 5, None),
    "neg_b2c_words": ("negative", -40, 180),
    "neg_liquidated": ("negative", -50, None),
    "neg_non_cn_origin": ("negative", -20, 365),
    "neg_state_body": ("negative", -20, None),
    "flag_competitor": ("gate", 0, None),
    "flag_supplier_cn": ("gate", 0, None),
}


def _spec_rows() -> set[tuple[object, ...]]:
    """The spec table as (key, group_key, points, group_cap, decay_days)."""
    return {
        (key, group, points, CAPS[group], decay)
        for key, (group, points, decay) in SPEC_TABLE.items()
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


def test_both_copies_are_the_spec_table():
    assert len(SPEC_TABLE) == 26
    assert {tuple(w) for w in e.WEIGHTS_V1} == _spec_rows()
    assert set(_migration_weights()) == _spec_rows()


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
    seeded = {(r.key, r.group_key, r.points, r.group_cap, r.decay_days) for r in rows}
    assert seeded == {tuple(w) for w in e.WEIGHTS_V1}
    assert seeded == _spec_rows()
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


# --- validate_version (02 §2.7, 07 §1.3 «Weights contract») --------------------

V1 = scoring.weights_v1()


def _with(key: str, **change: object) -> scoring.WeightSet:
    rows = dict(V1.rows)
    rows[key] = replace(rows[key], **change)  # type: ignore[arg-type]
    return scoring.WeightSet(version=2, rows=rows)


def test_version_1_against_itself_is_valid():
    assert scoring.validate_version(V1, previous=None, proposed_by="seed") == []
    copy = scoring.WeightSet(version=2, rows=dict(V1.rows))
    assert (
        scoring.validate_version(copy, previous=V1, proposed_by="fit_weights@2026-11-01")
        == []
    )


def test_an_incomplete_version_is_refused():
    rows = {k: w for k, w in V1.rows.items() if k != "derived_corroboration"}
    problems = scoring.validate_version(
        scoring.WeightSet(version=2, rows=rows), previous=V1, proposed_by="seed"
    )
    assert "missing:derived_corroboration" in problems


@pytest.mark.parametrize(
    ("points", "ok"),
    [(21, True), (30, True), (39, True), (20, False), (40, False)],
)
def test_each_weight_moves_at_most_thirty_percent(points, ok):
    """vol_stated_3plus is 30 in version 1: 21 … 39 pass, 20 and 40 do not."""
    candidate = _with("vol_stated_3plus", points=points)
    problems = scoring.validate_version(
        candidate, previous=V1, proposed_by="fit_weights@x"
    )
    assert problems == ([] if ok else ["jump:vol_stated_3plus"])


def test_a_thirty_one_percent_jump_is_refused_unless_the_owner_proposed_it():
    up = _with("vol_stated_3plus", points=40)  # +33 %
    down = _with("vol_stated_1_3", points=13)  # −35 %
    edge = _with("vol_stated_1_3", points=26)  # +30 % exactly
    for candidate, key in ((up, "vol_stated_3plus"), (down, "vol_stated_1_3")):
        assert f"jump:{key}" in scoring.validate_version(
            candidate, previous=V1, proposed_by="fit_weights@2026-11-01"
        )
        assert (
            scoring.validate_version(candidate, previous=V1, proposed_by="owner:1001")
            == []
        )
    assert scoring.validate_version(edge, previous=V1, proposed_by="seed") == []


def test_a_zero_weight_stays_zero_and_signs_stay():
    gate = _with("flag_competitor", points=5)
    assert "points:flag_competitor" in scoring.validate_version(
        gate, previous=V1, proposed_by="owner:1001"
    )
    flipped = _with("neg_b2c_words", points=10)
    assert "points:neg_b2c_words" in scoring.validate_version(
        flipped, previous=V1, proposed_by="owner:1001"
    )


@pytest.mark.parametrize(("points", "ok"), [(40, True), (41, False), (0, False)])
def test_a_positive_key_is_worth_more_than_nothing_and_at_most_its_cap(points, ok):
    """vol_container's cap is the volume group's 40: 41 could never count in
    full, and 0 is no positive key."""
    candidate = _with("vol_container", points=points)
    problems = scoring.validate_version(candidate, previous=V1, proposed_by="owner:1001")
    assert problems == ([] if ok else ["points:vol_container"])


def test_caps_must_agree_and_sum_to_a_hundred():
    split = _with("vol_container", cap=30)
    problems = scoring.validate_version(split, previous=V1, proposed_by="owner:1001")
    assert "caps:volume" in problems
    rows = {
        k: (replace(w, cap=35) if w.group == "volume" else w) for k, w in V1.rows.items()
    }
    rows["vol_container"] = replace(rows["vol_container"], points=35)
    problems = scoring.validate_version(
        scoring.WeightSet(version=2, rows=rows), previous=V1, proposed_by="owner:1001"
    )
    assert "caps_sum:95" in problems


def test_a_key_may_not_change_group():
    moved = _with("derived_fresh_7d", group="volume", cap=40)
    assert "group_changed:derived_fresh_7d" in scoring.validate_version(
        moved, previous=V1, proposed_by="owner:1001"
    )


async def test_activation_approves_a_valid_version_and_refuses_a_jump(session):
    """A version 2 inserted and approved, then rolled back: scoring_weights is
    seeded configuration the test truncate keeps (07 §1.0)."""
    now = datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo("Asia/Tashkent"))
    insert = sa.text(
        "INSERT INTO scoring_weights (version, key, group_key, points, group_cap, "
        "decay_days, proposed_by) SELECT 2, key, group_key, "
        "CASE WHEN key = 'vol_stated_3plus' THEN :p ELSE points END, group_cap, "
        "decay_days, :by FROM scoring_weights WHERE version = 1"
    )
    try:
        await session.execute(insert, {"p": 40, "by": "fit_weights@2026-11-01"})
        with pytest.raises(scoring.VersionRefused) as refused:
            await scoring.activate_version(
                session, 2, by="fit_weights@2026-11-01", now=now
            )
        assert refused.value.problems == ("jump:vol_stated_3plus",)
        assert await scoring.active_version(session) == 1
        await session.execute(
            sa.text(
                "UPDATE scoring_weights SET points = 36 "
                "WHERE version = 2 AND key = 'vol_stated_3plus'"
            )
        )
        await scoring.activate_version(session, 2, by="owner:1001", now=now)
        assert await scoring.active_version(session) == 2
        loaded = await scoring.load_weights(session)
        assert (loaded.version, loaded.points("vol_stated_3plus")) == (2, 36)
        with pytest.raises(scoring.VersionRefused) as again:
            await scoring.activate_version(session, 2, by="owner:1001", now=now)
        assert again.value.problems == ("already_approved",)
    finally:
        await session.rollback()
    assert await scoring.active_version(session) == 1
