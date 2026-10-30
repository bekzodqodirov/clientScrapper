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
    # 04 §3 E2 «goods»: what the site says it sells, its description and
    # heading (the classifier and the word rules read goods_text).
    assert obs.goods_text == (
        "Xitoydan optom mato va gazlama. Ulgurji savdo, ombor Toshkentda."
        " · Optom mato va gazlama"
    )


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


# --- the contract's branches, one at a time (T-24 review: mutants that
# survived the happy-path fixtures). Each page here is a few lines of HTML
# written inline for the one rule it pins — no shape a recording must
# confirm. ----------------------------------------------------------------------


def html(body: str, *, head: str = "") -> bytes:
    return f"<html><head>{head}</head><body>{body}</body></html>".encode()


def parsed(body: str, *, head: str = "", url: str = SITE):
    found = cs.parse_company_site(html(body, head=head), url=url, fetched_at=AT)
    assert len(found) == 1
    return found[0]


# Over JS_ONLY_TEXT: a page, not a shell, whatever scripts it carries.
FILLER = (
    "<p>Namuna kompaniyasi haqida maʼlumot, mahsulotlar roʻyxati va ish vaqti shu "
    "sahifada.</p>"
)


def test_a_cargo_self_description_is_a_competitor_under_a_neutral_name():
    """COMPETITOR_SELF_RE on the page's own words decides, whatever its name
    says (03 §1.4): «Namuna Savdo» offering «yuk tashish» is a forwarder."""
    obs = parsed(
        "<p>Xitoydan yuk tashish xizmati, kargo va rastamojka.</p>",
        head="<title>Namuna Savdo</title>",
    )
    assert obs.company_name_raw == "Namuna Savdo"
    assert "competitor" in obs.signal_types
    assert cs.own_markers("Namuna Savdo", "Kargo xizmati: Xitoydan yuk tashish") == [
        "china_origin",
        "competitor",
    ]


def test_wholesale_needs_a_bulky_category_beside_it():
    assert cs.own_markers(None, "Optom savdo, ulgurji narxlar") == []
    assert cs.own_markers(None, "Mato va gazlama") == []
    assert cs.own_markers(None, "Optom mato va gazlama") == ["wholesale_bulky"]


def test_a_stir_alone_is_entity_solid_and_quoted():
    obs = parsed(f"{FILLER}<p>STIR 300000041</p>", head="<title>Namuna Savdo</title>")
    assert (obs.stir, obs.legal_form) == ("300000041", None)
    assert "entity_solid" in obs.signal_types
    ref = EvidenceRef(id=1, source_key="enrich_site", fetched_at=AT)
    drafts = from_rules(ref, to_observation_json(obs), CompanyFacts())
    quotes = {d.key: d.quote for d in drafts}
    assert quotes["ent_not_micro"] == "STIR 300000041"


def test_the_site_s_legal_block_is_quoted_verbatim():
    """ent_not_micro cites the footer the page wrote (CLAUDE.md: URL + date +
    verbatim quote), however long the page before it."""
    obs = one("site_landing")
    assert obs.excerpt.endswith('© 2026 "Namuna Tekstil Savdo" MChJ · STIR 300000031')
    long_page = parsed(
        "".join(f"<p>{'Optom mato va gazlama ' * 8}{n}</p>" for n in range(6))
        + "<p>© 2026 «Namuna Uzun» MChJ</p>",
        head="<title>Namuna Uzun</title>",
    )
    assert len(long_page.excerpt) <= cs.EXCERPT_MAX
    assert long_page.excerpt.endswith("© 2026 «Namuna Uzun» MChJ")
    ref = EvidenceRef(id=1, source_key="enrich_site", fetched_at=AT)
    quotes = {
        d.key: d.quote
        for d in from_rules(ref, to_observation_json(long_page), CompanyFacts())
    }
    assert quotes["ent_not_micro"] == "MChJ"


def test_the_legal_sentence_is_the_last_short_line_naming_a_form():
    obs = parsed(
        "<p>Hamkorimiz «Boshqa Firma» MChJ bilan ishlaymiz.</p>"
        f"{FILLER}"
        "<p>© 2026 «Namuna Oxirgi» XK</p>"
    )
    assert obs.company_name_raw == "© 2026 «Namuna Oxirgi» XK"
    assert obs.legal_form == "XK"
    long_line = "Biz " + "juda " * 40 + "katta «Namuna» MChJ kompaniyasimiz."
    assert len(long_line) > 160
    solid = parsed(f"{FILLER}<p>{long_line}</p>", head="<title>Namuna</title>")
    assert solid.legal_form is None and "entity_solid" not in solid.signal_types


def test_the_address_needs_a_place_and_a_street_word():
    assert cs.address_line(["Toshkent sh., Chilonzor tumani"]) is None
    assert cs.address_line(["Bunyodkor koʻchasi, 1-uy"]) is None
    assert (
        cs.address_line(["Manzil: Toshkent sh., Bunyodkor koʻchasi, 1-uy"])
        == "Toshkent sh., Bunyodkor koʻchasi, 1-uy"
    )


def test_a_js_only_shell_needs_a_script_and_almost_no_text():
    shell = html(
        "<div id='root'>Yuklanmoqda, kuting...</div><script src='/a.js'></script>"
    )
    assert cs.parse_company_site(shell, url=SITE, fetched_at=AT) == []
    quiet = html("<div>Namuna Savdo. Tel +998 90 000 00 41</div>")
    assert len(cs.parse_company_site(quiet, url=SITE, fetched_at=AT)) == 1


