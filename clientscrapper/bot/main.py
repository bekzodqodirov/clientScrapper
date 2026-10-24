# VENDORED_FROM dailyJournal@6b1f67f miya/bot/main.py
"""The owner bot process.

Skeleton (T-01): it configures logging and idles until stopped. It does not
log in to Telegram — the dispatcher, the owner-only routers and the watchdog
arrive with T-20, together with the refusal to start without
ASSISTANT_BOT_TOKEN and OWNER_TELEGRAM_ID (06 §1.1).

T-08: while it idles it beats the ``bot`` heartbeat every minute (06 §4.1),
``{"telegram": false}`` until T-20 connects it, so the container's
healthcheck (``tools/healthcheck.py``) and the health job see a live process.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Final

from clientscrapper.db.session import engine
from clientscrapper.logs import setup_logging
from clientscrapper.services import health
from clientscrapper.services.lifecycle import (
    install_stop_signals,
    remove_stop_signals,
    run_main,
)

log = logging.getLogger(__name__)

BEAT_EVERY: Final = timedelta(minutes=1)


def beat_detail() -> dict[str, Any]:
    return {"telegram": False}


async def run() -> None:
    setup_logging("bot")
    log.info("bot started: not connected to Telegram yet")
    stop = asyncio.Event()
    installed = install_stop_signals(stop)
    try:
        await health.keep_beating("bot", every=BEAT_EVERY, detail=beat_detail, stop=stop)
    finally:
        remove_stop_signals(installed)
        await engine.dispose()
    log.info("bot stopped")


def main() -> int:
    return run_main(run)


if __name__ == "__main__":
    raise SystemExit(main())
