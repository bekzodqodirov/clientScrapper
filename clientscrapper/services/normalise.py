"""The normaliser (04 §1): raw collector fields and free text → stored shapes.

``services/keys.py`` holds the key functions of 02 §1 (the three with SQL
mirrors and the rest of that list); this module turns text into them: phones
found in a post, handles found in a bio with Telegram's reserved paths
dropped, the website filter for link hubs and social subdomains, labelled
STIRs, legal forms, the city, and the person-shaped-name predicate. 04 §1.1
spells ``phone_e164`` / ``phone_tail`` / ``phone_kind`` as part of this
module; they are re-exported from keys so both spellings name one function.

Pure: no I/O, no clock, no settings — and no import of ``clientscrapper.db``
(its package import opens the engine); ``tests/test_normalise.py`` holds
``LegalForm`` equal to ``db.enums.LEGAL_FORMS``.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal

from clientscrapper.collectors.keywords import (
    CITY_RES,
    IG_RE,
    INTENT_RE,
    LEGAL_FORM_RE,
    PHONE_RE,
    STIR_RE,
    TASHKENT_PLACES_RE,
    TG_RE,
)
from clientscrapper.services.keys import (
    LEGAL_FORM_TOKENS_RE,
    NOT_WEBSITE_HOSTS,
    TENANT_HOSTS,
    PhoneKind,
    domain_key,
    instagram_key,
    loose_key,
    name_key,
    phone_e164,
    phone_kind,
    phone_tail,
    stir_key,
    username_key,
)
from clientscrapper.services.names import FIRST_NAMES
from clientscrapper.services.text import fold_apostrophes, storable, to_latin
from clientscrapper.services.urlsafe import strip_secrets

if TYPE_CHECKING:
    # Type only: collectors/base imports the Fetcher, and this module stays pure.
    from clientscrapper.collectors.base import Observation

__all__ = [
    "GENERIC_TOKENS",
    "LINK_HUB_HOSTS",
    "NOT_OBSERVATIONS",
    "PARSED_VERSION",
    "PERSON_HONORIFICS",
    "SHARED_HOSTS",
    "TASHKENT_REGION",
    "TG_RESERVED",
    "LegalForm",
    "ParsedKind",
    "Phone",
    "canonical_city",
    "find_legal_form",
    "find_stir",
    "instagram_handles",
    "is_person_shaped",
    "legal_form_code",
    "legal_form_in_name",
    "make_phone",
    "parse_phones",
    "parsed_kind",
    "phone_e164",
    "phone_kind",
    "phone_tail",
    "telegram_handles",
    "telegram_key",
    "to_observation_json",
    "website_key",
]

# --- Phones (04 §1.1) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Phone:
    e164: str  # '+998901234567'
    tail: str  # last 9 digits — the ERP identity, == contacts.phone_tail
    kind: PhoneKind
    country: Literal["UZ", "other"]
    raw: str


def make_phone(raw: str, *, city: str | None = None) -> Phone | None:
    """One phone as written (an ``Observation.phones`` item) → ``Phone``, or None
    when ``phone_e164`` refuses it. ``city`` lets a bare 7-digit Tashkent
    landline through (04 §1.1 rule 6)."""
    e164 = phone_e164(raw, city=city)
    if e164 is None:
        return None
    tail = phone_tail(e164)
    if tail is None:  # a foreign number of 7–8 digits has no ERP identity
        return None
    country: Literal["UZ", "other"] = "UZ" if e164.startswith("+998") else "other"
    return Phone(e164=e164, tail=tail, kind=phone_kind(e164), country=country, raw=raw)


def parse_phones(text: str, *, limit: int = 5) -> list[Phone]:
    """``PHONE_RE`` over ``text``: valid numbers, deduplicated by tail, in order of
    appearance, at most ``limit``. A number inside a labelled STIR («ИНН
    301234567») is the company's tax id, not a phone, and is skipped."""
    stir_spans = [m.span(1) for m in STIR_RE.finditer(text)]
    phones: list[Phone] = []
    seen: set[str] = set()
    for match in PHONE_RE.finditer(text):
        start, end = match.span()
        if any(s < end and start < e for s, e in stir_spans):
            continue
        phone = make_phone(match.group(0))
        if phone is None or phone.tail in seen:
            continue
        seen.add(phone.tail)
        phones.append(phone)
        if len(phones) >= limit:
            break
    return phones


