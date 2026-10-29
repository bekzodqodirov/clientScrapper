# Fixtures: directories

Recorded through `clientscrapper.tools.record_fixture.record()` — the function the CLI
`python -m clientscrapper.tools.record_fixture` (07 §1.1) calls — by a driver script that is not
in the repo (build session scratchpad), over a URL list, one `Fetcher` per batch, `record()`
called per URL with `scrub="on"`. The CLI itself was not run. Every request
went through `services/http.Fetcher`: User-Agent `clientScrapperBot/0.1.0 python-httpx/0.28.1`
(no `+mailto` part: `HH_USER_AGENT` is blank in the sandbox), robots.txt read once per
authority with the project's RFC 9309 parser, per-host pace 0.3 req/s (`COLLECTOR_RPS_DEFAULT`),
glotr.uz `Crawl-delay: 10` honoured, breaker armed. No browser User-Agent, no extra headers,
no cookies, no reveal/XHR call was made anywhere.

- **Date:** 2026-10-29 (Asia/Tashkent), every file.
- **Recorded from:** build sandbox (US datacenter IP via egress proxy) — re-check from the VPS
  with `make smoke` before `sources.enabled`. A site may answer the VPS differently (prom.uz
  refused this IP outright, see below).
- **Scrubbed:** every file — `--scrub on`, then a second and (after review) a third pass for
  person data the scrubber cannot see (section «Scrubbing»). Firm names, legal entities'
  STIRs, addresses and rubrics are kept (company tier). Full phones in the files are
  synthetic `+998 90 000 00 NN` shapes (written the way each site writes them), e-mails
  `namuna_NN@example.uz`, handles `namuna_NN` (the directories' own Telegram bots stay
  verbatim, «Scrubbing» item 3), the person names found `Seller NN`, the one
  individual entrepreneur's STIR `400000001`. Masked phones: goldenpages' `+998 (NN) NNN-XX-XX`
  rewritten to `+998 (90) 000-XX-XX`; yellowpages' `+998NN ...` / `NN Позвонить` keep the
  2-digit area/operator code as served (not a number; it does not match the synthetic
  payload number of the same row); top.uz's `+998...` carries no digit.
- **robots.txt as the recorder read it:** kept in `robots/` (section «robots.txt»).

## Files

| File | Source URL | HTTP | robots.txt for this path | What it proves |
|---|---|---|---|---|
| `goldenpages_rubric_1424_p1.html` | https://www.goldenpages.uz/rubrics/?Id=1424 | 200 | allowed (`*` group, no Crawl-delay) | rubric list page 1: 20 cards, «Найдено организаций: 74», masked phones, importer/exporter/manufacturer/distributor badges, pagination links `&Page=N` |
| `goldenpages_rubric_1424_p2.html` | https://www.goldenpages.uz/rubrics/?Id=1424&Page=2 | 200 | allowed | page 2 = 20 different company ids → the pagination parameter is `Page` (capital P) |
| `goldenpages_rubric_1424_p99_negative.html` | https://www.goldenpages.uz/rubrics/?Id=1424&Page=99 | 200 (after a redirect; final URL `/rubrics/?Id=1424`) | allowed | negative / paginator stop: a page past the last redirects to page 1 and repeats its first card id |
| `goldenpages_rubric_106080_p1.html` | https://www.goldenpages.uz/rubrics/?Id=106080 | 200 | allowed | «Оборудование из Китая» rubric: 8 organisations on one page (China-origin rubric cue) |
| `goldenpages_company_85641.html` | https://www.goldenpages.uz/company/?Id=85641 | 200 | allowed | company card: masked visible phone + «Показать телефон» XHR button, but full phones and e-mails server-rendered in JSON-LD; «Обновлено» date; masked INN; badges |
| `yellowpages_rubrika_kanctovary_tashkent_p1.html` | https://www.yellowpages.uz/rubrika/optovaya-torgovlya-kanctovarami-kancelyarskimi-tovarami/tashkent | 200 | allowed (`*` group) | rubrika list: 15 of 25 organisations; full contacts (phones, faxes, e-mails, websites, rubrics, tags, inn) in the server-rendered Nuxt payload; phone masked in the HTML |
| `yellowpages_rubrika_kanctovary_tashkent_p2.html` | …/tashkent?page=2 | 200 | allowed | probe: `?page=2` is **ignored** by the server render (payload `current_page` = 1, same 15 slugs) — the page-2 parameter is not `page` |
| `yellowpages_kompaniya_alta_trade_ooo.html` | https://www.yellowpages.uz/kompaniya/alta-trade-ooo | 200 | allowed | company card: phone masked in HTML («71 Позвонить»), full `contacts` in the payload; e-mail, site, rubrics rendered |
| `yellowpages_teg_tovary_iz_kitaya.html` | https://www.yellowpages.uz/teg/tovary-iz-kitaya | 200 | allowed | `/teg/` seed: total = 1 organisation, and it is a logistics firm (a competitor shape, not an importer) |
| `yellowpages_kompaniya_missing_negative.html` | https://www.yellowpages.uz/kompaniya/namuna-net-takoj-kompanii-0 | 404 | allowed | negative: unknown slug → real 404 page |
| `topuz_stroymaterialy_optom_p1.html` | https://top.uz/section/stroymaterialy-optom | 200 | allowed (`*` group — the same 69 rules as the `Yandex` group, see «robots.txt») | section list page 1: 20 rows, payload `meta.total` = 231 (= organiser's figure), `last_page` 12; HTML phone masked «+998...», payload `phones[].phone` full for 20/20 |
| `topuz_stroymaterialy_optom_p2.html` | https://top.uz/section/stroymaterialy-optom?PAGEN_1=2 | 200 | allowed | `PAGEN_1=2` → payload `current_page` 2, 20 new slugs |
| `topuz_stroymaterialy_optom_p99_negative.html` | https://top.uz/section/stroymaterialy-optom?PAGEN_1=99 | 200 | allowed | negative / paginator stop: `current_page` 99, `data` = [] (no redirect, no repeat) |
| `topuz_optovaya_torgovlya_p1.html` | https://top.uz/section/optovaya-torgovlya | 200 | allowed | payload `meta.total` = 815 (= organiser's figure), `last_page` 41 |
| `topuz_company_uzbek_chinese_trading_house.html` | https://top.uz/company/international-business-and-travel-sp-ooo-uzbeksko-kitayskiy-torgovyy-dom-predstavitelstvo | 200 | allowed | company card: visible «+998...Позвонить», the full number in `a[href^="tel:"]` and in the payload; description names Chinese manufacturers |
| `glotr_oborudovanie_p1.html` | https://glotr.uz/ru/oborudovanie/ (redirected to https://glotr.uz/oborudovanie/) | 200 | allowed, `Crawl-delay: 10` | category page 1: «Найдено 39 162 предложений», 56 product cards each with its seller link `/showcase/<slug>[-<id>]/` (50 of them end in `?s=1`, 6 carry no query); brand filters `/oborudovanie/<brand>/`; pagination links `?page=N` |
| `glotr_showcase_jack.html` | https://glotr.uz/showcase/jack/ | 200 | allowed | seller showcase: JSON-LD LocalBusiness with telephone, e-mail, address, geo, `sameAs` (Instagram/Facebook/Telegram); «Проверено … Soliq.uz» text; 27 product cards; official distributor of a Chinese brand in the description |
| `glotr_showcase_missing_negative.html` | https://glotr.uz/showcase/namuna-net-takogo-prodavca-0/ (redirected to https://glotr.uz/showcases/) | 200 | allowed | negative: an unknown showcase is a soft 404 — a redirect to the partners index («Наши партнеры»), status 200 |

