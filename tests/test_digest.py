"""The owner's digest (06 §1.8, 07 §4.1 T-35): ``render_digest`` renders
≤ 4,096 characters from fixtures and lists the skipped live tests; the
loader reads each number from the table that holds it (synthetic rows); it
is said once per Tashkent day, to the owner only, and a delivery that fails
leaves the day's digest due."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import (
    ChatMonitor,
    CollectorRun,
    Interaction,
    LeadReview,
    Outcome,
    ReminderLog,
    RuntimeFlag,
    Source,
    Suppression,
)
from clientscrapper.services import digest, health, smoke
from tests import gate_fixtures as gf
from tests.fixtures import digest_views as dv
from tests.test_cards import BUBBLE_PX, width_px
from tests.test_uz_text import problems as uz_problems

NOW = dv.NOW  # Thursday 29.10.2026 09:00 Tashkent
YESTERDAY = NOW - timedelta(days=1)


# --- rendering ----------------------------------------------------------------------


@pytest.mark.parametrize("make", dv.VIEWS, ids=lambda f: f.__name__)
def test_the_digest_fits_one_message_and_follows_the_text_rules(make):
    text = digest.render_digest(make())
    assert len(text) <= digest.MESSAGE_MAX == 4096
    assert uz_problems(text, rendered=True) == set()
    # Line 1 on a 360 px phone (07 §3.1), measured as test_cards does.
    first = text.splitlines()[0]
    assert first == "☀️ 29.10.2026 — Qidiruv agenti"
    assert width_px(first) <= BUBBLE_PX


def test_the_longest_digest_still_has_room():
    # Every list at its cap: the caps, not the clip, keep it in one message.
    assert len(digest.render_digest(dv.longest())) <= 3600


def test_a_quiet_morning_says_only_the_always_said_lines():
    assert digest.render_digest(dv.quiet()).splitlines() == [
        "☀️ 29.10.2026 — Qidiruv agenti",
        "Kecha: topildi 0 · ball olgan 0 · karta 0 · tasdiq 0 · CRMʼga 0/10 · rad 0",
        "Navbat: koʻrib chiqish 0 · boyitish 0 · kutish 0 · menejerda 0",
        "Xarajat: kecha $0.42 · oy $3.10/$250.00 (1%)",
        "Tizim: hammasi joyida",
    ]


def test_a_busy_morning_is_06_s_layout():
    lines = digest.render_digest(dv.busy()).splitlines()
    assert lines == [
        "☀️ 29.10.2026 — Qidiruv agenti",
        "Kecha: topildi 37 · ball olgan 21 · karta 12 · tasdiq 7 · CRMʼga 6/10 · rad 3",
        "Navbat: koʻrib chiqish 9 (eng eskisi 1 kun) · boyitish 4 · kutish 11 · "
        "menejerda 2",
        "ERP (30 kun): bogʻlanildi 67% · yutuq 3 (birinchi yuk oʻrniga, taxminiy) · "
        "yoʻqotildi 9 (shundan «Qayta qilinmasin» 2, notoʻgʻri raqam 1)",
        "ERP qabul qilmadi: chegara 1 · bostirilgan 2",
        "⚠️ Sinflanmagan yoʻqotish sabablari: «Mijoz oʻzi olib ketdi» 2 ta — "
        "dasturchiga ayting, aniqlik hisobiga qoʻshilsin",
        "Manbalar:",
        "  hh.uz vakansiyalari 5 yangi · 62% (18) ✅",
        "  OLX sotuvchilari 0 yangi · — (0) ⏸",
        "  Telegram chatlari 12 yangi · 41% (22) ✅",
        "🔁 Avtomatik push yoqildi: hh.uz vakansiyalari",
        "Menejerlar kecha: bogʻlandi 2 · «Yozmang» dedi 1 (Namuna Tekstil Savdo) · "
        "notoʻgʻri/B2C 1",
        "Menejer javob bermadi: 1",
        "Telegram oʻquvchi: 14 chat · kecha 33 signal · 1 yangi chat · flood yoʻq",
        "Xarajat: kecha $6.18 · oy $74.90/$250.00 (29%)",
        "Tekshiruv: ✓ 9 / ✗ 1 / oʻtkazildi 2",
        "  ✗ oʻzgargan: OLX sotuvchilari — fixture yangilansin, /manbalar",
        "  oʻtkazildi: Instagram biznes akkauntlari, Google Places",
        "Tizim: 2 ta muammo — /holat",
        "Sizni 9 ta karta kutmoqda → /lidlar",
    ]


def test_skipped_live_tests_are_listed_and_never_counted_green():
    text = digest.render_digest(dv.busy())
    assert "Tekshiruv: ✓ 9 / ✗ 1 / oʻtkazildi 2" in text
    assert "  oʻtkazildi: Instagram biznes akkauntlari, Google Places" in text
    full = digest.render_digest(dv.longest())
    assert "Choraklik tekshiruv: ✓ 999 / ✗ 999 / oʻtkazildi 999" in full
    assert "va yana 7 ta" in full.split("oʻtkazildi: ")[-1]


def test_won_is_labelled_the_first_shipment_proxy_and_each_unclassed_label_shown():
    view = dv.busy()
    text = digest.render_digest(view)
    assert "yutuq 3 (birinchi yuk oʻrniga, taxminiy)" in text
    many = digest.render_digest(
        replace_view(
            view,
            unclassed=(
                digest.Unclassed("Mijoz oʻzi olib ketdi", 2),
                digest.Unclassed("Klient <b>yoʻq</b>", 1),
            ),
        )
    )
    assert "«Mijoz oʻzi olib ketdi» 2 ta · «Klient &lt;b&gt;yoʻq&lt;/b&gt;» 1 ta" in many


def test_the_dead_man_line_says_an_unset_or_refused_watcher():
    view = dv.quiet()
    unset = digest.render_digest(
        replace_view(view, deadman=digest.DeadmanLine(configured=False))
    )
    assert unset.endswith(
        "⚠️ Tashqi kuzatuvchi sozlanmagan — <code>.env</code> DEADMAN_PING_URL"
    )
    down = digest.render_digest(
        replace_view(view, deadman=digest.DeadmanLine(ok=False, status=None))
    )
    assert "oxirgi signalni olmadi (javob yoʻq)" in down
    fine = digest.render_digest(replace_view(view, deadman=digest.DeadmanLine(ok=True)))
    assert "kuzatuvchi" not in fine


def test_past_the_daily_card_cap_the_waiting_cards_are_said():
    capped = replace_view(
        dv.quiet(),
        yesterday=digest.Yesterday(asked=20),
        waiting_cards=14,
    )
    assert "Kunlik karta chegarasi (20) toʻldi — navbatda 14 ta" in digest.render_digest(
        capped
    )
    under = replace_view(capped, yesterday=digest.Yesterday(asked=19))
    assert "chegarasi" not in digest.render_digest(under)


def test_fit_drops_middle_lines_and_keeps_the_first_and_the_last():
    lines = ["bosh", *[f"qator {n} " + "x" * 90 for n in range(60)], "oxir"]
    text = digest.fit(lines, limit=1000)
    assert len(text) <= 1000
    assert text.startswith("bosh\nqator 0 ") and text.endswith("\n  …\noxir")
    assert digest.fit(["a", "b"], limit=1000) == "a\nb"


def test_a_digest_past_the_caps_is_still_one_message():
    """No list can pass its cap; a count can (an int has no bound): the
    renderer gives way line by line instead of handing Telegram 4,097."""
    huge = 10**3000
    view = replace_view(dv.longest(), yesterday=digest.Yesterday(huge, 0, 0, 0, 0, 0))
    text = digest.render_digest(view)
    assert len(text) <= 4096
    lines = text.splitlines()
    assert lines[0] == "☀️ 29.10.2026 — Qidiruv agenti"
    assert lines[-2:] == ["  …", "Sizni 99999 ta karta kutmoqda → /lidlar"]
    assert uz_problems(text, rendered=True) == set()


def replace_view(view: digest.DigestView, **changes: Any) -> digest.DigestView:
    from dataclasses import replace

    return replace(view, **changes)


# --- loading (synthetic rows) ----------------------------------------------------------


async def outcome(
    session: AsyncSession,
    log: Any,
    label: str,
    *,
    landing: str = "created",
    lost_reason: str | None = None,
    contacted_at: datetime | None = None,
    erp_at: datetime | None = None,
) -> None:
    session.add(
        Outcome(
            external_id=log.external_id,
            intake_id=uuid.uuid4(),
            candidate_id=log.candidate_id,
            company_id=log.company_id,
            landing=landing,
            stage_kind="lost" if label.startswith("lost_") else None,
            lost_reason=lost_reason,
            contacted_at=contacted_at,
            erp_at=erp_at or NOW - timedelta(days=1),
            label=label,
            precision_class="neutral",
            raw={},
        )
    )
    await session.flush()


async def pushed(
    session: AsyncSession,
    n: int,
    *,
    sent_at: datetime,
    synthetic: bool = False,
    status: str = "sent",
) -> Any:
    company = await gf.firm(session, f"Namuna Push {n}")
    cand = await gf.candidate(
        session,
        company,
        state="pushed",
        approved_at=sent_at - timedelta(minutes=5),
        score_breakdown={"gates": {"test": True}} if synthetic else None,
    )
    return await gf.push(
        session, cand, sent_at=sent_at, phone_tail=gf.tail(n), status=status
    )


async def test_the_erp_line_counts_first_deliveries_by_label(session):
    t = NOW - timedelta(days=3)
    won = await pushed(session, 1, sent_at=t)
    await outcome(session, won, "won", contacted_at=t + timedelta(hours=2))
    nc = await pushed(session, 2, sent_at=t)
    await outcome(session, nc, "lost_no_contact", contacted_at=t + timedelta(hours=1))
    wrong = await pushed(session, 3, sent_at=t)
    await outcome(session, wrong, "lost_wrong_number")
    capped = await pushed(session, 4, sent_at=t)
    await outcome(session, capped, "dropped_capped", landing="dropped")
    await pushed(session, 5, sent_at=t)  # no outcome yet
    # make push-test's company, a 40-day-old push and a failed push: none count.
    test_push = await pushed(session, 6, sent_at=t, synthetic=True)
    await outcome(session, test_push, "won")
    old = await pushed(session, 7, sent_at=NOW - timedelta(days=40))
    await outcome(session, old, "won")
    await pushed(session, 8, sent_at=t, status="failed")
    # A re-push of the won id (attempt 2) is the same push, counted once.
    won.superseded_at = t + timedelta(days=1)
    await session.flush()
    await gf.push(
        session,
        (await session.get(gf.LeadCandidate, won.candidate_id)),
        sent_at=t + timedelta(days=1),
        phone_tail=gf.tail(1),
        external_id=won.external_id,
        attempt=2,
    )
    await session.commit()
    line = await digest.erp_line(session, NOW)
    assert line == digest.ErpLine(
        pushes=5,
        created=3,
        contacted=2,
        won=1,
        lost=2,
        lost_no_contact=1,
        lost_wrong_number=1,
        dropped_capped=1,
    )
    assert line.contacted_pct == 66


async def test_unclassed_labels_are_every_lost_other_reason_once(session):
    t = NOW - timedelta(days=2)
    rows = [
        ("lost_other", "Mijoz  oʻzi OLIB ketdi", t),
        ("lost_other", "mijoz oʻzi olib ketdi", t + timedelta(hours=1)),
        ("lost_other", "Сезон закончился", t),
        ("lost_other", None, t),  # free text arrives as null: nothing to map
        ("lost_price", "Narx qimmat", t),  # classed
    ]
    for n, (label, reason, at) in enumerate(rows, start=1):
        log = await pushed(session, n, sent_at=t)
        await outcome(session, log, label, lost_reason=reason, erp_at=at)
    await session.commit()
    assert await digest.unclassed_labels(session) == (
        digest.Unclassed("mijoz oʻzi olib ketdi", 2),
        digest.Unclassed("Сезон закончился", 1),
    )


async def test_yesterday_s_counts_are_the_tashkent_day_before(session):
    start, end = digest.yesterday_window(NOW)
    assert (start, end) == (
        datetime(2026, 10, 28, 0, 0, tzinfo=dv.TZ),
        datetime(2026, 10, 29, 0, 0, tzinfo=dv.TZ),
    )
    inside = YESTERDAY.replace(hour=14)
    for n in (1, 2):
        await pushed(session, n, sent_at=inside)
    await pushed(session, 3, sent_at=inside, synthetic=True)
    await pushed(session, 4, sent_at=NOW - timedelta(minutes=30))  # today
    company = await gf.firm(session, "Namuna Rad", created_at=inside)
    await gf.candidate(session, company, state="rejected", rejected_at=inside)
    for at in (inside, NOW - timedelta(days=3)):
        await gf.firm(session, created_at=at)
    await session.commit()
    y = await digest._yesterday(session, NOW)
    assert (y.found, y.pushed, y.rejected) == (2, 2, 1)
    # Candidates scored and approved yesterday: the pushed ones' approvals.
    assert y.approved == 2


async def test_the_managers_day_counts_contacts_names_refusals_and_silence(session):
    inside = YESTERDAY.replace(hour=11)
    rows = [
        ("Namuna Bogʻlandi", "contacted", "approved", None),
        ("Namuna Yozmang", "said_no", "declined", None),
        ("Namuna B2C", "wrong_or_b2c", "declined", None),
        ("Namuna Jim", None, "expired", None),
        ("Namuna Eski", None, "expired", "stale:pushed"),
    ]
    for name, answer, state, note in rows:
        company = await gf.firm(session, name)
        cand = await gf.candidate(session, company, state="hold")
        session.add(
            LeadReview(
                candidate_id=cand.id,
                kind="manager",
                state=state,
                answer=answer,
                answered_at=inside if answer else None,
                asked_at=inside - timedelta(days=3),
                expires_at=inside,
                note=note,
                by_telegram_id=2002,
            )
        )
    await session.commit()
    assert await digest._managers(session, NOW) == digest.ManagerLine(
        contacted=1, said_no=("Namuna Yozmang",), wrong=1, expired=1
    )


async def test_new_companies_per_source_skip_smoke_rows(session):
    inside = YESTERDAY.replace(hour=10)
    session.add_all(
        [
            CollectorRun(
                source_key="hh_vacancies", started_at=inside, status="ok", new_companies=4
            ),
            CollectorRun(
                source_key="hh_vacancies",
                started_at=inside + timedelta(hours=4),
                status="ok",
                new_companies=3,
            ),
            CollectorRun(
                source_key="hh_vacancies",
                started_at=NOW - timedelta(minutes=10),
                status="ok",
                new_companies=9,
            ),
            CollectorRun(
                source_key="hh_vacancies",
                started_at=inside,
                status="skipped",
                new_companies=5,
                meta={"note": "smoke"},
            ),
        ]
    )
    await session.commit()
    assert await digest._new_by_source(session, NOW) == {"hh_vacancies": 7}


@pytest.fixture
async def sources_kept(session):
    """The seeded sources' switches, restored after the test (KEPT_TABLES)."""
    rows = (
        await session.execute(sa.select(Source.key, Source.enabled, Source.auto_push))
    ).all()
    yield
    for key, enabled, auto in rows:
        await session.execute(
            sa.update(Source)
            .where(Source.key == key)
            .values(enabled=enabled, auto_push=auto)
        )
    await session.commit()


