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
    assert rid.next_number() == 2  # this tree: 0001_initial only


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
    assert rev == "0002_probe_slug"
    assert (scripts / "versions" / "0002_probe_slug.py").is_file()
    script = ScriptDirectory.from_config(cfg).get_revision(rev)
    assert script is not None and script.down_revision == "0001_initial"
