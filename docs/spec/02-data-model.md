# 02 — Data model (PostgreSQL 16)

Scope: every table of the shared contract, its DDL, who writes and reads it, the JSON shapes it holds, the evidence files on disk, retention, Alembic and the ORM layout. Names here are canonical; `03`–`07` reference them. Shared context (business, budget, ERP contract, env names) is assumed, not restated.

## 0. Conventions

| Rule | Detail |
|---|---|
| Database | One PG16 database, schema `public`. Extension `pg_trgm` (alias search). `gen_random_uuid()` is core since PG13 — no `pgcrypto`. |
| Primary keys | `uuid` where the id leaves the system (`companies`, `contacts`, `lead_candidates`; `external_id` embeds the company uuid). `bigint GENERATED ALWAYS AS IDENTITY` for ledgers (`evidence`, `observations`, `signals`, `interactions`, logs). Natural keys: `sources.key`, `heartbeats.component`, `runtime_flags.key`, `outcomes.external_id`. |
| Time | Every timestamp is `timestamptz`; business day is Asia/Tashkent in code. `created_at` = MIYA `created_at_column()`; `updated_at` = `created_at_column(onupdate=sa.func.now())` **plus** trigger `cs_touch_updated_at`, because the bot, userbot and worker also write raw SQL (ORM `onupdate` fires only on ORM updates). |
| Enumerations | **CHECK constraints**, never native enums. Allowed values are frozen tuples in `clientscrapper/db/enums.py` and rendered with `sql_list()` (MIYA `models.py` `_sql_list` / `SheetRow` pattern). A migration keeps its own frozen copy; `tests/test_enums_frozen.py` holds the two equal. `pg_enum()` stays in `base.py` unused. |
| Names | `ix_<table>_<cols>` index, `ux_<table>_<what>` unique (partial) index, `uq_<table>_<cols>` unique constraint, `ck_<table>_<rule>` check, `fk_<table>_<col>` foreign key, `pk_<table>`. MIYA names every constraint explicitly; we do the same and additionally set `Base.metadata.naming_convention` so an autogenerate diff is empty. Inline column CHECKs written unnamed in this document are declared in the ORM as `CheckConstraint(..., name="ck_<table>_<column>")`. |
| Numbers | Cost `numeric(12,6)` USD; volume `numeric(12,3)` m³; score `smallint 0–100`. Never float for money. |
| FK hygiene | Every FK with `CASCADE`/`SET NULL`/`RESTRICT` has an index (MIYA WP-23; RESTRICT is checked on every parent delete too). `tests/test_fk_indexes.py` walks `Base.metadata`. **No CHECK spans an `ON DELETE SET NULL`/`SET DEFAULT` column** (fullprompt #809: the referential action runs, the CHECK then fails the parent's DELETE): such a rule is a RESTRICT FK or a trigger instead (§2.4 `merged_into`, §2.9 `trg_lead_candidates_suppressed`); `tests/test_migrations.py::test_no_check_spans_a_set_null_foreign_key_column` reads the catalog. |
| Key CHECKs | A column «stored only as its key» reads `col IS NOT DISTINCT FROM cs_key(col)`, never `col = cs_key(col)`: the key functions answer NULL for what they refuse, and a CHECK reads the NULL of `x = NULL` as passed. A NULL column stays legal (§2.4, §2.8, §2.12). |
| PII placement | Person-tier fields live only in `contacts`; transiently in `interactions.sender_*`, `observations.parsed`, `push_log.payload`, `model_jobs.pseudonyms` — each with a retention step (§4). `tests/test_pii_columns.py` keeps an allowlist of such columns; any other column whose name matches `phone|name|username|bio|email` fails the test. |
| Append-only | `push_log`, `outcomes`, `merge_log`, `purge_log`, `usage_log` refuse `DELETE` through trigger `cs_append_only` (retention nulls columns, never deletes rows). |

## 1. Key normalisation — one home

Python (`clientscrapper/services/keys.py`) is the writer of every normalised key and the home of every function below — the phone ones too (`phone_e164`, `phone_tail`, `phone_kind`), with `site_key`, `loose_key`, `instagram_key`, `stir_key`, `name_key` and `MOBILE_CODES`/`LANDLINE_CODES`; `services/normalise.py` re-exports the three phone functions (04 §1.1 spells them as its own) and turns free text into these keys. SQL mirrors exist for CHECKs, generated columns and ad-hoc queries. `tests/test_keys.py` runs both over one fixture list and demands equality.

```python
def phone_tail(raw: str | None) -> str | None:
    """Digits only; >= 9 digits -> last 9, else None. Identical to the ERP's inboundPhoneDigits."""

def phone_e164(raw: str | None, *, city: str | None = None) -> str | None:
    """'+998XXXXXXXXX' for a UZ number of exactly 9 national digits (04 §1.1 rules 1–8: a bare
    9-digit number gets +998; 7 digits only when city is Toshkent); '+<digits>' for a foreign one;
    otherwise None. A UZ number needs nine national digits, so no short code is ever produced."""

def phone_kind(e164: str) -> Literal["mobile", "landline", "short", "unknown"]:
    """By the two digits after +998 (04 §1.1, keys.MOBILE_CODES / LANDLINE_CODES): 33 77 88 90 91
    93 94 95 97 98 99 -> mobile; 61 62 65 66 67 69 70–79 except 77 -> landline; 20/50/55 -> unknown until 04 Q4
    confirms them; < 7 national digits -> short (never produced from phone_e164's output — the
    value stays in the enum for a hand-entered contact); a foreign number or an unlisted code -> unknown."""

def username_key(raw: str | None) -> str | None:
    """Strip https://, www., t.me/, telegram.me/ (the prefix in either case, ASCII only), '@',
    anything from '/', '?', '#'; lowercase; must match ^[a-z0-9_]{5,32}$ and not end with 'bot'."""

def instagram_key(raw: str | None) -> str | None:
    """Strip instagram.com/, '@', trailing '/', query; lowercase; ^[a-z0-9._]{1,30}$."""

def domain_key(raw: str | None) -> str | None:
    """urlsplit host (or the raw string when no scheme); lowercase; IDNA-encode; strip every leading 'www.';
    None for social/marketplace hosts (instagram.com, t.me, telegram.me, facebook.com, olx.uz,
    uzum.uz, youtube.com, wa.me) — those are handles, not websites."""

def stir_key(raw: str | None) -> str | None:
    """9 digits, first digit in 2..6, not all equal. UNVERIFIED — check whether a STIR checksum exists;
    v1 validates shape only."""

def name_key(raw: str) -> str:
    """fold_apostrophes -> transliterate (Cyrillic->Latin) -> lower -> drop diacritics -> strip «»“”" -> strip legal forms
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
          '^([A-Za-z]+://)?([Ww][Ww][Ww]\.)?([Tt]\.[Mm][Ee]/|[Tt][Ee][Ll][Ee][Gg][Rr][Aa][Mm]\.[Mm][Ee]/)?@?',
          ''), '[/?#].*$', '')) AS u) s $$;

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

Two designed differences between `domain_key` and `cs_domain_key`, both so a stored value is a fixed point of its CHECK `website_domain IS NOT DISTINCT FROM cs_domain_key(website_domain)`: SQL does not IDNA-encode (Python stores the encoded value), and SQL strips **one** leading `www.` while Python strips every one — `cs_domain_key('www.www.x.uz')` is `www.x.uz`, whose own key is `x.uz`, so the SQL function is not idempotent and a byte-exact mirror would write a row its CHECK refuses. A later migration may make the SQL strip `'^(www\.)+'` (§5); until then Python never writes such a value. `cs_username_key` matches the scheme, `www.` and the `t.me`/`telegram.me` host in either case as ASCII classes (not the `i` flag), the same letters Python folds.

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

Seeded in `0001` — **23 rows**, `enabled=false` unless stated. **Schedulable units** (the runner reads `enabled/paused_until/daily_page_budget/daily_cost_cap_usd` by `Collector.key`, 03 §1.2): `telegram_monitor`, `instagram_discovery`, `olx_business`, `uzum_sellers`, `directories` (family `directory`), `hh_vacancies`, `exhibitors`, `maps` (family `maps`), `brand_dealers`, `serp_layer` (family `search`), `registry_lookup`, `erp_sync`, `manual`, `enrich_site` (family `search`, `enabled=true`, `cron=NULL`: pages fetched by enrichment, 04 §3). **Attribution-only sub-sources** (`cron=NULL`, never scheduled; they exist so `evidence.source_key`, `observations.source_key`, `contacts.source_key` and `usage_log.source_key` can name the engine or directory that produced a row): `directories_goldenpages`, `directories_yellowpages`, `directories_glotr`, `directories_prom`, `maps_2gis`, `maps_google`, `search_brave`, `search_yandex`, `search_searxng`. `erp_sync.config = {"suppression":{"salt_id":null,"last_full_at":null,"last_delta_at":null,"count":0},"outcomes":{"cursor":null,"last_at":null}}`.
Auto-push rule stored here: `auto_push` may be true only when `pushes_30d >= 15 AND precision_30d >= 0.60` — `source_precision_job` (05 §B.2) flips it both ways weekly, except that it never flips it **on** while `config.auto_push_forbidden` is true (the owner's `src:<key>:a` in 06 §1.2 can only forbid, never grant).
A parent row's dollar budget counts its sub-sources' spend: `Budget.usd = daily_cost_cap_usd − spend(source_key, with_subsources=True)` (`costs.source_budget_usd`, the one home), where `costs.SUBSOURCE_PREFIXES = {"serp_layer": ("search_",), "directories": ("directories_",), "maps": ("maps_",)}` — a call is recorded under the sub-source it was made for (`search_brave`) and budgeted on the parent (`serp_layer`), and the runner refuses a paid call attributed to anything but the run's row or one of those sub-sources (03 §1.2 `UnattributedCall`). A host breaker (03 §1.1) writes **both** pause pairs on the run's row: `paused_until` (the runner skips the row) and `breaker_until` (06 §4.2 `collector_blocked` reads it), each with reason `host_blocked:<host>`; on `erp_sync`, `breaker_until` is also the push/feed breaker (`push_404`, `erp_redirect`, 02 §2.10 rule 4).
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
  error            text,                                      -- an error code (timeout|host_blocked|budget|…), never a body; a host breaker ends the run status='failed', error='host_blocked' (there is no 'blocked' status)
  meta             jsonb NOT NULL DEFAULT '{}'::jsonb,         -- runner extras (03 §1.2): unchanged, skipped_robots, truncated (evidence stored without a file), and when set: note, variant, fetch_truncated (answers cut at 5 MB), captcha, duplicates, serp_hits, dropped {bad_evidence, bad_observation}, exhausted (pages|usd|deadline|cost_cap|refused|model_unavailable), refused (the costs.ask code), blocked_hosts, problem, sources (per sub-source counts), deadline_at; live smoke runs write note='smoke'
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
  meta             jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {etag, last_modified, final_url, status, truncated}: conditional-fetch inputs and fetch facts (03 §1.1)
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
  website_domain   text CHECK (website_domain IS NOT DISTINCT FROM cs_domain_key(website_domain)),
  telegram_channel text CHECK (telegram_channel IS NOT DISTINCT FROM cs_username_key(telegram_channel)),   -- the @company channel
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
  merged_into      uuid REFERENCES companies(id) ON DELETE RESTRICT,   -- RESTRICT: ck_companies_merged spans it (§0)
  dup_of           uuid REFERENCES companies(id) ON DELETE SET NULL,   -- resolver's 80–89 fuzzy band (04 §2.2): the «🔀 Birlashtir» proposal; cleared by merge or by the owner
  dup_score        smallint CHECK (dup_score BETWEEN 0 AND 100),        -- no CHECK pairing it with dup_of: ON DELETE SET NULL clears dup_of alone
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
CREATE INDEX ix_companies_dup_of         ON companies (dup_of) WHERE dup_of IS NOT NULL;

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
                'tg_chat','instagram','facebook','yandex_org','myorg','goldenpages','yellowpages',
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
  telegram_username text CHECK (telegram_username IS NOT DISTINCT FROM cs_username_key(telegram_username)),
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
Rules: **a company row is never deleted** — purge sets `state='purged'` (06 §5.4) — so `merged_into` is `ON DELETE RESTRICT` and `ck_companies_merged` is the lineage rule; a merge target is pinned by `merge_log.target_company_id` RESTRICT as well. Purging a merged source sets `state='purged'` **and** `merged_into = NULL` in the same UPDATE, or the CHECK refuses it. `ux_contacts_one_person` is the "one person per company" law; a second decision maker replaces the first (old row anonymised, not deleted). Company-tier rows hold switchboard phones, `info@`, the @company handle. `google_place` id_value is the only thing kept from Places for longer than 30 days (policy): a Places phone copied into `contacts` (`source_key='maps_google'`) is cleared by retention step R4b (§4) unless a push already used it. A poster's personal Telegram id is person-tier — `contacts.telegram_id`, never `company_ids` (`ID_TYPES` has no `tg_user`). W/R: resolver (`services/companies.py`), enrichment, bot `edit_phone` / `/stir`, merge, retention / scorer, card, push, suppression gate.

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
Validators (`normalise.to_observation_json`, the only writer): `name_key` recomputed from `company.name_raw` — except for a person-shaped name, which moves to `person.name` and is replaced by `'<source label> <external id>'` (03 §2.5: `OLX sotuvchi <olx_user_id>`, `Uzum sotuvchi <uzum_shop_slug>`) with `name_key = null`: two such labels would fuzzy-match each other and merge two sellers, while the external id resolves them exactly; `phones[].e164`/`kind` recomputed; `excerpt ≤ 600`; a 14-digit run — a maximal run of **exactly** 14 digits, so a 17-digit 2GIS firm id or Graph user id survives — anywhere → the field is dropped and `hints.pinfl_dropped=true`; `person` is kept only when `kind ∈ {intent_post, profile, vacancy, registry, seller}` (`normalise.PERSON_KINDS`; `seller` so a person-shaped OLX `user.name` has a place to live — a directory entry names a firm, not a person); the whole tree is storable — no U+0000, lone surrogates → U+FFFD (`text.storable`), non-finite floats → null, and `hints.volume_m3` treats NaN/∞ as no volume. W/R: collectors (via normaliser) / resolver (`resolve_job`), retention.


#### Observation kinds across sections (mapping)

03's `Observation.kind` is the collector-side vocabulary; `observations.parsed.kind` (this section) is the stored one. The runner maps them once, in `services/normalise.py`:

| 03 `Observation.kind` | collector family | stored `parsed.kind` |
|---|---|---|
| `post` | telegram_monitor, a group post | `intent_post` |
| `post` | telegram_monitor, a channel's own post (`attrs.chat_type='channel'`) | `profile` |
| `company` | instagram_discovery, telegram about (E3) | `profile` |
| `company` | directories, maps, exhibitors (company card) | `directory_entry` |
| `company` | brand_dealers | `dealer_page` |
| `company` | registry lookups (E4), tenders | `registry` |
| `company` | a page fetched under a `search_*` engine row | `directory_entry` |
| `company` | any other source (`enrich_site`, a fetched company site) | `profile` |
| `company` | olx_business / uzum_sellers seller profile | `seller` |
| `listing` | olx_business, marketplaces, B2B catalogues | `listing` |
| `vacancy` | hh_vacancies | `vacancy` |
| `exhibitor` | exhibitors | `exhibitor` |
| `place` | maps | `place` |
| `serp_hit` | serp_layer | not an observation (`normalise.NOT_OBSERVATIONS`; `parsed_kind` raises): it only enqueues a URL for the owning collector (`sources.config.queue`) and is kept as `evidence` with `source_key='search_*'` — the card or site fetch that follows emits the observation (04 §1.6) |
| `suppression`, `outcome` | erp_sync | not observations: written straight to `suppression` / `outcomes` (05 §B.2) |

Rows whose `source_key = 'maps_google'` get `expires_at = fetched_at + 30 d` instead of the default 180 d (Maps terms); R4b clears any phone copied from them that produced no push.

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
  CONSTRAINT ck_scoring_weights_approved CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
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
| ent_legal_confirmed | solidity (15) | 8 | — | a registry hit (enrichment E4) only — never a bare number parsed from a page (04 §1.5/§5.3) |
| ent_age_2y_active | solidity | 4 | — | registry: registered ≥ 2 y, active |
| ent_not_micro | solidity | 3 | — | >1 vacancy, branches, ≥20 SKUs, several phones, or a parsed STIR/legal block without a registry hit (04 §5.3) |
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

Group caps sum to 100. `score = max(0, min(100, Σ capped positive groups) + Σ negatives)`; a signal past `expires_at` counts `points × decayed_factor`. `tests/test_weights_seed.py`: every key in both tuples has exactly one row in version 1, caps sum to 100, version 1 is approved (by the activation UPDATE of the next paragraph, §5).

**Versioning rules.** A version is a complete set (one row per key). Active version = `max(version) WHERE approved_at IS NOT NULL`. Activation = one `UPDATE … SET approved_by, approved_at WHERE version = $1` after `services/scoring.validate_version()` (completeness, caps, each weight within ±30 % of the previous version unless `proposed_by` starts with `owner:`). Trigger `cs_scoring_weights_frozen` (BEFORE UPDATE OR DELETE) refuses every DELETE and, once `approved_at IS NOT NULL`, **any** change to the row (`NEW IS DISTINCT FROM OLD`): no un-approve/edit/re-approve, no move to another version, no edit of `key`, `note`, `proposed_by` or `created_at`, no second approval — `approved_*` can be set only while `approved_at IS NULL`, and `ck_scoring_weights_approved` sets them together. An INSERT into an approved version is not refused by a trigger: the primary key blocks additions to version 1 (every key exists), and completeness of a later version is `validate_version`'s job (T-18). `/vazn key points` copies the active version into `version+1`, applies the change and approves it. `lead_candidates.weights_version` + `score_breakdown` make every score reproducible. W/R: 0001 seed, bot `/vazn`, `tools/fit_weights.py` / `score_job`.

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
  CONSTRAINT ck_suppression_username   CHECK (key_type <> 'tg_username' OR key IS NOT DISTINCT FROM cs_username_key(key)),
  CONSTRAINT ck_suppression_domain     CHECK (key_type <> 'domain' OR key IS NOT DISTINCT FROM cs_domain_key(key)),
  CONSTRAINT ck_suppression_erp_kinds  CHECK (kind NOT IN ('client','lost') OR source IN ('erp_feed','outcomes','manual_import')),
  CONSTRAINT ck_suppression_cooldown   CHECK ((kind = 'cooldown') = (expires_at IS NOT NULL)),
  CONSTRAINT uq_suppression_key UNIQUE (kind, key_type, key)
);
CREATE INDEX ix_suppression_lookup    ON suppression (key_type, key);
CREATE INDEX ix_suppression_feed_seen ON suppression (last_seen_at) WHERE source = 'erp_feed';
CREATE INDEX ix_suppression_expiry    ON suppression (expires_at) WHERE expires_at IS NOT NULL;
```

No SQL shape CHECK exists for the `instagram`, `tg_user_id` and `company_id` key types (there is no SQL Instagram mirror, by design — `services/suppression.py` writes them through `keys.instagram_key` and typed ids); `phone_tail`, `phone_hash`, `stir`, `tg_username` and `domain` have theirs.

Kinds: `client`/`lost` from the ERP feed (hashed phones, `source='erp_feed'`), from outcomes (`won`/`landed_client`/`dropped_suppressed` → `client`, `lost_no_contact` → `lost`; clear `phone_tail` and, for `lost`, the company's other keys; `source='outcomes'`, `reason='outcome:<external_id>'` or `'erp_belt:<external_id>'` — recomputed from the company's current labels, so a label that changes retracts its rows, §2.11) or from the XLSX fallback (`make import-clients` / `/import`: clear `phone_tail`, `source='manual_import'`, 05 §B.3 — retired by the first successful full pull, 05 §B.2 step 5); `dnc` («yozmang», owner button), `bounced` (wrong number ×2 — the first time is `reason='wrong_number_1'` on a `cooldown` of 30 d, the second makes it permanent), `legal` (removal request), `competitor`, `manager_veto` («bu bizga kerak emas»), `b2c`, `cooldown` (lost for price: 180 d).

Lookup (`services/suppression.is_suppressed(session, keys) -> Hit | None`): the gate builds `keys = {phone_tail, phone_hash(current secret), tg_username, tg_user_id, instagram, domain, stir, company_id}` and runs **one** query `WHERE (key_type, key) IN (…) AND (expires_at IS NULL OR expires_at > now()) ORDER BY array_position(ARRAY['legal','dnc','client','lost','bounced','competitor','manager_veto','b2c','cooldown'], kind) LIMIT 1`. The array is rendered from the one constant `KIND_RANK` in `services/suppression.py` (`legal 0 · dnc 1 · client 2 · lost 3 · bounced 4 · competitor 5 · manager_veto 6 · b2c 7 · cooldown 8`, 06 §5.5): a permanent kind always beats a `cooldown`, so the gate and `/lidlar` report the lasting reason. Checked before a card and again inside the push transaction. The keys are built over the company's **lineage** (`companies.lineages`: a merged source's rows count), as the writers store them (seam review 2026-10-24, for T-19): `phone_tail` and `phone_hash = erp_feeds.hash_tail(CRM_AGENT_KEY, tail)` for every live contact tail and the tail to be pushed (a hash row of an older key carries another `salt_id` and cannot match); `domain` = each lineage company's `website_domain` **as stored** — the host `services/outcomes` writes, not `keys.site_key(…)`, which would miss `shop.namuna.uz` stored by an outcome (the resolver keeps one `website_domain` per company and merges hosts of one site by `site_key`, so the stored host is the key both sides hold); `tg_username`, `tg_user_id`, `instagram`, `stir` and `company_id` likewise; a `cooldown` row counts only while `expires_at > now()`.

