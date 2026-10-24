"""Every dependency is pinned (06 §5.6): ``name==x.y.z``, one per line.

A range or a bare name lets a rebuild pull a release nobody tested; the image
is rebuilt on every ``make update``. The heavy stacks MIYA carried and FastAPI
(optional, not built in v1) stay out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = ("requirements.txt", "requirements-dev.txt")
PIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[a-z0-9,_-]+\])?==\d+(?:\.\d+){1,3}")
FORBIDDEN = {"sentence-transformers", "torch", "fastapi"}


def _entries(name: str) -> list[str]:
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return [
        line.split("#", 1)[0].strip()
        for line in lines
        if line.split("#", 1)[0].strip() and not line.startswith("-r ")
    ]


@pytest.mark.parametrize("name", FILES)
def test_every_line_is_an_exact_pin(name):
    assert [e for e in _entries(name) if not PIN.fullmatch(e)] == []


def test_dev_requirements_include_the_runtime_ones():
    assert "-r requirements.txt" in (ROOT / "requirements-dev.txt").read_text()


def test_no_heavy_or_out_of_scope_package():
    names = {
        re.split(r"[\[=]", e, maxsplit=1)[0].lower() for f in FILES for e in _entries(f)
    }
    assert names & FORBIDDEN == set()


def test_the_pin_rule_refuses_each_loose_shape():
    for loose in ("httpx", "httpx>=0.28", "httpx~=0.28.1", "httpx==0.28.*", "httpx==1"):
        assert not PIN.fullmatch(loose), loose
    assert PIN.fullmatch("psycopg[binary,pool]==3.2.3")
