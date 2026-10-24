# VENDORED_FROM dailyJournal@6b1f67f miya/services/backup.py
"""Nightly encrypted database backup (06 §4.4).

``pg_dump -Fc`` piped into ``age -r $BACKUP_AGE_RECIPIENT``, written as
``clientscrapper-<yyyymmdd-hhmmss>.dump.age`` under ``BACKUP_DIR`` and kept for
``BACKUP_RETENTION_DAYS``. The database is a phone book of prospects, so the
dump is never written to disk in the clear: the plaintext exists only inside
the pipe. With no recipient configured nothing is written at all — an
unencrypted dump is not a fallback (06 §4.4); ``health`` says
``backup_unconfigured`` instead.

Kept from MIYA: the password through ``PGPASSWORD`` and never argv
(``libpq_dsn_and_env``), the ``.partial`` file renamed only after both
processes succeeded, the 0600 mode, pruning by the stamp in the name, and the
Telegram split into ``.partNN`` pieces of ≤ 45 MB (``split_for_telegram``)
that ``tools/restore.py`` joins back. MIYA's legacy ``.sql.age`` format is
gone: this project never wrote one.

One change: the dump flows through this process on its way to ``age`` instead
of an OS pipe between the two, so the bytes can be counted. A run whose
pg_dump wrote nothing, or wrote something that is not a custom-format archive
(``PGDMP``), is a failure — ``age`` would happily encrypt an empty input into
a 200-byte file that looks like a backup (07 §4.1 T-08: «make backup →
non-zero .age»). The bytes stay in memory one chunk at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final
from urllib.parse import unquote, urlsplit, urlunsplit

from clientscrapper.config import settings

log = logging.getLogger(__name__)

BACKUP_PREFIX: Final = "clientscrapper-"
BACKUP_SUFFIX: Final = ".dump.age"
_STAMP_RE: Final = re.compile(r"(\d{8}-\d{6})")
_STAMP_FORMAT: Final = "%Y%m%d-%H%M%S"
PG_DUMP_TIMEOUT_SECONDS: Final = 1800
# pg_dump's custom-format archive starts with these bytes.
DUMP_MAGIC: Final = b"PGDMP"
# Where the age identity (the private key) lives: inside the app containers
# (./secrets mounted read-only) and on the host checkout.
IDENTITY_PATHS: Final = ("/app/secrets/backup-key.txt", "secrets/backup-key.txt")

# Telegram accepts documents up to 50 MB from a bot; 45 MB leaves margin for
# the multipart envelope. A backup larger than MAX_PARTS pieces is not sent.
TELEGRAM_PART_BYTES: Final = 45 * 1024 * 1024
MAX_PARTS: Final = 10
_PART_RE: Final = re.compile(r"\.part(\d{2,})$")
_CHUNK_BYTES: Final = 1024 * 1024


class BackupTooLarge(Exception):
    """The backup would need more than MAX_PARTS Telegram documents."""


@dataclass(slots=True)
class BackupResult:
    path: Path | None = None
    size: int = 0
    # Plaintext bytes pg_dump produced (counted in the pipe).
    dump_bytes: int = 0
    pruned: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.path is not None and self.size > 0


def libpq_dsn_and_env(url: str) -> tuple[str, dict[str, str]]:
    """SQLAlchemy URL → libpq URL plus environment for pg_dump / pg_restore.

    The password travels via PGPASSWORD, never argv: ``/proc/*/cmdline`` is
    world-readable, so a DSN with the password inline would show the database
    credentials to every process on the host for the duration of the dump.
    """
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    parsed = urlsplit(url)
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
        host = parsed.hostname or ""
        netloc = (f"{parsed.username}@" if parsed.username else "") + host
        if parsed.port:
            netloc += f":{parsed.port}"
        url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
    return url, env


def backup_dir(directory: Path | None = None) -> Path:
    path = directory if directory is not None else Path(settings.backup_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_identity() -> Path | None:
    """The age identity file, where the containers and the host keep it."""
    for candidate in IDENTITY_PATHS:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def backup_name(now: datetime) -> str:
    local = now.astimezone(settings.tz)
    return f"{BACKUP_PREFIX}{local.strftime(_STAMP_FORMAT)}{BACKUP_SUFFIX}"


async def _pump(
    source: asyncio.StreamReader, sink: asyncio.StreamWriter
) -> tuple[int, bytes]:
    """Copy pg_dump's stdout into age's stdin; returns (bytes, first bytes)."""
    total = 0
    head = b""
    try:
        while chunk := await source.read(_CHUNK_BYTES):
            if len(head) < len(DUMP_MAGIC):
                head = (head + chunk)[: len(DUMP_MAGIC)]
            total += len(chunk)
            sink.write(chunk)
            await sink.drain()
    except (BrokenPipeError, ConnectionResetError):
        # age stopped reading; its exit code and stderr say why.
        pass
    finally:
        sink.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await sink.wait_closed()
    return total, head


async def create_backup(
    *,
    now: datetime | None = None,
    recipient: str | None = None,
    directory: Path | None = None,
    database_url: str | None = None,
) -> BackupResult:
    """Dump, encrypt, check, prune. Never raises — the caller is a scheduler
    job. Each argument defaults to its setting."""
    recipient = recipient if recipient is not None else settings.backup_age_recipient
    recipient = recipient.strip()
    if not recipient:
        log.info("BACKUP_AGE_RECIPIENT is not set: no backup is written")
        return BackupResult(error="no_recipient")

    now = now or datetime.now(settings.tz)
    try:
        folder = backup_dir(directory)
    except OSError as exc:
        log.error("backup directory is not usable", extra={"error": type(exc).__name__})
        return BackupResult(error=f"backup directory unusable: {type(exc).__name__}")

    target = folder / backup_name(now)
    partial = target.with_name(target.name + ".partial")
    dsn, dump_env = libpq_dsn_and_env(database_url or settings.database_url)
    try:
        dump = await asyncio.create_subprocess_exec(
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            dsn,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dump_env,
        )
    except (OSError, ValueError) as exc:
        log.error("pg_dump could not start", extra={"error": type(exc).__name__})
        return BackupResult(error=f"pg_dump: {type(exc).__name__}")
    try:
        encrypt = await asyncio.create_subprocess_exec(
            "age",
            "-r",
            recipient,
            "-o",
            str(partial),
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        dump.kill()
        await dump.wait()
        log.error("age could not start", extra={"error": type(exc).__name__})
        return BackupResult(error=f"age: {type(exc).__name__}")
    assert dump.stdout is not None and dump.stderr is not None
    assert encrypt.stdin is not None and encrypt.stderr is not None

    async def finish() -> tuple[int, bytes, bytes, bytes]:
        assert dump.stdout is not None and dump.stderr is not None
        assert encrypt.stdin is not None and encrypt.stderr is not None
        (total, head), dump_err, age_err = await asyncio.gather(
            _pump(dump.stdout, encrypt.stdin),
            dump.stderr.read(),
            encrypt.stderr.read(),
        )
        await asyncio.gather(dump.wait(), encrypt.wait())
        return total, head, dump_err, age_err

    try:
        total, head, dump_err, age_err = await asyncio.wait_for(
            finish(), timeout=PG_DUMP_TIMEOUT_SECONDS
        )
    except TimeoutError:
        for process in (encrypt, dump):
            if process.returncode is None:
                process.kill()
        await asyncio.gather(encrypt.wait(), dump.wait(), return_exceptions=True)
        partial.unlink(missing_ok=True)
        log.error("backup timed out", extra={"seconds": PG_DUMP_TIMEOUT_SECONDS})
        return BackupResult(error="timeout")

    if dump.returncode != 0 or encrypt.returncode != 0:
        partial.unlink(missing_ok=True)
        detail = (dump_err or age_err).decode(errors="replace").strip()[-400:]
        log.error(
            "backup failed",
            extra={"pg_dump": dump.returncode, "age": encrypt.returncode},
        )
        return BackupResult(error=detail or "backup command failed", dump_bytes=total)
    if total == 0 or head != DUMP_MAGIC:
        # A dump that is not an archive restores nothing, however it encrypts.
        partial.unlink(missing_ok=True)
        log.error("pg_dump produced no archive", extra={"bytes": total})
        return BackupResult(error="empty_dump", dump_bytes=total)

    # Renamed only after both processes succeeded, so a crashed run never
    # leaves behind a file that looks like a usable backup.
    partial.replace(target)
    os.chmod(target, 0o600)
    size = target.stat().st_size
    result = BackupResult(path=target, size=size, dump_bytes=total)
    result.pruned = prune_old(now=now, directory=folder)
    log.info(
        "backup written",
        extra={
            "file": target.name,
            "bytes": size,
            "dump_bytes": total,
            "pruned": result.pruned,
        },
    )
    return result


def backup_stamp(path: Path) -> datetime | None:
    """When a backup was taken, read from its name; None for a foreign file."""
    match = _STAMP_RE.search(path.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), _STAMP_FORMAT).replace(
            tzinfo=settings.tz
        )
    except ValueError:
        return None


def current_backups(directory: Path | None = None) -> list[Path]:
    """Every ``*.dump.age`` of ours, oldest first by stamp. A ``.partial``
    still being written and a ``.partNN`` piece fall outside the glob; a file
    without a stamp in its name is not one of ours."""
    folder = directory if directory is not None else Path(settings.backup_dir)
    try:
        # Read-only: a reader (health, /holat) never creates the directory.
        candidates = list(folder.glob(f"*{BACKUP_SUFFIX}"))
    except OSError:
        log.warning("backup directory is not readable")
        return []
    stamped = [(backup_stamp(p), p) for p in candidates]
    return [p for _, p in sorted((s, p) for s, p in stamped if s is not None)]


async def newest_backup(directory: Path | None = None) -> Path | None:
    """The most recent backup on disk, or None. Never raises."""
    current = await asyncio.to_thread(current_backups, directory)
    return current[-1] if current else None


def prune_old(
    *,
    now: datetime | None = None,
    directory: Path | None = None,
    retention_days: int | None = None,
) -> int:
    """Delete backups older than the retention window; returns how many."""
    now = now or datetime.now(settings.tz)
    days = (
        retention_days if retention_days is not None else settings.backup_retention_days
    )
    cutoff = now - timedelta(days=days)
    pruned = 0
    for path in current_backups(directory):
        stamp = backup_stamp(path)
        if stamp is not None and stamp < cutoff:
            try:
                path.unlink()
                pruned += 1
            except OSError:
                log.warning("could not prune an old backup", extra={"file": path.name})
    return pruned


# --- Telegram copy ----------------------------------------------------------


def split_for_telegram(path: Path) -> list[Path]:
    """Cut one backup into pieces Telegram accepts; ``[path]`` when it fits.

    Pieces are ``<name>.part01`` … ``<name>.partNN`` next to the file, each
    TELEGRAM_PART_BYTES except the last, owner-only like the backup itself,
    written with fixed-size reads. Raises BackupTooLarge (before writing
    anything) past MAX_PARTS pieces. The caller removes the pieces with
    ``remove_parts`` once they are sent — or failed to send.
    """
    size = path.stat().st_size
    if size <= TELEGRAM_PART_BYTES:
        return [path]
    count = -(-size // TELEGRAM_PART_BYTES)
    if count > MAX_PARTS:
        raise BackupTooLarge(f"{path.name}: {count} pieces, the cap is {MAX_PARTS}")
    parts: list[Path] = []
    try:
        with path.open("rb") as source:
            for index in range(1, count + 1):
                part = path.with_name(f"{path.name}.part{index:02d}")
                fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                parts.append(part)
                remaining = TELEGRAM_PART_BYTES
                with os.fdopen(fd, "wb") as sink:
                    while remaining > 0:
                        chunk = source.read(min(_CHUNK_BYTES, remaining))
                        if not chunk:
                            break
                        sink.write(chunk)
                        remaining -= len(chunk)
    except OSError:
        remove_parts(parts)
        raise
    return parts


def remove_parts(parts: list[Path]) -> None:
    """Delete the ``.partNN`` pieces; the backup itself is never touched."""
    for part in parts:
        if _PART_RE.search(part.name) is None:
            continue
        try:
            part.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove a backup piece", extra={"file": part.name})


def part_number(path: Path) -> int | None:
    """``…part03`` → 3; None for a whole backup."""
    match = _PART_RE.search(path.name)
    return int(match.group(1)) if match else None
