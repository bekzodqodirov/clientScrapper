"""No raw phone, handle, e-mail, name or PINFL reaches the model (06 §5.2,
04 AC 5): through the fake transport every request is seen as the API would
see it, and every stored request is grepped (02 §2.6).

The messages run the production path: the reader's insert, the
telegram_monitor run, then ``classify_batch`` — over a SYNTHETIC answer
(tests/fixtures/model/README.md).
"""

from __future__ import annotations

import json
import re

import httpx

from clientscrapper.services import classify
from tests import classify_fixtures as cf
from tests.classify_fixtures import NOW, chat, interaction, link, model_jobs, store
from tests.fakes import FakeAnthropic, message, model_fixture, pii_in

# The shared fixtures (tests/classify_fixtures.py).
enabled = cf.enabled
offline = cf.offline

# 02 §2.6: what a stored request must never hold.
RAW = re.compile(r"\+998\d{9}|@[a-z0-9_]{5,}")
TEXT = (
    "Rustam aka, Guangzhoudan 2 kub mato kerak. Tel +998 90 000 00 01, "
    "@namuna_tekstil, pochta rustam@namuna.example.uz, Instagram: namuna_shop, "
    "JSHSHIR 31234567890123"
)
BIO = "Namuna Tekstil Savdo MChJ direktori, 90 000 00 02, t.me/namuna_savdo"
SECRETS = (
    "+998 90 000 00 01",
    "998900000001",
    "namuna_tekstil",
    "rustam@namuna.example.uz",
    "namuna_shop",
    "31234567890123",
    "90 000 00 02",
    "namuna_savdo",
    "Rustam",
    "Karimov",
)
BUYER = model_fixture("classify_buyer_stated.json")


def visible(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False)


async def test_the_api_sees_no_personal_data(
    session, enabled, offline, tmp_path, model_api
):
    await chat(session, title="Namuna Optom 90 000 00 07")
    row_id = await store(session, TEXT, message_id=11, bio=BIO)
    await link(offline, tmp_path)
    model_api.queue(message(BUYER))

    report = await classify.classify_batch(now=NOW)
    assert report.applied == 1

    [body] = model_api.requests
    assert pii_in(body) == []
    seen = visible(body)
    for value in SECRETS:
        assert value.casefold() not in seen.casefold(), value
    assert body["messages"][0]["content"] == (
        "SOURCE: telegram group «Namuna Optom <PHONE_1>»\n"
        "DATE: 2026-10-24T11:58:00+05:00\n"
        "SENDER_BIO: Namuna Tekstil Savdo MChJ direktori, <PHONE_2>, t.me/<TG_1>\n"
        "TEXT: <NAME_1> aka, Guangzhoudan 2 kub mato kerak. Tel <PHONE_3>, "
        "@<TG_2>, pochta <EMAIL_1>, Instagram: <IG_1>, JSHSHIR <ID>"
    )
    stored = await interaction(session, row_id)
    assert stored.processed_at == NOW


async def test_stored_requests_hold_no_raw_phone_or_handle(
    session, enabled, offline, tmp_path, model_api
):
    await chat(session)
    for n, text in enumerate(
        (
            TEXT,
            "Рустамдан сўранг: 5 куб ўйинчоқ керак, тел 90 000-00-03, @namuna_oyinchoq",
            "Нужно привезти 3 кубa мебели, пишите namuna@mail.example.uz, +998900000004",
        ),
        start=21,
    ):
        await store(session, text, message_id=n, bio=BIO, name="Рустам")
    await link(offline, tmp_path)
    model_api.queue(message(BUYER), message(BUYER), message(BUYER))

    report = await classify.classify_batch(now=NOW)
    assert report.applied == 3

    jobs = await model_jobs(session)
    assert len(jobs) == 3 == len(model_api.requests)
    for job, body in zip(jobs, model_api.requests, strict=True):
        request = visible(job.request)
        assert RAW.search(request) is None
        assert pii_in(job.request) == []
        for value in (*SECRETS, "namuna_oyinchoq", "Рустам", "90 000-00-03"):
            assert value.casefold() not in request.casefold(), value
        # What was stored is what was sent.
        assert job.request["user"] == body["messages"][0]["content"]
        # The reverse map stays here, with the values (30 d, never logged).
        kinds = ("PHONE", "TG", "EMAIL", "IG", "NAME")
        assert set(job.pseudonyms) <= {f"<{k}_{i}>" for k in kinds for i in (1, 2, 3)}
    assert jobs[0].pseudonyms["<TG_2>"] == "namuna_tekstil"
    assert jobs[0].pseudonyms["<NAME_1>"] == "Rustam"
    assert jobs[1].pseudonyms["<NAME_1>"] == "Рустам"


def test_the_fence_sees_a_leak():
    """The transport's own check — what tests/conftest.py fails a test on."""
    fake = FakeAnthropic().queue(message(BUYER), message(BUYER))
    leaking = {"messages": [{"role": "user", "content": "tel +998 90 000 00 01"}]}
    fake(httpx.Request("POST", "https://api.anthropic.test/v1/messages", json=leaking))
    masked = {"messages": [{"role": "user", "content": "tel <PHONE_1>, @<TG_1>"}]}
    fake(httpx.Request("POST", "https://api.anthropic.test/v1/messages", json=masked))
    assert fake.leaks == [["PHONE_RE"]]
    assert pii_in({"a": ["x", {"b": "info@namuna.example.uz"}]}) == ["EMAIL_RE"]
    assert pii_in({"k": "t.me/namuna_optom", "j": "31234567890123"}) == [
        "PINFL_RE",
        "TG_RE",
    ]
    # The fence is pseudonyms.unmasked: the shapes the E2E review found, and
    # a labelled STIR left as the company-tier fact it is (04 §4.2).
    assert pii_in({"a": "WeChat: rustam_cn88", "b": "+7 701 000 00 00"}) == [
        "INTL_PHONE_RE",
        "MESSENGER_ID_RE",
    ]
    assert pii_in({"a": "Namuna Savdo MChJ, STIR 300000011"}) == []
    fake(httpx.Request("POST", "https://api.anthropic.test/v1/messages", json=masked))
    assert fake.unexpected == 1


