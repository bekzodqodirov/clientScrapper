"""The seven gates (04 §6, 04 AC 8): one failing fixture per gate, the first
failure decides, «kim kargo qiladi?» from a buyer passes ``not_competitor``,
a state word in the text only is no exclusion while one in the name is.

The pure half judges ``run_gates`` over views; the DB half reads the views
from rows (``gates.inputs_for`` — the self-description rule lives there: a
buyer's post is never read for competitor or B2C words) and walks a
candidate through ``score_job`` (``services/candidates``): exclusions and
suppressions at once, holds with the gate's window, gate 1's «not yet B2B»
waiting for the card. 04 §6's named fixtures: a cargo channel owner, a buyer
asking «kim kargo qiladi», a Chinese exhibitor, a client phone, a cooldown,
a liquidated STIR, a hokimiyat. Synthetic data only.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from clientscrapper.db.models import (
    ChatMonitor,
    Company,
    CompanyId,
    Evidence,
    LeadCandidate,
    ModelJob,
    Observation,
    Suppression,
)
from clientscrapper.services import candidates, gates
from clientscrapper.services.gates import (
    GateCompany,
    GateContact,
    GateSignal,
    ImportView,
    OwnText,
    Recent,
    run_gates,
)
from clientscrapper.services.suppression import Hit
from tests.gate_fixtures import (
    NOW,
    candidate,
    contact,
    firm,
    phone,
    push,
    row,
    seq,
    signal,
    tail,
)
from tests.test_uz_text import problems as uz_problems

ID = __import__("uuid").UUID(int=7)
PASSING = GateCompany(id=ID, display_name="Namuna Savdo MChJ", legal_form="MChJ")
NO_WINDOW = Recent()
PASS_MARKS = {
    "is_b2b": "pass:legal_form",
    "not_competitor": "pass",
    "not_supplier": "pass",
    "not_client": "pass",
    "not_dnc": "pass",
    "not_recent": "pass",
    "active_entity": "unknown",
}


def judge(
    company: GateCompany = PASSING,
    *,
    contacts: tuple[GateContact, ...] = (),
    signals: tuple[GateSignal, ...] = (),
    verdict: dict[str, Any] | None = None,
    hit: Hit | None = None,
    recent: Recent = NO_WINDOW,
) -> gates.GateResult:
    return run_gates(company, contacts, signals, verdict, hit, recent, NOW)


def only_failure(result: gates.GateResult) -> gates.Failure:
    """The fixture fails exactly one gate."""
    [failure] = result.failures
    return failure


def hit(kind: str, *, expires_at=None) -> Hit:
    return Hit(
        id=41,
        kind=kind,
        key_type="phone_tail",
        key="900000001",
        source="owner_bot",
        expires_at=expires_at,
    )


def vol(n: int, *, days: int = 3, key: str = "vol_stated_1_3") -> GateSignal:
    return GateSignal(key, evidence_id=n, observed_at=NOW - timedelta(days=days))


# --- the passing company and the card's words ----------------------------------------


def test_a_clean_company_passes_every_gate():
    result = judge(replace(PASSING, registry_status="active"))
    assert (result.decision, result.failures, result.decide()) == ("pass", (), None)
    assert result.marks == {**PASS_MARKS, "active_entity": "pass:active"}
    assert result.summary() == (
        "Darvozalar: B2B ✓ (MChJ) · raqobatchi emas ✓ · Xitoy yetkazib beruvchisi "
        "emas ✓ · mijoz emas ✓ · DNC yoʻq ✓ · 90 kunda yuborilmagan ✓ · reestr: faol"
    )


@pytest.mark.parametrize(
    "result",
    [
        judge(),
        judge(GateCompany(id=ID, display_name="Namuna")),
        judge(hit=hit("dnc")),
        judge(hit=hit("cooldown", expires_at=NOW + timedelta(days=9))),
        judge(replace(PASSING, registry_status="liquidated")),
        judge(replace(PASSING, is_competitor=True, is_supplier_cn=True, state="client")),
        judge(
            GateCompany(
                id=ID, display_name="Namuna", own=(OwnText("o'zim uchun", "seller"),)
            )
        ),
    ],
)
def test_the_gate_words_are_owner_text(result):
    for line in (*result.lines, result.summary()):
        assert uz_problems(line, rendered=True) == set(), line


# --- gate 1: is_b2b -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("company", "signals", "verdict", "mark", "word"),
    [
        (
            GateCompany(id=ID, display_name="Namuna Savdo MChJ"),
            (),
            None,
            "legal_form",
            "MChJ",
        ),
        (GateCompany(id=ID, display_name="ООО «Намуна»"), (), None, "legal_form", "ООО"),
        (
            GateCompany(id=ID, display_name="Namuna", names=("Namuna XK",)),
            (),
            None,
            "legal_form",
            "XK",
        ),
        (
            GateCompany(id=ID, display_name="Namuna", stir="300000011"),
            (),
            None,
            "stir",
            "STIR",
        ),
        (
            GateCompany(id=ID, display_name="Namuna", id_types=frozenset({"gis2_firm"})),
            (),
            None,
            "id",
            "katalog",
        ),
        (
            GateCompany(
                id=ID,
                display_name="Namuna",
                own=(OwnText("Mato optom narxda", "listing"),),
            ),
            (),
            None,
            "b2b_words",
            "optom",
        ),
        (
            GateCompany(
                id=ID,
                display_name="Namuna",
                import_signals=(ImportView("buyer", b2b=True, confidence=0.7),),
            ),
            (),
            None,
            "intent_b2b",
            "xaridor",
        ),
        (
            GateCompany(id=ID, display_name="Namuna"),
            (),
            {"is_b2b": True},
            "verdict",
            "tahlil",
        ),
        (
            GateCompany(id=ID, display_name="Namuna"),
            (vol(1), vol(2, days=89)),
            None,
            "volume",
            "hajm",
        ),
        # «within 90 d» holds the 90th day itself
        (
            GateCompany(id=ID, display_name="Namuna"),
            (vol(1), vol(2, days=90)),
            None,
            "volume",
            "hajm",
        ),
        (
            GateCompany(id=ID, display_name="Namuna", repeat_count_60d=3),
            (),
            None,
            "repeat",
            "takroriy",
        ),
    ],
)
def test_is_b2b_passes_on_each_kind_of_evidence(company, signals, verdict, mark, word):
    result = judge(company, signals=signals, verdict=verdict)
    assert result.marks["is_b2b"] == f"pass:{mark}"
    assert result.lines[0] == f"B2B ✓ ({word})"
    assert result.failures == ()


@pytest.mark.parametrize(
    ("company", "signals"),
    [
        (GateCompany(id=ID, display_name="Namuna"), ()),
        # one volume signal, or two of one evidence, or one older than 90 d
        (GateCompany(id=ID, display_name="Namuna"), (vol(1),)),
        (
            GateCompany(id=ID, display_name="Namuna"),
            (vol(1), vol(1, key="vol_container")),
        ),
        (GateCompany(id=ID, display_name="Namuna"), (vol(1), vol(2, days=91))),
        (GateCompany(id=ID, display_name="Namuna", repeat_count_60d=2), ()),
        # a B2B buyer below 0.7, a seller, a B2C verdict
        (
            GateCompany(
                id=ID,
                display_name="Namuna",
                import_signals=(
                    ImportView("buyer", b2b=True, confidence=0.69),
                    ImportView("seller", b2b=True, confidence=0.9),
                ),
            ),
            (),
        ),
    ],
)
def test_without_b2b_evidence_the_company_is_held(company, signals):
    result = judge(company, signals=signals)
    failure = only_failure(result)
    assert (failure.gate, failure.decision, failure.reason) == (
        "is_b2b",
        "hold",
        "not_b2b",
    )
    assert result.lines[0] == "B2B ? (tasdiqlanmagan)"
    # Not bound for a card: enrichment may still make it B2B.
    assert result.decide(card=False) is None
    assert result.decide(card=True) == failure


def test_b2c_words_without_volume_exclude_and_with_volume_hold():
    b2c = GateCompany(
        id=ID, display_name="Namuna", own=(OwnText("o'zim uchun 1 dona kerak", "seller"),)
    )
    failure = only_failure(judge(b2c))
    assert (failure.decision, failure.reason) == ("excluded", "b2c")
    # The exclusion is not deferred: it is not «may become B2B».
    assert judge(b2c).decide(card=False) == failure
    held = only_failure(judge(b2c, signals=(vol(1),)))
    assert (held.decision, held.reason) == ("hold", "not_b2b")
    marked = GateCompany(
        id=ID,
        display_name="Namuna",
        import_signals=(
            ImportView("buyer", b2b=False, b2c_markers=True, confidence=0.9),
        ),
    )
    assert only_failure(judge(marked)).reason == "b2c"


# --- gate 2: not_competitor -----------------------------------------------------------


@pytest.mark.parametrize(
    ("company", "signals", "verdict", "why"),
    [
        (replace(PASSING, is_competitor=True), (), None, "flag"),
        (replace(PASSING, display_name="Namuna Kargo MChJ"), (), None, "words"),
        (
            replace(
                PASSING, own=(OwnText("Xitoydan yuk tashish xizmati, kargo", "profile"),)
            ),
            (),
            None,
            "words",
        ),
        (PASSING, (), {"is_competitor": True}, "verdict"),
        (PASSING, (GateSignal("flag_competitor", 1, NOW),), None, "signal"),
        # 04 §6 fixture: a cargo channel owner
        (replace(PASSING, competitor_channel=True), (), None, "channel"),
    ],
)
def test_a_competitor_is_excluded(company, signals, verdict, why):
    result = judge(company, signals=signals, verdict=verdict)
    failure = only_failure(result)
    assert (failure.gate, failure.decision, failure.reason) == (
        "not_competitor",
        "excluded",
        "competitor",
    )
    assert result.marks["not_competitor"] == f"fail:{why}"


def test_a_buyer_asking_kim_kargo_qiladi_passes():
    """04 §6: the competitor words read the entity's self-description only —
    a buyer's question is no self-description, so ``own`` never carries it
    (the DB half below proves the loader keeps it out)."""
    buyer = GateCompany(
        id=ID,
        display_name="Namuna Tekstil Savdo MChJ",
        legal_form="MChJ",
        import_signals=(ImportView("buyer", b2b=True, confidence=0.9),),
    )
    result = judge(buyer)
    assert result.marks["not_competitor"] == "pass"
    assert result.failures == ()


# The T-19 review: an importer's own words about delivery are no cargo.
@pytest.mark.parametrize(
    ("name", "kind", "text"),
    [
        (
            "Namuna Maishiy Texnika MChJ",
            "listing",
            "Стиральные машины оптом со склада. Доставка по Ташкенту бесплатно.",
        ),
        (
            "Namuna Mebel MChJ",
            "listing",
            "Oshxona mebeli optom. Toshkent boʻylab yetkazib beramiz!",
        ),
        ("Namuna Mebel MChJ", "seller", "Bepul dostavka, optom narxlar"),
        ("Mebel Express MChJ", "profile", "Yumshoq mebel optom"),
        (
            "Namuna Santexnika MChJ",
            "profile",
            "Сантехника из Китая оптом, своя логистика и склад",
        ),
        ("Namuna Avto MChJ", "listing", "Barcha tovarlar rastamojka qilingan"),
        ("Namuna Avto MChJ", "listing", "Растаможенные запчасти в наличии"),
    ],
)
def test_an_importer_that_delivers_is_no_competitor(name, kind, text):
    company = GateCompany(
        id=ID,
        display_name=name,
        legal_form="MChJ",
        own=(OwnText(text, kind, title=name),),
    )
    result = judge(company)
    assert result.marks["not_competitor"] == "pass"
    assert result.failures == ()


@pytest.mark.parametrize(
    ("company", "found"),
    [
        # a name read for logistics, a page's own title too
        (replace(PASSING, display_name="Namuna Logistika MChJ"), True),
        (
            replace(
                PASSING,
                own=(OwnText("Xitoydan tovarlar", "profile", title="Namuna Logistics"),),
            ),
            True,
        ),
        # logistics in a self-description is no cargo …
        (replace(PASSING, own=(OwnText("Ombor va logistika", "profile"),)), False),
        # … unless offered as a service
        (
            replace(PASSING, own=(OwnText("Logistika xizmatlari, Xitoydan", "profile"),)),
            True,
        ),
        (replace(PASSING, own=(OwnText("Доставка грузов из Китая", "site"),)), True),
    ],
)
def test_a_name_reads_the_logistics_words_a_text_reads_the_cargo_core(company, found):
    assert (judge(company).marks["not_competitor"] == "fail:words") is found


# --- gate 3: not_supplier -------------------------------------------------------------


@pytest.mark.parametrize(
    ("company", "contacts", "verdict", "why"),
    [
        (replace(PASSING, is_supplier_cn=True), (), None, "flag"),
        (
            GateCompany(
                id=ID, display_name="Yiwu Namuna Trading Co., Ltd", stir="300000011"
            ),
            (),
            None,
            "name",
        ),
        # 04 §6 fixture: a Chinese exhibitor
        (replace(PASSING, exhibitor_countries=frozenset({"CN"})), (), None, "exhibitor"),
        (PASSING, (GateContact("+8613800000001"),), None, "phone"),
        (
            replace(
                PASSING,
                own=(
                    OwnText(
                        "义乌纳木纳",
                        "profile",
                        title="义乌纳木纳",
                        source_key="enrich_site",
                    ),
                ),
            ),
            (),
            None,
            "site_title",
        ),
        (
            replace(PASSING, urls=("https://namuna.1688.com/page/1",)),
            (),
            None,
            "seller_url",
        ),
        (replace(PASSING, urls=("https://www.alibaba.com/x",)), (), None, "seller_url"),
        (PASSING, (), {"is_supplier_cn": True}, "verdict"),
    ],
)
def test_a_chinese_supplier_is_excluded(company, contacts, verdict, why):
    result = judge(company, contacts=contacts, verdict=verdict)
    failure = only_failure(result)
    assert (failure.gate, failure.decision, failure.reason) == (
        "not_supplier",
        "excluded",
        "supplier_cn",
    )
    assert result.marks["not_supplier"] == f"fail:{why}"


@pytest.mark.parametrize(
    "company",
    [
        # a supplier-shaped name with a UZ legal form, or a UZ address
        GateCompany(id=ID, display_name="Namuna Import & Export MChJ"),
        GateCompany(
            id=ID, display_name="Namuna Co. Ltd", stir="300000011", city="Toshkent"
        ),
        # a CJK title on a page that is not the company's own site
        replace(
            PASSING,
            own=(OwnText("义乌", "listing", title="义乌", source_key="olx_business"),),
        ),
        replace(PASSING, urls=("https://not1688.com.example.uz/",)),
    ],
)
def test_an_uzbek_importer_is_no_supplier(company):
    assert judge(company).marks["not_supplier"] == "pass"


def test_an_anonymised_chinese_phone_is_no_evidence():
    assert (
        judge(contacts=(GateContact("+8613800000001", anonymised=True),)).failures == ()
    )


@pytest.mark.parametrize(
    ("name", "address", "city"),
    [
        # a Chinese exporter's card carries an address too (T-19 review)
        (
            "Yiwu Namuna Trading Co., Ltd.",
            "No. 5 Futian Road, Yiwu, Zhejiang, China",
            None,
        ),
        ("Guangzhou Namuna Import & Export Co., Ltd", "Baiyun District, Guangzhou", None),
        ("义乌市纳木纳贸易有限公司", "浙江省义乌市", None),
        ("Yiwu Namuna Co., Limited", None, "Yiwu"),
    ],
)
def test_a_supplier_name_with_a_chinese_address_is_a_supplier(name, address, city):
    company = GateCompany(id=ID, display_name=name, address=address, city=city)
    assert judge(company).marks["not_supplier"] == "fail:name"


@pytest.mark.parametrize(
    ("address", "city"),
    [
        ("Toshkent sh., Chilonzor tumani, Bunyodkor koʻchasi 1", None),
        ("г. Ташкент, ул. Намуна, 5", None),
        ("Chorsu bozori, 3-qator", None),
        (None, "Samarqand"),
    ],
)
def test_a_supplier_name_with_a_uz_address_is_no_supplier(address, city):
    company = GateCompany(
        id=ID,
        display_name="Namuna Import & Export Co., Ltd",
        address=address,
        city=city,
    )
    assert judge(company).marks["not_supplier"] == "pass"


@pytest.mark.parametrize(
    "country",
    ["CN", "CHN", "PRC", "CHINA", "CHINA (MAINLAND)", "XITOY", "ХИТОЙ", "КИТАЙ", "КНР"],
)
def test_every_chinese_country_spelling_is_an_exhibitor_from_china(country):
    company = replace(PASSING, exhibitor_countries=frozenset({country}))
    assert judge(company).marks["not_supplier"] == "fail:exhibitor"


# --- gates 4 and 5: the suppression hit -----------------------------------------------


def test_a_client_phone_suppresses():
    """04 §6 fixture: a client phone."""
    held = hit("client")
    failure = only_failure(judge(hit=held))
    assert (failure.gate, failure.decision, failure.reason, failure.hit) == (
        "not_client",
        "suppressed",
        "client",
        held,
    )


def test_a_client_company_without_a_row_is_held():
    failure = only_failure(judge(replace(PASSING, state="client")))
    assert (failure.gate, failure.decision, failure.reason) == (
        "not_client",
        "hold",
        "client_state",
    )


@pytest.mark.parametrize(
    "kind", ["legal", "dnc", "lost", "bounced", "competitor", "manager_veto", "b2c"]
)
def test_a_dnc_kind_suppresses(kind):
    result = judge(hit=hit(kind))
    failure = only_failure(result)
    assert (failure.gate, failure.decision, failure.reason) == (
        "not_dnc",
        "suppressed",
        kind,
    )
    assert result.marks["not_dnc"] == f"fail:{kind}"
    assert result.marks["not_client"] == "pass"


# --- gate 6: not_recent ---------------------------------------------------------------


def test_a_cooldown_holds_until_it_ends():
    """04 §6 fixture: a cooldown."""
    until = NOW + timedelta(days=180)
    failure = only_failure(judge(hit=hit("cooldown", expires_at=until)))
    assert (failure.gate, failure.decision, failure.reason, failure.until) == (
        "not_recent",
        "hold",
        "cooldown",
        until,
    )


@pytest.mark.parametrize(
    ("recent", "why"),
    [
        (Recent(company_push_until=NOW + timedelta(days=60)), "company_push"),
        (Recent(phone_push_until=NOW + timedelta(hours=2)), "phone_push"),
        (Recent(rejected_until=NOW + timedelta(days=100)), "rejected"),
    ],
)
def test_a_recent_window_holds(recent, why):
    failure = only_failure(judge(recent=recent))
    assert (failure.gate, failure.reason) == ("not_recent", why)
    assert judge(recent=recent).lines[5].startswith("kutish: ")


def test_the_hold_ends_with_the_earliest_window():
    recent = Recent(
        company_push_until=NOW + timedelta(days=60),
        phone_push_until=NOW + timedelta(hours=5),
        rejected_until=NOW + timedelta(days=100),
    )
    failure = only_failure(
        judge(recent=recent, hit=hit("cooldown", expires_at=NOW + timedelta(days=9)))
    )
    assert (failure.reason, failure.until) == ("phone_push", NOW + timedelta(hours=5))
    # A window already over is no window.
    over = Recent(company_push_until=NOW, phone_push_until=NOW - timedelta(minutes=1))
    assert judge(recent=over).failures == ()


def test_the_hold_line_prints_the_tashkent_date():
    """A hold ending at 01:00 on 2 Nov in Tashkent is the 2nd, whatever zone
    the database handed the moment back in (T-19 review)."""
    from datetime import UTC, datetime

    until = datetime(2026, 11, 1, 20, 0, tzinfo=UTC)
    result = judge(recent=Recent(company_push_until=until))
    assert result.lines[5] == "kutish: 02.11.2026 gacha"


# --- gate 7: active_entity ------------------------------------------------------------


def test_a_liquidated_stir_is_excluded():
    """04 §6 fixture: a liquidated STIR (the registry), or Places' closed."""
    company = replace(PASSING, stir="300000011", registry_status="liquidated")
    failure = only_failure(judge(company))
    assert (failure.gate, failure.decision, failure.reason) == (
        "active_entity",
        "excluded",
        "liquidated",
    )
    closed = only_failure(judge(signals=(GateSignal("neg_liquidated", 1, NOW),)))
    assert closed.reason == "liquidated"


