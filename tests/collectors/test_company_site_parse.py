"""collectors/company_site and collectors/telegram_page (03 §2.1, 04 §3 E2/E3).

Over the SYNTHETIC pages of ``tests/fixtures/company_sites/`` — no company
site, link hub or ``t.me/s/`` page is recorded yet (its README says so and
marks each shape STILL UNVERIFIED). What is pinned here is the contract 03
§2.1 states and the shapes the gates read: ``name_raw`` as the site's title
(gate 3), ``competitor`` only through the cargo core (03 §1.4), a parsed
STIR or legal block → ``entity_solid`` (never a registry confirmation).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from clientscrapper.collectors import company_site as cs
from clientscrapper.collectors import telegram_page as tp
from clientscrapper.collectors.keywords import CJK_RE
from clientscrapper.services.normalise import to_observation_json
from clientscrapper.services.signals import CompanyFacts, EvidenceRef, from_rules

TZ = ZoneInfo("Asia/Tashkent")
AT = datetime(2026, 10, 24, 10, 0, tzinfo=TZ)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "company_sites"
SITE = "https://namuna-tekstil.example.uz/"


def page(name: str) -> bytes:
    return (FIXTURES / f"{name}_synthetic.html").read_bytes()


def one(name: str, url: str = SITE):
    found = cs.parse_company_site(page(name), url=url, fetched_at=AT)
    assert len(found) == 1
    return found[0]


def test_every_fixture_here_is_marked_synthetic_in_the_readme():
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    files = sorted(p.name for p in FIXTURES.glob("*.html"))
    assert files and all(name.endswith("_synthetic.html") for name in files)
    assert all(f"`{name}`" in readme for name in files)
    assert "STILL UNVERIFIED" in readme


def test_the_landing_page():
    obs = one("site_landing")
    assert (obs.source, obs.kind, obs.url) == ("enrich_site", "company", SITE)
    assert obs.company_name_raw == "Namuna Tekstil Savdo"
    assert (obs.legal_form, obs.stir) == ("MChJ", "300000031")
    # tel: first, once — the footer text repeats the same number.
    assert obs.phones == ["+998900000031"]
    assert (obs.telegram, obs.instagram) == ("namuna_tekstil", "namuna.tekstil")
    assert obs.website == SITE
    assert obs.address == "Toshkent sh., Chilonzor tumani, Bunyodkor koʻchasi, 1-uy"
    assert obs.signal_types == ["wholesale_bulky", "china_origin", "entity_solid"]
    assert obs.attrs["title"] == "Namuna Tekstil Savdo | Optom mato Toshkentda"
    assert cs.about_link(page("site_landing"), url=SITE) == f"{SITE}kontakty"


def test_the_landing_page_as_stored_and_its_rule_signals():
    parsed = to_observation_json(one("site_landing"))
    assert parsed["kind"] == "profile"
    assert (parsed["name_key"], parsed["company"]["city"]) == (
        "namuna tekstil savdo",
        "Toshkent",
    )
    assert [(p["e164"], p["owner"]) for p in parsed["phones"]] == [
        ("+998900000031", "company")
    ]
    ref = EvidenceRef(id=1, source_key="enrich_site", fetched_at=AT)
    keys = {d.key for d in from_rules(ref, parsed, CompanyFacts())}
    # ent_not_micro from the STIR/legal block, never ent_legal_confirmed (E4's).
    assert {"vol_wholesale_bulky", "cn_origin", "ent_not_micro"} <= keys
    assert "ent_legal_confirmed" not in keys
    assert "vol_container" in keys  # «har oy konteyner bilan mato olib kelamiz»


def test_the_contacts_page():
    obs = one("site_contacts", url=f"{SITE}kontakty")
    assert obs.company_name_raw == "Namuna Tekstil Savdo"  # «Контакты» skipped
    assert obs.phones == ["+998 71 200 00 32", "+998 90 000 00 31"]
    assert obs.address == "Ташкент, Чиланзарский район, улица Бунёдкор, 1"
    assert obs.legal_form == "ООО" and "entity_solid" in obs.signal_types
    assert cs.about_link(page("site_contacts"), url=f"{SITE}kontakty") is None


def test_the_schema_org_organization():
    url = "https://namuna-elektronika.example.uz/"
    obs = one("site_jsonld", url=url)
    assert obs.company_name_raw == "Namuna Elektronika"
    assert (obs.stir, obs.phones) == ("300000033", ["+998 90 000 00 33"])
    assert (obs.telegram, obs.instagram) == ("namuna_elektr", "namuna.elektr")
    assert obs.address == "Samarqand, Registon koʻchasi, 7"
    assert to_observation_json(obs)["company"]["city"] == "Samarqand"


def test_a_js_only_shell_is_no_observation():
    assert cs.parse_company_site(page("site_js_only"), url=SITE, fetched_at=AT) == []


def test_a_cargo_site_is_a_competitor_through_the_cargo_core_only():
    cargo = one("site_cargo", url="https://namuna-kargo.example.uz/")
    assert "competitor" in cargo.signal_types
    # An importer's «yetkazib beramiz» / «olib kelamiz» is no competitor.
    assert "competitor" not in one("site_landing").signal_types
    assert (
        cs.own_markers("Namuna Mebel Express", "Bepul dostavka, yetkazib beramiz") == []
    )
    # The logistics words name a forwarder in a NAME only: an importer's own
    # «logistika boʻlimi» is no cargo offer (03 §1.4, COMPETITOR_SELF_RE).
    assert cs.own_markers("Namuna Mebel", "Oʻz logistika boʻlimimiz bor") == []
    assert cs.own_markers("Namuna Logistika", "") == ["competitor"]


def test_a_chinese_title_is_kept_as_written_for_gate_3():
    obs = one("site_cn_title", url="https://namuna-yiwu.example.cn/")
    assert obs.company_name_raw == "义乌市纳穆纳贸易有限公司"
    assert CJK_RE.search(obs.company_name_raw)
    parsed = to_observation_json(obs)
    assert parsed["company"]["name_raw"] == "义乌市纳穆纳贸易有限公司"
    # The +86 number is not cut into a bogus Uzbek one.
    assert obs.phones == ["+8657900000036"]
    assert [p["e164"] for p in parsed["phones"]] == ["+8657900000036"]


def test_the_link_hub():
    found = cs.parse_link_hub(
        page("hub_taplink"), url="https://taplink.cc/namuna_mebel", fetched_at=AT
    )
    assert len(found) == 1
    hub = found[0]
    assert hub.phones == ["+998900000035"]
    assert (hub.telegram, hub.instagram) == ("namuna_mebel", "namuna.mebel")
    assert hub.website == "https://namuna-mebel.example.uz/"
    assert cs.is_link_hub("https://taplink.cc/namuna_mebel")
    assert not cs.is_link_hub(SITE)


@pytest.mark.parametrize(
    ("url", "phone"),
    [
        ("https://wa.me/998900000035", "+998900000035"),
        ("https://api.whatsapp.com/send?phone=998900000035&text=salom", "+998900000035"),
        ("https://wa.me/", None),
        ("https://namuna.example.uz/998900000035", None),
    ],
)
def test_a_whatsapp_link_is_read_without_a_fetch(url, phone):
    assert cs.whatsapp_phone(url) == phone


def test_the_channel_public_page():
    url = tp.public_url("namuna_optom")
    page_ = tp.parse_channel_page(page("tme_channel"), url=url)
    assert page_ is not None
    assert (page_.username, page_.title) == ("namuna_optom", "Namuna Optom Uy-roʻzgʻor")
    assert page_.newest_post_at == datetime.fromisoformat("2026-10-20T07:41:00+00:00")
    obs = tp.channel_observation(page_, url=url, fetched_at=AT)
    assert (obs.source, obs.url, obs.telegram) == ("enrich_site", url, "namuna_optom")
    assert obs.phones == ["+998 90 000 00 37"]
    assert obs.website == "https://namuna-optom.example.uz/"
    assert obs.address == "Toshkent, Qoʻyliq bozori, Yangi koʻcha, 5"
    assert "china_origin" in obs.signal_types and "wholesale_bulky" in obs.signal_types
    assert to_observation_json(obs)["kind"] == "profile"


def test_a_person_s_page_is_no_channel():
    assert (
        tp.parse_channel_page(page("tme_user"), url="https://t.me/namuna_person") is None
    )
