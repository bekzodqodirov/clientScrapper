"""services/http.Fetcher (03 §1.1, §1.5; 07 T-05): pacing, robots.txt and its
opt-out allow-list, the conditional GET, Retry-After, the breaker, budgets,
the User-Agent, log scrubbing, CAPTCHA pages, paid hosts.

No network: every request goes to ``tests/fakes.Site`` (an
``httpx.MockTransport``) and every wait to ``FakeClock``.
"""

from __future__ import annotations

import ast
import hashlib
import logging
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from clientscrapper.services import http, retry
from clientscrapper.services.http import (
    BREAKER_PAUSE,
    USER_AGENT,
    BudgetExhausted,
    FetchResult,
    HostBlocked,
    HostPaused,
    HostState,
    PaidCall,
    RobotsDisallowed,
    RobotsOptOutRefused,
    StoredCopy,
    UnmeteredCall,
)
from tests.fakes import ALLOW_ALL, T0, FakeClock, Site, budget, fetcher_for

PACKAGE = Path(__file__).resolve().parents[2] / "clientscrapper"
EX = "https://www.namuna.example.uz"
HTML = {"content-type": "text/html; charset=utf-8"}


def page(text: str = "<html><body>Namuna</body></html>", status: int = 200, **headers):
    return httpx.Response(status, headers={**HTML, **headers}, text=text)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def site_with(clock: FakeClock, routes: dict) -> Site:
    return Site(clock, {f"{EX}/robots.txt": ALLOW_ALL, **routes})


class Hooks:
    def __init__(self, stored: dict[str, StoredCopy] | None = None) -> None:
        self.stored = stored or {}
        self.results: list[FetchResult] = []

    async def stored_copy(self, url: str) -> StoredCopy | None:
        return self.stored.get(url)

    def fetched(self, result: FetchResult) -> None:
        self.results.append(result)


# --- User-Agent ---------------------------------------------------------------


async def test_every_request_carries_our_user_agent(clock):
    site = site_with(clock, {f"{EX}/a": page(), f"{EX}/b": page()})
    fetcher = fetcher_for(site)
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    await fetcher.get(
        f"{EX}/b", source="directories", budget=budget(), headers={"Accept": "text/html"}
    )
    assert [s.path for s in site.seen] == ["/robots.txt", "/a", "/b"]
    assert {s.request.headers["user-agent"] for s in site.seen} == {USER_AGENT}
    assert site.seen[2].request.headers["accept"] == "text/html"
    assert USER_AGENT.startswith("clientScrapperBot/")
    assert USER_AGENT.endswith(f"python-httpx/{httpx.__version__}")


def test_user_agent_names_the_contact_address():
    agent = http.user_agent(http.contact_email_from("clientScrapper/1.0 (ops@namuna.uz)"))
    assert agent == (
        f"clientScrapperBot/{http.VERSION} (+mailto:ops@namuna.uz) "
        f"python-httpx/{httpx.__version__}"
    )


async def test_a_collector_cannot_replace_the_user_agent(clock):
    site = site_with(clock, {f"{EX}/a": page()})
    fetcher = fetcher_for(site)
    with pytest.raises(ValueError, match="User-Agent"):
        await fetcher.get(
            f"{EX}/a", source="directories", budget=budget(), headers={"user-agent": "x"}
        )
    assert site.seen == []


# --- pacing -------------------------------------------------------------------


async def test_two_requests_to_one_host_are_one_interval_apart(clock):
    site = site_with(clock, {f"{EX}/p1": page(), f"{EX}/p2": page(), f"{EX}/p3": page()})
    fetcher = fetcher_for(site, host_rps={"www.namuna.example.uz": 0.5})
    for name in ("p1", "p2", "p3"):
        await fetcher.get(f"{EX}/{name}", source="directories", budget=budget())
    times = [s.at for s in site.seen]
    assert times == [0.0, 2.0, 4.0, 6.0]  # robots.txt, then a page every 2 s


async def test_the_default_rate_is_point_three_per_second(clock):
    site = site_with(clock, {f"{EX}/p1": page()})
    fetcher = fetcher_for(site, rps_default=0.3)
    await fetcher.get(f"{EX}/p1", source="directories", budget=budget())
    assert site.seen[1].at == pytest.approx(1 / 0.3)
    assert http.HOST_RPS["www.olx.uz"] == 0.33
    assert http.HOST_RPS["api.hh.ru"] == 1.0


