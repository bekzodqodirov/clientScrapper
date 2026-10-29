"""Owner-facing Uzbek text samples (07 §1.0, §1.11). Synthetic only.

``tests/test_uz_text.py`` holds every rule of 07 §1.11 to these samples:
each GOOD text passes all of them, each BAD text trips exactly the rules it
names, so a rule that stopped seeing its shape turns the corpus red.

T-30's sections (04 §4.5, ``tests/test_redlines.py``):
  * REDLINE_BAD / REDLINE_GOOD — DraftOpener drafts held to the redline gate
    (≥ 30 bad, each with the flags it raises; ≥ 10 good);
  * PLAYBOOK_TEMPLATES / PLAYBOOK_TEMPLATES_RU — templates A–E of
    docs/sotuv-playbook.md §3.1 as the code fills them (the reference copy of
    ``services/playbook.TEMPLATES``): the playbook's messages without the
    day counts, digits, tariff and customs words a machine-written first
    message may not carry, spelled with ʻ/ʼ (04 Q9 a).
The openers are prospect-facing, but a manager reads them on the card and in
the ERP note, so the Uzbek Latin ones follow these rules too.
"""

# A review card as 06 §1.3 renders it, with synthetic values. The quote in
# «…» is evidence, verbatim and possibly Russian: data, not text.
CARD = (
    "🆕 Nomzod · 70/100 · Bozor ulgurji savdosi\n"
    "🏢 Namuna Tekstil Savdo (MChJ) · Toshkent · STIR 300000011 · faol\n"
    "📦 mato, furnitura · Xitoy: ha\n"
    "📐 Hajm: «2 kub» — oʻzi yozgan\n"
    "📞 +998 90 000 00 01 (mobil) · 👤 Rustam, direktor · @namuna_1\n"
    "🔎 «Гуанчжоудан 2 куб мато керак, ким олиб келади?» — Telegram guruh, 20.10.2026\n"
    "🔗 https://t.me/namuna_optom/18234\n"
    "🧮 hajm 20 · Xitoy 20 · firma 12 · yangilik 10 · aloqa 8 · tasdiq 0\n"
    "✔️ mijoz emas · DNC yoʻq · 90 kunda yuborilmagan"
)

GOOD = (
    CARD,
    "✅ Navbatda — CRMʼga yuboriladi",
    "⏳ Ertaga 09:30 da yuboriladi",
    "⚠️ CRM javob bermadi — qayta soʻrayman",
    "🔀 Birlashtirildi → Namuna Mebel Plus",
    "🔒 DNC — boshqa koʻrsatilmaydi",
    "🚚 Kargo — chiqarildi; kanali manba sifatida qoladi",
    "⏸ Toʻxtatildi. Davom ettirish: /yoq",
    "Bu karta eskirgan — /lidlar",
    "Masʼul: Rustam (direktor)",
    "NIMA OLIB KELADI: mato, aksessuar · Xitoy: ehtimol (Guangzhou)",
    "Bugun: push 3/10 · karta 7/40 · xarajat $1.20/10 · oy $14/250",
    "🩺 Agent holati — 24.10.2026 09:00",
    # A verbatim quote keeps the source's own apostrophes.
    "🔎 «to'g'ri, 2 kub kerak, ma'lumot bering» — OLX, 20.10.2026",
)

