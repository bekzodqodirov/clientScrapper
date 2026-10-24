"""The ERP contract against its own recorded answers (07 §1.7).

``tests/fixtures/erp/`` was captured from fullprompt ``agent-feed`` at
``23fe1736`` on a throwaway seeded database (still the wire at ``d138b3bc``,
05 §A.0); its README names the request, sha256 and case of every file, maps
every captured ``h`` to its synthetic tail and names the THROWAWAY key of the
dropped capture database. Three things are held here:

1. **the parsers accept every recorded body byte for byte** —
   ``SuppressionPage`` / ``parse_entry`` over every suppression page,
   ``OutcomesPage`` over both outcomes pages (each row's 15 fields; the row
   parser and the labels are T-10's), and the README's tail → hash table
   recomputes from the README's key with ``erp_feeds.hash_tail``;
2. **the recorded refusals are what the agent expects** — every POST 200
   ``{"ok":true}`` with ``cache-control: no-store`` (the replay too), every
   404 ``text/plain`` «Not found», every 400 JSON with its code;
3. **the fake ERP answers the same requests the same way**, modulo ids and
   instants: the capture's whole sequence is replayed against
   ``tests/fake_erp.py`` (its seed, every POST in order, the two «touch»
   steps, every GET) and the answers, the ledger after each POST, the
   suppression set and the outcome rows are compared with the recorded ones.

When fullprompt changes the feed, the capture is redone and this file is the
diff.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from clientscrapper.services import erp_feeds
from clientscrapper.services.erp_feeds import (
    FEED_RULE,
    OutcomesPage,
    SuppressionPage,
    parse_entry,
    parse_page,
)
from tests import fake_erp
from tests.fake_erp import FakeErp

FIXTURES = Path(__file__).parent / "fixtures" / "erp"
README = (FIXTURES / "README.md").read_text(encoding="utf-8")
SALT = "7a659490"
MS_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
OUTCOME_FIELDS = (
    "external_id",
    "intake_id",
    "outcome",
    "reason",
    "lead_id",
    "client_id",
    "stage_kind",
    "stage_name",
    "lost_reason",
    "no_contact",
    "closed_at",
    "contacted_at",
    "contact_kind",
    "quoted_volume_m3",
    "at",
)
SUPPRESSION_FILES = (
    "suppression_full.json",
    "suppression_page1.json",
    "suppression_page2.json",
    "suppression_resume_empty.json",
    "suppression_delta.json",
)
OUTCOMES_FILES = ("outcomes.json", "outcomes_since.json")
# README «What was done» 4: the delta anchor, between touch1 and touch2.
ANCHOR = "2026-10-24T08:56:34.544Z"


# --- the README -------------------------------------------------------------------


def readme_key() -> str:
    """The THROWAWAY key of the dropped capture database (not a credential)."""
    m = re.search(r"Agent door key .*?database\): `([^`]+)`", README, re.S)
    assert m is not None
    return m.group(1)


@dataclass(frozen=True, slots=True)
class TailRow:
    tail: str
    h: str
    kind: str
    in_full: int
    in_delta: int


def tail_table() -> list[TailRow]:
    rows = re.findall(
        r"^\| `(\d{9})` \| `([0-9a-f]{20})` \| (client|lost|—) \| (\d) \| (\d) \|",
        README,
        re.M,
    )
    return [TailRow(t, h, k, int(f), int(d)) for t, h, k, f, d in rows]


@dataclass(frozen=True, slots=True)
class Listed:
    name: str
    request: str
    sha256: str
    status: int
    size: int


def listed_files() -> dict[str, Listed]:
    found = re.findall(
        r"^- `([^`]+)` · \S+ · (.+?) · sha256 `([0-9a-f]{64})` · (\d{3}) · (\d+) bytes",
        README,
        re.M,
    )
    return {n: Listed(n, r, s, int(st), int(b)) for n, r, s, st, b in found}


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_bytes())


def query_of(name: str) -> dict[str, str]:
    """The query the README says a file's request sent."""
    request = listed_files()[name].request
    m = re.match(r"\S+ (\S+?)(?:\?(\S+))? \(", request)
    assert m is not None, request
    return dict(p.split("=", 1) for p in (m.group(2) or "").split("&") if p)


@dataclass(frozen=True, slots=True)
class Raw:
    status: int
    headers: dict[str, list[str]]
    body: bytes


