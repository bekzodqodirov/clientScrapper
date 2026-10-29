"""Synthetic ``DigestView`` fixtures (06 §1.8, T-35): a quiet morning, a
busy one, and the longest one the lists can make — every list at its cap,
every name at its cut, a Russian ERP label inside «…». The values are
visibly synthetic (Namuna …)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from clientscrapper.services import digest, smoke

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 29, 9, 0, tzinfo=TZ)
DAY = date(2026, 10, 29)


def quiet() -> digest.DigestView:
    """A day with nothing but the always-said lines."""
    return digest.DigestView(
        day=DAY,
        push_cap=10,
        review_cap=20,
        spend=digest.SpendLine(Decimal("0.42"), Decimal("3.10"), Decimal("250")),
        now=NOW,
    )


def busy() -> digest.DigestView:
    return digest.DigestView(
        day=DAY,
        yesterday=digest.Yesterday(
            found=37, scored=21, asked=12, approved=7, pushed=6, rejected=3
        ),
        push_cap=10,
        review_cap=20,
        queue=digest.QueueLine(
            review=9,
            review_oldest_at=NOW - timedelta(hours=30),
            enriching=4,
            hold=11,
            handoff_open=2,
        ),
        erp=digest.ErpLine(
            pushes=48,
            created=40,
            due=40,
            in_time=27,
            won=3,
            lost=9,
            lost_no_contact=2,
            lost_wrong_number=1,
            dropped_capped=1,
            dropped_suppressed=2,
        ),
        unclassed=(digest.Unclassed("Mijoz oʻzi olib ketdi", 2, NOW),),
        sources=(
            digest.SourceRow("hh.uz vakansiyalari", "on", 5, Decimal("0.62"), 18),
            digest.SourceRow("OLX sotuvchilari", "paused", 0, None, 0),
            digest.SourceRow("Telegram chatlari", "on", 12, Decimal("0.41"), 22),
        ),
        flips=(digest.Flip("hh.uz vakansiyalari", True),),
        reader=digest.ReaderLine(chats_on=14, hits=33, joined=1, flood_until=None),
        managers=digest.ManagerLine(
            contacted=2, said_no=("Namuna Tekstil Savdo",), wrong=1, expired=1
        ),
        spend=digest.SpendLine(Decimal("6.18"), Decimal("74.90"), Decimal("250")),
        smoke=smoke.SmokeRun(
            run=(NOW - timedelta(hours=4)).isoformat(),
            started_at=NOW - timedelta(hours=4),
            full=False,
            passed=("hh.uz vakansiyalari", "Biznes katalogi"),
            failed=("OLX sotuvchilari",),
            skipped=("Instagram biznes akkauntlari", "Google Places"),
            counts=(9, 1, 2),
        ),
        problems=2,
        waiting_cards=9,
        now=NOW,
    )


def longest() -> digest.DigestView:
    """Every list at its cap, every name at its cut."""
    long_title = "Namuna juda uzun nomli manba " * 4
    names = tuple(f"Namuna Firma {n} " + "Savdo " * 8 for n in range(12))
    return digest.DigestView(
        day=DAY,
        yesterday=digest.Yesterday(9999, 9999, 9999, 9999, 9999, 9999),
        push_cap=9999,
        review_cap=40,
        queue=digest.QueueLine(
            review=99999,
            review_oldest_at=NOW - timedelta(days=400),
            enriching=99999,
            hold=99999,
            handoff_open=99999,
        ),
        erp=digest.ErpLine(
            pushes=99999,
            created=99999,
            due=99999,
            # Under 40 %: the «Menejer javobi» escalation is said too.
            in_time=9999,
            won=99999,
            lost=99999,
            lost_no_contact=99999,
            lost_wrong_number=99999,
            dropped_capped=99999,
            dropped_suppressed=99999,
            dropped_other=99999,
        ),
        unclassed=tuple(
            digest.Unclassed("Клиент передумал и ушёл к другому " * 3 + str(n), 999, NOW)
            # Past the shown ones: the rest are counted (decision 1a).
            for n in range(digest.UNCLASSED_SHOWN + 2)
        ),
        sources=tuple(
            digest.SourceRow(f"{long_title}{n}", "off", 99999, Decimal("1"), 99999)
            for n in range(23)
        ),
        flips=tuple(
            digest.Flip(f"{long_title}{n}", n % 2 == 0, pct=100, pushes=99999)
            for n in range(23)
        ),
        paused_low=tuple(
            digest.LowPause(f"{long_title}{n}", "19") for n in range(digest.MAX_PAUSED + 2)
        ),
        reader=digest.ReaderLine(
            chats_on=500, hits=99999, joined=3, flood_until=NOW + timedelta(hours=20)
        ),
        managers=digest.ManagerLine(
            contacted=9999, said_no=names, wrong=9999, expired=9999
        ),
        spend=digest.SpendLine(Decimal("9999.99"), Decimal("99999.99"), Decimal("250")),
        smoke=smoke.SmokeRun(
            run=NOW.isoformat(),
            started_at=NOW,
            full=True,
            passed=names,
            failed=names,
            skipped=names,
            counts=(999, 999, 999),
        ),
        problems=33,
        deadman=digest.DeadmanLine(configured=True, ok=False, status=404),
        no_lost_list=True,
        waiting_cards=99999,
        now=NOW,
    )


VIEWS = (quiet, busy, longest)
