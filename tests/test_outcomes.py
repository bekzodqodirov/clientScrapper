"""The outcomes walks against the fake ERP and Postgres (07 §4.1 T-10; 05 §B.2,
02 §2.10 rule 5, 02 §2.11).

Every test pushes through the fake ERP's real door (the fake lands, joins or
drops the arrival as ``agent-feed`` does) with the agent's own ``push_log``
row beside it — what ``crm_push`` (T-28) will write — then moves the lead the
way the ERP's screens would, runs a walk and reads back ``outcomes``, the
local ``suppression`` rows, the company, the candidate, the push, the raised
problems and the requests the fake saw. ``sources`` is migration-seeded
configuration: ``erp_sync`` and every row's precision numbers are
snapshotted before each test and put back after.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors import erp_sync
from clientscrapper.config import settings
from clientscrapper.db.models import (
    CollectorRun,
    Company,
    Contact,
    Heartbeat,
    LeadCandidate,
    MergeLog,
    Outcome,
    PushLog,
    RuntimeFlag,
    Source,
    Suppression,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import companies, erp_feeds, health, outcomes
from clientscrapper.services.erp_feeds import ErpClient
from tests import fake_erp
from tests.fake_erp import FakeErp
from tests.fakes import FakeClock, Site, fetcher_for

TASHKENT = ZoneInfo("Asia/Tashkent")
# The nightly run (0 3 * * *); 2026-10-23T22:00:00Z.
NOW = datetime(2026, 10, 24, 3, 0, tzinfo=TASHKENT)
SECRET = "test-secret"
OTHER_KEY = "other-key"
BASE = "https://erp.test"
PATH = erp_feeds.OUTCOMES_PATH
DAY = timedelta(days=1)


# --- fixtures -------------------------------------------------------------------------

ROW_FIELDS = ("enabled", "config", "breaker_until", "breaker_reason", "paused_until",
              "paused_reason", "last_run_at")  # fmt: skip
NUMBERS = ("precision_30d", "positives_30d", "pushes_30d", "auto_push")


@pytest.fixture
async def erp_row(session: AsyncSession) -> AsyncIterator[None]:
    """``sources.erp_sync`` and every row's precision numbers as they were,
    put back after the test; erp_sync enabled for it (the seed ships it off)."""
    row = await session.get(Source, "erp_sync", populate_existing=True)
    assert row is not None
    kept = {f: getattr(row, f) for f in ROW_FIELDS}
    numbers = {
        r.key: {f: getattr(r, f) for f in NUMBERS}
        for r in await session.scalars(
            sa.select(Source).execution_options(populate_existing=True)
        )
    }
    row.enabled = True
    await session.commit()
    yield
    async with SessionLocal() as own:
        await own.execute(
            sa.update(Source).where(Source.key == "erp_sync").values(**kept)
        )
        for key, values in numbers.items():
            await own.execute(sa.update(Source).where(Source.key == key).values(**values))
        await own.commit()


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "crm_push_source", "agent")
    monkeypatch.setattr(settings, "crm_agent_key", SECRET)
    monkeypatch.setattr(settings, "crm_base_url", BASE)
    monkeypatch.setattr(settings, "collectors_enabled", True)


async def no_sleep(_: float) -> None:
    return None


class ErpClock:
    """The ERP's own clock (the rows' ``at``), set by the test."""

    def __init__(self, at: datetime) -> None:
        self.at = at.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.at

    def set(self, at: datetime) -> None:
        self.at = at.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Pushed:
    company_id: uuid.UUID
    candidate_id: uuid.UUID
    external_id: str
    tail: str
    push_id: int


def phone(n: int) -> str:
    return f"+998 90 000 00 {n:02d}"


def tail(n: int) -> str:
    return f"9000000{n:02d}"


@dataclass
class World:
    session: AsyncSession
    clock: ErpClock = field(default_factory=lambda: ErpClock(NOW - DAY))
    fake: FakeErp = field(init=False)
    erp: ErpClient = field(init=False)

    def __post_init__(self) -> None:
        self.fake = FakeErp(SECRET, other_secret=OTHER_KEY, clock=self.clock)
        self.erp = ErpClient(
            BASE, SECRET, transport=self.fake.transport(), sleep=no_sleep
        )

    async def company(self, n: int, **attrs: Any) -> uuid.UUID:
        company = Company(display_name=f"Namuna Savdo {n}", name_key=f"namuna savdo {n}",
                          **attrs)  # fmt: skip
        self.session.add(company)
        await self.session.flush()
        return company.id

    async def push(
        self,
        n: int,
        *,
        company_id: uuid.UUID | None = None,
        status: str = "sent",
        http: int | None = 200,
        sent_at: datetime | None = None,
        source: str = "telegram_monitor",
        door: str = "agent",
        post: bool = True,
        month: str = "202610",
        attempt: int = 1,
        payload: dict[str, Any] | None = None,
        superseded_at: datetime | None = None,
        phone_n: int | None = None,
        candidate_id: uuid.UUID | None = None,
    ) -> Pushed:
        """The agent's push of company ``n``: the candidate and the push_log
        row crm_push writes, and (``post``) the POST the ERP received."""
        at = sent_at or self.clock()
        cid = company_id or await self.company(n)
        eid = f"cs:{cid}:{month}"
        number = phone_n if phone_n is not None else n
        pushed = status == "sent"
        if candidate_id is None:
            candidate = LeadCandidate(
                company_id=cid,
                state="pushed" if pushed else "approved",
                score=70,
                primary_source_key=source,
                approved_by="owner:1",
                approved_at=at,
                external_id=eid if pushed else None,
                pushed_at=at if pushed else None,
            )
            self.session.add(candidate)
            await self.session.flush()
            candidate_id = candidate.id
        body = {
            "external_id": eid,
            "name": f"Namuna Savdo {n}",
            "phone": phone(number),
            "note": "NEGA TOPILDI: namuna",
            "utm_source": "clientscrapper",
        }
        row = PushLog(
            external_id=eid,
            attempt=attempt,
            candidate_id=candidate_id,
            company_id=cid,
            phone_tail=tail(number),
            endpoint=f"/api/leads/in/{door}",
            source_key_sent=door,
            payload=body if payload is None else payload,
            payload_sha256="0" * 64,
            status=status,
            http_status=http,
            sent_at=at,
            superseded_at=superseded_at,
        )
        self.session.add(row)
        await self.session.commit()
        if post:
            await self.post(body, door=door)
        return Pushed(cid, candidate_id, eid, tail(number), row.id)

    async def post(self, body: dict[str, Any], *, door: str = "agent") -> httpx.Response:
        key = SECRET if door == "agent" else OTHER_KEY
        async with httpx.AsyncClient(transport=self.fake.transport(), base_url=BASE) as c:
            answer = await c.post(
                f"/api/leads/in/{door}", json=body, headers={"X-GSR-Key": key}
            )
        assert answer.status_code == 200
        return answer

    def lead(self, p: Pushed) -> fake_erp.FakeLead:
        lead = self.fake.lead_of(p.external_id)
        assert lead is not None
        return lead

    async def walk(self, walk: erp_feeds.Walk = "cursor", now: datetime = NOW):
        return await erp_feeds.sync_outcomes(self.session, self.erp, walk=walk, now=now)

    def gets(self) -> list[dict[str, str]]:
        return [g.params for g in self.fake.gets if g.path == PATH]


@pytest.fixture
async def world(session: AsyncSession, erp_row: None) -> World:
    return World(session)


async def outcome(session: AsyncSession, eid: str) -> Outcome:
    row = await session.get(Outcome, eid, populate_existing=True)
    assert row is not None
    return row


async def local(session: AsyncSession) -> set[tuple[str, str, str, str | None]]:
    rows = await session.execute(
        sa.select(
            Suppression.kind, Suppression.key_type, Suppression.key, Suppression.reason
        )  # fmt: skip
        .where(Suppression.source == "outcomes")
        .execution_options(populate_existing=True)
    )
    return {tuple(r) for r in rows}  # type: ignore[misc]


async def company(session: AsyncSession, cid: uuid.UUID) -> Company:
    row = await session.get(Company, cid, populate_existing=True)
    assert row is not None
    return row


async def raised(session: AsyncSession) -> dict[str, dict[str, Any]]:
    rows = await session.scalars(
        sa.select(RuntimeFlag)
        .where(RuntimeFlag.key.startswith(health.PROBLEM_FLAG_PREFIX))
        .execution_options(populate_existing=True)
    )
    return {r.key.removeprefix(health.PROBLEM_FLAG_PREFIX): r.value for r in rows}


def because(p: Pushed, prefix: str = "outcome:") -> str:
    return prefix + p.external_id


async def merge(session: AsyncSession, src: uuid.UUID, tgt: uuid.UUID) -> None:
    """What T-17's ``merge_into`` leaves behind (04 §2.5, 02 §2.14): the
    source's contacts on the target, the source merged into it, a merge_log
    row — ``push_log`` and ``outcomes`` where they were."""
    moved = (
        await session.scalars(sa.select(Contact.id).where(Contact.company_id == src))
    ).all()
    await session.execute(
        sa.update(Contact).where(Contact.company_id == src).values(company_id=tgt)
    )
    await session.execute(
        sa.update(Company)
        .where(Company.id == src)
        .values(state="merged", merged_into=tgt)
    )
    session.add(
        MergeLog(
            source_company_id=src,
            target_company_id=tgt,
            decided_by="owner:1",
            moved={"contacts": [str(c) for c in moved]},
        )
    )
    await session.commit()


async def add_contact(session: AsyncSession, cid: uuid.UUID, **attrs: Any) -> None:
    session.add(Contact(company_id=cid, tier="company", **attrs))
    await session.commit()


# --- the labels land, and what each one does (02 §2.11) -------------------------------


async def test_won_makes_the_company_a_client_and_keeps_its_tail(world, session):
    p = await world.push(1)
    world.fake.win(world.lead(p).id)
    report = await world.walk()
    row = await outcome(session, p.external_id)
    assert (row.label, row.precision_class, row.landing, row.stage_kind) == (
        "won",
        "positive",
        "created",
        "won",
    )
    assert (row.candidate_id, row.company_id, row.source_key) == (
        p.candidate_id,
        p.company_id,
        "telegram_monitor",
    )
    assert (await company(session, p.company_id)).state == "client"
    # 05 §B.2: the feed brings the hash a day later; the tail holds the phone now.
    assert await local(session) == {("client", "phone_tail", "900000001", because(p))}
    assert (report.rows, report.stored, report.changed, report.labels) == (
        1,
        1,
        1,
        {"won": 1},
    )
    assert (report.inserted, report.deleted) == (1, 0)


async def test_a_fallback_client_landing_is_a_client_too(world, session):
    world.fake.add_client([phone(2)])
    p = await world.push(2, door="other")
    await world.walk()
    row = await outcome(session, p.external_id)
    assert (row.landing, row.label) == ("client", "landed_client")
    assert (await company(session, p.company_id)).state == "client"
    assert await local(session) == {("client", "phone_tail", "900000002", because(p))}


async def test_dropped_suppressed_keeps_the_tail_and_raises_the_stale_hit(world, session):
    world.fake.add_client([phone(3)])  # the ERP's belt refuses it
    p = await world.push(3)
    report = await world.walk()
    row = await outcome(session, p.external_id)
    assert (row.landing, row.landing_reason, row.label) == (
        "dropped",
        "suppressed",
        "dropped_suppressed",
    )
    assert await local(session) == {
        ("client", "phone_tail", "900000003", because(p, "erp_belt:"))
    }
    # The belt says neither client nor lost: the company is left as it was.
    assert (await company(session, p.company_id)).state == "new"
    assert report.stale_hits == 1
    assert (await raised(session))["suppression_stale_hit"]["detail"] == {"n": 1}
    # The next completed full pull clears it, and a re-read of the same row
    # (the recent walk) does not raise it again.
    await erp_feeds.sync_suppression(session, world.erp, mode="full", now=NOW)
    assert "suppression_stale_hit" not in await raised(session)
    again = await world.walk("recent", NOW + timedelta(minutes=5))
    assert (again.rows, again.changed, again.stale_hits) == (1, 0, 0)
    assert "suppression_stale_hit" not in await raised(session)


async def test_lost_no_contact_closes_every_key_of_the_company(world, session):
    cid = await world.company(
        4,
        telegram_channel="namuna_kanal",
        instagram="namuna.savdo",
        website_domain="namuna.example.uz",
        stir="300000011",
    )
    await session.execute(
        sa.text(
            "INSERT INTO contacts (company_id, tier, telegram_username, instagram) "
            "VALUES (:c, 'company', 'namuna_ofis', 'namuna_ofis'), "
            "(:c, 'company', 'namuna_eski', NULL)"
        ),
        {"c": cid},
    )
    await session.execute(
        sa.text(
            "UPDATE contacts SET anonymised_at = now() "
            "WHERE telegram_username = 'namuna_eski'"
        )
    )
    p = await world.push(4, company_id=cid)
    world.fake.move(world.lead(p).id, "lost", reason="Qayta qilinmasin")
    await world.walk()
    row = await outcome(session, p.external_id)
    assert (row.no_contact, row.lost_reason, row.label, row.precision_class) == (
        True,
        "Qayta qilinmasin",
        "lost_no_contact",
        "negative",
    )
    r = because(p)
    assert await local(session) == {
        ("lost", "phone_tail", "900000004", r),
        ("lost", "tg_username", "namuna_kanal", r),
        ("lost", "tg_username", "namuna_ofis", r),
        ("lost", "instagram", "namuna.savdo", r),
        ("lost", "instagram", "namuna_ofis", r),
        ("lost", "domain", "namuna.example.uz", r),
        ("lost", "stir", "300000011", r),
    }


async def test_price_and_not_importer_cool_the_company_down(world, session):
    world.fake.dictionary = ["Narx qimmat", "Import qilmaydi"]
    price = await world.push(5)
    importer = await world.push(6)
    world.fake.move(world.lead(price).id, "lost", reason="Narx qimmat")
    world.fake.move(world.lead(importer).id, "lost", reason="Import qilmaydi")
    await world.walk()
    rows = {
        r.key: r
        for r in await session.scalars(
            sa.select(Suppression).where(Suppression.kind == "cooldown")
        )
    }
    assert rows[str(price.company_id)].expires_at == NOW + timedelta(days=180)
    assert rows[str(importer.company_id)].expires_at == NOW + timedelta(days=365)
    assert {(r.key_type, r.reason) for r in rows.values()} == {
        ("company_id", because(price)),
        ("company_id", because(importer)),
    }
    assert (await outcome(session, price.external_id)).label == "lost_price"
    assert (await outcome(session, importer.external_id)).precision_class == "negative"


async def test_a_wrong_number_twice_is_bounced_and_bounced_is_never_taken_back(
    world, session
):
    world.fake.dictionary = ["Raqam notoʻgʻri"]
    first = await world.push(7)
    world.fake.move(world.lead(first).id, "lost", reason="Raqam notoʻgʻri")
    await world.walk()
    once = await session.scalar(
        sa.select(Suppression).where(Suppression.kind == "cooldown")
    )
    assert once is not None
    assert (once.key_type, once.key, once.reason, once.expires_at) == (
        "phone_tail",
        "900000007",
        because(first),
        NOW + timedelta(days=30),
    )
    # Another company on the same phone, a week later: wrong again.
    later = NOW + timedelta(days=7)
    world.clock.set(later - timedelta(hours=1))
    second = await world.push(8, phone_n=7)
    world.fake.move(world.lead(second).id, "lost", reason="Raqam notoʻgʻri")
    await world.walk(now=later)
    # The first company's cooldown goes (it is recomputed too: the count is
    # the one input across companies); the row names the smaller id of the two.
    named = min(because(first), because(second))
    assert await local(session) == {("bounced", "phone_tail", "900000007", named)}
    # Both leads revived: the bounced row stays (a fact about the number).
    world.fake.revive(world.lead(first).id)
    world.fake.revive(world.lead(second).id)
    await world.walk("year", now=later + DAY)
    assert (await outcome(session, second.external_id)).label == "open_untouched"
    assert await local(session) == {("bounced", "phone_tail", "900000007", named)}


async def test_a_competitor_label_flags_the_company_and_the_flag_stays(world, session):
    world.fake.dictionary = ["Kargo kompaniya"]
    p = await world.push(9)
    world.fake.move(world.lead(p).id, "lost", reason="Kargo kompaniya")
    await world.walk()
    assert (await company(session, p.company_id)).is_competitor is True
    assert (await outcome(session, p.external_id)).label == "lost_competitor"
    world.fake.revive(world.lead(p).id)
    await world.walk("year", now=NOW + DAY)
    assert (await outcome(session, p.external_id)).label == "open_untouched"
    assert (await company(session, p.company_id)).is_competitor is True


# --- retractions: a label that changes takes back what it wrote -----------------------


async def test_outcomes_retractions(world, session):
    """A revived lost lead, a won lead moved back to open and a changed label
    each remove exactly the rows their old label justified; rows a person or
    another sync wrote stay."""
    world.fake.dictionary = ["Narx qimmat", "Import qilmaydi"]
    lost = await world.push(11)
    won = await world.push(12)
    changed = await world.push(13)
    session.add_all(
        [
            # A person's DNC on the lost lead's phone, the XLSX fallback's
            # copy of the won phone: neither is the outcomes' to take back.
            Suppression(
                kind="dnc", key_type="phone_tail", key=lost.tail, source="owner_bot"
            ),  # fmt: skip
            Suppression(
                kind="client", key_type="phone_tail", key=won.tail, source="manual_import"
            ),  # fmt: skip
        ]
    )
    await session.commit()
    world.fake.move(world.lead(lost).id, "lost", reason="Qayta qilinmasin")
    world.fake.win(world.lead(won).id)
    world.fake.move(world.lead(changed).id, "lost", reason="Narx qimmat")
    await world.walk()
    assert await local(session) == {
        ("lost", "phone_tail", lost.tail, because(lost)),
        ("cooldown", "company_id", str(changed.company_id), because(changed)),
    }  # the won phone's client row is the manual_import one
    assert (await company(session, won.company_id)).state == "client"

    later = NOW + DAY
    world.clock.set(later - timedelta(hours=2))
    world.fake.revive(world.lead(lost).id)
    world.fake.revive(world.lead(won).id)
    world.fake.move(world.lead(changed).id, "lost", reason="Import qilmaydi")
    report = await world.walk("recent", now=later)
    assert (report.deleted, report.updated) == (1, 1)
    assert await local(session) == {
        ("cooldown", "company_id", str(changed.company_id), because(changed))
    }
    cooldown = await session.scalar(
        sa.select(Suppression).where(Suppression.kind == "cooldown")
    )
    assert cooldown is not None and cooldown.expires_at == later + timedelta(days=365)
    assert (await outcome(session, changed.external_id)).label_changed_at == later
    left = await session.execute(
        sa.select(Suppression.kind, Suppression.source).where(
            Suppression.source != "outcomes"
        )
    )
    assert set(left) == {("dnc", "owner_bot"), ("client", "manual_import")}
    # The won lead is open again, but the XLSX copy still holds its phone as a
    # client's: the company stays a client until no client row holds it.
    assert (await company(session, won.company_id)).state == "client"
    await session.execute(
        sa.delete(Suppression).where(Suppression.source == "manual_import")
    )
    await session.commit()
    world.fake.contact(won.external_id, "call", at=later)
    await world.walk("recent", now=later + timedelta(hours=1))
    assert (await company(session, won.company_id)).state == "active"


async def test_a_won_lead_moved_back_goes_back_to_its_state(world, session):
    excluded = await world.company(14, state="excluded", exclude_reason="owner")
    plain = await world.push(15)
    other = await world.push(14, company_id=excluded)
    for p in (plain, other):
        world.fake.win(world.lead(p).id)
    await world.walk()
    assert (await company(session, plain.company_id)).state == "client"
    assert (await company(session, excluded)).state == "client"
    world.clock.set(NOW + timedelta(hours=1))
    for p in (plain, other):
        world.fake.revive(world.lead(p).id)
    await world.walk("recent", now=NOW + timedelta(hours=2))
    assert (await outcome(session, plain.external_id)).label == "open_untouched"
    assert await local(session) == set()
    assert (await company(session, plain.company_id)).state == "active"
    assert (await company(session, excluded)).state == "excluded"


async def test_the_feed_s_hash_keeps_a_revived_won_company_a_client(world, session):
    p = await world.push(16)
    world.fake.win(world.lead(p).id)
    await world.walk()
    await erp_feeds.sync_suppression(session, world.erp, mode="full", now=NOW)
    feed_row = await session.scalar(
        sa.select(Suppression).where(Suppression.source == "erp_feed")
    )
    assert feed_row is not None and feed_row.key == erp_feeds.hash_tail(SECRET, p.tail)
    world.clock.set(NOW + timedelta(hours=1))
    world.fake.revive(world.lead(p).id)  # the card winLead minted stays
    await world.walk("recent", now=NOW + timedelta(hours=2))
    assert await local(session) == set()
    assert (await company(session, p.company_id)).state == "client"


async def test_a_shared_phone_s_row_passes_to_the_other_company(world, session):
    first = await world.push(17)
    world.fake.win(world.lead(first).id)
    world.clock.set(NOW - timedelta(hours=20))
    # A second company on that phone: the ERP's belt now refuses it.
    second = await world.push(18, phone_n=17)
    await world.walk()
    assert (await outcome(session, second.external_id)).label == "dropped_suppressed"
    assert await local(session) == {("client", "phone_tail", first.tail, because(first))}
    row_id = await session.scalar(sa.select(Suppression.id))
    world.clock.set(NOW + timedelta(hours=1))
    world.fake.revive(world.lead(first).id)
    report = await world.walk("recent", now=NOW + timedelta(hours=2))
    # The first company lets the row go; the second still wants it: handed
    # over in place, its id kept (a candidate's suppressed_by points at it).
    assert (report.deleted, report.inserted, report.updated) == (0, 0, 1)
    assert await local(session) == {
        ("client", "phone_tail", first.tail, because(second, "erp_belt:"))
    }
    assert await session.scalar(sa.select(Suppression.id)) == row_id


# --- unknown → sent, foreign rows -----------------------------------------------------


async def test_an_unknown_push_the_feed_shows_becomes_sent(world, session):
    timeout = await world.push(21, status="unknown", http=None)
    badbody = await world.push(22, status="unknown", http=200)
    report = await world.walk()
    assert report.promoted == 2
    for p, http in ((timeout, None), (badbody, 200)):
        row = await session.get(PushLog, p.push_id, populate_existing=True)
        assert row is not None and (row.status, row.http_status) == ("sent", http)
        cand = await session.get(LeadCandidate, p.candidate_id, populate_existing=True)
        assert cand is not None
        assert (cand.state, cand.external_id, cand.pushed_at) == (
            "pushed",
            p.external_id,
            row.sent_at,
        )
    again = await world.walk("recent")
    assert again.promoted == 0


async def test_a_superseded_unknown_row_stays_history(world, session):
    old = NOW - timedelta(days=3)
    p = await world.push(23, status="unknown", http=None, sent_at=old, superseded_at=old)
    await world.walk()
    row = await session.get(PushLog, p.push_id, populate_existing=True)
    assert row is not None and row.status == "unknown"
    cand = await session.get(LeadCandidate, p.candidate_id, populate_existing=True)
    assert cand is not None and cand.state == "approved"
    assert (await outcome(session, p.external_id)).label == "open_untouched"


async def test_outcomes_ignores_foreign_rows(world, session):
    """The «Boshqa» clause is permanent: a cs: id the agent never pushed is
    counted and never stored (05 §A.13)."""
    ours = await world.push(24)
    await world.post(
        {
            "external_id": f"cs:{uuid.uuid4()}:202610",
            "name": "Begona",
            "phone": phone(25),
            "utm_source": "clientscrapper",
        }  # fmt: skip
    )
    await world.post(
        {
            "external_id": f"cs:{uuid.uuid4()}:202610",
            "name": "Begona 2",
            "phone": phone(26),
            "utm_source": "clientscrapper",
        },  # fmt: skip
        door="other",
    )
    report = await world.walk()
    assert (report.rows, report.foreign, report.stored) == (3, 2, 1)
    stored = await session.scalars(sa.select(Outcome.external_id))
    assert stored.all() == [ours.external_id]


async def test_raw_is_the_15_fields_verbatim(world, session):
    p = await world.push(27)
    world.fake.outcome_overrides[p.external_id] = {"first_receipt_at": None}
    await world.walk()
    row = await outcome(session, p.external_id)
    [sent] = [r for _, _, r in world.fake.outcome_rows()]
    assert set(row.raw) == set(outcomes.FIELDS)
    assert row.raw == {k: v for k, v in sent.items() if k != "first_receipt_at"}
    assert row.erp_at == datetime.fromisoformat(sent["at"].replace("Z", "+00:00"))


# --- one row, many reads: the label clock and the renamed label -----------------------


async def test_label_changed_at_moves_only_with_the_label(world, session):
    p = await world.push(28)
    world.fake.contact(p.external_id, "call", at=NOW - DAY)
    await world.walk()
    first = await outcome(session, p.external_id)
    assert (first.label, first.precision_class, first.contact_kind) == (
        "contacted_open",
        "pending",
        "call",
    )
    assert first.first_read_at == first.label_changed_at == first.read_at == NOW
    ripe = NOW + timedelta(days=13)
    await world.walk("quarter", now=ripe)
    row = await outcome(session, p.external_id)
    assert (row.label, row.precision_class) == ("contacted_open", "positive")
    assert (row.first_read_at, row.label_changed_at, row.read_at) == (NOW, NOW, ripe)


async def test_a_renamed_dictionary_label_keeps_the_label(world, session):
    world.fake.dictionary = ["Narx qimmat"]
    p = await world.push(29)
    world.fake.move(world.lead(p).id, "lost", reason="Narx qimmat")
    await world.walk()
    # The owner renames the label: the ERP now sends null for every old lead.
    world.fake.dictionary = ["Narx juda qimmat"]
    [sent] = [r for _, _, r in world.fake.outcome_rows()]
    assert sent["lost_reason"] is None
    await world.walk("year", now=NOW + DAY)
    row = await outcome(session, p.external_id)
    assert (row.label, row.lost_reason, row.label_changed_at) == (
        "lost_price",
        "Narx qimmat",
        NOW,
    )
    assert row.raw["lost_reason"] is None
    assert await local(session) == {
        ("cooldown", "company_id", str(p.company_id), because(p))
    }


# --- the walks: params, cursor, failure -----------------------------------------------


async def test_the_four_walks_send_exactly_their_params(world, session):
    p = await world.push(31)
    await world.walk("cursor")
    await world.walk("cursor")
    await world.walk("recent")
    await world.walk("quarter")
    await world.walk("year")
    resume = fake_erp.encode_cursor(
        fake_erp.us_text(NOW.astimezone(UTC) - DAY),
        world.fake.intakes[p.external_id].id,
        0,
    )
    assert world.gets() == [
        {"limit": "1000"},  # the very first cursor walk: the whole year
        {"limit": "1000", "cursor": resume},
        {"limit": "1000", "since": "2026-10-15T22:00:00.000000Z"},  # now − 8 d
        {"limit": "1000", "since": "2026-07-25T22:00:00.000000Z"},  # now − 90 d
        {"limit": "1000"},  # neither since nor cursor
    ]
    cfg = await erp_feeds.outcomes_config(session)
    assert cfg == erp_feeds.OutcomesConfig(
        cursor=resume,
        last_cursor_at=NOW,
        last_recent_at=NOW,
        last_quarter_at=NOW,
        last_year_at=NOW,
    )
    stored = (await session.get(Source, "erp_sync", populate_existing=True)).config
    assert stored["outcomes"] == {
        "cursor": resume,
        "last_cursor_at": NOW.isoformat(),
        "last_recent_at": NOW.isoformat(),
        "last_quarter_at": NOW.isoformat(),
        "last_year_at": NOW.isoformat(),
    }
    # The suppression half of the config is left as it stands.
    assert stored["suppression"]["salt_id"] is None


async def test_an_empty_first_walk_stores_no_cursor(world, session):
    # resume is null on an empty page when no cursor was sent: keep none.
    report = await world.walk("cursor")
    assert (report.pages, report.rows, report.cursor_stored) == (1, 0, False)
    cfg = await erp_feeds.outcomes_config(session)
    assert (cfg.cursor, cfg.last_cursor_at) == (None, NOW)
    p = await world.push(37)
    await world.walk("cursor", now=NOW + timedelta(hours=1))
    assert world.gets() == [{"limit": "1000"}, {"limit": "1000"}]
    assert (await erp_feeds.outcomes_config(session)).cursor is not None
    assert (await outcome(session, p.external_id)).label == "open_untouched"


async def test_the_cursor_is_the_last_non_null_resume(world, session, monkeypatch):
    monkeypatch.setattr(erp_feeds, "OUTCOMES_LIMIT", 2)
    pushed = []
    for n in range(32, 37):
        world.clock.set(NOW - DAY + timedelta(minutes=n))
        pushed.append(await world.push(n))
    report = await world.walk("cursor")
    assert (report.pages, report.rows, report.cursor_stored) == (3, 5, True)
    sent = world.gets()
    assert [set(q) for q in sent] == [{"limit"}, {"limit", "cursor"}, {"limit", "cursor"}]

    def cursor_of(p: Pushed) -> str:
        intake = world.fake.intakes[p.external_id]
        return fake_erp.encode_cursor(fake_erp.us_text(intake.created_at), intake.id, 0)

    assert (await erp_feeds.outcomes_config(session)).cursor == cursor_of(pushed[-1])
    # Nothing new: the stored cursor goes back verbatim, and the empty page's
    # resume (the cursor sent) leaves it as it was.
    world.fake.gets.clear()
    nothing = await world.walk("cursor", now=NOW + DAY)
    assert world.gets() == [{"limit": "2", "cursor": cursor_of(pushed[-1])}]
    assert (nothing.rows, nothing.cursor_stored) == (0, True)
    assert (await erp_feeds.outcomes_config(session)).cursor == cursor_of(pushed[-1])
    # A contact moves one row's `at`: the next cursor walk reads that row.
    world.clock.set(NOW + DAY)
    world.fake.contact(pushed[0].external_id, "note", at=NOW + DAY)
    world.fake.gets.clear()
    moved = await world.walk("cursor", now=NOW + 2 * DAY)
    assert moved.rows == 1
    intake = world.fake.intakes[pushed[0].external_id]
    assert (await erp_feeds.outcomes_config(session)).cursor == fake_erp.encode_cursor(
        fake_erp.us_text(NOW.astimezone(UTC) + DAY), intake.id, 0
    )


async def test_a_walk_that_fails_midway_keeps_its_committed_pages(
    world, session, monkeypatch
):
    monkeypatch.setattr(erp_feeds, "OUTCOMES_LIMIT", 2)
    pushed = []
    for n in range(41, 45):
        world.clock.set(NOW - DAY + timedelta(minutes=n))
        pushed.append(await world.push(n))
    world.fake.feed_modes[PATH] = ["ok", "feed_shape"]  # page 2 carries a phone
    with pytest.raises(erp_feeds.FeedShapeError):
        await world.walk("cursor")
    stored = await session.scalars(
        sa.select(Outcome.external_id).order_by(Outcome.erp_at)
    )
    assert stored.all() == [pushed[0].external_id, pushed[1].external_id]
    intake = world.fake.intakes[pushed[1].external_id]
    cfg = await erp_feeds.outcomes_config(session)
    assert cfg.cursor == fake_erp.encode_cursor(fake_erp.us_text(intake.created_at),
                                                intake.id, 0)  # fmt: skip
    assert cfg.last_cursor_at is None  # the walk did not complete
    problem = (await raised(session))["feed_contract"]["detail"]
    assert problem == {"code": "shape", "feed": "outcomes"}
    # The next walk starts from page 1's resume and completes; the contract
    # problem it raised is cleared by its own feed reading cleanly.
    report = await world.walk("cursor", now=NOW + timedelta(hours=1))
    assert (report.rows, report.foreign) == (2, 0)
    assert "feed_contract" not in await raised(session)


async def test_a_feed_404_on_outcomes_trips_the_breaker(world, session):
    await world.push(46)
    world.fake.rotate_secret("re-minted")
    with pytest.raises(erp_feeds.ErpHttpError):
        await world.walk("recent")
    row = await session.get(Source, "erp_sync", populate_existing=True)
    assert row is not None
    assert (row.breaker_until, row.breaker_reason) == (NOW + timedelta(hours=6),
                                                       "push_404:feed")  # fmt: skip
    assert await session.scalar(sa.select(sa.func.count(Outcome.external_id))) == 0


async def test_the_unknown_walk_reads_from_the_oldest_unknown_push(world, session):
    nothing = await world.walk("unknown")
    assert (nothing.status, world.gets()) == ("nothing", [])
    older = NOW - timedelta(hours=30)
    await world.push(47, status="unknown", http=None, sent_at=older)
    await world.push(48, status="unknown", http=None, sent_at=NOW - timedelta(hours=2))
    # Older than the 48-hour window and superseded rows do not count.
    await world.push(49, status="unknown", http=None, sent_at=NOW - timedelta(hours=49))
    await world.push(50, status="unknown", http=None, sent_at=NOW - timedelta(hours=40),
                     superseded_at=NOW - timedelta(hours=1))  # fmt: skip
    report = await world.walk("unknown")
    assert world.gets() == [
        {"limit": "1000", "since": erp_feeds.fmt_since(older - timedelta(minutes=10))}
    ]
    assert world.gets()[0]["since"] == "2026-10-22T15:50:00.000000Z"
    assert report.promoted == 3  # 47, 48 and 49: the feed shows them
    cfg = await erp_feeds.outcomes_config(session)
    assert cfg.last_cursor_at is None and cfg.cursor is None  # stamps nothing


# --- the 48-hour rule (02 §2.10 rule 5(c)) --------------------------------------------


async def _nightly(world: World, now: datetime = NOW) -> None:
    await world.walk("cursor", now)
    await world.walk("recent", now)


async def test_push_unconfirmed_after_healthy_nightly_walks_only(world, session):
    lost_answer = await world.push(51, sent_at=NOW - timedelta(days=3), post=False)
    await world.push(52, sent_at=NOW - timedelta(days=3))  # the ERP shows it
    await world.push(53, sent_at=NOW - timedelta(hours=47), post=False)  # too young
    await world.push(54, sent_at=NOW - timedelta(days=31), post=False)  # too old
    early = await erp_feeds.reconcile_unconfirmed(session, world.erp, now=NOW)
    assert (early.checked, early.why) == (False, "nightly_walks_incomplete")
    assert "push_unconfirmed" not in await raised(session)
    await _nightly(world)
    report = await erp_feeds.reconcile_unconfirmed(session, world.erp, now=NOW)
    assert (report.checked, report.unconfirmed, report.repushable) == (
        True,
        1,
        [lost_answer.push_id],
    )
    assert (await raised(session))["push_unconfirmed"]["detail"] == {
        "n": 1,
        "repushable": 1,
    }


async def test_a_failed_nightly_walk_holds_the_rule(world, session):
    await world.push(55, sent_at=NOW - timedelta(days=3), post=False)
    await _nightly(world, NOW - DAY)  # last night was healthy
    tonight = NOW
    await world.walk("cursor", tonight)
    world.fake.feed_modes[PATH] = "feed_503"
    with pytest.raises(erp_feeds.ErpHttpError):
        await world.walk("recent", tonight)
    report = await erp_feeds.reconcile_unconfirmed(session, world.erp, now=tonight)
    assert (report.checked, report.why) == (False, "nightly_walks_incomplete")
    assert "push_unconfirmed" not in await raised(session)
    # Through the collector: the failed walk ends the run before the rule.
    world.fake.feed_modes[PATH] = "feed_503"
    run = await erp_sync.run("outcomes", fetcher=a_fetcher(), now=tonight + DAY,
                             erp=world.erp)  # fmt: skip
    assert run.status == "failed"
    assert "reconcile" not in run.meta
    assert "push_unconfirmed" not in await raised(session)


async def test_the_rule_s_edges_and_who_may_be_re_pushed(world, session):
    edge = NOW - timedelta(hours=48)
    oldest = NOW - timedelta(days=30)
    at_48 = await world.push(56, sent_at=edge, post=False)
    at_30d = await world.push(57, sent_at=oldest, post=False)
    await world.push(58, sent_at=edge + timedelta(seconds=1), post=False)
    await world.push(59, sent_at=oldest - timedelta(seconds=1), post=False)
    no_payload = await world.push(60, sent_at=edge, post=False, payload={})
    await session.execute(
        # Retention's R6: the body is gone after 90 d (an SQL NULL).
        sa.update(PushLog)
        .where(PushLog.id == no_payload.push_id)
        .values(payload=sa.null())
    )
    # A JSON null is no body to re-send either.
    json_null = await world.push(64, sent_at=edge, post=False, payload={})
    await session.execute(
        sa.update(PushLog)
        .where(PushLog.id == json_null.push_id)
        .values(payload=sa.text("'null'::jsonb"))
    )
    await world.push(61, sent_at=edge, post=False, attempt=3)
    # Rule 5 already re-pushed this one once: the re-push is still unconfirmed.
    cid = await world.company(62)
    first = await world.push(
        62,
        company_id=cid,
        sent_at=NOW - timedelta(days=5),
        post=False,
        superseded_at=NOW - timedelta(days=3),
    )
    again = await world.push(63, company_id=cid, sent_at=NOW - timedelta(days=3),
                             post=False, attempt=2, phone_n=62,
                             candidate_id=first.candidate_id)  # fmt: skip
    assert first.external_id == again.external_id
    await session.commit()
    await _nightly(world)
    report = await erp_feeds.reconcile_unconfirmed(session, world.erp, now=NOW)
    assert report.unconfirmed == 6
    assert report.repushable == [at_30d.push_id, at_48.push_id]
    assert (await raised(session))["push_unconfirmed"]["detail"] == {
        "n": 6,
        "repushable": 2,
    }
    # Every one of them shows up in the ERP: the problem is cleared.
    await session.execute(sa.update(PushLog).values(sent_at=NOW - timedelta(hours=1)))
    await session.commit()
    cleared = await erp_feeds.reconcile_unconfirmed(session, world.erp, now=NOW)
    assert cleared.unconfirmed == 0
    assert "push_unconfirmed" not in await raised(session)


async def test_the_rule_waits_for_the_feeds(world, session, monkeypatch):
    monkeypatch.setattr(settings, "crm_push_source", "other")
    report = await erp_feeds.reconcile_unconfirmed(session, world.erp, now=NOW)
    assert (report.checked, report.why) == (False, "feeds_not_configured")


# --- precision (05 §B.2, 02 §2.1) -----------------------------------------------------


async def test_the_precision_numbers_per_source(world, session):
    world.fake.dictionary = ["Import qilmaydi"]
    won = await world.push(71)
    importer = await world.push(72)
    contacted = await world.push(73)
    await world.push(74, source="olx_business")
    await world.push(75, sent_at=NOW - timedelta(days=31))
    await world.push(76, status="unknown", http=None, superseded_at=NOW - DAY)
    world.fake.win(world.lead(won).id)
    world.fake.move(world.lead(importer).id, "lost", reason="Import qilmaydi")
    world.fake.contact(contacted.external_id, "call", at=NOW - DAY)
    await world.walk()
    numbers = await erp_feeds.source_precision(session, "telegram_monitor", NOW)
    # Pushes: 71–73; 75 is older than 30 days, 76 was superseded.
    assert numbers == erp_feeds.Precision(
        "telegram_monitor", positives=1, negatives=1, pushes=3
    )
    assert numbers.precision_30d == Decimal("0.5000")
    rows = {
        r.key: r
        for r in await session.scalars(
            sa.select(Source).execution_options(populate_existing=True)
        )
    }
    tg, olx = rows["telegram_monitor"], rows["olx_business"]
    assert (tg.precision_30d, tg.positives_30d, tg.pushes_30d, tg.auto_push) == (
        Decimal("0.5000"),
        1,
        3,
        False,
    )
    assert (olx.precision_30d, olx.positives_30d, olx.pushes_30d) == (None, 0, 1)
    # An outcome first read more than 30 days ago is not this month's evidence.
    await session.execute(
        sa.update(Outcome)
        .where(Outcome.external_id == won.external_id)
        .values(first_read_at=NOW - timedelta(days=31))
    )
    await session.commit()
    later = await erp_feeds.source_precision(session, "telegram_monitor", NOW)
    assert (later.positives, later.negatives, later.precision_30d) == (0, 1, Decimal("0"))


def test_precision_is_rounded_to_the_column():
    assert erp_feeds.Precision("x", positives=2, negatives=1).precision_30d == Decimal(
        "0.6667"
    )
    assert erp_feeds.Precision("x").precision_30d is None


# --- the collector, the jobs ----------------------------------------------------------


def a_fetcher():
    return fetcher_for(Site(FakeClock()))


async def test_the_nightly_run_walks_both_and_then_checks_the_rule(world, session):
    await world.push(81)
    # Last Sunday's and the 1st's walks completed: nothing to catch up.
    await world.walk("quarter", NOW - DAY)
    await world.walk("year", NOW - DAY)
    world.fake.gets.clear()
    report = await erp_sync.run("outcomes", fetcher=a_fetcher(), now=NOW, erp=world.erp)
    assert (report.status, report.component) == ("ok", "collector:erp_sync.outcomes")
    assert [set(q) for q in world.gets()] == [{"limit"}, {"limit", "since"}]
    run = await session.scalar(
        sa.select(CollectorRun).execution_options(populate_existing=True)
    )
    assert run is not None
    assert run.meta["variant"] == "outcomes"
    assert set(run.meta["outcomes"]) == {"cursor", "recent"}
    assert run.meta["outcomes"]["cursor"]["rows"] == 1
    assert run.meta["outcomes"]["recent"]["foreign"] == 0
    assert run.meta["reconcile"] == {"checked": True, "unconfirmed": 0, "repushable": []}
    assert "catch_up" not in run.meta
    beat = await session.get(Heartbeat, "collector:erp_sync.outcomes",
                             populate_existing=True)  # fmt: skip
    assert beat is not None and beat.detail["status"] == "ok"


@pytest.mark.parametrize("variant", ["quarter", "year"])
async def test_the_sunday_and_monthly_runs(world, session, variant):
    await world.push(82)
    report = await erp_sync.run(variant, fetcher=a_fetcher(), now=NOW, erp=world.erp)
    assert (report.status, report.component) == ("ok", f"collector:erp_sync.{variant}")
    expected = [{"limit", "since"}] if variant == "quarter" else [{"limit"}]
    assert [set(q) for q in world.gets()] == expected
    assert set(report.meta["outcomes"]) == {variant}
    assert "reconcile" not in report.meta


async def test_no_outcomes_walk_during_the_fallback(world, session, monkeypatch):
    monkeypatch.setattr(settings, "crm_push_source", "other")
    for variant in ("outcomes", "quarter", "year"):
        report = await erp_sync.run(variant, fetcher=a_fetcher(), now=NOW, erp=world.erp)
        assert (report.status, report.meta["note"]) == ("skipped", "feeds_not_configured")
    assert world.fake.gets == []


def test_the_jobs_are_03_s_cadences():
    assert [(j.id, j.cron, j.variant, j.task) for j in erp_sync.JOBS] == [
        ("erp_sync_delta", "15 * * * *", "delta", "T-09"),
        ("erp_sync_full", "30 2 * * *", "full", "T-09"),
        ("erp_sync_outcomes", "0 3 * * *", "outcomes", "T-10"),
        ("erp_sync_quarter", "20 3 * * 0", "quarter", "T-10"),
        ("erp_sync_year", "40 3 1 * *", "year", "T-10"),
    ]
    assert erp_sync.OUTCOME_WALKS == {
        "outcomes": ("cursor", "recent"),
        "quarter": ("quarter",),
        "year": ("year",),
    }


# --- apply_effects alone --------------------------------------------------------------


def test_desired_prefers_the_longer_cooldown_and_the_landing_over_the_belt():
    cid = uuid.UUID("00000000-0000-4000-8000-0000000000c1")
    keys = outcomes.CompanyKeys(id=cid)
    at = NOW - DAY

    def item(eid: str, label: str) -> outcomes.Labelled:
        return outcomes.Labelled(f"cs:{cid}:{eid}", label, at, "900000091")

    want = outcomes.desired(
        keys,
        [
            item("202610", "dropped_suppressed"),
            item("202611", "won"),
            item("202612", "lost_not_importer"),
            item("202701", "lost_price"),
        ],
        wrong_numbers={},
        now=NOW,
    )
    assert want == {
        ("client", "phone_tail", "900000091"): outcomes.Want(f"outcome:cs:{cid}:202611"),
        ("cooldown", "company_id", str(cid)): outcomes.Want(
            f"outcome:cs:{cid}:202612", at + timedelta(days=365)
        ),
    }
    # A cooldown already past is not wanted.
    gone = outcomes.desired(keys, [item("202610", "lost_price")], wrong_numbers={},
                            now=at + timedelta(days=180))  # fmt: skip
    assert gone == {}
    # The first wrong number's 30 days, to the microsecond.
    wrong = [item("202610", "lost_wrong_number")]
    before = at + timedelta(days=30) - timedelta(microseconds=1)
    assert outcomes.desired(keys, wrong, wrong_numbers={"900000091": 1}, now=before) == {
        ("cooldown", "phone_tail", "900000091"): outcomes.Want(
            f"outcome:cs:{cid}:202610", at + timedelta(days=30)
        )
    }
    past = at + timedelta(days=30)
    assert outcomes.desired(keys, wrong, wrong_numbers={"900000091": 1}, now=past) == {}


async def test_apply_effects_on_nothing_does_nothing(session):
    effects = await outcomes.apply_effects(session, [], now=NOW)
    assert effects == outcomes.Effects()


# --- foreignness is decided first (05 §A.13) ------------------------------------------


async def test_a_foreign_row_is_never_validated(world, session):
    """The «Boshqa» key holder chooses the id, and the ERP keeps an id of
    2,048 bytes and more as sent: a row we never pushed is skipped before a
    field of it is read, so it cannot refuse the page our own row is on."""
    ours = await world.push(24)
    long_id = ("cs:" + uuid.uuid4().hex * 100)[:2600]
    shaped = f"cs:{uuid.uuid4()}:202610"
    nameless = f"cs:{uuid.uuid4()}:202610"
    for n, eid in ((25, long_id), (26, shaped), (27, nameless)):
        await world.post(
            {"external_id": eid, "name": "Begona", "phone": phone(n),
             "utm_source": "clientscrapper"},
            door="other",
        )  # fmt: skip
    # A stranger's card can carry any kub; a field of the wrong form; an id
    # that is no string at all.
    world.fake.outcome_overrides[shaped] = {"quoted_volume_m3": 1e12, "intake_id": "17"}
    world.fake.outcome_overrides[nameless] = {"external_id": 17}
    sent = [r["external_id"] for _, _, r in world.fake.outcome_rows()]
    assert long_id in sent and len(long_id) == 2600
    report = await world.walk()
    assert (report.rows, report.foreign, report.stored) == (4, 3, 1)
    stored = await session.scalars(sa.select(Outcome.external_id))
    assert stored.all() == [ours.external_id]
    assert (await erp_feeds.outcomes_config(session)).last_cursor_at == NOW


def test_an_id_is_ours_only_in_push_log_s_shape():
    eid = "cs:00000000-0000-4000-8000-000000000101:202610"
    assert outcomes.pushed_id({"external_id": eid}) == eid
    for other in (eid + "\n", eid.upper(), eid + "0", ("cs:" + "0" * 36) * 70, None, 17):
        assert outcomes.pushed_id({"external_id": other}) is None
    assert outcomes.pushed_id({}) is None


async def test_a_pii_key_on_any_row_is_the_feed_changing_shape(world, session):
    # The keys are the ERP's own projection — no sender chooses them.
    await world.push(28)
    foreign = f"cs:{uuid.uuid4()}:202610"
    await world.post(
        {"external_id": foreign, "name": "Begona", "phone": phone(29),
         "utm_source": "clientscrapper"},
        door="other",
    )  # fmt: skip
    world.fake.outcome_overrides[foreign] = {"phone": "x"}
    with pytest.raises(erp_feeds.FeedShapeError, match="carries"):
        await world.walk()
    assert await session.scalar(sa.select(sa.func.count(Outcome.external_id))) == 0


# --- no outcomes call while the feeds are not configured (05 §B.3) --------------------


async def test_no_outcomes_call_during_the_fallback_whoever_calls(
    world, session, monkeypatch
):
    """T-28's reconcile_job calls the unknown walk directly, every 10 minutes:
    during the «Boshqa» fallback each GET would be a 404 and block every
    push for 6 hours."""
    monkeypatch.setattr(settings, "crm_push_source", "other")
    monkeypatch.setattr(settings, "crm_agent_key", OTHER_KEY)
    erp = ErpClient(BASE, OTHER_KEY, transport=world.fake.transport(), sleep=no_sleep)
    await world.push(90, status="unknown", http=None, door="other",
                     sent_at=NOW - timedelta(hours=1))  # fmt: skip
    for walk in ("unknown", "cursor", "recent", "quarter", "year"):
        report = await erp_feeds.sync_outcomes(session, erp, walk=walk, now=NOW)
        assert (report.walk, report.status, report.pages) == (walk, "not_configured", 0)
    assert world.fake.gets == []
    row = await session.get(Source, "erp_sync", populate_existing=True)
    assert row is not None and (row.breaker_until, row.breaker_reason) == (None, None)
    assert await erp_feeds.outcomes_config(session) == erp_feeds.OutcomesConfig()


# --- a lost row stands while its label stands; keys over the lineage (04 §2.5) --------


async def _lost_with_a_contact(world: World, session, n: int) -> Pushed:
    cid = await world.company(n, telegram_channel="namuna_kanal")
    await add_contact(session, cid, telegram_username="namuna_ofis",
                      instagram="namuna_ofis")  # fmt: skip
    p = await world.push(n, company_id=cid)
    world.fake.move(world.lead(p).id, "lost", reason="Qayta qilinmasin")
    await world.walk()
    return p


def _lost_rows(p: Pushed, *extra: tuple[str, str]) -> set[tuple[str, str, str, str]]:
    r = because(p)
    return {
        ("lost", "phone_tail", p.tail, r),
        ("lost", "tg_username", "namuna_kanal", r),
        ("lost", "tg_username", "namuna_ofis", r),
        ("lost", "instagram", "namuna_ofis", r),
        *(("lost", t, k, r) for t, k in extra),
    }


async def test_a_merge_keeps_the_never_again_and_reads_the_target(world, session):
    p = await _lost_with_a_contact(world, session, 4)
    assert await local(session) == _lost_rows(p)
    tgt = await world.company(5, telegram_channel="namuna_bosh")
    await merge(session, p.company_id, tgt)
    # The 1st of the month: every row of the year re-read, the lead still lost.
    report = await world.walk("year", now=NOW + DAY)
    assert (await outcome(session, p.external_id)).label == "lost_no_contact"
    # Nothing taken back; the target's own handle is the lineage's too.
    assert report.deleted == 0
    assert await local(session) == _lost_rows(p, ("tg_username", "namuna_bosh"))
    # Revived: the label changed, so every row it wrote goes — on the moved
    # contact's handles too.
    world.clock.set(NOW + DAY)
    world.fake.revive(world.lead(p).id)
    await world.walk("recent", now=NOW + 2 * DAY)
    assert await local(session) == set()


async def test_an_anonymised_contact_keeps_its_lost_rows(world, session):
    p = await _lost_with_a_contact(world, session, 6)
    # T-17's anonymise: the person's handles nulled, anonymised_at stamped.
    await session.execute(
        sa.update(Contact)
        .where(Contact.company_id == p.company_id)
        .values(telegram_username=None, instagram=None, anonymised_at=NOW)
    )
    await session.commit()
    report = await world.walk("year", now=NOW + DAY)
    assert report.deleted == 0
    assert await local(session) == _lost_rows(p)


async def test_a_purge_keeps_the_lost_rows(world, session):
    # 04 §7: a purge keeps dnc|legal|lost|client rows and removes the company.
    p = await _lost_with_a_contact(world, session, 7)
    await session.execute(sa.delete(Contact).where(Contact.company_id == p.company_id))
    await session.execute(
        sa.update(Company)
        .where(Company.id == p.company_id)
        .values(state="purged", telegram_channel=None)
    )
    await session.commit()
    report = await world.walk("year", now=NOW + DAY)
    assert report.deleted == 0
    assert await local(session) == _lost_rows(p)


async def test_a_loss_read_after_the_merge_finds_the_moved_contacts(world, session):
    cid = await world.company(8, telegram_channel="namuna_kanal")
    await add_contact(session, cid, telegram_username="namuna_ofis",
                      instagram="namuna_ofis")  # fmt: skip
    p = await world.push(8, company_id=cid)
    tgt = await world.company(9)
    await merge(session, cid, tgt)  # before the seller closed the lead
    world.fake.move(world.lead(p).id, "lost", reason="Qayta qilinmasin")
    await world.walk()
    assert await local(session) == _lost_rows(p)


async def test_a_real_merge_chain_reaches_the_root_s_keys(world, session):
    """T-17's own ``merge_into``, not this file's stand-in, and a chain: A into
    B, then B into C. A's outcome is keyed by A, and A's never-again reads the
    keys of its root C and of every company merged into C — B's channel
    (carried onto C) and C's own Instagram — not A's alone."""
    a = await world.company(77)
    p = await world.push(77, company_id=a)
    b = await world.company(78, telegram_channel="namuna_bosh")
    c = await world.company(79, instagram="namuna_ildiz")
    for source, target in ((a, b), (b, c)):
        await companies.merge_into(
            session,
            await company(session, source),
            await company(session, target),
            by="owner:1",
            now=NOW - DAY,
        )
    await session.commit()
    assert (await company(session, a)).merged_into == b  # A's row points one hop
    assert await companies.company_lineage(session, c) == {a, b, c}
    world.fake.move(world.lead(p).id, "lost", reason="Qayta qilinmasin")
    await world.walk()
    assert (await outcome(session, p.external_id)).company_id == a
    assert await local(session) == {
        ("lost", "phone_tail", p.tail, because(p)),
        ("lost", "tg_username", "namuna_bosh", because(p)),
        ("lost", "instagram", "namuna_ildiz", because(p)),
    }


async def test_an_undone_merge_is_no_lineage(world, session):
    cid = await world.company(10, telegram_channel="namuna_kanal")
    p = await world.push(10, company_id=cid)
    tgt = await world.company(11, telegram_channel="namuna_bosh")
    q = await world.push(11, company_id=tgt)
    await merge(session, cid, tgt)
    # T-17's unmerge: the source active again, the merge_log row stamped.
    await session.execute(
        sa.update(Company)
        .where(Company.id == cid)
        .values(state="active", merged_into=None)
    )
    await session.execute(
        sa.update(MergeLog).values(undone_at=NOW - DAY, undone_by="owner:1")
    )
    await session.commit()
    world.fake.move(world.lead(q).id, "lost", reason="Qayta qilinmasin")
    await world.walk()
    # The target's never-again sits on its own keys only: the source left.
    assert await local(session) == {
        ("lost", "phone_tail", q.tail, because(q)),
        ("lost", "tg_username", "namuna_bosh", because(q)),
    }
    # And the source's own loss reads the source alone.
    world.clock.set(NOW)
    world.fake.move(world.lead(p).id, "lost", reason="Qayta qilinmasin")
    await world.walk(now=NOW + timedelta(hours=1))
    assert ("lost", "tg_username", "namuna_bosh", because(p)) not in await local(session)
    assert ("lost", "tg_username", "namuna_kanal", because(p)) in await local(session)


async def test_a_row_let_go_passes_to_a_merged_company_s_lineage(world, session):
    """A and a merged company S hold one handle (S's contact now sits on its
    target T): when A's loss is revived, the company that holds the handle is
    T, and the label that wants it is S's — the lineage finds it."""
    a = await world.company(13)
    await add_contact(session, a, telegram_username="namuna_ofis")
    first = await world.push(13, company_id=a)
    world.fake.move(world.lead(first).id, "lost", reason="Qayta qilinmasin")
    await world.walk("cursor")
    world.clock.set(NOW)
    src = await world.company(14)
    await add_contact(session, src, telegram_username="namuna_ofis")
    second = await world.push(14, company_id=src)
    await merge(session, src, await world.company(15))
    world.fake.move(world.lead(second).id, "lost", reason="Qayta qilinmasin")
    await world.walk("cursor", now=NOW + timedelta(hours=1))
    held = ("lost", "tg_username", "namuna_ofis")
    assert (*held, because(first)) in await local(session)
    world.clock.set(NOW + timedelta(hours=2))
    world.fake.revive(world.lead(first).id)
    report = await world.walk("cursor", now=NOW + timedelta(hours=3))
    assert report.rows == 1
    assert await local(session) == {
        ("lost", "phone_tail", second.tail, because(second)),
        (*held, because(second)),
    }


