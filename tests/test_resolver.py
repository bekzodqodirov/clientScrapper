"""Entity resolution and merge (04 §2, 07 §1.4, 04 AC 2; Postgres).

Bands are asserted through the real ``companies._score`` (rapidfuzz over
``loose_key``): each case states the expected band and the companies it
leaves behind, never a hand-computed number. Every function takes ``now``;
synthetic data only (07 §1.0).
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import uuid
from datetime import timedelta
from typing import Any

import psycopg
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper import logs
from clientscrapper.collectors.base import Observation as Found
from clientscrapper.config import settings
from clientscrapper.db.models import (
    Company,
    CompanyAlias,
    CompanyId,
    Contact,
    Interaction,
    LeadCandidate,
    MergeLog,
    ModelJob,
    Observation,
    Signal,
    UsageLog,
)
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import companies as c
from clientscrapper.services.contacts import anonymise
from clientscrapper.services.keys import name_key
from clientscrapper.services.normalise import to_observation_json
from tests.companies_fixtures import NOW, company, contact, count, observe, parsed

SOURCE = "directories_goldenpages"


async def resolve(session: AsyncSession, body: dict[str, Any]) -> c.Resolution:
    obs = await observe(session, body)
    result = await c.resolve_company(session, obs, source_key=obs.source_key, now=NOW)
    await session.flush()
    return result


async def companies(session: AsyncSession) -> list[Company]:
    rows = await session.scalars(
        sa.select(Company).order_by(Company.first_seen_at, Company.id)
    )
    return list(rows)


# --- Pure: the band table of 07 §1.4 through the real scorer --------------------------


def candidate(
    name: str, *, city: str | None = None, age_days: int = 1
) -> c.NameCandidate:
    return c.NameCandidate(
        company_id=uuid.uuid5(uuid.NAMESPACE_DNS, name),
        keys=(name_key(name),),
        city=city,
        first_seen_at=NOW - timedelta(days=age_days),
    )


@pytest.mark.parametrize(
    ("stored", "seen", "band"),
    [
        ("«NAMUNA TEKSTIL SAVDO» MChJ", "ООО «Намуна Текстиль Савдо»", "attach"),
        ("Gozal Mebel XK", "Goʻzal Mebel Plus", "attach"),
        ("Tex Snab Import", "TexSnab", "review"),
        ("Namuna Gilam", "Namuna Gilamlar", "review"),
        ("Optom Savdo", "Optom Trade", "new"),
        ("Mebel Optom Savdo", "Mebel Optom Savdo Plus", "new"),
        ("Karimov R R", "Karimova R", "new"),
        ("Namuna Mebel", "Boshqa Mebel", "new"),
    ],
)
def test_the_band_of_each_fixture_pair(stored, seen, band):
    match = c.best_name_match(name_key(seen), [candidate(stored)])
    assert c.fuzzy_decision(match) == band


def test_the_guarded_pairs_would_attach_without_their_guard():
    # The two guard cases are not low scores in disguise: unguarded, both pairs
    # are in the attach band, so the guard is what keeps them apart.
    for stored, seen in (
        ("Mebel Optom Savdo", "Mebel Optom Savdo Plus"),
        ("Karimov R R", "Karimova R"),
    ):
        assert c._score(name_key(stored), name_key(seen)) >= c.AUTO_MERGE
        match = c.best_name_match(name_key(seen), [candidate(stored)], guarded=False)
        assert c.fuzzy_decision(match) == "attach"


def test_a_glued_spelling_stops_in_the_review_band():
    # token_set_ratio alone scores «tex snab import» / «texsnab» 45.
    assert c._score("tex snab import", "texsnab") == c.GLUED_CEILING
    assert c._score("tex snab", "texsnab") == c.GLUED_CEILING
    assert c._score("namuna tekstil", "boshqa tekstil") < c.REVIEW_LOW


def test_ambiguity_in_the_attach_band_is_a_review():
    match = c.best_name_match(
        name_key("Namuna Mebel"),
        [
            candidate("Namuna Mebel Savdo", age_days=9),
            candidate("Namuna Mebel Lux", age_days=3),
        ],
    )
    assert (match.score, match.runner_up_score, match.ambiguous) == (100.0, 100.0, True)
    assert c.fuzzy_decision(match) == "review"
    assert match.company_id == candidate("Namuna Mebel Savdo").company_id  # the older


def test_names_from_another_city_are_not_candidates():
    match = c.best_name_match(
        "namuna tekstil savdo",
        [candidate("Namuna Tekstil Savdo", city="Samarqand")],
        city="Toshkent",
    )
    assert match.company_id is None


def test_usable_name_guards():
    assert c.usable_name("namuna tekstil savdo")
    assert not c.usable_name("optom savdo")  # generic tokens only
    assert not c.usable_name("ooo")  # a legal form alone, < 4 characters
    assert not c.usable_name("#olx sotuvchi aujte")  # a label
    assert not c.usable_name(None)


@pytest.mark.parametrize(
    ("role", "rank"),
    [
        ("direktor", 3),
        ("Директор", 3),
        ("rahbari", 3),
        ("учредитель", 3),
        ("taʼminot boʻlimi", 2),
        ("Отдел закупок", 2),
        ("ВЭД менеджер", 2),
        ("menejer", 1),
        ("менеджер по продажам", 1),
        ("savdo boʻlimi boshligʻi", 1),
        ("hisobchi", 0),
        (None, 0),
        # The firm's head and owner, in the three languages a role is written in.
        ("bosh direktor", 3),
        ("Генеральный директор", 3),
        ("Владелец", 3),
        ("doʻkon egasi", 3),
        ("kompaniya rahbari", 3),
        # «bosh» is «chief», not «head of the firm»; Russian says the same.
        ("bosh hisobchi", 0),
        ("Bosh buxgalter", 0),
        ("bosh muhandis", 0),
        ("Главный бухгалтер", 0),
        # An assistant or secretary to the head is not the head.
        ("Rahbar yordamchisi", 0),
        ("direktor yordamchisi", 0),
        ("Секретарь директора", 0),
        ("помощник руководителя", 0),
    ],
)
def test_role_rank(role, rank):
    assert c.role_rank(role) == rank


def test_identity_keys_read_the_02_shape():
    keys = c.identity_keys(
        parsed(
            kind="intent_post",
            source_key="telegram_monitor",
            name="Namuna Tekstil Savdo MChJ",
            phones=(("+998900000001", "person"), ("+79260000000", "person")),
            telegram="Namuna_Tekstil",
            tg_user_id=5100000001,
            ids={"tg_chat": "-1001000000001", "olx_user_id": "AujTE"},
            website="https://shop.namuna.example.uz/contacts",
        )
    )
    assert keys.phone_tails == ("900000001",)  # a foreign number is no ERP identity
    assert keys.tg_username == "namuna_tekstil"
    assert keys.tg_user_id == 5100000001
    # The group's chat id is every poster's; a poster's id is person-tier.
    assert keys.external_ids == (("olx_user", "AujTE"),)
    # Stored as written, matched by the registrable domain (synthetic hosts
    # all fold to example.uz).
    assert (keys.domain, keys.site_key) == ("shop.namuna.example.uz", "example.uz")
    assert keys.person_side_handles


def test_identity_keys_skip_phones_of_unknown_owner_outside_posts():
    keys = c.identity_keys(
        parsed(kind="listing", name="Namuna", phones=(("+998900000002", "unknown"),))
    )
    assert keys.phone_tails == ()


def test_resolve_job_fills_the_worker_slot_as_it_is():
    """T-08's table holds ``JobSlot("resolve", ("every 2m",), "T-17")`` and
    calls its ``job`` with no argument (``Job = Callable[[], Awaitable]``);
    its listener writes ``job:resolve``. The job takes nothing it needs."""
    assert inspect.iscoroutinefunction(c.resolve_job)
    params = inspect.signature(c.resolve_job).parameters.values()
    assert all(
        p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is not p.empty
        for p in params
    )
    # No second registration home beside the worker's table.
    assert not hasattr(c, "register_jobs") and not hasattr(c, "JOBS")


# --- Name bands against the database --------------------------------------------------


async def test_an_exact_name_key_attaches_and_learns_the_alias(session):
    first = await resolve(session, parsed(name="«NAMUNA TEKSTIL SAVDO» MChJ"))
    second = await resolve(session, parsed(name="ООО «Намуна Текстиль Савдо»"))
    assert first.created and first.how == "new"
    assert (second.company.id, second.how, second.score, second.created) == (
        first.company.id,
        "fuzzy",
        100.0,
        False,
    )
    aliases = (
        await session.execute(sa.select(CompanyAlias.alias, CompanyAlias.alias_key))
    ).all()
    assert aliases == [("ООО «Намуна Текстиль Савдо»", "namuna tekstil savdo")]
    assert [x.display_name for x in await companies(session)] == [
        "«NAMUNA TEKSTIL SAVDO» MChJ"
    ]


async def test_a_close_name_attaches(session):
    first = await resolve(session, parsed(name="Gozal Mebel XK"))
    second = await resolve(session, parsed(name="Goʻzal Mebel Plus"))
    assert (second.company.id, second.how, second.created) == (
        first.company.id,
        "fuzzy",
        False,
    )
    assert second.score >= c.AUTO_MERGE
    assert len(await companies(session)) == 1
    aliases = (
        await session.execute(sa.select(CompanyAlias.company_id, CompanyAlias.alias))
    ).all()
    assert aliases == [(first.company.id, "Goʻzal Mebel Plus")]  # alias learned


async def test_the_review_band_creates_a_new_company_with_dup_of(session):
    first = await resolve(session, parsed(kind="vacancy", name="Tex Snab Import"))
    second = await resolve(session, parsed(name="TexSnab"))
    assert second.created and second.how == "new" and second.score == 0
    assert second.duplicate_of is not None and second.duplicate_of.id == first.company.id
    assert len(await companies(session)) == 2
    row = await session.get(Company, second.company.id)
    assert row.dup_of == first.company.id
    assert c.REVIEW_LOW <= row.dup_score < c.AUTO_MERGE


async def test_generic_names_never_match(session):
    await resolve(session, parsed(name="Optom Savdo"))
    await resolve(session, parsed(name="Optom Trade"))
    await resolve(session, parsed(name="Mebel Optom Savdo"))
    await resolve(session, parsed(name="Mebel Optom Savdo Plus"))
    # Not even an exact one: thousands of shops are «Optom Savdo».
    await resolve(session, parsed(name="Optom Savdo"))
    rows = await companies(session)
    assert len(rows) == 5 and all(r.dup_of is None for r in rows)


async def test_person_shaped_names_are_never_merged_below_100(session):
    # Keys set by hand: the normaliser would have moved such a name to the
    # person, the resolver's own guard is what this pins.
    await resolve(session, parsed(name="Karimov R R", key="karimov r r"))
    await resolve(session, parsed(name="Karimova R", key="karimova r"))
    rows = await companies(session)
    assert len(rows) == 2 and all(r.dup_of is None for r in rows)


async def test_ambiguous_names_land_in_the_review_band(session):
    older = await company(
        session, "Namuna Mebel Savdo", first_seen_at=NOW - timedelta(days=9)
    )
    await company(session, "Namuna Mebel Lux", first_seen_at=NOW - timedelta(days=3))
    result = await resolve(session, parsed(name="Namuna Mebel"))
    assert result.created and result.duplicate_of is not None
    assert result.duplicate_of.id == older.id
    assert result.company.dup_of == older.id


async def test_a_name_in_another_city_is_another_company(session):
    await resolve(session, parsed(name="Namuna Tekstil Savdo", city="Samarqand"))
    other = await resolve(session, parsed(name="Namuna Tekstil Savdo", city="Toshkent"))
    unknown = await resolve(session, parsed(name="Namuna Tekstil Savdo"))
    assert other.created and not unknown.created
    assert len(await companies(session)) == 2


async def test_an_exact_name_in_another_city_leaves_this_citys_attach_band(session):
    # The exact holder is another city's firm; this city's close name is still
    # looked for, and attaches (≥ 90).
    here = await company(session, "Namuna Tekstil Savdo Plus", city="Toshkent")
    await company(session, "Namuna Tekstil Savdo", city="Samarqand")
    result = await resolve(session, parsed(name="Namuna Tekstil Savdo", city="Toshkent"))
    assert (result.company.id, result.how, result.created) == (here.id, "fuzzy", False)
    assert len(await companies(session)) == 2


async def test_an_exact_name_in_another_city_leaves_this_citys_review_band(session):
    here = await company(session, "Namuna Gilam", city="Toshkent")
    await company(session, "Namuna Gilamlar", city="Samarqand")
    result = await resolve(session, parsed(name="Namuna Gilamlar", city="Toshkent"))
    assert result.created and result.duplicate_of is not None
    assert (result.duplicate_of.id, result.company.dup_of) == (here.id, here.id)
    assert len(await companies(session)) == 3


# --- Exact keys -----------------------------------------------------------------------


async def test_stir_beats_a_conflicting_phone_and_nothing_is_merged(session):
    a = await company(session, "Namuna Tekstil Savdo", stir="300000011")
    b = await company(session, "Boshqa Mebel Uyi")
    await contact(session, b, phone="+998900000002")
    result = await resolve(
        session,
        parsed(
            name="Namuna Tekstil",
            stir="300000011",
            phones=(("+998900000002", "company"),),
        ),
    )
    assert (result.company.id, result.how) == (a.id, "stir")
    assert result.conflict == ("phone", b.id)
    states = dict((await session.execute(sa.select(Company.id, Company.state))).all())
    assert states == {a.id: "active", b.id: "active"}
    assert await count(session, "merge_log") == 0
    # The phone is a shared-phone candidate, carried to the winner too.
    assert await count(session, "contacts", "phone_tail = '900000002'") == 2
    assert (await session.get(Company, b.id)).dup_of is None  # names far apart


async def test_a_conflict_between_close_names_proposes_the_merge(session):
    a = await company(session, "Namuna Tekstil Savdo", stir="300000012")
    b = await company(session, "Namuna Tekstil Savdo Plus")
    await contact(session, b, phone="+998900000003")
    result = await resolve(
        session, parsed(stir="300000012", phones=(("+998900000003", "company"),))
    )
    assert result.company.id == a.id and result.conflict == ("phone", b.id)
    refreshed = await session.get(Company, b.id)
    assert refreshed.dup_of == a.id and refreshed.dup_score >= c.REVIEW_LOW


async def test_a_conflicting_handle_is_not_carried(session):
    a = await company(session, "Namuna Tekstil Savdo", stir="300000013")
    b = await company(session, "Boshqa Mebel Uyi", telegram_channel="boshqa_mebel")
    result = await resolve(session, parsed(stir="300000013", telegram="boshqa_mebel"))
    assert result.company.id == a.id and result.conflict == ("telegram", b.id)
    assert (await session.get(Company, a.id)).telegram_channel is None


async def test_a_conflicting_site_is_not_carried(session):
    a = await company(session, "Namuna Tekstil Savdo")
    await contact(session, a, phone="+998900000051")
    b = await company(session, "Boshqa Mebel Uyi", website_domain="namuna.example.uz")
    result = await resolve(
        session,
        parsed(
            name="Namuna Tekstil Savdo",
            phones=(("+998900000051", "company"),),
            website="https://namuna.example.uz",
        ),
    )
    assert (result.company.id, result.how, result.conflict) == (
        a.id,
        "phone",
        ("domain", b.id),
    )
    assert (await session.get(Company, a.id)).website_domain is None


async def test_a_posters_telegram_id_outranks_a_stir_which_is_not_carried(session):
    # 04 §2.6: the poster's Telegram id is MIYA's shortcut, before the STIR.
    a = await company(session, "Namuna Tekstil Savdo")
    await contact(session, a, tier="person", telegram_id=5100000009, full_name="Namuna")
    b = await company(session, "Boshqa Mebel Uyi", stir="300000091")
    result = await resolve(
        session,
        parsed(
            kind="intent_post",
            source_key="telegram_monitor",
            tg_user_id=5100000009,
            stir="300000091",
        ),
    )
    assert (result.company.id, result.how, result.conflict) == (
        a.id,
        "telegram",
        ("stir", b.id),
    )
    assert (await session.get(Company, a.id)).stir is None


async def test_a_purged_companys_site_is_never_carried_nor_a_conflict(session):
    a = await company(session, "Namuna Tekstil Savdo")
    await contact(session, a, phone="+998900000052")
    await company(
        session, "[oʻchirilgan]", website_domain="namuna.example.uz", state="purged"
    )
    # ux_companies_domain still holds the purged row's domain: carrying it
    # would refuse the whole observation.
    result = await resolve(
        session,
        parsed(
            name="Namuna Tekstil Savdo",
            phones=(("+998900000052", "company"),),
            website="https://namuna.example.uz",
        ),
    )
    assert (result.company.id, result.how, result.conflicts) == (a.id, "phone", ())
    assert (await session.get(Company, a.id)).website_domain is None


async def test_a_purged_companys_stir_is_never_carried_nor_a_conflict(session):
    a = await company(session, "Namuna Tekstil Savdo")
    await contact(session, a, tier="person", telegram_id=5100000010, full_name="Namuna")
    await company(session, "[oʻchirilgan]", stir="300000092", state="purged")
    result = await resolve(
        session,
        parsed(
            kind="intent_post",
            source_key="telegram_monitor",
            tg_user_id=5100000010,
            stir="300000092",
        ),
    )
    assert (result.company.id, result.how, result.conflicts) == (a.id, "telegram", ())
    assert (await session.get(Company, a.id)).stir is None


async def test_a_returning_poster_is_found_by_their_username(session):
    first = await resolve(
        session,
        parsed(
            kind="intent_post",
            source_key="telegram_monitor",
            name="Namuna Tekstil Savdo",
            telegram="namuna_xaridor",
        ),
    )
    # A later post: no Telegram id, no firm name — only the username, which
    # sits on the poster's contact, not on companies.telegram_channel.
    again = await resolve(
        session,
        parsed(
            kind="intent_post", source_key="telegram_monitor", telegram="namuna_xaridor"
        ),
    )
    assert first.company.telegram_channel is None
    assert (again.company.id, again.how, again.created) == (
        first.company.id,
        "telegram",
        False,
    )


async def test_a_second_instagram_account_is_found_through_its_contact(session):
    target = await company(session, "Namuna Tekstil Savdo", instagram="namuna.tekstil")
    # The same firm's second account: a company-tier contact, not the column.
    first = await resolve(
        session, parsed(name="Namuna Tekstil Savdo", instagram="namuna.tekstil.optom")
    )
    assert first.company.id == target.id
    again = await resolve(session, parsed(instagram="namuna.tekstil.optom"))
    assert (again.company.id, again.how, again.created) == (target.id, "instagram", False)


async def test_an_anonymised_contacts_landline_names_nobody(session):
    firm = await company(session, "Namuna Avto Qismlar", city="Toshkent")
    # anonymise keeps a landline (02 §4 R4); it is no key any more (04 §2.1).
    await contact(session, firm, tier="person", phone="+998712000071", anonymised_at=NOW)
    result = await resolve(
        session,
        parsed(
            name="Boshqa Firma Uyi",
            city="Toshkent",
            phones=(("+998712000071", "company"),),
        ),
    )
    assert result.created and result.how == "new"


async def test_the_winner_gains_only_the_fields_it_lacked(session):
    firm = await company(session, "Namuna Tekstil Savdo")
    await contact(session, firm, phone="+998900000022")
    await resolve(
        session,
        parsed(
            name="Namuna Tekstil Savdo",
            city="Toshkent",
            legal_form="MChJ",
            phones=(("+998900000022", "company"),),
        ),
    )
    await resolve(
        session,
        parsed(name="Namuna Tekstil Savdo", city="Samarqand", legal_form="XK"),
    )
    row = await session.get(Company, firm.id)
    assert (row.city, row.legal_form) == ("Toshkent", "MChJ")


async def test_an_exact_key_never_teaches_another_firms_name(session):
    a = await company(session, "Namuna Tekstil Savdo")
    # The firm's number is also its accountant's, who keeps another firm's books.
    await contact(session, a, phone="+998900000061")
    first = await resolve(
        session, parsed(name="Boshqa Mebel Uyi", phones=(("+998900000061", "company"),))
    )
    assert (first.company.id, first.how) == (a.id, "phone")
    assert await count(session, "company_aliases") == 0
    again = await resolve(
        session, parsed(name="Boshqa Mebel Uyi", phones=(("+998900000062", "company"),))
    )
    assert again.created and again.company.id != a.id


async def test_an_exact_key_learns_a_spelling_of_its_own_name(session):
    a = await company(session, "Namuna Tekstil Savdo")
    await contact(session, a, phone="+998900000063")
    result = await resolve(
        session,
        parsed(
            name="ООО «Намуна Текстиль Савдо»", phones=(("+998900000063", "company"),)
        ),
    )
    assert (result.company.id, result.how) == (a.id, "phone")
    aliases = (await session.execute(sa.select(CompanyAlias.alias))).scalars().all()
    assert aliases == ["ООО «Намуна Текстиль Савдо»"]


async def test_an_exact_key_under_another_known_firms_name_is_a_conflict(session):
    a = await company(session, "Namuna Tekstil Savdo")
    await contact(session, a, phone="+998900000064")
    b = await company(session, "Boshqa Mebel Uyi")
    result = await resolve(
        session, parsed(name="Boshqa Mebel Uyi", phones=(("+998900000064", "company"),))
    )
    # The phone wins (04 §2.1); the name, the ladder's last key, names B.
    assert (result.company.id, result.how, result.conflict) == (
        a.id,
        "phone",
        ("fuzzy", b.id),
    )
    assert await count(session, "company_aliases") == 0
    assert await count(session, "merge_log") == 0


async def test_a_labelled_poster_learns_the_name_it_later_gives(session):
    first = await resolve(
        session,
        parsed(kind="intent_post", source_key="telegram_monitor", tg_user_id=5100000031),
    )
    assert first.company.name_key.startswith(c.LABEL_PREFIX)
    named = await resolve(
        session,
        parsed(
            kind="intent_post",
            source_key="telegram_monitor",
            tg_user_id=5100000031,
            name="Namuna Tekstil Savdo",
        ),
    )
    assert (named.company.id, named.how) == (first.company.id, "telegram")
    aliases = (await session.execute(sa.select(CompanyAlias.alias))).scalars().all()
    assert aliases == ["Namuna Tekstil Savdo"]


async def test_a_landline_tail_needs_the_city(session):
    samarqand = await company(session, "Namuna Avto Qismlar", city="Samarqand")
    await contact(session, samarqand, phone="+998712000001")
    elsewhere = await resolve(
        session,
        parsed(
            name="Boshqa Firma Uyi",
            city="Toshkent",
            phones=(("+998712000001", "company"),),
        ),
    )
    assert elsewhere.created and elsewhere.how == "new"
    same = await resolve(
        session,
        parsed(
            name="Uchinchi Nom", city="Samarqand", phones=(("+998712000001", "company"),)
        ),
    )
    unknown = await resolve(session, parsed(phones=(("+998712000001", "company"),)))
    assert (same.company.id, same.how) == (samarqand.id, "phone")
    assert unknown.company.id == samarqand.id


async def test_every_exact_key_of_the_ladder_resolves(session):
    target = await company(
        session,
        "Namuna Tekstil Savdo",
        telegram_channel="namuna_tekstil",
        website_domain="namuna.example.uz",
        instagram="namuna.tekstil",
    )
    session.add(CompanyId(company_id=target.id, id_type="hh_employer", id_value="7781"))
    await contact(session, target, phone="+998900000004")
    await session.flush()
    cases = {
        "phone": parsed(phones=(("+998900000004", "company"),)),
        "telegram": parsed(kind="profile", telegram="namuna_tekstil"),
        "domain": parsed(website="https://shop.namuna.example.uz/contacts"),
        "instagram": parsed(instagram="namuna.tekstil"),
        "external_id": parsed(kind="vacancy", ids={"hh_employer_id": "7781"}),
    }
    for how, body in cases.items():
        result = await resolve(session, body)
        assert (result.company.id, result.how, result.created) == (target.id, how, False)
    assert len(await companies(session)) == 1


async def test_a_posters_telegram_id_short_cuts_the_ladder(session):
    first = await resolve(
        session,
        parsed(
            kind="intent_post",
            source_key="telegram_monitor",
            name="Namuna Tekstil Savdo",
            telegram="namuna_tekstil",
            tg_user_id=5100000001,
        ),
    )
    # The username changed; the id did not.
    second = await resolve(
        session,
        parsed(
            kind="intent_post",
            source_key="telegram_monitor",
            telegram="namuna_yangi",
            tg_user_id=5100000001,
        ),
    )
    assert (second.company.id, second.how) == (first.company.id, "telegram")
    assert await count(session, "company_ids") == 0  # the id is person-tier


async def test_a_match_through_a_merged_company_follows_it_to_the_root(session):
    root = await company(session, "Namuna Tekstil Savdo", instagram="namuna.yangi")
    # The target has an account of its own, so this one stays on the source.
    old = await company(session, "Namuna Tekstil", instagram="namuna.eski")
    await c.merge_into(session, old, root, by="owner:111111111", now=NOW)
    result = await resolve(session, parsed(instagram="namuna.eski"))
    assert (result.company.id, result.how, result.created) == (
        root.id,
        "instagram",
        False,
    )


async def test_a_purged_company_is_not_collected_again(session):
    await company(session, "Namuna Tekstil Savdo", stir="300000014", state="purged")
    with pytest.raises(c.ObservationRejected) as refused:
        await resolve(session, parsed(name="Namuna Tekstil", stir="300000014"))
    assert refused.value.reason == "purged"


@pytest.mark.parametrize(
    "body",
    [
        parsed(name="GSR Logistics"),
        parsed(name="Imex Services MChJ"),
        parsed(name="Namuna Kargo", website="https://gsrwms.uz/login"),
        parsed(kind="profile", name="Namuna Kanal", telegram="gsrlogistics"),
        parsed(name="GSR Logistiqa"),  # q/k: equal only under loose_key
    ],
)
async def test_gsr_itself_never_becomes_a_company(session, body):
    with pytest.raises(c.ObservationRejected) as refused:
        await resolve(session, body)
    assert refused.value.reason == "competitor"
    assert await count(session, "companies") == 0


async def test_nothing_to_resolve_is_no_company(session):
    with pytest.raises(c.ObservationRejected) as refused:
        await resolve(
            session, parsed(kind="listing", phones=(("+998900000006", "unknown"),))
        )
    assert refused.value.reason == "no_company"


async def test_a_nameless_poster_gets_a_label_never_their_handle(session):
    result = await resolve(
        session,
        parsed(
            kind="intent_post",
            source_key="telegram_monitor",
            telegram="namuna_xaridor",
            phones=(("+998900000007", "person"),),
        ),
    )
    row = result.company
    assert (
        row.display_name.startswith("Telegram mijoz #")
        and "namuna" not in row.display_name
    )
    assert row.name_key.startswith(c.LABEL_PREFIX)
    # Two labels share their words and must never fuzzy-match each other.
    other = await resolve(
        session,
        parsed(
            kind="intent_post", source_key="telegram_monitor", telegram="namuna_boshqa"
        ),
    )
    assert other.created and other.company.dup_of is None


async def test_a_person_shaped_olx_seller_resolves_by_its_id(session):
    body = parsed(
        kind="seller",
        source_key="olx_business",
        name="OLX sotuvchi AujTE",
        key=None,
        ids={"olx_user_id": "AujTE"},
        telegram="sardor_optom_tex",
        person={"name": "Sardor", "role": None},
    )
    first = await resolve(session, body)
    again = await resolve(
        session,
        parsed(
            kind="seller",
            source_key="olx_business",
            name="OLX sotuvchi AujTE",
            key=None,
            ids={"olx_user_id": "AujTE"},
        ),
    )
    assert first.created and (again.company.id, again.how) == (
        first.company.id,
        "external_id",
    )
    row = first.company
    assert (row.display_name, row.telegram_channel) == ("OLX sotuvchi AujTE", None)
    person = first.contact
    assert person is not None and (
        person.tier,
        person.full_name,
        person.telegram_username,
    ) == (
        "person",
        "Sardor",
        "sardor_optom_tex",
    )
    assert await count(session, "company_aliases") == 0  # a label is no spelling


# --- Contacts inside a company (04 §2.4, 06 §5.1) -------------------------------------


async def test_one_person_per_company_and_a_better_one_replaces_it(session):
    owner = await company(session, "Namuna Tekstil Savdo")
    url = "https://t.me/namuna_optom/18234"
    first = await c.upsert_contact(
        session,
        owner,
        tier="person",
        phone="+998900000011",
        full_name="Namuna Bir",
        role="menejer",
        source_url=url,
        source_key="telegram_monitor",
        now=NOW,
    )
    # A lower-ranked newcomer: their phone is kept as the firm's, no name.
    weaker = await c.upsert_contact(
        session,
        owner,
        tier="person",
        phone="+998900000012",
        full_name="Namuna Ikki",
        role="hisobchi",
        source_url=url,
        source_key="telegram_monitor",
        now=NOW,
    )
    assert (weaker.tier, weaker.full_name, weaker.phone) == (
        "company",
        None,
        "+998900000012",
    )
    better = await c.upsert_contact(
        session,
        owner,
        tier="person",
        phone="+998900000013",
        full_name="Namuna Uch",
        role="direktor",
        source_url=url,
        source_key="telegram_monitor",
        now=NOW,
    )
    await session.refresh(first)
    assert (first.full_name, first.phone, first.anonymised_at) == (None, None, NOW)
    assert (better.tier, better.full_name, better.is_decision_maker) == (
        "person",
        "Namuna Uch",
        True,
    )
    assert (better.legal_basis, better.source_url, better.collected_at) == (
        "public_source",
        url,
        NOW,
    )
    assert (
        await count(session, "contacts", "tier = 'person' AND anonymised_at IS NULL") == 1
    )


async def test_an_equal_rank_never_replaces_the_person(session):
    owner = await company(session, "Namuna Tekstil Savdo")
    url = "https://t.me/namuna_optom/18235"
    first = await c.upsert_contact(
        session,
        owner,
        tier="person",
        phone="+998900000017",
        full_name="Namuna Bir",
        role="menejer",
        source_url=url,
        now=NOW,
    )
    same = await c.upsert_contact(
        session,
        owner,
        tier="person",
        phone="+998900000018",
        full_name="Namuna Ikki",
        role="sotuv menejeri",
        source_url=url,
        now=NOW,
    )
    await session.refresh(first)
    assert (first.tier, first.full_name, first.anonymised_at) == (
        "person",
        "Namuna Bir",
        None,
    )
    assert (same.tier, same.full_name, same.phone) == ("company", None, "+998900000018")


async def test_a_poster_whose_number_another_row_holds_leaves_it_there(session):
    firm = await company(session, "Namuna Tekstil Savdo")
    me = await contact(
        session, firm, tier="person", telegram_id=5100000021, full_name="Namuna"
    )
    switchboard = await contact(session, firm, phone="+998900000021")
    result = await resolve(
        session,
        parsed(
            kind="intent_post",
            source_key="telegram_monitor",
            tg_user_id=5100000021,
            phones=(("+998900000021", "person"),),
        ),
    )
    assert (result.company.id, result.contact.id) == (firm.id, me.id)
    phones = dict((await session.execute(sa.select(Contact.id, Contact.phone))).all())
    assert phones == {me.id: None, switchboard.id: "+998900000021"}


LANDLINE = "+998712000055"


async def _a_replaced_person_kept_their_landline(session: AsyncSession) -> Company:
    """A menejer who listed the office landline, replaced by a direktor: the
    old row is anonymised and keeps the landline (02 §4 R4's SQL), and
    ux_contacts_company_phone covers it still."""
    firm = await company(session, "Namuna Tekstil Savdo", city="Toshkent")
    for phone, name, role in (
        (LANDLINE, "Namuna Bir", "menejer"),
        ("+998900000056", "Namuna Ikki", "direktor"),
    ):
        await c.upsert_contact(
            session,
            firm,
            tier="person",
            phone=phone,
            full_name=name,
            role=role,
            source_url="https://t.me/namuna_optom/1",
            now=NOW,
        )
    return firm


async def _contacts_now(session: AsyncSession) -> list[tuple[Any, ...]]:
    rows = await session.execute(
        sa.select(Contact.phone, Contact.full_name, Contact.anonymised_at.is_not(None))
        .order_by(Contact.created_at, Contact.phone)
        .execution_options(populate_existing=True)
    )
    return [tuple(r) for r in rows]


BEFORE = [(LANDLINE, None, True), ("+998900000056", "Namuna Ikki", False)]


async def test_a_directory_listing_an_anonymised_rows_landline(session):
    firm = await _a_replaced_person_kept_their_landline(session)
    assert await _contacts_now(session) == BEFORE
    result = await resolve(
        session,
        parsed(
            name="Namuna Tekstil Savdo", city="Toshkent", phones=((LANDLINE, "company"),)
        ),
    )
    assert (result.company.id, result.contact) == (firm.id, None)
    assert await _contacts_now(session) == BEFORE


async def test_a_weaker_newcomer_with_an_anonymised_rows_landline(session):
    firm = await _a_replaced_person_kept_their_landline(session)
    row = await c.upsert_contact(
        session,
        firm,
        tier="person",
        phone=LANDLINE,
        full_name="Namuna Uch",
        role="menejer",
        source_url="https://t.me/namuna_optom/3",
        now=NOW,
    )
    assert row is None
    assert await _contacts_now(session) == BEFORE


async def test_a_new_person_with_an_anonymised_rows_landline(session):
    firm = await _a_replaced_person_kept_their_landline(session)
    live = await session.scalar(
        sa.select(Contact).where(Contact.full_name == "Namuna Ikki")
    )
    anonymise(live, now=NOW)  # retention R5: nobody is the firm's person now
    await session.flush()
    row = await c.upsert_contact(
        session,
        firm,
        tier="person",
        phone=LANDLINE,
        full_name="Namuna Uch",
        role="direktor",
        source_url="https://t.me/namuna_optom/3",
        now=NOW,
    )
    assert row is not None and (row.tier, row.full_name, row.phone) == (
        "person",
        "Namuna Uch",
        None,
    )
    assert await count(session, "contacts", "phone = :p", p=LANDLINE) == 1


async def test_a_vacancy_never_yields_a_contact(session):
    result = await resolve(
        session,
        parsed(
            kind="vacancy",
            name="Tex Snab Import",
            person={"name": "HR Namuna", "role": "HR"},
        ),
    )
    assert result.contact is None and await count(session, "contacts") == 0


def _found(kind: str, **fields: Any) -> Found:
    return Found(
        source=fields.pop("source", "hh_vacancies"),
        source_ref=fields.pop("source_ref", "hh:1"),
        url=fields.pop("url", "https://hh.uz/vacancy/1"),
        fetched_at=NOW,
        raw_sha256="",
        kind=kind,  # type: ignore[arg-type]
        **fields,
    )


async def test_a_vacancys_hr_handle_is_never_the_firms_channel(session):
    # Through the real normaliser: person_telegram is a group poster's username
    # (03 §1.2), the firm's channel only ever comes from ``telegram``.
    vacancy = to_observation_json(
        _found(
            "vacancy",
            company_name_raw="Namuna Tekstil Savdo MChJ",
            person_name="Namuna HR",
            person_role="HR menejer",
            person_telegram="namuna_hr_dilnoza",
        ),
        collector_version="hh_vacancies@1",
    )
    assert vacancy["handles"]["telegram"] is None
    post = to_observation_json(
        _found(
            "post",
            source="telegram_monitor",
            source_ref="-1001000000001:7",
            url="https://t.me/namuna_optom/7",
            person_telegram="namuna_xaridor",
        ),
        collector_version="telegram_monitor@1",
    )
    assert (post["kind"], post["handles"]["telegram"]) == (
        "intent_post",
        "namuna_xaridor",
    )
    result = await resolve(session, vacancy)
    # A collector that did put a handle on a vacancy: still the HR person's.
    again = await resolve(
        session,
        parsed(
            kind="vacancy",
            name="Namuna Tekstil Savdo MChJ",
            telegram="namuna_hr_dilnoza",
            instagram="namuna.hr",
        ),
    )
    firm = await session.get(Company, result.company.id)
    assert again.company.id == firm.id
    assert (firm.telegram_channel, firm.instagram) == (None, None)
    assert await count(session, "contacts") == 0


async def test_a_posters_second_number_is_not_stored(session):
    result = await resolve(
        session,
        parsed(
            kind="intent_post",
            source_key="telegram_monitor",
            name="Namuna Tekstil Savdo",
            phones=(("+998900000014", "person"), ("+998900000015", "person")),
        ),
    )
    phones = await session.scalars(sa.select(Contact.phone))
    assert list(phones) == ["+998900000014"]
    assert result.contact is not None and result.contact.tier == "person"


async def test_the_poster_upgrades_a_company_row_holding_their_number(session):
    owner = await company(session, "Namuna Tekstil Savdo")
    listed = await contact(session, owner, phone="+998900000016")
    result = await resolve(
        session,
        parsed(
            kind="intent_post",
            source_key="telegram_monitor",
            phones=(("+998900000016", "person"),),
            person={"name": "Namuna", "role": "direktor"},
        ),
    )
    assert result.company.id == owner.id and result.contact.id == listed.id
    assert (result.contact.tier, result.contact.full_name) == ("person", "Namuna")
    assert await count(session, "contacts") == 1


# --- Concurrency: the advisory lock (04 §2.2; 07 §1.8 observes the waiter) ------------


async def _waits_on_advisory(
    watcher: AsyncSession, pid: int, task: asyncio.Task[Any]
) -> bool:
    """Poll until the backend ``pid`` waits on an advisory lock — through
    pg_stat_activity on its own connection, rolled back between polls (its
    snapshot freezes inside an open transaction, fullprompt #873) — or until
    the task finished without waiting."""
    for _ in range(250):
        if task.done():
            return False
        row = (
            await watcher.execute(
                sa.text(
                    "SELECT wait_event_type, wait_event FROM pg_stat_activity "
                    "WHERE pid = :p"
                ),
                {"p": pid},
            )
        ).first()
        await watcher.rollback()
        if row is not None and tuple(row) == ("Lock", "advisory"):
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.parametrize(
    ("one", "two"),
    [
        (
            parsed(name="Birinchi Namuna Uyi", phones=(("+998900000031", "company"),)),
            parsed(name="Ikkinchi Boshqa Nom", phones=(("+998900000031", "company"),)),
        ),
        (parsed(name="Yangi Namuna Savdo Uyi"), parsed(name="Yangi Namuna Savdo Uyi")),
    ],
    ids=["same new phone", "same new name"],
)
async def test_two_concurrent_resolvers_make_one_company(session, one, two):
    first = await observe(session, one)
    second = await observe(session, two)
    await session.commit()
    async with SessionLocal() as a, SessionLocal() as b, SessionLocal() as watcher:
        obs_a = await a.get(Observation, first.id)
        result_a = await c.resolve_company(a, obs_a, source_key=SOURCE, now=NOW)
        pid = (await b.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()
        obs_b = await b.get(Observation, second.id)
        task = asyncio.create_task(
            c.resolve_company(b, obs_b, source_key=SOURCE, now=NOW)
        )
        waited = await _waits_on_advisory(watcher, pid, task)
        await a.commit()
        result_b = await task
        await b.commit()
    assert waited
    assert result_a.created and not result_b.created
    assert result_b.company.id == result_a.company.id
    assert await count(session, "companies") == 1


# --- Merge and unmerge (04 §2.5, 02 §2.14) --------------------------------------------


async def _evidence(session: AsyncSession) -> int:
    obs = await observe(session, parsed(name="Namuna Dalil"))
    return obs.evidence_id


async def _full_source(session: AsyncSession) -> tuple[Company, Company, dict[str, Any]]:
    """A source with one row of every kind merge moves, and a target."""
    target = await company(session, "Namuna Tekstil Savdo", stir="300000021")
    source = await company(
        session,
        "Namuna Tekstil",
        telegram_channel="namuna_tekstil",
        city="Toshkent",
        registry_status="active",
    )
    rows: dict[str, Any] = {}
    rows["contact"] = await contact(session, source, phone="+998900000041")
    rows["dup_phone"] = await contact(session, source, phone="+998900000042")
    await contact(session, target, phone="+998900000042")
    rows["person"] = await contact(session, source, tier="person", full_name="Namuna Bir")
    rows["alias"] = CompanyAlias(
        company_id=source.id, alias="Намуна Текстиль", alias_key="namuna tekstil"
    )
    session.add(rows["alias"])
    session.add(CompanyId(company_id=source.id, id_type="hh_employer", id_value="7782"))
    ev = await _evidence(session)
    rows["signal"] = Signal(
        company_id=source.id,
        evidence_id=ev,
        key="cn_origin",
        observed_at=NOW,
        source_key=SOURCE,
    )
    rows["job"] = ModelJob(
        kind="verdict",
        lane="instant",
        model="namuna-model",
        custom_id=f"cs-{uuid.uuid4()}",
        company_id=source.id,
    )
    rows["usage"] = UsageLog(
        provider="anthropic", operation="verdict", cost_usd=0, company_id=source.id
    )
    rows["candidate"] = LeadCandidate(company_id=source.id, state="review", score=55)
    session.add_all([rows["signal"], rows["job"], rows["usage"], rows["candidate"]])
    obs = await observe(session, parsed(name="Namuna Tekstil"))
    obs.company_id = source.id
    rows["observation"] = obs
    await session.flush()
    return source, target, rows


async def _owner(session: AsyncSession, model: type[Any], row_id: Any) -> uuid.UUID:
    value = await session.scalar(sa.select(model.company_id).where(model.id == row_id))
    assert isinstance(value, uuid.UUID)
    return value


async def test_merge_moves_every_row_and_unmerge_puts_each_back(session):
    source, target, rows = await _full_source(session)
    merged = await c.merge_into(
        session, source, target, by="owner:111111111", score=87, now=NOW
    )
    assert merged.id == target.id
    entry = await session.scalar(sa.select(MergeLog))
    moved = entry.moved
    assert set(moved["contacts"]) == {str(rows["contact"].id), str(rows["person"].id)}
    # The target already has this number: the row stays rather than vanish.
    assert moved["stayed_contacts"] == [str(rows["dup_phone"].id)]
    assert moved["aliases"] == [rows["alias"].id]
    assert moved["company_ids"] == [["hh_employer", "7782"]]
    assert moved["observations"] == [rows["observation"].id]
    assert moved["signals"] == [rows["signal"].id]
    assert moved["model_jobs"] == [rows["job"].id]
    assert moved["usage_log"] == [rows["usage"].id]
    assert moved["candidates"] == [str(rows["candidate"].id)]
    assert entry.carried == {
        "telegram_channel": "namuna_tekstil",
        "city": "Toshkent",
        "registry_status": "active",
    }
    assert (entry.decided_by, entry.match_score) == ("owner:111111111", 87)
    src = await session.get(Company, source.id)
    assert (src.state, src.merged_into) == ("merged", target.id)
    assert (merged.telegram_channel, merged.city, merged.stir) == (
        "namuna_tekstil",
        "Toshkent",
        "300000021",
    )
    for key, model in (
        ("contact", Contact),
        ("alias", CompanyAlias),
        ("signal", Signal),
        ("job", ModelJob),
        ("usage", UsageLog),
        ("candidate", LeadCandidate),
        ("observation", Observation),
        ("person", Contact),
    ):
        assert await _owner(session, model, rows[key].id) == target.id, key
    assert await c.company_lineage(session, target.id) == {target.id, source.id}

    restored = await c.unmerge(
        session, entry.id, by="owner:111111111", now=NOW + timedelta(hours=23)
    )
    assert (restored.state, restored.merged_into) == ("active", None)
    for key, model in (
        ("contact", Contact),
        ("alias", CompanyAlias),
        ("signal", Signal),
        ("job", ModelJob),
        ("usage", UsageLog),
        ("candidate", LeadCandidate),
        ("observation", Observation),
        ("dup_phone", Contact),
        ("person", Contact),
    ):
        assert await _owner(session, model, rows[key].id) == source.id, key
    assert (
        await session.scalar(
            sa.select(CompanyId.company_id).where(CompanyId.id_value == "7782")
        )
        == source.id
    )
    back = await session.get(Company, target.id)
    assert (back.telegram_channel, back.city, back.registry_status, back.stir) == (
        None,
        None,
        "unknown",
        "300000021",
    )
    await session.refresh(entry)
    assert (entry.undone_at, entry.undone_by) == (
        NOW + timedelta(hours=23),
        "owner:111111111",
    )
    assert await c.company_lineage(session, target.id) == {target.id}


async def test_the_target_keeps_its_person_and_the_sources_stays_put(session):
    target = await company(session, "Namuna Tekstil Savdo")
    source = await company(session, "Namuna Tekstil")
    mine = await contact(session, target, tier="person", full_name="Namuna Bir")
    theirs = await contact(session, source, tier="person", full_name="Namuna Ikki")
    await c.merge_into(session, source, target, by="owner:111111111", now=NOW)
    assert await _owner(session, Contact, mine.id) == target.id
    assert await _owner(session, Contact, theirs.id) == source.id
    assert (
        await count(session, "contacts", "tier = 'person' AND anonymised_at IS NULL") == 2
    )


async def test_merge_refuses_two_stirs_and_allows_one(session):
    a = await company(session, "Namuna Tekstil Savdo", stir="300000022")
    b = await company(session, "Namuna Tekstil", stir="300000023")
    with pytest.raises(c.MergeRefused) as refused:
        await c.merge_into(session, b, a, by="owner:111111111", now=NOW)
    assert refused.value.reason == "stir"
    assert await count(session, "merge_log") == 0
    # The same STIR on one side only: the target gets it, the log carries it.
    d = await company(session, "Namuna Tekstil Plus")
    await c.merge_into(session, b, d, by="resolver:stir", now=NOW)
    entry = await session.scalar(sa.select(MergeLog))
    assert entry.carried == {"stir": "300000023"}
    assert (await session.get(Company, d.id)).stir == "300000023"


@pytest.mark.parametrize(
    ("source_fields", "target_fields", "reason"),
    [
        (
            {"telegram_channel": "namuna_bir"},
            {"telegram_channel": "namuna_ikki"},
            "telegram",
        ),
        (
            {"website_domain": "namuna.example.uz", "registry_status": "active"},
            {"website_domain": "namuna.example.com", "registry_status": "active"},
            "domain",
        ),
        ({"state": "purged"}, {}, "purged"),
    ],
)
async def test_merge_refusals(session, source_fields, target_fields, reason):
    source = await company(session, "Namuna Bir", **source_fields)
    target = await company(session, "Namuna Ikki", **target_fields)
    with pytest.raises(c.MergeRefused) as refused:
        await c.merge_into(session, source, target, by="owner:111111111", now=NOW)
    assert refused.value.reason == reason


async def test_two_unconfirmed_sites_do_not_refuse_the_merge(session):
    source = await company(session, "Namuna Bir", website_domain="namuna.example.uz")
    target = await company(session, "Namuna Ikki", website_domain="namuna.example.com")
    merged = await c.merge_into(session, source, target, by="owner:111111111", now=NOW)
    assert merged.website_domain == "namuna.example.com"


async def test_a_merged_company_cannot_be_merged_again(session):
    source = await company(session, "Namuna Bir Savdo")
    first = await company(session, "Namuna Ikki Savdo")
    other = await company(session, "Namuna Uch Savdo")
    await c.merge_into(session, source, first, by="owner:111111111", now=NOW)
    with pytest.raises(c.MergeRefused) as refused:
        await c.merge_into(session, source, other, by="owner:111111111", now=NOW)
    assert refused.value.reason == "source_merged"
    assert await count(session, "merge_log") == 1


async def _dup_of(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        sa.select(Company.dup_of)
        .where(Company.id == company_id)
        .execution_options(populate_existing=True)
    )


async def test_a_proposal_about_the_source_follows_it_and_comes_back(session):
    target = await company(session, "Namuna Tekstil Savdo")
    source = await company(session, "Namuna Tekstil")
    pointer = await company(session, "Namuna Tekstil Uyi", dup_of=source.id, dup_score=85)
    await c.merge_into(session, source, target, by="owner:111111111", now=NOW)
    assert await _dup_of(session, pointer.id) == target.id
    entry = await session.scalar(sa.select(MergeLog))
    assert entry.moved["dup_of"] == [str(pointer.id)]
    await c.unmerge(session, entry.id, by="owner:111111111", now=NOW)
    assert await _dup_of(session, pointer.id) == source.id


async def test_unmerge_leaves_what_moved_on_since(session):
    target = await company(session, "Namuna Tekstil Savdo")
    source = await company(
        session, "Namuna Tekstil", city="Toshkent", telegram_channel="namuna_tekstil"
    )
    elsewhere = await company(session, "Boshqa Mebel Uyi")
    row = await contact(session, source, phone="+998900000043")
    await c.merge_into(session, source, target, by="owner:111111111", now=NOW)
    # Since the merge: the owner moved the contact on, enrichment re-read the city.
    await session.execute(
        sa.update(Contact).where(Contact.id == row.id).values(company_id=elsewhere.id)
    )
    await session.execute(
        sa.update(Company).where(Company.id == target.id).values(city="Samarqand")
    )
    entry = await session.scalar(sa.select(MergeLog))
    assert entry.carried == {"city": "Toshkent", "telegram_channel": "namuna_tekstil"}
    await c.unmerge(session, entry.id, by="owner:111111111", now=NOW)
    assert await _owner(session, Contact, row.id) == elsewhere.id
    back = await session.get(Company, target.id, populate_existing=True)
    # Still what was carried: given back. Changed since: kept.
    assert (back.telegram_channel, back.city) == (None, "Samarqand")


async def test_the_sources_open_candidate_is_excluded_beside_the_targets(session):
    target = await company(session, "Namuna Tekstil Savdo")
    source = await company(session, "Namuna Tekstil")
    mine = LeadCandidate(company_id=target.id, state="review", score=70)
    theirs = LeadCandidate(company_id=source.id, state="enriching", score=50)
    session.add_all([mine, theirs])
    await session.flush()
    await c.merge_into(session, source, target, by="owner:111111111", now=NOW)
    state = await session.execute(
        sa.select(
            LeadCandidate.state, LeadCandidate.exclude_reason, LeadCandidate.company_id
        ).where(LeadCandidate.id == theirs.id)
    )
    assert state.one() == ("excluded", "merged", source.id)
    entry = await session.scalar(sa.select(MergeLog))
    await c.unmerge(session, entry.id, by="owner:111111111", now=NOW)
    state = await session.execute(
        sa.select(LeadCandidate.state, LeadCandidate.exclude_reason).where(
            LeadCandidate.id == theirs.id
        )
    )
    assert state.one() == ("enriching", None)


async def test_an_approved_source_candidate_refuses_the_merge(session):
    target = await company(session, "Namuna Tekstil Savdo")
    source = await company(session, "Namuna Tekstil")
    session.add_all(
        [
            LeadCandidate(company_id=target.id, state="review", score=70),
            LeadCandidate(
                company_id=source.id,
                state="approved",
                score=80,
                approved_by="owner:1",
                approved_at=NOW,
            ),
        ]
    )
    await session.flush()
    with pytest.raises(c.MergeRefused) as refused:
        await c.merge_into(session, source, target, by="owner:111111111", now=NOW)
    assert refused.value.reason == "candidate_approved"


async def test_unmerge_after_24_hours_or_twice_is_refused(session):
    target = await company(session, "Namuna Tekstil Savdo")
    source = await company(session, "Namuna Tekstil")
    await c.merge_into(session, source, target, by="owner:111111111", now=NOW)
    entry = await session.scalar(sa.select(MergeLog))
    with pytest.raises(c.UnmergeRefused) as late:
        await c.unmerge(
            session,
            entry.id,
            by="owner:111111111",
            now=NOW + timedelta(hours=24, seconds=1),
        )
    assert late.value.reason == "too_late"
    await c.unmerge(
        session, entry.id, by="owner:111111111", now=NOW + timedelta(hours=24)
    )
    with pytest.raises(c.UnmergeRefused) as twice:
        await c.unmerge(
            session, entry.id, by="owner:111111111", now=NOW + timedelta(hours=24)
        )
    assert twice.value.reason == "already_undone"


async def test_a_chain_is_one_lineage_and_unmerge_waits_for_the_later_merge(session):
    a = await company(session, "Namuna Bir Savdo")
    b = await company(session, "Namuna Ikki Savdo")
    root = await company(session, "Namuna Uch Savdo")
    await c.merge_into(session, a, b, by="owner:111111111", now=NOW)
    await c.merge_into(session, b, root, by="owner:111111111", now=NOW)
    assert await c.company_lineage(session, root.id) == {a.id, b.id, root.id}
    first = await session.scalar(sa.select(MergeLog).order_by(MergeLog.id))
    with pytest.raises(c.UnmergeRefused) as refused:
        await c.unmerge(session, first.id, by="owner:111111111", now=NOW)
    assert refused.value.reason == "target_merged"
    # Merging into a merged company lands on its root.
    late = await company(session, "Namuna Tort Savdo")
    assert (
        await c.merge_into(session, late, b, by="owner:111111111", now=NOW)
    ).id == root.id


async def test_merge_log_is_append_only(session):
    target = await company(session, "Namuna Tekstil Savdo")
    source = await company(session, "Namuna Tekstil")
    await c.merge_into(session, source, target, by="owner:111111111", now=NOW)
    await session.commit()
    with pytest.raises(sa.exc.DBAPIError) as refused:
        async with session.begin_nested():
            await session.execute(sa.text("DELETE FROM merge_log"))
    assert "merge_log is append-only" in str(refused.value.orig)
    assert await count(session, "merge_log") == 1


# --- resolve_job and 02 §8 ------------------------------------------------------------


EXAMPLE_POST = {
    "kind": "intent_post",
    "source_key": "telegram_monitor",
    "name": "Namuna Tekstil Savdo MChJ",
    "city": "Toshkent",
    "phones": (("+998900000001", "person"),),
    "telegram": "namuna_tekstil",
    "tg_user_id": 5100000001,
    "person": {"name": "Rustam", "role": "direktor"},
    "url": "https://t.me/namuna_optom/18234",
}


async def test_the_02_section_8_example_resolves_as_written(session):
    """02 §8 rows 3–5 (and 04 §9.1's resolver line): one intent post becomes a
    company, its one decision maker and no company_ids row; the registry page
    later adds the STIR and the Cyrillic spelling to the same company."""
    obs = await observe(session, parsed(**EXAMPLE_POST))
    interaction = Interaction(
        tg_chat_id=-1001000000001,
        tg_message_id=18234,
        sender_id=5100000001,
        occurred_at=NOW,
        prefilter_hit=["volume_kub", "china", "intent"],
        observation_id=obs.id,
    )
    session.add(interaction)
    await session.commit()

    heard: list[str] = []
    url = sa.engine.make_url(settings.database_url).set(drivername="postgresql")
    async with await psycopg.AsyncConnection.connect(
        url.render_as_string(hide_password=False), autocommit=True
    ) as listener:
        await listener.execute("LISTEN cs_interaction")
        report = await c.resolve_batch(now=NOW)
        async for note in listener.notifies(timeout=2.0, stop_after=1):
            heard.append(note.payload)
    assert report.as_dict() == {
        "claimed": 1,
        "resolved": 1,
        "created": 1,
        "rejected": 0,
        "needs_review": 0,
        "deferred": 0,
    }
    # score_job wakes (04 §0), and only it: the announcement is the resolver's.
    assert heard == [f"resolve:{interaction.id}"]

    row = await session.scalar(
        sa.select(Observation)
        .where(Observation.id == obs.id)
        .execution_options(populate_existing=True)
    )
    firm = await session.scalar(sa.select(Company))
    person = await session.scalar(sa.select(Contact))
    assert (row.status, row.company_id, row.contact_id, row.resolved_at) == (
        "resolved",
        firm.id,
        person.id,
        NOW,
    )
    assert (firm.display_name, firm.name_key, firm.legal_form, firm.city, firm.state) == (
        "Namuna Tekstil Savdo MChJ",
        "namuna tekstil savdo",
        "MChJ",
        "Toshkent",
        "active",
    )
    assert (firm.telegram_channel, firm.first_seen_at) == (None, NOW)
    got = {
        k: getattr(person, k)
        for k in (
            "company_id",
            "tier",
            "full_name",
            "role",
            "phone",
            "phone_tail",
            "phone_kind",
            "telegram_username",
            "telegram_id",
            "is_decision_maker",
            "legal_basis",
            "source_url",
            "source_key",
            "collected_at",
        )
    }
    assert got == {
        "company_id": firm.id,
        "tier": "person",
        "full_name": "Rustam",
        "role": "direktor",
        "phone": "+998900000001",
        "phone_tail": "900000001",
        "phone_kind": "mobile",
        "telegram_username": "namuna_tekstil",
        "telegram_id": 5100000001,
        "is_decision_maker": True,
        "legal_basis": "public_source",
        "source_url": "https://t.me/namuna_optom/18234",
        "source_key": "telegram_monitor",
        "collected_at": NOW,
    }
    assert await count(session, "company_ids") == 0

    later = NOW + timedelta(minutes=6)
    await observe(
        session,
        parsed(
            kind="registry",
            source_key="registry_lookup",
            name="НАМУНА ТЕКСТИЛЬ САВДО",
            stir="300000011",
            city="Toshkent",
            url="https://orginfo.example.uz/300000011",
        ),
    )
    await session.commit()
    assert (await c.resolve_batch(now=later)).as_dict()["created"] == 0
    firm = await session.scalar(
        sa.select(Company).execution_options(populate_existing=True)
    )
    assert firm.stir == "300000011"
    aliases = (await session.execute(sa.select(CompanyAlias.alias))).scalars().all()
    assert aliases == ["НАМУНА ТЕКСТИЛЬ САВДО"]


async def test_the_job_marks_each_outcome_and_one_failure_costs_only_itself(session):
    good = await observe(session, parsed(name="Namuna Tekstil Savdo"))
    self_post = await observe(session, parsed(name="GSR Logistics"))
    broken = await observe(session, parsed(name="Namuna Buzilgan Firma"))
    await session.commit()

    async def hook(
        s: AsyncSession, obs: Observation, res: c.Resolution, now: Any
    ) -> None:
        if obs.id == broken.id:
            raise RuntimeError("signal writer failed")

    report = await c.resolve_batch(now=NOW, after=(hook,))
    assert report.as_dict() == {
        "claimed": 3,
        "resolved": 1,
        "created": 1,
        "rejected": 1,
        "needs_review": 1,
        "deferred": 0,
    }
    statuses = dict(
        (await session.execute(sa.select(Observation.id, Observation.status))).all()
    )
    assert statuses == {
        good.id: "resolved",
        self_post.id: "rejected",
        broken.id: "needs_review",
    }
    reason = await session.scalar(
        sa.select(Observation.reject_reason).where(Observation.id == self_post.id)
    )
    assert reason == "competitor"
    # The broken row's company went with its savepoint.
    assert [x.display_name for x in await companies(session)] == ["Namuna Tekstil Savdo"]


async def test_lock_trouble_leaves_the_row_new_for_the_next_run(session):
    obs = await observe(session, parsed(name="Namuna Tekstil Savdo"))
    await session.commit()

    class Deadlock(Exception):
        sqlstate = "40P01"

    async def hook(s: AsyncSession, o: Observation, res: c.Resolution, now: Any) -> None:
        raise sa.exc.OperationalError("SELECT 1", {}, Deadlock())

    report = await c.resolve_batch(now=NOW, after=(hook,))
    assert (report.claimed, report.deferred) == (1, 1)
    assert (
        await session.scalar(
            sa.select(Observation.status).where(Observation.id == obs.id)
        )
        == "new"
    )
    assert (await c.resolve_batch(now=NOW)).resolved == 1


async def test_a_registered_hook_runs_inside_resolve_job(session, monkeypatch):
    obs = await observe(session, parsed(name="Namuna Tekstil Savdo"))
    await session.commit()
    monkeypatch.setattr(c, "AFTER_RESOLVE", [])
    ran: list[tuple[Any, ...]] = []

    async def hook(s: AsyncSession, o: Observation, res: c.Resolution, now: Any) -> None:
        ran.append((o.id, res.created, now))

    c.register_after_resolve(hook)
    c.register_after_resolve(hook)  # idempotent
    registered = c.AFTER_RESOLVE
    assert registered == [hook]
    report = await c.resolve_job(now=NOW)
    assert (report["resolved"], ran) == (1, [(obs.id, True, NOW)])


async def test_a_failed_resolution_logs_no_person(session):
    """06 §5.1: the person tier is never in logs. A database error's text
    carries the row it refused (``[parameters: …]``) — here a person's name,
    role and handle — so the line names the error, never its message."""
    obs = await observe(session, parsed(name="Namuna Tekstil Savdo"))
    await session.commit()

    async def hook(s: AsyncSession, o: Observation, res: c.Resolution, now: Any) -> None:
        s.add(
            Contact(
                company_id=res.company.id,
                tier="person",  # no legal basis: ck_contacts_person_basis refuses it
                full_name="Rustamjon Namunaviy",
                role="direktor",
                telegram_username="namuna_rustamjon",
            )
        )
        await s.flush()

    root = logging.getLogger()
    level = root.level
    stream = io.StringIO()
    assert logs.setup_logging("worker", fmt="json", stream=stream, force=True)
    try:
        report = await c.resolve_batch(now=NOW, after=(hook,))
    finally:
        for handler in root.handlers[:]:
            if isinstance(handler, logs.CsHandler):
                root.removeHandler(handler)
        root.setLevel(level)
    assert report.needs_review == 1
    out = stream.getvalue()
    for word in ("Rustamjon", "Namunaviy", "namuna_rustamjon", "direktor", "parameters"):
        assert word not in out, word
    (line,) = [
        json.loads(x)
        for x in out.splitlines()
        if json.loads(x)["msg"] == "resolution failed"
    ]
    assert {
        k: line[k] for k in ("observation_id", "error", "sqlstate", "constraint")
    } == {
        "observation_id": obs.id,
        "error": "IntegrityError",
        "sqlstate": "23514",
        "constraint": "ck_contacts_person_basis",
    }
    assert line["where"].startswith("companies.py:") and "exc" not in line


async def test_the_job_stops_at_its_limit(session):
    for n in range(3):
        await observe(session, parsed(name=f"Namuna Firma {n} Savdo"))
    await session.commit()
    assert (await c.resolve_batch(now=NOW, limit=2)).claimed == 2
    assert await count(session, "observations", "status = 'new'") == 1
