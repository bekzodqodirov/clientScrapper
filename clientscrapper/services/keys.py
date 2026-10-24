"""Normalised keys — one home (02 §1).

Python is the writer of every stored key. Three of them also exist in SQL
(``cs_phone_tail``, ``cs_username_key``, ``cs_domain_key`` in 0001_initial),
because a CHECK or a generated column needs them, and ``tests/test_keys.py``
runs both over one input table and demands equality. So the mirrored
functions copy the SQL operation by operation rather than reaching for
``urlsplit`` or ``str.strip()``: ``trim()`` removes spaces only, ``.`` crosses
a newline, ``$`` is the end of the string, and ``lower()`` turns İ into a
plain i. Two designed differences, both in ``domain_key`` and both so that the
stored value is a fixed point of the CHECK ``website_domain =
cs_domain_key(website_domain)``: a non-ASCII host is IDNA-encoded where the SQL
answers NULL (02 §1), and every leading ``www.`` is stripped where the SQL strips
one — ``cs_domain_key('www.www.x.uz')`` is ``www.x.uz``, whose own key is
``x.uz``, so a byte-exact mirror would write a row its CHECK refuses.

Everything here is pure: no I/O, no clock, no settings.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from typing import Final, Literal
from urllib.parse import urlsplit

from clientscrapper.services.text import fold_apostrophes, to_latin

PhoneKind = Literal["mobile", "landline", "short", "unknown"]

_NOT_DIGIT = re.compile(r"[^0-9]")
_DIGITS = re.compile(r"[0-9]+")


def _pg_trim(text: str) -> str:
    # SQL trim(text) with no character list strips spaces, not tabs or newlines.
    return text.strip(" ")


def _pg_lower(text: str) -> str:
    # Postgres lowers by the simple case mapping (İ → i); Python's full mapping
    # gives "i̇" (two code points). No other character lowers to ASCII apart
    # from A–Z and the Kelvin sign, which both sides map to k.
    return text.replace("\u0130", "i").lower()


# --- Phones -----------------------------------------------------------------

UZ_COUNTRY_CODE = "998"

# 04 §1.1, by the two digits after +998. UNVERIFIED against the current
# numbering plan (04 Q4); 20/50/55 join MOBILE_CODES only after that check,
# so until then they read "unknown" and are not usable lead phones.
MOBILE_CODES = frozenset(
    {"33", "77", "88", "90", "91", "93", "94", "95", "97", "98", "99"}
)
MOBILE_CODES_UNCONFIRMED = frozenset({"20", "50", "55"})
LANDLINE_CODES = frozenset(
    {
        "61",
        "62",
        "65",
        "66",
        "67",
        "69",
        "70",
        "71",
        "72",
        "73",
        "74",
        "75",
        "76",
        "78",
        "79",
    }
)
# Placeholder numbers people type into forms and adverts.
_FAKE_NATIONAL = frozenset({"123456789", "987654321"})
_CITY_WORDS = frozenset({"g", "sh", "shahri", "shahar", "gorod", "city"})


def phone_tail(raw: str | None) -> str | None:
    """Digits only; >= 9 digits -> last 9, else None. Identical to the ERP's
    inboundPhoneDigits and to SQL cs_phone_tail (ASCII digits only)."""
    if raw is None:
        return None
    digits = _NOT_DIGIT.sub("", raw)
    return digits[-9:] if len(digits) >= 9 else None


def _is_tashkent(city: str | None) -> bool:
    if not city:
        return False
    tokens = [t for t in name_key(city).split() if t not in _CITY_WORDS]
    return tokens in (["toshkent"], ["tashkent"])


def _valid_national(national: str) -> bool:
    """A 9-digit Uzbek national number that is not a placeholder."""
    if len(national) != 9 or national[0] == "0" or national in _FAKE_NATIONAL:
        return False
    # «+998 90 000 00 00 → None (all equal after prefix)» (04 §1.1): the seven
    # subscriber digits all equal; this also covers (\d)\1{8}.
    return len(set(national[2:])) > 1


def phone_e164(raw: str | None, *, city: str | None = None) -> str | None:
    """'+998XXXXXXXXX' for an Uzbek number, '+<digits>' for a foreign one, else None.

    04 §1.1, in order: a '+' counts only when leading and '00' stands for it;
    +998 + 9 digits; bare 998 + 9; legacy trunk 8 or 0 + 9; exactly 9 digits;
    7 digits only when ``city`` is Toshkent (a city landline without its 71);
    '+' and another country code with 7–15 digits is kept as E.164. A UZ
    number must have 9 national digits not starting with 0 and must not be a
    placeholder (subscriber digits all equal, 123456789, 987654321).
    """
    if raw is None:
        return None
    text = raw.strip()
    plus = text.startswith("+")
    digits = _NOT_DIGIT.sub("", text)
    if not plus and digits.startswith("00"):
        plus, digits = True, digits[2:]
    if plus:
        if digits.startswith(UZ_COUNTRY_CODE):
            national = digits[3:] if len(digits) == 12 else ""
        elif 7 <= len(digits) <= 15 and digits[0] != "0":
            return "+" + digits
        else:
            return None
    elif len(digits) == 12 and digits.startswith(UZ_COUNTRY_CODE):
        national = digits[3:]
    elif len(digits) == 10 and digits[0] in "80":
        national = digits[1:]
    elif len(digits) == 9:
        national = digits
    elif len(digits) == 7 and _is_tashkent(city):
        national = "71" + digits
    else:
        return None
    return "+998" + national if _valid_national(national) else None


def phone_kind(e164: str) -> PhoneKind:
    """+998 mobile / landline by the two digits after the country code;
    < 7 national digits -> short; a foreign number or an unlisted code -> unknown."""
    if not e164.startswith("+" + UZ_COUNTRY_CODE):
        return "unknown"
    national = e164[4:]
    if national and not _DIGITS.fullmatch(national):
        return "unknown"
    if len(national) < 7:
        return "short"
    if len(national) != 9:
        return "unknown"
    code = national[:2]
    if code in MOBILE_CODES:
        return "mobile"
    if code in LANDLINE_CODES:
        return "landline"
    return "unknown"


# --- Telegram usernames (mirror of cs_username_key) --------------------------

# The scheme, www. and the t.me/telegram.me host in either case, spelled as
# ASCII classes exactly as cs_username_key spells them (re.I would also fold
# the Kelvin sign and the long s, which the SQL does not).
_TG_PREFIX = re.compile(
    r"\A([A-Za-z]+://)?([Ww][Ww][Ww]\.)?"
    r"([Tt]\.[Mm][Ee]/|[Tt][Ee][Ll][Ee][Gg][Rr][Aa][Mm]\.[Mm][Ee]/)?@?"
)
_PATH_TAIL = re.compile(r"[/?#].*\Z", re.DOTALL)
_USERNAME = re.compile(r"[a-z0-9_]{5,32}")


def username_key(raw: str | None) -> str | None:
    """Strip https://, www., t.me/, telegram.me/, '@', anything from '/', '?', '#';
    lowercase; must match ^[a-z0-9_]{5,32}$ and not end with 'bot'.

    The prefix is matched in either case (ASCII only), exactly as the SQL
    does: «HTTPS://T.ME/x» is a link to x. Reserved
    t.me paths (joinchat, s, share, …) are the normaliser's business
    (``normalise.telegram_key``), because the SQL mirror does not know them.
    """
    if raw is None:
        return None
    rest = _TG_PREFIX.sub("", _pg_trim(raw), count=1)
    key = _pg_lower(_PATH_TAIL.sub("", rest, count=1))
    if _USERNAME.fullmatch(key) and not key.endswith("bot"):
        return key
    return None


# --- Instagram handles (no SQL mirror; the CHECK is instagram = lower(instagram))

IG_RESERVED = frozenset(
    {
        "p",
        "reel",
        "reels",
        "explore",
        "stories",
        "tv",
        "accounts",
        "tags",
        "direct",
        "about",
        "developer",
        "legal",
    }
)
_IG_PREFIX = re.compile(r"\A(?:[A-Za-z]+://)?(?:(?:www|m)\.)?instagram\.com/", re.I)
_IG_KEY = re.compile(r"[a-z0-9._]{1,30}")


def instagram_key(raw: str | None) -> str | None:
    """Strip instagram.com/, '@', trailing '/', query; lowercase; strip trailing
    dots; ^[a-z0-9._]{1,30}$; never a reserved path (p, reel, explore, …)."""
    if raw is None:
        return None
    rest = _IG_PREFIX.sub("", raw.strip(), count=1).removeprefix("@")
    handle = re.split(r"[/?#]", rest, maxsplit=1)[0].lower().rstrip(".")
    if _IG_KEY.fullmatch(handle) and handle not in IG_RESERVED:
        return handle
    return None


# --- Website domains (mirror of cs_domain_key, plus IDNA) --------------------

# Social and marketplace hosts are handles, not websites (02 §1). The list is
# the SQL function's, byte for byte: changing it means a new migration.
NOT_WEBSITE_HOSTS = frozenset(
    {
        "instagram.com",
        "t.me",
        "telegram.me",
        "facebook.com",
        "olx.uz",
        "uzum.uz",
        "youtube.com",
        "wa.me",
    }
)
_SCHEME = re.compile(r"\A[A-Za-z]+://")
_HOST_TAIL = re.compile(r"[/?#:].*\Z", re.DOTALL)
_WWW = re.compile(r"\A(?:www\.)+")
_DOMAIN = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")


def domain_key(raw: str | None) -> str | None:
    """The host of a URL (or of a bare host), lowercased, IDNA-encoded, without
    leading 'www.'s; None for the social/marketplace hosts of NOT_WEBSITE_HOSTS
    and for anything that is not a dotted [a-z0-9-] name."""
    if raw is None:
        return None
    rest = _SCHEME.sub("", _pg_trim(raw), count=1)
    host = _pg_lower(_HOST_TAIL.sub("", rest, count=1))
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    host = _WWW.sub("", host, count=1)
    if _DOMAIN.fullmatch(host) and host not in NOT_WEBSITE_HOSTS:
        return host
    return None


# Registrable-domain folding for matching only (04 §1.3); stored columns keep
# domain_key. A suffix outside this list folds nothing: guessing «the last two
# labels» would merge every company under a two-label public suffix.
SITE_SUFFIXES = frozenset(
    {
        "uz",
        "co.uz",
        "com.uz",
        "org.uz",
        "net.uz",
        "biz.uz",
        "com",
        "net",
        "org",
        "ru",
        "kz",
        "shop",
        "store",
    }
)


# Site builders and free hosts that give every customer a subdomain: on them
# the tenant label is the company, so they fold like a public suffix —
# namuna.blogspot.com and boshqa.blogspot.com are two sites, never one
# «blogspot.com» (04 §1.3 names no such list; without one, site_key turned
# every free-hosted firm into the same exact-key identity, 04 §2.1).
# services/normalise.website_key refuses the bare platform host itself.
TENANT_HOSTS = frozenset(
    {
        "blogspot.com",
        "wixsite.com",
        "wordpress.com",
        "tilda.ws",
        "business.site",
        "weebly.com",
        "webflow.io",
        "github.io",
        "netlify.app",
        "myshopify.com",
        "jimdosite.com",
        "nethouse.ru",
        "ukit.me",
        "narod.ru",
        # uCoz's free domains
        "ucoz.ru",
        "ucoz.net",
        "ucoz.com",
        "ucoz.org",
        "at.ua",
        "3dn.ru",
        "my1.ru",
        "moy.su",
    }
)


def site_key(domain: str) -> str:
    """shop.example.uz and example.uz -> example.uz; example.co.uz stays itself;
    shop.namuna.blogspot.com -> namuna.blogspot.com (the tenant, not the host)."""
    labels = domain.split(".")
    for i in range(1, len(labels)):
        suffix = ".".join(labels[i:])
        if suffix in SITE_SUFFIXES or suffix in TENANT_HOSTS:
            return ".".join(labels[i - 1 :])
    return domain


# --- STIR ---------------------------------------------------------------------


def stir_key(raw: str | None) -> str | None:
    """9 digits, first digit 2–3 (legal entity) or 4–6 (individual entrepreneur),
    not all equal, not 123456789. Shape only: whether a STIR carries a check
    digit is UNVERIFIED (04 §1.5), so v1 validates nothing more."""
    if raw is None:
        return None
    digits = _NOT_DIGIT.sub("", raw)
    if len(digits) != 9 or digits[0] not in "23456":
        return None
    if len(set(digits)) == 1 or digits == "123456789":
        return None
    return digits


# --- Company name key (04 §1.4) -----------------------------------------------

# Matched as whole tokens AFTER transliteration to lower-case Latin, longest
# first. The spec's list (04 §1.4) plus two kinds of spelling it implies: the
# ones ``to_latin`` actually makes of the Cyrillic forms listed (щ → sh,
# ы → i, ц → ts — else «Общество с ограниченной ответственностью» keeps its
# form in the key), and the Uzbek possessive the full forms take after a name
# («Namuna mas'uliyati cheklangan jamiyati», «… xususiy korxonasi»).
LEGAL_FORMS = (
    "masuliyati cheklangan jamiyat",
    "masuliyati cheklangan jamiyati",
    "obshchestvo s ogranichennoy otvetstvennostyu",
    "obshestvo s ogranichennoy otvetstvennostyu",
    "xususiy korxona",
    "xususiy korxonasi",
    "qoshma korxona",
    "qoshma korxonasi",
    "oilaviy korxona",
    "oilaviy korxonasi",
    "chastnoe predpriyatie",
    "individualnyy predprinimatel",
    "individualniy predprinimatel",
    "yakka tartibdagi tadbirkor",
    "aksiyadorlik jamiyati",
    "aktsiyadorlik jamiyati",
    "aktsionernoe obshchestvo",
    "aktsionernoe obshestvo",
    "fermer xojaligi",
    "mchj",
    "ooo",
    "llc",
    "ltd",
    "qk",
    "xk",
    "aj",
    "ao",
    "oao",
    "zao",
    "chp",
    "xchk",
    "ip",
    "yatt",
    "sp",
    "too",
    "jv",
    "ok",
    "fx",
    "uk",
    "dp",
    "gup",
)
QUOTES = "«»“”\"'`„‟‹›"
NAME_KEY_MAX = 160

LEGAL_FORM_TOKENS_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(re.escape(f) for f in sorted(set(LEGAL_FORMS), key=len, reverse=True))
    + r")(?![a-z0-9])"
)
_QUOTE_RE = re.compile(f"[{re.escape(QUOTES)}]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Latin letters that do not decompose into a base letter and a mark.
_LATIN_FOLDS = str.maketrans(
    {"ı": "i", "ł": "l", "ø": "o", "đ": "d", "ð": "d", "ß": "ss", "æ": "ae", "œ": "oe"}
)


def _cut(text: str) -> str:
    return " ".join(text.split())[:NAME_KEY_MAX].rstrip()


def _strip_marks(text: str) -> str:
    # After to_latin, so the Cyrillic й, ё and ў (letters with a mark under
    # NFD) are already Latin; what is left are Latin letters with diacritics.
    decomposed = unicodedata.normalize("NFKD", text.translate(_LATIN_FOLDS))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def name_key(raw: str) -> str:
    """fold_apostrophes -> transliterate (Cyrillic -> Latin) -> lower -> drop
    diacritics -> drop apostrophes and quotes -> strip legal forms -> collapse
    spaces.

    ``ООО «Намуна Текстиль Савдо»`` and ``"NAMUNA TEKSTIL SAVDO" MChJ`` both give
    ``namuna tekstil savdo``. A name that is only a legal form keeps the form
    (``OOO`` -> ``ooo``), so the key is never empty for a non-empty name; such
    a key is never fuzzy-matched (04 §2.2). The diacritics step is not in 04
    §1.4's pseudo-code: without it «Öztürk Tekstil» keyed as «zt rk tekstil»
    (the marked letters fell out with the punctuation) and «İstanbul» as
    «i stanbul» (Python lowers İ to i + a combining dot), so the Turkish and
    old-alphabet spellings of a name never met their plain ones.
    """
    text = to_latin(fold_apostrophes(unicodedata.normalize("NFKC", raw)))
    text = _strip_marks(text)
    text = _QUOTE_RE.sub(" ", text.replace("'", ""))
    text = _NON_ALNUM.sub(" ", text)
    key = _cut(LEGAL_FORM_TOKENS_RE.sub(" ", text))
    return key or _cut(text) or to_latin(fold_apostrophes(raw)).strip()[:NAME_KEY_MAX]


_LOOSE_LETTERS = str.maketrans({"q": "k", "w": "v", "x": "h"})
_DOUBLED_LETTER = re.compile(r"([a-z])\1+")


def loose_key(key: str) -> str:
    """Comparison form only, never stored: 'tsh' -> 'ch', q -> k, w -> v,
    x -> h, doubled letters collapsed. «Toshkent Tekstil» and «Tashkent
    Textile» still differ (o/a, e/i) — that is rapidfuzz's job."""
    folded = key.replace("tsh", "ch").translate(_LOOSE_LETTERS)
    return _DOUBLED_LETTER.sub(r"\1", folded)


