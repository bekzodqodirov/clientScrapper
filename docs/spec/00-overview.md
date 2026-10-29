# 00 — Overview

## Qisqacha (o'zbekcha)

clientScrapper — GSR Logistics uchun mijoz topuvchi agent. U ochiq internetdan (Instagram, OLX, Telegram, kataloglar, hh.uz, ko'rgazmalar, xaritalar, brend dilerlari) Xitoydan ≥1 m³ hajmda tovar olib keluvchi O'zbekiston kompaniyalarini topadi, kompaniyaga bog'laydi, ball qo'yadi, egasi botda ko'rib chiqadi va lid ERP'ga (fullprompt) tushadi. Menejerlar o'z Telegram akkauntidan yozadi; agent hech kimga yozmaydi. Byudjet kuniga $10. «Qayta qilinmasin» raqamlar va mavjud mijozlar hech qachon qayta tegilmaydi. Bu papkadagi 7 ta hujjat — qurilish uchun yagona manba; `docs/` dagi qolgan fayllar — tadqiqot va asos.

## 1. Purpose

Give GSR's sales managers a steady flow of qualified B2B leads (Uzbek companies that import
goods from China at ≥1 m³ per shipment, or by truck/container), each with evidence of why it
was found, a phone or a Telegram handle, and a suggested opener — without the agent ever
contacting anyone, without buying customs data, and within $10/day.

## 2. System in one picture

```
            ┌──────────── collectors (clientscrapper/collectors) ────────────┐
 open web   │ serp_layer · telegram_monitor · instagram_discovery · olx_business│
 ─────────► │ directories · hh_vacancies · exhibitors · maps · brand_dealers  │
            │ erp_sync (suppression + outcomes feeds)                         │
            └───────────────┬────────────────────────────────────────────────┘
                            ▼ Observation (+ evidence file)
            normaliser → resolver (companies/contacts/aliases) → enrichment (regex → registry → SERP/Places → Claude)
                            ▼ signals
            scorer (deterministic, 0–100, breakdown) → gates (b2b, not competitor, not client, not DNC, not recent)
                            ▼ lead_candidates
            owner bot review (✅ ✖️ 🔀 🔒 ⏭)  ──approved──►  crm_push → POST /api/leads/in/agent → push_log
                                                                  ▲
            ERP (fullprompt): lead → manager (own Telegram) → stage/lost_reason ──► GET /api/agent/outcomes ──► outcomes → source precision, weights
            ERP client book + «Qayta qilinmasin» leads ─────────► GET /api/agent/suppression (HMAC tails) ──► suppression
```

## 3. Components and ownership

| Component | Where | Spec |
|---|---|---|
| Collectors, HTTP/SERP layer | `clientscrapper/collectors/`, `services/http.py`, `services/serp.py` | 03 |
| Normaliser, resolver, enrichment, Claude, scoring, gates, suppression, budget | `clientscrapper/services/` | 04 |
| Data model, migrations, retention | `clientscrapper/db/`, `alembic/` | 02 |
| ERP push + feeds client | `clientscrapper/services/crm_push.py`, `services/erp_feeds.py` | 05 |
| ERP side (branch `agent-feed` in fullprompt) | `src/modules/wms/crm/agent-feed.ts`, `src/app/api/agent/*`, `inbound.ts`, `scripts/seed.ts`, settings + locales | 05 |
| Owner bot, manager handoff | `clientscrapper/bot/` | 06 |
| Telegram reader | `clientscrapper/userbot/` | 03 (telegram_monitor), 06 (account policy) |
| Scheduler, health, backups, digest | `clientscrapper/worker/`, `services/health.py`, `services/backup.py` | 06 |
| Tests, CI, plan | `tests/`, `.github/workflows/ci.yml` | 07 |

## 4. Lifecycle of one lead (happy path)

1. `telegram_monitor` stores a message «Guangzhoudan 2 kub mato kerak, kim olib keladi?» from @username in a wholesale group → `interactions` → regex prefilter hits → Haiku `ImportSignal` (goods=mato, volume 2 m³, buyer, b2b) → `signals`.
2. Resolver finds no company; enrichment: username bio mentions «Textile Trade MChJ, +998 90 …» → `companies` + `contacts` (phone tail-9) → registry lookup by name (stat.uz/myorg) → STIR, status active → `signals` (entity solid).
3. Gates: b2b ✓, not competitor ✓, phone tail not in suppression ✓, no open push for this company in 90 d and none for this phone in 24 h ✓.
4. Score 72 (volume 20 + China 20 + solidity 12 + recency 10 + contact 10 + corroboration 0) → `lead_candidates.state=review` → owner bot card.
5. Owner taps ✅ → `crm_push` builds the Uzbek note (NEGA TOPILDI / NIMA OLIB KELADI / HAJM / KOMPANIYA / NIMA DEYISH / BALL), `external_id=cs:<uuid>:202610`, POST to the ERP within business hours → `push_log(200)`.
6. ERP routes it by `kub=2` to the cargo team; the manager gets «Yangi lid», writes from their own account, later moves it to won/lost.
7. Nightly `erp_sync` reads `/api/agent/outcomes` → `outcomes.label` → source precision and weight proposals; if lost with «Qayta qilinmasin», the phone tail enters suppression via the next feed pull.

## 5. Non-goals (v1)

No sending by the agent (no outbox, no drafts sent, no in-thread replies); no paid customs datasets; no Uzum/tenders collectors until month 1–2; no public API; no ML — weights are edited by hand until ≥150 labelled outcomes exist; no multi-tenant.

## 6. Reading order for the implementing model

`CLAUDE.md` → this file → `01-requirements.md` → `07-testing-and-plan.md` (tasks) → `02` → `04` → `03` → `05` → `06`. Background when needed: `docs/kod-audit-miya.md` (what to copy), `docs/manbalar.json` (source evidence), `docs/huquqiy-memo.md` (legal), `docs/sotuv-playbook.md` (how managers contact).
