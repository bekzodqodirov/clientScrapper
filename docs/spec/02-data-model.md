# 02 — Data model (PostgreSQL 16)

Scope: every table of the shared contract, its DDL, who writes and reads it, the JSON shapes it holds, the evidence files on disk, retention, Alembic and the ORM layout. Names here are canonical; `03`–`07` reference them. Shared context (business, budget, ERP contract, env names) is assumed, not restated.

## 0. Conventions

| Rule | Detail |
|---|---|
| Database | One PG16 database, schema `public`. Extension `pg_trgm` (alias search). `gen_random_uuid()` is core since PG13 — no `pgcrypto`. |
| Primary keys | `uuid` where the id leaves the system (`companies`, `contacts`, `lead_candidates`; `external_id` embeds the company uuid). `bigint GENERATED ALWAYS AS IDENTITY` for ledgers (`evidence`, `observations`, `signals`, `interactions`, logs). Natural keys: `sources.key`, `heartbeats.component`, `outcomes.external_id`. |
| Time | Every timestamp is `timestamptz`; business day is Asia/Tashkent in code. `created_at` = MIYA `created_at_column()`; `updated_at` = `created_at_column(onupdate=sa.func.now())` **plus** trigger `cs_touch_updated_at`, because the bot, userbot and worker also write raw SQL (ORM `onupdate` fires only on ORM updates). |
| Enumerations | **CHECK constraints**, never native enums. Allowed values are frozen tuples in `clientscrapper/db/enums.py` and rendered with `sql_list()` (MIYA `models.py` `_sql_list` / `SheetRow` pattern). A migration keeps its own frozen copy; `tests/test_enums_frozen.py` holds the two equal. `pg_enum()` stays in `base.py` unused. |
| Names | `ix_<table>_<cols>` index, `ux_<table>_<what>` unique (partial) index, `uq_<table>_<cols>` unique constraint, `ck_<table>_<rule>` check, `fk_<table>_<col>` foreign key, `pk_<table>`. MIYA names every constraint explicitly; we do the same and additionally set `Base.metadata.naming_convention` so an autogenerate diff is empty. Inline column CHECKs written unnamed in this document are declared in the ORM as `CheckConstraint(..., name="ck_<table>_<column>")`. |
| Numbers | Cost `numeric(12,6)` USD; volume `numeric(12,3)` m³; score `smallint 0–100`. Never float for money. |
| FK hygiene | Every FK with `CASCADE`/`SET NULL` has an index (MIYA WP-23). `tests/test_fk_indexes.py` walks `Base.metadata`. |
| PII placement | Person-tier fields live only in `contacts`; transiently in `interactions.sender_*`, `observations.parsed`, `push_log.payload`, `model_jobs.pseudonyms` — each with a retention step (§4). `tests/test_pii_columns.py` keeps an allowlist of such columns; any other column whose name matches `phone|name|username|bio|email` fails the test. |
| Append-only | `push_log`, `outcomes`, `merge_log`, `purge_log`, `usage_log` refuse `DELETE` through trigger `cs_append_only` (retention nulls columns, never deletes rows). |

## 1. Key normalisation — one home

Python (`clientscrapper/services/keys.py`) is the writer of every normalised key; SQL mirrors exist for CHECKs, generated columns and ad-hoc queries. `tests/test_keys.py` runs both over one fixture list and demands equality.

```python
def phone_tail(raw: str | None) -> str | None:
    """Digits only; >= 9 digits -> last 9, else None. Identical to the ERP's inboundPhoneDigits."""

def phone_e164(raw: str | None) -> str | None:
    """'+998XXXXXXXXX'. A bare 9-digit UZ number gets +998; landlines/short codes kept; otherwise None."""

def phone_kind(e164: str) -> Literal["mobile", "landline", "short", "unknown"]:
    """+9989x/+9983x/+9987x/+9988x -> mobile; +99871/+99861… -> landline; <7 digits -> short."""

def username_key(raw: str | None) -> str | None:
    """Strip https://, www., t.me/, telegram.me/, '@', anything from '/', '?', '#'; lowercase;
    must match ^[a-z0-9_]{5,32}$ and not end with 'bot'."""

def instagram_key(raw: str | None) -> str | None:
    """Strip instagram.com/, '@', trailing '/', query; lowercase; ^[a-z0-9._]{1,30}$."""

def domain_key(raw: str | None) -> str | None:
    """urlsplit host (or the raw string when no scheme); lowercase; IDNA-encode; strip leading 'www.';
    None for social/marketplace hosts (instagram.com, t.me, telegram.me, facebook.com, olx.uz,
    uzum.uz, youtube.com, wa.me) — those are handles, not websites."""

def stir_key(raw: str | None) -> str | None:
    """9 digits, first digit in 2..6, not all equal. UNVERIFIED — check whether a STIR checksum exists;
    v1 validates shape only."""

def name_key(raw: str) -> str:
    """fold_apostrophes -> transliterate (Cyrillic->Latin) -> lower -> strip «»“”" -> strip legal forms
    (MChJ, OOO, ООО, LLC, QK, AJ, XK, ChP, ЧП, IP, ИП, YaTT, SP, ТОО) -> collapse spaces. From services/text.py."""

def erp_phone_hash(secret: str, tail9: str) -> str:
    """hmac_sha256(secret, tail9).hexdigest()[:20] — the suppression feed rule."""

def erp_salt_id(secret: str) -> str:
    """sha256(secret).hexdigest()[:8] — must equal the feed's salt_id or a full pull is forced."""
```

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE FUNCTION cs_phone_tail(raw text) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT CASE WHEN length(d) >= 9 THEN right(d, 9) END
  FROM (SELECT regexp_replace(raw, '[^0-9]', '', 'g') AS d) s $$;

CREATE FUNCTION cs_username_key(raw text) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT CASE WHEN u ~ '^[a-z0-9_]{5,32}$' AND u !~ 'bot$' THEN u END
  FROM (SELECT lower(regexp_replace(regexp_replace(trim(raw),
          '^([A-Za-z]+://)?(www\.)?(t\.me/|telegram\.me/)?@?', ''), '[/?#].*$', '')) AS u) s $$;

CREATE FUNCTION cs_domain_key(raw text) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT CASE WHEN h ~ '^[a-z0-9-]+(\.[a-z0-9-]+)+$'
          AND h NOT IN ('instagram.com','t.me','telegram.me','facebook.com','olx.uz','uzum.uz','youtube.com','wa.me')
         THEN h END
  FROM (SELECT regexp_replace(lower(regexp_replace(regexp_replace(trim(raw),
          '^[A-Za-z]+://', ''), '[/?#:].*$', '')), '^www\.', '') AS h) s $$;

CREATE FUNCTION cs_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

CREATE FUNCTION cs_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END $$;
```

(SQL `cs_domain_key` does not IDNA-encode; Python stores the encoded value, so the CHECK `website_domain = cs_domain_key(website_domain)` holds for stored values.)

## 2. Tables

Order below is the migration's creation order. "W/R" = who writes / who reads.

### 2.1 `sources` — one row per collector or feed

Purpose: registry, switches, budgets, breaker and precision per source; `config` holds per-source state (keywords, cursors, the ERP feed's `salt_id`).

```sql
CREATE TABLE sources (
  key                text PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_]{2,40}$'),
  family             text NOT NULL CHECK (family IN ('telegram','instagram','olx','marketplace','directory',
                       'vacancy','exhibitor','maps','brand','search','registry','erp','manual')),
  title_uz           text NOT NULL,
  enabled            boolean NOT NULL DEFAULT false,
  cron               text,                                   -- APScheduler cron; NULL = event-driven
  rps                numeric(6,3) NOT NULL DEFAULT 0.3 CHECK (rps > 0 AND rps <= 5),
  daily_page_budget  integer NOT NULL DEFAULT 500 CHECK (daily_page_budget >= 0),
  daily_cost_cap_usd numeric(8,4) NOT NULL DEFAULT 0 CHECK (daily_cost_cap_usd >= 0),
  auto_push          boolean NOT NULL DEFAULT false,
  precision_30d      numeric(5,4) CHECK (precision_30d BETWEEN 0 AND 1),
  pushes_30d         integer NOT NULL DEFAULT 0,
  positives_30d      integer NOT NULL DEFAULT 0,
  paused_until       timestamptz,
  paused_reason      text,
  breaker_until      timestamptz,
  breaker_reason     text,
  last_run_at        timestamptz,
  config             jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
