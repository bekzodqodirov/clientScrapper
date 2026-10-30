"""A Telegram channel's public preview page, ``https://t.me/s/<username>``
(04 §3 E3; 03 §2.3 reads the same page for the join check).

Enrichment reads a company's channel *about* from this page through the
Fetcher — the worker never imports Telethon (06 §2, 07 §1.2
``test_collector_imports``). ``parse_channel_page`` returns the channel's
title, description and its newest posts (the channel speaking for itself),
or None when the page is no channel preview: a user account (``t.me/s/<a
person>`` redirects to the person's page), a private channel, a name nobody
holds. A person's page is never read for anything — a bio is person-tier
data (06 §5.1) — and E3 then queues the username for the reader instead
(``userbot/about.py`` drains it).

No real page is recorded yet: the markup is Telegram's widget classes as
built over ``tests/fixtures/company_sites/tme_*_synthetic.html``, STILL
UNVERIFIED until a page is recorded from the VPS.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.company_site import (
    SITE_SOURCE,
    address_line,
    own_markers,
    text_phones,
    unique_phones,
    whatsapp_phone,
)
from clientscrapper.services import markup
from clientscrapper.services.normalise import (
    find_stir,
    instagram_handles,
    telegram_key,
    website_key,
)

PUBLIC_PAGE: Final = "https://t.me/s/{username}"
POSTS_READ: Final = 5
EXCERPT_MAX: Final = 600
PHONES_MAX: Final = 5


@dataclass(frozen=True, slots=True)
class ChannelPage:
    username: str
    title: str | None
    description: str | None
    posts: tuple[str, ...]
    newest_post_at: datetime | None
    links: tuple[str, ...]


def public_url(username: str) -> str:
    return PUBLIC_PAGE.format(username=username)


def parse_channel_page(body: bytes, *, url: str) -> ChannelPage | None:
    """The channel preview, or None for any other page (a user's, a missing
    channel's): only ``tgme_channel_info`` is a channel speaking."""
    root = markup.parse(markup.decode(body))
    info = root.find("div", cls="tgme_channel_info")
    if info is None:
        return None
    title_node = info.find("div", cls="tgme_channel_info_header_title")
    name_node = info.find("div", cls="tgme_channel_info_header_username")
    description_node = info.find("div", cls="tgme_channel_info_description")
    username = telegram_key(name_node.text() if name_node is not None else None)
    username = username or telegram_key(url)
    if username is None:
        return None
    posts = [
        node
        for node in root.find_all("div", cls="tgme_widget_message_text")
        if node.text()
    ]
    stamps: list[datetime] = []
    for stamp in root.find_all("time"):
        try:
            when = datetime.fromisoformat(stamp.get("datetime") or "")
        except ValueError:
            continue
        if when.tzinfo is not None:
            stamps.append(when)
    links = [
        a.get("href") or ""
        for node in ([description_node] if description_node is not None else [])
        + posts[-POSTS_READ:]
        for a in node.find_all("a")
    ]
    return ChannelPage(
        username=username,
        title=markup.collapse(title_node.text()) if title_node is not None else None,
        description="\n".join(description_node.lines()) if description_node else None,
        posts=tuple("\n".join(p.lines()) for p in posts[-POSTS_READ:]),
        newest_post_at=max(stamps) if stamps else None,
        links=tuple(h for h in links if h),
    )


def channel_observation(
    page: ChannelPage, *, url: str, fetched_at: datetime, raw_sha256: str = ""
) -> Observation:
    """The channel's about as a ``company`` observation (stored kind
    ``profile``, 02 §2.5 «telegram about (E3)»): its phones, the website and
    Instagram it names, an address line, a labelled STIR — the description
    first, then its newest posts."""
    texts = [t for t in (page.description, *reversed(page.posts)) if t]
    text = "\n".join(texts)
    phones: list[str] = []
    for href in page.links:
        if href.lower().startswith("tel:"):
            phones.append(href[4:].strip())
        elif (wa := whatsapp_phone(href)) is not None:
            phones.append(wa)
    phones += text_phones(text)
    # ``website_key`` refuses t.me itself, a link hub and a shortener.
    website = next(
        (
            h
            for h in page.links
            if h.startswith(("http://", "https://")) and website_key(h) is not None
        ),
        None,
    )
    instagram = next(iter(instagram_handles(text)), None)
    address = address_line(line for t in texts for line in t.splitlines())
    stir = find_stir(text)
    types = own_markers(page.title, "\n".join(p for p in (page.title, text) if p))
    if stir is not None:
        types.append("entity_solid")
    excerpt = " · ".join(t for t in (page.description, *page.posts[-1:]) if t)
    if len(excerpt) > EXCERPT_MAX:
        excerpt = excerpt[:EXCERPT_MAX].rsplit(" ", 1)[0].rstrip()
    return Observation(
        source=SITE_SOURCE,
        source_ref=f"tme:{page.username}",
        url=url,
        fetched_at=fetched_at,
        raw_sha256=raw_sha256,
        kind="company",
        company_name_raw=page.title,
        stir=stir,
        phones=unique_phones(phones, limit=PHONES_MAX),
        telegram=page.username,
        instagram=instagram,
        website=website,
        address=address,
        signal_types=types,
        excerpt=excerpt,
        attrs={"title": page.title, "page": "telegram_channel"},
        published_at=page.newest_post_at,
    )


__all__ = [
    "PUBLIC_PAGE",
    "ChannelPage",
    "channel_observation",
    "parse_channel_page",
    "public_url",
]
