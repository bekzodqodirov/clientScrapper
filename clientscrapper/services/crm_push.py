"""The push: an approved lead handed to the ERP (05 §B.1, 02 §2.10).

The agent never contacts a prospect (CLAUDE.md): this module posts ONLY to
the ERP's lead door, ``POST /api/leads/in/<CRM_PUSH_SOURCE>``, where a human
sales manager picks the lead up. Nothing here sends a Telegram message, an
SMS, an e-mail or a call to anybody.

The body (``PushPayload``) is built by ``build_payload`` from a
``candidates.CandidateView``: the company's display name, the lead phone,
the Uzbek note (``render_note``) and one extra key per fact the company has.
Its field order is the order of the ERP's note lines (05 §A.2), and every
limit is the ERP's, counted in UTF-16 units (``push_gate.text_len``):
nothing is ever cut by the ERP, so no cut can halve an emoji on the old
build. ``landed_note`` mirrors what the ERP keeps — the note, then one
``key: value`` line per extra key, every whitespace run collapsed with
JavaScript's ``\\s`` (written out: Python's class differs) — and the builder
shortens the note in 05 §B.1's drop order until both the note (≤ 700) and
the landed note (≤ 1,000) fit.

``push_one`` is the one way in (07 §1.2's guard): it takes the candidate
``FOR UPDATE``, checks it is ``approved`` (a rule-5 re-push of its own
``external_id`` also reads ``pushed``), claims the ``push_log`` row through
``push_gate.claim`` and COMMITS before the HTTP call (02 §2.10 rule 3), then
posts through ``_post`` and records what the answer means in a second
transaction. Two levels, one scheme (05 §B.1): inside one attempt the POST
is retried by ``with_backoff`` only when a re-send cannot land the push twice
— the connection never opened, or a 5xx came back (a re-send is fenced by the
ERP's replay) — and never after a read timeout (the first request may still
be running); a further attempt is the next claim, after ``ATTEMPT_DELAYS``.

``crm_push_job`` (every minute inside the window) pushes the approved
candidates, passing over the ones the gate refuses for themselves;
``reconcile_job`` (every 10 minutes) settles a crashed claim
(``sending`` → ``unknown``), runs the ``unknown`` outcomes walk, and makes
02 §2.10 rule 5's one re-push of a push the outcomes feed never showed.
``push_tekshir_*`` is the owner's ``/push tekshir`` (06 §1.2): the view, and
the confirm that supersedes the delivered row and re-pushes it at once.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

import httpx
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors.keywords import PRICE_RE
from clientscrapper.collectors.runner import SessionFactory
from clientscrapper.config import settings
from clientscrapper.db import enums as e
from clientscrapper.db.models import Company, LeadCandidate, Outcome, PushLog
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import (
    candidates,
    companies,
    costs,
    erp_feeds,
    health,
    outcomes,
    push_gate,
)
from clientscrapper.services.candidates import CandidateView, SignalEvidence
from clientscrapper.services.erp_feeds import ErpClient, ErpHttpError
from clientscrapper.services.gates import REGISTRY_UZ
from clientscrapper.services.normalise import person_names
from clientscrapper.services.pseudonyms import (
    TOKEN_RE,
    TOKEN_TRACE_RE,
    pseudonymise,
    unmasked,
)
from clientscrapper.services.push_gate import Refusal, text_len
from clientscrapper.services.retry import with_backoff
from clientscrapper.services.text import well_formed

log = logging.getLogger(__name__)

# --- the contract (05 §A.2, §B.1) -----------------------------------------------------

# One value under both names; the ERP reads X-GSR-Key only (ErpClient sends both).
ERP_KEY_HEADERS: Final = erp_feeds.ERP_KEY_HEADERS
# source = settings.crm_push_source ('agent' | 'other'). Named only by
# endpoint_for (07 §1.2's guard).
PUSH_PATH: Final = "/api/leads/in/{source}"
UTM_SOURCE: Final = "clientscrapper"
# The four identity names the agent uses; fromPlain reads them as identity,
# never as note lines (05 §A.2).
IDENTITY_KEYS: Final = push_gate.PAYLOAD_IDENTITY
# fromPlain matches body keys lower-cased: no payload field's lower-cased name
# may be one of these — the shared secret's four names (read as the key and
# dropped from note, pairs and ref), fromPlain's other identity spellings, and
# the switch to the Google parser.
RESERVED_BODY_KEYS: Final = frozenset(
    {
        "key",
        "google_key",
        "secret",
        "token",
        "id",
        "ism",
        "telefon",
        "izoh",
        "user_column_data",
    }
)
# The ERP keeps note + one 'key: value' line per extra key, collapsed, cut at
# 1000 UTF-16 units; our own note's budget inside it is push_gate.NOTE_MAX.
LANDED_NOTE_MAX: Final = 1000
NOTE_MAX: Final = push_gate.NOTE_MAX
# The seller's Telegram push shows «<first 159 code points>…» of the landed
# note (clipText — the one ERP cut that counts code points).
PUSH_PREVIEW: Final = 159
QUOTE_MAX: Final = 140
OPENER_MAX: Final = 220
OPENER_SHORT: Final = 120
NAME_MIN, NAME_MAX = 2, 120
COMPANY_MAX: Final = 200
PAIR_KEY_MAX, PAIR_VALUE_MAX, PAIRS_MAX = 80, 300, 30
URL_MAX: Final = 200
ADDRESS_SHORT: Final = 120
EVIDENCE_QUOTE_MAX: Final = 100
GOODS_MAX: Final = 6
ORIGIN_HINT_MAX: Final = 50
ELLIPSIS: Final = "…"
# A band's words for the HAJM section — never sent as ``kub``: a mapped kub
# becomes the lead's quoted volume (05 §A.2). The ERP's textVolume routes by
# the number RIGHT BEFORE the first «kub», so each band puts its LOW END
# there: 1_3 → 1, 3_10 → 3, container → 28, truck → 80.
BAND_HAJM: Final[Mapping[str, str]] = {
    "1_3": "taxminan 1 kubdan 3 kubgacha",
    "3_10": "taxminan 3 kubdan 10 kubgacha",
    "container": "konteyner (taxminan 28 kub)",
    "truck": "fura (taxminan 80 kub)",
}
# JavaScript's \s (= String.prototype.trim()'s set), written out: Python's
# str.split()/strip() also split on U+001C–U+001F and U+0085, which JS \s
# does not, and do NOT split on U+FEFF, which JS does (05 §B.1).
JS_SPACE: Final = "[\t\n\v\f\r    -     　﻿]"
JS_SPACE_RUN: Final = re.compile(JS_SPACE + "+")
_JS_EDGES: Final = re.compile(f"^{JS_SPACE}+|{JS_SPACE}+$")
# The ERP keeps these as invisible control characters on the lenta: a space.
_CONTROLS: Final = str.maketrans(
    {"\x1c": " ", "\x1d": " ", "\x1e": " ", "\x1f": " ", "\x85": " "}
)
# field-map.ts textVolume (the first match of name + landed note), for the
# routing tests; parseMeasure turns a first 0 or a number ≥ 100000 into none.
TEXT_VOLUME_RE: Final = re.compile(
    r"([0-9]+(?:[.,][0-9]+)?)" + JS_SPACE + r"*(?:kub|куб|m3|м3|m³|м³)", re.I
)
MEASURE_MAX: Final = Decimal(100000)
# 04 §7 item 11: what a value may not carry (a pseudonym never restored).
_PSEUDONYM: Final = TOKEN_TRACE_RE
# The note's person line (05 §B.1), as push_gate's item 11 finds it.
MASUL_PREFIX: Final = push_gate.MASUL_PREFIX


def js_trim(s: str) -> str:
    """``String.prototype.trim()``."""
    return _JS_EDGES.sub("", s)


def collapse(s: str) -> str:
    """The ERP's ``\\s+`` collapse, then trim."""
    return js_trim(JS_SPACE_RUN.sub(" ", s))


