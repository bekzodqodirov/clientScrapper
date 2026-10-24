"""No push without the ``approved`` state — the source-shape half (07 §1.2).

A push is irreversible for the manager who receives it, so the ERP's lead
door has exactly one way in: ``services/crm_push.py::_post``, which alone
names ``ErpClient.post_lead`` or ``PUSH_PATH``, reached only from
``push_one``; and ``push_one`` takes the candidate ``FOR UPDATE`` and compares
its state with ``'approved'`` (05 §B.1, 02 §2.10). Nobody else spells the
lead path by hand either — a second module building ``/api/leads/in/agent``
would be the same door with a different key.

The one exception is the visibly synthetic ``make push-test``
(``tools/push_test.py``, 05 §B.1): it posts one payload whose external id is
``cs:00000000-0000-0000-0000-000000000000:<yyyymm>`` and may reach ``_post``
for that, and only while it carries that id.

The behavioural half (Postgres + the fake ERP: every non-approved state is
refused ``not_approved`` and nothing is posted) arrives with crm_push in
T-28. Until services/crm_push.py exists, the fence holds the package to
naming the door nowhere at all.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

import pytest

from tests.ast_tools import (
    PACKAGE,
    dotted_name,
    functions,
    imported_modules,
    modules,
    owners,
    reached_only_from,
    references,
    rel,
    texts,
    tree_of,
)

CRM_PUSH = "services/crm_push.py"
CRM_PUSH_MODULE = "clientscrapper.services.crm_push"
PUSH_TEST = "tools/push_test.py"
SYNTHETIC_ID = "cs:00000000-0000-0000-0000-000000000000:"
DOOR = "_post"
ENTRY = "push_one"
DOOR_NAMES = ("post_lead", "PUSH_PATH")
# The lead path as one token — a bare path, a URL, a template; the 404 alert
# sentence that quotes it for the owner (05 §B.1) is prose and does not match.
PUSH_LITERAL = re.compile(r"\S*/api/leads/in/\S*")


@dataclass(frozen=True, slots=True)
class Module:
    rel: str
    dotted: str
    tree: ast.Module


def _package() -> list[Module]:
    return [Module(rel(p), dotted_name(p), tree_of(p)) for p in modules()]


def _push_path_definition(tree: ast.Module) -> set[int]:
    """The literal of ``PUSH_PATH = "/api/leads/in/{source}"`` at module level."""
    ids: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PUSH_PATH" for t in node.targets
        ):
            ids.update(id(n) for n in ast.walk(node.value))
    return ids


def door_offences(mods: list[Module]) -> list[str]:
    found: list[str] = []
    for m in mods:
        owner = owners(m.tree)

        def where(node: ast.AST, owner: dict = owner) -> str:
            function = owner.get(id(node))
            return function.name if function else "<module>"

        at_door = m.rel == CRM_PUSH
        for name in DOOR_NAMES:
            for ref in references(m.tree, name):
                if not (at_door and where(ref) == DOOR):
                    found.append(f"{m.rel}:{where(ref)}: names {name}")
        defining = _push_path_definition(m.tree) if at_door else set()
        for text in texts(m.tree):
            if id(text.node) in defining:
                continue
            if PUSH_LITERAL.fullmatch(text.value.strip()):
                found.append(f"{m.rel}:{where(text.node)}: spells the lead path")

        # Who reaches the door.
        door_refs = references(m.tree, DOOR)
        if at_door:
            for function in functions(m.tree):
                if function.name == DOOR and not reached_only_from(
                    m.tree, function, lambda f: f.name == ENTRY
                ):
                    found.append(f"{m.rel}:{DOOR}: reached from outside {ENTRY}")
        elif door_refs and CRM_PUSH_MODULE in imported_modules(m.tree, module=m.dotted):
            synthetic = any(SYNTHETIC_ID in t.value for t in texts(m.tree))
            if not (m.rel == PUSH_TEST and synthetic):
                found.append(f"{m.rel}:{where(door_refs[0])}: reaches crm_push.{DOOR}")
    return sorted(found)


def entry_shape(tree: ast.Module) -> list[str]:
    """What push_one and _post must contain; empty when both are right."""
    by_name = {f.name: f for f in functions(tree)}
    missing = [name for name in (DOOR, ENTRY) if name not in by_name]
    if missing:
        return [f"defines no {name}" for name in missing]
    problems = []
    door = by_name[DOOR]
    if not any(references(door, name) for name in DOOR_NAMES):
        problems.append(f"{DOOR} names neither of {DOOR_NAMES}")
    entry = by_name[ENTRY]
    locks = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "with_for_update"
        for n in ast.walk(entry)
    ) or any("FOR UPDATE" in t.value.upper() for t in texts(entry))
    if not locks:
        problems.append(f"{ENTRY} takes no FOR UPDATE lock")

    def is_approved(node: ast.AST) -> bool:
        return (isinstance(node, ast.Constant) and node.value == "approved") or (
            isinstance(node, ast.Attribute) and node.attr == "approved"
        )

    compares = [n for n in ast.walk(entry) if isinstance(n, ast.Compare)]
    if not any(is_approved(x) for c in compares for x in (c.left, *c.comparators)):
        problems.append(f"{ENTRY} never compares the state with 'approved'")
    return problems


def test_the_lead_door_is_named_only_inside_post():
    assert door_offences(_package()) == []


def test_push_one_locks_and_checks_approved():
    path = PACKAGE / CRM_PUSH
    if not path.exists():
        # The fence above holds meanwhile: nothing in the package names the
        # door, spells the path or reaches a _post.
        assert door_offences(_package()) == []
        pytest.skip("services/crm_push.py arrives with T-28 (05 §B.1)")
    assert entry_shape(tree_of(path)) == []


# --- the detector ---------------------------------------------------------------------

GOOD_CRM_PUSH = """
import sqlalchemy as sa
PUSH_PATH = "/api/leads/in/{source}"
ALERT = "ERP kaliti yoki manba notoʻgʻri: /api/leads/in/{source} 404 qaytardi."