# --- Handles (04 §1.2) --------------------------------------------------------

TG_RESERVED: Final = frozenset(
    {
        "joinchat",
        "addstickers",
        "addtheme",
        "share",
        "proxy",
        "socks",
        "iv",
        "s",
        "c",
        "login",
        "setlanguage",
        "confirmphone",
        "telegram",
        "durov",
    }
)
_TG_LINK = re.compile(
    r"\A(?:[A-Za-z]+://)?(?:www\.)?(?:t\.me|telegram\.me)/(?P<path>.*)\Z",
    re.I | re.DOTALL,
)


def telegram_key(raw: str | None) -> str | None:
    """A Telegram handle or link → ``username_key``, after dropping what is not a
    username: reserved paths (joinchat, share, addstickers, c/…, …), an invite
    (``t.me/+…``) and Telegram's own accounts. ``t.me/s/<name>`` is a channel's
    public preview page and names that channel."""
    if raw is None:
        return None
    text = raw.strip()
    link = _TG_LINK.match(text)
    if link is None:
        bare = text.removeprefix("@")
        return None if bare.lower() in TG_RESERVED else username_key(text)
    segments = link["path"].split("/")
    first = segments[0]
    if first.lower() == "s" and len(segments) > 1:
        first = segments[1]
    first = re.split(r"[?#]", first, maxsplit=1)[0]
    if first.startswith("+") or first.lower() in TG_RESERVED:
        return None
    return username_key(first)


def _unique(keys: list[str | None]) -> list[str]:
    return list(dict.fromkeys(k for k in keys if k is not None))


def telegram_handles(text: str) -> list[str]:
    """Every ``TG_RE`` hit in ``text`` that is a usable username, in order."""
    return _unique([telegram_key(m.group(0)) for m in TG_RE.finditer(text)])


# IG_RE's label-and-a-space form («Instagram namuna_shop») is wide on purpose —
# the pseudonymiser masks with it — so it also reads «Instagram sahifamiz» or
# «Instagram 2024». A handle is an exact identity key (04 §2.1): a word read as
# one would join every firm that writes it. After a bare space, only a
# handle-shaped token (a letter and a dot, underscore or digit) is a handle.
_IG_BARE_LABEL = re.compile(r"(?:инстаграм|instagram|insta|ig)\s+", re.I)
_HANDLE_MARK = re.compile(r"[._0-9]")
_HANDLE_LETTER = re.compile(r"[A-Za-z]")


def instagram_handles(text: str) -> list[str]:
    """Every ``IG_RE`` hit in ``text`` that is a usable Instagram handle, in order."""
    keys: list[str | None] = []
    for match in IG_RE.finditer(text):
        handle = match.group(1)
        label = text[match.start() : match.start(1)]
        if _IG_BARE_LABEL.fullmatch(label) and not (
            _HANDLE_MARK.search(handle) and _HANDLE_LETTER.search(handle)
        ):
            continue
        keys.append(instagram_key(handle))
    return _unique(keys)


# --- Website (04 §1.3) --------------------------------------------------------