def text_volume(text: str) -> Decimal | None:
    """The ERP's routing volume of ``text`` (textVolume + parseMeasure): the
    FIRST «N kub» alone decides; a first 0 or one ≥ 100000 is no volume."""
    found = TEXT_VOLUME_RE.search(text)
    if found is None:
        return None
    value = Decimal(found.group(1).replace(",", "."))
    return value if 0 < value < MEASURE_MAX else None


# --- the body ----------------------------------------------------------------------


class PushPayload(BaseModel):
    """FIELD ORDER IS THE JSON BODY ORDER, which is the order of the ERP's
    note lines (05 §A.2): utm_source right after the note, so the fallback
    door's keyword rule always sees it inside the 1000-unit cut and no digit
    at the end of the note is read by textVolume as «<n> kub» with the next
    line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    external_id: str  # cs:<company_uuid>:<yyyymm>, Tashkent month at claim time
    name: str  # the company display_name, never the person alone
    phone: str  # E.164 from contacts.phone; the ERP keeps the last 9
    note: str  # render_note(), ≤ NOTE_MAX
    utm_source: str = UTM_SOURCE
    kub: str | None  # a STATED volume ≥ 1 only, a plain number string
    firma: str | None  # display_name + legal form → leads.company (≤ 200)
    stir: str | None
    telegram: str | None
    instagram: str | None
    website: str | None
    address: str | None
    imports: str | None
    score: str
    evidence: str


class PayloadError(Exception):
    """The candidate's facts cannot make a body within the ERP's rules."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NoPhone(PayloadError):
    def __init__(self) -> None:
        super().__init__("no_phone")


def safe(value: str) -> str:
    """No NUL, no lone surrogate (``well_formed``), U+001C–U+001F and U+0085
    as a space — what every body value goes through (05 §B.1)."""
    return well_formed(value).translate(_CONTROLS)


def clean(value: str | None) -> str | None:
    """A body value as the ERP may store it: ``safe``, every JS whitespace run
    one space, trimmed; an empty value is None (``exclude_none`` drops it)."""
    if value is None:
        return None
    text = collapse(safe(value))
    return text or None


def clip_words(text: str, max_units: int) -> str:
    """``text`` within ``max_units`` UTF-16 units, cut at a word boundary
    with «…» when it does not fit — at a code point only when the first word
    alone is too long (a code point is never halved)."""
    if text_len(text) <= max_units:
        return text
    budget = max_units - text_len(ELLIPSIS)
    cut = ""
    for ch in text:
        if text_len(cut + ch) > budget:
            break
        cut += ch
    space = max((m.start() for m in JS_SPACE_RUN.finditer(cut)), default=-1)
    if space > 0:
        cut = cut[:space]
    return js_trim(cut).rstrip(",.;:·—-") + ELLIPSIS


# A sentence ends at «.», «!», «?» or «…» before a space.
_SENTENCE_BREAK: Final = re.compile(r"(?<=[.!?…])\s+")


def fit_opener(text: str, max_units: int) -> str:
    """An opener within ``max_units`` UTF-16 units that still ends the way
    every opener must (04 §4.5): its last sentence — the opt-out line — and
    the question before it are kept whole; then whole sentences from the
    start, in order, while they fit; the sentences dropped between are one
    «…». Never a sentence cut in the middle. ``text`` whole when it fits;
    cut by words (``clip_words``) only when not even its last sentence does.
    Pure."""
    text = collapse(text)
    if text_len(text) <= max_units:
        return text
    parts = _SENTENCE_BREAK.split(text)
    closing, body = [parts[-1]], parts[:-1]
    gap = text_len(ELLIPSIS) + 1

    def size(items: Sequence[str]) -> int:
        return text_len(" ".join(items))

    if body and body[-1].endswith("?") and size([body[-1], *closing]) + gap <= max_units:
        closing.insert(0, body.pop())
    if size(closing) + gap > max_units:
        return clip_words(text, max_units)
    head: list[str] = []
    for sentence in body:
        if size([*head, sentence, *closing]) + gap > max_units:
            break
        head.append(sentence)
    return " ".join([*head, ELLIPSIS, *closing])


def _clip_points(text: str, max_points: int) -> str:
    """The same at a code-point budget (the seller's push preview)."""
    if len(text) <= max_points:
        return text
    cut = text[: max_points - 1]
    space = max((m.start() for m in JS_SPACE_RUN.finditer(cut)), default=-1)
    if space > 0:
        cut = cut[:space]
    return js_trim(cut).rstrip(",.;:·—-") + ELLIPSIS


