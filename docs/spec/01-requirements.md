# 01 — Requirements

## 1. Actors

| Actor | Role | Interface |
|---|---|---|
| Owner (Bekzod) | Reviews candidates, toggles sources/chats, sets caps, handles DNC/purge requests | Owner bot (aiogram), weekly digest, Sheets mirror (optional) |
| Sales manager | Receives leads in the ERP, contacts the company from their own Telegram account, moves the lead through stages, picks a lost reason | ERP (fullprompt) only; optional card from the owner bot for Telegram-only prospects |
| ERP (fullprompt) | System of record for leads, clients, outcomes | `POST /api/leads/in/agent`, `GET /api/agent/suppression`, `GET /api/agent/outcomes` |
| Reader Telegram account | Read-only presence in 30–60 public groups/channels | Telethon |
| Prospect | Uzbek company importing from China | Never contacted by the agent |

## 2. Ideal customer profile (ICP) — executable

Include when **all** hold:
- Located in Uzbekistan (Tashkent first; regions allowed).
- A legal entity or a trading business (MChJ/OOO/XK/QK/AJ, or YaTT/ИП with a shop, warehouse or wholesale listing). Signals: legal form, STIR, wholesale rubric, "optom/ulgurji/оптом", shop/warehouse address, ≥10 listings, marketplace shop with legal block.
- Imports from China now or plausibly: explicit (Xitoydan/из Китая/1688/Taobao/Yiwu/Guangzhou, Chinese brand dealership, VED/China-procurement vacancy) or sector prior (furniture, appliances, electronics, lighting, sanitary, tools, textile machinery/fabrics, building finishes, toys, packaging, auto parts, EV/cars, solar).
- Volume ≥1 m³ per shipment: stated («2 kub», «konteyner», «fura»), implied (wholesale + bulky category, ≥10 listings, order velocity, VED vacancy, exhibitor, dealer of a bulky brand), or measured (ERP receipts for unclaimed markings).