async def test_an_auto_push_flip_is_one_line_once(session, sources_kept):
    await session.execute(sa.update(Source).values(auto_push=False))
    await session.commit()
    rows, flips, seen = await digest._sources(session, NOW)
    # The first digest has nothing to compare with: it only remembers.
    assert flips == () and seen["hh_vacancies"] is False
    await digest._remember(session, seen)
    await session.execute(
        sa.update(Source).where(Source.key == "hh_vacancies").values(auto_push=True)
    )
    await session.commit()
    _, flips, seen = await digest._sources(session, NOW)
    title = await session.scalar(
        sa.select(Source.title_uz).where(Source.key == "hh_vacancies")
    )
    assert flips == (digest.Flip(title, True),)
    await digest._remember(session, seen)
    await session.commit()
    assert (await digest._sources(session, NOW))[1] == ()


async def test_only_enabled_sources_are_listed_with_their_state(session, sources_kept):
    await session.execute(sa.update(Source).values(enabled=False))
    await session.execute(
        sa.update(Source)
        .where(Source.key == "hh_vacancies")
        .values(
            enabled=True,
            paused_until=NOW + timedelta(hours=1),
            precision_30d=Decimal("0.5"),
            pushes_30d=16,
        )
    )
    await session.commit()
    try:
        rows, _, _ = await digest._sources(session, NOW)
        assert [(r.state, r.precision, r.pushes_30d) for r in rows] == [
            ("paused", Decimal("0.500"), 16)
        ]
    finally:
        await session.execute(
            sa.update(Source)
            .where(Source.key == "hh_vacancies")
            .values(paused_until=None, precision_30d=None, pushes_30d=0)
        )
        await session.commit()


