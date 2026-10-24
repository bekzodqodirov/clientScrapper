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
from collections.abc import Callable, Iterable, Iterator, Mapping
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


def _concat_operands(node: ast.BinOp) -> list[ast.expr]:
    """The operands of a ``a + b + c`` chain, left to right."""
    found: list[ast.expr] = []
    for side in (node.left, node.right):
        if isinstance(side, ast.BinOp) and isinstance(side.op, ast.Add):
            found += _concat_operands(side)
        else:
            found.append(side)
    return found


def _is_literal_text(node: ast.AST) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _template(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
        )
    return "{}"


def texts(tree: ast.AST) -> Iterator[Text]:
    """Every literal text that is not a docstring, a built string as one
    template so a URL split around a placeholder is still one string to the
    guards: an f-string (``f"https://{host}/x"`` → ``"https://{}/x"``) and a
    ``+`` chain holding a literal (``"https://" + HOST + "/x"`` → the same).
    The pieces of a built string are not yielded on their own."""
    skip = docstring_ids(tree)
    parts: set[int] = set()
    specs: set[int] = set()
    concat: dict[int, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts.update(id(v) for v in node.values)
        elif isinstance(node, ast.FormattedValue) and node.format_spec is not None:
            specs.add(id(node.format_spec))
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and id(node) not in parts
        ):
            operands = _concat_operands(node)
            if any(_is_literal_text(o) for o in operands):
                # ast.walk is breadth first: the outermost + of a chain comes
                # first, and claims its inner + nodes and its literals.
                concat[id(node)] = operands
                for inner in ast.walk(node):
                    if inner is not node and (
                        isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Add)
                    ):
                        parts.add(id(inner))
                parts.update(id(o) for o in operands if _is_literal_text(o))
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and id(node) in concat:
            yield Text(node, "".join(_template(o) for o in concat[id(node)]))
        elif isinstance(node, ast.JoinedStr) and id(node) not in specs | parts:
            yield Text(node, _template(node))
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


# id(tree) → (tree, its parent map); the tree is kept so its id is never reused.
_PARENTS: dict[int, tuple[ast.AST, dict[int, ast.AST]]] = {}


def parents(tree: ast.AST) -> dict[int, ast.AST]:
    """Node id → the node directly containing it."""
    cached = _PARENTS.get(id(tree))
    if cached is not None and cached[0] is tree:
        return cached[1]
    parent = {
        id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    _PARENTS[id(tree)] = (tree, parent)
    return parent


def module_aliases(tree: ast.AST, *, module: str = "") -> dict[str, str]:
    """Names an import binds to a module (or to what may be one):
    ``import a.b`` → ``a: a``, ``import a.b as c`` → ``c: a.b``, ``from a
    import b`` → ``b: a.b``."""
    aliases: dict[str, str] = {}
    for imp in imports(tree, module=module):
        if not imp.alias or imp.name == "*":
            continue
        if imp.name is None:
            plain = isinstance(imp.node, ast.Import) and not any(
                a.asname for a in imp.node.names if a.name == imp.module
            )
            aliases[imp.alias] = imp.module.split(".")[0] if plain else imp.module
        else:
            aliases[imp.alias] = f"{imp.module}.{imp.name}" if imp.module else imp.name
    return aliases


def resolve_attribute(
    node: ast.Attribute, aliases: dict[str, str]
) -> tuple[str, str] | None:
    """(module, name) for a load through an imported module —
    ``serp.BRAVE_URL`` or ``clientscrapper.services.serp.BRAVE_URL`` — or
    None when the chain does not start at an import alias."""
    chain = attr_chain(node)
    if chain[0] not in aliases:
        return None
    return ".".join([aliases[chain[0]], *chain[1:-1]]), chain[-1]


# Where a bare host only classifies (compared, or listed in a table) and no
# URL is built on it.
CLASSIFYING = ast.Compare | ast.Set | ast.List | ast.Tuple | ast.Dict


def bound_name(
    node: ast.AST,
    mine: set[str],
    aliases: dict[str, str],
    bound: Mapping[str, set[str]],
) -> str | None:
    """The bound name a load reads, resolved where it was bound: a plain name
    of this module (``mine`` includes what it imported), an attribute through
    an imported module (``serp.BRAVE_URL`` reads serp's binding, ``httpx.URL``
    nothing of ours), or an attribute of anything else — ``self.URL`` — read
    as this module's class attribute."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        return node.id if node.id in mine else None
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        resolved = resolve_attribute(node, aliases)
        if resolved is not None:
            module, name = resolved
            return name if name in bound.get(module, set()) else None
        return node.attr if node.attr in mine else None
    return None


# --- names a guard cannot read ---------------------------------------------------

_NAMESPACES = {"vars", "globals", "locals"}
_LOOKUPS = {"get", "pop", "setdefault", "__getitem__"}


def _is_namespace(node: ast.expr) -> bool:
    """``vars(m)``, ``globals()``, ``locals()`` or ``x.__dict__``."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in _NAMESPACES
    return isinstance(node, ast.Attribute) and node.attr == "__dict__"


def _literal_text(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def dynamic_names(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    """Every place a module, an attribute or a binding is reached by a name
    built at run time, which no source-shape guard can read: an import with a
    non-literal name (``import_module("ht" + "tpx")``, ``__import__(n)``),
    ``getattr``/``setattr``/``delattr``/``hasattr`` and ``attrgetter`` with a
    non-literal name, and a namespace (``vars(m)``, ``globals()``,
    ``m.__dict__``) indexed or looked up by one. Fail closed: the guards that
    use this refuse the shape whatever the name would have been."""
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            first = node.args[0] if node.args else None
            name = (
                func.id
                if isinstance(func, ast.Name)
                else (func.attr if isinstance(func, ast.Attribute) else "")
            )
            if name in {"import_module", "__import__"} and not _literal_text(first):
                found.append((node, f"{name}(<built name>)"))
            elif name in {"getattr", "setattr", "delattr", "hasattr"}:
                attr = node.args[1] if len(node.args) > 1 else None
                if not _literal_text(attr):
                    found.append((node, f"{name}(…, <built name>)"))
            elif name == "attrgetter" and not all(_literal_text(a) for a in node.args):
                found.append((node, "attrgetter(<built name>)"))
            elif (
                isinstance(func, ast.Attribute)
                and func.attr in _LOOKUPS
                and _is_namespace(func.value)
                and not _literal_text(first)
            ):
                found.append((node, f"namespace.{func.attr}(<built name>)"))
        elif (
            isinstance(node, ast.Subscript)
            and _is_namespace(node.value)
            and not _literal_text(node.slice)
        ):
            found.append((node, "namespace[<built name>]"))
    return found
