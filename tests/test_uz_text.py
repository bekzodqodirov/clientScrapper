"""Owner-facing text is Uzbek Latin with the right apostrophes (07 §1.11).

The owner and the managers read cards, notes, the digest and alerts on a
phone; a typed ``'`` for oʻ, a Russian phrase, a forgotten ``{placeholder}``
or a pseudonym token that never got restored is a bug they see. The rules:

| code | rule |
|---|---|
| okina | oʻ / gʻ use U+02BB — any other apostrophe after o/g fails |
| tutuq | the glottal stop between letters uses U+02BC (maʼlumot, Masʼul) |
| okina_misplaced | U+02BB after a letter other than o/g (taʻminot) |
| cyrillic | no Cyrillic, unless the literal is in RU_ALLOWED |
| pseudonym | no ``<PHONE_n>``/``<TG_n>``/``<IG_n>``/``<NAME_n>``/``<EMAIL_n>`` left |
| placeholder | no ``{name}`` left in rendered output |
| too_long | a Telegram message ≤ 4,096 characters, a button label ≤ 40 |

okina also catches ʼ after o/g and okina_misplaced is its mirror: both are
the same rule as 07's regexes, seen from the other character (``oʼ`` is the
commonest wrong apostrophe on an Android keyboard).

Collected: string literals of the owner-text modules by AST — not
docstrings, not logging-call arguments, not ``re`` patterns, not the
developer's ``raise`` messages (code-facing English) — and the rendered
output of the renderers that exist. A raise message is the owner's text in
config.py (every validator error ends in get_settings()'s one-line
SystemExit) and wherever the exception is a SystemExit. In rendered output a
«…» span is a verbatim evidence quote: its spelling and script are the
source's, so the apostrophe and Cyrillic rules leave it alone — but an
unrestored pseudonym or an unfilled placeholder inside it is still ours, and
those two rules read the whole text. bot/texts.py, services/cards.py,
digest.py and health.py arrive in later tasks; costs.py's alert texts, the
doctor's lines, config.py's error lines and crm_push.py's lead note, 4xx
alert and /push tekshir view reach the owner and the managers today and are
held to the same rules.
"""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from clientscrapper.services import costs, crm_push
from clientscrapper.services.candidates import PersonView
from tests.ast_tools import (
    PACKAGE,
    attr_chain,
    imports,
    modules,
    rel,
    texts,
    tree_of,
)
from tests.fixtures import candidate_views, uz_corpus

OWNER_TEXT_MODULES = (
    "bot/texts.py",
    # T-20: the button labels, the list pages and every handler (their
    # words are bot/texts', a literal left in one is caught here).
    "bot/keyboards.py",
    "bot/pages.py",
    "bot/deliver.py",
    "bot/main.py",
    "bot/handlers/__init__.py",
    "bot/handlers/common.py",
    "bot/handlers/admin.py",
    "bot/handlers/chats.py",
    "bot/handlers/manager.py",
    "bot/handlers/review.py",
    "bot/handlers/sources.py",
    "bot/handlers/status.py",
    "services/cards.py",
    "services/owner.py",
    "services/crm_push.py",
    "services/digest.py",
    "services/health.py",
    # Owner-facing today: 06 §4.2's spend alerts, make doctor's lines,
    # get_settings()'s one-line errors.
    "services/costs.py",
    "tools/doctor.py",
    "config.py",
    # 05 §B.1/06 §4.2: the ERP breaker's alert, told at once by the feed sync.
    "services/erp_feeds.py",
    # 05 §B.3/06 §1.2: the client import's plan, printed by the tool and sent
    # by /import.
    "services/client_import.py",
    "tools/import_clients.py",
    # 05 §B.1 (T-28): make push-test's lines and the synthetic lead's words.
    "tools/push_test.py",
    # 04 §6/§7 (T-19): the card's gate words and the push gate's refusal
    # notes (lead_reviews.note, shown in /lidlar).
    "services/gates.py",
    "services/push_gate.py",
)
# Modules whose raise messages reach the owner (see the docstring).
OWNER_RAISE_MODULES = frozenset({"config.py"})
# Literals allowed to carry Cyrillic: Russian phrases the code matches or
# quotes as data. Empty until a module needs one.
RU_ALLOWED: frozenset[str] = frozenset()