LINK_HUB_HOSTS: Final = frozenset(
    {
        "taplink.cc",
        "taplink.ws",
        "linktr.ee",
        "lnk.bio",
        "instabio.cc",
        "bio.site",
        "clck.ru",
        "bit.ly",
        "cutt.ly",
        "wa.me",
        "api.whatsapp.com",
    }
)
# Hosts whose pages belong to many companies: a card, a profile, a map pin or a
# shop on one of them is never a company's own website. 04 §1.3 names only the
# social hosts and the link hubs; the rule is the same for these — two firms
# whose «website» is a 2GIS card or a sites.google.com page must not share a
# website_key (an exact identity key, 04 §2.1), and ux_companies_domain would
# refuse the second. The directory and news hosts are 03 §2.1's.
SHARED_HOSTS: Final = frozenset(
    {
        # maps and search
        "2gis.uz",
        "2gis.ru",
        "2gis.kz",
        "2gis.com",
        "yandex.uz",
        "yandex.ru",
        "yandex.com",
        "yandex.kz",
        "ya.ru",
        "google.com",
        "google.uz",
        "google.ru",
        "goo.gl",
        "g.page",
        "g.co",
        # directories (03 §2.1 DIRECTORY_HOSTS)
        "goldenpages.uz",
        "yellowpages.uz",
        "top.uz",
        "glotr.uz",
        "prom.uz",
        "flagma.uz",
        "orginfo.uz",
        "uztender.com",
        "hh.uz",
        "hh.ru",
        "enfsolar.com",
        "all.biz",
        "optlist.ru",
        # news (03 §2.1 NEWS_HOSTS) and reference
        "spot.uz",
        "kun.uz",
        "gazeta.uz",
        "daryo.uz",
        "uznews.uz",
        "podrobno.uz",
        "review.uz",
        "afisha.uz",
        "wikipedia.org",
        # social, video, messengers
        "tiktok.com",
        "vk.com",
        "ok.ru",
        "x.com",
        "twitter.com",
        "linkedin.com",
        "pinterest.com",
        "threads.net",
        "youtu.be",
        "fb.com",
        "fb.me",
        "instagr.am",
        "telegra.ph",
        "whatsapp.com",
        "wa.link",
        # short links
        "tinyurl.com",
        "vk.cc",
        "t.ly",
        "is.gd",
        # marketplaces and the Chinese suppliers' platforms
        "wildberries.ru",
        "wildberries.uz",
        "ozon.ru",
        "alibaba.com",
        "1688.com",
        "taobao.com",
        "aliexpress.com",
        "aliexpress.ru",
        "made-in-china.com",
    }
)
_NOT_A_SITE = NOT_WEBSITE_HOSTS | LINK_HUB_HOSTS | SHARED_HOSTS


def website_key(raw: str | None) -> str | None:
    """``domain_key`` for a company website, or None when the host is a social or
    marketplace host, a link hub (the enrichment step E2 fetches those once and
    parses them), a shared host (``SHARED_HOSTS``), a subdomain of any of them
    (``m.facebook.com``, ``sites.google.com``), or a site builder's own host
    (``blogspot.com`` — its tenants ``namuna.blogspot.com`` are websites)."""
    domain = domain_key(raw)
    if domain is None:
        return None
    if any(domain == host or domain.endswith("." + host) for host in _NOT_A_SITE):
        return None
    if domain in TENANT_HOSTS:
        return None
    return domain


# --- STIR (04 §1.5) -----------------------------------------------------------


def find_stir(text: str) -> str | None:
    """The first labelled STIR (``STIR_RE``: STIR/СТИР/ИНН/INN/TIN) that passes
    ``stir_key``. An unlabelled 9-digit run is not a STIR — it is usually a
    phone tail."""
    for match in STIR_RE.finditer(text):
        stir = stir_key(match.group(1))
        if stir is not None:
            return stir
    return None


# --- Legal forms (04 §1.6) -----------------------------------------------------

LegalForm = Literal[
    "MChJ", "OOO", "LLC", "QK", "AJ", "XK", "YaTT", "IP", "ChP", "SP", "TOO", "other"
]

