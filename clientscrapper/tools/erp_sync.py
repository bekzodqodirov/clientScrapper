"""Run one ERP suppression pull by hand (06 §4.6).

    python -m clientscrapper.tools.erp_sync --full [--accept-shrink]
    python -m clientscrapper.tools.erp_sync --delta

The same run the scheduler makes (``collectors/erp_sync.run``): the
``sources.erp_sync`` row must be enabled and the feeds configured
(``CRM_PUSH_SOURCE=agent`` with ``CRM_AGENT_KEY``), or the run is skipped and
says why. ``--accept-shrink`` lets ONE full pull delete although it saw
fewer than 90 % of the previous pull's entries — after the owner checked why
the ERP's list shrank (``suppression_shrunk``, 06 §4.6).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from clientscrapper.collectors import erp_sync
from clientscrapper.collectors.runner import RunReport
from clientscrapper.services.http import Fetcher


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="erp_sync", description="ERP suppression pull")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--full", action="store_true", help="a full pull (the only delete)"
    )
    which.add_argument("--delta", action="store_true", help="a delta since the last read")
    parser.add_argument(
        "--accept-shrink",
        action="store_true",
        help="let this full pull delete although the list shrank > 10 %%",
    )
    args = parser.parse_args(argv)
    if args.accept_shrink and not args.full:
        parser.error("--accept-shrink goes with --full")
    return args


def describe(report: RunReport) -> str:
    """One line for the terminal: status, then why or what."""
    line = f"erp_sync.{report.meta.get('variant', '?')}: {report.status}"
    if report.status == "skipped":
        return f"{line} ({report.meta.get('note')})"
    if report.error:
        line += f" — {report.error}"
    feed = report.meta.get("suppression_full") or report.meta.get("suppression")
    if isinstance(feed, dict):
        keys = ("walk", "status", "entries", "seen", "deleted")
        line += " · " + " ".join(f"{k}={feed[k]}" for k in keys if k in feed)
    return line


async def _main(args: argparse.Namespace) -> int:
    variant: erp_sync.Variant = "full" if args.full else "delta"
    async with Fetcher() as fetcher:
        report = await erp_sync.run(
            variant, fetcher=fetcher, accept_shrink=args.accept_shrink
        )
    print(describe(report))
    return 0 if report.status in ("ok", "partial") else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
