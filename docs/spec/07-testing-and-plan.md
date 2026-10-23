# 07 — Testing, CI, definition of done, implementation plan

Scope: how the build is verified (test strategy, fixtures, guards, CI, pre-push), what "done" means per component and for the MVP, the ordered task list T-01…T-40 the implementing model follows, the Day-1 checklist, the risk register and the glossary. Names of tables, modules, jobs and tests are the ones `02`–`05` already use; where this file introduces a new test or tool it says so. The shared context (business, budget, ERP contract, env names) is assumed. `06-bot-ops-security.md` owns the bot command list, health keys and deployment; this file references them.

## 1. Test strategy

### 1.0 Layout, conventions, fixtures

```
tests/
  conftest.py            session fixture (PG), CS_REQUIRE_DB, truncate order, FakeClock, fake_erp fixture
  fake_erp.py            05 §B.4 — httpx.MockTransport ERP
  fakes.py               FakeFetcher (fixture-backed), FakeSerp, FakeRegistry, FakePlaces, FakeGraph, FakeAnthropic (recorded JSON), FakeClock
  fixtures/<source_key>/ recorded pages + README.md (one line per file: date · url · sha256 · status · bytes · scrubbed? · note)
  fixtures/erp/          suppression_full.json suppression_delta.json outcomes.json clients.xlsx — captured from fullprompt `agent-feed` (05 §B.4)
  fixtures/telegram/     hand-written synthetic messages (never real ones)
  fixtures/model/        recorded Claude responses, already pseudonymised
  fixtures/uz_corpus.py  redline corpus (≥30 bad / ≥10 good openers), playbook templates A–E, card samples
  collectors/            test_keywords.py test_http.py test_<key>_parse.py (one per collector, 03 §1.5)
  live/                  test_<key>_live.py — @pytest.mark.live, LIVE=1 only, run from the VPS by `make smoke`
  test_*.py              everything else, flat, named as 02–05 name them
```

