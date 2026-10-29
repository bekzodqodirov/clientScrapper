# Fixtures: exhibitors

Recorded with `python -m clientscrapper.tools.record_fixture … --key exhibitors --scrub on`
(07 §1.1): every request went through `services/http.Fetcher` — the project's own
User-Agent (`clientScrapperBot/<VERSION> … python-httpx/<v>`), robots.txt asked first (on
`catalog.automechanika.uz` it could not be read for the first two pages — see the robots
note), at least 3 s between two requests to one host, no extra headers except the recorder's own
`--json` (`Accept: application/json`). No 403, 429, captcha, JS challenge or login was met
on the way to any file kept here; nothing was routed around.

**Every file below:** date **2026-10-29**; recorded from **build sandbox (US datacenter IP
via egress proxy) — re-check from the VPS with `make smoke` before `sources.enabled`**.

**robots.txt.** Each recorder call is a new process; the Fetcher reads robots.txt from its
disk cache (24 h) and asks the host only when no fresh entry is there, and it writes that
cache only on a 200 or 4xx answer. Six hosts were asked once, on their first request (the
cache entry's `fetched_at` equals the first `requests.log` line of each host).
`automechanika.uz` — `User-Agent: *` with ~80 `Disallow` lines for Tilda service paths and
the book/press/news/service pages, none of which is a list page, no `Crawl-delay`.
`catalog.automechanika.uz` — Joomla defaults (`/administrator/ /components/ /modules/ …`
disallowed, `/en/expositions/…`, `/en/companies/…` and `/?…` allowed), no `Crawl-delay`;
this host was asked **three** times: the calls for `automechanika_catalog_2026_p1.html`
(14:44:53Z) and `automechanika_catalog_2026_p2.html` (14:45:49Z) each got no answer the
Fetcher could cache (a network error, 5xx or 429 — no cache file was written, and which of
the three it was is not recorded), so **both pages were fetched under the Fetcher's
in-memory "unreachable → allow for 1 h" rule, before any robots.txt of the host had been
read**; the third call (`automechanika_catalog_robots.html`, 14:46:20Z) got a 200 and
cached it, and every later call on the host used that cache. Checked afterwards against
that file, both earlier paths are allowed. `uzbuild.uz`, `power-uzbekistan.uz`,
`caitme.uz`, `mebelexpo.uz`, `textileexpo.uz` — `User-agent: * / Allow: /`, no
`Crawl-delay`. The verdict column below is
`services.robots.parse_robots(<cached file>, "clientScrapperBot").allows(url)`, computed
after the recording.

Requests on the wire (pages + robots + retries): automechanika.uz 4 ·
catalog.automechanika.uz 11 · uzbuild.uz 12 · power-uzbekistan.uz 6 · caitme.uz 6 ·
mebelexpo.uz 6 · textileexpo.uz 6.

## Files

Status is the HTTP status the Fetcher got. «from» = the sandbox line above.

### Automechanika Tashkent (BMCA / Messe Frankfurt) — `automechanika.uz` (Tilda) + `catalog.automechanika.uz` (expodat, Joomla `com_expo`)

| File | Source URL | Status | robots | from | What it proves |
|---|---|---|---|---|---|
| `automechanika_2026_list_en.html` | https://automechanika.uz/exhibitorslisteng | 200 | allowed | sandbox | The spec's list URL carries **no rows and no `t-store`/`data-*` JSON**: Tilda records 121/396/33/191 only — a heading and three buttons. «Exhibitors List 2024» → `/exhibitors-eng2024`, «Exhibitors List 2025» → `https://catalog.automechanika.uz/en/expositions/exposition/6400`. The page already advertises the next show (20–22 Oct 2027). |
| `automechanika_participants_en.html` | https://automechanika.uz/eng/participants | 200 | allowed | sandbox | The menu target of «Exhibitors List»: its one button «Exhibitors Catalogue 2026» → `https://catalog.automechanika.uz/en/expositions/exposition/7012`. **This is where the 2026 rows live.** (`<title>` was over-scrubbed to «Seller 01».) |
| `automechanika_2024_list_en.html` | https://automechanika.uz/exhibitors-eng2024 | 200 | allowed | sandbox | 2024 list = a Tilda **Feed** block (record type 915, `t_feed_init('1221168096', {feeduid:'327968451841', amountOfPosts:'12', btnText:'More'})`) plus a site-search block (838): the rows are fetched by JS from the Tilda feeds API and are **not in the HTML**. |
| `automechanika_catalog_2026_p1.html` | https://catalog.automechanika.uz/en/expositions/exposition/7012 | 200 | allowed (checked afterwards; fetched before robots.txt was read — see robots note) | sandbox | Server-rendered list, **48 rows**: 41 China, 5 Uzbekistan, 1 UAE, 1 Saudi Arabia. Country filter options (21 countries, Uzbekistan = 345), product-index filter (20 rubrics), show filter (Automechanika / Comtrux / Bautrux). Pager `.pagination` with `data-href=…?start=N`. |
| `automechanika_catalog_2026_p2.html` | https://catalog.automechanika.uz/en/expositions/exposition/7012-automechanika-tashkent-comtrux-tashkent-bautrux-tashkent-2026.html?start=48 | 200 | allowed (checked afterwards; fetched before robots.txt was read — see robots note) | sandbox | Page 2 = rows 49–96: 48 rows, **no overlap with page 1**, alphabetical continuation (page 1 ends «Changzhou Jt Turbo…», page 2 starts «Changzhou Liuxi…»). `start` is a row offset; the page size is 48 (the infinite-scroll script's `lim = '48'`), while the pager widget itself counts in 8s (its active item reads «7» here). |
| `automechanika_catalog_2026_last.html` | …-2026.html?start=456 (the pager's «End» link) | 200 | allowed | sandbox | 6 rows («Grass» … «JV UzEraeAlternator LLC»: 4 Uzbekistan, 1 Russia, 1 Kazakhstan) → **total = 456 + 6 = 462 rows**. Titles carry HTML entities (`JV LLC &quot;UZCHASYS&quot;`): unescape them. |
| `automechanika_catalog_2026_past_end.html` | …-2026.html?start=504 | 200 | allowed | sandbox | **NEGATIVE / paginator stop**: 0 rows, `#scroll_list` says «no results found», still HTTP 200. A parser must return `[]` and the paginator must stop on it. |
| `automechanika_catalog_company_uz.html` | https://catalog.automechanika.uz/en/companies/company/150345-exzap.html | 200 | allowed | sandbox | One Uzbek exhibitor's card: `.company_img_country` (flag + «Uzbekistan»), `.company_stand_num` (AM328), `.company_contacts` (address), `.company_site a[href]` (website), `.company_email`, `.company_phone` (server-rendered; scrubbed), `.chanels-btn a` (Telegram/YouTube, scrubbed), `.rubr_cat .rubr` (show + product index). Scrub artefact: the firm's own YouTube handle equals its name, so «EXZAP» reads `namuna_02` in this file (the list row in `…_p2.html` keeps «EXZAP»). |
| `automechanika_catalog_2025_p1.html` | https://catalog.automechanika.uz/en/expositions/exposition/6400 | 200 | allowed | sandbox | **NEGATIVE / list removed**: the 2025 catalogue (organiser's 489) is gone — final URL `https://catalog.automechanika.uz/automechanika-main.html`, «Ошибка — The event was not found» over the exhibitors' personal-cabinet login page («Авторизация»). HTTP 200 with zero rows: the parser must detect it (final URL ≠ the exposition, no `#scroll_list`). Nothing behind the login was tried. |
| `automechanika_catalog_robots.html` | https://catalog.automechanika.uz/robots.txt | 200 | always allowed | sandbox | The catalogue host's robots.txt as served (text/plain, stored by the recorder as .html), kept because the Fetcher's first two reads of it failed and two pages were fetched before it was read (robots note above). |

### UzBuild (Iteca) — `uzbuild.uz` (ERA CMS)

| File | Source URL | Status | robots | from | What it proves |
|---|---|---|---|---|---|
| `uzbuild_list_en.html` | https://uzbuild.uz/en/exhibitors-list | 200 | allowed | sandbox | The list page is a **DataTables shell**: `<table id="ERADataTable">` with only `<thead>` (Company · Country · Stand № · Location · Favourite), filters (company text, country, product categories, location), the edition select — raw markup `<select class="form-select single-select-field custom__select" onchange="ChangeYear(this);" data-placeholder="Archive">`, so select it with `select[onchange^="ChangeYear"]` (an exact `[onchange="ChangeYear(this)"]` matches nothing because of the trailing `;`); its **first `<option>` has no `value`** (the «Archive» placeholder, skip it), the others carry `value` = exhibition id (401 = 2024, 430 = 2025, **461 = 2026**, 490 = 2027 `selected`) — and `var dataTableURL = 'ERAForms/companies_list.php?l=en&exhibition=490'` resolved against `<base href="https://uzbuild.uz/">`. The page loads `era_cms/plugins/custom.js?v=50.3.1` (recorded: `uzbuild_custom_js.html`). |
| `uzbuild_custom_js.html` | https://uzbuild.uz/era_cms/plugins/custom.js?v=50.3.1 | 200 | allowed | sandbox | The ERA site script the list page loads (`application/javascript`, stored by the recorder as .html). It initialises `$('#ERADataTable').DataTable({… "lengthMenu": [ 10, 25, 50, 75, 100, 500, 1000, 5000 ], "pageLength": 10, "serverSide": true, "ajax": { url: dataTableURL, type: "post", data: … field1–field4 } })` — i.e. the browser **POSTs** the DataTables server-side parameters plus the four filter fields. Its recorder line reads `scrubbed: no` (it was first read as a scratch aux file); `scrub_fixture` run on it before it was moved here found nothing to replace (bytes and sha256 unchanged). |
| `uzbuild_2026_companies_all.json` | https://uzbuild.uz/ERAForms/companies_list.php?l=en&exhibition=461&draw=1&start=0&length=500 | 200 | allowed | sandbox | The same endpoint answers a **GET** with the DataTables parameters (served as `text/html`, body is JSON; recorded with `--json`): `{"draw","recordsTotal":341,"recordsFiltered":341,"data":[[company_html, country, stand, location, favourite_html], …]}` — **all 341 rows of UzBuild 2026 in one request** (the site's own `lengthMenu` offers up to 5000 — `uzbuild_custom_js.html`; `length=500` is the largest value tried). 78 Uzbekistan, 128 China, 45 Russian Federation, 25 Türkiye … (19 countries). |
| `uzbuild_2026_companies_p2.json` | …&exhibition=461&draw=2&start=100&length=100 | 200 | allowed | sandbox | Pagination proof: 100 rows, `recordsTotal` 341, `data` equal to rows 100–199 of the full answer. |
| `uzbuild_2026_company_uz.html` | https://uzbuild.uz/en/exhibitor-info-461-1155 | 200 | allowed | sandbox | Card (the row link `/en/exhibitor-info-<exhibition>-<company id>`): `h1.company__name`, `.company__stand` (last `span` = stand), `.company__description`, `.company__website a[href]` (empty here), `.company__socials a`, `.company__indexes .company__index` (product categories = the sector). No phone, no e-mail, no address on ERA cards. |
| `uzbuild_archive_p1.html` | https://uzbuild.uz/en/exhibition/spisok-uchastnikov.php?PAGEN_2=1 | **404** | allowed | sandbox | **NEGATIVE / archive gone**: the Bitrix archive URL of the spec answers 404 with the site's home page as the body (a soft 404 that a status check catches; 0 rows). `PAGEN_2=2` was checked the same way (404, same 115,773-byte page; not kept). The home page block it renders says «450 Participating companies · 22 Countries» (UzBuild 2026 post-show results). |

### Power Uzbekistan (Iteca) — `power-uzbekistan.uz` (ERA CMS)

| File | Source URL | Status | robots | from | What it proves |
|---|---|---|---|---|---|
| `power_list_en.html` | https://power-uzbekistan.uz/en/exhibitors-list | 200 | allowed | sandbox | Same ERA shell as UzBuild: editions 409 = 2024, 440 = 2025, **474 = 2026** (12–14 May 2026), 501 = 2027 selected; `dataTableURL = 'ERAForms/companies_list.php?l=en&exhibition=501'`. |
| `power_2026_companies_all.json` | https://power-uzbekistan.uz/ERAForms/companies_list.php?l=en&exhibition=474&draw=1&start=0&length=500 | 200 | allowed | sandbox | **218 rows**: 154 China, 19 Uzbekistan, 19 Russian Federation, 13 Türkiye … One row's `location` is `null` (a parser must accept it). |
| `power_2026_companies_p2.json` | …&exhibition=474&draw=2&start=100&length=100 | 200 | allowed | sandbox | 100 rows = rows 100–199 of the full answer. |
| `power_2026_company_uz.html` | https://power-uzbekistan.uz/en/exhibitor-info-474-29934 | 200 | allowed | sandbox | Uzbek card with a website: `.company__website a[href="https://www.chtz.uz"]`; description in Russian; categories. |
| `power_2027_companies_foreign_only.json` | …&exhibition=501&draw=1&start=0&length=10 | 200 | allowed | sandbox | **NEGATIVE / foreign only**: the list page's default edition (2027) has `recordsTotal: 1` — one Russian Federation row (`location: null`). A parser keeping Uzbek rows must return `[]`. |

### CAITME (Iteca) — `caitme.uz` (ERA CMS)

| File | Source URL | Status | robots | from | What it proves |
|---|---|---|---|---|---|
| `caitme_list_en.html` | https://caitme.uz/en/exhibitors-list | 200 | allowed | sandbox | **NEGATIVE / not yet published**: the default edition is **2028 (6–8 Sep 2028)** (507); the block says «This information will be available soon» and carries **no table and no `dataTableURL`**. Editions: 362 = 2024, 446 = 2025, **480 = 2026**, 507 = 2028 — no 2027 option. |
| `caitme_2026_companies_all.json` | https://caitme.uz/ERAForms/companies_list.php?l=en&exhibition=480&draw=1&start=0&length=500 | 200 | allowed | sandbox | **170 rows**: 47 China, 41 Uzbekistan, 35 Türkiye, 19 Germany … (15 countries); one `location` null. |
| `caitme_2026_companies_p2.json` | …&exhibition=480&draw=2&start=100&length=100 | 200 | allowed | sandbox | The 70-row tail = rows 100–169 of the full answer (a short last page). |
| `caitme_2025_count.json` | …&exhibition=446&draw=1&start=0&length=1 | 200 | allowed | sandbox | CAITME 2025 `recordsTotal: 163` (organiser: 210). Its one row shows the sponsor marker: the anchor's class is `sponsor-glass-red cursor_pointer` instead of ` cursor_pointer `. |
| `caitme_2026_company_uz.html` | https://caitme.uz/en/exhibitor-info-480-48027 | 200 | allowed | sandbox | Uzbek card (sewing-machine supplier): description filled, website and categories empty — the empty-field shape. |

### WoodTech & MebelExpo (Iteca) — `mebelexpo.uz` (ERA CMS)

The organiser's Instagram handle equals the site's host name, so the scrubber replaced the
host with `namuna_02`/`namuna_03` in these HTML bodies (including `<base href>`) and in
their `.headers.json` URLs; the JSON files keep the host. The real URLs are the ones below.

| File | Source URL | Status | robots | from | What it proves |
|---|---|---|---|---|---|
| `mebelexpo_home.html` | https://mebelexpo.uz/ | 200 | allowed | sandbox | Pattern discovery: the home page links «Exhibitors List» → `/en/exhibitors-list` and «Official catalogue» → `/en/official-catalogue` — the ERA CMS, **not Bitrix**. |
| `mebelexpo_list_en.html` | https://mebelexpo.uz/en/exhibitors-list | 200 | allowed | sandbox | Editions 407 = 2024, 443 = 2025, **463 = 2026** (28–30 Apr 2026), 499 = 2027 selected; the default (2027) block says «This information will be available soon» (no table) — a second not-yet-published negative. |
| `mebelexpo_2026_companies_all.json` | https://mebelexpo.uz/ERAForms/companies_list.php?l=en&exhibition=463&draw=1&start=0&length=500 | 200 | allowed | sandbox | **80 rows**: 33 China, 25 Uzbekistan, 11 Türkiye, 4 Russian Federation, 3 Hong Kong … |
| `mebelexpo_2026_companies_p2.json` | …&exhibition=463&draw=2&start=40&length=40 | 200 | allowed | sandbox | 40 rows = rows 40–79 of the full answer. |
| `mebelexpo_2026_company_uz.html` | https://mebelexpo.uz/en/exhibitor-info-463-154069 | 200 | allowed | sandbox | Uzbek card with website and Instagram (`.company__socials a.company__social-link`, scrubbed) and nine product categories. |

### TextileExpo Uzbekistan (Iteca) — `textileexpo.uz` (ERA CMS)

| File | Source URL | Status | robots | from | What it proves |
|---|---|---|---|---|---|
| `textileexpo_home.html` | https://textileexpo.uz/ | 200 | allowed | sandbox | Pattern discovery: «Exhibitors List» → `/en/exhibitors-list` (ERA CMS). |
| `textileexpo_list_en.html` | https://textileexpo.uz/en/exhibitors-list | 200 | allowed | sandbox | Editions (`select[onchange^="ChangeYear"]`, placeholder option first): 410 = 28–30 May 2024, 442 = 15–17 Oct 2024, 452 = 14–16 May 2025, 472 = 9–11 Sep 2025, **478 = 13–15 May 2026**, 504 = 27–29 Apr 2027 `selected` («available soon»). **Two editions a year in 2024 and 2025, one in 2026** (no autumn 2026 edition is listed, although it would already be over on 2026-10-29), and the next one is in April, not May. |
| `textileexpo_2026_companies_all.json` | https://textileexpo.uz/ERAForms/companies_list.php?l=en&exhibition=478&draw=1&start=0&length=500 | 200 | allowed | sandbox | **144 rows**: 84 China, 57 Uzbekistan, 1 each Kazakhstan / Russian Federation / Türkiye. |
| `textileexpo_2026_companies_p2.json` | …&exhibition=478&draw=2&start=100&length=100 | 200 | allowed | sandbox | The 44-row tail = rows 100–143 of the full answer. |
| `textileexpo_2026_company_uz.html` | https://textileexpo.uz/en/exhibitor-info-478-33959 | 200 | allowed | sandbox | Uzbek card with every optional field empty (description, website, socials, categories) — the minimal shape. |

## Day-1 answers

The UNVERIFIED items of 03 §2.8 for this key's sub-sources, and the Day-1 line («Fetch
Automechanika 2026 and UzBuild archive page 1; record fixtures; confirm the `PAGEN_2`
parameter»), answered from what was seen on 2026-10-29 from the sandbox.

**Two platforms, not «Tilda + Bitrix».** Automechanika's rows are on the expodat catalogue
(`catalog.automechanika.uz`, Joomla `com_expo`, server-rendered HTML). All five Iteca shows
(UzBuild, Power, CAITME, MebelExpo, TextileExpo) run the same ERA CMS with the same
DataTables JSON endpoint. One parser per platform covers all six shows.

| # | Item (spec 03 §2.8) | Answer | Status |
|---|---|---|---|
| 1 | Automechanika: «Tilda; look for an embedded JSON/XHR» | No rows and no `t-store`/`data-*` JSON on `/exhibitorslisteng`; it only links out. The 2026 list is `https://catalog.automechanika.uz/en/expositions/exposition/7012` (found via «Exhibitors Catalogue 2026» on `/eng/participants`). | VERIFIED — `automechanika_2026_list_en.html`, `automechanika_participants_en.html` |
| 2 | Automechanika row selectors | Row = `#scroll_list .row-fluid.rexc > .span12 > a[href^="/en/companies/company/"]`; company = the anchor's `title` (also the `.exh_name` text); stand = `.numb_stand b` (absent on some rows, e.g. «Autoport», «BMCA»); country = `.img_country[title]` (one flag per row; flag image `/images/flags/<country id>.png`); description = `.short_opis`; sponsor rows add `cl_sponsor` to `.table_exc`. | VERIFIED — `automechanika_catalog_2026_p1.html` |
| 3 | Automechanika pagination | Query `start=<row offset>`, **48 rows per page** (`lim = '48'` in the infinite-scroll script; page 2 at `start=48` continues page 1 with no overlap). The pager widget's own links step by 8 (`start=8,16,…`, «End» = `start=456`); a crawler that follows those links would request overlapping pages — step by 48 itself. Past the end: HTTP 200, 0 rows, «no results found». That `start=8` returns rows 9–56 is inferred, not fetched. | VERIFIED (step 48, stop page) — `…_p1`, `…_p2`, `…_last`, `…_past_end`; step-8 overlap STILL UNVERIFIED (not fetched) |
| 4 | Automechanika country filter | The filter is a POST form: `<form action="" method="post" id="listForm">` with a multiple `<select name="filter_country">` (no `[]` in the name; Uzbekistan = 345), plus `filter_rubricator_1`, `filter_rubricator_2`, `filter_expo_id`. One GET probe was sent as `?filter_country%5B%5D=345` — **not the form's field name** — and its answer was deleted; its sha256 (`5b40694e…`) differs from page 1's (`b369c2be…`), so it was not a byte-for-byte duplicate, and whether its rows matched page 1 cannot be re-checked from the repo. Not needed for the crawl: the collector reads every page and filters by `.img_country[title]` (462 rows = 10 requests). | Form shape VERIFIED — `automechanika_catalog_2026_p1.html`; whether a GET or POST filter works STILL UNVERIFIED (probe used the wrong field name; POST not attempted) |
| 5 | Automechanika year variants / «list removed after the show» | 2026 catalogue (7012) online now, 6 days after the show (21–23 Oct). The 2025 catalogue (6400) is **removed**: «The event was not found» on the exhibitor-login page. The 2024 list is a Tilda Feed (`feeduid 327968451841`) loaded by JS; its API was not fetched. So: crawl inside the window, an archive cannot be counted on. | VERIFIED (6400 removed) — `automechanika_catalog_2025_p1.html`; 2024 feed STILL UNVERIFIED (other host, JS-only, outside the crawl plan) |
| 6 | Automechanika Uzbek rows | 5 of 48 on page 1, 2 of 48 on page 2, 4 of 6 on the last page (the order is not strictly alphabetical: the sponsor «Shell Lubricants» first, «JV …» names at the end). The organiser **BMCA** itself appears as an Uzbekistan row (exclude the organiser). The total Uzbek count needs all 10 pages — not fetched. | VERIFIED per page; total STILL UNVERIFIED |
| 7 | Automechanika card (phones server-rendered?) | Yes: the expodat card renders address, website, e-mail, phone and Telegram/YouTube links in HTML (scrubbed in the fixture). Company-tier, but only `website` is needed for resolution. | VERIFIED — `automechanika_catalog_company_uz.html` |
| 8 | UzBuild: «archive `/en/exhibition/spisok-uchastnikov.php?PAGEN_2=N` (2022–2026)»; «confirm the `PAGEN_2` parameter» | **`PAGEN_2` is dead**: page 1 and page 2 both answer 404 (soft 404 with the home page). The site moved from Bitrix to ERA CMS; the archive is now the edition `<select>` on `/en/exhibitors-list`, which offers 2024–2027 only (401/430/461/490). Whether the 2022–2023 lists still exist anywhere was **not tested**: the 2022 archive URL in `docs/manbalar.json` (`spisok-uchastnikov.php?ELEMENT_ID=57355`) and ERA ids below 401 were not requested. | `PAGEN_2` 404 VERIFIED — `uzbuild_archive_p1.html` (page 2 checked, not kept); editions offered VERIFIED — `uzbuild_list_en.html`; 2022–2023 gone STILL UNVERIFIED (inferred from the select only) |
| 9 | ERA list endpoint (UzBuild, Power, CAITME, MebelExpo, TextileExpo) | `GET <show>/ERAForms/companies_list.php?l=en&exhibition=<id>&draw=1&start=<offset>&length=<n>` → JSON `{draw, recordsTotal, recordsFiltered, data: [[company_html, country, stand, location, favourite_html]]}`; `<id>` = the `value` of the `<option>`s of `select[onchange^="ChangeYear"]` on `/en/exhibitors-list` (the raw attribute is `ChangeYear(this);`; skip the first, value-less placeholder option); company name = text of the `<a>` in `data[i][0]`, card = its `href` `/en/exhibitor-info-<id>-<company id>`; `length=500` returns a whole show in one request. The answer's `content-type` is `text/html` — parse it as JSON regardless. Without the DataTables parameters the endpoint answers **HTTP 500, empty body** (seen 3 times on the bare `dataTableURL`; not kept). The site's own JS sends these parameters as a **POST** (`serverSide: true`, `type: "post"`, plus `field1`–`field4` for the filters — `uzbuild_custom_js.html`); the GET form was used because the Fetcher has no form-POST path for non-API hosts (§1.1). **No POST was sent**, so the GET answer was not compared with a POST answer; it is a well-formed DataTables answer whose `recordsTotal` matches `len(data)` at `length=500`. | GET endpoint and JSON shape VERIFIED — `*_companies_all.json`, `*_companies_p2.json` (slice equality checked for all five); the site's POST call VERIFIED — `uzbuild_custom_js.html`; GET ≡ POST STILL UNVERIFIED (no POST sent) |
| 10 | Paginate «until a page repeats or is empty» | ERA: stop when `start ≥ recordsTotal` (or ask `length=500` once and check `len(data) == recordsTotal`). Expodat: stop on a page with 0 rows. | VERIFIED |
| 11 | Columns `company \| pavilion \| stand \| country \| sector` | ERA: company · country · stand · location (location = the venue, «Uzexpocentre NEC» / «CAEx Uzbekistan», sometimes `null`; **no pavilion and no sector column** — the hall is the stand's letter, the sector is the card's `.company__index` list). Expodat: company · stand · country flag · short description (no pavilion; sector = the card's product index). | VERIFIED |
| 12 | Uzbek vs foreign | **The country is on every row of all six shows**: ERA `data[i][1]` = «Uzbekistan» (English list), expodat `.img_country[title]` = «Uzbekistan». The legal-form fallback is not needed — and would be wrong: in UzBuild 2026, 40 of the 263 foreign rows match `collectors.keywords.LEGAL_FORM_RE` (all on «LLC»/«OOO»: 29 Russian Federation, 4 Azerbaijan, 2 Kyrgyzstan, 1 each Kazakhstan, Iran, Türkiye, Georgia, China; 36 of them end in the form, 4 carry it mid-name, e.g. «LLC ALLIANCE CITY»), and 18 of its 78 Uzbek rows match no `LEGAL_FORM_RE` form (among them «LUSTRALAR OLAMI, PE», «SAMA GROUP, PE» and «HIGH TECH PANELS, LTD / / AKFA STEEL» — «PE» and «LTD» are not in the regex). Uzbek rows that are not leads: the organiser (BMCA), «GOLDEN PAGES» (a directory, in 4 of 5 ERA lists), a state agency (TextileExpo), local offices of foreign brands (DAHUA TECHNOLOGY, HUAWEI DIGITAL POWER at Power; DATANG LIGHTING at UzBuild) and a Turkish shipping firm («MARTRADE DENIZCILIK TASIMACILIK A.S.», Power — a forwarder, competitor, never a lead). Russian/Uzbek spellings («Узбекистан», «O'zbekiston») were not seen because only `/en/` was recorded. | VERIFIED (EN) — every `*_companies_all.json`, `automechanika_catalog_2026_p1.html`; RU/UZ spellings STILL UNVERIFIED |
| 13 | `attrs.country` for gate 3 (CN rows) | Available on every row (China: UzBuild 128, Power 154, CAITME 47, MebelExpo 33, TextileExpo 84, Automechanika 41 of the first 48). T-32 can store it. | VERIFIED |
| 14 | Phones in lists | No list (ERA JSON or expodat page) carries a phone, e-mail or handle. ERA cards: website + socials only; expodat cards: full contacts. | VERIFIED |
| 15 | MebelExpo «pattern UNVERIFIED, assume Bitrix» | ERA CMS, `/en/exhibitors-list` + the ERA endpoint; editions 407/443/463/499. | VERIFIED — `mebelexpo_home.html`, `mebelexpo_list_en.html`, `mebelexpo_2026_companies_all.json` |
| 16 | TextileExpo «pattern UNVERIFIED» | ERA CMS, same endpoint. Editions: two a year in 2024 (May, Oct) and 2025 (May, Sep), **one in 2026** (13–15 May; no autumn 2026 edition listed), next 27–29 Apr 2027. The calendar must read the editions from this select, not assume a May + autumn pair. Whether an autumn edition returns after 2026 is not shown. | Editions VERIFIED — `textileexpo_list_en.html`; future cadence STILL UNVERIFIED |
| 17 | Rate 1 req/3 s, ≤ 100 pages/run | No `Crawl-delay` on any of the seven hosts. One run needs ~1 request per ERA show (+1 for the list page to read the edition id) and ~10 for Automechanika. | VERIFIED |
| 18 | Calendar (next crawl windows) | Seen on the sites: Automechanika 20–22 Oct 2027; UzBuild 16–18 Mar 2027 (= spec); Power 12–14 May 2027; MebelExpo 27–29 Apr 2027 (= spec); **TextileExpo 27–29 Apr 2027** (spec says May 2027); **CAITME's next edition is listed as 6–8 Sep 2028**, there is no 2027 option (spec says Sep 2027). | VERIFIED for what the sites print; whether CAITME skips 2027 STILL UNVERIFIED (ask the organiser) |
| 19 | China–Central Asia Trade Expo (CCPIT), Chinese brands fair (CAEx) lists | Not in this task's host list; nothing fetched. | STILL UNVERIFIED |
| 20 | TransLogistica (Iteca, competitors only) | Probably the same ERA CMS — not fetched (not in this task's host list). | STILL UNVERIFIED |

**Row counts against the organiser's figures** (a crawl under 50 % is `partial`):

| Show | Edition | Rows seen | Organiser's figure (spec / `docs/manbalar.json`) | Ratio | Uzbek rows |
|---|---|---|---|---|---|
| Automechanika Tashkent | 2026 (7012) | 462 | 489 (2025 edition) | 94 % | 11 in the 3 pages fetched (of 10) |
| UzBuild | 2026 (461) | 341 | 418–450 («450 participating companies» on the site) | 76–82 % | 78 |
| Power Uzbekistan | 2026 (474) | 218 | 250 (last edition, JETRO) | 87 % | 19 |
| CAITME | 2026 (480) | 170 | — (2025: 210) | — | 41 |
| CAITME | 2025 (446) | 163 | 210 | 78 % | not fetched (`length=1`) |
| WoodTech & MebelExpo | 2026 (463) | 80 | 127–130 | 62–63 % | 25 (31 %; organiser: 72 % international) |
| TextileExpo | 2026 (478) | 144 | «170+ brands» | 85 % | 57 |

The organisers count brands, co-exhibitors and pavilion members; the lists count exhibitor
entries — every ratio is above the 50 % alert line, MebelExpo the lowest.

## Refused / not recorded

- **Nothing was refused** by a site: no 403, 429, captcha, JS challenge or login wall on any
  request (every stored body passes `services.http.looks_like_captcha` as clean).
- `catalog.automechanika.uz` 2025 catalogue (6400): removed by the organiser; the redirect
  lands on the exhibitors' login page. Kept as a negative fixture; nothing behind the login.
- Automechanika 2024 rows: in a Tilda Feed fetched by JS from the Tilda feeds API (another
  host); not fetched.
- Expodat country filter as a POST form: not attempted (the Fetcher has no form POST for a
  non-API host). One GET probe, `…-2026.html?filter_country%5B%5D=345`, was recorded and
  **deleted**; it used `filter_country[]`, which is not the form's field name
  (`filter_country`), and its sha256
  (`5b40694e1645b2a1ce2814130614eed1ef308a6bc2bc343726419503593133d9`) differs from page 1's,
  so it proves nothing about the filter and is not a byte-for-byte page-1 duplicate. A GET
  with the right field name was not tried.
- `uzbuild.uz/ERAForms/companies_list.php?l=en&exhibition=461` without DataTables
  parameters: HTTP 500, empty body, three attempts (the Fetcher's backoff); the empty file
  was deleted.
- `uzbuild.uz/en/exhibition/spisok-uchastnikov.php?PAGEN_2=2`: 404, same soft-404 page as
  page 1; kept out of the repo.
- The ERA site script `era_cms/plugins/custom.js` was first read into the scratch directory
  and is now kept here as `uzbuild_custom_js.html` (same bytes, scrubber found nothing), so
  the README's DataTables claims rest on a committed file. One read stays out of the repo: a
  `length=10` probe of the UzBuild endpoint (superseded by
  `uzbuild_2026_companies_all.json`).
- No POST was sent to any `companies_list.php`; the claim that the endpoint answers a GET the
  same way as the site's POST is untested.
- Only English pages were recorded (`/en/`); the RU/UZ variants of the lists were not.
- Not fetched at all: CCPIT / China–Central Asia Trade Expo, the Chinese brands fair,
  TransLogistica.
- Scrubber artefacts (not hand-edited, 07 §1.1): EXZAP → `namuna_02` in
  `automechanika_catalog_company_uz.html` (its YouTube handle is its name); the MebelExpo
  host → `namuna_02`/`namuna_03` in the three MebelExpo HTML files and their header URLs (the
  organiser's Instagram handle equals the host name); a few two-word labels and
  `<meta name="developer">` → `Seller NN`; Tilda's `<!-- @classes: … -->` comments →
  `@namuna_01`. Phones, e-mails, Telegram links and Instagram handles are pseudonymised
  throughout. The organisers' **Facebook, YouTube and LinkedIn** page names are not touched by
  the scrubber and stay verbatim (company-tier: `facebook.com/UzBuild.uz`,
  `youtube.com/user/iteuzbekistan`, `linkedin.com/company/automechanika-tashkent/`,
  `linkedin.com/showcase/…` and the like), as do firm names, stands and countries.

## Recorder lines

One line per file: file · date · url · sha256 · status · bytes · scrubbed · note
(written by the recorder; `power_2027_companies_foreign_only.json` was renamed from
`power_2027_companies_empty.json` after its body showed one foreign row;
`uzbuild_custom_js.html` was moved here from the recorder's scratch aux directory).

- automechanika_2026_list_en.html · 2026-10-29 · https://automechanika.uz/exhibitorslisteng · 6cae0334b6d15d08134bc9bb0898a96277b79e8f8c5a64131426d08858c466c4 · 200 · 131202 · scrubbed: yes · Automechanika Tashkent 2026 exhibitor list (Tilda page)
- automechanika_participants_en.html · 2026-10-29 · https://automechanika.uz/eng/participants · 4096bb12ee58e4ddf0f93e85a8dd7ecee8791e56a796aa67dee1b341cabde886 · 200 · 138071 · scrubbed: yes · Automechanika 'Participants' landing (EN menu target of 'Exhibitors List')
- automechanika_catalog_2026_p1.html · 2026-10-29 · https://catalog.automechanika.uz/en/expositions/exposition/7012 · b369c2be9ed3af9816b542e7e8baf3beb28f915f2ecfa4a656452e975b9caee6 · 200 · 160577 · scrubbed: yes · Automechanika Tashkent 2026 exhibitors catalogue, page 1
- automechanika_catalog_2026_p2.html · 2026-10-29 · https://catalog.automechanika.uz/en/expositions/exposition/7012-automechanika-tashkent-comtrux-tashkent-bautrux-tashkent-2026.html?start=48 · 5579b1bf3bf16b2eda73e49664dcc76d98f99a5038ddd39b01481c5299ebb3de · 200 · 157963 · scrubbed: yes · Automechanika 2026 catalogue, page 2 (start=48)
- automechanika_catalog_robots.html · 2026-10-29 · https://catalog.automechanika.uz/robots.txt · 9b24179b2c6dd789831beba1ae0c1dfc950305b9a120556184e2c4ca14f33ea0 · 200 · 686 · scrubbed: no · robots.txt of the expodat catalogue host (recorded to state the robots verdict)
- automechanika_catalog_2026_last.html · 2026-10-29 · https://catalog.automechanika.uz/en/expositions/exposition/7012-automechanika-tashkent-comtrux-tashkent-bautrux-tashkent-2026.html?start=456 · 351d38dcceccb2e03f235cf781b4089553022a34d4b98863defe4bb22eb27b69 · 200 · 52484 · scrubbed: yes · Automechanika 2026 catalogue, last page (start=456, the pager's End link) - gives the row total
- automechanika_catalog_2026_past_end.html · 2026-10-29 · https://catalog.automechanika.uz/en/expositions/exposition/7012-automechanika-tashkent-comtrux-tashkent-bautrux-tashkent-2026.html?start=504 · 66d6d1fc28f79a85eb9d3942180e3a192cc45f3ea499389fe7b0bef318ebb200 · 200 · 37112 · scrubbed: yes · NEGATIVE: start=504 is past the 462-row end - the paginator-stop case
- automechanika_catalog_company_uz.html · 2026-10-29 · https://catalog.automechanika.uz/en/companies/company/150345-exzap.html · cdc05a382796aa1bb3ed7019b563d8e437d31733a387b8cb24998f39be58dac4 · 200 · 25298 · scrubbed: yes · Automechanika 2026 catalogue: one Uzbek exhibitor's card (EXZAP)
- automechanika_catalog_2025_p1.html · 2026-10-29 · https://catalog.automechanika.uz/en/expositions/exposition/6400 · e8a55faef32d7dcc912399588f292bedeeeeeac5668ac7dfda16cdf0de1720c1 · 200 · 31831 · scrubbed: yes · Automechanika Tashkent 2025 exhibitors catalogue (archive edition), page 1
- automechanika_2024_list_en.html · 2026-10-29 · https://automechanika.uz/exhibitors-eng2024 · 7d470e507fab16b1f177792605043dd10583f232f618a2f726d1c84f770b3871 · 200 · 141616 · scrubbed: yes · Automechanika Tashkent 2024 exhibitor list (Tilda page, the 'Exhibitors List 2024' button)
- uzbuild_list_en.html · 2026-10-29 · https://uzbuild.uz/en/exhibitors-list · faf98e2a6dd440b7f424d8f50917b791f4862672caf1317812b0c129e12356b7 · 200 · 93002 · scrubbed: yes · UzBuild current exhibitor list (EN)
- uzbuild_custom_js.html · 2026-10-29 · https://uzbuild.uz/era_cms/plugins/custom.js?v=50.3.1 · 76934389645990da226de4a1c30edcbf5ae272c20e6c236af80cb31cc57de825 · 200 · 11398 · scrubbed: no (scrub_fixture afterwards: nothing) · the site's DataTables init script (lengthMenu, serverSide, POST)
- uzbuild_archive_p1.html · 2026-10-29 · https://uzbuild.uz/en/exhibition/spisok-uchastnikov.php?PAGEN_2=1 · 193e0f159c1890eee2fd5554be95beeb3fdc91161fac48741ee90e56f131c501 · 404 · 115773 · scrubbed: yes · UzBuild archive exhibitor list (Bitrix), PAGEN_2=1
- uzbuild_2026_companies_all.json · 2026-10-29 · https://uzbuild.uz/ERAForms/companies_list.php?l=en&exhibition=461&draw=1&start=0&length=500 · d56fdc87282b9f632b745e6b69f1a83174aa67347f9f5dc82afff0bac257dca4 · 200 · 119712 · scrubbed: yes · UzBuild 2026 (exhibition=461) DataTables XHR as GET, start=0 length=500 = every row
- uzbuild_2026_companies_p2.json · 2026-10-29 · https://uzbuild.uz/ERAForms/companies_list.php?l=en&exhibition=461&draw=2&start=100&length=100 · b5860b250e0e513996718918155020e01c5ea8cd651e9d96d26d1069ad03f2bb · 200 · 35105 · scrubbed: yes · UzBuild 2026 XHR page 2 (start=100 length=100) - proves start/length pagination
- uzbuild_2026_company_uz.html · 2026-10-29 · https://uzbuild.uz/en/exhibitor-info-461-1155 · efb002428cc43b466491f39849bbcd41bae2f912cdc4fbdee21b3361c23b0ab0 · 200 · 82595 · scrubbed: yes · UzBuild 2026: one Uzbek exhibitor's card (row link /en/exhibitor-info-<exhibition>-<company id>)
- power_list_en.html · 2026-10-29 · https://power-uzbekistan.uz/en/exhibitors-list · 2f8bc0abc46e483937bdc7c7f38d6f27790b5ab3b7890bf5f977526772c767c7 · 200 · 83923 · scrubbed: yes · Power Uzbekistan exhibitor list page (EN)
- power_2026_companies_all.json · 2026-10-29 · https://power-uzbekistan.uz/ERAForms/companies_list.php?l=en&exhibition=474&draw=1&start=0&length=500 · 16761cbff0dd003db430b8a028129d3ce2b36bc5032ce59c2852bd69b29aa65e · 200 · 76351 · scrubbed: yes · Power Uzbekistan 2026 (12-14 May 2026, exhibition=474) DataTables XHR as GET, start=0 length=500 = every row
- power_2026_companies_p2.json · 2026-10-29 · https://power-uzbekistan.uz/ERAForms/companies_list.php?l=en&exhibition=474&draw=2&start=100&length=100 · c81965ebc25e78b9bcd49f5268da7af1a8118940c8966a869ac98763449c7fe3 · 200 · 35146 · scrubbed: yes · Power Uzbekistan 2026 XHR page 2 (start=100 length=100)
- power_2027_companies_foreign_only.json · 2026-10-29 · https://power-uzbekistan.uz/ERAForms/companies_list.php?l=en&exhibition=501&draw=1&start=0&length=10 · 32486a7fb7309bc41cbf9f1e780125021cf146dac2c4ed84c16f3fc924e2faed · 200 · 379 · scrubbed: yes · NEGATIVE: Power Uzbekistan 2027 (exhibition=501, the list page's default selection) - a single foreign (Russian Federation) row, no Uzbek row
- power_2026_company_uz.html · 2026-10-29 · https://power-uzbekistan.uz/en/exhibitor-info-474-29934 · 9a88c130f05a2cc8e2efafc71f28bda3402f62b1901477c947409995a4c589d9 · 200 · 76741 · scrubbed: yes · Power Uzbekistan 2026: one Uzbek exhibitor's card
- caitme_list_en.html · 2026-10-29 · https://caitme.uz/en/exhibitors-list · 289b078c694060f6231041cd7d6628aee6acf11c5a69894872df84155923e722 · 200 · 80879 · scrubbed: yes · CAITME exhibitor list page (EN)
- caitme_2026_companies_all.json · 2026-10-29 · https://caitme.uz/ERAForms/companies_list.php?l=en&exhibition=480&draw=1&start=0&length=500 · d72e0a908ea5e6f311f40d3f5221e8d7ecca7580256bc796d7667603b2b535be · 200 · 60091 · scrubbed: yes · CAITME 2026 (8-10 Sep 2026, exhibition=480) DataTables XHR as GET, start=0 length=500 = every row
- caitme_2026_companies_p2.json · 2026-10-29 · https://caitme.uz/ERAForms/companies_list.php?l=en&exhibition=480&draw=2&start=100&length=100 · 501812cad1b721238655a2e28f39e96f682316ff78f6fa62ce49208f9f742afe · 200 · 24753 · scrubbed: yes · CAITME 2026 XHR page 2 (start=100 length=100, the 70-row tail)
- caitme_2025_count.json · 2026-10-29 · https://caitme.uz/ERAForms/companies_list.php?l=en&exhibition=446&draw=1&start=0&length=1 · 087ce03cf1eede3e52ea1cc2558e8d3e8274bd3c23f31a53eb63f1eb451f4080 · 200 · 454 · scrubbed: yes · CAITME 2025 (exhibition=446) XHR, length=1: recordsTotal against the organiser's 210
- caitme_2026_company_uz.html · 2026-10-29 · https://caitme.uz/en/exhibitor-info-480-48027 · 8cd29ab8bae540c2aeab41287abc903ecb0ab804ddeda8ab34070da2f8b1b8bf · 200 · 74821 · scrubbed: yes · CAITME 2026: one Uzbek exhibitor's card
- mebelexpo_home.html · 2026-10-29 · https://namuna_02/ · f3e54b9652b0adaf835051ad6bd45162adaec9abee37f65f72e0b843f7843e59 · 200 · 108282 · scrubbed: yes · WoodTech & MebelExpo home page - used to find the exhibitor-list link
- mebelexpo_list_en.html · 2026-10-29 · https://namuna_02/en/exhibitors-list · 32090a85ede5139061678779c538ec40209134256f02f693060c57b9544eebfd · 200 · 83051 · scrubbed: yes · WoodTech & MebelExpo exhibitor list page (EN), found from the home page's 'Exhibitors List' link
- mebelexpo_2026_companies_all.json · 2026-10-29 · https://mebelexpo.uz/ERAForms/companies_list.php?l=en&exhibition=463&draw=1&start=0&length=500 · 76088106cc7ae5d361938f2f4be3844d3259a8ce066851e57915042a844d687f · 200 · 28152 · scrubbed: yes · WoodTech & MebelExpo 2026 (28-30 Apr 2026, exhibition=463) DataTables XHR as GET, start=0 length=500 = every row
- mebelexpo_2026_companies_p2.json · 2026-10-29 · https://mebelexpo.uz/ERAForms/companies_list.php?l=en&exhibition=463&draw=2&start=40&length=40 · 6fd67aac8f34d86aa197c67e6aec79b848ed2c94d79306c860a2875d55edee42 · 200 · 14101 · scrubbed: yes · WoodTech & MebelExpo 2026 XHR page 2 (start=40 length=40)
- mebelexpo_2026_company_uz.html · 2026-10-29 · https://namuna_03/en/exhibitor-info-463-154069 · 03b306482f58c2b9e82fc0b50419364f6d3ddd7c3b28cfc396a034012713f172 · 200 · 78057 · scrubbed: yes · WoodTech & MebelExpo 2026: one Uzbek exhibitor's card
- textileexpo_home.html · 2026-10-29 · https://textileexpo.uz/ · 5f95bd9b39f1daafc3922ee81e3ddaf9ee4d24ca89820b384a0d4a299f7e44f1 · 200 · 99730 · scrubbed: yes · TextileExpo Uzbekistan home page - used to find the exhibitor-list link
- textileexpo_list_en.html · 2026-10-29 · https://textileexpo.uz/en/exhibitors-list · b605adc4c8080276859ba61eecc48292f2603efceba6478782dc2f954bb53d73 · 200 · 81765 · scrubbed: yes · TextileExpo exhibitor list page (EN), found from the home page's 'Exhibitors List' link
- textileexpo_2026_companies_all.json · 2026-10-29 · https://textileexpo.uz/ERAForms/companies_list.php?l=en&exhibition=478&draw=1&start=0&length=500 · ac8f1ac5d58481f5a397aaf8e1c330b42d8d31939466300d83af9ff1f954005d · 200 · 50418 · scrubbed: yes · TextileExpo 2026 (13-15 May 2026, exhibition=478) DataTables XHR as GET, start=0 length=500 = every row
- textileexpo_2026_companies_p2.json · 2026-10-29 · https://textileexpo.uz/ERAForms/companies_list.php?l=en&exhibition=478&draw=2&start=100&length=100 · 76dfdaaf85c579368540ffc192871187049b03dc7f29e74c5e4c37e487ef8b6e · 200 · 15605 · scrubbed: yes · TextileExpo 2026 XHR page 2 (start=100 length=100, the 44-row tail)
- textileexpo_2026_company_uz.html · 2026-10-29 · https://textileexpo.uz/en/exhibitor-info-478-33959 · a7ad598157ddebb8ba04beb017ecd04a4d8fcaed4f86fb10d6df3f495420f30a · 200 · 75401 · scrubbed: yes · TextileExpo 2026: one Uzbek exhibitor's card