# Keyed by the transliterated, apostrophe-free, space-collapsed spelling, so one
# entry serves both scripts: «ООО» and «OOO» are both "ooo". The enum values,
# their Cyrillic spellings and their full names; 04 §1.6 adds AO/АО → AJ;
# everything else is "other".
_LEGAL_FORM_BY_KEY: Final[dict[str, LegalForm]] = {
    "mchj": "MChJ",
    "masuliyati cheklangan jamiyat": "MChJ",
    "masuliyati cheklangan jamiyati": "MChJ",
    "ooo": "OOO",
    "obshchestvo s ogranichennoy otvetstvennostyu": "OOO",
    "obshestvo s ogranichennoy otvetstvennostyu": "OOO",
    "llc": "LLC",
    "qk": "QK",
    "qoshma korxona": "QK",
    "qoshma korxonasi": "QK",
    "qoshma": "QK",
    "aj": "AJ",
    "ao": "AJ",
    "aksiyadorlik jamiyati": "AJ",
    "aktsiyadorlik jamiyati": "AJ",
    "aktsionernoe obshchestvo": "AJ",
    "aktsionernoe obshestvo": "AJ",
    "xk": "XK",
    "xususiy korxona": "XK",
    "xususiy korxonasi": "XK",
    "yatt": "YaTT",
    "yakka tartibdagi tadbirkor": "YaTT",
    "ip": "IP",
    "individualnyy predprinimatel": "IP",
    "individualniy predprinimatel": "IP",
    "chp": "ChP",
    "chastnoe predpriyatie": "ChP",
    "sp": "SP",
    "too": "TOO",
}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _form_key(raw: str) -> str:
    text = to_latin(fold_apostrophes(unicodedata.normalize("NFKC", raw))).replace("'", "")
    return " ".join(_NON_ALNUM.sub(" ", text).split())


def legal_form_code(raw: str | None) -> LegalForm | None:
    """A legal form as written (``ООО``, ``ХК``, ``ЧП``, ``ИП``, ``ЯТТ``, ``ТОО``,
    ``АО``, ``MChJ``, ``Mas'uliyati cheklangan jamiyat`` …) → the 02 enum value;
    an unknown form → ``"other"``; None or blank → None."""
    if raw is None:
        return None
    key = _form_key(raw)
    if not key:
        return None
    return _LEGAL_FORM_BY_KEY.get(key, "other")


def find_legal_form(text: str) -> LegalForm | None:
    """The first ``LEGAL_FORM_RE`` hit in free text, as the enum value."""
    match = LEGAL_FORM_RE.search(text)
    return legal_form_code(match.group(1)) if match else None


def legal_form_in_name(name: str) -> LegalForm | None:
    """The legal form inside a company name, short or spelled out, either script
    («Mas'uliyati cheklangan jamiyati „Namuna"», «Намуна МЧЖ»). Names only: on
    free text the short forms (ok, ip, sp) are ordinary words."""
    match = LEGAL_FORM_TOKENS_RE.search(_form_key(name))
    return legal_form_code(match.group(0)) if match else None


# --- City (04 §1.6) -------------------------------------------------------------


# The Tashkent region is not the city: its landlines are +998 70, the city's
# +998 71, and a bare 7-digit number there is not a city landline (04 §1.1
# rule 6). It keeps a value of its own so neither the phone rule nor the
# resolver's city agreement (04 §2.1/§2.2) reads a region firm as a city one.
TASHKENT_REGION: Final = "Toshkent viloyati"
_TASHKENT_REGION_RE = re.compile(
    r"(?<!\w)(?:(?:toshkent|тошкент|ташкент|tashkent)(?:\s+|\s*-\s*)"
    r"(?:viloyat\w*|вилоят\w*|област\w*|обл\.?|oblast\w*|region\w*)"
    r"|ташкентск(?:ая|ой|ую)\s+(?:област\w*|обл\.?))(?!\w)",
    re.I,
)


