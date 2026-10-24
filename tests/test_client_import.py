"""The ERP client export as suppression — the XLSX fallback (T-11; 05 §B.3,
§B.2 step 5, §A.7; 02 §2.8; 03 §2.2; 06 §1.2).

Three oracles, none of them the code under test restated:

* concrete vectors for every rule 07's T-11 row names, pinned value by value;
* the ERP's own ``tailsSql`` (``agent-feed.ts`` at d138b3bc), run verbatim in
  Postgres over the fixture's phones and a seeded random corpus;
* a round trip through the fake ERP (``tests/fake_erp.py``, whose cards yield
  the feed's tails by its own copy of the rule): the export of a client book,
  imported, is retired completely by a full pull of the same book — and a
  tail the ERP no longer carries stays.

``tests/fixtures/client_import/clients.xlsx`` is written by the ERP's own
writer (exceljs) from ``clients_book.json``; its README says how.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import subprocess
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.db.models import Source, Suppression
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import client_import as ci
from clientscrapper.services import erp_feeds, keys
from clientscrapper.services.client_import import BadFile, BadPhone, ImportRefused
from clientscrapper.services.erp_feeds import ErpClient
from clientscrapper.tools import import_clients
from tests.fake_erp import FakeErp
from tests.test_uz_text import problems

TASHKENT = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 24, 10, 0, tzinfo=TASHKENT)
SECRET = "test-secret"
BASE = "http://erp.test"
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "client_import"
XLSX = FIXTURE / "clients.xlsx"
BOOK = json.loads((FIXTURE / "clients_book.json").read_text(encoding="utf-8"))
HEADER = ["Code", "Name", "Manager", "Phones", "Active", "Shahar"]

# Every tail of the fixture, in the order the file first gives them (worked
# by hand from 05 §A.7, card by card; the ERP's SQL agrees below).
FIXTURE_TAILS = (
    "900000001",
    "900000002", "930000003",
    "910000005", "900000004",
    "900000007", "900000006",
    "900000010", "900000008", "900000009",
    "900000013",
    "900000015",
    "900000031",
    "900000042",
    "900000052", *(f"9000000{n}" for n in range(43, 52)),
    "900000056",
    "900000057", "900000058",
    "900000060", "900000059",
    "900000062", "900000061",
    "900000064", "900000063",
    "900000066", "900000065",
    "900000069", "900000067", "900000068",
    "712000071", "900000070",
    "800000075", "900000072",
    "900000054", "900000053",
)  # fmt: skip


# --- the tail rule: 07 T-11's vectors ---------------------------------------------------


def run998(*nns: int) -> str:
    return "".join(f"9989000000{n:02d}" for n in nns)


def run9(*nns: int) -> str:
    return "".join(f"9000000{n:02d}" for n in nns)


TAIL_VECTORS = [
    # the whole value's last nine; ( ) and - are not separators
    ("+998 90 000 00 01", ["900000001"]),
    ("+998 (93) 000-00-03", ["930000003"]),
    ("8 90 000 00 14", ["900000014"]),
    # a 24-digit 998…998… part → two tails
    (run998(6, 7), ["900000007", "900000006"]),
    # a 27-digit part → three 9-digit chunks
    (run9(8, 9, 10), ["900000010", "900000008", "900000009"]),
    # a 20-digit part → its last nine only, no chunks: 900000012 is NOT a tail
    ("8 90 000 00 12 8 90 000 00 13", ["900000013"]),
    # a 130-digit part → its last nine only (900000016 is nobody's tail)
    (run998(*[16] * 10) + "1900000015", ["900000015"]),
    # past 120 digits even a run that would chunk gives its last nine only
    (run9(*range(18, 32)), ["900000031"]),  # 126 digits, 14 × 9
    (run998(*range(32, 43)), ["900000042"]),  # 132 digits, 11 × 998
    # exactly 120 digits still chunks
    (run998(*range(43, 53)), ["900000052", *(f"9000000{n}" for n in range(43, 52))]),
    # 36 digits of 998 numbers is a multiple of nine too: never the 9-chunks
    (run998(67, 68, 69), ["900000069", "900000067", "900000068"]),
    # 18 digits = two nine-digit numbers
    (run9(53, 54), ["900000054", "900000053"]),
    # 16 digits, neither a run nor a multiple of nine; 15 digits is no run at all
    ("9000000779000000", ["779000000"]),
    ("900000078900000", ["078900000"]),
    # 12 digits of one 998 number: just the number
    (run998(79), ["900000079"]),
    # every separator of PHONE_SPLIT_PG
    ("+998 90 000 00 04 / +998 91 000 00 05", ["910000005", "900000004"]),
    ("+998 90 000 00 30; +998 90 000 00 31", ["900000031", "900000030"]),
    ("+998 90 000 00 32 | +998 90 000 00 33", ["900000033", "900000032"]),
    ("+998 90 000 00 34 \\ +998 90 000 00 35", ["900000035", "900000034"]),
    ("+998 90 000 00 59\n+998 90 000 00 60", ["900000060", "900000059"]),
    ("+998 90 000 00 61\r+998 90 000 00 62", ["900000062", "900000061"]),
    ("+998 90 000 00 63  90 000 00 64", ["900000064", "900000063"]),
    ("90 000 00 36\t\t90 000 00 37", ["900000037", "900000036"]),
    ("+998 90 000 00 72 +86 138 0000 0075", ["800000075", "900000072"]),
    ("+998 90 000 00 65\t+998 90 000 00 66", ["900000066", "900000065"]),
    # ONE space without a «+» is no separator: one 19-digit part, its last nine
    ("90 000 00 38 99 000 00 39 1", ["900000391"]),
    # words are no separators; a semicolon is
    ("Tel: +998 90 000 00 70; ofis +998 71 200 00 71", ["712000071", "900000070"]),
    # a part shorter than nine digits is a fragment: only the whole value counts
    ("90/000-00-57", ["900000057"]),
    ("90/12/34", []),
    # short tails only for whole values — and none is ever stored
    ("000-00-05", []),
    ("1234567", []),
    # nothing at all
    ("", []),
    (", ;", []),
    ("yoʻq", []),
    # only ASCII digits are digits (postgres [^0-9], keys.phone_tail)
    ("９９８ ９０ ０００ ００ ４０", []),
    ("+998 ９０ 000 00 40", ["980000040"]),
]


@pytest.mark.parametrize(("value", "tails"), TAIL_VECTORS, ids=lambda x: repr(x)[:40])
def test_the_tails_of_one_phones_value(value, tails):
    assert ci.tails_of(value) == tails


def test_only_nine_digit_tails_are_ever_returned():
    for value, _ in TAIL_VECTORS:
        assert all(
            len(t) == 9 and t.isascii() and t.isdigit() for t in ci.tails_of(value)
        )


def test_a_cell_splits_back_into_the_cards_list():
    assert ci.elements_of("+998 90 000 00 02, +998 (93) 000-00-03") == [
        "+998 90 000 00 02",
        "+998 (93) 000-00-03",
    ]
    assert ci.elements_of("") == []
    assert ci.elements_of(", ") == []
    # A comma with no space is not the export's join: the ERP's own rule
    # splits it inside the element.
    assert ci.elements_of("900000041,900000042") == ["900000041,900000042"]
    assert ci.tails_of("900000041,900000042") == ["900000042", "900000041"]


def test_the_cell_is_read_per_element_not_as_one_value():
    # 05 §A.7 / verify:final: «90/000-00-57» answers only by its whole value.
    # Read as ONE value the cell would lose it — its parts are fragments and
    # the cell's own last nine is the second number's.
    cell = "90/000-00-57, +998 90 000 00 58"
    assert ci.tails_of(cell) == ["900000058"]
    got = ci.file_tails(ci.ExportFile((ci.ExportRow(2, "NMN14", (cell,)),), "x"))
    assert got.tails == ("900000057", "900000058")


# --- the ERP's own SQL ------------------------------------------------------------------

# tailsSql, agent-feed.ts @ d138b3bc lines 202–224, with ${expr} bound as :v
# and PHONE_SPLIT_PG (line 172) bound as :split — the JS string's value.
PHONE_SPLIT_PG = r"[,;/|\\\r\n]+|[\t ]{2,}|[\t ]+(?=\+)"
TAILS_SQL = sa.text(
    """
