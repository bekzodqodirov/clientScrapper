"""Every mirrored key function equals its SQL twin (02 §1; 07 T-03).

``services/keys.py`` writes every key; ``cs_phone_tail``, ``cs_username_key``
and ``cs_domain_key`` (0001_initial) exist for CHECKs, the generated
``contacts.phone_tail`` and ad-hoc queries. One input table per function, run
through both and pinned to the expected value, so neither side can move alone.
The designed exceptions are in ``domain_key`` and keep the stored value a
fixed point of the CHECK ``website_domain = cs_domain_key(website_domain)``:
Python IDNA-encodes a non-ASCII host the SQL leaves NULL, and strips every
leading ``www.`` where the SQL strips one (the SQL function is not idempotent
on ``www.www.``) — both pinned below. Synthetic data only (07 §1.0).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.services.keys import (
    domain_key,
    erp_phone_hash,
    erp_salt_id,
    instagram_key,
    phone_e164,
    phone_kind,
    phone_tail,
    username_key,
)

Case = tuple[str | None, str | None]

PHONE_TAIL_CASES: list[Case] = [
    ("+998 90 000 00 01", "900000001"),
    ("+998900000001", "900000001"),
    ("998900000002", "900000002"),
    ("8 (71) 200-00-00", "712000000"),
    ("90-000-00-03", "900000003"),
    ("0 90 000 00 04", "900000004"),
    ("(90) 000 00 05", "900000005"),
    ("+998 (90) 000-00-06", "900000006"),
    ("+7 926 000 00 00", "260000000"),
    ("+86 138 0000 0000", "800000000"),
    ("tel: 90 000 00 01, 91 000 00 02", "910000002"),
    ("+998 90 000 00 01 доб. 12", "000000112"),
    ("\n+998900000007\n", "900000007"),
    ("123456789", "123456789"),
    ("12345678", None),
    ("71 200 00", None),
    ("", None),
    ("abc", None),
    ("+", None),
    # Non-ASCII digits are not digits to either side ([^0-9] is ASCII).
    ("９０００００００１", None),
    ("٩٠٠٠٠٠٠٠١", None),
    (None, None),
]

USERNAME_CASES: list[Case] = [
    ("@Namuna_Optom", "namuna_optom"),
    ("namuna_optom", "namuna_optom"),
    ("https://t.me/namuna_optom", "namuna_optom"),
    ("http://www.t.me/namuna_optom/123", "namuna_optom"),
    ("telegram.me/namuna_optom?start=1", "namuna_optom"),
    ("t.me/@namuna_optom", "namuna_optom"),
    ("@namuna_optom#about", "namuna_optom"),
    ("  @namuna_optom  ", "namuna_optom"),
    ("NAMUNA_1", "namuna_1"),
    ("a" * 32, "a" * 32),
    ("a" * 33, None),
    ("abcd", None),
    ("namuna_bot", None),
    ("NamunaBot", None),
    ("namuna-optom", None),
    ("namuna.optom", None),
    ("@@namuna_optom", None),
    # The prefix in either case, on both sides (02 §1's SQL stripped only
    # lower case, so a link typed in capitals keyed to NULL).
    ("T.ME/namuna_optom", "namuna_optom"),
    ("T.me/namuna_optom", "namuna_optom"),
    ("HTTPS://T.ME/namuna_optom", "namuna_optom"),
    ("Http://WWW.Telegram.Me/@Namuna_Optom", "namuna_optom"),
    ("WWW.t.me/namuna_optom", "namuna_optom"),
    # ASCII only: the Kelvin sign is not the k of a scheme to either side
    # (Python's re.I would have folded it), so the prefix stays and the
    # whole string is no username.
    ("Kttps://t.me/namuna_optom", None),
    # A bare host without the slash is no prefix.
    ("T.ME", None),
    # Reserved paths are the normaliser's job: the mirror keeps them.
    ("t.me/joinchat/AAAAAEnamuna", "joinchat"),
    ("t.me/+AbCdEfGh12", None),
    ("t.me/s/namuna_optom", None),
    # trim() strips spaces only; '$' is the end of the string.
    ("\tnamuna_optom", None),
    ("namuna_optom\n", None),
    # Postgres lowers İ to i and the Kelvin sign to k, as Python now does.
    ("İnamuna_optom", "inamuna_optom"),
    ("Knamuna_optom", "knamuna_optom"),
    ("Ｎamuna_optom", None),
    ("", None),
    (None, None),
]

DOMAIN_CASES: list[Case] = [
    ("https://www.Namuna.uz/aloqa", "namuna.uz"),
    ("namuna.example.uz", "namuna.example.uz"),
    ("http://shop.namuna.example.uz:8080/path?q=1#x", "shop.namuna.example.uz"),
    ("www.namuna.uz", "namuna.uz"),
    ("WWW.NAMUNA.UZ", "namuna.uz"),
    ("namuna.uz/", "namuna.uz"),
    ("HTTPS://Namuna-Savdo.example.uz", "namuna-savdo.example.uz"),
    ("ftp://files.namuna.example.uz", "files.namuna.example.uz"),
    ("   namuna.example.uz   ", "namuna.example.uz"),
    ("namuna.example.uz?utm_source=x", "namuna.example.uz"),
    ("namuna.example.uz#kontakt", "namuna.example.uz"),
    # Social and marketplace hosts are handles, not websites.
    ("https://instagram.com/namuna", None),
    ("www.instagram.com", None),
    ("t.me/namuna_optom", None),
    ("https://telegram.me/namuna_optom", None),
    ("https://www.facebook.com/namuna", None),
    ("https://olx.uz/d/obyavlenie", None),
    ("uzum.uz/ru/shop/namuna", None),
    ("https://youtube.com/@namuna", None),
    ("wa.me/998900000001", None),
    # The SQL list is exact; a subdomain passes here (normalise.website_key drops it).
    ("m.facebook.com/namuna", "m.facebook.com"),
    ("namuna", None),
    ("localhost:8000", None),
    ("user@namuna.example.uz", None),
    ("mailto:info@namuna.example.uz", None),
    ("namuna_uz.uz", None),
    ("namuna.uz.", None),
    ("\tnamuna.uz", None),
    ("namuna.uz\n", None),
    ("İnfo.example.uz", "info.example.uz"),
    ("", None),
    (None, None),
]

# (raw, Python's stored value, SQL on the raw input). Python IDNA-encodes where
# SQL answers NULL, and strips all leading www. where SQL strips one.
DIVERGENT_DOMAIN_CASES: list[tuple[str, str | None, str | None]] = [
    ("https://café.example.uz/menu", "xn--caf-dma.example.uz", None),
    ("münchen.example.uz", "xn--mnchen-3ya.example.uz", None),
    ("www.пример.рф", "xn--e1afmkfd.xn--p1ai", None),
    ("намуна.уз", "xn--80aa9acc9a.xn--g1ax", None),
    ("ＥＸＡＭＰＬＥ.uz", "example.uz", None),
    ("www.www.namuna.uz", "namuna.uz", "www.namuna.uz"),
    ("https://www.www.instagram.com/namuna", None, "www.instagram.com"),
]


async def _sql(
    session: AsyncSession, fn: str, values: Sequence[str | None]
) -> list[str | None]:
    """``fn`` over ``values`` in one statement, in input order (NULLs included)."""
    rows = await session.execute(
        sa.text(
            f"SELECT {fn}(v) FROM unnest(CAST(:vals AS text[])) "
            "WITH ORDINALITY AS t(v, n) ORDER BY n"
        ),
        {"vals": list(values)},
    )
    return [r[0] for r in rows]


async def _assert_mirror(
    session: AsyncSession,
    sql_fn: str,
    py_fn: Callable[[str | None], str | None],
    cases: list[Case],
) -> None:
    raws = [raw for raw, _ in cases]
    expected = [want for _, want in cases]
    assert [py_fn(raw) for raw in raws] == expected
    assert await _sql(session, sql_fn, raws) == expected


async def test_phone_tail_equals_cs_phone_tail(session: AsyncSession) -> None:
    await _assert_mirror(session, "cs_phone_tail", phone_tail, PHONE_TAIL_CASES)


async def test_username_key_equals_cs_username_key(session: AsyncSession) -> None:
    await _assert_mirror(session, "cs_username_key", username_key, USERNAME_CASES)


async def test_domain_key_equals_cs_domain_key(session: AsyncSession) -> None:
    await _assert_mirror(session, "cs_domain_key", domain_key, DOMAIN_CASES)


async def test_domain_key_diverges_only_toward_a_check_fixed_point(
    session: AsyncSession,
) -> None:
    raws = [raw for raw, _, _ in DIVERGENT_DOMAIN_CASES]
    stored = [domain_key(raw) for raw in raws]
    assert stored == [py for _, py, _ in DIVERGENT_DOMAIN_CASES]
    assert await _sql(session, "cs_domain_key", raws) == [
        sql for _, _, sql in DIVERGENT_DOMAIN_CASES
    ]
    kept = [d for d in stored if d is not None]
    assert await _sql(session, "cs_domain_key", kept) == kept


async def test_python_keys_are_fixed_points_of_their_sql_checks(
    session: AsyncSession,
) -> None:
    # The CHECKs read «col = cs_x(col)»: whatever Python stores must survive.
    usernames = [username_key(raw) for raw, _ in USERNAME_CASES]
    usernames = [u for u in usernames if u is not None]
    assert await _sql(session, "cs_username_key", usernames) == usernames
    domains = [domain_key(raw) for raw, _ in DOMAIN_CASES]
    domains += [domain_key(raw) for raw, _, _ in DIVERGENT_DOMAIN_CASES]
    domains = [d for d in domains if d is not None]
    assert await _sql(session, "cs_domain_key", domains) == domains


async def test_keys_written_by_python_pass_the_table_checks(
    session: AsyncSession,
) -> None:
    """A real company and contact row carrying Python's keys: the CHECKs accept
    them and the generated phone_tail equals Python's tail."""
    e164 = phone_e164("8 (71) 200-00-00")
    assert e164 == "+998712000000"
    company_id = (
        await session.execute(
            sa.text(
                "INSERT INTO companies (display_name, name_key, website_domain, "
                "telegram_channel, instagram) VALUES ('Namuna Tekstil Savdo', "
                "'namuna tekstil savdo', :site, :channel, :ig) RETURNING id"
            ),
            {
                "site": domain_key("https://café.example.uz/menu"),
                "channel": username_key("https://t.me/Namuna_Tekstil"),
                "ig": instagram_key("https://www.instagram.com/Namuna.Tekstil/"),
            },
        )
    ).scalar_one()
    tail = (
        await session.execute(
            sa.text(
                "INSERT INTO contacts (company_id, tier, phone, phone_kind, "
                "telegram_username, instagram) VALUES (:c, 'company', :phone, :kind, "
                ":tg, :ig) RETURNING phone_tail"
            ),
            {
                "c": company_id,
                "phone": e164,
                "kind": phone_kind(e164),
                "tg": username_key("@Namuna_Optom"),
                "ig": instagram_key("@namuna.optom."),
            },
        )
    ).scalar_one()
    assert tail == phone_tail(e164) == "712000000"
    row = (
        await session.execute(
            sa.text(
                "SELECT k.website_domain, k.telegram_channel, k.instagram, c.phone_kind, "
                "c.telegram_username, c.instagram FROM companies k "
                "JOIN contacts c ON c.company_id = k.id WHERE k.id = :c"
            ),
            {"c": company_id},
        )
    ).one()
    assert tuple(row) == (
        "xn--caf-dma.example.uz",
        "namuna_tekstil",
        "namuna.tekstil",
        "landline",
        "namuna_optom",
        "namuna.optom",
    )


@pytest.mark.parametrize(
    ("secret", "tail9", "digest", "salt"),
    [
        # 07 §1.5: pinned here and in fullprompt tests/unit/agent-feed.test.ts.
        ("k", "901234567", "2ea6869e504cebca50f0", "8254c329"),
        ("test-secret", "900000001", "d9d4ae601e164f812f1d", "9caf06bb"),
    ],
)
def test_erp_hash_vectors(secret: str, tail9: str, digest: str, salt: str) -> None:
    assert erp_phone_hash(secret, tail9) == digest
    assert erp_salt_id(secret) == salt


@pytest.mark.parametrize(
    "bad", ["+998901234567", "90123456", "9012345678", "90 123 45 67", ""]
)
def test_erp_phone_hash_refuses_anything_but_a_tail(bad: str) -> None:
    with pytest.raises(ValueError):
        erp_phone_hash("k", bad)
