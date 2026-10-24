"""The fake ERP is the ERP (05 §B.4, T-12): its codec, its door and its
landing behave as ``agent-feed`` does, so the push and feed tests that run
against it (T-09, T-10, T-28) test the contract and not a simplification.

The vectors are the ERP's own (recomputed in Node over the shipped functions,
05 §B.4 / 07 §1.5 / verify:final); the capture-driven half is
``tests/test_erp_feeds_contract.py``.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests import fake_erp
from tests.fake_erp import FakeErp, clamp_limit, decode_cursor, encode_cursor, parse_since

SECRET = "test-secret"
BASE = "http://erp.test"
NOW = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
EID = "cs:00000000-0000-4000-8000-000000000101:202610"


def body(eid: str = EID, **extra: object) -> dict[str, object]:
    return {
        "external_id": eid,
        "name": "Namuna MChJ",
        "phone": "+998 90 000 00 41",
        "note": "NEGA TOPILDI: namuna",
        "utm_source": "clientscrapper",
        **extra,
    }


def client_for(fake: FakeErp, transport: httpx.AsyncBaseTransport | None = None):
    return httpx.AsyncClient(transport=transport or fake.transport(), base_url=BASE)


async def post(fake: FakeErp, payload: object, *, source: str = "agent", **headers: str):
    async with client_for(fake) as c:
        return await c.post(
            f"/api/leads/in/{source}",
            json=payload,
            headers=headers or {"X-GSR-Key": SECRET, "X-ERP-Key": SECRET},
        )


def outcomes(fake: FakeErp) -> list[dict[str, object]]:
    return [row for _, _, row in fake.outcome_rows()]


# --- the codec ------------------------------------------------------------------------

VECTOR = (
    "MjAyNi0xMC0yM1QwOTowMDowMC4xMjM0NTZafDBmM2MxYTJlLTViNmQtNGU3Zi04YTliLTBjMWQy"
    "ZTNmNGE1Ynwy"
)
UUID = "0f3c1a2e-5b6d-4e7f-8a9b-0c1d2e3f4a5b"


def test_the_cursor_vector_round_trips():
    assert encode_cursor("2026-10-23T09:00:00.123456Z", UUID, 2) == VECTOR
    assert decode_cursor(VECTOR) == ("2026-10-23T09:00:00.123456Z", UUID, 2)
    two_part = (
        "MjAyNi0xMC0yM1QwOTowMDowMC4wMDBafDBmM2MxYTJlLTViNmQtNGU3Zi04YTliLTBjMWQyZTNm"
        "NGE1Yg"
    )
    assert decode_cursor(two_part) == ("2026-10-23T09:00:00.000Z", UUID, 0)
    assert decode_cursor(encode_cursor("2026-10-23T09:00:00Z", UUID.upper(), 7)) == (
        "2026-10-23T09:00:00Z",
        UUID,
        7,
    )


@pytest.mark.parametrize(
    "text",
    [
        "nope",
        "2026-02-30T00:00:00.000000Z|00000000-0000-4000-8000-000000000000|0",
        "2026-10-23T24:00:00.000000Z|00000000-0000-4000-8000-000000000000|0",
        "2026-10-23T09:00:60.000000Z|00000000-0000-4000-8000-000000000000|0",
        "2026-10-23T09:00:00.1234567Z|00000000-0000-4000-8000-000000000000|0",
        "2026-10-23T09:00:00+05:00|00000000-0000-4000-8000-000000000000|0",
        "2026-10-23T09:00:00Z|not-a-uuid|0",
        "2026-10-23T09:00:00Z|00000000-0000-4000-8000-000000000000|1234567",
        "2026-10-23T09:00:00Z|00000000-0000-4000-8000-000000000000|1|2",
    ],
)
def test_a_cursor_the_erp_refuses(text):
    raw = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    assert decode_cursor(raw) == "invalid"
    assert decode_cursor("bm9wZQ") == "invalid"


def test_an_empty_cursor_or_since_is_absent():
    assert decode_cursor("") is None and decode_cursor(None) is None
    assert parse_since("") is None and parse_since(None) is None


@pytest.mark.parametrize(
    ("raw", "utc"),
    [
        ("2026-10-24T05:00:00Z", "2026-10-24T05:00:00.000000Z"),
        ("2026-10-24T05:00Z", "2026-10-24T05:00:00.000000Z"),
        ("2026-10-24T05:00:00.5Z", "2026-10-24T05:00:00.500000Z"),
        ("2026-10-24T05:00:00.123456Z", "2026-10-24T05:00:00.123456Z"),
        ("2026-10-24T05:00:00+05:00", "2026-10-24T00:00:00.000000Z"),
        ("2026-10-24T05:00:00-0130", "2026-10-24T06:30:00.000000Z"),
        ("2026-10-24T05:00:00+23:59", "2026-10-23T05:01:00.000000Z"),
        ("2026-10-24T00:30:00.250+05:00", "2026-10-23T19:30:00.250000Z"),
    ],
)
def test_since_is_read_as_the_erp_reads_it(raw, utc):
    assert parse_since(raw) == utc


@pytest.mark.parametrize(
    "raw",
    [
        "yesterday",
        "0",
        "2026-10-24",
        "2026-10-24T05:00:00",
        "2026-10-24T05:00:00 05:00",
        "2026-02-30T00:00:00Z",
        "2026-10-24T24:00:00Z",
        "2026-10-24T05:60:00Z",
        "2026-10-24T05:00:60Z",
        "2026-10-24T05:00:00+24:00",
        "2026-10-24T05:00:00+12:60",
        "2026-10-24T05:00:00+1260",
        "2026-10-24T05:00:00.1234567Z",
    ],
)
def test_since_the_erp_refuses(raw):
    assert parse_since(raw) == "invalid"


@pytest.mark.parametrize(
    ("raw", "limit"),
    [
        (None, 1000),
        ("", 1000),
        (" 7 ", 7),
        ("\n7\n", 7),
        ("﻿7", 7),
        (" 7", 7),
        ("+7", 7),
        ("1e3", 1000),
        ("0x10", 16),
        ("0b11", 3),
        ("0o7", 7),
        (".5", 1000),
        ("0.5", 1000),
        ("1.9", 1),
        ("-0", 1000),
        ("-0.5", 1),
        ("-3", 1),
        ("-Infinity", 1),
        ("Infinity", 5000),
        ("5001", 5000),
        ("-0x10", 1000),
        ("7px", 1000),
        ("1_000", 1000),
    ],
)
def test_fake_clamp_limit_vectors(raw, limit):
    assert clamp_limit(raw) == limit


# --- the door -------------------------------------------------------------------------


async def test_x_erp_key_alone_and_a_body_that_is_not_json_are_404():
    fake = FakeErp(SECRET)
    for answer in (
        await post(fake, body(), **{"X-ERP-Key": SECRET}),
        await post(fake, body(), **{"X-GSR-Key": "wrong"}),
        await post(fake, body(), source="agentx"),
    ):
        assert (answer.status_code, answer.text) == (404, "Not found")
        assert answer.headers["content-type"] == "text/plain;charset=UTF-8"
    async with client_for(fake) as c:
        answer = await c.post(
            "/api/leads/in/agent", content=b"name=Namuna", headers={"X-GSR-Key": SECRET}
        )
        feed = await c.get("/api/agent/suppression", headers={"X-ERP-Key": SECRET})
        nowhere = await c.get("/api/agent/nowhere", headers={"X-GSR-Key": SECRET})
    assert (answer.status_code, feed.status_code) == (404, 404)
    assert feed.text == "Not found"
    # A path no route handles is Next's HTML page, not the routes' text.
    assert nowhere.status_code == 404
    assert nowhere.headers["content-type"].startswith("text/html")
    assert fake.intakes == {} and fake.posted == []


async def test_the_body_key_is_read_only_without_the_header():
    fake = FakeErp(SECRET)
    answer = await post(
        fake, {**body(), "key": SECRET}, **{"Content-Type": "application/json"}
    )
    assert answer.status_code == 200
    assert len(fake.intakes) == 1
    # The key never reaches the ref.
    assert "key" not in (next(iter(fake.intakes.values())).ref or {})


async def test_rotate_secret_makes_the_old_key_404_at_once():
    fake = FakeErp(SECRET)
    old_salt = fake.salt_id
    fake.rotate_secret("re-minted")
    assert (await post(fake, body())).status_code == 404
    assert fake.salt_id != old_salt == fake_erp.salt_id_for(SECRET)
    fake.rotate_secret(None)
    assert (await post(fake, body(), **{"X-GSR-Key": "re-minted"})).status_code == 404


async def test_a_replay_answers_ok_true_and_records_nothing_twice():
    fake = FakeErp(SECRET)
    first = await post(fake, body())
    again = await post(fake, body(note="qayta"))
    assert first.json() == again.json() == {"ok": True}
    assert again.headers["cache-control"] == "no-store"
    assert len(fake.intakes) == 1 and len(fake.leads) == 1
    assert [row["outcome"] for row in outcomes(fake)] == ["created"]


async def test_fake_landing_order():
    fake = FakeErp(SECRET)
    await post(fake, body(eid="cs:a:1", name=None, phone="12345"))
    await post(fake, body(eid="cs:b:1", phone="12345"))
    # no_contact is written ON CONFLICT DO NOTHING before the replay fence.
    await post(fake, body(eid="cs:a:1", name=None, phone="12345"))
    reasons = {i.external_id: (i.outcome, i.reason) for i in fake.intakes.values()}
    assert reasons == {
        "cs:a:1": ("dropped", "no_contact"),
        "cs:b:1": ("dropped", "no_phone"),
    }
    # Through the «Boshqa» door a short phone is not refused (no belt there).
    fake.secrets["other"] = "other-key"
    await post(fake, body(eid="cs:c:1", phone="12345"), source="other",
               **{"X-GSR-Key": "other-key"})  # fmt: skip
    assert fake.intakes["cs:c:1"].outcome == "created"


async def test_caps_count_rolling_24_hours_per_family():
    clock = [NOW]
    fake = FakeErp(SECRET, other_secret="other-key", clock=lambda: clock[0])
    for n in range(3):
        await post(fake, body(eid=f"cs:x:{n}"))
    await post(fake, body(eid="cs:x:3"))
    assert fake.intakes["cs:x:3"].reason == "capped"
    # A person's own enquiry on that phone is not capped by the agent's pushes.
    await post(fake, body(eid="cs:p:1"), source="other", **{"X-GSR-Key": "other-key"})
    assert fake.intakes["cs:p:1"].outcome == "joined"
    clock[0] = NOW + timedelta(hours=24, seconds=1)
    await post(fake, body(eid="cs:x:4"))
    assert fake.intakes["cs:x:4"].outcome == "joined"


async def test_the_belt_refuses_a_client_s_phone_and_a_seven_digit_card():
    fake = FakeErp(SECRET)
    fake.add_client(["+998 90 000 00 51"], active=False)
    fake.add_client(["000-00-52"])
    fake.add_lead("900000053", stage_kind="lost", lost_reason="  qayta   QILINMASIN ")
    for n in (51, 52, 53):
        await post(fake, body(eid=f"cs:s:{n}", phone=f"+998 90 000 00 {n}"))
    assert {i.reason for i in fake.intakes.values()} == {"suppressed"}
    assert fake.leads.keys() == {next(iter(fake.leads))}


async def test_mode_503_records_nothing_and_503_committed_s_retry_is_a_replay():
    fake = FakeErp(SECRET)
    fake.modes[EID] = ["503", "503"]
    assert (await post(fake, body())).status_code == 503
    assert (await post(fake, body())).json() == {"error": "unavailable"}
    assert fake.intakes == {} and fake.leads == {}
    assert (await post(fake, body())).status_code == 200
    assert len(fake.intakes) == 1

    other = "cs:00000000-0000-4000-8000-000000000102:202610"
    fake.modes[other] = "503_committed"
    answer = await post(fake, body(eid=other, phone="+998 90 000 00 42"))
    assert (answer.status_code, answer.json()) == (503, {"error": "unavailable"})
    assert (
        await post(fake, body(eid=other, phone="+998 90 000 00 42"))
    ).status_code == 503
    del fake.modes[other]
    assert (
        await post(fake, body(eid=other, phone="+998 90 000 00 42"))
    ).status_code == 200
    assert [i.outcome for i in fake.intakes.values()].count("created") == 2
    row = next(r for r in outcomes(fake) if r["external_id"] == other)
    assert (row["outcome"], row["contacted_at"]) == ("created", None)


async def test_an_orphan_s_retry_joins_it():
    fake = FakeErp(SECRET)
    fake.modes[EID] = "orphan"
    assert (await post(fake, body())).status_code == 503
    assert fake.intakes == {} and len(fake.leads) == 1
    assert (await post(fake, body())).status_code == 200
    [intake] = fake.intakes.values()
    assert (intake.outcome, intake.lead_id) == ("joined", next(iter(fake.leads)))
    assert len(fake.leads) == 1


async def test_an_orphan_moved_to_lost_makes_the_retry_a_second_lead():
    fake = FakeErp(SECRET)
    fake.modes[EID] = "orphan"
    await post(fake, body())
    fake.move(next(iter(fake.leads)), "lost", reason="Narx qimmat")
    await post(fake, body())
    assert fake.intakes[EID].outcome == "created"
    assert len(fake.leads) == 2


async def test_slow_a_second_post_waits_for_the_first_and_is_a_replay():
    fake = FakeErp(SECRET)
    fake.modes[EID] = "slow"
    timeout = fake.timeout_transport(0.05)
    async with client_for(fake, timeout) as c:
        with pytest.raises(httpx.ReadTimeout):
            await c.post(
                "/api/leads/in/agent", json=body(), headers={"X-GSR-Key": SECRET}
            )
    assert fake.intakes == {}
    second = asyncio.ensure_future(post(fake, body()))
    await asyncio.sleep(0.05)
    assert not second.done()
    fake.release(EID)
    answer = await second
    await fake.drain()
    assert answer.json() == {"ok": True}
    assert len(fake.intakes) == 1 and len(fake.leads) == 1
    assert len([p for p in fake.posted if p[0] == "agent"]) == 2


async def test_the_fallback_modes_and_their_switch_over():
    fake = FakeErp(SECRET, other_secret="other-key")
    fake.modes["cs:t:1"] = "throw200"
    fake.modes["cs:t:2"] = "throw200_orphan"
    for eid in ("cs:t:1", "cs:t:2"):
        answer = await post(fake, body(eid=eid, phone=f"+998 90 000 00 6{eid[-1]}"),
                            source="other", **{"X-GSR-Key": "other-key"})  # fmt: skip
        assert answer.json() == {"ok": True}
    assert fake.intakes == {} and len(fake.leads) == 1
    # The switch: the same ids through the agent door land — the orphan joined.
    for eid in ("cs:t:1", "cs:t:2"):
        await post(fake, body(eid=eid, phone=f"+998 90 000 00 6{eid[-1]}"))
    assert fake.intakes["cs:t:1"].outcome == "created"
    assert fake.intakes["cs:t:2"].outcome == "joined"
    # throw200 is the «Boshqa» door's answer only: on agent it is ignored.
    fake.modes["cs:t:3"] = "throw200"
    await post(fake, body(eid="cs:t:3", phone="+998 90 000 00 63"))
    assert fake.intakes["cs:t:3"].outcome == "created"


async def test_the_other_answers():
    fake = FakeErp(SECRET)
    fake.modes.update(
        {
            "cs:m:1": "3xx",
            "cs:m:2": "nonjson5xx",
            "cs:m:3": "nonjson5xx:502",
            "cs:m:4": "badjson",
            "cs:m:5": "404",
            "cs:m:6": "capped",
        }
    )
    got = {}
    for n in range(1, 7):
        got[n] = await post(fake, body(eid=f"cs:m:{n}", phone=f"+998 90 000 00 7{n}"))
    assert (got[1].status_code, got[1].headers["location"]) == (
        308,
        "https://erp.test/api/leads/in/agent",
    )
    assert (got[2].status_code, got[2].content) == (500, b"")
    assert got[3].status_code == 502 and got[3].headers["content-type"] == "text/html"
    assert (got[4].status_code, got[4].text) == (200, "<html>")
    assert got[5].status_code == 404
    assert (got[6].status_code, fake.intakes["cs:m:6"].reason) == (200, "capped")
    # badjson landed (the lead may have landed — the outcomes feed says so).
    assert set(fake.intakes) == {"cs:m:4", "cs:m:6"}


async def test_fake_external_id_storable():
    fake = FakeErp(SECRET)
    sent = "cs:00000000-0000-4000-8000-000000000103:\x00202610"
    await post(fake, body(eid=sent))
    stored = "cs:00000000-0000-4000-8000-000000000103:�202610"
    assert list(fake.intakes) == [stored]
    assert (await post(fake, body(eid=sent.replace("\x00", "�")))).status_code == 200
    assert len(fake.intakes) == 1
    assert [r["external_id"] for r in outcomes(fake)] == [stored]


async def test_the_landed_note_is_the_erp_s():
    fake = FakeErp(SECRET)
    payload = {
        "external_id": EID,
        "name": "  Namuna \t MChJ  ",
        "phone": "+998 90 000 00 44",
        "note": "NEGA TOPILDI:\nbir ikki",
        "utm_source": "clientscrapper",
        "empty": "   ",
        "kub": 2,
        "firma": "Namuna MChJ",
    }
    await post(fake, payload)
    lead = fake.lead_of(EID)
    assert lead is not None
    assert lead.name == "Namuna MChJ"
    assert lead.company == "Namuna MChJ"
    assert lead.notes == [
        "📣 agent\nNEGA TOPILDI: bir ikki utm_source: clientscrapper kub: 2 "
        "firma: Namuna MChJ"
    ]
    long = "a" * 999 + "😀"
    assert fake_erp.landed_note({"note": long}) == "a" * 999
    assert fake_erp.utf16_len(fake_erp.landed_note({"note": "😀" * 600}) or "") == 1000


# --- the feeds ---------------------------------------------------------------------


async def get(fake: FakeErp, path: str, **params: str):
    async with client_for(fake) as c:
        return await c.get(path, params=params, headers={"X-GSR-Key": SECRET})


async def test_the_cursor_is_checked_before_since():
    fake = FakeErp(SECRET)
    both = await get(fake, "/api/agent/suppression", cursor="bm9wZQ", since="0")
    assert both.json() == {"error": "bad_cursor"}
    assert (await get(fake, "/api/agent/outcomes", since="0")).json() == {
        "error": "bad_since"
    }
    assert both.headers["cache-control"] == "private, no-store"


async def test_since_is_strictly_after_and_resume_keeps_the_sent_cursor():
    clock = [NOW]
    fake = FakeErp(SECRET, clock=lambda: clock[0])
    fake.add_entry("900000081", at=NOW)
    fake.add_entry("900000082", at=NOW + timedelta(microseconds=1))
    since = fake_erp.us_text(NOW)
    delta = (await get(fake, "/api/agent/suppression", since=since)).json()
    assert delta["mode"] == "delta"
    assert [e["h"] for e in delta["entries"]] == [fake_erp.hash_tail(SECRET, "900000082")]
    empty = (await get(fake, "/api/agent/suppression", cursor=delta["resume"])).json()
    assert (empty["entries"], empty["next"], empty["resume"]) == (
        [],
        None,
        delta["resume"],
    )
    nothing = (await get(fake, "/api/agent/outcomes")).json()
    assert (nothing["rows"], nothing["next"], nothing["resume"]) == ([], None, None)


async def test_the_feed_modes():
    fake = FakeErp(SECRET)
    fake.add_entry("900000083")
    path = "/api/agent/suppression"
    fake.feed_modes[path] = ["feed_503", "feed_rule_changed", "feed_shape", "feed_3xx"]
    unavailable = await get(fake, path)
    assert (unavailable.status_code, unavailable.json()) == (
        503,
        {"error": "unavailable"},
    )
    assert (await get(fake, path)).json()["rule"] != fake_erp.HASH_RULE
    assert "phone" in (await get(fake, path)).json()["entries"][0]
    assert (await get(fake, path)).status_code == 308
    assert (await get(fake, path)).status_code == 200
    fake.feed_modes[path] = "feed_timeout"
    with pytest.raises(httpx.ReadTimeout):
        await get(fake, path)


async def test_a_big_body_is_gzipped_when_asked():
    fake = FakeErp(SECRET)
    for n in range(40):
        fake.add_entry(f"9000001{n:02d}")
    async with client_for(fake) as c:
        answer = await c.get(
            "/api/agent/suppression",
            headers={"X-GSR-Key": SECRET, "Accept-Encoding": "gzip"},
        )
    assert answer.headers["content-encoding"] == "gzip"
    assert len(answer.json()["entries"]) == 40
