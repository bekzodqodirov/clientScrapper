"""The owner's digest (06 §1.8, 07 §4.1 T-35): ``render_digest`` renders
≤ 4,096 characters from fixtures and lists the skipped live tests; the
loader reads each number from the table that holds it (synthetic rows); it
is said once per Tashkent day, to the owner only, and a delivery that fails
leaves the day's digest due."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
from clientscrapper.services import digest, erp_feeds, health, metrics, smoke
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
    # Every list at its cap: the caps, not the clip, keep it in one message —
    # measured as Telegram can count it at its widest (UTF-16 of the HTML).
    # 4,070 of 4,096 since the flips carry the precision job's numbers, the
    # low-precision pauses their own lines (wave-4 integration) and an «on»
    # flip the AUTO_APPROVE sentence (seam review wave 4).
    text = digest.render_digest(dv.longest())
    assert digest.units(text) <= 4070
    assert "  …" not in text.splitlines()


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
        "ERP (30 kun): 1 ish kunida bogʻlanildi 67% (27/40) · yutuq 3 (birinchi yuk "
        "oʻrniga, taxminiy) · yoʻqotildi 9 (shundan «Qayta qilinmasin» 2, notoʻgʻri "
        "raqam 1)",
        "ERP qabul qilmadi: chegara 1 · bostirilgan 2",
        "⚠️ Sinflanmagan yoʻqotish sabablari: «Mijoz oʻzi olib ketdi» 2 ta — "
        "dasturchiga ayting, aniqlik hisobiga qoʻshilsin",
        "Manbalar:",
        "  hh.uz vakansiyalari 5 yangi · 62% (18) ✅",
        "  OLX sotuvchilari 0 yangi · — (0) ⏸",
        "  Telegram chatlari 12 yangi · 41% (22) ✅",
        "🔁 Avtomatik push yoqildi: hh.uz vakansiyalari",
        "  AUTO_APPROVE oʻchiq — bu manbalarning kartalari hali ham sizga keladi",
        "Menejerlar kecha: bogʻlandi 2 · «Yozmang» dedi 1 (Namuna Tekstil Savdo) · "
        "notoʻgʻri/B2C 1",
        "Menejer javob bermadi: 1",
        "Telegram oʻquvchi: 14 chat · kecha 33 signal · 1 yangi chat · flood yoʻq",
        "Xarajat: kecha $6.18 · oy $74.90/$250.00 (29%)",
        "Tekshiruv: ✓ 9 / ✗ 1 / oʻtkazildi 2",
        "  ✗ oʻzgargan: OLX sotuvchilari — sayt tuzilishi oʻzgargan boʻlishi mumkin, "
        "dasturchiga ayting; /manbalar",
        "  oʻtkazildi: Instagram biznes akkauntlari, Google Places",
        "Tizim: 2 ta muammo — /holat",
        "Sizni 9 ta karta kutmoqda → /lidlar",
    ]


def test_an_on_flip_says_its_cards_still_come_while_auto_approve_is_off():
    """The alert's sentence, one rule (erp_feeds.flips_not_live): with
    AUTO_APPROVE off a source that earned auto_push still sends its cards to
    the owner (seam review: the digest said «yoqildi» and nothing else)."""
    caveat = erp_feeds.FLIPS_NOT_LIVE_UZ
    assert caveat in digest.render_digest(dv.busy()).splitlines()
    live = replace_view(dv.busy(), auto_approve=True)
    assert caveat not in digest.render_digest(live).splitlines()
    off_only = replace_view(dv.busy(), flips=(digest.Flip("OLX sotuvchilari", False),))
    assert caveat not in digest.render_digest(off_only).splitlines()


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
                digest.Unclassed("Mijoz oʻzi olib ketdi", 2, NOW),
                digest.Unclassed("Klient <b>yoʻq</b>", 1, NOW),
            ),
        )
    )
    assert "«Mijoz oʻzi olib ketdi» 2 ta · «Klient &lt;b&gt;yoʻq&lt;/b&gt;» 1 ta" in many
    # Decision 1a: every label — the first ten by name, the rest counted.
    eleven = tuple(
        digest.Unclassed(f"Namuna sabab {n:02}", 11 - n, NOW) for n in range(11)
    )
    line = [
        x
        for x in digest.render_digest(replace_view(view, unclassed=eleven)).splitlines()
        if "Sinflanmagan" in x
    ]
    assert line == [
        "⚠️ Sinflanmagan yoʻqotish sabablari: "
        + " · ".join(f"«Namuna sabab {n:02}» {11 - n} ta" for n in range(10))
        + " · va yana 1 ta sabab — dasturchiga ayting, aniqlik hisobiga qoʻshilsin"
    ]


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
    # At the cap with nothing waiting there is nothing to say about it.
    emptied = replace_view(capped, waiting_cards=0)
    assert "chegarasi" not in digest.render_digest(emptied)


def test_fit_drops_middle_lines_and_keeps_the_first_and_the_last():
    lines = ["bosh", *[f"qator {n} " + "x" * 90 for n in range(60)], "oxir"]
    text = digest.fit(lines, limit=1000)
    assert len(text) <= 1000
    assert text.startswith("bosh\nqator 0 ") and text.endswith("\n  …\noxir")
    assert digest.fit(["a", "b"], limit=1000) == "a\nb"


def test_fit_keeps_the_closing_lines_it_is_told_to():
    lines = ["bosh", *[f"qator {n} " + "x" * 90 for n in range(60)], "t1", "t2", "t3"]
    text = digest.fit(lines, limit=1000, keep_tail=3)
    assert digest.units(text) <= 1000
    assert text.startswith("bosh\nqator 0 ") and text.endswith("\n  …\nt1\nt2\nt3")


def test_fit_counts_what_telegram_can_count_at_its_widest():
    """A name made of emoji off the BMP is two UTF-16 units a character: a
    code-point count would let 4,096 such characters through as 4,096."""
    wide = "🚚" * 30
    assert (len(wide), digest.units(wide)) == (30, 60)
    lines = ["bosh", *[wide for _ in range(80)], "oxir"]
    text = digest.fit(lines, limit=1000)
    assert digest.units(text) <= 1000 < len(text) * 2
    assert text.endswith("\n  …\noxir")
    view = replace_view(
        dv.longest(),
        managers=digest.ManagerLine(said_no=tuple("🚚" * 40 for _ in range(5))),
        unclassed=tuple(digest.Unclassed("🚚" * 60, 1, NOW) for _ in range(12)),
    )
    assert digest.units(digest.render_digest(view)) <= 4096


def test_the_manager_escalation_is_said_under_target_with_twenty_pushes():
    """07 §3 / R9: contacted within one business day < 40 % while pushes ≥ 20
    is the owner's «Menejer javobi» — the managers' number, not a code fix."""
    view = dv.busy()

    def said(**erp: int) -> list[str]:
        line = replace_view(view, erp=digest.ErpLine(**erp))
        return [
            x for x in digest.render_digest(line).splitlines() if "Menejer javobi" in x
        ]

    assert said(pushes=20, created=20, due=20, in_time=7) == [
        "⚠️ Menejer javobi: 1 ish kunida bogʻlanildi 35% (7/20), maqsad ≥ 40% "
        "(faqat ERP koʻrgani) — menejerlar bilan gaplashing; ERPda arizalar kimga "
        "tushayotganini tekshiring"
    ]
    assert said(pushes=19, created=19, due=19, in_time=0) == []
    assert said(pushes=20, created=20, due=20, in_time=8) == []  # 40 %: on target
    assert said(pushes=25, created=25, due=0, in_time=0) == []  # nobody due yet
    assert digest.ErpLine(pushes=20, due=20, in_time=7).escalated


def test_a_digest_past_the_caps_is_still_one_message():
    """No list can pass its cap; a count can (an int has no bound): the
    renderer gives way line by line instead of handing Telegram 4,097."""
    huge = 10**3000
    view = replace_view(dv.longest(), yesterday=digest.Yesterday(huge, 0, 0, 0, 0, 0))
    text = digest.render_digest(view)
    assert len(text) <= 4096
    lines = text.splitlines()
    assert lines[0] == "☀️ 29.10.2026 — Qidiruv agenti"
    # The closing lines are the last to give way: the system's state, the
    # outside watcher and the cards waiting are still said.
    assert lines[-4:] == [
        "  …",
        "Tizim: 33 ta muammo — /holat",
        "⚠️ Tashqi kuzatuvchi oxirgi signalni olmadi (HTTP 404) — manzilni tekshiring",
        "Sizni 99999 ta karta kutmoqda → /lidlar",
    ]
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
        due=3,
        in_time=2,
        won=1,
        lost=2,
        lost_no_contact=1,
        lost_wrong_number=1,
        dropped_capped=1,
    )
    assert line.contacted_pct == 66


def test_the_next_business_day_ends_at_its_tashkent_midnight():
    tz = dv.TZ
    friday = datetime(2026, 10, 30, 16, 0, tzinfo=tz)
    saturday = datetime(2026, 10, 31, 10, 0, tzinfo=tz)
    sunday = datetime(2026, 11, 1, 10, 0, tzinfo=tz)
    assert digest.next_business_day_end(friday) == datetime(2026, 11, 1, 0, 0, tzinfo=tz)
    for weekend in (saturday, sunday):
        assert digest.next_business_day_end(weekend) == datetime(
            2026, 11, 3, 0, 0, tzinfo=tz
        )
    # A UTC moment is read on the Tashkent calendar: 20:00 UTC Thursday is
    # already Friday in Tashkent.
    late = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
    assert digest.next_business_day_end(late) == datetime(2026, 11, 1, 0, 0, tzinfo=tz)


def test_contacted_in_time_counts_only_the_leads_whose_day_is_over():
    tz = dv.TZ
    now = datetime(2026, 11, 3, 12, 0, tzinfo=tz)  # Tuesday
    friday = datetime(2026, 10, 30, 16, 0, tzinfo=tz)
    raw = [
        (friday, "created", datetime(2026, 10, 31, 23, 59, tzinfo=tz)),  # in time
        (friday, "created", datetime(2026, 11, 1, 0, 1, tzinfo=tz)),  # late
        (friday, "created", None),  # due, never contacted
        (friday, "client", None),  # not a lead the managers work
        (datetime(2026, 11, 3, 9, 0, tzinfo=tz), "created", None),  # not due yet
        (datetime(2026, 11, 3, 9, 0, tzinfo=tz), "created", now),  # early hit
    ]
    rows = [
        metrics.PushRow(
            f"cs:{n}", sent, 72, landing=landing, label="open", contacted_at=at
        )
        for n, (sent, landing, at) in enumerate(raw)
    ]
    assert digest.contacted_in_time(rows, now=now) == metrics.Ratio(2, 4)


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
    assert [(u.label, u.n) for u in await digest.unclassed_labels(session)] == [
        ("mijoz oʻzi olib ketdi", 2),
        ("Сезон закончился", 1),
    ]


async def test_a_re_pushed_lead_is_dated_by_the_push_the_erp_recorded(session):
    """Rule 5 re-pushes an id the ERP never recorded (02 §2.10): the lead was
    born at the re-push, so the managers' day runs from there — as /holat
    and the daily cap date it (the live row)."""
    first = NOW - timedelta(days=8)  # Wednesday 21.10
    log = await pushed(session, 1, sent_at=first)
    log.superseded_at = first + timedelta(days=2)
    await session.flush()
    again = await gf.push(
        session,
        (await session.get(gf.LeadCandidate, log.candidate_id)),
        sent_at=NOW - timedelta(days=2),  # Tuesday 27.10
        phone_tail=gf.tail(1),
        external_id=log.external_id,
        attempt=2,
    )
    await outcome(session, again, "contacted_open", contacted_at=NOW - timedelta(days=1))
    await session.commit()
    line = await digest.erp_line(session, NOW)
    assert (line.pushes, line.in_time, line.due) == (1, 1, 1)


async def test_slow_managers_reach_the_owner_as_menejer_javobi(session):
    """The review's probe: twenty pushes contacted four days later — 0 % in
    one business day, said as the escalation (it read 100 % before)."""
    t = NOW - timedelta(days=10)
    for n in range(1, 21):
        log = await pushed(session, n, sent_at=t)
        await outcome(session, log, "contacted_open", contacted_at=t + timedelta(days=4))
    await session.commit()
    text = digest.render_digest((await digest.load(session, now=NOW)).view)
    erp = [x for x in text.splitlines() if x.startswith(("ERP (30", "⚠️ Menejer"))]
    assert erp == [
        "ERP (30 kun): 1 ish kunida bogʻlanildi 0% (0/20) · yutuq 0 (birinchi yuk "
        "oʻrniga, taxminiy) · yoʻqotildi 0 (shundan «Qayta qilinmasin» 0, notoʻgʻri "
        "raqam 0)",
        "⚠️ Menejer javobi: 1 ish kunida bogʻlanildi 0% (0/20), maqsad ≥ 40% "
        "(faqat ERP koʻrgani) — menejerlar bilan gaplashing; ERPda arizalar kimga "
        "tushayotganini tekshiring",
    ]


async def test_the_loaded_digest_reads_auto_approve(session, monkeypatch):
    """The view carries settings.auto_approve, so the «on» flip's sentence
    follows the owner's switch (erp_feeds.flips_not_live)."""
    for value in (False, True):
        monkeypatch.setattr(settings, "auto_approve", value)
        assert (await digest.load(session, now=NOW)).view.auto_approve is value


