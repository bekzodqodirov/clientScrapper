"""normalise.to_observation_json (04 §1.6 → 02 §2.5): the only writer of
``observations.parsed``. Pure; synthetic data only. Each case pins the whole
shape or the concrete fields the mapping table names."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from clientscrapper.collectors.base import Observation
from clientscrapper.services.normalise import parsed_kind, to_observation_json

TZ = ZoneInfo("Asia/Tashkent")
AT = datetime(2026, 10, 20, 7, 41, tzinfo=TZ)


def obs(**fields) -> Observation:
    base = {
        "source": "telegram_monitor",
        "source_ref": "tg:-1001000000001:18234",
        "url": "https://t.me/namuna_optom/18234",
        "fetched_at": AT,
        "raw_sha256": "",
        "kind": "post",
    }
    return Observation(**{**base, **fields})


def test_a_telegram_intent_post_is_the_02_example():
    parsed = to_observation_json(
        obs(
            company_name_raw="Namuna Tekstil Savdo MChJ",
            city="Toshkent",
            goods_text="mato, furnitura",
            phones=["+998 90 000 00 01"],
            telegram="namuna_tekstil",
            person_name="Rustam",
            person_role="direktor",
            excerpt="Guangzhoudan 25 kub mato olib kelish kerak, kim qiladi?",
            volume_text="25 kub",
            origin_text="Guangzhou",
            signal_types=["volume_m3_3plus", "china_origin"],
            attrs={"tg_user_id": 5100000001, "volume_m3": 25},
            lang="uz-Latn",
        ),
        collector_version="telegram_monitor@1",
    )
    assert parsed == {
        "v": 1,
        "kind": "intent_post",
        "source_key": "telegram_monitor",
        "source_ref": "tg:-1001000000001:18234",
        "url": "https://t.me/namuna_optom/18234",
        "observed_at": "2026-10-20T07:41:00+05:00",
        "name_key": "namuna tekstil savdo",
        "company": {
            "name_raw": "Namuna Tekstil Savdo MChJ",
            "legal_form": "MChJ",
            "stir": None,
            "oked": None,
            "city": "Toshkent",
            "address": None,
            "website": None,
            "goods_text": "mato, furnitura",
            "sector_hints": [],
        },
        "handles": {
            "telegram": "namuna_tekstil",
            "tg_user_id": 5100000001,
            "instagram": None,
            "olx_user_id": None,
            "hh_employer_id": None,
            "gis2_firm_id": None,
            "google_place_id": None,
            "uzum_shop": None,
        },
        "phones": [
            {
                "raw": "+998 90 000 00 01",
                "e164": "+998900000001",
                "kind": "mobile",
                "owner": "person",
            }
        ],
        "person": {"name": "Rustam", "role": "direktor"},
        "text": {
            "excerpt": "Guangzhoudan 25 kub mato olib kelish kerak, kim qiladi?",
            "volume_text": "25 kub",
            "origin_text": "Guangzhou",
            "intent_words": ["olib kelish", "kerak"],
        },
        "hints": {
            "volume_m3": 25.0,
            "volume_basis": "stated",
            "container_words": False,
            "wholesale_words": False,
            "b2c_words": False,
            "competitor_words": False,
            "china_words": True,
            "state_words": False,
            "collector": {
                "signal_types": ["volume_m3_3plus", "china_origin"],
                "attrs": {"tg_user_id": 5100000001, "volume_m3": 25},
            },
            "pinfl_dropped": False,
        },
        "lang": "uz-Latn",
        "legal_basis": "public_source",
        "collector_version": "telegram_monitor@1",
    }


def test_a_person_shaped_olx_seller_name_never_becomes_the_company_name():
    parsed = to_observation_json(
        obs(
            source="olx_business",
            source_ref="olx:user:123456",
            url="https://www.olx.uz/list/user/namuna/",
            kind="company",
            company_name_raw="Sardor",
            external_ids={"olx_user_id": "123456"},
            phones=["90 000-00-02"],
        )
    )
    assert parsed["kind"] == "seller"
    assert parsed["company"]["name_raw"] == "OLX sotuvchi 123456"
    assert parsed["name_key"] is None
    assert parsed["person"] == {"name": "Sardor", "role": None}
    assert parsed["handles"]["olx_user_id"] == "123456"
    assert parsed["phones"] == [
        {
            "raw": "90 000-00-02",
            "e164": "+998900000002",
            "kind": "mobile",
            "owner": "unknown",
        }
    ]


def test_a_directory_card_keeps_the_firm_and_drops_a_person():
    parsed = to_observation_json(
        obs(
            source="directories_goldenpages",
            source_ref="goldenpages:42",
            url="https://www.goldenpages.example.uz/company/42",
            kind="company",
            company_name_raw="ООО «Намуна Текстиль Савдо»",
            legal_form="ООО",
            address="г. Ташкент, Чиланзар",
            phones=["71 200 00 01", "1234", "+998 71 200-00-01"],
            person_name="Rustam",
            stir="300000001",
            website="https://namuna.example.uz/",
            external_ids={"place_id": "ChIJnamuna"},
        )
    )
    assert parsed["kind"] == "directory_entry"
    assert parsed["name_key"] == "namuna tekstil savdo"
    assert parsed["company"]["legal_form"] == "OOO"
    assert parsed["company"]["city"] == "Toshkent"
    assert parsed["company"]["stir"] == "300000001"
    assert parsed["company"]["website"] == "https://namuna.example.uz/"
    assert parsed["person"] is None
    assert parsed["handles"]["google_place_id"] == "ChIJnamuna"
    # The bare landline gets Tashkent's code from the city; the junk and the
    # repeat of the same tail are gone.
    assert parsed["phones"] == [
        {
            "raw": "71 200 00 01",
            "e164": "+998712000001",
            "kind": "landline",
            "owner": "company",
        }
    ]


def test_a_field_with_a_14_digit_run_is_dropped():
    parsed = to_observation_json(
        obs(
            excerpt="Pasport JSHSHIR 30000000000001, 3 kub kerak",
            goods_text="mebel",
            attrs={"note": "ПИНФЛ 30000000000002"},
        )
    )
    assert parsed["text"]["excerpt"] == ""
    assert parsed["text"]["intent_words"] == []
    assert parsed["company"]["goods_text"] == "mebel"
    assert parsed["hints"]["collector"]["attrs"] == {"note": None}
    assert parsed["hints"]["pinfl_dropped"] is True


def test_the_excerpt_is_cut_at_600():
    parsed = to_observation_json(obs(excerpt="x" * 900))
    assert len(parsed["text"]["excerpt"]) == 600


def test_signal_types_become_word_hints():
    parsed = to_observation_json(
        obs(
            kind="listing",
            source="olx_business",
            signal_types=[
                "container_truck",
                "wholesale_bulky",
                "competitor",
                "b2c_marker",
            ],
            attrs={"volume_m3": "28", "basis": "container_words"},
        )
    )
    hints = parsed["hints"]
    assert (hints["volume_m3"], hints["volume_basis"]) == (28.0, "container_words")
    assert hints["container_words"] and hints["wholesale_words"]
    assert hints["competitor_words"] and hints["b2c_words"]
    assert not hints["china_words"] and not hints["state_words"]


def test_external_ids_are_renamed():
    parsed = to_observation_json(
        obs(
            kind="place",
            source="maps_2gis",
            external_ids={
                "twogis_firm_id": "70000001",
                "uzum_shop_slug": "namuna-shop",
                "tg_chat_id": "-1001000000001",
                "hh_employer_id": "9000001",
            },
        )
    )
    handles = parsed["handles"]
    assert handles["gis2_firm_id"] == "70000001"
    assert handles["uzum_shop"] == "namuna-shop"
    assert handles["tg_chat"] == "-1001000000001"
    assert handles["hh_employer_id"] == "9000001"


@pytest.mark.parametrize(
    ("kind", "source", "attrs", "expected"),
    [
        ("post", "telegram_monitor", {}, "intent_post"),
        ("post", "telegram_monitor", {"chat_type": "channel"}, "profile"),
        ("company", "instagram_discovery", {}, "profile"),
        ("company", "telegram_monitor", {}, "profile"),
        ("company", "enrich_site", {}, "profile"),
        ("company", "directories_yellowpages", {}, "directory_entry"),
        ("company", "maps_google", {}, "directory_entry"),
        ("company", "exhibitors", {}, "directory_entry"),
        ("company", "brand_dealers", {}, "dealer_page"),
        ("company", "registry_lookup", {}, "registry"),
        ("company", "olx_business", {}, "seller"),
        ("company", "uzum_sellers", {}, "seller"),
        ("listing", "olx_business", {}, "listing"),
        ("vacancy", "hh_vacancies", {}, "vacancy"),
        ("exhibitor", "exhibitors", {}, "exhibitor"),
        ("place", "maps_google", {}, "place"),
    ],
)
def test_the_kind_mapping_table(kind, source, attrs, expected):
    assert parsed_kind(kind, source, attrs) == expected


@pytest.mark.parametrize("kind", ["serp_hit", "suppression", "outcome", "nonsense"])
def test_what_is_not_an_observation_is_refused(kind):
    with pytest.raises(ValueError):
        parsed_kind(kind, "serp_layer")