async def test_two_hosts_interleave(clock):
    other = "https://catalog.namuna.example.uz"
    site = site_with(
        clock,
        {
            f"{EX}/a1": page(),
            f"{EX}/a2": page(),
            f"{other}/robots.txt": ALLOW_ALL,
            f"{other}/b1": page(),
        },
    )
    fetcher = fetcher_for(site, rps_default=0.25)
    await fetcher.get(f"{EX}/a1", source="directories", budget=budget())  # 0, 4
    await fetcher.get(f"{other}/b1", source="directories", budget=budget())
    await fetcher.get(f"{EX}/a2", source="directories", budget=budget())
    seen = [(s.request.url.host, s.path, s.at) for s in site.seen]
    # The second host does not wait for the first host's bucket: its robots and
    # page go at its own pace while the first host's next slot is still ahead.
    assert seen == [
        ("www.namuna.example.uz", "/robots.txt", 0.0),
        ("www.namuna.example.uz", "/a1", 4.0),
        ("catalog.namuna.example.uz", "/robots.txt", 4.0),
        ("catalog.namuna.example.uz", "/b1", 8.0),
        ("www.namuna.example.uz", "/a2", 8.0),
    ]


async def test_a_slower_crawl_delay_wins(clock):
    robots = httpx.Response(200, text="User-agent: *\nCrawl-delay: 10\nDisallow:\n")
    site = Site(
        clock,
        {f"{EX}/robots.txt": robots, f"{EX}/a": page(), f"{EX}/b": page()},
    )
    fetcher = fetcher_for(site, rps_default=1.0)
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    await fetcher.get(f"{EX}/b", source="directories", budget=budget())
    assert [s.at for s in site.seen] == [0.0, 10.0, 20.0]


# --- robots.txt -----------------------------------------------------------------


async def test_a_disallowed_url_is_never_requested(clock):
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    site = Site(clock, {f"{EX}/robots.txt": robots, f"{EX}/private/x": page()})
    fetcher = fetcher_for(site)
    b = budget(pages=5)
    with pytest.raises(RobotsDisallowed):
        await fetcher.get(f"{EX}/private/x", source="directories", budget=b)
    assert site.paths() == ["/robots.txt"]
    assert fetcher.stats.skipped_robots == 1
    assert b.pages == 5  # a refused URL costs no page


async def test_our_agent_group_is_read_before_the_star_group(clock):
    robots = httpx.Response(
        200,
        text=(
            "User-agent: clientScrapperBot\nDisallow: /catalog\n\n"
            "User-agent: *\nDisallow:\n"
        ),
    )
    site = Site(clock, {f"{EX}/robots.txt": robots, f"{EX}/about": page()})
    fetcher = fetcher_for(site)
    await fetcher.get(f"{EX}/about", source="directories", budget=budget())
    with pytest.raises(RobotsDisallowed):
        await fetcher.get(f"{EX}/catalog/1", source="directories", budget=budget())
    assert site.paths() == ["/robots.txt", "/about"]


async def test_the_fetcher_decides_robots_by_rfc_9309(clock):
    # robotparser let the first line win (Allow: / ahead of the Disallows),
    # knew no «*» and «$», and lost the first group behind a BOM.
    robots = httpx.Response(
        200,
        content=(
            "\ufeffUser-agent: *\nAllow: /\nDisallow: /private/\n"
            "Disallow: /*?*page=\nDisallow: /*/contacts$\n"
        ).encode(),
    )
    site = Site(
        clock,
        {
            f"{EX}/robots.txt": robots,
            f"{EX}/private/x": page(),
            f"{EX}/list": page(),
            f"{EX}/firm/contacts": page(),
            f"{EX}/firm/1": page(),
        },
    )
    fetcher = fetcher_for(site)
    for path in ("/private/x", "/list?sort=new&page=2", "/firm/contacts"):
        with pytest.raises(RobotsDisallowed):
            await fetcher.get(EX + path, source="directories", budget=budget())
    await fetcher.get(f"{EX}/firm/1", source="directories", budget=budget())
    assert site.paths() == ["/robots.txt", "/firm/1"]
    assert fetcher.stats.skipped_robots == 3