def test_a_hokimiyat_is_excluded_by_its_name():
    """04 §6 fixture: a hokimiyat — a state word in the NAME excludes."""
    company = GateCompany(
        id=ID, display_name="Yunusobod tumani hokimligi", stir="200000011"
    )
    failure = only_failure(judge(company))
    assert (failure.gate, failure.decision, failure.reason) == (
        "active_entity",
        "excluded",
        "state_body",
    )
    assert judge(company).lines[6] == "davlat tashkiloti ✗"


def test_a_state_word_in_the_text_only_excludes_nothing():
    """04 §6: a state word only in the text is scoring's −20, never this."""
    company = replace(
        PASSING, own=(OwnText("Maktab mebeli va hokimiyat buyurtmalari", "listing"),)
    )
    assert judge(company).failures == ()
    alias = replace(PASSING, names=("Toshkent shahar hokimligi",))
    assert only_failure(judge(alias)).reason == "state_body"


@pytest.mark.parametrize(
    "name",
    [
        "Davlatbek Mebel MChJ",
        "Davlat Mebel Plus",
        "IP Davlatov",
        "Davlatov Savdo",
        "Maktab Mebel Import",
        "Maktab Jihozlari Savdo",
        "Shifoxona Jihozlari Savdo",
        "Namuna Reklama Agentligi",
    ],
)
def test_a_founder_s_name_or_an_equipment_trader_is_no_state_body(name):
    """T-19 review: a state body by its shape, never by a stem a given
    name or a school/hospital supplier carries."""
    company = GateCompany(id=ID, display_name=name, legal_form="MChJ")
    assert judge(company).marks["active_entity"] == "unknown"
    assert judge(company).failures == ()