async def test_every_unclassed_label_reaches_the_digest(session):
    """Decision 1a «every»: seven labels, seven in the view, none dropped."""
    t = NOW - timedelta(days=2)
    words = ("bahor", "yoz", "kuz", "qish", "erta", "kech", "tun")
    for n, word in enumerate(words, start=1):
        log = await pushed(session, n, sent_at=t)
        reason = f"Mijoz {word} ketdi"
        await outcome(session, log, "lost_other", lost_reason=reason, erp_at=t)
    await session.commit()
    view = (await digest.load(session, now=NOW)).view
    assert sorted(u.label for u in view.unclassed) == sorted(
        f"Mijoz {w} ketdi" for w in words
    )
    text = digest.render_digest(view)
    assert all(f"«Mijoz {w} ketdi» 1 ta" in text for w in words)


async def test_push_test_s_company_is_in_none_of_yesterday_s_counts(session):
    """05 §B.1: make push-test's synthetic company — found, scored, approved,
    pushed or rejected — is no number of the owner's morning."""
    from clientscrapper.tools import push_test

    inside = YESTERDAY.replace(hour=10)
    company, cand = await push_test.mint(session, "+998900000099", now=inside)
    company.created_at = inside
    cand.last_scored_at = inside
    cand.rejected_at = inside
    await session.commit()
    assert await digest._yesterday(session, NOW) == digest.Yesterday()
    # A real company the same hour is counted.
    real = await gf.firm(session, "Namuna Haqiqiy", created_at=inside)
    await gf.candidate(session, real, state="rejected", rejected_at=inside)
    await session.commit()
    got = await digest._yesterday(session, NOW)
    assert (got.found, got.rejected) == (1, 1)


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


