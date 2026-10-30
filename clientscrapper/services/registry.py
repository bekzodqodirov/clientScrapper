"""Registry lookups: enrichment steps E4 (by STIR) and E4′ (by name), 04 §3.

**Hosts, in order** (04 §3 «Registry hosts», 07 §4.1 T-24 «orginfo first,
then stat/soliq/myorg»): ``orginfo.uz`` is the one built host — its pages
are recorded (``tests/fixtures/registries/README.md``: server-rendered, no
captcha, no login seen from the sandbox; re-check from the VPS with ``make
smoke`` before ``sources.registry_lookup.enabled``). ``registr.stat.uz``,
``my.soliq.uz`` and ``myorg.uz`` have no recorded page, so they are
configured-off adapters behind the same interface (``built = False``): they
answer ``not_built`` and fetch nothing until a task records their fixtures.
Tests drive the host chain with fakes.

**orginfo.uz as recorded.** A company page is keyed by an opaque 12-hex id,
not by the STIR, so E4 is two GETs: the «organizations» tab of the search
(``/ru/search/organizations/?q=<STIR>``) → one card → the company page
(``/ru/organization/<id>/``). E4′ reads the same search by name: 10 cards a
page (only the first is read), the match is fuzzy («xitoy» finds «XIDOY»),
so a card counts only when its name key is the company's and — when the
company knows its city — its address names the same city; two or more such
cards are ``ambiguous`` and give nothing (04 §3). The plan builds exactly
those two URL shapes (``search_url``, ``card_url``); a card link is followed
only when it is the company-page shape (``is_card_url``), so robots.txt's
``/api/``, the ``/pdf/`` route and the person-lookup tabs (``founders``,
``managers``, ``ips``, ``phones``) are never requested — and the Fetcher
still reads robots.txt before every request.

**The company page** is read from its labelled rows (``section[aria-label=
«Основные сведения…»]``: official and short name, registration date, status,
ИНН in ``#organizationTinValue``, OPF, ОКЭД), the contact block (phone, legal
address — the e-mail is not kept), the «Руководитель» row and the JSON-LD
``Organization`` (``taxID``, ``telephone``, ``employee``), and the page's
freshness note («актуальна на 15.06.2026», ``data_as_of``). The STIR is never
read from the «Похожие организации» cards at the foot of the page. The
founders are person data and are not read at all.

**The director** is the one person a registry hit may name (CLAUDE.md: at
most one decision maker per company, with ``legal_basis='public_registry'``,
``source_url`` = the page, ``collected_at``). A registry phone is often the
director's own mobile, so when the page names a director the phone goes on
the director's person-tier row (``owner='person'``; 04 §3 calls myorg's phone
company-tier — orginfo's is STILL UNVERIFIED either way, so the stricter
tier holds it); with no director it is company-tier.

**What a hit writes** (04 §3, §5.3): ``ent_legal_confirmed`` (only a registry
hit writes it), ``ent_age_2y_active`` when registered ≥ 730 days ago and
active, ``neg_liquidated`` on a liquidated one (the observation's
``liquidated`` type), and ``companies.registry_status / registered_on /
oked`` (the registry's word) and ``legal_form`` / ``address`` / ``city``
where the company has none. The pure halves are here; ``services/enrich``
fetches, stores the evidence and resolves the observation into the company.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, ClassVar, Final, Literal
from urllib.parse import quote, urlsplit

from clientscrapper.collectors.base import Observation
from clientscrapper.services import markup
from clientscrapper.services.http import (
    BudgetExhausted,
    Fetcher,
    FetchResult,
    HostBlocked,
    HostPaused,
    PageBudget,
    RobotsDisallowed,
    is_transport_failure,
)
from clientscrapper.services.keys import loose_key, name_key, stir_key
from clientscrapper.services.normalise import canonical_city, legal_form_in_name
from clientscrapper.services.signals import EvidenceRef, SignalDraft

SOURCE_KEY: Final = "registry_lookup"
# 04 §3 E4: ≤ 60 lookups a day across hosts, cache 90 d, a host that answers
# with a captcha or a 403 rests 24 h.
LOOKUPS_PER_DAY: Final = 60
CACHE_DAYS: Final = 90
HOST_PAUSE: Final = timedelta(hours=24)
# 04 §5.1 ent_age_2y_active: registered_on ≤ now − 730 d and active.
AGE_ACTIVE_DAYS: Final = 730
LEGAL_CONFIRMED: Final = "ent_legal_confirmed"
AGE_ACTIVE: Final = "ent_age_2y_active"
LIQUIDATED_TYPE: Final = "liquidated"

ORGINFO_HOST: Final = "orginfo.uz"
ORGINFO_BASE: Final = "https://orginfo.uz"
SEARCH_PATH: Final = "/ru/search/organizations/"
# orginfo's search form refuses fewer than three characters (minlength="3").
QUERY_MIN: Final = 3
_CARD_PATH: Final = re.compile(r"^/(?:ru|uz|en)/organization/([0-9a-f]{12})/$")
_DATE: Final = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
_ISO_DATE: Final = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_CODE: Final = re.compile(r"^\s*(\d{3,5})\s*-\s*(.*)$", re.S)
_OKED: Final = re.compile(r"^\d{5}$")
_AS_OF: Final = re.compile(r"актуальна\s+на\s+(\d{2}\.\d{2}\.\d{4})", re.I)
_BASIC_LABEL: Final = "Основные сведения об организации"
_CONTACT_LABEL: Final = "Контактные данные"
_MANAGEMENT_LABEL: Final = "Управленческая информация"
_ATTENTION_LABEL: Final = "Внимание"

Status = Literal["active", "inactive", "liquidated", "unknown"]
Result = Literal[
    "hit",
    "none",
    "ambiguous",
    "mismatch",
    "captcha",
    "blocked",
    "paused",
    "robots",
    "http_error",
    "unreachable",
    "not_built",
    "not_supported",
]
# Answers after which the next host is asked (the host could not say), and
# those that rest the host 24 h. ``unreachable`` is a request that got no
# answer at all (a timeout, DNS, a reset — ``http.is_transport_failure``):
# the host could not say, so it is no answer, and it rests nothing (it is
# asked again at the next attempt, 04 §3 «retried next attempt»).
UNANSWERED: Final[frozenset[str]] = frozenset(
    {
        "captcha",
        "blocked",
        "paused",
        "robots",
        "http_error",
        "unreachable",
        "not_built",
        "not_supported",
    }
)
RESTING: Final[frozenset[str]] = frozenset({"captcha", "blocked"})


# --- what a page says (pure) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryCard:
    """One card of a search list."""

    url: str
    stir: str | None
    name: str
    registered_on: date | None
    liquidated: bool
    address: str | None

    @property
    def city(self) -> str | None:
        return canonical_city(self.address)


@dataclass(frozen=True, slots=True)
class RegistryRecord:
    """One company page as the registry printed it."""

    host: str
    url: str
    stir: str
    official_name: str | None = None
    short_name: str | None = None
    status: Status = "unknown"
    status_text: str | None = None
    registered_on: date | None = None
    oked: str | None = None
    oked_text: str | None = None
    opf: str | None = None
    address: str | None = None
    phones: tuple[str, ...] = ()
    director_name: str | None = None
    director_role: str | None = None
    data_as_of: date | None = None
    ref: str | None = None  # the host's own id of the company (orginfo's hex id)

    @property
    def name(self) -> str | None:
        return self.official_name or self.short_name

    @property
    def city(self) -> str | None:
        return canonical_city(self.address)

    @property
    def legal_form(self) -> str | None:
        """The form the names carry (``LEGAL_FORM_RE``'s codes), never the
        OPF's words, which name no code of 02's list."""
        for name in (self.official_name, self.short_name):
            form = legal_form_in_name(name) if name else None
            if form is not None:
                return form
        return None


def status_of(text: str | None) -> Status:
    """A registry's status words → 02's four (Russian, Uzbek, English).
    «В процессе ликвидации» is not liquidated yet: inactive."""
    if not text:
        return "unknown"
    folded = text.strip().lower()
    if "процессе ликвид" in folded or "tugatish jarayon" in folded:
        return "inactive"
    if any(w in folded for w in ("ликвидир", "tugatilgan", "liquidated")):
        return "liquidated"
    if any(
        w in folded
        for w in (
            "неактив",
            "не актив",
            "недейств",
            "бездейств",
            "приостанов",
            "банкрот",
            "faol emas",
            "inactive",
        )
    ):
        return "inactive"
    if any(w in folded for w in ("актив", "действ", "faol", "active")):
        return "active"
    return "unknown"


def _date(text: str | None) -> date | None:
    if not text:
        return None
    iso = _ISO_DATE.match(text.strip())
    if iso is not None:
        y, m, d = (int(g) for g in iso.groups())
    else:
        found = _DATE.search(text)
        if found is None:
            return None
        d, m, y = (int(g) for g in found.groups())
    try:
        return date(y, m, d)
    except ValueError:
        return None


def search_url(query: str) -> str:
    """The «organizations» tab of orginfo's search (README: the tab lists
    organisations only; the person tabs are never asked)."""
    return f"{ORGINFO_BASE}{SEARCH_PATH}?q={quote(query.strip(), safe='')}"


def card_url(ref: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{12}", ref):
        raise ValueError("an orginfo id is 12 hex digits")
    return f"{ORGINFO_BASE}/ru/organization/{ref}/"


def card_ref(url: str) -> str | None:
    """The 12-hex id of a company-page URL on orginfo.uz, else None — the
    one shape the plan follows (never ``/pdf/``, ``/api/``, a person tab)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host not in (ORGINFO_HOST, f"www.{ORGINFO_HOST}") or parts.query:
        return None
    found = _CARD_PATH.match(parts.path)
    return found.group(1) if found is not None else None


