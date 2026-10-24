# 05 — ERP integration (fullprompt branch `agent-feed` + agent push/feeds)

Scope: both sides of the ERP contract. **Part A is what was built** on fullprompt branch
`agent-feed` (head `d138b3bc`, 2026-10-24) — a summary for the Python side, not a proposal.
**Part B** is `clientscrapper/services/crm_push.py` and `services/erp_feeds.py`, written to consume
Part A. Tables referenced on the agent side (`push_log`, `outcomes`, `suppression`,
`lead_candidates`, `sources.config`) are defined in `02-data-model.md` §2.8–2.11; this file does not
restate their DDL.

Order of truth for the wire: the ERP code on `agent-feed` (`src/modules/wms/crm/agent-feed.ts`,
`agent-feed-door.ts`, `agent-source.ts`, `inbound.ts`, `inbound-webhook.ts`, `first-contact.ts`,
`inbound-notify.ts`, `src/modules/platform/text/clip.ts`, `src/app/api/agent/*/route.ts`,
`src/app/api/leads/in/[source]/route.ts`), then fullprompt
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
  **`d138b3bc`** (2026-10-24 11:20 UTC). Fourteen commits (`git rev-list 7bea3051..d138b3bc`) over
  `origin/main` at `7bea3051` — the merge commit of PR #129 into `main` (parents `71025447` and `24607e8c`; the PR's
  head, the tip of `claude/gsr-logistics-wms-phase1-o8h4en`, is `24607e8c`, an ancestor of it), so the
  agent-feed PR targets `main`: the build (`98437a75`, `2ebafe83`, `fec8e1b4`,
  `0bd4bcdc`, `abcc9e6a` — DECISIONS #1455–#1459), the first review's fixes (`70a28720`, `c6537cae`,
  `f2d96526` — #1460–#1466), the second review's fixes (`aefbf278`, `23fe1736` — #1467–#1472) and
  the third review's (`bfb5625b` code, `a4f9f433` docs — #1473–#1475: cuts back to UTF-16 units with a
  whole-emoji back-off, NUL storable on every door, a `ref` that never throws, the `external_id`
  spelled as postgres stores it) and the fourth review's (`021a2ca5` code, `d138b3bc` docs —
  #1476–#1477: an `external_id` too long for the ledger's index lands whole).
- **The fourth review, for the agent.** An `external_id` that the ledger's unique index
  (`lead_intakes_external_idx`, a btree on `(channel, external_id)`) refuses for its size used to fail
  the ledger row AFTER the lead and its lenta note (54000): a 503 on every retry here, a 200 with an
  orphan the caps never counted on every other door. Now `record()` writes the row under the id as
  sent and, only when postgres refuses it with 54000 AND the id is over 2,048 UTF-8 bytes
  (`INDEX_SAFE_ID_BYTES` — only such an id can be refused; postgres compresses first and takes about
  2,680 bytes it cannot compress, a repetitive 50 KB id fits), writes it again as `sha256:` + the hex
  SHA-256 of its UTF-8 bytes (after the U+FFFD rule; `oversizeIdSpelling`). That arrival lands, its
  retry is a replay (both fences — the landing's and the bot's `inboundLanded` — ask both spellings,
  `externalIdSpellings`), and it never appears in the outcomes feed, which reports only `cs:` ids.
  Every id the index holds keeps its own spelling. The agent's ids are `cs:<uuid>:<yyyymm>`, 46
  bytes, so the agent is not affected: nothing changes in the push, the reconcile or the outcomes
  logic (§B.1, §B.2).
- **No PR is open, nothing is merged, nothing is deployed.** No migration: the journal stays at 136
  files, so a deploy is checked by the build stamp at `/api/version`, not by the migration count.
- 42 files, +4370 / −79 (the 41st, the extended `tests/integration/field-map.integration.test.ts`,
  came with the third review; the 42nd, the new `tests/unit/intake-id.test.ts`, with the fourth). The
  wire contract's ERP-side home is `docs/AGENT-FEED.md` (443 lines; §0 the
  owner's five steps in Uzbek, §1 the door, §2 the landing, §3 the feeds, §4 privacy, §5 honest limits,
  §6 the «Boshqa» fallback).
