# 05 — ERP integration (fullprompt branch `agent-feed` + agent push/feeds)

Scope: both sides of the ERP contract named in the shared context. Part A is the change set for
`fullprompt` (inspected read-only at `/home/user/bekzodqodirov/fullprompt`, HEAD `24607e8`, branch
`claude/gsr-logistics-wms-phase1-o8h4en` — origin's HEAD; there is **no `main`**, so `agent-feed`
branches from that branch). Part B is `clientscrapper/services/crm_push.py` and `services/erp_feeds.py`.
Tables referenced on the agent side (`push_log`, `outcomes`, `suppression`, `lead_candidates`,
`sources.config`) are defined in `02-data-model.md` §2.8–2.11; this file does not restate their DDL.

## 0. Memo audit — `docs/erp-ozgarishlar-memo.md` against fullprompt

The memo was written against LogisticsERP. Every assumption was re-checked in fullprompt; the
differences below are binding.

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

## A. fullprompt change set — branch `agent-feed`

### A.1 File map

| # | File | Change | LOC |
|---|---|---|---|
| 1 | `src/modules/wms/crm/inbound.ts` | `'agent'` in `INBOUND_SOURCE_KEYS` + `AGENT_SOURCE_KEY`/`isAgentSource`; suppression belt; `company` pass-through | ~30 |
| 2 | `src/modules/wms/crm/inbound-webhook.ts` | `fromPlain` reads `company`/`firma` into `InboundFields.company` | ~6 |
| 3 | `src/modules/wms/crm/first-contact.ts` | `REMINDER_EXEMPT_SOURCES`, `isReminderExempt()` | ~8 |
| 4 | `src/modules/wms/crm/inbound-notify.ts` | `startContactClock(intakeId, now, remind)`; `announceArrival` passes `!isReminderExempt(a.sourceKey)` | ~6 |
| 5 | `src/modules/wms/crm/agent-feed.ts` | NEW: hash, salt, cursor codec, reason list, `suppressionPage`, `isSuppressedTail`, `outcomesPage` | ~170 |
| 6 | `src/app/api/agent/suppression/route.ts`, `src/app/api/agent/outcomes/route.ts` | NEW GET routes | ~50 each |
| 7 | `src/modules/platform/settings/service.ts`, `src/modules/platform/settings/reason-list.ts` (new) | `agent_no_contact_reasons` default + validator; the pure `;`-list parser both sides import | ~25 |
| 8 | `messages/{ru,uz,zh-CN,en}.json` | `settings.descriptions.agent_no_contact_reasons` | 4 lines |
| 9 | `scripts/seed.ts` | KEYED row | 1 |
| 10 | `src/app/(protected)/crm/(pages)/kelganlar/page.tsx` + `doors.tsx` | optional: no `/ariza` and bot links for the agent door | ~6 |
| 11 | `docs/AGENT-FEED.md` (new), `docs/ADS.md` §1 §5.3, `CHANGELOG.md` (Uzbek entry) | docs | — |
| 12 | `tests/unit/agent-feed.test.ts`, `tests/unit/inbound-source-keys.test.ts`, `tests/integration/agent-feed.integration.test.ts` | tests | ~300 |

No migration. `platform` imports nothing from `wms` (routes and `agent-feed.ts` are on the app/wms side;
`settings`, `http/json`, `db/client` are platform → wms, the allowed direction).

### A.2 `inbound.ts`

```ts
export const INBOUND_SOURCE_KEYS = [
  'meta', 'instagram', 'facebook', 'telegram', 'tiktok', 'google', 'sayt',
  /**
   * A lead a MACHINE found (clientScrapper), not one a person sent. The only
   * key whose arrivals are also read back (/api/agent/*), whose phone is
   * checked against the client book and the «Qayta qilinmasin» leads before
   * it may become a lead, and whose owner reminder is off (docs/AGENT-FEED.md).
   */
  'agent',
  'other',
] as const;

/** One home for «is this the agent» — the belt, the clock and the doors screen all ask it. */
export const AGENT_SOURCE_KEY = 'agent';
export const isAgentSource = (key: string | null | undefined) => key === AGENT_SOURCE_KEY;
```

`InboundArrival` gains `company?: string | null`. In `landInboundLead`, after the caps block and before
step (0):

```ts
  // The agent's belt. The agent holds the suppression feed, so reaching this
  // line means its copy was stale: the arrival is recorded as `suppressed`
  // (the outcomes feed tells it to pull again) and nobody is told — a client's
  // manager must not get «a machine found your client on OLX». Adverts and the
  // public form keep today's behaviour: a client's question still lands on
  // the client's card.
  if (isAgentSource(sourceKey) && phone && (await isSuppressedTail(phone, await noContactReasons()))) {
    await record('dropped', { reason: 'suppressed' });
    return { outcome: 'dropped', reason: 'suppressed' };
  }
```

`createLead({ …, company: trim(arrival.company, 160) ?? undefined })` — only if `leadSchema` accepts
`company` (see §0). The route passes `company: fields.company`.

### A.3 `inbound-webhook.ts`

`InboundFields.company: string | null`; in `fromPlain` add `'company', 'firma'` to `known` and
`company: str(pick(body, 'company')) ?? str(pick(body, 'firma'))`. Google shape: `company: null`.
Consequence: `firma` no longer appears as a pair, so it needs no mapping; the lead card's own
`company` column holds it.

### A.4 Reminder exemption — `first-contact.ts` + `inbound-notify.ts`

```ts
// first-contact.ts
/**
 * Sources whose arrivals are MEASURED but never REMINDED (the owner, 2026-10-23:
 * the agent may push 25 leads a day and «⏰ tegilmagan» would be 25 lines). The
 * clock still starts, so `stampFirstContacts` still writes contacted_at — the
 * outcomes feed and /crm/tahlil read that, not the reminder.
 */
export const REMINDER_EXEMPT_SOURCES: ReadonlySet<string> = new Set([AGENT_SOURCE_KEY]);
export const isReminderExempt = (sourceKey: string | null | undefined) =>
  REMINDER_EXEMPT_SOURCES.has(sourceKey ?? '');
```

```ts
// inbound-notify.ts
async function startContactClock(intakeId: string, now: Date, remind: boolean): Promise<void> {
  const minutes = remind ? Number(await getSetting('inbound_contact_minutes')) || 0 : 0;
  const clock = contactClockFrom(now);
  const due = contactDueAt(clock, minutes);          // null when minutes = 0 → never claimed
  …unchanged UPDATE…
}
// announceArrival:
    if (!live) await startContactClock(a.intakeId, now, !isReminderExempt(a.sourceKey));
```

`claimUntouched` requires `contact_due_at IS NOT NULL`, so no reminder and no
`contact_alerted_at`; `announceReassigned` still works (it reads `contact_clock_at`). UNVERIFIED —
check that the /crm/tahlil measurement (`crm/analytics.ts`) reads `contact_clock_at`/`contacted_at`
and not `contact_due_at`; if it reads `due`, agent rows fall out of the «in time» ratio, which is
acceptable but must be stated in `docs/AGENT-FEED.md`.

### A.5 `agent-feed.ts` (new, `src/modules/wms/crm/`)

