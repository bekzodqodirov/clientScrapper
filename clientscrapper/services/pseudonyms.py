"""Pseudonymisation before any model call (04 §4.2, 06 §5.2).

A text leaves the process for a model only with its personal data replaced by
tokens, and the reverse map stays here (``model_jobs.pseudonyms``, 30 days,
never logged). The patterns are ``collectors/keywords.py``'s — imported, never
redefined, so the collectors, this module and the log redactor agree on what
a phone is (06 §5.2's one-home rule; ``tests/test_imports_fence.py`` holds it).

What is masked, in this order:

1. a run of exactly 14 digits (a PINFL's shape) → ``<ID>``: not reversible,
   never stored. First, so no later pattern takes its digits into a
   reversible token (IG_RE's label branch reads «ig 12345678901234»);
2. phones (``PHONE_RE``, through ``normalise.phone_matches``: a labelled
   STIR is the company's tax id, not a phone) → ``<PHONE_n>``; one number is
   one token however it is written («+998 90 123 45 67» and «90 123-45-67»,
   ``phone_e164``);
3. Telegram handles and links (``TG_RE``) → ``<TG_n>``, the handle only
   («@<TG_1>», «t.me/<TG_1>»: the reader still sees what kind of thing it was);
4. e-mails (``EMAIL_RE``) → ``<EMAIL_n>``;
5. Instagram handles (``IG_RE``) → ``<IG_n>`` — last of the four on purpose:
   its label branch takes the word after «IG »/«Instagram », so run first it
   would swallow the phone, handle or address that follows the label;
6. the ``names`` the caller knows (a sender's first and last name, a person a
   bio names) → ``<NAME_n>``, each word in both scripts and with its Uzbek
   case ending kept outside the token («Rustamga» → «<NAME_1>ga»), through
   ``text.alias_pattern`` (the one name matcher).

STIR, company names, cities, goods and prices stay: they are company-tier.
``prior`` continues a bundle's numbering: a value seen in an earlier block
keeps its token, and its names are masked again in the later blocks.

A token stands for the **first** spelling of its value; ``restore`` puts that
spelling back. A text that writes one number, handle or name twice in two
spellings comes back with the first one in both places — exact for every
text that spells each value one way.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from clientscrapper.collectors.keywords import EMAIL_RE, IG_RE, PHONE_RE, TG_RE
from clientscrapper.services.keys import phone_e164
from clientscrapper.services.normalise import phone_matches
from clientscrapper.services.text import (
    ALIAS_SUFFIXES,
    alias_pattern,
    fold_apostrophes,
    to_latin,
)

Kind = Literal["PHONE", "TG", "EMAIL", "IG", "NAME"]
KINDS: Final[tuple[Kind, ...]] = ("PHONE", "TG", "EMAIL", "IG", "NAME")
TOKEN_RE: Final = re.compile(r"<(PHONE|TG|EMAIL|IG|NAME)_(\d+)>")
ID_TOKEN: Final = "<ID>"
# A maximal run of exactly 14 digits: a 17-digit 2GIS or Graph id is not one
# (02 §2.5's rule for observations).
PINFL_RE: Final = re.compile(r"(?<!\d)\d{14}(?!\d)")
# What assert_masked refuses: the four patterns of 06 §5.2, by name.
GUARDED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("PHONE_RE", PHONE_RE),
    ("TG_RE", TG_RE),
    ("IG_RE", IG_RE),
    ("EMAIL_RE", EMAIL_RE),
)
# A name word shorter than this is a particle or an initial, not a name.
NAME_MIN: Final = 2


class UnmaskedPayload(ValueError):
    """A text bound for a model still matches a personal-data pattern. The
    message names the pattern, never the value."""

    def __init__(self, pattern: str) -> None:
        super().__init__(pattern)
        self.pattern = pattern


@dataclass(frozen=True, slots=True)
class Pseudonymised:
    """The masked text and the token → value map that undoes it
    (``{"<PHONE_1>": "+998 90 000 00 01", "<TG_1>": "namuna_tekstil",
    "<NAME_1>": "Rustam"}``)."""

    text: str
    reverse: dict[str, str] = field(default_factory=dict)


# --- the keys a value is known by ---------------------------------------------


def _phone_key(raw: str) -> str:
    return phone_e164(raw) or "#" + re.sub(r"\D", "", raw)


def _name_key(word: str) -> str:
    return to_latin(fold_apostrophes(word)).casefold()


def _key(kind: Kind, value: str) -> str:
    if kind == "PHONE":
        return _phone_key(value)
    if kind == "NAME":
        return _name_key(value)
    return value.casefold()


class _Tokens:
    """Value → token, per kind, continuing ``prior``'s numbering."""

    def __init__(self, prior: Pseudonymised | None) -> None:
        self.reverse: dict[str, str] = dict(prior.reverse) if prior else {}
        self.known: dict[tuple[Kind, str], str] = {}
        self.count: dict[Kind, int] = dict.fromkeys(KINDS, 0)
        for token, value in self.reverse.items():
            match = TOKEN_RE.fullmatch(token)
            if match is None:
                continue
            kind: Kind = match.group(1)  # type: ignore[assignment]
            self.known.setdefault((kind, _key(kind, value)), token)
            self.count[kind] = max(self.count[kind], int(match.group(2)))

    def token(self, kind: Kind, value: str) -> str:
        key = (kind, _key(kind, value))
        held = self.known.get(key)
        if held is not None:
            return held
        self.count[kind] += 1
        token = f"<{kind}_{self.count[kind]}>"
        self.known[key] = token
        self.reverse[token] = value
        return token

    def names(self) -> list[str]:
        return [v for t, v in self.reverse.items() if t.startswith("<NAME_")]