async def test_robots_is_read_from_the_urls_own_port(clock):
    searx = "http://searxng:8080"
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /stats\n")
    site = Site(clock, {f"{searx}/robots.txt": robots, f"{searx}/search": page("{}")})
    fetcher = fetcher_for(site)
    await fetcher.get(f"{searx}/search?q=optom", source="search_searxng", budget=budget())
    with pytest.raises(RobotsDisallowed):
        await fetcher.get(f"{searx}/stats", source="search_searxng", budget=budget())
    assert [s.url for s in site.seen] == [
        "http://searxng:8080/robots.txt",
        "http://searxng:8080/search?q=optom",
    ]
    assert http.authority_of("https://a.example.uz:443/x") == "a.example.uz"
    assert http.authority_of("https://A.example.uz:8443/x") == "a.example.uz:8443"


async def test_a_robots_429_is_asked_again_within_the_hour(clock):
    robots = [httpx.Response(429), ALLOW_ALL]
    site = Site(clock, {f"{EX}/robots.txt": robots, f"{EX}/a": page()})
    fetcher = fetcher_for(site)
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    clock.advance(61 * 60)
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert site.paths().count("/robots.txt") == 2


async def test_robots_is_fetched_once_per_24_hours(clock):
    site = site_with(clock, {f"{EX}/a": page()})
    fetcher = fetcher_for(site)
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert site.paths().count("/robots.txt") == 1
    clock.advance(timedelta(hours=24, seconds=1).total_seconds())
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert site.paths().count("/robots.txt") == 2


async def test_a_failing_robots_allows_for_one_hour_then_asks_again(clock):
    robots = [httpx.Response(503), ALLOW_ALL]
    site = Site(clock, {f"{EX}/robots.txt": robots, f"{EX}/a": page()})
    fetcher = fetcher_for(site)
    first = await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert first.status == 200
    clock.advance(30 * 60)
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert site.paths().count("/robots.txt") == 1
    clock.advance(31 * 60)
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert site.paths().count("/robots.txt") == 2


async def test_a_missing_robots_allows_everything(clock):
    site = Site(clock, {f"{EX}/a": page()})  # robots.txt → 404
    fetcher = fetcher_for(site)
    result = await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert result.status == 200
    assert site.paths() == ["/robots.txt", "/a"]


async def test_robots_is_cached_on_disk_across_processes(clock, tmp_path):
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    site = Site(clock, {f"{EX}/robots.txt": robots, f"{EX}/a": page()})
    first = fetcher_for(site, robots_dir=tmp_path)
    await first.get(f"{EX}/a", source="directories", budget=budget())
    cached = (tmp_path / "www.namuna.example.uz.txt").read_text(encoding="utf-8")
    assert cached.splitlines()[1:] == ["User-agent: *", "Disallow: /private"]

    # A restarted process: new HostState, same disk.
    second = fetcher_for(site, robots_dir=tmp_path, hosts=HostState())
    with pytest.raises(RobotsDisallowed):
        await second.get(f"{EX}/private", source="directories", budget=budget())
    await second.get(f"{EX}/a", source="directories", budget=budget())
    assert site.paths().count("/robots.txt") == 1


async def test_robots_opt_out_only_for_the_documented_apis(clock):
    api = "https://api.hh.ru"
    site = Site(clock, {f"{api}/vacancies": httpx.Response(200, json={"items": []})})
    fetcher = fetcher_for(site)
    result = await fetcher.get(
        f"{api}/vacancies", source="hh_vacancies", budget=budget(), obey_robots=False
    )
    assert result.status == 200
    assert site.paths() == ["/vacancies"]  # no robots.txt for a documented API

    with pytest.raises(RobotsOptOutRefused):
        await fetcher.get(
            f"{EX}/a", source="directories", budget=budget(), obey_robots=False
        )
    with pytest.raises(RobotsOptOutRefused):
        await fetcher.post_json(
            f"{EX}/api", source="directories", budget=budget(), json={}
        )
    assert site.paths() == ["/vacancies"]
    assert {"api.hh.ru", "graph.facebook.com", "api.apify.com"} <= (
        http.ROBOTS_OPTOUT_HOSTS
    )
    assert "www.olx.uz" not in http.ROBOTS_OPTOUT_HOSTS


# --- the opt-out call-site fence (03 §1.1, 07 §1.2) -----------------------------

# Modules that call documented JSON APIs with a key (03 §2.1, §2.4, §2.5, §2.7,
# §2.9, §2.11). services/http.py passes its own parameter through.
OPTOUT_MODULES = frozenset(
    {
        "services/http.py",
        "services/serp.py",
        "collectors/hh_vacancies.py",
        "collectors/maps.py",
        "collectors/instagram_discovery.py",
        "collectors/olx_business.py",
        "collectors/uzum_sellers.py",
    }
)


