# VENDORED_FROM dailyJournal@6b1f67f miya/bot/main.py
"""The owner bot process.

Skeleton (T-01): it configures logging and idles until stopped. It does not
log in to Telegram — the dispatcher, the owner-only routers and the watchdog
arrive with T-20, together with the refusal to start without
ASSISTANT_BOT_TOKEN and OWNER_TELEGRAM_ID (06 §1.1).
"""

from __future__ import annotations

import logging

from clientscrapper.logs import setup_logging
from clientscrapper.services.lifecycle import idle_until_stopped, run_main

log = logging.getLogger(__name__)


async def run() -> None:
    setup_logging("bot")
    log.info("bot started: not connected to Telegram yet")
    await idle_until_stopped()
    log.info("bot stopped")


def main() -> int:
    return run_main(run)


if __name__ == "__main__":
    raise SystemExit(main())