@pytest.mark.parametrize(
    "name",
    [
        "Davlat soliq qoʻmitasi",
        "Toshkent shahar 3-son shifoxonasi",
        "12-son umumtaʼlim maktabi",
        "Sogʻliqni saqlash vazirligi",
        "ГУП «Намуна»",
        "Государственный налоговый комитет",
    ],
)
def test_a_state_body_shape_in_the_name_excludes(name):
    failure = only_failure(judge(GateCompany(id=ID, display_name=name, stir="200000011")))
    assert (failure.gate, failure.decision, failure.reason) == (
        "active_entity",
        "excluded",
        "state_body",
    )


def test_an_inactive_registry_entry_is_held():
    failure = only_failure(judge(replace(PASSING, registry_status="inactive")))
    assert (failure.gate, failure.decision, failure.reason) == (
        "active_entity",
        "hold",
        "inactive",
    )


# --- the order ------------------------------------------------------------------------


def test_the_first_failing_gate_decides():
    company = replace(
        PASSING,
        display_name="Namuna Kargo",
        legal_form=None,
        registry_status="liquidated",
    )
    result = judge(company, hit=hit("dnc"))
    assert [f.gate for f in result.failures] == [
        "is_b2b",
        "not_competitor",
        "not_dnc",
        "active_entity",
    ]
    assert result.decide().gate == "is_b2b"  # card-bound: the hold decides
    assert result.decide(card=False).gate == "not_competitor"
    assert result.decision == "hold"


