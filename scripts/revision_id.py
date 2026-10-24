"""The id of the next migration, ``000N_<slug>`` (02 §5).

    python scripts/revision_id.py "add outcomes no_contact"
    0002_add_outcomes_no_contact

``make revision m=…`` passes it to ``alembic revision --rev-id``; with
``file_template = %%(rev)s`` in alembic.ini the file is ``<id>.py``, the shape
of ``0001_initial.py``. Alembic alone minted a random hex id and file name.
N is one past the highest number already in ``alembic/versions`` (a gap stays
a gap: renumbering a migration that ran somewhere would orphan it).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
_NUMBERED = re.compile(r"(\d{4})_[a-z0-9_]+\.py")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")
SLUG_MAX = 40


def slug(message: str) -> str:
    """Lower-case ASCII words joined by underscores, at most SLUG_MAX long."""
    words = _NOT_SLUG.sub("_", message.lower()).strip("_")
    cut = words[:SLUG_MAX].rstrip("_")
    if not cut:
        raise ValueError(f"no slug in {message!r}: name the change in ASCII words")
    return cut


def next_number(versions: Path = VERSIONS) -> int:
    numbers = [
        int(m.group(1))
        for path in versions.glob("*.py")
        if (m := _NUMBERED.fullmatch(path.name))
    ]
    return max(numbers, default=0) + 1


def revision_id(message: str, versions: Path = VERSIONS) -> str:
    return f"{next_number(versions):04d}_{slug(message)}"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print('usage: python scripts/revision_id.py "message"', file=sys.stderr)
        return 2
    try:
        print(revision_id(argv[0]))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
