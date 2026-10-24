"""``derive_label`` over 02 §2.11's table (07 §4.1 T-10; 05 §B.2's picture of it).

Pure: every row goes through the real parser (``erp_feeds.parse_outcome``)
and the real rule, and the expected label and precision class are written
out. The boundaries are the spec's own: 14 days on ``contacted_at``, 30 days
on the push's ``sent_at`` — never on the row's ``at`` — and they hold in
Asia/Tashkent as in UTC. ``no_contact`` decides ``lost_no_contact``, the
stage decides ``won`` (``client_id`` does not: a lead revived from won keeps
it), and the lost-reason classes read dictionary LABELS (the ERP never sends
a seller's free text, 05 §A.9). The captured rows of
``tests/fixtures/erp/outcomes.json`` get their labels here too.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args
from zoneinfo import ZoneInfo

import pytest

from clientscrapper.db import enums
from clientscrapper.services import erp_feeds, outcomes
from clientscrapper.services.outcomes import FeedRow, carry_lost_reason, derive_label

TASHKENT = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 11, 24, 3, 0, tzinfo=TASHKENT)
PUSHED = NOW - timedelta(days=3)
FIXTURES = Path(__file__).parent / "fixtures" / "erp"
EID = "cs:00000000-0000-4000-8000-000000000101:202611"


def ms(at: datetime) -> str:
    """An instant as the feed prints it: UTC, milliseconds, Z."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def feed(**over: Any) -> dict[str, Any]:
    """A feed row: an open, untouched ``created`` lead, then ``over``."""
    row: dict[str, Any] = {
        "external_id": EID,
        "intake_id": "884a3c79-d3d6-4a9d-a027-ab33058b2988",
        "outcome": "created",
        "reason": None,
        "lead_id": "01a16d20-37ff-743a-8ca1-f16ddf450da3",
        "client_id": None,
        "stage_kind": "open",
        "stage_name": "Yangi",
        "lost_reason": None,
        "no_contact": False,
        "closed_at": None,
        "contacted_at": None,
        "contact_kind": None,
        "quoted_volume_m3": None,
        "at": ms(PUSHED),
    }
    row.update(over)
    return row


def lost(reason: str | None, *, no_contact: bool = False) -> dict[str, Any]:
    return feed(
        stage_kind="lost",
        stage_name="Yo‘qotildi",
        lost_reason=reason,
        no_contact=no_contact,
        closed_at=ms(PUSHED + timedelta(days=1)),
    )


def dropped(reason: str) -> dict[str, Any]:
    return feed(outcome="dropped", reason=reason, lead_id=None, stage_kind=None,
                stage_name=None)  # fmt: skip


def label(d: dict[str, Any], *, pushed_at: datetime = PUSHED, now: datetime = NOW):
    return derive_label(erp_feeds.parse_outcome(d), pushed_at=pushed_at, now=now)


CLIENT = "6c3f1d6e-0000-4000-8000-0000000000aa"
CONTACTED = ms(NOW - timedelta(days=2))