SELECT DISTINCT tl.tail FROM (
  SELECT right(regexp_replace(:v, '[^0-9]', '', 'g'), 9) AS tail, 0::bigint AS o
  UNION ALL
  SELECT ch.tail, pt.k * 1000 + ch.c
    FROM regexp_split_to_table(:v, :split) WITH ORDINALITY AS pt(s, k)
    CROSS JOIN LATERAL (SELECT regexp_replace(pt.s, '[^0-9]', '', 'g') AS d) pd
    CROSS JOIN LATERAL (
      SELECT right(pd.d, 9) AS tail, 0 AS c
       WHERE length(pd.d) >= 9
      UNION ALL
      SELECT substr(pd.d, g.i * 12 + 4, 9), 1 + g.i
        FROM generate_series(0, length(pd.d) / 12 - 1) AS g(i)
       WHERE length(pd.d) > 15 AND length(pd.d) <= 120 AND pd.d ~ '^(998[0-9]{9})+$'
      UNION ALL
      SELECT substr(pd.d, g.i * 9 + 1, 9), 500 + g.i
        FROM generate_series(0, length(pd.d) / 9 - 1) AS g(i)
       WHERE length(pd.d) > 15 AND length(pd.d) <= 120 AND length(pd.d) % 9 = 0
         AND pd.d !~ '^(998[0-9]{9})+$'
    ) ch
   WHERE pd.d <> ''
) tl
WHERE tl.tail <> '' AND length(tl.tail) = 9
"""
)


def random_corpus(n: int, seed: int = 11) -> list[str]:
    """Phone-shaped noise: digit runs of every length around the rule's
    edges, every separator, the near-misses (one space, a comma glued on)."""
    rng = random.Random(seed)
    seps = [", ", ",", ";", "/", "|", "\\", "\n", "\r", "\r\n", "  ", "\t", " +", " ",
            "-", "(", ")", " ( ", "+998 ", "Tel: ", "\t\t", " \t+", ".",
            "доб."]  # fmt: skip
    out = []
    for _ in range(n):
        tokens = []
        for _ in range(rng.randint(1, 6)):
            if rng.random() < 0.3:
                tokens.append(
                    run998(*[rng.randint(0, 99) for _ in range(rng.randint(1, 11))])
                )
            else:
                length = rng.choice(
                    [1, 2, 3, 7, 8, 9, 10, 12, 15, 16, 18, 20, 27, 119, 121]
                )
                tokens.append("".join(rng.choice("0123456789") for _ in range(length)))
            tokens.append(rng.choice(seps))
        out.append("".join(tokens))
    return out


async def erp_tails(session: AsyncSession, value: str) -> set[str]:
    rows = await session.scalars(TAILS_SQL, {"v": value, "split": PHONE_SPLIT_PG})
    return set(rows)


async def test_the_tails_equal_the_erps_own_sql(session):
    values = [v for v, _ in TAIL_VECTORS]
    values += [p for card in BOOK["cards"] for p in card["phones"]]
    values += random_corpus(400)
    differ = {}
    for value in values:
        theirs = await erp_tails(session, value)
        if set(ci.tails_of(value)) != theirs:
            differ[value] = (ci.tails_of(value), sorted(theirs))
    assert differ == {}
    # The corpus reaches every branch: chunked runs and nine-digit chunks.
    many = [v for v in values if len(ci.tails_of(v)) >= 3]
    assert len(many) > 20


async def test_the_fixture_tails_are_the_erps(session):
    erp: set[str] = set()
    for card in BOOK["cards"]:
        for phone in card["phones"]:
            erp |= await erp_tails(session, phone)
    assert erp == set(FIXTURE_TAILS)
    assert len(FIXTURE_TAILS) == len(set(FIXTURE_TAILS)) == 44


# --- reading the export -----------------------------------------------------------------


def test_the_fixture_is_the_one_the_readme_names():
    data = XLSX.read_bytes()
    readme = (FIXTURE / "README.md").read_text(encoding="utf-8")
    assert hashlib.sha256(data).hexdigest() in readme
    assert f"{len(data)} bytes" in readme
    for name in ("clients_book.json", "make_clients_xlsx.mjs"):
        digest = hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest()
        assert digest in readme, name


def test_the_erp_export_reads_back_as_its_book():
    export = ci.read_rows(XLSX)
    assert export.sha256 == hashlib.sha256(XLSX.read_bytes()).hexdigest()
    assert [r.line for r in export.rows] == list(range(2, 25))
    assert [r.code for r in export.rows] == [c["code"] for c in BOOK["cards"]]
    # The writer's XML turned a CR LF into LF; nothing else moved.
    assert [r.phones for r in export.rows] == [
        (", ".join(c["phones"]).replace("\r\n", "\n"),) for c in BOOK["cards"]
    ]
    assert [r.active for r in export.rows] == [c["active"] for c in BOOK["cards"]]
    assert all(r.unreadable == () for r in export.rows)


def test_the_fixture_yields_exactly_its_tails():
    got = ci.file_tails(ci.read_rows(XLSX))
    assert got.tails == FIXTURE_TAILS
    assert got.bad == (BadPhone(12, "NMN11", "000-00-05", "short"),)
    assert (got.rows, got.inactive, got.without_phone) == (23, 1, 1)


def write_xlsx(path: Path, rows: list[list[Any]]) -> Path:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


def test_a_view_without_phones_is_refused(tmp_path):
    path = write_xlsx(
        tmp_path / "view.xlsx",
        [["Code", "Name", "Manager"], ["NMN01", "Namuna Savdo 01", ""]],
    )
    with pytest.raises(BadFile) as caught:
        ci.read_rows(path)
    assert caught.value.code == "no_phones_column"


def test_the_header_is_matched_trimmed_and_in_any_case(tmp_path):
    path = write_xlsx(
        tmp_path / "c.xlsx",
        [[None], [" PHONES ", "code"], ["+998 90 000 00 01", "NMN01"]],
    )
    [row] = ci.read_rows(path).rows
    assert (row.line, row.code, row.phones, row.active) == (
        3,
        "NMN01",
        ("+998 90 000 00 01",),
        None,
    )


def test_a_second_phones_column_is_read_too(tmp_path):
    # A custom field labelled «Phones»: read, so nothing is let through.
    path = write_xlsx(
        tmp_path / "c.xlsx",
        [
            ["Code", "Phones", "Phones"],
            ["NMN01", "+998 90 000 00 01", "+998 90 000 00 02"],
        ],
    )
    assert ci.file_tails(ci.read_rows(path)).tails == ("900000001", "900000002")


@pytest.mark.parametrize(
    ("cell", "tails", "bad"),
    [
        # what Excel makes of a phone it was allowed to retype
        (998900000080, ("900000080",), ()),
        (998900000081.0, ("900000081",), ()),
        (9.98900000082998e23, (), ("precision",)),
        (datetime(2026, 5, 12), (), ("not_text",)),
        (90.5, (), ("not_text",)),
        (True, (), ("not_text",)),
    ],
)
def test_a_cell_excel_retyped(tmp_path, cell, tails, bad):
    path = write_xlsx(tmp_path / "c.xlsx", [["Code", "Phones"], ["NMN01", cell]])
    got = ci.file_tails(ci.read_rows(path))
    assert got.tails == tails
    assert tuple(b.why for b in got.bad) == bad
    assert got.without_phone == 0


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("clients.xls", b"x", "type"),
        ("clients", b"x", "type"),
        ("clients.xlsx", b"not a zip", "unreadable"),
    ],
)
def test_a_file_that_is_not_an_export(tmp_path, name, data, code):
    path = tmp_path / name
    path.write_bytes(data)
    with pytest.raises(BadFile) as caught:
        ci.read_rows(path)
    assert caught.value.code == code


def test_empty_header_only_and_oversized_files(tmp_path):
    cases = {
        "empty.xlsx": ([[None, None]], "empty"),
        "header.xlsx": ([HEADER], "no_rows"),
        "big.xlsx": (
            [["Code", "Phones"]]
            + [[f"N{n}", "+998 90 000 00 01"] for n in range(ci.MAX_ROWS + 1)],
            "too_many_rows",
        ),
    }
    for name, (rows, code) in cases.items():
        with pytest.raises(BadFile) as caught:
            ci.read_rows(write_xlsx(tmp_path / name, rows))
        assert caught.value.code == code, name
    # The cap itself is an export (the ERP's CLIENT_EXPORT_CAP).
    rows = [["Code", "Phones"]] + [[f"N{n}", ""] for n in range(ci.MAX_ROWS)]
    assert len(ci.read_rows(write_xlsx(tmp_path / "cap.xlsx", rows)).rows) == 5000


def test_a_csv_saved_from_the_export(tmp_path):
    semicolons = tmp_path / "a.csv"
    semicolons.write_bytes(
        b"\xef\xbb\xbf"
        + "Code;Name;Phones;Active\n"
        'NMN15;Namuna Qator 15;"+998 90 000 00 59\n+998 90 000 00 60";✓\n'
        "NMN12;Namuna Faol emas 12;+998 90 000 00 56;\n".encode()
    )
    rows = ci.read_rows(semicolons).rows
    assert [(r.code, r.phones, r.active) for r in rows] == [
        ("NMN15", ("+998 90 000 00 59\n+998 90 000 00 60",), True),
        ("NMN12", ("+998 90 000 00 56",), False),
    ]
    commas = tmp_path / "b.csv"
    line = 'NMN02,Намуна,"+998 90 000 00 02, +998 (93) 000-00-03"'
    commas.write_bytes(f"Code,Name,Phones\n{line}\n".encode("cp1251"))
    export = ci.read_rows(commas)
    assert export.rows[0].phones == ("+998 90 000 00 02, +998 (93) 000-00-03",)
    assert ci.file_tails(export).tails == ("900000002", "930000003")


# --- the database: plan and apply -------------------------------------------------------

ROW_FIELDS = ("enabled", "config", "breaker_until", "breaker_reason", "paused_until",
              "paused_reason", "last_run_at")  # fmt: skip


@pytest.fixture
async def erp_row(session: AsyncSession) -> AsyncIterator[None]:
    """``sources.erp_sync`` as it was, put back after the test: the import
    remembers itself in its ``config`` (seeded configuration, never wiped)."""
    row = await session.get(Source, "erp_sync", populate_existing=True)
    assert row is not None
    kept = {f: getattr(row, f) for f in ROW_FIELDS}
    yield
    async with SessionLocal() as own:
        await own.execute(
            sa.update(Source).where(Source.key == "erp_sync").values(**kept)
        )
        await own.commit()


async def rows_of(session: AsyncSession) -> list[Suppression]:
    found = await session.scalars(
        sa.select(Suppression)
        .order_by(Suppression.key)
        .execution_options(populate_existing=True)
    )
    return list(found)


async def manual_tails(session: AsyncSession) -> list[str]:
    found = await session.scalars(
        sa.select(Suppression.key)
        .where(Suppression.source == "manual_import")
        .order_by(Suppression.key)
    )
    return list(found)


async def imported(session: AsyncSession, export: ci.ExportFile, now: datetime = NOW):
    plan = await ci.plan_import(session, export)
    applied = await ci.apply_import(session, plan, now=now)
    await session.commit()
    return plan, applied


async def test_the_import_stores_clear_tails(session, erp_row):
    plan = await ci.plan_import(session, ci.read_rows(XLSX))
    assert (plan.new, plan.same, plan.refusal, plan.cautions) == (
        FIXTURE_TAILS,
        (),
        None,
        (),
    )
    assert ci.summary_line(plan) == "45 ta telefon: 44 yangi, 0 bor, 1 notoʻgʻri"
    assert await rows_of(session) == []  # a plan writes nothing
    applied = await ci.apply_import(session, plan, now=NOW)
    await session.commit()
    assert (applied.inserted, applied.restamped, applied.elsewhere) == (44, 0, 0)
    rows = await rows_of(session)
    assert [r.key for r in rows] == sorted(FIXTURE_TAILS)
    assert {
        (r.kind, r.key_type, r.source, r.salt_id, r.reason, r.erp_at, r.expires_at)
        for r in rows
    } == {("client", "phone_tail", "manual_import", None, None, None, None)}
    assert {(r.created_at, r.last_seen_at) for r in rows} == {(NOW, NOW)}
    # Clear tails: not one hash, under any key (05 §B.3).
    hashed = {keys.erp_phone_hash(SECRET, t) for t in FIXTURE_TAILS}
    assert not hashed & {r.key for r in rows}
    last = await ci.last_import(session)
    assert last == ci.LastImport(rows=23, tails=44, sha256=plan.sha256, at=NOW)
    # The feed's own part of the config is left as it stands.
    config = (await session.get(Source, "erp_sync", populate_existing=True)).config
    assert config["suppression"]["count"] == 0
    assert config["outcomes"] == {"cursor": None, "last_at": None}


async def test_a_second_run_of_the_same_file_adds_nothing(session, erp_row):
    await imported(session, ci.read_rows(XLSX))
    later = NOW + timedelta(days=7)
    plan = await ci.plan_import(session, ci.read_rows(XLSX))
    assert (plan.new, plan.same) == ((), FIXTURE_TAILS)
    assert plan.cautions == ("same_file",)
    applied = await ci.apply_import(session, plan, now=later)
    await session.commit()
    assert (applied.inserted, applied.restamped, applied.elsewhere) == (0, 44, 0)
    rows = await rows_of(session)
    assert len(rows) == 44
    assert {(r.created_at, r.last_seen_at) for r in rows} == {(NOW, later)}


async def test_a_tail_another_source_holds_is_left_to_it(session, erp_row):
    session.add(
        Suppression(
            kind="client",
            key_type="phone_tail",
            key="900000001",
            source="outcomes",
            reason="outcome:cs:namuna",
        )
    )
    # The same number as a DNC is another kind: the import still adds its row.
    session.add(
        Suppression(
            kind="dnc", key_type="phone_tail", key="900000002", source="owner_bot"
        )
    )
    await session.commit()
    plan, applied = await imported(session, ci.read_rows(XLSX))
    assert plan.same == ("900000001",)
    assert (applied.inserted, applied.restamped, applied.elsewhere) == (43, 0, 1)
    held = await session.scalar(
        sa.select(Suppression).where(
            Suppression.kind == "client", Suppression.key == "900000001"
        )
    )
    assert (held.source, held.reason) == ("outcomes", "outcome:cs:namuna")
    kinds = await session.scalars(
        sa.select(Suppression.kind)
        .where(Suppression.key == "900000002")
        .order_by(Suppression.kind)
    )
    assert list(kinds) == ["client", "dnc"]


def book_rows(cards: list[dict[str, Any]]) -> list[list[Any]]:
    """The export the ERP's route writes for ``cards`` (openpyxl, same cells)."""
    return [HEADER] + [
        [
            c["code"],
            c["name"],
            c["manager"],
            ", ".join(c["phones"]),
            "✓" if c["active"] else "",
            c["custom"],
        ]
        for c in cards
    ]


