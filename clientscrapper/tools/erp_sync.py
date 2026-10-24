"""Run one ERP feed walk by hand (06 §4.6, 07 §5.6).

    make erp-sync WALK=full|delta|outcomes|quarter|year [ACCEPT_SHRINK=1]
    python -m clientscrapper.tools.erp_sync --full [--accept-shrink]
    python -m clientscrapper.tools.erp_sync --delta | --outcomes | --quarter | --year

The same run the scheduler makes (``collectors/erp_sync.run``), one flag per
cadence of ``collectors/erp_sync.JOBS`` (``--outcomes`` is the nightly cursor
+ recent walks): the ``sources.erp_sync`` row must be enabled and the feeds
configured (``CRM_PUSH_SOURCE=agent`` with ``CRM_AGENT_KEY``), or the run is
skipped and says why. ``--accept-shrink`` lets ONE full pull delete although
it saw fewer than 90 % of the previous pull's entries — after the owner
checked why the ERP's list shrank (``suppression_shrunk``, 06 §4.6).

Exit codes: 0 the walk ran (``ok`` or ``partial``), 1 it failed, 3 it was
skipped (disabled, feeds not configured, already running) — a script can
tell a skip from a success.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Final

from clientscrapper.collectors import erp_sync
from clientscrapper.collectors.runner import RunReport
from clientscrapper.services.http import Fetcher

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_SKIPPED: Final = 3

HELP: Final[dict[erp_sync.Variant, str]] = {
    "full": "a suppression full pull (the only delete)",
    "delta": "a suppression delta since the last read",
    "outcomes": "the nightly outcomes walks (cursor, then recent)",
    "quarter": "the Sunday outcomes walk (90 days)",
    "year": "the monthly outcomes walk (the whole year)",
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="erp_sync", description="ERP feed walk")
    which = parser.add_mutually_exclusive_group(required=True)
    for variant in erp_sync.VARIANTS:
        which.add_argument(
            f"--{variant}",
            action="store_const",
            const=variant,
            dest="variant",
            help=HELP[variant],
        )
    parser.add_argument(
        "--accept-shrink",
        action="store_true",
        help="let this full pull delete although the list shrank > 10 %%",
    )
    args = parser.parse_args(argv)
    if args.accept_shrink and args.variant != "full":
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


def exit_code(report: RunReport) -> int:
    if report.status in ("ok", "partial"):
        return EXIT_OK
    if report.status == "skipped":
        return EXIT_SKIPPED
    return EXIT_FAILED


async def _main(args: argparse.Namespace) -> int:
    variant: erp_sync.Variant = args.variant
    async with Fetcher() as fetcher:
        report = await erp_sync.run(
            variant, fetcher=fetcher, accept_shrink=args.accept_shrink
        )
    print(describe(report))
    return exit_code(report)


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