Not kept: `glotr …/oborudovanie/?page=2` (refused by robots.txt before any request), the prom.uz
block page, and the eight other yellowpages `/teg/` pages (status only, below).
Kept beside them: the five robots.txt files the verdicts were read from (`robots/`, section
«robots.txt»).

**Sidecars.** Every `.html` above has a `<name>.headers.json` written by the recorder (18 in
all): `url` (as requested), `final_url` (after redirects), `status`, `fetched_at` and the
response headers (no `set-cookie`). Three VERIFIED answers below rest on a sidecar, not on
the body: the goldenpages paginator stop (`goldenpages_rubric_1424_p99_negative.headers.json`),
glotr's `/ru/` → `/` redirect (`glotr_oborudovanie_p1.headers.json`) and glotr's soft 404
(`glotr_showcase_missing_negative.headers.json`). `fetched_at` also dates each recording (see
«Scrubbing» item 5 for the re-recorded batch).

### Requests made (HTTP requests sent, robots.txt and redirect hops included)

| Host | Requests | Note |
|---|---|---|
| www.goldenpages.uz | 13 | robots 1; 10 recorder calls (5 pages recorded twice — the first copies lost their CRs to my own second-pass script, see «Scrubbing»); 2 redirect hops (Page=99 → page 1) |
| www.yellowpages.uz | 14 | robots 1; rubrika p1 + `?page=2` probe + card + negative; the 9 `/teg/` seeds the task asked to check |
| top.uz | 11 | robots 1; 5 pages, recorded twice (in the same 20:01 re-record batch; the stored top.uz pages have no CRs to lose — see «Scrubbing» item 5) |
| glotr.uz | 7 | robots 1; category p1 (+1 redirect hop /ru/ → /), showcase (twice — re-recorded in the 20:01 batch with the goldenpages/top.uz pages, see «Scrubbing» item 5), negative (+1 redirect hop); page 2 refused locally, nothing sent |
| prom.uz | 2 | robots.txt 403, then `/tags/optom/` 403 — stopped after the second (see below) |

No 429, no captcha, no 403 except prom.uz. The breaker never opened.

**prom.uz got one request more than it should have.** Its first answer was the 403 on
robots.txt. The Fetcher reads a 4xx robots.txt as «no rules» (03 §1.1, RFC 9309 §2.3.1.3),
and that answer counts neither as a strike nor toward the breaker; my driver stopped a host
only on a 403/429 for a *page*, and never saw the robots status. So the recorder went on and
sent the page request, which was refused too, and only then was the host stopped. The
robots cache file (`robots/prom.uz.txt`, header only) records the 403; in the Fetcher's
24 h cache it reads as «allow everything», so a Fetcher that reused that cache dir would
have crawled prom.uz with no rules until the breaker's page-403 streak opened. That cache
dir was this session's scratch, not `/data/http-cache/robots/`. Gap for a code change
(not made here): a 401/403/429 or a WAF block page on robots.txt should stop the host
for the run like a page refusal does.

### yellowpages `/teg/` seeds (status checks through the recorder, not kept)

| Seed | HTTP | Organisations in the payload (`meta.total`) |
|---|---|---|
| `/teg/tovary-iz-kitaya` | 200 | 1 (kept as a fixture; a logistics firm) |
| `/teg/mebel-iz-kitaya` | 200 | 2 |
| `/teg/mebel-optom` | 200 | 5 |
| `/teg/optovaya-torgovlya-hoztovarami` | 200 | 1 |
| `/teg/optovaya-torgovlya-strojmaterialami` | 200 | 40 (3 pages of 15) |
| `/teg/optovaya-torgovlya-ofisnymi-tovarami` | 200 | 1 |
| `/teg/optovaya-torgovlya-bumagoj` | 200 | 1 |
| `/teg/optovaya-prodazha` | 200 | 8 |
| `/teg/optovaya-torgovlya-v-uzbekistane` | 200 | 1 |

Only the first row is backed by a file in this folder. The other eight rows are what the
recorder's status checks returned on 2026-10-29; their bodies stayed in the build session's
scratch directory and are not in the repo, so those eight statuses and totals (and the sum
of 60 organisations, with overlaps) cannot be re-checked from here. As observed, tags are
self-applied labels and sparse, as `manbalar.json` [v 23] predicted: rubrika pages are the
primary seed, tags a supplement only.

## Day-1 answers (03 §2.6)

### goldenpages.uz

- **Pagination parameter — VERIFIED: `&Page=N` (capital P), not `&page=N`.** Links
  `<a href="/rubrics/?Id=1424&amp;Page=2" data-page="1">` (0-based `data-page`), next link in
  `li.gp_next a` (`goldenpages_rubric_1424_p1.html`); page 2 has 20 different ids
  (`goldenpages_rubric_1424_p2.html`). Lowercase `&page=` was not tried — use the link as printed.
  20 cards per page. robots.txt does not disallow `Page`; it disallows `*&s=*`, `*&tab=*`,
  `*&region=*`, `*/?sort=*`, `*/search/*`, `*/person/*`, `*/maps/*` among others.