async def test_a_key_the_suppression_table_cannot_hold_is_left_out(world, session):
    # ck_suppression_key: 3–200 characters; companies.instagram only has to
    # be lower case, a domain only to look like one.
    long_domain = "a" * 70 + "." + "b" * 70 + "." + "c" * 60 + ".uz"
    assert len(long_domain) == 205
    cid = await world.company(12, instagram="ab", website_domain=long_domain)
    await add_contact(session, cid, instagram="x" * 201)
    p = await world.push(12, company_id=cid)
    world.fake.move(world.lead(p).id, "lost", reason="Qayta qilinmasin")
    report = await world.walk()
    assert report.stored == 1
    assert await local(session) == {("lost", "phone_tail", p.tail, because(p))}


# --- the shared phone, and the client a company stays ---------------------------------


async def test_a_shared_phone_s_row_passes_over_on_a_walk_that_reads_one_row(
    world, session
):
    """The cursor walk carries only the revived company's row (the other's
    did not move): the company holding the same phone is found and takes the
    row over in place."""
    first = await world.push(17)
    world.fake.win(world.lead(first).id)
    world.clock.set(NOW - timedelta(hours=20))
    second = await world.push(18, phone_n=17)  # the belt refuses it
    await world.walk("cursor")
    row_id = await session.scalar(sa.select(Suppression.id))
    world.clock.set(NOW + timedelta(hours=1))
    world.fake.revive(world.lead(first).id)
    report = await world.walk("cursor", now=NOW + timedelta(hours=2))
    assert report.rows == 1
    assert await local(session) == {
        ("client", "phone_tail", first.tail, because(second, "erp_belt:"))
    }
    assert await session.scalar(sa.select(Suppression.id)) == row_id