```

Seeded in `0001` (enabled=false): `telegram_monitor`, `instagram_discovery`, `olx_business`, `uzum_sellers`, `directories_goldenpages`, `directories_yellowpages`, `directories_glotr`, `directories_prom`, `hh_vacancies`, `exhibitors`, `maps_2gis`, `maps_google`, `brand_dealers`, `search_brave`, `search_yandex`, `search_searxng`, `registry_lookup`, `erp_sync`, `manual`. `erp_sync.config = {"suppression":{"salt_id":null,"last_full_at":null,"last_delta_at":null,"count":0},"outcomes":{"cursor":null,"last_at":null}}`.
Auto-push rule stored here: `auto_push` may be true only when `pushes_30d >= 15 AND precision_30d >= 0.60` — `source_precision_job` flips it both ways weekly.
W/R: 0001 seed, bot `/manbalar` (enabled, paused), worker (`last_run_at`, breaker, precision, auto_push, config cursors) / scheduler, collectors, gate, `/holat`.

### 2.2 `collector_runs` — one row per collector execution

```sql
CREATE TABLE collector_runs (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_key       text NOT NULL REFERENCES sources(key),
  started_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  status           text NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','partial','failed','skipped')),
  requests         integer NOT NULL DEFAULT 0,
  pages            integer NOT NULL DEFAULT 0,
  http_403         integer NOT NULL DEFAULT 0,
  http_429         integer NOT NULL DEFAULT 0,
  new_evidence     integer NOT NULL DEFAULT 0,
  new_observations integer NOT NULL DEFAULT 0,
  new_companies    integer NOT NULL DEFAULT 0,
  new_signals      integer NOT NULL DEFAULT 0,
  cost_usd         numeric(12,6) NOT NULL DEFAULT 0,
  error            text,                                      -- an error code, never a body
  meta             jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT ck_collector_runs_finished CHECK (finished_at IS NULL OR finished_at >= started_at)
);
CREATE INDEX ix_collector_runs_source_started ON collector_runs (source_key, started_at DESC);
CREATE UNIQUE INDEX ux_collector_runs_one_running ON collector_runs (source_key) WHERE status = 'running';
```
W/R: collectors / `/holat`, `source_precision_job`, breaker logic (403/429 streak over the last 5 runs).

### 2.3 `evidence` — an immutable fetched artefact

```sql
CREATE TABLE evidence (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_key       text NOT NULL REFERENCES sources(key),
  collector_run_id bigint REFERENCES collector_runs(id) ON DELETE SET NULL,
  url              text NOT NULL CHECK (length(url) <= 2048),
  source_ref       text,                 -- platform-native id: 'tg:<chat>:<msg>', OLX ad id, hh vacancy id, place_id
  fetched_at       timestamptz NOT NULL,
  published_at     timestamptz,          -- the post/ad date when the platform shows one
  sha256           char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  content_type     text NOT NULL DEFAULT 'text/html',
  bytes            integer NOT NULL CHECK (bytes >= 0),
  raw_path         text,                 -- '<yy>/<sha256>.gz' under EVIDENCE_DIR; NULL = not stored or purged
  excerpt          text NOT NULL CHECK (length(excerpt) <= 600),
  title            text CHECK (length(title) <= 300),
  lang             char(2),
  expires_at       timestamptz NOT NULL,  -- fetched_at + 180 d: the file goes, the row stays
  raw_deleted_at   timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_evidence_url_sha      ON evidence (md5(url), sha256);
CREATE INDEX ix_evidence_source_fetched      ON evidence (source_key, fetched_at DESC);
CREATE INDEX ix_evidence_sha                 ON evidence (sha256);
CREATE INDEX ix_evidence_ref                 ON evidence (source_key, source_ref) WHERE source_ref IS NOT NULL;
CREATE INDEX ix_evidence_expiry              ON evidence (expires_at) WHERE raw_path IS NOT NULL;
CREATE INDEX ix_evidence_collector_run       ON evidence (collector_run_id);
```
Rules: a re-fetch with identical bytes is a no-op (`ux_evidence_url_sha`); changed bytes = a new row. `excerpt` is the ≤600-char slice the card and the ERP note quote; it never contains a phone that is not already in the public post. W/R: collectors (`services/evidence.store`) / resolver, scorer, card renderer, retention.

### 2.4 `companies`, `company_aliases`, `company_ids`, `contacts`

```sql
CREATE TABLE companies (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name     text NOT NULL CHECK (length(display_name) BETWEEN 2 AND 160),
  name_key         text NOT NULL CHECK (length(name_key) BETWEEN 1 AND 160),
  legal_form       text CHECK (legal_form IN ('MChJ','OOO','LLC','QK','AJ','XK','YaTT','IP','ChP','SP','TOO','other')),
  stir             char(9) CHECK (stir ~ '^[0-9]{9}$'),
  oked             char(5) CHECK (oked ~ '^[0-9]{5}$'),
  city             text,
  region           text,
  address          text CHECK (length(address) <= 300),
  website_domain   text CHECK (website_domain = cs_domain_key(website_domain)),
  telegram_channel text CHECK (telegram_channel = cs_username_key(telegram_channel)),   -- the @company channel
  instagram        text CHECK (instagram = lower(instagram)),                            -- the company account
  registry_status  text NOT NULL DEFAULT 'unknown' CHECK (registry_status IN ('unknown','active','inactive','liquidated')),
  registered_on    date,
  sectors          text[] NOT NULL DEFAULT '{}',
  goods            text[] NOT NULL DEFAULT '{}',        -- uz, 1–6 items, from ImportSignal
  segment          char(1) CHECK (segment ~ '^[A-Z]$'),  -- A bazaar wholesaler … X competitor … Z unclear (namuna)
  volume_band      text NOT NULL DEFAULT 'unknown' CHECK (volume_band IN ('unknown','lt1','1_3','3_10','truck','container')),
  is_competitor    boolean NOT NULL DEFAULT false,
  is_supplier_cn   boolean NOT NULL DEFAULT false,
  is_state_body    boolean NOT NULL DEFAULT false,
  state            text NOT NULL DEFAULT 'new' CHECK (state IN ('new','active','excluded','client','suppressed','merged','purged')),
  exclude_reason   text,
  merged_into      uuid REFERENCES companies(id) ON DELETE SET NULL,
  verdict          jsonb,                                -- last RelevanceVerdict (model output only)
  verdict_at       timestamptz,
  first_seen_at    timestamptz NOT NULL DEFAULT now(),
  last_signal_at   timestamptz,
  best_score       smallint CHECK (best_score BETWEEN 0 AND 100),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_companies_merged CHECK ((state = 'merged') = (merged_into IS NOT NULL)),
  CONSTRAINT ck_companies_excluded CHECK (state <> 'excluded' OR exclude_reason IS NOT NULL)
);
CREATE UNIQUE INDEX ux_companies_stir    ON companies (stir) WHERE stir IS NOT NULL AND state <> 'merged';
CREATE UNIQUE INDEX ux_companies_domain  ON companies (website_domain) WHERE website_domain IS NOT NULL AND state <> 'merged';
CREATE INDEX ix_companies_name_key_trgm  ON companies USING gin (name_key gin_trgm_ops);
CREATE INDEX ix_companies_state_signal   ON companies (state, last_signal_at DESC);
CREATE INDEX ix_companies_merged_into    ON companies (merged_into);

CREATE TABLE company_aliases (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  alias       text NOT NULL CHECK (length(alias) BETWEEN 2 AND 160),
  alias_key   text NOT NULL,
  source_key  text REFERENCES sources(key),
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_company_aliases_company_key UNIQUE (company_id, alias_key)
);
CREATE INDEX ix_company_aliases_key_trgm ON company_aliases USING gin (alias_key gin_trgm_ops);
CREATE INDEX ix_company_aliases_key      ON company_aliases (alias_key);

CREATE TABLE company_ids (
  company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  id_type     text NOT NULL CHECK (id_type IN ('hh_employer','gis2_firm','google_place','uzum_shop','olx_user',
                'tg_user','tg_chat','instagram','facebook','yandex_org','myorg','goldenpages','yellowpages',
                'glotr','prom','exhibitor_slug')),
  id_value    text NOT NULL CHECK (length(id_value) BETWEEN 1 AND 200),
  source_key  text REFERENCES sources(key),
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id_type, id_value)
);
CREATE INDEX ix_company_ids_company ON company_ids (company_id);

