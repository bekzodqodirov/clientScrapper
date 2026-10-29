# VENDORED_FROM dailyJournal@6b1f67f miya/bot/replies.py
"""Every word the owner bot says that is not a card (06 §1.1: MIYA's
``replies.py`` → ``texts.py``). Cards are ``services/cards.py``'s.

Uzbek Latin with ʻ (U+02BB) after o/g and ʼ (U+02BC) between letters
(07 §1.11, ``tests/test_uz_text.py`` lints every value here). ``t(key, …)``
fills ``{name}`` placeholders with ``str.format``; the HTML a value carries
(``<code>``, ``<b>``) is the bot's own — a filled value is escaped by the
caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

UZ: Final[Mapping[str, str]] = MappingProxyType(
    {
        # --- every handler -----------------------------------------------------
        "error": "⚠️ Xatolik yuz berdi — birozdan keyin qaytadan urinib koʻring.",
        "stale_button": "Bu tugma eskirgan",
        "stale_card": "Bu karta eskirgan — /lidlar",
        # A manager's card: /lidlar is the owner's alone (06 §1.7).
        "stale_card_manager": "Bu karta eskirgan",
        "not_yours": "Bu karta sizga tegishli emas",
        "forwarded": (
            "↪️ Uzatilgan xabardagi buyruqni bajarmayman — buyruqni oʻzingiz "
            "yozing (/yordam)."
        ),
        "db_down": "⛔ Baza javob bermayapti — birozdan keyin qaytadan urinib koʻring.",
        "start_owner": (
            "Salom! Men mijoz qidiruvchi agentning botiman. Buyruqlar roʻyxati: /yordam"
        ),
        "start_manager": (
            "Salom! Bu bot sizga Telegram orqali yozish kerak boʻlgan nomzodlarni "
            "yuboradi. Buyruqlar: /yordam"
        ),
        "help_owner": (
            "/lidlar — koʻrib chiqiladigan kartalar (/lidlar holat — sanoq)\n"
            "/manbalar — manbalar: yoqish, oʻchirish, maʼlumot\n"
            "/chats — Telegram chatlar (/chats qoʻsh @username)\n"
            "/holat — agent holati va muammolar\n"
            "/xarajat — bugungi va oylik xarajat\n"
            "/toxtat [sabab] — hammasini darhol toʻxtatish\n"
            "/yoq — davom ettirish (/yoq crm — CRM toʻsigʻini olish)\n"
            "/import — ERP mijozlar faylini yuklash (fayl bilan)\n"
            "/unut — kompaniyani butunlay oʻchirish\n"
            "/qidir &lt;soʻz&gt; — kompaniyani qidirish\n"
            "/dnc &lt;+998… | @user&gt; — bu raqam yoki akkauntga yozilmasin\n"
            "/vazn [kalit ball] — ball vaznlari\n"
            "/push tekshir &lt;id&gt; — CRMʼda koʻrinmagan pushni tekshirish\n"
            "/stir &lt;nomzod_id&gt; &lt;9 raqam&gt; — STIRni qoʻlda yozish\n"
            "/yordam — shu roʻyxat"
        ),
        "help_manager": (
            "/qidir &lt;soʻz&gt; — kompaniyani qidirish\n"
            "/tel &lt;karta&gt; &lt;+998…&gt; — mijoz bergan raqamni yozish\n"
            "/yordam — shu roʻyxat\n"
            "Kartadagi tugmalar: 📞 Bogʻlandim · 🙅 Yozmang dedi · "
            "❌ Notoʻgʻri / B2C · ⏭ Keyinroq"
        ),
        # --- /lidlar -----------------------------------------------------------
        "lidlar.none": "Hozircha koʻrib chiqiladigan karta yoʻq.",
        "lidlar.more": "Navbatda yana {n} ta",
        "lidlar.skipped": (
            "{n} ta karta koʻrsatilmadi (darvozadan oʻtmadi yoki dalil yoʻq) "
            "va navbatdan olindi."
        ),
        "lidlar.holat_title": "📋 Nomzodlar holati",
        "lidlar.holat_line": "{state}: {n}",
        "lidlar.holat_oldest": "Koʻrib chiqishdagi eng eski karta: {age}",
        "lidlar.holat_no_review": "Koʻrib chiqishda karta yoʻq.",
        "lidlar.holat_auto": "⏳ Avtomatik tasdiqlangan, yuborilmagan: {n} ta",
        "lidlar.holat_auto_line": "  {name}: {note}",
        "lidlar.send_failed": "⚠️ Kartani yuborib boʻlmadi — birozdan keyin /lidlar",
        "lidlar.paused": (
            "⏸ Agent toʻxtatilgan — karta koʻrsatilmaydi. Davom ettirish: /yoq"
        ),
        "state.found": "topildi",
        "state.enriching": "boyitilmoqda",
        "state.scored": "ball olgan",
        "state.review": "koʻrib chiqishda",
        "state.approved": "tasdiqlangan",
        "state.rejected": "rad etilgan",
        "state.pushed": "CRMʼga yuborilgan",
        "state.suppressed": "bostirilgan",
        "state.excluded": "chiqarilgan",
        "state.hold": "kutishda",
        # --- review buttons (services/reviews, T-21) ----------------------------
        "review.no_targets": "Birlashtirish uchun mos kompaniya topilmadi.",
        "review.refused.wrong_card": "Bu tugma eskirgan",
        "review.refused.not_yours": "Bu karta sizga tegishli emas",
        "review.refused.stale": "Bu karta eskirgan — /lidlar",
        "review.refused.no_phone": (
            "Telefon yaroqsiz — CRMʼga yuborib boʻlmaydi. Karta joyida qoldi."
        ),
        "review.refused.no_target": "Bu kompaniya topilmadi.",
        "review.refused.same_company": "Bu oʻsha kompaniyaning oʻzi.",
        "review.refused.merge": "Birlashtirib boʻlmaydi: {why}. Karta joyida qoldi.",
        "review.merge.stir": "STIRlar har xil",
        "review.merge.telegram": "Telegram kanallari har xil",
        "review.merge.domain": "ikkalasi reestrda, saytlari har xil",
        "review.merge.candidate_approved": "nomzod CRMʼga yuborilmoqda",
        "review.merge.purged": "kompaniya oʻchirilgan",
        "review.merge.source_merged": "kompaniya allaqachon birlashtirilgan",
        "review.merge.target_merged": "tanlangan kompaniya allaqachon birlashtirilgan",
        "review.phone_ask": (
            "✏️ Toʻgʻri raqamni 5 daqiqa ichida yozing (+998 90 123 45 67)."
        ),
        "review.phone_done": "✏️ Telefon tuzatildi",
        "review.refused.bad_phone": (
            "Bu Oʻzbekiston mobil yoki shahar raqami emas — ✏️ ni qayta bosing."
        ),
        "review.refused.same_phone": "Bu raqam kartada bor — oʻzgarmadi.",
        "review.refused.phone_taken": (
            "Bu raqam kompaniyaning boshqa aloqasida bor — oʻzgarmadi."
        ),
        # --- /tel (a manager, 06 §1.7) ----------------------------------------
        "tel.how": "/tel &lt;karta raqami&gt; &lt;+998901234567&gt;",
        "tel.done": "✅ Raqam yozildi — lid CRMʼga yuboriladi ({score}/100).",
        "tel.held": (
            "Raqam yozildi, lekin lid hali yuborilmaydi ({score}/100) — kutishda qoldi."
        ),
        "tel.gated": (
            "Raqam yozildi, lekin lid CRMʼga yuborilmaydi ({score}/100) — firma "
            "tekshiruvdan oʻtmadi (mijoz, DNC yoki B2C boʻlishi mumkin)."
        ),
        "tel.no_card": "Bunday karta yoʻq.",
        "tel.not_yours": "Bu karta sizga tegishli emas.",
        "tel.stale": "Bu karta yopilgan — raqam yozilmadi.",
        "tel.bad_phone": "Bu Oʻzbekiston mobil yoki shahar raqami emas.",
        "tel.not_waiting": "Bu nomzod endi kutishda emas — raqam yozilmadi.",
        "tel.no_source": "Dalil topilmadi — raqam yozilmadi.",
        "review.merge_title": "🔀 Qaysi kompaniyaga birlashtiray?",
        # --- /manbalar ---------------------------------------------------------
        "manbalar.title": "🛰 Manbalar ({page}/{pages})",
        "manbalar.empty": "Manba yoʻq.",
        "manbalar.on": "✅ {title} yoqildi",
        "manbalar.off": "⛔ {title} oʻchirildi",
        "manbalar.gone": "Bu manba topilmadi",
        "manbalar.forbidden": "🚫 {title}: avtomatik push taqiqlandi",
        "manbalar.already_forbidden": "{title}: avtomatik push allaqachon taqiqlangan",
        "manbalar.line": (
            "{icon} {title} · oxirgi {age} · bugun {pages}/{budget} sahifa · {cost} · "
            "aniqlik {precision} ({pushes}) · {mode}"
        ),
        "manbalar.info_title": "ℹ️ {title} ({key})",
        "manbalar.info_runs": "Oxirgi ishga tushirishlar:",
        "manbalar.info_run": "  {at} · {status} · {pages} sahifa · {new} yangi{error}",
        "manbalar.info_no_runs": "Hali ishga tushmagan.",
        "manbalar.info_breaker": "Toʻsiq: {until} gacha ({reason})",
        "manbalar.info_paused": "Pauza: {until} gacha ({reason})",
        "manbalar.info_auto": "Avtomatik push: {state}",
        "manbalar.auto_on": "yoqilgan",
        "manbalar.auto_off": "oʻchiq",
        "manbalar.auto_earned": (
            "aniqlik yetarli, lekin hali yoqilmagan — lidlar sizning kartangizdan oʻtadi"
        ),
        "manbalar.auto_forbidden": "taqiqlangan",
        "manbalar.mode_auto": "avto",
        "manbalar.mode_manual": "qoʻlda",
        "manbalar.never": "hech qachon",
        # --- /chats ------------------------------------------------------------
        "chats.title": "💬 Telegram chatlar ({page}/{pages}) · {total} ta",
        "chats.empty": "Hali chat yoʻq. Qoʻshish: /chats qoʻsh @username",
        # seen_count counts every message ever read (02 §2.12): no 7-day count
        # is stored, so the line says «jami».
        "chats.line": (
            "{state} {icon} {title} · {hits} signal (7 kun) / jami {seen} xabar"
        ),
        "chats.on": "✅ {title} oʻqiladi",
        "chats.off": "❌ {title} endi oʻqilmaydi (chatdan chiqilmaydi)",
        "chats.never_read": "Shaxsiy yoki GSR chatini oʻqib boʻlmaydi",
        "chats.gone": "Bu chat topilmadi",
        "chats.added": "➕ @{username} qoʻshilish navbatiga qoʻyildi (kuniga 3 tagacha)",
        "chats.not_added": "@{username} navbatda bor yoki qoʻshib boʻlmaydi",
        "chats.bad_username": "Username kerak: /chats qoʻsh @username",
        # --- /holat, /xarajat --------------------------------------------------
        "holat.failed": "⚠️ Holatni oʻqib boʻlmadi — birozdan keyin /holat",
        # --- /toxtat, /yoq -----------------------------------------------------
        "toxtat.done": "⏸ Toʻxtatildi. Davom ettirish: /yoq",
        "yoq.ask": "Davom ettiraymi?",
        "yoq.ask_crm": "CRM toʻsigʻi {until} gacha. Olib tashlaymi?",
        "yoq.not_paused": "Agent toʻxtatilmagan — ishlayapti.",
        "yoq.no_breaker": "CRM toʻsigʻi yoʻq.",
        "yoq.resumed": "▶️ Davom etmoqda.",
        "yoq.kept": "⏸ Toʻxtatilgan holicha qoldi.",
        "yoq.unchanged": "Hech narsa oʻzgarmadi.",
        "yoq.breaker_cleared": "✅ CRM toʻsigʻi olindi — push davom etadi.",
        # --- /import -----------------------------------------------------------
        "import.how": (
            "ERP → Mijozlar → XLSX faylini /import izohi bilan yuboring "
            "(yoki faylga javoban /import deb yozing)."
        ),
        "import.too_big": (
            "Fayl juda katta ({mb} MB): ERP mijozlar fayli bunday boʻlmaydi."
        ),
        "import.download_failed": "Faylni yuklab boʻlmadi — qaytadan yuboring.",
        "import.ask": "Yozaymi?",
        "import.ask_fewer": "Kam qatorni tasdiqlab yozaymi?",
        "import.expired": "Reja eskirgan — faylni qayta yuboring.",
        "import.cancelled": "Bekor qilindi — hech narsa yozilmadi.",
        # --- /unut (services/purge, T-31) ---------------------------------------
        "unut.how": (
            "Nimani oʻchiray? /unut &lt;STIR | +998… | @username | sayt | uuid | nom&gt;"
        ),
        "unut.none": "Hech narsa topilmadi: «{query}»",
        "unut.many": (
            "{n} ta kompaniya topildi — bittasini uuid bilan yozing "
            "(nomini /qidir koʻrsatadi):\n{ids}"
        ),
        "unut.subject": "Kompaniya: <code>{id}</code> (nomi: /qidir {id})",
        "unut.ask": "Oʻchiraymi? Buni qaytarib boʻlmaydi.",
        "unut.by_name": (
            "⚠️ Nom boʻyicha topildi — avval /qidir {id} bilan "
            "shu kompaniya ekanini tekshiring."
        ),
        "unut.expired": "Reja eskirgan — /unut ni qaytadan yuboring.",
        "unut.cancelled": "Bekor qilindi — hech narsa oʻchirilmadi.",
        "unut.done": "Bu reja allaqachon bajarilgan.",
        "unut.closed": "Bu reja yopilgan — kerak boʻlsa /unut ni qaytadan yuboring.",
        "unut.running": (
            "Bu reja bajarilmoqda. Toʻxtab qolgan boʻlsa, /unut ni qaytadan "
            "yuboring — qolgan maʼlumot boʻlsa, yangi reja chiqadi."
        ),
        "unut.unfinished": (
            "Bu reja oxirigacha yetmagan — /unut ni qaytadan yuboring, "
            "qolgani oʻchiriladi."
        ),
        "unut.failed": (
            "⚠️ Oʻchirish oxirigacha yetmadi. /unut <code>{id}</code> ni qaytadan "
            "yuboring: qolgan maʼlumot boʻlsa, yangi reja chiqadi."
        ),
        # --- /qidir ------------------------------------------------------------
        "qidir.how": (
            "Nimani qidiray? /qidir &lt;telefon | @username | STIR | sayt | nom&gt;"
        ),
        "qidir.none": "Hech narsa topilmadi: «{query}»",
        # --- /dnc --------------------------------------------------------------
        "dnc.how": "Telefon yoki @username kerak: /dnc +998901234567 yoki /dnc @username",
        "dnc.foreign": (
            "Faqat Oʻzbekiston raqami (+998…) yoki @username: boshqa davlat raqamining "
            "oxirgi 9 raqami oʻzbek raqamiga toʻgʻri kelib qolishi mumkin."
        ),
        "dnc.preview": "🔒 DNC: {key}\n{companies}\nYozaymi?",
        "dnc.no_company": "Bu kalit hech qaysi kompaniyada yoʻq.",
        "dnc.company": "Kompaniya: {name}",
        "dnc.written": "🔒 DNC yozildi — {key}",
        "dnc.ended": "{n} ta ochiq nomzod yopildi.",
        "dnc.exists": "Allaqachon bor ({kind}, {day})",
        "dnc.expired": "Reja eskirgan — /dnc ni qaytadan yuboring.",
        "dnc.cancelled": "Bekor qilindi — hech narsa yozilmadi.",
        # --- /vazn -------------------------------------------------------------
        "vazn.title": "⚖️ Ball vaznlari — {version}-versiya",
        "vazn.line": "{key}: {points} ({group}, chegara {cap})",
        "vazn.how": "Oʻzgartirish: /vazn &lt;kalit&gt; &lt;ball&gt;",
        "vazn.done": "⚖️ {key}: {old} → {new} · {version}-versiya",
        "vazn.unknown": "«{key}» kaliti faol versiyada yoʻq. Roʻyxat: /vazn",
        "vazn.bad_points": "Ball butun son boʻlishi kerak.",
        "vazn.refused": "Rad etildi: {key} uchun ball {range} oraligʻida boʻlishi kerak.",
        "vazn.range_positive": "1–{cap}",
        "vazn.range_negative": "−100…−1",
        "vazn.range_gate": "0",
        "vazn.invalid": "Rad etildi: {problems}",
        # --- /push tekshir -----------------------------------------------------
        "push.how": "/push tekshir &lt;external_id&gt; — masalan cs:…:202610",
        "push.sent": "✅ Qayta yuborildi — {attempt}-urinish, ERP qabul qildi.",
        "push.unknown": "⏳ Qayta yuborildi — {attempt}-urinish, ERP javobi nomaʼlum.",
        "push.failed": "⚠️ Qayta yuborilmadi — ERP javob bermadi ({error}).",
        "push.refused": "Qayta yuborilmadi: {why}",
        "push.cancelled": "Bekor qilindi — qayta yuborilmadi.",
        "push.no_erp": "CRM sozlanmagan (CRM_AGENT_KEY) — qayta yuborib boʻlmaydi.",
        # --- /stir -------------------------------------------------------------
        "stir.how": "/stir &lt;nomzod_id&gt; &lt;9 raqamli STIR&gt;",
        "stir.bad_id": "Nomzod identifikatori notoʻgʻri.",
        "stir.bad_stir": "STIR 9 raqam boʻlishi kerak (2–6 bilan boshlanadi).",
        "stir.no_candidate": "Bunday nomzod topilmadi.",
        "stir.written": "✅ STIR {stir} yozildi: {name}. Reestr qayta tekshiriladi.",
        "stir.same": "{name} da bu STIR allaqachon bor: {stir}.",
        "stir.held": (
            "Bu STIR boshqa kompaniyada: {other}. Birlashtirish kerak boʻlsa — "
            "kartadagi 🔀."
        ),
        "stir.has_other": "{name} da boshqa STIR bor: {other}. Oʻzgartirilmadi.",
        # --- the process (bot/main.py; printed, then the process ends) ----------
        "start.no_token": "ASSISTANT_BOT_TOKEN boʻsh — bot ishga tushmaydi.",
        "start.no_owner": (
            "OWNER_TELEGRAM_ID boʻsh — hech kimga cheklanmagan botni ishga tushirmayman."
        ),
        "start.no_telegram": "Telegramga 5 urinishda ulanib boʻlmadi.",
    }
)

# The command menu (``set_my_commands``): the owner's, and the managers' own.
COMMAND_MENU: Final[tuple[tuple[str, str], ...]] = (
    ("lidlar", "Koʻrib chiqiladigan kartalar"),
    ("manbalar", "Manbalar"),
    ("chats", "Telegram chatlar"),
    ("holat", "Agent holati"),
    ("xarajat", "Xarajat"),
    ("toxtat", "Hammasini toʻxtatish"),
    ("yoq", "Davom ettirish"),
    ("import", "Mijozlar faylini yuklash"),
    ("unut", "Kompaniyani oʻchirish"),
    ("qidir", "Kompaniyani qidirish"),
    ("dnc", "Yozilmasin roʻyxatiga qoʻshish"),
    ("vazn", "Ball vaznlari"),
    ("push", "Pushni tekshirish"),
    ("stir", "STIRni qoʻlda yozish"),
    ("yordam", "Buyruqlar roʻyxati"),
)
MANAGER_MENU: Final[tuple[tuple[str, str], ...]] = (
    ("qidir", "Kompaniyani qidirish"),
    ("tel", "Mijoz bergan raqamni yozish"),
    ("yordam", "Buyruqlar roʻyxati"),
)


def t(name: str, /, **values: object) -> str:
    """The text under ``name``, its ``{placeholders}`` filled."""
    text = UZ[name]
    return text.format(**values) if values else text


__all__ = ["COMMAND_MENU", "MANAGER_MENU", "UZ", "t"]
