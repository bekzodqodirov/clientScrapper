# Fixtures: registries (orginfo.uz)

Registry pages for enrichment step **E4 / E4′** of `docs/spec/04-processing.md` §3
(`services/registry.py`, «orginfo first, then stat/soliq/myorg» — task T-24 in
`docs/spec/07-testing-and-plan.md` §4.1) and the «Registry hosts» row of 07 §5.3. The
directory is named `registries` for now; 04 §3 stores this evidence under
`source_key='registry_lookup'` (a seeded `sources` row, 02 §2.1), so T-24 decides whether the
files move to `tests/fixtures/registry_lookup/`. Signals the parser feeds: `ent_legal_confirmed`,
`ent_age_2y_active`, `neg_liquidated` and `companies.registry_status/registered_on/oked/legal_form`
(04 §3 registry paragraph, 02 §2.7).

**How it was recorded (provenance).** The CLI `python -m clientscrapper.tools.record_fixture` was
**not** run. A small driver script that is not in the repo (it lived in the build session's
scratchpad) called the same function the CLI calls, `clientscrapper.tools.record_fixture.record()`,
once per URL with `scrub="on"`, inside one `async with Fetcher(robots_dir=<scratch>/robots)` per
**stage**. The fixtures for `registries`, `tenders` and `maps` were recorded in six stages, each its
own driver run with its own `Fetcher` (so its own in-memory per-host pace state):

| Stage | Driver run (UTC, 2026-10-29) | URLs (in order) |
|---|---|---|
| 1 | 15:06:32 | `orginfo.uz/robots.txt`, `uztender.com/robots.txt`, `2gis.uz/robots.txt` |
| 2 | 15:07:08 | `orginfo.uz/ru/organization/cf68f132a2f4/` |
| 3 | 15:08:00 | `orginfo.uz/ru/search/organizations/?q=310514454` |
| 4 | 15:08:37 | `orginfo.uz/ru/search/organizations/?q=xitoy` (default sort — recorded, then deleted, see «Scrubbing») |
| 5 | 15:09:41 | `…?q=xitoy&page=1&sort=-registered_at`, `…?q=xitoy&page=2&sort=-registered_at`, `orginfo.uz/ru/organization/000000000000/` |
| 6 | 15:10:22 | `uztender.com/supplier/306825083` |

Every request went through `services/http.Fetcher`: User-Agent `clientScrapperBot/0.1.0
python-httpx/0.28.1` (no `+mailto` part: `HH_USER_AGENT` is blank in the sandbox), robots.txt read
with the project's RFC 9309 parser (cached on disk in the driver's `robots_dir` after stage 1, so
later stages did not fetch it again), breaker armed. No browser User-Agent, no extra headers, no
cookies, no login, no form post, no `/api/` call. **Pace:** inside a stage the `Fetcher` held the
per-host pace, 0.3 req/s (`COLLECTOR_RPS_DEFAULT`; orginfo.uz sets no `Crawl-delay`), stamped at
send time. Between stages no code enforced it: the gap between the last orginfo request of one
stage and the first of the next was ≥ 30 s each time (it is the time between driver runs above),
which is wider than the pace, but by circumstance, not by the `Fetcher`. To reproduce with the
CLI: one `record_fixture` call per URL above (`--key registries --name <file stem> --scrub on`);
each call opens its own `Fetcher`, so keep ≥ 4 s between calls to the same host by hand.

- **Date:** 2026-10-29 (Asia/Tashkent), every file.
- **Recorded from:** build sandbox (US datacenter IP via egress proxy) — re-check from the VPS
  with `make smoke` before `sources.enabled`. orginfo.uz's robots.txt says load is limited «in
  nginx and CheckAccessMiddleware», so the VPS may be answered differently.
- **Scrubbed:** every file — `--scrub on`, then a second pass on the company page for the
  director/founder name the scrubber cannot see (section «Scrubbing»). Firm names, STIRs,
  addresses, OKED/OPF/SOOGU codes and dates are kept (company tier). All phones in the files are
  synthetic `9000000NN` shapes, all e-mails `namuna_NN@example.uz`, all handles `namuna_NN`,
  all person names `Seller NN`.

## Files