async def test_the_reader_line_counts_yesterday_s_hits_and_joins(session, monkeypatch):
    monkeypatch.setattr(settings, "userbot_enabled", True)
    inside = YESTERDAY.replace(hour=15)
    session.add_all(
        [
            ChatMonitor(
                tg_chat_id=-1001,
                chat_type="group",
                status="joined",
                joined_at=inside,
            ),
            ChatMonitor(
                tg_chat_id=-1002,
                chat_type="group",
                status="joined",
                joined_at=NOW - timedelta(days=5),
            ),
        ]
    )
    for n, at in enumerate(
        (inside, inside + timedelta(hours=1), NOW - timedelta(minutes=5))
    ):
        session.add(
            Interaction(
                tg_chat_id=-1001,
                tg_message_id=n + 1,
                occurred_at=at,
                text="kub",
                prefilter_hit=["volume"],
            )
        )
    await session.commit()
    status = health.Status(
        now=NOW,
        db_ok=True,
        disk_free_bytes=None,
        disk_total_bytes=None,
        disk_low=False,
        backup=health.BackupInfo(configured=True),
        components={"userbot": health.judge("userbot", NOW, {"enabled": True}, now=NOW)},
        chats_on=2,
        flood_until=NOW + timedelta(hours=2),
    )
    line = await digest._reader(session, status, NOW)
    assert line == digest.ReaderLine(2, 2, 1, NOW + timedelta(hours=2))
    monkeypatch.setattr(settings, "userbot_enabled", False)
    assert await digest._reader(session, status, NOW) is None


