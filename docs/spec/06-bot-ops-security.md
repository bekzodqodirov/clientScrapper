# 06 — Owner bot, manager handoff, Telegram account, deployment, operations, security

Scope: everything between the pipeline (02–05) and the people who run it. The shared context
(budget, threshold, env names, ERP contract, legal rules) is assumed, not restated. Table and
column names are 02's; push and feed semantics are 05's; collector schedules are 03's.
Owner-facing strings are Uzbek Latin with ʻ/ʼ; all identifiers are English.

## 1. Owner bot (`clientscrapper/bot/`)

### 1.1 Skeleton

Copied from MIYA `miya/bot/` (`main.py`, `formatting.py`, `keyboards.py`, `notices.py`,
`handlers.py` → split into `handlers/{review,sources,chats,status,admin,manager}.py`,
`replies.py` → `texts.py`). Kept verbatim: aiogram 3 long polling with
`drop_pending_updates=False`; `get_me` retry ×5; HTML default parse mode with the plain-text
retry in `_send`; `@dp.errors()` that answers «⚠️ Xatolik yuz berdi — birozdan keyin qaytadan
urinib koʻring.» so a tapped button never spins; the 5-minute watchdog (`heartbeats.bot`,
`worker_silent`, in-memory `db_down` memo); `set_my_commands` from `COMMAND_MENU`. Startup
refuses to run without `ASSISTANT_BOT_TOKEN` and `OWNER_TELEGRAM_ID` (never an unrestricted bot).

Routers, in registration order:

| Router | Filter | Reaches |
|---|---|---|
| `owner_router` | `F.from_user.id == settings.owner_telegram_id` on messages and callbacks | every command and button |
| `manager_router` | `F.from_user.id in settings.manager_telegram_ids` | `/start`, `/yordam`, `/qidir`, `/tel`, buttons `mh:*` on the manager's own cards only (`lead_reviews.by_telegram_id` must match) |
| `reject_router` | everything else | answers nothing; logs `rejected_update` with the user id at most once per id per hour; `my_chat_member` → `leave_chat` when the bot is added to any group or channel |

The bot never speaks to anyone who is not the owner or an allow-listed manager, and never
posts into a group.

### 1.2 Commands

| Command | Answer | Writes |
|---|---|---|
| `/lidlar` | Sends the next ≤ `REVIEW_BURST_MAX` pending review cards now (ignores `REVIEW_HOURS`: the owner asked) and «Navbatda yana N ta». `/lidlar holat` prints counts per `lead_candidates.state` and the oldest `review` age | `lead_reviews.state=asked`, `card_message_id`, `card_hash`, `expires_at` |
| `/manbalar` | One line per `sources` row, paged by 8: `{✅/⏸/⛔} {title_uz} · oxirgi {age} · bugun {pages}/{budget} sahifa · ${cost} · aniqlik {p}% ({pushes_30d}) · {avto/qoʻlda}`; buttons per row `src:<key>:t` (on/off) and `src:<key>:i` (last 5 `collector_runs`, breaker, paused reason) | `sources.enabled`; `src:<key>:a` sets `config.auto_push_forbidden=true` (owner can only forbid auto-push, never grant it — F-11 computes it) |
| `/chats` | MIYA `chats_keyboard` with one toggle: `{✅/❌} {👥/📢} {title} · {hit_7d} signal / {seen_7d} xabar`; `/chats qoʻsh @username` enqueues `chat_monitors(status='queued', priority=0)` (joined by `userbot/joiner.py`, still ≤ 3/day) | `chat_monitors.monitor_enabled`, `decided_by='owner'` |
| `/holat` | §4.1 layout: processes, db, disk, backup, Anthropic, ERP (last push, last full/delta pull, breaker), per-source lines, queues, pushes today/cap, cost today/month, open problems each with its remedy line | — |
| `/xarajat` | Today and month by `usage_log.provider` and `operation`, refusals count from `costs`, top 5 companies by spend (ids and names only), `% of DAILY_COST_CAP_USD` / `MONTHLY_COST_CAP_USD` | — |
| `/toxtat [sabab]` | Global pause at once, no confirmation (a safety action): collectors fetch nothing, no paid call, no push, no card, no join. The userbot keeps its connection and keeps storing prefilter hits (no egress, no cost). Reply: «⏸ Toʻxtatildi. Davom ettirish: /yoq» | `runtime_flags('paused')` §6 |
| `/yoq` | Confirm card «Davom ettiraymi?» `ps:y` / `ps:n`; `/yoq crm` clears `sources.erp_sync.breaker_until` after confirm `ps:c` | `runtime_flags`, `sources` |
| `/import` | Reply `/import` to an XLSX/CSV, or send it with that caption: `client_import.plan` → «{n} ta telefon: {new} yangi, {same} bor, {bad} notoʻgʻri» → `im:<plan_id>:y|n`; plan kept in memory 10 min (restart = send again) | `suppression(kind='client', key_type='phone_tail', source='manual_import')` |
| `/unut <STIR \| +998… \| @user \| domen \| uuid \| nom>` | `purge.plan_company` → plan card (§5.4) → `pg:<purge_log_id>:y|n` → counts | `purge_log`, deletions, `suppression(legal|dnc)` |
| `/qidir <soʻz>` | Resolves `+998…`/tail-9, `@username`, STIR, domain, or trigram name match (≤ 5) to a company card: company-tier fields, state, best score, last push + outcome label, and `🚫 Bostirilgan: {kind_uz} ({source}, {dd.mm.yyyy})` when `is_suppressed()` hits. Read-only, creates nothing | — |
| `/yordam` | The command list | — |

Every handler has a test in `tests/test_bot_commands.py` (F-13) driven by MIYA's fake-bot
pattern (`tests/test_jarvis_cards.py::_owner_message`, `_Callback`).

### 1.3 Review card

Rendered by `bot/cards.render_review(view: CandidateView) -> Card(text, keyboard, hash)`; pure,
golden-tested. `view` is 05's `CandidateView`. HTML-escaped; quote verbatim from
`evidence.excerpt`, ≤ 140 chars; whole card ≤ 3,500 chars.

```
🆕 Nomzod · {score}/100 · {segment_label_uz}
🏢 {display_name}{ ({legal_form})} · {city}{ · STIR {stir}}{ · {registry_status_uz}}
📦 {goods joined ', '} · Xitoy: {ha|ehtimol}
📐 Hajm: {«2 kub» | 1–3 kub | 3–10 kub | fura | konteyner} — {oʻzi yozgan | eʼlon | vakansiya | katalog | koʻrgazma}
📞 {phone} ({mobil|shahar|qisqa}){ · 👤 {full_name}, {role}}{ · @{telegram_username}}{ · IG {instagram}}{ · {website_domain}}
🔎 «{quote}» — {source_title_uz}, {dd.mm.yyyy}
🔗 {evidence_url}
🧮 hajm {v} · Xitoy {o} · firma {s} · yangilik {r} · aloqa {c} · tasdiq {k}{ · minus {negatives}}
✔️ mijoz emas · DNC yoʻq · 30 kunda yuborilmagan
```

The last line is literal: a card exists only because every gate passed; the gate is re-run
inside the push transaction (02 §2.10), never trusted from the card.

Keyboard (row 1, row 2, row 3):

```
[✅ CRMʼga yubor] [✖️ Yoʻq]
[🔀 Birlashtir] [🔒 DNC] [⏭ Keyinroq]
[⋯ Boshqa]  →  [🙍 B2C] [🚚 Kargo/raqobatchi] [✏️ Telefon] [◀️ Orqaga]
```

When the candidate has no usable phone, row 1 is `[👤 Menejerga] [✖️ Yoʻq]` (§1.7). After a
tap the card is edited in place (same `card_message_id`), keyboard removed, one footer line
appended:

| Tap | `lead_reviews.answer` | Candidate | Footer |
|---|---|---|---|
| ✅ | `push` | `approved(approved_by='owner:<id>')` | «✅ Navbatda — CRMʼga yuboriladi» → worker edits to «✅ CRMʼga yuborildi {hh:mm}» on `push_log.sent`, or «⏳ Ertaga 09:30 da yuboriladi» outside `PUSH_WINDOW`, or «⚠️ CRM javob bermadi — qayta soʻrayman» after attempt 3 |
| ✖️ | `decline` | `rejected` | «✖️ Rad etildi» |
| 🔀 | `merge` | `excluded(merged)` after target chosen | card shows ≤ 5 targets from `companies.suggest_merge_targets()` (the resolver's own function) as `mg:<review_id>:<uuid>` + `◀️`; footer «🔀 Birlashtirildi → {target}»; a refused merge (two STIRs) answers an alert and keeps the card |
| 🔒 | `dnc` | `suppressed` | writes `suppression(dnc, phone_tail)` plus `tg_username`/`instagram`/`domain` rows the company has; «🔒 DNC — boshqa koʻrsatilmaydi» |
| ⏭ | `later` | stays `review`; `expires_at += 24 h` | «⏭ Keyinroq — ertaga qayta soʻrayman» |
| 🙍 | `not_b2b` | `excluded(b2c)` + `suppression(b2c, company_id)` | «🙍 B2C — chiqarildi» |
| 🚚 | `competitor` | `excluded(competitor)`, `companies.is_competitor=true` | «🚚 Kargo — chiqarildi; kanali manba sifatida qoladi» |
| ✏️ | `edit_phone` | stays `review`; the owner's next message within 5 min must be a phone; digits replaced on the same contact row (typo fix, `legal_basis`/`source_url` unchanged); card re-rendered | «✏️ Telefon tuzatildi» |
| 👤 | `handoff` | `hold`, `hold_until=+30 d`, manager card sent (§1.7) | «👤 Menejerga yuborildi — {manager}» |

Expiry: `review_cards_sweep` (every 20 min) marks `asked` rows past `expires_at` as `expired`,
candidate → `hold` 7 d, re-asked at most twice (02 §2.9). A tap on an expired or already
answered card answers «Bu karta eskirgan — /lidlar» and nothing else (idempotent: the
`UPDATE … WHERE state='asked'` row count decides).

### 1.4 `/holat` and `/xarajat` layouts

```
🩺 Agent holati — {dd.mm.yyyy hh:mm}
Jarayonlar: bot ✅ · worker ✅ ({age}) · oʻquvchi ✅ ({chats_on} chat, oxirgi xabar {age}) | ⏸ oʻchiq | ❌ {age}
Baza ✅ · Disk {free} GB boʻsh ({evidence_gb} GB dalil) · Zaxira {age} {✅ Telegramga yetdi | ⚠️}
Anthropic: oxirgi muvaffaqiyat {age} · ERP: oxirgi push {age} · roʻyxat toʻliq {age} / farq {age} · breaker {yoʻq|{until}}
Navbat: koʻrib chiqish {n} (eng eskisi {age}) · boyitish {n} · kutish {n} · push navbati {n}
Bugun: push {sent}/{PUSH_DAILY_CAP} · karta {asked}/{REVIEW_DAILY_CAP} · xarajat ${d}/{cap} · oy ${m}/{cap}
Manbalar: {title} {✅|⏸|⛔} {age} · {new_companies} yangi · aniqlik {p}%   (one line per enabled source)
Muammolar: yoʻq | {n} ta:
  ⛔ {problem text with remedy}
```

```
💸 Xarajat — bugun ${d} ({pct}% / ${cap}) · oy ${m} ({pct}% / ${cap}) · rad etilgan chaqiruvlar {n}
Anthropic ${a}: tasnif {n} ta ${x} · xulosa {n} ta ${x} · tungi batch {n} ta ${x} · qoralama ${x}
Qidiruv ${s}: Brave {q} · Yandex {q} (bepul) · SearXNG {q} · Places {q} · 2GIS {q}
Apify ${p} · proksi ${p}
Eng qimmat kompaniyalar: {name} ${x} …
```

### 1.5 `callback_data` grammar, parser, fence

Telegram caps `callback_data` at 64 bytes. All payloads are ASCII, colon-separated, prefix first:

```
rv:<review_id>:<a>        a ∈ p push · d decline · l later · x dnc · m merge-menu · b not_b2b · c competitor · e edit_phone · h handoff · o more · k back
mg:<review_id>:<uuid>     merge into target company (36-char uuid; 3+19+1+36 = 59 bytes worst case)
mh:<review_id>:<a>        manager card: c contacted · n no_reply · x said_no · w wrong_or_b2c · l later
src:<key>:<a>             a ∈ t toggle · i info · a forbid-auto        (key ≤ 40 chars → ≤ 46 bytes)
src:p:<page>              sources page
ch:t:<monitor_id> · ch:p:<page> · ch:noop
ps:<a>                    a ∈ y resume · n keep-paused · c clear-crm-breaker
im:<plan_id>:<y|n>        import plan
pg:<purge_log_id>:<y|n>   purge plan
```

```python
# bot/callbacks.py
@dataclass(frozen=True, slots=True)
class Callback:
    kind: Literal["rv", "mg", "mh", "src", "srcp", "ch", "chp", "chnoop", "ps", "im", "pg"]
    action: str | None = None
    ref: int | None = None          # review_id | monitor_id | plan_id | purge_log_id
    target: str | None = None       # uuid | source key
    page: int | None = None

_GRAMMAR: dict[str, re.Pattern[str]] = {
    "rv":  re.compile(r"^rv:(?P<ref>\d{1,19}):(?P<action>[pdlxmbcehok])$"),
    "mg":  re.compile(r"^mg:(?P<ref>\d{1,19}):(?P<target>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"),
    "mh":  re.compile(r"^mh:(?P<ref>\d{1,19}):(?P<action>[cnxwl])$"),
    "src": re.compile(r"^src:(?P<target>[a-z][a-z0-9_]{2,40}):(?P<action>[tia])$"),
    "srcp": re.compile(r"^src:p:(?P<page>\d{1,3})$"),
    "ch":  re.compile(r"^ch:t:(?P<ref>\d{1,10})$"), "chp": re.compile(r"^ch:p:(?P<page>\d{1,3})$"),
    "chnoop": re.compile(r"^ch:noop$"),
    "ps":  re.compile(r"^ps:(?P<action>[ync])$"),
    "im":  re.compile(r"^im:(?P<ref>\d{1,10}):(?P<action>[yn])$"),
    "pg":  re.compile(r"^pg:(?P<ref>\d{1,19}):(?P<action>[yn])$"),
}

def parse(data: str) -> Callback | None: ...   # first full match wins; never guesses; None → handler answers «Bu tugma eskirgan»
```

`tests/test_bot_callbacks.py` (the fence, F-08): `bot/keyboards.py` exports
`KEYBOARD_SAMPLES: list[tuple[Callable[..., InlineKeyboardMarkup], dict]]` — one entry per
builder with synthetic arguments, including the longest legal `source.key`, a 19-digit
`review_id` and a uuid target. The test (a) builds every keyboard, collects every
`callback_data`, asserts `parse(d) is not None` and `len(d.encode()) <= 64`; (b) asserts every
`(kind, action)` the parser can return has a registered handler (`HANDLERS[kind][action]`, the
dict the routers are built from) and every handler key is producible by some sample; (c) feeds
200 random ASCII strings and MIYA-style near-misses (`rv:1:P`, `rv::p`, `mg:1:not-a-uuid`) and
expects `None`. An unparsed `callback_data` is answered by nobody and the button spins 15 s
(MIYA #937) — hence (a) and (b) together.

### 1.6 Delivery budget and quiet hours

| Setting | Default | Rule |
|---|---|---|
| `REVIEW_HOURS` | `09:00-18:00` Mon–Sat | `review_cards_job` sends only inside; `pending` rows wait |
| `REVIEW_BURST_MAX` | 10 | cards per job run (every 20 min) |
| `REVIEW_DAILY_CAP` | 40 | `asked` rows per Tashkent day; beyond it the digest says «Navbatda N ta» |
| `REVIEW_EXPIRY_HOURS` | 72 | `expires_at` |
| `QUIET_HOURS` | `21:00-08:00` | warnings and the recovery line are held (first tick after quiet hours says them); **critical** problems (§4.2) are said at any hour; cards never go out in quiet hours |
| `DIGEST_TIME` | `09:00` | must lie inside `REVIEW_HOURS` and outside `QUIET_HOURS` (Settings validator, MIYA `MORNING_BRIEF_TIME` shape) |
| `ALERT_REPEAT_HOURS` | 6 | same problem repeated no more often; one «tiklandi» when it clears |

Ordering inside a burst: score desc, then `created_at` asc. Cards are the only unsolicited
question the bot asks; the join queue, merges and enrichment never ask (03, 04).

### 1.7 Manager handoff — Telegram-only prospects

The agent sends nothing to a prospect. A hot candidate without a phone goes to a human who
writes from their own account. Mechanism: `lead_reviews(kind='manager')` (§6) delivered by the
owner bot to one id from `MANAGER_TELEGRAM_IDS` — the manager with the fewest
`kind='manager'` cards today (ties: list order). A manager who has not `/start`ed the bot cannot
receive it; `make doctor` and the go-live checklist verify each id with `get_chat`.

```
📨 Telegram orqali nomzod · {score}/100 · {segment_label_uz}
🏢 {display_name | «firma nomaʼlum»}{ · {city}}
📦 {goods} · 📐 {volume_label}
🔎 «{quote}» — {chat_title}, {dd.mm.yyyy}
🔗 {message_url}
👤 Yozish: t.me/{username} — oʻz akkauntingizdan, 10:00–17:00 (Du–Sha)
✍️ Taklif: {opener ≤ 220, 04's draft or the segment template of docs/sotuv-playbook.md §3}
⚠️ Birinchi xabarda narx yoʻq. Oxirgi jumla: «Agar qiziq boʻlmasa, "yoʻq" deb yozing — boshqa yozmayman.»
[📞 Bogʻlandim] [🙅 Yozmang dedi] [❌ Notoʻgʻri / B2C] [⏭ Keyinroq]
```

| Tap | Effect |
|---|---|
| 📞 Bogʻlandim | `answer='contacted'`; candidate stays `hold`; digest counts it; the manager may then send `/tel <review_id> +998…` — stored as the company's person-tier phone with `legal_basis='pre_contract_request'`, `source_url=message_url`, `collected_at=now()`; candidate → `approved(approved_by='owner:<id> via handoff')` (the owner already approved this company) → normal push gate → ERP lead with phone |
| 🙅 Yozmang dedi | `answer='said_no'` → `suppression(dnc, tg_username, source='owner_bot', reason='manager_report')`; candidate `suppressed`; the owner's digest lists it |
| ❌ Notoʻgʻri / B2C | `answer='wrong_or_b2c'` → `excluded(b2c)`; outcome fed to source precision as negative |
| ⏭ Keyinroq | `expires_at += 24 h` |

No card after 72 h → `expired`, owner digest line «Menejer javob bermadi: {n}». Managers never
receive health alerts, never see `/lidlar`, `/manbalar`, `/unut`. `tests/test_manager_handoff.py`
pins: round-robin, allowlist (a non-listed id tapping `mh:` gets nothing), `/tel` creates a
person-tier contact with all three legal columns, and no Telethon call exists anywhere in
`bot/` (the AST fence of §5.3 covers the package).

### 1.8 Daily digest (`digest_job`, `DIGEST_TIME`, owner only)

```
☀️ {dd.mm.yyyy} — Qidiruv agenti
Kecha: topildi {found} · ball olgan {scored} · karta {asked} · tasdiq {approved} · CRMʼga {pushed}/{cap} · rad {rejected}
Navbat: koʻrib chiqish {review} (eng eskisi {age}) · boyitish {enriching} · kutish {hold} · menejerda {handoff_open}
ERP (30 kun): bogʻlanildi {contacted_pct}% · yutuq {won} · yoʻqotildi {lost} (shundan «Qayta qilinmasin» {lost_no_contact}, notoʻgʻri raqam {lost_wrong_number})
Manbalar: {title} {n} yangi · {p}% ({pushes_30d}) {✅|⏸|⛔} …   (enabled sources; a flip of auto_push is its own line)
Telegram oʻquvchi: {chats_on} chat · kecha {hits} signal · {joined_yesterday} yangi chat · flood {yoʻq|{until}}
Xarajat: kecha ${d} · oy ${m}/{cap} ({pct}%)
Tizim: hammasi joyida | {n} ta muammo — /holat
Sizni {pending} ta karta kutmoqda → /lidlar
```

Lines with nothing to say are omitted. `reminder_log(kind='digest', ref=date)` prevents a
second digest after a restart.

## 2. Telegram reader account

| Step | Rule |
|---|---|
| Provisioning | A new +998 SIM owned by the company, registered to the account on an ordinary phone; profile: real-looking first name, photo, no links; 2FA password set the same day; ageing **2–4 weeks** of normal use (join 2–3 chats by hand, read daily) before the agent logs in; Telegram Premium optional (raises the chat cap to 1,000 and, per vendor claims, tolerance — UNVERIFIED; the agent caps itself at `USERBOT_MAX_CHATS=60`) |
| api_id / api_hash | From my.telegram.org under the company account; a **separate** app from MIYA's; stored only in `.env` |
| Login | `make userbot-login` → `tools/userbot_login.py` (MIYA verbatim): interactive phone, code, 2FA → prints `TELETHON_SESSION=<StringSession>`; paste into `.env`, `make reload`. The string is a full credential; a leak is handled by Settings → Devices → terminate, then a new login |
| One client per session | `AuthKeyDuplicatedError` = two processes on one string; `make doctor` refuses to start when `USERBOT_ENABLED=true` and a second `userbot` container is running; never run the session on a phone and the server at once |
| Runtime | `connect()` not `start()`; `is_user_authorized()` false → beat `{"enabled": true, "authorized": false, "error": code}` and idle (no login prompt in a container); `USERBOT_ENABLED=false` → beat `{"enabled": false}` and idle forever (the container never exits, so `unless-stopped` is safe) |
| Join policy | `userbot/joiner.py`, daily at a random minute 10:00–20:00, `USERBOT_JOINS_PER_DAY=3`, public usernames only, no invite links, `≥ 90 s` between joins, cap 60 chats, nothing joined while `flood_until` or the global pause is set; the agent **never leaves** a chat — the owner leaves by hand from the phone; `/chats` off only stops reading |
| `chat_monitors` defaults | groups and channels `monitor_enabled=true, decided_by='rule:default'`; private chats `false, decided_by='rule:private_off'` and never read (MIYA default flipped; `services/chats.default_monitor_enabled` is the one home) |
| FloodWait | `flood_sleep_threshold=60`; a `FloodWaitError`/`FloodPremiumWaitError` > `USERBOT_FLOOD_BREAKER_SECONDS=300` sets `runtime_flags('flood_until') = now + USERBOT_FLOOD_BREAKER_HOURS (24)`, beats it, raises problem `flood_breaker`; while set: no joins, no backfill, no `GetFullUser`, no dialog sync; live `events.NewMessage` keeps flowing (it is a push from Telegram, not a request) |
| Restriction | `UserDeactivatedBanError`, `AuthKeyUnregisteredError`, `SessionRevokedError`, `PhoneNumberBannedError`, `UserDeactivatedError` → beat `authorized=false`, problem `userbot_unauthorized` (critical), process idles with a 15-min re-check, no reconnect storm. Runbook §4.6 |
| Never | `SendMessage`, `SendReaction`, `SetTyping`, `ReadHistory`, `GetParticipants`, `ImportChatInvite`, `LeaveChannel`, `DeleteHistory`, media download (v1). Fences: `tests/test_no_telegram_writes.py` (MIYA `WRITE_CALLS` + `RAW_REQUEST` scan over every module importing telethon), `test_join_only_in_joiner` (`JoinChannelRequest` appears only in `userbot/joiner.py`), `test_no_participants_calls` |
| Standby | A second SIM, aged the same way, 2FA on, **never** logged into the agent until needed; its `api_id` is the same app |

## 3. Deployment

### 3.1 `docker-compose.yml`

```yaml
name: clientscrapper
x-logging: &log { driver: json-file, options: { max-size: "10m", max-file: "3" } }
x-app: &app
  build: .
  restart: unless-stopped
  env_file: .env
  logging: *log
  environment: { TZ: "${TIMEZONE:-Asia/Tashkent}" }
  volumes: [ "./data:/data", "./secrets:/app/secrets:ro" ]
  depends_on:
    db:   { condition: service_healthy }
    init: { condition: service_completed_successfully }
services:
  db:
    image: postgres:16            # plain postgres: no pgvector, no embeddings in this project
    restart: unless-stopped
    oom_score_adj: -500
    mem_limit: 1536m
    logging: *log
    environment: { POSTGRES_USER: "${POSTGRES_USER:-clientscrapper}", POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:?required}", POSTGRES_DB: "${POSTGRES_DB:-clientscrapper}", TZ: "${TIMEZONE:-Asia/Tashkent}" }
    command: ["postgres", "-c", "shared_buffers=384MB", "-c", "work_mem=16MB", "-c", "timezone=Asia/Tashkent", "-c", "log_min_duration_statement=2000"]
    volumes: [ "db_data:/var/lib/postgresql/data" ]
    ports: [ "127.0.0.1:${DB_PORT:-5433}:5432" ]   # localhost only; 5433 because MIYA may hold 5432 on the same VPS
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-clientscrapper} -d ${POSTGRES_DB:-clientscrapper}"], interval: 5s, timeout: 5s, retries: 20 }
  init:                            # chown ./data, ./secrets to uid 10001, then alembic upgrade head
    build: .
    user: root
    env_file: .env
    logging: *log
    depends_on: { db: { condition: service_healthy } }
    volumes: [ "./data:/data", "./secrets:/secrets" ]
    command: ["sh", "-c", "mkdir -p /data/evidence /data/backups /data/imports && { chown -R 10001:10001 /data /secrets || echo 'chown skipped'; } && su -s /bin/sh clientscrapper -c 'alembic upgrade head'"]
    restart: "no"
  worker:  { <<: *app, mem_limit: 2g,   cpus: 2.0, command: ["python", "-m", "clientscrapper.worker.main"] }
  userbot: { <<: *app, mem_limit: 768m, cpus: 0.5, command: ["python", "-m", "clientscrapper.userbot.main"] }
  bot:     { <<: *app, mem_limit: 512m, cpus: 0.5, command: ["python", "-m", "clientscrapper.bot.main"] }
  searxng:                         # optional: docker compose --profile searxng up -d
    image: searxng/searxng:2026.10.1      # UNVERIFIED tag — pin the current one on day 1
    profiles: ["searxng"]
    restart: unless-stopped
    mem_limit: 512m
    logging: *log
    volumes: [ "./searxng:/etc/searxng:ro" ]   # settings.yml: json format on, limiter off, engines duckduckgo,yandex,bing
    # no ports: reachable only as http://searxng:8080 inside the compose network
volumes: { db_data: {} }
```

No service publishes a port to the network; the only host binding is Postgres on `127.0.0.1`
for `make psql`. There is no `api` service in v1 (CLAUDE.md: FastAPI optional, not built). No
reverse proxy, no TLS, no public DNS. `tests/test_compose.py` asserts: no `ports:` entry
without the `127.0.0.1:` prefix, every app service has `mem_limit`, `env_file`, `depends_on
init completed`, `restart: unless-stopped`, log rotation, and the service set is exactly
`{db, init, worker, userbot, bot, searxng}`.

### 3.2 `Dockerfile`

MIYA's minus torch, ffmpeg and the HF cache: `FROM python:3.12-slim` (tag pinned, digest recorded
in a comment and refreshed monthly), `ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
TZ=Asia/Tashkent PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10`, `apt-get install --no-install-recommends
tzdata ca-certificates curl age postgresql-client`, the PGDG `postgresql-client-16` attempt
(non-fatal, MIYA verbatim, so `pg_dump` matches the server major), `pip install -r
requirements.txt` under a cache mount, `COPY alembic.ini alembic clientscrapper`, `useradd
--uid 10001 clientscrapper`, `mkdir -p /data/evidence /data/backups && chown`, `USER
clientscrapper`, `CMD ["python", "-m", "clientscrapper.worker.main"]`. Image ≈ 350 MB.
`tests/test_dockerfile.py`: no `torch`, `ffmpeg`, `sentence-transformers`, `huggingface` strings;
`USER clientscrapper` after the last `COPY`; `age` and `postgresql-client` present.

### 3.3 `Makefile` targets

| Target | Does |
|---|---|
| `env` | `cp .env.example .env` if missing |
| `doctor` | builds the image, runs `tools/doctor.py` with `.env` injected: every key of `EXPECTED_ENV_KEYS` present (a test keeps that tuple equal to `.env.example`), `POSTGRES_PASSWORD` inside `DATABASE_URL`, `OWNER_TELEGRAM_ID` numeric, `MANAGER_TELEGRAM_IDS` each reachable via `get_chat`, bot token via `get_me`, `TELETHON_*` when `USERBOT_ENABLED`, `CRM_BASE_URL` answers `GET /api/agent/suppression?limit=1` (200 ok · 404 «kalit yoki manba yoʻq» warning when `CRM_PUSH_ENABLED=false`, error when true), `ANTHROPIC_API_KEY` present (no call), `BACKUP_AGE_RECIPIENT` starts `age1` and `secrets/backup-key.txt` exists, RAM ≥ 4 GB, swap ≥ 1 GB, free disk ≥ `DISK_MIN_FREE_GB`, `age`/`pg_dump` binaries, `DIGEST_TIME` inside `REVIEW_HOURS`. Exit 1 on any ❌ |
| `up` | `env` → `doctor` (skip with `SKIP_DOCTOR=1`) → `compose up -d --build db init` → `compose up -d --build worker userbot bot` |
| `migrate` | `compose run --rm init alembic upgrade head` |
| `revision m="…"` / `downgrade` | Alembic autogenerate / `-1` |
| `userbot-login` | `compose run --rm -it userbot python -m clientscrapper.tools.userbot_login` |
| `backup` / `backup-key` / `backup-key-show` / `restore FILE= [DRY=1] [FORCE=1]` | MIYA verbatim (§4.4) |
| `update` | `backup` (fails = stop) → `git pull --ff-only` → `compose stop worker userbot bot` → `up IN_UPDATE=1` → `docker image prune -f` → «Yangilandi. Botda /holat ni tekshir.» |
| `import-clients FILE= [APPLY=1]` | `tools/import_clients.py` → `suppression(manual_import)`; dry run by default |
| `push-test` | 05 §B.1 synthetic push |
| `purge --company <uuid> \| --phone <tail9> \| --tg-user <id> [--apply]` | `tools/purge.py` (§5.4) |
| `reload` | `compose up -d --force-recreate worker userbot bot` after editing `.env` |
| `logs` / `ps` / `psql` / `bot` / `worker` / `userbot` | tails and shells |
| `test` / `lint` / `fmt` / `typecheck` / `check` | `pytest -q` · `ruff check .` · `ruff format .` · `mypy` (files list in `pyproject.toml`: `services/`, `bot/callbacks.py`, `userbot/`, `logs.py`) |

### 3.4 `.env.example` (complete; defaults as shown; blank = must be filled or feature off)

```
# --- Database ---
POSTGRES_USER=clientscrapper
POSTGRES_PASSWORD=
POSTGRES_DB=clientscrapper
DATABASE_URL=postgresql+psycopg://clientscrapper:CHANGE_ME@db:5432/clientscrapper   # same password as above, literally
DB_PORT=5433                           # host-side bind on 127.0.0.1 only
# --- Claude ---
ANTHROPIC_API_KEY=
EXTRACT_MODEL=claude-haiku-5-5         # ImportSignal / classify
VERDICT_MODEL=claude-sonnet-5-5        # RelevanceVerdict
BATCH_MODEL=claude-opus-5-5            # nightly company verdicts via Message Batches (−50 %)
MODEL_PRICES=claude-haiku-5-5:0.10/0.50,claude-sonnet-5-5:2.00/10.00,claude-opus-5-5:4.00/20.00   # $/MTok in/out; an unlisted model is unpriced = refused by costs.ask
DAILY_COST_CAP_USD=10
MONTHLY_COST_CAP_USD=250
CLAUDE_DAILY_CAP_USD=5                 # per-provider allocation; the rest per 03 §1.3 lives in sources.daily_cost_cap_usd
SPEND_ALERT_PCT=80
# --- Telegram: owner bot and managers ---
ASSISTANT_BOT_TOKEN=
OWNER_TELEGRAM_ID=
MANAGER_TELEGRAM_IDS=                  # comma-separated; empty = Telegram-only prospects wait in hold
# --- Telegram: reader account ---
USERBOT_ENABLED=true
TELETHON_API_ID=
TELETHON_API_HASH=
TELETHON_SESSION=                      # make userbot-login
USERBOT_JOINS_PER_DAY=3
USERBOT_MAX_CHATS=60
USERBOT_FLOOD_BREAKER_SECONDS=300
USERBOT_FLOOD_BREAKER_HOURS=24
# --- Collectors ---
COLLECTORS_ENABLED=true
COLLECTOR_RPS_DEFAULT=0.3
COLLECTOR_DAILY_PAGE_BUDGET=3000       # sum over sources; per-source budgets in the sources table
HH_USER_AGENT=                         # "<app name> (<contact e-mail>)" as hh.ru requires
HH_APP_TOKEN=
BRAVE_API_KEY=
YANDEX_SEARCH_API_KEY=
YANDEX_FOLDER_ID=
GOOGLE_PLACES_API_KEY=
APIFY_TOKEN=
SEARXNG_URL=http://searxng:8080        # blank = SearXNG off
PROXY_URL=                             # phase 2 (Uzum); blank = no proxy ever used
IG_GRAPH_TOKEN=
IG_BUSINESS_ACCOUNT_ID=
TWOGIS_API_KEY=
# --- ERP (fullprompt) ---
CRM_PUSH_ENABLED=false                 # true only after make push-test succeeded
CRM_BASE_URL=https://erp.example.uz
CRM_PUSH_SOURCE=agent                  # other = fallback door (05 §B.3)
CRM_AGENT_KEY=
PUSH_DAILY_CAP=25
PUSH_WINDOW=09:30-17:30
PUSH_DAYS=1-6                          # ISO weekdays Mon–Sat
# --- Owner bot cadence ---
REVIEW_HOURS=09:00-18:00
REVIEW_BURST_MAX=10
REVIEW_DAILY_CAP=40
REVIEW_EXPIRY_HOURS=72
DIGEST_TIME=09:00
QUIET_HOURS=21:00-08:00
ALERT_REPEAT_HOURS=6
# --- Health ---
HEARTBEAT_STALE_MINUTES=10
USERBOT_STALE_MINUTES=5
DISK_MIN_FREE_GB=5
DEADMAN_PING_URL=
DEADMAN_PING_MINUTES=5
# --- Backups ---
BACKUP_DIR=/data/backups
BACKUP_TIME=03:45                      # after erp_sync outcomes (03:00), before retention (04:15)
BACKUP_RETENTION_DAYS=14
BACKUP_AGE_RECIPIENT=                  # make backup-key; blank = no backup is written at all
BACKUP_TO_TELEGRAM=true
BACKUP_MAX_AGE_HOURS=30
RESTORE_DRILL_ENABLED=true
RESTORE_DRILL_DAY=sun
# --- Data on disk ---
EVIDENCE_DIR=/data/evidence
EVIDENCE_MAX_GB=30
# --- Misc ---
TIMEZONE=Asia/Tashkent
LOG_LEVEL=INFO
LOG_FORMAT=json
```

`tests/test_env_example.py` (MIYA shape): every `Settings` field has a line in `.env.example`
and vice versa; `tools/doctor.EXPECTED_ENV_KEYS` equals the file's keys; no value in the example
looks like a real secret (`sk-ant-`, a 46-char Telegram token, an `age1` key, `1A…` session).

### 3.5 Secrets, server, time, disk

- Secrets exist in exactly one place: the server's `.env` (mode 0600, owner root) and
  `secrets/backup-key.txt` (0600). `.env`, `secrets/`, `data/`, `searxng/` are gitignored.
  Nothing is passed on the command line (`PGPASSWORD` via env, MIYA `libpq_dsn_and_env`).
  Rotation: `CRM_AGENT_KEY` (re-mint in the ERP → `.env` → `make reload`; the next feed pull
  sees the new `salt_id`, 05 §B.2), `ASSISTANT_BOT_TOKEN` (BotFather revoke → `.env` →
  `make reload`), `TELETHON_SESSION` (§2), API keys (provider console; all keys carry the
  provider's hard spend limit as the second fence behind `costs.ask`).
- Server: **4 vCPU / 8 GB RAM / 80 GB SSD**, Ubuntu 24.04, Docker Engine + Compose v2, 2 GB
  swap, `ufw default deny incoming`, SSH key-only. RAM budget: db 1.5 GB, worker 2 GB, userbot
  0.75 GB, bot 0.5 GB, searxng 0.5 GB, OS 1 GB ≈ 6.3 GB. Co-location with MIYA is allowed only
  as a separate compose project, database container, port, backup key, bot token and Telegram
  account (audit memo §4: the owner's private ledger never shares a container or a key with a
  sales database).
- Outbound hosts (document every addition in this table, MIYA «Documented egress» rule):
  `api.anthropic.com` (pseudonymised text), `api.telegram.org` (bot), Telegram MTProto (reader),
  `CRM_BASE_URL`, `api.search.brave.com`, `searchapi.api.cloud.yandex.net`, `places.googleapis.com`,
  `api.apify.com`, `api.hh.uz`, `graph.facebook.com`, `catalog.api.2gis.com`, the crawled sites
  of 03, `DEADMAN_PING_URL`. A proxy host only when `PROXY_URL` is set.
- Time: `TZ=Asia/Tashkent` in every container, Postgres `timezone`, APScheduler timezone,
  `settings.tz`; «today» for caps, budgets, `external_id` months and the digest is the Tashkent
  calendar day (`services/time.day_start`).
- Disk: evidence at `EVIDENCE_DIR/<yy>/<sha256>.gz` (02 §3); expected ≈ 150–250 MB/day at 200
  observations/h → ≤ 45 GB at the 180-day horizon, hence `EVIDENCE_MAX_GB=30`: `retention_job`
  R1 deletes expired files first, then, while the directory exceeds the cap, the oldest
  `raw_path` files (rows stay), and raises `evidence_over_cap` (warning) when it had to.
  Backups ≈ 50–300 MB each × 14 days. `disk_low` (critical) at `DISK_MIN_FREE_GB`.

## 4. Operations

### 4.1 Health model (`services/health.py`, MIYA core verbatim)

Heartbeats: `bot`, `worker`, `userbot`, `backup`, `restore_drill`, `job:<id>` for every
scheduler job, `collector:<key>` per collector run. `gather()` adds: db ping, disk, evidence
size, newest backup, last Anthropic success, ERP (last `push_log.sent`, `erp_sync.config`
timestamps, breaker), queue depths, today's pushes/cards/spend, per-source rows. The alert
ledger is `reminder_log` with kinds `alert:<key>` / `recovered:<key>` (MIYA names; 02's comment
`health:<problem>` is to be aligned to these). Whichever of the worker's `health_job` (every 5
min) or the bot's watchdog is alive speaks; neither repeats the other.

### 4.2 Problems (`PROBLEM_KEYS`, ordered as shown; each has Uzbek text with its remedy and a recovery line)

| key | severity | condition | alert text (first line) |
|---|---|---|---|
| `db_down` | critical | the bot cannot reach Postgres (in-memory memo) | «⛔ Baza javob bermayapti — `make ps`, `make logs`» |
| `disk_low` | critical | free < `DISK_MIN_FREE_GB` | «⛔ Diskda {free} GB qoldi — `make backup` tekshir, `EVIDENCE_MAX_GB` kamaytir» |
| `worker_silent` | critical | `worker` beat older than `HEARTBEAT_STALE_MINUTES` | «⛔ Rejalashtiruvchi {age} jim — `make worker`» |
| `userbot_unauthorized` | critical | userbot detail `authorized=false` | «⛔ Telegram oʻquvchi akkaunti chiqib ketgan/cheklangan ({code}). @SpamBot tekshiring; `make userbot-login`» |
| `push_404` | critical | ERP 404 on push or feed (05) | «⛔ ERP kaliti yoki manba notoʻgʻri: 404. Kelgan arizalar → Reklama havolalari → Agent → kalit; `.env` CRM_AGENT_KEY; `make reload`; `/yoq crm`» |
| `backup_failed` | critical | `backup` heartbeat `ok=false` | «⛔ Tungi zaxira yozilmadi: {error}» |
| `restore_drill_failed` | critical | drill beat `ok=false` | «⛔ Zaxirani tiklash sinovi oʻtmadi — zaxiralar ishonchsiz» |
| `suppression_stale` | warning → critical after 6 h | feed configured and last full pull > 36 h or `salt_id` mismatch (05) | «⚠️ ERP bostirish roʻyxati {age} yangilanmadi — push toʻxtatildi» |
| `userbot_silent` | warning | userbot beat > `USERBOT_STALE_MINUTES`, not disabled | «⚠️ Telegram oʻquvchi {age} jim — `make userbot`» |
| `flood_breaker` | warning | `runtime_flags.flood_until > now` | «⚠️ Telegram FloodWait ({seconds} s) — oʻquvchi {until} gacha qoʻshilmaydi/oʻqimaydi (jonli xabarlar davom etadi)» |
| `collector_blocked` | warning | any `sources.breaker_until > now` with reason 403/429 | «⚠️ {title}: sayt bloklayapti (403/429) — {until} gacha toʻxtadi» |
| `collector_silent` | warning | enabled source with cron, `last_run_at` older than 2× its interval | «⚠️ {title} {age} ishlamadi — `/manbalar`» |
| `cost_cap` | warning, told first by `costs.ask` | today ≥ `DAILY_COST_CAP_USD` or month ≥ monthly cap | «💸 Kunlik xarajat chegarasi ($10) — ertagacha pullik chaqiruvlar toʻxtadi; kollektorlar bepul qismini davom ettiradi» |
| `spend_high` | warning | ≥ `SPEND_ALERT_PCT` of either cap | «⚠️ Xarajat {pct}% ga yetdi (${d}/${cap})» |
| `no_signals_24h` | warning | userbot enabled, ≥ 1 chat on, zero `interactions` in 24 h | «⚠️ 24 soatda bitta ham Telegram signal yoʻq — chatlar oʻlikmi? `/chats`» |
| `push_failing` | warning | 3 consecutive candidates failed on 5xx/transport | «⚠️ ERP javob bermayapti ({n} ta push muvaffaqiyatsiz)» |
| `push_unconfirmed` | warning | `push_log.unknown` older than 48 h | «⚠️ {n} ta push tasdiqlanmagan — `/push tekshir`» |
| `feed_contract` | warning | `FeedShapeError` / rule changed / runaway / bad request (05) | «⚠️ ERP feed shakli oʻzgardi ({code}) — dasturchi kerak» |
| `suppression_shrunk` | warning | full pull < 90 % of previous count | «⚠️ ERP roʻyxati {pct}% ga qisqardi — oʻchirish toʻxtatildi» |
| `review_backlog` | warning | ≥ 30 candidates in `review` older than 72 h | «⚠️ {n} ta karta 3 kundan beri kutmoqda — /lidlar» |
| `anthropic_failing` | warning | ≥ 5 `API_FAILURES` in 1 h, or no success in 3 h with jobs pending | «⚠️ Anthropic javob bermayapti ({code})» |
| `backup_unconfigured` / `backup_stale` | warning | no recipient / newest older than `BACKUP_MAX_AGE_HOURS` | «⚠️ Zaxira sozlanmagan — `make backup-key`» / «⚠️ Oxirgi zaxira {age}» |
| `ig_token_expired` | warning | Graph API 190 | «⚠️ Instagram tokeni eskirdi — yangilang» |
| `source_low_precision` | warning, once per source per week | `pushes_30d ≥ 30` and `precision_30d < 0.20` → `sources.paused_until = +7 d` | «⚠️ {title} aniqligi {pct}% — bir haftaga toʻxtatildi (`/manbalar` bilan yoqiladi)» |
| `evidence_over_cap` | warning | retention had to delete under-age files | «⚠️ Dalil papkasi {gb} GB — eski fayllar muddatidan oldin oʻchirildi» |

Critical problems ignore `QUIET_HOURS`. `TOLD_FIRST_ELSEWHERE = {cost_cap, push_404,
flood_breaker}` (their own job says it at once; the sweep repeats after `ALERT_REPEAT_HOURS`).
Dead-man: the worker GETs `DEADMAN_PING_URL` every `DEADMAN_PING_MINUTES` from `heartbeat_job`
(a healthchecks.io-style monitor with a 15-minute grace; the free tier is UNVERIFIED — check
the plan before relying on it). This is the only alarm that does not depend on the bot.

### 4.3 Alert routing

Owner only, through the bot (`notify()` with the plain-text retry, delivered-then-marked).
Managers receive only their own cards. Problems that depend on a component are never reported
by that component (the bot watches the worker, the worker watches the userbot and itself, the
outside watcher watches the worker).

### 4.4 Backups

- `backup_job` at `BACKUP_TIME`: `pg_dump -Fc | age -r $BACKUP_AGE_RECIPIENT -o
  /data/backups/clientscrapper-<yyyymmdd-hhmmss>.dump.age` (plaintext only inside the pipe),
  prune older than `BACKUP_RETENTION_DAYS`, then send the file to the owner's Telegram as a
  document (`BACKUP_TO_TELEGRAM`, split into ≤ 45 MB `.partNN` pieces, ciphertext), beat
  `backup` with `{ok, size, sent, path}` and `backup_counts` with row counts of
  `COUNTED_TABLES = (companies, contacts, lead_candidates, push_log, suppression, outcomes)`.
  No recipient → nothing written (an unencrypted dump of a phone book is not a fallback).
- Key: `make backup-key` once (refuses to overwrite), `make backup-key-show` → password manager
  off the server. Losing the key loses every backup.
- `restore_drill` weekly (`RESTORE_DRILL_DAY` 05:00): restore the newest file into
  `clientscrapper_restore_drill`, compare counts with `backup_counts`, drop the scratch db, beat
  `restore_drill`. Monthly the owner also runs the real procedure on a scratch VPS or laptop:
  `docker compose up -d db` → `make restore FILE=… DRY=1` → `make restore FILE=…` → `make up`
  (`FORCE=1` only on a non-empty database).
- Not in the dump: `EVIDENCE_DIR` (cards and ERP notes need only `evidence.url/excerpt`, which
  are rows). Optional `restic` to object storage is an ops choice, not v1.
- `make update` begins with a backup that must succeed.

### 4.5 Logs

JSON lines (`LOG_FORMAT=json`), ids and counts only. `RedactBodies` drops extras named
`text, body, raw_text, transcript, draft, phone, phones, note, payload, headers, key, secret,
token, session, html, quote, bio`. A second filter, `RedactPatterns`, rewrites `msg` and
`exc`:

| pattern | replacement |
|---|---|
| `\+?998[\s-]?\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}` | `<phone>` |
| `(?i)(x-gsr-key|x-erp-key|authorization|x-api-key)[=:]\s*\S+` | `\1=<key>` |
| `sk-ant-[A-Za-z0-9_-]{20,}` · `\d{8,10}:[A-Za-z0-9_-]{35}` · `(?i)telethon_session=\S+` | `<secret>` |
| `postgresql(\+\w+)?://([^:/]+):([^@]+)@` | `postgresql\1://\2:***@` |
| `@[A-Za-z][A-Za-z0-9_]{4,31}` (outside URLs) | `@…` |

Logged: component, job id, counts, durations, HTTP status, host, `source_key`, `company_id`,
`candidate_id`, `external_id`, `push_log.id`, error codes, `usage_log` cost. Never logged:
message bodies, names, phones, handles, quotes, payloads, headers, any `.env` value, evidence
bytes. `tests/test_logs.py` feeds each pattern through a real handler and reads the line back.
Container logs rotate at 3 × 10 MB; the database (`collector_runs`, `push_log`, `usage_log`) is
the durable record.

### 4.6 Runbook

| Incident | Signs | Steps | Verify |
|---|---|---|---|
| Reader account restricted | `userbot_unauthorized`; `/holat` oʻquvchi ❌ `{code}` | (1) On the phone with that SIM open @SpamBot → read the status, appeal if offered. (2) `USERBOT_ENABLED=false`, `make reload` — collectors continue. (3) If a temporary limit: wait it out, then `make userbot-login`, new `TELETHON_SESSION`, `make reload`. (4) If banned: standby SIM → `make userbot-login` → `.env` → `make reload`; `chat_monitors.joined_at := NULL` for all rows (tool `tools/chats.py --rejoin`), the joiner re-joins 3/day by priority. (5) Review what tripped it: joins/day, backfill size, `GetFullUser` count in `usage_log`/logs | `/holat` oʻquvchi ✅; first `interactions` row within 1 h |
| ERP key rotated / 404 | `push_404`; pushes `failed`; feeds `failed` | New key from **Kelgan arizalar → Reklama havolalari → Agent**; `.env CRM_AGENT_KEY`; `make reload`; `/yoq crm`; the next hourly delta sees a new `salt_id` → full pull runs (05 §B.2); pushes stay refused by `suppression_stale` until it finishes | `/holat`: roʻyxat toʻliq {minutes ago}; `make push-test` 200 |
| Cost cap hit | `cost_cap` at hh:mm | Nothing to do today: `costs.ask` refuses, collectors keep the free part, signals queue. Tomorrow: `/xarajat` → which operation; lower `sources.daily_cost_cap_usd` for the offender via `/manbalar`, or raise `DAILY_COST_CAP_USD` only with a matching provider-side limit | spend < 80 % by noon next day |
| Disk full | `disk_low`; writes failing | `du -sh data/evidence data/backups`; `docker system df`; `make backup` must still work; lower `EVIDENCE_MAX_GB` and run `tools/retention.py --step R1 --now`; `docker image prune -f`; never delete `data/backups` newer than 3 days | free ≥ `DISK_MIN_FREE_GB` × 2 |
| Duplicate pushes suspected | manager reports two leads for one company | `SELECT external_id, attempt, status, http_status FROM push_log WHERE company_id=… ORDER BY sent_at`; one `sent` per month per company is correct; two different months with new signals is correct by design (02 §2.9); two `sent` in one month is impossible (`ux_push_log_delivered`) — if the ERP shows two leads, they came through different doors (`other` fallback + `agent`): delete the `manba=other` route (05 §B.3) and tell the manager to join the leads in the ERP | `push_log` matches `outcomes`; `push_unconfirmed` clear |
| Worker silent | `worker_silent` | `make worker` → traceback; usually a migration or a failing job at startup; `make update` rollback = `git checkout <prev>` + `make up` + `make restore` only if a migration broke data | heartbeat fresh; `/holat` |
| Backup failed | `backup_failed` | `make backup` by hand → error: pg_dump major (rebuild image), disk, missing recipient | `backup` beat `ok=true`, file in Telegram |
| Feed shrank | `suppression_shrunk` | Nothing deleted; check the ERP: did a client export change, did `agent_no_contact_reasons` get renamed (05 A.7)? Fix the label, then `tools/erp_sync.py --full --accept-shrink` once | full pull count ≥ previous |

## 5. Security and legal as code

### 5.1 Data tiers by column

| Tier | Columns | Rule in code |
|---|---|---|
| Company (free) | `companies.*`, `company_aliases`, `company_ids`, `contacts(tier='company')`: switchboard `phone`, `info@` `email`, `telegram_username` of the @company channel, `instagram` of the business account; `signals`, `evidence.url/excerpt`, `lead_candidates.score*`, `outcomes` | stored for the life of the company; mirrored to Sheets/digest freely |
| Person (restricted) | `contacts(tier='person')`: `full_name`, `role`, personal `phone`, `telegram_id`, `telegram_username`, `instagram`, `email`; `interactions.sender_*`; `observations.parsed.person/phones`; `model_jobs.pseudonyms` | one live row per company (`ux_contacts_one_person`); `legal_basis`, `source_url`, `collected_at` NOT NULL by CHECK; never in Sheets, logs, digest; anonymised by R4/R5; purged on request |
| Forbidden | passports, PINFL, birth dates, home addresses, member lists, profile photos, full chat logs, customs declaration rows, second/third personal numbers | no columns exist; `ck_contacts_no_pinfl`; `interactions` accepts only prefilter hits; `tests/test_schema_tiers.py` greps `db/models.py` for forbidden names (`pinfl`, `passport`, `birth`, `members`, `photo`) |

### 5.2 Pseudonymisation before any model call (`services/pseudonym.py`)

```python
@dataclass(frozen=True, slots=True)
class Masked:
    text: str
    mapping: dict[str, str]        # "<PHONE_1>" -> "+998901234567"; stored in model_jobs.pseudonyms (30 d), never logged

PHONE_RE = re.compile(r"(?<!\d)(?:\+?998|8)?[\s(-]*\d{2}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
TG_RE    = re.compile(r"(?<![\w/])@[A-Za-z][A-Za-z0-9_]{4,31}\b|t\.me/(?!s/)([A-Za-z][A-Za-z0-9_]{4,31})")
IG_RE    = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", re.I)

def mask(text: str, *, known_names: Sequence[str] = ()) -> Masked      # phones→<PHONE_n>, handles→<TG_n>/<IG_n>, e-mails→<EMAIL_n>, known sender names→<NAME>; company names and goods untouched
def unmask(text: str, mapping: Mapping[str, str]) -> str
def assert_masked(text: str) -> None                                     # raises UnmaskedPayload when PHONE_RE/TG_RE/EMAIL_RE still match
```

`services/extraction.extract()` and `services/batch.submit_pending()` accept only `Masked`
and call `assert_masked` before building the request; the response is unmasked locally. Fences:
`tests/test_pseudonym.py` (golden Uzbek/Russian/Cyrillic cases); `tests/conftest.py` installs
an `httpx.MockTransport` on the Anthropic client for the whole suite that fails any test whose
request body matches `PHONE_RE`/`TG_RE`; `tests/test_imports_fence.py` (AST) allows `import
anthropic` only in `services/extraction.py`, `services/batch.py`, `services/costs.py`, and
`import telethon` only in `userbot/`, `tools/userbot_login.py`.

### 5.3 No outbound contact, ever

No sender module, no outbox, no draft send. `tests/test_no_telegram_writes.py` (MIYA
`WRITE_CALLS`, `RAW_REQUEST` scan over every module that imports telethon) and the aiogram side:
`bot/` may call `send_message`/`edit_message_text`/`answer` only with `chat_id ∈ {owner} ∪
managers` — enforced by one wrapper `bot/deliver.py::to_person(bot, telegram_id, …)` that checks
the allowlist, and an AST test that no other module in `bot/` calls `bot.send_*` directly. No
SMS, e-mail or telephony dependency exists in `requirements.txt` (test).

### 5.4 Retention and purge

Retention is 02 §4 (`retention_job` 04:15, steps R1–R7, each its own `purge_log(retention)`
row); 06 adds the evidence cap (§3.5). Purge:

```python
# services/purge.py (MIYA plan→execute shape)
def plan_company(session, company_id, *, reason) -> PurgePlan      # counts per table + files; nothing changes
def plan_contact(session, phone_tail | tg_user_id) -> PurgePlan      # resolves to companies first
async def execute(session, plan, *, requested_by) -> PurgeResult     # one transaction per table group; files unlinked last
```

Deleted: `contacts`, `observations`, `interactions` of the subject's `tg_user_id`,
`model_jobs.request/pseudonyms`, `evidence` raw files whose only rows belong to the subject,
`company_aliases`, `company_ids`, open `lead_candidates`/`lead_reviews`, `sheet_rows`
(tombstoned). Anonymised: `push_log.payload := NULL`, `companies.display_name := '[oʻchirilgan]'`
and `state='purged'` when the whole company goes. Kept: `outcomes`, `purge_log` (hash only),
`usage_log`, and hashed `suppression` rows; with `reason='legal_request'` or `'dnc'` a
`suppression(legal|dnc)` row is written for every key the subject had, so the same company
cannot be re-created by the next observation. The plan card shows counts, never names.
`/unut` and `tools/purge.py` share the same two functions; execution requires the explicit
`pg:<id>:y` tap or `--apply`.

### 5.5 Suppression precedence and overrides

`services/suppression.is_suppressed()` (02 §2.8) orders hits by
`KIND_RANK = legal 0 · dnc 1 · client 2 · lost 3 · bounced 4 · competitor 5 · manager_veto 6 · b2c 7 · cooldown 8`
and returns the first. A hit stops a card, a manager card, a push and a Sheets row; the owner
has no button that overrides it (a candidate that became suppressed between card and tap gets
«🚫 Bu raqam endi bostirilgan» and the card closes). Removal: `client`/`lost` rows belong to the
ERP and disappear only when the feed stops listing them; `legal` rows are never removed;
`dnc`/`manager_veto`/`b2c`/`competitor` rows only via `tools/suppression.py remove <id> --reason`
(logged in `purge_log(reason='owner')`). Consent: the agent asks no one for anything; the only
consent-like facts it stores are `contacts.legal_basis` (`public_source`, `public_registry`,
`pre_contract_request` from a manager's `/tel`) with `source_url` and `collected_at`.
`consent_status` and refusals live in the ERP (contacted/lost reasons) and reach the agent as
outcomes and feed entries.

### 5.6 Access control, surface, dependencies

- Bot: owner id on every owner router; managers only on `manager_router`; everyone else is
  ignored; the bot leaves any group it is added to. No web UI, no public API, no open port
  (`tests/test_compose.py`). Postgres reachable only from the compose network and `127.0.0.1`.
- Processes run as uid 10001; `secrets/` is read-only in app containers; `data/` is 0750.
- Dependencies: every line in `requirements.txt` and `requirements-dev.txt` is `name==x.y.z`
  (`tests/test_requirements_pinned.py`); starting set from MIYA: `SQLAlchemy==2.0.36`,
  `alembic==1.14.0`, `psycopg[binary,pool]==3.2.3`, `APScheduler==3.11.0`, `httpx==0.28.1`,
  `anthropic==0.122.0`, `aiogram==3.30.0`, `Telethon==1.44.0`, `rapidfuzz==3.14.5`,
  `pydantic==2.10.4`, `pydantic-settings==2.7.1`, `openpyxl==3.1.5` (versions as of MIYA's
  2026-10-22 HEAD — bump on day 1 and re-pin); no `sentence-transformers`, `torch`, `fastapi`.
  Monthly bump PR (Dependabot or by hand) run through `make check` and a 24-hour canary.
- The Docker base image tag is pinned and its digest recorded; `make update` never pulls `latest`.

## 6. Schema touches requested from 02

| Table | Change | Why |
|---|---|---|
| `runtime_flags` (new) | `key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(), updated_by text NOT NULL` | `/toxtat` must survive restarts and be read by all three processes; keys `paused` (`{"since","reason"}`), `flood_until`, `review_hours_override` |
| `lead_reviews` | `kind text NOT NULL DEFAULT 'owner' CHECK (kind IN ('owner','manager'))`; `answer` CHECK widened with `handoff, contacted, said_no, wrong_or_b2c, no_reply`; `ux_lead_reviews_live` becomes `(candidate_id, kind)` | manager cards (§1.7) without a new table |
| `reminder_log` | comment: kinds `alert:<key>`, `recovered:<key>`, `digest`, `review_expired`, `push_unconfirmed`, `source_paused` | §4.1 |
| `chat_monitors` | `status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','joined','gone','left'))`, `priority smallint NOT NULL DEFAULT 5` | already proposed by 03 §4; the joiner (§2) reads them |

## 7. Checklists

### 7.1 Go-live (ops, English)

1. VPS per §3.5; `ufw` enabled; Docker installed; repo cloned; `make env`; fill `.env`; `chmod 600 .env`.
2. `make backup-key`; secret key stored off-server (two places); `BACKUP_AGE_RECIPIENT` set.
3. `make doctor` green; `make up`; `make ps` shows db, worker, userbot, bot healthy; `/holat` from the owner account answers.
4. BotFather: bot created, group joining disabled; owner and every manager `/start` the bot; `make doctor` shows each manager id reachable.
5. Reader account: SIM aged ≥ 14 days, 2FA, `make userbot-login`, `make reload`, `/chats` lists the 3 seed chats within 24 h.
6. ERP (fullprompt `agent-feed` deployed): key minted, `agent_no_contact_reasons` matches the lost-reason label, routing rules A and B present; `make push-test` → 200; `CRM_PUSH_ENABLED=true`; first delta pull shows a non-zero suppression count in `/holat`.
7. Fallback if `agent-feed` is not deployed: `CRM_PUSH_SOURCE=other`, `make import-clients FILE=clients.xlsx APPLY=1`, `PUSH_DAILY_CAP=10`.
8. Billing: Anthropic monthly limit $150 and e-mail alert at $100; Google Cloud budget $5/month with hard cap; Brave $5 credit only; Apify prepaid ≤ $20.
9. `DEADMAN_PING_URL` configured and a test alert received on the owner's phone; `make backup` run once by hand and the file seen in Telegram; `make restore … DRY=1` on the file.
10. Legal: pd.gov.uz registration filed (owner); `docs/huquqiy-memo.md` §4 checklist signed off; managers briefed on `docs/sotuv-playbook.md` §2 (own account, 10:00–17:00, opt-out line).
11. Week 1 settings: `REVIEW_DAILY_CAP=20`, `PUSH_DAILY_CAP=10`, one manager in `MANAGER_TELEGRAM_IDS`; raise after the first weekly review.

### 7.2 Haftalik koʻrik (egasi uchun, dushanba 10:00, 20 daqiqa)

- `/holat` — qizil muammo bormi? Bor boʻlsa avval shu, keyin qolgani.
- `/xarajat` — oʻtgan hafta kuniga oʻrtacha necha dollar? $10 ga yaqinlashsa qaysi manba yeyapti?
- `/manbalar` — har manba boʻyicha aniqlik: 60 % dan past boʻlsa kamaytiring yoki toʻxtating; 20 % dan past boʻlsa agent oʻzi toʻxtatgan — sababini soʻrang.
- ERP: oʻtgan haftadagi agent lidlari — nechtasi 1 ish kunida bogʻlanildi? «Qayta qilinmasin» va «Notoʻgʻri raqam» nechta? Koʻp boʻlsa manbani kamaytiring.
- Menejerlar bilan: eng yaxshi 3 va eng yomon 3 lid — nega? Kartadagi «NIMA DEYISH» ishladimi?
- `/chats` — kecha signal bermagan chatlar; oʻlik chatlarni oʻchiring, yangi 1–3 ta chat qoʻshing (`/chats qoʻsh @…`).
- Telegram akkaunt: @SpamBot toza; FloodWait boʻlganmi (`/holat`)? Boʻlsa — bu hafta yangi chat qoʻshilmaydi.
- Zaxira: oxirgi zaxira Telegramga kelganmi; tiklash sinovi ✅ mi; disk necha GB boʻsh?
- Huquqiy: oʻchirish soʻrovi keldimi (`/unut` qilindimi); menejerlar faqat oʻz akkauntidan, 10:00–17:00 da yozdimi?
- Qaror yozib qoʻyiladi: qaysi manba kuchaytiriladi/toʻxtatiladi, kunlik push limiti, menejerlar roʻyxati.

## Open questions

1. `runtime_flags` and the `lead_reviews.kind` widening (§6): accepted into 02's `0001`, or a `0002` on day 2?
2. Owner review of **every** candidate in week 1 (`REVIEW_DAILY_CAP=20`) versus auto-push from day 1 for `hh_vacancies`/`exhibitors` (company-tier only, no person data) — recommended: review everything until the first precision numbers exist.
3. Manager-provided phones (`/tel`) use `legal_basis='pre_contract_request'`; is a phone the prospect typed to a manager in Telegram a pre-contract request under ЗРУ-547 Art. 18 (huquqiy-memo §5 q.1), or should it stay `public_source` with the chat URL?
4. Telegram Premium for the reader account ($5/month): buy, or stay at the free 500-chat cap given `USERBOT_MAX_CHATS=60`? Recommended: skip until a FloodWait pattern appears.
5. Healthchecks.io (or equivalent) free tier for `DEADMAN_PING_URL` — UNVERIFIED; alternative is a cron on the owner's laptop that curls nothing (no) or MIYA's worker pinging this one's heartbeat table (couples the two systems; not recommended).
6. Co-location with MIYA on one VPS: acceptable with separate projects (§3.5), or a second VPS from day 1?
7. Should `/qidir` be available to managers (shows suppression kind, which reveals «client of ours» for a phone)? Recommended: yes — that is exactly what stops a wrong call.
8. `QUIET_HOURS=21:00-08:00` for warnings; the owner may prefer MIYA's `23:30-07:30` so evening pushes' failures are seen the same night.

## Acceptance criteria

- `tests/test_bot_commands.py`: every command of §1.2 has a handler test with the fake bot; a non-owner message produces no reply; a manager id reaches only the manager surface; the bot leaves a group it is added to.
- `tests/test_bot_callbacks.py`: every `callback_data` built by `KEYBOARD_SAMPLES` parses, is ≤ 64 bytes and has a handler; 200 random strings and the listed near-misses parse to `None`; every handler key is producible.
- `tests/test_cards.py`: review card, manager card and digest render from synthetic views to the §1.3/§1.7/§1.8 templates; quote ≤ 140, card ≤ 3,500, apostrophes are ʻ/ʼ, a card for a suppressed or phone-less candidate is refused or switches to `[👤 Menejerga]`; after-tap footers match the table; a tap on an answered card changes nothing.
- `tests/test_manager_handoff.py`: least-loaded manager chosen; `/tel` writes a person-tier contact with `legal_basis`, `source_url`, `collected_at` and moves the candidate to `approved`; «Yozmang dedi» writes `suppression(dnc)`; no Telethon import in `bot/`.
- `tests/test_no_telegram_writes.py`, `test_join_only_in_joiner`, `test_imports_fence.py`, the `anthropic_guard` transport: green; a deliberately injected `send_message` in `userbot/main.py` and a raw phone in a prompt each turn the suite red (red proof documented in 07).
- `tests/test_health.py`: `problems()` returns the §4.2 keys for synthetic statuses with the stated severities; `alerts_due` repeats after `ALERT_REPEAT_HOURS` only, recovers once, holds warnings in quiet hours and never critical ones; `TOLD_FIRST_ELSEWHERE` keys are not said first by the sweep.
- `tests/test_backup.py`, `test_restore_drill.py`: no recipient → nothing written; a dump is produced and pruned on a scratch database; the drill compares counts and drops its database even on failure.
- `tests/test_logs.py`: each `RedactPatterns` row and each `REDACTED_EXTRAS` name is proven on a real handler.
- `tests/test_pseudonym.py`, `test_purge.py`, `test_suppression_precedence.py`, `test_pause.py`: golden masks; plan counts equal executed counts and the `purge_log` row holds only a hash; rank order; `/toxtat` stops collectors, pushes, cards and joins while the userbot keeps storing hits, and survives a worker restart.
- `tests/test_compose.py`, `test_dockerfile.py`, `test_env_example.py`, `test_requirements_pinned.py`: as specified in §3.
- `make doctor` on a fresh VPS with a correct `.env` prints no ❌; `make up` starts four containers; `/holat` answers within 60 s; the dead-man monitor receives its first ping within 5 minutes; a forced `docker stop worker` produces «⛔ Rejalashtiruvchi … jim» within 15 minutes and «tiklandi» once after `docker start`.