def _opt_outs(tree: ast.AST) -> list[int]:
    """Lines of every call that passes ``obey_robots`` anything but literal True."""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "obey_robots":
                continue
            value = keyword.value
            if not (isinstance(value, ast.Constant) and value.value is True):
                lines.append(node.lineno)
    return lines


def test_obey_robots_false_only_at_allow_listed_call_sites():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(PACKAGE).as_posix()
        if rel in OPTOUT_MODULES:
            continue
        lines = _opt_outs(ast.parse(path.read_text(encoding="utf-8")))
        offenders += [f"{rel}:{line}" for line in lines]
    assert offenders == []


def test_the_fence_catches_each_shape():
    source = (
        "async def f(fetcher, b, flag):\n"
        "    await fetcher.get(u, source='x', budget=b, obey_robots=False)\n"
        "    await fetcher.get(u, source='x', budget=b, obey_robots=flag)\n"
        "    await fetcher.get(u, source='x', budget=b, obey_robots=0)\n"
        "    await fetcher.get(u, source='x', budget=b, obey_robots=True)\n"
        "    await fetcher.get(u, source='x', budget=b)\n"
    )
    assert _opt_outs(ast.parse(source)) == [2, 3, 4]


# --- conditional fetch ------------------------------------------------------------


async def test_a_304_answers_with_the_stored_copy(clock):
    stored = StoredCopy(sha256="a" * 64, body=b"<html>old</html>", etag='"v1"')
    hooks = Hooks({f"{EX}/a": stored})

    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"etag": '"v1"'})
        return page("<html>new</html>")

    site = site_with(clock, {f"{EX}/a": answer})
    fetcher = fetcher_for(site, hooks=hooks)
    result = await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert result.status == 304
    assert result.not_modified is True
    assert result.from_cache is True
    assert result.body == b"<html>old</html>"
    assert result.sha256 == "a" * 64
    assert fetcher.stats.unchanged == 1
    assert hooks.results == [result]


async def test_last_modified_is_sent_as_if_modified_since(clock):
    stamp = "Wed, 21 Oct 2026 07:28:00 GMT"
    stored = StoredCopy(sha256="b" * 64, body=b"x", last_modified=stamp)
    site = site_with(clock, {f"{EX}/a": httpx.Response(304)})
    fetcher = fetcher_for(site, hooks=Hooks({f"{EX}/a": stored}))
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert site.seen[-1].request.headers["if-modified-since"] == stamp


async def test_identical_bytes_count_unchanged_but_are_a_fresh_answer(clock):
    body = b"<html>same</html>"
    stored = StoredCopy(sha256=hashlib.sha256(body).hexdigest(), body=body)
    site = site_with(clock, {f"{EX}/a": httpx.Response(200, headers=HTML, content=body)})
    fetcher = fetcher_for(site, hooks=Hooks({f"{EX}/a": stored}))
    result = await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert (result.status, result.from_cache, result.not_modified) == (200, False, False)
    assert fetcher.stats.unchanged == 1


async def test_cache_false_sends_no_validators(clock):
    stored = StoredCopy(sha256="c" * 64, body=b"x", etag='"v1"')
    site = site_with(clock, {f"{EX}/a": page()})
    fetcher = fetcher_for(site, hooks=Hooks({f"{EX}/a": stored}))
    await fetcher.get(f"{EX}/a", source="directories", budget=budget(), cache=False)
    assert "if-none-match" not in site.seen[-1].request.headers
    assert fetcher.stats.unchanged == 0


# --- Retry-After and backoff --------------------------------------------------------


async def test_retry_after_is_honoured(clock, monkeypatch):
    monkeypatch.setattr(retry.random, "uniform", lambda low, high: 0.0)
    site = site_with(
        clock,
        {f"{EX}/a": [httpx.Response(429, headers={"retry-after": "7"}), page()]},
    )
    fetcher = fetcher_for(site, rps_default=10.0)
    result = await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert result.status == 200
    assert 7.0 in clock.sleeps
    assert fetcher.stats.http_429 == 1
    assert fetcher.stats.requests == 3  # robots + 429 + 200


