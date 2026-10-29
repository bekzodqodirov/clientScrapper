"""``make smoke`` on the VPS (07 §1.12, 03 §1.4; T-01's carried note: the
host has no dev venv and the app image ships neither pytest nor tests/).

* The recorder writes one ``collector_runs`` row per live test — skipped,
  ``meta.note='smoke'``, its outcome in ``meta.smoke``; a host breaker ends
  it ``failed`` / ``host_blocked`` — and nothing else (run for real, in a
  child pytest over planted live tests).
* A smoke row never restarts a silent collector's clock.
* Every live test names its source and never takes the database-wiping
  ``session`` fixture, which refuses under ``LIVE=1`` anyway.
* The Makefile, compose service and Dockerfile stage that run it.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml

from clientscrapper.config import settings
from clientscrapper.db.models import CollectorRun, Source
from clientscrapper.services import health, smoke
from tests import conftest as root_conftest
from tests.test_health import NOW

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "tests" / "live"

PLANTED = """
from datetime import datetime, timezone

import pytest

from clientscrapper.services import http


@pytest.mark.live(source="hh_vacancies")
def test_the_selector_still_finds_rows():
    assert True


@pytest.mark.live(source="olx_business")
def test_the_selector_drifted():
    assert False, "no row"


@pytest.mark.live(source="instagram_discovery")
def test_no_token_on_this_server():
    pytest.skip("IG_ACCESS_TOKEN is empty")


@pytest.mark.live(source="directories")
def test_the_host_blocked_us():
    raise http.HostBlocked("www.namuna.example.uz", datetime.now(timezone.utc))


@pytest.mark.live
def test_without_a_source():
    assert True
"""
CONFTEST = "from tests.live.conftest import pytest_configure  # noqa: F401\n"


def child_pytest(folder: Path, **env: str) -> subprocess.CompletedProcess[str]:
    environ = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "LIVE": "1",
        **env,
    }
    environ.pop("CS_REQUIRE_DB", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-W",
            "ignore::pytest.PytestUnknownMarkWarning",
            str(folder),
        ],
        cwd=folder,
        env=environ,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


async def test_the_recorder_writes_one_row_per_live_test_and_nothing_else(
    session, tmp_path
):
    (tmp_path / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (tmp_path / "test_planted_live.py").write_text(PLANTED, encoding="utf-8")
    done = child_pytest(tmp_path, CS_SMOKE_FULL="1")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "smoke: 4 outcome(s) recorded for the digest" in done.stdout
    assert "test_without_a_source names no source" in done.stdout
    rows = (
        await session.execute(
            sa.select(
                CollectorRun.source_key,
                CollectorRun.status,
                CollectorRun.error,
                CollectorRun.meta,
            ).order_by(CollectorRun.id)
        )
    ).all()
    assert [(r[0], r[1], r[2], r[3]["smoke"], r[3]["full"]) for r in rows] == [
        ("hh_vacancies", "skipped", None, "passed", True),
        ("olx_business", "skipped", None, "failed", True),
        ("instagram_discovery", "skipped", None, "skipped", True),
        ("directories", "failed", "host_blocked", "failed", True),
    ]
    assert {r[3]["note"] for r in rows} == {"smoke"}
    assert {r[3]["variant"] for r in rows} == {"smoke"}
    assert len({r[3]["run"] for r in rows}) == 1
    # One outcome per test, read back as the digest's line.
    run = await smoke.latest(session, now=datetime.now(settings.tz))
    assert run is not None and run.full is True and run.counts == (1, 2, 1)
    assert [len(run.passed), len(run.failed), len(run.skipped)] == [1, 2, 1]
    # «Live tests never write anywhere but collector_runs» (07 §1.12).
    for table in ("evidence", "observations", "companies", "heartbeats"):
        assert await session.scalar(sa.text(f"SELECT count(*) FROM {table}")) == 0


PHASES = """
import pytest


@pytest.fixture
def needs_token():
    pytest.skip("no token")


@pytest.fixture
def breaks_after():
    yield
    raise RuntimeError("teardown broke")


@pytest.fixture
def breaks_before():
    raise RuntimeError("setup broke")


@pytest.mark.live(source="hh_vacancies")
def test_a_fixture_skips_in_setup(needs_token):
    assert True


