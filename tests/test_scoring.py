"""The score function's golden tests (07 §1.3; 04 §5, §9 — no database).

Inputs are SignalView/ContactView fixtures written from the worked examples'
own words; weights are version 1 from the seed tuple; ``now`` is fixed. The
expected numbers are the spec's, never recomputed here: 04 §9.1 52 → 70,
§9.2 65, §9.3 48 → 87, and 04 §5.2's printed breakdown for §9.1 is compared
key by key. Synthetic data only (07 §1.0).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from jsonschema import Draft202012Validator

from clientscrapper.services import scoring
from clientscrapper.services.scoring import ContactView, SignalView, score

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 20, 7, 53, tzinfo=TZ)
V1 = scoring.weights_v1()
# sources.family of the seeded rows these fixtures name (02 §2.1).
FAMILIES = {
    "telegram_monitor": "telegram",
    "registry_lookup": "registry",
    "directories_yellowpages": "directory",
    "olx_business": "olx",
    "hh_vacancies": "vacancy",
    "enrich_site": "search",
}
SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "clientscrapper"
        / "schemas"
        / "score_breakdown.json"
    ).read_text(encoding="utf-8")
)
VALIDATOR = Draft202012Validator(SCHEMA)


def sig(
    id: int,
    key: str,
    *,
    source: str = "telegram_monitor",
    age: timedelta = timedelta(0),
    evidence: int = 7001,
    value: str | None = None,
) -> SignalView:
    return SignalView(
        id=id,
        key=key,
        observed_at=NOW - age,
        evidence_id=evidence,
        source_key=source,
        value=Decimal(value) if value is not None else None,
    )


def run(signals, contacts=(), *, now=NOW) -> scoring.ScoreResult:
    result = score(
        list(signals), list(contacts), source_families=FAMILIES, weights=V1, now=now
    )
    VALIDATOR.validate(result.breakdown)
    return result


def used(result: scoring.ScoreResult) -> dict[str, int | float]:
    return {g: v["used"] for g, v in result.breakdown["groups"].items()}


# --- 04 §9.1 -------------------------------------------------------------------------

POSTER = ContactView(tier="person", telegram_username="namuna_tekstil")
SIGNALS_91 = (
    sig(8101, "vol_stated_1_3", value="2"),
    sig(8102, "cn_origin"),
)


# 04 §5.2's printed breakdown, verbatim (its gates are T-19's).
SPEC_5_2 = """
{"version": 1, "computed_at": "2026-10-20T07:53:00+05:00",
 "groups": {"volume": {"cap": 40, "raw": 20, "used": 20, "signals": [{"key":
 "vol_stated_1_3", "points": 20, "factor": 1.0, "signal_id": 8101, "evidence_id": 7001,
 "value": 2.0}]},
 "origin": {"cap": 20, "raw": 20, "used": 20, "signals": [{"key": "cn_origin",
 "points": 20, "factor": 1.0, "signal_id": 8102, "evidence_id": 7001}]},
 "solidity": {"cap": 15, "raw": 0, "used": 0, "signals": []},
 "recency": {"cap": 10, "raw": 10, "used": 10, "signals": [{"key": "derived_fresh_7d",
 "points": 10, "age_days": 0}]},
 "contact": {"cap": 10, "raw": 2, "used": 2, "signals": [{"key":
 "derived_contact_dm_channel", "points": 2}]},
 "corroboration": {"cap": 5, "raw": 0, "used": 0, "families": ["telegram"]}},
 "negatives": [], "total": 52, "tier": "warm"}
