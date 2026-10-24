"""collectors/telegram_monitor (03 §2.3, 07 T-14): stored ``interactions`` →
observations, the tout filter, the weekly channel about, repeat posters.

The parsers are pure and run on ``Post`` values; the run half goes through
the real runner on Postgres, over rows the reader's own writers stored
(``userbot/ingest.store_hit``, ``userbot/about.store_about``), with a Fetcher
whose every request would be a 404 — and none is made. All data is synthetic
(07 §1.0): «Namuna …», ``namuna_<n>``, ``+998 90 000 00 NN``, ``*.example.uz``.
The ``telegram_monitor`` row is seeded configuration: each run test enables
it and puts back what it found.
"""

from __future__ import annotations

import gzip
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
import sqlalchemy as sa

from clientscrapper.collectors import telegram_monitor as tm
from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.runner import run_collector
from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import chats, companies
from clientscrapper.services.normalise import to_observation_json
from clientscrapper.userbot import about, ingest, prefilter
from tests.fakes import FakeClock, Site, fetcher_for

TZ = ZoneInfo("Asia/Tashkent")
T0 = datetime(2026, 10, 20, 7, 41, tzinfo=TZ)
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)
GROUP_ID = -1001000000001
CHANNEL_ID = -1001000000002
OTHER_CHANNEL = -1001000000009
POSTER = 5100000001
ASK = "Guangzhoudan 25 kub mato olib kelish kerak, kim qiladi?"
BIO = "Namuna Tekstil Savdo MChJ direktori"
PRICE_LIST = "Xitoydan yuk: 1 kub 250$, 100 kg 300$, 20 fut 2500$"

GROUP = tm.Chat(GROUP_ID, "group", "Namuna Optom", "namuna_optom")
CHANNEL = tm.Chat(CHANNEL_ID, "channel", "Namuna Mebel Optom", "namuna_mebel")


def post(text: str = ASK, *, chat: tm.Chat = GROUP, **fields) -> tm.Post:
    defaults = {
        "interaction_id": 1,
        "chat": chat,
        "tg_message_id": 18234,
        "text": text,
        "occurred_at": T0,
        "stored_at": T0 + timedelta(seconds=20),
        "sender_id": POSTER,
        "sender_username": "namuna_tekstil",
        "sender_name": "Rustam",
        "sender_bio": BIO,
    }
    defaults.update(fields)
    return tm.Post(**defaults)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- a buyer's ask (pure) ------------------------------------------------------------


def test_the_specs_worked_example_becomes_an_intent_post():
    """02 §8 / 04 §9.1: the ask, the poster, the firm and the role from the bio."""
    obs = tm.parse_post(post(ASK + " +998 90 000 00 01"))
    text = ASK + " +998 90 000 00 01"
    assert obs == Observation(
        source="telegram_monitor",
        source_ref="tg:-1001000000001:18234",
        url="https://t.me/namuna_optom/18234",
        fetched_at=T0 + timedelta(seconds=20),
        raw_sha256=sha(text),
        kind="post",
        company_name_raw="Namuna Tekstil Savdo MChJ",
        phones=["+998 90 000 00 01"],
        volume_text="25 kub",
        origin_text="Guangzhoudan",
        signal_types=["intent_ask", "volume_m3_3plus", "china_origin"],
        excerpt=text,
        person_name="Rustam",
        person_role="direktori",
        person_telegram="namuna_tekstil",
        attrs={
            "chat_title": "Namuna Optom",
            "chat_type": "group",
            "volume_m3": 25.0,
            "basis": "stated",
            "frequency": None,
            "repeat_count_60d": None,
            "tg_user_id": POSTER,
        },
        published_at=T0,
    )
    parsed = to_observation_json(obs, collector_version="telegram_monitor@1")
    assert (parsed["kind"], parsed["name_key"]) == ("intent_post", "namuna tekstil savdo")
    assert parsed["company"]["legal_form"] == "MChJ"
    assert parsed["person"] == {"name": "Rustam", "role": "direktori"}
    assert (parsed["handles"]["telegram"], parsed["handles"]["tg_user_id"]) == (
        "namuna_tekstil",
        POSTER,
    )
    assert parsed["phones"] == [
        {
            "raw": "+998 90 000 00 01",
            "e164": "+998900000001",
            "kind": "mobile",
            "owner": "person",
        }
    ]
    assert (parsed["hints"]["volume_m3"], parsed["hints"]["volume_basis"]) == (
        25.0,
        "stated",
    )