| File | Source URL | HTTP | robots.txt for this path | What it proves |
|---|---|---|---|---|
| `orginfo_robots.html` | https://orginfo.uz/robots.txt | 200 | (the file itself) | robots.txt as served (`text/plain`, stored under the recorder's `.html` name). The `*` group (the one that applies to `clientScrapperBot`) has 16 `Disallow` lines and no `Allow`: `/media/files/`, `/media/user-files/`, `/*.pdf$`, `/*.zip$`, `/*.rar$`, `/*.doc$`, `/*.docx$`, `/*/organization/*/pdf/`, `/*/trademark/*/pdf/`, `/*/organization/requests/`, `/*/user/`, `/*/blogs/create/`, `/*/blogs/my-posts/`, `/api/`, `/i18n/`, `/tgoiadmin/` — read the file, not this list, when a collector path is added. A second group for `YandexAdditional` / `YandexAdditionalBot` repeats the same 16 lines (its comment: those tokens do not obey `*`). No `Crawl-delay` (on purpose, per its own comment). SEO crawlers (`SemrushBot`, `AhrefsBot`, `MJ12bot`, `BLEXBot`, `DotBot`, `SeekportBot`, `Seekport Crawler`) get `Disallow: /`. Checked with `services/robots.parse_robots` for `clientScrapperBot`: every URL recorded here (and the deleted default-sort search) is allowed; `/ru/trademark/<id>/pdf/`, `/ru/organization/<id>/pdf/` and `/api/organizations/<id>/update-request/` are disallowed |
| `orginfo_organization_cf68f132a2f4.html` | https://orginfo.uz/ru/organization/cf68f132a2f4/ | 200 | allowed (`*`) | **per-company page**, server-rendered, no captcha, no login: official + short name, INN, registration date, status, registering body, OPF, SOOGU, OKED, charter fund, e-mail, phone, address, director, founders with shares, tax-committee and IT-park flags, stability rating, JSON-LD `Organization` with `taxID`. The `manbalar.json` evidence URL (OOO «ORGINFO», INN 310514454 — the site's own operator; chosen because it is the one company URL already in the docs) |
| `orginfo_search_stir_310514454.html` | https://orginfo.uz/ru/search/organizations/?q=310514454 | 200 | allowed | **search by STIR** (E4): exactly one card, linking to the company page's opaque id |
| `orginfo_search_name_xitoy_newest_p1.html` | https://orginfo.uz/ru/search/organizations/?q=xitoy&page=1&sort=-registered_at | 200 | allowed | **search by name** (E4′): 10 cards, newest registration first, three «Ликвидирована» badges; pagination block to page 39 |
| `orginfo_search_name_xitoy_newest_p2.html` | https://orginfo.uz/ru/search/organizations/?q=xitoy&page=2&sort=-registered_at | 200 | allowed | page 2 = 10 different cards → the pagination parameter is `page=N` |
| `orginfo_organization_missing_negative.html` | https://orginfo.uz/ru/organization/000000000000/ | 404 | allowed | negative: an unknown organisation id is a real 404 («Страница не найдена») |

Each file has its `<name>.headers.json` (status, final URL, response headers without cookies).

### Requests made (HTTP requests sent, robots.txt included)

| Host | Requests | Note |
|---|---|---|
| orginfo.uz | 8 | robots.txt 1 (rules) + robots.txt recorded 1; company page 1; STIR search 1; name search default sort 1 (not kept, see below); newest p1, p2 2; negative 1 |

No 403, no 429, no captcha or challenge page, no redirect. The breaker never opened.

## Day-1 answers (04 §3 «Registry hosts», 07 §5.3 «Registry hosts», manbalar.json orginfo entry)

- **Serves without captcha / login — VERIFIED** (every file): plain server-rendered HTML
  (Django-style, nginx), `200`, zero `captcha` markers; the only login walls are the
  «Таможенные склады организации» block («Для просмотра этой информации зарегистрируйтесь…») and the
  paid «Быстрое обновление» / «Скрыть номер телефона» services — none of them needed. So E4 can be
  automated on orginfo.uz; the manual `/stir` queue is the fallback only (from the VPS: re-check).
