"""The time machine CI's time-travel job runs the suite in (07 §1.0, §2.1)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest
from time_machine import escape_hatch

from clientscrapper.config import settings
from tests import timetravel

NOW = datetime(2026, 10, 24, 15, 30, tzinfo=settings.tz)


def test_the_two_destinations():
    assert timetravel.target("newyear", now=NOW) == datetime(
        2027, 1, 1, 10, 0, tzinfo=settings.tz
    )
    assert timetravel.target("+400d", now=NOW) == datetime(
        2027, 11, 28, 15, 30, tzinfo=settings.tz
    )
    with pytest.raises(ValueError, match="CS_TIME_TRAVEL"):
        timetravel.target("tomorrow", now=NOW)


def test_the_clock_has_travelled_when_asked():
    spec = os.environ.get(timetravel.ENV)
    if not spec:
        pytest.skip("CS_TIME_TRAVEL is not set (CI's time-travel job sets it)")
    assert escape_hatch.is_travelling()
    real = escape_hatch.datetime.datetime.now(tz=settings.tz)
    travelled = datetime.now(settings.tz)
    if spec == "newyear":
        assert (travelled.month, travelled.day) == (1, 1)
        assert travelled.year == real.year + 1
    else:
        days = int(spec.strip("+d"))
        assert travelled - real >= timedelta(days=days) - timedelta(minutes=5)