TELEGRAM_MESSAGE_MAX = 4096
BUTTON_LABEL_MAX = 40

_WRONG = "'’‘`´ʹ"
RULES: dict[str, re.Pattern[str]] = {
    "okina": re.compile(rf"[oOgG][{_WRONG}ʼ]"),
    "tutuq": re.compile(rf"(?<=[A-Za-z])(?<![oOgG])[{_WRONG}](?=[A-Za-z])"),
    "okina_misplaced": re.compile(r"(?<=[A-Za-z])(?<![oOgG])ʻ"),
    "cyrillic": re.compile(r"[Ѐ-ӿ]"),
    "pseudonym": re.compile(r"<(?:PHONE|TG|IG|NAME|EMAIL)_\d+>"),
}
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
QUOTE = re.compile(r"«[^»]*»")
# The rules a verbatim quote is exempt from: the source's spelling and script.
QUOTE_EXEMPT = frozenset({"okina", "tutuq", "okina_misplaced", "cyrillic"})
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}


def problems(text: str, *, rendered: bool = False) -> set[str]:
    """The rule codes ``text`` breaks."""
    found = set()
    if len(text) > TELEGRAM_MESSAGE_MAX:
        found.add("too_long")
    unquoted = QUOTE.sub("«»", text) if rendered else text
    for code, rule in RULES.items():
        if code == "cyrillic" and text in RU_ALLOWED:
            continue
        if rule.search(unquoted if code in QUOTE_EXEMPT else text):
            found.add(code)
    if rendered and PLACEHOLDER.search(text):
        found.add("placeholder")
    return found


def button_problems(label: str) -> set[str]:
    found = problems(label, rendered=True)
    if len(label) > BUTTON_LABEL_MAX:
        found.add("too_long")
    return found


def _raises_system_exit(node: ast.Raise) -> bool:
    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    named = isinstance(exc, ast.Name | ast.Attribute)
    return named and attr_chain(exc)[-1] == "SystemExit"


