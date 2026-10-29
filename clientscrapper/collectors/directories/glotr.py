"""glotr.uz (03 §2.6, T-22): category page 1 → sellers → showcase pages.

VERIFIED (tests/fixtures/directories/README.md «glotr.uz»):

* **robots.txt** — ``User-agent: *`` disallows ``/*?page=`` and ``/*&page=``
  (and ``?sort=``, ``?region=``, ``?catid=``…) with ``Crawl-delay: 10``. The
  category pagination is exactly ``?page=N``, so **page 2+ is never asked**:
  ``parse_list`` returns no next link whatever the pager prints, and the
  spec's «category → products grouped by seller» crawl reads page 1 of each
  entry point. ``/ru/<cat>/`` redirects to ``/<cat>/``; the seeds are written
  unprefixed.
* **category page** — 56 ``div.product-card__parent`` per page; the seller of
  each is ``div.product_company a.product_company_link[href^="/showcase/"]``
  — most hrefs end in ``?s=1``, some carry no query: a seller is keyed on the
  path with the query dropped (``glotr:<slug>``), and asked for at that path.
  The badge icon beside a seller's name is not read (what it certifies is
  STILL UNVERIFIED).
* **showcase** — JSON-LD ``LocalBusiness``: ``telephone``, ``address``,
  ``description``, ``sameAs`` (Instagram, Facebook, t.me); the visible phone
  is masked («+998 (показать номер)»), so the JSON-LD is the source. The
  site's own ``Organization`` JSON-LD (glotr's help desk) is never read. The
  name is ``h1``; the website the seller-info line with ``i.fa-globe``.
  «Проверено — … через систему Soliq.uz» in ``div.seller-info__checked`` →
  ``entity_solid``; ``SKU_VELOCITY`` or more products on the showcase →
  ``marketplace_velocity`` (page 1 alone — a total is not printed — and each
  product once: the recorded showcase prints 27 cards of 19 products, a
  second block repeating eight, so it stays under the line). The spec's
  other branch («manufacturer = Chinese brand») needs the brand list
  (``collectors/brands.py``, T-34): owed.
* **negative** — an unknown showcase is a soft 404: a redirect to
  ``/showcases/`` («Наши партнеры») answering 200, so the parser checks the
  final URL's path and the ``LocalBusiness``, never the status.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlsplit

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.directories import common
from clientscrapper.collectors.directories.markup import Node, parse, scripts
from clientscrapper.services.keys import instagram_key
from clientscrapper.services.normalise import telegram_key

SITE: Final = "glotr"
HOST: Final = "glotr.uz"
BASE: Final = f"https://{HOST}"
SOURCE_KEY: Final = "directories_glotr"
_SLUG = r"([a-z0-9][a-z0-9-]{0,199})"
SHOWCASE_HREF: Final = re.compile(rf"\A/showcase/{_SLUG}/(?:\?[^#]*)?\Z")
SHOWCASE_PATH: Final = re.compile(rf"\A/showcase/{_SLUG}/\Z")
# A product's own page: «…-p-<id>/». The showcase repeats some products in a
# second block (8 of the recorded 27 cards), so SKUs are counted by this id.
PRODUCT_HREF: Final = re.compile(r"-p-(\d{1,12})/\Z")
CHECKED: Final = "Проверено"
SOLIQ: Final = "Soliq.uz"
# The product titles a showcase's goods are read from (page 1).
GOODS_TITLES: Final = 20
# 03 §2.6's categories: oborudovanie is recorded; mebel and sanoat-uskunalari
# are the same page kind, not fetched (README «STILL UNVERIFIED»). The
# region sub-categories are each their own page 1 (links seen on the recorded
# page), Tashkent first.
SEEDS: Final = (
    f"{BASE}/oborudovanie/",
    f"{BASE}/oborudovanie-v-tashkente/",
    f"{BASE}/mebel/",
    f"{BASE}/sanoat-uskunalari/",
)


def showcase_url(href: str | None) -> str | None:
    """The seller's showcase, query dropped, when the href is the selector's
    shape."""
    match = SHOWCASE_HREF.match(href or "")
    return f"{BASE}/showcase/{match.group(1)}/" if match else None


def parse_list(body: bytes, *, url: str, fetched_at: datetime) -> common.ListPage:
    """A category page: its sellers, each once, in page order. No next link:
    robots.txt refuses ``?page=``. A seller row alone is no company record
    (a name and a link), so the entries carry no observation."""
    root = parse(body)
    entries: list[common.Entry] = []
    for block in root.find_all("div", cls="product_company"):
        link = block.find("a", cls="product_company_link")
        target = showcase_url(link.get("href")) if link else None
        if target is None:
            continue
        slug = SHOWCASE_PATH.match(urlsplit(target).path)
        assert slug is not None
        ref = f"{SITE}:{slug.group(1)}"
        if all(e.ref != ref for e in entries):
            entries.append(common.Entry(ref, target))
    counter = root.find("span", attr=("id", "total_proposals_count"))
    digits = re.sub(r"\D", "", counter.text()) if counter else ""
    return common.ListPage(
        entries=tuple(entries), total=int(digits) if digits else None, page=1
    )


def _business(root: Node) -> Mapping[str, Any] | None:
    for text in scripts(root, type_="application/ld+json"):
        try:
            data = json.loads(text)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, Mapping) and item.get("@type") == "LocalBusiness":
                return item
    return None


def _string(value: Any) -> str | None:
    return common.squash(value) if isinstance(value, str) else None


def _handle(links: list[str], host: str, key: Callable[[str], str | None]) -> str | None:
    """The first link on ``host`` that names a usable handle."""
    for link in links:
        if host in link:
            found = key(link)
            if found:
                return found
    return None


def _website(root: Node) -> str | None:
    listing = root.find("ul", cls="seller-info__list")
    for item in listing.find_all("li") if listing else []:
        if item.find("i", cls="fa-globe") is not None:
            return common.squash(item.text())
    return None


def parse_showcase(body: bytes, *, url: str, fetched_at: datetime) -> list[Observation]:
    """A seller's showcase → one observation; [] for anything else (the soft
    404 lands on ``/showcases/``)."""
    match = SHOWCASE_PATH.match(urlsplit(url).path)
    if match is None:
        return []
    root = parse(body)
    business = _business(root)
    heading = root.find("h1")
    if business is None or heading is None:
        return []
    slug = match.group(1)
    raw = common.squash(heading.text())
    # An individual entrepreneur's name is a person's: kept in no field.
    name = common.company_name(raw)
    given = business.get("address")
    address: Mapping[str, Any] = given if isinstance(given, Mapping) else {}
    links = [s for s in business.get("sameAs") or [] if isinstance(s, str)]
    handle = _handle(links, "t.me/", telegram_key)
    insta = _handle(links, "instagram.com/", instagram_key)
    checked = root.find("div", cls="seller-info__checked")
    note = checked.text() if checked else ""
    solid = CHECKED in note and SOLIQ in note
    products: dict[str, str | None] = {}
    for card in root.find_all("div", cls="product-card__parent"):
        link = card.find("a", cls="product-card__link")
        product = PRODUCT_HREF.search(link.get("href") or "") if link else None
        if link is not None and product is not None:
            products.setdefault(product.group(1), common.squash(link.text()))
    titles = [t for t in products.values() if t][:GOODS_TITLES]
    description = _string(business.get("description"))
    attrs: dict[str, Any] = {"site": SITE, "sku_count": len(products)}
    if solid:
        attrs["checked"] = "soliq"
    phone = _string(business.get("telephone"))
    obs = Observation(
        source=SOURCE_KEY,
        source_ref=f"{SITE}:{slug}",
        url=url,
        fetched_at=fetched_at,
        raw_sha256=common.sha256(body),
        kind="company",
        company_name_raw=name,
        legal_form=common.legal_form_in(raw),
        phones=common.phones_as_written([phone] if phone else []),
        telegram=handle,
        instagram=insta,
        website=_website(root),
        address=_string(address.get("streetAddress")),
        city=_string(address.get("addressRegion")),
        goods_text=common.joined([description, *titles]),
        excerpt=common.excerpt(name, description),
        external_ids={SITE: slug},
        attrs=attrs,
        lang="ru",
    )
    verdict = common.judge(
        name=name,
        own=description or "",
        goods=common.joined([description, *titles]) or "",
        wholesale=False,
        china=None,
        solid=solid,
        skus=len(products),
    )
    return [common.apply(obs, verdict)]


SPEC: Final = common.SiteSpec(
    site=SITE,
    host=HOST,
    source_key=SOURCE_KEY,
    parse_list=parse_list,
    parse_card=parse_showcase,
    seeds=SEEDS,
    paginated=False,
)
