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

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

from clientscrapper.collectors.keywords import (
    CITY_RES,
    IG_RE,
    LEGAL_FORM_RE,
    PHONE_RE,
    STIR_RE,
    TG_RE,
)
from clientscrapper.services.keys import (
    LEGAL_FORM_TOKENS_RE,
    NOT_WEBSITE_HOSTS,
    PhoneKind,
    domain_key,
    instagram_key,
    loose_key,
    phone_e164,
    phone_kind,
    phone_tail,
    stir_key,
    username_key,
)
from clientscrapper.services.names import FIRST_NAMES, HONORIFICS
from clientscrapper.services.text import fold_apostrophes, to_latin

__all__ = [
    "GENERIC_TOKENS",
    "LINK_HUB_HOSTS",
    "TG_RESERVED",
    "LegalForm",
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
    "phone_e164",
    "phone_kind",
    "phone_tail",
    "telegram_handles",
    "telegram_key",
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


def instagram_handles(text: str) -> list[str]:
    """Every ``IG_RE`` hit in ``text`` that is a usable Instagram handle, in order."""
    return _unique([instagram_key(m.group(1)) for m in IG_RE.finditer(text)])


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
_NOT_A_SITE = NOT_WEBSITE_HOSTS | LINK_HUB_HOSTS


def website_key(raw: str | None) -> str | None:
    """``domain_key`` for a company website, or None when the host is a social or
    marketplace host, a link hub (the enrichment step E2 fetches those once and
    parses them), or a subdomain of either (``m.facebook.com``)."""
    domain = domain_key(raw)
    if domain is None:
        return None
    if any(domain == host or domain.endswith("." + host) for host in _NOT_A_SITE):
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


def canonical_city(text: str | None) -> str | None:
    """The canonical Latin city of the first ``GEO`` place in ``text`` (an address
    or a city field): «г. Ташкент» → «Toshkent», «Chorsu bozori» → «Toshkent»,
    «Фергана» → «Fargʻona»."""
    if not text:
        return None
    best: tuple[int, str] | None = None
    for city, pattern in CITY_RES.items():
        match = pattern.search(text)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), city)
    return best[1] if best else None


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
    if any(_surname_shaped(t) or t in HONORIFICS for t in tokens):
        return True
    return (
        len(tokens) <= 2
        and _first_name(tokens[0])
        and not any(t in GENERIC_TOKENS for t in tokens)
    )