| Rule | Detail |
|---|---|
| Markers | `live` (network, VPS only), `slow` (CS_RUN_SLOW=1), default = pure or Postgres. `pytest -q` on a laptop without Postgres **skips** DB tests; CI sets `CS_REQUIRE_DB=1` and a skip becomes a failure (MIYA `conftest.py` pattern; a suite that silently ran without its database cannot pass). |
| DB fixture | `session` yields an `AsyncSession` after `TRUNCATE <all tables except sources, scoring_weights> RESTART IDENTITY CASCADE` in **one statement**. `TRUNCATE` is used on purpose: the `cs_append_only` triggers refuse row `DELETE` on `push_log`, `outcomes`, `merge_log`, `purge_log`, `usage_log`, and `TRUNCATE` does not fire row triggers. `sources` and `scoring_weights` are migration-seeded configuration: a test that flips `sources.enabled/config` or activates a weights version snapshots in `beforeAll`-style fixture setup and restores it (fullprompt #716 — a restore written after the fact restores what it found). |
| Time | Every function under test takes `now:` explicitly; tests pass fixed Tashkent-aware datetimes. Anything compared against the real clock derives from the real clock. `CS_TIME_TRAVEL=newyear|+400d` (port of MIYA `tests/timetravel.py`) runs the non-DB suite at another moment in CI (§2.1) — the Tashkent-day, push-window and decay maths must not depend on the calendar of the day the test was written. |
| Synthetic data | Visibly fake: companies «Namuna …», phones `+998 90 000 00 NN`, STIR `3000000NN`, handles `namuna_<n>`, domains `*.example.uz`. No ERP export, no real person, no real phone ever enters `tests/` or `docs/`. `tests/test_forbidden_strings.py` (new) greps the tree for the strings in the CI secret `CS_FORBIDDEN_STRINGS` (real names, handles, phone tails the owner lists; empty until set) — the MIYA username guard, generalised. |
| Red proof | A test for a fix is shown to fail without the fix by a string edit of the code, then undone by string edit — never `git stash`/`git checkout` (fullprompt #430). Commit before a series of red proofs. |
| Assertion shape | A parser test asserts concrete field values, never `len(result) > 0` alone; a parser that returns `[]` on its own fixture fails. A test that restates the code (re-implements the formula) proves nothing — call the real function with inputs and pin outputs. |

### 1.1 Pure parser tests with fixtures

Every collector is `parse_<page>(body: bytes, *, url, fetched_at) -> list[Observation]` (03 §1.2) tested on recorded bytes; the fetch layer is tested once (`tests/collectors/test_http.py`).

**Capture tool** — `tools/record_fixture.py` (new):

```
python -m clientscrapper.tools.record_fixture <url> --key <source_key> --name <name>
       [--json] [--header k=v]... [--post-json file] [--scrub auto|on|off] [--note "..."]
```

Goes through `Fetcher` (UA, robots, rate limits apply), writes `tests/fixtures/<key>/<name>.<html|json|xml>`, strips `Set-Cookie`/`Authorization` from any stored headers, and appends the README line. `--scrub auto` (default) applies `tools/scrub_fixture.py` for person-tier sources (`olx_business`, `instagram_discovery`, `telegram_monitor`, any page whose body matches `IP|ИП|YaTT|ЯТТ` next to a person-shaped name): phones → deterministic synthetic `+998 90 000 00 NN` (same real number → same NN inside one file), `@handles` → `namuna_NN`, person names in `user.name`-shaped fields → `Seller NN`; structure, keys and lengths are kept so selectors still match. Company-tier public pages (directories, hh employers, exhibitors, brand pages, registries) are stored verbatim. The README line says `scrubbed: yes|no`.

**Refresh policy.** A fixture is re-recorded when (a) its `@live` smoke test turns red (selector drift), (b) the quarterly `make smoke` run on the VPS (1st of Jan/Apr/Jul/Oct, cron, result posted to the owner digest as «Tekshiruv: N ta manba oʻzgardi») lists it, or (c) a parser change needs a new shape. A refresh is one commit containing the new fixture, the README line, and the parser change; the old file is kept only as `<name>.<yyyymmdd>.html` when the parser must support both shapes (then both are tested). Fixtures are never edited by hand except through the scrubber.

**Per collector** (`tests/collectors/test_<key>_parse.py`): each `parse_*` × each fixture → exact `company_name_raw`, `phones` (raw strings), `telegram`, `instagram`, `website`, `signal_types`, `excerpt` prefix, `external_ids`, `attrs` numbers; plus the negative fixture (a page of the same site that must yield `[]` — e.g. a 404 page, a CAPTCHA page, a non-UZ exhibitor row) and the paginator-stop case where it applies.

### 1.2 AST guard tests (ported from MIYA `tests/test_userbot.py`)

Guards are source-shape tests: they parse the package with `ast`, strip nothing by regex, and fail the build on a shape, not on behaviour. The three helpers (`_calls_by_function`, `_raw_requests_by_function`, `_imports_telethon`) are copied from MIYA with a `# VENDORED_FROM dailyJournal@6b1f67f tests/test_userbot.py` header.

| Test (file) | Invariant | Mechanism |
|---|---|---|
| `tests/test_no_telegram_writes.py` | **No code path writes to Telegram as the reader account.** | Scan = every module under `clientscrapper/userbot/` + every module anywhere that imports `telethon`. `WRITE_CALLS` (MIYA set, unchanged) may appear **nowhere** — there is no sender, so `SENDER_WRITE_FUNCTIONS` is empty and the exemption branch is deleted. `RAW_REQUEST` names are allowed only in `RAW_ALLOWED = {("userbot/joiner.py","join_one"): {"JoinChannelRequest","GetFullChannelRequest"}, ("userbot/about.py","read_about"): {"GetFullChannelRequest","GetFullUserRequest"}}`; any other `*Request` name anywhere (called, imported, aliased, attribute) fails. `JoinChannelRequest` outside `join_one` fails (03: ≤3 joins/day, one function). A second test asserts `join_one` is reached only from `join_daily`, whose cap test runs with a fake clock. |
| same file | **History is read only where the owner asked.** | `HISTORY_CALLS = {get_messages, iter_messages}` allowed only in `backfill_chat` (`userbot/backfill.py`) and `read_about`; the live handler lives on `events.NewMessage`. |
| same file | **Kill switch idles, never exits; missing session fails loudly.** | `USERBOT_ENABLED=false` → `_idle_forever` called, `TelegramClient` never constructed (monkeypatched to raise); `TELETHON_SESSION=""` → `SystemExit` with the key named. |
| `tests/test_collector_imports.py` (new) | **No collector imports `telethon` or `anthropic` directly.** Collectors call `services.extraction` and read `interactions`; the network is `services/http.py`. | Walk `clientscrapper/collectors/**`; fail on `import telethon`, `from telethon`, `import anthropic`, `from anthropic`, and on `import httpx` outside `services/http.py`, `services/serp.py`, `services/erp_feeds.py`, `services/crm_push.py` (collectors use `Fetcher`, never a bare client). |
| `tests/test_push_requires_approved.py` (new) | **No push without `approved` state.** | AST half: `ErpClient.post_lead` / `PUSH_PATH` are referenced only in `services/crm_push.py::_post`, reached only from `push_one`; `push_one` contains a `FOR UPDATE` select and a comparison with `'approved'`. Behavioural half (Postgres + fake ERP): a candidate in each of `found, enriching, scored, review, rejected, pushed, suppressed, excluded, hold` → `push_one` returns `Refusal('not_approved')` and `fake.posted == []`; `approved` with `approved_by=NULL` is refused by the DB CHECK before it gets that far. |
| `tests/test_paid_calls_via_costs.py` (new) | **Every paid call goes through `costs.ask`.** | Allow-list of (module, function) that may call `client.messages.parse/create/batches.*` (`services/extraction.py`, `services/model_jobs.py`), `api.search.brave.com`, `searchapi.api.cloud.yandex.net`, `places.googleapis.com`, `api.apify.com`, `graph.facebook.com` URL constants (`services/serp.py`, `collectors/maps.py`, `collectors/instagram_discovery.py`, `collectors/olx_business.py`, `collectors/uzum_sellers.py`); each allowed function must contain a call to `costs.ask` or be called only from one that does (call-graph walk within the module). `obey_robots=False` only at the allow-listed API call sites (03 §1.1). `/limited-phones/` (OLX phone reveal) must not appear anywhere (`tests/test_no_phone_reveal.py`, a one-liner grep with comments stripped first — fullprompt #725). |
| `tests/test_env_example.py` (MIYA port) | **`.env.example` ↔ `Settings`.** | No comment-shaped value, no inline comment on an assignment line; `Settings(_env_file='.env.example')` builds with blank credentials; every `Settings` field appears as a `KEY=` line and every `KEY=` line is a field (both directions — the shared env list plus 03's additions `IG_GRAPH_TOKEN`, `IG_BUSINESS_ACCOUNT_ID`, `TWOGIS_API_KEY`, `HH_APP_TOKEN`, `EVIDENCE_DIR`, `TEST_PHONE`); `get_settings()` on a bad value exits with one line naming the key, no traceback. |
| `tests/test_pii_columns.py`, `test_fk_indexes.py`, `test_enums_frozen.py`, `test_weights_seed.py`, `test_pseudonymised_requests.py`, `test_migrations.py` | as defined in 02 §0, §2.6, §2.7, §5 | unchanged |
| `tests/test_vendored_headers.py` (new) | Every file listed as AS-IS/ADAPT in `docs/kod-audit-miya.md` §1 carries `# VENDORED_FROM dailyJournal@<sha> <path>` on line 1–3. | regex over the file head; the list of expected files is in the test, mirrored from the memo. |
| `tests/test_dockerfile.py` (new) | Image has no `torch`/`ffmpeg`/CUDA index, runs as uid 10001, installs `age` and `postgresql-client-16`; compose has no published Postgres port other than `127.0.0.1`. | string checks on `Dockerfile`/`docker-compose.yml` (fullprompt `dockerfile-offline.test.ts` pattern). |

### 1.3 Scoring golden tests (`tests/test_scoring.py`)

Inputs are `SignalView`/`ContactView` fixtures (no DB), weights = version 1 loaded from `enums`/the seed tuple, `now` fixed.

| Case | Expected |
|---|---|
| 04 §9.1 Telegram «2 kub mato» before enrichment | total **52**, tier `warm`, volume 20, origin 20, recency 10, contact 2 |
| same after registry + directory phone | **70**, `hot`; solidity 12, contact 8, corroboration 0 (registry/directory are not volume/origin families) |
| 04 §9.2 OLX seller, 14 ads, no phone | **65**, `warm` (≥60 without usable phone) |
| 04 §9.3 hh vacancy before/after site + registry | **48** → **87**, `hot` with a landline (usable, no mobile points), corroboration 5 |
| Caps bind | `vol_container 40 + vol_stated_3plus 30` → volume `raw 70, used 40` |
| Decay | `vol_stated_3plus` observed 181 d ago → 15 points, `factor 0.5` in breakdown; 179 d → 30 |
| Negatives | `neg_b2c_words` −40 with no volume signal → total clamps at 0 when positives < 40; with `vol_stated_3plus` present the B2C negative is not emitted by `from_rules` (rule 1) — tested in `test_signals.py`, not here |
| Clamp | positives 100 + no negatives → 100; all negatives → 0, never negative |
| Shared phone | `shared_phone=true` mobile → no `derived_contact_mobile`; usable-phone rule in `test_push_gate` |
| Derived keys | never read from `signals` rows named `derived_*` (a fixture containing one is ignored) |
| Determinism | `score(x) == score(x)` bytewise after `json.dumps(sort_keys=True)`; breakdown validates against `schemas/score_breakdown.json` |
| Weights contract | `tests/test_weights_seed.py`: 26 keys × version 1, caps sum 100, `validate_version` refuses a ±30 % jump unless `proposed_by` starts with `owner:` |

### 1.4 Resolver tests (`tests/test_resolver.py`, `tests/test_shared_phone.py`; Postgres)

Bands are asserted through the real `companies._score` (rapidfuzz over `loose_key`) — the test states the expected **band**, not a hand-computed number.

| Fixture pair | Expected |
|---|---|
| `«NAMUNA TEKSTIL SAVDO» MChJ` vs `ООО «Намуна Текстиль Савдо»` | exact `name_key` → attach, `how='fuzzy'`, score 100, alias learned |
| `Gozal Mebel XK` vs `Goʻzal Mebel Plus` | ≥ 90 → attach |
| `Tex Snab Import` vs `TexSnab` | 80–89 → new company with `dup_of`, card line «🔀 Birlashtir» |
| `Optom Savdo` vs `Optom Trade` | generic-token guard → no fuzzy at all, two companies |
| `Karimov R R` vs `Karimova R` | person-shaped guard → never merged below 100 |
| STIR match vs conflicting phone match | STIR wins; `Resolution.conflict=('phone', <other uuid>)` logged, nothing merged |
| landline tail, cities differ | no phone match |
| same tail on 3 unrelated companies | `shared_phone=true` on all rows; a 4th observation with that tail does not resolve by phone |
| two concurrent `resolve_company` of one new name | exactly one row (`pg_advisory_xact_lock`) |
| `merge_into` with different STIRs | `MergeRefused`; same STIR → moved ids in `merge_log.moved`, `unmerge` within 24 h restores, after 24 h refused |

### 1.5 Suppression tests (`tests/test_suppression.py`, `tests/test_erp_feeds.py`)

- **HMAC vectors** (pinned here and in fullprompt `tests/unit/agent-feed.test.ts` so both sides agree on bytes): `hash_tail("k", "901234567") == "2ea6869e504cebca50f0"`, `salt_id("k") == "8254c329"`, `hash_tail("test-secret", "900000001") == "d9d4ae601e164f812f1d"`, `salt_id("test-secret") == "9caf06bb"`. A fixture cursor `base64url("2026-10-23T09:00:00.000Z|0f3c1a2e-5b6d-4e7f-8a9b-0c1d2e3f4a5b")` = `MjAyNi0xMC0yM1QwOTowMDowMC4wMDBafDBmM2MxYTJlLTViNmQtNGU3Zi04YTliLTBjMWQyZTNmNGE1Yg` must round-trip through the Python decoder.
- **Full vs delta**: delta upserts additively; a full pull stamps `last_seen_at` and deletes unseen `erp_feed` rows **only** when complete and ≥ 90 % of the previous count (`suppression_shrunk` otherwise, nothing deleted); `salt_id` change on a delta aborts and schedules a full; on a full, old-salt rows vanish by the unseen rule; `rule` mismatch → `feed_contract`, no rows applied; 404 → `push_404`; `manual_import` rows retire after a full pull whose hashes cover them.
- **DNC precedence**: a company with both a live `cooldown` and a `dnc` (or `legal`, `client`, `lost`, `bounced`, `manager_veto`, `b2c`, `competitor`) row → `is_suppressed` returns the **permanent** hit, the candidate becomes `suppressed`, not `hold`; when the cooldown expires the permanent hit still holds; `client` beats `lost` for `companies.state`. The lookup orders by an explicit `KIND_RANK` (permanent kinds before `cooldown`) — see Open question 2 for 02's `ORDER BY kind`.
- **Any key suppresses the company**: a `dnc` on `tg_username` suppresses a candidate whose lead contact has a clean phone.
- **Self-test**: after a full pull the hash of the last `landed_client` tail must be present, else `suppression_selftest_failed` (warning, pull still applied).
- **Gate freshness**: `suppression_fresh()` false (no full ≤ 26 h or no delta ≤ 3 h while the feed is configured) → every push refused `stale`; with the feed unconfigured and `manual_import` rows present, pushes proceed with the digest line «yoʻqotilgan lidlar roʻyxati yoʻq».

### 1.6 `crm_push` against the fake ERP (`tests/test_crm_push.py`)

All 05 §B.4 tests, verbatim names. The table that matters:

| Fake mode | Expected `push_log` | candidate | side effects |
|---|---|---|---|
| `ok` | `sent`, 200 | `pushed`, `external_id`, `pushed_at` | — |
| `404` | `failed`, `http_404`, one attempt | stays `approved` | `erp_sync.breaker_until = +6 h`, problem `push_404`, **one** owner alert per 6 h (second 404 within the window → no second alert) |
| `500` ×3 | `failed`, `http_5xx` | stays `approved`; after attempt 3 → `review`, note «CRM javob bermadi» | `push_failing` after 3 consecutive candidates |
| `timeout` (landed, then `ReadTimeout`) | attempt 1 `failed/timeout`, attempt 2 `sent` | `pushed` | fake ERP holds **one** intake (replay fence) |
| `badjson` | `failed`, `http_2xx_badbody` → treated as `unknown` | blocked by `ux_push_log_delivered` | nightly outcomes promote |
| crash (row `sending` > 10 min) | reconcile → `unknown` | — | `push_unconfirmed` after 48 h |
| other 4xx | `failed`, `http_4xx` | `review`, note «ERP rad etdi (HTTP n)» | owner alert |
| fallback source `other` | endpoint `/api/leads/in/other`, both headers, `utm_source` present | as `ok` | fake rejects `X-ERP-Key` alone (mirrors fullprompt `x-gsr-key`) |

Plus payload tests: section order, line 1 ≤ 160 and whole-word cut, total ≤ 700 with the reverse-priority drops, URL never cut, `kub` string rules (`"2"`, band lower bounds, omitted < 1), no person name without `legal_basis`, no `<PHONE_|<TG_|<NAME_` token, `exclude_none`, `canonical_sha256` stable across key order, `external_id` month in Asia/Tashkent at claim time (a claim at 23:30 UTC on the 31st is next month).

### 1.7 Feed contract tests against recorded ERP responses (`tests/test_erp_feeds_contract.py`)

Fixtures in `tests/fixtures/erp/` are **captured from the `agent-feed` branch** running on a seeded `gsr_ci` (05 open question 8 → recommended: captured, not hand-written), with `tests/fixtures/erp/README.md` recording the fullprompt commit sha and date. Tests: each file validates against pydantic `SuppressionPage`/`OutcomesPage` (`h ~ ^[0-9a-f]{20}$`, `kind ∈ {client,lost}`, ISO `at`, `salt_id` 8 hex, `rule` equals the constant, `next` null or decodable); `parse_outcome` accepts all 14 fields and ignores unknown ones; a row carrying `phone`/`name`/`note` raises `FeedShapeError` (contract breach test uses a mutated copy); `derive_label` over every fixture row yields a label in the enum; the cursor of the last page decodes to the last row's `(at, intake_id)`. When fullprompt changes the feed, the capture is redone and this test is the diff.

### 1.8 Integration tests with Postgres

`postgres:16` service in CI (§2.1); locally `make db-test` starts a throwaway container on `127.0.0.1:${DB_PORT:-5433}` with `cs_test`. Integration tests are the ones 02–05 already name: `test_migrations.py` (upgrade → `compare_metadata == []` → downgrade base leaves zero tables → upgrade), `test_resolver.py`, `test_shared_phone.py`, `test_enrich.py`, `test_push_gate.py`, `test_crm_push.py`, `test_erp_feeds.py`, `test_outcomes.py`, `test_reviews.py`, `test_candidates.py` (every transition of 02 §2.9; every illegal one raises `IllegalTransition`; DB CHECKs refuse the inconsistent rows directly via `sqlalchemy.exc.IntegrityError`), `test_retention.py` (each step R1–R7 on seeded rows, the shared-sha file rule with a `tmp_path` evidence dir), `test_purge.py`, `test_model_jobs.py`, `test_health.py`, `test_backup_restore.py` (MIYA port: `pg_dump | age` non-zero file, restore drill on the scratch database). Two-worker tests (`push_gate`, resolver lock) open two sessions and use `asyncio.gather`; the `FOR UPDATE` waiter is observed through `pg_stat_activity.wait_event_type = 'Lock'`, not `pg_locks` (fullprompt #873).

### 1.9 Budget controller tests (`tests/test_costs.py`)

04 AC 11, pinned numerically: `sum(ALLOCATIONS) == Decimal("10.00")`, `sum(FAMILY_CAPS) == Decimal("10.00")`; refusal order monthly → daily → family → op with one fixture per rung; **boundary**: day spend 9.98 + est 0.01 → allowed, 9.99 + 0.01 → refused (`≥`); unpriced `est_usd=None` → `unpriced`; a Claude op borrows `model_reserve`, a SERP op borrows `serp_reserve` only with `sources.config.serp_paid_overflow=true`; timeout after dispatch → row with `cost_usd=est_usd, units=0`; refusal before dispatch → no row, `refusals[op]` incremented, `refused_until[op]` set; batch provisional rows updated in place (count unchanged); `cost_80`/`cost_cap`/`cost_month_80` fire once per Tashkent day through `reminder_log`; unknown model id in `EXTRACT_MODEL` → every `classify` refused and `make doctor` red.

### 1.10 Bot callback parser fence (`tests/test_bot_callbacks.py`)

Derived fence (fullprompt #937: an unparsed `callback_data` is answered by nobody): import every keyboard builder in `bot/keyboards.py`, build each with synthetic ids (`review_id=1…3`, `source_key` from the seed list, `chat_id=-1001`, `candidate_id` uuid), collect every `callback_data`, and assert `bot/callbacks.parse(data)` returns a typed `Callback` (not `None`) and `len(data.encode()) ≤ 64`. The reverse: `parse` refuses garbage, a wrong prefix, a non-integer id, an unknown answer. Every handler registered on the dispatcher is reachable from at least one parsed kind (no dead handlers, no handler-less kinds).

### 1.11 Uzbek text lint (`tests/test_uz_text.py`, new)

Owner-facing strings live in `bot/texts.py` (`UZ` dict), `services/cards.py`, `services/crm_push.py` (note labels), `services/digest.py`, `services/health.py` (alert texts). The test collects string literals from those modules by AST and the rendered outputs of `render_note`, `render_card`, `render_digest` on fixture data, then asserts:

| Rule | Regex (Python `re`, `u`) | Fix |
|---|---|---|
| `oʻ`/`gʻ` use U+02BB | `[oOgG][\'’‘\x60´]` → fail | `oʻ`, `gʻ` |
| glottal stop uses U+02BC | `(?<=[A-Za-z])[\'’‘\x60´](?=[A-Za-z])` → fail | `maʼlumot`, `taʼminot`, `Masʼul` |
| no Cyrillic in owner texts | `[Ѐ-ӿ]` → fail unless the literal is in `RU_ALLOWED` (evidence quotes are data, not texts) | — |
| no unfilled placeholders | `\{[a-z_]+\}` in rendered output → fail | — |
| no pseudonym tokens | `<(PHONE|TG|IG|NAME|EMAIL)_\d+>` → fail | restore before render |
| Telegram limits | message ≤ 4,096 chars, button label ≤ 40 | split / shorten |
| every text key used is defined | `texts.UZ[key]` for every `t("key")` call | — |

### 1.12 Live smoke tests (`tests/live/`, `make smoke`)

One real request per collector, `@pytest.mark.live`, run only with `LIVE=1` from the VPS (weekly cron Sunday 05:00 and quarterly full). Each asserts the fixture-era selector still yields ≥ 1 row and that no `CAPTCHA_MARKERS` body came back. Results are posted to the owner digest as «Tekshiruv: ✓ N / ✗ M / oʻtkazildi K» — a **skipped** live test is reported as skipped, never counted green. Live tests never write anywhere but `collector_runs(status='skipped', note='smoke')`.

## 2. CI and pre-push

### 2.1 `.github/workflows/ci.yml`

```yaml
name: CI
on:
  push: { branches: ["**"] }
  pull_request:
  workflow_dispatch:
permissions: { contents: read }
concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: true }

jobs:
  lint:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip, cache-dependency-path: requirements-dev.txt }
      - run: pip install -r requirements-dev.txt
      - run: python scripts/secret_scan.py          # sk-ant-, bot tokens, Telethon sessions, +998 tails outside tests/fixtures
      - run: ruff check .
      - run: ruff format --check .
      - run: mypy                                   # strict scope from pyproject (§2.3)

  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 25
    services:
      postgres:
        image: postgres:16
        env: { POSTGRES_USER: cs, POSTGRES_PASSWORD: cs, POSTGRES_DB: cs }
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U cs -d cs" --health-interval 5s --health-timeout 5s --health-retries 20
    env:
      DATABASE_URL: postgresql+psycopg://cs:cs@localhost:5432/cs
      CS_MIGRATION_DATABASE_URL: postgresql+psycopg://cs:cs@localhost:5432/cs_rt
      TZ: Asia/Tashkent
      CS_REQUIRE_DB: "1"
      EVIDENCE_DIR: ${{ runner.temp }}/evidence
      CS_FORBIDDEN_STRINGS: ${{ secrets.CS_FORBIDDEN_STRINGS }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip, cache-dependency-path: requirements-dev.txt }
      - run: sudo apt-get update -q && sudo apt-get install -y -q age postgresql-client-16
      - run: pip install -r requirements-dev.txt
      - name: Migration round trip on a second database
        run: |
          PGPASSWORD=cs psql -h localhost -U cs -d cs -c 'CREATE DATABASE cs_rt'
          DATABASE_URL=$CS_MIGRATION_DATABASE_URL alembic upgrade head
          DATABASE_URL=$CS_MIGRATION_DATABASE_URL alembic downgrade base
          DATABASE_URL=$CS_MIGRATION_DATABASE_URL alembic upgrade head
      - run: alembic upgrade head
      - run: alembic check                          # ORM metadata == migrations, no drift
      - run: pytest -q -p no:cacheprovider          # live tests skip without LIVE=1 and are reported as skipped

  time-travel:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    env: { DATABASE_URL: "postgresql+psycopg://none:none@127.0.0.1:1/none", TZ: Asia/Tashkent }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip, cache-dependency-path: requirements-dev.txt }
      - run: pip install -r requirements-dev.txt
      - run: CS_TIME_TRAVEL=newyear pytest -q -p no:cacheprovider
      - run: CS_TIME_TRAVEL=+400d  pytest -q -p no:cacheprovider

  docker:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - run: docker build -t clientscrapper:ci .
      - run: docker run --rm clientscrapper:ci python -c "import clientscrapper.worker.main, clientscrapper.bot.main, clientscrapper.userbot.main"
      - name: Compose parses .env.example without comment-shaped values
        run: |
          cp .env.example .env
          docker compose config --format json > compose.json
          python3 scripts/check_compose_env.py compose.json
```

Rules: the `test` job is the only one that touches a database; `alembic check` runs after `upgrade head` on the same database so drift between `Base.metadata` and the migration is red before `pytest`; Postgres is `postgres:16` (no pgvector in this project); `CS_REQUIRE_DB=1` turns every DB skip into a failure; secrets are read only from repository secrets, never from files. A branch protection rule requires all four jobs.

### 2.2 Pre-push — `scripts/pre-push.sh` (installed by `make hooks` into `.git/hooks/pre-push`)

```bash
#!/usr/bin/env bash
# Each gate prints its own marker and the chain stops at the first red one (fullprompt #738/#803:
# `cmd | tail` reports tail's exit code, so nothing here is piped).
set -euo pipefail
echo "[1/7] secrets";     python scripts/secret_scan.py
echo "[2/7] ruff";        ruff check . && ruff format --check .
echo "[3/7] mypy";        mypy
echo "[4/7] guards";      pytest -q -p no:cacheprovider tests/test_no_telegram_writes.py tests/test_collector_imports.py \
                           tests/test_push_requires_approved.py tests/test_paid_calls_via_costs.py tests/test_env_example.py \
                           tests/test_uz_text.py tests/test_bot_callbacks.py tests/test_forbidden_strings.py
echo "[5/7] suite";       CS_REQUIRE_DB="${CS_REQUIRE_DB:-0}" pytest -q -p no:cacheprovider -x
echo "[6/7] migrations";  { [ "${CS_REQUIRE_DB:-0}" = "1" ] && alembic check; } || echo "  (no database: alembic check skipped — CI runs it)"
echo "[7/7] trailers";    git log -1 --format=%B | grep -q '^Co-Authored-By:' || { echo "missing Co-Authored-By trailer"; exit 1; }
echo "pre-push: green"
```

`scripts/secret_scan.py` (new, no third-party dependency): regexes `sk-ant-[A-Za-z0-9_-]{20,}`, `\b\d{8,10}:[A-Za-z0-9_-]{35}\b` (bot token), `\b1[A-Za-z0-9+/=_-]{300,}` (Telethon StringSession), `AIza[0-9A-Za-z_-]{35}` (Google key), `apify_api_[A-Za-z0-9]{20,}`, `BSA[A-Za-z0-9_-]{20,}` (Brave), `\+998\s?\d{2}\s?\d{3}\s?\d{2}\s?\d{2}` outside `tests/fixtures/` and outside the synthetic `+998 90 000 00 NN` range, `age1[a-z0-9]{58}` outside `.env.example` comments; exits 1 naming file:line. Also used by CI `lint`.

### 2.3 Tool configuration (pyproject)

- **ruff**: MIYA's set (`E F I UP B C4 SIM RUF`, line 90, `RUF001-003` ignored for the multilingual literals), `extend-exclude = ["alembic/versions", "tests/fixtures"]`.
- **mypy strict scope** (`files = [...]`, `strict = true`, `follow_imports = "silent"`, `warn_unreachable = true`): `clientscrapper/config.py`, `logs.py`, `services/{keys,normalise,text,safety,retry,http,serp,costs,usage,pseudonyms,validators,redlines,signals,scoring,gates,suppression,push_gate,crm_push,erp_feeds,outcomes,candidates,evidence}.py`, `collectors/base.py`, `collectors/keywords.py`, `collectors/runner.py`, `bot/callbacks.py`. Every new pure module joins the list in the task that adds it; bot handlers, Telethon code and Alembic stay outside (imports analysed, own errors not reported).
- **pytest**: `asyncio_mode = "auto"`, `filterwarnings = ["error::DeprecationWarning:clientscrapper.*"]`, markers `live`, `slow`.

### 2.4 Repository rules the CI cannot see

Branch per task group (`t01-skeleton`, `t09-erp-sync`, …) merged to `main` by PR with a green CI; commits in English, trailers `Co-Authored-By` and `Claude-Session`; no model identifier in any commit, PR, comment or doc; secrets only in the server `.env`; fullprompt work follows **its** `CLAUDE.md` (05 §A.11) in a separate session and branch `agent-feed`.

## 3. Definition of done

### 3.1 Per component

| Component | Done when |
|---|---|
| Skeleton (T-01) | `make up` brings `db`, `init`, `worker`, `bot`, `userbot` to healthy on a clean VPS and on a laptop; `make doctor` green with blank optional credentials; `test_env_example` green; CI green |
| Schema (T-02) | `alembic upgrade head` on empty DB, `alembic check` empty, downgrade/upgrade round trip; the 19 `sources` and 26 `scoring_weights` rows present; `test_enums_frozen`, `test_fk_indexes`, `test_pii_columns` green |
| HTTP/evidence/runner (T-05) | `test_http.py` all cases; a fake collector run writes `collector_runs`, `evidence`, `observations`, a heartbeat; a planted 403 streak pauses the source and raises `collector_blocked` |
| Each collector | fixture parse tests with concrete fields; negative fixture; `@live` smoke; day-1 README line for every UNVERIFIED item of 03; `collector_runs` counters; `sources.enabled=true` only after the smoke passed on the VPS |
| erp_sync | 05 agent-side acceptance for feeds; `suppression_fresh()` gates pushes; XLSX fallback hashes equal the feed's |
| Telegram reader | `test_no_telegram_writes` green; 3 seed chats joined; synthetic prefilter tests; a real intent message reaches `interactions` within 1 minute on the VPS |
| Resolver/scoring/gates | 04 AC 1–3, 7–10 green; the three worked examples reproduce |
| Claude lane | 04 AC 5–6 green; `model_jobs.request` contains no raw PII (grep test); spend per op under its allocation in `usage_log` |
| Owner bot | every command has a handler test; callback fence green; `test_uz_text` green; a card seen on a phone at 360 px width without horizontal truncation of line 1 |
| crm_push | 05 agent-side acceptance for push; `make push-test` lands a visibly synthetic lead; a 404 alerts once |
| Ops | digest at 09:00 for 3 consecutive days; dead-man ping; `make backup` non-zero `.age` file nightly; restore drill on the scratch DB documented |
| ERP branch (T-07) | 05 fullprompt acceptance: typecheck/lint/test green on a fresh `gsr_ci`; no migration; feeds answer 404/400/200 as documented; belt, clock and reminder tests; 4 locales; PR with Uzbek summary |

### 3.2 MVP (end of week 2 of the pilot)

| Metric | Target | Measured by (`tools/mvp_report.py`, numbers also in `/holat`) |
|---|---|---|
| Pushed leads with phone and score ≥ 60 | **≥ 20** in week 2 | `SELECT count(*) FROM push_log p JOIN lead_candidates c ON c.id = p.candidate_id WHERE p.status IN ('sent','unknown') AND c.score >= 60 AND p.sent_at >= :w2_start AND p.sent_at < :w2_end` |
| Real importer rate | **≥ 60 %** | over pushes whose outcome is terminal or ≥ 14 d old: `1 − count(label IN ('lost_not_importer','lost_wrong_number','lost_competitor','lost_small')) / count(label NOT LIKE 'dropped_%')`. This is the owner's metric (01 §5); `sources.precision_30d` (positives/(positives+negatives)) is the stricter auto-push metric and is reported beside it |
| Contacted within 1 business day | **≥ 40 %** | `outcomes.contacted_at <= business_day_end(push_log.sent_at + 1 business day)` with Mon–Sat Tashkent calendar (`platform/time`-style helper `next_business_day_end`); denominator = pushes with `landing='created'` |
| Claude spend | **≤ $5/day** every day | `SELECT (created_at AT TIME ZONE 'Asia/Tashkent')::date d, sum(cost_usd) FROM usage_log WHERE provider='anthropic' GROUP BY d` — max ≤ 5.00; total ≤ 10.00 |
| Operational | no `push_404`, `suppression_stale` or `cost_cap` problem open for > 6 h during the pilot; `test_no_telegram_writes` green on every commit; zero complaints routed through «yozmang» that were not already in suppression |

A metric that depends on managers (contact rate) failing while the agent's own metrics pass is escalated to the owner in the digest as «menejer javobi», not fixed in code.

## 4. Implementation plan

### 4.0 Epics

E1 Skeleton & guards (T-01–T-06, T-08) · E2 ERP branch (T-07) · E3 ERP sync (T-09–T-12) · E4 Telegram (T-13–T-16) · E5 Core processing & bot (T-17–T-21) · E6 Collectors wave 1 (T-22–T-27) · E7 Push & pilot (T-28–T-31) · E8 Collectors wave 2 & ops (T-32–T-36) · E9 Month 1–2 (T-37–T-40).

Hours are focused build-and-test effort per task (not calendar days); the "Day" labels are the owner's phase plan. The sum for Days 1–10 is ≈ 205 h, so the ten phases are three to four weeks of one model stream; T-07, T-22/T-23 and T-32–T-34 are independent of the bot and can run in a parallel session.

### 4.1 Tasks

| ID | Task — goal | Files / modules | Deps | Acceptance (testable) | h |
|---|---|---|---|---|---|
| **Day 1–2 — skeleton + ERP branch** | | | | | |
| T-01 | Repo skeleton: tooling, compose, config, logs, db plumbing, Makefile targets of `CLAUDE.md` | `pyproject.toml`, `requirements*.txt`, `Dockerfile`, `docker-compose.yml` (db, init, worker, userbot, bot, searxng profile), `Makefile`, `.env.example`, `config.py`, `logs.py`, `db/base.py`, `db/session.py`, `tools/doctor.py` (skeleton), `VENDORED_FROM` headers | — | `make up` healthy; `make doctor` runs; `test_env_example`, `test_dockerfile`, `test_vendored_headers` green; CI `lint`/`docker` green | 4 |
| T-02 | Schema: enums, models, `0001_initial` with functions, triggers, seeds | `db/enums.py`, `db/models.py`, `alembic/env.py`, `alembic/versions/0001_initial.py` | T-01 | `test_migrations`, `test_enums_frozen`, `test_fk_indexes`, `test_pii_columns`, `test_weights_seed` green; `alembic check` empty | 7 |
| T-03 | Keys, normaliser, text, keyword regexes | `services/text.py`, `services/keys.py`, `services/normalise.py`, `collectors/keywords.py` | T-02 | `test_keys` (Python == SQL mirrors), `test_normalise` (≥ 40 phones, ≥ 20 names), `collectors/test_keywords` (≥ 3 pos/neg per regex per script, «оптом» vs «оптика», «kargo») | 5 |
| T-04 | Guard tests, secret scan, pre-push, CI workflow | `tests/test_no_telegram_writes.py`, `test_collector_imports.py`, `test_paid_calls_via_costs.py`, `test_push_requires_approved.py` (AST half), `test_uz_text.py`, `test_bot_callbacks.py` (skeleton), `test_forbidden_strings.py`, `scripts/secret_scan.py`, `scripts/pre-push.sh`, `scripts/check_compose_env.py`, `.github/workflows/ci.yml` | T-01 | all four CI jobs green on the skeleton; pre-push refuses a planted `sk-ant-x…` string and a missing trailer | 4 |
| T-05 | HTTP layer, evidence store, collector contract, runner, fixture recorder | `services/http.py`, `services/evidence.py`, `collectors/base.py`, `collectors/runner.py`, `tools/record_fixture.py`, `tools/scrub_fixture.py` | T-02 | `collectors/test_http.py` (pacing, robots, 304, Retry-After, breaker at 5, budget, UA, log scrubbing, robots opt-out allow-list); runner on a fake collector writes the three tables + heartbeat; recorder writes file + README line; scrubber keeps structure | 8 |
| T-06 | Budget controller and pricing | `services/usage.py`, `services/costs.py` | T-02 | `test_costs` (§1.9) green; `make doctor` prints priced model ids and the allocations table | 5 |
| T-07 | **fullprompt branch `agent-feed`** (05 Part A) in a separate session under fullprompt's `CLAUDE.md` | 12 files of 05 §A.1, `docs/AGENT-FEED.md`, tests of 05 §A.10, PR | — | 05 fullprompt acceptance; feed fixtures captured into `tests/fixtures/erp/` with sha + date; owner steps (05 §A.12) written in Uzbek | 10 |
| T-08 | Health, heartbeats, scheduler skeleton, backup/restore, doctor checks | `services/health.py`, `worker/main.py` (scheduler, `heartbeat_job`, `health_job`, dead-man), `services/backup.py`, `tools/restore.py`, `services/restore_drill.py`, `tools/doctor.py` (config, db, telegram, crm reachability, prices, salt_id) | T-02, T-06 | `test_health` (problem list of 06 incl. `collector_blocked`, `push_404`, `cost_cap`, `userbot_unauthorized`, `flood_breaker`, `no_signals_24h`, `suppression_stale`, `collector_silent`, `batch_stuck`, `push_unconfirmed`, `push_failing`, `suppression_shrunk`, `feed_contract`, `pii_leak`, `ig_token_expired`); `make backup` → non-zero `.age`; `test_backup_restore` on scratch DB | 4 |
| **Day 3 — erp_sync** | | | | | |
| T-09 | Suppression feed sync (full/delta), freshness gate | `services/erp_feeds.py` (ErpClient, hash_tail, salt_id, parse_entry, fetch_pages, sync_suppression), `collectors/erp_sync.py`, `sources.config` cursors | T-05, T-06, T-12 | §1.5 feed tests; `suppression_fresh()`; problems `suppression_stale/shrunk`, `feed_rule_changed`, `feed_contract`, `push_404` | 6 |
| T-10 | Outcomes sync, labels, side effects, precision numbers | `services/outcomes.py` (`derive_label`, `LOST_REASON_PATTERNS`, side effects), `erp_feeds.sync_outcomes`, `source_precision` | T-09 | `test_outcomes_labels_table` parametrised over 05's table; bounced ×2, cooldowns, `landed_client`; `unknown → sent` promotion; cursor persistence | 5 |
| T-11 | XLSX client import (suppression fallback) | `services/client_import.py`, `tools/import_clients.py`, `make import-clients` | T-09 | synthetic `clients.xlsx` with multi-phone cells → `suppression(manual_import)`; same tail hashes equal the feed's; retired after a covering full pull | 3 |
| T-12 | Fake ERP and contract fixtures | `tests/fake_erp.py`, `tests/test_erp_feeds_contract.py`, `tests/fixtures/erp/*` + README | T-07 (capture) | §1.7 green; fake rejects `X-ERP-Key` alone; replay returns `{ok:true}` without a second intake | 4 |
| **Day 4–5 — Telegram** | | | | | |
| T-13 | Read-only userbot: connect, kill switch, prefilter, live handler, joiner, backfill, login tool, chat monitors | `userbot/main.py`, `userbot/prefilter.py`, `userbot/joiner.py`, `userbot/about.py`, `userbot/backfill.py`, `tools/userbot_login.py`, `services/chats.py` | T-02, T-03 | `test_no_telegram_writes` green (raw requests only in the allow-list); kill switch idles; unauthorized → beat + idle; 20 synthetic messages (Latin, Cyrillic Uzbek, Russian) classified by the prefilter; join cap 3/day with fake clock; non-matches only increment `seen_count`; edits re-run the prefilter ≤ 10 | 8 |
| T-14 | `telegram_monitor` collector (no network): tout filter, observations, channel about, repeat posters | `collectors/telegram_monitor.py` | T-13, T-05 | parse tests on synthetic `interactions`; tout rules (COMPETITOR_RE on sender/bio, ≥ 3 price lists/7 d, forwarded channel post); `test_collector_imports` green | 5 |
| T-15 | Pseudonymisation, Claude extraction, `ImportSignal` classify, validators | `services/pseudonyms.py`, `services/extraction.py`, `services/schemas.py`, `services/validators.py`, `classify_job` | T-06, T-14 | `test_pseudonyms` (round trip, idempotence, numbering), `test_pseudonymised_requests` (fake transport sees no raw PII), `test_extraction` (structured, grammar fallback, nudge, refusal, max_tokens), `test_validators` (quote substring, volume sanity, tout downgrade, output hygiene); every call under `costs.ask('classify')` | 7 |
| T-16 | `model_jobs` lifecycle and the batch lane | `services/model_jobs.py`, `model_batch_submit`, `model_batch_collect` | T-15 | `test_model_jobs`: pending → submitted → applied/failed/fallback; provisional usage row updated in place; keyed by `custom_id`; `batch_stuck` after 26 h; 3 attempts then instant fallback | 5 |
| **Day 6 — resolver, scoring, gates, bot** | | | | | |
| T-17 | Entity resolution and merge | `services/companies.py` (`resolve_company`, fuzzy bands, `_carry_identity`, shared phones, `upsert_contact`, `merge_into`, `unmerge`, `company_lineage`), `resolve_job` | T-03, T-05 | `test_resolver`, `test_shared_phone` (§1.4); advisory-lock concurrency test | 8 |
| T-18 | Signals from rules, scoring, candidate state machine | `services/signals.py`, `services/scoring.py`, `schemas/score_breakdown.json`, `services/candidates.py` (`TRANSITIONS`), `score_job` | T-17 | `test_scoring` (§1.3), `test_signals` (≥ 1 m³ rules 1–6, vacancy duties not volume), `test_candidates` (every transition; illegal raise) | 6 |
| T-19 | Gates, suppression lookup and keys, pre-push checklist | `services/gates.py`, `services/suppression.py` (`build_keys`, `is_suppressed`, `KIND_RANK`, DNC events), `services/push_gate.py` | T-18, T-09 | `test_gates` (one failing fixture per gate; «kim kargo qiladi» passes; state word in name vs text), `test_suppression` (§1.5), `test_push_gate` (14 checks refuse individually; two workers → one `push_log` claim) | 5 |
| T-20 | Owner bot: texts, keyboards, callback parser, commands, card renderer | `bot/main.py`, `bot/texts.py`, `bot/keyboards.py`, `bot/callbacks.py`, `bot/handlers/*.py` (`/lidlar /manbalar /chats /holat /xarajat /toxtat /yoq /import /unut /qidir /dnc /vazn /push /stir`), `services/cards.py` | T-18, T-19 | `test_bot_callbacks` (§1.10), `test_bot_commands` (one test per command, owner-only guard: unset `OWNER_TELEGRAM_ID` locks everyone out), `test_uz_text` (§1.11), `test_cards` (gate lines, breakdown, quote, Telegram-only card, ⚠️ shared-phone line) | 8 |
| T-21 | Review flow: state machine, card job, expiry | `services/reviews.py`, `review_cards_job`, expiry sweep | T-20 | `test_reviews`: each answer → the 02 §2.9 transition; `expired` → `hold` + 7 d, re-asked ≤ 2; `card_hash` edit-only-on-change; `later` +24 h | 4 |
| **Day 7 — directories + hh** | | | | | |
| T-22 | `directories` collector with sub-source parsers | `collectors/directories/{__init__,goldenpages,yellowpages,topuz,glotr,prom,uzts,openinfo,uzpharm}.py`, fixtures | T-05 | parse tests per sub-source with concrete fields; paginator stop on repeated first card; `BULKY_CATEGORIES` filter; live smoke; goldenpages phone rendering recorded in README | 8 |
| T-23 | `hh_vacancies` collector | `collectors/hh_vacancies.py`, fixtures `vacancies.json vacancy.json employer.json areas.json` | T-05 | parse tests; area resolved from `/areas` (never hard-coded; a request without `area` is refused by a fence test); agency regex cases; HR contact → `attrs.hr_contact` only; 403 → breaker + alert | 5 |
| T-24 | Enrichment chain E0–E6 (E7 in T-40) | `services/enrich.py`, `services/registry.py` (orginfo first, then stat/soliq/myorg), `parse_company_site` (shared with T-25), link hubs, `enrich_job` | T-17, T-18, T-06 | `test_enrich` with fakes: stop S1–S4, $0.10/company cap, 1 h/24 h/7 d schedule, `hold` after attempt 3, no paid step outside `costs.ask`, `enrichment[]` entry per step | 8 |
| **Day 8 — SERP, Instagram, OLX** | | | | | |
| T-25 | SERP client and `serp_layer` | `services/serp.py` (Brave, Yandex night async + poll, SearXNG, DDG; routing; `classify_hit`), `collectors/serp_templates.py`, `collectors/serp_layer.py`, fixtures `brave.json yandex.xml searxng.json ddg.html` | T-05, T-06 | parse tests per engine; `classify_hit` table (≥ 20 URLs incl. reserved paths); night-window refusal; one engine per query per week; `company_site` fetch ≤ 120/day; cost ops recorded | 8 |
| T-26 | `instagram_discovery` | `collectors/instagram_discovery.py` (Business Discovery, hashtag search, Apify fallback), bio parsing | T-25, T-24 | parse tests; `not_business` never stored; 30 tags / 7 days tracker; token expiry → `ig_token_expired`; scam pattern excluded | 6 |
| T-27 | `olx_business` | `collectors/olx_business.py` (JSON route, `__PRERENDERED_STATE__` route, seller aggregation, Apify fallback) | T-05, T-15 | parse tests on three fixtures; `test_no_phone_reveal`; competitor seller → `competitor` only; velocity ≥ 10 → `marketplace_velocity`; one `classify` per seller | 6 |
| **Day 9 — crm_push + pilot** | | | | | |
| T-28 | `crm_push`: payload, Uzbek note, claim/commit/post, breaker, reconcile, push test tool | `services/crm_push.py`, `crm_push_job`, `reconcile_job`, `tools/push_test.py`, `make push-test` | T-19, T-12 | §1.6 tests; `test_push_requires_approved` behavioural half; both headers; `exclude_none`; `make push-test` prints the status | 8 |
| T-29 | Pilot wiring with the owner and one manager | ERP UI steps (05 §A.12), `.env` (`CRM_PUSH_SOURCE`, `CRM_AGENT_KEY`, `PUSH_DAILY_CAP=10`), `docs/sotuv-playbook.md` briefing, digest recipients | T-28, T-07 (or fallback) | §5.6 end-to-end passed; first 5 real approved pushes `sent`; `/holat` shows them; `kub` mapped; routing rules A and B present | 4 |
| T-30 | Verdict, draft opener, redlines | `RelevanceVerdict`, `verdict_job`, `apply_verdict`, `DraftOpener`, `services/redlines.py`, playbook templates A–E in `fixtures/uz_corpus.py` | T-15, T-20 | `test_redlines` (corpus + templates); verdict quotes verified; `est_volume_band` adds no points; refusal → template; card shows Uzbek reasons | 6 |
| T-31 | Retention and purge | `retention_job` R1–R7, `services/purge.py`, `/unut` | T-18, T-20 | `test_retention` per step (file unlink only when no other row shares the sha); `test_purge` (counts, hashed log, append-only tables intact) | 4 |
| **Day 10 — exhibitors, maps, brand dealers, digest** | | | | | |
| T-32 | `exhibitors` | `collectors/exhibitors.py`, calendar in `sources.config`, fixtures Automechanika + UzBuild | T-05 | parse tests; country filter; `< 50 %` of organiser count → `partial` + alert; `PAGEN_2` stop | 4 |
| T-33 | `maps` (Places New two-step, 2GIS demo) | `collectors/maps.py` | T-05, T-06, T-24 | details at most once per `place_id`; Places evidence `retention='30d'`; only `place_id` in `external_ids`; caps `places_text_ids`/`places_details_contact`/`twogis_api` | 5 |
| T-34 | `brand_dealers` | `collectors/brands.py`, `collectors/brand_dealers.py` | T-25 | parse tests on 3 site fixtures; official regex within 200 chars; news regex; retailers → `hold`; ≥ 10 `official=True` pages in the first live run | 4 |
| T-35 | Digest, alerts, dead-man, smoke cron | `services/digest.py`, `health_job` alert routing, `DEADMAN_PING_URL`, `make smoke`, VPS cron | T-08, T-20 | `test_digest` renders ≤ 4,096 chars from fixtures and lists skipped live tests; every problem key has a trigger test; dead-man ping observed | 5 |
| T-36 | Source precision job, `/holat` table, MVP report, ops docs | `source_precision_job` (weekly flip both ways), `tools/mvp_report.py`, `docs/OPS.md`, README | T-10, T-20 | `test_precision_and_autopush_flip`; `mvp_report` prints the four §3.2 numbers from fixture data; `CLAUDE.md` command table matches the Makefile (test) | 4 |
| **Month 1** | | | | | |
| T-37 | `uzum_sellers` (phase 2; gated: phase-1 real-importer rate ≥ 50 % over ≥ 30 pushes and owner go) | `collectors/uzum_sellers.py`, proxy use, Apify actor | T-24, T-06 | 20–30 shop pages hand-checked for the legal block first (README); `proxy_uzum`/`apify_uzum` caps; velocity thresholds; `самозанят` excluded | 10 |
| T-38 | `tenders` enrichment | `collectors/tenders.py` | T-24 | parse test on supplier page; robots recorded; `entity_solid` rule | 4 |
| T-39 | Auto-push and the Sheets mirror | `approved_by='auto:<source>'` path, `services/sheets.py`, `services/sheets_sync.py`, builders, `sheet_rows` | T-36 | auto-approval only when `pushes_30d ≥ 15 ∧ precision_30d ≥ 0.60` (test both flips); mirror carries company-tier columns only (test) | 8 |
| T-40 | Weights v2, E7 `enrich_search`, manual `/stir` queue, restore drill | `tools/fit_weights.py`, `/vazn`, E7 (`ContactFind`, `web_search`), digest manual queue, monthly drill | T-10 (≥ 150 labelled outcomes), T-24 | version 2 proposed with ±30 % rule, approved via `/vazn`; E7 values stored only when verbatim in search results; drill report | 10 |

**Month 2 backlog** (unnumbered, owner-prioritised): STIR checksum adoption (`stir_probe`), Instagram hashtag app review, 2GIS licence or Apify monthly, regional seeds (Fergana, Samarkand), Telegram-only prospect cards to the on-duty manager (F-12 polish), per-source ERP caps, KPI dashboard page in the ERP, second manager pool, SearXNG tuning, evidence off-site copy (restic).

### 4.2 Order rationale

Suppression before any card (T-09–T-12 before T-20): the owner must never see a client or a «Qayta qilinmasin» number. Telegram before the web collectors: it is the fastest intent source and exercises the whole pipeline (interactions → classify → resolve → score → card) on day 5. `crm_push` on day 9, after the gates and the bot exist, because a push is irreversible for the manager who receives it. Collectors that need no bot (T-22, T-23, T-32–T-34) are parallelisable.

## 5. Day-1 checklist for the implementing model

### 5.1 Read, in this order (≈ 2 h)

1. `CLAUDE.md` (this repo) → `docs/spec/00-overview.md` → `01-requirements.md`.
2. This file §4 (the task you are on) and §1 (how it is tested).
3. `02-data-model.md` fully; then `04-processing.md`, `03-collectors.md`, `05-erp-integration.md`, `06-bot-ops-security.md`.
4. `docs/kod-audit-miya.md` §1 and §3 (what to copy, file by file); then open each MIYA file you copy and read its tests first.
5. Before T-07: `/home/user/bekzodqodirov/fullprompt/CLAUDE.md` (all of it), `docs/ADS.md`, `src/modules/wms/crm/inbound.ts`, `inbound-webhook.ts`, `inbound-notify.ts`, `first-contact.ts`, `tests/integration/inbound-webhook.integration.test.ts`.
6. Background only when a task needs it: `docs/manbalar.json` (per source), `docs/huquqiy-memo.md`, `docs/sotuv-playbook.md`.

### 5.2 Ask the owner (numbered, Uzbek, options with the recommendation marked)

1. ERP tartibi: a) avval `agent-feed` branch deploy qilinadi, keyin birinchi push — **tavsiya**; b) `other` eshigi bilan boshlaymiz, feed keyin.
2. Koʻrib chiqish stolida kim oʻtiradi va 1-haftada kunlik push chegarasi: a) siz, 10 ta/kun — **tavsiya**; b) rahbar, 25 ta/kun.
3. Telegram seed roʻyxati (10 ta) tasdiqlanadimi; katta bozor guruhlariga (165 ming aʼzo) qoʻshilamizmi: a) faqat raqobatchi muhokama guruhlari — **tavsiya**; b) bozor guruhlari ham.
4. Google Cloud va Brave kartasi ulanganmi; limit: a) Google oyiga $10 byudjet + API kvota, Brave $5 kredit — **tavsiya**; b) hozircha kartasiz.
5. Yoʻqotish sababi matni: «Qayta qilinmasin» — aynan shu yozuvmi; «Notoʻgʻri raqam» ham qoʻshilsinmi (a — ha, tavsiya; b — yoʻq).
6. Push oynasi: a) Dush–Shan 09:30–17:30 — **tavsiya**; b) 10:00–17:00.
7. Faqat shahar telefoni (landline) bor kompaniya push qilinsinmi: a) ha — **tavsiya**; b) faqat mobil.
8. Maxsus Telegram akkaunt: raqam, 2FA, yoshi ≥ 30 kun; login kodini kim kiritadi va qachon.