async def test_a_client_row_on_another_of_its_phones_keeps_it_a_client(world, session):
    p = await world.push(97)
    await add_contact(session, p.company_id, phone="+998" + tail(98), phone_kind="mobile")
    session.add(
        Suppression(kind="client", key_type="phone_tail", key=tail(98),
                    source="manual_import")
    )  # fmt: skip
    await session.commit()
    world.fake.win(world.lead(p).id)
    await world.walk()
    world.clock.set(NOW + timedelta(hours=1))
    world.fake.revive(world.lead(p).id)
    await world.walk("recent", now=NOW + timedelta(hours=2))
    assert await local(session) == set()
    assert (await company(session, p.company_id)).state == "client"


async def test_a_won_outcome_on_a_merged_or_purged_company_moves_no_state(world, session):
    tgt = await world.company(66)
    merged = await world.push(67)
    purged = await world.push(68)
    for p in (merged, purged):
        world.fake.win(world.lead(p).id)
    await merge(session, merged.company_id, tgt)
    await session.execute(
        sa.update(Company).where(Company.id == purged.company_id).values(state="purged")
    )
    await session.commit()
    report = await world.walk()
    assert report.labels == {"won": 2}
    assert (await company(session, merged.company_id)).state == "merged"
    assert (await company(session, purged.company_id)).state == "purged"
    assert await local(session) == {
        ("client", "phone_tail", merged.tail, because(merged)),
        ("client", "phone_tail", purged.tail, because(purged)),
    }