async def test_a_retry_after_date_is_read_relative_to_now(clock):
    when = T0 + timedelta(seconds=40)
    stamp = when.astimezone(http.settings.tz).strftime("%a, %d %b %Y %H:%M:%S %z")
    assert http.retry_after_seconds(stamp, now=T0) == 40.0
    assert http.retry_after_seconds("120", now=T0) == 120.0
    assert http.retry_after_seconds("soon", now=T0) is None


async def test_a_retry_after_beyond_15_minutes_trips_the_breaker(clock):
    site = site_with(
        clock, {f"{EX}/a": httpx.Response(429, headers={"retry-after": "3600"})}
    )
    fetcher = fetcher_for(site)
    with pytest.raises(HostBlocked):
        await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert site.paths().count("/a") == 1
    assert 3600.0 not in clock.sleeps


async def test_a_5xx_is_retried_three_times_then_returned(clock):
    site = site_with(clock, {f"{EX}/a": httpx.Response(502)})
    fetcher = fetcher_for(site)
    result = await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert result.status == 502
    assert site.paths().count("/a") == 3


async def test_a_4xx_other_than_429_is_never_retried(clock):
    site = site_with(clock, {f"{EX}/a": httpx.Response(404)})
    fetcher = fetcher_for(site)
    result = await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert result.status == 404
    assert site.paths().count("/a") == 1


# --- the breaker --------------------------------------------------------------------


async def test_the_breaker_opens_on_the_fifth_consecutive_403(clock):
    site = site_with(clock, {f"{EX}/a": httpx.Response(403)})
    fetcher = fetcher_for(site)
    for _ in range(4):
        result = await fetcher.get(f"{EX}/a", source="directories", budget=budget())
        assert result.status == 403
    with pytest.raises(HostBlocked) as caught:
        await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert caught.value.host == "www.namuna.example.uz"
    assert caught.value.until == clock.now() + BREAKER_PAUSE
    assert fetcher.stats.http_403 == 5
    assert fetcher.stats.blocked_hosts == ["www.namuna.example.uz"]

    # Paused: nothing more is sent to that host until the pause ends.
    before = len(site.seen)
    with pytest.raises(HostPaused):
        await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert len(site.seen) == before
    clock.advance(BREAKER_PAUSE.total_seconds() + 1)
    site.add(f"{EX}/a", page())
    assert (
        await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    ).status == 200


async def test_429s_and_403s_share_one_streak(clock):
    site = site_with(
        clock,
        {f"{EX}/a": [httpx.Response(403), httpx.Response(403), httpx.Response(429)]},
    )
    fetcher = fetcher_for(site)
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())  # 403
    await fetcher.get(f"{EX}/a", source="directories", budget=budget())  # 403
    # 429 → retried by backoff: three attempts, three strikes → the fifth trips.
    with pytest.raises(HostBlocked):
        await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert (fetcher.stats.http_403, fetcher.stats.http_429) == (2, 3)


async def test_a_2xx_resets_the_streak(clock):
    answers = [httpx.Response(403)] * 4 + [page()] + [httpx.Response(403)] * 4 + [page()]
    site = site_with(clock, {f"{EX}/a": answers})
    fetcher = fetcher_for(site)
    statuses = [
        (await fetcher.get(f"{EX}/a", source="directories", budget=budget())).status
        for _ in range(10)
    ]
    assert statuses == [403] * 4 + [200] + [403] * 4 + [200]
    assert fetcher.stats.blocked_hosts == []


async def test_a_captcha_page_counts_as_a_403(clock):
    challenge = page("<html><h1>Are you a robot?</h1></html>")
    site = site_with(clock, {f"{EX}/a": challenge})
    fetcher = fetcher_for(site)
    for _ in range(4):
        result = await fetcher.get(f"{EX}/a", source="directories", budget=budget())
        assert (result.status, result.captcha) == (200, True)
    with pytest.raises(HostBlocked):
        await fetcher.get(f"{EX}/a", source="directories", budget=budget())
    assert (fetcher.stats.captcha, fetcher.stats.http_403) == (5, 5)


@pytest.mark.parametrize(
    "body",
    [
        "<p>Введите captcha</p>",
        "<script src='/anomaly.js'></script>",
        "<h1>Доступ ограничен</h1>",
        "<div class='cf-chl-widget'></div>",
    ],
)
def test_captcha_markers(body):
    assert http.looks_like_captcha(body.encode()) is True


def test_an_ordinary_page_is_not_a_captcha():
    assert http.looks_like_captcha(b"<p>Namuna Savdo, optom</p>") is False


