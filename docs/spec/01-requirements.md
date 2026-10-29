# 01 — Requirements

## 1. Actors

| Actor | Role | Interface |
|---|---|---|
| Owner (Bekzod) | Reviews candidates, toggles sources/chats, sets caps, handles DNC/purge requests | Owner bot (aiogram), weekly digest, Sheets mirror (optional) |
| Sales manager | Receives leads in the ERP, contacts the company from their own Telegram account, moves the lead through stages, picks a lost reason | ERP (fullprompt) only; optional card from the owner bot for Telegram-only prospects |
| ERP (fullprompt) | System of record for leads, clients, outcomes | `POST /api/leads/in/agent`, `GET /api/agent/suppression`, `GET /api/agent/outcomes` |
| Reader Telegram account | Read-only presence in 30–60 public groups/channels | Telethon |
| Prospect | Uzbek company importing from China | Never contacted by the agent |

## 2. Ideal customer profile (ICP) — executable

Include when **all** hold:
- Located in Uzbekistan (Tashkent first; regions allowed).
- A legal entity or a trading business (MChJ/OOO/XK/QK/AJ, or YaTT/ИП with a shop, warehouse or wholesale listing). Signals: legal form, STIR, wholesale rubric, "optom/ulgurji/оптом", shop/warehouse address, ≥10 listings, marketplace shop with legal block.
- Imports from China now or plausibly: explicit (Xitoydan/из Китая/1688/Taobao/Yiwu/Guangzhou, Chinese brand dealership, VED/China-procurement vacancy) or sector prior (furniture, appliances, electronics, lighting, sanitary, tools, textile machinery/fabrics, building finishes, toys, packaging, auto parts, EV/cars, solar).
- Volume ≥1 m³ per shipment: stated («2 kub», «konteyner», «fura»), implied (wholesale + bulky category, ≥10 listings, order velocity, VED vacancy, exhibitor, dealer of a bulky brand), or measured (ERP receipts for unclaimed markings).

