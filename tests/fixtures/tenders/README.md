# Fixtures: tenders (uztender.com)

Phase-2 collector `tenders` of `docs/spec/03-collectors.md` §2.12 — entity solidity and goods
categories for companies already holding a STIR (enrichment, not discovery; no `sources` row yet,
03 §3). Observation `kind="company"`, `source_ref=f"uztender:{INN}"`.

**How it was recorded (provenance).** The CLI `python -m clientscrapper.tools.record_fixture` was
**not** run. A driver script that is not in the repo (build session scratchpad) called the function
the CLI calls, `clientscrapper.tools.record_fixture.record()`, once per URL with `scrub="on"`, inside
one `async with Fetcher(robots_dir=<scratch>/robots)` per stage. uztender.com went through **two**
stages, i.e. two `Fetcher`s (the full six-stage table is in `tests/fixtures/registries/README.md`):
stage 1 (driver run 15:06:32 UTC: the robots.txt rules check, started 15:06:41 UTC, then `/robots.txt`
recorded — response at 15:06:44 UTC) and stage 6 (driver run 15:10:22 UTC: `/supplier/306825083` — response at 15:10:24 UTC;
robots.txt read from the driver's on-disk cache, not fetched again). Every request went through
`services/http.Fetcher`: User-Agent `clientScrapperBot/0.1.0 python-httpx/0.28.1`, robots.txt read
first with the project's RFC 9309 parser, breaker armed. Pace 0.3 req/s (no `Crawl-delay` on this
host) held inside stage 1 by the `Fetcher`; between stage 1 and stage 6 (~3.5 min) no code enforced
it. No browser User-Agent, no extra headers, no cookies, no `/api/` call. To reproduce with the CLI:
one `record_fixture` call per URL (`--key tenders --name <file stem> --scrub on`), ≥ 4 s apart.

- **Date:** 2026-10-29 (Asia/Tashkent).
- **Recorded from:** build sandbox (US datacenter IP via egress proxy) — re-check from the VPS with
  `make smoke` before `sources.enabled`. The host is behind Cloudflare (`server: cloudflare`,
  `cf-cache-status`), which may answer another IP differently.
- **Scrubbed:** `--scrub on` (company-tier page; nothing person-shaped needed a second pass). The
  scrubber's rewrites in `uztender_supplier_306825083.html`, all false positives (no real contact was
  on the page): five label text nodes → `Seller 01`–`Seller 05` (listed under «Visible labels»
  below), and one **handle** rewrite inside an HTML comment, line 330: `<!-- Fonts are self-hosted via
  @namuna_01 (see src/styles/globals.css) …` — a font-package reference read as an `@handle` and
  replaced by the synthetic `namuna_01`. It is a developer comment, not data; a parser must not read
  it as a contact (the page has no contacts, see «Contacts»).

## Files

| File | Source URL | HTTP | robots.txt for this path | What it proves |
|---|---|---|---|---|
| `uztender_robots.html` | https://uztender.com/robots.txt | 200 | (the file itself) | robots.txt as served (`text/plain`, stored under the recorder's `.html` name): `User-agent: *`, `Allow: /`, Disallow `/app`, `/login`, `/register`, `/uz/app`, `/uz/login`, `/uz/register`, `/s/`, `/api/`; `Sitemap: https://uztender.com/sitemap.xml`; no `Crawl-delay` |
| `uztender_supplier_306825083.html` | https://uztender.com/supplier/306825083 | 200 | allowed (`*`: `Allow: /`, no Disallow matches) | one supplier profile, public, no login, no challenge: name, INN, sector, region, tax status, VAT payer, charter fund, registration date, contract count, total sum, customers, 12-month wins, recent contracts, category mix, regions, «debarred» |

STIR choice: `306825083` («MUQAYYAS GROUP» XUSUSIY KORXONA, a private enterprise — a company, not a
person) is the first `/supplier/{INN}` evidence URL in `docs/manbalar.json` (`[v 3]`).

### Requests made

| Host | Requests | Note |
|---|---|---|
| uztender.com | 3 | robots.txt 1 (rules) + robots.txt recorded 1; `/supplier/306825083` 1 |

No 403, 429, challenge or redirect.

## Day-1 answers (03 §2.12)

- **robots — VERIFIED:** `/supplier/{INN}` (and `/buyer/{INN}`, `/market`, the `/uz/` mirror) is
  allowed for `*`; `/api/`, `/app`, `/s/`, login/register are disallowed (`uztender_robots.html`, checked
  with `services/robots.parse_robots` for `clientScrapperBot`). No `Crawl-delay`; the spec's 1 req/3 s
  stands.
- **Served without captcha / login — VERIFIED** for this IP: 200, Cloudflare in front
  (`cf-cache-status: MISS`, `cache-control: public, max-age=0, must-revalidate, s-maxage=600`), no
  challenge markers. The page itself says «Профили поставщиков доступны на тарифе Pro»: the public page
  is the summary below; contacts and documents are Pro-only.