RECAPTCHA_FORM = (
    "<html><head><title>Namuna Tekstil MChJ</title>"
    '<script src="https://www.google.com/recaptcha/api.js"></script></head>'
    '<body><div class="firm">Namuna Tekstil MChJ, +998 90 000 00 01</div>'
    '<form id="contact"><div class="g-recaptcha" data-sitekey="x"></div>'
    '<input type="hidden" name="g-recaptcha-response"></form>'
    "<noscript>Please enable JavaScript to use reCAPTCHA</noscript></body></html>"
)


def test_a_page_embedding_a_recaptcha_widget_is_not_a_captcha_page():
    assert http.looks_like_captcha(RECAPTCHA_FORM.encode()) is False


def test_a_long_page_with_a_captcha_label_is_not_a_captcha_page():
    listing = "".join(f"<li>Namuna Savdo {i}, optom mebel</li>" for i in range(100))
    body = f"<html><body><ul>{listing}</ul><label>Captcha</label></body></html>"
    assert http.looks_like_captcha(body.encode()) is False
    titled = f"<html><head><title>Captcha</title></head><body>{listing}</body></html>"
    assert http.looks_like_captcha(titled.encode()) is True


async def test_five_firm_pages_with_a_recaptcha_form_do_not_trip_the_breaker(clock):
    site = site_with(clock, {f"{EX}/firm": page(RECAPTCHA_FORM)})
    fetcher = fetcher_for(site)
    for _ in range(6):
        result = await fetcher.get(f"{EX}/firm", source="directories", budget=budget())
        assert (result.status, result.captcha) == (200, False)
    assert (fetcher.stats.http_403, fetcher.stats.blocked_hosts) == (0, [])


async def test_bind_shares_the_breaker_but_not_the_counters(clock):
    site = site_with(clock, {f"{EX}/a": httpx.Response(403)})
    shared = fetcher_for(site)
    run1, run2 = shared.bind(), shared.bind()
    for _ in range(3):
        await run1.get(f"{EX}/a", source="directories", budget=budget())
    await run2.get(f"{EX}/a", source="directories", budget=budget())
    with pytest.raises(HostBlocked):
        await run2.get(f"{EX}/a", source="directories", budget=budget())
    assert (run1.stats.http_403, run2.stats.http_403) == (3, 2)
    assert run1.stats.blocked_hosts == []
    with pytest.raises(HostPaused):
        await run1.get(f"{EX}/a", source="directories", budget=budget())


# --- budgets ------------------------------------------------------------------------


async def test_budget_exhausted_stops_a_loop_mid_page(clock):
    routes = {f"{EX}/page/{n}": page(f"<html>{n}</html>") for n in range(1, 6)}
    site = site_with(clock, routes)
    fetcher = fetcher_for(site)
    b = budget(pages=2)
    got: list[int] = []
    with pytest.raises(BudgetExhausted) as caught:
        for n in range(1, 6):
            await fetcher.get(f"{EX}/page/{n}", source="directories", budget=b)
            got.append(n)
    assert got == [1, 2]
    assert caught.value.reason == "pages"
    assert site.paths() == ["/robots.txt", "/page/1", "/page/2"]
    assert (fetcher.stats.pages, fetcher.stats.exhausted, b.pages) == (2, "pages", 0)


async def test_the_run_deadline_stops_requests(clock):
    site = site_with(clock, {f"{EX}/a": page()})
    fetcher = fetcher_for(site)
    with pytest.raises(BudgetExhausted) as caught:
        await fetcher.get(f"{EX}/a", source="directories", budget=budget(deadline=T0))
    assert caught.value.reason == "deadline"
    assert "/a" not in site.paths()


# --- paid hosts ---------------------------------------------------------------------


class Priced:
    def __init__(self, cost: str | None) -> None:
        self.cost_usd = Decimal(cost) if cost is not None else None


class Gate:
    """Stands in for functools.partial(costs.ask, session, now=…)."""

    def __init__(self, refuse: bool = False) -> None:
        self.refuse = refuse
        self.calls: list[dict] = []

    async def __call__(self, *, operation, est_usd, call, price, source_key):
        self.calls.append(
            {"operation": operation, "est_usd": est_usd, "source_key": source_key}
        )
        if self.refuse:
            raise RuntimeError("cost_cap_op:serp_brave")
        result = await call()
        price(result)
        return result


