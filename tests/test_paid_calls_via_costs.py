"""Every paid call goes through ``costs.ask`` (07 §1.2, 04 §8).

The daily cap of $10 is enforced in code only if no paid request can leave
the process without ``ask`` deciding first. A *paid site* is any of:

* an Anthropic request — ``*.messages.create/parse/stream``, including the
  batch submit ``*.messages.batches.create`` (and the legacy
  ``completions.create``); the other ``messages`` calls (batch retrieve,
  results, count_tokens) cost nothing but still belong to the two model
  modules only;
* a URL of a paid API host, as a literal (an f-string or a ``+`` chain
  counts as one template), or any name bound to one at module or class level
  — followed through other module-level names and through imports between
  our modules, each name read where it was bound (``serp.BRAVE_URL`` is
  serp's; a namesake in another module is not);
* a paid host bound bare to a name (``BRAVE_HOST = "api.search.brave.com"``)
  wherever a URL is built on it; comparing it or listing it in a table
  (``HOST_RPS``) builds nothing;
* an Anthropic call through a name bound to ``client.messages`` (or to the
  bound method itself);
* a Fetcher request with ``proxy=`` anything but a literal False (03 §1.1:
  the Uzum proxy is paid) — Fetcher itself passes the flag through.

Each paid site must sit in an allow-listed module, and its function must
contain a call to ``costs.ask`` or be reached only from functions that do —
called, passed as ``call=…``, or wrapped in ``partial`` (a call-graph walk
within the module). A paid site at module level, or in a function that a
module-level table can reach, is a door from anywhere and fails.

``obey_robots=False`` is allowed only at documented JSON APIs with a key
(03 §1.1: Brave, Yandex, Places, 2GIS, hh, Graph, Apify): in an allow-listed
module, in a function that names such an API's URL. Fetcher itself
(services/http.py) passes the flag through and is exempt.

The model modules, the SERP client and the paid collectors do not exist
yet; the self-test below plants each shape so the guard is proven now and
holds the day they land.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Iterable
from dataclasses import dataclass

from clientscrapper.services import costs
from tests.ast_tools import (
    CLASSIFYING,
    Function,
    attr_chain,
    bound_name,
    called,
    dotted_name,
    imports,
    module_aliases,
    modules,
    owners,
    parents,
    reached_only_from,
    rel,
    texts,
    tree_of,
)

COSTS = "clientscrapper.services.costs"

ANTHROPIC_MODULES = {"services/extraction.py", "services/model_jobs.py"}
URL_MODULES = {
    "services/serp.py",
    "collectors/maps.py",
    "collectors/instagram_discovery.py",
    "collectors/olx_business.py",
    "collectors/uzum_sellers.py",
}
# 07 §1.2 lists the first five hosts; catalog.api.2gis.com is the 2GIS API
# (op twogis_api, collectors/maps.py, 03 §2.8), paid once past the demo key.
PAID_HOSTS = (
    "api.search.brave.com",
    "searchapi.api.cloud.yandex.net",
    "places.googleapis.com",
    "api.apify.com",
    "graph.facebook.com",
    "catalog.api.2gis.com",
)
# Documented APIs with a key, where robots.txt does not apply (03 §1.1). The
# Yandex poll host is free but is the same documented API.
ROBOTS_API_HOSTS = (*PAID_HOSTS, "api.hh.ru", "operation.api.cloud.yandex.net")
ROBOTS_MODULES = URL_MODULES | {"collectors/hh_vacancies.py"}
FETCHER = "services/http.py"

ANTHROPIC_SPEND = {"create", "parse", "stream"}
NO_ASK = "not behind costs.ask"
OFF_API = "obey_robots opt-out off an API call site"


def _url_pattern(hosts: Iterable[str]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(h) for h in hosts)
    return re.compile(rf"https?://(?:[\w-]+\.)*(?:{alternatives})(?![\w.-])", re.I)


def _host_pattern(hosts: Iterable[str]) -> re.Pattern[str]:
    """A bare host (no scheme), for fullmatch."""
    alternatives = "|".join(re.escape(h) for h in hosts)
    return re.compile(rf"(?:[\w-]+\.)*(?:{alternatives})", re.I)


PAID_URL = _url_pattern(PAID_HOSTS)
API_URL = _url_pattern(ROBOTS_API_HOSTS)
PAID_HOST = _host_pattern(PAID_HOSTS)
API_HOST = _host_pattern(ROBOTS_API_HOSTS)


@dataclass(frozen=True, slots=True)
class Module:
    rel: str  # services/serp.py
    dotted: str  # clientscrapper.services.serp
    tree: ast.Module


@dataclass(frozen=True, slots=True)
class Bindings:
    """Per module (dotted name): names holding a URL, names holding a bare host."""

    urls: dict[str, set[str]]
    hosts: dict[str, set[str]]


@dataclass(frozen=True, slots=True)
class Site:
    kind: str  # "anthropic", "anthropic_api", "url", "proxy"
    node: ast.AST
    what: str


def _package() -> list[Module]:
    return [Module(rel(p), dotted_name(p), tree_of(p)) for p in modules()]


# --- names bound to a URL --------------------------------------------------------


def _top_level_assigns(tree: ast.Module) -> list[tuple[list[str], ast.expr]]:
    """(targets, value) of every assignment at module or class level whose
    value makes no call: ``URL = BASE + "/x"`` defines a constant,
    ``R = httpx.get(URL)`` at import time is a request."""
    owner = owners(tree)
    found = []
    for node in ast.walk(tree):
        if id(node) in owner:
            continue
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is None:
                continue
            targets, value = [node.target.id], node.value
        else:
            continue
        if not any(isinstance(n, ast.Call) for n in ast.walk(value)):
            found.append((targets, value))
    return found


def _aliases(m: Module) -> dict[str, str]:
    return module_aliases(m.tree, module=m.dotted)


def _mentions(expr: ast.AST, pattern: re.Pattern[str], m: Module, b: Bindings) -> bool:
    """``expr`` (a node of ``m``) names a URL: a literal ``pattern`` matches,
    a URL name, or a host name a URL is built on."""
    if any(pattern.search(t.value) for t in texts(expr)):
        return True
    aliases, parent = _aliases(m), parents(m.tree)
    urls, hosts = b.urls.get(m.dotted, set()), b.hosts.get(m.dotted, set())
    for node in ast.walk(expr):
        if bound_name(node, urls, aliases, b.urls):
            return True
        host = bound_name(node, hosts, aliases, b.hosts)
        if host and not isinstance(parent.get(id(node)), CLASSIFYING):
            return True
    return False


def host_names(mods: list[Module], host: re.Pattern[str]) -> dict[str, set[str]]:
    """Per module, the names bound at module or class level to a bare host
    ``host`` fullmatches, to another such name, or imported as one."""
    bound: dict[str, set[str]] = {m.dotted: set() for m in mods}
    changed = True
    while changed:
        changed = False
        for m in mods:
            mine, aliases = bound[m.dotted], _aliases(m)
            for targets, value in _top_level_assigns(m.tree):
                literal = (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and host.fullmatch(value.value.strip()) is not None
                )
                alias = bound_name(value, mine, aliases, bound) is not None
                if set(targets) - mine and (literal or alias):
                    mine.update(targets)
                    changed = True
            for imp in imports(m.tree, module=m.dotted):
                if imp.name in bound.get(imp.module, set()) and imp.alias not in mine:
                    mine.add(imp.alias)
                    changed = True
    return bound


def bindings(mods: list[Module], url: re.Pattern[str], host: re.Pattern[str]) -> Bindings:
    """The URL names of every module: assigned at module or class level from a
    literal, a URL name or a URL built on a host name, or imported from one of
    our modules where the name is one. A fixpoint over all."""
    hosts = host_names(mods, host)
    urls: dict[str, set[str]] = {m.dotted: set() for m in mods}
    b = Bindings(urls, hosts)
    changed = True
    while changed:
        changed = False
        for m in mods:
            mine, aliases = urls[m.dotted], _aliases(m)
            for targets, value in _top_level_assigns(m.tree):
                if bound_name(value, hosts[m.dotted], aliases, hosts):
                    continue  # another name for the host, not a URL
                if set(targets) - mine and _mentions(value, url, m, b):
                    mine.update(targets)
                    changed = True
            for imp in imports(m.tree, module=m.dotted):
                if imp.name in urls.get(imp.module, set()) and imp.alias not in mine:
                    mine.add(imp.alias)
                    changed = True
    return b


# --- the paid sites ------------------------------------------------------------------


def _expansions(tree: ast.Module) -> dict[str, list[str]]:
    """Plain names bound to an attribute chain anywhere in the module:
    ``messages = client.messages`` → ``messages: [client, messages]``."""
    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.Attribute):
            for target in targets:
                if isinstance(target, ast.Name):
                    found[target.id] = attr_chain(value)
    return found


def _expand(chain: list[str], expansions: dict[str, list[str]]) -> list[str]:
    seen: set[str] = set()
    while chain[0] in expansions and chain[0] not in seen:
        seen.add(chain[0])
        chain = [*expansions[chain[0]], *chain[1:]]
    return chain


def _anthropic_kind(call: ast.Call, expansions: dict[str, list[str]]) -> str | None:
    for target in called(call):
        chain = _expand(attr_chain(target), expansions)
        inner = chain[1:-1]
        if "messages" in inner or "completions" in inner:
            return "anthropic" if chain[-1] in ANTHROPIC_SPEND else "anthropic_api"
    return None


def _definition_ids(tree: ast.Module, names: set[str]) -> set[int]:
    """Nodes inside a module/class-level assignment that defines a bound name:
    the definition of a URL or host constant is not a use of it."""
    ids: set[int] = set()
    for targets, value in _top_level_assigns(tree):
        if set(targets) & names:
            ids.update(id(n) for n in ast.walk(value))
    return ids


def _is_off(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value in (False, None)


def paid_sites(m: Module, b: Bindings) -> list[Site]:
    urls, hosts = b.urls.get(m.dotted, set()), b.hosts.get(m.dotted, set())
    aliases, parent = _aliases(m), parents(m.tree)
    defining = _definition_ids(m.tree, urls | hosts)
    expansions = _expansions(m.tree)
    sites: list[Site] = []
    for node in ast.walk(m.tree):
        if isinstance(node, ast.Call):
            kind = _anthropic_kind(node, expansions)
            if kind:
                sites.append(Site(kind, node, ".".join(attr_chain(node.func))))
            for kw in node.keywords:
                if kw.arg == "proxy" and not _is_off(kw.value):
                    sites.append(Site("proxy", node, f"proxy={ast.unparse(kw.value)}"))
        if id(node) in defining:
            continue
        # A URL name counts wherever it is read; a host name only where a URL
        # is built on it.
        building = not isinstance(parent.get(id(node)), CLASSIFYING)
        url = bound_name(node, urls, aliases, b.urls)
        host = bound_name(node, hosts, aliases, b.hosts) if building else None
        if name := url or host:
            sites.append(Site("url", node, name))
    for text in texts(m.tree):
        if id(text.node) not in defining and (hit := PAID_URL.search(text.value)):
            sites.append(Site("url", text.node, hit.group(0)))
    return sites


# --- the ask door ------------------------------------------------------------------


def _ask_aliases(m: Module) -> tuple[set[str], set[str]]:
    """(names bound to the costs module, names bound to costs.ask)."""
    module_names: set[str] = set()
    ask_names: set[str] = {"ask"} if m.dotted == COSTS else set()
    for imp in imports(m.tree, module=m.dotted):
        whole = imp.name is None and imp.module == COSTS
        if (whole and imp.alias != COSTS.split(".")[0]) or (
            imp.name == "costs" and imp.module == "clientscrapper.services"
        ):
            module_names.add(imp.alias)
        elif imp.name == "ask" and imp.module == COSTS:
            ask_names.add(imp.alias)
    return module_names, ask_names


def _asks(m: Module) -> set[int]:
    """Ids of the functions that call costs.ask themselves (innermost)."""
    module_names, ask_names = _ask_aliases(m)
    owner = owners(m.tree)
    found: set[int] = set()
    for node in ast.walk(m.tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        chain = attr_chain(func)
        is_ask = (isinstance(func, ast.Name) and func.id in ask_names) or (
            isinstance(func, ast.Attribute)
            and func.attr == "ask"
            and (chain[0] in module_names or chain == [*COSTS.split("."), "ask"])
        )
        if is_ask and (function := owner.get(id(node))) is not None:
            found.add(id(function))
    return found


def _where(m: Module, node: ast.AST) -> Function | None:
    return owners(m.tree).get(id(node))


def offences(mods: list[Module]) -> list[str]:
    paid = bindings(mods, PAID_URL, PAID_HOST)
    api = bindings(mods, API_URL, API_HOST)
    found: list[str] = []
    for m in mods:
        asking = _asks(m)

        def asks(f: Function, asking: set[int] = asking) -> bool:
            return id(f) in asking

        for site in paid_sites(m, paid):
            if site.kind == "proxy" and m.rel == FETCHER:
                continue  # Fetcher passes its caller's flag through
            function = _where(m, site.node)
            name = function.name if function else "<module>"
            home = ANTHROPIC_MODULES if site.kind.startswith("anthropic") else URL_MODULES
            if m.rel not in home:
                found.append(f"{m.rel}:{name}: {site.what} outside {sorted(home)}")
                continue
            if site.kind == "anthropic_api":
                continue
            guarded = function is not None and reached_only_from(m.tree, function, asks)
            if not guarded:
                found.append(f"{m.rel}:{name}: {site.what} {NO_ASK}")
        if m.rel == FETCHER:
            continue
        for node in ast.walk(m.tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                keeps = isinstance(kw.value, ast.Constant) and kw.value.value is True
                if kw.arg != "obey_robots" or keeps:
                    continue
                function = _where(m, node)
                name = function.name if function else "<module>"
                at_api = function is not None and _mentions(function, API_URL, m, api)
                if m.rel not in ROBOTS_MODULES or not at_api:
                    found.append(f"{m.rel}:{name}: {OFF_API}")
    return sorted(found)


# --- the tree ------------------------------------------------------------------------


def test_ask_is_the_04_8_signature():
    """The door the guard points at: ask(session, *, operation, est_usd,
    call, price, now) — the call is handed over, never made before."""
    params = inspect.signature(costs.ask).parameters
    assert list(params)[:7] == [
        "session",
        "operation",
        "est_usd",
        "call",
        "price",
        "now",
        "source_key",
    ]
    assert params["session"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    keyword_only = {"operation", "est_usd", "call", "price", "now"}
    assert all(params[p].kind is inspect.Parameter.KEYWORD_ONLY for p in keyword_only)


def test_the_scan_covers_the_package_and_finds_ask_in_costs():
    mods = _package()
    assert {"services/costs.py", "services/usage.py", "config.py"} <= {
        m.rel for m in mods
    }
    [costs_module] = [m for m in mods if m.rel == "services/costs.py"]
    # costs.ask itself is defined there; its own module counts its calls.
    assert _ask_aliases(costs_module)[1] == {"ask"}


def test_every_paid_call_goes_through_costs_ask():
    assert offences(_package()) == []


# --- the detector ----------------------------------------------------------------------


def _mods(sources: dict[str, str]) -> list[Module]:
    return [
        Module(
            path,
            "clientscrapper." + path.removesuffix(".py").replace("/", "."),
            ast.parse(src),
        )
        for path, src in sources.items()
    ]


GUARDED_EXTRACTION = """
from clientscrapper.services import costs