async def test_no_lost_list_is_said_only_on_the_fallback_with_an_import(
    session, monkeypatch
):
    monkeypatch.setattr(settings, "crm_push_source", "other")
    assert await digest._no_lost_list(session) is False
    await gf.row(session, "client", "phone_tail", gf.tail(9), source="manual_import")
    await session.commit()
    assert await digest._no_lost_list(session) is True
    monkeypatch.setattr(settings, "crm_push_source", "agent")
    monkeypatch.setattr(settings, "crm_agent_key", "k" * 32)
    assert await digest._no_lost_list(session) is False


async def test_the_last_smoke_run_of_the_day_is_read(session):
    started = NOW - timedelta(hours=4)
    results = [
        smoke.TestResult(
            "hh_vacancies", "tests/live/test_hh_vacancies_live.py::t", "passed"
        ),
        smoke.TestResult(
            "olx_business", "tests/live/test_olx_business_live.py::t", "failed"
        ),
        smoke.TestResult(
            "instagram_discovery", "tests/live/test_ig_live.py::t", "skipped"
        ),
        smoke.TestResult(
            "olx_business",
            "tests/live/test_olx_business_live.py::u",
            "failed",
            blocked=True,
        ),
    ]
    await smoke.record(
        session,
        results[:1],
        started_at=started - timedelta(days=2),
        now=started,
        full=False,
    )
    run = await smoke.record(
        session,
        results,
        started_at=started,
        now=started + timedelta(minutes=3),
        full=False,
    )
    await session.commit()
    found = await smoke.latest(session, now=NOW)
    titles = dict((await session.execute(sa.select(Source.key, Source.title_uz))).all())
    assert found == smoke.SmokeRun(
        run=run,
        started_at=started,
        full=False,
        passed=(titles["hh_vacancies"],),
        failed=(titles["olx_business"],),
        skipped=(titles["instagram_discovery"],),
        counts=(1, 2, 1),
    )
    statuses = (
        await session.execute(
            sa.select(CollectorRun.status, CollectorRun.error)
            .where(CollectorRun.meta["run"].astext == run)
            .order_by(CollectorRun.id)
        )
    ).all()
    assert statuses == [
        ("skipped", None),
        ("skipped", None),
        ("skipped", None),
        ("failed", "host_blocked"),
    ]
    assert await smoke.latest(session, now=NOW + timedelta(days=1)) is None


