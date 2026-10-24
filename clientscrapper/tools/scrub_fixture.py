"""Make a recorded page safe to commit (07 §1.1): person data out, shape kept.

    python -m clientscrapper.tools.scrub_fixture tests/fixtures/olx_business/search.html

Rewrites each file in place. Inside one file the same real value always maps
to the same synthetic one, numbered in order of first appearance:

* a phone (``PHONE_RE`` and a valid ``phone_e164``) → ``+998 90 000 00 NN``,
  digit for digit, so the separators, the brackets and the length stay;
* a Telegram ``@handle`` / ``t.me/<name>`` and an Instagram handle →
  ``namuna_NN`` (reserved paths, bots, CSS ``@media`` and JSON-LD ``@context``
  are left alone);
* a person-shaped name in a ``user.name``-shaped field (``user``/``seller``/
  ``owner``/``author``/``contact`` → ``name``, or ``user_name``,
  ``sellerName``, ``first_name`` …) and a person-shaped name beside
  ``IP``/``ИП``/``YaTT``/``ЯТТ`` → ``Seller NN``; a firm's name stays.

JSON is parsed and written back in its own layout (indent, separators,
``\\uXXXX`` escapes), so keys and list lengths are unchanged; HTML and text
are rewritten in place, so every tag and attribute stays and selectors still
match. ``needs_scrub`` is the recorder's ``--scrub auto`` rule.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from clientscrapper.collectors.keywords import IG_RE, PHONE_RE, STIR_RE, TG_RE
from clientscrapper.services.keys import instagram_key, name_key, phone_e164, phone_tail
from clientscrapper.services.normalise import is_person_shaped, telegram_key

FixtureKind = Literal["json", "html", "xml", "text"]

# Person-tier sources (07 §1.1): always scrubbed under --scrub auto.
PERSON_TIER_SOURCES: Final = frozenset(
    {"olx_business", "instagram_discovery", "telegram_monitor"}
)
SYNTHETIC_PREFIX: Final = "998"
PERSON_PARENTS: Final = frozenset(
    {"user", "seller", "owner", "author", "contact", "person", "profile"}
)
NESTED_NAME_KEYS: Final = frozenset(
    {"name", "first_name", "last_name", "full_name", "display_name", "fullName",
     "displayName", "firstName", "lastName"}
)  # fmt: skip
FLAT_NAME_KEYS: Final = frozenset(
    {"user_name", "userName", "seller_name", "sellerName", "contact_name",
     "contactName", "author_name", "authorName", "owner_name", "ownerName",
     "first_name", "firstName", "last_name", "lastName", "full_name", "fullName"}
)  # fmt: skip
# «@media», «@context»: CSS at-rules and JSON-LD keywords are not handles.
AT_KEYWORDS: Final = frozenset(
    {"media", "import", "charset", "namespace", "supports", "keyframes", "layer",
     "container", "property", "document", "viewport", "context", "graph", "value",
     "index", "reverse", "version", "vocab", "language", "nest", "included"}
)  # fmt: skip

_UPPER = "A-ZА-ЯЁЎҚҒҲ"
_WORD = rf"[{_UPPER}][\w'ʻʼ.\-]*"
_FORM = r"(?:IP|ИП|YaTT|ЯТТ)"
_FORM_THEN_NAME = re.compile(rf"(?<!\w)({_FORM})(\s+)({_WORD}(?:\s+{_WORD}){{0,2}})")
_NAME_THEN_FORM = re.compile(
    rf"(?<!\w)((?:{_WORD}\s+){{0,2}}{_WORD})(\s+)({_FORM})(?!\w)"
)
_JSON_STRING = r'(\\?")((?:[^"\\]|\\u[0-9a-fA-F]{4}|\\[^"u])*)(\\?")'
_FLAT_FIELD = re.compile(
    rf'(\\?"(?:{"|".join(sorted(FLAT_NAME_KEYS))})\\?"\s*:\s*){_JSON_STRING}'
)
_PARENT_OBJECT = re.compile(
    rf'(\\?"(?:{"|".join(sorted(PERSON_PARENTS))})\\?"\s*:\s*\{{)([^{{}}]*)'
)
_NESTED_FIELD = re.compile(
    rf'(\\?"(?:{"|".join(sorted(NESTED_NAME_KEYS))})\\?"\s*:\s*){_JSON_STRING}'
)


def _swap_digits(raw: str, digits: str) -> str:
    """Replace the digits of ``raw`` with the tail of ``digits``, keeping every
    other character where it was."""
    count = sum(ch.isdigit() for ch in raw)
    fresh = iter(digits[-count:].rjust(count, "0"))
    return "".join(next(fresh) if ch.isdigit() else ch for ch in raw)


def _decoded(value: str) -> str:
    try:
        decoded = json.loads(f'"{value}"')
    except ValueError:
        return value
    return decoded if isinstance(decoded, str) else value


def _person_shaped(value: str) -> bool:
    return bool(value.strip()) and is_person_shaped(name_key(value))


@dataclass(frozen=True, slots=True)
class ScrubResult:
    body: bytes
    counts: dict[str, int]


class Scrubber:
    """One file's mapping: real value → synthetic number, in order of first
    appearance. A new Scrubber per file."""

    def __init__(self) -> None:
        self._phones: dict[str, int] = {}
        self._handles: dict[str, int] = {}
        self._names: dict[str, int] = {}
        self.replaced: Counter[str] = Counter()

    # -- values ---------------------------------------------------------------------

    def phone(self, raw: str) -> str:
        e164 = phone_e164(raw)
        tail = phone_tail(e164) if e164 else None
        if tail is None:
            return raw
        n = self._phones.setdefault(tail, len(self._phones) + 1)
        self.replaced["phones"] += 1
        return _swap_digits(raw, f"{SYNTHETIC_PREFIX}90{n:07d}")

    def handle(self, key: str) -> str:
        n = self._handles.setdefault(key, len(self._handles) + 1)
        self.replaced["handles"] += 1
        return f"namuna_{n:02d}"

    def name(self, value: str) -> str:
        n = self._names.setdefault(name_key(value), len(self._names) + 1)
        self.replaced["names"] += 1
        return f"Seller {n:02d}"

    # -- text -----------------------------------------------------------------------

    def text(self, text: str) -> str:
        text = _FLAT_FIELD.sub(self._field, text)
        text = _PARENT_OBJECT.sub(
            lambda m: m.group(1) + _NESTED_FIELD.sub(self._field, m.group(2)), text
        )
        text = _FORM_THEN_NAME.sub(
            lambda m: m.group(1) + m.group(2) + self._maybe_name(m.group(3)), text
        )
        text = _NAME_THEN_FORM.sub(
            lambda m: self._maybe_name(m.group(1)) + m.group(2) + m.group(3), text
        )
        text = self._phones_in(text)
        text = TG_RE.sub(self._telegram, text)
        return self._instagram_in(text)

    def _phones_in(self, text: str) -> str:
        # A labelled STIR («ИНН 300000041») is a firm's tax id, not a phone —
        # the same exception normalise.parse_phones makes.
        stirs = [m.span(1) for m in STIR_RE.finditer(text)]

        def swap(match: re.Match[str]) -> str:
            start, end = match.span()
            if any(s < end and start < e for s, e in stirs):
                return match.group(0)
            return self.phone(match.group(0))

        return PHONE_RE.sub(swap, text)

    def _instagram_in(self, text: str) -> str:
        # IG_RE's label branch can swallow the host of a link written after the
        # label («Insta: instagram.com/x»); then search again one character on,
        # where the link branch matches.
        out: list[str] = []
        done = start = 0
        while (match := IG_RE.search(text, start)) is not None:
            if match.group(1).lower().startswith(("instagram.", "www.instagram.")):
                start = match.start() + 1
                continue
            out += [text[done : match.start()], self._instagram(match)]
            done = start = match.end()
        out.append(text[done:])
        return "".join(out)

    def _field(self, match: re.Match[str]) -> str:
        value = _decoded(match.group(3))
        if not _person_shaped(value):
            return match.group(0)
        return match.group(1) + match.group(2) + self.name(value) + match.group(4)

    def _maybe_name(self, words: str) -> str:
        return self.name(words) if _person_shaped(words) else words

    def _telegram(self, match: re.Match[str]) -> str:
        whole, user = match.group(0), match.group(1)
        if whole.startswith("@"):
            if user.lower() in AT_KEYWORDS:
                return whole
            after = match.string[match.end() : match.end() + 1]
            if after == "/":  # «@scope/package», not a person
                return whole
        key = telegram_key(whole)
        if key is None:
            return whole
        return whole[: match.start(1) - match.start(0)] + self.handle(f"tg:{key}")

    def _instagram(self, match: re.Match[str]) -> str:
        key = instagram_key(match.group(1))
        if key is None:
            return match.group(0)
        prefix = match.group(0)[: match.start(1) - match.start(0)]
        return prefix + self.handle(f"ig:{key}")

    # -- JSON -----------------------------------------------------------------------

    def tree(
        self, value: Any, *, key: str | None = None, parent: str | None = None
    ) -> Any:
        if isinstance(value, dict):
            return {k: self.tree(v, key=k, parent=key) for k, v in value.items()}
        if isinstance(value, list):
            return [self.tree(v, key=key, parent=parent) for v in value]
        if isinstance(value, str):
            named = key in FLAT_NAME_KEYS or (
                key in NESTED_NAME_KEYS and parent in PERSON_PARENTS
            )
            if named and _person_shaped(value):
                return self.name(value)
            return self.text(value)
        if isinstance(value, int) and not isinstance(value, bool):
            digits = str(value)
            if PHONE_RE.fullmatch(digits) and phone_e164(digits):
                return int(self.phone(digits))
        return value


def _json_layout(text: str) -> dict[str, Any]:
    stripped = text.strip()
    indent: int | None = None
    lines = stripped.splitlines()
    if len(lines) > 1:
        second = lines[1]
        indent = len(second) - len(second.lstrip(" ")) or None
    compact = '":' in stripped and '": ' not in stripped
    return {
        "indent": indent,
        "separators": (",", ":")
        if compact
        else ((",", ": ") if indent else (", ", ": ")),
        "ensure_ascii": "\\u0" in text,
    }


def scrub_bytes(body: bytes, *, kind: FixtureKind) -> ScrubResult:
    """``body`` with person data replaced; see the module docstring."""
    try:
        text, encoding = body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = body.decode("latin-1"), "latin-1"
    scrubber = Scrubber()
    out: str | None = None
    if kind == "json":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            layout = _json_layout(text)
            out = json.dumps(scrubber.tree(data), **layout)
            if text.endswith("\n"):
                out += "\n"
    if out is None:
        out = scrubber.text(text)
    return ScrubResult(out.encode(encoding), dict(scrubber.replaced))


def has_entrepreneur_name(text: str) -> bool:
    """A person-shaped name beside IP/ИП/YaTT/ЯТТ: a person-tier page."""
    return any(_person_shaped(m.group(3)) for m in _FORM_THEN_NAME.finditer(text)) or any(
        _person_shaped(m.group(1)) for m in _NAME_THEN_FORM.finditer(text)
    )


def needs_scrub(source_key: str, body: bytes) -> bool:
    """``--scrub auto``: person-tier sources, and any page naming an
    individual entrepreneur. Company-tier public pages stay verbatim."""
    if source_key in PERSON_TIER_SOURCES:
        return True
    return has_entrepreneur_name(body.decode("utf-8", errors="ignore"))


_KINDS: Final[dict[str, FixtureKind]] = {
    ".json": "json",
    ".html": "html",
    ".htm": "html",
    ".xml": "xml",
}


def kind_of(path: Path) -> FixtureKind:
    return _KINDS.get(path.suffix.lower(), "text")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrub recorded fixtures in place (07 §1.1)."
    )
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    for path in args.files:
        result = scrub_bytes(path.read_bytes(), kind=kind_of(path))
        path.write_bytes(result.body)
        counts = (
            ", ".join(f"{k}={v}" for k, v in sorted(result.counts.items())) or "nothing"
        )
        print(f"{path}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