async def classify(session, client, now):
    async def _call():
        return await client.messages.parse(model="m", messages=[])
    return await costs.ask(session, operation="classify", est_usd=1, call=_call,
                           price=lambda r: r, now=now)

async def verdict(session, client, now):
    return await costs.ask(session, operation="verdict", est_usd=1,
                           call=lambda: client.messages.create(model="m"),
                           price=lambda r: r, now=now)

async def submit(session, client, now):
    return await costs.ask(session, operation="company_verdict", est_usd=1,
                           call=partial(_submit, client), price=lambda r: r, now=now)

async def _submit(client):
    return await client.messages.batches.create(requests=[])

async def collect(client, batch_id):
    return await client.messages.batches.results(batch_id)
"""

GUARDED_SERP = """
from clientscrapper.services.costs import ask as spend

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SEARCH = BRAVE_URL
HOST_RPS = {"api.search.brave.com": 1.0}

class Serp:
    async def brave(self, session, q, now):
        return await spend(session, operation="serp_brave", est_usd=1,
                           call=lambda: self._get(q), price=lambda r: r, now=now)

    async def _get(self, q):
        return await self.fetcher.get(SEARCH, params={"q": q}, obey_robots=False)

    async def yandex_poll(self, op_id):
        url = f"https://operation.api.cloud.yandex.net/operations/{op_id}"
        return await self.fetcher.get(url, obey_robots=False)
