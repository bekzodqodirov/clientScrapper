# VENDORED_FROM dailyJournal@6b1f67f miya/tools/import_clients.py
"""Load the ERP's client export as suppression (05 §B.3, 06 §3.3).

    make import-clients FILE=clients.xlsx [APPLY=1] [ACCEPT_FEWER=1]
    python -m clientscrapper.tools.import_clients FILE [--apply] [--accept-fewer]

The file is the ERP's **Mijozlar → XLSX**, downloaded from an unfiltered
screen with no saved view, copied into the server's ``data/imports/`` —
the container's ``/data/imports/``, where ``make`` looks for a bare file
name (a name with a «/» is passed as it stands). A file that is not there
is said to be missing, with the place looked at, never «broken». The file
holds every client's name, code, manager and phones: once its tails are
written it is deleted from ``data/imports/`` (a file elsewhere: the owner is
told to delete it), and a file the app cannot read is given to the app's
user (uid 10001), never opened to everybody.
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
# What to do about a file the container cannot reach: the server's
# data/imports/ is the only folder it sees (docker-compose.yml, ./data:/data).
HINT_TEXT: dict[client_import.BadFileCode, str] = {
    "missing": (
        "Qidirilgan joy: {path}. Faylni serverdagi data/imports/ papkasiga "
        "koʻchiring va make import-clients FILE={name} bilan qayta ishga tushiring."
    ),
    # The containers run as uid 10001 (Dockerfile); the file holds every
    # client's phones, so it is given to that user, never opened to all.
    "denied": (
        "Qidirilgan joy: {path}. Fayl bor, lekin dastur uni oʻqiy olmaydi: serverda "
        "sudo chown 10001 data/imports/{name} qiling (hammaga ochmang) va qayta "
        "ishga tushiring."
    ),
}
# Where make puts a bare FILE name: the server's data/imports/, the
# containers' /data/imports (docker-compose.yml, ./data:/data).
IMPORTS_DIR = Path("/data/imports")
# The export holds the name, code, manager and phones of every client: once
# written, it is not left on the server's disk.
REMOVED_TEXT = (
    "Fayl serverdan oʻchirildi: data/imports/{name} — mijozlar raqamlari diskda "
    "qolmasin."
)
REMOVE_FAILED_TEXT = (
    "⚠️ Faylni oʻchirib boʻlmadi: serverda rm data/imports/{name} qiling — mijozlar "
    "raqamlari diskda qolmasin."
)
REMOVE_YOURSELF_TEXT = (
    "⚠️ Endi faylni oʻchiring ({path}) — mijozlar raqamlari diskda qolmasin."
)


def _forget_file(path: Path, imports_dir: Path) -> str:
    """After a written import: the file goes when it is one of data/imports/
    (where make put it), else the owner is told to delete it."""
    resolved = path.resolve()
    if resolved.parent != imports_dir.resolve():
        return REMOVE_YOURSELF_TEXT.format(path=path)
    try:
        resolved.unlink()
    except OSError:
        return REMOVE_FAILED_TEXT.format(name=path.name)
    return REMOVED_TEXT.format(name=path.name)


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
    imports_dir: Path = IMPORTS_DIR,
) -> tuple[int, list[str]]:
    """The exit code and the lines to print. A written import deletes its
    file from ``imports_dir`` (or tells the owner to delete it)."""
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
        lines.append(_forget_file(Path(path), imports_dir))
        return 0, lines
    except client_import.BadFile as exc:
        name = Path(path).name
        lines = [CANNOT_READ_TEXT.format(name=name, why=exc.text())]
        if exc.code in HINT_TEXT:
            hint = HINT_TEXT[exc.code]
            lines.append(hint.format(path=Path(path).absolute(), name=name))
        return 1, lines


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