### 5.3 Verify live from the VPS (record every answer in `tests/fixtures/<key>/README.md` with the date)

| Check | How | Expected | If not |
|---|---|---|---|
| hh.uz API | `curl -sS -A "$HH_USER_AGENT" 'https://api.hh.ru/areas' -o areas.json -w '%{http_code}'`; then `/vacancies?text=ВЭД&area=<uz id>&per_page=1` | 200 + JSON | 403 → register an app at dev.hh.ru, set `HH_APP_TOKEN`, retry with `Authorization: Bearer`; still 403 → SEO-page fallback (03 §2.7) |
| OLX JSON key | `curl -sS https://www.olx.uz/robots.txt`; `curl -sS 'https://www.olx.uz/api/v1/offers/?offset=0&limit=1&query=оптом'`; one search page → grep `__PRERENDERED_STATE__` | robots allows `/api/`; `data[].business` present; state key present | `/api/` disallowed → HTML route only; both blocked → Apify only; key renamed → update parser + fixture |
| 2GIS | `curl -sS https://2gis.uz/robots.txt`; demo key request `catalog.api.2gis.com/3.0/items?q=оптом&region_id=…&key=…` | robots read; API 200 | SPA/disallowed → never crawl HTML; API only, then Apify monthly |
| Instagram logged-out | `curl -sS -A "$USER_AGENT" https://www.instagram.com/optom_obuv_uzb/` → grep `biography` | unstable (expected) | rely on Graph Business Discovery; record whether bio is visible at all |
| Instagram Graph | one `business_discovery` call with `IG_GRAPH_TOKEN`; one `ig_hashtag_search` | 200; hashtag may need app review | hashtags off until review; Business Discovery for own-app testers |
| Yandex Search API night window | at 02:10–09:59 Tashkent: `POST searchAsync` + poll | operation `done`, XML in `rawData` | 400 on `SEARCH_TYPE_UZ` → stay RU; quota wording checked on the pricing page |
| Brave | one call with `country=UZ` | 200 | 422 → `country=ALL` + geo words |
| Google Places | one `searchText` with IDs-only mask, one details call; **next day** read the SKU report in Cloud Console | $0 for IDs; details SKU as expected | SKU surprise → lower `places_details_contact` cap before enabling |
| Registry hosts | one per-INN GET on orginfo.uz, registr.stat.uz, my.soliq.uz, myorg.uz | at least one answers without captcha | all captcha → E4 manual queue (`/stir`) |
| fullprompt routes | `curl -s -o /dev/null -w '%{http_code}' -H 'X-GSR-Key: wrong' -X POST $CRM_BASE_URL/api/leads/in/other` → 404; `GET $CRM_BASE_URL/api/agent/suppression` → 404 until `agent-feed` is deployed; confirm `grep -n "x-gsr-key" src/app/api/leads/in/[source]/route.ts` (line 63 at HEAD `24607e8`) | 404 / 404 | a 200 on GET before deploy means production differs from the checkout — stop and ask |
| Telegram reader | `make userbot-login`; join 3 seeds; `GetFullChannelRequest(@mk_express).linked_chat_id` | session string printed once; linked group resolved | FloodWait → wait; `UsernameNotOccupied` → fix the seed |