"""


def test_9_1_the_telegram_post_before_enrichment_is_52_warm():
    result = run(SIGNALS_91, [POSTER])
    assert (result.total, result.tier, result.usable_phone) == (52, "warm", False)
    assert used(result) == {
        "volume": 20,
        "origin": 20,
        "solidity": 0,
        "recency": 10,
        "contact": 2,
        "corroboration": 0,
    }


def test_9_1_reproduces_the_breakdown_04_section_5_2_prints():
    """04 §5.2's JSON is this candidate's score: groups, negatives, total and
    tier as printed (the gates there are T-19's, filled later)."""
    printed = json.loads(SPEC_5_2)
    got = run(SIGNALS_91, [POSTER]).breakdown
    for key in ("version", "computed_at", "groups", "negatives", "total", "tier"):
        assert got[key] == printed[key], key


def test_9_1_after_the_registry_and_the_directory_phone_is_70_hot():
    signals = (
        *SIGNALS_91,
        sig(8103, "ent_legal_confirmed", source="registry_lookup", evidence=7002),
        sig(8104, "ent_age_2y_active", source="registry_lookup", evidence=7002),
    )
    phone = ContactView(tier="company", phone="+998900000001", phone_kind="mobile")
    result = run(signals, [POSTER, phone], now=NOW + timedelta(hours=1))
    assert (result.total, result.tier, result.usable_phone) == (70, "hot", True)
    assert used(result) == {
        "volume": 20,
        "origin": 20,
        "solidity": 12,
        "recency": 10,
        "contact": 8,
        # registry/directory are not volume/origin families (04 §9.1).
        "corroboration": 0,
    }
    contact = result.breakdown["groups"]["contact"]["signals"]
    assert contact == [
        {"key": "derived_contact_dm_channel", "points": 2},
        {"key": "derived_contact_mobile", "points": 6},
    ]


# --- 04 §9.2 -------------------------------------------------------------------------


def test_9_2_the_olx_seller_without_a_phone_is_65_warm():
    three_days = timedelta(days=3)
    signals = (
        sig(9101, "vol_wholesale_bulky", source="olx_business", age=three_days),
        sig(
            9102,
            "vol_marketplace_velocity",
            source="olx_business",
            age=three_days,
            value="14",
        ),
        sig(9103, "cn_sector_prior", source="olx_business", age=three_days),
        sig(9104, "ent_not_micro", source="olx_business", age=three_days),
    )
    seller = ContactView(tier="person", telegram_username="sardor_optom_tex")
    result = run(signals, [seller])
    assert (result.total, result.tier, result.usable_phone) == (65, "warm", False)
    assert used(result) == {
        "volume": 40,
        "origin": 10,
        "solidity": 3,
        "recency": 10,
        "contact": 2,
        "corroboration": 0,
    }
    volume = result.breakdown["groups"]["volume"]
    assert (volume["raw"], volume["used"]) == (40, 40)
    assert result.breakdown["groups"]["recency"]["signals"] == [
        {"key": "derived_fresh_7d", "points": 10, "age_days": 3}
    ]


# --- 04 §9.3 -------------------------------------------------------------------------

TWO_DAYS = timedelta(days=2)
SIGNALS_93 = (
    sig(9301, "vol_vacancy_import", source="hh_vacancies", age=TWO_DAYS, evidence=7301),
    sig(9302, "cn_origin", source="hh_vacancies", age=TWO_DAYS, evidence=7301),
    sig(9303, "ent_not_micro", source="hh_vacancies", age=TWO_DAYS, evidence=7302),
)


def test_9_3_the_vacancy_before_enrichment_is_48_warm():
    result = run(SIGNALS_93)
    assert (result.total, result.tier) == (48, "warm")
    assert used(result) == {
        "volume": 15,
        "origin": 20,
        "solidity": 3,
        "recency": 10,
        "contact": 0,
        "corroboration": 0,
    }


def test_9_3_after_the_site_and_the_registry_is_87_hot_on_a_landline():
    signals = (
        *SIGNALS_93,
        sig(9304, "vol_wholesale_bulky", source="enrich_site", evidence=7303),
        sig(9305, "ent_legal_confirmed", source="registry_lookup", evidence=7304),
        sig(9306, "ent_age_2y_active", source="registry_lookup", evidence=7304),
    )
    contacts = [
        ContactView(tier="company", phone="+998712000001", phone_kind="landline"),
        # the company's own channel @texsnab_uz (companies.telegram_channel)
        ContactView(tier="company", telegram_username="texsnab_uz"),
    ]
    result = run(signals, contacts)
    assert (result.total, result.tier, result.usable_phone) == (87, "hot", True)
    assert used(result) == {
        "volume": 35,
        "origin": 20,
        "solidity": 15,
        "recency": 10,
        # dm_channel only: a landline is usable but earns no mobile points.
        "contact": 2,
        "corroboration": 5,
    }
    assert result.breakdown["groups"]["corroboration"]["families"] == [
        "search",
        "vacancy",
    ]


# --- caps, decay, negatives, clamp ---------------------------------------------------


def test_caps_bind():
    result = run([sig(1, "vol_container"), sig(2, "vol_stated_3plus")])
    volume = result.breakdown["groups"]["volume"]
    assert (volume["raw"], volume["used"]) == (70, 40)


@pytest.mark.parametrize(
    ("days", "points", "factor"),
    [(181, 15, 0.5), (180, 30, 1), (179, 30, 1)],
)
def test_decay_halves_a_signal_past_its_days(days, points, factor):
    result = run([sig(1, "vol_stated_3plus", age=timedelta(days=days))])
    [entry] = result.breakdown["groups"]["volume"]["signals"]
    assert (entry["points"], entry["factor"]) == (points, factor)


def test_a_decayed_fifteen_is_seven_and_a_half_in_the_breakdown():
    result = run(
        [sig(1, "vol_vacancy_import", source="hh_vacancies", age=timedelta(days=181))]
    )
    [entry] = result.breakdown["groups"]["volume"]["signals"]
    assert entry["points"] == 7.5
    assert result.breakdown["groups"]["volume"]["used"] == 7.5


def test_a_b2c_negative_clamps_the_total_at_zero():
    result = run([sig(1, "cn_origin"), sig(2, "neg_b2c_words")])
    assert result.total == 0
    assert result.breakdown["negatives"] == [
        {
            "key": "neg_b2c_words",
            "points": -40,
            "factor": 1,
            "signal_id": 2,
            "evidence_id": 7001,
        }
    ]


def test_negatives_decay_like_positives():
    result = run(
        [
            sig(1, "vol_container"),
            sig(2, "cn_origin"),
            sig(3, "neg_non_cn_origin", age=timedelta(days=366)),
        ]
    )
    # 40 + 20 + recency 10 − 20 × 0.5
    assert result.total == 60
    assert result.breakdown["negatives"][0]["points"] == -10


FULL = (
    sig(1, "vol_container", source="telegram_monitor"),
    sig(2, "cn_origin", source="olx_business"),
    sig(3, "ent_legal_confirmed", source="registry_lookup"),
    sig(4, "ent_age_2y_active", source="registry_lookup"),
    sig(5, "ent_not_micro", source="registry_lookup"),
)
EVERY_CONTACT = (
    ContactView(tier="company", phone="+998900000002", phone_kind="mobile"),
    ContactView(tier="person", is_decision_maker=True, telegram_username="namuna_1"),
)


def test_every_positive_group_full_is_100_and_never_more():
    result = run(FULL, EVERY_CONTACT)
    assert result.total == 100
    assert sum(used(result).values()) == 100


def test_a_negative_takes_from_a_full_hundred():
    result = run([*FULL, sig(6, "neg_state_body")], EVERY_CONTACT)
    assert result.total == 80


def test_negatives_alone_are_zero_never_below():
    result = run(
        [sig(1, k) for k in ("neg_b2c_words", "neg_liquidated", "neg_state_body")]
    )
    assert result.total == 0
    assert result.tier == "cold"


def test_a_key_counts_once_its_best_signal():
    old = sig(1, "vol_stated_3plus", age=timedelta(days=200), evidence=1)
    new = sig(2, "vol_stated_3plus", age=timedelta(days=1), evidence=2)
    result = run([old, new])
    [entry] = result.breakdown["groups"]["volume"]["signals"]
    assert (entry["signal_id"], entry["points"]) == (2, 30)


def test_a_negative_key_counts_once_its_heaviest():
    old = sig(1, "neg_b2c_words", age=timedelta(days=200), evidence=1)
    new = sig(2, "neg_b2c_words", evidence=2)
    result = run([sig(3, "vol_container"), old, new])
    [entry] = result.breakdown["negatives"]
    assert (entry["signal_id"], entry["points"]) == (2, -40)


# --- recency --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "key", "points"),
    [
        (timedelta(days=7), "derived_fresh_7d", 10),
        (timedelta(days=7, seconds=1), "derived_fresh_30d", 7),
        (timedelta(days=30), "derived_fresh_30d", 7),
        (timedelta(days=31), "derived_fresh_90d", 4),
        (timedelta(days=90), "derived_fresh_90d", 4),
    ],
)
def test_recency_is_the_age_of_the_newest_volume_or_origin_signal(age, key, points):
    result = run([sig(1, "vol_exhibitor", source="olx_business", age=age)])
    [entry] = result.breakdown["groups"]["recency"]["signals"]
    assert (entry["key"], entry["points"]) == (key, points)


