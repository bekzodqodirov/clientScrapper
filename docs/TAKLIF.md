# GSR Logistics — B2B mijoz topuvchi agent: taklif

Sana: 2026-10-23. Muallif: Claude (tadqiqot + loyiha). Tilimiz: o'zbekcha; kod va texnik nomlar inglizcha.

Maqsad: Xitoydan **≥3 m³ / fura / 20–40 ft konteyner** hajmida tovar olib keladigan O'zbekiston
kompaniyalarini (B2B) **doimiy** topib, kontaktlarini yig'ib, LogisticsERP (gsrwms.uz) CRM'dagi sotuvchilarga
tayyor lid qilib uzatadigan avtonom agent. B2C (o'ziga Taobao'dan buyurtma qiladigan jismoniy shaxslar) — qamrovdan tashqari.

---

## Qisqa xulosa (2 daqiqalik)

1. **Eng katta kaliti sizda.** LogisticsERP'dagi o'lchangan m³ bilan 400+ mijoz — skoringni kalibrlash va «o'xshash»larni topish uchun etalon; kodsiz kelgan yuklar — tayyor lidlar; `TelegramCustoms` ga yuklanadigan bojxona Excel'ida **importyor nomi/STIR ustuni bo'lsa** — boshqa manbalarning yarmi keraksiz bo'lib qoladi. Bu 1-savol.
2. **Rasmiy «importyorlar ro'yxati» yo'q.** Bojxona va stat.uz faqat agregat beradi. Firma kesimidagi deklaratsiya ma'lumoti faqat (a) Bojxona qo'mitasining 2025-yil avgustdagi pullik xizmati orqali — korxona kesimida beriladimi, so'rov bilan aniqlash kerak, yoki (b) kulrang brokerlar (vedrating, ImportGenius, Tendata, Volza) orqali — hammasi bitta manbani sotadi, 10–13 oy orqada, provenansi noma'lum; faqat namuna 20 mijozingiz STIR'i bilan tekshirilgach, bittasini olish mumkin.
3. **Eng yaxshi bepul signallar:** hh.uz'dagi «ВЭД/закупщик Китай/китайский язык» vakansiyalari (kompaniya nomi bilan), Uzum'ning hajmli kategoriyalaridagi do'konlar (buyurtma soni ochiq), Telegram ulgurji va kargo guruhlaridagi «X kub / konteyner kerak» xabarlari (faqat o'qish), ko'rgazma ishtirokchilari ro'yxatlari, uzts.uz/openinfo.uz/farmatsevtika reestrlari, OLX biznes akkauntlar. Kontakt va STIR uchun — myorg.uz, 2GIS, yellowpages/goldenpages, stat.uz EGRPO, my.soliq.uz QQS reestri.
4. **Huquq 2026-11-01 dan qattiqlashadi:** reklama SMS/qo'ng'iroqlar faqat oldindan rozilik bilan, 18:00–09:00 taqiq; AI bilan noqonuniy ishlov uchun 50–100 BRV + vositalarni musodara. Shuning uchun **agent hech kimga yozmaydi**: u topadi, tekshiradi, baholaydi va ERP'dagi menejerga «nega topildi» dalili bilan uzatadi; menejer o'z akkauntidan, qo'lda, kuniga 15–20 ta yozadi.
5. **Reja:** 10 ish kunlik MVP (3 manba + ERP'ga push + egasi ko'rib chiqadigan bot) → 2 haftalik o'lchov → har yangi manba faqat o'lchangan aniqlik bilan qo'shiladi. Oylik xarajat MVP'da ≈$75–150, 3-oyda ≈$300–850. Kutilgan natija: oyiga o'nlab malakali lid, yuzlab emas.
6. **Darhol tuzatish:** sayt `GSR-…` kodli xabar yaratadi, ERP faqat `LEAD-…` qabul qiladi — saytdan kelgan mijozlar avtomatik lid ochmayapti. Bitta konstanta.

---

## 0. Sizda allaqachon bor narsalar (repolardan o'qidim)

| Tizim | Repo | Agent uchun nima beradi |
|---|---|---|
| **LogisticsERP** (gsrwms.uz) | `LogisticsERP` | Tayyor lid eshigi: `POST /api/leads/in/<manba>` + `X-ERP-Key`, `{name, phone, note, ...}`; navbat bilan sotuvchiga taqsimlash, manba/so'z/kub bo'yicha oqim qoidalari, sotuvchiga Telegram xabar + «📞 Bog'landim», 15 daqiqada rahbarga eslatma, telefon bo'yicha takroriy lid/mavjud mijozni tanish, `lead_sources`, `crm_people`. Menejerlarning **o'z Telegram akkauntlari** GramJS orqali ulangan — yozishma CRM'da. |
| **Sayt** gsrlogistics.uz | `gsrlogistics.uz` | `/api/lead/assign` kontrakti (team=cargo/buying/general, tag, LEAD-kod), tariflar (`tariffs.json`), xizmatlar ro'yxati, 3 ta Xitoy qabul punkti. Sotuv matnlari uchun «tasdiqlanmagan da'vo yo'q» qoidasi (`check-content.mjs`). |
| **MIYA** | `dailyJournal` | Python stack: FastAPI + PostgreSQL 16/pgvector + APScheduler, **Telethon userbot** (passiv o'quvchi + egasi tasdiqlagan yuborish, flood-wait, kunlik limitlar, bitta bayroq bilan o'chirish), Claude bilan JSON ekstraksiya, rapidfuzz bilan odam/kompaniya birlashtirish, CSV/XLSX import, Sheets sync, backup, health. |
| **Rastamojka boti** | `TelegramCustoms` | **500 000+ qatorli bojxona deklaratsiyalari statistik bazasi** (TN VED, tovar nomi, narx, miqdor, netto/brutto, kelib chiqish davlati, sana). Agar asl Excel'da **importyor (oluvchi) nomi / STIR** ustuni ham bo'lsa — bu eng kuchli manba (quyida 1-savol). |
| **clientScrapper** | bo'sh repo | Yangi agent shu yerda yashaydi. |

Xulosa: CRM, taqsimot, sotuvchi bildirishnomasi, Telegram-yozishma, dedupe — hammasi **bor**. Agentning ishi faqat
**topish → tozalash → baholash → `/api/leads/in/agent` ga yuborish → natijani qaytib o'qish**.

## 1. Birinchi navbatda hal qilinadigan savol: bojxona deklaratsiyalari ma'lumoti

«Kim Xitoydan ≥3 m³ olib keladi?» degan savolga eng to'g'ri javob bojxona deklaratsiyalarida (GTD) yotadi:
importyor nomi + STIR + TN VED + netto/brutto + kelib chiqish davlati. Bu ma'lumot bo'lsa, qolgan hamma manba faqat
**kontakt topish va boyitish** uchun kerak bo'ladi.

Tadqiqot natijasi (manbalar bilan hisobotning 3-bo'limida):

| Yo'l | Holat | Nima qilish kerak |
|---|---|---|
| Sizdagi 500k qatorli Excel (`TelegramCustoms`) | Parser faqat TN VED, tovar, narx, miqdor, netto/brutto, davlat, sana ustunlarini oladi; **importyor ustuni bor-yo'qligi noma'lum** | Asl Excel'ni oching: «Получатель/Импортёр», «ИНН/СТИР», «Декларант» ustunlari bormi? Bo'lsa — bu loyihaning №1 manbasi va parserga 3 ustun qo'shish kifoya |
| Bojxona qo'mitasining rasmiy pullik xizmati | 2025-yil 12-avgustdan «tashqi savdo bojxona statistikasi»ni to'g'ridan-to'g'ri integratsiya orqali sotish tartibi bor (lex.uz 7682204): yiliga 25–250 BHM (~10–103 mln so'm). **Korxona kesimida beriladimi — hujjatda aniq emas** | Imex services LLC nomidan rasmiy so'rov yuboring: «STIR kesimida, Xitoydan import, chorak bo'yicha» variant va narxini so'rang |
| Rossiyalik qayta sotuvchilar (vedrating.ru, ved-uz.ru, vedsbase.ru) | vedrating: 2024-yil uchun 59 062 TIF ishtirokchisi, shundan 55 376 importyor, STIR + manzil + tovar + taxminiy $ hajm; narx so'rov bo'yicha | Demo so'rang va **20 ta mavjud mijozingiz STIR'i** bilan tekshiring (nechtasi bor, oxirgi yuk sanasi qancha yangi). Provenansi kulrang — faqat ichki skoring uchun, qayta sotmaslik |
| Xalqaro brokerlar (ImportGenius, TradeInt, Tendata, Volza, 52wmb) | Hammasi bitta kulrang manbani qayta sotadi; yangilik oynasi har xil (ImportGenius: 2023-01…2025-08). Panjiva va Datamyne'da O'zbekiston **yo'q** | Faqat eng yangi oyi 2026 bo'lgan vendorga pul to'lang; TradeInt Lite ~$999/yil, Volza ~$1 500 dan |
| Rasmiy ochiq ma'lumot (customs.uz, stat.uz) | Faqat agregat (davlat × TN VED × oy). Korxona kesimi yo'q | Skoring uchun «qaysi tovar guruhida Xitoy ulushi katta» jadvalini olish |

