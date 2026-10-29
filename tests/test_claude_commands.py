"""``CLAUDE.md``'s command table matches the Makefile (07 §4.1 T-36).

The orientation file every session reads first names seven commands («keep
these names»); each must be a Makefile target, every ``VAR=…`` it shows must
reach that target's recipe, and the one-line comment beside it must say what
the recipe does: each comment is pinned word for word (``COMMENTS``) beside
what it promises (``PROMISES``), so a comment that changes is re-checked.
Read through ``make -n`` (the commands make would run, none executed), like
tests/test_makefile.py.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = ROOT / "CLAUDE.md"
MAKEFILE = ROOT / "Makefile"

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="no make")

# One ``make <target> [VAR=value]`` of the table.
_COMMAND = re.compile(r"make\s+(?P<target>[a-z][a-z0-9-]*)(?P<vars>(?:\s+[A-Z_]+=\S+)*)")
_VAR = re.compile(r"([A-Z_]+)=(\S+)")
_TARGET = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*):", re.MULTILINE)


def commands_block(text: str) -> list[str]:
    """The lines of the fenced block under «## Commands»."""
    section = text.split("## Commands", 1)[1]
    block = section.split("```", 2)[1]
    return [line for line in block.splitlines() if line.strip()]


def table() -> list[tuple[str, dict[str, str], str]]:
    """(target, {VAR: example}, comment) for every ``make`` the block shows
    (``make backup / make restore FILE=…`` is two)."""
    found = []
    for line in commands_block(CLAUDE_MD.read_text(encoding="utf-8")):
        command, _, comment = line.partition("#")
        for match in _COMMAND.finditer(command):
            found.append(
                (match["target"], dict(_VAR.findall(match["vars"])), comment.strip())
            )
    return found


def targets() -> set[str]:
    return set(_TARGET.findall(MAKEFILE.read_text(encoding="utf-8")))


def dry_run(target: str, **variables: str) -> str:
    done = subprocess.run(
        ["make", "-s", "-n", target, *(f"{k}={v}" for k, v in variables.items())],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_the_table_is_read_whole():
    assert [(t, sorted(v)) for t, v, _ in table()] == [
        ("up", []),
        ("migrate", []),
        ("doctor", []),
        ("userbot-login", []),
        ("test", []),
        ("backup", []),
        ("restore", ["FILE"]),
        ("import-clients", ["FILE"]),
    ]


@pytest.mark.parametrize(("target", "variables", "comment"), table())
def test_every_command_is_a_makefile_target(target, variables, comment):
    assert target in targets()
    # A variable the table shows reaches the recipe (an example value that
    # is the table's «…» is given a real-looking one).
    given = {
        k: ("/data/backups/x.dump.age" if v == "…" else v) for k, v in variables.items()
    }
    out = dry_run(target, **given)
    for value in given.values():
        assert value in out


# Each line's comment as CLAUDE.md prints it, and what that comment promises
# as the recipe must show it: the promises were checked against these exact
# words, so a comment that changes must change here too (and be re-checked).
COMMENTS = {
    "up": "docker compose up -d (db, init, worker, userbot, bot)",
    "migrate": "alembic upgrade head",
    "doctor": "config + db + telegram + crm reachability checks",
    "userbot-login": "create the Telethon StringSession for the reader account",
    "test": "ruff + mypy (scoped) + pytest (needs postgres)",
    "backup": "",
    "restore": "",
    "import-clients": "suppression fallback before the ERP feed exists",
}
PROMISES = {
    "up": ["docker compose up -d", "db init", "worker userbot bot"],
    "migrate": ["alembic upgrade head"],
    "doctor": ["clientscrapper.tools.doctor"],
    "userbot-login": ["clientscrapper.tools.userbot_login"],
    "test": ["ruff check . && ruff format --check . && mypy && pytest"],
    "backup": ["clientscrapper.worker.main backup"],
    "restore": ["clientscrapper.tools.restore"],
    "import-clients": ["clientscrapper.tools.import_clients"],
}


def test_every_comment_is_the_one_its_promises_were_checked_against():
    assert {t: c for t, _, c in table()} == COMMENTS


@pytest.mark.parametrize("target", sorted(t for t, c in COMMENTS.items() if c))
def test_a_command_a_comment_names_is_what_the_recipe_runs(target):
    """Where the comment is itself a command (``alembic upgrade head``,
    ``docker compose up -d``), the recipe runs it word for word; the tool
    names the others stand for are each in the recipe."""
    out = dry_run(
        target, **{k: "x" for k in dict(next(v for t, v, _ in table() if t == target))}
    )
    words = {
        "up": ["docker compose up -d", "db", "init", "worker", "userbot", "bot"],
        "migrate": ["alembic upgrade head"],
        "doctor": ["doctor"],
        "userbot-login": ["userbot_login"],
        "test": ["ruff", "mypy", "pytest"],
        "import-clients": ["import_clients"],
    }[target]
    for word in words:
        assert word in out, (target, word)


@pytest.mark.parametrize("target", sorted(PROMISES))
def test_every_recipe_does_what_its_line_says(target):
    variables = next(v for t, v, _ in table() if t == target)
    given = {
        k: ("/data/backups/x.dump.age" if v == "…" else v) for k, v in variables.items()
    }
    out = dry_run(target, **given)
    for piece in PROMISES[target]:
        assert piece in out, (target, piece)


def test_the_promises_cover_the_table():
    assert sorted(PROMISES) == sorted(COMMENTS) == sorted(t for t, _, _ in table())


def test_the_reader_sees_each_shape():
    """The parser on the block's own shapes: a comment, two commands on one
    line, a variable."""
    sample = "## Commands\n```\nmake a-b   # x\nmake c / make d FILE=e.f\n```\n"
    lines = commands_block(sample)
    assert lines == ["make a-b   # x", "make c / make d FILE=e.f"]
    found = [
        (m["target"], dict(_VAR.findall(m["vars"]))) for m in _COMMAND.finditer(lines[1])
    ]
    assert found == [("c", {}), ("d", {"FILE": "e.f"})]


def test_every_make_target_the_ops_runbook_names_exists():
    """docs/OPS.md (T-36) tells an operator what to type: every ``make x``
    in it is a target, and its command table lists the CLAUDE.md seven."""
    text = (ROOT / "docs" / "OPS.md").read_text(encoding="utf-8")
    named = set(re.findall(r"`make ([a-z][a-z0-9-]*)", text))
    assert named - targets() == set()
    assert {t for t, _, _ in table()} <= named
    assert "mvp-report" in named