# (text, the rules it trips). Rules: okina (oʻ/gʻ need U+02BB), tutuq (the
# glottal stop needs U+02BC), okina_misplaced (U+02BB after anything but
# o/g), cyrillic, pseudonym, placeholder (rendered output only), too_long.
BAD = (
    ("Kunlik chegara — pullik chaqiruvlar to'xtadi", {"okina"}),
    ("Kunlik chegara — pullik chaqiruvlar to’xtadi", {"okina"}),
    ("Koʼrib chiqish navbati", {"okina"}),
    ("Bog`lanish yoʻq", {"okina"}),
    ("Ma'lumot yetarli emas", {"tutuq"}),
    ("Mas`ul: Rustam (direktor)", {"tutuq"}),
    ("CRM’ga yuborildi", {"tutuq"}),
    ("Yangi taʻminotchi topildi", {"okina_misplaced"}),
    ("CRM не ответила", {"cyrillic"}),
    ("📞 <PHONE_1> · @namuna_1", {"pseudonym"}),
    ("Masʼul: <NAME_2>", {"pseudonym"}),
    # A quote is the source's text, but a token left in it is ours.
    ("🔎 «<PHONE_1> ga qoʻngʻiroq qiling» — OLX", {"pseudonym"}),
    ("Masʼul: «Seller <NAME_2>»", {"pseudonym"}),
    ("Salom, {full_name}!", {"placeholder"}),
    ("🔎 «{quote}» — OLX, 20.10.2026", {"placeholder"}),
    ("a" * 4097, {"too_long"}),
)

# Button labels of 06 §1.3 and one that would be cut on a phone.
GOOD_BUTTONS = (
    "✅ CRMʼga yubor",
    "✖️ Yoʻq",
    "🔀 Birlashtir",
    "🔒 DNC",
    "⏭ Keyinroq",
    "⋯ Boshqa",
    "🙍 B2C",
    "🚚 Kargo/raqobatchi",
    "✏️ Telefon",
    "◀️ Orqaga",
    "👤 Menejerga",
)
BAD_BUTTONS = ("✅ CRMʼga yubor va menejerga hoziroq xabar qoldir",)

# --- T-30 (04 §4.5): the redline gate ------------------------------------------------

# The opt-out sentences (docs/sotuv-playbook.md §2.6, spelled with ʻ).
OPTOUT_UZ = 'Agar qiziq boʻlmasa, "yoʻq" deb yozing — boshqa yozmayman.'
OPTOUT_UZ_CYRL = "Агар қизиқ бўлмаса, «йўқ» деб ёзинг — бошқа ёзмайман."
OPTOUT_RU = "Если неактуально — напишите «нет», больше не побеспокою."
# What an opener in this corpus may hook on, and the seeds' forwarder names.
HOOKS = ("mato", "mebel", "kafel", "oʻyinchoq", "OLX", "Telegram", "Instagram", "katalog")
COMPETITORS = ("MK Express", "Halol Cargo")

_HEAD = "Assalomu alaykum, [Ism]. Men [Men], GSR Logistics. OLXda mato optom savdongizni koʻrdim. "
_ASK = "Oyiga necha kub olasiz? "
_RU_HEAD = "Здравствуйте. Я [Men], GSR Logistics. Увидел ваш опт по ткани на OLX. "
_RU_ASK = "Сколько кубов в месяц обычно везёте? "


def _uz(middle: str) -> str:
    return _HEAD + middle + " " + _ASK + OPTOUT_UZ


def _ru(middle: str) -> str:
    return _RU_HEAD + middle + " " + _RU_ASK + OPTOUT_RU


