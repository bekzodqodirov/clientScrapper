"""The suppression feed sync against the fake ERP and Postgres (07 §1.5,
T-09; 05 §B.2 steps 1–5, §B.6).

Every test runs ``erp_feeds.sync_suppression`` (or the ``erp_sync`` collector
over the runner) on a ``FakeErp`` and reads back the ``suppression`` rows,
``sources.erp_sync`` (config, breaker), the raised problems
(``runtime_flags`` ``problem:<key>``, what ``health`` reads) and the
requests the fake saw. ``sources`` is migration-seeded configuration: the
``erp_sync`` row is snapshotted before each test and put back after.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
    Heartbeat,
    ReminderLog,
    RuntimeFlag,
    Source,
    Suppression,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import costs, erp_feeds, health
from clientscrapper.services.erp_feeds import ErpClient, SuppressionConfig
from tests import fake_erp
from tests.fake_erp import FakeErp
from tests.fakes import FakeClock, Site, fetcher_for

TASHKENT = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 24, 2, 30, tzinfo=TASHKENT)
SECRET = "test-secret"
BASE = "http://erp.test"
PATH = erp_feeds.SUPPRESSION_PATH


# --- fixtures ----------------------------------------------------------------------


ROW_FIELDS = ("enabled", "config", "breaker_until", "breaker_reason", "paused_until",
              "paused_reason", "last_run_at")  # fmt: skip


@pytest.fixture
async def erp_row(session: AsyncSession) -> AsyncIterator[None]:
    """The seeded ``sources.erp_sync`` row as it was, put back after the
    test; enabled for it (the seed ships it off)."""
    row = await session.get(Source, "erp_sync", populate_existing=True)
    assert row is not None
    kept = {f: getattr(row, f) for f in ROW_FIELDS}
    row.enabled = True
    await session.commit()
    yield
    async with SessionLocal() as own:
        await own.execute(
            sa.update(Source).where(Source.key == "erp_sync").values(**kept)
        )
        await own.commit()


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "crm_push_source", "agent")
    monkeypatch.setattr(settings, "crm_agent_key", SECRET)
    monkeypatch.setattr(settings, "crm_base_url", BASE)
    monkeypatch.setattr(settings, "collectors_enabled", True)


@pytest.fixture
def told(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The owner notifier (what the worker registers): every text delivered."""
    said: list[str] = []

    async def notify(text: str) -> bool:
        said.append(text)
        return True

    costs.set_cap_notifier(notify)
    yield said
    costs.set_cap_notifier(None)


class ErpClock:
    """The ERP's own clock (``generated_at``): a minute after each read."""

    def __init__(self, at: datetime) -> None:
        self.at = at.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.at


async def no_sleep(_: float) -> None:
    return None


def make(entries: int = 0, *, secret: str = SECRET) -> tuple[FakeErp, ErpClient]:
    fake = FakeErp(secret, clock=ErpClock(NOW - timedelta(minutes=1)))
    for n in range(entries):
        fake.add_entry(
            tail(n), "client", at=NOW.astimezone(UTC) - timedelta(days=1, seconds=n)
        )
    erp = ErpClient(BASE, SECRET, transport=fake.transport(), sleep=no_sleep)
    return fake, erp


def tail(n: int) -> str:
    return f"9000001{n:02d}"


def h(n: int, secret: str = SECRET) -> str:
    return erp_feeds.hash_tail(secret, tail(n))


async def feed_rows(session: AsyncSession) -> dict[str, Suppression]:
    rows = await session.scalars(
        sa.select(Suppression)
        .where(Suppression.source == "erp_feed")
        .execution_options(populate_existing=True)
    )
    return {r.key: r for r in rows}


async def config(session: AsyncSession) -> SuppressionConfig:
    return await erp_feeds.suppression_config(session)


async def raised(session: AsyncSession) -> dict[str, dict[str, Any]]:
    rows = await session.scalars(
        sa.select(RuntimeFlag)
        .where(RuntimeFlag.key.startswith(health.PROBLEM_FLAG_PREFIX))
        .execution_options(populate_existing=True)
    )
    return {r.key.removeprefix(health.PROBLEM_FLAG_PREFIX): r.value for r in rows}


async def breaker(session: AsyncSession) -> tuple[datetime | None, str | None]:
    row = await session.get(Source, "erp_sync", populate_existing=True)
    assert row is not None
    return row.breaker_until, row.breaker_reason


def params_seen(fake: FakeErp) -> list[dict[str, str]]:
    return [g.params for g in fake.gets if g.path == PATH]


