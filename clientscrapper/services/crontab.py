"""Standard crontab expressions, read the way the spec writes them.

Every cadence in 03 §3, 04 §0, 05 and 06 — and every ``sources.cron`` the
owner edits from ``/manbalar`` — is a five-field crontab in Asia/Tashkent,
where day-of-week 0 (and 7) is **Sunday**: 05 §B.2's quarter walk is «Sunday
``20 3 * * 0``», 03 §3's exhibitors run Monday ``0 9 * * 1``. APScheduler 3's
``CronTrigger.from_crontab`` reads the same digits as its own weekday numbers,
where 0 is **Monday** — ``20 3 * * 0`` would fire on Mondays, every weekly job
a day late, and nothing would say so. So the scheduler and the health check
never call ``from_crontab`` on a spec string; they call :func:`trigger`, which
rewrites the day-of-week field into weekday names first.

Refused rather than guessed: anything but five fields, and an expression that
restricts both day-of-month and day-of-week (standard cron ORs the two,
APScheduler ANDs them — none of the spec's crons needs it).

Pure: no clock, no settings, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta, tzinfo
from typing import Final

from apscheduler.triggers.cron import CronTrigger

# Standard cron numbering: 0 and 7 are Sunday.
_DAY_NAMES: Final = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_NAMES: Final = frozenset(_DAY_NAMES)
_TOKEN: Final = re.compile(r"(?P<lo>\*|\d+)(?:-(?P<hi>\d+))?(?:/(?P<step>\d+))?")
_NAME_TOKEN: Final = re.compile(r"[a-z]{3}(?:-[a-z]{3})?")


class CrontabError(ValueError):
    """A crontab expression this module will not translate."""


def _days(token: str) -> list[int]:
    match = _TOKEN.fullmatch(token)
    if match is None:
        raise CrontabError(f"day-of-week token {token!r}")
    lo, hi, step = match["lo"], match["hi"], match["step"]
    if lo == "*":
        if hi is not None:
            raise CrontabError(f"day-of-week token {token!r}")
        first, last = 0, 6
    else:
        first = int(lo)
        last = int(hi) if hi is not None else (6 if step is not None else first)
    stride = int(step) if step is not None else 1
    if not (0 <= first <= 7 and 0 <= last <= 7 and first <= last and stride >= 1):
        raise CrontabError(f"day-of-week token {token!r}")
    return [day % 7 for day in range(first, last + 1, stride)]


def day_of_week(field: str) -> str:
    """The day-of-week field with every number spelled as a name: «0» →
    «sun», «1-6» → «mon,tue,wed,thu,fri,sat», «*/2» → «sun,tue,thu,sat».
    Names (``sun``, ``mon-fri``) and a bare ``*`` pass unchanged."""
    if field == "*":
        return field
    out: list[str] = []
    for token in field.lower().split(","):
        if _NAME_TOKEN.fullmatch(token):
            parts = token.split("-")
            if not all(part in _NAMES for part in parts):
                raise CrontabError(f"day-of-week name {token!r}")
            out.append(token)
            continue
        for day in _days(token):
            name = _DAY_NAMES[day]
            if name not in out:
                out.append(name)
    if not out:
        raise CrontabError(f"day-of-week field {field!r}")
    return ",".join(out)


def trigger(expr: str, tz: tzinfo) -> CronTrigger:
    """The APScheduler trigger for a standard five-field crontab in ``tz``."""
    fields = expr.split()
    if len(fields) != 5:
        raise CrontabError(f"a crontab has five fields: {expr!r}")
    minute, hour, day, month, dow = fields
    if day != "*" and dow != "*":
        raise CrontabError(f"day-of-month and day-of-week both set: {expr!r}")
    try:
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week(dow),
            timezone=tz,
        )
    except ValueError as exc:
        raise CrontabError(f"{expr!r}: {exc}") from None


def fires(
    expr: str, *, after: datetime, until: datetime, tz: tzinfo, limit: int = 1000
) -> Iterator[datetime]:
    """The fire times of ``expr`` in (``after``, ``until``], oldest first, at
    most ``limit`` of them."""
    cron = trigger(expr, tz)
    previous: datetime | None = None
    moment = after + timedelta(seconds=1)
    for _ in range(limit):
        nxt = cron.get_next_fire_time(previous, moment)
        if nxt is None or nxt > until:
            return
        yield nxt
        previous, moment = nxt, nxt + timedelta(seconds=1)
