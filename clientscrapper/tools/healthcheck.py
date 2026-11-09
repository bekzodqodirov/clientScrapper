"""Container healthcheck: «healthy» is «a fresh heartbeat» (06 §4.1).

    python -m clientscrapper.tools.healthcheck worker|bot|userbot

Docker runs this inside each app container (``docker-compose.yml``), so
``make ps`` reads «healthy» exactly when the process's heartbeat row is
younger than its threshold — ``HEARTBEAT_STALE_MINUTES``, or
``USERBOT_STALE_MINUTES`` for the reader — or the reader says it is switched
off on purpose. The verdict is ``health.judge`` / ``health.healthy``, the
same rule the health job alerts on, so a container Docker calls unhealthy is
one the owner's alert calls silent; there is no second definition here.

A beat counts only when the process this container runs wrote it: one
older than the container's PID 1 is the previous container's. ``make reload``
recreates the three services, and for up to the stale threshold the old
container's last beat read «healthy» over a new reader that had not beaten
once (the docker re-run: it hung in ``get_me`` and turned unhealthy only at
five minutes). The start is PID 1's ``starttime`` against the boot time in
``/proc``; where /proc cannot say (not Linux), every beat counts as before.

One short read of one row through a plain psycopg connection (no pool, no
event loop), a short connect timeout, and one line on stdout for ``docker
inspect``. Exit codes are Docker's: 0 healthy, 1 unhealthy (a stale or
missing beat, one from before this process started, or the database
unreadable); 2 is a bad argument.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from clientscrapper.config import settings
from clientscrapper.services import health

EXIT_HEALTHY: Final = 0
EXIT_UNHEALTHY: Final = 1
EXIT_USAGE: Final = 2
CONNECT_TIMEOUT_SECONDS: Final = 5
# The processes a container runs; jobs and collectors are not containers.
COMPONENTS: Final = health.COMPONENTS


def _libpq(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def read_heartbeat(
    url: str, component: str
) -> tuple[datetime | None, Mapping[str, Any] | None]:
    """The component's ``last_seen_at`` and ``detail``; (None, None) when it
    never beat. Raises when the database cannot be read."""
    import psycopg

    with psycopg.connect(_libpq(url), connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
        row = conn.execute(
            "SELECT last_seen_at, detail FROM heartbeats WHERE component = %s",
            (component,),
        ).fetchone()
    if row is None:
        return None, None
    last_seen_at, detail = row
    return last_seen_at, detail if isinstance(detail, Mapping) else {}


def process_started_at(proc: Path = Path("/proc")) -> datetime | None:
    """When this container's PID 1 — the process the healthcheck vouches
    for — started; None when /proc cannot say."""
    try:
        stat = (proc / "1" / "stat").read_text()
        boot = next(
            int(line.split()[1])
            for line in (proc / "stat").read_text().splitlines()
            if line.startswith("btime ")
        )
        # The fields after «(comm)», which may itself hold spaces and «)»;
        # the first is field 3 (state), so field 22 (starttime) is index 19.
        ticks = int(stat.rsplit(")", 1)[1].split()[19])
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return datetime.fromtimestamp(boot + ticks / hz, tz=UTC)


def verdict(
    component: str,
    last_seen_at: datetime | None,
    detail: Mapping[str, Any] | None,
    *,
    now: datetime,
    started_at: datetime | None = None,
) -> tuple[bool, str]:
    """(healthy, the one line ``docker inspect`` shows). ``started_at``: when
    the container's process started; a beat from before it is not its own."""
    if started_at is not None and last_seen_at is not None and last_seen_at < started_at:
        minutes = int(max(now - last_seen_at, timedelta(0)).total_seconds() // 60)
        return False, (
            f"{component}: no heartbeat from this process yet (the last one, "
            f"{minutes} min ago, is from before it started)"
        )
    judged = health.judge(component, last_seen_at, detail, now=now)
    # The verdict is health.healthy's alone; the branches below only word it.
    ok = health.healthy(judged)
    limit = int(health.stale_after(component).total_seconds() // 60)
    if judged.age is None:
        return ok, f"{component}: no heartbeat yet"
    minutes = int(judged.age.total_seconds() // 60)
    if not ok:
        return ok, f"{component}: last beat {minutes} min ago (limit {limit} min)"
    if judged.disabled:
        return ok, f"{component}: switched off on purpose (beat {minutes} min ago)"
    return ok, f"{component}: beat {minutes} min ago (limit {limit} min)"


def main(
    argv: Sequence[str] | None = None,
    *,
    now: datetime | None = None,
    database_url: str | None = None,
    started_at: Callable[[], datetime | None] | None = None,
) -> int:
    """``started_at``: when the container's process started (default: PID 1
    in /proc; tests hand in their own clock's answer)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in COMPONENTS:
        sys.stdout.write(f"usage: healthcheck {{{'|'.join(COMPONENTS)}}}\n")
        return EXIT_USAGE
    component = args[0]
    try:
        last_seen_at, detail = read_heartbeat(
            database_url or settings.database_url, component
        )
    except Exception as exc:
        # The class only: a connection error's message may carry the DSN.
        sys.stdout.write(f"{component}: heartbeat unreadable ({type(exc).__name__})\n")
        return EXIT_UNHEALTHY
    ok, line = verdict(
        component,
        last_seen_at,
        detail,
        now=now or datetime.now(settings.tz),
        started_at=(started_at or process_started_at)(),
    )
    sys.stdout.write(line + "\n")
    return EXIT_HEALTHY if ok else EXIT_UNHEALTHY


if __name__ == "__main__":
    raise SystemExit(main())
