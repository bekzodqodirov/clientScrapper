# VENDORED_FROM dailyJournal@6b1f67f miya/services/restore_drill.py
"""The weekly restore drill (06 §4.4): a backup is proven only when it restores.

The newest encrypted backup is restored into a scratch database
(``clientscrapper_restore_drill``), the row counts of ``COUNTED_TABLES`` are
compared with what the backup job counted at dump time (the
``backup_counts`` heartbeat), and the scratch database is dropped whatever
happens. The outcome is the ``restore_drill`` heartbeat, which ``/holat``
shows and ``health.problems`` judges (``restore_drill_failed``). Blocking work
(psql, age, pg_restore) runs in a thread.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, LiteralString
from urllib.parse import urlsplit, urlunsplit

from clientscrapper.config import settings
from clientscrapper.services import backup

log = logging.getLogger(__name__)

DRILL_DATABASE: Final = "clientscrapper_restore_drill"
COMPONENT: Final = "restore_drill"
COUNTS_COMPONENT: Final = "backup_counts"
# What the backup job counts at dump time, and the drill after restoring
# (06 §4.4).
COUNTED_TABLES: Final = (
    "companies",
    "contacts",
    "lead_candidates",
    "push_log",
    "suppression",
    "outcomes",
)
_NAME: Final = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_DROP: Final = "DROP DATABASE IF EXISTS {} WITH (FORCE)"
# One drill at a time across processes (a session advisory lock in the main
# database): the scratch database has one fixed name, so that a drill killed
# half-way leaves nothing the next one does not drop, and two at once would
# DROP … WITH (FORCE) each other's.
DRILL_LOCK: Final = "cs:restore_drill"


class DrillBusy(Exception):
    """Another process is running the drill."""


@dataclass(slots=True)
class DrillResult:
    ok: bool
    skipped: str | None = None
    error: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    expected: dict[str, int] | None = None
    seconds: float = 0.0
    path: str | None = None

    def detail(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "error": self.error,
            "counts": self.counts,
            "expected": self.expected,
            "seconds": round(self.seconds, 1),
            "path": self.path,
        }


def _libpq(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def with_database(url: str, name: str) -> str:
    parts = urlsplit(_libpq(url))
    return urlunsplit(parts._replace(path=f"/{name}"))


def count_rows(url: str) -> dict[str, int]:
    """Row counts of the tables a restore must bring back."""
    import psycopg
    from psycopg import sql

    counts: dict[str, int] = {}
    with psycopg.connect(_libpq(url), connect_timeout=10) as conn:
        for table in COUNTED_TABLES:
            try:
                with conn.transaction():
                    row = conn.execute(
                        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                    ).fetchone()
            except psycopg.errors.UndefinedTable:
                continue
            counts[table] = int(row[0]) if row is not None else 0
    return counts


def _admin(url: str, template: LiteralString, database: str) -> None:
    """One DDL statement about ``database``, its name quoted as an identifier."""
    import psycopg
    from psycopg import sql

    statement = sql.SQL(template).format(sql.Identifier(database))
    with psycopg.connect(_libpq(url), autocommit=True, connect_timeout=10) as conn:
        conn.execute(statement)


def _restore(file: Path, identity: Path, target: str) -> None:
    from clientscrapper.tools import restore

    # Quiet: pg_restore's own lines may carry row values and this runs in
    # the worker, whose stderr is the container log (06 §4.5).
    code = restore.restore_into(target, file, identity, quiet=True)
    if code != restore.EXIT_OK:
        raise RuntimeError(f"restore exited with {code}")


def drill_sync(file: Path, identity: Path, url: str, database: str) -> dict[str, int]:
    """Create ``database``, restore into it, count, and drop it — the drop in
    a ``finally``, so a failed restore leaves no scratch database behind."""
    if not _NAME.fullmatch(database):
        raise ValueError(f"not a database name: {database!r}")
    import psycopg

    target = with_database(url, database)
    # The lock lives as long as this connection: a killed drill releases it.
    with psycopg.connect(_libpq(url), autocommit=True, connect_timeout=10) as lock:
        row = lock.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))", (DRILL_LOCK,)
        ).fetchone()
        if row is None or row[0] is not True:
            raise DrillBusy(DRILL_LOCK)
        _admin(url, _DROP, database)
        _admin(url, "CREATE DATABASE {}", database)
        try:
            _restore(file, identity, target)
            return count_rows(target)
        finally:
            try:
                _admin(url, _DROP, database)
            except Exception as exc:
                log.error(
                    "the drill database was not dropped",
                    extra={"error": type(exc).__name__},
                )


def close_enough(got: int | None, want: int) -> bool:
    """The counts are taken just after the dump, so a few rows may have
    arrived (or been purged) in between; a broken restore is far off."""
    if got is None:
        return False
    return abs(got - want) <= max(5, want // 100)


async def run(
    *,
    identity: Path | None = None,
    expected: dict[str, int] | None = None,
    database: str = DRILL_DATABASE,
    directory: Path | None = None,
    database_url: str | None = None,
) -> DrillResult:
    """Restore the newest backup into a scratch database and compare."""
    from clientscrapper.services import health

    free, _ = health.disk_usage()
    if health.is_disk_low(free):
        return DrillResult(ok=True, skipped="disk_low")
    key = identity if identity is not None else backup.default_identity()
    if key is None or not key.is_file():
        log.warning("restore drill skipped: no age identity")
        return DrillResult(ok=True, skipped="no_key")
    newest = await backup.newest_backup(directory)
    if newest is None:
        return DrillResult(ok=True, skipped="no_backup")
    started = time.monotonic()
    result = DrillResult(ok=False, path=str(newest), expected=expected)
    try:
        result.counts = await asyncio.to_thread(
            drill_sync, newest, key, database_url or settings.database_url, database
        )
        mismatch = {
            table: (want, result.counts.get(table))
            for table, want in (expected or {}).items()
            if not close_enough(result.counts.get(table), want)
        }
        if mismatch:
            result.error = "counts differ: " + ", ".join(
                f"{t} {want}≠{got}" for t, (want, got) in mismatch.items()
            )
        else:
            result.ok = True
    except DrillBusy:
        # Its beat is the other drill's to write.
        log.info("restore drill skipped: another one is running")
        return DrillResult(ok=True, skipped="busy", path=str(newest))
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("restore drill failed", extra={"error": type(exc).__name__})
    result.seconds = time.monotonic() - started
    return result