def canonical_city(text: str | None) -> str | None:
    """The canonical Latin city of ``text`` (an address or a city field): the
    first city named — «г. Ташкент» → «Toshkent», «Фергана» → «Fargʻona»,
    «Toshkent viloyati, Chirchiq» → «Toshkent viloyati» — else «Toshkent» when
    a Tashkent bazaar or district is named («Chorsu bozori»). A city beats a
    bazaar wherever it stands: «Chorsu bozori Samarqand» is Samarqand."""
    if not text:
        return None
    best: tuple[int, str] | None = None
    for city, pattern in CITY_RES.items():
        match = pattern.search(text)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), city)
    region = _TASHKENT_REGION_RE.search(text)
    if region and (best is None or region.start() <= best[0]):
        return TASHKENT_REGION
    if best:
        return best[1]
    return "Toshkent" if TASHKENT_PLACES_RE.search(text) else None


# --- Person-shaped names (04 §1.6, §2.2) -----------------------------------------

GENERIC_TOKENS: Final = frozenset(
    {
        "optom",
        "ulgurji",
        "savdo",
        "trade",
        "trading",
        "group",
        "company",
        "kompaniya",
        "firma",
        "textile",
        "tekstil",
        "mebel",
        "stroy",
        "qurilish",
        "import",
        "export",
        "uz",
        "uzbekistan",
        "toshkent",
        "tashkent",
        "plus",
        "pro",
        "star",
        "gold",
        "best",
        "lux",
        "market",
        "shop",
        "dokon",
        "sklad",
        "baza",
        "biznes",
        "business",
    }
)
_INITIALS = re.compile(r"[a-z]+( [a-z]\.?){1,2}")
_SURNAME_SUFFIXES = ("ovich", "evich", "ovna", "evna", "ova", "eva", "ov", "ev")
# The stem before a surname suffix: «karim-ov», «ali-yev»; shorter ones are
# ordinary words («n-ova», «ki-ev», «l-ev»).
_SURNAME_STEM_MIN = 3
_PERSON_MAX_TOKENS = 3
# 04 §1.6's honorific clause, «aka|opa|ака|опа», in name_key form. Not MIYA's
# whole HONORIFICS list: «Ustoz Mebel», «Bro Shop», «Bobo Baraka» are shop
# names, and a person-shaped name loses its place as the company name.
PERSON_HONORIFICS: Final = frozenset({"aka", "opa"})
_LOOSE_FIRST_NAMES: Final = frozenset(loose_key(n) for n in FIRST_NAMES)


def _surname_shaped(token: str) -> bool:
    return any(
        token.endswith(s) and len(token) - len(s) >= _SURNAME_STEM_MIN
        for s in _SURNAME_SUFFIXES
    )


def _first_name(token: str) -> bool:
    return token in FIRST_NAMES or loose_key(token) in _LOOSE_FIRST_NAMES


def is_person_shaped(key: str) -> bool:
    """True when a ``name_key`` reads as a person rather than a firm (≤ 3 tokens):
    initials («karimov r r»), a surname or patronymic suffix («aliyev»,
    «rustamovna»), an honorific («aziz aka»), or a 1–2-token key that starts with
    a given name and carries no generic business word («sardor», «akmal
    karimov» — but not «sardor savdo»)."""
    tokens = key.split()
    if not tokens or len(tokens) > _PERSON_MAX_TOKENS:
        return False
    if _INITIALS.fullmatch(key):
        return True
    if any(_surname_shaped(t) or t in PERSON_HONORIFICS for t in tokens):
        return True
    return (
        len(tokens) <= 2
        and _first_name(tokens[0])
        and not any(t in GENERIC_TOKENS for t in tokens)
    )


# --- Observation (03) → observations.parsed (02 §2.5, 04 §1.6) ---------------------

