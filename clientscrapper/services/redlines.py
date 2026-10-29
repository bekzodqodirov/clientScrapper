# VENDORED_FROM dailyJournal@6b1f67f miya/jarvis/redlines.py
"""The redline gate on a first message to a prospect (04 §4.5). Pure.

A descendant of MIYA's ``check_outbound``, cut to what an opener needs: a
human manager sends it from their own account (the agent sends nothing,
06 §1.7), so the gate only decides whether 04's draft may stand on the card
and in the ERP note, or whether the playbook template takes its place
(``services/playbook``, which passes this gate by construction).

``check_opener(text, lang=…)`` reads the text twice, as MIYA does:

* the **surface** view — NFKC, apostrophes folded, case-folded, Cyrillic in
  Latin (``text.to_latin``): digits, currency signs, percent signs, links;
* the **word** view — ``text.normalise_for_search``: the hook words.

Every lexicon below is written as people write it, in Uzbek Latin and in
Cyrillic, and taken to the surface view at import (Uzbek Cyrillic and
Russian land on the same Latin). The flags (04 §4.5):

| flag | the text has |
|---|---|
| ``digit`` | a digit of any script outside ``allowed_tokens`` («2018») |
| ``money`` | a price word, a currency, a percent sign |
| ``time`` | a day count, a duration, a speed («kun», «hafta», «soat», «срок», «tez») |
| ``promise`` | a guarantee («kafolat», «гарант», «albatta», «eng arzon», «bojsiz») |
| ``legal`` | STIR/ИНН, a declaration, customs, tax |
| ``link`` | a URL, a t.me link, an @handle, «www.» |
| ``competitor`` | a forwarder's word or name (``COMPETITOR_NAME_RE``, the seeds' names) |
| ``optout_missing`` | not ending with the opt-out sentence of its language |
| ``language`` | not Uzbek (Latin, Cyrillic) or Russian, or not ``lang`` |
| ``hook_missing`` | none of the goods words nor the source's name |
| ``pseudonym`` | a pseudonym token left in it (``<NAME_1>``, ``<PHONE_1>``, ``<ID>``) |

**The opt-out sentence** is the playbook's (``docs/sotuv-playbook.md``
§2.6, the line the manager card prints, 06 §1.7), spelled with ʻ per 04
Q9 (a); the gate folds apostrophes and drops quote marks, so «bo'lmasa» or a
«yo'q» in guillemets still matches — and the Uzbek Cyrillic sentence lands
on the Latin one.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from clientscrapper.collectors.keywords import COMPETITOR_NAME_RE
from clientscrapper.services.pseudonyms import TOKEN_TRACE_RE
from clientscrapper.services.text import fold_apostrophes, normalise_for_search, to_latin

Lang = Literal["uz", "ru"]
Script = Literal["latin", "cyrillic", "russian"]
Flag = Literal[
    "digit",
    "money",
    "time",
    "promise",
    "legal",
    "link",
    "competitor",
    "optout_missing",
    "language",
    "hook_missing",
    "pseudonym",
]
# The order flags are reported in.
FLAGS: Final[tuple[Flag, ...]] = (
    "digit",
    "money",
    "time",
    "promise",
    "legal",
    "link",
    "competitor",
    "optout_missing",
    "language",
    "hook_missing",
    "pseudonym",
)
# 04 §4.5: the one digit run an opener may carry (GSR works «2018-yildan»).
ALLOWED_TOKENS: Final = ("2018",)
# Our own name: «GSR Logistics» names us, not a forwarder.
OWN_NAMES: Final = ("GSR Logistics", "GSR")

# The playbook's opt-out line (§2.6), spelled with ʻ (04 Q9 a). The manager
# card prints the Uzbek Latin one (services/cards.NO_PRICE_UZ).
OPTOUT_UZ: Final = 'Agar qiziq boʻlmasa, "yoʻq" deb yozing — boshqa yozmayman.'
OPTOUT_UZ_CYRL: Final = "Агар қизиқ бўлмаса, «йўқ» деб ёзинг — бошқа ёзмайман."
OPTOUT_RU: Final = "Если неактуально — напишите «нет», больше не побеспокою."
OPTOUT: Final[Mapping[Lang, tuple[str, ...]]] = {
    "uz": (OPTOUT_UZ, OPTOUT_UZ_CYRL),
    "ru": (OPTOUT_RU,),
}


@dataclass(frozen=True, slots=True)
class GuardResult:
    ok: bool
    flags: tuple[Flag, ...] = ()


# --- the two views ---------------------------------------------------------------

_FORMAT_CHARS: Final = frozenset({"Cf"})
_QUOTES: Final = str.maketrans({c: "" for c in '"«»“”„‟‹›'})
_DASHES: Final = str.maketrans({c: "-" for c in "‐‑‒–—―−"})
_SPACES: Final = re.compile(r"\s+")


def surface(text: str) -> str:
    """NFKC, format characters stripped, apostrophes folded, case-folded,
    Cyrillic in Latin — every symbol kept."""
    normal = unicodedata.normalize("NFKC", text or "")
    kept = "".join(ch for ch in normal if unicodedata.category(ch) not in _FORMAT_CHARS)
    return to_latin(fold_apostrophes(kept).casefold())


def _sentence_key(text: str) -> str:
    """The surface view with quote marks dropped, dashes as «-» and
    whitespace collapsed: how two spellings of one sentence are compared."""
    folded = surface(text).translate(_QUOTES).translate(_DASHES)
    return _SPACES.sub(" ", folded).strip()


# --- the lexicons (written in either script, compiled in the surface view) ------

# A word boundary in the surface view: an Uzbek apostrophe is a letter.
_B: Final = r"(?<![a-z0-9'])"
_E: Final = r"(?![a-z0-9'])"


def _lexicon(*patterns: str) -> re.Pattern[str]:
    """One pattern over the surface view; each alternative is taken there
    (``to_latin`` — every escape here is lower-case, which it keeps)."""
    return re.compile("|".join(f"(?:{to_latin(p)})" for p in patterns))


MONEY_RE: Final = _lexicon(
    r"\$",
    "%",
    "€",
    "₽",
    "¥",
    rf"{_B}usd{_E}",
    rf"{_B}dollar",
    rf"{_B}доллар",
    rf"{_B}so'?m(?:ga|dan|lik|lar)?{_E}",
    rf"{_B}сум(?:ов|ах|ами|ы)?{_E}",
    rf"{_B}[yu]\.\s?e\.?(?![a-z])",
    rf"{_B}у\.\s?е\.?",
    rf"{_B}narx",
    rf"{_B}нарх",
    rf"{_B}цен(?:а|ы|у|е|ой|ам|ами|ах){_E}",
    rf"{_B}стоимост",
    rf"{_B}tarif",
    rf"{_B}тариф",
    rf"{_B}chegirm",
    rf"{_B}чегирм",
    rf"{_B}скидк",
    rf"{_B}pr[ae][yi]s",
    rf"{_B}прайс",
    rf"{_B}bepul",
    rf"{_B}бесплатн",
)
TIME_RE: Final = _lexicon(
    rf"{_B}kun",
    rf"{_B}кун",
    rf"{_B}hafta",
    rf"{_B}ҳафта",
    rf"{_B}soat",
    rf"{_B}соат",
    rf"{_B}день{_E}",
    rf"{_B}дн(?:я|ей|ю|ям|ями|ях){_E}",
    rf"{_B}недел",
    rf"{_B}час(?:а|ов|у|ам|ами){_E}",
    rf"{_B}tez",
    rf"{_B}тез",
    rf"{_B}быстр",
    rf"{_B}срок",
    rf"{_B}muddat",
    rf"{_B}муддат",
    rf"{_B}ertaga",
    rf"{_B}эртага",
    rf"{_B}bugun",
    rf"{_B}бугун",
    rf"{_B}завтра",
    rf"{_B}сегодня",
)
PROMISE_RE: Final = _lexicon(
    rf"{_B}kafolat",
    rf"{_B}кафолат",
    rf"{_B}гарант",
    rf"{_B}garant",
    r"(?<![0-9])100(?![0-9])",
    rf"{_B}albatta",
    rf"{_B}албатта",
    rf"{_B}обязательно",
    rf"{_B}eng\s+arzon",
    rf"{_B}энг\s+арзон",
    rf"{_B}eng\s+tez",
    rf"{_B}сам(?:ый|ые|ая|ую|ое)\s+(?:деш(?:е|ё)в|быстр)",
    rf"{_B}bojsiz",
    rf"{_B}божсиз",
    rf"{_B}без\s+пошлин",
    rf"{_B}muammosiz",
    rf"{_B}муаммосиз",
    rf"{_B}без\s+проблем",
)
LEGAL_RE: Final = _lexicon(
    rf"{_B}stir",
    rf"{_B}стир{_E}",
    rf"{_B}инн{_E}",
    rf"{_B}deklar",
    rf"{_B}деклар",
    rf"{_B}bojxona",
    rf"{_B}божхона",
    rf"{_B}таможн",
    rf"{_B}rastamoj",
    rf"{_B}растамож",
    rf"{_B}soliq",
    rf"{_B}солиқ",
    rf"{_B}налог",
    rf"{_B}gtd{_E}",
    rf"{_B}гтд{_E}",
    rf"{_B}tn\s?ved{_E}",
    rf"{_B}тн\s?вэд{_E}",
)
LINK_RE: Final = re.compile(r"https?://|t\.me/|@[a-z0-9_]{5,}|www\.")
_OWN_RE: Final = re.compile(
    "|".join(rf"(?<!\w){re.escape(n)}(?!\w)" for n in OWN_NAMES), re.I
)


def _digits(text: str, allowed: Sequence[str]) -> bool:
    for token in allowed:
        text = re.sub(rf"(?<!\d){re.escape(token)}(?!\d)", " ", text)
    return any(ch.isdigit() for ch in text)


def _competitor(text: str, names: Iterable[str]) -> bool:
    bare = _OWN_RE.sub(" ", text)
    if COMPETITOR_NAME_RE.search(bare):
        return True
    view = f" {_sentence_key(bare)} "
    for name in names:
        key = _sentence_key(name)
        if len(key) >= 3 and re.search(rf"{_B}{re.escape(key)}{_E}", view):
            return True
    return False


def ends_with_optout(text: str, lang: Lang) -> bool:
    """Does ``text`` end with the opt-out sentence of ``lang`` (any
    apostrophe, quote marks and dash)?"""
    body = _sentence_key(text).rstrip(" .!")
    return any(body.endswith(_sentence_key(s).rstrip(" .!")) for s in OPTOUT[lang])


# --- the language ----------------------------------------------------------------

# Letters only Uzbek Cyrillic has, and the Cyrillic of Russian and Uzbek: any
# other Cyrillic letter («і», «ї», «є», «ә», «ө», «ү», «ң») writes another
# language (MIYA).
_UZ_CYRILLIC: Final = frozenset("ўқғҳЎҚҒҲ")
_OWN_CYRILLIC: Final = frozenset("абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ")
_CYRILLIC_RANGE: Final = ("Ѐ", "ӿ")
# MIYA's English allowlist, cut to the words an opener would carry.
_EN_WORDS: Final = frozenset(
    """
    a about after all also an and any are as at be because been best but by can
    cargo cheap china come contact could day days deal delivery do does fast for
    free from get good great have hello hi how if in is it its just know let like
    look make many me more most need new next not now of offer on one only or our
    out over price products quality question reply send service ship shipping so
    some than thank thanks that the their them then there these they this time to
    today tomorrow truck us very want was we week what when where which who why will
    with would write you your
    """.split()
)
_NOT_EN_WORDS: Final = frozenset({"a", "i", "men", "no", "ok", "to", "va", "bu"})
_EN_SHAPE: Final = re.compile(r"th|wh|ph|ck|w|c(?!h)|tion$")
# Names and brands an opener writes: their spelling is not English.
_NOT_EN_NAMES: Final = (
    "gsr",
    "logistics",
    "yiwu",
    "guangzhou",
    "wechat",
    "whatsapp",
    "instagram",
    "telegram",
    "olx",
    "uzum",
    "alibaba",
    "taobao",
    "huawei",
)
_WORD: Final = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")


def script_of(text: str) -> Script | None:
    """Latin, Uzbek Cyrillic or Russian: what most of the letters are
    written in. None when there are no letters."""
    letters = [ch for ch in text if ch.isalpha()]
    latin = sum(1 for ch in letters if ch.isascii())
    cyrillic = sum(1 for ch in letters if _CYRILLIC_RANGE[0] <= ch <= _CYRILLIC_RANGE[1])
    if not latin and not cyrillic:
        return None
    if latin >= cyrillic:
        return "latin"
    return "cyrillic" if any(ch in _UZ_CYRILLIC for ch in letters) else "russian"


def _english_word(word: str) -> bool:
    if not word.isascii() or word in _NOT_EN_WORDS or word.startswith(_NOT_EN_NAMES):
        return False
    bare = word.replace("'", "")
    if bare in _EN_WORDS:
        return True
    return "'" not in word and _EN_SHAPE.search(bare) is not None


def language_ok(text: str, lang: Lang) -> bool:
    """Written in the language ``lang`` names: Uzbek in Latin or Cyrillic, or
    Russian; not mainly English (half its words or more), and no Cyrillic
    letter of another alphabet."""
    folded = fold_apostrophes(text).casefold()
    if any(
        _CYRILLIC_RANGE[0] <= ch <= _CYRILLIC_RANGE[1]
        and ch.isalpha()
        and ch not in _OWN_CYRILLIC
        for ch in folded
    ):
        return False
    script = script_of(text)
    wanted: tuple[Script, ...] = ("latin", "cyrillic") if lang == "uz" else ("russian",)
    if script not in wanted:
        return False
    words = _WORD.findall(folded)
    english = sum(1 for w in words if _english_word(w))
    return not (english and english * 2 >= len(words))


# --- the gate ---------------------------------------------------------------------


def _hooked(text: str, hooks: Iterable[str]) -> bool:
    words = normalise_for_search(text).split()
    for hook in hooks:
        for part in normalise_for_search(hook).split():
            stem = part[: max(4, len(part) - 2)] if len(part) > 4 else part
            if len(stem) >= 2 and any(w.startswith(stem) for w in words):
                return True
    return False


def check_opener(
    text: str,
    *,
    lang: Lang,
    allowed_tokens: Sequence[str] = ALLOWED_TOKENS,
    hooks: Sequence[str] = (),
    competitors: Iterable[str] = (),
) -> GuardResult:
    """04 §4.5's gate: every flag ``text`` raises, in ``FLAGS`` order (the
    module docstring). ``hooks``: the goods words and the source's name —
    none of them in the text is ``hook_missing`` (no hooks given, no check);
    ``competitors``: the seeds' forwarder names, beside
    ``COMPETITOR_NAME_RE``."""
    view = surface(text)
    found: set[Flag] = set()
    if _digits(view, allowed_tokens):
        found.add("digit")
    if MONEY_RE.search(view):
        found.add("money")
    if TIME_RE.search(view):
        found.add("time")
    if PROMISE_RE.search(view):
        found.add("promise")
    if LEGAL_RE.search(view):
        found.add("legal")
    if LINK_RE.search(view):
        found.add("link")
    if _competitor(text, competitors):
        found.add("competitor")
    if not ends_with_optout(text, lang):
        found.add("optout_missing")
    if not language_ok(text, lang):
        found.add("language")
    if hooks and not _hooked(text, hooks):
        found.add("hook_missing")
    if TOKEN_TRACE_RE.search(text):
        found.add("pseudonym")
    flags = tuple(f for f in FLAGS if f in found)
    return GuardResult(ok=not flags, flags=flags)


def trips(value: str) -> bool:
    """Would ``value`` alone raise a content flag — a digit, money, time, a
    promise, a legal word, a link, a forwarder's word or a pseudonym? What a
    template may not be filled with (``services/playbook``)."""
    view = surface(value)
    return bool(
        _digits(view, ())
        or MONEY_RE.search(view)
        or TIME_RE.search(view)
        or PROMISE_RE.search(view)
        or LEGAL_RE.search(view)
        or LINK_RE.search(view)
        or _competitor(value, ())
        or TOKEN_TRACE_RE.search(value)
    )


__all__ = [
    "ALLOWED_TOKENS",
    "FLAGS",
    "OPTOUT",
    "OPTOUT_RU",
    "OPTOUT_UZ",
    "OPTOUT_UZ_CYRL",
    "Flag",
    "GuardResult",
    "Lang",
    "check_opener",
    "ends_with_optout",
    "language_ok",
    "script_of",
    "surface",
    "trips",
]
