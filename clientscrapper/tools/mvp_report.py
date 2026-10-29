"""The MVP report (07 §3.2): the pilot's four numbers against their targets.

    python -m clientscrapper.tools.mvp_report [--start YYYY-MM-DD] [--now ISO]

``--start`` is the pilot's first day (default: the Tashkent day of the first
delivered push); week 2 is days 8–14. ``--now`` reads the database as of
that moment (default: now). The numbers are ``services/metrics``' — the
same ``/holat`` shows — and the text ``services/cards.render_mvp``'s:
pushes with a phone and score ≥ 60 in week 2, the real importer rate (with
the strict precision beside it), contacted within one business day (what
the ERP can see) and the Claude spend per day; then quotes sent (approximate,
01 §7 decision 5a), won as the first-shipment proxy (3a) and the lost-reason
labels no class matched (1a). Read-only.

Exit codes: 0 printed, 1 nothing to report yet (no push and no ``--start``),
2 a bad argument.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import date, datetime
from typing import Final

from clientscrapper.config import settings
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import cards, metrics

EXIT_OK: Final = 0
EXIT_EMPTY: Final = 1
NOTHING_YET: Final = (
    "📊 MVP hisoboti: hali birorta push yoʻq — --start bilan kunni bering"
)


def _moment(text: str) -> datetime:
    at = datetime.fromisoformat(text)
    return at if at.tzinfo is not None else at.replace(tzinfo=settings.tz)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mvp_report", description="07 §3.2 MVP report")
    parser.add_argument(
        "--start", type=date.fromisoformat, help="the pilot's first day, YYYY-MM-DD"
    )
    parser.add_argument(
        "--now", type=_moment, help="read as of this moment (ISO, Tashkent if naive)"
    )
    return parser.parse_args(argv)


async def report_text(*, now: datetime, start: date | None = None) -> str | None:
    """The report as printed, None when there is nothing to report."""
    async with SessionLocal() as session:
        report = await metrics.mvp_report(session, now=now, start=start)
    return None if report is None else cards.render_mvp(report)


async def run(argv: Sequence[str] | None = None) -> int:
    """The tool, in the caller's event loop (``main`` gives it one)."""
    args = parse_args(argv)
    now = args.now or datetime.now(settings.tz)
    text = await report_text(now=now, start=args.start)
    if text is None:
        print(NOTHING_YET)
        return EXIT_EMPTY
    print(text)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