# --- the full pull ------------------------------------------------------------------


async def test_feed_full_pull_never_sends_since_or_cursor(session, erp_row, monkeypatch):
    monkeypatch.setattr(erp_feeds, "SUPPRESSION_LIMIT", 2)
    fake, erp = make(5)
    report = await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    sent = params_seen(fake)
    assert sent[0] == {"limit": "2"}
    assert [set(p) for p in sent] == [{"limit"}, {"limit", "cursor"}, {"limit", "cursor"}]
    assert all("since" not in p for p in sent)
    # next is followed verbatim, with the same params.
    assert sent[1]["cursor"] == fake_erp.encode_cursor(
        fake_erp.us_text(fake.entries[3].at), fake.entries[3].id, 1
    )
    rows = await feed_rows(session)
    assert set(rows) == {h(n) for n in range(5)}
    row = rows[h(0)]
    assert (row.kind, row.key_type, row.salt_id, row.last_seen_at) == (
        "client",
        "phone_hash",
        erp_feeds.salt_id(SECRET),
        NOW,
    )
    assert row.erp_at == datetime(2026, 10, 22, 21, 30, tzinfo=UTC)
    assert (report.walk, report.pages, report.entries, report.seen) == ("full", 3, 5, 5)
    cfg = await config(session)
    assert cfg == SuppressionConfig(
        salt_id="9caf06bb",
        last_full_at=NOW,
        last_delta_at=NOW,
        full_generated_at=datetime(2026, 10, 23, 21, 29, tzinfo=UTC),
        delta_generated_at=None,
        count=5,
    )
    # The outcomes part of the config is left as it stands.
    row_cfg = (await session.get(Source, "erp_sync", populate_existing=True)).config
    assert row_cfg["outcomes"] == {"cursor": None, "last_at": None}


async def test_duplicate_entries_upsert_to_one_row(session, erp_row):
    fake, erp = make()
    old = NOW.astimezone(UTC) - timedelta(days=2)
    fake.add_entry(tail(1), "client", at=old)
    fake.add_entry(tail(1), "client", at=old + timedelta(hours=1))
    fake.add_entry(tail(1), "lost", at=old)
    report = await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    rows = await session.execute(
        sa.select(Suppression.kind, Suppression.erp_at).where(Suppression.key == h(1))
    )
    assert sorted(tuple(r) for r in rows.all()) == [
        ("client", old + timedelta(hours=1)),
        ("lost", old),
    ]
    assert (report.entries, report.seen) == (3, 2)


async def test_feed_full_pull_deletes_unseen_only_when_not_shrunk(session, erp_row):
    fake, erp = make(10)
    await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert len(await feed_rows(session)) == 10

    # 9 of 10 seen: 9 >= 0.9 x 10, the unseen one goes.
    fake.entries.pop()
    later = NOW + timedelta(days=1)
    report = await erp_feeds.sync_suppression(session, erp, mode="full", now=later)
    assert (report.status, report.deleted, report.previous) == ("ok", 1, 10)
    assert set(await feed_rows(session)) == {h(n) for n in range(9)}
    assert (await config(session)).count == 9

    # 7 of 9: below 8.1 — nothing deleted, the count kept, the problem raised.
    del fake.entries[7:]
    report = await erp_feeds.sync_suppression(
        session, erp, mode="full", now=later + timedelta(days=1)
    )
    assert (report.status, report.deleted, report.seen) == ("shrunk", 0, 7)
    assert len(await feed_rows(session)) == 9
    cfg = await config(session)
    assert (cfg.count, cfg.last_full_at) == (9, later + timedelta(days=1))
    problems = await raised(session)
    assert problems["suppression_shrunk"]["detail"] == {
        "pct": 22,
        "seen": 7,
        "previous": 9,
    }

    # The owner's --accept-shrink: one pull deletes; the problem clears.
    report = await erp_feeds.sync_suppression(
        session, erp, mode="full", now=later + timedelta(days=2), accept_shrink=True
    )
    assert (report.status, report.deleted) == ("ok", 2)
    assert set(await feed_rows(session)) == {h(n) for n in range(7)}
    assert (await config(session)).count == 7
    assert "suppression_shrunk" not in await raised(session)


async def test_exactly_ninety_percent_still_deletes(session, erp_row):
    fake, erp = make(20)
    await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    del fake.entries[18:]
    report = await erp_feeds.sync_suppression(
        session, erp, mode="full", now=NOW + timedelta(hours=1)
    )
    assert (report.status, report.deleted) == ("ok", 2)
    del fake.entries[16:]
    report = await erp_feeds.sync_suppression(
        session, erp, mode="full", now=NOW + timedelta(hours=2)
    )
    # 16 < 0.9 x 18 = 16.2
    assert (report.status, report.deleted) == ("shrunk", 0)


