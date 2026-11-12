# VENDORED_FROM dailyJournal@6b1f67f miya/tools/userbot_login.py
"""One-time Telethon login for the reader account (``make userbot-login``).

Prints a session string for ``TELETHON_SESSION``. Run it interactively — it
asks for the phone number of the **dedicated reader account** (never a
manager's or the owner's, 06 §2), the login code and the 2FA password.

The session string is a **full credential** for that account. It is printed
to the operator's terminal once and nowhere else: never written to a file,
never logged (this tool installs no log handler, and the string never passes
through ``logging``). Put it in the server ``.env``, never in the repository,
and treat a leak like a stolen password: Telegram → Settings → Devices →
terminate the session, then log in again.

Nothing is read or sent: the tool asks Telegram for a login code, signs in,
prints the string and the account's first name, and disconnects. The auth
calls it makes (``send_code_request``, ``sign_in``, and two raw requests:
``ResendCodeRequest`` in ``_resend_code`` and, for a code sent to the login
e-mail, ``SignInRequest`` in ``_sign_in_by_email``) put nothing in any chat;
``tests/test_no_telegram_writes.py`` allows those two raw requests in those
two functions and nowhere else.

The owner runs this himself, so the words are his (Uzbek), and every step is
the tool's own rather than Telethon's ``start()``:

* The number must carry its country code — a leading «+», a leading «00», or
  digits starting with 998 (taken as +998…); a «+» with nine digits that
  start like an Uzbek operator code («+90 123 45 67») is the same slip and is
  refused too, and a number of another country is taken only after «ha». A
  bare «901234567» is read by Telegram as +90… (Turkey). That is one
  plausible cause of the owner's first install (2026-11-12, the code
  «arrived neither by SMS nor in the app»), **not a verified one**: +90 and
  seven digits is too short for a Turkish number, so Telegram more likely
  refused it outright. The likelier causes are a code delivered to the
  «Telegram» chat of the app (perhaps under another account there) and SMS
  that Telegram withholds from third-party clients (core.telegram.org/api/auth
  — «only the official applications can receive a login/signup code via
  SMS/call» in some conditions). The masked echo and the «where it went» line
  are what settle it on the next run. A bot token is refused too: Telethon's
  own prompt says «(or bot token)», and a token there signs the reader in as
  the bot.
* Where the code went is said from ``SentCode.type`` (the app's «Telegram»
  chat, SMS, a call, the login e-mail, Fragment), with what to do when it
  does not come. «qayta» is offered only when ``SentCode.next_type`` says
  there is another way, and only after ``SentCode.timeout`` seconds; a resend
  Telegram refuses keeps the code already sent. A demand to set a login
  e-mail first stops the tool with what to do in the official app.
* A code sent to the login e-mail goes to Telegram in auth.signIn's
  ``email_verification`` field: Telethon's ``sign_in`` only fills
  ``phone_code``, which auth.signIn documents as the code from the phone.
* At most three wrong codes and three wrong 2FA passwords, then exit 1; an
  expired code says that «qayta» asks for a new one.
* Every refusal from Telegram (a flood wait, a banned or unknown number, a
  wrong api id, no way left to send the code, a lost connection) ends in one
  Uzbek sentence and exit 1, never a traceback; anything else names its
  class, with the error's text on a second line for the developer. The
  client is disconnected on every way out, and one Ctrl+C stops the tool.

The connect and every request are bounded: without a route to Telegram the
login used to wait in silence (the first docker dry run, 2026-11-09). A
sign-in that is answered after its bound may still have logged the account
in; the tool then asks Telegram once and prints the session if it did, so no
authorised session is left behind unprinted.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import math
import re
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from clientscrapper.config import settings

SESSION_KEY = "TELETHON_SESSION"
# What an isinstance() check takes: one exception class or several.
type ErrorKinds = type[BaseException] | tuple[type[BaseException], ...]
CONNECT_TIMEOUT_SECONDS: Final = 60
# Each request after the connect: a route that took the connection and then
# stopped answering would otherwise leave the owner at a frozen prompt. Longer
# than Telethon's flood_sleep_threshold (60 s), which it sleeps through on its
# own, so a short flood wait is waited out rather than read as a lost line.
REQUEST_TIMEOUT_SECONDS: Final = 120
# Telethon's sign_in is up to four requests (get_me, auth.signIn, then
# GetState and GetDifference in its _on_login), the 2FA one five (get_me,
# GetPassword, CheckPassword and the same two); each may sleep through a
# flood wait of up to 60 s on its own.
SIGN_IN_TIMEOUT_SECONDS: Final = 300
MAX_WRONG_CODES: Final = 3
MAX_WRONG_PASSWORDS: Final = 3
RESEND_WORD: Final = "qayta"
YES_WORD: Final = "ha"

# Spaces, dashes, dots and brackets between the digits are how people write
# a number; they go. What stays must be «+» and digits, or digits alone —
# ASCII digits only: Telethon passes any other digit on unchanged.
SEPARATORS: Final = re.compile(r"[\s().\-]")
WITH_PLUS: Final = re.compile(r"\+[0-9]{7,15}")
DIGITS: Final = re.compile(r"[0-9]+")
INTERNATIONAL_PREFIX: Final = "00"
UZ_CODE: Final = "998"
# +998, a two-digit operator code and seven digits.
UZ_DIGITS: Final = 12
UZ_LOCAL_DIGITS: Final = 9
# The operator codes an Uzbek number starts with after +998: «+90 123 45 67»
# is the local number with a «+» put on, not a number of the country +90.
UZ_OPERATORS: Final = frozenset(
    {"20", "33", "50", "55", "77", "88", "90", "91", "93", "94", "95", "97", "98", "99"}
)
# A login code: digits, perhaps typed with spaces or dashes. SmsWord /
# SmsPhrase codes are words and are taken as typed.
CODE: Final = re.compile(r"[0-9]+")
CODE_SEPARATORS: Final = re.compile(r"[\s\-]")
# Runs of digits in an error's text, masked before it is shown.
LONG_DIGITS: Final = re.compile(r"[0-9]{7,}")
DETAIL_MAX: Final = 200

PHONE_PROMPT: Final = (
    "Oʻquvchi akkauntning telefon raqami, +998 bilan (masalan +998901234567): "
)
NOT_A_PHONE: Final = (
    "Bu telefon raqami emas. Bot tokeni ham emas — bu yerga oʻquvchi akkaunt "
    "SIM kartasining raqamini yoz, masalan +998901234567."
)
NO_COUNTRY_CODE: Final = (
    "Raqamni +998 bilan yoz, masalan +998901234567: +998 siz yozilsa, Telegram "
    "uni boshqa davlat raqami deb oʻqiydi (masalan «90…» — Turkiya, +90)."
)
UZ_LENGTH: Final = (
    "Oʻzbekiston raqamida +998 dan keyin 9 ta raqam boʻladi (masalan "
    "+998901234567) — qaytadan, toʻliq yoz."
)
FOREIGN_CONFIRM: Final = (
    "Diqqat: {masked} +998 bilan boshlanmaydi — bu Oʻzbekiston raqami emas. "
    "Oʻquvchi akkaunt SIM kartasi boshqa davlatniki boʻlsa «ha» deb yoz, aks "
    "holda «yoʻq»: "
)
FOREIGN_REFUSED: Final = "Unda raqamni +998 bilan qaytadan yoz."
SENDING_CODE: Final = "Kod soʻralmoqda: {masked}"
CODE_PROMPT: Final = "Telegram yuborgan kod: "
CODE_PROMPT_RESEND: Final = "Telegram yuborgan kod (kelmasa: qayta): "
WORD_PROMPT: Final = "SMS dagi soʻz yoki ibora: "
WORD_PROMPT_RESEND: Final = "SMS dagi soʻz yoki ibora (kelmasa: qayta): "
NOT_A_CODE: Final = "Kod — faqat raqamlar (masalan 12345)."
NOT_A_WORD: Final = "SMS dagi soʻz yoki iborani yoz."
WRONG_CODE: Final = "Kod notoʻgʻri — qaytadan yoz."
TOO_MANY_CODES: Final = (
    "Kod {count} marta notoʻgʻri yozildi — toʻxtatildi. Kod qayerga kelganini "
    "yuqoridagi qatordan oʻqib, qaytadan: make userbot-login"
)
CODE_EXPIRED: Final = (
    "Kodning muddati tugagan (biror chatga yuborilgan kodni ham Telegram bekor "
    "qiladi). Yangi kod uchun: qayta"
)
RESENDING: Final = "Kod qayta soʻralmoqda…"
NO_OTHER_WAY: Final = (
    "Telegram kodni boshqa yoʻl bilan qayta yubormaydi — yuborilgan kodni yoz "
    "(qayerga kelgani yuqorida yozilgan)."
)
RESEND_TOO_SOON: Final = (
    "Qayta soʻrashga hali erta: yana {seconds} soniya kut — kod shu orada kelishi "
    "mumkin."
)
RESEND_UNAVAILABLE: Final = (
    "Telegram kodni yana yubormaydi (hamma yoʻllar ishlatilgan yoki soʻrov koʻp). "
    "Avval kelgan kodni yoz; umuman kelmagan boʻlsa, Ctrl+C bos va bir necha "
    "soat kutib, qaytadan: make userbot-login"
)
RESEND_FLOOD: Final = (
    "Telegram qayta yuborishdan oldin {wait} kutishni soʻradi — avval kelgan "
    "kodni yoz yoki shuncha kutib «qayta» deb yoz."
)
PASSWORD_PROMPT: Final = "Ikki bosqichli tekshiruv (2FA) paroli: "
EMPTY_PASSWORD: Final = "Parol boʻsh — 2FA parolini yoz."
WRONG_PASSWORD: Final = "2FA paroli notoʻgʻri — qaytadan yoz."
TOO_MANY_PASSWORDS: Final = (
    "2FA paroli {count} marta notoʻgʻri — toʻxtatildi. Parolni eslab (yoki "
    "rasmiy Telegram ilovasida tiklab), qaytadan: make userbot-login"
)
LATE_SIGN_IN: Final = "Telegram javobi kechikdi, lekin kirish bajarilgan."

# Where the code went, by the class of SentCode.type in the pinned Telethon
# (telethon.tl.types.auth; test_userbot_login checks every name is there).
SENT_APP: Final = (
    "Kod SMS bilan emas, Telegram ilovasiga keldi: shu raqam bilan kirilgan "
    "telefon yoki kompyuterdagi Telegramda «Telegram» nomli chatni och."
)
APP_ACCOUNTS: Final = (
    "Ilovada bir nechta akkaunt boʻlsa, oʻquvchi akkauntga oʻt — kod oʻsha "
    "akkauntning «Telegram» chatida."
)
DO_NOT_SHARE: Final = (
    "Kodni hech qayerga — Telegram chatiga ham — yuborma, bu yerga qoʻlda yoz: "
    "chatga yuborilgan kodni Telegram bekor qiladi."
)
SENT_SMS: Final = (
    "Kod SMS bilan yuborildi: shu SIM karta turgan telefonning SMS xabarlarini qara."
)
SMS_MAY_NOT_COME: Final = (
    "SMS kelmasligi mumkin: Telegram SMS ni koʻpincha faqat rasmiy ilovalarga "
    "yuboradi."
)
SENT_FIREBASE_SMS: Final = (
    "Telegram bu kodni SMS bilan faqat rasmiy ilova orqali yuboradi — bu yerga "
    "SMS kelmaydi."
)
OFFICIAL_APP_FIRST: Final = (
    "Kelmasa: oʻquvchi akkauntga telefondagi rasmiy Telegram ilovasida kirib "
    "qoʻy, shu yerda Ctrl+C bos va qaytadan: make userbot-login — kod oʻsha "
    "ilovaning «Telegram» chatiga keladi."
)
SENT_SMS_WORD: Final = (
    "Kod SMS bilan soʻz koʻrinishida yuborildi{start}: SMS dagi soʻzni yoz."
)
SENT_SMS_PHRASE: Final = (
    "Kod SMS bilan ibora koʻrinishida yuborildi{start}: SMS dagi iborani yoz."
)
STARTS_WITH: Final = " («{beginning}…» bilan boshlanadi)"
SENT_CALL: Final = (
    "Kod telefon qoʻngʻirogʻi bilan aytiladi: shu SIM karta turgan telefonga "
    "qoʻngʻiroq keladi."
)
SENT_FLASH_CALL: Final = (
    "Shu SIM karta turgan telefonga qisqa qoʻngʻiroq keladi: kod — qoʻngʻiroq "
    "qilgan raqamning oʻzi (toʻliq, + siz)."
)
SENT_MISSED_CALL: Final = (
    "Shu SIM karta turgan telefonga qoʻngʻiroq keladi (koʻtarma): kod — "
    "qoʻngʻiroq qilgan raqamning oxirgi {length} ta raqami ({prefix}… bilan "
    "boshlanadi)."
)
SENT_EMAIL: Final = (
    "Kod akkauntning kirish e-pochtasiga yuborildi: {pattern} (Spam papkasini ham qara)."
)
SETUP_EMAIL_REQUIRED: Final = (
    "Telegram kod yuborishdan oldin bu akkauntga kirish e-pochtasi (login email) "
    "qoʻyishni talab qilyapti. Oʻquvchi akkauntni rasmiy Telegram ilovasida och "
    "va e-pochtani qoʻy (yoki shu raqam bilan rasmiy ilovaga bir marta kir — "
    "ilova e-pochtani oʻzi soʻraydi), keyin qaytadan: make userbot-login"
)
SENT_FRAGMENT: Final = (
    "Bu raqam Fragment raqami (anonim): kod fragment.com dagi hisobga keladi — {url}"
)
SENT_OTHER: Final = (
    "Kod yuborildi ({name}) — qayerga ekani nomaʼlum: Telegram ilovasini, SMS ni "
    "va e-pochtani qara."
)
RESEND_HINT: Final = (
    "Kod kelmasa, {after}«qayta» deb yoz — Telegram uni {way} qayta yuboradi."
)
RESEND_AFTER: Final = "{seconds} soniyadan keyin "
# SentCode.next_type: the way «qayta» will use.
NEXT_WAYS: Final = {
    "CodeTypeSms": "SMS bilan",
    "CodeTypeCall": "telefon qoʻngʻirogʻi bilan",
    "CodeTypeFlashCall": "qisqa qoʻngʻiroq bilan",
    "CodeTypeMissedCall": "qoʻngʻiroq bilan",
    "CodeTypeFragmentSms": "Fragment orqali",
}
NEXT_WAY_OTHER: Final = "{name} yoʻli bilan"
# The code is a word or a phrase, not digits.
WORD_CODES: Final = frozenset({"SentCodeTypeSmsWord", "SentCodeTypeSmsPhrase"})
# Codes Telegram sends as an SMS: one that may not reach a third-party client.
SMS_CODES: Final = frozenset({"SentCodeTypeSms", *WORD_CODES})
EMAIL_CODE: Final = "SentCodeTypeEmailCode"
FLASH_CALL: Final = "SentCodeTypeFlashCall"

# Telegram's refusals: one sentence each, then exit 1 (refusal()).
FLOOD_WAIT: Final = (
    "Telegram koʻp urinish uchun kutishni soʻradi: {wait}. Shuncha kutib, keyin "
    "qaytadan: make userbot-login (oldinroq urinish kutishni uzaytiradi)."
)
PHONE_FLOOD: Final = (
    "Bu raqamga juda koʻp kod soʻraldi — Telegram vaqtincha bermayapti. Bir necha "
    "soat (ehtimol bir kun) kutib, qaytadan: make userbot-login"
)
PASSWORD_FLOOD: Final = (
    "2FA paroli juda koʻp marta notoʻgʻri yozildi — Telegram vaqtincha "
    "toʻxtatgan. Bir necha soat kutib, qaytadan: make userbot-login"
)
PHONE_INVALID: Final = (
    "Telegram bu raqamni qabul qilmadi (notoʻgʻri raqam). Raqamni +998 bilan, "
    "toʻliq yoz (masalan +998901234567) va qaytadan: make userbot-login"
)
PHONE_BANNED: Final = (
    "Bu raqam Telegramda bloklangan — oʻquvchi akkaunt uchun boshqa (zaxira) SIM "
    "kerak; keyin qaytadan: make userbot-login"
)
NO_ACCOUNT: Final = (
    "Bu raqamda Telegram akkaunti yoʻq. Avval shu SIM bilan rasmiy Telegram "
    "ilovasida akkaunt och (bu vosita roʻyxatdan oʻtkazmaydi), keyin qaytadan: "
    "make userbot-login"
)
API_ID_INVALID: Final = (
    ".env dagi TELETHON_API_ID yoki TELETHON_API_HASH notoʻgʻri — my.telegram.org "
    "→ API development tools dan qayta koʻchirib yoz, keyin qaytadan: "
    "make userbot-login"
)
API_ID_PUBLISHED: Final = (
    "Bu TELETHON_API_ID ochiq joyda eʼlon qilingan ilovaniki — Telegram u bilan "
    "kirishga ruxsat bermaydi. my.telegram.org → API development tools dan "
    "oʻquvchi akkauntning oʻz API_ID va API_HASH ni .env ga yoz, keyin qaytadan: "
    "make userbot-login"
)
SEND_CODE_UNAVAILABLE: Final = (
    "Telegram kodni yuborishning hamma yoʻllarini ishlatib boʻldi. Bir necha soat "
    "kutib, qaytadan: make userbot-login"
)
UPDATE_APP: Final = (
    "Telegram bu dastur versiyasi bilan kirishga ruxsat bermadi — shu xabarni "
    "dasturchiga yubor (Telethon kutubxonasini yangilash kerak)."
)
CONNECTION_LOST: Final = (
    "Telegram bilan aloqa uzildi — serverning internetini tekshir va qaytadan: "
    "make userbot-login"
)
STOPPED: Final = "Toʻxtatildi — kirish tugamadi. Qaytadan: make userbot-login"
UNEXPECTED: Final = (
    "Telegram kutilmagan javob berdi ({name}). Bir necha daqiqadan keyin "
    "qaytadan: make userbot-login; yana shunday boʻlsa, shu xabarni dasturchiga yubor."
)
UNEXPECTED_LOCAL: Final = (
    "Kutilmagan xato ({name}). Bir necha daqiqadan keyin qaytadan: "
    "make userbot-login; yana shunday boʻlsa, shu xabarni dasturchiga yubor."
)
DETAIL: Final = "({detail})"
MASKED_DIGITS: Final = "<raqam>"
SECONDS: Final = "{n} soniya"
MINUTES: Final = "{n} daqiqa"
HOURS: Final = "{n} soat"

CONNECTING: Final = "Telegramga ulanmoqda…"
NO_CONNECTION: Final = (
    "Telegramga {seconds} soniyada ulanib boʻlmadi — serverning internetini "
    "tekshir va qaytadan: make userbot-login"
)
MISSING_APP_KEYS: Final = (
    "Avval .env ga TELETHON_API_ID va TELETHON_API_HASH ni yoz: my.telegram.org → "
    "API development tools (oʻquvchi akkaunt raqami bilan kir; MIYA ilovasidan "
    "alohida ilova)."
)

# The connection, not Telegram: a refused or reset socket, a bound that ran
# out, or a route that answered with something other than MTProto (an HTTP
# proxy or a block page ends the auth-key read with an IncompleteReadError —
# the docker dry run, 06 §2 Runtime).
LINE_ERRORS: Final = (ConnectionError, OSError, TimeoutError, asyncio.IncompleteReadError)
# The clock «qayta» is timed by; the tests move it.
_now: Callable[[], float] = time.monotonic


class Stop(Exception):
    """The login ends here; the message is the sentence the owner reads."""


@dataclass
class _Progress:
    """What the login has done: after a sign-in request that was sent and got
    no answer, the account may be logged in all the same."""

    sign_in_sent: bool = False


def normalise_phone(text: str) -> tuple[str | None, str]:
    """The number to send the code to, or None and the sentence that says why
    not. Only a number with its country code is taken."""
    bare = SEPARATORS.sub("", text)
    if bare.startswith(INTERNATIONAL_PREFIX) and DIGITS.fullmatch(bare):
        bare = "+" + bare[len(INTERNATIONAL_PREFIX) :]
    if WITH_PLUS.fullmatch(bare):
        phone = bare
    elif DIGITS.fullmatch(bare) and 7 <= len(bare) <= 15:
        if not bare.startswith(UZ_CODE):
            return None, NO_COUNTRY_CODE
        phone = "+" + bare
    else:
        return None, NOT_A_PHONE
    digits = phone[1:]
    if digits.startswith(UZ_CODE):
        if len(digits) != UZ_DIGITS:
            return None, UZ_LENGTH
    elif len(digits) == UZ_LOCAL_DIGITS and digits[:2] in UZ_OPERATORS:
        return None, NO_COUNTRY_CODE
    return phone, ""


def is_uzbek(phone: str) -> bool:
    return phone.startswith("+" + UZ_CODE)


def ask_phone(read: Callable[[str], str] = input) -> str:
    """The reader account's number, with its country code; asked again until
    it is one. A number of another country is taken only after «ha»."""
    while True:
        phone, why = normalise_phone(read(PHONE_PROMPT).strip())
        if not phone:
            print(why)
            continue
        if is_uzbek(phone):
            return phone
        answer = read(FOREIGN_CONFIRM.format(masked=mask_phone(phone)))
        if answer.strip().lower() == YES_WORD:
            return phone
        print(FOREIGN_REFUSED)


def mask_phone(phone: str) -> str:
    """The country and operator code and the last two digits: enough to see
    a wrong country, not the whole number on a shared screenshot."""
    head = max(2, min(6, len(phone) - 6))
    return phone[:head] + "*" * (len(phone) - head - 2) + phone[-2:]


def _class_name(value: object) -> str:
    return type(value).__name__


def delivery(sent: Any) -> list[str]:
    """Where Telegram says it sent the code, what to do when it does not come,
    and how «qayta» re-sends it. Raises Stop when Telegram wants a login
    e-mail set first."""
    kind = sent.type
    name = _class_name(kind)
    lines: list[str]
    if name == "SentCodeTypeApp":
        lines = [SENT_APP, APP_ACCOUNTS]
    elif name == "SentCodeTypeSms":
        lines = [SENT_SMS, SMS_MAY_NOT_COME, OFFICIAL_APP_FIRST]
    elif name == "SentCodeTypeFirebaseSms":
        # Sent only after Play Integrity attestation (auth.requestFirebaseSms),
        # which only official apps can do: no SMS will come here.
        lines = [SENT_FIREBASE_SMS, OFFICIAL_APP_FIRST]
    elif name in WORD_CODES:
        beginning = getattr(kind, "beginning", None)
        start = STARTS_WITH.format(beginning=beginning) if beginning else ""
        text = SENT_SMS_WORD if name == "SentCodeTypeSmsWord" else SENT_SMS_PHRASE
        lines = [text.format(start=start), SMS_MAY_NOT_COME, OFFICIAL_APP_FIRST]
    elif name == "SentCodeTypeCall":
        lines = [SENT_CALL]
    elif name == FLASH_CALL:
        lines = [SENT_FLASH_CALL]
    elif name == "SentCodeTypeMissedCall":
        lines = [
            SENT_MISSED_CALL.format(
                length=getattr(kind, "length", "?"), prefix=getattr(kind, "prefix", "")
            )
        ]
    elif name == EMAIL_CODE:
        lines = [SENT_EMAIL.format(pattern=getattr(kind, "email_pattern", ""))]
    elif name == "SentCodeTypeSetUpEmailRequired":
        raise Stop(SETUP_EMAIL_REQUIRED)
    elif name == "SentCodeTypeFragmentSms":
        lines = [SENT_FRAGMENT.format(url=getattr(kind, "url", "fragment.com"))]
    else:
        lines = [SENT_OTHER.format(name=name)]
    next_type = getattr(sent, "next_type", None)
    if next_type is not None:
        next_name = _class_name(next_type)
        way = NEXT_WAYS.get(next_name) or NEXT_WAY_OTHER.format(name=next_name)
        timeout = getattr(sent, "timeout", None)
        after = RESEND_AFTER.format(seconds=timeout) if timeout else ""
        lines.append(RESEND_HINT.format(after=after, way=way))
        if next_name == "CodeTypeSms" and name != "SentCodeTypeApp":
            # «qayta» promises an SMS that a third-party client may never get
            # (an app code means the account is in an app already: look there).
            caveat = (SMS_MAY_NOT_COME, OFFICIAL_APP_FIRST)
            lines += [line for line in caveat if line not in lines]
    return lines


def _wait_words(seconds: int) -> str:
    if seconds < 120:
        return SECONDS.format(n=seconds)
    minutes = math.ceil(seconds / 60)
    if minutes < 120:
        return MINUTES.format(n=minutes)
    return HOURS.format(n=math.ceil(minutes / 60))


def _detail(exc: BaseException) -> str:
    """The error's own text, for the developer the owner forwards it to:
    clipped, one line, long digit runs (a phone number) masked. On these
    paths it holds no session and no password (Telethon's RPC errors name
    request classes, not their values)."""
    text = " ".join(str(exc).split())
    return LONG_DIGITS.sub(MASKED_DIGITS, text)[:DETAIL_MAX]


def refusal(exc: BaseException) -> str:
    """What the owner reads for ``exc``: one sentence for Telegram's refusal
    or a lost connection; anything else by its class name, with the error's
    text on a second line."""
    from telethon import errors

    if isinstance(exc, Stop):
        return str(exc)
    if isinstance(exc, errors.FloodWaitError):
        return FLOOD_WAIT.format(wait=_wait_words(exc.seconds))
    table: tuple[tuple[ErrorKinds, str], ...] = (
        (errors.PhoneNumberFloodError, PHONE_FLOOD),
        (errors.PhonePasswordFloodError, PASSWORD_FLOOD),
        (errors.PhoneNumberInvalidError, PHONE_INVALID),
        (errors.PhoneNumberBannedError, PHONE_BANNED),
        # sendCode answers PHONE_NUMBER_APP_SIGNUP_FORBIDDEN for a number with
        # no account; sign_in raises PhoneNumberUnoccupied. Never a sign-up.
        (
            (
                errors.PhoneNumberUnoccupiedError,
                errors.PhoneNumberAppSignupForbiddenError,
            ),
            NO_ACCOUNT,
        ),
        (errors.ApiIdInvalidError, API_ID_INVALID),
        (errors.ApiIdPublishedFloodError, API_ID_PUBLISHED),
        (errors.SendCodeUnavailableError, SEND_CODE_UNAVAILABLE),
        # UPDATE_APP_TO_LOGIN: waiting does not help, a newer client does.
        (errors.UpdateAppToLoginError, UPDATE_APP),
        # IncompleteReadError is an EOFError: the connection, not the keyboard,
        # so it is matched before EOFError below.
        (LINE_ERRORS, CONNECTION_LOST),
        (EOFError, STOPPED),
    )
    for kinds, sentence in table:
        if isinstance(exc, kinds):
            return sentence
    name = _class_name(exc)
    if isinstance(exc, errors.RPCError):
        code = exc.message
        named = f"{name}: {code}" if code and code != name else name
        head = UNEXPECTED.format(name=named)
    else:
        # A local error (Telethon's own AttributeError on a result it does not
        # handle, a ValueError) is not Telegram's answer.
        head = UNEXPECTED_LOCAL.format(name=name)
    detail = _detail(exc)
    return f"{head}\n{DETAIL.format(detail=detail)}" if detail else head


async def _bounded[T](call: Awaitable[T], seconds: float | None = None) -> T:
    return await asyncio.wait_for(call, seconds or REQUEST_TIMEOUT_SECONDS)


async def _resend_code(client: Any, phone: str, phone_code_hash: str) -> Any:
    """auth.resendCode: the code again, by SentCode.next_type's way. One of the
    two raw requests this tool makes (test_no_telegram_writes.RAW_ALLOWED): an
    auth call that puts nothing in any chat."""
    from telethon.tl.functions.auth import ResendCodeRequest

    # The number as Telethon sends it (utils.parse_phone drops the «+»):
    # Telegram ties the hash to it.
    digits = phone.lstrip("+")
    return await _bounded(client(ResendCodeRequest(digits, phone_code_hash)))


async def _sign_in_by_email(
    client: Any, phone: str, code: str, phone_code_hash: str
) -> None:
    """auth.signIn with a code sent to the login e-mail, which goes in
    ``email_verification`` (core.telegram.org/method/auth.signIn): Telethon's
    sign_in only fills ``phone_code``, documented as the code from the phone.
    The other raw request this tool makes (RAW_ALLOWED); it puts nothing in
    any chat. The session string needs nothing Telethon's _on_login does: the
    auth key is authorised on Telegram's side."""
    from telethon import errors
    from telethon.tl.functions.auth import SignInRequest
    from telethon.tl.types import EmailVerificationCode
    from telethon.tl.types.auth import AuthorizationSignUpRequired

    request = SignInRequest(
        phone.lstrip("+"),
        phone_code_hash,
        email_verification=EmailVerificationCode(code),
    )
    result = await _bounded(client(request))
    if isinstance(result, AuthorizationSignUpRequired):
        # As Telethon's sign_in does: a number with no account, never a sign-up.
        raise errors.PhoneNumberUnoccupiedError(request=request)


