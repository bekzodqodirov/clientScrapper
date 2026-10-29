"""Pseudonymisation before any model call (04 §4.2, 06 §5.2).

A text leaves the process for a model only with its personal data replaced by
tokens, and the reverse map stays here (``model_jobs.pseudonyms``, 30 days,
never logged). The patterns are ``collectors/keywords.py``'s — imported, never
redefined, so the collectors, this module and the log redactor agree on what
a phone is (06 §5.2's one-home rule; ``tests/test_imports_fence.py`` holds it).

What is masked, in this order:

1. a run of exactly 14 digits (``PINFL_RE``, a PINFL's shape) → ``<ID>``:
   not reversible, never stored. First, where 04 §4.2 lists it after IG_RE:
   IG_RE's label branch reads «ig 12345678901234» as a handle, and a run
   taken into a reversible ``<IG_n>`` would be stored in the reverse map —
   04's own «never stored» needs this order;
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
   bio names) → ``<NAME_n>``: each word as a whole word, any case, in both
   scripts, with any apostrophe a keyboard types for oʻ/gʻ/ʼ or none at all
   («Oʻtkir», «O’tkir», «O'tkir», «Otkir», «Ўткир»), and with its ending kept
   outside the token — an Uzbek case ending or the affectionate «-jon»
   («Rustamga» → «<NAME_1>ga», «Rustamjon» → «<NAME_1>jon») and, on a
   Cyrillic spelling, a Russian one («Рустаму» → «<NAME_1>у», «Дилнозы» →
   «<NAME_1>ы»). Any match is masked: a «Bekzodjon» who is not the sender's
   Bekzod is a person's name too.

STIR, company names, cities, goods and prices stay: they are company-tier.
``prior`` continues a bundle's numbering: a value seen in an earlier block
keeps its token, and its names are masked again in the later blocks.

A token stands for the **first** spelling of its value; ``restore`` puts that
spelling back. A text that writes one number, handle or name twice in two
spellings comes back with the first one in both places — exact for every
text that spells each value one way.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from clientscrapper.collectors.keywords import (
    EMAIL_RE,
    IG_RE,
    PHONE_RE,
    PINFL_RE,
    RU_ENDINGS,
    RU_STEM_DROP,
    TG_RE,
)
from clientscrapper.services.keys import phone_e164
from clientscrapper.services.normalise import phone_matches
from clientscrapper.services.text import (
    ALIAS_SUFFIXES,
    APOSTROPHE_CLASS,
    fold_apostrophes,
    to_latin,
    transliterate,
)

Kind = Literal["PHONE", "TG", "EMAIL", "IG", "NAME"]
KINDS: Final[tuple[Kind, ...]] = ("PHONE", "TG", "EMAIL", "IG", "NAME")
TOKEN_RE: Final = re.compile(r"<(PHONE|TG|EMAIL|IG|NAME)_(\d+)>")
ID_TOKEN: Final = "<ID>"
# What a text bound for a person (the ERP's lead, its note) may not carry: a
# token or the start of one, numbered or not — never restored (04 §7 item 11).
TOKEN_TRACE_RE: Final = re.compile(r"<(?:PHONE|TG|IG|NAME|EMAIL)_|<ID>")
# What assert_masked refuses, by name: the four patterns of 06 §5.2, and a
# 14-digit run (a PINFL never reaches a model either; this module masks every
# one, so a request that still carries one did not come through here).
GUARDED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("PHONE_RE", PHONE_RE),
    ("TG_RE", TG_RE),
    ("IG_RE", IG_RE),
    ("EMAIL_RE", EMAIL_RE),
    ("PINFL_RE", PINFL_RE),
)
# A name word shorter than this is a particle or an initial, not a name.
NAME_MIN: Final = 2
# A stem shorter than this takes no Russian ending («Ли» + «и»).
RU_NAME_STEM_MIN: Final = 3
# The affectionate «-jon» a name carries before its case ending: «Rustamjon»,
# «Рустамжонга» are Rustam.
JON: Final = ("jon", "жон")
# A Russian name's endings: keywords' noun endings and the adjectival
# instrumental of a «-ов»/«-ин» surname («Ивановым», «Пушкиным»).
RU_NAME_ENDINGS: Final = (*RU_ENDINGS, "ым", "им")
_A: Final = APOSTROPHE_CLASS
_CYRILLIC: Final = re.compile(r"[Ѐ-ӿ]")


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
    # Apostrophes out: «Oʻtkir», «O'tkir» and «Otkir» are one name.
    return to_latin(fold_apostrophes(word)).replace("'", "").casefold()


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

    def token(self, kind: Kind, value: str, *, also: Iterable[str] = ()) -> str:
        """The token of ``value`` — or of any of the keys ``also`` names (the
        name word a declined form was matched by): one name is one token
        however it is declined or spelled."""
        keys = [(kind, _key(kind, value)), *((kind, k) for k in also)]
        held = next((self.known[k] for k in keys if k in self.known), None)
        if held is None:
            self.count[kind] += 1
            held = f"<{kind}_{self.count[kind]}>"
            self.reverse[held] = value
        for key in keys:
            self.known.setdefault(key, held)
        return held

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
            word = word.strip('.,;:!?"«»()')
            word = re.sub(rf"\A{_A}+|{_A}+\Z", "", word)
            if sum(c.isalpha() for c in word) < NAME_MIN or TOKEN_RE.search(word):
                continue
            key = _name_key(word)
            if key not in seen:
                seen.add(key)
                words.append(word)
    return words


def _literal(spelling: str) -> str:
    """A spelling as a pattern: every apostrophe any variant or none at all
    («Oʻtkir» is typed «O'tkir», «O’tkir» and «Otkir»), е and ё alike."""
    parts = []
    for char in spelling:
        if char == "'":
            parts.append(f"{_A}?")
        elif char in "её":
            parts.append("[её]")
        else:
            parts.append(re.escape(char))
    return "".join(parts)


def _alternation(options: Iterable[str]) -> str:
    return "|".join(sorted({re.escape(o) for o in options}, key=len, reverse=True))


_UZ_ENDING: Final = f"(?:{_alternation(JON)})?(?:{_alternation(ALIAS_SUFFIXES)})?"
_RU_ENDING: Final = _alternation(RU_NAME_ENDINGS)


def _forms(word: str, *, from_prior: bool = False) -> list[tuple[int, str, str]]:
    """The (stem, ending) patterns ``word`` is written as, each with the
    length of its stem as written (for the order of ``_names_pattern``): its
    Latin and Cyrillic spellings with an Uzbek ending or «-jon»; a Cyrillic
    spelling also with a Russian one, the stem losing the vowel a Russian
    ending replaces («Дилноз-а», «Дилноз-ы», «Дилноз-ага»). A word that ends
    in «-jon» is its base with the ending («Ahmad-jon»). One stem for every
    form, so a text that declines a name one way round-trips exactly.

    ``from_prior``: ``word`` is a token's value from ``prior`` — the stem a
    declined form left («Дилноз», «Серге») — so its Cyrillic spelling also
    takes a Russian ending, or a dropped vowel and an Uzbek one, as it is."""
    latin = to_latin(fold_apostrophes(word))
    base = latin.removesuffix("jon")
    if base != latin and sum(c.isalpha() for c in base) >= RU_NAME_STEM_MIN:
        latin = base
    spellings = [latin, transliterate(latin)]
    if _CYRILLIC.search(word):
        lowered = fold_apostrophes(word).lower()
        spellings.append(lowered.removesuffix("жон") if latin == base else lowered)
    forms: list[tuple[int, str, str]] = []
    for spelling in dict.fromkeys(spellings):
        if not _CYRILLIC.search(spelling):
            forms.append((len(spelling), _literal(spelling), _UZ_ENDING))
            continue
        stem, vowel = spelling, ""
        if spelling[-1] in RU_STEM_DROP and len(spelling) > RU_NAME_STEM_MIN:
            stem, vowel = spelling[:-1], spelling[-1]
        endings = f"{_literal(vowel)}{_UZ_ENDING}"
        if sum(c.isalpha() for c in stem) >= RU_NAME_STEM_MIN:
            endings = f"(?:{_RU_ENDING}|{endings})"
        forms.append((len(stem), _literal(stem), endings))
        if from_prior and len(spelling) >= RU_NAME_STEM_MIN:
            drop = "".join(sorted(RU_STEM_DROP))
            endings = f"(?:{_RU_ENDING}|[{drop}]?{_UZ_ENDING})"
            forms.append((len(spelling), _literal(spelling), endings))
    return forms


@functools.lru_cache(maxsize=64)
def _names_pattern(
    words: tuple[str, ...], stems: tuple[str, ...] = ()
) -> tuple[re.Pattern[str], tuple[str, ...]] | None:
    """One pattern for every form of ``words``, and the word each pair of
    groups (``s<n>`` the stem, ``e<n>`` the ending) belongs to. The longest
    stem comes first, so «Aziza» is not read as «Aziz» + «-a». Never inside a
    longer word or a token, nor after an apostrophe (oʻ, gʻ). ``stems`` are
    ``prior``'s name values (``_forms(from_prior=True)``)."""
    alternatives = [(*form, word) for word in words for form in _forms(word)]
    alternatives += [(*form, w) for w in stems for form in _forms(w, from_prior=True)]
    if not alternatives:
        return None
    alternatives.sort(key=lambda a: a[0], reverse=True)  # stable: words in order
    body = "|".join(
        f"(?P<s{n}>{stem})(?P<e{n}>{ending})"
        for n, (_, stem, ending, _) in enumerate(alternatives)
    )
    pattern = re.compile(rf"(?<![\w@<])(?<!{_A})(?:{body})(?!\w|{_A}\w)", re.I)
    return pattern, tuple(word for *_, word in alternatives)


def _mask_names(text: str, words: list[str], tokens: _Tokens, *, stems: list[str]) -> str:
    """Every form of each name word and of each of ``prior``'s name
    ``stems`` (see the module docstring) as a token, its ending kept outside
    it."""
    built = _names_pattern(tuple(words), tuple(stems)) if words or stems else None
    if built is None:
        return text
    pattern, owners = built

    def swap(match: re.Match[str]) -> str:
        for n, word in enumerate(owners):
            stem = match.group(f"s{n}")
            if stem is not None:
                ending = match.group(f"e{n}") or ""
                return tokens.token("NAME", stem, also=(_name_key(word),)) + ending
        return match.group(0)  # unreachable: one alternative matched

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
    stems = _name_words(tokens.names())
    held = {_name_key(stem) for stem in stems}
    words = [w for w in _name_words(names) if _name_key(w) not in held]
    masked = text
    for step in _PASSES:
        masked = step(masked, tokens)
    masked = _mask_names(masked, words, tokens, stems=stems)
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
