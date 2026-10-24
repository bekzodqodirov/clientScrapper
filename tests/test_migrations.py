"""0001 on a scratch database (02 §5, §10 AC 1; 07 §1.8).

Runs on ``CS_MIGRATION_DATABASE_URL`` — never on ``DATABASE_URL``: the fixture
empties its ``public`` schema first and the round trip ends in ``downgrade
base``. Without it the tests skip, and under
``CS_REQUIRE_DB=1`` they fail: a skipped migration test proves nothing.

``compare_metadata`` (what ``alembic check`` runs) is blind to CHECK text, a
partial index's WHERE, an index's method or operator class, and a generated
column's expression. So the models are also built for real: inside a
rolled-back transaction ``Base.metadata.create_all`` makes a second copy of
every table in a probe schema, and every constraint, index and column default
the server prints for it must equal what it prints for the migration's tables,
character for character. The server canonicalises both sides the same way
(``IN`` → ``= ANY (ARRAY[…])``, casts), so no text is compared by hand — an
operator turned round (``<=`` → ``>=``, ``!~`` → ``~``, ``IN`` → ``NOT IN``)
is a difference. ``alembic upgrade head --sql`` is rebuilt the same way, so
offline mode cannot drift from online either.
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
    "cs_candidate_suppressed_by",
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
MODEL_PROBE = "cs_model_probe"
OFFLINE_PROBE = "cs_offline_probe"


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


def _config(url: str, output: io.StringIO | None = None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"), output_buffer=output)
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
        for schema in (MODEL_PROBE, OFFLINE_PROBE):
            conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
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


# --- the catalog as the server prints it -------------------------------------------

_NAMESPACE = "(SELECT oid FROM pg_namespace WHERE nspname = :schema)"


def catalog(conn: sa.Connection, schema: str) -> dict[str, dict[tuple[str, ...], str]]:
    """Every constraint, index, column default / generated expression, trigger
    and function of ``schema``, keyed by name, with the schema's own name taken
    out of the text.

    The caller sets the search path first: ``public`` alone for the migration's
    tables, ``<probe>, public`` for a probe, so a name the probe shadows (its
    own tables, its own functions) and one it borrows from public (pg_trgm, the
    cs_* functions the model probe has none of) print unqualified on both
    sides.
    """
    params = {"schema": schema}

    def rows(sql: str) -> list[sa.Row[Any]]:
        return list(conn.execute(sa.text(sql), params))

    def plain(text: str) -> str:
        return text.replace(f"{schema}.", "")

    constraints = {
        (r.table, r.name): f"{r.kind}: {plain(r.definition)}"
        for r in rows(
            "SELECT cl.relname AS table, c.conname AS name, c.contype AS kind, "
            "pg_get_constraintdef(c.oid) AS definition FROM pg_constraint c "
            f"JOIN pg_class cl ON cl.oid = c.conrelid WHERE c.connamespace = {_NAMESPACE}"
        )
    }
    indexes = {
        (r.tablename, r.indexname): plain(r.indexdef)
        for r in rows("SELECT tablename, indexname, indexdef FROM pg_indexes "
                      "WHERE schemaname = :schema")
    }  # fmt: skip
    defaults = {
        (r.table, r.column): f"{r.generated or '-'}: {plain(r.expression)}"
        for r in rows(
            "SELECT c.relname AS table, a.attname AS column, "
            "a.attgenerated AS generated, "
            "pg_get_expr(d.adbin, d.adrelid) AS expression FROM pg_attrdef d "
            "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
            f"JOIN pg_class c ON c.oid = d.adrelid WHERE c.relnamespace = {_NAMESPACE}"
        )
    }
    triggers = {
        (r.table, r.name): plain(r.definition)
        for r in rows(
            "SELECT c.relname AS table, t.tgname AS name, "
            "pg_get_triggerdef(t.oid) AS definition FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            f"WHERE NOT t.tgisinternal AND c.relnamespace = {_NAMESPACE}"
        )
    }
    functions = {
        (r.name,): plain(r.definition)
        for r in rows(
            "SELECT p.proname AS name, pg_get_functiondef(p.oid) AS definition "
            f"FROM pg_proc p WHERE p.pronamespace = {_NAMESPACE} "
            "AND p.proname LIKE 'cs\\_%'"
        )
    }
    return {
        "constraints": constraints,
        "indexes": indexes,
        "defaults": defaults,
        "triggers": triggers,
        "functions": functions,
    }


def differences(
    ours: dict[tuple[str, ...], str], theirs: dict[tuple[str, ...], str]
) -> dict[tuple[str, ...], tuple[str | None, str | None]]:
    return {
        key: (ours.get(key), theirs.get(key))
        for key in ours.keys() | theirs.keys()
        if ours.get(key) != theirs.get(key)
    }


@contextmanager
def rolled_back(engine: sa.Engine) -> Iterator[sa.Connection]:
    """One connection inside a transaction that is always rolled back."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
        finally:
            trans.rollback()


def _public_catalog(conn: sa.Connection) -> dict[str, dict[tuple[str, ...], str]]:
    conn.execute(sa.text("SET LOCAL search_path TO public"))
    return catalog(conn, "public")


