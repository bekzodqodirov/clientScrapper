## Zero-cost discovery stack — verification results (12 WebSearch queries, standard mode; budget not reported exhausted)

### Table

| # | Route | Free quota (as found) | What it gives | Reliability | Risk | Use in MVP |
|---|---|---|---|---|---|---|
| 1 | **Brave Search API** | Free plan removed for new users (Feb 2026 per third-party trackers); now $5 free credit/month on the Search plan at $5/1k ≈ **~1,000 queries/mo**; **credit card required**, no spend cap by default (set limit in dashboard); attribution required. Official page not reached — confirm in Brave dashboard. | Full web SERP JSON (title/url/snippet), commercial use allowed on paid plan | High (official API) | Overage billing; card needed conflicts with "no budget" unless hard cap set | **Yes, conditional** (only if owner accepts a card on file with usage limit) |
| 2 | **Yandex Search API (Yandex Cloud)** | **1,000 req/user/month free, night-only 00:00–07:59 UTC+3** (02:00–09:59 Tashkent); paid ≈$4/1k sync requests daytime, ≈$3/1k night; default quota 1,000/day, hourly caps (e.g. 100/h 11:00–22:59); USD pricing for non-Russian contracting entities. Legacy "Yandex XML verified-site" free allowance **not found** in results. | RU/UZ-language web SERP (XML), up to 250 results, 400-char queries | High (official) but night window only | Availability for an Uzbek legal entity not confirmed; terms forbid any other automated querying of Yandex | **Yes** (night cron, RU/UZ queries) |
| 3 | **Bing Web Search API** | **Retired 11 Aug 2025** (announced 12 May 2025). Replacement "Grounding with Bing Search" inside Azure AI Agents: third-party reports $35/1k, **no free tier**, raw results not returned — not verified against Microsoft pricing. | Nothing usable as a SERP source | n/a | n/a | **No** |
| 4 | **DuckDuckGo html/lite endpoints** | No API; scraping `html.duckduckgo.com` / `lite.duckduckgo.com` works at low rate; `anomaly.js` bot challenge and HTTP 403 "too many requests" on frequent calls; forum/code reports of blocks and fallbacks | Web SERP HTML (parseable) | Medium at ≤ a few req/min with browser headers + backoff; degrades fast | Unofficial, ToS grey, IP blocks | **Yes, as fallback** (low QPS) |
| 5 | **SearXNG self-hosted** | Free software on existing server; **Google/Bing/Yahoo/Qwant ship `disabled: true` upstream**; blocks are keyed to server IP and persist; one 2025 hands-on test: Google 0 results, Brave & Startpage suspended, only DDG answered; Sept 2025 issue logs CAPTCHA from DDG and Qwant too | Aggregated SERP JSON over whatever engines still answer from your IP | Low–medium from a single datacenter IP; essentially a DDG proxy with retries | Engine CAPTCHAs, no SLA; needs IP rotation for volume | **Yes, thin layer** (DDG/Startpage/Mojeek engines, JSON format, strict rate limit) — do not rely on Google engine |
| 6 | **Google Programmable Search JSON API** | Official docs: **"not available for new customers"**; existing customers keep 100/day free ($5/1k overage) until **discontinuation 1 Jan 2027**; after that only whitelisted-site engines | Google SERP | n/a for new project | Cannot obtain key | **No** |
| 7 | **Google Places API (New)** | Per-SKU free monthly caps since Mar 2025: **Essentials 10,000 / Pro 5,000 / Enterprise 1,000 events**, not pooled; e.g. Text Search Enterprise + Atmosphere SKU = 1,000 free; **billing must be enabled (card) — charges only above free cap**. Which tier a Text Search call lands in depends on requested fields (not verified this run). | Business name, address, phone, website, category by text/area query — strongest phone source | High (official) | Card required; field choice can push calls into 1,000-cap SKU | **Yes, conditional** (same card condition as Brave; request minimal field mask, set budget alert) |
| 8 | **Instagram logged-out profile** | No quota; 2026 reports: public profile serves picture, **bio, counts and ~6–12 recent posts** before login wall; allowance "retuned regularly"; intermittent failures; private profiles/stories blocked. `web_profile_info` JSON endpoint behaviour **not found** — must be tested. | Bio text, contact hints, link-in-bio | Low; changes without notice; third-party viewers already defunct | Account/IP blocks, Meta ToS | **Enrichment only**, one-off fetch per handle with cache; never bulk-crawl |
| 9 | **2GIS (2gis.uz / Platform API)** | Official **demo key: created once, valid 1 month, 1,000 requests per service**; paid = per-service monthly limit, quote-based. 2gis.uz robots.txt and HTML render mode **not verified**; third-party scraper notes ~5,000-row cap per search term (split by district/rubric/grid) | Rubric (category) listings: firm name, address, phone, rubrics | High for API within quota; HTML crawl unknown | Key suspended on quota; HTML scrape unverified | **Yes, seed only** (1,000-req demo for target rubrics), then evaluate HTML crawl |
| 10 | **OLX.uz** | Free; listings embed **`__PRERENDERED_STATE__`** JSON (search feed ad array + full ad object); one scraper reports **Uzbekistan robots.txt allows `/api/`** and uses the JSON search API directly; an OLX.uz actor returns business-vs-private flag, prices in tiyin, **no seller personal data**. Phone: not shown retrievable from listing JSON; for OLX.ua phone is behind a click; **login requirement for OLX.uz phone reveal not confirmed** | Importer/wholesaler ads, seller/shop identity, category, city | Medium–high (structured JSON) | Phone needs separate reveal step → may require account; ToS | **Yes** (lead discovery); phone via reveal to be tested |