def is_card_url(url: str) -> bool:
    return card_ref(url) is not None


def _absolute(href: str) -> str:
    return href if href.startswith("http") else f"{ORGINFO_BASE}{href}"


def parse_orginfo_search(body: bytes, *, url: str) -> list[RegistryCard]:
    """The cards of one search page (``a.og-card``), in page order. A card
    whose link is not the company-page shape is left out."""
    root = markup.parse(markup.decode(body))
    cards: list[RegistryCard] = []
    for link in root.find_all("a", cls="og-card"):
        target = _absolute(link.get("href") or "")
        if not is_card_url(target):
            continue
        badge = next(
            (s for s in link.find_all("span") if s.has_class("bg-success")), None
        )
        stir = stir_key(badge.text()) if badge is not None else None
        title = link.find("h6", cls="card-title")
        address_node = next(
            (p for p in link.find_all("p") if p.has_class("text-body-tertiary")), None
        )
        when = next(
            (
                s.text()
                for s in link.find_all("span")
                if s.find("img", alt="calendar") is not None
            ),
            None,
        )
        liquidated = any(
            s.has_class("bg-danger") and status_of(s.text()) == "liquidated"
            for s in link.find_all("span")
        )
        name = markup.collapse(title.text() if title is not None else None)
        if name is None:
            continue
        cards.append(
            RegistryCard(
                url=target,
                stir=stir,
                name=name,
                registered_on=_date(when),
                liquidated=liquidated,
                address=markup.collapse(
                    address_node.text() if address_node is not None else None
                ),
            )
        )
    return cards


