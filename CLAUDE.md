# clientScrapper — orientation for the implementing model

Read this first, then `docs/spec/00-overview.md`, then the spec sections in order.
Everything here was decided with the owner; do not re-litigate it, build it.

## What this is

**clientScrapper** is the client-finding agent for GSR Logistics (Imex services LLC,
Tashkent), a freight forwarder China → Uzbekistan. It discovers Uzbek B2B importers of
Chinese goods on the open internet (Instagram, OLX, Telegram, business directories,
hh.uz vacancies, exhibitor lists, maps, brand-dealer pages), resolves them to companies,
scores them, lets the owner review, and pushes leads into the company ERP where human
sales managers contact them. **The agent never sends a message to a prospect.**

The ERP is the repo **fullprompt** (GSR LOGISTICS ERP: Next.js 15, PostgreSQL 16,
Drizzle). ERP-side changes live on a separate branch there named `agent-feed`.
The older generic fork `LogisticsERP` is not the target.

## Who you are talking to

The owner, **Bekzod**. He writes in Uzbek — **reply in Uzbek**. He is not a developer:
explain in business terms (mijoz, lid, kub, optom, Xitoydan import), not tables and
functions. Ask numbered questions and offer options (a / b / c) with your recommendation
marked. He decides scope; if a request looks wrong, say so in one sentence, then build it.

## Binding decisions (2026-10-23)

| Topic | Decision |
|---|---|
| Discovery | Open internet is the primary source. Databases (the customs Excel in `TelegramCustoms`, the ERP) are for calibration, suppression and feedback only. The customs Excel has no importer or STIR column. |
| Threshold | A prospect qualifies at **≥1 m³ per single shipment** or any truck/container. B2C buyers are excluded. Cargo/forwarding competitors are never leads (their channels are seeds). |
| Budget | **≤ $10/day** for all paid APIs and tools, enforced in code (`costs.ask` before every paid call). A billing card with hard limits is attached to Google Cloud (Places API) and Brave. No purchase of customs-declaration datasets in v1. |
| Telegram | One **dedicated read-only account** (Telethon). It never writes, never DMs, never joins more than 3 chats/day. Managers' accounts are never used by the agent. |
| Split | Python collectors, resolution, scoring, review bot → this repo. Lead state, manager work, outcomes → the ERP. The agent pushes leads (`POST /api/leads/in/agent`) and reads two feeds back (`/api/agent/suppression`, `/api/agent/outcomes`). |
| Suppression | Phones in the ERP client book and leads closed with lost reason «Qayta qilinmasin» are never contacted again. The agent also keeps its own DNC (replies «yozmang», wrong number ×2) keyed by phone tail-9, Telegram username, Instagram handle, website domain, STIR. Checked before every card and every push. |
| Reminder | The ERP's 15-minute "untouched lead" reminder is disabled for agent-sourced leads. |
| Outreach | Human managers write from their own ERP-connected Telegram accounts, 10:00–17:00 Tashkent, one personalised message, opt-out line, no prices before density is known. See `docs/sotuv-playbook.md`. |
| Language | Owner-facing text (bot, cards, lead notes) in Uzbek Latin with ʻ/ʼ apostrophes. Code, comments, docs, commits in English. |

## Hard rules

- **Never** put an API key, bot token, session string or password in chat, in the repo or
  in a commit. They live only in the server `.env`.
- **Never** add a code path that sends a Telegram message, SMS, e-mail or call to a
  prospect. The AST test ported from MIYA (`tests/test_no_telegram_writes.py`) must stay
  green. There is no sender in v1.
- **Never** store passports/PINFL, Telegram member lists, full chat logs or
  customs-declaration rows. Person-tier data: at most one decision maker per company,
  with `legal_basis`, `source_url`, `collected_at`. Pseudonymise phones and handles before
  any model call.
- **Never** write the model identifier into a commit message, PR, code comment or any
  pushed artifact. Chat replies only.