def _code_facing(tree: ast.Module, *, raises_reach_owner: bool = False) -> set[int]:
    """Literals in logging calls, re calls and the developer's raises."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            if not (raises_reach_owner or _raises_system_exit(node)):
                ids.update(id(n) for n in ast.walk(node))
        elif isinstance(node, ast.Call):
            chain = attr_chain(node.func)
            is_log = chain[-1] in LOG_METHODS and chain[0] in {
                "log",
                "logger",
                "logging",
                "_log",
            }
            if is_log or chain[0] == "re":
                ids.update(id(n) for n in ast.walk(node))
    return ids


def owner_literals(
    tree: ast.Module, *, raises_reach_owner: bool = False
) -> list[tuple[int, str]]:
    skip = _code_facing(tree, raises_reach_owner=raises_reach_owner)
    return [(t.node.lineno, t.value) for t in texts(tree) if id(t.node) not in skip]


def _present_owner_modules():
    return [PACKAGE / m for m in OWNER_TEXT_MODULES if (PACKAGE / m).is_file()]


# --- the rules on the corpus ------------------------------------------------------


@pytest.mark.parametrize("text", uz_corpus.GOOD, ids=lambda t: t[:30])
def test_a_good_owner_text_passes_every_rule(text):
    assert problems(text, rendered=True) == set()


@pytest.mark.parametrize(("text", "codes"), uz_corpus.BAD, ids=lambda x: str(x)[:30])
def test_a_bad_owner_text_trips_exactly_its_rule(text, codes):
    assert problems(text, rendered=True) == codes


def test_every_rule_has_a_bad_sample():
    tripped = set().union(*(codes for _, codes in uz_corpus.BAD))
    assert tripped == {*RULES, "placeholder", "too_long"}


def test_button_labels_fit_a_phone():
    assert [b for b in uz_corpus.GOOD_BUTTONS if button_problems(b)] == []
    assert all(button_problems(b) == {"too_long"} for b in uz_corpus.BAD_BUTTONS)


def test_a_quote_is_data_but_only_inside_its_guillemets():
    quoted = "🔎 «Нужен 1 куб, кто привезёт?» — OLX, 20.10.2026"
    assert problems(quoted, rendered=True) == set()
    assert problems("🔎 Нужен 1 куб — OLX", rendered=True) == {"cyrillic"}
    # The source's spelling is data; what we left inside the quote is not.
    assert problems("🔎 «to'g'ri, ma'lumot bering» — OLX", rendered=True) == set()
    assert problems("🔎 «<TG_3> yozdi» — OLX", rendered=True) == {"pseudonym"}
    assert problems("🔎 «{quote}» — OLX", rendered=True) == {"placeholder"}
    # A literal is not rendered output: its placeholders are the template's.
    assert problems("{key} boʻsh — toʻldir: {hint}") == set()


# --- the rules on the tree -----------------------------------------------------------


def test_the_owner_text_modules_present_today_are_scanned():
    present = {rel(p) for p in _present_owner_modules()}
    assert {"services/costs.py", "tools/doctor.py", "config.py"} <= present


@pytest.mark.parametrize("path", _present_owner_modules(), ids=rel)
def test_owner_literals_follow_the_rules(path):
    raises = rel(path) in OWNER_RAISE_MODULES
    bad = {
        f"line {line}: {sorted(found)}"
        for line, text in owner_literals(tree_of(path), raises_reach_owner=raises)
        if (found := problems(text))
    }
    assert bad == set()


def test_the_literal_collector_skips_code_facing_strings():
    source = (
        '"""The caller\'s docstring."""\n'
        "import logging, re\n"
        "log = logging.getLogger(__name__)\n"
        'TEXT = "Toʻxtatildi"\n'
        "def f(x):\n"
        '    log.warning("the caller\'s row", extra={"k": "it\'s"})\n'
        '    if re.search(r"[o\']", x):\n'
        '        raise ValueError("isn\'t a phone")\n'
        '    return f"{x} to\'xtadi"\n'
    )
    assert owner_literals(ast.parse(source)) == [(4, "Toʻxtatildi"), (9, "{} to'xtadi")]
    # A raise message is the owner's where it reaches him: in config.py
    # (raises_reach_owner) and in any SystemExit.
    exits = (
        "def g(v):\n"
        '    raise ValueError(f"noma\'lum vaqt mintaqasi: {v}") from None\n'
        "def h():\n"
        '    raise SystemExit("TELETHON_SESSION bo\'sh")\n'
    )
    assert owner_literals(ast.parse(exits)) == [(4, "TELETHON_SESSION bo'sh")]
    assert owner_literals(ast.parse(exits), raises_reach_owner=True) == [
        (2, "noma'lum vaqt mintaqasi: {}"),
        (4, "TELETHON_SESSION bo'sh"),
    ]


def test_the_spend_alerts_render_clean():
    """costs.py's real renderers, at a cap and at 80 % of every scope."""
    texts_out = [
        costs.cost_cap_text(code, daily_cap=Decimal("10"), monthly_cap=Decimal("250"))
        for code in (costs.CAP_DAILY, costs.CAP_MONTHLY)
    ]
    snap = costs.SpendSnapshot(
        day_usd=Decimal("10.00"),
        month_usd=Decimal("240.00"),
        family_day_usd={f: costs.FAMILY_CAPS[f] for f in costs.FAMILY_CAPS},
    )
    alerts = costs.spend_alerts(
        snap, daily_cap=Decimal("10"), monthly_cap=Decimal("250"), alert_pct=80
    )
    texts_out += [a.text for a in alerts]
    assert {a.key for a in alerts} == {costs.COST_CAP_KEY, costs.SPEND_HIGH_KEY}
    assert [t for t in texts_out if problems(t, rendered=True)] == []


