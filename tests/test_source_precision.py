"""The weekly precision job (07 §4.1 T-36; 05 §B.2, 02 §2.1, 06 §4.2).

``auto_push`` is earned at ``pushes_30d ≥ 15 AND precision_30d ≥ 0.60`` and
lost below either, flipped by the job both ways — and never ON while the
owner forbade it (``/manbalar`` → ``src:<key>:a``). A source whose precision
fell under 20 % over ≥ 30 pushes is paused for a week and the owner told
(``source_low_precision``). The numbers come from real pushes and the real
``record_page`` (tests/pilot_fixtures.py); ``sources`` is configuration, put
back after every test.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from clientscrapper.db.models import RuntimeFlag, Source
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import erp_feeds, health, owner
from clientscrapper.services.erp_feeds import Precision
from tests import pilot_fixtures as pf
from tests.pilot_fixtures import sources_kept  # noqa: F401  (fixture)

TZ = ZoneInfo("Asia/Tashkent")
# The Sunday 04:00 run.
NOW = datetime(2026, 11, 1, 4, 0, tzinfo=TZ)
DAY = timedelta(days=1)

pytestmark = pytest.mark.usefixtures("sources_kept")


async def pushes(
    session,
    source: str,
    *,
    good: int = 0,
    bad: int = 0,
    neutral: int = 0,
    pending: int = 0,
    read_at: datetime = NOW - DAY,
) -> None:
    """``good + bad + neutral + pending`` live pushes of ``source`` in the
    window: won (positive), lost «Import qilmaydi» (negative), lost «Boshqa»
    (neutral) and untouched (pending) outcomes, read at ``read_at``."""
    sent = NOW - 10 * DAY
    for kind, n in (
        ("good", good),
        ("bad", bad),
        ("neutral", neutral),
        ("pending", pending),
    ):
        for _ in range(n):
            log = await pf.push(session, source, sent_at=sent)
            if kind == "good":
                await pf.judged(session, log, read_at=read_at, stage_kind="won")
            elif kind == "bad":
                await pf.judged(
                    session, log, read_at=read_at, stage_kind="lost",
                    lost_reason="Import qilmaydi",
                )  # fmt: skip
            elif kind == "neutral":
                await pf.judged(
                    session, log, read_at=read_at, stage_kind="lost", lost_reason="Boshqa"
                )
            else:
                await pf.judged(session, log, read_at=read_at)
    await session.commit()


async def row(session, key: str) -> Source:
    found = await session.get(Source, key, populate_existing=True)
    assert found is not None
    return found


async def forbid(session, key: str) -> None:
    assert await owner.forbid_auto_push(session, key) is True
    await session.commit()


async def enable(session, *keys: str, on: bool = True) -> None:
    """``sources.enabled`` is the owner's (the fixture puts it back)."""
    await session.execute(
        sa.update(Source).where(Source.key.in_(keys)).values(enabled=on)
    )
    await session.commit()


async def problem(session) -> dict | None:
    flag = await session.get(
        RuntimeFlag,
        health.PROBLEM_FLAG_PREFIX + "source_low_precision",
        populate_existing=True,
    )
    return None if flag is None else flag.value


# --- the rule at its edges (pure) --------------------------------------------------


@pytest.mark.parametrize(
    ("positives", "negatives", "pushes", "forbidden", "due"),
    [
        (3, 2, 15, False, True),  # exactly 0.60 over exactly 15
        (3, 2, 14, False, False),  # one push short
        (4, 3, 15, False, False),  # 0.5714
        (149, 100, 15, False, False),  # 0.5984: rounds to the column, still short
        (5, 0, 40, False, True),
        (5, 0, 40, True, False),  # forbidden: never on, whatever the numbers
        (0, 0, 40, False, False),  # nothing judged: no precision, never due
    ],
)
def test_auto_push_due_at_the_thresholds(positives, negatives, pushes, forbidden, due):
    numbers = Precision("x", positives=positives, negatives=negatives, pushes=pushes)
    assert erp_feeds.auto_push_due(numbers, forbidden=forbidden) is due


@pytest.mark.parametrize(
    ("positives", "negatives", "pushes", "low"),
    [
        (1, 9, 30, True),  # 0.10 over 30
        (1, 9, 29, False),  # too few pushes to judge
        (1, 4, 30, False),  # exactly 0.20 is not below it
        (19, 81, 30, True),  # 0.19
        (0, 0, 60, False),  # nothing judged
    ],
)
def test_low_precision_at_the_thresholds(positives, negatives, pushes, low):
    numbers = Precision("x", positives=positives, negatives=negatives, pushes=pushes)
    assert erp_feeds.low_precision(numbers) is low


