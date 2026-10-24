"""Record one page as a test fixture (07 §1.1, 03 §1.5).

    python -m clientscrapper.tools.record_fixture <url> --key <source_key> --name <name>
           [--json] [--header k=v]... [--post-json file] [--scrub auto|on|off]
           [--note "..."] [--root tests/fixtures] [--paid <operation> --est <usd>]

The request goes through ``Fetcher`` — our User-Agent, robots.txt and the
per-host rate apply; at a documented JSON API (``http.ROBOTS_OPTOUT_HOSTS``:
hh, Graph, Brave, Places …) robots.txt is skipped as the collector that calls
it skips it, so the fixture is the answer the collector gets. A metered
(paid) host is refused unless ``--paid <operation> --est <usd>`` names the
call: then it goes through ``costs.ask`` like any other paid call (a
``usage_log`` row with ``source_key=<key>``, refused at a cap before anything
is sent), the estimate being both the claim and the price. Writes
``<root>/<key>/<name>.<html|json|xml>``,
``<name>.headers.json`` (status, final URL and the response headers, with
``Set-Cookie``, ``Authorization`` and every key header stripped and every URL
through ``scrub_url``), and one line per file in ``<root>/<key>/README.md``.
A scrubbed page's URL, final URL and header values go through the same
``Scrubber`` as its body (a ``t.me/<person>`` path becomes the body's
``namuna_NN``), and its ``content-length`` — the unscrubbed size — is dropped:

    file · date · url · sha256 · status · bytes · scrubbed: yes|no · note

The sha256 is of the stored file. A re-recording replaces its own line.
``--scrub auto`` scrubs person-tier sources and pages that name an individual
entrepreneur (``scrub_fixture.needs_scrub``); company-tier pages are verbatim.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

from clientscrapper.collectors.base import Budget
from clientscrapper.config import settings
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import costs
from clientscrapper.services.http import (
    ROBOTS_OPTOUT_HOSTS,
    CostGate,
    Fetcher,
    FetchError,
    FetchResult,
    PaidCall,
    host_of,
)
from clientscrapper.services.urlsafe import scrub_url
from clientscrapper.services.usage import CALLABLE, Operation, Priced
from clientscrapper.tools.scrub_fixture import (
    FixtureKind,
    Scrubber,
    needs_scrub,
    scrub_bytes,
)

ScrubMode = Literal["auto", "on", "off"]

DEFAULT_ROOT: Final = Path("tests/fixtures")
# Never stored: cookies, credentials and the providers' key headers.
STRIPPED_HEADERS: Final = frozenset(
    {
        "set-cookie",
        "cookie",
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "x-subscription-token",
        "x-goog-api-key",
        "x-gsr-key",
        "x-erp-key",
    }
)
SEPARATOR: Final = " · "
README_HEAD: Final = (
    "# Fixtures: {key}\n\n"
    "Recorded with `python -m clientscrapper.tools.record_fixture` (07 §1.1).\n"
    "One line per file: file · date · url · sha256 · status · bytes · scrubbed · note\n\n"
)
_KEY = re.compile(r"[a-z][a-z0-9_]{2,40}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_.\-]{0,80}")


@dataclass(frozen=True, slots=True)
class Recorded:
    path: Path
    headers_path: Path
    readme_line: str
    sha256: str
    scrubbed: bool


def fixture_kind(result: FetchResult, *, json_mode: bool) -> FixtureKind:
    if json_mode or "json" in result.content_type:
        return "json"
    if "xml" in result.content_type:
        return "xml"
    return "html"


# Describes the bytes as they came, not the scrubbed file.
SCRUBBED_DROPS: Final = frozenset({"content-length"})


def stored_headers(
    headers: Mapping[str, str], *, scrubber: Scrubber | None = None
) -> dict[str, str]:
    """The headers kept beside a fixture; with ``scrubber`` (a scrubbed page)
    every value through it, with the body's mapping."""
    kept: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in STRIPPED_HEADERS:
            continue
        if scrubber is not None:
            if lowered in SCRUBBED_DROPS:
                continue
            value = scrubber.meta(value)
        kept[lowered] = scrub_url(value) if lowered == "location" else value
    return kept


def readme_line(
    *,
    file: str,
    day: str,
    url: str,
    sha256: str,
    status: int,
    size: int,
    scrubbed: bool,
    note: str,
) -> str:
    fields = [
        file,
        day,
        scrub_url(url),
        sha256,
        str(status),
        str(size),
        f"scrubbed: {'yes' if scrubbed else 'no'}",
        " ".join(note.split()) or "—",
    ]
    return SEPARATOR.join(fields)


def update_readme(readme: Path, key: str, file: str, line: str) -> None:
    """Append ``line``, or replace the line this file already has."""
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
    else:
        text = README_HEAD.format(key=key)
    prefix = f"- {file}{SEPARATOR}"
    lines = [
        existing for existing in text.splitlines() if not existing.startswith(prefix)
    ]
    while lines and not lines[-1].strip():
        lines.pop()
    if not any(existing.startswith("- ") for existing in lines):
        lines.append("")  # a blank line between the heading and the first entry
    lines.append(f"- {line}")
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")


def paid_call(operation: Operation, est_usd: Decimal) -> PaidCall:
    """``--paid``: the recorder cannot read a price off an arbitrary answer,
    so the estimate is the price too (never below what was claimed)."""
    if operation not in CALLABLE:
        raise ValueError(f"not a callable operation: {operation!r}")
    if not est_usd.is_finite() or est_usd < 0:
        raise ValueError("--est must be a non-negative number of dollars")
    return PaidCall(operation, est_usd, lambda _: Priced(cost_usd=est_usd))


