"""Shared phones (04 §2.3, 07 §1.4, 04 AC 3; Postgres).

An accountant, a broker or a business centre answers for many firms: a tail on
three unrelated live companies is a shared phone — flagged on every row that
carries it, never resolved by, never merged on. Names that score ≥ 80 against
each other are one firm seen twice and do not make a phone shared.
"""

from __future__ import annotations

import sqlalchemy as sa

from clientscrapper.db.models import Company, Contact
from clientscrapper.services import companies as c
from tests.companies_fixtures import NOW, company, contact, count, observe, parsed

TAIL = "900000077"
PHONE = "+998900000077"


async def three_firms_on_one_phone(session) -> None:
    """Three unrelated companies that each list PHONE (exact keys of their own
    put them there: a phone alone would have resolved to the first)."""
    for name in ("Namuna Tekstil Savdo", "Boshqa Mebel Uyi", "Uchinchi Qurilish Baza"):
        firm = await company(session, name)
        await c.upsert_contact(session, firm, tier="company", phone=PHONE, now=NOW)


async def resolve(session, body) -> c.Resolution:
    obs = await observe(session, body)
    result = await c.resolve_company(session, obs, source_key=obs.source_key, now=NOW)
    await session.flush()
    return result


async def shared_flags(session) -> list[bool]:
    rows = await session.scalars(
        sa.select(Contact.shared_phone)
        .where(Contact.phone_tail == TAIL)
        .execution_options(populate_existing=True)
    )
    return list(rows)


def test_names_that_match_are_one_firm():
    assert (
        c.unrelated_count(["namuna tekstil savdo", "boshqa mebel", "uchinchi qurilish"])
        == 3
    )
    assert (
        c.unrelated_count(
            ["namuna tekstil savdo", "namuna tekstil savdo plus", "boshqa mebel"]
        )
        == 2
    )
    # Labels and generic names never join anything: each is a firm of its own.
    assert (
        c.unrelated_count(["#telegram mijoz 1", "#telegram mijoz 2", "optom savdo"]) == 3
    )


async def test_the_third_unrelated_company_makes_the_phone_shared(session):
    for name in ("Namuna Tekstil Savdo", "Boshqa Mebel Uyi"):
        firm = await company(session, name)
        await contact(session, firm, phone=PHONE)
    assert await c.check_shared_phone(session, TAIL) is False
    assert await shared_flags(session) == [False, False]
    third = await company(session, "Uchinchi Qurilish Baza")
    row = await c.upsert_contact(session, third, tier="company", phone=PHONE, now=NOW)
    assert row is not None and row.shared_phone is True
    assert await shared_flags(session) == [True, True, True]


async def test_a_fourth_observation_with_a_shared_tail_does_not_resolve_by_phone(session):
    await three_firms_on_one_phone(session)
    assert await count(session, "companies") == 3
    assert await shared_flags(session) == [True, True, True]
    fourth = await resolve(
        session, parsed(name="Tortinchi Avto Qism", phones=((PHONE, "company"),))
    )
    assert fourth.created and fourth.how == "new"
    assert await count(session, "companies") == 4
    assert await shared_flags(session) == [True, True, True, True]
    # Without a name, a shared number names nobody.
    nameless = await resolve(
        session,
        parsed(
            kind="intent_post", source_key="telegram_monitor", phones=((PHONE, "person"),)
        ),
    )
    assert nameless.created and nameless.how == "new"


async def test_one_firm_seen_twice_does_not_make_a_phone_shared(session):
    for name in ("Namuna Tekstil Savdo", "Namuna Tekstil Savdo Plus", "Boshqa Mebel Uyi"):
        firm = await company(session, name)
        await contact(session, firm, phone=PHONE)
    assert await c.check_shared_phone(session, TAIL) is False
    firm = await company(session, "Uchinchi Qurilish Baza")
    await contact(session, firm, phone=PHONE)
    assert await c.check_shared_phone(session, TAIL) is True
    assert await shared_flags(session) == [True, True, True, True]


async def test_merged_companies_do_not_count(session):
    root = await company(session, "Namuna Tekstil Savdo")
    old = await company(session, "Boshqa Mebel Uyi", state="merged", merged_into=root.id)
    for firm in (root, old):
        await contact(session, firm, phone=PHONE)
    firm = await company(session, "Uchinchi Qurilish Baza")
    await contact(session, firm, phone=PHONE)
    assert await c.check_shared_phone(session, TAIL) is False


async def test_a_tail_on_two_companies_is_a_conflict_not_a_merge(session):
    a = await company(session, "Namuna Tekstil Savdo", stir="300000031")
    b = await company(session, "Boshqa Mebel Uyi")
    third = await company(session, "Uchinchi Qurilish Baza", stir="300000032")
    await contact(session, b, phone=PHONE)
    result = await resolve(
        session, parsed(stir="300000031", phones=((PHONE, "company"),))
    )
    assert (result.company.id, result.conflict) == (a.id, ("phone", b.id))
    states = set((await session.scalars(sa.select(Company.state))).all())
    assert states == {"active"} and await count(session, "merge_log") == 0
    assert await shared_flags(session) == [False, False]
    # Counted again on the next observation: a third firm makes it shared.
    again = await resolve(session, parsed(stir="300000032", phones=((PHONE, "company"),)))
    assert again.company.id == third.id and again.conflict is not None
    assert await shared_flags(session) == [True, True, True]


async def test_a_shared_phone_is_never_a_usable_lead_phone(session):
    """04 §1.1: usable = UZ mobile/landline on a live, non-shared contact. The
    scorer (T-18) and the push gate read the flag set here; this pins what
    they read: no contact on this tail is usable once it is shared."""
    await three_firms_on_one_phone(session)
    usable = await count(
        session,
        "contacts",
        "phone_tail = :t AND shared_phone = false AND anonymised_at IS NULL "
        "AND phone_kind IN ('mobile', 'landline')",
        t=TAIL,
    )
    assert usable == 0