- **Paginator stop — VERIFIED.** `&Page=99` redirects to `/rubrics/?Id=1424` and serves page 1
  again (`goldenpages_rubric_1424_p99_negative.html`; the final URL without `Page` is in
  `goldenpages_rubric_1424_p99_negative.headers.json`: `url` `…&Page=99`, `final_url`
  `https://www.goldenpages.uz/rubrics/?Id=1424`). Stop when the
  final URL has no `Page` or the first card id repeats (the spec's rule works).
- **Phones server-rendered? — VERIFIED: not in the visible markup, yes in JSON-LD on the card.**
  List and card show the number masked as `+998 (NN) NNN-XX-XX` (in the stored files every
  mask reads `+998 (90) 000-XX-XX` — the served code and first three digits were rewritten by
  the third scrub pass) with a «Показать телефон» button
  (`onclick="loadDataByOrg('<hash>', '#PhonesByOrg_<id>')"` — an XHR, not called). The card's
  `<script type="application/ld+json">` FAQPage carries the Question «Номер телефона … ?» whose
  `acceptedAnswer.text` lists every number, `;`-separated, written `(+998NN) NNNNNNN`, and the
  LocalBusiness `email` lists the e-mails (`goldenpages_company_85641.html`). So the phone comes
  from the card, one request per company, no headless browser. The list page's JSON-LD has 20
  LocalBusiness items (name, address, `sameAs` = card URL, e-mail, slogan, lat/long) but **no
  telephone** (`goldenpages_rubric_1424_p1.html`).
- **Selectors (list) — VERIFIED.** One card = `section.gp_company`; name `h3.h3 a[href^="/company/?Id="]`;
  second name `div.gp_name_legal`; address `div.gp_wrap_address p`; activities
  `div.gp_td_uL9y ul li`; badges `.tooltip__icon img.sub__icons` with `alt` «Импортер»,
  «Экспортёр», «Производитель», «Дистрибьютор» (`src` `/icons/rubrics/importer.svg`, `exporter.svg`,
  `proizvoditel.svg`, `distributor.svg`; on p1: 3 importer, 10 exporter, 9 manufacturer,
  4 distributor); count `span.gp_numberTitle` after «Найдено организаций:»; social/website links
  are opaque redirects `/go/?u=<hash>` with `title` «Facebook»/«Instagram»/«Telegram»/«Перейти на сайт»
  (the target URL is not in the page).
- **Selectors (card) — VERIFIED.** `h1.h3` = name + «(City)»; badges as on the list; INN shown
  masked in `div.inn__number` («30452****» — not usable as a STIR); address
  `div.gp_card__adresses p` with `/orgbyindex/?Id=`, `/city/?Id=`, `/district/?Id=`,
  `/street/?Id=` links; updated date `span.gp_com_date` «Обновлено: dd.mm.yyyy» (the «drop
  < 2024» rule is implementable); hours in the FAQPage answer and in `openingHoursSpecification`.
- **Bot trap (honeypot links) — VERIFIED on every goldenpages page, rubric pages included.**
  Each of the five files carries two hidden wrappers, each with two links under `/gp/`
  (`rel` with `nofollow`/`noindex`, the anchors themselves hidden by inline style too):
  rubric pages link `/gp/list/company/` + `/gp/items/page/` and `/gp/nav/services/` +
  `/gp/menu/contacts/`; the company card links `/gp/item/company/` + `/gp/card/detail/` and
  `/gp/nav/services/` + `/gp/menu/contacts/`. **The wrapper tag and its inline hiding style
  change on every response**, even for the same content: rubric 1424 p1 `<div>` (`left:-9999px`,
  1×1 px) + `<nav>` (`visibility:hidden`); p2 two `<section>`s; the p99 page (same 20 cards as
  p1) `<p>` (`clip:rect…`) + `<span>` (`visibility:hidden`); rubric 106080 `<nav>` + `<span>`;
  company 85641 two `<nav aria-hidden="true">` (`clip:rect(0 0 0 0)`, 1×1 px). The only constant
  is `aria-hidden="true"` on the wrapper and the `/gp/` path prefix. robots.txt **allows**
  every one of these paths (checked with `parse_robots` against `robots/www.goldenpages.uz.txt`),
  so robots will not protect us. Rule for the collector, on rubric pages as much as cards:
  follow only `/rubrics/?Id=…&Page=…` and `/company/?Id=…` hrefs taken by the selectors above,
  never a link discovered by walking the page, and never any `/gp/…` path. A test for the
  trap must not key on one tag or style; assert «no `/gp/` URL is ever queued» over all five
  files.
- **Organiser figures.** Rubric 1424 now holds 74 organisations (`manbalar.json` said 71).
  Rubric 106080 «Оборудование из Китая» holds 8, all on one page.
- **The page's own help-desk numbers** are four `tel:` links on every goldenpages file: one in
  `<header id="js-header">`, two in the `<footer>`, and one in the mobile menu
  (`div.menu > div.menu__inner#menu-nav`, beside `t.me/gpuzbot`), which is inside neither. A
  filter that drops only header/footer `tel:` links still picks the menu one up; a parser must
  read phones only from the company block / JSON-LD.
- **STILL UNVERIFIED:** whether the card JSON-LD phone list is complete for cards with many
  numbers (one card seen, 2 numbers); `/street/?Id=41003` and rubrics 1277, 1558, 1029, 1686,
  1800 not fetched.

### yellowpages.uz

- **Rendering — VERIFIED: Nuxt 3 SSR.** Everything a parser needs is in
  `script#__NUXT_DATA__` (`data-ssr="true"`), devalue-encoded (a flat JSON array; objects hold
  integer indices into it; `["ShallowReactive", i]`/`["Reactive", i]` wrap values). The list is the
  object with keys `data`, `links`, `meta`, `seo`, `slugs`; `meta` = `current_page`, `last_page`,
  `per_page` (15), `total`. Each organisation: `id, name, slug, brand_name, official_name, inn,
  created_at, updated_at, address` (HTML with `a.addr_link` to `/oblast/…`, `/gorod/…`,
  `/rayon/…`, `/ulica/…`), `landmark, latitude, longitude` (comma decimals), `city_slug`,
  `rubrics[{id,slug,name}]`, `tags[{id,slug,name}]`, `contacts{phone[], emails[], websites[],
  telegram[], faxes[], instagram[], facebook[], linkedin[]}` (`yellowpages_rubrika_kanctovary_tashkent_p1.html`).
  A parser needs a ~20-line devalue resolver; the HTML alone is not enough.
- **Phones — VERIFIED: masked in the HTML, full in the payload.** List HTML prints «+99871
  ...Позвонить» (a button), card HTML «71 Позвонить»; faxes and e-mails are printed in full;
  `contacts.phone[]` holds the full numbers on both list and card
  (`yellowpages_kompaniya_alta_trade_ooo.html`). No reveal request exists or was made — the
  number is in the page as served. The masked HTML keeps the served 2-digit code (`+99871 ...`),
  while the payload number of the same row is the synthetic `90 00000NN`: a test must not
  expect the two to agree.
- **Rows with only a fax or an e-mail — STILL UNVERIFIED.** Every recorded row has at least
  one `contacts.phone[]` entry: 15 of 15 on the rubrika page (p2 is the same 15), the one row
  on `yellowpages_teg_tovary_iz_kitaya.html`, and the card. 7 of the 15 rubrika rows have no
  fax, 1 has no e-mail; no row is phone-less, so the fax/e-mail-only branch has no fixture.
  One rubrika row has `null` instead of `[]` for `telegram`, `instagram`, `facebook` and
  `linkedin` — the parser must accept both.
- **List selectors — VERIFIED, corrected.** `div.custom-card` alone is **not** the
  organisation card: it matches 22 blocks on the rubrika page (15 organisations + the
  pagination box, four `mt-6` blocks, one `mt-[10px]` block and one grid block — «Компании из
  рубрики», SEO text, FAQ, statistics) and 5 on the teg page (1 organisation). The organisation
  card is `div.custom-card.border-2` (15 on p1/p2, 1 on the teg page), i.e. the `custom-card`
  that contains `h4 > a.grid__card__link[href^="/kompaniya/"]` — that name selector matches
  exactly 15 (and 1 on the teg page) and is the one to iterate. Name text is prefixed «N. »;
  «Юридическое название:» span. Card: E-mail and «Сайт:» `h3` blocks, rubrics
  `#rubrics a[href^="/rubrika/"]`. The payload's `meta` agrees: `total` 25, `per_page` 15,
  `data` 15.
- **STIR — VERIFIED present as `inn`** on 7 of the 15 rubrika rows, empty (`""`) on 8. As
  stored: one real legal-entity STIR kept (`313…`, Nuqta Stationery); five rewritten by the
  first pass as phones (`9000000NN` — they start like a mobile code); and the individual
  entrepreneur's taxpayer number (the «ИндП» row, a natural person's TIN — it would name the
  person the name scrub removed) replaced by the synthetic `400000001`. A parser test may
  assert the field and its position, never these values as real STIRs.