async def test_a_full_pull_that_fails_midway_deletes_nothing(
    session, erp_row, monkeypatch
):
    monkeypatch.setattr(erp_feeds, "SUPPRESSION_LIMIT", 2)
    fake, erp = make(4)
    await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    before = await config(session)
    fake.entries.pop(0)
    fake.add_entry(tail(50), at=NOW.astimezone(UTC))
    # Page 1 answers, then the feed is down for every retry of page 2.
    fake.feed_modes[PATH] = ["ok", "feed_503", "feed_503", "feed_503", "feed_503"]
    with pytest.raises(erp_feeds.ErpHttpError) as caught:
        await erp_feeds.sync_suppression(
            session, erp, mode="full", now=NOW + timedelta(hours=1)
        )
    assert caught.value.status == 503
    rows = await feed_rows(session)
    # Page 1 (the two oldest entries) was applied as it came; nothing went —
    # not even h(0), which the ERP no longer carries; page 2's new entry
    # never arrived.
    assert set(rows) == {h(n) for n in range(4)}
    seen = {key: row.last_seen_at for key, row in rows.items()}
    assert seen == {
        h(3): NOW + timedelta(hours=1),
        h(2): NOW + timedelta(hours=1),
        h(1): NOW,
        h(0): NOW,
    }
    assert await config(session) == before
    # A 503 is retried and is never the push's misconfiguration.
    assert await breaker(session) == (None, None)
    assert await raised(session) == {}


# --- the delta ----------------------------------------------------------------------


async def test_feed_delta_anchor_is_erp_generated_at(session, erp_row):
    fake, erp = make(3)
    erp_clock = fake.clock
    assert isinstance(erp_clock, ErpClock)
    erp_clock.at = datetime(2026, 10, 23, 21, 29, 59, 123456, tzinfo=UTC)
    await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    fake.gets.clear()

    erp_clock.at = datetime(2026, 10, 23, 22, 14, 30, 500000, tzinfo=UTC)
    report = await erp_feeds.sync_suppression(
        session, erp, mode="delta", now=NOW + timedelta(minutes=45)
    )
    # The ERP's generated_at (milliseconds on the wire) minus ten minutes, Z.
    assert params_seen(fake) == [
        {"limit": "5000", "since": "2026-10-23T21:19:59.123000Z"}
    ]
    assert (report.walk, report.since) == ("delta", "2026-10-23T21:19:59.123000Z")
    cfg = await config(session)
    assert cfg.delta_generated_at == datetime(
        2026, 10, 23, 22, 14, 30, 500000, tzinfo=UTC
    )
    assert cfg.last_delta_at == NOW + timedelta(minutes=45)
    assert cfg.last_full_at == NOW

    fake.gets.clear()
    await erp_feeds.sync_suppression(
        session, erp, mode="delta", now=NOW + timedelta(minutes=105)
    )
    assert params_seen(fake)[0]["since"] == "2026-10-23T22:04:30.500000Z"


async def test_a_delta_adds_and_never_deletes(session, erp_row):
    fake, erp = make(10)
    await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    # One entry leaves the ERP's set (a delta cannot say so, A.7); the other
    # nine are touched after the anchor and one is new — a delta that held
    # 90 % of the last full pull, so no shrink rule stands in the way.
    fake.entries.pop(0)
    for entry in fake.entries:
        entry.at = NOW.astimezone(UTC)
    fake.add_entry(tail(50), "lost", at=NOW.astimezone(UTC))
    report = await erp_feeds.sync_suppression(
        session, erp, mode="delta", now=NOW + timedelta(hours=1)
    )
    assert (report.walk, report.entries, report.deleted) == ("delta", 10, 0)
    rows = await feed_rows(session)
    assert set(rows) == {h(n) for n in range(10)} | {h(50)}
    assert rows[h(0)].last_seen_at == NOW
    assert rows[h(50)].kind == "lost"
    # A delta is no yardstick for the shrink rule.
    cfg = await config(session)
    assert (cfg.count, cfg.last_full_at) == (10, NOW)


async def test_a_delta_with_no_anchor_runs_as_a_full_pull(session, erp_row):
    fake, erp = make(2)
    report = await erp_feeds.sync_suppression(session, erp, mode="delta", now=NOW)
    assert (report.mode, report.walk) == ("delta", "full")
    assert params_seen(fake) == [{"limit": "5000"}]
    assert (await config(session)).count == 2