PARSED_VERSION: Final = 1
ParsedKind = Literal[
    "intent_post",
    "profile",
    "listing",
    "directory_entry",
    "vacancy",
    "exhibitor",
    "place",
    "dealer_page",
    "registry",
    "seller",
]
# Collector kinds that are not observations (02 §2.5 mapping): a SERP hit only
# enqueues a URL and is kept as evidence; ERP rows go straight to their tables.
NOT_OBSERVATIONS: Final = frozenset({"serp_hit", "suppression", "outcome"})
_DIRECT_KINDS: Final[dict[str, ParsedKind]] = {
    "listing": "listing",
    "vacancy": "vacancy",
    "exhibitor": "exhibitor",
    "place": "place",
}
# What a `company` observation is, by the source that saw it (first prefix wins).
_COMPANY_KINDS: Final[tuple[tuple[str, ParsedKind], ...]] = (
    ("olx_business", "seller"),
    ("uzum_sellers", "seller"),
    ("brand_dealers", "dealer_page"),
    ("registry_lookup", "registry"),
    ("tenders", "registry"),
    ("directories", "directory_entry"),
    ("maps", "directory_entry"),
    ("exhibitors", "directory_entry"),
    ("search_", "directory_entry"),
)
# 04 §1.6 keeps a person for these kinds. `seller` is added: the same table
# moves a person-shaped OLX `user.name` into `person` (03 §2.5) — the seller
# profile is the one place that name can live.
PERSON_KINDS: Final = frozenset(
    {"intent_post", "profile", "vacancy", "registry", "seller"}
)
_COMPANY_PHONE_KINDS: Final = frozenset(
    {"directory_entry", "profile", "place", "dealer_page", "registry"}
)
_EXTERNAL_ID_RENAMES: Final = {
    "place_id": "google_place_id",
    "twogis_firm_id": "gis2_firm_id",
    "uzum_shop_slug": "uzum_shop",
    "tg_chat_id": "tg_chat",
}
_HANDLE_KEYS: Final = (
    "telegram",
    "tg_user_id",
    "instagram",
    "olx_user_id",
    "hh_employer_id",
    "gis2_firm_id",
    "google_place_id",
    "uzum_shop",
)
# A person-shaped seller name is replaced by «<label> <external id>» (03 §2.5).
_PERSON_NAME_LABELS: Final = {
    "olx_business": ("OLX sotuvchi", "olx_user_id"),
    "uzum_sellers": ("Uzum sotuvchi", "uzum_shop_slug"),
}
_HINT_SIGNALS: Final = {
    "container_words": frozenset({"container_truck"}),
    "wholesale_words": frozenset({"wholesale_bulky", "directory_wholesale"}),
    "b2c_words": frozenset({"b2c_marker"}),
    "competitor_words": frozenset({"competitor"}),
    "china_words": frozenset({"china_origin"}),
    "state_words": frozenset({"state_body"}),
}
_VOLUME_BASES: Final = frozenset({"stated", "container_words", "weight", "proxy", "none"})
# A PINFL is 14 digits; a field carrying one is dropped (02 §2.5 validators).
# Exactly 14: a 2GIS firm id or a Graph user id is a 17-digit run, and holds
# no PINFL (dropping it would lose the id the resolver matches on).
_PINFL = re.compile(r"(?<!\d)\d{14}(?!\d)")
_EXCERPT_MAX = 600


def parsed_kind(
    kind: str, source: str, attrs: dict[str, Any] | None = None
) -> ParsedKind:
    """03 ``Observation.kind`` → 02 ``parsed.kind`` (the mapping table of 02 §2.5)."""
    if kind in NOT_OBSERVATIONS:
        raise ValueError(f"{kind!r} is not an observation")
    if kind == "post":
        # A channel's own post speaks for the channel; a group post is a person's.
        return "profile" if (attrs or {}).get("chat_type") == "channel" else "intent_post"
    if kind in _DIRECT_KINDS:
        return _DIRECT_KINDS[kind]
    if kind != "company":
        raise ValueError(f"unknown observation kind {kind!r}")
    for prefix, mapped in _COMPANY_KINDS:
        if source.startswith(prefix):
            return mapped
    return "profile"