@pytest.mark.parametrize(
    ("text", "signals", "volume", "basis", "volume_text", "origin"),
    [
        (
            "ким олиб келади? Хитойдан 2 куб мебель керак",
            ["intent_ask", "volume_m3_1to3", "china_origin"],
            2.0,
            "stated",
            "2 куб",
            "Хитойдан",
        ),
        (
            "Нужен контейнер из Китая, 40 фут, кто доставит?",
            ["intent_ask", "container_truck", "china_origin"],
            None,
            "container_words",
            "контейнер",
            "из Китая",
        ),
        (
            "O'zim uchun Xitoydan 1 dona telefon kerak",
            ["intent_ask", "china_origin", "b2c_marker"],
            None,
            "none",
            None,
            "Xitoydan",
        ),
        # Under 1 m³ the number is kept and no volume signal is given (the
        # ≥ 1 m³ threshold is the scorer's, 04 §5.3).
        ("Yarim kub, 0,5 kub yuk kerak", ["intent_ask"], 0.5, "stated", "0,5 kub", None),
        # The largest stated volume speaks for the shipment.
        (
            "3 kub yoki 5 kub tovar kerak",
            ["intent_ask", "volume_m3_3plus"],
            5.0,
            "stated",
            "5 kub",
            None,
        ),
    ],
)
def test_an_ask_in_each_script_carries_its_signals(
    text, signals, volume, basis, volume_text, origin
):
    obs = tm.parse_post(post(text, sender_bio=None))
    assert obs is not None
    assert obs.signal_types == signals
    assert (obs.attrs["volume_m3"], obs.attrs["basis"]) == (volume, basis)
    assert (obs.volume_text, obs.origin_text) == (volume_text, origin)
    assert (obs.company_name_raw, obs.person_role) == (None, None)


def test_a_price_list_with_no_buyer_trigger_is_counted_not_observed():
    """02 §12: a price list is stored so the tout rule can count it; with no
    volume, container or ask in it, it is nobody's demand."""
    narxlar = "Narxlar: stul 250000 so'm, stol 900000 so'm, shkaf 1500000 so'm"
    assert prefilter.hits(narxlar, chat_type="group") == ("pricelist",)
    assert tm.parse_post(post(narxlar)) is None


@pytest.mark.parametrize(
    ("intents", "count", "frequency"),
    [(2, None, None), (3, 3, "repeat"), (7, 7, "repeat")],
)
def test_a_repeat_poster_carries_the_60_day_count(intents, count, frequency):
    obs = tm.parse_post(post(intents_60d=intents))
    assert obs is not None
    assert (obs.attrs["repeat_count_60d"], obs.attrs["frequency"]) == (count, frequency)


# --- a chat's own post (pure) ----------------------------------------------------------

CHANNEL_POST = (
    "Optom mebel! Yangi partiya keldi. Xitoydan 40 fut konteyner. "
    "Tel +998 90 000 00 02, sayt namuna-mebel.example.uz, Instagram: namuna.mebel"
)


def test_a_channels_post_speaks_for_the_channel():
    obs = tm.parse_post(
        post(
            CHANNEL_POST,
            chat=CHANNEL,
            tg_message_id=77,
            sender_id=CHANNEL_ID,
            sender_username="namuna_mebel",
            sender_name="Namuna Mebel Optom",
            sender_bio=None,
        )
    )
    assert obs is not None
    assert (obs.source_ref, obs.url) == (
        "tg:-1001000000002:77",
        "https://t.me/namuna_mebel/77",
    )
    assert (obs.company_name_raw, obs.telegram, obs.person_telegram) == (
        "Namuna Mebel Optom",
        "namuna_mebel",
        None,
    )
    assert obs.signal_types == ["wholesale_bulky", "container_truck", "china_origin"]
    assert obs.phones == ["+998 90 000 00 02"]
    assert (obs.website, obs.instagram) == ("namuna-mebel.example.uz", "namuna.mebel")
    assert obs.external_ids == {"tg_chat_id": str(CHANNEL_ID)}
    assert obs.attrs == {
        "chat_title": "Namuna Mebel Optom",
        "chat_type": "channel",
        "volume_m3": None,
        "basis": "container_words",
    }
    parsed = to_observation_json(obs)
    assert parsed["kind"] == "profile"
    assert parsed["phones"][0]["owner"] == "company"
    assert (parsed["handles"]["telegram"], parsed["handles"]["tg_chat"]) == (
        "namuna_mebel",
        str(CHANNEL_ID),
    )


