# VENDORED_FROM dailyJournal@6b1f67f miya/worker/main.py
"""The scheduler process.

Skeleton (T-01): it configures logging and idles until stopped. The scheduler,
its jobs and the heartbeat arrive with T-08; nothing here touches the network.
"""

from __future__ import annotations

import logging

from clientscrapper.logs import setup_logging
from clientscrapper.services.lifecycle import idle_until_stopped, run_main

log = logging.getLogger(__name__)


async def run() -> None:
    setup_logging("worker")
    log.info("worker started: no jobs are registered yet")
    await idle_until_stopped()
    log.info("worker stopped")


def main() -> int:
    return run_main(run)


if __name__ == "__main__":
    raise SystemExit(main())
