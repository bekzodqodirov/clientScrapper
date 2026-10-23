# Ochiq internetdan mijoz topish: Instagram, Facebook, OLX, Google, Telegram, marketplace va kataloglar

Sana: 2026-10-23. Bu hujjat `TAKLIF.md` ning 3-bo'limini kengaytiradi: **asosiy qidiruv ochiq internetdan**, bazalar (bojxona Excel, ERP) esa faqat
kalibrlash, bostirish (suppression) va tasdiqlash uchun.

## 1. Tamoyil: internet «ochiq baza» sifatida qanday ishlatiladi

Internetdagi har bir platforma uchta savolga javob berishi kerak: **kim** (do'kon/kompaniya nomi → yuridik nom, STIR), **nima va qancha** (tovar,
«optom/ulgurji», «konteyner/fura/kub», «zavod narxida», «Xitoydan to'g'ridan-to'g'ri», buyurtma soni, e'lonlar soni), **qanday bog'lanish**
(kompaniya telefoni, Telegram, manzil). Hech bir platforma uchalasini birdan bermaydi, shuning uchun agent **zanjir** quradi:

```
topish (Instagram / OLX / Telegram / Google / katalog)  →  yuridik nomni aniqlash (bio, sayt, Uzum do'kon sahifasi, 2GIS kartasi)
→ STIR va holat (stat.uz EGRPO, my.soliq.uz QQS, myorg.uz)  →  kontakt (katalog telefoni, bio'dagi Telegram, sayt)
→ ball (≥3 m³ belgilari)  →  ERP'ga lid («nega topildi» dalili bilan)
```

Qoidalar: faqat **ommaviy** sahifalar; shaxsiy akkauntlar emas, biznes profillar; a'zolar ro'yxati yig'ilmaydi; har yozuvda manba URL + sana + iqtibos;
platformalarning rasmiy API'si bor joyda — API, yo'q joyda — past tezlikdagi o'qish (1 so'rov / 2–5 soniya, robots.txt), bloklansa to'xtash.

## 2. Platformalar bo'yicha usul

### 2.1 Instagram — O'zbekistonda eng katta savdo vitrinasi

| Nima qidiriladi | Qanday | Qaysi belgilar B2B ≥3 m³ degani |
|---|---|---|
| Biznes profillar: bio'da «optom», «ulgurji», «оптом», «zavod narxida», «Xitoydan to'g'ridan-to'g'ri», «1688», «Guangzhou/Yiwu», bozor manzili (Abu Saxiy, Malika, Urikzor, Qo'yliq, Sergeli, Yangiobod) | (1) Google/qidiruv orqali kashf: `site:instagram.com optom Toshkent <tovar>`; (2) rasmiy **Instagram Graph API — Business Discovery**: username bo'yicha ommaviy biznes profil (bio, sayt, obunachilar, postlar soni) — GSR'ning o'z biznes akkaunti + Facebook app orqali; (3) **Hashtag Search API** (#optom, #ulgurji, #xitoydantovar, #оптомташкент; 7 kunda 30 ta noyob hashtag); (4) uchinchi tomon skraperlari (Apify, ~$1/1 000 profil) — faqat prototip, Meta ToS xavfi | bio'da optom/ulgurji + tovar kategoriyasi hajmli (mebel, maishiy texnika, o'yinchoq, santexnika, qurilish, avtoehtiyot, gazlama) + Telegram/telefon + «zavod/konteyner/partiya» so'zlari + 500+ post yoki 5k+ obunachi + alohida ombor/do'kon manzili |
| Highlights/postlarda «yangi konteyner keldi», «partiya», «Xitoydan yuk keldi» | caption'lar (API orqali faqat o'z/ommaviy biznes media; qidiruv snippetlari) | «konteyner keldi» = to'g'ridan-to'g'ri hajm dalili |

Instagram beradi: do'kon nomi, bio, Telegram/telefon (ko'pincha), manzil, tovar. Bermaydi: yuridik nom, STIR → zanjirning keyingi bosqichi.

