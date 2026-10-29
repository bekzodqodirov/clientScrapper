"""The Telegram path across the lanes, on Postgres: the reader's row →
``telegram_monitor`` (T-14) → ``classify`` (T-15, the SYNTHETIC
FakeAnthropic) → the resolver (T-17) → signals (T-18, the rules' and the
model's on one evidence) → the gates (T-19) → ``score_job`` → the
candidate's state (04 §0, §4.3, §5.3, 02 §2.7).

The classifier answers in seconds and the resolver every two minutes, so
the result meets its company on whichever side comes second: the resolver's
hook reads ``classification_for`` (the usual order), the classifier's hook
writes at once when the company is there first. Both orders end the same.

All data is synthetic (07 §1.0); every model answer is SYNTHETIC
(tests/fixtures/model/README.md).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db import models as m
from clientscrapper.services import candidates, classify, companies, crm_push, health
from tests import classify_fixtures as cf
from tests.classify_fixtures import ASK, NOW, chat, interaction, link, store
from tests.fakes import message, model_fixture

enabled = cf.enabled
offline = cf.offline

BUYER = model_fixture("classify_buyer_stated.json")
WORDS = model_fixture("classify_words_stated.json")
CONTAINER = model_fixture("classify_container_no_number.json")
TONNES = model_fixture("classify_weight_tonnes.json")


async def _signals(session) -> dict[str, m.Signal]:
    rows = await session.scalars(
        sa.select(m.Signal)
        .order_by(m.Signal.key)
        .execution_options(populate_existing=True)
    )
    return {s.key: s for s in rows}


async def _candidate(session) -> m.LeadCandidate:
    [row] = list(
        await session.scalars(
            sa.select(m.LeadCandidate).execution_options(populate_existing=True)
        )
    )
    return row


async def _classify_job(session) -> m.ModelJob:
    [job] = list(
        await session.scalars(
            sa.select(m.ModelJob)
            .where(m.ModelJob.kind == "classify")
            .execution_options(populate_existing=True)
        )
    )
    return job


async def _message(session, offline, tmp_path, text: str = ASK, **fields) -> int:
    await chat(session)
    row_id = await store(session, text, message_id=101, **fields)
    await link(offline, tmp_path)
    return row_id


async def test_the_worked_example_lands_as_signals_and_a_scored_candidate(
    session, enabled, offline, tmp_path, model_api
):
    """04 §9.1 end to end, in the usual order: classify first (no company
    yet), the resolver second — its hook finds the result, writes the
    model's signals beside the rules' and names the company on the job."""
    row_id = await _message(session, offline, tmp_path)
    model_api.queue(message(BUYER))
    assert (await classify.classify_batch(now=NOW)).applied == 1
    job = await _classify_job(session)
    assert job.company_id is None  # asked before the company existed
    assert (await companies.resolve_batch(now=NOW)).resolved == 1

    row = await interaction(session, row_id)
    obs = await session.get(m.Observation, row.observation_id, populate_existing=True)
    assert obs is not None and obs.company_id is not None
    job = await _classify_job(session)
    assert job.company_id == obs.company_id  # gates read it by the lineage
    found = await _signals(session)
    assert sorted(found) == ["cn_origin", "cn_sector_prior", "vol_stated_1_3"]
    stated = found["vol_stated_1_3"]
    # One row per (company, key, evidence): the model's verified quote took
    # the rule's «2 kub».
    assert (stated.value, stated.quote, stated.model, stated.model_job_id) == (
        Decimal("2"),
        "2 kub mato",
        settings.extract_model,
        job.id,
    )
    assert stated.evidence_id == row.evidence_id
    # cn_origin is the rule's (CHINA_RE's fragment), never the model's: an
    # ImportSignal carries no origin quote (04 §4.7).
    assert (found["cn_origin"].quote, found["cn_origin"].model) == ("Guangzhoudan", None)
    assert found["cn_sector_prior"].model == settings.extract_model

    report = await candidates.score_batch(now=NOW)
    assert (report.scored, report.failed) == (1, 0)
    cand = await _candidate(session)
    # volume 20 + origin 20 (cap; the sector prior adds nothing) + recency 10
    # + contact 4 (dm channel 2, the bio's «direktori» a decision maker 2):
    # 54, warm, no phone → enriching, its first attempt in an hour.
    assert (cand.score, cand.tier, cand.state) == (54, "warm", "enriching")
    assert cand.primary_source_key == "telegram_monitor"
    groups = cand.score_breakdown["groups"]
    assert {k: g["used"] for k, g in groups.items()} == {
        "volume": 20,
        "origin": 20,
        "solidity": 0,
        "recency": 10,
        "contact": 4,
        "corroboration": 0,
    }
    assert cand.score_breakdown["gates"]["is_b2b"] == "pass:legal_form"


async def test_the_worked_example_9_1_is_54_then_72_end_to_end(
    session, enabled, offline, tmp_path, model_api
):
    """04 §9.1 / 07 §1.3's golden on the contact the resolver writes (not a
    hand-built view): the poster's display name, the bio's «direktori» as
    written and the decision-maker flag it implies — 54 warm before
    enrichment; then E4's registry hit and E5's directory phone — 72 hot,
    on to the owner's card (settled 2026-10-29: the spec printed 52 → 70)."""
    await _message(session, offline, tmp_path)
    model_api.queue(message(BUYER))
    assert (await classify.classify_batch(now=NOW)).applied == 1
    assert (await companies.resolve_batch(now=NOW)).resolved == 1
    [person] = list(
        await session.scalars(
            sa.select(m.Contact).execution_options(populate_existing=True)
        )
    )
    assert (person.tier, person.full_name, person.role, person.is_decision_maker) == (
        "person",
        "Rustam Karimov",
        "direktori",
        True,
    )
    assert (await candidates.score_batch(now=NOW)).scored == 1
    cand = await _candidate(session)
    assert (cand.score, cand.tier, cand.state) == (54, "warm", "enriching")
    contact = cand.score_breakdown["groups"]["contact"]["signals"]
    assert contact == [
        {"key": "derived_contact_decision_maker", "points": 2},
        {"key": "derived_contact_dm_channel", "points": 2},
    ]

    # E4: the registry by name — legal, active since 2019; E5: a directory
    # card's company-tier mobile. T-24 writes these; here by hand.
    later = NOW + timedelta(hours=1)
    registry = m.Evidence(
        source_key="registry_lookup",
        url="https://registry.example.uz/300000011",
        fetched_at=later,
        sha256=f"{9101:064d}",
        bytes=0,
        excerpt="Namuna Tekstil Savdo MChJ, STIR 300000011, faol, 2019",
        expires_at=later + timedelta(days=180),
    )
    session.add(registry)
    await session.flush()
    for key, value in (("ent_legal_confirmed", None), ("ent_age_2y_active", Decimal(7))):
        session.add(
            m.Signal(
                company_id=cand.company_id,
                evidence_id=registry.id,
                key=key,
                value=value,
                observed_at=later,
                source_key="registry_lookup",
            )
        )
    session.add(
        m.Contact(
            company_id=cand.company_id,
            tier="company",
            phone="+998900000001",
            phone_kind="mobile",
        )
    )
    await session.flush()
    from clientscrapper.services import signals

    await signals.mark_stale(session, cand.company_id)
    await session.commit()
    assert (await candidates.score_batch(now=later)).scored == 1
    cand = await _candidate(session)
    assert (cand.score, cand.tier, cand.state) == (72, "hot", "review")
    groups = cand.score_breakdown["groups"]
    assert {k: g["used"] for k, g in groups.items()} == {
        "volume": 20,
        "origin": 20,
        "solidity": 12,
        "recency": 10,
        "contact": 10,
        "corroboration": 0,
    }
    # The model's goods are the company's (02 §2.2 «from ImportSignal»), so
    # the ERP note names them (05 §B.1 «NIMA OLIB KELADI», ``imports``).
    company = await session.get(m.Company, cand.company_id, populate_existing=True)
    assert company is not None and company.goods == ["mato"]
    view = await candidates.load_view(session, cand.id, now=later)
    body = crm_push.build_payload(view)
    assert body.imports == "mato"
    assert body.note.split("\n")[2].startswith("NIMA OLIB KELADI: mato · Xitoy: ha")


