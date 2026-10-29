"""The pilot's numbers, rule by rule (07 §3.2; 01 §7 decisions 1a, 3a, 5a, 6a).

Pure rules first (the business-day clock, the «price given» stage, the
ratios), then each number over pushes and outcomes written through the real
``record_page`` (tests/pilot_fixtures.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from clientscrapper.config import Settings
from clientscrapper.db.models import LeadCandidate
from clientscrapper.services import cards, metrics
from clientscrapper.services.metrics import PushRow, Ratio
from tests import pilot_fixtures as pf

TZ = ZoneInfo("Asia/Tashkent")
DAY_ = timedelta(days=1)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


# --- the business day (07 §3.2: Mon–Sat, Asia/Tashkent) -----------------------------


@pytest.mark.parametrize(
    ("sent", "deadline"),
    [
        (at(2, 11), at(4, 0)),  # Monday → Tuesday 24:00
        (at(6, 23, 59), at(8, 0)),  # Friday night → Saturday 24:00
        (at(7, 11), at(10, 0)),  # Saturday → Monday 24:00 (Sunday is off)
        (at(8, 11), at(10, 0)),  # Sunday → Monday 24:00
        (at(2, 0, 0), at(4, 0)),  # midnight belongs to its own day
    ],
)
def test_next_business_day_end(sent, deadline):
    assert metrics.next_business_day_end(sent) == deadline


def test_the_business_day_is_tashkent_s_not_utc_s():
    # 20:30 UTC on Saturday is 01:30 on Sunday in Tashkent.
    sent = datetime(2026, 11, 7, 20, 30, tzinfo=ZoneInfo("UTC"))
    assert metrics.next_business_day_end(sent) == at(10, 0)


def test_business_days_are_monday_to_saturday():
    assert frozenset({1, 2, 3, 4, 5, 6}) == metrics.BUSINESS_DAYS


# --- decision 5a: the «price given» stage --------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind", "names", "quoted"),
    [
        ("Narx aytildi", "open", ("narx",), True),
        ("  NARX   berildi ", "open", ("narx",), True),  # case and spacing
        ("Muzokara", "open", ("narx",), False),
        ("Yutildi", "won", ("narx",), True),  # every won stage
        ("Yutildi", "won", (), True),
        ("Narx aytildi", "lost", (), False),  # no fragments: only won
        (None, "open", ("narx",), False),
        ("Taklif yuborildi", "open", ("narx", "taklif"), True),
    ],
)
def test_quote_stage(name, kind, names, quoted):
    assert metrics.quote_stage(name, kind, names) is quoted


def test_the_stage_list_is_configurable():
    s = Settings(_env_file=None, quote_stage_names=" Narx ; Taklif  yuborildi;;")
    assert s.quote_stage_names_parsed == ("narx", "taklif yuborildi")
    assert Settings(_env_file=None).quote_stage_names_parsed == ("narx",)
    assert Settings(_env_file=None, quote_stage_names="").quote_stage_names_parsed == ()


# --- the ratios on plain rows ----------------------------------------------------------

NOW = at(16, 12)


def row(**fields) -> PushRow:
    fields.setdefault("external_id", "cs:x")
    fields.setdefault("sent_at", at(3, 11))
    fields.setdefault("score", 72)
    return PushRow(**fields)


def test_ratio_pct_floors_and_is_none_on_nothing():
    assert Ratio(1, 3).pct == 33
    assert Ratio(2, 3).pct == 66
    assert Ratio(0, 0).pct is None
    assert Ratio(5, 5).pct == 100


def test_the_real_importer_rate_counts_terminal_or_ripe_outcomes_only():
    rows = [
        row(label="won", stage_kind="won", landing="created"),
        row(label="lost_not_importer", stage_kind="lost", landing="created"),
        row(label="lost_other", stage_kind="lost", landing="created"),
        row(label="lost_price", stage_kind="lost", landing="created"),
        # Open, 13 days: not yet judged; 14 days: judged (and real).
        row(label="open_untouched", stage_kind="open", sent_at=NOW - timedelta(days=13)),
        row(label="stale_open", stage_kind="open", sent_at=NOW - timedelta(days=14)),
        # Dropped: terminal, but never a judgement on the company.
        row(label="dropped_capped", landing="dropped"),
        row(label="dropped_suppressed", landing="dropped"),
        # A client landing is terminal.
        row(label="landed_client", landing="client"),
        # No outcome yet: nothing to judge.
        row(sent_at=NOW - timedelta(days=20)),
    ]
    assert metrics.importer_rate(rows, now=NOW) == Ratio(5, 6)


@pytest.mark.parametrize("label", sorted(metrics.NOT_IMPORTER_LABELS))
def test_each_not_importer_label_counts_against(label):
    assert metrics.importer_rate([row(label=label, stage_kind="lost")], now=NOW) == Ratio(
        0, 1
    )


def test_the_not_importer_labels_are_07_s_four():
    assert frozenset(
        {"lost_not_importer", "lost_wrong_number", "lost_competitor", "lost_small"}
    ) == (metrics.NOT_IMPORTER_LABELS)


def test_precision_reads_the_stored_class():
    rows = [
        row(precision_class="positive"),
        row(precision_class="negative"),
        row(precision_class="negative"),
        row(precision_class="neutral"),
        row(precision_class="pending"),
    ]
    assert metrics.precision(rows) == Ratio(1, 3)


def test_contact_in_time_over_created_leads_whose_day_has_passed():
    sat = at(7, 11)
    rows = [
        row(landing="created", contacted_at=at(3, 23)),  # in time
        row(landing="created", contacted_at=at(5, 0, 1)),  # a minute late
        row(landing="created", sent_at=sat, contacted_at=at(9, 23, 59)),  # Monday
        row(landing="created"),  # never, and the day passed
        row(landing="created", sent_at=at(16, 9)),  # today: not due yet
        row(landing="created", sent_at=at(16, 9), contacted_at=at(16, 10)),  # done
        row(landing="joined", contacted_at=at(3, 12)),  # not a lead we made
        row(landing="dropped"),
    ]
    assert metrics.contacted_in_time(rows, now=NOW) == Ratio(3, 5)


def test_a_contact_before_the_push_is_in_time():
    early = row(landing="created", contacted_at=at(3, 10, 58))
    assert metrics.contacted_in_time([early], now=NOW) == Ratio(1, 1)


def test_quoted_of_contacted_or_quoted():
    rows = [
        row(label="won", stage_kind="won", stage_name="Yutildi"),  # quoted
        row(
            label="contacted_open",
            stage_kind="open",
            stage_name="Narx aytildi",
            contacted_at=at(3, 12),
        ),  # fmt: skip
        row(
            label="contacted_open",
            stage_kind="open",
            stage_name="Muzokara",
            contacted_at=at(3, 12),
        ),  # fmt: skip
        # Quoted without a contact the ERP saw: still in both.
        row(label="open_untouched", stage_kind="open", stage_name="Narx aytildi"),
        row(label="open_untouched", stage_kind="open", stage_name="Yangi"),  # neither
        row(stage_name="Narx aytildi"),  # no outcome row: not counted
    ]
    assert metrics.quoted(rows, ("narx",)) == Ratio(3, 4)


def test_first_shipment_is_won_or_landed_over_every_push():
    rows = [
        row(label="won"),
        row(label="landed_client"),
        row(label="contacted_open"),
        row(),
    ]
    assert metrics.first_shipment(rows) == Ratio(2, 4)


# --- over the database ---------------------------------------------------------------


async def test_a_push_is_counted_once_and_dated_by_the_row_its_outcome_is_about(
    session,
):
    """Rule 5 re-pushes an id the ERP never recorded (02 §2.10): its lead was
    born at the re-push, so the live row dates it, as ``outcomes.deliveries``
    dates its label. A superseded delivery whose re-push failed still dates
    its own push; a push that never delivered is none."""
    first = await pf.push(
        session, "telegram_monitor", sent_at=at(3, 11), status="unknown"
    )
    await pf.repush(session, first, sent_at=at(5, 10))
    lost = await pf.push(
        session,
        "telegram_monitor",
        sent_at=at(3, 12),
        status="unknown",
        superseded_at=at(5, 12),
    )
    await pf.push(
        session, "telegram_monitor", sent_at=at(5, 12), status="failed", attempt=2
    )
    failed = await pf.push(
        session, "telegram_monitor", sent_at=at(3, 13), status="failed"
    )
    await session.commit()
    rows = await metrics.pushes(session, since=at(2, 0), until=at(9, 0))
    assert [(r.external_id, r.sent_at) for r in rows] == [
        (lost.external_id, at(3, 12)),
        (first.external_id, at(5, 10)),
    ]
    assert failed.external_id not in {r.external_id for r in rows}
    # Until the re-push the re-pushed id is not in the window, after it is.
    late = await metrics.pushes(session, since=at(4, 0), until=at(9, 0))
    assert [r.external_id for r in late] == [first.external_id]
    early = await metrics.pushes(session, since=at(2, 0), until=at(5, 10))
    assert [r.external_id for r in early] == [lost.external_id]


async def test_make_push_test_is_no_push_of_the_pilot(session):
    """make push-test's synthetic lead (``gates.test``, 05 §B.1) goes out on
    day 1, before the pilot (06 §7.1): it neither starts the pilot nor
    counts in a number."""
    test_push = await pf.push(session, "telegram_monitor", sent_at=at(2, 11))
    candidate = await session.get(LeadCandidate, test_push.candidate_id)
    assert candidate is not None
    candidate.score_breakdown = {"gates": {"test": True}}
    candidate.approved_by = "owner:push-test"
    real = await pf.push(session, "telegram_monitor", sent_at=at(7, 11))
    await session.commit()
    assert await metrics.pilot_start(session) == at(7, 0).date()
    rows = await metrics.pushes(session, since=at(1, 0), until=at(30, 0))
    assert [r.external_id for r in rows] == [real.external_id]
    report = await metrics.mvp_report(session, now=at(28, 12))
    assert report is not None
    assert (report.start, report.week2_start) == (at(7, 0).date(), at(14, 0))
    assert report.results.pushed == 1


async def test_unclassed_labels_are_the_dictionary_labels_no_class_matched(session):
    # One label, two spellings: shown as the ERP spelt it last.
    for reason, erp_at in (
        ("mijoz  BILAN kelishmadi", at(3, 11)),
        ("Mijoz bilan kelishmadi", at(4, 9)),
        ("Boshqa sabab", at(3, 11)),
    ):
        log = await pf.push(session, "telegram_monitor", sent_at=at(3, 11))
        await pf.judged(
            session,
            log,
            read_at=at(9, 3),
            stage_kind="lost",
            lost_reason=reason,
            at=erp_at,
        )
    # Classed labels, the setting's «never again» and a loss with no label
    # (a seller's free text) are not «unclassed».
    for reason, no_contact in (("Import qilmaydi", False), ("Qayta qilinmasin", True)):
        log = await pf.push(session, "telegram_monitor", sent_at=at(3, 11))
        await pf.judged(session, log, read_at=at(9, 3), stage_kind="lost",
                        lost_reason=reason, no_contact=no_contact)  # fmt: skip
    log = await pf.push(session, "telegram_monitor", sent_at=at(3, 11))
    await pf.judged(session, log, read_at=at(9, 3), stage_kind="lost")
    await session.commit()
    found = await metrics.unclassed_labels(session)
    assert [(u.label, u.n) for u in found] == [
        ("Mijoz bilan kelishmadi", 2),
        ("Boshqa sabab", 1),
    ]
    assert found[0].last_at == at(4, 9)


async def test_every_unclassed_label_is_kept_and_the_line_counts_the_rest(session):
    """Decision 1a: none is dropped — the line names the first ten and says
    how many more there are."""
    for i in range(12):
        log = await pf.push(session, "telegram_monitor", sent_at=at(3, 11))
        await pf.judged(
            session,
            log,
            read_at=at(9, 3),
            stage_kind="lost",
            lost_reason=f"Yangi sabab {i:02d}",
            at=at(3, 11) + timedelta(minutes=i),
        )
    await session.commit()
    found = await metrics.unclassed_labels(session)
    assert [u.label for u in found] == [f"Yangi sabab {i:02d}" for i in range(11, -1, -1)]
    [line] = cards.unclassed_lines(found)
    assert cards.UNCLASSED_SHOWN == 10
    assert "«Yangi sabab 02» 1 ta · va yana 2 ta sabab — dasturchiga" in line
    assert "Yangi sabab 01" not in line
    [whole] = cards.unclassed_lines(found[:10])
    assert "va yana" not in whole


async def test_spend_is_grouped_by_the_tashkent_day(session):
    await pf.spend(session, at(4, 23, 30), "2.40")  # 18:30 UTC, still the 4th
    await pf.spend(session, at(5, 0, 30), "0.10")  # 19:30 UTC on the 4th
    await pf.spend(session, at(5, 9), "1.00", provider="brave", operation="serp_brave")
    await session.commit()
    days = await metrics.spend_by_day(session, since=at(2, 0), until=at(9, 0))
    assert [(d.day.day, d.anthropic, d.total) for d in days] == [
        (4, Decimal("2.400000"), Decimal("2.400000")),
        (5, Decimal("0.100000"), Decimal("1.100000")),
    ]


async def test_holat_reads_the_last_30_days(session):
    old = await pf.push(session, "telegram_monitor", sent_at=NOW - timedelta(days=31))
    await pf.judged(session, old, read_at=NOW - timedelta(days=30), stage_kind="won")
    new = await pf.push(session, "telegram_monitor", sent_at=NOW - timedelta(days=2))
    await pf.judged(session, new, read_at=NOW - DAY_, stage_kind="won")
    await session.commit()
    results = await metrics.holat(session, now=NOW)
    assert (results.pushed, results.hot, results.first_shipment) == (1, 1, Ratio(1, 1))
    assert (results.since, results.until) == (NOW - timedelta(days=30), NOW)


async def test_the_pilot_by_its_numbers(session):
    """The four numbers and the two approximate ones of tests/pilot_fixtures
    .pilot, as metrics counts them (test_mvp_report prints the same)."""
    await pf.pilot(session)
    report = await metrics.mvp_report(
        session, now=pf.PILOT_NOW, start=pf.PILOT_START.date()
    )
    assert report is not None
    r = report.results
    assert (report.week2_start, report.week2_end) == (at(9, 0), at(16, 0))
    assert report.week2_hot == 22
    assert (r.pushed, r.hot) == (33, 31)
    assert (r.importer, r.precision, r.contacted) == (
        Ratio(2, 4),
        Ratio(1, 3),
        Ratio(5, 7),
    )
    assert (r.quoted, r.first_shipment) == (Ratio(2, 5), Ratio(1, 33))
    assert [(u.label, u.n) for u in r.unclassed] == [(pf.UNCLASSED, 1)]
    top_ai, top_all = report.anthropic_max, report.total_max
    assert top_ai is not None and top_all is not None
    assert (top_ai.day.day, top_ai.anthropic) == (10, Decimal("5.200000"))
    assert (top_all.day.day, top_all.total) == (10, Decimal("7.200000"))