def _code_from(answer: str, kind: str) -> str | None:
    """The code as Telegram wants it, or None when ``answer`` is not one."""
    if kind in WORD_CODES:
        return answer or None
    if kind == FLASH_CALL:
        # The code is the calling number itself, typed perhaps with «+».
        code = SEPARATORS.sub("", answer).lstrip("+")
    else:
        code = CODE_SEPARATORS.sub("", answer)
    return code if CODE.fullmatch(code) else None


def _prompt(kind: str, offer_resend: bool) -> str:
    if kind in WORD_CODES:
        return WORD_PROMPT_RESEND if offer_resend else WORD_PROMPT
    return CODE_PROMPT_RESEND if offer_resend else CODE_PROMPT


@dataclass
class _Code:
    """The code Telegram last sent, and whether and when «qayta» may ask again."""

    sent: Any
    resend_from: float
    can_resend: bool
    expired: bool = False

    @classmethod
    def of(cls, sent: Any) -> _Code:
        timeout = getattr(sent, "timeout", None) or 0
        return cls(
            sent=sent,
            resend_from=_now() + timeout,
            can_resend=getattr(sent, "next_type", None) is not None,
        )

    @property
    def kind(self) -> str:
        return _class_name(self.sent.type)

    @property
    def offer_resend(self) -> bool:
        # After an expired code «qayta» is a fresh auth.sendCode, always there.
        return self.expired or self.can_resend


