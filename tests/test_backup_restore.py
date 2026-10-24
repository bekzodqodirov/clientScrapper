"""Backups, restore and the drill (06 §4.4, 07 §4.1 T-08) on scratch databases.

The round trip is real: ``pg_dump -Fc | age`` of the test database with
synthetic rows, ``tools/restore.py`` into a scratch database, the counts
compared, and the weekly drill restoring into its own scratch database that
it drops whatever happens. The scratch databases are named after the test
database (``<db>_restore``, ``<db>_drill``), created and dropped here.

Needs ``age``, ``age-keygen``, ``pg_dump`` and ``pg_restore`` on PATH (CI's
test job installs ``age`` and ``postgresql-client-16``, 07 §2.1); without them
these tests skip — or fail under ``CS_REQUIRE_DB=1``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.services import backup, restore_drill
from clientscrapper.tools import restore
from tests.conftest import require_db

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 24, 3, 45, tzinfo=TZ)
TOOLS = ("age", "age-keygen", "pg_dump", "pg_restore")


def need_tools() -> None:
    missing = [t for t in TOOLS if shutil.which(t) is None]
    if missing:
        if require_db():
            pytest.fail(f"CS_REQUIRE_DB=1 but {missing} are not on PATH")
        pytest.skip(f"{missing} not on PATH")


@pytest.fixture
def keys(tmp_path: Path) -> tuple[str, Path]:
    """A throwaway age key pair: (recipient, identity file)."""
    need_tools()
    identity = tmp_path / "backup-key.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(identity)], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert recipient.startswith("age1")
    return recipient, identity


def _db_name() -> str:
    return sa.engine.make_url(settings.database_url).database or "clientscrapper"


def _url(database: str) -> str:
    url = sa.engine.make_url(settings.database_url).set(database=database)
    return url.render_as_string(hide_password=False)


def _exists(database: str) -> bool:
    import psycopg

    dsn = _url(_db_name()).replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
    return row is not None


@pytest.fixture
def scratch() -> str:
    """An empty scratch database ``<db>_restore``, dropped afterwards."""
    name = f"{_db_name()}_restore"
    restore_drill._admin(settings.database_url, restore_drill._DROP, name)
    restore_drill._admin(settings.database_url, "CREATE DATABASE {}", name)
    yield _url(name)
    restore_drill._admin(settings.database_url, restore_drill._DROP, name)


async def _seed(session) -> None:
    company = await session.scalar(
        sa.text(
            "INSERT INTO companies (display_name, name_key) "
            "VALUES ('Namuna Tekstil Savdo', 'namuna tekstil savdo') RETURNING id"
        )
    )
    await session.execute(
        sa.text("INSERT INTO lead_candidates (company_id) VALUES (:c)"),
        {"c": company},
    )
    for i in range(3):
        await session.execute(
            sa.text(
                "INSERT INTO suppression (kind, key_type, key, source, reason) "
                "VALUES ('dnc', 'phone_tail', :k, 'owner_bot', 'owner_command')"
            ),
            {"k": f"90000000{i}"},
        )
    await session.commit()


# --- no recipient, no file ---------------------------------------------------------


async def test_no_recipient_writes_nothing(tmp_path):
    result = await backup.create_backup(recipient="", directory=tmp_path, now=NOW)
    assert (result.ok, result.error, result.path) == (False, "no_recipient", None)
    assert list(tmp_path.iterdir()) == []


# --- the round trip ------------------------------------------------------------------


async def test_a_backup_is_a_non_zero_encrypted_archive_that_restores(
    session, keys, tmp_path, scratch
):
    await _seed(session)
    recipient, identity = keys
    folder = tmp_path / "backups"
    result = await backup.create_backup(recipient=recipient, directory=folder, now=NOW)
    assert result.ok and result.error is None
    assert result.path == folder / "clientscrapper-20261024-034500.dump.age"
    assert result.size == result.path.stat().st_size > 0
    assert result.dump_bytes > 0
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    sealed = result.path.read_bytes()
    assert sealed.startswith(b"age-encryption.org/v1")
    # The plaintext never reached the disk: no archive magic, no row text.
    assert b"PGDMP" not in sealed and b"Namuna Tekstil" not in sealed
    assert [p.name for p in folder.iterdir()] == [result.path.name]

    expected = restore_drill.count_rows(settings.database_url)
    assert expected == {
        "companies": 1,
        "contacts": 0,
        "lead_candidates": 1,
        "push_log": 0,
        "suppression": 3,
        "outcomes": 0,
    }
    assert restore.restore(result.path, identity, to=scratch) == restore.EXIT_OK
    assert restore_drill.count_rows(scratch) == expected
    # The restored schema is the source's, whatever the head is today.
    source_revision = restore.schema_revision(settings.database_url)
    assert source_revision is not None
    assert restore.schema_revision(scratch) == source_revision
    # A target that holds data is refused without --force …
    with pytest.raises(restore.RestoreError) as refused:
        restore.restore(result.path, identity, to=scratch)
    assert refused.value.code == restore.EXIT_USAGE
    assert "pass --force" in str(refused.value)
    # … and replaced with it, not duplicated.
    assert restore.restore(result.path, identity, to=scratch, force=True) == 0
    assert restore_drill.count_rows(scratch) == expected


async def test_a_dry_run_lists_the_archive_and_touches_no_database(
    session, keys, tmp_path, capsys
):
    recipient, identity = keys
    result = await backup.create_backup(recipient=recipient, directory=tmp_path, now=NOW)
    nowhere = "postgresql+psycopg://none:none@127.0.0.1:1/none"
    assert restore.restore(result.path, identity, to=nowhere, dry_run=True) == 0
    toc = capsys.readouterr().out
    assert "TABLE DATA public suppression" in toc


async def test_the_telegram_pieces_join_back(
    session, keys, tmp_path, scratch, monkeypatch
):
    await _seed(session)
    recipient, identity = keys
    result = await backup.create_backup(recipient=recipient, directory=tmp_path, now=NOW)
    assert result.path is not None
    monkeypatch.setattr(backup, "TELEGRAM_PART_BYTES", result.size // 3 + 1)
    parts = backup.split_for_telegram(result.path)
    assert [backup.part_number(p) for p in parts] == [1, 2, 3]
    assert b"".join(p.read_bytes() for p in parts) == result.path.read_bytes()
    assert restore.resolve_parts(parts[1]) == parts
    assert restore.restore(parts[2], identity, to=scratch) == 0
    assert restore_drill.count_rows(scratch)["suppression"] == 3
    parts[1].unlink()
    with pytest.raises(restore.RestoreError):
        restore.resolve_parts(parts[0])
    backup.remove_parts(parts)
    assert result.path.exists()


# --- the non-zero check -------------------------------------------------------------


def _fake_pg_dump(folder: Path, body: str) -> None:
    folder.mkdir()
    script = folder / "pg_dump"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("exit 0", "empty_dump"),  # wrote nothing at all
        ("printf 'not an archive'", "empty_dump"),  # wrote something else
        ("echo 'pg_dump: error: connection refused' >&2; exit 1", "connection refused"),
    ],
)
async def test_a_dump_that_is_not_an_archive_leaves_no_file(
    keys, tmp_path, monkeypatch, body, error
):
    recipient, _ = keys
    _fake_pg_dump(tmp_path / "bin", body)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    folder = tmp_path / "backups"
    result = await backup.create_backup(recipient=recipient, directory=folder, now=NOW)
    assert not result.ok and result.path is None
    assert error in (result.error or "")
    assert list(folder.iterdir()) == []  # no .partial left behind either


def test_old_backups_are_pruned_by_the_stamp_in_their_name(tmp_path):
    names = [
        "clientscrapper-20261009-034500.dump.age",  # 15 days: past retention
        "clientscrapper-20261011-034500.dump.age",
        "clientscrapper-20261024-034500.dump.age",
        "clientscrapper-20261001-034500.dump.age.part01",  # a piece, not a backup
        "notes.txt",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    pruned = backup.prune_old(now=NOW, directory=tmp_path, retention_days=14)
    assert pruned == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names[1:])
    assert [p.name for p in backup.current_backups(tmp_path)] == names[1:3]


# --- the drill -------------------------------------------------------------------------


async def test_the_drill_restores_compares_and_drops_its_database(
    session, keys, tmp_path
):
    await _seed(session)
    recipient, identity = keys
    folder = tmp_path / "backups"
    await backup.create_backup(recipient=recipient, directory=folder, now=NOW)
    expected = restore_drill.count_rows(settings.database_url)
    drill_db = f"{_db_name()}_drill"
    result = await restore_drill.run(
        identity=identity, expected=expected, database=drill_db, directory=folder
    )
    assert result.ok and result.error is None, result.error
    assert result.counts == expected
    assert not _exists(drill_db)

    wrong = dict(expected, suppression=500)
    result = await restore_drill.run(
        identity=identity, expected=wrong, database=drill_db, directory=folder
    )
    assert not result.ok
    assert result.error == "counts differ: suppression 500≠3"
    assert not _exists(drill_db)


async def test_a_drill_that_cannot_restore_still_drops_its_database(
    session, keys, tmp_path
):
    recipient, identity = keys
    folder = tmp_path / "backups"
    result = await backup.create_backup(recipient=recipient, directory=folder, now=NOW)
    assert result.path is not None
    result.path.write_bytes(b"age-encryption.org/v1\nnot really\n")  # corrupted
    drill_db = f"{_db_name()}_drill"
    drill = await restore_drill.run(
        identity=identity, database=drill_db, directory=folder
    )
    assert not drill.ok and drill.error is not None
    assert drill.error.startswith("RuntimeError: restore exited with 1")
    assert not _exists(drill_db)


async def test_the_drill_skips_without_a_key_or_a_backup(tmp_path):
    missing = tmp_path / "no-key.txt"
    assert (await restore_drill.run(identity=missing)).skipped == "no_key"
    key = tmp_path / "key.txt"
    key.write_text("AGE-SECRET-KEY-placeholder\n", encoding="utf-8")
    empty = tmp_path / "empty"
    skipped = await restore_drill.run(identity=key, directory=empty)
    assert (skipped.ok, skipped.skipped) == (True, "no_backup")


def test_the_drill_database_name_is_an_identifier():
    with pytest.raises(ValueError):
        restore_drill.drill_sync(Path("x"), Path("y"), "postgresql://a/b", 'x"; DROP')


def test_masked_urls_never_show_a_password():
    url = "postgresql+psycopg://clientscrapper:s3cr3t@db:5432/clientscrapper"
    assert restore.masked(url) == "postgresql://clientscrapper:***@db:5432/clientscrapper"
    dsn, env = backup.libpq_dsn_and_env(url)
    assert "s3cr3t" not in dsn and env["PGPASSWORD"] == "s3cr3t"


def test_extension_ownership_noise_is_not_a_serious_restore_error():
    stderr = (
        "pg_restore: error: could not execute query: ERROR: must be owner of "
        "extension pg_trgm\n"
        "Command was: COMMENT ON EXTENSION pg_trgm IS 'text similarity';\n"
        "pg_restore: error: could not execute query: ERROR: relation exists\n"
        "Command was: CREATE TABLE public.companies (id uuid);\n"
    )
    [serious] = restore.serious_errors(stderr)
    assert "CREATE TABLE public.companies" in serious


def test_the_backup_file_is_named_in_tashkent_time():
    utc = datetime(2026, 10, 23, 22, 45, tzinfo=ZoneInfo("UTC"))
    assert backup.backup_name(utc) == "clientscrapper-20261024-034500.dump.age"
    assert backup.backup_stamp(Path(backup.backup_name(utc))) == utc
    assert backup.backup_stamp(Path("other.dump.age")) is None
    assert utc + timedelta(0) == NOW


# --- review fixes ----------------------------------------------------------------------


async def test_the_counts_are_the_dumps_own_snapshot(
    session, keys, tmp_path, scratch, monkeypatch
):
    """Rows that land after the snapshot was exported — a resolve run, a feed
    delta during the dump — are in neither the counts nor the file, so the
    drill compares like with like."""
    await _seed(session)
    recipient, identity = keys

    async def a_delta_lands() -> None:
        import psycopg

        dsn = _url(_db_name()).replace("postgresql+psycopg://", "postgresql://")
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as other:
            for i in range(7):
                await other.execute(
                    "INSERT INTO suppression (kind, key_type, key, source, reason) "
                    "VALUES ('dnc', 'phone_tail', %s, 'owner_bot', 'owner_command')",
                    (f"90000010{i}",),
                )

    monkeypatch.setattr(backup, "_after_snapshot", a_delta_lands)
    result = await backup.create_backup(
        recipient=recipient,
        directory=tmp_path,
        now=NOW,
        count_tables=restore_drill.COUNTED_TABLES,
    )
    assert result.ok, result.error
    assert result.counts == {
        "companies": 1,
        "contacts": 0,
        "lead_candidates": 1,
        "push_log": 0,
        "suppression": 3,
        "outcomes": 0,
    }
    assert restore_drill.count_rows(settings.database_url)["suppression"] == 10
    assert restore.restore(result.path, identity, to=scratch) == restore.EXIT_OK
    assert restore_drill.count_rows(scratch) == result.counts


async def test_the_ciphertext_is_owner_only_while_it_is_written(
    session, keys, tmp_path, monkeypatch
):
    """age gets a .partial this process created 0600: `age -o` would create
    it with the umask's mode and the bytes would sit readable until the
    rename."""
    recipient, _ = keys
    mode_file = tmp_path / "mode"
    folder = tmp_path / "bin"
    folder.mkdir()
    fake_age = folder / "age"
    fake_age.write_text(
        "#!/bin/sh\n"
        "umask 022\n"
        # Where the ciphertext goes: -o's file, else the file on stdout.
        'out="$(readlink /proc/$$/fd/1)"\n'
        'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
        'cat > "$out"\n'
        f'stat -c %a "$out" > "{mode_file}"\n',
        encoding="utf-8",
    )
    fake_age.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    result = await backup.create_backup(
        recipient=recipient, directory=tmp_path / "backups", now=NOW
    )
    assert result.ok, result.error
    assert mode_file.read_text().strip() == "600"
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600


async def test_the_drill_writes_nothing_past_the_log_redactors(
    session, keys, tmp_path, capfd
):
    """pg_restore's raw lines (a DETAIL with a row's values) must not reach
    the worker's stderr, which is the container log."""
    recipient, identity = keys
    folder = tmp_path / "backups"
    result = await backup.create_backup(recipient=recipient, directory=folder, now=NOW)
    assert result.path is not None
    result.path.write_bytes(b"age-encryption.org/v1\nnot really\n")  # corrupted
    capfd.readouterr()
    drill = await restore_drill.run(
        identity=identity, database=f"{_db_name()}_drill", directory=folder
    )
    assert not drill.ok
    out, err = capfd.readouterr()
    # Only the JSON log lines (redacted) may appear, if a handler is installed.
    assert out == ""
    assert [line for line in err.splitlines() if not line.startswith("{")] == []