- **URL shape — VERIFIED, and it differs from the spec's «per-INN GET».** The company page is keyed
  by an **opaque 12-hex id** (`/ru/organization/cf68f132a2f4/`), not by the INN. The INN lookup used
  here is the **«organizations» tab** of the search, `GET /ru/search/organizations/?q=<INN>` → one card
  `a.og-card[href^="/ru/organization/"]` (`orginfo_search_stir_310514454.html`). That URL is a tab
  link on the results page; the search form itself submits to `/ru/search/` (the «all» tab, not
  recorded). The organizations tab was chosen because its name says it lists organisations only
  (company tier); what the «all» tab returns was not looked at. So E4 = **2 GETs**
  for a first lookup (search → card), 1 GET afterwards if the hex id is kept (04 §3 says «cache 90 d in
  `company_ids(id_type='myorg')`» — the orginfo id needs an `id_type` of its own). The only search
  form on the recorded pages is `<form action="/ru/search/">`, input `name="q"`, `minlength="3"`,
  placeholder «Введите STIR,
  название организации или бренда, имя учредителя…». Locales `/ru/`, `/uz/`, `/en/` (hreflang on
  the card; `/zh/` and `/ko/` in the language menu).
- **Search card fields — VERIFIED** (`orginfo_search_stir_310514454.html`,
  `orginfo_search_name_xitoy_newest_p1.html`, `…_p2.html`): `a.og-card` → STIR in
  `span.bg-success.bg-opacity-25` (first `<p>`), registration date `dd.mm.yyyy` after
  `img[alt=calendar]`, status badge `span.bg-danger` «Ликвидирована» only when liquidated (no badge =
  not liquidated), name `h6.card-title` (the official Uzbek name with legal form — the forms in
  the 21 kept cards: «"…" mas`uliyati cheklangan jamiyati» (16), «"…" xususiy korxonasi» (1, p1),
  «"…" mas`uliyati cheklangan jamiyati Qo‘shma korxona» (2, p2 — a joint venture, a second form
  word after the first), «Mas`uliyati cheklangan jamiyati "…" xorijiy korxonasi» (1, p2, form first),
  and «… UYUSHMASI» (1, p2, an association, no quotes); the legal form is not always last), address
  `p.text-body-tertiary`. The card alone already answers `registered_on` and `neg_liquidated`; OKED,
  OPF and the director need the company page.
- **Company page fields — VERIFIED** (`orginfo_organization_cf68f132a2f4.html`), each a
  `div.row` with the label in `div.col-6.text-body-tertiary > span` and the value in the next
  `div.col-6`, inside `section[aria-label="Основные сведения об организации"]`:
  «Официальное название организации», «Краткое название организации», «Дата регистрации»
  (`<time datetime="2023-05-27">`), «Статус» (badge text «Активный»), «Регистрирующий орган», «ИНН»
  (`span#organizationTinValue[content]`), «ОПФ» (`152 - Общество с ограниченной …` = legal form),
  «СООГУ», «ОКЭД» (`63110 - …` = activity code), «Уставный фонд» (`10 000 000,00 UZS`). Then
  `section[aria-label="Контактные данные"]`: e-mail (`a[href^="mailto:"]`), phone (`a[href^="tel:"]`,
  nine digits without `+998`), address (two `span`s); `section[aria-label="Управленческая информация"]`:
  «Руководитель» (a link to `/ru/search/managers/?q=<name>`); `section[aria-label="Учредители"]`:
  founder link `/ru/search/founders/?q=<name>` + share «100.00 %»; tax committee «Крупный
  налогоплательщик: Нет»; IT Park resident since a date; «Рейтинг стабильности» category (chamber.uz).
  The same facts are in the `application/ld+json` block: `mainEntity` `Organization` with `name`,
  `alternateName`, `taxID`, `identifier[]` (`STIR`, `Registration number`), `email`, `telephone`,
  `address`, `foundingDate`, `employee` (`Person`, `jobTitle` «Руководитель») and `founder[]` (`Person`)
  — the easiest parse target.
- **Director — VERIFIED present, and it is a person** (both the «Руководитель» block and JSON-LD
  `employee`): at most one decision maker per company with `legal_basis`, `source_url`,
  `collected_at` (CLAUDE.md hard rules, `docs/huquqiy-memo.md` item 1); scrubbed to `Seller 04` here.
  The founder list is person data too (a natural person founder) — not needed by 04 §3, do not store.
- **Status / liquidated — VERIFIED on the list, STILL UNVERIFIED on a card.** The list shows
  «Ликвидирована» (`span.bg-danger`) for 3 of 10 rows on p1; the one card recorded is «Активный». The
  text and class of a liquidated company's own «Статус» row were not seen (no liquidated card recorded).
- **Phones — VERIFIED server-rendered** on the card (`tel:` link + JSON-LD `telephone`). Not in the
  search list. Note the site sells «Скрыть номер телефона», and a registry phone is often the
  director's own mobile: treat it as person-tier until shown otherwise (04 §3 calls myorg's phone
  company-tier; for orginfo this is STILL UNVERIFIED — a legal call for T-24).