- **Pagination — STILL UNVERIFIED.** The pagination block is rendered client-side (empty
  `div.custom__pagination` in the SSR HTML, no `href` to page 2 anywhere), and `?page=2` is
  ignored by the server render (`yellowpages_rubrika_kanctovary_tashkent_p2.html`: payload
  `current_page` 1, same slugs, path echoed with `?page=2`). The payload's own `links` name an
  internal API, relative `api/v1/organizations?per_page=15&page=N&lang=ru&rubric=<slug>&location=<city>&location_type=city`
  (tags: `…&tag=<slug>`), host not stated in the page. Not called: undocumented API, robots of
  its host unknown. Day-1 on the VPS: open a 2-page rubric in a browser, read the address bar
  after «next», record page 2 with the recorder. Until then only page 1 (15 rows) per
  rubric/city is reachable; `pagesize=` is disallowed by robots.txt.
- **`/teg/` seeds — VERIFIED for `tovary-iz-kitaya` only** (`yellowpages_teg_tovary_iz_kitaya.html`:
  200, `meta.total` 1): it lists a single firm, a logistics/cargo company — `china_origin` from
  this tag must pass the competitor filter first. The other eight seeds — STILL UNVERIFIED from
  the repo: they answered 200 with the totals in the table above when checked through the
  recorder, but those bodies were not kept, so no file shows it (re-record them on the VPS if a
  test or a seed list is to rest on them).
- **Negative — VERIFIED:** an unknown `/kompaniya/` slug is a real 404 (`yellowpages_kompaniya_missing_negative.html`).
- **STILL UNVERIFIED:** city sub-paths other than `/tashkent`, the `/en/rubric/` mirror, the
  rubric index used to pick slugs.

### top.uz

- **Rendering — VERIFIED: the same Nuxt platform as yellowpages** (devalue `__NUXT_DATA__`,
  list object `data`/`links`/`meta`, `per_page` 20). Organisation keys: `id, name_ru, name_uz,
  slug, description_ru, address_ru, landmark_ru, lat, long, index, email, instagram, facebook,
  tg_channel, tg_contact, fax, site, phones[{id, organization_id, phone}], sections[{id,
  name_ru, slug, parent_id, children}], reviews, rating, worktime_ru, created_at, updated_at`,
  plus `prom_id`, `prom_uuid`, `yellow_id`, `yellow_link`, `inside_source` («prom.uz» on 11 of
  20 rows of p1 — top.uz imports part of its book from prom.uz).
- **Pagination — VERIFIED: `?PAGEN_1=N`.** p2 payload `current_page` 2, 20 new slugs
  (`topuz_stroymaterialy_optom_p2.html`); HTML links `/section/stroymaterialy-optom?PAGEN_1=N`.
  robots.txt's `*` group allows it — but that group is **not** permissive: it repeats the
  `Yandex` group's 69 rules line for line, and they bind clientScrapperBot (checked with
  `parse_robots` against `robots/top.uz.txt`). Refused for `*`, among others: `*?p=`
  (so `/section/stroymaterialy-optom?p=2` is out — matching is case-sensitive, `?PAGEN_1=` is
  in), `*SORT=`, `*TYPE=`, `*SECTION_ID=`, `*BRAND=`, `*?q=`, `*?tags=`, `*?FILTER*=`,
  `*utm*=`, `*from=`, `*/search/`, `/personal/`, `/auth/`. A collector must take page links as
  printed (`?PAGEN_1=N`, nothing appended) and not add sort, filter or tracking parameters.
  No `Crawl-delay`.
- **Paginator stop — VERIFIED.** `PAGEN_1=99` (past `last_page` 12) answers 200 with
  `current_page` 99 and `data: []` (`topuz_stroymaterialy_optom_p99_negative.html`); stop on an
  empty `data` or when `current_page > last_page`.
