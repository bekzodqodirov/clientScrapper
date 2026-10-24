"""The database refuses the inconsistent rows directly (02 §10 AC 4).

Raw SQL on purpose: the bot, userbot and worker write raw SQL too, so the
constraints and triggers — not the ORM — are the fence. Each case runs in a
savepoint so one refusal does not end the test's transaction. Synthetic data
only (07 §1.0).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

PHONE = "+998900000001"
EXTERNAL = "cs:{}:202610"


async def _run(session: AsyncSession, sql: str, **params: Any) -> Any:
    """Execute; the first column of the first row when the statement returns rows."""
    result = await session.execute(sa.text(sql), params)
    return result.scalar() if result.returns_rows else None


async def refused(session: AsyncSession, sql: str, **params: Any) -> str | None:
    """The server's message when the statement is refused, else None. A
    constraint is named in it in double quotes, which the assertions match."""
    try:
        async with session.begin_nested():
            await session.execute(sa.text(sql), params)
    except sa.exc.DBAPIError as exc:
        return str(exc.orig)
    return None


@pytest.fixture
async def company(session: AsyncSession) -> uuid.UUID:
    return await _run(
        session,
        "INSERT INTO companies (display_name, name_key) "
        "VALUES ('Namuna Tekstil Savdo', 'namuna tekstil savdo') RETURNING id",
    )


@pytest.fixture
async def candidate(session: AsyncSession, company: uuid.UUID) -> uuid.UUID:
    return await _run(
        session,
        "INSERT INTO lead_candidates (company_id) VALUES (:c) RETURNING id",
        c=company,
    )


PERSON = (
    "INSERT INTO contacts (company_id, tier, full_name, phone, phone_kind, legal_basis, "
    "source_url, collected_at) VALUES (:c, 'person', :name, :phone, 'mobile', "
    "'public_source', 'https://t.me/namuna_optom/1', now())"
)


async def test_one_live_person_per_company(session, company):
    assert await refused(session, PERSON, c=company, name="Namuna A", phone=PHONE) is None
    second = await refused(
        session, PERSON, c=company, name="Namuna B", phone="+998900000002"
    )
    assert second is not None and '"ux_contacts_one_person"' in second
    # A second decision maker replaces the first: the old row is anonymised.
    await _run(
        session,
        "UPDATE contacts SET anonymised_at = now() WHERE company_id = :c",
        c=company,
    )
    assert (
        await refused(session, PERSON, c=company, name="Namuna B", phone="+998900000002")
        is None
    )


async def test_a_person_needs_basis_source_and_time(session, company):
    error = await refused(
        session,
        "INSERT INTO contacts (company_id, tier, full_name) VALUES (:c, 'person', "
        "'Namuna')",
        c=company,
    )
    assert error is not None and '"ck_contacts_person_basis"' in error


async def test_a_name_carrying_a_14_digit_run_is_refused(session, company):
    error = await refused(
        session, PERSON, c=company, name="Namuna 30000000000011", phone=PHONE
    )
    assert error is not None and '"ck_contacts_no_pinfl"' in error


async def test_a_company_tier_contact_carries_no_person(session, company):
    error = await refused(
        session,
        "INSERT INTO contacts (company_id, tier, full_name) VALUES (:c, 'company', "
        "'Namuna')",
        c=company,
    )
    assert error is not None and '"ck_contacts_company_no_name"' in error


async def test_phone_tail_is_generated_and_kind_goes_with_the_phone(session, company):
    await _run(session, PERSON, c=company, name="Namuna", phone=PHONE)
    tail = await _run(
        session, "SELECT phone_tail FROM contacts WHERE company_id = :c", c=company
    )
    assert tail == "900000001"
    error = await refused(
        session, "UPDATE contacts SET phone = NULL WHERE company_id = :c", c=company
    )
    assert error is not None and '"ck_contacts_phone_kind"' in error


async def test_a_handle_is_stored_only_as_its_key(session, company):
    insert = (
        "INSERT INTO contacts (company_id, tier, telegram_username) "
        "VALUES (:c, 'company', :u)"
    )
    assert await refused(session, insert, c=company, u="@Namuna_01") is not None
    assert await refused(session, insert, c=company, u="namuna_01") is None


# (table, column, extra columns for a valid row, a valid key, the constraint).
COMPANY = {"display_name": "'Namuna'", "name_key": "'namuna'"}
KEY_COLUMNS = [
    ("companies", "website_domain", COMPANY, "namuna.example.uz",
     "ck_companies_website_domain"),
    ("companies", "telegram_channel", COMPANY, "namuna_optom",
     "ck_companies_telegram_channel"),
    ("contacts", "telegram_username", {"company_id": ":c", "tier": "'company'"},
     "namuna_optom", "ck_contacts_telegram_username"),
    ("interactions", "sender_username",
     {"tg_chat_id": "-1001000000001", "tg_message_id": "7", "occurred_at": "now()",
      "prefilter_hit": "'{volume_kub}'"},
     "namuna_optom", "ck_interactions_sender_username"),
    ("suppression", "key",
     {"kind": "'dnc'", "key_type": "'tg_username'", "source": "'owner_bot'"},
     "namuna_optom", "ck_suppression_username"),
    ("suppression", "key",
     {"kind": "'dnc'", "key_type": "'domain'", "source": "'owner_bot'"},
     "namuna.example.uz", "ck_suppression_domain"),
]  # fmt: skip
# Values the key function answers NULL for: «x = NULL» is NULL, which a CHECK
# read as passed, so 02's «col = cs_key(col)» stored every one of them.
NOT_KEYS = ["namunabot", "Mixed Case!!", "instagram.com", "not a domain", "t.me", "abc"]


@pytest.mark.parametrize(("table", "column", "extra", "good", "constraint"), KEY_COLUMNS)
@pytest.mark.parametrize("bad", NOT_KEYS)
async def test_a_value_the_key_function_refuses_is_never_stored(
    session, company, table, column, extra, good, constraint, bad
):
    names = ", ".join([*extra, column])
    values = ", ".join([*extra.values(), ":v"])
    insert = f"INSERT INTO {table} ({names}) VALUES ({values})"
    params = {"c": company} if ":c" in values else {}
    error = await refused(session, insert, v=bad, **params)
    assert error is not None and f'"{constraint}"' in error, (bad, error)
    assert await refused(session, insert, v=good, **params) is None


async def test_a_key_column_may_stay_empty(session, company):
    assert (
        await refused(
            session,
            "UPDATE companies SET website_domain = NULL, telegram_channel = NULL "
            "WHERE id = :c",
            c=company,
        )
        is None
    )


async def test_one_delivered_push_per_external_id(session, company, candidate):
    insert = (
        "INSERT INTO push_log (external_id, attempt, candidate_id, company_id, "
        "phone_tail, "
        "endpoint, source_key_sent, payload_sha256, status, http_status) VALUES (:e, :a, "
        ":cand, :c, '900000001', '/api/leads/in/agent', 'agent', :sha, :s, :h)"
    )
    params = {
        "e": EXTERNAL.format(company),
        "cand": candidate,
        "c": company,
        "sha": "0" * 64,
    }
    assert await refused(session, insert, a=1, s="sent", h=200, **params) is None
    second = await refused(session, insert, a=2, s="unknown", h=None, **params)
    assert second is not None and '"ux_push_log_delivered"' in second
    # A failed attempt beside the delivered one is just history.
    assert await refused(session, insert, a=3, s="failed", h=503, **params) is None


async def test_a_superseded_delivery_frees_the_index_for_its_re_push(
    session, company, candidate
):
    """0002_erp_contract: rule 5's re-push (and /push tekshir) sets
    ``superseded_at`` on the delivered row — never ``failed``, which
    ck_push_log_sent forbids on a 2xx — and the attempt that re-sends it may
    then be delivered under the same id; a second LIVE delivery still may not."""
    insert = (
        "INSERT INTO push_log (external_id, attempt, candidate_id, company_id, "
        "phone_tail, endpoint, source_key_sent, payload_sha256, status, http_status, "
        "superseded_at) VALUES (:e, :a, :cand, :c, '900000001', "
        "'/api/leads/in/agent', 'agent', :sha, :s, :h, :gone)"
    )
    params = {
        "e": EXTERNAL.format(company),
        "cand": candidate,
        "c": company,
        "sha": "0" * 64,
    }
    first = {"a": 1, "s": "sent", "h": 200}
    assert await refused(session, insert, gone=None, **first, **params) is None
    await _run(session, "UPDATE push_log SET superseded_at = now() WHERE attempt = 1")
    re_push = {"a": 2, "s": "unknown", "h": None, "gone": None}
    assert await refused(session, insert, **re_push, **params) is None
    third = await refused(session, insert, a=3, s="sent", h=200, gone=None, **params)
    assert third is not None and '"ux_push_log_delivered"' in third


@pytest.mark.parametrize(
    ("status", "http", "ok"),
    [
        ("failed", 200, False),
        ("unknown", 200, True),
        ("sent", 500, False),
        ("sent", 201, True),
        # Allowed on purpose (02 §2.10 rule 5b): an 'unknown' row with no HTTP
        # answer (a read timeout, a crash) that an outcomes walk confirms
        # becomes 'sent' with http_status left as it was.
        ("sent", None, True),
        ("failed", None, True),
    ],
)
async def test_sent_means_2xx_and_2xx_means_sent_or_unknown(
    session, company, candidate, status, http, ok
):
    error = await refused(
        session,
        "INSERT INTO push_log (external_id, attempt, candidate_id, company_id, "
        "phone_tail, "
        "endpoint, source_key_sent, payload_sha256, status, http_status) VALUES (:e, 1, "
        ":cand, :c, '900000001', '/api/leads/in/agent', 'agent', :sha, :s, :h)",
        e=EXTERNAL.format(company),
        cand=candidate,
        c=company,
        sha="0" * 64,
        s=status,
        h=http,
    )
    assert (error is None) == ok, error
    if not ok:
        assert '"ck_push_log_sent"' in str(error)


async def test_one_live_card_of_each_kind_per_candidate(session, candidate):
    insert = "INSERT INTO lead_reviews (candidate_id, kind) VALUES (:cand, :k)"
    assert await refused(session, insert, cand=candidate, k="owner") is None
    assert await refused(session, insert, cand=candidate, k="manager") is None
    second = await refused(session, insert, cand=candidate, k="manager")
    assert second is not None and '"ux_lead_reviews_live"' in second


async def test_a_joined_chat_has_a_join_time(session):
    error = await refused(
        session,
        "INSERT INTO chat_monitors (tg_chat_id, chat_type, status) "
        "VALUES (-1001000000001, 'group', 'joined')",
    )
    assert error is not None and '"ck_chat_monitors_joined"' in error


@pytest.mark.parametrize(
    ("assignment", "constraint"),
    [
        ("state = 'pushed', score = 70, approved_at = now(), approved_by = 'owner:1', "
         "pushed_at = now()", "ck_lead_candidates_pushed"),
        ("state = 'approved', score = 70, approved_at = now()",
         "ck_lead_candidates_approved"),
        ("state = 'review'", "ck_lead_candidates_scored"),
        ("state = 'excluded'", "ck_lead_candidates_excluded"),
        ("state = 'suppressed'", "trg_lead_candidates_suppressed"),
    ],
)  # fmt: skip
async def test_candidate_state_invariants(session, candidate, assignment, constraint):
    error = await refused(
        session,
        f"UPDATE lead_candidates SET {assignment} WHERE id = :cand",
        cand=candidate,
    )
    assert error is not None and f'"{constraint}"' in error


COOLDOWN_ROW = (
    "INSERT INTO suppression (kind, key_type, key, source, expires_at) VALUES "
    "('cooldown', 'company_id', :key, 'outcomes', now() - interval '40 days') "
    "RETURNING id"
)
FEED_ROW = (
    "INSERT INTO suppression (kind, key_type, key, salt_id, source, last_seen_at) "
    "VALUES ('client', 'phone_hash', 'a1b2c3d4e5f6a7b8c9d0', '9caf06bb', 'erp_feed', "
    "now() - interval '2 days') RETURNING id"
)
# The statements that retire suppression rows: 02 §4 R6 and the full pull's
# delete of unseen feed rows (02 §2.8, 05 §B.2 step 4).
R6_COOLDOWN = (
    "DELETE FROM suppression WHERE kind = 'cooldown' "
    "AND expires_at < now() - interval '30 days'"
)
FULL_PULL_UNSEEN = (
    "DELETE FROM suppression WHERE source = 'erp_feed' "
    "AND last_seen_at < now() - interval '1 day'"
)


@pytest.mark.parametrize(
    ("row", "retire"), [(COOLDOWN_ROW, R6_COOLDOWN), (FEED_ROW, FULL_PULL_UNSEEN)]
)
async def test_retiring_a_suppression_row_keeps_its_suppressed_candidate(
    session, company, candidate, row, retire
):
    params = {"key": str(company)} if ":key" in row else {}
    suppression_id = await _run(session, row, **params)
    await _run(
        session,
        "UPDATE lead_candidates SET state = 'suppressed', suppressed_by = :s "
        "WHERE id = :cand",
        s=suppression_id,
        cand=candidate,
    )
    # 02's CHECK made this DELETE fail on the FK's own SET NULL (#809).
    assert await refused(session, retire) is None
    state, pointer = (
        await session.execute(
            sa.text("SELECT state, suppressed_by FROM lead_candidates WHERE id = :cand"),
            {"cand": candidate},
        )
    ).one()
    assert (state, pointer) == ("suppressed", None)
    # The «row is gone» state is still an ordinary row to every other write.
    assert (
        await refused(
            session,
            "UPDATE lead_candidates SET hold_until = now() WHERE id = :cand",
            cand=candidate,
        )
        is None
    )


async def test_a_candidate_enters_suppressed_only_naming_its_row(session, company):
    error = await refused(
        session,
        "INSERT INTO lead_candidates (company_id, state) VALUES (:c, 'suppressed')",
        c=company,
    )
    assert error is not None and '"trg_lead_candidates_suppressed"' in error
    suppression_id = await _run(session, COOLDOWN_ROW, key=str(company))
    assert (
        await refused(
            session,
            "INSERT INTO lead_candidates (company_id, state, suppressed_by) "
            "VALUES (:c, 'suppressed', :s)",
            c=company,
            s=suppression_id,
        )
        is None
    )


async def test_a_merge_target_cannot_be_deleted_from_under_its_sources(session, company):
    source = await _run(
        session,
        "INSERT INTO companies (display_name, name_key, state, merged_into) "
        "VALUES ('Namuna Ikki', 'namuna ikki', 'merged', :c) RETURNING id",
        c=company,
    )
    # RESTRICT names the real reason; 02's SET NULL failed on ck_companies_merged.
    error = await refused(session, "DELETE FROM companies WHERE id = :c", c=company)
    assert error is not None and '"fk_companies_merged_into"' in error
    error = await refused(
        session, "UPDATE companies SET merged_into = NULL WHERE id = :s", s=source
    )
    assert error is not None and '"ck_companies_merged"' in error
    # Undoing the merge clears both in one statement.
    assert (
        await refused(
            session,
            "UPDATE companies SET state = 'active', merged_into = NULL WHERE id = :s",
            s=source,
        )
        is None
    )
    delete = "DELETE FROM companies WHERE id = :c"
    assert await refused(session, delete, c=company) is None


# 02 §4 R6's hold sweep as T-31's retention_job must run it: push_log keeps
# its candidate (candidate_id ON DELETE RESTRICT, the ledger is append-only),
# so a hold candidate with push history (approved → 3 failed attempts →
# review → hold) is skipped, not deleted. Without the NOT EXISTS clause one
# such row aborts the whole statement.
R6_HOLD_SWEEP = (
    "DELETE FROM lead_candidates WHERE state = 'hold' "
    "AND updated_at < now() - interval '365 days' "
    "AND NOT EXISTS (SELECT 1 FROM push_log p WHERE p.candidate_id = lead_candidates.id)"
)


async def test_r6_hold_sweep_skips_candidates_with_push_history(session):
    old = "now() - interval '400 days'"
    ids = []
    for n in (1, 2):
        company_id = await _run(
            session,
            "INSERT INTO companies (display_name, name_key) "
            f"VALUES ('Namuna {n}', 'namuna {n}') RETURNING id",
        )
        candidate_id = await _run(
            session,
            "INSERT INTO lead_candidates (company_id, state, updated_at) "
            f"VALUES (:c, 'hold', {old}) RETURNING id",
            c=company_id,
        )
        ids.append((company_id, candidate_id))
    (pushed_company, pushed), (_, quiet) = ids
    await _run(
        session,
        "INSERT INTO push_log (external_id, attempt, candidate_id, company_id, "
        "phone_tail, endpoint, source_key_sent, payload_sha256, status, http_status) "
        "VALUES (:e, 3, :cand, :c, '900000001', '/api/leads/in/agent', 'agent', :sha, "
        "'failed', 503)",
        e=EXTERNAL.format(pushed_company),
        cand=pushed,
        c=pushed_company,
        sha="0" * 64,
    )
    bare = R6_HOLD_SWEEP.split(" AND NOT EXISTS", 1)[0]
    error = await refused(session, bare)
    assert error is not None and '"fk_push_log_candidate_id"' in error
    assert await refused(session, R6_HOLD_SWEEP) is None
    left = (
        await session.execute(
            sa.text("SELECT id FROM lead_candidates WHERE id IN (:a, :b)"),
            {"a": pushed, "b": quiet},
        )
    ).scalars()
    assert set(left) == {pushed}


async def test_a_second_open_candidate_per_company_is_refused(
    session, company, candidate
):
    error = await refused(
        session, "INSERT INTO lead_candidates (company_id) VALUES (:c)", c=company
    )
    assert error is not None and '"ux_lead_candidates_open"' in error


@pytest.mark.parametrize(
    ("row", "constraint"),
    [
        ("'client', 'phone_hash', 'a1b2c3d4e5f6a7b8c9d0', NULL, 'erp_feed', NULL",
         "ck_suppression_phone_hash"),
        ("'client', 'phone_tail', '900000001', NULL, 'owner_bot', NULL",
         "ck_suppression_erp_kinds"),
        ("'cooldown', 'phone_tail', '900000001', NULL, 'outcomes', NULL",
         "ck_suppression_cooldown"),
        ("'dnc', 'tg_username', '@Namuna_01', NULL, 'owner_bot', NULL",
         "ck_suppression_username"),
    ],
)  # fmt: skip
async def test_suppression_rows_are_well_formed(session, row, constraint):
    error = await refused(
        session,
        "INSERT INTO suppression (kind, key_type, key, salt_id, source, expires_at) "
        f"VALUES ({row})",
    )
    assert error is not None and f'"{constraint}"' in error


async def test_a_well_formed_hashed_suppression_row_is_accepted(session):
    assert (
        await refused(
            session,
            "INSERT INTO suppression (kind, key_type, key, salt_id, source) "
            "VALUES ('client', 'phone_hash', 'a1b2c3d4e5f6a7b8c9d0', '9caf06bb', "
            "'erp_feed')",
        )
        is None
    )


async def test_an_excerpt_over_600_is_refused(session):
    error = await refused(
        session,
        "INSERT INTO evidence (source_key, url, fetched_at, sha256, bytes, excerpt, "
        "expires_at) "
        "VALUES ('manual', 'https://namuna.example.uz/', now(), :sha, 1, :ex, now())",
        sha="a" * 64,
        ex="x" * 601,
    )
    assert error is not None and '"ck_evidence_excerpt"' in error


async def test_an_interaction_without_a_prefilter_hit_is_refused(session):
    error = await refused(
        session,
        "INSERT INTO interactions (tg_chat_id, tg_message_id, occurred_at, "
        "prefilter_hit) "
        "VALUES (-1001000000001, 1, now(), '{}')",
    )
    assert error is not None and '"ck_interactions_prefilter_nonempty"' in error


ONE_ROW = "WHERE version = 1 AND key = 'cn_origin'"


@pytest.mark.parametrize(
    "assignment",
    [
        "points = 35",
        # 02 §2.16 froze five weight columns only; each of these went through.
        "approved_at = NULL, approved_by = NULL",
        "version = 7",
        "key = 'cn_sector_prior'",
        "note = 'namuna'",
        "proposed_by = 'owner:1'",
        "approved_at = now(), approved_by = 'owner:1'",
        "created_at = now() - interval '1 day'",
    ],
)
async def test_an_approved_weights_row_refuses_every_change(session, assignment):
    error = await refused(session, f"UPDATE scoring_weights SET {assignment} {ONE_ROW}")
    assert error is not None and "is frozen" in error, error


async def test_un_approve_edit_re_approve_is_refused_at_the_first_step(session):
    steps = [
        "UPDATE scoring_weights SET approved_at = NULL, approved_by = NULL",
        "UPDATE scoring_weights SET points = 99",
        "UPDATE scoring_weights SET approved_at = now(), approved_by = 'seed'",
    ]
    error = await refused(session, f"{steps[0]} {ONE_ROW}")
    assert error is not None and "is frozen" in error
    # Even run as one transaction the first step stops it.
    with pytest.raises(sa.exc.DBAPIError, match="is frozen"):
        async with session.begin_nested():
            for step in steps:
                await session.execute(sa.text(f"{step} {ONE_ROW}"))
    points = await _run(session, f"SELECT points FROM scoring_weights {ONE_ROW}")
    assert points == 20


async def test_an_approved_weights_version_refuses_delete(session):
    error = await refused(session, "DELETE FROM scoring_weights WHERE version = 1")
    assert error is not None and "append-only" in error


async def test_a_proposed_version_is_edited_then_approved_then_frozen(session):
    # /vazn's path (02 §2.7), inside the test's rolled-back transaction.
    await _run(
        session,
        "INSERT INTO scoring_weights (version, key, group_key, points, group_cap, "
        "decay_days, proposed_by) SELECT 2, key, group_key, points, group_cap, "
        "decay_days, 'owner:1' FROM scoring_weights WHERE version = 1",
    )
    v2 = "WHERE version = 2 AND key = 'cn_origin'"
    assert await refused(session, f"UPDATE scoring_weights SET points = 18 {v2}") is None
    assert await refused(session, f"UPDATE scoring_weights SET note = 'x' {v2}") is None
    # Approval is one act with a name: half of it is refused.
    error = await refused(
        session, "UPDATE scoring_weights SET approved_at = now() WHERE version = 2"
    )
    assert error is not None and '"ck_scoring_weights_approved"' in error
    assert (
        await refused(
            session,
            "UPDATE scoring_weights SET approved_by = 'owner:1', approved_at = now() "
            "WHERE version = 2",
        )
        is None
    )
    error = await refused(session, f"UPDATE scoring_weights SET points = 17 {v2}")
    assert error is not None and "version 2 is frozen" in error


APPEND_ONLY_ROWS = {
    "usage_log": "INSERT INTO usage_log (provider, operation, cost_usd) "
    "VALUES ('anthropic', 'classify', 0)",
    "purge_log": "INSERT INTO purge_log (subject_kind, subject_hash, reason, "
    "requested_by, plan) "
    "VALUES ('retention', :sha, 'retention', 'job:retention', '{}')",
    "merge_log": "INSERT INTO merge_log (source_company_id, target_company_id, "
    "decided_by, moved) "
    "VALUES (gen_random_uuid(), :c, 'owner:1', '{}')",
    "outcomes": "INSERT INTO outcomes (external_id, intake_id, landing, erp_at, label, "
    "precision_class, raw) VALUES ('cs:x', gen_random_uuid(), 'created', now(), "
    "'open_untouched', 'pending', '{}')",
    "push_log": "INSERT INTO push_log (external_id, attempt, candidate_id, company_id, "
    "phone_tail, endpoint, source_key_sent, payload_sha256) VALUES (:e, 1, :cand, :c, "
    "'900000001', '/api/leads/in/agent', 'agent', :sha)",
}


@pytest.mark.parametrize("table", sorted(APPEND_ONLY_ROWS))
async def test_append_only_tables_refuse_delete(session, company, candidate, table):
    params = {
        "sha": "0" * 64,
        "c": company,
        "cand": candidate,
        "e": EXTERNAL.format(company),
    }
    sql = APPEND_ONLY_ROWS[table]
    used = {k: v for k, v in params.items() if f":{k}" in sql}
    await session.execute(sa.text(sql), used)
    error = await refused(session, f"DELETE FROM {table}")
    assert error is not None and f"{table} is append-only" in error


async def test_updated_at_is_the_triggers_even_for_raw_sql(session, company):
    await _run(
        session,
        "UPDATE companies SET city = 'Toshkent', updated_at = '2000-01-01' WHERE id = :c",
        c=company,
    )
    year = await _run(
        session,
        "SELECT extract(year FROM updated_at) FROM companies WHERE id = :c",
        c=company,
    )
    assert year != 2000
