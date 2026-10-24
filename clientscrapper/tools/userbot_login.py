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
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from clientscrapper.config import settings

SESSION_KEY = "TELETHON_SESSION"


def session_line(session_string: str) -> str:
    """The .env line the operator pastes."""
    return f"{SESSION_KEY}={session_string}"


async def _login(client_factory: Any = None) -> None:
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
    # start() here and only here: this is the one place a person sits at the
    # terminal to type the phone number, the code and the password.
    await client.start()
    try:
        session_string = client.session.save()
        # The credential goes out first, before anything that could fail:
        # logging in mints a new authorised session on the account, and if the
        # string is lost the login cannot be repeated, only redone — leaving an
        # orphan session behind each time.
        print("\nAdd this line to the server .env (keep it secret, never commit it):\n")
        print(session_line(session_string) + "\n")
        try:
            me = await client.get_me()
            name = getattr(me, "first_name", None) or getattr(me, "id", "?")
            print(f"Logged in as: {name}")
        except Exception as exc:  # cosmetic only — never lose the session over it
            print(f"(signed in; could not read the account name: {type(exc).__name__})")
    finally:
        await client.disconnect()


def main() -> int:
    if not (settings.telethon_api_id and settings.telethon_api_hash):
        print(
            "TELETHON_API_ID and TELETHON_API_HASH must be set first "
            "(my.telegram.org → API development tools, a separate app from MIYA's).",
            file=sys.stderr,
        )
        return 1
    asyncio.run(_login())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
