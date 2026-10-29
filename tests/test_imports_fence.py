"""The Anthropic SDK is imported only where a model call is made (06 §5.2).

The package-wide half of the model fence 07 §1.2 points to: ``anthropic`` is
imported only by ``services/extraction.py`` and ``services/model_jobs.py``
(the two lanes that take a ``Pseudonymised`` payload and pay through
``costs.ask``) and ``services/costs.py`` (06 §5.2's list). Imported anywhere
else, it is a model call that nobody masks and nobody counts —
tests/test_paid_calls_via_costs.py then holds the two lanes' calls behind
``ask``.

The Telethon half of 06's fence lives in
``tests/test_collector_imports.py::test_telethon_is_imported_only_by_the_reader``
(07 §1.2 gives it that home); one rule, one test.

T-15 extends the file (07 §4.1: not a new one) with the masking half of 06
§5.2: the personal-data patterns have one home, ``collectors/keywords.py``
— ``services/pseudonyms.py`` imports ``PHONE_RE``, ``TG_RE``, ``IG_RE``,
``EMAIL_RE`` and the 14-digit ``PINFL_RE`` from there, and no module of the
package binds one of those names or compiles one of those patterns anew —
and a model lane that imports the SDK checks every request with
``pseudonyms.assert_masked``.
"""

from __future__ import annotations

import ast

from tests.ast_tools import (
    PACKAGE,
    dotted_name,
    imported_modules,
    imports,
    is_under,
    modules,
    references,
    rel,
    strings,
    tree_of,
)

ANTHROPIC_HOMES = {
    "services/extraction.py",
    "services/model_jobs.py",
    "services/costs.py",
}


def anthropic_offences(path_rel: str, tree: ast.Module, module: str) -> list[str]:
    if path_rel in ANTHROPIC_HOMES:
        return []
    return [
        f"imports {name}"
        for name in sorted(imported_modules(tree, module=module))
        if is_under(name, ["anthropic"])
    ]


def test_anthropic_is_imported_only_by_the_model_lanes():
    offending = {
        rel(p): found
        for p in modules()
        if (found := anthropic_offences(rel(p), tree_of(p), dotted_name(p)))
    }
    assert offending == {}, (
        "the Anthropic SDK belongs to services/extraction.py and "
        f"services/model_jobs.py (and costs.py), 06 §5.2: {offending}"
    )


def test_the_fence_catches_each_shape():
    def found(path: str, source: str) -> list[str]:
        module = "clientscrapper." + path.removesuffix(".py").replace("/", ".")
        return anthropic_offences(path, ast.parse(source), module)

    assert found("services/enrich.py", "import anthropic\n") == ["imports anthropic"]
    assert found("worker/main.py", "from anthropic import AsyncAnthropic as A\n") == [
        "imports anthropic",
        "imports anthropic.AsyncAnthropic",
    ]
    assert found("bot/main.py", "import anthropic.types\n") == ["imports anthropic.types"]
    dynamic = "import importlib\nm = importlib.import_module('anthropic')\n"
    assert found("services/registry.py", dynamic) == ["imports anthropic"]
    assert found("services/extraction.py", "import anthropic\n") == []
    assert found("services/model_jobs.py", "from anthropic import AsyncAnthropic\n") == []


# --- the masking half (T-15) ---------------------------------------------------

KEYWORDS = "clientscrapper.collectors.keywords"
KEYWORDS_HOME = "collectors/keywords.py"
# 06 §5.2's four, the PINFL run beside them (keywords.PINFL_RE), and the
# shapes the E2E review found reaching the model raw: a foreign number, a long
# digit run, a messenger id after its label (one home since 2026-10-29; the
# push note's quote filter and the scrubber kept copies).
PII_PATTERNS = (
    "PHONE_RE",
    "TG_RE",
    "IG_RE",
    "EMAIL_RE",
    "PINFL_RE",
    "INTL_PHONE_RE",
    "LONG_NUMBER_RE",
    "MESSENGER_ID_RE",
)
# What the pseudonymiser reads the shapes through (a pattern or its function;
# PHONE_RE through normalise.phone_matches, the labelled-STIR rule).
PSEUDONYMISER_NEEDS = (
    "TG_RE",
    "IG_RE",
    "EMAIL_RE",
    "PINFL_RE",
    "MESSENGER_ID_RE",
    "intl_phones",
    "long_numbers",
)
PSEUDONYMS = "services/pseudonyms.py"
# The SDK's homes that send requests (costs.py only prices them).
MASKING_LANES = ANTHROPIC_HOMES - {"services/costs.py"}


def _pattern_sources() -> dict[str, str]:
    from clientscrapper.collectors import keywords

    return {name: getattr(keywords, name).pattern for name in PII_PATTERNS}


def pattern_offences(path_rel: str, tree: ast.Module, module: str) -> list[str]:
    """A module other than keywords.py that binds one of the four names, or
    holds one of the four patterns' sources as a literal (a copy)."""
    if path_rel == KEYWORDS_HOME:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        found += [
            f"binds {t.id}"
            for t in targets
            if isinstance(t, ast.Name) and t.id in PII_PATTERNS
        ]
    sources = _pattern_sources()
    for literal in strings(tree):
        found += [
            f"copies {name}"
            for name, source in sources.items()
            if isinstance(literal.value, str) and literal.value == source
        ]
    return sorted(found)


def test_the_four_patterns_have_one_home():
    offending = {
        rel(p): found
        for p in modules()
        if (found := pattern_offences(rel(p), tree_of(p), dotted_name(p)))
    }
    assert (
        offending == {}
    ), f"PHONE_RE/TG_RE/IG_RE/EMAIL_RE live in keywords.py: {offending}"


def test_the_pseudonymiser_imports_the_four_from_keywords():
    path = PACKAGE / PSEUDONYMS
    tree = tree_of(path)
    imported = {
        imp.name
        for imp in imports(tree, module=dotted_name(path))
        if imp.module == KEYWORDS
    }
    assert set(PSEUDONYMISER_NEEDS) <= imported
    assert pattern_offences(PSEUDONYMS, tree, dotted_name(path)) == []


def test_every_sending_lane_checks_the_mask():
    """06 §5.2: ``extraction.call`` (and ``model_jobs``' batch submit, T-16)
    run ``assert_masked`` on every request before dispatch."""
    missing = []
    for lane in sorted(MASKING_LANES):
        path = PACKAGE / lane
        if not path.is_file():
            continue  # model_jobs.py arrives with T-16
        if not references(tree_of(path), "assert_masked"):
            missing.append(lane)
    assert missing == []
    assert (PACKAGE / "services/extraction.py").is_file()


def test_the_pattern_fence_catches_each_shape():
    def found(path: str, source: str) -> list[str]:
        module = "clientscrapper." + path.removesuffix(".py").replace("/", ".")
        return pattern_offences(path, ast.parse(source), module)

    copy = _pattern_sources()["TG_RE"]
    assert found("services/pseudonyms.py", "import re\nTG_RE = re.compile('@x')\n") == [
        "binds TG_RE"
    ]
    assert found("services/enrich.py", f"import re\nX = re.compile({copy!r})\n") == [
        "copies TG_RE"
    ]
    assert found("services/logs.py", "PHONE_RE: object = None\n") == ["binds PHONE_RE"]
    assert found(KEYWORDS_HOME, "import re\nEMAIL_RE = re.compile('x')\n") == []
    assert (
        found(
            "services/pseudonyms.py",
            "from clientscrapper.collectors.keywords import TG_RE\n",
        )
        == []
    )