def _rows(section: markup.Node | None) -> dict[str, markup.Node]:
    """A section's labelled rows: label text → the value column."""
    found: dict[str, markup.Node] = {}
    if section is None:
        return found
    for row in section.find_all("div", cls="row"):
        cols = [c for c in row.elements() if "col-6" in c.classes()]
        if len(cols) != 2:
            continue
        label = cols[0].text()
        if label and label not in found:
            found[label] = cols[1]
    return found


def _section(root: markup.Node, label: str) -> markup.Node | None:
    return root.find("section", aria_label=label)


def _organization(root: markup.Node) -> Mapping[str, Any]:
    for item in markup.json_ld(root):
        if not isinstance(item, Mapping):
            continue
        entity = item.get("mainEntity")
        if isinstance(entity, Mapping) and entity.get("@type") == "Organization":
            return entity
        if item.get("@type") == "Organization":
            return item
    return {}


def _str(value: Any) -> str | None:
    return markup.collapse(value) if isinstance(value, str) else None


def _code(node: markup.Node | None) -> tuple[str | None, str | None]:
    text = node.text() if node is not None else ""
    found = _CODE.match(text)
    if found is None:
        return None, markup.collapse(text)
    return found.group(1), markup.collapse(found.group(2))


def parse_orginfo_company(body: bytes, *, url: str) -> RegistryRecord | None:
    """One company page → its record, or None when the page carries no
    STIR (a 404 page, another page). The STIR comes from the ИНН row or the
    JSON-LD ``taxID`` — never from the «similar organisations» badges."""
    root = markup.parse(markup.decode(body))
    org = _organization(root)
    basic = _rows(_section(root, _BASIC_LABEL))
    tin = root.find("span", id="organizationTinValue")
    stir = stir_key(tin.get("content") if tin is not None else None) or stir_key(
        _str(org.get("taxID"))
    )
    if stir is None:
        return None
    status_node = basic.get("Статус")
    status_text = markup.collapse(status_node.text()) if status_node else None
    registered = basic.get("Дата регистрации")
    when: date | None = None
    if registered is not None:
        stamp = registered.find("time")
        when = _date(stamp.get("datetime") if stamp is not None else None) or _date(
            registered.text()
        )
    when = when or _date(_str(org.get("foundingDate")))
    oked, oked_text = _code(basic.get("ОКЭД"))
    opf_code, opf_text = _code(basic.get("ОПФ"))

    contacts = _rows(_section(root, _CONTACT_LABEL))
    phones: list[str] = []
    for value in contacts.values():
        for link in value.find_all("a"):
            href = link.get("href") or ""
            if href.lower().startswith("tel:"):
                phones.append(href[4:].strip())
    if not phones and (telephone := _str(org.get("telephone"))):
        phones.append(telephone)
    address_node = next((v for k, v in contacts.items() if "Адрес" in k), None)
    address = (
        markup.collapse(" ".join(address_node.lines()))
        if address_node is not None
        else None
    )
    if address is None and isinstance(org.get("address"), Mapping):
        parts = (
            org["address"].get("addressLocality"),
            org["address"].get("streetAddress"),
        )
        address = markup.collapse(" ".join(p for p in parts if isinstance(p, str)))

    management = _rows(_section(root, _MANAGEMENT_LABEL))
    head_label = next((k for k in management if "Руковод" in k), None)
    director = markup.collapse(management[head_label].text()) if head_label else None
    role = head_label
    employee = org.get("employee")
    if isinstance(employee, Mapping):
        director = director or _str(employee.get("name"))
        role = role or _str(employee.get("jobTitle"))

    attention = _section(root, _ATTENTION_LABEL)
    as_of = _AS_OF.search(attention.text()) if attention is not None else None

    official = basic.get("Официальное название организации")
    short = basic.get("Краткое название организации")
    return RegistryRecord(
        host=ORGINFO_HOST,
        url=url,
        stir=stir,
        official_name=markup.collapse(official.text())
        if official
        else _str(org.get("name")),
        short_name=markup.collapse(short.text())
        if short
        else _str(org.get("alternateName")),
        status=status_of(status_text),
        status_text=status_text,
        registered_on=when,
        oked=oked if oked and _OKED.match(oked) else None,
        oked_text=oked_text,
        opf=" - ".join(p for p in (opf_code, opf_text) if p) or None,
        address=address[:300] if address else None,
        phones=tuple(dict.fromkeys(p for p in phones if p)),
        director_name=director if director else None,
        director_role=role if director else None,
        data_as_of=_date(as_of.group(1)) if as_of is not None else None,
        ref=card_ref(url),
    )