def raw_answer(name: str) -> Raw:
    """A ``curl -i``-style capture: status line, headers, blank line, body."""
    head, _, body = (FIXTURES / name).read_bytes().partition(b"\r\n\r\n")
    if not body and b"\n\n" in head:
        head, _, body = head.partition(b"\n\n")
    lines = head.decode().splitlines()
    status = int(lines[0].split()[1])
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers.setdefault(k.strip().lower(), []).append(v.strip())
    return Raw(status, headers, body.strip())


# --- 0. the capture is what its README says ------------------------------------------


def test_every_file_is_listed_with_its_sha256():
    files = sorted(p.name for p in FIXTURES.iterdir() if p.name != "README.md")
    listed = listed_files()
    assert len(files) == 38
    assert sorted(listed) == files
    for name in files:
        data = (FIXTURES / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == listed[name].sha256, name
        assert len(data) == listed[name].size, name


def test_the_readme_key_is_the_capture_s_salt():
    assert erp_feeds.salt_id(readme_key()) == SALT
    assert fake_erp.salt_id_for(readme_key()) == SALT


def test_every_hash_in_the_table_recomputes_from_its_tail():
    rows = tail_table()
    assert len(rows) == 16
    key = readme_key()
    for row in rows:
        assert erp_feeds.hash_tail(key, row.tail) == row.h, row.tail
        assert fake_erp.hash_tail(key, row.tail) == row.h, row.tail


def test_the_full_pull_and_the_delta_hold_exactly_the_table():
    full = Counter(e["h"] for e in load("suppression_full.json")["entries"])
    delta = Counter(e["h"] for e in load("suppression_delta.json")["entries"])
    for row in tail_table():
        assert full[row.h] == row.in_full, row.tail
        assert delta[row.h] == row.in_delta, row.tail
    assert sum(full.values()) == 9
    assert sum(delta.values()) == 3
    # Not deduplicated: card GS100 and the won agent lead it was minted from.
    assert full["534dd284275834ecc27f"] == 2
    kinds = {e["h"]: e["kind"] for e in load("suppression_full.json")["entries"]}
    assert {r.h: r.kind for r in tail_table() if r.in_full} == kinds


# --- 1. the parsers over every recorded body --------------------------------------------


@pytest.mark.parametrize("name", SUPPRESSION_FILES)
def test_every_suppression_page_parses(name):
    body = load(name)
    page = parse_page(SuppressionPage, body)
    assert page.rule == FEED_RULE
    assert page.salt_id == SALT
    assert page.mode == ("delta" if name == "suppression_delta.json" else "full")
    assert MS_Z.match(page.generated_at)
    assert page.next is None or isinstance(page.next, str)
    assert "resume" in body
    entries = [parse_entry(e) for e in page.entries]
    assert [(e.h, e.kind) for e in entries] == [
        (e["h"], e["kind"]) for e in body["entries"]
    ]
    for raw, parsed in zip(body["entries"], entries, strict=True):
        assert MS_Z.match(raw["at"])
        assert parsed.at == datetime.fromisoformat(raw["at"])
        assert parsed.at.tzinfo is not None


def test_the_entries_decode_to_concrete_values():
    entries = [parse_entry(e) for e in load("suppression_full.json")["entries"]]
    assert entries[0].h == "bbc9434b92f2f7f81776"
    assert entries[0].kind == "client"
    assert entries[0].at == datetime(2026, 10, 24, 8, 56, 8, 541000, tzinfo=UTC)
    assert entries[3].kind == "lost"
    assert entries[-1].at == datetime(2026, 10, 24, 8, 56, 37, 214000, tzinfo=UTC)


@pytest.mark.parametrize("name", OUTCOMES_FILES)
def test_every_outcomes_page_parses(name):
    body = load(name)
    page = parse_page(OutcomesPage, body)
    assert MS_Z.match(page.generated_at)
    assert page.next is None
    assert page.resume is not None
    for row in page.rows:
        assert tuple(row) == OUTCOME_FIELDS
        assert not erp_feeds.PII_KEYS & set(row)
        assert row["external_id"].startswith("cs:")
        for at in ("closed_at", "contacted_at", "at"):
            assert row[at] is None or MS_Z.match(row[at])


def test_the_outcome_rows_say_what_the_readme_says():
    rows = {r["external_id"][: len("cs:") + 36]: r for r in load("outcomes.json")["rows"]}

    def row(n: int) -> dict[str, Any]:
        return rows[f"cs:00000000-0000-4000-8000-{n:012d}"]

    assert (row(21)["outcome"], row(21)["contact_kind"]) == ("created", "note")
    assert (row(22)["stage_kind"], row(22)["stage_name"]) == ("won", "Sotuv")
    assert row(22)["client_id"] is not None
    assert (row(23)["lost_reason"], row(23)["no_contact"]) == ("Narx qimmat", False)
    assert (row(24)["lost_reason"], row(24)["stage_kind"]) == (None, "lost")
    assert (row(25)["lost_reason"], row(25)["no_contact"]) == ("Qayta qilinmasin", True)
    assert (row(26)["outcome"], row(26)["stage_kind"]) == ("joined", "open")
    assert row(33)["reason"] == "capped"
    assert row(36)["outcome"] == "client"
    assert all(r["quoted_volume_m3"] is None for r in rows.values())
    assert len(rows) == 16


def test_a_pii_key_on_a_recorded_entry_is_a_shape_error():
    entry = dict(load("suppression_full.json")["entries"][0])
    for key in ("phone", "name", "note", "Phone"):
        with pytest.raises(erp_feeds.FeedShapeError):
            parse_entry({**entry, key: "x"})
    assert parse_entry({**entry, "added_later": 1}).h == entry["h"]


@pytest.mark.parametrize("missing", ["mode", "generated_at", "salt_id", "rule", "entries",
                                     "next", "resume"])  # fmt: skip
def test_a_page_without_one_of_its_keys_is_a_shape_error(missing):
    body = dict(load("suppression_full.json"))
    del body[missing]
    with pytest.raises(erp_feeds.FeedShapeError):
        parse_page(SuppressionPage, body)


# --- cursors ------------------------------------------------------------------------


def sent_cursors() -> dict[str, str]:
    """Every cursor a captured request sent, by file."""
    found: dict[str, str] = {}
    for name, listed in listed_files().items():
        m = re.search(r"[?&]cursor=([A-Za-z0-9_-]+)", listed.request)
        if m:
            found[name] = m.group(1)
    return found


def test_every_captured_cursor_decodes_to_microseconds_a_uuid_and_n():
    bad = {n: c for n, c in sent_cursors().items() if n.startswith("err_400_bad_cursor")}
    good = [c for n, c in sent_cursors().items() if n not in bad]
    for name in (*SUPPRESSION_FILES, *OUTCOMES_FILES):
        body = load(name)
        good += [c for c in (body["next"], body["resume"]) if c]
    # Two the requests sent, eight the bodies carry (page 1: next and resume).
    assert len(good) == 10
    for raw in good:
        decoded = fake_erp.decode_cursor(raw)
        assert isinstance(decoded, tuple), raw
        at, id_, n = decoded
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", at), raw
        assert id_ == id_.lower() and len(id_) == 36
        assert isinstance(n, int)
        # Opaque to the agent, but the fake's codec is the ERP's: the same bytes.
        assert fake_erp.encode_cursor(at, id_, n) == raw
    # The two the ERP refused: base64url «nope», and a rolled 2026-02-30.
    assert sorted(bad.values()) == sorted(
        [
            "bm9wZQ",
            "MjAyNi0wMi0zMFQwMDowMDowMC4wMDAwMDBafDAwMDAwMDAwLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwMHww",
        ]
    )
    assert [fake_erp.decode_cursor(c) for c in bad.values()] == ["invalid", "invalid"]


def test_the_walk_and_its_resume():
    full, page1, page2, empty = (
        load("suppression_full.json"),
        load("suppression_page1.json"),
        load("suppression_page2.json"),
        load("suppression_resume_empty.json"),
    )
    assert page1["next"] == page1["resume"]
    assert query_of("suppression_page2.json")["cursor"] == page1["next"]
    assert page2["next"] is None and full["next"] is None
    assert page2["resume"] == full["resume"]
    assert query_of("suppression_resume_empty.json")["cursor"] == page2["resume"]
    assert empty["entries"] == [] and empty["next"] is None
    assert empty["resume"] == query_of("suppression_resume_empty.json")["cursor"]
    assert page1["entries"] + page2["entries"] == full["entries"]
    assert fake_erp.decode_cursor(full["resume"]) == (
        "2026-10-24T08:56:37.214000Z",
        "01a16d1f-e25c-70ad-993b-ec3bf3abff3a",
        2,
    )


# --- 2. the recorded refusals and answers ---------------------------------------------


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("post_*.json")))
def test_every_post_answer_is_ok_true_no_store(name):
    capture = load(name)
    response = capture["response"]
    assert response["status"] == 200
    assert response["body"] == {"ok": True}
    assert response["headers"]["content-type"] == "application/json"
    assert response["headers"]["cache-control"] == "no-store"
    assert "<REDACTED" in capture["request"]["headers"]["X-GSR-Key"]


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("err_*404*.txt")))
def test_every_404_is_text_plain_not_found(name):
    raw = raw_answer(name)
    assert (raw.status, raw.body) == (404, b"Not found")
    assert raw.headers["content-type"] == ["text/plain;charset=UTF-8"]


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("err_400_*.txt")))
def test_every_400_is_json_with_its_code(name):
    raw = raw_answer(name)
    code = "bad_cursor" if "cursor" in name else "bad_since"
    assert raw.status == 400
    assert json.loads(raw.body) == {"error": code}
    assert raw.headers["content-type"] == ["application/json"]
    assert raw.headers["cache-control"] == ["private, no-store"]