def test_a_channel_posting_in_a_group_as_itself_speaks_for_that_channel():
    obs = tm.parse_post(
        post(
            "Yangi partiya keldi, optom narxlarda mebel",
            sender_id=CHANNEL_ID,
            sender_username="namuna_mebel",
            sender_name="Namuna Mebel Optom",
            sender_bio=None,
        )
    )
    assert obs is not None
    assert obs.url == "https://t.me/namuna_optom/18234"  # cited where it was said
    assert (obs.company_name_raw, obs.telegram, obs.external_ids) == (
        "Namuna Mebel Optom",
        "namuna_mebel",
        {"tg_chat_id": str(CHANNEL_ID)},
    )
    assert obs.signal_types == ["wholesale_bulky", "container_truck"]
    assert to_observation_json(obs)["kind"] == "profile"


def test_a_chat_with_no_username_is_cited_by_its_t_me_c_link():
    hidden = tm.Chat(GROUP_ID, "group", "Namuna", None)
    obs = tm.parse_post(post(chat=hidden))
    assert obs is not None and obs.url == "https://t.me/c/1000000001/18234"


# --- the tout filter (pure) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "reasons"),
    [
        ({"sender_username": "fx_express"}, ("competitor_sender",)),
        ({"sender_username": "halol_cargo777"}, ("competitor_sender",)),
        ({"sender_name": "Halol Kargo"}, ("competitor_sender",)),
        ({"sender_bio": "Xitoydan yuk tashish xizmati"}, ("competitor_sender",)),
        ({"sender_bio": "Карго Хитой – Тошкент"}, ("competitor_sender",)),
        (
            {"text": "Kargo Xitoy-Toshkent: 1 kub 250$, 3-5 kunda yetkazib beramiz"},
            ("competitor_text",),
        ),
        ({"pricelists_7d": 3}, ("pricelists_7d",)),
        ({"fwd_from_peer_id": OTHER_CHANNEL}, ("forwarded_channel",)),
    ],
)
def test_each_tout_rule_excludes_the_poster_and_keeps_the_offer(fields, reasons):
    text = fields.pop("text", ASK)
    obs = tm.parse_post(post(text, **fields))
    assert obs is not None
    assert obs.attrs["tout"] == list(reasons)
    assert obs.signal_types == ["competitor"]
    # The offer is kept for the weekly diff; the competitor's person is not.
    assert obs.excerpt == text
    assert (obs.person_name, obs.person_role, obs.phones) == (None, None, [])
    assert "tg_user_id" not in obs.attrs
    assert obs.person_telegram == fields.get("sender_username", "namuna_tekstil")
    assert to_observation_json(obs)["hints"]["competitor_words"] is True


@pytest.mark.parametrize(
    "fields",
    [
        {"pricelists_7d": 2},
        {"fwd_from_peer_id": 5100000009},  # a user's message forwarded
    ],
)
def test_below_each_threshold_the_ask_stands(fields):
    obs = tm.parse_post(post(**fields))
    assert obs is not None
    assert "tout" not in obs.attrs
    assert obs.signal_types == ["intent_ask", "volume_m3_3plus", "china_origin"]


def test_a_chat_posting_as_itself_is_judged_by_its_words_and_role_only():
    """A wholesaler channel's price lists copied into its discussion group are
    not a poster's spam: the price-list and forward rules are about people."""
    speaking = {
        "sender_id": CHANNEL_ID,
        "sender_username": "namuna_mebel",
        "sender_name": "Namuna Mebel Optom",
        "sender_bio": None,
    }
    assert tm.tout_reasons(post(PRICE_LIST, pricelists_7d=9, **speaking)) == ()
    assert (
        tm.tout_reasons(post(PRICE_LIST, fwd_from_peer_id=OTHER_CHANNEL, **speaking))
        == ()
    )
    assert tm.tout_reasons(post(PRICE_LIST, competitor_sender=True, **speaking)) == (
        "competitor_chat",
    )
    seeded = tm.Chat(CHANNEL_ID, "channel", "Namuna Kanal", "namuna_kanal", "competitor")
    own = post(PRICE_LIST, chat=seeded, sender_id=CHANNEL_ID, sender_bio=None)
    assert tm.tout_reasons(own) == ("competitor_chat",)
    # A person posting in a competitor-role group is that group's surface.
    surface = tm.Chat(
        GROUP_ID, "group", "Namuna Muhokama", "namuna_muhokama", "competitor"
    )
    assert tm.tout_reasons(post(chat=surface)) == ()