# --- the recompute's lock, the stale hit ----------------------------------------------


def _try_lock() -> sa.Select[tuple[bool]]:
    return sa.select(
        sa.func.pg_try_advisory_xact_lock(sa.func.hashtext(outcomes.EFFECTS_LOCK))
    )


async def test_the_recompute_holds_the_effects_lock(world, session):
    p = await world.push(94)
    await outcomes.apply_effects(session, [p.company_id], now=NOW)
    async with SessionLocal() as other:
        assert await other.scalar(_try_lock()) is False
        await other.rollback()
    await session.commit()
    async with SessionLocal() as other:
        assert await other.scalar(_try_lock()) is True
        await other.rollback()


async def test_the_stale_hit_counts_only_since_the_last_full_pull(world, session):
    world.fake.add_client([phone(95)])
    await world.push(95)
    await world.walk()
    await erp_feeds.sync_suppression(session, world.erp, mode="full", now=NOW)
    assert "suppression_stale_hit" not in await raised(session)
    world.fake.add_client([phone(96)])
    world.clock.set(NOW)
    await world.push(96)
    await world.walk(now=NOW + timedelta(hours=1))
    # Two refusals labelled, one of them after the pull that cleared the first.
    assert (await raised(session))["suppression_stale_hit"]["detail"] == {"n": 1}