def test_about_link_is_the_first_preferred_same_site_page_never_itself():
    page_ = html(
        '<a href="https://boshqa.example.uz/about">x</a>'
        '<a href="/kontakty">x</a><a href="/about">x</a>'
    )
    assert cs.about_link(page_, url=SITE) == f"{SITE}about"
    assert (
        cs.about_link(html('<a href="https://boshqa.example.uz/about">x</a>'), url=SITE)
        is None
    )
    assert cs.about_link(html('<a href="/uz/kontakty">x</a>'), url=SITE) == (
        f"{SITE}uz/kontakty"
    )
    assert cs.about_link(html('<a href="/kontakty">x</a>'), url=f"{SITE}kontakty") is None


def test_the_name_falls_back_to_the_schema_org_name():
    org = (
        '<script type="application/ld+json">'
        '{"@type": "Organization", "name": "Namuna JSON"}</script>'
    )
    assert parsed(FILLER, head=org).company_name_raw == "Namuna JSON"


def test_a_page_on_a_host_that_is_no_website_names_no_website():
    obs = parsed(FILLER, url="https://sites.google.com/view/namuna")
    assert obs.website is None
    assert parsed(FILLER).website == SITE


def test_phones_are_capped_and_handles_come_from_the_text_too():
    numbers = [f"+99890000005{n}" for n in range(7)]
    assert cs.unique_phones(numbers) == numbers[: cs.PHONES_MAX]
    obs = parsed(
        "<p>Telegram: @namuna_matn</p>"
        '<a href="https://namuna.example.uz/instagram/namuna_fake">Instagram</a>'
    )
    assert obs.telegram == "namuna_matn"
    assert obs.instagram is None


@pytest.mark.parametrize(
    ("url", "phone"),
    [
        ("https://wa.me/12345678", None),  # 8 digits: too short
        ("https://wa.me/1234567890123456", None),  # 16 digits: too long
        ("https://wa.me/998901234", "+998901234"),
    ],
)
def test_a_whatsapp_number_is_9_to_15_digits(url, phone):
    assert cs.whatsapp_phone(url) == phone


def test_a_hub_names_no_hub_as_the_website_keeps_nothing_empty_and_no_wholesale():
    other_hub = html(
        '<a href="https://linktr.ee/namuna_x">x</a>'
        '<a href="https://bit.ly/namuna">x</a>'
        '<a href="https://namuna-mebel.example.uz/">x</a>'
        '<a href="https://t.me/namuna_mebel">x</a>'
        "<p>Optom mebel</p>"
    )
    hub = cs.parse_link_hub(other_hub, url="https://taplink.cc/namuna", fetched_at=AT)
    assert len(hub) == 1
    assert hub[0].website == "https://namuna-mebel.example.uz/"
    assert "wholesale_bulky" not in hub[0].signal_types
    empty = html("<p>Salom</p>")
    assert cs.parse_link_hub(empty, url="https://taplink.cc/x", fetched_at=AT) == []


def channel(
    posts: list[tuple[str, str]], *, username: bool = True, about: str = ""
) -> bytes:
    """A t.me/s/ preview with ``posts`` = [(text, datetime attr)], oldest first."""
    header = (
        '<div class="tgme_channel_info_header_username">'
        '<a href="https://t.me/namuna_kanal">@namuna_kanal</a></div>'
        if username
        else ""
    )
    items = "".join(
        f'<div class="tgme_widget_message_text">{text}</div>'
        f'<time datetime="{when}">x</time>'
        for text, when in posts
    )
    return (
        "<html><body><div class='tgme_channel_info'>"
        "<div class='tgme_channel_info_header_title'>Namuna Kanal</div>"
        f"{header}<div class='tgme_channel_info_description'>{about}</div></div>"
        f"<section>{items}</section></body></html>"
    ).encode()


def test_the_channel_page_reads_its_newest_posts_and_aware_stamps_only():
    posts = [(f"Post {n}", f"2026-10-{10 + n:02d}T09:00:00+00:00") for n in range(6)]
    posts.append(("Eski post", "2026-10-30T09:00:00"))  # no zone: not a moment
    page_ = tp.parse_channel_page(channel(posts), url=tp.public_url("namuna_kanal"))
    assert page_ is not None
    assert page_.posts == ("Post 2", "Post 3", "Post 4", "Post 5", "Eski post")
    assert page_.newest_post_at == datetime.fromisoformat("2026-10-15T09:00:00+00:00")


def test_the_channel_name_falls_back_to_the_url():
    page_ = tp.parse_channel_page(
        channel([], username=False), url=tp.public_url("namuna_kanal")
    )
    assert page_ is not None and page_.username == "namuna_kanal"


def test_the_channel_s_posts_its_stir_and_instagram_are_read():
    url = tp.public_url("namuna_kanal")
    page_ = tp.parse_channel_page(
        channel(
            [
                (
                    "Tel: +998 90 000 00 42 · Instagram: @namuna.kanal",
                    "2026-10-20T09:00:00+00:00",
                )
            ],
            about="Xitoydan optom mato. STIR 300000042",
        ),
        url=url,
    )
    assert page_ is not None
    obs = tp.channel_observation(page_, url=url, fetched_at=AT)
    assert obs.phones == ["+998 90 000 00 42"]  # from a post, not the description
    assert obs.instagram == "namuna.kanal"
    assert obs.stir == "300000042" and "entity_solid" in obs.signal_types


def test_a_channel_names_no_hub_or_telegram_link_as_its_website():
    url = tp.public_url("namuna_kanal")
    links = (
        '<a href="https://t.me/namuna_kanal">t</a> <a href="https://taplink.cc/n">h</a>'
        ' <a href="https://namuna-kanal.example.uz/">s</a>'
    )
    page_ = tp.parse_channel_page(channel([], about=links), url=url)
    assert page_ is not None
    assert tp.channel_observation(page_, url=url, fetched_at=AT).website == (
        "https://namuna-kanal.example.uz/"
    )