async def _post(erp, source, payload):
    r = await erp.client.post(PUSH_PATH.format(source=source), json=payload)
    return r.status_code

async def push_one(session, candidate_id, *, now, erp):
    row = (await session.execute(
        sa.select(Candidate).where(Candidate.id == candidate_id).with_for_update()
    )).scalar_one()
    if row.state != "approved":
        return Refusal("not_approved")
    return await with_backoff(lambda: _post(erp, "agent", payload))
"""

PUSH_TEST_TOOL = """
from clientscrapper.services import crm_push
EXTERNAL_ID = "cs:00000000-0000-0000-0000-000000000000:{month}"

async def main(erp):
    return await crm_push._post(erp, "agent", payload(EXTERNAL_ID))
"""


def _mods(sources: dict[str, str]) -> list[Module]:
    return [
        Module(
            path,
            "clientscrapper." + path.removesuffix(".py").replace("/", "."),
            ast.parse(src),
        )
        for path, src in sources.items()
    ]


def test_the_detector_accepts_the_spec_shape():
    mods = _mods({CRM_PUSH: GOOD_CRM_PUSH, PUSH_TEST: PUSH_TEST_TOOL})
    assert door_offences(mods) == []
    assert entry_shape(mods[0].tree) == []


def test_the_detector_catches_each_shape():
    def found(sources: dict[str, str]) -> list[str]:
        return door_offences(_mods(sources))

    # A second door into _post inside crm_push.
    shortcut = GOOD_CRM_PUSH + (
        "\nasync def push_now(erp, p):\n    return await _post(erp, 'agent', p)\n"
    )
    assert found({CRM_PUSH: shortcut}) == [
        f"{CRM_PUSH}:_post: reached from outside push_one"
    ]
    # post_lead or PUSH_PATH named anywhere else.
    erp_post = "async def push(erp, p):\n    return await erp.post_lead(p)\n"
    reviews = "services/reviews.py"
    assert found({reviews: erp_post}) == [f"{reviews}:push: names post_lead"]
    path_use = (
        "from clientscrapper.services.crm_push import PUSH_PATH\n"
        "async def p(c):\n    await c.post(PUSH_PATH)\n"
    )
    handler = "bot/handlers/review.py"
    assert found({handler: path_use}) == [f"{handler}:p: names PUSH_PATH"]
    # The path spelled by hand: a path, a template, a URL.
    sheets = "services/sheets.py"
    by_hand = "async def p(c):\n    await c.post('/api/leads/in/agent', json={})\n"
    assert found({sheets: by_hand}) == [f"{sheets}:p: spells the lead path"]
    template = "U = f'{BASE}/api/leads/in/{SOURCE}'\n"
    assert found({sheets: template}) == [f"{sheets}:<module>: spells the lead path"]
    full = "U = 'https://erp.example.uz/api/leads/in/other'\n"
    assert found({"tools/doctor.py": full}) == [
        "tools/doctor.py:<module>: spells the lead path"
    ]
    # push_test only while it carries the synthetic id; anyone else never.
    real = PUSH_TEST_TOOL.replace(SYNTHETIC_ID, "cs:")
    assert found({CRM_PUSH: GOOD_CRM_PUSH, PUSH_TEST: real}) == [
        f"{PUSH_TEST}:main: reaches crm_push._post"
    ]
    assert found({CRM_PUSH: GOOD_CRM_PUSH, "worker/main.py": PUSH_TEST_TOOL}) == [
        "worker/main.py:main: reaches crm_push._post"
    ]
    # push_one without its lock or without the state check.
    unlocked = ast.parse(GOOD_CRM_PUSH.replace(".with_for_update()", ""))
    assert entry_shape(unlocked) == ["push_one takes no FOR UPDATE lock"]
    unchecked = ast.parse(GOOD_CRM_PUSH.replace('row.state != "approved"', "row is None"))
    assert entry_shape(unchecked) == ["push_one never compares the state with 'approved'"]
    assert entry_shape(ast.parse("async def push_one():\n    pass\n")) == [
        "defines no _post"
    ]
