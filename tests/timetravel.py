# VENDORED_FROM dailyJournal@6b1f67f tests/timetravel.py
"""Run the suite at another moment, to catch tests that rot on a calendar date.

Switched on by the environment, from the root conftest's ``pytest_configure``
(07 §1.0, the CI ``time-travel`` job):

    CS_TIME_TRAVEL=newyear   the next 1 January, 10:00 Asia/Tashkent
    CS_TIME_TRAVEL=+400d     now + 400 days

It is a plain module, not a pytest plugin: ``pytest -p tests.timetravel``
fails because pytest imports ``-p`` plugins before the repository root is on
``sys.path``.

For runs WITHOUT a database only. Python's clock travels; PostgreSQL's
``now()`` does not, so database tests would compare two different "nows".
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

import time_machine

from clientscrapper.config import settings

ENV = "CS_TIME_TRAVEL"
_traveller: time_machine.travel | None = None


def target(spec: str, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(settings.tz)
    if spec == "newyear":
        return datetime(now.year + 1, 1, 1, 10, 0, tzinfo=settings.tz)
    days = re.fullmatch(r"\+(\d+)d", spec)
    if days:
        return now + timedelta(days=int(days.group(1)))
    raise ValueError(f"{ENV} must be 'newyear' or '+Nd', not {spec!r}")


def start() -> None:
    global _traveller
    if _traveller is not None:
        return
    _traveller = time_machine.travel(target(os.environ[ENV]), tick=True)
    _traveller.start()


def stop() -> None:
    global _traveller
    if _traveller is not None:
        _traveller.stop()
        _traveller = None