# --- the DB half ----------------------------------------------------------------------


async def observe(
    session,
    owner: Company,
    *,
    kind: str,
    excerpt: str = "",
    name: str | None = None,
    source: str = "telegram_monitor",
    url: str | None = None,
    attrs: dict | None = None,
) -> Observation:
    n = seq()
    link = url or f"https://t.me/namuna_optom/{n}"
    ev = Evidence(
        source_key=source,
        url=link,
        fetched_at=NOW,
        sha256=hashlib.sha256(f"obs:{n}".encode()).hexdigest(),
        bytes=0,
        excerpt=excerpt,
        expires_at=NOW + timedelta(days=180),
    )
    session.add(ev)
    await session.flush()
    obs = Observation(
        evidence_id=ev.id,
        source_key=source,
        status="resolved",
        company_id=owner.id,
        parsed={
            "v": 1,
            "kind": kind,
            "source_key": source,
            "url": link,
            "company": {"name_raw": name, "goods_text": None},
            "text": {"excerpt": excerpt},
            "hints": {"collector": {"signal_types": [], "attrs": attrs or {}}},
        },
    )
    session.add(obs)
    await session.flush()
    return obs


async def test_the_loader_reads_self_descriptions_only(session):
    owner = await firm(session, "Namuna Tekstil Savdo MChJ")
    await observe(
        session,
        owner,
        kind="intent_post",
        excerpt="Guangzhoudan 2 kub mato, kim kargo qiladi?",
    )
    got = await gates.inputs_for(session, owner.id, now=NOW)
    assert got.company.own == ()
    result = await gates.evaluate(session, owner.id, now=NOW)
    assert result.marks["not_competitor"] == "pass" and result.failures == ()
    # The same words in the company's own channel post are a competitor.
    await observe(session, owner, kind="profile", excerpt="Xitoydan kargo xizmati")
    result = await gates.evaluate(session, owner.id, now=NOW)
    assert only_failure(result).reason == "competitor"