def test_the_seven_day_window_holds_its_ends():
    at = T0
    times = [
        at - timedelta(days=7),  # out: the window is (at - 7 d, at]
        at - timedelta(days=7) + timedelta(seconds=1),
        at,
        at + timedelta(seconds=1),  # later than the message: not yet said
    ]
    assert tm.window_count(times, at=at, window=tm.TOUT_WINDOW) == 2


# --- the small readers (pure) -----------------------------------------------------------


@pytest.mark.parametrize(
    ("bio", "firm"),
    [
        (BIO, tm.Firm("Namuna Tekstil Savdo MChJ", "direktori")),
        (
            "Direktor: Namuna Savdo MChJ | Chorsu",
            tm.Firm("Namuna Savdo MChJ", "Direktor"),
        ),
        ('ООО "Namuna Savdo", Toshkent', tm.Firm('ООО "Namuna Savdo"', None)),
        ("Namuna Mebel XK ta'minot bo'limi", tm.Firm("Namuna Mebel XK", "ta'minot")),
        ("Optom kiyimlar, Chorsu", None),
        (None, None),
    ],
)
def test_the_firm_a_bio_names_by_its_legal_form(bio, firm):
    assert tm.firm_in(bio) == firm


def test_websites_addresses_and_handle_words():
    assert tm.websites_in(
        "Sayt: namuna.example.uz, www.namuna-mebel.example.com va https://t.me/x "
        "Toshkent.Chorsu, 1688.com, https://namuna.example.uz/katalog."
    ) == [
        "namuna.example.uz",
        "www.namuna-mebel.example.com",
        "https://namuna.example.uz/katalog",
    ]
    assert tm.address_in("Manzil: Toshkent, Chorsu bozori 5-qator\nTel") == (
        "Toshkent, Chorsu bozori 5-qator"
    )
    assert tm.address_in("📍 Samarqand, Siyob bozori") == "Samarqand, Siyob bozori"
    assert tm.address_in("Tel +998 90 000 00 01") is None
    assert tm.handle_words("fx_express") == "fx express"
    assert tm.handle_words("halol.cargo777") == "halol cargo"


# --- a channel's about (pure) -----------------------------------------------------------

ABOUT = (
    "Optom mebel va furnitura Xitoydan. Manzil: Toshkent, Chorsu bozori 5-qator\n"
    "Tel: +998 90 000 00 03, +998 71 200 00 04\n"
    "Sayt: https://namuna-mebel.example.uz\nInstagram: @namuna.mebel\nSTIR: 300000011"
)


def _monitor(chat_id: int = CHANNEL_ID, **fields) -> m.ChatMonitor:
    row = m.ChatMonitor(
        tg_chat_id=chat_id,
        chat_type=fields.pop("chat_type", "channel"),
        title=fields.pop("title", "Namuna Mebel Optom"),
        username=fields.pop("username", "namuna_mebel"),
        role=fields.pop("role", "market"),
        **fields,
    )
    return row


