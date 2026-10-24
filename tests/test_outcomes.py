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
    Heartbeat,
    LeadCandidate,
    Outcome,
    PushLog,
    RuntimeFlag,
    Source,
    Suppression,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import erp_feeds, health, outcomes
from clientscrapper.services.erp_feeds import ErpClient
from tests import fake_erp
from tests.fake_erp import FakeErp
from tests.fakes import FakeClock, Site, fetcher_for

TASHKENT = ZoneInfo("Asia/Tashkent")
# The nightly run (0 3 * * *); 2026-10-23T22:00:00Z.
NOW = datetime(2026, 10, 24, 3, 0, tzinfo=TASHKENT)
SECRET = "test-secret"
OTHER_KEY = "other-key"
BASE = "http://erp.test"
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
    world.clock.set(NOW + timedelta(hours=1))
    world.fake.revive(world.lead(first).id)
    report = await world.walk("recent", now=NOW + timedelta(hours=2))
    # The first company lets the row go; the second still wants it.
    assert (report.deleted, report.inserted) == (1, 1)
    assert await local(session) == {
        ("client", "phone_tail", first.tail, because(second, "erp_belt:"))
    }


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
    assert report.unconfirmed == 5
    assert report.repushable == [at_30d.push_id, at_48.push_id]
    assert (await raised(session))["push_unconfirmed"]["detail"] == {
        "n": 5,
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


async def test_apply_effects_on_nothing_does_nothing(session):
    effects = await outcomes.apply_effects(session, [], now=NOW)
    assert effects == outcomes.Effects()
