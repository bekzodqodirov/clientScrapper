"""Three Makefile recipes the T-01/T-02 review found wrong, read through
``make -n`` (the commands make would run, none executed).

``make psql`` expanded POSTGRES_USER/POSTGRES_DB in the host shell, which never
loaded .env; ``make db-test`` printed a recipe that was red when followed (an
unmigrated database, no scratch database for test_migrations); ``make
revision`` minted a random hex id where 02 §5 names ``000N_<slug>``.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]


def _load_revision_id():
    spec = importlib.util.spec_from_file_location(
        "revision_id", ROOT / "scripts" / "revision_id.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["revision_id"] = module
    spec.loader.exec_module(module)
    return module


rid = _load_revision_id()

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="no make")


def dry_run(*args: str) -> str:
    done = subprocess.run(
        ["make", "-s", "-n", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_psql_reads_the_role_from_the_db_containers_environment():
    out = dry_run("psql")
    assert 'exec db sh -c \'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"\'' in out
    assert ":-clientscrapper" not in out


def test_db_test_migrates_and_prints_both_databases():
    out = dry_run("db-test")
    assert "CREATE DATABASE cs_test_rt" in out
    migrate = [line for line in out.splitlines() if "alembic upgrade head" in line]
    assert migrate == [
        "DATABASE_URL=postgresql+psycopg://cs:cs@localhost:${DB_PORT:-5433}/cs_test "
        "alembic upgrade head"
    ]
    (printed,) = [line for line in out.splitlines() if line.startswith("echo ")]
    url = "postgresql+psycopg://cs:cs@localhost:${DB_PORT:-5433}"
    assert f"DATABASE_URL={url}/cs_test " in printed
    assert (
        "CS_MIGRATION_DATABASE_URL=postgresql+psycopg://cs:cs@localhost:"
        "${DB_PORT:-5433}/cs_test_rt" in printed
    )
    assert printed.endswith('CS_REQUIRE_DB=1 make test"')
    # Readiness over TCP, before anything talks to the server.
    assert out.index("pg_isready -q -h 127.0.0.1") < out.index("CREATE DATABASE")


def test_revision_passes_a_numbered_id():
    out = dry_run("revision", "m=Add outcomes no_contact")
    assert 'rev_id="$(python scripts/revision_id.py "Add outcomes no_contact")"' in out
    assert '--rev-id "$rev_id"' in out


@pytest.mark.parametrize(
    ("message", "slug"),
    [
        ("add outcomes no_contact", "add_outcomes_no_contact"),
        ("Push log: superseded_at!", "push_log_superseded_at"),
        ("  widen  the CHECK  ", "widen_the_check"),
        ("x" * 50, "x" * 40),
    ],
)
def test_the_slug(message, slug):
    assert rid.slug(message) == slug


def test_a_message_without_words_is_refused():
    with pytest.raises(ValueError):
        rid.slug("ʻʼ — …")


def test_the_next_number_is_one_past_the_highest(tmp_path):
    assert rid.revision_id("first", tmp_path) == "0001_first"
    for name in ("0001_initial.py", "0003_later.py", "cae10a182ef9_probe.py", "env.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    assert rid.revision_id("add x", tmp_path) == "0004_add_x"
    # This tree: 0001_initial, 0002_erp_contract, 0003_review_flow (T-21).
    assert rid.next_number() == 4


def test_alembic_names_the_file_after_the_id_alone(tmp_path):
    """alembic.ini's file_template, on a copy of the migrations tree."""
    scripts = tmp_path / "alembic"
    skip = shutil.ignore_patterns("__pycache__")
    shutil.copytree(ROOT / "alembic", scripts, ignore=skip)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(scripts))
    # The ruff post-write hook needs ruff on PATH; the name is what is tested.
    cfg.set_section_option("post_write_hooks", "hooks", "")
    rev = rid.revision_id("probe slug", scripts / "versions")
    command.revision(cfg, message="probe slug", rev_id=rev)
    assert rev == "0004_probe_slug"
    assert (scripts / "versions" / "0004_probe_slug.py").is_file()
    script = ScriptDirectory.from_config(cfg).get_revision(rev)
    assert script is not None and script.down_revision == "0003_review_flow"


# --- the backup recipes (06 §3.3, §4.4) ----------------------------------------------

# Options of `docker compose run` that take a value.
_RUN_VALUE_OPTIONS = {
    "--user",
    "-u",
    "-e",
    "--env",
    "-v",
    "--volume",
    "-w",
    "--entrypoint",
}


