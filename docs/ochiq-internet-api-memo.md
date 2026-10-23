# Open-web importer discovery for GSR Logistics — platform design memo (verified 2026-10-23, 14 WebSearch calls: 13 standard + 1 extended)

## 0. Headline findings that change the plan
- **Google Custom Search JSON API is closing**: official page says it is "not available for new customers" and pricing (100/day free, $5/1k, max 10k/day) applies only to existing customers "until the service discontinuation on January 1, 2027" ([developers.google.com](https://developers.google.com/custom-search/v1/overview?hl=en)). If GSR has no existing CSE key → use a SERP provider (DataForSEO queue $0.60/1k) from day 1.
- **Instagram hashtag budget is 30 unique hashtags / rolling 7 days per Business account**, re-queries free; needs App Review for "Instagram Public Content Access" ([Meta docs](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/hashtag-search)). No 2025–26 change found.
- **Facebook Pages Search is NOT dead**: Meta guide (updated Apr 2026) still documents `pages/search`; app token + "Page Public Metadata Access" suffices for public page info ([Meta docs](https://developers.facebook.com/documentation/pages-api/search-pages)). Marketplace has no API; UZ availability unconfirmed.
- **Meta v. Bright Data**: N.D. Cal. (Judge Chen) granted Bright Data summary judgment — "logged-out scraping of public data … did not breach the terms"; only tortious-interference claim remained ([fbm.com](https://www.fbm.com/data-analytics/publications/major-decision-affects-law-of-scraping-and-online-data-collection-meta-platforms-v-bright-data/), [MediaPost](https://www.mediapost.com/publications/article/392920/judge-sides-against-meta-in-battle-over-scraping-p.html)). Ruling date/appeal status not in results. US precedent only; it does not bind Uzbek courts or stop Meta IP bans.
- **Unknowns after this run**: TGStat API price (not found; task-known S plan 4,200 RUB/mo stands), ZRU-776 art. 31 and PQ-265 (not found on the open web — verify on lex.uz), OLX `__PRERENDERED_STATE__` key (not confirmed), parse.bot Uzum endpoints (not found), 2GIS official API price (not found), Glotr/Flagma/Top.uz (not searched — budget cap).

---

## 1. Platform tables

### 1.1 Instagram
| Route | Fields | Limits / price | Legal risk | MVP? |
|---|---|---|---|---|
| **Official: Graph API Business Discovery** (by username, requires own IG Business acct + FB app + review; permission in results: `instagram_basic` + "Instagram Public Content Access" feature) | biography, website, followers_count, media_count, recent media captions | Per-app Graph rate limits not surfaced in this run (unverified). Free. Review needed ([Meta](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/hashtag-search)) | Low (ToS-compliant) | **Yes** — enrichment step for usernames found via SERP/Telegram |
| **Official: Hashtag Search API** (`ig_hashtag_search` → `top_media`/`recent_media`) | media id, caption, permalink, like/comment count (no author username on recent_media per docs — treat as unverified) | **30 unique hashtags / 7 days / account**; emojis & Stories unsupported; `recently_searched_hashtags` shows quota ([Meta](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-hashtag-search.md)). Supermetrics: quota scales per connected profile (2 profiles → 60/week) ([Supermetrics](https://docs.supermetrics.com/docs/good-to-know-about-instagram-public-data.md)) | Low | Yes, but ration: ~4 new hashtags/day |
| **Unofficial: Apify actors** | full public profile, bio, external URL, posts | `apify/instagram-profile-scraper` "from $1.60/1,000 profiles", "$2.30 per 1,000 on Starter" ([Apify](https://apify.com/apify/instagram-profile-scraper?fpr=4nrc8)); PPR actor $2.00/1k; TrueFetch $2.50/1k; mighty_monk from $5/1k ([Apify](https://apify.com/truefetch/instagram-profile-api)) | Medium: Meta ToS breach for logged-in accounts; Bright Data ruling helps only for logged-out public data in US; account/IP bans | Only as fallback for bulk bio pulls (company handles only) |
| **Discovery via SERP** (`site:instagram.com "optom" "Toshkent"`) | handle, bio snippet | SERP provider price (see §1.3) | Low | **Yes — primary discovery** |

### 1.2 Facebook
| Route | Fields | Limits / price | Legal risk | MVP? |
|---|---|---|---|---|
| **Official: Pages Search (`/pages/search?q=`)** | id, name, location, link, verification_status (public page metadata) | Needs app token + "Page Public Metadata Access"; "Page Public Content Access" (review) for content/competitive analysis ([Meta](https://developers.facebook.com/documentation/pages-api/search-pages); [v6.0 changelog](https://developers.secure.facebook.com/docs/graph-api/changelog/version6.0)) | Low | Yes (cheap, keyword → page list) |
| **Official: Page public content (posts)** via PPCA | posts, about, phone/website if public | App Review; PPCA approval harder since 2018 restriction ([Meta 2018](https://about.fb.com/news/2018/07/a-platform-update/)) | Low | Phase 2 |
| **Facebook Marketplace** | — | No API found; UZ availability not confirmed by results | Scraping = ToS breach, logged-in only | **No** |
| **Groups** (e.g. "Оптом Ташкент") | — | No API for non-admin groups (not re-verified) | High if scraped logged-in | No (manual only) |
| **Meta Lead Ads** (inbound) | lead form fields → webhook → LogisticsERP | Ad spend only; not searched this run | None | Yes, as compliant inbound |

### 1.3 Google (web + Maps)
| Route | Fields | Limits / price | Legal risk | MVP? |
|---|---|---|---|---|
| **Custom Search JSON API** | title, link, snippet | 100/day free, $5/1k, ≤10k/day — **existing customers only; discontinued 2027-01-01** ([Google](https://developers.google.com/custom-search/v1/overview?hl=en)) | None | Only if key already exists |
| **SERP providers** | organic results, snippets, maps pack | DataForSEO Standard Queue $0.60/1k, Live $2.00/1k; SerpApi Dev $75/mo = 5k ($15/1k); Bright Data PAYG $3/1k, Growth $2.30, Business $2.03 ([apiserpent](https://apiserpent.com/blog/serpapi-vs-dataforseo-benchmark), [keyword.com](https://keyword.com/blog/bright-data-serp-api-vs-datafor-seo/)) | Low (provider bears scraping) | **Yes — DataForSEO queue** |
| **Places API (New) Text Search** | name, formatted_address, phone, website, types, rating, place_id | IDs Only: free unlimited; Pro $32/1k (5k free/mo); Enterprise $35/1k (1k free); Enterprise+Atmosphere $40/1k; Place Details Essentials $5/1k (10k free) ([Google pricing](https://developers.google.com/maps/billing-and-pricing/pricing)) | Low if ToS: place_id cacheable "in accordance with Places API Policies"; lat/lng cache ≤30 days then delete ([Maps terms 2025-05-01](https://cloud.google.com/archive/maps-platform/terms/maps-service-terms-20250501)). "Other content" retention rule not confirmed in this run — assume no persistent storage of name/phone beyond place_id | Yes, ≤5k Pro calls/mo free |
| **Unofficial Maps scrapers** | all card fields + reviews | Apify ~$1–5/1k (not verified for Google Maps this run) | Medium (Google ToS) | No |

### 1.4 Yandex Maps / 2GIS
| Route | Fields | Limits / price | Legal risk | MVP? |
|---|---|---|---|---|
| **Yandex Geosearch (Places) API** | org name, address, phone, URL, categories | Annual minimum: $2,000/yr for 1,000 queries/day … $22,000/yr for 200,000/day; overage $1–3 per 1k; >200k → quote; **Uzbekistan not mentioned** ([Yandex](https://yandex.com/dev/tariffs/doc/en/geosearch/prices/)) | Low | **No** for MVP ($2k/yr floor) |
| **Yandex Maps unofficial** | same | Apify yandex-maps scrapers exist ([Apify](https://apify.com/dami_studio/yandex-maps-scraper)), price not captured | Medium | Fallback |
| **2GIS Places API (official)** | org, phone, rubrics | **No official price found** — request via 2GIS developer sales | Low | Ask quote (2GIS strong in Tashkent) |
| **2GIS unofficial (Apify)** | places + business data | $0.40–$4.99 per 1k places; covers Uzbekistan ([Apify crawlerbros](https://apify.com/crawlerbros/2gis-places-scraper), [dami_studio](https://apify.com/dami_studio/2gis-places-scraper)) | Medium (2GIS ToS) | Pilot 1k rows for "оптовая база/склад" rubrics |

### 1.5 OLX.uz
| Route | Fields | Limits / price | Legal risk | MVP? |
|---|---|---|---|---|
| **Official API** | — | None public (confirmed by absence; no partner API in results) | — | — |
| **Embedded JSON / SSR state** | listing JSON, seller type | `__PRERENDERED_STATE__` **not confirmed by results**; verify in page source | Medium (OLX ToS not retrieved this run; phone reveal is behind a logged-in/limited endpoint — not verified) | Internal pilot only |
| **Apify `lowlanddata/olx-uz-scraper`** | listings, price (tiyin), **business-vs-private flag**, "zero seller personal data", no coordinates ([Apify](https://apify.com/lowlanddata/olx-uz-scraper.md)) | $1.99/1k listings PAYG | Low–medium (no PD) | **Yes** — business-flag + volume words |
| **Other Apify** | listings | abotapi OLX Global $1.00/1k (incl. UZ) ([Apify](https://apify.com/abotapi/olx-scraper)); scrapyx $1.05/1k (incl. UZ) ([Apify](https://apify.com/scrapyx/olx-listings-scraper)); logiover UA $3.50/1k with "phone-available" filter ([Apify](https://apify.com/logiover/olx-scraper.md)) | Medium if phones pulled | Secondary |

### 1.6 Telegram
| Route | Fields | Limits / price | Legal risk | MVP? |
|---|---|---|---|---|
| **t.me/s/<channel> public preview** | last ~20 posts HTML, no login | Free; HTML only, no search | Low | Yes, for channel snapshots |
| **Telethon/MTProto (own acct)** | messages, members where public | Known (not re-verified): FloodWait, join limits | Medium (TOS + PD of individuals) | Yes, read-only, company-named posts only |
| **TGStat API** | channel search, posts search, stats | Search API / Stat API split; paid tiers unlock starred methods ([rtgstat README](https://cran.rstudio.com/web/packages/rtgstat/readme/README.html)); **price not in results** (known: S 4,200 RUB/mo) | Low | **Yes** |
| **Telegram Ads via reseller (inbound)** | — | ClickMe = "official Telegram partner in Uzbekistan", $1,000 min deposit ([Spot.uz](https://www.spot.uz/ru/2025/01/14/telegram-ad/)); eLama "от 0 €" for UZ ([eLama](https://elama.ru/blog/zapuskayte-reklamu-telegram-ads-v-uzbekistane-s-elama/)); Magnetto from €500; direct account €2M (Spot) vs €2,000 (blog) — conflicting ([telegramadsanalytics](https://telegramadsanalytics.com/blog/telegram-ads-agency-guide-2026)) | None | Phase 2 inbound |

### 1.7 Marketplaces & catalogues
| Route | Fields | Limits / price | Legal risk | MVP? |
|---|---|---|---|---|
| **Uzum — Apify `getascraper/uzum-scraper`** | price, discount, rating, **order count**, review count, category, image; keyword search; seller-level pull not confirmed ([Apify](https://apify.com/getascraper/uzum-scraper)) | $3.30/1k | Medium (Uzum ToS) | Yes — order-count → volume proxy; seller name needs verification |
| **Uzum internal catalog endpoints / parse.bot** | — | **Not found in results** | Medium | Only after manual check |
| **Glotr / Flagma / Top.uz** | company name, phone, category, "оптом" listings | **Not searched (budget)** — check robots.txt/sitemap first | Low–medium (B2B directories, company-level data) | Yes if robots allow; else manual |

---

## 2. Keyword matrix (search = category term × B2B marker × geo)

**B2B markers (AND one of):**
| UZ-Latin | UZ-Cyrillic | RU |
|---|---|---|
| optom, ulgurji, ulgurji narx, optom baza | оптом, улгуржи, улгуржи нарх | оптом, опт, мелкий опт, крупный опт, оптовая база, оптовые цены |
| Xitoydan, Xitoydan to'g'ridan-to'g'ri, zavoddan, ishlab chiqaruvchidan | Хитойдан, заводдан | из Китая, напрямую из Китая, прямые поставки, от производителя, с фабрики |
| konteyner, fura, partiya, katta hajmda | контейнер, фура, партия | контейнер, фура, партиями, крупными партиями |
| diler, distribyutor, yetkazib beruvchi, import | дилер, дистрибьютор, импорт | дилер, дистрибьютор, поставщик, импортёр, официальный импортер |
| do'konlar uchun, sklad, minimal buyurtma | дўконлар учун, склад | для магазинов, склад, мин. заказ, MOQ, 1688, Taobao, Alibaba, Иу/Yiwu, Гуанчжоу |

**Goods categories (China-import heavy):**
| UZ-Latin | UZ-Cyrillic | RU |
|---|---|---|
| mato, gazlama | мато, газлама | ткани, текстиль |
| poyabzal | пойабзал | обувь |
| kiyim, trikotaj | кийим | одежда, трикотаж |
| uy-ro'zg'or buyumlari, idish-tovoq | уй-рўзғор, идиш-товоқ | хозтовары, посуда |
| avto ehtiyot qismlar, shina | авто эҳтиёт қисмлар | автозапчасти, шины |
| qurilish mollari, santexnika, kafel | қурилиш моллари, сантехника | стройматериалы, сантехника, плитка |
| mebel furnitura, mebel | мебель фурнитура | мебельная фурнитура, мебель |
| o'yinchoqlar | ўйинчоқлар | игрушки |
| kosmetika, parfyumeriya | косметика | косметика, бытовая химия |
| telefon aksessuarlari, elektronika, maishiy texnika | телефон аксессуарлари | аксессуары для телефонов, электроника, бытовая техника |
| yoritgich, LED, elektr mollari | ёритгич | светильники, LED, электротовары |
| uskuna, dastgoh, stanok | ускуна, дастгоҳ | оборудование, станки, линия |
| xomashyo, granula, plastik | хомашё | сырьё, гранулы, полимеры |
| qadoqlash, paket, karobka | қадоқлаш | упаковка, пакеты, гофрокороб |
| kanselyariya | канцелярия | канцтовары |
| sumka, chamadon | сумка | сумки, чемоданы |

**Geo anchors:** Toshkent/Тошкент/Ташкент, Abu Sahiy, Bek Baraka, Chorsu, Urikzor, Yangiobod, Malika, Qo'yliq, Sergeli, Andijon, Samarqand, Farg'ona, Namangan, Buxoro, Nukus.

**Query templates:** `site:instagram.com ("optom" OR "оптом") "<category>" ("Toshkent" OR "Ташкент")`; `site:t.me ("оптом" OR "optom") "<category>"`; `site:olx.uz "оптом" "<category>"`; Places text: `"оптовая база <category> Ташкент"`, `"<category> optom Toshkent"`; FB pages/search `q=<category> optom`.

---

## 3. Weekly crawl schedule (automated, UTC+5)
| Day | Job | Volume / cost control |
|---|---|---|
| Mon | SERP batch (DataForSEO queue): 17 categories × 3 languages × 3 markers × 3 geos ≈ 460 queries → extract instagram/t.me/olx/company-site URLs | ~$0.30 at $0.60/1k |
| Tue | Places Text Search (Pro tier) for "оптовая база/склад/optom" × category × city; store **place_id only** + score; re-fetch details on demand | ≤1,250 calls/wk to stay inside 5k/mo free |
| Wed | Instagram Business Discovery enrichment of new handles (official); Hashtag API: 4 new hashtags (28/week < 30 cap) | free; log `recently_searched_hashtags` |
| Thu | OLX.uz (lowlanddata actor) for business-flag listings in wholesale categories; dedupe by seller name | ~2k rows ≈ $4 |
| Fri | Uzum keyword runs (order_count ≥ threshold) + Glotr/Flagma/Top.uz (robots-permitting) | ~1k rows ≈ $3.30 |
| Sat | Telegram: t.me/s snapshots of ~50 wholesale/cargo-adjacent channels; TGStat posts search for "оптом из Китая", "konteyner"; Telethon read-only on public groups | TGStat plan + 0 |
| Sun | Score (§4), dedupe (phone/handle/site), suppress competitors (cargo/kargo/логистика/карго in name), push to LogisticsERP lead webhook; weekly report to Telegram | — |
| Monthly | Facebook `pages/search` sweep (metadata only); re-check quota/pricing pages; purge lat/lng >30 days | — |

---

## 4. Deterministic "B2B ≥3 m³" signal checklist (score ≥ 4 = lead; any HARD-NEG = drop)
**Universal (+ points):** +2 "оптом/optom/ulgurji" in name/bio/title; +2 "из Китая/Xitoydan/1688/Taobao/Yiwu/Guangzhou"; +2 "контейнер/konteyner/фура/fura/партия/partiya"; +1 "от производителя/zavoddan/прямые поставки"; +1 "дилер/дистрибьютор/поставщик/yetkazib beruvchi"; +1 "склад/sklad/база" or market name (Abu Sahiy, Bek Baraka, Chorsu, Urikzor, Yangiobod, Malika, Sergeli); +1 "для магазинов/do'konlar uchun/мин. заказ/MOQ/от N штук"; +1 legal-entity marker (MChJ/ООО/OOO/XK/ИП/ИНН/СТИР). **HARD-NEG:** name/bio contains "карго/kargo/cargo/логистика/logistika/доставка из Китая/yuk tashish" (competitor); B2C-only ("в розницу", "1 dona", "доставка по городу", personal profile no company name).
- **Instagram:** bio has B2B marker + website or Telegram link + `media_count ≥ 50`; captions with pack units ("коробка/karobka", "упаковка/qadoq", "мешок/qop", "рулон/rulon"); business category label (Wholesale/Shopping & retail); followers alone ≠ signal.
- **Facebook Pages:** category "Wholesale & Supply Store"/"Wholesaler"/"Manufacturer" or name contains marker; location Uzbekistan; verification_status any.
- **Google/2GIS/Yandex Places:** types/rubric contains wholesale/warehouse/"оптовая база"/"склад"; name marker; phone + website present; address inside a known wholesale market polygon.
- **OLX.uz:** business flag = true AND title/description contains "оптом/optom/партия/контейнер/со склада/от N шт"; price per unit low + quantity field; same seller ≥10 active listings in one category; "по предзаказу из Китая" = strong.
- **Telegram:** channel/group description has marker; posts with "приход/новое поступление/новый контейнер/yangi konteyner keldi/фура пришла"; price lists as files; "опт от N штук/коробки"; admin contact is a company handle. Members of cargo-competitor chats posting "ищу поставщика/нужен контейнер" = lead.
- **Uzum:** seller with ≥N SKUs in one China-import category and order_count totals implying monthly volume (set N empirically; order_count is the only volume field returned); seller legal name present.
- **Glotr/Flagma/Top.uz:** listing type "оптом/опт", "предложение поставщика", quantity ≥ pallet/ton/container, company card with INN.
- **Company websites (from SERP):** pages "о компании" with "импортёр/официальный дистрибьютор", "прямые поставки из Китая", catalogue ≥50 SKUs, B2B price list download.

---

## 5. Legal posture (Uzbekistan) — one paragraph
ZRU-547 "On Personal Data" (2 Jul 2019, in force 1 Oct 2019) has no closed list of personal data: anything identifying a natural person counts ([lex.uz](https://www.lex.uz/acts/4396428); [Servercore](https://servercore.com/ru/blog/articles/uz-zakon-o-personalnyh-dannyh/)); amendments by ZRU-1125 of 26 Mar 2026 softened localization — only biometric/genetic data and telecom-subscriber data must be stored in Uzbekistan, other data may be stored abroad under conditions (reported as art. 27¹) ([Servercore](https://servercore.com/ru/blog/articles/uz-zakon-o-personalnyh-dannyh/); [ryskiyeva.com](https://ru.ryskiyeva.com/publications/post/personalnye-dannye-v-uzbekistane-rukovodstvo); [Legal500](https://www.legal500.com/intelligence/uzbekistan/privacy/)). Company-level data (legal name, INN/STIR, business phone, address, website) is not personal data; individual Instagram/Telegram handles and personal mobile numbers are, so store them only with a lawful basis (e.g., published by the person for commercial contact) and delete on request. **ZRU-776 art. 31 and PQ-265 (effective 2026-11-01) were not found by any query in this run** — treat those citations as unverified and confirm on lex.uz before relying on them. Practical rules: prefer official APIs; logged-out public collection only; no circumvention of logins/CAPTCHAs; keep Places data as place_id (lat/lng ≤30 days); log provenance per field; honour robots.txt; suppress competitors and B2C.

---

## Sources
- [Meta — Hashtag Search](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/hashtag-search) · [IG Hashtag Search ref](https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-hashtag-search.md) · [recently_searched_hashtags](https://developers.secure.facebook.com/docs/instagram-api/reference/ig-user/recently_searched_hashtags/) · [Supermetrics note](https://docs.supermetrics.com/docs/good-to-know-about-instagram-public-data.md)
- [Apify instagram-profile-scraper](https://apify.com/apify/instagram-profile-scraper?fpr=4nrc8) · [TrueFetch](https://apify.com/truefetch/instagram-profile-api) · [mighty_monk](https://apify.com/mighty_monk/instagram-profile-scraper)
- [FBM on Meta v. Bright Data](https://www.fbm.com/data-analytics/publications/major-decision-affects-law-of-scraping-and-online-data-collection-meta-platforms-v-bright-data/) · [MediaPost](https://www.mediapost.com/publications/article/392920/judge-sides-against-meta-in-battle-over-scraping-p.html) · [Yahoo/TechCrunch](https://au.news.yahoo.com/court-rules-favor-scraper-bright-155721877.html)
- [Meta — Pages Search](https://developers.facebook.com/documentation/pages-api/search-pages) · [Graph v6.0 changelog](https://developers.secure.facebook.com/docs/graph-api/changelog/version6.0) · [2018 platform update](https://about.fb.com/news/2018/07/a-platform-update/)
- [Google Custom Search JSON API](https://developers.google.com/custom-search/v1/overview?hl=en) · [Maps Platform pricing](https://developers.google.com/maps/billing-and-pricing/pricing) · [Maps service terms 2025-05-01](https://cloud.google.com/archive/maps-platform/terms/maps-service-terms-20250501)
- [Yandex Geosearch prices](https://yandex.com/dev/tariffs/doc/en/geosearch/prices/) · [Apify 2GIS crawlerbros](https://apify.com/crawlerbros/2gis-places-scraper) · [Apify 2GIS dami_studio](https://apify.com/dami_studio/2gis-places-scraper) · [Apify Yandex Maps](https://apify.com/dami_studio/yandex-maps-scraper)
- [Apify OLX.uz lowlanddata](https://apify.com/lowlanddata/olx-uz-scraper.md) · [abotapi OLX](https://apify.com/abotapi/olx-scraper) · [scrapyx OLX](https://apify.com/scrapyx/olx-listings-scraper) · [logiover OLX](https://apify.com/logiover/olx-scraper.md)
- [rtgstat README (TGStat API)](https://cran.rstudio.com/web/packages/rtgstat/readme/README.html) · [vc.ru TGStat overview](https://vc.ru/id2417772/1959009-tgstat-analitika-telegram)
- [Spot.uz Telegram Ads](https://www.spot.uz/ru/2025/01/14/telegram-ad/) · [eLama UZ](https://elama.ru/blog/zapuskayte-reklamu-telegram-ads-v-uzbekistane-s-elama/) · [telegramadsanalytics guide](https://telegramadsanalytics.com/blog/telegram-ads-agency-guide-2026)
- [Apify Uzum scraper](https://apify.com/getascraper/uzum-scraper)
- [apiserpent SerpApi vs DataForSEO](https://apiserpent.com/blog/serpapi-vs-dataforseo-benchmark) · [keyword.com Bright Data vs DataForSEO](https://keyword.com/blog/bright-data-serp-api-vs-datafor-seo/) · [apiserpent pricing comparison](https://apiserpent.com/blog/serp-api-pricing-comparison)
- [lex.uz ZRU-547](https://www.lex.uz/acts/4396428) · [Servercore ZRU-547](https://servercore.com/ru/blog/articles/uz-zakon-o-personalnyh-dannyh/) · [ryskiyeva.com guide](https://ru.ryskiyeva.com/publications/post/personalnye-dannye-v-uzbekistane-rukovodstvo) · [Legal500 Uzbekistan privacy](https://www.legal500.com/intelligence/uzbekistan/privacy/)

Notes: budget cap (14 queries) reached; not exhausted by the tool. Not verified this run: Graph API numeric rate limits, instagram_business_basic vs instagram_basic naming, OLX `__PRERENDERED_STATE__`, OLX ToS text, parse.bot, Glotr/Flagma/Top.uz robots, 2GIS official pricing, Facebook Marketplace availability in UZ, Meta Lead Ads pricing, ZRU-776/PQ-265.