async def test_a_file_with_fewer_rows_is_refused_until_confirmed(
    session, erp_row, tmp_path
):
    await imported(session, ci.read_rows(XLSX))
    cards = BOOK["cards"]
    # 20 < 0.9 × 23: a filtered screen, most likely.
    fewer = ci.read_rows(write_xlsx(tmp_path / "f.xlsx", book_rows(cards[:20])))
    plan = await ci.plan_import(session, fewer)
    assert (plan.refusal, plan.previous.rows) == ("fewer", 23)
    with pytest.raises(ImportRefused):
        await ci.apply_import(session, plan, now=NOW + timedelta(days=1))
    await session.rollback()
    assert (await ci.last_import(session)).rows == 23
    # The owner checked: the book really shrank. Nothing is removed.
    applied = await ci.apply_import(
        session, plan, now=NOW + timedelta(days=1), accept_fewer=True
    )
    await session.commit()
    assert applied.inserted == 0
    assert await manual_tails(session) == sorted(FIXTURE_TAILS)
    assert (await ci.last_import(session)).rows == 20
    # 21 rows against 23 is within the feed's 10 %: no refusal.
    await imported(session, ci.read_rows(XLSX))
    near = ci.read_rows(write_xlsx(tmp_path / "n.xlsx", book_rows(cards[:21])))
    assert (await ci.plan_import(session, near)).refusal is None