Exclude when **any** holds: B2C wording («o'zim uchun», «для себя», single item, personal profile with no business); cargo/forwarding/customs-broker/logistics company (competitor); Chinese-side supplier; state body (tender path, not direct); liquidated entity; phone/handle/STIR in suppression; an open push for the same company in the last 90 days or the same phone in the last 24 hours.

Two clauses above depend on what the ERP exposes (T-07 verification, 2026-10-24): the «measured» volume signal (§7 question 3) and «phone/handle/STIR in suppression» for ERP clients, which the ERP feeds by phone only (§7 question 2).

Segments (for routing and scripts): A bazaar wholesalers · B marketplace sellers · C factories (equipment/raw materials) · D construction/furniture importers · E electronics/auto/appliance distributors.

## 3. Functional requirements

| ID | Requirement | Acceptance |
|---|---|---|
| F-01 | Collect observations from the open web per `03-collectors.md` on their schedules, within per-host rate limits and daily page budgets | Each collector has a fixture test and a live smoke test; `collector_runs` rows exist; no host breaker tripped in normal operation |
| F-02 | Store immutable evidence (URL, fetched_at, sha256, excerpt, gzipped body) for every observation | Every candidate card can show URL + date + quote |
| F-03 | Normalise phones (E.164, tail-9), handles, domains, names (name_key) and STIR | Golden tests over the examples in `04-processing.md` |
| F-04 | Resolve observations to companies/contacts with exact keys first, then fuzzy name match with review band | Resolver tests: merge ≥90, review 80–89, new <80; shared phones never merge |
| F-05 | Enrich cheapest-first (regex → registry → SERP/Places → Claude) and stop when score ≥60 and a phone exists | Enrichment budget per company respected; steps logged |
| F-06 | Score deterministically 0–100 with stored breakdown; tiers review/enrich/hold | Golden scoring tests; breakdown visible on card |
| F-07 | Apply gates and suppression before any card and any push | Suppressed/competitor/B2C never reach the owner; test fixtures |
| F-08 | Owner review via bot with ✅ ✖️ 🔀 🔒 ⏭; decisions audited | `lead_reviews` rows; callback parser fence test |
| F-09 | Push approved leads with a phone to the ERP with the Uzbek note template, idempotent `external_id`, business-hours window, `PUSH_DAILY_CAP` | Fake-ERP tests 200/404/500/timeout/replay; 404 trips the breaker and alerts the owner |
| F-10 | Sync suppression (full nightly, delta hourly) and outcomes (nightly) from the ERP; verify HMAC rule and salt_id | Contract tests over recorded feed responses; resync on salt change |
| F-11 | Compute per-source precision weekly; auto-push only for sources ≥60% over ≥15 pushes; pause sources <20% over ≥30 pushes with an alert | `/holat` shows precision; tests over synthetic outcomes |
| F-12 | Telegram-only prospects (no phone, score ≥60, username known) reach state review as tier 'warm' after three enrichment attempts and go to a manager via the owner bot as a card with a deep link; the manager's `/tel` turns an obtained phone into a push; never auto-sent | Card template test; `test_manager_handoff`; no Telegram write calls (AST test) |
| F-13 | Owner commands: /lidlar /manbalar /chats /holat /xarajat /toxtat /yoq /import /unut /qidir /dnc /vazn /push tekshir /stir | Each command has a handler test; callback_data parser fence |
| F-14 | Daily 09:00 digest and health alerts (collector_blocked, push_404, cost_cap, userbot_unauthorized, flood_breaker, no_signals_24h) | Health tests; dead-man ping configured |
| F-15 | Retention and purge: raw evidence 180 d, intent posts 30 d, unanswered leads 90 d → company-tier only, DNC hashes forever; `/unut` purges a company/person with audit | Retention job tests; purge_log rows |

## 4. Non-functional requirements

| ID | Requirement |
|---|---|
| N-01 | Budget: total paid spend ≤ $10/day and ≤ $250/month, enforced by `costs.ask` before each paid call; refusal is logged; alert at 80% |
| N-02 | No message, call, SMS or e-mail is ever sent to a prospect by the system; enforced by an AST test and by the absence of a sender module |
| N-03 | Legal: company-tier data free; person-tier limited to one decision maker with basis/source/date; pseudonymised model calls; retention as F-15; data hosted in Uzbekistan or an adequacy-list country; registration of the database at pd.gov.uz is the owner's task before go-live |
| N-04 | Robustness: every external call has timeout, retry with backoff, breaker; the process survives Telegram flood waits; kill switches `COLLECTORS_ENABLED`, `USERBOT_ENABLED`, `CRM_PUSH_ENABLED`, runtime `/toxtat` |
| N-05 | Observability: JSON logs with redaction, heartbeats per job, `/holat`, digest, usage_log priced per call |
| N-06 | Latency: Telegram intent → owner card p90 < 5 minutes (event-driven: userbot NOTIFY → classify/score → review cards every 2 min in review hours); approved push → ERP < 1 minute inside business hours |
| N-07 | Throughput: up to 200 observations/hour, 2,000 companies/month, 25 pushes/day without schema changes |
| N-08 | Portability: single VPS (4 vCPU/8 GB), Docker Compose, nightly encrypted backups, restore drill monthly |
| N-09 | Code quality: ruff + mypy on services, pytest with Postgres in CI, fixtures for every parser, no secrets in repo |

## 5. Success metrics

| Metric | MVP (end of week 2) | Month 3 |
|---|---|---|
| Pushed leads with phone and score ≥60 | ≥20 | ≥40/week |
| Precision (not lost as «Import qilmaydi» / «Raqam notoʻgʻri» / «Kargo/konkurent») | ≥60% | ≥75% |
| Contacted within 1 business day (`contacted_at`) | ≥40% | ≥70% |
| Quotes sent / contacted | ≥25% | ≥35% |
| First shipment / pushed (60-day lag) | ≥3% | ≥8% |
| Paid spend | ≤$5/day | ≤$10/day |
| Cost per qualified lead | <$5 | <$12 |

How the ERP-fed rows can be measured (T-07 verification, 2026-10-24): precision needs the owner's lost-reason labels (§7 question 1); «contacted within 1 business day» counts only contacts the ERP can see and is measured from `push_log.sent_at` (§7 question 6); «quotes sent» and «first shipment» are not in the ERP's feeds and have a v1 proxy until the owner decides (§7 questions 5 and 3).

## 6. Constraints and assumptions

- The ERP branch `agent-feed` may not be deployed on day 1: the agent must work with `POST /api/leads/in/other` + `utm_source=clientscrapper` and a weekly client XLSX import for suppression until the feeds exist.
- The customs Excel in `TelegramCustoms` has no importer identity; it is used only for category priors.
- Web search engines index Uzbek sites unevenly; directory rubric pages and Telegram catalogues must be read directly (see `docs/ochiq-internet.md` §4).
- From 2026-11-01 advertising calls/SMS require prior consent and are banned 18:00–09:00; the design keeps all outreach human, 10:00–17:00, with an opt-out line.

## 7. Open questions (owner decisions)

**Answered by Bekzod on 2026-10-29** — «1 a bular hozir ham bor / Qolgani sen aytgandek». Every
question below is decided as its ⭐ option; the «Meanwhile» lines are superseded by these:

| # | Decision | What it means for the build | Owner of the work |
|---|---|---|---|
| 1 | **(a)**, follow-up **(a)** | The lost-reason dictionary in the live ERP already holds the reasons («bular hozir ham bor»); only «Qayta qilinmasin» is added if it is missing (05 §A.14 step 2). The exact spellings are the owner's, so the agent keeps matching them by keyword (`outcomes.LOST_REASON_PATTERNS`) and must surface every dictionary label it could not class (in `/holat` and the digest) so a new label is mapped, never silently `lost_other`. «Raqam notoʻgʻri» is NOT added to `agent_no_contact_reasons`: the agent closes a number itself after two wrong-number outcomes. | T-10 (patterns), T-35/T-36 (unclassed labels shown) |
| 2 | **(a)** | Agent side only: when a pushed company becomes `won`/`landed_client`, the agent suppresses all of that company's keys (Telegram username, Instagram, domain, STIR), not only the pushed phone tail; every manager card carries the line «Yozishdan oldin ERPda nomi boʻyicha qidiring». Old ERP clients stay phone-only. | T-10/T-19 (keys), T-20/T-21 (card line) |
| 3 | **(a)** | `won` is the first-shipment proxy in `/holat` and `tools/mvp_report.py`, labelled as a proxy; no `measured` volume signal in v1. After the pilot an ERP round adds a feed of the client's first receipt date and m³ (no price). | T-36 now; ERP round after the pilot |
| 4 | **(a)** | `agent-feed` is merged and deployed to production BEFORE the pilot; the «Boshqa» fallback is not used for pushes (it stays a capability only). | Owner deploy (05 §A.14), T-29 |
| 5 | **(a)** | Quotes-sent is judged from the lead's current stage name: a configurable list of «price was given» stage names (default: stages whose name contains «narx», plus every `won` stage), approximate and labelled so. The owner can correct the list later. | T-36 |
| 6 | **(a)** | Seller rule in `docs/sotuv-playbook.md`: after the first message press «📞 Bogʻlandim» or move the stage, and write only from an ERP-connected Telegram account. The metric stays as measured, with the under-count note. | Playbook (done), T-29 briefing |


Raised by the T-07 verification (2026-10-24) against the ERP as built (fullprompt `agent-feed` at
`23fe1736`, 05 Part A): requirements in §2 and §5 that the ERP cannot satisfy today. None of them is
weakened here; each is a decision for Bekzod. The lead asks him the Uzbek question as written, with
the options; ⭐ marks the recommendation. Until he answers, the build follows the line «Meanwhile».
The ERP side of each is in 05's open questions 12–17.

1. **Precision needs lost-reason labels** (§5 precision, F-11, the agent's «wrong number ×2» DNC; 05
   Q12, Q6). The ERP sends a lost reason only when it is a label of the owner's lost-reason dictionary
   (or of the `agent_no_contact_reasons` setting); a seller's free text arrives as `null`. The owner's
   step 2 (05 §A.14) adds only «Qayta qilinmasin», so «Import qilmaydi», «Raqam notoʻgʻri» and
   «Kargo/konkurent» losses arrive as `lost_other` (neutral) and the wrong-number rule never fires.
   **Savol:** «Sotuvchi lidni yoʻqotganda sababini roʻyxatdan tanlaydi. Agent qaysi manba yaxshi lid
   berayotganini oʻlchashi uchun shu roʻyxatda quyidagi sabablar boʻlishi kerak. Qaysi variant?
   a) ⭐ CRM sozlamalari → «Yoʻqotish sabablari»ga «Qayta qilinmasin» bilan birga «Import qilmaydi»,
   «Raqam notoʻgʻri», «Kargo/konkurent», «Kichik yuk», «Narx qimmat» ni ham qoʻshamiz; jamoa hozir
   ishlatadigan boshqa sabablarni ham oʻsha zahoti qoʻshamiz (roʻyxatda birinchi sabab paydo boʻlgach,
   faqat roʻyxatdagi sabab tanlanadi);
   b) faqat «Qayta qilinmasin» — agent aniqlikni faqat «yutildi / bogʻlanildi» boʻyicha oʻlchaydi,
   notoʻgʻri raqam va konkurentlar avtomatik yopilmaydi, avtomatik push juda kech yoqiladi;
   c) roʻyxat kerak emas — aniqlik oʻlchanmaydi, avtomatik push hech qachon yoqilmaydi.»
   Follow-up if (a): «“Raqam notoʻgʻri” ni `agent_no_contact_reasons` sozlamasiga ham qoʻshamizmi?
   a) ⭐ yoʻq — agent raqamni ikki marta “notoʻgʻri” boʻlgandan keyin oʻzi yopadi, bitta xato bosish
   raqamni abadiy yopmaydi; b) ha — birinchi marta tanlanganda raqam ERPda ham abadiy yopiladi.»
   Meanwhile: the labels table of 05 §B.2 / 02 §2.11 stays as specified; precision counts what
   arrives.