def test_the_thresholds_are_05_s():
    assert (
        15,
        Decimal("0.60"),
    ) == (erp_feeds.AUTO_PUSH_MIN_PUSHES, erp_feeds.AUTO_PUSH_MIN_PRECISION)
    assert (30, Decimal("0.20"), timedelta(days=7)) == (
        erp_feeds.LOW_PRECISION_MIN_PUSHES,
        erp_feeds.LOW_PRECISION_BELOW,
        erp_feeds.LOW_PRECISION_PAUSE,
    )


# --- the job --------------------------------------------------------------------------


async def test_precision_and_autopush_flip(session):
    """Both flips at the thresholds, a forbidden source never on."""
    # On at exactly 15 pushes and 60 %; neutral and pending outcomes are no
    # evidence either way.
    await pushes(session, "telegram_monitor", good=3, bad=2, neutral=6, pending=4)
    # One push short of 15 with a perfect record: stays off.
    await pushes(session, "olx_business", good=14)
    # 15 pushes, 4/7 = 57 %: stays off.
    await pushes(session, "hh_vacancies", good=4, bad=3, pending=8)
    # The owner forbade it: perfect numbers, never on.
    await pushes(session, "exhibitors", good=20)
    await forbid(session, "exhibitors")

    run = await erp_feeds.source_precision_job(SessionLocal, NOW)
    tg, olx, hh, ex = [
        await row(session, k)
        for k in ("telegram_monitor", "olx_business", "hh_vacancies", "exhibitors")
    ]
    assert (tg.auto_push, tg.precision_30d, tg.pushes_30d, tg.positives_30d) == (
        True,
        Decimal("0.6000"),
        15,
        3,
    )
    assert (olx.auto_push, olx.precision_30d, olx.pushes_30d) == (
        False,
        Decimal("1.0000"),
        14,
    )
    assert (hh.auto_push, hh.precision_30d, hh.pushes_30d) == (
        False,
        Decimal("0.5714"),
        15,
    )
    assert (ex.auto_push, ex.precision_30d, ex.pushes_30d) == (
        False,
        Decimal("1.0000"),
        20,
    )
    assert ex.config[erp_feeds.AUTO_PUSH_FORBIDDEN] is True
    assert [f.as_dict() for f in run.flips] == [
        {
            "key": "telegram_monitor",
            "title": tg.title_uz,
            "on": True,
            "pct": 60,
            "pushes": 15,
        }
    ]
    assert run.paused == []

    # A week later the same source fell to 3/6: the job takes it back.
    await pushes(session, "telegram_monitor", bad=1, read_at=NOW + 6 * DAY)
    again = await erp_feeds.source_precision_job(SessionLocal, NOW + 7 * DAY)
    tg = await row(session, "telegram_monitor")
    assert (tg.auto_push, tg.precision_30d) == (False, Decimal("0.5000"))
    assert [(f.key, f.on, f.pct) for f in again.flips] == [
        ("telegram_monitor", False, 50)
    ]
    # Nothing moved on the third run: no flip, and the stored flips say so.
    third = await erp_feeds.source_precision_job(SessionLocal, NOW + 7 * DAY)
    assert third.flips == []
    assert await erp_feeds.precision_flips(session, since=NOW + 6 * DAY) == []


async def test_a_forbidden_source_found_on_is_turned_off(session):
    """A row that is somehow on while forbidden (a hand edit) goes off."""
    await pushes(session, "exhibitors", good=20)
    await session.execute(
        sa.update(Source)
        .where(Source.key == "exhibitors")
        .values(auto_push=True, config={erp_feeds.AUTO_PUSH_FORBIDDEN: True})
    )
    await session.commit()
    run = await erp_feeds.source_precision_job(SessionLocal, NOW)
    assert (await row(session, "exhibitors")).auto_push is False
    assert [(f.key, f.on) for f in run.flips] == [("exhibitors", False)]


async def test_the_turn_on_update_re_reads_the_forbid_itself(session, monkeypatch):
    """The SQL guard on its own: a row the job's predicate thinks allowed but
    whose stored config says forbidden is never set on."""
    await pushes(session, "telegram_monitor", good=15)
    await forbid(session, "telegram_monitor")
    monkeypatch.setattr(erp_feeds, "forbidden_of", lambda config: False)
    run = await erp_feeds.source_precision_job(SessionLocal, NOW)
    assert (await row(session, "telegram_monitor")).auto_push is False
    assert run.flips == []


