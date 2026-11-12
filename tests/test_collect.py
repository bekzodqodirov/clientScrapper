"""``make collect`` (06 §3.3, ``tools/collect.py``): one collector run now.

What is pinned:

* the sources it takes are the worker's built collector slots, read from
  ``JOB_TABLE`` — today the directories and the exhibitors — and a slot a
  later task fills is offered the day it is filled;
* every refusal is one Uzbek sentence and exit 1, with no traceback and
  nothing written: an unknown source, one not built yet, the Telegram
  reader's, the ERP feeds', FULL=1, a source switched off on /manbalar, a
  paused one (until when, Tashkent time, and why), the directories with no
  site of their own switched on, ``/toxtat``, COLLECTORS_ENABLED=false, and
  a run already going (the runner's own one-running claim decides it);
* a switched-on source runs the slot's own ``job`` — the very function the
  scheduler registers for it, ``collector_job(<Collector>)`` — in this
  process, with no leader lock taken, no run reclaimed, no catch-up;
* the summary is the run's own ``collector_runs`` row: its counters, and per
  site for the directories (``meta.subsources`` / ``meta.sources``), per list
  for the exhibitors (``meta.lists``) — end to end on the recorded directory
  pages too; exit 0 for a finished run, 2 for a failed one.

``sources`` is seeded configuration: every row a test switches is put back
(07 §1.0).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa

from clientscrapper.collectors import directories, runner
from clientscrapper.collectors.directories import Directories
from clientscrapper.collectors.runner import RunReport
from clientscrapper.config import settings
from clientscrapper.db.models import CollectorRun, Observation, RuntimeFlag, Source
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import costs, reviews
from clientscrapper.services.http import Fetcher
from clientscrapper.tools import collect
from clientscrapper.worker import main as worker
from tests.collectors.test_directories_crawl import THURSDAY, Web
from tests.fakes import FakeClock, fetcher_for
from tests.test_uz_text import problems

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 11, 12, 15, 0, tzinfo=TZ)  # a Thursday, not a full night
ALLOWED = "directories, exhibitors"
SWITCHED = (
    "directories",
    "directories_goldenpages",
    "directories_yellowpages",
    "directories_glotr",
    "directories_prom",
    "directories_topuz",
    "exhibitors",
)
KEPT = (
    "enabled",
    "paused_until",
    "paused_reason",
    "breaker_until",
    "breaker_reason",
    "daily_page_budget",
    "last_run_at",
)
FULL_REFUSED = (
    "FULL=1 hozircha yoʻq: kataloglarning toʻliq yurishini worker oʻzi qiladi — "
    "har oyning 1–7-kunlari soat 23:00 da, har kecha bitta sayt (qaysi saytligini "
    "sana belgilaydi). Saytni qoʻlda tanlash keyinroq quriladi. Hozir yangi "
    "kompaniyalar uchun: make collect SOURCE=directories."
)
NEXT = (
    "➡️ Topilgan kompaniyalarni ishlab turgan worker oʻz jadvali boʻyicha tekshiradi "
    "va baholaydi; kartalar botga dushanba–shanba 09:00–18:00 oraligʻida keladi, "
    "kuniga koʻpi bilan 40 ta."
)


# --- which sources (the worker's table) ------------------------------------------


async def _noop() -> None:
    return None


def test_make_collect_takes_the_tables_built_collectors_and_nothing_else():
    table = worker.job_table()
    assert collect.allowed(table) == ("directories", "exhibitors")
    offered = collect.allowed(table)
    for slot in table:
        if collect.is_collector(slot) and slot.primary:
            # Offered, or named as another process's (the reader) or command's.
            assert (slot.id in offered) != (slot.source_key in collect.ELSEWHERE), slot
    # Derived, not listed: a slot a later task fills is offered that day.
    filled = tuple(replace(s, job=_noop) if s.id == "hh_vacancies" else s for s in table)
    assert collect.allowed(filled) == ("directories", "hh_vacancies", "exhibitors")
    assert collect.choose("hh_vacancies", table=filled) == next(
        s for s in filled if s.id == "hh_vacancies"
    )


REFUSALS = [
    (
        [],
        "Qaysi manbani yigʻay? make collect SOURCE=<manba> deb yozing. "
        f"Hozir mumkin: {ALLOWED}.",
    ),
    (
        [""],
        "Qaysi manbani yigʻay? make collect SOURCE=<manba> deb yozing. "
        f"Hozir mumkin: {ALLOWED}.",
    ),
    (["nope"], f"«nope» degan manba yoʻq. Hozir mumkin: {ALLOWED}."),
    (["backup"], f"«backup» degan manba yoʻq. Hozir mumkin: {ALLOWED}."),
    (
        ["hh_vacancies"],
        "hh_vacancies hali qurilmagan — bu manbadan yigʻish keyinroq quriladi. "
        f"Hozir mumkin: {ALLOWED}.",
    ),
    (
        ["olx_business_full"],
        "olx_business_full hali qurilmagan — bu manbadan yigʻish keyinroq quriladi. "
        f"Hozir mumkin: {ALLOWED}.",
    ),
    (
        ["telegram_monitor"],
        "Telegram guruhlarini userbot oʻqiydi, make collect emas: u doim oʻzi "
        "ishlab turadi (chatlar: botda /chats).",
    ),
    (
        ["erp_sync_delta"],
        "ERP roʻyxatlari make erp-sync bilan yangilanadi, make collect emas.",
    ),
    (["directories", "full"], FULL_REFUSED),
    (["directories_full"], FULL_REFUSED),
    (
        ["exhibitors", "full"],
        "exhibitors uchun «full» yurishi yoʻq — FULL=1 siz ishga tushiring: "
        "make collect SOURCE=exhibitors.",
    ),
]


@pytest.mark.parametrize(("argv", "line"), REFUSALS, ids=lambda x: str(x)[:30])
def test_a_refusal_the_table_decides_is_one_sentence_and_exit_1(argv, line, capsys):
    """Through the worker's own CLI, as `make collect` calls it; decided
    before any connection is opened (no database here)."""
    assert worker.main(["collect", *argv]) == collect.EXIT_REFUSED
    out, err = capsys.readouterr()
    assert out == line + "\n"
    assert "Traceback" not in err
    assert problems(line, rendered=True) == set()


def test_full_is_refused_because_the_full_crawl_picks_its_site_by_the_night():
    """The premise of NO_MANUAL (03 §2.6): the ``full`` variant crawls the
    site whose night it is, the 1st–7th, and on any other day nothing in
    full — there is no site to choose by hand."""
    for day in range(1, 32):
        now = datetime(2026, 10, day, 15, 0, tzinfo=TZ)
        full = [
            sub.site
            for sub, mode in directories.plan("full", now, diff_due=False)
            if mode == "full"
        ]
        if day in directories.FULL_NIGHTS:
            assert full == [s.site for s in directories.SUBSOURCES if s.full_night == day]
            assert full, day
        else:
            assert full == [], day
    assert collect.NO_MANUAL == {"directories_full": FULL_REFUSED}


# --- the summary, rendered from a run row -----------------------------------------

START = datetime(2026, 11, 12, 15, 0, tzinfo=TZ)
TITLES = {
    "directories": "Kataloglar",
    "directories_goldenpages": "goldenpages.uz",
    "directories_yellowpages": "yellowpages.uz",
    "directories_topuz": "top.uz",
    "directories_glotr": "glotr.uz",
    "directories_prom": "prom.uz",
    "exhibitors": "Koʻrgazmalar",
}


def site(**figures: Any) -> dict[str, Any]:
    keys = ("seeds", "pages", "cards", "rows", "held_rows", "known", "card_missing")
    keys += ("card_errors", "dropped", "parse_errors", "robots")
    return {
        "mode": "diff",
        **dict.fromkeys(keys, 0),
        "stops": {},
        "list_status": {},
        **figures,
    }


DIRECTORIES_META: dict[str, Any] = {
    "mode": "diff",
    "unchanged": 0,
    "skipped_robots": 3,
    "subsources": {
        "goldenpages": site(pages=2, cards=1, rows=27, known=4, list_status={"404": 5}),
        "yellowpages": site(pages=2, rows=16, known=0, held_rows=9),
        "topuz": {"skipped": "disabled"},
        "glotr": site(pages=1, cards=1, robots=3, stops={"transport": 1}),
        "prom": {"skipped": "not_built"},
        "uzts": {"skipped": "not_built"},
        "openinfo": {"skipped": "not_built"},
        "uzpharm": {"skipped": "not_built"},
    },
    "sources": {
        "directories_goldenpages": {"evidence": 20, "observations": 28},
        "directories_yellowpages": {"evidence": 2, "observations": 16},
    },
    "incomplete": ["glotr: 5 failed requests in a row (transport), the rest not read"],
}
DIRECTORIES_SUMMARY = [
    "✅ Kataloglar (directories) — yigʻish tugadi, lekin hammasi oʻqilmadi; "
    "12 daqiqa 4 soniya davom etdi (yurish #41).",
    "Oʻqildi: 87 sahifa (92 soʻrov). Yangi yozuvlar: 44 ta.",
    "• goldenpages.uz (yangilari): 2 roʻyxat sahifasi, 1 kompaniya kartasi, "
    "27 roʻyxat qatori; avval koʻrilgan 4 ta, yangi yozuv 28 ta.",
    "  ⚠️ roʻyxat javoblari: 404×5.",
    "• yellowpages.uz (yangilari): 2 roʻyxat sahifasi, 0 kompaniya kartasi, "
    "16 roʻyxat qatori; avval koʻrilgan 0 ta, yangi yozuv 16 ta.",
    "• top.uz: oʻtkazildi — /manbalar da oʻchiq.",
    "• glotr.uz (yangilari): 1 roʻyxat sahifasi, 1 kompaniya kartasi, "
    "0 roʻyxat qatori; avval koʻrilgan 0 ta, yangi yozuv 0 ta.",
    "  ⚠️ robots.txt taqiqladi: 3 · sayt javob bermay qoldi.",
    "• Hali qurilmagan saytlar (oʻtkazildi): prom.uz, uzts.uz, openinfo.uz, "
    "uzpharm-control.uz.",
    "⚠️ robots.txt taqiqlagani uchun 3 ta sahifa oʻqilmadi.",
    "⚠️ 1 ta roʻyxat yoki sayt toʻliq oʻqilmadi (tafsiloti yurish #41 da).",
    NEXT,
]


def a_run(**given: Any) -> CollectorRun:
    values: dict[str, Any] = {
        "id": 41,
        "source_key": "directories",
        "started_at": START,
        "finished_at": START + timedelta(minutes=12, seconds=4),
        "status": "partial",
        "error": None,
        "pages": 87,
        "requests": 92,
        "new_observations": 44,
        "meta": DIRECTORIES_META,
    }
    return CollectorRun(**{**values, **given})


def test_the_summary_names_each_directory_site_from_the_run_s_meta():
    lines = collect.render(a_run(), title="Kataloglar", titles=TITLES)
    assert lines == DIRECTORIES_SUMMARY
    assert [x for x in lines if problems(x, rendered=True)] == []


def test_the_summary_names_each_exhibitor_list_and_what_cut_the_run():
    meta = {
        "unchanged": 0,
        "skipped_robots": 0,
        "lists": [
            {
                "show": "caitme",
                "year": 2026,
                "why": "window",
                "status": "complete",
                "rows": 412,
                "kept": 57,
                "foreign": 355,
                "pages": 9,
            },
            {
                "show": "textileexpo",
                "year": 2025,
                "why": "archive",
                "status": "failed",
                "rows": 0,
                "kept": 0,
                "pages": 0,
                "error": "status_503",
            },
        ],
        "calendar": ["entry 3: no end_date"],
        "exhausted": "pages",
        "blocked_hosts": ["ite-expo.uz"],
        "captcha": 2,
        "dropped": {"bad_observation": 1},
    }
    run = a_run(
        source_key="exhibitors",
        status="failed",
        error="host_blocked",
        pages=11,
        requests=13,
        new_observations=57,
        finished_at=START + timedelta(hours=1, minutes=3),
        meta=meta,
    )
    lines = collect.render(run, title="Koʻrgazmalar", titles=TITLES)
    assert lines == [
        "❌ Koʻrgazmalar (exhibitors) — yigʻish xato bilan tugadi: sayt toʻsdi "
        "(403/429); 1 soat 3 daqiqa davom etdi (yurish #41).",
        "Oʻqildi: 11 sahifa (13 soʻrov). Yangi yozuvlar: 57 ta.",
        "• caitme 2026: toʻliq oʻqildi — 57 ta oʻzbek kompaniya (412 qatordan), "
        "9 sahifa.",
        "• textileexpo 2025: oʻqib boʻlmadi (status_503) — 0 ta oʻzbek kompaniya "
        "(0 qatordan), 0 sahifa, arxiv.",
        "⚠️ Koʻrgazmalar kalendarida 1 ta notoʻgʻri yozuv bor "
        "(sources.exhibitors.config.calendar).",
        "⚠️ Bugungi sahifa limiti tugadi — qolgani keyingi yurishda oʻqiladi.",
        "⛔ Sayt toʻsdi (403/429): ite-expo.uz — manba 6 soatga pauzaga qoʻyildi.",
        "⚠️ CAPTCHA sahifalari: 2 ta (oʻqilmadi).",
        "⚠️ Baza 1 ta yozuvni qabul qilmadi.",
        NEXT,
    ]
    assert collect.exit_code(run.status) == collect.EXIT_FAILED
    assert [x for x in lines if problems(x, rendered=True)] == []


def test_a_run_with_no_list_this_week_says_so_and_a_failed_empty_run_says_no_next():
    quiet = a_run(source_key="exhibitors", status="ok", meta={"lists": []}, pages=0)
    lines = collect.render(quiet, title="Koʻrgazmalar", titles=TITLES)
    assert lines[2] == (
        "• Bu hafta oʻqiladigan koʻrgazma roʻyxati yoʻq: oynasi ochiq koʻrgazma yoʻq, "
        "arxivlar avval oʻqilgan."
    )
    assert lines[-1] == NEXT
    assert collect.exit_code("ok") == collect.exit_code("partial") == collect.EXIT_DONE
    failed = a_run(status="failed", error="ConnectError", new_observations=0, meta={})
    out = collect.render(failed, title="Kataloglar", titles=TITLES)
    assert out[0].startswith("❌ Kataloglar (directories) — yigʻish xato bilan tugadi:")
    assert "ConnectError" in out[0]
    assert NEXT not in out


def test_the_next_line_reads_review_hours_and_the_daily_cap(monkeypatch):
    """06 §1.6: REVIEW_HOURS on Mon–Sat (``reviews.REVIEW_DAYS``, the words
    «dushanba–shanba»), at most REVIEW_DAILY_CAP owner cards a day."""
    assert frozenset(range(1, 7)) == reviews.REVIEW_DAYS
    monkeypatch.setattr(settings, "review_hours", "10:00-17:30")
    monkeypatch.setattr(settings, "review_daily_cap", 20)
    assert collect.next_line() == (
        "➡️ Topilgan kompaniyalarni ishlab turgan worker oʻz jadvali boʻyicha "
        "tekshiradi va baholaydi; kartalar botga dushanba–shanba 10:00–17:30 "
        "oraligʻida keladi, kuniga koʻpi bilan 20 ta."
    )


# --- against the database -------------------------------------------------------


@pytest.fixture
async def sources(session) -> AsyncIterator[None]:
    """The directories, their sites and the exhibitors switched off and
    unpaused for the test; every column put back after."""
    async with SessionLocal() as s:
        rows = await s.scalars(sa.select(Source).where(Source.key.in_(SWITCHED)))
        snapshot = {r.key: {c: getattr(r, c) for c in KEPT} for r in rows}
        await s.execute(
            sa.update(Source)
            .where(Source.key.in_(SWITCHED))
            .values(enabled=False, paused_until=None, paused_reason=None)
        )
        await s.commit()
    yield
    async with SessionLocal() as s:
        for key, values in snapshot.items():
            await s.execute(sa.update(Source).where(Source.key == key).values(**values))
        await s.commit()


async def switch(key: str, **values: Any) -> None:
    async with SessionLocal() as s:
        await s.execute(sa.update(Source).where(Source.key == key).values(**values))
        await s.commit()


def slot(key: str) -> worker.JobSlot:
    chosen = collect.choose(key)
    assert isinstance(chosen, worker.JobSlot), chosen
    return chosen


@pytest.fixture
def never_runs(monkeypatch) -> list[str]:
    """Fails the test if anything would run: the job, the leader lock, the
    reclaim of a dead worker's runs, the catch-up."""
    touched: list[str] = []

    async def must_not(*args: Any, **kwargs: Any) -> None:
        touched.append("run")
        raise AssertionError("nothing may run on a refusal")

    monkeypatch.setattr(worker, "collect_now", must_not)
    return touched


