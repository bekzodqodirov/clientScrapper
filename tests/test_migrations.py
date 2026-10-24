"""0001 on a scratch database (02 §5, §10 AC 1; 07 §1.8).

Runs on ``CS_MIGRATION_DATABASE_URL`` — never on ``DATABASE_URL``: the fixture
empties its ``public`` schema first and the round trip ends in ``downgrade
base``. Without it the tests skip, and under
``CS_REQUIRE_DB=1`` they fail: a skipped migration test proves nothing.

``compare_metadata`` (what ``alembic check`` runs) is blind to four things,
measured by editing the models one at a time: CHECK text, a partial index's
WHERE, an index's method/operator class, and a generated column's expression.
Those four are compared against the catalog here instead. PG rewrites the text
it stores (``IN`` → ``= ANY (ARRAY[…])``, ``BETWEEN`` → ``>= AND <=``, casts),
so the comparison is by signature: the literals and numbers, and the table's
columns the expression names.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from clientscrapper.db import models as _models  # noqa: F401 - populates Base.metadata
from clientscrapper.db.base import Base
from clientscrapper.db.session import async_url

ROOT = Path(__file__).resolve().parents[1]

FUNCTIONS = {
    "cs_phone_tail",
    "cs_username_key",
    "cs_domain_key",
    "cs_touch_updated_at",
    "cs_append_only",
    "cs_scoring_weights_frozen",
}
TOUCHED = {
    "sources",
    "companies",
    "contacts",
    "lead_candidates",
    "chat_monitors",
    "sheet_rows",
    "runtime_flags",
}
APPEND_ONLY = {"push_log", "outcomes", "merge_log", "purge_log", "usage_log"}


def _scratch_url() -> str:
    url = os.environ.get("CS_MIGRATION_DATABASE_URL", "")
    reason = ""
    if not url:
        reason = (
            "CS_MIGRATION_DATABASE_URL is not set (a scratch database for the round trip)"
        )
    elif async_url(url) == async_url(os.environ.get("DATABASE_URL", "")):
        reason = (
            "CS_MIGRATION_DATABASE_URL is DATABASE_URL: the round trip drops everything"
        )
    if reason:
        if os.environ.get("CS_REQUIRE_DB") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return async_url(url)


def _config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="module")
def scratch() -> Iterator[tuple[sa.Engine, Config]]:
    url = _scratch_url()
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception as exc:
        engine.dispose()
        if os.environ.get("CS_REQUIRE_DB") == "1":
            pytest.fail(f"scratch database unreachable: {type(exc).__name__}")
        pytest.skip(f"scratch database unreachable: {type(exc).__name__}")
    # Start from an empty database every run, so the migration as it stands
    # in the tree is what gets measured (a scratch left at head by an earlier
    # run would skip it).
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
    assert _public_tables(engine) == set()
    cfg = _config(url)
    command.upgrade(cfg, "head")
    yield engine, cfg
    engine.dispose()


def _scalars(engine: sa.Engine, sql: str) -> list[object]:
    with engine.connect() as conn:
        return list(conn.execute(sa.text(sql)).scalars())


def _public_tables(engine: sa.Engine) -> set[str]:
    rows = _scalars(engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {str(r) for r in rows} - {"alembic_version"}


# --- signatures ------------------------------------------------------------------

_QUOTED = re.compile(r"'((?:[^']|'')*)'")
_CAST = re.compile(
    r"::(?:character varying|double precision|timestamp with time zone|[a-z_]+)"
    r"(?:\[\])?(?:\(\d+\))?",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")
_WORD = re.compile(r"[a-z_][a-z0-9_]*")


def signature(sql: str, columns: set[str]) -> tuple[Counter[str], frozenset[str]]:
    """(literals and numbers as a multiset, the columns and cs_* functions named)."""
    literals = [m.group(1).replace("''", "'") for m in _QUOTED.finditer(sql)]
    rest = _CAST.sub("", _QUOTED.sub(" ", sql))
    numbers = _NUMBER.findall(rest)
    words = {
        w for w in _WORD.findall(rest.lower()) if w in columns or w.startswith("cs_")
    }
    return Counter(literals + numbers), frozenset(words)


def _columns(table: sa.Table) -> set[str]:
    return {c.name for c in table.columns}


# --- tests (file order matters: the round trip runs last) -------------------------


def test_upgrade_creates_the_02_counts(scratch):
    engine, _ = scratch
    tables = _public_tables(engine)
    assert len(tables) == 25
    assert tables == set(Base.metadata.tables)
    functions = _scalars(
        engine,
        "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
        "AND proname LIKE 'cs\\_%'",
    )
    assert set(map(str, functions)) == FUNCTIONS
    assert _scalars(engine, "SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'")
    assert _scalars(engine, "SELECT count(*) FROM sources") == [23]
    assert _scalars(engine, "SELECT count(*) FROM scoring_weights") == [26]
    assert _scalars(engine, "SELECT version_num FROM alembic_version") == ["0001_initial"]


def test_the_23_sources_are_the_schedulable_units_and_the_sub_sources(scratch):
    engine, _ = scratch
    with engine.connect() as conn:
        rows = {
            r.key: r
            for r in conn.execute(
                sa.text("SELECT key, family, enabled, cron, config FROM sources")
            )
        }
    schedulable = {
        "telegram_monitor",
        "instagram_discovery",
        "olx_business",
        "uzum_sellers",
        "directories",
        "hh_vacancies",
        "exhibitors",
        "maps",
        "brand_dealers",
        "serp_layer",
        "registry_lookup",
        "erp_sync",
        "manual",
        "enrich_site",
    }
    attribution = {
        "directories_goldenpages",
        "directories_yellowpages",
        "directories_glotr",
        "directories_prom",
        "maps_2gis",
        "maps_google",
        "search_brave",
        "search_yandex",
        "search_searxng",
    }
    assert set(rows) == schedulable | attribution
    assert [k for k, r in rows.items() if r.enabled] == ["enrich_site"]
    assert all(rows[k].cron is None for k in attribution)
    assert (
        rows["serp_layer"].family,
        rows["directories"].family,
        rows["maps"].family,
    ) == (
        "search",
        "directory",
        "maps",
    )
    assert (rows["enrich_site"].family, rows["enrich_site"].cron) == ("search", None)
    assert rows["erp_sync"].config == {
        "suppression": {
            "salt_id": None,
            "last_full_at": None,
            "last_delta_at": None,
            "count": 0,
        },
        "outcomes": {"cursor": None, "last_at": None},
    }


def test_no_drift_between_the_models_and_the_migration(scratch):
    engine, _ = scratch
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "target_metadata": Base.metadata,
            },
        )
        assert compare_metadata(ctx, Base.metadata) == []


def test_check_constraints_match_the_models(scratch):
    engine, _ = scratch
    with engine.connect() as conn:
        live = {
            (r.table, r.name): r.definition
            for r in conn.execute(
                sa.text(
                    "SELECT c.conrelid::regclass::text AS table, c.conname AS name, "
                    "pg_get_constraintdef(c.oid) AS definition FROM pg_constraint c "
                    "WHERE c.contype = 'c' AND c.connamespace = 'public'::regnamespace"
                )
            )
        }
    model = {
        (table.name, str(ck.name)): (str(ck.sqltext), _columns(table))
        for table in Base.metadata.tables.values()
        for ck in table.constraints
        if isinstance(ck, sa.CheckConstraint)
    }
    assert set(live) == set(model)
    differs = {
        key: (text, live[key])
        for key, (text, columns) in model.items()
        if signature(text, columns) != signature(live[key], columns)
    }
    assert differs == {}


def test_partial_indexes_and_index_methods_match_the_models(scratch):
    engine, _ = scratch
    with engine.connect() as conn:
        live = {
            r.indexname: r.indexdef
            for r in conn.execute(
                sa.text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = "
                    "'public'"
                )
            )
        }
    wrong = {}
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            name = str(index.name)
            definition = live.get(name)
            if definition is None:
                wrong[name] = "missing"
                continue
            options = index.dialect_options["postgresql"]
            method = options["using"] or "btree"
            if f"USING {method} " not in definition:
                wrong[name] = f"method: {definition}"
            for opclass in (options["ops"] or {}).values():
                if opclass not in definition:
                    wrong[name] = f"opclass: {definition}"
            where = options["where"]
            live_where = (
                definition.split(" WHERE ", 1)[1] if " WHERE " in definition else None
            )
            if (where is None) != (live_where is None):
                wrong[name] = f"where: {definition}"
            elif where is not None and live_where is not None:
                columns = _columns(table)
                if signature(str(where), columns) != signature(live_where, columns):
                    wrong[name] = f"where: {where} vs {live_where}"
    assert wrong == {}


def test_phone_tail_is_generated_from_the_phone(scratch):
    engine, _ = scratch
    (expression,) = _scalars(
        engine,
        "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
        "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
        "WHERE d.adrelid = 'contacts'::regclass AND a.attname = 'phone_tail' "
        "AND a.attgenerated = 's'",
    )
    computed = Base.metadata.tables["contacts"].c.phone_tail.computed
    assert computed is not None
    columns = _columns(Base.metadata.tables["contacts"])
    assert signature(str(expression), columns) == signature(
        str(computed.sqltext), columns
    )
    assert signature(str(expression), columns)[1] == {"cs_phone_tail", "phone"}


def test_the_triggers_of_02_section_2_16(scratch):
    engine, _ = scratch
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT c.relname AS table, t.tgname AS name, p.proname AS function "
                "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_proc p ON p.oid = t.tgfoid "
                "WHERE NOT t.tgisinternal AND c.relnamespace = 'public'::regnamespace"
            )
        ).all()
    found = {(r.table, r.name, r.function) for r in rows}
    expected = (
        {(t, "trg_touch_updated_at", "cs_touch_updated_at") for t in TOUCHED}
        | {(t, "trg_append_only", "cs_append_only") for t in APPEND_ONLY}
        | {("scoring_weights", "trg_scoring_weights_frozen", "cs_scoring_weights_frozen")}
    )
    assert found == expected


def test_downgrade_base_leaves_nothing_and_upgrade_head_comes_back(scratch):
    engine, cfg = scratch
    command.downgrade(cfg, "base")
    assert _public_tables(engine) == set()
    assert _scalars(
        engine,
        "SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
        "AND proname LIKE 'cs\\_%'",
    ) == [0]
    assert _scalars(
        engine, "SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'"
    ) == [0]
    command.upgrade(cfg, "head")
    assert len(_public_tables(engine)) == 25
    assert _scalars(engine, "SELECT count(*) FROM sources") == [23]
    assert _scalars(engine, "SELECT count(*) FROM scoring_weights") == [26]
