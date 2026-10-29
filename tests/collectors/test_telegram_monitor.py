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

import asyncio
import gzip
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
import sqlalchemy as sa

from clientscrapper.collectors import runner as runner_mod
from clientscrapper.collectors import telegram_monitor as tm
from clientscrapper.collectors.base import Observation
from clientscrapper.collectors.runner import run_collector
from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import chats, companies
from clientscrapper.services.normalise import to_observation_json
from clientscrapper.userbot import about, ingest, prefilter
from tests.fake_telegram import channel as tg_channel
from tests.fake_telegram import message as tg_message
from tests.fake_telegram import user as tg_user
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
        # The binding threshold is ≥ 1 m³ (00): exactly 1 is in, and exactly
        # 3 is the 3-plus step.
        (
            "Xitoydan 1 kub mato kerak",
            ["intent_ask", "volume_m3_1to3", "china_origin"],
            1.0,
            "stated",
            "1 kub",
            "Xitoydan",
        ),
        (
            "3 kub mato kerak",
            ["intent_ask", "volume_m3_3plus"],
            3.0,
            "stated",
            "3 kub",
            None,
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


@pytest.mark.parametrize(
    ("display", "kept"),
    [
        ("Rustam 90 000 00 07", "Rustam"),
        ("Rustam @namuna_boshqa", "Rustam"),
        ("Rustam (WeChat rustam_cn88)", "Rustam"),
        ("Rustam viber rustamk", "Rustam"),
        ("Oʻtkir Gʻaniyev", "Oʻtkir Gʻaniyev"),
        ("🇺🇿 Rustam Karimov", "Rustam Karimov"),
        ("90 000 00 07", None),
    ],
)
def test_the_posters_name_is_its_name_shaped_words(display, kept):
    """06 §5.1: a contact holds one phone and its handle — a display name is
    free text, and a second number or handle typed into it is not stored
    (E2E review 2026-10-29: it reached contacts.full_name and the ERP note)."""
    obs = tm.parse_post(post(sender_name=display))
    assert obs is not None and obs.person_name == kept


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
        # Glued: the word bounds cannot see inside them.
        ({"sender_username": "chinacargouz"}, ("competitor_sender",)),
        ({"sender_name": "HalolCargo"}, ("competitor_sender",)),
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
    # The offer is kept for the weekly diff; the competitor's person is not
    # (06 §5.1), nor any identity a company could be made of.
    assert obs.excerpt == text
    assert (obs.person_name, obs.person_role, obs.person_telegram) == (None, None, None)
    assert (obs.phones, obs.company_name_raw, obs.telegram) == ([], None, None)
    assert "tg_user_id" not in obs.attrs
    parsed = to_observation_json(obs)
    assert parsed["hints"]["competitor_words"] is True
    assert (parsed["kind"], parsed["handles"]["telegram"], parsed["person"]) == (
        "intent_post",
        None,
        None,
    )
    assert companies.tout_refusal(parsed) == "competitor"


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


@pytest.mark.parametrize(
    "text",
    [
        # A buyer who names the service he looks for is GSR's customer (04
        # §6: «kim kargo qiladi?» is a buyer) — the E2E review's four asks
        # and the delivery words a buyer writes.
        "Guangzhoudan 2 kub mato bor, kim kargo qiladi?",
        "Yiwudan 3 kub yuk bor, qaysi kargo yaxshi? Tel +998 90 000 00 01",
        "Xitoydan 5 kub mebel olib kelish kerak, kargo kerak, narxi qancha?",
        "Нужно доставить 4 куба из Гуанчжоу, посоветуйте карго",
        "Guangzhoudan 3 kub mato kim kargo qiladi",  # no «?»: the interrogative
        "rastamojka bilan kim yordam beradi? 5 kub",
        "Нужна доставка 3 куба из Китая",
        "3 kub logistika kerak",
        "express kerak 2 kub",
        "broker kerak 2 kub",
        "Guangzhoudan 3 kub mato, dostavka bilan",  # no cargo core: no offer
    ],
)
def test_a_buyers_ask_that_names_the_service_is_no_tout(text):
    obs = tm.parse_post(post(text))
    assert obs is not None
    assert "tout" not in obs.attrs
    assert obs.person_telegram == "namuna_tekstil"
    assert obs.signal_types and "competitor" not in obs.signal_types


@pytest.mark.parametrize(
    "text",
    [
        "Kargo Xitoy-Toshkent: 1 kub 250$, 3-5 kunda yetkazib beramiz",
        # A tariff beside a rhetorical question is still the forwarder's
        # (keywords.buyer_question: a buyer asks for a price, never states one).
        "Kargo Xitoy-Toshkent: 1 kub 250$, 3 kub kerakmi?",
        "Доставка из Китая: 1 куб 230$, 10 дней",
        "Xitoydan yuk tashish xizmati, 1 kub 240$",
    ],
)
def test_an_offer_of_the_cargo_service_is_a_tout(text):
    obs = tm.parse_post(post(text, sender_bio=None))
    assert obs is not None
    assert obs.attrs["tout"] == ["competitor_text"]


def test_a_wholesalers_delivery_words_are_no_cargo_offer():
    """A channel's own post is its self-description, read as gate 2 reads one
    (COMPETITOR_SELF_RE): every importer delivers."""
    speaking = {
        "sender_id": CHANNEL_ID,
        "sender_username": "namuna_mebel",
        "sender_name": "Namuna Mebel Optom",
        "sender_bio": None,
    }
    own = "Yangi konteyner keldi! Optom mebel, Toshkent bo'ylab dostavka bepul"
    assert tm.tout_reasons(post(own, chat=CHANNEL, **speaking)) == ()
    cargo = "Kargo xizmati: Xitoydan yuk olib kelamiz, 1 kub 250$"
    assert tm.tout_reasons(post(cargo, chat=CHANNEL, **speaking)) == ("competitor_text",)
    # A chat never asks: its question is its offer.
    asked = "Xitoydan yuk olib kelmoqchimisiz? Kargo xizmati"
    assert tm.tout_reasons(post(asked, chat=CHANNEL, **speaking)) == ("competitor_text",)


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


def test_a_wholesalers_about_that_delivers_is_no_competitor():
    """An about is a self-description: gate 2's cargo core reads it, so
    «Toshkent boʻylab dostavka» is a wholesaler's selling point, not a tout."""
    body = about.about_body(
        _monitor(),
        about.About(text="Optom mebel Xitoydan. Toshkent bo'ylab dostavka bepul"),
        read_at=T0,
    )
    [obs] = tm.parse_about(body, url="https://t.me/s/namuna_mebel", fetched_at=T0)
    assert "tout" not in obs.attrs
    assert "competitor" not in obs.signal_types


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


async def test_the_companies_a_run_minted_are_counted_on_it_once_resolved(
    session, enabled, offline, tmp_path
):
    """02 §2.2 ``collector_runs.new_companies``: 0 when the run ends (nothing
    of it is resolved yet), then one per company the resolver creates from
    the run's evidence — never one an observation only attached to. That is
    the number ``/holat``'s «bugun N yangi» and ``/manbalar`` ℹ️ print."""
    from clientscrapper.services import cards, health, owner

    await chat(session)
    await store(session, ASK, message_id=1, bio=BIO)
    await store(session, "Yana 30 kub mato kerak, Guangzhoudan", message_id=2)
    first = await run(offline, tmp_path)
    await store(
        session,
        "Xitoydan 20 kub poyabzal olib kelish kerak",
        message_id=3,
        sender_id=POSTER + 1,
        username="namuna_poyabzal",
        name="Dilshod",
        at=T0 + timedelta(minutes=5),
    )
    second = await run(offline, tmp_path, now=NOW + timedelta(minutes=15))
    assert (first.counters["new_observations"], first.counters["new_companies"]) == (
        2,
        0,
    )
    assert (second.counters["new_observations"], second.counters["new_companies"]) == (
        1,
        0,
    )
    report = await companies.resolve_batch(now=NOW + timedelta(minutes=16))
    assert (report.resolved, report.created) == (3, 2)
    counted = (
        await session.execute(
            sa.select(m.CollectorRun.started_at, m.CollectorRun.new_companies)
            .where(m.CollectorRun.source_key == "telegram_monitor")
            .order_by(m.CollectorRun.started_at)
            .execution_options(populate_existing=True)
        )
    ).all()
    assert counted == [(NOW, 1), (NOW + timedelta(minutes=15), 1)]

    later = NOW + timedelta(minutes=20)
    status = await health.gather(session, now=later)
    [source] = [s for s in status.sources if s.key == "telegram_monitor"]
    assert source.new_companies_today == 2
    assert "· bugun 2 yangi ·" in cards.render_holat(status, [])
    info = await owner.source_info(session, "telegram_monitor", now=later)
    assert info is not None
    assert [r.new_companies for r in info.runs] == [1, 1]


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
    await chat(session, GROUP_ID - 1, username="namuna_ochiq")  # read as usual
    readable = await store(session, ASK, message_id=2, chat_id=GROUP_ID - 1)
    row_id = await store(session, ASK, message_id=1)
    off.monitor_enabled = False  # the owner switched it off after the hit
    await session.commit()
    report = await run(offline, tmp_path)
    assert report.meta["telegram"]["messages"] == 1
    row = await interaction(session, row_id)
    assert (row.evidence_id, row.processed_at) == (None, None)
    assert (await interaction(session, readable)).observation_id is not None
    [obs] = await observations(session)
    assert obs.parsed["source_ref"] == f"tg:{GROUP_ID - 1}:2"


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


# --- review round: the poster's contacts, classifieds, the bio (pure) ---------------


def test_a_phone_in_the_bio_is_the_posters_after_the_texts():
    bio = "Namuna Savdo MChJ, tel +998 90 000 00 07"
    obs = tm.parse_post(post(ASK + " +998 90 000 00 01", sender_bio=bio))
    assert obs is not None
    assert obs.phones == ["+998 90 000 00 01", "+998 90 000 00 07"]
    assert [p["owner"] for p in to_observation_json(obs)["phones"]] == ["person"] * 2


FEED_ID = -1001000000044
FEED = tm.Chat(FEED_ID, "channel", "OPTOM BOZOR | Rasmiy", "optom_bozor", "buyers")


def _ad(text: str, **fields) -> tm.Post:
    speaking = {
        "chat": FEED,
        "sender_id": FEED_ID,
        "sender_username": "optom_bozor",
        "sender_name": "OPTOM BOZOR | Rasmiy",
        "sender_bio": None,
    }
    speaking.update(fields)
    return post(text, **speaking)


def test_the_classifieds_feed_is_seeded_as_a_buyers_chat():
    """03 §2.3: «@Optom_bozor (classifieds feed: the lead is the poster, not
    the channel)»."""
    assert chats.Seed("optom_bozor", chats.BUYERS_ROLE) in chats.SEEDS
    assert chats.BUYERS_ROLE == "buyers"


def test_a_classifieds_ad_speaks_for_its_poster_never_for_the_feed():
    text = (
        "Namuna Mebel MChJ: optom mebel Xitoydan, yangi partiya keldi. "
        "Tel +998 90 000 00 11, @namuna_mebelchi, @optom_bozor"
    )
    obs = tm.parse_post(_ad(text, tg_message_id=100))
    assert obs is not None
    assert (obs.url, obs.source_ref) == (
        "https://t.me/optom_bozor/100",
        f"tg:{FEED_ID}:100",
    )
    # Nothing of the feed: its title, handle and id name the board, not a lead.
    assert (obs.company_name_raw, obs.telegram, obs.external_ids) == (
        "Namuna Mebel MChJ",
        None,
        {},
    )
    assert (obs.person_telegram, obs.phones) == ("namuna_mebelchi", ["+998 90 000 00 11"])
    assert obs.signal_types == ["wholesale_bulky", "container_truck", "china_origin"]
    assert obs.attrs["chat_type"] == "classifieds"
    parsed = to_observation_json(obs)
    assert parsed["kind"] == "intent_post"  # the poster's: person-tier, R3 scrubs it
    assert parsed["handles"]["telegram"] == "namuna_mebelchi"
    assert "tg_chat" not in parsed["handles"]
    assert [(p["e164"], p["owner"]) for p in parsed["phones"]] == [
        ("+998900000011", "person")
    ]


def test_a_classifieds_ask_carries_the_askers_signals():
    obs = tm.parse_post(_ad("Kerak: Xitoydan 5 kub mato, optom"))
    assert obs is not None
    assert obs.signal_types == [
        "intent_ask",
        "wholesale_bulky",
        "volume_m3_3plus",
        "china_origin",
    ]
    # B2C words count only beside an ask; a seller's «1 dona» is a price.
    seller = tm.parse_post(_ad("Optom mebel, yangi partiya keldi: 1 dona 900000 so'm"))
    assert seller is not None and "b2c_marker" not in seller.signal_types


def test_a_classifieds_tout_is_nobodys():
    obs = tm.parse_post(_ad("Kargo Xitoy-Toshkent: 1 kub 250$. Tel +998 90 000 00 12"))
    assert obs is not None
    assert (obs.signal_types, obs.attrs["tout"]) == (["competitor"], ["competitor_text"])
    assert (obs.phones, obs.person_telegram, obs.company_name_raw) == ([], None, None)
    assert companies.tout_refusal(to_observation_json(obs)) == "competitor"
    # A post the feed forwards from another channel is that channel's offer.
    relay = tm.parse_post(_ad("Optom mebel keldi", fwd_from_peer_id=OTHER_CHANNEL))
    assert relay is not None and relay.attrs["tout"] == ["forwarded_channel"]


def test_a_person_in_a_buyers_group_still_speaks_for_themself():
    companion = tm.Chat(
        GROUP_ID, "group", "Namuna Xaridorlar", "namuna_xaridorlar", "buyers"
    )
    obs = tm.parse_post(post(chat=companion))
    assert obs is not None
    assert (obs.attrs["chat_type"], obs.person_telegram) == ("group", "namuna_tekstil")


# --- review round: the runs (Postgres) -----------------------------------------------


async def _evidence_of(session, row_id: int) -> m.Evidence:
    row = await interaction(session, row_id)
    proof = await session.get(m.Evidence, row.evidence_id, populate_existing=True)
    assert proof is not None
    return proof


async def test_two_messages_with_the_same_words_are_each_cited_where_said(
    session, enabled, offline, tmp_path
):
    """The same ask cross-posted to two groups by two people: two evidence
    rows, each at its own link, each message linked to its own (02 §8: the
    card and the push cite evidence.url)."""
    await chat(session)
    await chat(session, GROUP_ID - 1, username="namuna_ikki_guruh")
    first = await store(session, ASK, message_id=11, username="namuna_bir", name="Bir")
    second = await store(
        session,
        ASK,
        message_id=22,
        chat_id=GROUP_ID - 1,
        sender_id=5100000077,
        username="namuna_boshqa",
        name="Boshqa",
        at=T0 + timedelta(hours=3),
    )
    report = await run(offline, tmp_path)
    assert (report.counters["new_evidence"], report.counters["new_observations"]) == (
        2,
        2,
    )
    for row_id, url, ref, at in (
        (first, "https://t.me/namuna_optom/11", f"tg:{GROUP_ID}:11", T0),
        (
            second,
            "https://t.me/namuna_ikki_guruh/22",
            f"tg:{GROUP_ID - 1}:22",
            T0 + timedelta(hours=3),
        ),
    ):
        proof = await _evidence_of(session, row_id)
        assert (proof.url, proof.source_ref, proof.published_at, proof.sha256) == (
            url,
            ref,
            at,
            sha(ASK),
        )
        row = await interaction(session, row_id)
        obs = await session.get(m.Observation, row.observation_id)
        assert obs is not None
        assert (obs.evidence_id, obs.parsed["url"]) == (proof.id, url)


async def test_a_bio_the_sender_already_has_is_read_for_their_next_message(
    session, enabled, offline, tmp_path
):
    """The real order (03 §2.3): the collector wakes on the reader's NOTIFY
    within seconds, the bio comes hours later from the about pass. The bio
    then sits on the sender's earlier rows, and the next message, stored with
    none (``store_hit`` never writes one), is judged with it."""
    await chat(session)
    await store(session, ASK, message_id=1, at=T0 - timedelta(days=3))
    await run(offline, tmp_path, now=T0 - timedelta(days=3) + timedelta(minutes=1))
    await about.fill_bio(session, POSTER, BIO, now=T0 - timedelta(days=2))
    await session.commit()
    later = await store(session, "Toshkentga 3 kub mato yuk kerak", message_id=2)
    assert (await interaction(session, later)).sender_bio is None
    await run(offline, tmp_path)
    obs = await session.get(
        m.Observation, (await interaction(session, later)).observation_id
    )
    assert obs is not None
    assert (obs.parsed["company"]["name_raw"], obs.parsed["person"]) == (
        "Namuna Tekstil Savdo MChJ",
        {"name": "Rustam", "role": "direktori"},
    )


async def test_a_tout_bio_on_an_earlier_row_makes_the_next_message_a_touts(
    session, enabled, offline, tmp_path
):
    await chat(session)
    await store(
        session,
        "Toshkentga 2 kub yuk kerak",
        message_id=1,
        at=T0 - timedelta(days=3),
        bio="Xitoydan yuk tashish xizmati",
    )
    await run(offline, tmp_path, now=T0 - timedelta(days=3) + timedelta(minutes=1))
    later = await store(session, "Toshkentga 3 kub yuk kerak", message_id=2)
    await run(offline, tmp_path)
    row = await interaction(session, later)
    obs = await session.get(m.Observation, row.observation_id)
    assert obs is not None
    assert obs.parsed["hints"]["collector"]["attrs"]["tout"] == ["competitor_sender"]
    assert row.processed_at == NOW


async def test_the_bio_cache_is_the_newest_bio_of_30_days(session):
    """``chats.known_bios``, the one home of the reader's 30-day cache: the
    newest bio any row of the sender holds, '' (read, and empty) included."""
    await chat(session)
    rows = [
        (POSTER, 31, "Xitoydan yuk tashish xizmati"),  # past the cache
        (POSTER, 29, "Eski bio"),
        (POSTER, 10, BIO),
        (POSTER, 1, None),  # not read yet
        (5100000004, 31, "Juda eski"),
        (5100000006, 5, ""),
    ]
    for n, (sender, days, bio) in enumerate(rows):
        await store(
            session,
            ASK,
            message_id=n + 1,
            sender_id=sender,
            at=NOW - timedelta(days=days),
            bio=bio,
        )
    known = await chats.known_bios(
        session, [POSTER, 5100000004, 5100000005, 5100000006], now=NOW
    )
    assert known == {POSTER: BIO, 5100000006: ""}
    assert await about.cached_bio(session, POSTER, now=NOW) == BIO
    assert await about.cached_bio(session, 5100000004, now=NOW) is None


GROUP_ENTITY = tg_channel(
    1000000001, title="Namuna Optom", username="namuna_optom", megagroup=True
)
POSTER_ENTITY = tg_user(POSTER, first_name="Rustam", username="namuna_tekstil")


class _EditMeanwhile(tm.TelegramMonitor):
    """The reader's edit of a message the run is handling arrives mid-run,
    from the reader's own session, through its own handler."""

    edit: asyncio.Task[str] | None = None
    waiting: bool | None = None

    async def collect(self, session, budget):
        found = await super().collect(session, budget)

        async def edit() -> str:
            async with SessionLocal() as reader:
                newer = tg_message(
                    GROUP_ENTITY,
                    9,
                    "Guangzhoudan 1 dona sumka kerak, o'zim uchun",
                    date=T0,
                    sender=POSTER_ENTITY,
                )
                outcome = await ingest.handle_edit(
                    reader, newer, now=NOW, edits=ingest.EditCounter()
                )
                await reader.commit()
                return outcome

        type(self).edit = asyncio.ensure_future(edit())
        await asyncio.sleep(0.5)
        type(self).waiting = not type(self).edit.done()
        return found


async def test_an_edit_during_a_run_waits_and_never_splits_row_from_evidence(
    session, enabled, offline, tmp_path
):
    """04 §0: a job holds the rows it claims (FOR UPDATE SKIP LOCKED). The
    edit waits for the run's commit, then finds the row handled and keeps it
    as its evidence quotes it."""
    await chat(session)
    row_id = await store(session, ASK, message_id=9)
    await run(offline, tmp_path, collector=_EditMeanwhile)
    assert _EditMeanwhile.edit is not None
    assert _EditMeanwhile.waiting is True
    assert await asyncio.wait_for(_EditMeanwhile.edit, timeout=5) == "unchanged"
    proof = await _evidence_of(session, row_id)
    stored = gzip.decompress((tmp_path / proof.raw_path).read_bytes()).decode()
    row = await interaction(session, row_id)
    assert row.text == stored == ASK


async def test_a_touts_message_names_nobody_and_is_rejected_as_a_competitor(
    session, enabled, offline, tmp_path
):
    """06 §5.1: a competitor's person has no lead purpose — no company, no
    contact; the observation is rejected 'competitor', so R2a takes the
    message row at 30 d and only the evidence stays for the weekly diff."""
    await chat(session)
    forwarded = await store(
        session,
        ASK,
        message_id=7,
        fwd=OTHER_CHANNEL,
        username="namuna_tout",
        name="Namuna Odam",
    )
    worded = await store(
        session,
        "Kargo Xitoy-Toshkent: 1 kub 250$, 3 kub kerakmi? +998 90 000 00 55",
        message_id=8,
        sender_id=5100000055,
        username="namuna_tout2",
        name="Ikki",
    )
    await run(offline, tmp_path)
    report = await companies.resolve_batch(now=NOW)
    assert report.rejected == 2
    for row_id in (forwarded, worded):
        row = await interaction(session, row_id)
        obs = await session.get(m.Observation, row.observation_id, populate_existing=True)
        assert obs is not None
        assert (obs.status, obs.reject_reason, obs.company_id) == (
            "rejected",
            "competitor",
            None,
        )
        assert (obs.parsed["handles"]["telegram"], obs.parsed["phones"]) == (None, [])
    assert (await count(session, m.Company), await count(session, m.Contact)) == (0, 0)


R3_SCRUB = (
    # 02 §4 R3, verbatim (T-31 builds the retention job).
    "UPDATE observations SET parsed = (parsed - 'phones' - 'person') || CASE WHEN "
    "parsed->>'kind' = 'intent_post' THEN jsonb_build_object('handles', "
    "(parsed->'handles') - 'tg_user_id' - 'telegram') ELSE '{}'::jsonb END || "
    "jsonb_build_object('scrubbed', true), scrubbed_at = now() WHERE scrubbed_at IS NULL "
    "AND coalesce(resolved_at, created_at) < now() - interval '30 days'"
)


def _leaves(tree) -> list:
    if isinstance(tree, dict):
        return [x for k, v in tree.items() for x in [k, *_leaves(v)]]
    if isinstance(tree, list):
        return [x for v in tree for x in _leaves(v)]
    return [tree]


async def test_r3_leaves_no_trace_of_the_poster_in_an_intent_post(
    session, enabled, offline, tmp_path
):
    await chat(session)
    await store(session, ASK + " +998 90 000 00 01", message_id=31, bio=BIO)
    await run(offline, tmp_path)
    [obs] = await observations(session)
    assert POSTER in _leaves(obs.parsed)  # handles.tg_user_id, before R3
    await session.execute(
        sa.update(m.Observation).values(created_at=sa.func.now() - timedelta(days=31))
    )
    await session.execute(sa.text(R3_SCRUB))
    await session.commit()
    [scrubbed] = await observations(session)
    leaves = _leaves(scrubbed.parsed)
    assert scrubbed.parsed["scrubbed"] is True
    for trace in (POSTER, "tg_user_id", "namuna_tekstil", "Rustam", "+998900000001"):
        assert trace not in leaves, trace
    # The firm is company-tier and stays.
    assert scrubbed.parsed["company"]["name_raw"] == "Namuna Tekstil Savdo MChJ"


async def test_classifieds_ads_become_their_posters_not_the_feed(
    session, enabled, offline, tmp_path, evidence_dir
):
    monitor = await chat(
        session,
        FEED_ID,
        "channel",
        title="OPTOM BOZOR | Rasmiy",
        username="optom_bozor",
        role=chats.BUYERS_ROLE,
    )
    ads = [
        "Optom mebel Xitoydan, yangi partiya keldi. "
        "Tel +998 90 000 00 11, @namuna_mebelchi",
        "Optom poyabzal, zavod narxida, yangi partiya. "
        "Tel +998 90 000 00 12, @namuna_oyoq",
    ]
    for n, text in enumerate(ads):
        await store(
            session,
            text,
            message_id=100 + n,
            chat_id=FEED_ID,
            chat_type="channel",
            sender_id=FEED_ID,
            username="optom_bozor",
            name="OPTOM BOZOR | Rasmiy",
        )
    # The feed's own about names the board and its admin, not a lead.
    await _about_row(session, monitor, "E'lon berish uchun: +998 90 000 00 19")
    report = await run(offline, evidence_dir)
    assert report.meta["telegram"]["abouts"] == 0
    found = await observations(session)
    assert [o.parsed["kind"] for o in found] == ["intent_post", "intent_post"]
    await companies.resolve_batch(now=NOW)
    firms = list(
        await session.scalars(sa.select(m.Company).order_by(m.Company.first_seen_at))
    )
    assert len(firms) == 2
    assert all("OPTOM BOZOR" not in (f.display_name or "") for f in firms)
    people = {
        c.phone: (c.tier, c.telegram_username, c.company_id)
        for c in await session.scalars(sa.select(m.Contact))
    }
    assert {phone: v[:2] for phone, v in people.items()} == {
        "+998900000011": ("person", "namuna_mebelchi"),
        "+998900000012": ("person", "namuna_oyoq"),
    }
    assert len({v[2] for v in people.values()}) == 2  # one seller, one company


async def test_a_repeat_poster_is_counted_over_60_days_of_stored_asks(
    session, enabled, offline, tmp_path
):
    """03 §2.3: ≥ 3 intent posts in 60 days. The earlier asks were handled by
    earlier runs, so the count comes from the stored history, not the batch;
    a price list is no intent post, and an ask 61 days back is out."""
    await chat(session)
    for n, days in enumerate((61, 59, 30, 10)):
        await store(session, ASK, message_id=10 + n, at=T0 - timedelta(days=days))
    narxlar = "Narxlar: stul 250000 so'm, stol 900000 so'm, shkaf 1500000 so'm"
    await store(session, narxlar, message_id=20, at=T0 - timedelta(days=5))
    await run(offline, tmp_path, now=T0 - timedelta(days=1))
    newest = await store(session, ASK, message_id=30)
    report = await run(offline, tmp_path)
    assert report.meta["telegram"]["messages"] == 1
    row = await interaction(session, newest)
    obs = await session.get(m.Observation, row.observation_id)
    assert obs is not None
    attrs = obs.parsed["hints"]["collector"]["attrs"]
    assert (attrs["repeat_count_60d"], attrs["frequency"]) == (4, "repeat")


COMPETITOR_CHANNEL = -1001000000066


async def test_a_competitor_role_channel_is_a_tout_in_a_group_and_in_its_about(
    session, enabled, offline, evidence_dir
):
    """The seeds' cargo channels (03 §2.3, role 'competitor'): their posts in
    a group as themselves, and their weekly about, are a competitor's even
    when no word says so. A chat's tout keeps the chat (company-tier)."""
    await chat(session)
    seeded = await chat(
        session,
        COMPETITOR_CHANNEL,
        "channel",
        title="Namuna Kanal",
        username="namuna_kanal",
        role="competitor",
    )
    in_group = await store(
        session,
        "Yangi partiya keldi, optom narxlarda mebel",
        message_id=5,
        sender_id=COMPETITOR_CHANNEL,
        username="namuna_kanal",
        name="Namuna Kanal",
    )
    await _about_row(session, seeded, "Mebel optom Xitoydan. Tel +998 90 000 00 66")
    report = await run(offline, evidence_dir)
    assert (report.meta["telegram"]["touts"], report.meta["telegram"]["abouts"]) == (1, 1)
    tout = {
        o.parsed["source_ref"]: o.parsed["hints"]["collector"]["attrs"].get("tout")
        for o in await observations(session)
    }
    assert tout == {
        f"tg:{GROUP_ID}:5": ["competitor_chat"],
        f"tg:{COMPETITOR_CHANNEL}": ["competitor_chat"],
    }
    row = await interaction(session, in_group)
    assert row.processed_at == NOW
    obs = await session.get(m.Observation, row.observation_id)
    assert obs is not None
    assert (obs.parsed["kind"], obs.parsed["handles"]["telegram"]) == (
        "profile",
        "namuna_kanal",
    )


async def test_an_about_whose_file_does_not_match_its_row_is_read_from_the_row(
    session, enabled, offline, evidence_dir
):
    """The file is the row's bytes only while it hashes to the row's sha256;
    another file at that path (a restore gone wrong) is not read."""
    monitor = await chat(
        session,
        CHANNEL_ID,
        "channel",
        title="Namuna Mebel Optom",
        username="namuna_mebel",
    )
    proof = await _about_row(session, monitor)
    other = about.about_body(
        monitor, about.About(text="Tel +998 90 000 00 09"), read_at=T0
    )
    (evidence_dir / proof.raw_path).write_bytes(gzip.compress(other))
    await run(offline, evidence_dir)
    [obs] = await observations(session)
    assert [p["e164"] for p in obs.parsed["phones"]] == ["+998900000003", "+998712000004"]


async def test_an_about_observation_quotes_its_evidence_rows_excerpt(
    session, enabled, offline, evidence_dir
):
    """02 §2.3: the excerpt is what the card quotes — the row's, as stored,
    whatever the parser would cut from the file."""
    monitor = await chat(
        session,
        CHANNEL_ID,
        "channel",
        title="Namuna Mebel Optom",
        username="namuna_mebel",
    )
    proof = await _about_row(session, monitor)
    quoted = "Optom mebel va furnitura Xitoydan"
    await session.execute(
        sa.update(m.Evidence).where(m.Evidence.id == proof.id).values(excerpt=quoted)
    )
    await session.commit()
    await run(offline, evidence_dir)
    [obs] = await observations(session)
    assert obs.parsed["text"]["excerpt"] == quoted


async def test_the_about_of_a_switched_off_channel_waits(
    session, enabled, offline, evidence_dir
):
    monitor = await chat(
        session,
        CHANNEL_ID,
        "channel",
        title="Namuna Mebel Optom",
        username="namuna_mebel",
    )
    await _about_row(session, monitor)
    monitor.monitor_enabled = False  # the owner's /chats
    await session.commit()
    off = await run(offline, evidence_dir)
    assert (off.meta["telegram"]["abouts"], await count(session, m.Observation)) == (0, 0)
    monitor.monitor_enabled = True
    await session.commit()
    on = await run(offline, evidence_dir, now=NOW + timedelta(minutes=15))
    assert (on.meta["telegram"]["abouts"], await count(session, m.Observation)) == (1, 1)


async def test_a_message_whose_observation_was_refused_is_done_with_its_evidence(
    session, enabled, offline, tmp_path, monkeypatch
):
    """The database refused the observation (``bad_observation``): the
    evidence stays, and no later run writes an observation on evidence it
    already holds — so the row is linked to it and done, not read again every
    run at the head of the queue. The runner adopts only a row stored outside
    any run (the reader's about), never one an earlier run wrote."""
    await chat(session)
    row_id = await store(session, ASK, message_id=1)
    real = runner_mod.to_observation_json

    def refusing(obs, **kwargs):
        if obs.source_ref == f"tg:{GROUP_ID}:1":
            raise ValueError("refused")
        return real(obs, **kwargs)

    monkeypatch.setattr(runner_mod, "to_observation_json", refusing)
    first = await run(offline, tmp_path)
    assert (first.status, first.meta["dropped"]) == ("partial", {"bad_observation": 1})
    assert (await interaction(session, row_id)).evidence_id is None
    monkeypatch.setattr(runner_mod, "to_observation_json", real)
    later = NOW + timedelta(minutes=15)
    second = await run(offline, tmp_path, now=later)
    assert second.status == "ok"
    assert (
        second.meta["telegram"]["unobserved"],
        second.counters["new_observations"],
    ) == (
        1,
        0,
    )
    [proof] = list(await session.scalars(sa.select(m.Evidence)))
    row = await interaction(session, row_id)
    assert (row.evidence_id, row.observation_id, row.processed_at) == (
        proof.id,
        None,
        later,
    )
    third = await run(offline, tmp_path, now=later + timedelta(minutes=15))
    assert third.meta["telegram"]["messages"] == 0


async def test_a_message_seen_again_under_a_new_link_cites_the_first_observation(
    session, enabled, offline, tmp_path
):
    """The link step failed and the chat took a new username before the next
    run: the message is a repeat of the last 24 h at another URL (the runner's
    duplicate), and the row is linked to the observation already written."""
    monitor = await chat(session)
    row_id = await store(session, ASK, message_id=1)
    failed = await run(offline, tmp_path, collector=_Failing)
    assert failed.meta["dropped"] == {"after_write": 1}
    [first] = await observations(session)
    monitor.username = "namuna_yangi"
    await session.commit()
    async with Listener() as listener:
        healed = await run(offline, tmp_path, now=NOW + timedelta(minutes=15))
        heard = await listener.heard()
    assert healed.counters["new_observations"] == 0
    row = await interaction(session, row_id)
    assert (row.observation_id, row.evidence_id, row.processed_at) == (
        first.id,
        first.evidence_id,
        None,
    )
    assert heard == [f"telegram_monitor:{row_id}"]


async def test_a_message_whose_evidence_was_refused_is_read_again(
    session, enabled, offline, tmp_path, monkeypatch
):
    """No evidence row, nothing to link: the row stays in the queue, counted
    (``unplaced``), and the next run stores it."""
    await chat(session)
    row_id = await store(session, ASK, message_id=1)
    real = runner_mod.evidence_store.store

    async def refusing(session, **kwargs):
        if kwargs["url"] == "https://t.me/namuna_optom/1":
            raise ValueError("refused")
        return await real(session, **kwargs)

    monkeypatch.setattr(runner_mod.evidence_store, "store", refusing)
    first = await run(offline, tmp_path)
    assert (first.status, first.meta["dropped"]) == ("partial", {"bad_evidence": 1})
    assert first.meta["telegram"]["unplaced"] == 1
    row = await interaction(session, row_id)
    assert (row.evidence_id, row.processed_at) == (None, None)
    monkeypatch.setattr(runner_mod.evidence_store, "store", real)
    second = await run(offline, tmp_path, now=NOW + timedelta(minutes=15))
    assert (second.status, second.counters["new_observations"]) == ("ok", 1)
    assert "unplaced" not in second.meta["telegram"]
    assert (await interaction(session, row_id)).observation_id is not None