Exclude when **any** holds: B2C wording («o'zim uchun», «для себя», single item, personal profile with no business); cargo/forwarding/customs-broker/logistics company (competitor); Chinese-side supplier; state body (tender path, not direct); liquidated entity; phone/handle/STIR in suppression; an open push for the same company in the last 90 days or the same phone in the last 24 hours.

Segments (for routing and scripts): A bazaar wholesalers · B marketplace sellers · C factories (equipment/raw materials) · D construction/furniture importers · E electronics/auto/appliance distributors.

## 3. Functional requirements

| ID | Requirement | Acceptance |
|---|---|---|
| F-01 | Collect observations from the open web per `03-collectors.md` on their schedules, within per-host rate limits and daily page budgets | Each collector has a fixture test and a live smoke test; `collector_runs` rows exist; no host breaker tripped in normal operation |
| F-02 | Store immutable evidence (URL, fetched_at, sha256, excerpt, gzipped body) for every observation | Every candidate card can show URL + date + quote |
| F-03 | Normalise phones (E.164, tail-9), handles, domains, names (name_key) and STIR | Golden tests over the examples in `04-processing.md` |
| F-04 | Resolve observations to companies/contacts with exact keys first, then fuzzy name match with review band | Resolver tests: merge ≥90, review 80–89, new <80; shared phones never merge |
| F-05 | Enrich cheapest-first (regex → registry → SERP/Places → Claude) and stop when score ≥60 and a phone exists | Enrichment budget per company respected; steps logged |
| F-06 | Score deterministically 0–100 with stored breakdown; tiers review/enrich/hold | Golden scoring tests; breakdown visible on card |
| F-07 | Apply gates and suppression before any card and any push | Suppressed/competitor/B2C never reach the owner; test fixtures |
| F-08 | Owner review via bot with ✅ ✖️ 🔀 🔒 ⏭; decisions audited | `lead_reviews` rows; callback parser fence test |
| F-09 | Push approved leads with a phone to the ERP with the Uzbek note template, idempotent `external_id`, business-hours window, `PUSH_DAILY_CAP` | Fake-ERP tests 200/404/500/timeout/replay; 404 trips the breaker and alerts the owner |
| F-10 | Sync suppression (full nightly, delta hourly) and outcomes (nightly) from the ERP; verify HMAC rule and salt_id | Contract tests over recorded feed responses; resync on salt change |
| F-11 | Compute per-source precision weekly; auto-push only for sources ≥60% over ≥15 pushes; pause sources <20% over ≥30 pushes with an alert | `/holat` shows precision; tests over synthetic outcomes |
| F-12 | Telegram-only prospects (no phone, score ≥60, username known) reach state review as tier 'warm' after three enrichment attempts and go to a manager via the owner bot as a card with a deep link; the manager's `/tel` turns an obtained phone into a push; never auto-sent | Card template test; `test_manager_handoff`; no Telegram write calls (AST test) |
| F-13 | Owner commands: /lidlar /manbalar /chats /holat /xarajat /toxtat /yoq /import /unut /qidir /dnc /vazn /push tekshir /stir | Each command has a handler test; callback_data parser fence |
| F-14 | Daily 09:00 digest and health alerts (collector_blocked, push_404, cost_cap, userbot_unauthorized, flood_breaker, no_signals_24h) | Health tests; dead-man ping configured |
| F-15 | Retention and purge: raw evidence 180 d, intent posts 30 d, unanswered leads 90 d → company-tier only, DNC hashes forever; `/unut` purges a company/person with audit | Retention job tests; purge_log rows |

## 4. Non-functional requirements

| ID | Requirement |
|---|---|
| N-01 | Budget: total paid spend ≤ $10/day and ≤ $250/month, enforced by `costs.ask` before each paid call; refusal is logged; alert at 80% |
| N-02 | No message, call, SMS or e-mail is ever sent to a prospect by the system; enforced by an AST test and by the absence of a sender module |
| N-03 | Legal: company-tier data free; person-tier limited to one decision maker with basis/source/date; pseudonymised model calls; retention as F-15; data hosted in Uzbekistan or an adequacy-list country; registration of the database at pd.gov.uz is the owner's task before go-live |
| N-04 | Robustness: every external call has timeout, retry with backoff, breaker; the process survives Telegram flood waits; kill switches `COLLECTORS_ENABLED`, `USERBOT_ENABLED`, `CRM_PUSH_ENABLED`, runtime `/toxtat` |
| N-05 | Observability: JSON logs with redaction, heartbeats per job, `/holat`, digest, usage_log priced per call |
| N-06 | Latency: Telegram intent → owner card p90 < 5 minutes (event-driven: userbot NOTIFY → classify/score → review cards every 2 min in review hours); approved push → ERP < 1 minute inside business hours |
| N-07 | Throughput: up to 200 observations/hour, 2,000 companies/month, 25 pushes/day without schema changes |
| N-08 | Portability: single VPS (4 vCPU/8 GB), Docker Compose, nightly encrypted backups, restore drill monthly |
| N-09 | Code quality: ruff + mypy on services, pytest with Postgres in CI, fixtures for every parser, no secrets in repo |

## 5. Success metrics

| Metric | MVP (end of week 2) | Month 3 |
|---|---|---|
| Pushed leads with phone and score ≥60 | ≥20 | ≥40/week |
| Precision (not lost as «Import qilmaydi» / «Raqam noto'g'ri» / «Kargo/konkurent») | ≥60% | ≥75% |
| Contacted within 1 business day (`contacted_at`) | ≥40% | ≥70% |
| Quotes sent / contacted | ≥25% | ≥35% |
| First shipment / pushed (60-day lag) | ≥3% | ≥8% |
| Paid spend | ≤$5/day | ≤$10/day |
| Cost per qualified lead | <$5 | <$12 |

## 6. Constraints and assumptions

- The ERP branch `agent-feed` may not be deployed on day 1: the agent must work with `POST /api/leads/in/other` + `utm_source=clientscrapper` and a weekly client XLSX import for suppression until the feeds exist.
- The customs Excel in `TelegramCustoms` has no importer identity; it is used only for category priors.
- Web search engines index Uzbek sites unevenly; directory rubric pages and Telegram catalogues must be read directly (see `docs/ochiq-internet.md` §4).
- From 2026-11-01 advertising calls/SMS require prior consent and are banned 18:00–09:00; the design keeps all outreach human, 10:00–17:00, with an opt-out line.