async def test_the_flips_are_kept_for_the_digest(session):
    await pushes(session, "telegram_monitor", good=15)
    await erp_feeds.source_precision_job(SessionLocal, NOW)
    flips = await erp_feeds.precision_flips(session, since=NOW - DAY)
    assert [(f.key, f.on, f.pct, f.pushes) for f in flips] == [
        ("telegram_monitor", True, 100, 15)
    ]
    # The digest of two days later reads nothing new.
    assert (
        await erp_feeds.precision_flips(session, since=NOW + timedelta(minutes=1)) == []
    )


async def test_a_low_precision_source_is_paused_for_a_week_and_the_owner_told(session):
    await enable(session, "olx_business", "hh_vacancies")
    await pushes(session, "olx_business", good=3, bad=27)  # 10 % over 30
    await pushes(session, "hh_vacancies", good=1, bad=9, pending=19)  # 29 pushes
    run = await erp_feeds.source_precision_job(SessionLocal, NOW)
    olx, hh = await row(session, "olx_business"), await row(session, "hh_vacancies")
    assert (olx.paused_until, olx.paused_reason) == (NOW + 7 * DAY, "low_precision")
    assert (hh.paused_until, hh.paused_reason) == (None, None)
    assert run.paused == ["olx_business"]
    raised = await problem(session)
    assert raised is not None
    assert raised["detail"] == {
        "sources": [{"key": "olx_business", "title": olx.title_uz, "pct": "10"}]
    }
    assert raised["until"] == (NOW + 7 * DAY).isoformat()
    # The owner reads it as 06 §4.2 words it.
    status = await health.gather(session, now=NOW)
    shown = [p for p in health.problems(status) if p.key == "source_low_precision"]
    assert [p.text for p in shown] == [
        f"⚠️ {olx.title_uz} aniqligi 10% — bir haftaga toʻxtatildi "
        "(/manbalar bilan yoqiladi)"
    ]
    # A week on, with nothing low any more, the problem is cleared.
    await session.execute(
        sa.update(Source).where(Source.key == "olx_business").values(paused_until=None)
    )
    await pushes(session, "olx_business", good=40, read_at=NOW + 6 * DAY)
    later = await erp_feeds.source_precision_job(SessionLocal, NOW + 7 * DAY)
    assert later.paused == []
    assert await problem(session) is None
    assert (await row(session, "olx_business")).paused_until is None


async def test_a_switched_off_source_is_neither_paused_nor_named(session):
    """A source the owner switched off runs nothing a pause could stop, and
    switching it back on is his say (06 §4.2's alert names what it paused)."""
    await enable(session, "olx_business", on=False)
    await pushes(session, "olx_business", good=1, bad=29)
    run = await erp_feeds.source_precision_job(SessionLocal, NOW)
    olx = await row(session, "olx_business")
    assert (olx.enabled, olx.paused_until, olx.paused_reason) == (False, None, None)
    assert run.paused == []
    assert await problem(session) is None


async def test_a_longer_pause_is_never_shortened(session):
    await enable(session, "olx_business")
    await pushes(session, "olx_business", good=1, bad=29)
    held = NOW + 20 * DAY
    await session.execute(
        sa.update(Source)
        .where(Source.key == "olx_business")
        .values(paused_until=held, paused_reason="host_blocked:www.olx.uz")
    )
    await session.commit()
    run = await erp_feeds.source_precision_job(SessionLocal, NOW)
    olx = await row(session, "olx_business")
    assert (olx.paused_until, olx.paused_reason) == (held, "host_blocked:www.olx.uz")
    assert run.paused == ["olx_business"]  # still reported: its numbers are low


async def test_the_window_is_30_days(session):
    """A push sent more than 30 days ago is not this month's volume, an
    outcome first read more than 30 days ago is not this month's evidence."""
    for _ in range(15):
        log = await pf.push(session, "telegram_monitor", sent_at=NOW - 31 * DAY)
        await pf.judged(session, log, read_at=NOW - 31 * DAY, stage_kind="won")
    await session.commit()
    await erp_feeds.source_precision_job(SessionLocal, NOW)
    tg = await row(session, "telegram_monitor")
    assert (tg.auto_push, tg.precision_30d, tg.pushes_30d) == (False, None, 0)