# --- deciding (pure) -----------------------------------------------------------------


def _same_name(a: str, b: str) -> bool:
    ka, kb = name_key(a), name_key(b)
    return bool(ka) and (ka == kb or loose_key(ka) == loose_key(kb))


def name_agrees(
    card_name: str, company_name: str, *, card_city: str | None, city: str | None
) -> bool:
    """A card is the company's when its name is the company's (its name key,
    or the loose form of it) and — when the company knows its city — the
    card's address names the same one (E5's finder cards, which may carry
    no city). E4′ asks more: ``pick_by_name``."""
    if not _same_name(card_name, company_name):
        return False
    return city is None or card_city == city


def pick_by_name(
    cards: Sequence[RegistryCard], company_name: str, *, city: str | None
) -> tuple[Result, RegistryCard | None]:
    """The one card E4′ may follow (04 §3 «name + city must agree, else
    none»): a company that knows no city has nothing for a same-named firm
    elsewhere in the country to agree with → ``none``; a card with no STIR,
    or one the search list itself marks liquidated, is no candidate (an
    older firm under the same name — following it would exclude ours as
    liquidated); exactly one agreeing card → ``hit``; two or more →
    ``ambiguous``. A lookup by STIR (E4) is not this rule and keeps a
    liquidated firm's answer."""
    if city is None:
        return "none", None
    agreeing = [
        c
        for c in cards
        if c.stir is not None
        and not c.liquidated
        and name_agrees(c.name, company_name, card_city=c.city, city=city)
    ]
    if len({c.stir for c in agreeing}) > 1:
        return "ambiguous", None
    if not agreeing:
        return "none", None
    return "hit", agreeing[0]


