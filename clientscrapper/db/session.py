# VENDORED_FROM dailyJournal@6b1f67f miya/db/session.py
"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from clientscrapper.config import settings


def async_url(url: str) -> str:
    """psycopg3 speaks both sync and async; normalise the driver name."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


# A statement that waits this long on a row or table lock raises (a forgotten
# `make psql` holding FOR UPDATE, a stuck transaction) instead of hanging its
# job for ever while max_instances=1 refuses every later run: the job fails,
# and job_failing says so (seam review wave 4). Longer than any wait this code
# makes on purpose — a tap behind a card edit's Telegram call, a purge behind
# a model call that holds its company (60 s per try).
LOCK_TIMEOUT = "300s"

engine: AsyncEngine = create_async_engine(
    async_url(settings.database_url),
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    connect_args={"options": f"-c lock_timeout={LOCK_TIMEOUT}"},
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on error."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """A dependency-style generator around ``session_scope``."""
    async with session_scope() as session:
        yield session