# --- two writers at once, and no plaintext on disk (seam review 2026-10-24) -------------


async def test_two_backups_in_the_same_second_each_write_a_whole_file(
    session, keys, tmp_path, scratch
):
    """Two schedulers (or a catch-up beside the cron) at the same second used
    one ``.partial`` opened O_TRUNC: one run renamed a file the other was
    still writing, the other raised at its rename."""
    import asyncio

    await _seed(session)
    recipient, identity = keys
    folder = tmp_path / "backups"
    first, second = await asyncio.gather(
        backup.create_backup(recipient=recipient, directory=folder, now=NOW),
        backup.create_backup(recipient=recipient, directory=folder, now=NOW),
    )
    assert first.ok and second.ok, (first.error, second.error)
    assert first.path == second.path
    assert [p.name for p in folder.iterdir()] == [first.path.name]
    assert restore.restore(first.path, identity, to=scratch) == restore.EXIT_OK
    assert restore_drill.count_rows(scratch)["suppression"] == 3


def test_a_partial_a_killed_run_left_is_swept_and_a_live_one_is_not(tmp_path):
    old = tmp_path / "clientscrapper-20261023-034500.dump.age.41-0a1b2c3d.partial"
    live = tmp_path / "clientscrapper-20261024-034500.dump.age.42-0a1b2c3e.partial"
    other = tmp_path / "notes.partial"
    for path in (old, live, other):
        path.write_bytes(b"x")
    now = live.stat().st_mtime
    os.utime(old, (now - backup.PARTIAL_STALE_SECONDS - 1,) * 2)
    os.utime(other, (now - backup.PARTIAL_STALE_SECONDS - 1,) * 2)
    assert backup.sweep_partials(tmp_path, now=now) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([live.name, other.name])


