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