def test_a_channels_about_is_a_profile_with_its_contacts():
    body = about.about_body(
        _monitor(), about.About(text=ABOUT, member_count=870), read_at=T0
    )
    [obs] = tm.parse_about(body, url="https://t.me/s/namuna_mebel", fetched_at=T0)
    assert obs == Observation(
        source="telegram_monitor",
        source_ref=f"tg:{CHANNEL_ID}",
        url="https://t.me/s/namuna_mebel",
        fetched_at=T0,
        raw_sha256=hashlib.sha256(body).hexdigest(),
        kind="company",
        company_name_raw="Namuna Mebel Optom",
        stir="300000011",
        phones=["+998 90 000 00 03", "+998 71 200 00 04"],
        telegram="namuna_mebel",
        instagram="namuna.mebel",
        website="https://namuna-mebel.example.uz",
        address="Toshkent, Chorsu bozori 5-qator",
        city="Toshkent",
        origin_text="Xitoydan",
        signal_types=["wholesale_bulky", "china_origin"],
        excerpt=ABOUT,
        external_ids={"tg_chat_id": str(CHANNEL_ID)},
        attrs={
            "chat_type": "channel",
            "chat_title": "Namuna Mebel Optom",
            "member_count": 870,
            "volume_m3": None,
            "basis": "none",
        },
    )
    parsed = to_observation_json(obs)
    assert parsed["kind"] == "profile"
    assert [(p["e164"], p["owner"]) for p in parsed["phones"]] == [
        ("+998900000003", "company"),
        ("+998712000004", "company"),
    ]
    assert (parsed["company"]["website"], parsed["company"]["stir"]) == (
        "https://namuna-mebel.example.uz",
        "300000011",
    )


def test_a_competitors_about_is_kept_as_a_competitor():
    body = about.about_body(
        _monitor(title="Namuna Express", username="namuna_express"),
        about.About(text="Xitoydan yuk tashish, 1 kub 250$"),
        read_at=T0,
    )
    [obs] = tm.parse_about(body, url="https://t.me/s/namuna_express", fetched_at=T0)
    assert (obs.signal_types, obs.attrs["tout"]) == (
        ["competitor"],
        ["competitor_sender", "competitor_text"],
    )
    quiet = about.about_body(_monitor(), about.About(text="Mebel"), read_at=T0)
    [seeded] = tm.parse_about(quiet, url="u", fetched_at=T0, competitor_chat=True)
    assert seeded.attrs["tout"] == ["competitor_chat"]


# --- the runs (Postgres) --------------------------------------------------------------

KEPT = (
    "enabled",
    "paused_until",
    "paused_reason",
    "breaker_until",
    "breaker_reason",
    "last_run_at",
)


@pytest.fixture
async def enabled(session):
    async with SessionLocal() as s:
        row = await s.get(m.Source, "telegram_monitor")
        assert row is not None
        snapshot = {name: getattr(row, name) for name in KEPT}
        row.enabled = True
        await s.commit()
    yield
    async with SessionLocal() as s:
        await s.execute(
            sa.update(m.Source)
            .where(m.Source.key == "telegram_monitor")
            .values(**snapshot)
        )
        await s.commit()


@pytest.fixture
async def offline():
    """A Fetcher that could only answer 404s; the collector must not ask it."""
    site = Site(FakeClock(NOW))
    fetcher = fetcher_for(site)
    yield fetcher
    await fetcher.aclose()
    assert site.seen == []


async def chat(
    session, tg_chat_id=GROUP_ID, chat_type="group", **fields
) -> m.ChatMonitor:
    dialog = chats.DialogInfo(
        tg_chat_id,
        chat_type,
        fields.pop("title", "Namuna Optom"),
        fields.pop("username", "namuna_optom"),
    )
    row = await chats.ensure_monitor(
        session, dialog, now=T0 - timedelta(days=90), **fields
    )
    await session.commit()
    return row


async def store(
    session,
    text: str,
    *,
    message_id: int,
    at: datetime = T0,
    chat_id: int = GROUP_ID,
    chat_type: str = "group",
    sender_id: int | None = POSTER,
    username: str | None = "namuna_tekstil",
    name: str | None = "Rustam",
    bio: str | None = None,
    fwd: int | None = None,
) -> int:
    """One row as the reader stores it (its prefilter, its insert, its notify)."""
    hits = prefilter.hits(text, chat_type=chat_type)
    assert hits, text
    row_id = await ingest.store_hit(
        session,
        ingest.Incoming(
            tg_chat_id=chat_id,
            tg_message_id=message_id,
            text=text,
            occurred_at=at,
            sender_id=sender_id,
            sender_username=username,
            sender_name=name,
            media_kind="none",
            reply_to_message_id=None,
            fwd_from_peer_id=fwd,
        ),
        hits,
    )
    assert row_id is not None
    await session.execute(
        sa.update(m.Interaction)
        .where(m.Interaction.id == row_id)
        .values(created_at=at + timedelta(seconds=20), sender_bio=bio)
    )
    await session.commit()
    return row_id


