"""What the source-shape guards share (07 §1.2).

The guards parse the package with ``ast`` and judge a shape, not behaviour:
who calls what, from which function, and what each module imports. The rules
live in the guard files; this module only answers questions about trees.

Two conventions every guard follows:

* A call or a name belongs to the **innermost** function that contains it
  (MIYA ``tests/test_userbot.py``), ``None`` being module or class level. A
  lambda is not a function here: what it does, its enclosing ``def`` does.
* Docstrings are prose, not code. A fence that matched the sentence
  explaining it would forbid its own documentation (fullprompt #725), so
  ``strings()`` skips them; comments never reach the AST at all.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "clientscrapper"

Function = ast.FunctionDef | ast.AsyncFunctionDef
_SCOPES = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
SKIPPED_PARTS = {"__pycache__"}


def modules(base: Path = PACKAGE) -> list[Path]:
    """Every ``.py`` file under ``base``, sorted."""
    return sorted(
        p for p in base.rglob("*.py") if not SKIPPED_PARTS & set(p.parts) and p.is_file()
    )


def rel(path: Path) -> str:
    """``userbot/main.py`` for a file of the package."""
    return path.relative_to(PACKAGE).as_posix()


def dotted_name(path: Path) -> str:
    """``clientscrapper.userbot.main`` (a package's ``__init__`` is the package)."""
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@functools.cache
def _parse_cached(path: str, mtime: float) -> ast.Module:
    return ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)


def tree_of(path: Path) -> ast.Module:
    return _parse_cached(str(path), path.stat().st_mtime)


# id(tree) → (tree, its owner map); the tree is kept so its id is never reused.
_OWNERS: dict[int, tuple[ast.AST, dict[int, Function]]] = {}


def owners(tree: ast.AST) -> dict[int, Function]:
    """Node id → the innermost function containing it (absent = module level).

    ``ast.walk`` is breadth first, so an enclosing function is visited before
    the functions nested in it and the innermost one is written last. A
    function's own ``def`` belongs to the function around it.
    """
    cached = _OWNERS.get(id(tree))
    if cached is not None and cached[0] is tree:
        return cached[1]
    owner: dict[int, Function] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                if child is not node:
                    owner[id(child)] = node
    _OWNERS[id(tree)] = (tree, owner)
    return owner


def functions(tree: ast.AST) -> list[Function]:
    return [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def docstring_ids(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _SCOPES) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def strings(tree: ast.AST) -> Iterator[ast.Constant]:
    """Every string constant that is not a docstring (f-string parts included)."""
    skip = docstring_ids(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
        ):
            yield node


@dataclass(frozen=True, slots=True)
class Text:
    node: ast.expr  # the Constant, or the whole JoinedStr of an f-string
    value: str


def texts(tree: ast.AST) -> Iterator[Text]:
    """Every literal text that is not a docstring, an f-string as one
    template (``f"https://{host}/x"`` → ``"https://{}/x"``) so a URL split
    around a placeholder is still one string to the guards."""
    skip = docstring_ids(tree)
    parts: set[int] = set()
    specs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts.update(id(v) for v in node.values)
        elif isinstance(node, ast.FormattedValue) and node.format_spec is not None:
            specs.add(id(node.format_spec))
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) and id(node) not in specs:
            value = "".join(
                v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
            )
            yield Text(node, value)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and id(node) not in parts
        ):
            yield Text(node, node.value)


def attr_chain(node: ast.AST) -> list[str]:
    """``client.messages.batches.create`` → ``['client', 'messages', 'batches',
    'create']``; a chain that does not start at a name starts with ``''``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    parts.append(node.id if isinstance(node, ast.Name) else "")
    return parts[::-1]


def called(call: ast.Call) -> list[ast.expr]:
    """What a call invokes, now or later: its function, and the first argument
    of ``partial(f, …)`` (a call made later)."""
    targets: list[ast.expr] = [call.func]
    func = call.func
    is_partial = (isinstance(func, ast.Name) and func.id == "partial") or (
        isinstance(func, ast.Attribute) and func.attr == "partial"
    )
    if is_partial and call.args:
        targets.append(call.args[0])
    return targets


@dataclass(frozen=True, slots=True)
class Import:
    module: str  # the dotted module imported, resolved when relative
    name: str | None  # the imported name of a ``from`` import
    alias: str  # the name bound in the importing module ("" for a dynamic one)
    node: ast.AST


def _is_dynamic_import(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in {"__import__", "import_module"}
    return isinstance(func, ast.Attribute) and func.attr == "import_module"


def _resolve(package: str, level: int, module: str) -> str:
    anchor = package.split(".") if package else []
    if level > 1:
        anchor = anchor[: len(anchor) - (level - 1)]
    return ".".join([*anchor, module] if module else anchor)


def imports(tree: ast.AST, *, module: str = "") -> list[Import]:
    """Every import, including ``importlib.import_module("x")`` and
    ``__import__("x")`` with a literal name. ``module`` (the importing file's
    dotted name) resolves relative imports."""
    found: list[Import] = []
    package = module.rsplit(".", 1)[0] if "." in module else ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.append(Import(a.name, None, a.asname or a.name.split(".")[0], node))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = _resolve(package, node.level, base)
            for a in node.names:
                found.append(Import(base, a.name, a.asname or a.name, node))
        elif isinstance(node, ast.Call) and node.args and _is_dynamic_import(node):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append(Import(first.value, None, "", node))
    return found


def imported_modules(tree: ast.AST, *, module: str = "") -> set[str]:
    """Dotted names a module imports: ``from a.b import c`` yields ``a.b`` and
    ``a.b.c`` (``c`` may be a submodule)."""
    names: set[str] = set()
    for imp in imports(tree, module=module):
        if imp.module:
            names.add(imp.module)
        if imp.name and imp.name != "*":
            names.add(f"{imp.module}.{imp.name}" if imp.module else imp.name)
    return names


def is_under(name: str, roots: Iterable[str]) -> str | None:
    """The first of ``roots`` that ``name`` is (or is a submodule of)."""
    for root in roots:
        if name == root or name.startswith(root + "."):
            return root
    return None


def references(tree: ast.AST, name: str) -> list[ast.Name | ast.Attribute]:
    """Loads of ``name``: a bare name or an attribute (``self.name``,
    ``module.name``). Definitions and assignments are not references."""
    found: list[ast.Name | ast.Attribute] = []
    for node in ast.walk(tree):
        loaded = isinstance(node, ast.Name | ast.Attribute) and isinstance(
            node.ctx, ast.Load
        )
        if not loaded:
            continue
        if (isinstance(node, ast.Name) and node.id == name) or (
            isinstance(node, ast.Attribute) and node.attr == name
        ):
            found.append(node)
    return found


def reached_only_from(
    tree: ast.AST,
    function: Function,
    is_root: Callable[[Function], bool],
    _seen: frozenset[int] = frozenset(),
) -> bool:
    """True when ``function`` is a root, or is referenced (called, passed,
    wrapped in ``partial``) at least once and only from functions that are
    themselves reached only from a root. A module-level or class-level
    reference (a registry dict, a decorator table) is a door from anywhere:
    it fails the walk. The walk stays inside one module (07 §1.2)."""
    if is_root(function):
        return True
    if id(function) in _seen:
        return False
    seen = _seen | {id(function)}
    owner = owners(tree)
    # A function calling itself opens no door of its own.
    callers = [owner.get(id(ref)) for ref in references(tree, function.name)]
    callers = [c for c in callers if c is not function]
    if not callers:
        return False
    return all(
        caller is not None and reached_only_from(tree, caller, is_root, seen)
        for caller in callers
    )
