# VENDORED_FROM dailyJournal@6b1f67f miya/tools/restore.py
"""Restore an encrypted backup into a database (06 §4.4).

    make restore FILE=/data/backups/clientscrapper-….dump.age [DRY=1] [FORCE=1]
    python -m clientscrapper.tools.restore FILE --identity secrets/backup-key.txt \\
        [--to DSN] [--dry-run] [--force]

The file is what the nightly job wrote (``pg_dump -Fc | age``) or any
``.partNN`` piece of one that went to Telegram in pieces; the pieces are joined
in order. It is decrypted with the age *identity* (the private key — losing it
loses every backup) twice and never onto a disk (06 §4.4: plaintext only
inside the pipe): once to check that all of it decrypts to a pg_dump archive,
before anything in the target is touched, then straight into the stdin of
``pg_restore --clean --if-exists``, so an existing schema is replaced rather
than duplicated. A restore killed half-way leaves no plaintext behind.

Safety: a target that already holds companies, contacts, candidates, pushes or
suppression rows is refused unless ``--force`` — a restore over live data is a
decision, not a slip. A freshly migrated database (its seeded ``sources`` and
``scoring_weights`` only) is empty. ``--dry-run`` only decrypts and prints the
archive's table of contents; it never connects to the target.

Exit codes: 0 restored, 1 something failed (decrypt, connect, pg_restore),
2 bad arguments or a refused target. Messages are English: this is the one
place a helper on the phone with the owner reads them. Neither the identity's
content nor any password is ever printed.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, Final, TextIO
from urllib.parse import urlsplit, urlunsplit

from clientscrapper.services import backup

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2

_CHUNK: Final = 1024 * 1024
_ERROR_LINE: Final = "pg_restore: error:"
# pg_restore counts these as «errors ignored» when the database user does not
# own the extension (a non-superuser target). The schema and every row still
# land; anything else that fails is a real loss and is reported.
_BENIGN_COMMAND: Final = re.compile(
    r"^Command was:\s*(?:DROP|CREATE|COMMENT ON)\s+EXTENSION\b", re.IGNORECASE
)
# Rows here mean the target is somebody's working database.
DATA_TABLES: Final = (
    "companies",
    "contacts",
    "lead_candidates",
    "push_log",
    "suppression",
)


class RestoreError(Exception):
    """A failure with a message for the operator and an exit code."""

    def __init__(self, message: str, code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.code = code


def _libpq(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


# --- pieces -------------------------------------------------------------------


def resolve_parts(path: Path) -> list[Path]:
    """The files to feed age, in order: ``[path]`` or every ``.partNN`` piece.

    Any piece of a split backup may be named; all its siblings are found next
    to it and must run ``part01 … partNN`` without a gap, or the archive would
    be truncated somewhere in the middle and pg_restore would fail late, after
    dropping tables."""
    if backup.part_number(path) is None:
        return [path]
    stem = path.name[: path.name.rfind(".part")]
    pieces = sorted(
        (p for p in path.parent.glob(f"{stem}.part*") if backup.part_number(p)),
        key=lambda p: backup.part_number(p) or 0,
    )
    numbers = [backup.part_number(p) for p in pieces]
    if numbers != list(range(1, len(pieces) + 1)):
        raise RestoreError(
            f"pieces of {stem} are incomplete: found {numbers}, expected "
            f"part01..part{len(pieces):02d} with no gap",
            EXIT_USAGE,
        )
    return pieces


# --- helpers ------------------------------------------------------------------


def masked(url: str) -> str:
    """The target for messages: password hidden, driver dropped."""
    parsed = urlsplit(_libpq(url))
    if not parsed.password:
        return urlunsplit(parsed)
    netloc = f"{parsed.username or ''}:***@{parsed.hostname or ''}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def serious_errors(stderr: str) -> list[str]:
    """pg_restore error blocks other than extension-ownership noise."""
    lines = stderr.splitlines()
    serious: list[str] = []
    for index, line in enumerate(lines):
        if not line.startswith(_ERROR_LINE):
            continue
        command = ""
        for follower in lines[index + 1 :]:
            if follower.startswith("pg_restore:"):
                break
            if follower.startswith("Command was:"):
                command = follower.strip()
                break
        if command and _BENIGN_COMMAND.match(command):
            continue
        serious.append(f"{line}\n{command}".strip())
    return serious


def target_has_data(url: str) -> bool:
    """True when the target already holds rows the agent wrote."""
    import psycopg
    from psycopg import sql

    with psycopg.connect(_libpq(url), connect_timeout=10) as conn:
        for table in DATA_TABLES:
            try:
                with conn.transaction():
                    row = conn.execute(
                        sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(table))
                    ).fetchone()
            except psycopg.errors.UndefinedTable:
                continue
            if row is not None:
                return True
    return False


def schema_revision(url: str) -> str | None:
    """The alembic revision the restored database is at, or None."""
    import psycopg

    with psycopg.connect(_libpq(url), connect_timeout=10) as conn:
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        except psycopg.errors.UndefinedTable:
            return None
    return str(row[0]) if row else None


class Decryption:
    """``age -d`` with the pieces joined into its stdin by a thread and the
    plaintext on ``stdout`` — a pipe this process reads or hands to
    pg_restore, never a file (06 §4.4: plaintext only inside the pipe; a
    restore killed half-way leaves no phone book on disk). Streamed a
    megabyte at a time, so a large backup never sits in memory."""

    def __init__(self, parts: list[Path], identity: Path) -> None:
        try:
            self.process = subprocess.Popen(
                ["age", "-d", "-i", str(identity)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise RestoreError(f"could not start age: {exc}") from exc
        assert self.process.stdout is not None
        self.stdout: IO[bytes] = self.process.stdout
        self._stderr = b""
        self._feeder = threading.Thread(target=self._feed, args=(parts,), daemon=True)
        self._drain = threading.Thread(target=self._read_stderr, daemon=True)
        self._feeder.start()
        self._drain.start()

    def _feed(self, parts: list[Path]) -> None:
        sink = self.process.stdin
        assert sink is not None
        try:
            for part in parts:
                with part.open("rb") as source:
                    while chunk := source.read(_CHUNK):
                        sink.write(chunk)
        except (BrokenPipeError, OSError):
            pass  # age (or its reader) gave up early; its exit code says why
        finally:
            with contextlib.suppress(OSError):
                sink.close()

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        self._stderr = self.process.stderr.read()

    def finish(self) -> int:
        """Close this process's end of the plaintext, wait for age and its
        threads; age's exit code."""
        with contextlib.suppress(OSError):
            self.stdout.close()
        code = self.process.wait()
        self._feeder.join()
        self._drain.join()
        return code

    def error(self) -> RestoreError:
        detail = self._stderr.decode(errors="replace").strip()[-400:]
        return RestoreError(f"age could not decrypt the backup: {detail or 'no detail'}")


