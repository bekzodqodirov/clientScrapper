# VENDORED_FROM dailyJournal@6b1f67f miya/tools/import_clients.py
"""Load the ERP's client export as suppression (05 §B.3, 06 §3.3).

    make import-clients FILE=/data/imports/clients.xlsx [APPLY=1] [ACCEPT_FEWER=1]
    python -m clientscrapper.tools.import_clients FILE [--apply] [--accept-fewer]

The file is the ERP's **Mijozlar → XLSX**, downloaded from an unfiltered
screen with no saved view, copied into the server's ``data/imports/``.
Without ``--apply`` it prints what would happen and writes nothing; with it
the new tails are written in one transaction. ``--accept-fewer`` lets one
file with fewer rows than the previous import's through, after the owner
checked that the ERP's client book really shrank (``services/client_import``
refuses it otherwise: it reads like a filtered export). Exit 0 for a dry run
or a written import, 1 for a file it cannot read or an import it refused.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from clientscrapper.collectors.runner import SessionFactory
from clientscrapper.db.session import SessionLocal, engine
from clientscrapper.services import client_import

DRY_RUN_TEXT = "Sinov: hech narsa yozilmadi (yozish uchun APPLY=1, yaʼni --apply)."
ACCEPT_HINT_TEXT = (
    "Fayl toʻgʻri boʻlsa: ACCEPT_FEWER=1 APPLY=1 (--accept-fewer --apply) bilan qayta "
    "ishga tushiring."
)
REFUSED_TEXT = "Hech narsa yozilmadi."
CANNOT_READ_TEXT = "{name} oʻqilmadi: {why}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="import_clients", description="ERP client export → suppression"
    )
    parser.add_argument("file", help="the ERP export Mijozlar → XLSX (.xlsx or .csv)")
    parser.add_argument("--apply", action="store_true", help="write the new tails")
    parser.add_argument(
        "--accept-fewer",
        action="store_true",
        help="let a file with fewer rows than the previous import through",
    )
    return parser.parse_args(argv)


async def run(
    path: str | Path,
    *,
    apply: bool,
    accept_fewer: bool = False,
    now: datetime | None = None,
    sessions: SessionFactory = SessionLocal,
) -> tuple[int, list[str]]:
    """The exit code and the lines to print."""
    try:
        export = client_import.read_rows(path)
        async with sessions() as session:
            plan = await client_import.plan_import(session, export)
            lines = client_import.plan_lines(plan)
            refused = plan.refusal is not None and not accept_fewer
            if refused:
                lines.append(ACCEPT_HINT_TEXT)
            if not apply:
                lines.append(DRY_RUN_TEXT)
                return 0, lines
            if refused:
                lines.append(REFUSED_TEXT)
                return 1, lines
            applied = await client_import.apply_import(
                session, plan, now=now or datetime.now(UTC), accept_fewer=accept_fewer
            )
            await session.commit()
            lines.append(client_import.applied_line(applied))
            return 0, lines
    except client_import.BadFile as exc:
        return 1, [CANNOT_READ_TEXT.format(name=Path(path).name, why=exc.text())]


async def _main(args: argparse.Namespace) -> int:
    try:
        code, lines = await run(
            args.file, apply=args.apply, accept_fewer=args.accept_fewer
        )
    finally:
        await engine.dispose()
    print("\n".join(lines))
    return code


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