def _probe_catalog(
    conn: sa.Connection, probe: str
) -> dict[str, dict[tuple[str, ...], str]]:
    conn.execute(sa.text(f"SET LOCAL search_path TO {probe}, public"))
    return catalog(conn, probe)


def _seed_rows(conn: sa.Connection) -> dict[str, list[object]]:
    # Unqualified: the caller's search path picks the schema.
    sources = conn.execute(
        sa.text(
            "SELECT to_jsonb(s) - 'created_at' - 'updated_at' FROM sources s ORDER BY key"
        )
    ).scalars()
    weights = conn.execute(
        sa.text(
            "SELECT to_jsonb(w) - 'created_at' - 'approved_at', approved_at IS NOT NULL "
            "FROM scoring_weights w ORDER BY version, key"
        )
    ).all()
    version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
    return {
        "sources": list(sources),
        "weights": [tuple(r) for r in weights],
        "alembic_version": list(version),
    }


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


def test_the_models_build_the_migrations_catalog_exactly(scratch):
    """CHECK text, partial-index predicates, index methods and operator classes,
    FK actions, defaults and the generated column — as the server prints them."""
    engine, _ = scratch
    with rolled_back(engine) as conn:
        live = _public_catalog(conn)
        conn.execute(sa.text(f"CREATE SCHEMA {MODEL_PROBE}"))
        conn.execute(sa.text(f"SET LOCAL search_path TO {MODEL_PROBE}, public"))
        Base.metadata.create_all(
            conn.execution_options(schema_translate_map={None: MODEL_PROBE}),
            checkfirst=False,
        )
        model = _probe_catalog(conn, MODEL_PROBE)
    for part in ("constraints", "indexes", "defaults"):
        # alembic_version is the migration's own table, not a model.
        tables = {k: v for k, v in live[part].items() if k[0] != "alembic_version"}
        assert len(tables) > 20, part  # the comparison must have a subject
        assert differences(model[part], tables) == {}, part


def test_offline_sql_builds_the_same_schema_and_seeds(scratch):
    """``alembic upgrade head --sql`` once crashed on the JSONB seed; its
    script, run into a probe schema, must give what the online upgrade gave."""
    engine, _ = scratch
    url = engine.url.render_as_string(hide_password=False)
    output = io.StringIO()
    command.upgrade(_config(url, output), "head", sql=True)
    script = output.getvalue()
    body = "\n".join(
        line for line in script.splitlines() if line.strip() not in {"BEGIN;", "COMMIT;"}
    )
    assert "INSERT INTO sources" in body and "INSERT INTO scoring_weights" in body
    with rolled_back(engine) as conn:
        live = _public_catalog(conn)
        live_seeds = _seed_rows(conn)
        conn.execute(sa.text(f"CREATE SCHEMA {OFFLINE_PROBE}"))
        conn.execute(sa.text(f"SET LOCAL search_path TO {OFFLINE_PROBE}, public"))
        # The driver's own cursor and no parameters: the script holds «%» in
        # the function bodies, which a parameterised execute reads as a
        # placeholder.
        conn.connection.cursor().execute(body)
        offline = _probe_catalog(conn, OFFLINE_PROBE)
        offline_seeds = _seed_rows(conn)
    for part, entries in live.items():
        assert entries, part
        assert differences(offline[part], entries) == {}, part
    assert offline_seeds == live_seeds
    assert len(live_seeds["sources"]) == 23 and len(live_seeds["weights"]) == 26


def test_no_check_spans_a_set_null_foreign_key_column(scratch):
    """fullprompt #809: a referential SET NULL (or SET DEFAULT) runs as an
    UPDATE, and a CHECK on the same column then refuses the parent's DELETE
    instead of letting it through — the action can never take effect."""
    engine, _ = scratch
    with engine.connect() as conn:
        clashes = conn.execute(
            sa.text(
                "SELECT f.conrelid::regclass::text AS table, a.attname AS column, "
                "c.conname AS check FROM pg_constraint f "
                "JOIN pg_attribute a ON a.attrelid = f.conrelid "
                "AND a.attnum = ANY (f.conkey) "
                "JOIN pg_constraint c ON c.conrelid = f.conrelid AND c.contype = 'c' "
                "AND a.attnum = ANY (c.conkey) "
                "WHERE f.contype = 'f' AND f.confdeltype IN ('n', 'd') "
                "AND f.connamespace = 'public'::regnamespace"
            )
        ).all()
        set_null = conn.execute(
            sa.text(
                "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
                "AND confdeltype = 'n' AND connamespace = 'public'::regnamespace"
            )
        ).scalar()
    assert set_null and set_null > 10  # the fence must have something to guard
    assert [tuple(r) for r in clashes] == []


def test_phone_tail_is_generated_from_the_phone(scratch):
    engine, _ = scratch
    (expression,) = _scalars(
        engine,
        "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
        "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
        "WHERE d.adrelid = 'contacts'::regclass AND a.attname = 'phone_tail' "
        "AND a.attgenerated = 's'",
    )
    assert expression == "cs_phone_tail(phone)"


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
        # Replaces 02's ck_lead_candidates_suppressed (#809, see 0001).
        | {
            (
                "lead_candidates",
                "trg_lead_candidates_suppressed",
                "cs_candidate_suppressed_by",
            )
        }
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