async def _send_again(client: Any, phone: str, code: _Code) -> _Code:
    """«qayta»: a fresh code after an expired one, else auth.resendCode when
    Telegram offered another way and its wait is over. A resend Telegram
    refuses keeps the code already sent (it may still come, or have come)."""
    from telethon import errors

    if code.expired:
        print(RESENDING, flush=True)
        return _Code.of(await _bounded(client.send_code_request(phone)))
    if not code.can_resend:
        print(NO_OTHER_WAY)
        return code
    wait = math.ceil(code.resend_from - _now())
    if wait > 0:
        print(RESEND_TOO_SOON.format(seconds=wait))
        return code
    print(RESENDING, flush=True)
    try:
        sent = await _resend_code(client, phone, code.sent.phone_code_hash)
    except errors.PhoneCodeExpiredError:
        # A resend cannot revive an expired hash; a new code can. Telethon
        # drops its own copy of the hash only in sign_in, so this is a fresh
        # auth.sendCode.
        sent = await _bounded(client.send_code_request(phone))
    except (errors.SendCodeUnavailableError, errors.PhoneNumberFloodError):
        code.can_resend = False
        print(RESEND_UNAVAILABLE)
        return code
    except errors.FloodWaitError as exc:
        code.resend_from = _now() + exc.seconds
        print(RESEND_FLOOD.format(wait=_wait_words(exc.seconds)))
        return code
    return _Code.of(sent)