### 5.4 Create the ERP secret

CRM → ⋯ → **Kelgan arizalar → Reklama havolalari** → the door («Agent (qidiruv)» after `agent-feed` is deployed; «Boshqa» during the fallback) → **«Yoqish va kalit yaratish»**. Paste the value into the agent's server `.env` as `CRM_AGENT_KEY`, set `CRM_PUSH_SOURCE=agent` (or `other`), `make reload`. Never paste it in chat or in the repo. Re-minting kills the old key at once and changes `salt_id` — the next feed call forces a full pull (05 §B.2).

### 5.5 Fixtures to record before any collector is enabled

One list page + one card per directory sub-source; hh `areas/vacancies/vacancy/employer`; OLX API + search page + seller page; Brave/Yandex/SearXNG/DDG responses; Automechanika + UzBuild pages; 3 brand-distributor pages; Places IDs + details; 2GIS items; Graph business_discovery + hashtag; ERP feeds from the branch. Commit them with their README lines in the task that adds the parser.

### 5.6 First end-to-end test (one synthetic lead → ERP → outcome read back)

1. `make up && make doctor` green; `CRM_PUSH_ENABLED=true`, `PUSH_DAILY_CAP=10`, `TEST_PHONE=+998900000099` (a number the owner controls).
2. `make push-test` → prints `200`; the ERP's **Kelgan arizalar** shows a lead «TEST agent — oʻchiring», `external_id cs:00000000-0000-0000-0000-000000000000:<yyyymm>`. Map `kub → kub (m³)` under **Forma savollari → Kelgan savollar** now.
3. The owner (as the lead's manager) taps «📞 Bogʻlandim», then moves the lead to a lost stage with «Qayta qilinmasin».
4. `make job JOB=erp_sync.outcomes` → `outcomes` row for that `external_id` with `label='lost_no_contact'`, `contacted_at` set; `make job JOB=erp_sync.full` → `suppression` holds `hash_tail(CRM_AGENT_KEY, '900000099')`.
5. `make push-test` again → pre-flight refusal `suppressed` (no HTTP; `/lidlar` shows the note). Same month without step 3 it would be a `replay` on the ERP side — also fine.
6. `python -m clientscrapper.tools.purge --company 00000000-0000-0000-0000-000000000000 --yes`; leave the ERP lead as lost (it is the suppression proof).

During the `other` fallback, steps 4–5 are replaced by `make import-clients FILE=clients.xlsx` with the test phone added to the export, then the refusal in step 5; the outcomes half is re-run on the day `agent-feed` is deployed.

## 6. Risk register

| # | Risk | Trigger / indicator | Response |
|---|---|---|---|
| R1 | hh.uz API 403 for unregistered UAs | day-1 check; `collector_blocked:hh` | `HH_APP_TOKEN`; else SEO pages; vacancies are 15 points, never blocking |
| R2 | OLX changes `__PRERENDERED_STATE__` or blocks `/api/` | parse test red on refresh; 403 streak | HTML route ↔ JSON route swap; Apify ≤ $0.60/day; never the phone-reveal endpoint |
| R3 | Reader account limited or banned | `userbot_unauthorized`, `UserDeactivatedBan`, `FloodWait > 300 s` | process idles; owner alert; no second session ever; joins stay ≤ 3/day; account aged ≥ 30 d, 2FA |
| R4 | Instagram Graph app review pending / token expired | `ig_token_expired`; hashtag 400 | Business Discovery continues for own-app testers; monthly token refresh job; Apify fallback bounded |
| R5 | SERP quota exhausted (Brave credit, Yandex 1,000/month) | `cost_cap_op:serp_*`; Yandex silent skip | SearXNG/DDG overflow; `serp_paid_overflow` is an owner switch, default off |
| R6 | `agent-feed` not deployed on day 1 | GET feeds 404 | fallback (05 §B.3): `other` + `utm_source`, XLSX suppression weekly, cap ≤ 10/day, reminder noise told to the owner |
| R7 | ERP secret re-minted or wrong | `push_404` | breaker 6 h, one alert per 6 h, owner re-pastes; salt change → full pull |
| R8 | Suppression stale → **all pushes stop** (fail closed) | `suppression_stale` critical after 6 h | digest and alert say so in Uzbek; the fix is the ERP's availability or the key; never bypass the gate |
| R9 | Managers do not contact within a day | `contacted_within_1bd < 40 %` while pushes ≥ 20 | owner escalation in the digest («menejer javobi»); not a code fix; consider routing rule membership |
| R10 | Telegram yields mostly touts → low precision | `precision_30d < 0.2` over ≥ 30 pushes for `telegram_monitor` | `source_precision_job` pauses the source with an alert; tout filter tuned on the recorded false positives |
| R11 | Legal: advertising rules from 2026-11-01, pd.gov.uz registration | date; a prospect complaint | outreach is human, 10:00–17:00, opt-out line; registration is the owner's go-live task; `/unut` purge path tested |
| R12 | Cost overrun from an unpriced or mis-priced SKU | `unpriced` refusals; Places SKU report | `costs.ask` refuses unpriced units by design; read the Cloud Console SKU the day after the first call; card limits on Google/Brave |
| R13 | PII in logs, fixtures or model requests | `test_pseudonymised_requests`, `secret_scan`, `test_forbidden_strings` red; `pii_leak` counter | `RedactBodies` covers headers and `phone`; fixtures scrubbed; a leak is a stop-the-line bug |
| R14 | Database or evidence loss | backup job red; disk ≥ 85 % | nightly `.age` backup off-box, monthly restore drill; evidence has 2 MB/body cap and 180 d retention; losing evidence files loses nothing a card needs |
| R15 | Research sandbox ≠ VPS (no outbound HTTP here) | a fixture recorded outside the VPS | fixtures are recorded only from the VPS; UNVERIFIED items stay marked until the README line exists |
| R16 | Scope creep (the 40+ sources of the critique) | a new collector proposed before precision is measured | every new source is gated on 30-day precision of the existing ones and an owner decision in the digest |
| R17 | Anthropic outage or model id retired | `anthropic_silence`; unpriced model id | classify/verdict refusals are non-fatal (cards without AI reasons); price overrides via `*_MODEL_PRICE` env |
| R18 | Duplicate companies pushed under two ids | `dup_of` set; `lost_other` labelled «takror» | owner 🔀 merge; `company_lineage` blocks a second push for 90 d |
| R19 | Calendar-dependent tests (Tashkent day, windows, decay) go red on a date nobody chose | CI `time-travel` job red | explicit `now:` everywhere; fix the test's clock derivation, not the date |
| R20 | ERP changes the feed shape | `feed_contract` / contract test red after re-capture | re-capture fixtures from the branch; `parse_outcome` ignores unknown keys, refuses PII keys |

## 7. Glossary (uz / ru / en)

| uz | ru | en | Note |
|---|---|---|---|
| lid | лид | lead | an inbound enquiry card in the ERP (`leads`) |
| bitim | сделка | deal | a priced agreement on a lead; not touched by the agent |
| mijoz | клиент | client | in the ERP client book → always suppressed |
| nomzod | кандидат | candidate | `lead_candidates` row in the agent |
| manba | источник | source | collector / feed (`sources.key`) |
| dalil | доказательство | evidence | URL + date + verbatim quote (`evidence`) |
| ball | балл | score | 0–100, deterministic |
| kub (m³) | куб | cubic metre | the volume unit of the business; ≥ 1 m³ qualifies |
| zichlik | плотность | density | kg/m³, drives the tariff |
| optom / ulgurji | оптом / опт | wholesale | B2B marker; «ulgurji» is the Uzbek word, «optom» the loan word |
| partiya | партия | batch / consignment | one truck or one shipment |
| fura | фура | truck (FTL) | ≈ 80–90 m³ |
| konteyner (20/40 ft) | контейнер | container | ≈ 28–33 / 60–68 m³ |
| kargo | карго | freight forwarder | competitor; excluded as a lead, used as a seed |
| rastamojka / bojxona | растаможка / таможня | customs clearance / customs | — |
| VED (TIF) | ВЭД | foreign-trade activity | «ВЭД menejer» vacancy = importer signal |
| GTD | ГТД | customs declaration | declaration rows are never stored |
| TN VED / TNVED | ТН ВЭД | HS code | customs tariff code |
| STIR | ИНН | taxpayer id | 9 digits; legal entities start 2–3, individuals 4–6 (shape only) |
| OKED | ОКЭД | activity code | 5 digits |
| QQS | НДС | VAT | registration = «active» signal |
| MChJ | ООО | LLC | legal form |
| YaTT | ИП | sole proprietor | legal form; person-named |
| XK / QK / AJ | ЧП / СП / АО | private enterprise / JV / JSC | legal forms |
| reestr | реестр | registry | stat.uz / soliq / myorg |
| bozor | базар / рынок | bazaar / market | Abu Saxiy, Chorsu, Qoʻyliq… |
| hokimiyat | хокимият | local authority | state body → negative |
| voronka / lenta | воронка / лента | funnel / activity feed | ERP CRM terms |
| kelgan arizalar / taqsimot | входящие / распределение | inbound leads / routing | ERP screens |
| yoʻqotish sababi | причина проигрыша | lost reason | «Qayta qilinmasin» = never again |
| yozmang | не пишите | «do not write» | prospect opt-out → DNC |
| bostirish | подавление | suppression | the do-not-contact set |
| egasi / menejer | владелец / менеджер | owner / sales manager | the two human roles |
| tannarx | себестоимость | landed cost | ERP costing; not used by the agent |
| sklad / ostatka | склад / остаток | warehouse / stock | — |

## Open questions

1. Hours vs the ten-day phase plan: ≈ 205 h for Days 1–10. Run T-07 and the bot-independent collectors in a parallel session (recommended), or accept ~4 weeks sequentially?
2. 02 §2.8 orders the suppression hit `ORDER BY kind` (alphabetical). This file's tests require permanent kinds before `cooldown` (`KIND_RANK`). Amend 02, or keep alphabetical and make the gate treat any non-cooldown hit as suppressed (equivalent for the gate, different for `/lidlar` display)?
3. Fixture path: 03 names `tests/fixtures/erp_sync/`, 05 `tests/fixtures/erp/`. This file uses `tests/fixtures/erp/`; 03 to adopt.
4. Scrubbed fixtures for person-tier sources (OLX, Instagram) vs verbatim: the scrubber keeps structure, but a selector that depends on a real name length could pass on scrubbed bytes and fail live. Accept the live smoke as the backstop?
5. Should `CS_FORBIDDEN_STRINGS` (owner-listed real names/handles) be a repository secret from day 1, or added after the pilot when the first real data exists?
6. `make smoke` weekly on the VPS makes ~15 real requests; acceptable as part of the page budgets, or quarterly only?
7. The MVP "real importer rate" counts `lost_other` and `lost_price` as not-negative. Keep (owner's wording) or align with `precision_30d`?
8. Branch protection on `main` requiring all four CI jobs — who holds admin on `bekzodqodirov/clientScrapper` to set it?

## Acceptance criteria

- `make test` = `ruff check && ruff format --check && mypy && pytest` runs green locally with Postgres and in CI with `CS_REQUIRE_DB=1`; the four workflow jobs (`lint`, `test`, `time-travel`, `docker`) are required on `main`.
- Every guard of §1.2 exists, is green, and has a self-test that proves it catches its shape (as MIYA's `test_the_write_detector_catches_each_shape`).
- Every phase-1 collector has recorded fixtures with README lines dated from the VPS, a parse test with concrete fields, a negative fixture and a `@live` smoke test; `sources.enabled` is false for any collector without them.
- The scoring golden tests reproduce 52 → 70, 65 and 48 → 87; the resolver band table, the suppression precedence table, the fake-ERP outcome table and the §1.9 budget boundaries are each pinned by a test named in this file.
- `scripts/pre-push.sh` is installed by `make hooks`, stops at the first red gate, and refuses a planted secret or a missing trailer.
- `tools/mvp_report.py` prints the four §3.2 metrics from fixture data in tests and from the live database in the pilot; `/holat` shows the same numbers.
- The Day-1 checklist (§5.3) is executed on the VPS before any `sources.enabled=true`, every UNVERIFIED item of 03/04/05 has a dated README line, and the §5.6 end-to-end run (push → ERP → outcome → suppression → refusal) has been performed once with the synthetic lead.
- T-01…T-36 each have their acceptance column satisfied before the next task starts; T-37…T-40 start only on the stated gates.