async def test_the_first_import_and_the_cap_say_so(session, erp_row, tmp_path):
    rows = [["Code", "Phones"]] + [
        [f"N{n}", f"+998 90 000 {n // 100:02d} {n % 100:02d}"] for n in range(ci.MAX_ROWS)
    ]
    plan = await ci.plan_import(
        session, ci.read_rows(write_xlsx(tmp_path / "c.xlsx", rows))
    )
    assert plan.previous is None
    assert plan.cautions == ("at_cap",)
    lines = ci.plan_lines(plan)
    assert lines[-2].startswith("Birinchi import: 5000 qator")
    assert lines[-1].startswith("⚠️ Faylda 5000 qator")


async def test_a_file_with_no_tail_is_refused(session, erp_row, tmp_path):
    path = write_xlsx(
        tmp_path / "c.xlsx", [["Code", "Phones"], ["NMN11", "000-00-05"], ["NMN13", ""]]
    )
    with pytest.raises(BadFile) as caught:
        await ci.plan_import(session, ci.read_rows(path))
    assert caught.value.code == "no_phones"


# --- 05 §B.2 step 5: a full pull retires what it carries -------------------------------


def fake_with(
    cards: list[dict[str, Any]], *, at: datetime = NOW - timedelta(days=2)
) -> tuple[FakeErp, ErpClient]:
    """The ERP holding ``cards`` (edited at ``at``): its feed yields their
    tails by its own rule."""
    fake = FakeErp(SECRET, clock=lambda: NOW.astimezone(UTC))
    for card in cards:
        fake.add_client(
            card["phones"],
            active=card["active"],
            code=card["code"],
            at=at.astimezone(UTC),
        )

    async def no_sleep(_: float) -> None:
        return None

    return fake, ErpClient(BASE, SECRET, transport=fake.transport(), sleep=no_sleep)


