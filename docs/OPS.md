# clientScrapper — operations runbook

For whoever runs the agent on the VPS. The specification is the source of truth
(`docs/spec/06-bot-ops-security.md` §3–§4 and §7, `docs/spec/07-testing-and-plan.md` §3.2);
this file is the short, practical version of it, in the order you need it. Owner-facing
texts (the bot, the alerts) are Uzbek; this runbook is English.

Hard rules that hold in every step below: no secret (API key, bot token, Telethon session,
password, backup key) is ever pasted into chat, a commit or this repository — they live
only in the server's `.env` (mode 600) and, for the backup key, off the server too. The
agent never sends a message to a prospect: managers write from their own ERP-connected
Telegram accounts, 10:00–17:00 Tashkent (`docs/sotuv-playbook.md`).

## 1. Commands

| Command | Does |
|---|---|
| `make env` | creates `.env` from `.env.example` if it does not exist |
| `make doctor` | checks `.env`, the server and the database (exit 1 on any ❌) |
| `make up` | `env` → `doctor` → database + migrations → worker, userbot, bot (`SKIP_DOCTOR=1` skips the check) |
| `make migrate` | `alembic upgrade head` in the `init` container |
| `make reload` | recreates worker, userbot and bot after an `.env` edit (never re-runs `init`) |
| `make db-password-sync` | after `POSTGRES_PASSWORD` was changed in `.env` once the database existed: gives the database the new password |
| `make ps` / `make logs` / `make worker` / `make bot` / `make userbot` | status and logs |
| `make userbot-login` | one-time Telethon login of the reader account; prints `TELETHON_SESSION` for `.env` |
| `make backup` / `make backup-key` / `make backup-key-show` | an encrypted backup now / create the key once / print it for the password manager |
| `make restore FILE=… [DRY=1] [FORCE=1]` | restore a backup (`DRY=1` checks the file only); a bare file name — the `file` a backup's log line names — is looked for in `data/backups/` |
| `make import-clients FILE=clients.xlsx [APPLY=1]` | the ERP client export as suppression (the «Boshqa» fallback only) |
| `make erp-sync WALK=full\|delta\|outcomes\|quarter\|year [ACCEPT_SHRINK=1]` | one ERP feed walk by hand (exit 3 = skipped) |
| `make push-test` | pushes one visibly synthetic lead to the ERP |
| `make mvp-report [START=YYYY-MM-DD]` | the pilot's four MVP numbers (§4 below) |
| `make purge COMPANY=… \| PHONE=… \| TG_USER=… \| TG_HANDLE=@… [REASON=…] [APPLY=1]` | forget a subject (06 §5.4): the plan's counts first, nothing changes without `APPLY=1`; `REASON` is `legal_request` (the default, as `/unut`), `dnc`, `b2c` or `owner`; `TG_HANDLE` is `/unut @username`'s rule for a person kept under no contact (the ids the username was seen on) |
| `make smoke [FULL=1]` / `make smoke-cron` | the live smoke tests, VPS only (`docs/SMOKE.md`) / print the two crontab lines that run them |
| `make update` | backup (must succeed) → pull → rebuild → prune |
| `make test` | ruff + mypy + pytest (needs Postgres: `make db-test` prints the line) |

`CLAUDE.md`'s command table is held to the Makefile by `tests/test_claude_commands.py`.

## 2. Go-live checklist (06 §7.1)

1. VPS per 06 §3.5; `ufw` enabled; Docker installed; repo cloned; `make env` (written mode
   600); fill `.env`: a new `POSTGRES_PASSWORD` (`openssl rand -hex 24`, the same inside
   `DATABASE_URL` — every target that can start the database refuses `CHANGE_ME` in either: the
   database is created with whatever password the first start finds, so do not change it after
   step 2; if it was changed, `make db-password-sync` gives the database the new one — the doctor
   says «Baza parolni qabul qilmadi»), `ANTHROPIC_API_KEY`, `ASSISTANT_BOT_TOKEN` and
   `OWNER_TELEGRAM_ID` (step 4's BotFather part and the owner's `/start` to the bot come first —
   the doctor asks Telegram whether the bot can reach the owner), `TELETHON_API_ID` and
   `TELETHON_API_HASH`; `chmod 600 .env`.
