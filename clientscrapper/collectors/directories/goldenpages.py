"""goldenpages.uz (03 §2.6, T-22): rubric pages → company cards.

Every selector below is VERIFIED on the recorded pages
(tests/fixtures/directories/README.md «goldenpages.uz»):

* **list** — one company = ``section.gp_company``; its card link
  ``h3 a[href^="/company/?Id="]``; activities ``div.gp_td_uL9y ul li``;
  badges ``img.sub__icons[alt]`` («Импортер», «Экспортёр», «Производитель»,
  «Дистрибьютор»); address ``div.gp_wrap_address p``; the slogan
  ``blockquote.GP_description``; the organiser's count
  ``span.gp_numberTitle``; the next page ``li.gp_next a`` =
  ``/rubrics/?Id=N&Page=M`` (capital P, VERIFIED on page 2). The list's phones
  are masks (``+998 (NN) NNN-XX-XX`` and a «Показать телефон» XHR button that
  is never called): **a mask is not a phone**, so a list row carries none.
* **paginator stop** — a page past the last redirects to page 1
  (``…&Page=99`` → ``/rubrics/?Id=1424``) and repeats its first card id: the
  plan stops on a final URL without ``Page`` (``redirect_stop``) or on a
  repeated first card id (the shared rule).
* **card** — the phones are in the card's JSON-LD ``FAQPage``: the question
  «Номер телефона …?» answers with every number, ``;``-separated, as
  ``(+998NN) NNNNNNN``; the name and the card's own URL in the
  ``LocalBusiness``; the rubrics in ``#tab-3``'s ``a[href^="/rubrics/?Id="]``;
  the address ``div.gp_card__adresses p`` (city ``a[href^="/city/?Id="]``); the
  description ``div.short_info``; «Обновлено: dd.mm.yyyy» in
  ``span.gp_com_date`` — a card updated before 2024 is dropped (03 §2.6). The
  INN is shown masked («30452****») and is not a STIR. The page's own
  help-desk numbers (``tel:`` links in the header, footer and mobile menu)
  are never read: phones come from the JSON-LD alone.
* **bot trap** — every page carries hidden ``/gp/…`` links under wrappers whose
  tag and inline style change on every response; robots.txt allows them. Only
  the two hrefs the selectors above pick are ever followed (``CARD_HREF``,
  ``PAGE_HREF``), and a ``/gp/`` path is refused outright.

Signals: an «Импортер» badge → ``brand_distributor(official=False)``; a
rubric «… из Китая» (``CHINA_RE`` on the rubric names, e.g. 106080
«Оборудование из Китая») → ``china_origin``; a wholesale rubric (1424
«Оптово-розничная торговля») → ``directory_wholesale`` under the shared
category filter (``common.judge``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.directories import common
from clientscrapper.collectors.directories.markup import Node, parse, scripts
from clientscrapper.collectors.keywords import CHINA_RE

SITE: Final = "goldenpages"
HOST: Final = "www.goldenpages.uz"
BASE: Final = f"https://{HOST}"
SOURCE_KEY: Final = "directories_goldenpages"
# The only hrefs the plan follows (as the page prints them, entities decoded).
CARD_HREF: Final = re.compile(r"\A/company/\?Id=(\d{1,9})\Z")
PAGE_HREF: Final = re.compile(r"\A/rubrics/\?Id=(\d{1,9})&Page=(\d{1,4})\Z")
BOT_TRAP: Final = "/gp/"
IMPORTER: Final = "Импортер"
PHONE_QUESTION: Final = "Номер телефона"
UPDATED: Final = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
# 03 §2.6: «updated date (drop < 2024)».
OLDEST_YEAR: Final = 2024
TASHKENT: Final = common.TASHKENT
# 03 §2.6's seeds (manbalar.json [v 34]). 1424 and 106080 are recorded; the
# others are the same page kind, not fetched (README «STILL UNVERIFIED»).
# /street/?Id=41003 (Abu-Sahiy) is another page kind with no recording: owed.
RUBRICS: Final = (1424, 1277, 1558, 106080, 1029, 1686, 1800)
SEEDS: Final = tuple(f"{BASE}/rubrics/?Id={rubric}" for rubric in RUBRICS)


def card_url(href: str | None) -> str | None:
    """The card a list row links to, when the href is the selector's shape."""
    if not href or href.startswith(BOT_TRAP) or CARD_HREF.match(href) is None:
        return None
    return BASE + href