async def _ask_code(
    client: Any, phone: str, sent: Any, read: Callable[[str], str], progress: _Progress
) -> bool:
    """Sign in with the code; True when signed in, False on the third wrong
    code. Raises SessionPasswordNeededError for the 2FA step."""
    from telethon import errors

    code = _Code.of(sent)
    wrong = 0
    while True:
        answer = read(_prompt(code.kind, code.offer_resend)).strip()
        if answer.lower() == RESEND_WORD:
            again = await _send_again(client, phone, code)
            if again is not code:
                for line in delivery(again.sent):
                    print(line)
                code = again
            continue
        typed = _code_from(answer, code.kind)
        if typed is None:
            print(NOT_A_WORD if code.kind in WORD_CODES else NOT_A_CODE)
            continue
        progress.sign_in_sent = True
        try:
            if code.kind == EMAIL_CODE:
                await _sign_in_by_email(client, phone, typed, code.sent.phone_code_hash)
            else:
                await _bounded(
                    client.sign_in(
                        phone, typed, phone_code_hash=code.sent.phone_code_hash
                    ),
                    SIGN_IN_TIMEOUT_SECONDS,
                )
            return True
        # CodeInvalid / EmailVerifyExpired: what an e-mail code may get instead
        # (not documented for auth.signIn; never a «wrong code» left unsaid).
        except (errors.PhoneCodeInvalidError, errors.CodeInvalidError):
            wrong += 1
            if wrong >= MAX_WRONG_CODES:
                print(TOO_MANY_CODES.format(count=wrong), file=sys.stderr)
                return False
            print(WRONG_CODE)
        except (errors.PhoneCodeExpiredError, errors.EmailVerifyExpiredError):
            code.expired = True
            print(CODE_EXPIRED)


