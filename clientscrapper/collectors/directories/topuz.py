"""top.uz (03 §2.6, T-22): section pages → company cards.

The same Nuxt platform as yellowpages (``nuxt.py``): a section page's
organisations are the payload's ``data``/``links``/``meta`` block (20 per
page), each with ``id, name_ru, slug, description_ru, address_ru`` (HTML),
``email, instagram, tg_channel, tg_contact, fax, site, phones[{phone}],
sections[{name_ru, slug}]``. VERIFIED (tests/fixtures/directories/README.md
«top.uz»):

* **pagination** — ``?PAGEN_1=N``: the plan takes the page link the pager
  prints for the next page (``ul.articles-pagination a[href]``), as printed —
  robots.txt's ``*`` group refuses ``*?p=``, ``*SORT=``, ``*utm*=`` … and
  nothing is ever appended. **Stop** — a page past the last answers 200 with
  ``current_page`` 99 and ``data: []``: an empty page, or ``current_page >
  last_page``, ends the seed.
* **phones** — the list HTML masks the number («+998...» and a «Показать»
  button, never pressed), the payload carries ``phones[].phone`` in full as
  served; the card shows it in its ``tel:`` link and its payload. The phone
  is read from the payload, list or card (no reveal request exists). The
  card is still fetched for every company the plan keeps: it is that
  company's own evidence (its excerpt is the quote a lead card verifies,
  02 §2.6) — a list page is one evidence row for twenty companies.
* **wholesale** — 03 §2.6: «``directory_wholesale`` only for ``*-optom``
  sections»; read as a wholesale section (``WHOLESALE_RUBRIC_RE`` on the
  section slugs and names), which keeps ``stroymaterialy-optom`` and the
  ``optovaya-torgovlya`` seed itself (815 organisations, the section the
  spec names first) and leaves out the general directory's other sections.
* top.uz has no ``sources`` row of its own: observations attribute to the
  parent ``directories`` (03 §2.6), the site named in ``source_ref``
  (``topuz:<id>``). No id type exists for it in ``company_ids`` (02 §2.4).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.directories import common, nuxt
from clientscrapper.collectors.directories.markup import Node, parse
from clientscrapper.services.keys import instagram_key
from clientscrapper.services.normalise import telegram_key

SITE: Final = "topuz"
HOST: Final = "top.uz"
BASE: Final = f"https://{HOST}"
SOURCE_KEY: Final = "directories"
_SLUG = r"([a-z0-9][a-z0-9-]{0,199})"
SLUG: Final = re.compile(rf"\A{_SLUG}\Z")
CARD_PATH: Final = re.compile(rf"\A/company/{_SLUG}/?\Z")
PAGE_HREF: Final = re.compile(rf"\A/section/{_SLUG}\?PAGEN_1=(\d{{1,4}})\Z")
# 03 §2.6's seeds; the two recorded sections. ``/section/prodazha-stroitelnykh-
# materialov`` and the regional ``/tashkent-obl`` pages were not recorded
# (README «STILL UNVERIFIED»): owed.
SEEDS: Final = (
    f"{BASE}/section/optovaya-torgovlya",
    f"{BASE}/section/stroymaterialy-optom",
)


def card_url(slug: Any) -> str | None:
    if not isinstance(slug, str) or SLUG.match(slug) is None:
        return None
    return f"{BASE}/company/{slug}"


def _sections(org: dict[str, Any], key: str) -> list[str]:
    found: list[str] = []
    for section in org.get("sections") or []:
        value = common.squash(section.get(key)) if isinstance(section, dict) else None
        if value and value not in found:
            found.append(value)
    return found


def _city(address: Any) -> str | None:
    if not isinstance(address, str):
        return None
    link = parse(address.encode("utf-8")).find("a", prefix=("href", "/gorod/"))
    return common.squash(link.text()) if link else None


def _text(org: dict[str, Any], key: str) -> str | None:
    value = org.get(key)
    return common.squash(value) if isinstance(value, str) else None


def organisation(
    org: dict[str, Any], *, url: str, fetched_at: datetime, sha: str
) -> Observation | None:
    ident = org.get("id")
    if isinstance(ident, bool) or not isinstance(ident, int):
        return None
    raw = _text(org, "name_ru") or _text(org, "name_uz")
    given_legal = _text(org, "legal_name")
    # An individual entrepreneur's name («… ИндП») is a person's: kept in no
    # field (common.company_name); the firm's own name where one is printed.
    legal_name = common.company_name(given_legal)
    name = common.company_name(raw, _text(org, "name_uz"), legal_name)
    numbers = [p.get("phone") for p in org.get("phones") or [] if isinstance(p, dict)]
    fax = _text(org, "fax")
    phones = common.phones_as_written(
        [*(n for n in numbers if isinstance(n, str)), *([fax] if fax else [])]
    )
    sections = _sections(org, "name_ru")
    slugs = _sections(org, "slug")
    description = common.plain(org.get("description_ru"))
    handle = _text(org, "tg_channel") or _text(org, "tg_contact")
    insta = _text(org, "instagram")
    attrs: dict[str, Any] = {"site": SITE, "sections": slugs}
    if legal_name and legal_name != name:
        attrs["legal_name"] = legal_name
    source = _text(org, "inside_source")
    if source:
        attrs["inside_source"] = source
    target = card_url(org.get("slug"))
    if target is not None:
        attrs["card_url"] = target
    obs = Observation(
        source=SOURCE_KEY,
        source_ref=f"{SITE}:{ident}",
        url=url,
        fetched_at=fetched_at,
        raw_sha256=sha,
        kind="company",
        company_name_raw=name,
        legal_form=common.legal_form_in(raw) or common.legal_form_in(given_legal),
        phones=phones,
        telegram=telegram_key(handle) if handle else None,
        instagram=instagram_key(insta) if insta else None,
        website=_text(org, "site"),
        address=common.plain(org.get("address_ru")),
        city=_city(org.get("address_ru")),
        goods_text=common.joined([*sections, description]),
        excerpt=common.excerpt(
            name, legal_name if legal_name != name else None, description, *sections
        ),
        attrs=attrs,
        lang="ru",
    )
    own = common.joined([description, *sections]) or ""
    verdict = common.judge(
        name=name,
        own=own,
        goods=own,
        wholesale=common.wholesale(*slugs, *sections),
        china=None,
    )
    return common.apply(obs, verdict)


def _next_link(root: Node, *, section: str | None, wanted: int | None) -> str | None:
    if section is None or wanted is None:
        return None
    pager = root.find("ul", cls="articles-pagination")
    for link in pager.find_all("a") if pager else []:
        href = link.get("href") or ""
        match = PAGE_HREF.match(href)
        if match and match.group(1) == section and int(match.group(2)) == wanted:
            return BASE + href
    return None


def parse_list(body: bytes, *, url: str, fetched_at: datetime) -> common.ListPage:
    """A section page: its organisations (complete rows) and the pager's link
    to the next page, as printed."""
    root = parse(body)
    block = nuxt.list_block(nuxt.payload(root))
    if block is None:
        return common.ListPage(entries=())
    sha = common.sha256(body)
    entries: list[common.Entry] = []
    for org in block["data"]:
        if not isinstance(org, dict):
            continue
        obs = organisation(org, url=url, fetched_at=fetched_at, sha=sha)
        if obs is None or any(e.ref == obs.source_ref for e in entries):
            continue
        entries.append(
            common.Entry(obs.source_ref, card_url(org.get("slug")), obs, complete=True)
        )
    meta = block["meta"]
    page, last = _int(meta.get("current_page")), _int(meta.get("last_page"))
    path = urlsplit(url).path
    section = path.removeprefix("/section/") if path.startswith("/section/") else None
    wanted = page + 1 if page is not None and last is not None and page < last else None
    return common.ListPage(
        entries=tuple(entries),
        next_url=_next_link(root, section=section, wanted=wanted) if entries else None,
        total=_int(meta.get("total")),
        page=page,
        last_page=last,
    )


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_section(body: bytes, *, url: str, fetched_at: datetime) -> list[Observation]:
    """A section page's organisations as observations."""
    page = parse_list(body, url=url, fetched_at=fetched_at)
    return [e.observation for e in page.entries if e.observation is not None]


def parse_company(body: bytes, *, url: str, fetched_at: datetime) -> list[Observation]:
    """A company card → one observation; [] when the payload does not hold
    the organisation the URL names."""
    match = CARD_PATH.match(urlsplit(url).path)
    if match is None:
        return []
    org = nuxt.organisation(nuxt.payload(parse(body)), match.group(1), needs="phones")
    if org is None:
        return []
    obs = organisation(org, url=url, fetched_at=fetched_at, sha=common.sha256(body))
    return [obs] if obs is not None else []


def page_number(url: str) -> int | None:
    values = parse_qs(urlsplit(url).query).get("PAGEN_1")
    return int(values[0]) if values and values[0].isdigit() else None


SPEC: Final = common.SiteSpec(
    site=SITE,
    host=HOST,
    source_key=SOURCE_KEY,
    parse_list=parse_list,
    parse_card=parse_company,
    seeds=SEEDS,
    paginated=True,
)