async def test_a_capped_push_is_no_stale_hit(world, session):
    cid = await world.company(93)
    world.fake.modes[f"cs:{cid}:202610"] = "capped"
    p = await world.push(93, company_id=cid)
    report = await world.walk()
    assert (await outcome(session, p.external_id)).label == "dropped_capped"
    assert report.stale_hits == 0
    assert "suppression_stale_hit" not in await raised(session)
    assert await local(session) == set()


async def test_an_outcomes_walk_that_reads_ends_the_feed_s_breaker(world, session):
    await erp_feeds.trip_breaker(session, "push_404", status=404, now=NOW - DAY)
    await session.commit()
    await world.walk()
    row = await session.get(Source, "erp_sync", populate_existing=True)
    assert row is not None and (row.breaker_until, row.breaker_reason) == (None, None)


# --- unknown → sent: which row, which status, which candidate --------------------------


async def test_a_re_push_the_feed_shows_is_the_row_promoted(world, session):
    """Rule 5's re-push: the first delivery superseded, the second unknown —
    the feed's row is about the live one, which is promoted, and its sent_at
    ages the label."""
    first = await world.push(65, sent_at=NOW - timedelta(days=40), post=False,
                             superseded_at=NOW - timedelta(days=2))  # fmt: skip
    again = await world.push(65, company_id=first.company_id, status="unknown",
                             http=None, sent_at=NOW - timedelta(days=2), attempt=2,
                             candidate_id=first.candidate_id)  # fmt: skip
    report = await world.walk()
    assert report.promoted == 1
    rows = {
        r.attempt: (r.status, r.superseded_at)
        for r in await session.scalars(
            sa.select(PushLog).execution_options(populate_existing=True)
        )
    }
    assert rows == {1: ("sent", NOW - timedelta(days=2)), 2: ("sent", None)}
    assert again.push_id != first.push_id
    assert (await outcome(session, first.external_id)).label == "open_untouched"