Huquqiy eslatma: deklaratsiya mazmuni TIF ishtirokchisi uchun tijorat siri hisoblanadi (Bojxona kodeksi, «Tijorat siri» qonuni).
Kompaniya darajasidagi (MChJ/OOO) yozuvlar shaxsiy ma'lumot emas, lekin **YaTT (ИП)** qatorlari — jismoniy shaxs ma'lumoti.

## 2. LogisticsERP bilan ulanish — texnik kontrakt (repodan o'qildi)

Agent lidni **`POST https://gsrwms.uz/api/leads/in/<manba>`** orqali yuboradi (`X-ERP-Key` sarlavha, JSON tana).
Qoidalar (`src/modules/wms/crm/inbound.ts`, `docs/LEAD-ADS.md`):

| Qoida | Agent uchun oqibati |
|---|---|
| Ruxsat etilgan manba kalitlari: `meta, instagram, facebook, telegram, tiktok, google, sayt, other` | `agent` kaliti **yo'q** — ERP'ga bitta konstanta + `lead_sources` qatori qo'shish kerak (aks holda `other` + `utm_source=agent`) |
| `phone` ham, `name` ham bo'lmasa — `no_contact`, tashlab yuboriladi | Faqat telefon yoki ism bor lidlar yuboriladi |
| Identifikatsiya **faqat telefon** (oxirgi 9 raqam) | Telegram-username'li, telefonsiz nomzodlar agentning o'z bazasida «telefon kerak» holatida turadi; ERP'ga telefon topilgach yuboriladi |
| Telefon mijozlar kitobida bo'lsa → lid emas, mijoz kartasiga savol tushadi | Agent 400+ mijoz ro'yxatini **oldindan** bostirish (suppression) ro'yxati qilib saqlaydi |
| Bir telefonga kuniga 3 ta, bir manbaga kuniga 200 ta cheklov | Agentning kunlik push limiti 20–30; bir telefon kuniga 1 marta |
| Javob doim `{"ok":true}` — natija bilinmaydi | Agent `push_log` yuritadi (external_id, hash, vaqt), 200'ni qayta yubormaydi, 404 bo'lsa — kalit noto'g'ri, to'xtab egaga xabar beradi |
| Noma'lum JSON maydonlar izohga va «Forma savollari»ga tushadi | `kub` maydoni bir marta `quoted_volume_m3`ga bog'lanadi → «manba=agent, min_m3=3 → cargo menejerlar» oqim qoidasi ishlaydi |

Agent yuboradigan lid namunasi:

```json
POST /api/leads/in/agent        X-ERP-Key: <kalit>
{
  "external_id": "cs:<company_id>:<yyyymm>",
  "name": "Rustam Karimov — Textile Trade MChJ",
  "phone": "+998901234567",
  "note": "Xitoydan mato va furnitura, oyiga ~1 konteyner. Dalil: <url> (2026-10-20). Taklif: 25 m³ uchun zichlik bo'yicha narx, foto-hisobot.",
  "kub": "25", "company": "Textile Trade MChJ", "stir": "30xxxxxxx",
  "telegram": "@username", "instagram": "…", "website": "…", "address": "Toshkent, …",
  "imports": "mato, furnitura", "score": "82", "evidence": "<url>", "utm_source": "clientscrapper"
}
```

Menejerlar lidni o'zlarining ERP'ga ulangan Telegram akkauntidan yozadi (`docs/TELEGRAM-CRM.md`) — agent **hech qachon** o'zi birinchi xabar yubormaydi.

### Tezkor tuzatish (loyihadan oldin, 10 daqiqa)

Sayt `lead-client.ts` da kod `GSR-XXXXXXXXXX` ko'rinishida yaratadi, ERP esa (`site-assign-rules.ts`, migratsiya `0110_lead_assign.sql`)
faqat `LEAD-…` kodni qabul qiladi. Natija: `/api/lead/assign` har doim `null` qaytaradi, mijoz kompaniya akkauntiga tushadi, lid avtomatik
ochilmaydi. Bir tomonda bitta konstantani o'zgartirish kifoya (saytda `CODE_PREFIX` → `LEAD-`, yoki ERP'da regex + CHECK + listener).
Agar ishlab turgan gsrwms.uz bu repodan farq qilsa — bitta `curl` bilan tekshiring (har javob real yuklama sanaladi, shuning uchun faqat bir marta).

## 3. Manbalar: qayerdan topiladi (tekshirilgan reyting)

Usul: 10 yo'nalishda 14 ta qidiruv agenti 150 ta nomzod manba topdi (111 noyob). Eng muhim 45 tasi alohida
«shubhali» agentlar tomonidan qayta tekshirildi (mavjudligi, 2025–2026 holati, aynan nima berishi, narxi, huquqiy xavfi).
Natija: **37 tasdiqlangan, 7 ehtimoliy, 1 rad etilgan**; qolgan 50 tasi faqat birinchi agent da'vosi (quyida «tekshirilmagan»).
Barcha dalil URL'lari `docs/manbalar.json` da (bu hujjat bilan birga).

«Mos»lik bahosi (0–10) — ≥3 m³ B2B importyorlarni topishga qanchalik yordam berishi.

### 3.1 Birinchi darajali manbalar (loyiha shular ustiga quriladi)