### Recommended zero-cost stack (5 lines)
1. **SERP layer:** SearXNG on the existing server (DDG/Startpage/Mojeek engines, JSON output, ≤1 req/s with backoff) + direct DDG `html` fallback; Yandex Cloud Search API **1,000 free night requests/month** via a 02:00–09:00 Tashkent cron for RU/UZ keyword sets ("оптом", "импорт из Китая", "ulgurji", city + product).
2. **Structured sources (no key):** OLX.uz `__PRERENDERED_STATE__`/`/api/` for business sellers in import-heavy categories; 2GIS demo key (1,000 req, one month) to seed phone+address for target rubrics, then decide on low-rate HTML crawl after reading `2gis.uz/robots.txt`.
3. **Optional card-on-file routes (free within caps, must set hard limits):** Google Places API (New) with minimal field mask for phone/website (1,000–10,000 free events per SKU) and Brave Search $5 credit (~1,000 queries/mo). Skip both if the owner refuses any card.
4. **Do not build on:** Google CSE JSON API (closed to new customers, ends 1 Jan 2027), Bing Search API (retired Aug 2025), paid SERP vendors; Instagram only as per-handle enrichment with cache, never bulk.
5. **Suppression fits here:** every collector normalizes phones to E.164 (+998…) before insert; the ERP keeps a `do_not_contact` set = client-book phones ∪ leads closed with the "Qayta qilinmasin / bizning mijoz emas" lost reason, and the agent checks it on every candidate — note that OLX gives no phone in JSON, so OLX leads can only be suppressed by phone after the reveal step (or by seller/shop id as a secondary key).

### Not verified in this run
Yandex XML legacy verified-site quota; 2gis.uz robots.txt/SSR; Instagram `web_profile_info` JSON; OLX.uz phone-reveal login; exact Places SKU per field mask; Brave official pricing page.