async def test_the_loader_reads_ids_channels_models_and_attrs(session):
    owner = await firm(
        session, "Namuna", legal_form=None, telegram_channel="namuna_kanal"
    )
    session.add(
        CompanyId(company_id=owner.id, id_type="gis2_firm", id_value=f"7000{seq()}")
    )
    session.add(
        ChatMonitor(
            tg_chat_id=-1001000000099,
            chat_type="channel",
            username="namuna_kanal",
            role="competitor",
        )
    )
    session.add(
        ModelJob(
            kind="classify",
            lane="instant",
            model="test",
            custom_id=f"cs-{seq()}",
            status="applied",
            company_id=owner.id,
            result={
                "speaker_role": "buyer",
                "b2b": True,
                "b2c_markers": False,
                "confidence": 0.8,
            },
        )
    )
    await observe(session, owner, kind="intent_post", attrs={"repeat_count_60d": 4})
    await observe(
        session, owner, kind="exhibitor", source="exhibitors", attrs={"country": "cn"}
    )
    await observe(
        session,
        owner,
        kind="listing",
        source="olx_business",
        url="https://namuna.1688.com/x",
    )
    got = await gates.inputs_for(session, owner.id, now=NOW)
    company = got.company
    assert "gis2_firm" in company.id_types
    assert company.competitor_channel is True
    assert company.import_signals == (ImportView("buyer", b2b=True, confidence=0.8),)
    assert company.repeat_count_60d == 4
    assert company.exhibitor_countries == frozenset({"CN"})
    assert "https://namuna.1688.com/x" in company.urls
    result = run_gates(
        company, got.contacts, got.signals, got.verdict, got.hit, got.recent, NOW
    )
    assert result.marks["is_b2b"] == "pass:id"
    assert result.marks["not_competitor"] == "fail:channel"
    assert result.marks["not_supplier"] == "fail:exhibitor"


async def test_the_loader_reads_the_push_windows(session):
    owner = await firm(session)
    lead = await contact(session, owner, phone(51))
    old = await candidate(
        session, owner, state="pushed", lead=lead, external_id=f"cs:{owner.id}:202609"
    )
    await push(
        session,
        old,
        sent_at=NOW - timedelta(days=20),
        phone_tail=tail(51),
        external_id=f"cs:{owner.id}:202609",
    )
    other = await firm(session)
    stranger = await candidate(
        session, other, state="pushed", external_id=f"cs:{other.id}:202610"
    )
    await push(
        session,
        stranger,
        sent_at=NOW - timedelta(hours=3),
        phone_tail=tail(51),
        external_id=f"cs:{other.id}:202610",
    )
    got = await gates.inputs_for(session, owner.id, now=NOW)
    assert got.recent == Recent(
        company_push_until=NOW - timedelta(days=20) + gates.OPEN_PUSH_WINDOW,
        phone_push_until=NOW - timedelta(hours=3) + gates.PHONE_PUSH_WINDOW,
    )
    # A superseded row and the push's own id count for nothing.
    excluded = await gates.recent_pushes(
        session,
        lineage=got.lineage,
        tails=[tail(51)],
        now=NOW,
        exclude_external_id=f"cs:{owner.id}:202609",
    )
    assert excluded.company_push_until is None