async def test_an_unknown_push_with_a_5xx_is_promoted_with_no_status(world, session):
    # A 503, then a try that timed out: the feed shows the id, so it landed —
    # and the 503 was not its answer.
    p = await world.push(91, status="unknown", http=503, sent_at=NOW - timedelta(days=3))
    report = await world.walk()
    assert report.promoted == 1
    row = await session.get(PushLog, p.push_id, populate_existing=True)
    assert row is not None and (row.status, row.http_status) == ("sent", None)
    cand = await session.get(LeadCandidate, p.candidate_id, populate_existing=True)
    assert cand is not None and cand.state == "pushed"


async def test_a_promotion_moves_only_an_approved_candidate(world, session):
    p = await world.push(92, status="unknown", http=None)
    await session.execute(
        sa.update(LeadCandidate)
        .where(LeadCandidate.id == p.candidate_id)
        .values(state="review")
    )
    await session.commit()
    report = await world.walk()
    assert report.promoted == 1
    row = await session.get(PushLog, p.push_id, populate_existing=True)
    assert row is not None and row.status == "sent"
    cand = await session.get(LeadCandidate, p.candidate_id, populate_existing=True)
    assert cand is not None
    assert (cand.state, cand.pushed_at, cand.external_id) == ("review", None, None)


# --- the 48-hour rule: what it reads, when it may run -----------------------------------