- **Search behaviour — VERIFIED:** 10 cards per page, `ul.pagination a.page-link` with
  `?q=…&page=N&sort=…` and a «Следующий» link; no total count is printed (39 pages for «xitoy»);
  sort values `default`, `active`, `-registered_at`, `registered_at`. The match is **fuzzy —
  VERIFIED** by one card: the query «xitoy» returned «"QODIRBOBO XIDOY ZAMINI"»
  (`…newest_p1`, card `/ru/organization/fbaf1f8262e7/`), so E4′'s «≥ 2 hits → none» and «name + city
  must agree» rules will often fire; the city is in the card's address. Whether a Latin query
  matches **Cyrillic** names is **STILL UNVERIFIED**: it was seen only on the default-sort page,
  which was deleted (see «Scrubbing»), and no kept file has a Cyrillic name card. Tabs on every
  search page:
  `all`, `organizations`, `founders`, `managers`, `ips` (individual entrepreneurs), `trademarks`,
  `phones` (reverse phone lookup), `auctions` — only `organizations` is company-tier; `founders`,
  `managers`, `ips` and `phones` are person lookups and must never be called by the collector.
- **Negative — VERIFIED:** an unknown id is a real 404 (`orginfo_organization_missing_negative.html`).
  A STIR search with no hit was not recorded (STILL UNVERIFIED: empty list vs. message).
- **Data age — VERIFIED what the page says, per block** (`orginfo_organization_cf68f132a2f4.html`).
  The registry facts (name, INN, status, OPF, OKED, contacts, director, founders) carry one
  **page-level** freshness note: `section[aria-label="Внимание"]`, a top-level section after
  «Учредители» (not inside «Управленческая информация»), `p.text-body-tertiary` «Приведенная выше
  информация актуальна на 15.06.2026. Более актуальную информацию вы можете получить на сайте stat.uz»
  (link to `registr.stat.uz`), followed by the «0 / 7 … Запрос на обновление информации» counter. So
  the card was **~4.5 months** old when recorded (15.06.2026 → 29.10.2026). Blocks from other sources
  carry their own date inside themselves: «Рейтинг стабильности … актуальна на 16.04.2025 … chamber.uz»,
  «Резидент IT-парка … 01.05.2025 … itpark.uz». JSON-LD `dateModified` (`2025-04-16T13:38:34…`) and
  the `Last-Modified` header (`Wed, 16 Apr 2025 13:38:34 GMT`) fall on the stability rating's day and
  **not** on the stat.uz date, so they do not measure how fresh the registry facts are: E4 should read
  the «Внимание» date as the card's freshness (and prefer a fresher host, the my.soliq.uz VAT check,
  for `registry_status` when it is old), not `dateModified`. One card seen: STILL UNVERIFIED whether
  every card has the «Внимание» note, and how quickly a liquidation reaches an existing card. Newly
  registered firms (08.10.2026) already appear in the search list (`…newest_p1`).
- **STIR checksum corpus (04 §1.5 `tools/stir_probe.py`):** search lists carry 10 STIRs per page with
  registration dates — a cheap corpus, but see «Scrubbing»: the recorder's scrubber rewrites STIRs
  starting with 20/33/50/55/77/88/9x, so the corpus must be read live, not from these files.
- **STILL UNVERIFIED (not requested, out of this task):** `registr.stat.uz`, `my.soliq.uz`,
  `myorg.uz` — the other three registry hosts of 07 §5.3; the `/uz/` and `/en/` page variants;
  whether the VPS IP is throttled by «CheckAccessMiddleware».

## Scrubbing

- `--scrub on` replaced: the company's phone (`9000000NN`), the site's own e-mail, the login form's
  placeholder e-mail and the site's Telegram link (`namuna_01`), and text nodes it read as names but
  are labels: the footer link to `/ru/objects/` on every page («Seller 01» on the search and 404
  pages, «Seller 03» on the company page), and on the company page «Seller 01» in the review block
  and «Seller 02» beside «0 / 7» (the update-request counter).
