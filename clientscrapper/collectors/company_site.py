"""A company's own pages: its website, its link hub (03 §2.1, 04 §3 E2).

Pure parsers over bytes, shared by enrichment step E2 (``services/enrich``)
and T-25's ``serp_layer`` (which fetches the company sites its hits point
at). No recorded company site exists yet: the shapes are built over the
SYNTHETIC pages in ``tests/fixtures/company_sites/`` (their README says what
each stands for) and stay UNVERIFIED until real pages are recorded from the
VPS.

``parse_company_site`` (03 §2.1) reads one landing or about page and returns
one ``kind="company"`` observation:

* ``company_name_raw`` — the page's ``<title>`` (its first segment that is
  not a generic word such as «Bosh sahifa»/«Главная»/«Home»), else
  ``og:site_name``, else the schema.org ``Organization`` name, else the
  footer sentence carrying a legal form. The title is the name gate 3 reads
  for «website title in Chinese» (04 §6: an ``enrich_site`` self-description's
  ``name_raw``), so it is kept as written; the whole ``<title>`` is in
  ``attrs.title``;
* phones — ``tel:`` links first, then the JSON-LD ``telephone``, then
  ``PHONE_RE`` over the visible lines; at most ``PHONES_MAX``;
* Telegram and Instagram from links and ``TG_RE``/``IG_RE`` in the text, the
  JSON-LD ``sameAs`` too; the website is the page's own;
* the STIR from a labelled ``STIR_RE`` in the text or the JSON-LD
  ``taxID``/``vatID``; a legal form from the footer sentence;
* the address: a line naming a place (``GEO_RE``) and a street word
  (``STREET_RE``: koʻcha, улица, ул., проспект, MFY …), or the JSON-LD
  ``PostalAddress``;
* ``signal_types``: ``entity_solid`` for a parsed STIR or legal block (→
  ``ent_not_micro``; 04 §5.3: shape, never a registry confirmation),
  ``wholesale_bulky`` for a B2B word beside a bulky category,
  ``china_origin`` for a China word, and ``competitor`` only through the
  cargo core — ``COMPETITOR_NAME_RE`` on the name and ``COMPETITOR_SELF_RE``
  on the page's own words (03 §1.4: every importer writes «yetkazib
  beramiz»). ``brand_distributor`` needs T-34's brand table and is not
  emitted here.

A page whose visible text is a few words over a script bundle is
``js_only`` (04 §3: ``result='js_only'``): no observation. ``about_link``
finds the one second page E2 may read (``/about``, ``/kontakty`` …, same
site). ``parse_link_hub`` reads a taplink/linktr.ee page for its links;
``whatsapp_phone`` reads a ``wa.me`` link's number without fetching it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Final
from urllib.parse import unquote, urljoin, urlsplit

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.keywords import (
    B2B_RE,
    CHINA_RE,
    COMPETITOR_NAME_RE,
    COMPETITOR_SELF_RE,
    GEO_RE,
    LEGAL_FORM_RE,
    bulky_categories,
    intl_phones,
)
from clientscrapper.services import markup
from clientscrapper.services.keys import domain_key, instagram_key, stir_key
from clientscrapper.services.normalise import (
    LINK_HUB_HOSTS,
    find_stir,
    instagram_handles,
    make_phone,
    phone_matches,
    telegram_handles,
    telegram_key,
    website_key,
)

SITE_SOURCE: Final = "enrich_site"
PHONES_MAX: Final = 5
EXCERPT_MAX: Final = 600
# A visible text this short over a script is an empty shell a browser fills.
JS_ONLY_TEXT: Final = 80
# The second page E2 reads (03 §2.1), in order of preference.
ABOUT_PATHS: Final = (
    "/about",
    "/o-kompanii",
    "/kompaniya-haqida",
    "/biz-haqimizda",
    "/contacts",
    "/kontakty",
    "/aloqa",
)
STREET_RE: Final = re.compile(
    r"(?<!\w)(?:ko[ʻʼ'`‘’]?cha(?:si)?|kucha|улиц\w*|ул\.|проспект\w*|prospekt\w*|"
    r"пр-т|MFY|МФЙ|mahalla\w*|massiv\w*|мавзе\w*|mavze\w*)",
    re.I,
)
# Title segments that name no firm.
GENERIC_TITLE: Final = frozenset(
    {
        "home",
        "main",
        "главная",
        "главная страница",
        "bosh sahifa",
        "asosiy",
        "asosiy sahifa",
        "o'zbekcha",
        "русский",
        "контакты",
        "kontaktlar",
        "aloqa",
        "о компании",
        "biz haqimizda",
    }
)
_TITLE_SEPARATORS: Final = re.compile(r"\s+[|—–\-:·•]\s+|\s*\|\s*")
WHATSAPP_HOSTS: Final = frozenset({"wa.me", "api.whatsapp.com"})
SHORTENER_HOSTS: Final = frozenset({"clck.ru", "bit.ly", "cutt.ly"})


# --- small readers -----------------------------------------------------------------


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def is_link_hub(url: str) -> bool:
    """A host of ``normalise.LINK_HUB_HOSTS`` (04 §1.3): fetched once and
    parsed for its links, never a company's website."""
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in LINK_HUB_HOSTS)


