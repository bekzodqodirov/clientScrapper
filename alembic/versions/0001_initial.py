"""The initial schema (02 §5): pg_trgm, 7 functions, 25 tables, the triggers,
23 `sources` rows and the 26 version-1 `scoring_weights` rows.

Every day-1 column is here, not in a 0002: runtime_flags, companies.dup_of /
dup_score, lead_reviews.kind, chat_monitors.status / priority /
backfilled_at, evidence.meta, interactions.fwd_from_peer_id.

The CHECK value lists, the weights and the source rows are frozen here, never
imported from the app: a migration must keep meaning what it meant the day it
ran. tests/test_enums_frozen.py and tests/test_weights_seed.py hold these
copies equal to clientscrapper/db/enums.py; tests/test_migrations.py holds
the tables equal to Base.metadata (``alembic check`` in CI).

Where this file departs from 02's literal DDL, on purpose (the T-01/T-02
review, before 0001 ever ran anywhere; each is commented where it is made and
pinned in tests/test_constraints.py):

* the «stored only as its key» CHECKs read ``col IS NOT DISTINCT FROM
  cs_key(col)``: with «=» every value the key function refuses (and answers
  NULL for) passed;
* ``companies.merged_into`` is ON DELETE RESTRICT and ``ck_lead_candidates_
  suppressed`` became the trigger ``trg_lead_candidates_suppressed``: a CHECK
  spanning a SET NULL FK column turns the parent's DELETE into an error
  (fullprompt #809);
* ``cs_scoring_weights_frozen`` freezes the whole approved row, and
  ``ck_scoring_weights_approved`` pairs approved_by with approved_at;
* ``cs_username_key`` strips the t.me prefix in either case;
* version 1 is approved by the activation UPDATE of 02 §2.7, and the sources
  seed renders as a literal, so ``alembic upgrade head --sql`` works.

Revision ID: 0001_initial
Revises:
Create Date: T-02
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- Frozen value lists (names as in clientscrapper/db/enums.py) ----------------

SOURCE_FAMILIES = (
    "telegram",
    "instagram",
    "olx",
    "marketplace",
    "directory",
    "vacancy",
    "exhibitor",
    "maps",
    "brand",
    "search",
    "registry",
    "erp",
    "manual",
)
RUN_STATUSES = ("running", "ok", "partial", "failed", "skipped")
OBSERVATION_STATUSES = ("new", "resolved", "rejected", "needs_review")
COMPANY_STATES = ("new", "active", "excluded", "client", "suppressed", "merged", "purged")
REGISTRY_STATUSES = ("unknown", "active", "inactive", "liquidated")
VOLUME_BANDS = ("unknown", "lt1", "1_3", "3_10", "truck", "container")
LEGAL_FORMS = ("MChJ", "OOO", "LLC", "QK", "AJ", "XK", "YaTT", "IP", "ChP", "SP", "TOO", "other")
CONTACT_TIERS = ("company", "person")
PHONE_KINDS = ("mobile", "landline", "short", "unknown")
LEGAL_BASES = ("public_source", "public_registry", "pre_contract_request", "consent", "contract")
ID_TYPES = (
    "hh_employer",
    "gis2_firm",
    "google_place",
    "uzum_shop",
    "olx_user",
    "tg_chat",
    "instagram",
    "facebook",
    "yandex_org",
    "myorg",
    "goldenpages",
    "yellowpages",
    "glotr",
    "prom",
    "exhibitor_slug",
)
SIGNAL_KEYS = (
    "vol_container",
    "vol_stated_3plus",
    "vol_stated_1_3",
    "vol_wholesale_bulky",
    "vol_marketplace_velocity",
    "vol_vacancy_import",
    "vol_exhibitor",
    "vol_directory_wholesale",
    "cn_origin",
    "cn_sector_prior",
    "ent_legal_confirmed",
    "ent_age_2y_active",
    "ent_not_micro",
    "neg_b2c_words",
    "neg_liquidated",
    "neg_non_cn_origin",
    "neg_state_body",
    "flag_competitor",
    "flag_supplier_cn",
)
DERIVED_KEYS = (
    "derived_fresh_7d",
    "derived_fresh_30d",
    "derived_fresh_90d",
    "derived_contact_mobile",
    "derived_contact_decision_maker",
    "derived_contact_dm_channel",
    "derived_corroboration",
)
WEIGHT_GROUPS = (
    "volume",
    "origin",
    "solidity",
    "recency",
    "contact",
    "corroboration",
    "negative",
    "gate",
)
CANDIDATE_STATES = (
    "found",
    "enriching",
    "scored",
    "review",
    "approved",
    "rejected",
    "pushed",
    "suppressed",
    "excluded",
    "hold",
)
CANDIDATE_OPEN_STATES = ("found", "enriching", "scored", "review", "approved", "hold")
CANDIDATE_TIERS = ("hot", "warm", "cold")
EXCLUDE_REASONS = (
    "competitor",
    "supplier_cn",
    "b2c",
    "liquidated",
    "state_body",
    "no_phone_after_retries",
    "owner",
    "merged",
    "foreign",
)
REVIEW_STATES = ("pending", "asked", "approved", "declined", "expired", "merged")
REVIEW_LIVE_STATES = ("pending", "asked")
REVIEW_KINDS = ("owner", "manager")
REVIEW_ANSWERS = (
    "push",
    "decline",
    "later",
    "dnc",
    "not_b2b",
    "competitor",
    "merge",
    "edit_phone",
    "handoff",
    "contacted",
    "said_no",
    "wrong_or_b2c",
    "no_reply",
)
PUSH_STATUSES = ("sending", "sent", "failed", "unknown")
PUSH_DELIVERED_STATUSES = ("sent", "unknown")
PUSH_SOURCE_KEYS = ("agent", "other")
LANDINGS = ("created", "joined", "client", "dropped")
STAGE_KINDS = ("open", "won", "lost")
CONTACT_KINDS = ("call", "telegram", "note", "stage", "followup")
OUTCOME_LABELS = (
    "won",
    "contacted_open",
    "open_untouched",
    "stale_open",
    "lost_no_contact",
    "lost_price",
    "lost_not_importer",
    "lost_small",
    "lost_wrong_number",
    "lost_competitor",
    "lost_other",
    "landed_client",
    "landed_joined",
    "dropped_capped",
    "dropped_suppressed",
    "dropped_other",
)
PRECISION_CLASSES = ("positive", "negative", "pending", "neutral")
SUPPRESSION_KINDS = (
    "client",
    "lost",
    "dnc",
    "bounced",
    "legal",
    "competitor",
    "manager_veto",
    "b2c",
    "cooldown",
)
SUPPRESSION_KEY_TYPES = (
    "phone_tail",
    "phone_hash",
    "tg_username",
    "tg_user_id",
    "instagram",
    "domain",
    "stir",
    "company_id",
)
SUPPRESSION_SOURCES = (
    "erp_feed",
    "outcomes",
    "owner_bot",
    "userbot_reply",
    "manual_import",
    "resolver",
    "retention",
)
SUPPRESSION_ERP_SOURCES = ("erp_feed", "outcomes", "manual_import")
CHAT_TYPES = ("private", "group", "channel")
CHAT_ROLES = ("market", "competitor", "buyers", "calibration", "other")
CHAT_STATUSES = ("queued", "joined", "gone", "left")
CHAT_DECIDED_BY = ("owner", "rule:seed", "rule:private_off", "rule:default")
MEDIA_KINDS = ("none", "photo", "document", "video", "other")
PROVIDERS = (
    "anthropic",
    "brave",
    "yandex_search",
    "google_places",
    "twogis",
    "apify",
    "proxy",
    "searxng",
    "hh",
    "other",
)
OPERATIONS = (
    "classify",
    "verdict",
    "company_verdict",
    "draft",
    "enrich_search",
    "serp_brave",
    "serp_yandex_night",
    "serp_searxng",
    "serp_ddg",
    "places_text_ids",
    "places_details_contact",
    "twogis_api",
    "apify_olx",
    "apify_2gis",
    "apify_instagram",
    "apify_uzum",
    "proxy_uzum",
    "model_reserve",
    "serp_reserve",
    "other",
)
JOB_KINDS = ("classify", "verdict", "company_verdict", "draft")
JOB_LANES = ("instant", "batch")
JOB_STATUSES = ("pending", "submitted", "applied", "failed", "fallback")
PURGE_SUBJECTS = ("company", "contact", "tg_user", "interaction_range", "retention")
PURGE_REASONS = ("legal_request", "dnc", "b2c", "retention", "owner", "merge_cleanup")
SHEET_TABS = ("Kompaniyalar", "Lidlar", "Yuborilganlar", "Manbalar")
SHEET_ENTITY_KINDS = ("company", "candidate", "push", "source")
SHEET_ROW_STATES = ("pending", "synced", "failed", "tombstone", "cleared")

# --- Frozen seeds -------------------------------------------------------------

# Version-1 weights (02 §2.7, 04 §5.1): (key, group_key, points, group_cap, decay_days).
WEIGHTS_V1 = (
    ("vol_container", "volume", 40, 40, 180),
    ("vol_stated_3plus", "volume", 30, 40, 180),
    ("vol_stated_1_3", "volume", 20, 40, 180),
    ("vol_wholesale_bulky", "volume", 20, 40, 365),
    ("vol_marketplace_velocity", "volume", 20, 40, 60),
    ("vol_vacancy_import", "volume", 15, 40, 180),
    ("vol_exhibitor", "volume", 15, 40, 365),
    ("vol_directory_wholesale", "volume", 10, 40, 365),
    ("cn_origin", "origin", 20, 20, 365),
    ("cn_sector_prior", "origin", 10, 20, None),
    ("ent_legal_confirmed", "solidity", 8, 15, None),
    ("ent_age_2y_active", "solidity", 4, 15, None),
    ("ent_not_micro", "solidity", 3, 15, None),
    ("derived_fresh_7d", "recency", 10, 10, None),
    ("derived_fresh_30d", "recency", 7, 10, None),
    ("derived_fresh_90d", "recency", 4, 10, None),
    ("derived_contact_mobile", "contact", 6, 10, None),
    ("derived_contact_decision_maker", "contact", 2, 10, None),
    ("derived_contact_dm_channel", "contact", 2, 10, None),
    ("derived_corroboration", "corroboration", 5, 5, None),
    ("neg_b2c_words", "negative", -40, 0, 180),
    ("neg_liquidated", "negative", -50, 0, None),
    ("neg_non_cn_origin", "negative", -20, 0, 365),
    ("neg_state_body", "negative", -20, 0, None),
    ("flag_competitor", "gate", 0, 0, None),
    ("flag_supplier_cn", "gate", 0, 0, None),
)

_ERP_SYNC_CONFIG = {
    "suppression": {"salt_id": None, "last_full_at": None, "last_delta_at": None, "count": 0},
    "outcomes": {"cursor": None, "last_at": None},
}

# 02 §2.1: (key, family, title_uz, enabled, cron, rps, daily_page_budget,
# daily_cost_cap_usd). cron/rps/pages/$ of the schedulable rows are 03 §3's; a
# row with several cadences carries its primary cron, the others live with the
# job. Attribution-only sub-sources are never scheduled: no cron, no budget.
_SOURCES = (
    ("telegram_monitor", "telegram", "Telegram guruh va kanallari", False, "*/15 * * * *", "0.3", 0, "0"),
    ("instagram_discovery", "instagram", "Instagram", False, "0 11 * * *", "1.0", 100, "0.40"),
    ("olx_business", "olx", "OLX sotuvchilari", False, "0 7 * * *", "0.33", 400, "0.60"),
    ("uzum_sellers", "marketplace", "Uzum doʻkonlari", False, "0 9 * * 5", "0.2", 300, "1.60"),
    ("directories", "directory", "Kataloglar", False, "0 23 * * 4", "0.5", 600, "0"),
    ("hh_vacancies", "vacancy", "hh.uz vakansiyalari", False, "20 */4 * * *", "1.0", 400, "0"),
    ("exhibitors", "exhibitor", "Koʻrgazmalar", False, "0 9 * * 1", "0.33", 100, "0"),
    ("maps", "maps", "Xaritalar", False, "0 10 * * 2", "2.0", 150, "1.40"),
    ("brand_dealers", "brand", "Brend dilerlari", False, "0 13 1-10 * *", "0.3", 120, "0"),
    ("serp_layer", "search", "Qidiruv tizimlari", False, "0 10,14 * * *", "0.3", 300, "1.00"),
    ("registry_lookup", "registry", "Davlat reyestrlari", False, None, "0.33", 150, "0"),
    ("erp_sync", "erp", "ERP (fullprompt)", False, "15 * * * *", "1.0", 50, "0"),
    ("manual", "manual", "Qoʻlda kiritilgan", False, None, "0.3", 0, "0"),
    ("enrich_site", "search", "Kompaniya saytlari", True, None, "0.3", 150, "0"),
    ("directories_goldenpages", "directory", "goldenpages.uz", False, None, "0.3", 0, "0"),
    ("directories_yellowpages", "directory", "yellowpages.uz", False, None, "0.3", 0, "0"),
    ("directories_glotr", "directory", "glotr.uz", False, None, "0.3", 0, "0"),
    ("directories_prom", "directory", "prom.uz", False, None, "0.3", 0, "0"),
    ("maps_2gis", "maps", "2GIS", False, None, "0.3", 0, "0"),
    ("maps_google", "maps", "Google Maps", False, None, "0.3", 0, "0"),
    ("search_brave", "search", "Brave qidiruvi", False, None, "0.3", 0, "0"),
    ("search_yandex", "search", "Yandex qidiruvi", False, None, "0.3", 0, "0"),
    ("search_searxng", "search", "SearXNG", False, None, "0.3", 0, "0"),
)

# --- Functions (02 §1, §2.16), in creation order --------------------------------

_FUNCTIONS = (
    (
        "cs_phone_tail(text)",
        r"""CREATE FUNCTION cs_phone_tail(raw text) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT CASE WHEN length(d) >= 9 THEN right(d, 9) END
  FROM (SELECT regexp_replace(raw, '[^0-9]', '', 'g') AS d) s $$""",
    ),
    # The prefix in either case, spelled as ASCII classes rather than the 'i'
    # flag, so that SQL and Python (services/keys.username_key) fold exactly
    # the same letters: 02 §1's case-sensitive strip turned «HTTPS://T.ME/x»,
    # a link to a real channel, into NULL.
    (
        "cs_username_key(text)",
        r"""CREATE FUNCTION cs_username_key(raw text) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT CASE WHEN u ~ '^[a-z0-9_]{5,32}$' AND u !~ 'bot$' THEN u END
  FROM (SELECT lower(regexp_replace(regexp_replace(trim(raw),
          '^([A-Za-z]+://)?([Ww][Ww][Ww]\.)?([Tt]\.[Mm][Ee]/|[Tt][Ee][Ll][Ee][Gg][Rr][Aa][Mm]\.[Mm][Ee]/)?@?',
          ''), '[/?#].*$', '')) AS u) s $$""",
    ),
    (
        "cs_domain_key(text)",
        r"""CREATE FUNCTION cs_domain_key(raw text) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT CASE WHEN h ~ '^[a-z0-9-]+(\.[a-z0-9-]+)+$'
          AND h NOT IN ('instagram.com','t.me','telegram.me','facebook.com','olx.uz','uzum.uz','youtube.com','wa.me')
         THEN h END
  FROM (SELECT regexp_replace(lower(regexp_replace(regexp_replace(trim(raw),
          '^[A-Za-z]+://', ''), '[/?#:].*$', '')), '^www\.', '') AS h) s $$""",
    ),
    (
        "cs_touch_updated_at()",
        """CREATE FUNCTION cs_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$""",
    ),
    (
        "cs_append_only()",
        """CREATE FUNCTION cs_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END $$""",
    ),
    # The whole row, not 02 §2.16's five weight columns: with those alone an
    # approved row could be un-approved, edited and re-approved, or moved to
    # another version, and every stored score names its version (02 §2.7).
    # approved_by/approved_at are set only while approved_at is still NULL.
    (
        "cs_scoring_weights_frozen()",
        """CREATE FUNCTION cs_scoring_weights_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'scoring_weights is append-only'; END IF;
  IF OLD.approved_at IS NOT NULL AND NEW IS DISTINCT FROM OLD
  THEN RAISE EXCEPTION 'approved scoring_weights version % is frozen', OLD.version; END IF;
  RETURN NEW;
