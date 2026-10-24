"""Rule signals (04 §5.3 rules 1–6, the own-text detections of §5.1) and
``signals.emit`` (02 §2.7).

The observations are built the way a collector's are stored: a
``collectors.base.Observation`` through ``normalise.to_observation_json``, the
only writer of ``observations.parsed``. Each test pins the exact keys, values
and quotes — a rule that returns nothing on its own example fails. Synthetic
data only (07 §1.0).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from clientscrapper.collectors.base import Observation
from clientscrapper.db.models import Company, Evidence, LeadCandidate, Signal
from clientscrapper.services import scoring, signals
from clientscrapper.services.normalise import to_observation_json
from clientscrapper.services.signals import CompanyFacts, EvidenceRef

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 20, 7, 53, tzinfo=TZ)
POSTED = NOW - timedelta(minutes=12)
EVIDENCE = EvidenceRef(id=7001, source_key="telegram_monitor", fetched_at=NOW)
NO_FACTS = CompanyFacts()

# 03 kind and source → the stored kind (02 §2.5's mapping table).
KINDS = {
    "intent_post": ("post", "telegram_monitor"),
    "profile": ("company", "enrich_site"),
    "directory_entry": ("company", "directories_goldenpages"),
    "seller": ("company", "olx_business"),
    "listing": ("listing", "olx_business"),
    "vacancy": ("vacancy", "hh_vacancies"),
    "dealer_page": ("company", "brand_dealers"),
}


def parsed(text: str, kind: str = "intent_post", **fields: Any) -> dict[str, Any]:
    raw_kind, source = KINDS[kind]
    source = fields.pop("source", source)
    obs = Observation(
        source=source,
        source_ref=f"test:{hashlib.sha1(text.encode()).hexdigest()[:12]}",
        url="https://namuna.example.uz/1",
        fetched_at=NOW,
        raw_sha256="",
        kind=raw_kind,  # type: ignore[arg-type]
        company_name_raw=fields.pop("name", "Namuna Savdo MChJ"),
        excerpt=text,
        published_at=POSTED,
        **fields,
    )
    body = to_observation_json(obs)
    assert body["kind"] == kind
    return body


def rules(
    text: str, kind: str = "intent_post", facts: CompanyFacts = NO_FACTS, **fields: Any
) -> dict[str, signals.SignalDraft]:
    body = parsed(text, kind, **fields)
    evidence = EvidenceRef(id=7001, source_key=body["source_key"], fetched_at=NOW)
    drafts = signals.from_rules(evidence, body, facts)
    assert [d.key for d in drafts] == sorted(d.key for d in drafts)
    return {d.key: d for d in drafts}


def keys(found: dict[str, signals.SignalDraft]) -> set[str]:
    return set(found)


# --- rule 1: stated volume ---------------------------------------------------------


def test_9_1_two_cubes_of_cloth_from_guangzhou():
    found = rules("Guangzhoudan 2 kub mato kerak, kim olib keladi?")
    assert keys(found) == {"vol_stated_1_3", "cn_origin"}
    volume = found["vol_stated_1_3"]
    assert (volume.value, volume.quote) == (Decimal("2"), "2 kub")
    assert found["cn_origin"].quote == "Guangzhoudan"
    assert volume.observed_at == POSTED
    assert (volume.model, volume.evidence_id, volume.source_key) == (
        None,
        7001,
        "telegram_monitor",
    )


@pytest.mark.parametrize(
    ("text", "key", "value", "quote"),
    [
        ("Yiwudan 25 kub mebel kerak", "vol_stated_3plus", "25", "25 kub"),
        ("3 kub yuk bor", "vol_stated_3plus", "3", "3 kub"),
        ("1 kub yuk bor", "vol_stated_1_3", "1", "1 kub"),
        ("2,5 м3 груза", "vol_stated_1_3", "2.5", "2,5 м3"),
        ("Нужно 30 кубов мебели", "vol_stated_3plus", "30", "30 кубов"),
        ("2 kub yoki 10 kub bo'ladi", "vol_stated_3plus", "10", "10 kub"),
        ("2 tonna mato kerak", "vol_stated_3plus", "6", "2 tonna"),
        ("0,5 tonna gazlama", "vol_stated_1_3", "1.5", "0,5 tonna"),
        ("yarim fura mebel kerak", "vol_stated_3plus", "40", "yarim fura"),
        ("полфуры мебели из Китая", "vol_stated_3plus", "40", "полфуры"),
    ],
)
def test_rule_1_the_largest_amount_wins(text, key, value, quote):
    found = rules(text)
    stated = {k for k in found if k.startswith("vol_stated")}
    assert stated == {key}
    assert (found[key].value, found[key].quote) == (Decimal(value), quote)
    assert "vol_container" not in found  # a tonne or a half truck is not rule 2


def test_rule_1_reads_the_volume_fragment_too():
    found = rules("Mebel kerak, narxi qancha?", volume_text="5 kub")
    assert (found["vol_stated_3plus"].value, found["vol_stated_3plus"].quote) == (
        Decimal("5"),
        "5 kub",
    )


def test_rule_1_under_a_cube_is_no_volume_and_feeds_the_b2c_negative():
    found = rules("0,5 kub mato kerak")
    assert keys(found) == {"neg_b2c_words"}
    assert found["neg_b2c_words"].quote == "0,5 kub"


# --- rule 2: containers and trucks ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "quote"),
    [
        ("1 konteyner mebel olib kelamiz", "konteyner"),
        ("Ищу фуру до Ташкента", "фуру"),
        ("40 fut konteyner kerak", "40 fut"),
        ("контейнерда келади", "контейнерда"),
    ],
)
def test_rule_2_a_container_or_truck_word(text, quote):
    found = rules(text)
    assert found["vol_container"].quote == quote


@pytest.mark.parametrize(
    "text",
    ["Yangi partiya keldi", "Новая партия товара", "tonnadan sotamiz"],
)
def test_rule_2_partiya_and_tonna_alone_count_as_nothing(text):
    assert keys(rules(text)) & signals.VOLUME_KEYS == set()


# --- rule 3: the classifier -----------------------------------------------------------


def model(
    text: str,
    *,
    quote: str | None,
    kind: str = "intent_post",
    facts: CompanyFacts = NO_FACTS,
    **signal: Any,
) -> dict[str, signals.SignalDraft]:
    body = parsed(text, kind)
    defaults: dict[str, Any] = {
        "speaker_role": "buyer",
        "b2b": True,
        "b2c_markers": False,
        "goods": [],
        "goods_category": "none",
        "origin_cn": "unknown",
        "volume_m3_est": None,
        "volume_basis": "none",
        "volume_quote": quote,
        "frequency": "unknown",
        "confidence": 0.86,
    }
    defaults.update(signal)
    drafts = signals.from_model(
        EVIDENCE,
        body,
        defaults,
        quote=quote,
        company=facts,
        model="model-x",
        model_job_id=55,
    )
    assert all((d.model, d.model_job_id) == ("model-x", 55) for d in drafts)
    return {d.key: d for d in drafts}


def test_rule_3_the_9_1_import_signal():
    found = model(
        "Guangzhoudan 2 kub mato kerak, kim olib keladi?",
        quote="2 kub mato",
        volume_m3_est=2,
        volume_basis="stated",
        goods_category="textile",
        origin_cn="explicit",
    )
    assert keys(found) == {"vol_stated_1_3", "cn_sector_prior"}
    assert (found["vol_stated_1_3"].value, found["vol_stated_1_3"].quote) == (
        Decimal("2"),
        "2 kub mato",
    )


@pytest.mark.parametrize(
    ("basis", "estimate", "quote", "key", "value"),
    [
        ("stated", 5, "5 kub", "vol_stated_3plus", "5"),
        ("container", 30, "1 konteyner", "vol_container", "30"),
        ("truck", 85, "bitta fura", "vol_container", "85"),
        ("weight", 2, "2 tonna", "vol_stated_3plus", "6"),
    ],
)
def test_rule_3_a_quoted_volume_becomes_its_key(basis, estimate, quote, key, value):
    found = model(
        f"{quote} kerak", quote=quote, volume_m3_est=estimate, volume_basis=basis
    )
    assert keys(found) == {key}
    assert (found[key].value, found[key].quote) == (Decimal(value), quote)


@pytest.mark.parametrize(
    ("basis", "quote"),
    [
        ("stated", None),  # no verified quote: inferred (04 §4.7)
        ("inferred", "ko'p mebel"),
        ("none", None),
        ("weight", "og'ir yuk"),  # a weight quote with no tonnes in it
    ],
)
def test_rule_3_no_volume_without_a_verified_stated_quote(basis, quote):
    found = model(
        "ko'p mebel og'ir yuk kerak", quote=quote, volume_m3_est=5, volume_basis=basis
    )
    assert keys(found) & signals.VOLUME_KEYS == set()


def test_rule_3_never_reads_volume_into_a_vacancy():
    found = model(
        "контроль отгрузок контейнеров",
        quote="контейнеров",
        kind="vacancy",
        volume_m3_est=60,
        volume_basis="container",
    )
    assert keys(found) == set()


def test_rule_3_goods_origin_and_b2c_from_the_model():
    bulky = model("optom mebel bor", quote=None, goods_category="furniture")
    assert keys(bulky) == {"cn_sector_prior", "vol_wholesale_bulky"}
    assert bulky["vol_wholesale_bulky"].quote == "optom"
    assert keys(model("optom pomada", quote=None, goods_category="cosmetics")) == set()
    other = model("mato kerak", quote=None, origin_cn="other_origin")
    assert keys(other) == {"neg_non_cn_origin"}
    with_china = model("Xitoydan mato", quote=None, origin_cn="other_origin")
    assert keys(with_china) == set()
    b2c = model("bitta kurtka kerak", quote=None, b2c_markers=True)
    assert keys(b2c) == {"neg_b2c_words"}
    stated = model(
        "o'zim uchun 5 kub",
        quote="5 kub",
        b2c_markers=True,
        volume_m3_est=5,
        volume_basis="stated",
    )
    assert keys(stated) == {"vol_stated_3plus"}


# --- rule 4: optom + a bulky category -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "quote"),
    [
        ("Optom mebel, Chorsu", "profile", "Optom"),
        ("Ulgurji narxda santexnika", "directory_entry", "Ulgurji narxda"),
        ("Продаём оптом сантехнику", "profile", "оптом"),
        ("optom mato kerak", "intent_post", "optom"),
    ],
)
def test_rule_4_a_marker_and_a_bulky_category(text, kind, quote):
    found = rules(text, kind)
    assert found["vol_wholesale_bulky"].quote == quote


@pytest.mark.parametrize(
    "text",
    ["optom kosmetika", "Канцтовары оптом", "Салон оптика: мебель", "optom oziq-ovqat"],
)
def test_rule_4_no_bulky_category_or_no_marker_is_nothing(text):
    assert "vol_wholesale_bulky" not in rules(text, "profile")


def test_rule_4_the_category_may_come_from_the_company_or_the_goods_text():
    facts = CompanyFacts(sectors=frozenset({"furniture"}))
    assert "vol_wholesale_bulky" in rules("Optom narxlarda", "profile", facts)
    assert "vol_wholesale_bulky" not in rules("Optom narxlarda", "profile")
    found = rules("Optom narxlarda", "profile", goods_text="mebel furnitura")
    assert found["vol_wholesale_bulky"].quote == "Optom"


# --- rule 5: marketplace counts -------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "attrs", "value"),
    [
        ("olx_business", {"categories": {"Бытовая техника": 14}}, 14),
        ("olx_business", {"categories": {"furniture": 10}}, 10),
        ("olx_business", {"categories": {"Бытовая техника": 9}}, None),
        ("olx_business", {"categories": {"Косметика": 40}}, None),
        ("olx_business", {"category": "Мебель", "ads_count": 12}, 12),
        ("uzum_sellers", {"categories": {"mebel": 15}, "orders_30d": 300}, 15),
        ("uzum_sellers", {"categories": {"mebel": 15}, "orders_30d": 299}, None),
        ("uzum_sellers", {"categories": {"mebel": 14}, "orders_30d": 900}, None),
    ],
)
def test_rule_5_marketplace_velocity_in_one_bulky_category(source, attrs, value):
    found = rules("Sotuvchi sahifasi", "seller", source=source, attrs=attrs)
    if value is None:
        assert "vol_marketplace_velocity" not in found
    else:
        assert found["vol_marketplace_velocity"].value == Decimal(value)


def test_9_2_the_olx_seller_s_rule_signals():
    found = rules(
        "оптом со склада, стиральные машины",
        "seller",
        name="Sardor",
        external_ids={"olx_user_id": "AujTE"},
        attrs={"ads_count": 14, "business": True, "categories": {"Бытовая техника": 14}},
        signal_types=["wholesale_bulky", "marketplace_velocity", "entity_solid"],
    )
    assert keys(found) == {
        "vol_wholesale_bulky",
        "vol_marketplace_velocity",
        "ent_not_micro",
    }
    assert found["vol_wholesale_bulky"].quote == "оптом"
    assert found["vol_marketplace_velocity"].value == Decimal(14)


# --- rule 6: the collector's own types ------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        ("container_truck", "vol_container"),
        ("volume_m3_3plus", "vol_stated_3plus"),
        ("volume_m3_1to3", "vol_stated_1_3"),
        ("wholesale_bulky", "vol_wholesale_bulky"),
        ("directory_wholesale", "vol_directory_wholesale"),
        ("exhibitor", "vol_exhibitor"),
        ("marketplace_velocity", "vol_marketplace_velocity"),
        ("china_origin", "cn_origin"),
        ("entity_solid", "ent_not_micro"),
        ("b2c_marker", "neg_b2c_words"),
        ("non_china_origin", "neg_non_cn_origin"),
        ("state_body", "neg_state_body"),
        ("liquidated", "neg_liquidated"),
        ("competitor", "flag_competitor"),
    ],
)
def test_rule_6_maps_03_s_types_onto_02_s_keys(kind, key):
    found = rules("Namuna kartochkasi", "directory_entry", signal_types=[kind])
    assert keys(found) == {key}


def test_rule_6_ved_vacancy_on_a_vacancy():
    found = rules("Менеджер ВЭД", "vacancy", signal_types=["ved_vacancy"])
    assert keys(found) == {"vol_vacancy_import"}
    assert found["vol_vacancy_import"].quote == "ВЭД"


@pytest.mark.parametrize(
    "kind", ["contact_phone", "contact_handle", "intent_ask", "suppressed"]
)
def test_rule_6_contacts_asks_and_suppression_are_not_signals(kind):
    assert (
        keys(rules("Namuna kartochkasi", "directory_entry", signal_types=[kind])) == set()
    )


def test_an_entity_block_is_never_a_registry_confirmation():
    found = rules("ИНН 300000011, ООО", "profile", signal_types=["entity_solid"])
    assert keys(found) == {"ent_not_micro"}


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        (
            {"brand": "Namunacar", "official": True, "brand_group": "vehicles"},
            {"vol_container", "cn_origin"},
        ),
        (
            {"brand": "Namunacar", "official": False, "brand_group": "vehicles"},
            {"vol_wholesale_bulky", "cn_origin"},
        ),
        (
            {"brand": "Namunafon", "official": True, "brand_group": "electronics"},
            {"cn_origin"},
        ),
    ],
)
def test_rule_6_brand_distributor_is_decided_here(attrs, expected):
    found = rules(
        "Namunacar va Namunafon rasmiy distribyutori",
        "dealer_page",
        attrs=attrs,
        signal_types=["brand_distributor"],
    )
    assert keys(found) == expected
    assert found["cn_origin"].quote == attrs["brand"]


# --- a vacancy's duties are not volume (04 §9.3) --------------------------------------

VACANCY_93 = (
    "Менеджер ВЭД со знанием китайского … закупки оборудования в Китае, работа с 1688 "
    "и фабриками, контроль отгрузок контейнеров"
)


def test_9_3_the_vacancy_yields_its_import_duty_and_china_only():
    found = rules(
        VACANCY_93,
        "vacancy",
        signal_types=[
            "ved_vacancy",
            "china_origin",
            "container_truck",
            "volume_m3_3plus",
        ],
    )
    assert keys(found) == {"vol_vacancy_import", "cn_origin"}
    assert found["vol_vacancy_import"].quote == "ВЭД"
    assert found["cn_origin"].quote == "Китае"


def test_the_same_words_on_the_firm_s_own_page_are_volume():
    found = rules(VACANCY_93, "profile")
    assert found["vol_container"].quote == "контейнеров"
    assert "vol_vacancy_import" not in found  # the duty regex reads vacancies only


def test_a_vacancy_s_state_language_is_not_a_state_body():
    found = rules("Davlat tilini bilish, xitoy tilini bilish", "vacancy")
    # «Xitoy» is a CHINA_WORDS word (03 §2.7: china_origin when one appears).
    assert keys(found) == {"vol_vacancy_import", "cn_origin"}


# --- the own-text negatives -----------------------------------------------------------


def test_b2c_words_without_volume():
    found = rules("o'zim uchun 1 dona kurtka kerak")
    assert keys(found) == {"neg_b2c_words"}
    assert found["neg_b2c_words"].quote == "o'zim uchun"


def test_no_b2c_negative_beside_a_volume_signal():
    found = rules("o'zim uchun emas, 25 kub mebel kerak")
    assert keys(found) == {"vol_stated_3plus"}


def test_no_b2c_negative_while_the_company_holds_volume():
    facts = CompanyFacts(holds_volume=True)
    assert keys(rules("o'zim uchun 1 dona kerak", facts=facts)) == set()
    assert (
        keys(rules("Namuna", "directory_entry", facts, signal_types=["b2c_marker"]))
        == set()
    )


def test_another_origin_without_a_china_word():
    found = rules("Turkiyadan mato olib kelamiz", "profile")
    assert keys(found) == {"neg_non_cn_origin"}
    assert found["neg_non_cn_origin"].quote == "Turkiyadan"
    both = rules("Turkiyadan va Xitoydan mato", "profile")
    assert keys(both) == {"cn_origin"}


def test_a_state_word_in_a_self_description_only():
    found = rules("Toshkent shahar hokimligi xo'jalik bo'limi", "profile")
    assert found["neg_state_body"].quote == "hokimligi"
    assert "neg_state_body" not in rules("maktab uchun mebel kerak, 5 kub")


def test_a_quote_is_clipped_to_the_column():
    found = rules("optom mebel", "profile", signal_types=["exhibitor"])
    assert found["vol_exhibitor"].quote is None
    long_marker = signals._clip("x" * 400)
    assert long_marker is not None and len(long_marker) == 300


def test_a_buyer_asking_for_cargo_is_not_flagged_by_rules():
    # «kim kargo qiladi?» is a buyer (04 §6); flag_competitor comes from the
    # collector's type or the gates, never from the rules' own text.
    assert keys(rules("Guangzhoudan 3 kub, kim kargo qiladi?")) == {
        "vol_stated_3plus",
        "cn_origin",
    }


# --- emit (Postgres) ------------------------------------------------------------------


async def _company_and_evidence(session) -> tuple[Company, Evidence]:
    firm = Company(
        display_name="Namuna Savdo MChJ", name_key="namuna savdo", state="active"
    )
    evidence = Evidence(
        source_key="telegram_monitor",
        url="https://t.me/namuna_optom/1",
        fetched_at=NOW,
        sha256="a" * 64,
        bytes=0,
        excerpt="Guangzhoudan 2 kub mato kerak",
        expires_at=NOW + timedelta(days=180),
    )
    session.add_all([firm, evidence])
    await session.flush()
    return firm, evidence


async def test_emit_upserts_one_row_per_key_and_evidence(session):
    firm, evidence = await _company_and_evidence(session)
    ref = EvidenceRef(id=evidence.id, source_key="telegram_monitor", fetched_at=NOW)
    body = parsed("Guangzhoudan 2 kub mato kerak")
    weights = scoring.weights_v1()
    drafts = signals.from_rules(ref, body, NO_FACTS)
    assert await signals.emit(session, firm.id, drafts, weights=weights) == 2
    again = signals.from_model(
        ref,
        body,
        {"volume_basis": "stated", "volume_m3_est": 2, "goods_category": "textile"},
        quote="2 kub mato",
        company=NO_FACTS,
        model="model-x",
    )
    assert await signals.emit(session, firm.id, again, weights=weights) == 2
    rows = (
        await session.execute(
            sa.select(
                Signal.key, Signal.value, Signal.quote, Signal.model, Signal.expires_at
            )
            .where(Signal.company_id == firm.id)
            .order_by(Signal.key)
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        ("cn_origin", None, "Guangzhoudan", None, POSTED + timedelta(days=365)),
        ("cn_sector_prior", None, None, "model-x", None),
        # the rule wrote it first; the model's verified quote replaces the
        # regex fragment (04 §9.1 quotes «2 kub mato») and the model names itself
        (
            "vol_stated_1_3",
            Decimal("2.000"),
            "2 kub mato",
            "model-x",
            POSTED + timedelta(days=180),
        ),
    ]
    await session.refresh(firm)
    assert firm.last_signal_at == POSTED


async def test_emit_moves_last_signal_at_forward_only_and_marks_the_candidate(session):
    firm, evidence = await _company_and_evidence(session)
    firm.last_signal_at = NOW + timedelta(days=1)
    candidate = LeadCandidate(
        company_id=firm.id, state="enriching", score=52, last_scored_at=NOW
    )
    pushed = LeadCandidate(
        company_id=firm.id,
        state="rejected",
        score=40,
        last_scored_at=NOW,
        rejected_at=NOW,
    )
    session.add_all([candidate, pushed])
    await session.flush()
    ref = EvidenceRef(id=evidence.id, source_key="telegram_monitor", fetched_at=NOW)
    drafts = signals.from_rules(ref, parsed("25 kub mebel"), NO_FACTS)
    await signals.emit(session, firm.id, drafts, weights=scoring.weights_v1())
    await session.refresh(firm)
    await session.refresh(candidate)
    await session.refresh(pushed)
    assert firm.last_signal_at == NOW + timedelta(days=1)
    assert candidate.last_scored_at is None  # score_job owes it a score
    assert pushed.last_scored_at == NOW  # a terminal row is left alone
    assert await signals.emit(session, firm.id, [], weights=scoring.weights_v1()) == 0


async def test_company_facts_reads_sectors_and_standing_volume(session):
    firm, evidence = await _company_and_evidence(session)
    firm.sectors = ["furniture"]
    await session.flush()
    assert await signals.company_facts(session, firm) == CompanyFacts(
        sectors=frozenset({"furniture"}), holds_volume=False
    )
    ref = EvidenceRef(id=evidence.id, source_key="telegram_monitor", fetched_at=NOW)
    await signals.emit(
        session,
        firm.id,
        signals.from_rules(ref, parsed("1 konteyner"), NO_FACTS),
        weights=scoring.weights_v1(),
    )
    assert (await signals.company_facts(session, firm)).holds_volume is True