def _excerpt(record: RegistryRecord) -> str:
    """What the card quotes for the registry facts: verbatim pieces of the
    page — the STIR, the status, the registration date, the OKED code. The
    names and the OKED words stay out of it: a firm called «Xitoy …» is no
    China origin, «Оптовая торговля …» no wholesale signal of its own."""
    parts = [f"ИНН {record.stir}"]
    if record.status_text:
        parts.append(record.status_text)
    if record.registered_on is not None:
        parts.append(record.registered_on.strftime("%d.%m.%Y"))
    if record.oked:
        parts.append(f"ОКЭД {record.oked}")
    return " · ".join(parts)


def excerpt_of(record: RegistryRecord) -> str:
    return _excerpt(record)


def observation_of(
    record: RegistryRecord, *, fetched_at: datetime, sha256: str
) -> Observation:
    """The registry hit as an 03 ``Observation`` (stored kind ``registry``,
    02 §2.5): the names, the STIR, the legal address, the phone — on the
    director's row when the page names one — and the director, the
    ``liquidated`` type for a liquidated firm, and the registry facts in
    ``attrs`` for the record."""
    attrs: dict[str, Any] = {
        "registry_host": record.host,
        "status": record.status,
        "registered_on": record.registered_on.isoformat()
        if record.registered_on
        else None,
        "oked": record.oked,
        "data_as_of": record.data_as_of.isoformat() if record.data_as_of else None,
    }
    if record.ref:
        attrs["registry_ref"] = record.ref
    return Observation(
        source=SOURCE_KEY,
        source_ref=f"{record.host}:{record.ref or record.stir}",
        url=record.url,
        fetched_at=fetched_at,
        raw_sha256=sha256,
        kind="company",
        company_name_raw=record.name,
        stir=record.stir,
        phones=list(record.phones),
        address=record.address,
        city=record.city,
        signal_types=[LIQUIDATED_TYPE] if record.status == "liquidated" else [],
        excerpt=_excerpt(record),
        person_name=record.director_name,
        person_role=record.director_role,
        attrs=attrs,
        lang="ru",
    )


def phones_to_person(parsed: dict[str, Any]) -> dict[str, Any]:
    """The stored observation with its phones on the director's row when it
    names one (module docstring: orginfo's phone is held person-tier)."""
    person = parsed.get("person")
    if isinstance(person, Mapping) and person.get("name"):
        for phone in parsed.get("phones") or ():
            if isinstance(phone, dict):
                phone["owner"] = "person"
    return parsed