async def test_feed_salt_change_forces_full_pull(session, erp_row, told):
    fake, erp = make(3)
    await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    old_rows = set(await feed_rows(session))

    # The owner re-mints the key and pastes it into .env.
    fake.rotate_secret("re-minted")
    erp2 = ErpClient(BASE, "re-minted", transport=fake.transport(), sleep=no_sleep)
    delta = await erp_feeds.sync_suppression(
        session, erp2, mode="delta", now=NOW + timedelta(hours=1)
    )
    assert (delta.status, delta.salt_changed, delta.entries) == ("salt_changed", True, 0)
    assert set(await feed_rows(session)) == old_rows
    assert (await config(session)).salt_id == erp_feeds.salt_id(SECRET)
    # Until the full pull ran, the gate is stale for the new key.
    assert not erp_feeds.suppression_fresh(
        await config(session), key="re-minted", now=NOW + timedelta(hours=1)
    )

    full = await erp_feeds.sync_suppression(
        session, erp2, mode="full", now=NOW + timedelta(hours=1)
    )
    assert (full.status, full.salt_changed, full.deleted) == ("ok", True, 3)
    rows = await feed_rows(session)
    assert set(rows) == {h(n, "re-minted") for n in range(3)}
    assert {r.salt_id for r in rows.values()} == {erp_feeds.salt_id("re-minted")}
    assert erp_feeds.suppression_fresh(
        await config(session), key="re-minted", now=NOW + timedelta(hours=1)
    )
    assert told == []


async def test_the_collector_turns_a_salt_change_into_a_full_pull_in_the_same_run(
    session, erp_row
):
    fake, erp = make(2)
    await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    fake.rotate_secret("re-minted")
    erp2 = ErpClient(BASE, "re-minted", transport=fake.transport(), sleep=no_sleep)
    fake.gets.clear()
    report = await erp_sync.run(
        "delta", fetcher=fetcher_for(Site(FakeClock())), now=NOW + timedelta(hours=1),
        erp=erp2,
    )  # fmt: skip
    assert report.status == "ok"
    assert report.meta["suppression"]["status"] == "salt_changed"
    assert report.meta["suppression_full"]["deleted"] == 2
    assert params_seen(fake)[1] == {"limit": "5000"}
    assert set(await feed_rows(session)) == {h(n, "re-minted") for n in range(2)}


# --- what a failure writes ------------------------------------------------------------


async def test_feed_rule_mismatch_stops(session, erp_row):
    fake, erp = make(2)
    fake.feed_modes[PATH] = "feed_rule_changed"
    with pytest.raises(erp_feeds.FeedContract) as caught:
        await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert caught.value.code == "rule_changed"
    assert await feed_rows(session) == {}
    assert await config(session) == SuppressionConfig()
    problem = (await raised(session))["feed_contract"]
    assert problem["detail"] == {"code": "rule_changed", "feed": "suppression"}
    assert problem["since"] == NOW.isoformat()


async def test_feed_404_is_push_404(session, erp_row, told):
    fake, erp = make(2)
    fake.rotate_secret("somebody-re-minted-it")
    with pytest.raises(erp_feeds.ErpHttpError) as caught:
        await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert caught.value.status == 404
    assert await breaker(session) == (NOW + timedelta(hours=6), "push_404")
    assert await feed_rows(session) == {}
    assert told == [erp_feeds.PUSH_404_TEXT]
    # A second 404 inside the 6 h: the breaker moves on, the owner is not
    # told twice (05 §B.1 «once per 6 h»).
    with pytest.raises(erp_feeds.ErpHttpError):
        await erp_feeds.sync_suppression(
            session, erp, mode="delta", now=NOW + timedelta(hours=1)
        )
    assert await breaker(session) == (NOW + timedelta(hours=7), "push_404")
    assert len(told) == 1
    kinds = (await session.scalars(sa.select(ReminderLog.kind))).all()
    assert kinds == ["alert:push_404"]
    # After the window it is said again.
    with pytest.raises(erp_feeds.ErpHttpError):
        await erp_feeds.sync_suppression(
            session, erp, mode="delta", now=NOW + timedelta(hours=6, minutes=1)
        )
    assert len(told) == 2
    assert await raised(session) == {}