async def test_a_closed_outcome_ends_the_90_day_window(session):
    from clientscrapper.db.models import Outcome

    owner = await firm(session)
    eid = f"cs:{owner.id}:202609"
    old = await candidate(session, owner, state="pushed", external_id=eid)
    await push(
        session,
        old,
        sent_at=NOW - timedelta(days=20),
        phone_tail=tail(52),
        external_id=eid,
    )
    session.add(
        Outcome(
            external_id=eid,
            intake_id=__import__("uuid").uuid4(),
            company_id=owner.id,
            landing="created",
            stage_kind="lost",
            erp_at=NOW,
            label="lost_other",
            precision_class="neutral",
            raw={},
        )
    )
    await session.flush()
    got = await gates.inputs_for(session, owner.id, now=NOW)
    assert got.recent.company_push_until is None


# --- through score_job ----------------------------------------------------------------


async def scored(session, cand: LeadCandidate) -> LeadCandidate:
    await session.commit()
    await candidates.score_batch(now=NOW)
    got = await session.get(LeadCandidate, cand.id, populate_existing=True)
    assert got is not None
    return got


async def test_a_competitor_is_excluded_flagged_and_suppressed(session):
    owner = await firm(session, "Namuna Logistika MChJ")
    lead = await contact(session, owner, phone(53))
    cand = await candidate(session, owner, state="scored", lead=lead, last_scored_at=None)
    await signal(session, owner)
    got = await scored(session, cand)
    assert (got.state, got.exclude_reason) == ("excluded", "competitor")
    flag = await session.scalar(
        sa.select(Company.is_competitor)
        .where(Company.id == owner.id)
        .execution_options(populate_existing=True)
    )
    rows = (
        await session.execute(
            sa.select(
                Suppression.kind,
                Suppression.key_type,
                Suppression.key,
                Suppression.source,
            )
        )
    ).all()
    assert flag is True
    assert [tuple(r) for r in rows] == [
        ("competitor", "company_id", str(owner.id), "resolver")
    ]


async def test_a_supplier_is_excluded_and_flagged(session):
    owner = await firm(session, "Namuna", legal_form=None, stir="300000014")
    lead = await contact(session, owner, phone(54))
    await contact(session, owner, "+8613800000001")
    cand = await candidate(session, owner, state="scored", lead=lead, last_scored_at=None)
    await signal(session, owner)
    got = await scored(session, cand)
    flag = await session.scalar(
        sa.select(Company.is_supplier_cn)
        .where(Company.id == owner.id)
        .execution_options(populate_existing=True)
    )
    assert (got.state, got.exclude_reason, flag) == ("excluded", "supplier_cn", True)


async def test_a_liquidated_company_is_excluded_and_a_hokimiyat_too(session):
    gone = await firm(session, registry_status="liquidated")
    body = await firm(
        session, "Chilonzor tumani hokimligi", legal_form=None, stir="200000012"
    )
    out = []
    for owner, nn in ((gone, 55), (body, 56)):
        lead = await contact(session, owner, phone(nn))
        cand = await candidate(
            session, owner, state="scored", lead=lead, last_scored_at=None
        )
        await signal(session, owner)
        out.append(cand)
    await scored(session, out[0])
    got = [await session.get(LeadCandidate, c.id, populate_existing=True) for c in out]
    assert [(g.state, g.exclude_reason) for g in got if g] == [
        ("excluded", "liquidated"),
        ("excluded", "state_body"),
    ]


async def test_not_yet_b2b_waits_for_the_card(session):
    """Gate 1's hold is judged before a card: a warm candidate goes on to
    enrichment, a hot one is held instead of carded."""
    warm_firm = await firm(session, "Namuna Bir", legal_form=None)
    warm = await candidate(
        session, warm_firm, state="scored", lead=None, last_scored_at=None
    )
    await signal(session, warm_firm)  # 40 + recency: warm without a phone
    hot_firm = await firm(session, "Namuna Ikki", legal_form=None)
    lead = await contact(session, hot_firm, phone(57))
    hot = await candidate(
        session, hot_firm, state="scored", lead=lead, last_scored_at=None
    )
    await signal(session, hot_firm)
    await signal(session, hot_firm, "cn_origin")
    await session.commit()
    await candidates.score_batch(now=NOW)
    w = await session.get(LeadCandidate, warm.id, populate_existing=True)
    h = await session.get(LeadCandidate, hot.id, populate_existing=True)
    assert w is not None and h is not None
    assert (w.state, w.tier) == ("enriching", "warm")
    assert (h.state, h.tier, h.hold_until) == ("hold", "hot", None)
    assert h.score_breakdown is not None
    assert h.score_breakdown["gates"]["is_b2b"] == "fail:not_b2b"


async def test_a_b2c_buyer_is_excluded(session):
    owner = await firm(session, "Namuna", legal_form=None)
    lead = await contact(session, owner, phone(58))
    await observe(
        session,
        owner,
        kind="seller",
        source="olx_business",
        excerpt="o'zim uchun 1 dona sotaman",
    )
    cand = await candidate(session, owner, state="scored", lead=lead, last_scored_at=None)
    await signal(session, owner, "cn_origin")
    got = await scored(session, cand)
    assert (got.state, got.exclude_reason) == ("excluded", "b2c")


async def test_a_recent_push_holds_until_its_window_ends(session):
    owner = await firm(session)
    lead = await contact(session, owner, phone(59))
    other = await firm(session)
    stranger = await candidate(
        session, other, state="pushed", external_id=f"cs:{other.id}:202610"
    )
    sent = NOW - timedelta(hours=3)
    await push(
        session,
        stranger,
        sent_at=sent,
        phone_tail=tail(59),
        external_id=f"cs:{other.id}:202610",
    )
    cand = await candidate(session, owner, state="scored", lead=lead, last_scored_at=None)
    await signal(session, owner)
    got = await scored(session, cand)
    assert (got.state, got.hold_until) == ("hold", sent + gates.PHONE_PUSH_WINDOW)