def test_no_recency_past_ninety_days_or_without_volume_and_origin():
    stale = run([sig(1, "vol_exhibitor", age=timedelta(days=91))])
    assert stale.breakdown["groups"]["recency"]["signals"] == []
    solid_only = run([sig(1, "ent_not_micro"), sig(2, "cn_sector_prior")])
    assert solid_only.breakdown["groups"]["recency"]["used"] == 0
    assert solid_only.newest_signal_at is None


def test_recency_reads_the_newest_signal_even_a_decayed_one():
    result = run(
        [
            sig(1, "vol_stated_3plus", age=timedelta(days=181)),
            sig(2, "ent_not_micro"),
        ]
    )
    assert result.newest_signal_at == NOW - timedelta(days=181)
    assert result.breakdown["groups"]["recency"]["signals"] == []


# --- contacts -------------------------------------------------------------------------


def test_a_shared_mobile_scores_nothing_and_is_not_usable():
    shared = ContactView(phone="+998900000003", phone_kind="mobile", shared_phone=True)
    result = run(FULL, [shared])
    assert result.usable_phone is False
    assert result.breakdown["groups"]["contact"]["signals"] == []
    assert result.tier == "warm"  # ≥ 60 without a usable phone


@pytest.mark.parametrize(
    "contact",
    [
        ContactView(phone="+998900000004", phone_kind="mobile", anonymised=True),
        ContactView(phone="+998200000020", phone_kind="unknown"),  # 04 Q4
        ContactView(phone="+79260000000", phone_kind="unknown"),
        ContactView(tier="person", is_decision_maker=True, anonymised=True),
        ContactView(telegram_username="namuna_2", anonymised=True),
    ],
)
def test_contacts_that_earn_nothing(contact):
    result = run([sig(1, "vol_container")], [contact])
    assert result.breakdown["groups"]["contact"]["signals"] == []
    assert result.usable_phone is False


