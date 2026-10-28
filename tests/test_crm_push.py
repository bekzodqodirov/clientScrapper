"""crm_push against the fake ERP (07 §1.6, 05 §B.1, §B.4, 02 §2.10).

The payload half is pure: a ``CandidateView`` built here, the body and the
note the ERP would keep (``landed_note`` against the fake's own
``fake_erp.landed_note``, a copy of fromPlain). The push half runs on
Postgres with ``tests/fake_erp.py`` as the ERP: one approved candidate per
test, ``push_one`` / ``crm_push_job`` / ``reconcile_job`` with an explicit
clock (``NOW`` is a Tuesday 11:00 Tashkent, inside the push window), and
the fake's modes for every answer the door can give. Synthetic data only
(07 §1.0): «Namuna …», +998 90 000 00 NN, STIR 3000000NN, *.example.uz.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import (
    Company,
    Evidence,
    LeadCandidate,
    LeadReview,
    Outcome,
    PushLog,
    ReminderLog,
    Source,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import (
    costs,
    crm_push,
    erp_feeds,
    health,
    push_gate,
    scoring,
)
from clientscrapper.services.candidates import CandidateView, PersonView
from clientscrapper.services.crm_push import PushPayload
from clientscrapper.services.erp_feeds import ErpClient
from clientscrapper.tools import push_test
from tests import fake_erp, gate_fixtures
from tests.fake_erp import FakeErp
from tests.fixtures.candidate_views import EXCERPT, sig, view
from tests.gate_fixtures import KEY, NOW, phone, tail

BASE = "https://erp.test"
OTHER_KEY = "other-key"
DAY = timedelta(days=1)


# --- the pure half ------------------------------------------------------------------


def fake_landed(p: PushPayload) -> str:
    """What the fake ERP keeps of this body (its own fromPlain copy)."""
    note = fake_erp.landed_note(p.model_dump(exclude_none=True))
    assert note is not None
    return note


def test_payload_sections_and_limits():
    v = view(
        person=PersonView("Namuna Akmal", "rahbar", None),
        opener="Assalomu alaykum, Toshkentdagi mato savdongizni koʻrib yozyapman.",
    )
    p = crm_push.build_payload(v)
    lines = p.note.split("\n")
    assert [line.split(":")[0].strip() for line in lines] == [
        "NEGA TOPILDI",
        "https",
        "NIMA OLIB KELADI",
        "HAJM",
        "KOMPANIYA",
        "Masʼul",
        "NIMA DEYISH",
        "BALL",
    ]
    assert lines[0] == ("NEGA TOPILDI: Telegram guruh · 25.10.2026 · «5 kub mato kerak»")
    assert lines[1] == "https://t.me/namuna_optom/101"
    assert lines[2] == (
        "NIMA OLIB KELADI: mato, aksessuar · Xitoy: ha («Xitoydan har oy keltiramiz»)"
    )
    assert lines[3] == "HAJM: 5 kub"
    assert lines[4] == (
        "KOMPANIYA: Namuna Tekstil Savdo MChJ · STIR 300000011 · Toshkent · faol"
        " · tekstil"
    )
    assert lines[5] == "  Masʼul: Namuna Akmal (rahbar)"
    assert lines[7] == (
        "BALL: 74/100 (hajm 30, Xitoy 20, firma 8, yangilik 10, aloqa 6, tasdiq 0)"
    )
    # The body: field order is the note-line order; utm_source right after
    # the note; only the four identity names; exclude_none.
    body = p.model_dump(exclude_none=True)
    assert list(body) == [
        "external_id",
        "name",
        "phone",
        "note",
        "utm_source",
        "kub",
        "firma",
        "stir",
        "telegram",
        "instagram",
        "website",
        "address",
        "imports",
        "score",
        "evidence",
    ]
    assert body["utm_source"] == "clientscrapper"
    assert body["kub"] == "5"
    assert body["telegram"] == "@namuna_tekstil"
    assert body["evidence"] == 'https://t.me/namuna_optom/101 — "5 kub mato kerak"'
    assert body["name"] == "Namuna Tekstil Savdo"
    landed = crm_push.landed_note(p)
    assert landed == fake_landed(p)
    assert landed.startswith(lines[0] + " https://t.me/")
    assert " utm_source: clientscrapper kub: 5 firma: " in landed
    # NEGA TOPILDI is whole within the seller's push preview (159 code points).
    assert landed[: crm_push.PUSH_PREVIEW].startswith(lines[0])
    assert push_gate.text_len(p.note) <= 700
    assert push_gate.text_len(landed) <= 1000
    # The gate's own invariants hold for the builder's body.
    assert (
        push_gate.payload_problems(
            body, display_name=v.display_name, phone=v.phone, stated_m3=v.stated_m3
        )
        == []
    )


def test_payload_without_optional_facts_drops_their_keys():
    v = view(
        legal_form=None,
        stir=None,
        city=None,
        address=None,
        website_domain=None,
        telegram=None,
        instagram=None,
        sectors=(),
        goods=(),
        stated_m3=None,
        volume_band="unknown",
        signals=(sig(value=None),),
    )
    body = crm_push.build_payload(v).model_dump(exclude_none=True)
    assert list(body) == [
        "external_id",
        "name",
        "phone",
        "note",
        "utm_source",
        "firma",
        "score",
        "evidence",
    ]
    assert body["note"].split("\n")[2:] == [
        "KOMPANIYA: Namuna Tekstil Savdo · STIR — · faol",
        "BALL: 74/100 (hajm 30, Xitoy 20, firma 8, yangilik 10, aloqa 6, tasdiq 0)",
    ]


def test_no_field_is_reserved_and_identity_is_exactly_four():
    fields = list(PushPayload.model_fields)
    assert {f.lower() for f in fields} & crm_push.RESERVED_BODY_KEYS == set()
    assert {
        "key",
        "google_key",
        "secret",
        "token",
        "id",
        "ism",
        "telefon",
        "izoh",
        "user_column_data",
    } == crm_push.RESERVED_BODY_KEYS
    assert crm_push.IDENTITY_KEYS == ("external_id", "name", "phone", "note")
    assert fields[:4] == list(crm_push.IDENTITY_KEYS)
    assert fields[4] == "utm_source"
    # One of company/firma, never both (fromPlain's company pass-through).
    assert "firma" in fields and "company" not in fields
    # Every key the fake's fromPlain would consume as identity is ours or absent.
    assert set(fields) & fake_erp.KNOWN_BODY_KEYS == {
        "external_id",
        "name",
        "phone",
        "note",
    }


def test_person_only_with_legal_basis_and_name_is_the_company():
    # load_view hands a PersonView only for a legal_basis row; without one the
    # note names nobody and the body's name is the company's.
    v = view(person=None)
    p = crm_push.build_payload(v)
    assert "Masʼul" not in p.note
    assert p.name == "Namuna Tekstil Savdo"
    for token in ("<PHONE_", "<TG_", "<NAME_"):
        assert token not in json.dumps(p.model_dump(), ensure_ascii=False)


def test_a_quote_with_a_phone_or_handle_is_not_cited():
    shown = sig(
        quote="5 kub mato kerak, tel +998 90 000 00 77",
        excerpt="Kerak: 5 kub mato kerak, tel +998 90 000 00 77",
        evidence_id=3,
        points=30,
    )
    handle = sig(
        quote="mato kerak @namuna_boshqa",
        excerpt="mato kerak @namuna_boshqa",
        evidence_id=4,
    )
    clean_one = sig(
        "vol_wholesale_bulky",
        quote="optom mato olamiz",
        excerpt="Biz optom mato olamiz",
        points=20,
        value=None,
        evidence_id=5,
    )
    p = crm_push.build_payload(view(signals=(shown, handle, clean_one)))
    assert p.note.split("\n")[0].endswith("«optom mato olamiz»")
    assert "000 00 77" not in p.note and "namuna_boshqa" not in p.note
    # A quote that is not a verbatim substring of its excerpt is never cited.
    forged = sig(quote="10 kub kerak", excerpt="Biz mato sotamiz")
    with pytest.raises(crm_push.PayloadError) as caught:
        crm_push.build_payload(view(signals=(forged,)))
    assert caught.value.code == "no_quote"


def test_payload_drops_nul_and_lone_surrogates():
    quote = "5 kub\x00 mato \ud83d kerak"
    s = sig(quote=quote, excerpt="Yiwu: " + quote)
    p = crm_push.build_payload(
        view(
            signals=(s,),
            legal_form="MChJ",
            address="Namuna\x00 koʻchasi \udc00 1",
            goods=("ma\x00to",),
        )
    )
    body = p.model_dump(exclude_none=True)
    blob = json.dumps(body, ensure_ascii=False)
    assert "\x00" not in blob
    assert all(not 0xD800 <= ord(ch) <= 0xDFFF for value in body.values() for ch in value)
    assert body["address"] == "Namuna koʻchasi 1"
    assert body["imports"] == "mato"
    assert p.note.split("\n")[0].endswith("«5 kub mato kerak»")
    # A NUL in the display name: the name cannot be sent as is — no body.
    with pytest.raises(crm_push.PayloadError) as caught:
        crm_push.build_payload(view(display_name="Namuna\x00 Savdo"))
    assert caught.value.code == "name"
    # firma is built from the cleaned name: no NUL reaches it either.
    p2 = crm_push.build_payload(view(legal_form="MChJ"))
    assert "\x00" not in (p2.firma or "")


def test_landed_note_skips_empty_values():
    p = crm_push.build_payload(view())
    with_empty = p.model_copy(update={"stir": "", "website": " \t﻿ "})
    landed = crm_push.landed_note(with_empty)
    assert "stir:" not in landed and "website:" not in landed
    assert landed == fake_landed(with_empty)
    # build_payload itself never sends one: an empty value is None.
    assert crm_push.clean("  ") is None and crm_push.clean("﻿") is None


def test_landed_note_js_whitespace():
    p = crm_push.build_payload(view())
    odd = p.model_copy(update={"stir": "300﻿000011", "website": "a b"})
    landed = crm_push.landed_note(odd)
    assert "stir: 300 000011" in landed and "website: a b" in landed
    assert landed == fake_landed(odd)
    # U+001C–U+001F and U+0085 are no JS whitespace: build_payload maps them.
    for ch in "\x1c\x1d\x1e\x1f\x85":
        assert crm_push.clean(f"a{ch}b") == "a b"
    p2 = crm_push.build_payload(view(address="Namuna\x1ckoʻchasi\x851"))
    assert p2.address == "Namuna koʻchasi 1"


@pytest.mark.parametrize(
    ("band", "low"), [("1_3", 1), ("3_10", 3), ("container", 28), ("truck", 80)]
)
def test_band_routes_by_low_end(band, low):
    quiet = sig(quote="mato kerak", excerpt="Bizga mato kerak", value=None)
    p = crm_push.build_payload(view(stated_m3=None, volume_band=band, signals=(quiet,)))
    assert p.kub is None  # a band is never kub
    assert crm_push.BAND_HAJM[band] in p.note
    text = p.name + "\n" + crm_push.landed_note(p)
    assert crm_push.text_volume(text) == Decimal(low)


def test_a_stated_volume_is_kub_and_routes_by_itself():
    p = crm_push.build_payload(view(stated_m3=Decimal("2.5"), signals=(sig(value=None),)))
    assert p.kub == "2.5"
    assert "HAJM: 2.5 kub" in p.note
    # The quote comes first: «5 kub» in it routes before the HAJM line.
    assert crm_push.text_volume(p.name + "\n" + crm_push.landed_note(p)) == Decimal(5)
    quiet = sig(quote="mato kerak", excerpt="Bizga mato kerak", value=None)
    p2 = crm_push.build_payload(view(stated_m3=Decimal(25), signals=(quiet,)))
    assert p2.kub == "25"
    assert crm_push.text_volume(p2.name + "\n" + crm_push.landed_note(p2)) == Decimal(25)
    # < 1 m³: no kub.
    p3 = crm_push.build_payload(view(stated_m3=Decimal("0.5"), signals=(quiet,)))
    assert p3.kub is None


def test_text_volume_is_parse_measure_s():
    assert crm_push.text_volume("0 kub mato, keyin 5 kub") is None
    assert crm_push.text_volume("100000 kub") is None
    assert crm_push.text_volume("99999,5 m³") == Decimal("99999.5")
    assert crm_push.text_volume("3КУБ") == Decimal(3)


def _level(v: CandidateView) -> int:
    for level in range(len(crm_push.SHRINK_STEPS) + 1):
        if crm_push.fits(crm_push._payload(v, level)):
            return level
    raise AssertionError("nothing fits")


def test_the_note_is_shortened_in_the_drop_order():
    assert crm_push.SHRINK_STEPS[:5] == (
        "ball_detail",
        "masul",
        "opener_short",
        "per_month",
        "address_short",
    )
    opener = " ".join(["soʻz"] * 54)  # 269 units: cut to 220, then to 120
    v = view(
        person=PersonView("Namuna Akmal", "rahbar", None),
        opener=opener,
        address=" ".join(["koʻcha"] * 5),
        signals=(
            sig(url="https://t.me/" + "n" * 150, excerpt=EXCERPT),
            sig(
                "cn_origin",
                quote="Xitoydan har oy keltiramiz",
                group="origin",
                points=20,
                value=None,
                evidence_id=8,
            ),
        ),  # fmt: skip
    )
    assert _level(v) == 3
    p = crm_push.build_payload(v)
    lines = p.note.split("\n")
    assert lines[-1] == "BALL: 74/100"
    assert not any(line.strip().startswith("Masʼul") for line in lines)
    deyish = next(line for line in lines if line.startswith("NIMA DEYISH: "))
    assert push_gate.text_len(deyish.removeprefix("NIMA DEYISH: ")) <= 120
    assert deyish.endswith("…")
    # The address waits for its own step: still whole here …
    assert p.address == " ".join(["koʻcha"] * 5)
    # … and is cut to 120 at step 5 when the steps before it are not enough.
    longer = replace(
        v,
        address=" ".join(["koʻcha"] * 40),
        signals=(sig(url="https://t.me/" + "n" * 50, excerpt=EXCERPT), v.signals[1]),
    )
    assert _level(longer) == 5
    cut = crm_push.build_payload(longer).address
    assert cut is not None and push_gate.text_len(cut) <= 120 and cut.endswith("…")
    assert push_gate.text_len(p.note) <= 700
    assert push_gate.text_len(crm_push.landed_note(p)) <= 1000
    # The URL is never cut.
    assert lines[1] == "https://t.me/" + "n" * 150


def test_an_overfull_candidate_still_fits_on_the_last_resorts():
    v = view(
        person=PersonView("Namuna Akmal", "rahbar", None),
        opener=" ".join(["soʻz"] * 80),
        address=" ".join(["koʻcha"] * 60),
        goods=tuple(f"tovar{n} " + "x" * 40 for n in range(6)),
        signals=(
            sig(url="https://t.me/" + "n" * 180, excerpt=EXCERPT),
            sig(
                "cn_origin",
                quote="Xitoydan har oy keltiramiz",
                group="origin",
                points=20,
                value=None,
                evidence_id=8,
            ),
        ),  # fmt: skip
    )
    assert _level(v) == 11
    p = crm_push.build_payload(v)
    assert p.note == "\n".join(
        [
            "NEGA TOPILDI: Telegram guruh · 25.10.2026 · «5 kub mato kerak»",
            "https://t.me/" + "n" * 180,
            "HAJM: 5 kub",
            "KOMPANIYA: Namuna Tekstil Savdo MChJ",
            "BALL: 74/100",
        ]
    )
    assert p.evidence == "https://t.me/" + "n" * 180
    assert push_gate.text_len(crm_push.landed_note(p)) == 946


def test_a_url_over_200_is_named_by_its_evidence():
    s = sig(url="https://namuna.example.uz/" + "a" * 200, evidence_id=42)
    p = crm_push.build_payload(view(signals=(s,)))
    assert p.note.split("\n")[1] == "evidence #42"
    assert p.evidence.startswith("evidence #42 — ")


def test_text_len_limits_with_an_emoji_at_each_edge():
    emoji = "\U0001f4e6"  # two UTF-16 units
    name = emoji + "N" * 117 + emoji  # 121 units > 120
    with pytest.raises(crm_push.PayloadError):
        crm_push.build_payload(view(display_name=name))
    ok = emoji + "N" * 116 + emoji  # 120 units
    assert crm_push.build_payload(view(display_name=ok)).name == ok
    cut = crm_push.clip_words("ab " + emoji * 3, 6)
    assert push_gate.text_len(cut) <= 6
    assert all(not 0xD800 <= ord(ch) <= 0xDFFF for ch in cut)
    assert cut == "ab…"


def test_external_id_month_tashkent():
    company = uuid.UUID(int=9)
    # 23:30 UTC on the 31st is the 1st in Tashkent.
    late = datetime(2026, 10, 31, 23, 30, tzinfo=UTC)
    assert crm_push.external_id_for(company, late) == f"cs:{company}:202611"
    early = datetime(2026, 10, 31, 18, 59, tzinfo=UTC)
    assert crm_push.external_id_for(company, early) == f"cs:{company}:202610"


def test_canonical_sha_stable():
    p = crm_push.build_payload(view())
    body = p.model_dump(exclude_none=True)
    reordered = dict(reversed(list(body.items())))
    assert push_gate.canonical_sha256(reordered) == crm_push.canonical_sha256(p)
    # A None is dropped: the same body with an absent key hashes the same.
    assert push_gate.canonical_sha256({**body, "absent": None}) == (
        crm_push.canonical_sha256(p)
    )
    assert crm_push.canonical_sha256(p.model_copy(update={"score": "75"})) != (
        crm_push.canonical_sha256(p)
    )


def test_render_note_is_owner_text():
    from tests.test_uz_text import problems

    v = view(
        person=PersonView("Namuna Akmal", "rahbar", None),
        opener="Assalomu alaykum, mato savdongizni koʻrib yozyapman.",
    )
    for level in range(len(crm_push.SHRINK_STEPS) + 1):
        assert problems(crm_push.render_note(v, level=level), rendered=True) == set()


# --- the push half -------------------------------------------------------------------


async def no_sleep(_: float) -> None:
    return None


class ErpClock:
    def __init__(self, at: datetime) -> None:
        self.at = at.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.at

    def set(self, at: datetime) -> None:
        self.at = at.astimezone(UTC)


@pytest.fixture
async def erp_ready(session, monkeypatch) -> AsyncIterator[None]:
    monkeypatch.setattr(settings, "crm_base_url", BASE)
    async with gate_fixtures.erp_ready(session, monkeypatch):
        yield


@pytest.fixture
def told(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    said: list[str] = []

    async def notify(text: str) -> bool:
        said.append(text)
        return True

    costs.set_cap_notifier(notify)
    yield said
    costs.set_cap_notifier(None)


class World:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.clock = ErpClock(NOW + timedelta(minutes=1))
        self.fake = FakeErp(KEY, other_secret=OTHER_KEY, clock=self.clock)
        self.erp = ErpClient(BASE, KEY, transport=self.fake.transport(), sleep=no_sleep)

    def other(self) -> ErpClient:
        return ErpClient(BASE, OTHER_KEY, transport=self.fake.transport(), sleep=no_sleep)

    async def lead(
        self,
        nn: int = 61,
        *,
        score: int = 72,
        state: str = "approved",
        quote: str = "5 kub mato kerak",
        value: Decimal | None = Decimal(5),
        **firm: Any,
    ) -> LeadCandidate:
        owner = await gate_fixtures.firm(self.session, **firm)
        lead = await gate_fixtures.contact(self.session, owner, phone(nn))
        ev = Evidence(
            source_key="telegram_monitor",
            url=f"https://t.me/namuna_optom/{nn}",
            fetched_at=NOW - timedelta(days=2),
            published_at=NOW - timedelta(days=2),
            sha256=f"{nn:064d}",
            bytes=0,
            excerpt=EXCERPT,
            expires_at=NOW + timedelta(days=180),
        )
        self.session.add(ev)
        await self.session.flush()
        self.session.add(scoring_signal(owner.id, ev.id, quote=quote, value=value))
        cand = await gate_fixtures.candidate(
            self.session, owner, state=state, lead=lead, score=score
        )
        await self.session.commit()
        return cand

    async def push(self, cand: LeadCandidate, *, now: datetime = NOW, erp=None, **kw):
        async with SessionLocal() as s:
            return await crm_push.push_one(s, cand.id, now=now, erp=erp or self.erp, **kw)

    async def rows(self) -> list[PushLog]:
        return list(
            (
                await self.session.scalars(
                    sa.select(PushLog)
                    .order_by(PushLog.id)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )

    async def candidate(self, cand: LeadCandidate) -> LeadCandidate:
        found = await self.session.get(LeadCandidate, cand.id, populate_existing=True)
        assert found is not None
        return found

    async def breaker(self) -> tuple[datetime | None, str | None]:
        row = await self.session.get(Source, "erp_sync", populate_existing=True)
        assert row is not None
        return row.breaker_until, row.breaker_reason

    def posts(self) -> int:
        return len(self.fake.posted)

    def lead_posts(self) -> list[httpx.Request]:
        return [r for r in self.fake.requests if r.method == "POST"]


def scoring_signal(
    company_id: uuid.UUID, evidence_id: int, *, quote: str, value: Decimal | None
):
    from clientscrapper.db.models import Signal

    return Signal(
        company_id=company_id,
        evidence_id=evidence_id,
        key=scoring.STATED_3PLUS if value is not None else "vol_wholesale_bulky",
        value=value,
        quote=quote,
        observed_at=NOW - timedelta(days=2),
        source_key="telegram_monitor",
    )


@pytest.fixture
async def world(session, erp_ready) -> World:
    return World(session)


async def test_push_200_marks_pushed(world):
    cand = await world.lead()
    result = await world.push(cand)
    assert (result.status, result.http_status, result.attempt) == ("sent", 200, 1)
    [row] = await world.rows()
    assert (row.status, row.http_status, row.error, row.attempt) == ("sent", 200, None, 1)
    assert row.endpoint == "/api/leads/in/agent" and row.source_key_sent == "agent"
    assert row.finished_at == NOW
    c = await world.candidate(cand)
    assert (c.state, c.external_id, c.pushed_at) == ("pushed", row.external_id, NOW)
    # The body as sent: the builder's, and its sha is the row's.
    [(source, headers, body)] = world.fake.posted
    assert source == "agent"
    assert body == row.payload
    assert row.payload_sha256 == push_gate.canonical_sha256(body)
    assert headers["x-gsr-key"] == KEY and headers["x-erp-key"] == KEY
    # The ERP made one created lead with the landed note.
    [intake] = world.fake.intakes.values()
    assert intake.outcome == "created"
    assert world.fake.leads[intake.lead_id].notes == [
        "📣 agent\n" + fake_erp.landed_note(body)
    ]


async def test_push_test_tool_payload_has_every_optional_key(world, monkeypatch):
    monkeypatch.setattr(settings, "test_phone", "+998 90 000 00 99")
    report = await push_test.run(now=NOW, erp=world.erp)
    assert report.status == "sent", report
    [(_, _, body)] = world.fake.posted
    optional = [k for k in PushPayload.model_fields if k not in crm_push.IDENTITY_KEYS]
    assert [k for k in optional if k not in body] == []
    assert body["kub"] == "1"
    assert body["stir"] == "300000099"
    assert body["website"] == "test.example.uz"
    assert body["name"] == "TEST agent — oʻchiring"
    assert report.external_id == f"cs:{report.company_id}:202610"
    assert report.lines() == [
        "push-test: sent · HTTP 200",
        f"external_id: {report.external_id}",
        f"company: {report.company_id}",
    ]


# --- helpers for the answers ---------------------------------------------------------


def eid_of(cand: LeadCandidate, at: datetime = NOW) -> str:
    return push_gate.external_id_for(cand.company_id, at)


def answering(status: int, *, body: bytes = b"", raises: type[Exception] | None = None):
    """An ERP stand-in for answers the fake has no mode for (a proxy's 400,
    a refused connection); counts the requests it saw."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if raises is not None:
            raise raises("refused", request=request)
        return httpx.Response(status, content=body)

    erp = ErpClient(BASE, KEY, transport=httpx.MockTransport(handler), sleep=no_sleep)
    return erp, seen


