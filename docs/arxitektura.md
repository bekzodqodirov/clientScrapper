# clientScrapper — end-to-end design (GSR Logistics client-finding agent)

Decisions taken up front (everything below follows from them):

1. **Copy MIYA's skeleton, never import it** (config/db/logs/retry/extraction/usage/costs/health/backup/sheets/userbot/sender/outbox verbatim; `people.py` becomes `companies.py`). Python 3.12, 4 processes (`api`, `bot`, `worker`, `userbot`) + PG16, one VPS.
2. **Claude never scores and never sends.** Claude produces typed features (what they import, volume with a quoted evidence string, role of the speaker, decision maker); a deterministic Python scorer turns features into 0–100 with a stored breakdown; the only ways out are the ERP webhook (phone leads) and an owner-approved outbox (Telegram-only leads).
3. **Phone is the lead currency.** The ERP identifies by last-9-digits; a prospect without a phone is not pushed, it is enriched or handled in the Telegram path.
4. **Handoff = one new source key `agent` in LogisticsERP** (one constant + one `lead_sources` row + one secret); fall back to `other` + `utm_source=clientscrapper` until that ships.
5. **Feedback = read the ERP, don't ask managers to do anything new.** `lead_intakes.outcome/contacted_at`, `leads.stage_id→lead_stages.kind`, `leads.lost_reason`, `leads.client_id`, `leads.quoted_volume_m3` already record everything the scorer needs. Add four standard lost reasons.
6. **MVP sources: (a) ERP's own data, (b) Telegram group intent monitor, (c) hh.uz vacancy monitor**, plus a one-off exhibitor-list batch (Automechanika Tashkent closes 23 Oct 2026 — crawl it next week). Paid trade data only after a sample passes an acceptance test (month 2).

---

## 1. ICP as executable rules

### 1.1 Hard gates (evaluated before any scoring; a failed gate = `lead_candidates.state='excluded'` with `exclude_reason`)

| Gate | Rule (code-level) | Why |
|---|---|---|
| `is_b2b` | legal form present (`MChJ|OOO|ООО|LLC|QK|AJ|XK|ЧП|YaTT|ИП` or STIR 9 digits with valid checksum) **or** Claude `RelevanceVerdict.b2b=true` with `confidence>=0.7` **or** ≥2 volume signals ≥3 m³ in 90 d | B2C out of scope |
| `not_competitor` | name/bio/text matches `kargo|карго|cargo|express|logist|логист|freight|forwarder|ekspeditor|экспедитор|bojxona broker|таможенный брокер|dostavka Xitoydan` **and** no buyer-side signal → exclude, tag `competitor` | they are supply side; also a legal/PR risk to contact |
| `not_supplier` | exhibitor/sender with country=CN, Chinese company name, or 1688/Alibaba seller profile → exclude, tag `partner_cn` | supply side |
| `not_client` | phone tail-9 or STIR or GS client code in `suppression(kind='client')` → `suppressed` | ERP would route it to the client's manager, not as a lead; also burns the 3/phone/day cap |
| `not_dnc` | in `suppression(kind='dnc'|'legal'|'bounced')` | opt-outs, "yozmang", blocked us |
| `not_recent` | `push_log` has a `200` for this company in last 30 d (ERP `INBOUND_JOIN_DAYS`) or any push for this phone in last 24 h | ERP joins/caps anyway; avoid noise |
| `active_entity` | registry status ≠ liquidated/inactive (when known) | dead companies |

### 1.2 Positive signals (points; `scoring_weights` table, version 1)

Score = `min(100, Σ positive) − Σ negative`, floored at 0. Each signal is a row in `signals` with `evidence_id`, `observed_at`, `value`, so the breakdown is always reproducible.

| Group (cap) | Signal key | Points | Where it comes from | Decay |
|---|---|---|---|---|
| **Volume (40)** | `vol_stated_container` — own words: `konteyner|контейнер|20ft|40ft|fura|фура|full truck|≥10 m³|≥10 t` | 40 | Telegram msg, OLX text, vacancy text, tender lot | 90 d → ×0.5 |
| | `vol_stated_3_10` — number next to `kub|куб|m3|м3` in [3,10) | 25 | same | 90 d → ×0.5 |
| | `vol_tradedata_container` — one CN shipment ≥10,000 kg or ≥12 CN shipments/yr | 40 | trade-data feed (month 2) | 12 mo |
| | `vol_tradedata_lcl` — CN shipment ≥600 kg (HS 94/69/39/73) or ≥1,500 kg (dense) | 30 | trade-data | 12 mo |
| | `vol_own_receipt` — unclaimed marking / lapsed client with receipt ≥3 m³ | 40 | ERP `receipts/receipt_lots` | 12 mo |
| | `vol_marketplace_proxy` — Σ(orders_30d × category m³ factor) ≥3 | 20 | Uzum/OLX seller aggregation | 30 d |
| | `vol_vacancy_import_role` — in-house `ВЭД|деклар|закупк.*Кита|tashqi savdo|Xitoy` role | 15 (+5 if ≥2 roles or text mentions контейнер/партии) | hh.uz | 180 d |
| | `vol_exhibitor_distributor` — Uzbek exhibitor in auto parts/building/textile machinery/furniture/power + named foreign brand | 15 | exhibitor lists | 365 d |
| | `vol_directory_wholesale` — wholesale rubric in China-heavy category | 10 | 2GIS / yellowpages / uzts | 365 d |
| | `vol_tender_goods_winner` — ≥2 wins in imported-goods categories, Σ ≥ $50k | 15 | UzTender/xarid | 365 d |
| **China origin (20)** | `cn_explicit` — `Xitoy|Китай|China|1688|Taobao|Yiwu|Guangzhou|Urumqi|xitoy tili|китайского языка` in own text, or CN supplier in trade data | 20 | any | 180 d |
| | `cn_sector_prior` — goods in China-dominant sectors (furniture, auto parts, electronics, lighting, sanitary, tools, textile/sewing machinery, building finishes, toys, packaging, EV/cars) | 10 | goods classification | — |
| **Solidity (15)** | `ent_legal_confirmed` — STIR resolved in registry | 8 | stat.uz / myorg | — |
| | `ent_age_2y_active` | 4 | registry | — |
| | `ent_not_micro` — >1 vacancy open, branches, charter capital, ≥20 SKUs, multiple phones | 3 | hh / directories / marketplace | — |
| **Recency (10)** | `fresh_7d` / `fresh_30d` / `fresh_90d` of the newest volume or intent signal | 10 / 7 / 4 | — | rolling |
| **Contactability (10)** | mobile phone +998 9x/3x/7x/8x present | 6 | any | — |
| | decision-maker name (director/owner/purchasing head) | 2 | registry / site / vacancy | — |
| | Telegram username or Instagram with DM | 2 | any | — |
| **Corroboration (5)** | ≥2 independent source families agree | 5 | resolver | — |