# --- 3. the fake against the capture ----------------------------------------------------


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        self.at += timedelta(milliseconds=1)
        return self.at


OTHER_KEY = "fixture-only-other-key-NOT-a-credential"
START = datetime(2026, 10, 24, 8, 56, 8, tzinfo=UTC)


@dataclass(slots=True)
class Replayed:
    fake: FakeErp
    clock: Clock
    answers: dict[str, httpx.Response]
    ledgers: dict[str, list[dict[str, Any]]]


def seeded(clock: Clock) -> FakeErp:
    """README «What was done» 1: four cards, four leads; dictionary empty,
    the setting its default."""
    fake = FakeErp(readme_key(), other_secret=OTHER_KEY, clock=clock)
    fake.add_client(["+998 90 000 00 01", "+998 90 000 00 02"], code="NMN01")
    fake.add_client(["+998 90 000 00 03"], code="NMN03")
    fake.add_client(["+998 90 000 00 04"], active=False, code="NMN04")
    fake.add_client(["000-00-05"], code="NMN05")
    fake.add_lead("900000010", name="Namuna lid 10", stage_kind="lost",
                  lost_reason="Qayta qilinmasin")  # fmt: skip
    fake.add_lead("900000011", name="Namuna lid 11", stage_kind="lost",
                  lost_reason="Qayta qilinmasin")  # fmt: skip
    fake.add_lead("900000012", name="Namuna lid 12", stage_kind="lost",
                  lost_reason="Narx qimmat")  # fmt: skip
    fake.add_lead("900000013", name="Namuna lid 13")
    return fake