| # | Manba oilasi | Mos | Nima beradi | Narx / kirish | Huquqiy xavf | Izoh (tekshiruvdan) |
|---|---|---|---|---|---|---|
| 1 | **O'zingizning ma'lumotlaringiz** — LogisticsERP `receipts`/`receipt_lots` (m³, kg, karobka), `clients`, `unclaimed_marking`; 400+ mijoz | 8 | Haqiqiy o'lchangan m³ bo'yicha mijoz profili → «o'xshash» (lookalike) qidiruv uchun etalon; kodsiz kelgan yuklar (markirovka + telefon) → tayyor lidlar; konsolidatsiya qiluvchi agentlar → referal dasturi | 0 | past | `clients` jadvalida STIR/Telegram ustuni yo'q — CRM boyitishi kerak. Bu loyihaning **kalibrlash to'plami**: skoring shu 400 mijozga qarab sozlanadi |
| 2 | **Ish e'lonlari** — hh.uz rasmiy API (`api.hh.ru/vacancies`) | 7 | «менеджер ВЭД», «закупщик Китай», «со знанием китайского», «tashqi savdo» e'lonlari → kompaniya nomi, manzil, sayt. Bir qidiruvdayoq: Chilon Lubricants, Asiya Iron Steel, BRESSO, ALPHAMEDICA, TYRE ZONE, MERAS TRADE | bepul (API; User-Agent kerak, 403 bo'lsa dev.hh.ru token) | o'rta (ish beruvchi — kompaniya; HR telefoni — shaxsiy) | Har 4 soatda so'rov; area ID ni `/areas` dan aniqlash; OLX ish, Telegram ish kanallari — mayda segment uchun |
| 3 | **Marketplace sotuvchilari** — Uzum katalog (buyurtmalar soni ochiq), OLX biznes akkauntlar, Top.uz/Glotr/Flagma/Stroyka.uz | 6 | Hajmli kategoriyalarda (mebel, maishiy texnika, o'yinchoq, santexnika, avtoehtiyot) buyurtma tezligi → oylik m³ bahosi; Uzum oferta bo'yicha do'kon sahifasida yuridik nom + ro'yxat raqami majburiy | Apify Uzum: $3.30 / 1 000 tovar; parse.bot seller_info; OLX: telefon ko'rsatish autentifikatsiya talab qiladi | o'rta (ToS) | Buyurtma→m³ koeffitsiyenti o'z Uzum-mijozlaringizda kalibrlanadi |
| 4 | **Bojxona/TIF ma'lumot brokerlari** — vedrating.ru, ImportGenius, Tendata, Volza, Trademo (bepul sahifalar), TradeInt | 5–6 | Importyor nomi (ba'zida STIR), HS kod, og'irlik, qiymat, yetkazib beruvchi, sana. vedrating: 2024 → 59 062 TIF ishtirokchisi / 55 376 importyor; Trademo bepul sahifalari: «OOO Uzbegim Les — 3 131 import, $24.68M» kabi | vedrating: tanlama 7 500 RUB dan, baza — so'rov bo'yicha; ImportGenius: davlat qo'shimchasi $99–399/oy; Volza: $1 500/yil dan; Tendata: so'rov | **o'rta-yuqori** (deklaratsiya — tijorat siri; provenans noma'lum) | Hammasi bitta kulrang manbani sotadi. Qoida: eng yangi oyi 2026 bo'lsin; 20 mijozingiz STIR'i bilan namunani tekshiring; faqat ichki skoring, mijozga «sizning deklaratsiyangiz»ni hech qachon aytmaslik |
| 5 | **Tarmoq va sanoat ro'yxatlari** — uzts.uz (to'qimachilik, STIR+telefon), Iteca ko'rgazmalari (UzBuild, Power, TextileExpo, CAITME, MebelExpo — Bitrix sahifalar), Automechanika (BMCA), openinfo.uz (AJ yirik bitimlari, 6-tur faktlar), uzpharm-control/uzpharm-gxp | 5–6 | Sektor bo'yicha tayyor kompaniya ro'yxatlari; ko'rgazma ishtirokchisi = import qiluvchi distribyutor; openinfo — «uskuna xaridi» shartnomalari kontragent nomi bilan | bepul | past | uzts 500-qator cheklovi `?pagenum=` bilan aylanib o'tiladi; ro'yxatlar eski (test qatorlar bor) — boyitish shart |
| 6 | **Telegram** — Telethon passiv o'quvchi + TGStat Search API; @Optom_bozor, @optomelonuz, @mk_express, @fx_express, Uzum sotuvchilar jamoasi (@MarketplaceUzum, 35–39k) | 5–6 | «X kub», «konteyner kerak», «kim olib keladi» kabi niyat signallari real vaqtda; kanal tavsifidagi biznes kontaktlar | bepul + TGStat Search API S-tarif 4 200 RUB/oy (50 kalit so'z); Premium akkaunt | o'rta (ToS) | Akkaunt 500 (Premium 1 000) chatgacha; a'zolar ro'yxatini yig'ish — ban yo'li, **xabarlar** bo'yicha ishlash kerak; o'quvchi akkaunt hech qachon yozmaydi |
| 7 | **Kompaniya kataloglari va boyitish** — myorg.uz (~563k yuridik shaxs, telefon/manzil, API), 2GIS rubrikalari («Стройматериалы оптом» …, Apify $1.99/1k dan), yellowpages.uz `/rubrika/`, goldenpages.uz (raqamli ID), orginfo.uz, stat.uz EGRPO (STIR bo'yicha, captcha, PDF), my.soliq.uz QQS reestri, UzTender `supplier/{INN}` | 4–6 | Telefon, manzil, OKED, QQS holati, ta'sischilar, davlat xaridlaridagi faollik — ya'ni **kontakt va saralash qatlami** | bepul / myorg API — so'rov bo'yicha | past–o'rta | Reestrlar kontakt bermaydi, kataloglar STIR bermaydi — ikkalasini rapidfuzz bilan birlashtirish kerak. QQS to'lovchisi = B2B filtri |

### 3.2 Inbound (rozilik asosida) kanallar — scraping'ga muqobil

| Kanal | Holat | Qiymat |
|---|---|---|
| Telegram Ads resellerlar orqali (Telegrad — 900 000 so'mdan, Click/Payme; AdHand, e-Profit) — ulgurji/import kanallariga kanal-darajali targeting, `t.me/<bot>?start=ad_telegram` deep-link LogisticsERP allaqachon qabul qiladi | tasdiqlangan | Rozilik birinchi xabarda olinadi; 2026-11-01 qoidasiga to'liq mos |
| Ko'rgazmalar: TransLogistica (10–12 noyabr 2026), UzBuild (fevral), MebelExpo (aprel), Power+TextileExpo (may), CAITME (sentabr), Automechanika (oktabr), CCPIT China–Central Asia (oktabr 2027) | tasdiqlangan | Tashrif buyuruvchi ro'yxati faqat tashkilotchi hamkorligi orqali |
| Referal dasturi: 1688-agentlar, Ivu/Guanchjoudagi o'zbek vositachilar, bozor do'konlari — har agent uchun alohida `/api/leads/in/<manba>` kaliti | taklif | BILLZ namunasi: sotuvning 20% 12 oy |

### 3.3 Rad etilgan yoki zaif deb topilganlar

- **birdarcha.uz** — bu ERI bilan kiriladigan ro'yxatdan o'tkazish portali, qidiruv reestri emas (rad etildi).
- **Panjiva/S&P, Descartes Datamyne** — O'zbekiston qamrovi yo'q. **Apollo, Lusha, Kaspr, RocketReach** — O'zbekiston telefonlari yo'q. **LinkedIn** — avtomatlashtirish taqiqlangan, UZ auditoriyasi yupqa.
- **Xitoy tomoni** — GACC faqat agregat; «海关数据» sotuvchilari aslida O'zbekiston tomonidagi ma'lumotni sotadi, Xitoy DSL/PIPL xavfi; Alibaba RFQ — faqat o'z supplier-akkaunt bilan, qo'lda. Yagona foydali narsa — o'z Ivu/Guanchjou punktlaringiz ma'lumoti (№1).
- **xarid.uzex.uz** — lot kartasida g'olib STIR ochiqligi tasdiqlanmadi; 2026-yilda xaridlar xt-xarid.uz ga ham bo'lingan; UzTender orqali olish osonroq.
- **O'zstandart muvofiqlik sertifikatlari reestri** (sert2.standart.uz) — nazariy jihatdan eng kuchli «bu kompaniya importyor» signali (ariza beruvchi = importyor, har sertifikat ortida real yuk), lekin sayt qidiruvda indekslanmagan — **brauzerda qo'lda tekshirish kerak**; ishlasa, 1-darajaga ko'tariladi.
- **Rasmiy ochiq ma'lumot** (customs.uz, stat.uz, data.egov.uz ~13 000 dataset) — kompaniya nomi yo'q; faqat qaysi tovar guruhida Xitoy ulushi katta (2025: import $47.4 mlrd, Xitoy $14.8 mlrd; mashina-uskuna importning 34%).

### 3.4 Tekshirilmagan, lekin arzon sinab ko'rish mumkin bo'lganlar

Statsnet API (UZ reestr + aloqalar), Kontur.Focus «Uzbekistan», INFOLine «130 yirik chakana tarmoq», Uzum seller-analitika botlari (ZoomSelling, MPSTATS), Sellers Association tadbirlari,
chamber.uz a'zolar, uzeltech.uz, ish.mehnat.uz (ish beruvchi STIR'i bilan), Telega.in, Telemetr.me, 52wmb, Yandex Market UZ.

## 4. Huquqiy chegaralar (40 ta qidiruv bilan tekshirilgan memo xulosasi; yurist maslahati emas)

| Hujjat | Agent uchun amaliy qoida |
|---|---|
| «Shaxsga doir ma'lumotlar to'g'risida» ZRU-547 (2019; ZRU-1125 bilan 2026-03-27 dan o'zgargan) | **Yuridik shaxs** ma'lumoti (nom, STIR, manzil, OKED, kompaniya telefoni) shaxsiy ma'lumot emas — erkin yig'iladi. **Direktor ismi, shaxsiy mobil, Telegram/Instagram akkaunti** — shaxsiy ma'lumot: asos (ommaviy manba / rozilik), maqsad, muddat, o'chirish huquqi kerak. Ommaviy manbadan olingan ma'lumot uchun alohida bob bor (6-bob), lekin umumiy qoidalar baribir amal qiladi |
| Bazalarni ro'yxatga olish (VM 71-son Nizom, 2020) | pd.gov.uz orqali, bepul; Imex services LLC nomidan «B2B lid bazasi» ni ro'yxatdan o'tkazish. 2026-11-01 dan reklama qo'ng'iroq/SMS tizimlari uchun majburiy |
| Lokalizatsiya (27-1-modda, VM 415-son qarori, 2026-08-03) | Endi faqat biometrik/genetik (va aloqa abonent) ma'lumotlari uchun; 49 ta «tenglik» davlati ro'yxati (AQSh, Rossiya, EI bor; **Xitoy yo'q**). Postgres'ni O'zbekistonda yoki shu ro'yxatdagi davlatda saqlash mumkin; Claude API (AQSh) — ruxsat etilgan, lekin xabarni **pseudonimlashtirib** yuborish (telefon/username → token) |
| Javobgarlik: MJtK 46-2, JK 141-2, ZRU-1115 (2026-01, sun'iy intellekt) | Noqonuniy yig'ish/ishlov: 7 BRV (fuqaro) / 50 BRV (mansabdor). **AI yordamida** noqonuniy ishlov yoki internet orqali tarqatish: 50–100 BRV + vositalarni musodara. Bir yil ichida takror — jinoiy (100–150 BRV, 3 yilgacha huquqdan mahrum). Agent Claude ishlatgani uchun bu band to'g'ridan-to'g'ri tegishli — ishlov **qonuniy** bo'lsa, qo'llanilmaydi |
| «Reklama to'g'risida» ZRU-776 (2022), 31-modda | Telekom tarmoqlari orqali reklama — faqat abonentning **oldindan roziligi** bilan (Internet/Telegram ham shu ostida deb talqin qilinadi) |
| PQ-265 (2026-07-15), **2026-11-01 dan kuchda** | Reklama SMS va qo'ng'iroqlar — faqat yozma/elektron rozilik bilan; **18:00–09:00 taqiq**; operatorlar so'rov bo'yicha bloklaydi; yuboruvchi tizim davlat reestrida bo'lishi shart. Xulosa: sovuq SMS — yo'q; sovuq qo'ng'iroq — faqat kompaniya raqamiga, ish vaqtida, yurist tasdig'i bilan |
| Telegram ToS / API Terms | Spam va scraping taqiq; a'zolar ro'yxatini yig'ib DM yuborish — akkaunt bloki (PEER_FLOOD → doimiy). O'quvchi va yuboruvchi akkauntlar alohida; birinchi xabar — faqat menejerning o'z akkauntidan, qo'lda, kunlik limit bilan |
| Bojxona kodeksi, «Tijorat siri» ZRU-374 | Deklaratsiya mazmuni — TIF ishtirokchisining maxfiy ma'lumoti. Sotib olingan baza — faqat ichki skoring; mijozga uning «40 tonna olib kelgani»ni hech qachon aytmaslik; xom qatorlarni saqlamaslik, faqat hosila (band, STIR, kategoriya) |
| Xitoy PIPL/DSL | Xitoy fuqarolari (yetkazib beruvchilar, agentlar) kontaktini Toshkentga eksport qilish — alohida rozilik; agentni faqat O'zbekiston importyorlari bilan cheklash |

**Bir xabar bilan rozilik olish namunasi (birinchi xabar reklama emas, so'rov):**
«Assalomu alaykum, {ism}. Men {menejer}, GSR Logistics (Imex services LLC, Toshkent). Kontaktingizni {manba} orqali topdik. Kompaniyangiz Xitoydan yuk olib keladi deb o'ylaymiz — mos bo'lsa, tariflarimizni yuborishga ruxsat berasizmi? Xabar olishni istamasangiz, «STOP» deb yozing — boshqa yozmaymiz.»

**Majburiy texnik qoidalar (kod darajasida):** har lidda `legal_basis`, `source_url`, `collected_at`, `consent_status`, `consent_evidence`, `do_not_contact`;
saqlanadi: kompaniya maydonlari + bitta qaror qabul qiluvchi (ism, lavozim, biznes telefon/Telegram); saqlanmaydi: pasport/JShShIR, tug'ilgan sana, a'zolar ro'yxati, to'liq chat tarixi, deklaratsiya qatorlari;
muddat: niyat-signal 30 kun, javobsiz lid 90 kun → anonimlashtirish; rad etganlar — faqat xesh (abadiy bostirish ro'yxati); «STOP/yo'q/нет» 24 soat ichida butun tizimda (agent + MIYA + ERP).
Yuristga 5 savol memoda (`docs/huquqiy-memo.md`).

## 5. Sotuv jarayoni: lid menejerga qanday beriladi va menejer nima qiladi

### 5.1 Segmentlar va taklif (saytdagi real tariflar asosida; «bojsiz», «eng tez», «100% kafolat» — taqiq)

| Segment | Qayerdan topiladi | Og'riq (manbalar bilan) | GSR taklifi | Saralash savoli |
|---|---|---|---|---|
| A. Bozor ulgurjichilari (Abu Saxiy, Chorsu, Urikzor, Qo'yliq, viloyat bozorlari) | OLX «optom», Golden/Yellow Pages, 2GIS, Telegram optom kanallar | Zichlik «qayta o'lchanadi», yashirin to'lovlar, konsolidatsiyada yo'qolgan yuk, oldindan to'lov firibgarligi, Xorgosda 3–5 hafta | Ivu/Guanchjouda o'z qabul punkti, o'lchov + foto-hisobot, yozma m³ tarifi (110–320 $/m³, formula ochiq), 15–25 kun | «Oyiga necha kub? Ivu yoki Guanchjou?» |
| B. Marketplace sotuvchilari (Uzum, Olcha, Instagram do'konlar) | Uzum katalog → yuridik nom; Uzum sotuvchilar guruhlari; Instagram biznes profillar | Kartochka uchun sertifikat/hujjat talabi, QC xatolari → qaytarish, Xitoydan yetkazish hajm oshgach buziladi | 1688/Taobao xarid (3%), QC + foto, GTD/invoys buxgalteriya va sertifikatga mos, 15–25 kun yoki avia | «Keyingi partiya qachon, necha kub/kg? FBO?» |
| C. Zavodlar (uskuna, xomashyo) | uzts.uz, openinfo.uz, uzpharm, UzTender, hh.uz «снабженец/ВЭД» | Bojxona qiymati bahsi, TN VED xatosi, VM-352 imtiyozini o'tkazib yuborish, konteyner muddati 20–35 kun vs ishlab chiqarish rejasi | 20/40 ft konteyner, TN VED ni VM-352/482 ro'yxatiga tekshirish (qaror bojxonaniki), GTD, zavodda QC | «Qanday uskuna, qachon, 20 yoki 40 fut?» |
| D. Qurilish materiallari va mebel importyorlari | Stroyka.uz, Glotr/Flagma, 2GIS «стройматериалы оптом», UzBuild/MebelExpo ishtirokchilari | Zich yuk m³ bo'yicha noto'g'ri narxlanadi, mavsumiylik (sentabr–noyabr, XYY oldi), sinish, Gisht-Ko'prik yopiq (2026 Q4 gacha) | Zich yuk uchun to'liq fura/konteyner hisobi yonma-yon, mavsum uchun bron kalendari, qadoqlash/QC foto | «To'liq fura/konteynermi yoki yig'ma? Oyiga nechta?» |
| E. Elektronika, avtoehtiyot, maishiy texnika distribyutorlari | hh.uz, Automechanika, Volza/Trademo bepul sahifalar, yellowpages | Brend hujjatlari, sertifikat, batareyali yuk (avia emas) | Avto kargo (litiy batareya ruxsat), GTD, foto-hisobot | «Oyiga necha kub, qaysi brendlar?» |

### 5.2 Birinchi aloqa qoidalari

- Kanal tartibi: 1) menejerning **o'z** Telegram akkauntidan DM (ERP'ga ulangan — yozishma CRM'da qoladi); 2) qo'ng'iroq faqat **kompaniya** raqamiga, 10:00–17:00; 3) Instagram DM — Telegram bo'lmasa; 4) tashrif — faqat javobdan keyin.
- Vaqt: Du–Sha 10:00–17:00; yakshanba, 18:00 dan keyin va 09:00 dan oldin — hech qachon (2026-11-01 qoidasi Telegram'ga ham qo'llanadi).
- Kim yozadi: ismi bor odam, o'z akkauntidan; bot yo'q, umumiy «GSR Sales» akkaunt yo'q, avtomatik birinchi xabar yo'q. MIYA'ning tasdiqlangan yuboruvchisi — faqat **tasdiqlangan follow-up** uchun (kuniga ≤20 yangi kontakt/akkaunt, ≥3 daqiqa oraliq, tinch soatlar, PEER_FLOOD bo'lsa to'xtash).
- Birinchi xabarda **bo'ladi**: ism, kim ekanligi, bitta **ommaviy** sabab (OLX e'loni, Uzum do'koni, ko'rgazma ro'yxati), 1–2 fakt (o'z punktlar, foto-hisobot, m³ zichlik bo'yicha, 15–25 kun), bitta savol, rad etish qatori. 50–90 so'z.
- Birinchi xabarda **bo'lmaydi**: narx (zichlik ma'lum bo'lmaguncha), «bojsiz/eng tez/100%», uning deklaratsiyasi/STIR/soliq holati haqida gap, havola/rasm/PDF, raqobatchi nomi, chegirma, «faqat bugun».
- Rad etish qatori (majburiy, oxirgi gap): «Agar qiziq bo'lmasa, "yo'q" deb yozing — boshqa yozmayman.» / «Если неактуально — напишите «нет», больше не побеспокою.» Har «yo'q» → `do_not_contact` o'sha kuni.
- Kadens: 0-kun xabar → 3-kun bir gapli eslatma → 7-kun foydali fakt (Gisht-Ko'prik ochilishi, 2026-10-01 dan bojxona yig'imlari −30%, XYY yopilish kalendari) → 14-kun yopish → 6 oy tinch.

### 5.3 Tayyor skriptlar (segment A, bozor ulgurjichisi)

```
Assalomu alaykum, [Ism] aka. Men [Men], GSR Logistics (Toshkent), 2018-yildan Xitoydan yuk olib kelamiz.
[manba]da [tovar] optom savdongizni ko'rib yozyapman. Yukni Yiwu va Guangzhoudagi o'z punktlarimizda qabul
qilib o'lchaymiz, fotohisobot yuboramiz, narx zichlik bo'yicha kubga yoziladi, Toshkentgacha 15–25 kun.
Oyiga taxminan necha kub olasiz? Agar qiziq bo'lmasa, "yo'q" deb yozing — boshqa yozmayman.
```
```
Здравствуйте, [Имя]. Я [Я], GSR Logistics (Ташкент), с 2018 года возим грузы из Китая. Пишу, потому что
увидел ваш опт по [товар] на [источник]. Принимаем груз на своих пунктах в Иу и Гуанчжоу, замеряем,
присылаем фотоотчёт, тариф за м³ по плотности фиксируем письменно, до Ташкента 15–25 дней.
Сколько кубов в месяц обычно везёте? Если неактуально — напишите «нет», больше не побеспокою.
```
Segment B (marketplace) va C (zavod) uchun UZ-lotin/UZ-kirill/RU variantlari, 60 soniyalik qo'ng'iroq skripti va 8 ta e'tirozga javob — `docs/sotuv-playbook.md` da.

### 5.4 Menejer ko'radigan lid kartasi va qaytarishi shart bo'lgan 5 maydon

```
GSR AGENT LEAD | Ball 78 | Segment A — bozor optom | Til: UZ-lotin
Kompaniya: "Baraka Tekstil" MChJ (STIR 30xxxxxxx, Toshkent, Yakkasaroy)
Qaror qabul qiluvchi: Sardor A. — egasi (manba: OLX biznes profil)
Tel: +998 9x xxx xx xx (biznes) | Telegram: @... | Instagram: @...
Tovar: bolalar kiyimi optom, Ivu | Taxminiy hajm: 5–10 m³/oy (yig'ma)
Dalil (ommaviy): OLX e'lon 2026-10-20; Golden Pages karta; TG post 2026-10-18
Tavsiya: Telegram DM, skript A, 10:00–17:00 | DNC: yo'q (2026-10-22) | STIR va bojxona ma'lumotini aytmang
[📞 Bog'landim] [Keyinroq] [Noto'g'ri lid]
```

| Maydon | Qiymatlar | Nimaga kerak |
|---|---|---|
| 1. Natija | javob yo'q / qiziq emas / qiziq / **malakali** (≥3 m³ yoki fura/konteyner tasdiqlandi) / taklif yuborildi / birinchi yuk / noto'g'ri kontakt / B2C / raqobatchi / yopilgan | Skoring modelini o'rgatadi; «noto'g'ri kontakt» manbani jazolaydi |
| 2. Hajm | m³/partiya, m³/oy, tur (yig'ma/fura/20ft/40ft/avia), shahar (Ivu/Guanchjou/Qashqar) | Manba bo'yicha hajm bahosini kalibrlaydi |
| 3. Hozirgi kargo | nomi yoki «o'zi», qoniqish 1–5, nimani o'zgartirardi (narx/muddat/yo'qotish/bojxona/hujjat) | Raqobatchi xaritasi, segment og'riqlari |
| 4. Keyingi qadam | DM / qo'ng'iroq / taklif / tashrif / kutish + sana | SLA |
| 5. Bog'lanmaslik | ha/yo'q + sabab (so'radi / shikoyat / noto'g'ri odam / jismoniy shaxs / davlat / huquqiy xavf) | Telefon, username, STIR bo'yicha abadiy bostirish |

Metrikalar (haftalik, menejer × manba × segment): yuborilgan lidlar, 1 ish kunida tegilganlar %, javob %, malakali %, taklif %, birinchi yuk %, 1 malakali lid narxi; gigiyena: DNC soni, shikoyat/blok soni (biri bo'lsa — akkaunt to'xtaydi), xato kontaktlar %.
Dushanba 10:00, 45 daqiqa — manba va skript bo'yicha ko'rib chiqish, eng yomon manbani o'chirish, eng yaxshi skriptni kengaytirish.

## 6. Arxitektura va reja (to'liq: `docs/arxitektura.md`, kod auditi: `docs/kod-audit-miya.md`)

### 6.1 Oldindan qabul qilingan qarorlar

1. **MIYA'dan nusxa olinadi, import qilinmaydi.** config/db/logs/retry/extraction/usage/costs/health/backup/sheets/userbot/sender/outbox — deyarli o'zgarishsiz; `people.py` → `companies.py`. Python 3.12, 4 ta jarayon (`api`, `bot`, `worker`, `userbot`) + PostgreSQL 16, bitta VPS. MIYA ning xavfsizlik testlari (Telegram'ga yozish faqat sender funksiyalarida, `claim_ready` orqali, kill-switch) o'zgarishsiz ko'chiriladi.
2. **Claude hech qachon ball qo'ymaydi va hech qachon yubormaydi.** Claude faqat tipli belgilar chiqaradi (nima olib keladi, hajm + dalil iqtibosi, yozuvchining roli); ball — deterministik Python funksiyasi, har ballning tarkibi saqlanadi.
3. **Telefon — lidning «valyutasi».** Telefonsiz nomzod ERP'ga yuborilmaydi: boyitiladi yoki Telegram yo'li bilan ishlanadi.
4. **ERP'ga ulanish = bitta yangi `agent` manba kaliti** (bitta konstanta + bitta `lead_sources` qatori + bitta kalit). Bo'lmaguncha `other` + `utm_source=clientscrapper`.
5. **Teskari aloqa = ERP'ni o'qish**, menejerdan yangi ish so'ramaslik: `lead_intakes.outcome/contacted_at`, `leads.stage → kind`, `lost_reason`, `client_id`, `quoted_volume_m3` allaqachon hamma kerakli narsani yozadi. Faqat 4 ta standart yo'qotish sababi qo'shiladi: «Import qilmaydi», «Hajm kichik (<3 m³)», «Raqam noto'g'ri», «Kargo/konkurent».
6. **MVP manbalari: (a) ERP'ning o'z ma'lumoti, (b) Telegram guruhlari niyat-monitori, (c) hh.uz vakansiya monitori** + bir martalik ko'rgazma ro'yxatlari (Automechanika 21–23 oktabr 2026 — kelasi hafta yig'ish). Pullik bojxona ma'lumoti — faqat namuna testdan o'tgach (2-oy).

### 6.2 Quvur (pipeline)

```
kollektorlar → kuzatuvlar (xom, manba bo'yicha) → normalizator → rezolver (kompaniya/kontakt/taxallus)
→ boyitish (arzon → reestr/katalog → Claude xulosa) → ball → darvozalar/bostirish
→ lead_candidates(review) → egasi boti (MVP) / avtomatik → crm_push (POST /api/leads/in/agent) → push_log
→ tungi feedback o'quvchi (ERP read-only rol) → natijalar → vaznlar/bostirish/manba aniqligi
```

**Qattiq darvozalar** (ballgacha): `is_b2b` (huquqiy shakl yoki STIR yoki ≥2 hajm signali), `not_competitor` (kargo/ekspeditor so'zlari), `not_supplier` (Xitoy tomoni), `not_client` (400 mijoz — telefon/STIR/GS kod), `not_dnc`, `not_recent` (30 kun ichida yuborilgan), `active_entity`.

**Ball (0–100):** hajm 40 gacha (o'zi «konteyner/fura/20–40 ft» degan — 40; «3–10 kub» — 25; bojxona ma'lumotida ≥10 t yuk — 40; o'z omborida ≥3 m³ qabul — 40; marketplace tezligi — 20; VED vakansiyasi — 15; ko'rgazma distribyutori — 15), Xitoy manbai 20 gacha, kompaniya mustahkamligi 15, yangilik 10, bog'lanish imkoni 10, 2+ mustaqil manba tasdig'i 5. Salbiy: B2C so'zlar −40, mayda qayta sotuvchi −30, davlat tashkiloti −20, Xitoy bo'lmagan manba −20, tugatilgan −50, oldin yo'qotilgan −30/doimiy.

| Ball | Telefon | Harakat |
|---|---|---|
| ≥70 | bor | Egasi botida karta → tasdiq → ERP'ga push (2-oydan: 30 kunlik aniqligi ≥60% manbalar uchun avtomatik) |
| ≥70 | yo'q | Telegram yo'li (quyida) yoki 14 kun ichida 3 marta boyitish |
| 45–69 | — | boyitish navbati; faqat `/qidir` va haftalik Sheets'da |
| <45 | — | kutish; yangi signal kelsa qayta baholanadi; 365 kun signal bo'lmasa o'chiriladi |

### 6.3 Telegram niyat-signal yo'li (daqiqalar ichida lidga)

Alohida, eski, Premium o'qish akkaunti → 30–50 chat (`@Optom_bozor`, `@optomelonuz`, raqobatchi kanallarning muhokama guruhlari, «xaridorlar» guruhlari) → `events.NewMessage` → regex (`\d+\s*(kub|куб|m3)|konteyner|fura|kim olib keladi|kerak|ищу`), kargo-tout filtri → Haiku `ImportSignal` (2–4 s) → kerak bo'lsa Sonnet `RelevanceVerdict` → rezolver + web-qidiruv boyitish (telefon, STIR) → ball → egasi botida «Yangi signal» kartasi: **CRM'ga yubor** / **Qoralama** / **Keyinroq** / **Yo'q** / **DNC**. Jami < 2 daqiqa.

Telefonsiz (Telegram-only) nomzodlar bilan kim gaplashadi: **MVP — menejer o'z akkauntidan** (karta navbatchi menejerga bot orqali `t.me/<username>` havolasi va qoralama bilan boradi; javobda telefon chiqsa agent ERP'ga push qiladi). Agent akkauntidan DM — MVP'da yo'q; 2-oydan faqat **guruh ichida ochiq javob** (`draft` rejimi, kuniga 10 ta, har chatda 1 ta, egasi tasdig'i bilan).

### 6.4 Ma'lumotlar modeli (qisqa)

`sources`, `collector_runs`, `evidence` (har kuzatuvning xom tanasi gzip + ≤600 belgi iqtibos + URL + vaqt), `observations`, `companies`, `company_aliases`, `company_ids` (hh/2gis/uzum/olx/tg/instagram), `contacts`, `signals` (har signal dalil bilan), `scoring_weights` (versiyali), `lead_candidates` (holat mashinasi), `lead_reviews`, `push_log`, `outcomes`, `suppression` (client/dnc/legal/competitor/b2c/bounced), `drafts`; MIYA'dan: `interactions`, `chat_monitors`, `outbox_messages`, `heartbeats`, `usage_log`, `sheet_rows`, `model_jobs`.
Har lid kartasi va ERP izohi **doim** dalil URL + sana + iqtibosni ko'rsatadi («NEGA TOPILDI»). Xom dalil 180 kun, iqtibos — kompaniya umri davomida, B2C/istisno qilingan shaxslar ma'lumoti 30 kunda o'chiriladi.

### 6.5 Reja

| Bosqich | Nima | Muvaffaqiyat mezoni |
|---|---|---|
| **MVP, 10 ish kuni** (1 dasturchi + egasi kuniga 30 daqiqa) | 1–2-kun: skelet MIYA'dan, `0001` migratsiya, ERP tomonida `agent` kaliti/sekret/`kub`/2 ta oqim qoidasi/4 ta lost reason · 3-kun: ERP ichki ma'lumot (bostirish ro'yxati, kodsiz yuklar, uxlab qolgan mijozlar) · 4–5-kun: userbot (faqat o'qish) + Telegram kollektori + Haiku/Sonnet · 6-kun: rezolver + ball + bostirish · 7-kun: hh.uz · 8-kun: boyitish + Opus Batch · 9-kun: egasi boti (`/lidlar`, `/manbalar`, `/holat`, `/xarajat`, `/toxtat`) + `crm_push` (soxta server testlari) · 10-kun: ko'rgazmalar, feedback v1, digest, Sheets, bitta menejer bilan pilot | 2-hafta oxirida: ≥20 telefonli lid (ball ≥70); menejer ≥60% ni «haqiqiy importyor» deb belgilaydi; ≥40% 1 ish kunida bog'lanilgan; ≥3 taklif; Claude xarajati <$3/kun |
| **1-oy** | Uzum sotuvchilari, OLX biznes, kataloglar (2GIS 100 ta pilot, yellowpages rubrikalari, uzts to'liq), tenderlar; reestr avtomatlashtirish (stat.uz moduli yoki myorg API); ERP read-only rol + avtomatik feedback; manba aniqligi; isbotlangan manbalar uchun auto-push; **bojxona ma'lumoti qabul testi** (TradeInt/ImportGenius/Tendata/vedrating bepul namunalari: 2025–2026 sanalar, kg+USD, CN filtri, 400 mijoz STIR bilan mos kelish) | Haftasiga 25+ malakali lid; aniqlik ≥65% |
| **2–3-oy** | Namuna o'tgan bo'lsa **bitta** bojxona ma'lumoti manbai; vaznlar v2 (logistik regressiya, ≥150 belgilangan nomzod); Telegram `draft` javoblar; Instagram biznes profillar; farmatsevtika/to'qimachilik reestrlari; menejer uchun `/qidir`; uxlab qolgan mijozlarni qayta faollashtirish | Haftasiga 40 malakali lid; aniqlik ≥75%; 1 malakali lid narxi <$12 |

Jamoa: 1 backend dasturchi (kodning ~60% ko'chiriladi) — MVP 10 kun, 1-oy +12 kun, 2–3-oy +15 kun; egasi 1–4-haftada kuniga 30 daqiqa ko'rib chiqish; 1 pilot menejer.

### 6.6 Oylik xarajat

| Modda | MVP | 3-oy |
|---|---|---|
| VPS 4 vCPU / 8 GB | $25–40 | $25–40 |
| Telegram Premium + SIM | $5 | $5 |
| Claude API (Haiku signal, Sonnet xulosa, Opus Batch tungi baholash, qoralamalar) | $25–60 | $60–150 |
| Web-qidiruv boyitish (≤150 so'rov/kun) | $20–45 | $30–60 |
| Apify (Uzum $3.30/1k, OLX $1.59/1k, 2GIS $1.99/1k) | 0 | $30–60 |
| Rezident proksi (faqat do'kon sahifalari) | 0 | $20–50 |
| Bojxona/TIF ma'lumoti | 0 | $100–400 |
| myorg API / reestr | 0 | $0–100 |
| **Jami** | **≈$75–150** | **≈$300–850** |

Kunlik Claude cheklovi `DAILY_COST_CAP_USD=5` — byudjet bu baho emas, cheklov.

### 6.7 KPI (haftalik, `/holat` va Sheets)

Malakali lid/hafta (MVP 10 → 3-oy 40) · aniqlik (≥60% → ≥75%) · 1 ish kunida bog'lanish (≥40% → ≥70%) · taklif ulushi (≥25% → ≥35%) · birinchi yuk (≥3% → ≥8%, 60 kun kechikish bilan) · birinchi aloqagacha median vaqt (<120 → <45 daqiqa) · 1 malakali lid narxi (<$5 → <$12) · Telegram signal → karta p90 (<3 → <2 daqiqa).

### 6.8 Asosiy xavflar

| Xavf | Chora |
|---|---|
| Telegram o'qish akkaunti bloklanishi | eski alohida akkaunt, faqat o'qish, kuniga ≤3 chat, FloodWait breaker, zaxira akkaunt, menejer akkauntlari agentga hech qachon berilmaydi |
| ERP cheklovlari (3/telefon/kun, mijoz, 30 kun join) lidni jimgina yutib yuborishi | mahalliy bostirish + `push_log`, kunlik 25 ta, oylik `external_id`, `push_log` vs `lead_intakes` haftalik solishtirish |
| Noto'g'ri/g'azablangan kontaktlar | qattiq darvozalar, `shared_phone` belgisi, 2 marta xato → `bounced`, har «yozmang» → DNC, narx/va'da taqiqi (redlines), manbani halol aytish |
| Huquqiy (shaxsiy ma'lumot, sayt ToS, bojxona ma'lumoti provenansi) | faqat B2B kontakt, minimal maydonlar, har yozuvda provenans, robots.txt + past tezlik, hh rasmiy API, qayta nashr yo'q, saqlash muddatlari, shartnoma asosida pullik ma'lumot |
| Eskirgan ma'lumot (bojxona 10–13 oy orqada, kataloglar yillar) | yangilik vazni + so'nish, eski manba yangi tasdiqsiz 30 balldan oshmaydi, push oldidan reestr holati |
| Claude xayoliy hajm/kompaniya | dalil-iqtibos validatori, deterministik ball, kartada ball tarkibi, MVP'da egasi ko'rib chiqadi |
| Telegram'da real talab kam (asosan kargo reklamasi) | tout filtri, kanallar emas — muhokama guruhlari, takroriy yozuvchi mantiqi, parallel hh/ko'rgazma/ERP manbalari, haftalik chat tozalash |
| Sotuvchilar qabul qilmasligi | lid o'sha eshik va botdan keladi, izohda «nega/nima deyish», m³ bo'yicha to'g'ri jamoaga, ERP'ning 15 daqiqalik eslatmasi, lost reason bilan haftalik ko'rik, bitta motivlangan menejer bilan pilot |

## 7. Tanqid: nimani soddalashtirish kerak (mustaqil «to'liqlik tanqidchisi» agenti xulosasi, `docs/tanqid.md`)

Tanqidchi arxitekturani «40+ kollektor, reestrlarni to'liq sanab chiqish, Telegram o'quvchi + yuboruvchi, bot, Sheets, Batch API, o'rganuvchi sikl» ko'rinishida 2 dasturchi uchun 6–9 oylik loyiha deb baholadi va quyidagilarni taklif qildi. **Men bularga qo'shilaman va yakuniy tavsiyaga kiritdim:**

| Tanqid | Qaror |
|---|---|
| «Reestr-birinchi» (50k yuridik shaxsni OKED bo'yicha sanab chiqish) emas, **«signal-birinchi»**: o'z ma'lumot + inbound + 4–5 kuchli kollektor; STIR faqat ballga ega nomzodlar uchun, top-N qo'lda (captcha/OneID avtomatlashtirishni sindiradi va ToS'ni buzadi) | Qabul qilindi — 3-bo'limdagi 7-oila «boyitish qatlami» sifatida qoladi, sanab chiqish yo'q |
| Pilot uchun **5 ta manba**: (a) o'z ma'lumot (lookalike, kodsiz yuklar, yo'qotilgan/uxlagan lidlar), (b) bitta tekshirilgan bojxona namunasi yoki rasmiy pullik xizmat, (c) Uzum hajmli kategoriyalar, (d) ish e'lonlari (hh.uz + «xitoy tili tarjimoni»), (e) 30–60 Telegram guruhini faqat o'qish | Qabul qilindi — 6.5 jadvaldagi MVP shunga mos; qolgani aniqlik o'lchangach |
| **Yuboruvchini olib tashlash**: 2026-11-01 dan keyin va Telegram'ning 2026 shartlari ostida agent tomonidan sovuq DM/SMS/qo'ng'iroq — eng katta huquqiy va blok xavfi, eng past natija. Agent hech kimga yozmaydi; menejerlar ERP'ga ulangan o'z akkauntidan yozadi. Bu MIYA'dan ko'chiriladigan kodning ~40% ini (outbox/gate/sender) va huquqiy xavfning ko'p qismini olib tashlaydi | **Qabul qilindi** — 2-oydagi «guruh ichida javob» rejimi ham yurist tasdig'igacha muzlatiladi |
| «Avtonom, uzluksiz» emas — **haftalik partiya + egasi ko'rib chiqish stoli**: oyiga 10 fura va bir nechta menejer bilan haftasiga 20–50 tadan ortiq tekshirilgan lid ishlanmaydi | Qabul qilindi — Telegram niyat-signallari real vaqtda qoladi (ular tez eskiradi), qolgan manbalar haftalik |
| «Natijadan o'rganadi» = ML emas, **ERP bosqich/lost_reason ni manba bo'yicha haftalik qo'shish** → manba aniqligi jadvali | Qabul qilindi — logistik regressiya faqat ≥150 belgilangan nomzoddan keyin, ixtiyoriy |
| m³ bahosini soddalashtirish: `sharhlar × koeffitsiyent × hajm` formulasi o'rniga 3 ta chelak (hajmli kategoriya × faollik × huquqiy shakl × QQS), 400 mijozda kalibrlangan, 50 ta menejer qo'ng'irog'i bilan tekshirilgan | Qabul qilindi |
| Ikki qatlamli sxema **strukturaviy** majburlansin: kompaniya qatlamida shaxsiy telefon/username ustuni yo'q; shaxs qatlami faqat rozilik hodisasidan keyin yoziladi; LLM'ga faqat kompaniya darajasidagi matn | Qabul qilindi |
| **Alohida Python repo kerakmi?** Yuboruvchi ketgach MIYA'dan qoladigan narsa — sanitariya kodi. LogisticsERP ichida modul (bir xil DB, bir xil Telegram steki, bir xil lid jadvallari, bitta deploy kam) 1–2 dasturchi uchun arzonroq bo'lishi mumkin | **Sizning qaroringiz** (8-savol). Mening tavsiyam: kollektorlar va Claude-boyitish — Python (scraping ekotizimi, MIYA testlari), lekin lid holati, ko'rib chiqish va menejer kartasi — ERP ichida; agent ERP'ga faqat push qiladi va read-only o'qiydi |
| `GSR-`/`LEAD-` nomuvofiqligi va `lead_assign_origins` sozlamasi — **birinchi** ish | Qabul qilindi |

Tanqidchi topgan, 3-bo'limda bo'lmagan **qo'shimcha manbalar** (tekshirilmagan, lekin mantiqli):

- **Yuk ekotizimi hamkorlari:** bojxona brokerlari (Bojxona brokerlari assotsiatsiyasi a'zolari va 2025 reytingi — atb.uz; singlewindow.uz reestrlari; logistika.uz katalogi), bojxona omborlari, **ATI.SU Xitoy→O'zbekiston yuk birjasi** (yuk beruvchi e'lonlari = to'liq fura niyati, telefon bilan, o'qish qonuniy), temir yo'l konteyner terminallari (Chuqursoy, Sergeli), aeroport yuk agentlari — bular scraping ob'ekti emas, **referal hamkorlar**.
- **Pullik qidiruv niyati:** Google Ads + Yandex Direct «карго из Китая Ташкент / Xitoydan yuk / konteyner Xitoy / 1688 optom» (Toshkentda CPC ≈$0.10–0.80; $200–300 test), saytdagi kalkulyatorga Telegram deep-link, Meta lead-form (ERP eshigi allaqachon bor), Search Console so'rovlari.
- **Xitoy brendlarining rasmiy dilerlari:** har brend O'zbekistondagi distribyutorini e'lon qiladi (Gree, Haier, Midea… — maishiy texnika, HVAC, yuk mashinalari, qurilish texnikasi, EV, telefon, asbob) — 200 brend ro'yxati = konteyner darajasidagi importyorlar, telefon bilan, qonuniy.
- **Xitoy ta'sischili O'zbekiston kompaniyalari** (1 800–5 000 ta, manbaga qarab) — EGRPO/myorg'da xorijiy ta'sischi bo'yicha filtr; O'zbekistondagi Xitoy savdo palatasi (2019).
- **Moliya/lizing/sug'urta:** banklarning savdo-moliya bo'limlari (har import shartnomasi EEISVO'da), lizing kompaniyalari (Xitoy uskunalarini moliyalashtiradi), yuk sug'urtachilari, VED-buxgalterlar jamoalari — hamkorlik kanallari.
- Boshqa: tovar belgilari reestri (ima.uz, xususiy brend importyorlari), yirik soliq to'lovchilar toifasi, ASL BELGISI ishtirokchilari, sud qarorlari (sud.uz), bozor ma'muriyatlari (do'kon ijarachilari ro'yxati), e'tibordan chetda qolgan segmentlar: avto/EV importyorlari, viloyat importyorlari (Farg'ona vodiysi, Samarqand), avia-elektronika.

## 8. Yakuniy tavsiya (nimadan boshlash)

1. **Bu hafta (kod yozilmasdan):** bojxona Excel'ini ochib ustunlarini tekshirish; Bojxona qo'mitasiga rasmiy so'rov; `GSR-`/`LEAD-` tuzatish + `lead_assign_origins`; pd.gov.uz ro'yxatdan o'tish; yuristga 5 savol; 400 mijoz eksporti (STIR, m³).
2. **MVP (10 ish kuni):** ERP o'z ma'lumoti → bostirish ro'yxati + kodsiz yuklar + uxlagan mijozlar; Telegram faqat-o'qish monitori (30 guruh); hh.uz monitori; Automechanika 2026 va UzBuild ro'yxatlari; egasi botida ko'rib chiqish; `POST /api/leads/in/other` (yoki `agent`) orqali push; manba bo'yicha aniqlik jadvali.
3. **O'lchov (2 hafta):** ≥20 lid, ≥60% «haqiqiy importyor», ≥40% 1 kunda bog'lanilgan — bo'lmasa manbani almashtiramiz, kengaytirmaymiz.
4. **Keyin, faqat o'lchov asosida:** Uzum/OLX/kataloglar, bojxona namunasi testi, brend-dilerlar ro'yxati, broker/lizing hamkorliklari, Telegram Ads va Google/Yandex test ($500).

## 9. Sizga savollar (javobi rejani eng ko'p o'zgartiradiganlar birinchi)

1. **Bojxona Excel'i.** `TelegramCustoms` ga yuklanadigan choraklik/oylik Excel'da «Получатель/Импортёр», «ИНН/СТИР», «Декларант» ustunlari bormi? Fayl qayerdan keladi (rasmiy xarid, broker, hamkor)? Firma kesimidagi versiyasini olish mumkinmi, narxi qancha?
2. **Pullik ma'lumot byudjeti.** Bojxona qo'mitasi rasmiy xizmati (yiliga 10–103 mln so'm) yoki broker (vedrating/ImportGenius/Tendata, taxminan $1 000–5 000/yil) uchun byudjet bormi? Kulrang provenansli bazani sotib olishga rozimisiz (faqat ichki skoring uchun)?
3. **Chegaraning aniq ta'rifi.** «≥3 m³» — bitta partiyadami yoki oyigami? YaTT (ИП) lar qamrovdami (ular ko'p bozor importyorlari, lekin shaxsiy ma'lumot rejimi)? Geografiya — faqat Toshkentmi yoki Farg'ona vodiysi, Samarqand, Buxoro ham?
4. **Ustuvor tovar guruhlari.** 400 mijozingizda qaysi kategoriyalar ko'p va m³ bo'yicha eng foydali (mebel/maishiy texnika/o'yinchoq — hajmli; elektronika — avia)? Bu reestr/sertifikat/marketplace filtrlarini belgilaydi.
5. **400 mijoz ma'lumoti.** STIR + oylik real m³ ro'yxatini (CRM/GTD tarixidan) skoringni kalibrlash va bostirish (suppression) uchun berasizmi? Ularning Telegram/telefonlari — qaysi chatlarda o'tirishini bilish uchun?
6. **Telegram akkauntlari.** Faqat o'qish uchun alohida, eski (6+ oy), Premium akkaunt (alohida SIM) ajrata olasizmi? Kodlarni kim kiritadi? Bloklanib qolsa almashtirishga rozimisiz?
7. **Aloqa siyosati.** Menejerlar faqat kompaniya raqamiga qo'ng'iroq va o'z akkauntidan shaxsiy DM (kuniga 15–20 ta) — shu bilan cheklanamizmi? Sovuq SMS/ommaviy xabar — butunlay rad etiladimi (tavsiya: ha)?
8. **Huquqiy egalik.** gsrwms.uz va yangi baza pd.gov.uz reestrida ro'yxatdanmi? Mas'ul shaxs kim? Yurist bormi (1-noyabr qoidasi, 18-modda asoslari, rozilik matnlari uchun)? Serverlar qayerda (O'zbekiston/EI/AQSh)?
9. **ERP o'zgarishlari.** LogisticsERP'ga `agent` manba kaliti, `kub` maydonini bog'lash, `agent + min_m3=3 → cargo` oqim qoidasi va lid natijalarini o'qish uchun kichik API qo'shishga rozimisiz? `GSR-`/`LEAD-` prefiks nomuvofiqligini qaysi tomonda tuzatamiz?
10. **Telefonsiz lidlar.** Telegram-username'i bor, telefoni yo'q nomzodlar menejerga alohida «Telegram niyat» navbati bo'lib borsinmi, yoki agent o'zi telefon topmaguncha ushlab tursinmi?
11. **Raqobatchilar va seed ro'yxat.** Siz kuzatadigan 10–20 ta kargo kanali/guruhi (MK Express, FX Express, Transasia, Transitpro dan tashqari) va mijozlaringiz o'tiradigan chatlar — bu haftalarni tejaydi.
12. **Referal va inbound.** Ivu/Guanchjoudagi agentlar, 1688-vositachilar va bozor do'konlari uchun komissiyali referal dasturi (m³ uchun yoki yuk haqining %) va Telegram Ads (oyiga 6–18 mln so'm) — qaysi biriga tayyorsiz?
13. **Natijani qaytarish.** Menejerlar har lidga 5 maydonni (natija, hajm, hozirgi kargo, keyingi qadam, bog'lanmaslik) to'ldirishga majbur qilinsinmi? Bu bo'lmasa agent o'rganmaydi.
14. **Qo'shimcha ICP.** Toshkentda ofis ochayotgan Xitoy kompaniyalari (Liepin/BOSS直聘 e'lonlari) — Xitoy omborlaringiz uchun ikkinchi mijoz turi sifatida kerakmi?

---

## Ilova: bu hujjat qanday tayyorlandi

Repolar o'qildi: `LogisticsERP` (lid eshigi, taqsimot, Telegram CRM), `gsrlogistics.uz` (tariflar, lid-assign), `dailyJournal` (MIYA), `TelegramCustoms`, `gsrerpv1`.
Tadqiqot: 10 yo'nalish × 14 qidiruv agenti → 150 nomzod manba (111 noyob); eng muhim 45 tasi alohida «rad etishga urinuvchi» agentlar bilan tekshirildi
(37 tasdiqlangan, 7 ehtimoliy, 1 rad etilgan); huquqiy memo (40 qidiruv), sotuv playbook (web tadqiqot bilan), arxitektura memo, mustaqil tanqid.
Cheklov: bu muhitda saytlarni to'g'ridan-to'g'ri ochib bo'lmadi (faqat qidiruv natijalari), shuning uchun «brauzerda tekshirish kerak» deb belgilangan joylar bor.
Barcha dalil URL'lari — `docs/manbalar.json`. Fayllar: `docs/arxitektura.md`, `docs/huquqiy-memo.md`, `docs/sotuv-playbook.md`, `docs/kod-audit-miya.md`, `docs/tanqid.md`.