async def _ask_password(
    client: Any, read_password: Callable[[str], str], progress: _Progress
) -> bool:
    from telethon.errors import PasswordHashInvalidError

    wrong = 0
    while True:
        password = read_password(PASSWORD_PROMPT)
        if not password:
            # Telethon reads an empty password as «no password» and raises.
            print(EMPTY_PASSWORD)
            continue
        progress.sign_in_sent = True
        try:
            await _bounded(client.sign_in(password=password), SIGN_IN_TIMEOUT_SECONDS)
            return True
        except PasswordHashInvalidError:
            wrong += 1
            if wrong >= MAX_WRONG_PASSWORDS:
                print(TOO_MANY_PASSWORDS.format(count=wrong), file=sys.stderr)
                return False
            print(WRONG_PASSWORD)


async def _sign_in(
    client: Any,
    read: Callable[[str], str],
    read_password: Callable[[str], str],
    progress: _Progress,
) -> bool:
    from telethon.errors import SessionPasswordNeededError

    phone = ask_phone(read)
    print(SENDING_CODE.format(masked=mask_phone(phone)), flush=True)
    sent = await _bounded(client.send_code_request(phone))
    for line in delivery(sent):
        print(line)
    print(DO_NOT_SHARE)
    try:
        return await _ask_code(client, phone, sent, read, progress)
    except SessionPasswordNeededError:
        return await _ask_password(client, read_password, progress)


