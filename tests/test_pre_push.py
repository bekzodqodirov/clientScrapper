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
from tests.git_env import isolated_env

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
TRAILERS = (
    "\n\nCo-Authored-By: Namuna CI <ci@example.uz>\n"
    "Claude-Session: https://namuna.example.uz/session\n"
)
CHECK = ROOT / "scripts" / "check_trailers.sh"
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _gate(n: int) -> str:
    """The script's line for gate ``n`` (one physical line)."""
    [line] = [x for x in TEXT.splitlines() if x.startswith(f'echo "[{n}/7]')]
    return line


def _bash(command: str, cwd: Path, env: dict[str, str] | None = None, *, stdin: str = ""):
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{command}"],
        cwd=cwd,
        env=isolated_env(env),
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _repo(path: Path, message: str) -> Path:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=path, env=isolated_env(), check=True, capture_output=True
        )

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


def _with_check(repo: Path) -> Path:
    (repo / "scripts").mkdir(exist_ok=True)
    shutil.copy(CHECK, repo / "scripts")
    return repo


def _commit(repo: Path, message: str) -> str:
    (repo / "README.md").write_text(message)
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_the_trailer_gate_refuses_a_commit_without_co_authored_by(tmp_path):
    bare = _with_check(_repo(tmp_path / "bare", "T-04: no trailer\n\nsigned by nobody"))
    result = _bash(_gate(7), bare, {"PUSHED": ""})
    assert result.returncode == 1
    assert "missing Co-Authored-By trailer" in result.stdout
    assert "missing Claude-Session trailer" in result.stdout
    signed = _with_check(_repo(tmp_path / "signed", "T-04: with trailer" + TRAILERS))
    assert _bash(_gate(7), signed, {"PUSHED": ""}).returncode == 0
    half = "T-04: one trailer\n\nCo-Authored-By: Namuna CI <ci@example.uz>\n"
    one = _with_check(_repo(tmp_path / "one", half))
    result = _bash(_gate(7), one, {"PUSHED": ""})
    assert result.returncode == 1
    reported = [line.split(":")[0] for line in result.stdout.splitlines()[1:]]
    assert reported == ["missing Claude-Session trailer"]


def test_every_pushed_commit_carries_both_trailers_not_only_the_tip(tmp_path):
    # The seam review's case: three commits pushed, the middle one unsigned.
    repo = _with_check(_repo(tmp_path / "repo", "base" + TRAILERS))
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "first" + TRAILERS)
    unsigned = _commit(repo, "second, unsigned")
    tip = _commit(repo, "third" + TRAILERS)
    line = f"refs/heads/wt {tip} refs/heads/wt {base}\n"
    result = _bash("bash scripts/check_trailers.sh", repo, stdin=line)
    assert result.returncode == 1
    short = _git(repo, "rev-parse", "--short", unsigned)
    assert result.stdout.splitlines() == [
        f"missing Co-Authored-By trailer: {short}",
        f"missing Claude-Session trailer: {short}",
    ]
    # Only what the push sends is read: the remote already has «second».
    sent = f"refs/heads/wt {tip} refs/heads/wt {unsigned}\n"
    assert _bash("bash scripts/check_trailers.sh", repo, stdin=sent).returncode == 0
    # A new branch sends what no remote has (here: every commit); a deletion
    # sends nothing.
    zeros = "0" * 40
    new = f"refs/heads/wt {tip} refs/heads/wt {zeros}\n"
    assert _bash("bash scripts/check_trailers.sh", repo, stdin=new).returncode == 1
    gone = f"(delete) {zeros} refs/heads/old {tip}\n"
    assert _bash("bash scripts/check_trailers.sh", repo, stdin=gone).returncode == 0


def test_the_hook_hands_its_stdin_to_the_trailer_gate(tmp_path):
    repo = _repo(tmp_path / "repo", "base" + TRAILERS)
    base = _git(repo, "rev-parse", "HEAD")
    unsigned = _commit(repo, "unsigned")
    tip = _commit(repo, "tip" + TRAILERS)
    shutil.copy(SCRIPT, repo / "pre-push.sh")
    _with_check(repo)
    result = subprocess.run(
        ["bash", "pre-push.sh"],
        cwd=repo,
        env=_fake_tools(tmp_path / "bin"),
        input=f"refs/heads/wt {tip} refs/heads/wt {base}\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    short = _git(repo, "rev-parse", "--short", unsigned)
    assert f"missing Co-Authored-By trailer: {short}" in result.stdout
    assert "pre-push: green" not in result.stdout


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
    return isolated_env({"PATH": f"{bin_dir}:{os.environ['PATH']}"})


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
    _with_check(repo)
    env = {**_fake_tools(tmp_path / "bin"), "FAKE_FAIL": failing, "CS_REQUIRE_DB": "1"}
    result = subprocess.run(
        ["bash", "pre-push.sh"],
        cwd=repo,
        env=env,
        input="",
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
    _with_check(repo)
    env = _fake_tools(tmp_path / "bin")
    result = subprocess.run(
        ["bash", "pre-push.sh"],
        cwd=repo,
        env=env,
        input="",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("pre-push: green")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=isolated_env(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _victim(path: Path) -> Path:
    """A repository standing in for the checkout whose hook runs the suite."""
    repo = _repo(path, "victim: base")
    _git(repo, "config", "user.email", "victim@example.uz")
    return repo


def test_scratch_repositories_ignore_an_inherited_git_dir(tmp_path, monkeypatch):
    """A hook in a linked worktree exports GIT_DIR; the suite's scratch git
    must still act on its own directory, not on the repository being pushed."""
    victim = _victim(tmp_path / "victim")
    monkeypatch.setenv("GIT_DIR", str(victim / ".git"))
    monkeypatch.setenv("GIT_PREFIX", "")
    scratch = _with_check(_repo(tmp_path / "scratch", "x" + TRAILERS))
    assert _git(victim, "config", "user.email") == "victim@example.uz"
    assert _git(victim, "log", "--format=%s") == "victim: base"
    assert (scratch / ".git").is_dir()
    assert _bash(_gate(7), scratch, {"PUSHED": ""}).returncode == 0  # scratch's


def test_the_hook_does_not_lend_its_git_dir_to_the_gates(tmp_path):
    """The whole script, run as git runs a linked worktree's hook: GIT_DIR
    names another repository, and a gate that runs git in its own directory
    (the suite's scratch repositories do) must not reach it."""
    victim = _victim(tmp_path / "victim")
    repo = _repo(tmp_path / "repo", "x" + TRAILERS)
    shutil.copy(SCRIPT, repo / "pre-push.sh")
    _with_check(repo)
    env = _fake_tools(tmp_path / "bin")
    # The suite's gate stands in for tests/test_pre_push.py's own _repo().
    (tmp_path / "bin" / "pytest").write_text(
        "#!/usr/bin/env bash\ngit config user.email ci@example.uz\n"
    )
    env = {**env, "GIT_DIR": str(victim / ".git"), "GIT_PREFIX": ""}
    result = subprocess.run(
        ["bash", "pre-push.sh"],
        cwd=repo,
        env=env,
        input="",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(victim, "config", "user.email") == "victim@example.uz"
    assert _git(repo, "config", "user.email") == "ci@example.uz"
