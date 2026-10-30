"""A small, forgiving HTML tree for the page parsers (pure, stdlib only).

The parsers of enrichment (``services/registry``, ``collectors/company_site``,
``collectors/telegram_page``) read server-rendered pages with no browser and
no third-party HTML library (``requirements.txt`` carries none, 03 §1.5:
«every collector is a pure parser over recorded fixtures»). ``parse`` builds
an element tree with ``html.parser``: void elements close themselves, an end
tag closes the nearest open element of its name (anything opened inside it
with it — a missing ``</p>`` or ``</li>`` costs nothing), an unmatched end tag
is ignored. ``Node.text`` is what a reader sees: script, style, template and
noscript bodies are left out, entities are decoded, whitespace collapsed.
``Node.raw`` keeps a script's body (the JSON-LD block is read from it).

Nothing here fetches or decides; ``decode`` picks the charset a page states.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Final

VOID: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
# Bodies a reader never sees (their text is kept in ``raw`` only).
HIDDEN: Final = frozenset({"script", "style", "template", "noscript"})
# Elements a line of text ends at (``Node.lines``).
BLOCK: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_SPACE: Final = re.compile(r"\s+")
_CHARSET: Final = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", re.I)


@dataclass(eq=False, slots=True)
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = None

    # -- attributes -------------------------------------------------------------------

    def get(self, name: str) -> str | None:
        return self.attrs.get(name)

    def classes(self) -> frozenset[str]:
        return frozenset((self.attrs.get("class") or "").split())

    def has_class(self, *names: str) -> bool:
        held = self.classes()
        return all(n in held for n in names)

    # -- walking ------------------------------------------------------------------------

    def iter(self) -> Iterator[Node]:
        """Every element below this one, in document order (not itself)."""
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(
        self, tag: str | None = None, *, cls: str | None = None, **attrs: str
    ) -> list[Node]:
        """Elements named ``tag`` (any when None) carrying the class ``cls``
        and every attribute in ``attrs`` with that exact value (``aria_label``
        stands for ``aria-label``)."""
        wanted = {k.replace("_", "-"): v for k, v in attrs.items()}
        return [
            n
            for n in self.iter()
            if (tag is None or n.tag == tag)
            and (cls is None or cls in n.classes())
            and all(n.attrs.get(k) == v for k, v in wanted.items())
        ]

    def find(
        self, tag: str | None = None, *, cls: str | None = None, **attrs: str
    ) -> Node | None:
        found = self.find_all(tag, cls=cls, **attrs)
        return found[0] if found else None

    def ancestors(self) -> Iterator[Node]:
        current = self.parent
        while current is not None:
            yield current
            current = current.parent

    def elements(self) -> list[Node]:
        """The direct child elements."""
        return [c for c in self.children if isinstance(c, Node)]

    # -- text --------------------------------------------------------------------------

    def _pieces(self, out: list[str], *, lines: bool) -> None:
        for child in self.children:
            if isinstance(child, str):
                # A newline in the source is layout, not a line of the page.
                out.append(child.replace("\n", " ") if lines else child)
            elif child.tag not in HIDDEN:
                if lines and child.tag in BLOCK:
                    out.append("\n")
                child._pieces(out, lines=lines)
                if lines and child.tag in BLOCK:
                    out.append("\n")

    def text(self) -> str:
        """The visible text, whitespace collapsed."""
        out: list[str] = []
        self._pieces(out, lines=False)
        return _SPACE.sub(" ", "".join(out)).strip()

    def lines(self) -> list[str]:
        """The visible text cut at block elements, each line collapsed, empty
        ones dropped."""
        out: list[str] = []
        self._pieces(out, lines=True)
        found = []
        for line in "".join(out).split("\n"):
            collapsed = _SPACE.sub(" ", line).strip()
            if collapsed:
                found.append(collapsed)
        return found

    def raw(self) -> str:
        """The element's text as written, hidden bodies included (a script)."""
        out: list[str] = []
        for child in self.children:
            out.append(child if isinstance(child, str) else child.raw())
        return "".join(out)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(
            tag.lower(),
            {name.lower(): (value or "") for name, value in attrs},
            parent=self.current,
        )
        self.current.children.append(node)
        if node.tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(
            tag.lower(),
            {name.lower(): (value or "") for name, value in attrs},
            parent=self.current,
        )
        self.current.children.append(node)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in VOID:
            return
        probe: Node | None = self.current
        while probe is not None and probe is not self.root:
            if probe.tag == name:
                self.current = probe.parent or self.root
                return
            probe = probe.parent
        # An end tag nothing opened: ignored.

    def handle_data(self, data: str) -> None:
        if data:
            self.current.children.append(data)


def parse(markup: str) -> Node:
    """The page as a tree under a ``#root`` node."""
    builder = _Builder()
    builder.feed(markup)
    builder.close()
    return builder.root


def decode(body: bytes, content_type: str = "") -> str:
    """The page's text in the charset its header or its ``<meta>`` names,
    UTF-8 otherwise; an unknown charset or a bad byte never raises."""
    charset: str | None = None
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    if match is not None:
        charset = match.group(1)
    else:
        meta = _CHARSET.search(body[:4096])
        if meta is not None:
            charset = meta.group(1).decode("ascii", errors="ignore")
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def json_ld(root: Node) -> list[Any]:
    """Every ``application/ld+json`` block's objects, flattened one level
    (a list block gives its items, ``@graph`` its members); a block that is
    not JSON is skipped."""
    found: list[Any] = []
    for script in root.find_all("script"):
        if (script.get("type") or "").strip().lower() != "application/ld+json":
            continue
        try:
            data = json.loads(script.raw())
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                found.extend(item["@graph"])
            else:
                found.append(item)
    return found


def collapse(text: str | None) -> str | None:
    """``text`` with whitespace collapsed; None for nothing left."""
    if text is None:
        return None
    collapsed = _SPACE.sub(" ", text).strip()
    return collapsed or None


__all__ = ["BLOCK", "HIDDEN", "VOID", "Node", "collapse", "decode", "json_ld", "parse"]