def ask_gate(session: Any) -> CostGate:
    """``costs.ask`` with the recorder's session bound; the Fetcher passes the
    operation, the estimate, the call, the price, the key and the minute."""

    async def gate(
        *,
        operation: Operation,
        est_usd: Decimal,
        call: Any,
        price: Any,
        source_key: str,
        now: datetime,
    ) -> FetchResult:
        result: FetchResult = await costs.ask(
            session,
            operation=operation,
            est_usd=est_usd,
            call=call,
            price=price,
            now=now,
            source_key=source_key,
        )
        return result

    return gate


async def record(
    url: str,
    *,
    key: str,
    name: str,
    fetcher: Fetcher,
    now: datetime,
    root: Path = DEFAULT_ROOT,
    json_mode: bool = False,
    headers: Mapping[str, str] | None = None,
    post_json: Any = None,
    scrub: ScrubMode = "auto",
    note: str = "",
    paid: PaidCall | None = None,
) -> Recorded:
    """``paid``: a metered host's call (``paid_call``), with ``fetcher`` bound
    to a cost gate (``ask_gate``); without it a metered host is refused."""
    if not _KEY.fullmatch(key):
        raise ValueError(f"not a sources.key: {key!r}")
    if not _NAME.fullmatch(name):
        raise ValueError(f"fixture names are [a-z0-9_.-]: {name!r}")
    usd = paid.est_usd if paid is not None else Decimal("0")
    budget = Budget(pages=1, usd=usd, deadline=now + timedelta(minutes=10))
    request_headers = dict(headers or {})
    if json_mode:
        request_headers.setdefault("Accept", "application/json")
    if post_json is not None:
        result = await fetcher.post_json(
            url,
            source=key,
            budget=budget,
            json=post_json,
            headers=request_headers,
            paid=paid,
        )
    else:
        result = await fetcher.get(
            url,
            source=key,
            budget=budget,
            headers=request_headers,
            cache=False,
            # Fetcher refuses an opt-out anywhere but these documented APIs.
            obey_robots=host_of(url) not in ROBOTS_OPTOUT_HOSTS,
            paid=paid,
        )
    kind = fixture_kind(result, json_mode=json_mode)
    body = result.body
    scrubbed = scrub == "on" or (scrub == "auto" and needs_scrub(key, body))
    scrubber = Scrubber() if scrubbed else None
    shown_url, final_url = url, result.final_url
    if scrubber is not None:
        body = scrub_bytes(body, kind=kind, scrubber=scrubber).body
        shown_url, final_url = scrubber.meta(url), scrubber.meta(final_url)

    folder = root / key
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.{kind}"
    path.write_bytes(body)
    headers_path = folder / f"{name}.headers.json"
    facts = {
        "url": scrub_url(shown_url),
        "final_url": scrub_url(final_url),
        "status": result.status,
        "fetched_at": result.fetched_at.isoformat(),
        "headers": stored_headers(result.headers, scrubber=scrubber),
    }
    headers_path.write_text(
        json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sha = hashlib.sha256(body).hexdigest()
    line = readme_line(
        file=path.name,
        day=now.astimezone(settings.tz).date().isoformat(),
        url=shown_url,
        sha256=sha,
        status=result.status,
        size=len(body),
        scrubbed=scrubbed,
        note=note,
    )
    update_readme(folder / "README.md", key, path.name, line)
    return Recorded(path, headers_path, line, sha, scrubbed)


def _header(text: str) -> tuple[str, str]:
    name, sep, value = text.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError("--header takes k=v")
    return name.strip(), value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record one page as a test fixture.")
    parser.add_argument("url")
    parser.add_argument("--key", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--json", action="store_true", dest="json_mode")
    parser.add_argument("--header", action="append", type=_header, default=[])
    parser.add_argument("--post-json", type=Path)
    parser.add_argument("--scrub", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--note", default="")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--paid", metavar="OPERATION", help="a metered host's operation")
    parser.add_argument("--est", type=Decimal, metavar="USD", help="its estimate")
    args = parser.parse_args(argv)
    if (args.paid is None) != (args.est is None):
        parser.error("--paid and --est go together")
    return args


async def _main(args: argparse.Namespace) -> int:
    post = (
        json.loads(args.post_json.read_text(encoding="utf-8")) if args.post_json else None
    )
    robots_dir = Path(settings.evidence_dir).parent / "http-cache" / "robots"
    async with Fetcher(robots_dir=robots_dir) as fetcher, SessionLocal() as session:
        try:
            paid = paid_call(args.paid, args.est) if args.paid else None
            bound = fetcher.bind(cost_gate=ask_gate(session)) if paid else fetcher
            done = await record(
                args.url,
                key=args.key,
                name=args.name,
                fetcher=bound,
                now=datetime.now(settings.tz),
                root=args.root,
                json_mode=args.json_mode,
                headers=dict(args.header),
                post_json=post,
                scrub=args.scrub,
                note=args.note,
                paid=paid,
            )
        except (FetchError, ValueError) as exc:
            print(f"not recorded: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        finally:
            # costs.ask commits its own row; what it left for this session's
            # transaction (an attribution it could not take) lands here.
            await session.commit()
    print(done.path)
    print(done.readme_line)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