2. **ERP clients reached by another phone, handle, domain or STIR** (§2 exclusion «phone/handle/STIR
   in suppression», F-07; 05 Q13). The ERP feeds only nine-digit phone hashes and checks arrivals by
   phone; an ERP client or a «Qayta qilinmasin» lead that the agent finds through another number, a
   Telegram username, an Instagram account, a website or a STIR is not suppressed, and F-12's
   Telegram-only cards never reach any ERP check.
   **Savol:** «ERP agentga mijozlarni faqat telefon raqami boʻyicha beradi. Mavjud mijozni (yoki
   “Qayta qilinmasin” lidni) agent boshqa raqam, Telegram, Instagram, sayt yoki STIR orqali topsa, u
   yangi lid boʻlib keladi; raqamsiz (faqat Telegram) kartalar ERPda umuman tekshirilmaydi. Qaysi
   variant?
   a) ⭐ hozircha agent tomonida: agent oʻzi yuborgan va “yutildi” boʻlgan kompaniyaning Telegram,
   Instagram, sayt va STIR ini ham yopadi; menejer kartasida “yozishdan oldin ERPda nomi boʻyicha
   qidiring” qatori boʻladi; ERPdagi eski mijozlar faqat raqam boʻyicha tekshiriladi;
   b) ERPga keyingi bosqichda: mijoz kartasidagi Telegram, Instagram, sayt va STIR ham (xeshlangan
   holda) agentga beriladi — buning uchun avval kartada shu maydonlar boʻlishi kerak;
   c) hech narsa qilmaymiz — menejer oʻzi tekshiradi.»
   Meanwhile: phone-only for ERP clients; `won`/`landed_client` suppress the pushed phone tail
   (02 §2.11); option (a) would widen those two rows to the company's other keys.
