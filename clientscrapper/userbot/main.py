# VENDORED_FROM dailyJournal@6b1f67f miya/userbot/main.py
"""The read-only Telegram reader process.

Skeleton (T-01): it configures logging and idles until stopped, whatever
USERBOT_ENABLED says — the Telethon client, the kill switch's heartbeat and the
prefilter arrive with T-13. It never constructs a client and never writes.
"""

from __future__ import annotations

import logging

from clientscrapper.config import settings
from clientscrapper.logs import setup_logging
from clientscrapper.services.lifecycle import idle_until_stopped, run_main

log = logging.getLogger(__name__)


async def run() -> None:
    setup_logging("userbot")
    log.info(
        "userbot started: the reader is not built yet",
        extra={"enabled": settings.userbot_enabled},
    )
    await idle_until_stopped()
    log.info("userbot stopped")


def main() -> int:
    return run_main(run)


if __name__ == "__main__":
    raise SystemExit(main())