@pytest.mark.parametrize("status", [401, 403])
async def test_a_proxy_s_401_and_403_are_push_404_too(session, erp_row, status):
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="denied")

    erp = ErpClient(BASE, SECRET, transport=httpx.MockTransport(deny), sleep=no_sleep)
    with pytest.raises(erp_feeds.ErpHttpError):
        await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert await breaker(session) == (NOW + timedelta(hours=6), "push_404")
    # No owner notifier registered: nothing is claimed, so the first process
    # that can tell still tells.
    assert (await session.scalars(sa.select(ReminderLog.kind))).all() == []


async def test_feed_3xx_is_never_followed_and_is_erp_redirect(session, erp_row, told):
    fake, erp = make(2)
    fake.feed_modes[PATH] = "feed_3xx:308"
    with pytest.raises(erp_feeds.ErpHttpError) as caught:
        await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert caught.value.status == 308
    # One request: the redirect was not followed (the key not re-sent).
    assert len(fake.requests) == 1
    assert await breaker(session) == (NOW + timedelta(hours=6), "erp_redirect:308")
    assert told == [erp_feeds.ERP_REDIRECT_TEXT.format(code=308)]
    assert "HTTP 308" in told[0]


async def test_feed_503_is_retried_not_push_404(session, erp_row):
    fake, erp = make(2)
    fake.feed_modes[PATH] = ["feed_503", "feed_500", "feed_timeout"]
    report = await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert (report.status, report.entries) == ("ok", 2)
    assert len(fake.requests) == 4
    assert await breaker(session) == (None, None)

    fake.feed_modes[PATH] = ["feed_503", "feed_503", "feed_connect", "feed_502"]
    with pytest.raises(erp_feeds.ErpHttpError) as caught:
        await erp_feeds.sync_suppression(
            session, erp, mode="full", now=NOW + timedelta(hours=1)
        )
    assert caught.value.status == 502
    assert len(fake.requests) == 8
    assert await breaker(session) == (None, None)
    assert await raised(session) == {}
    assert (await config(session)).last_full_at == NOW


async def test_feed_400_is_feed_contract_and_never_retried(session, erp_row, monkeypatch):
    fake, erp = make(2)
    await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    fake.gets.clear()
    # An agent bug: a since without its zone.
    monkeypatch.setattr(erp_feeds, "fmt_since", lambda dt: "2026-10-24T05:00:00")
    with pytest.raises(erp_feeds.ErpHttpError) as caught:
        await erp_feeds.sync_suppression(
            session, erp, mode="delta", now=NOW + timedelta(hours=1)
        )
    assert (caught.value.status, caught.value.code) == (400, "bad_since")
    assert len(fake.gets) == 1
    assert (await raised(session))["feed_contract"]["detail"] == {
        "code": "bad_request",
        "feed": "suppression",
        "status": 400,
        "erp": "bad_since",
    }
    assert await breaker(session) == (None, None)


async def test_feed_runaway_same_cursor(session, erp_row, monkeypatch):
    monkeypatch.setattr(erp_feeds, "SUPPRESSION_LIMIT", 1)
    fake, erp = make(3)
    fake.feed_modes[PATH] = ["ok", "feed_same_cursor"]
    with pytest.raises(erp_feeds.FeedContract) as caught:
        await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert caught.value.code == "runaway"
    assert len(fake.gets) == 2
    assert (await raised(session))["feed_contract"]["detail"]["code"] == "runaway"
    # Nothing was deleted, the stored state did not move.
    assert await config(session) == SuppressionConfig()


async def test_more_pages_than_max_pages_is_a_runaway():
    fake, erp = make(5)
    pages = erp_feeds.fetch_pages(
        erp, PATH, params={"limit": 1}, model=erp_feeds.SuppressionPage, max_pages=3
    )
    got = []
    with pytest.raises(erp_feeds.FeedContract) as caught:
        async for page in pages:
            got.append(page)
    assert (caught.value.code, len(got)) == ("runaway", 3)


@pytest.mark.parametrize(
    "mode", ["feed_shape", "feed_badjson", "feed_missing_key", "feed_missing_key:entries"]
)
async def test_a_body_of_the_wrong_shape_is_feed_contract_shape(session, erp_row, mode):
    fake, erp = make(2)
    fake.feed_modes[PATH] = mode
    with pytest.raises(erp_feeds.FeedShapeError):
        await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert (await raised(session))["feed_contract"]["detail"] == {
        "code": "shape",
        "feed": "suppression",
    }
    assert await feed_rows(session) == {}


