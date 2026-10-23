# Completeness critique — clientScrapper plan (GSR Logistics)

## 1. Missing source families / signals

**A. GSR's own data (cheapest, zero legal risk, absent as a first-class pillar)**
- Lookalike profile of the 400+ clients (TN VED groups, district, legal form, OKED, Uzum/OLX presence, shipment cadence) → defines scoring weights; today the plan only mentions "calibrate" in passing.
- `receipts.unclaimed_marking` boxes at Yiwu/Guangzhou/Kashgar: importers whose goods already reach GSR via third parties — a warm list sitting in the WMS.
- ERP's own lost/stale leads (`lost_reason`, `next_action` overdue) and dormant clients (no receipt >90 days): reactivation beats any scraper on CAC.
- Managers' TelegramCustoms bot usage: every quote request is a prospect event — not logged as a lead today.
- Site inbound is broken: `lead-client.ts` mints `GSR-…` codes, ERP accepts only `LEAD-[A-Z0-9]{5,16}` (`site-assign-rules.ts:41`, migration 0110 CHECK) → every site visitor falls back to the owner's handle and no lead auto-opens (confirmed in checkouts). Fixing one constant is the single highest-ROI "source" in the whole dossier.

**B. Paid search intent (entirely absent; only Telegram Ads covered)**
- Google Ads + Yandex Direct on "карго из Китая Ташкент / Xitoydan yuk / konteyner Xitoy / 1688 optom"; agency guides put Tashkent CPC at $0.10–0.80 and test budgets at $200–300/mo ([uzneo](https://uzneo.uz/en/blog/yandex-direct-ili-google-ads-uzbekistan), [tipa.uz](https://tipa.uz/ru/news/seo-digital-uzbekistan-2026)); no cargo CPL published anywhere → needs a $500 test, not a dossier entry. Also missing: Search Console/Wordstat query mining for the existing Astro guides, Google Business/Yandex Business profiles, a volume-pre-qualifying calculator as lead magnet, Meta lead-form ads (ERP already has `meta/instagram/facebook` doors — the plan never feeds them).
- Website-visitor identification: reverse-IP B2B de-anonymisation is useless in UZ (mobile/NAT); the realistic version is a Telegram-login/bot deep-link on the calculator + retargeting pixels. Not discussed.

**C. Freight-ecosystem partners (the brief named them; the dossier skipped them)**
- Customs brokers: state register exists by law (Res. 239/2016) but only a 2016 list (440 firms) is public ([norma.uz](https://www.norma.uz/nashi_obzori/gtk_opublikoval_spisok_deystvuyushchih_tamojennyh_brokerov)); Association of Customs Brokers keeps a member register + 2025 rating ([atb.uz](https://atb.uz/rejting-atb-2/aktualnyj-rejting-list/)); singlewindow.uz lists registers of customs warehouses, AEOs and brokers ([singlewindow.uz](https://singlewindow.uz/)); logistika.uz has a broker catalogue ([logistika.uz](https://logistika.uz/broker/)). Brokers and bonded/temporary-storage warehouses see every declarant — referral partners, not scrape targets.
- Freight exchanges: ATI.SU has a live China→Uzbekistan board ([trucks.ati.su](https://trucks.ati.su/kitay/uzbekistan)); della.uz is a cargo board (no China lanes seen in snippets, [della.uz](https://www.della.uz/)). Shipper postings = full-truck intent with phone, legal to read.
- Truck parks / carriers (AIRCUZ/TIR holders), rail container terminals (Chukursay, Sergeli), Tashkent airport cargo agents — human networks; no mention.
- Kazakhstan transit: KZ customs data is confirmed useless (transit excluded), but Khorgos ICBC visa-free zone, Altynkol/Dostyk consolidators and Almaty wholesale markets are where Uzbek shuttle-traders buy — a lookalike pool for "switch to direct China". Kyrgyz route (Irkeshtam→Osh→Andijan, Qorasuv/Dordoi) for Fergana-valley clients, relevant to GSR's Kashgar point, is absent.

**D. Finance / insurance / leasing (absent)**
- Banks register every import contract (EEISVO) and sell CNY/USD to importers; trade-finance desks and Uzum Business/Payme Business/Click SME units are partnership channels. Leasing companies finance Chinese equipment (Uzbek Leasing International, MK Leasing — Fitch-rated Aug 2026, [mikro-leasing.uz](https://mikro-leasing.uz/), [uzbekleasing.uz](https://uzbekleasing.uz/ru/)); cargo insurers (Uzbekinvest, Gross, Apex); accountants/VED-consultant Telegram communities (buxgalter.uz readership) who file GTD VAT.

**E. Chinese-side (thin in dossier)**
- WeChat 1688/义乌 Uzbek-trader groups and Yiwu stall owners (people network, manual).
- Chinese brand dealer pages: every brand publishes its Uzbek distributor (Gree → My Gree Group, Haier Uzbekistan, multiple "official" Midea importers with conflicting contacts — [gree_uz](https://www.instagram.com/gree_uz/), [haiershop.uz](https://haiershop.uz/ru), [mideaofficial.uz](https://mideaofficial.uz/)); a 200-brand list (appliances, HVAC, trucks, construction machinery, EVs, phones, tools) yields container-scale importers with phones, legally.
- Chinese Chamber of Commerce in Uzbekistan (founded 2019, MOFCOM-registered, contact published; no public member list — [huanenet](https://www.huanenet.com/wsbkst/3492.html), [MOFCOM report](https://fdi.mofcom.gov.cn/resource/pdf/2025/01/23/c6fcd1bd508b416a9be7c4f9156eb3f7.pdf)); Chinese-founded UZ entities (1,823–5,000 depending on source) import equipment/materials continuously — filter EGRPO/myorg by foreign founder.

**F. Other public identity sources not in the list**
- Trademark register (ima.uz): private-label importers register marks for Chinese-made goods; public searchability contested ([ip-coster](https://ip-coster.com/IPGuides/trademark-uzbekistan)) — check once.
- Tax Committee "large taxpayers" category (>100 bn UZS turnover; criteria on [gov.uz/soliq](https://gov.uz/oz/soliq/activity_page/yirik-soliq-to-lovchilar), only top-20 lists public) — container-tier filter.
- Digital-marking (ASL BELGISI) participant lists for marked categories (appliances/beverages/pharma) — unverified, worth one check.
- Economic court decisions naming importers vs customs (sud.uz) — unverified.
- Bazaar administrations (Abu Saxiy, Malika, Sergeli auto, Chilonzor furniture, Qo'yliq; regional Jahon bozori) — stall-tenant lists via management, plus physical canvassing with the bot.

**G. Segments GSR sells to but the plan ignores**: car/EV import (avtosalon dealers on OLX/avtoelon importing BYD/Chery), equipment import (SEZ residents, leasing-financed factories), air-cargo electronics importers, regional importers (Fergana valley, Samarkand) — all sources are Tashkent-wholesale-centric.

## 2. Weakly evidenced claims to re-check before money is spent

| Claim | Why weak |
|---|---|
| Customs Committee paid statistics (Order 3659 / lex 7682204, 25–250 BHM) is usable | Nobody knows if it is per-declarant. One written request decides whether the whole broker-data pillar ($1–3k/yr each) is needed or redundant. |
| Any customs-data vendor (ImportGenius, TradeInt, Tendata, Volza, vedrating, 52wmb) | Same grey upstream; counts diverge 55k→1M "importers"; freshness verified only to Aug–Dec 2025; UZ add-on may be sales-gated; TradeInt Lite is search-only; vedrating has zero footprint; ExportGenius total exceeds official imports; free samples show only corporates, not 3–10 m³ LCL traders. Buy nothing before a sample is scored against 20 known GSR clients (precision/recall). |
| TelegramCustoms Excel has no importer column | Inferred from the parser's HEADER_ALIASES, never from the raw file. Open the file — 10 minutes. |
| hh.uz API "free with User-Agent" | Contradicted inside the dossier (403 since April 2026, employer-app moderation). Smoke-test before building. |
| Uzum shop pages show legal name/registration number (oferta 5.3) | Clause is Russian boilerplate ("ОГРН"); no evidence uzum.uz renders it; Apify actors emit no seller field; ~90% of sellers are micro. The "primary machine-readable seller list" rests on an unverified render. |
| Conformity-certificate register = "strongest importer signal" | sert2.standart.uz not indexed; current-year-only limit and search fields come from a 2015 article; rules replaced 28.12.2024; certificates can cover a whole contract (kills "one cert = one consignment"); mandatory list cut by 747 positions. |
| Registry-first enumeration (EGRPO/registr.stat.uz, data.egov.uz 2-002-0010, VAT register) | No source shows OKED/region enumeration or bulk export; captcha/ONEID+EDS per lookup; dataset schema unverified. The "tens of thousands of 46.xx entities" never appeared in any result. |
| Telegram Ads channel-level targeting for UZ, CPM €0.05, Telegrad price | Three contradictory price statements; specific-channel targeting unverified; reseller cabinets can't send to websites; UZS-only price rule. |
| Yield figures (5–15 qualified/week Telegram, 20–100 companies/month hiring, 50–200/month OLX, 100–200/yr exhibitions) | All planning guesses; verifiers downgraded most. Build nothing on them until a 2-week pilot measures. |
| Legal: no legitimate-interest/public-source ground in ZRU-547 vs memo's DLA Piper reading; ПП-265 covers calls or only SMS; whether Telegram DMs are "telecom-network advertising" (DLA Piper: likely yes); whether Haiku extraction on company text triggers КоАО 46-2 part 2 (AI processing) | Primary lex.uz text not read; each answer changes what the agent may store and who may contact whom after 1 Nov 2026. |
| Apify actors for 2GIS/OLX/Instagram/hh.uz as production collectors | 1-MAU actors, deprecated siblings, platform ToS explicitly ban automated collection (2GIS, OLX, Meta, hh). Prototype-only. |
| Dossier states `other` is not an allowed inbound key | Wrong: `INBOUND_SOURCE_KEYS` includes `'other'` (inbound.ts:47) and unknown keys fall back to it (l.143–145). No ERP change is needed to push. |

## 3. Design decisions to reverse or simplify

1. **Reverse "registry-first, STIR as primary key, enumerate 50k entities"** → signal-first: own data + inbound + 4 high-signal collectors; resolve STIR only for candidates that score, by hand for the top-N (captcha/ONEID make automation both fragile and ToS-breaching).
2. **Cut 40+ sources to 5 for the pilot**: (a) own-data lookalikes/unclaimed markings/lost leads; (b) one validated customs sample or the official paid feed; (c) Uzum bulky-category shops; (d) hiring monitor (hh.uz + "Chinese translator" vacancies); (e) read-only Telegram monitor on 30–60 groups. Everything else is backlog gated on measured precision.
3. **Delete the sender**: after 1 Nov 2026 and under Telegram's 2026 terms, agent-initiated cold DMs/SMS/calls are the highest legal and account-ban risk and the lowest yield. The agent never writes to anyone; managers contact from their own GramJS-connected accounts inside the ERP. This drops MIYA's outbox/caps/gate/policy/sender reuse (~40% of the proposed codebase) and most of the legal exposure.
4. **Drop outright**: KZ customs data, Apollo/Lusha-class brokers, LinkedIn automation, Facebook, Panjiva/Datamyne, BiCoTender, GACC portal, CCPIT roster scraping, Instagram hashtag scraping, 2GIS/Yandex/Google Maps scraping (use a 2GIS data licence or manual), WeChat automation, ZoodMall/Sello, tender.uz/etender/dxarid, Telega.in.
5. **Simplify the m³ estimator**: replace `review_delta × orders_per_review × unit_volume` with three buckets (bulky category × activity × legal form × VAT status) calibrated on GSR's clients; validate with 50 manager calls, then iterate.
6. **Push through the existing `other` door** (or one new `agent` key) with source/score/evidence in `capturePairs` fields; respect PHONE_CAP 3/day, SOURCE_CAP 200/day, 30-day join; keep username-only prospects in the agent DB until a phone exists; load the client export as a suppression list before pushing.
7. **"Autonomous continuous"** → weekly batch + owner review desk: at 10 trucks/month and a few managers, more than 20–50 vetted leads/week cannot be worked; autonomy adds cost and risk without throughput.
8. **"Learns from outcomes"** → a weekly join of ERP stage/`lost_reason` by source → precision table per source. No ML.
9. **LLM use**: Haiku classification on company-level text only; strip person fields before any model call (46-2 part 2); no embeddings/pgvector in v1.
10. **Two-tier schema enforced structurally**: company tier has no personal-phone/handle columns; person tier is written only after a consent event (bot deep-link tap / manager-confirmed). Register the DB at pd.gov.uz regardless (free).
11. **Reconsider the separate Python repo**: with the sender gone, remaining MIYA reuse is plumbing. A module inside LogisticsERP (same DB, same Telegram stack, same lead tables, one deployment fewer) may be cheaper for 1–2 devs; decide explicitly rather than by default.
12. **Fix the GSR-/LEAD- code mismatch and set `lead_assign_origins` first** — before any collector is written.

## 4. Ten owner questions, ordered by plan impact

1. Does the raw customs Excel behind TelegramCustoms carry importer name/STIR, who supplies it, and can it be bought monthly? (Yes → most discovery sources are redundant.)
2. Which segment first: LCL 3–10 m³ bazaar/marketplace traders, full-truck/container corporate importers, or equipment/car importers — and Tashkent-only or regions? (Different sources, scripts, pricing, legal profile; YaTT in or out.)
3. Outreach policy: may the agent ever send a message, or only managers from their own accounts? Will GSR fund a Tashkent lawyer sign-off + pd.gov.uz registration before go-live (1 Nov 2026 rules)?
4. Will GSR file the written request / pay 10–100 mln UZS for the Customs Committee statistics service, and accept a 4–8-week wait for the answer?
5. ERP access and ownership: client export (goods, volumes, districts), lost leads, unclaimed markings; who fixes the site→ERP code defect; `other` vs new `agent` key.
6. Budget and target CAC: monthly ceiling for data (customs sample/sub), ads (Google/Yandex/Telegram tests), tooling, Claude; acceptable cost per first shipment given margin per m³.
7. Manager capacity and SLA: how many new leads/week can the team actually call within 15 min, and who sits at the review desk (owner or supervisor)?
8. Telegram account: will GSR provision a dedicated aged (Premium) account for read-only monitoring and accept ban risk; how many groups may it join?
9. Hosting/data residency and model processing: DB in Uzbekistan? Claude (US) allowed on company-level text? Chinese-side (WeChat) data in scope at all?
10. Partnership appetite: commissions for referrals (clients, Yiwu translators, 1688 agents, customs brokers, accountants, leasing/bank desks), TransLogistica 10–12 Nov 2026 presence, brand-distributor outreach — these the owner must open personally; the agent only queues them.

## 5. Judgement

As written — 40+ collectors, registry enumeration, Telethon monitor plus sender, owner bot, Sheets mirror, backups, batch API, learning loop — this is a 6–9-month build for two developers, and it would launch into unverified data access (no enumerable registry, no legal declarant-level dataset), unmeasured yields, and a legal regime that from 1 Nov 2026 forbids most of the outreach it was designed to automate. A 2–3-month version is realistic only if scoped to: fix inbound, mine GSR's own data for lookalikes and reactivation, validate one customs dataset against known clients, crawl Uzum bulky categories, monitor hiring and 30–60 Telegram groups read-only, score with a coarse calibrated rule, push through the existing `other` door to the ERP, and report precision per source weekly — with a two-week measurement pilot gating every additional source. Expect tens of qualified leads per month, not hundreds; the partner and paid-search channels the dossier omitted will likely outperform the scrapers it spent most of its effort on.