"""


def test_the_detector_accepts_the_shapes_the_spec_uses():
    mods = _mods(
        {
            "services/extraction.py": GUARDED_EXTRACTION,
            "services/serp.py": GUARDED_SERP,
            "services/http.py": "async def get(url, obey_robots=True):\n"
            "    return await _fetch(url, obey_robots=obey_robots)\n",
            "collectors/hh_vacancies.py": "API = 'https://api.hh.ru/vacancies'\n"
            "async def page(f):\n    return await f.get(API, obey_robots=False)\n",
        }
    )
    assert offences(mods) == []


def test_the_detector_catches_each_shape():
    def found(path: str, source: str, extra: dict[str, str] | None = None) -> list[str]:
        return offences(_mods({path: source, **(extra or {})}))

    x = "services/extraction.py"
    # A direct call with no ask anywhere on its path.
    direct = "async def classify(client):\n    return await client.messages.create()\n"
    assert found(x, direct) == [f"{x}:classify: client.messages.create {NO_ASK}"]
    # The right call in the wrong module.
    assert found("services/enrich.py", direct) == [
        "services/enrich.py:classify: client.messages.create outside "
        "['services/extraction.py', 'services/model_jobs.py']"
    ]
    # A helper reached from a guarded function *and* from an unguarded one.
    backdoor = "async def backdoor(client):\n    await _submit(client)\n"
    batch = f"{x}:_submit: client.messages.batches.create {NO_ASK}"
    assert found(x, GUARDED_EXTRACTION + backdoor) == [batch]
    # A function a module-level table can reach is a door from anywhere.
    assert found(x, GUARDED_EXTRACTION + "JOBS = {'submit': _submit}\n") == [batch]
    # ask inside a nested helper does not guard the enclosing function.
    nested = (
        "from clientscrapper.services import costs\n"
        "async def classify(session, client, now):\n"
        "    r = await client.messages.create()\n"
        "    async def _later():\n"
        "        await costs.ask(session, operation='classify', est_usd=1,\n"
        "                        call=lambda: None, price=lambda r: r, now=now)\n"
        "    return r\n"
    )
    assert found(x, nested) == [f"{x}:classify: client.messages.create {NO_ASK}"]

    # URL constants: used unguarded, in an f-string, at module level, and
    # imported (by name or through the module) where no paid call may be.
    maps = "collectors/maps.py"
    unguarded = (
        "URL = 'https://places.googleapis.com/v1/places:searchText'\n"
        "async def text_ids(f):\n    return await f.post_json(URL)\n"
    )
    assert found(maps, unguarded) == [f"{maps}:text_ids: URL {NO_ASK}"]
    fstring = (
        "async def details(f, pid):\n"
        "    return await f.get(f'https://places.googleapis.com/v1/places/{pid}')\n"
    )
    assert found(maps, fstring) == [
        f"{maps}:details: https://places.googleapis.com {NO_ASK}"
    ]
    olx = "collectors/olx_business.py"
    module_level = "import httpx\nR = httpx.get('https://api.apify.com/v2/acts')\n"
    assert found(olx, module_level) == [f"{olx}:<module>: https://api.apify.com {NO_ASK}"]
    serp = {"services/serp.py": GUARDED_SERP}
    elsewhere = " outside " + str(sorted(URL_MODULES))
    importer = (
        "from clientscrapper.services.serp import BRAVE_URL as B\n"
        "async def look(f):\n    return await f.get(B)\n"
    )
    assert found("services/enrich.py", importer, serp) == [
        "services/enrich.py:look: B" + elsewhere
    ]
    via_module = (
        "from clientscrapper.services import serp\n"
        "async def look(f):\n    return await f.get(serp.BRAVE_URL)\n"
    )
    assert found("services/registry.py", via_module, serp) == [
        "services/registry.py:look: BRAVE_URL" + elsewhere
    ]
    # The paid proxy.
    uzum = "collectors/uzum_sellers.py"
    proxy = "async def shop(f, url):\n    return await f.get(url, proxy=True)\n"
    assert found(uzum, proxy) == [f"{uzum}:shop: proxy=True {NO_ASK}"]

    # Robots: an opt-out on an HTML page, in a module that may not, or
    # passed through a variable.
    html = (
        "async def page(f):\n"
        "    return await f.get('https://www.olx.uz/x/', obey_robots=False)\n"
    )
    assert found(olx, html) == [f"{olx}:page: {OFF_API}"]
    hh = (
        "API = 'https://api.hh.ru/vacancies'\n"
        "async def page(f):\n    return await f.get(API, obey_robots=False)\n"
    )
    gp = "collectors/directories/goldenpages.py"
    assert found(gp, hh) == [f"{gp}:page: {OFF_API}"]
    flag = (
        "async def page(f, url, keep):\n    return await f.get(url, obey_robots=keep)\n"
    )
    hhv = "collectors/hh_vacancies.py"
    assert found(hhv, flag) == [f"{hhv}:page: {OFF_API}"]
    # A messages object or a bound method kept in a name is the same call.
    kept = (
        "async def classify(client):\n"
        "    messages = client.messages\n"
        "    return await messages.create(model='m')\n"
        "async def verdict(client):\n"
        "    create = client.messages.create\n"
        "    return await create(model='m')\n"
    )
    assert found(x, kept) == [
        f"{x}:classify: messages.create {NO_ASK}",
        f"{x}:verdict: create {NO_ASK}",
    ]
    assert found("services/enrich.py", kept)[0] == (
        "services/enrich.py:classify: messages.create outside "
        "['services/extraction.py', 'services/model_jobs.py']"
    )
    # A paid host bound bare: a URL built on it is paid, a table entry or a
    # comparison is not.
    serp_host = (
        "BRAVE_HOST = 'api.search.brave.com'\n"
        "HOST_RPS = {BRAVE_HOST: 1.0}\n"
        "async def search(fetcher, q):\n"
        "    if fetcher.host == BRAVE_HOST:\n"
        "        pass\n"
        "    return await fetcher.get(f'https://{BRAVE_HOST}/res/v1/web/search')\n"
    )
    s = "services/serp.py"
    assert found(s, serp_host) == [f"{s}:search: BRAVE_HOST {NO_ASK}"]
    # A URL concatenated from pieces, and a class attribute read through self.
    concat = (
        "BASE = 'https://' + 'places.googleapis.com'\n"
        "class Maps:\n"
        "    URL = BASE + '/v1/places:searchText'\n"
        "    async def ids(self, f):\n"
        "        return await f.post_json(self.URL)\n"
        "async def text_ids(f):\n"
        "    return await f.post_json(BASE + '/v1/places:searchText')\n"
    )
    assert found(maps, concat) == [
        f"{maps}:ids: URL {NO_ASK}",
        f"{maps}:text_ids: BASE {NO_ASK}",
    ]
    # The proxy flag from a variable is the paid proxy too; Fetcher passes it.
    flagged = (
        "USE = True\nasync def shop(f, url):\n    return await f.get(url, proxy=USE)\n"
    )
    assert found(uzum, flagged) == [f"{uzum}:shop: proxy=USE {NO_ASK}"]
    passing = (
        "class Fetcher:\n"
        "    async def get(self, url, proxy=False):\n"
        "        return await self._send(url, proxy=proxy)\n"
        "async def plain(f, url):\n"
        "    return await f.get(url, proxy=False)\n"
    )
    assert found("services/http.py", passing) == []
    # A name is read where it was bound: hh's API constant does not make
    # OLX's namesake an API, and httpx.URL is not serp's URL.
    namesake = {"collectors/hh_vacancies.py": "URL = 'https://api.hh.ru/vacancies'\n"}
    olx_html = (
        "URL = 'https://www.olx.uz/d/obyavleniya/'\n"
        "async def page(f):\n    return await f.get(URL, obey_robots=False)\n"
    )
    assert found(olx, olx_html, namesake) == [f"{olx}:page: {OFF_API}"]
    serp_url = {"services/serp.py": GUARDED_SERP.replace("BRAVE_URL", "URL")}
    host_of = "import httpx\ndef host_of(url):\n    return httpx.URL(url).host\n"
    assert found("services/http.py", host_of, serp_url) == []
    # Prose and bare hosts are not calls.
    prose = (
        '"""We call https://api.search.brave.com through costs.ask."""\n'
        'RPS = {"api.apify.com": 1.0}\n'
    )
    assert found("services/http.py", prose) == []