async def test_a_full_pull_of_the_same_book_retires_every_imported_tail(session, erp_row):
    # The round trip that proves the import's tails ARE the feed's: nothing
    # imported is left behind once the feed carries the book.
    await imported(session, ci.read_rows(XLSX))
    _, erp = fake_with(BOOK["cards"])
    report = await erp_feeds.sync_suppression(
        session, erp, mode="full", now=NOW + timedelta(days=1)
    )
    assert (report.status, report.retired_manual) == ("ok", 44)
    assert await manual_tails(session) == []
    feed = await session.scalars(
        sa.select(Suppression.key).where(Suppression.source == "erp_feed")
    )
    assert set(feed) == {erp_feeds.hash_tail(SECRET, t) for t in FIXTURE_TAILS}


async def test_a_tail_the_full_pull_does_not_carry_stays(session, erp_row):
    await imported(session, ci.read_rows(XLSX))
    # Since the export NMN22's number was corrected on its card, and NMN05's
    # card lost its third number: the ERP no longer carries those tails.
    cards = [dict(c) for c in BOOK["cards"]]
    by_code = {c["code"]: c for c in cards}
    by_code["NMN22"]["phones"] = ["+998 90 000 00 76"]
    by_code["NMN05"]["phones"] = [run9(8, 9)]
    _, erp = fake_with(cards)
    report = await erp_feeds.sync_suppression(
        session, erp, mode="full", now=NOW + timedelta(days=1)
    )
    assert report.retired_manual == 41
    assert await manual_tails(session) == ["900000010", "900000053", "900000054"]
    # A delta retires nothing, whatever it carries (05 §A.7): the cards are
    # back as they were, edited after the full pull's read.
    _, erp = fake_with(BOOK["cards"], at=NOW + timedelta(minutes=30))
    report = await erp_feeds.sync_suppression(
        session, erp, mode="delta", now=NOW + timedelta(days=1, hours=1)
    )
    # 45: NMN01 and NMN23 share a number — two cards, two entries (05 §A.7).
    assert (report.walk, report.entries) == ("delta", 45)
    assert await manual_tails(session) == ["900000010", "900000053", "900000054"]