### 2.2 Facebook — O'zbekistonda zaif, lekin ikki foydali joyi bor

- Guruhlar («Оптом Ташкент», «Xitoydan tovar») — ko'pi o'lik yoki spam; tekshiruv shuni ko'rsatdi. Faqat ommaviy sahifa telefoni/manzili bo'lgan biznes sahifalar olinadi.
- Rasmiy yo'l: **Meta Lead Ads** (ERP'da `meta/instagram/facebook` eshiklari allaqachon bor) — rozilik bilan inbound. Sahifalarni qidirish API'si ko'p ilovalar uchun yopilgan; sahifa skrapingi — ToS buzilishi, past hosil.

### 2.3 OLX.uz — ulgurji sotuvchilarning eng tez topiladigan joyi

| Nima | Qanday | B2B belgilar |
|---|---|---|
| «optom/оптом», «ulgurji», «zavod», «to'g'ridan-to'g'ri Xitoydan», «konteyner», «fura», «min. partiya» e'lonlari; «Бизнес» belgili akkauntlar; bitta sotuvchida 15–20+ e'lon bir hajmli kategoriyada | Kategoriya × kalit so'z bo'yicha kunlik «yangi» e'lonlar (ommaviy sahifalar va ichki JSON; Apify olx-uz-scraper ~$1.59/1k); sotuvchi profili `olx.uz/list/user/<id>` → e'lonlar soni, ro'yxatdan o'tgan yil, shahar | ko'p e'lon + optom + hajmli kategoriya + «konteyner/zavod» + 1+ yil + Toshkent/Samarqand/Farg'ona |

OLX beradi: e'lon matni, sotuvchi nomi, shahar, ko'pincha matn ichida Telegram. Bermaydi: telefon (ko'rish uchun login, cheklangan) → telefon katalog/bio orqali.

### 2.4 Google (va Yandex) — kompaniya saytlari, xaritalar, brend dilerlari

| Nima | Qanday | B2B belgilar |
|---|---|---|
| Kompaniya saytlari: «импорт из Китая» «оптом» ООО site:.uz; «Xitoydan import» «ulgurji» MChJ; «rasmiy distribyutor» + Xitoy brendi (Gree, Haier, Midea, Hisense, Chery, BYD, Sany, XCMG, Jinko, Huawei Solar…) | **Google Custom Search JSON API** (kuniga 100 bepul, keyin $5 / 1 000 so'rov) yoki SerpAPI/DataForSEO; har hafta kalit so'z matritsasi | sayt + «импорт/dilerlik/distribyutor» + tovar katalogi + ofis/ombor manzili + yuridik nom footer'da (STIR ko'pincha shu yerda) |
| Google Maps / Yandex Maps / 2GIS rubrikalari: «оптом стройматериалы Ташкент», «оптовая база», «optom bozor» | **Google Places API (New) Text Search** (pullik, faqat place_id saqlanadi), Yandex/2GIS Places API yoki 2GIS litsenziyali eksport; Apify 2GIS ~$1.99/1k | rubrika «оптом» + telefon + bozor/ombor manzili + ish vaqti |
| Brend dilerlari ro'yxati (≈200 Xitoy brendi × «O'zbekistondagi rasmiy distribyutor») | brendlarning o'z saytlari «where to buy / dilerlar» sahifalari + qidiruv | distribyutor = konteyner darajasidagi importyor, telefon bilan, 100% qonuniy |

### 2.5 Telegram — niyat signallari va ulgurji kanallar