def test_a_decision_maker_needs_the_person_tier():
    company_flag = ContactView(tier="company", is_decision_maker=True)
    person = ContactView(tier="person", is_decision_maker=True)
    assert run([], [company_flag]).breakdown["groups"]["contact"]["signals"] == []
    assert run([], [person]).breakdown["groups"]["contact"]["signals"] == [
        {"key": "derived_contact_decision_maker", "points": 2}
    ]


def test_an_instagram_account_is_a_dm_channel():
    result = run([], [ContactView(instagram="namuna_shop")])
    assert result.breakdown["groups"]["contact"]["signals"] == [
        {"key": "derived_contact_dm_channel", "points": 2}
    ]


# --- corroboration --------------------------------------------------------------------


def test_corroboration_needs_two_families_of_volume_or_origin():
    one = run([sig(1, "vol_container"), sig(2, "cn_origin")])
    assert one.breakdown["groups"]["corroboration"]["used"] == 0
    two = run([sig(1, "vol_container"), sig(2, "cn_origin", source="olx_business")])
    assert two.breakdown["groups"]["corroboration"] == {
        "cap": 5,
        "raw": 5,
        "used": 5,
        "families": ["olx", "telegram"],
    }


def test_solidity_and_unknown_sources_do_not_corroborate():
    result = run(
        [
            sig(1, "vol_container"),
            sig(2, "ent_legal_confirmed", source="registry_lookup"),
            sig(3, "cn_origin", source="not_a_source"),
        ]
    )
    assert result.breakdown["groups"]["corroboration"]["families"] == ["telegram"]
    assert result.breakdown["groups"]["corroboration"]["used"] == 0


# --- derived keys, flags, determinism ------------------------------------------------


def test_derived_keys_are_never_read_from_signals():
    planted = [
        sig(1, "derived_fresh_7d"),
        sig(2, "derived_contact_mobile"),
        sig(3, "derived_corroboration"),
        sig(4, "derived_contact_dm_channel"),
    ]
    result = run(planted)
    assert result.total == 0
    assert all(not g.get("signals") for g in result.breakdown["groups"].values())


def test_a_gate_flag_is_listed_and_worth_nothing():
    result = run([sig(1, "vol_container"), sig(2, "flag_competitor", evidence=9)])
    assert result.breakdown["flags"] == [
        {"key": "flag_competitor", "signal_id": 2, "evidence_id": 9}
    ]
    assert result.total == 50


def test_score_is_referentially_transparent():
    signals = [*SIGNALS_93, sig(9304, "vol_wholesale_bulky", source="enrich_site")]
    contacts = [ContactView(phone="+998712000001", phone_kind="landline")]
    first = json.dumps(run(signals, contacts).breakdown, sort_keys=True)
    again = json.dumps(run(signals, contacts).breakdown, sort_keys=True)
    shuffled = json.dumps(
        run(list(reversed(signals)), list(reversed(contacts))).breakdown, sort_keys=True
    )
    assert first == again == shuffled


@pytest.mark.parametrize(
    ("total", "phone", "tier"),
    [
        (60, True, "hot"),
        (60, False, "warm"),
        (59, True, "warm"),
        (40, True, "warm"),
        (39, True, "cold"),
        (0, False, "cold"),
        (100, True, "hot"),
    ],
)
def test_tiers(total, phone, tier):
    assert scoring.tier_of(total, phone=phone) == tier


def test_the_schema_refuses_a_breakdown_with_a_stray_key():
    breakdown = run(SIGNALS_91, [POSTER]).breakdown
    breakdown["groups"]["volume"]["signals"][0]["oops"] = 1
    assert list(VALIDATOR.iter_errors(breakdown)) != []