def hit_drafts(
    record: RegistryRecord, evidence: EvidenceRef, *, now: datetime
) -> list[SignalDraft]:
    """The registry's own keys (04 §3): ``ent_legal_confirmed`` on every
    hit, ``ent_age_2y_active`` for an active firm registered 730 days ago or
    more. ``neg_liquidated`` comes through the observation's ``liquidated``
    type (rule 6)."""
    base = SignalDraft(
        key=LEGAL_CONFIRMED,
        evidence_id=evidence.id,
        observed_at=evidence.fetched_at,
        source_key=SOURCE_KEY,
        quote=f"ИНН {record.stir}",
        observation_id=evidence.observation_id,
    )
    drafts = [base]
    if (
        record.status == "active"
        and record.registered_on is not None
        and record.registered_on <= (now - timedelta(days=AGE_ACTIVE_DAYS)).date()
    ):
        drafts.append(
            SignalDraft(
                key=AGE_ACTIVE,
                evidence_id=evidence.id,
                observed_at=evidence.fetched_at,
                source_key=SOURCE_KEY,
                quote=record.registered_on.strftime("%d.%m.%Y"),
                observation_id=evidence.observation_id,
            )
        )
    return drafts


@dataclass(frozen=True, slots=True)
class CompanyNow:
    registry_status: str
    registered_on: date | None
    oked: str | None
    legal_form: str | None
    address: str | None
    city: str | None


def company_values(record: RegistryRecord, company: CompanyNow) -> dict[str, Any]:
    """The ``companies`` columns a hit sets: the registry's word on status,
    registration date and OKED (it is the source of those facts); the legal
    form, the address and the city only where the company has none (or the
    form is only «other»)."""
    values: dict[str, Any] = {}
    if record.status != "unknown" and record.status != company.registry_status:
        values["registry_status"] = record.status
    if record.registered_on is not None and record.registered_on != company.registered_on:
        values["registered_on"] = record.registered_on
    if record.oked and record.oked != (company.oked or "").strip():
        values["oked"] = record.oked
    form = record.legal_form
    if form is not None and company.legal_form in (None, "other"):
        values["legal_form"] = form
    if record.address and company.address is None:
        values["address"] = record.address
    if record.city and company.city is None:
        values["city"] = record.city
    return values


# --- the hosts ------------------------------------------------------------------------


@dataclass(slots=True)
class Answer:
    """One host's answer: what it found, and every page it fetched (each is
    stored as evidence, 04 §3 «evidence for every fetched page»)."""

    result: Result
    record: RegistryRecord | None = None
    pages: list[FetchResult] = field(default_factory=list)
    host: str = ""


class RegistryHost(ABC):
    """One registry host behind the shared interface (04 §3)."""

    key: ClassVar[str]
    host: ClassVar[str]
    # False: no recorded page, so nothing is fetched (``not_built``).
    built: ClassVar[bool] = True
    by_name_supported: ClassVar[bool] = True

    @abstractmethod
    async def by_stir(self, stir: str, *, fetcher: Fetcher, budget: PageBudget) -> Answer:
        """E4: the company page of ``stir``."""

    @abstractmethod
    async def by_name(
        self, name: str, *, city: str | None, fetcher: Fetcher, budget: PageBudget
    ) -> Answer:
        """E4′: the one company page whose name (and city) agree."""

    async def refresh(self, url: str, *, fetcher: Fetcher, budget: PageBudget) -> Answer:
        """A known company page read again (the cache ran out)."""
        return Answer("not_supported", host=self.host)


def _refusal(result: FetchResult) -> Result | None:
    if result.captcha:
        return "captcha"
    if result.status in (403, 429):
        return "blocked"
    if not 200 <= result.status < 300:
        return "http_error"
    return None


