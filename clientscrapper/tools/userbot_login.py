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

Nothing is read or sent: the tool signs in, prints the string and the
account's first name, and disconnects.

The owner runs this himself, so the words are his (Uzbek): the prompts are
the tool's own, not Telethon's English ones — whose phone prompt says «(or bot
token)» right after the owner has made a bot, and a bot token pasted there
signs the reader in as that bot. The phone prompt refuses anything that is not
a phone number. The connect is bounded: without a route to Telegram the
login used to wait in silence (the first docker dry run, 2026-11-09).
"""

from __future__ import annotations

import asyncio
import getpass
import re
import sys
from collections.abc import Callable
from typing import Any, Final

from clientscrapper.config import settings

SESSION_KEY = "TELETHON_SESSION"
CONNECT_TIMEOUT_SECONDS: Final = 60
# A phone number as typed: digits with an optional «+», spaces, dashes or
# brackets between them. A bot token («123456:AA…») has a colon and letters.
PHONE: Final = re.compile(r"\+?\d[\d ()-]{6,}\d")
PHONE_PROMPT: Final = "Oʻquvchi akkauntning telefon raqami (+998…): "
NOT_A_PHONE: Final = (
    "Bu telefon raqami emas. Bot tokeni ham emas — bu yerga oʻquvchi akkaunt "
    "SIM kartasining raqamini yoz, masalan +998901234567."
)
CODE_PROMPT: Final = "Telegram yuborgan kod (akkauntning Telegram ilovasiga keladi): "
PASSWORD_PROMPT: Final = "Ikki bosqichli tekshiruv (2FA) paroli: "
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


def ask_phone(read: Callable[[str], str] = input) -> str:
    """The reader account's number; asked again until it looks like one."""
    while True:
        answer = read(PHONE_PROMPT).strip()
        if PHONE.fullmatch(answer):
            return answer
        print(NOT_A_PHONE)


def session_line(session_string: str) -> str:
    """The .env line the operator pastes."""
    return f"{SESSION_KEY}={session_string}"


async def _login(client_factory: Any = None) -> bool:
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
        await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT_SECONDS)
    except (OSError, ConnectionError, TimeoutError):
        print(NO_CONNECTION.format(seconds=CONNECT_TIMEOUT_SECONDS), file=sys.stderr)
        await client.disconnect()
        return False
    # start() here and only here: this is the one place a person sits at the
    # terminal to type the phone number, the code and the password.
    await client.start(
        phone=ask_phone,
        code_callback=lambda: input(CODE_PROMPT),
        password=lambda: getpass.getpass(PASSWORD_PROMPT),
    )
    try:
        session_string = client.session.save()
        # The credential goes out first, before anything that could fail:
        # logging in mints a new authorised session on the account, and if the
        # string is lost the login cannot be repeated, only redone — leaving an
        # orphan session behind each time.
        print(
            "\nQuyidagi qatorni serverdagi .env faylidagi TELETHON_SESSION= oʻrniga "
            "yoz, keyin: make reload.\nBu qator — akkaunt paroli kabi maxfiy: "
            "hech kimga yuborma, hech qayerga joylama.\n"
        )
        print(session_line(session_string) + "\n")
        try:
            me = await client.get_me()
            name = getattr(me, "first_name", None) or getattr(me, "id", "?")
            print(f"Kirildi: {name}")
        except Exception as exc:  # cosmetic only — never lose the session over it
            print(f"(kirildi; akkaunt nomini oʻqib boʻlmadi: {type(exc).__name__})")
    finally:
        await client.disconnect()
    return True


def main() -> int:
    if not (settings.telethon_api_id and settings.telethon_api_hash):
        print(MISSING_APP_KEYS, file=sys.stderr)
        return 1
    return 0 if asyncio.run(_login()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