EVIDENCE_PAIR_QUOTE = re.compile(r' — "([^"\n]*)"')


def test_the_lead_note_and_push_texts_render_clean():
    """crm_push.py's real renderers (T-28): the note at every shrink level
    (with and without a decision maker, an opener, a Russian quote — verbatim
    evidence, exempt), the note the ERP keeps, the 4xx alert, and the
    /push tekshir view with each refusal."""
    rich = replace(
        candidate_views.view(),
        person=PersonView("Namuna Akmal", "rahbar", None),
        opener="Assalomu alaykum, Toshkentdagi mato savdongizni koʻrib yozyapman.",
    )
    russian = replace(
        candidate_views.view(),
        signals=(
            candidate_views.sig(
                quote="нужно 5 кубов ткани",
                excerpt="Здравствуйте, нужно 5 кубов ткани из Иу, опт.",
            ),
        ),
    )
    views = [candidate_views.view(), rich, russian]
    texts_out = [
        crm_push.render_note(v, level=level)
        for v in views
        for level in range(len(crm_push.SHRINK_STEPS) + 1)
    ]
    # The evidence pair quotes in ASCII double quotes (05 §B.1: '<url> —
    # "<quote ≤ 100>"'): that span is the same verbatim quote as «…».
    texts_out += [
        EVIDENCE_PAIR_QUOTE.sub(
            r" — «\1»", crm_push.landed_note(crm_push.build_payload(v))
        )
        for v in views
    ]
    texts_out += [crm_push.rejected_text(status) for status in (403, None)]
    checks = [
        crm_push.PushCheck(
            external_id="cs:00000000-0000-0000-0000-000000000002:202610",
            push_log_id=7,
            company="Namuna Tekstil Savdo",
            attempt=1,
            status="sent",
            sent_at=datetime(2026, 10, 27, 11, 0, tzinfo=UTC),
            http_status=200,
            refusal=refusal,
        )
        for refusal in (None, *crm_push.PUSH_CHECK_REFUSED_UZ)
    ]
    texts_out += [crm_push.render_push_check(c) for c in checks]
    assert texts_out and [t for t in texts_out if problems(t, rendered=True)] == []
    # The Russian quote is in the note, inside «…», and nothing else is.
    note = crm_push.render_note(russian)
    assert "«нужно 5 кубов ткани»" in note
    assert problems(QUOTE.sub("«»", note)) == set()


RENDERERS = (
    ("services/cards.py", "render_review", "T-20"),
    ("services/digest.py", "render_digest", "T-35"),
)


def _cards_rendered() -> list[str]:
    """services/cards' real renderers (T-20) over the push tests' views: the
    owner card, its handoff variant, a shared phone, a Russian quote (exempt
    inside «…»), every segment, and the manager card with and without an
    opener."""
    from clientscrapper.services import cards, gates

    company = gates.GateCompany(
        id=candidate_views.view().company_id,
        display_name="Namuna Tekstil Savdo",
        legal_form="MChJ",
    )
    gate = gates.run_gates(company, (), (), None, None, gates.Recent(), NOW_RENDER)
    russian = replace(
        candidate_views.view(),
        signals=(
            candidate_views.sig(
                quote="нужно 5 кубов ткани",
                excerpt="Здравствуйте, нужно 5 кубов ткани из Иу, опт.",
            ),
        ),
    )
    phoneless = replace(candidate_views.view(), phone=None)
    reviews = [
        cards.ReviewView(
            1, v, gate, segment=segment, phone_kind="mobile", shared_phone=True
        )
        for v in (candidate_views.view(), russian)
        for segment in (*cards.SEGMENT_UZ, None)
    ]
    reviews.append(
        cards.ReviewView(2, phoneless, gate, handoff=True, handoff_username="u_x")
    )
    out = [cards.render_review(r).text for r in reviews]
    out += [
        cards.render_manager(cards.ManagerView(3, v, "u_x", segment="B")).text
        for v in (
            phoneless,
            replace(phoneless, opener="Assalomu alaykum, mato savdongizni koʻrdim."),
        )
    ]
    return out


