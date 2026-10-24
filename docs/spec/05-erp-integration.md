# 05 — ERP integration (fullprompt branch `agent-feed` + agent push/feeds)

Scope: both sides of the ERP contract. **Part A is what was built** on fullprompt branch
`agent-feed` (head `23fe1736`, 2026-10-24) — a summary for the Python side, not a proposal.
**Part B** is `clientscrapper/services/crm_push.py` and `services/erp_feeds.py`, written to consume
Part A. Tables referenced on the agent side (`push_log`, `outcomes`, `suppression`,
`lead_candidates`, `sources.config`) are defined in `02-data-model.md` §2.8–2.11; this file does not
restate their DDL.

Order of truth for the wire: the ERP code on `agent-feed` (`src/modules/wms/crm/agent-feed.ts`,
`agent-feed-door.ts`, `agent-source.ts`, `inbound.ts`, `inbound-webhook.ts`, `first-contact.ts`,
`src/app/api/agent/*/route.ts`, `src/app/api/leads/in/[source]/route.ts`), then fullprompt
`docs/AGENT-FEED.md`, then this file. When they disagree this file is the one corrected.

## 0. Pre-build memo audit — `docs/erp-ozgarishlar-memo.md` against fullprompt (2026-10-23, kept for the record)

The memo was written against LogisticsERP; this table is the audit that preceded the build. Every
consequence in it is now either built (Part A) or superseded by it — where a row and Part A
disagree (the `X-ERP-Key` name, the seed's sort order 90, the «always `{ok:true}`» answer, the
UNVERIFIED `company` pass-through), **Part A wins**.


| Memo assumption | fullprompt reality | Consequence |
|---|---|---|
| Key header `X-ERP-Key` | `request.headers.get('x-gsr-key')` (`src/app/api/leads/in/[source]/route.ts:63`); `docs/ADS.md` §5.3 documents `X-GSR-Key` | **Canonical header is `X-GSR-Key`.** The new GET routes read the same header. The agent sends both `X-GSR-Key` and `X-ERP-Key` with one value (`ERP_KEY_HEADERS`), so the shared contract's name also holds. |
| `docs/LEAD-ADS.md` | Does not exist. The owner doc is `docs/ADS.md` (Uzbek; §1 `manba` list, §5 webhook, §5.3 body shape) | Doc touches go to `docs/ADS.md` §1 and §5.3; the contract goes to a new `docs/AGENT-FEED.md`. |
| Journal tail `0131_calc_excise`, `when 1785190000110`, 132 files | Tail is `0135_load_mode`, idx 135, `when 1785190000114`; 136 SQL files | Still **no migration** in the recommended set. If one is ever minted: idx 136, `when 1785190000115`. |
| 4 locales | Confirmed: `messages/{ru,uz,zh-CN,en}.json`; `tests/unit/locales.test.ts:199-210` fails when any `SETTING_DEFAULTS` key lacks `settings.descriptions.<key>` | 4 description strings for the new setting. |
| `agent` key absent | Confirmed: `INBOUND_SOURCE_KEYS = ['meta','instagram','facebook','telegram','tiktok','google','sayt','other']` (`inbound.ts:40-50`) | Add it. |
| `json()` helper | Confirmed: `src/modules/platform/http/json.ts`, `json(request, data, { headers?, status? })` — ETag/304 + gzip ≥ 1 KB | Both feeds answer through it. |
| Contact clock in `announceArrival` | Confirmed: `inbound-notify.ts` `startContactClock(intakeId, now)` writes `contact_clock_at` AND `contact_due_at`; `remindUntouched`/`claimUntouched` need `contact_due_at IS NOT NULL`; **`stampFirstContacts` needs `contact_clock_at IS NOT NULL`** (`first-contact.ts`) | The memo's «skip `startContactClock` for agent» is **wrong for this contract**: without a clock, `contacted_at`/`contact_kind` are never stamped and the outcomes feed can never report a contact. Fix: start the clock, write `contact_due_at = NULL` (§A.4). |
| Seed `KEYED` rows | Confirmed `scripts/seed.ts:280-289` (`tiktok` 45, `google` 55, `sayt` 80, `other` 200) | `{ key: 'agent', name: 'Agent (qidiruv)', sortOrder: 90 }`. |
| `SETTING_VALIDATORS` | Exists (`settings/service.ts:317`), used by `lead_assign_origins` | Add a validator for the new setting. |
| `lead_intakes.reason` free text | Confirmed (no CHECK) | `reason = 'suppressed'` needs no migration. |
| `lead_intakes_source_idx (source_key, created_at)` | Confirmed (`0066_inbound_leads.sql:38`) | Carries the outcomes query. |
| Route convention | `export const dynamic = 'force-dynamic'` in `api/lead/assign/route.ts` | Same on both GET routes. |
| Note length on the card | `landInboundLead` trims `note` to 1000 chars **after** `fromPlain` appended `key: value` lines for every extra body key; the manager's Telegram push shows `clipText(note, 160)` (`inbound-text.ts:14`) | Agent note ≤ 700 chars, first line ≤ 160 chars and self-sufficient (§B.1). |
| `InboundArrival.company` | Absent; `leads.company` column exists | Optional 6-line addition (§A.3); UNVERIFIED — grep `company` in `leadSchema`, `src/modules/wms/crm/service.ts`, before relying on it. |

Facts confirmed unchanged: landing order (client → open lead ≤ 30 d → new lead), replay fence
`(channel, external_id)` with no second ledger row, `PHONE_CAP_PER_DAY = 3`, `SOURCE_CAP_PER_DAY = 200`,
constant `{ok:true}` answer, 404 on missing/wrong key, `inboundPhoneDigits` = last 9, clients'
phones in `clients.phones jsonb`, `activeClientsByPhone` prefilters on 7 digits then
`phoneBelongsToClient`, `lost_reason` is TEXT and `reasonAllowed` makes free text illegal once the
list is non-empty, `matchRoute` skips windowed rules on unknown volume, CI order
typecheck → lint → migrate → seed → seed:demo → vitest → build → Playwright, compose `migrate`
runs `pnpm db:migrate && pnpm db:seed`.

---

## A. fullprompt — what was built on `agent-feed`

### A.0 Status (2026-10-24)

- Repository `bekzodqodirov/fullprompt`, branch `agent-feed`, pushed to `origin/agent-feed` at
  **`23fe1736`** (2026-10-24 08:31 UTC). Ten commits over `7bea3051` (the merge of PR #129, the tip of
  `claude/gsr-logistics-wms-phase1-o8h4en`): the build (`98437a75`, `2ebafe83`, `fec8e1b4`,
  `0bd4bcdc`, `abcc9e6a` — DECISIONS #1455–#1459), the first review's fixes (`70a28720`, `c6537cae`,
  `f2d96526` — #1460–#1466) and the second review's fixes (`aefbf278`, `23fe1736` — #1467–#1472).
- **No PR is open, nothing is merged, nothing is deployed.** No migration: the journal stays at 136
  files, so a deploy is checked by the build stamp at `/api/version`, not by the migration count.
- 40 files, +3686 / −61. The wire contract's ERP-side home is `docs/AGENT-FEED.md` (402 lines; §0 the
  owner's five steps in Uzbek, §1 the door, §2 the landing, §3 the feeds, §4 privacy, §5 honest limits,
  §6 the «Boshqa» fallback).
- Gates of the last round: a freshly recreated `gsr_ci` migrated (136 = 136), seeded and demo-seeded;
  `pnpm typecheck` and `pnpm lint` exit 0; full vitest 675 files / 7629 tests green; 16 red proofs by
  string edit, all red first time. Playwright was not re-run in round 2 (no UI change); round 1 ran
  `pnpm build`, the bundle host sweep and five e2e specs (16/16) and looked at the doors card at 360 px.

### A.1 File map as built