### 1.3 Negative signals

| Key | Points | Rule |
|---|---|---|
| `neg_b2c_words` | −40 | `o'zim uchun|для себя|1 dona|bitta|shaxsiy|личн` with no volume ≥3 |
| `neg_tiny_reseller` | −30 | Instagram/OLX-only presence, <20 listings, no legal form, no volume signal |
| `neg_state_body` | −20 | `davlat|ГУП|министер|hokimiyat|maktab|shifoxona` (state buyer → tender path, not direct) |
| `neg_non_cn_origin` | −20 | only Russia/Turkey/Korea/Europe mentioned as source |
| `neg_out_of_vertical` | −20 | perishables, live animals, hazardous, cold-chain pharma finished goods |
| `neg_liquidated` | −50 | registry status |
| `neg_lost_before` | −30 (180 d) / permanent | ERP lost reason `Narx` → −30 for 180 d; `Import qilmaydi`/`Kargo-konkurent`/`Raqam noto'g'ri` → permanent suppression |

### 1.4 Tiers and what happens

| Score | Phone? | Action |
|---|---|---|
| ≥70 | yes | `review` card to owner (MVP) → `approved` → push. From month 2, auto-push for sources whose 30-day precision ≥60% (`sources.auto_push=true`) |
| ≥70 | no | Telegram path (§3) or enrichment retry (3 attempts over 14 d) |
| 45–69 | any | `enrich` queue; owner sees them only in `/qidir` and weekly Sheets tab |
| <45 | any | `hold`, re-scored when new signals arrive; purged after 365 d without signals |

### 1.5 The minimal lead card (what a sales manager must see)

| Field | Max | Source of truth |
|---|---|---|
| Company (legal + brand) | 80 | `companies.display_name` |
| Contact person + role | 60 | `contacts.full_name`, `role` |
| Phone E.164 (+ second phone) | — | `contacts.phone` |
| Telegram / Instagram / website | — | `contacts` |
| What they import (3–6 words, uz) | 80 | `RelevanceVerdict.goods` |
| Volume estimate + basis ("≈1 konteyner/oy — o'zi yozgan", "vakansiya: VED menejer") | 120 | `signals` |
| City / address | 80 | registry / directory |
| **Why found** (1 line + evidence URL + date) | 200 | `evidence.url`, `excerpt` |
| Suggested opener (uz or ru, no prices) | 300 | `drafts` (templated, see §2.7) |
| Score + top 3 reasons, source key | — | `lead_candidates.score_breakdown` |
| STIR | 9 | registry |

---

## 2. Pipeline

```
collectors → observations (raw, per source) → normaliser → resolver (companies/contacts/aliases)
→ enrichment (deterministic → registry/directory → Claude verdict) → scorer → gates/suppression
→ lead_candidates(review) → owner bot (MVP) / auto → crm_push (POST /api/leads/in/agent) → push_log
→ nightly feedback reader (ERP RO role) → outcomes → weights/suppression/source precision
```

### 2.1 Collectors (each: `collect(session, budget) -> list[Observation]`, pure parser tested on fixtures, own heartbeat `job:collector:<name>`, own breaker)

