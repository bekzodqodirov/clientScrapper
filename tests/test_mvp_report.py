"""``tools/mvp_report.py`` prints 07 §3.2's four numbers from fixture data
(07 §4.1 T-36), then the two approximate rows of 01 §5 — quotes sent (01 §7
decision 5a) and won as the first-shipment proxy (3a) — and the unclassed
lost reasons (1a). The pilot is tests/pilot_fixtures.pilot: every number
below is counted by hand from its docstring.
"""

from __future__ import annotations

from datetime import date

import pytest

from clientscrapper.tools import mvp_report
from tests import pilot_fixtures as pf

EXPECTED = [
    "📊 MVP hisoboti — pilot 02.11.2026 dan, 16.11.2026 12:00 gacha",
    # 21 + the Sunday 23:30 push; the two at score 50 and day 15's are out.
    "1. ≥60 ball, telefonli push, 2-hafta (09.11–15.11): 22 ta (maqsad ≥ 20) ✅",
    # Judged (terminal): won, two not-importer losses, the unclassed loss;
    # the capped drop is no judgement. Strict: won / (won + two negatives).
    "2. Haqiqiy importyor: 50% (2/4) (maqsad ≥ 60%) ❌ · aniqlik 33% (1/3)",
    # Created and due: seven (the drop is no lead); in time: won, «Narx
    # aytildi», the unclassed loss, the Saturday push contacted on Monday.
    # The re-pushed lead was contacted after its FIRST delivery's deadline.
    "3. 1 ish kunida bogʻlanildi: 57% (4/7) (maqsad ≥ 40%) ✅ — faqat ERP koʻrgani",
    "4. Claude xarajati: eng koʻp kun $5.20 (10.11, maqsad ≤ $5.00) ❌ · hammasi eng "
    "koʻp kun $7.20 (10.11, chegara $10.00) ✅",
    "Qoʻshimcha (taxminiy):",
    # Contacted or quoted: won, the re-pushed loss, «Narx aytildi», the
    # unclassed loss, «Muzokara»; quoted: won and «Narx aytildi».
    "  Narx aytildi / bogʻlanildi: ~40% (2/5) (maqsad ≥ 25%) — taxminiy, bosqich nomidan",
    "  Yutildi / push: 3% (1/33) (maqsad ≥ 3%, 60 kun kechikadi) — taxminiy: «yutildi» "
    "birinchi yuk oʻrniga",
    "  Jami push: 33 · ≥60 ball 31",
    "⚠️ Sinflanmagan yoʻqotish sabablari: «Mijoz bilan kelishmadi» 1 ta — dasturchiga "
    "ayting, aniqlik hisobiga qoʻshilsin",
]


@pytest.fixture
async def piloted(session) -> None:
    await pf.pilot(session)


@pytest.mark.usefixtures("piloted")
async def test_mvp_report_prints_the_four_numbers(capsys):
    code = await mvp_report.run(["--start", "2026-11-02", "--now", "2026-11-16T12:00"])
    assert code == mvp_report.EXIT_OK
    assert capsys.readouterr().out.splitlines() == EXPECTED


@pytest.mark.usefixtures("piloted")
async def test_the_pilot_starts_on_the_first_push_s_day_by_default(capsys):
    """The first push went out on Tuesday 03.11: week 2 is 10.11–16.11, so
    day 15's 00:30 push is in it."""
    assert await mvp_report.run(["--now", "2026-11-17T12:00+05:00"]) == mvp_report.EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [
        "📊 MVP hisoboti — pilot 03.11.2026 dan, 17.11.2026 12:00 gacha",
        "1. ≥60 ball, telefonli push, 2-hafta (10.11–16.11): 23 ta (maqsad ≥ 20) ✅",
    ]


@pytest.mark.usefixtures("piloted")
async def test_read_inside_week_two_counts_what_was_sent_so_far(capsys):
    """Read on Sunday at noon, the Sunday 23:30 push has not happened yet."""
    assert (
        await mvp_report.run(["--start", "2026-11-02", "--now", "2026-11-15T12:00"]) == 0
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == (
        "1. ≥60 ball, telefonli push, 2-hafta (09.11–15.11): 21 ta (maqsad ≥ 20) ✅"
    )


async def test_nothing_to_report_before_the_first_push(session, capsys):
    text = await mvp_report.report_text(now=pf.PILOT_NOW)
    assert text is None
    started = await mvp_report.report_text(now=pf.PILOT_NOW, start=date(2026, 11, 2))
    assert started is not None
    assert started.splitlines()[1:5] == [
        "1. ≥60 ball, telefonli push, 2-hafta (09.11–15.11): 0 ta (maqsad ≥ 20) ❌",
        "2. Haqiqiy importyor: — (maqsad ≥ 60%) — · aniqlik —",
        "3. 1 ish kunida bogʻlanildi: — (maqsad ≥ 40%) — — faqat ERP koʻrgani",
        "4. Xarajat: hali yoʻq",
    ]


def test_the_empty_answer_exits_1(monkeypatch, capsys):
    async def nothing(**_: object) -> None:
        return None

    monkeypatch.setattr(mvp_report, "report_text", nothing)
    assert mvp_report.main([]) == mvp_report.EXIT_EMPTY
    assert capsys.readouterr().out.strip() == mvp_report.NOTHING_YET


def test_a_bad_date_is_refused():
    with pytest.raises(SystemExit) as stopped:
        mvp_report.parse_args(["--start", "02.11.2026"])
    assert stopped.value.code == 2
