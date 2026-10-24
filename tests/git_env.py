"""Git started by a test acts on the directory it is given, never on the
checkout that runs the suite.

A pre-push hook in a linked worktree (``.wt/*``) runs with ``GIT_DIR``
exported (git 2.43; the main checkout's hook gets none), and every process
the suite starts inherits it: a ``git init``/``git commit`` meant for a
scratch directory then commits into the repository being pushed and rewrites
its ``user.email``. A test's git therefore runs with no ``GIT_*`` variable at
all — a scratch repository inherits nothing from the one around it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

SKIPPED_DIRS = frozenset(
    {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache"}
)


def isolated_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """``os.environ`` without any ``GIT_*`` variable, plus ``extra``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {**env, **(extra or {})}


def tree_files(root: Path) -> list[Path]:
    """The files of the checkout at ``root``: git's tracked ones plus the
    untracked ones it does not ignore, or — outside git — every file under
    ``root`` but the tool directories.

    Asking git is what keeps a walk inside its own checkout: the task
    worktrees live under ``.wt/`` of the main one (``.git/info/exclude``), and
    a bare ``rglob`` from the main checkout would read every one of them."""
    if shutil.which("git"):
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            env=isolated_env(),
            capture_output=True,
            check=False,
        )
        if listed.returncode == 0:
            names = [n for n in listed.stdout.decode().split("\0") if n]
            return sorted({root / n for n in names if (root / n).is_file()})
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and not SKIPPED_DIRS & set(p.relative_to(root).parts)
    )
