# 03 — Collectors and the shared HTTP/SERP layer

Scope: every network-facing collector in `clientscrapper/collectors/`, the shared fetch layer (`services/http.py`), the SERP client (`services/serp.py`), the `Collector`/`Observation`/`Budget` contracts, budgets, heartbeats and the fixture-test discipline. Normalisation, resolution, Claude prompts and scoring are in `04-processing.md`; tables in `02-data-model.md`; the ERP contract in `05-erp-integration.md`. Names below are the names the code uses. Facts marked **UNVERIFIED** were not confirmed against a live response in this environment (no outbound HTTP from the research sandbox); each carries its day-1 check.

Sources of record for the per-platform facts: `docs/manbalar.json` (cited as `[v N]` = `verified[N]`), `docs/ochiq-internet.md` (keyword matrix §3, platform reality §4), `docs/ochiq-internet-api-memo.md`, `docs/bepul-qidiruv-memo.md`, `docs/namuna-nomzodlar.md` (seed handles), `docs/huquqiy-memo.md`.

---

## 1. Shared layer

### 1.1 `services/http.py` — the only way a collector touches the network

```python
USER_AGENT = f"clientScrapperBot/{VERSION} (+mailto:{contact_email_from(settings.hh_user_agent)}) python-httpx/{httpx.__version__}"
# HH_USER_AGENT is "clientScrapper/1.0 (email)" as hh.ru requires; the same address
# names the company's crawler everywhere, so one env var serves both.

@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str; final_url: str; status: int; body: bytes; headers: Mapping[str, str]
    fetched_at: datetime; sha256: str; from_cache: bool; not_modified: bool
    truncated: bool = False; captcha: bool = False; source: str = ""

class FetchError(Exception): ...          # base of every refusal this layer raises itself
class RobotsDisallowed(FetchError): ...
class HostPaused(FetchError): ...         # breaker open for this host
class HostBlocked(HostPaused): ...        # raised by the request that tripped the breaker
class BudgetExhausted(FetchError): ...    # .reason ∈ pages | usd | deadline | cost_cap | refused, .detail = the costs.ask code
class UnmeteredCall(FetchError): ...      # a METERED_HOSTS request without a PaidCall, or a PaidCall with no cost gate
class RobotsOptOutRefused(ValueError): ...  # obey_robots=False, or a POST, to a host outside ROBOTS_OPTOUT_HOSTS
class ProxyUnavailable(FetchError): ...   # proxy=True with PROXY_URL blank

@dataclass(frozen=True, slots=True)
class PaidCall:                           # what costs.ask clears before the request (§1.3, 04 §8)
    operation: Operation; est_usd: Decimal; price: Callable[[FetchResult], usage.Priced]   # usage.Priced itself, not a look-alike

class CostGate(Protocol):                 # collectors/runner.ask_gate(session, run_id): costs.ask with the run bound
    def __call__(self, *, operation, est_usd, call, price, source_key: str, now: datetime) -> Awaitable[FetchResult]: ...

class Fetcher:
    def __init__(self, *, rps_default: float | None = None, host_rps: Mapping[str, float] = HOST_RPS,
                 robots_ttl=timedelta(hours=24), robots_dir: Path | None = None, transport=None,
                 hosts: HostState | None = None, hooks=None, cost_gate: CostGate | None = None,
                 agent: str = USER_AGENT, monotonic=…, wall=…, sleep=…) -> None: ...
    def bind(self, *, hooks=None, cost_gate=None) -> Fetcher: ...   # one run's view: shared client, buckets, robots, breaker; its own counters, hooks, gate
    async def get(self, url: str, *, source: str, budget: Budget, params=None, headers=None,
                  timeout: float = 30.0, cache: bool = True, obey_robots: bool = True,
                  paid: PaidCall | None = None, proxy: bool = False) -> FetchResult: ...
    async def post_json(self, url: str, *, source: str, budget: Budget, json: Any, headers=None,
                        timeout: float = 60.0, paid: PaidCall | None = None) -> FetchResult: ...   # APIs: Places, Yandex
```

`source` is the `sources.key` the request is made for — a sub-source (`search_brave`, `maps_google`) under a parent run — and is what a paid call is attributed to. `params` are **merged** into the URL's own query (`copy_merge_params`), never replace it.

| Concern | Rule |
|---|---|
| Per-host token bucket | One bucket per `host`, rate = `HOST_RPS.get(host, COLLECTOR_RPS_DEFAULT=0.3)`, burst 1. `HOST_RPS = {"api.hh.ru": 1.0, "api.search.brave.com": 1.0, "places.googleapis.com": 2.0, "searchapi.api.cloud.yandex.net": 1.0, "operation.api.cloud.yandex.net": 2.0, "catalog.api.2gis.com": 1.0, "www.olx.uz": 0.33, "2gis.uz": 0.2, "html.duckduckgo.com": 0.1, "graph.facebook.com": 1.0, "api.apify.com": 1.0}`; every other host 0.3 (= 1 req/3.3 s). A `Crawl-delay` in robots.txt that is slower wins. |
| robots.txt | Fetched once per **authority** per 24 h — the host, plus the port when it is not the scheme's default, so `http://` and `https://` of one host share one entry — from the URL's own scheme; cached in memory and in `/data/http-cache/robots/<authority>.txt` (a header line records `fetched_at` and status). Read by `services/robots.py` the way RFC 9309 reads it: the groups naming our product token `clientScrapperBot` (case-insensitive, several merged), else the `*` groups, else no rules; the longest matching rule wins and `Allow` wins a tie; `*` matches any run and a trailing `$` anchors; paths and patterns compared after one percent-encoding normalisation; `/robots.txt` itself is always allowed; `Crawl-delay` honoured by convention. (`urllib.robotparser` lets the first matching line win, knows neither `*` nor `$` and drops the first group after a BOM, so it is not used — a collector may not even import it, 07 §1.2.) Disallowed URL → `RobotsDisallowed`, counted in `collector_runs.meta.skipped_robots`, never fetched. A robots.txt that is unreachable or answers 5xx **or 429** → treated as "allow" for 1 h then asked again (kept from this spec although RFC 9309 says a 5xx means disallow; a 429 asks us to slow down and says nothing about rules); a 4xx → no rules for 24 h. `obey_robots=False` (and every `post_json`) is allowed only for `ROBOTS_OPTOUT_HOSTS` — the documented JSON APIs with a key: `api.search.brave.com`, `searchapi.api.cloud.yandex.net`, `operation.api.cloud.yandex.net`, `places.googleapis.com`, `catalog.api.2gis.com`, `api.hh.ru`, `graph.facebook.com`, `api.apify.com` — else `RobotsOptOutRefused`; a test fences the call sites. |
| Conditional fetch | Latest `evidence` row for the URL supplies `meta.etag` / `meta.last_modified` → `If-None-Match` / `If-Modified-Since`. 304 → `not_modified=True`, body from the stored evidence file, no new evidence row. 200 with identical `sha256` → `from_cache=False, not_modified=False`, and the runner counts it in `collector_runs.meta.unchanged` instead of writing a new observation. |
| UA | Always `USER_AGENT` (`clientScrapperBot/<VERSION> (+mailto:<the e-mail in HH_USER_AGENT>) python-httpx/<v>`, the `+mailto` part left out when `HH_USER_AGENT` carries none); collectors may add headers, never replace it — a `User-Agent` in `headers` raises `ValueError` (hh.ru's own UA requirement is Open question 9). |
| Retry-After | On 429/503 with `Retry-After` ≤ 900 s: sleep that long (counts as a 429 for the breaker). Larger → treated as a breaker trip. |
| Backoff | `with_backoff(fn, attempts=3, base=2.0, cap=60.0, retry_on=is_transient_http)` from `services/retry.py` (MIYA, as-is). 4xx other than 429 is never retried. |
| Breaker | Per host, in-process counter of **consecutive** 403/429 (a 2xx resets; a 503 with `Retry-After` counts as a 429; a challenge page counts as 403 — `CAPTCHA_MARKERS` of §1.4, five entries incl. `cf-chl`: a markup marker (`anomaly.js`, `cf-chl`) anywhere, a word marker in the title or a heading, or in the visible text of a page of at most 2,000 visible characters; a JSON answer is data and never a challenge page). At **5** the host is paused 6 h in the process and the request raises `HostBlocked`; further requests to that host raise `HostPaused` until the pause ends. The runner then writes, on the **run's** `sources` row — the schedulable parent for `serp_layer`/`directories`/`maps`, not the sub-site that tripped it (Open question 10) — both `paused_until` (the runner skips the row) and `breaker_until = now + 6 h` (06 §4.2 `collector_blocked` reads it), each with reason `host_blocked:<host>`, and ends the run `failed`, `error='host_blocked'`, `meta.problem='collector_blocked'`; health problem `collector_blocked` (severity warning; critical if still paused after 24 h), owner alert in Uzbek («Manba to'xtatildi: <host>, 6 soat»). |
| Budgets | `Budget.take_page()` before every request (and the run deadline: past it → `BudgetExhausted('deadline')`); at zero → `BudgetExhausted('pages')` and the run ends `status='partial'`. A host in `METERED_HOSTS` (Brave, Yandex search, Places, 2GIS catalog, Apify) refuses a request without a `PaidCall` (`UnmeteredCall`, nothing sent); a paid call checks `Budget.can_spend(est)` (`'usd'`), then the cost gate (§1.3), then sends. |
| Timeouts | connect 10 s, read 30 s (60 s for APIs); body cap 5 MB (larger → cut, `FetchResult.truncated`, counted in the run's `meta.fetch_truncated`); the evidence store then keeps no file for a cut body or for one over 2 MB (`evidence.meta.truncated`, the run's `meta.truncated`, 02 §3). |
| Logging | JSON (`logs.py`, `RedactBodies` + `RedactPatterns`, 06 §4.5): host, status, elapsed, bytes, source, `cache` and a `url_id` (the first 12 hex digits of the sha256 of the stored form of the URL) — **never the URL itself**, not even with its query scrubbed: a path names a person on t.me or instagram.com and a query may carry a phone; never a body. Every URL that is *stored* (evidence, observations, the robots cache) goes through `urlsafe.strip_secrets` first (`key=`, `token=`, `apikey=`, `access_token=` … removed), so an API key never reaches a row or a backup. |
| Redirects | ≤5, same-host or to `www.`; a cross-host redirect is returned (status 3xx) and classified by the caller. |
| Proxy | `PROXY_URL` is applied only when the collector passes `proxy=True` (uzum_sellers shop pages). Everything else goes direct. |

Evidence: the runner (not the collector) writes the body gzipped to `/data/evidence/<yy>/<sha256>.gz` and the `evidence` row (02 §2.3: `source_key, url, fetched_at, sha256, content_type, bytes, raw_path, excerpt ≤600, expires_at`, plus `meta jsonb NOT NULL DEFAULT '{}'` holding `{etag, last_modified, final_url, status, truncated}` — in `0001`, so `final_url` and the HTTP status are not columns of their own).

### 1.2 Collector contract

```python
# clientscrapper/collectors/base.py
Kind = Literal["company", "listing", "post", "vacancy", "exhibitor", "place", "serp_hit", "suppression", "outcome"]

@dataclass(slots=True)
class Observation:
    source: str                      # sources.key, e.g. "olx_business"
    source_ref: str                  # stable id inside the source (see each collector)
    url: str
    fetched_at: datetime
    raw_sha256: str                  # evidence sha256; "" for API rows the runner hashes itself
    kind: Kind
    company_name_raw: str | None = None
    legal_form: str | None = None    # MChJ/OOO/ООО/XK/YaTT/ИП/AJ/QK… as written
    stir: str | None = None
    phones: list[str] = field(default_factory=list)    # as written; normaliser → E.164 (04)
    telegram: str | None = None      # company/channel handle, no "@"
    instagram: str | None = None
    website: str | None = None       # full URL as found
    address: str | None = None
    city: str | None = None
    goods_text: str | None = None
    volume_text: str | None = None   # the verbatim fragment that carried volume
    origin_text: str | None = None   # the verbatim fragment that carried China origin
    signal_types: list[str] = field(default_factory=list)   # from SIGNAL_TYPES below
    excerpt: str = ""                # ≤600 chars, verbatim, what the card will quote
    person_name: str | None = None   # person-tier: at most one, only when self-published
    person_role: str | None = None
    person_telegram: str | None = None   # a group poster's username (person-tier)
    external_ids: dict[str, str] = field(default_factory=dict)  # hh_employer_id, olx_user_id, place_id, twogis_firm_id, uzum_shop_slug, tg_chat_id
    attrs: dict[str, Any] = field(default_factory=dict)         # source numbers: ads_count, followers, order_count, business, volume_m3, brand, official
    published_at: datetime | None = None
    lang: Literal["uz-Latn", "uz-Cyrl", "ru", "en", None] = None

@dataclass(slots=True)
class Budget:
    pages: int                 # sources.daily_page_budget − pages already used today (collector_runs)
    usd: Decimal               # costs.source_budget_usd: sources.daily_cost_cap_usd − today's spend of this source,
                               # a parent row's including its sub-sources' (costs.SUBSOURCE_PREFIXES, §1.3; 04 §8)
    deadline: datetime         # hard wall-clock stop for the run
    pages_taken: int = 0; spent_usd: Decimal = Decimal("0")
    def take_page(self) -> None: ...        # raises BudgetExhausted('pages')
    def can_spend(self, est: Decimal) -> bool: ...
    def charge(self, usd: Decimal) -> None: ...   # the Fetcher, after a paid call (its price, or the estimate when it was sent and failed)

class Collector(ABC):
    key: ClassVar[str]; family: ClassVar[str]; phase: ClassVar[int] = 1
    default_cron: ClassVar[str]; default_rps: ClassVar[float] = 0.3
    default_page_budget: ClassVar[int]; default_usd_cap: ClassVar[Decimal] = Decimal("0")   # seeds sources.daily_cost_cap_usd
    heartbeat: ClassVar[str]              # derived: health.collector_component(key) = f"collector:{key}"; a subclass may not set it
    version: ClassVar[int] = 1            # parsed.collector_version = f"{key}@{version}"
    def __init__(self, fetcher: Fetcher, serp: SerpClient | None = None) -> None: ...   # sets self.found = [] (kept when a run stops)
    async def collect(self, session: AsyncSession, budget: Budget) -> list[Observation]: ...
    # Every HTML/JSON parser is a pure, synchronous @staticmethod tested on fixtures:
    # parse_<page>(body: bytes, *, url: str, fetched_at: datetime) -> list[Observation]
```

`SIGNAL_TYPES` (collectors emit; `04` scores; points per the shared contract): `container_truck` 40 · `volume_m3_3plus` 30 · `volume_m3_1to3` 20 · `wholesale_bulky` 20 (optom/ulgurji + bulky category) · `directory_wholesale` 10 · `ved_vacancy` 15 · `exhibitor` 15 · `marketplace_velocity` 20 · `brand_distributor` (40 when `attrs.official`, else 20 — decided in 04) · `china_origin` 20 · `entity_solid` 15 · `contact_phone` / `contact_handle` 10 · `intent_ask` (volume via Haiku fields) · negatives `b2c_marker` −40, `non_china_origin` −20, `state_body` −20, `liquidated` −50 · exclusions `competitor`, `suppressed`.