2. `make backup-key`: it creates and migrates the database first (`Running upgrade … ->
   0004_directories_topuz`); a failed migration stops it with «Baza tayyorlanmadi …» before any
   key is made. Store the secret key off the server in two places; `BACKUP_AGE_RECIPIENT` set in
   `.env`. Run twice, it refuses («allaqachon bor») and make adds its own `Error 1` line.
3. `make doctor` green; `make up` (its own doctor ends «make up davom etadi»; on its first start
   the worker takes a catch-up backup and sends the file to the owner); `make ps` shows the
   database healthy and worker, userbot and bot healthy — a container counts only a heartbeat its
   own process wrote, so «health: starting» for the first two minutes after `make up` or
   `make reload` is normal. A bot that cannot start (token empty or refused, no owner id) says
   why once in `make bot` and stays up, unhealthy — never a restart loop. `/holat` from the
   owner's account answers. Until step 5's `make reload` the reader idles and `/holat` says
   «Telegram oʻquvchi akkaunti hali ulanmagan» — that is expected, not a fault.
4. BotFather: the bot exists, group joining is disabled; the owner and every manager have sent
   `/start` to the bot; `make doctor` shows each manager id reachable.
5. Reader account: a SIM at least 30 days old, 2FA on, `make userbot-login` (it never touches
   the database). Every prompt and line is the tool's own, in Uzbek: Telethon's English ones
   («Signed in successfully as …», «Invalid code», «Invalid password») are gone. Only on a bad
   line to Telegram do Telethon's English connection warnings («Attempt 1 at connecting failed:
   …») still come before the Uzbek sentence.
   - The number **with its country code**: `+998901234567`, or `998 90 123-45-67` (taken as
     +998…). A number without it is refused and says why: Telegram reads «901234567» as +90…
     (Turkey) and sends the code to somebody else's number. A bot token is refused too.
   - «Kod soʻralmoqda: +99890*****67» — the number it asked for, masked; check the country and
     operator code here.
   - Then where Telegram says the code went. «Telegram ilovasiga keldi» means the code is
     **not** an SMS: it is in the chat named «Telegram» in the Telegram app where this number is
     already logged in (the reader phone, or a computer). Otherwise SMS, a phone call, the
     account's login e-mail (its pattern is shown) or fragment.com. When Telegram can send it
     another way the next line says which and after how many seconds; typing `qayta` at the code
     prompt asks for it again that way (and asks for a fresh code after «Kodning muddati
     tugagan»).
   - The code is digits (spaces are fine); three wrong codes or three wrong 2FA passwords end
     the login with exit 1.
   - If Telegram first wants a login e-mail on the account, the tool says so and stops: open the
     reader account in the official Telegram app and set it (or log in there once), then run the
     login again.
   - Every refusal is one Uzbek sentence and exit 1, never a traceback: a flood wait (in
     seconds, minutes or hours — wait it out; trying sooner makes it longer), too many codes for
     this number, an invalid or banned number, no Telegram account on the number (open one in
     the official app; the tool never signs up), wrong `TELETHON_API_ID`/`TELETHON_API_HASH`
     (re-copy from my.telegram.org), a published api id, every way of sending the code used up,
     a lost connection. Anything else names its class: send that name to the developer.
   Then replace the whole `TELETHON_SESSION=` line in `.env` with the printed one (the doctor
   refuses a doubled «TELETHON_SESSION=» or a cut paste), `make reload`; the ⛔ reader line
   leaves `/holat`; `/chats` lists the three seed chats within 24 h.
