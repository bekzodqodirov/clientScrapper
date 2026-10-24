"""Container healthcheck: «healthy» is «a fresh heartbeat» (06 §4.1).

    python -m clientscrapper.tools.healthcheck worker|bot|userbot

Docker runs this inside each app container (``docker-compose.yml``), so
``make ps`` reads «healthy» exactly when the process's heartbeat row is
younger than its threshold — ``HEARTBEAT_STALE_MINUTES``, or
``USERBOT_STALE_MINUTES`` for the reader — or the reader says it is switched
off on purpose. The verdict is ``health.judge`` / ``health.healthy``, the
same rule the health job alerts on, so a container Docker calls unhealthy is
one the owner's alert calls silent; there is no second definition here.

One short read of one row through a plain psycopg connection (no pool, no
event loop), a short connect timeout, and one line on stdout for ``docker
inspect``. Exit codes are Docker's: 0 healthy, 1 unhealthy (a stale or
missing beat, or the database unreadable); 2 is a bad argument.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
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


def verdict(
    component: str,
    last_seen_at: datetime | None,
    detail: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> tuple[bool, str]:
    """(healthy, the one line ``docker inspect`` shows)."""
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
) -> int:
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
        component, last_seen_at, detail, now=now or datetime.now(settings.tz)
    )
    sys.stdout.write(line + "\n")
    return EXIT_HEALTHY if ok else EXIT_UNHEALTHY


if __name__ == "__main__":
    raise SystemExit(main())