# (draft, lang, the flags it raises — exactly these).
REDLINE_BAD: tuple[tuple[str, str, set[str]], ...] = (
    # prices, currencies, percentages
    (_uz("Kubiga narx 150 dollar."), "uz", {"digit", "money"}),
    (_uz("Narxlarimiz hammadan qulay."), "uz", {"money"}),
    (_uz("Tarifimiz zichlik boʻyicha yoziladi."), "uz", {"money"}),
    (_uz("Doimiy mijozga 10% chegirma."), "uz", {"digit", "money"}),
    (_uz("Hisob-kitob soʻm bilan."), "uz", {"money"}),
    (_uz("Yukni bepul olib ketamiz."), "uz", {"money"}),
    (_uz("Komissiya atigi 3%."), "uz", {"digit", "money"}),
    (_ru("Цена от 150 долларов за куб."), "ru", {"digit", "money"}),
    (_ru("Даём скидку постоянным клиентам."), "ru", {"money"}),
    # a percentage in words, a claim about the price
    (_uz("Uch foiz komissiya olamiz."), "uz", {"money"}),
    (_uz("Bizda juda arzon."), "uz", {"money"}),
    (_uz("Boshqalardan qimmat emasmiz."), "uz", {"money"}),
    (_ru("На двадцать процентов дешевле."), "ru", {"money"}),
    (_ru("Недорого и надёжно."), "ru", {"money"}),
    (_ru("Возим дешевле других."), "ru", {"money"}),
    (_ru("Условия выгоднее, чем у других."), "ru", {"money"}),
    # days, durations, speed
    (_uz("Toshkentgacha 15 kunda yetkazamiz."), "uz", {"digit", "time"}),
    (_uz("Bir hafta ichida keladi."), "uz", {"time"}),
    (_uz("Yukni tez olib kelamiz."), "uz", {"time"}),
    (_uz("Muddati juda qisqa."), "uz", {"time"}),
    (_uz("Ertaga javob bersangiz yaxshi."), "uz", {"time"}),
    (_uz("Yetkazib berish 5-7 kun."), "uz", {"digit", "time"}),
    (_ru("Срок доставки — неделя."), "ru", {"time"}),
    (_ru("Довезём быстрее всех."), "ru", {"time"}),
    # a month as a delivery time
    (_uz("Bir oyda Toshkentga yetkazamiz."), "uz", {"time"}),
    (_uz("Oy ichida yetkazib beramiz."), "uz", {"time"}),
    (_ru("Доставим за месяц."), "ru", {"time"}),
    (_ru("Привезём через два месяца."), "ru", {"time"}),
    # guarantees and promises
    (_uz("Kafolat beramiz."), "uz", {"promise"}),
    (_uz("Albatta oʻz vaqtida yetkazamiz."), "uz", {"promise"}),
    (_uz("Eng arzon xizmat bizda."), "uz", {"money", "promise"}),
    (_uz("Bojsiz olib kelamiz."), "uz", {"promise"}),
    (_uz("Yuz foiz emas, 100 foiz ishonchli."), "uz", {"digit", "money", "promise"}),
    (_ru("Гарантируем сохранность груза."), "ru", {"promise"}),
    # STIR, customs, declarations, tax
    (_uz("STIR raqamingizni yuboring."), "uz", {"legal"}),
    (_uz("Bojxona hujjatlarini oʻzimiz qilamiz."), "uz", {"legal"}),
    (_uz("Deklaratsiyani oʻzimiz toʻldiramiz."), "uz", {"legal"}),
    (_uz("Soliq masalasida ham yordam beramiz."), "uz", {"legal"}),
    (_ru("Растаможка под ключ."), "ru", {"legal", "competitor"}),
    (_ru("Поможем с ИНН и налогами."), "ru", {"legal"}),
    # links and handles
    (_uz("Saytimiz: www.namuna-gsr.uz"), "uz", {"link"}),
    (_uz("Kanalimiz t.me/namuna_kanal da."), "uz", {"link"}),
    (_uz("Yozing: @namuna_menejer"), "uz", {"link"}),
    (_uz("Batafsil https://namuna.uz sahifasida."), "uz", {"link"}),
    # a link with no scheme
    (_uz("Saytimiz: namuna-gsr.uz"), "uz", {"link"}),
    (_uz("Sahifamiz instagram.com/namuna_gsr da."), "uz", {"link"}),
    (_uz("WhatsApp orqali yozing: wa.me/namuna"), "uz", {"link"}),
    (_ru("Сайт: намуна.уз"), "ru", {"link"}),
    # competitors
    (_uz("Halol Kargo kabi emasmiz."), "uz", {"competitor"}),
    (_uz("MK Express dan qulayroq."), "uz", {"competitor"}),
    # another language
    (
        "Hello! We saw your mato listing on OLX, we ship goods from China with great "
        "service and quality, just write to us. " + OPTOUT_UZ,
        "uz",
        {"language"},
    ),
    (_uz("Сәлеметсіз бе, достар."), "uz", {"language"}),
    (_HEAD + _ASK + OPTOUT_RU, "ru", {"language"}),
    # the opt-out sentence
    (_HEAD + _ASK, "uz", {"optout_missing"}),
    (_HEAD + _ASK + OPTOUT_UZ + " Rahmat.", "uz", {"optout_missing"}),
    (_HEAD + _ASK + "Qiziq boʻlmasa, yozing.", "uz", {"optout_missing"}),
    (_RU_HEAD + _RU_ASK + OPTOUT_UZ, "ru", {"optout_missing"}),
    # no hook, a pseudonym left in it, a figure in another script
    (
        "Assalomu alaykum. Men [Men], GSR Logistics. Xitoydan yuk olib kelamiz. "
        + _ASK
        + OPTOUT_UZ,
        "uz",
        {"hook_missing"},
    ),
    (_uz("Raqamingiz <ID> boʻyicha yozdim."), "uz", {"pseudonym"}),
    (_uz("Yuk ２０ kub boʻlsa ham olamiz."), "uz", {"digit"}),
)