async def test_a_clean_walk_clears_its_own_feed_contract_only(session, erp_row):
    fake, erp = make(1)
    await health.raise_problem(
        session,
        "feed_contract",
        now=NOW,
        by="x",
        detail={"code": "shape", "feed": "outcomes"},
    )
    await session.commit()
    await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert "feed_contract" in await raised(session)
    await health.raise_problem(
        session, "feed_contract", now=NOW, by="x",
        detail={"code": "runaway", "feed": "suppression"},
    )  # fmt: skip
    await session.commit()
    await erp_feeds.sync_suppression(session, erp, mode="delta", now=NOW)
    assert "feed_contract" not in await raised(session)


async def test_a_200_ends_the_erp_breaker(session, erp_row):
    fake, erp = make(1)
    await session.execute(
        sa.update(Source)
        .where(Source.key == "erp_sync")
        .values(breaker_until=NOW + timedelta(hours=5), breaker_reason="push_404")
    )
    await session.commit()
    await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert await breaker(session) == (None, None)
    # Another breaker on the row is not the feed's to end.
    await session.execute(
        sa.update(Source)
        .where(Source.key == "erp_sync")
        .values(breaker_until=NOW + timedelta(hours=5), breaker_reason="host_blocked:x")
    )
    await session.commit()
    await erp_feeds.sync_suppression(session, erp, mode="delta", now=NOW)
    assert await breaker(session) == (NOW + timedelta(hours=5), "host_blocked:x")


# --- step 5 -----------------------------------------------------------------------


async def test_manual_import_retired_after_full_pull(session, erp_row):
    fake, erp = make(2)
    for t in (tail(0), tail(1), tail(7)):
        session.add(
            Suppression(
                kind="client", key_type="phone_tail", key=t, source="manual_import"
            )
        )
    await session.commit()
    report = await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert report.retired_manual == 2
    left = await session.scalars(
        sa.select(Suppression.key).where(Suppression.source == "manual_import")
    )
    # The tail the feed does not carry stays (fail closed).
    assert left.all() == [tail(7)]
    # A delta retires nothing.
    fake.add_entry(tail(7), at=NOW.astimezone(UTC))
    await erp_feeds.sync_suppression(session, erp, mode="delta", now=NOW)
    left = await session.scalars(
        sa.select(Suppression.key).where(Suppression.source == "manual_import")
    )
    assert left.all() == [tail(7)]


async def _won_push(session: AsyncSession, tail9: str, label: str = "won") -> None:
    company = await session.scalar(
        sa.text(
            "INSERT INTO companies (display_name, name_key) VALUES ('Namuna Savdo', "
            "'namuna savdo') RETURNING id"
        )
    )
    candidate = await session.scalar(
        sa.text("INSERT INTO lead_candidates (company_id) VALUES (:c) RETURNING id"),
        {"c": company},
    )
    external = f"cs:{company}:202610"
    await session.execute(
        sa.text(
            "INSERT INTO push_log (external_id, attempt, candidate_id, company_id, "
            "phone_tail, endpoint, source_key_sent, payload_sha256, status, http_status) "
            "VALUES (:e, 1, :cand, :c, :t, '/api/leads/in/agent', 'agent', :sha, "
            "'sent', 200)"
        ),
        {"e": external, "cand": candidate, "c": company, "t": tail9, "sha": "0" * 64},
    )
    await session.execute(
        sa.text(
            "INSERT INTO outcomes (external_id, intake_id, landing, erp_at, label, "
            "precision_class, raw) VALUES (:e, gen_random_uuid(), 'created', now(), "
            ":label, 'positive', '{}')"
        ),
        {"e": external, "label": label},
    )
    await session.commit()


async def test_the_self_test_finds_a_known_client_in_the_feed(session, erp_row):
    fake, erp = make(2)
    await _won_push(session, tail(1))
    report = await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert report.selftest is True
    assert "suppression_selftest_failed" not in await raised(session)


async def test_the_self_test_warns_when_a_known_client_is_absent(session, erp_row):
    fake, erp = make(2)
    await _won_push(session, tail(5))
    report = await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert (report.selftest, report.status) == (False, "ok")
    assert report.as_meta()["selftest"] is False
    assert (await raised(session))["suppression_selftest_failed"]["detail"] == {
        "salt_id": "9caf06bb"
    }
    # The pull is still applied.
    assert len(await feed_rows(session)) == 2
    fake.add_entry(tail(5), at=NOW.astimezone(UTC))
    report = await erp_feeds.sync_suppression(
        session, erp, mode="full", now=NOW + timedelta(days=1)
    )
    assert report.selftest is True
    assert "suppression_selftest_failed" not in await raised(session)


