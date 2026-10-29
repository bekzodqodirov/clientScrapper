"""``exhibitors`` — Uzbek exhibitors of the trade shows (03 §2.8).

An Uzbek firm with a stand at Automechanika, UzBuild, Power Uzbekistan,
CAITME, WoodTech & MebelExpo or TextileExpo imports at container scale
(``exhibitor`` 15). A batch collector driven by a calendar: the weekly run
(Monday 09:00, 03 §3) reads every show whose list is in its window and, once a
year, every past edition the show's own site still lists.

**The calendar** is ``sources.exhibitors.config.calendar`` — a list of
``{show, url_patterns, end_date}`` (plus, optionally, ``name``, ``expected``
— the organiser's own exhibitor count — and ``exclude``, names on the list
that are no importer: the organiser, a media partner). ``url_patterns`` is
two addresses: the show's index page, where its editions are listed, and the
address of an edition's rows with ``{edition}`` in place of the site's
edition id. While the row holds no ``calendar`` the code's
``DEFAULT_CALENDAR`` is the calendar (the six shows the recorded fixtures
prove, with the dates their sites print); an entry the collector cannot read
is skipped and named in ``meta.calendar``.

**The window rule** (03 §2.8): a show is crawled every Monday while
``end_date + 10 d ≤ today < end_date + 60 d``. Where the site prints an
edition's dates (the ERA edition ``<select>``) those dates are read too: the
index of an ERA show is read every Monday (one request a show), and an
edition it lists inside its window is crawled even when the calendar does
not name it (TextileExpo had two editions a year in 2024 and 2025 and one in
2026 — the site, not a fixed May-and-autumn pair, says when). Expodat prints
no dates: its index is read only for a calendar entry in its window or the
archive sweep. **The archive** — every edition the index lists whose window
is over — is swept once a year per show (``ARCHIVE_EVERY``): a run that read
every archive of a show names it in ``meta.archived``, and the next sweep of
that show waits 365 days from that run. A run reads every index first, then
every list in its window, then the archives, so a budget that runs out stops
an archive, never a show in its window. An ERA index whose select lists no
edition is a page the parser no longer reads (``index_empty``), never a
sweep done.

**Two platforms, one parser each** (``tests/fixtures/exhibitors/README.md``,
«Day-1 answers»): the expodat catalogue (Automechanika, Joomla ``com_expo``,
server-rendered pages of 48 rows, ``?start=<offset>``) and the ERA CMS of the
five Iteca shows (a DataTables JSON endpoint, ``length=500`` returns a whole
show). The spec's Bitrix archive (``PAGEN_2=N``) answers 404 now and the
Tilda page of Automechanika holds no rows: neither has a parser. A list is
paginated until a page is empty, a page repeats one already read, a page is
short of the page size, or the site's own count is reached
(``recordsTotal``; expodat's «End» link bounds it from below).

**Uzbek, not foreign.** Every recorded row carries its country (ERA's second
cell, expodat's flag title); a row is kept when that country is Uzbekistan
(``UZ_COUNTRIES``, any script) — or, only when a list prints no country, when
the name carries an Uzbek legal form (``UZ_LEGAL_FORMS``: LLC and OOO name
Russian firms as often, README item 12). Foreign rows are the supply side and
are not stored at all — they are counted (``meta.lists[].foreign``). Every
kept row stores ``attrs.country`` as the list prints it (04 §6 gate 3's
«exhibitor country CN» reads it).

**Observation** ``kind="exhibitor"``, ``source_ref=f"{show}:{year}:{slug}"``,
the name, ``attrs={show, year, edition, stand, pavilion, country}`` (+ the
venue on ERA lists; no list prints a pavilion or a sector, so ``pavilion`` is
None and ``goods_text`` empty), ``external_ids.exhibitor_slug`` =
``era:<company id>`` / ``expodat:<company id>`` (ERA ids are one register
across the Iteca sites: «GOLDEN PAGES» is 1341 on four of them), signal
``exhibitor``; a brand of ``collectors/brands.py`` in the name or the
description → ``brand_distributor`` with ``attrs.official=False`` and
``attrs.brand`` the brand as the row spells it (the signal rule's quote is
that fragment of the excerpt); a cargo firm's name (``COMPETITOR_NAME_RE``)
or self-description (``COMPETITOR_SELF_RE``) → ``competitor``. A name that
is a brand's own office (``brands.own_office``: «DAHUA TECHNOLOGY», «HUAWEI
DIGITAL POWER») is the supply side, like a foreign row: not stored, counted
``skipped``. The excerpt is the row's own cells joined by « · », each as
printed; ``published_at`` is the show's last day (``held_on``), so a row of
a past edition ages from the show, not from the crawl.

**Counts against the organiser.** A list walked to its end with fewer than
half the organiser's figure (``expected``), or an in-window list the site no
longer has, ends the run ``partial`` (``Collector.incomplete``) and raises
``exhibitor_list_short`` (06 §4.2) for the owner; a later run that reads the
same show whole clears its line, and a line expires with its window. A walk
the budget, the deadline or the breaker cut short is ``partial`` the
runner's way and is never judged short. Only the catalogue's own first page
can say a list is gone (it left the exposition, README «Files»); a captcha
or challenge page is no page at all (``served``: ``failed``, error
``captcha``).

Rate: every request goes through the Fetcher — robots.txt, 1 request per
3.3 s per host (``COLLECTOR_RPS_DEFAULT``), ≤ 100 pages a day (the row's
``daily_page_budget``); none of the seven hosts sets a ``Crawl-delay``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import parse_qs, urljoin, urlsplit

import sqlalchemy as sa

from clientscrapper.collectors.base import Budget, Collector, Observation, Placed
from clientscrapper.collectors.brands import BrandHit, brand_hits, own_office
from clientscrapper.collectors.keywords import (
    COMPETITOR_NAME_RE,
    COMPETITOR_SELF_RE,
    LEGAL_FORM_RE,
)
from clientscrapper.config import settings
from clientscrapper.db.models import CollectorRun, Source
from clientscrapper.services import health
from clientscrapper.services.http import Fetcher, FetchResult, RobotsDisallowed
from clientscrapper.services.text import fold_apostrophes, normalise_for_search, to_latin

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

Platform = Literal["era", "expodat"]
# A list read in its window, one read by the archive sweep, or (for an index
# that failed with nothing due) the show's weekly index alone.
Why = Literal["window", "archive", "index"]
ListStatus = Literal["complete", "truncated", "removed", "missing", "failed", "robots"]

SOURCE: Final = "exhibitors"
# 03 §2.8: crawl while end_date + 10 d ≤ today < end_date + 60 d.
WINDOW_OPENS: Final = timedelta(days=10)
WINDOW_CLOSES: Final = timedelta(days=60)
# 03 §2.8: «every archive once (yearly)».
ARCHIVE_EVERY: Final = timedelta(days=365)
# How far a printed edition date may lie from a calendar entry's end_date and
# still be that entry (TextileExpo 2027: the spec said May, the site 27–29
# April).
MATCH_SLACK: Final = timedelta(days=45)
# 03 §2.8: «a crawl under 50 % of the figure is partial and alerts».
SHORT_RATIO: Final = 0.5
# ERA: the site's own lengthMenu offers up to 5,000; 500 is the largest value
# recorded (uzbuild_2026_companies_all.json: all 341 rows in one answer).
ERA_PAGE: Final = 500
# A list longer than this many pages is a runaway (Automechanika: 10).
MAX_PAGES_PER_LIST: Final = 40
EXCERPT_MAX: Final = 600
SHORT_PROBLEM: Final = "exhibitor_list_short"
# Where a calendar's rows address takes the site's edition id.
EDITION_SLOT: Final = "{edition}"
# Country names an exhibitor list prints for Uzbekistan, compared after
# normalise_for_search (any case and script, apostrophes dropped):
# «Uzbekistan», «Узбекистан», «O'zbekiston», «Oʻzbekiston», «Ўзбекистон».
UZ_COUNTRIES: Final = frozenset({"uzbekistan", "ozbekiston", "uzbekiston"})
# The Uzbek legal forms of LEGAL_FORM_RE, upper-cased as written. Read only
# on a row whose list prints no country: OOO, ООО, LLC, JV, АО, ИП, ЧП, СП and
# ТОО name firms in Russia and Kazakhstan as often (README item 12: 40 of
# UzBuild 2026's 263 foreign rows carry LLC or OOO).
UZ_LEGAL_FORMS: Final = frozenset(
    {
        "MCHJ",
        "МЧЖ",
        "XK",
        "ХК",
        "QK",
        "ҚК",
        "AJ",
        "АЖ",
        "XCHK",
        "ХЧК",
        "YATT",
        "ЯТТ",
        "QOSHMA",
        "QO'SHMA",
    }
)
_MONTHS: Final = {
    name: number
    for number, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}
# «2024 (27 February - 1 March 2024)», «2026 (10 - 12 February 2026)».
_DATES_RE: Final = re.compile(
    r"\(\s*(\d{1,2})(?:\s+([A-Za-z]+))?\s*[-–—]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s*\)"
)
_YEAR_RE: Final = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_ERA_CARD_RE: Final = re.compile(r"exhibitor-info-(\d+)-(\d+)")
_EXPODAT_COMPANY_RE: Final = re.compile(r"/companies/company/(\d+)")
_EXPODAT_EXPOSITION_RE: Final = re.compile(r"/expositions/exposition/(\d+)(?![\d])")
_LIM_RE: Final = re.compile(r"\blim\s*=\s*['\"](\d+)['\"]")
_SHOW_KEY_RE: Final = re.compile(r"[a-z][a-z0-9_]{1,30}")
_SLUG_JUNK: Final = re.compile(r"[^a-z0-9]+")


# --- the calendar ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShowEntry:
    """One edition of a show in the calendar (03 §2.8)."""

    show: str
    url_patterns: tuple[str, str]  # (index page, rows address with {edition})
    end_date: date
    name: str = ""
    expected: int | None = None  # the organiser's own exhibitor count
    exclude: tuple[str, ...] = ()

    @property
    def platform(self) -> Platform:
        platform = platform_of(self.url_patterns[1])
        assert platform is not None  # calendar_from refuses any other entry
        return platform

    @property
    def title(self) -> str:
        return self.name or self.show


def platform_of(rows_pattern: str) -> Platform | None:
    """Which parser reads the rows behind ``rows_pattern``."""
    if "companies_list.php" in rows_pattern:
        return "era"
    if "/expositions/exposition/{edition}" in rows_pattern:
        return "expodat"
    return None


def _era(
    host: str, end: date, *, name: str, expected: int, exclude: tuple[str, ...]
) -> ShowEntry:
    return ShowEntry(
        show=_SHOW_OF_HOST[host],
        url_patterns=(
            f"https://{host}/en/exhibitors-list",
            f"https://{host}/ERAForms/companies_list.php?l=en&exhibition={{edition}}",
        ),
        end_date=end,
        name=name,
        expected=expected,
        exclude=exclude,
    )


_SHOW_OF_HOST: Final = {
    "uzbuild.uz": "uzbuild",
    "power-uzbekistan.uz": "power",
    "caitme.uz": "caitme",
    "mebelexpo.uz": "mebelexpo",
    "textileexpo.uz": "textileexpo",
}
# A directory publisher, on four of the five ERA lists (README item 12): no
# importer.
_ERA_EXCLUDE: Final = ("GOLDEN PAGES",)
_AUTOMECHANIKA: Final = (
    "https://automechanika.uz/eng/participants",
    "https://catalog.automechanika.uz/en/expositions/exposition/{edition}",
)
# The six shows the recorded fixtures prove (tests/fixtures/exhibitors), each
# edition with the end date its site prints (2026-10-29) and the organiser's
# figure 03 §2.8 cites — the last edition's where the next one has none yet.
# TransLogistica (competitors only), the CCPIT expo and the Chinese brands
# fair were never recorded: no entry, no parser.
DEFAULT_CALENDAR: Final[tuple[ShowEntry, ...]] = (
    ShowEntry(
        "automechanika",
        _AUTOMECHANIKA,
        date(2026, 10, 23),
        name="Automechanika Tashkent",
        expected=489,
        # The organiser itself is an Uzbekistan row (README item 6).
        exclude=("BMCA",),
    ),
    ShowEntry(
        "automechanika",
        _AUTOMECHANIKA,
        date(2027, 10, 22),
        name="Automechanika Tashkent",
        expected=489,
        exclude=("BMCA",),
    ),
    _era(
        "uzbuild.uz",
        date(2026, 2, 12),
        name="UzBuild",
        expected=418,
        exclude=_ERA_EXCLUDE,
    ),
    _era(
        "uzbuild.uz",
        date(2027, 3, 18),
        name="UzBuild",
        expected=418,
        exclude=_ERA_EXCLUDE,
    ),
    _era(
        "power-uzbekistan.uz",
        date(2026, 5, 14),
        name="Power Uzbekistan",
        expected=250,
        exclude=_ERA_EXCLUDE,
    ),
    _era(
        "power-uzbekistan.uz",
        date(2027, 5, 14),
        name="Power Uzbekistan",
        expected=250,
        exclude=_ERA_EXCLUDE,
    ),
    _era(
        "caitme.uz", date(2026, 9, 10), name="CAITME", expected=210, exclude=_ERA_EXCLUDE
    ),
    _era(
        "caitme.uz", date(2028, 9, 8), name="CAITME", expected=210, exclude=_ERA_EXCLUDE
    ),
    _era(
        "mebelexpo.uz",
        date(2026, 4, 30),
        name="WoodTech & MebelExpo",
        expected=127,
        exclude=_ERA_EXCLUDE,
    ),
    _era(
        "mebelexpo.uz",
        date(2027, 4, 29),
        name="WoodTech & MebelExpo",
        expected=127,
        exclude=_ERA_EXCLUDE,
    ),
    _era(
        "textileexpo.uz",
        date(2026, 5, 15),
        name="TextileExpo",
        expected=170,
        exclude=_ERA_EXCLUDE,
    ),
    _era(
        "textileexpo.uz",
        date(2027, 4, 29),
        name="TextileExpo",
        expected=170,
        exclude=_ERA_EXCLUDE,
    ),
)


def entry_config(entry: ShowEntry) -> dict[str, Any]:
    """``entry`` as ``sources.config.calendar`` holds it (JSON)."""
    found: dict[str, Any] = {
        "show": entry.show,
        "url_patterns": list(entry.url_patterns),
        "end_date": entry.end_date.isoformat(),
    }
    if entry.name:
        found["name"] = entry.name
    if entry.expected is not None:
        found["expected"] = entry.expected
    if entry.exclude:
        found["exclude"] = list(entry.exclude)
    return found


def _entry(raw: Any) -> ShowEntry | str:
    """One calendar item, or why it cannot be read."""
    if not isinstance(raw, Mapping):
        return "not an object"
    show = raw.get("show")
    if not isinstance(show, str) or not _SHOW_KEY_RE.fullmatch(show):
        return "show"
    patterns = raw.get("url_patterns")
    if (
        not isinstance(patterns, list | tuple)
        or len(patterns) != 2
        or not all(
            isinstance(p, str) and p.startswith(("https://", "http://")) for p in patterns
        )
    ):
        return f"{show}: url_patterns"
    index, rows = str(patterns[0]), str(patterns[1])
    if platform_of(rows) is None or EDITION_SLOT not in rows:
        return f"{show}: url_patterns[1] is no list address this collector reads"
    try:
        end = date.fromisoformat(str(raw.get("end_date")))
    except ValueError:
        return f"{show}: end_date"
    expected = raw.get("expected")
    if expected is not None and (
        isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0
    ):
        return f"{show}: expected"
    exclude = raw.get("exclude", ())
    if not isinstance(exclude, list | tuple) or not all(
        isinstance(x, str) for x in exclude
    ):
        return f"{show}: exclude"
    name = raw.get("name", "")
    return ShowEntry(
        show=show,
        url_patterns=(index, rows),
        end_date=end,
        name=name if isinstance(name, str) else "",
        expected=expected,
        exclude=tuple(exclude),
    )


def calendar_from(
    config: Mapping[str, Any] | None,
) -> tuple[tuple[ShowEntry, ...], list[str]]:
    """The calendar of ``sources.exhibitors.config`` and the items it could not
    read; ``DEFAULT_CALENDAR`` while the row holds no ``calendar`` (an empty
    list is the owner's «no show»)."""
    if not isinstance(config, Mapping) or "calendar" not in config:
        return DEFAULT_CALENDAR, []
    raw = config["calendar"]
    if not isinstance(raw, list):
        return (), ["calendar is not a list"]
    entries: list[ShowEntry] = []
    problems: list[str] = []
    for item in raw:
        entry = _entry(item)
        if isinstance(entry, str):
            problems.append(entry)
        else:
            entries.append(entry)
    return tuple(entries), problems


def in_window(end: date, today: date) -> bool:
    """03 §2.8: the list is complete and still online."""
    return end + WINDOW_OPENS <= today < end + WINDOW_CLOSES


def window_over(end: date, today: date) -> bool:
    return today >= end + WINDOW_CLOSES


@dataclass(frozen=True, slots=True)
class Visit:
    """One show this run reads: its index page, the calendar entries in their
    window today (``due``), and whether its yearly archive sweep is due."""

    show: str
    index_url: str
    rows_pattern: str
    platform: Platform
    entries: tuple[ShowEntry, ...]
    due: tuple[ShowEntry, ...]
    archive: bool


def plan_visits(
    calendar: Sequence[ShowEntry], today: date, archived: Iterable[str]
) -> list[Visit]:
    """The shows to read today, in-window ones first (pure). ``archived`` are
    the shows whose archive was swept in the last ``ARCHIVE_EVERY``. An ERA
    show is read every week: its index prints the dates of editions the
    calendar may not name."""
    swept = frozenset(archived)
    groups: dict[tuple[str, str, str], list[ShowEntry]] = {}
    for entry in calendar:
        key = (entry.show, *entry.url_patterns)
        groups.setdefault(key, []).append(entry)
    visits: list[Visit] = []
    for (show, index, rows), entries in groups.items():
        due = tuple(e for e in entries if in_window(e.end_date, today))
        archive = show not in swept
        platform = entries[0].platform
        if not due and not archive and platform != "era":
            continue
        visits.append(
            Visit(
                show=show,
                index_url=index,
                rows_pattern=rows,
                platform=platform,
                entries=tuple(entries),
                due=due,
                archive=archive,
            )
        )
    return sorted(visits, key=lambda v: not v.due)


# --- editions -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Edition:
    """An edition a show's index lists: the site's id, its year, its last day
    when the site prints it, and the address the index links (expodat)."""

    id: str
    year: int
    end: date | None = None
    label: str = ""
    url: str | None = None


def edition_dates(label: str) -> tuple[date, date] | None:
    """(first day, last day) of «2024 (27 February - 1 March 2024)» or
    «2026 (10 - 12 February 2026)»; None when the label prints no dates."""
    match = _DATES_RE.search(label)
    if match is None:
        return None
    first_day, first_month, last_day, last_month, year = match.groups()
    month = _MONTHS.get(last_month.lower())
    start_month = _MONTHS.get((first_month or last_month).lower())
    if month is None or start_month is None:
        return None
    try:
        end = date(int(year), month, int(last_day))
        start_year = int(year) - 1 if start_month > month else int(year)
        start = date(start_year, start_month, int(first_day))
    except ValueError:
        return None
    return start, end


def match_entry(edition: Edition, entries: Sequence[ShowEntry]) -> ShowEntry | None:
    """The calendar entry an edition is: the nearest end date within
    ``MATCH_SLACK`` when the site prints one, else the entry of its year."""
    if edition.end is not None:
        end = edition.end
        near = [e for e in entries if abs(e.end_date - end) <= MATCH_SLACK]
        return min(near, key=lambda e: abs(e.end_date - end)) if near else None
    same = [e for e in entries if e.end_date.year == edition.year]
    return same[0] if len(same) == 1 else None


@dataclass(frozen=True, slots=True)
class Target:
    """One list this run reads."""

    show: str
    edition: Edition
    why: Why
    entry: ShowEntry | None  # the calendar entry it is (its organiser count)


def pick_targets(
    visit: Visit, editions: Sequence[Edition], today: date
) -> tuple[list[Target], list[ShowEntry]]:
    """The lists to read of one show (pure), and the in-window calendar
    entries the index lists no edition for (``missing``: removed, or not
    published). An edition is read in its window — the calendar's or the
    printed one — and, on an archive sweep, once its window is over."""
    targets: list[Target] = []
    matched: set[ShowEntry] = set()
    seen: set[str] = set()
    for edition in editions:
        if edition.id in seen:
            continue
        seen.add(edition.id)
        entry = match_entry(edition, visit.entries)
        if entry is not None:
            matched.add(entry)
        end = edition.end or (entry.end_date if entry is not None else None)
        printed_due = edition.end is not None and in_window(edition.end, today)
        if (entry is not None and entry in visit.due) or printed_due:
            targets.append(Target(visit.show, edition, "window", entry))
        elif visit.archive and (
            window_over(end, today) if end is not None else edition.year < today.year
        ):
            targets.append(Target(visit.show, edition, "archive", entry))
    missing = [e for e in visit.due if e not in matched]
    # The lists in their window first: a budget that runs out stops an
    # archive, never this week's show.
    return sorted(targets, key=lambda t: t.why != "window"), missing


# --- a small HTML tree (the standard library's tokenizer) --------------------------

_VOID: Final = frozenset(
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


class _El:
    __slots__ = ("attrs", "children", "tag")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_El | str] = []

    def classes(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())

    def walk(self) -> Iterator[_El]:
        """Every element below this one, in document order."""
        stack: list[_El] = [c for c in reversed(self.children) if isinstance(c, _El)]
        while stack:
            el = stack.pop()
            yield el
            stack.extend(c for c in reversed(el.children) if isinstance(c, _El))

    def find_all(
        self, tag: str | None = None, *, cls: str | None = None, id: str | None = None
    ) -> list[_El]:
        return [
            el
            for el in self.walk()
            if (tag is None or el.tag == tag)
            and (cls is None or cls in el.classes())
            and (id is None or el.attrs.get("id") == id)
        ]

    def find(
        self, tag: str | None = None, *, cls: str | None = None, id: str | None = None
    ) -> _El | None:
        found = self.find_all(tag, cls=cls, id=id)
        return found[0] if found else None

    def text(self) -> str:
        parts: list[str] = []
        stack: list[_El | str] = [self]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                parts.append(node)
            elif node.tag not in {"script", "style"}:
                stack.extend(reversed(node.children))
        return clean(" ".join(parts)) or ""


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _El("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        el = _El(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(el)
        if tag not in _VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_El(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _tree(body: bytes | str) -> _El:
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    builder = _Builder()
    builder.feed(text)
    builder.close()
    return builder.root


# --- rows ----------------------------------------------------------------------------


def clean(text: str | None) -> str | None:
    """Whitespace collapsed (a no-break space too); None when nothing is left."""
    if text is None:
        return None
    joined = " ".join(text.split())
    return joined or None


def slug(name: str) -> str:
    """The name as a ``source_ref`` part: Latin, lower-case, words joined by
    «-» («JV LLC "UZCHASYS"» → ``jv-llc-uzchasys``)."""
    text = to_latin(fold_apostrophes(unicodedata.normalize("NFKC", name)).casefold())
    return _SLUG_JUNK.sub("-", text.replace("'", "")).strip("-")


def _key(text: str) -> str:
    return normalise_for_search(text)


def uz_form(name: str) -> str | None:
    """The Uzbek legal form ``name`` carries, as written (``UZ_LEGAL_FORMS``)."""
    for match in LEGAL_FORM_RE.finditer(name):
        written = match.group(1)
        if fold_apostrophes(written).upper() in UZ_LEGAL_FORMS:
            return written
    return None


def is_uzbek(country: str | None, name: str) -> bool:
    """03 §2.8: the country column says Uzbekistan; only where the list
    prints no country, an Uzbek legal form in the name."""
    if country:
        return _key(country) in UZ_COUNTRIES
    return uz_form(name) is not None


def _excerpt(cells: Iterable[str | None]) -> str:
    """The row's own cells, as printed, joined by « · »; cut at a word under
    ``EXCERPT_MAX``."""
    text = " · ".join(c for c in cells if c)
    if len(text) <= EXCERPT_MAX:
        return text
    cut = text[:EXCERPT_MAX]
    return cut.rsplit(" ", 1)[0].rstrip(" ·") if " " in cut else cut


@dataclass(frozen=True, slots=True)
class Row:
    """One exhibitor row as a list prints it."""

    name: str
    country: str | None
    stand: str | None
    company_id: str | None
    card_url: str | None
    venue: str | None = None  # ERA's «Location»
    description: str | None = None  # expodat's short description


@dataclass(frozen=True, slots=True)
class ListPage:
    """One page of an exhibitor list, read.

    ``kept`` are the observations of its Uzbek rows; ``seen`` counts every
    row; ``foreign`` the rows of another country (the supply side, not
    stored); ``skipped`` the rows the calendar excludes, a brand's own office
    (``brands.own_office``: the supply side too) or no name reads in;
    ``refs`` every row's id, for the repeat stop. ERA: ``total`` is the
    site's ``recordsTotal``. Expodat: ``no_list`` — the page has no
    ``#scroll_list``; ``removed`` — and it is no longer the exposition's
    page (README: the 2025 catalogue lands on the login page, «The event was
    not found»); ``page_size``, the next pages' address (``base``) and the
    «End» link's offset (``end_start``)."""

    kept: tuple[Observation, ...] = ()
    seen: int = 0
    foreign: int = 0
    skipped: int = 0
    refs: tuple[str, ...] = ()
    total: int | None = None
    no_list: bool = False
    removed: bool = False
    page_size: int | None = None
    base: str | None = None
    end_start: int | None = None


def held_on(edition: Edition, fetched_at: datetime, held: date | None = None) -> datetime:
    """When the show was (``Observation.published_at``): its last day —
    ``held`` (the calendar entry's), else the date the site prints, else the
    year's last day — never after the page was read. A list read a year on
    is last year's evidence, and its signal ages from the show, not from
    the crawl."""
    read_on = fetched_at.astimezone(settings.tz).date()
    day = held or edition.end or date(edition.year, 12, 31)
    return datetime.combine(min(day, read_on), time(), tzinfo=settings.tz)


def _brands(row: Row) -> list[BrandHit]:
    """The brands the row names — its name, then its description, each read
    on its own (a common-word brand needs its goods in the same text)."""
    hits: list[BrandHit] = []
    for text in (row.name, row.description):
        for hit in brand_hits(text or ""):
            if all(h.brand != hit.brand for h in hits):
                hits.append(hit)
    return hits


def _observation(
    row: Row,
    *,
    show: str,
    edition: Edition,
    platform: Platform,
    url: str,
    fetched_at: datetime,
    sha: str,
    cells: Sequence[str | None],
    held: date | None = None,
) -> Observation:
    attrs: dict[str, Any] = {
        "show": show,
        "year": edition.year,
        "edition": edition.id,
        "stand": row.stand,
        "pavilion": None,
        "country": row.country,
    }
    if row.venue:
        attrs["venue"] = row.venue
    signals = ["exhibitor"]
    excerpt = _excerpt(cells)
    hits = _brands(row)
    if hits:
        # ``attrs.brand`` is the brand as the row spells it, so the signal
        # rule's quote (signals._brand) is a fragment of the excerpt: the
        # first brand the excerpt still holds.
        hit = next((h for h in hits if h.written in excerpt), hits[0])
        signals.append("brand_distributor")
        attrs.update(
            brand=hit.written,
            brand_group=hit.brand.group,
            official=False,
            brands=[h.brand.name for h in hits],
        )
    if COMPETITOR_NAME_RE.search(row.name) or (
        row.description and COMPETITOR_SELF_RE.search(row.description)
    ):
        signals.append("competitor")
    form = LEGAL_FORM_RE.search(row.name)
    ref = slug(row.name) or f"id{row.company_id}"
    return Observation(
        source=SOURCE,
        source_ref=f"{show}:{edition.year}:{ref}",
        url=row.card_url or url,
        fetched_at=fetched_at,
        raw_sha256=sha,
        kind="exhibitor",
        company_name_raw=row.name,
        legal_form=form.group(1) if form else None,
        signal_types=signals,
        excerpt=excerpt,
        external_ids=(
            {"exhibitor_slug": f"{platform}:{row.company_id}"} if row.company_id else {}
        ),
        attrs=attrs,
        published_at=held_on(edition, fetched_at, held),
    )


def _sort_rows(
    rows: Iterable[tuple[Row, Sequence[str | None]]],
    *,
    show: str,
    edition: Edition,
    platform: Platform,
    url: str,
    fetched_at: datetime,
    sha: str,
    exclude: Iterable[str],
    held: date | None = None,
) -> tuple[list[Observation], int, int, int, list[str]]:
    excluded = {_key(name) for name in exclude}
    kept: list[Observation] = []
    seen = foreign = skipped = 0
    refs: list[str] = []
    for row, cells in rows:
        seen += 1
        refs.append(row.company_id or row.name)
        if not is_uzbek(row.country, row.name):
            foreign += 1
            continue
        if _key(row.name) in excluded or own_office(row.name) is not None:
            skipped += 1
            continue
        kept.append(
            _observation(
                row,
                show=show,
                edition=edition,
                platform=platform,
                url=url,
                fetched_at=fetched_at,
                sha=sha,
                cells=cells,
                held=held,
            )
        )
    return kept, seen, foreign, skipped, refs


def _cell(row: Sequence[Any], index: int) -> str | None:
    value = row[index] if index < len(row) else None
    return clean(value) if isinstance(value, str) else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _on_exposition(url: str, edition: str) -> bool:
    """``url`` is still the exposition page of ``edition`` (any of its
    pages: «…/exposition/7012-automechanika-….html?start=48»)."""
    match = _EXPODAT_EXPOSITION_RE.search(urlsplit(url).path)
    return match is not None and match.group(1) == edition


def _start_of(href: str) -> int | None:
    values = parse_qs(urlsplit(href).query).get("start")
    return _int(values[0]) if values else None


class Exhibitors(Collector):
    key = SOURCE
    family = "exhibitor"
    default_cron = "0 9 * * 1"
    default_rps = 0.33
    default_page_budget = 100

    def __init__(self, fetcher: Fetcher, serp: object | None = None) -> None:
        super().__init__(fetcher, serp)
        # Every list this run judged, in order (``meta.lists`` is their view).
        self.lists: list[ListResult] = []

    # --- parsers (pure) ----------------------------------------------------------

    @staticmethod
    def parse_era_editions(
        body: bytes, *, url: str, fetched_at: datetime
    ) -> list[Edition]:
        """The editions of an ERA ``/en/exhibitors-list`` page: the options of
        ``select[onchange^="ChangeYear"]`` (the raw attribute is
        ``ChangeYear(this);``), the value-less «Archive» placeholder left out.
        Each label prints the year and the dates («2026 (10 - 12 February
        2026)»)."""
        found: list[Edition] = []
        for select in _tree(body).find_all("select"):
            if not select.attrs.get("onchange", "").startswith("ChangeYear"):
                continue
            for option in select.find_all("option"):
                value = option.attrs.get("value", "").strip()
                label = option.text()
                if not value or not label:
                    continue
                dates = edition_dates(label)
                end = dates[1] if dates is not None else None
                year = _YEAR_RE.search(label)
                if year is not None:
                    found.append(Edition(value, int(year.group(1)), end, label))
                elif end is not None:
                    found.append(Edition(value, end.year, end, label))
        return found

    @staticmethod
    def parse_era_rows(
        body: bytes,
        *,
        url: str,
        fetched_at: datetime,
        show: str,
        edition: Edition,
        exclude: Iterable[str] = (),
        held: date | None = None,
    ) -> ListPage:
        """One answer of ``ERAForms/companies_list.php``: ``{draw,
        recordsTotal, recordsFiltered, data: [[company_html, country, stand,
        location, favourite_html], …]}`` (served as ``text/html``). The name
        is the text of the cell's ``<a>``, the card its ``href``
        (``/en/exhibitor-info-<edition>-<company id>``). Raises ``ValueError``
        on anything else (the bare endpoint answers HTTP 500, empty body).
        ``held``: the show's last day when the calendar knows it
        (``held_on``)."""
        answer = json.loads(body.decode("utf-8-sig"))
        if not isinstance(answer, dict) or not isinstance(answer.get("data"), list):
            raise ValueError("not a DataTables answer")
        rows: list[tuple[Row, Sequence[str | None]]] = []
        unreadable = 0
        for raw in answer["data"]:
            if not isinstance(raw, list) or not raw or not isinstance(raw[0], str):
                unreadable += 1
                continue
            anchor = _tree(raw[0]).find("a")
            name = anchor.text() if anchor is not None else clean(_tree(raw[0]).text())
            if not name:
                unreadable += 1
                continue
            href = anchor.attrs.get("href", "") if anchor is not None else ""
            card = _ERA_CARD_RE.search(href)
            country, stand, venue = _cell(raw, 1), _cell(raw, 2), _cell(raw, 3)
            row = Row(
                name=name,
                country=country,
                stand=stand,
                company_id=card.group(2) if card else None,
                card_url=urljoin(url, href) if href else None,
                venue=venue,
            )
            rows.append((row, (name, country, stand, venue)))
        kept, seen, foreign, skipped, refs = _sort_rows(
            rows,
            show=show,
            edition=edition,
            platform="era",
            url=url,
            fetched_at=fetched_at,
            sha=hashlib.sha256(body).hexdigest(),
            exclude=exclude,
            held=held,
        )
        return ListPage(
            kept=tuple(kept),
            seen=seen + unreadable,
            foreign=foreign,
            skipped=skipped + unreadable,
            refs=tuple(refs),
            total=_int(answer.get("recordsTotal")),
        )

    @staticmethod
    def parse_expodat_links(
        body: bytes, *, url: str, fetched_at: datetime
    ) -> list[Edition]:
        """The expodat catalogues an index page links (Automechanika's Tilda
        pages: «Exhibitors Catalogue 2026» →
        ``…/en/expositions/exposition/7012``), each with the year its text
        names; a link that names no year is left out."""
        found: list[Edition] = []
        for anchor in _tree(body).find_all("a"):
            href = urljoin(url, anchor.attrs.get("href", "").strip())
            match = _EXPODAT_EXPOSITION_RE.search(urlsplit(href).path)
            year = _YEAR_RE.search(anchor.text())
            if match is None or year is None:
                continue
            found.append(
                Edition(
                    id=match.group(1),
                    year=int(year.group(1)),
                    label=anchor.text(),
                    url=href,
                )
            )
        return found

    @staticmethod
    def parse_expodat_page(
        body: bytes,
        *,
        url: str,
        fetched_at: datetime,
        show: str,
        edition: Edition,
        exclude: Iterable[str] = (),
        held: date | None = None,
    ) -> ListPage:
        """One page of an expodat catalogue (``url`` = the address the page
        was served from). Row = ``#scroll_list a[href*="/companies/company/"]``:
        the name is the anchor's ``title`` (entities unescaped), the stand
        ``.numb_stand b`` (absent on some rows), the country
        ``.img_country[title]``, the description ``.short_opis``. A page past
        the end says «no results found» (0 rows); a page with no
        ``#scroll_list`` at all is ``no_list``, and ``removed`` when ``url``
        is no longer the edition's exposition (README: «final URL ≠ the
        exposition, no ``#scroll_list``»)."""
        tree = _tree(body)
        scroll = tree.find(id="scroll_list")
        if scroll is None:
            return ListPage(no_list=True, removed=not _on_exposition(url, edition.id))
        rows: list[tuple[Row, Sequence[str | None]]] = []
        unreadable = 0
        for anchor in scroll.find_all("a"):
            href = anchor.attrs.get("href", "")
            company = _EXPODAT_COMPANY_RE.search(href)
            if company is None:
                continue
            title = anchor.find(cls="exh_name")
            name = clean(anchor.attrs.get("title")) or (title.text() if title else None)
            if not name:
                unreadable += 1
                continue
            stand_el = anchor.find(cls="numb_stand")
            stand_b = stand_el.find("b") if stand_el is not None else None
            stand = stand_b.text() if stand_b is not None else None
            flag = anchor.find(cls="img_country")
            country = clean(flag.attrs.get("title")) if flag is not None else None
            about = anchor.find(cls="short_opis")
            description = about.text() if about is not None else None
            row = Row(
                name=name,
                country=country,
                stand=stand or None,
                company_id=company.group(1),
                card_url=urljoin(url, href),
                description=description or None,
            )
            rows.append((row, (name, stand, country, description)))
        kept, seen, foreign, skipped, refs = _sort_rows(
            rows,
            show=show,
            edition=edition,
            platform="expodat",
            url=url,
            fetched_at=fetched_at,
            sha=hashlib.sha256(body).hexdigest(),
            exclude=exclude,
            held=held,
        )
        base: str | None = None
        end_start: int | None = None
        pager = tree.find(cls="pagination")
        for link in pager.find_all(cls="hasTooltip") if pager is not None else ():
            href = link.attrs.get("data-href", "")
            if not href:
                continue
            base = base or urlsplit(href).path
            if link.attrs.get("title") == "End":
                end_start = _start_of(href)
        lim = _LIM_RE.search(body.decode("utf-8", errors="replace"))
        return ListPage(
            kept=tuple(kept),
            seen=seen + unreadable,
            foreign=foreign,
            skipped=skipped + unreadable,
            refs=tuple(refs),
            page_size=int(lim.group(1)) if lim else None,
            base=base,
            end_start=end_start,
        )

    # --- the run -----------------------------------------------------------------

    async def collect(self, session: AsyncSession, budget: Budget) -> list[Observation]:
        now = self.started_at or datetime.now(settings.tz)
        today = now.astimezone(settings.tz).date()
        config = await session.scalar(
            sa.select(Source.config).where(Source.key == self.key)
        )
        calendar, problems = calendar_from(config)
        if problems:
            self.run_meta["calendar"] = problems
        # Written as the run goes: a run the budget or the breaker stops keeps
        # what it read (the runner writes run_meta and Collector.found).
        archived: list[str] = []
        self.run_meta["lists"] = []
        self.run_meta["archived"] = archived
        swept = await archived_shows(session, now=now)
        # Every index first, then this week's lists of every show, then the
        # archives: a budget that runs out stops an archive, never a show in
        # its window — whichever show the archive belongs to.
        plans: list[tuple[Visit, list[Target]]] = []
        for visit in plan_visits(calendar, today, swept):
            targets = await self._index(visit, budget, today)
            if targets is not None:
                plans.append((visit, targets))
        for visit, targets in plans:
            for target in targets:
                if target.why == "window":
                    self._record(await self._read(visit, target, budget))
        for visit, targets in plans:
            done = True
            for target in targets:
                if target.why != "archive":
                    continue
                result = await self._read(visit, target, budget)
                self._record(result)
                # A removed archive is as read as it will ever be; a failed
                # or cut one is asked again next week.
                if result.status not in ("complete", "removed"):
                    done = False
            if visit.archive and done:
                archived.append(visit.show)
        return self.found

    async def _index(
        self, visit: Visit, budget: Budget, today: date
    ) -> list[Target] | None:
        """Read one show's index: the lists to read of it, or None when the
        index failed (said in ``meta.lists``). An in-window entry the index
        lists no edition for is recorded ``missing`` here."""
        try:
            index = await self.fetcher.get(
                visit.index_url, source=self.key, budget=budget
            )
        except RobotsDisallowed:
            self._index_failed(visit, "robots")
            return None
        if not served(index):
            self._index_failed(visit, "failed", error=unserved(index))
            return None
        if visit.platform == "era":
            editions = self.parse_era_editions(
                index.body, url=index.final_url, fetched_at=index.fetched_at
            )
            if not editions:
                # Every recorded ERA index lists its past editions in the
                # ChangeYear select (README «Files»): none is a page this
                # parser no longer reads, never a show with no archive.
                self._index_failed(visit, "failed", error="index_empty")
                return None
        else:
            # A Tilda page links the current catalogue only, or none (the
            # 2024 list is a JS feed, README item 5): no link is a state the
            # site is recorded in; in a window it is the entry «missing».
            editions = self.parse_expodat_links(
                index.body, url=index.final_url, fetched_at=index.fetched_at
            )
            host = urlsplit(visit.rows_pattern).hostname
            editions = [e for e in editions if urlsplit(e.url or "").hostname == host]
        targets, missing = pick_targets(visit, editions, today)
        for entry in missing:
            self._record(
                ListResult(
                    visit.show, entry.end_date.year, None, "window", "missing", entry
                )
            )
        return targets

    def _index_failed(
        self, visit: Visit, status: ListStatus, *, error: str | None = None
    ) -> None:
        for entry in visit.due:
            self._record(
                ListResult(
                    visit.show,
                    entry.end_date.year,
                    None,
                    "window",
                    status,
                    entry,
                    error=error,
                )
            )
        if not visit.due:
            why: Why = "archive" if visit.archive else "index"
            self._record(
                ListResult(visit.show, None, None, why, status, None, error=error)
            )

    def _record(self, result: ListResult) -> None:
        self.lists.append(result)
        self.run_meta["lists"].append(result.as_meta())
        reason = result.incomplete()
        if reason is not None:
            self.incomplete.append(reason)

    async def _read(self, visit: Visit, target: Target, budget: Budget) -> ListResult:
        entry = target.entry
        result = ListResult(
            target.show,
            target.edition.year,
            target.edition.id,
            target.why,
            "complete",
            entry,
        )
        exclude = entry.exclude if entry is not None else _exclude_of(visit)
        held = entry.end_date if entry is not None else None
        try:
            if visit.platform == "era":
                await self._read_era(visit, target, budget, result, exclude, held)
            else:
                await self._read_expodat(visit, target, budget, result, exclude, held)
        except RobotsDisallowed:
            result.status = "robots"
        except ValueError as exc:
            # An answer that is no list (an ERA 500 page, a JSON of another
            # shape): this list is unread, the run goes on.
            result.status, result.error = "failed", type(exc).__name__
            log.warning(
                "exhibitor list unreadable",
                extra={
                    "source": self.key,
                    "show": target.show,
                    "edition": target.edition.id,
                },
            )
        return result

    def _take(self, page: ListPage, result: ListResult) -> None:
        self.found.extend(page.kept)
        result.rows += page.seen
        result.kept += len(page.kept)
        result.foreign += page.foreign
        result.skipped += page.skipped
        result.pages += 1

    async def _read_era(
        self,
        visit: Visit,
        target: Target,
        budget: Budget,
        result: ListResult,
        exclude: Sequence[str],
        held: date | None,
    ) -> None:
        url = rows_url(visit.rows_pattern, target.edition.id)
        start = 0
        seen: set[str] = set()
        for draw in range(1, MAX_PAGES_PER_LIST + 1):
            answer = await self.fetcher.get(
                url,
                source=self.key,
                budget=budget,
                params={"draw": draw, "start": start, "length": ERA_PAGE},
            )
            if not served(answer):
                result.status, result.error = "failed", unserved(answer)
                return
            page = self.parse_era_rows(
                answer.body,
                url=answer.url,
                fetched_at=answer.fetched_at,
                show=target.show,
                edition=target.edition,
                exclude=exclude,
                held=held,
            )
            result.total = page.total
            if page.seen == 0 or (page.refs and set(page.refs) <= seen):
                break
            seen.update(page.refs)
            self._take(page, result)
            start += page.seen
            if page.total is not None and start >= page.total:
                break
        else:
            result.status = "truncated"
            return
        if result.total is not None and result.rows < result.total:
            result.status = "truncated"

    async def _read_expodat(
        self,
        visit: Visit,
        target: Target,
        budget: Budget,
        result: ListResult,
        exclude: Sequence[str],
        held: date | None,
    ) -> None:
        url = target.edition.url or rows_url(visit.rows_pattern, target.edition.id)
        params: dict[str, int] | None = None
        start = 0
        end_start: int | None = None
        seen: set[str] = set()
        for number in range(MAX_PAGES_PER_LIST):
            answer = await self.fetcher.get(
                url, source=self.key, budget=budget, params=params
            )
            if not served(answer):
                result.status, result.error = "failed", unserved(answer)
                return
            page = self.parse_expodat_page(
                answer.body,
                url=answer.final_url,
                fetched_at=answer.fetched_at,
                show=target.show,
                edition=target.edition,
                exclude=exclude,
                held=held,
            )
            if page.no_list:
                # Only the catalogue's own first page can say it is gone; a
                # page with no list that is still the exposition is a page
                # this parser cannot read, and a later page with none cuts
                # the walk short — neither is judged against the organiser.
                if number > 0:
                    result.status, result.error = "truncated", "no_list"
                elif page.removed:
                    result.status = "removed"
                else:
                    result.status, result.error = "failed", "no_list"
                return
            end_start = page.end_start if page.end_start is not None else end_start
            if page.seen == 0 or (page.refs and set(page.refs) <= seen):
                break
            seen.update(page.refs)
            self._take(page, result)
            size = page.page_size or page.seen
            if page.seen < size:
                break
            start += size
            if end_start is not None and start > end_start:
                break
            url = urljoin(answer.final_url, page.base or urlsplit(answer.final_url).path)
            params = {"start": start}
        else:
            result.status = "truncated"
            return
        if end_start is not None:
            result.total = end_start + 1
            if result.rows < result.total:
                result.status = "truncated"

    async def after_write(self, session: AsyncSession, placed: list[Placed]) -> None:
        """06 §4.2 ``exhibitor_list_short``: this run's in-window lists judged
        against the organiser's count, merged with the lines still open from
        earlier runs (in the run's transaction)."""
        now = self.started_at or datetime.now(settings.tz)
        await note_short_lists(session, self.lists, now=now)


def rows_url(pattern: str, edition: str) -> str:
    """The rows address of one edition (``{edition}`` replaced; any other
    brace in an owner's pattern is left as written)."""
    return pattern.replace(EDITION_SLOT, edition)


def served(result: FetchResult) -> bool:
    """A page to read: a 200, or a 304 whose body is the stored copy (the
    Fetcher's conditional GET, 03 §1.1) — never a captcha or challenge page
    (``FetchResult.captcha``), which holds no list and says nothing of
    one."""
    return (result.status == 200 or result.not_modified) and not result.captcha


def unserved(result: FetchResult) -> str:
    """Why a page was no page to read (``ListResult.error``)."""
    return "captcha" if result.captcha else f"http_{result.status}"


def _exclude_of(visit: Visit) -> tuple[str, ...]:
    names: list[str] = []
    for entry in visit.entries:
        names.extend(n for n in entry.exclude if n not in names)
    return tuple(names)


@dataclass(slots=True)
class ListResult:
    """What one list gave: its rows (every country), the Uzbek rows kept, the
    foreign ones, the site's own count, and how the walk ended."""

    show: str
    year: int | None
    edition: str | None
    why: Why
    status: ListStatus
    entry: ShowEntry | None
    rows: int = 0
    kept: int = 0
    foreign: int = 0
    skipped: int = 0
    pages: int = 0
    total: int | None = None
    error: str | None = None

    @property
    def expected(self) -> int | None:
        return self.entry.expected if self.entry is not None else None

    def short(self) -> bool:
        """03 §2.8: a list read whole (or gone) with fewer rows than half the
        organiser's figure. A walk cut short is not judged."""
        expected = self.expected
        if expected is None or self.status not in ("complete", "removed", "missing"):
            return False
        return self.rows < expected * SHORT_RATIO

    def judged(self) -> bool:
        """An in-window list this run read to its end (or found gone)."""
        return self.why == "window" and self.status in ("complete", "removed", "missing")

    def incomplete(self) -> str | None:
        """The run's ``meta.incomplete`` line for this list, if any: the show,
        the year and the site's edition id where they are known
        («textileexpo:2024:442:failed» — TextileExpo had two editions in
        2024)."""
        where = ":".join(
            str(part) for part in (self.show, self.year, self.edition) if part is not None
        )
        if self.status != "complete":
            return f"{where}:{self.status}"
        if self.short():
            return f"{where}:short:{self.rows}/{self.expected}"
        return None

    def as_meta(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "show": self.show,
            "year": self.year,
            "edition": self.edition,
            "why": self.why,
            "status": self.status,
            "rows": self.rows,
            "kept": self.kept,
            "foreign": self.foreign,
            "skipped": self.skipped,
            "pages": self.pages,
        }
        if self.total is not None:
            found["total"] = self.total
        if self.expected is not None:
            found["expected"] = self.expected
        if self.error:
            found["error"] = self.error
        return found


# --- state from earlier runs ---------------------------------------------------------


async def archived_shows(session: AsyncSession, *, now: datetime) -> frozenset[str]:
    """The shows whose archive a run swept in the last ``ARCHIVE_EVERY`` (an
    ``ok`` or ``partial`` run). A failed run is not counted: one that raised
    or whose write failed kept nothing it read, and the runner's status
    cannot tell those apart from a breaker stop, which keeps what it read
    (``runner``: ``HostPaused`` writes ``Collector.found``) — so a sweep in a
    run the breaker failed is read again the next Monday, at the cost of one
    sweep's pages."""
    rows = await session.scalars(
        sa.select(CollectorRun.meta["archived"]).where(
            CollectorRun.source_key == SOURCE,
            CollectorRun.status.in_(["ok", "partial"]),
            CollectorRun.started_at > now - ARCHIVE_EVERY,
        )
    )
    found: set[str] = set()
    for value in rows:
        if isinstance(value, list):
            found.update(v for v in value if isinstance(v, str))
    return frozenset(found)


def _until(entry: ShowEntry) -> str:
    """When an entry's window ends: a line's end, and with its show the
    line's key (a show may hold two entries in one year)."""
    return (entry.end_date + WINDOW_CLOSES).isoformat()


def _line(result: ListResult) -> dict[str, Any] | None:
    entry = result.entry
    if entry is None or not result.short():
        return None
    return {
        "show": result.show,
        "title": entry.title,
        "year": result.year,
        "rows": result.rows,
        "expected": entry.expected,
        "status": result.status,
        "until": _until(entry),
    }


async def note_short_lists(
    session: AsyncSession, results: Sequence[ListResult], *, now: datetime
) -> None:
    """Raise, narrow or clear ``exhibitor_list_short``: the lines open from
    earlier runs, less the shows this run read again and those whose window
    is over, plus this run's short in-window lists. A line ends with its
    window (``until``): after it nothing is left to crawl. A line is one
    calendar entry's (its show and its ``until``): a list read of another
    edition of the same show and year — one the calendar does not name —
    leaves it standing."""
    today = now.astimezone(settings.tz).date()
    judged = {
        (r.show, _until(r.entry)) for r in results if r.judged() and r.entry is not None
    }
    open_ = (await health.reported_problems(session, now)).get(SHORT_PROBLEM)
    held: list[dict[str, Any]] = []
    for item in open_.detail.get("lists", []) if open_ is not None else []:
        if not isinstance(item, Mapping):
            continue
        if (item.get("show"), item.get("until")) in judged:
            continue
        try:
            if date.fromisoformat(str(item.get("until"))) <= today:
                continue
        except ValueError:
            continue
        held.append(dict(item))
    lines = held + [
        line for r in results if r.judged() and (line := _line(r)) is not None
    ]
    if not lines:
        if open_ is not None:
            await health.clear_problem(session, SHORT_PROBLEM)
        return
    until = max(date.fromisoformat(str(line["until"])) for line in lines)
    await health.raise_problem(
        session,
        SHORT_PROBLEM,
        now=now,
        by=f"collector:{SOURCE}",
        detail={"lists": lines},
        until=datetime.combine(until, time(), tzinfo=settings.tz),
    )