Runner (`collectors/runner.py`), one function per scheduled job. `Collector.key` **is** a `sources.key` — the schedulable row. Collectors that fan out over several engines or sites run under a parent row (`serp_layer` family `search`, `directories` family `directory`, `maps` family `maps`, seeded in 02 §2.1), and the runner reads `enabled`, `paused_until`, `daily_page_budget`, `daily_cost_cap_usd` from that row; the sub-source rows (`search_brave`, `search_yandex`, `search_searxng`, `directories_goldenpages`, …, `maps_2gis`, `maps_google`) are what `evidence.source_key` / `observations.source_key` carry for attribution and per-source precision. Skip when `COLLECTORS_ENABLED=false`, `sources.enabled=false`, `paused_until > now`, or `policy.paused()`; build `Budget`; `health.beat(f"collector:{key}", detail={"phase":"start"})`; call `collect`; for each observation write evidence (if not already) and `observations(status='new')`, dedupe on `(source, source_ref, raw_sha256)` within 24 h; write `collector_runs` (02 §2.2 columns: `source_key, started_at, finished_at, status ok|partial|failed|skipped, requests, pages, http_403, http_429, new_evidence, new_observations, new_companies, new_signals, cost_usd, error`; everything else — `unchanged`, `skipped_robots`, `note`, per-sub-source counts — goes into `collector_runs.meta`). A breaker trip ends the run `status='failed', error='host_blocked'` (02's CHECK has no `blocked`); the final beat carries the same counters. An exception ends the run `failed` with the error class only; the scheduler's `coalesce=True, max_instances=1` prevents overlap. Heartbeat names: `collector:<key>`; cadence variants `collector:<key>.<variant>` (`erp_sync.full`, `erp_sync.delta`, `erp_sync.outcomes`, `telegram_monitor.join`, `telegram_monitor.backfill`, `serp_layer.night`). `Collector.heartbeat` is **derived** from the key (`health.collector_component`); a subclass that sets one is refused at class creation. Stale threshold per job = 2 × its interval (problem `collector_silent`, warning). A skipped run writes its `collector_runs(status='skipped', meta.note=<reason>)` row and **no** heartbeat, so `collector_silent` (T-08) must leave out sources that are disabled, paused, breaker-paused or globally paused.

As built (T-05, the wave-1 integration and the seam review):

- **The cost gate.** `run_collector(…, cost_gate_for=ask_gate)` — the real gate is the default; `ask_gate(session, run_id)` binds `costs.ask` to the run's session and `collector_run_id`, and the Fetcher passes each paid request's own wall-clock `now` (never the run's start: a run started 23:50 that pays at 00:50 spends the new day's cap, a Yandex call at 10:29 is outside its window however early the run began). `cost_gate_for=None` leaves the run without a gate: a paid call is then refused `UnmeteredCall` before anything is sent. A paid call attributed to anything but the run's row or one of its sub-sources (`costs.SUBSOURCE_PREFIXES`, §1.3) is refused `UnattributedCall` before `ask` is asked — a collector bug, the run fails on it.
- **The gate's contract.** Only `costs.CostCapReached` raised before dispatch is a budget stop (`BudgetExhausted('cost_cap' | 'refused', <code>)`: `cost_cap` for a cap rung, `refused` for `unpriced`, `op_window:<op>`, `anthropic_silence`); any other exception the gate raises before dispatch is a programming error and propagates. A call that was sent and failed is charged its estimate. `PaidCall.price` must return `usage.Priced` itself (`costs.ask` tells one answer from a batch's list by `isinstance`).
- **Endings.** `ok`; `partial` when a page/dollar budget, the deadline, a cost cap or any other `costs.ask` refusal stopped the run — `meta.exhausted` ∈ `pages|usd|deadline|cost_cap|refused|model_unavailable` and `meta.refused` = the refusal code — including a refusal the collector met outside the Fetcher (a `costs.ModelUnavailable`, `CostCapReached` included, raised inside `collect()`: what the collector put in `Collector.found` is still written); `partial` too when the database refused a row (each evidence group in its own savepoint and each observation in one inside it; a refused group or observation is dropped and counted in `meta.dropped.bad_evidence` / `bad_observation`, the rest is kept); `failed` + `error='host_blocked'` on a breaker trip; `failed` + the error class otherwise. A cancelled run (worker shutdown) closes its own row `failed`/`cancelled` from a fresh session; a `running` row more than 30 min past its `meta.deadline_at` belongs to a dead process and is closed `failed`/`abandoned` before the next claim.
- **Writes.** Evidence for an API row (`raw_sha256=""`) is the canonical JSON of the observation itself; an observation is deduplicated on (source, source_ref, sha256) within 24 h; `parsed` comes from `normalise.to_observation_json` (storable: no NUL, no lone surrogate, no NaN, 02 §2.5); a `serp_hit` is counted (`meta.serp_hits`), never stored as an observation.

### 1.3 Paid calls and budgets

`costs.ask(session, *, operation, est_usd, call, price, now)` — the signature and the refusal order are 04 §8's and are not restated here (MIYA `jarvis/costs.py` generalised): month → day → family (`FAMILY_CAPS`) → operation (`ALLOCATIONS`), then the per-op unit counts (`cost_cap_day_op:<op>` — the free ops' daily caps below; `cost_cap_month_op:<op>` — Brave's and Yandex's 1,000/month) and Yandex's window (`op_window:serp_yandex_night`, 02:00–09:59 Tashkent); it records `usage_log(operation, units, cost_usd, source_key)` in its own transaction, an unpriced unit counts as over cap, and a refusal is persisted too (a `usage_log` row with `cost_usd=0, units=0, model='refused:<code>'`, so `/xarajat` in the bot process can count it). The two budget layers run in a fixed order: first `Budget.can_spend(est)` (per source: `costs.source_budget_usd` = `sources.daily_cost_cap_usd − spend(source_key, with_subsources=True)`), then `costs.ask`. A parent row's dollar budget counts its sub-sources' spend — `costs.SUBSOURCE_PREFIXES = {"serp_layer": ("search_",), "directories": ("directories_",), "maps": ("maps_",)}`: a call is recorded with the sub-source it was made for (`usage_log.source_key='search_brave'`) and budgeted on the parent (`serp_layer`). Collectors never call a paid endpoint outside `costs.ask`; the runner's gate is the default (§1.2).

| operation | unit price | daily cap | used by |
|---|---|---|---|
| `serp_brave` | $0.005/query ($5/1k; $5 credit/month ≈ 1,000) | **32** queries (`ALLOCATIONS["serp_brave"] = 0.165`: a money rung refuses at `spend + est ≥ cap`, so the 33rd query, which would reach 0.165 exactly, is over it — 33 needs 0.170, i.e. 0.005 moved from `serp_reserve`, Open question 11) **and** ≤ 1,000/month (`cost_cap_month_op:serp_brave`) | serp_layer, brand_dealers |
| `serp_yandex_night` | $0 inside 1,000/month night quota; paid Yandex is **not** used in v1 (refuse) | 35 queries/night (`cost_cap_day_op:`), ≤ 1,000/month (`cost_cap_month_op:`), only 02:00–09:59 (`op_window:`) | serp_layer |
| `serp_searxng`, `serp_ddg` | $0 | 300 / 40 (`cost_cap_day_op:`) | serp_layer |
| `places_text_ids` | $0 (IDs-only mask) | 200 (`cost_cap_day_op:`) | maps |
| `places_details_contact` | $0 for the first 1,000/month, then $0.020/call (**UNVERIFIED** SKU, §2.9) | $1.00 | maps |
| `twogis_api` | $0 (demo key) | 40 (`cost_cap_day_op:`) | maps |
| `apify_olx` | $0.00199/listing | $0.60 | olx_business (fallback only) |
| `apify_2gis` | $0.00199/place (searchapi) | $0.40 | maps (fallback) |
| `apify_instagram` | $0.0023/profile | $0.40 | instagram_discovery (fallback) |
| `apify_uzum` | $0.0033/product | $0.60 | uzum_sellers (phase 2) |
| `proxy_uzum` | per GB (provider price → `sources.config`) | $1.00 | uzum_sellers (phase 2) |
| `classify` (Haiku ImportSignal) | per token (04 `MODEL_PRICES`) | `ALLOCATIONS["classify"]` inside `FAMILY_CAPS["anthropic"] = 5.00` (04 §8) | telegram_monitor, instagram_discovery, olx_business |

Sum of collector caps ≤ $5/day; Claude ≤ $5/day (`FAMILY_CAPS["anthropic"]`, 04 §8); total ≤ `DAILY_COST_CAP_USD=10`. The `$/day` column of §3 is seeded into `sources.daily_cost_cap_usd` (the column, not a `config` key). Page budgets (`sources.daily_page_budget`, overridable by `COLLECTOR_DAILY_PAGE_BUDGET`): see the schedule table §3.

### 1.4 Shared keywords and regexes — `collectors/keywords.py`

The matrix from `docs/ochiq-internet.md` §3 and `docs/ochiq-internet-api-memo.md` §2, as code. Every collector imports from here; no collector keeps its own copy — and neither does the pseudonymiser: `services/pseudonyms.py` (`Pseudonymised`, `pseudonymise`, `restore`, `assert_masked`; 04 §4.2) imports `PHONE_RE`, `TG_RE`, `IG_RE` and `EMAIL_RE` from this module, and so do the log redactor (06 §4.5), the fixture scrubber and `contact_email_from` in `services/http.py`. The blocks below are the design; `collectors/keywords.py` (T-03) is the source of truth, and its docstring names each departure — summarised after the blocks.

```python
B2B_MARKERS = {
  "uz-Latn": ("optom", "ulgurji", "ulgurji narx", "optom baza", "zavod narxida", "zavoddan", "ishlab chiqaruvchidan",
              "dilerlik", "distribyutor", "diler", "yetkazib beruvchi", "do'konlar uchun", "minimal buyurtma", "sklad"),
  "uz-Cyrl": ("оптом", "улгуржи", "улгуржи нарх", "заводдан", "дилер", "дистрибьютор", "дўконлар учун", "склад"),
  "ru":      ("оптом", "опт", "мелкий опт", "крупный опт", "оптовая база", "оптовые цены", "от производителя",
              "с фабрики", "прямые поставки", "дилер", "дистрибьютор", "поставщик", "импортёр", "официальный импортер",
              "для магазинов", "склад", "мин. заказ", "MOQ"),
}
VOLUME_WORDS = ("kub", "куб", "m3", "m³", "м3", "м³", "konteyner", "kontayner", "контейнер", "20 fut", "40 fut", "20ft", "40ft",
                "fura", "фура", "tonna", "тонна", "partiya", "партия", "konteyner keldi", "yuk keldi", "фура пришла", "новое поступление")
CHINA_WORDS = ("Xitoydan", "Xitoy", "Хитойдан", "Хитой", "из Китая", "Китай", "КНР", "China", "1688", "Taobao", "Alibaba", "Pinduoduo",
               "Yiwu", "Iwu", "Иу", "Guangzhou", "Guanchjou", "Гуанчжоу", "Urumchi", "Урумчи", "Xorgos", "Хоргос", "Qashqar", "Кашгар", "Shenzhen")
GEO = ("Toshkent", "Тошкент", "Ташкент", "Tashkent", "Abu Saxiy", "Abu Sahiy", "Абу Сахий", "Bek Baraka", "Chorsu", "Чорсу", "Urikzor", "Урикзор",
       "Yangiobod", "Янгиобод", "Malika", "Малика", "Qo'yliq", "Куйлюк", "Sergeli", "Сергели", "Chinni bozor",
       "Andijon", "Андижан", "Samarqand", "Самарканд", "Farg'ona", "Фергана", "Namangan", "Наманган", "Buxoro", "Бухара", "Nukus", "Нукус")
GEO_CITY = {"Toshkent": ("Toshkent", "Тошкент", "Ташкент", "Tashkent"), "Andijon": ("Andijon", "Андижан"),   # canonical Latin city → its spellings
            "Samarqand": (...), "Fargʻona": ("Farg'ona", "Фергана"), "Namangan": (...), "Buxoro": (...), "Nukus": (...)}
GEO_TASHKENT_PLACES = ("Abu Saxiy", "Abu Sahiy", "Абу Сахий", "Bek Baraka", "Chorsu", "Чорсу", ..., "Chinni bozor")   # bazaars and districts
# GEO == the union of GEO_CITY's spellings and GEO_TASHKENT_PLACES (tests/collectors/test_keywords.py holds them equal):
# a place reads «Toshkent» only when no city is named (04 §1.6 canonical_city)
BULKY_CATEGORIES = {   # canonical key → terms in the three scripts (also the SERP <category> axis, 17 keys)
  "textile": ("mato", "gazlama", "мато", "газлама", "ткани", "текстиль"),
  "footwear": ("poyabzal", "пойабзал", "обувь"),
  "apparel": ("kiyim", "trikotaj", "кийим", "одежда", "трикотаж"),
  "household": ("uy-ro'zg'or", "idish-tovoq", "xoztovar", "идиш-товоқ", "хозтовары", "посуда"),
  "auto_parts": ("avto ehtiyot qismlar", "zapchast", "shina", "авто эҳтиёт қисмлар", "автозапчасти", "шины"),
  "building": ("qurilish mollari", "santexnika", "kafel", "қурилиш моллари", "стройматериалы", "сантехника", "плитка"),
  "furniture": ("mebel", "mebel furnitura", "мебель", "мебельная фурнитура"),
  "toys": ("o'yinchoq", "ўйинчоқ", "игрушки"),
  "cosmetics": ("kosmetika", "parfyumeriya", "косметика", "бытовая химия"),
  "electronics": ("telefon aksessuar", "elektronika", "maishiy texnika", "электроника", "бытовая техника", "аксессуары для телефонов"),
  "lighting": ("yoritgich", "LED", "elektr mollari", "ёритгич", "светильники", "электротовары"),
  "equipment": ("uskuna", "dastgoh", "stanok", "ускуна", "дастгоҳ", "оборудование", "станки", "линия"),
  "raw": ("xomashyo", "granula", "plastik", "хомашё", "сырьё", "гранулы", "полимеры"),
  "packaging": ("qadoqlash", "paket", "karobka", "қадоқлаш", "упаковка", "пакеты", "гофрокороб"),
  "stationery": ("kanselyariya", "канцелярия", "канцтовары"),
  "bags": ("sumka", "chamadon", "сумка", "сумки", "чемоданы"),
  "sport": ("sport", "trenajyor", "спорт", "тренажер"),
}
```