def page_url(href: str | None) -> str | None:
    if not href or href.startswith(BOT_TRAP) or PAGE_HREF.match(href) is None:
        return None
    return BASE + href


def company_id(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.path != "/company/":
        return None
    values = parse_qs(parts.query).get("Id")
    return values[0] if values and values[0].isdigit() else None


def page_number(url: str) -> int | None:
    values = parse_qs(urlsplit(url).query).get("Page")
    return int(values[0]) if values and values[0].isdigit() else None


def redirect_stop(asked: str, final: str) -> str | None:
    """A page past the last answers page 1 at a URL without ``Page``."""
    asked_page = page_number(asked)
    if asked_page is not None and asked_page > 1 and page_number(final) is None:
        return "redirected"
    return None


def _badges(scope: Node) -> list[str]:
    found: list[str] = []
    for img in scope.find_all("img", cls="sub__icons"):
        alt = common.squash(img.get("alt"))
        if alt and alt not in found:
            found.append(alt)
    return found


def _activities(section: Node) -> list[str]:
    found: list[str] = []
    for cell in section.find_all("div", cls="gp_td_uL9y"):
        listing = cell.find("ul")
        if listing is None:
            continue
        for item in listing.find_all("li"):
            text = common.squash(item.text())
            if text and text not in found:
                found.append(text)
    return found


def _verdict(
    *, name: str | None, rubrics: list[str], badges: list[str], own: list[str | None]
) -> common.Verdict:
    words = common.joined([*rubrics, *own]) or ""
    return common.judge(
        name=name,
        own=words,
        goods=common.joined([*rubrics, *own]) or "",
        wholesale=common.wholesale(*rubrics),
        china=common.first_match(CHINA_RE, rubrics),
        importer=IMPORTER in badges,
    )


def _row(
    section: Node, *, rubric: str | None, url: str, fetched_at: datetime, sha: str
) -> common.Entry | None:
    title = section.find("h3")
    link = title.find("a", prefix=("href", "/company/?Id=")) if title else None
    target = card_url(link.get("href")) if link else None
    if link is None or target is None:
        return None
    ident = company_id(target)
    assert ident is not None
    raw = common.squash(link.text())
    # An individual entrepreneur's name is a person's: kept in no field.
    name = common.company_name(raw)
    rubrics = _activities(section)
    if rubric and rubric not in rubrics:
        rubrics.append(rubric)
    badges = _badges(section)
    second = [
        common.squash(n.text()) for n in section.find_all("div", cls="gp_name_legal")
    ]
    address = section.find("div", cls="gp_wrap_address")
    line = address.find("p") if address else None
    slogan = section.find("blockquote", cls="GP_description")
    description = common.squash(slogan.text()) if slogan else None
    obs = Observation(
        source=SOURCE_KEY,
        source_ref=f"{SITE}:{ident}",
        url=url,
        fetched_at=fetched_at,
        raw_sha256=sha,
        kind="company",
        company_name_raw=name,
        legal_form=common.legal_form_in(raw),
        address=common.squash(line.text()) if line else None,
        goods_text=common.joined([*rubrics, description]),
        excerpt=common.excerpt(name, *badges, *rubrics, description),
        external_ids={SITE: ident},
        attrs={
            "site": SITE,
            "card_url": target,
            "rubrics": rubrics,
            "badges": badges,
            "list_row": True,
        },
        lang="ru",
    )
    own = [*second, description]
    verdict = _verdict(name=name, rubrics=rubrics, badges=badges, own=own)
    return common.Entry(obs.source_ref, target, common.apply(obs, verdict))


def parse_list(body: bytes, *, url: str, fetched_at: datetime) -> common.ListPage:
    """A rubric page: its companies (no phones: the list shows masks), the
    next page as printed, the organiser's count."""
    root = parse(body)
    heading = root.find("h1")
    rubric = common.squash(heading.text()) if heading else None
    sha = common.sha256(body)
    entries: list[common.Entry] = []
    for section in root.find_all("section", cls="gp_company"):
        entry = _row(section, rubric=rubric, url=url, fetched_at=fetched_at, sha=sha)
        if entry is not None and all(e.ref != entry.ref for e in entries):
            entries.append(entry)
    following = root.find("li", cls="gp_next")
    link = following.find("a") if following else None
    count = root.find("span", cls="gp_numberTitle")
    total_text = common.squash(count.text()) if count else None
    return common.ListPage(
        entries=tuple(entries),
        next_url=page_url(link.get("href")) if link else None,
        total=int(total_text) if total_text and total_text.isdigit() else None,
        page=page_number(url) or 1,
    )


def parse_rubric(body: bytes, *, url: str, fetched_at: datetime) -> list[Observation]:
    """The list rows of a rubric page as observations (03 §1.2's ``parse_*``)."""
    page = parse_list(body, url=url, fetched_at=fetched_at)
    return [e.observation for e in page.entries if e.observation is not None]


def _json_ld(root: Node) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    for text in scripts(root, type_="application/ld+json"):
        try:
            data = json.loads(text)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, Mapping):
                found.append(item)
    return found