def verify(parts: list[Path], identity: Path) -> int:
    """Decrypt the whole backup and throw the plaintext away: age
    authenticates every chunk, so a wrong key, a truncated piece or a
    corrupted byte is found here — before pg_restore ``--clean`` has dropped
    anything in the target. The plaintext must be a pg_dump archive.
    Returns its size."""
    run = Decryption(parts, identity)
    total, head = 0, b""
    try:
        while chunk := run.stdout.read(_CHUNK):
            if len(head) < len(backup.DUMP_MAGIC):
                head = (head + chunk)[: len(backup.DUMP_MAGIC)]
            total += len(chunk)
    finally:
        code = run.finish()
    if code != 0:
        raise run.error()
    if total == 0 or head != backup.DUMP_MAGIC:
        raise RestoreError("the backup decrypts, but not to a pg_dump archive")
    return total


def pg_restore(
    parts: list[Path],
    identity: Path,
    url: str,
    *,
    list_only: bool = False,
    err: TextIO | None = None,
) -> str:
    """Decrypt into pg_restore's stdin; returns its stdout (the TOC for
    ``list_only``). pg_restore's own messages go to ``err`` (default: the
    terminal)."""
    err = err if err is not None else sys.stderr
    dsn, env = backup.libpq_dsn_and_env(url)
    if list_only:
        command = ["pg_restore", "--list"]
    else:
        command = [
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            "--clean",
            "--if-exists",
            "-d",
            dsn,
        ]
    run = Decryption(parts, identity)
    try:
        result = subprocess.run(
            command, stdin=run.stdout, capture_output=True, env=env, check=False
        )
    except OSError as exc:
        raise RestoreError(f"could not start pg_restore: {exc}") from exc
    finally:
        age_code = run.finish()
    if result.returncode != 0 and age_code != 0:
        # pg_restore read a stream that broke off: age says why.
        raise run.error()
    stderr = result.stderr.decode(errors="replace")
    if stderr.strip():
        err.write(stderr.rstrip() + "\n")
    if result.returncode == 0:
        return result.stdout.decode(errors="replace")
    if list_only:
        raise RestoreError("pg_restore could not read the archive")
    serious = serious_errors(stderr)
    if result.returncode == 1 and "errors ignored on restore" in stderr and not serious:
        err.write(
            "pg_restore reported ignored errors, all about extension ownership; "
            "the schema and the rows are restored.\n"
        )
        return result.stdout.decode(errors="replace")
    raise RestoreError(
        "pg_restore failed"
        + (f" ({len(serious)} serious error(s), see above)" if serious else "")
    )