def _compose_run(line: str) -> tuple[str, dict[str, str], list[str]]:
    """(service, options with values, command) of a `docker compose run` line."""
    import shlex

    tokens = shlex.split(line)
    rest = tokens[tokens.index("run") + 1 :]
    options: dict[str, str] = {}
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        options[option] = rest.pop(0) if option in _RUN_VALUE_OPTIONS else ""
    return rest[0], options, rest[1:]


def _host_path(service: dict, container_path: str) -> tuple[str, bool]:
    """The host path a container path lands on, and whether it is writable."""
    best: tuple[str, str, bool] | None = None
    for volume in service.get("volumes", []):
        host, target, *mode = str(volume).split(":")
        inside = container_path == target or container_path.startswith(target + "/")
        if inside and (best is None or len(target) > len(best[1])):
            best = (host, target, "ro" not in mode)
    assert best is not None, f"{container_path} is on no mount"
    host, target, writable = best
    return str(Path(host) / Path(container_path).relative_to(target)), writable


def test_backup_key_writes_through_a_writable_mount_as_the_app_user():
    """The app services mount ./secrets read-only; the key must be written
    where the mount is writable, as uid 10001 (the 0600 file must be
    readable by the worker that decrypts the drill), and land on the very
    host file the readers then read."""
    from tests.test_compose import _services

    services = _services()
    out = dry_run("backup-key").splitlines()
    [writer] = [line for line in out if "age-keygen -o" in line]
    service, options, command = _compose_run(writer)
    written = command[command.index("-o") + 1]
    host, writable = _host_path(services[service], written)
    assert writable, writer
    user = options.get("--user") or options.get("-u") or services[service].get("user")
    assert user in (None, "10001:10001", "10001"), writer
    assert host == "secrets/backup-key.txt"
    readers = [line for line in out if "age-keygen -y" in line or " cat " in line]
    readers += [
        line for line in dry_run("backup-key-show").splitlines() if " cat " in line
    ]
    assert len(readers) == 2
    for line in readers:
        reader, _, rest = _compose_run(line)
        path = next(
            t for t in " ".join(rest).replace('"', " ").split() if "backup-key" in t
        )
        path = path.rstrip(")")
        assert _host_path(services[reader], path)[0] == host, line


def test_make_backup_runs_the_nightly_job_itself():
    """Not create_backup alone: the job beats `backup`, keeps the drill's
    counts and sends the Telegram copy, so a good manual run clears
    backup_failed (06 §4.6) and the drill can check a file `make update` wrote."""
    [line] = [x for x in dry_run("backup").splitlines() if "compose run" in x]
    service, _, command = _compose_run(line)
    assert service == "worker"
    assert command == ["python", "-m", "clientscrapper.worker.main", "backup"]


# --- switches: «1» is on, empty or «0» is off, anything else stops make ------------


def _flags(line: str) -> set[str]:
    return {t for t in line.replace("\\", " ").split() if t.startswith("--")}


@pytest.mark.parametrize(
    ("target", "switches"),
    [
        (
            ["restore", "FILE=/data/backups/x.dump.age"],
            {"DRY": "--dry-run", "FORCE": "--force"},
        ),
        (["purge", "PHONE=901234567"], {"APPLY": "--apply"}),
        (
            ["import-clients", "FILE=clients.xlsx"],
            {"APPLY": "--apply", "ACCEPT_FEWER": "--accept-fewer"},
        ),
    ],
)
def test_a_switch_is_on_only_at_1(target, switches):
    """The T-11 fixer's finding: `$(if $(X),…)` read FORCE=0 / DRY=0 /
    APPLY=0 as on. Off for empty and «0», on for «1», refused otherwise."""
    for off in (
        [],
        [f"{name}=0" for name in switches],
        [f"{name}=" for name in switches],
    ):
        assert not _flags(dry_run(*target, *off)) & set(switches.values()), off
    on = _flags(dry_run(*target, *(f"{name}=1" for name in switches)))
    assert set(switches.values()) <= on
    for name in switches:
        done = subprocess.run(
            ["make", "-s", "-n", *target, f"{name}=yes"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert done.returncode != 0
        assert f"{name}=yes: use {name}=1 (on) or {name}=0 (off)" in done.stderr


def test_skip_doctor_skips_only_at_1():
    assert "tools.doctor" in dry_run("up", "SKIP_DOCTOR=0")
    assert "tools.doctor" in dry_run("up")
    assert "tools.doctor" not in dry_run("up", "SKIP_DOCTOR=1")


def test_mvp_report_passes_the_start_only_when_given():
    out = dry_run("mvp-report", "START=2026-11-02")
    assert 'clientscrapper.tools.mvp_report \\\n\t--start "2026-11-02"' in out
    bare = dry_run("mvp-report")
    assert "clientscrapper.tools.mvp_report" in bare and "--start" not in bare