async def test_a_failed_push_is_never_unconfirmed(world, session):
    await world.push(69, status="failed", http=503, sent_at=NOW - timedelta(days=3),
                     post=False)  # fmt: skip
    await _nightly(world)
    report = await erp_feeds.reconcile_unconfirmed(session, world.erp, now=NOW)
    assert (report.checked, report.unconfirmed, report.repushable) == (True, 0, [])
    assert "push_unconfirmed" not in await raised(session)


@pytest.mark.parametrize(
    ("ago", "checked"),
    [(timedelta(hours=25), True), (timedelta(hours=25, microseconds=1), False)],
    ids=["25 h", "past 25 h"],
)
async def test_the_nightly_walks_count_for_25_hours(world, session, ago, checked):
    await _nightly(world, NOW - ago)
    report = await erp_feeds.reconcile_unconfirmed(session, world.erp, now=NOW)
    assert report.checked is checked


def test_the_nightly_pair_is_the_cursor_walk_then_the_recent_one():
    at = NOW - timedelta(hours=1)
    pair = erp_feeds.OutcomesConfig(last_cursor_at=at, last_recent_at=at)
    assert pair.nightly_done(NOW)
    early = erp_feeds.OutcomesConfig(last_cursor_at=at, last_recent_at=at - DAY)
    assert not early.nightly_done(NOW)
    assert not erp_feeds.OutcomesConfig(last_cursor_at=at).nightly_done(NOW)