- **Row counts vs the organiser's figures — VERIFIED:** stroymaterialy-optom `total` 231 (= 231),
  optovaya-torgovlya `total` 815 (= 815), 41 pages (`topuz_optovaya_torgovlya_p1.html`).
- **Phone masked in the list, shown on the card — VERIFIED, with a correction.** The list HTML
  shows «+998...» + a «Показать» button; the card shows «+998...Позвонить» but its
  `a[href^="tel:"]` carries the full number (`href="tel: +998(…)…"`, note the space)
  (`topuz_company_uzbek_chinese_trading_house.html`). **Correction:** the list page's payload
  already carries `phones[].phone` in full for 20 of 20 rows (`topuz_stroymaterialy_optom_p1.html`),
  so the card request is not needed for the phone. Whether the parser may take it from the list
  payload (as served, no reveal call) or must take it from the card is a decision for the
  spec/owner — recorded here, not decided.
- **Category filter:** `sections[].slug` gives the `*-optom` test for `directory_wholesale`.
- **STILL UNVERIFIED:** regional `/tashkent-obl`, `/section/prodazha-stroitelnykh-materialov`.

### glotr.uz

- **robots.txt — VERIFIED, and it changes the plan.** `User-agent: *` disallows `/*?page=` and
  `/*&page=` (also `?catid=`, `?term=`, `?region=`, `?sort=`, `?price_type=`, `/search`) and sets
  `Crawl-delay: 10`. The category pagination is exactly `?page=N` (links on p1), so **page 2+
  of every category may not be crawled** — the recorder refused `…/oborudovanie/?page=2` before
  sending anything. Reachable: page 1 of each category, region sub-categories
  (`/oborudovanie-v-tashkente/` …) and brand filters (`/oborudovanie/<brand>/`, e.g. Chinese
  brands hangcha, shimge, midea, haier on p1) — each its own page 1. The spec's «category →
  products grouped by seller» crawl must be rebuilt on those entry points.
- **Category page — VERIFIED.** «Найдено 39 162 предложений» (manbalar: ~38k); 56
  `div.product-card__parent` per page; product link `a.product-card__swiper[href$="-p-<id>/"]`;
  seller `div.product_company a.product_company_link[href^="/showcase/"]` (56 links; 50 end in
  `/?s=1`, 6 have no query string — `/showcase/ooo-mezon-plast-11871/`,
  `/showcase/ooo-imagine-7309/`, `/showcase/kans-profil-36855/`,
  `/showcase/ooo-kans-art-sales-8109/`, `/showcase/joymax-35067/`,
  `/showcase/texnosaruz-28535/`, the last one also present once with `?s=1`: do not require
  `?s=1`, key a seller on the path with the query dropped) and an `i.fa-badge-check` icon on
  55 of 56 sellers (what that icon certifies is STILL UNVERIFIED); `/ru/` URLs redirect to the
  unprefixed path (`glotr_oborudovanie_p1.headers.json`: `url` `/ru/oborudovanie/`,
  `final_url` `/oborudovanie/`).
- **Showcase — VERIFIED.** `script[type="application/ld+json"]` LocalBusiness: `name`,
  `description`, `telephone`, `email`, `address{streetAddress, addressRegion, addressLocality}`,
  `geo`, `openingHoursSpecification`, `aggregateRating`, `sameAs` [Instagram, Facebook, t.me];
  the visible phone is masked «+998 (показать номер)», so JSON-LD is the source
  (`glotr_showcase_jack.html`). «Проверено — Информация о данном продавце была проверена через
  систему Soliq.uz» is printed in the page → `entity_solid` is readable. 27 product cards on the
  showcase (no page-2 links); «≥ 20 SKUs» holds for this seller on page 1 alone; a total SKU count
  is not printed (STILL UNVERIFIED for sellers with more products).
- **Negative — VERIFIED:** an unknown showcase is a soft 404 — 200 at `/showcases/`
  («Наши партнеры», 48 showcase links) (`glotr_showcase_missing_negative.html`; the redirect and
  the 200 are in `glotr_showcase_missing_negative.headers.json`: `final_url`
  `https://glotr.uz/showcases/`, `status` 200). The parser must
  check the final URL / `h1`, not the status.
- **STILL UNVERIFIED:** the spec's named showcases (China Motors, Uzbek-Chinese Trading House,
  XITOY UPAKOVSHIK, TAO.UZ) — their URLs were not linked from the recorded page and were not
  guessed; categories `/mebel/`, `/sanoat-uskunalari/`.

### prom.uz

- **Refused by the site (Cloudflare WAF) — every Day-1 item STILL UNVERIFIED.** robots.txt
  answered 403 and `/tags/optom/` answered 403 «Sorry, you have been blocked … This website is
  using a security service to protect itself» (`server: cloudflare`). Stopped on the host after
  that; no retry, nothing routed around. The tag page, a company page `/uz/company/<slug>/<cat>/`
  and a product card with «страна производства: Китай» were **not** recorded. Re-check from the
  VPS; if it is refused there too, prom.uz is off for v1 (top.uz's `inside_source: prom.uz`
  rows show that part of its book is published on top.uz anyway).

## Scrubbing

1. **First pass:** the recorder's own `--scrub on` (phones → `+998 90 000 00 NN` digit for
   digit, e-mails, handles, person-shaped text nodes → `Seller NN`).
2. **Second pass** (a script over the stored files, using `Scrubber.name()` so the labels are
   the scrubber's `Seller NN`, numbered after the highest `Seller NN` already in each file;
   bytes otherwise untouched, line endings kept): names the first pass cannot see —
   - an individual entrepreneur's name before «ИндП» (`_FORM` knows `IP|ИП|YaTT|ЯТТ`, not «ИндП»):
     2 names in 3 places in the top.uz card, 1 name in 2 places in each yellowpages rubrika
     page; the card's `<surname>-<i>-<i>-indp` slug rewritten to `seller-NN-indp` (4 places);
   - single-token review authors (first names) in top.uz's review widget (HTML `h3` and the
     payload's `username` values — devalue separates the key from its value, so the
     `username`-key rule never fires) and in glotr's showcase reviews (`<b>` before the date),
     plus a self-introduction inside one review text («Я <name>»).
   Counts per file are in the ledger notes below. The second pass covered top.uz and glotr
   reviews only; it **missed** the yellowpages site-wide review carousel (third pass).
