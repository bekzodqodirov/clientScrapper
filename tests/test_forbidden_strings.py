# VENDORED_FROM dailyJournal@6b1f67f tests/test_owner_aliases.py
"""No real name, handle or phone tail enters the repository (07 §1.0).

MIYA's username guard, generalised: the strings come only from the
environment — ``CS_FORBIDDEN_STRINGS``, a CI secret holding the real names,
handles and phone tails the owner lists (empty until he sets it, 07 open
question 5) — because writing them here would be the leak. Entries are
separated by commas or new lines; ``@`` is dropped from a handle; matching
ignores case and folds every apostrophe variant to one (``Goʻzal`` and
``Go'zal`` are the same name); an entry of digits is a phone tail and matches
with any spaces, dashes or brackets between its digits.

The report names the file and the entry's position in the list, never the
entry: the failing CI log must not print what it protects.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from clientscrapper.services.text import fold_apostrophes
from tests.ast_tools import ROOT
from tests.git_env import isolated_env, tree_files

ENV = "CS_FORBIDDEN_STRINGS"
MIN_LENGTH = 4  # shorter entries would match half the tree
PHONE_TAIL = re.compile(r"\+?[\d\s()-]{7,}")


@dataclass(frozen=True, slots=True)
class Forbidden:
    index: int
    pattern: re.Pattern[str]


def _normalise(text: str) -> str:
    return fold_apostrophes(text).casefold()


def forbidden(raw: str) -> list[Forbidden]:
    found = []
    entries = [e.strip() for e in re.split(r"[,\n]", raw)]
    for index, entry in enumerate(e for e in entries if e):
        entry = entry.lstrip("@")
        if PHONE_TAIL.fullmatch(entry):
            digits = re.sub(r"\D", "", entry)
            if len(digits) < 7:
                continue
            body = r"[\s()-]*".join(digits)
            found.append(Forbidden(index, re.compile(rf"(?<!\d){body}(?!\d)")))
        elif len(entry) >= MIN_LENGTH:
            found.append(Forbidden(index, re.compile(re.escape(_normalise(entry)))))
    return found


def offenders(root: Path, entries: list[Forbidden]) -> list[str]:
    found = []
    for path in tree_files(root):
        content = path.read_bytes()
        if b"\0" in content[:8192]:
            continue  # binary
        text = _normalise(content.decode("utf-8", errors="ignore"))
        found += [
            f"{path.relative_to(root)}: entry #{e.index + 1}"
            for e in entries
            if e.pattern.search(text)
        ]
    return found


def test_no_file_carries_a_forbidden_string():
    entries = forbidden(os.environ.get(ENV, ""))
    if not entries:
        pytest.skip(f"{ENV} is empty (a CI secret the owner sets, 07 open question 5)")
    assert offenders(ROOT, entries) == []


def test_the_guard_finds_each_shape(tmp_path):
    raw = "@Namuna_Real, Goʻzal Namunova\n90 111 22 33,ab, 12"
    entries = forbidden(raw)
    # "ab" and "12" are too short to mean anything.
    assert [e.index for e in entries] == [0, 1, 2]
    (tmp_path / "handle.md").write_text("yozdi @NAMUNA_REAL kecha", encoding="utf-8")
    (tmp_path / "name.py").write_text('OWNER = "Go\'zal namunova"\n', encoding="utf-8")
    (tmp_path / "phone.json").write_text('{"p": "+998 (90) 111-22-33"}', encoding="utf-8")
    (tmp_path / "longer.txt").write_text(
        "id 9011122334 is another number", encoding="utf-8"
    )
    (tmp_path / "clean.txt").write_text(
        "Namuna Tekstil, +998 90 000 00 01", encoding="utf-8"
    )
    (tmp_path / "blob.bin").write_bytes(b"\0namuna_real")
    assert sorted(offenders(tmp_path, entries)) == [
        "handle.md: entry #1",
        "name.py: entry #2",
        "phone.json: entry #3",
    ]
    # The report never carries the value itself.
    assert all("namuna" not in line.lower() for line in offenders(tmp_path, entries))


def test_an_inherited_git_dir_does_not_change_the_files_read(tmp_path, monkeypatch):
    """A pre-push hook in a linked worktree exports GIT_DIR: the walk still
    reads the root's own index (a file tracked there despite .gitignore is
    only in that index)."""

    def git(repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, env=isolated_env(), check=True, capture_output=True
        )

    victim = tmp_path / "victim"
    victim.mkdir()
    git(victim, "init", "-q")
    root = tmp_path / "root"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("*.log\n")
    (root / "kept.log").write_text("Namuna Realname\n", encoding="utf-8")
    git(root, "add", "-f", "kept.log")
    monkeypatch.setenv("GIT_DIR", str(victim / ".git"))
    assert offenders(root, forbidden("Namuna Realname")) == ["kept.log: entry #1"]