def whatsapp_phone(url: str) -> str | None:
    """The number a ``wa.me/<digits>`` or ``api.whatsapp.com/send?phone=``
    link carries — read off the link, nothing fetched."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host not in WHATSAPP_HOSTS:
        return None
    digits = re.sub(r"\D", "", parts.path)
    if not digits:
        found = re.search(r"(?:^|&)phone=([+\d]+)", parts.query)
        digits = re.sub(r"\D", "", found.group(1)) if found else ""
    return f"+{digits}" if 9 <= len(digits) <= 15 else None


def _meta(root: markup.Node, *names: str) -> str | None:
    for meta in root.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in names:
            value = markup.collapse(meta.get("content"))
            if value:
                return value
    return None


def _organization(root: markup.Node) -> Mapping[str, Any]:
    kinds = {"Organization", "LocalBusiness", "Corporation", "Store", "WholesaleStore"}
    for item in markup.json_ld(root):
        if not isinstance(item, Mapping):
            continue
        kind = item.get("@type")
        names = set(kind) if isinstance(kind, list) else {kind}
        if names & kinds:
            return item
    return {}


def _str(value: Any) -> str | None:
    return markup.collapse(value) if isinstance(value, str) else None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _title_name(title: str | None) -> str | None:
    if not title:
        return None
    for part in _TITLE_SEPARATORS.split(title):
        clean = markup.collapse(part)
        if clean and clean.lower() not in GENERIC_TITLE:
            return clean
    return None


def _hrefs(root: markup.Node, base: str) -> list[str]:
    found = []
    for link in root.find_all("a"):
        href = (link.get("href") or "").strip()
        if href:
            found.append(
                href
                if href.lower().startswith(("tel:", "mailto:"))
                else urljoin(base, href)
            )
    return found


def text_phones(text: str) -> list[str]:
    """``PHONE_RE``'s numbers in ``text`` as written, none inside a foreign
    number (``+86 579 …`` is no Uzbek number) or a labelled STIR."""
    foreign = intl_phones(text)
    return [
        m.group(0)
        for m in phone_matches(text)
        if not any(s < m.end() and m.start() < e for s, e in foreign)
    ]


def unique_phones(raw: Iterable[str], *, limit: int = PHONES_MAX) -> list[str]:
    """Numbers as written, one per phone (by tail; a foreign one by its
    digits), at most ``limit`` — the normaliser decides which are kept."""
    found: list[str] = []
    seen: set[str] = set()
    for value in raw:
        phone = make_phone(value)
        key = phone.tail if phone is not None else re.sub(r"\D", "", value)
        if not value or not key or key in seen:
            continue
        seen.add(key)
        found.append(value)
        if len(found) >= limit:
            break
    return found


def _phones(
    org: Mapping[str, Any], lines: Iterable[str], hrefs: Iterable[str]
) -> list[str]:
    raw: list[str] = []
    for href in hrefs:
        if href.lower().startswith("tel:"):
            raw.append(unquote(href[4:]).strip())
        elif (wa := whatsapp_phone(href)) is not None:
            raw.append(wa)
    raw += _strings(org.get("telephone"))
    points = org.get("contactPoint")
    for point in points if isinstance(points, list) else [points]:
        if isinstance(point, Mapping):
            raw += _strings(point.get("telephone"))
    for line in lines:
        raw += text_phones(line)
    return unique_phones(raw)


def _telegram(hrefs: Iterable[str], text: str, same_as: Iterable[str]) -> str | None:
    for href in [*hrefs, *same_as]:
        host = _host(href)
        if host in ("t.me", "telegram.me", "telegram.dog"):
            key = telegram_key(href)
            if key is not None:
                return key
    found = telegram_handles(text)
    return found[0] if found else None


def _instagram(hrefs: Iterable[str], text: str, same_as: Iterable[str]) -> str | None:
    for href in [*hrefs, *same_as]:
        if _host(href) == "instagram.com":
            path = urlsplit(href).path.strip("/").split("/")
            key = instagram_key(path[0]) if path and path[0] else None
            if key is not None:
                return key
    found = instagram_handles(text)
    return found[0] if found else None


# «Manzil:», «Адрес:», «Офис:» before an address line.
_ADDRESS_LABEL: Final = re.compile(r"^[^\W\d_]{2,12}\s*:\s*")


def address_line(lines: Iterable[str]) -> str | None:
    """The first line naming a place (``GEO_RE``) and a street word
    (``STREET_RE``), its label dropped."""
    for line in lines:
        if len(line) <= 300 and GEO_RE.search(line) and STREET_RE.search(line):
            return _ADDRESS_LABEL.sub("", line).strip() or None
    return None


def _address(lines: Iterable[str], org: Mapping[str, Any]) -> str | None:
    found = address_line(lines)
    if found is not None:
        return found
    address = org.get("address")
    if isinstance(address, Mapping):
        parts = [
            _str(address.get(k))
            for k in ("addressLocality", "addressRegion", "streetAddress")
        ]
        joined = markup.collapse(", ".join(p for p in parts if p))
        return joined[:300] if joined else None
    return _str(address)


def _legal_line(lines: Iterable[str]) -> str | None:
    """The footer sentence with a legal form («© 2026 "Namuna Tekstil" MChJ»)."""
    for line in reversed(list(lines)):
        if len(line) <= 160 and LEGAL_FORM_RE.search(line):
            return line
    return None


def _excerpt(*parts: str | None) -> str:
    text = " · ".join(dict.fromkeys(p for p in parts if p))
    if len(text) > EXCERPT_MAX:
        text = text[:EXCERPT_MAX].rsplit(" ", 1)[0].rstrip()
    return text


def own_markers(name: str | None, own: str) -> list[str]:
    """The page's own types (03 §2.1): wholesale beside a bulky category,
    a China word, and the cargo core — by name and by self-description."""
    types: list[str] = []
    if B2B_RE.search(own) and bulky_categories(own):
        types.append("wholesale_bulky")
    if CHINA_RE.search(own):
        types.append("china_origin")
    if (name and COMPETITOR_NAME_RE.search(name)) or COMPETITOR_SELF_RE.search(own):
        types.append("competitor")
    return types


def is_js_only(root: markup.Node) -> bool:
    """A shell a browser fills: almost no visible text over script tags."""
    body = root.find("body") or root
    return len(body.text()) < JS_ONLY_TEXT and bool(root.find_all("script"))


def about_link(body: bytes, *, url: str) -> str | None:
    """The one second page 03 §2.1 reads: a same-site link whose path starts
    with one of ``ABOUT_PATHS`` (their order decides), never the page itself."""
    root = markup.parse(markup.decode(body))
    own = domain_key(url)
    candidates: list[tuple[int, str]] = []
    for href in _hrefs(root, url):
        if not href.startswith(("http://", "https://")) or domain_key(href) != own:
            continue
        path = urlsplit(href).path.rstrip("/").lower()
        for rank, prefix in enumerate(ABOUT_PATHS):
            if path == prefix or path.startswith(prefix + "/") or path.endswith(prefix):
                candidates.append((rank, href.split("#", 1)[0]))
                break
    candidates = [c for c in candidates if c[1].rstrip("/") != url.rstrip("/")]
    return min(candidates)[1] if candidates else None


# --- the parsers ------------------------------------------------------------------------


def parse_company_site(
    body: bytes,
    *,
    url: str,
    fetched_at: datetime,
    raw_sha256: str = "",
    content_type: str = "text/html",
    source: str = SITE_SOURCE,
) -> list[Observation]:
    """One page of a company's own website → one ``company`` observation
    (stored kind ``profile``), or none for a JS-only shell."""
    root = markup.parse(markup.decode(body, content_type))
    if is_js_only(root):
        return []
    title_node = root.find("title")
    title = markup.collapse(title_node.text()) if title_node is not None else None
    org = _organization(root)
    lines = (root.find("body") or root).lines()
    text = " \n".join(lines)
    hrefs = _hrefs(root, url)
    same_as = _strings(org.get("sameAs"))
    legal = _legal_line(lines)
    name = (
        _title_name(title)
        or _meta(root, "og:site_name")
        or _str(org.get("name"))
        or _str(org.get("legalName"))
        or legal
    )
    stir = (
        find_stir(text)
        or stir_key(_str(org.get("taxID")))
        or stir_key(_str(org.get("vatID")))
    )
    legal_form_raw = None
    for candidate in (legal, _str(org.get("legalName")), name):
        found = LEGAL_FORM_RE.search(candidate) if candidate else None
        if found is not None:
            legal_form_raw = found.group(0)
            break
    description = _meta(root, "description", "og:description") or _str(
        org.get("description")
    )
    heading = root.find("h1")
    paragraphs = [p.text() for p in root.find_all("p") if p.text()]
    about = _excerpt(description, heading.text() if heading else None, *paragraphs[:4])
    own = "\n".join(p for p in (name, description, *lines) if p)
    types = own_markers(name, own)
    if stir is not None or legal is not None:
        types.append("entity_solid")
    website = url if website_key(url) is not None else None
    return [
        Observation(
            source=source,
            source_ref=f"site:{domain_key(url) or _host(url)}{urlsplit(url).path}",
            url=url,
            fetched_at=fetched_at,
            raw_sha256=raw_sha256,
            kind="company",
            company_name_raw=name,
            legal_form=legal_form_raw,
            stir=stir,
            phones=_phones(org, lines, hrefs),
            telegram=_telegram(hrefs, text, same_as),
            instagram=_instagram(hrefs, text, same_as),
            website=website,
            address=_address(lines, org),
            signal_types=types,
            excerpt=about,
            attrs={"title": title, "page": "site"},
        )
    ]


def parse_link_hub(
    body: bytes,
    *,
    url: str,
    fetched_at: datetime,
    raw_sha256: str = "",
    content_type: str = "text/html",
    source: str = SITE_SOURCE,
) -> list[Observation]:
    """A link hub page (taplink, linktr.ee …): its phones (``tel:`` and
    WhatsApp links), Telegram, Instagram and the first link to a website of
    the company's own (``website_key``: no social host, no other hub)."""
    root = markup.parse(markup.decode(body, content_type))
    hrefs = _hrefs(root, url)
    lines = (root.find("body") or root).lines()
    text = " \n".join(lines)
    title_node = root.find("title")
    title = markup.collapse(title_node.text()) if title_node is not None else None
    website = next(
        (
            h
            for h in hrefs
            if h.startswith(("http://", "https://"))
            and not is_link_hub(h)
            and website_key(h) is not None
            and domain_key(h) != domain_key(url)
        ),
        None,
    )
    phones = _phones({}, lines, hrefs)
    telegram = _telegram(hrefs, text, ())
    instagram = _instagram(hrefs, text, ())
    if not (phones or telegram or instagram or website):
        return []
    name = _title_name(title) or _meta(root, "og:title")
    return [
        Observation(
            source=source,
            source_ref=f"hub:{_host(url)}{urlsplit(url).path}",
            url=url,
            fetched_at=fetched_at,
            raw_sha256=raw_sha256,
            kind="company",
            company_name_raw=name,
            phones=phones,
            telegram=telegram,
            instagram=instagram,
            website=website,
            signal_types=[t for t in own_markers(name, text) if t != "wholesale_bulky"],
            excerpt=_excerpt(_meta(root, "description", "og:description"), *lines[:3]),
            attrs={"title": title, "page": "link_hub"},
        )
    ]


def whatsapp_observation(
    url: str, *, fetched_at: datetime, raw_sha256: str = "", source: str = SITE_SOURCE
) -> list[Observation]:
    """A WhatsApp link the company published (in its bio, its site): the
    number it carries as the company's phone, cited at the link itself."""
    phone = whatsapp_phone(url)
    if phone is None:
        return []
    return [
        Observation(
            source=source,
            source_ref=f"wa:{phone.lstrip('+')}",
            url=url,
            fetched_at=fetched_at,
            raw_sha256=raw_sha256,
            kind="company",
            phones=[phone],
            excerpt=url,
            attrs={"page": "whatsapp_link"},
        )
    ]


__all__ = [
    "ABOUT_PATHS",
    "PHONES_MAX",
    "SHORTENER_HOSTS",
    "SITE_SOURCE",
    "STREET_RE",
    "WHATSAPP_HOSTS",
    "about_link",
    "address_line",
    "is_js_only",
    "is_link_hub",
    "own_markers",
    "parse_company_site",
    "parse_link_hub",
    "text_phones",
    "unique_phones",
    "whatsapp_observation",
    "whatsapp_phone",
]
