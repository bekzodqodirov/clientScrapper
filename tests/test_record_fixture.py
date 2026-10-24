"""tools/record_fixture (07 §1.1): the page goes through the Fetcher (UA,
robots, rates), the file and one README line with the file's sha256 are
written, cookies and credentials never reach the stored headers, person-tier
pages are scrubbed.

No network: ``tests/fakes.Site`` answers. Files go to ``tmp_path``. The paid
mode runs the real ``costs.ask`` on Postgres.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from clientscrapper.db.models import UsageLog
from clientscrapper.services import costs
from clientscrapper.services.http import (
    USER_AGENT,
    BudgetExhausted,
    RobotsDisallowed,
    UnmeteredCall,
)
from clientscrapper.tools import record_fixture as rf
from tests.fakes import ALLOW_ALL, FakeClock, Site, fetcher_for

EX = "https://www.goldenpages.example.uz"
OLX = "https://www.olx.uz"
PAGE = b"<html><title>Namuna</title><li>Namuna Mebel MChJ</li></html>"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def site(clock: FakeClock, **routes) -> Site:
    return Site(
        clock,
        {f"{EX}/robots.txt": ALLOW_ALL, f"{OLX}/robots.txt": ALLOW_ALL, **routes},
    )


async def test_the_recorder_writes_the_file_its_headers_and_a_readme_line(
    clock, tmp_path
):
    answer = httpx.Response(
        200,
        headers={
            "content-type": "text/html; charset=utf-8",
            "set-cookie": "sid=SECRETCOOKIE; Path=/",
            "etag": '"v7"',
        },
        content=PAGE,
    )
    fake = site(clock, **{f"{EX}/katalog": answer})
    done = await rf.record(
        f"{EX}/katalog?page=1&key=K3Y",
        key="directories",
        name="katalog",
        fetcher=fetcher_for(fake),
        now=clock.now(),
        root=tmp_path,
        headers={"Authorization": "Bearer T0KEN"},
        note="list page, first card",
    )

    folder = tmp_path / "directories"
    assert done.path == folder / "katalog.html"
    assert done.path.read_bytes() == PAGE  # company-tier: verbatim
    sha = hashlib.sha256(PAGE).hexdigest()
    assert done.sha256 == sha
    assert done.scrubbed is False
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert readme.splitlines()[0] == "# Fixtures: directories"
    assert readme.splitlines()[-1] == (
        f"- katalog.html · 2026-10-24 · {EX}/katalog?page=1&key=*** · {sha}"
        f" · 200 · {len(PAGE)} · scrubbed: no · list page, first card"
    )
    stored = json.loads(done.headers_path.read_text(encoding="utf-8"))
    assert stored["status"] == 200
    assert stored["url"] == f"{EX}/katalog?page=1&key=***"
    assert stored["headers"]["etag"] == '"v7"'
    assert "set-cookie" not in stored["headers"]
    everything = done.headers_path.read_text() + readme
    for secret in ("SECRETCOOKIE", "T0KEN", "K3Y"):
        assert secret not in everything
    # Through the Fetcher: robots first, our User-Agent, the extra header sent.
    assert [s.path for s in fake.seen] == ["/robots.txt", "/katalog"]
    assert fake.seen[1].request.headers["user-agent"] == USER_AGENT
    assert fake.seen[1].request.headers["authorization"] == "Bearer T0KEN"


async def test_a_rerecording_replaces_its_own_readme_line(clock, tmp_path):
    fake = site(
        clock,
        **{
            f"{EX}/a": [
                httpx.Response(
                    200, headers={"content-type": "text/html"}, content=b"<p>1</p>"
                ),
                httpx.Response(
                    200, headers={"content-type": "text/html"}, content=b"<p>2</p>"
                ),
            ],
            f"{EX}/b": httpx.Response(
                200, headers={"content-type": "text/html"}, content=PAGE
            ),
        },
    )
    fetcher = fetcher_for(fake)
    common = {"key": "directories", "fetcher": fetcher, "root": tmp_path}
    await rf.record(f"{EX}/a", name="a", now=clock.now(), **common)
    await rf.record(f"{EX}/b", name="b", now=clock.now(), **common)
    second = await rf.record(f"{EX}/a", name="a", now=clock.now(), **common)
    entries = [
        line
        for line in (tmp_path / "directories" / "README.md").read_text().splitlines()
        if line.startswith("- ")
    ]
    assert [line.split(" · ")[0] for line in entries] == ["- b.html", "- a.html"]
    assert second.sha256 == hashlib.sha256(b"<p>2</p>").hexdigest()
    assert second.sha256 in entries[1]


async def test_a_person_tier_page_is_scrubbed(clock, tmp_path):
    body = json.dumps(
        {"data": [{"id": 5, "user": {"name": "Sardor"}, "phone": "+998 90 000 00 47"}]}
    ).encode()
    fake = site(
        clock,
        **{
            f"{OLX}/api/v1/offers/": httpx.Response(
                200, headers={"content-type": "application/json"}, content=body
            )
        },
    )
    done = await rf.record(
        f"{OLX}/api/v1/offers/",
        key="olx_business",
        name="offers",
        fetcher=fetcher_for(fake),
        now=clock.now(),
        root=tmp_path,
        json_mode=True,
    )
    assert done.path.name == "offers.json"
    assert json.loads(done.path.read_bytes()) == {
        "data": [{"id": 5, "user": {"name": "Seller 01"}, "phone": "+998 90 000 00 01"}]
    }
    assert done.scrubbed is True
    assert "scrubbed: yes" in done.readme_line
    assert done.sha256 == hashlib.sha256(done.path.read_bytes()).hexdigest()
    assert fake.seen[-1].request.headers["accept"] == "application/json"


async def test_a_scrubbed_page_s_url_and_headers_use_the_body_mapping(clock, tmp_path):
    tg = "https://t.me"
    person = "namuna_person_90"
    body = (
        f'<html><title>Dilshod Rahimov</title><a href="{tg}/{person}">@{person}</a>'
        " +998 90 000 00 47</html>"
    ).encode()
    answer = httpx.Response(
        200,
        headers={
            "content-type": "text/html",
            "link": f"<{tg}/{person}>; rel=canonical",
        },
        content=body,
    )
    fake = Site(clock, {f"{tg}/robots.txt": ALLOW_ALL, f"{tg}/{person}": answer})
    done = await rf.record(
        f"{tg}/{person}",
        key="telegram_monitor",
        name="profile",
        fetcher=fetcher_for(fake),
        now=clock.now(),
        root=tmp_path,
    )
    assert done.path.read_text(encoding="utf-8") == (
        f'<html><title>Seller 01</title><a href="{tg}/namuna_01">@namuna_01</a>'
        " +998 90 000 00 01</html>"
    )
    stored = json.loads(done.headers_path.read_text(encoding="utf-8"))
    assert (stored["url"], stored["final_url"]) == (f"{tg}/namuna_01", f"{tg}/namuna_01")
    assert stored["headers"] == {
        "content-type": "text/html",
        "link": f"<{tg}/namuna_01>; rel=canonical",
    }  # content-length described the unscrubbed bytes
    readme = (tmp_path / "telegram_monitor" / "README.md").read_text(encoding="utf-8")
    assert f"profile.html · 2026-10-24 · {tg}/namuna_01 · " in readme
    everything = done.path.read_text() + done.headers_path.read_text() + readme
    assert person not in everything and "Rahimov" not in everything


async def test_scrub_off_keeps_the_bytes(clock, tmp_path):
    body = b'{"user": {"name": "Sardor"}}'
    fake = site(
        clock,
        **{
            f"{OLX}/x": httpx.Response(
                200, headers={"content-type": "application/json"}, content=body
            )
        },
    )
    done = await rf.record(
        f"{OLX}/x",
        key="olx_business",
        name="x",
        fetcher=fetcher_for(fake),
        now=clock.now(),
        root=tmp_path,
        scrub="off",
    )
    assert done.path.read_bytes() == body and done.scrubbed is False


async def test_robots_applies_and_nothing_is_written(clock, tmp_path):
    fake = Site(
        clock,
        {
            f"{EX}/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /\n"),
            f"{EX}/a": httpx.Response(200, content=PAGE),
        },
    )
    with pytest.raises(RobotsDisallowed):
        await rf.record(
            f"{EX}/a",
            key="directories",
            name="a",
            fetcher=fetcher_for(fake),
            now=clock.now(),
            root=tmp_path,
        )
    assert list(tmp_path.iterdir()) == []
    assert "/a" not in fake.paths()


async def test_a_metered_host_is_refused(clock, tmp_path):
    brave = "https://api.search.brave.com/res/v1/web/search"
    fake = Site(clock, {brave: httpx.Response(200, json={})})
    with pytest.raises(UnmeteredCall):
        await rf.record(
            brave,
            key="search_brave",
            name="brave",
            fetcher=fetcher_for(fake),
            now=clock.now(),
            root=tmp_path,
            json_mode=True,
        )
    assert fake.seen == [] and list(tmp_path.iterdir()) == []


async def test_a_documented_api_is_recorded_as_its_collector_reads_it(clock, tmp_path):
    # hh's collector skips robots.txt at api.hh.ru (03 §1.1); so does the
    # recorder, or a robots.txt written for browsers keeps T-23's fixture away.
    hh = "https://api.hh.ru"
    answer = httpx.Response(
        200, headers={"content-type": "application/json"}, content=b'{"items": []}'
    )
    fake = Site(
        clock,
        {
            f"{hh}/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /\n"),
            f"{hh}/vacancies": answer,
        },
    )
    done = await rf.record(
        f"{hh}/vacancies?text=ВЭД&area=97",
        key="hh_vacancies",
        name="search",
        fetcher=fetcher_for(fake),
        now=clock.now(),
        root=tmp_path,
        json_mode=True,
    )
    assert done.path.read_bytes() == b'{"items": []}'
    assert fake.paths() == ["/vacancies"]


BRAVE = "https://api.search.brave.com/res/v1/web/search"


@pytest.fixture
def no_refusals():
    costs.clear_refusals()
    yield
    costs.clear_refusals()


async def test_a_paid_recording_goes_through_costs_ask(
    session, clock, tmp_path, no_refusals
):
    fake = Site(clock, {BRAVE: httpx.Response(200, json={"web": {"results": []}})})
    fetcher = fetcher_for(fake, cost_gate=rf.ask_gate(session))
    done = await rf.record(
        f"{BRAVE}?q=optom&country=UZ",
        key="serp_layer",
        name="brave",
        fetcher=fetcher,
        now=clock.now(),
        root=tmp_path,
        json_mode=True,
        paid=rf.paid_call("serp_brave", Decimal("0.005")),
    )
    assert done.path.name == "brave.json" and len(fake.seen) == 1
    [row] = (await session.scalars(sa.select(UsageLog))).all()
    assert (row.operation, row.source_key, row.cost_usd, row.created_at) == (
        "serp_brave",
        "serp_layer",
        Decimal("0.005000"),
        clock.now(),
    )


async def test_a_paid_recording_at_the_cap_sends_and_writes_nothing(
    session, clock, tmp_path, no_refusals
):
    session.add(
        UsageLog(
            provider="other",
            operation="other",
            cost_usd=Decimal("10"),
            created_at=clock.now(),
        )
    )
    await session.commit()
    fake = Site(clock, {BRAVE: httpx.Response(200, json={})})
    with pytest.raises(BudgetExhausted) as refused:
        await rf.record(
            BRAVE,
            key="serp_layer",
            name="brave",
            fetcher=fetcher_for(fake, cost_gate=rf.ask_gate(session)),
            now=clock.now(),
            root=tmp_path,
            paid=rf.paid_call("serp_brave", Decimal("0.005")),
        )
    assert refused.value.detail == "cost_cap_daily"
    assert fake.seen == [] and list(tmp_path.iterdir()) == []


def test_paid_takes_a_callable_operation_and_an_estimate():
    with pytest.raises(ValueError):
        rf.paid_call("model_reserve", Decimal("0.01"))  # a pool, not a call
    with pytest.raises(ValueError):
        rf.paid_call("serp_brave", Decimal("NaN"))
    args = rf.parse_args(
        ["https://x.example.uz", "--key", "serp_layer", "--name", "b",
         "--paid", "serp_brave", "--est", "0.005"]
    )  # fmt: skip
    assert (args.paid, args.est) == ("serp_brave", Decimal("0.005"))
    with pytest.raises(SystemExit):
        rf.parse_args(
            ["https://x.example.uz", "--key", "k1x", "--name", "n", "--paid", "x"]
        )


@pytest.mark.parametrize(("key", "name"), [("Directories", "a"), ("directories", "../a")])
async def test_bad_keys_and_names_are_refused(clock, tmp_path, key, name):
    with pytest.raises(ValueError):
        await rf.record(
            f"{EX}/a",
            key=key,
            name=name,
            fetcher=fetcher_for(site(clock)),
            now=clock.now(),
            root=tmp_path,
        )


def test_stored_headers_strip_cookies_and_credentials():
    assert rf.stored_headers(
        {
            "Set-Cookie": "a=1",
            "Authorization": "Bearer x",
            "X-Subscription-Token": "x",
            "Content-Type": "text/html",
            "Location": "https://a.example.uz/?token=abc",
        }
    ) == {"content-type": "text/html", "location": "https://a.example.uz/?token=***"}


def test_the_cli_parses_its_arguments():
    args = rf.parse_args(
        [
            "https://api.hh.ru/areas",
            "--key", "hh_vacancies",
            "--name", "areas",
            "--json",
            "--header", "Authorization=Bearer x",
            "--header", "Accept-Language=ru",
            "--scrub", "off",
            "--note", "UZ id",
        ]
    )  # fmt: skip
    assert (args.key, args.name, args.json_mode, args.scrub, args.note) == (
        "hh_vacancies",
        "areas",
        True,
        "off",
        "UZ id",
    )
    assert args.header == [("Authorization", "Bearer x"), ("Accept-Language", "ru")]
    with pytest.raises(SystemExit):
        rf.parse_args(
            ["https://a.example.uz", "--key", "k", "--name", "n", "--header", "x"]
        )
