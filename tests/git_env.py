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
from collections.abc import Mapping


def isolated_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """``os.environ`` without any ``GIT_*`` variable, plus ``extra``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {**env, **(extra or {})}