# 02 §2.11 — one row per line of the table, and the edges the text names.
TABLE: list[tuple[str, dict[str, Any], tuple[str, str]]] = [
    (
        "fallback client",
        feed(
            outcome="client",
            lead_id=None,
            stage_kind=None,
            stage_name=None,
            client_id=CLIENT,
        ),
        ("landed_client", "neutral"),
    ),  # fmt: skip
    ("capped", dropped("capped"), ("dropped_capped", "neutral")),
    ("belt", dropped("suppressed"), ("dropped_suppressed", "neutral")),
    ("no phone", dropped("no_phone"), ("dropped_other", "neutral")),
    ("no contact", dropped("no_contact"), ("dropped_other", "neutral")),
    ("a reason the ERP adds later", dropped("quarantined"), ("dropped_other", "neutral")),
    (
        "won",
        feed(
            stage_kind="won", stage_name="Sotuv", client_id=CLIENT, closed_at=ms(PUSHED)
        ),
        ("won", "positive"),
    ),  # fmt: skip
    (
        "won without client_id",
        feed(stage_kind="won", stage_name="Sotuv"),
        ("won", "positive"),
    ),  # fmt: skip
    ("joined, then won", feed(outcome="joined", stage_kind="won"), ("won", "positive")),
    # A lead revived from won back to open keeps its client_id: not won.
    (
        "revived from won",
        feed(client_id=CLIENT, contacted_at=CONTACTED, contact_kind="stage"),
        ("contacted_open", "pending"),
    ),  # fmt: skip
    (
        "revived from won, untouched",
        feed(client_id=CLIENT),
        ("open_untouched", "pending"),
    ),
    (
        "lost no_contact",
        lost("Qayta qilinmasin", no_contact=True),
        ("lost_no_contact", "negative"),
    ),  # fmt: skip
    # no_contact decides first, whatever the label says.
    (
        "no_contact beats a price label",
        lost("Narx qimmat", no_contact=True),
        ("lost_no_contact", "negative"),
    ),  # fmt: skip
    # The setting's label without the setting's flag (the owner unlisted it).
    ("unlisted Qayta qilinmasin", lost("Qayta qilinmasin"), ("lost_other", "neutral")),
    ("price uz", lost("Narx qimmat"), ("lost_price", "neutral")),
    ("price ru", lost("Цена высокая"), ("lost_price", "neutral")),
    ("price qimmat", lost("Juda qimmat"), ("lost_price", "neutral")),
    ("price dorogo", lost("Дорого"), ("lost_price", "neutral")),
    ("not importer uz", lost("Import qilmaydi"), ("lost_not_importer", "negative")),
    ("not importer ru", lost("Не импортирует"), ("lost_not_importer", "negative")),
    ("not importer xitoy", lost("Xitoy emas"), ("lost_not_importer", "negative")),
    ("small uz", lost("Hajm kichik"), ("lost_small", "negative")),
    ("small ru", lost("Мало груза"), ("lost_small", "negative")),
    ("wrong number uz ʻ", lost("Raqam notoʻgʻri"), ("lost_wrong_number", "negative")),
    ("wrong number ascii", lost("noto'g'ri"), ("lost_wrong_number", "negative")),
    ("wrong number ru", lost("Неверный номер"), ("lost_wrong_number", "negative")),
    ("competitor kargo", lost("Kargo kompaniya"), ("lost_competitor", "negative")),
    ("competitor cargo", lost("CARGO"), ("lost_competitor", "negative")),
    ("competitor uz", lost("Konkurent"), ("lost_competitor", "negative")),
    ("competitor ru", lost("Конкурент"), ("lost_competitor", "negative")),
    # Free text never leaves the ERP: a lost row without a label.
    ("lost free text", lost(None), ("lost_other", "neutral")),
    ("lost other label", lost("Boshqa sabab"), ("lost_other", "neutral")),
    # The ERP's normalisation: whitespace runs (no-break space too), case.
    ("normalised", lost("  NARX  qimmat "), ("lost_price", "neutral")),
    # The first class in LOST_REASON_PATTERNS' order decides.
    ("first class wins", lost("Narx qimmat, hajm kichik"), ("lost_price", "neutral")),
    (
        "contacted",
        feed(contacted_at=CONTACTED, contact_kind="call"),
        ("contacted_open", "pending"),
    ),  # fmt: skip
    (
        "contacted, joined",
        feed(outcome="joined", contacted_at=CONTACTED, contact_kind="telegram"),
        ("contacted_open", "pending"),
    ),  # fmt: skip
    ("joined untouched", feed(outcome="joined"), ("landed_joined", "neutral")),
    # A lead the ERP removed: no stage, the contact decides.
    (
        "no stage",
        feed(stage_kind=None, stage_name=None, lead_id=None),
        ("open_untouched", "pending"),
    ),  # fmt: skip
    ("untouched", feed(), ("open_untouched", "pending")),
]


@pytest.mark.parametrize(("case", "row", "expected"), TABLE, ids=[c for c, _, _ in TABLE])
def test_outcomes_labels_table(case, row, expected):
    assert label(row) == expected


def test_the_table_covers_every_label():
    assert {expected[0] for _, _, expected in TABLE} | {"stale_open"} == set(
        enums.OUTCOME_LABELS
    )


@pytest.mark.parametrize("zone", [UTC, TASHKENT])
def test_contacted_open_turns_positive_at_14_days(zone):
    now = NOW.astimezone(zone)
    at = now - timedelta(days=14)
    row = feed(contacted_at=ms(at), contact_kind="note")
    assert label(row, now=now) == ("contacted_open", "positive")
    assert label(row, now=now - timedelta(milliseconds=1)) == (
        "contacted_open",
        "pending",
    )