async def runs(session) -> list[tuple[str, str, Any]]:
    rows = await session.execute(
        sa.select(CollectorRun.source_key, CollectorRun.status, CollectorRun.meta)
        .order_by(CollectorRun.id)
        .execution_options(populate_existing=True)
    )
    return [(k, s, m.get("note")) for k, s, m in rows.all()]


async def test_a_switched_off_source_is_refused_and_nothing_is_written(
    session, sources, never_runs
):
    out = await collect.collect(slot("exhibitors"), now=NOW)
    assert out == collect.Outcome(
        (
            "Koʻrgazmalar oʻchiq — avval botda /manbalar → Koʻrgazmalar ni yoqing, "
            "keyin qaytadan make collect SOURCE=exhibitors.",
        ),
        collect.EXIT_REFUSED,
    )
    assert never_runs == []
    assert await runs(session) == []


async def test_directories_switched_off_with_no_site_on_names_the_site_rows_too(
    session, sources, never_runs
):
    out = await collect.collect(slot("directories"), now=NOW)
    assert out.lines == (
        "Kataloglar oʻchiq — avval botda /manbalar → Kataloglar ni yoqing, keyin "
        "qaytadan make collect SOURCE=directories. Saytlaridan ham kamida bittasini "
        "yoqing: goldenpages.uz oʻchiq, yellowpages.uz oʻchiq, top.uz oʻchiq, "
        "glotr.uz oʻchiq.",
    )
    assert out.code == collect.EXIT_REFUSED
    assert await runs(session) == []