3. **Third pass** (after review; same script rules — `Scrubber.name()`, numbering after the
   file's highest `Seller NN`, bytes otherwise untouched):
   - `yellowpages_kompaniya_alta_trade_ooo.html`: the site-wide «reviews» carousel
     (`div.main-compilation__item`, reviews of other firms) printed its authors' names —
     one first name + patronymic and three first names (four people, 9 places): the visible
     `<p>NAME dd.mm.yyyy hh:mm:ss</p>` and the payload's review objects
     `{reviewable_type, reviewable_id, name, email, content, …}` → `Seller 05`…`Seller 08`
     (their e-mails were already `namuna_NN`). The review texts were read: they name no person.
   - `yellowpages_rubrika_kanctovary_tashkent_p1/p2.html`: the «ИндП» row's `inn` is a natural
     person's taxpayer number; a registry lookup of it would return the name the second pass
     removed → `"400000001"` (1 place per file).
   - the five goldenpages files: masked phones `+998 (NN) NNN-XX-XX` kept 5 real digits of a
     listed number and contradicted the synthetic JSON-LD number of the same card → all 69
     masks rewritten to `+998 (90) 000-XX-XX` (consistent with the card's synthetic
     `(+99890) 0000001`).
   - `glotr_showcase_jack.html`: one reviewer's uploaded profile photo
     (`files.glotr.uz/user/<user id>/photo/…`) → `/static/img/user-avatar.png`, the generic
     avatar the other five reviewers already carry.
   Afterwards, by script over the whole folder (`robots/` included): no phone outside the
   synthetic range, no e-mail outside `example.uz`, no Instagram handle other than `namuna_NN`,
   no t.me handle other than `namuna_NN` and the directories' **own** Telegram bots, which stay
   verbatim as company-tier (`t.me/gpuzbot` in the 5 goldenpages files, `t.me/topuz2_bot` in
   the 5 top.uz files, `t.me/yellowpages_support_bot` in the 2 rubrika files — a parser must not
   take them as a listed firm's handle), no `files.glotr.uz/user/` link, no masked prefix with real digits, no
   patronymic, no name beside a review date other than `Seller NN`, none of the names found
   in any pass remains (see «Final check»).
4. **Scrubber gaps found (for a code fix, then re-scrub; nothing in code was changed here):**
   - «ИндП» (and probably «ИнП») missing from `_FORM`.
   - Nuxt devalue payloads: key and value live in different array slots, so every key-based
     rule (`username`, a review's `name`, STIR-labelled keys) is blind there. Consequence for
     tests: on the yellowpages list, every `inn` that starts like a mobile code was rewritten
     as a phone (`"9000000NN"`); of the two others one is a legal entity's STIR (kept) and one
     was an individual entrepreneur's TIN (now synthetic) — a parser test must not assert any
     of these values as real STIRs.
   - An individual entrepreneur's STIR/TIN is person data, but the scrubber keeps every
     STIR-labelled value; the row's legal form (`ИП`/`ИндП`/`YaTT`) should decide.
   - Single-token person names (review authors) are not person-shaped to `_written_name`, and
     a text node «Name dd.mm.yyyy hh:mm:ss» is not a whole person-shaped node either.
   - A reviewer's photo URL (`…/user/<id>/photo/…`) is not recognised as person data.
   - Digit runs inside SVG path data (`d="…"`) are sometimes rewritten as phones (cosmetic;
     5 goldenpages and 5 top.uz files, one or two paths each).
   - Masked phones are left as served: goldenpages' 5-digit masks were fixed by hand (above);
     yellowpages' `+99871 ...` (2-digit code only) and top.uz's `+998...` stay.
5. My own mistake, recorded: the first version of the second-pass script read and wrote text
   with universal newlines and dropped the CRs of the goldenpages pages (96 on p1). Eleven
   files were therefore re-recorded in one batch (same URLs, `fetched_at`
   2026-10-29T20:01:12–20:01:31 in their sidecars): the five goldenpages files, the five top.uz
   files and `glotr_showcase_jack.html`; the fixed script was re-applied to them. The other
   seven files keep their first recording (19:45–19:53). Only the goldenpages pages contain CRs
   (46–96 per stored file); the stored top.uz and glotr pages contain none, so the CR loss
   explains the goldenpages re-records only. Why the top.uz files and the glotr showcase went
   into the same batch is not recorded in the recorder's notes and is not claimed here (the
   yellowpages pages the second pass also changed were not re-recorded). The ledger's
   sha256/bytes describe the files as stored now.

## Refused / not recorded

- **prom.uz** — Cloudflare 403 on robots.txt and `/tags/optom/` (site protection). Block page
  not kept (its body prints the requesting IP). No company page, no product card. The page
  request was one too many: it was sent after the robots.txt 403 (see «Requests made»); the
  host stays refused and was not touched again.
- **glotr.uz page 2+** — `Disallow: /*?page=` for `*`; refused by the recorder's robots check,
  no request sent.
- **yellowpages page 2** — no page-2 URL found in the server render; `?page=2` ignored; internal
  API not called.
- **uzts.uz, openinfo.uz (and new.openinfo.uz), uzpharm-control.uz, uzpharm-gxp.uz** — host not
  in the allowlist; not touched.
- **goldenpages** `/street/?Id=41003`, rubrics 1277, 1558, 1029, 1686, 1800; **top.uz** regional
  pages; **yellowpages** other cities and `/en/rubric/`; **glotr** named seed showcases — not
  recorded (budget of ~12 requests per host; not needed to answer the Day-1 lines above).
- The goldenpages phone XHR (`loadDataByOrg`), the yellowpages/top.uz `api/v1/organizations`
  APIs and glotr's «показать номер» — never called.
- **Left open after review, because closing them needs a new recording (none was made in the
  close-out):** the eight yellowpages `/teg/` bodies other than `tovary-iz-kitaya` (status
  checks only, not kept — their statuses and totals rest on no committed file); a yellowpages
  row with only a fax or an e-mail (no recorded row has one, so that parser branch has no
  fixture); the robots.txt-403 stop for prom.uz is a Fetcher code change, not a fixture.

## robots.txt (`robots/`)

The five files are the Fetcher's robots cache as written during the run (one header line
`# clientScrapper robots fetched_at=… status=…`, then the body as the Fetcher decoded it),
copied here so every «robots.txt for this path» verdict above can be re-checked without a
new request: `parse_robots(text, "clientScrapperBot")` from `services/robots.py`.
Re-checked on 2026-10-29 against these copies: all 18 recorded URLs, the 3 redirect
targets and the 8 unkept `/teg/` probes are allowed; `glotr …/oborudovanie/?page=2` is
disallowed (`/*?page=`). A fresh fetch is still the rule on the VPS (24 h cache).

| File | fetched_at (Asia/Tashkent) | status | sha256 | What it shows |
|---|---|---|---|---|
| `robots/www.goldenpages.uz.txt` | 2026-10-29T19:45:09 | 200 | `dce82d22d61156207515e7a464bd637c83d87d4f8195450028f2624b2af39077` | `*` group: `*&s=*`, `*&tab=*`, `*&region=*`, `*/?sort=*`, `*/search/*`, `*/person/*`, `*/maps/*` … disallowed; `Page`, `/company/`, `/gp/…` allowed; no Crawl-delay |
| `robots/www.yellowpages.uz.txt` | 2026-10-29T19:45:14 | 200 | `90f86343c896e46c87c2f214fcafaa6163707842d160f266a53696d5f21bb216` | `*` group: `*?key=`, `*pagesize=`, `/user/`, `/account/`, `/norma/`, `/content/`, `/data/` disallowed; the `Yandex` group has only `Clean-param` lines |
| `robots/top.uz.txt` | 2026-10-29T19:45:19 | 200 | `9f25167eae3e311360d1ea22f4296aee5b82483578968650c272d21fd360894b` | a `Yandex` group and a `*` group with the **same** 69 Disallow/Allow lines (`*?p=`, `*SORT=`, `*TYPE=`, `*utm*=`, `*/search/` …); `?PAGEN_1=` allowed. The Cyrillic `#` comments are mojibake (the server sent no charset and the cache decoded UTF-8 as Latin-1); rules are ASCII and unaffected |
| `robots/glotr.uz.txt` | 2026-10-29T19:45:23 | 200 | `94842314f546462d652ba54815161bb48ce73a5e619142d1bbd0350ef38c7745` | `*` group: `/*?page=`, `/*&page=`, `?catid=`, `?term=`, `?region=`, `?sort=`, `?price_type=`, `/search` disallowed, `Crawl-delay: 10`; `ClaudeBot`, `GPTBot`, `CCBot` … `Disallow: /` (not our token) |
| `robots/prom.uz.txt` | 2026-10-29T19:45:46 | 403 | `62d223ffd8d764eb2b26c08b896b86de5a8a0b5fa5ab8bfdf2a5bf71dc38a6fd` | header only: the Cloudflare 403. The Fetcher treats it as «no rules» — see «Requests made» |

## Final check (2026-10-29, after the third pass)

A script over every file in this folder and `robots/` (raw text and HTML-unescaped), with the
repo's own patterns (`scripts/secret_scan.py` `PHONE`/`EMAIL`/`synthetic()`,
`collectors/keywords.PHONE_RE`) plus: t.me / instagram.com / `@handle` other than
`namuna_NN` (as worded this rule matches the three directory bots named in «Scrubbing» item 3,
14 occurrences, so the «hits: 0» below holds only with them excluded as company-tier);
`files.glotr.uz/user/`; goldenpages masks with digits other than `(90) 000`;
patronymic endings (`-овна/-евна/-ович/-евич/qizi/o‘g‘li/…`); any text beside a review
timestamp that is not `Seller NN`; the review authors' names and the individual's STIR
removed in pass 3; `is_person_shaped` over every devalue payload string that is not a
lowercase slug. Not counted as phones: digit runs inside a hex hash, UUID or file name, the
goldenpages input-mask template `+998(99) 999-99-99`, and the kept legal-entity STIR `313…`.
Result: `files checked: 42; hits: 0`. `python scripts/secret_scan.py` over the folder and
`robots/`: `secret_scan: clean`.

