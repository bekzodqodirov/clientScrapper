# VENDORED_FROM dailyJournal@6b1f67f miya/userbot/main.py
"""The read-only Telegram reader process.

Skeleton (T-01): it configures logging and idles until stopped, whatever
USERBOT_ENABLED says — the Telethon client and the prefilter arrive with
T-13. It never constructs a client and never writes.

T-08: before idling it beats the ``userbot`` heartbeat once as switched off,
``{"enabled": false, "built": false}`` — MIYA's kill-switch beat (06 §4.1):
nothing reads Telegram until T-13, whatever the switch says, and a reader
that reads nothing on purpose is neither silent nor unhealthy. The pool is
then released: an idle process holds no database connection.
"""

from __future__ import annotations

import logging

from clientscrapper.config import settings
from clientscrapper.db.session import engine, session_scope
from clientscrapper.logs import setup_logging
from clientscrapper.services import health
from clientscrapper.services.lifecycle import idle_until_stopped, run_main

log = logging.getLogger(__name__)


async def beat_switched_off() -> None:
    """The one beat of a reader that reads nothing. Never raises."""
    try:
        async with session_scope() as session:
            await health.beat(
                session, "userbot", detail={"enabled": False, "built": False}
            )
    except Exception as exc:
        log.warning("userbot heartbeat not written", extra={"error": type(exc).__name__})
    finally:
        await engine.dispose()


async def run() -> None:
    setup_logging("userbot")
    log.info(
        "userbot started: the reader is not built yet",
        extra={"enabled": settings.userbot_enabled},
    )
    await beat_switched_off()
    await idle_until_stopped()
    log.info("userbot stopped")


def main() -> int:
    return run_main(run)


if __name__ == "__main__":
    raise SystemExit(main())