BRAVE = "https://api.search.brave.com/res/v1/web/search"


def brave_site(clock: FakeClock) -> Site:
    return Site(clock, {BRAVE: httpx.Response(200, json={"web": {"results": []}})})


async def test_a_metered_host_refuses_a_request_without_a_paid_call(clock):
    site = brave_site(clock)
    fetcher = fetcher_for(site, cost_gate=Gate())
    with pytest.raises(UnmeteredCall):
        await fetcher.get(
            BRAVE, source="search_brave", budget=budget(), obey_robots=False
        )
    assert site.seen == []


async def test_a_paid_call_needs_the_cost_gate(clock):
    site = brave_site(clock)
    fetcher = fetcher_for(site)
    paid = PaidCall("serp_brave", Decimal("0.005"), lambda r: Priced("0.005"))
    with pytest.raises(UnmeteredCall):
        await fetcher.get(
            BRAVE,
            source="search_brave",
            budget=budget(usd="1"),
            obey_robots=False,
            paid=paid,
        )
    assert site.seen == []


async def test_a_paid_call_clears_the_budget_then_the_gate_then_goes(clock):
    site = brave_site(clock)
    gate = Gate()
    fetcher = fetcher_for(site, cost_gate=gate)
    paid = PaidCall("serp_brave", Decimal("0.005"), lambda r: Priced("0.004"))
    b = budget(usd="0.010")
    result = await fetcher.get(
        BRAVE, source="search_brave", budget=b, obey_robots=False, paid=paid
    )
    assert result.status == 200
    assert gate.calls == [
        {
            "operation": "serp_brave",
            "est_usd": Decimal("0.005"),
            "source_key": "search_brave",
        }
    ]
    assert (b.usd, b.spent_usd, fetcher.stats.cost_usd) == (
        Decimal("0.006"),
        Decimal("0.004"),
        Decimal("0.004"),
    )


async def test_the_source_budget_refuses_before_the_gate_is_asked(clock):
    site = brave_site(clock)
    gate = Gate()
    fetcher = fetcher_for(site, cost_gate=gate)
    paid = PaidCall("serp_brave", Decimal("0.005"), lambda r: Priced("0.005"))
    with pytest.raises(BudgetExhausted) as caught:
        await fetcher.get(
            BRAVE,
            source="search_brave",
            budget=budget(usd="0.001"),
            obey_robots=False,
            paid=paid,
        )
    assert caught.value.reason == "usd"
    assert gate.calls == [] and site.seen == []


async def test_a_gate_refusal_is_a_budget_stop_and_nothing_is_sent(clock):
    site = brave_site(clock)
    fetcher = fetcher_for(site, cost_gate=Gate(refuse=True))
    paid = PaidCall("serp_brave", Decimal("0.005"), lambda r: Priced("0.005"))
    with pytest.raises(BudgetExhausted) as caught:
        await fetcher.get(
            BRAVE,
            source="search_brave",
            budget=budget(usd="1"),
            obey_robots=False,
            paid=paid,
        )
    assert caught.value.reason == "cost_cap"
    assert fetcher.stats.exhausted == "cost_cap"
    assert site.seen == []


async def test_proxy_needs_proxy_url(clock, monkeypatch):
    monkeypatch.setattr(http.settings, "proxy_url", "")
    site = site_with(clock, {f"{EX}/a": page()})
    fetcher = fetcher_for(site)
    with pytest.raises(http.ProxyUnavailable):
        await fetcher.get(f"{EX}/a", source="uzum_sellers", budget=budget(), proxy=True)
    assert site.seen == []


# --- body cap, redirects --------------------------------------------------------------


async def test_a_body_over_the_cap_is_truncated(clock, monkeypatch):
    monkeypatch.setattr(http, "BODY_CAP", 10)
    site = site_with(clock, {f"{EX}/big": page("x" * 50)})
    fetcher = fetcher_for(site)
    result = await fetcher.get(f"{EX}/big", source="directories", budget=budget())
    assert (result.body, result.truncated) == (b"x" * 10, True)
    assert fetcher.stats.truncated == 1


