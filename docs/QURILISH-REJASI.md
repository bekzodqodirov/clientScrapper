# Qurilish rejasi — GSR Qidiruv Agenti v1 (javoblaringiz asosida)

Sana: 2026-10-23. Egasi qarorlari: **STIR yo'q** (bojxona Excel'ida importyor ustuni yo'q) · **pullik ma'lumot va vositalarga pul yo'q** ·
**chegara: bitta partiyada ≥1 m³** · **alohida Telegram akkaunt bor** · **Python kollektorlar + lid holati ERP'da** ·
**«qayta qilinmasin» raqamlar abadiy bostiriladi**.

## 1. Bu qarorlar rejani qanday o'zgartirdi

| Qaror | Oqibat |
|---|---|
| Bojxona Excel'ida STIR yo'q | Excel faqat «qaysi tovar guruhi Xitoydan ko'p keladi» va mavsumiylik uchun ishlatiladi. Importyor **kimligi** faqat internetdan topiladi, STIR — kompaniya nomi bo'yicha reestrdan (stat.uz/my.soliq.uz/myorg.uz, bepul, faqat ball olgan nomzodlar uchun) |
| Pul yo'q | Broker ma'lumoti (vedrating, ImportGenius, Tendata, Volza), Apify, TGStat API, DataForSEO, Yandex Places, rezident proksi — **hech biri yo'q**. Faqat: mavjud server, bepul API'lar (hh.uz, Google Places bepul limiti, Instagram Business Discovery, Brave/Yandex bepul qidiruv), o'z yengil kraulerlar, Telethon. Claude — faqat Haiku, kunlik cheklov ≤$1 |
| ≥1 m³ / partiya | Nishon kengayadi: kichik ulgurjichilar, Uzum/Instagram do'konlari, bozor do'konlari ham kiradi. B2C («o'zim uchun», bitta dona) baribir chiqariladi. Ball jadvalida «1–3 m³» endi ijobiy signal, ERP oqim qoidasi `min_m3=1` |
| Alohida Telegram akkaunt | Faqat o'qish; hech qachon yozmaydi; menejer akkauntlariga tegmaydi |
| Python + ERP | Kollektorlar, boyitish, ball — Python servisi (`clientScrapper`); lid holati, karta, menejer ishi, natija — LogisticsERP. Agent ERP'ga faqat **push** qiladi va **read-only** o'qiydi |
| Bostirish ro'yxati | Uch manba: (1) ERP mijozlar kitobi telefonlari, (2) menejer yopgan lidlar `lost_reason = «Qayta qilinmasin»` (siz ERP lug'atiga qo'shasiz yoki men qo'shaman), (3) agentning o'z DNC ro'yxati (Telegram'da «yozmang» deganlar, noto'g'ri raqam ×2). Kalitlar: telefon oxirgi 9 raqam, Telegram username, Instagram handle, sayt domeni, STIR |

## 2. Nimani quraman (komponentlar)

```
[ERP read-only]  mijozlar telefonlari + «Qayta qilinmasin» lidlar + natijalar  ─┐
[Telegram o'quvchi]  30–60 guruh, regex → Haiku                                   ├─► normalizator → rezolver → bostirish darvozasi
[Internet kollektorlar]  Instagram · OLX · kataloglar · hh.uz · ko'rgazmalar · xaritalar ┘        → ball (≥1 m³ belgilari) → egasi botida karta
                                                                                                   → ERP'ga push (nega topildi + nima deyish) → push_log
                                                                                                   ◄── tungi natija o'qish (bosqich, lost_reason) → manba aniqligi
```

### 2.1 `clientScrapper` Python servisi (MIYA skeletidan nusxa)
- Jarayonlar: `worker` (kollektorlar, boyitish, ball, push), `userbot` (Telethon, faqat o'qish), `bot` (egasi uchun ko'rib chiqish stoli), PostgreSQL 16. Docker-compose, Makefile, `.env.example`, kunlik shifrlangan backup, health/heartbeat, xarajat cheklovi — MIYA'dan.
- Jadvallar: `companies`, `contacts`, `company_aliases`, `evidence` (har topilma: URL + sana + iqtibos), `signals`, `lead_candidates`, `push_log`, `outcomes`, `suppression`, `sources`, `interactions`, `chat_monitors`.
- Claude faqat **tasniflaydi** (nima olib keladi, hajm so'zlari, B2B/B2C, raqobatchi), ball — deterministik Python. Modelga shaxsiy telefon/username yuborilmaydi (tokenga almashtiriladi).

### 2.2 Kollektorlar v1 (hammasi bepul)

| # | Kollektor | Nima qiladi | Tezlik/limit |
|---|---|---|---|
| 1 | `erp_sync` | Mijozlar telefonlari → `suppression(client)`; `lost_reason=Qayta qilinmasin` lidlar → `suppression(dnc)`; agent lidlarining bosqichi/lost_reason → `outcomes` | tungi, ERP read-only (3-bo'lim) |
| 2 | `telegram_monitor` | Alohida akkaunt 30–60 ulgurji/kargo guruhiga qo'shiladi (kuniga ≤3), `kub/konteyner/fura/kim olib keladi/kerak` xabarlarini ushlaydi, kanal tavsifidagi biznes kontaktlarni oladi | real vaqt; FloodWait breaker |
| 3 | `instagram_discovery` | Bepul qidiruv (Yandex Cloud tungi 1 000/oy + SearXNG/DDG) bilan `optom/ulgurji/оптом + tovar + Toshkent` handle'larini topadi; bio/sayt/Telegram — Instagram Graph **Business Discovery** (bepul, o'z biznes akkaunt + app) yoki past tezlikdagi ommaviy sahifa o'qish | kuniga ≤200 profil |
| 4 | `olx_business` | Kategoriya × «optom/оптом/ulgurji/sklad/konteyner/zavod» e'lonlari, biznes akkauntlar, bitta sotuvchida ≥10 e'lon, matndagi Telegram | kuniga 1 marta, 1 so'rov/3 s |
| 5 | `directories` | goldenpages («Оборудование из Китая», ulgurji rubrikalar), yellowpages (`tovary-iz-kitaya`, `optovaya-torgovlya-*`), top.uz, glotr.uz `/showcase/`, prom.uz «страна производства: Китай» — kompaniya + telefon + manzil | oyda 1 to'liq, haftada 1 farq |
| 6 | `hh_vacancies` | hh.uz rasmiy API: «ВЭД», «закупщик Китай», «китайский язык», «tashqi savdo» → ish beruvchi, sayt | har 4 soat |
| 7 | `exhibitors` | UzBuild, Automechanika, TextileExpo, Power, MebelExpo ro'yxatlari — O'zbekiston kompaniyalari | ko'rgazmadan keyin |
| 8 | `maps` | 2GIS demo kalit (1 000 so'rov) + rubrika sahifalari; Google Places — faqat karta bog'lansa (bepul limit ichida, `place_id` saqlanadi) | haftasiga ≤1 000 |
| 9 | `brand_dealers` | Xitoy brendlari (Gree, Haier, Midea, Hisense, XCMG, Zoomlion, Sany, Changan, Chery, BYD, Jinko, Huawei Solar…) «O'zbekistondagi rasmiy distribyutor» sahifalari | oyda 1 |

Keyinroq (pul paydo bo'lsa yoki talab bo'lsa): Uzum (rezident proksi kerak), bojxona ma'lumoti namunasi, Telegram Ads.

### 2.3 Ball (≥1 m³ uchun)
Hajm: o'zi «konteyner/fura/20–40 ft» — 40; «3+ kub» — 30; «1–3 kub» yoki «partiya/optom + hajmli tovar» — 20; ulgurji katalog rubrikasi — 10; VED vakansiyasi — 15; ko'rgazma — 15. Xitoy manbai — 20. Kompaniya mustahkamligi (reestrda bor, faol, QQS) — 15. Yangilik — 10. Bog'lanish imkoni — 10. Salbiy: B2C so'zlar −40, raqobatchi −∞ (chiqariladi), tugatilgan −50, «Qayta qilinmasin» — chiqariladi.
≥60 ball + telefon → egasi botida karta → tasdiq → ERP. 40–59 → boyitish navbati. <40 → kutish.

### 2.4 Bostirish darvozasi (sizning talabingiz)
Har push oldidan va har karta oldidan: telefon oxirgi 9 raqam ∈ mijozlar ∪ «Qayta qilinmasin» ∪ DNC → **to'xtaydi, karta ham chiqmaydi**, sabab logga yoziladi. Telegram username / Instagram handle / sayt domeni ham tekshiriladi (bir odam yangi raqam bilan kelsa ham ushlanadi). ERP tomonida ham mavjud himoya qoladi (mijoz raqami → lid emas, mijoz kartasi).

### 2.5 Menejer nimani ko'radi
ERP'dagi oddiy «Yangi lid» kartasi, manba «Qidiruv agenti», izohda: NEGA TOPILDI (iqtibos + URL + sana) · NIMA OLIB KELADI · HAJM (dalil bilan) · KOMPANIYA (nom, STIR bo'lsa, shahar) · QO'NG'IROQ/XABAR UCHUN 2 jumla (narx va muddat va'dasiz) · ball tarkibi. `kub` maydoni to'ldirilgan, `min_m3=1` qoidasi bilan cargo jamoasiga tushadi. Menejer odatdagidek «📞 Bog'landim» bosadi, yopsa — sabab tanlaydi; «Qayta qilinmasin» tanlasa, agent ertasi kuni shu raqamni abadiy bostiradi.

## 3. ERP tomonida kerak bo'ladigan kichik o'zgarishlar
(aniq fayl yo'llari va hajmi — 4-bo'limda, tekshiruvdan keyin to'ldiriladi)

1. `lost_reasons` lug'atiga «Qayta qilinmasin — bizning mijoz emas» qatori (siz UI'dan qo'shasiz, kod kerak emas).
2. Manba kaliti `agent` (bitta konstanta + `lead_sources` qatori) — bo'lmaguncha `other` + `utm_source=clientscrapper`.
3. «Kelgan arizalar → Forma savollari»da `kub → quoted_volume_m3`; «Arizalar taqsimoti»da qoida `manba=agent, min_m3=1 → cargo menejerlar`.
4. Agent uchun read-only o'qish: ikkita kichik GET marshrut (`/api/agent/suppression`, `/api/agent/outcomes`, `X-ERP-Key` bilan, telefonlar xesh ko'rinishida) — 4.2 da batafsil; Postgres rol rad etildi.

## 4. Tekshiruv natijalari

### 4.1 Bepul qidiruv steki — nima haqiqatan bepul (to'liq: `docs/bepul-qidiruv-memo.md`)

| Yo'l | Bepul limit | Karta kerakmi | MVP'da |
|---|---|---|---|
| **Yandex Cloud Search API** | oyiga 1 000 so'rov, faqat tunda (Toshkent 02:00–10:00); RU/UZ so'rovlar uchun eng mos | yo'q (akkaunt) | **Ha** — tungi cron, kalit so'z matritsasi |
| **SearXNG** (o'z serverda) + DuckDuckGo HTML | cheksiz, lekin past tezlik (≤1 so'rov/s), Google dvigateli ishlamaydi, DDG vaqti-vaqti bilan bloklaydi | yo'q | **Ha** — zaxira qatlam |
| **OLX.uz** | `__PRERENDERED_STATE__` JSON va `/api/` (robots ruxsat beradi), biznes/xususiy bayrog'i bor; telefon alohida «ko'rsatish» qadami | yo'q | **Ha** |
| **2GIS Platform API** | demo kalit: 1 oy, har servis uchun 1 000 so'rov; keyin so'rov bo'yicha | yo'q | Ha — seed uchun; keyin HTML o'qish robots.txt'ga qarab |
| **hh.uz API**, **Instagram Business Discovery**, **kataloglar** (goldenpages, yellowpages, top.uz, glotr, prom) | bepul | yo'q | Ha |
| Instagram loginsiz profil | bio + 6–12 post ko'rinadi, barqaror emas | yo'q | Faqat boyitish, handle bo'yicha bir marta, kesh bilan |
| **Google Places API (New)** | har SKU uchun oyiga 1 000–10 000 bepul hodisa; telefon+sayt uchun eng kuchli manba | **ha** (billing yoqiladi, limit qo'yiladi) | Faqat siz kartani qattiq limit bilan bog'lasangiz |
| **Brave Search API** | yangi foydalanuvchilarga bepul tarif yo'q; oyiga $5 kredit ≈ 1 000 so'rov | **ha** | Faqat karta bo'lsa |
| Google CSE JSON API | yangi mijozlarga yopiq (2027-01-01 to'xtaydi) | — | Yo'q |
| Bing Search API | 2025-08 da yopilgan | — | Yo'q |

Kartasiz stek: Yandex (tun) + SearXNG/DDG (kun) + OLX JSON + 2GIS demo + hh.uz + kataloglar + Instagram Business Discovery + Telegram. Bu yetarli; karta bo'lsa Google Places telefon topishni ancha osonlashtiradi.

### 4.2 ERP tomonida eng kichik o'zgarish to'plami (to'liq: `docs/erp-ozgarishlar-memo.md`)

**Migratsiya kerak emas.** Tavsiya — (a) varianti: ikkita kichik GET marshrut, autentifikatsiya `agent` manbasining o'sha `X-ERP-Key` kaliti bilan (UI'da yaratiladi va bir tugma bilan bekor qilinadi). Read-only Postgres rol (b) rad etildi: 5432 port ochilmagan, parol `.env`ga qo'lda, xom telefon va ismlar agentga o'tadi.

| # | O'zgarish | Fayl | Hajm |
|---|---|---|---|
| 1 | `agent` manba kaliti (+ `lead_sources` qatori seed'da, idempotent) | `src/modules/wms/crm/inbound.ts`, `scripts/seed.ts` | 2 qator |
| 2 | `GET /api/agent/suppression?since=&cursor=` — mijozlar kitobidagi barcha telefonlar (faol va nofaol) + `lost_reason ∈ «Qayta qilinmasin»` lidlar; telefon **oxirgi 9 raqamning HMAC xeshi** (xom raqam chiqmaydi); kechasi to'liq, soatiga farq | `src/app/api/agent/suppression/route.ts`, `src/modules/wms/crm/agent-feed.ts` | ~45 + ~150 qator |
| 3 | `GET /api/agent/outcomes?since=` — agent lidlari uchun: `external_id`, tushish natijasi (created/joined/client/dropped), hozirgi bosqich (open/won/lost), `lost_reason`, `contacted_at`, `quoted_volume_m3`, `client_id`; ism va telefon **yo'q** | `src/app/api/agent/outcomes/route.ts` | ~45 qator |
| 4 | Sozlama `agent_no_contact_reasons = "Qayta qilinmasin"` (`;` bilan bir nechta) + 4 tilda tavsif (aks holda sozlamalar sahifasi render'da yiqiladi) | `settings/service.ts`, `messages/{ru,uz,zh-CN,en}.json` | 5 qator |
| 5 | **Kamar** (tavsiya): `landInboundLead` ichida faqat `agent` uchun — telefon bostirish to'plamida bo'lsa `dropped/suppressed` yozib qaytish, lid ochilmaydi (ERP bugun yo'qotilgan lidni qayta tushganda **yangi lid** ochadi — bu tuzatiladi) | `inbound.ts` | ~15 qator |
| 6 | Ixtiyoriy: agent lidlari uchun 15 daqiqalik «tegilmagan» eslatmasini o'chirish (kuniga 50–200 lidda rahbarga shovqin) | `inbound-notify.ts` | 1 qator |
| 7 | Testlar: kalitlar ro'yxati ↔ seed; HMAC/kursor; integratsiya (mijoz telefoni + «Qayta qilinmasin» lid feed'da, ochiq lid yo'q; bostirilgan tushish lid ochmaydi) | `tests/unit/*`, `tests/integration/agent-feed*` | ~150 qator |
| 8 | `docs/AGENT-FEED.md` — Python tomoni uchun kontrakt | docs | — |

Jami ≈ 9–11 fayl, 350–450 qator. Siz UI'dan qilasiz: **CRM → Sozlamalar** da «Qayta qilinmasin» yo'qotish sababi (seed qilinmaydi — ro'yxat bo'sh bo'lmasa erkin matn taqiqlanadi); **Kelgan arizalar → Reklama havolalari → Agent → Yoqish va kalit yaratish**; **Boshqaruv → Arizalar taqsimoti** da `manba=agent, min_m3=1 → cargo` + hajmsiz lidlar uchun ikkinchi qoida; **Forma savollari** da `kub → m³` (birinchi test lid kelgach bir marta).

Muhim: oynali qoida (`min_m3`) hajmi noma'lum lidga tushmaydi — shuning uchun ikkinchi, oynasiz `agent` qoidasi kerak. Agent lidlariga ham kunlik cheklov amal qiladi: manbaga 200, telefonga 3.

### 4.3 Bostirish qanday ishlaydi (oxirgi ko'rinish)

```
ERP:   mijozlar telefonlari ∪ «Qayta qilinmasin» lidlar  ──(HMAC xesh, kechasi to'liq, soatiga farq)──►  agent: suppression jadvali
agent: har nomzod → telefon oxirgi 9 raqam → xesh → to'plamda bormi?  bor → karta ham, push ham yo'q (log: suppressed)
       + o'z DNC: Telegram'da «yozmang», noto'g'ri raqam ×2, Instagram handle / username / sayt domeni bo'yicha
ERP:   kamar — agent push qilsa ham telefon to'plamda bo'lsa `dropped/suppressed`, lid ochilmaydi
```
Bir odam yangi raqam bilan kelsa: telefon bo'yicha ushlanmaydi (ERP lidda bitta telefon), lekin agent username/handle/domen bo'yicha ushlaydi. Sababni qayta nomlasangiz — eskisini o'chirmay yangi qo'shing yoki sozlamaga ikkalasini yozing.


## 5. Reja (10 ish kuni) va sizdan kerak bo'ladigan narsalar

| Kun | Ish |
|---|---|
| 1–2 | Skelet MIYA'dan, DB sxemasi, docker; ERP: `agent` kaliti, `kub` bog'lash, `min_m3=1` qoidasi, «Qayta qilinmasin» sababi |
| 3 | `erp_sync`: mijozlar + «Qayta qilinmasin» → bostirish; natijalar o'qish |
| 4–5 | `telegram_monitor`: akkaunt login, 10 seed guruh, regex + Haiku, birinchi kartalar |
| 6 | Rezolver + ball + bostirish darvozasi + egasi boti (`/lidlar`, `/manbalar`, `/holat`, `/toxtat`) |
| 7 | `directories` + `hh_vacancies` |
| 8 | `instagram_discovery` + `olx_business` |
| 9 | `crm_push` (soxta server testlari: 200/404/500), birinchi real push'lar, bitta menejer bilan pilot |
| 10 | `exhibitors`, `maps`, `brand_dealers`; haftalik hisobot; Sheets ko'zgusi (ixtiyoriy) |

Sizdan: (1) Telegram akkaunt raqami va login kodini kiritadigan odam; (2) ERP'da `agent` manbasi uchun kalit yaratish (Kelgan arizalar → Reklama havolalari) va read-only kirish; (3) 10–20 ta raqobatchi/ulgurji kanal ro'yxati (seed); (4) qaysi serverga deploy qilamiz (MIYA turgan server bo'lsa, alohida DB va konteynerlar); (5) ko'rib chiqish stolida kim o'tiradi (siz yoki rahbar), kuniga 20–30 daqiqa.

Xarajat: server — mavjud; Claude Haiku — kuniga ≤$1 cheklov; Telegram Premium — ixtiyoriy $5; qolgani 0.

## 6. Sizdan 3 ta qaror (javob bersangiz boshlayman)

1. **Karta.** Google Places (telefon va sayt uchun eng kuchli bepul limit) va Brave uchun billing kartasi qattiq limit bilan bog'lanadimi? a) ha, $5–10/oy limit bilan — **tavsiya**; b) yo'q, faqat kartasiz stek.
2. **ERP o'zgarishlari.** 4.2 dagi to'plamni men `LogisticsERP` repoda alohida branch'da qilaymi (testlar, typecheck bilan, siz ko'rib chiqib deploy qilasiz)? a) ha — **tavsiya**; b) faqat spetsifikatsiya, o'zingiz qilasiz.
3. **Eslatma.** Agent lidlari uchun ERP'ning 15 daqiqalik «tegilmagan» eslatmasi: a) o'chirilsin (menejerga xabar baribir boradi) — **tavsiya**; b) qolsin.

«Boshla» desangiz — 1-kun ishidan boshlayman: ERP branch (agent kaliti, ikki feed, kamar, testlar) va `clientScrapper` skeleti.
