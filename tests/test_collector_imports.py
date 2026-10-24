"""Collectors reach the network through Fetcher and nothing else (07 §1.2).

Collectors call ``services.extraction`` for the model and read
``interactions`` for Telegram; the network is ``services/http.py``. So a
collector may not import Telethon, the Anthropic SDK, aiogram, an HTTP client
of its own, or anything that opens a socket; nor the reader or the bot
packages, which would hand it those transitively. Enrichment step E3 reads
the public ``https://t.me/s/<username>`` page through Fetcher, and the only
Telethon read is ``userbot/about.py`` draining ``chat_monitors`` (03 §2.3,
04 §3).

A second walk covers the whole package: Telethon is imported only by the
reader (``clientscrapper/userbot/``) and its login tool. The package-wide
``anthropic`` allow-list is 06 §5.2's ``tests/test_imports_fence.py``.

Written to hold on a tree with or without services/http.py and
collectors/base.py: the rules name what a collector may not touch, never
what must exist.
"""

from __future__ import annotations

import ast

import pytest

from tests.ast_tools import (
    PACKAGE,
    dotted_name,
    imported_modules,
    is_under,
    modules,
    owners,
    rel,
    tree_of,
)

COLLECTORS = PACKAGE / "collectors"

# Roots a collector may not import. httpx's homes are the shared layer
# (services/http.py, serp.py, erp_feeds.py, crm_push.py — 07 §1.2); a
# collector uses Fetcher, never a bare client.
COLLECTOR_FORBIDDEN = (
    "telethon",
    "anthropic",
    "aiogram",
    "httpx",
    "requests",
    "aiohttp",
    "urllib3",
    "websockets",
    "pycurl",
    "socket",
    "ssl",
    "http.client",
    "urllib.request",
    # robots.txt is Fetcher's to read (03 §1.1); RobotFileParser.read() fetches.
    "urllib.robotparser",
    "ftplib",
    "smtplib",
    "telnetlib",
    "subprocess",
    "clientscrapper.userbot",
    "clientscrapper.bot",
)
# Calls that open a connection or a process without an import of their own.
RAW_CALLS = {
    "open_connection",
    "open_unix_connection",
    "create_connection",
    "create_datagram_endpoint",
    "start_server",
    "system",
    "popen",
}
# Where Telethon may be imported (07 §1.2 says userbot/; 06 §5.2 adds the
# login tool, which must build the StringSession — T-13).
TELETHON_HOMES = ("userbot/",)
TELETHON_TOOLS = {"tools/userbot_login.py"}


def _collector_offences(tree: ast.Module, module: str = "") -> list[str]:
    offending = [
        f"imports {name}"
        for name in sorted(imported_modules(tree, module=module))
        if is_under(name, COLLECTOR_FORBIDDEN)
    ]
    owner = owners(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in RAW_CALLS
        ):
            function = owner.get(id(node))
            where = function.name if function else "<module>"
            offending.append(f"{where}: {node.func.attr}()")
    return offending


def _telethon_outside_the_reader(path_rel: str, tree: ast.Module, module: str) -> bool:
    if path_rel.startswith(TELETHON_HOMES) or path_rel in TELETHON_TOOLS:
        return False
    return any(
        is_under(name, ["telethon"]) for name in imported_modules(tree, module=module)
    )


COLLECTOR_MODULES = modules(COLLECTORS)


def test_the_walk_sees_the_collectors_package():
    names = {rel(p) for p in COLLECTOR_MODULES}
    assert {"collectors/__init__.py", "collectors/keywords.py"} <= names


@pytest.mark.parametrize("path", COLLECTOR_MODULES, ids=rel)
def test_a_collector_touches_the_network_only_through_fetcher(path):
    offending = _collector_offences(tree_of(path), module=dotted_name(path))
    assert offending == [], (
        f"{rel(path)}: collectors use services/http.py's Fetcher and "
        f"services/extraction, never their own client: {offending}"
    )


def test_telethon_is_imported_only_by_the_reader():
    outside = [
        rel(p)
        for p in modules()
        if _telethon_outside_the_reader(rel(p), tree_of(p), dotted_name(p))
    ]
    assert outside == [], (
        "only clientscrapper/userbot/ (and tools/userbot_login.py) may import "
        f"telethon; services/enrich.py reads t.me/s/ through Fetcher: {outside}"
    )


def test_the_import_detector_catches_each_shape():
    def offences(source: str, module: str = "clientscrapper.collectors.x") -> list[str]:
        return _collector_offences(ast.parse(source), module=module)

    assert offences("import telethon\n") == ["imports telethon"]
    assert offences("from telethon.tl.functions import channels\n") == [
        "imports telethon.tl.functions",
        "imports telethon.tl.functions.channels",
    ]
    assert offences("import anthropic as a\n") == ["imports anthropic"]
    assert offences("from anthropic import AsyncAnthropic\n") == [
        "imports anthropic",
        "imports anthropic.AsyncAnthropic",
    ]
    assert offences("import httpx\n") == ["imports httpx"]
    assert offences("from aiogram import Bot\n") == [
        "imports aiogram",
        "imports aiogram.Bot",
    ]
    assert offences("import socket\n") == ["imports socket"]
    assert offences("from urllib import request\n") == [
        "imports urllib.request"
    ]  # the module, reached through its parent
    assert offences("import importlib\nT = importlib.import_module('telethon')\n") == [
        "imports telethon"
    ]
    assert offences("x = __import__('requests')\n") == ["imports requests"]
    # The reader and the bot are not a back door to Telethon or aiogram.
    assert offences("from clientscrapper.userbot import about\n") == [
        "imports clientscrapper.userbot",
        "imports clientscrapper.userbot.about",
    ]
    assert offences("from ..bot import deliver\n") == [
        "imports clientscrapper.bot",
        "imports clientscrapper.bot.deliver",
    ]
    raw = (
        "import asyncio\n"
        "async def peek(host):\n"
        "    return await asyncio.open_connection(host, 443)\n"
    )
    assert offences(raw) == ["peek: open_connection()"]
    # What a collector does use stays allowed.
    fine = (
        "from urllib.parse import urljoin\n"
        "from http import HTTPStatus\n"
        "from clientscrapper.services.http import Fetcher\n"
        "from clientscrapper.collectors.keywords import PHONE_RE\n"
        "from clientscrapper.services import extraction\n"
    )
    assert offences(fine) == []

    assert _telethon_outside_the_reader(
        "services/enrich.py", ast.parse("from telethon import TelegramClient\n"), ""
    )
    assert not _telethon_outside_the_reader(
        "userbot/about.py", ast.parse("from telethon import TelegramClient\n"), ""
    )
    assert not _telethon_outside_the_reader(
        "tools/userbot_login.py",
        ast.parse("from telethon.sessions import StringSession\n"),
        "",
    )
