# LogisticsERP change set for `clientScrapper` — minimal spec

Repo read: `/home/user/logisticserp` (CLAUDE.md respected; nothing written). Migration ledger tail: `0131_calc_excise`, `when: 1785190000110`, 132 SQL files. **The recommended set needs no migration.**

## 0. Facts the design rests on (verified in source)

| Fact | Where |
|---|---|
| Inbound door is generic: `POST /api/leads/in/<key>` for any key in `INBOUND_SOURCE_KEYS`; 404 when `lead_sources.webhook_secret` is NULL; key via `X-ERP-Key` header or body `key`; always answers `{"ok":true}` | `src/app/api/leads/in/[source]/route.ts`, `src/modules/wms/crm/inbound.ts` (`sourceWebhookSecret`, `setSourceWebhookSecret`), `src/modules/wms/crm/inbound-webhook.ts` (`secretFrom`, `secretMatches`, `refWithoutSecret`) |
| Unknown body keys become `fields: [{key,value}]` pairs → the "Forma savollari" mapper (`kub`/`kg`/custom field/note) | `inbound-webhook.ts` `fromPlain`, `src/modules/wms/crm/field-map.ts` (`applyFieldMap`, `parseMeasure`, `saveMapping`, `seenKeys`) |
| Landing order: active client by phone → open lead on same phone (open stages, 30 days) → new lead routed by `routeInboundOwner`; every branch writes `lead_intakes`; replay (same `channel`+`external_id`) returns `dropped/replay` **without** a new ledger row | `inbound.ts` `landInboundLead`, `inboundMatch`, `INBOUND_JOIN_DAYS` |
| Caps: `PHONE_CAP_PER_DAY = 3`, `SOURCE_CAP_PER_DAY = 200` (counted over `lead_intakes`, dropped included) | `inbound.ts` |
| Phone identity = last 9 digits (`inboundPhoneDigits`); clients' phones live in `clients.phones jsonb` (array of text); `activeClientsByPhone` prefilters on last 7 then `phoneBelongsToClient` | `inbound.ts`, `src/modules/platform/db/schema/platform.ts:292-343`, `src/modules/wms/client-cabinet/service.ts:93`, `src/modules/platform/clients/phone.ts` |
| `lead_sources.key` is stamped by `scripts/seed.ts` `seedCrm()` `KEYED` loop on every deploy (idempotent: existing key skipped, default-named row stamped, else inserted); seed runs from the compose `migrate` service | `scripts/seed.ts:270-305`, `tests/unit/seed-demo-gate.test.ts` |
| Source keys render raw (no i18n): `route-list.tsx:130` prints `{key}`; `/crm/kelganlar` shows `lead_sources.name` | `src/app/(protected)/admin/taqsimot/route-list.tsx`, `src/app/(protected)/crm/(pages)/kelganlar/page.tsx` |
| `lead_intakes.channel` CHECK = `('form','meta','telegram','webhook','site')`; `outcome` CHECK = `('created','joined','client','dropped')`; `reason` is free text | `schema/wms.ts` `leadIntakes` |
| `leads.lost_reason` is TEXT = the label at the moment of choosing; `lost_reasons` dictionary is owner-edited; non-empty list makes free text illegal (`reasonAllowed`) | `schema/wms.ts`, `src/modules/wms/crm/stage-law.ts`, `src/modules/wms/crm/service.ts` (`saveLostReason`, `moveLead`) |
| Routing rule with `min_m3`/`max_m3` never matches an arrival whose volume is unknown | `src/modules/wms/crm/routing.ts` `matchRoute` |
| Contact clock + super-admin «⏰ tegilmagan» starts for every non-`site` arrival that produced a lead | `src/modules/wms/crm/inbound-notify.ts` `announceArrival` |
| Route handlers are self-guarded (no `src/middleware.ts`/`proxy.ts` exists); authenticated staff routes call `authorize('<perm>')`; device routes use `Authorization: Bearer` + a per-device token (410 to stop) | `src/app/api/clients/xlsx/route.ts`, `src/app/api/calls/logs/route.ts`, `src/app/api/track/positions/route.ts` |
| A route handler's JSON is not compressed by Next; `json(request, data, opts)` adds gzip ≥1 KB + weak ETag/304 | `src/modules/platform/http/json.ts` (#752) |
| Postgres is **not** published: `ports:` exist only on `caddy`/`caddy-local`; `erp_ai_reader` is `NOLOGIN` and engaged by `SET LOCAL ROLE` inside the app's own transaction, with a per-table GRANT allowlist | `docker-compose.yml:186,208`, `src/modules/platform/db/migrations/0080_ai_assistant.sql`, `src/modules/platform/ai/sql-runner.ts:51` |
| New setting key ⇒ `settings.descriptions.<key>` must exist in all 4 bundles or `/admin/settings` throws at render | `tests/unit/locales.test.ts:175-210`, `src/app/(protected)/admin/settings/page.tsx:38` |
| Pool `max: 10` (#714) | `src/modules/platform/db/client.ts:13` |

## 1. Source key `agent`

| File | Change |
|---|---|
| `src/modules/wms/crm/inbound.ts` | Add `'agent'` to `INBOUND_SOURCE_KEYS` with a why-comment: *a lead a machine found, not one a person sent; the only key whose arrivals are also read back (§2–3)*. |
| `scripts/seed.ts` `KEYED` | Add `{ key: 'agent', name: 'Agent (qidiruv)', sortOrder: 95 }`. Idempotent by construction; no migration for the row. |
| `src/app/(protected)/crm/(pages)/kelganlar/page.tsx` `DoorView` map | Optional (2 lines): `formUrl`/`botUrl` = `null` when `door.key === 'agent'` (same pattern as `assignUrl` being `sayt`-only); `doors.tsx` already renders conditionally for `assignUrl`, check it tolerates null for the other two. Without this the agent door shows a public `/ariza?manba=agent` link and a bot deep link that work but misattribute human submissions to the agent. |
| `docs/LEAD-ADS.md` §1 (`manba` list) and §4.3 | Mention `agent` as machine-only; point to the new `docs/AGENT-FEED.md`. |
| i18n | None: keys render raw; `lead_sources.name` is the human label. `tests/unit/i18n-keys.test.ts` unaffected. |
| Channel | Use `channel: 'webhook'`, `sourceKey: 'agent'` (the existing route does this already). No CHECK widening, no migration. "Agent-sourced" = `lead_intakes.source_key = 'agent'`. |
| Secret | Minted in UI: **CRM → Kelgan arizalar → Reklama havolalari → Agent → «Yoqish va kalit yaratish»** (`kelganlar/actions.ts`, gated `admin.settings.manage`). Re-minting invalidates the old key immediately (`sourceWebhookSecret` is read per request). |

Tests to add/adjust:
- `tests/unit/inbound-source-keys.test.ts` (new, pure): `INBOUND_SOURCE_KEYS` contains `'agent'`; source-shape fence (comments stripped, #725) that every key in `INBOUND_SOURCE_KEYS` appears in `scripts/seed.ts` `KEYED` — the seed is the only thing that gives a key a row, and `sourceIdForKey` silently falls back to `other` when it is missing.
- `tests/integration/inbound-webhook.integration.test.ts` «the door directory» already loops over all doors; passes once the CI database has been seeded (CI order: migrate → seed → seed:demo → vitest → Playwright, per CLAUDE.md).

Behavioural consequences to decide with the owner:
- **Caps**: ≤200 agent arrivals/day (`SOURCE_CAP_PER_DAY`), ≤3 per phone/day. Excess is recorded as `dropped/capped`, visible in the outcomes feed. Raising it per source is a separate change (`inbound.ts` constant → per-source column).
- **Contact clock**: every agent lead starts the `inbound_contact_minutes` (default 15) clock and the super-admin reminder, because `announceArrival` exempts only `site`. At 50–200 leads/day this is a reminder flood. Option (a) keep (owner wants same-day calls); (b, recommended) one line in `announceArrival`: skip `startContactClock` when `a.sourceKey === 'agent'` — notifications to the assigned seller still go out.
- **`company`**: `InboundArrival` has no `company`; `leadSchema`/`createLead` accept it (`crm/service.ts:306,508`). Optional ≈6 LOC: `fromPlain` reads `company|firma`, `InboundArrival.company`, passed to `createLead`. Otherwise the agent sends `firma` as a field and the owner maps it to a custom lead field or leaves it in the note.

## 2. Suppression feed

### Options
| | (a) `GET /api/agent/suppression` behind the agent source's `X-ERP-Key` | (b) read-only Postgres role for the agent |
|---|---|---|
| Network | Already reachable (HTTPS through Caddy) | 5432 is not published; needs a port mapping or running the agent on the compose network, plus a LOGIN role + password in `.env` and `pg_hba` exposure |
| Blast radius of a leaked credential | Hashed tails + ids; revoke = one button on `/crm/kelganlar` (re-mint) | Raw `clients.phones`, `leads.name/phone/note`; `erp_ai_reader` is NOLOGIN and `SET LOCAL ROLE`-only, so a new role + GRANT migration per table would be needed; revoke = `ALTER ROLE` by hand on the server |
| Privacy stance | Server decides what leaves (HMAC tails, no names) | The agent (Python, Claude-assisted) reads names/phones; `no installation's private data` rule becomes the agent's job |
| Repo rules | Matches the existing door: fail-closed 404, constant-time compare, `json()` compression; everything testable in vitest | A `CREATE ROLE … LOGIN PASSWORD` cannot be in a migration (secret in repo) → manual ops step per installation |
| Cost | ~2 small routes + 1 module | migration + ops doc + agent-side SQL + connection pooling against a 10-connection app pool |

**Recommendation: (a).** Reuse the per-source secret (generated, never typed; already has UI, audit and revocation). A dedicated setting `agent_api_key` is rejected: a human-typed key gets reused, it would show on `/admin/settings`, and it costs 4 locale strings for nothing the webhook secret does not already give. One secret for push and read is acceptable because the same process does both; if separation is ever wanted it is a second column, not a redesign.

### Route
`src/app/api/agent/suppression/route.ts` (new):
- `export const dynamic = 'force-dynamic'`; `GET` only (Next runs GET for HEAD — harmless for a read; add explicit `HEAD` returning 200 empty if the ETag cost matters).
- Auth: `const secret = await sourceWebhookSecret('agent')`; 404 when null; `secretMatches(request.headers.get('x-erp-key'), secret)` else 404 + `logger.warn` (same words as the POST door; no body key for GET).
- Params: `since` (ISO, optional), `cursor` (opaque `base64url("<iso>|<uuid>")`, optional), `limit` (default 1000, max 5000).
- Answer via `json(request, payload, { headers: { 'Cache-Control': 'private, no-store' } })`.
- Catch everything → 503 `{error:'unavailable'}`; never 500 with a stack.

### Query (one statement, `src/modules/wms/crm/agent-feed.ts`, new)
```sql
SELECT h, kind, at, id FROM (
  SELECT right(regexp_replace(p, '[^0-9]', '', 'g'), 9) AS tail, 'client' AS kind,
         greatest(c.created_at, c.updated_at) AS at, c.id
    FROM clients c, jsonb_array_elements_text(c.phones) AS p          -- active AND inactive
  UNION ALL
  SELECT right(regexp_replace(coalesce(l.phone,''), '[^0-9]', '', 'g'), 9), 'lost',
         coalesce(l.closed_at, l.updated_at), l.id
    FROM leads l JOIN lead_stages s ON s.id = l.stage_id
   WHERE s.kind = 'lost' AND lower(trim(l.lost_reason)) IN (<labels>)   -- sql.join / inArray, never a JS array in raw sql
) x WHERE length(tail) = 9 AND (at, id) > (<cursor_at>::timestamptz, <cursor_id>::uuid)
ORDER BY at, id LIMIT <limit+1>
```
Hashing in JS: `h = hmac_sha256(key = agentSecret, msg = tail9).hex.slice(0, 20)`. Dates bound as ISO strings with `::timestamptz` (#156); `numeric`/`count` → `Number()` (#503); no transaction, so no pooled-helper trap (#714); `ORDER BY` is mandatory for the keyset (#524); one grouped/UNION query, not per-row (#432).

Labels come from setting `agent_no_contact_reasons` (see §5), split on `;`, lower-cased/trimmed. Inactive clients are included on purpose (a former client is still "bizning mijoz"); this is wider than `activeClientsByPhone` — say so in the module comment.

### Response
```json
{ "mode": "delta|full", "generated_at": "2026-10-23T09:00:00Z",
  "salt_id": "a1b2c3d4",                      // first 8 hex of sha256(secret): agent re-syncs fully when it changes
  "rule": "hmac-sha256(secret, last9digits) hex[0:20]",
  "entries": [ { "h": "…20 hex…", "kind": "client|lost", "at": "…" } ],
  "next": "<cursor>|null" }
```
- **Hashed, not raw**: the agent's cache/logs then do not contain the customer book in clear. Honest limit: a 9-digit space is enumerable by anyone holding the secret — hashing protects against accidental exposure (a dump in a log, a repo), not against the secret holder, who is authorised anyway.
- **Deletions cannot be expressed as a delta** (a phone removed from a client, a lead revived out of lost). Contract: `since`/`cursor` is an optimisation; the agent does a **full pull nightly** (`mode:"full"`, no `since`; ~tens of thousands of rows, gzipped by `json()`) and deltas hourly. Entries are additive within a delta.
- Optional per-lead `do_not_contact` flag: **not now** (needs a migration + a lead-card control + i18n in 4 locales). The label rule covers the owner's stated workflow; the column is the later upgrade if a reason-independent flag is wanted.

## 3. Outcomes feed

`src/app/api/agent/outcomes/route.ts` (new), same auth/params/`json()` shell as §2. Query in `agent-feed.ts`:

```sql
SELECT i.id AS intake_id, i.external_id, i.outcome, i.reason, i.created_at,
       i.lead_id, coalesce(l.client_id, i.client_id) AS client_id,
       s.kind AS stage_kind, s.name AS stage_name, l.lost_reason, l.closed_at,
       i.contacted_at, i.contact_kind,
       l.quoted_volume_m3,                       -- Number() in JS
       greatest(i.created_at, i.contacted_at, l.updated_at) AS at
  FROM lead_intakes i
  LEFT JOIN leads l        ON l.id = i.lead_id
  LEFT JOIN lead_stages s  ON s.id = l.stage_id
 WHERE i.source_key = 'agent' AND i.channel = 'webhook'
   AND i.created_at > now() - interval '1 year'           -- lets lead_intakes_source_idx carry the scan
   AND (greatest(i.created_at, i.contacted_at, l.updated_at), i.id) > (<cursor_at>, <cursor_id>)
 ORDER BY at, i.id LIMIT <limit+1>
```
Response rows: `{ external_id, intake_id, outcome, reason, lead_id, client_id, stage_kind: 'open'|'won'|'lost'|null, stage_name, lost_reason, closed_at, contacted_at, contact_kind, quoted_volume_m3, at }`.

Semantics the agent must know:
- `outcome` is the **landing** verdict (`created`/`joined`/`client`/`dropped`); `stage_kind` is the **current** state. `joined` points at a pre-existing open lead; `client` has `client_id` and no lead; `dropped` (`no_contact`/`capped`/`suppressed`) has neither.
- A replay (same `external_id`) writes no new row → the original row is the truth; `external_id` must be stable per candidate (e.g. `agent:<sha1(normalised phone)>`).
- `contacted_at`/`contact_kind` come from the first-contact rule (`src/modules/wms/crm/first-contact.ts`): a call, a Telegram message, a note by the responsible seller, a stage move, or «✓ Bajarildi».
- Won: `stage_kind='won'` and `client_id` set (`winLead` converts; a lead row stays).
- **Privacy**: no `name`, `phone`, `note`, `owner_id`, `quoted_amount` (money sight rules, `rbac/money-sight.ts`). `stage_name` and `lost_reason` are the company's own words, not personal data.

## 4. Routing `agent` ≥ 1 m³ → cargo managers

UI: **Boshqaruv → Arizalar taqsimoti** (`/admin/taqsimot`, `admin.settings.manage`; `src/app/(protected)/admin/taqsimot/page.tsx` → `route-list.tsx` form: `source` select lists `INBOUND_SOURCE_KEYS` raw, so `agent` appears after §1; `keyword` blank; `min_m3 = 1`; `max_m3` blank; tick the cargo managers; move the rule above any catch-all with ↑↓). Stored in `inbound_routes` (numeric → `Number()` in `listRoutes`).

Because a windowed rule skips unknown volume (`matchRoute`), add a second rule `source=agent`, no window → a fallback pool (or let it fall to the general rota — owner's choice). Rules are read top-down, first match wins.

`kub` → `quoted_volume_m3`: the agent sends the volume as its own body key, e.g.
```json
POST /api/leads/in/agent   X-ERP-Key: <secret>
{ "external_id": "agent:…", "name": "…", "phone": "+998…", "note": "…", "kub": "3", "firma": "…" }
```
`fromPlain` turns `kub` into a field pair → it shows under **Forma savollari → Kelgan savollar** (`field-map-panel.tsx`, `seenKeys`, 30-day window) → owner maps `kub → kub (m³)` once (`saveMapping`, target `'kub'`) → `applyFieldMap` → `mapped.volumeM3` drives `routeInboundOwner` and is written to `leads.quoted_volume_m3` (`toFixed(3)`). `parseMeasure` accepts `"3"`, `"3 kub"`, `"10,5"`, `"5-10"`→5, refuses > 100 000. A volume only in free text (`textVolume`) routes but is never written to the card. **Send one test arrival, map `kub`, then start the real run** — arrivals before the mapping exists route unwindowed.

## 5. The lost reason

- Owner-editable: **CRM → ⋯ → Sozlamalar** (`/crm/settings`, `crm.manage`; `src/app/(protected)/crm/(pages)/settings/page.tsx`, `LostReasonForm` in `./forms`, service `saveLostReason` in `crm/service.ts`, unique on `lower(label)`). The owner adds the label (e.g. `Qayta qilinmasin`). Do **not** seed it: `lost_reasons` non-empty switches `reasonAllowed` from free text to list-only for every lost move in that installation.
- Configuration: new setting `agent_no_contact_reasons: 'Qayta qilinmasin'` in `src/modules/platform/settings/service.ts` `SETTING_DEFAULTS` (string, `;`-separated, case-insensitive match) + `settings.descriptions.agent_no_contact_reasons` in `messages/ru.json`, `uz.json`, `zh-CN.json`, `en.json` (otherwise `/admin/settings` throws at render — `tests/unit/locales.test.ts`). Alternative (`lost_reasons.no_contact boolean` column, migration `0132_lost_reason_no_contact.sql`, journal `idx 132, when 1785190000111`, checkbox in `LostReasonForm`, feed query behind `isServerBehind` on deploy morning #472) is cleaner long-term but larger; not in the minimal set.
- `leads.lost_reason` stores the label **text** chosen at the move (`stageWrite` trims; `moveLead` writes it; moving out of lost writes `null` and clears `closed_at`).
- Exclusion from future joining: the ERP does **not** suppress a lost phone today — `inboundMatch` looks at open stages only, so a lost lead's phone that arrives again opens a **new** lead (`docs/LEAD-ADS.md` §5). Therefore: (i) the agent must check the feed before pushing; (ii) **recommended belt** (~15 LOC in `landInboundLead`, `sourceKey === 'agent'` only, before the client step): if the phone tail is in the suppression set (`isSuppressedTail(tail)` from `agent-feed.ts`, one indexed read: clients' phones EXISTS + lost leads with listed reasons), `record('dropped', { reason: 'suppressed' })` and return — response stays `{ok:true}`, the outcomes feed shows `dropped/suppressed`. Adverts and the public form keep today's behaviour.
- Pitfalls: same person, new phone (a lead has one `phone`; a second number in the note is not matched; `person_id`/`crm_people` exists only for linked clients); renaming the suppress label (old leads keep the old words — match case-insensitively, and tell the owner to deactivate+create rather than rename, or keep the old label in the setting list); a revived lead drops out of suppression (correct); tails < 9 digits never match; a deal lost with a reason (`deals.lost_reason`) belongs to a client already suppressed via `clients.phones`; `activeClientsByPhone` prefilters on 7 digits, the feed uses 9 — document both.

## 6. Size, migration, risks

Files (≈ 9–11, ≈ 350–450 LOC incl. tests):
1. `src/modules/wms/crm/inbound.ts` — +1 key; optional suppression branch (~15 LOC).
2. `scripts/seed.ts` — +1 `KEYED` line.
3. `src/modules/wms/crm/agent-feed.ts` — new: `suppressionPage()`, `outcomesPage()`, `isSuppressedTail()`, `hashTail()`, cursor codec, label parsing (~150 LOC).
4. `src/app/api/agent/suppression/route.ts`, `src/app/api/agent/outcomes/route.ts` — new, ~45 LOC each.
5. `src/modules/platform/settings/service.ts` + 4 × `messages/*.json` — one setting + descriptions.
6. `src/modules/wms/crm/inbound-notify.ts` — optional one-line clock exemption for `agent`.
7. `src/app/(protected)/crm/(pages)/kelganlar/page.tsx` — optional door-link hiding.
8. `docs/AGENT-FEED.md` (new: contract for the Python side — auth, phone rule, HMAC rule, full/delta cadence, caps, `external_id` rule, body shape) + `docs/LEAD-ADS.md` touch.
9. Tests: `tests/unit/inbound-source-keys.test.ts`; `tests/unit/agent-feed.test.ts` (pure: HMAC/cursor/label parsing, `secretMatches` reuse); `tests/integration/agent-feed.integration.test.ts` (fixture: a client with a phone, a lost lead with the listed reason, an open lead with another phone → feed holds exactly the first two; `since` cursor excludes older rows; suppressed landing writes `dropped/suppressed` and no lead; setting snapshotted in `beforeAll` and restored (#716); agent secret set to `null` in `afterAll` (#183); audited fixture users deactivated, never deleted). Each fix test shown red by string edit, never `git stash` (#430).

Migration: **none** in the recommended set. If the column variant is chosen: `0132_lost_reason_no_contact.sql`, journal `when 1785190000111` (must exceed `1785190000110`; `tests/unit/migration-journal.test.ts` fences order/holes/placeholders).

Risks (CLAUDE.md):
- i18n render-time throw for the new setting in any locale; `pnpm typecheck` before push (tests are typed only there, #1223).
- Raw-SQL traps: ISO + `::timestamptz` for dates, `sql.join`/`inArray` for the label list, `Number()` on `numeric`, `ORDER BY` for keyset, `db.execute` timestamps come back as text (#925).
- #714: feeds use plain `db` reads, no transaction; `getSetting` read before any work.
- Route-handler JSON uncompressed unless via `json()` (#752); feeds are private → `Cache-Control: private, no-store`.
- Configuration left behind by tests (secret, setting) changes what every later spec sees (#183); CI shares one database across vitest and Playwright.
- Constant `{ok:true}` means the agent only learns outcomes through the feed; caps (200/day/source, 3/day/phone) bound push volume; the contact-clock decision affects the super admin's reminder load.
- Adding `agent` to the allowlist also makes `/ariza?manba=agent` and `?start=ad_agent` valid public attributions (cosmetic; hide the links).
- `platform` never imports `wms`: both routes and `agent-feed.ts` live on the `wms`/app side; `settings` and `http/json` are platform imports into wms (allowed direction).