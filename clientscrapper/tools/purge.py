"""Forget a subject from the command line (06 §5.4, §3.3).

    make purge COMPANY=<uuid> | PHONE=<tail9> | TG_USER=<id> [APPLY=1]
    python -m clientscrapper.tools.purge --company <uuid> | --phone <tail9>
        | --tg-user <id> [--reason legal_request|dnc|b2c|owner] [--apply]

The same two functions as ``/unut`` (``services/purge``): the plan is printed
first — counts only, never a name — and nothing changes without ``--apply``,
which writes the plan's ``purge_log`` row and executes it. ``--reason``
defaults to ``legal_request``: every key the subject had becomes a
``suppression(legal)`` row (``dnc`` writes ``dnc`` rows; ``b2c``/``owner``
write none). Exit codes: 0 planned or executed, 1 the subject is unknown or
the purge was refused.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from clientscrapper.collectors.runner import SessionFactory
from clientscrapper.config import settings
from clientscrapper.db.session import SessionLocal, engine
from clientscrapper.services import purge
from clientscrapper.services.owner import owner_ref

DRY_RUN_TEXT: Final = "Sinov: hech narsa oʻchirilmadi. Oʻchirish uchun APPLY=1 qoʻshing."
UNKNOWN_TEXT: Final = "Bunday kompaniya topilmadi: {subject}"
REFUSED_TEXT: Final = "Oʻchirilmadi: {code}"
CLI_REQUESTER: Final = "owner:cli"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="purge", description="forget a subject")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--company", type=uuid.UUID, help="a company uuid")
    which.add_argument("--phone", help="the last nine digits of a phone")
    which.add_argument("--tg-user", type=int, help="a Telegram user id")
    parser.add_argument(
        "--reason", choices=purge.OWNER_REASONS, default=purge.DEFAULT_REASON
    )
    parser.add_argument("--apply", action="store_true", help="execute the plan")
    return parser.parse_args(argv)


def requester() -> str:
    """``owner:<tg_id>`` when the owner is configured (the owner runs make)."""
    owner = settings.owner_telegram_id
    return owner_ref(owner) if owner is not None else CLI_REQUESTER


async def run(
    args: argparse.Namespace,
    *,
    now: datetime | None = None,
    sessions: SessionFactory = SessionLocal,
) -> tuple[int, list[str]]:
    """The exit code and the lines to print."""
    async with sessions() as session:
        try:
            if args.company is not None:
                plan = await purge.plan_company(session, args.company, reason=args.reason)
            else:
                plan = await purge.plan_contact(
                    session,
                    phone_tail=args.phone,
                    tg_user_id=args.tg_user,
                    reason=args.reason,
                )
        except purge.PurgeRefused:
            return 1, [UNKNOWN_TEXT.format(subject=args.company)]
        except ValueError as bad:
            return 1, [REFUSED_TEXT.format(code=bad)]
        lines = purge.plan_lines(plan)
        if not args.apply:
            lines.append(DRY_RUN_TEXT)
            return 0, lines
        try:
            result = await purge.execute(
                session,
                plan,
                requested_by=requester(),
                now=now or datetime.now(settings.tz),
            )
        except purge.PurgeRefused as refused:
            return 1, [*lines, REFUSED_TEXT.format(code=refused.code)]
    return 0, [*lines, *purge.result_lines(result, reason=plan.reason)]


async def _main(args: argparse.Namespace) -> int:
    try:
        code, lines = await run(args)
    finally:
        await engine.dispose()
    print("\n".join(lines))
    return code


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