# --- the catch-up of a Sunday or monthly walk that did not complete --------------------


def test_the_catch_up_is_due_past_a_week_and_the_longest_month():
    on_time = erp_feeds.OutcomesConfig(
        last_quarter_at=NOW - timedelta(days=7, hours=1),
        last_year_at=NOW - timedelta(days=31, hours=1),
    )
    assert on_time.catch_up(NOW) == ()
    assert on_time.catch_up(NOW + timedelta(microseconds=1)) == ("quarter", "year")
    # A normal Sunday 03:00: last Sunday's 03:20 walk is 6 d 23 h 40 min old.
    sunday = erp_feeds.OutcomesConfig(
        last_quarter_at=NOW - timedelta(days=6, hours=23, minutes=40),
        last_year_at=NOW - DAY,
    )
    assert sunday.catch_up(NOW) == ()
    # Never completed: the switch-over day (05 §B.3).
    assert erp_feeds.OutcomesConfig().catch_up(NOW) == ("quarter", "year")


async def test_the_nightly_run_catches_up_a_missed_sunday(world, session):
    await world.push(83)
    await world.walk("quarter", NOW - timedelta(days=8))
    await world.walk("year", NOW - timedelta(days=3))
    world.fake.gets.clear()
    report = await erp_sync.run("outcomes", fetcher=a_fetcher(), now=NOW, erp=world.erp)
    assert report.status == "ok"
    assert world.gets() == [
        {"limit": "1000"},
        {"limit": "1000", "since": erp_feeds.fmt_since(NOW - timedelta(days=8))},
        {"limit": "1000", "since": erp_feeds.fmt_since(NOW - timedelta(days=90))},
    ]
    assert report.meta["catch_up"] == ["quarter"]
    assert set(report.meta["outcomes"]) == {"cursor", "recent", "quarter"}
    assert report.meta["reconcile"]["checked"] is True
    cfg = await erp_feeds.outcomes_config(session)
    assert (cfg.last_quarter_at, cfg.last_year_at) == (NOW, NOW - timedelta(days=3))


async def test_a_failing_catch_up_never_holds_the_rule(world, session):
    lost = await world.push(84, sent_at=NOW - timedelta(days=3), post=False)
    # Cursor and recent answer; the overdue quarter walk gets four 503s.
    world.fake.feed_modes[PATH] = ["ok", "ok", *["feed_503"] * 4]
    report = await erp_sync.run("outcomes", fetcher=a_fetcher(), now=NOW, erp=world.erp)
    assert report.status == "failed"
    assert report.meta["catch_up"] == ["quarter", "year"]
    assert report.meta["reconcile"] == {
        "checked": True,
        "unconfirmed": 1,
        "repushable": [lost.push_id],
    }
    assert (await raised(session))["push_unconfirmed"]["detail"]["n"] == 1
