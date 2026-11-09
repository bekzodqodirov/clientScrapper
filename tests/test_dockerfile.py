"""The image (07 §1.2, 06 §3.2); the compose file is tests/test_compose.py.

String checks on purpose (fullprompt's dockerfile-offline test pattern): no
build is needed to see that the image grew a heavy dependency, runs as root,
or lost the backup tools.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")

# MIYA's image carried these; nothing in this project uses them.
FORBIDDEN = (
    "torch",
    "ffmpeg",
    "sentence-transformers",
    "huggingface",
    "download.pytorch.org",
    "cuda",
    "nvidia",
)


def _commands() -> list[str]:
    """Dockerfile lines with their backslash continuations folded in."""
    return DOCKERFILE.replace("\\\n", " ").splitlines()


def test_no_heavy_or_gpu_dependency():
    lowered = DOCKERFILE.lower()
    assert [word for word in FORBIDDEN if word in lowered] == []


def test_base_image_is_python_3_12_slim():
    assert re.search(r"^FROM python:3\.12-slim\b", DOCKERFILE, re.MULTILINE)


def test_backup_tools_are_installed():
    installs = [line for line in _commands() if "apt-get install" in line]
    words = set(" ".join(installs).split())
    assert {"age", "postgresql-client", "postgresql-client-16"} <= words


def test_runs_as_uid_10001_after_the_last_copy():
    lines = DOCKERFILE.splitlines()
    copies = [i for i, line in enumerate(lines) if line.startswith("COPY ")]
    users = [i for i, line in enumerate(lines) if line.startswith("USER ")]
    assert users and lines[users[-1]] == "USER clientscrapper"
    assert users[-1] > copies[-1]
    assert re.search(r"useradd [^\n]*--uid 10001 clientscrapper", DOCKERFILE)


def test_default_command_is_the_worker():
    assert 'CMD ["python", "-m", "clientscrapper.worker.main"]' in DOCKERFILE


def test_the_16_client_comes_first_on_path():
    """pg_dump/pg_restore resolve to the server's major (16), never the
    distribution's 17 through pg_wrapper: pg_restore 17 sends
    «SET transaction_timeout», which a 16 server refuses, so every weekly
    restore drill failed (the first docker dry run, 2026-11-09). The PATH is
    set after the PGDG install and before anything runs the tools."""
    lines = DOCKERFILE.splitlines()
    [path] = [i for i, line in enumerate(lines) if line.startswith("ENV PATH=")]
    assert lines[path] == "ENV PATH=/usr/lib/postgresql/16/bin:$PATH"
    pgdg = next(i for i, line in enumerate(lines) if "postgresql-client-16" in line)
    assert pgdg < path
    # In the app stage, which the smoke stage and the final image build on.
    stages = [i for i, line in enumerate(lines) if line.startswith("FROM ")]
    assert stages[0] < path < stages[1]
