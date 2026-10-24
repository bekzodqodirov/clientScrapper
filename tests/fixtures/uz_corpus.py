"""Owner-facing Uzbek text samples (07 §1.0, §1.11). Synthetic only.

``tests/test_uz_text.py`` holds every rule of 07 §1.11 to these samples:
each GOOD text passes all of them, each BAD text trips exactly the rules it
names, so a rule that stopped seeing its shape turns the corpus red.

Sections later tasks fill:
  * REDLINE_BAD / REDLINE_GOOD — DraftOpener drafts for tests/test_redlines.py
    (T-30, 04 §4.5: ≥ 30 bad, ≥ 10 good).
  * PLAYBOOK_TEMPLATES — templates A–E of docs/sotuv-playbook.md §3 (T-30).
Those are prospect-facing and follow the playbook's own spelling; the rules
here are for what the owner and the managers read (cards, notes, digest).
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

# T-30 (04 §4.5).
REDLINE_BAD: tuple[tuple[str, set[str]], ...] = ()
REDLINE_GOOD: tuple[str, ...] = ()
PLAYBOOK_TEMPLATES: dict[str, str] = {}