def _spy_pg_restore(folder: Path, seen: Path) -> None:
    """A pg_restore that records what lies in TMPDIR and its arguments while
    it runs, then is the real one."""
    real = shutil.which("pg_restore")
    assert real is not None
    folder.mkdir()
    spy = folder / "pg_restore"
    spy.write_text(
        "#!/bin/sh\n"
        f'ls -A "$TMPDIR" >> "{seen}"\n'
        f'echo "args: $*" >> "{seen}"\n'
        f'exec "{real}" "$@"\n',
        encoding="utf-8",
    )
    spy.chmod(0o755)


async def test_a_restore_puts_no_plaintext_on_disk(
    session, keys, tmp_path, scratch, monkeypatch
):
    """06 §4.4: plaintext only inside the pipe. The drill runs in the
    long-lived worker, whose /tmp survives restarts: a decrypted dump a
    SIGKILL left there would be the whole phone book in clear."""
    import tempfile

    await _seed(session)
    recipient, identity = keys
    result = await backup.create_backup(recipient=recipient, directory=tmp_path, now=NOW)
    assert result.path is not None
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    seen = tmp_path / "seen.txt"
    _spy_pg_restore(tmp_path / "bin", seen)
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    assert restore.restore(result.path, identity, to=scratch) == restore.EXIT_OK
    nowhere = "postgresql+psycopg://none:none@127.0.0.1:1/none"
    assert restore.restore(result.path, identity, to=nowhere, dry_run=True) == 0
    lines = seen.read_text().splitlines()
    # Two runs (the restore, the dry run's --list): nothing in TMPDIR while
    # pg_restore ran, and no file named to it — the archive is its stdin.
    assert [line for line in lines if not line.startswith("args: ")] == []
    args = [line for line in lines if line.startswith("args: ")]
    assert len(args) == 2
    assert not any(".dump" in line for line in args)
    assert list(tmp.iterdir()) == []
    assert restore_drill.count_rows(scratch)["suppression"] == 3


