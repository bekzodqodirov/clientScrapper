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
reader (``clientscrapper/userbot/``) and its login tool, and an HTTP
connection is opened only in httpx's four homes (``services/http.py``,
``serp.py``, ``erp_feeds.py``, ``crm_push.py`` — 07 §1.2) and in one function
beside them, ``tools/doctor.py::probe_crm`` (06 §3.3); elsewhere httpx is
types and exceptions, and no other HTTP client exists. The package-wide
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
    attr_chain,
    dotted_name,
    dynamic_names,
    imported_modules,
    imports,
    is_under,
    module_aliases,
    modules,
    owners,
    rel,
    resolve_attribute,
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
# httpx's homes (07 §1.2). Outside them httpx may be named for its types and
# exceptions (services/retry.py classifies httpx.TimeoutException) but not
# for what opens a connection: a client, a transport, a request function.
HTTPX_HOMES = {
    "services/http.py",
    "services/serp.py",
    "services/erp_feeds.py",
    "services/crm_push.py",
}
# One function outside the homes may open a client, for one keyed GET:
# ``make doctor``'s ERP probe (06 §3.3). The doctor may not import the
# settings (it diagnoses a .env they refuse), so it cannot use Fetcher; the
# allowance is that function by name, not the module.
HTTPX_FUNCTION_HOMES = {("tools/doctor.py", "probe_crm")}
HTTPX_OPENERS = {
    "Client",
    "AsyncClient",
    "HTTPTransport",
    "AsyncHTTPTransport",
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "request",
    "stream",
}
# The package has one HTTP client; a second one is a route around Fetcher,
# its robots/breaker rules, and the outbound guards (06 §5.3).
OTHER_HTTP_CLIENTS = (
    "requests",
    "aiohttp",
    "urllib3",
    "pycurl",
    "httplib2",
    "http.client",
    "urllib.request",
    "websockets",
)


def fetcher_private_names(tree: ast.Module | None = None) -> frozenset[str]:
    """Every private name of services/http.Fetcher: the attributes it sets on
    ``self`` (``_client``, ``_proxy_client``, ``_transport`` …) and its own
    ``_methods`` (``_send``, ``_request`` …). Through any of them a collector
    holds a bare client past robots.txt, the User-Agent, pacing, the breaker,
    the page budget and evidence capture. Derived, so a renamed attribute is
    still fenced."""
    tree = tree if tree is not None else tree_of(PACKAGE / "services" / "http.py")
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "Fetcher"):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef):
                if inner.name.startswith("_") and not inner.name.startswith("__"):
                    names.add(inner.name)
            elif (
                isinstance(inner, ast.Attribute)
                and isinstance(inner.ctx, ast.Store)
                and attr_chain(inner)[0] == "self"
                and inner.attr.startswith("_")
            ):
                names.add(inner.attr)
    return frozenset(names)


FETCHER_PRIVATE = fetcher_private_names()


def _collector_offences(tree: ast.Module, module: str = "") -> list[str]:
    offending = [
        f"imports {name}"
        for name in sorted(imported_modules(tree, module=module))
        if is_under(name, COLLECTOR_FORBIDDEN)
    ]
    owner = owners(tree)
    aliases = module_aliases(tree, module=module)
    for node in ast.walk(tree):
        function = owner.get(id(node))
        where = function.name if function else "<module>"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in RAW_CALLS
        ):
            offending.append(f"{where}: {node.func.attr}()")
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr in FETCHER_PRIVATE:
            offending.append(f"{where}: .{node.attr} (Fetcher's own)")
            continue
        # A forbidden module reached as an attribute of an imported one
        # («http.httpx.AsyncClient» after «from clientscrapper.services
        # import http»): the import walk sees only the allowed module. Only
        # chains that start at an import: «stats.requests» is a counter.
        chain = attr_chain(node)
        resolved = resolve_attribute(node, aliases)
        if resolved is None:
            continue
        path = [*resolved[0].split("."), resolved[1]]
        start = len(aliases[chain[0]].split("."))
        for j in range(start, len(path)):
            root = is_under(".".join(path[j:]), COLLECTOR_FORBIDDEN)
            if root is not None and ".".join(path[j : j + root.count(".") + 1]) == root:
                offending.append(f"{where}: {'.'.join(chain)} (reaches {root})")
                break
    # A name built at run time is a shape no rule above can read: fail closed.
    for node, what in dynamic_names(tree):
        function = owner.get(id(node))
        offending.append(f"{function.name if function else '<module>'}: {what}")
    return offending


def _telethon_outside_the_reader(path_rel: str, tree: ast.Module, module: str) -> bool:
    if path_rel.startswith(TELETHON_HOMES) or path_rel in TELETHON_TOOLS:
        return False
    return any(
        is_under(name, ["telethon"]) for name in imported_modules(tree, module=module)
    )