def fmt_number(value: Decimal) -> str:
    """A plain number string: «2», «2.5», «30» (never «3E+1»)."""
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fmt_points(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return "0"
    return fmt_number(number) if number else "0"


SOURCE_TITLE_UZ: Final[Mapping[str, str]] = {
    "telegram": "Telegram guruh",
    "olx": "OLX",
    "instagram": "Instagram",
    "directory": "Katalog",
    "vacancy": "hh.uz vakansiya",
    "exhibitor": "Koʻrgazma",
    "maps": "Xarita",
    "brand": "Brend diler sahifasi",
    "marketplace": "Uzum doʻkoni",
    "search": "Qidiruv natijasi",
    "registry": "Reestr",
    "manual": "Qoʻlda kiritilgan",
    "erp": "ERP",
}


def names_a_person(text: str, *, known: Sequence[str] = ()) -> bool:
    """A person's name in ``text``, found as the pseudonymiser finds one (04
    §4.2 — its one home, ``pseudonyms.pseudonymise(names=…)``): the names
    the caller knows (``known``, any case, any apostrophe, with the endings
    people write — «Akmalga» is Akmal) and the person names the text itself
    states (``normalise.person_names``) written as a name is written, with a
    capital: «umid qilamiz» is a hope, «Umid» a person."""
    stated = [w for w in person_names(text) if w[:1].isupper()]
    names = [n for n in (*known, *stated) if n]
    if not names:
        return False
    masked = pseudonymise(text, names=names).text
    return any(m.group(1) == "NAME" for m in TOKEN_RE.finditer(masked))


def contactless(text: str) -> bool:
    """No phone (Uzbek or foreign, or a long digit run), no Telegram,
    Instagram or messenger handle, no e-mail and no pseudonym in ``text`` —
    05 §B.1: no phone or handle beyond what the body already carries. The
    shapes are the pseudonymiser's (``pseudonyms.unmasked``, one home)."""
    return not (unmasked(text) or _PSEUDONYM.search(text))


def _quotable(text: str, *, known: Sequence[str] = ()) -> bool:
    """A quote the note may carry (05 §B.1): ``contactless`` — the body
    carries the one phone and the company's own handles — no price figure
    (the note template's «no price figures»), and no person's name (04 §7
    item 11: the person appears only on the «Masʼul» line, and only with a
    legal basis; ``names_a_person``)."""
    return not (
        not contactless(text)
        or PRICE_RE.search(text)
        or names_a_person(text, known=known)
    )


def masul_line(c: CandidateView) -> str | None:
    """The «Masʼul» line (05 §B.1: only a person-tier contact with its legal
    basis), or None — also when the name or the role carries a phone, a
    handle or a messenger id (a display name «Rustam 90 000 00 07»: 06 §5.1
    allows no second number, and the body carries the one phone)."""
    if c.person is None:
        return None
    name = clean(c.person.full_name)
    role = clean(c.person.role)
    if not name or not contactless(name) or (role and not contactless(role)):
        return None
    return f"{MASUL_PREFIX}{name}" + (f" ({role})" if role else "")


def _shown(text: str) -> str:
    """Text as a body value carries it, whitespace collapsed (a quote and the
    excerpt it must be a substring of are compared this way)."""
    return collapse(safe(text))


def known_names(c: CandidateView) -> tuple[str, ...]:
    """The person names the view knows: its decision maker's."""
    return (c.person.full_name,) if c.person is not None else ()


def verified_quote(s: SignalEvidence, *, known: Sequence[str] = ()) -> str | None:
    """The signal's quote as the note prints it (whitespace collapsed), when
    it is a verbatim substring of its evidence excerpt (02 §2.6) and
    quotable (``known``: the person names a quote must not carry beside the
    ones it states); else None."""
    if not s.quote:
        return None
    quote = _shown(s.quote)
    if not quote or quote not in _shown(s.excerpt):
        return None
    return quote if _quotable(quote, known=known) else None


def cited(c: CandidateView) -> tuple[SignalEvidence, str]:
    """The signal NEGA TOPILDI cites — the best ranked one with a verified
    quote (``CandidateView.signals`` is ranked) among those that add points:
    a negative signal or a gate flag says why the company is NOT a lead.
    Every ERP note cites evidence: URL + date + verbatim quote (CLAUDE.md),
    so none is a refusal."""
    known = known_names(c)
    for s in c.signals:
        if s.points <= 0:
            continue
        quote = verified_quote(s, known=known)
        if quote is not None:
            return s, quote
    raise PayloadError("no_quote")


def _day(at: datetime) -> str:
    return at.astimezone(settings.tz).strftime("%d.%m.%Y")


def url_line(s: SignalEvidence) -> str:
    """The URL is never cut: one over URL_MAX is named by its evidence id
    (the note's and the owner card's 🔗 line, one rule)."""
    url = clean(s.url) or ""
    return url if url and text_len(url) <= URL_MAX else f"evidence #{s.evidence_id}"


_url_line = url_line


def nega_topildi(c: CandidateView) -> str:
    """The first section, whole within the seller's push preview (159 code
    points): source · date · «quote», the quote cut at a word if need be."""
    s, quote = cited(c)
    title = SOURCE_TITLE_UZ.get(s.source_family, s.source_key)
    prefix = f"NEGA TOPILDI: {title} · {_day(s.published_at or s.fetched_at)} · «"
    budget = min(QUOTE_MAX, PUSH_PREVIEW - len(prefix) - 1)
    return f"{prefix}{_clip_points(quote, budget)}»"


# What the note says of a firm only the resolver's label names (05 §B.1).
NAMELESS_FIRM_UZ: Final = "nomi nomaʼlum"


def firma_of(c: CandidateView) -> str | None:
    """The display name with its legal form (≤ COMPANY_MAX); None for the
    resolver's label of a firm nobody named (``companies.is_nameless_label``:
    «Telegram mijoz #1» is no firm — the ERP leaves leads.company empty,
    and the lead keeps the label as its name)."""
    if companies.is_nameless_label(c.display_name):
        return None
    name = clean(c.display_name) or ""
    form = clean(c.legal_form)
    if form and form.lower() not in name.lower().split():
        joined = f"{name} {form}"
        if text_len(joined) <= COMPANY_MAX:
            return joined
    return clip_words(name, COMPANY_MAX)


# Whether the goods come from China, as the note and the owner card say it:
# a cited China origin is «ha», a China-dominant sector «ehtimol».
CHINA_WORDS: Final = (("cn_origin", "ha"), ("cn_sector_prior", "ehtimol"))


def china_signal(c: CandidateView) -> tuple[SignalEvidence, str] | None:
    """The signal that says China and its word (``CHINA_WORDS``), or None."""
    for key, word in CHINA_WORDS:
        found = next((s for s in c.signals if s.key == key), None)
        if found is not None:
            return found, word
    return None


def _origin(c: CandidateView) -> str | None:
    said = china_signal(c)
    if said is None:
        return None
    found, word = said
    hint = verified_quote(found, known=known_names(c))
    if hint:
        return f"Xitoy: {word} («{_clip_points(hint, ORIGIN_HINT_MAX)}»)"
    return f"Xitoy: {word}"


def _goods(c: CandidateView) -> list[str]:
    return [g for g in (clean(x) for x in c.goods[:GOODS_MAX]) if g]


def stated_m3(c: CandidateView) -> Decimal | None:
    """The stated volume a note or card names, when one was stated."""
    return c.stated_m3 if c.stated_m3 is not None and c.stated_m3 > 0 else None


def band_of(c: CandidateView) -> str | None:
    """The volume band said when nothing was stated: the company's band (a
    ``BAND_HAJM`` key), or «container» when the band is unknown and a
    container signal stands; None when there is no band to say."""
    band = c.volume_band
    if band not in BAND_HAJM and any(s.key == "vol_container" for s in c.signals):
        band = "container"
    return band if band in BAND_HAJM else None


def _hajm(c: CandidateView, *, per_month: bool) -> str | None:
    words: str | None = None
    stated = stated_m3(c)
    if stated is not None:
        words = f"{fmt_number(stated)} kub"
    else:
        band = band_of(c)
        words = BAND_HAJM[band] if band is not None else None
    if words is None:
        return None
    month = clean(c.per_month) if per_month else None
    return f"HAJM: {words}" + (f" · {month}" if month else "")


def ball_parts(c: CandidateView) -> list[str]:
    """The score's breakdown in the owner's words — «hajm 30», «Xitoy 20» …
    and «minus N» when negatives counted; empty without a breakdown (the
    note's BALL section and the owner card's 🧮 line, one rule)."""
    groups = c.breakdown.get("groups")
    if not isinstance(groups, Mapping):
        return []

    def used(name: str) -> str:
        group = groups.get(name)
        return _fmt_points(group.get("used", 0) if isinstance(group, Mapping) else 0)

    parts = [
        f"hajm {used('volume')}",
        f"Xitoy {used('origin')}",
        f"firma {used('solidity')}",
        f"yangilik {used('recency')}",
        f"aloqa {used('contact')}",
        f"tasdiq {used('corroboration')}",
    ]
    negatives = c.breakdown.get("negatives")
    if isinstance(negatives, list):
        total = sum(
            (
                Decimal(str(n.get("points", 0)))
                for n in negatives
                if isinstance(n, Mapping)
            ),
            Decimal(0),
        )
        if total:
            parts.append(f"minus {fmt_number(abs(total))}")
    return parts


def _ball(c: CandidateView, *, detail: bool) -> str:
    line = f"BALL: {c.score}/100"
    parts = ball_parts(c) if detail else []
    if not parts:
        return line
    return f"{line} ({', '.join(parts)})"


# 05 §B.1's drop order (reverse priority), then the agent's own last resorts
# when a note still does not fit (a long URL with every section full).
SHRINK_STEPS: Final = (
    "ball_detail",
    "masul",
    "opener_short",
    "per_month",
    "address_short",
    "opener",
    "goods_short",
    "imports_short",
    "kompaniya_short",
    "evidence_url",
    "goods_line",
    "imports",
    "address",
    "kompaniya",
)


def render_note(c: CandidateView, *, level: int = 0) -> str:
    """The Uzbek note (05 §B.1), sections in order, an empty one omitted, one
    per line (the ERP makes one paragraph of it). ``level`` is how many of
    ``SHRINK_STEPS`` apply. Pure."""
    drop = set(SHRINK_STEPS[:level])
    s, _ = cited(c)
    sections = [nega_topildi(c), _url_line(s)]
    goods = _goods(c)
    origin = _origin(c)
    if (goods or origin) and "goods_line" not in drop:
        what = ", ".join(goods)
        if "goods_short" in drop:
            what = clip_words(what, 80)
        parts = [b for b in (what, origin) if b]
        sections.append(clip_words("NIMA OLIB KELADI: " + " · ".join(parts), 240))
    hajm = _hajm(c, per_month="per_month" not in drop)
    if hajm:
        sections.append(hajm)
    if "kompaniya" in drop:
        pass  # the firma line still names it
    elif "kompaniya_short" in drop:
        sections.append(f"KOMPANIYA: {firma_of(c) or NAMELESS_FIRM_UZ}")
    else:
        stir = clean(c.stir) or "—"
        sector = ", ".join(x for x in (clean(v) for v in c.sectors[:2]) if x)
        bits: list[str | None] = [
            firma_of(c) or NAMELESS_FIRM_UZ,
            f"STIR {stir}",
            clean(c.city),
            REGISTRY_UZ.get(c.registry_status),
            sector or None,
        ]
        sections.append("KOMPANIYA: " + " · ".join(b for b in bits if b))
    masul = masul_line(c) if "masul" not in drop else None
    if masul:
        sections.append(masul)
    opener = clean(c.opener)
    if opener and "opener" not in drop:
        # Shortened, the opener keeps its question and its opt-out line.
        limit = OPENER_SHORT if "opener_short" in drop else OPENER_MAX
        sections.append("NIMA DEYISH: " + fit_opener(opener, limit))
    sections.append(_ball(c, detail="ball_detail" not in drop))
    return "\n".join(sections)


def _handle(value: str | None) -> str | None:
    text = clean(value)
    if not text:
        return None
    return "@" + text.lstrip("@")


def _evidence_value(c: CandidateView, *, url_only: bool) -> str:
    s, quote = cited(c)
    head = _url_line(s)
    if url_only:
        return head
    value = f'{head} — "{clip_words(quote, EVIDENCE_QUOTE_MAX)}"'
    return value if text_len(value) <= PAIR_VALUE_MAX else head


def _payload(c: CandidateView, level: int) -> PushPayload:
    drop = set(SHRINK_STEPS[:level])
    stated = c.stated_m3
    kub = fmt_number(stated) if stated is not None and stated >= 1 else None
    address = None if "address" in drop else clean(c.address)
    if address:
        address = clip_words(
            address, ADDRESS_SHORT if "address_short" in drop else PAIR_VALUE_MAX
        )
    imports = None if "imports" in drop else (", ".join(_goods(c)) or None)
    if imports:
        imports = clip_words(imports, 120 if "imports_short" in drop else PAIR_VALUE_MAX)
    assert c.phone is not None
    return PushPayload(
        external_id=c.external_id,
        name=c.display_name,
        phone=c.phone,
        note=render_note(c, level=level),
        kub=kub,
        firma=firma_of(c),
        stir=clean(c.stir),
        telegram=_handle(c.telegram),
        instagram=clean(c.instagram.lstrip("@")) if c.instagram else None,
        website=clean(c.website_domain),
        address=address,
        imports=imports,
        score=str(c.score),
        evidence=_evidence_value(c, url_only="evidence_url" in drop),
    )


def fits(p: PushPayload) -> bool:
    return text_len(p.note) <= NOTE_MAX and text_len(landed_note(p)) <= LANDED_NOTE_MAX


def build_payload(c: CandidateView) -> PushPayload:
    """The body for ``c`` (pure): raises ``NoPhone`` without a lead phone and
    ``PayloadError`` when the name breaks the ERP's limits, no signal carries
    a verified quote, or nothing fits. The note is shortened in
    ``SHRINK_STEPS`` order until the note (≤ 700) and the landed note
    (≤ 1,000) both fit."""
    if not c.phone:
        raise NoPhone
    # The name goes as the company's display name exactly (item 11): one
    # that carries a character the body may not is refused, never altered.
    name = c.display_name
    if safe(name) != name or not NAME_MIN <= text_len(collapse(name)) <= NAME_MAX:
        raise PayloadError("name")
    for level in range(len(SHRINK_STEPS) + 1):
        payload = _payload(c, level)
        if fits(payload):
            return payload
    raise PayloadError("too_long")


def extras_in_order(p: PushPayload) -> list[tuple[str, str]]:
    """The body's extra keys in body order, as ``exclude_none`` sends them."""
    body = p.model_dump(exclude_none=True)
    return [(k, v) for k, v in body.items() if k not in IDENTITY_KEYS]


def landed_note(p: PushPayload) -> str:
    """The note the ERP keeps (05 §A.2, ``fromPlain``): the note, then one
    ``key: value`` line per extra key whose value JS ``trim()`` leaves
    non-empty, every whitespace run collapsed to one space, trimmed. Pure."""
    lines = [js_trim(p.note)] + [
        f"{k}: {js_trim(v)}" for k, v in extras_in_order(p) if js_trim(v)
    ]
    return collapse("\n".join(lines))


def canonical_sha256(p: PushPayload) -> str:
    """02 §2.10 rule 6: sorted keys, no spaces, a None dropped — the claim's
    own (``push_gate.canonical_sha256``)."""
    return push_gate.canonical_sha256(p.model_dump(exclude_none=True))


def external_id_for(company_id: uuid.UUID, now: datetime) -> str:
    return push_gate.external_id_for(company_id, now)


def push_window_open(now: datetime) -> bool:
    """Mon–Sat 09:30–17:30 Tashkent by default (``PUSH_WINDOW``/``PUSH_DAYS``)."""
    return push_gate.window_opens(now) is None


# --- the transport (05 §B.1) -------------------------------------------------------

# The request never reached the ERP: a re-send cannot land it twice.
RETRY_IN_ATTEMPT: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# The ERP may still be running it, or be done: no re-send inside the attempt.
MAY_HAVE_LANDED: Final = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)
# The request never left for the ERP either (refused connection, a proxy
# that refused the tunnel, a scheme the client cannot speak): ``failed``.
NEVER_SENT: Final = (httpx.ConnectError, httpx.ProxyError, httpx.UnsupportedProtocol)
TRIES_IN_ATTEMPT: Final = 3
TRY_BASE: Final = 1.0
TRY_CAP: Final = 8.0