@pytest.mark.live(source="olx_business")
def test_a_pass_whose_teardown_fails(breaks_after):
    assert True


@pytest.mark.live(source="instagram_discovery")
def test_a_failure_whose_teardown_fails_too(breaks_after):
    assert False


@pytest.mark.live(source="directories")
def test_a_fixture_fails_in_setup(breaks_before):
    assert True
"""


async def test_the_recorder_reads_setup_and_teardown_too(session, tmp_path):
    """A test skipped or broken in its setup never reaches its call, and a
    failed teardown turns a pass into a failure: each is recorded so."""
    (tmp_path / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (tmp_path / "test_phases_live.py").write_text(PHASES, encoding="utf-8")
    done = child_pytest(tmp_path)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "smoke: 4 outcome(s) recorded for the digest" in done.stdout
    rows = (
        await session.execute(
            sa.select(
                CollectorRun.source_key, CollectorRun.meta["smoke"].astext
            ).order_by(CollectorRun.id)
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        ("hh_vacancies", "skipped"),
        ("olx_business", "failed"),
        ("instagram_discovery", "failed"),
        ("directories", "failed"),
    ]


def test_a_session_with_no_live_test_yet_is_not_an_error(tmp_path):
    (tmp_path / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (tmp_path / "test_nothing_yet_live.py").write_text("", encoding="utf-8")
    done = child_pytest(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "smoke: no live test yet" in done.stdout


def test_without_live_the_recorder_is_not_registered(tmp_path):
    (tmp_path / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (tmp_path / "test_planted_live.py").write_text(PLANTED, encoding="utf-8")
    done = child_pytest(tmp_path, LIVE="0")
    assert "outcome(s) recorded" not in done.stdout


async def test_the_session_fixture_refuses_under_live(monkeypatch):
    monkeypatch.setenv("LIVE", "1")
    fixture = root_conftest.session.__pytest_wrapped__.obj  # type: ignore[attr-defined]
    with pytest.raises(pytest.fail.Exception, match="wipes the database"):
        await fixture().__anext__()


# --- a smoke row is not the scheduler's ---------------------------------------------


@pytest.fixture
async def hh_row(session):
    keep = (
        await session.execute(
            sa.select(Source.enabled, Source.cron, Source.last_run_at).where(
                Source.key == "hh_vacancies"
            )
        )
    ).one()
    await session.execute(
        sa.update(Source)
        .where(Source.key == "hh_vacancies")
        .values(enabled=True, cron="20 */4 * * *", last_run_at=NOW - timedelta(days=1))
    )
    await session.commit()
    yield
    await session.execute(
        sa.update(Source)
        .where(Source.key == "hh_vacancies")
        .values(enabled=keep[0], cron=keep[1], last_run_at=keep[2])
    )
    await session.commit()


async def silent_keys(session) -> list[str]:
    status = await health.gather(session, now=NOW)
    return [s.key for s, _ in health.silent_sources(status)]


async def test_a_smoke_row_does_not_hide_a_silent_collector(session, hh_row):
    assert "hh_vacancies" in await silent_keys(session)
    session.add(
        CollectorRun(
            source_key="hh_vacancies",
            started_at=NOW - timedelta(hours=1),
            finished_at=NOW - timedelta(hours=1),
            status="skipped",
            meta={"note": "smoke", "variant": "smoke", "smoke": "passed"},
        )
    )
    await session.commit()
    assert "hh_vacancies" in await silent_keys(session)
    # Control: the scheduler's own skip does restart the clock.
    session.add(
        CollectorRun(
            source_key="hh_vacancies",
            started_at=NOW - timedelta(hours=1),
            finished_at=NOW - timedelta(hours=1),
            status="skipped",
            meta={"note": "paused"},
        )
    )
    await session.commit()
    assert "hh_vacancies" not in await silent_keys(session)


async def test_a_smoke_row_is_not_a_run_on_the_source_card(session):
    """/manbalar's «last runs» are the scheduler's: a smoke row (status
    skipped) there would read as a run the scheduler skipped."""
    from clientscrapper.services import owner

    session.add_all(
        [
            CollectorRun(
                source_key="hh_vacancies",
                started_at=NOW - timedelta(hours=2),
                status="ok",
                new_companies=3,
            ),
            CollectorRun(
                source_key="hh_vacancies",
                started_at=NOW - timedelta(hours=1),
                status="skipped",
                meta={"note": "smoke", "variant": "smoke", "smoke": "passed"},
            ),
        ]
    )
    await session.commit()
    info = await owner.source_info(session, "hh_vacancies", now=NOW)
    assert info is not None
    assert [(r.status, r.new_companies) for r in info.runs] == [("ok", 3)]


async def test_a_blocked_smoke_row_is_its_own_cadence(session, hh_row):
    """A host_blocked smoke row neither breaks nor makes a real cadence's
    failing streak (job_failing): it is variant «smoke»."""
    for n in range(3):
        session.add(
            CollectorRun(
                source_key="hh_vacancies",
                started_at=NOW - timedelta(hours=3 - n),
                finished_at=NOW - timedelta(hours=3 - n),
                status="failed",
                error="timeout",
            )
        )
    session.add(
        CollectorRun(
            source_key="hh_vacancies",
            started_at=NOW - timedelta(minutes=10),
            finished_at=NOW - timedelta(minutes=9),
            status="failed",
            error="host_blocked",
            meta={"note": "smoke", "variant": "smoke", "smoke": "failed"},
        )
    )
    await session.commit()
    status = await health.gather(session, now=NOW)
    assert [(r.source_key, r.variant, r.runs) for r in status.failing_runs] == [
        ("hh_vacancies", "", 3)
    ]


# --- every live test names its source, and never wipes the database ---------------------

FORBIDDEN_FIXTURES = frozenset({"session", "db"})


def live_test_problems(source: str, known: set[str]) -> list[str]:
    """What a ``tests/live/test_*.py`` source breaks: a test without
    ``pytest.mark.live(source=<literal>)`` (on itself or the module's
    ``pytestmark``), a source no ``sources`` row has, or a fixture that wipes
    the database."""
    tree = ast.parse(source)

    def live_source(decorators: list[ast.expr]) -> str | None | bool:
        for node in decorators:
            target = node.func if isinstance(node, ast.Call) else node
            if ast.unparse(target) != "pytest.mark.live":
                continue
            if not isinstance(node, ast.Call):
                return None
            for kw in node.keywords:
                if kw.arg == "source" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
            return None
        return False

    module_mark: str | None | bool = False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            marks = node.value.elts if isinstance(node.value, ast.List) else [node.value]
            module_mark = live_source(list(marks))
    found: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test"):
            continue
        mark = live_source(node.decorator_list)
        if mark is False:
            mark = module_mark
        if mark is False or mark is None:
            found.append(f"{node.name}: no pytest.mark.live(source=...)")
        elif mark not in known:
            found.append(f"{node.name}: {mark} is no sources.key")
        wiping = {a.arg for a in node.args.args} & FORBIDDEN_FIXTURES
        if wiping:
            found.append(f"{node.name}: takes {sorted(wiping)}")
    return found


async def seeded_keys(session) -> set[str]:
    return set((await session.scalars(sa.select(Source.key))).all())


async def test_every_live_test_names_a_source_and_spares_the_database(session):
    known = await seeded_keys(session)
    files = sorted(LIVE_DIR.glob("test_*.py"))
    found = {
        str(path.relative_to(ROOT)): live_test_problems(path.read_text("utf-8"), known)
        for path in files
    }
    assert {name: issues for name, issues in found.items() if issues} == {}


async def test_the_live_test_detector_sees_each_shape(session):
    known = await seeded_keys(session)
    assert live_test_problems(PLANTED, known) == [
        "test_without_a_source: no pytest.mark.live(source=...)"
    ]
    module = (
        "import pytest\npytestmark = [pytest.mark.live(source='nonesuch')]\n"
        "def test_a(session):\n    pass\n"
    )
    assert live_test_problems(module, known) == [
        "test_a: nonesuch is no sources.key",
        "test_a: takes ['session']",
    ]
    fine = (
        "import pytest\n"
        "pytestmark = pytest.mark.live(source='maps')\n"
        "def test_b():\n    pass\n"
    )
    assert live_test_problems(fine, known) == []


# --- make smoke, the compose service, the image stage ------------------------------


def make_n(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-s", "-n", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )


needs_make = pytest.mark.skipif(shutil.which("make") is None, reason="no make")


@needs_make
def test_make_smoke_runs_the_live_tests_in_the_smoke_service():
    weekly = " ".join(make_n("smoke").stdout.split())
    assert weekly == (
        "docker compose --profile smoke run --rm --build smoke \\ python -m pytest -q "
        '-rs -p no:cacheprovider -m "live and not quarterly" tests/live'
    )
    full = " ".join(make_n("smoke", "FULL=1").stdout.split())
    assert "run --rm --build -e CS_SMOKE_FULL=1 smoke" in full
    assert full.endswith('-m "live" tests/live')
    refused = make_n("smoke", "FULL=yes")
    assert refused.returncode != 0 and "FULL=yes: use FULL=1" in refused.stderr


@needs_make
def smoke_cron_lines() -> list[str]:
    done = subprocess.run(
        ["make", "-s", "smoke-cron"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return done.stdout.splitlines()


def test_make_smoke_cron_prints_the_weekly_and_the_quarterly_line():
    log = f">> {ROOT}/data/smoke.log 2>&1"
    assert smoke_cron_lines() == [
        '0 * * * * [ "$(TZ=Asia/Tashkent date +\\%u\\%H)" = 705 ] && '
        f"cd {ROOT} && make smoke {log}",
        '30 * * * * case "$(TZ=Asia/Tashkent date +\\%m\\%d\\%H)" in '
        f"010105|040105|070105|100105) cd {ROOT} && make smoke FULL=1 {log};; esac",
    ]


@pytest.mark.parametrize(
    ("line", "now", "ran"),
    [
        (0, "705", "weekly"),  # Sunday 05 in Tashkent
        (0, "704", None),  # Sunday 04 (an Ubuntu host's 23:00 UTC Saturday)
        (0, "105", None),  # Monday 05
        (1, "010105", "full"),  # 1 January 05
        (1, "100105", "full"),  # 1 October 05
        (1, "050105", None),  # 1 May
        (1, "010106", None),  # the hour after
    ],
)
def test_the_smoke_cron_lines_run_at_tashkent_s_hour_on_any_host(
    tmp_path, line, now, ran
):
    """Cron runs a line in the host's zone (UTC on a stock Ubuntu VPS): each
    line fires hourly and runs make smoke only when ``date`` in Tashkent's
    zone says it is the hour. The command is the one cron hands to sh — its
    ``\\%`` unescaped — with make replaced and ``date`` a stand-in that
    answers only when asked in Asia/Tashkent."""
    fields = smoke_cron_lines()[line].split(" ", 5)
    command = fields[5].replace("\\%", "%")
    command = command.replace("make smoke FULL=1", "echo full").replace(
        "make smoke", "echo weekly"
    )
    command = command.replace(f"{ROOT}/data/smoke.log", str(tmp_path / "smoke.log"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "date"
    fake.write_text(
        '#!/bin/sh\n[ "$TZ" = Asia/Tashkent ] && echo "$FAKE_NOW" || echo 0\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_NOW": now}
    subprocess.run(["sh", "-c", command], env=env, check=False)
    log = tmp_path / "smoke.log"
    said = log.read_text(encoding="utf-8").split() if log.exists() else []
    assert said == ([] if ran is None else [ran])


def test_the_smoke_service_runs_only_when_asked():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["smoke"]
    assert service["profiles"] == ["smoke"]
    assert service["build"] == {"context": ".", "target": "smoke"}
    assert service["restart"] == "no"
    assert service["environment"]["LIVE"] == "1"
    assert "ports" not in service and "healthcheck" not in service
    assert service["command"][-1] == "tests/live"
    assert service["env_file"] == ".env" and service.get("mem_limit")


def test_the_image_the_services_run_is_still_the_app():
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    froms = [line for line in lines if line.startswith("FROM ")]
    assert froms == ["FROM python:3.12-slim AS app", "FROM app AS smoke", "FROM app"]
    start = lines.index("FROM app AS smoke")
    stage = lines[start : lines.index("FROM app")]
    assert "COPY tests ./tests" in stage
    assert "COPY requirements-dev.txt pyproject.toml ./" in stage
    assert [line for line in stage if line.startswith("USER ")][
        -1
    ] == "USER clientscrapper"