# --- the command --------------------------------------------------------------


def restore(
    file: Path,
    identity: Path,
    *,
    to: str,
    dry_run: bool = False,
    force: bool = False,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """The command's work; its messages go to ``out``/``err`` (default: the
    terminal of the person who ran it)."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if not file.exists():
        raise RestoreError(f"no such file: {file}", EXIT_USAGE)
    if not identity.is_file():
        raise RestoreError(
            "identity file not found (the age private key, usually "
            "secrets/backup-key.txt)",
            EXIT_USAGE,
        )
    parts = resolve_parts(file)
    if not dry_run:
        try:
            occupied = target_has_data(to)
        except Exception as exc:
            raise RestoreError(
                f"could not connect to {masked(to)}: {type(exc).__name__}"
            ) from exc
        if occupied and not force:
            raise RestoreError(
                f"{masked(to)} already holds data; pass --force (FORCE=1) to replace "
                "everything in it with this backup",
                EXIT_USAGE,
            )

    # The plaintext never touches a disk: it is decrypted twice, once to
    # check it all (nothing in the target is touched yet), then straight into
    # pg_restore's stdin.
    err.write(f"decrypting {len(parts)} file(s) …\n")
    verify(parts, identity)
    if dry_run:
        out.write(pg_restore(parts, identity, to, list_only=True, err=err))
        err.write("dry run: nothing was written\n")
        return EXIT_OK
    err.write(f"restoring into {masked(to)} …\n")
    pg_restore(parts, identity, to, err=err)

    try:
        revision = schema_revision(to)
    except Exception as exc:  # the restore ran; only the check failed
        err.write(f"restored, but the schema could not be read: {exc}\n")
        return EXIT_FAILED
    if revision is None:
        err.write("restored, but the database has no alembic_version table\n")
        return EXIT_FAILED
    out.write(f"restored {file.name} into {masked(to)} (schema {revision})\n")
    out.write("run `make migrate` next if the code is newer than this backup\n")
    return EXIT_OK


def restore_into(
    database_url: str, file: Path, identity: Path, *, quiet: bool = False
) -> int:
    """Load ``file`` into ``database_url`` with no questions asked — for a
    scratch database such as the weekly drill's. Returns an exit code.

    ``quiet``: nothing is written anywhere. The drill runs inside the worker,
    whose stderr is the container log: pg_restore's raw lines (a DETAIL or a
    CONTEXT with a row's values) would reach it around the log redactors
    (06 §4.5). The drill's verdict is its heartbeat; the detail is for a
    person running ``make restore`` by hand."""
    sink: TextIO | None = io.StringIO() if quiet else None
    try:
        return restore(file, identity, to=database_url, force=True, out=sink, err=sink)
    except RestoreError as exc:
        if not quiet:
            sys.stderr.write(f"{exc}\n")
        return exc.code


def main(argv: list[str] | None = None) -> int:
    from clientscrapper.config import settings

    parser = argparse.ArgumentParser(
        prog="python -m clientscrapper.tools.restore",
        description="Restore an encrypted clientScrapper backup into a database.",
    )
    parser.add_argument(
        "file", type=Path, help="a .dump.age backup, or any .partNN piece of one"
    )
    parser.add_argument(
        "--identity",
        required=True,
        type=Path,
        help="the age private key file (secrets/backup-key.txt)",
    )
    parser.add_argument(
        "--to",
        default=settings.database_url,
        help="target database URL (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="decrypt and list the archive's contents; touch no database",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="restore even if the target already holds data",
    )
    args = parser.parse_args(argv)
    try:
        return restore(
            args.file,
            args.identity,
            to=args.to,
            dry_run=args.dry_run,
            force=args.force,
        )
    except RestoreError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return exc.code
    except Exception as exc:
        sys.stderr.write(f"error: restore failed: {type(exc).__name__}\n")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