def ledger(fake: FakeErp, external_id: str) -> list[dict[str, Any]]:
    """The capture's ``erp_ledger_after``, read off the fake."""
    intake = fake.intakes.get(fake_erp.storable_text(external_id))
    if intake is None:
        return []
    return [
        {
            "outcome": intake.outcome,
            "reason": intake.reason,
            "source_key": intake.source,
            "lead": intake.lead_id is not None,
            "client": intake.client_id is not None,
        }
    ]


def posts_in_order() -> list[str]:
    names = [p.name for p in FIXTURES.glob("post_*.json")]
    return sorted(names, key=lambda n: load(n)["captured_at"])


async def replay() -> Replayed:
    clock = Clock(START)
    fake = seeded(clock)
    answers: dict[str, httpx.Response] = {}
    ledgers: dict[str, list[dict[str, Any]]] = {}
    async with httpx.AsyncClient(
        transport=fake.transport(), base_url="http://erp.test"
    ) as c:
        for name in posts_in_order():
            capture = load(name)
            request = capture["request"]
            key = OTHER_KEY if request["path"].endswith("/other") else readme_key()
            headers = {
                k: key if "REDACTED" in v else v for k, v in request["headers"].items()
            }
            answers[name] = await c.post(
                request["path"], json=request["body"], headers=headers
            )
            ledgers[name] = ledger(fake, request["body"]["external_id"])
    # README «What was done» 3, touch1.
    lead = {
        n: fake.lead_of(f"cs:00000000-0000-4000-8000-{n:012d}:202610")
        for n in range(21, 26)
    }
    clock.at = datetime(2026, 10, 24, 8, 56, 32, 990000, tzinfo=UTC)
    fake.contact("cs:00000000-0000-4000-8000-000000000021:202610", "note")
    assert lead[22] is not None and lead[23] is not None and lead[24] is not None
    fake.win(lead[22].id, code="GS100")
    fake.contact("cs:00000000-0000-4000-8000-000000000022:202610", "stage")
    fake.move(lead[24].id, "lost", reason="Namuna erkin matn: synthetic")
    fake.contact("cs:00000000-0000-4000-8000-000000000024:202610", "stage")
    fake.dictionary += ["Qayta qilinmasin", "Narx qimmat"]
    fake.move(lead[23].id, "lost", reason="Narx qimmat")
    fake.contact("cs:00000000-0000-4000-8000-000000000023:202610", "stage")
    # touch2, after the anchor.
    clock.at = datetime(2026, 10, 24, 8, 56, 37, 190000, tzinfo=UTC)
    assert lead[25] is not None
    fake.move(lead[25].id, "lost", reason="Qayta qilinmasin")
    fake.contact("cs:00000000-0000-4000-8000-000000000025:202610", "stage")
    lid11 = next(x for x in fake.leads.values() if x.name == "Namuna lid 11")
    fake.revive(lid11.id)
    nmn03 = next(c for c in fake.clients.values() if c.code == "NMN03")
    fake.edit_client(nmn03.id, phones=["+998 90 000 00 03", "+998 90 000 00 07"])
    return Replayed(fake, clock, answers, ledgers)


