"""Nothing in the package writes to stdout/stderr past the log filters (06 §4.5).

Container stdout is the log: ``RedactBodies`` and ``RedactPatterns`` sit on
the one handler ``setup_logging`` installs, and a ``print(phone)`` or a
``sys.stdout.write(body)`` goes around both. So outside the command-line
tools (``clientscrapper/tools/*.py``, whose output is the terminal of the
person who ran them) and the code under ``if __name__ == "__main__":``, the
package does not call ``print``/``pprint`` or write to ``sys.stdout`` /
``sys.stderr`` / file descriptors 1 and 2. One exception is listed by name:
``services/lifecycle.run_main`` prints a ``SystemExit`` reason (a config
error's code) on its way out, MIYA's ``main()``.

Handing ``sys.stderr`` to a handler as its stream (``logs.setup_logging``) is
not a write and passes.
"""

from __future__ import annotations

import ast

from tests.ast_tools import Function, attr_chain, modules, owners, parents, rel, tree_of

TOOLS = "tools/"
ALLOWED: dict[str, set[str]] = {"services/lifecycle.py": {"run_main"}}
PRINTERS = {"print", "pprint", "pp"}
STREAMS = {"stdout", "stderr", "__stdout__", "__stderr__"}
WRITES = {"write", "writelines", "buffer"}


def _under_main(node: ast.AST, parent: dict[int, ast.AST]) -> bool:
    """Inside ``if __name__ == "__main__":``."""
    current = parent.get(id(node))
    while current is not None:
        if isinstance(current, ast.If):
            test = current.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and any(
                    isinstance(c, ast.Constant) and c.value == "__main__"
                    for c in test.comparators
                )
            ):
                return True
        current = parent.get(id(current))
    return False


def _writes(call: ast.Call) -> str | None:
    """What this call writes to, when it writes past the logging handler."""
    chain = attr_chain(call.func)
    if isinstance(call.func, ast.Name) and call.func.id in PRINTERS:
        return call.func.id
    if chain[-1] in {"pprint", "pp"} and chain[0] == "pprint":
        return ".".join(chain)
    # sys.stdout.write(…), sys.stderr.buffer.write(…)
    if any(part in STREAMS for part in chain[:-1]) and chain[-1] in WRITES | {"write"}:
        return ".".join(chain)
    # os.write(1, …) / os.write(2, …)
    if chain[-1] == "write" and chain[0] == "os" and call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and first.value in (1, 2):
            return f"os.write({first.value})"
    return None


def print_offences(path_rel: str, tree: ast.Module) -> list[str]:
    if path_rel.startswith(TOOLS):
        return []
    owner: dict[int, Function] = owners(tree)
    parent = parents(tree)
    allowed = ALLOWED.get(path_rel, set())
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or (what := _writes(node)) is None:
            continue
        function = owner.get(id(node))
        name = function.name if function else "<module>"
        if name in allowed or _under_main(node, parent):
            continue
        found.append(f"{path_rel}:{name}: {what}")
    return sorted(found)


def test_nothing_in_the_package_prints_past_the_log_filters():
    offending = [found for p in modules() for found in print_offences(rel(p), tree_of(p))]
    assert offending == [], (
        "log through logging (RedactPatterns runs there); stdout is the "
        f"container log (06 §4.5): {offending}"
    )


def test_the_detector_catches_each_shape():
    def found(path: str, source: str) -> list[str]:
        return print_offences(path, ast.parse(source))

    planted = (
        "import os, sys, pprint\n"
        "def seller(phone):\n"
        "    print(phone)\n"
        "    sys.stdout.write(phone)\n"
        "    sys.stderr.buffer.write(phone.encode())\n"
        "    pprint.pprint({'phone': phone})\n"
        "    os.write(1, phone.encode())\n"
    )
    assert found("collectors/probe.py", planted) == [
        "collectors/probe.py:seller: os.write(1)",
        "collectors/probe.py:seller: pprint.pprint",
        "collectors/probe.py:seller: print",
        "collectors/probe.py:seller: sys.stderr.buffer.write",
        "collectors/probe.py:seller: sys.stdout.write",
    ]
    assert found("worker/main.py", "print('started')\n") == [
        "worker/main.py:<module>: print"
    ]
    # The CLIs, the __main__ guard, the one named exit path and a handler's
    # stream are no leak.
    assert found("tools/doctor.py", planted) == []
    main = "import sys\nif __name__ == '__main__':\n    print(sys.argv)\n"
    assert found("services/x.py", main) == []
    exit_reason = "import sys\ndef run_main():\n    print(1, file=sys.stderr)\n"
    assert found("services/lifecycle.py", exit_reason) == []
    assert found("services/other.py", exit_reason) == [
        "services/other.py:run_main: print"
    ]
    stream = "import logging, sys\nh = logging.StreamHandler(sys.stderr)\n"
    assert found("logs.py", stream) == []