- **Second pass (company page only):** the director and the founder (one person, written four ways:
  upper case in a `<span>`, title case with `&#x27;`, URL-encoded in the two search links, and in JSON-LD
  `employee`/`founder`) → `Seller 04` everywhere (6 replacements), by a script that finds the names
  in the JSON-LD `Person` objects and the `/search/managers|founders/?q=` links and replaces every
  encoding; the README line's sha256 was rewritten through the recorder's own helpers. A grep for the
  surname, given name and patronymic in any case finds 0 across all files.
- **Scrubber defect found (not fixed here — a code change for the scrubber's owner):** nine bare
  digits that start with a mobile code (`_BARE_PHONE_CODES`: 20, 33, 50, 55, 77, 88, 90–99) and stand
  in an unlabelled element are rewritten as phones. On orginfo the STIR badge of a card carries no
  «ИНН» label, so every pre-2001 STIR (`20xxxxxxx`) became `9000000NN`. Hit here: the three «Похожие
  организации» cards at the foot of the company page (their badges read `900000002–4`; they are STIRs,
  not phones) and the `phones` tab link of the STIR search (`/ru/search/phones/?q=900000001` — the
  query was the STIR; `_PHONE_LABEL` reads «phones/?q=» as a phone label). The first name search
  (stage 4, default sort) came back with all 10 card STIRs rewritten and was **deleted**; it was
  re-recorded newest-first (stage 5), where every STIR is `31xxxxxxx` and survives. Nothing else said
  about that deleted page can be checked against a file, so no Day-1 answer above rests on it. Parser
  tests must not read the «Похожие организации» badges as STIRs.

## Refused / not recorded

- Nothing was refused by the site.
- **Not kept:** `/ru/search/organizations/?q=xitoy` (stage 4, default sort, 200) — its STIRs were
  destroyed by the scrubber (above); the newest-first pages replace it. That fetch is the one orginfo
  request beyond the Day-1 set (list p1, p2, one card, one negative, plus the STIR lookup E4 needs);
  orginfo.uz got 8 requests in all, inside the ~12 cap.
- **Not requested on purpose:** `/api/…` (robots: Disallow), `/ru/organization/<id>/pdf/` (Disallow),
  the person-lookup tabs (`founders`, `managers`, `ips`, `phones`), the login and the paid services,
  the «Таможенные склады» block behind login.

## Recorder ledger

- orginfo_robots.html · 2026-10-29 · https://orginfo.uz/robots.txt · 133a3855b27c35a93cbab0dce57d226b7a39c7ff0bccc9eed9719c744288f31d · 200 · 1590 · scrubbed: yes · orginfo.uz robots.txt (read before any page)
- orginfo_search_stir_310514454.html · 2026-10-29 · https://orginfo.uz/ru/search/organizations/?q=310514454 · 81c3c1877d0844bd7c0117a1b52dd870c733c87f4451051d1be6fd6468e58a31 · 200 · 53768 · scrubbed: yes · search by STIR (E4: registry by STIR) — the «organizations» tab of the search, /ru/search/organizations/?q= (the form itself submits to /ru/search/)
- orginfo_search_name_xitoy_newest_p1.html · 2026-10-29 · https://orginfo.uz/ru/search/organizations/?q=xitoy&page=1&sort=-registered_at · 32f1bcfec98394ae927f4189bf8f896b2974dd3f015966632779f6422a9913f8 · 200 · 69064 · scrubbed: yes · search by name, sort=-registered_at (newest first), page 1
- orginfo_search_name_xitoy_newest_p2.html · 2026-10-29 · https://orginfo.uz/ru/search/organizations/?q=xitoy&page=2&sort=-registered_at · 35c28c22a2b5655960640797ff408ec4042da0515c6ce270980b45228a8ac63c · 200 · 68653 · scrubbed: yes · search by name, sort=-registered_at, page 2 (pagination parameter page=N)
- orginfo_organization_missing_negative.html · 2026-10-29 · https://orginfo.uz/ru/organization/000000000000/ · f6252d85e1ccc9c24cf1186976ec15863864d57600dfac68cb70d8eec07f1ed1 · 404 · 47083 · scrubbed: yes · negative: organization id that does not exist
- orginfo_organization_cf68f132a2f4.html · 2026-10-29 · https://orginfo.uz/ru/organization/cf68f132a2f4/ · c73aad5d7d1e6768028772f809f57af0c857067fa7b4a3c234ffa120761ec01d · 200 · 127789 · scrubbed: yes · per-company page (the manbalar.json evidence URL: OOO ORGINFO, INN 310514454) (second scrub pass: 1 person name(s) → Seller NN)