async def test_names_reach_the_model_in_no_spelling(
    session, enabled, offline, tmp_path, model_api
):
    """04 §4.2's names: the sender's display name, in any apostrophe and with
    any ending, and the people a bio names (``normalise.person_names``)."""
    await chat(session)
    await store(
        session,
        "Men O’tkir, Guangzhoudan 2 kub mato kerak, kim olib keladi? Otkirga yozing",
        message_id=31,
        sender_id=5100000031,
        username=None,
        name="Oʻtkir Gʻaniyev",
        bio="Oʻtkir Gʻaniyev, Chilonzor",
    )
    await store(
        session,
        "Guangzhoudan 3 kub mato kerak, kim olib keladi? Murojaat: Dilshodga",
        message_id=32,
        sender_id=5100000032,
        username=None,
        name="Sardor",
        bio="Menejer: Dilshod Karimov",
    )
    await store(
        session,
        "Нужно привезти 3 куба ткани из Китая, звоните Рустаму или Ивановой",
        message_id=33,
        sender_id=5100000033,
        username=None,
        name="Рустам Иванов",
        bio=None,
    )
    await link(offline, tmp_path)
    model_api.queue(message(BUYER), message(BUYER), message(BUYER))
    assert (await classify.classify_batch(now=NOW)).applied == 3
    first, second, third = (r["messages"][0]["content"] for r in model_api.requests)
    assert first.splitlines()[2:] == [
        "SENDER_BIO: <NAME_1> <NAME_2>, Chilonzor",
        # One person, one token, however the text spells the name.
        "TEXT: Men <NAME_1>, Guangzhoudan 2 kub mato kerak, kim olib keladi? "
        "<NAME_1>ga yozing",
    ]
    assert second.splitlines()[2:] == [
        "SENDER_BIO: Menejer: <NAME_1> <NAME_2>",
        "TEXT: Guangzhoudan 3 kub mato kerak, kim olib keladi? Murojaat: <NAME_1>ga",
    ]
    assert third.splitlines()[3] == (
        "TEXT: Нужно привезти 3 куба ткани из Китая, звоните <NAME_1>у или <NAME_2>ой"
    )
    seen = " ".join((first, second, third)).casefold()
    for value in ("tkir", "aniyev", "dilshod", "karimov", "sardor", "рустам", "иванов"):
        assert value not in seen, value
    jobs = await model_jobs(session)
    assert jobs[1].pseudonyms == {"<NAME_1>": "Dilshod", "<NAME_2>": "Karimov"}


async def test_a_capitalised_telegram_link_is_masked(
    session, enabled, offline, tmp_path, model_api
):
    await chat(session)
    await store(
        session,
        "Guangzhoudan 2 kub mato kerak. T.me/namuna_tekstil ga yozing, "
        "kanal TELEGRAM.ME/namuna_optom",
        message_id=41,
    )
    await link(offline, tmp_path)
    model_api.queue(message(BUYER))
    assert (await classify.classify_batch(now=NOW)).applied == 1
    [body] = model_api.requests
    assert pii_in(body) == []
    assert body["messages"][0]["content"].splitlines()[3] == (
        "TEXT: Guangzhoudan 2 kub mato kerak. T.me/<TG_1> ga yozing, "
        "kanal TELEGRAM.ME/<TG_2>"
    )


async def test_labelled_ids_foreign_numbers_and_the_senders_bare_handle_stay_here(
    session, enabled, offline, tmp_path, model_api
):
    """The E2E review's probe (2026-10-29): a handle written without «@»
    after a label — the sender's own, which classify knows — a WeChat id and
    foreign numbers reached the model raw, and the fence, reading the same
    four patterns, saw nothing. A labelled STIR stays: it is company-tier."""
    await chat(session)
    await store(
        session,
        "Guangzhoudan 3 kub mato kerak, kim olib keladi? Telegram: namuna_tekstil, "
        "WeChat: rustam_cn88, Qozogʻiston nomer +7 701 000 00 00, "
        "yoki namuna_tekstil ga yozing. STIR 300000011",
        message_id=51,
        bio="Namuna Tekstil Savdo MChJ direktori, wa: +7 701 000 00 09",
    )
    await link(offline, tmp_path)
    model_api.queue(message(BUYER))
    assert (await classify.classify_batch(now=NOW)).applied == 1
    [body] = model_api.requests
    assert pii_in(body) == []
    seen = visible(body)
    for value in ("namuna_tekstil", "rustam_cn88", "701 000 00 00", "701 000 00 09"):
        assert value not in seen, value
    lines = body["messages"][0]["content"].splitlines()
    assert lines[2] == "SENDER_BIO: Namuna Tekstil Savdo MChJ direktori, wa: <PHONE_1>"
    assert lines[3] == (
        "TEXT: Guangzhoudan 3 kub mato kerak, kim olib keladi? Telegram: <TG_1>, "
        "WeChat: <MSG_1>, Qozogʻiston nomer <PHONE_2>, yoki <TG_1> ga yozing. "
        "STIR 300000011"
    )
    [job] = await model_jobs(session)
    assert job.pseudonyms == {
        "<PHONE_1>": "+7 701 000 00 09",
        "<TG_1>": "namuna_tekstil",
        "<MSG_1>": "rustam_cn88",
        "<PHONE_2>": "+7 701 000 00 00",
    }
