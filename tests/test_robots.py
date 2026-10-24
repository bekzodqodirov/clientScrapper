"""services/robots: robots.txt as RFC 9309 reads it (03 §1.1).

Pure. Each case is one a bare ``urllib.robotparser`` gets wrong or one the
RFC spells out; the synthetic host is ``www.namuna.example.uz``.
"""

from __future__ import annotations

import pytest

from clientscrapper.services.robots import parse_robots, robots_path

AGENT = "clientScrapperBot"
EX = "https://www.namuna.example.uz"


def allows(robots: str, path: str, agent: str = AGENT) -> bool:
    return parse_robots(robots, agent).allows(EX + path)


def test_the_longest_rule_wins_whatever_its_place_in_the_file():
    robots = "User-agent: *\nAllow: /\nDisallow: /private/\nDisallow: /search\n"
    assert allows(robots, "/private/x") is False
    assert allows(robots, "/search?q=optom") is False
    assert allows(robots, "/katalog/1") is True


def test_a_longer_allow_opens_a_door_in_a_disallow():
    robots = "User-agent: *\nDisallow: /firm/\nAllow: /firm/namuna-savdo\n"
    assert allows(robots, "/firm/namuna-savdo/contacts") is True
    assert allows(robots, "/firm/other") is False


def test_allow_wins_a_tie():
    robots = "User-agent: *\nDisallow: /page\nAllow: /page\n"
    assert allows(robots, "/page") is True


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/list?page=2", False),
        ("/list?sort=new&page=2", False),
        ("/list", True),
        ("/firm/contacts", False),
        ("/firm/contacts/map", True),  # «$» anchors the end
        ("/a/b/c.pdf", False),
        ("/a/b/c.pdf?x=1", True),
    ],
)
def test_star_and_dollar(path, expected):
    robots = (
        "User-agent: *\n"
        "Disallow: /*?*page=\n"
        "Disallow: /*/contacts$\n"
        "Disallow: /*.pdf$\n"
    )
    assert allows(robots, path) is expected


def test_a_byte_order_mark_does_not_hide_the_first_group():
    robots = "\ufeffUser-agent: *\nDisallow: /private\n"
    assert allows(robots, "/private/x") is False


def test_our_groups_are_merged_and_beat_the_star_group():
    robots = (
        "User-agent: clientScrapperBot\nDisallow: /a\n\n"
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: CLIENTSCRAPPERBOT/2.0\nUser-agent: otherbot\nDisallow: /b\n"
    )
    assert allows(robots, "/a/1") is False
    assert allows(robots, "/b/1") is False
    assert allows(robots, "/c/1") is True  # the star group's «/» is not ours
    assert allows(robots, "/c/1", agent="somebot") is False


def test_no_group_for_us_and_no_star_group_means_no_rules():
    assert allows("User-agent: otherbot\nDisallow: /\n", "/x") is True


def test_an_empty_disallow_and_rules_outside_any_group_are_ignored():
    robots = "Disallow: /x\nUser-agent: *\nDisallow:\n"
    assert allows(robots, "/x") is True


def test_comments_and_unknown_lines_are_ignored():
    robots = (
        "# rules\nUser-agent: * # everyone\nSitemap: https://x.example.uz/s.xml\n"
        "Disallow: /tmp # temporary\n"
    )
    assert allows(robots, "/tmp/1") is False
    assert allows(robots, "/tmp2") is False  # a prefix match, as the RFC has it


def test_robots_txt_itself_is_always_allowed():
    assert allows("User-agent: *\nDisallow: /\n", "/robots.txt") is True


def test_percent_encoding_is_compared_in_one_spelling():
    robots = "User-agent: *\nDisallow: /~karim\nDisallow: /katalog/мебель\n"
    assert allows(robots, "/%7Ekarim/1") is False
    assert allows(robots, "/katalog/%D0%BC%D0%B5%D0%B1%D0%B5%D0%BB%D1%8C") is False
    assert robots_path(EX + "/%7ea?q=%2f#frag") == "/~a?q=%2F"


def test_crawl_delay_from_our_group():
    robots = (
        "User-agent: *\nCrawl-delay: 2\n\n"
        "User-agent: clientScrapperBot\nCrawl-delay: 7.5\n"
    )
    assert parse_robots(robots, AGENT).crawl_delay == 7.5
    assert parse_robots("User-agent: *\nCrawl-delay: x\n", AGENT).crawl_delay is None
