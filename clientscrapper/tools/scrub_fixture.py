"""Make a recorded page safe to commit (07 §1.1): person data out, shape kept.

    python -m clientscrapper.tools.scrub_fixture tests/fixtures/olx_business/search.html

Rewrites each file in place. Inside one file the same real value always maps
to the same synthetic one, numbered in order of first appearance:

* a phone → ``+998 90 000 00 NN`` digit for digit, so the separators, the
  brackets and the length stay; a foreign ``+<country>`` number keeps its
  country code and gets zeros and NN (``+86 000 0000 0001``). A number counts
  as a phone when it is written as one — a ``998`` / ``+`` prefix, separators,
  a ``tel:`` link, a phone label or key in front of it — or, nine bare digits,
  when it starts with a mobile operator code and stands in no id-shaped key,
  attribute or STIR-labelled cell. So a STIR (``"inn": "301234567"``,
  ``<td>STIR</td><td>301234567</td>``) and a platform id (``"ad_id":
  901234567``) stay as the parser tests need them;
  hh's split phone (``country``/``city``/``number`` in one object) is one
  number, its parts rewritten in place;
* a Telegram ``@handle`` / ``t.me/<name>``, an Instagram handle and the value
  of a ``username``-shaped key → ``namuna_NN`` (reserved paths, bots, CSS
  ``@media`` and JSON-LD ``@context`` are left alone);
* an e-mail address → ``namuna_NN@example.uz`` (one already at an
  ``example.*`` domain stays, unless its local part is a person's name);
* a person-shaped name in a ``user.name``-shaped field (``user``/``seller``/
  ``owner``/``author``/``contact``/``contacts`` → ``name`` at any depth inside
  them, or
  ``user_name``, ``sellerName``, ``first_name`` …), beside ``IP``/``ИП``/
  ``YaTT``/``ЯТТ``, and — in HTML — a whole text node or a ``title``/``alt``/
  ``content`` attribute that is person-shaped → ``Seller NN``; a firm's name
  stays.

Then every real handle and full name found is replaced wherever else it is
written (a relative ``/malika.karimova/`` link), and ``Scrubber.meta`` does the
same to the URL and the headers the recorder stores beside the file, with the
same mapping.

JSON is parsed and written back in its own layout (indent, separators,
``\\uXXXX`` escapes), so keys and list lengths are unchanged; HTML and text
are rewritten in place, so every tag and attribute stays and selectors still
match. JSON embedded in a page (OLX ``__PRERENDERED_STATE__``, escaped or
not) is walked with balanced braces. ``needs_scrub`` is the recorder's
``--scrub auto`` rule: a person-tier source, a page naming an individual
entrepreneur, or one carrying a person-shaped name in a person field.
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

from clientscrapper.collectors.keywords import (
    EMAIL_RE,
    IG_RE,
    LEGAL_FORM_RE,
    PHONE_RE,
    STIR_RE,
    TG_RE,
)
from clientscrapper.services.keys import (
    MOBILE_CODES,
    MOBILE_CODES_UNCONFIRMED,
    instagram_key,
    name_key,
    phone_e164,
    phone_tail,
)
from clientscrapper.services.normalise import is_person_shaped, telegram_key

FixtureKind = Literal["json", "html", "xml", "text"]

# Person-tier sources (07 §1.1): always scrubbed under --scrub auto. 07 §1.1
# names the first three; hh_vacancies (a vacancy's «contacts» block is the HR
# person: name, e-mail, phones) and uzum_sellers (a seller is often a YaTT,
# a person) carry people too, and 07 §1.0 lets no real person into tests/.
PERSON_TIER_SOURCES: Final = frozenset(
    {
        "olx_business",
        "instagram_discovery",
        "telegram_monitor",
        "hh_vacancies",
        "uzum_sellers",
    }
)
SYNTHETIC_PREFIX: Final = "998"
PERSON_PARENTS: Final = frozenset(
    {"user", "seller", "owner", "author", "contact", "contacts", "contact_person",
     "contactPerson", "person", "profile"}
)  # fmt: skip
# An e-mail at one of these (or a subdomain) is already synthetic (07 §1.0).
SYNTHETIC_MAIL_DOMAINS: Final = (
    "example.uz",
    "example.com",
    "example.org",
    "example.net",
)
# hh.ru's split phone ({"country": "998", "city": "90", "number": "000-00-01",
# "formatted": "+998 (90) 000-00-01"}): the three parts are one number.
PHONE_PARTS: Final = ("country", "city", "number")
NESTED_NAME_KEYS: Final = frozenset(
    {"name", "first_name", "last_name", "full_name", "display_name", "fullName",
     "displayName", "firstName", "lastName"}
)  # fmt: skip
FLAT_NAME_KEYS: Final = frozenset(
    {"user_name", "userName", "seller_name", "sellerName", "contact_name",
     "contactName", "author_name", "authorName", "owner_name", "ownerName",
     "first_name", "firstName", "last_name", "lastName", "full_name", "fullName"}
)  # fmt: skip
# Keys whose value is a person's handle on the platform.
HANDLE_KEYS: Final = frozenset(
    {"username", "screen_name", "screenName", "login", "nickname", "handle",
     "tg_username", "ig_username", "instagram", "telegram"}
)  # fmt: skip
# «@media», «@context»: CSS at-rules and JSON-LD keywords are not handles.
AT_KEYWORDS: Final = frozenset(
    {"media", "import", "charset", "namespace", "supports", "keyframes", "layer",
     "container", "property", "document", "viewport", "context", "graph", "value",
     "index", "reverse", "version", "vocab", "language", "nest", "included"}
)  # fmt: skip
# Country codes a foreign number in these sources carries (three digits first).
_COUNTRY_CODES: Final = (
    frozenset({"996", "992", "993", "994", "995", "971", "972", "380", "375"}),
    frozenset({"86", "90", "82", "81", "91", "92", "93", "98", "44", "49", "33"}),
    frozenset({"7", "1"}),
)

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
_PARENT_KEY = re.compile(rf'(\\?)"(?:{"|".join(sorted(PERSON_PARENTS))})\\?"\s*:\s*\{{')
_NESTED_FIELD = re.compile(
    rf'(\\?"(?:{"|".join(sorted(NESTED_NAME_KEYS))})\\?"\s*:\s*){_JSON_STRING}'
)
_HANDLE_FIELD = re.compile(
    rf'(\\?"(?:{"|".join(sorted(HANDLE_KEYS))})\\?"\s*:\s*\\?")([A-Za-z0-9._]{{2,32}})(\\?")'
)
_TEXT_NODE = re.compile(r">([^<>]{2,80})<")
# What a name written out looks like: letters, with spaces, dots, apostrophes
# or hyphens between them — not «@handle_90», not a path, not a number.
_WRITTEN_NAME = re.compile(r"[^\W\d_]+(?:[\s.'ʻʼ\-]+[^\W\d_]+)*\.?")
_NAME_ATTR = re.compile(
    r"""(\s(?:title|alt|content|aria-label|data-name)\s*=\s*)(["'])([^"'<>]{2,80})\2""",
    re.I,
)

# A '+<country>' number, 10–15 digits with single separators (not +998).
_INTL_PHONE = re.compile(r"(?<![\w+])\+(?!998)\d(?:[\s\-.()]{0,2}\d){8,14}(?!\d)")
# A STIR label a few tags or spaces before nine digits (a table row).
_STIR_NEAR = re.compile(
    r"(?<!\w)(?:STIR|СТИР|ИНН|INN|TIN)(?:\s|[:№#]|<[^<>]{0,80}>){0,6}(\d{9})(?!\d)",
    re.I,
)
# What stands right before nine bare digits.
_PHONE_LABEL = re.compile(
    r"(?i)(?:tel|тел|phone|телефон|telefon|mob|моб|aloqa|raqam|номер|whatsapp"
    r"|☎|📞)[^\d\n<>]{0,12}\Z"
)
_ID_BEFORE = re.compile(
    r"""(?i)[\\"']*[\w.\-]*(?:id|ids|inn|stir|tin|okpo|oked|code|sku|number|no)"""
    r"""[\\"']*\s*[:=]\s*[\\"']*\Z"""
)
_IN_ATTRIBUTE = re.compile(r"""<[^<>]*\s[\w:.\-]+\s*=\s*["'][^"'<>]*\Z""")
_ID_KEY = re.compile(
    r"(?:\A(?i:id|ids|inn|stir|tin|okpo|oked|sku|code)"
    r"|[_\-.](?i:id|ids|code|inn|stir|tin)|[a-z0-9](?:Id|Ids|ID|IDs|Code))\Z"
)
_PHONE_KEY = re.compile(
    r"(?i:phone|mobile|msisdn|whatsapp|telefon|телефон)"
    r"|(?:\A|[_\-.])(?i:tel)(?:\Z|[_\-.])|[a-z]Tel(?:\Z|[A-Z_])"
)
_HANDLE_VALUE = re.compile(r"@?[A-Za-z0-9._]{2,32}")
# t.me/s/<channel> (the web preview TG_RE leaves alone).
_TG_PREVIEW = re.compile(r"(t\.me/s/)([A-Za-z][A-Za-z0-9_]{4,31})(?![A-Za-z0-9_])")
# Nine bare digits starting so are a phone; 20/50/55 too, though not yet
# usable lead phones (keys.MOBILE_CODES_UNCONFIRMED): a fixture errs private.
_BARE_PHONE_CODES: Final = MOBILE_CODES | MOBILE_CODES_UNCONFIRMED


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


def _written_name(text: str) -> bool:
    """A whole text node or attribute that is a person's name written out.
    «Karimov MChJ» is a firm named after its owner: a firm's name stays."""
    text = text.strip()
    return (
        _WRITTEN_NAME.fullmatch(text) is not None
        and LEGAL_FORM_RE.search(text) is None
        and _person_shaped(text)
    )


def _country_code(digits: str, raw: str) -> str:
    first = re.match(r"\+?(\d+)", raw)
    group = first.group(1) if first else ""
    if 1 <= len(group) <= 3:
        return group
    for size, codes in zip((3, 2, 1), _COUNTRY_CODES, strict=True):
        if digits[:size] in codes:
            return digits[:size]
    return digits[:2]


def _closing_brace(text: str, start: int, *, escaped: bool) -> int | None:
    """Index of the ``}`` closing the ``{`` at ``start``; quotes are skipped.
    In JSON embedded in a JS string a quote is ``\\"`` (one backslash, or 4k+1),
    a quote inside a value ``\\\\\\"`` (three)."""
    depth = 0
    in_string = False
    for i in range(start, len(text)):
        ch = text[i]
        if ch == '"':
            run = 0
            while i - run - 1 >= 0 and text[i - run - 1] == "\\":
                run += 1
            if (run % 4 == 1) if escaped else (run % 2 == 0):
                in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


@dataclass(frozen=True, slots=True)
class ScrubResult:
    body: bytes
    counts: dict[str, int]


class Scrubber:
    """One file's mapping: real value → synthetic number, in order of first
    appearance. A new Scrubber per file (and its sidecar metadata)."""

    def __init__(self) -> None:
        self._phones: dict[str, int] = {}
        self._handles: dict[str, int] = {}
        self._names: dict[str, int] = {}
        self._emails: dict[str, int] = {}
        self._handle_raw: dict[str, str] = {}
        self._name_raw: dict[str, str] = {}
        self.replaced: Counter[str] = Counter()

    # -- values ---------------------------------------------------------------------

    def phone(self, raw: str) -> str:
        e164 = phone_e164(raw)
        if e164 is None:
            return raw
        if not e164.startswith("+" + SYNTHETIC_PREFIX):
            return self._foreign(raw, e164)
        tail = phone_tail(e164)
        if tail is None:
            return raw
        n = self._phones.setdefault(tail, len(self._phones) + 1)
        self.replaced["phones"] += 1
        return _swap_digits(raw, f"{SYNTHETIC_PREFIX}90{n:07d}")

    def _foreign(self, raw: str, e164: str) -> str:
        digits = e164[1:]
        n = self._phones.setdefault(f"+{digits}", len(self._phones) + 1)
        self.replaced["phones"] += 1
        code = _country_code(digits, raw)
        return _swap_digits(raw, code + str(n).zfill(len(digits) - len(code)))

    def email(self, raw: str) -> str:
        local, domain = raw.rsplit("@", 1)
        synthetic_domain = any(
            domain.lower() == d or domain.lower().endswith("." + d)
            for d in SYNTHETIC_MAIL_DOMAINS
        )
        # «info@namuna.example.uz» is made up; «malika.karimova@example.uz»
        # still names somebody.
        if synthetic_domain and not _person_shaped(re.sub(r"[._\-]+", " ", local)):
            return raw
        n = self._emails.setdefault(raw.lower(), len(self._emails) + 1)
        self.replaced["emails"] += 1
        return f"namuna_{n:02d}@example.uz"

    def phone_parts(self, parts: dict[str, Any]) -> dict[str, Any] | None:
        """hh's split phone as one number, each part rewritten with the digits
        the whole number gets; None when the parts are not a phone."""
        values = [parts.get(k) for k in PHONE_PARTS]
        if not all(isinstance(v, str | int) and not isinstance(v, bool) for v in values):
            return None
        country, city, number = (str(v) for v in values)
        raw = f"+{country} {city} {number}"
        if phone_e164(raw) is None:
            return None
        digits = re.sub(r"\D", "", self.phone(raw))
        sizes = [sum(ch.isdigit() for ch in part) for part in (country, city, number)]
        if sum(sizes) != len(digits):
            return None
        out: list[str] = []
        at = 0
        for part, size in zip((country, city, number), sizes, strict=True):
            out.append(_swap_digits(part, digits[at : at + size]))
            at += size
        fresh = dict(parts)
        for key, value, new in zip(PHONE_PARTS, values, out, strict=True):
            fresh[key] = int(new) if isinstance(value, int) else new
        return fresh

    def handle(self, key: str, raw: str | None = None) -> str:
        n = self._handles.setdefault(key, len(self._handles) + 1)
        self.replaced["handles"] += 1
        synthetic = f"namuna_{n:02d}"
        if raw is not None and len(raw) >= 5:
            self._handle_raw.setdefault(raw.lower(), synthetic)
        return synthetic

    def name(self, value: str) -> str:
        n = self._names.setdefault(name_key(value), len(self._names) + 1)
        self.replaced["names"] += 1
        synthetic = f"Seller {n:02d}"
        if len(value.strip()) >= 4:
            self._name_raw.setdefault(value.strip().lower(), synthetic)
        return synthetic

    def handle_value(self, value: str) -> str | None:
        """A ``username``-shaped value as a synthetic handle, None if not one
        (a URL under such a key goes through ``text``, which keeps its shape)."""
        if not _HANDLE_VALUE.fullmatch(value):
            return None
        value = value.lstrip("@")
        tg = telegram_key(value)
        if tg is not None:
            return self.handle(f"tg:{tg}", value)
        ig = instagram_key(value)
        if ig is not None:
            return self.handle(f"ig:{ig}", value)
        return None

    # -- text -----------------------------------------------------------------------

    def text(self, text: str, *, phone_field: bool = False, markup: bool = False) -> str:
        """``phone_field``: the value of a phone-labelled key, where every
        valid number is a phone however it is written. ``markup``: HTML/XML,
        where a whole text node or a name attribute can be a person's name."""
        text = EMAIL_RE.sub(lambda m: self.email(m.group(0)), text)
        text = _FLAT_FIELD.sub(self._field, text)
        text = self._parents_in(text)
        text = _HANDLE_FIELD.sub(self._handle_field, text)
        text = _FORM_THEN_NAME.sub(
            lambda m: m.group(1) + m.group(2) + self._maybe_name(m.group(3)), text
        )
        text = _NAME_THEN_FORM.sub(
            lambda m: self._maybe_name(m.group(1)) + m.group(2) + m.group(3), text
        )
        if markup:
            text = _TEXT_NODE.sub(self._text_node, text)
            text = _NAME_ATTR.sub(self._name_attr, text)
        text = self._phones_in(text, force=phone_field)
        text = TG_RE.sub(self._telegram, text)
        text = _TG_PREVIEW.sub(
            lambda m: m.group(1) + self.handle(f"tg:{m.group(2).lower()}", m.group(2)),
            text,
        )
        return self._instagram_in(text)

    def known(self, text: str) -> str:
        """Every real handle and name this Scrubber has replaced, replaced again
        wherever else it is written."""
        # A lone first name is a weak key and a common word in firm names; only
        # full names are replaced wherever else they are written.
        names = {raw: syn for raw, syn in self._name_raw.items() if " " in raw}
        pairs = {**self._handle_raw, **names}
        if not pairs:
            return text
        alternatives = "|".join(
            re.escape(raw) for raw in sorted(pairs, key=len, reverse=True)
        )
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_.])(?:{alternatives})(?![A-Za-z0-9_]|\.[A-Za-z0-9_])", re.I
        )
        return pattern.sub(lambda m: pairs[m.group(0).lower()], text)

    def meta(self, value: str) -> str:
        """A URL or a header value stored beside the file: the same mapping."""
        return self.known(self.text(value))

    def _text_node(self, match: re.Match[str]) -> str:
        inner = match.group(1)
        if not _written_name(inner):
            return match.group(0)
        lead = inner[: len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()) :]
        return f">{lead}{self.name(inner.strip())}{trail}<"

    def _name_attr(self, match: re.Match[str]) -> str:
        value = match.group(3)
        if not _written_name(value):
            return match.group(0)
        return match.group(1) + match.group(2) + self.name(value) + match.group(2)

    def _parents_in(self, text: str) -> str:
        out: list[str] = []
        done = 0
        for match in _PARENT_KEY.finditer(text):
            if match.start() < done:
                continue  # inside a parent already walked
            open_at = match.end() - 1
            close_at = _closing_brace(text, open_at, escaped=bool(match.group(1)))
            if close_at is None:
                continue
            out += [
                text[done : open_at + 1],
                _NESTED_FIELD.sub(self._field, text[open_at + 1 : close_at]),
            ]
            done = close_at
        out.append(text[done:])
        return "".join(out)

    def _phones_in(self, text: str, *, force: bool = False) -> str:
        stirs = [m.span(1) for m in STIR_RE.finditer(text)]
        stirs += [m.span(1) for m in _STIR_NEAR.finditer(text)]
        edits: list[tuple[int, int, str]] = []

        def taken(start: int, end: int) -> bool:
            return any(s < end and start < e for s, e, _ in edits) or any(
                s < end and start < e for s, e in stirs
            )

        for match in _INTL_PHONE.finditer(text):
            digits = re.sub(r"\D", "", match.group(0))
            if 10 <= len(digits) <= 15 and phone_e164(match.group(0)):
                edits.append((*match.span(), self.phone(match.group(0))))
        for match in PHONE_RE.finditer(text):
            start, end = match.span()
            if taken(start, end) or not self._is_phone(text, match, force=force):
                continue
            edits.append((start, end, self.phone(match.group(0))))
        out: list[str] = []
        done = 0
        for start, end, new in sorted(edits):
            out += [text[done:start], new]
            done = end
        out.append(text[done:])
        return "".join(out)

    @staticmethod
    def _is_phone(text: str, match: re.Match[str], *, force: bool) -> bool:
        raw = match.group(0)
        e164 = phone_e164(raw)
        if e164 is None:
            return False
        if force or not raw.isdigit() or raw.startswith(SYNTHETIC_PREFIX):
            return True
        # Nine bare digits: a phone typed without spaces, or an id, a STIR, a
        # price. Decide by what stands in front of it.
        before = text[max(0, match.start() - 200) : match.start()]
        if _PHONE_LABEL.search(before):
            return True
        if _ID_BEFORE.search(before) or _IN_ATTRIBUTE.search(before):
            return False
        return e164[4:6] in _BARE_PHONE_CODES

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

    def _handle_field(self, match: re.Match[str]) -> str:
        synthetic = self.handle_value(match.group(2))
        if synthetic is None:
            return match.group(0)
        return match.group(1) + synthetic + match.group(3)

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
        prefix = whole[: match.start(1) - match.start(0)]
        return prefix + self.handle(f"tg:{key}", user)

    def _instagram(self, match: re.Match[str]) -> str:
        key = instagram_key(match.group(1))
        if key is None:
            return match.group(0)
        prefix = match.group(0)[: match.start(1) - match.start(0)]
        return prefix + self.handle(f"ig:{key}", match.group(1))

    # -- JSON -----------------------------------------------------------------------

    def tree(
        self, value: Any, *, key: str | None = None, parent: str | None = None
    ) -> Any:
        if isinstance(value, dict):
            whole = self.phone_parts(value) if set(PHONE_PARTS) <= set(value) else None
            return {
                k: (
                    whole[k]
                    if whole is not None and k in PHONE_PARTS
                    else self.tree(v, key=k, parent=key)
                )
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.tree(v, key=key, parent=parent) for v in value]
        if isinstance(value, str):
            return self._tree_string(value, key=key, parent=parent)
        if isinstance(value, int) and not isinstance(value, bool):
            return self._tree_int(value, key=key)
        return value

    def _tree_string(self, value: str, *, key: str | None, parent: str | None) -> str:
        named = key in FLAT_NAME_KEYS or (
            key in NESTED_NAME_KEYS and parent in PERSON_PARENTS
        )
        if named and _person_shaped(value):
            return self.name(value)
        if key in HANDLE_KEYS:
            synthetic = self.handle_value(value)
            if synthetic is not None:
                return ("@" if value.startswith("@") else "") + synthetic
        if key is not None and _ID_KEY.search(key) and re.fullmatch(r"[\d\s\-]+", value):
            return value  # a STIR, an ad id: the parser tests read it
        return self.text(
            value, phone_field=key is not None and bool(_PHONE_KEY.search(key))
        )

    def _tree_int(self, value: int, *, key: str | None) -> int:
        digits = str(value)
        if key is not None and _ID_KEY.search(key):
            return value
        phone_key = key is not None and _PHONE_KEY.search(key) is not None
        prefixed = len(digits) == 12 and digits.startswith(SYNTHETIC_PREFIX)
        if (phone_key or prefixed) and PHONE_RE.fullmatch(digits) and phone_e164(digits):
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