```ts
import { createHash, createHmac } from 'node:crypto';
import { sql } from 'drizzle-orm';
import { db } from '../../platform/db/client';
import { getSetting } from '../../platform/settings/service';

export const HASH_RULE = 'hmac-sha256(secret, last9digits) hex[0:20]';
export const FEED_LIMIT_DEFAULT = 1000;
export const FEED_LIMIT_MAX = 5000;
export const OUTCOMES_WINDOW = sql`interval '1 year'`;

/** The feed's phone identity: never the digits, only this. */
export function hashTail(secret: string, tail9: string): string {
  return createHmac('sha256', secret).update(tail9).digest('hex').slice(0, 20);
}
/** Changes when the secret is re-minted — the agent then re-pulls fully. */
export function saltIdFor(secret: string): string {
  return createHash('sha256').update(secret).digest('hex').slice(0, 8);
}

// The parser lives on the PLATFORM side (settings/reason-list.ts) because the
// settings validator needs it and platform never imports wms:
//   export function parseReasonList(raw: unknown): string[]   // `;`-split, trim, lower, dedupe, drop < 2 chars; never throws
//   export const reasonListValid = (raw: string) => parseReasonList(raw).length > 0 && parseReasonList(raw).every((s) => s.length <= 80);
import { parseReasonList } from '../../platform/settings/reason-list';
export async function noContactReasons(): Promise<string[]> {
  return parseReasonList(await getSetting('agent_no_contact_reasons'));
}

export interface Cursor { at: string; id: string }   // ISO instant, uuid
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const encodeCursor = (c: Cursor) => Buffer.from(`${c.at}|${c.id}`, 'utf8').toString('base64url');
/** null = absent; 'invalid' = present and unreadable (the route answers 400). */
export function decodeCursor(raw: string | null): Cursor | null | 'invalid' {
  if (!raw) return null;
  const [at, id] = Buffer.from(raw, 'base64url').toString('utf8').split('|');
  if (!at || !id || Number.isNaN(Date.parse(at)) || !UUID.test(id)) return 'invalid';
  return { at: new Date(at).toISOString(), id };
}
export const clampLimit = (raw: string | null) =>
  Math.min(FEED_LIMIT_MAX, Math.max(1, Number(raw) || FEED_LIMIT_DEFAULT));

const labelList = (labels: string[]) => sql.join(labels.map((l) => sql`${l}`), sql`, `);
const lostWithListedReason = (labels: string[]) =>
  labels.length
    ? sql`JOIN lead_stages s ON s.id = l.stage_id AND s.kind = 'lost'
         WHERE lower(trim(l.lost_reason)) IN (${labelList(labels)})`
    : sql`JOIN lead_stages s ON s.id = l.stage_id AND s.kind = 'lost' WHERE false`;

export async function suppressionPage(o: {
  secret: string; since: string | null; cursor: Cursor | null; limit: number;
}): Promise<{ entries: { h: string; kind: 'client' | 'lost'; at: string }[]; next: string | null }> {
  const labels = await noContactReasons();
  const rows = await db.execute<{ tail: string; kind: 'client' | 'lost'; at: string; id: string }>(sql`
    WITH tails AS (
      -- Active AND inactive clients: a former client is still «bizning mijoz»
      -- (wider than activeClientsByPhone on purpose — stated in AGENT-FEED.md).
      SELECT right(regexp_replace(p.v, '[^0-9]', '', 'g'), 9) AS tail, 'client'::text AS kind,
             greatest(c.created_at, c.updated_at) AS at, c.id
        FROM clients c CROSS JOIN LATERAL jsonb_array_elements_text(c.phones) AS p(v)
      UNION ALL
      SELECT right(regexp_replace(coalesce(l.phone, ''), '[^0-9]', '', 'g'), 9), 'lost',
             coalesce(l.closed_at, l.updated_at), l.id
        FROM leads l ${lostWithListedReason(labels)}
    )
    SELECT tail, kind, at, id FROM tails
     WHERE length(tail) = 9
       AND (${o.since}::timestamptz IS NULL OR at > ${o.since}::timestamptz)
       AND (${o.cursor?.at ?? null}::timestamptz IS NULL
            OR (at, id) > (${o.cursor?.at ?? null}::timestamptz, ${o.cursor?.id ?? null}::uuid))
     ORDER BY at, id
     LIMIT ${o.limit + 1}`);
  const page = rows.slice(0, o.limit);
  const last = page.at(-1);
  return {
    // db.execute returns timestamps as TEXT (#925): normalise once.
    entries: page.map((r) => ({ h: hashTail(o.secret, r.tail), kind: r.kind, at: new Date(r.at).toISOString() })),
    next: rows.length > o.limit && last ? encodeCursor({ at: new Date(last.at).toISOString(), id: last.id }) : null,
  };
}

/** The landing belt's question, one statement. `phone` as typed; nine digits taken here. */
export async function isSuppressedTail(phone: string, labels: string[]): Promise<boolean> {
  const tail = phone.replace(/[^0-9]/g, '').slice(-9);
  if (tail.length < 9) return false;
  const rows = await db.execute<{ hit: boolean }>(sql`
    SELECT EXISTS (
             SELECT 1 FROM clients c
              WHERE EXISTS (SELECT 1 FROM jsonb_array_elements_text(c.phones) p(v)
                             WHERE right(regexp_replace(p.v, '[^0-9]', '', 'g'), 9) = ${tail}))
        OR EXISTS (
             SELECT 1 FROM leads l ${lostWithListedReason(labels)}
                AND right(regexp_replace(coalesce(l.phone, ''), '[^0-9]', '', 'g'), 9) = ${tail}) AS hit`);
  return rows[0]?.hit === true;
}

export interface OutcomeRow {
  external_id: string; intake_id: string; outcome: string; reason: string | null;
  lead_id: string | null; client_id: string | null;
  stage_kind: 'open' | 'won' | 'lost' | null; stage_name: string | null; lost_reason: string | null;
  closed_at: string | null; contacted_at: string | null; contact_kind: string | null;
  quoted_volume_m3: number | null; at: string;
}
export async function outcomesPage(o: { since: string | null; cursor: Cursor | null; limit: number })
  : Promise<{ rows: OutcomeRow[]; next: string | null }> {
  const rows = await db.execute<OutcomeRow & { quoted_volume_m3: string | null }>(sql`
    SELECT i.external_id, i.id AS intake_id, i.outcome, i.reason, i.lead_id,
           coalesce(l.client_id, i.client_id) AS client_id,
           s.kind AS stage_kind, s.name AS stage_name, l.lost_reason, l.closed_at,
           i.contacted_at, i.contact_kind, l.quoted_volume_m3,
           greatest(i.created_at, i.contacted_at, l.updated_at, l.closed_at) AS at
      FROM lead_intakes i
      LEFT JOIN leads l ON l.id = i.lead_id
      LEFT JOIN lead_stages s ON s.id = l.stage_id
     WHERE i.channel = 'webhook'
       AND i.external_id LIKE 'cs:%'
       -- The fallback months: pushes that went through the `other` door before
       -- this branch was deployed are the agent's too.
       AND (i.source_key = 'agent' OR (i.source_key = 'other' AND i.ref ->> 'utm_source' = 'clientscrapper'))
       AND i.created_at > now() - ${OUTCOMES_WINDOW}
       AND (${o.since}::timestamptz IS NULL OR greatest(i.created_at, i.contacted_at, l.updated_at, l.closed_at) > ${o.since}::timestamptz)
       AND (${o.cursor?.at ?? null}::timestamptz IS NULL
            OR (greatest(i.created_at, i.contacted_at, l.updated_at, l.closed_at), i.id)
               > (${o.cursor?.at ?? null}::timestamptz, ${o.cursor?.id ?? null}::uuid))
     ORDER BY at, i.id
     LIMIT ${o.limit + 1}`);
  const page = rows.slice(0, o.limit).map((r) => ({
    ...r,
    quoted_volume_m3: r.quoted_volume_m3 === null ? null : Number(r.quoted_volume_m3),   // numeric → string (#503)
    at: new Date(r.at).toISOString(),
    closed_at: r.closed_at && new Date(r.closed_at).toISOString(),
    contacted_at: r.contacted_at && new Date(r.contacted_at).toISOString(),
  }));
  const last = page.at(-1);
  return { rows: page, next: rows.length > o.limit && last ? encodeCursor({ at: last.at, id: last.intake_id }) : null };
}
```

