# VENDORED_FROM dailyJournal@6b1f67f miya/services/text.py
"""Text normalisation shared by the keys, the normaliser and the collectors.

One table for the apostrophes people type for oʻ / gʻ, so a company name in
a directory, a post in a chat and a transliterated name all fold the same way.
Extend it here and nowhere else.

Copied from MIYA as-is (docs/kod-audit-miya.md §1) with four changes: type
annotations for the strict mypy scope; ``normalise_for_search`` no longer
joins MIYA's GS client codes (``services/codes.py`` is not ported, 02 §7);
``APOSTROPHE_CLASS``, the apostrophe table as a regex class for the
keyword patterns that run on unfolded text; and ``storable``, the one rule
for what a ``text``/``jsonb`` column accepts (the evidence store and the
normaliser ask it).
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterable

# Every apostrophe-like character an Uzbek keyboard or phone produces for the
# tutuq belgisi, folded to the ASCII one:
#   ʻ U+02BB modifier letter turned comma   (the official Uzbek Latin form)
#   ʼ U+02BC modifier letter apostrophe     (most Android keyboards)
#   ‘ U+2018 left single quotation mark     (smart quotes)
#   ’ U+2019 right single quotation mark    (smart quotes, iOS)
#   ` U+0060 grave accent                   (typed on a Latin layout)
#   ʹ U+02B9 modifier letter prime          (some transliteration keyboards)
APOSTROPHES = str.maketrans(
    {
        "ʻ": "'",
        "ʼ": "'",
        "‘": "'",
        "’": "'",
        "`": "'",
        "ʹ": "'",
    }
)

# The same set as a regex character class, for patterns that run on text as
# written (collectors/keywords.py). Derived, so the two cannot drift.
APOSTROPHE_CLASS = "[" + "".join(sorted({"'", *map(chr, APOSTROPHES)})) + "]"


def fold_apostrophes(text: str) -> str:
    """Pure: the same text with every apostrophe variant as ASCII ``'``."""
    return text.translate(APOSTROPHES)


# --- What the database can store ----------------------------------------------

_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def storable(text: str) -> str:
    """``text`` as Postgres ``text`` and ``jsonb`` accept it: NUL characters
    removed (a page with binary junk in it, ``\u0000`` in an API answer) and
    lone surrogates (half an emoji cut by a byte-wise clip, a ``\ud83d`` an
    API escaped alone) replaced by U+FFFD. Anything else is kept as written."""
    if "\x00" in text:
        text = text.replace("\x00", "")
    return _LONE_SURROGATE.sub("\ufffd", text)


# --- Latin → Cyrillic, for matching an alias in either script ----------------
#
# Uzbek Latin → Cyrillic, longest digraphs first. Enough to spell a name the
# way the other script would: "Bekzod aka" → "бекзод ака", "G'ani" → "ғани".
# Everything is lower-cased first; the match itself ignores case.
_LATIN_TO_CYRILLIC = (
    ("sh", "ш"),
    ("ch", "ч"),
    ("ng", "нг"),
    ("yo", "ё"),
    ("yu", "ю"),
    ("ya", "я"),
    ("ye", "е"),
    ("ts", "ц"),
    ("o'", "ў"),
    ("g'", "ғ"),
    ("a", "а"),
    ("b", "б"),
    ("c", "к"),
    ("d", "д"),
    ("e", "е"),
    ("f", "ф"),
    ("g", "г"),
    ("h", "ҳ"),
    ("i", "и"),
    ("j", "ж"),
    ("k", "к"),
    ("l", "л"),
    ("m", "м"),
    ("n", "н"),
    ("o", "о"),
    ("p", "п"),
    ("q", "қ"),
    ("r", "р"),
    ("s", "с"),
    ("t", "т"),
    ("u", "у"),
    ("v", "в"),
    ("w", "в"),
    ("x", "х"),
    ("y", "й"),
    ("z", "з"),
    ("'", "ъ"),
)