class Orginfo(RegistryHost):
    key = "orginfo"
    host = ORGINFO_HOST

    async def _get(
        self, url: str, answer: Answer, *, fetcher: Fetcher, budget: PageBudget
    ) -> FetchResult | Result:
        try:
            result = await fetcher.get(url, source=SOURCE_KEY, budget=budget)
        except HostBlocked:
            return "blocked"
        except HostPaused:
            return "paused"
        except RobotsDisallowed:
            return "robots"
        except Exception as exc:
            if is_transport_failure(exc):
                return "unreachable"
            raise
        refused = _refusal(result)
        if refused is not None:
            return refused
        answer.pages.append(result)
        return result

    async def _card(
        self,
        url: str,
        answer: Answer,
        *,
        stir: str | None,
        fetcher: Fetcher,
        budget: PageBudget,
    ) -> Answer:
        if not is_card_url(url):
            answer.result = "none"
            return answer
        got = await self._get(url, answer, fetcher=fetcher, budget=budget)
        if not isinstance(got, FetchResult):
            answer.result = got
            return answer
        record = parse_orginfo_company(got.body, url=url)
        if record is None:
            answer.result = "none"
        elif stir is not None and record.stir != stir:
            answer.result = "mismatch"
        else:
            answer.result, answer.record = "hit", record
        return answer

    async def by_stir(self, stir: str, *, fetcher: Fetcher, budget: PageBudget) -> Answer:
        answer = Answer("none", host=self.host)
        got = await self._get(search_url(stir), answer, fetcher=fetcher, budget=budget)
        if not isinstance(got, FetchResult):
            answer.result = got
            return answer
        cards = [c for c in parse_orginfo_search(got.body, url=got.url) if c.stir == stir]
        if not cards:
            return answer
        return await self._card(
            cards[0].url, answer, stir=stir, fetcher=fetcher, budget=budget
        )

    async def by_name(
        self, name: str, *, city: str | None, fetcher: Fetcher, budget: PageBudget
    ) -> Answer:
        answer = Answer("none", host=self.host)
        query = name_key(name)
        if len(query) < QUERY_MIN or city is None:
            # E4′ needs the city to agree (``pick_by_name``): nothing to ask.
            return answer
        got = await self._get(search_url(query), answer, fetcher=fetcher, budget=budget)
        if not isinstance(got, FetchResult):
            answer.result = got
            return answer
        picked, card = pick_by_name(
            parse_orginfo_search(got.body, url=got.url), name, city=city
        )
        if card is None:
            answer.result = picked
            return answer
        answer = await self._card(
            card.url, answer, stir=card.stir, fetcher=fetcher, budget=budget
        )
        if answer.record is not None and (
            (answer.record.name and not _same_name(answer.record.name, name))
            or (answer.record.city is not None and answer.record.city != city)
        ):
            # The card page names another firm, or another city, than the
            # search card did: not the one the rule agreed to.
            answer.result, answer.record = "mismatch", None
        return answer

    async def refresh(self, url: str, *, fetcher: Fetcher, budget: PageBudget) -> Answer:
        return await self._card(
            url, Answer("none", host=self.host), stir=None, fetcher=fetcher, budget=budget
        )


class _Unbuilt(RegistryHost):
    """A host 04 §3 names with no recorded page: configured off, fetches
    nothing (07 §4.1 T-24 — owed until its fixtures are recorded)."""

    built = False

    async def by_stir(self, stir: str, *, fetcher: Fetcher, budget: PageBudget) -> Answer:
        return Answer("not_built", host=self.host)

    async def by_name(
        self, name: str, *, city: str | None, fetcher: Fetcher, budget: PageBudget
    ) -> Answer:
        return Answer(
            "not_built" if self.by_name_supported else "not_supported", host=self.host
        )


class StatUz(_Unbuilt):
    """``registr.stat.uz`` EGRPO (per-INN form; a captcha is reported: never
    automated past one form post — on a captcha the manual ``/stir`` queue)."""

    key = "stat"
    host = "registr.stat.uz"


class MySoliq(_Unbuilt):
    """``my.soliq.uz`` VAT-payer check (per-INN; no name search)."""

    key = "soliq"
    host = "my.soliq.uz"
    by_name_supported = False


class MyOrg(_Unbuilt):
    """``myorg.uz`` public page (per-INN; sometimes a company phone)."""

    key = "myorg"
    host = "myorg.uz"


HOSTS: Final[tuple[RegistryHost, ...]] = (Orginfo(), StatUz(), MySoliq(), MyOrg())
# Which hosts are asked when ``sources.registry_lookup.config.hosts`` says
# nothing: the built one. An unbuilt host turned on answers ``not_built``.
DEFAULT_ON: Final[frozenset[str]] = frozenset({"orginfo"})