**Close-out re-check (2026-10-29, after the review findings were closed; no file body changed
in that step, only this README).** A wider grep over this folder (and `exhibitors`,
`registries`, `tenders`, `maps`), raw, HTML-unescaped and `\uXXXX`-unescaped: `+998` numbers in
every separator form, `(+998NN) NNNNNNN`, URL-encoded `%2B998…`, foreign `+7/+86/+90/…`
shapes, the digits of every `tel:` and `wa.me` link, operator-coded bare 9-digit runs near a
phone word, e-mails (also `[at]`/`&#64;`), t.me and instagram.com handles, `@handles`,
patronymic and `o‘g‘li`/`qizi` shapes, initials-name shapes, avatar / profile-photo URL shapes,
4xx–6xx nine-digit runs near a STIR word, PINFL and passport shapes, and `is_person_shaped`
over every text node and JSON string. Every hit was read: none is person data. What is left is
district and place names (`Зафар`, `Гульбахор`, `Ахунбабаев` …), firm names that contain a
person's name (`AKMAL KONSULT ООО`, `INAGOMOV MILLIY TAOMLARI ООО` — a firm's name stays,
company tier), a street (`ul. SHERKULOVA`), CSS class names (`reviews-rating__avatar`,
`user-avatar`), the generic `/static/img/user-avatar.png`, glotr's own Twitter meta already
pseudonymised to `@namuna_01.uz`, and this README's own prose.

## Recorder ledger

One line per file, written by the tool: file · date · url · sha256 · status · bytes · scrubbed · note