async def _signed_in_after_all(client: Any) -> bool:
    """A sign-in whose answer never came may still have logged the account
    in; get_me answers None for a key that is not (Telethon catches the 401)."""
    with contextlib.suppress(Exception):
        return bool(await _bounded(client.get_me()))
    return False


def session_line(session_string: str) -> str:
    """The .env line the operator pastes."""
    return f"{SESSION_KEY}={session_string}"


async def _print_session(client: Any) -> None:
    session_string = client.session.save()
    # The credential goes out first, before anything that could fail: logging
    # in mints a new authorised session on the account, and if the string is
    # lost the login cannot be repeated, only redone — leaving an orphan
    # session behind each time.
    print(
        "\nQuyidagi qatorni serverdagi .env faylidagi TELETHON_SESSION= oʻrniga "
        "yoz, keyin: make reload.\nBu qator — akkaunt paroli kabi maxfiy: "
        "hech kimga yuborma, hech qayerga joylama.\n"
    )
    print(session_line(session_string) + "\n", flush=True)
    try:
        me = await _bounded(client.get_me())
        name = getattr(me, "first_name", None) or getattr(me, "id", "?")
        print(f"Kirildi: {name}")
    except Exception as exc:  # cosmetic only — never lose the session over it
        print(f"(kirildi; akkaunt nomini oʻqib boʻlmadi: {type(exc).__name__})")


