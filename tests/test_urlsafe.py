"""services/urlsafe: credentials out of a URL — masked for a log line or a
fixture README, removed for anything stored. Pure."""

from __future__ import annotations

import pytest

from clientscrapper.services.urlsafe import is_secret_param, scrub_url, strip_secrets


@pytest.mark.parametrize(
    ("url", "stripped"),
    [
        (
            "https://graph.facebook.com/v21.0/1?fields=a%2Cb&access_token=EAAB&x=1",
            "https://graph.facebook.com/v21.0/1?fields=a%2Cb&x=1",
        ),
        (
            "https://api.apify.com/v2/acts/x/runs?token=apify_api_1",
            "https://api.apify.com/v2/acts/x/runs",
        ),
        (
            "https://catalog.api.2gis.com/3.0/items?q=mebel&key=K",
            "https://catalog.api.2gis.com/3.0/items?q=mebel",
        ),
        (
            "https://a.example.uz/x?API_KEY=1&appsecret_proof=2#f",
            "https://a.example.uz/x#f",
        ),
        (
            "https://a.example.uz/x?q=key&keyword=optom",
            "https://a.example.uz/x?q=key&keyword=optom",
        ),
        ("https://a.example.uz/x?access%5Ftoken=1&p=2", "https://a.example.uz/x?p=2"),
        ("https://a.example.uz/x", "https://a.example.uz/x"),
    ],
)
def test_strip_secrets(url, stripped):
    assert strip_secrets(url) == stripped


def test_scrub_masks_and_keeps_the_shape():
    assert (
        scrub_url("https://a.example.uz/x?q=1&token=abc&signature=s#f")
        == "https://a.example.uz/x?q=1&token=***&signature=***#f"
    )


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("key", True),
        ("apikey", True),
        ("client_secret", True),
        ("keyword", False),
        ("q", False),
    ],
)
def test_is_secret_param(name, secret):
    assert is_secret_param(name) is secret