Sources:
- [parallel.ai — best free web search APIs 2026](https://parallel.ai/articles/best-free-web-search-api)
- [agentdeals.dev — Brave Search API free tier caution](https://agentdeals.dev/vendor/brave-search-api)
- [claudemarket.ai — Brave Search API guide](https://claudemarket.ai/blog/brave-search-api-openclaw-guide)
- [apicostcalc.com — Brave (stale free plan)](https://apicostcalc.com/brave-search.html)
- [Yandex Cloud — Search API pricing](https://yandex.cloud/en/docs/search-api/pricing)
- [Yandex Cloud — quotas and limits](https://yandex.cloud/en/docs/search-api/concepts/limits)
- [Yandex Cloud — request limits](https://yandex.cloud/en/docs/search-api/operations/request-limits)
- [Yandex AI Studio — Search API pricing](https://aistudio.yandex.ru/docs/en/search-api/pricing)
- [Yandex — Search API terms](https://yandex.com/legal/cloud_terms_search_api/en/index.md)
- [ppc.land — Microsoft ends Bing Search APIs Aug 11](https://ppc.land/microsoft-ends-bing-search-apis-on-august-11-alternative-costs-40-483-more/)
- [neowin — Microsoft pulls plug on Bing Search APIs](https://www.neowin.net/amp/microsoft-pulls-plug-on-bing-search-apis/)
- [valyu.ai — Bing Search API is dead](https://www.valyu.ai/blogs/bing-search-api-is-dead-now-what)
- [cloro.dev — Bing Search API key 2026](https://cloro.dev/blog/bing-search-api-key/)
- [freecodecamp forum — DuckDuckGo blocking frequent requests](https://forum.freecodecamp.org/t/duckduckgo-is-blocking-frequent-requests/515655)
- [apify — DuckDuckGo SERP (anomaly.js note)](https://apify.com/s-r/duckduckgo-serp?fpr=97nrp4)
- [glama — docdex ddg.rs anomaly handling](https://glama.ai/mcp/servers/@bekirdag/docdex/blob/0ab9bb69fc9b58409ad5ddc5e67fe35bd5d66af8/src/web/ddg.rs)
- [SSD Nodes — SearXNG engines to enable/disable](https://www.ssdnodes.com/learn/searxng-engines-to-enable-and-disable)
- [SSD Nodes — fix SearXNG engine CAPTCHA errors](https://www.ssdnodes.com/learn/fix-searxng-engine-captcha-errors)
- [apiserpent — self-hosted SearXNG as SERP API, tested](https://apiserpent.com/blog/searxng-self-hosted-serp-api-tested)
- [GitHub agenticSeek issue #410 — SearXNG CAPTCHA](https://github.com/Fosowl/agenticSeek/issues/410)
- [Google — Custom Search JSON API overview](https://developers.google.com/custom-search/v1/overview?hl=en)
- [octoparse — Google official search API](https://www.octoparse.com/blog/google-official-search-api)
- [Google Maps Platform — billing & pricing FAQ](https://developers.google.com/maps/billing-and-pricing/faq?hl=en)
- [Google Maps Platform — pricing](https://developers.google.com/maps/billing-and-pricing/pricing?authuser=1)
- [Google Maps Platform — legacy SKU pricing](https://developers.google.com/maps/billing-and-pricing/legacy-sku-pricing)
- [Google Maps Platform — cloud setup (billing must be enabled)](https://developers.google.com/maps/documentation/datasets/cloud-setup?hl=ar)
- [woosmap — Google Maps API pricing 2026](https://www.woosmap.com/blog/google-maps-api-pricing-breakdown)
- [woosmap — is Google Maps API free in 2026](https://www.woosmap.com/blog/is-google-maps-api-free)
- [peekstories — Instagram web vs app 2026](https://peekstories.com/blog/instagram-web-vs-app-what-you-can-see-2026)
- [view-ig-story — Instagram web story viewer](https://www.view-ig-story.com/blog/instagram-web-story-viewer)
- [view-ig-story — user search without account](https://www.view-ig-story.com/blog/instagram-user-search-without-account)
- [adweek — logged-out Instagram allowance](https://www.adweek.com/?p=1092573)
- [roboin.io — Instagram login outage June 2026](https://roboin.io/article/en/2026/06/13/instagram-login-outage-logs-users-out-and-prevents-post-viewing/)
- [socialrails — view Instagram without account](https://socialrails.com/blog/how-to-view-instagram-without-account)
- [2GIS docs — access keys (demo key limits)](https://docs.2gis.com/en/platform-manager/keys)
- [2GIS docs — subscription purchase](https://docs.2gis.com/en/platform-manager/subscription/purchase)
- [2GIS — API platform](https://docs.2gis.com/en/api-platform)
- [dev.2gis.com — API](https://dev.2gis.com/api)
- [apify — 2GIS places scraper (5,000-row cap note)](https://apify.com/dami_studio/2gis-places-scraper)
- [zyte — State of Web Access 2026, Uzbekistan](https://www.zyte.com/sowa/2026/geo/country/as-uz)
- [apify — OLX marketplace scraper, 7 countries (`__PRERENDERED_STATE__`)](https://apify.com/memo23/olx-marketplace-scraper.md)
- [apify — OLX.uz scraper](https://apify.com/lowlanddata/olx-uz-scraper.md)
- [apify — OLX scraper, 6 countries (robots `/api/` note)](https://apify.com/tagadanar/olx-scraper.md)
- [apify — OLX.ua scraper (phone behind click)](https://apify.com/maroon_trio/olx-ua-scraper-parser.md)
- [apify — OLX.pl listings scraper](https://apify.com/axlymxp/olx-pl-listings-scraper.md)