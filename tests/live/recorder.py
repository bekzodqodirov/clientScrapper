"""``make smoke``'s recorder (07 §1.12): every live test's outcome, written to
``collector_runs`` through ``services/smoke.record`` for the owner's digest.

Registered only with ``LIVE=1`` (``tests/live/conftest.py``). A test names its
collector with ``@pytest.mark.live(source="<sources.key>")``; the outcome is
the call's (or the setup's, when that skipped or failed; a failed teardown
turns a pass into a failure). A failure on the Fetcher's host breaker
(``http.HostPaused``) is ``blocked``: its row ends ``failed`` with
``error='host_blocked'`` (07 §1.12). ``CS_SMOKE_FULL=1`` marks the quarterly
full run. A session that collected no live test at all (before the
collectors bring theirs) exits 0 with a line that says so, rather than
pytest's «no tests ran» error in the weekly cron; a session whose outcomes
could not be written exits 1, so the cron's log shows it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

import pytest

from clientscrapper.config import settings
from clientscrapper.services import http, smoke


def outcome_of(report: pytest.TestReport) -> smoke.Outcome:
    if report.passed:
        return "passed"
    if report.skipped:
        return "skipped"
    return "failed"


@dataclass
class SmokeRecorder:
    NAME: ClassVar[str] = "clientscrapper-smoke-recorder"

    full: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(settings.tz))
    results: dict[str, smoke.TestResult] = field(default_factory=dict)
    unnamed: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> SmokeRecorder:
        return cls(full=os.environ.get("CS_SMOKE_FULL") == "1")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[Any]
    ) -> Generator[None, Any, None]:
        made = yield
        report: pytest.TestReport = made.get_result()
        marker = item.get_closest_marker("live")
        if marker is None:
            return
        source = marker.kwargs.get("source")
        if not isinstance(source, str) or not source:
            if item.nodeid not in self.unnamed:
                self.unnamed.append(item.nodeid)
            return
        if report.when != "call" and report.passed:
            return
        earlier = self.results.get(item.nodeid)
        # A failed teardown turns a pass into a failure, nothing else.
        if (
            report.when == "teardown"
            and earlier is not None
            and earlier.outcome != "passed"
        ):
            return
        blocked = call.excinfo is not None and call.excinfo.errisinstance(http.HostPaused)
        self.results[item.nodeid] = smoke.TestResult(
            source_key=source,
            test=item.nodeid,
            outcome=outcome_of(report),
            blocked=blocked,
        )

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")

        def say(line: str) -> None:
            if reporter is not None:
                reporter.write_line(line)

        for nodeid in self.unnamed:
            say(f"smoke: {nodeid} names no source (pytest.mark.live(source=...))")
        if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
            say("smoke: no live test yet — the collectors bring theirs")
            session.exitstatus = pytest.ExitCode.OK
            return
        if not self.results:
            return
        try:
            asyncio.run(self.write(list(self.results.values())))
        except Exception as exc:
            say(f"smoke: the outcomes were not recorded ({type(exc).__name__})")
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            return
        say(f"smoke: {len(self.results)} outcome(s) recorded for the digest")

    async def write(self, results: list[smoke.TestResult]) -> None:
        from clientscrapper.db.session import SessionLocal, engine

        try:
            async with SessionLocal() as session:
                await smoke.record(
                    session,
                    results,
                    started_at=self.started_at,
                    now=datetime.now(settings.tz),
                    full=self.full,
                )
                await session.commit()
        finally:
            await engine.dispose()