Feed sync (`erp_sync` collector, steps in 05 §B.2; cron full `30 2 * * *`, delta `15 * * * *` Tashkent; neither runs during the «Boshqa» fallback, 05 §B.3): delta (`since` = the previous delta's ERP `generated_at` − 10 min) = upsert each entry as `(kind, 'phone_hash', h, salt_id, source='erp_feed', erp_at=at, last_seen_at=run_started)` — a delta never retires anything (the ERP cannot express a deletion, 05 §A.7); the full pull (`limit=5000`, **neither `since` nor `cursor`** — the only walk allowed to delete) stamps `last_seen_at = run_started` on every entry, then `DELETE … WHERE source='erp_feed' AND last_seen_at < run_started` — only when the pull completed and saw ≥ 90 % of the distinct `(kind, h)` the previous **completed full pull** saw (otherwise health problem `suppression_shrunk`, no delete; the owner's `--accept-shrink` overrides once). The feed carries nine-digit tails only: a client card of 7–8 digits is refused by the ERP's belt and comes back as `dropped_suppressed`, which leaves a local `phone_tail` row (§2.11). Freshness has one home, `services/erp_feeds.suppression_fresh()` (`FULL_MAX_AGE_H = 26`, `DELTA_MAX_AGE_H = 3`): when it is false, or the feed's `salt_id` differs from `erp_sync.config.suppression.salt_id` (re-minted secret), health problem `suppression_stale` is raised and the push gate refuses every push until a full pull succeeded. A prospect's «yozmang» reaches a manager's account, not the agent, so `dnc` rows are entered by people (owner bot button «🔒 DNC», `/dnc <phone|@user>`, the manager card's «🙅 Yozmang dedi») or derived from outcomes (`lost_wrong_number` ×2 → `bounced`, `lost_no_contact` → `lost`). W/R: erp_sync (feeds and outcomes walks, `services/outcomes.apply_effects`), bot, resolver (`competitor`), retention / gate, scorer, resolver.

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
  approved_by        text,                   -- 'owner:<tg_id>' | 'auto:<source_key>' | 'manager:<tg_id>' (a manager's /tel after a handoff, 06 §1.7)
  approved_at        timestamptz,
  rejected_at        timestamptz,
  pushed_at          timestamptz,
  last_scored_at     timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_lead_candidates_excluded CHECK (state <> 'excluded' OR exclude_reason IS NOT NULL),
  CONSTRAINT ck_lead_candidates_pushed   CHECK (state <> 'pushed' OR (external_id IS NOT NULL AND pushed_at IS NOT NULL)),
  CONSTRAINT ck_lead_candidates_approved CHECK (state NOT IN ('approved','pushed') OR (approved_at IS NOT NULL AND approved_by IS NOT NULL)),
  CONSTRAINT ck_lead_candidates_scored   CHECK (state NOT IN ('scored','review','approved','pushed') OR score IS NOT NULL)
  -- no CHECK on suppressed_by (§0: it is ON DELETE SET NULL); trigger trg_lead_candidates_suppressed (§2.16) refuses
  -- the step INTO 'suppressed' without a pointer, and lets the FK's own SET NULL pass later
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
  kind            text NOT NULL DEFAULT 'owner' CHECK (kind IN ('owner','manager')),   -- 'manager' = the handoff card of 06 §1.7, same table, same machine
  state           text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','asked','approved','declined','expired','merged')),
  card_chat_id    bigint,
  card_message_id bigint,
  card_hash       char(64),                  -- sha256 of the rendered card; edit only when it changed
  asked_at        timestamptz,
  answered_at     timestamptz,
  answer          text CHECK (answer IN ('push','decline','later','dnc','not_b2b','competitor','merge','edit_phone','handoff',
                                         'contacted','said_no','wrong_or_b2c','no_reply')),   -- the last five belong to the handoff flow (06 §1.3/§1.7)
  by_telegram_id  bigint,
  note            text CHECK (length(note) <= 500),
  expires_at      timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_lead_reviews_answered CHECK ((state IN ('approved','declined','merged')) = (answered_at IS NOT NULL)),
  CONSTRAINT ck_lead_reviews_asked    CHECK (state <> 'asked' OR (asked_at IS NOT NULL AND card_message_id IS NOT NULL AND expires_at IS NOT NULL))
);
CREATE UNIQUE INDEX ux_lead_reviews_live ON lead_reviews (candidate_id, kind) WHERE state IN ('pending','asked');   -- one live owner card and one live manager card per candidate
CREATE UNIQUE INDEX ux_lead_reviews_card ON lead_reviews (card_chat_id, card_message_id) WHERE card_message_id IS NOT NULL;
CREATE INDEX ix_lead_reviews_expiry    ON lead_reviews (expires_at) WHERE state = 'asked';
CREATE INDEX ix_lead_reviews_candidate ON lead_reviews (candidate_id);
```

**Candidate state machine** (`services/candidates.TRANSITIONS`, enforced in Python; the DB checks values and invariants only). Tier: `hot` = score ≥ 60 and a usable phone; `warm` = 40–59, or ≥ 60 without phone; `cold` = < 40.

| from | to | by |
|---|---|---|
| found | enriching, scored, excluded, suppressed | resolver creates `found` on the first resolved observation; `score_job` scores at once when a volume signal exists |
| enriching | scored, review, hold, excluded, suppressed | `enrich_job` (≤ 3 attempts over 14 d). After attempt 3 still no phone: `score ≥ 60 AND contacts.telegram_username IS NOT NULL` → `review` with `tier='warm'` and `score_breakdown.gates.no_phone=true` (the owner card renders the handoff variant `[👤 Menejerga]`, 06 §1.3); otherwise `hold`, `exclude_reason` stays NULL |
| scored | review, enriching, hold, approved, excluded, suppressed | `review` when hot; `enriching` when warm; `hold` when cold; `approved(approved_by='auto:<source>')` when hot and `sources.auto_push`; `approved(approved_by='manager:<tg_id>')` when the re-score follows a manager's `/tel` phone (06 §1.7: `hold → scored → approved` — the owner's earlier `handoff` answer is the owner's approval) |
| review | approved, rejected, hold, suppressed, excluded | owner answers (`push`→approved, `decline`→rejected, `later`→stays, `dnc`→suppressed + suppression(dnc), `not_b2b`→excluded(b2c), `competitor`→excluded + company flag, `merge`→excluded(merged), `handoff`→hold with `hold_until = now + 30 d` and a `lead_reviews(kind='manager', state='pending')` row) |
| approved | pushed, review, suppressed | `crm_push_job`; three failed attempts → back to `review` with a note |
| hold | scored, excluded, suppressed | a new signal re-scores; a manager's `/tel` phone re-scores at once; manager answers `said_no` → suppressed + suppression(dnc), `wrong_or_b2c` → excluded(b2c); 365 d without a signal → row deleted (company kept, R6) |
| pushed · rejected · excluded · suppressed | — | terminal for this row |

A new candidate for the same company may be created only when no open one exists (`ux_lead_candidates_open`) **and**: after `pushed` — the outcome is terminal or absent for 90 d, a new signal is newer than `pushed_at`, and the month differs; after `rejected` — 180 d passed and a new signal arrived; after `suppressed` — the suppression row is gone (`suppressed_by IS NULL` on a `suppressed` candidate means exactly that: four writers retire suppression rows — R6, the ERP full pull, the `manual_import` retirement, the outcomes retractions — and the FK's `SET NULL` keeps the candidate); after `excluded` with `competitor|supplier_cn|liquidated` — never (owner can clear the company flag by hand).

**Review state machine** (`services/reviews.py`, MIYA `approvals.py` shape + `merged`; one machine for both `kind`s): `pending` (created by `review_cards_job` — every 2 min inside `REVIEW_HOURS`, 06 §1.6 — from candidates in state `review`; a `kind='manager'` row is created by the owner's `handoff` answer) → `asked` (card sent; `expires_at = asked_at + 72 h`) → `approved | declined | merged` (one tap) or `expired` (sweep; owner card: candidate → `hold`, `hold_until = now + 7 d`, re-asked at most twice, `reminder_log(kind='review_expired', ref=candidate_id)`; manager card: digest line «Menejer javob bermadi»). `later` keeps `asked` and adds 24 h; `edit_phone` keeps `asked`, updates the contact and re-renders. Owner answers `push`/`handoff` → `approved`; `decline`/`dnc`/`not_b2b`/`competitor` → `declined`; `merge` → `merged`. Manager answers (06 §1.7): `contacted` → `approved` (the candidate stays `hold` until `/tel`); `said_no`/`wrong_or_b2c`/`no_reply` → `declined`. The callback-data grammar (`rv:<review_id>:<a>` with one-letter codes, `mh:*` for manager cards) is 06 §1.5's, not restated here; `tests/test_bot_callbacks.py` demands the parser accept every button the keyboards build. W/R: `review_cards_job`, bot / bot, `/lidlar`, `/holat`.

### 2.10 `push_log` — every attempt to hand a lead to the ERP

```sql
CREATE TABLE push_log (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  external_id     text NOT NULL CHECK (external_id ~ '^cs:[0-9a-f-]{36}:[0-9]{6}$'),
  attempt         smallint NOT NULL CHECK (attempt BETWEEN 1 AND 3),
  candidate_id    uuid NOT NULL REFERENCES lead_candidates(id) ON DELETE RESTRICT,   -- deliberate: the append-only ledger keeps its candidate (R6 and purge skip/settle such rows, §4, 06 §5.4)
  company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
  phone_tail      char(9) NOT NULL CHECK (phone_tail ~ '^[0-9]{9}$'),
  endpoint        text NOT NULL,                        -- '/api/leads/in/agent' | '/api/leads/in/other'
  source_key_sent text NOT NULL CHECK (source_key_sent IN ('agent','other')),
  payload         jsonb,                                -- the body as sent; NULL after 90 d
  payload_sha256  char(64) NOT NULL,
  status          text NOT NULL DEFAULT 'sending' CHECK (status IN ('sending','sent','failed','unknown')),
  http_status     smallint,
  error           text,                                 -- timeout|conn|http_5xx|http_404|http_3xx|http_4xx|http_2xx_badbody|breaker (a comment, not a CHECK: http_3xx needs no migration)
  sent_at         timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  superseded_at   timestamptz,                          -- 0002_erp_contract: set by rule 5's re-push or by /push tekshir; frees ux_push_log_delivered without rewriting status/http_status
  CONSTRAINT uq_push_log_external_attempt UNIQUE (external_id, attempt),
  CONSTRAINT ck_push_log_sent CHECK ((status <> 'sent' OR http_status BETWEEN 200 AND 299)                                       -- sent ⇒ 2xx or no HTTP answer (rule 5b: a NULL passes)
                                 AND (http_status IS NULL OR http_status NOT BETWEEN 200 AND 299 OR status IN ('sent','unknown')))   -- 2xx ⇒ sent | unknown (unparseable 2xx body: 'unknown', error='http_2xx_badbody')
);
CREATE UNIQUE INDEX ux_push_log_delivered ON push_log (external_id) WHERE status IN ('sent','unknown') AND superseded_at IS NULL;   -- predicate widened by 0002_erp_contract (0001 has no superseded_at clause)
CREATE INDEX ix_push_log_phone_sent   ON push_log (phone_tail, sent_at DESC);
CREATE INDEX ix_push_log_company_sent ON push_log (company_id, sent_at DESC);
CREATE INDEX ix_push_log_day          ON push_log (sent_at) WHERE status IN ('sent','unknown');
CREATE INDEX ix_push_log_candidate    ON push_log (candidate_id);
```

**Idempotency rules** (`services/crm_push.py`):

1. `external_id = f"cs:{company_id}:{yyyymm}"`, month = Asia/Tashkent month at claim time.
2. Gate, in one transaction with `SELECT … FOR UPDATE` on the candidate: `state='approved'`; `is_suppressed()` misses; no `sent|unknown` row with `superseded_at IS NULL` for this `external_id` (the partial unique index is the real fence, the pre-check only gives a readable refusal); no `sent|unknown` row for `phone_tail` in the last 24 h; no `sent|unknown` row for `company_id` whose outcome is `open` or absent and younger than 90 d; `count(sent|unknown today) < PUSH_DAILY_CAP` (these three count only rows with `superseded_at IS NULL` and a different `external_id` — a rule-5 re-push is the same push, not a second one); Mon–Sat 09:30–17:30 Tashkent; `CRM_PUSH_ENABLED`; `sources.erp_sync.breaker_until` clear (05 §B.1; `/yoq crm` clears it by hand); `suppression_fresh()` true and no `salt_id` mismatch (§2.8).
3. Claim: `INSERT push_log (status='sending', attempt=n)` and **commit before the HTTP call**.
4. Response (05 §B.1 owns the mapping table; the scheme has **two levels**). Inside one attempt the POST is retried up to three times by `with_backoff(attempts=3, base=1.0, cap=8.0)` **only when a re-send cannot land it twice**: the connection never opened (connect error, connect or pool timeout), or a 5xx came back — retried because a re-send of the same `external_id` is fenced by the ERP (replay, rule 7), not because a 5xx proves nothing landed (the ERP's own 503 `{"error":"unavailable"}` normally means no ledger row — rarely the row committed and only its answer was lost, and the retry is then a replay for an arrival that was never announced or clocked: it shows as `created` with no contact, i.e. `open_untouched` → `stale_open` (05 §A.13 (i)) — and a proxy 502/504 can follow a recorded arrival, 05 §B.1; should a half-way failure repeat on every try, each of the ≤ 9 POSTs adds one lenta note to the orphan lead, 05 §A.13 (iii)). A read/write timeout, a read/write error or a protocol error ends the attempt as `unknown`, `error='timeout'`, with **no resend** — the first request may still be running (the ERP chains a second delivery behind it, but that one can time out too). Then: 2xx `{ok:true}` → `sent`, candidate → `pushed` (same transaction); 2xx with an unparseable body → `unknown`, `error='http_2xx_badbody'` (rule 5 settles it). 404 → `failed`, `error='http_404'`, `sources.erp_sync.breaker_until = now + 6 h`, `breaker_reason='push_404'`, health problem `push_404`, no retry. 3xx (a proxy redirect, e.g. Caddy's 308 to an `http://` `CRM_BASE_URL`) → never followed, `failed`, `error='http_3xx'`, the same breaker with `breaker_reason='erp_redirect'`, health problem `erp_redirect` (05 §B.1). Still failing after the three tries → `failed`; the next **attempt** (`attempt+1`, identical body) may be claimed only after `ATTEMPT_DELAYS = {2: 5 min, 3: 30 min}`; after attempt 3 the candidate returns to `review` with note «CRM javob bermadi» (≤ 9 POSTs worst case). Other 4xx → `failed`, no retry, owner alert.
5. Reconcile (`reconcile_job` every 10 min + the nightly outcomes walks, 05 §B.2): (a) a `sending` row older than 10 min becomes `unknown` (the request may have landed); (b) `unknown` blocks the unique index exactly like `sent`; any outcomes walk that shows its `external_id` — the 10-minute `unknown` walk (`since` = its `sent_at` − 10 min) or a nightly one — turns it `sent` (`http_status` stays as it was; the CHECK allows a NULL) and its candidate `approved → pushed`; (c) **the 48-hour rule**, run only when the feeds are configured and that night's `cursor` and `recent` walks both completed: a row with `status IN ('sent','unknown')`, `superseded_at IS NULL`, `sent_at` between now − 30 d and now − 48 h and **no `outcomes` row** — `sent` rows too, because the «Boshqa» fallback door answers 200 to a landing that threw (05 §B.3) and a pre-`aefbf278` build did the same on the agent door — raises `push_unconfirmed` (count) and, **at most once per `external_id`** (no superseded row for it yet), when `payload IS NOT NULL` and `attempt < 3`: `superseded_at = now()` on the row and `push_one(repush_of=row.id)` claims `attempt + 1` with the identical payload through the ordinary gate (suppression, freshness, breaker, window, `CRM_PUSH_ENABLED`; its `state='approved'` check reads `state IN ('approved','pushed')` for a re-push of the candidate's own `external_id` — a `sent` row's candidate is already `pushed`, which stays terminal). The ERP lands it (the first one was lost) or replays it (it was recorded but the feed cannot see it) — either is its answer. No outcome after another 48 h → no second re-push: an `approved` candidate goes to `review` with note «CRM tasdiqlamadi», a `pushed` one stays, and `push_unconfirmed` stays until `/push tekshir`; a row with `attempt = 3` or no payload is only flagged; (d) owner command `/push tekshir <external_id>` (06 §1.2) sets `superseded_at` on the delivered row — a 2xx row can never become `failed` under `ck_push_log_sent` — and runs the same `push_one(repush_of=…)` at once (refused when `attempt = 3`, when the payload is gone, or when the ERP already reported the id).
6. `payload_sha256 = sha256(canonical JSON: sorted keys, no spaces)`. A retry re-sends the identical body; a different body under the same `external_id` in the same month is refused (the ERP keeps the first anyway).
7. ERP-side replay (`same channel + external_id`, once the first delivery FINISHED, whatever its outcome) answers `{ok:true}` and writes no ledger row; a second delivery that arrives while the first is still running waits for it inside the ERP (a per-id chain per copy of the landing module — each channel has one lander, production runs one app process) and is then that replay (05 §A.2); the wait has no bound, so a first delivery that never settles holds it until the ERP restarts, and the agent's read timeout ends that try as `unknown` (05 §A.13 (iv)). So an accidental double send is harmless, but the agent never sends one on purpose (rule 4: no resend after a read timeout).

### 2.11 `outcomes` — what the ERP did with each push (snapshot, upserted by the outcomes walks)

```sql
CREATE TABLE outcomes (
  external_id      text PRIMARY KEY,
  intake_id        uuid NOT NULL,                         -- ERP lead_intakes.id (a uuid: the ERP's outcomes cursor is keyed on it, 05 §A.6)
  candidate_id     uuid REFERENCES lead_candidates(id) ON DELETE SET NULL,
  company_id       uuid REFERENCES companies(id) ON DELETE SET NULL,
  source_key       text REFERENCES sources(key),          -- copied from the candidate for precision
  landing          text NOT NULL CHECK (landing IN ('created','joined','client','dropped')),   -- the feed's `outcome`
  landing_reason   text,                                  -- the feed's `reason`: suppressed|capped|no_phone|no_contact for dropped, NULL otherwise (a replay writes no ERP row, so never 'replay')
  lead_id          uuid,
  client_id        uuid,
  stage_kind       text CHECK (stage_kind IN ('open','won','lost')),
  stage_name       text,
  lost_reason      text,                                  -- only a LABEL (the owner's dictionary or agent_no_contact_reasons), ≤ 200; a seller's free text arrives as NULL (05 §A.9)
  no_contact       boolean NOT NULL DEFAULT false,        -- 0002_erp_contract: the ERP's own «lost with a listed no-contact reason»
  closed_at        timestamptz,
  contacted_at     timestamptz,
  contact_kind     text CHECK (contact_kind IN ('call','telegram','note','stage','followup')),
  quoted_volume_m3 numeric(12,3),
  erp_at           timestamptz NOT NULL,                  -- the feed row's `at` (milliseconds)
  label            text NOT NULL CHECK (label IN ('won','contacted_open','open_untouched','stale_open',
                     'lost_no_contact','lost_price','lost_not_importer','lost_small','lost_wrong_number',
                     'lost_competitor','lost_other','landed_client','landed_joined',
                     'dropped_capped','dropped_suppressed','dropped_other')),
  precision_class  text NOT NULL CHECK (precision_class IN ('positive','negative','pending','neutral')),
  label_changed_at timestamptz NOT NULL DEFAULT now(),
  first_read_at    timestamptz NOT NULL DEFAULT now(),
  read_at          timestamptz NOT NULL DEFAULT now(),
  raw              jsonb NOT NULL                          -- the feed row verbatim: exactly the 15 fields of 05 §A.9, no name/phone/note
);
CREATE INDEX ix_outcomes_source_first_read ON outcomes (source_key, first_read_at);
CREATE INDEX ix_outcomes_company ON outcomes (company_id);
CREATE INDEX ix_outcomes_label   ON outcomes (label);
CREATE INDEX ix_outcomes_candidate ON outcomes (candidate_id);
```

Rows are written only for an `external_id` the agent pushed (a `push_log` row exists); the feed's
other rows — the permanent «Boshqa» clause lets any holder of that key post `cs:` ids — are counted as
`foreign` and dropped (05 §B.2). `candidate_id`, `company_id`, `source_key` come from that
`push_log` row.

**Label derivation** (`services/outcomes.derive_label`, pure; patterns are one dict
`LOST_REASON_PATTERNS`, lower-cased `re.search`, flag `u`). There is **no `NO_CONTACT_REASONS`
mirror**: the ERP answers `no_contact` itself, so the agent keeps no copy of the setting.

```python
def derive_label(row: FeedRow, *, pushed_at: datetime, now: datetime) -> tuple[Label, PrecisionClass]:
    # pushed_at = sent_at of the push_log row that delivered this external_id (never row.at: `at`
    # moves on any touch of the lead — a board re-order, an owner change, a joined landing)
    if row.outcome == "client":                          return "landed_client", "neutral"      # fallback door only (05 §A.3)
    if row.outcome == "dropped":
        if row.reason == "capped":                       return "dropped_capped", "neutral"     # eligible again next month
        if row.reason == "suppressed":                   return "dropped_suppressed", "neutral" # the ERP belt refused this phone
        return "dropped_other", "neutral"                                                       # no_phone | no_contact | anything new
    if row.stage_kind == "won":                          return "won", "positive"               # NOT client_id: it stays set on a lead revived from won
    if row.stage_kind == "lost":
        if row.no_contact:                               return "lost_no_contact", "negative"
        r = " ".join((row.lost_reason or "").split()).lower()     # a label or "" (free text never leaves the ERP)
        for label, pat in LOST_REASON_PATTERNS.items():  # lost_price: narx|цена|qimmat|дорог ; lost_not_importer: import qilmaydi|не импорт|xitoy emas
            if pat.search(r):                            # lost_small: hajm|kichik|мал ; lost_wrong_number: raqam|номер|noto.g.ri
                return label, PRECISION[label]           # lost_competitor: kargo|cargo|konkurent|конкурент
        return "lost_other", "neutral"
    # open, or no stage (a lead the ERP removed): the contact decides
    if row.contacted_at:
        return "contacted_open", ("positive" if now - row.contacted_at >= timedelta(days=14) else "pending")
    if row.outcome == "joined":                          return "landed_joined", "neutral"
    return ("stale_open", "neutral") if now - pushed_at >= timedelta(days=30) else ("open_untouched", "pending")

PRECISION = {"lost_price": "neutral", "lost_not_importer": "negative", "lost_small": "negative",
             "lost_wrong_number": "negative", "lost_competitor": "negative"}
```

What the inputs mean (05 §A.4, §A.9): `contacted_at` is stamped for an agent arrival for 90 days
after it arrived, from the earliest evidence — but «earliest wins» only during the arrival's first
24 h (a late-uploaded call can move it earlier only then; afterwards the stamp is final, and a day-1
correction can LOWER the row's `at`, which only the `since` walks catch). Evidence counts as follows:
`call` and `telegram` by anybody (a call from 2 minutes before the arrival); `note`, `stage` and
`followup` only by the responsible person — the lead's current owner or the arrival's assignee,
anybody when both are NULL; the «📞 Bogʻlandim» button's note by whoever pressed it — but a press by
the responsible person first clears the lead's booking when it is today or earlier, and that audit row
is `followup` evidence, earlier than the note, so such a press reads `contact_kind='followup'`. A
CREATED agent lead is always booked for today, so the routed seller's press on it reads `followup`; a
JOINED push lands on an existing lead whose `next_action_at` stays whatever it was — NULL or a future
date clears nothing and the press reads `note`, today or earlier is cleared and reads `followup`. The
note decides (`note`) also when the presser is not responsible (the office on a seller's lead) or the
date was already moved past today (05 §A.4). So
`stale_open` (30 d) really means «nobody the ERP can see called»; for a
fallback (`other`) push the look-back is 7 days, so after day 7 it means «unknown». A `joined` push
into a conversation contacted in the last 24 h never gets a `contacted_at` (it lands
`landed_joined`). A `joined` row may also be the agent's own orphan lead (a 503 half-way, then the
retry joined it, 05 §A.2) — nothing here reads `joined` as «a person already had this company»; and a
`created` row that never gets a `contacted_at` may be an arrival whose ledger row committed while its
answer was lost, never announced to a seller (05 §A.13 (i)) — it ages into `stale_open` like any other.
Before the owner keeps a lost-reason dictionary every `lost_reason` is NULL **except** a text equal,
after normalisation, to an `agent_no_contact_reasons` label (e.g. «Qayta qilinmasin» typed before the
dictionary existed), which the ERP returns verbatim; `derive_label` is unaffected, because
`no_contact` decides first. So lost rows are `lost_no_contact` or `lost_other`; the regex classes
match labels only, and `lost_not_importer` / `lost_wrong_number` / `lost_competitor` / `lost_small` /
`lost_price` need the owner's dictionary labels (01 §7 question 1).

**A renamed dictionary label** (verified on `agent-feed` `d138b3bc`): `saveLostReason` UPDATEs
`lost_reasons.label` in place, and the feed's `lost_reason` compares each lead's stored text with the
CURRENT labels (or the setting). After a rename every old lead carrying the old text comes back with
`lost_reason: null` — unless `agent_no_contact_reasons` lists that text — whenever a walk next reads
it (its `at` does not move, so the monthly `year` walk, or any later touch of the lead). Recomputed
naïvely, `lost_price`/`lost_not_importer`/`lost_small`/`lost_wrong_number`/`lost_competitor` would
turn `lost_other` and `apply_effects` would retract their 180-/365-day cooldowns and shift precision.
So the outcomes upsert (T-10, before `derive_label`) reads such a row as **unchanged**: a lost row
whose `lost_reason` goes from a label to `null` while `stage_kind` stays `lost` and `closed_at` is
unchanged keeps its stored `lost_reason` and label (`label_changed_at` does not move, `apply_effects`
retracts nothing); `raw` still stores the row verbatim. The owner rule «deactivate + create, never
rename» (05 §A.14 step 2) stays the first defence; this rule is what keeps a rename that happened
anyway from reopening companies.

**Side effects are derived, not appended** (`services/outcomes.apply_effects(session, company_id)`,
same transaction as the upsert, run for every company a page touched). The desired set is computed
from the company's CURRENT labels over all its `outcomes` rows; the company's
`suppression(source='outcomes')` rows (found by `reason LIKE 'outcome:%' OR reason LIKE 'erp_belt:%'`
with an `external_id` of that company) that are not in the set are deleted, missing ones inserted:

| Label | Desired rows / flags |
|---|---|
| `won`, `landed_client` | `suppression(client, phone_tail = push_log.phone_tail, reason='outcome:<external_id>')`; `companies.state='client'` — the ERP feed brings the hashed `phone_hash` row for the same phone a day later (05 §B.2), so the phone is never carded again even while the feed lags |
| `dropped_suppressed` | `suppression(client, phone_tail, reason='erp_belt:<external_id>')` — the ERP refused this phone: a stale feed copy, or a 7–8-digit card the feed can never carry (05 §A.8); no `companies.state` change (the belt does not say client or lost); health problem `suppression_stale_hit`, cleared by the next completed full pull |
| `lost_no_contact` | `suppression(lost, …)` on the pushed `phone_tail` and the company's `tg_username`, `instagram`, `domain`, `stir`, `reason='outcome:<external_id>'` (the ERP suppresses by phone only) |
| `lost_price` / `lost_not_importer` | `suppression(cooldown, company_id, expires_at = label_changed_at + 180 d / 365 d)` |
| `lost_wrong_number` | `suppression(bounced)` or `suppression(cooldown)` on `push_log.phone_tail` (×2 rule, §2.8) |
| `lost_competitor` | `companies.is_competitor = true` |

**Retractions** follow from the recompute: a lead revived out of `lost` in the ERP (it also leaves the
ERP's suppression set at the next full pull, 05 §A.13), a reason changed to another label, or a lead
moved from won back to open drops the rows that label justified — the ERP's decision to work it
again is the truth for its own labels. `companies.state` returns from `client` to its previous state
(`active` unless excluded) only when no `client` row for the company remains from any source. Kept
stricter **on purpose**, never retracted by an outcome: `bounced` (a wrong number is a fact about
the number), `companies.is_competitor` (the owner clears it by hand), every row a person wrote
(`dnc`, `legal`, `manager_veto`, `b2c`) and every `erp_feed` / `manual_import` row (their own syncs
own them). `source_precision_job`: `precision_30d = positives / (positives + negatives)` over outcomes
whose `first_read_at` is within 30 d, ignoring `pending`/`neutral`, per `source_key`. W/R: `erp_sync`
outcomes walks / `source_precision_job`, `/holat`, `tools/fit_weights.py`.

### 2.12 `interactions`, `chat_monitors` — the Telegram reader

```sql
CREATE TABLE interactions (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tg_chat_id          bigint NOT NULL,
  tg_message_id       bigint NOT NULL,
  sender_id           bigint,
  sender_username     text CHECK (sender_username IS NOT DISTINCT FROM cs_username_key(sender_username)),
  sender_name         text CHECK (length(sender_name) <= 120),
  sender_bio          text CHECK (length(sender_bio) <= 300),
  text                text CHECK (length(text) <= 4000),
  occurred_at         timestamptz NOT NULL,
  media_kind          text NOT NULL DEFAULT 'none' CHECK (media_kind IN ('none','photo','document','video','other')),
  reply_to_message_id bigint,
  fwd_from_peer_id    bigint,                 -- forwarded-from channel/user id (tout rule: a forward from a channel other than the sender's own, 03 §2.3); NULL = not a forward
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
  role                  text NOT NULL DEFAULT 'market' CHECK (role IN ('market','competitor','buyers','calibration','other')),   -- 'calibration' = GSR's own chat: monitor_enabled=false, nothing is read (03 §2.3)
  status                text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','joined','gone','left')),   -- the joiner's queue (03 §2.3, 06 §1.2)
  priority              smallint NOT NULL DEFAULT 5 CHECK (priority BETWEEN 0 AND 9),   -- 0 owner /chats qoʻsh · 1 linked discussion group · 2 companion group · 3 resolved username · 5 seed
  monitor_enabled       boolean NOT NULL DEFAULT true,         -- MIYA default flipped: groups/channels on, private off by rule
  decided_by            text CHECK (decided_by IN ('owner','rule:seed','rule:private_off','rule:default')),
  asked_at              timestamptz,
  joined_at             timestamptz,
  backfill_requested_at timestamptz,
  backfilled_at         timestamptz,                           -- set by telegram_monitor.backfill (03 §2.3)
  backfill_attempts     smallint NOT NULL DEFAULT 0,
  monitoring_since      timestamptz,
  last_seen_message_id  bigint,
  last_seen_at          timestamptz,
  seen_count            integer NOT NULL DEFAULT 0,            -- every message seen (counted, never stored)
  hit_count             integer NOT NULL DEFAULT 0,            -- prefilter hits stored in interactions
  member_count          integer,
  flood_until           timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_chat_monitors_joined CHECK (status <> 'joined' OR joined_at IS NOT NULL)
);
CREATE INDEX ix_chat_monitors_queue ON chat_monitors (priority, created_at) WHERE status = 'queued';
```
The "no full chat logs" rule is structural: the userbot inserts only messages whose text matched a prefilter pattern (`cardinality(prefilter_hit) > 0`); everything else only increments `seen_count`. `pricelist` hits are stored so the tout rule (≥ 3 price-list-shaped messages per sender in 7 d, 03 §2.3) can be counted; they are purged at 30 d like every unconverted row. Member lists and profile photos are never read. A channel's `about` text is not an interaction (it has no `prefilter_hit`): it is stored as `evidence` + `observations(kind='profile')` (03 §2.3). The userbot's insert transaction ends with `NOTIFY cs_interaction, '<id>'`; `classify_job` and `score_job` wake on it and the 15-min sweep is only the fallback (04 §0, 06 §1.6 — this is what makes N-06 reachable). W/R: userbot / `telegram_monitor` collector (no network: it reads this table), retention, `/chats`.

### 2.13 `heartbeats`, `usage_log`, `reminder_log`, `runtime_flags`

```sql
CREATE TABLE heartbeats (                                   -- MIYA verbatim
  component    text PRIMARY KEY,                             -- bot|worker|userbot|backup|job:<id>|collector:<key>[.variant] (erp_sync.full, erp_sync.delta, erp_sync.outcomes, telegram_monitor.join, telegram_monitor.backfill, serp_layer.night)
  last_seen_at timestamptz NOT NULL,
  detail       jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE usage_log (                                    -- MIYA adapted: every paid call, not only Anthropic
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider          text NOT NULL CHECK (provider IN ('anthropic','brave','yandex_search','google_places','twogis','apify','proxy','searxng','hh','other')),
  model             text,                                    -- model id | apify actor | places SKU | 'refused:<code>' on a refusal row (cost_usd=0, units=0; 04 §8)
  operation         text NOT NULL CHECK (operation IN (     -- = the Operation literal of 04 §8, verbatim; costs.ask writes it
                      'classify','verdict','company_verdict','draft','enrich_search',
                      'serp_brave','serp_yandex_night','serp_searxng','serp_ddg',
                      'places_text_ids','places_details_contact','twogis_api',
                      'apify_olx','apify_2gis','apify_instagram','apify_uzum','proxy_uzum','model_reserve','serp_reserve','other')),
  input_tokens      integer, output_tokens integer, cache_read_tokens integer, cache_write_tokens integer,
  units             numeric(14,3),                            -- queries, results, MB for non-token providers
  batch             boolean NOT NULL DEFAULT false,
  cost_usd          numeric(12,6) NOT NULL CHECK (cost_usd >= 0),  -- unknown price never reaches here: costs.ask refuses it (the refusal itself is a 0-cost row)
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
  kind    text NOT NULL,                                     -- alert:<key>|recovered:<key> (health, MIYA constants, 06 §4.1)|review_expired|push_unconfirmed|digest|source_paused
  ref     text NOT NULL,
  sent_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_reminder_log_kind_ref_sent ON reminder_log (kind, ref, sent_at);

CREATE TABLE runtime_flags (                                -- switches that must survive a restart and be read by bot, worker and userbot (06 §6)
  key        text PRIMARY KEY,                               -- 'paused' {"since","reason"} | 'flood_until' | 'review_hours_override'
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by text NOT NULL                                   -- 'owner:<tg_id>' | 'userbot' | 'worker:<job>'
);
```
The budget controller is 04 §8 and its signature is the only one: `costs.ask(session, *, operation, est_usd, call, price, now, …)` sums `usage_log.cost_usd` for today (Tashkent) and the month and refuses **before** the call in the order month → day → family → op; ahead of it the collector's `Budget.can_spend` (03 §1.2) checks the per-source budget `Budget.usd = sources.daily_cost_cap_usd − spend(source_key)`. A refusal is persisted here as a row with `cost_usd=0, units=0, model='refused:<code>'` — the bot is another process and cannot read a worker counter, so `/xarajat` and the digest count these rows. `runtime_flags` holds the switches the three processes share (`/toxtat` → `paused`, the userbot's `flood_until`); writers upsert with `updated_at = now()`. W/R: every paid caller, `health.beat`, bot (`/toxtat`, `/yoq`), userbot (`flood_until`) / `/xarajat`, `/holat`, health, `policy.paused()`.

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
CREATE TRIGGER trg_touch_updated_at BEFORE UPDATE ON runtime_flags   FOR EACH ROW EXECUTE FUNCTION cs_touch_updated_at();
CREATE TRIGGER trg_append_only BEFORE DELETE ON push_log  FOR EACH ROW EXECUTE FUNCTION cs_append_only();
CREATE TRIGGER trg_append_only BEFORE DELETE ON outcomes  FOR EACH ROW EXECUTE FUNCTION cs_append_only();
CREATE TRIGGER trg_append_only BEFORE DELETE ON merge_log FOR EACH ROW EXECUTE FUNCTION cs_append_only();
CREATE TRIGGER trg_append_only BEFORE DELETE ON purge_log FOR EACH ROW EXECUTE FUNCTION cs_append_only();
CREATE TRIGGER trg_append_only BEFORE DELETE ON usage_log FOR EACH ROW EXECUTE FUNCTION cs_append_only();
CREATE FUNCTION cs_scoring_weights_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'scoring_weights is append-only'; END IF;
  IF OLD.approved_at IS NOT NULL AND NEW IS DISTINCT FROM OLD   -- the whole row, not five weight columns
  THEN RAISE EXCEPTION 'approved scoring_weights version % is frozen', OLD.version; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_scoring_weights_frozen BEFORE UPDATE OR DELETE ON scoring_weights FOR EACH ROW EXECUTE FUNCTION cs_scoring_weights_frozen();
CREATE FUNCTION cs_candidate_suppressed_by() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state = 'suppressed' AND NEW.suppressed_by IS NULL
     AND (TG_OP = 'INSERT' OR OLD.state IS DISTINCT FROM 'suppressed') THEN
    RAISE EXCEPTION USING ERRCODE = 'check_violation', TABLE = 'lead_candidates',
      CONSTRAINT = 'trg_lead_candidates_suppressed',
      MESSAGE = 'lead_candidates: a candidate enters state suppressed only with suppressed_by ("trg_lead_candidates_suppressed")';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_lead_candidates_suppressed BEFORE INSERT OR UPDATE OF state ON lead_candidates FOR EACH ROW EXECUTE FUNCTION cs_candidate_suppressed_by();
```

`trg_lead_candidates_suppressed` replaces the CHECK this table once carried (`ck_lead_candidates_suppressed`, §0): it refuses only the step INTO `suppressed` without a pointer (errcode 23514, like a CHECK), so the FK's own `SET NULL`, when the suppression row is retired, leaves the state alone.

## 3. Evidence files on disk

- Root `EVIDENCE_DIR=/data/evidence` (compose volume `evidence:/data/evidence`, uid 10001, mode 0750). Path `<yy>/<sha256>.gz`, `yy` = two-digit year of `fetched_at` in Tashkent, `sha256` = hash of the **raw** bytes before gzip (level 6). Written to `<path>.tmp` then `os.replace` — a crash never leaves a half file.
- One file per distinct body: the same bytes at two URLs are two `evidence` rows and one file. Deletion only when no row with `raw_path IS NOT NULL` shares the `sha256`.
- Two caps: the fetch reads at most **5 MB** of an answer (03 §1.1; a cut answer is `FetchResult.truncated`, counted in the run's `meta.fetch_truncated`), and the store keeps a file only for a body of at most **2 MB** — larger bodies, and every cut one, get `raw_path=NULL`, `excerpt` and `bytes` still recorded, `evidence.meta.truncated=true` on the row and the count in the run's `meta.truncated`.
- A file is written only for a row its transaction inserted, and only after that transaction commits (`after_commit`); a rollback of the transaction or of the savepoint the row was stored in drops the write, so no file exists that no row points to. A crash between the commit and the write leaves a row whose file is missing (harmless: `open_raw` answers None, the excerpt still quotes; the next store of the same bytes heals it, `keep_raw`). Nothing yet sweeps an orphan `<path>.tmp` left by a crash mid-write — the retention task (T-31) owns that sweep.
- Not part of `pg_dump` backups (MIYA `backup.py` unchanged). Losing the directory loses nothing a card or ERP note needs — `url`, `fetched_at`, `excerpt` live in the row. Optional `restic` to object storage is an ops choice (§9).
- API: `services/evidence.store(session, *, source_key, url, body: bytes, content_type, fetched_at, source_ref, excerpt, title, lang, published_at, collector_run_id, meta, root) -> Evidence` (the existing row when the same bytes at the same URL were stored before — evidence is immutable); `open_raw(evidence, *, root) -> bytes | None`; `purge_raw(session, evidence, *, now, root) -> bool` — it takes the session because it must check whether another row still shares the file before unlinking it.

## 4. Retention (`retention_job`, daily 04:15 Tashkent; each step its own transaction and its own `purge_log(subject_kind='retention')` row)

| Step | Rule | SQL / pseudocode |
|---|---|---|
| R1 raw evidence 180 d | file goes, row stays | `SELECT id, sha256, raw_path FROM evidence WHERE expires_at < now() AND raw_path IS NOT NULL LIMIT 5000` → for each: if `NOT EXISTS (SELECT 1 FROM evidence e2 WHERE e2.sha256 = sha256 AND e2.raw_path IS NOT NULL AND e2.id <> id)` unlink file → `UPDATE evidence SET raw_path = NULL, raw_deleted_at = now() WHERE id = …` |
| R2a unconverted Telegram posts 30 d | delete | `DELETE FROM interactions WHERE occurred_at < now() - interval '30 days' AND (observation_id IS NULL OR observation_id IN (SELECT id FROM observations WHERE status = 'rejected'))` |
| R2b converted Telegram posts 90 d | keep ids, drop text | `UPDATE interactions SET text = NULL, sender_bio = NULL, sender_name = NULL, text_purged_at = now() WHERE occurred_at < now() - interval '90 days' AND text IS NOT NULL` |
| R3 observation scrub 30 d after resolution | person data out of `parsed` | `UPDATE observations SET parsed = (parsed - 'phones' - 'person') \|\| CASE WHEN parsed->>'kind' = 'intent_post' THEN jsonb_build_object('handles', (parsed->'handles') - 'tg_user_id' - 'telegram') ELSE '{}'::jsonb END \|\| jsonb_build_object('scrubbed', true), scrubbed_at = now() WHERE scrubbed_at IS NULL AND coalesce(resolved_at, created_at) < now() - interval '30 days'` — for an intent post the poster's personal `handles.telegram`/`tg_user_id` go too (a company channel lives in `companies.telegram_channel` by then) |
| R4b Places phones 30 d | Maps terms: no caching beyond 30 d | `UPDATE contacts c SET phone = NULL, phone_kind = NULL WHERE c.source_key = 'maps_google' AND c.phone IS NOT NULL AND c.collected_at < now() - interval '30 days' AND NOT EXISTS (SELECT 1 FROM push_log p WHERE p.company_id = c.company_id AND p.status IN ('sent','unknown'))` — a phone the ERP already received stays (the lead exists there); both columns go together because of `ck_contacts_phone_kind` |
| R4 unanswered leads 90 d | company-tier only | candidates `pushed` with `pushed_at < now() - 90 d` and outcome `label IN ('open_untouched','stale_open','dropped_capped','dropped_other')` or no outcome row → `services/contacts.anonymise(contact)`: `full_name, role, telegram_id, email, telegram_username, instagram := NULL; phone := NULL WHERE tier='person' AND phone_kind='mobile'; anonymised_at := now()` |
| R5 excluded persons 30 d | company-tier only | person-tier contacts of companies whose open candidate is `excluded`/`rejected` for 30 d, or company `state='excluded'` → `anonymise(contact)` |
| R6 ledgers | | `UPDATE push_log SET payload = NULL WHERE sent_at < now() - 90 d AND payload IS NOT NULL`; `UPDATE model_jobs SET request = NULL, pseudonyms = NULL WHERE status IN ('applied','failed','fallback') AND coalesce(applied_at, created_at) < now() - 30 d`; `DELETE FROM collector_runs WHERE started_at < now() - 180 d`; `DELETE FROM reminder_log WHERE sent_at < now() - 90 d`; `DELETE FROM lead_candidates WHERE state = 'hold' AND updated_at < now() - 365 d AND NOT EXISTS (SELECT 1 FROM push_log p WHERE p.candidate_id = lead_candidates.id)` (`push_log.candidate_id` is RESTRICT on purpose — the append-only ledger keeps its candidate; the bare statement is refused by `fk_push_log_candidate_id`; `tests/test_constraints.py::R6_HOLD_SWEEP` pins this text and T-31's `test_retention` uses it); `DELETE FROM suppression WHERE kind = 'cooldown' AND expires_at < now() - 30 d` |
| R6b client exports 1 d | file goes | every file in `data/imports/` (the container's `/data/imports`) whose mtime is older than 1 day — the ERP's client export is every client's name, code, manager and phones; `tools/import_clients.py --apply` already deletes its file once the tails are written (seam review 2026-10-24), so this step only sweeps a dry run's or a refused run's leftover (T-31) |
| R7 never | | `suppression` (non-cooldown), `outcomes`, `purge_log`, `merge_log`, `usage_log` rows (payload-free) |
| Immediate | legal / DNC | `purge.plan_company(company_id)` → owner confirms → `execute`; leaves `suppression(legal|dnc)` hashes and the `purge_log` row |

## 5. Alembic plan

- `alembic/env.py` = MIYA's minus the pgvector `render_item`; imports `clientscrapper.db.models` to populate `Base.metadata`; `compare_type=True, compare_server_default=True`. `alembic.ini` as MIYA (ruff post-write hook, URL from settings). The compose `init` service runs `alembic upgrade head`; `worker`, `bot`, `userbot` `depends_on: init: condition: service_completed_successfully`.
- **`0001_initial`** (`revision="0001_initial"`, `down_revision=None`): `upgrade()` in this order — extension `pg_trgm`; the **7** functions (`cs_phone_tail`, `cs_username_key`, `cs_domain_key`, `cs_touch_updated_at`, `cs_append_only`, `cs_scoring_weights_frozen`, `cs_candidate_suppressed_by`); tables exactly in the order of §2 (`sources, collector_runs, evidence, companies, company_aliases, company_ids, contacts, observations, model_jobs, signals, scoring_weights, suppression, lead_candidates, lead_reviews, push_log, outcomes, interactions, chat_monitors, heartbeats, usage_log, reminder_log, runtime_flags, merge_log, purge_log, sheet_rows` — 25 tables; `runtime_flags`, `companies.dup_of/dup_score`, `lead_reviews.kind`, `chat_monitors.status/priority`, `evidence.meta`, `interactions.fwd_from_peer_id` are day-1 columns, not a `0002`); triggers; `op.bulk_insert` of the 23 `sources` rows (§2.1, rendered as literals so `alembic upgrade head --sql` works) and the 26 `scoring_weights` version-1 rows inserted **unapproved** (`proposed_by='seed'`), then approved the way every later version is — the activation UPDATE of §2.7, `UPDATE scoring_weights SET approved_by='seed', approved_at=now() WHERE version=1` (it also keeps `--sql` free of a server-side `now()` inside the insert). `downgrade()` drops in reverse, then functions, then the extension. `0001` was edited in place by the T-01/T-02 review **before it ran anywhere** (its docstring lists every departure from the first draft of this file — all folded into the DDL above); from now on it is frozen and every change is a new migration. The migration carries its own frozen tuples (CHECK lists) — `tests/test_enums_frozen.py` compares them with `db/enums.py`.
- Later migrations: `000N_<slug>.py`, one concern each, minted by `make revision m="…"` — `scripts/revision_id.py` gives the id (N = one past the highest number in `alembic/versions`; a gap stays a gap) and `alembic.ini` `file_template = %%(rev)s` makes the file `<id>.py`; widening a CHECK = `op.drop_constraint` + `op.create_check_constraint` with the new tuple, plus the tuple change in `enums.py` in the same commit; new source = a data migration inserting the `sources` row (`tenders`, phase 2 in 03 §3, is **not** among the 23 seeded rows: the FK refuses `source_key='tenders'` and `tests/test_sources_seed.py` pins both facts — its row arrives with a data migration in the task that builds the collector); new weight version = never a migration (runtime through `/vazn` or `fit_weights`). Known follow-up: `cs_domain_key` may strip `'^(www\.)+'` (§1) in a later migration.
- **`0002_erp_contract`** (T-10, the first task that needs it; 05 §B.2, 2026-10-24 «T-07 as built»): `outcomes.no_contact boolean NOT NULL DEFAULT false`; `push_log.superseded_at timestamptz`; `ux_push_log_delivered` dropped and recreated `WHERE status IN ('sent','unknown') AND superseded_at IS NULL`. Additive only, on top of the reviewed `0001_initial`, unchanged by that review. The DDL blocks of §2.10/§2.11 show the state after `0002`, marked in their comments; `test_migrations`/`compare_metadata` run against head.
- `tests/test_migrations.py` (needs postgres; skips loudly, never silently): on an empty scratch database `upgrade head` gives the counts of §10 AC 1; `compare_metadata(ctx, Base.metadata) == []`; **and** `Base.metadata.create_all` builds every table in a probe schema inside a rolled-back transaction, and every constraint (`pg_get_constraintdef`), index (`pg_get_indexdef`) and column default must equal the migration's as the server prints them — `alembic check` misses a flipped operator inside a CHECK; `upgrade head --sql` builds the same schema and seeds; no CHECK spans a SET NULL FK (§0); then `downgrade base` leaves zero tables in `public`, then `upgrade head` again.

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
                                     REVIEW_KINDS, REVIEW_ANSWERS, PUSH_STATUSES, PUSH_SOURCE_KEYS, LANDINGS, STAGE_KINDS,
                                     CONTACT_KINDS, OUTCOME_LABELS, PRECISION_CLASSES, SUPPRESSION_KINDS,
                                     SUPPRESSION_KEY_TYPES, SUPPRESSION_SOURCES, CHAT_TYPES, CHAT_ROLES, CHAT_STATUSES, MEDIA_KINDS,
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
clientscrapper/db/models/ops.py         Heartbeat, UsageLog, ReminderLog, RuntimeFlag, ModelJob, MergeLog, PurgeLog, SheetRow
```
Model conventions (MIYA): `Mapped[...]` annotations; `relationship(lazy="raise")` everywhere; JSONB typed `dict[str, Any]`; `Decimal` for numeric; every index/constraint named in `__table_args__`; a comment says **why** the column exists. State transition dicts live in `services/candidates.py` and `services/reviews.py`, not in models. Raw SQL writers (userbot `already_stored` insert + `NOTIFY cs_interaction`, `costs._record`, `health.beat`, `runtime_flags` upserts) are allowed; they rely on the triggers above, not on ORM hooks. `KIND_RANK` (§2.8) lives in `services/suppression.py`, not in a model.

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
| — | `sources`, `collector_runs`, `observations`, `company_ids`, `signals`, `scoring_weights`, `lead_candidates`, `push_log`, `outcomes`, `suppression`, `runtime_flags`, `merge_log`, `purge_log` | **new** (`merge_log`/`purge_log` follow `people.merge_into` and `purge.PurgePlan` shapes) |

## 8. Worked example — one company, observation → pushed lead → outcome (synthetic)

All values are invented: chat `@namuna_optom`, company «Namuna Tekstil Savdo MChJ», phone `+998900000001`, STIR `300000011`. Ids shortened.

| # | table | row |
|---|---|---|
| 1 | `evidence` | `id=7001, source_key='telegram_monitor', url='https://t.me/namuna_optom/18234', source_ref='tg:-1001000000001:18234', fetched_at='2026-10-20 07:41+05', published_at=same, sha256='3f9c…e1', content_type='text/plain', bytes=142, raw_path='26/3f9c…e1.gz', excerpt='Guangzhoudan 25 kub mato olib kelish kerak, kim qiladi? @namuna_tekstil', lang='uz', expires_at='2027-04-18 07:41+05'` |
| 2 | `interactions` | `tg_chat_id=-1001000000001, tg_message_id=18234, sender_id=5100000001, sender_username='namuna_tekstil', sender_name='Rustam', sender_bio='Namuna Tekstil Savdo MChJ direktori', text='Guangzhoudan 25 kub …', occurred_at='2026-10-20 07:41+05', prefilter_hit='{volume_kub,china,intent}', processed_at='2026-10-20 07:41:40+05', evidence_id=7001, observation_id=9001` |
| 3 | `observations` | `id=9001, evidence_id=7001, ordinal=0, source_key='telegram_monitor', status='resolved', parsed=<§2.5 JSON: kind intent_post, name_key 'namuna tekstil savdo', phones [+998900000001 mobile person], person {Rustam, direktor}, hints volume_m3 25 stated china_words true>, company_id='0f0e0d0c-…', contact_id='c0ffee00-…', resolved_at='2026-10-20 07:46+05'` |
| 4 | `companies` | `id='0f0e0d0c-…', display_name='Namuna Tekstil Savdo MChJ', name_key='namuna tekstil savdo', legal_form='MChJ', stir='300000011' (after registry step), city='Toshkent', telegram_channel=NULL, registry_status='active', registered_on='2019-03-11', sectors='{textile}', goods='{mato,furnitura}', segment='D', volume_band='3_10', state='active', first_seen_at='2026-10-20 07:46+05', last_signal_at='2026-10-20 07:41+05', best_score=82` (+ `company_aliases` rows `'Namuna Tekstil Savdo MChJ'`, `'НАМУНА ТЕКСТИЛЬ САВДО'`; **no** `company_ids` row — the poster's Telegram id `5100000001` is person-tier and sits in `contacts.telegram_id`, row 5) |
| 5 | `contacts` | `id='c0ffee00-…', company_id='0f0e0d0c-…', tier='person', full_name='Rustam', role='direktor', phone='+998900000001', phone_tail='900000001' (generated), phone_kind='mobile', telegram_username='namuna_tekstil', telegram_id=5100000001, is_decision_maker=true, legal_basis='public_source', source_url='https://t.me/namuna_optom/18234', source_key='telegram_monitor', collected_at='2026-10-20 07:46+05'` |
| 6 | `signals` | `(company 0f0e…, evidence 7001, key='vol_stated_3plus', value=25.000, quote='25 kub mato olib kelish kerak', observed_at='2026-10-20 07:41+05', expires_at=+180 d, source_key='telegram_monitor', model=NULL)`; `(…, key='cn_origin', quote='Guangzhoudan', …)`; `(…, evidence 7002 (registry page), key='ent_legal_confirmed', value=NULL, observed_at='2026-10-20 07:52+05', expires_at=NULL, source_key='registry_lookup')`; `(…, evidence 7002, key='ent_age_2y_active', value=7)` |
| 7 | `lead_candidates` | `id='1ead0001-…', company_id='0f0e…', contact_id='c0ffee00-…', state: found (07:46) → scored (07:53) → review (07:55) → approved (09:12) → pushed (09:31); tier='hot', score=82, weights_version=1, primary_source_key='telegram_monitor', score_breakdown={"version":1,"groups":{"volume":{"cap":40,"raw":30,"used":30},"origin":{"cap":20,"raw":20,"used":20},"solidity":{"cap":15,"raw":12,"used":12},"recency":{"cap":10,"raw":10,"used":10},"contact":{"cap":10,"raw":10,"used":10},"corroboration":{"cap":5,"raw":0,"used":0}},"negatives":[],"total":82}, approved_by='owner:111111111', approved_at='2026-10-20 09:12+05', external_id='cs:0f0e0d0c-…:202610', pushed_at='2026-10-20 09:31+05'` |
| 8 | `lead_reviews` | `id=501, candidate_id='1ead0001-…', state='approved', card_chat_id=111111111, card_message_id=4521, card_hash='9a…', asked_at='2026-10-20 07:55+05', expires_at='2026-10-23 07:55+05', answer='push', answered_at='2026-10-20 09:12+05', by_telegram_id=111111111` |
| 9 | `push_log` | `external_id='cs:0f0e0d0c-…:202610', attempt=1, candidate_id='1ead0001-…', company_id='0f0e…', phone_tail='900000001', endpoint='/api/leads/in/agent', source_key_sent='agent', payload={external_id, name:'Namuna Tekstil Savdo MChJ', phone:'+998900000001', note:'NEGA TOPILDI: 20.10.2026 @namuna_optom: «Guangzhoudan 25 kub mato olib kelish kerak, kim qiladi?» … Masʼul: Rustam (direktor) …', utm_source:'clientscrapper', kub:'25', firma:'Namuna Tekstil Savdo MChJ', stir:'300000011', telegram:'@namuna_tekstil', imports:'mato, furnitura', score:'82', evidence:'https://t.me/namuna_optom/18234'} — in `PushPayload`'s field order, which is the body order and so the order of the ERP's note lines (05 §B.1: `utm_source` right after `note`); `kub:'25'` because the volume was STATED (a band is never sent as `kub`); `name` is the company only (05 §B.1); the person appears solely in the note's «Masʼul» line, and only because the contact carries `legal_basis`, payload_sha256='7b…', status='sent', http_status=200, sent_at='2026-10-20 09:31:02+05', finished_at='2026-10-20 09:31:03+05'` |
| 10 | `outcomes` | night 1: `external_id='cs:0f0e…:202610', intake_id='a1b2…', landing='created', lead_id='d4e5…', stage_kind='open', contacted_at=NULL, label='open_untouched', precision_class='pending', erp_at='2026-10-20 09:31:03+05'` → night 2 upsert: `contacted_at='2026-10-20 11:04+05', contact_kind='call', label='contacted_open', precision_class='pending'` → 2026-11-06 upsert: `stage_kind='won', client_id='f6a7…', quoted_volume_m3=25.000, label='won', precision_class='positive', label_changed_at='2026-11-06 03:00+05'` |

Follow-on rows the `won` label produces (same transaction as the upsert, §2.11): `companies.state='client'`; a local `suppression(kind='client', key_type='phone_tail', key='900000001', source='outcomes')`; `sources.telegram_monitor.positives_30d += 1`. The next full pull of the suppression feed adds the ERP's own `suppression(kind='client', key_type='phone_hash', key=erp_phone_hash(secret,'900000001'), salt_id='<8 hex>', source='erp_feed')` — two rows, one phone, either one is a hit, so the phone is never carded again even if the feed lags a day.

## 9. Open questions

1. STIR checksum: does the 9-digit STIR carry a check digit? UNVERIFIED — until confirmed `stir_key` validates shape only.
2. The agent's own DNC phones are stored as plain `phone_tail` (needed for lookups without the ERP secret). The legal memo prefers hashes; a peppered hash needs one more secret (`SUPPRESSION_PEPPER`) outside the agreed env list. Decide: (a) plain tail-9 in v1 — recommended, the table never leaves the server; (b) peppered hash.
3. `outcomes` is a snapshot. Keep a history table (one row per label change) for weight fitting, or is `label_changed_at` enough for v1?
4. `lost_price` cooldown 180 d and `lost_not_importer` 365 d — owner to confirm the two windows.
5. ~~ERP feed id types~~ — resolved (2026-10-24): `intake_id`, `lead_id`, `client_id` are uuid strings (`agent-feed.ts` casts each `::text`; the outcomes cursor refuses a non-uuid id; captured in `tests/fixtures/erp/outcomes.json`).
6. Evidence directory backup: excluded from `pg_dump` backups by design; is a weekly `restic` copy wanted (≈ 1–3 GB/year)?
7. Is the Sheets mirror (`sheet_rows`) part of v1 or dropped from `0001`?
8. May `MANAGER_TELEGRAM_IDS` answer review cards (`lead_reviews.by_telegram_id` ≠ owner), or owner only in v1?
9. `brand_dealers` is seeded at `daily_cost_cap_usd = 0` while 03 §3 says its spend is «inside serp». If it pays Brave under its own `source_key`, `Budget.can_spend` refuses every query; if it spends under `serp_layer`, the per-source budget no longer says who spent. Decide before T-33: (a) it calls `SerpClient` attributing to `serp_layer`'s sub-sources — recommended, one SERP budget; (b) give it a cap of its own.
10. `enrich_site` is seeded with 150 pages/day, while 04 §3 E2 draws its 150 pages from `sources.registry_lookup.daily_page_budget` — two budgets for one fetch. Decide before T-19 which row E2 counts against (recommended: `enrich_site`, the row its evidence carries).
11. `erp_sync.title_uz` is «ERP (fullprompt)» — the repository's codename on an owner screen (`/manbalar`). Recommended: «ERP (GSR)» in the data migration that next touches `sources`.

## 10. Acceptance criteria

1. `alembic upgrade head` on an empty PG16 creates 25 tables, 7 functions, 1 extension and seeds 23 `sources` rows and 26 `scoring_weights` rows; `compare_metadata` returns `[]`; `downgrade base` then `upgrade head` both succeed (`tests/test_migrations.py`).
2. `tests/test_enums_frozen.py`: every CHECK tuple in `0001` equals its `db/enums.py` tuple; `SIGNAL_KEYS ∪ DERIVED_KEYS` equals the `scoring_weights` key CHECK; version 1 has one row per key, group caps sum to 100, and is approved.
3. `tests/test_keys.py`: Python `phone_tail/username_key/domain_key` agree with `cs_*` SQL functions on ≥ 40 fixtures (Cyrillic, `t.me/`, `www.`, IDN, bots, short numbers).
4. Constraint tests, each shown red by dropping the constraint in a string-edited copy: second live person-tier contact per company refused; person-tier row without `legal_basis/source_url/collected_at` refused; `full_name` containing a 14-digit run refused; two `sent|unknown` rows for one `external_id` refused; a `push_log` row with `http_status=200` and `status='failed'` refused while `status='unknown'` is accepted; a second live `kind='manager'` review for one candidate refused while a live `kind='owner'` one beside it is accepted; a `chat_monitors` row `status='joined'` with `joined_at` NULL refused; `pushed` candidate without `external_id` refused; `phone_hash` suppression without `salt_id` refused; `excerpt` > 600 refused; `interactions` row with empty `prefilter_hit` refused; every change to an approved `scoring_weights` row refused (points, version, key, note, un-approval, re-approval) and a half approval (`approved_by` without `approved_at`) refused; a candidate entering `suppressed` without `suppressed_by` refused while retiring its suppression row keeps it; a merge target cannot be deleted from under its sources; a value the key function refuses (`'Mixed Case!!'`, `'not a domain'`) is refused in every key column while NULL is accepted; `DELETE FROM push_log` refused.
5. `tests/test_candidate_states.py` and `tests/test_review_states.py` enumerate `TRANSITIONS`; every illegal move raises; `ux_lead_candidates_open` refuses a second open candidate per company.
6. `tests/test_push_idempotency.py` against a fake ERP: crash between claim and response leaves `sending` → reconciled to `unknown` → no second send; 404 trips `sources.erp_sync.breaker_until`; 5xx: three in-attempt tries, then `failed`, then ≤ 3 attempts with the same `payload_sha256` after the 05 §B.1 delays; a read timeout lands as `unknown` with ONE POST (no resend inside the attempt) and the fake's late landing then promotes it to `sent`; a 503 is retried inside the attempt and lands once; a 2xx with an unparseable body lands as `unknown`; a `sent` row with no outcome after 48 h is superseded and re-pushed exactly once (rule 5) and a second 48 h re-pushes nothing; `/push tekshir` on a 2xx row supersedes it (never `failed`); a different body under the same `external_id` is refused.
7. `tests/test_outcomes_labels.py`: `derive_label` table-driven over all 16 labels, including the 14-day boundary on `contacted_at` and the 30-day boundary on `pushed_at` (not `at`) in Asia/Tashkent, `no_contact` deciding `lost_no_contact`, `stage_kind='won'` deciding `won` (a revived lead with `client_id` set is not won), every `dropped` reason; `apply_effects` (`suppression(client, phone_tail, source='outcomes')` for `won`, `landed_client` and `dropped_suppressed`, `lost` rows on every key for `lost_no_contact`, `bounced ×2`, `is_competitor`) asserted, and the retractions: a revived lost lead, a won lead moved back to open and a changed label each remove exactly the rows their old label justified, while `bounced`, `is_competitor` and person-written rows stay; a feed row whose `external_id` has no `push_log` row writes nothing.
8. `tests/test_retention.py` on fixtures aged by string-edited timestamps: R1 removes the file and keeps the row; R2a deletes an unconverted post at 31 d and keeps a converted one; R2b nulls text at 91 d; R3 scrubs `phones`/`person` and, for `kind='intent_post'`, `handles.telegram`/`tg_user_id` (a `directory_entry`'s `handles.telegram` stays); R4b nulls a `maps_google` phone at 31 d when no push used it and keeps one a `sent` push used; R4 anonymises the person-tier contact at 91 d after an untouched push and leaves the company-tier row; every step writes a `purge_log` row.
9. `tests/test_pii_columns.py` and `tests/test_fk_indexes.py` pass over `Base.metadata`; `tests/test_pseudonymised_requests.py` finds no raw phone or handle in `model_jobs.request`.
10. `tests/fixtures/example_flow.sql` loads §8 rows 1–6; `tests/test_example_flow.py` drives score → review → approve → push (fake ERP) → outcomes upsert and ends with `outcomes.label='won'`, `companies.state='client'`, one local `suppression(client, phone_tail, source='outcomes')` row (no feed pull runs in the test).

## Review pass changelog

Review of 2026-10-23 (`REVIEW-2026-10-23.md`), items applied to this file:

- **1** — §2.1 seeds the schedulable parents `serp_layer`, `directories`, `maps` (and `enrich_site`); sub-source rows stay as attribution keys; §5/AC 1 now say 23 `sources` rows.
- **2/3** — §2.8 names the three origins of `client`/`lost` rows (feed hashes, outcomes tails, `manual_import` tails) and the retirement of the XLSX fallback by 05 §B.2 step 5; `make import-clients` no longer listed as a `dnc` entry route.
- **4** — `0001` gains `runtime_flags` (§2.13, trigger in §2.16), `companies.dup_of/dup_score` + index, `lead_reviews.kind` + widened `answer` + `ux_lead_reviews_live (candidate_id, kind)`, `chat_monitors.status/priority` (+ `ck_chat_monitors_joined`, queue index), `evidence.meta`, `interactions.fwd_from_peer_id`, the `enrich_site` row; `backfill_done_at` renamed `backfilled_at` (03's name) rather than adding a duplicate; §5 order list, §6 enums/models, §7 mapping and AC 1 restated as 25 tables / 23 sources / 26 weights.
- **5** — §2.13 `usage_log.operation` CHECK is 04 §8's `Operation` literal verbatim; `provider` gains `twogis`.
- **6** — §2.8 lookup orders by `array_position(...)` rendered from `KIND_RANK` in `services/suppression.py`.
- **7** — §2.8 points freshness to `services/erp_feeds.suppression_fresh()` (26 h / 3 h) and names the crons `30 2 * * *` / `15 * * * *`; §2.10 rule 2 gates on it.
- **8** — §2.12 states the userbot's `NOTIFY cs_interaction` and the wake-on-notify jobs; §2.9 states `review_cards_job` every 2 min inside `REVIEW_HOURS`.
- **9** — §2.9: `enriching → review` for the phone-less Telegram candidate (`tier='warm'`, `gates.no_phone=true`), `handoff` → `hold` + manager review row, `/tel` path `hold → scored → approved(manager:<tg_id>)`, `approved_by` comment widened; review machine covers both `kind`s and the manager answers.
- **11** — §8 row 9 payload `name` is the company; the person is only the note's «Masʼul» line.
- **12** — `tg_user` removed from `ID_TYPES` and from §8 row 4; R3 strips an intent post's `handles.telegram`/`tg_user_id`; new R4b clears unused `maps_google` phones after 30 d; §2.4 rule text and AC 8 updated.
- **15** — §2.13 points to the single `costs.ask(session, *, operation, est_usd, call, price, now, …)` signature and the order `Budget.can_spend` → month → day → family → op; `Budget.usd` reads `sources.daily_cost_cap_usd`.
- **16** — §2.10 rule 4 adopts 05 §B.1's two-level attempt scheme; `ck_push_log_sent` becomes `(sent ⇒ 2xx) AND (2xx ⇒ sent|unknown)`; `error` gains `http_2xx_badbody`; AC 4/6 updated.
- **17** — §2.13 comments: `collector:<key>[.variant]` heartbeats, `alert:<key>` / `recovered:<key>` reminder kinds.
- **21** — §2.10 rules 2 and 4 name `sources.erp_sync.breaker_until` (`/yoq crm` clears it) instead of a `crm_push` breaker.
- **23** — §2.12 tout window 7 d; channel `about` text stored as `evidence` + `observations(kind='profile')`, never as an interaction.
- **25** — §2.7 producers: `ent_legal_confirmed` only from a registry hit (E4); a parsed STIR/legal block feeds `ent_not_micro`.
- **30** — §2.13: refusals persisted as `usage_log` rows with `cost_usd=0, units=0, model='refused:<code>'`.
- **32** — §2.11 and §8: `won` writes `companies.state='client'` and a local `suppression(client, phone_tail, source='outcomes')` row at once; the feed's hashed row arrives later; AC 7/10 updated.
- **33** — §2.1: `source_precision_job` never flips `auto_push` on while `config.auto_push_forbidden` is true.
- **37** — §2.9 callback grammar points to 06 §1.5; §2.2 `collector_runs.meta` carries `unchanged/skipped_robots/note/truncated`, a host breaker ends a run `failed` + `error='host_blocked'`; §6 keeps `db/models/` as the package.
- **38** — §2.12 `chat_monitors.role` gains `calibration` (`monitor_enabled=false`, nothing read).

Not applied here (other files' homes): 10, 13, 14, 18, 19 (02's `/dnc`, `/vazn`, `/push tekshir`, `/stir` mentions already match 06 §1.2), 20 (02's `config.outcomes.{cursor,last_at}` already equals 05's), 22, 24, 26 (§8's 82 = 30+20+12+10+10+0 is consistent), 27 (02 already says 90 d / 24 h), 28, 29, 31, 34, 35, 36.

**T-07 as built (2026-10-24)** — the ERP side shipped on fullprompt `agent-feed` at `23fe1736` (05 Part A); this file now matches what it sends:

- §2.11 `outcomes`: new column `no_contact` (the ERP's own answer); `landing_reason` is `suppressed|capped|no_phone|no_contact` (never `replay`); `lost_reason` holds a label only; `raw` = the 15 fields. Rows only for `external_id`s the agent pushed (`foreign` rows dropped).
- §2.11 `derive_label`: takes `pushed_at` (the delivering `push_log.sent_at`) — `stale_open` no longer ages on `at`; `won` from `stage_kind` only (not `client_id`); `lost_no_contact` from `no_contact` (the `NO_CONTACT_REASONS` mirror is gone); contact checked before `landed_joined`; the 90-day / 7-day contact horizons and the orphan `joined` stated.
- §2.11 side effects are recomputed per company from current labels (`apply_effects`), which gives retractions — a revived lost lead, a won lead moved back to open, a changed label — and lists what stays stricter on purpose (`bounced`, `is_competitor`, person-written rows, feed and import rows). `dropped_suppressed` now leaves a local `phone_tail` row (the 7–8-digit card the feed cannot carry).
- §2.8: the `client` kind's third outcomes origin (`dropped_suppressed`), the `reason` tags; feed sync = full pull from nothing (`limit=5000`, no `since`/`cursor`) as the only deleting walk, ≥ 90 % against the previous full pull's own count, delta anchored on the ERP's `generated_at`; no feed call during the fallback.
- §2.10: `push_log.superseded_at` and the narrowed `ux_push_log_delivered`; rule 2 ignores superseded and same-`external_id` rows; rule 4 retries in an attempt only when nothing reached the ERP or it answered 5xx (503 = nothing recorded), a read timeout is `unknown` with no resend; rule 5 adds the 10-minute `unknown` walk, the 48-hour rule over `sent` and `unknown` rows with one re-push, and `/push tekshir` superseding instead of `failed` (which `ck_push_log_sent` forbids on a 2xx row — a latent clash in the previous text); rule 7 states the ERP's in-flight chain.
- §5: migration `0002_erp_contract` (T-10) carries the two columns and the index; `0001` untouched. §9 Q5 resolved (uuid strings). AC 6 and 7 restated.

**T-07 verification (2026-10-24)** — checked against fullprompt `agent-feed` at `23fe1736` and the v2 capture:

- §2.10: `push_log.error` gains `http_3xx` (a comment, no migration; the model comment in `db/models/leads.py` follows in T-28); rule 4 says a 5xx is retried because a re-send is fenced (not because nothing landed) and maps a 3xx to `failed` + breaker `erp_redirect` (05 §B.1).
- §2.11 «What the inputs mean»: «earliest wins» only during the arrival's first 24 h (`stampFirstContacts` re-reads a stamped arrival only while `created_at ≥ now − 1 day`), the `at` that can move backwards then, and who each kind of evidence counts from (`contactEvidenceSql`); `lost_reason` is NULL before the dictionary except a text equal to a setting label, which comes back verbatim (`outcomesPage`'s `labelled`).
- §8 row 9: the payload example in `PushPayload`'s body order (`utm_source` after `note`), `kub` only because the volume was stated.

**T-07 third review (2026-10-24)** — checked against fullprompt `agent-feed` at `a4f9f433` (DECISIONS #1473–#1475, `inbound-notify.ts` `markContactedFromBot`, `service.ts` `setFollowUp`, `first-contact.ts`, AGENT-FEED.md §5):

- §2.10 rule 4: the ERP's 503 «normally» means no ledger row — a ledger row can commit while its answer is lost (the retry is a replay; the arrival shows as `created`, never contacted), and a repeating half-way failure adds a lenta note per POST (05 §A.13 (i), (iii)).
- §2.10 rule 7: the in-flight chain is per copy of the landing module (one lander per channel) and its wait has no bound (05 §A.13 (iv)).
- §2.11: a «📞 Bogʻlandim» press by the responsible person reads `followup` — the booking it clears before writing its note is earlier evidence — and `note` only when the presser is not responsible or the date was already moved; a never-contacted `created` row may be a lost-answer arrival.

**Wave 1 as built (2026-10-24)** — folded from the wave-1 build (T-01…T-06 integration notes, the T-02 review fixes `69b9493`, the seam review), each checked against the code at `f8c3303` (`alembic/versions/0001_initial.py`, `db/models/`, `services/keys.py`, `services/normalise.py`, `services/evidence.py`, `services/costs.py`); §2.11 also against fullprompt `agent-feed` `d138b3bc` (the verify:final mismatches):

- §0, §1: key CHECKs read `col IS NOT DISTINCT FROM cs_key(col)`; no CHECK on a SET NULL FK column (`companies.merged_into` is RESTRICT, `trg_lead_candidates_suppressed` replaces `ck_lead_candidates_suppressed` — fullprompt #809's rule). `services/keys.py` is the one home of the key functions (`normalise.py` re-exports the phone ones); `phone_kind` names its mobile codes (33 77 88 90 91 93 94 95 97 98 99; 20/50/55 read `unknown` until verified; 70–79 except 77 are landlines) and refuses all-equal subscriber digits; `cs_username_key`'s prefix is case-blind like `username_key`; `domain_key` strips every leading `www.` and `cs_domain_key` one — the two designed differences are stated, the SQL follow-up is a later migration.
- §2.1 a parent row's budget counts its sub-sources (`SUBSOURCE_PREFIXES`); the breaker writes `paused_until` and `breaker_until`. §2.4 `merged_into` RESTRICT, «a company is never deleted; purge clears `merged_into`». §2.5 validators as built (a PINFL is a maximal run of exactly 14 digits, a replacement label carries `name_key` NULL, `seller` is a person kind, `serp_hit`/`suppression`/`outcome` are not observations) and the mapping rows. §2.7 `ck_scoring_weights_approved` and the whole-row freeze; version 1 is inserted unapproved, then approved. §2.8, §2.12 the `IS NOT DISTINCT FROM` CHECKs, and that no instagram shape CHECK exists beyond `lower()`. §2.9, §2.16 the trigger and `cs_candidate_suppressed_by()`; what `suppressed_by` NULL means. §2.10 `candidate_id` RESTRICT; `sent ⇒ 2xx or no HTTP answer`.
- §2.11: only a CREATED lead is booked for today (a joined one keeps its date). A renamed lost-reason label comes back `null` for old leads (`saveLostReason` renames in place) — decided here, without the owner: the outcomes upsert keeps the stored `lost_reason` and label when the reason goes from a label to `null` with `stage_kind` still `lost` and `closed_at` unchanged (05 §B.6).
- §3 two caps (5 MB per fetch, 2 MB per evidence file; `fetch_truncated` vs `truncated`), the file written after the commit, `purge_raw(session, evidence, *, now, root)`; orphan `.tmp` files are T-31's. §4 R6's hold sweep keeps a candidate that has `push_log` rows. §5 seven functions; `0001` was edited in place by the review before it ran anywhere and is frozen now; `make revision`; `tenders` needs a data migration; `test_migrations` as built. §10 AC 1 and AC 4 follow.
- Open questions 9–11 added: `brand_dealers` seeded at $0/day, `enrich_site` vs `registry_lookup` pages, the «ERP (fullprompt)» title.