async def test_no_known_client_yet_is_no_self_test(session, erp_row):
    fake, erp = make(1)
    report = await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert report.selftest is None


async def test_a_completed_full_pull_clears_the_stale_hit(session, erp_row):
    fake, erp = make(1)
    await health.raise_problem(session, "suppression_stale_hit", now=NOW, by="x",
                               detail={"n": 1})  # fmt: skip
    await session.commit()
    await erp_feeds.sync_suppression(session, erp, mode="delta", now=NOW)
    # That delta had no anchor: it ran as the full pull.
    assert "suppression_stale_hit" not in await raised(session)
    await health.raise_problem(session, "suppression_stale_hit", now=NOW, by="x")
    await session.commit()
    await erp_feeds.sync_suppression(session, erp, mode="delta", now=NOW)
    assert "suppression_stale_hit" in await raised(session)


# --- the collector, the jobs, the fallback ---------------------------------------


def a_fetcher():
    return fetcher_for(Site(FakeClock()))


async def test_feeds_not_called_during_fallback(session, erp_row, monkeypatch):
    monkeypatch.setattr(settings, "crm_push_source", "other")
    fake, erp = make(2)
    for variant in ("full", "delta"):
        report = await erp_sync.run(variant, fetcher=a_fetcher(), now=NOW, erp=erp)
        assert (report.status, report.meta["note"]) == ("skipped", "feeds_not_configured")
    assert fake.requests == []
    assert await feed_rows(session) == {}
    monkeypatch.setattr(settings, "crm_push_source", "agent")
    monkeypatch.setattr(settings, "crm_agent_key", "  ")
    report = await erp_sync.run("full", fetcher=a_fetcher(), now=NOW, erp=erp)
    assert report.meta["note"] == "feeds_not_configured"
    assert fake.requests == []


async def test_a_run_beats_its_variant_and_records_the_report(session, erp_row):
    fake, erp = make(3)
    report = await erp_sync.run("full", fetcher=a_fetcher(), now=NOW, erp=erp)
    assert report.status == "ok"
    assert report.component == "collector:erp_sync.full"
    run = await session.scalar(
        sa.select(CollectorRun).execution_options(populate_existing=True)
    )
    assert run is not None
    assert (run.status, run.meta["variant"]) == ("ok", "full")
    assert run.meta["suppression"] == {
        "mode": "full",
        "walk": "full",
        "status": "ok",
        "pages": 1,
        "entries": 3,
        "seen": 3,
        "previous": 0,
        "deleted": 0,
        "retired_manual": 0,
        "salt_changed": False,
        "generated_at": "2026-10-23T21:29:00.000Z",
    }
    beat = await session.get(Heartbeat, "collector:erp_sync.full", populate_existing=True)
    assert beat is not None and beat.detail["status"] == "ok"
    source = await session.get(Source, "erp_sync", populate_existing=True)
    assert source is not None and source.last_run_at == NOW


async def test_a_failed_feed_fails_the_run_and_keeps_what_it_wrote(
    session, erp_row, told
):
    fake, erp = make(1)
    fake.rotate_secret("re-minted")
    report = await erp_sync.run("full", fetcher=a_fetcher(), now=NOW, erp=erp)
    assert (report.status, report.error) == ("failed", "ErpHttpError")
    assert report.meta["feed_error"] == {"error": "ErpHttpError", "status": 404}
    # The runner rolled its session back; the breaker had been committed.
    assert await breaker(session) == (NOW + timedelta(hours=6), "push_404")
    assert len(told) == 1


async def test_a_delta_is_skipped_while_a_full_pull_runs(session, erp_row):
    fake, erp = make(1)
    session.add(
        CollectorRun(
            source_key="erp_sync",
            started_at=NOW,
            status="running",
            meta={
                "variant": "full",
                "deadline_at": (NOW + timedelta(hours=2)).isoformat(),
            },
        )
    )
    await session.commit()
    report = await erp_sync.run("delta", fetcher=a_fetcher(), now=NOW, erp=erp)
    assert (report.status, report.meta["note"]) == ("skipped", "already_running")
    assert fake.requests == []


async def test_a_disabled_row_is_skipped(session, erp_row):
    await session.execute(
        sa.update(Source).where(Source.key == "erp_sync").values(enabled=False)
    )
    await session.commit()
    fake, erp = make(1)
    report = await erp_sync.run("full", fetcher=a_fetcher(), now=NOW, erp=erp)
    assert report.meta["note"] == "source_disabled"
    assert fake.requests == []


