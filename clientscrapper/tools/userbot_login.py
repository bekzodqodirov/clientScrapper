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
prints the string and the account's first name, and disconnects. The three
auth calls it makes (``send_code_request``, the raw ``ResendCodeRequest`` and
``sign_in``) put nothing in any chat; ``tests/test_no_telegram_writes.py``
allows the raw one in ``_resend_code`` and nowhere else.

The owner runs this himself, so the words are his (Uzbek), and every step is
the tool's own rather than Telethon's ``start()``:

* The number must carry its country code — a leading «+», or digits starting
  with 998 (taken as +998…). A bare «901234567» is what Telegram reads as
  +90 (Turkey), so the code went to somebody else's number and «arrived
  neither by SMS nor in the app» (the owner's first install, 2026-11-12). A
  bot token is refused too: Telethon's own prompt says «(or bot token)», and
  a token there signs the reader in as the bot. The number is echoed masked
  before the code is asked for, so a wrong country shows on the screen.
* Where the code went is said from ``SentCode.type`` (the app's «Telegram»
  chat, SMS, a call, the login e-mail, Fragment), and by which other way
  «qayta» re-sends it after how many seconds. A demand to set a login e-mail
  first stops the tool with what to do in the official app.
* At most three wrong codes and three wrong 2FA passwords, then exit 1; an
  expired code says that «qayta» asks for a new one.
* Every refusal from Telegram (a flood wait, a banned or unknown number, a
  wrong api id, no way left to send the code, a lost connection) ends in one
  Uzbek sentence and exit 1, never a traceback; the client is disconnected on
  every way out.

The connect and every request are bounded: without a route to Telegram the
login used to wait in silence (the first docker dry run, 2026-11-09).
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import math
import re
import sys
from collections.abc import Awaitable, Callable
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
MAX_WRONG_CODES: Final = 3
MAX_WRONG_PASSWORDS: Final = 3
RESEND_WORD: Final = "qayta"

# Spaces, dashes and brackets between the digits are how people write a
# number; they go. What stays must be «+» and digits, or digits alone.
SEPARATORS: Final = re.compile(r"[\s()\-]")
WITH_PLUS: Final = re.compile(r"\+\d{7,15}")
DIGITS: Final = re.compile(r"\d+")
UZ_CODE: Final = "998"
# +998, a two-digit operator code and seven digits.
UZ_DIGITS: Final = 12
# A login code: digits, perhaps typed with spaces. SmsWord / SmsPhrase codes
# are words and are taken as typed.
CODE: Final = re.compile(r"[0-9]+")

PHONE_PROMPT: Final = (
    "Oʻquvchi akkauntning telefon raqami, +998 bilan (masalan +998901234567): "
)
NOT_A_PHONE: Final = (
    "Bu telefon raqami emas. Bot tokeni ham emas — bu yerga oʻquvchi akkaunt "
    "SIM kartasining raqamini yoz, masalan +998901234567."
)
NO_COUNTRY_CODE: Final = (
    "Raqamni +998 bilan yoz, masalan +998901234567. +998 siz yozilsa, Telegram "
    "«90…» ni Turkiya raqami (+90) deb oʻqiydi va kodni boshqa raqamga yuboradi."
)
UZ_LENGTH: Final = (
    "Oʻzbekiston raqamida +998 dan keyin 9 ta raqam boʻladi (masalan "
    "+998901234567) — qaytadan, toʻliq yoz."
)
SENDING_CODE: Final = "Kod soʻralmoqda: {masked}"
CODE_PROMPT: Final = "Telegram yuborgan kod (kelmasa: qayta): "
WORD_PROMPT: Final = "SMS dagi soʻz yoki ibora (kelmasa: qayta): "
NOT_A_CODE: Final = "Kod — faqat raqamlar (masalan 12345). Yangi kod kerak boʻlsa: qayta"
WRONG_CODE: Final = "Kod notoʻgʻri — qaytadan yoz (yangi kod kerak boʻlsa: qayta)."
TOO_MANY_CODES: Final = (
    "Kod {count} marta notoʻgʻri yozildi — toʻxtatildi. Kod qayerga kelganini "
    "yuqoridagi qatordan oʻqib, qaytadan: make userbot-login"
)
CODE_EXPIRED: Final = "Kodning muddati tugagan. Yangi kod uchun: qayta"
RESENDING: Final = "Kod qayta soʻralmoqda…"
PASSWORD_PROMPT: Final = "Ikki bosqichli tekshiruv (2FA) paroli: "
EMPTY_PASSWORD: Final = "Parol boʻsh — 2FA parolini yoz."
WRONG_PASSWORD: Final = "2FA paroli notoʻgʻri — qaytadan yoz."
TOO_MANY_PASSWORDS: Final = (
    "2FA paroli {count} marta notoʻgʻri — toʻxtatildi. Parolni eslab (yoki "
    "rasmiy Telegram ilovasida tiklab), qaytadan: make userbot-login"
)

# Where the code went, by the class of SentCode.type in the pinned Telethon
# (telethon.tl.types.auth; test_userbot_login checks every name is there).
SENT_APP: Final = (
    "Kod SMS bilan emas, Telegram ilovasiga keldi: shu raqam bilan kirilgan "
    "telefon yoki kompyuterdagi Telegramda «Telegram» nomli chatni och."
)
SENT_SMS: Final = (
    "Kod SMS bilan yuborildi: shu SIM karta turgan telefonning SMS xabarlarini qara."
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
    "qilgan raqamning oxirgi raqamlari."
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
CONNECTION_LOST: Final = (
    "Telegram bilan aloqa uzildi — serverning internetini tekshir va qaytadan: "
    "make userbot-login"
)
STOPPED: Final = "Toʻxtatildi — kirish tugamadi. Qaytadan: make userbot-login"
UNEXPECTED: Final = (
    "Telegram kutilmagan javob berdi ({name}). Bir necha daqiqadan keyin "
    "qaytadan: make userbot-login; yana shunday boʻlsa, shu nomni dasturchiga yubor."
)
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


class Stop(Exception):
    """The login ends here; the message is the sentence the owner reads."""


def normalise_phone(text: str) -> tuple[str | None, str]:
    """The number to send the code to, or None and the sentence that says why
    not. Only a number with its country code is taken."""
    bare = SEPARATORS.sub("", text)
    if WITH_PLUS.fullmatch(bare):
        phone = bare
    elif DIGITS.fullmatch(bare) and 7 <= len(bare) <= 15:
        if not bare.startswith(UZ_CODE):
            return None, NO_COUNTRY_CODE
        phone = "+" + bare
    else:
        return None, NOT_A_PHONE
    if phone.startswith("+" + UZ_CODE) and len(phone) - 1 != UZ_DIGITS:
        return None, UZ_LENGTH
    return phone, ""


def ask_phone(read: Callable[[str], str] = input) -> str:
    """The reader account's number, with its country code; asked again until
    it is one."""
    while True:
        phone, why = normalise_phone(read(PHONE_PROMPT).strip())
        if phone:
            return phone
        print(why)


def mask_phone(phone: str) -> str:
    """The country and operator code and the last two digits: enough to see
    a wrong country, not the whole number on a shared screenshot."""
    head = max(2, min(6, len(phone) - 6))
    return phone[:head] + "*" * (len(phone) - head - 2) + phone[-2:]


def _class_name(value: object) -> str:
    return type(value).__name__


def delivery(sent: Any) -> list[str]:
    """Where Telegram says it sent the code, and how «qayta» re-sends it.
    Raises Stop when Telegram wants a login e-mail set first."""
    kind = sent.type
    name = _class_name(kind)
    lines: list[str]
    if name == "SentCodeTypeApp":
        lines = [SENT_APP]
    elif name in {"SentCodeTypeSms", "SentCodeTypeFirebaseSms"}:
        lines = [SENT_SMS]
    elif name in WORD_CODES:
        beginning = getattr(kind, "beginning", None)
        start = STARTS_WITH.format(beginning=beginning) if beginning else ""
        text = SENT_SMS_WORD if name == "SentCodeTypeSmsWord" else SENT_SMS_PHRASE
        lines = [text.format(start=start)]
    elif name == "SentCodeTypeCall":
        lines = [SENT_CALL]
    elif name == "SentCodeTypeFlashCall":
        lines = [SENT_FLASH_CALL]
    elif name == "SentCodeTypeMissedCall":
        lines = [
            SENT_MISSED_CALL.format(
                length=getattr(kind, "length", "?"), prefix=getattr(kind, "prefix", "")
            )
        ]
    elif name == "SentCodeTypeEmailCode":
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
    return lines


def _wait_words(seconds: int) -> str:
    if seconds < 120:
        return SECONDS.format(n=seconds)
    minutes = math.ceil(seconds / 60)
    if minutes < 120:
        return MINUTES.format(n=minutes)
    return HOURS.format(n=math.ceil(minutes / 60))


def refusal(exc: BaseException) -> str:
    """The one sentence the owner reads for ``exc`` (Telegram's refusal, a
    lost connection, or anything else by its class name)."""
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
        # IncompleteReadError is an EOFError: the connection, not the keyboard,
        # so it is matched before EOFError below.
        (
            (ConnectionError, OSError, TimeoutError, asyncio.IncompleteReadError),
            CONNECTION_LOST,
        ),
        (EOFError, STOPPED),
    )
    for kinds, sentence in table:
        if isinstance(exc, kinds):
            return sentence
    name = _class_name(exc)
    code = getattr(exc, "message", None) if isinstance(exc, errors.RPCError) else None
    return UNEXPECTED.format(name=f"{name}: {code}" if code and code != name else name)


async def _bounded[T](call: Awaitable[T]) -> T:
    return await asyncio.wait_for(call, REQUEST_TIMEOUT_SECONDS)


async def _resend_code(client: Any, phone: str, phone_code_hash: str) -> Any:
    """auth.resendCode: the code again, by SentCode.next_type's way. The one
    raw request this tool makes (test_no_telegram_writes.RAW_ALLOWED): an auth
    call that puts nothing in any chat."""
    from telethon.errors import PhoneCodeExpiredError
    from telethon.tl.functions.auth import ResendCodeRequest

    # The number as Telethon sends it (utils.parse_phone drops the «+»):
    # Telegram ties the hash to it.
    digits = phone.lstrip("+")
    try:
        return await _bounded(client(ResendCodeRequest(digits, phone_code_hash)))
    except PhoneCodeExpiredError:
        # A resend cannot revive an expired hash; a new code can.
        return await _bounded(client.send_code_request(phone))


async def _ask_code(
    client: Any, phone: str, sent: Any, read: Callable[[str], str]
) -> bool:
    """Sign in with the code; True when signed in, False on the third wrong
    code. Raises SessionPasswordNeededError for the 2FA step."""
    from telethon.errors import PhoneCodeExpiredError, PhoneCodeInvalidError

    phone_code_hash = sent.phone_code_hash
    words = _class_name(sent.type) in WORD_CODES
    expired = False
    wrong = 0
    while True:
        answer = read(WORD_PROMPT if words else CODE_PROMPT).strip()
        if answer.lower() == RESEND_WORD:
            print(RESENDING, flush=True)
            if expired:
                # sign_in dropped Telethon's own copy of the expired hash, so
                # this is a fresh auth.sendCode.
                sent = await _bounded(client.send_code_request(phone))
            else:
                sent = await _resend_code(client, phone, phone_code_hash)
            for line in delivery(sent):
                print(line)
            phone_code_hash = sent.phone_code_hash
            words = _class_name(sent.type) in WORD_CODES
            expired = False
            continue
        code = answer if words else answer.replace(" ", "")
        if not code or (not words and not CODE.fullmatch(code)):
            print(NOT_A_CODE)
            continue
        try:
            await _bounded(client.sign_in(phone, code, phone_code_hash=phone_code_hash))
            return True
        except PhoneCodeInvalidError:
            wrong += 1
            if wrong >= MAX_WRONG_CODES:
                print(TOO_MANY_CODES.format(count=wrong), file=sys.stderr)
                return False
            print(WRONG_CODE)
        except PhoneCodeExpiredError:
            expired = True
            print(CODE_EXPIRED)


async def _ask_password(client: Any, read_password: Callable[[str], str]) -> bool:
    from telethon.errors import PasswordHashInvalidError

    wrong = 0
    while True:
        password = read_password(PASSWORD_PROMPT)
        if not password:
            # Telethon reads an empty password as «no password» and raises.
            print(EMPTY_PASSWORD)
            continue
        try:
            await _bounded(client.sign_in(password=password))
            return True
        except PasswordHashInvalidError:
            wrong += 1
            if wrong >= MAX_WRONG_PASSWORDS:
                print(TOO_MANY_PASSWORDS.format(count=wrong), file=sys.stderr)
                return False
            print(WRONG_PASSWORD)


async def _sign_in(
    client: Any, read: Callable[[str], str], read_password: Callable[[str], str]
) -> bool:
    from telethon.errors import SessionPasswordNeededError

    phone = ask_phone(read)
    print(SENDING_CODE.format(masked=mask_phone(phone)), flush=True)
    sent = await _bounded(client.send_code_request(phone))
    for line in delivery(sent):
        print(line)
    try:
        return await _ask_code(client, phone, sent, read)
    except SessionPasswordNeededError:
        return await _ask_password(client, read_password)


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
        except (OSError, ConnectionError, TimeoutError):
            print(NO_CONNECTION.format(seconds=CONNECT_TIMEOUT_SECONDS), file=sys.stderr)
            return False
        # Here and only here a person sits at the terminal to type the phone
        # number, the code and the password.
        try:
            signed_in = await _sign_in(client, read, read_password)
        except Exception as exc:  # every refusal is a sentence, never a traceback
            print(refusal(exc), file=sys.stderr)
            return False
        if signed_in:
            await _print_session(client)
        return signed_in
    finally:
        # Always, whatever ended the login; a disconnect that fails has
        # nothing left to protect and must not bury the sentence above it.
        with contextlib.suppress(Exception):
            await client.disconnect()


def main() -> int:
    if not (settings.telethon_api_id and settings.telethon_api_hash):
        print(MISSING_APP_KEYS, file=sys.stderr)
        return 1
    try:
        return 0 if asyncio.run(_login()) else 1
    except KeyboardInterrupt:
        print("\n" + STOPPED, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
