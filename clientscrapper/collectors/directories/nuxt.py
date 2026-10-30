"""The Nuxt 3 server payload (``script#__NUXT_DATA__``) as plain values (T-22).

yellowpages.uz and top.uz render on one Nuxt platform: everything a parser
needs is in the payload, devalue-encoded — a flat JSON array whose element 0
is the root; an object's values and an array's items are indices into the
array; a value that is a string, number, boolean or null stands for itself;
a list whose first item is a type name is a wrapped value
(``["ShallowReactive", i]``, ``["Reactive", i]``, ``["Ref", i]``,
``["Set", i, …]``, ``["Map", k, v, …]``, ``["Date", "<iso>"]``,
``["null"]`` …); negative indices are devalue's constants (−1 undefined,
−2 a hole, −3 NaN, −4/−5 ±infinity, −6 −0), read here as None.
(tests/fixtures/directories/README.md, yellowpages «Rendering».)
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final

from clientscrapper.collectors.directories.markup import Node

# Wrappers that hold one value: the value is the wrapped one.
_WRAPPERS: Final = frozenset(
    {
        "ShallowReactive",
        "Reactive",
        "Ref",
        "ShallowRef",
        "EmptyRef",
        "EmptyShallowRef",
        "NuxtError",
        "Island",
    }
)


def resolve(data: list[Any]) -> Any:
    """The devalue array → the value it encodes (shared references stay one
    object; a cycle ends at the object already being built)."""
    memo: dict[int, Any] = {}

    def value(index: Any) -> Any:
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if index < 0 or index >= len(data):
            return None
        if index in memo:
            return memo[index]
        item = data[index]
        if isinstance(item, dict):
            mapping: dict[str, Any] = {}
            memo[index] = mapping
            for key, ref in item.items():
                mapping[str(key)] = value(ref)
            return mapping
        if isinstance(item, list):
            if item and isinstance(item[0], str):
                return _special(index, item)
            items: list[Any] = []
            memo[index] = items
            items.extend(value(ref) for ref in item)
            return items
        memo[index] = item
        return item

    def _special(index: int, item: list[Any]) -> Any:
        kind = item[0]
        if kind in _WRAPPERS:
            memo[index] = None
            out = value(item[1]) if len(item) > 1 else None
        elif kind == "Set":
            out = [value(ref) for ref in item[1:]]
        elif kind == "Map":
            pairs = range(1, len(item) - 1, 2)
            out = {str(value(item[i])): value(item[i + 1]) for i in pairs}
        elif kind in ("Date", "RegExp", "BigInt", "URL"):
            out = item[1] if len(item) > 1 else None
        else:  # "null", "Object" without a body, anything unknown
            out = None
        memo[index] = out
        return out

    return value(0) if data else None


def payload(root: Node) -> Any:
    """The resolved ``__NUXT_DATA__`` of a page, or None (no payload, or not
    the devalue shape)."""
    script = root.find("script", attr=("id", "__NUXT_DATA__"))
    if script is None:
        return None
    try:
        data = json.loads(script.raw())
    except ValueError:
        return None
    return resolve(data) if isinstance(data, list) else None


def mappings(
    value: Any, keep: Callable[[Mapping[str, Any]], bool]
) -> Iterator[dict[str, Any]]:
    """Every dict inside ``value`` (itself included) that ``keep`` accepts, in
    document order; each visited once."""
    seen: set[int] = set()
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict | list):
            if id(item) in seen:
                continue
            seen.add(id(item))
        if isinstance(item, dict):
            if keep(item):
                yield item
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def list_block(value: Any) -> dict[str, Any] | None:
    """The organisations list of a list page: the dict holding ``data`` (a list
    of organisations, each with a ``slug``) and ``meta`` (with
    ``current_page``) — yellowpages' rubrika/teg pages and top.uz's sections."""
    for block in mappings(
        value,
        lambda m: isinstance(m.get("data"), list)
        and isinstance(m.get("meta"), dict)
        and "current_page" in m["meta"],
    ):
        return block
    return None


def organisation(value: Any, slug: str, *, needs: str) -> dict[str, Any] | None:
    """The organisation a card page is about: the dict with ``slug == slug``
    and the key ``needs`` (``contacts`` on yellowpages, ``phones`` on top.uz)."""
    for org in mappings(value, lambda m: m.get("slug") == slug and needs in m):
        return org
    return None