# --- the tool ---------------------------------------------------------------------------


async def test_the_tool_dry_run_writes_nothing(session, erp_row):
    code, lines = await import_clients.run(XLSX, apply=False, now=NOW)
    assert code == 0
    assert lines[:3] == [
        "Fayl: 23 ta mijoz qatori (1 tasi faol emas, 1 tasida telefon yoʻq).",
        "45 ta telefon: 44 yangi, 0 bor, 1 notoʻgʻri",
        "  12-qator NMN11: «000-00-05» — 9 raqamdan qisqa — ERP roʻyxati ham bunday "
        "raqamni bermaydi, saqlanmadi",
    ]
    assert lines[-1] == import_clients.DRY_RUN_TEXT
    assert await rows_of(session) == []
    assert await ci.last_import(session) is None


async def test_the_tool_applies_and_refuses(session, erp_row, tmp_path):
    code, lines = await import_clients.run(XLSX, apply=True, now=NOW)
    assert (code, lines[-1]) == (
        0,
        "Yozildi: 44 ta yangi raqam; 0 tasi oldingi importda bor edi, 0 tasi boshqa "
        "manbadan bor.",
    )
    assert await manual_tails(session) == sorted(FIXTURE_TAILS)
    one = write_xlsx(
        tmp_path / "one.xlsx", [["Code", "Phones"], ["NMN99", "+998 90 000 00 77"]]
    )
    code, lines = await import_clients.run(one, apply=True, now=NOW + timedelta(days=7))
    assert code == 1
    assert lines[-2:] == [import_clients.ACCEPT_HINT_TEXT, import_clients.REFUSED_TEXT]
    assert "900000077" not in await manual_tails(session)
    code, _ = await import_clients.run(
        one, apply=True, accept_fewer=True, now=NOW + timedelta(days=7)
    )
    assert code == 0
    assert "900000077" in await manual_tails(session)
    code, lines = await import_clients.run(tmp_path / "x.ods", apply=True)
    assert (code, lines) == (
        1,
        ["x.ods oʻqilmadi: Faqat .xlsx yoki .csv fayl oʻqiladi (ERP → Mijozlar → XLSX)."],
    )


