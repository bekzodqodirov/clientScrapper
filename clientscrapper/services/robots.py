"""robots.txt read the way RFC 9309 reads it (03 §1.1).

``urllib.robotparser`` lets the first matching line win, knows neither ``*``
nor ``$``, and drops the first group of a file that starts with a byte-order
mark — so a site's ``Allow: /`` written above ``Disallow: /private/`` would
let us into /private/. This module parses the groups itself and decides by
the RFC:

* the groups naming our product token (case-insensitive, several merged into
  one), else the ``*`` groups, else no rules at all;
* the longest matching rule wins, ``Allow`` wins a tie;
* ``*`` matches any run of characters, a trailing ``$`` anchors the end;
* paths and patterns are compared after the same percent-encoding
  normalisation, so ``/%7Ekarim`` and ``/~karim`` are one path;
* ``/robots.txt`` itself is always allowed.

``Crawl-delay`` is not in the RFC; it is honoured by convention, read from the
same group. Pure: no I/O.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

__all__ = ["RobotsRules", "Rule", "parse_robots", "robots_path"]

_LINE: Final = re.compile(r"^\s*([A-Za-z][A-Za-z-]*)\s*:\s*(.*?)\s*$")
_UNRESERVED: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_HEX: Final = frozenset("0123456789abcdefABCDEF")
ROBOTS_FILE: Final = "/robots.txt"


def _normalise(text: str) -> str:
    """One spelling per path: escapes of unreserved characters decoded, other
    escapes upper-cased, non-ASCII characters percent-encoded as UTF-8."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        pair = text[i + 1 : i + 3]
        if ch == "%" and len(pair) == 2 and set(pair) <= _HEX:
            decoded = chr(int(pair, 16))
            out.append(decoded if decoded in _UNRESERVED else f"%{pair.upper()}")
            i += 3
            continue
        if ord(ch) > 0x7F:
            out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def robots_path(url: str) -> str:
    """What a rule is matched against: the path (``/`` when empty) and the
    query, normalised; never the fragment."""
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return _normalise(path)


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + (r"\Z" if anchored else ""), re.S)


@dataclass(frozen=True, slots=True)
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]

    @classmethod
    def of(cls, allow: bool, value: str) -> Rule:
        pattern = _normalise(value)
        return cls(allow, pattern, _compile(pattern))


@dataclass(frozen=True, slots=True)
class RobotsRules:
    """The rules that apply to one agent at one host."""

    rules: tuple[Rule, ...] = ()
    crawl_delay: float | None = None

    def allows(self, url: str) -> bool:
        path = robots_path(url)
        if path == ROBOTS_FILE:
            return True
        best: Rule | None = None
        for rule in self.rules:
            if rule.regex.match(path) is None:
                continue
            if (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow)
            ):
                best = rule
        return best is None or best.allow


@dataclass(slots=True)
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    delay: float | None = None


def _product_token(value: str) -> str:
    return value.split("/", 1)[0].strip().lower()


def _delay(value: str) -> float | None:
    try:
        delay = float(value)
    except ValueError:
        return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def _groups(text: str) -> list[_Group]:
    groups: list[_Group] = []
    current: _Group | None = None
    open_agents = False  # consecutive user-agent lines share one group
    for raw in text.lstrip("\ufeff").splitlines():
        match = _LINE.match(raw.split("#", 1)[0])
        if match is None:
            continue
        name, value = match.group(1).lower(), match.group(2)
        if name == "user-agent":
            if current is None or not open_agents:
                current = _Group()
                groups.append(current)
            current.agents.append(_product_token(value))
            open_agents = True
        elif name in ("allow", "disallow"):
            open_agents = False
            # Rules before any user-agent line belong to no group; an empty
            # value matches nothing (``Disallow:`` = allow everything).
            if current is not None and value:
                current.rules.append(Rule.of(name == "allow", value))
        elif name == "crawl-delay":
            open_agents = False
            if current is not None and current.delay is None:
                current.delay = _delay(value)
    return groups


def parse_robots(text: str, agent: str) -> RobotsRules:
    """The rules ``agent`` (a product token such as ``clientScrapperBot``) has
    to obey at a host whose robots.txt is ``text``."""
    token = agent.lower()
    groups = _groups(text)
    chosen = [g for g in groups if token in g.agents] or [
        g for g in groups if "*" in g.agents
    ]
    rules = tuple(rule for g in chosen for rule in g.rules)
    delays = [g.delay for g in chosen if g.delay is not None]
    return RobotsRules(rules=rules, crawl_delay=delays[0] if delays else None)