async def test_the_company_first_order_ends_the_same(
    session, enabled, offline, tmp_path, model_api
):
    """The resolver first (the classifier was held up): the classifier's own
    hook writes the model's signals at once, on the company it finds."""
    await _message(session, offline, tmp_path)
    assert (await companies.resolve_batch(now=NOW)).resolved == 1
    before = await _signals(session)
    assert before["vol_stated_1_3"].model is None  # the rule's «2 kub»
    model_api.queue(message(BUYER))
    assert (await classify.classify_batch(now=NOW)).applied == 1

    found = await _signals(session)
    assert sorted(found) == ["cn_origin", "cn_sector_prior", "vol_stated_1_3"]
    assert (found["vol_stated_1_3"].quote, found["vol_stated_1_3"].model) == (
        "2 kub mato",
        settings.extract_model,
    )
    job = await _classify_job(session)
    assert job.company_id == found["vol_stated_1_3"].company_id
    # The classifier's own hook carries the goods too (02 §2.2).
    company = await session.get(m.Company, job.company_id, populate_existing=True)
    assert company is not None and company.goods == ["mato"]
    await candidates.score_batch(now=NOW)
    cand = await _candidate(session)
    assert (cand.score, cand.tier, cand.state) == (54, "warm", "enriching")


async def test_a_buyer_who_asks_for_cargo_ends_as_a_scored_candidate(
    session, enabled, offline, tmp_path, model_api
):
    """04 §6: «kim kargo qiladi?» is a buyer. The tout filter reads a
    message for competitor words only when it offers the service
    (keywords.cargo_offer), so the ask reaches the classifier, the resolver,
    the gates and the scorer like any other — the customer GSR looks for."""
    text = "Guangzhoudan 2 kub mato bor, kim kargo qiladi?"
    row_id = await _message(session, offline, tmp_path, text)
    row = await interaction(session, row_id)
    assert row.processed_at is None  # standing: the classifier's queue
    model_api.queue(message(BUYER))
    assert (await classify.classify_batch(now=NOW)).applied == 1
    report = await companies.resolve_batch(now=NOW)
    assert (report.resolved, report.rejected) == (1, 0)
    found = await _signals(session)
    assert found["vol_stated_1_3"].quote == "2 kub mato"
    assert (await candidates.score_batch(now=NOW)).scored == 1
    cand = await _candidate(session)
    assert (cand.score, cand.tier, cand.state) == (54, "warm", "enriching")
    assert cand.score_breakdown["gates"]["not_competitor"] == "pass"


