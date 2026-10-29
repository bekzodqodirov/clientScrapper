"""What the four directory parsers share (03 §2.6, T-22): the list-page and
card shapes the crawl plan reads, the one signal rule, and small text helpers.

A list page is a ``ListPage``: its companies as ``Entry`` rows (the
``source_ref``, the card URL a selector picked — never a link found by walking
the page — and the row's own observation when the row alone is worth keeping),
the page's own next link as printed (None where pagination is not followed),
and the organiser's figures where the page prints them.

**The signal rule** (``judge``), the same for every site:

* a cargo firm is a competitor and nothing else: ``COMPETITOR_NAME_RE`` on the
  name, ``COMPETITOR_SELF_RE`` (the cargo core, 03 §1.4) on the card's own text
  — its description, rubrics and tags — so an importer that delivers is no
  forwarder (07 T-22, carried from wave 3; 04 §6 gate 2);
* ``china_origin`` from the China cue the spec names for the site (a
  goldenpages rubric «… из Китая», a yellowpages tag ``tovary-iz-kitaya`` /
  ``mebel-iz-kitaya``);
* the category filter: goods in a bulky category (``signals.BULKY`` — the 17
  ``BULKY_CATEGORIES`` less stationery and cosmetics) or a China cue keep the
  card's volume-type signals; anything else — food, metal, agri, stationery
  wholesale with no China cue — is **held** (``attrs.hold = "category"``): the
  observation is kept (a directory is the strongest phone source) but carries
  no ``directory_wholesale``, ``brand_distributor`` or ``marketplace_velocity``,
  so the scorer leaves it cold, i.e. in ``hold``;
* ``directory_wholesale`` only for a wholesale rubric or section
  (``WHOLESALE_RUBRIC_RE``); ``brand_distributor`` (``official`` False) for a
  goldenpages «Импортер» badge; ``marketplace_velocity`` for a glotr seller
  with ≥ ``SKU_VELOCITY`` products; ``entity_solid`` for a glotr seller
  «Проверено … Soliq.uz».
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.directories.markup import parse as parse_markup
from clientscrapper.collectors.keywords import (
    COMPETITOR_NAME_RE,
    COMPETITOR_SELF_RE,
    INDIVIDUAL_FORM_RE,
    LEGAL_FORM_RE,
    PHONE_RE,
    WHOLESALE_RUBRIC_RE,
    bulky_categories,
)
from clientscrapper.services import evidence as evidence_store
from clientscrapper.services.signals import BULKY

EXCERPT_MAX: Final = evidence_store.EXCERPT_MAX
# 03 §2.6 glotr: «seller with ≥ 20 SKUs … → marketplace_velocity».
SKU_VELOCITY: Final = 20
HELD: Final = "category"
# A phone's identity for de-duplication: its last nine digits.
_TAIL: Final = 9
SEPARATOR: Final = " · "
# Two numbers in one field («(998) 90-000-00-33, 90-000-00-34»).
_LIST_SPLIT: Final = re.compile(r"[;,]")
# Line and block ends inside a payload's HTML field: a space, so two lines do
# not glue into one word.
_BREAKS: Final = re.compile(r"<br\s*/?>|</br>|</p>|</div>|</li>", re.I)


@dataclass(frozen=True, slots=True)
class Entry:
    """One company on a list page. ``complete``: the row carries the
    company's contacts and goods as the card does (the Nuxt payload rows of
    yellowpages and top.uz), so a held row needs no card request."""

    ref: str
    card_url: str | None
    observation: Observation | None = None
    complete: bool = False


@dataclass(frozen=True, slots=True)
class ListPage:
    entries: tuple[Entry, ...]
    next_url: str | None = None
    total: int | None = None
    page: int | None = None
    last_page: int | None = None


class ListParser(Protocol):
    def __call__(self, body: bytes, *, url: str, fetched_at: datetime) -> ListPage: ...


class CardParser(Protocol):
    def __call__(
        self, body: bytes, *, url: str, fetched_at: datetime
    ) -> list[Observation]: ...


def no_redirect_stop(asked: str, final: str) -> str | None:
    return None


@dataclass(frozen=True, slots=True)
class SiteSpec:
    """One directory as the crawl plan sees it. ``paginated``: the full crawl
    follows ``ListPage.next_url`` (False where pagination is unverified or
    robots.txt refuses it). ``redirect_stop(asked, final)`` names a paginator
    stop the final URL shows (goldenpages sends a page past the last back to
    page 1)."""

    site: str
    host: str
    source_key: str
    parse_list: ListParser
    parse_card: CardParser
    seeds: tuple[str, ...]
    paginated: bool
    redirect_stop: Callable[[str, str], str | None] = no_redirect_stop


@dataclass(frozen=True, slots=True)
class Verdict:
    signal_types: tuple[str, ...]
    held: bool = False
    competitor: bool = False
    origin_text: str | None = None
    bulky: tuple[str, ...] = field(default_factory=tuple)


def judge(
    *,
    name: str | None,
    own: str,
    goods: str,
    wholesale: bool,
    china: str | None,
    importer: bool = False,
    solid: bool = False,
    skus: int | None = None,
) -> Verdict:
    """The module docstring's rule for one card or row. ``own`` is the card's
    own text (description, rubrics, tags), ``goods`` what it sells, ``china``
    the verbatim China cue or None."""
    if (name and COMPETITOR_NAME_RE.search(name)) or COMPETITOR_SELF_RE.search(own):
        return Verdict(("competitor",), competitor=True)
    bulky = tuple(sorted(bulky_categories(goods) & BULKY))
    held = china is None and not bulky
    types: list[str] = []
    if china is not None:
        types.append("china_origin")
    if not held:
        if wholesale:
            types.append("directory_wholesale")
        if importer:
            types.append("brand_distributor")
        if skus is not None and skus >= SKU_VELOCITY:
            types.append("marketplace_velocity")
    if solid:
        types.append("entity_solid")
    return Verdict(tuple(types), held=held, origin_text=china, bulky=bulky)


def apply(obs: Observation, verdict: Verdict) -> Observation:
    """The verdict onto ``obs``: its types, its China fragment, the hold mark
    and the bulky keys in ``attrs``."""
    obs.signal_types = list(verdict.signal_types)
    obs.origin_text = verdict.origin_text
    if verdict.bulky:
        obs.attrs["bulky"] = list(verdict.bulky)
    if verdict.held:
        obs.attrs["hold"] = HELD
    if "brand_distributor" in verdict.signal_types:
        obs.attrs["official"] = False
    return obs


def wholesale(*names: str | None) -> bool:
    return any(n and WHOLESALE_RUBRIC_RE.search(n) for n in names)


def squash(text: str | None) -> str | None:
    """Whitespace collapsed; None for nothing."""
    if text is None:
        return None
    flat = " ".join(str(text).split())
    return flat or None


def plain(markup: str | None) -> str | None:
    """A payload field that carries HTML (an address with links, a
    description with ``<br>``) as its visible text."""
    if not markup:
        return None
    spaced = _BREAKS.sub(" ", markup)
    return squash(parse_markup(spaced.encode("utf-8")).text())


def strings(values: Any) -> list[str]:
    """The non-blank strings of a payload list (``null`` items and a ``null``
    list are nothing)."""
    if not isinstance(values, list):
        return []
    found: list[str] = []
    for value in values:
        text = squash(value) if isinstance(value, str) else None
        if text is not None:
            found.append(text)
    return found


def phones_as_written(values: Iterable[str]) -> list[str]:
    """The values that are phone numbers (``PHONE_RE``), as written, the same
    number (its last nine digits) once; a field holding two numbers split at
    its comma or semicolon."""
    found: list[str] = []
    seen: set[str] = set()
    parts = (part for value in values for part in _LIST_SPLIT.split(value))
    for value in parts:
        text = squash(value)
        if text is None or PHONE_RE.search(text) is None:
            continue
        digits = re.sub(r"\D", "", text)
        tail = digits[-_TAIL:]
        if len(tail) < _TAIL or tail in seen:
            continue
        seen.add(tail)
        found.append(text)
    return found


def legal_form_in(name: str | None) -> str | None:
    """The legal form a name carries, as written; a directory's own
    individual-entrepreneur abbreviation (``ИндП``, ``ИнП``) as ``ИП``."""
    if not name:
        return None
    forms = list(LEGAL_FORM_RE.finditer(name))
    following: list[re.Match[str] | None] = [*forms[1:], None]
    for here, after in zip(forms, following[: len(forms)], strict=True):
        # «ИП ООО», «СП OOO»: a foreign or joint enterprise whose form is the
        # company form that follows.
        if after is not None and not name[here.end() : after.start()].strip():
            continue
        return here.group(1)
    individual = INDIVIDUAL_FORM_RE.search(name)
    return "ИП" if individual is not None else None


def individual(name: str | None) -> bool:
    """The name is an individual entrepreneur's: the words before the form
    are a person's name, and the taxpayer number beside it a person's."""
    return bool(name and INDIVIDUAL_FORM_RE.search(name))


def excerpt(*parts: str | None) -> str:
    """The card's own words, joined, at most ``EXCERPT_MAX`` characters."""
    text = SEPARATOR.join(p for p in (squash(x) for x in parts) if p)
    # A cut at the limit must not end on a dangling separator.
    return evidence_store.clip(text, EXCERPT_MAX).rstrip(" ·")


def joined(parts: Iterable[str | None]) -> str | None:
    return squash(SEPARATOR.join(p for p in (squash(x) for x in parts) if p))


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def first_match(pattern: re.Pattern[str], texts: Iterable[str]) -> str | None:
    """The first text ``pattern`` finds something in (the whole text: a
    rubric's name is the fragment a card quotes)."""
    for text in texts:
        if pattern.search(text):
            return text
    return None