Rules honoured: no transaction, so no pooled-helper trap (#714); ISO + `::timestamptz` (#156);
`sql.join` for the label list; `ORDER BY` for keyset (#524); one statement per page (#432). Privacy:
no `name`, `phone`, `note`, `owner_id`, `quoted_amount` leaves the server. Duplicates (two clients on
one phone) are not deduplicated — the agent upserts on `(kind, key_type, key)`. Deletions cannot be
expressed as a delta; the full nightly pull is the truth, the hourly delta an optimisation.
UNVERIFIED — (a) the `clients` table has **both** `created_at` and `updated_at`
(`greatest(c.created_at, c.updated_at)` depends on it; day-1 check: `\d clients` on `gsr_ci`, or grep
the `clients` table in fullprompt's schema — if `updated_at` is absent, use `created_at` alone and
say so in `AGENT-FEED.md`); (b) `clients.updated_at` moves on a phone edit (`updatedAt()` helper has
`$onUpdate`); if not, a phone added to an old client appears only in the full pull, which is acceptable.

### A.6 The two routes

`src/app/api/agent/suppression/route.ts` (outcomes identical bar the body):

```ts
import { sourceWebhookSecret, AGENT_SOURCE_KEY } from '@/modules/wms/crm/inbound';
import { secretMatches } from '@/modules/wms/crm/inbound-webhook';
import { clampLimit, decodeCursor, HASH_RULE, saltIdFor, suppressionPage } from '@/modules/wms/crm/agent-feed';
import { json } from '@/modules/platform/http/json';
import { logger } from '@/modules/platform/logger';

export const dynamic = 'force-dynamic';
const NO_STORE = { 'Cache-Control': 'private, no-store' };
const notFound = () => new Response('Not found', { status: 404 });

export async function GET(request: Request) {
  // The POST door's two rules, unchanged: 404 for a door that is off, 404 for a wrong key.
  const secret = await sourceWebhookSecret(AGENT_SOURCE_KEY);
  if (!secret) return notFound();
  if (!secretMatches(request.headers.get('x-gsr-key'), secret)) {
    logger.warn({ feed: 'suppression' }, '[agent-feed] bad key');
    return notFound();
  }
  const q = new URL(request.url).searchParams;
  const cursor = decodeCursor(q.get('cursor'));
  const sinceRaw = q.get('since');
  const since = sinceRaw ? (Number.isNaN(Date.parse(sinceRaw)) ? 'invalid' : new Date(sinceRaw).toISOString()) : null;
  if (cursor === 'invalid' || since === 'invalid') {
    return json(request, { error: cursor === 'invalid' ? 'bad_cursor' : 'bad_since' }, { status: 400, headers: NO_STORE });
  }
  try {
    const page = await suppressionPage({ secret, since, cursor, limit: clampLimit(q.get('limit')) });
    return json(request, {
      mode: since ? 'delta' : 'full', generated_at: new Date().toISOString(),
      salt_id: saltIdFor(secret), rule: HASH_RULE, entries: page.entries, next: page.next,
    }, { headers: NO_STORE });
  } catch (err) {
    // An authenticated machine may be told we are down; it backs off. Never a stack.
    logger.error({ err }, '[agent-feed] suppression failed');
    return json(request, { error: 'unavailable' }, { status: 503, headers: NO_STORE });
  }
}
```

Outcomes body: `{ generated_at, rows, next }`. Answer codes: 200, 304 (ETag from `json()`), 400
`bad_cursor|bad_since`, 404 (door off / wrong key / unknown path), 503 `unavailable`.
HEAD is served by Next as GET; harmless.

### A.7 Setting `agent_no_contact_reasons`

`settings/service.ts`:

```ts
  /**
   * Lost reasons that mean «never again» to the client-finding agent
   * (2026-10-23). `;`-separated labels, matched case-insensitively against
   * `leads.lost_reason` — the TEXT chosen at the move, so a renamed reason
   * keeps suppressing under its old spelling as long as it stays listed here.
   * Read by /api/agent/suppression and by the agent's landing belt.
   */
  agent_no_contact_reasons: 'Qayta qilinmasin',
…
export const SETTING_VALIDATORS = { …, agent_no_contact_reasons: reasonListValid };
```

`parseReasonList`/`reasonListValid` live in `src/modules/platform/settings/reason-list.ts` (pure, no
db): the validator is platform code and `agent-feed.ts` is wms code, and platform never imports wms.

`messages/*.json` → `settings.descriptions.agent_no_contact_reasons`:

| Locale | Text |
|---|---|
| ru | `Причины закрытия, после которых агент поиска клиентов больше никогда не предлагает этот номер (через «;», регистр не важен)` |
| uz | `Shu sabab bilan yopilgan lid raqamini mijoz topuvchi agent boshqa hech qachon taklif qilmaydi («;» bilan ajrating, katta-kichik harf farqsiz)` |
| zh-CN | `以这些原因关闭的线索号码，找客户代理永不再推荐（用「;」分隔，不区分大小写）` |
| en | `Lost reasons after which the client-finding agent never proposes that number again («;»-separated, case-insensitive)` |

### A.8 Seed and doors screen

`scripts/seed.ts` KEYED: `{ key: 'agent', name: 'Agent (qidiruv)', sortOrder: 90 },` between `sayt` and
`other`. Idempotent by the existing loop; the row appears on the next deploy's `migrate` service.

`kelganlar/page.tsx` (optional): `formUrl: isAgentSource(door.key) ? null : …`, `botUrl: … && !isAgentSource(door.key) ? … : null`;
`doors.tsx` `DoorView.formUrl: string | null` and `{door.formUrl && <Copyable …/>}` (same pattern as
`assignUrl`). Without it the agent door shows a working `/ariza?manba=agent` link that would
mis-attribute a human submission to the agent — cosmetic, but cheap.

### A.9 `docs/AGENT-FEED.md` (new) — contents

0. **Egasi uchun** (Uzbek): the five UI steps of §A.12, one screen each, with the exact menu path.
1. Door: `POST /api/leads/in/agent`, header `X-GSR-Key`, body keys and limits (`name` ≤ 120,
   `note` ≤ 1000 after extras, pairs ≤ 30 × 300 chars), `external_id = cs:<company_uuid>:<yyyymm>`,
   answer always `{ok:true}`, 404 semantics, caps 3/phone/day and 200/source/day, replay fence.
2. What the ERP does on landing for `agent` only: suppression belt → `dropped/suppressed`;
   contact clock measured, reminder off; routing by mapped `kub`.
3. Feeds: both URLs, params, bodies, codes, hash rule, `salt_id`, cadence (full nightly 02:30,
   delta hourly at :15, outcomes nightly 03:00, Tashkent — §B.2), «deletions only in full»,
   duplicates, 1-year outcomes window, the `other`+`utm_source=clientscrapper` clause.
4. Privacy stance: hashes not digits; what never leaves (name, phone, note, owner, money).
5. Honest limits: a 9-digit space is enumerable by the secret holder; re-minting the key rotates
   `salt_id`; a lead revived out of lost drops out of suppression (correct); tails < 9 digits never
   match; the same person on a new number is a new person.
6. Fallback before this branch is live: `other` door + `utm_source`; the outcomes clause that
   picks those rows up afterwards; **never re-push an `external_id` after switching source** (same
   `channel`, so it is a replay).

`docs/ADS.md`: §1 add `agent` to the list with «faqat mashina uchun — qo'lda ishlatmang»; §5.3 add
`external_id` to the example and a pointer to `docs/AGENT-FEED.md`. `CHANGELOG.md`: one Uzbek entry.

### A.10 Tests

**Unit — `tests/unit/agent-feed.test.ts`** (pure, no db):
`hashTail('k','901234567')` equals a fixed vector computed once with `openssl` and pasted;
`saltIdFor` is 8 hex and changes with the secret; `encodeCursor`/`decodeCursor` round-trip,
`'invalid'` for garbage, missing `|`, bad uuid, bad date; `parseReasonList(' Qayta qilinmasin ; NARX;;')`
→ `['qayta qilinmasin','narx']`; `reasonListValid('')` false; `isReminderExempt('agent')` true,
`'tiktok'` false; `clampLimit('0')` → 1000, `'99999'` → 5000, `'7'` → 7.

**Unit — `tests/unit/inbound-source-keys.test.ts`** (source-shape fence, comments stripped first,
#725): every key in `INBOUND_SOURCE_KEYS` appears as `key: '<k>'` inside the `KEYED` array of
`scripts/seed.ts` — the seed is the only thing that gives a key a row and `sourceIdForKey` falls
back to `other` silently without one. Also: `'agent'` is in `INBOUND_SOURCE_KEYS` and
`parseInboundPayload({ firma: 'X MChJ', name: 'A', phone: '9' }).company === 'X MChJ'` with no
`firma` pair left.

`tests/unit/locales.test.ts` covers the setting description automatically.

**Integration — `tests/integration/agent-feed.integration.test.ts`** (pattern of
`inbound-webhook.integration.test.ts`; `STAMP = Date.now()`, names prefixed `AG-${STAMP}`):

Fixtures in `beforeAll`: snapshot `agent_no_contact_reasons` and set it to `'Qayta qilinmasin;Test sabab'`
(#716); mint the agent secret with `setSourceWebhookSecret('agent', true, ctx)`; insert client
`{ clientCode: 'AGT…', name, phones: [P1] }` (active) and one inactive client with `[P6]`; a lead on a
lost stage with `lost_reason = 'qayta QILINMASIN'` and phone P2; a lost lead with reason `'Narx'`
phone P4; an open lead phone P3. Resolve the lost stage id by `kind = 'lost'` from the seed.

| Test | Assertion | Red proof (string edit, never `git stash`, #430) |
|---|---|---|
| full pull | `suppressionPage({since:null,cursor:null,limit:5000}).entries` contains `hashTail(secret,P1)`, `(P2)`, `(P6)`, not `(P3)`, not `(P4)`; kinds right | remove the `UNION ALL` branch → P2 missing |
| pagination | `limit:1` walks all fixture tails through `next` without repeats, ends `null` | drop `ORDER BY` → nondeterministic/duplicate |
| delta | `since = now` → none of the fixtures; `since = STAMP-1h` → all | drop the `since` predicate |
| label rename | setting `'boshqa'` → P2 absent; restore → present | — |
| belt | `landInboundLead({channel:'webhook', sourceKey:'agent', externalId:'cs:…', phone:P1})` → `dropped/suppressed`; ledger row exists with `reason='suppressed'`; no `leads` row, no `crm_activities` on the client, no `notifications` row | delete the belt lines → outcome `client` |
| belt is agent-only | same with `sourceKey:'tiktok'` → `client` (today's behaviour) | — |
| clock | agent arrival with fresh P5 → `created`; `lead_intakes.contact_clock_at IS NOT NULL AND contact_due_at IS NULL`; a `tiktok` arrival has `contact_due_at NOT NULL` | pass `true` unconditionally → due not null |
| no reminder | `claimUntouched(now + 2 days)` returns no row for the agent intake | — |
| contact stamp | add a `call` activity by the owner (or `CONTACT_NOTE`) then `stampFirstContacts(now, leadId)` → `contacted_at` set | skip the clock → stays null |
| outcomes | `outcomesPage` returns the agent rows with `external_id`, `outcome`, `stage_kind='open'`, `contacted_at`; after `moveLead` to lost with `'Qayta qilinmasin'` → `stage_kind='lost'`, `lost_reason`; never a `phone`/`name` key in the row | — |
| fallback rows | an `other` arrival with `ref: { utm_source: 'clientscrapper' }`, `externalId 'cs:…'` appears; one with `utm_source: 'x'` does not | drop the `other` clause |
| company | arrival with `company` → `leads.company` set (only if §A.3 lands) | — |

`afterAll`: restore the setting (#716); `setSourceWebhookSecret('agent', false, ctx)` (#183); delete
`notifications` by `payload->>'intakeId'`, `lead_intakes` by name prefix, `crm_activities`, `leads`,
fixture `clients` (inserted directly, never audited → deletable); `pgClient.end()`. Fixture lost
reasons go in `leads.lost_reason` TEXT only — **no `lost_reasons` dictionary row is created** (a
non-empty dictionary switches `reasonAllowed` to list-only for every later spec, #183).

Also run `tests/integration/inbound-webhook.integration.test.ts` «door directory» (loops all doors —
passes once the CI db is seeded) and `tests/unit/tx-pool.test.ts` (the belt calls `getSetting`
outside any transaction).

### A.11 PR checklist (fullprompt `CLAUDE.md`)

1. Branch `agent-feed` from `claude/gsr-logistics-wms-phase1-o8h4en`; commits end with
   `Co-Authored-By` and `Claude-Session` trailers; **no model identifier** in commit, PR, comment, doc.
2. No secret anywhere: the test mints and nulls its own; `docs/AGENT-FEED.md` shows `<kalit>`.
3. Commit the fix, then the red-proof series by string edit; never `git checkout`/`git stash`.
4. Local gate, each with its own marker: `pnpm typecheck && pnpm lint && pnpm test` (tests/ are typed
   only by typecheck, #1223/#591); `rm -rf .next/cache/eslint` if lint and build disagree.
5. Reproduce CI's order on a fresh `gsr_ci`: migrate → `db:seed` → `db:seed:demo` → vitest → `pnpm build`
   → Playwright without re-seeding (`fuser -k 3000/tcp` first; no `CI=1` locally).
6. Locale parity: 4 bundles; `tests/unit/locales.test.ts` green.
7. `grep -rhoE "https://[a-z.]+/" .next/static/` unchanged (no new browser dependency).
8. PR body (Uzbek summary for the owner + English detail) lists: no migration; the seed row; the
   setting; the five owner steps; what was NOT done (A.3 if skipped, doors cosmetics if skipped).

### A.12 Deploy notes

- `docs/UPDATE.md` ritual: verified non-zero backup (`docker compose exec -T backup /backup.sh`,
  list, copy off-box), then `docker compose build migrate` and `docker compose --profile https up -d --build`.
  The `migrate` service runs `pnpm db:migrate && pnpm db:seed` → the `agent` source row appears. No
  migration, so the end check is the build stamp at `/api/version`, not the migration count.
- Owner steps, in order (Uzbek in `AGENT-FEED.md` §0):
  1. **CRM → ⋯ → Kelgan arizalar → Reklama havolalari → «Agent (qidiruv)» → «Yoqish va kalit yaratish»**
     (`setSourceWebhookAction`, `admin.settings.manage`). Paste into the agent's `.env` as
     `CRM_AGENT_KEY`. Re-minting kills the old key at once and changes `salt_id`.
  2. **CRM → ⋯ → Sozlamalar → Yo'qotish sabablari**: add «Qayta qilinmasin». **Warn first**: if the
     list is empty today, adding the first label makes free-text reasons illegal for every lost move
     (`reasonAllowed`); add the reasons the team uses today in the same sitting. Never rename a
     listed label later — deactivate and create, or add the new spelling to the setting too.
  3. **Boshqaruv → Arizalar taqsimoti**: rule A `manba = agent`, `min_m3 = 1`, members = cargo
     managers; rule B `manba = agent`, no window, members = fallback pool (or leave to the rota).
     Order A above B, both above any catch-all.
  4. Agent sends one test push (`make push-test`, §B.3). **Forma savollari → Kelgan savollar**: map
     `kub → kub (m³)`; optionally `stir`, `imports`, `score` → custom lead fields (create them under
     the lead's custom fields first); `telegram`, `instagram`, `website`, `address`, `evidence`,
     `utm_source` → «izohda qolsin». Arrivals before the `kub` mapping route unwindowed.
  5. **Boshqaruv → Sozlamalar**: check `agent_no_contact_reasons` matches the label(s) of step 2.

---

## B. Agent side

### B.1 `services/crm_push.py`

```python
ERP_KEY_HEADERS = ("X-GSR-Key", "X-ERP-Key")          # same value; fullprompt reads the first
PUSH_PATH = "/api/leads/in/{source}"                   # source = settings.crm_push_source ('agent' | 'other')
UTM_SOURCE = "clientscrapper"
NOTE_MAX = 700          # ERP appends 'key: value' lines for extras, then trims at 1000
FIRST_LINE_MAX = 160    # the manager's Telegram push shows the first 160 chars only
QUOTE_MAX = 140
OPENER_MAX = 220
NAME_MAX = 120
PAIR_VALUE_MAX = 300    # ERP capturePairs
PUSH_DAYS = range(0, 6)                                # Mon–Sat
PUSH_FROM, PUSH_TO = time(9, 30), time(17, 30)         # Asia/Tashkent (02 §2.10 item 2)
ATTEMPT_DELAYS = {2: timedelta(minutes=5), 3: timedelta(minutes=30)}   # between push_log attempts (the HTTP tries INSIDE one attempt are with_backoff's, below)
BAND_KUB = {"1_3": "1", "3_10": "3", "container": "28", "truck": "80"} # lower bound when no stated m³: managers read `kub`, so the low end of 04 §4.3 (20ft ≈ 28–33, fura ≈ 80–90)

class PushPayload(BaseModel, frozen=True):
    external_id: str        # cs:<company_uuid>:<yyyymm>, Tashkent month at claim time
    name: str               # company display_name (2..120); never the person alone
    phone: str              # E.164 from contacts.phone; the ERP keeps the last 9
    note: str               # render_note(); ≤ NOTE_MAX
    kub: str | None         # plain number string ("2", "1", "28", "80" — stated m³ or BAND_KUB); omitted when unknown/<1
    firma: str | None       # display_name + legal_form → leads.company (A.3) or a pair
    stir: str | None
    telegram: str | None    # '@company' or the decision maker's handle only with legal_basis
    instagram: str | None
    website: str | None     # domain
    address: str | None     # ≤ 300
    imports: str | None     # 'mato, aksessuar' (goods, ≤ 6, joined)
    score: str              # '74'
    evidence: str           # '<url> — "<quote ≤ 100>"' ≤ 300
    utm_source: str = UTM_SOURCE

def external_id_for(company_id: UUID, now: datetime) -> str
def build_payload(c: CandidateView) -> PushPayload          # pure; raises NoPhone
def render_note(c: CandidateView) -> str                     # pure; sections below
def canonical_sha256(p: PushPayload) -> str                  # sorted keys, no spaces, None dropped
def push_window_open(now: datetime) -> bool                  # tz-aware Tashkent
async def push_one(session, candidate_id: UUID, *, now: datetime, erp: ErpClient) -> PushResult
async def crm_push_job(session_factory, erp, *, now) -> JobReport     # worker: every 60 s inside the window
```

`CandidateView` is a read-only dataclass assembled by `services/candidates.load_view()` from
`lead_candidates` + `companies` + the one `contacts` row + top signals (with `evidence.url`,
`published_at`, `signals.quote`) + `score_breakdown` + the opener draft stored by 04 (`lead_reviews`
or `score_breakdown.opener`; UNVERIFIED — 04 decides where the draft lives).

**Uzbek note template** (sections in this order; a section with nothing to say is omitted; every
line is plain text; apostrophes ʻ/ʼ; no price figures; no phone or handle beyond what the ERP body
already carries):

```
NEGA TOPILDI: {source_title_uz} · {published_dd.mm.yyyy} · «{quote ≤ 140}»          ← line 1, ≤ 160 chars
{evidence_url}
NIMA OLIB KELADI: {goods joined ', '} · Xitoy: {ha|ehtimol} ({origin_hint})
HAJM: {stated «2 kub» | band label: 1–3 kub / 3–10 kub / fura / konteyner} · {per_month if known}
KOMPANIYA: {legal_name} · STIR {stir|—} · {city} · {registry_status_uz} · {sector}
  Masʼul: {full_name} ({role})                                                      ← only tier=person with legal_basis
NIMA DEYISH: {opener ≤ 220, from 04 or the segment template in docs/sotuv-playbook.md §3}
BALL: {score}/100 (hajm {v}, Xitoy {o}, firma {s}, yangilik {r}, aloqa {c}, tasdiq {k}{, minus … if any})
```

Length rules enforced in `render_note` (tests pin them): line 1 ≤ 160 and never cut mid-word;
total ≤ 700 after sections are dropped in reverse priority order (BALL detail → KOMPANIYA second line
→ NIMA DEYISH shortened to 120 → HAJM per-month); the URL is never cut (if a URL exceeds 200 chars it
is replaced by `evidence #<id>`); quote is a verbatim substring of `evidence.excerpt` (02 §2.6).

**Gate and transport** (02 §2.10 items 1–7 are the law for the gate; the attempt scheme and the
outcome mapping below are owned HERE — 02 §2.10 rule 4 and 07 §1.6 point to this table):

```python
TRANSIENT = (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError)
class ErpHttpError(Exception): status: int
class ErpBadBody(Exception): status: int      # a 2xx whose body is not {"ok": true} — never retried, the lead may have landed
def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, TRANSIENT) or (isinstance(exc, ErpHttpError) and exc.status >= 500)

async def _post(erp, source, payload) -> int:
    r = await erp.client.post(PUSH_PATH.format(source=source), json=payload.model_dump(exclude_none=True),
                              headers={h: erp.key for h in ERP_KEY_HEADERS}, timeout=20.0)
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
`push_log` attempt* (one claim row, ≤ 15 s, delays 1 s → 2 s → 4 s capped at 8 s). A further
**attempt** (`attempt+1`, identical `payload_sha256`) may be claimed only after `ATTEMPT_DELAYS[n+1]`
— 5 min before attempt 2, 30 min before attempt 3 — so the worst case is 9 POSTs, and after attempt 3
the candidate returns to `review`. Re-sending inside an attempt after a `ReadTimeout` is safe
**because of the ERP replay fence** (same `channel` + `external_id` → `dropped/replay`, no second
lead): a timeout on try 1 followed by `200 {ok:true}` on try 2 lands as **attempt 1 `sent`** with ONE
intake in the ERP; the test `test_push_timeout_resend_is_replay` pins it against the fake ERP.
Outcome mapping (the `push_log` CHECK of 02 §2.10 is `(status='sent' ⇒ 2xx) AND (2xx ⇒ status IN
('sent','unknown'))`, so every 2xx row below is one of those two):

| Result of the three tries | `push_log` | candidate | side effect |
|---|---|---|---|
| 2xx with body `{"ok":true}` | `sent`, `http_status` | `pushed`, `pushed_at`, `external_id` | — |
| 2xx with any other body (HTML, empty, bad JSON) | `unknown`, `http_status`, `error='http_2xx_badbody'` | stays `approved`, blocked by `ux_push_log_delivered` | not retried (the lead may have landed); the nightly outcomes read promotes `unknown` → `sent` when the `external_id` appears, else `push_unconfirmed` after 48 h |
| 404 | `failed`, `error='http_404'` | stays `approved` | breaker: `sources.erp_sync.breaker_until = now+6h`, `breaker_reason='push_404'` (`/yoq crm` clears it by hand, 06 §1.2; it also expires); health problem `push_404`; owner alert once per 6 h — `reminder_log(kind='alert:push_404')`, recovery `recovered:push_404` (06 §4.1): «ERP kaliti yoki manba notoʻgʻri: /api/leads/in/{source} 404 qaytardi. Kelgan arizalar → Reklama havolalari → Agent → kalitni tekshiring.» |
| other 4xx | `failed`, `error='http_4xx'` | → `review`, note «ERP rad etdi (HTTP n)» | owner alert |
| 5xx / transport after 3 tries | `failed`, `error='http_5xx'|'timeout'|'conn'` | stays `approved`; next attempt allowed after `ATTEMPT_DELAYS[n+1]`; after attempt 3 → `review`, note «CRM javob bermadi» | health problem `push_failing` when 3 consecutive candidates fail |
| `sending` row older than 10 min (crash) | `unknown` (reconcile job) | stays `approved` but blocked by `ux_push_log_delivered` | nightly outcomes promote `unknown` → `sent` when the `external_id` appears; `push_unconfirmed` after 48 h |

Pre-flight refusals (no HTTP, no `push_log` row, result codes): `window` (outside
Mon–Sat 09:30–17:30 Tashkent), `cap` (`count(sent|unknown today) ≥ PUSH_DAILY_CAP`), `disabled`
(`CRM_PUSH_ENABLED=false`), `breaker` (`sources.erp_sync.breaker_until > now`), `suppressed` (gate
re-check inside the `FOR UPDATE` transaction), `stale` (`suppression_stale`: `suppression_fresh()`
of §B.2 is false — feed `salt_id` ≠ stored, last successful full pull > 26 h or last delta > 3 h —
while the feed is configured), `no_phone`, `phone_recent` (same tail `sent|unknown` < 24 h),
`company_recent` (a `sent|unknown` row for this company whose outcome is `open` or absent and younger
than 90 d — the card's «90 kunda yuborilmagan» line, 02 §2.10 rule 2). A `cap` or `window` refusal is
silent (the job runs again); every other refusal writes a `lead_reviews` note.

`make push-test` (`tools/push_test.py`): builds a visibly synthetic payload
(`name='TEST agent — oʻchiring'`, phone from `TEST_PHONE` env, `external_id='cs:00000000-0000-0000-0000-000000000000:<yyyymm>'`),
posts once, prints status; used for step 4 of §A.12.

### B.2 `services/erp_feeds.py`

```python
class ErpClient:                       # one httpx.AsyncClient; base_url=CRM_BASE_URL; key=CRM_AGENT_KEY
    async def get(self, path, params) -> httpx.Response     # headers = both key names; timeout 30 s
    async def post_lead(...)                                # used by crm_push

def hash_tail(secret: str, tail9: str) -> str      # hmac_sha256(secret, tail9).hexdigest()[:20]
def salt_id(secret: str) -> str                    # sha256(secret).hexdigest()[:8]
def parse_entry(d: dict) -> FeedEntry              # h ~ ^[0-9a-f]{20}$, kind in {client,lost}, at ISO → else FeedShapeError
def parse_outcome(d: dict) -> FeedRow              # the 14 fields; unknown keys ignored; a 'phone'/'name'/'note' key → FeedShapeError (contract breach)
async def fetch_pages(erp, path, *, since: datetime | None, limit: int = 1000, max_pages: int = 500) -> AsyncIterator[dict]
async def sync_suppression(session, erp, *, mode: Literal["full", "delta"], now) -> SyncReport
async def sync_outcomes(session, erp, *, now) -> SyncReport
async def source_precision(session, source_key: str, now) -> Precision
async def source_precision_job(session_factory, now) -> None     # weekly; flips sources.auto_push both ways — never ON while config.auto_push_forbidden

FULL_MAX_AGE_H = 26      # one nightly full pull (30 2 * * *) plus slack
DELTA_MAX_AGE_H = 3      # one hourly delta plus slack
def suppression_fresh(cfg: SuppressionConfig | None, *, key: str, now: datetime) -> bool
    # THE home of feed freshness — 02 §2.8, 02 §2.10 rule 2, 03 §2.2, 04's gate, 06 §4.2 and /holat all
    # call it, nobody restates the numbers. Feed configured ⇒ last_full_at ≥ now − FULL_MAX_AGE_H
    # AND last_delta_at ≥ now − DELTA_MAX_AGE_H AND cfg.salt_id == salt_id(key). False → health
    # problem `suppression_stale` (warning, critical after 6 h; texts say «26 soat») and every push
    # is refused `stale` (§B.1). Feed not configured ⇒ True (the XLSX fallback carries the gate, §B.3).
```

**Suppression sync** (`erp_sync` collector calls it; cron: full `30 2 * * *` — after the Yandex
night run 02:10, before outcomes 03:00 — delta `15 * * * *`, Tashkent, delta skipped while a full
pull runs):

1. `GET /api/agent/suppression?limit=1000` (+`since=<last_delta_at - 10 min>` for delta) via
   `with_backoff(attempts=4, base=2, cap=60, retry_on=TRANSIENT|5xx|503-json)`. 404 → problem
   `push_404` (same misconfiguration as a push 404), owner alert, stop. 400 → bug: problem
   `feed_contract` (code `bad_request`), stop.
2. First page: assert `rule == "hmac-sha256(secret, last9digits) hex[0:20]"` (else
   `feed_contract`, code `rule_changed`, stop) and compare `salt_id` with
   `sources.erp_sync.config.suppression.salt_id`: different → if `mode == "delta"` abort and schedule
   a **full** pull now; if `full`, continue and store the new `salt_id` only after the pull completed.
3. Each entry → upsert `suppression (kind, 'phone_hash', h, salt_id, source='erp_feed', erp_at=at,
   last_seen_at=run_started)` on conflict `(kind, key_type, key)` update `last_seen_at, erp_at, salt_id`.
   Follow `next` until `null` (`max_pages` guard → `feed_contract`, code `runaway`).
4. Full pull only: `DELETE FROM suppression WHERE source='erp_feed' AND last_seen_at < run_started`
   **iff** the pull completed and `count ≥ 0.9 × previous count`; otherwise problem
   `suppression_shrunk`, nothing deleted. On a `salt_id` change, rows carrying the old `salt_id` are
   deleted by this same rule (they were not re-seen).
5. Write `config.suppression = {salt_id, last_full_at|last_delta_at, count}`; heartbeat
   `collector:erp_sync`. Full pull only, after step 4: retire the XLSX fallback **per row** — for
   every `suppression` row with `source='manual_import'`, `key_type='phone_tail'`, compute
   `hash_tail(settings.crm_agent_key, tail)` and delete the row when a `source='erp_feed'`,
   `key_type='phone_hash'` row with that hash exists (the feed now carries the same tail under the
   current key's `salt_id`). A tail the feed does **not** carry stays (fail closed), is counted by
   `/holat` («qoʻlda kiritilgan: n») and is removed only by hand — whether a client the XLSX listed
   and the ERP no longer does may be contacted is the owner's call, not the agent's.

Health keys of this sync: the four feed failure codes — `bad_request`, `rule_changed`, `runaway` and
`shape` (`FeedShapeError`, §B.6) — are ONE health key `feed_contract` (06 §4.2 «ERP feed shakli
oʻzgardi ({code})»); the code travels in the problem's detail, never as a key of its own.
`suppression_stale`, `suppression_shrunk` and `suppression_stale_hit` are keys of their own (06 §4.2).

**The gate's hash**: `is_suppressed()` computes `phone_hash = hash_tail(settings.crm_agent_key, tail)`
with the *current* key; after a re-mint the stored rows carry the old `salt_id` and cannot match →
that is exactly why `suppression_fresh()` is false and `suppression_stale` blocks pushes until the
full pull ran (02 §2.8).

**Outcomes sync** (cron `0 3 * * *` Tashkent):

1. `GET /api/agent/outcomes?limit=1000&cursor=<config.outcomes.cursor>`; **no `since`** once a cursor
   exists (keyset is complete; `since` is only for the first run: `now - 400 d`).
2. Upsert `outcomes` by `external_id` with `raw`, `erp_at`, `read_at`; `first_read_at` kept.
3. `label, precision_class = services.outcomes.derive_label(row, now)` (one home; defined in 02 §2.11)
   → write on change with `label_changed_at` and run the side effects of 02 §2.11 in the same
   transaction. `push_log` rows `unknown` with this `external_id` → `sent`.
4. Save `config.outcomes = {cursor: last next or the last row's cursor, last_at}`.

**Outcomes → labels** (the table form of `derive_label`, for reviewers):

| Feed row | label | precision | side effect |
|---|---|---|---|
| `outcome='client'` | `landed_client` | neutral | `companies.state='client'`; `suppression(client, phone_tail, source='outcomes')` written locally, now |
| `outcome='dropped'`, `reason='capped'` | `dropped_capped` | neutral | candidate eligible next month |
| `outcome='dropped'`, `reason='suppressed'` | `dropped_suppressed` | neutral | problem `suppression_stale_hit` → full pull tonight |
| `outcome='dropped'`, other | `dropped_other` | neutral | owner digest line |
| `stage_kind='won'` or `client_id` set | `won` | positive | `companies.state='client'`; `suppression(client, phone_tail, source='outcomes')` written **locally, now** (tail = the phone the agent pushed) — the nightly feed brings the same tail as a hash a day later; 02 §2.11 points here |
| `stage_kind='lost'`, `lost_reason ∈ agent_no_contact_reasons` | `lost_no_contact` | negative | `suppression(lost)` for the company's other keys (tg, instagram, domain, stir) — the feed brings the phone |
| lost, reason ~ `narx|цена|qimmat|дорог` | `lost_price` | neutral | `suppression(cooldown 180 d, company_id)` |
| lost, reason ~ `import qilmaydi|не импорт|xitoy emas` | `lost_not_importer` | negative | `suppression(cooldown 365 d, company_id)` |
| lost, reason ~ `hajm|kichik|мал` | `lost_small` | negative | — |
| lost, reason ~ `raqam|номер|noto.g.ri` | `lost_wrong_number` | negative | `suppression(bounced)` ×2 rule |
| lost, reason ~ `kargo|cargo|konkurent|конкурент` | `lost_competitor` | negative | `companies.is_competitor=true` |
| lost, other | `lost_other` | neutral | — |
| `outcome='joined'`, open, no `contacted_at` | `landed_joined` | neutral | — |
| open, `contacted_at` set | `contacted_open` | positive after 14 d, else pending | — |
| open, no contact, `at` ≥ 30 d | `stale_open` | neutral | — |
| open, no contact, younger | `open_untouched` | pending | — |

**Source precision** (`source_precision`): over `outcomes` joined to `lead_candidates.primary_source_key`
with `first_read_at ≥ now − 30 d`: `positives = count(precision_class='positive')`,
`negatives = count('negative')`; `precision_30d = positives / (positives + negatives)` or NULL when the
denominator is 0; `pushes_30d = count(push_log sent|unknown, sent_at ≥ now − 30 d)` for that source.
`auto_push = pushes_30d ≥ 15 AND precision_30d ≥ 0.60`, flipped both ways weekly (Sunday 04:00) —
**except that the job never turns `auto_push` on while the owner has forbidden it** (`/manbalar` →
`src:<key>:a` sets `config.auto_push_forbidden=true`, 06 §1.2): the UPDATE reads
`SET auto_push = (pushes_30d >= 15 AND precision_30d >= 0.60) AND NOT coalesce(config->>'auto_push_forbidden','false')::bool`.
A flip is one digest line. `/holat` prints the three numbers per source.

### B.3 Fallback while the `agent` door is not deployed

| Item | Fallback | Switch-over |
|---|---|---|
| Door | `CRM_PUSH_SOURCE=other`; `CRM_AGENT_KEY` = the **«Boshqa»** door's secret (owner mints it on the same screen); body always carries `utm_source=clientscrapper`; `push_log.endpoint='/api/leads/in/other'`, `source_key_sent='other'` | set `CRM_PUSH_SOURCE=agent`, replace `CRM_AGENT_KEY`, restart `worker`. Candidates already `pushed` are **not** re-pushed (same `channel`, replay). |
| Routing | the `other` door is also where unknown `?manba=` lands; a rule `manba=other` + keyword `clientscrapper` routes agent arrivals to the cargo managers. UNVERIFIED — that the ERP's keyword rule (`matchRoute`) searches the lead **note** (where the `utm_source: clientscrapper` line ends up) and not only name/phone; day-1 check with the rule in place: `make push-test` through the `other` door, confirm the test lead lands on a cargo manager. If the keyword does not read the note, route `manba=other` by members alone until switch-over and tell the owner that unknown `?manba=` arrivals share that pool. | delete that rule after switch-over |
| Suppression | `make import-clients FILE=clients.xlsx` (ERP **Mijozlar → XLSX**); `services/client_import.py` stores `suppression(kind='client', key_type='phone_tail', source='manual_import')` — clear tails, because the future agent secret is unknown and hashes would not match. Lost leads invisible; `/holat` says «yoʻqotilgan lidlar roʻyxati yoʻq». Weekly re-import. | a full pull retires each `manual_import` row whose tail's hash it carries (§B.2 step 5); tails it does not carry stay until the owner removes them |
| Outcomes | none; labels stay `pending`, precision NULL, `auto_push` impossible | first outcomes run reads the fallback months through the `other`+`utm_source` clause |
| Reminder | the ERP's 15-min reminder **fires** for `other` arrivals — tell the owner; keep `PUSH_DAILY_CAP ≤ 10` during the fallback | — |

### B.4 Fake ERP for tests — `tests/fake_erp.py`

`FakeErp(secret, default_mode='ok')` → `httpx.MockTransport(fake.handler)`; the client under test is
built with `ErpClient(base_url='http://erp.test', key=…, transport=…)`.

- State: `posted: list[tuple[source, headers, body]]`, `intakes: dict[external_id, row]`,
  `suppression: list[entry]`, `outcomes: list[row]`, `salt`, `secret`, `modes: dict[external_id, mode]`.
- `POST /api/leads/in/{source}`: unknown source or `x-gsr-key` ≠ secret → `404 'Not found'`
  (`x-erp-key` alone is **not** accepted — mirrors fullprompt). Mode per `external_id`:
  `ok` → 200 `{"ok":true}`, writes intake `created`; `joined` → 200, intake `joined` with a prior
  `lead_id`; `client` → 200, intake `client` + `client_id`; `capped`/`suppressed` → 200, intake
  `dropped` with reason; `404` → 404; `500` → 500 `Internal`; `timeout` → records the intake **then**
  raises `httpx.ReadTimeout`, once per `external_id` (the landed-but-unanswered case: the in-attempt
  resend is a replay → 200, so the push lands as attempt 1 `sent` and the fake holds ONE intake);
  `badjson` → 200 `'<html>'` (→ `push_log.unknown`, `http_2xx_badbody`, §B.1). A second POST with a
  known `external_id` → 200, no change (replay).
- `GET /api/agent/suppression|outcomes`: key check as above; `limit` clamp; `since` filter;
  keyset `cursor` identical to §A.5's codec; `salt_id = sha256(secret)[:8]`; `rotate_secret(new)`
  changes it; mode `feed_500` / `feed_timeout` / `feed_rule_changed` per path.

Tests (pytest, async): `test_payload_sections_and_limits` (order, 160/700, URL never cut, no
person name without `legal_basis`, `kub` rules, `utm_source` present, `exclude_none`),
`test_external_id_month_tashkent`, `test_canonical_sha_stable`, `test_push_200_marks_pushed`,
`test_push_404_trips_breaker_and_alerts_once`, `test_push_500_three_tries_then_failed_then_review_after_attempt_3`,
`test_push_timeout_resend_is_replay` (attempt 1 `sent`, two intakes never created), `test_push_2xx_badbody_is_unknown`
(never `sent`; promoted by outcomes), `test_push_unknown_promoted_by_outcomes`,
`test_push_refused_outside_window_and_over_cap`, `test_push_refused_when_suppressed_or_stale`,
`test_push_fallback_other_source_sends_both_headers`, `test_feed_salt_change_forces_full_pull`,
`test_feed_full_pull_deletes_unseen_only_when_not_shrunk`, `test_feed_rule_mismatch_stops`,
`test_feed_404_is_push_404`, `test_outcomes_labels_table` (parametrised over the table above),
`test_outcomes_joined_and_client_rows`, `test_outcomes_contract_breach_on_phone_key`,
`test_precision_and_autopush_flip` (both ways at the thresholds; never on while `auto_push_forbidden`),
`test_manual_import_retired_after_full_pull` (import a tail, then a fake full pull carrying that tail's
hash → the `manual_import` row is gone; a tail the pull does not carry stays),
`test_suppression_fresh` (26 h / 3 h / `salt_id` edges; feed unconfigured → true). Recorded fixtures:
`tests/fixtures/erp/{suppression_full.json,suppression_delta.json,outcomes.json}` are **captured
from the real `agent-feed` branch** on a seeded `gsr_ci` on day 1 and committed (synthetic data).

### B.5 Sequences

```
PUSH (happy)                                  PUSH (404)
owner ✅ → candidate approved                 push_one → claim(sending,1) → POST → 404
crm_push_job (window open, cap ok)            push_log failed/http_404; breaker 6 h; problem push_404
 └ push_one: FOR UPDATE; gate ok              owner ← «ERP kaliti yoki manba notoʻgʻri …»
   claim push_log(sending, attempt 1) COMMIT  next ticks: refused 'breaker' until the key is pasted and /yoq crm clears it (or 6 h pass)
   POST /api/leads/in/agent  ← X-GSR-Key      /holat shows push_404 until a 2xx
   ERP: belt → client? → open? → create → route(kub) → notify manager (clock, no due)
   200 {ok:true} → sent; candidate pushed
nightly: outcomes row external_id → label

SUPPRESSION (full, salt changed)              OUTCOMES (nightly)
02:30 GET ?limit=1000 → salt_id ≠ stored      03:00 GET ?cursor=… → rows
  continue (full); upsert pages with new salt   upsert outcomes; derive_label; side effects
  next=null → count ≥ 0.9×prev → DELETE unseen  unknown push_log → sent when external_id seen
  store salt_id; clear suppression_stale        save cursor; precision numbers refresh weekly
```

### B.6 Failure modes → behaviour

| Failure | Agent behaviour | Owner sees |
|---|---|---|
| ERP down (conn/timeout) on push | 3 tries in ≤ 15 s; `failed`; attempt 2 after 5 min, 3 after 30 min; then `review` | `/holat` `push_failing`; digest line |
| ERP down on feeds | `with_backoff` 4 tries; collector run `failed`; `suppression_fresh()` false (no full pull ≤ 26 h or no delta ≤ 3 h) → gate `suppression_stale` → pushes stop | alert «ERP roʻyxati 26 soatdan beri yangilanmadi — push toʻxtatildi» (06 §4.2 renders the real `{age}`) |
| 404 on any call | breaker 6 h (`sources.erp_sync.breaker_until`; `/yoq crm` clears it), `push_404`, alert once per 6 h | «kalit yoki manba notoʻgʻri» |
| Secret re-minted in the ERP | next feed call: `salt_id` differs → full pull; pushes 404 until `.env` updated | alert from the 404 |
| ERP answers 200 but dropped (capped/suppressed) | learned next night; `dropped_suppressed` → full pull tonight; `dropped_capped` → retry next month | digest |
| Timeout after the ERP landed the lead | `unknown`; resend is a replay; outcomes promote to `sent` | nothing |
| Feed returns a `phone`/`name` key | `FeedShapeError`, run `failed`, problem `feed_contract` (code `shape`; same key as `bad_request`/`rule_changed`/`runaway`, §B.2) | alert (contract breach) |
| Full pull shrinks > 10 % | no delete; `suppression_shrunk` | alert |
| Label list renamed in the ERP without the setting | lost leads drop out of the feed | caught by the integration test's rename case; owner doc says «deactivate + create» |
| Owner maps `kub` late | early arrivals route unwindowed (rule B) | none; stated in step 4 |
| Agent DB loses `push_log` (restore from backup) | unique index gone for lost rows → a re-push is a replay in the ERP; outcomes rebuild `push_log.unknown→sent` | `push_unconfirmed` until the nightly read |

---

## Open questions

1. §A.3 `company` pass-through: ship it on `agent-feed`, or send `firma` as a pair and map it? (Recommended: ship — six lines, the card already has the column.)
2. Suppression feed includes **inactive** clients and the belt matches them too; the advert path only matches active ones. Keep the wider rule for the agent? (Recommended: yes.)
3. Should `dropped/suppressed` also be written when the agent pushes a phone that joins an **open** lead of another manager? Today it is `joined` + note, which is useful; left as is.
4. Push window 09:30–17:30 Mon–Sat vs the playbook's 10:00–17:00 DM window — one number, which?
5. `OUTCOMES_WINDOW` 1 year: enough for the precision maths; make it a setting?
6. `agent_no_contact_reasons` default «Qayta qilinmasin» only — add «Notoʻgʻri raqam» so a wrong number is also fed back by phone, not only by the agent's ×2 rule?
7. Does `/crm/tahlil` read `contact_due_at`? (UNVERIFIED, §A.4.) If yes, decide whether agent rows should be in the «in time» ratio.
8. Should the fake-ERP fixtures be captured from the branch on day 1 (recommended) or hand-written?

## Acceptance criteria

- fullprompt `agent-feed`: `pnpm typecheck && pnpm lint && pnpm test` green locally and in CI on a fresh db; no migration; `GET /api/agent/suppression` and `/outcomes` answer 404 without a key, 400 on a bad cursor, 200 gzipped ≥ 1 KB with the documented bodies; an `agent` arrival on a client-book or «Qayta qilinmasin» phone writes `dropped/suppressed`, creates nothing and notifies nobody; an `agent` lead has `contact_clock_at` set and `contact_due_at NULL`, is never claimed by `remindUntouched`, and gets `contacted_at` from `stampFirstContacts`; `tiktok`/form arrivals behave exactly as before (control group); every new setting key is described in 4 locales; the seed adds exactly one row; `docs/AGENT-FEED.md` lets the Python side be built without reading TypeScript.
- Agent: `make test` green with the fake ERP covering 200/404/500/timeout/replay/joined/client/badjson and both feeds; a 404 trips the breaker and alerts once; a timeout re-send never creates a second lead and lands as attempt 1 `sent`; a 2xx with an unreadable body lands as `unknown`, never `sent`; `salt_id` change forces a full pull before any push; `suppression_fresh()` refuses pushes at 26 h without a full pull or 3 h without a delta; a shrunk full pull deletes nothing; a full pull retires exactly the `manual_import` tails whose hash it carries; outcomes labels match the table for every row kind (`won` writes the local `suppression(client)` row); precision/auto-push flips both ways at the thresholds and never on while `auto_push_forbidden`; the fallback (`other` + both headers + `utm_source`) passes the same push tests; `tests/test_no_telegram_writes.py` stays green (nothing here sends to a prospect); no secret in repo or logs (`RedactBodies` covers `X-GSR-Key`/`X-ERP-Key` headers and the payload's `phone`).

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
