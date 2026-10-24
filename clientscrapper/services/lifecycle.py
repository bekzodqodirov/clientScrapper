# VENDORED_FROM dailyJournal@6b1f67f miya/userbot/main.py
"""Process lifecycle shared by the worker, bot and userbot.

From MIYA's userbot: SIGTERM (``docker compose stop``) and SIGINT become a
clean shutdown instead of Python's immediate exit, and a process with nothing
to do idles rather than exits — every app container runs with ``restart:
unless-stopped``, so a clean exit would be restarted in a tight loop forever.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Callable, Coroutine
from typing import Any

log = logging.getLogger(__name__)


def install_stop_signals(stop: asyncio.Event) -> tuple[int, ...]:
    """Set ``stop`` on SIGINT/SIGTERM. Returns the signals installed (none
    where the loop cannot take them)."""
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError) as exc:
            log.warning(
                "signal handler not installed",
                extra={"signal": int(sig), "error": type(exc).__name__},
            )
    return tuple(installed)


def remove_stop_signals(installed: tuple[int, ...]) -> None:
    loop = asyncio.get_running_loop()
    for sig in installed:
        try:
            loop.remove_signal_handler(sig)
        except Exception as exc:
            log.warning(
                "signal handler not removed",
                extra={"signal": int(sig), "error": type(exc).__name__},
            )


async def idle_until_stopped() -> None:
    """Wait for SIGINT/SIGTERM and return."""
    stop = asyncio.Event()
    installed = install_stop_signals(stop)
    try:
        await stop.wait()
    finally:
        remove_stop_signals(installed)


def run_main(run: Callable[[], Coroutine[Any, Any, None]]) -> int:
    """MIYA's ``main()``: a ``SystemExit`` with a reason prints it and exits 1."""
    try:
        asyncio.run(run())
    except SystemExit as exc:
        if exc.code:
            print(exc.code, file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        pass
    return 0
