#!/usr/bin/env python3
"""Refuse a credential or a real phone number in the repository (07 §2.2).

    python scripts/secret_scan.py            # the checkout: tracked + new files
    python scripts/secret_scan.py FILE...    # just these

Exits 1 naming ``file:line`` and the rule for every hit, 0 when clean. The
matched value itself is never printed — the CI log is not a place for it
either. No third-party dependency: CI's lint job and the pre-push hook run it
before anything is installed.

Phones: a ``+998`` number fails outside ``tests/fixtures/`` (recorded public
pages, already scrubbed for person-tier sources) unless it is visibly
synthetic (``synthetic()``: ``+998 90 000 00 NN`` and the same shape under
other codes) or one of the two example numbers the specification itself
prints (``SPEC_EXAMPLE_TAILS``). An ``age1…`` recipient
is allowed only on a comment line of ``.env.example``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 20 * 1024 * 1024
SKIPPED_PARTS = {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache"}
FIXTURES = "tests/fixtures/"
ENV_EXAMPLE = ".env.example"

SECRETS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    # A StringSession is one long base64 run starting with "1"; inside a
    # longer base64 blob (a data: URI in a fixture) a "1" after "+" or "/" is
    # no start of a token, hence the look-behind instead of 07's \b ("=" is
    # padding, only ever at a blob's end, so "KEY=1…" still starts one).
    ("telethon session", re.compile(r"(?<![A-Za-z0-9+/_-])1[A-Za-z0-9+/=_-]{300,}")),
    ("google api key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("apify token", re.compile(r"apify_api_[A-Za-z0-9]{20,}")),
    ("brave key", re.compile(r"BSA[A-Za-z0-9_-]{20,}")),
    ("age secret key", re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{58}")),
)
AGE_RECIPIENT = re.compile(r"age1[a-z0-9]{58}")
# 07's pattern, with a dash allowed beside the spaces ("+998 90 123-45-67").
PHONE = re.compile(r"\+998[\s-]?(\d{2})[\s-]?(\d{3})[\s-]?(\d{2})[\s-]?(\d{2})")
# Visibly synthetic numbers (07 §1.0): +998 90 000 00 NN, and the same shape
# under the other operator and area codes the normaliser must be tested on
# (XX 000 00 NN, and 04 §1.1's landline shape XX 200 00 NN), and placeholders
# whose seven digits are one digit repeated (90 111 11 11).
SYNTHETIC_MIDDLES = frozenset({"00000", "20000"})
# Example numbers the specification itself prints: 04 §1.1's normaliser table
# (+998 90 123 45 67, mirrored by tests/test_normalise.py and test_keys.py)
# and the State Statistics Committee register office's public line, quoted as
# evidence in docs/manbalar.json. Nothing else is exempt.
SPEC_EXAMPLE_TAILS = frozenset({"901234567", "712028093"})


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


def synthetic(tail: str) -> bool:
    """A national 9-digit number that is visibly made up."""
    return tail[2:7] in SYNTHETIC_MIDDLES or len(set(tail[2:])) == 1


def _phone_allowed(path: str, tail: str) -> bool:
    return path.startswith(FIXTURES) or synthetic(tail) or tail in SPEC_EXAMPLE_TAILS


def scan_text(path: str, text: str) -> list[Finding]:
    """Every hit in one file; ``path`` is relative to the repository root."""
    found: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        found += [Finding(path, number, rule) for rule, rx in SECRETS if rx.search(line)]
        comment = line.lstrip().startswith("#")
        if AGE_RECIPIENT.search(line) and not (path == ENV_EXAMPLE and comment):
            found.append(Finding(path, number, "age recipient outside .env.example"))
        for m in PHONE.finditer(line):
            if not _phone_allowed(path, "".join(m.groups())):
                found.append(
                    Finding(path, number, "+998 phone outside the synthetic range")
                )
    return found


def _listed(root: Path) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return sorted({n for n in result.stdout.decode().split("\0") if n})


def candidates(root: Path, paths: Sequence[str] = ()) -> Iterator[Path]:
    """The files to read: the given ones, else git's tracked and new
    (not ignored) files, else every file under ``root``."""
    if paths:
        yield from (Path(p) for p in paths)
        return
    listed = _listed(root)
    if listed is None:
        for p in sorted(root.rglob("*")):
            if p.is_file() and not SKIPPED_PARTS & set(p.relative_to(root).parts):
                yield p
        return
    for name in listed:
        yield root / name


def scan(root: Path, paths: Sequence[str] = ()) -> list[Finding]:
    found: list[Finding] = []
    for path in candidates(root, paths):
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            continue
        content = path.read_bytes()
        if b"\0" in content[:8192]:
            continue  # binary
        try:
            shown = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            shown = str(path)
        found += scan_text(shown, content.decode("utf-8", errors="replace"))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = ROOT
    if args[:1] == ["--root"]:
        root, args = Path(args[1]).resolve(), args[2:]
    findings = scan(root, args)
    for finding in findings:
        print(finding)
    if findings:
        print(
            f"secret_scan: {len(findings)} finding(s) — a credential lives only in the "
            "server .env; test data uses +998 90 000 00 NN",
            file=sys.stderr,
        )
        return 1
    print("secret_scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
