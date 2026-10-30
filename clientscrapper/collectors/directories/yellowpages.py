"""yellowpages.uz (03 §2.6, T-22): rubrika and teg pages → company cards.

The site is Nuxt 3 SSR: the parser reads ``script#__NUXT_DATA__`` (devalue,
``nuxt.py``), never the HTML, whose phone is masked («+99871 ...Позвонить»)
while ``contacts.phone[]`` carries the number as served — no reveal request
exists or is made (tests/fixtures/directories/README.md «yellowpages.uz»,
VERIFIED). A list page's organisations are the payload's
``data``/``links``/``meta`` block (15 per page); each carries ``id, name,
slug, brand_name, official_name, inn, address`` (HTML), ``rubrics``, ``tags``
and ``contacts{phone, emails, websites, telegram, faxes, instagram, …}`` —
a ``null`` list is accepted as an empty one. The card (``/kompaniya/<slug>``)
is the same organisation with its ``description``; an unknown slug is a real
404 whose payload holds no organisation.

* **Pagination — STILL UNVERIFIED.** The pager is drawn client-side and
  ``?page=2`` is ignored by the server render, so the plan reads page 1 of
  each seed only (``paginated=False``) and the run notes the pages it could
  not reach (the payload's ``last_page``).
* **Phones** — ``contacts.phone`` then ``contacts.faxes`` (03 §2.6: «phones
  (sometimes only fax/e-mail)»), the same number once. A row with only a fax
  or an e-mail has no recording (README): the fax branch is read, unproven.
* **STIR** — ``inn`` where printed and a legal entity's (``common.entity_stir``:
  nine digits, the first 2–3); never an individual entrepreneur's (``ИндП``,
  ``ИП``…, or a 4–6 number): that is a natural person's taxpayer number, and
  the name before the form is the person's — such a row is named by its
  ``brand_name`` (``common.company_name``), and a person's name is kept in no
  field, ``attrs.brand_name`` included.
* **China** — the tags 03 §2.6 names, ``tovary-iz-kitaya`` and
  ``mebel-iz-kitaya``, give ``china_origin``; the one recorded
  ``tovary-iz-kitaya`` firm is a logistics company, which the competitor rule
  takes first (``common.judge``).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlsplit

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.directories import common, nuxt
from clientscrapper.collectors.directories.markup import parse
from clientscrapper.services.keys import instagram_key
from clientscrapper.services.normalise import telegram_key

SITE: Final = "yellowpages"
HOST: Final = "www.yellowpages.uz"
BASE: Final = f"https://{HOST}"
SOURCE_KEY: Final = "directories_yellowpages"
SLUG: Final = re.compile(r"\A[a-z0-9][a-z0-9-]{0,199}\Z")
CARD_PATH: Final = re.compile(r"\A/kompaniya/([a-z0-9][a-z0-9-]{0,199})/?\Z")
# 03 §2.6: «/teg/tovary-iz-kitaya and mebel-iz-kitaya give china_origin».
CHINA_TAGS: Final = frozenset({"tovary-iz-kitaya", "mebel-iz-kitaya"})
# 03 §2.6's seeds. The rubrika page is recorded (stationery wholesale,
# Tashkent); the rubric index the spec picks slugs from is not (owed). The
# teg seeds answered 200 when checked; only tovary-iz-kitaya's body is kept.
SEEDS: Final = (
    f"{BASE}/rubrika/optovaya-torgovlya-kanctovarami-kancelyarskimi-tovarami/tashkent",
    *(
        f"{BASE}/teg/{tag}"
        for tag in (
            "tovary-iz-kitaya",
            "mebel-iz-kitaya",
            "mebel-optom",
            "optovaya-torgovlya-hoztovarami",
            "optovaya-torgovlya-strojmaterialami",
            "optovaya-torgovlya-ofisnymi-tovarami",
            "optovaya-torgovlya-bumagoj",
            "optovaya-prodazha",
            "optovaya-torgovlya-v-uzbekistane",
        )
    ),
)


def card_url(slug: Any) -> str | None:
    if not isinstance(slug, str) or SLUG.match(slug) is None:
        return None
    return f"{BASE}/kompaniya/{slug}"


def _named(items: Any, key: str) -> list[str]:
    found: list[str] = []
    for item in items if isinstance(items, list) else []:
        value = common.squash(item.get(key)) if isinstance(item, dict) else None
        if value and value not in found:
            found.append(value)
    return found


def _city(address: Any) -> str | None:
    if not isinstance(address, str):
        return None
    link = parse(address.encode("utf-8")).find("a", prefix=("href", "/gorod/"))
    return common.squash(link.text()) if link else None


def _text(value: Any) -> str | None:
    return common.squash(value) if isinstance(value, str) else None


def _first(values: list[str], key: Any) -> str | None:
    for value in values:
        found = key(value)
        if isinstance(found, str) and found:
            return found
    return None


def organisation(
    org: dict[str, Any], *, url: str, fetched_at: datetime, sha: str
) -> Observation | None:
    """One organisation of the payload → an observation (None without an id)."""
    ident = org.get("id")
    if isinstance(ident, bool) or not isinstance(ident, int):
        return None
    legal, brand = _text(org.get("name")), _text(org.get("brand_name"))
    person = common.individual(legal)
    # The name before «ИндП» is a person's: the shop's own name instead.
    name = common.company_name(legal, brand)
    stir = common.entity_stir(org.get("inn"), person=person)
    given = org.get("contacts")
    contacts: dict[str, Any] = given if isinstance(given, dict) else {}
    phones = common.phones_as_written(
        [*common.strings(contacts.get("phone")), *common.strings(contacts.get("faxes"))]
    )
    sites = common.strings(contacts.get("websites"))
    rubrics = _named(org.get("rubrics"), "name")
    rubric_slugs = _named(org.get("rubrics"), "slug")
    tags = _named(org.get("tags"), "name")
    tag_slugs = _named(org.get("tags"), "slug")
    description = common.plain(org.get("description")) or common.plain(org.get("about"))
    china_tags = [
        tag.get("name")
        for tag in org.get("tags") or []
        if isinstance(tag, dict) and tag.get("slug") in CHINA_TAGS
    ]
    china = _text(china_tags[0]) if china_tags else None
    attrs: dict[str, Any] = {"site": SITE, "rubrics": rubrics, "tags": tag_slugs}
    shop = common.company_name(brand)
    if shop and shop != name:
        attrs["brand_name"] = shop
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
        legal_form=common.legal_form_in(legal),
        stir=stir,
        phones=phones,
        telegram=_first(common.strings(contacts.get("telegram")), telegram_key),
        instagram=_first(common.strings(contacts.get("instagram")), instagram_key),
        website=sites[0] if sites else None,
        address=common.plain(org.get("address")),
        city=_city(org.get("address")),
        goods_text=common.joined([*rubrics, *tags, description]),
        excerpt=common.excerpt(name, china, description, *rubrics, *tags),
        external_ids={SITE: str(ident)},
        attrs=attrs,
        # The entry's own date (03 §2.6 as built: every site alike).
        published_at=common.stamp(org.get("updated_at"), fetched_at=fetched_at),
        lang="ru",
    )
    own = common.joined([description, *rubrics, *tags]) or ""
    verdict = common.judge(
        name=name,
        own=own,
        goods=own,
        wholesale=common.wholesale(*rubrics, *rubric_slugs, *tags),
        china=china,
    )
    return common.apply(obs, verdict)


def parse_list(body: bytes, *, url: str, fetched_at: datetime) -> common.ListPage:
    """A rubrika or teg page: its organisations, each complete (contacts and
    goods are in the payload row); no next link (pagination unverified)."""
    block = nuxt.list_block(nuxt.payload(parse(body)))
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
    return common.ListPage(
        entries=tuple(entries),
        total=_int(meta.get("total")),
        page=_int(meta.get("current_page")),
        last_page=_int(meta.get("last_page")),
    )


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_rubrika(body: bytes, *, url: str, fetched_at: datetime) -> list[Observation]:
    """A rubrika (or teg) page's organisations as observations."""
    page = parse_list(body, url=url, fetched_at=fetched_at)
    return [e.observation for e in page.entries if e.observation is not None]


def parse_kompaniya(body: bytes, *, url: str, fetched_at: datetime) -> list[Observation]:
    """A company card → one observation; [] for a slug the payload does not
    hold (the 404 page)."""
    match = CARD_PATH.match(urlsplit(url).path)
    if match is None:
        return []
    org = nuxt.organisation(nuxt.payload(parse(body)), match.group(1), needs="contacts")
    if org is None:
        return []
    obs = organisation(org, url=url, fetched_at=fetched_at, sha=common.sha256(body))
    return [obs] if obs is not None else []


SPEC: Final = common.SiteSpec(
    site=SITE,
    host=HOST,
    source_key=SOURCE_KEY,
    parse_list=parse_list,
    parse_card=parse_kompaniya,
    seeds=SEEDS,
    paginated=False,
)