@pytest.mark.skipif(shutil.which("make") is None, reason="no make")
def test_make_import_clients_passes_the_flags():
    def dry_run(*args: str) -> str:
        done = subprocess.run(
            ["make", "-s", "-n", "import-clients", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return " ".join(done.stdout.replace("\\\n", " ").split())

    tool = 'python -m clientscrapper.tools.import_clients "/data/imports/c.xlsx"'
    assert dry_run("FILE=/data/imports/c.xlsx").endswith(tool)
    assert dry_run("FILE=/data/imports/c.xlsx", "APPLY=1", "ACCEPT_FEWER=1").endswith(
        f"{tool} --apply --accept-fewer"
    )


# --- the owner's text -------------------------------------------------------------------


def test_every_owner_text_renders_clean():
    previous = ci.LastImport(rows=23, tails=44, sha256="a" * 64, at=NOW)
    plan = ci.ImportPlan(
        sha256="a" * 64,
        rows=20,
        inactive=1,
        without_phone=2,
        tails=("900000001",),
        new=("900000001",),
        same=(),
        bad=tuple(
            BadPhone(3 + i, "NMN0" + str(i), value, why)
            for i, (value, why) in enumerate(
                [
                    ("000-00-05", "short"),
                    ("yoʻq", "no_digits"),
                    ("", "precision"),
                    ("", "not_text"),
                    ("доб. 12", "short"),
                ]  # fmt: skip
            )
        ),
        previous=previous,
        refusal="fewer",
        cautions=("at_cap", "same_file"),
    )
    texts = ci.plan_lines(plan)
    assert texts[-1].startswith("⛔ Faylda oʻtgan safardan kam qator (20 < 23)")
    assert "Oʻtgan import (24.10.2026): 23 qator, 44 ta raqam." in texts
    texts += [ci.applied_line(ci.Applied(1, 2, 3))]
    texts += [BadFile(code).text() for code in ci.BAD_FILE_TEXT]
    texts += [import_clients.DRY_RUN_TEXT, import_clients.ACCEPT_HINT_TEXT]
    texts += [import_clients.REFUSED_TEXT]
    assert {
        t: problems(t, rendered=True) for t in texts if problems(t, rendered=True)
    } == {}
    # The Telegram message the bot will send fits one message even when every
    # phone of a 5,000-row file is bad.
    many = replace(
        plan, bad=tuple(BadPhone(n, "GS1000", "000-00-05", "short") for n in range(5000))
    )
    assert len("\n".join(ci.plan_lines(many))) <= 4096
    assert ci.plan_lines(many)[ci.SHOWN_BAD + 2] == "  … yana 4980 ta"