# --- once a day, to the owner only -----------------------------------------------------


class Owner:
    def __init__(self, delivered: bool = True) -> None:
        self.told: list[str] = []
        self.delivered = delivered

    async def __call__(self, text: str) -> bool:
        self.told.append(text)
        return self.delivered


async def digest_rows(session: AsyncSession) -> list[tuple[str, str]]:
    return list(
        (
            await session.execute(
                sa.select(ReminderLog.kind, ReminderLog.ref)
                .where(ReminderLog.kind == "digest")
                .order_by(ReminderLog.id)
            )
        ).all()
    )


async def test_the_digest_is_sent_once_per_tashkent_day(session):
    owner = Owner()
    assert await digest.send(owner, now=NOW) == "sent"
    assert await digest.send(owner, now=NOW + timedelta(hours=3)) == "already"
    assert len(owner.told) == 1 and owner.told[0].startswith("☀️ 29.10.2026")
    assert await digest_rows(session) == [("digest", "2026-10-29")]
    assert await digest.send(owner, now=NOW + timedelta(days=1)) == "sent"
    assert len(owner.told) == 2


async def test_two_speakers_at_once_send_one_digest(session, monkeypatch):
    """The worker's cron and its start-up catch-up (or two worker
    processes) both pass the cheap «sent today?» look before either has
    claimed: the claim under the day's advisory lock lets one through."""
    import asyncio

    real_load = digest.load
    entered = 0
    both_looked = asyncio.Event()

    async def load(session: AsyncSession, *, now: datetime) -> digest.Loaded:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_looked.set()
        await asyncio.wait_for(both_looked.wait(), timeout=10)
        return await real_load(session, now=now)

    monkeypatch.setattr(digest, "load", load)
    owner = Owner()
    outcomes = await asyncio.gather(
        digest.send(owner, now=NOW), digest.send(owner, now=NOW + timedelta(seconds=1))
    )
    assert sorted(outcomes) == ["already", "sent"]
    assert len(owner.told) == 1
    assert await digest_rows(session) == [("digest", "2026-10-29")]