```python
# Volume: a number next to a cubic-metre word. Group 1 = the number.
VOLUME_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,]\d)?)\s*(?:kub|куб(?:\.?\s*м)?|m3|м3|m³|м³|kubometr|кубометр)(?![a-zа-яёʻʼ'])", re.I)
# Container / truck / batch words (no number needed).
CONTAINER_RE = re.compile(r"(?<!\w)(?:konteyner|kontayner|контейнер|fura|фура|yarim\s*fura|полфуры|20\s*(?:ft|fut|фут|')|40\s*(?:ft|fut|фут|')|"
                          r"tonna|тонн[аы]?|partiya|партия|партиями)(?!\w)", re.I)
# Buyer intent (groups): Latin, Cyrillic Uzbek, Russian.
INTENT_RE = re.compile(r"(?<!\w)(?:kim\s+olib\s+kel|olib\s+kel(?:ish|adi|asiz|ib)|kerak|kere|kimda\s+bor|kim\s+sotadi|ta[ʼ']?minotchi|"
                       r"ким\s+олиб\s+кел|олиб\s+кел|керак|кимда\s+бор|таъминотчи|"
                       r"ищу|нужен|нужна|нужно|требуется|кто\s+(?:возит|доставит|привез[её]т)|поставщик|закуп(?:аю|ить|ка))(?!\w)", re.I)
# Supply-side posts by a wholesaler (channel posts): arrival of stock.
SUPPLY_RE = re.compile(r"(?<!\w)(?:konteyner\s+keldi|yangi\s+(?:tovar|partiya|konteyner)|yuk\s+keldi|фура\s+пришла|контейнер\s+(?:пришёл|пришел)|"
                       r"новое\s+поступление|новая\s+партия|zavod\s+narxida|optom\s+narx|оптов(?:ые|ая)\s+цен)(?!\w)", re.I)
# Competitors / touts (forwarders, buying agents, brokers) — exclusion, their channels are seeds.
COMPETITOR_RE = re.compile(r"(?<!\w)(?:kargo|karg[oa]|cargo|карго|logistik[a]?|логистик[аи]|ekspeditor|экспедитор|dostavka|доставка|yetkazib\s+beramiz|"
                           r"yuk\s+tashi(?:sh|ymiz|sh\s+xizmati)|грузоперевозк\w*|broker|брокер|rastamojka|растамож\w*|bojxona\s+(?:xizmat|broker)|"
                           r"таможенн(?:ое|ый)\s+оформлен\w*|vikup|выкуп|avia\s*kargo|авиа\s*карго|express|экспресс|1688\s*(?:kargo|cargo|карго|zakupka))(?!\w)", re.I)
# B2C / personal use.
B2C_RE = re.compile(r"(?<!\w)(?:o[ʻ']?zim\s+uchun|ўзим\s+учун|для\s+себя|1\s*(?:dona|шт\.?|штук[аи]?)|bitta\s+(?:dona|narsa)|битта|shaxsiy|шахсий|"
                    r"лично\s+для|taobao\s*zakaz|zakaz\s+qilib\s+ber|в\s+розницу|доставка\s+по\s+городу)(?!\w)", re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?998[\s\-]?)?\(?(\d{2})\)?[\s\-]?(\d{3})[\s\-]?(\d{2})[\s\-]?(\d{2})(?!\d)")   # normaliser validates operator codes
TG_RE = re.compile(r"(?:(?:https?://)?t\.me/(?!joinchat|\+|s/)|(?<![\w@])@)([A-Za-z][A-Za-z0-9_]{4,31})")      # reject names ending in "bot"
IG_RE = re.compile(r"(?:instagram\.com/|(?<![\w.@])(?:инстаграм|instagram|insta|ig)(?:\s*[:\-–—]\s*@?|\s*@|\s+))([A-Za-z0-9._]{2,30})(?![\w.])", re.I)
EMAIL_RE = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")   # 06 §5.2: beside the others
STIR_RE = re.compile(r"(?<!\w)(?:STIR|СТИР|ИНН|INN|TIN)\s*[:№#]?\s*(\d{9})(?!\d)", re.I)
LEGAL_FORM_RE = re.compile(r"(?<!\w)(MChJ|MCHJ|МЧЖ|OOO|ООО|LLC|XK|ХК|QK|ҚК|AJ|АЖ|AO|АО|ЧП|XChK|ХЧК|YaTT|ЯТТ|ИП|SP|СП|ТОО|JV|QO'?SHMA)(?!\w)", re.I)
CAPTCHA_MARKERS = ("captcha", "anomaly.js", "Are you a robot", "Доступ ограничен", "cf-chl")   # the one list; §1.1's breaker reads it
```

