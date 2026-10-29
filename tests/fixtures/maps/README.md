# Fixtures: maps (2GIS robots.txt only)

`docs/spec/03-collectors.md` §2.9 `maps` — Google Places (New) and 2GIS. Both providers are keyed,
metered APIs (`http.METERED_HOSTS`: `places.googleapis.com`, `catalog.api.2gis.com`) and no key exists
in the build sandbox, so **no API call was made**. The only request to 2GIS was its robots.txt, to
record what it says about the HTML site, which 03 §2.9 rules out («`2gis.uz` HTML is a SPA
(**UNVERIFIED** server rendering) → do not crawl»).

**How it was recorded (provenance).** The CLI `python -m clientscrapper.tools.record_fixture` was
**not** run. A driver script that is not in the repo (build session scratchpad) called the function
the CLI calls, `clientscrapper.tools.record_fixture.record()`, with `scrub="on"`, inside one
`async with Fetcher(robots_dir=<scratch>/robots)` — stage 1 of the six-stage run tabled in
`tests/fixtures/registries/README.md` (driver run 15:06:32 UTC, together with the orginfo.uz and
uztender.com robots.txt). Both 2gis.uz requests were in that one stage and that one `Fetcher`:
the robots.txt rules check (started 15:06:44 UTC, the time in the cache file's header), then the same file recorded as the page
(response at 15:06:50 UTC), paced by the `Fetcher` at `HOST_RPS["2gis.uz"] = 0.2` (one request per
5 s). User-Agent `clientScrapperBot/0.1.0 python-httpx/0.28.1`, breaker armed. To reproduce with the
CLI: `record_fixture https://2gis.uz/robots.txt --key maps --name twogis_robots --scrub on`.

- **Date:** 2026-10-29 (Asia/Tashkent).
- **Recorded from:** build sandbox (US datacenter IP via egress proxy) — re-check from the VPS with
  `make smoke` before `sources.enabled`.

## Files

| File | Source URL | HTTP | robots.txt for this path | What it proves |
|---|---|---|---|---|
| `twogis_robots.html` | https://2gis.uz/robots.txt | 200 | (the file itself; `/robots.txt` is always allowed) | 2gis.uz's robots.txt as served (`text/plain`, stored under the recorder's `.html` name) |

### Requests made

| Host | Requests | Note |
|---|---|---|
| 2gis.uz | 2 | robots.txt read by the Fetcher's robots check, then the same file recorded as the page |
| catalog.api.2gis.com, places.googleapis.com | 0 | keyed + metered; not called (no key, `costs.ask` would be needed) |

## Verdict for «HTML is a SPA — do not crawl» (03 §2.9, 07 §5.3 «2GIS» row)

- **robots.txt does NOT forbid the HTML site to us — VERIFIED.** There is no `clientScrapperBot`
  group, so the `User-agent: *` group applies. It disallows only `/_/*`, `/maps_action`, any URL with
  `printmode=`, `login_token=`, `reference=`, `stat=`, `*/my$` (and `*/my?*`, `*/my/*`), `*/user*`,
  `*/geolocation*`, `*?refHash*` and one `/geo/70030076128110973`; no `Crawl-delay` for `*` (Mail.Ru
  gets 0.2; `GoogleOther` is disallowed entirely; Yandex alone is refused `/search/%D0%A0/rubricId`;
  `Host:` and `Clean-param:` are Yandex directives). Checked with `services/robots.parse_robots` for
  `clientScrapperBot`: `/tashkent`, `/tashkent/search/<text>` and `/tashkent/firm/<id>` are
  **allowed** — including the spec's building-level ids, which are 2GIS **firm** ids used under
  `/firm/` (03 §2.9 «building-level pulls for firm ids `70000001057601648` (Abu Saxiy Center),
  `70000001038443805`, `70000001043300380`»; `docs/manbalar.json` cites
  `https://2gis.uz/tashkent/firm/70000001057601648`): all three `/tashkent/firm/<id>` URLs are
  allowed. `/tashkent/geo/<id>` is allowed too; the only geo path robots.txt names is the
  root-level `/geo/70030076128110973` (disallowed); `/_/…` and `?stat=…` are disallowed.
  A `Sitemap: https://2gis.uz/sitemap.xml` is declared (not fetched).
- **So the rule stays, on other grounds.** «Do not crawl 2gis.uz HTML» remains the decision, but
  robots.txt is not its reason: (a) server rendering of firm/search pages is **STILL UNVERIFIED** — no
  HTML page was requested, by instruction (one robots.txt only); (b) 2GIS's data is licensed through its
  API/key terms, and 03 §2.9 already routes it through the demo key, then Apify, then a licensed export
  asked of 2GIS Uzbekistan. If a later stage wants the HTML after all, it must first record one firm
  page from the VPS and answer (a), and read 2GIS's terms of use — robots.txt alone would let the
  Fetcher through.
- **STILL UNVERIFIED (no key here):** the demo-key `3.0/items` answer and its fields
  (`items[].contact_groups`…), `region_id` for Tashkent via `2.0/region/search`, the «firms in a
  building» parameter, empty `contact_groups` on UZ records; every Google Places item (IDs-only
  `searchText`, details SKU, the `wholesaler` type name).

## Refused / not recorded

- Nothing was refused. No 2GIS page other than robots.txt and no maps API was requested (instruction:
  no keyed/metered API, no other 2GIS request).

## Recorder ledger

- twogis_robots.html · 2026-10-29 · https://2gis.uz/robots.txt · 9439f2b4b5f219f52f787e1985679333106ff5658a9010c5f1d6e7228e5bd636 · 200 · 1193 · scrubbed: yes · 2gis.uz robots.txt only (03 §2.9: HTML is a SPA, do not crawl) — no other 2GIS request