def scrub_bytes(
    body: bytes, *, kind: FixtureKind, scrubber: Scrubber | None = None
) -> ScrubResult:
    """``body`` with person data replaced; see the module docstring. Pass the
    ``scrubber`` on to ``Scrubber.meta`` for what is stored beside the file."""
    try:
        text, encoding = body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = body.decode("latin-1"), "latin-1"
    scrubber = scrubber if scrubber is not None else Scrubber()
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
        out = scrubber.text(text, markup=kind in ("html", "xml"))
    out = scrubber.known(out)
    return ScrubResult(out.encode(encoding), dict(scrubber.replaced))


def has_entrepreneur_name(text: str) -> bool:
    """A person-shaped name beside IP/ИП/YaTT/ЯТТ: a person-tier page."""
    return any(_person_shaped(m.group(3)) for m in _FORM_THEN_NAME.finditer(text)) or any(
        _person_shaped(m.group(1)) for m in _NAME_THEN_FORM.finditer(text)
    )


def has_person_field(text: str) -> bool:
    """A person-shaped name in a person field (``user.name``, ``contacts.name``,
    ``seller_name`` …), as the scrubber itself reads one."""
    probe = Scrubber()
    probe.text(text)
    return probe.replaced["names"] > 0


def needs_scrub(source_key: str, body: bytes) -> bool:
    """``--scrub auto``: person-tier sources, any page naming an individual
    entrepreneur, and any page carrying a person-shaped name in a person
    field (whatever its source: a directory's API answer with a «contacts»
    block is a person's). Company-tier public pages stay verbatim."""
    if source_key in PERSON_TIER_SOURCES:
        return True
    text = body.decode("utf-8", errors="ignore")
    return has_entrepreneur_name(text) or has_person_field(text)


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