- Gates of the last round (the fourth review): full vitest 676 files / 7642 tests green; 5 red proofs
  by string edit with a hash-verified restore, all red (#1477). The third review's: a freshly
  recreated `gsr_ci` migrated (136 = 136), seeded and demo-seeded; `pnpm typecheck` and `pnpm lint`
  exit 0; full vitest 675 files / 7636 tests green; 12 red proofs by string edit, all red. Playwright
  was not re-run in rounds 2–4 (no UI change); round 1 ran `pnpm build`, the bundle host sweep and five
  e2e specs (16/16) and looked at the doors card at 360 px. Red proofs over the build and the review
  rounds: 16 + 21 + 16 + 12 + 5 (#1458, #1466, #1472, #1475, #1477). The vitest and e2e counts come
  from the build session's own report: no fullprompt document records them (DECISIONS records only the
  red proofs) — the PR's CI run is where they are re-checked.
- The captured fixtures (07 §1.0) were taken at `23fe1736` and stay valid at `d138b3bc`: neither the
  third nor the fourth review changes a captured answer — no captured value reaches a cut (the longest
  captured string is a 128-unit note), none carries a NUL, a lone surrogate or a deep body, and no
  captured `external_id` is longer than 46 bytes, far under the 2,048 bytes at or under which an id is never
  re-spelled (checked over the v2 capture, 2026-10-24).

### A.1 File map as built

| File | What it holds now |
|---|---|
| `src/modules/wms/crm/agent-source.ts` (new) | `AGENT_SOURCE_KEY = 'agent'`, `isAgentSource()`, `personPickableSources()` (lead forms never offer «Agent (qidiruv)» except as a record's own stored value). No imports, on purpose (an import cycle through `inbound-notify` → `first-contact` would read the constant in its temporal dead zone). |
| `src/modules/wms/crm/inbound.ts` | `'agent'` in `INBOUND_SOURCE_KEYS` (between `sayt` and `other`); `inboundSourceKey(raw, channel)` — `agent` only on channel `webhook`, else `other`; `formSourceKey()` — `/ariza?manba=agent` is «Sayt»; `landInboundLead` = a per-`(channel, external_id)` in-process chain around `landOnce`, the id normalised by `storableText` first (the chain key, the replay SELECT and the bot fence `inboundLanded` all use that spelling); `record()` writes the ledger row under that id and, only when postgres refuses it for its size (54000) and it is over `INDEX_SAFE_ID_BYTES` = 2,048 UTF-8 bytes, again under `oversizeIdSpelling` (`sha256:` + hex digest), both fences asking `externalIdSpellings` (the id, plus the hash for an id over the limit); the agent's `no_phone` drop, per-family phone cap, belt, `company` onto the card; every text cut by `clipUnits` (UTF-16 units, an emoji left out whole) and made storable (NUL and lone surrogates → U+FFFD — name, note, company, the card's phone); `ref` decided before any write by `storedRef` (`storableJson`, or `{"_truncated":true}` for a body too deep to serialise). |
| `src/modules/wms/crm/inbound-webhook.ts` | `InboundFields.company` read from `company` then `firma` (key names case-insensitive), **beside** — not instead of — the note line and question pair those keys always produced. |
| `src/modules/wms/crm/agent-feed.ts` (new, 524 lines) | `hashTail`, `saltIdFor`, `HASH_RULE`, the cursor codec, `parseSince`, `clampLimit`, `noContactReasons()`, the derived suppression table (`suppressionTailsSql`), `suppressionPage`, the belt `isSuppressedTail`, `outcomesPage`. |
| `src/modules/wms/crm/agent-feed-door.ts` (new) | `agentFeedResponse(request, feed)` (`feed` = `'suppression'` or `'outcomes'`) — the one GET door both routes share (key, 400/404/503, body, `Cache-Control`). |
| `src/app/api/agent/suppression/route.ts`, `src/app/api/agent/outcomes/route.ts` (new) | `export const dynamic = 'force-dynamic'`; `GET` delegates to the door. |
| `src/app/api/leads/in/[source]/route.ts` | The agent's door answers **503** when the landing (or, with `X-GSR-Key` sent, the key lookup) throws; every other door unchanged. |
| `src/modules/wms/crm/first-contact.ts` | `REMINDER_EXEMPT_SOURCES = {agent}`, `isReminderExempt()`, `CONTACT_LOOKBACK_DAYS = 7`, `EXEMPT_CONTACT_LOOKBACK_DAYS = 90`, `JOINED_FRESH_CONTACT_HOURS = 24`; `stampFirstContacts` looks at an unstamped exempt arrival for 90 days. |
| `src/modules/wms/crm/inbound-notify.ts` | `startContactClock(intakeId, now, remind)`; `announceArrival` passes `!isReminderExempt(sourceKey)`. |
| `src/modules/wms/crm/inbound-contact-jobs.ts` | Comment only: the 90-day branch and its measured cost. |
| `src/modules/wms/crm/analytics.ts` | /crm/tahlil's «Birinchi aloqa» median takes only contacts within `CONTACT_LOOKBACK_DAYS` of the clock (late agent contacts stay in the late counts). |
| `src/modules/wms/crm/field-map.ts`, `src/modules/wms/crm/service.ts` | `clipUnits` cuts in `normalizeKey`/`capturePairs` (key 80, value 300) and in `addActivity`'s 200-unit audit excerpt — the UTF-16 budget the old `slice` had, so a `lead_field_map` key saved before the deploy is spelled the same and still matches (#1473). |
| `src/modules/wms/client-cabinet/service.ts` | `activeClientsByPhone` reads a non-array `clients.phones` as no phones (one such card used to fail every arrival). |
| `src/modules/platform/text/clip.ts` (new) | `storableText` (every lone surrogate and NUL → U+FFFD, one unit for one), `clipUnits` (`storableText(t).slice(0, max)`, minus a high surrogate left dangling at the edge), `storableJson` (`JSON.stringify` untouched, then its `\u0000` and lone `\udXXX` escapes rewritten to `\ufffd` in the output — so two keys of one object that differ only there become one key, postgres keeping the last value). |
| `src/modules/platform/settings/reason-list.ts` (new), `settings/service.ts`, `src/app/(protected)/admin/settings/page.tsx` | Setting `agent_no_contact_reasons` (default `Qayta qilinmasin`), its validator and the refusal sentence `badValueReasons`. |
| `messages/{ru,uz,zh-CN,en}.json` | `settings.descriptions.agent_no_contact_reasons` and `badValueReasons` in all four locales. |
| `scripts/seed.ts` | `{ key: 'agent', name: 'Agent (qidiruv)', sortOrder: 190 }` (just above «Boshqa» 200; 90 is «Oʻzi keldi»'s). |
| `src/app/(protected)/crm/(pages)/kelganlar/door-views.ts` (new), `doors.tsx`, `page.tsx` | The doors screen decided on the server: no form or bot link for the agent's door; a door's key sent only to `admin.settings.manage`. |
| `src/app/(protected)/crm/leads/new/page.tsx`, `crm/leads/[id]/page.tsx`, `src/app/(public)/ariza/page.tsx` | Person doors never claim the agent's source. |
| `docs/AGENT-FEED.md` (new), `docs/ADS.md` §1 §5.3, `DECISIONS.md` #1455–#1477, `CHANGELOG.md` (Uzbek) | Docs. |
| `tests/unit/agent-feed.test.ts`, `agent-feed-door.test.ts`, `agent-door-landing.test.ts`, `inbound-source-keys.test.ts`, `text-clip.test.ts`, `intake-id.test.ts`; `tests/integration/agent-feed.integration.test.ts` (1445 lines); `tests/integration/field-map.integration.test.ts` (extended, +30) | Tests (§A.12). |

`platform` imports nothing from `wms` (the reason parser lives in `platform/settings` because the
settings validator needs it).

### A.2 The push door — `POST /api/leads/in/agent`

**Key.** Header `X-GSR-Key: <the agent source's webhook secret>`. Only that header is read;
`X-ERP-Key` alone is a 404 (captured, `err_post_404_erp_key_only.txt`). With **no** `X-GSR-Key`
header the door also takes the key from a body field `key` or `google_key` (Google's shape) — so the
agent must never name a payload field `key`, `google_key`, `secret` or `token` (all four are also
dropped from the note, the pairs and `ref`). Body keys are matched lower-cased against `fromPlain`'s
`known` set (`inbound-webhook.ts`): besides those four it consumes `name`, `ism`, `phone`, `telefon`,
`note`, `izoh`, `id` and `external_id` as identity and never turns them into note lines, and a
non-empty `user_column_data` array switches the whole body to the Google parser — so the agent uses
exactly `external_id`, `name`, `phone`, `note` and never `id`, `ism`, `telefon`, `izoh` or
`user_column_data` (§B.1 `RESERVED_BODY_KEYS`).

**Body.** A JSON object (anything that is not JSON → 404, nothing recorded).

| Key | Read as | Limit (all cuts by **UTF-16 units** — JavaScript's `.length`, an emoji is two — through `clipUnits`, see below) |
|---|---|---|
| `name` / `ism` | the lead's name; whitespace runs collapsed, trimmed | 120 |
| `phone` / `telefon` | identity = its last 9 digits; the card keeps the text as sent | fewer than 9 digits → `dropped/no_phone` |
| `note` / `izoh` | the first lenta note | see below |
| `external_id` / `id` | the replay key | — |
| `company` / `firma` (case-insensitive key; `company` first) | `leads.company` on a **created** lead — and also a note line and a question pair like any other key, so a joined push still shows it | 200 on the card |
| any other key | one `key: value` line appended to the note **and** a question pair for the mapping screen — only when the value is a number, or a string JavaScript's `trim()` leaves non-empty (`str()` in `inbound-webhook.ts`; a string value is used trimmed, anything else makes no line and no pair) | 30 pairs, key 80, value 300 |

A cut never halves an emoji: `clipUnits(t, max)` = `storableText(t).slice(0, max)`, and a high
surrogate left dangling at the edge is dropped, so an emoji that does not fit is left out whole —
while a sequence of several code points (a ZWJ family, a flag, a skin tone, a keycap, a letter +
combining mark) can lose its tail at the cut (AGENT-FEED.md §1). Every string the old `slice` could
store comes out spelled exactly as before (#1473).

**The landed note** = the agent's `note`, then one `key: value` line per extra key in the body's own
key order, then **every whitespace run (line breaks included) collapsed to one space**, trimmed, and
cut at 1000 UTF-16 units. The lenta shows `📣 agent` + a line break + that paragraph; the seller's
Telegram push shows `«<first 159 code points>…»` (`clipText(note, 160)` — the one cut here that still
counts code points); `📦 N kub` appears in the push
only for a MAPPED `kub` — and a mapped `kub` is also written as the lead's quoted volume
(`leads.quoted_volume_m3`, the card's «Kub (m³)» field, `inbound.ts`), echoed back by the outcomes
feed and carried into the deal's `quotedVolumeM3` by `winLead` (`service.ts`). A volume read from the
text (`textVolume`) only routes the lead and is never printed or stored — the ERP's own rule that a
guess must not stand on the card as the customer's figure; hence the agent sends `kub` only for a
STATED volume (§B.1). `ref` stores the whole body minus the four secret names, uncut — or
`{"_truncated":true}` for a body nested too deep to serialise (about 4,000 levels; `storedRef` decides
it before any write, and the arrival still lands). A lone UTF-16 surrogate or a NUL (U+0000) the body
carries (`"\ud83d"` and `"\u0000"` are valid JSON) is stored as U+FFFD wherever it lands — the name,
the note, the company, the card's phone, every pair, the `ref` (`storableJson` rewrites only the
`\u0000` and lone `\udXXX` escapes in `JSON.stringify`'s output) **and the `external_id`**: the id is
normalised before the chain key and the replay SELECT, so the outcomes feed hands such an id back with
U+FFFD in its place (the agent's own `cs:` ids carry neither). It follows that two `external_id`s
that differ only by a NUL against a U+FFFD, or by which lone surrogate, are ONE id — the second
delivery is a replay — and that two `ref` keys of one object that differ only that way collapse to one
key, the last value kept (AGENT-FEED.md §1, #1477). The agent's payload tests already keep every NUL
and lone surrogate out of every value (07 §1.6), so neither reaches it. The `name` is not checked for
a minimum: an empty one becomes `agent · <digits>`.

**`external_id`** = `cs:<company_uuid>:<yyyymm>` (46 bytes); the outcomes feed reports only ids
starting `cs:` (`agent-feed.ts`, `external_id LIKE 'cs:%'`). An id that the ledger's unique index
refuses for its size — only one over 2,048 UTF-8 bytes can be refused, and postgres takes about 2,680
bytes it cannot compress — is stored as `sha256:` + the hex SHA-256 of its UTF-8 bytes (after the
U+FFFD rule): it lands, its retry is a replay, and the outcomes feed never shows it. Every id the
index holds keeps its own spelling (#1476). The agent's ids are far below the limit, so this never
applies to them.

**Answer** (captured in `post_*.json`, `err_post_404_*.txt`; the 503 is read from the code):

| Code | Body / type | Means |
|---|---|---|
| 200 | `{"ok":true}`, `application/json`, `cache-control: no-store` | the arrival was **recorded** — any outcome (created, joined, suppressed, capped, no_phone, no_contact) — **or** it is a replay of one that was |
| 503 | `{"error":"unavailable"}`, `application/json`, `cache-control: no-store` | the landing threw — whichever way the key came — or the key lookup threw while `X-GSR-Key` was sent: normally no ledger row — retry with the same `external_id` (a lead, and on a retry a joined lenta note, may already stand: the orphan, below; rarely the ledger row did commit and only its answer was lost, §A.13 (i)) |
| 500 | empty body | the database failed before the key could be checked and no `X-GSR-Key` header was sent (the route rethrows) — never the agent, which always sends the header |
| 404 | `Not found`, `text/plain` | unknown source in the path, door off (no secret, or the `agent` source deactivated), key wrong or absent, body not JSON |

The outcome never changes the answer. Only this door says 503; every advert door and the «Boshqa»
fallback still answer 200 to a landing that threw.

**Replay fence.** A second `(channel='webhook', external_id)` is 200 and lands nothing — no lead, no
ledger row — once the first delivery FINISHED, whatever its outcome (a capped or suppressed first
delivery spends the month's id too). The fence is per channel across every source: an id first
posted through another webhook door makes the agent's push a replay. A delivery that arrives while
one of the same id is still running **waits for it** (a `Map<channel+id, Promise>` chain per copy of
the landing module — every channel that carries an id lands through one caller, `webhook` the route,
so each channel's deliveries meet in one map; production runs one app process) and is then that
replay; across processes only the ledger's unique index stands (one ledger row, not one lead). The
wait has no bound (§A.13 (iv)). A landing that failed AFTER its lead was written (only a database
failure half-way does that now: no text the body carries — an emoji at a cut, a lone surrogate, a
NUL, a body nested too deep, an id too long for the index — makes a write refuse any more, #1467,
#1473, #1476) leaves that lead standing with no ledger row; the retry normally JOINS it, so the
outcome reads `joined` and the seller hears «🔁 Qayta yozdi» about a lead whose «🆕» never came
(AGENT-FEED.md §1, §5) — §A.13 lists when it makes a second lead or comes back `capped` instead. One
input-driven cause that repeats on every attempt is known (AGENT-FEED.md §1, §5; #1477): a body of
tens of megabytes — `ref` is the whole body, nothing caps a route handler's body, and postgres
refuses a jsonb past its limits (measured in postgres, not through the route: an array of twenty
million zeros, about 40 MB of JSON, is refused; ten million is stored) — would land its lead and fail
its ledger row every time: a 503 on every retry and one more lenta note each. The agent's bodies are
a few KB (note ≤ 700 chars, §B.1), so they cannot reach it; the ERP's cure (write the ledger row
first) is deferred.

**Caps** (counted over the arrivals ledger, dropped arrivals included, each over a **rolling 24 h**
of `lead_intakes.created_at` — `inboundCount`, not a calendar day): 3 per phone and 200 per source →
`dropped/capped`. The source cap is not split by family: on the «Boshqa» fallback its 200 also count
every person's «Boshqa» webhook, bot `ad_<unknown>` and hand-posted arrival. The per-phone count is
per **family**: an agent push through this door counts only the agent's arrivals on that phone and never caps a person's own enquiry; a
push through the «Boshqa» fallback is an ordinary arrival and counts in the people's family.

**Who may be `agent`.** Only this webhook: `/ariza?manba=agent` lands under «Sayt», a bot
`?start=ad_agent` or the form posted by hand under «Boshqa» (`inboundSourceKey`), so a person never
inherits the belt, the silent clock or a row in the outcomes feed.

### A.3 The landing of an `agent` arrival (`landOnce`, in order)

1. no name (after the whitespace collapse) **and** fewer than nine phone digits →
   `dropped/no_contact` — before the replay fence (its ledger row is `ON CONFLICT DO NOTHING`, so a
   re-send of the same id writes no second row);
2. replay (above) → nothing, 200;
3. agent source, a name, but fewer than nine phone digits → `dropped/no_phone` (after the fence);
4. caps (§A.2) → `dropped/capped` — so a phone the belt would refuse, pushed a fourth time in a day,
   reads `capped`, not `suppressed` (captured: `post_capped.json`);
5. **the belt** (§A.8) → `dropped/suppressed`: nothing created, nobody told;
6. «already a customer»: `activeClientsByPhone` (active cards) returns **exactly one** card →
   `client`, a note on that card (`inbound.ts`, `known.length === 1`). Two or more active cards on
   the phone — the ERP's normal case of one person holding several codes — skip this step, and the
   arrival goes on to join (7) or create (8). The step cannot fire for an agent push, because the belt
   is never narrower; it appears only through the fallback door (`post_fallback_client.json`), where a
   multi-code client's phone therefore lands as a NEW lead with a «🆕» push (§B.3);
7. the one open lead on the same last-9 within 30 days (`updated_at`) → `joined`: a note on it, its
   owner unchanged, `updated_at` touched, the owner pushed «🔁 Qayta yozdi». Two open leads on one
   number refuse to join (a new lead is created);
8. otherwise a **new lead**: owner by the taqsimot rules (`manba = agent`, the mapped `kub`, else the
   first «N kub|куб|m3|м3|m³|м³» in name + landed note — `textVolume`, flags `iu`; the FIRST match
   alone decides: `parseMeasure` turns a first «0 kub», or a number ≥ 100000, into no volume — so no
   windowed rule matches — and never looks at a later match), `next_action_at` = today
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
  arrival (`contactEvidenceSql`, `first-contact.ts`): `call` (calls app — outgoing, or incoming and
  answered — by anybody, counted from **2 minutes before** the arrival for a phone clock running
  early); `telegram` (a message SENT to the lead's chat from a CONNECTED account, by anybody); `note`
  (a lenta note by the **responsible person** — the lead's current owner or the arrival's assignee,
  anybody when both are NULL; the «📞 Bogʻlandim» button's own note counts from whoever pressed it,
  and the button's push reaches the lead's owner or, for an unowned lead, the office); `stage` and
  `followup` (a stage move, a cleared follow-up — both only by that same responsible person; an owner
  change alone is not contact). **What a «📞 Bogʻlandim» press reads as**: `markContactedFromBot`
  (`inbound-notify.ts`) first clears the lead's booking when it is today or earlier
  (`setFollowUp(lead, null)`), and only then writes the note. Only a CREATED lead is booked for today
  (`createLead`, `nextActionAt: tashkentDay()`); a JOINED push lands on an existing lead whose
  `next_action_at` stays whatever it was. That clear is an `audit_log` row `after.nextActionAt = null`,
  which is `followup` evidence when the presser is the responsible person, and it is EARLIER than the
  note (`firstContactSql` orders by `(at, kind)`, and `followup` sorts before `note` even on a tie). So
  a press by the responsible person — the routed seller, or anybody on an unowned lead — on a created
  lead reads `contact_kind='followup'`; on a joined lead it reads `followup` when its date was today or
  earlier and `note` when it was NULL or in the future (nothing is cleared); the note also decides
  (`note`) when the presser is not responsible (the office on a seller's lead) or the date was already
  moved past today. Likewise a stage change through
  the card's ✏️ form clears today's booking in the SAME audit row (`clearsFollowUp`), so it reads
  `followup`; a move on the board or the card's stage control (`moveLead`, whose audit row carries
  only `stageId`/`lostReason`) reads `stage`. (The ERP's own test of the note calls `addActivity`
  directly, not the button, so it pins `note` for a path the button does not take.) Earliest wins
  **only during the arrival's first 24 h**: a stamped
  arrival is re-read while `created_at ≥ now − 1 day`, so a late-uploaded call can move
  `contacted_at` earlier only then; afterwards the stamp is final. Because the outcomes `at` is
  `greatest(created_at, contacted_at, updated_at, closed_at)`, such a day-1 correction can LOWER a
  row's `at` — only the `since` walks (recent / quarter / year, §B.2) catch it, never the stored
  cursor.
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
| 404 | `Not found`, text/plain — written by the two agent routes and `/api/leads/in/[source]` | door off, key wrong or absent, `X-ERP-Key` only, an unknown `[source]` — and any failure while **no** key was sent | `push_404` breaker |
| 404 | Next's own 404 page (HTML) | a path no route handles: a wrong path under `CRM_BASE_URL`, or `/api/agent/*` on a build without `agent-feed` | `push_404` breaker — the same behaviour; the fake and the contract checks require `text/plain` only for the routes above, never for an unknown path |
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
- **`limit`**: `min(5000, max(1, floor(Number(raw)) || 1000))` (`clampLimit`) — floored FIRST, so
  absent, `''`, garbage, `0` and anything that floors to 0 (`0.5`) → 1000; negative → 1; above 5000
  (and `Infinity`) → 5000. JavaScript `Number` also reads `1e3` (1000), `0x10` (16) and ` 7 ` (7),
  which Python `int()` refuses: the fake copies the expression (§B.4); the agent only ever sends a
  plain integer.
- A walk started from a stored cursor **without** `since` is labelled `mode: "full"` but holds only
  the rows after the cursor (§B.2 never deletes on such a walk).

### A.7 The suppression feed

```json
{ "mode": "full", "generated_at": "2026-10-24T08:56:38.538Z", "salt_id": "7a659490",
  "rule": "hmac-sha256(secret, last9digits) hex[0:20]",
  "entries": [{ "h": "bbc9434b92f2f7f81776", "kind": "client", "at": "2026-10-24T08:56:08.541Z" }],
  "next": null,
  "resume": "MjAyNi0xMC0yNFQwODo1NjozNy4yMTQwMDBafDAxYTE2ZDFmLWUyNWMtNzBhZC05OTNiLWVjM2JmM2FiZmYzYXwy" }
```

Copied from the stored capture `suppression_full.json` (07 §1.0), the first of its nine entries shown;
`resume` decodes to `2026-10-24T08:56:37.214000Z|01a16d1f-e25c-70ad-993b-ec3bf3abff3a|2`.

- `h = hmac_sha256(key=secret UTF-8, msg=tail9 ASCII).hexdigest()[:20]` (lower case); `salt_id =
  sha256(secret).hexdigest()[:8]` — it changes when the owner re-mints the key (every stored hash is
  then stale). `generated_at` and every `at` are ISO UTC with **milliseconds** and `Z`.
- `kind = client`: every phone on every client card, **active or inactive**; every phone on a
  PERSON record (CRM → Odamlar) that holds at least one client code; the phone of every lead that
  became a client (on a `won` stage, or carrying a `client_id` — attaching does not copy the lead's
  phone onto the card). A card or `crm_people` row whose `phones` is not a JSON array (typed by hand,
  an old import) is read as having **no phones** — by the feed, the belt, the client step and the XLSX
  export alike (`CASE WHEN jsonb_typeof(phones) = 'array' …`): fail-open, its numbers are never
  suppressed and an agent push on one becomes a new lead with a «🆕» push (A.13; the day-1 count is
  07 §5.3). `kind = lost`: a lead on a `lost` stage whose `lost_reason`, normalised, is
  one of the setting's labels (§A.10). Normalisation on both sides: every run of JavaScript `\s`
  (no-break space included) → one space, trimmed, lower case.
- **Which tails one phone text yields** (one element of a card's `phones` list — never the list
  joined): its whole last nine digits; the last nine of each part
  when it holds several numbers (split at `, ; / | \`, a line break (LF or CR), a run of two or
  more spaces or tabs, or spaces/tabs before `+`; `PHONE_SPLIT_PG`) — only a part of nine digits or
  more; and inside a part of **16 to 120** digits (the client import keeps a multi-number cell as one
  run): a part made **entirely** of 12-digit `998…` numbers (`^(998[0-9]{9})+$`) → each number's last
  nine; else a part whose length is a **multiple of 9** → each 9-digit chunk; anything else — a
  20-digit part, say — and any part over 120 digits → its last nine only (`tailsSql`,
  `agent-feed.ts`). Only nine-digit tails are in the feed. `services/client_import.py` mirrors these
  rules for the XLSX fallback (03 §2.2).
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
{ "generated_at": "2026-10-24T08:56:38.687Z",
  "rows": [{ "external_id": "cs:00000000-0000-4000-8000-000000000025:202610",
             "intake_id": "884a3c79-d3d6-4a9d-a027-ab33058b2988", "outcome": "created", "reason": null,
             "lead_id": "01a16d20-37ff-743a-8ca1-f16ddf450da3", "client_id": null,
             "stage_kind": "lost", "stage_name": "Yo‘qotildi", "lost_reason": "Qayta qilinmasin",
             "no_contact": true, "closed_at": "2026-10-24T08:56:37.196Z",
             "contacted_at": "2026-10-24T08:56:37.201Z", "contact_kind": "stage",
             "quoted_volume_m3": null, "at": "2026-10-24T08:56:37.201Z" }],
  "next": null,
  "resume": "MjAyNi0xMC0yNFQwODo1NjozNy4yMDE3NDNafDg4NGEzYzc5LWQzZDYtNGE5ZC1hMDI3LWFiMzMwNThiMjk4OHww" }
```

Copied verbatim from the stored capture `outcomes_since.json` (07 §1.0; the stage name is the seed's,
with the ERP's U+2018).

Exactly **15 fields**:

| Field | Meaning |
|---|---|
| `external_id`, `intake_id` | the push and its ledger row (uuid) |
| `outcome` | `created` / `joined` / `client` / `dropped` |
| `reason` | for `dropped`: `suppressed` / `capped` / `no_phone` / `no_contact`; `null` otherwise. A replay writes no row, so `replay` never appears |
| `lead_id` | the lead it became or joined (uuid; `null` when dropped or `client`) |
| `client_id` | the lead's client once won or attached, else the client it was recognised as (uuid or `null`) |
| `stage_kind`, `stage_name` | the lead's stage NOW (`open` / `won` / `lost`, the owner's name for it); `null` without a lead |
| `lost_reason` | the text chosen at the move (≤ 200) **only when it is a label** — one of the owner's lost-reason dictionary rows as they read NOW (active or retired, i.e. deactivated) or a label of `agent_no_contact_reasons`, compared normalised. A seller's free text is `null` (captured: `post_created_lost_free.json` → `null`). A RENAMED dictionary row (`saveLostReason` UPDATEs `lost_reasons.label` in place) no longer matches the old text, so every old lead carrying it comes back `null` unless the setting lists that text — at the next read of the row (its `at` does not move: the monthly `year` walk, or a later touch); 02 §2.11 keeps such a row's stored label |
| `no_contact` | `true` when the lead is lost with a reason `agent_no_contact_reasons` lists — the ERP's own answer; the agent keeps no copy of the setting |
| `closed_at` | when the lead was decided |
| `contacted_at`, `contact_kind` | the FIRST contact after the arrival (§A.4): `call` / `telegram` / `note` / `stage` / `followup` |
| `quoted_volume_m3` | the lead's kub as a number — the agent's own `kub` echoed back once the owner mapped it (a STATED volume only, §B.1), or whatever a person typed on the card — or `null` |
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
  link (a person through them is not the agent). Each door card has ONE toggle, inside the
  «Platformaning o'z formasi» fold, and the fold is drawn only for `admin.settings.manage` (the key
  also READS the client phone hashes): with the door ON it shows the webhook URL, the key and
  «O'chirish», which clears the secret so every route 404s; with the door OFF, «Yoqish va kalit
  yaratish» mints a NEW key. There is no rotate button — a re-mint is «O'chirish» then «Yoqish», which
  kills the old key for every sender. Other viewers receive `on` in the page data, but the fold is not
  drawn for them. The screen lists ACTIVE sources only (`inboundDoors`): deactivating the source itself
  happens in the CRM settings' source list and removes its card from this screen.
- Lead forms offer every source but the agent's (`personPickableSources`).

### A.12 Tests as built

Unit: `agent-feed.test.ts` (hash/salt vectors `k`/`901234567` → `2ea6869e504cebca50f0`, `k` →
`8254c329`; cursor round trip incl. 2-part and rolled dates; `parseSince`; `clampLimit`; reason
parser), `agent-feed-door.test.ts`, `agent-door-landing.test.ts` (503 on a landing / key-lookup throw,
200 elsewhere), `inbound-source-keys.test.ts` (seed fence, `inboundSourceKey`, `formSourceKey`,
`company` parse), `text-clip.test.ts`, `intake-id.test.ts` (`oversizeIdSpelling`/`externalIdSpellings`:
no second spelling at or under 2,048 UTF-8 bytes — ASCII, 4-byte emoji, 2-byte Cyrillic — and
`sha256:` + the digest computed in the test one byte past it; deterministic, two ids stay two).
Integration: `agent-feed.integration.test.ts` — full/paged/
delta feeds, kinds (active, inactive, person, won, attached, listed lost), tail splitting and the
998-run rule, the belt incl. the 7–8-digit suffix, per-family caps, `no_phone`, `company` on the
card, the clock (`contact_due_at NULL`, 90-day stamp, joined-live), the outcomes window, labels only,
`no_contact`, the fallback clause, the in-flight chain, an emoji at the name's 120-unit, a pair
value's 300-unit and the lenta note's 200-unit audit-excerpt edge (left out whole, never halved), a
lone surrogate and a NUL anywhere in the body through the real route (each 200, `created`, one note,
its retry a replay, in the outcomes, no `\u0000` stored — on the tiktok door and the public form too,
where the daily caps now count it), a NUL `external_id` (stored with U+FFFD, the retry a replay), a
body nested 3,000 deep stored whole and one 50,000 deep kept as `{"_truncated":true}`, an
`external_id` longer than the ledger's index holds through the agent's real route (three posts: 200
×3, one lead, one note, the ledger row under the hash, `inboundLanded` true) and on the Google door
(created, a replay, the per-phone cap counts it), and the control — a 2,6xx-byte random id and a
20,000-byte repetitive one on the `telegram` channel stored as sent and replayed; it walks the full
feed (green on a 17,000-entry copy). `field-map.integration.test.ts` maps the old 80-unit
spelling of a question with an emoji in its first 80 units and lands the whole question (kub filled);
`text-clip.test.ts` is a property test that `clipUnits` equals `slice` for every text `slice` could
store. Red proofs (fullprompt DECISIONS): 16 for the build (#1458), 21 for the first review (#1466),
16 for the second review (#1472), 12 for the third (#1475), 5 for the fourth (#1477).

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
- A landing that fails half-way (the database, after the lead's INSERT) leaves an orphan lead the
  retry normally joins (§A.2). Four narrower cases the ERP states (AGENT-FEED.md §5, DECISIONS #1475 —
  reasoned, not reproduced):
  (i) the ledger row COMMITS and its answer is lost on the way back: the agent is told 503, its retry
  is a replay answered 200, and nothing announces that arrival again — no «🆕», no contact clock — so
  its outcomes row reads `created` and never gets `contacted_at` (`open_untouched`, then `stale_open`,
  although no seller was told; the same window exists on every door);
  (ii) after a half-way failure the retry makes a SECOND lead when the orphan was moved to won or lost
  meanwhile (no open lead to join), or comes back `dropped/capped` (no `lead_id`) while the orphan
  stands, when other agent arrivals on that phone filled the cap;
  (iii) a cause that repeated on every attempt would answer 503 to every retry and add one lenta note
  each — up to 9 notes for the agent's 9 POSTs (§B.1); one is known since #1477 and no agent body
  reaches it: a body of tens of megabytes (about 40 MB of JSON, measured in postgres), which postgres
  refuses as jsonb after the lead was written (§A.2) — the agent's bodies are a few KB;
  (iv) the in-flight chain is per copy of the landing module — each channel that carries an id has one
  lander, production runs one app process, and across processes only the ledger's unique index stands
  (one ledger row, not one lead) — and its wait has no bound: a first delivery that never settles (a
  statement on a dead connection) holds every later delivery of that id until the ERP restarts. Each
  waiter holds only its HTTP request, so the agent's read timeout ends that try as `unknown` (§B.1).
- A card (or a `crm_people` row) whose `phones` is not a JSON array is read as having no phones on
  `agent-feed` (A.7): fail-open — its numbers are never suppressed, and an agent push on one becomes a
  new lead with a «🆕» push. Production `7bea3051` fails the other way (§B.3 Door row). Count such rows
  on day 1 (07 §5.3).
- Re-minting the key (the door's «O'chirish» then «Yoqish va kalit yaratish», A.11) rotates `salt_id`
  and makes the old key 404 at once on every route; turning the door off, or deactivating the `agent`
  source in the CRM settings' source list (which also removes it from the doors screen), makes
  everything 404.

### A.14 Still to do on the ERP side, and the owner's steps

Before production (fullprompt's `CLAUDE.md` rules): open the PR from `agent-feed` with an Uzbek
summary (no model identifier anywhere); reproduce CI's order on a fresh database once more after any
rebase; deploy by `docs/UPDATE.md` — verified non-zero backup, `docker compose build migrate`, then
`docker compose --profile https up -d --build`; end by reading the build stamp at `/api/version`
(no migration, so the count proves nothing). The seed row appears on that deploy.

Owner's steps after the deploy (Uzbek; the same five as AGENT-FEED.md §0):

1. **CRM → ⋯ → Kelgan arizalar → «Reklama havolalari» → «Agent (qidiruv)» → «Platformaning oʻz
   formasi» → «Yoqish va kalit yaratish».** Chiqqan kalitni agent serveridagi `.env` faylga
   `CRM_AGENT_KEY` qilib qoʻying (chatga yozmang). Eshik allaqachon yoqilgan boʻlsa, koʻrsatilgan
   kalitni nusxalang — yangisini faqat «Oʻchirish» → «Yoqish» bilan yarating: shunda eskisi hamma
   yuboruvchi uchun darhol ishlamay qoladi va agent butun roʻyxatni qaytadan oladi.
2. **CRM → ⋯ → CRM sozlamalari → «Yoʻqotish sabablari»**: «Qayta qilinmasin» sababini qoʻshing.
   **Diqqat:** roʻyxat hozir boʻsh boʻlsa, birinchi sabab qoʻshilgan zahoti lidni yoʻqotishda faqat
   roʻyxatdagi sabablarni tanlash mumkin boʻladi — jamoa hozir ishlatadigan sabablarni ham oʻsha
   zahoti qoʻshing. Roʻyxatdagi sabab nomini keyin oʻzgartirmang: kerak boʻlsa eskisini oʻchirib,
   yangisini qoʻshing (yoki yangi yozuvni 5-qadamdagi sozlamaga ham qoʻshing).
3. **Boshqaruv → Arizalar taqsimoti → «Oqim qoidalari»**: A qoida — manba `agent`, «Kub (dan)» = 1,
   kimga = yuk boʻyicha sotuvchilar; B qoida — manba `agent`, kubsiz, kimga = qolgan sotuvchilar
   (yoki umumiy navbatga qoldiring). A ni B dan yuqoriga, ikkalasini ham umumiy qoidalardan yuqoriga
   qoʻying.
4. Agent bitta sinov arizasini yuboradi (`make push-test`; u har bir kalitni sinov qiymati bilan
   yuboradi, `kub` = 1 ham). Keyin shu ekranning **«Forma savollari» → «Reklamadan kelgan yangi
   savollar»** roʻyxatida `kub` → **Kub (m³)** qiling (agent `kub` ni faqat mijozning oʻzi yozgan
   hajm uchun yuboradi; taxminiy hajm izohda qoladi va lidning kubiga yozilmaydi). Bu roʻyxat faqat
   soʻnggi 30 kunda kelgan arizalardagi savollarni koʻrsatadi: biror kalit koʻrinmasa, uni
   «Savolni qo'lda qo'shish» qatori orqali qoʻshing; xohlasangiz
   `stir`, `imports`, `score` ni lidning maxsus maydonlariga bogʻlang (avval maydonni yarating);
   `firma`, `telegram`, `instagram`, `website`, `address`, `evidence`, `utm_source` → **«Izohda
   qolsin»** (firma nomi lidning «Kompaniya» maydoniga baribir yoziladi).
5. **Boshqaruv → Sozlamalar → `agent_no_contact_reasons`**: 2-qadamdagi sabab nomi bilan bir xil
   ekanini tekshiring (standart: `Qayta qilinmasin`; bir nechta boʻlsa `;` bilan).

The list under step 4 is `seenKeys()` (`field-map.ts`): only the pair keys arrivals carried in the
last 30 days (`lead_intakes.fields`). An ordinary push carries only the keys its company has, so
`make push-test` gives its synthetic company a value for every optional key (§B.1) — each then
appears once; a key that dropped off the list (30 days without an arrival carrying it) is added by
hand through «Savolni qo'lda qo'shish» (`routing.mapManual`).

Routing reads the mapped `kub` first and otherwise the first «N kub» of name + landed note
(`mapped.volumeM3 ?? textVolume(name, note)` in `landOnce` — VERIFIED from the code, 2026-10-24).
Before step 4 a note that states «2 kub» therefore routes by rule A, and so does a band, because the
agent writes the band's low end right before «kub» in the HAJM text («taxminan 1 kubdan 3 kubgacha»
→ 1, «fura (taxminan 80 kub)» → 80, §B.1 `BAND_HAJM`) and never as `kub`; only a push with no
number at all routes by rule B. After step 4 a stated volume also arrives
as the mapped `kub` (the card's «Kub (m³)», the seller's «📦 N kub», the deal's quote when won); a band
still only through the text. (AGENT-FEED.md §0 says «kubsiz qoida (B)» for every arrival before the
mapping — the code reads the text first; open question 10.)

---

## B. Agent side

### B.1 `services/crm_push.py`

```python
ERP_KEY_HEADERS = ("X-GSR-Key", "X-ERP-Key")          # one value; the ERP reads X-GSR-Key only (A.2) — X-ERP-Key is the shared contract's old name, sent and ignored
PUSH_PATH = "/api/leads/in/{source}"                   # source = settings.crm_push_source ('agent' | 'other')
UTM_SOURCE = "clientscrapper"
IDENTITY_KEYS = ("external_id", "name", "phone", "note")       # the four identity names the agent uses; fromPlain reads them as identity, never as note lines (A.2)
RESERVED_BODY_KEYS = frozenset({"key", "google_key", "secret", "token",   # read as the shared secret, dropped from note, pairs and ref
                                "id", "ism", "telefon", "izoh",          # fromPlain's other identity spellings: consumed, never a note line (A.2)
                                "user_column_data"})                     # a non-empty array switches the body to the Google parser
                                # fromPlain matches body keys LOWER-CASED: never a payload field whose lower-cased name is here
LANDED_NOTE_MAX = 1000  # ERP: note + one 'key: value' line per extra key, whitespace collapsed, cut at 1000 UTF-16 units (clipUnits)
NOTE_MAX = 700          # our own note's budget inside it
PUSH_PREVIEW = 159      # the seller's Telegram push shows «<first 159 code points>…» of the landed note (clipText — the one ERP cut that counts code points)
QUOTE_MAX = 140
OPENER_MAX = 220
NAME_MAX = 120          # after the ERP's whitespace collapse; every limit here is measured by text_len() below
COMPANY_MAX = 200       # leads.company on a created lead
PAIR_KEY_MAX, PAIR_VALUE_MAX, PAIRS_MAX = 80, 300, 30  # ERP capturePairs
def text_len(s: str) -> int: return len(s.encode("utf-16-le")) // 2   # UTF-16 units — the measure of both agent-feed (clipUnits, DECISIONS #1473) and production 7bea3051, which the «Boshqa» fallback posts to (trim() = .replace(/\s+/g, ' ').trim().slice(0, max)); a cut never halves an emoji on agent-feed, and on production an emoji at a cut throws after the lead is written
PUSH_DAYS = range(0, 6)                                # Mon–Sat
PUSH_FROM, PUSH_TO = time(9, 30), time(17, 30)         # Asia/Tashkent (02 §2.10 item 2)
ATTEMPT_DELAYS = {2: timedelta(minutes=5), 3: timedelta(minutes=30)}   # between push_log attempts (the HTTP tries INSIDE one attempt are with_backoff's, below)
BAND_HAJM = {"1_3": "taxminan 1 kubdan 3 kubgacha", "3_10": "taxminan 3 kubdan 10 kubgacha",   # a band's words for the HAJM section — never sent as `kub`:
             "container": "konteyner (taxminan 28 kub)", "truck": "fura (taxminan 80 kub)"}   # a mapped kub becomes the lead's quoted volume (A.2). The ERP's textVolume routes by the number RIGHT BEFORE the first «kub», so each band puts its LOW END there (04 §4.3): 1_3 → 1, 3_10 → 3, container → 28, truck → 80 — recomputed with textVolume, 2026-10-24; «taxminan 1–3 kub» would route as 3 and «3–10 kub» as 10, wrong for any rule with a «gacha» window
JS_SPACE = r"[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"   # JavaScript's \s (= trim()'s set), written out; NOT str.split()/str.strip()
JS_SPACE_RUN = re.compile(JS_SPACE + "+")                                 # the ERP's \s+ collapse
def js_trim(s: str) -> str: return re.sub(f"^{JS_SPACE}+|{JS_SPACE}+$", "", s)   # String.prototype.trim()
TEXT_VOLUME_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)" + JS_SPACE + r"*(?:kub|куб|m3|м3|m³|м³)", re.I)   # field-map.ts textVolume (first match of name + landed note), for the routing tests only

class PushPayload(BaseModel, frozen=True):
    # FIELD ORDER IS THE JSON BODY ORDER, which is the order of the ERP's note lines (A.2):
    # utm_source comes right after the note so that the fallback door's keyword rule always sees it
    # inside the 1000-unit cut, and so that no digit at the end of the note can be read by
    # the ERP's textVolume as «<n> kub» together with the next line.
    external_id: str        # cs:<company_uuid>:<yyyymm>, Tashkent month at claim time
    name: str               # company display_name (2..120 UTF-16 units by text_len); never the person alone
    phone: str              # E.164 from contacts.phone; the ERP keeps the last 9
    note: str               # render_note(); ≤ NOTE_MAX
    utm_source: str = UTM_SOURCE
    kub: str | None         # a STATED volume only, as a plain number string ("2", "25"); omitted for a band (its words go in the HAJM section, BAND_HAJM), when unknown and when < 1
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
def build_payload(c: CandidateView) -> PushPayload          # pure; raises NoPhone; every value through services/text.well_formed: no lone surrogate AND no U+0000 (below)
def render_note(c: CandidateView) -> str                     # pure; sections below
def landed_note(p: PushPayload) -> str                       # pure mirror of the ERP's assembly (A.2), for the length tests
def canonical_sha256(p: PushPayload) -> str                  # sorted keys, no spaces, None dropped
def push_window_open(now: datetime) -> bool                  # tz-aware Tashkent
async def push_one(session, candidate_id: UUID, *, now: datetime, erp: ErpClient, repush_of: int | None = None) -> PushResult
async def crm_push_job(session_factory, erp, *, now) -> JobReport     # worker: every 60 s inside the window
```

`landed_note(p)` = `JS_SPACE_RUN.sub(" ", "\n".join([js_trim(p.note)] + [f"{k}: {js_trim(v)}" for k, v in extras_in_order(p) if js_trim(v)])).strip(" ")`
(after the substitution every run is one space) — `fromPlain`'s assembly: each value as `str()` reads
it (`inbound-webhook.ts`: a string JS `trim()` leaves empty is **no line and no pair**, a non-empty one
is used trimmed), then the ERP's `\s+` collapse with the JavaScript class written out, because
Python's `str.split()`/`str.strip()` is a different class: it also splits on U+001C–U+001F and
U+0085, which JS `\s` does not, and does NOT split on U+FEFF, which JS does (enumerated over every
code point in Python and Node, 2026-10-24). An empty or whitespace-only value would otherwise add a
«k: » line the ERP never writes and miscount the length; `build_payload` sends none anyway (an empty
value becomes `None`, dropped by `exclude_none`), and a test with one pins the skip.
`build_payload` additionally maps U+001C–U+001F and U+0085 to a space in every value (the ERP would
keep them as invisible control characters on the lenta), and `well_formed` drops every U+0000 and
every lone surrogate from every value: production `7bea3051`, which the «Boshqa» fallback posts to,
handles neither — a NUL in the name or phone fails `createLead` itself (200, nothing recorded), and a
NUL or a lone surrogate anywhere else (the note, any pair, or the whole body stored as `ref` jsonb)
fails a later write after the lead stands (200 and an orphan lead); `text_len` cannot prevent either.
On agent-feed the same character is stored silently as U+FFFD on the lenta (A.2). A test pins that a
NUL in a quote, the name or `firma` never reaches the body. `build_payload` guarantees
`text_len(landed_note(p)) ≤ LANDED_NOTE_MAX` by shortening `render_note` (the drop order below), so the
ERP never cuts; `name`, `firma` and every extra value respect the limits above by `text_len` — so
nothing is cut on either ERP build, and no cut can halve an emoji on the old one; no field's
lower-cased name is in `RESERVED_BODY_KEYS`, the only identity fields are `IDENTITY_KEYS`, and only
one of `company`/`firma` is ever sent (`firma`) — a test pins all three over
`PushPayload.model_fields` (fromPlain's `known` set, `inbound-webhook.ts`).

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
HAJM: {stated «2 kub» | BAND_HAJM: taxminan 1 kubdan 3 kubgacha / taxminan 3 kubdan 10 kubgacha / konteyner (taxminan 28 kub) / fura (taxminan 80 kub)} · {per_month if known}
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
is written as «N kub» in the HAJM section **and** sent as `kub`; a band is written as its
`BAND_HAJM` words — with its LOW END as the number right before «kub», so the ERP's `textVolume`
routes it by that figure (1, 3, 28 or 80; rule A, A.14) — and is **never** sent as `kub`: a mapped
`kub` becomes the lead's «Kub (m³)», the seller's «📦 N kub» and the won deal's quoted volume (A.2),
and a guess must not stand there as the customer's figure. `textVolume` reads the FIRST
«N kub|куб|m3|м3|m³|м³» of name + landed note, and NEGA TOPILDI comes first: a quote that carries its
own «N kub» routes by the customer's number, before the HAJM line. The FIRST match alone decides:
the ERP's `parseMeasure` turns a first «0 kub», or a number ≥ 100000, into no volume (no windowed
rule matches) and never looks at a later match. The routing test asserts `TEXT_VOLUME_RE` over
`name + "\n" + landed_note(p)`, with `parseMeasure`'s range applied to the first match, gives the
stated volume, or the band's low end when no earlier text carries a number.

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
**only when a re-send cannot land the push twice**: the connection never opened
(`RETRY_IN_ATTEMPT`), or a 5xx came back. A 5xx is retried because a re-send is **fenced** — the
ERP's replay fence and its in-process per-id chain turn a second delivery of the same `external_id`
into a replay (A.2) — not because a 5xx proves nothing landed: the ERP's own 503
`{"error":"unavailable"}` normally means no ledger row — rarely the row committed and only its answer
was lost, and the retry is then a replay answered 200 for an arrival that was never announced or
clocked: it shows as `created` with no contact (`open_untouched` → `stale_open`, A.13 (i)) — and a
proxy's 502/504 (Caddy in front of the app) can come after the app recorded the arrival, and the
retry is then a replay answered 200 too. After a
read/write timeout, a read/write error or a protocol error
(`MAY_HAVE_LANDED`) there is **no resend inside the attempt**: the first request may still be
running, and although the ERP now chains a second delivery of the same id behind it (A.2), that
second request waits too and can time out the same way. The attempt ends `unknown` and the
reconcile path settles it (02 §2.10 rule 5). A further **attempt** (`attempt+1`, identical
`payload_sha256`) may be claimed only after a `failed` attempt and `ATTEMPT_DELAYS[n+1]` — 5 min
before attempt 2, 30 min before attempt 3 — so the worst case is 9 POSTs (and, should a half-way
failure repeat on every try, up to 9 lenta notes on the orphan lead, A.13 (iii)), and after attempt 3
the candidate returns to `review`. Outcome mapping (the `push_log` CHECK of 02 §2.10 is
`(status='sent' ⇒ 2xx) AND (2xx ⇒ status IN ('sent','unknown'))`):

| Result | `push_log` | candidate | side effect |
|---|---|---|---|
| 200 `{"ok":true}` (recorded, any outcome — or a replay) | `sent`, `http_status` | `pushed`, `pushed_at`, `external_id` | — (the outcomes feed says what it became) |
| 2xx with any other body (HTML, empty, bad JSON) | `unknown`, `http_status`, `error='http_2xx_badbody'` | stays `approved`, blocked by `ux_push_log_delivered` | not retried (the lead may have landed); settled by 02 §2.10 rule 5 |
| `MAY_HAVE_LANDED` on any try | `unknown`, `error='timeout'` | stays `approved`, blocked | no resend; `reconcile_job`'s quick outcomes read (≤ 10 min, §B.2) promotes it, else rule 5's 48 h |
| 404 | `failed`, `error='http_404'` | stays `approved` | breaker: `sources.erp_sync.breaker_until = now+6h`, `breaker_reason='push_404'` (`/yoq crm` clears it by hand, 06 §1.2; it also expires); health problem `push_404`; owner alert once per 6 h — `reminder_log(kind='alert:push_404')`, recovery `recovered:push_404` (06 §4.1): «ERP kaliti yoki manba notoʻgʻri: /api/leads/in/{source} 404 qaytardi. Kelgan arizalar → Reklama havolalari → Agent → kalitni tekshiring.» |
| 3xx (Caddy answers `308` to an `http://` `CRM_BASE_URL`, `--profile https`) | `failed`, `error='http_3xx'` | stays `approved` | never followed (`follow_redirects=False` — the key is not re-sent to wherever it points); breaker as 404 (`breaker_until = now+6h`, `breaker_reason='erp_redirect'`), health problem `erp_redirect`, owner alert once per 6 h (`alert:erp_redirect`): «ERP manzili yoʻnaltiryapti (HTTP n): `.env` dagi CRM_BASE_URL `https://` bilan boshlanishi kerak.» `make doctor` refuses an `http://` `CRM_BASE_URL` (06 §3.3) |
| other 4xx | `failed`, `error='http_4xx'` | → `review`, note «ERP rad etdi (HTTP n)» | owner alert — the ERP door answers the agent (which always sends `X-GSR-Key`) only 200 / 404 / 503, so this is a proxy or a wrong `CRM_BASE_URL` |
| 5xx (503 JSON, the ERP's empty-body 500, or a proxy's HTML page — any 5xx) / connect error after 3 tries | `failed`, `error` = `http_5xx`, `conn` or `timeout` (a connect timeout) | stays `approved`; next attempt after `ATTEMPT_DELAYS[n+1]`; after attempt 3 → `review`, note «CRM javob bermadi» | health problem `push_failing` when 3 consecutive candidates fail |
| `sending` row older than 10 min (crash) | `unknown` (`reconcile_job`) | stays `approved`, blocked | as `MAY_HAVE_LANDED` |

What 200 does and does not prove: on the `agent` door, that the ERP recorded the arrival (or had
recorded it before). On the «Boshqa» fallback door (§B.3) a 200 can also be a landing that threw
with nothing recorded — which is why rule 5 also reconciles `sent` rows. A 503 after the ERP's
`createLead` (a database failure half-way) leaves an orphan lead; the next try normally JOINS it and
the outcome reads `joined` for a lead nobody else was working (A.2) — the agent never reads `joined`
as «a person already had this company» (no side effect keys on it, 02 §2.11). The retry can instead
make a second lead (the orphan was moved to won/lost meanwhile) or come back `dropped/capped` while
the orphan stands (A.13 (ii)): the outcome row then speaks for the retry, not for the orphan. The
fallback door can leave the same orphan behind a 200: a failure after `createLead` (production's
UTF-16 cut through an emoji, which `text_len` prevents; a NUL or a lone surrogate, which `well_formed`
prevents; or any database error) leaves the lead standing with no ledger row; rule 5's re-push
through the `agent` door after the switch then JOINS it (`joined`; fake mode `throw200_orphan`,
§B.4).

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
`display_name='TEST agent — oʻchiring'`, a visibly synthetic value for **every optional key** of
`PushPayload` (`kub: "1"` as a stated volume — so rule A routes it, and once `kub` is mapped it lands
as the test lead's quoted volume — STIR `300000099`, `telegram`/`instagram` `namuna_test`, `website`
`test.example.uz`, an address, `imports`, `evidence`), so that every key arrives once and appears
on the ERP's «Reklamadan kelgan yangi savollar» list (A.14 step 4; `seenKeys()` lists only keys
arrivals carried in the last 30 days), one company-tier `contacts` row with `TEST_PHONE`, an
`approved` candidate (`approved_by='owner:push-test'`, `score=0` with `score_breakdown.gates.test=true`
so no scorer or card ever touches it) — and pushes it through `push_one` with exactly two checks
skipped: 04 §7 item 13 (score ≥ 60) and 04 §6 gate 1 `is_b2b` (a synthetic company has neither a
score nor B2B evidence — checked against 04 §6/§7, 2026-10-24); gates 2–7 and every other checklist
item (suppression, freshness, breaker, window, `phone_recent`, caps) are kept. So the `external_id`
(`cs:<new uuid>:<yyyymm>`) is new on every run — a fixed id would be a silent replay on the second run of a month — and the `push_log` row
exists, which the outcomes read needs (rows whose `external_id` the agent never pushed are ignored,
§B.2). It prints the HTTP status, the `external_id` and the company uuid (for `tools/purge`).
`TEST_PHONE` must be a number **outside the client book and outside every «Qayta qilinmasin» lead**,
or the push lands silently as `dropped/suppressed` (still 200); `make push-test` refuses to run when
the local gate already suppresses it. Used for step 4 of A.14 and 07 §5.6. It reaches the ERP only
through `push_one`, so it needs no exemption from the push guard (07 §1.2): the exemption that
`tests/test_push_requires_approved.py` carries as built (T-04) — `tools/push_test.py` calling `_post`
directly with the zero-uuid id `cs:00000000-0000-0000-0000-000000000000:<yyyymm>` — is the pre-T-07
design, a fixed id that would replay silently on a month's second run; T-28 deletes it.

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
just sent → `FeedContract('runaway')`. HTTP answers (A.5): 404 — and a 401/403, which the ERP never
sends but a proxy in front of it might, decided here once for both feeds — → the same misconfiguration
as a push 404 (breaker, `push_404`, stop; any other 4xx on a feed is `feed_contract` code
`bad_request`); 3xx → as a push 3xx (never followed; breaker, `erp_redirect`,
stop); 400 → `feed_contract` code `bad_request` with the ERP's
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
   retires whole classes of tails (it did once, in `aefbf278`: the 9-digit chunks of 998 runs left the
   feed; the 7–8-digit split fragments left only the belt — the feed has always kept
   `length(tail) = 9`). The branch was never deployed, so no stored pull predates it.
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

Before the owner keeps a lost-reason dictionary every lost reason is free text and `lost_reason` is
`null` — **except** a text equal, after normalisation, to an `agent_no_contact_reasons` label (e.g.
«Qayta qilinmasin» typed before the dictionary existed), which comes back verbatim (`outcomesPage`'s
`labelled` = the setting's labels OR a dictionary match). Lost rows land as `lost_no_contact` (when
listed in the setting — `no_contact` decides first) or `lost_other`; the regex classes match only
labels, so the precision split of F-11 needs the owner's labels (01 §7 question 1, open question 12).

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
| Door | `CRM_PUSH_SOURCE=other`; `CRM_AGENT_KEY` = the **«Boshqa»** door's secret (on the same screen: another platform may already be posting to «Boshqa», so if its door is ON, copy the key shown — a re-mint is «O'chirish» then «Yoqish va kalit yaratish», which kills the old key for every sender, A.11); body always carries `utm_source=clientscrapper` (right after the note, §B.1); `push_log.endpoint='/api/leads/in/other'`, `source_key_sent='other'`. That door is an ordinary advert door: no belt, the 15-minute reminder fires, its pushes count in the people's per-phone cap, a client is recognised only when exactly one active card holds the phone (A.3 step 6), and a landing that throws is still answered **200** with no ledger row — possibly with an orphan lead standing (§B.1), which a later re-push through `agent` joins. Production without `agent-feed` also still throws on text after (or, for a NUL in the name or phone, at) `createLead`: an emoji at a UTF-16 cut, a NUL anywhere (a text column refuses it, and `ref`'s jsonb refuses `\u0000`) and a lone surrogate (jsonb) — `text_len` (§B.1) keeps every value under the cut and `well_formed` drops every NUL and lone surrogate, so none of the three can happen. One failure the agent cannot avoid: production's `activeClientsByPhone` runs `jsonb_array_elements_text(phones)` unguarded over every active card, so ONE card whose `phones` is not a JSON array makes every arrival with a phone throw before `createLead` — on «Boshqa» that is 200 with nothing recorded, for every push (whether production holds such a card is UNVERIFIED; the read-only count of 07 §5.3 runs before the fallback) | set `CRM_PUSH_SOURCE=agent`, replace `CRM_AGENT_KEY`, restart `worker`. Candidates already `pushed` are **not** re-pushed by hand (same `channel`, so a replay); rule 5 re-pushes only a push the outcomes feed never shows |
| Feeds | **not configured** (`feeds_configured()` false): the «Boshqa» key reads nothing — every GET would be 404 and trip the `push_404` breaker — so no feed call is made | the first suppression full pull and the outcomes `year` walk run on the day of the switch |
| Routing | the `other` door is also where a bot `?start=ad_<unknown or agent>` deep link and a hand-posted form with an unknown or `agent` source land (`inboundSourceKey`; an unknown `/ariza?manba=` goes to «Sayt» — `formSourceKey`, the same `: 'sayt'` fallback in production `7bea3051`); a rule `manba=other` + keyword `clientscrapper` routes agent arrivals to the cargo managers. VERIFIED from the code: the keyword is a case-insensitive «contains» over name + the landed note (`matchRoute` over `[name, note]`, and the note carries the `utm_source: clientscrapper` line within the 1000 cut, §B.1); still confirm once with `make push-test` (07 §5.6) | delete that rule after switch-over |
| Suppression | `make import-clients FILE=clients.xlsx` (ERP **Mijozlar → XLSX**, the column «Phones» — exported from an unfiltered screen with no saved view, ≤ 5,000 rows, inactive cards included; 03 §2.2 has the export's shape and the per-element tail rule); `services/client_import.py` stores `suppression(kind='client', key_type='phone_tail', source='manual_import')` — clear tails, because the future agent secret is unknown and hashes would not match. Lost leads invisible; `/holat` says «yoʻqotilgan lidlar roʻyxati yoʻq». Weekly re-import. These tails are the **only** guard for a multi-code client: the «Boshqa» door has no belt and knows a client only when exactly one active card holds the phone (A.3 step 6), so a phone on two codes would land as a new lead | a full pull retires each `manual_import` row whose tail's hash it carries (§B.2 step 5); tails it does not carry stay until the owner removes them |
| Outcomes | none; labels stay `pending`, precision NULL, `auto_push` impossible; a fallback push's fate is unknown until the switch | the `year` walk reads the fallback months through the `other` + `utm_source` clause (A.9); rule 5 then re-pushes, through the `agent` door, the ones the ERP never recorded (≤ 30 d old, payload kept) |
| Reminder | the ERP's 15-min reminder **fires** for `other` arrivals — tell the owner; keep `PUSH_DAILY_CAP ≤ 10` during the fallback. The binding «no reminder for agent leads» holds only on the `agent` door — owner decision 01 §7 question 4 (open question 15) | — |

### B.4 Fake ERP for tests — `tests/fake_erp.py`

`FakeErp(secret, default_mode='ok')` → `httpx.MockTransport(fake.handler)` (async handler); the client
under test is built with `ErpClient(base_url='http://erp.test', key=…, transport=…)`. It copies the
real ERP's codec and answers (A.2, A.5–A.9), not a simplification of them: when fullprompt changes,
the captured fixtures (07 §1.7) and this fake change together.

- **State**: `posted: list[tuple[source, headers, body]]`, `intakes: dict[external_id, row]` keyed
  by the id as the ERP stores it — `storable_text(external_id)` (every NUL and lone surrogate →
  U+FFFD) BEFORE the replay check, so the outcomes it serves hand such an id back with U+FFFD in its
  place, as the ERP does (A.2; the agent's own `cs:` ids are unaffected; the ERP's `sha256:` spelling
  of an id over 2,048 UTF-8 bytes the index refuses is not modelled — the agent's ids are 46 bytes and
  such an id never reaches the outcomes feed) (`intake_id` uuid4,
  `created_at` microseconds), `leads: dict[lead_id, lead]` (an orphan lead may
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
  reason; `404` → 404; `503` → 503 `{"error":"unavailable"}`, nothing recorded; `503_committed` → the
  intake is written as for `ok` and the answer is 503 (the ledger row committed, its answer lost, A.13
  (i)): the next POST of that id is a replay, 200, and the fake's lead never gets a contact;
  `nonjson5xx` → a non-JSON 5xx — by default 500 with an empty body (the ERP's own answer when the
  database fails before the key check and no `X-GSR-Key` was sent, A.2), parametrised as a proxy's
  `text/html` 502 page; `orphan` → creates a lead with no intake and answers 503 (a throw after the
  lead's INSERT); the next POST of that id joins it (`joined`) — through the fake's own landing order,
  so a test that first moves the orphan to won/lost gets a second lead (`created`) and one that fills
  the phone's cap gets `dropped/capped` (A.13 (ii)); `throw200` (only `source='other'`) → 200
  `{"ok":true}`, nothing recorded (the fallback door's answer to a failed landing);
  `throw200_orphan` (only `source='other'`) → 200 `{"ok":true}` with a lead and no intake (a failure
  after `createLead` on the fallback door); a later POST of that id through `agent` joins it
  (`joined`); `slow` → the
  handler waits on `held[external_id]`, the test raises `httpx.ReadTimeout` on the client side (a
  `MockTransport` wrapper with a timeout), and the intake is written when the test calls
  `fake.release(external_id)` — a first request still RUNNING, not one that finished; a concurrent
  second POST of the same id waits for the first (the real per-id chain) and is a replay;
  `badjson` → 200 `'<html>'`. A POST whose `external_id` already has an intake → 200, nothing
  changes (replay), whatever the first outcome was. The order is the ERP's (A.3): (1) no name after
  the whitespace collapse **and** fewer than 9 phone digits → `dropped/no_contact` (written
  `ON CONFLICT DO NOTHING`, before the fence); (2) replay; (3) `source='agent'` and fewer than 9 digits
  → `dropped/no_phone`; (4) caps (rolling 24 h, the source cap over every arrival of that source);
  (5) belt; then the mode — so a body with no name and phone `12345` is `no_contact`, not `no_phone`.
  The landed note is built
  as A.2 (`fromPlain`: extras in body order, each value JS-trimmed and skipped when that leaves it
  empty; then the whitespace collapse, `storable_text`, and `clip_units(…, 1000)` — UTF-16 units, a
  high surrogate dangling at the edge dropped so an emoji is left out whole; the name the same at 120)
  and kept for assertions.
- **`GET /api/agent/suppression` / `outcomes`** — key as above; then `cursor` → `decode_cursor` FIRST,
  then `since` → `parse_since` (both invalid → `bad_cursor`), either invalid → 400 `{"error":"bad_cursor"}` / `{"error":"bad_since"}`;
  `limit` by `clamp_limit`; mode `feed_503` → 503 `{"error":"unavailable"}`, `feed_timeout`,
  `feed_rule_changed`, `feed_shape` (a row with a `phone` key) per path. Bodies exactly as A.7 / A.9
  (`generated_at`, ms `at`, `resume`); suppression `mode` = `delta` iff `since`.
- **Codec** (a copy of `agent-feed.ts`; vectors pinned in 07 §1.5):
  ```python
  CURSOR_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
  def encode_cursor(at_us: str, id_: str, n: int) -> str:            # at_us 'YYYY-MM-DDTHH:MM:SS.ffffffZ'
      return base64.urlsafe_b64encode(f"{at_us}|{id_}|{n}".encode()).decode().rstrip("=")
  def decode_cursor(raw: str | None) -> tuple[str, str, int] | None | Literal["invalid"]:
      # '' or None → None (absent); padding restored; 2 parts (n = 0) or 3; at ~ CURSOR_AT and its
      # first 19 characters survive the round trip through a real instant (a rolled date '02-30' AND an
      # invalid time 'T24:00:00Z' or ':60' are refused); id a uuid (lower-cased); n ^\d{1,6}$ — else "invalid"
  def parse_since(raw: str | None) -> str | None | Literal["invalid"]:
      # '' or None → None (absent: `since=` with no value is mode 'full');
      # ^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})$, real date, h ≤ 23,
      # m ≤ 59, s ≤ 59, and the offset itself in range (Date.parse NaN on +24:00, +12:60, +1260; +23:59
      # is accepted) → UTC with six fraction digits; '0', a date alone, a naive time, a space
      # where '+' was → "invalid"
  def clamp_limit(raw: str | None) -> int:   # the ERP's expression (A.6): n = floor(js_number(raw)); NaN, 0 or -0 → 1000; then clamp to [1, 5000]
      # js_number = ECMAScript StringToNumber (what Number(raw) does): absent → the ERP reads q.get('limit') = null → 0;
      # strip JS whitespace incl. line terminators and U+FEFF (JS_SPACE); '' → 0; a signed decimal ('+7', '-3', '.5',
      # '5.', '1e3', '1E3') or a signed 'Infinity' (exactly that spelling); an UNSIGNED 0x/0o/0b prefix, any case
      # ('0x10' → 16, '0O7' → 7, '0b11' → 3); anything else NaN ('-0x10', '+0x10', '7px', '1_000', 'infinity', 'nan',
      # '0x'). Never Python float()/int(): float('1_000'), float('infinity') and float('nan') read what JS refuses.
      # Vectors (recomputed in Node over clampLimit, 2026-10-24): None → 1000, '' → 1000, ' 7 ' → 7, '\n7\n' → 7,
      # '\ufeff7' → 7, '\u00a07' → 7, '+7' → 7, '1e3' → 1000, '0x10' → 16, '0b11' → 3, '0o7' → 7, '.5' → 1000,
      # '0.5' → 1000, '1.9' → 1, '-0' → 1000, '-0.5' → 1, '-3' → 1, '-Infinity' → 1, 'Infinity' → 5000, '5001' → 5000,
      # '-0x10' → 1000, '7px' → 1000, '1_000' → 1000
  ```
  Keyset: suppression `(at_us, id, n) > cursor`, outcomes `(at_us, intake_id) > cursor`; `since`
  strict `>`; `next` = the cursor of the last row when one more row exists, else `null`; `resume` =
  the last row's cursor, or the sent cursor on an empty page, or `null`.

Tests (pytest, async): `test_payload_sections_and_limits` (order, NEGA TOPILDI first and whole in 159
code points, note ≤ 700, `text_len(landed_note) ≤ 1000`, URL never cut, no person name without `legal_basis`,
`kub` rules, `utm_source` right after `note`, `exclude_none`, no field in `RESERVED_BODY_KEYS`,
`text_len` limits with an emoji at each edge), `test_payload_drops_nul_and_lone_surrogates` (a NUL in
a quote, the name or `firma`, and a lone surrogate anywhere, never reach the body),
`test_landed_note_skips_empty_values` (an empty and a whitespace-only extra make no «k:» line, as
`fromPlain`), `test_band_routes_by_low_end` (`TEXT_VOLUME_RE` over name + landed note → 1, 3, 28, 80
for the four bands; the stated volume for a stated push), `test_external_id_month_tashkent`,
`test_canonical_sha_stable`, `test_push_200_marks_pushed`, `test_push_404_trips_breaker_and_alerts_once`,
`test_push_503_three_tries_then_failed_then_review_after_attempt_3` (nothing recorded by the fake
until a try succeeds), `test_push_nonjson5xx_is_retried_like_503` (an empty-body 500 and a proxy's
HTML 502), `test_push_503_committed_retry_is_replay` (attempt 1 `sent` after the in-attempt retry's
200; one intake; the outcome `created` with no contact → `open_untouched`),
`test_push_read_timeout_is_unknown_without_resend` (mode `slow`: exactly ONE POST in the attempt,
`push_log.unknown` + `error='timeout'`; after `fake.release()` the `unknown` walk promotes it to
`sent` and the candidate to `pushed`), `test_push_orphan_retry_joins` (mode `orphan`: the second try
lands `joined`, the outcome row is `landed_joined`, no side effect),
`test_push_2xx_badbody_is_unknown` (never `sent`; promoted by outcomes),
`test_push_3xx_is_not_followed_and_trips_breaker` (`erp_redirect`, one POST),
`test_fake_landing_order` (no name + `12345` → `no_contact`; a name + `12345` → `no_phone`),
`test_fake_clamp_limit_vectors` (the codec comment's vectors), `test_fake_external_id_storable` (an
id with a NUL is stored and reported with U+FFFD; its re-send is a replay),
`test_landed_note_js_whitespace` (U+FEFF collapsed, U+001C/U+0085 mapped by `build_payload`),
`test_reconcile_repush_joins_fallback_orphan` (`throw200_orphan`, then the switch: the re-push is
`joined`, `landed_joined`, no side effect),
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
database (07 §1.0 lists the files) and still valid at `d138b3bc`: neither the third nor the fourth
review changes a captured answer (no captured value reaches a cut or carries a NUL, a lone surrogate
or a deep body, and no captured `external_id` is over 46 bytes, A.0);
`tests/test_erp_feeds_contract.py` runs the parsers over them.

### B.5 Sequences

```
PUSH (happy)                                  PUSH (404)
owner ✅ → candidate approved                 push_one → claim(sending,1) → POST → 404
crm_push_job (window open, cap ok)            push_log failed/http_404; breaker 6 h; problem push_404
 └ push_one: FOR UPDATE; gate ok              owner ← «ERP kaliti yoki manba notoʻgʻri …»
   claim push_log(sending, attempt 1) COMMIT  next ticks: refused 'breaker' until the key is pasted and /yoq crm clears it (or 6 h pass)
   POST /api/leads/in/agent  ← X-GSR-Key      /holat shows push_404 until a 2xx
   ERP: no_contact? replay? no_phone? caps? belt → one client card? → open lead? → create → route(kub|text) → push seller (clock, no due)
   ledger row LAST → 200 {ok:true} → sent; candidate pushed
nightly: outcomes row external_id → label

PUSH (read timeout)                           PUSH (503)
POST … ReadTimeout (ERP may still be running) POST … 503 {"error":"unavailable"} (normally nothing recorded)
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
| ERP down (conn refused / connect timeout / 503 / any other 5xx — the ERP's empty-body 500, a proxy's HTML page) on push | 3 tries in ≤ 15 s; `failed`; attempt 2 after 5 min, 3 after 30 min; then `review` | `/holat` `push_failing`; digest line |
| Read timeout / dropped connection after the request was sent | `unknown`, no resend; the 10-minute `unknown` walk or the nightly read promotes it; 48 h without an outcome → `push_unconfirmed` + one re-push (02 §2.10 rule 5) | nothing, or `push_unconfirmed` |
| ERP database fails half-way through a landing | 503 → retry; the retry normally joins the orphan lead (`joined`, «🔁 Qayta yozdi» to the seller). It can instead make a SECOND lead (the orphan was moved to won/lost meanwhile) or come back `dropped/capped` while the orphan stands (A.13 (ii)) — the outcome row is the retry's; and a cause that repeats on every try adds one lenta note per POST, ≤ 9, with no Telegram push (A.13 (iii) — the one known is a body of tens of MB, which the agent's few-KB bodies cannot reach) | nothing; stated in A.2, A.13 |
| The ERP's ledger row commits but its answer is lost (A.13 (i)) | 503 → the in-attempt retry is a replay, 200 → `sent`, `pushed`; the outcome reads `created` and never gets `contacted_at` → `open_untouched`, `stale_open` after 30 d — although no seller was told | nothing; a `stale_open` row nobody was told about (reasoned by the ERP, not reproduced) |
| A first delivery that never settles in the ERP (A.13 (iv)) | every later POST of that id waits in the ERP's per-id chain until it restarts; the agent's 20 s read timeout ends the try as `unknown` (no resend); the `unknown` walk, then rule 5 settle it | nothing, or `push_unconfirmed` |
| ERP down on feeds | `with_backoff` 4 tries (503 is retried, never `push_404`); collector run `failed`; `suppression_fresh()` false (no full pull ≤ 26 h or no delta ≤ 3 h) → gate `suppression_stale` → pushes stop | alert «ERP roʻyxati 26 soatdan beri yangilanmadi — push toʻxtatildi» (06 §4.2 renders the real `{age}`) |
| 404 on any call | breaker 6 h (`sources.erp_sync.breaker_until`; `/yoq crm` clears it), `push_404`, alert once per 6 h | «kalit yoki manba notoʻgʻri» |
| 400 on a feed (`bad_cursor` / `bad_since`) | `feed_contract` code `bad_request`, run `failed`, no retry — an agent bug (a decoded or hand-built cursor, a `since` without `Z`) | «ERP feed shakli oʻzgardi (bad_request) — dasturchi kerak» |
| Secret re-minted in the ERP | next call 404 → breaker; after the new key is pasted the next feed call sees a new `salt_id` → full pull; pushes refused `stale` until it completes | alert from the 404 |
| ERP answers 200 but dropped (capped/suppressed) | learned at the next outcomes read; `dropped_suppressed` → local phone-tail row + `suppression_stale_hit` (cleared by the next full pull); `dropped_capped` → retry next month | digest |
| «Boshqa» fallback door answers 200 to a landing that threw | invisible until the switch-over; then the `year` walk finds no row and rule 5 re-pushes once through `agent` | `push_unconfirmed` while it is open |
| Feed returns a PII key | `FeedShapeError`, run `failed`, problem `feed_contract` (code `shape`) | alert (contract breach) |
| Full pull shrinks > 10 % | no delete; `suppression_shrunk` | alert; runbook 06 §4.6 |
| A lost-reason **dictionary** label renamed, the setting unchanged | `saveLostReason` UPDATEs the label in place and never rewrites `leads.lost_reason` (the text chosen at the move): old leads keep their spelling, stay in the suppression feed and keep `no_contact=true` while the setting lists it (A.10) — but the outcomes feed compares their text with the CURRENT labels, so their `lost_reason` comes back `null` at the next read (the `year` walk or a later touch). The agent keeps such a row's stored label — a lost row whose `lost_reason` goes from a label to `null` with `stage_kind` still `lost` and `closed_at` unchanged is read as unchanged (02 §2.11), so no 180-/365-day cooldown is retracted and precision does not shift. NEW «never again» losses under the renamed label are not in the setting: they never enter the suppression feed and arrive with `no_contact=false` (their `lost_reason` is still sent — it is a dictionary label) | the owner doc says «deactivate + create, never rename» (A.14 step 2); the fix is to list the new spelling in `agent_no_contact_reasons` too (06 §4.6) |
| The **setting** `agent_no_contact_reasons` changed | old leads whose text is no longer listed leave the feed at the next full pull (`suppression_shrunk` if > 10 %) and `no_contact` turns false for them; a label ADDED brings its old leads in only through a full pull (A.7) | runbook 06 §4.6 «Feed shrank» |
| Owner changes a funnel stage's KIND | invisible to the delta and to `since` reads of older rows; the nightly full pull and the monthly `year` walk see it | nothing |
| Owner maps `kub` late | early arrivals route by the note's «N kub» (a band's `BAND_HAJM` words included), only a push with no number by rule B; a band is never printed as a quote either way (A.14) | none |
| Agent DB loses `push_log` (restore from backup) | outcome rows of the lost pushes are `foreign` and ignored; a re-push of such a company is a replay in the ERP | `push_unconfirmed` for the restored rows until the nightly read |

---

## Open questions

1. ~~§A.3 `company` pass-through~~ — resolved: shipped (A.2; `company`/`firma` → `leads.company` on a created lead, and still a note line and a pair).
2. ~~Inactive clients in the suppression set and the belt~~ — resolved: built that way (A.7, A.8).
3. Should a push that JOINS another manager's open lead be `dropped/suppressed` instead? Today it is `joined` + a note + «🔁 Qayta yozdi» to that lead's owner, and the outcomes feed shows that lead's stage (A.9); left as is.
4. Push window 09:30–17:30 Mon–Sat vs the playbook's 10:00–17:00 DM window — one number, which? (07 §5.2 q.6.)
5. ~~`OUTCOMES_WINDOW` as a setting~~ — resolved: a constant of one year in `agent-feed.ts`; the agent's 30-day precision needs far less.
6. `agent_no_contact_reasons` default «Qayta qilinmasin» only — add «Notoʻgʻri raqam» so a wrong number is fed back by phone, not only by the agent's ×2 rule? (07 §5.2 q.5.) Folded into question 12: the ×2 rule itself needs the dictionary label.
7. ~~Does /crm/tahlil read `contact_due_at`?~~ — resolved: it reads `contact_clock_at`/`contacted_at`; agent leads are in the late counts, and in the median only for contacts within 7 days (A.4). The ERP's own owner questions remain (its CHANGELOG): should agent leads count in a seller's late counts at all, and should they be booked on «Bugun qoʻngʻiroq» / the 08:00 digest (they are, up to 25 lines a day).
8. ~~Fixtures captured or hand-written?~~ — resolved: captured from `23fe1736` (07 §1.0).
9. 02 §2.10 rule 5's one re-push needs `push_log.superseded_at` (migration `0002_erp_contract`, T-10). Accept, or keep `unknown`/`sent` rows blocking for ever and leave every unconfirmed push to `/push tekshir`? (Recommended: accept — without it a 2xx row can never be retried, because `ck_push_log_sent` forbids `failed` on a 2xx.)
10. AGENT-FEED.md §0 step 4 says arrivals before the `kub` mapping route by rule B; the code reads a «N kub» from the note first (A.14 — VERIFIED 2026-10-24: `mapped.volumeM3 ?? textVolume(name, note)` in `landOnce`, and AGENT-FEED.md line 46 still says «kubsiz qoida (B)»). Since the agent now writes every band with a number (§B.1 `BAND_HAJM`), nearly every agent push routes by rule A before and after the mapping. Ask the ERP session to correct the sentence; nothing to ask the owner.
11. The orphan lead (A.2, A.13): the ERP chose not to claim the ledger row first (again at #1473, which would have closed the class for any input postgres refuses next). The agent lives with `joined` after a 503; is a «🔁 Qayta yozdi» about a never-announced lead acceptable to the owner, or should the ERP adopt the claim-first ledger later?

Owner decisions that came out of the T-07 verification (2026-10-24) — each is asked of Bekzod in
Uzbek in **01 §7** (the numbered question there, its options and the ⭐ recommendation); here only
what the ERP side would have to do:

12. **Lost-reason labels for precision** (01 §7 question 1). A lost reason leaves the ERP only as a
    dictionary or setting label (A.9); owner step 2 (A.14) adds only «Qayta qilinmasin», so
    `lost_not_importer`, `lost_wrong_number`, `lost_competitor`, `lost_small` and `lost_price` never
    fire and the ×2 wrong-number rule of 02 §2.8 has no input. ERP side: nothing to build — the owner
    adds dictionary rows on **CRM sozlamalari → Yoʻqotish sabablari** whose words the agent's
    `LOST_REASON_PATTERNS` match (02 §2.11). Recommended: add them in the same sitting as step 2.
13. **ERP clients reached by another phone, handle, domain or STIR** (01 §7 question 2). The
    suppression feed and the belt know only nine-digit phone tails (A.7, A.8, A.13 «the same company on
    a new phone is a new phone»); a client card or a «Qayta qilinmasin» lead found again through its
    Telegram username, Instagram, website or STIR — and every F-12 Telegram-only handoff card, which
    never reaches the ERP — passes. ERP-side option: a later round adds hashed handles/domains/STIR
    of client cards to the suppression feed (needs those fields on the card first, and a privacy
    review — the same enumeration caveat as A.13). Recommended for v1: the agent-side half only.
14. **Measured volume and first shipment** (01 §7 question 3). No route exposes receipts, unclaimed
    markings, cargo volumes or shipments; `stage_kind='won'` is `winLead` (a client card and a deal),
    not cargo. ERP-side option: a later outcomes field such as the first receipt's date and m³ for the
    lead's client (never a price), or a separate unclaimed-markings feed. Recommended: `won` as the v1
    proxy, the ERP round after the pilot.
15. **The reminder during the «Boshqa» fallback** (01 §7 question 4). Only the `agent` door is
    reminder-exempt (A.4); fallback pushes are reminded and count in people's per-phone cap.
    Recommended: open the PR, merge and deploy `agent-feed` before the pilot (A.14), so the fallback
    is never used; the ERP side has nothing new to build.
16. **«Quotes sent / contacted»** (01 §7 question 5). The outcomes feed carries only the lead's
    CURRENT stage and never a price (A.9), so a lead that passed «Narx aytildi» and moved on is
    invisible. ERP-side option: a later outcomes field «when a price was first written on the lead»
    (a timestamp, no amount). Recommended for v1: count from stage-name snapshots the owner names.
17. **«Contacted within 1 business day» under-counts** (01 §7 question 6). `contacted_at` sees only
    what the ERP can see (A.4, A.13: a personal Telegram account whose chat sits undecided on the
    tray is invisible), and an outcomes row carries no arrival time (the agent uses
    `push_log.sent_at`). ERP side: nothing to build; the remedy is the sellers' habit (01 §7).

## Acceptance criteria

- fullprompt `agent-feed` (head `d138b3bc`: built and twice reviewed at `23fe1736`, the third review's fixes `bfb5625b`, the fourth's `021a2ca5`; T-07): `pnpm typecheck && pnpm lint && pnpm test` green on a fresh `gsr_ci` in CI's order (met, by the build session's report: 675 files / 7636 tests after the third review, full vitest 676 files / 7642 tests after the fourth — re-checked by the PR's CI); an arrival carrying an emoji at a cut, a lone surrogate, a NUL (in any field or the `external_id`), a body nested too deep or an `external_id` too long for the ledger's index lands whole with 200 and one note, its retry a replay (met, pinned through the real route; the long id also on the Google door's landing); a `lead_field_map` key saved before the deploy still matches (met); no migration (met); `GET /api/agent/suppression` and `/outcomes` answer 404 without a key or with `X-ERP-Key` only, 400 `bad_cursor`/`bad_since` on a bad cursor or `since`, 503 JSON when the database fails after the key was sent, 200 gzipped ≥ 1 KB with the documented bodies and `resume` (met; captured except the 503 — no capture has the ERP database down — which is read from the code and pinned by fullprompt `tests/unit/agent-feed-door.test.ts` and `agent-door-landing.test.ts`); an `agent` arrival on a client-book (active or inactive, card, person or won/attached lead, 7–8-digit card by suffix) or «Qayta qilinmasin» phone writes `dropped/suppressed`, creates nothing and notifies nobody (met); an `agent` lead has `contact_clock_at` set and `contact_due_at NULL`, is never claimed by `remindUntouched`, and gets `contacted_at` from `stampFirstContacts` for 90 days (met); the agent door answers 503 to a landing that threw and every other door keeps its 200 (met); `tiktok`/form arrivals behave exactly as before (control group, met); the setting is described in 4 locales (met); the seed adds exactly one row (met); `docs/AGENT-FEED.md` lets the Python side be built without reading TypeScript (met). **Still open: the PR, the merge, the deploy and the owner's five steps (A.14).**
- Agent: `make test` green with the fake ERP covering 200 / 404 / 503 / 503 committed (the retry a replay) / non-JSON 5xx (empty-body 500, proxy HTML) / read timeout with the first request still running / replay / joined / orphan-joined / client (fallback) / `throw200` (fallback) / badjson and both feeds; a 404 trips the breaker and alerts once; a 3xx is never followed and trips `erp_redirect`; a 503 is retried and never trips it; a read timeout is never resent inside the attempt and lands as `unknown`, promoted by the next outcomes read; a 2xx with an unreadable body lands as `unknown`, never `sent`; an unconfirmed `sent`/`unknown` row is re-pushed at most once (02 §2.10 rule 5); `salt_id` change forces a full pull before any push; a full pull never sends `since` or `cursor`, and only it deletes; `suppression_fresh()` refuses pushes at 26 h without a full pull or 3 h without a delta; a shrunk full pull deletes nothing; a full pull retires exactly the `manual_import` tails whose hash it carries; the four outcomes walks send the documented params and the stored cursor is the last non-null `resume`; outcome rows the agent never pushed are ignored; labels match the table for every row kind and the local suppression rows follow the labels both ways (retractions); precision/auto-push flips both ways at the thresholds and never on while `auto_push_forbidden`; the fallback (`other` + both headers + `utm_source` after the note) passes the same push tests and makes no feed call; no U+0000 or lone surrogate ever reaches a body, the landed note skips empty values, and every band routes by its low end; `make push-test` makes a new `external_id` every run; `tests/test_no_telegram_writes.py` stays green (nothing here sends to a prospect); no secret in repo or logs (`RedactBodies` covers `X-GSR-Key`/`X-ERP-Key` headers and the payload's `phone`).

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

**T-07 verification (2026-10-24)** — an adversarial check of this file against fullprompt `agent-feed` at `23fe1736` and the v2 capture; every item below re-read in the ERP code (or recomputed) before it was changed:

- `kub` is sent for a STATED volume only: a mapped `kub` is the lead's quoted volume (`inbound.ts`), the seller's «📦 N kub» and the won deal's quote (`winLead`), so `BAND_KUB` is replaced by `BAND_HAJM` — the band's words with a number in the HAJM section, which the ERP's `textVolume` routes (rule A) and never prints (A.2, A.9, A.14 incl. owner step 4, §B.1, §B.6).
- A.3 step 6: the client step needs **exactly one** active card (`known.length === 1`); a multi-code phone on the fallback lands as a new lead, and the XLSX tails are the only guard (§B.3).
- §B.6: the «label renamed» row split into the dictionary-label case (new losses unsuppressed, `no_contact=false`) and the setting case (old leads leave the feed).
- A.4: «earliest wins» only during the arrival's first 24 h; `stage`/`followup` (and `note`) count only from the responsible person, the «📞 Bogʻlandim» note from any presser; `call` from 2 minutes before; the outcomes `at` can move backwards in that window.
- A.3 / §B.4: landing order `no_contact` (no name AND < 9 digits, before the fence) → replay → `no_phone` → caps → belt; the fake's GET checks `cursor` before `since`; `clamp_limit` copies `clampLimit` exactly (floored first; JS `Number`).
- A.2 / §B.1: caps are rolling 24 h and the source cap counts every arrival of the source; `RESERVED_BODY_KEYS` extended to `fromPlain`'s whole `known` set plus `user_column_data`, `IDENTITY_KEYS` added; `landed_note()` uses JavaScript's `\s` written out (Python's class differs on U+001C–U+001F, U+0085, U+FEFF).
- §B.1: a 5xx is retried because a re-send is fenced, not because a 5xx proves nothing landed (a proxy 502/504 can follow a recorded arrival); a new 3xx row (`http_3xx`, never followed, breaker `erp_redirect`); the fallback's 200-on-throw may leave an orphan the re-push joins (fake mode `throw200_orphan`); `make push-test` skips exactly 04 §7 item 13 and gate 1. §B.2 feeds map 3xx the same way.
- §B.3 Routing: an unknown `/ariza?manba=` lands under «Sayt»; `other` gets bot `ad_<unknown>` links and hand-posted forms.
- A.0: `7bea3051` is `origin/main` (PR #129's merge; its head `24607e8c`); the vitest/e2e counts are the build session's report, not a repo record. A.12 red proofs 16 + 21 + 16 (DECISIONS #1458, #1466, #1472). A.6/A.7 tail rules: CR, tabs, runs of 16–120 digits. §B.2 step 4: `aefbf278` removed the 998-run chunks from the feed (fragments only from the belt), and no pull predates it. A.7/A.9 examples copied from `suppression_full.json` / `outcomes_since.json`. §B.2 labels note: a free-text reason equal to a setting label comes back verbatim. Acceptance: the 503 is pinned by unit tests, not captured.
- Previous editor's open items now checked: production `7bea3051` cuts by UTF-16 units and answers 200 to a thrown landing (resolved, §B.1 `text_len`); `landOnce` routes by `mapped.volumeM3 ?? textVolume(name, note)` (Q10 VERIFIED, still an ERP doc fix); the «📞 Bogʻlandim» note maps to `note` from any presser (A.4). Open questions 12–17 added (owner decisions asked in 01 §7).

**T-07 third review (2026-10-24)** — fullprompt `agent-feed` moved to `a4f9f433` (`bfb5625b` code, `a4f9f433` docs; DECISIONS #1473–#1475). The 11 mismatches the re-verification left were each re-read in the code at that head (`platform/text/clip.ts`, `inbound.ts`, `inbound-webhook.ts`, `field-map.ts`, `inbound-notify.ts`, `service.ts` `setFollowUp`/`moveLead`/`updateLead`, `first-contact.ts`, `agent-feed.ts`, the leads-in route, `docs/AGENT-FEED.md`) or recomputed in Node (`clampLimit`, `textVolume`), then corrected:

- Part A describes `a4f9f433`: header; A.0 (12 commits, 41 files +4061/−63, three review rounds, 7636 tests, red proofs 16 + 21 + 16 + 12; the fixtures captured at `23fe1736` stay valid — no captured value reaches a cut); A.1 (`storableText`/`clipUnits`/`storableJson`, `storedRef`, the normalised id, AGENT-FEED.md 425 lines, the integration test 1373 lines, `field-map.integration.test.ts`); A.12 (the third review's tests and its 12 red proofs).
- Every ERP cut is UTF-16 units with a whole-emoji back-off, not code points (A.2 table and landed note, B.1 `LANDED_NOTE_MAX`, `text_len`, `name`, the field-order comment, B.4); only the Telegram preview (`PUSH_PREVIEW`) counts code points.
- A NUL becomes U+FFFD like a lone surrogate, everywhere, the `ref` and the `external_id` included (the outcomes feed returns such an id with U+FFFD); a body too deep to serialise is `{"_truncated":true}` (A.2; the fake normalises the id before its replay check, B.4). `well_formed` also drops U+0000, because production throws on it after (or at) `createLead` (B.1, B.3 Door row, a payload test).
- The no-header database failure is `500` with an empty body, not Next's HTML page (A.2 answer table); fake mode `html500` renamed `nonjson5xx` (an empty-body 500, or a proxy's HTML 502), with B.1's mapping row, B.6 and the acceptance criteria.
- #1475's four cases — a committed ledger row whose answer is lost, a retry that makes a second lead or is capped, a repeating cause adding a note per POST, the per-module-copy chain with no bound on its wait — in A.2, A.13, B.1 (503 «normally» means no ledger row), B.6 (two new rows) and a new fake mode `503_committed`.
- A.4: a «📞 Bogʻlandim» press by the responsible person first clears the landing's today booking, an earlier `followup` evidence row, so it reads `followup`; `note` only when the presser is not responsible or the date was already moved; a ✏️-form stage move also reads `followup`, a board move `stage`.
- A.7: `tailsSql`'s chunk conditions as written — a part made entirely of `998…` numbers → each, else a length that is a multiple of 9 → each 9-digit chunk, else (a 20-digit part, any part over 120) the last nine only.
- B.4 `clamp_limit`: `js_number` is ECMAScript `StringToNumber` (JS whitespace incl. U+FEFF, signed decimals and `Infinity`, unsigned `0x`/`0o`/`0b`), with the vectors recomputed in Node.
- B.1 `landed_note`: a value JS `trim()` leaves empty makes no line (`fromPlain`'s `str()`); `js_trim` written out.
- B.1 `BAND_HAJM`: `textVolume` reads the number right before the first «kub», so «taxminan 1–3 kub» routed as 3 and «3–10 kub» as 10; the words now put the low end there («taxminan 1 kubdan 3 kubgacha» → 1, «taxminan 3 kubdan 10 kubgacha» → 3; container 28 and truck 80 unchanged), with `TEXT_VOLUME_RE` and a routing test; the note template and A.14 follow.

**T-07 fourth review (2026-10-24)** — fullprompt `agent-feed` moved to `d138b3bc` (`021a2ca5` code, `d138b3bc` docs; DECISIONS #1476–#1477). Each fact re-read at that head (`inbound.ts` `record`/`oversizeIdSpelling`/`externalIdSpellings`/`inboundLanded`, `platform/text/clip.ts`, `agent-feed.ts`'s `LIKE 'cs:%'`, `tests/unit/intake-id.test.ts`, the integration test's two new cases, `docs/AGENT-FEED.md` §1 §5, DECISIONS) and the v2 capture re-checked (longest `external_id` 46 bytes, no string over 2,048 bytes):

- Header, A.0 (14 commits, 42 files +4370/−79, AGENT-FEED.md 443 lines, the fourth review's gates 676 files / 7642 tests by the build session's report, red proofs 16 + 21 + 16 + 12 + 5, a new bullet stating the fix for the agent: an `external_id` the ledger's unique index refuses for its size — only one over 2,048 UTF-8 bytes can be; postgres takes about 2,680 incompressible bytes — is stored as `sha256:` + hex SHA-256, lands, replays, and never appears in the outcomes feed; the agent's 46-byte ids are not affected, so the push, reconcile and outcomes logic are unchanged); A.1 (`inbound.ts`, `clip.ts`, docs and tests rows; integration test 1445 lines, `intake-id.test.ts`).
- A.2: two `external_id`s that differ only by NUL vs U+FFFD or by which lone surrogate are one id, and `ref` keys that differ only that way collapse to one key keeping the last value (the agent's payload tests already keep both out, 07 §1.6); the `external_id` paragraph gains the 46 bytes, the `cs:` filter and the hashed spelling; the replay-fence paragraph names #1476 and the one known input-driven repeating cause — a body of tens of MB (about 40 MB of JSON, measured in postgres), 503 on every retry plus a lenta note each, out of the agent's reach. A.13 (iii) and the B.6 half-way row follow.
- A.12: `intake-id.test.ts`, the long-id integration cases and the control, 5 red proofs (#1477). B.4: the fake does not model the `sha256:` spelling (the agent's ids are 46 bytes); the fixtures stay valid at `d138b3bc`. Acceptance criteria restated at the new head.

**Wave 1 as built (2026-10-24)** — the ten verify:final mismatches, each re-read in fullprompt `agent-feed` at `d138b3bc` (read-only), plus the wave-1 build's push-test note:

- A.3 step 8 and B.1: `textVolume` matches `kub|куб|m3|м3|m³|м³` (flags `iu`) and the FIRST match alone decides through `parseMeasure` (a first «0 kub» or a number ≥ 100000 is no volume; a later match is never read); the routing test applies that range.
- A.4: only a CREATED lead is booked for today (`createLead`); a joined push keeps the lead's date, so a «📞 Bogʻlandim» by the responsible person reads `followup` on a created lead, and on a joined one `followup` only when its date was today or earlier, else `note`.
- A.5: the 404 row split — `Not found` text/plain from the two agent routes and `/api/leads/in/[source]`, Next's own HTML 404 for a path no route handles; both trip `push_404`, and `text/plain` is required only of the former.
- A.7, A.13, B.3: a `phones` value that is not a JSON array reads as no phones on `agent-feed` (fail-open — never suppressed, a push on it is a new lead); production `7bea3051` throws on it instead (Door row); tails are derived per element, never from the joined list; day-1 count in 07 §5.3.
- A.9, B.6: `saveLostReason` renames a label in place, so old leads' `lost_reason` comes back `null` at the next read; the agent keeps the stored label (02 §2.11).
- A.11, A.13, A.14 step 1, B.3 Door row: the doors fold is drawn only for `admin.settings.manage`; there is no rotate button — a re-mint is «Oʻchirish» then «Yoqish va kalit yaratish» and kills the old key for every sender, so an existing «Boshqa» key is copied, not re-minted (owner text in Uzbek).
- A.14 step 4 and B.1: `seenKeys()` lists only keys arrivals carried in the last 30 days, so `make push-test` now gives every optional key a synthetic value (`kub: "1"` included) and a dropped key is added by hand through the «Savolni qo'lda qo'shish» row (decided here, without the owner); the zero-uuid `_post` exemption the T-04 guard carries is the pre-T-07 design and T-28 deletes it.
- B.2: a feed 404 — and a proxy's 401/403 — is `push_404`; any other 4xx is `feed_contract` `bad_request`; 3xx as a push's. B.3 Suppression row: the XLSX «Phones» column from an unfiltered screen with no saved view, ≤ 5,000 rows, inactive cards included. B.4 codec comments: an empty value means absent, the cursor's time survives a round trip (`T24:00:00Z` and `:60` refused), the `since` offset itself in range (`+24:00`, `+12:60`, `+1260` refused).
