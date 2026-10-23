# Local code audit for the GSR client-finding agent (clientScrapper)

Scope: local files only. Checkouts audited (HEAD dates matter — contracts below are as of these commits):

| Path | What it is | HEAD |
|---|---|---|
| `/home/user/dailyJournal` | MIYA: owner's personal second brain. Python 3.12, FastAPI 0.115.6, SQLAlchemy 2.0 async + psycopg 3, Alembic, `pgvector/pgvector:pg16`, APScheduler 3.11, anthropic 0.122.0, aiogram 3.30, Telethon 1.44, rapidfuzz 3.14, sentence-transformers (bge-m3), google-api-python-client, pdfplumber/openpyxl/python-docx. 4 processes (`api`, `bot`, `worker`, `userbot`) + `db`, non-root uid 10001, localhost-only ports. | `6b1f67f` 2026-10-22 |
| `/home/user/logisticserp` | **This is the gsrwms.uz codebase** ("LogisticsERP", one installation per company): Next.js 16 + Drizzle + PG16 + pnpm; CRM module `src/modules/wms/crm/` (60 files); Telegram listener on managers' accounts via gramjs (`telegram@2.26.22`, `pnpm tg-listen`); company bot (`TELEGRAM_BOT_TOKEN`). Contains both `/api/lead/assign` and `/api/leads/in/[source]`. | `57fb49e` 2026-10-15 |
| `/home/user/bekzodqodirov/gsrlogistics.uz` | Astro site (uz/ru/en + Cyrillic), Cloudflare Pages. Lead routing in `src/lib/{site,leads,lead-client}.ts` + `src/components/ContactPicker.astro`. `functions/api/lead.ts` (POST → bot) is legacy; README says the form now goes through gsrwms. | `74d58ea` 2026-10-03 |
| `/home/user/bekzodqodirov/gsrerpv1` | Earlier "GSR ERP" skeleton (Next.js 16 + Drizzle, phase 0, modules `users/cargo/clients`, no leads). Superseded by logisticserp. Nothing to reuse. | — |
| `/home/user/bekzodqodirov/TelegramCustoms` | "Rastamojka kalkulyator boti": aiogram 3 + anthropic + SQLite. Extracts goods from docs/images (Haiku), groups into TN VED (Opus 5) with structured outputs (`ai/client.py:structured_request`, server-side fallback beta), 500k-row customs-stats base (`db/import_stats.py`, `db/price_stats.py`), local file parsing (`services/file_parser.py`), uz/ru transliteration+token match (`services/textmatch.py`). The `stats` table has **no declarant/importer column** (`tnved_code, product_name, unit_price, unit, qty, customs_value, net_weight, gross_weight, origin_country, declared_at`) → not a lead source as stored; whether the original Excel carries «декларант/получатель» is unknown. | — |

MIYA has **no web collectors at all** (no scraper, no search client); its only "collectors" are the Telethon reader and CSV/XLSX import. `httpx` is already a dependency.

---

## 1. MIYA reuse matrix

Legend: **AS-IS** = copy file, change imports only · **ADAPT** = keep structure/tests, replace domain parts · **NO** = not reusable.