- **Where the data is — VERIFIED: a JSON state in the page, not the visible labels.** The page is a React
  SSR page; `<script>window.__RQ_STATE__={…}</script>` is a React Query dehydrated state whose
  `queries[0]` has `queryKey ["supplier-public","306825083"]` and `state.data.profile` with:
  `tin` «306825083», `name`, `sector` (OKED text «ДЕЯТЕЛЬНОСТЬ В ОБЛАСТИ АРХИТЕКТУРЫ»), `region`,
  `status`, `statusGroup` «ACTIVE», `regDate` «2019-10-31», `liquidationDate` null, `vatPayer` true,
  `charterFundUzs` «169083810», `totalContracts` **47**, `totalValueUzs` **6104154789**, `avgValueUzs`,
  `distinctBuyers` **7**, `firstAwardAt`, `lastAwardAt`, `awards24m`, `recentContracts[]`
  (`tenderId`, `slug`, `title`, `valueUzs`, `awardedAt`), `procurement12m` (`wins`, `valueUzs`,
  `medianDiscountPct`, `participations`, `winsWithDetail`, `medianParticipants`), `categoryMix[]`
  (`section` «71», `name`, `contracts`, `valueUzs`), `regionReach[]`, `debarred` null, `kgdFlags` [],
  `goodStanding` (`tier`, `label`, `signals[]`), `dxmapRateType` «A». So 03 §2.12's
  `contracts_count`/`total_sum`/`customers_count`/categories map to `totalContracts`/`totalValueUzs`/
  `distinctBuyers`/`categoryMix`. A second source in the same page: `script[type="application/ld+json"]`
  `Organization` with `identifier` (`propertyID` «INN») and a `Dataset.description` «47 госконтрактов на
  6,1 млрд сум, 7 заказчиков, 2022–2026».
- **Figures vs manbalar.json `[v 3]` — VERIFIED identical:** 47 contracts, 6.1 bln UZS, 7 customers,
  road/asphalt works.
- **Categories — VERIFIED shape:** `categoryMix[].section` is a two-digit division code (here «71»,
  «Архитектурные, инженерные и проектные услуги», a CPV-style division), so 03 §2.12's goods filter
  (equipment, spare parts, electronics, furniture, textiles, medical) is a set of `section` codes; this
  supplier is works/services only → no `entity_solid` by the spec's rule. Which codes are goods is
  STILL UNVERIFIED (one supplier seen; `/market` not fetched).
- **Unreliable-supplier flag — partly VERIFIED:** the profile has `debarred` (null here) and `kgdFlags`
  ([] here). What a debarred supplier's value looks like, and whether it mirrors the
  `xarid.uzex.uz` unreliable list (`[v 4]`), is STILL UNVERIFIED (no debarred example; xarid not touched).
- **Contacts — VERIFIED absent:** no phone, e-mail or person on the public page («Это ваша компания?
  Скрыть телефон или исправить данные» is an owner link); `tenders` gives solidity, not contacts.
- **Visible labels — do not parse them:** the scrubber rewrote the label text nodes it read as
  person names into `Seller NN` (false positives): `Seller 01` = the label beside «2019», `Seller 02`
  beside the contract counts «47»/«34», `Seller 03` beside the sums «6,1 млрд сум»/«5,5 млрд сум»,
  `Seller 04` beside the customer count «7», `Seller 05` = the footer link to `/guide`. The parser must read `__RQ_STATE__` (keys are stable
  and unscrubbed) and may use JSON-LD as a cross-check.
- **STILL UNVERIFIED:** an unknown INN's answer (404 vs a 200 with an empty state — no negative was
  recorded, the task allowed one supplier page); `/buyer/{INN}`; the `/uz/` variant; `/market`; the
  sitemap (it would list supplier URLs, i.e. discovery, which 03 §2.12 does not use).

## Refused / not recorded

- Nothing was refused by the site.
- **Not requested on purpose:** `/api/…` (robots: Disallow), `/app`, `/s/`, login, Pro features, other
  suppliers, `/market`, `xarid.uzex.uz`.

## Recorder ledger

- uztender_robots.html · 2026-10-29 · https://uztender.com/robots.txt · 70f47ec14d9a0f4c7428493eff4d89d1ed4fe6e7b1f20cfa8e6e579fa80f8150 · 200 · 384 · scrubbed: yes · uztender.com robots.txt (03 §2.12 robots UNVERIFIED)
- uztender_supplier_306825083.html · 2026-10-29 · https://uztender.com/supplier/306825083 · f091a3e96fc52a486742da37439a5bbc9047d1e2ed6e3195da2f15e6bb1fb553 · 200 · 55876 · scrubbed: yes · supplier page for STIR 306825083 (MUQAYYAS GROUP, manbalar.json [v 3] evidence URL)
