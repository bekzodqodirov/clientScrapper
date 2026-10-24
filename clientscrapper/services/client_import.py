# VENDORED_FROM dailyJournal@6b1f67f miya/services/client_import.py
"""The ERP client export as suppression — the feed's fallback (05 §B.3, T-11).

While the ERP's ``agent`` door is not deployed there is no suppression feed,
so the owner exports **Mijozlar → XLSX** and runs ``make import-clients``
(or sends the file to the bot, ``/import``, 06 §1.2). Every phone tail the
file yields becomes ``suppression(kind='client', key_type='phone_tail',
source='manual_import')`` — CLEAR tails, never hashes: the feed hashes with
the agent door's secret, which does not exist yet (05 §B.3). A completed
full pull later retires each such row whose hash it carries
(``erp_feeds._retire_manual_import``, 05 §B.2 step 5); a tail it does not
carry stays until the owner removes it.

MIYA's shape is kept: ``read_rows`` reads the file and writes nothing,
``plan_import`` says what would happen and writes nothing, ``apply_import``
writes in the caller's transaction, and a second run of the same file adds
nothing. What changed is what a row means — not a person to attach a code
to, but the phones of an ERP client card — and so how a cell is read:

* **The file is the ERP's export** (``src/app/api/clients/xlsx/route.ts``):
  headers «Code, Name, Manager, Phones, Active» plus the owner's custom-field
  labels, one row per card, active AND inactive (the feed carries both), at
  most 5,000 rows (``CLIENT_EXPORT_CAP``). Only the «Phones» column is read —
  of a client card the feed reads its ``phones`` list and nothing else, so a
  tail from any other column could never retire. A file without that column
  (a saved view that hides it) is refused. The feed's ``client`` rows also
  come from two places no export carries — the phones of a person record
  behind several codes (``crm_people``) and of every lead that became a
  client (won, or attached to a code) — so during the fallback those numbers
  are not suppressed by this file.
* **A cell is the card's list joined with ", "**, so it is split back at
  ", " first and each element is read by the ERP's own rule for ONE phones
  value (``tails_of``, a copy of ``tailsSql`` in ``agent-feed.ts``). Read
  as one value, an element such as «90/000-00-22» before another number
  would lose its tail: its parts are fragments, and only its own whole-value
  last nine carries it. The cell is never whitespace-folded: a line break
  and a run of two spaces are separators to the ERP.
* **Only nine-digit tails are stored**: the feed carries nothing shorter, the
  ``phone_tail`` CHECK refuses it, and a part shorter than nine digits is a
  fragment of a number, not a number. A whole value of seven or eight digits
  (a legacy card) is listed back to the owner as one the file cannot carry —
  the ERP's own belt catches it on the agent door (05 §A.8).
* **The export carries the screen's filters** (``q``, custom filters, a
  saved view's columns), so a filtered screen exports fewer rows. Nothing
  in the file says so; what the import can see is the row count, so a file
  with fewer than ``SHRINK_FLOOR`` of the previous import's rows is refused
  until the owner confirms it (``accept_fewer``). The first import has no
  yardstick: the plan tells the owner to compare the count with the screen.
  One more sign is in the header: the plain download always carries
  «Active», a saved view's download never does (the screen's column picker
  does not offer it), and a view may hold filters — so a file without
  «Active» is cautioned (``view``) and its inactive count is not claimed.
* **An import only adds.** A tail absent from the new file is not removed: a
  filtered export must not un-suppress anybody (fail closed). Rows already
  there from an earlier import get ``last_seen_at`` moved; a phone another
  source already holds as a ``client`` tail (an outcome) is left to it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import math
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Literal, TypeGuard

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.config import settings
from clientscrapper.db.models import Source, Suppression
from clientscrapper.services import erp_feeds, keys

log = logging.getLogger(__name__)

# --- the ERP's export (fullprompt agent-feed d138b3bc) ---------------------------------

# platform/clients/list.ts CLIENT_EXPORT_CAP: the screen and its file stop here.
MAX_ROWS: Final = 5000
# api/clients/xlsx/route.ts CORE_TITLES — compared trimmed and case-folded.
PHONES_HEADER: Final = "phones"
CODE_HEADER: Final = "code"
ACTIVE_HEADER: Final = "active"
# The route writes a card's phones as ``phones.join(', ')`` and Active as «✓».
ELEMENT_JOIN: Final = ", "
ACTIVE_MARK: Final = "✓"

# --- tailsSql (agent-feed.ts), one phones value ----------------------------------------

# PHONE_SPLIT_PG: list punctuation, a line break, two spaces or tabs, or
# spaces/tabs before a «+».
PHONE_SPLIT: Final = re.compile(r"[,;/|\\\r\n]+|[\t ]{2,}|[\t ]+(?=\+)")
_NOT_DIGIT: Final = re.compile(r"[^0-9]")
# A part made entirely of 12-digit «998…» numbers.
_RUN_998: Final = re.compile(r"(?:998[0-9]{9})+")
# Chunks only inside a part of more than 15 digits (E.164 stops at 15) and at
# most 120; anything longer is read by its last nine alone.
RUN_MIN: Final = 16
RUN_MAX: Final = 120
TAIL_LEN: Final = 9

# --- the import's own rules ------------------------------------------------------------

# A file with fewer rows than this share of the previous import's is taken for
# a filtered export: the feed's own tolerance for a list that shrank.
FEWER_FLOOR: Final = erp_feeds.SHRINK_FLOOR
# Excel keeps 15 significant digits: a phone it turned into a number at or past
# this is no longer the phone.
EXACT_FLOAT_MAX: Final = 10**15
CHUNK: Final = 1000
CONFIG_KEY: Final = "client_import"
# Bad phones listed back to the owner one by one; the rest are counted.
SHOWN_BAD: Final = 20
SUFFIXES: Final = (".xlsx", ".csv")

BadFileCode = Literal[
    "type", "missing", "denied", "unreadable", "empty", "no_phones_column",
    "too_many_rows", "no_rows", "no_phones",
]  # fmt: skip
Why = Literal["short", "no_digits", "precision", "not_text"]
Refusal = Literal["fewer"]
Caution = Literal["view", "at_cap", "same_file"]


class BadFile(Exception):
    """Not a file the import can read as the ERP's client export."""

    def __init__(self, code: BadFileCode) -> None:
        super().__init__(code)
        self.code: BadFileCode = code

    def text(self) -> str:
        return BAD_FILE_TEXT[self.code].format(cap=MAX_ROWS)