class _Pinfl:
    """Drops every value carrying a 14-digit run and remembers that it did."""

    def __init__(self) -> None:
        self.dropped = False

    def __call__(self, value: str | None) -> str | None:
        if value is None:
            return None
        if _PINFL.search(value):
            self.dropped = True
            return None
        return value

    def number(self, value: int | float | Decimal) -> int | float | Decimal | None:
        # A PINFL typed as a number (``director_pinfl: 31234567890123``).
        digits = f"{value:f}" if isinstance(value, Decimal | float) else str(value)
        return None if self(digits) is None else value


def _jsonable(value: Any, clean: _Pinfl) -> Any:
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return clean.number(value)
    if isinstance(value, Decimal):
        kept = clean.number(value)
        return None if kept is None else float(kept)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v, clean) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = sorted(value, key=str) if isinstance(value, set | frozenset) else value
        return [_jsonable(v, clean) for v in items]
    return clean(str(value))


def _phones(raws: list[str], *, city: str | None, owner: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raws:
        phone = make_phone(raw, city=city)
        if phone is None or phone.tail in seen:
            continue
        seen.add(phone.tail)
        out.append({"raw": raw, "e164": phone.e164, "kind": phone.kind, "owner": owner})
    return out


def _volume(attrs: dict[str, Any]) -> tuple[float | None, str]:
    raw = attrs.get("volume_m3")
    try:
        volume = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        volume = None
    if volume is not None and not math.isfinite(volume):
        volume = None  # «nan», «inf»: no volume was stated
    basis = attrs.get("basis") or attrs.get("volume_basis")
    if basis not in _VOLUME_BASES:
        basis = "stated" if volume is not None else "none"
    return volume, str(basis)


def _storable_tree(value: Any) -> Any:
    """The parsed tree as ``jsonb`` accepts it: every string (keys too)
    ``text.storable`` (no NUL, no lone surrogate), every non-finite float
    None — NaN and Infinity are not JSON, and one such value in a page's rows
    would cost the whole page its observations."""
    if isinstance(value, str):
        return storable(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {storable(str(k)): _storable_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_storable_tree(v) for v in value]
    return value


def to_observation_json(
    obs: Observation, *, collector_version: str | None = None
) -> dict[str, Any]:
    """The only writer of ``observations.parsed`` (04 §1.6): an ``Observation``
    as a collector emitted it → the 02 §2.5 shape, keys recomputed here (name
    key, E.164 phones and their kind, handles, legal form, city), with the
    02 §2.5 validators applied: excerpt ≤ 600, a field with a 14-digit run
    dropped (``hints.pinfl_dropped``), a person only for the kinds that may
    carry one, and a person-shaped company name moved to ``person``; and the
    whole tree storable (``_storable_tree``: no NUL, no NaN).
    ``collector_version`` is ``<collector key>@<Collector.version>``."""
    kind = parsed_kind(obs.kind, obs.source, obs.attrs)
    clean = _Pinfl()
    attrs = _jsonable(dict(obs.attrs), clean)
    person_ok = kind in PERSON_KINDS

    name_raw = clean(obs.company_name_raw)
    person_name = clean(obs.person_name) if person_ok else None
    person_role = clean(obs.person_role) if person_ok else None
    key = name_key(name_raw) if name_raw else None
    legal_form = legal_form_code(obs.legal_form) or (
        legal_form_in_name(name_raw) if name_raw else None
    )
    if key is not None and name_raw is not None and is_person_shaped(key):
        # Person-shaped names never become a company name (04 §1.6). The label
        # carries no name_key on purpose: two labels would fuzzy-match each
        # other and merge two sellers; the external id resolves them exactly.
        if person_name is None:
            person_name = name_raw
        label = _PERSON_NAME_LABELS.get(obs.source)
        external = obs.external_ids.get(label[1]) if label else None
        name_raw = f"{label[0]} {external}" if label and external else None
        key = None
        legal_form = legal_form_code(obs.legal_form)

    address = clean(obs.address)
    city = canonical_city(obs.city) or canonical_city(address)
    if city is None and isinstance(attrs.get("city"), str):
        city = canonical_city(attrs["city"])

    handles: dict[str, Any] = dict.fromkeys(_HANDLE_KEYS)
    for raw_key, value in obs.external_ids.items():
        handles[_EXTERNAL_ID_RENAMES.get(raw_key, raw_key)] = clean(str(value))
    handles["telegram"] = telegram_key(obs.telegram) if obs.telegram else None
    # ``person_telegram`` is a group poster's username (03 §1.2): only on an
    # intent post is ``handles.telegram`` the poster's (02 §2.5 «company
    # channel OR poster username», scrubbed by R3). On every other kind the
    # resolver reads ``handles.telegram`` as the firm's own channel, so a
    # person's handle there would become a company-tier column — a vacancy's
    # HR person's above all (03 §2.7).
    if kind == "intent_post" and handles["telegram"] is None and obs.person_telegram:
        handles["telegram"] = telegram_key(obs.person_telegram)
    handles["tg_user_id"] = attrs.get("tg_user_id") if person_ok else None
    handles["instagram"] = instagram_key(obs.instagram) if obs.instagram else None

    if kind == "intent_post":
        owner = "person"
    elif kind in _COMPANY_PHONE_KINDS:
        owner = "company"
    else:
        owner = "unknown"
    phones = _phones(
        [p for p in obs.phones if clean(p) is not None], city=city, owner=owner
    )

    excerpt = clean(obs.excerpt) or ""
    if len(excerpt) > _EXCERPT_MAX:
        excerpt = excerpt[:_EXCERPT_MAX].rstrip()
    signals = set(obs.signal_types)
    volume, basis = _volume(attrs)
    hints: dict[str, Any] = {"volume_m3": volume, "volume_basis": basis}
    for hint, names in _HINT_SIGNALS.items():
        hints[hint] = bool(signals & names)
    hints["collector"] = {"signal_types": list(obs.signal_types), "attrs": attrs}
    hints["pinfl_dropped"] = False  # set below, after the last field is cleaned

    observed_at = obs.published_at or obs.fetched_at
    parsed: dict[str, Any] = {
        "v": PARSED_VERSION,
        "kind": kind,
        "source_key": obs.source,
        "source_ref": obs.source_ref,
        # A collector may cite the API URL it called: never with its key.
        "url": strip_secrets(obs.url),
        "observed_at": observed_at.isoformat(),
        "name_key": key,
        "company": {
            "name_raw": name_raw,
            "legal_form": legal_form,
            "stir": stir_key(obs.stir),
            "oked": None,
            "city": city,
            "address": address,
            "website": clean(obs.website),
            "goods_text": clean(obs.goods_text),
            "sector_hints": [],
        },
        "handles": handles,
        "phones": phones,
        "person": (
            {"name": person_name, "role": person_role}
            if person_ok and (person_name or person_role)
            else None
        ),
        "text": {
            "excerpt": excerpt,
            "volume_text": clean(obs.volume_text),
            "origin_text": clean(obs.origin_text),
            "intent_words": list(
                dict.fromkeys(m.group(0) for m in INTENT_RE.finditer(excerpt))
            ),
        },
        "hints": hints,
        "lang": obs.lang,
        "legal_basis": "public_source",
        "collector_version": collector_version or f"{obs.source}@1",
    }
    hints["pinfl_dropped"] = clean.dropped
    tree: dict[str, Any] = _storable_tree(parsed)
    return tree
