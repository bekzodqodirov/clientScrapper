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
"""

from __future__ import annotations

import ast

from tests.ast_tools import dotted_name, imported_modules, is_under, modules, rel, tree_of

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