async def test_a_truncated_backup_is_refused_before_the_target_is_touched(
    session, keys, tmp_path, scratch
):
    await _seed(session)
    # Enough rows that the archive's data runs past its table of contents:
    # a cut inside the TOC fails pg_restore before it runs anything at all.
    await session.execute(
        sa.text(
            "INSERT INTO suppression (kind, key_type, key, source, reason) "
            "SELECT 'dnc', 'phone_tail', (910000000 + g)::text, 'owner_bot', "
            "'owner_command' FROM generate_series(1, 40000) AS g"
        )
    )
    await session.commit()
    recipient, identity = keys
    result = await backup.create_backup(recipient=recipient, directory=tmp_path, now=NOW)
    assert result.path is not None
    assert restore.restore(result.path, identity, to=scratch) == restore.EXIT_OK
    # A row the backup does not hold: pg_restore --clean would drop it.
    import psycopg

    with psycopg.connect(scratch.replace("postgresql+psycopg://", "postgresql://")) as c:
        c.execute(
            "INSERT INTO suppression (kind, key_type, key, source, reason) "
            "VALUES ('dnc', 'phone_tail', '900000009', 'owner_bot', 'owner_command')"
        )
    cut = tmp_path / "clientscrapper-20261024-034501.dump.age"
    sealed = result.path.read_bytes()
    cut.write_bytes(sealed[:-300])  # the last chunk lost
    with pytest.raises(restore.RestoreError) as refused:
        restore.restore(cut, identity, to=scratch, force=True)
    assert "age could not decrypt" in str(refused.value)
    # Nothing was dropped: the target is as it was, the extra row included.
    assert restore_drill.count_rows(scratch)["suppression"] == 40_004


async def test_a_second_drill_at_once_stands_aside(session, keys, tmp_path):
    """The scratch database has one name; a second drill would DROP … WITH
    (FORCE) the first's. It skips while the lock is held, and writes no beat."""
    import psycopg

    recipient, identity = keys
    folder = tmp_path / "backups"
    await backup.create_backup(recipient=recipient, directory=folder, now=NOW)
    drill_db = f"{_db_name()}_drill"
    dsn = _url(_db_name()).replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as holder:
        holder.execute(
            "SELECT pg_advisory_lock(hashtext(%s))", (restore_drill.DRILL_LOCK,)
        )
        busy = await restore_drill.run(
            identity=identity, database=drill_db, directory=folder
        )
        assert (busy.ok, busy.skipped) == (True, "busy")
        assert not _exists(drill_db)
    done = await restore_drill.run(identity=identity, database=drill_db, directory=folder)
    assert done.ok and done.skipped is None, done.error