def _http_offences(path_rel: str, tree: ast.Module, module: str) -> list[str]:
    found = [
        f"imports {name}"
        for name in sorted(imported_modules(tree, module=module))
        if is_under(name, OTHER_HTTP_CLIENTS)
    ]
    if path_rel in HTTPX_HOMES:
        return found
    for imp in imports(tree, module=module):
        if imp.module == "httpx" and (imp.name in HTTPX_OPENERS or imp.name == "*"):
            found.append(f"imports httpx.{imp.name}")
    aliases = module_aliases(tree, module=module)
    owner = owners(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)):
            continue
        resolved = resolve_attribute(node, aliases)
        if resolved and resolved[0] == "httpx" and resolved[1] in HTTPX_OPENERS:
            function = owner.get(id(node))
            where = function.name if function else "<module>"
            if (path_rel, where) in HTTPX_FUNCTION_HOMES:
                continue
            found.append(f"{where}: httpx.{resolved[1]}")
    return sorted(found)


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


def test_an_http_connection_opens_only_in_httpx_s_homes():
    offending = {
        rel(p): found
        for p in modules()
        if (found := _http_offences(rel(p), tree_of(p), dotted_name(p)))
    }
    assert offending == {}, (
        "httpx clients live in services/http.py, serp.py, erp_feeds.py, "
        "crm_push.py and doctor.probe_crm; nothing else opens a connection "
        f"(07 §1.2, 06 §3.3): {offending}"
    )


def test_the_http_detector_catches_each_shape():
    def found(path: str, source: str) -> list[str]:
        module = "clientscrapper." + path.removesuffix(".py").replace("/", ".")
        return _http_offences(path, ast.parse(source), module)

    sender = (
        "import httpx\n"
        "async def notify(url):\n"
        "    async with httpx.AsyncClient() as c:\n"
        "        await c.post(url)\n"
        "def once(url):\n"
        "    return httpx.post(url)\n"
    )
    assert found("services/notify.py", sender) == [
        "notify: httpx.AsyncClient",
        "once: httpx.post",
    ]
    assert found("services/http.py", sender) == []
    assert found("services/notify.py", "from httpx import AsyncClient as C\n") == [
        "imports httpx.AsyncClient"
    ]
    assert found("tools/doctor.py", "import requests\n") == ["imports requests"]
    # The doctor's one probe may open a client; any other function of it may not.
    probe = sender.replace("async def notify", "async def probe_crm")
    assert found("tools/doctor.py", probe) == ["once: httpx.post"]
    assert found("tools/other.py", probe) == [
        "once: httpx.post",
        "probe_crm: httpx.AsyncClient",
    ]
    assert found("services/http.py", "from urllib import request\n") == [
        "imports urllib.request"
    ]
    # Types and exceptions are no connection.
    types = (
        "import httpx\n"
        "TRANSIENT = (httpx.ConnectError, httpx.ReadTimeout)\n"
        "def is_ok(r: httpx.Response) -> bool:\n"
        "    return httpx.URL(str(r.url)).host != ''\n"
    )
    assert found("services/retry.py", types) == []


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
    # The Fetcher's own client, and httpx reached through an allowed module.
    private = (
        "class Olx:\n"
        "    async def collect(self, session, budget):\n"
        "        return await self.fetcher._client.get('https://www.olx.uz/api/v1/')\n"
        "    async def raw(self):\n"
        "        f = self.fetcher\n"
        "        return await f._send('GET', 'https://www.olx.uz/', host='x')\n"
    )
    assert sorted(offences(private)) == [
        "collect: ._client (Fetcher's own)",
        "raw: ._send (Fetcher's own)",
    ]
    through = (
        "from clientscrapper.services import http\n"
        "import clientscrapper.services.http as h2\n"
        "async def collect(session, budget):\n"
        "    await http.httpx.AsyncClient().get('https://www.olx.uz/')\n"
        "    return h2.socket.create_connection\n"
    )
    assert sorted(offences(through)) == [
        "collect: h2.socket (reaches socket)",
        "collect: h2.socket.create_connection (reaches socket)",
        "collect: http.httpx (reaches httpx)",
        "collect: http.httpx.AsyncClient (reaches httpx)",
    ]
    # Names built at run time (the seam review's probe_p4 shape).
    built = (
        "import importlib\n"
        "async def collect(self, session, budget):\n"
        "    client = importlib.import_module('ht' + 'tpx').AsyncClient()\n"
        "    verb = 'get'\n"
        "    return await getattr(client, verb)('https://www.olx.uz/')\n"
    )
    assert sorted(offences(built)) == [
        "collect: getattr(…, <built name>)",
        "collect: import_module(<built name>)",
    ]
    # What a collector does use stays allowed.
    fine = (
        "from urllib.parse import urljoin\n"
        "from http import HTTPStatus\n"
        "from clientscrapper.services.http import Fetcher\n"
        "from clientscrapper.collectors.keywords import PHONE_RE\n"
        "from clientscrapper.services import extraction\n"
    )
    assert offences(fine) == []
    public = (
        "async def collect(self, session, budget):\n"
        "    r = await self.fetcher.get('https://x.example.uz/', source='s',"
        " budget=budget)\n"
        "    s = self.fetcher.stats\n"
        "    return s.pages, s.requests, r.fetched_at, http.BudgetExhausted\n"
    )
    assert offences(public) == []


def test_the_fetcher_private_names_are_derived_from_fetcher():
    assert {"_client", "_proxy_client", "_transport", "_send", "_request"} <= (
        FETCHER_PRIVATE
    )
    assert not {"get", "post_json", "bind", "stats", "__init__"} & FETCHER_PRIVATE

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