# (draft, lang): openers the gate lets stand.
REDLINE_GOOD: tuple[tuple[str, str], ...] = (
    (
        "Assalomu alaykum. Men [Men], GSR Logistics, 2018-yildan Xitoydan yuk olib "
        "kelamiz. OLXda mato optom savdongizni koʻrdim. Yukni Yiwuda qabul qilib "
        "fotohisobot yuboramiz. " + _ASK + OPTOUT_UZ,
        "uz",
    ),
    (
        _HEAD + "Yukni Guangzhouda qabul qilib oʻlchaymiz. " + _ASK
        + "Agar qiziq bo'lmasa, \"yo'q\" deb yozing - boshqa yozmayman.",
        "uz",
    ),
    (
        _HEAD + _ASK + "Agar qiziq boʻlmasa, «yoʻq» deb yozing — boshqa yozmayman",
        "uz",
    ),
    (
        "Assalomu alaykum, [Ism]. Men [Men], GSR Logistics. Katalogda mebel boʻyicha "
        "korxonangizni koʻrdim. Xitoydan toʻliq fura yoki konteynerda olib kelamiz. "
        "Odatda yigʻma yukmi yoki toʻliq fura? " + OPTOUT_UZ,
        "uz",
    ),
    (
        "Ассалому алайкум. Мен [Men], GSR Logistics. ОЛХда мато савдонгизни кўрдим. "
        "Юкни Иу ва Гуанчжоуда қабул қиламиз. Ойига неча куб оласиз? " + OPTOUT_UZ_CYRL,
        "uz",
    ),
    (
        _RU_HEAD + "Принимаем груз в Иу и Гуанчжоу, присылаем фотоотчёт. "
        + _RU_ASK + OPTOUT_RU,
        "ru",
    ),
    (
        "Здравствуйте, [Ism]. Я [Men], GSR Logistics, с 2018 года возим грузы из "
        "Китая. Увидел ваш магазин мебели в Telegram. Есть ли закупка в ближайшие "
        "месяцы? " + OPTOUT_RU,
        "ru",
    ),
    (
        "Assalomu alaykum. Men [Men], GSR Logistics. Instagramdagi doʻkoningizni "
        "koʻrdim — oʻyinchoqlar Xitoydan keladi shekilli. Tovarni Xitoyda tekshirib "
        "foto yuboramiz. Keyingi partiya qachon va necha kub? " + OPTOUT_UZ,
        "uz",
    ),
    (
        "Assalomu alaykum. Men [Men], GSR Logistics. Telegram guruhda yozganingizni "
        "koʻrdim. Yiwu va Guangzhouda oʻz punktlarimiz bor, yukni oʻlchab foto "
        "yuboramiz. " + _ASK + OPTOUT_UZ,
        "uz",
    ),
    (
        "Assalomu alaykum. Men [Men], GSR Logistics. Koʻrgazma roʻyxatida kafel "
        "boʻyicha kompaniyangizni koʻrdim. Ogʻir yuk uchun fura va konteyner "
        "hisobini yonma-yon koʻrsatamiz. Odatda toʻliq furami? " + OPTOUT_UZ,
        "uz",
    ),
    (_HEAD + _ASK + OPTOUT_UZ.replace(".", "!"), "uz"),
    # goods words that only begin like a time or STIR word, and the volume
    # a month («oyiga», «в месяц») and a purchase to come («yaqin oylarda»)
    (
        "Assalomu alaykum. Men [Men], GSR Logistics. OLXda kungaboqar yogʻi va "
        "soatlar savdongizni koʻrdim. Yaqin oylarda Xitoydan xarid bormi? "
        + _ASK
        + OPTOUT_UZ,
        "uz",
    ),
    (
        "Здравствуйте. Я [Men], GSR Logistics. Увидел на OLX ваш опт: стиральные "
        "машины. Есть ли закупка в ближайшие месяцы? " + _RU_ASK + OPTOUT_RU,
        "ru",
    ),
)