CREATE TABLE contacts (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id        uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  tier              text NOT NULL CHECK (tier IN ('company','person')),
  full_name         text CHECK (length(full_name) <= 120),
  role              text CHECK (length(role) <= 80),
  phone             text CHECK (phone ~ '^\+[1-9][0-9]{6,14}$'),
  phone_tail        char(9) GENERATED ALWAYS AS (cs_phone_tail(phone)) STORED,
  phone_kind        text CHECK (phone_kind IN ('mobile','landline','short','unknown')),
  telegram_username text CHECK (telegram_username = cs_username_key(telegram_username)),
  telegram_id       bigint,
  instagram         text CHECK (instagram = lower(instagram)),
  email             text CHECK (email ~ '^[^@[:space:]]+@[^@[:space:]]+$'),
  is_decision_maker boolean NOT NULL DEFAULT false,
  shared_phone      boolean NOT NULL DEFAULT false,      -- same phone on >=3 unrelated companies (accountant/broker)
  legal_basis       text CHECK (legal_basis IN ('public_source','public_registry','pre_contract_request','consent','contract')),
  source_url        text CHECK (length(source_url) <= 2048),
  source_key        text REFERENCES sources(key),
  collected_at      timestamptz,
  verified_at       timestamptz,
  anonymised_at     timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_contacts_person_basis CHECK (tier = 'company' OR anonymised_at IS NOT NULL
      OR (legal_basis IS NOT NULL AND source_url IS NOT NULL AND collected_at IS NOT NULL)),
  CONSTRAINT ck_contacts_company_no_name CHECK (tier = 'person' OR (full_name IS NULL AND role IS NULL AND telegram_id IS NULL)),
  CONSTRAINT ck_contacts_phone_kind CHECK ((phone IS NULL) = (phone_kind IS NULL)),
  CONSTRAINT ck_contacts_no_pinfl CHECK (coalesce(full_name,'') !~ '[0-9]{14}' AND coalesce(role,'') !~ '[0-9]{14}')
);
CREATE UNIQUE INDEX ux_contacts_one_person    ON contacts (company_id) WHERE tier = 'person' AND anonymised_at IS NULL;
CREATE UNIQUE INDEX ux_contacts_company_phone ON contacts (company_id, phone) WHERE phone IS NOT NULL;
CREATE INDEX ix_contacts_company     ON contacts (company_id);
CREATE INDEX ix_contacts_phone_tail  ON contacts (phone_tail) WHERE phone_tail IS NOT NULL;
CREATE INDEX ix_contacts_tg_username ON contacts (telegram_username) WHERE telegram_username IS NOT NULL;
CREATE INDEX ix_contacts_tg_id       ON contacts (telegram_id) WHERE telegram_id IS NOT NULL;
CREATE INDEX ix_contacts_instagram   ON contacts (instagram) WHERE instagram IS NOT NULL;
```
Rules: `ux_contacts_one_person` is the "one person per company" law; a second decision maker replaces the first (old row anonymised, not deleted). Company-tier rows hold switchboard phones, `info@`, the @company handle. `google_place` id_value is the only thing kept from Places (policy). W/R: resolver (`services/companies.py`), enrichment, bot `edit_phone`, merge, retention / scorer, card, push, suppression gate.

### 2.5 `observations` — one parsed finding, before resolution

```sql
CREATE TABLE observations (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  evidence_id    bigint NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  ordinal        smallint NOT NULL DEFAULT 0 CHECK (ordinal >= 0),   -- a directory page yields many
  source_key     text NOT NULL REFERENCES sources(key),
  status         text NOT NULL DEFAULT 'new' CHECK (status IN ('new','resolved','rejected','needs_review')),
  parsed         jsonb NOT NULL,
  parsed_version smallint NOT NULL DEFAULT 1,
  company_id     uuid REFERENCES companies(id) ON DELETE SET NULL,
  contact_id     uuid REFERENCES contacts(id) ON DELETE SET NULL,
  reject_reason  text,                       -- competitor|supplier_cn|b2c|no_company|duplicate|foreign
  resolved_at    timestamptz,
  scrubbed_at    timestamptz,                -- phones/person removed from parsed (retention R3)
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_observations_evidence_ordinal UNIQUE (evidence_id, ordinal)
);
CREATE INDEX ix_observations_queue    ON observations (created_at) WHERE status IN ('new','needs_review');
CREATE INDEX ix_observations_company  ON observations (company_id);
CREATE INDEX ix_observations_contact  ON observations (contact_id);
CREATE INDEX ix_observations_name_key ON observations ((parsed->>'name_key'));
```

**Observation JSON shape** (`observations.parsed`, pydantic `Observation` in `services/observations.py`, `parsed_version=1`):

```json
{
  "v": 1,
  "kind": "intent_post | profile | listing | directory_entry | vacancy | exhibitor | place | dealer_page | registry | seller",
  "source_key": "telegram_monitor",
  "source_ref": "tg:-1001000000001:18234",
  "url": "https://t.me/namuna_optom/18234",
  "observed_at": "2026-10-20T07:41:00+05:00",
  "name_key": "namuna tekstil savdo",
  "company": {
    "name_raw": "Namuna Tekstil Savdo MChJ", "legal_form": "MChJ", "stir": null, "oked": null,
    "city": "Toshkent", "address": null, "website": null, "goods_text": "mato, furnitura",
    "sector_hints": ["textile"]
  },
  "handles": {"telegram": "namuna_tekstil", "tg_user_id": 5100000001, "instagram": null, "olx_user_id": null,
              "hh_employer_id": null, "gis2_firm_id": null, "google_place_id": null, "uzum_shop": null},
  "phones": [{"raw": "+998 90 000 00 01", "e164": "+998900000001", "kind": "mobile", "owner": "person | company | unknown"}],
  "person": {"name": "Rustam", "role": "direktor"},
  "text": {"excerpt": "Guangzhoudan 25 kub mato olib kelish kerak, kim qiladi?", "volume_text": "25 kub",
           "origin_text": "Guangzhou", "intent_words": ["kub", "olib kelish", "kerak"]},
  "hints": {"volume_m3": 25.0, "volume_basis": "stated | container_words | weight | proxy | none",
            "container_words": false, "wholesale_words": false, "b2c_words": false,
            "competitor_words": false, "china_words": true, "state_words": false},
  "legal_basis": "public_source",
  "collector_version": "telegram_monitor@1"
}
```
Validators: `name_key` recomputed from `company.name_raw`; `phones[].e164`/`kind` recomputed; `excerpt ≤ 600`; any 14-digit run anywhere → the field is dropped and `hints.pinfl_dropped=true`; `person` is kept only when `kind ∈ {intent_post, profile, vacancy, registry}` (a directory entry names a firm, not a person). W/R: collectors (via normaliser) / resolver (`resolve_job`), retention.

### 2.6 `model_jobs` — Claude calls, instant and Batch

```sql
CREATE TABLE model_jobs (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind          text NOT NULL CHECK (kind IN ('classify','verdict','company_verdict','draft')),
  lane          text NOT NULL CHECK (lane IN ('instant','batch')),
  model         text NOT NULL,                           -- the configured id (EXTRACT_MODEL / VERDICT_MODEL / BATCH_MODEL)
  custom_id     text NOT NULL UNIQUE,                    -- 'cs-' || uuid4, minted in Python before insert
  batch_id      text,
  status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','submitted','applied','failed','fallback')),
  attempts      smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
  company_id    uuid REFERENCES companies(id) ON DELETE CASCADE,
  evidence_ids  bigint[] NOT NULL DEFAULT '{}',
  request       jsonb,                                   -- pseudonymised payload; NULL 30 d after terminal
  pseudonyms    jsonb,                                   -- {"PHONE_1":"+998…","TG_1":"…"} reverse map; NULL with request
  result        jsonb,                                   -- validated structured output
  error         text,
  cost_usd      numeric(12,6),
  submitted_at  timestamptz,
  applied_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_model_jobs_status_created ON model_jobs (status, created_at);
CREATE INDEX ix_model_jobs_batch   ON model_jobs (batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX ix_model_jobs_company ON model_jobs (company_id);
```
Lifecycle as MIYA `ConversationWindow`: `pending → submitted → applied | failed`; `fallback` = batch gave up after 3 attempts and the instant lane took it. `pseudonyms` never leaves the process; `request` must contain no raw phone/handle — `tests/test_pseudonymised_requests.py` greps stored requests for `\+998\d{9}|@[a-z0-9_]{5,}`. W/R: enrich/score jobs, `model_batch_submit/collect` / scorer (signals), cost accounting.

### 2.7 `signals`, `scoring_weights`

```sql
CREATE TABLE signals (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id     uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  contact_id     uuid REFERENCES contacts(id) ON DELETE SET NULL,
  evidence_id    bigint NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  observation_id bigint REFERENCES observations(id) ON DELETE SET NULL,
  key            text NOT NULL CHECK (key IN (
                   'vol_container','vol_stated_3plus','vol_stated_1_3','vol_wholesale_bulky','vol_marketplace_velocity',
                   'vol_vacancy_import','vol_exhibitor','vol_directory_wholesale',
                   'cn_origin','cn_sector_prior',
                   'ent_legal_confirmed','ent_age_2y_active','ent_not_micro',
                   'neg_b2c_words','neg_liquidated','neg_non_cn_origin','neg_state_body',
                   'flag_competitor','flag_supplier_cn')),
  value          numeric(12,3),                       -- m³, order count, years — the measured quantity
  quote          text CHECK (length(quote) <= 300),   -- verbatim substring of evidence.excerpt (validated in Python)
  observed_at    timestamptz NOT NULL,
  expires_at     timestamptz,                         -- observed_at + decay_days of the key; NULL = no decay
  source_key     text NOT NULL REFERENCES sources(key),
  model          text,                                -- NULL = deterministic rule; else the model that said so
  model_job_id   bigint REFERENCES model_jobs(id) ON DELETE SET NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_signals_company_key_evidence UNIQUE (company_id, key, evidence_id)
);
CREATE INDEX ix_signals_company_key ON signals (company_id, key);
CREATE INDEX ix_signals_observed    ON signals (observed_at DESC);
CREATE INDEX ix_signals_evidence    ON signals (evidence_id);
CREATE INDEX ix_signals_contact     ON signals (contact_id);
CREATE INDEX ix_signals_observation ON signals (observation_id);
CREATE INDEX ix_signals_model_job   ON signals (model_job_id);

CREATE TABLE scoring_weights (
  version        integer NOT NULL CHECK (version >= 1),
  key            text NOT NULL,                       -- SIGNAL_KEYS ∪ DERIVED_KEYS (CHECK below)
  group_key      text NOT NULL CHECK (group_key IN ('volume','origin','solidity','recency','contact','corroboration','negative','gate')),
  points         smallint NOT NULL CHECK (points BETWEEN -100 AND 100),
  group_cap      smallint NOT NULL CHECK (group_cap BETWEEN 0 AND 100),
  decay_days     integer CHECK (decay_days > 0),
  decayed_factor numeric(3,2) NOT NULL DEFAULT 0.5 CHECK (decayed_factor BETWEEN 0 AND 1),
  note           text,
  proposed_by    text NOT NULL,                       -- 'seed' | 'owner:<tg_id>' | 'fit_weights@<date>'
  created_at     timestamptz NOT NULL DEFAULT now(),
  approved_by    text,
  approved_at    timestamptz,
  PRIMARY KEY (version, key),
  CONSTRAINT ck_scoring_weights_key CHECK (key IN (
    'vol_container','vol_stated_3plus','vol_stated_1_3','vol_wholesale_bulky','vol_marketplace_velocity',
    'vol_vacancy_import','vol_exhibitor','vol_directory_wholesale','cn_origin','cn_sector_prior',
    'ent_legal_confirmed','ent_age_2y_active','ent_not_micro','neg_b2c_words','neg_liquidated',
    'neg_non_cn_origin','neg_state_body','flag_competitor','flag_supplier_cn',
    'derived_fresh_7d','derived_fresh_30d','derived_fresh_90d',
    'derived_contact_mobile','derived_contact_decision_maker','derived_contact_dm_channel','derived_corroboration'))
);
CREATE INDEX ix_scoring_weights_approved ON scoring_weights (version) WHERE approved_at IS NOT NULL;
```

**Signal key registry** (`enums.SIGNAL_KEYS` = observed keys, stored in `signals`; `enums.DERIVED_KEYS` = computed by the scorer from `contacts`/`signals` at score time, never stored as signals; both carry weights). Version 1, seeded by `0001`:

| key | group (cap) | points | decay d | produced by |
|---|---|---|---|---|
| vol_container | volume (40) | 40 | 180 | regex `konteyner|контейнер|20ft|40ft|fura|фура|full truck` on own text |
| vol_stated_3plus | volume | 30 | 180 | `(\d+[.,]?\d*)\s*(kub|куб|m3|м3|m³)` ≥ 3 |
| vol_stated_1_3 | volume | 20 | 180 | same, 1 ≤ n < 3 |
| vol_wholesale_bulky | volume | 20 | 365 | `optom|оптом|ulgurji` + bulky category (furniture, building, auto parts, appliances, textile rolls, toys) |
| vol_marketplace_velocity | volume | 20 | 60 | Σ(orders_30d × category m³ factor) ≥ 1 |
| vol_vacancy_import | volume | 15 | 180 | hh.uz vacancy `ВЭД|деклар|закуп.*Кита|tashqi savdo|xitoy tili` |
| vol_exhibitor | volume | 15 | 365 | UZ exhibitor in a China-heavy sector |
| vol_directory_wholesale | volume | 10 | 365 | wholesale rubric in a directory |
| cn_origin | origin (20) | 20 | 365 | `Xitoy|Китай|China|1688|Taobao|Yiwu|Guangzhou|Urumqi` in own text / verdict `imports_from_china=likely` |
| cn_sector_prior | origin | 10 | — | goods in a China-dominant sector |
| ent_legal_confirmed | solidity (15) | 8 | — | STIR resolved in a registry |
| ent_age_2y_active | solidity | 4 | — | registry: registered ≥ 2 y, active |
| ent_not_micro | solidity | 3 | — | >1 vacancy, branches, ≥20 SKUs, several phones |
| derived_fresh_7d / 30d / 90d | recency (10) | 10 / 7 / 4 | rolling | age of the newest volume/intent signal |
| derived_contact_mobile | contact (10) | 6 | — | a mobile phone on a non-anonymised contact, `shared_phone=false` |
| derived_contact_decision_maker | contact | 2 | — | person-tier contact with `is_decision_maker` |
| derived_contact_dm_channel | contact | 2 | — | telegram_username or instagram present |
| derived_corroboration | corroboration (5) | 5 | — | signals from ≥ 2 source families |
| neg_b2c_words | negative | −40 | 180 | `o'zim uchun|для себя|1 dona|bitta|shaxsiy|личн` with no volume ≥ 1 |
| neg_liquidated | negative | −50 | — | registry status |
| neg_non_cn_origin | negative | −20 | 365 | only RU/TR/KR/EU named as origin |
| neg_state_body | negative | −20 | — | `davlat|ГУП|министер|hokimiyat|maktab|shifoxona` |
| flag_competitor, flag_supplier_cn | gate | 0 | — | evidence for an exclusion; no points |

Group caps sum to 100. `score = max(0, min(100, Σ capped positive groups) + Σ negatives)`; a signal past `expires_at` counts `points × decayed_factor`. `tests/test_weights_seed.py`: every key in both tuples has exactly one row in version 1, caps sum to 100, version 1 is approved.

**Versioning rules.** A version is a complete set (one row per key). Active version = `max(version) WHERE approved_at IS NOT NULL`. Activation = one `UPDATE … SET approved_by, approved_at WHERE version = $1` after `services/scoring.validate_version()` (completeness, caps, each weight within ±30 % of the previous version unless `proposed_by` starts with `owner:`). Trigger `cs_scoring_weights_frozen` (BEFORE UPDATE OR DELETE) refuses any change other than setting `approved_*` once `approved_at IS NOT NULL`. `/vazn key points` copies the active version into `version+1`, applies the change and approves it. `lead_candidates.weights_version` + `score_breakdown` make every score reproducible. W/R: 0001 seed, bot `/vazn`, `tools/fit_weights.py` / `score_job`.

### 2.8 `suppression` — the do-not-contact set

```sql
CREATE TABLE suppression (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind         text NOT NULL CHECK (kind IN ('client','lost','dnc','bounced','legal','competitor','manager_veto','b2c','cooldown')),
  key_type     text NOT NULL CHECK (key_type IN ('phone_tail','phone_hash','tg_username','tg_user_id','instagram','domain','stir','company_id')),
  key          text NOT NULL CHECK (length(key) BETWEEN 3 AND 200),
  salt_id      char(8),                       -- phone_hash only: the feed's salt_id the hash was made with
  reason       text CHECK (length(reason) <= 200),
  source       text NOT NULL CHECK (source IN ('erp_feed','outcomes','owner_bot','userbot_reply','manual_import','resolver','retention')),
  erp_at       timestamptz,                   -- the feed entry's `at`
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),   -- stamped by every full pull
  expires_at   timestamptz,                   -- cooldown only
  CONSTRAINT ck_suppression_phone_tail CHECK (key_type <> 'phone_tail' OR key ~ '^[0-9]{9}$'),
  CONSTRAINT ck_suppression_phone_hash CHECK (key_type <> 'phone_hash' OR (key ~ '^[0-9a-f]{20}$' AND salt_id IS NOT NULL)),
  CONSTRAINT ck_suppression_stir       CHECK (key_type <> 'stir' OR key ~ '^[0-9]{9}$'),
  CONSTRAINT ck_suppression_username   CHECK (key_type <> 'tg_username' OR key = cs_username_key(key)),
  CONSTRAINT ck_suppression_domain     CHECK (key_type <> 'domain' OR key = cs_domain_key(key)),
  CONSTRAINT ck_suppression_erp_kinds  CHECK (kind NOT IN ('client','lost') OR source IN ('erp_feed','outcomes','manual_import')),
  CONSTRAINT ck_suppression_cooldown   CHECK ((kind = 'cooldown') = (expires_at IS NOT NULL)),
  CONSTRAINT uq_suppression_key UNIQUE (kind, key_type, key)
);
CREATE INDEX ix_suppression_lookup    ON suppression (key_type, key);
CREATE INDEX ix_suppression_feed_seen ON suppression (last_seen_at) WHERE source = 'erp_feed';
CREATE INDEX ix_suppression_expiry    ON suppression (expires_at) WHERE expires_at IS NOT NULL;
```

Kinds: `client`/`lost` from the ERP feed (hashed phones); `dnc` («yozmang», owner button), `bounced` (wrong number ×2 — the first time is `reason='wrong_number_1'` on a `cooldown` of 30 d, the second makes it permanent), `legal` (removal request), `competitor`, `manager_veto` («bu bizga kerak emas»), `b2c`, `cooldown` (lost for price: 180 d).

Lookup (`services/suppression.is_suppressed(session, keys) -> Hit | None`): the gate builds `keys = {phone_tail, phone_hash(current secret), tg_username, tg_user_id, instagram, domain, stir, company_id}` and runs **one** query `WHERE (key_type, key) IN (…) AND (expires_at IS NULL OR expires_at > now()) ORDER BY kind LIMIT 1`. Checked before a card and again inside the push transaction.

Feed sync (`erp_sync` collector): hourly delta = upsert each entry as `(kind, 'phone_hash', h, salt_id, source='erp_feed', erp_at=at, last_seen_at=now())`; nightly full pull stamps `last_seen_at = run_started` on every entry, then `DELETE … WHERE source='erp_feed' AND last_seen_at < run_started` — only when the pull completed and returned ≥ 90 % of the previous count (otherwise health problem `suppression_shrunk`, no delete). A `salt_id` different from `erp_sync.config.suppression.salt_id` (re-minted secret) forces a full pull before any push (gate line `suppression_stale`). A prospect's «yozmang» reaches a manager's account, not the agent, so `dnc` rows are entered by people (owner bot button «🔒 DNC», `/dnc <phone|@user>`, `make import-clients`) or derived from outcomes (`lost_wrong_number`, `lost_no_contact`). W/R: erp_sync, `feedback_job`, bot, resolver (`competitor`), retention / gate, scorer, resolver.

### 2.9 `lead_candidates`, `lead_reviews`

```sql
CREATE TABLE lead_candidates (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id         uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  contact_id         uuid REFERENCES contacts(id) ON DELETE SET NULL,
  state              text NOT NULL DEFAULT 'found' CHECK (state IN ('found','enriching','scored','review','approved',
                       'rejected','pushed','suppressed','excluded','hold')),
  tier               text CHECK (tier IN ('hot','warm','cold')),
  score              smallint CHECK (score BETWEEN 0 AND 100),
  score_breakdown    jsonb,                  -- {"version":1,"groups":{"volume":{"cap":40,"raw":30,"used":30,"signals":[{"key":"vol_stated_3plus","points":30,"signal_id":…}]},…},"negatives":[…],"total":82}
  weights_version    integer,
  primary_source_key text REFERENCES sources(key),     -- the source whose signal created the candidate (precision attribution)
  exclude_reason     text CHECK (exclude_reason IN ('competitor','supplier_cn','b2c','liquidated','state_body',
                       'no_phone_after_retries','owner','merged','foreign')),
  suppressed_by      bigint REFERENCES suppression(id) ON DELETE SET NULL,
  hold_until         timestamptz,
  enrich_attempts    smallint NOT NULL DEFAULT 0 CHECK (enrich_attempts BETWEEN 0 AND 3),
  next_enrich_at     timestamptz,
  external_id        text CHECK (external_id ~ '^cs:[0-9a-f-]{36}:[0-9]{6}$'),
  approved_by        text,                   -- 'owner:<tg_id>' | 'auto:<source_key>'
  approved_at        timestamptz,
  rejected_at        timestamptz,
  pushed_at          timestamptz,
  last_scored_at     timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_lead_candidates_excluded CHECK (state <> 'excluded' OR exclude_reason IS NOT NULL),
  CONSTRAINT ck_lead_candidates_pushed   CHECK (state <> 'pushed' OR (external_id IS NOT NULL AND pushed_at IS NOT NULL)),
  CONSTRAINT ck_lead_candidates_approved CHECK (state NOT IN ('approved','pushed') OR (approved_at IS NOT NULL AND approved_by IS NOT NULL)),
  CONSTRAINT ck_lead_candidates_scored   CHECK (state NOT IN ('scored','review','approved','pushed') OR score IS NOT NULL),
  CONSTRAINT ck_lead_candidates_suppressed CHECK (state <> 'suppressed' OR suppressed_by IS NOT NULL)
);
CREATE UNIQUE INDEX ux_lead_candidates_open ON lead_candidates (company_id)
  WHERE state IN ('found','enriching','scored','review','approved','hold');
CREATE UNIQUE INDEX ux_lead_candidates_external ON lead_candidates (external_id) WHERE external_id IS NOT NULL;
CREATE INDEX ix_lead_candidates_state_score ON lead_candidates (state, score DESC);
CREATE INDEX ix_lead_candidates_enrich_due  ON lead_candidates (next_enrich_at) WHERE state = 'enriching';
CREATE INDEX ix_lead_candidates_hold_until  ON lead_candidates (hold_until) WHERE state = 'hold';
CREATE INDEX ix_lead_candidates_company     ON lead_candidates (company_id);
CREATE INDEX ix_lead_candidates_contact     ON lead_candidates (contact_id);
CREATE INDEX ix_lead_candidates_suppressed  ON lead_candidates (suppressed_by);

CREATE TABLE lead_reviews (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  candidate_id    uuid NOT NULL REFERENCES lead_candidates(id) ON DELETE CASCADE,
  state           text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','asked','approved','declined','expired','merged')),
  card_chat_id    bigint,
  card_message_id bigint,
  card_hash       char(64),                  -- sha256 of the rendered card; edit only when it changed
  asked_at        timestamptz,
  answered_at     timestamptz,
  answer          text CHECK (answer IN ('push','decline','later','dnc','not_b2b','competitor','merge','edit_phone')),
  by_telegram_id  bigint,
  note            text CHECK (length(note) <= 500),
  expires_at      timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_lead_reviews_answered CHECK ((state IN ('approved','declined','merged')) = (answered_at IS NOT NULL)),
  CONSTRAINT ck_lead_reviews_asked    CHECK (state <> 'asked' OR (asked_at IS NOT NULL AND card_message_id IS NOT NULL AND expires_at IS NOT NULL))
);
CREATE UNIQUE INDEX ux_lead_reviews_live ON lead_reviews (candidate_id) WHERE state IN ('pending','asked');
CREATE UNIQUE INDEX ux_lead_reviews_card ON lead_reviews (card_chat_id, card_message_id) WHERE card_message_id IS NOT NULL;
CREATE INDEX ix_lead_reviews_expiry    ON lead_reviews (expires_at) WHERE state = 'asked';
CREATE INDEX ix_lead_reviews_candidate ON lead_reviews (candidate_id);
```

**Candidate state machine** (`services/candidates.TRANSITIONS`, enforced in Python; the DB checks values and invariants only). Tier: `hot` = score ≥ 60 and a usable phone; `warm` = 40–59, or ≥ 60 without phone; `cold` = < 40.

| from | to | by |
|---|---|---|
| found | enriching, scored, excluded, suppressed | resolver creates `found` on the first resolved observation; `score_job` scores at once when a volume signal exists |
| enriching | scored, hold, excluded, suppressed | `enrich_job` (≤ 3 attempts over 14 d; still no phone → `hold`, `exclude_reason` stays NULL) |
| scored | review, enriching, hold, approved, excluded, suppressed | `review` when hot; `enriching` when warm; `hold` when cold; `approved(approved_by='auto:<source>')` when hot and `sources.auto_push` |
| review | approved, rejected, hold, suppressed, excluded | owner answers (`push`→approved, `decline`→rejected, `later`→stays, `dnc`→suppressed + suppression(dnc), `not_b2b`→excluded(b2c), `competitor`→excluded + company flag, `merge`→excluded(merged)) |
| approved | pushed, review, suppressed | `crm_push_job`; three failed attempts → back to `review` with a note |
| hold | scored, excluded, suppressed | a new signal re-scores; 365 d without a signal → row deleted (company kept, R6) |
| pushed · rejected · excluded · suppressed | — | terminal for this row |

A new candidate for the same company may be created only when no open one exists (`ux_lead_candidates_open`) **and**: after `pushed` — the outcome is terminal or absent for 90 d, a new signal is newer than `pushed_at`, and the month differs; after `rejected` — 180 d passed and a new signal arrived; after `suppressed` — the suppression row is gone; after `excluded` with `competitor|supplier_cn|liquidated` — never (owner can clear the company flag by hand).

**Review state machine** (`services/reviews.py`, MIYA `approvals.py` shape + `merged`): `pending` (created by `review_cards_job`, not yet sent) → `asked` (card sent; `expires_at = asked_at + 72 h`) → `approved | declined | merged` (one tap) or `expired` (sweep; candidate → `hold`, `hold_until = now + 7 d`, re-asked at most twice, `reminder_log(kind='review_expired', ref=candidate_id)`). `later` keeps `asked` and adds 24 h; `edit_phone` keeps `asked`, updates the contact and re-renders. Callback data `rv:<review_id>:<answer>`; `tests/test_bot_callbacks.py` demands the parser accept every button the keyboard builds. W/R: `review_cards_job`, bot / bot, `/lidlar`, `/holat`.

### 2.10 `push_log` — every attempt to hand a lead to the ERP

```sql
CREATE TABLE push_log (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  external_id     text NOT NULL CHECK (external_id ~ '^cs:[0-9a-f-]{36}:[0-9]{6}$'),
  attempt         smallint NOT NULL CHECK (attempt BETWEEN 1 AND 3),
  candidate_id    uuid NOT NULL REFERENCES lead_candidates(id) ON DELETE RESTRICT,
  company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
  phone_tail      char(9) NOT NULL CHECK (phone_tail ~ '^[0-9]{9}$'),
  endpoint        text NOT NULL,                        -- '/api/leads/in/agent' | '/api/leads/in/other'
  source_key_sent text NOT NULL CHECK (source_key_sent IN ('agent','other')),
  payload         jsonb,                                -- the body as sent; NULL after 90 d
  payload_sha256  char(64) NOT NULL,
  status          text NOT NULL DEFAULT 'sending' CHECK (status IN ('sending','sent','failed','unknown')),
  http_status     smallint,
  error           text,                                 -- timeout|conn|http_5xx|http_404|http_4xx|breaker
  sent_at         timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  CONSTRAINT uq_push_log_external_attempt UNIQUE (external_id, attempt),
  CONSTRAINT ck_push_log_sent CHECK ((status = 'sent') = (http_status BETWEEN 200 AND 299))
);
CREATE UNIQUE INDEX ux_push_log_delivered ON push_log (external_id) WHERE status IN ('sent','unknown');
CREATE INDEX ix_push_log_phone_sent   ON push_log (phone_tail, sent_at DESC);
CREATE INDEX ix_push_log_company_sent ON push_log (company_id, sent_at DESC);
CREATE INDEX ix_push_log_day          ON push_log (sent_at) WHERE status IN ('sent','unknown');
CREATE INDEX ix_push_log_candidate    ON push_log (candidate_id);
```

**Idempotency rules** (`services/crm_push.py`):

1. `external_id = f"cs:{company_id}:{yyyymm}"`, month = Asia/Tashkent month at claim time.
2. Gate, in one transaction with `SELECT … FOR UPDATE` on the candidate: `state='approved'`; `is_suppressed()` misses; no `sent|unknown` row for this `external_id` (the partial unique index is the real fence, the pre-check only gives a readable refusal); no `sent|unknown` row for `phone_tail` in the last 24 h; no `sent|unknown` row for `company_id` whose outcome is `open` or absent and younger than 90 d; `count(sent|unknown today) < PUSH_DAILY_CAP`; Mon–Sat 09:30–17:30 Tashkent; `CRM_PUSH_ENABLED`; breaker clear; `suppression_stale` clear.
3. Claim: `INSERT push_log (status='sending', attempt=n)` and **commit before the HTTP call**.
4. Response: 2xx → `sent`, candidate → `pushed` (same transaction). 404 → `failed`, `error='http_404'`, trip breaker `crm_push`, health problem `push_404`, no retry. 5xx/timeout/connection → `failed`; the next scheduler tick retries with `attempt+1` after `with_backoff` delays (30 s, 5 min, 30 min); after attempt 3 the candidate returns to `review` with note «CRM javob bermadi». Other 4xx → `failed`, no retry, owner alert.
5. Reconcile: a `sending` row older than 10 min becomes `unknown` (the request may have landed). `unknown` blocks the unique index exactly like `sent`; the nightly outcomes read promotes it to `sent` when the ERP shows the `external_id`, and raises `push_unconfirmed` after 48 h without one. Owner command `/push tekshir <external_id>` marks it `failed` so a new attempt can be claimed.
6. `payload_sha256 = sha256(canonical JSON: sorted keys, no spaces)`. A retry re-sends the identical body; a different body under the same `external_id` in the same month is refused (the ERP keeps the first anyway).
7. ERP-side replay (`same channel + external_id`) answers `{ok:true}` and writes no ledger row — so an accidental double send is harmless but never done on purpose.

### 2.11 `outcomes` — what the ERP did with each push (snapshot, upserted nightly)

```sql
CREATE TABLE outcomes (
  external_id      text PRIMARY KEY,
  intake_id        uuid NOT NULL,                         -- ERP lead_intakes.id
  candidate_id     uuid REFERENCES lead_candidates(id) ON DELETE SET NULL,
  company_id       uuid REFERENCES companies(id) ON DELETE SET NULL,
  source_key       text REFERENCES sources(key),          -- copied from the candidate for precision
  landing          text NOT NULL CHECK (landing IN ('created','joined','client','dropped')),
  landing_reason   text,                                  -- replay|capped|no_contact|suppressed|… (ERP free text)
  lead_id          uuid,
  client_id        uuid,
  stage_kind       text CHECK (stage_kind IN ('open','won','lost')),
  stage_name       text,
  lost_reason      text,
  closed_at        timestamptz,
  contacted_at     timestamptz,
  contact_kind     text CHECK (contact_kind IN ('call','telegram','note','stage','followup')),
  quoted_volume_m3 numeric(12,3),
  erp_at           timestamptz NOT NULL,                  -- the feed row's `at`
  label            text NOT NULL CHECK (label IN ('won','contacted_open','open_untouched','stale_open',
                     'lost_no_contact','lost_price','lost_not_importer','lost_small','lost_wrong_number',
                     'lost_competitor','lost_other','landed_client','landed_joined',
                     'dropped_capped','dropped_suppressed','dropped_other')),
  precision_class  text NOT NULL CHECK (precision_class IN ('positive','negative','pending','neutral')),
  label_changed_at timestamptz NOT NULL DEFAULT now(),
  first_read_at    timestamptz NOT NULL DEFAULT now(),
  read_at          timestamptz NOT NULL DEFAULT now(),
  raw              jsonb NOT NULL                          -- the feed row verbatim (contract: no name/phone/note in it)
);
CREATE INDEX ix_outcomes_source_first_read ON outcomes (source_key, first_read_at);
CREATE INDEX ix_outcomes_company ON outcomes (company_id);
CREATE INDEX ix_outcomes_label   ON outcomes (label);
CREATE INDEX ix_outcomes_candidate ON outcomes (candidate_id);
```

**Label derivation** (`services/outcomes.derive_label`, pure; patterns are one dict `LOST_REASON_PATTERNS`, lower-cased `re.search`, flag `u`; `NO_CONTACT_REASONS` mirrors the ERP setting `agent_no_contact_reasons`, `;`-split, trimmed, lower-cased):

```python
def derive_label(row: FeedRow, now: datetime) -> tuple[Label, PrecisionClass]:
    if row.landing == "client":                          return "landed_client", "neutral"      # + suppression(client) for push_log.phone_tail
    if row.landing == "dropped":
        if row.reason == "capped":                       return "dropped_capped", "neutral"     # eligible again next month
        if row.reason == "suppressed":                   return "dropped_suppressed", "neutral" # our list was stale -> full pull
        return "dropped_other", "neutral"
    if row.stage_kind == "won" or row.client_id:         return "won", "positive"
    if row.stage_kind == "lost":
        r = (row.lost_reason or "").strip().lower()
        if r in NO_CONTACT_REASONS:                      return "lost_no_contact", "negative"   # + suppression(lost) — also arrives via the feed
        for label, pat in LOST_REASON_PATTERNS.items():  # lost_price: narx|цена|qimmat|дорог ; lost_not_importer: import qilmaydi|не импорт|xitoy emas
            if pat.search(r):                            # lost_small: hajm|kichik|мал ; lost_wrong_number: raqam|номер|noto.g.ri
                return label, PRECISION[label]           # lost_competitor: kargo|cargo|konkurent|конкурент
        return "lost_other", "neutral"
    if row.landing == "joined" and row.stage_kind == "open" and not row.contacted_at:
                                                         return "landed_joined", "neutral"
    if row.contacted_at:
        return "contacted_open", ("positive" if now - row.contacted_at >= timedelta(days=14) else "pending")
    return ("stale_open", "neutral") if now - row.at >= timedelta(days=30) else ("open_untouched", "pending")

PRECISION = {"lost_price": "neutral", "lost_not_importer": "negative", "lost_small": "negative",
             "lost_wrong_number": "negative", "lost_competitor": "negative"}
```

Side effects of a label change (same transaction as the upsert): `lost_wrong_number` → `suppression(bounced|cooldown)` on `push_log.phone_tail` (×2 rule); `lost_competitor` → `companies.is_competitor=true`; `lost_not_importer` → `suppression(cooldown, 365 d, company_id)`; `lost_price` → `suppression(cooldown, 180 d, company_id)`; `landed_client` → `companies.state='client'` + `suppression(client, phone_tail, source='outcomes')`. `source_precision_job`: `precision_30d = positives / (positives + negatives)` over outcomes whose `first_read_at` is within 30 d, ignoring `pending`/`neutral`, per `source_key`. W/R: `feedback_job` / `source_precision_job`, `/holat`, `tools/fit_weights.py`.

### 2.12 `interactions`, `chat_monitors` — the Telegram reader

```sql
CREATE TABLE interactions (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tg_chat_id          bigint NOT NULL,
  tg_message_id       bigint NOT NULL,
  sender_id           bigint,
  sender_username     text CHECK (sender_username = cs_username_key(sender_username)),
  sender_name         text CHECK (length(sender_name) <= 120),
  sender_bio          text CHECK (length(sender_bio) <= 300),
  text                text CHECK (length(text) <= 4000),
  occurred_at         timestamptz NOT NULL,
  media_kind          text NOT NULL DEFAULT 'none' CHECK (media_kind IN ('none','photo','document','video','other')),
  reply_to_message_id bigint,
  prefilter_hit       text[] NOT NULL CONSTRAINT ck_interactions_prefilter_nonempty CHECK (cardinality(prefilter_hit) > 0),   -- {volume_kub, container, china, intent, wholesale, pricelist}
  processed_at        timestamptz,
  evidence_id         bigint REFERENCES evidence(id) ON DELETE SET NULL,
  observation_id      bigint REFERENCES observations(id) ON DELETE SET NULL,
  text_purged_at      timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_interactions_tg_message UNIQUE (tg_chat_id, tg_message_id)
);
CREATE INDEX ix_interactions_unprocessed    ON interactions (created_at) WHERE processed_at IS NULL;
CREATE INDEX ix_interactions_sender         ON interactions (sender_id, occurred_at DESC);
CREATE INDEX ix_interactions_chat_occurred  ON interactions (tg_chat_id, occurred_at DESC);
CREATE INDEX ix_interactions_text_retention ON interactions (occurred_at) WHERE text IS NOT NULL;
CREATE INDEX ix_interactions_evidence       ON interactions (evidence_id);
CREATE INDEX ix_interactions_observation    ON interactions (observation_id);

CREATE TABLE chat_monitors (
  id                    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tg_chat_id            bigint NOT NULL UNIQUE,
  chat_type             text NOT NULL CHECK (chat_type IN ('private','group','channel')),
  title                 text,
  username              text,
  role                  text NOT NULL DEFAULT 'market' CHECK (role IN ('market','competitor','buyers','other')),
  monitor_enabled       boolean NOT NULL DEFAULT true,         -- MIYA default flipped: groups/channels on, private off by rule
  decided_by            text CHECK (decided_by IN ('owner','rule:seed','rule:private_off','rule:default')),
  asked_at              timestamptz,
  joined_at             timestamptz,
  backfill_requested_at timestamptz,
  backfill_done_at      timestamptz,
  backfill_attempts     smallint NOT NULL DEFAULT 0,
  monitoring_since      timestamptz,
  last_seen_message_id  bigint,
  last_seen_at          timestamptz,
  seen_count            integer NOT NULL DEFAULT 0,            -- every message seen (counted, never stored)
  hit_count             integer NOT NULL DEFAULT 0,            -- prefilter hits stored in interactions
  member_count          integer,
  flood_until           timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
```
The "no full chat logs" rule is structural: the userbot inserts only messages whose text matched a prefilter pattern (`cardinality(prefilter_hit) > 0`); everything else only increments `seen_count`. `pricelist` hits are stored so the tout rule (≥ 3 price lists per sender in 60 d) can be counted; they are purged at 30 d like every unconverted row. Member lists and profile photos are never read. W/R: userbot / `telegram_monitor` collector (no network: it reads this table), retention, `/chats`.

### 2.13 `heartbeats`, `usage_log`, `reminder_log`

```sql
CREATE TABLE heartbeats (                                   -- MIYA verbatim
  component    text PRIMARY KEY,                             -- bot|worker|userbot|backup|job:<id>|collector:<key>
  last_seen_at timestamptz NOT NULL,
  detail       jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE usage_log (                                    -- MIYA adapted: every paid call, not only Anthropic
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider          text NOT NULL CHECK (provider IN ('anthropic','brave','yandex_search','google_places','apify','proxy','searxng','hh','other')),
  model             text,                                    -- model id | apify actor | places SKU
  operation         text NOT NULL CHECK (operation IN ('classify','verdict','company_verdict','draft','enrich_search',
                      'serp','places','actor','proxy_bytes','other')),
  input_tokens      integer, output_tokens integer, cache_read_tokens integer, cache_write_tokens integer,
  units             numeric(14,3),                            -- queries, results, MB for non-token providers
  batch             boolean NOT NULL DEFAULT false,
  cost_usd          numeric(12,6) NOT NULL CHECK (cost_usd >= 0),  -- unknown price never reaches here: costs.ask refuses it
  source_key        text REFERENCES sources(key),
  company_id        uuid REFERENCES companies(id) ON DELETE SET NULL,
  model_job_id      bigint REFERENCES model_jobs(id) ON DELETE SET NULL,
  collector_run_id  bigint REFERENCES collector_runs(id) ON DELETE SET NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_usage_log_created_at       ON usage_log (created_at);
CREATE INDEX ix_usage_log_provider_created ON usage_log (provider, created_at);
CREATE INDEX ix_usage_log_company          ON usage_log (company_id);
CREATE INDEX ix_usage_log_model_job        ON usage_log (model_job_id);
CREATE INDEX ix_usage_log_collector_run    ON usage_log (collector_run_id);

CREATE TABLE reminder_log (                                 -- MIYA verbatim
  id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind    text NOT NULL,                                     -- health:<problem>|review_expired|push_unconfirmed|digest|source_paused
  ref     text NOT NULL,
  sent_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_reminder_log_kind_ref_sent ON reminder_log (kind, ref, sent_at);
```
`costs.ask(operation, provider, estimate)` sums `usage_log.cost_usd` for today (Tashkent) and the month against `DAILY_COST_CAP_USD`/`MONTHLY_COST_CAP_USD` and the per-provider allocation; refuses before the call. W/R: every paid caller, `health.beat` / `/xarajat`, `/holat`, health.

### 2.14 `merge_log`, `purge_log`

```sql
CREATE TABLE merge_log (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_company_id uuid NOT NULL,                              -- no FK: the source row stays (state=merged) but may be purged later
  target_company_id uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
  decided_by        text NOT NULL,                              -- resolver:stir | resolver:phone | resolver:domain | resolver:fuzzy:<score> | owner:<tg_id>
  match_score       smallint CHECK (match_score BETWEEN 0 AND 100),
  moved             jsonb NOT NULL,                             -- {"contacts":[uuid…],"signals":[id…],"aliases":[…],"company_ids":[["hh_employer","123"]],"observations":[…],"candidates":[uuid…]}
  carried           jsonb NOT NULL DEFAULT '{}'::jsonb,         -- fields copied onto target because it lacked them: {"stir":"…","website_domain":"…"}
  created_at        timestamptz NOT NULL DEFAULT now(),
  undone_at         timestamptz,
  undone_by         text
);
CREATE INDEX ix_merge_log_source ON merge_log (source_company_id);
CREATE INDEX ix_merge_log_target ON merge_log (target_company_id);

CREATE TABLE purge_log (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  subject_kind text NOT NULL CHECK (subject_kind IN ('company','contact','tg_user','interaction_range','retention')),
  subject_hash char(64) NOT NULL,                              -- sha256 of the canonical key: company uuid | phone_tail | tg_user_id | 'range:<from>:<to>' | 'retention:<step>:<date>'
  reason       text NOT NULL CHECK (reason IN ('legal_request','dnc','b2c','retention','owner','merge_cleanup')),
  requested_by text NOT NULL,                                  -- owner:<tg_id> | job:retention
  plan         jsonb NOT NULL,                                 -- counts per table before execution
  executed     jsonb,                                          -- counts removed/anonymised, files removed
  planned_at   timestamptz NOT NULL DEFAULT now(),
  executed_at  timestamptz,
  error        text
);
CREATE INDEX ix_purge_log_subject ON purge_log (subject_kind, subject_hash);
```
Merge (`services/companies.merge_into`, MIYA `people.merge_into` shape): refused when both have different STIRs or different registry-confirmed domains; re-points every FK row of the source, records their ids in `moved`, copies identity fields the target lacks (`carried`), demotes the source's open candidate to `excluded(merged)`, sets `source.state='merged', merged_into=target`. Undo within 24 h re-points the ids in `moved` and clears `carried` fields; later than that the owner splits by hand. Purge (`services/purge.plan_company/execute`, MIYA shape): count first, then delete contacts/observations/interactions/model_jobs of the subject, anonymise `push_log.payload`, keep `outcomes` and hashed `suppression`; the log row never holds the name or phone, only the hash.

### 2.15 `sheet_rows` (optional, Sheets mirror)

```sql
CREATE TABLE sheet_rows (                                   -- MIYA adapted: tabs, kinds, text entity_id
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tab             varchar(32) NOT NULL CHECK (tab IN ('Kompaniyalar','Lidlar','Yuborilganlar','Manbalar')),
  entity_kind     varchar(16) NOT NULL CHECK (entity_kind IN ('company','candidate','push','source')),
  entity_id       text NOT NULL,
  row_number      integer CHECK (row_number IS NULL OR row_number >= 2),
  content_hash    char(64) NOT NULL,
  state           varchar(16) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','synced','failed','tombstone','cleared')),
  attempts        smallint NOT NULL DEFAULT 0,
  next_attempt_at timestamptz,
  last_error      varchar(64),
  synced_at       timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_sheet_rows_entity UNIQUE (tab, entity_kind, entity_id)
);
CREATE UNIQUE INDEX ux_sheet_rows_row_number ON sheet_rows (tab, row_number) WHERE row_number IS NOT NULL;
CREATE INDEX ix_sheet_rows_due ON sheet_rows (next_attempt_at) WHERE state IN ('pending','failed','tombstone');
```
The mirror carries company-tier columns only (name, city, goods, band, score, state, source, dates); never a person name or phone.

### 2.16 Triggers attached by `0001`

```sql
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON sources         FOR EACH ROW EXECUTE FUNCTION cs_touch_updated_at();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON companies       FOR EACH ROW EXECUTE FUNCTION cs_touch_updated_at();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON contacts        FOR EACH ROW EXECUTE FUNCTION cs_touch_updated_at();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON lead_candidates FOR EACH ROW EXECUTE FUNCTION cs_touch_updated_at();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON chat_monitors   FOR EACH ROW EXECUTE FUNCTION cs_touch_updated_at();
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON sheet_rows      FOR EACH ROW EXECUTE FUNCTION cs_touch_updated_at();
CREATE TRIGGER trg_append_only BEFORE DELETE ON push_log  FOR EACH ROW EXECUTE FUNCTION cs_append_only();
CREATE TRIGGER trg_append_only BEFORE DELETE ON outcomes  FOR EACH ROW EXECUTE FUNCTION cs_append_only();
CREATE TRIGGER trg_append_only BEFORE DELETE ON merge_log FOR EACH ROW EXECUTE FUNCTION cs_append_only();
CREATE TRIGGER trg_append_only BEFORE DELETE ON purge_log FOR EACH ROW EXECUTE FUNCTION cs_append_only();
CREATE TRIGGER trg_append_only BEFORE DELETE ON usage_log FOR EACH ROW EXECUTE FUNCTION cs_append_only();
CREATE FUNCTION cs_scoring_weights_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'scoring_weights is append-only'; END IF;
  IF OLD.approved_at IS NOT NULL AND (NEW.points, NEW.group_cap, NEW.decay_days, NEW.decayed_factor, NEW.group_key)
       IS DISTINCT FROM (OLD.points, OLD.group_cap, OLD.decay_days, OLD.decayed_factor, OLD.group_key)
  THEN RAISE EXCEPTION 'approved scoring_weights version % is frozen', OLD.version; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_scoring_weights_frozen BEFORE UPDATE OR DELETE ON scoring_weights FOR EACH ROW EXECUTE FUNCTION cs_scoring_weights_frozen();
```

## 3. Evidence files on disk

- Root `EVIDENCE_DIR=/data/evidence` (compose volume `evidence:/data/evidence`, uid 10001, mode 0750). Path `<yy>/<sha256>.gz`, `yy` = two-digit year of `fetched_at` in Tashkent, `sha256` = hash of the **raw** bytes before gzip (level 6). Written to `<path>.tmp` then `os.replace` — a crash never leaves a half file.
- One file per distinct body: the same bytes at two URLs are two `evidence` rows and one file. Deletion only when no row with `raw_path IS NOT NULL` shares the `sha256`.
- Cap 2 MB per body; larger bodies get `raw_path=NULL`, `excerpt` and `bytes` still recorded, `meta.truncated=true` on the run.
- Not part of `pg_dump` backups (MIYA `backup.py` unchanged). Losing the directory loses nothing a card or ERP note needs — `url`, `fetched_at`, `excerpt` live in the row. Optional `restic` to object storage is an ops choice (§9).
- API: `services/evidence.store(session, *, source_key, url, body: bytes, content_type, fetched_at, source_ref, excerpt, title, lang, published_at, collector_run_id) -> Evidence`; `open_raw(evidence) -> bytes | None`; `purge_raw(evidence) -> bool`.

## 4. Retention (`retention_job`, daily 04:15 Tashkent; each step its own transaction and its own `purge_log(subject_kind='retention')` row)

| Step | Rule | SQL / pseudocode |
|---|---|---|
| R1 raw evidence 180 d | file goes, row stays | `SELECT id, sha256, raw_path FROM evidence WHERE expires_at < now() AND raw_path IS NOT NULL LIMIT 5000` → for each: if `NOT EXISTS (SELECT 1 FROM evidence e2 WHERE e2.sha256 = sha256 AND e2.raw_path IS NOT NULL AND e2.id <> id)` unlink file → `UPDATE evidence SET raw_path = NULL, raw_deleted_at = now() WHERE id = …` |
| R2a unconverted Telegram posts 30 d | delete | `DELETE FROM interactions WHERE occurred_at < now() - interval '30 days' AND (observation_id IS NULL OR observation_id IN (SELECT id FROM observations WHERE status = 'rejected'))` |
| R2b converted Telegram posts 90 d | keep ids, drop text | `UPDATE interactions SET text = NULL, sender_bio = NULL, sender_name = NULL, text_purged_at = now() WHERE occurred_at < now() - interval '90 days' AND text IS NOT NULL` |
| R3 observation scrub 30 d after resolution | person data out of `parsed` | `UPDATE observations SET parsed = (parsed - 'phones' - 'person') \|\| jsonb_build_object('scrubbed', true), scrubbed_at = now() WHERE scrubbed_at IS NULL AND coalesce(resolved_at, created_at) < now() - interval '30 days'` |
| R4 unanswered leads 90 d | company-tier only | candidates `pushed` with `pushed_at < now() - 90 d` and outcome `label IN ('open_untouched','stale_open','dropped_capped','dropped_other')` or no outcome row → `services/contacts.anonymise(contact)`: `full_name, role, telegram_id, email, telegram_username, instagram := NULL; phone := NULL WHERE tier='person' AND phone_kind='mobile'; anonymised_at := now()` |
| R5 excluded persons 30 d | company-tier only | person-tier contacts of companies whose open candidate is `excluded`/`rejected` for 30 d, or company `state='excluded'` → `anonymise(contact)` |
| R6 ledgers | | `UPDATE push_log SET payload = NULL WHERE sent_at < now() - 90 d AND payload IS NOT NULL`; `UPDATE model_jobs SET request = NULL, pseudonyms = NULL WHERE status IN ('applied','failed','fallback') AND coalesce(applied_at, created_at) < now() - 30 d`; `DELETE FROM collector_runs WHERE started_at < now() - 180 d`; `DELETE FROM reminder_log WHERE sent_at < now() - 90 d`; `DELETE FROM lead_candidates WHERE state = 'hold' AND updated_at < now() - 365 d`; `DELETE FROM suppression WHERE kind = 'cooldown' AND expires_at < now() - 30 d` |
| R7 never | | `suppression` (non-cooldown), `outcomes`, `purge_log`, `merge_log`, `usage_log` rows (payload-free) |
| Immediate | legal / DNC | `purge.plan_company(company_id)` → owner confirms → `execute`; leaves `suppression(legal|dnc)` hashes and the `purge_log` row |

## 5. Alembic plan

- `alembic/env.py` = MIYA's minus the pgvector `render_item`; imports `clientscrapper.db.models` to populate `Base.metadata`; `compare_type=True, compare_server_default=True`. `alembic.ini` as MIYA (ruff post-write hook, URL from settings). The compose `init` service runs `alembic upgrade head`; `worker`, `bot`, `userbot` `depends_on: init: condition: service_completed_successfully`.
- **`0001_initial`** (`revision="0001_initial"`, `down_revision=None`): `upgrade()` in this order — extension `pg_trgm`; functions (`cs_phone_tail`, `cs_username_key`, `cs_domain_key`, `cs_touch_updated_at`, `cs_append_only`, `cs_scoring_weights_frozen`); tables exactly in the order of §2 (`sources, collector_runs, evidence, companies, company_aliases, company_ids, contacts, observations, model_jobs, signals, scoring_weights, suppression, lead_candidates, lead_reviews, push_log, outcomes, interactions, chat_monitors, heartbeats, usage_log, reminder_log, merge_log, purge_log, sheet_rows`); triggers; `op.bulk_insert` of the 19 `sources` rows and the 26 `scoring_weights` version-1 rows (`proposed_by='seed'`, `approved_by='seed'`, `approved_at=now()`). `downgrade()` drops in reverse, then functions, then the extension. The migration carries its own frozen tuples (CHECK lists) — `tests/test_enums_frozen.py` compares them with `db/enums.py`.
- Later migrations: `000N_<slug>.py`, one concern each; widening a CHECK = `op.drop_constraint` + `op.create_check_constraint` with the new tuple, plus the tuple change in `enums.py` in the same commit; new source = a data migration inserting the `sources` row; new weight version = never a migration (runtime through `/vazn` or `fit_weights`).
- `tests/test_migrations.py` (needs postgres; skips loudly, never silently): on an empty scratch database `upgrade head`, then `alembic.autogenerate.compare_metadata(ctx, Base.metadata) == []`, then `downgrade base` leaves zero tables in `public`, then `upgrade head` again.

## 6. SQLAlchemy layout

```
clientscrapper/db/__init__.py        Base, SessionLocal, engine, session_scope, get_session   # VENDORED_FROM dailyJournal@<sha> miya/db/__init__.py
clientscrapper/db/base.py            MIYA verbatim (Base, pg_enum, created_at_column, money_column)
                                     + Base.metadata.naming_convention {ix, uq, ck, fk, pk} + updated_at_column()
clientscrapper/db/session.py         MIYA verbatim; imports clientscrapper.config.settings
clientscrapper/db/enums.py           frozen tuples + sql_list(); no native enums. SOURCE_FAMILIES, RUN_STATUSES,
                                     OBSERVATION_STATUSES, COMPANY_STATES, REGISTRY_STATUSES, VOLUME_BANDS, LEGAL_FORMS,
                                     CONTACT_TIERS, PHONE_KINDS, LEGAL_BASES, ID_TYPES, SIGNAL_KEYS, DERIVED_KEYS,
                                     WEIGHT_GROUPS, CANDIDATE_STATES, CANDIDATE_TIERS, EXCLUDE_REASONS, REVIEW_STATES,
                                     REVIEW_ANSWERS, PUSH_STATUSES, PUSH_SOURCE_KEYS, LANDINGS, STAGE_KINDS,
                                     CONTACT_KINDS, OUTCOME_LABELS, PRECISION_CLASSES, SUPPRESSION_KINDS,
                                     SUPPRESSION_KEY_TYPES, SUPPRESSION_SOURCES, CHAT_TYPES, CHAT_ROLES, MEDIA_KINDS,
                                     PROVIDERS, OPERATIONS, JOB_KINDS, JOB_LANES, JOB_STATUSES, PURGE_SUBJECTS,
                                     PURGE_REASONS, SHEET_TABS, SHEET_ENTITY_KINDS, SHEET_ROW_STATES
clientscrapper/db/models/__init__.py re-exports every model; the only import alembic/env.py needs
clientscrapper/db/models/sources.py     Source, CollectorRun
clientscrapper/db/models/evidence.py    Evidence, Observation
clientscrapper/db/models/companies.py   Company, CompanyAlias, CompanyId, Contact
clientscrapper/db/models/signals.py     Signal, ScoringWeight
clientscrapper/db/models/leads.py       LeadCandidate, LeadReview, PushLog, Outcome
clientscrapper/db/models/suppression.py Suppression
clientscrapper/db/models/telegram.py    Interaction, ChatMonitor
clientscrapper/db/models/ops.py         Heartbeat, UsageLog, ReminderLog, ModelJob, MergeLog, PurgeLog, SheetRow
```
Model conventions (MIYA): `Mapped[...]` annotations; `relationship(lazy="raise")` everywhere; JSONB typed `dict[str, Any]`; `Decimal` for numeric; every index/constraint named in `__table_args__`; a comment says **why** the column exists. State transition dicts live in `services/candidates.py` and `services/reviews.py`, not in models. Raw SQL writers (userbot `already_stored` insert, `costs._record`, `health.beat`) are allowed; they rely on the triggers above, not on ORM hooks.

## 7. MIYA table → clientScrapper table

| MIYA (`miya/db/models.py`) | clientScrapper | How |
|---|---|---|
| `people` (`display_name`, `aliases[]` GIN, `telegram_id`, `telegram_username`, `phone`) | `companies` + `company_aliases` + `contacts` | **adapted**: the person becomes a company with a separate alias table (trgm GIN instead of array GIN) and ≤ 1 person-tier contact |
| `client_codes`, `code_mentions` | — | not ported (suppression by GS code is not in the v1 key set) |
| `chat_monitors` | `chat_monitors` | **adapted**: CHECK instead of native enum, default on for groups, `role`, `hit_count`, no vision/docs/style columns |
| `interactions` | `interactions` | **adapted**: trimmed to Telegram, unique `(tg_chat_id, tg_message_id)` kept (`ux_interactions_tg_message` → `uq_interactions_tg_message`), prefilter-only inserts |
| `interactions.media` / `conversation_windows` (`custom_id`, `batch_id`, `attempts`, `status`) | `evidence` / `model_jobs` | **new** (evidence: url + sha + excerpt + file) / **adapted** (batch lifecycle generalised to any model call) |
| `interactions.media->approval` (approvals.py) | `lead_reviews` | **new table, copied state machine** `pending → asked → approved|declined|expired` + `merged` |
| `heartbeats` | `heartbeats` | **verbatim** |
| `usage_log` | `usage_log` | **adapted**: `provider` CHECK widened to every paid API, `units`, attribution FKs, `cost_usd NOT NULL`, no `audio_seconds` |
| `reminder_log` | `reminder_log` | **verbatim** |
| `sheet_rows` | `sheet_rows` | **adapted**: tabs/kinds, `entity_id text` |
| `question_log`, `recap_digests`, `daily_reports` | — | not ported (owner digest is computed, not stored) |
| `outbox_messages`, `jarvis_*`, `reply_rules`, `escalations`, `owner_style_*`, `knowledge_entries` | — | **not ported** (no sender in v1) |
| `debts`, `debt_payments`, `promises`, `transactions`, `transaction_evidence`, `events`, `tasks`, `claims`, `memories`, `passages`, `receipts` | — | not ported (domain) |
| — | `sources`, `collector_runs`, `observations`, `company_ids`, `signals`, `scoring_weights`, `lead_candidates`, `push_log`, `outcomes`, `suppression`, `merge_log`, `purge_log` | **new** (`merge_log`/`purge_log` follow `people.merge_into` and `purge.PurgePlan` shapes) |

## 8. Worked example — one company, observation → pushed lead → outcome (synthetic)

All values are invented: chat `@namuna_optom`, company «Namuna Tekstil Savdo MChJ», phone `+998900000001`, STIR `300000011`. Ids shortened.

| # | table | row |
|---|---|---|
| 1 | `evidence` | `id=7001, source_key='telegram_monitor', url='https://t.me/namuna_optom/18234', source_ref='tg:-1001000000001:18234', fetched_at='2026-10-20 07:41+05', published_at=same, sha256='3f9c…e1', content_type='text/plain', bytes=142, raw_path='26/3f9c…e1.gz', excerpt='Guangzhoudan 25 kub mato olib kelish kerak, kim qiladi? @namuna_tekstil', lang='uz', expires_at='2027-04-18 07:41+05'` |
| 2 | `interactions` | `tg_chat_id=-1001000000001, tg_message_id=18234, sender_id=5100000001, sender_username='namuna_tekstil', sender_name='Rustam', sender_bio='Namuna Tekstil Savdo MChJ direktori', text='Guangzhoudan 25 kub …', occurred_at='2026-10-20 07:41+05', prefilter_hit='{volume_kub,china,intent}', processed_at='2026-10-20 07:41:40+05', evidence_id=7001, observation_id=9001` |
| 3 | `observations` | `id=9001, evidence_id=7001, ordinal=0, source_key='telegram_monitor', status='resolved', parsed=<§2.5 JSON: kind intent_post, name_key 'namuna tekstil savdo', phones [+998900000001 mobile person], person {Rustam, direktor}, hints volume_m3 25 stated china_words true>, company_id='0f0e0d0c-…', contact_id='c0ffee00-…', resolved_at='2026-10-20 07:46+05'` |
| 4 | `companies` | `id='0f0e0d0c-…', display_name='Namuna Tekstil Savdo MChJ', name_key='namuna tekstil savdo', legal_form='MChJ', stir='300000011' (after registry step), city='Toshkent', telegram_channel=NULL, registry_status='active', registered_on='2019-03-11', sectors='{textile}', goods='{mato,furnitura}', segment='D', volume_band='3_10', state='active', first_seen_at='2026-10-20 07:46+05', last_signal_at='2026-10-20 07:41+05', best_score=82` (+ `company_aliases` rows `'Namuna Tekstil Savdo MChJ'`, `'НАМУНА ТЕКСТИЛЬ САВДО'`; `company_ids` `('tg_user','5100000001')`) |
| 5 | `contacts` | `id='c0ffee00-…', company_id='0f0e0d0c-…', tier='person', full_name='Rustam', role='direktor', phone='+998900000001', phone_tail='900000001' (generated), phone_kind='mobile', telegram_username='namuna_tekstil', telegram_id=5100000001, is_decision_maker=true, legal_basis='public_source', source_url='https://t.me/namuna_optom/18234', source_key='telegram_monitor', collected_at='2026-10-20 07:46+05'` |
| 6 | `signals` | `(company 0f0e…, evidence 7001, key='vol_stated_3plus', value=25.000, quote='25 kub mato olib kelish kerak', observed_at='2026-10-20 07:41+05', expires_at=+180 d, source_key='telegram_monitor', model=NULL)`; `(…, key='cn_origin', quote='Guangzhoudan', …)`; `(…, evidence 7002 (registry page), key='ent_legal_confirmed', value=NULL, observed_at='2026-10-20 07:52+05', expires_at=NULL, source_key='registry_lookup')`; `(…, evidence 7002, key='ent_age_2y_active', value=7)` |
| 7 | `lead_candidates` | `id='1ead0001-…', company_id='0f0e…', contact_id='c0ffee00-…', state: found (07:46) → scored (07:53) → review (07:55) → approved (09:12) → pushed (09:31); tier='hot', score=82, weights_version=1, primary_source_key='telegram_monitor', score_breakdown={"version":1,"groups":{"volume":{"cap":40,"raw":30,"used":30},"origin":{"cap":20,"raw":20,"used":20},"solidity":{"cap":15,"raw":12,"used":12},"recency":{"cap":10,"raw":10,"used":10},"contact":{"cap":10,"raw":10,"used":10},"corroboration":{"cap":5,"raw":0,"used":0}},"negatives":[],"total":82}, approved_by='owner:111111111', approved_at='2026-10-20 09:12+05', external_id='cs:0f0e0d0c-…:202610', pushed_at='2026-10-20 09:31+05'` |
| 8 | `lead_reviews` | `id=501, candidate_id='1ead0001-…', state='approved', card_chat_id=111111111, card_message_id=4521, card_hash='9a…', asked_at='2026-10-20 07:55+05', expires_at='2026-10-23 07:55+05', answer='push', answered_at='2026-10-20 09:12+05', by_telegram_id=111111111` |
| 9 | `push_log` | `external_id='cs:0f0e0d0c-…:202610', attempt=1, candidate_id='1ead0001-…', company_id='0f0e…', phone_tail='900000001', endpoint='/api/leads/in/agent', source_key_sent='agent', payload={external_id, name:'Rustam (direktor) — Namuna Tekstil Savdo MChJ', phone:'+998900000001', kub:'25', firma:'Namuna Tekstil Savdo MChJ', stir:'300000011', telegram:'@namuna_tekstil', imports:'mato, furnitura', score:'82', evidence:'https://t.me/namuna_optom/18234', utm_source:'clientscrapper', note:'NEGA TOPILDI: 20.10.2026 @namuna_optom: «Guangzhoudan 25 kub mato olib kelish kerak, kim qiladi?» …'}, payload_sha256='7b…', status='sent', http_status=200, sent_at='2026-10-20 09:31:02+05', finished_at='2026-10-20 09:31:03+05'` |
| 10 | `outcomes` | night 1: `external_id='cs:0f0e…:202610', intake_id='a1b2…', landing='created', lead_id='d4e5…', stage_kind='open', contacted_at=NULL, label='open_untouched', precision_class='pending', erp_at='2026-10-20 09:31:03+05'` → night 2 upsert: `contacted_at='2026-10-20 11:04+05', contact_kind='call', label='contacted_open', precision_class='pending'` → 2026-11-06 upsert: `stage_kind='won', client_id='f6a7…', quoted_volume_m3=25.000, label='won', precision_class='positive', label_changed_at='2026-11-06 03:00+05'` |

Follow-on rows the `won` label produces: `companies.state='client'`; the next nightly full pull of the suppression feed brings `suppression(kind='client', key_type='phone_hash', key=erp_phone_hash(secret,'900000001'), salt_id='<8 hex>', source='erp_feed')`, so the same phone is never carded again; `sources.telegram_monitor.positives_30d += 1`.

## 9. Open questions

1. STIR checksum: does the 9-digit STIR carry a check digit? UNVERIFIED — until confirmed `stir_key` validates shape only.
2. The agent's own DNC phones are stored as plain `phone_tail` (needed for lookups without the ERP secret). The legal memo prefers hashes; a peppered hash needs one more secret (`SUPPRESSION_PEPPER`) outside the agreed env list. Decide: (a) plain tail-9 in v1 — recommended, the table never leaves the server; (b) peppered hash.
3. `outcomes` is a snapshot. Keep a history table (one row per label change) for weight fitting, or is `label_changed_at` enough for v1?
4. `lost_price` cooldown 180 d and `lost_not_importer` 365 d — owner to confirm the two windows.
5. ERP feed id types (`intake_id`, `lead_id`, `client_id`) are assumed uuid strings — UNVERIFIED until `/api/agent/outcomes` ships on `agent-feed`.
6. Evidence directory backup: excluded from `pg_dump` backups by design; is a weekly `restic` copy wanted (≈ 1–3 GB/year)?
7. Is the Sheets mirror (`sheet_rows`) part of v1 or dropped from `0001`?
8. May `MANAGER_TELEGRAM_IDS` answer review cards (`lead_reviews.by_telegram_id` ≠ owner), or owner only in v1?

## 10. Acceptance criteria

1. `alembic upgrade head` on an empty PG16 creates 24 tables, 6 functions, 1 extension; `compare_metadata` returns `[]`; `downgrade base` then `upgrade head` both succeed (`tests/test_migrations.py`).
2. `tests/test_enums_frozen.py`: every CHECK tuple in `0001` equals its `db/enums.py` tuple; `SIGNAL_KEYS ∪ DERIVED_KEYS` equals the `scoring_weights` key CHECK; version 1 has one row per key, group caps sum to 100, and is approved.
3. `tests/test_keys.py`: Python `phone_tail/username_key/domain_key` agree with `cs_*` SQL functions on ≥ 40 fixtures (Cyrillic, `t.me/`, `www.`, IDN, bots, short numbers).
4. Constraint tests, each shown red by dropping the constraint in a string-edited copy: second live person-tier contact per company refused; person-tier row without `legal_basis/source_url/collected_at` refused; `full_name` containing a 14-digit run refused; two `sent|unknown` rows for one `external_id` refused; `pushed` candidate without `external_id` refused; `phone_hash` suppression without `salt_id` refused; `excerpt` > 600 refused; `interactions` row with empty `prefilter_hit` refused; `UPDATE scoring_weights SET points` on an approved version refused; `DELETE FROM push_log` refused.
5. `tests/test_candidate_states.py` and `tests/test_review_states.py` enumerate `TRANSITIONS`; every illegal move raises; `ux_lead_candidates_open` refuses a second open candidate per company.
6. `tests/test_push_idempotency.py` against a fake ERP: crash between claim and response leaves `sending` → reconciled to `unknown` → no second send; 404 trips the breaker; 5xx retries ≤ 3 with the same `payload_sha256`; a different body under the same `external_id` is refused.
7. `tests/test_outcomes_labels.py`: `derive_label` table-driven over all 16 labels, including the 14-day and 30-day boundaries in Asia/Tashkent; side effects (`suppression(client)`, `bounced ×2`, `is_competitor`) asserted.
8. `tests/test_retention.py` on fixtures aged by string-edited timestamps: R1 removes the file and keeps the row; R2a deletes an unconverted post at 31 d and keeps a converted one; R2b nulls text at 91 d; R3 scrubs `phones`/`person`; R4 anonymises the person-tier contact at 91 d after an untouched push and leaves the company-tier row; every step writes a `purge_log` row.
9. `tests/test_pii_columns.py` and `tests/test_fk_indexes.py` pass over `Base.metadata`; `tests/test_pseudonymised_requests.py` finds no raw phone or handle in `model_jobs.request`.
10. `tests/fixtures/example_flow.sql` loads §8 rows 1–6; `tests/test_example_flow.py` drives score → review → approve → push (fake ERP) → outcomes upsert and ends with `outcomes.label='won'`, `companies.state='client'`, one `suppression(client)` row.