class ImportRefused(Exception):
    """``apply_import`` on a plan the owner has not confirmed."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal)
        self.refusal: Refusal = refusal


# --- the tail rule ---------------------------------------------------------------------


def tails_of(value: str) -> list[str]:
    """Every nine-digit tail ONE phones value answers to, in ``tailsSql``'s
    order (the whole value first, then each part's), without repeats.

    The whole value's last nine digits (``keys.phone_tail``); each part split
    at ``PHONE_SPLIT`` — only a part of nine digits or more; and inside a part
    of 16 to 120 digits, a part made entirely of 12-digit «998…» numbers gives
    each number's last nine, otherwise a length that is a multiple of nine
    gives each 9-digit chunk — never both: three 998 numbers are 36 digits
    and their 9-digit chunks are nobody's phone. Any other part, and any part
    over 120 digits, gives its last nine only."""
    found: dict[str, None] = {}

    def add(tail: str | None) -> None:
        if tail is not None:
            found.setdefault(tail, None)

    add(keys.phone_tail(value))
    for part in PHONE_SPLIT.split(value):
        digits = _NOT_DIGIT.sub("", part)
        add(keys.phone_tail(digits))
        if not RUN_MIN <= len(digits) <= RUN_MAX:
            continue
        if _RUN_998.fullmatch(digits):
            for i in range(len(digits) // 12):
                add(digits[i * 12 + 3 : i * 12 + 12])
        elif len(digits) % TAIL_LEN == 0:
            for i in range(len(digits) // TAIL_LEN):
                add(digits[i * TAIL_LEN : (i + 1) * TAIL_LEN])
    return list(found)


def elements_of(cell: str) -> list[str]:
    """The phones list of one card back from its export cell (", "-joined),
    blank elements dropped. An element that itself held ", " is split too:
    the cell cannot tell the two apart (stated in the module docstring)."""
    return [e for e in cell.split(ELEMENT_JOIN) if e.strip()]


# --- reading ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExportRow:
    """One client card of the export."""

    line: int  # the spreadsheet's own row number
    code: str
    # One string per «Phones» column (the ERP writes one; a custom field may
    # carry the same label and is read too — its tails then never retire).
    phones: tuple[str, ...]
    # A «Phones» cell that is not text the ERP wrote (Excel's number or date).
    unreadable: tuple[Why, ...] = ()
    # None when the file has no Active column.
    active: bool | None = None


@dataclass(frozen=True, slots=True)
class ExportFile:
    rows: tuple[ExportRow, ...]
    sha256: str

    @property
    def has_active(self) -> bool:
        """The file has an «Active» column: every row then says yes or no."""
        return any(row.active is not None for row in self.rows)


def _text(value: Any) -> str:
    """A cell that is not a phone column's, as text (the code, the mark)."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _phone_cell(value: Any) -> tuple[str, Why | None]:
    """A «Phones» cell as the ERP wrote it, or why it cannot be read — never
    whitespace-folded: a line break and a run of spaces are separators
    (``PHONE_SPLIT``). A number is what Excel made of a phone it was allowed
    to retype: a number below 10**15 is still its digits, one at or past it
    is not — Excel keeps 15 significant digits, and writes the rounded
    value back without a «.» when it is whole, which openpyxl reads as an
    ``int``."""
    if value is None:
        return "", None
    if isinstance(value, str):
        return value, None
    if isinstance(value, bool | datetime | date):
        return "", "not_text"
    if isinstance(value, int):
        if abs(value) >= EXACT_FLOAT_MAX:
            return "", "precision"
        return str(value), None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return "", "not_text"
        if abs(value) >= EXACT_FLOAT_MAX:
            return "", "precision"
        return str(int(value)), None
    return "", "not_text"


def _xlsx_rows(data: bytes) -> list[tuple[Any, ...]]:
    from openpyxl import load_workbook  # type: ignore[import-untyped]

    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many kinds for a bad file
        raise BadFile("unreadable") from exc
    try:
        sheet = book.worksheets[0]
        rows: list[tuple[Any, ...]] = []
        filled = 0
        for row in sheet.iter_rows(values_only=True):
            rows.append(tuple(row))
            filled += any(_text(v) for v in row)
            # The header, the cap and one more: enough to know it is no export.
            if filled > MAX_ROWS + 1:
                break
        return rows
    except Exception as exc:
        raise BadFile("unreadable") from exc
    finally:
        book.close()


def _csv_rows(data: bytes) -> list[tuple[Any, ...]]:
    """A CSV saved from the export: UTF-8 (with or without a BOM), else the
    Windows Cyrillic code page Excel uses here. The delimiter is the one the
    header line holds most of — a phones cell is full of commas, so a sniffer
    reading data lines guesses wrong."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1251")
        except UnicodeDecodeError as exc:
            raise BadFile("unreadable") from exc
    header = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = max((",", ";", "\t"), key=header.count)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        return [tuple(row) for row in reader]
    except csv.Error as exc:
        raise BadFile("unreadable") from exc


def _columns(header: Sequence[Any], name: str) -> list[int]:
    return [i for i, v in enumerate(header) if _text(v).casefold() == name]


def read_rows(path: str | Path, *, data: bytes | None = None) -> ExportFile:
    """The export's client rows (no database). ``data`` is the file's bytes
    when they are already in memory (the bot); ``path`` then only names it.
    Raises ``BadFile`` — ``missing`` when nothing is at ``path`` and
    ``denied`` when it cannot be opened, never ``unreadable``: that one says
    the FILE is broken, and the owner would download it again for ever."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUFFIXES:
        raise BadFile("type")
    if data is None:
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise BadFile("missing") from exc
        except PermissionError as exc:
            raise BadFile("denied") from exc
        except OSError as exc:
            raise BadFile("unreadable") from exc
    table = _xlsx_rows(data) if suffix == ".xlsx" else _csv_rows(data)
    first = next((i for i, r in enumerate(table) if any(_text(v) for v in r)), None)
    if first is None:
        raise BadFile("empty")
    header = table[first]
    phones = _columns(header, PHONES_HEADER)
    if not phones:
        raise BadFile("no_phones_column")
    code = next(iter(_columns(header, CODE_HEADER)), None)
    active = next(iter(_columns(header, ACTIVE_HEADER)), None)

    def cell(row: Sequence[Any], index: int | None) -> Any:
        return row[index] if index is not None and index < len(row) else None

    rows: list[ExportRow] = []
    for offset, raw in enumerate(table[first + 1 :]):
        if not any(_text(v) for v in raw):
            continue
        if len(rows) == MAX_ROWS:
            raise BadFile("too_many_rows")
        read = [_phone_cell(cell(raw, i)) for i in phones]
        rows.append(
            ExportRow(
                line=first + offset + 2,
                code=_text(cell(raw, code)),
                phones=tuple(text for text, why in read if why is None),
                unreadable=tuple(why for _, why in read if why is not None),
                active=None if active is None else _text(cell(raw, active)) != "",
            )
        )
    if not rows:
        raise BadFile("no_rows")
    return ExportFile(rows=tuple(rows), sha256=hashlib.sha256(data).hexdigest())


# --- what the file yields --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BadPhone:
    """A phone the file holds and the import cannot store."""

    line: int
    code: str
    value: str  # the element as written ('' for an unreadable cell)
    why: Why


@dataclass(frozen=True, slots=True)
class FileTails:
    tails: tuple[str, ...]  # distinct, in the order the file first gives them
    bad: tuple[BadPhone, ...]
    rows: int
    inactive: int | None  # None: the file has no «Active» column
    without_phone: int


def file_tails(export: ExportFile) -> FileTails:
    """Every tail of every card, and every phone that yields none."""
    seen: dict[str, None] = {}
    bad: list[BadPhone] = []
    inactive = without_phone = 0
    for row in export.rows:
        inactive += row.active is False
        elements = [e for cell in row.phones for e in elements_of(cell)]
        bad += [BadPhone(row.line, row.code, "", why) for why in row.unreadable]
        if not elements and not row.unreadable:
            without_phone += 1
        for element in elements:
            found = tails_of(element)
            if not found:
                why: Why = "short" if _NOT_DIGIT.sub("", element) else "no_digits"
                bad.append(BadPhone(row.line, row.code, element.strip(), why))
            for tail in found:
                seen.setdefault(tail, None)
    return FileTails(
        tails=tuple(seen),
        bad=tuple(bad),
        rows=len(export.rows),
        inactive=inactive if export.has_active else None,
        without_phone=without_phone,
    )


# --- the previous import (sources.erp_sync.config.client_import) -----------------------


def _count(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


@dataclass(frozen=True, slots=True)
class LastImport:
    rows: int
    tails: int
    sha256: str
    at: datetime

    @classmethod
    def from_json(cls, raw: Any) -> LastImport | None:
        if not isinstance(raw, dict):
            return None
        rows, tails, sha = raw.get("rows"), raw.get("tails"), raw.get("sha256")
        stamp = _aware(raw.get("at"))
        if (
            not _count(rows)
            or not _count(tails)
            or not isinstance(sha, str)
            or stamp is None
        ):
            return None
        return cls(rows=rows, tails=tails, sha256=sha, at=stamp)

    def to_json(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "tails": self.tails,
            "sha256": self.sha256,
            "at": self.at.isoformat(),
        }


async def last_import(session: AsyncSession) -> LastImport | None:
    raw = await session.scalar(
        sa.select(Source.config[CONFIG_KEY])
        .where(Source.key == erp_feeds.ERP_SOURCE)
        .execution_options(populate_existing=True)
    )
    return LastImport.from_json(raw)


async def _store_last(session: AsyncSession, last: LastImport) -> None:
    """``config.client_import`` replaced, the feeds' keys left as they stand."""
    await session.execute(
        sa.update(Source)
        .where(Source.key == erp_feeds.ERP_SOURCE)
        .values(
            config=sa.func.jsonb_set(
                Source.config,
                sa.text(f"'{{{CONFIG_KEY}}}'"),
                sa.cast(
                    sa.bindparam("client_import", last.to_json(), type_=JSONB), JSONB
                ),
                True,
            )
        )
    )


# --- plan and apply --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportPlan:
    sha256: str
    rows: int
    inactive: int | None  # None: the file cannot say (no «Active» column)
    without_phone: int
    tails: tuple[str, ...]
    new: tuple[str, ...]  # not yet a clear client phone_tail row
    # Already one, from any source. A feed row holds a hash, never compared
    # here: after the switch-over its tail reads «yangi», is inserted, and
    # the next full pull retires it (05 §B.2 step 5).
    same: tuple[str, ...]
    bad: tuple[BadPhone, ...]
    previous: LastImport | None
    refusal: Refusal | None
    cautions: tuple[Caution, ...]

    @property
    def phones(self) -> int:
        """06 §1.2's «{n} ta telefon»: the tails and the phones that gave none."""
        return len(self.tails) + len(self.bad)


def _chunks(items: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), CHUNK):
        yield items[start : start + CHUNK]


def _client_tails() -> sa.ColumnElement[bool]:
    return sa.and_(Suppression.kind == "client", Suppression.key_type == "phone_tail")


async def _held(session: AsyncSession, tails: Sequence[str]) -> set[str]:
    held: set[str] = set()
    for chunk in _chunks(tails):
        held.update(
            await session.scalars(
                sa.select(Suppression.key).where(
                    _client_tails(), Suppression.key.in_(chunk)
                )
            )
        )
    return held


async def plan_import(session: AsyncSession, export: ExportFile) -> ImportPlan:
    """What ``apply_import`` would do with this file. Writes nothing; raises
    ``BadFile('no_phones')`` when not one cell yields a tail."""
    got = file_tails(export)
    if not got.tails:
        raise BadFile("no_phones")
    held = await _held(session, got.tails)
    previous = await last_import(session)
    cautions: list[Caution] = []
    if not export.has_active:
        cautions.append("view")
    if got.rows >= MAX_ROWS:
        cautions.append("at_cap")
    if previous is not None and previous.sha256 == export.sha256:
        cautions.append("same_file")
    fewer = previous is not None and got.rows < FEWER_FLOOR * previous.rows
    return ImportPlan(
        sha256=export.sha256,
        rows=got.rows,
        inactive=got.inactive,
        without_phone=got.without_phone,
        tails=got.tails,
        new=tuple(t for t in got.tails if t not in held),
        same=tuple(t for t in got.tails if t in held),
        bad=got.bad,
        previous=previous,
        refusal="fewer" if fewer else None,
        cautions=tuple(cautions),
    )


@dataclass(frozen=True, slots=True)
class Applied:
    inserted: int  # new manual_import rows
    restamped: int  # earlier manual_import rows the file still carries
    elsewhere: int  # tails another source already holds as a client


async def apply_import(
    session: AsyncSession, plan: ImportPlan, *, now: datetime, accept_fewer: bool = False
) -> Applied:
    """Write the plan in the caller's transaction (it commits). Only adds:
    a new tail is inserted, one an earlier import stored gets ``last_seen_at
    = now``, one another source holds is left to it. Raises
    ``ImportRefused`` on a refused plan unless the owner accepted it."""
    if plan.refusal == "fewer" and not accept_fewer:
        raise ImportRefused(plan.refusal)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    inserted = restamped = 0
    for chunk in _chunks(plan.tails):
        stmt = (
            pg_insert(Suppression)
            .values(
                [
                    {
                        "kind": "client",
                        "key_type": "phone_tail",
                        "key": tail,
                        "source": "manual_import",
                        "created_at": now,
                        "last_seen_at": now,
                    }
                    for tail in chunk
                ]
            )
            .on_conflict_do_nothing(constraint="uq_suppression_key")
            .returning(Suppression.key)
        )
        added = set((await session.execute(stmt)).scalars())
        inserted += len(added)
        earlier = [t for t in chunk if t not in added]
        if earlier:
            result = await session.execute(
                sa.update(Suppression)
                .where(
                    _client_tails(),
                    Suppression.source == "manual_import",
                    Suppression.key.in_(earlier),
                )
                .values(last_seen_at=now)
                .returning(Suppression.id)
            )
            restamped += len(result.all())
    await _store_last(
        session,
        LastImport(rows=plan.rows, tails=len(plan.tails), sha256=plan.sha256, at=now),
    )
    applied = Applied(
        inserted=inserted,
        restamped=restamped,
        elsewhere=len(plan.tails) - inserted - restamped,
    )
    log.info(
        "client import: %d tails, %d new, %d already imported, %d held elsewhere",
        len(plan.tails),
        applied.inserted,
        applied.restamped,
        applied.elsewhere,
        extra={"rows": plan.rows, "bad": len(plan.bad)},
    )
    return applied


# --- the owner's text (06 §1.2) --------------------------------------------------------

BAD_FILE_TEXT: Final[dict[BadFileCode, str]] = {
    "type": "Faqat .xlsx yoki .csv fayl oʻqiladi (ERP → Mijozlar → XLSX).",
    "missing": "Fayl topilmadi.",
    "denied": "Faylni oʻqishga ruxsat yoʻq.",
    "unreadable": "Faylni ochib boʻlmadi: u buzilgan yoki .xlsx/.csv emas.",
    "empty": "Fayl boʻsh.",
    "no_phones_column": (
        "«Phones» ustuni yoʻq. ERP → Mijozlar ekranidan koʻrinish tanlamasdan va "
        "filtrsiz XLSX yuklab oling."
    ),
    "too_many_rows": (
        "Faylda {cap} tadan ortiq mijoz qatori bor, ERP eksporti esa {cap} tadan "
        "oshmaydi: bu ERP mijozlar fayli emas."
    ),
    "no_rows": "Faylda mijoz qatori yoʻq.",
    "no_phones": (
        "«Phones» ustunida birorta toʻliq telefon raqami yoʻq: eksportni tekshiring."
    ),
}

WHY_TEXT: Final[dict[Why, str]] = {
    "short": "9 raqamdan qisqa — ERP roʻyxati ham bunday raqamni bermaydi, saqlanmadi",
    "no_digits": "raqam yoʻq",
    "precision": (
        "Excel raqamni 15 xonagacha qisqartirgan — faylni ERP dan qayta yuklab oling"
    ),
    "not_text": "telefon emas (sana yoki boshqa qiymat)",
}

CAUTION_TEXT: Final[dict[Caution, str]] = {
    "view": (
        "⚠️ Faylda «Active» ustuni yoʻq: u saqlangan koʻrinishdan olingan va filtrli "
        "boʻlishi mumkin. ERP → Mijozlar ekranidan koʻrinish tanlamasdan va filtrsiz "
        "qayta yuklab oling."
    ),
    "at_cap": (
        "⚠️ Faylda {cap} qator — ERP eksporti shu yerda toʻxtaydi: qolgan mijozlar "
        "faylga tushmagan boʻlishi mumkin."
    ),
    "same_file": (
        "⚠️ Bu oʻtgan safargi faylning oʻzi: ERP dan yangisini yuklab oldingizmi?"
    ),
}

REFUSAL_TEXT: Final[dict[Refusal, str]] = {
    "fewer": (
        "⛔ Faylda oʻtgan safardan kam qator ({rows} < {previous}): eksport filtr yoki "
        "saqlangan koʻrinish bilan olingan boʻlishi mumkin. ERP → Mijozlar ekranida "
        "qidiruv va filtrlarni tozalab, qayta yuklab oling. Fayl toʻgʻri boʻlsa, "
        "kam qatorni tasdiqlang."
    ),
}

FIRST_IMPORT_TEXT: Final = (
    "Birinchi import: {rows} qator ERP → Mijozlar ekranidagi jami soni bilan bir xilmi, "
    "tekshiring — filtr yoki saqlangan koʻrinish bilan olingan fayl kam qator beradi."
)
PREVIOUS_TEXT: Final = "Oʻtgan import ({day}): {rows} qator, {tails} ta raqam."
ROWS_TEXT: Final = (
    "Fayl: {rows} ta mijoz qatori ({inactive} tasi faol emas, "
    "{without_phone} tasida telefon yoʻq)."
)
ROWS_NO_ACTIVE_TEXT: Final = (
    "Fayl: {rows} ta mijoz qatori ({without_phone} tasida telefon yoʻq)."
)
SUMMARY_TEXT: Final = "{n} ta telefon: {new} yangi, {same} bor, {bad} notoʻgʻri"
BAD_LINE_TEXT: Final = "  {line}-qator {code}: «{value}» — {why}"
MORE_BAD_TEXT: Final = "  … yana {more} ta"
APPLIED_TEXT: Final = (
    "Yozildi: {inserted} ta yangi raqam; {restamped} tasi oldingi importda bor edi, "
    "{elsewhere} tasi boshqa manbadan bor."
)


def summary_line(plan: ImportPlan) -> str:
    """06 §1.2's one line: «{n} ta telefon: {new} yangi, {same} bor, {bad} notoʻgʻri»."""
    return SUMMARY_TEXT.format(
        n=plan.phones, new=len(plan.new), same=len(plan.same), bad=len(plan.bad)
    )


def _bad_lines(bad: Iterable[BadPhone]) -> list[str]:
    listed = list(bad)
    lines = [
        BAD_LINE_TEXT.format(
            line=b.line,
            code=b.code or "—",
            value=" ".join(b.value.split()),
            why=WHY_TEXT[b.why],
        )
        for b in listed[:SHOWN_BAD]
    ]
    if len(listed) > SHOWN_BAD:
        lines.append(MORE_BAD_TEXT.format(more=len(listed) - SHOWN_BAD))
    return lines


def plan_lines(plan: ImportPlan) -> list[str]:
    """The plan for the owner, one line each (the tool prints them, the bot
    sends them joined): counts, the phones that cannot be stored, the
    yardstick, the cautions and the refusal."""
    rows = (
        ROWS_NO_ACTIVE_TEXT.format(rows=plan.rows, without_phone=plan.without_phone)
        if plan.inactive is None
        else ROWS_TEXT.format(
            rows=plan.rows, inactive=plan.inactive, without_phone=plan.without_phone
        )
    )
    lines = [
        rows,
        summary_line(plan),
        *_bad_lines(plan.bad),
    ]
    if plan.previous is None:
        lines.append(FIRST_IMPORT_TEXT.format(rows=plan.rows))
    else:
        day = plan.previous.at.astimezone(settings.tz).strftime("%d.%m.%Y")
        lines.append(
            PREVIOUS_TEXT.format(
                day=day, rows=plan.previous.rows, tails=plan.previous.tails
            )
        )
    lines += [CAUTION_TEXT[c].format(cap=MAX_ROWS) for c in plan.cautions]
    if plan.refusal is not None:
        previous = plan.previous.rows if plan.previous is not None else 0
        lines.append(REFUSAL_TEXT[plan.refusal].format(rows=plan.rows, previous=previous))
    return lines


def applied_line(applied: Applied) -> str:
    return APPLIED_TEXT.format(
        inserted=applied.inserted,
        restamped=applied.restamped,
        elsewhere=applied.elsewhere,
    )