def _phones(faq: Mapping[str, Any] | None) -> list[str]:
    """The «Номер телефона …?» answer's numbers, as written."""
    entities = faq.get("mainEntity") if faq else None
    for question in entities if isinstance(entities, list) else []:
        if not isinstance(question, Mapping):
            continue
        name = question.get("name")
        if not (isinstance(name, str) and name.strip().startswith(PHONE_QUESTION)):
            continue
        answer = question.get("acceptedAnswer")
        text = answer.get("text") if isinstance(answer, Mapping) else None
        if not isinstance(text, str):
            return []
        numbers = text.rsplit(" - ", 1)[-1]
        return common.phones_as_written(numbers.split(";"))
    return []


def _updated(root: Node) -> datetime | None:
    stamp = root.find("span", cls="gp_com_date")
    match = UPDATED.search(stamp.text()) if stamp else None
    if match is None:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, tzinfo=TASHKENT)
    except ValueError:
        return None


def parse_company(body: bytes, *, url: str, fetched_at: datetime) -> list[Observation]:
    """A company card → one observation; [] for a page that is no card.
    ``common.CardDropped`` for a card updated before 2024 (03 §2.6 «drop
    < 2024»): read, and not kept — nor its list row in its place."""
    ident = company_id(url)
    if ident is None:
        return []
    root = parse(body)
    blocks = _json_ld(root)
    business = next((b for b in blocks if b.get("@type") == "LocalBusiness"), None)
    faq = next((b for b in blocks if b.get("@type") == "FAQPage"), None)
    heading = root.find("h1")
    if business is None or heading is None:
        return []
    updated = _updated(root)
    if updated is not None and updated.year < OLDEST_YEAR:
        raise common.CardDropped(f"updated {updated.date().isoformat()}")
    named = business.get("name")
    raw = common.squash(named) if isinstance(named, str) else None
    if raw is None:
        raw = common.squash(re.sub(r"\s*\([^()]*\)\s*\Z", "", heading.text()))
    # An individual entrepreneur's name is a person's: kept in no field.
    name = common.company_name(raw)
    holder = heading.parent or heading
    badges = _badges(holder)
    tab = root.find("div", attr=("id", "tab-3"))
    rubrics: list[str] = []
    for link in tab.find_all("a", prefix=("href", "/rubrics/?Id=")) if tab else []:
        text = common.squash(link.text())
        if text and text not in rubrics:
            rubrics.append(text)
    info = root.find("div", cls="short_info")
    description = common.squash(info.text()) if info else None
    where = root.find("div", cls="gp_card__adresses")
    line = where.find("p") if where else None
    city = where.find("a", prefix=("href", "/city/?Id=")) if where else None
    attrs: dict[str, Any] = {"site": SITE, "rubrics": rubrics, "badges": badges}
    if updated is not None:
        attrs["updated_on"] = updated.date().isoformat()
    obs = Observation(
        source=SOURCE_KEY,
        source_ref=f"{SITE}:{ident}",
        url=url,
        fetched_at=fetched_at,
        raw_sha256=common.sha256(body),
        kind="company",
        company_name_raw=name,
        legal_form=common.legal_form_in(raw),
        phones=_phones(faq),
        address=common.squash(line.text()) if line else None,
        city=common.squash(city.text()) if city else None,
        goods_text=common.joined([*rubrics, description]),
        excerpt=common.excerpt(name, *badges, *rubrics, description),
        external_ids={SITE: ident},
        attrs=attrs,
        published_at=updated,
        lang="ru",
    )
    verdict = _verdict(name=name, rubrics=rubrics, badges=badges, own=[description])
    return [common.apply(obs, verdict)]


SPEC: Final = common.SiteSpec(
    site=SITE,
    host=HOST,
    source_key=SOURCE_KEY,
    parse_list=parse_list,
    parse_card=parse_company,
    seeds=SEEDS,
    paginated=True,
    redirect_stop=redirect_stop,
)