# Templates A–E (docs/sotuv-playbook.md §3.1) as services/playbook fills them:
# {salom} «, [Ism]» or nothing, {manba} where the company was seen, {tovar}
# its goods. The reference copy — tests/test_redlines.py pins the two equal.
PLAYBOOK_TEMPLATES: dict[str, str] = {
    "A": (
        "Assalomu alaykum{salom}. Men [Men], GSR Logistics (Toshkent), 2018-yildan "
        "Xitoydan yuk olib kelamiz. {manba}da {tovar} optom savdongizni koʻrib "
        "yozyapman. Yukni Yiwu va Guangzhoudagi oʻz punktlarimizda qabul qilib "
        "oʻlchaymiz va fotohisobot yuboramiz. Oyiga taxminan necha kub olasiz? "
        + OPTOUT_UZ
    ),
    "B": (
        "Assalomu alaykum{salom}. Men [Men], GSR Logistics. {manba}dagi "
        "doʻkoningizni koʻrdim — {tovar} Xitoydan keladi shekilli. Xitoydagi "
        "saytlardan xarid qilib beramiz, tovarni Xitoyda tekshirib foto yuboramiz, "
        "hujjatlarni sertifikat va buxgalteriyaga mos qilib tayyorlaymiz. Keyingi "
        "partiya qachon va taxminan necha kub yoki kilogramm? " + OPTOUT_UZ
    ),
    "C": (
        "Assalomu alaykum{salom}. Men [Men], GSR Logistics (Toshkent). {manba}da "
        "{tovar} boʻyicha korxonangizni koʻrdim. Xitoydan uskuna va xomashyoni "
        "konteynerda olib kelamiz, zavodda yukni tekshirib foto beramiz, "
        "hujjatlarni oʻzimiz tayyorlaymiz. Yaqin oylarda rejalashtirilgan xarid "
        "bormi? " + OPTOUT_UZ
    ),
    "D": (
        "Assalomu alaykum{salom}. Men [Men], GSR Logistics. {manba}da {tovar} "
        "boʻyicha optom savdongizni koʻrdim. Ogʻir yuk uchun toʻliq fura yoki "
        "konteyner hisobini yonma-yon koʻrsatamiz, Yiwu va Guangzhouda qabul qilib "
        "foto yuboramiz. Mavsumda Xorgosda navbat boʻladi — joyni oldindan band "
        "qilamiz. Odatda toʻliq furami yoki yigʻma? " + OPTOUT_UZ
    ),
    "E": (
        "Assalomu alaykum{salom}. Men [Men], GSR Logistics. {manba}da {tovar} "
        "boʻyicha faoliyatingizni koʻrdim. Guangzhouda oʻz qabul punktimiz bor: "
        "har bir joyni sanab, nozik tovarni tekshirib foto yuboramiz, hujjatlarni "
        "oʻzimiz rasmiylashtiramiz. Avtomobil va elektromobil importi ham bor. "
        "Oyiga taxminan necha kub yoki kilogramm olasiz? " + OPTOUT_UZ
    ),
}
PLAYBOOK_TEMPLATES_RU: dict[str, str] = {
    # {tovar} is the goods with the words around them (« по товару «mato»»,
    # «товар «mato»», «: «mato»»), or what stands when none can be named.
    "A": (
        "Здравствуйте{salom}. Я [Men], GSR Logistics (Ташкент), с 2018 года возим "
        "грузы из Китая. Пишу, потому что увидел {manba} ваш опт{tovar}. "
        "Принимаем груз на своих пунктах в Иу и Гуанчжоу, замеряем и присылаем "
        "фотоотчёт. Сколько кубов в месяц обычно везёте? " + OPTOUT_RU
    ),
    "B": (
        "Здравствуйте{salom}. Я [Men], GSR Logistics. Увидел ваш магазин {manba} — "
        "похоже, {tovar} идёт из Китая. Закупаем товар на китайских площадках, "
        "проверяем его в Китае и присылаем фото, документы готовим так, чтобы они "
        "подходили для сертификации и бухгалтерии. Когда следующая партия и "
        "сколько примерно кубов или килограммов? " + OPTOUT_RU
    ),
    "C": (
        "Здравствуйте{salom}. Я [Men], GSR Logistics (Ташкент). Увидел {manba} "
        "ваше предприятие{tovar}. Везём оборудование и сырьё из Китая в "
        "контейнерах, принимаем груз на заводе с фотоотчётом, документы оформляем "
        "сами. Есть ли закупка в ближайшие месяцы? " + OPTOUT_RU
    ),
    "D": (
        "Здравствуйте{salom}. Я [Men], GSR Logistics. Увидел {manba} ваш "
        "опт{tovar}. Для тяжёлого груза считаем рядом два варианта — полная "
        "фура и контейнер, приёмка и фото в Иу и Гуанчжоу. В сезон на Хоргосе "
        "очереди — бронируем место заранее. Вы обычно везёте полной фурой или "
        "сборным? " + OPTOUT_RU
    ),
    "E": (
        "Здравствуйте{salom}. Я [Men], GSR Logistics. Увидел {manba} вашу "
        "работу{tovar}. У нас свой пункт приёма в Гуанчжоу: пересчитываем "
        "места, проверяем хрупкое и присылаем фото, документы делаем сами. Есть и "
        "импорт авто и электромобилей. Сколько кубов или килограммов в месяц "
        "обычно везёте? " + OPTOUT_RU
    ),
}
# The playbook's own A–E messages (docs/sotuv-playbook.md §3.1, UZ Latin, as
# written there): what a template may not stay — each trips the gate.
PLAYBOOK_RAW_A = (
    "Assalomu alaykum, [Ism] aka. Men [Men], GSR Logistics (Toshkent), 2018-yildan "
    "Xitoydan yuk olib kelamiz. [manba]da [tovar] optom savdongizni ko'rib "
    "yozyapman. Yukni Yiwu va Guangzhoudagi o'z punktlarimizda qabul qilib "
    "o'lchaymiz, fotohisobot yuboramiz, narx zichlik bo'yicha kubga yoziladi, "
    "Toshkentgacha 15–25 kun. Oyiga taxminan necha kub olasiz? Agar qiziq "
    "bo'lmasa, \"yo'q\" deb yozing — boshqa yozmayman."
)
PLAYBOOK_RAW_B = (
    "Assalomu alaykum, [Ism]. Men [Men], GSR Logistics. [Uzum/Instagram]dagi "
    "[do'kon] do'koningizni ko'rdim — [tovar] Xitoydan keladi shekilli. Biz "
    "1688/Taobao/Alibaba dan xarid qilib beramiz (3% komissiya), Xitoyda tovarni "
    "tekshirib foto yuboramiz, GTD va invoyslarni sertifikat/buxgalteriyaga mos "
    "qilib rasmiylashtiramiz, yig'ma yuk 15–25 kun, shoshilinch bo'lsa avia. "
    "Keyingi partiya qachon va taxminan necha kub yoki kg? Agar qiziq bo'lmasa, "
    "\"yo'q\" deb yozing — boshqa yozmayman."
)
PLAYBOOK_RAW: dict[str, tuple[str, set[str]]] = {
    "A": (PLAYBOOK_RAW_A, {"digit", "money", "time"}),
    "B": (PLAYBOOK_RAW_B, {"digit", "money", "time", "legal"}),
}
