"""A fake fullprompt ERP for tests (05 §B.4): ``httpx.MockTransport`` over the
three routes the agent talks to.

    fake = FakeErp("test-secret")
    erp = ErpClient("http://erp.test", "test-secret", transport=fake.transport())

It copies the ERP on ``agent-feed`` (d138b3bc) rather than simplifying it:
the cursor codec, ``parseSince``, ``clampLimit`` (ECMAScript ``Number``), the
hash rule, the landing order of ``landOnce``, the per-family caps, the belt,
the outcomes row and the door's answers (200 / 400 / 404 / 503, the empty-body
500, the headers). When fullprompt changes, the captured fixtures in
``tests/fixtures/erp/`` and this file change together, and
``tests/test_erp_feeds_contract.py`` is the diff.

What it models, besides the explicit ``entries`` a test adds:

* **client cards** (``add_client``) — every phone of every card, active or
  not, yields the suppression tails of ``tailsSql`` (the whole value's last
  nine, each split part of nine digits or more, the 998-run and nine-digit
  chunks of a 16–120-digit part); the landing's client step reads only the
  ACTIVE cards;
* **leads** — created by a landing, or by ``add_lead``; ``move`` / ``win`` /
  ``revive`` / ``contact`` change them the way the ERP's own screens would; a
  lead that became a client is a ``client`` tail, one lost with a reason the
  ``no_contact_reasons`` setting lists a ``lost`` one;
* **intakes** (the ledger), keyed by the id as the ERP stores it
  (``storable_text``: NUL and lone surrogates → U+FFFD) — not modelled: the
  ``sha256:`` spelling of an id over 2,048 UTF-8 bytes (05 §B.4).

Modes. ``modes[external_id]`` (POST) and ``feed_modes[path]`` (GET) are a
string — every request of that id/path, except the one-shot ``orphan`` and
``throw200_orphan`` — or a list consumed one per request (then
``default_mode``). A mode may carry a parameter after a colon
(``nonjson5xx:502`` is a proxy's HTML page).
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import math
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

# --- the ERP's constants (agent-feed.ts, inbound.ts) ---------------------------------

HASH_RULE = "hmac-sha256(secret, last9digits) hex[0:20]"
FEED_LIMIT_DEFAULT = 1000
FEED_LIMIT_MAX = 5000
PHONE_CAP_PER_DAY = 3
SOURCE_CAP_PER_DAY = 200
CAP_WINDOW = timedelta(hours=24)
JOIN_WINDOW = timedelta(days=30)
# postgres `now() - interval '1 year'`
OUTCOMES_WINDOW = timedelta(days=365)
NAME_MAX = 120
NOTE_MAX = 1000
COMPANY_MAX = 200
LOST_REASON_MAX = 200
GZIP_MIN = 1024
INBOUND_SOURCE_KEYS = (
    "meta",
    "instagram",
    "facebook",
    "telegram",
    "tiktok",
    "google",
    "sayt",
    "agent",
    "other",
)
AGENT = "agent"
OTHER = "other"
SUPPRESSION_PATH = "/api/agent/suppression"
OUTCOMES_PATH = "/api/agent/outcomes"
LEADS_IN = "/api/leads/in/"
# fromPlain's identity keys and the secret's names (inbound-webhook.ts).
SECRET_BODY_KEYS = ("google_key", "key", "secret", "token")
KNOWN_BODY_KEYS = frozenset(
    {"name", "ism", "phone", "telefon", "note", "izoh", "id", "external_id"}
    | set(SECRET_BODY_KEYS)
)
STAGE_NAMES = {"open": "Yangi", "won": "Sotuv", "lost": "Yo‘qotildi"}
ONE_SHOT = frozenset({"orphan", "throw200_orphan"})
OTHER_ONLY = frozenset({"client", "throw200", "throw200_orphan"})

StageKind = Literal["open", "won", "lost"]

# --- JavaScript, spelled out --------------------------------------------------------

# JavaScript's \s (= String.prototype.trim()'s set), written out (05 §B.1).
JS_SPACE = "[\t\n\v\f\r    -     　﻿]"
_JS_SPACE_RUN = re.compile(JS_SPACE + "+")
_JS_EDGES = re.compile(f"^{JS_SPACE}+|{JS_SPACE}+$")


def js_trim(s: str) -> str:
    return _JS_EDGES.sub("", s)


def js_str(value: Any) -> str | None:
    """``str()`` of inbound-webhook.ts: a string ``trim()`` leaves non-empty
    (trimmed), a number as JavaScript prints it, anything else nothing."""
    if isinstance(value, str):
        trimmed = js_trim(value)
        return trimmed or None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return js_number_text(value)
    return None


def js_number_text(x: float) -> str:
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x.is_integer() and abs(x) < 1e21:
        return str(int(x))
    text = repr(x)
    if "e" in text:
        mantissa, exponent = text.split("e")
        sign = "-" if exponent.startswith("-") else "+"
        text = f"{mantissa}e{sign}{exponent.lstrip('+-').lstrip('0') or '0'}"
    return text


def storable_text(text: str) -> str:
    """clip.ts ``storableText``: every lone surrogate and NUL → U+FFFD. A
    Python string from ``json.loads`` holds a lone half as its own code point
    (a pair arrives already joined)."""
    return "".join(
        "�" if ch == "\x00" or 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text
    )


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def clip_units(text: str, max_units: int) -> str:
    """clip.ts ``clipUnits``: at most ``max_units`` UTF-16 units of the
    storable text, an emoji that does not fit left out whole."""
    out: list[str] = []
    used = 0
    for ch in storable_text(text):
        units = 2 if ord(ch) > 0xFFFF else 1
        if used + units > max(0, max_units):
            break
        out.append(ch)
        used += units
    return "".join(out)


def trim_field(value: str | None, max_units: int) -> str | None:
    """inbound.ts ``trim``: whitespace runs collapsed, trimmed, clipped."""
    collapsed = js_trim(_JS_SPACE_RUN.sub(" ", value or ""))
    return clip_units(collapsed, max_units) or None


_HEX = re.compile(r"^0[xX][0-9a-fA-F]+$")
_OCT = re.compile(r"^0[oO][0-7]+$")
_BIN = re.compile(r"^0[bB][01]+$")
_DECIMAL = re.compile(r"^[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)$")


def js_number(raw: str | None) -> float:
    """ECMAScript ``StringToNumber`` — what ``Number(raw)`` does. ``null`` is 0
    (``q.get('limit')`` of an absent parameter). Never Python's ``float()``:
    it reads ``1_000``, ``infinity`` and ``nan``, which JavaScript refuses."""
    if raw is None:
        return 0.0
    text = js_trim(raw)
    if text == "":
        return 0.0
    if text in ("Infinity", "+Infinity"):
        return math.inf
    if text == "-Infinity":
        return -math.inf
    for pattern, base in ((_HEX, 16), (_OCT, 8), (_BIN, 2)):
        if pattern.match(text):
            return float(int(text[2:], base))
    if _DECIMAL.match(text):
        return float(text)
    return math.nan


def clamp_limit(raw: str | None) -> int:
    """``Math.min(5000, Math.max(1, Math.floor(Number(raw)) || 1000))``."""
    x = js_number(raw)
    if math.isnan(x):
        n = math.nan
    elif math.isinf(x):
        n = x
    else:
        n = float(math.floor(x))
    if math.isnan(n) or n == 0:
        n = FEED_LIMIT_DEFAULT
    return int(min(FEED_LIMIT_MAX, max(1, n)))


# --- the codec (a copy of agent-feed.ts) ----------------------------------------------

CURSOR_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_N = re.compile(r"^\d{1,6}$")
_ISO_INSTANT = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(Z|[+-]\d{2}:?\d{2})$"
)
_B64_NOISE = re.compile(r"[^A-Za-z0-9_-]")


def encode_cursor(at_us: str, id_: str, n: int) -> str:
    """base64url, no padding, of ``<at>|<id>|<n>``."""
    return base64.urlsafe_b64encode(f"{at_us}|{id_}|{n}".encode()).decode().rstrip("=")


def _b64url_decode(raw: str) -> str:
    # Node's Buffer.from(raw, 'base64url') skips what is not in the alphabet
    # and reads '+' '/' as '-' '_'; toString('utf8') replaces bad bytes.
    text = _B64_NOISE.sub("", raw.replace("+", "-").replace("/", "_"))
    text = text[: len(text) - (len(text) % 4 == 1)]
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _real_instant(at: str) -> bool:
    """``new Date(ms).toISOString().slice(0, 19) === at.slice(0, 19)``: a date
    and a time that survive a calendar round trip (02-30, T24:00, :60 do not)."""
    try:
        datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return True


def decode_cursor(raw: str | None) -> tuple[str, str, int] | None | Literal["invalid"]:
    """None = absent (an empty ``cursor=`` too); 'invalid' = present and unreadable."""
    if not raw:
        return None
    try:
        parts = _b64url_decode(raw).split("|")
    except ValueError:
        return "invalid"
    if len(parts) not in (2, 3):
        return "invalid"
    at, id_ = parts[0], parts[1]
    n = parts[2] if len(parts) == 3 else "0"
    if not CURSOR_AT.match(at) or not _UUID.match(id_) or not _N.match(n):
        return "invalid"
    if not _real_instant(at):
        return "invalid"
    return at, id_.lower(), int(n)


def parse_since(raw: str | None) -> str | None | Literal["invalid"]:
    """``parseSince``: a full ISO 8601 instant with a zone → UTC with six
    fraction digits; None = absent (an empty ``since=`` too)."""
    if not raw:
        return None
    m = _ISO_INSTANT.match(raw)
    if not m:
        return "invalid"
    y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
    sec = int(m.group(6) or 0)
    frac = m.group(7) or ""
    zone = m.group(8)
    if h > 23 or mi > 59 or sec > 59:
        return "invalid"
    try:
        local = datetime(y, mo, d, h, mi, sec)
    except ValueError:
        return "invalid"
    offset = timedelta(0)
    if zone != "Z":
        digits = zone[1:].replace(":", "")
        oh, om = int(digits[:2]), int(digits[2:])
        # Date.parse is NaN for an offset hour of 24+ or a minute of 60+.
        if oh > 23 or om > 59:
            return "invalid"
        offset = timedelta(hours=oh, minutes=om) * (1 if zone[0] == "+" else -1)
    utc = local - offset
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{frac.ljust(6, '0')[:6]}Z"


def instant(at_us: str) -> datetime:
    return datetime.strptime(at_us, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def us_text(at: datetime) -> str:
    """The instant as postgres prints it in the cursor: µs and ``Z``."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def ms_text(at: datetime) -> str:
    """``new Date(…).toISOString()``: milliseconds (cut, never rounded) and ``Z``."""
    at = at.astimezone(UTC)
    return f"{at:%Y-%m-%dT%H:%M:%S}.{at.microsecond // 1000:03d}Z"