**As built (T-03 and its review).** The word lists (`B2B_MARKERS`, `VOLUME_WORDS`, `CHINA_WORDS`, `GEO`, `BULKY_CATEGORIES`) are matched through `terms_re(terms, *, inflect=True)`: whole words or phrases (`(?<!\w)…(?!\w)`), any case, any apostrophe (every variant `services/text.py` folds: ʻ ʼ ‘ ’ \` ʹ '), е/ё alike, and **inflection-aware** on the last word of a term — an Uzbek suffix chain (up to two of `UZ_SUFFIXES_LATN` / their Cyrillic spellings: «Xitoydan», «dilerlarga», «skladimizdan») and, for a Cyrillic word of ≥ 5 letters, the Russian endings («в Китае» is Китай in the prepositional case, «импортер» is «импортёр»); a word shorter than 4 letters matches only as written, so «опт» never grows into «оптика» (a plain substring test would). 04 §5.3's marker test and the China evidence words of §9.1/§9.3 («Guangzhoudan», «в Китае») are this rule. The regexes depart from the block above where 03's own acceptance (≥ 3 positives per regex in Latin Uzbek, Cyrillic Uzbek and Russian) or a defect forced it: their words take Uzbek suffixes and Russian endings («20 кубов», «5 kubdan», «фуру», «konteynerda», «Ищем», «kargoxonasi», «логистикой»); `SUPPLY_RE` and `COMPETITOR_RE` gained the Uzbek Cyrillic twins of their Latin alternatives and `INTENT_RE` the Cyrillic endings of «олиб кел…» («ким олиб келади?»); «кубик» is not a unit; IG_RE's label branch needs a word start and a separator («big shop» is not the handle «shop», «Instagramda» is a word); STIR_RE's label needs a word start; LEGAL_FORM_RE gained МЧЖ, ХЧК and СП. `GEO_CITY` holds only city names and `GEO_TASHKENT_PLACES` the Tashkent bazaars and districts; `GEO` is their union (`CITY_RES`, `TASHKENT_PLACES_RE`, `GEO_RE`). Known false positive, not fixed: «Malika» is a bazaar and a given name, so «Malika, Samarqand» still names Samarqand but a bare «Malika» reads as Tashkent.

Python's `re` is Unicode-aware for `str` patterns, so `(?<!\w)` works on Cyrillic; the JS `\b` trap does not apply here. `tests/collectors/test_keywords.py` pins ≥3 positive and ≥3 negative examples per regex in each script (e.g. «3 kub kerak Yiwudan» → VOLUME 3, INTENT; «1688 kargo Toshkentga» → COMPETITOR; «o'zim uchun 1 dona» → B2C; «оптом» must not match «оптика»).

### 1.5 Fixture-based tests

- `tests/fixtures/<key>/<name>.{html,json,xml}` recorded with `tools/record_fixture.py <url> --key <key> --name <name>` (goes through `Fetcher`, so robots and UA apply; robots.txt is skipped at `ROBOTS_OPTOUT_HOSTS` as the collector skips it; a metered host needs `--paid <operation> --est <usd>`, which goes through `costs.ask` like any paid call — 07 §1.1). Telegram fixtures are hand-written synthetic messages, never real ones.
- `tests/collectors/test_<key>_parse.py`: each `parse_*` on each fixture → exact expected `Observation` fields (name, phones, handles, signal_types, excerpt). A parser that returns `[]` on its fixture fails (a test that passes because its subject never appeared proves nothing).
- `tests/collectors/test_http.py`: token bucket with a fake clock (two requests to one host are ≥ 1/rps apart; two hosts interleave); robots disallow → no request made; 304 path; `Retry-After` honoured; breaker opens on the 5th 403 and `sources.paused_until` is written; `BudgetExhausted` stops a loop mid-page; UA header present on every request; `key=`/`token=` scrubbed from logs; `obey_robots=False` only at the allow-listed call sites (AST test).
- `tests/live/test_<key>_live.py` marked `@pytest.mark.live`, run only with `LIVE=1` from the VPS (`make smoke`); each performs one real request and asserts the fixture-era selector still yields ≥1 row. A skipped live test is reported as skipped in the digest, never as green.

---

## 2. Collectors

Schedules are Asia/Tashkent. "Yield" figures are estimates from the live sample (`docs/namuna-nomzodlar.md`: 118 candidates from ~84 queries) unless stated; treat all as UNVERIFIED until 30 days of `collector_runs`.

### 2.1 `serp_layer` — the search engines (`services/serp.py` + `collectors/serp_layer.py`)

**Purpose.** Turn the keyword matrix into URLs of Instagram profiles, Telegram channels, OLX sellers, directory cards and company sites, and fetch company sites for contacts. It also serves `brand_dealers` and the enrichment step in 04 (`SerpClient.search`).

```python
@dataclass(frozen=True, slots=True)
class SerpQuery: text: str; lang: Literal["ru","uz","en"]; site: str | None = None; freshness: Literal["pw","pm",None] = None; count: int = 20
@dataclass(frozen=True, slots=True)
class SerpHit: engine: str; rank: int; url: str; host: str; title: str; snippet: str; fetched_at: datetime
class SerpClient:
    async def search(self, q: SerpQuery, *, session, budget, engines: tuple[str, ...] = ("yandex_night","brave","searxng","ddg")) -> list[SerpHit]
def classify_hit(url: str) -> tuple[HitKind, dict]:   # HitKind = instagram_profile|instagram_post|telegram_channel|telegram_post|olx_listing|olx_seller|facebook_page|directory_card|company_site|news|noise
```

| Engine | Endpoint / auth | Request | Response parse | Limits / cost |
|---|---|---|---|---|
| **Brave** | `GET https://api.search.brave.com/res/v1/web/search`, header `X-Subscription-Token: $BRAVE_API_KEY`, `Accept: application/json` | `q`, `count=20`, `offset`, `search_lang=ru` (or `uz`?), `ui_lang=ru-RU`, `country=ALL` (**UNVERIFIED** whether `country=UZ` is accepted — day 1: send it once; a 422 means fall back to `ALL` + geo words), `freshness=pm` when `q.freshness` | `web.results[].{url,title,description,page_age}`, `query.more_results_available` | $5/1k, $5 credit/month ≈ 1,000 → cap **32/day** (`ALLOCATIONS["serp_brave"] = 0.165` under the ≥ rule, §1.3) **and** a monthly counter `serp_brave ≤ 1,000` inside `costs.ask('serp_brave')` — past the credit the card is charged silently; rate 1 rps (**UNVERIFIED** plan limit). Attribution requirement applies only to displayed results; we display none. |
| **Yandex Cloud Search API v2 (deferred)** | `POST https://searchapi.api.cloud.yandex.net/v2/web/searchAsync`, header `Authorization: Api-Key $YANDEX_SEARCH_API_KEY` | body `{"query":{"searchType":"SEARCH_TYPE_RU","queryText":q,"familyMode":"FAMILY_MODE_NONE","page":"0","fixTypoMode":"FIX_TYPO_MODE_ON"},"groupSpec":{"groupMode":"GROUP_MODE_FLAT","groupsOnPage":"50","docsInGroup":"1"},"maxPassages":"3","region":"10335","l10n":"LOCALIZATION_RU","folderId":$YANDEX_FOLDER_ID,"responseFormat":"FORMAT_XML","userAgent":USER_AGENT}` → `{"id": op}`; poll `GET https://operation.api.cloud.yandex.net/operations/{op}` every 20 s until `done` | `response.rawData` base64 → XML `yandexsearch/response/results/grouping/group/doc/{url,title,passages/passage}` | **1,000 free requests/user/month, only 00:00–07:59 MSK = 02:00–09:59 Tashkent** (`docs/bepul-qidiruv-memo.md`); cron 02:10, ≤35/night, `costs.ask('serp_yandex_night')` refuses outside the window or past 1,000/month. **UNVERIFIED**: `SEARCH_TYPE_UZ` acceptance (try once; 400 → stay on RU), region ids (Tashkent 10335, Uzbekistan 171 — confirm against the Yandex region list), whether the free quota applies to the deferred mode only (the memo says so). |
| **SearXNG** (own container, optional) | `GET {SEARXNG_URL}/search` | `q`, `format=json`, `language=ru`, `categories=general`, `engines=duckduckgo,startpage,mojeek,brave`, `pageno` | `results[].{url,title,content,engine}`, `unresponsive_engines` | $0; ≤0.2 rps; `settings.yml`: `search.formats:[html,json]`, `server.limiter:false` (bound to the compose network only); our own instance still goes through `Fetcher`, so its robots.txt applies — the stock image's may disallow `/search?q=` (Open question 12); empty `results` with non-empty `unresponsive_engines` = transient (retry next run), three in a row = breaker on `searxng`. Google/Bing engines stay disabled. |
| **DDG html** (last resort) | `POST https://html.duckduckgo.com/html/` form `q`, `kl=ru-ru` | parse `a.result__a[href]` (`uddg=` param URL-decoded), `a.result__snippet` | — | ≤0.1 rps, ≤40/day; `anomaly.js`/403 → breaker. |

Engine routing: `site:`-queries and English → Brave; RU/UZ-Cyrillic text queries → Yandex at night, SearXNG by day; overflow → SearXNG → DDG. Each query runs on exactly one engine per week unless it returned < 3 hits (then it is re-queued to the next engine).

`sources` rows: the collector is scheduled and budgeted under the parent row `serp_layer` (family `search`, 02 §2.1); each hit's `evidence.source_key` is the engine's own row — `search_brave`, `search_yandex`, `search_searxng` — so per-engine precision can be read; DDG has no row of its own and attributes to `serp_layer`.

**Query templates** (`collectors/serp_templates.py`), rendered per `(category ∈ BULKY_CATEGORIES, lang ∈ {uz-Latn, uz-Cyrl, ru}, marker ∈ 3 per lang, geo ∈ {Toshkent, Samarqand/Farg'ona/Andijon rotated, none})` = 17 × 3 × 3 × 3 = **459 text queries/week** (≈ 66/day) plus **34 `site:` queries/week** on Brave:

```
site:instagram.com ("optom" OR "оптом" OR "ulgurji") "<category_ru>" ("Toshkent" OR "Ташкент")
site:t.me ("оптом" OR "optom") "<category_ru>"
site:olx.uz "оптом" "<category_ru>"                      # cheap seed for olx_business
"<marker>" "<category>" "<geo>"                          # text query, lang-specific
"импорт из Китая" "оптом" ООО site:.uz · "Xitoydan import" "ulgurji" MChJ   # company sites
```

`classify_hit` rules (regex on `host`+`path`): `instagram.com/<h>/` (not `/p/ /reel/ /explore/ /tags/`) → `instagram_profile{handle}`; `instagram.com/p/|/reel/` → `instagram_post`; `t.me/s/<n>` or `t.me/<n>` (not `joinchat`, `+`, `share`) → `telegram_channel{username}`, `t.me/<n>/<id>` → `telegram_post`; `olx.uz/d/.*-ID\w+\.html` → `olx_listing`, `olx.uz/list/user/<id>` → `olx_seller{olx_user_id}`; `DIRECTORY_HOSTS = {goldenpages.uz, yellowpages.uz, top.uz, glotr.uz, prom.uz, flagma.uz, orginfo.uz, 2gis.uz, uztender.com, hh.uz, hh.ru, enfsolar.com, all.biz, optlist.ru}` → `directory_card` (queued to that collector's card fetch); `NEWS_HOSTS` (spot.uz, kun.uz, gazeta.uz, daryo.uz, uznews.uz, podrobno.uz, review.uz, afisha.uz) → `news` (kept for brand_dealers name extraction); `NOISE_HOSTS` (wikipedia, youtube, facebook, tiktok, pinterest, linkedin, .ru marketplaces) → `noise`; any other host → `company_site`.

**Observations.** One `kind="serp_hit"` per hit with `source_ref=f"{engine}:{sha1(url)[:16]}"`, `excerpt=snippet`, `instagram`/`telegram`/`website` set by kind, `attrs={"hit_kind","query","rank"}`, `lang`. For `company_site` hits (≤120 fetches/day from the page budget): fetch the landing page plus the first matching of `/about|/o-kompanii|/kompaniya-haqida|/biz-haqimizda|/contacts|/kontakty|/aloqa`; `parse_company_site` → `kind="company"`: `company_name_raw` (`<title>`/`og:site_name`/footer `LEGAL_FORM_RE` sentence), phones (`PHONE_RE` in `tel:` links first), `TG_RE`, `IG_RE`, `STIR_RE` (footer), address (line containing a GEO word + "ko'cha|улица|ул.|проспект|MFY"), markers → `wholesale_bulky`/`china_origin`/`brand_distributor`; `COMPETITOR_RE` in title/about → `competitor`.

**Schedule / rate.** `serp_layer` 10:00 and 14:00 (Brave + SearXNG, ≈ 33 queries per run; Brave itself ≤ 32/day in total — `site:`/English queries first, the rest on SearXNG), `serp_layer.night` 02:10 (Yandex, ≤35). Page budget 300/day. **Yield.** ~1.4 raw candidates/query in the live sample → 300–600 hits/week, 60–120 new handles/sites after dedupe. **Failure modes.** Brave 401/402 (key/credit) → `collector_blocked` immediately (not after 5); Yandex quota exhausted → silent skip until the 1st; SearXNG CAPTCHA storms → empty results, retried next run; `site:` ignored by an engine → post-filter on `host` (always applied). **Legal.** Low: provider terms; we store URL + snippet + our own fetch of public pages, robots respected. **Day 1.** One Brave call with `country=UZ`; one Yandex async call inside the window with `SEARCH_TYPE_UZ` then `RU`; record `tests/fixtures/serp_layer/{brave.json,yandex.xml,searxng.json,ddg.html}`; confirm the free-quota wording on the Yandex pricing page and the Brave plan rate limit in the dashboard.

### 2.2 `erp_sync` — the two ERP feeds and the XLSX fallback

**Purpose.** Keep the agent's `suppression` set equal to the ERP's (client-book phones ∪ leads lost with a no-contact reason) and read back what happened to pushed leads. No lead state lives here (see 05). The collector is a thin scheduler shell: every rule below is owned by `services/erp_feeds.py` (05 §B.2) and the `suppression` DDL (02 §2.8); this table says only when each function runs and what 03 adds.

| Item | Rule |
|---|---|
| Auth | `X-GSR-Key: $CRM_AGENT_KEY` — VERIFIED on `agent-feed` (05 §A.2, §A.5): the only header the ERP reads; `X-ERP-Key` alone is a 404. The agent sends both with one value (`ERP_KEY_HEADERS`, 05 §B.1). The feeds run only while `feeds_configured()` (`CRM_PUSH_SOURCE=agent`): during the fallback `CRM_AGENT_KEY` is the «Boshqa» door's secret, which reads nothing, and pushes carry `utm_source=clientscrapper` (05 §B.3). |
| Suppression full | `erp_sync.full`, cron `30 2 * * *` (after `serp_layer.night` 02:10, before outcomes 03:00): `sync_suppression(mode="full")` — `GET {CRM_BASE_URL}/api/agent/suppression?limit=5000` with **neither `since` nor `cursor`** (only such a walk may delete), follow `next` until null; steps 1–5 of 05 §B.2 (`rule`/`salt_id` check, upsert, deletion of unseen `erp_feed` rows when ≥ 90 % of the previous full pull's distinct entries were seen, `config.suppression` write). A `salt_id` different from the stored one means the key was re-minted: the completed full pull replaces the set (05 §B.2 steps 2 and 4). |
| Suppression delta | `erp_sync.delta`, cron `15 * * * *`: `sync_suppression(mode="delta")` with `since=<config.suppression.delta_generated_at − 10 min>&limit=5000` — the anchor is the ERP's own `generated_at` of the previous delta (or full pull), written with `Z`; additive upsert only (a delta never retires anything, 05 §A.7); a differing `salt_id` aborts the delta and schedules a full pull now (05 §B.2 step 2). Skipped while a full pull runs. |
| Storage | As 02 §2.8 / 05 §B.2 steps 3–4: `suppression(kind IN ('client','lost'), key_type='phone_hash', key=h, salt_id, source='erp_feed', erp_at, last_seen_at)`; rows a completed full pull did not re-see are **deleted** (there is no `removed_at` and no `origin`). The agent never holds the client book in clear: the gate hashes a candidate's tail with `erp_feeds.hash_tail(CRM_AGENT_KEY, tail9)` (= the feed's `rule` string; a `rule` mismatch → problem `feed_contract` with code `rule_changed` (05 §B.2), run `failed`, alert). |
| Outcomes | `erp_sync.outcomes`: four walks (05 §B.2 table) — nightly `0 3 * * *` the `cursor` walk (`GET /api/agent/outcomes?limit=1000&cursor=<config.outcomes.cursor>`, the stored cursor = the last non-null `resume`) then the `recent` walk (`since=now − 8 d`); Sunday `20 3 * * 0` the `quarter` walk (`since=now − 90 d`); the 1st of the month `40 3 1 * *` the `year` walk (neither — the only read that sees a stage-kind flip on an old row); plus `reconcile_job`'s 10-minute `unknown` walk. Rows whose `external_id` the agent never pushed are ignored; upsert `outcomes` by its primary key `external_id`; `label = services.outcomes.derive_label(row, pushed_at=…, now=…)` and `apply_effects` (02 §2.11: local suppression recomputed per company — `lost_no_contact` → `suppression(lost)` on the company's other keys because the ERP suppresses by phone only; `won`/`landed_client`/`dropped_suppressed` → a local `suppression(client, phone_tail)`; retractions when a label changes) in the same transaction; `push_log` rows `unknown` → `sent`; after the nightly walks the 48-hour rule of 02 §2.10 rule 5. |
| Freshness gate | `services/erp_feeds.suppression_fresh()` is the one home: last successful full pull ≤ `FULL_MAX_AGE_H = 26` h ago **and** last delta ≤ `DELTA_MAX_AGE_H = 3` h ago, with the stored `salt_id` equal to the current key's. False → health problem `suppression_stale` (warning; critical after 6 h; 06 §4.2 / 05 §B.6 texts say 26 soat) and the push gate (04) refuses every push. Fail closed. |
| Errors | 05 §B.2 / §B.6 are the table; in short: 404 — and a 401/403, which the ERP never sends but a proxy might (05 §B.2 decides it) — → the key or source is wrong → problem `push_404` (same misconfiguration as a push 404), owner alert «ERP kaliti yoki manba notoʻgʻri», stop; a 3xx (Caddy's 308 to an `http://` `CRM_BASE_URL`) is never followed → breaker `erp_redirect`, owner alert «CRM_BASE_URL `https://` bilan boshlanishi kerak», stop (05 §B.1); 400 (`bad_cursor`/`bad_since`), a changed `rule`, a `max_pages` runaway or a repeated cursor, a PII key → `feed_contract` (codes `bad_request`/`rule_changed`/`runaway`/`shape`), stop; 503 `{"error":"unavailable"}`, other 5xx, timeout → `with_backoff(attempts=4, base=2, cap=60)` then `failed` (never `push_404`); a 200 with invalid JSON → `feed_contract` `shape`, body sha logged. |
| XLSX fallback | Before the feed exists (or when it is down > 48 h): the owner exports **Mijozlar → XLSX** from the ERP (the ⬇ link of `/admin/clients`, route `/api/clients/xlsx`, gated on `clients.manage`). Verified on `agent-feed` `d138b3bc`: the headers are `Code, Name, Manager, Phones, Active` plus the custom fields' own labels (`CORE_TITLES`); the export carries the screen's `q`, custom filters, saved-view `cols` and sort, so a filtered screen or a view without «Phones» exports fewer rows or no phone column — **export from an unfiltered screen with no saved view (or one that shows Phones) and check the row count**; it is capped at 5,000 rows (`CLIENT_EXPORT_CAP`) and includes inactive cards; a card whose `phones` is not a JSON array exports an empty cell. The «Phones» cell is the card's `phones` list joined with `", "`. The owner runs `make import-clients FILE=clients.xlsx` or sends the file to the owner bot (`/import`). `services/client_import.py` (MIYA, adapted) finds the column by its header «Phones», splits the cell at `", "` back into the card's phone elements **first** (the ERP derives tails per element: an element like «90/1234567», whose parts are shorter than nine digits, yields 901234567 only from its own whole value), then derives each element's tails by the ERP's own rules (05 §A.7, `PHONE_SPLIT_PG`/`tailsSql` in `agent-feed.ts`): the element's last nine; each part split at `, ; / | \`, LF or CR, a run of two or more spaces or tabs, or spaces/tabs before `+`, when the part has ≥ 9 digits; and inside a part of 16–120 digits: a part made **entirely** of 12-digit `998…` numbers (`^(998[0-9]{9})+$`) → each number's last nine; else a part whose length is a **multiple of 9** → each 9-digit chunk; anything else (a 20-digit part, say) and any part over 120 digits → its last nine only — chunks the feed never carries would never retire (05 §B.2 step 5). It stores **clear tails**: `suppression(kind='client', key_type='phone_tail', key=tail9, source='manual_import')` (05 §B.3) — not hashes, because the future agent secret is unknown and hashes would not match. The fallback cannot see lost leads — the digest says so while no `erp_feed` row of `kind='lost'` exists. During the fallback these tails are the **only** guard for a client holding several codes: the «Boshqa» door has no belt and recognises a client only when exactly one active card holds the phone (05 §A.3 step 6), so such a phone would land as a new lead. The rows retire themselves: the first completed full pull that covers them deletes the `manual_import` rows (05 §B.2 step 5). |

Fixtures: `tests/fixtures/erp/` captured from the `agent-feed` branch at `23fe1736` (07 §1.0 lists the files: full, paged, resume-after-end and delta suppression pages, outcomes full and since, every POST outcome, every 400/404 answer) plus a synthetic `clients.xlsx`. Tests: salt change triggers a full resync; a row absent from a completed full pull is deleted, a shrunken pull deletes nothing (`suppression_shrunk`); the gate refuses when stale; the fake-ERP 404 raises `push_404`; `manual_import` rows are retired after a covering full pull.

### 2.3 `telegram_monitor` — the read-only reader account

**Purpose.** Capture buyer intent («3 kub kerak Yiwudan», «kim konteyner olib keladi») in public wholesale/cargo groups and wholesaler channel posts, and the business contacts channels publish about themselves. Two parts: `userbot/main.py` (Telethon, network) stores matching messages into `interactions`; `collectors/telegram_monitor.py` (no network, no `telethon` import — 07 `test_collector_imports`) reads them, classifies and emits observations. The path is event-driven (01 N-06 «Telegram intent → owner card p90 < 5 minutes»): the userbot runs `NOTIFY cs_interaction` after every insert, the worker's `classify_job` / `score_job` wake on that notification (04 §0) and `review_cards_job` runs every 2 min inside `REVIEW_HOURS` (06 §1.6, burst caps unchanged); a fallback sweep every 15 min catches anything a lost notification missed.

**Account policy** (binding): a dedicated number, 2FA on, aged ≥ 30 days before the first join; session in `TELETHON_SESSION` (StringSession made by `make userbot-login`); `connect()` not `start()`; `is_user_authorized()` false → beat `userbot_unauthorized`, idle; `USERBOT_ENABLED=false` → beat `{"enabled": false}` and idle forever; `flood_sleep_threshold=60`; `FloodWaitError > 300 s` → 24 h breaker (`flood_breaker`); never `SendMessage`, `SendReaction`, `SetTyping`, `GetParticipants`, `ImportChatInvite`, `ReadHistory`; media is not downloaded in v1 (document file names only). `tests/test_no_telegram_writes.py` (MIYA AST test) fences the write calls; a second test fences the join call to `userbot/joiner.py` only.

**Seeds** (`sources.config.seeds`, from `docs/namuna-nomzodlar.md` §1 and §3, `[v 9]`, `[v 16]`, `[v 29]`): buyer-side groups first — «CHINNiLAR OPTOM TOSHKENT» (~165k members; handle **UNVERIFIED** — resolve via TGStat `ratings/chats` Uzbekistan), `@optom_tavarlar_uz` («Chinni bozor online»), `@Optom_bozor` (classifieds feed: the lead is the poster, not the channel), `@optomelonuz`; wholesaler channels — `@Pekinoptom_uz`, `@abusaxiyoptomm`, Chinni Bozor Bekzod (`patnoss`, probable); competitor channels whose **linked discussion groups** are the surface — `@mk_express`, `@fx_express`, `@Halol_Cargo`, Abu Sahiy Cargo, Meest China, CARGO GREEN (handles to confirm inside Telegram); GSR's own `@gsrgroupchat` is seeded as `chat_monitors.role='calibration', monitor_enabled=false` — the account may sit in it, but **nothing is read or stored** from it (its members are existing clients; storing their posts would be person-tier collection with no lead purpose); prefilter calibration uses the synthetic fixtures instead. Excluded as seeds: `@cargo5566` (Russia-audience), religion/B2C clones of «Optom bozor» (dedupe by numeric channel id, never title).

**Join and discovery.** `userbot/joiner.py` daily at a random minute 10:00–20:00: take ≤ **3** rows from `chat_monitors(status='queued')` ordered by priority, `JoinChannelRequest(username)` (public usernames only; no invite links in v1), then `GetFullChannelRequest` → `full_chat.linked_chat_id` → enqueue the discussion group (priority 1), read `about`/pinned for `TG_RE` mentions of companion «xaridorlar/buyers» groups → enqueue (priority 2). Also resolve ≤ 20 usernames/day found by other collectors (`serp_layer` `telegram_channel` hits, directory cards) and by enrichment step E3 (04 §3) → `chat_monitors(status='queued', priority 3)` after a `t.me/s/<username>` HTML check through `Fetcher` (title, description, subscriber count, last post date; silent > 90 days → not queued). E3 itself parses that public page for phones/site/address; the Telethon read (`GetFullChannelRequest` for a channel's full `about`, `GetFullUserRequest` for a poster's bio) is done **only** by `userbot/about.py` (`read_about`, the one module besides the joiner allowed raw requests — 07 `RAW_ALLOWED`) when it drains those queued rows; no `telethon` import exists outside `userbot/`. Cap: 60 joined chats; the weekly yield report (`stored/week` per chat) proposes pruning (owner `/chats`).

**Backfill** (`telegram_monitor.backfill`, 01:00 nightly, chats joined ≥ 24 h ago and not yet backfilled): `iter_messages(chat, limit=500)` filtered **client-side** by the prefilter (server `search=` is per keyword and misses Latin/Cyrillic variants); store only matches; mark `chat_monitors.backfilled_at`. Live: `events.NewMessage(chats=ids)` → the same prefilter → store match + `fwd_from_peer_id` (`interactions.fwd_from_peer_id bigint`, 02 `0001`; NULL when not forwarded) + sender id/username/first+last name + chat id/title + date; non-matches increment `chat_monitors.seen_count` and are dropped (no full chat logs). Edits re-run the prefilter (`MAX_EDITS=10`).

**Prefilter** (`userbot/prefilter.py`, pure): a message is stored when (group) `VOLUME_RE | CONTAINER_RE | INTENT_RE` matches and `len(text) ≥ 12`; (channel post) `SUPPLY_RE | CONTAINER_RE | B2B_MARKERS` matches. Channel description and pinned post are **not** `interactions` (that table has no `kind` and requires a non-empty `prefilter_hit`): once per week they are stored as an `evidence` row (`url=https://t.me/s/<username>`, `source_key='telegram_monitor'`) plus one observation — `kind="company"` in the collector contract, `parsed.kind='profile'` in 02 §2.5 — from which «Observations» below parses the contacts.

**Tout filter** (`collectors/telegram_monitor.py`, before any model call): mark `competitor` when the sender's username/first/last name/bio (`interactions.sender_bio`, filled by `userbot/about.py` via `GetFullUserRequest` ≤ 50/day, cached 30 d — the collector itself never touches Telethon) or the message matches `COMPETITOR_RE`; or the sender posted ≥ 3 price-list-shaped messages (≥ 3 prices `\d{2,}\s*(?:so'm|сум|\$|usd|y\.e)` or > 1,500 chars) in 7 days; or `fwd_from` is a channel other than the sender's own. Competitors are excluded as leads but their messages feed the weekly competitor-offer diff (owner report). `B2C_RE` → `b2c_marker`. Messages still standing go to Haiku `ImportSignal` (04 owns the prompt; ≤ 300/day via `costs.ask('classify')`), pseudonymised by `services/pseudonyms.py` (04 §4.2): phones → `<PHONE_n>`, handles → `<TG_n>`, names → `<NAME>`. Output `{is_buyer_intent, goods, origin_cn, volume_m3_est, volume_basis ∈ stated|container|truck|inferred, frequency ∈ one_off|repeat, b2c, competitor, confidence}`.

**Observations.** Group message → `kind="post"`, `source_ref=f"{chat_id}:{msg_id}"`, `url=https://t.me/{username}/{msg_id}` (or `https://t.me/c/{internal_id}/{msg_id}`), `person_telegram=sender.username` (person-tier; `legal_basis='public_source'` is attached by 04), `excerpt` = message ≤ 600 chars, `volume_text`/`origin_text` = matched fragments, `attrs={"volume_m3": float|None, "basis", "frequency", "chat_title", "repeat_count_60d"}`, `signal_types` = `intent_ask` + `volume_m3_*`/`container_truck` + `china_origin`. Channel post → `kind="post"` for the channel owner (`telegram=channel username`, `signal_types` `wholesale_bulky`/`container_truck`); channel `about` (the weekly `evidence` row of the prefilter paragraph) → `kind="company"` (02 `parsed.kind='profile'`) with phones/website/instagram/address parsed from the description. Repeat posters (≥ 3 intent posts in 60 days) get `attrs.repeat_count_60d` (trader, not B2C).

**Schedule / rate.** Userbot continuous (heartbeat `userbot`); the collector wakes on `NOTIFY cs_interaction`, fallback sweep `*/15`; join daily; backfill nightly. **Yield.** 30–50 chats → 10–40 regex hits/day → 2–8 qualified/day after the tout filter (`docs/arxitektura.md`; UNVERIFIED — `[v 16]` warns most traffic is supply-side touts). **Failure modes.** FloodWait (breaker), `ChannelPrivateError` (chat removed → `chat_monitors.status='gone'`), `UsernameNotOccupied`, account limited (`UserDeactivatedBan` → `userbot_unauthorized`, process idles, owner alert), `AuthKeyDuplicatedError` (a second client on the same session — never run two). **Legal.** Medium (`docs/huquqiy-memo.md`: Telegram content-licensing terms ban aggregation for AI; mitigations: read-only, matched-only storage, no member lists, 30-day retention of unconverted posts, company-tier extraction). **Day 1.** Login; join 3 seeds; confirm `linked_chat_id` resolves for `@mk_express`; record 20 synthetic messages as fixtures; verify the prefilter catches Cyrillic Uzbek («ким олиб келади», «керак»).

### 2.4 `instagram_discovery`

**Purpose.** Find wholesale business profiles (bio: optom/ulgurji/оптом, Xitoydan, bozor address) and read their business contacts. Discovery is SERP + cross-source handles; enrichment is the official Graph API; Apify is a bounded fallback.

| Route | Endpoint / auth | Request | Parse | Limits |
|---|---|---|---|---|
| Discovery | `serp_layer` `instagram_profile` hits; `IG_RE` matches from Telegram `about`/posts, OLX text, directory cards, company sites | — | handle → `enrich_queue` (dedupe lower-case) | — |
| **Business Discovery** (official) | `GET https://graph.facebook.com/{graph_version}/{IG_BUSINESS_ACCOUNT_ID}?fields=business_discovery.username({handle}){username,name,biography,website,followers_count,media_count,media.limit(12){caption,timestamp,permalink,media_type}}&access_token=$IG_GRAPH_TOKEN` | one call per handle | bio → `PHONE_RE`, `TG_RE`, `IG_RE`, GEO/bozor address, markers; captions → `SUPPLY_RE`/`CONTAINER_RE`/pack units; `website` linktree/taplink → one hop fetched for `t.me`/phones | GSR's own IG Business account + FB app; `{graph_version}` = `sources.config.graph_version`, **UNVERIFIED** which version is current (`v21.0` was current in Oct 2024 and Graph versions retire after ~2 years) — never hard-coded; permissions `instagram_basic` (+ `pages_show_list`; exact names **UNVERIFIED**); app-level rate ~200 calls/h → cap 150/h, **200 handles/day**; error code 110/`(#100)` for personal accounts = `not_business` → the handle is dropped (B2C/personal, never stored). |
| **Hashtag Search** (official) | `GET /ig_hashtag_search?user_id=&q=<tag>` → id; `GET /{id}/recent_media?user_id=&fields=id,caption,permalink,timestamp,media_type&limit=50` | 4 new tags/day from `HASHTAGS` (30-tag rolling list) | captions → `@mentions` (companies tag themselves) and `TG_RE`; permalink stored as evidence | **30 unique hashtags / 7 days / account**; needs "Instagram Public Content Access" app review; `recent_media` carries **no author username** (per docs) — yield is captions only (**UNVERIFIED**: check once whether `username` is returned). |
| Apify fallback | `apify/instagram-profile-scraper` via `POST https://api.apify.com/v2/acts/apify~instagram-profile-scraper/run-sync-get-dataset-items?token=$APIFY_TOKEN`, input `{"usernames":[…≤50]}` | only when no Graph token is configured or Graph returned 4 (rate) | `biography, externalUrl, followersCount, postsCount, businessCategoryName, isBusinessAccount` (+ `businessPhoneNumber/businessEmail` **UNVERIFIED**) | $0.0023/profile, `costs.ask('apify_instagram')` ≤ $0.40/day (~170 profiles); `isBusinessAccount=false` → dropped. |

`HASHTAGS = (optom, optomtoshkent, optomnarx, оптомташкент, оптомузбекистан, ulgurji, xitoydan, xitoydanoptom, xitoytovarlari, abusaxiy, abusahiy, chinnibozor, mebeloptom, qurilishmollari, santexnikaoptom, elektronikaoptom, avtozapchastioptom, gazlamaoptom, poyabzaloptom, kiyimoptom, oyinchoqoptom, konteyner, zavodnarxida, tovaryizkitaya, оптомтекстиль, оптоммебель, оптомсантехника, guangzhouoptom, yiwuoptom, 1688uz)` — 30 exactly; rotation is weekly, `recently_searched_hashtags` logged.

**Observation.** `kind="company"`, `source_ref=handle`, `url=https://www.instagram.com/{handle}/`, `instagram=handle`, phones/telegram/website/address from bio, `goods_text` = bio, `attrs={"followers","media_count","category_name","is_business"}`, `excerpt` = bio + the matched caption. **Signals**: bio marker → `wholesale_bulky` (only with a bulky category word in bio or captions); CHINA_WORDS → `china_origin`; `SUPPLY_RE`/`CONTAINER_RE` in captions («yangi konteyner keldi», «фура пришла», «партия») → `container_truck`; pack units (`karobka|коробка|qop|мешок|rulon|рулон|upakovka|упаковка`) → `wholesale_bulky`; `media_count ≥ 50` and (website or telegram) → `contact_handle`; followers alone are not a signal. Negatives: `B2C_RE`, «dostavka shahar bo'ylab/доставка по городу» → `b2c_marker`; `COMPETITOR_RE` → `competitor`; scam pattern `musodara|конфискат|таможенный конфискат` → excluded (`[v 44]`). Geography noise: bios naming Shymkent/Bishkek/Almaty without a UZ city → `hold`.

**Schedule.** Enrichment 11:00 daily (≤ 200), hashtags 12:00 daily (4), `apify_instagram` only on the Wednesday batch. **Yield.** Live sample 25 handles/18 queries (~10 UZ-confirmed with a wholesale cue) → 30–60 new business handles/week, ~30% with a phone or Telegram in bio. **Failure modes.** Token expiry (60-day long-lived token; refresh job monthly, problem `ig_token_expired` → fallback path), app review pending (hashtags off; Business Discovery still works for own-app testers — verify), handle renamed (404 → keep the old evidence, mark `gone`). **Legal.** Official API low; Apify medium (logged-out public business fields only; sole-proprietor +998 numbers are personal data under ZRU-547 → stored as the company contact with `legal_basis='public_source'`). **Day 1.** Create the FB app, link GSR's IG Business account, obtain `IG_GRAPH_TOKEN` and `IG_BUSINESS_ACCOUNT_ID` (two env vars to add in 06); read the current Graph API version from the app dashboard into `sources.config.graph_version`; one Business Discovery call on `optom_obuv_uzb`; one `ig_hashtag_search` for `optomtoshkent`; record both JSON fixtures.

### 2.5 `olx_business`

**Purpose.** Wholesale sellers on OLX.uz: business-flagged accounts with many ads in bulky categories and optom/konteyner/sklad wording; Telegram handles from ad text. Phones are **not** taken from OLX.

| Item | Rule |
|---|---|
| Robots | Day 1: read `https://www.olx.uz/robots.txt`. `docs/bepul-qidiruv-memo.md` reports `/api/` allowed (**UNVERIFIED**). If `/api/` is disallowed → HTML route; if both are disallowed → Apify only. |
| JSON route | `GET https://www.olx.uz/api/v1/offers/?offset=0&limit=40&category_id=<id>&query=<kw>&sort_by=created_at:desc` → `{data:[offer], metadata:{total_elements,visible_total_count}, links:{next}}`; offer: `id, url, title, description, created_time, last_refresh_time, business (bool), user{id,name,created}, category{id}, location{city{name},region{name},district}, params[], contact{name, phone: bool, chat: bool}` (field set per Apify actors' descriptions; **UNVERIFIED** against a live response). Max offset 1,000 per query → split by category × city. Category ids: resolve once from `GET /api/v1/categories/` (**UNVERIFIED** endpoint; else from the search-page `__PRERENDERED_STATE__`). |
| HTML route | `GET https://www.olx.uz/list/q-<kw>/?search[order]=created_at:desc` (and `/tashkent/q-<kw>/`, `/dom-i-sad/.../santehnika/tashkent/q-оптом/`); parse `window.__PRERENDERED_STATE__ = "<escaped JSON>"` → `listing.listing.ads[]`. Seller page `https://www.olx.uz/list/user/<id>/` → state `user` + ads count. |
| Apify fallback | `lowlanddata/olx-uz-scraper` ($1.99/1k; business/private flag, no personal data) with the same category URLs; `costs.ask('apify_olx')` ≤ $0.60/day (≈ 300 listings). |
| Keywords | RU `оптом, опт, партия, со склада, контейнер, фура, от производителя, мин. заказ, завод, по предзаказу из Китая`; UZ `optom, ulgurji, sklad, konteyner, fura, zavod, partiya, Xitoydan buyurtma`. Categories: стройматериалы/qurilish, мебель, запчасти, оборудование для бизнеса, сантехника, освещение, бытовая техника, ткани/текстиль, одежда, обувь, игрушки, хозтовары, посуда. |
| Seller aggregation | Key `olx_user_id`; per seller `attrs={"ads_count","wholesale_ads","business","registered":user.created,"categories":{id:count}}`; one `kind="company"` (`company_name_raw=user.name` — but when `user.name` is person-shaped by 04 §1.6's test («Sardor», «Aziz aka») the resolver sets `companies.display_name='OLX sotuvchi <olx_user_id>'` and the name becomes the one person-tier contact with `legal_basis='public_source'`, never a company name; `city`, `external_ids.olx_user_id`, `telegram` from `TG_RE` over the seller's ad texts) + one `kind="listing"` per matching ad (`excerpt` = title + first 400 chars, `published_at=created_time`). |
| Signals | `business=true` and wording (`оптом|партия|контейнер|со склада|от \d+ шт|optom|partiya|konteyner|sklad`) → `wholesale_bulky` (+ `container_truck` on контейнер/фура/konteyner/fura); ≥ 10 active ads in one bulky category → `marketplace_velocity`; «по предзаказу из Китая / Xitoydan buyurtma» → `china_origin`; `registered ≥ 1 year` → `entity_solid` (weak, 04 may halve); `COMPETITOR_RE` in any ad of the seller (`карго, выкуп, доставка из Китая, 1688 cargo`) → `competitor` (OLX «Xitoydan» searches are mostly forwarders, `[v 6]`); `business=false` and < 3 ads → `hold`. |
| Phone policy | Never call the reveal endpoint (`/api/v1/offers/{id}/limited-phones/` is logged-in and rate-limited; ToS). Contacts = `TG_RE` and digits written in the text; a seller without a phone is routed to enrichment (directories/maps by name) and is never pushed without one. |
| Rate / budget | 0.33 rps, page budget 400/day; daily 07:00 newest-first over the last 24 h per (category × keyword); full sweep Thursday 07:00. |
| Yield | 50–150 wholesale sellers/month before the competitor filter (`docs/arxitektura.md`); live sample 16 rows/12 queries, 7 appliance/auto resellers. |
| Failures | 403/CAPTCHA (breaker), state key renamed (parser returns `[]` → live test red), `metadata.total_elements` > 1,000 (split by city), seller page 410 (account gone → `gone`). |
| Legal | OLX rules forbid aggregation for transfer to third parties (`docs/huquqiy-memo.md`); we do not republish and keep company-tier fields; medium. |
| Day 1 | robots.txt; one `/api/v1/offers/` call; one search page with `__PRERENDERED_STATE__`; one seller page; record all three as fixtures; confirm the `business` field name. |

### 2.6 `directories`

**Purpose.** Company name + phone + address + rubric from the Uzbek business directories; the strongest phone source, a weak import signal (`directory_wholesale` 10) except where a tag says «из Китая». The schedulable unit is the parent `sources` row `directories` (family `directory`, 02 §2.1), which carries the cron, page budget and pause; the sub-source rows `directories_goldenpages`, `directories_yellowpages`, `directories_glotr`, `directories_prom` are what `evidence.source_key` carries for attribution (sites without a row of their own — top.uz, uzts, openinfo, uzpharm — attribute to `directories`, the site being named in `source_ref`); one parser module per site.

| Sub-source | Seed URLs (verified in `manbalar.json`) | List → card | Fields / notes |
|---|---|---|---|
| goldenpages.uz `[v 34]` | `/rubrics/?Id=1424` (Оптово-розничная торговля, 71 orgs), `?Id=1277`, `?Id=1558`, `?Id=106080` (Оборудование из Китая), `?Id=1029` (Торгово-закупочные базы), `?Id=1686` (стройматериалы; cards carry «импортер/экспортер» tags), `?Id=1800` (pharma), `/street/?Id=41003` (Abu-Sahiy market) | rubric page (+ pagination param **UNVERIFIED**, try `&page=N`) → `/company/?Id=N` | name, address, district, phones (**UNVERIFIED** whether rendered server-side; if JS-only, look for the XHR in page source; no headless browser in v1 → phone from other sources), email, site, hours, description, updated date (drop < 2024); «импортер» tag → `brand_distributor(official=False)`; «из Китая» rubric → `china_origin`. |
| yellowpages.uz `[v 23]` | `/rubrika/<slug>[/<city>]` primary (slugs from the rubric index matching `optov|wholesale` + target goods), `/teg/<slug>` supplement: `tovary-iz-kitaya`, `mebel-iz-kitaya`, `mebel-optom`, `optovaya-torgovlya-hoztovarami`, `optovaya-torgovlya-strojmaterialami`, `optovaya-torgovlya-ofisnymi-tovarami`, `optovaya-torgovlya-bumagoj`, `optovaya-prodazha`, `optovaya-torgovlya-v-uzbekistane` (verify 200); cities `/tashkent /samarkand /namangan /andijan /fergana /bukhara`; `/en/rubric/` mirror for slug cross-check | list → `/kompaniya/<slug>` | name, legal form, district+street, phones (sometimes only fax/e-mail), website, rubrics, tags, description; tags are self-applied → `/teg/tovary-iz-kitaya` and `mebel-iz-kitaya` give `china_origin`. |
| top.uz `[v 43]` | `/section/optovaya-torgovlya?PAGEN_1=N` (815, Tashkent), `/section/stroymaterialy-optom?PAGEN_1=N` (231), `/section/prodazha-stroitelnykh-materialov`, regional `/tashkent-obl` | Bitrix pagination → company card (phone masked `+998…` in list, shown on card) | name, phone, address, rubric; general directory (233k orgs) → `directory_wholesale` only for `*-optom` sections. |
| glotr.uz `[v 43]` | `/showcase/<slug-id>/` seller pages (richest: China Motors, Uzbek-Chinese Trading House, XITOY UPAKOVSHIK, TAO.UZ), categories `/ru/<cat>/` (oborudovanie, mebel, sanoat-uskunalari) | category → products grouped by seller → showcase | seller with ≥ 20 SKUs or manufacturer = Chinese brand → `marketplace_velocity`; «Soliq tasdiqlagan» badge → `entity_solid`; phone/Telegram on showcase. |
| prom.uz | `/tags/optom/`, company pages `/uz/company/<slug>/<cat>/`, product cards with «страна производства: Китай» | tag → product → company | `china_origin` only from the origin field; `/ads/dostavka-*` are competitors (skip). |
| uzts.uz `[v 14]` | `/en/view/allmembers/entry/?pagenum=1..N` (TIN \| name \| region; bypasses the 500-row cap), detail `/en/members/entry/{id}/` | paginate → detail | STIR, region, activity, capacity, address, phone (partial); drop STIR `123456789`/«test»; textile mills → equipment/dyes/accessories importers (`entity_solid`, sector tag; no volume signal). |
| openinfo.uz `[v 37]` | `/ru/facts/6/{id}` (major-transaction facts, ids up to ~94089 seen), flat `/ru/facts/{id}/`, `new.openinfo.uz` mirror | weekly: enumerate ids above the stored high-water mark (≤ 300/week) | issuer name, address, phone, counterparty + INN, subject, amount; counterparty matches `Co\.?,?\s*Ltd|КНР|Кита|China|Shenzhen|Guangzhou|Yiwu|Xuzhou|Ningbo|Zhejiang` → `china_origin` + `container_truck` (equipment lines are 20/40 ft), `entity_solid` (JSC); 30–120 CN-linked issuers/yr. |
| uzpharm `[v 0]` | `uzpharm-control.uz/pages/certified-pharmaceutical-manufacturers`, `uzpharm-gxp.uz/ru/registry1..N` | table rows | organisation, scope, issue/expiry; active or expired ≤ 24 months → manufacturer (lines, APIs, packaging from China); contacts via yellowpages `farmacevticheskie-preparaty-proizvodstvo` / goldenpages `Id=1800`; `entity_solid`. |

Common parse → `kind="company"`, `source_ref=f"{site}:{card_id_or_slug}"`, `legal_form` via `LEGAL_FORM_RE`, `phones` (all on the card; the normaliser flags shared numbers), `website`, `address`, `city`, `goods_text` = rubrics + description, `stir` where printed, `external_ids={"twogis_firm_id"…}` where present. Category filter: keep China-heavy goods (`BULKY_CATEGORIES`); food/metal/agri/stationery wholesale without a China cue → `hold`. Rate 0.5 rps max (slower if `Crawl-delay`), robots per host; page budget 600/day shared, one sub-source per night during the monthly full crawl (1st–7th, 23:00), weekly diff (first list page of each rubric) Thursday 23:00; uzts/openinfo/uzpharm monthly. **Yield.** Thousands of contact rows: top.uz ~1,050 cards in two sections, yellowpages hundreds, goldenpages low hundreds genuinely wholesale, uzts 1,400–2,100 STIR rows, pharma ~125. **Failures.** Layout change (fixture test red), phone behind JS (goldenpages), paginator loops (stop when a page repeats its first card id), 403 streaks. **Legal.** Medium (directory terms; company-tier; no republishing; internal use). **Day 1.** Fetch one list + one card per sub-source, confirm selectors and the goldenpages phone rendering, record fixtures; verify each yellowpages `/teg/` seed returns 200.

### 2.7 `hh_vacancies`

**Purpose.** Employers hiring ВЭД/закупки-Китай/declarant/Chinese-speaking staff are importers with in-house logistics needs (`ved_vacancy` 15). Discovery only; contacts always from enrichment.

| Item | Rule |
|---|---|
| API | `GET https://api.hh.ru/vacancies?text=<kw>&area=<UZ>&search_field=name&search_field=description&order_by=publication_time&per_page=100&page=N&date_from=<iso −6 h>` (`page × per_page ≤ 2000`); then `GET /vacancies/{id}` (description HTML, address, `contacts` if `show_contacts`, employer, `published_at`), `GET /employers/{id}` (`site_url, description, industries[], open_vacancies, area, type ∈ company|agency|private_recruiter|project_director`, cached 30 d). Headers: hh.ru asks for `User-Agent: $HH_USER_AGENT` (`clientScrapper/1.0 (email)`), but the Fetcher never replaces its own UA (§1.1; it carries the same e-mail) — which one hh.ru accepts is Open question 9, checked live in T-23; 403 → register an app at dev.hh.ru and send `Authorization: Bearer <app token>` (env `HH_APP_TOKEN`, to add if needed). `obey_robots=False` (documented API). |
| Areas | `GET /areas` once (cached 30 d); walk the tree for name «Узбекистан» → country id, children for Tashkent/regions. Ids 97 / 2759 from the proposal are **UNVERIFIED**; never hard-code. |
| Keywords | RU: `ВЭД, специалист по ВЭД, менеджер ВЭД, импорт, закупки Китай, закупщик, снабженец, менеджер по закупкам, китайского языка, переводчик китайского, декларант, таможенное оформление, логист ВЭД, Alibaba, 1688`; UZ: `tashqi savdo, Xitoy, xitoy tili, bojxona deklarant, ta'minot, xarid menejeri`; EN: `procurement, sourcing China, import manager, supply chain`. ≈ 26 queries per run. |
| Agency handling | `employer.type != "company"` or name matches `InHunter|HR|Recruit|Staff|Кадров` → poster is an agency: parse the real client with `(?:от имени|для компании|наш клиент|компания-клиент)\s*[—:-]?\s*(?:ООО|OOO|MChJ|«)?([^»\n,.]{3,60})`; none → `attrs.agency=True`, no company row. Poster and inferred employer are always stored separately (`[v 5]` saw cross-attributed postings). |
| Exclusions | `COMPETITOR_RE` in employer name/description or `industries[].name` matching `Перевозки|Логистика|Транспорт` → `competitor`; staffing firms, state bodies (`ГУП|Агентство|Министерство|Davlat|hokimiyat` → `state_body`), Chinese contractors (`Co., Ltd` + construction project text) → `hold`. |
| Observation | `kind="vacancy"` (`source_ref=f"hh:{vacancy_id}"`, `url=https://hh.uz/vacancy/{id}`, `excerpt` = title + the sentence with the keyword, `published_at`) + `kind="company"` (`source_ref=f"hh_employer:{id}"`, `company_name_raw`, `website=site_url`, `goods_text` = employer description, `external_ids.hh_employer_id`, `attrs={"open_vacancies","industries","trusted"}`); signals `ved_vacancy`, `china_origin` when CHINA_WORDS appear, `entity_solid` when `trusted`. HR phone/e-mail in `contacts` → `attrs.hr_contact` only (never a lead contact). |
| Schedule / rate | every 4 h at :20; ≤ 1 rps; ~26 list calls + ≤ 60 detail calls per run; page budget 400/day. |
| Yield | 10–30 new importer-employers/month countrywide (`[v 5]`). |
| Failures | 403 (UA/token → breaker + alert), `captcha_required` JSON, 400 on a bad area, agency noise, Moscow postings when `area` is omitted (fence: a test refuses a request without `area`). Fallback: `hh.uz/vacancies/<slug>` SEO pages daily (no dates; dedupe by vacancy id; robots). |
| Legal | Official API, company-tier; low–medium. Its fixtures are nevertheless scrubbed as a **person-tier source** (`scrub_fixture.PERSON_TIER_SOURCES`, 07 §1.1): a vacancy's `contacts` block names the HR person, with an e-mail and a split phone. |
| Day 1 | `GET /vacancies?text=ВЭД&per_page=1&area=<resolved>` with the UA (and the header variant of Open question 9); `GET /areas`; record `vacancies.json`, `vacancy.json`, `employer.json`, `areas.json` fixtures. |

### 2.8 `exhibitors`

**Purpose.** Uzbek exhibitors at trade shows (auto parts, building, textile machinery, furniture, power) import at container scale (`exhibitor` 15). Batch collector driven by a calendar.

| Show | List endpoint (`[v 15]`) | Next crawl window |
|---|---|---|
| Automechanika Tashkent (BMCA/Messe Frankfurt, CAEx) | `https://automechanika.uz/exhibitorslisteng` (Tilda; year variants exist for 2024/2025 — look for an embedded JSON/XHR) | 21–23 Oct 2026 → crawl 2–15 Nov 2026 |
| TransLogistica Uzbekistan | Iteca Bitrix pattern | 10–12 Nov 2026 → competitors only (seeds, not leads) |
| UzBuild | `https://uzbuild.uz/en/exhibitors-list`; archive `/en/exhibition/spisok-uchastnikov.php?PAGEN_2=N` (2022–2026) | 16–18 Mar 2027 |
| WoodTech & MebelExpo | `mebelexpo.uz` (pattern **UNVERIFIED**, assume Bitrix) | 27–29 Apr 2027 |
| Power Uzbekistan | `https://power-uzbekistan.uz/en/exhibitors-list` | May 2027 (2026 list online now) |
| TextileExpo | `textileexpo.uz` (pattern **UNVERIFIED**) | May 2027 (2026 list online now) |
| CAITME | `https://caitme.uz/en/exhibitors-list` | Sep 2027 (2026 list online now) |
| China–Central Asia Trade Expo (CCPIT), Chinese brands fair (CAEx) `[v 20]` | exhibitor/visitor lists **UNVERIFIED** | Sep–Oct yearly |

Strategy: `sources.config.calendar = [{show, url_patterns, end_date}]`; weekly Monday 09:00 job crawls every show with `end_date + 10 d ≤ today < end_date + 60 d`, and every archive once (yearly). Paginate `PAGEN_2=N` until a page repeats or is empty; parse table rows `company | pavilion | stand | country | sector`. Keep `country ∈ {Uzbekistan, Узбекистан, O'zbekiston}` or, when absent, `LEGAL_FORM_RE` UZ forms; foreign rows are not stored as companies (supply side). **Observation** `kind="exhibitor"`, `source_ref=f"{show}:{year}:{slug(company)}"`, `company_name_raw`, `goods_text=sector`, `attrs={"stand","pavilion","show","year"}`, signal `exhibitor`; brand names in the profile → `brand_distributor(official=False)`. Cross-check row counts against the organiser's figures (UzBuild 2026 ~418–450; Automechanika 2025 489; Power 2026 250; CAITME 2025 210; MebelExpo 2026 ~130) — a crawl under 50% of the figure is `partial` and alerts. Rate 1 req/3 s, ≤ 100 pages/run. **Yield** tens of UZ rows per show, 100–200/yr (72% of MebelExpo exhibitors are foreign). **Failures.** Tilda list only in JS (look for `t-store`/`data-*` JSON; else Brave `site:automechanika.uz` snippets), list removed after the show (archive URL), truncated pagination. **Legal.** Low (published participant lists; company-tier). **Day 1.** Fetch Automechanika 2026 and UzBuild archive page 1; record fixtures; confirm the `PAGEN_2` parameter.

### 2.9 `maps` — Google Places (New) and 2GIS

**Purpose.** Phone + website + address for wholesale-rubric businesses («оптовая база», «склад», «optom») around the markets; a contact layer with a weak `directory_wholesale` signal. Scheduled and budgeted under the parent `sources` row `maps` (family `maps`, 02 §2.1); evidence is attributed to the sub-source rows `maps_google` / `maps_2gis`.

**Google Places (New).** Two-step to stay inside the free caps (`docs/bepul-qidiruv-memo.md` #7; SKU mapping **UNVERIFIED** — the data-fields page lists phone/website under the Enterprise/Advanced tier):
1. `POST https://places.googleapis.com/v1/places:searchText`, headers `X-Goog-Api-Key: $GOOGLE_PLACES_API_KEY`, `X-Goog-FieldMask: places.id,nextPageToken` (IDs-only SKU, $0), body `{"textQuery": q, "languageCode":"ru", "regionCode":"UZ", "pageSize":20, "locationBias":{"circle":{"center":{"latitude":41.2995,"longitude":69.2401},"radius":25000}}}`; ≤ 3 pages per query. Queries: `"{category_ru} оптом {city}"`, `"{category_uz} optom {city}"`, `"оптовая база {city}"`, `"склад {category_ru} {city}"` for cities Ташкент, Самарканд, Андижан, Фергана, Наманган, Бухара → ≈ 150 queries/week, `costs.ask('places_text_ids')`.
2. For each **new** `place_id`: `GET https://places.googleapis.com/v1/places/{place_id}` with `X-Goog-FieldMask: id,displayName,formattedAddress,types,businessStatus,nationalPhoneNumber,internationalPhoneNumber,websiteUri` → `costs.ask('places_details_contact')`: first 1,000/month at $0, then $0.020 (**UNVERIFIED**: Place Details Enterprise $20/1k with 1,000 free; confirm on the pricing page and in the Cloud Console SKU report the day after the first call). ≤ 33/day inside the free cap, ≤ 50 more/day under the $1 cap.

Storage per Maps terms: `place_id` is kept for ever (`external_ids.place_id`); name/address/phone/website from Places are evidence whose `expires_at = fetched_at + 30 d` (02 §2.3; purged by `retention_job`, 04/06) and are copied to `contacts` only when the candidate reaches a card or push inside those 30 days, with `source_url=https://www.google.com/maps/place/?q=place_id:{id}`; a copied phone does not outlive the terms either — retention step R4b (02 §4) clears `contacts.phone` where `source_key='maps_google'`, `collected_at < now() − 30 d` and the company has no `push_log` row `sent|unknown`. No lat/lng stored. Signals: `types` containing `wholesaler` (**UNVERIFIED** type name) or the name matching B2B markers → `directory_wholesale`; address inside a market area (`Абу Сахий|Abu Saxiy|Куйлюк|Qo'yliq|Янгиобод|Малика|Сергели|Урикзор|Чорсу|Бек Барака`) → `wholesale_bulky`; `businessStatus != OPERATIONAL` → `liquidated`-like negative (`closed`).

**2GIS.** Demo key first: `GET https://catalog.api.2gis.com/3.0/items?q=<rubric>&region_id=<tashkent>&type=branch&fields=items.contact_groups,items.address,items.rubrics,items.org&page_size=10&page=N&key=<TWOGIS_API_KEY>` (demo: created once, valid 1 month, 1,000 requests per service; `region_id` via `GET https://catalog.api.2gis.com/2.0/region/search?q=Tashkent&key=` — **UNVERIFIED**). Rubric texts: `Стройматериалы оптом, Цемент оптом, Сантехника оптом, Электротовары оптом, Автозапчасти оптом, Мебель оптом, Ткани оптом, Бытовая техника оптом, Хозтовары оптом, ulgurji, qurilish mollari`; building-level pulls for firm ids `70000001057601648` (Abu Saxiy Center), `70000001038443805` (Абу Сахий рынок), `70000001043300380` (`[v 11]`; parameter for "firms in building" **UNVERIFIED**). Parse `items[].{id,name,org.name,address_name,rubrics[].name,contact_groups[].contacts[]{type,value}}` (phone, website, telegram, instagram). After the demo month: `2gis.uz` HTML is a SPA (**UNVERIFIED** server rendering) → do not crawl; fallback `searchapi/2gis-scraper` on Apify ($1.99/1k, `costs.ask('apify_2gis')` ≤ $0.40/day) for the same rubrics, monthly; in parallel the owner asks 2GIS Uzbekistan for a licensed export. Observation `kind="place"`, `source_ref=f"2gis:{item.id}"`, `external_ids.twogis_firm_id`, phones/website/address/rubrics; signal `directory_wholesale` when a rubric contains `оптом|оптовая база|склад|ulgurji`.

**Schedule.** Places Tuesday 10:00 (text) and daily 10:30 (≤ 33 details); 2GIS daily 10:40 (40 req) during the demo month, then monthly Apify. **Yield.** Hundreds of wholesale-rubric businesses with phones in month 1 (UNVERIFIED). **Failures.** Places 403 (billing/key), SKU surprise (watch the console; the cap stops the bill), 2GIS key suspended at quota (expected; switch to fallback), empty `contact_groups` on UZ records. **Legal.** Places terms on caching (above); 2GIS API within key terms, Apify grey → bounded pilot. **Day 1.** One text search with IDs-only mask; one details call; next day read the SKU in the console; one 2GIS demo request; record fixtures.

### 2.10 `brand_dealers`

**Purpose.** Official distributors/dealers of Chinese brands in Uzbekistan — the live sample's strongest segment (Soventus/Jinko, Runking/Changan, XCMG, Zoomlion, Foton/Howo) — container-scale importers with public company contacts (`brand_distributor`).

`BRANDS` (`collectors/brands.py`, ~60, grouped): HVAC/appliances `Gree, Haier, Midea, Hisense, TCL, AUX, Chigo, Hofmann, Welkin`; solar/power `Huawei FusionSolar, Jinko, LONGi, JA Solar, Trina, Sungrow, Growatt, Deye, BYD Battery, CATL, Chint, Delixi`; vehicles `Chery, BYD, Changan, Geely, Haval, Great Wall, JAC, Jetour, Exeed, Omoda, Dongfeng, FAW, Foton, Sinotruk HOWO, Shacman, Yutong, King Long`; machinery `Sany, XCMG, Zoomlion, LiuGong, Lonking, SDLG, Shantui, Weichai, Haitian, Yizumi, Bodor, Jack (sewing), Zoje`; tyres `Linglong, Sailun, Triangle, Doublestar, Aeolus`; electronics `Xiaomi, OPPO, vivo, realme, Honor, ZTE, Lenovo, Hikvision, Dahua, TP-Link, Tenda`; plumbing/lighting `Frap, Ledeme, Zerix, Opple, Yeelight`. Each entry carries `brand_domains` for `site:` queries where known.

Queries per brand (via `SerpClient`, 3 per brand; 60 brands → 180 queries/month ≈ 6/day on Brave/Yandex): RU `"{brand}" официальный дистрибьютор Узбекистан`, `"{brand}" официальный дилер Ташкент`; UZ `"{brand}" rasmiy distribyutor O'zbekiston`; EN `"{brand}" authorized distributor Uzbekistan`; plus `site:{brand_domain} (Uzbekistan OR Узбекистан OR Tashkent)` for brands with a dealer locator, and the ENF Solar directory for solar (`enfsolar.com` sellers/installers in Uzbekistan, `[namuna §1]`).

Handling: `company_site` hits → `parse_company_site` (§2.1); the evidence excerpt must contain the brand name within 200 chars of `официальн\w+\s+(?:дистрибьютор|дилер|представитель)|rasmiy\s+(?:distribyutor|diler|vakil)|authorized\s+(?:distributor|dealer)|exclusive` → `brand_distributor(attrs.official=True, attrs.brand)`; a dealer page naming the company without «официальный» → `official=False`. `news` hits: regex `([А-ЯA-Z][^.«»\n]{2,60})\s+(?:стал[аи]?|является|назначен[аы]?)\s+официальным\s+(?:дистрибьютором|дилером)` → `kind="serp_hit"` with `company_name_raw` for enrichment. Brand's own global pages, marketplaces (uzum/olcha/asaxiy/texnomart/mediapark — retailers) → `hold`; `COMPETITOR_RE` → excluded; hits in KZ/KG only → `hold`.

**Schedule** monthly 1st–10th at 13:00 (6 queries/day), distributor pages re-fetched quarterly (conditional GET). **Yield** 30–80 distributor companies in month 1, then a trickle (new appointments via `news`). **Failures.** Engine ignores quotes (post-filter: brand word must be in title/snippet), distributor lists on image-only pages. **Legal.** Low (companies publish their distributor status). **Day 1.** Run the solar and vehicle groups first; confirm ≥ 10 `official=True` pages; record 3 company-site fixtures.

### 2.11 `uzum_sellers` — phase 2

**Purpose.** Marketplace sellers with high order velocity in bulky China-import categories (`marketplace_velocity`); the shop page's legal block gives the entity.

| Item | Rule |
|---|---|
| Catalog | Apify `getascraper/uzum-scraper` ($3.30/1k product records; `[v 1]`) with 40–80 bulky keywords (`mebel, divan, shkaf, kovyor, stiralnaya mashina, velosiped, avtozapchasti, santehnika, svetilnik, igrushki, trenajyor, …`) weekly Friday 09:00, `costs.ask('apify_uzum')` ≤ $0.60/day (≈ 180 records/day, ~5,400/month). Output `product_id, title, category, price, order_count, review_count, rating`; the **seller field is unconfirmed** — phase-2 day 1: run 50 rows; if no seller, use the product JSON (`api.uzum.uz` SPA endpoint, **UNVERIFIED**) through `PROXY_URL` at 1 req/5 s, or parse.bot `get_product_details` (price unknown). |
| Shop page | `https://uzum.uz/ru/shop/<slug>` via `PROXY_URL` (UZ/KZ residential, `costs.ask('proxy_uzum')` ≤ $1/day), 1 req/5 s, monthly, top 300 shops by bulky-order velocity. Oferta 5.3 mandates full name, state registration number, address on the shop page (`[v 17]`) — **UNVERIFIED** that pages render it: hand-check 20–30 shops before building. Parse `LEGAL_FORM_RE` sentence, reg number `\b\d{9,13}\b` after `регистрац|ro'yxat`, address; `самозанят|o'zini o'zi band` → excluded (retail only by law). |
| Aggregation | per `uzum_shop_slug`: SKU count per category, Σ `order_count`, 30-day delta (two weekly snapshots); `marketplace_velocity` when ≥ 15 SKUs in one bulky category **and** Δorders ≥ 300/30 d (initial threshold; 04 recalibrates from outcomes); `entity_solid` when the legal block parses; Uzum's own brands → excluded. |
| Observation | `kind="company"` (`source_ref=f"uzum:{slug}"`, `company_name_raw`=legal name or shop name, `external_ids.uzum_shop_slug`, `attrs={"sku_count","orders_30d","categories"}`) + `kind="listing"` for the top 3 products as evidence. No phone on Uzum → enrichment by legal name (directories, registry in 04). |
| Legal | Uzum terms medium; the legal block is a statutory disclosure (company-tier). |
| Yield | 2–3k sellers scanned → 100–300 bulky high-velocity shops (`docs/arxitektura.md`; UNVERIFIED). |

### 2.12 `tenders` — phase 2

**Purpose.** Entity solidity and goods categories for companies already in the DB with a STIR: `uztender.com/supplier/{INN}` shows contract count, total sum, customers, main categories (`[v 3]`). Enrichment, not discovery.

Weekly Saturday 04:00: for companies with `stir` and no `tenders` evidence in 90 days (≤ 150/week), `GET https://uztender.com/supplier/{INN}` at 1 req/3 s (robots **UNVERIFIED**); parse name, INN, `contracts_count`, `total_sum`, `customers_count`, categories. Signals: goods categories ∈ (equipment, spare parts, electronics, furniture, textiles, medical) with ≥ 3 contracts → `entity_solid`; works/services only → none; presence on `xarid.uzex.uz` unreliable-supplier list (`[v 4]`, endpoint **UNVERIFIED**) → `−` (04). `/market` category stats are read once a quarter for the category list. Legal medium (commercial aggregator; public pages, no export, internal use). Observation `kind="company"`, `source_ref=f"uztender:{INN}"`, `attrs={"contracts","sum","customers","categories"}`.

---

## 3. Schedule, budgets and heartbeats

| key | cron (Asia/Tashkent) | rps | pages/day | $/day | heartbeat(s) | phase |
|---|---|---|---|---|---|---|
| erp_sync | `15 * * * *` delta · `30 2 * * *` full · `0 3 * * *` outcomes (cursor + recent) · `20 3 * * 0` quarter · `40 3 1 * *` year | 1.0 | 50 | 0 | `collector:erp_sync.{delta,full,outcomes}` | 1 |
| telegram_monitor | wake on `NOTIFY cs_interaction` · `*/15 * * * *` fallback sweep · join daily (random 10–20 h) · `0 1 * * *` backfill | — | — | Claude only | `userbot`, `collector:telegram_monitor{,.join,.backfill}` | 1 |
| serp_layer | `0 10,14 * * *` · `10 2 * * *` night | per engine | 300 | ≤ 1.00 | `collector:serp_layer{,.night}` | 1 |
| instagram_discovery | `0 11 * * *` enrich · `0 12 * * *` hashtags | 1.0 (Graph) | 100 (linktree hops) | ≤ 0.40 | `collector:instagram_discovery` | 1 |
| olx_business | `0 7 * * *` · `0 7 * * 4` full | 0.33 | 400 | ≤ 0.60 | `collector:olx_business` | 1 |
| directories | `0 23 * * 4` diff · `0 23 1-7 * *` full | 0.5 | 600 | 0 | `collector:directories` (detail names the sub-source) | 1 |
| hh_vacancies | `20 */4 * * *` | 1.0 | 400 | 0 | `collector:hh_vacancies` | 1 |
| exhibitors | `0 9 * * 1` | 0.33 | 100 | 0 | `collector:exhibitors` | 1 |
| maps | `0 10 * * 2` text · `30 10 * * *` details · `40 10 * * *` 2GIS | 2.0 / 1.0 | 150 | ≤ 1.40 | `collector:maps` | 1 |
| brand_dealers | `0 13 1-10 * *` | per engine | 120 | inside serp | `collector:brand_dealers` | 1 |
| uzum_sellers | `0 9 * * 5` · shop pages `0 4 15 * *` | 0.2 (proxy) | 300 | ≤ 1.60 | `collector:uzum_sellers` | 2 |
| tenders | `0 4 * * 6` | 0.33 | 150 | 0 | `collector:tenders` | 2 — no `sources` row yet (02 §5: a data migration adds it with the collector) |

`key` is the `sources.key` the runner schedules under — for `serp_layer`, `directories` and `maps` the parent row of 02 §2.1, with the engine/site rows underneath for attribution only. `sources` rows carry `cron`, `rps`, `daily_page_budget`, `daily_cost_cap_usd` (the `$/day` column above — a column, not a `config` key) and `config jsonb` (seeds, calendar, cursors, `graph_version`; api keys never) and are editable from the owner bot (`/manbalar`), which is how a cadence is changed without a deploy.

---

## Open questions

1. ~~**Header name.**~~ Resolved by T-07 (2026-10-24): the ERP reads `X-GSR-Key` only (05 §A.2); the agent keeps sending both with one value, and `X-ERP-Key` is ignored.
2. **Places storage.** Maps terms allow caching `place_id` only; the lead's phone from Places is pushed to the ERP (a permanent store). Is "use within 30 days, then re-fetch on demand" acceptable to the owner's lawyer (`docs/huquqiy-memo.md` open item), or should Places be IDs-only and phones come from 2GIS/directories?
3. **Env additions.** `IG_GRAPH_TOKEN`, `IG_BUSINESS_ACCOUNT_ID`, `TWOGIS_API_KEY`, optional `HH_APP_TOKEN` are not in the shared env list — 06 to add them to `Settings` and `.env.example`.
4. **Schema touches for 02 — resolved.** `evidence.meta jsonb NOT NULL DEFAULT '{}'`, `chat_monitors.{status,priority,backfilled_at}` (`seen_count` was already there), `interactions.fwd_from_peer_id` and `sources.cron` are in `0001` (02 §5); the `collector_runs` extras (`unchanged`, `skipped_robots`, `note`) live in `collector_runs.meta` and the status set stays 02's (`blocked` → `failed` + `error='host_blocked'`); nothing is requested on `suppression` — its shape is 02 §2.8 / 05 §B.2.
5. **Health keys.** `collector_silent`, `suppression_stale`, `ig_token_expired` are proposed in addition to the six in the shared contract — confirm in 06.
6. **Telegram buyer groups.** Does the owner accept joining the large bazaar groups («CHINNiLAR OPTOM TOSHKENT», 165k members) with the company account, given the content-licensing risk, or only competitor discussion groups?
7. **Goldenpages phones.** If phones are JS-rendered, is a headless browser (Playwright) acceptable in v1, or do goldenpages cards stay name/address-only?
8. **Uzum phase 2 trigger.** What precision from phase-1 sources (30-day outcomes) unlocks the Apify + proxy spend for Uzum?
9. **hh.ru's User-Agent (T-23).** hh.ru wants its registered `HH_USER_AGENT` as the UA; the Fetcher never replaces `USER_AGENT` (which carries the same e-mail) and refuses a `User-Agent` header. Day 1: does hh.ru accept our UA? If not: (a) send `HH-User-Agent: $HH_USER_AGENT`, the header hh.ru documents for clients that cannot set the UA — recommended, keeps the one-UA rule (UNVERIFIED until the live call); (b) let `hh_vacancies` replace the UA for `api.hh.ru` only.
10. **Breaker scope.** A breaker trip pauses the run's `sources` row — for `directories` the whole collector, though one site (`goldenpages.uz`) tripped it. Keep (simple, the safe direction), or pause only the sub-site and let the run carry on with the others (needs a per-sub-source pause the runner reads)?
11. **Brave's 33rd query.** `ALLOCATIONS["serp_brave"] = 0.165` gives 32 queries/day under 04 §8's ≥ rule. Move 0.005 from `serp_reserve` (0.170 / 0.830, Σ unchanged) to get the 33 this file planned, or accept 32 (≈ 990/month, still under the credit)? Recommended: accept 32.
12. **SearXNG and robots.txt (T-25).** The stock SearXNG image's robots.txt may disallow `/search?q=`, which our own Fetcher obeys. Decide: (a) ship our own `robots.txt` in the image override (allow `/search` to `clientScrapperBot` only) — recommended; (b) add `searxng` to `ROBOTS_OPTOUT_HOSTS` (it is our own container, but the list means «documented API with a key»).

## Acceptance criteria

- `services/http.py` passes `tests/collectors/test_http.py`: per-host pacing, robots disallow, conditional GET/304, `Retry-After`, breaker at 5 consecutive 403/429 writing `sources.paused_until` and raising `collector_blocked`, `BudgetExhausted`, UA on every request, secret scrubbing in logs, `obey_robots=False` only at allow-listed call sites.
- Every phase-1 collector has: a `Collector` subclass with the names in §3; pure `parse_*` functions with ≥ 1 recorded fixture each and a parse test that asserts concrete fields; a `@live` smoke test; a `collector_runs` row per run with counters (extras in `meta`); a heartbeat under `collector:<key>`.
- `collectors/keywords.py` regex tests pass for Latin, Cyrillic Uzbek and Russian examples, including the «оптом»/«оптика» and «kargo» cases.
- No collector calls a paid endpoint outside `costs.ask`; a run that would exceed its `$/day` or `pages/day` ends `partial`, never over budget (test with a fake price table).
- `erp_sync`: salt change → full resync; stale feed (`suppression_fresh()` false: full pull > 26 h or delta > 3 h) → `suppression_stale` and the push gate refuses; XLSX import stores clear `phone_tail` rows with `source='manual_import'`, retired once a full pull holds the hash of the same tail (05 §B.2 step 5; test: import, then a fake full pull carrying that tail's hash → the `manual_import` row is gone).
- `telegram_monitor`: `tests/test_no_telegram_writes.py` green; join limited to 3/day (test with a fake clock); non-matching messages are not stored; the Haiku call receives no raw phone or handle (test on the pseudonymiser output).
- `instagram_discovery`: personal/`not_business` handles are never stored; hashtag quota tracker never exceeds 30 unique tags per 7 days (test).
- `olx_business`: no code path calls the phone-reveal endpoint (AST test on the URL constant); sellers with `COMPETITOR_RE` hits are emitted with `competitor` only.
- `maps`: Places evidence rows carry `expires_at = fetched_at + 30 d` (not the default 180 d); only `place_id` is in `external_ids`; details are requested at most once per `place_id`.
- Day-1 checklist (every **UNVERIFIED** item above) is executed from the VPS and its result written into `tests/fixtures/<key>/README.md` with the date, before the collector is enabled in `sources`.

## Review pass changelog

Items of `REVIEW-2026-10-23.md` applied to this file (numbers are the reviewer's):

- **1** — §1.2 runner, §2.1, §2.6, §2.9, §3: the schedulable key is a `sources` row; `serp_layer` / `directories` / `maps` are the parent rows (02 §2.1), the engine/site rows (`search_*`, `directories_*`, `maps_*`) remain for `evidence.source_key` attribution; DDG and the directory sites without a row attribute to the parent.
- **2** — §2.2 Storage / Suppression-full rows now point to 02 §2.8 / 05 §B.2 steps 2–5 (`kind client|lost`, `key_type='phone_hash'`, `source='erp_feed'`, `last_seen_at`, unseen rows deleted); Open Q4 no longer asks for `suppression.{origin,removed_at}`.
- **3** — §2.2 XLSX fallback and the `erp_sync` acceptance criterion: clear `phone_tail` rows, `source='manual_import'`, retired by the first covering full pull (05 §B.2 step 5, §B.3); no hashing with `CRM_AGENT_KEY`.
- **4** — §1.1 evidence line (`meta jsonb NOT NULL DEFAULT '{}'` with `etag, last_modified, final_url, status, truncated`), §2.3 (`interactions.fwd_from_peer_id`), Open Q4 marked resolved against `0001`.
- **7** — §2.2 freshness gate: `services/erp_feeds.suppression_fresh()` with `FULL_MAX_AGE_H = 26`, `DELTA_MAX_AGE_H = 3` is the one home; full pull cron stays `30 2 * * *`; acceptance criterion restated with the two numbers.
- **8** — §2.3 purpose, schedule line and §3 row: the Telegram path is event-driven (`NOTIFY cs_interaction` → `classify_job`/`score_job` wake; `review_cards_job` every 2 min inside `REVIEW_HOURS`); the 15-min sweep is the fallback.
- **10** — §2.3 join/discovery and tout filter: E3 reads the public `t.me/s/<username>` page through `Fetcher`; every Telethon read (`GetFullChannelRequest`, `GetFullUserRequest`, incl. the sender bio into `interactions.sender_bio`) is `userbot/about.py` draining `chat_monitors(status='queued', priority 3)`; no `telethon` import outside `userbot/`.
- **12 (b)** — §2.5 seller aggregation: a person-shaped OLX `user.name` → `display_name='OLX sotuvchi <olx_user_id>'` + one person-tier contact with `legal_basis='public_source'`. **12 (d)** — §2.9 storage: retention step R4b (02 §4) clears a copied Places phone after 30 d when the company has no `push_log` sent|unknown.
- **15** — §1.2 `Budget.usd = sources.daily_cost_cap_usd − spend(source_key)`; §1.3 uses 04 §8's `costs.ask(session, *, operation, est_usd, call, price, now)` and states the order `Budget.can_spend` → `costs.ask` (month → day → family → op); §3 footer: `$/day` is the column `daily_cost_cap_usd`, not `config.daily_usd_cap`.
- **17** — heartbeat component names `collector:<key>[.variant]` in §1.2 (class attribute, runner, variants), §3 table and the acceptance criteria.
- **18** — §2.2 Errors / Storage name `feed_contract` as the health key (05's `feed_bad_request` / `feed_rule_changed` / `feed_runaway` fold into it, 06 §4.2).
- **20** — §2.2 Suppression-full/delta/Outcomes rows: `limit=1000`, `since = last_delta_at − 10 min`, `outcomes` PK `external_id` (no `push_log.outcome`), cursor in `sources.config.outcomes.cursor`, labels via `services.outcomes.derive_label` + 02 §2.11 side effects; fixtures `tests/fixtures/erp/` captured from `agent-feed` (07 Q3).
- **23** — §2.3 prefilter and observations: the weekly channel `about` is an `evidence` row + an observation (`kind="company"` in the contract, `parsed.kind='profile'` in 02 §2.5), not `interactions(kind='about')`. The 7-day tout window in §2.3 is unchanged (02 adopts it).
- **24** — no change: §2.3 already says the reader account is aged ≥ 30 days (06 adopts it).
- **29** — §1.4 and §2.3: `services/pseudonyms.py` (`Pseudonymised`, `pseudonymise`, `restore`, `assert_masked`) imports `PHONE_RE`/`TG_RE`/`IG_RE` from `collectors/keywords.py`.
- **30** — §1.3: a `costs.ask` refusal is persisted as a `usage_log` row (`cost_usd=0, units=0, model='refused:<code>'`).
- **31** — §1.3 table and §2.1 Brave row / schedule line: `serp_brave` 33 queries/day (`ALLOCATIONS["serp_brave"] = 0.165`) plus a monthly counter ≤ 1,000 inside `costs.ask`.
- **32** — §2.2 Outcomes row: `won` → `companies.state='client'` **and** a local `suppression(client, phone_tail, source='outcomes')` row (05 §B.2 table).
- **35** — no change: §2.1 SearXNG engines `duckduckgo,startpage,mojeek,brave`, Google/Bing disabled (06's compose comment adopts it).
- **36** — §2.4: the Graph API version is `sources.config.graph_version`, marked **UNVERIFIED** with a day-1 check (read it from the app dashboard; `v21.0` dated Oct 2024).
- **37** — §1.1 robots/conditional-fetch rows, §1.2 runner, Open Q4: `collector_runs` extras (`unchanged`, `skipped_robots`, `note`) live in `collector_runs.meta`; a breaker trip is `status='failed', error='host_blocked'` (02's CHECK has no `blocked`).
- **38** — §2.3 seeds: `@gsrgroupchat` is `chat_monitors.role='calibration', monitor_enabled=false` — nothing is read or stored from it.

Items 5, 6, 9, 11, 12 (a)(c), 13, 14, 16, 19, 21, 22, 25–28, 33, 34 do not touch this file.

**T-07 as built (2026-10-24)** — fullprompt `agent-feed` shipped at `23fe1736` (05 Part A); §2.2 now follows it: Auth row VERIFIED (`X-GSR-Key` only; feeds not called during the fallback); full pull `limit=5000` with neither `since` nor `cursor`, ≥ 90 % against the previous full pull; delta anchored on the ERP's `generated_at`; Outcomes row = the four walks of 05 §B.2 (nightly cursor + 8 d, weekly 90 d, monthly whole year) instead of «`since` only on the very first run», foreign rows ignored, Open question 1 resolved, effects recomputed with retractions; Errors row: 503 is retried and never `push_404`, 400 is `feed_contract`; fixtures list points to 07 §1.0; §3 schedule row gains the weekly and monthly walks.

**T-07 verification (2026-10-24)** — §2.2 checked against fullprompt `agent-feed` at `23fe1736`: Errors row gains the 3xx case (never followed, breaker `erp_redirect`, 05 §B.1); the XLSX row now derives tails by the ERP's own split and chunk rules (`PHONE_SPLIT_PG` also splits on CR, tab runs and spaces/tabs before `+`; chunks only inside parts of 16–120 digits) instead of «`, ; /` or newline», and states that during the fallback the XLSX tails are the only guard for a multi-code client (the «Boshqa» door's client step needs exactly one active card, 05 §A.3).

**T-07 third review (2026-10-24)** — §2.2 XLSX row re-checked against `tailsSql` (`agent-feed.ts`) at fullprompt `agent-feed` `a4f9f433`: the 12-digit chunks come only from a part made entirely of `998…` numbers and the 9-digit chunks only from a part whose length is a multiple of nine (not a `998` run); any other part of 16–120 digits — a 20-digit one — yields its last nine only. The old wording («every whole 9-digit number» otherwise) would have stored `manual_import` chunks the feed never carries, so they would never retire; T-11 pins a 20-digit case (07).

**Wave 1 as built (2026-10-24)** — folded from the wave-1 build notes and the seam review, checked against `services/http.py`, `services/robots.py`, `services/costs.py`, `collectors/base.py`, `collectors/runner.py`, `collectors/keywords.py` and `tools/record_fixture.py` at `f8c3303`; §2.2 against fullprompt `agent-feed` `d138b3bc` (the verify:final mismatches):

- §1.1 the Fetcher as built: `FetchResult`'s fields, the exception set, `PaidCall` (whose `price` returns `usage.Priced`) and `CostGate` (with `now`), `bind`/`paid`/`proxy`; robots.txt read by RFC 9309 in `services/robots.py` (`urllib.robotparser` breaks it), cached per authority, an unreachable robots.txt, a 5xx or a 429 → allow for 1 h; the UA rule; logs carry a `url_id`, never a URL; the 5 MB fetch cap beside 02's 2 MB evidence cap; the breaker writes both `paused_until` and `breaker_until` on the parent row.
- §1.2: the heartbeat is derived and a skipped run does not beat; the default gate is `runner.ask_gate(session, run_id)` (None disables paid calls), only `CostCapReached` is a budget stop, `UnattributedCall` refuses a call outside the run's row and its sub-sources, meta `exhausted`/`refused`/`dropped`, `ModelUnavailable` ends the run partial with `found` kept. §1.3 the rung codes, `SUBSOURCE_PREFIXES`, unit caps; `serp_brave` 0.165 buys 32 queries under 04 §8's ≥ rule (two places in §2.1 follow).
- §1.4: `keywords.py` is the source of truth and its departures are listed — `terms_re` (inflection-aware whole words: «оптом» vs «оптика», «Guangzhoudan»), Cyrillic twins on `SUPPLY_RE`/`COMPETITOR_RE`/`INTENT_RE`, `EMAIL_RE`, five `CAPTCHA_MARKERS` (`cf-chl`), `GEO_CITY` + `GEO_TASHKENT_PLACES` = `GEO` (the «Malika» false positive).
- §1.5 the recorder's `--paid`; §2.7 hh.ru's UA and the person-tier fixtures; §2.1 the SearXNG robots note; §3 the `tenders` row.
- §2.2 (verify:final): the internal code is `feed_contract` with code `rule_changed`; a feed 401/403 is read as `push_404`, any other feed 4xx is `feed_contract` `bad_request` (05 §B.2); the XLSX fallback's shape (headers Code/Name/Manager/Phones/Active, ≤ 5,000 rows, `clients.manage`, inactive cards included, the export carries the screen's filters and saved view so it is taken from an unfiltered screen, the cell is the array joined by «, », tails derived per element).
- Open questions 9–12 added: hh.ru's UA, the breaker's scope, Brave 32 vs 33, SearXNG's robots.txt.