class ErpBadBody(Exception):
    """A 2xx whose body is not ``{"ok": true}`` — never retried: the lead may
    have landed."""

    def __init__(self, status: int) -> None:
        super().__init__(f"ERP answered {status} with an unreadable body")
        self.status = status


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, RETRY_IN_ATTEMPT) or (
        isinstance(exc, ErpHttpError) and exc.status >= 500
    )


def endpoint_for(source: str) -> str:
    """The lead door's path for ``source`` — what ``push_log.endpoint``
    records at claim time and what ``_post`` posts to."""
    return PUSH_PATH.format(source=source)


async def _post(erp: ErpClient, source: str, body: Mapping[str, Any]) -> int:
    """One POST of ``body``: its status on ``{"ok": true}``, else
    ``ErpBadBody`` (a 2xx) or ``ErpHttpError`` (anything else — a 3xx is
    never followed: the client does not, so the key is never re-sent)."""
    r = await erp.post_lead(endpoint_for(source), body)
    if 200 <= r.status_code < 300:
        try:
            parsed = r.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("ok") is True:
            return r.status_code
        raise ErpBadBody(r.status_code)
    raise ErpHttpError(r.status_code)


@dataclass(frozen=True, slots=True)
class Answer:
    """What one attempt's tries mean for its ``push_log`` row."""

    status: Literal["sent", "unknown", "failed"]
    http_status: int | None = None
    error: str | None = None


def error_for_status(status: int) -> str:
    if 300 <= status < 400:
        return "http_3xx"
    if status == 404:
        return "http_404"
    if status >= 500:
        return "http_5xx"
    return "http_4xx"