# --- The ERP suppression feed's hash (05 §B.2) ---------------------------------

_TAIL9 = re.compile(r"[0-9]{9}")


def erp_phone_hash(secret: str, tail9: str) -> str:
    """hmac_sha256(secret, tail9).hexdigest()[:20] — the suppression feed rule.
    Refuses anything but nine digits: a hash of a raw phone matches nothing
    and would fail open without a sound."""
    if not _TAIL9.fullmatch(tail9):
        raise ValueError("erp_phone_hash needs the 9-digit phone tail")
    digest = hmac.new(secret.encode(), tail9.encode(), hashlib.sha256)
    return digest.hexdigest()[:20]


# The host .env.example (and the settings default) ship for CRM_BASE_URL:
# example.uz is not a reserved name, so whoever owns it would receive the key.
ERP_PLACEHOLDER_HOST: Final = "erp.example.uz"
ErpBaseRefusal = Literal["not_https", "placeholder"]


def is_erp_placeholder(base_url: str) -> bool:
    """The .env.example host, still in place."""
    host = (urlsplit(base_url.strip()).hostname or "").rstrip(".")
    return host == ERP_PLACEHOLDER_HOST


def erp_base_refusal(base_url: str) -> ErpBaseRefusal | None:
    """Why the agent key may not be sent to ``base_url`` (05 §B.1, 06 §3.3):
    not ``https://`` (the key would cross the wire in clear, and the ERP's
    308 is never followed), or the placeholder host. One rule for
    ``make doctor``'s probe and every ERP client."""
    if not base_url.strip().lower().startswith("https://"):
        return "not_https"
    if is_erp_placeholder(base_url):
        return "placeholder"
    return None


def erp_salt_id(secret: str) -> str:
    """sha256(secret).hexdigest()[:8] — must equal the feed's salt_id or a
    full pull is forced."""
    return hashlib.sha256(secret.encode()).hexdigest()[:8]