async def problem_keys(session: AsyncSession, now: datetime) -> set[str]:
    status = await health.gather(session, now=now, disk=(10**12, 10**12))
    return {p.key for p in health.problems(status)}


async def owner_note(session: AsyncSession, cand: LeadCandidate) -> str | None:
    note = await session.scalar(
        sa.select(LeadReview.note)
        .where(LeadReview.candidate_id == cand.id, LeadReview.kind == "owner")
        .order_by(LeadReview.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return note


async def walk(world: World, kind: str, now: datetime) -> erp_feeds.OutcomesReport:
    async with SessionLocal() as s:
        return await erp_feeds.sync_outcomes(s, world.erp, walk=kind, now=now)  # type: ignore[arg-type]


async def outcome(session: AsyncSession, eid: str) -> Outcome:
    row = await session.get(Outcome, eid, populate_existing=True)
    assert row is not None
    return row


# --- the answers ----------------------------------------------------------------------


async def test_push_404_trips_breaker_and_alerts_once(world, told):
    cand = await world.lead()
    world.fake.modes[eid_of(cand)] = "404"
    result = await world.push(cand)
    assert (result.status, result.error, result.http_status) == (
        "failed",
        "http_404",
        404,
    )
    [row] = await world.rows()
    assert (row.status, row.error, row.attempt) == ("failed", "http_404", 1)
    assert len(world.lead_posts()) == 1
    assert (await world.candidate(cand)).state == "approved"
    assert await world.breaker() == (NOW + timedelta(hours=6), "push_404")
    assert told == [health.erp_breaker_text("push_404")]
    assert "push_404" in await problem_keys(world.session, NOW + timedelta(minutes=1))
    # Next ticks: refused 'breaker' with no POST until the key is mended.
    later = await world.push(cand, now=NOW + timedelta(minutes=10))
    assert later.codes == ["breaker"]
    assert len(world.lead_posts()) == 1
    # A second 404 inside the 6 h (the breaker cleared by hand, /yoq crm):
    # the breaker trips again, the owner is not told twice.
    await world.session.execute(
        sa.update(Source)
        .where(Source.key == "erp_sync")
        .values(breaker_until=None, breaker_reason=None)
    )
    await world.session.commit()
    again = await world.push(cand, now=NOW + timedelta(minutes=20))
    assert (again.error, again.attempt) == ("http_404", 2)
    assert await world.breaker() == (NOW + timedelta(hours=6, minutes=20), "push_404")
    assert len(told) == 1
    kinds = (await world.session.scalars(sa.select(ReminderLog.kind))).all()
    assert kinds == ["alert:push_404"]


async def test_a_feed_200_does_not_end_a_push_s_404(world):
    cand = await world.lead()
    world.fake.modes[eid_of(cand)] = "404"
    await world.push(cand)
    await walk(world, "recent", NOW + timedelta(minutes=5))
    assert await world.breaker() == (NOW + timedelta(hours=6), "push_404")


async def test_push_3xx_is_not_followed_and_trips_breaker(world, told):
    cand = await world.lead()
    world.fake.modes[eid_of(cand)] = "3xx:308"
    result = await world.push(cand)
    assert (result.status, result.error, result.http_status) == (
        "failed",
        "http_3xx",
        308,
    )
    # Exactly one POST: the redirect was not followed (the key not re-sent).
    assert len(world.fake.requests) == 1
    assert await world.breaker() == (NOW + timedelta(hours=6), "erp_redirect:308")
    assert told == [health.erp_breaker_text("erp_redirect", "308")]
    assert "erp_redirect" in await problem_keys(world.session, NOW + timedelta(minutes=1))
    assert (await world.candidate(cand)).state == "approved"


async def test_push_503_three_tries_then_failed_then_review_after_attempt_3(world, told):
    cand = await world.lead()
    world.fake.modes[eid_of(cand)] = "503"
    first = await world.push(cand)
    assert (first.status, first.error, first.http_status, first.attempt) == (
        "failed",
        "http_5xx",
        503,
        1,
    )
    assert len(world.lead_posts()) == 3
    # Attempt 2 only after 5 minutes: before it, a silent refusal and no POST.
    early = await world.push(cand, now=NOW + timedelta(minutes=4))
    assert early.codes == ["attempt_wait"]
    assert len(world.lead_posts()) == 3
    second = await world.push(cand, now=NOW + timedelta(minutes=5))
    assert (second.attempt, second.error) == (2, "http_5xx")
    # Attempt 3 only after 30 more.
    wait = await world.push(cand, now=NOW + timedelta(minutes=34))
    assert wait.codes == ["attempt_wait"]
    third = await world.push(cand, now=NOW + timedelta(minutes=35))
    assert (third.attempt, third.error) == (3, "http_5xx")
    # 9 POSTs, nothing recorded by the ERP, the candidate back to review.
    assert len(world.lead_posts()) == 9
    assert world.fake.intakes == {}
    rows = await world.rows()
    assert [(r.attempt, r.status, r.error, r.http_status) for r in rows] == [
        (1, "failed", "http_5xx", 503),
        (2, "failed", "http_5xx", 503),
        (3, "failed", "http_5xx", 503),
    ]
    assert len({r.payload_sha256 for r in rows}) == 1
    assert (await world.candidate(cand)).state == "review"
    assert await owner_note(world.session, cand) == push_gate.REFUSAL_UZ["attempts_spent"]
    assert push_gate.REFUSAL_UZ["attempts_spent"].startswith("CRM javob bermadi")
    # A 503 never trips the breaker and tells nobody.
    assert await world.breaker() == (None, None)
    assert told == []


async def test_three_failing_candidates_raise_push_failing(world):
    erp, _ = answering(503, body=b'{"error":"unavailable"}')
    cands = [await world.lead(nn) for nn in (61, 62, 63)]
    for cand in cands[:2]:
        await world.push(cand, erp=erp)
    assert "push_failing" not in await problem_keys(world.session, NOW)
    await world.push(cands[2], erp=erp)
    assert "push_failing" in await problem_keys(world.session, NOW)
    assert "push_404" not in await problem_keys(world.session, NOW)


@pytest.mark.parametrize(
    ("mode", "status"), [("nonjson5xx", 500), ("nonjson5xx:502", 502)]
)
async def test_push_nonjson5xx_is_retried_like_503(world, mode, status):
    cand = await world.lead()
    world.fake.modes[eid_of(cand)] = mode
    result = await world.push(cand)
    assert (result.status, result.error, result.http_status) == (
        "failed",
        "http_5xx",
        status,
    )
    assert len(world.lead_posts()) == 3
    assert world.fake.intakes == {}


async def test_a_503_then_a_200_inside_the_attempt_is_sent(world):
    cand = await world.lead()
    world.fake.modes[eid_of(cand)] = ["503"]
    result = await world.push(cand)
    assert (result.status, result.attempt, result.http_status) == ("sent", 1, 200)
    assert len(world.lead_posts()) == 2
    assert len(world.fake.intakes) == 1


async def test_push_503_committed_retry_is_replay(world):
    cand = await world.lead()
    eid = eid_of(cand)
    world.fake.modes[eid] = ["503_committed"]
    result = await world.push(cand)
    assert (result.status, result.attempt) == ("sent", 1)
    assert len(world.lead_posts()) == 2
    [intake] = world.fake.intakes.values()
    assert intake.outcome == "created"
    await walk(world, "recent", NOW + timedelta(minutes=5))
    got = await outcome(world.session, eid)
    assert (got.landing, got.contacted_at, got.label) == (
        "created",
        None,
        "open_untouched",
    )


async def test_push_read_timeout_is_unknown_without_resend(world):
    cand = await world.lead()
    eid = eid_of(cand)
    world.fake.modes[eid] = "slow"
    erp = ErpClient(
        BASE, KEY, transport=world.fake.timeout_transport(0.05), sleep=no_sleep
    )
    result = await world.push(cand, erp=erp)
    assert (result.status, result.error, result.http_status) == (
        "unknown",
        "timeout",
        None,
    )
    # Exactly one POST: no resend inside the attempt.
    assert len(world.lead_posts()) == 1
    [row] = await world.rows()
    assert (row.status, row.error) == ("unknown", "timeout")
    assert (await world.candidate(cand)).state == "approved"
    # The first request was still running; now it lands.
    world.fake.release(eid)
    await world.fake.drain()
    assert len(world.fake.intakes) == 1 and len(world.fake.leads) == 1
    # Blocked meanwhile: the job does not try it again.
    async with SessionLocal() as s:
        assert await crm_push.due_candidates(s, limit=10) == []
    report = await crm_push.reconcile_job(
        SessionLocal, world.erp, now=NOW + timedelta(minutes=10)
    )
    assert report.walk is not None and report.walk["promoted"] == 1
    [row] = await world.rows()
    assert (row.status, row.http_status) == ("sent", None)
    c = await world.candidate(cand)
    assert (c.state, c.external_id, c.pushed_at) == ("pushed", eid, NOW)


async def test_push_orphan_retry_joins(world):
    cand = await world.lead()
    eid = eid_of(cand)
    world.fake.modes[eid] = "orphan"
    result = await world.push(cand)
    assert (result.status, result.attempt) == ("sent", 1)
    assert len(world.lead_posts()) == 2
    [intake] = world.fake.intakes.values()
    assert intake.outcome == "joined" and len(world.fake.leads) == 1
    await walk(world, "recent", NOW + timedelta(minutes=5))
    got = await outcome(world.session, eid)
    assert (got.landing, got.label) == ("joined", "landed_joined")
    # No side effect: no local suppression, the company not a client.
    assert (await world.session.scalar(sa.select(sa.func.count()).select_from(
        sa.text("suppression")))) == 0  # fmt: skip
    company = await world.session.get(Company, cand.company_id, populate_existing=True)
    assert company is not None and company.state == "active"


async def test_an_orphan_moved_to_lost_makes_the_next_attempt_a_second_lead(world):
    cand = await world.lead()
    eid = eid_of(cand)
    world.fake.modes[eid] = ["orphan", "503", "503"]
    first = await world.push(cand)
    assert (first.status, first.error) == ("failed", "http_5xx")
    [orphan] = world.fake.leads.values()
    world.fake.move(orphan.id, "lost", reason="Boshqa")
    second = await world.push(cand, now=NOW + timedelta(minutes=5))
    assert (second.status, second.attempt) == ("sent", 2)
    assert len(world.fake.leads) == 2
    [intake] = world.fake.intakes.values()
    assert intake.outcome == "created"
    await walk(world, "recent", NOW + timedelta(minutes=10))
    assert (await outcome(world.session, eid)).label == "open_untouched"


async def test_an_orphan_with_the_phone_s_cap_full_comes_back_capped(world):
    cand = await world.lead()
    eid = eid_of(cand)
    world.fake.modes[eid] = ["orphan", "503", "503"]
    await world.push(cand)
    # Three other agent arrivals on that phone fill its 24-hour cap.
    async with httpx.AsyncClient(transport=world.fake.transport(), base_url=BASE) as c:
        for n in range(3):
            other = f"cs:{uuid.uuid4()}:202610"
            answer = await c.post(
                "/api/leads" + "/in/agent",
                json={"external_id": other, "name": f"Namuna {n}", "phone": phone(61)},
                headers={"X-GSR-Key": KEY},
            )
            assert answer.status_code == 200
    second = await world.push(cand, now=NOW + timedelta(minutes=5))
    assert second.status == "sent"
    assert world.fake.intakes[eid].reason == "capped"
    await walk(world, "recent", NOW + timedelta(minutes=10))
    assert (await outcome(world.session, eid)).label == "dropped_capped"


async def test_push_2xx_badbody_is_unknown(world):
    cand = await world.lead()
    eid = eid_of(cand)
    world.fake.modes[eid] = "badjson"
    result = await world.push(cand)
    assert (result.status, result.http_status, result.error) == (
        "unknown",
        200,
        "http_2xx_badbody",
    )
    assert len(world.lead_posts()) == 1  # never retried
    [row] = await world.rows()
    assert (row.status, row.http_status) == ("unknown", 200)
    assert (await world.candidate(cand)).state == "approved"
    report = await crm_push.reconcile_job(
        SessionLocal, world.erp, now=NOW + timedelta(minutes=10)
    )
    assert report.walk is not None and report.walk["promoted"] == 1
    [row] = await world.rows()
    assert (row.status, row.http_status) == ("sent", 200)
    assert (await world.candidate(cand)).state == "pushed"


async def test_other_4xx_goes_back_to_review_and_tells_the_owner(world, told):
    cand = await world.lead()
    erp, seen = answering(400, body=b"bad request")
    result = await world.push(cand, erp=erp)
    assert (result.status, result.error, result.http_status) == (
        "failed",
        "http_4xx",
        400,
    )
    assert len(seen) == 1
    assert (await world.candidate(cand)).state == "review"
    assert await owner_note(world.session, cand) == "ERP rad etdi (HTTP 400)"
    assert told == [crm_push.rejected_text(400)]
    assert await world.breaker() == (None, None)


async def test_a_refused_connection_is_three_tries_then_conn(world):
    cand = await world.lead()
    erp, seen = answering(0, raises=httpx.ConnectError)
    result = await world.push(cand, erp=erp)
    assert (result.status, result.error, result.http_status) == ("failed", "conn", None)
    assert len(seen) == 3
    erp, seen = answering(0, raises=httpx.ConnectTimeout)
    other = await world.lead(62)
    result = await world.push(other, erp=erp)
    assert (result.status, result.error) == ("failed", "timeout")
    assert len(seen) == 3


async def fresh_at(world: World, at: datetime) -> None:
    """The suppression list fresh at ``at`` — a test that moves the clock
    days ahead tests the window or ``phone_recent``, not freshness."""
    await gate_fixtures.set_config(
        world.session, "suppression", gate_fixtures.fresh_config(at)
    )
    await world.session.commit()


async def test_push_refused_outside_window_and_over_cap(world, monkeypatch):
    cand = await world.lead()
    sunday = NOW + timedelta(days=5)
    await fresh_at(world, sunday)
    result = await world.push(cand, now=sunday)
    assert result.codes == ["window"]
    assert result.refusals[0].text == (
        "Push vaqti emas (Du–Sh 09:30–17:30) (02.11.2026 09:30 gacha)"
    )
    await fresh_at(world, NOW)
    assert world.fake.posted == [] and await world.rows() == []
    assert await owner_note(world.session, cand) is None  # silent
    monkeypatch.setattr(settings, "push_daily_cap", 1)
    pushed = await world.lead(62)
    assert (await world.push(pushed)).status == "sent"
    capped = await world.push(cand, now=NOW + timedelta(minutes=1))
    assert capped.codes == ["cap"]
    assert len(world.fake.posted) == 1
    assert (await world.candidate(cand)).state == "approved"


async def test_push_refused_when_suppressed_or_stale(world):
    cand = await world.lead()
    hit = await gate_fixtures.row(world.session, "dnc", "phone_tail", tail(61))
    await world.session.commit()
    result = await world.push(cand)
    assert result.codes == ["suppressed"]
    assert world.fake.posted == []
    c = await world.candidate(cand)
    assert (c.state, c.suppressed_by) == ("suppressed", hit.id)
    # A list older than 26 h: refused 'stale', the candidate stays approved.
    other = await world.lead(62)
    await gate_fixtures.set_config(
        world.session, "suppression", gate_fixtures.fresh_config(NOW - DAY - DAY)
    )
    await world.session.commit()
    stale = await world.push(other)
    assert stale.codes == ["stale"]
    assert world.fake.posted == []
    assert (await world.candidate(other)).state == "approved"
    assert await owner_note(world.session, other) is None  # no card: no row made


async def fallback(world: World, monkeypatch: pytest.MonkeyPatch) -> ErpClient:
    """The «Boshqa» door (05 §B.3): the other source, its key, a fresh import."""
    monkeypatch.setattr(settings, "crm_push_source", "other")
    monkeypatch.setattr(settings, "crm_agent_key", OTHER_KEY)
    await gate_fixtures.set_config(
        world.session,
        "client_import",
        {
            "rows": 1,
            "tails": 1,
            "sha256": "0" * 64,
            "at": (NOW - timedelta(hours=1)).isoformat(),
        },
    )
    await world.session.commit()
    return world.other()


async def switch_over(
    world: World, monkeypatch: pytest.MonkeyPatch, at: datetime
) -> None:
    """Back on the agent door at ``at``: its key, a fresh list, that night's
    cursor and recent walks completed (02 §2.10 rule 5's precondition)."""
    monkeypatch.setattr(settings, "crm_push_source", "agent")
    monkeypatch.setattr(settings, "crm_agent_key", KEY)
    await gate_fixtures.set_config(
        world.session, "suppression", gate_fixtures.fresh_config(at)
    )
    await world.session.commit()
    world.clock.set(at)
    await walk(world, "cursor", at)
    await walk(world, "recent", at)


async def test_push_fallback_other_source_sends_both_headers(world, monkeypatch):
    erp = await fallback(world, monkeypatch)
    cand = await world.lead()
    result = await world.push(cand, erp=erp)
    assert result.status == "sent"
    [row] = await world.rows()
    assert (row.endpoint, row.source_key_sent) == ("/api/leads/in/other", "other")
    [(source, headers, body)] = world.fake.posted
    assert source == "other"
    assert headers["x-gsr-key"] == OTHER_KEY and headers["x-erp-key"] == OTHER_KEY
    assert body["utm_source"] == "clientscrapper"
    assert list(body).index("utm_source") == list(body).index("note") + 1
    # The fake, as the ERP, refuses X-ERP-Key alone.
    async with httpx.AsyncClient(transport=world.fake.transport(), base_url=BASE) as c:
        answer = await c.post(
            "/api/leads" + "/in/other", json=body, headers={"X-ERP-Key": OTHER_KEY}
        )
    assert answer.status_code == 404
    # The fallback makes no feed call (the reconcile walks nothing).
    report = await crm_push.reconcile_job(
        SessionLocal, erp, now=NOW + timedelta(minutes=10)
    )
    assert report.skipped == "feeds_not_configured"
    assert world.fake.gets == []


async def test_reconcile_unconfirmed_repushes_once(world, monkeypatch):
    erp = await fallback(world, monkeypatch)
    cand = await world.lead()
    eid = eid_of(cand)
    world.fake.modes[eid] = "throw200"
    assert (await world.push(cand, erp=erp)).status == "sent"
    assert world.fake.intakes == {}  # the fallback door's 200 with nothing recorded
    # The switch, 49 h later (a Thursday noon, inside the window).
    at = NOW + timedelta(hours=49)
    await switch_over(world, monkeypatch, at)
    report = await crm_push.reconcile_job(SessionLocal, world.erp, now=at)
    assert report.reconcile is not None and report.reconcile["unconfirmed"] == 1
    assert [r["status"] for r in report.repushed] == ["sent"]
    rows = await world.rows()
    assert [(r.attempt, r.status, r.superseded_at, r.source_key_sent) for r in rows] == [
        (1, "sent", at, "other"),
        (2, "sent", None, "agent"),
    ]
    assert rows[0].payload == rows[1].payload
    assert rows[0].payload_sha256 == rows[1].payload_sha256
    assert rows[1].endpoint == "/api/leads/in/agent"
    assert world.fake.posted[-1][0] == "agent"
    assert world.fake.posted[-1][2] == rows[0].payload
    assert world.fake.intakes[eid].outcome == "created"
    assert (await world.candidate(cand)).state == "pushed"
    # A second 48 h without an outcome (the ERP lost it again): no second
    # re-push, the pushed candidate stays, push_unconfirmed stays.
    world.fake.intakes.clear()
    later = at + timedelta(hours=49)
    await switch_over(world, monkeypatch, later)
    posts = len(world.fake.posted)
    report = await crm_push.reconcile_job(SessionLocal, world.erp, now=later)
    assert report.repushed == [] and report.reviewed == 0
    assert len(world.fake.posted) == posts
    assert (await world.candidate(cand)).state == "pushed"
    assert "push_unconfirmed" in await problem_keys(world.session, later)


async def test_an_unknown_row_s_approved_candidate_goes_to_review(world, monkeypatch):
    cand = await world.lead()
    eid = eid_of(cand)
    world.fake.modes[eid] = "badjson"
    assert (await world.push(cand)).status == "unknown"
    world.fake.intakes.clear()  # the ERP shows nothing of it
    at = NOW + timedelta(hours=49)
    await switch_over(world, monkeypatch, at)
    world.fake.modes[eid] = "badjson"
    report = await crm_push.reconcile_job(SessionLocal, world.erp, now=at)
    assert [r["status"] for r in report.repushed] == ["unknown"]
    world.fake.intakes.clear()
    later = at + timedelta(hours=49)
    await switch_over(world, monkeypatch, later)
    report = await crm_push.reconcile_job(SessionLocal, world.erp, now=later)
    assert (report.repushed, report.reviewed) == ([], 1)
    assert (await world.candidate(cand)).state == "review"
    assert await owner_note(world.session, cand) == "CRM tasdiqlamadi"


async def test_an_old_spent_or_payloadless_row_is_only_flagged(world, monkeypatch):
    at = NOW + timedelta(hours=49)
    spent = await world.lead(61, state="pushed")
    gone = await world.lead(62, state="pushed")
    for cand, attempt, kept in ((spent, 3, True), (gone, 1, False)):
        row = await gate_fixtures.push(
            world.session,
            cand,
            sent_at=NOW - timedelta(minutes=1),
            phone_tail=tail(61 if cand is spent else 62),
            external_id=cand.external_id,
            attempt=attempt,
        )
        row.payload = {"external_id": cand.external_id} if kept else None
    await world.session.commit()
    await switch_over(world, monkeypatch, at)
    report = await crm_push.reconcile_job(SessionLocal, world.erp, now=at)
    assert report.reconcile is not None and report.reconcile["unconfirmed"] == 2
    assert report.repushed == [] and world.fake.posted == []
    assert "push_unconfirmed" in await problem_keys(world.session, at)


async def test_reconcile_repush_joins_fallback_orphan(world, monkeypatch):
    erp = await fallback(world, monkeypatch)
    cand = await world.lead()
    eid = eid_of(cand)
    world.fake.modes[eid] = "throw200_orphan"
    assert (await world.push(cand, erp=erp)).status == "sent"
    assert world.fake.intakes == {} and len(world.fake.leads) == 1
    at = NOW + timedelta(hours=49)
    await switch_over(world, monkeypatch, at)
    report = await crm_push.reconcile_job(SessionLocal, world.erp, now=at)
    assert [r["status"] for r in report.repushed] == ["sent"]
    assert world.fake.intakes[eid].outcome == "joined"
    assert len(world.fake.leads) == 1
    await walk(world, "recent", at + timedelta(minutes=1))
    got = await outcome(world.session, eid)
    assert (got.landing, got.label) == ("joined", "landed_joined")
    company = await world.session.get(Company, cand.company_id, populate_existing=True)
    assert company is not None and company.state == "active"


async def test_a_crashed_claim_becomes_unknown_and_is_promoted(world):
    cand = await world.lead()
    eid = eid_of(cand)
    await gate_fixtures.push(
        world.session,
        cand,
        sent_at=NOW - timedelta(minutes=11),
        phone_tail=tail(61),
        external_id=eid,
        status="sending",
        http_status=None,
    )
    fresh = await world.lead(62)
    await gate_fixtures.push(
        world.session,
        fresh,
        sent_at=NOW - timedelta(minutes=9),
        phone_tail=tail(62),
        external_id=eid_of(fresh),
        status="sending",
        http_status=None,
    )
    await world.session.commit()
    # The ERP had it after all.
    async with httpx.AsyncClient(transport=world.fake.transport(), base_url=BASE) as c:
        await c.post(
            "/api/leads" + "/in/agent",
            json={"external_id": eid, "name": "Namuna", "phone": phone(61),
                  "utm_source": "clientscrapper"},
            headers={"X-GSR-Key": KEY},
        )  # fmt: skip
    report = await crm_push.reconcile_job(SessionLocal, world.erp, now=NOW)
    assert report.settled == 1
    assert report.walk is not None and report.walk["promoted"] == 1
    rows = {r.external_id: r for r in await world.rows()}
    assert rows[eid].status == "sent"
    assert rows[eid_of(fresh)].status == "sending"  # 9 minutes: still in flight
    assert (await world.candidate(cand)).state == "pushed"


# --- the job -------------------------------------------------------------------------


async def test_crm_push_job_pushes_the_approved_best_first(world):
    low = await world.lead(61, score=65)
    high = await world.lead(62, score=90)
    await world.lead(63, state="review")
    report = await crm_push.crm_push_job(SessionLocal, world.erp, now=NOW)
    assert (report.candidates, report.sent) == (2, 2)
    assert [b["phone"] for _, _, b in world.fake.posted] == [phone(62), phone(61)]
    for cand in (low, high):
        assert (await world.candidate(cand)).state == "pushed"
    # Nothing left: the next tick pushes nothing.
    again = await crm_push.crm_push_job(SessionLocal, world.erp, now=NOW)
    assert (again.candidates, again.sent) == (0, 0)


async def test_crm_push_job_stops_on_the_cap_window_and_breaker(world, monkeypatch):
    await world.lead(61)
    await world.lead(62)
    closed = await crm_push.crm_push_job(
        SessionLocal, world.erp, now=NOW + timedelta(days=5)
    )
    assert closed.skipped == "window" and world.fake.posted == []
    monkeypatch.setattr(settings, "push_daily_cap", 1)
    report = await crm_push.crm_push_job(SessionLocal, world.erp, now=NOW)
    assert (report.sent, report.refused, report.stopped) == (1, 1, "cap")
    monkeypatch.setattr(settings, "push_daily_cap", 25)
    third = await world.lead(63)
    world.fake.modes[eid_of(third)] = "404"
    report = await crm_push.crm_push_job(
        SessionLocal, world.erp, now=NOW + timedelta(minutes=1)
    )
    assert report.stopped == "push_404"
    monkeypatch.setattr(settings, "crm_push_enabled", False)
    off = await crm_push.crm_push_job(SessionLocal, world.erp, now=NOW)
    assert off.skipped == "disabled"


async def test_the_job_never_pushes_a_push_test_company(world, monkeypatch):
    monkeypatch.setattr(settings, "test_phone", "+998900000099")
    owner = await gate_fixtures.firm(world.session)
    lead = await gate_fixtures.contact(world.session, owner, phone(64))
    await gate_fixtures.candidate(
        world.session,
        owner,
        lead=lead,
        score=0,
        score_breakdown={"gates": {"test": True}},
    )
    await world.session.commit()
    async with SessionLocal() as s:
        assert await crm_push.due_candidates(s, limit=10) == []


# --- /push tekshir (06 §1.2) ---------------------------------------------------------


async def test_push_tekshir_supersedes_and_re_pushes_at_once(world):
    cand = await world.lead()
    eid = eid_of(cand)
    world.fake.modes[eid] = "badjson"
    await world.push(cand)
    world.fake.intakes.clear()
    world.fake.modes[eid] = "ok"
    async with SessionLocal() as s:
        check = await crm_push.push_tekshir_view(s, eid)
    assert (
        check.status,
        check.attempt,
        check.reported,
        check.repushed,
        check.refusal,
    ) == (
        "unknown",
        1,
        False,
        False,
        None,
    )
    text = crm_push.render_push_check(check)
    assert text.endswith("Qayta yuboraymi?")
    from tests.test_uz_text import problems

    assert problems(text, rendered=True) == set()
    async with SessionLocal() as s:
        result = await crm_push.push_tekshir_confirm(
            s, check.push_log_id, now=NOW + timedelta(hours=1), erp=world.erp
        )
    assert (result.status, result.attempt) == ("sent", 2)
    rows = await world.rows()
    assert [(r.attempt, r.status, r.superseded_at) for r in rows] == [
        (1, "unknown", NOW + timedelta(hours=1)),  # never 'failed' (ck_push_log_sent)
        (2, "sent", None),
    ]
    assert (await world.candidate(cand)).state == "pushed"


async def test_push_tekshir_refuses_a_reported_id_and_promotes_it(world):
    cand = await world.lead()
    eid = eid_of(cand)
    world.fake.modes[eid] = "badjson"
    await world.push(cand)
    await walk(world, "recent", NOW + timedelta(minutes=1))
    # The walk promoted it already; mark it unknown again to see the confirm do it.
    await world.session.execute(
        sa.update(PushLog).where(PushLog.external_id == eid).values(status="unknown")
    )
    await world.session.commit()
    async with SessionLocal() as s:
        check = await crm_push.push_tekshir_view(s, eid)
        assert (check.reported, check.refusal) == (True, "reported")
        result = await crm_push.push_tekshir_confirm(
            s, check.push_log_id, now=NOW + timedelta(hours=1), erp=world.erp
        )
    assert result.codes == ["reported"]
    [row] = await world.rows()
    assert (row.status, row.superseded_at) == ("sent", None)
    assert len(world.lead_posts()) == 1


async def test_push_tekshir_refuses_attempt_3_and_no_payload(world):
    cand = await world.lead(state="pushed")
    eid = cand.external_id
    assert eid is not None
    row = await gate_fixtures.push(
        world.session, cand, sent_at=NOW - DAY, phone_tail=tail(61), external_id=eid,
        attempt=3,
    )  # fmt: skip
    await world.session.commit()
    async with SessionLocal() as s:
        assert (await crm_push.push_tekshir_view(s, eid)).refusal == "attempts_spent"
        result = await crm_push.push_tekshir_confirm(s, row.id, now=NOW, erp=world.erp)
    assert result.codes == ["attempts_spent"]
    other = await world.lead(62, state="pushed")
    gone = await gate_fixtures.push(
        world.session, other, sent_at=NOW - DAY, phone_tail=tail(62),
        external_id=other.external_id,
    )  # fmt: skip
    await world.session.commit()
    async with SessionLocal() as s:
        result = await crm_push.push_tekshir_confirm(s, gone.id, now=NOW, erp=world.erp)
    assert result.codes == ["no_payload"]
    assert world.fake.posted == []
    async with SessionLocal() as s:
        missing = await crm_push.push_tekshir_view(s, f"cs:{uuid.uuid4()}:202610")
    assert missing.refusal == "not_found"
    assert (
        crm_push.render_push_check(missing) == crm_push.PUSH_CHECK_REFUSED_UZ["not_found"]
    )


# --- make push-test ------------------------------------------------------------------


async def test_push_test_tool_new_external_id_each_run(world, monkeypatch):
    monkeypatch.setattr(settings, "test_phone", "+998900000099")
    first = await push_test.run(now=NOW, erp=world.erp)
    assert first.status == "sent", first
    # A day and an hour later (outside phone_recent's 24 h): a new company,
    # a new id, a second push_log row.
    later = NOW + timedelta(hours=25)
    await fresh_at(world, later)
    second = await push_test.run(now=later, erp=world.erp)
    assert second.status == "sent", second
    assert first.external_id != second.external_id
    assert first.company_id != second.company_id
    assert first.external_id == f"cs:{first.company_id}:202610"
    rows = await world.rows()
    assert sorted(r.external_id for r in rows) == sorted(
        [first.external_id, second.external_id]
    )
    # The earlier TEST company gave up the unique STIR and domain.
    older = await world.session.get(Company, first.company_id, populate_existing=True)
    newer = await world.session.get(Company, second.company_id, populate_existing=True)
    assert older is not None and (older.stir, older.website_domain) == (None, None)
    assert newer is not None and (newer.stir, newer.website_domain) == (
        "300000099",
        "test.example.uz",
    )
    assert first.exit_code() == 0


async def test_push_test_refuses_a_suppressed_phone_before_minting(world, monkeypatch):
    monkeypatch.setattr(settings, "test_phone", "+998900000099")
    await gate_fixtures.row(world.session, "lost", "phone_tail", "900000099")
    await world.session.commit()
    report = await push_test.run(now=NOW, erp=world.erp)
    assert report.status == "refused"
    assert report.exit_code() == 2
    assert world.fake.posted == []
    assert await world.session.scalar(sa.select(sa.func.count(Company.id))) == 0
    monkeypatch.setattr(settings, "test_phone", "")
    assert (await push_test.run(now=NOW, erp=world.erp)).reason is not None


async def test_a_refused_push_test_rolls_its_company_back(world, monkeypatch):
    monkeypatch.setattr(settings, "test_phone", "+998900000099")
    sunday = NOW + timedelta(days=5)
    await fresh_at(world, sunday)
    report = await push_test.run(now=sunday, erp=world.erp)
    assert (report.status, report.refusals) == (
        "refused",
        ["Push vaqti emas (Du–Sh 09:30–17:30) (02.11.2026 09:30 gacha)"],
    )
    assert report.exit_code() == 2
    assert world.fake.posted == []
    assert await world.session.scalar(sa.select(sa.func.count(Company.id))) == 0
    assert await world.session.scalar(sa.select(sa.func.count(LeadCandidate.id))) == 0


async def test_push_test_main_prints_why_without_an_erp(monkeypatch, capsys):
    def unconfigured() -> ErpClient:
        raise ValueError("CRM_BASE_URL yoʻq")

    monkeypatch.setattr(erp_feeds, "client_from_settings", unconfigured)
    assert await push_test._main() == push_test.EXIT_REFUSED
    assert capsys.readouterr().out == "push-test: ERP sozlanmagan — CRM_BASE_URL yoʻq\n"


def test_push_test_report_lines_and_exit_codes():
    refused = push_test.PushTestReport("refused", refusals=["Push vaqti emas"])
    assert (refused.lines(), refused.exit_code()) == (
        ["push-test: rad etildi — Push vaqti emas"],
        2,
    )
    company = uuid.UUID(int=5)
    unknown = push_test.PushTestReport(
        "unknown",
        company_id=company,
        external_id=f"cs:{company}:202610",
        error="timeout",
    )
    assert (unknown.lines(), unknown.exit_code()) == (
        [
            "push-test: unknown · HTTP — · timeout",
            f"external_id: cs:{company}:202610",
            f"company: {company}",
        ],
        1,
    )