- Respect every site's robots.txt and rate limits; prefer official APIs; never
  circumvent logins or CAPTCHAs; stop on 403/429 streaks (breaker).
- Work on the branch you were told to; commits end with the `Co-Authored-By` and
  `Claude-Session` trailers used in the history.
- For `fullprompt` changes: read its `CLAUDE.md` first and obey it (live system, backups,
  typecheck before push, migrations are hand-written SQL + journal entry, i18n in all
  locales). Keep the ERP change set minimal (`docs/spec/05-erp-integration.md`).

## Stack and layout

Python 3.12 · FastAPI (internal only, optional) · SQLAlchemy 2 async + psycopg 3 + Alembic ·
PostgreSQL 16 · APScheduler · aiogram 3 (owner bot) · Telethon (reader) · anthropic SDK
(structured outputs) · rapidfuzz · httpx · openpyxl · Docker Compose.

The skeleton is **copied, not imported**, from MIYA (`bekzodqodirov/dailyJournal`, package
`miya`): config, db plumbing, JSON logs with redaction, retry, Claude extraction, usage and
cost caps, person resolution (→ companies), text utilities, safety shields, health,
backups, client import, Telethon reader, owner bot, scheduler. `docs/kod-audit-miya.md`
lists each file and what to keep. Put a `# VENDORED_FROM dailyJournal@<sha> <path>` header
on every copied file.

```
clientscrapper/
  config.py  logs.py  db/  services/  collectors/  userbot/  bot/  worker/  tools/
tests/   alembic/   docker-compose.yml   Dockerfile   Makefile   .env.example
docs/spec/   ← the specification (source of truth for the build)
docs/        ← research, proposal, memos (background)
```

## Commands (to be created in T-01; keep these names)

```
make up            # docker compose up -d (db, init, worker, userbot, bot)
make migrate       # alembic upgrade head
make doctor        # config + db + telegram + crm reachability checks
make userbot-login # create the Telethon StringSession for the reader account
make test          # ruff + mypy (scoped) + pytest (needs postgres)
make backup / make restore FILE=…
make import-clients FILE=clients.xlsx   # suppression fallback before the ERP feed exists
```

## Where the truth lives

| Question | File |
|---|---|
| What are we building and why | `docs/spec/00-overview.md`, `docs/spec/01-requirements.md` |
| Tables, DDL, states | `docs/spec/02-data-model.md` |
| Each collector, endpoints, limits, keywords | `docs/spec/03-collectors.md` |
| Normalisation, resolution, Claude prompts, scoring, gates, budget | `docs/spec/04-processing.md` |
| ERP contract, fullprompt change set, push and feeds | `docs/spec/05-erp-integration.md` |
| Owner bot, manager handoff, deployment, ops, security | `docs/spec/06-bot-ops-security.md` |
| Tests, CI, definition of done, task list, day-1 checklist | `docs/spec/07-testing-and-plan.md` |
| Verified sources and evidence URLs | `docs/manbalar.json`, `docs/ochiq-internet*.md`, `docs/namuna-nomzodlar.md` |
| Legal constraints | `docs/huquqiy-memo.md` |
| How managers should contact leads | `docs/sotuv-playbook.md` |
| What to copy from MIYA | `docs/kod-audit-miya.md` |
| Why a line is the way it is | the comment beside it, and the test that pins it |

## How to work here

- Build in the order of `docs/spec/07-testing-and-plan.md` (T-01 …). Each task has
  acceptance criteria; a task is done when its tests pass and `make doctor` is green.
- Every collector is a pure parser over recorded fixtures plus a thin fetch layer. Record
  the fixture on day 1 (the spec marks what is UNVERIFIED and must be checked live).
- Every lead card and ERP note must cite evidence: URL + date + verbatim quote.
- Prefer the cheapest thing that works; the daily cap is the budget, not the estimate.
- Say plainly what you did not verify.