3. **Measured volume and first shipment** (§2 «measured (ERP receipts for unclaimed markings)», §5
   «First shipment / pushed»; 05 Q14). No ERP route returns receipts, unclaimed markings, cargo volumes
   or shipments; `stage_kind='won'` means the lead became a client card and a deal, not that cargo
   arrived.
   **Savol:** «ERP hozir agentga prixod, kub va birinchi yuk kelganini bermaydi. Shuning uchun egasiz
   markirovkali yukdan olinadigan “oʻlchangan hajm” belgisi ishlamaydi va “birinchi yuk / yuborilgan
   lid” koʻrsatkichini oʻlchab boʻlmaydi. Qaysi variant?
   a) ⭐ hozircha “yutildi” (lid mijozga aylandi) ni birinchi yuk oʻrnida hisoblaymiz, oʻlchangan hajm
   belgisi v1 da yoʻq; pilotdan keyin ERPga alohida bosqich: mijozning birinchi prixodi sanasi va kubi
   (narxsiz) agentga beriladi;
   b) hozirdan ERPga shu bosqichni qoʻshamiz (pilot kechikadi);
   c) bu koʻrsatkichlar kerak emas.»
   Meanwhile: `won` is the proxy in `/holat` and `tools/mvp_report.py`, labelled as such; no
   `measured` signal is produced.