async def test_the_fake_answers_every_post_as_the_erp_did():
    done = await replay()
    for name in posts_in_order():
        capture, answer = load(name), done.answers[name]
        assert answer.status_code == capture["response"]["status"], name
        assert answer.json() == capture["response"]["body"], name
        for header in ("content-type", "cache-control"):
            assert answer.headers[header] == capture["response"]["headers"][header], name
        assert done.ledgers[name] == capture["erp_ledger_after"], name


async def test_the_fake_s_suppression_set_is_the_erp_s():
    done = await replay()
    erp = erp_feeds.ErpClient(
        "http://erp.test", readme_key(), transport=done.fake.transport()
    )
    async with erp:
        full = await erp.get_feed(erp_feeds.SUPPRESSION_PATH, {})
        delta = await erp.get_feed(erp_feeds.SUPPRESSION_PATH, {"since": ANCHOR})

    def kinds(body: dict[str, Any]) -> Counter[tuple[str, str]]:
        return Counter((e["h"], e["kind"]) for e in body["entries"])

    assert kinds(full) == kinds(load("suppression_full.json"))
    assert kinds(delta) == kinds(load("suppression_delta.json"))
    assert (full["mode"], delta["mode"]) == ("full", "delta")
    assert full["salt_id"] == delta["salt_id"] == SALT
    assert set(full) == set(load("suppression_full.json"))
    parse_page(SuppressionPage, full)


async def test_the_fake_s_outcome_rows_are_the_erp_s_modulo_ids_and_instants():
    done = await replay()
    erp = erp_feeds.ErpClient(
        "http://erp.test", readme_key(), transport=done.fake.transport()
    )
    async with erp:
        rows = (await erp.get_feed(erp_feeds.OUTCOMES_PATH, {}))["rows"]
        since = (await erp.get_feed(erp_feeds.OUTCOMES_PATH, {"since": ANCHOR}))["rows"]
    shape = ("outcome", "reason", "stage_kind", "stage_name", "lost_reason", "no_contact",
             "contact_kind", "quoted_volume_m3")  # fmt: skip
    present = ("lead_id", "client_id", "closed_at", "contacted_at")

    def view(row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            *(row[k] for k in shape),
            *(row[k] is not None for k in present),
        )

    recorded = {r["external_id"]: view(r) for r in load("outcomes.json")["rows"]}
    assert {r["external_id"]: view(r) for r in rows} == recorded
    assert [tuple(r) for r in rows] == [OUTCOME_FIELDS] * len(rows)
    assert [r["external_id"] for r in since] == [
        r["external_id"] for r in load("outcomes_since.json")["rows"]
    ]