END $$""",
    ),
    # Replaces 02's ck_lead_candidates_suppressed (see _lead_candidates): only
    # the step INTO 'suppressed' needs the pointer; the FK's own SET NULL,
    # when the suppression row is retired, leaves the state alone.
    (
        "cs_candidate_suppressed_by()",
        """CREATE FUNCTION cs_candidate_suppressed_by() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state = 'suppressed' AND NEW.suppressed_by IS NULL
     AND (TG_OP = 'INSERT' OR OLD.state IS DISTINCT FROM 'suppressed') THEN
    RAISE EXCEPTION USING
      ERRCODE = 'check_violation',
      TABLE = 'lead_candidates',
      CONSTRAINT = 'trg_lead_candidates_suppressed',
      MESSAGE = 'lead_candidates: a candidate enters state suppressed only with suppressed_by ("trg_lead_candidates_suppressed")';
  END IF;
  RETURN NEW;
END $$""",
    ),
)

# Tables in creation order (02 §5); downgrade drops them in reverse.
_TABLES = (
    "sources",
    "collector_runs",
    "evidence",
    "companies",
    "company_aliases",
    "company_ids",
    "contacts",
    "observations",
    "model_jobs",
    "signals",
    "scoring_weights",
    "suppression",
    "lead_candidates",
    "lead_reviews",
    "push_log",
    "outcomes",
    "interactions",
    "chat_monitors",
    "heartbeats",
    "usage_log",
    "reminder_log",
    "runtime_flags",
    "merge_log",
    "purge_log",
    "sheet_rows",
)
# The bot, userbot and worker write raw SQL too, so updated_at is the trigger's.
_TOUCHED = (
    "sources",
    "companies",
    "contacts",
    "lead_candidates",
    "chat_monitors",
    "sheet_rows",
    "runtime_flags",
)
# Retention nulls columns of these, never deletes rows.
_APPEND_ONLY = ("push_log", "outcomes", "merge_log", "purge_log", "usage_log")

_EXTERNAL_ID = "'^cs:[0-9a-f-]{36}:[0-9]{6}$'"
_NOW = sa.text("now()")
_ZERO = sa.text("0")
_EMPTY_JSONB = sa.text("'{}'::jsonb")
_EMPTY_ARRAY = sa.text("'{}'")
_UUID = sa.text("gen_random_uuid()")


class _SeedJsonb(sa.types.TypeDecorator[Any]):
    """JSONB that can also be written as a literal. ``alembic upgrade --sql``
    (offline, literal binds) has no literal renderer for JSONB and stopped at
    the sources seed; online, the value binds exactly as JSONB does."""

    impl = postgresql.JSONB
    cache_ok = True

    def process_literal_param(self, value: Any, dialect: sa.Dialect) -> str:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return "'" + text.replace("'", "''") + "'::jsonb"


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _in(table: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN ({_sql_list(values)})", name=f"ck_{table}_{column}")


def _ck(name: str, sqltext: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(sqltext, name=name)


def _fk(table: str, column: str, target: str, ondelete: str | None = None) -> sa.ForeignKey:
    return sa.ForeignKey(target, name=f"fk_{table}_{column}", ondelete=ondelete)


def _id(table: str) -> sa.Column:
    return sa.Column(
        "id", sa.BigInteger(), sa.Identity(always=True), primary_key=True, nullable=False
    )


def _ts(name: str, *, now: bool = False) -> sa.Column:
    if now:
        return sa.Column(name, sa.DateTime(timezone=True), server_default=_NOW, nullable=False)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _uuid(name: str, *args: object, nullable: bool = True, **kw: object) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, nullable=nullable, **kw)


def _int(name: str, type_: type = sa.Integer, *, default: str | None = "0") -> sa.Column:
    server_default = sa.text(default) if default is not None else None
    return sa.Column(name, type_(), server_default=server_default, nullable=default is None)


def _jsonb(name: str, *, empty: bool = False, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=_EMPTY_JSONB if empty else None,
        nullable=nullable,
    )


def _text(name: str, *, nullable: bool = True, default: str | None = None) -> sa.Column:
    return sa.Column(name, sa.Text(), server_default=default, nullable=nullable)


def _bool(name: str, default: bool) -> sa.Column:
    return sa.Column(
        name, sa.Boolean(), server_default=sa.true() if default else sa.false(), nullable=False
    )


def _pk(table: str, *columns: str) -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint(*columns, name=f"pk_{table}")


# --- Tables ---------------------------------------------------------------------


def _sources() -> None:
    op.create_table(
        "sources",
        _text("key", nullable=False),
        _text("family", nullable=False),
        _text("title_uz", nullable=False),
        _bool("enabled", False),
        _text("cron"),
        sa.Column("rps", sa.Numeric(6, 3), server_default=sa.text("0.3"), nullable=False),
        _int("daily_page_budget", default="500"),
        sa.Column("daily_cost_cap_usd", sa.Numeric(8, 4), server_default=_ZERO, nullable=False),
        _bool("auto_push", False),
        sa.Column("precision_30d", sa.Numeric(5, 4), nullable=True),
        _int("pushes_30d"),
        _int("positives_30d"),
        _ts("paused_until"),
        _text("paused_reason"),
        _ts("breaker_until"),
        _text("breaker_reason"),
        _ts("last_run_at"),
        _jsonb("config", empty=True),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        _pk("sources", "key"),
        _ck("ck_sources_key", "key ~ '^[a-z][a-z0-9_]{2,40}$'"),
        _in("sources", "family", SOURCE_FAMILIES),
        _ck("ck_sources_rps", "rps > 0 AND rps <= 5"),
        _ck("ck_sources_daily_page_budget", "daily_page_budget >= 0"),
        _ck("ck_sources_daily_cost_cap_usd", "daily_cost_cap_usd >= 0"),
        _ck("ck_sources_precision_30d", "precision_30d BETWEEN 0 AND 1"),
    )


def _collector_runs() -> None:
    t = "collector_runs"
    op.create_table(
        t,
        _id(t),
        sa.Column("source_key", sa.Text(), _fk(t, "source_key", "sources.key"), nullable=False),
        _ts("started_at", now=True),
        _ts("finished_at"),
        _text("status", nullable=False, default="running"),
        _int("requests"),
        _int("pages"),
        _int("http_403"),
        _int("http_429"),
        _int("new_evidence"),
        _int("new_observations"),
        _int("new_companies"),
        _int("new_signals"),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default=_ZERO, nullable=False),
        _text("error"),
        _jsonb("meta", empty=True),
        _pk(t, "id"),
        _in(t, "status", RUN_STATUSES),
        _ck("ck_collector_runs_finished", "finished_at IS NULL OR finished_at >= started_at"),
    )
    op.create_index(
        "ix_collector_runs_source_started", t, ["source_key", sa.text("started_at DESC")]
    )
    op.create_index(
        "ux_collector_runs_one_running",
        t,
        ["source_key"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def _evidence() -> None:
    t = "evidence"
    op.create_table(
        t,
        _id(t),
        sa.Column("source_key", sa.Text(), _fk(t, "source_key", "sources.key"), nullable=False),
        sa.Column(
            "collector_run_id",
            sa.BigInteger(),
            _fk(t, "collector_run_id", "collector_runs.id", "SET NULL"),
            nullable=True,
        ),
        _text("url", nullable=False),
        _text("source_ref"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        _ts("published_at"),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        _text("content_type", nullable=False, default="text/html"),
        sa.Column("bytes", sa.Integer(), nullable=False),
        _text("raw_path"),
        _text("excerpt", nullable=False),
        _text("title"),
        sa.Column("lang", sa.CHAR(2), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _ts("raw_deleted_at"),
        _jsonb("meta", empty=True),
        _ts("created_at", now=True),
        _pk(t, "id"),
        _ck("ck_evidence_url", "length(url) <= 2048"),
        _ck("ck_evidence_sha256", "sha256 ~ '^[0-9a-f]{64}$'"),
        _ck("ck_evidence_bytes", "bytes >= 0"),
        _ck("ck_evidence_excerpt", "length(excerpt) <= 600"),
        _ck("ck_evidence_title", "length(title) <= 300"),
    )
    op.create_index("ux_evidence_url_sha", t, [sa.text("md5(url)"), "sha256"], unique=True)
    op.create_index("ix_evidence_source_fetched", t, ["source_key", sa.text("fetched_at DESC")])
    op.create_index("ix_evidence_sha", t, ["sha256"])
    op.create_index(
        "ix_evidence_ref",
        t,
        ["source_key", "source_ref"],
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )
    op.create_index(
        "ix_evidence_expiry", t, ["expires_at"], postgresql_where=sa.text("raw_path IS NOT NULL")
    )
    op.create_index("ix_evidence_collector_run", t, ["collector_run_id"])


def _companies() -> None:
    t = "companies"
    op.create_table(
        t,
        _uuid("id", primary_key=True, server_default=_UUID, nullable=False),
        _text("display_name", nullable=False),
        _text("name_key", nullable=False),
        _text("legal_form"),
        sa.Column("stir", sa.CHAR(9), nullable=True),
        sa.Column("oked", sa.CHAR(5), nullable=True),
        _text("city"),
        _text("region"),
        _text("address"),
        _text("website_domain"),
        _text("telegram_channel"),
        _text("instagram"),
        _text("registry_status", nullable=False, default="unknown"),
        sa.Column("registered_on", sa.Date(), nullable=True),
        sa.Column(
            "sectors", postgresql.ARRAY(sa.Text()), server_default=_EMPTY_ARRAY, nullable=False
        ),
        sa.Column(
            "goods", postgresql.ARRAY(sa.Text()), server_default=_EMPTY_ARRAY, nullable=False
        ),
        sa.Column("segment", sa.CHAR(1), nullable=True),
        _text("volume_band", nullable=False, default="unknown"),
        _bool("is_competitor", False),
        _bool("is_supplier_cn", False),
        _bool("is_state_body", False),
        _text("state", nullable=False, default="new"),
        _text("exclude_reason"),
        # RESTRICT, not 02's SET NULL: ck_companies_merged spans this column, and
        # SET NULL beside a CHECK on the FK column makes the delete fail on the
        # CHECK instead (fullprompt #809). A company is never deleted (purge sets
        # state='purged') and a merge target is pinned by merge_log anyway.
        _uuid("merged_into", _fk(t, "merged_into", "companies.id", "RESTRICT")),
        _uuid("dup_of", _fk(t, "dup_of", "companies.id", "SET NULL")),
        _int("dup_score", sa.SmallInteger, default=None),
        _jsonb("verdict", nullable=True),
        _ts("verdict_at"),
        _ts("first_seen_at", now=True),
        _ts("last_signal_at"),
        _int("best_score", sa.SmallInteger, default=None),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        _pk(t, "id"),
        _ck("ck_companies_display_name", "length(display_name) BETWEEN 2 AND 160"),
        _ck("ck_companies_name_key", "length(name_key) BETWEEN 1 AND 160"),
        _in(t, "legal_form", LEGAL_FORMS),
        _ck("ck_companies_stir", "stir ~ '^[0-9]{9}$'"),
        _ck("ck_companies_oked", "oked ~ '^[0-9]{5}$'"),
        _ck("ck_companies_address", "length(address) <= 300"),
        # «Stored only as its key»: IS NOT DISTINCT FROM, not «=», because the
        # key functions answer NULL for what they refuse, and a CHECK reads the
        # NULL of «x = NULL» as passed. A NULL column stays legal.
        _ck(
            "ck_companies_website_domain",
            "website_domain IS NOT DISTINCT FROM cs_domain_key(website_domain)",
        ),
        _ck(
            "ck_companies_telegram_channel",
            "telegram_channel IS NOT DISTINCT FROM cs_username_key(telegram_channel)",
        ),
        _ck("ck_companies_instagram", "instagram = lower(instagram)"),
        _in(t, "registry_status", REGISTRY_STATUSES),
        _ck("ck_companies_segment", "segment ~ '^[A-Z]$'"),
        _in(t, "volume_band", VOLUME_BANDS),
        _in(t, "state", COMPANY_STATES),
        _ck("ck_companies_dup_score", "dup_score BETWEEN 0 AND 100"),
        _ck("ck_companies_best_score", "best_score BETWEEN 0 AND 100"),
        _ck("ck_companies_merged", "(state = 'merged') = (merged_into IS NOT NULL)"),
        _ck("ck_companies_excluded", "state <> 'excluded' OR exclude_reason IS NOT NULL"),
    )
    op.create_index(
        "ux_companies_stir",
        t,
        ["stir"],
        unique=True,
        postgresql_where=sa.text("stir IS NOT NULL AND state <> 'merged'"),
    )
    op.create_index(
        "ux_companies_domain",
        t,
        ["website_domain"],
        unique=True,
        postgresql_where=sa.text("website_domain IS NOT NULL AND state <> 'merged'"),
    )
    op.create_index(
        "ix_companies_name_key_trgm",
        t,
        ["name_key"],
        postgresql_using="gin",
        postgresql_ops={"name_key": "gin_trgm_ops"},
    )
    op.create_index("ix_companies_state_signal", t, ["state", sa.text("last_signal_at DESC")])
    op.create_index("ix_companies_merged_into", t, ["merged_into"])
    op.create_index(
        "ix_companies_dup_of", t, ["dup_of"], postgresql_where=sa.text("dup_of IS NOT NULL")
    )


def _company_aliases() -> None:
    t = "company_aliases"
    op.create_table(
        t,
        _id(t),
        _uuid(
            "company_id", _fk(t, "company_id", "companies.id", "CASCADE"), nullable=False
        ),
        _text("alias", nullable=False),
        _text("alias_key", nullable=False),
        sa.Column("source_key", sa.Text(), _fk(t, "source_key", "sources.key"), nullable=True),
        _ts("created_at", now=True),
        _pk(t, "id"),
        _ck("ck_company_aliases_alias", "length(alias) BETWEEN 2 AND 160"),
        sa.UniqueConstraint("company_id", "alias_key", name="uq_company_aliases_company_key"),
    )
    op.create_index(
        "ix_company_aliases_key_trgm",
        t,
        ["alias_key"],
        postgresql_using="gin",
        postgresql_ops={"alias_key": "gin_trgm_ops"},
    )
    op.create_index("ix_company_aliases_key", t, ["alias_key"])


def _company_ids() -> None:
    t = "company_ids"
    op.create_table(
        t,
        _uuid(
            "company_id", _fk(t, "company_id", "companies.id", "CASCADE"), nullable=False
        ),
        _text("id_type", nullable=False),
        _text("id_value", nullable=False),
        sa.Column("source_key", sa.Text(), _fk(t, "source_key", "sources.key"), nullable=True),
        _ts("created_at", now=True),
        _pk(t, "id_type", "id_value"),
        _in(t, "id_type", ID_TYPES),
        _ck("ck_company_ids_id_value", "length(id_value) BETWEEN 1 AND 200"),
    )
    op.create_index("ix_company_ids_company", t, ["company_id"])


def _contacts() -> None:
    t = "contacts"
    op.create_table(
        t,
        _uuid("id", primary_key=True, server_default=_UUID, nullable=False),
        _uuid(
            "company_id", _fk(t, "company_id", "companies.id", "CASCADE"), nullable=False
        ),
        _text("tier", nullable=False),
        _text("full_name"),
        _text("role"),
        _text("phone"),
        sa.Column(
            "phone_tail",
            sa.CHAR(9),
            sa.Computed("cs_phone_tail(phone)", persisted=True),
            nullable=True,
        ),
        _text("phone_kind"),
        _text("telegram_username"),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        _text("instagram"),
        _text("email"),
        _bool("is_decision_maker", False),
        _bool("shared_phone", False),
        _text("legal_basis"),
        _text("source_url"),
        sa.Column("source_key", sa.Text(), _fk(t, "source_key", "sources.key"), nullable=True),
        _ts("collected_at"),
        _ts("verified_at"),
        _ts("anonymised_at"),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        _pk(t, "id"),
        _in(t, "tier", CONTACT_TIERS),
        _ck("ck_contacts_full_name", "length(full_name) <= 120"),
        _ck("ck_contacts_role", "length(role) <= 80"),
        _ck("ck_contacts_phone", r"phone ~ '^\+[1-9][0-9]{6,14}$'"),
        # ck_contacts_phone_kind is the pairing rule below; the value list
        # takes its own suffix.
        _ck("ck_contacts_phone_kind_values", f"phone_kind IN ({_sql_list(PHONE_KINDS)})"),
        _ck(
            "ck_contacts_telegram_username",
            "telegram_username IS NOT DISTINCT FROM cs_username_key(telegram_username)",
        ),
        _ck("ck_contacts_instagram", "instagram = lower(instagram)"),
        _ck("ck_contacts_email", "email ~ '^[^@[:space:]]+@[^@[:space:]]+$'"),
        _in(t, "legal_basis", LEGAL_BASES),
        _ck("ck_contacts_source_url", "length(source_url) <= 2048"),
        _ck(
            "ck_contacts_person_basis",
            "tier = 'company' OR anonymised_at IS NOT NULL OR (legal_basis IS NOT NULL "
            "AND source_url IS NOT NULL AND collected_at IS NOT NULL)",
        ),
        _ck(
            "ck_contacts_company_no_name",
            "tier = 'person' OR (full_name IS NULL AND role IS NULL AND telegram_id IS NULL)",
        ),
        _ck("ck_contacts_phone_kind", "(phone IS NULL) = (phone_kind IS NULL)"),
        _ck(
            "ck_contacts_no_pinfl",
            "coalesce(full_name, '') !~ '[0-9]{14}' AND coalesce(role, '') !~ '[0-9]{14}'",
        ),
    )
    op.create_index(
        "ux_contacts_one_person",
        t,
        ["company_id"],
        unique=True,
        postgresql_where=sa.text("tier = 'person' AND anonymised_at IS NULL"),
    )
    op.create_index(
        "ux_contacts_company_phone",
        t,
        ["company_id", "phone"],
        unique=True,
        postgresql_where=sa.text("phone IS NOT NULL"),
    )
    op.create_index("ix_contacts_company", t, ["company_id"])
    op.create_index(
        "ix_contacts_phone_tail",
        t,
        ["phone_tail"],
        postgresql_where=sa.text("phone_tail IS NOT NULL"),
    )
    op.create_index(
        "ix_contacts_tg_username",
        t,
        ["telegram_username"],
        postgresql_where=sa.text("telegram_username IS NOT NULL"),
    )
    op.create_index(
        "ix_contacts_tg_id", t, ["telegram_id"], postgresql_where=sa.text("telegram_id IS NOT NULL")
    )
    op.create_index(
        "ix_contacts_instagram", t, ["instagram"], postgresql_where=sa.text("instagram IS NOT NULL")
    )


def _observations() -> None:
    t = "observations"
    op.create_table(
        t,
        _id(t),
        sa.Column(
            "evidence_id",
            sa.BigInteger(),
            _fk(t, "evidence_id", "evidence.id", "CASCADE"),
            nullable=False,
        ),
        _int("ordinal", sa.SmallInteger),
        sa.Column("source_key", sa.Text(), _fk(t, "source_key", "sources.key"), nullable=False),
        _text("status", nullable=False, default="new"),
        _jsonb("parsed"),
        _int("parsed_version", sa.SmallInteger, default="1"),
        _uuid("company_id", _fk(t, "company_id", "companies.id", "SET NULL")),
        _uuid("contact_id", _fk(t, "contact_id", "contacts.id", "SET NULL")),
        _text("reject_reason"),
        _ts("resolved_at"),
        _ts("scrubbed_at"),
        _ts("created_at", now=True),
        _pk(t, "id"),
        _ck("ck_observations_ordinal", "ordinal >= 0"),
        _in(t, "status", OBSERVATION_STATUSES),
        sa.UniqueConstraint("evidence_id", "ordinal", name="uq_observations_evidence_ordinal"),
    )
    op.create_index(
        "ix_observations_queue",
        t,
        ["created_at"],
        postgresql_where=sa.text("status IN ('new', 'needs_review')"),
    )
    op.create_index("ix_observations_company", t, ["company_id"])
    op.create_index("ix_observations_contact", t, ["contact_id"])
    op.create_index("ix_observations_name_key", t, [sa.text("(parsed ->> 'name_key')")])


def _model_jobs() -> None:
    t = "model_jobs"
    op.create_table(
        t,
        _id(t),
        _text("kind", nullable=False),
        _text("lane", nullable=False),
        _text("model", nullable=False),
        _text("custom_id", nullable=False),
        _text("batch_id"),
        _text("status", nullable=False, default="pending"),
        _int("attempts", sa.SmallInteger),
        _uuid("company_id", _fk(t, "company_id", "companies.id", "CASCADE")),
        sa.Column(
            "evidence_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=_EMPTY_ARRAY,
            nullable=False,
        ),
        _jsonb("request", nullable=True),
        _jsonb("pseudonyms", nullable=True),
        _jsonb("result", nullable=True),
        _text("error"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        _ts("submitted_at"),
        _ts("applied_at"),
        _ts("created_at", now=True),
        _pk(t, "id"),
        _in(t, "kind", JOB_KINDS),
        _in(t, "lane", JOB_LANES),
        _in(t, "status", JOB_STATUSES),
        _ck("ck_model_jobs_attempts", "attempts BETWEEN 0 AND 3"),
        sa.UniqueConstraint("custom_id", name="uq_model_jobs_custom_id"),
    )
    op.create_index("ix_model_jobs_status_created", t, ["status", "created_at"])
    op.create_index(
        "ix_model_jobs_batch", t, ["batch_id"], postgresql_where=sa.text("batch_id IS NOT NULL")
    )
    op.create_index("ix_model_jobs_company", t, ["company_id"])


def _signals() -> None:
    t = "signals"
    op.create_table(
        t,
        _id(t),
        _uuid("company_id", _fk(t, "company_id", "companies.id", "CASCADE"), nullable=False),
        _uuid("contact_id", _fk(t, "contact_id", "contacts.id", "SET NULL")),
        sa.Column(
            "evidence_id",
            sa.BigInteger(),
            _fk(t, "evidence_id", "evidence.id", "CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "observation_id",
            sa.BigInteger(),
            _fk(t, "observation_id", "observations.id", "SET NULL"),
            nullable=True,
        ),
        _text("key", nullable=False),
        sa.Column("value", sa.Numeric(12, 3), nullable=True),
        _text("quote"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        _ts("expires_at"),
        sa.Column("source_key", sa.Text(), _fk(t, "source_key", "sources.key"), nullable=False),
        _text("model"),
        sa.Column(
            "model_job_id",
            sa.BigInteger(),
            _fk(t, "model_job_id", "model_jobs.id", "SET NULL"),
            nullable=True,
        ),
        _ts("created_at", now=True),
        _pk(t, "id"),
        _in(t, "key", SIGNAL_KEYS),
        _ck("ck_signals_quote", "length(quote) <= 300"),
        sa.UniqueConstraint(
            "company_id", "key", "evidence_id", name="uq_signals_company_key_evidence"
        ),
    )
    op.create_index("ix_signals_company_key", t, ["company_id", "key"])
    op.create_index("ix_signals_observed", t, [sa.text("observed_at DESC")])
    op.create_index("ix_signals_evidence", t, ["evidence_id"])
    op.create_index("ix_signals_contact", t, ["contact_id"])
    op.create_index("ix_signals_observation", t, ["observation_id"])
    op.create_index("ix_signals_model_job", t, ["model_job_id"])


def _scoring_weights() -> None:
    t = "scoring_weights"
    op.create_table(
        t,
        sa.Column("version", sa.Integer(), nullable=False),
        _text("key", nullable=False),
        _text("group_key", nullable=False),
        sa.Column("points", sa.SmallInteger(), nullable=False),
        sa.Column("group_cap", sa.SmallInteger(), nullable=False),
        _int("decay_days", default=None),
        sa.Column(
            "decayed_factor", sa.Numeric(3, 2), server_default=sa.text("0.5"), nullable=False
        ),
        _text("note"),
        _text("proposed_by", nullable=False),
        _ts("created_at", now=True),
        _text("approved_by"),
        _ts("approved_at"),
        _pk(t, "version", "key"),
        _ck("ck_scoring_weights_version", "version >= 1"),
        _in(t, "group_key", WEIGHT_GROUPS),
        _ck("ck_scoring_weights_points", "points BETWEEN -100 AND 100"),
        _ck("ck_scoring_weights_group_cap", "group_cap BETWEEN 0 AND 100"),
        _ck("ck_scoring_weights_decay_days", "decay_days > 0"),
        _ck("ck_scoring_weights_decayed_factor", "decayed_factor BETWEEN 0 AND 1"),
        _in(t, "key", SIGNAL_KEYS + DERIVED_KEYS),
        # Not in 02 §2.7: approval is one act with a name, and the freeze
        # trigger makes it permanent, so a half-set approval would stay
        # anonymous for ever.
        _ck("ck_scoring_weights_approved", "(approved_by IS NULL) = (approved_at IS NULL)"),
    )
    op.create_index(
        "ix_scoring_weights_approved",
        t,
        ["version"],
        postgresql_where=sa.text("approved_at IS NOT NULL"),
    )


def _suppression() -> None:
    t = "suppression"
    op.create_table(
        t,
        _id(t),
        _text("kind", nullable=False),
        _text("key_type", nullable=False),
        _text("key", nullable=False),
        sa.Column("salt_id", sa.CHAR(8), nullable=True),
        _text("reason"),
        _text("source", nullable=False),
        _ts("erp_at"),
        _ts("created_at", now=True),
        _ts("last_seen_at", now=True),
        _ts("expires_at"),
        _pk(t, "id"),
        _in(t, "kind", SUPPRESSION_KINDS),
        _in(t, "key_type", SUPPRESSION_KEY_TYPES),
        _ck("ck_suppression_key", "length(key) BETWEEN 3 AND 200"),
        _ck("ck_suppression_reason", "length(reason) <= 200"),
        _in(t, "source", SUPPRESSION_SOURCES),
        _ck("ck_suppression_phone_tail", "key_type <> 'phone_tail' OR key ~ '^[0-9]{9}$'"),
        _ck(
            "ck_suppression_phone_hash",
            "key_type <> 'phone_hash' OR (key ~ '^[0-9a-f]{20}$' AND salt_id IS NOT NULL)",
        ),
        _ck("ck_suppression_stir", "key_type <> 'stir' OR key ~ '^[0-9]{9}$'"),
        _ck(
            "ck_suppression_username",
            "key_type <> 'tg_username' OR key IS NOT DISTINCT FROM cs_username_key(key)",
        ),
        _ck(
            "ck_suppression_domain",
            "key_type <> 'domain' OR key IS NOT DISTINCT FROM cs_domain_key(key)",
        ),
        _ck(
            "ck_suppression_erp_kinds",
            "kind NOT IN ('client', 'lost') "
            f"OR source IN ({_sql_list(SUPPRESSION_ERP_SOURCES)})",
        ),
        _ck("ck_suppression_cooldown", "(kind = 'cooldown') = (expires_at IS NOT NULL)"),
        sa.UniqueConstraint("kind", "key_type", "key", name="uq_suppression_key"),
    )
    op.create_index("ix_suppression_lookup", t, ["key_type", "key"])
    op.create_index(
        "ix_suppression_feed_seen",
        t,
        ["last_seen_at"],
        postgresql_where=sa.text("source = 'erp_feed'"),
    )
    op.create_index(
        "ix_suppression_expiry",
        t,
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )


def _lead_candidates() -> None:
    t = "lead_candidates"
    op.create_table(
        t,
        _uuid("id", primary_key=True, server_default=_UUID, nullable=False),
        _uuid("company_id", _fk(t, "company_id", "companies.id", "CASCADE"), nullable=False),
        _uuid("contact_id", _fk(t, "contact_id", "contacts.id", "SET NULL")),
        _text("state", nullable=False, default="found"),
        _text("tier"),
        _int("score", sa.SmallInteger, default=None),
        _jsonb("score_breakdown", nullable=True),
        _int("weights_version", default=None),
        sa.Column(
            "primary_source_key",
            sa.Text(),
            _fk(t, "primary_source_key", "sources.key"),
            nullable=True,
        ),
        _text("exclude_reason"),
        sa.Column(
            "suppressed_by",
            sa.BigInteger(),
            _fk(t, "suppressed_by", "suppression.id", "SET NULL"),
            nullable=True,
        ),
        _ts("hold_until"),
        _int("enrich_attempts", sa.SmallInteger),
        _ts("next_enrich_at"),
        _text("external_id"),
        _text("approved_by"),
        _ts("approved_at"),
        _ts("rejected_at"),
        _ts("pushed_at"),
        _ts("last_scored_at"),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        _pk(t, "id"),
        _in(t, "state", CANDIDATE_STATES),
        _in(t, "tier", CANDIDATE_TIERS),
        _ck("ck_lead_candidates_score", "score BETWEEN 0 AND 100"),
        _in(t, "exclude_reason", EXCLUDE_REASONS),
        _ck("ck_lead_candidates_enrich_attempts", "enrich_attempts BETWEEN 0 AND 3"),
        _ck("ck_lead_candidates_external_id", f"external_id ~ {_EXTERNAL_ID}"),
        _ck("ck_lead_candidates_excluded", "state <> 'excluded' OR exclude_reason IS NOT NULL"),
        _ck(
            "ck_lead_candidates_pushed",
            "state <> 'pushed' OR (external_id IS NOT NULL AND pushed_at IS NOT NULL)",
        ),
        _ck(
            "ck_lead_candidates_approved",
            "state NOT IN ('approved', 'pushed') "
            "OR (approved_at IS NOT NULL AND approved_by IS NOT NULL)",
        ),
        _ck(
            "ck_lead_candidates_scored",
            "state NOT IN ('scored', 'review', 'approved', 'pushed') OR score IS NOT NULL",
        ),
        # No ck_lead_candidates_suppressed: 02's CHECK spans suppressed_by, which
        # is ON DELETE SET NULL, so retiring a suppression row a candidate points
        # at failed the whole DELETE on the CHECK (fullprompt #809) — and R6, the
        # ERP full pull, the manual_import retirement and the outcomes
        # retractions all retire rows. The pointer must be able to go («after
        # suppressed — the suppression row is gone», 02 §2.9), so the rule is
        # trg_lead_candidates_suppressed's: a candidate ENTERS 'suppressed' only
        # naming its row (_triggers below).
    )
    op.create_index(
        "ux_lead_candidates_open",
        t,
        ["company_id"],
        unique=True,
        postgresql_where=sa.text(f"state IN ({_sql_list(CANDIDATE_OPEN_STATES)})"),
    )
    op.create_index(
        "ux_lead_candidates_external",
        t,
        ["external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_index("ix_lead_candidates_state_score", t, ["state", sa.text("score DESC")])
    op.create_index(
        "ix_lead_candidates_enrich_due",
        t,
        ["next_enrich_at"],
        postgresql_where=sa.text("state = 'enriching'"),
    )
    op.create_index(
        "ix_lead_candidates_hold_until",
        t,
        ["hold_until"],
        postgresql_where=sa.text("state = 'hold'"),
    )
    op.create_index("ix_lead_candidates_company", t, ["company_id"])
    op.create_index("ix_lead_candidates_contact", t, ["contact_id"])
    op.create_index("ix_lead_candidates_suppressed", t, ["suppressed_by"])


def _lead_reviews() -> None:
    t = "lead_reviews"
    op.create_table(
        t,
        _id(t),
        _uuid(
            "candidate_id",
            _fk(t, "candidate_id", "lead_candidates.id", "CASCADE"),
            nullable=False,
        ),
        _text("kind", nullable=False, default="owner"),
        _text("state", nullable=False, default="pending"),
        sa.Column("card_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("card_message_id", sa.BigInteger(), nullable=True),
        sa.Column("card_hash", sa.CHAR(64), nullable=True),
        _ts("asked_at"),
        _ts("answered_at"),
        _text("answer"),
        sa.Column("by_telegram_id", sa.BigInteger(), nullable=True),
        _text("note"),
        _ts("expires_at"),
        _ts("created_at", now=True),
        _pk(t, "id"),
        _in(t, "kind", REVIEW_KINDS),
        _in(t, "state", REVIEW_STATES),
        _in(t, "answer", REVIEW_ANSWERS),
        _ck("ck_lead_reviews_note", "length(note) <= 500"),
        _ck(
            "ck_lead_reviews_answered",
            "(state IN ('approved', 'declined', 'merged')) = (answered_at IS NOT NULL)",
        ),
        _ck(
            "ck_lead_reviews_asked",
            "state <> 'asked' OR (asked_at IS NOT NULL AND card_message_id IS NOT NULL "
            "AND expires_at IS NOT NULL)",
        ),
    )
    op.create_index(
        "ux_lead_reviews_live",
        t,
        ["candidate_id", "kind"],
        unique=True,
        postgresql_where=sa.text(f"state IN ({_sql_list(REVIEW_LIVE_STATES)})"),
    )
    op.create_index(
        "ux_lead_reviews_card",
        t,
        ["card_chat_id", "card_message_id"],
        unique=True,
        postgresql_where=sa.text("card_message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_lead_reviews_expiry", t, ["expires_at"], postgresql_where=sa.text("state = 'asked'")
    )
    op.create_index("ix_lead_reviews_candidate", t, ["candidate_id"])


def _push_log() -> None:
    t = "push_log"
    delivered = f"status IN ({_sql_list(PUSH_DELIVERED_STATUSES)})"
    op.create_table(
        t,
        _id(t),
        _text("external_id", nullable=False),
        sa.Column("attempt", sa.SmallInteger(), nullable=False),
        # RESTRICT on purpose: the ledger keeps its candidate, so a candidate
        # with push history is never deleted. 02 §4 R6's hold sweep must skip
        # them (AND NOT EXISTS push_log), or one such row aborts the whole
        # DELETE; tests/test_constraints.py holds the statement R6 must use.
        _uuid(
            "candidate_id",
            _fk(t, "candidate_id", "lead_candidates.id", "RESTRICT"),
            nullable=False,
        ),
        _uuid("company_id", _fk(t, "company_id", "companies.id", "RESTRICT"), nullable=False),
        sa.Column("phone_tail", sa.CHAR(9), nullable=False),
        _text("endpoint", nullable=False),
        _text("source_key_sent", nullable=False),
        _jsonb("payload", nullable=True),
        sa.Column("payload_sha256", sa.CHAR(64), nullable=False),
        _text("status", nullable=False, default="sending"),
        _int("http_status", sa.SmallInteger, default=None),
        _text("error"),
        _ts("sent_at", now=True),
        _ts("finished_at"),
        _pk(t, "id"),
        _ck("ck_push_log_external_id", f"external_id ~ {_EXTERNAL_ID}"),
        _ck("ck_push_log_attempt", "attempt BETWEEN 1 AND 3"),
        _ck("ck_push_log_phone_tail", "phone_tail ~ '^[0-9]{9}$'"),
        _in(t, "source_key_sent", PUSH_SOURCE_KEYS),
        _in(t, "status", PUSH_STATUSES),
        sa.UniqueConstraint("external_id", "attempt", name="uq_push_log_external_attempt"),
        # sent ⇒ 2xx or no answer at all, and 2xx ⇒ sent | unknown. The NULL
        # passes on purpose (02 §2.10 rule 5b): an `unknown` row the outcomes
        # walk confirms becomes `sent` with no HTTP status ever received.
        _ck(
            "ck_push_log_sent",
            "(status <> 'sent' OR http_status BETWEEN 200 AND 299) "
            "AND (http_status IS NULL OR http_status NOT BETWEEN 200 AND 299 "
            "OR status IN ('sent', 'unknown'))",
        ),
    )
    op.create_index(
        "ux_push_log_delivered",
        t,
        ["external_id"],
        unique=True,
        postgresql_where=sa.text(delivered),
    )
    op.create_index("ix_push_log_phone_sent", t, ["phone_tail", sa.text("sent_at DESC")])
    op.create_index("ix_push_log_company_sent", t, ["company_id", sa.text("sent_at DESC")])
    op.create_index("ix_push_log_day", t, ["sent_at"], postgresql_where=sa.text(delivered))
    op.create_index("ix_push_log_candidate", t, ["candidate_id"])


def _outcomes() -> None:
    t = "outcomes"
    op.create_table(
        t,
        _text("external_id", nullable=False),
        _uuid("intake_id", nullable=False),
        _uuid("candidate_id", _fk(t, "candidate_id", "lead_candidates.id", "SET NULL")),
        _uuid("company_id", _fk(t, "company_id", "companies.id", "SET NULL")),
        sa.Column("source_key", sa.Text(), _fk(t, "source_key", "sources.key"), nullable=True),
        _text("landing", nullable=False),
        _text("landing_reason"),
        _uuid("lead_id"),
        _uuid("client_id"),
        _text("stage_kind"),
        _text("stage_name"),
        _text("lost_reason"),
        _ts("closed_at"),
        _ts("contacted_at"),
        _text("contact_kind"),
        sa.Column("quoted_volume_m3", sa.Numeric(12, 3), nullable=True),
        sa.Column("erp_at", sa.DateTime(timezone=True), nullable=False),
        _text("label", nullable=False),
        _text("precision_class", nullable=False),
        _ts("label_changed_at", now=True),
        _ts("first_read_at", now=True),
        _ts("read_at", now=True),
        _jsonb("raw"),
        _pk(t, "external_id"),
        _in(t, "landing", LANDINGS),
        _in(t, "stage_kind", STAGE_KINDS),
        _in(t, "contact_kind", CONTACT_KINDS),
        _in(t, "label", OUTCOME_LABELS),
        _in(t, "precision_class", PRECISION_CLASSES),
    )
    op.create_index("ix_outcomes_source_first_read", t, ["source_key", "first_read_at"])
    op.create_index("ix_outcomes_company", t, ["company_id"])
    op.create_index("ix_outcomes_label", t, ["label"])
    op.create_index("ix_outcomes_candidate", t, ["candidate_id"])


def _interactions() -> None:
    t = "interactions"
    op.create_table(
        t,
        _id(t),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=True),
        _text("sender_username"),
        _text("sender_name"),
        _text("sender_bio"),
        _text("text"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        _text("media_kind", nullable=False, default="none"),
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
        sa.Column("fwd_from_peer_id", sa.BigInteger(), nullable=True),
        sa.Column("prefilter_hit", postgresql.ARRAY(sa.Text()), nullable=False),
        _ts("processed_at"),
        sa.Column(
            "evidence_id",
            sa.BigInteger(),
            _fk(t, "evidence_id", "evidence.id", "SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "observation_id",
            sa.BigInteger(),
            _fk(t, "observation_id", "observations.id", "SET NULL"),
            nullable=True,
        ),
        _ts("text_purged_at"),
        _ts("created_at", now=True),
        _pk(t, "id"),
        _ck(
            "ck_interactions_sender_username",
            "sender_username IS NOT DISTINCT FROM cs_username_key(sender_username)",
        ),
        _ck("ck_interactions_sender_name", "length(sender_name) <= 120"),
        _ck("ck_interactions_sender_bio", "length(sender_bio) <= 300"),
        _ck("ck_interactions_text", "length(text) <= 4000"),
        _in(t, "media_kind", MEDIA_KINDS),
        _ck("ck_interactions_prefilter_nonempty", "cardinality(prefilter_hit) > 0"),
        sa.UniqueConstraint("tg_chat_id", "tg_message_id", name="uq_interactions_tg_message"),
    )
    op.create_index(
        "ix_interactions_unprocessed",
        t,
        ["created_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )
    op.create_index("ix_interactions_sender", t, ["sender_id", sa.text("occurred_at DESC")])
    op.create_index(
        "ix_interactions_chat_occurred", t, ["tg_chat_id", sa.text("occurred_at DESC")]
    )
    op.create_index(
        "ix_interactions_text_retention",
        t,
        ["occurred_at"],
        postgresql_where=sa.text("text IS NOT NULL"),
    )
    op.create_index("ix_interactions_evidence", t, ["evidence_id"])
    op.create_index("ix_interactions_observation", t, ["observation_id"])


def _chat_monitors() -> None:
    t = "chat_monitors"
    op.create_table(
        t,
        sa.Column(
            "id", sa.Integer(), sa.Identity(always=True), primary_key=True, nullable=False
        ),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
        _text("chat_type", nullable=False),
        _text("title"),
        _text("username"),
        _text("role", nullable=False, default="market"),
        _text("status", nullable=False, default="queued"),
        _int("priority", sa.SmallInteger, default="5"),
        _bool("monitor_enabled", True),
        _text("decided_by"),
        _ts("asked_at"),
        _ts("joined_at"),
        _ts("backfill_requested_at"),
        _ts("backfilled_at"),
        _int("backfill_attempts", sa.SmallInteger),
        _ts("monitoring_since"),
        sa.Column("last_seen_message_id", sa.BigInteger(), nullable=True),
        _ts("last_seen_at"),
        _int("seen_count"),
        _int("hit_count"),
        _int("member_count", default=None),
        _ts("flood_until"),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        _pk(t, "id"),
        sa.UniqueConstraint("tg_chat_id", name="uq_chat_monitors_tg_chat_id"),
        _in(t, "chat_type", CHAT_TYPES),
        _in(t, "role", CHAT_ROLES),
        _in(t, "status", CHAT_STATUSES),
        _ck("ck_chat_monitors_priority", "priority BETWEEN 0 AND 9"),
        _in(t, "decided_by", CHAT_DECIDED_BY),
        _ck("ck_chat_monitors_joined", "status <> 'joined' OR joined_at IS NOT NULL"),
    )
    op.create_index(
        "ix_chat_monitors_queue",
        t,
        ["priority", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )


def _heartbeats() -> None:
    op.create_table(
        "heartbeats",
        _text("component", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        _jsonb("detail", empty=True),
        _pk("heartbeats", "component"),
    )


def _usage_log() -> None:
    t = "usage_log"
    op.create_table(
        t,
        _id(t),
        _text("provider", nullable=False),
        _text("model"),
        _text("operation", nullable=False),
        _int("input_tokens", default=None),
        _int("output_tokens", default=None),
        _int("cache_read_tokens", default=None),
        _int("cache_write_tokens", default=None),
        sa.Column("units", sa.Numeric(14, 3), nullable=True),
        _bool("batch", False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("source_key", sa.Text(), _fk(t, "source_key", "sources.key"), nullable=True),
        _uuid("company_id", _fk(t, "company_id", "companies.id", "SET NULL")),
        sa.Column(
            "model_job_id",
            sa.BigInteger(),
            _fk(t, "model_job_id", "model_jobs.id", "SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "collector_run_id",
            sa.BigInteger(),
            _fk(t, "collector_run_id", "collector_runs.id", "SET NULL"),
            nullable=True,
        ),
        _ts("created_at", now=True),
        _pk(t, "id"),
        _in(t, "provider", PROVIDERS),
        _in(t, "operation", OPERATIONS),
        _ck("ck_usage_log_cost_usd", "cost_usd >= 0"),
    )
    op.create_index("ix_usage_log_created_at", t, ["created_at"])
    op.create_index("ix_usage_log_provider_created", t, ["provider", "created_at"])
    op.create_index("ix_usage_log_company", t, ["company_id"])
    op.create_index("ix_usage_log_model_job", t, ["model_job_id"])
    op.create_index("ix_usage_log_collector_run", t, ["collector_run_id"])


def _reminder_log() -> None:
    t = "reminder_log"
    op.create_table(
        t,
        _id(t),
        _text("kind", nullable=False),
        _text("ref", nullable=False),
        _ts("sent_at", now=True),
        _pk(t, "id"),
    )
    op.create_index("ix_reminder_log_kind_ref_sent", t, ["kind", "ref", "sent_at"])


def _runtime_flags() -> None:
    op.create_table(
        "runtime_flags",
        _text("key", nullable=False),
        _jsonb("value"),
        _ts("updated_at", now=True),
        _text("updated_by", nullable=False),
        _pk("runtime_flags", "key"),
    )


def _merge_log() -> None:
    t = "merge_log"
    op.create_table(
        t,
        _id(t),
        _uuid("source_company_id", nullable=False),
        _uuid(
            "target_company_id",
            _fk(t, "target_company_id", "companies.id", "RESTRICT"),
            nullable=False,
        ),
        _text("decided_by", nullable=False),
        _int("match_score", sa.SmallInteger, default=None),
        _jsonb("moved"),
        _jsonb("carried", empty=True),
        _ts("created_at", now=True),
        _ts("undone_at"),
        _text("undone_by"),
        _pk(t, "id"),
        _ck("ck_merge_log_match_score", "match_score BETWEEN 0 AND 100"),
    )
    op.create_index("ix_merge_log_source", t, ["source_company_id"])
    op.create_index("ix_merge_log_target", t, ["target_company_id"])


def _purge_log() -> None:
    t = "purge_log"
    op.create_table(
        t,
        _id(t),
        _text("subject_kind", nullable=False),
        sa.Column("subject_hash", sa.CHAR(64), nullable=False),
        _text("reason", nullable=False),
        _text("requested_by", nullable=False),
        _jsonb("plan"),
        _jsonb("executed", nullable=True),
        _ts("planned_at", now=True),
        _ts("executed_at"),
        _text("error"),
        _pk(t, "id"),
        _in(t, "subject_kind", PURGE_SUBJECTS),
        _in(t, "reason", PURGE_REASONS),
    )
    op.create_index("ix_purge_log_subject", t, ["subject_kind", "subject_hash"])


def _sheet_rows() -> None:
    t = "sheet_rows"
    op.create_table(
        t,
        _id(t),
        sa.Column("tab", sa.String(32), nullable=False),
        sa.Column("entity_kind", sa.String(16), nullable=False),
        _text("entity_id", nullable=False),
        _int("row_number", default=None),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("state", sa.String(16), server_default="pending", nullable=False),
        _int("attempts", sa.SmallInteger),
        _ts("next_attempt_at"),
        sa.Column("last_error", sa.String(64), nullable=True),
        _ts("synced_at"),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
        _pk(t, "id"),
        _in(t, "tab", SHEET_TABS),
        _in(t, "entity_kind", SHEET_ENTITY_KINDS),
        _in(t, "state", SHEET_ROW_STATES),
        _ck("ck_sheet_rows_row_number", "row_number IS NULL OR row_number >= 2"),
        sa.UniqueConstraint("tab", "entity_kind", "entity_id", name="uq_sheet_rows_entity"),
    )
    op.create_index(
        "ux_sheet_rows_row_number",
        t,
        ["tab", "row_number"],
        unique=True,
        postgresql_where=sa.text("row_number IS NOT NULL"),
    )
    op.create_index(
        "ix_sheet_rows_due",
        t,
        ["next_attempt_at"],
        postgresql_where=sa.text("state IN ('pending', 'failed', 'tombstone')"),
    )


_CREATE = (
    _sources,
    _collector_runs,
    _evidence,
    _companies,
    _company_aliases,
    _company_ids,
    _contacts,
    _observations,
    _model_jobs,
    _signals,
    _scoring_weights,
    _suppression,
    _lead_candidates,
    _lead_reviews,
    _push_log,
    _outcomes,
    _interactions,
    _chat_monitors,
    _heartbeats,
    _usage_log,
    _reminder_log,
    _runtime_flags,
    _merge_log,
    _purge_log,
    _sheet_rows,
)


# --- Triggers and seeds -----------------------------------------------------------


def _triggers() -> None:
    for table in _TOUCHED:
        op.execute(
            f"CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION cs_touch_updated_at()"
        )
    for table in _APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER trg_append_only BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION cs_append_only()"
        )
    op.execute(
        "CREATE TRIGGER trg_scoring_weights_frozen BEFORE UPDATE OR DELETE ON scoring_weights "
        "FOR EACH ROW EXECUTE FUNCTION cs_scoring_weights_frozen()"
    )
    op.execute(
        "CREATE TRIGGER trg_lead_candidates_suppressed BEFORE INSERT OR UPDATE OF state "
        "ON lead_candidates FOR EACH ROW EXECUTE FUNCTION cs_candidate_suppressed_by()"
    )


def _seed() -> None:
    sources = sa.table(
        "sources",
        sa.column("key", sa.Text()),
        sa.column("family", sa.Text()),
        sa.column("title_uz", sa.Text()),
        sa.column("enabled", sa.Boolean()),
        sa.column("cron", sa.Text()),
        sa.column("rps", sa.Numeric(6, 3)),
        sa.column("daily_page_budget", sa.Integer()),
        sa.column("daily_cost_cap_usd", sa.Numeric(8, 4)),
        sa.column("config", _SeedJsonb()),
    )
    op.bulk_insert(
        sources,
        [
            {
                "key": key,
                "family": family,
                "title_uz": title,
                "enabled": enabled,
                "cron": cron,
                "rps": rps,
                "daily_page_budget": pages,
                "daily_cost_cap_usd": usd,
                "config": _ERP_SYNC_CONFIG if key == "erp_sync" else {},
            }
            for key, family, title, enabled, cron, rps, pages, usd in _SOURCES
        ],
    )
    weights = sa.table(
        "scoring_weights",
        sa.column("version", sa.Integer()),
        sa.column("key", sa.Text()),
        sa.column("group_key", sa.Text()),
        sa.column("points", sa.SmallInteger()),
        sa.column("group_cap", sa.SmallInteger()),
        sa.column("decay_days", sa.Integer()),
        sa.column("proposed_by", sa.Text()),
    )
    op.bulk_insert(
        weights,
        [
            {
                "version": 1,
                "key": key,
                "group_key": group,
                "points": points,
                "group_cap": cap,
                "decay_days": decay,
                "proposed_by": "seed",
            }
            for key, group, points, cap, decay in WEIGHTS_V1
        ],
    )
    # Approved the way every later version is (02 §2.7: one UPDATE of
    # approved_by and approved_at), which also keeps --sql free of a
    # server-side now() inside the insert.
    op.execute(
        "UPDATE scoring_weights SET approved_by = 'seed', approved_at = now() "
        "WHERE version = 1"
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for _signature, sql in _FUNCTIONS:
        op.execute(sql)
    for create in _CREATE:
        create()
    _triggers()
    _seed()


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
    for signature, _sql in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
