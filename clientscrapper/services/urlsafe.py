"""URLs without the credentials an API call carries in its query string.

Graph puts ``access_token`` in the query, Apify ``token``, 2GIS ``key``. Two
uses, two strengths: ``scrub_url`` masks the value (``key=***``) for a log
line or a fixture README, where the shape of the call is worth seeing;
``strip_secrets`` removes the parameter for anything stored (``evidence.url``,
``evidence.meta.final_url``, ``observations.parsed.url``), which goes into
backups and into the URL a card or an ERP note cites. A secret parameter is
one whose name ends in ``key``, ``token``, ``secret``, ``password`` or
``signature``, or contains ``secret`` (Graph's ``appsecret_proof``). Pure.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import unquote, urlsplit, urlunsplit

__all__ = ["is_secret_param", "scrub_url", "strip_secrets"]

_SECRET_NAME: Final = re.compile(r"(?i)(?:key|token|secret|password|signature)\Z|secret")
_PAIR: Final = re.compile(r"([?&;])([^=&#;]*)=([^&#;]*)")


def is_secret_param(name: str) -> bool:
    return _SECRET_NAME.search(unquote(name)) is not None


def scrub_url(url: str) -> str:
    """``?q=x&key=abc&access_token=def`` → ``?q=x&key=***&access_token=***``."""

    def mask(match: re.Match[str]) -> str:
        sep, name, value = match.groups()
        return f"{sep}{name}=***" if is_secret_param(name) else match.group(0)

    head, hash_, fragment = url.partition("#")
    return _PAIR.sub(mask, head) + hash_ + fragment


def strip_secrets(url: str) -> str:
    """``url`` with every secret parameter removed; the rest of the query is
    kept byte for byte, in its order."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [
        pair
        for pair in re.split(r"[&;]", parts.query)
        if pair and not is_secret_param(pair.split("=", 1)[0])
    ]
    return urlunsplit(parts._replace(query="&".join(kept)))
