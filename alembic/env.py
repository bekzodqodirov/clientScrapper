# VENDORED_FROM dailyJournal@6b1f67f alembic/env.py
"""Alembic environment.

Runs synchronously with psycopg3 — the same driver the app uses in async mode,
so the URL from settings works unchanged.

A caller may hand over its own target instead (tests/test_migrations.py runs
the round trip on a scratch database): a ``sqlalchemy.url`` already set on the
Config wins over DATABASE_URL, a ``connection`` in ``config.attributes`` wins
over both, and ``configure_logger=False`` keeps the ini's logging out of a
process that has its own.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from alembic import context
from clientscrapper.config import settings
from clientscrapper.db import models as _models  # noqa: F401 - populates Base.metadata
from clientscrapper.db.base import Base
from clientscrapper.db.session import async_url

config = context.config

configure_logger = config.attributes.get("configure_logger", True)
if config.config_file_name is not None and configure_logger:
    fileConfig(config.config_file_name)

if not config.get_main_option("sqlalchemy.url"):
    # psycopg3 serves the sync engine too; a bare postgresql:// would mean psycopg2.
    url = async_url(settings.database_url)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

target_metadata = Base.metadata

COMPARE = {"compare_type": True, "compare_server_default": True}


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **COMPARE,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, **COMPARE)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    given = config.attributes.get("connection")
    if given is not None:
        _run(given)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
