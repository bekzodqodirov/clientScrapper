"""The strict mypy scope holds every module 07 §2.3 names, once it exists.

``mypy`` checks only ``[tool.mypy] files``; a module left out is analysed for
its imports and its own errors are never reported. 07 §2.3 lists the pure
modules that belong; each joins in the task that builds it, and this guard
makes forgetting one a red test rather than a silent hole. A listed path that
does not exist is refused too (a typo would narrow the scope by one file).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 07 §2.3, verbatim.
NAMED = (
    "clientscrapper/config.py",
    "clientscrapper/logs.py",
    *(
        f"clientscrapper/services/{name}.py"
        for name in (
            "keys normalise text safety retry http serp costs usage pseudonyms "
            "validators redlines signals scoring gates suppression push_gate "
            "crm_push erp_feeds outcomes candidates evidence"
        ).split()
    ),
    "clientscrapper/collectors/base.py",
    "clientscrapper/collectors/keywords.py",
    "clientscrapper/collectors/runner.py",
    "clientscrapper/bot/callbacks.py",
)


def scope() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    files: list[str] = config["tool"]["mypy"]["files"]
    return files


def covered(path: str, listed: list[str]) -> bool:
    return any(
        path == entry or path.startswith(entry.rstrip("/") + "/") for entry in listed
    )


def test_every_named_module_that_exists_is_in_the_strict_scope():
    listed = scope()
    missing = [p for p in NAMED if (ROOT / p).is_file() and not covered(p, listed)]
    assert missing == []


def test_every_listed_path_exists():
    assert [entry for entry in scope() if not (ROOT / entry).exists()] == []


def test_the_scope_is_strict():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mypy = config["tool"]["mypy"]
    assert (mypy["strict"], mypy["follow_imports"], mypy["warn_unreachable"]) == (
        True,
        "silent",
        True,
    )