def _mask_group(
    text: str, pattern: re.Pattern[str], kind: Kind, tokens: _Tokens, *, group: int
) -> str:
    """Every match of ``pattern`` with its ``group`` replaced by a token
    (``group=0``: the whole match)."""

    def swap(match: re.Match[str]) -> str:
        whole, start = match.group(0), match.start()
        value = match.group(group)
        head = whole[: match.start(group) - start]
        tail = whole[match.end(group) - start :]
        return head + tokens.token(kind, value) + tail

    return pattern.sub(swap, text)


def _name_words(names: Iterable[str]) -> list[str]:
    """Each word of each name, once (by its folded key), in order."""
    words: list[str] = []
    seen: set[str] = set()
    for name in names:
        for word in re.split(r"[\s\-]+", name.strip()):
            word = word.strip(".,;:!?\"'«»()")
            if len(word) < NAME_MIN or not any(c.isalpha() for c in word):
                continue
            key = _name_key(word)
            if key not in seen:
                seen.add(key)
                words.append(word)
    return words


def _mask_names(text: str, words: list[str], tokens: _Tokens) -> str:
    """Each name word as a whole word, either script, any case; a case ending
    («ga», «ning», «дан» …) stays outside its token."""
    if not words:
        return text
    spellings = tuple(dict.fromkeys(s for w in words for s in (w, to_latin(w))))
    pattern = alias_pattern(spellings)
    if pattern is None:
        return text
    keys = {_name_key(w): w for w in words}
    suffixes = sorted(ALIAS_SUFFIXES, key=len, reverse=True)

    def swap(match: re.Match[str]) -> str:
        found = match.group(0)
        stem, ending = found, ""
        if _name_key(found) not in keys:
            for suffix in suffixes:
                head = found[: len(found) - len(suffix)]
                if found.casefold().endswith(suffix) and _name_key(head) in keys:
                    stem, ending = head, found[len(head) :]
                    break
        return tokens.token("NAME", stem) + ending

    return pattern.sub(swap, text)


def _mask_phones(text: str, tokens: _Tokens) -> str:
    out: list[str] = []
    last = 0
    for match in phone_matches(text):
        out += [text[last : match.start()], tokens.token("PHONE", match.group(0))]
        last = match.end()
    return "".join(out) + text[last:]


_Pass = Callable[[str, _Tokens], str]
# The order of 04 §4.2 (see the module docstring); names come last.
_PASSES: Final[tuple[_Pass, ...]] = (
    lambda t, _: PINFL_RE.sub(ID_TOKEN, t),
    _mask_phones,
    lambda t, k: _mask_group(t, TG_RE, "TG", k, group=1),
    lambda t, k: _mask_group(t, EMAIL_RE, "EMAIL", k, group=0),
    lambda t, k: _mask_group(t, IG_RE, "IG", k, group=1),
)


def pseudonymise(
    text: str, *, names: Iterable[str] = (), prior: Pseudonymised | None = None
) -> Pseudonymised:
    """``text`` with its personal data as tokens (the module docstring has
    the order); ``reverse`` holds ``prior``'s tokens and the new ones."""
    tokens = _Tokens(prior)
    words = _name_words([*tokens.names(), *names])
    masked = text
    for step in _PASSES:
        masked = step(masked, tokens)
    masked = _mask_names(masked, words, tokens)
    return Pseudonymised(masked, tokens.reverse)


def restore(text: str, reverse: Mapping[str, str]) -> str:
    """Every token of ``reverse`` back to its value; ``<ID>`` and an unknown
    token stay as they are."""
    return TOKEN_RE.sub(lambda m: reverse.get(m.group(0), m.group(0)), text)


def assert_masked(text: str) -> None:
    """Raise ``UnmaskedPayload`` when a phone, a Telegram handle, an
    Instagram handle or an e-mail is still in ``text`` (06 §5.2: run on every
    request before it is dispatched). A labelled STIR is no phone here either
    (``phone_matches``): it is company-tier and stays."""
    for name, pattern in GUARDED:
        found = phone_matches(text) if pattern is PHONE_RE else pattern.search(text)
        if found:
            raise UnmaskedPayload(name)


__all__ = [
    "ID_TOKEN",
    "KINDS",
    "TOKEN_RE",
    "Pseudonymised",
    "UnmaskedPayload",
    "assert_masked",
    "pseudonymise",
    "restore",
]