async def test_params_are_merged_into_the_query_the_url_already_has(clock):
    api = "https://api.hh.ru"
    site = Site(clock, {f"{api}/vacancies": httpx.Response(200, json={"items": []})})
    fetcher = fetcher_for(site)
    result = await fetcher.get(
        f"{api}/vacancies?text=optom&area=2759",
        source="hh_vacancies",
        budget=budget(),
        obey_robots=False,
        params={"page": 2},
    )
    assert [s.url for s in site.seen] == [
        "https://api.hh.ru/vacancies?text=optom&area=2759&page=2"
    ]
    assert result.url == "https://api.hh.ru/vacancies?text=optom&area=2759&page=2"


async def test_a_same_site_redirect_is_followed_and_a_cross_host_one_returned(clock):
    bare = "https://namuna.example.uz"
    site = Site(
        clock,
        {
            f"{bare}/robots.txt": ALLOW_ALL,
            f"{bare}/old": httpx.Response(301, headers={"location": f"{EX}/new"}),
            f"{EX}/new": page("<html>new</html>"),
            f"{EX}/robots.txt": ALLOW_ALL,
            f"{EX}/out": httpx.Response(
                302, headers={"location": "https://t.me/namuna_1"}
            ),
        },
    )
    fetcher = fetcher_for(site)
    followed = await fetcher.get(f"{bare}/old", source="directories", budget=budget())
    assert (followed.status, followed.final_url, followed.body) == (
        200,
        f"{EX}/new",
        b"<html>new</html>",
    )
    crossed = await fetcher.get(f"{EX}/out", source="directories", budget=budget())
    assert crossed.status == 302
    assert crossed.headers["location"] == "https://t.me/namuna_1"
    assert "t.me" not in {s.request.url.host for s in site.seen}


# --- logs -------------------------------------------------------------------------------


async def test_logs_scrub_keys_and_tokens_and_never_carry_the_body(clock, caplog):
    api = "https://api.hh.ru/vacancies"
    secret_body = "Namuna maxfiy tana +998 90 000 00 01"
    site = Site(clock, {api: httpx.Response(200, text=secret_body)})
    fetcher = fetcher_for(site)
    with caplog.at_level(logging.INFO, logger="clientscrapper.services.http"):
        await fetcher.get(
            api,
            source="hh_vacancies",
            budget=budget(),
            obey_robots=False,
            params={"text": "VED", "key": "K3YVALUE", "access_token": "T0KENVALUE"},
        )
    [record] = [r for r in caplog.records if r.name == "clientscrapper.services.http"]
    rendered = record.getMessage() + repr(record.__dict__)
    assert "K3YVALUE" not in rendered and "T0KENVALUE" not in rendered
    assert "maxfiy" not in rendered and "000 00 01" not in rendered
    stored = "https://api.hh.ru/vacancies?text=VED"
    assert record.url_id == hashlib.sha256(stored.encode()).hexdigest()[:12]
    assert record.getMessage() == f"GET api.hh.ru {record.url_id} -> 200"
    assert (record.host, record.status, record.source, record.bytes) == (
        "api.hh.ru",
        200,
        "hh_vacancies",
        len(secret_body.encode()),
    )


async def test_logs_never_carry_the_path_that_names_a_person(clock, caplog):
    tg = "https://t.me"
    site = Site(clock, {f"{tg}/robots.txt": ALLOW_ALL, f"{tg}/s/namuna_person_7": page()})
    fetcher = fetcher_for(site)
    with caplog.at_level(logging.INFO, logger="clientscrapper.services.http"):
        await fetcher.get(
            f"{tg}/s/namuna_person_7?q=%2B998900000007&phone=900000007",
            source="telegram_monitor",
            budget=budget(),
        )
    rendered = " ".join(
        r.getMessage() + repr(r.__dict__)
        for r in caplog.records
        if r.name == "clientscrapper.services.http"
    )
    assert "namuna_person_7" not in rendered and "900000007" not in rendered


@pytest.mark.parametrize(
    ("url", "scrubbed"),
    [
        ("https://a.uz/x?key=abc", "https://a.uz/x?key=***"),
        ("https://a.uz/x?q=1&token=abc&p=2", "https://a.uz/x?q=1&token=***&p=2"),
        ("https://a.uz/x?apikey=abc", "https://a.uz/x?apikey=***"),
        ("https://a.uz/x?access_token=abc#f", "https://a.uz/x?access_token=***#f"),
        ("https://a.uz/x?API_KEY=abc", "https://a.uz/x?API_KEY=***"),
        ("https://a.uz/x?q=key", "https://a.uz/x?q=key"),
    ],
)
def test_scrub_url(url, scrubbed):
    assert http.scrub_url(url) == scrubbed