async def _attempt(erp: ErpClient, source: str, body: Mapping[str, Any]) -> Answer:
    """The HTTP tries of one attempt and what they mean (05 §B.1's table)."""
    try:
        status = await with_backoff(
            lambda: _post(erp, source, body),
            attempts=TRIES_IN_ATTEMPT,
            base=TRY_BASE,
            cap=TRY_CAP,
            retry_on=_retryable,
            sleep=erp.sleep,
        )
    except ErpBadBody as exc:
        return Answer("unknown", exc.status, "http_2xx_badbody")
    except MAY_HAVE_LANDED:
        return Answer("unknown", None, "timeout")
    except ErpHttpError as exc:
        return Answer("failed", exc.status, error_for_status(exc.status))
    except (httpx.ConnectTimeout, httpx.PoolTimeout):
        return Answer("failed", None, "timeout")
    except NEVER_SENT:
        return Answer("failed", None, "conn")
    except httpx.HTTPError:
        # Any other transport failure (a close error, a protocol error of
        # our own side, a redirect loop): the request may have reached the
        # ERP, so no resend — the row is settled like a read timeout's.
        return Answer("unknown", None, "timeout")
    return Answer("sent", status, None)


# --- push_one -----------------------------------------------------------------------

PushStatus = Literal["refused", "sent", "unknown", "failed"]


@dataclass(frozen=True, slots=True)
class PushResult:
    status: PushStatus
    refusals: tuple[Refusal, ...] = ()
    push_log_id: int | None = None
    external_id: str | None = None
    attempt: int | None = None
    http_status: int | None = None
    error: str | None = None

    @property
    def codes(self) -> list[str]:
        return [r.code for r in self.refusals]

    @classmethod
    def refused(cls, *refusals: Refusal) -> PushResult:
        return cls(status="refused", refusals=tuple(refusals))


# Refusals true of every candidate at this moment: the job stops its run.
STOP_CODES: Final = frozenset(
    {"cap", "source_cap", "window", "disabled", "paused", "breaker", "stale"}
)
# A push of make push-test's synthetic company skips item 13 (it has no
# score) and only while its breakdown says so (05 §B.1).
SYNTHETIC_SKIPPED: Final = frozenset({13})
BREAKER_ERRORS: Final[Mapping[str, erp_feeds.BreakerKey]] = {
    "http_404": "push_404",
    "http_3xx": "erp_redirect",
}
# 06 §4.2 push_rejected: raised here on a 4xx other than 404 and told at once
# (TOLD_FIRST_ELSEWHERE), cleared by the next push the ERP takes — so a tell
# Telegram did not deliver is said by the health sweep, and its recovery too.
REJECTED_PROBLEM: Final = "push_rejected"
# Answers true of every push at this moment (05 §B.1): the job ends its run on
# them. A 4xx other than 404 is a proxy or a wrong CRM_BASE_URL — the ERP door
# answers the agent only 200 / 404 / 503 — so the candidate it hit goes back
# to review (05 §B.1's table) and the rest of the run would only follow it.
STOP_ERRORS: Final[Mapping[str, str]] = {**BREAKER_ERRORS, "http_4xx": "rejected"}


def synthetic_marked(candidate: LeadCandidate) -> bool:
    """``score_breakdown.gates.test`` — make push-test's company (05 §B.1)."""
    breakdown = candidate.score_breakdown or {}
    gates = breakdown.get("gates") if isinstance(breakdown, Mapping) else None
    return isinstance(gates, Mapping) and gates.get("test") is True


