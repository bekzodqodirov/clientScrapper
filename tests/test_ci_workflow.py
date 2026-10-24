"""The CI workflow has the four jobs and the gates 07 §2.1 asks for.

A shape fence on .github/workflows/ci.yml, read as YAML: a job dropped, a
gate reordered, the database requirement lost or a referenced script missing
is red here before a push finds it on GitHub.
"""

from __future__ import annotations

import re

import yaml

from tests.ast_tools import ROOT

WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
JOBS = WORKFLOW["jobs"]


def _runs(job: str) -> list[str]:
    return [step["run"] for step in JOBS[job]["steps"] if "run" in step]


def _index(runs: list[str], needle: str) -> int:
    [i] = [n for n, run in enumerate(runs) if needle in run]
    return i


def test_the_four_required_jobs():
    assert set(JOBS) == {"lint", "test", "time-travel", "docker"}
    # PyYAML reads the bare key `on` as True (YAML 1.1); GitHub reads "on".
    triggers = WORKFLOW.get("on", WORKFLOW.get(True))
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_lint_scans_for_secrets_before_anything_else():
    runs = _runs("lint")
    order = [
        _index(runs, "pip install"),
        _index(runs, "scripts/secret_scan.py"),
        _index(runs, "ruff check ."),
        _index(runs, "ruff format --check ."),
        _index(runs, "mypy"),
    ]
    assert order == sorted(order)


def test_the_test_job_requires_its_database_and_checks_drift_first():
    job = JOBS["test"]
    assert job["services"]["postgres"]["image"] == "postgres:16"
    env = job["env"]
    assert env["CS_REQUIRE_DB"] == "1"
    assert env["TZ"] == "Asia/Tashkent"
    assert env["CS_FORBIDDEN_STRINGS"] == "${{ secrets.CS_FORBIDDEN_STRINGS }}"
    assert env["CS_MIGRATION_DATABASE_URL"].endswith("/cs_rt")
    runs = _runs("test")
    round_trip = runs[_index(runs, "CREATE DATABASE cs_rt")]
    assert [line.split()[-2:] for line in round_trip.splitlines()[1:]] == [
        ["upgrade", "head"],
        ["downgrade", "base"],
        ["upgrade", "head"],
    ]
    upgrade = runs.index("alembic upgrade head")
    check = runs.index("alembic check")
    suite = _index(runs, "pytest -q")
    assert upgrade < check < suite
    [pytest_step] = [s for s in job["steps"] if "pytest" in s.get("run", "")]
    assert pytest_step["env"]["EVIDENCE_DIR"] == "${{ runner.temp }}/evidence"


def test_time_travel_runs_without_a_database_at_two_moments():
    job = JOBS["time-travel"]
    assert job["env"]["DATABASE_URL"].endswith("@127.0.0.1:1/none")
    moments = [re.search(r"CS_TIME_TRAVEL=(\S+)", r) for r in _runs("time-travel")]
    assert [m.group(1) for m in moments if m] == ["newyear", "+400d"]


def test_the_docker_job_parses_env_example_through_compose():
    runs = _runs("docker")
    assert _index(runs, "docker build") < _index(runs, "check_compose_env.py")
    compose = runs[_index(runs, "check_compose_env.py")]
    assert compose.splitlines()[0] == "cp .env.example .env"


def test_every_script_the_workflow_names_exists():
    named = set()
    for job in JOBS:
        for run in _runs(job):
            named |= set(re.findall(r"scripts/[\w.-]+", run))
    assert named == {"scripts/secret_scan.py", "scripts/check_compose_env.py"}
    assert [s for s in named if not (ROOT / s).is_file()] == []


def test_make_hooks_installs_the_pre_push_script():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^hooks:.*\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    assert recipe, "Makefile has no hooks target"
    assert "scripts/pre-push.sh" in recipe.group(1)
    assert "pre-push" in recipe.group(1).splitlines()[-1]