@pytest.mark.parametrize(("held_days", "kept_days"), [(30, 30), (1, 9)])
async def test_a_held_candidate_keeps_the_later_hold(session, held_days, kept_days):
    """A gate's window extends a hold, never shortens it."""
    owner = await firm(session)
    lead = await contact(session, owner, phone(60))
    cool = NOW + timedelta(days=9)
    await row(session, "cooldown", "company_id", str(owner.id), expires_at=cool)
    cand = await candidate(
        session,
        owner,
        state="hold",
        lead=lead,
        hold_until=NOW + timedelta(days=held_days),
        last_scored_at=None,
    )
    await signal(session, owner)
    got = await scored(session, cand)
    assert (got.state, got.hold_until) == ("hold", NOW + timedelta(days=kept_days))


# --- the T-19 review: the loader's filters and the lineage, pinned --------------------


async def test_a_wholesale_importer_that_delivers_is_scored_not_excluded(session):
    """The review's probe, end to end: an OLX wholesaler whose listing says
    «Доставка по Ташкенту бесплатно» is no cargo competitor — no exclusion,
    no company flag, no company-wide competitor row."""
    owner = await firm(session, "Namuna Maishiy Texnika MChJ")
    lead = await contact(session, owner, phone(71))
    cand = await candidate(session, owner, state="scored", lead=lead, last_scored_at=None)
    await signal(session, owner, "vol_wholesale_bulky")
    await observe(
        session,
        owner,
        kind="listing",
        source="olx_business",
        url="https://www.olx.uz/d/obyavlenie/namuna-1.html",
        excerpt="Стиральные машины оптом со склада, из Китая. "
        "Доставка по Ташкенту бесплатно.",
    )
    got = await scored(session, cand)
    flag = await session.scalar(
        sa.select(Company.is_competitor)
        .where(Company.id == owner.id)
        .execution_options(populate_existing=True)
    )
    rows = await session.scalar(sa.select(sa.func.count(Suppression.id)))
    assert got.state != "excluded" and got.exclude_reason is None
    assert (flag, rows) == (False, 0)
    assert got.score_breakdown is not None
    assert got.score_breakdown["gates"]["not_competitor"] == "pass"


@pytest.mark.parametrize(
    ("status", "read"),
    [("applied", True), ("fallback", True), ("failed", False), ("pending", False)],
)
async def test_only_a_finished_classify_job_is_read(session, status, read):
    owner = await firm(session, "Namuna", legal_form=None)
    session.add(
        ModelJob(
            kind="classify",
            lane="instant",
            model="test",
            custom_id=f"cs-{seq()}",
            status=status,
            company_id=owner.id,
            result={"speaker_role": "buyer", "b2b": True, "confidence": 0.9},
        )
    )
    await session.flush()
    got = await gates.inputs_for(session, owner.id, now=NOW)
    assert bool(got.company.import_signals) is read


@pytest.mark.parametrize("status", ["new", "rejected", "needs_review"])
async def test_an_unresolved_observation_is_no_self_description(session, status):
    owner = await firm(session)
    obs = await observe(session, owner, kind="profile", excerpt="Xitoydan kargo xizmati")
    obs.status = status
    await session.flush()
    got = await gates.inputs_for(session, owner.id, now=NOW)
    assert got.company.own == ()


async def test_a_competitor_channel_matches_whatever_case_it_was_seeded_in(session):
    owner = await firm(session, telegram_channel="namuna_kanal")
    session.add(
        ChatMonitor(
            tg_chat_id=-1001000000098,
            chat_type="channel",
            username="Namuna_Kanal",
            role="competitor",
        )
    )
    await session.flush()
    got = await gates.inputs_for(session, owner.id, now=NOW)
    assert got.company.competitor_channel is True


@pytest.mark.parametrize(("days", "held"), [(89, True), (91, False)])
async def test_an_open_push_holds_for_ninety_days_and_no_longer(session, days, held):
    owner = await firm(session)
    eid = f"cs:{owner.id}:202607"
    old = await candidate(session, owner, state="pushed", external_id=eid)
    sent = NOW - timedelta(days=days)
    await push(session, old, sent_at=sent, phone_tail=tail(72), external_id=eid)
    got = await gates.inputs_for(session, owner.id, now=NOW)
    assert got.recent.company_push_until == (
        sent + gates.OPEN_PUSH_WINDOW if held else None
    )


async def test_a_push_in_flight_counts_for_the_windows(session):
    """A committed ``sending`` row whose POST has not answered may land
    (02 §2.10 rule 5 (a)): it holds the phone and the company like a sent
    one (T-19 review)."""
    owner = await firm(session)
    eid = f"cs:{owner.id}:202610"
    old = await candidate(session, owner, state="approved")
    sent = NOW - timedelta(minutes=1)
    await push(
        session,
        old,
        sent_at=sent,
        phone_tail=tail(73),
        external_id=eid,
        status="sending",
        http_status=None,
    )
    got = await gates.recent_pushes(
        session, lineage=[owner.id], tails=[tail(73)], now=NOW
    )
    assert got.phone_push_until == sent + gates.PHONE_PUSH_WINDOW
    assert got.company_push_until == sent + gates.OPEN_PUSH_WINDOW
    assert gates.PUSH_COUNTED_STATUSES == ("sent", "unknown", "sending")


async def test_only_a_rejected_candidate_opens_the_rejection_window(session):
    owner = await firm(session)
    at = NOW - timedelta(days=10)
    await candidate(
        session,
        owner,
        state="excluded",
        exclude_reason="owner",
        score=40,
        rejected_at=at,
    )
    got = await gates.inputs_for(session, owner.id, now=NOW)
    assert got.recent.rejected_until is None
    await candidate(session, owner, state="rejected", score=40, rejected_at=at)
    got = await gates.inputs_for(session, owner.id, now=NOW)
    assert got.recent.rejected_until == at + candidates.REOPEN_AFTER_REJECT