- Kashf: `t.me/s/<kanal>` ommaviy ko'rinishlari (loginsiz), TGStat O'zbekiston katalogi (~203k kanal), kanal tavsifidagi biznes kontaktlar; kanallarga bog'langan **muhokama guruhlari** (`GetFullChannel.linked_chat_id`) — xaridorlar aynan shu yerda yozadi.
- Monitoring: alohida o'qish akkaunti, 30–60 guruh, regex `(\d+\s*(kub|куб|m3))|konteyner|fura|kim olib keladi|kerak|ищу` → Haiku tasnif → menejerga karta. Agent hech qachon yozmaydi.
- TGStat Search API (S-tarif 4 200 RUB/oy, 50 kalit so'z) — kanallar bo'ylab post qidiruvi, joinsiz.

### 2.6 Marketplace va B2B kataloglar

- **Uzum**: hajmli kategoriyalarda tovar kartalarida buyurtma soni ochiq → do'kon bo'yicha jamlash → do'kon sahifasida yuridik nom (oferta talabi) → STIR. Apify $3.30/1k tovar; do'kon sahifalari past tezlikda.
- **Glotr, Flagma, Top.uz, Stroyka.uz, Prom.uz**: «оптом из Китая», «завод Китай прямые поставки» e'lonlari — kompaniya nomi, telefon ko'pincha ochiq.
- **Olcha, Asaxiy, Sello**: sotuvchilar kam, ikkinchi daraja.

### 2.7 Kataloglar va bozorlar

yellowpages.uz `/rubrika/` (optovaya torgovlya, mebel optom, stroymaterialy optom, avtozapchasti optom…), goldenpages.uz rubrikalari (raqamli ID bo'yicha oson), 2GIS savdo markazlari («bino ichidagi firmalar»: Abu Saxiy, Malika, Urikzor, Qo'yliq, Sergeli), uzts.uz — telefon va manzil beradi, lekin «Xitoydan import» dalili bermaydi → faqat boshqa signal bilan birga ball oladi.

## 3. Kalit so'zlar matritsasi (UZ-lotin / UZ-kirill / RU)

| Tur | So'zlar |
|---|---|
| B2B markerlar | optom, ulgurji, оптом, ulgurji narx, zavod narxida, завод, прямые поставки, dilerlik, distribyutor, дистрибьютор, min. partiya, минимальный заказ, mijozlar bazasi |
| Hajm | kub, куб, m³, konteyner, kontayner, контейнер, 20 fut, 40 fut, fura, фура, tonna, partiya, партия, «konteyner keldi», «yuk keldi» |
| Xitoy manbai | Xitoydan, Хитойдан, из Китая, Китай, China, 1688, Taobao, Alibaba, Yiwu/Iwu/Иу, Guangzhou/Guanchjou/Гуанчжоу, Urumchi, Xorgos, Qashqar |
| Hajmli tovar kategoriyalari | mebel/мебель, maishiy texnika/бытовая техника, santexnika/сантехника, qurilish mollari/стройматериалы, kafel/плитка, LED/yoritgich/светильники, avto ehtiyot qismlar/автозапчасти, shinalar, gazlama/mato/ткани, kiyim/одежда, poyabzal/обувь, o'yinchoq/игрушки, sport, kanstovar, parfyumeriya, qadoqlash/упаковка, uskuna/оборудование, dastgoh/станок |
| B2C (salbiy) | o'zim uchun, для себя, 1 dona, bitta, shaxsiy, zakaz qilaman, Taobao zakaz |
| Raqobatchi (istisno) | kargo, карго, cargo, express, dostavka, yetkazib beramiz, ekspeditor, логист, bojxona broker |

## 4. Jonli sinov: 7 platforma × ~12 so'rov, bitta sessiyada nima topildi

Sinov faqat qidiruv tizimi snippetlari bilan o'tkazildi (bu muhitda sahifalarni ochib bo'lmaydi). Natija `docs/namuna-nomzodlar.md` va
`docs/ochiq-internet-namuna.json` da: **118 noyob nomzod**, shundan 70 tasi B2B segmentlarda (A–E), 18 ta raqobatchi-seed, 15 ta «crawl sahifasi»
(har birida o'nlab–yuzlab kompaniya telefoni bilan). Ishonchi ≥0.5 bo'lganlar: Soventus Energy (Jinko/Huawei distribyutori), Runking Motor (Changan),
UZBEK-XITOY SAVDO UYI (uskuna, yuk mashinalari), FOCUS FURNITURE (Turkiya/Xitoy/Malayziya to'g'ridan-to'g'ri), CHILONZOR SAVDO UYI (qurilish mollari optom),
CHINA MOTORS (Xitoy yuk mashinalari), @Pekinoptom_uz, best.shopping.uz, optom_obuv_uzb, tkani_tashkenta (Abu Saxiy), OLX'dagi «optom/sklad» maishiy texnika sotuvchilari.

Platformalar haqiqati (agentlarning o'z izohlari asosida):

| Platforma | Qidiruv nima berdi | Nima bermadi | Xulosa |
|---|---|---|---|
| Instagram | 25 akkaunt; ruscha «оптом Ташкент Китай», «Guangzhou оптом склад» so'rovlari eng zich; faqat `allowed_domains=instagram.com` ishladi, `site:` operatori emas | telefon, obunachi soni, caption — snippetda yo'q; Shimkent/Bishkek va Guanchjou bayerlari shovqini; «optom» «optika» bilan to'qnashadi | Kashf — qidiruv; **telefon/Telegram — bio'ni to'g'ridan-to'g'ri o'qish** (Business Discovery API yoki past tezlikdagi skraper); hashtaglar: #optomtoshkent #оптомташкент #xitoydanoptom #abusaxiy |
| Facebook | 12 so'rovdan 8 tasi 0 natija; guruhlar umuman indekslanmagan; topilganlar — mayda ulgurjichilar, Xitoy belgisi yo'q | guruhlar, telefon, faollik | **Asosiy kanal emas**; Odnoklassniki (ok.ru) hatto ko'proq Toshkent savdo guruhlarini ko'rsatdi; rasmiy yo'l — Meta Lead Ads |
| OLX | 16; «оптом» bo'yicha maishiy texnika/konditsioner/shina qayta sotuvchilari, «sklad» | telefon (tugma ortida), Telegram; profil sahifalari snippet'siz | OLX'ni **to'g'ridan-to'g'ri** o'qish kerak (Apify `lowlanddata/olx-uz-scraper`, biznes-bayroq + optom/konteyner/sklad filtri, bitta sotuvchida 10+ e'lon) |
| Google / saytlar | 25; eng kuchlilari brend distribyutorlari (Jinko/Huawei, XCMG, Changan, Zoomlion, Foton/Howo); natijalar asosan goldenpages/yellowpages/top.uz/prom.uz/glotr kartalari | kompaniya saytlari kam; 2gis.uz va yandex.uz/maps umuman chiqmaydi | Kataloglar rubrikalarini **crawl** qilish (goldenpages 1424/1277/1558, yellowpages «import», «tovary-iz-kitaya», top.uz «стройматериалы оптом» — 231 kompaniya) + brend-diler so'rovlari |
| Telegram | `t.me/s` va `site:t.me` qidiruvda chiqmaydi; faqat uz.tgstat.com/telemetr.me orqali: @Pekinoptom_uz, Ningbo MH, Chinni Bozor Bekzod, «CHINNiLAR OPTOM TOSHKENT» guruhi (165k a'zo) | telefon raqamlari sahifada bor, lekin snippet'da yo'q | Haqiqiy manba — **TGStat O'zbekiston «Sales» reytingi va bozor guruhlari** (Chinni bozor, Abu Saxiy, Chorsu, Olmazor); Telethon bilan ichkaridan o'qish |
| Marketplace/katalog | 22; glotr.uz `/showcase/` sahifalari eng boy (China Motors, Uzbek-Chinese Trading House, China Leasing Impex, XITOY UPAKOVSHIK, TAO.UZ, KIS.uz) | uzum.uz do'kon sahifalari, flagma, top.uz qidiruvda chiqmaydi | Uzum — faqat API/skraper; prom.uz «страна производства: Китай» filtri + `/tags/optom/`; glotr «Soliq tasdiqlagan» belgisi |
| Kataloglar | 30; goldenpages «Оборудование из Китая» (Id=106080), yellowpages «tovary-iz-kitaya», all.biz «Китайские товары оптом в Ташкенте», optlist.ru Toshkent | telefonlar «+998...» deb yashirilgan, faqat sahifada | Rubrika sahifalarini **ochib** kompaniya kartalarini olish — har rubrikada 56–231 kompaniya |

Umumiy xulosa: **qidiruv tizimi «kim»ni topadi, telefon va yuridik nom esa sahifaning o'zida.** Ishlab chiqarishdagi agent uchun bu muammo emas — u sahifani ochadi.
Bu sinovda eng ko'p hosil bergan tartib: (1) katalog va glotr/prom rubrikalari, (2) Instagram ruscha so'rovlar, (3) TGStat/Telemetr katalogi, (4) OLX biznes akkauntlar, (5) brend distribyutorlari.
Facebook — oxirgi o'rinda.

## 5. Rasmiy API'lar, cheklovlar va narxlar (tekshirilgan 2026-10-23; to'liq: `docs/ochiq-internet-api-memo.md`)

| Platforma | Rasmiy yo'l | Norasmiy yo'l | Narx / cheklov | MVP? |
|---|---|---|---|---|
| Instagram | Graph API **Business Discovery** (username → bio, sayt, obunachilar, postlar; o'z biznes akkaunt + app + review), **Hashtag Search** (7 kunda 30 ta noyob hashtag) | Apify profil skraperlari $1.60–2.50 / 1 000 profil | bepul / Apify; Meta ToS xavfi faqat norasmiy yo'lda | Ha: kashf — qidiruv, boyitish — Business Discovery |
| Facebook | `pages/search` **hali ishlaydi** (app token + Page Public Metadata Access); Meta Lead Ads | guruh/Marketplace skrapingi — yo'q | bepul | Oyda bir marta sahifa qidiruvi; asosan inbound |
| Google qidiruv | **Custom Search JSON API yangi mijozlarga yopiq** (2027-01-01 to'xtaydi) | **DataForSEO** navbat $0.60 / 1 000 so'rov; SerpApi $75/oy = 5k; Bright Data $2–3 / 1k | arzon | **Ha — DataForSEO** bilan haftalik 460 ta so'rov (≈$0.30) |
| Google Maps | **Places API (New) Text Search**: Pro $32 / 1k, **oyiga 5 000 bepul**; faqat `place_id` saqlanadi, koordinata ≤30 kun | Apify xarita skraperlari | bepul limit yetarli | Ha, haftasiga ≤1 250 so'rov |
| Yandex Maps | Geosearch API — yiliga **$2 000 dan** (O'zbekiston qamrovi ko'rsatilmagan) | Apify | qimmat | Yo'q |
| 2GIS | Places API — narx so'rov bo'yicha | Apify $0.40–4.99 / 1k, O'zbekiston bor | arzon | «оптовая база/склад» rubrikalarida 1 000 qatorli pilot |
| OLX.uz | API yo'q | Apify `lowlanddata/olx-uz-scraper` $1.99 / 1k (biznes-bayroq bor, shaxsiy ma'lumot yo'q) | arzon | Ha |
| Telegram | TGStat Search API (S-tarif 4 200 RUB/oy), `t.me/s` ko'rinishlar, Telethon faqat o'qish | — | arzon | Ha |
| Uzum | — | Apify `getascraper/uzum-scraper` $3.30 / 1k (buyurtma soni bor; sotuvchi maydoni tekshirilmagan) | arzon | Ha, sotuvchi nomi qo'lda tekshirilgach |
| Glotr / Flagma / Top.uz / Prom.uz | — | robots.txt ruxsat bersa past tezlikda | bepul | Ha |

## 6. Haftalik avtomatik tsikl (UTC+5)

| Kun | Ish | Hajm / xarajat |
|---|---|---|
| Du | SERP partiyasi (DataForSEO): 17 kategoriya × 3 til × 3 B2B marker × 3 geo ≈ 460 so'rov → instagram / t.me / olx / sayt URL'lari | ≈$0.30 |
| Se | Places Text Search «оптовая база / склад / optom» × kategoriya × shahar; faqat `place_id` + ball | ≤1 250 so'rov/hafta (oylik bepul limit ichida) |
| Ch | Instagram Business Discovery — yangi handle'lar bo'yicha bio/sayt; Hashtag API — 4 ta yangi hashtag | bepul |
| Pa | OLX biznes-bayroqli e'lonlar, ulgurji kategoriyalar; sotuvchi bo'yicha jamlash | ≈2 000 qator ≈ $4 |
| Ju | Uzum kalit so'zlar (buyurtma soni ≥ chegara) + Glotr/Flagma/Top.uz | ≈1 000 qator ≈ $3.30 |
| Sh | Telegram: ~50 kanalning `t.me/s` suratlari, TGStat post qidiruvi («оптом из Китая», «konteyner»), Telethon faqat o'qish | TGStat tarifi |
| Ya | Ball, dedupe (telefon/handle/sayt), raqobatchilarni bostirish, ERP'ga push, egaga haftalik hisobot | — |
| Oyda 1 | Facebook `pages/search`, API narx/limitlarini qayta tekshirish, 30 kundan eski koordinatalarni o'chirish | — |

## 7. Deterministik «B2B ≥3 m³» belgi ro'yxati (≥4 ball = lid; QATTIQ-SALBIY bo'lsa — tashlanadi)

- **Umumiy:** +2 «оптом/optom/ulgurji» nom/bio/sarlavhada; +2 «из Китая/Xitoydan/1688/Taobao/Yiwu/Guangzhou»; +2 «контейнер/konteyner/фура/fura/партия/partiya»; +1 «от производителя/zavoddan/прямые поставки»; +1 «дилер/дистрибьютор/поставщик»; +1 «склад/sklad/база» yoki bozor nomi (Abu Saxiy, Bek Baraka, Chorsu, Urikzor, Yangiobod, Malika, Sergeli); +1 «для магазинов/do'konlar uchun/мин. заказ/MOQ»; +1 yuridik shakl (MChJ/ООО/XK/ИП/STIR).
- **QATTIQ-SALBIY:** nomda/bio'da «карго/kargo/cargo/логистика/доставка из Китая/yuk tashish» (raqobatchi); faqat B2C («в розницу», «1 dona», shaxsiy profil, kompaniya nomi yo'q).
- **Instagram:** bio'da marker + sayt yoki Telegram + ≥50 post; caption'larda qadoq birliklari («коробка/karobka», «мешок/qop», «рулон/rulon»); biznes kategoriya «Wholesale». Obunachi soni o'zi signal emas.
- **OLX:** biznes-bayroq + «оптом/партия/контейнер/со склада/от N шт» + bir kategoriyada ≥10 faol e'lon; «по предзаказу из Китая» — kuchli.
- **Telegram:** tavsifda marker; «новое поступление / yangi konteyner keldi / фура пришла» postlari; narx ro'yxati fayl ko'rinishida; «опт от N штук/коробки»; admin — kompaniya akkaunti. Raqobatchi chatlarida «ищу поставщика / нужен контейнер» yozgan a'zo = lid.
- **Xaritalar/kataloglar:** rubrika «оптом/склад/оптовая база» + telefon + sayt + ulgurji bozor hududidagi manzil.
- **Uzum:** bitta Xitoy-import kategoriyasida ≥N SKU va buyurtma soni oylik hajmni ko'rsatsa; sotuvchining yuridik nomi bor.
- **Kompaniya saytlari:** «о компании»da «импортёр/официальный дистрибьютор», «прямые поставки из Китая», ≥50 SKU katalog, B2B narx ro'yxati.

Huquqiy eslatma: kompaniya darajasidagi ma'lumot (nom, STIR, biznes telefon, manzil, sayt) — shaxsiy ma'lumot emas; shaxsiy Instagram/Telegram handle va mobil — shaxsiy ma'lumot (asos: tijorat aloqasi uchun o'zi e'lon qilgan; so'rov bo'yicha o'chirish).
Faqat loginsiz ommaviy yig'ish; login/CAPTCHA aylanib o'tilmaydi; robots.txt hurmat qilinadi; Places ma'lumoti `place_id` sifatida saqlanadi; har maydon uchun manba va sana yoziladi.