def transliterate(latin: str) -> str:
    """A Latin-script Uzbek word in Cyrillic, lower-cased. Cyrillic input is
    returned unchanged (nothing in the table matches it)."""
    text = fold_apostrophes(latin.lower())
    out: list[str] = []
    i = 0
    while i < len(text):
        for src, dst in _LATIN_TO_CYRILLIC:
            if text.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


# Case endings an alias may carry and still be the same name: "Bekzodga",
# "Bekzod akaning", "Бекзодга". Deliberately not lar / jon / m — "Begalar",
# "Bekzodjon" and "Begim" are other people or other words.
ALIAS_SUFFIXES = (
    "ga",
    "ka",
    "qa",
    "ni",
    "ning",
    "niki",
    "da",
    "dan",
    "га",
    "ка",
    "қа",
    "ни",
    "нинг",
    "ники",
    "да",
    "дан",
)


@functools.lru_cache(maxsize=8)
def alias_pattern(aliases: tuple[str, ...]) -> re.Pattern[str] | None:
    """One regex that matches any alias as a whole word, in either script.

    Names match joined, spaced or hyphenated ("Bekzod aka", "bekzodaka",
    "Bekzod-aka", "Бекзодака") and with an Uzbek case ending ("Bekzodga");
    never inside a longer word — "Begalar" and "Bekzodjon" stay out. An
    ``@username`` matches only as itself, never inside an e-mail address.
    Cached per alias tuple, so a test that changes the setting gets a
    fresh one.
    """
    names: list[str] = []
    handles: list[str] = []
    for raw in aliases:
        alias = fold_apostrophes(raw.strip())
        if not alias:
            continue
        if alias.startswith("@"):
            if alias.lower() not in (h.lower() for h in handles):
                handles.append(alias)
            continue
        for spelling in (alias, transliterate(alias)):
            if spelling and spelling.lower() not in (n.lower() for n in names):
                names.append(spelling)
    alternatives: list[str] = []
    if names:
        words = "|".join(
            r"[\s\-]*".join(re.escape(part) for part in re.split(r"[\s\-]+", n))
            for n in names
        )
        suffixes = "|".join(sorted(ALIAS_SUFFIXES, key=len, reverse=True))
        alternatives.append(rf"(?<![\w'@])(?:{words})(?:{suffixes})?(?![\w'])")
    if handles:
        words = "|".join(re.escape(h) for h in handles)
        alternatives.append(rf"(?<![\w'@])(?:{words})(?![\w])")
    if not alternatives:
        return None
    return re.compile("|".join(alternatives), re.IGNORECASE)


# --- Cyrillic → Latin (Uzbek and Russian), for comparing and searching -------
#
# One table (WP-28). Everything is lower-cased first; longest sequences first;
# anything that is not Cyrillic passes through.
CYR_TO_LAT: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "ғ": "g'",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "j",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "қ": "q",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "ў": "o'",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "x",
    "ҳ": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sh",
    "ъ": "'",
    "ь": "",
    "ы": "i",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def to_latin(text: str) -> str:
    """Lower-cased text with every Cyrillic letter in Uzbek Latin."""
    return "".join(CYR_TO_LAT.get(ch, ch) for ch in text.lower())


_NOT_TOKEN = re.compile(r"[^a-z0-9-]+")
_LOOSE_HYPHEN = re.compile(r"(?<![a-z0-9])-+|-+(?![a-z0-9])")


def normalise_for_search(text: str) -> str:
    """The form both a question and a stored text are compared in.

    NFKC, apostrophes folded, casefold, Cyrillic to Latin; then every
    apostrophe dropped ("bo'ldi" = "boldi"), q→k and w→v in words,
    everything that is not a letter, digit or an inner hyphen turned into a
    space. Hyphenated codes survive: "yw26-004715".
    """
    text = to_latin(
        fold_apostrophes(unicodedata.normalize("NFKC", text or "")).casefold()
    )
    text = text.replace("'", "")
    text = _NOT_TOKEN.sub(" ", text)
    text = _LOOSE_HYPHEN.sub(" ", text)
    # q→k and w→v fold words, never codes: "YW26-004715" stays a waybill.
    return " ".join(
        token if any(ch.isdigit() for ch in token) else token.translate(_LETTER_FOLD)
        for token in text.split()
    )