def hosts_on(
    config: Mapping[str, Any], hosts: Iterable[RegistryHost] = HOSTS
) -> list[RegistryHost]:
    """The hosts in order, each kept when ``config.hosts[key]`` says so
    (absent: ``DEFAULT_ON``)."""
    said = config.get("hosts")
    switches: Mapping[str, Any] = said if isinstance(said, Mapping) else {}

    def on(host: RegistryHost) -> bool:
        if host.key in switches:
            return switches[host.key] is True
        return host.key in DEFAULT_ON

    return [h for h in hosts if on(h)]


def paused_hosts(config: Mapping[str, Any], *, now: datetime) -> frozenset[str]:
    """The host keys resting after a captcha or a 403 (``config.paused_hosts``,
    an ISO time each)."""
    said = config.get("paused_hosts")
    found: set[str] = set()
    if isinstance(said, Mapping):
        for key, until in said.items():
            try:
                when = datetime.fromisoformat(until) if isinstance(until, str) else None
            except ValueError:
                when = None
            if when is not None and when.tzinfo is not None and when > now:
                found.add(str(key))
    return frozenset(found)


@dataclass(slots=True)
class Lookup:
    """The chain's answer over the hosts: the first host that could say
    (hit, none, ambiguous, mismatch), or ``registry_unavailable`` when none
    could; the hosts to rest, and every page fetched."""

    result: str
    record: RegistryRecord | None = None
    host: str | None = None
    pages: list[FetchResult] = field(default_factory=list)
    rest: list[str] = field(default_factory=list)
    tried: list[tuple[str, str]] = field(default_factory=list)


UNAVAILABLE: Final = "registry_unavailable"


async def lookup(
    hosts: Sequence[RegistryHost],
    *,
    stir: str | None,
    name: str | None,
    city: str | None,
    fetcher: Fetcher,
    budget: PageBudget,
    paused: frozenset[str] = frozenset(),
    refresh_url: str | None = None,
) -> Lookup:
    """E4 (``stir``) or E4′ (``name``), host by host in order: a resting host
    is skipped, a host that cannot say passes to the next one, a captcha or a
    403/429 rests that host (``rest``). ``refresh_url`` reads a known company
    page again on the host it belongs to. ``BudgetExhausted`` propagates (the
    day's pages are gone: the attempt stops its registry step)."""
    out = Lookup(UNAVAILABLE)
    for host in hosts:
        if host.key in paused:
            out.tried.append((host.key, "paused"))
            continue
        try:
            if refresh_url is not None and host.key == "orginfo":
                answer = await host.refresh(refresh_url, fetcher=fetcher, budget=budget)
            elif stir is not None:
                answer = await host.by_stir(stir, fetcher=fetcher, budget=budget)
            elif name is not None:
                answer = await host.by_name(
                    name, city=city, fetcher=fetcher, budget=budget
                )
            else:
                return out
        except BudgetExhausted:
            raise
        out.pages.extend(answer.pages)
        out.tried.append((host.key, answer.result))
        if answer.result in RESTING:
            out.rest.append(host.key)
        if answer.result in UNANSWERED:
            continue
        out.result, out.record, out.host = answer.result, answer.record, host.key
        return out
    return out


__all__ = [
    "AGE_ACTIVE",
    "CACHE_DAYS",
    "DEFAULT_ON",
    "HOSTS",
    "HOST_PAUSE",
    "LEGAL_CONFIRMED",
    "LOOKUPS_PER_DAY",
    "SOURCE_KEY",
    "UNAVAILABLE",
    "Answer",
    "CompanyNow",
    "Lookup",
    "MyOrg",
    "MySoliq",
    "Orginfo",
    "RegistryCard",
    "RegistryHost",
    "RegistryRecord",
    "StatUz",
    "card_ref",
    "card_url",
    "company_values",
    "excerpt_of",
    "hit_drafts",
    "hosts_on",
    "is_card_url",
    "lookup",
    "name_agrees",
    "observation_of",
    "parse_orginfo_company",
    "parse_orginfo_search",
    "paused_hosts",
    "phones_to_person",
    "pick_by_name",
    "search_url",
    "status_of",
]
