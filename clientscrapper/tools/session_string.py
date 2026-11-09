"""Whether a ``TELETHON_SESSION`` value is one Telethon can read (06 §2).

One rule for two readers: the reader process (``userbot/main.py``), which
idles and beats ``BAD_SESSION`` on a value that does not parse, and ``make
doctor`` (``tools/doctor.py``), which says so before ``make up`` — the docker
re-run pasted «TELETHON_SESSION=TELETHON_SESSION=…», the doctor called it
green and only the reader, one ``make reload`` later, noticed.

The string is a full credential: nothing here echoes any of it, and the
refusal names the error's class alone. Parsing it opens no connection. It
lives beside the login tool (a Telethon home, ``test_collector_imports``)
rather than in ``userbot/``: the doctor importing the reader would put the
doctor under the reader's write scan.
"""

from __future__ import annotations

from telethon.sessions import StringSession


class BadSession(ValueError):
    """The value does not parse; ``str()`` is the class of Telethon's error."""


def parse(value: str) -> StringSession:
    """The session Telethon reads from ``value``; ``BadSession`` otherwise."""
    try:
        return StringSession(value)
    except Exception as exc:
        raise BadSession(type(exc).__name__) from None
