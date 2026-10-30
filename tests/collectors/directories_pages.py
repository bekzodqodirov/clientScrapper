"""The recorded directory pages (tests/fixtures/directories/, README.md):
each body with its recorder sidecar — the URL asked for, the final URL after
redirects, the status and ``fetched_at`` — and the robots.txt bodies as the
Fetcher read them (the cache header line dropped)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "directories"


@dataclass(frozen=True, slots=True)
class Page:
    name: str
    body: bytes
    url: str
    final_url: str
    status: int
    fetched_at: datetime

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


@cache
def page(name: str) -> Page:
    body = (FIXTURES / f"{name}.html").read_bytes()
    side = json.loads((FIXTURES / f"{name}.headers.json").read_text(encoding="utf-8"))
    return Page(
        name=name,
        body=body,
        url=side["url"],
        final_url=side["final_url"],
        status=side["status"],
        fetched_at=datetime.fromisoformat(side["fetched_at"]),
    )


def robots(host: str) -> str:
    """The robots.txt the recorder read for ``host`` (its body, no header)."""
    text = (FIXTURES / "robots" / f"{host}.txt").read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    assert first.startswith("# clientScrapper robots"), host
    return rest


GOLDENPAGES = (
    "goldenpages_rubric_1424_p1",
    "goldenpages_rubric_1424_p2",
    "goldenpages_rubric_1424_p99_negative",
    "goldenpages_rubric_106080_p1",
    "goldenpages_company_85641",
)