async def test_an_undelivered_digest_stays_due_and_remembers_nothing(session):
    flag = await session.get(RuntimeFlag, digest.AUTO_PUSH_FLAG)
    assert flag is None
    assert await digest.send(Owner(delivered=False), now=NOW) == "undelivered"
    assert await digest_rows(session) == []
    session.expire_all()
    assert await session.get(RuntimeFlag, digest.AUTO_PUSH_FLAG) is None
    owner = Owner()
    assert await digest.send(owner, now=NOW + timedelta(minutes=5)) == "sent"
    session.expire_all()
    assert await session.get(RuntimeFlag, digest.AUTO_PUSH_FLAG) is not None


async def test_the_digest_job_reaches_the_owner_and_no_one_else(session, monkeypatch):
    from clientscrapper.bot import deliver
    from clientscrapper.worker import main as worker
    from tests.fake_bot import fake_bot

    monkeypatch.setattr(settings, "assistant_bot_token", "42:TEST")
    monkeypatch.setattr(settings, "owner_telegram_id", 1001)
    monkeypatch.setattr(settings, "manager_telegram_ids", (2002,))
    bot, fake = fake_bot()
    monkeypatch.setattr(deliver, "make_bot", lambda token=None: bot)
    connected = worker.connect_owner_bot()
    try:
        assert await worker.digest_job(now=NOW) == "sent"
        assert await worker.digest_job(now=NOW + timedelta(minutes=1)) == "already"
    finally:
        await worker.disconnect_owner_bot(connected)
    assert fake.chats() == {1001}
    [text] = fake.texts()
    assert text.startswith("☀️ 29.10.2026 — Qidiruv agenti\nKecha: ")
    assert len(text) <= 4096