_LETTER_FOLD = str.maketrans({"q": "k", "w": "v"})


# Written as people type them; folded like the text they are compared with
# ("haqida" is matched as "hakida").
_STOPWORDS_RAW = """
    nima nimaga deb oylaysan oylaysiz oyla fikring fikringiz bolgandi bolgan boldi
    edi ekan bilan haqida uchun va ham bu shu u men sen siz menga senga kanday
    kachon kayerda kim kimga masala masalasi narsa shunday yana endi bor yok mi chi
    ku iltimos ayt aytib ber topib kidirib eslaysanmi esingdami degan degandi dedi
    aytgandi gapirgan gapirgandi keyin undan song keyinchi keyinchalik
    chto kak ty dumaesh pro o ob s i v na po eto byl byla bylo mne on ona
    what do you think about the
    """.split()
STOPWORDS: frozenset[str] = frozenset(
    w.replace("'", "").translate(_LETTER_FOLD) for w in _STOPWORDS_RAW
)

UZ_SUFFIXES = (
    "larining",
    "laridan",
    "lariga",
    "larini",
    "larda",
    "lardan",
    "larga",
    "larni",
    "lari",
    "lar",
    "ning",
    "dagi",
    "dan",
    "ga",
    "da",
    "ni",
    "mi",
    "dir",
    "gandi",
    "ganda",
    "gan",
    "yapti",
    "moqda",
    "si",
)
RU_ENDINGS = (
    "ami",
    "yami",
    "ogo",
    "ego",
    "omu",
    "emu",
    "oy",
    "ey",
    "om",
    "em",
    "ax",
    "yax",
    "uyu",
    "aya",
    "oe",
    "ee",
    "ye",
    "ie",
    "a",
    "u",
    "y",
    "i",
    "e",
    "o",
    "ya",
    "yu",
)
_MIN_STEM = 4


def _strip_one(token: str, suffixes: tuple[str, ...]) -> str | None:
    for suffix in sorted(suffixes, key=len, reverse=True):
        if token.endswith(suffix) and len(token) - len(suffix) >= _MIN_STEM:
            return token[: -len(suffix)]
    return None


def stem_query_token(token: str) -> str:
    """Up to two Uzbek suffixes; only when none matched, one Russian ending.
    Never below four characters. Query tokens only: documents are matched
    by prefix, unstemmed."""
    stripped = False
    for _ in range(2):
        shorter = _strip_one(token, UZ_SUFFIXES)
        if shorter is None:
            break
        token, stripped = shorter, True
    if not stripped:
        token = _strip_one(token, RU_ENDINGS) or token
    return token


def query_terms(text: str) -> list[str]:
    """The words of a question worth searching for, normalised and stemmed."""
    terms: list[str] = []
    for token in normalise_for_search(text).split():
        if len(token) < 2 or token in STOPWORDS:
            continue
        stem = stem_query_token(token)
        if stem not in terms:
            terms.append(stem)
    return terms


_SAFE_TERM = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def to_prefix_tsquery(terms: list[str]) -> str | None:
    """ "'konteyner':* | 'yw26-004715':*" — for to_tsquery('simple', :q) as a
    bind parameter. Anything but letters, digits and inner hyphens is dropped."""
    safe = [f"'{t}':*" for t in terms if _SAFE_TERM.match(t)]
    return " | ".join(safe) or None


def name_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """Any of ``names`` as a whole word, in either script, any case — to be
    run on ``normalise_for_search`` text."""
    forms = [normalise_for_search(n) for n in names]
    forms = [f for f in dict.fromkeys(forms) if f]
    if not forms:
        return None
    words = (r"\s+".join(re.escape(p) for p in f.split()) for f in forms)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(words) + r")(?![a-z0-9])")