- glotr_oborudovanie_p1.html · 2026-10-29 · https://glotr.uz/ru/oborudovanie/ · 224c459a31691d0681f76a348f57422980edec2dfca141c83c71ca00d5fa5f27 · 200 · 658170 · scrubbed: yes · category list page 1
- yellowpages_teg_tovary_iz_kitaya.html · 2026-10-29 · https://www.yellowpages.uz/teg/tovary-iz-kitaya · 2c69800728af522964b7291c4384942d7b1e9696df08612b6f48b56adecce844 · 200 · 258517 · scrubbed: yes · teg seed (china_origin)
- glotr_showcase_missing_negative.html · 2026-10-29 · https://glotr.uz/showcase/namuna-net-takogo-prodavca-0/ · 777cc7350c7eb5bc7f5f61dc5fd58558942b8ea1d90041467c7b24e76ae03f85 · 200 · 229997 · scrubbed: yes · negative: showcase slug that does not exist
- yellowpages_kompaniya_missing_negative.html · 2026-10-29 · https://www.yellowpages.uz/kompaniya/namuna-net-takoj-kompanii-0 · 13223ff36720c7a3e4c58456537ae61a99b6b235387f6ad315d7bc6e324a636e · 404 · 194504 · scrubbed: yes · negative: kompaniya slug that does not exist
- topuz_stroymaterialy_optom_p1.html · 2026-10-29 · https://top.uz/section/stroymaterialy-optom · 9c8a5cbf453baafe38b64ee8543d65612e06ce8eab0d5d8ea58304c0b4d4fcdf · 200 · 528336 · scrubbed: yes · section list page 1 (second scrub pass: 3 person name(s) → Seller NN)
- topuz_stroymaterialy_optom_p2.html · 2026-10-29 · https://top.uz/section/stroymaterialy-optom?PAGEN_1=2 · 96164db93eb8a924765bc7178edb06f89f54a5f96da520ba21fde01d72749dad · 200 · 537786 · scrubbed: yes · section list page 2 (PAGEN_1) (second scrub pass: 3 person name(s) → Seller NN)
- topuz_company_uzbek_chinese_trading_house.html · 2026-10-29 · https://top.uz/company/international-business-and-travel-sp-ooo-uzbeksko-kitayskiy-torgovyy-dom-predstavitelstvo · 300a412631e06113f31dc5270a41194411f3fca23b4ccfbcbf7b41bd97e90897 · 200 · 239553 · scrubbed: yes · company card from stroymaterialy-optom p1 (second scrub pass: 2 person name(s) → Seller NN)
- topuz_optovaya_torgovlya_p1.html · 2026-10-29 · https://top.uz/section/optovaya-torgovlya · c15d74d42938712e960c38e6b4f3182b0654fa21909ef7ddcd39f053e2e80dae · 200 · 615087 · scrubbed: yes · section list page 1 (count vs organiser figure 815) (second scrub pass: 8 person name(s) → Seller NN)
- topuz_stroymaterialy_optom_p99_negative.html · 2026-10-29 · https://top.uz/section/stroymaterialy-optom?PAGEN_1=99 · 9bf08f14f334a128ef46024bbfb996b2891f6b96f6fec4834e43b9558f485e3e · 200 · 196671 · scrubbed: yes · negative: page past the last (paginator stop) (second scrub pass: 3 person name(s) → Seller NN)
- yellowpages_kompaniya_alta_trade_ooo.html · 2026-10-29 · https://www.yellowpages.uz/kompaniya/alta-trade-ooo · f2bfebd2dc0ace71981114aef59b3e415f7127a5c405fd769eb46f6e55b78d1a · 200 · 279170 · scrubbed: yes · company card from rubrika p1 (third scrub pass: 4 review author name(s) → Seller NN (9 places))
- yellowpages_rubrika_kanctovary_tashkent_p1.html · 2026-10-29 · https://www.yellowpages.uz/rubrika/optovaya-torgovlya-kanctovarami-kancelyarskimi-tovarami/tashkent · 6218b6fee95e52090cadc85e6fc0a5f961c838425c7a470d4bcdb0660b26c08e · 200 · 513701 · scrubbed: yes · rubrika list page 1 (second scrub pass: 1 person name(s) → Seller NN) (third scrub pass: 1 individual entrepreneur STIR → 400000001 (1 place(s)))
- yellowpages_rubrika_kanctovary_tashkent_p2.html · 2026-10-29 · https://www.yellowpages.uz/rubrika/optovaya-torgovlya-kanctovarami-kancelyarskimi-tovarami/tashkent?page=2 · 242600dc0033b40eb6813b31d913107aece5854f3d5277645de0ad9bbba350f6 · 200 · 513610 · scrubbed: yes · rubrika list page 2 (?page=N probe) (second scrub pass: 1 person name(s) → Seller NN) (third scrub pass: 1 individual entrepreneur STIR → 400000001 (1 place(s)))
- goldenpages_company_85641.html · 2026-10-29 · https://www.goldenpages.uz/company/?Id=85641 · 6792a0332115d929edd4e6baf41686b800219e8b4bf7d83a5f02f889ee76b49b · 200 · 119327 · scrubbed: yes · company card (CHINT DISTRIBUTION, from rubric 1424) (third scrub pass: 1 masked phone prefix(es) → +998 (90) 000-XX-XX)
- goldenpages_rubric_106080_p1.html · 2026-10-29 · https://www.goldenpages.uz/rubrics/?Id=106080 · c67ed58df436e49285102b151ff2e9dade12607ce2899852a583b12ffdd640ae · 200 · 185795 · scrubbed: yes · rubric list page 1 (Оборудование из Китая — china_origin cue) (third scrub pass: 8 masked phone prefix(es) → +998 (90) 000-XX-XX)
- goldenpages_rubric_1424_p1.html · 2026-10-29 · https://www.goldenpages.uz/rubrics/?Id=1424 · aeb4a804d8a6a303f21aa1a0129627cf368b2fc4d932245b3e47120d735b05f4 · 200 · 293388 · scrubbed: yes · rubric list page 1 (Оптово-розничная торговля) (third scrub pass: 20 masked phone prefix(es) → +998 (90) 000-XX-XX)
- goldenpages_rubric_1424_p2.html · 2026-10-29 · https://www.goldenpages.uz/rubrics/?Id=1424&Page=2 · c5866aedfa345c71038cabb0c9588dc1d5229b0520bfc15175fe9a4099cad21d · 200 · 277764 · scrubbed: yes · rubric list page 2 (&Page=N, capital P) (third scrub pass: 20 masked phone prefix(es) → +998 (90) 000-XX-XX)
- goldenpages_rubric_1424_p99_negative.html · 2026-10-29 · https://www.goldenpages.uz/rubrics/?Id=1424&Page=99 · 6239ce36a6233c9ed56cf71eff133248959f3686fa27fdd6393c7c5f5a125c8e · 200 · 293770 · scrubbed: yes · negative: page past the last (paginator stop) (third scrub pass: 20 masked phone prefix(es) → +998 (90) 000-XX-XX)
- glotr_showcase_jack.html · 2026-10-29 · https://glotr.uz/showcase/jack/ · e2d2d4cf2942d6cc085d718850bc285660434240c13d1086c31ef51d7cc7fb56 · 200 · 430817 · scrubbed: yes · seller showcase (JACK sewing machines, from oborudovanie p1) (second scrub pass: 5 person name(s) → Seller NN) (third scrub pass: 1 reviewer profile photo URL → /static/img/user-avatar.png)