async def test_a_display_name_with_a_second_number_stores_the_name_alone(
    session, enabled, offline, tmp_path
):
    """06 §5.1 / 05 §B.1: the poster typed a second number into the display
    name; the contact keeps «Rustam» and the lead phone the message gives."""
    text = "Guangzhoudan 2 kub mato kerak, kim olib keladi? Tel +998 90 000 00 01"
    await _message(session, offline, tmp_path, text, name="Rustam 90 000 00 07")
    assert (await companies.resolve_batch(now=NOW)).resolved == 1
    people = list(
        await session.scalars(
            sa.select(m.Contact)
            .where(m.Contact.tier == "person")
            .execution_options(populate_existing=True)
        )
    )
    assert [(c.full_name, c.role) for c in people] == [("Rustam", "direktori")]
    assert all("000 00 07" not in (c.phone or "") for c in people)


async def test_a_volume_only_the_model_read_scores_the_found_candidate(
    session, enabled, offline, tmp_path, model_api
):
    """«ikki kub» is no VOLUME_RE number: the rules write only the origin, a
    ``found`` candidate waits for a volume signal (02 §2.9), and only the
    model's verified «ikki kub mato» lets it be scored."""
    text = "Guangzhoudan ikki kub mato kerak, kim olib keladi?"
    await _message(session, offline, tmp_path, text)
    model_api.queue(message(WORDS))
    assert (await classify.classify_batch(now=NOW)).applied == 1
    await companies.resolve_batch(now=NOW)

    found = await _signals(session)
    stated = found["vol_stated_1_3"]
    assert (stated.value, stated.quote, stated.model) == (
        Decimal("2"),
        "ikki kub mato",
        settings.extract_model,
    )
    report = await candidates.score_batch(now=NOW)
    assert report.scored == 1
    cand = await _candidate(session)
    assert (cand.score, cand.tier, cand.state) == (54, "warm", "enriching")