4. **The 15-minute reminder during the «Boshqa» fallback** (binding decision «Reminder»; §6 first
   constraint; 05 Q15). Only the `agent` door is reminder-exempt. Through the fallback every agent push
   is reminded like an advert lead and counts in people's per-phone cap; production runs only the
   fallback until `agent-feed` is merged and deployed.
   **Savol:** «Agent lidlari uchun 15 daqiqalik “tegilmagan lid” eslatmasi oʻchirilgan, lekin bu faqat
   yangi “Agent” eshigida ishlaydi. ERPning bu yangi qismi tayyor, lekin hali serverga qoʻyilmagan;
   vaqtincha “Boshqa” eshigidan yuborsak, har bir agent lidi uchun eslatma keladi va oddiy
   mijozlarning limitiga qoʻshiladi. Qaysi variant?
   a) ⭐ pilotdan oldin ERPning yangi qismini serverga qoʻyamiz (faqat birlashtirish va deploy
   qolgan) — “Boshqa” eshigi ishlatilmaydi;
   b) vaqtincha “Boshqa” eshigidan kuniga 10 tagacha lid yuboramiz, eslatmalarga chidaymiz;
   c) yangi qism qoʻyilguncha push qilmaymiz, faqat kartalarni koʻrib chiqamiz.»
   Meanwhile: the fallback stays a capability (05 §B.3, `PUSH_DAILY_CAP ≤ 10`), not the plan.
5. **«Quotes sent / contacted»** (§5; 05 Q16). The outcomes feed carries only the lead's CURRENT stage
   and never a price, by design; a lead that passed «Narx aytildi» and moved on is indistinguishable
   from one that never got a price.
   **Savol:** «ERP agentga lidning faqat hozirgi bosqichini aytadi, narxni hech qachon bermaydi.
   “Narx aytildi” bosqichidan oʻtib ketgan lidni agent koʻrmaydi. “Narx aytilgan / bogʻlanilgan”
   koʻrsatkichi uchun qaysi variant?
   a) ⭐ hozircha bosqich nomi boʻyicha: siz qaysi bosqichlar “narx aytilgan” degani ekanini aytasiz
   (masalan “Narx aytildi” va undan keyingilari, “Yutildi” ham), agent hozirgi bosqichni shu roʻyxat
   bilan solishtiradi — taxminiy;
   b) ERPga keyingi bosqichda: lidga birinchi marta narx yozilgan vaqt (summasiz) agentga beriladi;
   c) bu koʻrsatkich kerak emas.»
   Meanwhile: the metric is not computed; `/holat` shows it as «—».
6. **«Contacted within 1 business day» is measured partially** (§5; 05 Q17). `contacted_at` reflects
   only what the ERP can see: the calls app, a message from a CONNECTED Telegram account on a chat the
   lead owns, a note, a stage move or a cleared follow-up by the responsible person, or the «📞
   Bogʻlandim» button. A seller writing from a personal account whose chat sits undecided on the tray
   is not reported, and the outcomes row has no arrival time (the agent measures from
   `push_log.sent_at`). The metric under-counts.
   **Savol:** «ERP “bogʻlanildi” ni faqat oʻzi koʻrgan narsadan biladi: qoʻngʻiroqlar ilovasi, ERPga
   ulangan Telegram akkauntdan yozilgan xabar, lidga yozilgan izoh, bosqich oʻzgarishi yoki “📞
   Bogʻlandim” tugmasi. Sotuvchi ERPga ulanmagan shaxsiy Telegramdan yozsa, agent buni koʻrmaydi va
   koʻrsatkich past chiqadi. Qaysi variant?
   a) ⭐ sotuvchilarga qoida: birinchi xabardan keyin “📞 Bogʻlandim” ni bosish yoki bosqichni
   oʻzgartirish, yozishmalar faqat ERPga ulangan akkauntdan (sotuv qoʻllanmasida bor);
   b) koʻrsatkichni shundayligicha qabul qilamiz (past chiqadi);
   c) bu koʻrsatkichni olib tashlaymiz.»
   Meanwhile: measured as specified, with the under-count stated beside the number in `/holat`.

## Review pass changelog

**T-07 verification (2026-10-24)** — an adversarial check of the spec against fullprompt `agent-feed` at `23fe1736` found seven requirements the ERP as built cannot satisfy. None was weakened: §7 lists them as owner decisions (questions 1–6, Uzbek wording, options, ⭐), and one-line pointers were added under §2 (the «measured» and «handle/STIR» clauses) and §5 (how the ERP-fed metrics can be measured). The seven findings fit six questions: measured volume and first shipment share question 3. §5's «Raqam notoʻgʻri» is now spelt with ʻ, as the recommended dictionary label of question 1.

**Owner answers (2026-10-29)** — §7's six questions answered «1 a bular hozir ham bor / Qolgani sen aytgandek»: all ⭐ options (1a with follow-up a, 2a, 3a, 4a, 5a, 6a); the decision table at the top of §7 states what each means for the build and which task carries it.
