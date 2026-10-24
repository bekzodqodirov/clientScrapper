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


@pytest.mark.parametrize(
    ("status", "http", "ok"),
    # Not pinned: ('sent', NULL) passes — NULL BETWEEN … is NULL, which a CHECK
    # reads as true; that is the 02 §2.10 text as written (crm_push sets both).
    [
        ("failed", 200, False),
        ("unknown", 200, True),
        ("sent", 500, False),
        ("sent", 201, True),
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
        ("state = 'suppressed'", "ck_lead_candidates_suppressed"),
    ],
)  # fmt: skip
async def test_candidate_state_invariants(session, candidate, assignment, constraint):
    error = await refused(
        session,
        f"UPDATE lead_candidates SET {assignment} WHERE id = :cand",
        cand=candidate,
    )
    assert error is not None and f'"{constraint}"' in error


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


async def test_an_approved_weights_version_is_frozen(session):
    error = await refused(
        session,
        "UPDATE scoring_weights SET points = 35 WHERE version = 1 AND key = 'cn_origin'",
    )
    assert error is not None and "is frozen" in error
    error = await refused(session, "DELETE FROM scoring_weights WHERE version = 1")
    assert error is not None and "append-only" in error
    # The note is not a weight: editing it is allowed.
    assert (
        await refused(
            session,
            "UPDATE scoring_weights SET note = 'namuna' WHERE version = 1 AND key = "
            "'cn_origin'",
        )
        is None
    )


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