6. ERP: fullprompt `agent-feed` deployed (the build stamp at `/api/version` is newer than the
   merge — there is no migration, so a migration count proves nothing); the owner's five steps of
   05 §A.14 done (key minted, «Qayta qilinmasin» in the lost-reason dictionary, routing rules A
   and B, `kub` mapped after the first test push, `agent_no_contact_reasons` matching the label);
   `make push-test` → 200 (a new `external_id` each run); `CRM_PUSH_ENABLED=true`; the first delta
   pull shows a non-zero suppression count in `/holat`; the read-only count of client cards whose
   `phones` is not a JSON array is known (07 §5.3). The owner's decision 4a (01 §7): the pilot
   starts only after `agent-feed` is deployed — no fallback pushes.
7. Fallback only if `agent-feed` is not deployed (not the plan): the same count first;
   `CRM_PUSH_SOURCE=other`; `make import-clients FILE=clients.xlsx APPLY=1` (exported from an
   unfiltered client screen); `PUSH_DAILY_CAP=10`.
8. Billing: Anthropic monthly limit $150 with an e-mail alert at $100; Google Cloud budget
   $5/month with a hard cap; Brave $5 credit only (the code side is 32 queries/day and ≤ 1,000 a
   month in `costs.ask`); Apify prepaid ≤ $20. The code-side cap is `DAILY_COST_CAP_USD` ($10).
9. `DEADMAN_PING_URL` configured and a test alert received on the owner's phone; `make backup`
   run once by hand and the file seen in Telegram; `make restore FILE=<file> DRY=1` on that file
   (`<file>` = the `file` of `make backup`'s «backup written» line; the listing ends «dry run:
   nothing was written»).
   The smoke cron installed once (`docs/SMOKE.md`: `make -s smoke-cron` into the crontab); the
   digest says nothing about smoke before its first run, so `crontab -l` is the check.
10. Legal: pd.gov.uz registration filed (owner); `docs/huquqiy-memo.md` §4 signed off; managers
    briefed on `docs/sotuv-playbook.md` §2 (own account, 10:00–17:00, opt-out line, press
    «📞 Bogʻlandim» or move the stage after the first message — decision 6a).
11. Week-1 settings: `REVIEW_DAILY_CAP=20`, `PUSH_DAILY_CAP=10`, one manager in
    `MANAGER_TELEGRAM_IDS`; raise after the first weekly review.