async def run(fetcher, tmp_path, *, now=NOW, collector=tm.TelegramMonitor):
    return await run_collector(
        collector, fetcher=fetcher, now=now, clock=lambda: now, evidence_root=tmp_path
    )


async def interaction(session, row_id: int) -> m.Interaction:
    row = await session.get(m.Interaction, row_id, populate_existing=True)
    assert row is not None
    return row


async def observations(session) -> list[m.Observation]:
    return list(
        await session.scalars(
            sa.select(m.Observation)
            .order_by(m.Observation.id)
            .execution_options(populate_existing=True)
        )
    )


async def count(session, model) -> int:
    return int(await session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


class Listener:
    """``LISTEN cs_interaction`` on its own autocommit connection."""

    async def __aenter__(self) -> Listener:
        dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self.conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        await self.conn.execute("LISTEN cs_interaction")
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.conn.close()

    async def heard(self, timeout: float = 1.0) -> list[str]:
        return [n.payload async for n in self.conn.notifies(timeout=timeout)]


async def test_a_stored_ask_becomes_evidence_an_observation_and_a_linked_row(
    session, enabled, offline, tmp_path
):
    await chat(session)
    row_id = await store(session, ASK, message_id=18234, bio=BIO)
    async with Listener() as listener:
        report = await run(offline, tmp_path)
        heard = await listener.heard()
    assert report.status == "ok"
    assert (report.counters["new_evidence"], report.counters["new_observations"]) == (
        1,
        1,
    )
    assert report.meta["telegram"] == {
        "messages": 1,
        "touts": 0,
        "counted_only": 0,
        "abouts": 0,
        "linked": 1,
    }
    [proof] = list(await session.scalars(sa.select(m.Evidence)))
    assert (proof.url, proof.source_key, proof.source_ref) == (
        "https://t.me/namuna_optom/18234",
        "telegram_monitor",
        "tg:-1001000000001:18234",
    )
    assert (proof.content_type, proof.bytes, proof.excerpt, proof.sha256) == (
        "text/plain",
        len(ASK.encode()),
        ASK,
        sha(ASK),
    )
    assert (proof.published_at, proof.fetched_at) == (T0, T0 + timedelta(seconds=20))
    assert proof.collector_run_id == report.run_id
    assert gzip.decompress((tmp_path / proof.raw_path).read_bytes()) == ASK.encode()
    [obs] = await observations(session)
    assert (obs.evidence_id, obs.status, obs.source_key) == (
        proof.id,
        "new",
        "telegram_monitor",
    )
    assert (obs.parsed["kind"], obs.parsed["name_key"], obs.parsed["person"]) == (
        "intent_post",
        "namuna tekstil savdo",
        {"name": "Rustam", "role": "direktori"},
    )
    stored = await interaction(session, row_id)
    assert (stored.evidence_id, stored.observation_id, stored.processed_at) == (
        proof.id,
        obs.id,
        None,  # the classifier's queue (T-15)
    )
    # Classify and score are told at once; the collector's own payload.
    assert heard == [f"telegram_monitor:{row_id}"]

    # The resolver reads it as 04 §9.1 does: a new firm and its one person.
    await companies.resolve_batch(now=NOW)
    firm = await session.scalar(
        sa.select(m.Company).execution_options(populate_existing=True)
    )
    assert firm is not None and firm.display_name == "Namuna Tekstil Savdo MChJ"
    [person] = list(await session.scalars(sa.select(m.Contact)))
    assert (person.tier, person.full_name, person.role, person.telegram_username) == (
        "person",
        "Rustam",
        "direktori",
        "namuna_tekstil",
    )
    assert (person.telegram_id, person.legal_basis) == (POSTER, "public_source")


async def test_a_second_run_finds_nothing_left_to_do(session, enabled, offline, tmp_path):
    await chat(session)
    await store(session, ASK, message_id=1)
    first = await run(offline, tmp_path)
    again = await run(offline, tmp_path, now=NOW + timedelta(minutes=15))
    assert first.counters["new_observations"] == 1
    assert (
        again.status,
        again.counters["new_evidence"],
        again.counters["new_observations"],
    ) == (
        "ok",
        0,
        0,
    )
    assert again.meta["telegram"]["messages"] == 0
    assert (await count(session, m.Evidence), await count(session, m.Observation)) == (
        1,
        1,
    )


async def test_three_price_lists_in_seven_days_make_the_poster_a_tout(
    session, enabled, offline, tmp_path
):
    """The window is the 7 days up to each message (03 §2.3): the third and
    the fourth list are a tout's, the first two and a list a fortnight later
    still stand."""
    await chat(session)
    days = (0, 2, 5, 8, 20)
    ids = [
        await store(session, PRICE_LIST, message_id=100 + n, at=T0 + timedelta(days=d))
        for n, d in enumerate(days)
    ]
    report = await run(offline, tmp_path, now=T0 + timedelta(days=21))
    assert report.meta["telegram"]["touts"] == 2
    rows = [await interaction(session, i) for i in ids]
    by_ref = {o.parsed["source_ref"]: o for o in await observations(session)}
    tout = [
        by_ref[f"tg:{GROUP_ID}:{100 + n}"]
        .parsed["hints"]["collector"]["attrs"]
        .get("tout")
        for n in range(5)
    ]
    assert tout == [None, None, ["pricelists_7d"], ["pricelists_7d"], None]
    done = T0 + timedelta(days=21)
    assert [r.processed_at for r in rows] == [None, None, done, done, None]
    assert all(r.observation_id is not None for r in rows)


async def test_a_forward_and_a_bio_mark_touts_and_a_price_list_alone_is_counted(
    session, enabled, offline, tmp_path
):
    await chat(session)
    forwarded = await store(session, ASK, message_id=1, fwd=OTHER_CHANNEL)
    by_bio = await store(
        session,
        "Toshkentga 3 kub yuk kerak",
        message_id=2,
        sender_id=5100000002,
        username="namuna_ikki",
        name="Namuna",
    )
    # The bio arrives later, from the reader's about step (≤ 50 a day).
    await about.fill_bio(session, 5100000002, "Xitoydan yuk tashish xizmati", now=NOW)
    await session.commit()
    narxlar = await store(
        session,
        "Narxlar: stul 250000 so'm, stol 900000 so'm, shkaf 1500000 so'm",
        message_id=3,
        sender_id=5100000003,
        username="namuna_uch",
    )
    async with Listener() as listener:
        report = await run(offline, tmp_path)
        heard = await listener.heard()
    assert report.meta["telegram"] == {
        "messages": 3,
        "touts": 2,
        "counted_only": 1,
        "abouts": 0,
        "linked": 2,
    }
    reasons = {
        o.parsed["source_ref"]: o.parsed["hints"]["collector"]["attrs"]["tout"]
        for o in await observations(session)
    }
    assert reasons == {
        f"tg:{GROUP_ID}:1": ["forwarded_channel"],
        f"tg:{GROUP_ID}:2": ["competitor_sender"],
    }
    for row_id in (forwarded, by_bio):
        row = await interaction(session, row_id)
        assert row.processed_at == NOW and row.observation_id is not None
    counted = await interaction(session, narxlar)
    assert (counted.processed_at, counted.evidence_id, counted.observation_id) == (
        NOW,
        None,
        None,
    )
    assert heard == []  # nothing is due from the classifier
    assert await count(session, m.Evidence) == 2


async def test_rows_of_a_chat_the_reader_may_not_read_are_left_alone(
    session, enabled, offline, tmp_path
):
    off = await chat(session)
    off.monitor_enabled = False
    await session.commit()
    row_id = await store(session, ASK, message_id=1)
    report = await run(offline, tmp_path)
    assert report.meta["telegram"]["messages"] == 0
    row = await interaction(session, row_id)
    assert (row.evidence_id, row.processed_at) == (None, None)
    assert await count(session, m.Observation) == 0


async def test_a_channel_post_is_stored_for_the_channel(
    session, enabled, offline, tmp_path
):
    await chat(
        session,
        CHANNEL_ID,
        "channel",
        title="Namuna Mebel Optom",
        username="namuna_mebel",
    )
    await store(
        session,
        CHANNEL_POST,
        message_id=77,
        chat_id=CHANNEL_ID,
        chat_type="channel",
        sender_id=CHANNEL_ID,
        username="namuna_mebel",
        name="Namuna Mebel Optom",
    )
    await run(offline, tmp_path)
    [obs] = await observations(session)
    assert (obs.parsed["kind"], obs.parsed["company"]["name_raw"]) == (
        "profile",
        "Namuna Mebel Optom",
    )
    assert obs.parsed["handles"]["tg_chat"] == str(CHANNEL_ID)


@pytest.fixture
def evidence_dir(tmp_path, monkeypatch):
    """Where the reader's about step writes its files (it takes no root)."""
    monkeypatch.setattr(settings, "evidence_dir", str(tmp_path))
    return tmp_path


async def _about_row(session, monitor: m.ChatMonitor, text: str = ABOUT) -> m.Evidence:
    row = await about.store_about(
        session, monitor, about.About(text=text, member_count=870), now=T0
    )
    assert row is not None
    await session.commit()
    return row


async def test_the_weekly_about_lands_on_the_readers_evidence_row(
    session, enabled, offline, evidence_dir
):
    monitor = await chat(
        session,
        CHANNEL_ID,
        "channel",
        title="Namuna Mebel Optom",
        username="namuna_mebel",
    )
    proof = await _about_row(session, monitor)
    report = await run(offline, evidence_dir)
    assert report.meta["telegram"]["abouts"] == 1
    assert report.counters["new_evidence"] == 0  # the reader's row is the evidence
    [obs] = await observations(session)
    assert (obs.evidence_id, obs.parsed["kind"], obs.parsed["source_ref"]) == (
        proof.id,
        "profile",
        f"tg:{CHANNEL_ID}",
    )
    assert obs.parsed["text"]["excerpt"] == proof.excerpt
    assert [p["e164"] for p in obs.parsed["phones"]] == ["+998900000003", "+998712000004"]
    assert (obs.parsed["company"]["website"], obs.parsed["handles"]["instagram"]) == (
        "https://namuna-mebel.example.uz",
        "namuna.mebel",
    )
    # An about is never an interactions row (03 §2.3).
    assert await count(session, m.Interaction) == 0
    assert await count(session, m.Evidence) == 1
    again = await run(offline, evidence_dir, now=NOW + timedelta(minutes=15))
    assert again.meta["telegram"]["abouts"] == 0
    assert await count(session, m.Observation) == 1


async def test_an_about_whose_file_is_gone_is_read_from_its_excerpt(
    session, enabled, offline, evidence_dir
):
    monitor = await chat(
        session,
        CHANNEL_ID,
        "channel",
        title="Namuna Mebel Optom",
        username="namuna_mebel",
    )
    proof = await _about_row(session, monitor)
    (evidence_dir / proof.raw_path).unlink()
    await run(offline, evidence_dir)
    [obs] = await observations(session)
    assert obs.evidence_id == proof.id
    assert [p["e164"] for p in obs.parsed["phones"]] == ["+998900000003", "+998712000004"]


async def test_a_groups_about_names_a_community_and_becomes_nothing(
    session, enabled, offline, evidence_dir
):
    monitor = await chat(session)
    await _about_row(session, monitor, "Optom bozor guruhi. Admin: +998 90 000 00 05")
    report = await run(offline, evidence_dir)
    assert report.meta["telegram"]["abouts"] == 0
    assert await count(session, m.Observation) == 0


class _Failing(tm.TelegramMonitor):
    async def after_write(self, session, placed):
        await super().after_write(session, placed)
        raise RuntimeError("after_write")


async def test_a_failed_link_keeps_the_rows_and_the_next_run_links_them(
    session, enabled, offline, tmp_path
):
    """The hook runs in its own savepoint: its failure costs the links, never
    the evidence and observation, and the run ends partial; the next run finds
    the stored row and links the message to the observation already there."""
    await chat(session)
    row_id = await store(session, ASK, message_id=1)
    failed = await run(offline, tmp_path, collector=_Failing)
    assert (failed.status, failed.meta["dropped"]) == ("partial", {"after_write": 1})
    assert (await interaction(session, row_id)).evidence_id is None
    [obs] = await observations(session)
    healed = await run(offline, tmp_path, now=NOW + timedelta(minutes=15))
    assert healed.status == "ok"
    assert healed.counters["new_observations"] == 0
    row = await interaction(session, row_id)
    assert row.observation_id == obs.id and row.evidence_id == obs.evidence_id
