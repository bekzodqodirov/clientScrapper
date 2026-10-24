"""OLX's phone reveal is never called (07 §1.2, 03 §2.5).

``/api/v1/offers/{id}/limited-phones/`` is behind a login and rate-limited,
and the ToS forbid it; contacts come from the ad text only. A grep over the
code with comments and docstrings stripped first — a fence that matched the
sentence explaining it would forbid its own documentation (fullprompt #725).
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

from tests.ast_tools import ROOT, docstring_ids, modules, tree_of

REVEAL = re.compile(r"limited[-_]?phones", re.IGNORECASE)
SCANNED_DIRS = (ROOT / "clientscrapper", ROOT / "scripts")


def _code_without_prose(source: str, docstrings: set[tuple[int, int]]) -> str:
    """The source's tokens minus comments and docstrings, joined by spaces."""
    kept = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.start in docstrings:
            continue
        kept.append(tok.string)
    return " ".join(kept)


def _docstring_starts(path: Path) -> set[tuple[int, int]]:
    tree = tree_of(path)
    ids = docstring_ids(tree)
    return {
        (n.lineno, n.col_offset)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and id(n) in ids
    }


def _reveals(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    return bool(REVEAL.search(_code_without_prose(source, _docstring_starts(path))))


def test_no_module_names_the_phone_reveal_endpoint():
    files = [p for base in SCANNED_DIRS if base.is_dir() for p in modules(base)]
    assert files, "the scan found no code"
    assert [str(p.relative_to(ROOT)) for p in files if _reveals(p)] == []


def test_the_grep_ignores_prose_and_catches_code(tmp_path):
    prose = tmp_path / "prose.py"
    prose.write_text(
        '"""Never /api/v1/offers/{id}/limited-phones/ (ToS)."""\n'
        "# limited-phones is a login wall\n"
        "def f():\n"
        '    """Not limited_phones either."""\n'
        "    return 1\n",
        encoding="utf-8",
    )
    assert not _reveals(prose)
    code = tmp_path / "code.py"
    code.write_text(
        "def phones(ad_id):\n"
        "    return f'https://www.olx.uz/api/v1/offers/{ad_id}/limited-phones/'\n",
        encoding="utf-8",
    )
    assert _reveals(code)