NOW_RENDER = datetime(2026, 10, 27, 11, 0, tzinfo=UTC)


@pytest.mark.parametrize(("module", "function", "task"), RENDERERS)
def test_rendered_cards_notes_and_digest_follow_the_rules(module, function, task):
    path = PACKAGE / module
    if not path.exists():
        pytest.skip(f"{module}::{function} arrives with {task}")
    if function == "render_review":
        rendered = _cards_rendered()
        assert len(rendered) == 2 * (7 + 1) + 3
        assert [t for t in rendered if problems(t, rendered=True)] == []
        return
    # The renderers take views a later task defines; that task feeds its
    # golden fixtures through problems(rendered=True) here.
    pytest.fail(f"{module} exists: add its fixture views to this test ({task})")


def test_every_button_label_fits_and_reads_right():
    """bot/keyboards.KEYBOARD_SAMPLES (T-20): every label Uzbek Latin within
    40 characters — the longest titles the builders cut included."""
    from clientscrapper.bot import keyboards

    labels = [
        button.text
        for build, kwargs in keyboards.KEYBOARD_SAMPLES
        for row in build(**kwargs).inline_keyboard
        for button in row
    ]
    assert labels and [b for b in labels if button_problems(b)] == []


# --- text keys -------------------------------------------------------------------------


def _text_keys(tree: ast.Module, module: str) -> set[str]:
    """Keys used through bot.texts: ``t("key")`` with ``t`` imported from it,
    ``texts.t("key")`` and ``UZ["key"]``."""
    t_names = {
        i.alias
        for i in imports(tree, module=module)
        if i.module == "clientscrapper.bot.texts" and i.name == "t"
    }
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            by_t = (isinstance(func, ast.Name) and func.id in t_names) or (
                isinstance(func, ast.Attribute)
                and attr_chain(func)[-2:] == ["texts", "t"]
            )
            first = node.args[0]
            if by_t and isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
        if isinstance(node, ast.Subscript):
            target = node.value
            named_uz = (isinstance(target, ast.Name) and target.id == "UZ") or (
                isinstance(target, ast.Attribute) and target.attr == "UZ"
            )
            key = node.slice
            if named_uz and isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
    return keys


def test_every_text_key_used_is_defined():
    used = {}
    for path in modules():
        module = ".".join(path.relative_to(PACKAGE.parent).with_suffix("").parts)
        for key in _text_keys(tree_of(path), module):
            used.setdefault(key, rel(path))
    if not (PACKAGE / "bot" / "texts.py").exists():
        assert used == {}, "a text key is used but bot/texts.py does not exist"
        pytest.skip("bot/texts.py (the UZ dict) arrives with T-20")
    from clientscrapper.bot import texts as bot_texts  # type: ignore[attr-defined]

    assert {k: m for k, m in used.items() if k not in bot_texts.UZ} == {}
    assert [k for k, v in bot_texts.UZ.items() if problems(v)] == []


def test_the_key_collector_sees_each_shape():
    source = (
        "from clientscrapper.bot.texts import t, UZ\n"
        "from clientscrapper.bot import texts\n"
        "a = t('lidlar.empty')\n"
        "b = texts.t('holat.title')\n"
        "c = UZ['yordam']\n"
        "d = other.t('not_ours')\n"
    )
    keys = _text_keys(ast.parse(source), "clientscrapper.bot.handlers.review")
    assert keys == {"lidlar.empty", "holat.title", "yordam"}