12. The directories, the exhibitor lists and the registry (wave 5) — every one of these rows is
    seeded **off**, and none runs until it is switched on. First, from the VPS: `make smoke`
    (`tests/live/test_directories_live.py`, `tests/live/test_exhibitors_live.py`) and the
    «Registry hosts» check of 07 §5.3; record the seeds the fixture READMEs list as owed. Then
    on `/manbalar`, one a day while `/holat` stays quiet: `directories` (the schedulable parent)
    **and** each site's own row — `directories_goldenpages`, `directories_yellowpages`,
    `directories_glotr`, `directories_topuz` (the fan-out reads a site only while its row is on
    and not paused; with the parent alone on, nothing is crawled); `directories_prom` stays off
    (no parser: Cloudflare answers 403). `exhibitors` (the calendar is the code's
    `DEFAULT_CALENDAR` until `sources.exhibitors.config.calendar` is written; correct the
    organiser figures when the new ones are published). `registry_lookup` (enrichment's E4/E4′
    on orginfo.uz; while it is off E4 says `skipped`, and an owner's `/stir` waits for it).
    `enrich_site` (company sites and `t.me/s/` pages) is seeded on.

## 3. Incidents (06 §4.6)

The bot names every problem with its remedy (06 §4.2); `/holat` lists the open ones.

| Incident | Signs | Steps | Verify |
|---|---|---|---|
| Reader account restricted | `userbot_unauthorized`; `/holat` oʻquvchi ❌ | (1) On the phone with that SIM open @SpamBot, read the status, appeal if offered. (2) `USERBOT_ENABLED=false`, `make reload` — collectors continue. (3) A temporary limit: wait it out, then `make userbot-login`, new `TELETHON_SESSION`, `make reload`. (4) Banned: standby SIM → `make userbot-login` → `.env` → `make reload`; reset `chat_monitors.joined_at` so the joiner re-joins three a day by priority (06 §4.6's `tools/chats.py --rejoin` is not built yet: by hand in `make psql`). (5) Review what tripped it: joins per day, backfill size, `GetFullUser` count | `/holat` oʻquvchi ✅; a first `interactions` row within 1 h |
| ERP key rotated / 404 | `push_404`; pushes and feeds `failed` | New key from **Kelgan arizalar → Reklama havolalari → Agent**; `.env` `CRM_AGENT_KEY`; `make reload`; `/yoq crm`; the next hourly delta sees a new `salt_id` and a full pull runs; pushes stay refused (`suppression_stale`) until it finishes | `/holat`: roʻyxat toʻliq a few minutes ago; `make push-test` 200 |
| ERP redirect | `erp_redirect` (a 3xx) | `CRM_BASE_URL` must start with `https://`; `make reload`; `/yoq crm` | the next push or feed answers 200 |
| Cost cap hit | `cost_cap` | Nothing to do today: `costs.ask` refuses, collectors keep the free part, signals queue. Tomorrow: `/xarajat` → which operation; lower that source's `daily_cost_cap_usd` via `/manbalar`, or raise `DAILY_COST_CAP_USD` only with a matching provider-side limit | spend < 80 % by noon next day |
| Disk full | `disk_low` | `du -sh data/evidence data/backups`; `docker system df`; `make backup` must still work; lower `EVIDENCE_MAX_GB`; `docker image prune -f`; never delete backups newer than 3 days | free ≥ 2 × `DISK_MIN_FREE_GB` |
| Duplicate pushes suspected | a manager reports two leads for one company | Read `push_log` for the company ordered by `sent_at`: one `sent` per month per company is correct, two months with new signals are correct by design, two `sent` in one month are impossible (`ux_push_log_delivered`). Two leads in the ERP came through two doors (`other` fallback + `agent`): delete the `manba=other` route and ask the manager to join them. One lead with «🔁 Qayta yozdi» but no «🆕» is the ERP's orphan after a 503 half-way — not a duplicate | `push_log` matches `outcomes`; `push_unconfirmed` clear |
| Worker silent | `worker_silent` | `make worker` → traceback; usually a migration or a job failing at start; roll back = `git checkout <prev>` + `make up` + `make restore` only if a migration broke data | heartbeat fresh; `/holat` |
| `make push-test` refused after a purge | `make push-test` answers a suppression refusal | The TEST company was purged with `legal_request` (or `dnc`): its synthetic phone is now suppressed for good, so every later push-test refuses. Purge the TEST company with `REASON=owner` instead — the purged holder frees the synthetic STIR and domain and the next push-test mints a fresh one | `make push-test` 200 |
| Backup failed | `backup_failed` | `make backup` by hand → the error: pg_dump major version (rebuild the image), disk, missing recipient | `backup` beat `ok=true`, the file in Telegram |
| «Qayta qilinmasin» renamed in the dictionary | a seller says a «never again» lead came back, or no new `lost` rows for weeks | A rename never rewrites old leads, but new losses under the new spelling are not in `agent_no_contact_reasons` and arrive with `no_contact=false`. **Boshqaruv → Sozlamalar → `agent_no_contact_reasons`**: add the new spelling (`;`), keep the old one; then `make erp-sync WALK=full` | the next full pull holds the new losses as `kind='lost'` |
| Feed shrank | `suppression_shrunk` | Nothing was deleted. Check the ERP: a client export change, a renamed `agent_no_contact_reasons` label, a retired class of tails? Fix the cause, then `make erp-sync WALK=full ACCEPT_SHRINK=1` once | the full pull's count ≥ 90 % of the previous one |
| A lost reason the agent cannot class | `/holat`: «⚠️ Sinflanmagan yoʻqotish sabablari: «…» N ta» | The owner added a dictionary label no `outcomes.LOST_REASON_PATTERNS` entry matches (decision 1a): it counts as neutral until mapped. Tell the developer the exact spelling; the pattern is added in code with a test (`tests/test_outcomes_labels_table.py`); the next `quarter`/`year` walk re-labels the old rows | the line disappears from `/holat` |
| A source paused for low precision | «⚠️ {manba} aniqligi N% — bir haftaga toʻxtatildi» | The weekly job paused it (≥ 30 pushes, < 20 % precision, 06 §4.2). Ask why (wrong keywords, a B2C channel); fix the source's config, then `/manbalar` → tap its ⏸ row once — that ends the pause (the source stays on) and takes it out of the alert; otherwise it resumes by itself after 7 days and is judged again next Sunday. A source switched off is never paused or named | `/manbalar` shows ✅ |

## 4. The weekly precision job and auto-push

`source_precision` runs Sunday 04:00 Tashkent, after the Sunday `quarter` outcomes walk (03:20):

- every source's 30-day numbers are refreshed: `precision_30d = positives / (positives +
  negatives)` over outcomes first read in the last 30 days (pending and neutral outcomes are no
  evidence), `pushes_30d` = live delivered pushes sent in the last 30 days;
- `auto_push` is turned **on** at `pushes_30d ≥ 15 AND precision_30d ≥ 0.60` and **off** below
  either — both ways, every week (05 §B.2). It is **never on** while the owner forbade it
  (`/manbalar` → ℹ️ → «🚫 Avto-pushni taqiqlash», `config.auto_push_forbidden=true`): the owner can only forbid,
  never grant; the update that turns it on re-reads the flag under the row's lock;
- a source with `pushes_30d ≥ 30` and `precision_30d < 0.20` is paused for 7 days and the owner
  told once per source per week (`source_low_precision`);
- the run's flips are kept (`runtime_flags.precision_flips`) for the digest's «a flip is its own
  line» (T-35).

Run it by hand only if a Sunday was missed: `docker compose run --rm --no-deps worker python -c
"import asyncio; from clientscrapper.worker.main import source_precision_job;
print(asyncio.run(source_precision_job()))"`.

What a flip **does** is the owner's switch `AUTO_APPROVE` (`.env`, default `false`; 06 open
question 2's «review everything until the first precision numbers exist», T-39):

- `AUTO_APPROVE=false` — `auto_push` approves nothing. Every hot candidate still reaches the
  owner's card; `/manbalar` → ℹ️ says «aniqlik yetarli, lekin hali yoqilmagan» for a source that
  earned it, and the source's mode stays «qoʻlda».
- `AUTO_APPROVE=true` (then `make reload`) — from the moment the Sunday job turns a source's
  `auto_push` on, its hot candidates are approved as `auto:<source>` and pushed **without an
  owner card**; `/manbalar` and `/holat` say «avto». The owner is told of a flip in Telegram on
  the first health sweep after `QUIET_HOURS` (the job runs at Sunday 04:00, inside them;
  `erp_feeds.tell_flips`, once per week's flips), and Monday's digest has the flip as a line —
  with «AUTO_APPROVE oʻchiq — bu manbalarning kartalari hali ham sizga keladi» while the switch
  is off. Forbid a source on `/manbalar` (ℹ️ → «🚫 Avto-pushni taqiqlash») to keep its cards.

## 5. The pilot's numbers (07 §3.2) — `/holat` and `make mvp-report`

`/holat` shows them over the last 30 days; `make mvp-report [START=YYYY-MM-DD]` over the pilot
(default start: the day of the first push; week 2 = days 8–14). One home for both:
`services/metrics.py`.

| Number | Target (MVP) | How it is counted |
|---|---|---|
| Pushes with a phone and score ≥ 60, week 2 | ≥ 20 | one per `external_id` (a re-push is the same lead), dated by the row its outcome is about — a re-push of a lead the ERP never recorded is dated by the re-push; `make push-test`'s synthetic lead is not counted |
| Real importer rate | ≥ 60 % | over pushes whose outcome is terminal (won, lost, client, dropped) or ≥ 14 days old: 1 − (not importer, wrong number, competitor, small) / labels that are not `dropped_*`; the strict precision (positives / (positives + negatives)) is printed beside it |
| Contacted within 1 business day | ≥ 40 % | `contacted_at` by the end of the next Mon–Sat Tashkent business day after the push, over the leads the ERP created whose deadline has passed (or that were contacted in time). **Under-counts**: the ERP sees calls, connected Telegram accounts, notes, stage moves and «📞 Bogʻlandim» only (decision 6a) |
| Claude spend | ≤ $5/day | the most expensive Tashkent day; all providers together are held to `DAILY_COST_CAP_USD` ($10) |
| Quotes sent / contacted | ≥ 25 % (01 §5) | **approximate** (decision 5a): the lead's current ERP stage name contains a fragment of `QUOTE_STAGE_NAMES` (default `narx`, `;`-separated, case and spacing ignored) or the stage is won; a lead that passed «Narx aytildi» and moved on is not seen. To correct the list: edit `QUOTE_STAGE_NAMES` in `.env`, `make reload` |
| First shipment / pushed | ≥ 3 % (01 §5, 60-day lag) | **proxy** (decision 3a): `won` (or a fallback `landed_client`) — the ERP sends no receipt, kub or shipment yet; an ERP round after the pilot adds the first receipt's date and m³ |

A number that depends on the managers (contact rate) failing while the agent's own numbers pass
is the owner's conversation with the managers («menejer javobi»), not a code fix.

## 6. Suppression the outcomes write (02 §2.11)

- `won` / `landed_client` close the company on **every** key it is known by — the pushed phone
  tail, its Telegram usernames, Instagram accounts, domain and STIR (decision 2a: the ERP checks a
  client by phone only). A lead moved back from won takes them all back; while the label stands,
  the rows stay even if a contact is anonymised or a handle moves.
- «Qayta qilinmasin» (`lost_no_contact`) closes the same keys as `lost`.
- Old ERP clients stay phone-only (the feed carries nine-digit phone hashes); every manager card
  says «Yozishdan oldin ERPda nomi boʻyicha qidiring».

## 7. Backups and restore (06 §4.4)

- Nightly at `BACKUP_TIME`: `pg_dump -Fc | age` into `data/backups/`, pruned after
  `BACKUP_RETENTION_DAYS`, the file sent to the owner's Telegram (ciphertext, ≤ 45 MB pieces).
  No recipient → nothing is written (`backup_unconfigured`).
- Weekly drill (`RESTORE_DRILL_DAY` 05:00): the newest file restored into a scratch database and
  the row counts compared; `restore_drill_failed` when they do not match.
- Monthly by hand, on a scratch VPS or laptop: `docker compose up -d db` →
  `make restore FILE=… DRY=1` → `make restore FILE=…` → `make up` (`FORCE=1` only on a non-empty
  database; a bare file name is looked for in `data/backups/`). Losing the backup key loses every
  backup.

## 8. Weekly review (06 §7.2, the owner, Monday 10:00)

`/holat` (red problems first, then the numbers of §5), `/xarajat` (last week's daily average),
`/manbalar` (precision per source: below 60 % reduce or stop it; below 20 % the agent already
paused it), the ERP's agent leads (contacted within a day? «Qayta qilinmasin» and wrong
numbers?), the managers' best and worst three leads, `/chats` (drop dead chats, add one to
three), the reader account (@SpamBot, FloodWait), the last backup and drill, deletion requests
(`/unut`). Write down the decisions.