| Collector | Produces (Observation fields) | Cadence | Rate / budget | Expected yield | Phase |
|---|---|---|---|---|---|
| `erp_internal` (SQL over LogisticsERP RO role or nightly export job in the ERP repo) | (1) `receipts.unclaimed_marking` + `handovers.person_phone` → candidate with measured m³; (2) lapsed clients (no receipt 120 d, historic ≥3 m³/mo) → reactivation list (not pushed as leads, Sheets tab for managers); (3) consolidators (many categories, many small lots) → referral asks; (4) **client suppression list** (`client_code`, `phones[]`) | nightly 02:00 | none (local) | tens of unclaimed markings/mo; suppression 400+ | MVP day 1–3 |
| `telegram_groups` (reads `interactions` the userbot stored; no network) | sender id/username/name/bio, chat, text, date; regex prefilter → Haiku extraction | continuous (event) + 15-min sweep | userbot: join ≤3 chats/day, read-only, `flood_sleep_threshold` | 30–50 chats → 10–40 raw intent hits/day, 2–8 qualified/day after tout filter | MVP |
| `hh_vacancies` (api.hh.ru) | employer name/id/site/description/industries, vacancy title/text/address/salary/published_at, `contacts` if shown; poster vs inferred employer kept separately | every 4 h | descriptive UA, token from dev.hh.ru if 403; ≤1 req/s; `/areas` resolved once | 10–30 new importer-employers/month countrywide | MVP |
| `exhibitors` (uzbuild.uz Bitrix pagination, automechanika.uz, caitme.uz, power-uzbekistan.uz, mebelexpo.uz, textileexpo.uz) | company, country, stand, sector, (website) | 1–2 weeks after each show + yearly archive | 1 req/3 s | tens of UZ rows per show, 100–200/yr | MVP batch (Automechanika 21–23 Oct 2026), then calendar |
| `uzum_sellers` (Apify getascraper/uzum-scraper $3.30/1k + own low-rate shop-page crawl) | product_id, order_count, category, shop; shop page legal name/reg no./address | weekly keywords; monthly shop pages | Apify budget $20/mo; own crawl 1 req/5 s via UZ/KZ residential proxy | 2–3k sellers → 100–300 bulky high-velocity | month 1 |
| `directories` (2GIS via Apify bornoo/searchapi; yellowpages.uz `/rubrika/` primary, `/teg/` supplement; uzts.uz `/view/allmembers/entry/?pagenum=N`) | name, legal form, rubric, address, phones, website, (social) ; uzts: STIR+region+activity+capacity | quarterly full, monthly diff | 1 req/2–3 s, robots.txt | thousands of contact rows (enrichment layer, low import evidence) | month 1 |
| `olx_business` (Apify lowlanddata/olx-uz-scraper or own) | seller profile, ads_count, business flag, text (optom/konteyner/zavod), @handles and digits in text | daily newest-first on bulky categories | $1.59/1k | 50–150 wholesale sellers/mo | month 1 |
| `tenders` (uztender.com `/supplier/{INN}` via discovery, xarid.uzex.uz for bulk) | winner name, INN, categories, sums, contract count | weekly | 1 req/3 s | modest after goods filter | month 1–2 |
| `tradedata_import` (files the owner drops: TradeInt/ImportGenius/Tendata/vedrating XLSX; Trademo snippet seeds) | importer name, HS, kg, USD, supplier, date, Incoterms | monthly export | n/a | 500–3,000 CN importers/mo if the sample passes | month 2 (after acceptance test) |
| `registry_import` (stat.uz EGRPO per-INN module, myorg.uz API if priced; uzpharm-gxp registries; pharma rubric) | name, STIR, OKED, status, reg date, director, founders, address, phone | on demand per INN; cache 90 d | ≤1 req/3 s; captcha → fall back to owner-dropped bulk extract | enrichment only | MVP (manual INN lookups) → month 1 automated |
| `web_search_enrich` (Anthropic server-side `web_search` tool inside the enrichment call, `max_uses=3`, `allowed_domains` list of UZ directories/social) | phone/Telegram/Instagram/website for a named company | per company needing contact | daily cap 150 searches | fills phone for ~30–50% of name-only companies | MVP |
| `instagram_public`, `china_side` | bio phone/Telegram, follower/post counts; Yiwu/GZ walk-in logs (not modelled → skip) | month 2–3 | low | low | later |

All HTTP goes through `services/http.py`: per-host token bucket (`COLLECTOR_RPS_DEFAULT=0.3`), robots.txt cache, `Retry-After`, ETag/sha256 content cache (skip unchanged pages), fixed UA string naming the company and a contact email, `with_backoff` on 429/5xx, per-host breaker after 5 consecutive 403/429 (pause 6 h, health problem `collector_blocked`), daily page budget per collector.

### 2.2 Normaliser (`Observation` → canonical)

```
Observation{source, source_ref, url, fetched_at, raw_sha256,
  company_name_raw, legal_form, stir, phones[], telegram, instagram, website, address, city,
  goods_text, volume_text, origin_text, signal_type, excerpt(≤600 chars), person_name, person_role}
```
Rules: phones → E.164 (`+998XXXXXXXXX`), landline/short codes kept with `kind=landline`; `telegram` lower-case without `@`, `t.me/` stripped, reject bots; website → host without `www`; names: `fold_apostrophes` (`o'`,`oʻ`,`ў`,`ʼ`), Cyrillic→Latin `transliterate`, strip quotes «»“”, strip legal forms (`MChJ, OOO, ООО, LLC, QK, AJ, XK, ЧП, ИП, YaTT, SP, ТОО`), collapse spaces → `name_key`. STIR: 9 digits + checksum; `123456789`/test rows dropped. City from address/hh area/2GIS. Every observation is stored as `evidence` (immutable) before resolution.

### 2.3 Entity resolution (`services/companies.py` ← `people.py`)