async def test_the_directories_alone_are_refused_until_a_site_of_theirs_is_on(
    session, sources, never_runs
):
    """03 §2.6: the fan-out reads a site only while its own row is on and not
    paused — the parent alone crawls nothing; prom has no parser."""
    await switch("directories", enabled=True)
    await switch("directories_prom", enabled=True)
    await switch(
        "directories_glotr",
        enabled=True,
        paused_until=NOW + timedelta(days=7),
        paused_reason="low_precision",
    )
    out = await collect.collect(slot("directories"), now=NOW)
    assert out.lines == (
        "Kataloglar yoqilgan, lekin saytlaridan hech biri ishlamaydi: goldenpages.uz "
        "oʻchiq, yellowpages.uz oʻchiq, top.uz oʻchiq, glotr.uz 19.11 15:00 gacha "
        "pauzada. Avval botda /manbalar → kamida bittasini yoqing (yolgʻiz "
        "«Kataloglar» hech narsa oʻqimaydi), keyin qaytadan make collect "
        "SOURCE=directories.",
    )
    assert out.code == collect.EXIT_REFUSED
    assert never_runs == []
    assert await runs(session) == []


@pytest.mark.parametrize(
    ("reason", "why"),
    [
        (
            "host_blocked:www.goldenpages.uz",
            "sayt (www.goldenpages.uz) 403/429 bilan javob berdi, agent 6 soat kutadi",
        ),
        (
            "low_precision",
            "aniqligi past — botda /manbalar → Koʻrgazmalar (⏸) ni bir marta "
            "bossangiz pauza tugaydi",
        ),
    ],
)
async def test_a_paused_source_says_until_when_in_tashkent_time_and_why(
    session, sources, never_runs, reason, why
):
    until = datetime(2026, 11, 12, 16, 30, tzinfo=ZoneInfo("UTC"))  # 21:30 Tashkent
    await switch("exhibitors", enabled=True, paused_until=until, paused_reason=reason)
    out = await collect.collect(slot("exhibitors"), now=NOW)
    assert out.lines == (
        f"Koʻrgazmalar 12.11 21:30 gacha toʻxtatilgan (Toshkent vaqti): {why}. "
        "Shundan keyin qaytadan urinib koʻring.",
    )
    assert out.code == collect.EXIT_REFUSED
    assert await runs(session) == []