async def test_a_quoted_container_with_no_number_is_vol_container(
    session, enabled, offline, tmp_path, model_api
):
    """04 §5.3 rule 3: basis ``container`` with a verified quote and
    ``volume_m3_est`` null — the unit is what the text says. With a phone in
    the message the lead is hot and goes to the owner's card; gate 1 passes
    on the model's «b2b buyer» alone, which it reads from the job the
    resolver named the company on."""
    text = (
        "Yiwudan 40 futlik konteyner kafel kerak, kim olib keladi? "
        "Tel +998 90 000 00 01"
    )
    await _message(session, offline, tmp_path, text, bio=None)
    model_api.queue(message(CONTAINER))
    assert (await classify.classify_batch(now=NOW)).applied == 1
    await companies.resolve_batch(now=NOW)

    found = await _signals(session)
    box = found["vol_container"]
    job = await _classify_job(session)
    assert job.result["volume_m3_est"] is None
    assert (box.value, box.quote, box.model, box.model_job_id) == (
        None,
        "40 futlik konteyner",
        settings.extract_model,
        job.id,
    )
    await candidates.score_batch(now=NOW)
    cand = await _candidate(session)
    # volume 40 + origin 20 + recency 10 + contact 8 (mobile 6, dm 2) = 78.
    assert (cand.score, cand.tier, cand.state) == (78, "hot", "review")
    assert cand.score_breakdown["gates"]["is_b2b"] == "pass:intent_b2b"


async def test_a_weight_quote_takes_rule_one_on_the_quoted_tonnes(
    session, enabled, offline, tmp_path, model_api
):
    """Basis ``weight``: no number from the model, the quoted «5 tonna» read
    by rule 1 (1 t ≈ 3 m³) — 15 m³, ``vol_stated_3plus``."""
    text = "Guangzhoudan 5 tonna mato kerak, kim olib keladi?"
    await _message(session, offline, tmp_path, text)
    model_api.queue(message(TONNES))
    assert (await classify.classify_batch(now=NOW)).applied == 1
    await companies.resolve_batch(now=NOW)

    found = await _signals(session)
    big = found["vol_stated_3plus"]
    assert (big.value, big.quote, big.model) == (
        Decimal("15"),
        "5 tonna mato",
        settings.extract_model,
    )
    # «tonna» is a CONTAINER_RE word, so the collector typed the message
    # ``container_truck``; a tonne is rule 1's, never a container (04 §5.3).
    assert "vol_stated_1_3" not in found and "vol_container" not in found
    await candidates.score_batch(now=NOW)
    cand = await _candidate(session)
    # volume 30 + origin 20 + recency 10 + contact 4 = 64, no phone → warm.
    assert (cand.score, cand.tier, cand.state) == (64, "warm", "enriching")


PRICE_LIST = "Xitoydan yuk: 1 kub 250$, 100 kg 300$, 20 fut 2500$"


async def _price_lists(session, offline, tmp_path, days: tuple[int, ...]) -> None:
    await chat(session)
    for n, day in enumerate(days):
        await store(
            session,
            PRICE_LIST,
            message_id=300 + n,
            at=cf.T0 + timedelta(days=day),
            username="namuna_narx",
            name="Namuna",
            bio=None,
        )
    await link(offline, tmp_path, now=cf.T0 + timedelta(days=days[-1], hours=1))


async def test_a_live_touts_first_price_lists_do_not_stay_a_lead(
    session, enabled, offline, tmp_path
):
    """03 §2.3 counts price lists up to each message, so a tout's first two
    lists stand as asks and become a lead before the third arrives (T-14
    S2). Gate 2 reads the collector's later verdict: a person whose message
    within 7 days after the post was a tout's is a competitor."""
    await _price_lists(session, offline, tmp_path, (0, 2, 5))
    report = await companies.resolve_batch(now=cf.T0 + timedelta(days=5, hours=2))
    assert (report.resolved, report.rejected) == (2, 1)  # the third is the tout's
    await candidates.score_batch(now=cf.T0 + timedelta(days=5, hours=2))
    cand = await _candidate(session)
    assert (cand.state, cand.exclude_reason) == ("excluded", "competitor")
    assert cand.score_breakdown["gates"]["not_competitor"] == "fail:tout"
    company = await session.get(m.Company, cand.company_id, populate_existing=True)
    assert company is not None and company.is_competitor