async def _lock_push(session: AsyncSession, push_log_id: int) -> PushLog | None:
    found: PushLog | None = await session.scalar(
        sa.select(PushLog)
        .where(PushLog.id == push_log_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return found


async def resendable(session: AsyncSession, external_id: str) -> PushLog | None:
    """The row whose body the next attempt re-sends (02 §2.10 rules 4 and 6):
    the id's newest row, when it is a ``failed`` one nobody superseded with
    its body kept. A further attempt carries the identical body — never one
    rebuilt from today's facts: a signal that arrived, a city enriched or a
    signal expired between two attempts would make it a different body,
    which rule 6 refuses under this id for the rest of the month."""
    newest: PushLog | None = await session.scalar(
        sa.select(PushLog)
        .where(PushLog.external_id == external_id)
        .order_by(PushLog.attempt.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if newest is None or newest.status != "failed" or newest.superseded_at is not None:
        return None
    return newest if isinstance(newest.payload, dict) else None


async def last_attempt(session: AsyncSession, external_id: str) -> int:
    """The id's highest attempt so far (0 for none) — what «three attempts
    spent» is measured on, whichever row is the delivered one."""
    found = await session.scalar(
        sa.select(sa.func.max(PushLog.attempt)).where(PushLog.external_id == external_id)
    )
    return int(found or 0)


async def push_one(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    now: datetime,
    erp: ErpClient,
    repush_of: int | None = None,
    synthetic: bool = False,
) -> PushResult:
    """One push of ``candidate_id`` (05 §B.1, 02 §2.10). Commits: the claim
    before the POST, the answer after it. ``repush_of`` is the delivered
    ``push_log`` row a rule-5 re-push or ``/push tekshir`` supersedes — its
    stored body is re-sent as the next attempt, the supersede stands only if
    the claim does, and it is taken back when that attempt fails (``_record``:
    a failed attempt replaced nothing the ERP holds). Without ``repush_of``,
    a further attempt after a failed one re-sends that row's stored body
    (``resendable``); only attempt 1 of an id builds one. ``synthetic`` is
    ``make push-test``'s alone: item 13 skipped for a company marked
    ``gates.test``, nothing recorded on a refusal (the tool's synthetic
    company is rolled back with it); a marked company is never pushed by
    anybody else — the job, a rule-5 re-push, ``/push tekshir``."""
    source = settings.crm_push_source
    # Lock order: the push_log row, the company, the candidate — the row
    # before the candidate is outcomes.promote's and _record's order (the
    # other way round, the unknown walk promoting the row a re-push
    # supersedes deadlocks with it); the company before the candidate is the
    # resolver's and emit's (push_gate.lock_for_push).
    prior = await _lock_push(session, repush_of) if repush_of is not None else None
    company_id = await push_gate.lock_company_of(session, candidate_id)
    candidate: LeadCandidate | None = await session.scalar(
        sa.select(LeadCandidate)
        .where(LeadCandidate.id == candidate_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if candidate is not None and candidate.company_id != company_id:
        # A merge moved it between the read and the locks: the next run asks.
        candidate = None
    if candidate is None:
        await session.rollback()
        return PushResult.refused(Refusal(1, "not_approved"))
    if not synthetic and synthetic_marked(candidate):
        await session.rollback()
        return PushResult.refused(Refusal(13, "test_company"))
    if repush_of is not None:
        if prior is None or prior.candidate_id != candidate.id:
            await session.rollback()
            return PushResult.refused(Refusal(12, "external_id"))
        if prior.superseded_at is not None:
            await session.rollback()
            return PushResult.refused(Refusal(6, "repushed"))
        if prior.status not in e.PUSH_DELIVERED_STATUSES:
            await session.rollback()
            return PushResult.refused(Refusal(6, "external_id"))
    approved = candidate.state == "approved"
    # A re-push may read 'pushed', and only for the candidate's own id.
    own_repush = (
        prior is not None
        and candidate.state == "pushed"
        and candidate.external_id == prior.external_id
    )
    if not (approved or own_repush) or not candidate.approved_by:
        await session.rollback()
        return PushResult.refused(Refusal(1, "not_approved"))
    marked = synthetic and synthetic_marked(candidate)
    if approved and not synthetic_marked(candidate):
        # 04 §7 item 13: the score at push time. score_job never touches an
        # approved candidate (signals.RESCORE_STATES), so a negative signal,
        # a lost phone or plain decay since the owner's tap is read here,
        # under the company → candidate locks; item 13 reads the live total
        # and a drop below 60 takes the candidate back to review («Ball
        # tushdi», push_gate.TO_REVIEW). The score only — never the state.
        await candidates.rescore(session, candidate, now=now)
    skip = SYNTHETIC_SKIPPED if marked else frozenset()
    record = not synthetic
    body: dict[str, Any] | None
    resent = (
        None
        if prior is not None
        else await resendable(
            session, push_gate.external_id_for(candidate.company_id, now)
        )
    )
    if prior is not None:
        body = dict(prior.payload) if isinstance(prior.payload, dict) else None
    elif resent is not None and isinstance(resent.payload, dict):
        body = dict(resent.payload)
    else:
        try:
            view = await candidates.load_view(session, candidate.id, now=now)
            body = build_payload(view).model_dump(exclude_none=True)
        except NoPhone:
            body = None  # item 4 refuses the missing phone itself
        except PayloadError as exc:
            refusal = Refusal(11, "payload", detail=exc.code)
            if record:
                await push_gate.record_refusals(session, candidate, [refusal], now=now)
                await session.commit()
            else:
                await session.rollback()
            return PushResult.refused(refusal)
    savepoint = await session.begin_nested() if prior is not None else None
    if prior is not None:
        prior.superseded_at = now
        await session.flush()
    claim = await push_gate.claim(
        session,
        candidate.id,
        now=now,
        payload=body,
        endpoint=endpoint_for(source),
        repush_of=prior.id if prior is not None else None,
        skip_checks=skip,
        record=record and prior is None,
    )
    if not claim.claimed:
        if savepoint is not None:
            # The supersede goes with the refused claim; the refusals are
            # written outside the savepoint it rolled back.
            await savepoint.rollback()
            relocked = await push_gate.lock_candidate(session, candidate_id)
            if relocked is not None and record:
                await push_gate.record_refusals(
                    session, relocked, claim.refusals, now=now
                )
        if record:
            await session.commit()
        else:
            await session.rollback()
        return PushResult.refused(*claim.refusals)
    if savepoint is not None:
        await savepoint.commit()
    assert claim.push_log_id is not None and body is not None
    # 02 §2.10 rule 3: the claim is durable before the request leaves.
    await session.commit()
    answer = await _attempt(erp, source, body)
    return await _record(
        session,
        claim.push_log_id,
        candidate_id,
        answer,
        now=now,
        record=record,
        repush_of=prior.id if prior is not None else None,
    )


async def _record(
    session: AsyncSession,
    push_log_id: int,
    candidate_id: uuid.UUID,
    answer: Answer,
    *,
    now: datetime,
    record: bool,
    repush_of: int | None = None,
) -> PushResult:
    """The answer onto its row and its candidate (05 §B.1's table), then the
    owner told what only he can mend (after the commit: never a lock held
    across the notifier). A re-push whose attempt ``failed`` gives the
    superseded row back its place: nothing new reached the ERP, so the push
    is as unconfirmed as before — ``push_unconfirmed`` keeps counting it,
    rule 5 re-pushes it again as the next attempt (after ``ATTEMPT_DELAYS``,
    up to attempt 3), and ``/push tekshir`` still finds it."""
    row = await _lock_push(session, push_log_id)
    assert row is not None
    prior = await _lock_push(session, repush_of) if repush_of is not None else None
    if row.status in ("sending", "unknown"):
        row.status = answer.status
        row.http_status = answer.http_status
        row.error = answer.error
        row.finished_at = now
    if row.status == "failed" and prior is not None and prior.superseded_at is not None:
        prior.superseded_at = None
    candidate = await push_gate.lock_candidate(session, candidate_id)
    breaker = BREAKER_ERRORS.get(answer.error or "")
    if candidate is not None:
        if answer.status == "sent" and candidate.state == "approved":
            await candidates.transition(
                session, candidate, "pushed", now=now, external_id=row.external_id
            )
        elif answer.status == "failed" and breaker is None and record:
            refusal: Refusal | None = None
            if answer.error == "http_4xx":
                refusal = Refusal(0, "erp_refused", detail=str(answer.http_status))
            elif row.attempt >= push_gate.MAX_ATTEMPTS:
                refusal = Refusal(6, "attempts_spent")
            if refusal is not None:
                await push_gate.record_refusals(session, candidate, [refusal], now=now)
    if breaker is not None:
        await erp_feeds.trip_breaker(
            session, breaker, status=answer.http_status or 0, now=now, by="push"
        )
    if answer.error == "http_4xx":
        await health.raise_problem(
            session,
            REJECTED_PROBLEM,
            now=now,
            by="push",
            detail={"status": answer.http_status},
        )
    elif answer.status == "sent":
        await health.clear_problem(session, REJECTED_PROBLEM)
    await session.commit()
    if breaker is not None:
        await erp_feeds.tell_breaker(breaker, status=answer.http_status or 0, now=now)
    elif answer.error == "http_4xx":
        await costs.tell_once(
            REJECTED_PROBLEM, rejected_text(answer.http_status), now=now
        )
    log.info(
        "push %s",
        answer.status,
        extra={
            "push_log_id": push_log_id,
            "attempt": row.attempt,
            "http_status": answer.http_status,
            "error": answer.error,
        },
    )
    return PushResult(
        status=answer.status,
        push_log_id=push_log_id,
        external_id=row.external_id,
        attempt=row.attempt,
        http_status=answer.http_status,
        error=answer.error,
    )


def rejected_text(status: int | None) -> str:
    """The owner's line for a 4xx other than 404 (05 §B.1) — health's, the
    one home of its words (push_rejected)."""
    return health.push_rejected_text(status)


# --- crm_push_job (every minute inside the window) -----------------------------------

# Pushes that reach the ERP in one run (sent, unknown or failed).
JOB_LIMIT: Final = 25
# Approved candidates one run looks at: a refused one (a cooldown, a recent
# push of the phone or the company, an attempt that waits) does not take a
# push's place, so the ones behind it are reached.
JOB_SCAN: Final = 200
# After a 4xx other than 404 the job holds this long before it tries one
# more push: the same proxy would send every approved candidate back to
# review, one a minute (05 §B.1).
REJECTED_PAUSE: Final = timedelta(minutes=30)


@dataclass(slots=True)
class JobReport:
    skipped: str | None = None
    candidates: int = 0
    sent: int = 0
    unknown: int = 0
    failed: int = 0
    refused: int = 0
    errors: int = 0
    stopped: str | None = None

    def add(self, result: PushResult) -> None:
        if result.status == "sent":
            self.sent += 1
        elif result.status == "unknown":
            self.unknown += 1
        elif result.status == "failed":
            self.failed += 1
        else:
            self.refused += 1

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, 0)}

    @property
    def reached(self) -> int:
        """Pushes that went out to the ERP (whatever it answered)."""
        return self.sent + self.unknown + self.failed


async def rejected_until(session: AsyncSession, now: datetime) -> datetime | None:
    """While the ERP door's newest answer is a 4xx other than 404 (05 §B.1:
    a proxy or a wrong ``CRM_BASE_URL``, true of every push), the moment the
    job tries again — ``REJECTED_PAUSE`` after it; else None. Any later
    answer ends it, a 2xx from a rule-5 re-push included."""
    newest = (
        await session.execute(
            sa.select(PushLog.error, PushLog.finished_at)
            .where(PushLog.finished_at.is_not(None), PushLog.finished_at <= now)
            .order_by(PushLog.finished_at.desc(), PushLog.id.desc())
            .limit(1)
        )
    ).first()
    if newest is None or newest.error != "http_4xx":
        return None
    until: datetime = newest.finished_at + REJECTED_PAUSE
    return until if until > now else None


async def due_candidates(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """The approved candidates a push may go for, best first: none with a
    push in flight or delivered (``reconcile_job`` settles those), none of
    ``make push-test``'s synthetic companies (05 §B.1)."""
    busy = (
        sa.select(PushLog.id)
        .where(
            PushLog.candidate_id == LeadCandidate.id,
            PushLog.superseded_at.is_(None),
            PushLog.status.in_(("sending", *e.PUSH_DELIVERED_STATUSES)),
        )
        .exists()
    )
    synthetic = LeadCandidate.score_breakdown["gates"]["test"].as_boolean()
    rows = await session.scalars(
        sa.select(LeadCandidate.id)
        .where(
            LeadCandidate.state == "approved",
            ~busy,
            sa.func.coalesce(synthetic, sa.false()).is_(False),
        )
        .order_by(
            LeadCandidate.score.desc().nulls_last(),
            LeadCandidate.approved_at,
            LeadCandidate.id,
        )
        .limit(limit)
    )
    return list(rows)


async def crm_push_job(
    session_factory: SessionFactory = SessionLocal,
    erp: ErpClient | None = None,
    *,
    now: datetime | None = None,
    limit: int = JOB_LIMIT,
    scan: int = JOB_SCAN,
) -> JobReport:
    """The ``crm_push`` slot (every minute; it sends only inside the push
    window, 02 §2.10 item 2): the due candidates best first, each through
    ``push_one`` in its own session, until ``limit`` pushes went out or the
    ``scan`` looked at are done — a candidate the gate refuses for itself
    is passed over, never a stop. A refusal true of every candidate (cap,
    window, breaker, stale list …), a tripped breaker or a 4xx other than
    404 ends the run, and a 4xx holds the job ``REJECTED_PAUSE``. ``now``
    fixes the clock (tests); else each push reads it afresh.
    ``PUSH_DAILY_CAP`` is the gate's (item 9)."""
    report = JobReport()
    at = now or datetime.now(settings.tz)
    if not settings.crm_push_enabled:
        report.skipped = "disabled"
        return report
    if not push_window_open(at):
        report.skipped = "window"
        return report
    async with session_factory() as session:
        if await rejected_until(session, at) is not None:
            report.skipped = "rejected"
            return report
        ids = await due_candidates(session, limit=scan)
    report.candidates = len(ids)
    if not ids:
        return report
    client = erp
    if client is None:
        try:
            client = erp_feeds.client_from_settings()
        except ValueError as exc:
            log.warning("crm_push: no ERP client", extra={"error": str(exc)})
            report.skipped = "not_configured"
            return report
    try:
        for candidate_id in ids:
            moment = now or datetime.now(settings.tz)
            try:
                async with session_factory() as session:
                    result = await push_one(session, candidate_id, now=moment, erp=client)
            except Exception as exc:
                report.errors += 1
                log.error(
                    "crm_push: a push raised",
                    extra={
                        "candidate_id": str(candidate_id),
                        "error": type(exc).__name__,
                    },
                )
                continue
            report.add(result)
            stop = next((c for c in result.codes if c in STOP_CODES), None)
            if stop is None and result.error is not None:
                stop = STOP_ERRORS.get(result.error)
            if stop is not None:
                report.stopped = stop
                break
            if report.reached >= limit:
                break
    finally:
        if erp is None:
            await client.aclose()
    if report.sent or report.unknown or report.failed or report.errors:
        log.info("crm_push_job", extra={"counts": report.as_dict()})
    return report


async def crm_push_slot() -> dict[str, Any]:
    """The worker table's ``crm_push`` job (no arguments)."""
    return (await crm_push_job()).as_dict()


# --- reconcile_job (every 10 minutes) ------------------------------------------------

# 02 §2.10 rule 5(a): a claim this old without an answer crashed — the request
# may have landed.
CRASHED_AFTER: Final = timedelta(minutes=10)


async def settle_crashed(session: AsyncSession, *, now: datetime) -> int:
    """``sending`` rows older than ``CRASHED_AFTER`` become ``unknown``
    (blocked like ``sent``; the ``unknown`` walk or rule 5 settles them).
    Commits; returns how many."""
    result = await session.execute(
        sa.update(PushLog)
        .where(PushLog.status == "sending", PushLog.sent_at < now - CRASHED_AFTER)
        .values(status="unknown", finished_at=now)
        .returning(PushLog.id)
    )
    settled = len(result.all())
    await session.commit()
    return settled


@dataclass(slots=True)
class ReconcileJobReport:
    settled: int = 0
    walk: dict[str, Any] | None = None
    walk_error: str | None = None
    reconcile: dict[str, Any] | None = None
    reviewed: int = 0
    repushed: list[dict[str, Any]] = field(default_factory=list)
    skipped: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, 0, [])}


async def review_unconfirmed(
    session: AsyncSession, push_log_ids: Sequence[int], *, now: datetime
) -> int:
    """02 §2.10 rule 5(c), «another 48 h»: the re-pushed id still shows
    nothing — an ``approved`` candidate goes back to ``review`` with «CRM
    tasdiqlamadi»; a ``pushed`` one stays. Commits; returns how many moved."""
    moved = 0
    for push_log_id in push_log_ids:
        row = await session.get(PushLog, push_log_id)
        if row is None:
            continue
        candidate = await push_gate.lock_candidate(session, row.candidate_id)
        if candidate is None or candidate.state != "approved":
            continue
        if synthetic_marked(candidate):
            continue  # make push-test's company never becomes a card
        await push_gate.record_refusals(
            session, candidate, [Refusal(6, "unconfirmed")], now=now
        )
        moved += 1
    await session.commit()
    return moved


async def reconcile_job(
    session_factory: SessionFactory = SessionLocal,
    erp: ErpClient | None = None,
    *,
    now: datetime | None = None,
) -> ReconcileJobReport:
    """The ``reconcile`` slot (02 §2.10 rule 5): (a) a crashed claim becomes
    ``unknown``; (b) the ``unknown`` outcomes walk (``since`` the oldest
    unknown push − 10 min) promotes what the ERP shows; (c) the 48-hour rule
    — after healthy nightly walks — re-pushes each unconfirmed id once
    (inside the push window: the gate refuses one outside it, and the
    supersede is rolled back with the refusal; a re-push whose attempt
    failed reached nothing, so it stays unconfirmed and is re-pushed as the
    id's next attempt until the three are spent) and sends an ``approved``
    candidate whose re-push also went unseen back to ``review`` — never
    ``make push-test``'s company (``push_one`` refuses it ``test_company``).
    (b) and (c) only while the feeds are configured (05 §B.3)."""
    report = ReconcileJobReport()
    at = now or datetime.now(settings.tz)
    async with session_factory() as session:
        report.settled = await settle_crashed(session, now=at)
    if not erp_feeds.feeds_configured():
        report.skipped = "feeds_not_configured"
        return report
    client = erp
    if client is None:
        try:
            client = erp_feeds.client_from_settings()
        except ValueError as exc:
            log.warning("reconcile: no ERP client", extra={"error": str(exc)})
            report.skipped = "not_configured"
            return report
    try:
        async with session_factory() as session:
            try:
                walk = await erp_feeds.sync_outcomes(
                    session, client, walk="unknown", now=at
                )
                report.walk = walk.as_meta()
            except erp_feeds.FeedError as exc:
                # sync_outcomes recorded it (the breaker, feed_contract).
                report.walk_error = type(exc).__name__
        async with session_factory() as session:
            rule = await erp_feeds.reconcile_unconfirmed(session, client, now=at)
        report.reconcile = rule.as_meta()
        if rule.checked:
            async with session_factory() as session:
                report.reviewed = await review_unconfirmed(
                    session, rule.exhausted, now=at
                )
            if push_window_open(at):
                for push_log_id in rule.repushable:
                    report.repushed.append(
                        await _repush(session_factory, client, push_log_id, now=now)
                    )
    finally:
        if erp is None:
            await client.aclose()
    if report.settled or report.repushed or report.reviewed:
        log.info("reconcile_job", extra={"counts": report.as_dict()})
    return report


async def _repush(
    session_factory: SessionFactory,
    erp: ErpClient,
    push_log_id: int,
    *,
    now: datetime | None,
) -> dict[str, Any]:
    async with session_factory() as session:
        row = await session.get(PushLog, push_log_id)
        if row is None:
            return {"push_log_id": push_log_id, "status": "gone"}
        result = await push_one(
            session,
            row.candidate_id,
            now=now or datetime.now(settings.tz),
            erp=erp,
            repush_of=push_log_id,
        )
    return {"push_log_id": push_log_id, "status": result.status, "codes": result.codes}


async def reconcile_slot() -> dict[str, Any]:
    """The worker table's ``reconcile`` job (no arguments)."""
    return (await reconcile_job()).as_dict()


# --- /push tekshir (06 §1.2) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PushCheck:
    """What ``/push tekshir <external_id>`` shows before the confirm."""

    external_id: str
    push_log_id: int | None = None
    company: str | None = None
    attempt: int | None = None
    # The id's highest attempt: a re-push whose attempt failed left the
    # delivered row standing at its own (``_record``).
    attempts: int | None = None
    status: str | None = None
    sent_at: datetime | None = None
    http_status: int | None = None
    repushed: bool = False
    reported: bool = False
    payload_kept: bool = False
    refusal: str | None = None


async def push_tekshir_view(session: AsyncSession, external_id: str) -> PushCheck:
    """The delivered row of ``external_id`` and why a confirm would be
    refused (``refusal``): no delivered row, the ERP already reported it, the
    three attempts spent, the payload gone (> 90 d)."""
    row = await session.scalar(
        sa.select(PushLog)
        .where(
            PushLog.external_id == external_id,
            PushLog.status.in_(e.PUSH_DELIVERED_STATUSES),
            PushLog.superseded_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        return PushCheck(external_id=external_id, refusal="not_found")
    company = await session.get(Company, row.company_id)
    repushed = bool(
        await session.scalar(
            sa.select(sa.func.count(PushLog.id)).where(
                PushLog.external_id == external_id, PushLog.superseded_at.is_not(None)
            )
        )
    )
    reported = await session.get(Outcome, external_id) is not None
    kept = isinstance(row.payload, dict)
    attempts = await last_attempt(session, external_id)
    refusal = None
    if reported:
        refusal = "reported"
    elif attempts >= push_gate.MAX_ATTEMPTS:
        refusal = "attempts_spent"
    elif not kept:
        refusal = "no_payload"
    return PushCheck(
        external_id=external_id,
        push_log_id=row.id,
        company=company.display_name if company is not None else None,
        attempt=row.attempt,
        attempts=attempts,
        status=row.status,
        sent_at=row.sent_at,
        http_status=row.http_status,
        repushed=repushed,
        reported=reported,
        payload_kept=kept,
        refusal=refusal,
    )


# A push_log status as the owner reads it (06 §1.2's view).
PUSH_STATUS_UZ: Final[Mapping[str, str]] = {
    "sending": "yuborilmoqda",
    "sent": "yuborildi",
    "unknown": "javobi nomaʼlum",
    "failed": "xato",
}
PUSH_CHECK_REFUSED_UZ: Final[Mapping[str, str]] = {
    "not_found": "Bu identifikator bilan yuborilgan push topilmadi",
    "reported": "ERP bu pushni allaqachon koʻrsatgan — qayta yuborilmaydi",
    "attempts_spent": "Uch urinish tugagan — qayta yuborilmaydi",
    "no_payload": "Lid matni oʻchirilgan (90 kundan eski) — qayta yuborilmaydi",
}


def render_push_check(check: PushCheck) -> str:
    """The bot's text for the view (Uzbek, 06 §1.2); the confirm buttons
    ``pt:<push_log_id>:y|n`` are the bot's (T-20)."""
    if check.push_log_id is None:
        return PUSH_CHECK_REFUSED_UZ["not_found"]
    status = PUSH_STATUS_UZ.get(check.status or "", "—")
    lines = [
        f"🔎 {check.company or '—'}",
        f"Urinish: {check.attempt} · holat: {status} · "
        f"HTTP {check.http_status if check.http_status is not None else '—'}",
    ]
    if check.attempts is not None and check.attempt is not None:
        failed = check.attempts - check.attempt
        if failed > 0:
            lines.append(f"Qayta yuborish urinishlari xato bilan tugagan: {failed} ta")
    if check.sent_at is not None:
        lines.append(
            f"Yuborilgan: {check.sent_at.astimezone(settings.tz):%d.%m.%Y %H:%M}"
        )
    lines.append(
        "Qayta yuborilgan: "
        + ("ha" if check.repushed else "yoʻq")
        + " · ERPda koʻrindi: "
        + ("ha" if check.reported else "yoʻq")
    )
    if check.refusal is not None:
        lines.append(PUSH_CHECK_REFUSED_UZ[check.refusal])
    else:
        lines.append("Qayta yuboraymi?")
    return "\n".join(lines)


async def push_tekshir_confirm(
    session: AsyncSession,
    push_log_id: int,
    *,
    now: datetime,
    erp: ErpClient,
) -> PushResult:
    """The confirm of ``/push tekshir`` (06 §1.2, 02 §2.10 rule 5(d)): the
    delivered row superseded and re-pushed at once as the id's next attempt
    with the identical body. Refused when the ERP already reported the id —
    then an ``unknown`` row is promoted to ``sent`` instead — once the id's
    three attempts are spent, and without a payload."""
    row = await session.get(PushLog, push_log_id, populate_existing=True)
    if row is None or row.superseded_at is not None:
        return PushResult.refused(Refusal(12, "external_id"))
    if await session.get(Outcome, row.external_id) is not None:
        found = await outcomes.deliveries(session, [row.external_id])
        if row.external_id in found:
            await outcomes.promote(session, [found[row.external_id]])
        await session.commit()
        return PushResult.refused(Refusal(6, "reported"))
    if await last_attempt(session, row.external_id) >= push_gate.MAX_ATTEMPTS:
        await session.rollback()
        return PushResult.refused(Refusal(6, "attempts_spent"))
    if not isinstance(row.payload, dict):
        await session.rollback()
        return PushResult.refused(Refusal(11, "no_payload"))
    candidate_id = row.candidate_id
    await session.rollback()
    return await push_one(session, candidate_id, now=now, erp=erp, repush_of=push_log_id)


__all__ = [
    "BAND_HAJM",
    "ERP_KEY_HEADERS",
    "IDENTITY_KEYS",
    "JOB_LIMIT",
    "JOB_SCAN",
    "JS_SPACE",
    "JS_SPACE_RUN",
    "LANDED_NOTE_MAX",
    "MAY_HAVE_LANDED",
    "NOTE_MAX",
    "PUSH_PATH",
    "PUSH_PREVIEW",
    "PUSH_STATUS_UZ",
    "REJECTED_PAUSE",
    "RESERVED_BODY_KEYS",
    "RETRY_IN_ATTEMPT",
    "STOP_ERRORS",
    "TEXT_VOLUME_RE",
    "UTM_SOURCE",
    "Answer",
    "ErpBadBody",
    "JobReport",
    "NoPhone",
    "PayloadError",
    "PushCheck",
    "PushPayload",
    "PushResult",
    "ReconcileJobReport",
    "ball_parts",
    "band_of",
    "build_payload",
    "canonical_sha256",
    "china_signal",
    "cited",
    "clean",
    "clip_words",
    "collapse",
    "crm_push_job",
    "crm_push_slot",
    "due_candidates",
    "endpoint_for",
    "external_id_for",
    "fit_opener",
    "js_trim",
    "landed_note",
    "last_attempt",
    "push_one",
    "push_tekshir_confirm",
    "push_tekshir_view",
    "push_window_open",
    "reconcile_job",
    "reconcile_slot",
    "rejected_until",
    "render_note",
    "render_push_check",
    "resendable",
    "settle_crashed",
    "stated_m3",
    "text_volume",
    "url_line",
]