async def test_toxtat_and_collectors_disabled_are_said_before_the_source(
    session, sources, never_runs, monkeypatch
):
    await switch("exhibitors", enabled=True)
    session.add(RuntimeFlag(key=costs.PAUSE_FLAG, value=True, updated_by="owner"))
    await session.commit()
    out = await collect.collect(slot("exhibitors"), now=NOW)
    assert out.lines == (
        "Agent /toxtat bilan toʻxtatilgan — avval botda /yoq bilan davom ettiring, "
        "keyin qaytadan make collect.",
    )
    monkeypatch.setattr(settings, "collectors_enabled", False)
    out = await collect.collect(slot("exhibitors"), now=NOW)
    assert out.lines == (
        ".env da COLLECTORS_ENABLED=false — hamma yigʻish oʻchiq. Uni true qilib "
        "make reload, keyin qaytadan make collect.",
    )
    assert out.code == collect.EXIT_REFUSED
    assert await runs(session) == []


def frozen_at(moment: datetime) -> type[datetime]:
    """``datetime`` whose ``now`` is ``moment``: the clock ``collector_job``
    and the runner read, so a test's run is judged at the test's instant."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Frozen:  # type: ignore[override]
            return cls.fromtimestamp(moment.timestamp(), tz)

    return Frozen


def refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"a test reached the network: {request.url.host}")


@pytest.fixture(autouse=True)
def offline(monkeypatch) -> None:
    """No request leaves this file: the worker's shared Fetcher is one whose
    every request fails the test (the end-to-end test hands the recorded
    pages in its place)."""
    monkeypatch.setattr(
        worker, "shared_fetcher", lambda: Fetcher(transport=httpx.MockTransport(refuse))
    )


@pytest.fixture
def no_leadership(monkeypatch) -> None:
    """A manual run never touches the scheduler's lock, a dead worker's runs
    or the catch-up: each raises here."""

    def lock(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("make collect took the leader lock")

    async def reclaim(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("make collect reclaimed the running worker's runs")

    async def catch(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("make collect caught the scheduler's jobs up")

    monkeypatch.setattr(worker, "Leadership", lock)
    monkeypatch.setattr(worker, "reclaim_runs", reclaim)
    monkeypatch.setattr(worker, "catch_up", catch)


async def test_a_run_already_going_is_the_runner_s_own_claim(
    session, sources, no_leadership, monkeypatch
):
    """The runner's one-running claim decides: the open row (the scheduler's,
    started 14:10, due 16:10 — not stale at 15:00) stays as it is, and the
    manual run is told to wait."""
    monkeypatch.setattr(worker, "datetime", frozen_at(NOW))
    await switch("directories", enabled=True)
    await switch("directories_goldenpages", enabled=True)
    started = datetime(2026, 11, 12, 14, 10, tzinfo=TZ)
    session.add(
        CollectorRun(
            source_key="directories",
            started_at=started,
            status="running",
            meta={"deadline_at": (started + timedelta(hours=2)).isoformat()},
        )
    )
    await session.commit()
    out = await collect.collect(slot("directories"), now=NOW)
    assert out == collect.Outcome(
        (
            "Kataloglar hozir yigʻilmoqda (12.11 14:10 da boshlangan) — tugashini "
            "kuting va keyinroq qayta urinib koʻring.",
        ),
        collect.EXIT_REFUSED,
    )
    assert await runs(session) == [
        ("directories", "running", None),
        ("directories", "skipped", "already_running"),
    ]
    assert await session.scalar(sa.select(sa.func.count(Observation.id))) == 0


async def test_a_switched_on_source_runs_the_job_the_scheduler_registers(
    session, sources, no_leadership, monkeypatch
):
    """The slot's own job, the very object ``build_scheduler`` registers for
    ``directories`` — ``collector_job(Directories)``, no variant, the shared
    Fetcher — and the summary is the row its run wrote."""
    await switch("directories", enabled=True)
    await switch("directories_goldenpages", enabled=True)
    row = a_run(id=None, meta=DIRECTORIES_META)
    session.add(row)
    await session.commit()
    run_id = row.id
    ran: list[tuple[type, str | None, object]] = []

    async def run_collector(cls, *, fetcher, now, variant=None, **kw):
        ran.append((cls, variant, fetcher))
        return RunReport("directories", "collector:directories", "partial", run_id)

    monkeypatch.setattr(worker, "run_collector", run_collector)
    called: list[str] = []

    def spied(slot_: worker.JobSlot) -> worker.JobSlot:
        job = slot_.job
        assert job is not None

        async def spy() -> Any:
            called.append(slot_.id)
            return await job()

        return replace(slot_, job=spy)

    table = tuple(spied(s) if collect.is_collector(s) else s for s in worker.job_table())
    monkeypatch.setattr(worker, "job_table", lambda: table)
    scheduled = worker.build_scheduler(table).get_job("directories")
    chosen = collect.choose("directories")
    assert isinstance(chosen, worker.JobSlot)
    assert scheduled is not None and scheduled.func is chosen.job

    said: list[str] = []
    out = await collect.collect(chosen, now=NOW, say=said.append)
    assert called == ["directories"]
    assert [(cls, variant) for cls, variant, _ in ran] == [(Directories, None)]
    assert isinstance(ran[0][2], Fetcher)
    assert worker._shared.fetcher is None  # closed after the run
    assert said == [
        "▶️ Kataloglar (directories) yigʻilmoqda… Saytlar sekin oʻqiladi (har biriga "
        "hurmat bilan): bir necha daqiqadan bir-ikki soatgacha ketishi mumkin. "
        "Toʻxtatish: Ctrl-C."
    ]
    expected = tuple(x.replace("#41", f"#{run_id}") for x in DIRECTORIES_SUMMARY)
    assert out == collect.Outcome(expected, collect.EXIT_DONE)


async def test_the_summary_is_read_off_the_run_s_own_row(session):
    row = a_run(id=None, status="ok", meta=DIRECTORIES_META)
    session.add(row)
    await session.commit()
    out = await collect.summary(row.id)
    assert out.code == collect.EXIT_DONE
    assert out.lines[0] == (
        f"✅ Kataloglar (directories) — yigʻish tugadi, 12 daqiqa 4 soniya davom etdi "
        f"(yurish #{row.id})."
    )
    assert out.lines[2:4] == tuple(DIRECTORIES_SUMMARY[2:4])


async def test_end_to_end_the_recorded_directories_say_each_site(
    session, sources, no_leadership, monkeypatch, tmp_path
):
    """The real job, the real runner and Directories over the recorded pages
    (``tests/collectors/test_directories_crawl``'s ``Web``): two sites on,
    two off. The per-site lines carry what the run's meta says — goldenpages'
    2 list pages, 1 card and 28 new rows are the crawl test's own figures."""
    for key in ("directories", "directories_goldenpages", "directories_yellowpages"):
        await switch(key, enabled=True)
    clock = FakeClock(THURSDAY)
    web = Web(clock)
    monkeypatch.setattr(worker, "shared_fetcher", lambda: fetcher_for(web.site))
    monkeypatch.setattr(worker, "datetime", frozen_at(THURSDAY))
    monkeypatch.setattr(runner, "datetime", frozen_at(THURSDAY))
    monkeypatch.setattr(settings, "evidence_dir", str(tmp_path))
    out = await collect.collect(slot("directories"), now=THURSDAY)
    assert out.code == collect.EXIT_DONE, out
    run = await session.scalar(
        sa.select(CollectorRun).execution_options(populate_existing=True)
    )
    assert run is not None and run.status == "ok"
    meta = run.meta
    gold = meta["subsources"]["goldenpages"]
    assert (gold["pages"], gold["cards"]) == (2, 1)
    assert meta["sources"]["directories_goldenpages"]["observations"] == 28
    lines = list(out.lines)
    assert lines[0] == (
        f"✅ Kataloglar (directories) — yigʻish tugadi, 0 soniya davom etdi "
        f"(yurish #{run.id})."
    )
    assert lines[1] == (
        f"Oʻqildi: {run.pages} sahifa ({run.requests} soʻrov). "
        f"Yangi yozuvlar: {run.new_observations} ta."
    )
    assert lines[2:] == [
        "• goldenpages.uz (yangilari): 2 roʻyxat sahifasi, 1 kompaniya kartasi, "
        "27 roʻyxat qatori; avval koʻrilgan 0 ta, yangi yozuv 28 ta.",
        "  ⚠️ roʻyxat javoblari: 404×5.",
        "• yellowpages.uz (yangilari): 2 roʻyxat sahifasi, 0 kompaniya kartasi, "
        "16 roʻyxat qatori; avval koʻrilgan 0 ta, yangi yozuv 16 ta.",
        "  ⚠️ roʻyxat javoblari: 404×8.",
        "• top.uz: oʻtkazildi — /manbalar da oʻchiq.",
        "• glotr.uz: oʻtkazildi — /manbalar da oʻchiq.",
        "• Hali qurilmagan saytlar (oʻtkazildi): prom.uz, uzts.uz, openinfo.uz, "
        "uzpharm-control.uz.",
        NEXT,
    ]
    # Each figure is the run row's own (not a count of the tool's).
    yellow = meta["subsources"]["yellowpages"]
    assert (yellow["pages"], yellow["rows"], yellow["list_status"]) == (2, 16, {"404": 8})
    assert (gold["rows"], gold["known"], gold["list_status"]) == (27, 0, {"404": 5})
    assert run.new_observations == 28 + 16
