"""scripts/pre-push.sh stops at the first red gate (07 §2.2, AC).

The gates are run from the script's own text, never restated: a line is cut
out of the file and executed by bash under the script's ``set -euo
pipefail``, in a scratch git repository; the whole script is run with fake
tools on PATH, so ordering is proven without running the real suite inside
the suite.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.ast_tools import ROOT

SCRIPT = ROOT / "scripts" / "pre-push.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
GATES = [
    "secrets",
    "ruff",
    "mypy",
    "guards",
    "suite",
    "migrations",
    "trailers",
]
TRAILERS = "\n\nCo-Authored-By: Namuna CI <ci@example.uz>\n"
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _gate(n: int) -> str:
    """The script's line for gate ``n`` (one physical line)."""
    [line] = [x for x in TEXT.splitlines() if x.startswith(f'echo "[{n}/7]')]
    return line


def _bash(command: str, cwd: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{command}"],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def _repo(path: Path, message: str) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)

    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q")
    git("config", "user.email", "ci@example.uz")
    git("config", "user.name", "CI")
    (path / "README.md").write_text("x\n")
    git("add", "README.md")
    git("commit", "-q", "-m", message)
    return path


def test_the_script_is_executable_strict_and_unpiped():
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR
    assert TEXT.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in TEXT
    markers = re.findall(r'^echo "\[(\d)/7\] (\w+)"', TEXT, re.MULTILINE)
    assert markers == [(str(i), g) for i, g in enumerate(GATES, start=1)]
    code = [line for line in TEXT.splitlines() if not line.lstrip().startswith("#")]
    unquoted = [re.sub(r"\"[^\"]*\"|'[^']*'", "", line) for line in code]
    assert [line for line in unquoted if re.search(r"(?<!\|)\|(?!\|)", line)] == []


def test_every_listed_guard_exists():
    block = TEXT[TEXT.index("[4/7]") : TEXT.index("[5/7]")]
    guards = re.findall(r"tests/\w+\.py", block)
    assert len(guards) == 8
    assert [g for g in guards if not (ROOT / g).is_file()] == []


def test_the_secrets_gate_refuses_a_planted_key(tmp_path):
    repo = _repo(tmp_path, "x" + TRAILERS)
    (repo / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "secret_scan.py", repo / "scripts")
    assert _bash(_gate(1), repo).returncode == 0
    (repo / "notes.md").write_text("key: sk-" + "ant-" + "x" * 30 + "\n")
    result = _bash(_gate(1), repo)
    assert result.returncode == 1
    assert "notes.md:1: anthropic key" in result.stdout


def test_the_trailer_gate_refuses_a_commit_without_co_authored_by(tmp_path):
    bare = _repo(tmp_path / "bare", "T-04: no trailer\n\nsigned by nobody")
    result = _bash(_gate(7), bare)
    assert result.returncode == 1
    assert "missing Co-Authored-By trailer" in result.stdout
    signed = _repo(tmp_path / "signed", "T-04: with trailer" + TRAILERS)
    assert _bash(_gate(7), signed).returncode == 0


def test_the_migration_gate_fails_when_alembic_check_fails(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "alembic"
    fake.write_text("#!/usr/bin/env bash\necho 'drift'\nexit 1\n")
    fake.chmod(0o755)
    path = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    red = _bash(_gate(6), tmp_path, {**path, "CS_REQUIRE_DB": "1"})
    assert red.returncode == 1 and "drift" in red.stdout
    skipped = _bash(_gate(6), tmp_path, {**path, "CS_REQUIRE_DB": "0"})
    assert skipped.returncode == 0 and "alembic check skipped" in skipped.stdout


# A fake fails when FAKE_FAIL names it, "it <first arg>" or "it <last arg>".
FAKE = """#!/usr/bin/env bash
name=$(basename "$0")
for wanted in "$name" "$name $1" "$name ${@: -1}"; do
  if [ "${FAKE_FAIL:-}" = "$wanted" ]; then echo "$wanted red"; exit 1; fi
done
exit 0
"""


def _fake_tools(bin_dir: Path) -> dict[str, str]:
    bin_dir.mkdir()
    for tool in ("python", "ruff", "mypy", "pytest", "alembic"):
        (bin_dir / tool).write_text(FAKE)
        (bin_dir / tool).chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


@pytest.mark.parametrize(
    ("failing", "first_red"),
    [
        ("python", 1),
        ("ruff", 2),
        ("ruff format", 2),  # the second command of a gate stops it too
        ("mypy", 3),
        ("pytest", 4),
        ("pytest -x", 5),
        ("alembic", 6),
    ],
)
def test_the_chain_stops_at_the_first_red_gate(tmp_path, failing, first_red):
    repo = _repo(tmp_path / "repo", "x" + TRAILERS)
    shutil.copy(SCRIPT, repo / "pre-push.sh")
    env = {**_fake_tools(tmp_path / "bin"), "FAKE_FAIL": failing, "CS_REQUIRE_DB": "1"}
    result = subprocess.run(
        ["bash", "pre-push.sh"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert f"{failing} red" in result.stdout
    shown = re.findall(r"^\[(\d)/7\]", result.stdout, re.MULTILINE)
    assert shown == [str(i) for i in range(1, first_red + 1)]
    assert "pre-push: green" not in result.stdout


def test_all_green_with_fake_tools(tmp_path):
    repo = _repo(tmp_path / "repo", "x" + TRAILERS)
    shutil.copy(SCRIPT, repo / "pre-push.sh")
    env = _fake_tools(tmp_path / "bin")
    result = subprocess.run(
        ["bash", "pre-push.sh"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("pre-push: green")
