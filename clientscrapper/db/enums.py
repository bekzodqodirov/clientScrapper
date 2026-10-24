"""Allowed values of every enumerated column, as frozen tuples (02 §0).

These are CHECK constraints, never native enums: a value list here is
rendered into SQL by ``sql_list`` (MIYA's ``_sql_list`` pattern). The
migration that creates a CHECK keeps its own frozen copy of the tuple;
``tests/test_enums_frozen.py`` holds the two equal and reads the live
constraint definitions back from the database. Widening a list is a new
migration that drops and recreates the constraint, plus the change here, in
one commit (02 §5).
"""

from __future__ import annotations

from typing import Final, NamedTuple


def sql_list(values: tuple[str, ...]) -> str:
    """('a', 'b') → "'a', 'b'" for an ``IN (…)`` clause; values are code, never input."""
    return ", ".join(f"'{v}'" for v in values)


SOURCE_FAMILIES: Final = (
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
# collector_runs.status — a host breaker ends a run 'failed' with
# error='host_blocked'; there is no 'blocked' status (03 §1.2).
RUN_STATUSES: Final = ("running", "ok", "partial", "failed", "skipped")
OBSERVATION_STATUSES: Final = ("new", "resolved", "rejected", "needs_review")
COMPANY_STATES: Final = (
    "new",
    "active",
    "excluded",
    "client",
    "suppressed",
    "merged",
    "purged",
)
REGISTRY_STATUSES: Final = ("unknown", "active", "inactive", "liquidated")
VOLUME_BANDS: Final = ("unknown", "lt1", "1_3", "3_10", "truck", "container")
LEGAL_FORMS: Final = (
    "MChJ",
    "OOO",
    "LLC",
    "QK",
    "AJ",
    "XK",
    "YaTT",
    "IP",
    "ChP",
    "SP",
    "TOO",
    "other",
)
CONTACT_TIERS: Final = ("company", "person")
PHONE_KINDS: Final = ("mobile", "landline", "short", "unknown")
LEGAL_BASES: Final = (
    "public_source",
    "public_registry",
    "pre_contract_request",
    "consent",
    "contract",
)
# No 'tg_user': a poster's personal Telegram id is person-tier and lives only
# in contacts.telegram_id (02 §2.4, 06 §5.1).
ID_TYPES: Final = (
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

# Observed signal keys: stored in `signals`, weighted in `scoring_weights`.
SIGNAL_KEYS: Final = (
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
# Computed by the scorer at score time from contacts and signal ages; they
# carry weights but are never stored as signals.
DERIVED_KEYS: Final = (
    "derived_fresh_7d",
    "derived_fresh_30d",
    "derived_fresh_90d",
    "derived_contact_mobile",
    "derived_contact_decision_maker",
    "derived_contact_dm_channel",
    "derived_corroboration",
)
WEIGHT_GROUPS: Final = (
    "volume",
    "origin",
    "solidity",
    "recency",
    "contact",
    "corroboration",
    "negative",
    "gate",
)

CANDIDATE_STATES: Final = (
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
# The states ux_lead_candidates_open allows once per company.
CANDIDATE_OPEN_STATES: Final = (
    "found",
    "enriching",
    "scored",
    "review",
    "approved",
    "hold",
)
CANDIDATE_TIERS: Final = ("hot", "warm", "cold")
EXCLUDE_REASONS: Final = (
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
REVIEW_STATES: Final = ("pending", "asked", "approved", "declined", "expired", "merged")
# One live owner card and one live manager card per candidate.
REVIEW_LIVE_STATES: Final = ("pending", "asked")
REVIEW_KINDS: Final = ("owner", "manager")
# The last five belong to the manager handoff flow (06 §1.3/§1.7).
REVIEW_ANSWERS: Final = (
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

PUSH_STATUSES: Final = ("sending", "sent", "failed", "unknown")
# 'unknown' blocks a second push exactly like 'sent' (02 §2.10 rule 5).
PUSH_DELIVERED_STATUSES: Final = ("sent", "unknown")
PUSH_SOURCE_KEYS: Final = ("agent", "other")

LANDINGS: Final = ("created", "joined", "client", "dropped")
STAGE_KINDS: Final = ("open", "won", "lost")
CONTACT_KINDS: Final = ("call", "telegram", "note", "stage", "followup")
OUTCOME_LABELS: Final = (
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
PRECISION_CLASSES: Final = ("positive", "negative", "pending", "neutral")

SUPPRESSION_KINDS: Final = (
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
SUPPRESSION_KEY_TYPES: Final = (
    "phone_tail",
    "phone_hash",
    "tg_username",
    "tg_user_id",
    "instagram",
    "domain",
    "stir",
    "company_id",
)
SUPPRESSION_SOURCES: Final = (
    "erp_feed",
    "outcomes",
    "owner_bot",
    "userbot_reply",
    "manual_import",
    "resolver",
    "retention",
)
# The only sources that may write a client/lost row (ck_suppression_erp_kinds).
SUPPRESSION_ERP_SOURCES: Final = ("erp_feed", "outcomes", "manual_import")

CHAT_TYPES: Final = ("private", "group", "channel")
# 'calibration' = GSR's own chat: monitor_enabled=false, nothing is read.
CHAT_ROLES: Final = ("market", "competitor", "buyers", "calibration", "other")
CHAT_STATUSES: Final = ("queued", "joined", "gone", "left")
CHAT_DECIDED_BY: Final = ("owner", "rule:seed", "rule:private_off", "rule:default")
MEDIA_KINDS: Final = ("none", "photo", "document", "video", "other")

PROVIDERS: Final = (
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
# 04 §8 `Operation`, verbatim: what costs.ask writes.
OPERATIONS: Final = (
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
JOB_KINDS: Final = ("classify", "verdict", "company_verdict", "draft")
JOB_LANES: Final = ("instant", "batch")
JOB_STATUSES: Final = ("pending", "submitted", "applied", "failed", "fallback")

PURGE_SUBJECTS: Final = (
    "company",
    "contact",
    "tg_user",
    "interaction_range",
    "retention",
)
PURGE_REASONS: Final = (
    "legal_request",
    "dnc",
    "b2c",
    "retention",
    "owner",
    "merge_cleanup",
)

SHEET_TABS: Final = ("Kompaniyalar", "Lidlar", "Yuborilganlar", "Manbalar")
SHEET_ENTITY_KINDS: Final = ("company", "candidate", "push", "source")
SHEET_ROW_STATES: Final = ("pending", "synced", "failed", "tombstone", "cleared")


class Weight(NamedTuple):
    """One row of a scoring_weights version, as seeded."""

    key: str
    group_key: str
    points: int
    group_cap: int
    decay_days: int | None


# Version 1 (02 §2.7, 04 §5.1), seeded by 0001 with proposed_by='seed'. A
# group's cap is repeated on each of its rows; the positive groups' caps sum
# to 100, `negative` and `gate` carry 0. decayed_factor is the column default
# (0.5) everywhere.
WEIGHTS_V1: Final[tuple[Weight, ...]] = (
    Weight("vol_container", "volume", 40, 40, 180),
    Weight("vol_stated_3plus", "volume", 30, 40, 180),
    Weight("vol_stated_1_3", "volume", 20, 40, 180),
    Weight("vol_wholesale_bulky", "volume", 20, 40, 365),
    Weight("vol_marketplace_velocity", "volume", 20, 40, 60),
    Weight("vol_vacancy_import", "volume", 15, 40, 180),
    Weight("vol_exhibitor", "volume", 15, 40, 365),
    Weight("vol_directory_wholesale", "volume", 10, 40, 365),
    Weight("cn_origin", "origin", 20, 20, 365),
    Weight("cn_sector_prior", "origin", 10, 20, None),
    Weight("ent_legal_confirmed", "solidity", 8, 15, None),
    Weight("ent_age_2y_active", "solidity", 4, 15, None),
    Weight("ent_not_micro", "solidity", 3, 15, None),
    Weight("derived_fresh_7d", "recency", 10, 10, None),
    Weight("derived_fresh_30d", "recency", 7, 10, None),
    Weight("derived_fresh_90d", "recency", 4, 10, None),
    Weight("derived_contact_mobile", "contact", 6, 10, None),
    Weight("derived_contact_decision_maker", "contact", 2, 10, None),
    Weight("derived_contact_dm_channel", "contact", 2, 10, None),
    Weight("derived_corroboration", "corroboration", 5, 5, None),
    Weight("neg_b2c_words", "negative", -40, 0, 180),
    Weight("neg_liquidated", "negative", -50, 0, None),
    Weight("neg_non_cn_origin", "negative", -20, 0, 365),
    Weight("neg_state_body", "negative", -20, 0, None),
    Weight("flag_competitor", "gate", 0, 0, None),
    Weight("flag_supplier_cn", "gate", 0, 0, None),
)