async def test_waiting_cards_are_live_owner_cards_on_candidates_in_review(session):
    rows = [
        ("owner", "pending", "review"),  # waiting
        ("owner", "asked", "review"),  # waiting
        ("owner", "asked", "hold"),  # its candidate left review: not waiting
        ("owner", "declined", "review"),  # answered
        ("manager", "asked", "review"),  # a manager's card
    ]
    for n, (kind, state, cand_state) in enumerate(rows):
        company = await gf.firm(session, f"Namuna Kutish {n}")
        cand = await gf.candidate(session, company, state=cand_state)
        session.add(
            LeadReview(
                candidate_id=cand.id,
                kind=kind,
                state=state,
                answered_at=NOW if state == "declined" else None,
                asked_at=NOW - timedelta(hours=2),
                card_chat_id=1001,
                card_message_id=500 + n,
                expires_at=NOW + timedelta(days=1),
            )
        )
    await session.commit()
    assert await digest._waiting_cards(session) == 2


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
    # A source the last digest never saw (added since) is no flip, whatever
    # its switch says.
    remembered = dict(seen)
    remembered.pop("olx_business")
    await digest._remember(session, remembered)
    await session.execute(
        sa.update(Source).where(Source.key == "olx_business").values(auto_push=True)
    )
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
            # Joined this morning: today's, not yesterday's.
            ChatMonitor(
                tg_chat_id=-1003,
                chat_type="group",
                status="joined",
                joined_at=NOW - timedelta(minutes=10),
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


async def test_a_weekly_smoke_run_that_stopped_coming_is_said(session):
    view = dv.quiet()
    for days, said in ((None, False), (7, False), (9, True)):
        last = None if days is None else NOW - timedelta(days=days, hours=1)
        text = digest.render_digest(replace_view(view, smoke_last_at=last))
        expected = (
            f"⚠️ Haftalik tekshiruv {days} kundan beri boʻlmadi — serverdagi cron "
            "(<code>make smoke-cron</code>)"
        )
        assert (expected in text.splitlines()) is said, days
    results = [smoke.TestResult("hh_vacancies", "tests/live/t.py::t", "passed")]
    started = NOW - timedelta(days=9)
    await smoke.record(session, results, started_at=started, now=started, full=False)
    await session.commit()
    loaded = (await digest.load(session, now=NOW)).view
    assert (loaded.smoke, loaded.smoke_last_at) == (None, started)
    assert "Haftalik tekshiruv 9 kundan beri" in digest.render_digest(loaded)


def test_the_digest_restates_t_36_s_metrics_only_until_they_merge():
    """One home: ``services/metrics`` (T-36's branch) owns the contact rule
    and the unclassed labels. The digest's copies stand only while that
    module does not exist on this tree; once it does, this goes red until
    the digest reads it (the merge's task)."""
    import importlib.util

    restated = {"next_business_day_end", "contacted_in_time", "unclassed_labels"}
    if importlib.util.find_spec("clientscrapper.services.metrics") is None:
        assert restated <= set(vars(digest))
        return
    for name in restated:
        found = getattr(digest, name, None)
        home = getattr(found, "__module__", "clientscrapper.services.metrics")
        assert home == "clientscrapper.services.metrics", f"digest.{name}: read metrics'"


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


async def test_an_undelivered_digest_is_tried_again_the_same_day(session, monkeypatch):
    """Telegram is down at 09:00: the digest job moves itself to the next
    backoff step (5, 15, 30 minutes, then hourly) while the digest is due,
    and the first attempt Telegram takes sends it — once."""
    from clientscrapper.worker import main as worker

    monkeypatch.setattr(worker, "_digest_retry", worker._DigestRetry())
    scheduler = worker.build_scheduler()
    job = scheduler.get_job("digest")
    down = Owner(delivered=False)
    at = NOW
    steps = []
    for _ in range(5):
        assert await worker.digest_job(scheduler, now=at, notify=down) == "undelivered"
        at = scheduler.get_job("digest").next_run_time
        steps.append(at.strftime("%H:%M"))
    assert steps == ["09:05", "09:20", "09:50", "10:50", "11:50"]
    assert await digest_rows(session) == []
    up = Owner()
    assert await worker.digest_job(scheduler, now=at, notify=up) == "sent"
    assert len(up.told) == 1 and len(down.told) == 5
    assert await digest_rows(session) == [("digest", "2026-10-29")]
    # Sent: nothing moves the job again today; the cron's tomorrow stands.
    assert scheduler.get_job("digest").next_run_time == at
    assert job.id == "digest"


async def test_a_retry_past_review_hours_waits_for_tomorrow(session, monkeypatch):
    from clientscrapper.worker import main as worker

    monkeypatch.setattr(worker, "_digest_retry", worker._DigestRetry(tries=3))
    monkeypatch.setattr(worker._digest_retry, "day", "2026-10-29")
    scheduler = worker.build_scheduler()
    late = NOW.replace(hour=17, minute=30)
    assert (
        await worker.digest_job(scheduler, now=late, notify=Owner(delivered=False))
        == "undelivered"
    )
    # Not moved: the pending job still has only its cron (no run time set).
    assert getattr(scheduler.get_job("digest"), "next_run_time", None) is None
    # A new day starts the backoff from its first step.
    tomorrow = NOW + timedelta(days=1)
    assert (
        await worker.digest_job(scheduler, now=tomorrow, notify=Owner(delivered=False))
        == "undelivered"
    )
    assert scheduler.get_job("digest").next_run_time == tomorrow + timedelta(minutes=5)


async def test_a_digest_run_that_raises_is_tried_again(session, monkeypatch):
    from clientscrapper.worker import main as worker

    monkeypatch.setattr(worker, "_digest_retry", worker._DigestRetry())

    async def broken(text: str) -> bool:
        raise ConnectionError("telegram")

    scheduler = worker.build_scheduler()
    with pytest.raises(ConnectionError):
        await worker.digest_job(scheduler, now=NOW, notify=broken)
    assert scheduler.get_job("digest").next_run_time == NOW + timedelta(minutes=5)
    assert await digest_rows(session) == []


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
    assert view.erp.won == 1 and (view.erp.in_time, view.erp.due) == (1, 1)
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
