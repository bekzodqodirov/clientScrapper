"""The live tests' directory (07 §1.12): ``make smoke`` runs it from the VPS.

Each ``tests/live/test_<key>_live.py`` comes with its collector (03 §1.4),
carries ``@pytest.mark.live(source="<sources.key>")`` and makes one real
request; with ``LIVE=1`` the recorder (``tests/live/recorder.py``) writes
every test's outcome into ``collector_runs`` for the owner's digest. Without
``LIVE=1`` (CI, a laptop) the root conftest skips them and nothing is
recorded — a skipped live test is reported as skipped, never counted green.
"""

from __future__ import annotations

import os

import pytest

from clientscrapper.services.http import HostState

# One smoke run is one process, as the worker is: every live test's Fetcher
# shares these per-host buckets and robots cache, so two tests on one host
# are paced like two requests of one run and robots.txt is read once (the
# wave-5 seam review: a fresh HostState per test sent goldenpages two robots
# fetches and two pages 0.06 s apart).
HOSTS = HostState()


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get("LIVE") != "1":
        return
    from tests.live.recorder import SmokeRecorder

    if config.pluginmanager.get_plugin(SmokeRecorder.NAME) is None:
        config.pluginmanager.register(SmokeRecorder.from_env(), SmokeRecorder.NAME)
