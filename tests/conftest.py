# VENDORED_FROM dailyJournal@6b1f67f tests/conftest.py
"""Shared fixtures (07 §1.0).

Database-backed tests skip when no migrated database answers at
``DATABASE_URL``, so ``pytest -q`` works on a laptop with no containers
running. Set ``CS_REQUIRE_DB=1`` (CI does) to turn that skip into a failure: a
suite that silently ran without its database cannot pass. Tests that all
report «skipped» mean postgres is down, not that they pass.

``live`` tests run only with ``LIVE=1`` (``make smoke``, from the VPS), and
``slow`` ones only with ``CS_RUN_SLOW=1``; both are reported as skipped,
never counted green.

Dates in tests: hard-code a calendar date only when the test passes it to the
code under test (``now=``). Anything compared against code that reads the real
clock must be derived from the real clock.

``CS_TIME_TRAVEL=newyear`` or ``=+Nd`` runs the suite at another moment
(tests/timetravel.py, CI's ``time-travel`` job); it is for runs without a
database.

No test reaches the model (06 §5.2): ``model_api`` installs a
``FakeAnthropic`` transport on ``services/extraction`` for every test and
fails the test whose request still carried a phone, a handle, an e-mail or a
14-digit run, or that called the model with no answer scripted. A test that
expects a call asks for ``model_api`` by name and queues its answers.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.db import models as _models  # noqa: F401 - populates Base.metadata
from clientscrapper.db.base import Base
from clientscrapper.db.session import SessionLocal, engine

if TYPE_CHECKING:
    from tests.fakes import FakeAnthropic

# Migration-seeded configuration, never wiped between tests. A test that flips
# sources.enabled/config or activates a weights version snapshots what it
# changes in its own setup and restores it (a restore written after the fact
# restores what it found).
KEPT_TABLES = frozenset({"sources", "scoring_weights"})


def require_db() -> bool:
    return os.environ.get("CS_REQUIRE_DB") == "1"


def truncate_statement() -> str:
    """One TRUNCATE for every table but the seeded ones.

    TRUNCATE on purpose: the ``cs_append_only`` triggers refuse a row DELETE on
    push_log, outcomes, merge_log, purge_log and usage_log, and TRUNCATE fires
    no row triggers. One statement, so the order of foreign keys is the
    server's problem, not this list's.
    """
    tables = Base.metadata.sorted_tables
    names = sorted(t.name for t in tables if t.name not in KEPT_TABLES)
    return f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE"


def pytest_configure(config: pytest.Config) -> None:
    # Before any test module is imported, so module-level stamps are taken in
    # the travelled time.
    if os.environ.get("CS_TIME_TRAVEL"):
        from tests import timetravel

        timetravel.start()


def pytest_unconfigure(config: pytest.Config) -> None:
    if os.environ.get("CS_TIME_TRAVEL"):
        from tests import timetravel

        timetravel.stop()


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    live = os.environ.get("LIVE") == "1"
    slow = os.environ.get("CS_RUN_SLOW") == "1"
    for item in items:
        if "live" in item.keywords and not live:
            item.add_marker(pytest.mark.skip(reason="live: set LIVE=1 (make smoke, VPS)"))
        if "slow" in item.keywords and not slow:
            item.add_marker(pytest.mark.skip(reason="slow: set CS_RUN_SLOW=1"))


async def database_available() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1 FROM sources LIMIT 0"))
        return True
    except Exception:
        return False


async def need_database() -> None:
    """Skip — or, under CS_REQUIRE_DB=1, fail — when no migrated database answers."""
    if not await database_available():
        await engine.dispose()
        if require_db():
            pytest.fail("CS_REQUIRE_DB=1 but no migrated database at DATABASE_URL")
        pytest.skip("no migrated database reachable at DATABASE_URL")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """An AsyncSession on a database whose owner data was just wiped."""
    await need_database()
    async with SessionLocal() as s:
        await s.execute(sa.text(truncate_statement()))
        await s.commit()
        yield s
        await s.rollback()
    # Each test runs on its own event loop; a pooled connection must not
    # outlive the loop that opened it.
    await engine.dispose()


@pytest.fixture(autouse=True)
def model_api() -> Iterator[FakeAnthropic]:
    """The Messages API for this test: a fake transport, no network, and the
    PII fence on every request body (06 §5.2). Imported here, not at the
    top: conftest is loaded before ``CS_TIME_TRAVEL`` starts (see
    ``pytest_configure``), and these modules must be imported in the
    travelled time like every test module."""
    from clientscrapper.services import extraction
    from tests.fakes import FakeAnthropic

    fake = FakeAnthropic()
    extraction.set_client(fake.client())
    extraction.reset_grammar_memory()
    yield fake
    extraction.set_client(None)
    extraction.reset_grammar_memory()
    if fake.leaks:
        pytest.fail(f"a model request carried personal data: {fake.leaks}")
    if fake.unexpected:
        pytest.fail(f"{fake.unexpected} model request(s) with no scripted answer")