@pytest.mark.parametrize("zone", [UTC, TASHKENT])
def test_stale_open_ages_on_the_push_never_on_at(zone):
    now = NOW.astimezone(zone)
    month = timedelta(days=30)
    # The row's `at` is a year old, the push four days: untouched.
    old_at = feed(at=ms(now - timedelta(days=365)))
    assert label(old_at, pushed_at=now - timedelta(days=4), now=now) == (
        "open_untouched",
        "pending",
    )
    # The row moved an hour ago (a board re-order), the push is 30 days old.
    touched = feed(at=ms(now - timedelta(hours=1)))
    assert label(touched, pushed_at=now - month, now=now) == ("stale_open", "neutral")
    assert label(touched, pushed_at=now - month + timedelta(seconds=1), now=now) == (
        "open_untouched",
        "pending",
    )
    # Pushed in Tashkent's clock, judged in UTC's: one instant either way.
    assert label(touched, pushed_at=(now - month).astimezone(UTC), now=now) == (
        "stale_open",
        "neutral",
    )


def test_a_joined_or_contacted_row_never_goes_stale():
    old = NOW - timedelta(days=400)
    assert label(feed(outcome="joined"), pushed_at=old) == ("landed_joined", "neutral")
    assert label(feed(contacted_at=CONTACTED, contact_kind="call"), pushed_at=old)[0] == (
        "contacted_open"
    )


def test_the_patterns_are_the_spec_s_in_its_order():
    assert {k: v.pattern for k, v in outcomes.LOST_REASON_PATTERNS.items()} == {
        "lost_price": "narx|цена|qimmat|дорог",
        "lost_not_importer": "import qilmaydi|не импорт|xitoy emas",
        "lost_small": "hajm|kichik|мал",
        "lost_wrong_number": "raqam|номер|noto.g.ri",
        "lost_competitor": "kargo|cargo|konkurent|конкурент",
    }
    assert outcomes.PRECISION == {
        "lost_price": "neutral",
        "lost_not_importer": "negative",
        "lost_small": "negative",
        "lost_wrong_number": "negative",
        "lost_competitor": "negative",
    }


def test_the_literals_are_the_frozen_lists():
    assert get_args(outcomes.Label) == enums.OUTCOME_LABELS
    assert get_args(outcomes.PrecisionClass) == enums.PRECISION_CLASSES
    assert get_args(outcomes.Landing) == enums.LANDINGS
    assert get_args(outcomes.StageKind) == enums.STAGE_KINDS
    assert get_args(outcomes.ContactKind) == enums.CONTACT_KINDS


# --- the renamed label (verify:final): the stored label is carried -------------------


def _stored(row: FeedRow) -> dict[str, Any]:
    return {
        "stored_stage_kind": row.stage_kind,
        "stored_closed_at": row.closed_at,
        "stored_lost_reason": row.lost_reason,
    }


def test_a_renamed_label_keeps_the_text_chosen_at_the_move():
    before = erp_feeds.parse_outcome(lost("Narx qimmat"))
    after = erp_feeds.parse_outcome(lost(None))  # same lead, same decision
    carried = carry_lost_reason(after, **_stored(before))
    assert carried.lost_reason == "Narx qimmat"
    assert derive_label(carried, pushed_at=PUSHED, now=NOW) == ("lost_price", "neutral")


@pytest.mark.parametrize(
    "now_row",
    [
        # Lost again later (another decision, another closed_at): its own reason.
        lost(None) | {"closed_at": ms(PUSHED + timedelta(days=5))},
        # Revived: open, no reason.
        feed(),
        # The ERP sends a label: it stands.
        lost("Hajm kichik"),
    ],
    ids=["new decision", "revived", "a label"],
)
def test_nothing_else_is_carried(now_row):
    before = erp_feeds.parse_outcome(lost("Narx qimmat"))
    row = erp_feeds.parse_outcome(now_row)
    assert carry_lost_reason(row, **_stored(before)) == row


def test_a_free_text_loss_is_never_given_a_label():
    before = erp_feeds.parse_outcome(lost(None))
    row = erp_feeds.parse_outcome(lost(None))
    assert carry_lost_reason(row, **_stored(before)).lost_reason is None