async def test_the_owner_forbid_and_the_job_share_one_rule():
    assert owner.forbidden_of is erp_feeds.forbidden_of
    assert (
        owner.AUTO_PUSH_FORBIDDEN
        == erp_feeds.AUTO_PUSH_FORBIDDEN
        == "auto_push_forbidden"
    )
    assert erp_feeds.forbidden_of({"auto_push_forbidden": True}) is True
    assert erp_feeds.forbidden_of({"auto_push_forbidden": "true"}) is False
    assert erp_feeds.forbidden_of(None) is False


async def test_the_worker_slot_runs_the_job(session):
    from clientscrapper.worker import main as worker

    await pushes(session, "telegram_monitor", good=15)
    done = await worker.source_precision_job(now=NOW)
    assert done["flips"] == [
        {
            "key": "telegram_monitor",
            "title": (await row(session, "telegram_monitor")).title_uz,
            "on": True,
            "pct": 100,
            "pushes": 15,
        }
    ]
    assert (await row(session, "telegram_monitor")).auto_push is True


async def test_one_tap_on_the_paused_row_resumes_it_and_its_alert_line_goes(session):
    """06 §4.2 «/manbalar bilan yoqiladi»: one tap on the ⏸ row the job holds
    resumes the source (it stays on) and takes its line out of the open
    alert — cleared with the last one; a breaker's pause is not the owner's
    switch to end, and a tap on that row switches it off as it says."""
    await enable(session, "olx_business", "hh_vacancies")
    await pushes(session, "olx_business", good=1, bad=29)
    await pushes(session, "hh_vacancies", good=2, bad=28)
    await erp_feeds.source_precision_job(SessionLocal, NOW)
    keys = ("olx_business", "hh_vacancies")
    titles = {k: (await row(session, k)).title_uz for k in keys}
    assert [i["key"] for i in (await problem(session) or {})["detail"]["sources"]] == [
        "hh_vacancies",
        "olx_business",
    ]
    tap = NOW + DAY
    assert await owner.toggle_source(session, "olx_business", now=tap) is True
    await session.commit()
    olx = await row(session, "olx_business")
    assert (olx.enabled, olx.paused_until, olx.paused_reason) == (True, None, None)
    raised = await problem(session)
    assert raised is not None
    assert raised["detail"] == {
        "sources": [{"key": "hh_vacancies", "title": titles["hh_vacancies"], "pct": "6"}]
    }
    assert raised["until"] == (NOW + 7 * DAY).isoformat()
    assert raised["since"] == NOW.isoformat()
    assert await owner.toggle_source(session, "hh_vacancies", now=tap) is True
    await session.commit()
    assert await problem(session) is None
    # A second tap is an ordinary switch: off.
    assert await owner.toggle_source(session, "olx_business", now=tap) is False
    # A breaker-held row: the tap switches it off, the breaker's pause stays,
    # and switching it back on leaves that pause too.
    await session.execute(
        sa.update(Source)
        .where(Source.key == "hh_vacancies")
        .values(paused_until=tap + DAY, paused_reason="host_blocked:hh.uz")
    )
    await session.commit()
    assert await owner.toggle_source(session, "hh_vacancies", now=tap) is False
    assert await owner.toggle_source(session, "hh_vacancies", now=tap) is True
    await session.commit()
    hh = await row(session, "hh_vacancies")
    assert (hh.paused_until, hh.paused_reason) == (tap + DAY, "host_blocked:hh.uz")


async def test_a_low_precision_pause_that_ran_out_is_no_hold(session):
    """Past ``paused_until`` the row is ✅ again: a tap is the ordinary off."""
    await enable(session, "olx_business")
    await pushes(session, "olx_business", good=1, bad=29)
    await erp_feeds.source_precision_job(SessionLocal, NOW)
    after = NOW + 7 * DAY
    assert await owner.toggle_source(session, "olx_business", now=after) is False
    assert await owner.toggle_source(session, "olx_business", now=after) is True
    await session.commit()
    olx = await row(session, "olx_business")
    assert (olx.enabled, olx.paused_until, olx.paused_reason) == (True, None, None)


async def test_a_switched_off_paused_source_switched_on_runs_again(session):
    """A row the job paused that the owner then switched off (an old row):
    switching it on ends the pause as well."""
    await enable(session, "olx_business")
    await pushes(session, "olx_business", good=1, bad=29)
    await erp_feeds.source_precision_job(SessionLocal, NOW)
    await enable(session, "olx_business", on=False)
    assert await owner.toggle_source(session, "olx_business", now=NOW + DAY) is True
    await session.commit()
    olx = await row(session, "olx_business")
    assert (olx.enabled, olx.paused_until, olx.paused_reason) == (True, None, None)
    assert await problem(session) is None