def hash_tail(secret: str, tail9: str) -> str:
    return hmac.new(secret.encode(), tail9.encode(), hashlib.sha256).hexdigest()[:20]


def salt_id_for(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def normalise_reason(text: str | None) -> str:
    """reason-list.ts: JS whitespace runs → one space, trimmed, lower case."""
    return js_trim(_JS_SPACE_RUN.sub(" ", text or "")).strip(" ").lower()


# --- tailsSql -----------------------------------------------------------------------

_PHONE_SPLIT = re.compile(r"[,;/|\\\r\n]+|[\t ]{2,}|[\t ]+(?=\+)")
_NOT_DIGIT = re.compile(r"[^0-9]")
_RUN_998 = re.compile(r"^(?:998[0-9]{9})+$")


def tails_of(value: str) -> list[str]:
    """Every tail one phones value answers to, in its order (agent-feed.ts
    ``tailsSql``), DISTINCT, the empty ones left out."""
    found: list[str] = []

    def add(tail: str) -> None:
        if tail and tail not in found:
            found.append(tail)

    add(_NOT_DIGIT.sub("", value)[-9:])
    for part in _PHONE_SPLIT.split(value):
        digits = _NOT_DIGIT.sub("", part)
        if not digits:
            continue
        if len(digits) >= 9:
            add(digits[-9:])
        if 15 < len(digits) <= 120:
            if _RUN_998.match(digits):
                for i in range(len(digits) // 12):
                    add(digits[i * 12 + 3 : i * 12 + 12])
            elif len(digits) % 9 == 0:
                for i in range(len(digits) // 9):
                    add(digits[i * 9 : i * 9 + 9])
    return found


def phones_match(value: str, tail9: str) -> bool:
    """The app's ``phonesMatch`` as the belt and the client step read it: the
    whole value's last nine, or a whole value of seven or eight digits that
    ends the arrival's number."""
    digits = _NOT_DIGIT.sub("", value)
    if len(digits) >= 9:
        return digits[-9:] == tail9
    return len(digits) in (7, 8) and tail9.endswith(digits)


# --- state --------------------------------------------------------------------------


@dataclass(slots=True)
class FakeClient:
    id: str
    phones: list[str]
    active: bool
    created_at: datetime
    updated_at: datetime
    code: str = ""


@dataclass(slots=True)
class FakeLead:
    id: str
    phone: str | None
    name: str | None
    source: str
    created_at: datetime
    updated_at: datetime
    stage_kind: StageKind = "open"
    stage_name: str = STAGE_NAMES["open"]
    lost_reason: str | None = None
    closed_at: datetime | None = None
    client_id: str | None = None
    company: str | None = None
    quoted_volume_m3: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FakeIntake:
    id: str
    external_id: str | None
    source: str
    outcome: str
    reason: str | None
    phone: str | None
    name: str | None
    created_at: datetime
    ref: dict[str, Any] | None
    lead_id: str | None = None
    client_id: str | None = None
    contacted_at: datetime | None = None
    contact_kind: str | None = None
    channel: str = "webhook"


@dataclass(slots=True)
class Entry:
    """One row of the derived suppression table ``(tail, kind, at, id, n)``."""

    tail: str
    kind: str
    at: datetime
    id: str
    n: int


@dataclass(slots=True)
class Got:
    """One GET the fake answered: what the agent sent."""

    path: str
    params: dict[str, str]
    headers: dict[str, str]


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


def _etag(body: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha1(body).digest()).decode().rstrip("=")
    return f'W/"{digest}"'


def _not_found() -> httpx.Response:
    return httpx.Response(
        404, headers={"content-type": "text/plain;charset=UTF-8"}, content=b"Not found"
    )


def _post_json(status: int, data: Any) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json", "cache-control": "no-store"},
        content=_json_bytes(data),
    )


class FakeErp:
    def __init__(
        self,
        secret: str | None,
        default_mode: str = "ok",
        *,
        other_secret: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.secrets: dict[str, str | None] = {AGENT: secret, OTHER: other_secret}
        self.default_mode = default_mode
        self.clock = clock or (lambda: datetime.now(UTC))
        self.posted: list[tuple[str, dict[str, str], Any]] = []
        self.requests: list[httpx.Request] = []
        self.gets: list[Got] = []
        self.intakes: dict[str, FakeIntake] = {}
        self.leads: dict[str, FakeLead] = {}
        self.clients: dict[str, FakeClient] = {}
        self.entries: list[Entry] = []
        self.outcome_overrides: dict[str, dict[str, Any]] = {}
        self.modes: dict[str, str | list[str]] = {}
        self.feed_modes: dict[str, str | list[str]] = {}
        self.held: dict[str, asyncio.Event] = {}
        self.dictionary: list[str] = []
        self.no_contact_reasons: list[str] = ["Qayta qilinmasin"]
        self._chains: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- keys --------------------------------------------------------------------------

    @property
    def secret(self) -> str | None:
        return self.secrets[AGENT]

    @property
    def salt_id(self) -> str:
        assert self.secret is not None
        return salt_id_for(self.secret)

    def rotate_secret(self, new: str | None) -> None:
        """The owner re-mints the agent key: the old one 404s at once."""
        self.secrets[AGENT] = new

    # -- transports --------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def timeout_transport(self, timeout: float) -> TimeoutTransport:
        """A client-side read timeout over this fake (mode ``slow``)."""
        return TimeoutTransport(self, timeout)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.startswith(LEADS_IN):
            return await self._post(request, path[len(LEADS_IN) :])
        if request.method == "GET" and path in (SUPPRESSION_PATH, OUTCOMES_PATH):
            return self._get(request, path)
        # A path no route handles is Next's own 404 page, not the routes'
        # text/plain (verify:final): the behaviour (push_404) is the same.
        return httpx.Response(
            404,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<!DOCTYPE html><html><body>404: Not Found</body></html>",
        )

    # -- state helpers -----------------------------------------------------------------

    def add_client(
        self,
        phones: Iterable[str],
        *,
        active: bool = True,
        id: str | None = None,
        at: datetime | None = None,
        code: str = "",
    ) -> FakeClient:
        when = at or self.clock()
        card = FakeClient(
            id=id or str(uuid.uuid4()),
            phones=list(phones),
            active=active,
            created_at=when,
            updated_at=when,
            code=code,
        )
        self.clients[card.id] = card
        return card

    def edit_client(
        self,
        client_id: str,
        *,
        phones: Iterable[str] | None = None,
        at: datetime | None = None,
    ) -> None:
        card = self.clients[client_id]
        if phones is not None:
            card.phones = list(phones)
        card.updated_at = at or self.clock()

    def add_lead(
        self,
        phone: str | None,
        *,
        name: str | None = "Namuna lid",
        source: str = "other",
        stage_kind: StageKind = "open",
        lost_reason: str | None = None,
        at: datetime | None = None,
        id: str | None = None,
    ) -> FakeLead:
        when = at or self.clock()
        lead = FakeLead(
            id=id or str(uuid.uuid4()),
            phone=phone,
            name=name,
            source=source,
            created_at=when,
            updated_at=when,
        )
        self.leads[lead.id] = lead
        if stage_kind != "open":
            self.move(lead.id, stage_kind, reason=lost_reason, at=when)
        return lead

    def move(
        self,
        lead_id: str,
        stage_kind: StageKind,
        *,
        reason: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """A stage move (``moveLead``): a decision stamps ``closed_at``; a
        revival clears it and the reason."""
        lead = self.leads[lead_id]
        when = at or self.clock()
        lead.stage_kind = stage_kind
        lead.stage_name = STAGE_NAMES[stage_kind]
        lead.lost_reason = reason if stage_kind == "lost" else None
        lead.closed_at = when if stage_kind != "open" else None
        lead.updated_at = when

    def win(
        self, lead_id: str, *, at: datetime | None = None, code: str = ""
    ) -> FakeClient:
        """``winLead``: a card minted with the lead's phone, the lead won."""
        lead = self.leads[lead_id]
        when = at or self.clock()
        card = self.add_client([lead.phone or ""], at=when, code=code)
        lead.client_id = card.id
        self.move(lead_id, "won", at=when)
        return card

    def revive(self, lead_id: str, *, at: datetime | None = None) -> None:
        self.move(lead_id, "open", at=at)

    def contact(
        self, external_id: str, kind: str = "note", *, at: datetime | None = None
    ) -> None:
        """``stampFirstContacts`` found evidence: the arrival is contacted."""
        intake = self.intakes[storable_text(external_id)]
        intake.contacted_at = at or self.clock()
        intake.contact_kind = kind

    def add_entry(
        self,
        tail: str,
        kind: str = "client",
        *,
        at: datetime | None = None,
        id: str | None = None,
        n: int = 1,
    ) -> Entry:
        """An explicit row of the suppression table, beside the derived ones."""
        entry = Entry(tail, kind, at or self.clock(), id or str(uuid.uuid4()), n)
        self.entries.append(entry)
        return entry

    def release(self, external_id: str) -> None:
        """Let a held (``slow``) delivery finish."""
        self.held.setdefault(storable_text(external_id), asyncio.Event()).set()

    async def drain(self) -> None:
        """Wait for every delivery still running in the background."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def lead_of(self, external_id: str) -> FakeLead | None:
        intake = self.intakes.get(storable_text(external_id))
        if intake is None or intake.lead_id is None:
            return None
        return self.leads[intake.lead_id]

    # -- the suppression table (suppressionTailsSql) -----------------------------------

    def _labels(self) -> set[str]:
        return {
            normalise_reason(r)
            for r in self.no_contact_reasons
            if len(normalise_reason(r)) >= 2
        }

    def _dictionary(self) -> set[str]:
        return {normalise_reason(r) for r in self.dictionary}

    def table(self) -> list[Entry]:
        rows: list[Entry] = []
        for card in self.clients.values():
            at = max(card.created_at, card.updated_at)
            seen: list[str] = []
            for phone in card.phones:
                for tail in tails_of(phone):
                    if tail not in seen:
                        seen.append(tail)
            rows += [Entry(t, "client", at, card.id, i + 1) for i, t in enumerate(seen)]
        labels = self._labels()
        for lead in self.leads.values():
            if not lead.phone:
                continue
            if lead.client_id is not None or lead.stage_kind == "won":
                kind = "client"
            elif (
                lead.stage_kind == "lost" and normalise_reason(lead.lost_reason) in labels
            ):
                kind = "lost"
            else:
                continue
            at = max(lead.closed_at or lead.updated_at, lead.updated_at)
            rows += [
                Entry(t, kind, at, lead.id, i + 1)
                for i, t in enumerate(tails_of(lead.phone))
            ]
        return rows + list(self.entries)

    def belt(self, tail9: str) -> bool:
        """``isSuppressedTail``: a tail equal to the arrival's nine, or a whole
        value of seven or eight digits that ends it."""
        for row in self.table():
            if row.tail == tail9:
                return True
            if len(row.tail) in (7, 8) and tail9.endswith(row.tail):
                return True
        return False

    # -- POST /api/leads/in/{source} ------------------------------------------

    def _mode(self, key: str, table: dict[str, str | list[str]]) -> str:
        mode = table.get(key)
        if mode is None:
            return self.default_mode
        if isinstance(mode, list):
            return mode.pop(0) if mode else self.default_mode
        if mode.split(":", 1)[0] in ONE_SHOT:
            del table[key]
        return mode

    async def _post(self, request: httpx.Request, source: str) -> httpx.Response:
        if source not in INBOUND_SOURCE_KEYS:
            return _not_found()
        secret = self.secrets.get(source)
        if not secret:
            return _not_found()
        try:
            body = json.loads(request.content)
        except ValueError:
            return _not_found()
        header = js_trim(request.headers.get("x-gsr-key", ""))
        sent = header or js_str(_pick(body, "google_key")) or js_str(_pick(body, "key"))
        if not sent or sent != secret:
            return _not_found()
        self.posted.append((source, dict(request.headers), body))

        fields = from_plain(body)
        eid = storable_text(fields.external_id) if fields.external_id else None
        mode = self._mode(eid, self.modes) if eid else self.default_mode
        name, _, arg = mode.partition(":")
        if name in OTHER_ONLY and source != OTHER:
            name = "ok"
        if eid is None:
            return await self._answer(source, body, fields, None, name, arg)
        lock = self._chains.setdefault(eid, asyncio.Lock())
        async with lock:
            return await self._answer(source, body, fields, eid, name, arg)

    async def _answer(
        self,
        source: str,
        body: Any,
        fields: PlainFields,
        eid: str | None,
        mode: str,
        arg: str,
    ) -> httpx.Response:
        if mode == "404":
            return _not_found()
        if mode == "503":
            return _post_json(503, {"error": "unavailable"})
        if mode == "3xx":
            return httpx.Response(
                int(arg or 308),
                headers={"location": "https://erp.test/api/leads/in/" + source},
            )
        if mode == "nonjson5xx":
            status = int(arg or 500)
            if status == 500:
                return httpx.Response(500, content=b"")
            return httpx.Response(
                status,
                headers={"content-type": "text/html"},
                content=b"<html><body><h1>502 Bad Gateway</h1></body></html>",
            )
        if mode == "throw200":
            return _post_json(200, {"ok": True})
        if mode == "orphan":
            # A throw after the lead's INSERT: a lead, no ledger row, a 503.
            self._create_lead(source, fields)
            return _post_json(503, {"error": "unavailable"})
        if mode == "throw200_orphan":
            # The same on the fallback door, which still answers 200.
            self._create_lead(source, fields)
            return _post_json(200, {"ok": True})
        if mode == "slow" and eid is not None:
            await self.held.setdefault(eid, asyncio.Event()).wait()
            mode = "ok"
        self._land(source, body, fields, eid, mode)
        if mode == "503_committed":
            return _post_json(503, {"error": "unavailable"})
        if mode == "badjson":
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html>"
            )
        return _post_json(200, {"ok": True})

    def _family_count(self, source: str, phone: str, now: datetime) -> int:
        agent = source == AGENT
        return sum(
            1
            for i in self.intakes.values()
            if i.phone == phone
            and now - i.created_at < CAP_WINDOW
            and (i.source == AGENT) == agent
        )

    def _source_count(self, source: str, now: datetime) -> int:
        return sum(
            1
            for i in self.intakes.values()
            if i.source == source and now - i.created_at < CAP_WINDOW
        )

    def _record(
        self,
        source: str,
        body: Any,
        fields: PlainFields,
        eid: str | None,
        outcome: str,
        reason: str | None = None,
        *,
        lead_id: str | None = None,
        client_id: str | None = None,
    ) -> FakeIntake | None:
        key = eid if eid is not None else f"_noid:{uuid.uuid4()}"
        if key in self.intakes:
            # ON CONFLICT DO NOTHING
            return None
        intake = FakeIntake(
            id=str(uuid.uuid4()),
            external_id=eid,
            source=source,
            outcome=outcome,
            reason=reason,
            phone=fields.phone_digits,
            name=fields.name,
            created_at=self.clock(),
            ref=_ref_without_secret(body),
            lead_id=lead_id,
            client_id=client_id,
        )
        self.intakes[key] = intake
        return intake

    def _create_lead(self, source: str, fields: PlainFields) -> FakeLead:
        when = self.clock()
        lead = FakeLead(
            id=str(uuid.uuid4()),
            phone=fields.phone_digits,
            name=fields.name or f"{source} · {fields.phone_digits or ''}".strip(),
            source=source,
            created_at=when,
            updated_at=when,
            company=trim_field(fields.company, COMPANY_MAX) if source == AGENT else None,
        )
        lead.notes.append(_inbound_note(source, fields.note))
        self.leads[lead.id] = lead
        return lead

    def _land(
        self, source: str, body: Any, fields: PlainFields, eid: str | None, mode: str
    ) -> None:
        """``landOnce``, in its order (05 §A.3); then the mode."""
        now = self.clock()
        phone = fields.phone_digits
        if not phone and not fields.name:
            self._record(source, body, fields, eid, "dropped", "no_contact")
            return
        if eid is not None and eid in self.intakes:
            return  # replay: nothing lands
        if source == AGENT and not phone:
            self._record(source, body, fields, eid, "dropped", "no_phone")
            return
        if (phone and self._family_count(source, phone, now) >= PHONE_CAP_PER_DAY) or (
            self._source_count(source, now) >= SOURCE_CAP_PER_DAY
        ):
            self._record(source, body, fields, eid, "dropped", "capped")
            return
        if source == AGENT and phone and self.belt(phone):
            self._record(source, body, fields, eid, "dropped", "suppressed")
            return
        if mode in ("capped", "suppressed", "no_phone"):
            self._record(source, body, fields, eid, "dropped", mode)
            return
        if mode == "client":
            card = next(iter(self.clients.values()), None) or self.add_client(
                [phone or ""]
            )
            self._record(source, body, fields, eid, "client", client_id=card.id)
            return
        if phone:
            known = [
                c
                for c in self.clients.values()
                if c.active and any(phones_match(p, phone) for p in c.phones)
            ]
            if len(known) == 1:
                self._record(source, body, fields, eid, "client", client_id=known[0].id)
                return
        if mode == "joined":
            prior = self._open_leads(phone, now)
            lead = prior[0] if prior else self.add_lead(phone, name="Namuna oldingi lid")
            self._join(lead, source, body, fields, eid, now)
            return
        if phone:
            prior = self._open_leads(phone, now)
            if len(prior) == 1:
                self._join(prior[0], source, body, fields, eid, now)
                return
        lead = self._create_lead(source, fields)
        self._record(source, body, fields, eid, "created", lead_id=lead.id)

    def _open_leads(self, phone: str | None, now: datetime) -> list[FakeLead]:
        if not phone:
            return []
        return [
            lead
            for lead in self.leads.values()
            if lead.phone == phone
            and lead.stage_kind == "open"
            and now - lead.updated_at <= JOIN_WINDOW
        ]

    def _join(
        self,
        lead: FakeLead,
        source: str,
        body: Any,
        fields: PlainFields,
        eid: str | None,
        now: datetime,
    ) -> None:
        lead.notes.append(_inbound_note(source, fields.note))
        lead.updated_at = now
        self._record(source, body, fields, eid, "joined", lead_id=lead.id)

    # -- GET /api/agent/suppression, /api/agent/outcomes ---------------------------------

    def _get(self, request: httpx.Request, path: str) -> httpx.Response:
        params = dict(request.url.params)
        self.gets.append(Got(path, params, dict(request.headers)))
        sent = request.headers.get("x-gsr-key")
        if not self.secret or not sent or sent != self.secret:
            return _not_found()
        mode, _, arg = self._mode(path, self.feed_modes).partition(":")
        if mode == "feed_503":
            return self._feed_json(request, 503, {"error": "unavailable"})
        if mode == "feed_500":
            return httpx.Response(500, content=b"")
        if mode == "feed_502":
            return httpx.Response(
                502, headers={"content-type": "text/html"}, content=b"<html>502</html>"
            )
        if mode == "feed_timeout":
            raise httpx.ReadTimeout("fake: the feed did not answer", request=request)
        if mode == "feed_connect":
            raise httpx.ConnectError("fake: connection refused", request=request)
        if mode == "feed_404":
            return _not_found()
        if mode == "feed_3xx":
            return httpx.Response(
                int(arg or 308), headers={"location": "https://erp.test" + path}
            )
        if mode == "feed_badjson":
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html>"
            )
        q = request.url.params
        cursor = decode_cursor(q.get("cursor"))
        since = parse_since(q.get("since"))
        if cursor == "invalid" or since == "invalid":
            code = "bad_cursor" if cursor == "invalid" else "bad_since"
            return self._feed_json(request, 400, {"error": code})
        limit = clamp_limit(q.get("limit"))
        if path == SUPPRESSION_PATH:
            body = self._suppression_body(since, cursor, limit)
        else:
            body = self._outcomes_body(since, cursor, limit)
        items = "entries" if path == SUPPRESSION_PATH else "rows"
        if mode == "feed_rule_changed" and path == SUPPRESSION_PATH:
            body["rule"] = "hmac-sha512(secret, last9digits) hex[0:20]"
        if mode == "feed_shape" and body[items]:
            body[items][0]["phone"] = "+998 90 000 00 01"
        if mode == "feed_missing_key":
            body.pop(arg or "resume", None)
        if mode == "feed_same_cursor":
            body["next"] = q.get("cursor") or body["resume"]
        return self._feed_json(request, 200, body)

    def _feed_json(
        self, request: httpx.Request, status: int, data: Any
    ) -> httpx.Response:
        content = _json_bytes(data)
        headers = {
            "content-type": "application/json",
            "cache-control": "private, no-store",
            "etag": _etag(content),
            "vary": "Accept-Encoding",
        }
        wanted = request.headers.get("accept-encoding", "")
        gz = any(
            part.strip().lower() == "gzip" or part.strip().lower().startswith("gzip;")
            for part in wanted.split(",")
        )
        if len(content) >= GZIP_MIN and gz:
            content = gzip.compress(content)
            headers["content-encoding"] = "gzip"
        headers["content-length"] = str(len(content))
        return httpx.Response(status, headers=headers, content=content)

    def _suppression_body(
        self, since: str | None, cursor: tuple[str, str, int] | None, limit: int
    ) -> dict[str, Any]:
        assert self.secret is not None
        rows = [r for r in self.table() if len(r.tail) == 9]
        if since is not None:
            rows = [r for r in rows if r.at > instant(since)]
        if cursor is not None:
            c_at = instant(_pad_us(cursor[0]))
            rows = [r for r in rows if (r.at, r.id, r.n) > (c_at, cursor[1], cursor[2])]
        rows.sort(key=lambda r: (r.at, r.id, r.n))
        page = rows[:limit]
        last = page[-1] if page else None
        return {
            "mode": "delta" if since else "full",
            "generated_at": ms_text(self.clock()),
            "salt_id": salt_id_for(self.secret),
            "rule": HASH_RULE,
            "entries": [
                {"h": hash_tail(self.secret, r.tail), "kind": r.kind, "at": ms_text(r.at)}
                for r in page
            ],
            "next": encode_cursor(us_text(last.at), last.id, last.n)
            if len(rows) > limit and last
            else None,
            "resume": _resume(
                (us_text(last.at), last.id, last.n) if last else None, cursor
            ),
        }

    def outcome_rows(self) -> list[tuple[datetime, str, dict[str, Any]]]:
        """``outcomesPage``'s rows, unpaged: (at, intake_id, row)."""
        now = self.clock()
        setting = self._labels()
        labelled = setting | self._dictionary()
        rows: list[tuple[datetime, str, dict[str, Any]]] = []
        for intake in self.intakes.values():
            eid = intake.external_id
            if intake.channel != "webhook" or not eid or not eid.startswith("cs:"):
                continue
            utm = (intake.ref or {}).get("utm_source")
            if not (
                intake.source == AGENT
                or (intake.source == OTHER and utm == "clientscrapper")
            ):
                continue
            if intake.created_at <= now - OUTCOMES_WINDOW:
                continue
            lead = self.leads.get(intake.lead_id) if intake.lead_id else None
            reason = (
                normalise_reason(lead.lost_reason) if lead and lead.lost_reason else None
            )
            moved = max(
                t
                for t in (
                    intake.created_at,
                    intake.contacted_at,
                    lead.updated_at if lead else None,
                    lead.closed_at if lead else None,
                )
                if t is not None
            )
            row: dict[str, Any] = {
                "external_id": eid,
                "intake_id": intake.id,
                "outcome": intake.outcome,
                "reason": intake.reason,
                "lead_id": intake.lead_id,
                "client_id": (lead.client_id if lead and lead.client_id else None)
                or intake.client_id,
                "stage_kind": lead.stage_kind if lead else None,
                "stage_name": lead.stage_name if lead else None,
                "lost_reason": lead.lost_reason[:LOST_REASON_MAX]
                if lead and lead.lost_reason and reason in labelled
                else None,
                "no_contact": bool(
                    lead and lead.stage_kind == "lost" and reason in setting
                ),
                "closed_at": ms_text(lead.closed_at) if lead and lead.closed_at else None,
                "contacted_at": ms_text(intake.contacted_at)
                if intake.contacted_at
                else None,
                "contact_kind": intake.contact_kind,
                "quoted_volume_m3": lead.quoted_volume_m3 if lead else None,
                "at": ms_text(moved),
            }
            row.update(self.outcome_overrides.get(eid, {}))
            rows.append((moved, intake.id, row))
        rows.sort(key=lambda r: (r[0], r[1]))
        return rows

    def _outcomes_body(
        self, since: str | None, cursor: tuple[str, str, int] | None, limit: int
    ) -> dict[str, Any]:
        rows = self.outcome_rows()
        if since is not None:
            rows = [r for r in rows if r[0] > instant(since)]
        if cursor is not None:
            c_at = instant(_pad_us(cursor[0]))
            rows = [r for r in rows if (r[0], r[1]) > (c_at, cursor[1])]
        page = rows[:limit]
        last = page[-1] if page else None
        return {
            "generated_at": ms_text(self.clock()),
            "rows": [r[2] for r in page],
            "next": encode_cursor(us_text(last[0]), last[1], 0)
            if len(rows) > limit and last
            else None,
            "resume": _resume((us_text(last[0]), last[1], 0) if last else None, cursor),
        }


def _pad_us(at: str) -> str:
    """A cursor's instant with its fraction made six digits (postgres reads
    ``…09:00:00Z`` and ``…09:00:00.000000Z`` as one instant)."""
    head, _, rest = at.partition(".")
    if not rest:
        return head.rstrip("Z") + ".000000Z"
    return f"{head}.{rest.rstrip('Z').ljust(6, '0')}Z"


def _resume(
    last: tuple[str, str, int] | None, sent: tuple[str, str, int] | None
) -> str | None:
    """``resumeAfter``: the last row's cursor, else the one that was sent."""
    at = last or sent
    return encode_cursor(*at) if at else None


# --- fromPlain ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlainFields:
    name: str | None
    phone: str | None
    note: str | None
    company: str | None
    external_id: str | None
    extras: tuple[tuple[str, str], ...]

    @property
    def phone_digits(self) -> str | None:
        digits = _NOT_DIGIT.sub("", self.phone or "")
        return digits[-9:] if len(digits) >= 9 else None


def _pick(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def from_plain(body: Any) -> PlainFields:
    """inbound-webhook.ts ``fromPlain`` (the agent never sends
    ``user_column_data``) and ``landOnce``'s cuts: the name at 120 units, the
    note — its own text, then one ``key: value`` line per extra key in the
    body's order, a value JS ``trim()`` leaves empty making none — collapsed
    and cut at 1000."""
    extras: list[tuple[str, str]] = []
    if isinstance(body, dict):
        for key, value in body.items():
            if key.lower() in KNOWN_BODY_KEYS:
                continue
            text = js_str(value)
            if text:
                extras.append((key, text))
    own = js_str(_pick(body, "note")) or js_str(_pick(body, "izoh"))
    joined = "\n".join(filter(None, [own, "\n".join(f"{k}: {v}" for k, v in extras)]))
    company = None
    if isinstance(body, dict):
        for want in ("company", "firma"):
            company = next(
                (js_str(v) for k, v in body.items() if k.lower() == want and js_str(v)),
                None,
            )
            if company:
                break
    return PlainFields(
        name=trim_field(
            js_str(_pick(body, "name")) or js_str(_pick(body, "ism")), NAME_MAX
        ),
        phone=js_str(_pick(body, "phone")) or js_str(_pick(body, "telefon")),
        note=trim_field(joined or None, NOTE_MAX),
        company=company,
        external_id=js_str(_pick(body, "external_id")) or js_str(_pick(body, "id")),
        extras=tuple(extras),
    )


def landed_note(body: Any) -> str | None:
    """The note the ERP keeps for this body (A.2)."""
    return from_plain(body).note


def _inbound_note(source: str, text: str | None) -> str:
    return "\n".join(filter(None, [f"📣 {source}", text or ""]))


def _ref_without_secret(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    return {k: v for k, v in body.items() if k.lower() not in SECRET_BODY_KEYS}


# --- a client-side read timeout -------------------------------------------------------


class TimeoutTransport(httpx.AsyncBaseTransport):
    """Raises ``httpx.ReadTimeout`` on the client side after ``timeout``
    seconds while the fake's handler goes on RUNNING (shielded, kept by the
    fake) — a first request still in the ERP, not one that finished."""

    def __init__(self, fake: FakeErp, timeout: float) -> None:
        self.fake = fake
        self.timeout = timeout

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        task = asyncio.ensure_future(self.fake.handler(request))
        self.fake._tasks.add(task)
        task.add_done_callback(self.fake._tasks.discard)
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.timeout)
        except TimeoutError:
            raise httpx.ReadTimeout("fake: no answer in time", request=request) from None