async def test_price_lists_a_week_apart_stay_a_lead(session, enabled, offline, tmp_path):
    """The same lists eight days apart: no 7-day window holds three, the
    collector judges none a tout's, and the gate has nothing to read."""
    await _price_lists(session, offline, tmp_path, (0, 8, 16))
    report = await companies.resolve_batch(now=cf.T0 + timedelta(days=16, hours=2))
    assert (report.resolved, report.rejected) == (3, 0)
    await candidates.score_batch(now=cf.T0 + timedelta(days=16, hours=2))
    cand = await _candidate(session)
    assert cand.state == "enriching"
    assert cand.score_breakdown["gates"]["not_competitor"] == "pass"


async def test_the_classify_queue_is_the_work_anthropic_silence_counts(
    session, enabled, offline, tmp_path, model_api
):
    """06 §4.2 anthropic_silence fires «while jobs wait» and says «tasnif
    navbatda (n ta)»: the waiting work of the Telegram path is classify's
    queue (an instant job is never left pending), while a batch job waits
    for 23:30 by design and is batch_stuck's to report (T-08 ↔ T-15/T-16)."""
    await _message(session, offline, tmp_path)
    session.add(
        m.ModelJob(kind="company_verdict", lane="batch", model="m", custom_id="cs-b-1")
    )
    await session.commit()
    assert (await health.anthropic_facts(session)).pending_jobs == 1
    model_api.queue(message(BUYER))
    assert (await classify.classify_batch(now=NOW)).applied == 1
    assert (await health.anthropic_facts(session)).pending_jobs == 0


TOUT_BIO = "Xitoydan yuk tashish xizmati, kargo"


async def _ask_then_tout_bio(session, offline, tmp_path, *, after: timedelta) -> None:
    """A poster's ask, linked; then, ``after`` it, a message the collector
    judges a tout's by the bio the reader has learnt meanwhile (T-14 P2b:
    the bio arrives after a first-time poster's first message)."""
    await chat(session)
    await store(session, ASK, message_id=401, bio=None)
    await link(offline, tmp_path, now=cf.T0 + timedelta(minutes=5))
    await companies.resolve_batch(now=cf.T0 + timedelta(minutes=6))
    at = cf.T0 + after
    await store(
        session, "Toshkentga 3 kub yuk kerak", message_id=402, at=at, bio=TOUT_BIO
    )
    await link(offline, tmp_path, now=at + timedelta(minutes=5))
    report = await companies.resolve_batch(now=at + timedelta(minutes=6))
    assert (report.resolved, report.rejected) == (0, 1)


async def test_a_poster_the_collector_later_reads_as_a_tout_is_no_lead(
    session, enabled, offline, tmp_path
):
    """The first message was judged before the bio was known and stood; the
    second, a day later, is a tout's by the bio — gate 2 reads that verdict
    for the first message's company."""
    await _ask_then_tout_bio(session, offline, tmp_path, after=timedelta(days=1))
    await candidates.score_batch(now=cf.T0 + timedelta(days=1, hours=1))
    cand = await _candidate(session)
    assert (cand.state, cand.exclude_reason) == ("excluded", "competitor")


async def test_a_tout_verdict_beyond_the_window_does_not_reach_back(
    session, enabled, offline, tmp_path
):
    """A verdict ten days after the post is outside every 7-day window that
    holds it (03 §2.3): the lead stands."""
    await _ask_then_tout_bio(session, offline, tmp_path, after=timedelta(days=10))
    await candidates.score_batch(now=cf.T0 + timedelta(days=10, hours=1))
    cand = await _candidate(session)
    assert cand.state == "enriching"
    assert cand.score_breakdown["gates"]["not_competitor"] == "pass"