@pytest.mark.parametrize("state", ["merged", "purged"])
async def test_a_client_hit_leaves_a_merged_or_purged_company_as_it_is(session, state):
    owner = await firm(session)
    target = await firm(session)
    values: dict[str, Any] = {"state": state}
    if state == "merged":
        values["merged_into"] = target.id
    await session.execute(
        sa.update(Company).where(Company.id == owner.id).values(**values)
    )
    client = gates.Failure("not_client", "suppressed", "client", hit=hit("client"))
    await gates.apply_effects(session, owner.id, client)
    stored = await session.scalar(
        sa.select(Company.state)
        .where(Company.id == owner.id)
        .execution_options(populate_existing=True)
    )
    assert stored == state
    await gates.apply_effects(session, target.id, client)
    assert (
        await session.scalar(
            sa.select(Company.state)
            .where(Company.id == target.id)
            .execution_options(populate_existing=True)
        )
        == "client"
    )


async def test_a_merged_source_s_flag_and_name_reach_the_root(session):
    """The lineage (04 §2.5): a merged source's competitor flag and its own
    display name are the root's to answer for."""
    from clientscrapper.services import companies

    root = await firm(session, "Namuna Savdo MChJ")
    flagged = await firm(session, "Namuna Bir", is_competitor=True)
    named = await firm(session, "Namuna Kargo Servis")
    await companies.merge_into(session, flagged, root, by="owner:111111111", now=NOW)
    got = await gates.inputs_for(session, root.id, now=NOW)
    assert got.company.is_competitor is True
    other = await firm(session, "Namuna Ikki MChJ")
    await companies.merge_into(session, named, other, by="owner:111111111", now=NOW)
    got = await gates.inputs_for(session, other.id, now=NOW)
    assert got.company.is_competitor is False
    assert "Namuna Kargo Servis" in got.company.names
    result = run_gates(
        got.company, got.contacts, got.signals, got.verdict, got.hit, got.recent, NOW
    )
    assert result.marks["not_competitor"] == "fail:words"


def test_a_review_card_is_card_bound_whatever_its_walk():
    """A candidate sitting in a card is judged by gate 1 even when its walk
    takes it back to ``hold`` (06 §1.3: a review card lost its phone)."""
    assert candidates.card_bound("review", ("hold",)) is True
    assert candidates.card_bound("review", ()) is True
    assert candidates.card_bound("scored", ("enriching",)) is False
    assert candidates.card_bound("scored", ("review",)) is True
    assert candidates.card_bound("hold", ("scored", "approved")) is True


async def test_a_found_candidate_a_gate_holds_passes_through_scored(session):
    """02 §2.9 has no ``found → hold``: the gate's hold walks
    ``found → scored → hold`` with the gate's window."""
    owner = await firm(session)
    lead = await contact(session, owner, phone(74))
    other = await firm(session)
    stranger = await candidate(
        session, other, state="pushed", external_id=f"cs:{other.id}:202610"
    )
    sent = NOW - timedelta(hours=2)
    await push(
        session,
        stranger,
        sent_at=sent,
        phone_tail=tail(74),
        external_id=f"cs:{other.id}:202610",
    )
    cand = await candidate(
        session, owner, state="found", lead=lead, score=None, last_scored_at=None
    )
    await signal(session, owner)
    got = await scored(session, cand)
    assert (got.state, got.hold_until) == ("hold", sent + gates.PHONE_PUSH_WINDOW)


async def test_the_gate_marks_keep_the_phone_less_handoff_mark(session):
    """The marks are merged into ``score_breakdown.gates``, never written
    over it: the handoff's ``no_phone`` survives a re-score, or the next one
    would send the phone-less card back to ``hold``."""
    owner = await firm(session)
    await contact(session, owner, None, tier="person", telegram_username="namuna_bir")
    cand = await candidate(
        session,
        owner,
        state="review",
        score=65,
        last_scored_at=None,
        score_breakdown={"gates": {"no_phone": True}},
    )
    await signal(session, owner)
    await signal(session, owner, "cn_origin")
    got = await scored(session, cand)
    assert got.state == "review"
    assert got.score_breakdown is not None
    assert got.score_breakdown["gates"]["no_phone"] is True
    assert got.score_breakdown["gates"]["not_competitor"] == "pass"
    await session.execute(
        sa.update(LeadCandidate)
        .where(LeadCandidate.id == cand.id)
        .values(last_scored_at=None)
    )
    again = await scored(session, cand)
    assert again.state == "review"


async def test_an_anonymised_contact_s_tail_holds_no_phone_window(session):
    owner = await firm(session)
    await contact(session, owner, phone(75), anonymised_at=NOW - timedelta(days=1))
    other = await firm(session)
    stranger = await candidate(
        session, other, state="pushed", external_id=f"cs:{other.id}:202610"
    )
    await push(
        session,
        stranger,
        sent_at=NOW - timedelta(hours=2),
        phone_tail=tail(75),
        external_id=f"cs:{other.id}:202610",
    )
    got = await gates.inputs_for(session, owner.id, now=NOW)
    assert (got.tails, got.recent.phone_push_until) == ([], None)


async def test_the_tail_about_to_be_pushed_is_read_like_a_contact_s(session):
    """``inputs_for(tails=…)``: a phone no live contact carries yet is
    looked up and windowed too."""
    owner = await firm(session)
    other = await firm(session)
    stranger = await candidate(
        session, other, state="pushed", external_id=f"cs:{other.id}:202610"
    )
    sent = NOW - timedelta(hours=2)
    await push(
        session,
        stranger,
        sent_at=sent,
        phone_tail=tail(76),
        external_id=f"cs:{other.id}:202610",
    )
    dnc = await row(session, "dnc", "phone_tail", tail(76))
    got = await gates.inputs_for(session, owner.id, now=NOW, tails=[tail(76)])
    assert got.tails == [tail(76)]
    assert got.recent.phone_push_until == sent + gates.PHONE_PUSH_WINDOW
    assert got.hit is not None and got.hit.id == dnc.id