async def test_the_fake_walks_and_resumes_like_the_capture():
    """The capture's own rows (ids and instants from its cursors): the same
    page split at limit 5, the same ``next`` and ``resume`` byte for byte."""
    clock = Clock(datetime(2026, 10, 24, 8, 56, 38, tzinfo=UTC))
    fake = FakeErp(readme_key(), clock=clock)
    by_h = {r.h: r.tail for r in tail_table()}

    def at(text: str) -> datetime:
        return fake_erp.instant(text)

    a, b = "01a16d1f-0000-7000-8000-00000000000a", "01a16d1f-0000-7000-8000-00000000000b"
    gs100, nmn03 = (
        "01a16d20-4209-713b-a168-55deb4790989",
        "01a16d1f-e25c-70ad-993b-ec3bf3abff3a",
    )
    rows = [
        ("bbc9434b92f2f7f81776", "client", "2026-10-24T08:56:08.541000Z", a, 1),
        ("10edb63c599ee8181a50", "client", "2026-10-24T08:56:08.541000Z", a, 2),
        ("897984ecd03eae2d5c14", "client", "2026-10-24T08:56:08.541000Z", b, 1),
        ("65165ff193bf0ae2a1f4", "lost", "2026-10-24T08:56:08.553000Z", b, 2),
        ("534dd284275834ecc27f", "client", "2026-10-24T08:56:33.028936Z", gs100, 1),
        ("534dd284275834ecc27f", "client", "2026-10-24T08:56:33.273000Z", a, 1),
        ("bdd8244b10f2ed414998", "lost", "2026-10-24T08:56:37.196000Z", a, 1),
        ("46e22afa1ddbf67af6c1", "client", "2026-10-24T08:56:37.214000Z", nmn03, 1),
        ("c0b7c4e6d58f3e0fac32", "client", "2026-10-24T08:56:37.214000Z", nmn03, 2),
    ]
    for h, kind, when, id_, n in rows:
        fake.add_entry(by_h[h], kind, at=at(when), id=id_, n=n)
    transport = fake.transport()
    async with erp_feeds.ErpClient(
        "http://erp.test", readme_key(), transport=transport
    ) as erp:
        full = await erp.get_feed(erp_feeds.SUPPRESSION_PATH, {})
        page1 = await erp.get_feed(erp_feeds.SUPPRESSION_PATH, {"limit": 5})
        page2 = await erp.get_feed(
            erp_feeds.SUPPRESSION_PATH, {"limit": 5, "cursor": page1["next"]}
        )
        empty = await erp.get_feed(
            erp_feeds.SUPPRESSION_PATH, {"limit": 5, "cursor": page2["resume"]}
        )
        delta = await erp.get_feed(erp_feeds.SUPPRESSION_PATH, {"since": ANCHOR})
    for got, name in (
        (full, "suppression_full.json"),
        (page1, "suppression_page1.json"),
        (page2, "suppression_page2.json"),
        (empty, "suppression_resume_empty.json"),
        (delta, "suppression_delta.json"),
    ):
        recorded = load(name)
        assert {k: v for k, v in got.items() if k != "generated_at"} == {
            k: v for k, v in recorded.items() if k != "generated_at"
        }, name


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("err_*.txt")))
async def test_the_fake_refuses_what_the_erp_refused(name):
    listed = listed_files()[name]
    method, target = listed.request.split(" ", 2)[:2]
    path, _, query = target.partition("?")
    fake = FakeErp(readme_key(), clock=Clock(START))
    request = listed.request
    headers: dict[str, str] = {}
    if "X-GSR-Key: <REDACTED: fixture agent key>" in request:
        headers["X-GSR-Key"] = readme_key()
    elif "X-GSR-Key: <REDACTED: a WRONG key>" in request:
        headers["X-GSR-Key"] = "a-wrong-key"
    if "X-ERP-Key: <REDACTED: fixture agent key>" in request:
        headers["X-ERP-Key"] = readme_key()
    body = re.search(r"body '(.*)'\)", request)
    async with httpx.AsyncClient(
        transport=fake.transport(), base_url="http://erp.test"
    ) as c:
        if method == "POST":
            content = body.group(1).encode() if body else b""
            answer = await c.post(path, content=content, headers=headers)
        else:
            # The query exactly as curl sent it: an unencoded «+» is a space.
            answer = await c.get(f"{path}?{query}" if query else path, headers=headers)
    raw = raw_answer(name)
    assert answer.status_code == raw.status
    assert answer.content == raw.body
    assert answer.headers["content-type"] == raw.headers["content-type"][0]
    if raw.status == 400:
        assert answer.headers["cache-control"] == raw.headers["cache-control"][0]
        assert answer.headers["etag"] == raw.headers["etag"][0]
    assert fake.intakes == {}


def test_the_readme_key_is_in_no_recorded_file():
    key = readme_key()
    for path in FIXTURES.iterdir():
        if path.name != "README.md":
            assert key not in path.read_text(encoding="utf-8"), path.name