Identity key precedence (exact match wins, in this order): `stir` → `phone tail-9` (only if not flagged `shared_phone`) → `telegram username` → `website host` → `instagram` → external ids (`hh_employer_id`, `2gis_firm_id`, `uzum_shop_slug`, `olx_user_id`). Then fuzzy: `rapidfuzz.token_set_ratio` on `name_key` restricted to same city or unknown city: ≥90 → auto-merge; 80–89 → `review_merge` (owner button "birlashtir", same ambiguity rule as MIYA: ask, don't merge); <80 → new company. A phone that appears on ≥3 companies with non-matching names → `shared_phone=true` (accountant/broker/business centre), never used as merge key, lowers contactability by 3. Every raw spelling → `company_aliases`. Contacts resolve inside a company by phone/username/name (`people.py` logic as-is). Merges are logged (`merge_log`) and reversible (keep `merged_into`).

### 2.4 Enrichment (cheapest first; each step writes `evidence` + `signals`; stops when score ≥70 and phone present)

1. **Deterministic**: regex volumes (`(\d+[.,]?\d*)\s*(kub|куб|m3|м3|m³)`, `(\d+)\s*(ft|фут)`, `tonna|тонн`), China words, legal form, phones/@handles in text, HS→sector map.
2. **Cross-source join**: new observation joined to existing company adds corroboration.
3. **Registry**: STIR known → stat.uz module (or myorg API): status, OKED, director, founders, address, phone. Name-only → registry name search only if the module supports it; else `web_search_enrich`.
4. **Directory lookup**: 2GIS/yellowpages by `name_key`+city → phones, website, social.
5. **Website / Instagram public page**: `extract_urls`, `t.me/`, `instagram.com/`, `tel:` links; 1 page + contacts page only.
6. **Claude verdict** (`services/claude.py` ← `extraction.py`, structured outputs, never raises; port note: `output_format` is deprecated → `output_config={"format": …}`; models reject `temperature`, thinking is on by default → set `output_config.effort="low"` for classification):

```python
class ImportSignal(BaseModel):
    goods: list[str]                 # uz, 1–6 items
    origin: Literal["china","other","unknown"]
    volume_m3_est: float | None
    volume_basis: Literal["stated","container_words","weight","proxy","none"]
    evidence_quote: str | None       # verbatim substring; validator: must occur in input
    frequency: Literal["one_off","monthly","weekly","unknown"]

class RelevanceVerdict(BaseModel):
    b2b: bool
    speaker_role: Literal["buyer","forwarder","supplier","broker","job_seeker","unknown"]
    imports_from_china: Literal["likely","unknown","no"]
    decision_maker: str | None       # name + role, only if present in evidence
    confidence: float
    reasons: list[str]               # ≤5, uz
```
Hot path (Telegram, minutes): Haiku 5.5 `ImportSignal` on the message + bio; Sonnet 5.5 `RelevanceVerdict` only when a volume/container signal fired. Cold path (nightly): Opus 5.5 via Batch API (`model_jobs`), effort `low`/`medium`, with all evidence excerpts of a company (≤8k tokens), cached system block. The `evidence_quote` validator is the anti-hallucination rule for volumes: if the quote is not in the input, `volume_m3_est` is nulled and `volume_basis="none"`.

### 2.5 Scoring and dedupe before push

`services/scoring.py`: pure function `score(company, signals, weights) -> (int, breakdown: dict)`; weights from `scoring_weights(version)`; stores `score_breakdown` JSON on the candidate. Pre-push checks, in order (all local, no ERP call): suppression (client phone/STIR/code, DNC, competitor), `push_log` 24 h per phone, `push_log` 30 d per company, `PUSH_DAILY_CAP` (default 25), business hours (Mon–Sat 09:00–18:00 Tashkent), `CRM_PUSH_ENABLED`, push breaker not tripped. The ERP's own dedupe (known client → client card; open lead within 30 d → joined) remains the backstop, never the first line.

Client list for suppression: MVP = owner downloads the ERP client book (`/api/clients/xlsx`, needs `clients.manage`) weekly and sends it to the bot `/import` (adapted `client_import.read_rows`: code optional, phones jsonb split). Month 1 = read-only Postgres role on the ERP DB (`GRANT SELECT ON clients(id, client_code, name, phones), leads, lead_stages, lead_intakes, lead_sources, lost_reasons`), nightly sync; removes the manual step and powers the feedback loop.

### 2.6 Handoff to LogisticsERP

**Source key.** Add `'agent'` to `INBOUND_SOURCE_KEYS` in `src/modules/wms/crm/inbound.ts`, seed `lead_sources(name='Qidiruv agenti', key='agent')`, create the secret in *Kelgan arizalar → Reklama havolalari → agent → Yoqish va kalit yaratish*. Until merged: `POST /api/leads/in/other` with `utm_source=clientscrapper` (funnel shows «Boshqa»). Owner-side setup once: map form question `kub → quoted_volume_m3`, `company → custom field "Kompaniya"`, `stir → custom field`; create `inbound_routes`: (1) `source_key=agent, min_m3=3 → cargo managers`, (2) `source_key=agent, keyword=xarid|1688|sourcing → buying team`.

**Payload** (the agent composes; everything beyond name/phone/note lands in note + `fields`):

```json
POST /api/leads/in/agent        X-ERP-Key: <secret>
{
 "external_id": "cs:9f2a…:202610",
 "name": "Rustam Karimov (direktor) — OOO Textile Trade",
 "phone": "+998901234567",
 "kub": "25",
 "company": "OOO TEXTILE TRADE", "stir": "30xxxxxxx",
 "telegram": "@rustam_tt", "instagram": "textiletrade.uz", "website": "textiletrade.uz",
 "address": "Toshkent, Yakkasaroy", "imports": "mato, furnitura, tikuv mashinalari",
 "score": "82", "evidence": "https://t.me/optom_bozor/18234", "utm_source": "clientscrapper",
 "note": "NEGA TOPILDI: 20.10.2026 @Optom_bozor guruhida yozgan: «Guangzhoudan 25 kub mato olib kelish kerak, kim qiladi?»\nNIMA OLIB KELADI: mato, furnitura (Guangzhou).\nHAJM: ≈25 m³, oyiga 1 marta (o'zi yozgan).\nKOMPANIYA: OOO Textile Trade, STIR 30xxxxxxx, 2019 y., faol. Rahbar: Rustam Karimov.\nQO'NG'IROQ UCHUN: «Assalomu alaykum, Rustam aka. GSR Logistics, Xitoydan yuk tashiymiz. Guangzhoudan mato uchun kub narxini va muddatni aniq aytib beraman — hozir yukingiz Guangzhouda tayyormi?» Narx va muddat va'da qilmang — hisob-kitobdan keyin.\nBall: 82 (hajm 25 m³ 25, Xitoy 20, kompaniya 12, yangi 10, aloqa 10, tasdiq 5)."
}
```
`external_id = cs:<company_id>:<yyyymm>` — a new month = a new enquiry if a new signal appeared; the same month never re-sends. `note ≤1000` chars, `name ≤120`. The manager therefore sees **why** (verbatim quote + link + date), **what** (goods, volume, basis), **who** (company, decision maker), **what to say** (opener, no price promise), **score reasons**.

Opener templates (filled by the agent, redlines gate blocks digits+currency and delivery-day promises):

- uz (Telegram intent): «Assalomu alaykum, {ism}. {guruh}dagi xabaringizni ko'rdim — {shahar}dan {tovar} bo'yicha. GSR Logistics (2018 yildan, Yiwu/Guangzhou omborlari). Hajm va tovarni aytsangiz, aniq hisob-kitob yuboraman. Qulay vaqt qachon?»
- ru (hh/exhibitor): «Здравствуйте, {имя}. Это {менеджер}, GSR Logistics — доставка из Китая в Узбекистан (сборные грузы, контейнеры, растаможка). Вижу, что {компания} работает с импортом из Китая. Могу посчитать актуальный маршрут и стоимость под ваш объём — удобно обсудить 10 минут?»

**Push mechanics** (`services/crm_push.py`): `push_log(external_id, company_id, phone_tail, payload_sha256, sent_at, http_status, attempt)`; `200` → never re-send; network/5xx/timeout → `with_backoff` 3 attempts then `failed`; `404` → push breaker (`policy.trip_breaker('crm_push')`), health problem `push_404`, owner alert («CRM kaliti yoki manba noto'g'ri»); resume only via `/yoq crm`.

### 2.7 Feedback loop (nightly `feedback_job`, ERP RO role; month 1 alternative: `GET /api/leads/out/agent?since=` with X-ERP-Key, one new route in the ERP)

| Read from ERP | Mapped to `outcomes` | Used for |
|---|---|---|
| `lead_intakes(channel='webhook', external_id)` → `outcome` (created/joined/known_client/capped/dropped), `contacted_at`, `contact_kind`, `lead_id`, `client_id` | `landed`, `contacted_within_h` | contact-rate, time-to-contact; `known_client` → add phone to `suppression(client)` (our list was stale) |
| `leads.stage_id → lead_stages.kind` (`open/won/lost`), `lost_reason`, `closed_at`, `client_id`, `quoted_volume_m3`, `quoted_amount` | `label ∈ {won, quoted, contacted_open, lost_price, lost_not_importer, lost_small, lost_wrong_number, lost_competitor, lost_other}` | precision per source/signal, weight retraining, suppression |
| `lost_reasons` (owner adds 4 standard: `Import qilmaydi`, `Hajm kichik (<3 m³)`, `Raqam noto'g'ri`, `Kargo/konkurent`) | negative labels | — |

Policy: positive = `won` or `quoted` or (`contacted` and still open after 14 d); negative = the four standard lost reasons. Weekly: `source_precision = positives / pushed` (window 30 d, min 15 pushes) shown in `/holat`; `auto_push` flips on at ≥60%, collector auto-pauses at <20% over ≥30 pushes with an owner alert. Monthly: once ≥150 labelled candidates, fit a logistic regression on `score_breakdown` features (scikit-learn, offline, `tools/fit_weights.py`), propose new `scoring_weights` version with each weight clamped to ±30% of the previous, owner approves via bot; until then weights are edited by hand (`/vazn key value`). Lost `Narx` → 180-day cooldown; the other three → permanent suppression of that company and phone.

### 2.8 Suppression lists (`suppression(kind, key_type, key, reason, source, expires_at)`)

`client` (ERP client book, GS codes via `codes.py`), `dnc` (any reply containing `yozmang|не пишите|stop|bezovta qilmang`, blocks, complaints — added by the bot button and by the userbot on inbound replies), `legal` (removal requests; `purge.plan_company`), `competitor`, `b2c`, `bounced` (wrong number ×2), `pushed_recent` (derived from `push_log`), `manager_veto` (owner/manager marks "bu bizga kerak emas"). Checked at gate time and again immediately before send/push.

---

## 3. Intent-signal path (Telegram)

**Reader**: `userbot/main.py` from MIYA, dedicated account (new +998 SIM, aged 2–4 weeks with normal use, 2FA, Premium for the 1000-chat cap), `connect()` not `start()`, `USERBOT_ENABLED` kill switch idles the process, read-only, joins ≤3 chats/day from a seed list (`@Optom_bozor`, `@optomelonuz`, `@mk_express`, `@fx_express` + linked discussion groups via `GetFullChannel.linked_chat_id`, companion "buyers" groups from pinned posts, tg-cat/t.me/s previews). `ChatMonitor` default **on** for groups/channels (MIYA default flipped), `/chats` toggles. Backfill `iter_messages(search=kw, limit=500)` once per chat after the owner's «Ha».

**Signal → lead in minutes:**

| Step | Latency | Mechanism |
|---|---|---|
| 1. `events.NewMessage(chats=monitored)` → `interactions` row (`already_stored` unique index) | <5 s | userbot |
| 2. Regex prefilter: `(\d+\s*(kub|куб|m3|м3))|konteyner|контейнер|fura|фура|20ft|40ft|tonna|тонн|optom|оптом|zavod|kim olib keladi|kerak|нужен|ищу` AND not a known tout (bio/text `kargo|карго|express|dostavka|narxlar|прайс`, or the sender has posted ≥3 price lists) | <1 s | worker listener on `interactions` insert (LISTEN/NOTIFY or 20 s poll) |
| 3. Haiku 5.5 `ImportSignal` (message + sender bio + last 3 messages of the same sender) → `signals` | 2–4 s | `costs.ask` before the call |
| 4. If `volume_m3_est≥3` or container words or sender posted ≥3 demand messages in 60 d → Sonnet 5.5 `RelevanceVerdict` | 3–6 s | — |
| 5. Resolver: username/phone-in-bio/name → company; enrichment `web_search_enrich` for a named company (phone, STIR) | 10–60 s | — |
| 6. Scorer → candidate; owner bot card «Yangi signal» with buttons: **CRM'ga yubor** (needs phone), **Qoralama** (no phone → outreach draft), **Keyinroq**, **Yo'q**, **Shaxsiy/DNC** | <2 min total | `lead_reviews` state machine (`approvals.py` pattern) |
| 7. Approved + phone → `crm_push` immediately (business hours) → manager gets «Yangi lid» with the quote and link within the ERP's 15-minute clock | +1 min | — |

**Who contacts Telegram-only prospects** (no phone — the common case):

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| A. Agent account sends owner-approved drafts (MIYA `sender` + `outbox` + `gate`) | fastest; central caps; exactly-once | cold DMs from a shared account = spam reports/ban; manager not in the loop; conversation lives outside the ERP | **Not in MVP for DMs.** Month 2: `OUTREACH_MODE=draft`, only **in-thread public replies** in the group where the request was posted (what competitors do, far lower ban risk), `OUTREACH_DAILY_CAP=10`, `OUTREACH_PER_CHAT_CAP=1`, quiet hours, `cold_first_contact` requires `approved_by='owner'`, `auto` structurally impossible for that intent |
| B. Manager replies from their own CRM-connected account (GramJS listener in LogisticsERP already mirrors their chats) | sales-owned, conversation lands in the ERP thread, no shared-account risk, manager's own name | needs a lead card without phone → push with `phone` absent is a new lead every time → instead the owner bot forwards the card to the manager (bot DM with `t.me/<username>` deep link and the draft) and the manager presses «Bog'landim» after writing | **MVP default.** Card goes to the on-duty manager via the owner bot (`MANAGER_TELEGRAM_IDS` allowlist); when the prospect replies with a phone, the agent pushes to the ERP with the phone so the lead exists in the funnel |

Gate `LINES` order for the outbox (month 2): `kill_switch, pause, mode, suppression(DNC/client/competitor), safety(scam/link), owner_approved, caps(daily, per_account, per_chat=1), redlines(no digits+currency, no days), payload(plain text), quiet_hours`. The AST tests from MIYA (`WRITE_CALLS` only in sender functions, `claim_ready` only, kill switch idles before connect, history read only after owner asked) are ported unchanged.

---

## 4. Data model, scheduling, evidence, observability, caps

### 4.1 Tables (PostgreSQL 16; Alembic fresh `0001`)

| Table | Key columns |
|---|---|
| `sources` | `key pk, family, enabled, rps, daily_page_budget, auto_push bool, precision_30d, paused_until, paused_reason, config jsonb` |
| `collector_runs` | `id, source_key, started_at, finished_at, pages, new_observations, new_companies, new_signals, http_403, http_429, errors, cost_usd` |
| `evidence` | `id, source_key, url, source_ref, fetched_at, sha256, excerpt text, raw_path (gz on disk), content_type, expires_at` |
| `observations` | `id, evidence_id, company_id null, parsed jsonb (Observation), status (new/resolved/rejected)` |
| `companies` | `id, display_name, name_key, legal_form, stir unique null, city, address, website, oked, status, registered_at, director, sector[], goods[], merged_into, first_seen, last_signal_at, best_score, state` |
| `company_aliases` | `company_id, alias, alias_key, source_key` (GIN on `alias_key`) |
| `company_ids` | `company_id, id_type (hh_employer, 2gis_firm, uzum_shop, olx_user, tg_user, instagram), id_value` unique |
| `contacts` | `id, company_id, full_name, role, phone E.164 unique-per-company, phone_kind, telegram_username, telegram_id, instagram, email, is_decision_maker, source_key, verified_at, shared_phone bool` |
| `signals` | `id, company_id, contact_id null, key, points, value numeric null, text, evidence_id, observed_at, expires_at` |
| `scoring_weights` | `version, key, points, active, approved_by, created_at` |
| `lead_candidates` | `id, company_id, contact_id, score, score_breakdown jsonb, tier, state (found/enriching/scored/review/approved/rejected/pushed/suppressed/excluded), exclude_reason, review_card_msg_id, approved_by, approved_at, external_id` |
| `lead_reviews` | `candidate_id, state (pending/asked/approved/declined/expired/merged), asked_at, answered_at, answer, by_telegram_id` |
| `push_log` | `external_id pk, candidate_id, company_id, phone_tail, source_key_sent, payload_sha256, payload jsonb, sent_at, http_status, attempt, error` |
| `outcomes` | `external_id, lead_id, intake_outcome, contacted_at, stage_kind, stage_name, lost_reason, quoted_m3, quoted_amount, client_id, label, read_at` |
| `suppression` | `kind, key_type (phone_tail/stir/code/tg/company_id), key, reason, source, created_at, expires_at` unique(key_type,key,kind) |
| `drafts` | `id, candidate_id, channel (tg_reply/tg_dm/call_script), lang, text, redlines_ok, approved_by, outbox_id` |
| `interactions` (trimmed MIYA) | `chat_id, message_id, sender_id, sender_username, sender_name, sender_bio, text, date, media_kind, prefilter_hit, processed_at`, unique `(chat_id, message_id)` |
| `chat_monitors`, `outbox_messages`, `heartbeats`, `usage_log`, `reminder_log`, `sheet_rows`, `model_jobs` | verbatim from MIYA (`model_jobs` ← `conversation_windows` batch columns: `custom_id, batch_id, attempts, status, request jsonb, result jsonb`) |
| `merge_log`, `purge_log` | audit |

### 4.2 Scheduling — APScheduler in `worker` (not cron)

Reasons: MIYA's heartbeat ledger, `EVENT_JOB_*` listener, in-process `policy.pause` (no restart), per-job `coalesce/max_instances=1`, and the kill switches are all in-process. Cron only for `docker compose up` on reboot.

| Job | Cadence (Asia/Tashkent) |
|---|---|
| `collector:<name>` | per `sources.cron` (telegram listener continuous; hh 4 h; olx daily 07:00; uzum weekly Sun; directories monthly; exhibitors by calendar; erp_internal 02:00) |
| `resolve_job` | every 5 min |
| `enrich_job` (deterministic + registry + web_search, budgets) | every 15 min |
| `model_batch_submit` / `model_batch_collect` | 23:30 submit, every 30 min collect (Opus 5.5 Batch) |
| `score_job` | every 10 min + on signal insert |
| `review_cards_job` | 09:00–18:00 every 20 min, ≤10 cards/burst, question budget |
| `crm_push_job` | Mon–Sat 09:30–17:30 every 15 min, `PUSH_DAILY_CAP` |
| `feedback_job` | 03:00 |
| `source_precision_job`, `weights_proposal_job` | weekly Mon 08:00 / monthly 1st |
| `outbox reconcile`, `sender` | as MIYA (month 2) |
| `sheets_sync_job`, `health_job`, `heartbeat_job`, `_deadman_ping`, `backup_job`, `retention_job`, `digest_job` (09:00 owner summary) | as MIYA |

### 4.3 Raw evidence

Every observation keeps the full fetched body gzipped on disk `/data/evidence/<yy>/<sha256>.gz` (HTML/JSON/XLSX), excerpt ≤600 chars in `evidence.excerpt`, URL, timestamp. Lead card and ERP note always cite `evidence.url` + date + verbatim quote. Retention: raw 180 d (`retention_job`), excerpt + URL for the life of the company, Telegram message text 365 d, personal data of excluded/B2C persons purged 30 d after exclusion. Backups: `pg_dump | age` nightly as MIYA; evidence dir excluded from Telegram-split backups (restic/rsync to object storage if wanted).

### 4.4 Observability

`/holat`: per source (last run, pages, new companies, candidates, precision 30 d, breaker state), queue depths (`review`, `enrich`, `pushable`), today's pushes/cap, cost today/month vs cap, userbot (last message seen per chat, flood waits), push breaker. Daily 09:00 digest to the owner. `health.py` components: `api, bot, worker, userbot, db, anthropic, crm_push, collector:<name>`; problems `collector_blocked`, `push_404`, `cost_cap`, `userbot_unauthorized`, `flood_breaker`, `no_signals_24h` (dead chats). Dead-man ping. Sheets mirror tabs `Kompaniyalar / Lidlar / Yuborilganlar / Manbalar` (RAW values, hash rewrites, tombstones) for the owner's spreadsheet habit. JSON logs with `RedactBodies` (ids, never message bodies or phones).

### 4.5 Cost caps and kill switches

| Lever | Mechanism |
|---|---|
| `DAILY_COST_CAP_USD` (default 5), `MONTHLY_COST_CAP_USD` (120) | `costs.ask(operation)` before every model call; refuses over cap; unknown price = over cap; `usage_log` priced from `MODEL_PRICES` |
| Per-operation budgets | `classify` (Haiku) 40%, `verdict` 40%, `draft` 10%, `enrich_search` 10% of daily cap |
| `COLLECTOR_DAILY_PAGE_BUDGET`, per-host rps | `http.py` token buckets |
| Env kill switches | `COLLECTORS_ENABLED`, `USERBOT_ENABLED` (idles, never exits), `TELEGRAM_SEND_ENABLED`, `CRM_PUSH_ENABLED` |
| Runtime | `/toxtat` (global `policy.pause`), `/manbalar` per-source toggle, `/chats` per-chat, `/yoq crm` to clear the push breaker |
| Automatic breakers | host 403/429 streak → 6 h; push 404 → until owner; FloodWait >300 s → userbot 24 h breaker; cost cap → model calls refuse but collectors continue (signals queue) |

---

## 5. Delivery plan

### 5.1 MVP — 2 weeks (10 working days, 1 developer with Claude Code + owner 30 min/day)

| Day | Deliverable |
|---|---|
| 1–2 | Repo from MIYA skeleton (config, db, logs, retry, usage/costs, health, backup, docker, Makefile, tests ported: env_example, health, usage_backup, userbot AST guards). `0001` migration with §4.1 tables. ERP side: `agent` source key PR (constant + seed), secret created, `kub`/`company`/`stir` mapped, 2 `inbound_routes`, 4 lost reasons |
| 3 | `erp_internal`: client suppression import (`/import` XLSX, then RO role), unclaimed markings → candidates, lapsed-client Sheets tab |
| 4–5 | `userbot` (reader only) + `telegram_groups` collector + regex prefilter + Haiku `ImportSignal` + Sonnet verdict; join 10 seed chats; backfill |
| 6 | `companies.py` resolver + normaliser + `scoring.py` v1 + `suppression.py` |
| 7 | `hh_vacancies` collector (smoke test 403/UA/token; areas resolved) + agency/competitor exclusion rules |
| 8 | `enrich.py` (regex, registry per-INN manual queue, `web_search_enrich`) + Opus 5.5 Batch verdict path |
| 9 | Owner bot: `/lidlar` cards with buttons, `/manbalar`, `/holat`, `/xarajat`, `/toxtat`; `crm_push.py` with fake-server tests (200/404/500/timeout); first real pushes |
| 10 | `exhibitors` batch (Automechanika 2026 list + UzBuild 2026 archive), `feedback_job` v1 (RO role or manual), digest, Sheets mirror; pilot with one sales manager |

What the manager sees in week 2: ordinary «Yangi lid» cards in the ERP with source «Qidiruv agenti», note = why/what/who/what-to-say, `quoted_volume_m3` filled, routed by the ≥3 m³ rule. Nothing new to learn.

Success metric (end of week 2): ≥20 pushed leads with phone, score ≥70; manager marks ≥60% as "real importer" (not lost with `Import qilmaydi`/`Raqam noto'g'ri`); ≥40% contacted (`lead_intakes.contacted_at` within 1 business day); ≥3 quotes sent; Claude spend <$3/day.

### 5.2 Month 1

`uzum_sellers`, `olx_business`, `directories` (2GIS pilot 100 records, yellowpages rubrics, uzts.uz full list), `tenders` weekly; registry automation (stat.uz module XHR or myorg API quote); ERP RO role + automated feedback; `source_precision_job`; auto-push for proven sources; Sheets tabs; retention/purge; trade-data **acceptance test** (free samples from TradeInt, ImportGenius, Tendata, vedrating: named UZ consignees, 2025–2026 dates, per-shipment kg + USD, CN origin filter; STIR hit-rate against GSR's 400 clients as the validity check).

### 5.3 Month 2–3

Buy at most one trade-data feed if the sample passed (TradeInt Pro ~$2,999/yr or ImportGenius add-on $99–399/mo, confirm freshness ≤6 months); `tradedata_import` monthly; weights v2 from logistic regression; outreach `draft` mode with in-thread replies and caps; `instagram_public`; pharma/textile registries; manager-facing `/qidir` (search a company before a call); lapsed-client reactivation campaign via managers; quarterly directory recrawl.

### 5.4 Team / effort

1 backend developer (Python; ~60% of code is ported) — MVP 10 days, month 1 +12 days, month 2–3 +15 days. Owner: review desk 30 min/day in weeks 1–4, then 10 min/day. One pilot sales manager (cargo team) week 2–4, then all. No designer, no frontend (bot + ERP + Sheets).

### 5.5 Monthly running cost

| Item | MVP | Month 3 |
|---|---|---|
| VPS 4 vCPU / 8 GB / 160 GB (no torch/embeddings) | $25–40 | $25–40 |
| Telegram Premium (reader account) + SIM | $5 | $5 |
| Claude API (below) | $25–60 | $60–150 |
| Anthropic web_search tool (≤150 searches/day, per-search billing — verify current rate in console) | $20–45 | $30–60 |
| Apify (Uzum $3.30/1k, OLX $1.59/1k, 2GIS $1.99/1k) | $0 | $30–60 |
| Residential proxy UZ/KZ (shop pages only) | $0 | $20–50 |
| Trade data | $0 | $100–400 (annual plan amortised) |
| myorg.uz API / stat.uz bulk extract | $0 | $0–100 (quote) |
| TGStat | skip (t.me/s previews + GetFullChannel suffice) | optional |
| **Total** | **≈$75–150** | **≈$300–850** |

Claude token math (prices: Haiku 5.5 $0.10/$0.50, Sonnet 5.5 $2/$10, Opus 5.5 $4/$20 per MTok; Batch −50%; cached reads ≈ 10× cheaper than input):

| Operation | Volume/day | Tokens in/out | Model | $/day |
|---|---|---|---|---|
| Telegram `ImportSignal` (after regex prefilter ~5% of 20k msgs) | 1,000 | 700 (600 cached system) / 150 | Haiku 5.5 | ≈0.15 |
| Telegram `RelevanceVerdict` | 100 | 2,500 / 300 | Sonnet 5.5 | ≈0.8 |
| hh vacancy classify | 300 | 2,000 / 250 | Haiku 5.5 | ≈0.1 |
| Nightly company verdict (all evidence) | 100 | 6,000 / 800 | Opus 5.5 Batch | ≈2.0 (4.0 realtime) |
| Outreach/call-script drafts | 30 | 3,000 / 300 | Opus 5.5 | ≈0.55 |
| **Sum** | | | | **≈3.6/day ≈ $110/mo at full load; MVP load is ~1/3** |

Cap at `DAILY_COST_CAP_USD=5`; the cap, not the estimate, is the budget.

### 5.6 KPIs (weekly in `/holat` and Sheets)

| KPI | Definition | MVP target | Month 3 target |
|---|---|---|---|
| Qualified leads / week | pushed with `200`, phone, score ≥70 | 10 | 40 |
| Precision | pushed − (lost `Import qilmaydi` + `Raqam noto'g'ri` + `Kargo/konkurent`) ÷ pushed, 30 d | ≥60% | ≥75% |
| Contact rate | `contacted_at` within 1 business day ÷ landed | ≥40% | ≥70% |
| Quote rate | `quoted_volume_m3` or quote stage ÷ contacted | ≥25% | ≥35% |
| Meeting / first shipment | `won` or `client_id` set ÷ pushed (60-day lag) | ≥3% | ≥8% |
| Time to first contact | median minutes from push to `contacted_at` | <120 | <45 |
| Cost per qualified lead | (API + data + infra) ÷ qualified | <$5 | <$12 |
| Per-source precision & yield | from `outcomes` | reported | drives `auto_push`/pause |
| Telegram intent → card latency | p90 | <3 min | <2 min |

---

## 6. Top risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Telegram reader account limited/banned (joins, scraping pattern, spam reports) | signal source stops | dedicated aged account, read-only in MVP, ≤3 joins/day, `flood_sleep_threshold`, FloodWait breaker, never DM cold, in-thread replies only with caps from month 2, second standby account, kill switch idles process; managers' accounts never used by the agent |
| ERP cap/dedupe burn (3/phone/day, known clients, joined leads) | leads silently dropped, managers lose trust | local suppression + `push_log` checks before every push; `PUSH_DAILY_CAP` 25; `external_id` monthly; `known_client` outcomes feed suppression; weekly reconciliation of `push_log` vs `lead_intakes` |
| Wrong/angry contacts (B2C, competitors, shared phones, stale numbers) | reputation, complaints | hard gates, `shared_phone` flag, two-strike `bounced`, DNC on any "yozmang", redlines gate (no prices/promises), managers always introduce the source honestly («e'loningizni ko'rdik»), purge on request |
| Legal (personal data law, source ToS: Trademo/hh/2GIS/OLX scraping, trade-data provenance) | takedowns, fines | B2B contact data only, minimal fields, provenance per record, robots.txt + low rates, official hh API, licensed 2GIS export when offered, no republishing, Sheets internal only, data retention/purge, paid data under contract; treat vedrating/Tendata as unverified until sample |
| Data staleness (trade data 10–13 months behind; directories years old) | calling dead leads | recency weight + decay; stale sources capped at 30 points without a fresh corroborating signal; registry status check before push; re-verify phone via a second source |
| Claude hallucinated volumes/companies | wrong routing, embarrassing cards | `evidence_quote` validator, deterministic scorer, score breakdown on every card, verdict is a feature not a decision, owner review in MVP, per-source precision gate before auto-push |
| Low yield of real demand in Telegram (mostly touts) | MVP looks empty | tout filter, linked discussion groups not channels, repeat-poster logic, hh + exhibitors + ERP unclaimed markings as parallel MVP sources, weekly chat pruning by yield |
| hh API blocked / 403 | lose freshest source | app token from dev.hh.ru, UA, daily SEO landing-page fallback with ID dedupe, breaker + alert |
| Sales adoption (managers ignore agent leads, no feedback) | no loop, no proof | leads arrive through the same door and bot as ad leads; note answers "why/what to say"; routing to the right team by m³; the ERP's 15-minute reminder; weekly precision review with lost reasons; pilot with one motivated manager; agent leads tagged so the funnel report shows their conversion |
| Cost runaway (model loops, crawler budget) | bill | hard daily/monthly caps with refusal, per-operation budgets, Batch for bulk, page budgets, deadman + digest |
| Coupling to MIYA / schema drift | maintenance | copy not import; own `Base`; AST tests "no collector imports telethon/anthropic", "no push without approved state"; `.env.example`↔`Settings` test |
| ERP contract drift (source keys, field map, site code mismatch `GSR-` vs `LEAD-`) | pushes 404 / site leads lost | push breaker on 404 with alert; contract pinned in `tests/test_crm_push.py`; fix the site/ERP prefix constant as a side task (one constant) |