def test_the_jobs_are_03_s_cadences():
    assert [(j.id, j.cron, j.variant) for j in erp_sync.JOBS] == [
        ("erp_sync_delta", "15 * * * *", "delta"),
        ("erp_sync_full", "30 2 * * *", "full"),
    ]
    # The delta is the row's own cron (0001's seed, 02 §2.1).
    assert erp_sync.ErpSync.default_cron == "15 * * * *"


async def test_the_seeded_row_s_cron_is_the_delta_s(session):
    row = await session.get(Source, "erp_sync", populate_existing=True)
    assert row is not None and row.cron == erp_sync.JOBS[0].cron


# --- the client --------------------------------------------------------------------


async def test_the_client_sends_both_key_headers_and_no_key_in_the_url():
    fake, erp = make(1)
    await erp.get_feed(PATH, {"limit": 5})
    [got] = fake.gets
    assert got.headers["x-gsr-key"] == got.headers["x-erp-key"] == SECRET
    assert got.headers["accept-encoding"] == "gzip"
    assert got.headers["user-agent"].startswith("clientscrapper/")
    assert SECRET not in str(fake.requests[0].url)
    assert SECRET not in repr(erp)


def test_an_empty_key_is_refused():
    with pytest.raises(ValueError):
        ErpClient(BASE, " ")


def test_fmt_since_is_utc_with_z():
    assert erp_feeds.fmt_since(NOW) == "2026-10-23T21:30:00.000000Z"
    assert erp_feeds.fmt_since(datetime(2026, 1, 1, 0, 0, 0, 7, tzinfo=UTC)) == (
        "2026-01-01T00:00:00.000007Z"
    )
    with pytest.raises(ValueError):
        erp_feeds.fmt_since(datetime(2026, 1, 1))
    # The fake ERP reads it back to the same instant.
    assert fake_erp.parse_since(erp_feeds.fmt_since(NOW)) == "2026-10-23T21:30:00.000000Z"


# --- the owner's tool (06 §4.6) --------------------------------------------------------


def test_the_tool_takes_one_walk_and_accept_shrink_only_with_full():
    from clientscrapper.tools import erp_sync as tool

    args = tool.parse_args(["--full", "--accept-shrink"])
    assert (args.full, args.delta, args.accept_shrink) == (True, False, True)
    assert tool.parse_args(["--delta"]).delta
    for bad in ([], ["--full", "--delta"], ["--delta", "--accept-shrink"]):
        with pytest.raises(SystemExit):
            tool.parse_args(bad)


async def test_the_tool_s_line_says_what_the_run_did(session, erp_row):
    from clientscrapper.tools import erp_sync as tool

    fake, erp = make(3)
    report = await erp_sync.run("full", fetcher=a_fetcher(), now=NOW, erp=erp)
    assert tool.describe(report) == (
        "erp_sync.full: ok · walk=full status=ok entries=3 seen=3 deleted=0"
    )
    await session.execute(
        sa.update(Source).where(Source.key == "erp_sync").values(enabled=False)
    )
    await session.commit()
    skipped = await erp_sync.run("full", fetcher=a_fetcher(), now=NOW, erp=erp)
    assert tool.describe(skipped) == "erp_sync.full: skipped (source_disabled)"


async def test_accept_shrink_reaches_the_pull_through_the_run(session, erp_row):
    fake, erp = make(10)
    await erp_sync.run("full", fetcher=a_fetcher(), now=NOW, erp=erp)
    del fake.entries[5:]
    later = NOW + timedelta(days=1)
    shrunk = await erp_sync.run("full", fetcher=a_fetcher(), now=later, erp=erp)
    assert shrunk.meta["suppression"]["status"] == "shrunk"
    accepted = await erp_sync.run(
        "full", fetcher=a_fetcher(), now=later + timedelta(hours=1), erp=erp,
        accept_shrink=True,
    )  # fmt: skip
    assert (
        accepted.meta["suppression"]["status"],
        accepted.meta["suppression"]["deleted"],
    ) == (
        "ok",
        5,
    )


@pytest.mark.parametrize("status", [204, 304])
async def test_a_status_the_feed_never_sends_is_shape(session, erp_row, status):
    def odd(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    erp = ErpClient(BASE, SECRET, transport=httpx.MockTransport(odd), sleep=no_sleep)
    with pytest.raises(erp_feeds.ErpHttpError):
        await erp_feeds.sync_suppression(session, erp, mode="full", now=NOW)
    assert (await raised(session))["feed_contract"]["detail"]["code"] == "shape"
    assert await breaker(session) == (None, None)