| File | What it holds now |
|---|---|
| `src/modules/wms/crm/agent-source.ts` (new) | `AGENT_SOURCE_KEY = 'agent'`, `isAgentSource()`, `personPickableSources()` (lead forms never offer «Agent (qidiruv)» except as a record's own stored value). No imports, on purpose (an import cycle through `inbound-notify` → `first-contact` would read the constant in its temporal dead zone). |
| `src/modules/wms/crm/inbound.ts` | `'agent'` in `INBOUND_SOURCE_KEYS` (between `sayt` and `other`); `inboundSourceKey(raw, channel)` — `agent` only on channel `webhook`, else `other`; `formSourceKey()` — `/ariza?manba=agent` is «Sayt»; `landInboundLead` = a per-`(channel, external_id)` in-process chain around `landOnce`; the agent's `no_phone` drop, per-family phone cap, belt, `company` onto the card; every text cut by code points (`clipCodePoints`); `ref` stored through `wellFormedJson`. |
| `src/modules/wms/crm/inbound-webhook.ts` | `InboundFields.company` read from `company` then `firma` (key names case-insensitive), **beside** — not instead of — the note line and question pair those keys always produced. |
| `src/modules/wms/crm/agent-feed.ts` (new, 524 lines) | `hashTail`, `saltIdFor`, `HASH_RULE`, the cursor codec, `parseSince`, `clampLimit`, `noContactReasons()`, the derived suppression table (`suppressionTailsSql`), `suppressionPage`, the belt `isSuppressedTail`, `outcomesPage`. |
| `src/modules/wms/crm/agent-feed-door.ts` (new) | `agentFeedResponse(request, feed)` (`feed` = `'suppression'` or `'outcomes'`) — the one GET door both routes share (key, 400/404/503, body, `Cache-Control`). |
| `src/app/api/agent/suppression/route.ts`, `src/app/api/agent/outcomes/route.ts` (new) | `export const dynamic = 'force-dynamic'`; `GET` delegates to the door. |
| `src/app/api/leads/in/[source]/route.ts` | The agent's door answers **503** when the landing (or, with `X-GSR-Key` sent, the key lookup) throws; every other door unchanged. |
| `src/modules/wms/crm/first-contact.ts` | `REMINDER_EXEMPT_SOURCES = {agent}`, `isReminderExempt()`, `CONTACT_LOOKBACK_DAYS = 7`, `EXEMPT_CONTACT_LOOKBACK_DAYS = 90`, `JOINED_FRESH_CONTACT_HOURS = 24`; `stampFirstContacts` looks at an unstamped exempt arrival for 90 days. |
| `src/modules/wms/crm/inbound-notify.ts` | `startContactClock(intakeId, now, remind)`; `announceArrival` passes `!isReminderExempt(sourceKey)`. |
| `src/modules/wms/crm/inbound-contact-jobs.ts` | Comment only: the 90-day branch and its measured cost. |
| `src/modules/wms/crm/analytics.ts` | /crm/tahlil's «Birinchi aloqa» median takes only contacts within `CONTACT_LOOKBACK_DAYS` of the clock (late agent contacts stay in the late counts). |
| `src/modules/wms/crm/field-map.ts`, `src/modules/wms/crm/service.ts` | Code-point cuts in `normalizeKey`/`capturePairs` and in `addActivity`'s audit excerpt. |
| `src/modules/wms/client-cabinet/service.ts` | `activeClientsByPhone` reads a non-array `clients.phones` as no phones (one such card used to fail every arrival). |
| `src/modules/platform/text/clip.ts` (new) | `clipCodePoints`, `wellFormed`, `wellFormedJson`. |
| `src/modules/platform/settings/reason-list.ts` (new), `settings/service.ts`, `src/app/(protected)/admin/settings/page.tsx` | Setting `agent_no_contact_reasons` (default `Qayta qilinmasin`), its validator and the refusal sentence `badValueReasons`. |
| `messages/{ru,uz,zh-CN,en}.json` | `settings.descriptions.agent_no_contact_reasons` and `badValueReasons` in all four locales. |
| `scripts/seed.ts` | `{ key: 'agent', name: 'Agent (qidiruv)', sortOrder: 190 }` (just above «Boshqa» 200; 90 is «Oʻzi keldi»'s). |
| `src/app/(protected)/crm/(pages)/kelganlar/door-views.ts` (new), `doors.tsx`, `page.tsx` | The doors screen decided on the server: no form or bot link for the agent's door; a door's key sent only to `admin.settings.manage`. |
| `src/app/(protected)/crm/leads/new/page.tsx`, `crm/leads/[id]/page.tsx`, `src/app/(public)/ariza/page.tsx` | Person doors never claim the agent's source. |
| `docs/AGENT-FEED.md` (new), `docs/ADS.md` §1 §5.3, `DECISIONS.md` #1455–#1472, `CHANGELOG.md` (Uzbek) | Docs. |
| `tests/unit/agent-feed.test.ts`, `agent-feed-door.test.ts`, `agent-door-landing.test.ts`, `inbound-source-keys.test.ts`, `text-clip.test.ts`; `tests/integration/agent-feed.integration.test.ts` (1243 lines) | Tests (§A.12). |

`platform` imports nothing from `wms` (the reason parser lives in `platform/settings` because the
settings validator needs it).

### A.2 The push door — `POST /api/leads/in/agent`

**Key.** Header `X-GSR-Key: <the agent source's webhook secret>`. Only that header is read;
`X-ERP-Key` alone is a 404 (captured, `err_post_404_erp_key_only.txt`). With **no** `X-GSR-Key`
header the door also takes the key from a body field `key` or `google_key` (Google's shape) — so the
agent must never name a payload field `key`, `google_key`, `secret` or `token` (all four are also
dropped from the note, the pairs and `ref`).

**Body.** A JSON object (anything that is not JSON → 404, nothing recorded).

| Key | Read as | Limit (all cuts by **code points**) |
|---|---|---|
| `name` / `ism` | the lead's name; whitespace runs collapsed, trimmed | 120 |
| `phone` / `telefon` | identity = its last 9 digits; the card keeps the text as sent | fewer than 9 digits → `dropped/no_phone` |
| `note` / `izoh` | the first lenta note | see below |
| `external_id` / `id` | the replay key | — |
| `company` / `firma` (case-insensitive key; `company` first) | `leads.company` on a **created** lead — and also a note line and a question pair like any other key, so a joined push still shows it | 200 on the card |
| any other key | one `key: value` line appended to the note **and** a question pair for the mapping screen | 30 pairs, key 80, value 300 |

**The landed note** = the agent's `note`, then one `key: value` line per extra key in the body's own
key order, then **every whitespace run (line breaks included) collapsed to one space**, trimmed, and
cut at 1000 code points. The lenta shows `📣 agent` + a line break + that paragraph; the seller's
Telegram push shows `«<first 159 code points>…»` (`clipText(note, 160)`); `📦 N kub` appears in the push
only for a MAPPED `kub`. `ref` stores the whole body minus the four secret names, uncut. A lone UTF-16
surrogate the body carries is stored as U+FFFD wherever it lands. The `name` is not checked for a
minimum: an empty one becomes `agent · <digits>`.

**`external_id`** = `cs:<company_uuid>:<yyyymm>`; the outcomes feed reports only ids starting `cs:`.

**Answer** (captured in `post_*.json`, `err_post_404_*.txt`; the 503 is read from the code):

| Code | Body / type | Means |
|---|---|---|
| 200 | `{"ok":true}`, `application/json`, `cache-control: no-store` | the arrival was **recorded** — any outcome (created, joined, suppressed, capped, no_phone, no_contact) — **or** it is a replay of one that was |
| 503 | `{"error":"unavailable"}`, `application/json`, `cache-control: no-store` | the landing threw (or the key lookup threw while `X-GSR-Key` was sent): no ledger row — retry with the same `external_id` (a lead may already stand: the orphan, below) |
| 5xx HTML (Next's error page) | — | the key lookup threw and no `X-GSR-Key` was sent (never the agent) |
| 404 | `Not found`, `text/plain` | unknown source in the path, door off (no secret, or the `agent` source deactivated), key wrong or absent, body not JSON |

The outcome never changes the answer. Only this door says 503; every advert door and the «Boshqa»
fallback still answer 200 to a landing that threw.

**Replay fence.** A second `(channel='webhook', external_id)` is 200 and lands nothing — no lead, no
ledger row — once the first delivery FINISHED, whatever its outcome (a capped or suppressed first
delivery spends the month's id too). The fence is per channel across every source: an id first
posted through another webhook door makes the agent's push a replay. A delivery that arrives while
one of the same id is still running **waits for it** (a `Map<channel+id, Promise>` chain inside the
one app process) and is then that replay; across processes only the ledger's unique index stands
(one ledger row, not one lead). A landing that failed AFTER its lead was written (a database failure
half-way — the text cuts no longer throw) leaves that lead standing with no ledger row; the retry
JOINS it, so the outcome reads `joined` and the seller hears «🔁 Qayta yozdi» about a lead whose
«🆕» never came (AGENT-FEED.md §1, §5).

**Caps** (counted over the arrivals ledger, dropped arrivals included): 3 per phone per day and 200
per source per day → `dropped/capped`. The per-phone count is per **family**: an agent push through
this door counts only the agent's arrivals on that phone and never caps a person's own enquiry; a
push through the «Boshqa» fallback is an ordinary arrival and counts in the people's family.

**Who may be `agent`.** Only this webhook: `/ariza?manba=agent` lands under «Sayt», a bot
`?start=ad_agent` or the form posted by hand under «Boshqa» (`inboundSourceKey`), so a person never
inherits the belt, the silent clock or a row in the outcomes feed.

### A.3 The landing of an `agent` arrival (`landOnce`, in order)

1. no name and no phone of nine digits or more → `dropped/no_contact`;
2. replay (above) → nothing, 200;
3. fewer than nine phone digits → `dropped/no_phone`;
4. caps (§A.2) → `dropped/capped` — so a phone the belt would refuse, pushed a fourth time in a day,
   reads `capped`, not `suppressed` (captured: `post_capped.json`);
5. **the belt** (§A.8) → `dropped/suppressed`: nothing created, nobody told;
6. «already a customer» (`activeClientsByPhone`, active cards) → `client` — cannot fire for an agent
   push, because the belt is never narrower; it appears only through the fallback door
   (`post_fallback_client.json`);
7. the one open lead on the same last-9 within 30 days (`updated_at`) → `joined`: a note on it, its
   owner unchanged, `updated_at` touched, the owner pushed «🔁 Qayta yozdi». Two open leads on one
   number refuse to join (a new lead is created);
8. otherwise a **new lead**: owner by the taqsimot rules (`manba = agent`, the mapped `kub`, else the
   first «N kub|куб|m3|м3|m³» in name + landed note — `textVolume`), `next_action_at` = today
   (Tashkent), `company` = the body's `company`/`firma` cut at 200, mapped answers written in their
   own catch, the lenta note, then the ledger row, then «🆕 Yangi lid · Agent (qidiruv)».

The ledger row (`lead_intakes`) is written LAST on every path; the push to a person happens after it,
in its own catch, so a Telegram failure never changes the answer.

### A.4 The contact clock for agent arrivals

- `created` and `joined` arrivals get `contact_clock_at` with **`contact_due_at = NULL`**: the
  15-minute «⏰ tegilmagan» reminder never claims them (the owner's 2026-10-23 decision). A `joined`
  push into a conversation contacted within the last 24 h gets no clock at all (`contacted_at`
  stays null for ever). `client` and `dropped` get no clock.
- `stampFirstContacts` writes `contacted_at` / `contact_kind` from the earliest evidence after the
  arrival: `call` (calls app), `telegram` (a CONNECTED manager account, a chat owned by the lead),
  `note` (any lenta activity by the lead's people — the «📞 Bogʻlandim» button writes one), `stage`
  (a stage move), `followup` (a cleared follow-up). Earliest wins, so a late-uploaded call can move
  `contacted_at` earlier.
- An unstamped agent arrival is looked at for **90 days** (`EXEMPT_CONTACT_LOOKBACK_DAYS`); every
  other source's for 7. A contact on day 9–90 therefore DOES reach the outcomes feed; after day 90
  `contacted_at` can never arrive. Fallback (`other`) arrivals keep the 7-day look-back and are
  reminded.
- /crm/tahlil reads `contact_clock_at`/`contacted_at`, never the due time: agent leads are in a
  seller's late counts, and in the median only when the contact came within 7 days.

### A.5 The feeds door — `GET /api/agent/suppression`, `GET /api/agent/outcomes`

Header `X-GSR-Key` (the same key as the push door); never a query parameter. Query parameters, all
optional: `limit`, `since`, `cursor` (§A.6). Bodies ≥ 1 KB are gzipped when `Accept-Encoding: gzip`
is sent (httpx sends it); `Cache-Control: private, no-store`; an ETag is set (304 needs
`If-None-Match` equal to it — never in practice, the body carries `generated_at`).

| Code | Body | When | Agent |
|---|---|---|---|
| 200 | JSON (§A.7, §A.9) | — | — |
| 400 | `{"error":"bad_cursor"}` / `{"error":"bad_since"}` | a cursor or `since` the caller got wrong | bug: `feed_contract` code `bad_request`, never retried |
| 404 | `Not found`, text/plain | door off, key wrong or absent, `X-ERP-Key` only, unknown path — and any failure while **no** key was sent | `push_404` breaker |
| 503 | `{"error":"unavailable"}` | the database failed — at the key lookup (only to a caller that SENT `X-GSR-Key`) or during the page | back off and retry; never `push_404` |

### A.6 Cursor, `since`, `limit`

- **Cursor** (opaque; hand back `next` or `resume` verbatim): base64url **without padding** of
  `<at>|<id>|<n>` — `at` is the row's instant as postgres holds it, `YYYY-MM-DDTHH:MM:SS.ffffffZ`
  (six fraction digits, `Z`); `id` a lower-case uuid (a client card, a person, a lead, or for
  outcomes the `intake_id`); `n` the tail's position on its record (0 for outcomes). Keyset: suppression
  `(at, id, n)`, outcomes `(at, intake_id)`. Decoder: 2 or 3 parts (`n` defaults 0); `at` must match
  `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$` and survive a calendar round trip (`02-30` is
  refused), `id` a uuid, `n` 1–6 digits; anything else → 400 `bad_cursor`. Vector (07 §1.5):
  `2026-10-23T09:00:00.123456Z|0f3c1a2e-5b6d-4e7f-8a9b-0c1d2e3f4a5b|2` ↔
  `MjAyNi0xMC0yM1QwOTowMDowMC4xMjM0NTZafDBmM2MxYTJlLTViNmQtNGU3Zi04YTliLTBjMWQyZTNmNGE1Ynwy`.
  The cursor's instant is NOT the row's printed `at` (that is cut to milliseconds).
- **`next`**: the cursor after this page, `null` on the last page. Follow it until `null`, keeping the
  same `since` for the whole walk.
- **`resume`** (every body): the cursor of this page's last row — on the last page too; on an empty
  page, the cursor that was sent; `null` when the page is empty and no cursor was sent (keep the one
  you had). Captured: `suppression_page1/2.json`, `suppression_resume_empty.json`.
- **`since`**: a full ISO-8601 instant — date, `T`, `HH:MM`, optional `:SS` and up to six fraction
  digits, and `Z` or an offset (`±HH:MM` / `±HHMM`). Anything else — a date alone, a naive time, `0`,
  `yesterday`, `2026-02-30…` — is 400 `bad_since` (captured: `err_400_bad_since*.txt`). A `+` must be
  sent as `%2B` (an unencoded one arrives as a space → 400). Microseconds are kept; rows that moved
  **strictly after** it are returned. Suppression `mode` is `delta` with `since`, `full` without.
- **`limit`**: absent, `0` or not a number → 1000; below 1 → 1; above 5000 → 5000; floored.
- A walk started from a stored cursor **without** `since` is labelled `mode: "full"` but holds only
  the rows after the cursor (§B.2 never deletes on such a walk).

### A.7 The suppression feed

```json
{ "mode": "full", "generated_at": "2026-10-24T08:53:49.923Z", "salt_id": "7a659490",
  "rule": "hmac-sha256(secret, last9digits) hex[0:20]",
  "entries": [{ "h": "bbc9434b92f2f7f81776", "kind": "client", "at": "2026-10-24T08:53:22.872Z" }],
  "next": null,
  "resume": "MjAyNi0xMC0yNFQwODo1Mzo0OC42NDcwMDBafDAxYTE2ZDFkLTViMzctNzE4YS04ZTEyLWY5M2EwZDA3YjljMnwy" }
```

- `h = hmac_sha256(key=secret UTF-8, msg=tail9 ASCII).hexdigest()[:20]` (lower case); `salt_id =
  sha256(secret).hexdigest()[:8]` — it changes when the owner re-mints the key (every stored hash is
  then stale). `generated_at` and every `at` are ISO UTC with **milliseconds** and `Z`.
- `kind = client`: every phone on every client card, **active or inactive**; every phone on a
  PERSON record (CRM → Odamlar) that holds at least one client code; the phone of every lead that
  became a client (on a `won` stage, or carrying a `client_id` — attaching does not copy the lead's
  phone onto the card). `kind = lost`: a lead on a `lost` stage whose `lost_reason`, normalised, is
  one of the setting's labels (§A.10). Normalisation on both sides: every run of JavaScript `\s`
  (no-break space included) → one space, trimmed, lower case.
- **Which tails one phones value yields**: its whole last nine digits; the last nine of each part
  when it holds several numbers (split at `, ; / | \`, a line break, two spaces, or a space before
  `+`) — only a part of nine digits or more; and inside a part of more than fifteen digits (the
  client import keeps a multi-number cell as one run), every whole 12-digit `998…` number — or,
  only when the run is NOT made of those, every whole 9-digit number. A run that is neither is read
  by its last nine only. Only nine-digit tails are in the feed.
- `at` = when the row last moved: a card's `max(created_at, updated_at)`; a person's
  `max(created_at, updated_at, newest updated_at of the cards that hold it)` (tying a code to a person
  writes the card, so the person's numbers join the delta at that moment); a lead's
  `max(closed_at, updated_at)`.
- **Not deduplicated**: two cards, a card and its person, or a card and the won lead it was minted
  from, on one phone, are two entries with one `h` and different `at`s (captured: `534dd284…` twice —
  card GS100 and its won agent lead). Upsert on `(kind, h)`.
- **A delta cannot express a deletion**: a lead revived out of `lost`, a phone removed from a card, a
  label taken off the setting, a stage whose KIND the owner changes (open ↔ won ↔ lost — every lead on
  it joins or leaves with no `at` moving), and a label ADDED to the setting (its old leads come with
  their old `at`) are all seen only by a full pull.
- Every page recomputes the whole set (≈ 0.3 s at 17,000 entries): pull with `limit=5000`.

### A.8 The belt (`isSuppressedTail`)

The landing's own check, over the same derived table as the feed (one SQL sentence), narrowed to the
phone texts that contain the arrival's last seven digits (drops nothing it could match). It answers
`suppressed` when a tail equals the arrival's last nine, **or** a whole card/lead phone value of only
seven or eight digits equals the arrival's last seven or eight (the app's own `phonesMatch` rule).
So it is never narrower than step 6's client check — over inactive clients too — and a 7–8-digit card
(captured: `post_suppressed_legacy_card.json`, card «000-00-05») is refused although its number can
never appear in the feed as a hash. Cost ≈ 90 ms per arrival at 2,000 clients and 40,000 leads,
linear in the lead history.

### A.9 The outcomes feed

```json
{ "generated_at": "2026-10-24T08:53:50.027Z",
  "rows": [{ "external_id": "cs:…:202610", "intake_id": "…", "outcome": "created", "reason": null,
             "lead_id": "…", "client_id": null, "stage_kind": "open", "stage_name": "Yangi",
             "lost_reason": null, "no_contact": false, "closed_at": null,
             "contacted_at": "2026-10-24T06:02:00.000Z", "contact_kind": "note",
             "quoted_volume_m3": null, "at": "2026-10-24T08:53:44.339Z" }],
  "next": null, "resume": "…" }
```

Exactly **15 fields**:

| Field | Meaning |
|---|---|
| `external_id`, `intake_id` | the push and its ledger row (uuid) |
| `outcome` | `created` / `joined` / `client` / `dropped` |
| `reason` | for `dropped`: `suppressed` / `capped` / `no_phone` / `no_contact`; `null` otherwise. A replay writes no row, so `replay` never appears |
| `lead_id` | the lead it became or joined (uuid; `null` when dropped or `client`) |
| `client_id` | the lead's client once won or attached, else the client it was recognised as (uuid or `null`) |
| `stage_kind`, `stage_name` | the lead's stage NOW (`open` / `won` / `lost`, the owner's name for it); `null` without a lead |
| `lost_reason` | the text chosen at the move (≤ 200) **only when it is a label** — one of the owner's lost-reason dictionary rows (active or retired) or a label of `agent_no_contact_reasons`, compared normalised. A seller's free text is `null` (captured: `post_created_lost_free.json` → `null`) |
| `no_contact` | `true` when the lead is lost with a reason `agent_no_contact_reasons` lists — the ERP's own answer; the agent keeps no copy of the setting |
| `closed_at` | when the lead was decided |
| `contacted_at`, `contact_kind` | the FIRST contact after the arrival (§A.4): `call` / `telegram` / `note` / `stage` / `followup` |
| `quoted_volume_m3` | the lead's kub as a number (the agent's own `kub` echoed back once the owner mapped it), or `null` |
| `at` | when the row last moved: `max(arrival, first contact, any change on the lead, its decision)` |

All timestamps ms ISO `Z`. Rows: channel `webhook`, `external_id LIKE 'cs:%'`, arrived within the
last **year**, source `agent` — **or** `other` with `ref.utm_source = 'clientscrapper'` exactly (the
fallback months; permanent, so anybody holding the «Boshqa» key can post into it). Order and cursor
`(at, intake_id)`.

A cursor walk is **not complete on its own**: `contacted_at` is the evidence's time and evidence
surfaces late (a calls-app phone offline for a day, a Telegram history pulled a week back), a row
whose transaction committed after a read can carry an `at` that read already passed, and a stage
KIND flip moves no `at`. The ERP therefore prescribes three reads (AGENT-FEED.md §3.2), which §B.2
adopts: nightly **by cursor AND with `since = now − 8 days`**, weekly with `since = now − 90 days`
(the stamping horizon), monthly with **neither** `since` nor `cursor` (every row of the year — the
only read that catches a stage-kind flip everywhere).

`outcome = joined` reports the stage, kub and decision of a lead somebody else was already working,
even one from another source (stated by the ERP, not hidden). Never in a row: a name, a phone, a note,
an owner, a price.

### A.10 Setting `agent_no_contact_reasons`

Default `Qayta qilinmasin`. `;`-separated labels; `parseReasonList` normalises each (§A.7), drops
labels shorter than 2 characters and duplicates, never throws. `reasonListValid` refuses an empty
list and any label over 80 characters; the settings screen answers `badValueReasons` in four
locales. Read by the suppression feed, the belt and the outcomes `no_contact` / `lost_reason`
columns. A renamed dictionary reason keeps suppressing under its old spelling only while that
spelling stays listed (old leads keep the text chosen at the move).

### A.11 Seed, doors screen, person doors

- Seed row `{ key: 'agent', name: 'Agent (qidiruv)', sortOrder: 190 }`, written by the `migrate`
  service's `pnpm db:seed` on deploy.
- **CRM → ⋯ → Kelgan arizalar → «Reklama havolalari»**: the agent's door shows no `/ariza` and no bot
  link (a person through them is not the agent), only the webhook URL; its key is sent in the page
  data only to `admin.settings.manage` (it also READS the client phone hashes); others see `on/off`.
- Lead forms offer every source but the agent's (`personPickableSources`).

### A.12 Tests as built

Unit: `agent-feed.test.ts` (hash/salt vectors `k`/`901234567` → `2ea6869e504cebca50f0`, `k` →
`8254c329`; cursor round trip incl. 2-part and rolled dates; `parseSince`; `clampLimit`; reason
parser), `agent-feed-door.test.ts`, `agent-door-landing.test.ts` (503 on a landing / key-lookup throw,
200 elsewhere), `inbound-source-keys.test.ts` (seed fence, `inboundSourceKey`, `formSourceKey`,
`company` parse), `text-clip.test.ts`. Integration: `agent-feed.integration.test.ts` — full/paged/
delta feeds, kinds (active, inactive, person, won, attached, listed lost), tail splitting and the
998-run rule, the belt incl. the 7–8-digit suffix, per-family caps, `no_phone`, `company` on the
card, the clock (`contact_due_at NULL`, 90-day stamp, joined-live), the outcomes window, labels only,
`no_contact`, the fallback clause, the in-flight chain, the emoji cuts at 120/300/200 code points; it
walks the full feed (green on a 17,000-entry copy). Red proofs: 24 (round 1) + 16 (round 2).

### A.13 ERP-stated limits the agent designs around

- Nine digits are a small space: the key holder can enumerate the hashes back to tails. The hash keeps
  the list out of the agent's logs, backups and screenshots; it is not encryption against the key.
- Entries carry their card's `at`, so hashes that share an `at` share a card — phone groupings leak to
  the key holder too.
- A Chinese mobile's last nine digits on a client card also suppress the unrelated Uzbek number that
  ends the same way (fail-closed).
- A lead revived out of `lost` leaves the suppression set (correct: somebody decided to work it).
- The same company on a new phone number is a new phone to the ERP.
- A contact from a personal Telegram account whose chat sits undecided on the tray is not reported.
- The fallback clause is permanent: ignore outcome rows whose `external_id` the agent never pushed.
- A landing that fails half-way leaves an orphan lead the retry joins (§A.2).
- The in-flight fence is per app process (production runs one).
- Re-minting the key rotates `salt_id` and makes the old key 404 at once on every route; deactivating
  the `agent` source on the doors screen makes everything 404.

### A.14 Still to do on the ERP side, and the owner's steps

Before production (fullprompt's `CLAUDE.md` rules): open the PR from `agent-feed` with an Uzbek
summary (no model identifier anywhere); reproduce CI's order on a fresh database once more after any
rebase; deploy by `docs/UPDATE.md` — verified non-zero backup, `docker compose build migrate`, then
`docker compose --profile https up -d --build`; end by reading the build stamp at `/api/version`
(no migration, so the count proves nothing). The seed row appears on that deploy.

Owner's steps after the deploy (Uzbek; the same five as AGENT-FEED.md §0):

1. **CRM → ⋯ → Kelgan arizalar → «Reklama havolalari» → «Agent (qidiruv)» → «Platformaning oʻz
   formasi» → «Yoqish va kalit yaratish».** Chiqqan kalitni agent serveridagi `.env` faylga
   `CRM_AGENT_KEY` qilib qoʻying (chatga yozmang). Kalitni qayta yaratsangiz, eskisi darhol ishlamay
   qoladi va agent butun roʻyxatni qaytadan oladi.
2. **CRM → ⋯ → CRM sozlamalari → «Yoʻqotish sabablari»**: «Qayta qilinmasin» sababini qoʻshing.
   **Diqqat:** roʻyxat hozir boʻsh boʻlsa, birinchi sabab qoʻshilgan zahoti lidni yoʻqotishda faqat
   roʻyxatdagi sabablarni tanlash mumkin boʻladi — jamoa hozir ishlatadigan sabablarni ham oʻsha
   zahoti qoʻshing. Roʻyxatdagi sabab nomini keyin oʻzgartirmang: kerak boʻlsa eskisini oʻchirib,
   yangisini qoʻshing (yoki yangi yozuvni 5-qadamdagi sozlamaga ham qoʻshing).
3. **Boshqaruv → Arizalar taqsimoti → «Oqim qoidalari»**: A qoida — manba `agent`, «Kub (dan)» = 1,
   kimga = yuk boʻyicha sotuvchilar; B qoida — manba `agent`, kubsiz, kimga = qolgan sotuvchilar
   (yoki umumiy navbatga qoldiring). A ni B dan yuqoriga, ikkalasini ham umumiy qoidalardan yuqoriga
   qoʻying.
4. Agent bitta sinov arizasini yuboradi (`make push-test`). Keyin shu ekranning **«Forma savollari»
   → «Reklamadan kelgan yangi savollar»** roʻyxatida `kub` → **Kub (m³)** qiling; xohlasangiz
   `stir`, `imports`, `score` ni lidning maxsus maydonlariga bogʻlang (avval maydonni yarating);
   `firma`, `telegram`, `instagram`, `website`, `address`, `evidence`, `utm_source` → **«Izohda
   qolsin»** (firma nomi lidning «Kompaniya» maydoniga baribir yoziladi).
5. **Boshqaruv → Sozlamalar → `agent_no_contact_reasons`**: 2-qadamdagi sabab nomi bilan bir xil
   ekanini tekshiring (standart: `Qayta qilinmasin`; bir nechta boʻlsa `;` bilan).

Until `kub` is mapped (step 4), routing reads the first «N kub» of name + landed note
(`textVolume`), so a note that states «2 kub» still routes by rule A; a band with no number («fura»,
«konteyner») routes by rule B. (AGENT-FEED.md §0 says «kubsiz qoida (B)» for all of them — the code
reads the text first.)

---

## B. Agent side

### B.1 `services/crm_push.py`

```python
ERP_KEY_HEADERS = ("X-GSR-Key", "X-ERP-Key")          # one value; the ERP reads X-GSR-Key only (A.2) — X-ERP-Key is the shared contract's old name, sent and ignored
PUSH_PATH = "/api/leads/in/{source}"                   # source = settings.crm_push_source ('agent' | 'other')
UTM_SOURCE = "clientscrapper"
RESERVED_BODY_KEYS = frozenset({"key", "google_key", "secret", "token"})   # the ERP reads/drops these as the shared secret (A.2): never a payload field
LANDED_NOTE_MAX = 1000  # ERP: note + one 'key: value' line per extra key, whitespace collapsed, cut at 1000 code points
NOTE_MAX = 700          # our own note's budget inside it
PUSH_PREVIEW = 159      # the seller's Telegram push shows «<first 159 code points>…» of the landed note
QUOTE_MAX = 140
OPENER_MAX = 220
NAME_MAX = 120          # after the ERP's whitespace collapse; every limit here is measured by text_len() below
COMPANY_MAX = 200       # leads.company on a created lead
PAIR_KEY_MAX, PAIR_VALUE_MAX, PAIRS_MAX = 80, 300, 30  # ERP capturePairs
def text_len(s: str) -> int: return len(s.encode("utf-16-le")) // 2   # UTF-16 units ≥ code points: safe for agent-feed (cuts by code points) AND for the production build the «Boshqa» fallback posts to (cuts by UTF-16 units, and a cut emoji there throws after the lead is written)
PUSH_DAYS = range(0, 6)                                # Mon–Sat
PUSH_FROM, PUSH_TO = time(9, 30), time(17, 30)         # Asia/Tashkent (02 §2.10 item 2)
ATTEMPT_DELAYS = {2: timedelta(minutes=5), 3: timedelta(minutes=30)}   # between push_log attempts (the HTTP tries INSIDE one attempt are with_backoff's, below)
BAND_KUB = {"1_3": "1", "3_10": "3", "container": "28", "truck": "80"} # lower bound when no stated m³: managers read `kub`, so the low end of 04 §4.3 (20ft ≈ 28–33, fura ≈ 80–90)

class PushPayload(BaseModel, frozen=True):
    # FIELD ORDER IS THE JSON BODY ORDER, which is the order of the ERP's note lines (A.2):
    # utm_source comes right after the note so that the fallback door's keyword rule always sees it
    # inside the 1000-code-point cut, and so that no digit at the end of the note can be read by
    # the ERP's textVolume as «<n> kub» together with the next line.
    external_id: str        # cs:<company_uuid>:<yyyymm>, Tashkent month at claim time
    name: str               # company display_name (2..120 code points); never the person alone
    phone: str              # E.164 from contacts.phone; the ERP keeps the last 9
    note: str               # render_note(); ≤ NOTE_MAX
    utm_source: str = UTM_SOURCE
    kub: str | None         # plain number string ("2", "1", "28", "80" — stated m³ or BAND_KUB); omitted when unknown/<1
    firma: str | None       # display_name + legal_form → leads.company on a created lead (≤ 200), and a note line + pair
    stir: str | None
    telegram: str | None    # '@company' or the decision maker's handle only with legal_basis
    instagram: str | None
    website: str | None     # domain
    address: str | None     # ≤ 300
    imports: str | None     # 'mato, aksessuar' (goods, ≤ 6, joined)
    score: str              # '74'
    evidence: str           # '<url> — "<quote ≤ 100>"' ≤ 300

def external_id_for(company_id: UUID, now: datetime) -> str
def build_payload(c: CandidateView) -> PushPayload          # pure; raises NoPhone; every text through services/text.well_formed (no lone surrogate)
def render_note(c: CandidateView) -> str                     # pure; sections below
def landed_note(p: PushPayload) -> str                       # pure mirror of the ERP's assembly (A.2), for the length tests
def canonical_sha256(p: PushPayload) -> str                  # sorted keys, no spaces, None dropped
def push_window_open(now: datetime) -> bool                  # tz-aware Tashkent
async def push_one(session, candidate_id: UUID, *, now: datetime, erp: ErpClient, repush_of: int | None = None) -> PushResult
async def crm_push_job(session_factory, erp, *, now) -> JobReport     # worker: every 60 s inside the window
```

`landed_note(p)` = `" ".join((p.note + "\n" + "\n".join(f"{k}: {v}" for k, v in extras_in_order(p))).split())`
— Python's `str.split()` collapses whitespace runs like the ERP's `\s+` (the two classes differ on
U+FEFF/U+0085 only; `well_formed` keeps neither in a payload). `build_payload` guarantees
`text_len(landed_note(p)) ≤ LANDED_NOTE_MAX` by shortening `render_note` (the drop order below), so the
ERP never cuts; `name`, `firma` and every extra value respect the limits above by `text_len` — so
nothing is cut on either ERP build, and no cut can halve an emoji on the old one; no
field is named in `RESERVED_BODY_KEYS` (a test pins it over `PushPayload.model_fields`).

`CandidateView` is a read-only dataclass assembled by `services/candidates.load_view()` from
`lead_candidates` + `companies` + the one `contacts` row + top signals (with `evidence.url`,
`published_at`, `signals.quote`) + `score_breakdown` + the opener draft stored by 04 (`lead_reviews`
or `score_breakdown.opener`; UNVERIFIED — 04 decides where the draft lives).

**Uzbek note template** (sections in this order; a section with nothing to say is omitted; plain
text; apostrophes ʻ/ʼ; no price figures; no phone or handle beyond what the ERP body already carries).
The ERP collapses every line break to a space (A.2), so the line breaks below exist only for
`push_log.payload` readers: each section begins with its caps label, and the note reads as one
paragraph on the lenta.

```
NEGA TOPILDI: {source_title_uz} · {published_dd.mm.yyyy} · «{quote ≤ 140}»          ← the first 159 code points must stand alone (the seller's push)
{evidence_url}
NIMA OLIB KELADI: {goods joined ', '} · Xitoy: {ha|ehtimol} ({origin_hint})
HAJM: {stated «2 kub» | band label: 1–3 kub / 3–10 kub / fura / konteyner} · {per_month if known}
KOMPANIYA: {legal_name} · STIR {stir|—} · {city} · {registry_status_uz} · {sector}
  Masʼul: {full_name} ({role})                                                      ← only tier=person with legal_basis
NIMA DEYISH: {opener ≤ 220, from 04 or the segment template in docs/sotuv-playbook.md §3}
BALL: {score}/100 (hajm {v}, Xitoy {o}, firma {s}, yangilik {r}, aloqa {c}, tasdiq {k}{, minus … if any})
```

Length rules enforced in `render_note`/`build_payload` (tests pin them): the NEGA TOPILDI section is
first and never cut mid-word; total note ≤ 700 and landed note ≤ 1000 after sections are dropped in
reverse priority order (BALL detail → KOMPANIYA second line → NIMA DEYISH shortened to 120 → HAJM
per-month → `address` value cut to 120); the URL is never cut (a URL over 200 characters is replaced
by `evidence #<id>`); quote is a verbatim substring of `evidence.excerpt` (02 §2.6). A stated volume
is written as «N kub» in the HAJM section: until the owner maps `kub` the ERP routes by that text
(A.14), and a band without a number routes by rule B.

**Gate and transport** (02 §2.10 items 1–7 are the law for the gate; the attempt scheme and the
outcome mapping below are owned HERE — 02 §2.10 rule 4 and 07 §1.6 point to this table):

```python
RETRY_IN_ATTEMPT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)   # the request never reached the ERP
MAY_HAVE_LANDED = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError,
                   httpx.RemoteProtocolError)                                      # the ERP may still be running it, or be done
class ErpHttpError(Exception): status: int
class ErpBadBody(Exception): status: int      # a 2xx whose body is not {"ok": true} — never retried, the lead may have landed
def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, RETRY_IN_ATTEMPT) or (isinstance(exc, ErpHttpError) and exc.status >= 500)

async def _post(erp, source, payload) -> int:
    r = await erp.post_lead(source, payload.model_dump(exclude_none=True))   # json=, both key headers, timeout 20 s (connect 5 s)
    if 200 <= r.status_code < 300:
        try:
            if r.json().get("ok") is True: return r.status_code
        except ValueError:
            pass
        raise ErpBadBody(r.status_code)
    raise ErpHttpError(r.status_code)

# inside push_one, after the claim row (status='sending', attempt=n) is COMMITTED:
status = await with_backoff(lambda: _post(erp, source, payload), attempts=3, base=1.0, cap=8.0, retry_on=_retryable)
```

**Two levels, one scheme.** The three `with_backoff` tries above are HTTP tries *inside one
`push_log` attempt* (one claim row, ≤ 15 s, delays 1 s → 2 s → 4 s capped at 8 s), and they happen
**only when the ERP certainly did not land the push**: the connection never opened
(`RETRY_IN_ATTEMPT`), or the ERP answered 5xx — its 503 `{"error":"unavailable"}` means nothing was
recorded (A.2). After a read/write timeout, a read/write error or a protocol error
(`MAY_HAVE_LANDED`) there is **no resend inside the attempt**: the first request may still be
running, and although the ERP now chains a second delivery of the same id behind it (A.2), that
second request waits too and can time out the same way. The attempt ends `unknown` and the
reconcile path settles it (02 §2.10 rule 5). A further **attempt** (`attempt+1`, identical
`payload_sha256`) may be claimed only after a `failed` attempt and `ATTEMPT_DELAYS[n+1]` — 5 min
before attempt 2, 30 min before attempt 3 — so the worst case is 9 POSTs, and after attempt 3 the
candidate returns to `review`. Outcome mapping (the `push_log` CHECK of 02 §2.10 is
`(status='sent' ⇒ 2xx) AND (2xx ⇒ status IN ('sent','unknown'))`):

| Result | `push_log` | candidate | side effect |
|---|---|---|---|
| 200 `{"ok":true}` (recorded, any outcome — or a replay) | `sent`, `http_status` | `pushed`, `pushed_at`, `external_id` | — (the outcomes feed says what it became) |
| 2xx with any other body (HTML, empty, bad JSON) | `unknown`, `http_status`, `error='http_2xx_badbody'` | stays `approved`, blocked by `ux_push_log_delivered` | not retried (the lead may have landed); settled by 02 §2.10 rule 5 |
| `MAY_HAVE_LANDED` on any try | `unknown`, `error='timeout'` | stays `approved`, blocked | no resend; `reconcile_job`'s quick outcomes read (≤ 10 min, §B.2) promotes it, else rule 5's 48 h |
| 404 | `failed`, `error='http_404'` | stays `approved` | breaker: `sources.erp_sync.breaker_until = now+6h`, `breaker_reason='push_404'` (`/yoq crm` clears it by hand, 06 §1.2; it also expires); health problem `push_404`; owner alert once per 6 h — `reminder_log(kind='alert:push_404')`, recovery `recovered:push_404` (06 §4.1): «ERP kaliti yoki manba notoʻgʻri: /api/leads/in/{source} 404 qaytardi. Kelgan arizalar → Reklama havolalari → Agent → kalitni tekshiring.» |
| other 4xx | `failed`, `error='http_4xx'` | → `review`, note «ERP rad etdi (HTTP n)» | owner alert — the ERP door itself answers only 200 / 404 / 503, so this is a proxy or a wrong `CRM_BASE_URL` |
| 5xx (503 JSON or an HTML error page) / connect error after 3 tries | `failed`, `error` = `http_5xx`, `conn` or `timeout` (a connect timeout) | stays `approved`; next attempt after `ATTEMPT_DELAYS[n+1]`; after attempt 3 → `review`, note «CRM javob bermadi» | health problem `push_failing` when 3 consecutive candidates fail |
| `sending` row older than 10 min (crash) | `unknown` (`reconcile_job`) | stays `approved`, blocked | as `MAY_HAVE_LANDED` |

What 200 does and does not prove: on the `agent` door, that the ERP recorded the arrival (or had
recorded it before). On the «Boshqa» fallback door (§B.3) a 200 can also be a landing that threw
with nothing recorded — which is why rule 5 also reconciles `sent` rows. A 503 after the ERP's
`createLead` (a database failure half-way) leaves an orphan lead; the next try JOINS it and the
outcome reads `joined` for a lead nobody else was working (A.2) — the agent never reads `joined` as
«a person already had this company» (no side effect keys on it, 02 §2.11).

Pre-flight refusals (no HTTP, no `push_log` row, result codes): `window` (outside
Mon–Sat 09:30–17:30 Tashkent), `cap` (`count(sent|unknown today) ≥ PUSH_DAILY_CAP`), `disabled`
(`CRM_PUSH_ENABLED=false`), `breaker` (`sources.erp_sync.breaker_until > now`), `suppressed` (gate
re-check inside the `FOR UPDATE` transaction), `stale` (`suppression_stale`: `suppression_fresh()`
of §B.2 is false — feed `salt_id` ≠ stored, last successful full pull > 26 h or last delta > 3 h —
while the feed is configured), `no_phone`, `phone_recent` (same tail `sent|unknown` < 24 h),
`company_recent` (a `sent|unknown` row for this company whose outcome is `open` or absent and younger
than 90 d — the card's «90 kunda yuborilmagan» line, 02 §2.10 rule 2). Rows with
`superseded_at IS NOT NULL`, and rows of the **same `external_id`** as the push being made (a
rule-5 re-push), count for none of `cap`, `phone_recent`, `company_recent`. A `cap` or `window`
refusal is silent (the job runs again); every other refusal writes a `lead_reviews` note.

`make push-test` (`tools/push_test.py`): mints a **fresh synthetic company per run** — a new uuid4,
`display_name='TEST agent — oʻchiring'`, one company-tier `contacts` row with `TEST_PHONE`, an
`approved` candidate (`approved_by='owner:push-test'`, `score=0` with `score_breakdown.gates.test=true`
so no scorer or card ever touches it) — and pushes it through `push_one` with the score/tier gates
skipped and every other gate kept. So the `external_id` (`cs:<new uuid>:<yyyymm>`) is new on every
run — a fixed id would be a silent replay on the second run of a month — and the `push_log` row
exists, which the outcomes read needs (rows whose `external_id` the agent never pushed are ignored,
§B.2). It prints the HTTP status, the `external_id` and the company uuid (for `tools/purge`).
`TEST_PHONE` must be a number **outside the client book and outside every «Qayta qilinmasin» lead**,
or the push lands silently as `dropped/suppressed` (still 200); `make push-test` refuses to run when
the local gate already suppresses it. Used for step 4 of A.14 and 07 §5.6.

### B.2 `services/erp_feeds.py`

```python
FEED_RULE = "hmac-sha256(secret, last9digits) hex[0:20]"
SUPPRESSION_LIMIT = 5000      # every page recomputes the whole set on the ERP (A.7): few big pages
OUTCOMES_LIMIT = 1000
MAX_PAGES = 200               # 1,000,000 entries / 200,000 rows — past it: feed_contract code 'runaway'
PII_KEYS = frozenset({"phone", "telefon", "name", "ism", "note", "izoh", "owner", "owner_id",
                      "quoted_amount", "price", "amount", "digits", "tail"})   # contract breach if a feed row carries one

class ErpClient:                       # one httpx.AsyncClient; base_url=CRM_BASE_URL; key=CRM_AGENT_KEY
    def __init__(self, base_url: str, key: str, *, transport: httpx.AsyncBaseTransport | None = None)
        # headers on every request: X-GSR-Key and X-ERP-Key (same value), Accept-Encoding: gzip,
        # User-Agent clientscrapper/<version>; follow_redirects=False; the key never in a URL or a log line
    async def get_feed(self, path: str, params: dict[str, str | int]) -> dict   # timeout 30 s; raises ErpHttpError(status, code) for non-200, FeedShapeError for non-JSON
    async def post_lead(self, source: str, body: dict) -> httpx.Response        # timeout 20 s, connect 5 s; used by crm_push

def hash_tail(secret: str, tail9: str) -> str      # hmac.new(secret.encode(), tail9.encode(), sha256).hexdigest()[:20]
def salt_id(secret: str) -> str                    # sha256(secret.encode()).hexdigest()[:8]
def fmt_since(dt: datetime) -> str                 # aware → UTC → 'YYYY-MM-DDTHH:MM:SS.ffffffZ' (always Z, never '+')
def parse_entry(d: dict) -> FeedEntry              # h ~ ^[0-9a-f]{20}$, kind in {client,lost}, at = ISO with Z; unknown keys ignored, a PII_KEYS key → FeedShapeError
def parse_outcome(d: dict) -> FeedRow              # the 15 fields of A.9, all present (absent → FeedShapeError); unknown keys ignored, a PII_KEYS key → FeedShapeError
async def fetch_pages(erp, path, *, params: dict, max_pages: int = MAX_PAGES) -> AsyncIterator[FeedPage]
async def sync_suppression(session, erp, *, mode: Literal["full", "delta"], now, accept_shrink: bool = False) -> SyncReport
async def sync_outcomes(session, erp, *, walk: Literal["cursor", "recent", "quarter", "year", "unknown"], now) -> SyncReport
async def reconcile_unconfirmed(session, erp, *, now) -> ReconcileReport     # 02 §2.10 rule 5 (48 h); after the nightly walks
async def source_precision(session, source_key: str, now) -> Precision
async def source_precision_job(session_factory, now) -> None     # weekly; flips sources.auto_push both ways — never ON while config.auto_push_forbidden

FULL_MAX_AGE_H = 26      # one nightly full pull (30 2 * * *) plus slack
DELTA_MAX_AGE_H = 3      # one hourly delta plus slack
def feeds_configured(settings) -> bool              # CRM_PUSH_SOURCE == 'agent' and CRM_AGENT_KEY set; during the «Boshqa» fallback the feeds 404 (that key reads nothing) and are never called
def suppression_fresh(cfg: SuppressionConfig | None, *, key: str, now: datetime) -> bool
    # THE home of feed freshness — 02 §2.8, 02 §2.10 rule 2, 03 §2.2, 04's gate, 06 §4.2 and /holat all
    # call it, nobody restates the numbers. Feed configured ⇒ last_full_at ≥ now − FULL_MAX_AGE_H
    # AND last_delta_at ≥ now − DELTA_MAX_AGE_H AND cfg.salt_id == salt_id(key). False → health
    # problem `suppression_stale` (warning, critical after 6 h; texts say «26 soat») and every push
    # is refused `stale` (§B.1). Feed not configured ⇒ True (the XLSX fallback carries the gate, §B.3).
```

**`fetch_pages`** sends `params` exactly as given — it never adds a `since` of its own, never
`If-None-Match` — then follows `next` with the SAME params plus `cursor=<next>` until `next` is
`null`. Each page is validated before it is yielded: suppression pages carry `mode, generated_at,
salt_id, rule, entries, next, resume`; outcomes pages `generated_at, rows, next, resume`; a missing
key, non-JSON or a non-list → `FeedShapeError`. More than `max_pages`, or a `next` equal to the cursor
just sent → `FeedContract('runaway')`. HTTP answers (A.5): 404 → the same misconfiguration as a push
404 (breaker, `push_404`, stop); 400 → `feed_contract` code `bad_request` with the ERP's
`bad_cursor`/`bad_since` in the detail, stop, never retried; 503 and other 5xx / transport errors →
`with_backoff(attempts=4, base=2, cap=60)`, then the run is `failed` (the freshness gate reports it);
304 cannot happen (no `If-None-Match` sent). Cursors are opaque: the agent stores and hands back
`next`/`resume` verbatim and never decodes one (the fake ERP does, §B.4).

**Suppression sync** (`erp_sync` collector calls it; cron Tashkent: full `30 2 * * *` — after the
Yandex night run 02:10, before outcomes 03:00 — delta `15 * * * *`, skipped while a full pull runs;
neither runs unless `feeds_configured()`):

1. **Full** — `params = {"limit": 5000}`, **never `since`, never `cursor`**: only a walk that started
   from nothing may delete what it did not see (A.6 — a walk resumed from a cursor is labelled
   `full` and holds only the tail). **Delta** — `params = {"limit": 5000, "since": fmt_since(anchor −
   10 min)}` with `anchor = config.suppression.delta_generated_at` (the previous delta's first-page
   `generated_at`), else `full_generated_at`, else run as a full pull. The anchor is the ERP's clock,
   not the agent's, so a skewed agent clock cannot open a gap; the 10-minute overlap re-reads rows
   whose transaction committed after the previous read (re-reads are harmless upserts).
2. First page: assert `rule == FEED_RULE` (else `feed_contract` code `rule_changed`, stop) and compare
   `salt_id` with `sources.erp_sync.config.suppression.salt_id`: different → a **delta** aborts and
   schedules a full pull now; a **full** continues and stores the new `salt_id` only after it
   completed.
3. Each entry → upsert `suppression (kind, 'phone_hash', h, salt_id, source='erp_feed', erp_at=at,
   last_seen_at=run_started)` on conflict `(kind, key_type, key)` update `last_seen_at, erp_at,
   salt_id`. Entries are not deduplicated by the ERP (A.7): the upsert is the dedup. Pages are applied
   as they arrive; a full pull that fails midway deletes nothing.
4. **Full pull only**, after `next = null`: `seen = count(distinct (kind, h))` of this walk. Delete
   `FROM suppression WHERE source='erp_feed' AND last_seen_at < run_started` **iff** there is no
   previous count, or `seen ≥ 0.9 × config.suppression.count` (the previous COMPLETED full pull's
   `seen` — never the table's row count, which deltas only grow: **a delta never retires anything**,
   A.7), or `accept_shrink` (the owner's `tools/erp_sync.py --full --accept-shrink`, 06 §4.6);
   otherwise problem `suppression_shrunk`, nothing deleted. On a `salt_id` change, rows carrying the
   old `salt_id` go by this same rule (they were not re-seen). A shrink is expected after the ERP
   retires whole classes of tails (it did once, in `aefbf278`: 9-digit chunks of 998 runs and 7–8-digit
   fragments left the set) — only a pull made against an older build would see it.
5. Write `config.suppression = {salt_id, last_full_at | last_delta_at (agent clock, for
   suppression_fresh), full_generated_at | delta_generated_at (ERP clock, the next delta's anchor),
   count (full only)}`; heartbeat `collector:erp_sync.full|delta`. Full pull only, after step 4: (a)
   clear `suppression_stale_hit`; (b) the self-test of 06 §4.2 (`suppression_selftest_failed` when
   the hash of the newest `landed_client`/`won` tail is absent — a warning only: a `landed_client`
   recognised by a 7–8-digit card can fail it spuriously, because the feed cannot carry that number,
   A.8); (c) retire the XLSX fallback **per row** — for every `suppression` row with `source='manual_import'`, `key_type='phone_tail'`, compute
   `hash_tail(settings.crm_agent_key, tail)` and delete the row when a `source='erp_feed'`,
   `key_type='phone_hash'` row with that hash exists. A tail the feed does **not** carry stays (fail
   closed), is counted by `/holat` («qoʻlda kiritilgan: n») and is removed only by hand.

Health keys of this sync: the four feed failure codes — `bad_request`, `rule_changed`, `runaway` and
`shape` (`FeedShapeError`, §B.6) — are ONE health key `feed_contract` (06 §4.2 «ERP feed shakli
oʻzgardi ({code})»); the code travels in the problem's detail, never as a key of its own.
`suppression_stale`, `suppression_shrunk`, `suppression_stale_hit` and
`suppression_selftest_failed` are keys of their own (06 §4.2).

**The gate's hash**: `is_suppressed()` computes `phone_hash = hash_tail(settings.crm_agent_key, tail)`
with the *current* key; after a re-mint the stored rows carry the old `salt_id` and cannot match →
that is exactly why `suppression_fresh()` is false and `suppression_stale` blocks pushes until the
full pull ran (02 §2.8). The feed carries nine-digit tails only; a client card holding a 7–8-digit
number is caught by the ERP's belt and comes back `dropped/suppressed`, after which the agent keeps
that tail locally (02 §2.11) — it can never arrive as a hash.

**Outcomes sync** — four walks, the reading cadence the ERP prescribes (A.9), all upserting the same
rows (a re-read row is the same row), all skipped unless `feeds_configured()`:

| Walk | When (Tashkent) | Params | Cursor stored |
|---|---|---|---|
| `cursor` | nightly `0 3 * * *` | `limit=1000` + `cursor=config.outcomes.cursor` (absent the very first time: the whole year) | yes — `config.outcomes.cursor = page.resume` after every committed page, when not `null` |
| `recent` | nightly, right after `cursor` | `limit=1000` + `since=fmt_since(now − 8 d)` | no |
| `quarter` | Sunday `20 3 * * 0` (before `source_precision_job` 04:00) | `since=fmt_since(now − 90 d)` (the ERP's stamping horizon for agent arrivals, A.4) | no |
| `year` | the 1st of the month `40 3 1 * *` | neither `since` nor `cursor` — every row of the year; the only read that sees a stage-KIND flip on an old row | no |
| `unknown` | inside `reconcile_job` (every 10 min) when a `push_log` row is `unknown` and younger than 48 h | `since=fmt_since(min(sent_at of those rows) − 10 min)` | no |

Per row, in one transaction per page:

1. **Ignore rows the agent never pushed**: `external_id` absent from `push_log` → skipped and counted
   as `foreign` in `collector_runs.meta` (the fallback clause is permanent and anybody holding the
   «Boshqa» key can post into it, A.13). Never stored.
2. Upsert `outcomes` by `external_id` with every field of A.9, `raw` = the row verbatim, `erp_at =
   at`, `read_at = now`; `first_read_at` kept.
3. `label, precision_class = services.outcomes.derive_label(row, pushed_at=<the delivered push_log
   row's sent_at>, now=now)` — one home, 02 §2.11 — and `services.outcomes.apply_effects(session,
   company_id)` in the same transaction: the company's local `suppression(source='outcomes')` rows,
   `companies.state` and `is_competitor` are **recomputed from its current labels** (side effects and
   their retractions, 02 §2.11), never appended blindly.
4. `push_log` for this `external_id`: an `unknown` row becomes `sent` (`http_status` stays as it was)
   and its candidate `approved → pushed` (`pushed_at = push_log.sent_at`, `external_id` set).
5. After the nightly `cursor` + `recent` walks both completed: `reconcile_unconfirmed()` — 02 §2.10
   rule 5 (a `sent` or `unknown` row older than 48 h with no outcomes row → `push_unconfirmed` and at
   most one re-push).
6. Write `config.outcomes = {cursor, last_cursor_at, last_recent_at, last_quarter_at, last_year_at}`;
   heartbeat `collector:erp_sync.outcomes`.

**Outcomes → labels** (the table form of `derive_label`, for reviewers; 02 §2.11 is the code and the
home of the side effects):

| Feed row | label | precision | side effect (recomputed per company, 02 §2.11) |
|---|---|---|---|
| `outcome='client'` (only through the fallback door, A.3) | `landed_client` | neutral | `companies.state='client'`; local `suppression(client, phone_tail)` |
| `outcome='dropped'`, `reason='capped'` | `dropped_capped` | neutral | candidate eligible next month |
| `outcome='dropped'`, `reason='suppressed'` | `dropped_suppressed` | neutral | local `suppression(client, phone_tail, reason='erp_belt:…')` — the ERP refused this phone (a stale copy, or a 7–8-digit card the feed cannot carry); problem `suppression_stale_hit`, cleared by the next completed full pull |
| `outcome='dropped'`, `reason` `no_phone` / `no_contact` / other | `dropped_other` | neutral | owner digest line (the agent's own gate should make these impossible) |
| `stage_kind='won'` | `won` | positive | `companies.state='client'`; local `suppression(client, phone_tail)` — the feed brings the hash a day later. **Not** `client_id`: it stays set on a lead revived from won back to open |
| `stage_kind='lost'`, `no_contact=true` | `lost_no_contact` | negative | local `suppression(lost)` on the company's tg username, instagram, domain, stir and the pushed phone tail |
| lost, label ~ `narx\|цена\|qimmat\|дорог` | `lost_price` | neutral | `suppression(cooldown 180 d, company_id)` |
| lost, label ~ `import qilmaydi\|не импорт\|xitoy emas` | `lost_not_importer` | negative | `suppression(cooldown 365 d, company_id)` |
| lost, label ~ `hajm\|kichik\|мал` | `lost_small` | negative | — |
| lost, label ~ `raqam\|номер\|noto.g.ri` | `lost_wrong_number` | negative | `suppression(bounced)` ×2 rule |
| lost, label ~ `kargo\|cargo\|konkurent\|конкурент` | `lost_competitor` | negative | `companies.is_competitor=true` |
| lost, other (incl. `lost_reason = null`: free text, which the ERP never sends) | `lost_other` | neutral | — |
| open, `contacted_at` set | `contacted_open` | positive 14 d after `contacted_at`, else pending | — |
| `outcome='joined'`, open, no `contacted_at` | `landed_joined` | neutral | — |
| open, no contact, pushed ≥ 30 d ago | `stale_open` | neutral | — |
| open, no contact, younger | `open_untouched` | pending | — |

Before the owner keeps a lost-reason dictionary every lost reason is free text, `lost_reason` is
`null`, and lost rows land as `lost_no_contact` (when listed in the setting — the ERP's `no_contact`
still answers) or `lost_other`; the regex classes match only dictionary labels.

**Source precision** (`source_precision`): over `outcomes` joined to `lead_candidates.primary_source_key`
with `first_read_at ≥ now − 30 d`: `positives = count(precision_class='positive')`,
`negatives = count('negative')`; `precision_30d = positives / (positives + negatives)` or NULL when the
denominator is 0; `pushes_30d = count(push_log sent|unknown, superseded_at IS NULL, sent_at ≥ now − 30 d)`
for that source. `auto_push = pushes_30d ≥ 15 AND precision_30d ≥ 0.60`, flipped both ways weekly
(Sunday 04:00) — **except that the job never turns `auto_push` on while the owner has forbidden it**
(`/manbalar` → `src:<key>:a` sets `config.auto_push_forbidden=true`, 06 §1.2): the UPDATE reads
`SET auto_push = (pushes_30d >= 15 AND precision_30d >= 0.60) AND NOT coalesce(config->>'auto_push_forbidden','false')::bool`.
A flip is one digest line. `/holat` prints the three numbers per source.

### B.3 Fallback while the `agent` door is not deployed

| Item | Fallback | Switch-over |
|---|---|---|
| Door | `CRM_PUSH_SOURCE=other`; `CRM_AGENT_KEY` = the **«Boshqa»** door's secret (owner mints it on the same screen); body always carries `utm_source=clientscrapper` (right after the note, §B.1); `push_log.endpoint='/api/leads/in/other'`, `source_key_sent='other'`. That door is an ordinary advert door: no belt, the 15-minute reminder fires, its pushes count in the people's per-phone cap, and a landing that throws is still answered **200** with nothing recorded. Production without `agent-feed` also still cuts text by UTF-16 units (an emoji at a cut throws after the lead is written) — `text_len` (§B.1) keeps every value under the cut | set `CRM_PUSH_SOURCE=agent`, replace `CRM_AGENT_KEY`, restart `worker`. Candidates already `pushed` are **not** re-pushed by hand (same `channel`, so a replay); rule 5 re-pushes only a push the outcomes feed never shows |
| Feeds | **not configured** (`feeds_configured()` false): the «Boshqa» key reads nothing — every GET would be 404 and trip the `push_404` breaker — so no feed call is made | the first suppression full pull and the outcomes `year` walk run on the day of the switch |
| Routing | the `other` door is also where unknown `?manba=` lands; a rule `manba=other` + keyword `clientscrapper` routes agent arrivals to the cargo managers. VERIFIED from the code: the keyword is a case-insensitive «contains» over name + the landed note (`matchRoute` over `[name, note]`, and the note carries the `utm_source: clientscrapper` line within the 1000 cut, §B.1); still confirm once with `make push-test` (07 §5.6) | delete that rule after switch-over |
| Suppression | `make import-clients FILE=clients.xlsx` (ERP **Mijozlar → XLSX**); `services/client_import.py` stores `suppression(kind='client', key_type='phone_tail', source='manual_import')` — clear tails, because the future agent secret is unknown and hashes would not match. Lost leads invisible; `/holat` says «yoʻqotilgan lidlar roʻyxati yoʻq». Weekly re-import | a full pull retires each `manual_import` row whose tail's hash it carries (§B.2 step 5); tails it does not carry stay until the owner removes them |
| Outcomes | none; labels stay `pending`, precision NULL, `auto_push` impossible; a fallback push's fate is unknown until the switch | the `year` walk reads the fallback months through the `other` + `utm_source` clause (A.9); rule 5 then re-pushes, through the `agent` door, the ones the ERP never recorded (≤ 30 d old, payload kept) |
| Reminder | the ERP's 15-min reminder **fires** for `other` arrivals — tell the owner; keep `PUSH_DAILY_CAP ≤ 10` during the fallback | — |

### B.4 Fake ERP for tests — `tests/fake_erp.py`

`FakeErp(secret, default_mode='ok')` → `httpx.MockTransport(fake.handler)` (async handler); the client
under test is built with `ErpClient(base_url='http://erp.test', key=…, transport=…)`. It copies the
real ERP's codec and answers (A.2, A.5–A.9), not a simplification of them: when fullprompt changes,
the captured fixtures (07 §1.7) and this fake change together.

- **State**: `posted: list[tuple[source, headers, body]]`, `intakes: dict[external_id, row]`
  (`intake_id` uuid4, `created_at` microseconds), `leads: dict[lead_id, lead]` (an orphan lead may
  exist without an intake), `entries: list[(tail, kind, at_us, id, n)]`, `outcome_overrides`,
  `secret`, `modes: dict[external_id, mode]`, `held: dict[external_id, asyncio.Event]`.
- **Key**: `x-gsr-key` must equal `secret`; with no `x-gsr-key` the POST also reads body `key` /
  `google_key`; `x-erp-key` alone is **not** accepted. Unknown source, door off (`secret=None`), wrong
  key or a non-JSON body → `404`, `text/plain`, `Not found`. `rotate_secret(new)` changes the key and
  the `salt_id`; the old key 404s at once.
- **`POST /api/leads/in/{source}`** — per-`external_id` mode:
  `ok` → intake `created` + a lead, 200 `{"ok":true}`; `joined` → intake `joined` onto a prior lead;
  `client` → intake `client` + `client_id` (allowed only for `source='other'`, as the real belt makes
  it impossible on `agent`); `capped` / `suppressed` / `no_phone` → intake `dropped` with that
  reason; `404` → 404; `503` → 503 `{"error":"unavailable"}`, nothing recorded; `html500` → 500
  `<html>…`; `orphan` → creates a lead with no intake and answers 503 (a throw after the lead's
  INSERT); the next POST of that id joins it (`joined`); `throw200` (only `source='other'`) → 200
  `{"ok":true}`, nothing recorded (the fallback door's answer to a failed landing); `slow` → the
  handler waits on `held[external_id]`, the test raises `httpx.ReadTimeout` on the client side (a
  `MockTransport` wrapper with a timeout), and the intake is written when the test calls
  `fake.release(external_id)` — a first request still RUNNING, not one that finished; a concurrent
  second POST of the same id waits for the first (the real per-id chain) and is a replay;
  `badjson` → 200 `'<html>'`. A POST whose `external_id` already has an intake → 200, nothing
  changes (replay), whatever the first outcome was. A body with neither a name nor a phone →
  `dropped/no_contact`; phone with fewer than 9 digits → `dropped/no_phone`. The landed note is built
  as A.2 (extras in body order, whitespace collapsed, 1000 code points) and kept for assertions.
- **`GET /api/agent/suppression` / `outcomes`** — key as above; then `cursor` → `decode_cursor`,
  `since` → `parse_since`, either invalid → 400 `{"error":"bad_cursor"}` / `{"error":"bad_since"}`;
  `limit` by `clamp_limit`; mode `feed_503` → 503 `{"error":"unavailable"}`, `feed_timeout`,
  `feed_rule_changed`, `feed_shape` (a row with a `phone` key) per path. Bodies exactly as A.7 / A.9
  (`generated_at`, ms `at`, `resume`); suppression `mode` = `delta` iff `since`.
- **Codec** (a copy of `agent-feed.ts`; vectors pinned in 07 §1.5):
  ```python
  CURSOR_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
  def encode_cursor(at_us: str, id_: str, n: int) -> str:            # at_us 'YYYY-MM-DDTHH:MM:SS.ffffffZ'
      return base64.urlsafe_b64encode(f"{at_us}|{id_}|{n}".encode()).decode().rstrip("=")
  def decode_cursor(raw: str | None) -> tuple[str, str, int] | None | Literal["invalid"]:
      # padding restored; 2 parts (n = 0) or 3; at ~ CURSOR_AT and a real calendar date; id a uuid
      # (lower-cased); n ^\d{1,6}$ — else "invalid"
  def parse_since(raw: str | None) -> str | None | Literal["invalid"]:
      # ^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})$, real date, h ≤ 23,
      # m ≤ 59, s ≤ 59 → UTC with six fraction digits; '0', a date alone, a naive time, a space
      # where '+' was → "invalid"
  def clamp_limit(raw: str | None) -> int:   # absent/0/garbage → 1000; < 1 → 1; > 5000 → 5000; floored
  ```
  Keyset: suppression `(at_us, id, n) > cursor`, outcomes `(at_us, intake_id) > cursor`; `since`
  strict `>`; `next` = the cursor of the last row when one more row exists, else `null`; `resume` =
  the last row's cursor, or the sent cursor on an empty page, or `null`.

Tests (pytest, async): `test_payload_sections_and_limits` (order, NEGA TOPILDI first and whole in 159
code points, note ≤ 700, `landed_note` ≤ 1000, URL never cut, no person name without `legal_basis`,
`kub` rules, `utm_source` right after `note`, `exclude_none`, no field in `RESERVED_BODY_KEYS`,
`text_len` limits with an emoji at each edge), `test_external_id_month_tashkent`,
`test_canonical_sha_stable`, `test_push_200_marks_pushed`, `test_push_404_trips_breaker_and_alerts_once`,
`test_push_503_three_tries_then_failed_then_review_after_attempt_3` (nothing recorded by the fake
until a try succeeds), `test_push_html500_is_retried_like_503`,
`test_push_read_timeout_is_unknown_without_resend` (mode `slow`: exactly ONE POST in the attempt,
`push_log.unknown` + `error='timeout'`; after `fake.release()` the `unknown` walk promotes it to
`sent` and the candidate to `pushed`), `test_push_orphan_retry_joins` (mode `orphan`: the second try
lands `joined`, the outcome row is `landed_joined`, no side effect),
`test_push_2xx_badbody_is_unknown` (never `sent`; promoted by outcomes),
`test_push_unknown_promoted_by_outcomes`, `test_push_refused_outside_window_and_over_cap`,
`test_push_refused_when_suppressed_or_stale`, `test_push_fallback_other_source_sends_both_headers`,
`test_push_test_tool_new_external_id_each_run`, `test_reconcile_unconfirmed_repushes_once` (mode
`throw200` on `other`, then the switch: after 48 h with healthy walks one re-push with the same
`external_id` lands; a second 48 h without an outcome makes no second re-push — the `pushed`
candidate stays and `push_unconfirmed` stays; an `unknown` row's `approved` candidate goes to
`review`; a row older than 30 d, at attempt 3 or with `payload IS NULL` is only flagged), `test_feed_salt_change_forces_full_pull`,
`test_feed_full_pull_deletes_unseen_only_when_not_shrunk`, `test_feed_full_pull_never_sends_since_or_cursor`,
`test_feed_delta_anchor_is_erp_generated_at`, `test_feed_rule_mismatch_stops`,
`test_feed_404_is_push_404`, `test_feed_503_is_retried_not_push_404`, `test_feed_400_is_feed_contract`,
`test_feed_runaway_same_cursor`, `test_outcomes_cursor_stored_from_resume` (incl. an empty page
whose `resume` is `null` → the old cursor kept), `test_outcomes_walk_params` (cursor / recent 8 d /
quarter 90 d / year neither — `since` always ends in `Z`), `test_outcomes_ignores_foreign_rows`,
`test_outcomes_labels_table` (parametrised over the §B.2 table), `test_outcomes_retractions`
(revived lost, won → open, label changed: local rows recomputed, 02 §2.11),
`test_outcomes_joined_and_client_rows`, `test_outcomes_contract_breach_on_phone_key`,
`test_precision_and_autopush_flip` (both ways at the thresholds; never on while
`auto_push_forbidden`), `test_manual_import_retired_after_full_pull`, `test_suppression_fresh`
(26 h / 3 h / `salt_id` edges; feed unconfigured → true), `test_feeds_not_called_during_fallback`.
Recorded fixtures: `tests/fixtures/erp/` — captured from `agent-feed` at `23fe1736` on a seeded
database (07 §1.0 lists the files); `tests/test_erp_feeds_contract.py` runs the parsers over them.

### B.5 Sequences

```
PUSH (happy)                                  PUSH (404)
owner ✅ → candidate approved                 push_one → claim(sending,1) → POST → 404
crm_push_job (window open, cap ok)            push_log failed/http_404; breaker 6 h; problem push_404
 └ push_one: FOR UPDATE; gate ok              owner ← «ERP kaliti yoki manba notoʻgʻri …»
   claim push_log(sending, attempt 1) COMMIT  next ticks: refused 'breaker' until the key is pasted and /yoq crm clears it (or 6 h pass)
   POST /api/leads/in/agent  ← X-GSR-Key      /holat shows push_404 until a 2xx
   ERP: no_phone? caps? belt → client? → open lead? → create → route(kub) → push seller (clock, no due)
   ledger row LAST → 200 {ok:true} → sent; candidate pushed
nightly: outcomes row external_id → label

PUSH (read timeout)                           PUSH (503)
POST … ReadTimeout (ERP may still be running) POST … 503 {"error":"unavailable"} (nothing recorded)
no resend in the attempt                      with_backoff: 2nd try 1 s later → 200 → sent
push_log unknown/timeout; candidate blocked   (after 3 × 503: failed; attempt 2 after 5 min)
reconcile_job (≤ 10 min): outcomes since=sent_at−10 min
 → row seen → unknown → sent; candidate pushed

SUPPRESSION (full, salt changed)              OUTCOMES (nightly)
02:30 GET ?limit=5000 → salt_id ≠ stored      03:00 GET ?cursor=<stored> → rows; store resume
  continue (full); upsert pages with new salt  03:0x GET ?since=now−8d → rows (re-reads fine)
  next=null → seen ≥ 0.9×prev → DELETE unseen   upsert; derive_label; recompute effects; unknown → sent
  store salt_id; clear stale_hit; self-test     then reconcile_unconfirmed (48 h rule)
                                              Sunday +quarter (since=now−90d) · 1st +year (neither)
```

### B.6 Failure modes → behaviour

| Failure | Agent behaviour | Owner sees |
|---|---|---|
| ERP down (conn refused / connect timeout / 503 / 5xx HTML) on push | 3 tries in ≤ 15 s; `failed`; attempt 2 after 5 min, 3 after 30 min; then `review` | `/holat` `push_failing`; digest line |
| Read timeout / dropped connection after the request was sent | `unknown`, no resend; the 10-minute `unknown` walk or the nightly read promotes it; 48 h without an outcome → `push_unconfirmed` + one re-push (02 §2.10 rule 5) | nothing, or `push_unconfirmed` |
| ERP database fails half-way through a landing | 503 → retry; the retry joins the orphan lead (`joined`, «🔁 Qayta yozdi» to the seller) | nothing; stated in A.2 |
| ERP down on feeds | `with_backoff` 4 tries (503 is retried, never `push_404`); collector run `failed`; `suppression_fresh()` false (no full pull ≤ 26 h or no delta ≤ 3 h) → gate `suppression_stale` → pushes stop | alert «ERP roʻyxati 26 soatdan beri yangilanmadi — push toʻxtatildi» (06 §4.2 renders the real `{age}`) |
| 404 on any call | breaker 6 h (`sources.erp_sync.breaker_until`; `/yoq crm` clears it), `push_404`, alert once per 6 h | «kalit yoki manba notoʻgʻri» |
| 400 on a feed (`bad_cursor` / `bad_since`) | `feed_contract` code `bad_request`, run `failed`, no retry — an agent bug (a decoded or hand-built cursor, a `since` without `Z`) | «ERP feed shakli oʻzgardi (bad_request) — dasturchi kerak» |
| Secret re-minted in the ERP | next call 404 → breaker; after the new key is pasted the next feed call sees a new `salt_id` → full pull; pushes refused `stale` until it completes | alert from the 404 |
| ERP answers 200 but dropped (capped/suppressed) | learned at the next outcomes read; `dropped_suppressed` → local phone-tail row + `suppression_stale_hit` (cleared by the next full pull); `dropped_capped` → retry next month | digest |
| «Boshqa» fallback door answers 200 to a landing that threw | invisible until the switch-over; then the `year` walk finds no row and rule 5 re-pushes once through `agent` | `push_unconfirmed` while it is open |
| Feed returns a PII key | `FeedShapeError`, run `failed`, problem `feed_contract` (code `shape`) | alert (contract breach) |
| Full pull shrinks > 10 % | no delete; `suppression_shrunk` | alert; runbook 06 §4.6 |
| Label list renamed in the ERP without the setting | lost leads drop out of the feed and `no_contact` turns false | the owner doc says «deactivate + create», or list the new spelling in the setting |
| Owner changes a funnel stage's KIND | invisible to the delta and to `since` reads of older rows; the nightly full pull and the monthly `year` walk see it | nothing |
| Owner maps `kub` late | early arrivals route by the note's «N kub», else rule B (A.14) | none |
| Agent DB loses `push_log` (restore from backup) | outcome rows of the lost pushes are `foreign` and ignored; a re-push of such a company is a replay in the ERP | `push_unconfirmed` for the restored rows until the nightly read |

---

## Open questions

1. ~~§A.3 `company` pass-through~~ — resolved: shipped (A.2; `company`/`firma` → `leads.company` on a created lead, and still a note line and a pair).
2. ~~Inactive clients in the suppression set and the belt~~ — resolved: built that way (A.7, A.8).
3. Should a push that JOINS another manager's open lead be `dropped/suppressed` instead? Today it is `joined` + a note + «🔁 Qayta yozdi» to that lead's owner, and the outcomes feed shows that lead's stage (A.9); left as is.
4. Push window 09:30–17:30 Mon–Sat vs the playbook's 10:00–17:00 DM window — one number, which? (07 §5.2 q.6.)
5. ~~`OUTCOMES_WINDOW` as a setting~~ — resolved: a constant of one year in `agent-feed.ts`; the agent's 30-day precision needs far less.
6. `agent_no_contact_reasons` default «Qayta qilinmasin» only — add «Notoʻgʻri raqam» so a wrong number is fed back by phone, not only by the agent's ×2 rule? (07 §5.2 q.5.)
7. ~~Does /crm/tahlil read `contact_due_at`?~~ — resolved: it reads `contact_clock_at`/`contacted_at`; agent leads are in the late counts, and in the median only for contacts within 7 days (A.4). The ERP's own owner questions remain (its CHANGELOG): should agent leads count in a seller's late counts at all, and should they be booked on «Bugun qoʻngʻiroq» / the 08:00 digest (they are, up to 25 lines a day).
8. ~~Fixtures captured or hand-written?~~ — resolved: captured from `23fe1736` (07 §1.0).
9. 02 §2.10 rule 5's one re-push needs `push_log.superseded_at` (migration `0002_erp_contract`, T-10). Accept, or keep `unknown`/`sent` rows blocking for ever and leave every unconfirmed push to `/push tekshir`? (Recommended: accept — without it a 2xx row can never be retried, because `ck_push_log_sent` forbids `failed` on a 2xx.)
10. AGENT-FEED.md §0 step 4 says arrivals before the `kub` mapping route by rule B; the code reads a «N kub» from the note first (A.14). Ask the ERP session to correct the sentence, or ask the owner whether rule A should wait for the mapping.
11. The orphan lead (A.2): the ERP chose not to claim the ledger row first. The agent lives with `joined` after a 503; is a «🔁 Qayta yozdi» about a never-announced lead acceptable to the owner, or should the ERP adopt the claim-first ledger later?

## Acceptance criteria

- fullprompt `agent-feed` (built at `23fe1736`; T-07): `pnpm typecheck && pnpm lint && pnpm test` green on a fresh `gsr_ci` in CI's order (met: 675 files / 7629 tests); no migration (met); `GET /api/agent/suppression` and `/outcomes` answer 404 without a key or with `X-ERP-Key` only, 400 `bad_cursor`/`bad_since` on a bad cursor or `since`, 503 JSON when the database fails after the key was sent, 200 gzipped ≥ 1 KB with the documented bodies and `resume` (met, captured); an `agent` arrival on a client-book (active or inactive, card, person or won/attached lead, 7–8-digit card by suffix) or «Qayta qilinmasin» phone writes `dropped/suppressed`, creates nothing and notifies nobody (met); an `agent` lead has `contact_clock_at` set and `contact_due_at NULL`, is never claimed by `remindUntouched`, and gets `contacted_at` from `stampFirstContacts` for 90 days (met); the agent door answers 503 to a landing that threw and every other door keeps its 200 (met); `tiktok`/form arrivals behave exactly as before (control group, met); the setting is described in 4 locales (met); the seed adds exactly one row (met); `docs/AGENT-FEED.md` lets the Python side be built without reading TypeScript (met). **Still open: the PR, the merge, the deploy and the owner's five steps (A.14).**
- Agent: `make test` green with the fake ERP covering 200 / 404 / 503 / 5xx HTML / read timeout with the first request still running / replay / joined / orphan-joined / client (fallback) / `throw200` (fallback) / badjson and both feeds; a 404 trips the breaker and alerts once; a 503 is retried and never trips it; a read timeout is never resent inside the attempt and lands as `unknown`, promoted by the next outcomes read; a 2xx with an unreadable body lands as `unknown`, never `sent`; an unconfirmed `sent`/`unknown` row is re-pushed at most once (02 §2.10 rule 5); `salt_id` change forces a full pull before any push; a full pull never sends `since` or `cursor`, and only it deletes; `suppression_fresh()` refuses pushes at 26 h without a full pull or 3 h without a delta; a shrunk full pull deletes nothing; a full pull retires exactly the `manual_import` tails whose hash it carries; the four outcomes walks send the documented params and the stored cursor is the last non-null `resume`; outcome rows the agent never pushed are ignored; labels match the table for every row kind and the local suppression rows follow the labels both ways (retractions); precision/auto-push flips both ways at the thresholds and never on while `auto_push_forbidden`; the fallback (`other` + both headers + `utm_source` after the note) passes the same push tests and makes no feed call; `make push-test` makes a new `external_id` every run; `tests/test_no_telegram_writes.py` stays green (nothing here sends to a prospect); no secret in repo or logs (`RedactBodies` covers `X-GSR-Key`/`X-ERP-Key` headers and the payload's `phone`).

## Review pass changelog

Items of the consistency review applied to this file (05 is the named home for most of them; the
others point here):

- **3** — §B.2 step 5 and §B.3 Suppression row: the XLSX fallback (`phone_tail`, `source='manual_import'`) is retired per row when a full pull carries `hash_tail(key, tail)`; the old bulk «count ≥ 0.9 × manual_import count» rule is gone; test description updated.
- **7** — §B.2 declares `suppression_fresh()` with `FULL_MAX_AGE_H = 26` / `DELTA_MAX_AGE_H = 3` as the one home; §B.1 `stale`, §B.6 texts and the alert say 26 h / 3 h; full-pull cron `30 2 * * *` (§B.2, §B.5 02:30, §A.9 cadence); `test_suppression_fresh` added.
- **16** — §B.1 states the two-level attempt scheme (three HTTP tries inside one attempt, `ATTEMPT_DELAYS` between attempts, 9 POSTs worst case), the timeout resend lands as attempt 1 `sent` with one intake, `_post` checks `{"ok":true}` and a 2xx with another body is `unknown` + `http_2xx_badbody` (matches 02's widened CHECK); §B.4 fake `timeout` fires once per `external_id`; `test_push_2xx_badbody_is_unknown`; acceptance criteria updated.
- **17** — §B.1 404 row names the ledger kinds `alert:push_404` / `recovered:push_404` (heartbeat was already `collector:erp_sync`).
- **18** — §B.2 steps 1–3 and §B.6: `feed_bad_request` / `feed_rule_changed` / `feed_runaway` fold into the one health key `feed_contract` with codes `bad_request` / `rule_changed` / `runaway` / `shape`; `suppression_stale_hit` named as its own key.
- **21** — §B.1 404 row, §B.5 and §B.6 say `sources.erp_sync.breaker_until` and that `/yoq crm` clears it.
- **27** — §B.1 pre-flight list gains `company_recent` (no open push for 90 d per company) beside `phone_recent` (24 h per phone), matching the card line «90 kunda yuborilmagan».
- **28** — §B.1 `BAND_KUB = {"1_3":"1","3_10":"3","container":"28","truck":"80"}`; the `kub` field comment follows.
- **32** — §B.2 outcomes table: `won` (and `client`) write `suppression(client, phone_tail, source='outcomes')` locally, now; 02 §2.11 points here.
- **33** — §B.2 `source_precision_job` never turns `auto_push` on while `config.auto_push_forbidden` is true (`AND NOT coalesce(config->>'auto_push_forbidden','false')::bool`); test and acceptance updated.
- **36** — §B.3 Routing row marks «keyword rule reads the note» UNVERIFIED with a day-1 `make push-test` check; §A.5 marks the existence of both `clients.created_at`/`updated_at` UNVERIFIED with a `\d clients` check.
- Not touching this file (already consistent here, or homed elsewhere): 2, 11, 20, 22 (05 is the home and already says what the other files now point to); 1, 4–6, 8–10, 12–15, 19, 23–26, 29–31, 34, 35, 37, 38.

**T-07 as built (2026-10-24)** — fullprompt `agent-feed` at `23fe1736` (ERP DECISIONS #1455–#1472, two review rounds), read from the code and `docs/AGENT-FEED.md`:

- Part A rewritten from a proposal into a summary of the build: status and gates (A.0), the 40-file map (A.1), the door with its 200 / 503 / 5xx-HTML / 404 answers, code-point limits, the landed-note assembly, `company`/`firma`, the replay fence with the in-process per-id chain and the orphan (A.2), the landing order with `no_phone`, per-family caps and the belt before the client check (A.3), the clock with `contact_due_at NULL` and the 90-day look-back (A.4), the GET door (A.5), the 3-part microsecond cursor, `resume`, strict `since`, `limit` (A.6), the suppression kinds incl. person records and won/attached leads, the tail rules (A.7), the belt by `phonesMatch` incl. 7–8-digit whole values (A.8), the outcomes feed's 15 fields with labels-only `lost_reason` and `no_contact` (A.9), the setting (A.10), seed 190 and the doors screen's key gating (A.11), tests (A.12), limits (A.13), what is still to do and the owner's five steps in Uzbek (A.14). §0 kept as the pre-build record, marked superseded.
- §B.1: `X-GSR-Key` is the header the ERP reads (`X-ERP-Key` sent, ignored); `RESERVED_BODY_KEYS`; field order puts `utm_source` right after `note`; `landed_note()` and its 1000 budget; every limit measured by `text_len` (UTF-16 units — safe for both the new code-point cuts and the old production build the fallback posts to); no resend after a read timeout (`MAY_HAVE_LANDED` → `unknown`), retries only when nothing reached the ERP or it answered 5xx (503 = nothing recorded); the mapping table gains the timeout and 503 rows; the orphan join; same-`external_id` and superseded rows count for no pre-flight; `make push-test` mints a new company (new `external_id`) per run and writes `push_log`.
- §B.2: `ErpClient` headers and timeouts, `fmt_since` (always `Z`), `parse_outcome` over 15 fields, `PII_KEYS`, `fetch_pages` (params verbatim, `next` until null, runaway on a repeated cursor, 400/404/503 handling, cursors opaque); suppression full pull = `limit=5000` with neither `since` nor `cursor`, the only walk that deletes, ≥ 90 % against the previous full pull's own count (a delta never retires), delta anchored on the ERP's `generated_at` − 10 min; outcomes = four walks (`cursor` storing `resume`, `recent` 8 d, `quarter` 90 d, `year` neither) plus the 10-minute `unknown` walk — the ERP's cadence, which supersedes both review rounds' partial versions (first round: nightly cursor + 8 d, weekly 30 d; first fixer: weekly 90 d; second round: monthly whole year); foreign rows ignored; effects recomputed per company; `unknown → sent` promotes the candidate; the labels table now keys on `no_contact` and `stage_kind='won'` (not `client_id`), `dropped_suppressed` writes a local tail, `stale_open` ages on `push_log.sent_at`; `pushes_30d` skips superseded rows.
- §B.3: feeds not configured during the fallback; the fallback door's 200-on-throw; the keyword-over-note routing VERIFIED from `matchRoute`; switch-over via the `year` walk and rule 5.
- §B.4: the fake copies the real codec, `parse_since`, `clamp_limit`, `resume` and answers; new modes `503`, `html500`, `orphan`, `throw200`, `slow` (a first request still running); test list updated. §B.5/§B.6 follow.
- Open questions 1, 2, 5, 7, 8 resolved; 9–11 added. Acceptance criteria restated against the build.