async def _login(
    client_factory: Any = None,
    *,
    read: Callable[[str], str] = input,
    read_password: Callable[[str], str] = getpass.getpass,
) -> bool:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    factory = client_factory or TelegramClient
    # Driven with an explicit event loop rather than Telethon's synchronous
    # wrapper: that wrapper is installed as a side effect of `import
    # telethon.sync`, an import no linter can tell from an unused one, and
    # without it every client call returns an un-awaited coroutine (MIYA).
    client = factory(
        StringSession(), settings.telethon_api_id, settings.telethon_api_hash
    )
    print(CONNECTING, flush=True)
    try:
        try:
            await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT_SECONDS)
        except LINE_ERRORS:
            print(NO_CONNECTION.format(seconds=CONNECT_TIMEOUT_SECONDS), file=sys.stderr)
            return False
        except Exception as exc:  # whatever connect() raises: words, not a traceback
            print(refusal(exc), file=sys.stderr)
            return False
        # Here and only here a person sits at the terminal to type the phone
        # number, the code and the password.
        progress = _Progress()
        try:
            signed_in = await _sign_in(client, read, read_password, progress)
        except Exception as exc:  # every refusal is a sentence, never a traceback
            late = isinstance(exc, LINE_ERRORS) and progress.sign_in_sent
            if not (late and await _signed_in_after_all(client)):
                print(refusal(exc), file=sys.stderr)
                return False
            print(LATE_SIGN_IN)
            signed_in = True
        if signed_in:
            await _print_session(client)
        return signed_in
    finally:
        # Always, whatever ended the login; a disconnect that fails has
        # nothing left to protect and must not bury the sentence above it.
        with contextlib.suppress(Exception):
            await client.disconnect()


def _interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> int:
    if not (settings.telethon_api_id and settings.telethon_api_hash):
        print(MISSING_APP_KEYS, file=sys.stderr)
        return 1
    # asyncio.run puts its own SIGINT handler in place of the default one,
    # and that handler only cancels the main task: a prompt blocked in
    # input() went on waiting, and only a second Ctrl+C stopped the tool.
    # With a handler of our own asyncio keeps its hands off, and one Ctrl+C
    # raises KeyboardInterrupt wherever the login is.
    previous = signal.signal(signal.SIGINT, _interrupt)
    try:
        return 0 if asyncio.run(_login()) else 1
    except KeyboardInterrupt:
        print("\n" + STOPPED, file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