| Capability | MIYA source (functions/classes) | Verdict | Notes for a lead-finding agent |
|---|---|---|---|
| Settings | `miya/config.py`: `Settings(BaseSettings)`, `parse_price`, `OptionalInt`, validators (`API_TOKEN_MIN_LENGTH=32`, `BACKUP_RECIPIENT_ERROR`, `COMMENT_VALUE_ERROR`), `tz` property | ADAPT | 187 fields, ~150 MIYA-specific. Keep the validators and the `.env.example`↔`Settings` test (`tests/test_env_example.py`). |
| DB plumbing | `miya/db/session.py` (`engine`, `SessionLocal`, `session_scope`, `get_session`), `miya/db/base.py` (`Base`, `created_at_column`, `money_column`), `alembic/`, `alembic.ini`, Makefile `migrate/revision/downgrade` | AS-IS | New initial migration; do not import MIYA's `Base` (see §4). |
| Models | `miya/db/models.py`: `Person` (display_name, `aliases[]` GIN, `telegram_id` unique, `telegram_username`, `phone`), `Heartbeat`, `UsageLog`, `ReminderLog`, `SheetRow`, `OutboxMessage` (+`_history_column`), `ChatMonitor`, `Interaction` | ADAPT | `Person`→`Company`+`Contact`; `Interaction`→`Evidence` (source URL, hash, excerpt, fetched_at); keep `Heartbeat/UsageLog/ReminderLog/SheetRow/OutboxMessage/ChatMonitor` verbatim. `Debt/DebtPayment/Promise/Transaction/Receipt/Claim/Memory/Passage/DailyReport/RecapDigest/Jarvis*` → NO. |
| Logging | `miya/logs.py`: `setup_logging`, `JsonFormatter`, `RedactBodies` | AS-IS | Logs carry ids/codes, never bodies — keep for scraped personal data. |
| Retry | `miya/services/retry.py`: `with_backoff(fn, attempts, base, cap, retry_on, sleep)`, `backoff_delay` | AS-IS | Foundation for HTTP collectors (429/5xx). |
| Claude extraction | `miya/services/extraction.py`: `get_client`, `API_FAILURES`, `MODEL_CLIENT_MAX_RETRIES=3`, `extract()` (structured outputs via `messages.parse(output_format=…)`, prompted-JSON fallback on grammar failure `is_grammar_failure/note_grammar_failure`, one retry with JSON nudge, refusal/`max_tokens` handling, never raises), `json_payload`, `parse_extraction_json`, `UsageTally`, `extraction_system_block` (cache_control), `build_request_params` (batch shape) | ADAPT | Replace `ExtractionResult` with lead schemas (`CompanyProfile`, `ImportSignal{goods, origin_cn, volume_m3_est, frequency, evidence}`, `ContactCard`, `RelevanceVerdict{b2b, imports_from_china, score, reasons}`); keep everything else. `miya/services/prompts.py` → NO (content). TelegramCustoms `ai/client.py:structured_request` is a simpler alternative; MIYA's is better tested. |
| Batch API | `miya/services/batch.py`: `submit_pending`, `collect_batch`, `collect_submitted`, `_retry_or_fallback`; `ConversationWindow.{custom_id,batch_id,attempts,status}` | ADAPT | Generalise to a `model_jobs` table for bulk page/company classification at half price (`BATCH_MAX_ATTEMPTS`, fallback to real-time). |
| Cost caps | `miya/services/usage.py`: `MODEL_PRICES`, `price_for`, `anthropic_cost_usd`, `record_anthropic_usage(session, model, operation, usage, batch, source_interaction_id)`; `miya/jarvis/costs.py`: `spend_today`, `ask` (refuses over `JARVIS_DAILY_COST_CAP_USD` before the call, records usage in its own transaction, unknown price counts as over cap), `COST_CAP_CODE`; `/xarajat`; `health.JARVIS_COST_CAP` | AS-IS | Rename operations list (`classify`, `extract`, `enrich`, `draft`). Add a per-collector daily token budget on top. |
| Entity resolution | `miya/services/people.py`: `normalise`, `_HONORIFICS`, `best_match` (rapidfuzz, `MATCH_THRESHOLD=85`, `QUESTION_THRESHOLD=70`, `AMBIGUITY_MARGIN=10`), `find_person→Match`, `resolve_person(name, telegram_id, telegram_username, phone, create, strict…)`, `find_by_phone` (last 9 digits), `merge_plan/merge_into`, `known_addresses`; `miya/services/text.py`: `fold_apostrophes`, `to_latin`, `transliterate`, `normalise_for_search`, `stem_query_token`, `UZ_SUFFIXES`, `RU_ENDINGS` | ADAPT (people) / AS-IS (text) | Company resolution: strip legal forms (`MChJ, OOO, ООО, ЧП, XK, LLC, QK, AJ`) instead of honorifics; exact keys = STIR, phone tail-9, `@username`, website host; fuzzy on normalised name; same ambiguity rule (ask, don't merge). |
| Existing-client codes | `miya/services/codes.py`: `canonical_client_code`, `find_client_codes`, `canonicalise_codes`, `find_waybills` | AS-IS | For the **suppression list** (a prospect that already has a GS code is a client, not a lead). |
| CSV/XLSX import | `miya/services/client_import.py`: `read_rows` (uz/ru/en header aliases, CSV sniff, xlsx), `plan_import→ImportPlan{attach,create,same,conflict,bad}`, `apply_import`; `miya/tools/import_clients.py` (`make import-clients FILE= [APPLY=1]`) | ADAPT | Code column is mandatory (`"no code column"`) — make optional; add `stir, company, telegram, instagram, website, m3` columns. Keep plan→apply two-phase and idempotency. Use it to load the ERP's client export as suppression. |
| Document parsing | `miya/services/documents.py` (pdf/xlsx/docx/txt/csv, 15,000-char cap) | AS-IS | Registry/stat exports, price lists, tender attachments. |
| Telegram reading (userbot) | `miya/userbot/main.py`: `run()` (`USERBOT_ENABLED` → `beat_userbot(enabled=False)`+`_idle_forever`, `connect()` not `start()`, `is_user_authorized` → `SystemExit`, `flood_sleep_threshold`, `raise_last_call_error`), `_reader_call` (flood-wait retry ≤60 s ×3), `_read_dialogs/sync_from_client` (`DIALOG_FLOOD_WAIT_MAX_SECONDS=60`, 3 attempts), `on_new_message/ingest_message`, `already_stored` (unique `ux_interactions_tg_message`), `catch_up_sweep` (`last_seen_message_id`), `fetch_backfills` (history only after owner's «Ha»), `chat_type_of/display_name_of/chat_title_of`, `install_stop_signals`; `miya/services/chats.py`: `sync_dialogs`, `ensure_monitor`, `toggle`, `list_monitors`, `default_monitor_enabled`; `miya/tools/userbot_login.py` | ADAPT | Lead signal lives in **groups/channels** (trade groups: Yiwu/Guangzhou cargo, optom), which MIYA defaults OFF (`default_monitor_enabled`: private on, groups off) — flip per use, keep per-chat toggles (`/chats`). Drop voice/vision/video; keep documents. Needs its **own Telegram account/session** (see §4). |
| Approved sending | `miya/userbot/sender.py`: `Sender.{run,poll_once,send_one,_pacing_step,_peer,_settle,_finish}`, `classify(exc, on_wire)→Verdict` (codes `flood_wait, flood_wait_long, peer_flood, account_banned/frozen/restricted, user_blocked, write_forbidden, paid_only, outcome_unknown…`), `random_id_for(idempotency_key)` (exactly-once), `reconcile` (reads back 20 msgs), `breaker_until`, `supervise/start/Handle`; constants `POLL_SECONDS=2`, `CLAIM_LIMIT=1`, `FLOOD_BREAKER_SECONDS=300`, `BREAKER_FOR=24h`; writes only in `_mark_read/_typing/_send`. `miya/jarvis/outbox.py`: `create/approve/cancel/expire/claim_ready` (`FOR UPDATE SKIP LOCKED` + `pg_try_advisory_xact_lock`, returns `[]` unless `jarvis_enabled`), `complete/fail/reconcile`, `TRANSITIONS`. `miya/jarvis/caps.py`: `day_counts`, `channel_sent_today`, `last_send_at` (SQL-counted, restart-proof). `miya/jarvis/policy.py`: `pause`, `trip_breaker`, `effective_mode` (env ceiling `JARVIS_MODE off/shadow/draft/auto`). `miya/jarvis/gate.py`: `evaluate`, `LINES` order. `tests/test_userbot.py`: `WRITE_CALLS`, `RAW_REQUEST`, `test_write_calls_only_in_sender_functions`, `test_history_is_only_read_where_the_owner_asked_for_it`, `test_sender_uses_claim_ready_only`, `test_the_kill_switch_stops_the_process_before_it_connects` | AS-IS (sender, outbox, caps, policy, tests) / ADAPT (gate) | Gate line `trigger` (`NO_TRIGGER`) refuses rows without an inbound message — MIYA was built for replies, **not cold outreach**. Replace with `owner_approved` + `cold_daily_cap` + `per_account_daily_cap` + `suppression`; drop `chat_state/guard` lines (Jarvis responder). Default `OUTREACH_MODE=draft`; make `auto` structurally impossible for intent `cold_first_contact`. |
| Owner review UX | `miya/bot/*` (owner-only: unset `OWNER_TELEGRAM_ID` locks everyone out; `formatting.escape`, `keyboards`, `notices`), `miya/services/approvals.py` (state machine `pending→asked→approved→done/declined/expired`, shared by 3 processes through the DB), `miya/worker/main.py`: `deliver/notify/notify_parts` (HTML → plain-text retry), question budget, `QUIET_HOURS` | ADAPT | Bot = owner's review desk: candidate cards (✅ CRM'ga yubor / ✖️ / 🔀 birlashtir / 🔒 shaxsiy), `/manbalar` (collector toggles, modelled on `/chats`), `/xarajat`, `/holat`, `/toxtat`. Approvals state moves from `interactions.media` JSONB to a `lead_reviews` table. Managers never use this bot — they get leads from gsrwms. |
| Health & alerts | `miya/services/health.py`: `beat/beats/gather→Status/problems→[Problem]/alerts_due/mark_alerted/mark_recovered` (ledger in `reminder_log`), `COMPONENTS`, `JOB_PREFIX="job:"`, `ANTHROPIC_SILENCE`; `miya/worker/main.py`: `heartbeat_job`, `record_job_event`, `_on_job_event`, `health_job`, `_deadman_ping` (`DEADMAN_PING_URL`); `miya/tools/doctor.py` (`check/summary/exit_code`) | AS-IS | Add components `collector:<name>`; problems `collector_blocked` (429/403 streak), `push_404` (CRM key wrong). |
| Backups | `miya/services/backup.py`: `create_backup` (`pg_dump -Fc \| age`, no recipient → writes nothing), `split_for_telegram`, prune; `miya/tools/restore.py`; `miya/services/restore_drill.py`; Makefile `backup/backup-key/backup-key-show/restore`; `Dockerfile` (age + PGDG `postgresql-client-16`) | AS-IS | — |
| Sheets mirror | `miya/services/sheets.py`: `SheetsAPI` ABC, `GoogleSheetsAPI` (service account, `RAW`, `num_retries=5`), `get_api`, `config_problem`; `miya/services/sheets_sync.py`: `plan/_plan_rows/_due/_write/_record/sync/run`, `retry_at`, `content_hash`, `layout_requests`, `tombstone_missing`, `request_resync`; `SheetRow` | AS-IS (engine) / ADAPT (builders) | Replace `build_tasks_promises/build_followups/build_receipts` + `HEADERS` (from `miya/bot/replies.py`) with `build_companies/build_leads/build_pushes`. Safety already there: RAW values, `safety.display_name`, hash-only rewrites, tombstones. |
| Safety shields | `miya/services/safety.py`: `classify_attachment`, `sniff_magic`, `extract_urls`, `link_verdict`, `scam_verdict(MessageFeatures)`, `display_name` (bidi/zero-width strip), `script_of/dominant_script`, `is_loan_request` | AS-IS | Pure, no DB. Use on inbound replies from prospects and to scrub scraped names before Sheets/CRM. |
| Embeddings/pgvector | `miya/services/embeddings.py`: `LocalEmbedder/RemoteEmbedder`, `POST /v1/embed`; `Memory/Passage` HNSW + `tsvector/search_norm` | ADAPT (optional) | Near-duplicate company descriptions, semantic "imports from China" retrieval. Costs ~2 GB RAM (`API_MEM_LIMIT=4g`). Start without it; `rapidfuzz` + `normalise_for_search` suffice for v1. |
| Purge | `miya/services/purge.py`: `plan_person/plan_chat/plan_range/execute` (count first, cascade, unlink files) | ADAPT | "Forget a company/contact" (DNC + legal requests). |
| Scheduler skeleton | `miya/worker/main.py`: `run()` (`AsyncIOScheduler(timezone)`, `add_job` × 30, `EVENT_JOB_*` listener), `catch_up()`, `_prune_job_heartbeats` | ADAPT | Jobs: per-collector cron, `enrich_job`, `score_job`, `review_cards_job`, `crm_push_job`, `outbox reconcile`, `sheets_sync_job`, `health_job`, `backup_job`, `retention_job`. |
| Internal API | `miya/api/{main,deps,middleware}.py` (127.0.0.1, bearer constant-time) | ADAPT | `/health`, `/v1/companies`, `/v1/leads`, `/v1/stats`. |
| Docker/Make | `docker-compose.yml` (db/init/api/bot/worker/userbot, log rotation, `oom_score_adj`), `Makefile` (`up/doctor/migrate/reload/userbot-login/backup/restore/update`) | AS-IS | Drop `syncthing`, `hf_cache`, torch/ffmpeg from the Dockerfile. |
| NO | `amounts, money_events, money_notices, sms_money, receipts, claims, call_recordings, phone_events, transcription, audio, vision, recaps, brief, planner, reminders, nudges, loops, when, windows, rag, recall, queries, passages, gcal, memories, profiles`, `miya/jarvis/{responder,intent,draft,style,style_prompt,kb,qbank,stalls,rollout,cards,commands,status}.py`, `android/` | NO | Domain logic for debts/promises/money/replies. `redlines.py` idea (never emit price/promise unless from an approved KB) is worth re-implementing as a 50-line regex gate for outreach drafts. |

---

## 2. gsrwms.uz lead contract (from `/home/user/logisticserp`) and what a push must send

There are **two doors**. The one the site uses is not a push API.

### 2a. `GET https://gsrwms.uz/api/lead/assign` — manager picker for a browser (not usable by the agent)

Code: `src/app/api/lead/assign/route.ts`, rules `src/modules/wms/crm/site-assign-rules.ts`, gate `src/modules/wms/crm/site-assign-gate.ts`, landing `src/modules/wms/crm/site-assign.ts` (`assignForTag`, `landSiteTag`), spec `docs/WEBSITE-LEAD-ASSIGN.md`. Site side: `gsrlogistics.uz/src/lib/lead-client.ts` (`askManager`, `assign`), `leads.ts`, `site.ts` (`leadAssign`, `leadTeams`, `telegramCompany='bekzodkodirov556'`).

- Query: `team` ∈ `cargo|buying|general` (else `general`; site maps `truck/air/rail/customs/warehouse→cargo`, `sourcing/buying/equipment/cars→buying`), `lead` (one-off code), `tag` = topic slug `^[a-z0-9][a-z0-9_-]{0,39}$` (site sends `yuk|xarid|savol` or the service slug), `page` (pathname), `lang` ∈ `uz ru en zh zh-CN`.
- Response: always `200 {"username": "<tg username>"|null}`; `null` on refused origin, rate (20/10 min per IP, 120/min overall, 4 concurrent), invalid code, 0.8 s deadline, nobody free, any error. `HEAD` never assigns. **No `Origin` header (any server-side caller) → always `null`.**
- Effect: an offer row in `lead_assignments` (tag, team, topic, page, lang, user_id, username; 24 h validity); the lead opens only when the visitor's **first** Telegram message to that manager carries the code and `tg-listen` sees it (`landSiteTag`). Source «Sayt».
- **Finding (integration defect as of these checkouts):** the site mints `GSR-` codes (`lead-client.ts:51 CODE_RE=/^GSR-[A-HJ-NP-Z2-9]{10}$/`, message `Kod: GSR-…`), but the ERP accepts only `LEAD_TAG_RE=/^LEAD-[A-Z0-9]{5,16}$/` (`site-assign-rules.ts:41`, `TAG_IN_TEXT`, and DB `CHECK` in `src/modules/platform/db/migrations/0110_lead_assign.sql:42`). `readOfferInput` → `tag=null` → `count('invalid')` → `{"username":null}` → every visitor falls back to `@bekzodkodirov556` and no lead auto-opens. Unless the deployed gsrwms differs, fix one constant on one side (site `CODE_PREFIX`, or ERP regex + CHECK + listener). Also required in the ERP: setting `lead_assign_origins` = `https://gsrlogistics.uz https://www.gsrlogistics.uz`; managers ticked on `/admin/taqsimot → Saytdan so'rovlar`. Verify with one curl only (each answered call is real load): `curl -s -H 'Origin: https://gsrlogistics.uz' 'https://gsrwms.uz/api/lead/assign?team=general&lead=LEAD-TESTAB2345'`.

### 2b. `POST https://gsrwms.uz/api/leads/in/<source>` — the push door the agent must use

Code: `src/app/api/leads/in/[source]/route.ts`; parser `src/modules/wms/crm/inbound-webhook.ts` (`parseInboundPayload`, `secretFrom`, `secretMatches`, `refWithoutSecret`); landing `src/modules/wms/crm/inbound.ts` (`landInboundLead`, `INBOUND_SOURCE_KEYS`, `PHONE_CAP_PER_DAY=3`, `SOURCE_CAP_PER_DAY=200`, `INBOUND_JOIN_DAYS=30`, `inboundPhoneDigits` = last 9 digits); question mapping `src/modules/wms/crm/field-map.ts` (`capturePairs`: `MAX_PAIRS=30`, key ≤80, value ≤300; targets `kub|kg|field|note`); routing `src/modules/wms/crm/routing.ts` (`matchRoute`: `sourceKey`/`keyword`/`minM3–maxM3` → `userIds`, first active match; else `nextInboundOwner` over `users.inbound_rota`, least loaded today); notifications `inbound-notify.ts`; docs `docs/LEAD-ADS.md` §4–7.

| Item | Contract |
|---|---|
| URL | `/api/leads/in/<source>`, `<source>` ∈ `meta instagram facebook telegram tiktok google sayt other` (hard-coded `INBOUND_SOURCE_KEYS`). Any other path → 404. |
| Auth | Per-source secret, created in UI: *Kelgan arizalar → Reklama havolalari → source → «Platformaning o'z formasi» → «Yoqish va kalit yaratish»* (`setSourceWebhookSecret`, stored in `lead_sources.webhook_secret`). Sent as header `X-ERP-Key: <secret>` or body `"key"`. Unconfigured source, wrong key, unreadable JSON → **404** (indistinguishable by design). |
| Body (plain shape) | `{"name"\|"ism", "phone"\|"telefon", "note"\|"izoh", "external_id"\|"id", ...extras}`. Every unknown top-level key becomes a note line `key: value` **and** a `fields` pair (max 30) that the owner can map once on `/admin/taqsimot → Forma savollari` to `kub` (→ `leads.quoted_volume_m3`), `kg`, a custom field, or note. Secret-named keys (`key, secret, token, google_key`) are stripped. |
| Response | Always `200 {"ok":true}` (created / joined / known client / dropped look identical). No lead id is returned. |
| Landing order | drop `no_contact` if `!phone && !name` → replay fence on (`channel='webhook'`, `external_id`) → caps (3/phone/24 h, 200/source/24 h; capped arrivals still recorded in `lead_intakes`) → **known client by phone** (enquiry lands on the client card, the client's manager is notified, no lead) → open lead on same phone within 30 days (joined, owner unchanged) → new lead (`leads.name, phone, note, source_id, stage_id, owner_id, inbound_at`), owner from routing rules/rota. |
| After landing | Owner gets «🆕 Yangi lid» via the company bot with «📞 Bog'landim»; ownerless → super admin; `inbound_contact_minutes` (default 15, business hours) untouched-lead reminder; funnel report per source. |

**What the agent must therefore send (and keep locally):**

```json
POST /api/leads/in/<source>           X-ERP-Key: <secret>
{
  "external_id": "cs:<company_id>:<yyyymm>",      // replay fence; new id = new enquiry
  "name": "Rustam Karimov — OOO Textile Trade",  // contact person, then company; ≤120
  "phone": "+998 90 123 45 67",                   // required in practice (identity = last 9 digits)
  "note": "Xitoydan mato va furnitura, oyiga ~1 konteyner. Manba: t.me/… (2026-10-20)", // ≤1000
  "kub": "25",            // map once → quoted_volume_m3; enables routing rule min_m3=3 → cargo people
  "company": "OOO Textile Trade", "stir": "30xxxxxxx", "telegram": "@…", "instagram": "…",
  "website": "…", "address": "Toshkent, …", "imports": "mato, furnitura", "score": "82",
  "evidence": "https://…", "utm_source": "clientscrapper"
}
```

Rules forced by the contract:
1. **Source key.** Nothing in the allowlist means "agent". Add `agent` (or `qidiruv`) to `INBOUND_SOURCE_KEYS` + a `lead_sources` row (`key='agent'`) in logisticserp (one constant + one seed/migration) so the funnel report shows the agent's conversion separately; until then use `other` with `utm_source` (report shows «Boshqa»).
2. **Phone-only identity.** Telegram-only prospects would create a new lead on every push (no phone to join on) — push only prospects with a phone; keep Telegram-only ones in the agent's own review queue for owner-approved outreach.
3. **Suppression before push.** A phone already in the ERP client book is routed to that client's manager as a "client question", not a lead — the agent must hold the client list (import the ERP's clients export via `client_import`) and never push it.
4. **Idempotency and outcome are the agent's job.** `200` tells nothing → write `push_log(external_id, payload_hash, sent_at, http_status)`; never re-send a `200`; retry only network/5xx with `with_backoff`; `404` = key/source misconfigured → trip a push breaker and alert the owner (`health` problem `push_404`).
5. **Throttle.** `PUSH_DAILY_CAP` (e.g. 20–30) ≪ 200; ≤1 push per phone per day; batch pushes inside business hours so the 15-minute contact clock is fair to managers.
6. **Routing uses the volume you send.** Ask the owner to create an `inbound_routes` rule `source=agent, min_m3=3 → cargo managers` once `kub` is mapped; leads under 3 m³ never leave the agent (B2C/out of scope).

---

## 3. Proposed `clientScrapper` layout (Python 3.12, MIYA's four-process shape)

```
clientScrapper/
  pyproject.toml · requirements.txt (MIYA pins minus sentence-transformers/torch; keep anthropic, aiogram, Telethon, rapidfuzz, httpx, openpyxl, pdfplumber, google-api-python-client)
  Dockerfile (← dailyJournal/Dockerfile: drop torch/ffmpeg, keep age + postgresql-client-16)
  docker-compose.yml (← MIYA: db, init, api, bot, worker, userbot; no syncthing/hf_cache)
  Makefile (← MIYA: up/doctor/migrate/reload/userbot-login/backup/restore/update/import-clients)
  .env.example · alembic/ (fresh 0001)
  clientscrapper/
    config.py            ← miya/config.py. Keys: DATABASE_URL, ANTHROPIC_API_KEY, EXTRACT_MODEL, REASON_MODEL, DAILY_COST_CAP_USD,
                           OWNER_TELEGRAM_ID, ASSISTANT_BOT_TOKEN, API_BEARER_TOKEN,
                           COLLECTORS_ENABLED (kill switch for all crawling), COLLECTOR_RPS_DEFAULT, COLLECTOR_DAILY_PAGE_BUDGET,
                           USERBOT_ENABLED, TELETHON_API_ID/HASH/SESSION, TELEGRAM_SEND_ENABLED,
                           OUTREACH_MODE=off|shadow|draft (auto not offered for cold contact), OUTREACH_DAILY_CAP, OUTREACH_PER_CHAT_CAP=1, SEND_MIN_GAP_SECONDS, QUIET_HOURS,
                           CRM_PUSH_ENABLED, CRM_PUSH_URL, CRM_PUSH_SOURCE, CRM_PUSH_KEY, PUSH_DAILY_CAP,
                           SHEETS_*, BACKUP_*, DEADMAN_PING_URL
    logs.py              ← miya/logs.py (as-is)
    db/base.py, session.py, enums.py          ← as-is
    db/models.py         NEW tables: companies, contacts, company_aliases, evidence, sources, crawl_jobs, signals, lead_candidates
                           (state: found→enriched→scored→review→approved|rejected→pushed), lead_reviews (← approvals.py pattern),
                           push_log, suppression (clients, DNC, B2C); copied: outbox_messages, chat_monitors, interactions(trimmed),
                           heartbeats, usage_log, reminder_log, sheet_rows, model_jobs (← conversation_windows batch columns)
    services/
      text.py, safety.py, retry.py, documents.py, usage.py, health.py, backup.py, restore_drill.py, sheets.py   ← as-is
      claude.py          ← extraction.py (client, _one_attempt, UsageTally, json_payload) + schemas.py NEW (lead schemas)
      batch.py           ← services/batch.py generalised to model_jobs
      costs.py           ← jarvis/costs.py (ask(), spend_today, cap)
      companies.py       ← people.py (resolve_company, best_match, merge_into, find_by_phone)
      codes.py           ← as-is (suppression by GS code)
      client_import.py   ← adapted (code optional; stir/company/m3 columns)
      suppression.py     NEW (clients import, DNC, B2C heuristics: Taobao/“o'zim uchun”, private person signals)
      scoring.py         NEW deterministic: volume signals, category, recency, contactability, evidence count → score + reasons[]
      enrich.py          NEW: STIR/registry lookups, website/Instagram public fields, Claude classification via claude.py
      crm_push.py        NEW: POST /api/leads/in/<source>, external_id, push_log, breaker on 404/5xx, PUSH_DAILY_CAP
      sheets_sync.py     ← engine as-is; builders build_companies/build_leads/build_pushes
      purge.py           ← adapted
      http.py            NEW: httpx client, per-host token bucket, robots.txt, Retry-After, ETag/hash cache, UA; uses retry.with_backoff
    collectors/          NEW. Each: collect(session, budget) -> list[Evidence]; pure parsers tested on HTML fixtures; registered in
                           collectors/__init__.py with enabled flag, rps, heartbeat job:collector:<name>; per-source breaker on 403/429 streaks
      base.py · web_search.py · directories.py (business directories/2GIS/Yandex Business exports) · marketplaces.py (public seller pages)
      tenders.py · vacancies.py (hiring “VED menejer”, “Xitoy bilan ishlash”) · telegram_groups.py (reads messages the userbot stored — no network)
      instagram_public.py · registry_import.py (files the owner drops: stat/customs exports) · china_side.py (optional, low yield)
    userbot/
      main.py            ← miya/userbot/main.py trimmed (run(), kill-switch idle, connect(), flood handling, dialog sync, ChatMonitor,
                           on_new_message → interactions, catch_up_sweep; documents only, no voice/vision)
      sender.py          ← as-is
    outbox/              ← miya/jarvis/{outbox,gate,caps,policy,chatkey}.py + services/authorship.py
      gate.py LINES: kill_switch, pause, mode, rules(DNC/suppression/personal), safety, owner_approved, caps(daily, per-account, per-chat=1),
                       payload(plain text), quiet_hours   (drop trigger/chat_state/guard)
      redlines.py        NEW small: block digits+currency, delivery-time promises in drafts
    bot/                 ← miya/bot skeleton (owner-only). /lidlar (cards: ✅ CRM'ga yubor · ✖️ · 🔀 · 🔒), /manbalar (collector toggles ← /chats),
                           /chats, /qoralamalar (outreach drafts), /toxtat & /yoq (policy.pause), /xarajat, /holat, /import, /unut, /qidir
    worker/main.py       ← scheduler skeleton; jobs: collectors (cron per source), enrich_job, score_job, review_cards_job, crm_push_job,
                           outbox_reconcile, sheets_sync_job, health_job, heartbeat_job, backup_job, retention_job
    api/                 ← thin bearer API: /health, /v1/companies, /v1/leads, /v1/stats
    tools/doctor.py, userbot_login.py, restore.py, import_clients.py, reprice_usage.py   ← as-is/adapted
  tests/   ported: test_userbot.py (AST WRITE_CALLS/RAW_REQUEST guard, claim_ready-only, kill switch idles, no history reads outside owner-asked),
           test_outbox.py (gate order), test_safety_*.py, test_client_import.py, test_sheets_sync.py, test_health.py, test_usage_and_backup.py,
           test_env_example.py; new: collector fixtures, scoring, crm_push against a fake server (200/404/500/timeout), company resolution,
           “no collector module imports telethon or anthropic” AST test, “no push without approved state” test
```

Safety properties carried over (and the mechanism that enforces each): no unapproved sending — every Telegram write goes through `outbox.claim_ready` and the AST test, `OUTREACH_MODE` ceiling, cold first contact requires `approved_by='owner'` in the gate; flood-wait handling — `sender.classify` verdicts, `FLOOD_BREAKER_SECONDS`, `policy.trip_breaker`, `flood_sleep_threshold`, plus `http.py` per-host limits and per-collector breakers; cost caps — `costs.ask` before every model call, `usage_log` priced from config, Batch API for bulk; one-flag kill switches — `USERBOT_ENABLED` (idles, never exits), `COLLECTORS_ENABLED`, `TELEGRAM_SEND_ENABLED`, `CRM_PUSH_ENABLED`, and runtime `/toxtat` (`policy.pause` global, no restart); data hygiene — `RedactBodies` logs, `age`-encrypted backups, `purge`, suppression of B2C and existing clients.

---

## 4. Coupling to MIYA vs copying

**Importing MIYA as a dependency (pip -e / submodule) — not recommended:**
- `miya.config.settings` is a module-level singleton with validators (`API_BEARER_TOKEN ≥ 32`, `BACKUP_AGE_RECIPIENT` must start `age1`, `MORNING_BRIEF_TIME` vs `QUIET_HOURS`) — importing any MIYA module requires a full MIYA `.env`.
- Import graph is dense: `people.py` → `codes`, `text`, `db.models` (all 30+ tables on one `Base`) → Alembic autogenerate in the new repo would see MIYA's schema; `outbox.py` runs `redlines.install()` at import; `sender._peer` imports `miya.userbot.main` (reverse edge) → pulls `transcription`, `vision`, `documents`, aiogram.
- MIYA changes fast (JV-01..JV-23 landed 2026-10-22); renames would break the agent at deploy time; MIYA's mypy-strict scope and tests assume its own layout.
- Data boundary: MIYA holds the owner's private ledger (debts, call transcripts). A sales database that managers see must not share a DB, container, or backup key with it.
- Telegram: one Telethon session per account; a second client on the same session string risks `AuthKeyDuplicatedError`, and the gsrwms `tg-listen` (gramjs) already sits on managers' accounts. The agent needs **its own company Telegram account** (userbot login via `tools/userbot_login.py`), never MIYA's owner session nor a manager's. Group-reading on that account is the only Telegram collector that is safe to run.

**Copying (vendoring) — recommended, with two mitigations:**
- Risk: ~2k lines of hard-won edge cases (`sender.py`, `outbox.py`, `gate.py`, `sheets_sync.py`) fork; fixes in MIYA do not propagate. Mitigate by (a) a `# VENDORED_FROM dailyJournal@6b1f67f <path>` header on every copied file and a `tools/sync_from_miya.py` that diffs the listed files against a MIYA checkout, and (b) copying the **tests** with the code so behaviour is pinned.
- Later, if both stabilise, extract a shared `miya-core` package (`text, safety, retry, health, backup, sheets engine, outbox+sender`) — not before: a shared package now couples two moving targets.

**gsrwms side changes the agent depends on (small, in `/home/user/logisticserp`):** add source key `agent`; create its webhook secret in the UI; map `kub` in «Forma savollori»; add routing rule `agent, min_m3=3`; fix the `GSR-`/`LEAD-` prefix mismatch (§2a); no read API exists for lead outcomes (the agent reports pushes in Sheets/`/holat`; conversion is read in the ERP's «Manba bo'yicha natija»).

**Unknowns:** whether production gsrwms.uz equals this 2026-10-15 checkout; whether the customs-stat Excel behind TelegramCustoms carries declarant columns (would make it the strongest B2B source); the legal basis for storing contact data of individuals at companies under Uzbek personal-data law — treat as a question for the owner's lawyer, keep the `purge` path and DNC list from day one.