@pytest.mark.parametrize(
    ("clock", "due"),
    [((8, 59), False), ((9, 0), True), ((17, 59), True), ((18, 0), False)],
)
def test_a_missed_digest_is_due_until_review_hours_end(clock, due):
    from clientscrapper.worker import main as worker

    assert worker.digest_due(NOW.replace(hour=clock[0], minute=clock[1])) is due


async def test_the_start_up_catch_up_sends_a_missed_digest(session):
    from clientscrapper.worker import main as worker

    scheduler = worker.build_scheduler()
    noon = NOW.replace(hour=12)
    await worker.catch_up(now=noon, scheduler=scheduler)
    job = scheduler.get_job("digest")
    assert job.next_run_time == noon
    # Sent already today: the catch-up leaves it alone.
    session.add(ReminderLog(kind="digest", ref="2026-10-29", sent_at=NOW))
    await session.commit()
    later = worker.build_scheduler()
    await worker.catch_up(now=noon, scheduler=later)
    assert getattr(later.get_job("digest"), "next_run_time", None) != noon


async def test_load_reads_a_whole_morning(session, monkeypatch):
    """The loader end to end on an empty database plus one of each fact: the
    rendered text is the busy layout's skeleton with these numbers."""
    monkeypatch.setattr(settings, "deadman_ping_url", "")
    log = await pushed(session, 1, sent_at=YESTERDAY.replace(hour=12))
    await outcome(session, log, "won", contacted_at=YESTERDAY.replace(hour=13))
    session.add(
        Suppression(
            kind="client", key_type="phone_tail", key=gf.tail(2), source="manual_import"
        )
    )
    await session.commit()
    loaded = await digest.load(session, now=NOW)
    view = loaded.view
    assert view.day == dv.DAY
    assert view.yesterday.pushed == 1 and view.yesterday.approved == 1
    assert view.erp.won == 1 and view.erp.contacted == 1
    assert view.deadman == digest.DeadmanLine(configured=False)
    assert view.smoke is None
    text = digest.render_digest(view)
    assert "CRMʼga 1/" in text and "yutuq 1 (birinchi yuk oʻrniga, taxminiy)" in text
    assert "Tashqi kuzatuvchi sozlanmagan" in text


async def test_a_refused_dead_man_ping_reaches_the_digest(session, monkeypatch):
    """The ping the worker made (06 §7.2) is observed by the monitor's fake
    transport, carried in the next «worker» beat and said in the morning:
    a watcher that answers 404 never alarms on the day it should."""
    import httpx

    from clientscrapper.worker import main as worker

    pinged: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        pinged.append(str(request.url))
        return httpx.Response(404)

    monkeypatch.setattr(settings, "deadman_ping_url", "https://hc.example.uz/ping/namuna")
    monkeypatch.setattr(worker._deadman, "last", None)
    monkeypatch.setattr(worker._deadman, "seen", None)
    transport = httpx.MockTransport(refuse)
    await worker.heartbeat_job(None, transport=transport)  # beat, then the ping
    await worker.heartbeat_job(None, transport=transport)  # beat carrying it
    assert pinged == ["https://hc.example.uz/ping/namuna"]
    view = (await digest.load(session, now=NOW)).view
    assert view.deadman == digest.DeadmanLine(configured=True, ok=False, status=404)
    assert (
        "⚠️ Tashqi kuzatuvchi oxirgi signalni olmadi (HTTP 404) — manzilni tekshiring"
        in digest.render_digest(view).split("\n")
    )
