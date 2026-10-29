"""A tolerant HTML tree over the standard library's tokenizer (T-22).

No HTML library is a dependency of this repo (``requirements.txt``), and the
directory parsers need little: a node's tag, attributes and classes, its
children, its text, and the raw text of a ``<script>`` (JSON-LD, the Nuxt
payload). ``html.parser`` tokenizes; this module builds the tree the way a
browser would for the markup the recorded pages carry: void elements take no
children, an end tag closes the nearest open element of its name (and every
element opened inside it), a stray end tag is ignored. Entities are decoded
in text and attribute values; a script's or a style's content is kept raw.

Selectors are plain predicates (``find_all(tag, cls=…, attr=…)``): a parser
names exactly the elements it reads — the goldenpages bot trap (hidden ``/gp/``
links under ever-changing wrappers, 03 §2.6) is avoided by never following a
link a selector did not pick.
"""

from __future__ import annotations

from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Final

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
# Elements a browser lays out as blocks (and ``br``): their text is spaced.
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
# Elements whose text is not the page's visible text.
NOT_TEXT: Final = frozenset({"script", "style", "noscript", "template"})


class Node:
    """One element: ``tag``, ``attrs`` (first occurrence of a name wins),
    ``children`` (nodes and text strings), ``parent``."""

    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []
        self.parent = parent

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.tag} {self.attrs!r}>"

    def get(self, name: str) -> str | None:
        return self.attrs.get(name)

    @property
    def classes(self) -> frozenset[str]:
        return frozenset((self.attrs.get("class") or "").split())

    def has_class(self, *names: str) -> bool:
        return set(names) <= self.classes

    def elements(self) -> Iterator[Node]:
        """Every descendant element, depth first, in document order."""
        stack: list[Node] = [c for c in reversed(self.children) if isinstance(c, Node)]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(c for c in reversed(node.children) if isinstance(c, Node))

    def find_all(
        self,
        tag: str | None = None,
        *,
        cls: str | tuple[str, ...] = (),
        attr: tuple[str, str] | None = None,
        prefix: tuple[str, str] | None = None,
    ) -> list[Node]:
        """Descendants of ``tag`` (any when None) carrying every class in
        ``cls``, an attribute equal to ``attr = (name, value)`` and one
        starting with ``prefix = (name, start)``."""
        wanted = (cls,) if isinstance(cls, str) else cls
        found: list[Node] = []
        for node in self.elements():
            if tag is not None and node.tag != tag:
                continue
            if wanted and not node.has_class(*wanted):
                continue
            if attr is not None and node.attrs.get(attr[0]) != attr[1]:
                continue
            if prefix is not None and not (node.attrs.get(prefix[0]) or "").startswith(
                prefix[1]
            ):
                continue
            found.append(node)
        return found

    def find(
        self,
        tag: str | None = None,
        *,
        cls: str | tuple[str, ...] = (),
        attr: tuple[str, str] | None = None,
        prefix: tuple[str, str] | None = None,
    ) -> Node | None:
        hits = self.find_all(tag, cls=cls, attr=attr, prefix=prefix)
        return hits[0] if hits else None

    def text(self) -> str:
        """The visible text below this node, whitespace collapsed (scripts,
        styles and templates left out)."""
        parts: list[str] = []
        stack: list[Node | str] = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.tag not in NOT_TEXT:
                # A block's text never glues to its neighbour's.
                edge = [" "] if item.tag in BLOCK else []
                stack.extend([*edge, *reversed(item.children), *edge])
        return " ".join("".join(parts).split())

    def raw(self) -> str:
        """The node's own text children, as written (a script's content)."""
        return "".join(c for c in self.children if isinstance(c, str))

    def ancestors(self) -> Iterator[Node]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self.stack: list[Node] = [self.root]

    def _node(self, tag: str, attrs: list[tuple[str, str | None]]) -> Node:
        values: dict[str, str] = {}
        for name, value in attrs:
            values.setdefault(name, value or "")
        node = Node(tag, values, self.stack[-1])
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._node(tag, attrs)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._node(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse(body: bytes) -> Node:
    """The document tree of ``body`` (UTF-8; a bad byte is replaced)."""
    builder = _Builder()
    builder.feed(body.decode("utf-8", errors="replace"))
    builder.close()
    return builder.root


def scripts(root: Node, *, type_: str) -> list[str]:
    """The raw content of every ``<script type=type_>``."""
    return [node.raw() for node in root.find_all("script", attr=("type", type_))]