# --- the parser (05 §A.9: exactly 15 fields) ------------------------------------------


def test_parse_outcome_reads_the_15_fields():
    row = erp_feeds.parse_outcome(
        feed(
            stage_kind="won",
            client_id=CLIENT,
            closed_at="2026-10-24T08:56:37.196Z",
            contacted_at="2026-10-24T08:56:37.201Z",
            contact_kind="stage",
            quoted_volume_m3=25,
            at="2026-10-24T08:56:37.201Z",
        )
    )
    assert row.closed_at == datetime(2026, 10, 24, 8, 56, 37, 196000, tzinfo=UTC)
    assert row.contacted_at == datetime(2026, 10, 24, 8, 56, 37, 201000, tzinfo=UTC)
    assert row.quoted_volume_m3 == Decimal("25.000")
    assert (row.outcome, row.stage_kind, row.client_id) == ("created", "won", CLIENT)
    assert tuple(feed()) == outcomes.FIELDS


@pytest.mark.parametrize("missing", outcomes.FIELDS)
def test_an_absent_field_is_a_shape_error(missing):
    d = feed()
    del d[missing]
    with pytest.raises(erp_feeds.FeedShapeError, match="misses"):
        erp_feeds.parse_outcome(d)


@pytest.mark.parametrize("pii", ["phone", "name", "note", "owner_id", "Price"])
def test_a_pii_key_is_a_shape_error(pii):
    with pytest.raises(erp_feeds.FeedShapeError, match="carries"):
        erp_feeds.parse_outcome(feed(**{pii: "x"}))


def test_an_unknown_key_is_ignored():
    row = erp_feeds.parse_outcome(feed(first_receipt_at=None))
    assert row == erp_feeds.parse_outcome(feed())


@pytest.mark.parametrize(
    "bad",
    [
        {"outcome": "replay"},
        {"stage_kind": "closed"},
        {"contact_kind": "email"},
        {"no_contact": "false"},
        {"no_contact": None},
        {"intake_id": "17"},
        {"at": "2026-10-24 08:56:37"},
        {"at": None},
        {"closed_at": 1729760197},
        {"quoted_volume_m3": "25"},
        {"quoted_volume_m3": True},
        {"quoted_volume_m3": float("nan")},
        {"quoted_volume_m3": 1e9},
        {"external_id": ""},
    ],
)
def test_a_field_of_the_wrong_form_is_a_shape_error(bad):
    with pytest.raises(erp_feeds.FeedShapeError):
        erp_feeds.parse_outcome(feed(**bad))


def test_the_kub_is_rounded_to_the_column():
    assert erp_feeds.parse_outcome(feed(quoted_volume_m3=2.5)).quoted_volume_m3 == (
        Decimal("2.500")
    )
    assert erp_feeds.parse_outcome(
        feed(quoted_volume_m3=999999999.999)
    ).quoted_volume_m3 == Decimal("999999999.999")


def test_a_row_that_is_not_an_object_is_a_shape_error():
    with pytest.raises(erp_feeds.FeedShapeError):
        erp_feeds.parse_outcome(["external_id"])


# --- the capture ----------------------------------------------------------------------


def test_the_captured_rows_get_their_labels():
    body = json.loads((FIXTURES / "outcomes.json").read_text(encoding="utf-8"))
    pushed = datetime(2026, 10, 24, 8, 56, tzinfo=UTC)
    got = {
        r["external_id"][len("cs:00000000-0000-4000-8000-") :][:12].lstrip("0"): label(
            r, pushed_at=pushed, now=pushed + timedelta(hours=1)
        )
        for r in body["rows"]
    }
    assert got == {
        "21": ("contacted_open", "pending"),
        "22": ("won", "positive"),
        "23": ("lost_price", "neutral"),
        "24": ("lost_other", "neutral"),
        "25": ("lost_no_contact", "negative"),
        "26": ("landed_joined", "neutral"),
        **{str(n): ("dropped_suppressed", "neutral") for n in range(27, 33)},
        "33": ("dropped_capped", "neutral"),
        "34": ("dropped_other", "neutral"),
        "35": ("dropped_other", "neutral"),
        "36": ("landed_client", "neutral"),
    }
