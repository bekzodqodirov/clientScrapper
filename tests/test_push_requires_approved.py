"""No push without the ``approved`` state (07 §1.2).

A push is irreversible for the manager who receives it, so the ERP's lead
door has exactly one way in: ``services/crm_push.py::_post``, which alone
names ``ErpClient.post_lead``, reached only from ``push_one``; ``PUSH_PATH``
is named only there and in ``endpoint_for`` — the path the claim records in
``push_log.endpoint`` before the POST (02 §2.10 rule 3), itself reached only
from ``push_one`` — and ``push_one`` takes the candidate ``FOR UPDATE`` and
compares its state with ``'approved'`` (05 §B.1, 02 §2.10). Nobody else
spells the lead path by hand either — a second module building
``/api/leads/in/agent`` would be the same door with a different key — and no
other module reaches ``_post`` or ``endpoint_for``.

There is no exemption: ``make push-test`` (``tools/push_test.py``) pushes its
fresh synthetic company through ``push_one`` like every push (05 §B.1). The
zero-uuid exemption the T-04 guard carried — the tool calling ``_post``
directly with ``cs:00000000-0000-0000-0000-000000000000:<yyyymm>`` — was the
pre-T-07 design and is gone (T-28). Only that tool may pass ``synthetic=``
to ``push_one`` (the one checklist item it skips, 05 §B.1).

The behavioural half (Postgres + the fake ERP): a candidate in every state
but ``approved`` is refused ``not_approved`` and nothing is posted; a
re-push reads ``pushed`` only for the candidate's own ``external_id``; an
``approved`` row with no ``approved_by`` is refused by the database itself.
"""

from __future__ import annotations

import ast
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa

from clientscrapper.config import settings
from clientscrapper.db.enums import EXCLUDE_REASONS
from clientscrapper.db.models import LeadCandidate, PushLog, Signal
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import crm_push, scoring
from clientscrapper.services.erp_feeds import ErpClient
from tests import gate_fixtures
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
from tests.fake_erp import FakeErp
from tests.gate_fixtures import KEY, NOW

CRM_PUSH = "services/crm_push.py"
CRM_PUSH_MODULE = "clientscrapper.services.crm_push"
PUSH_TEST = "tools/push_test.py"
DOOR = "_post"
# Formats PUSH_PATH for the claim's push_log.endpoint (02 §2.10).
PATH_FORMATTER = "endpoint_for"
ENTRY = "push_one"
DOOR_NAMES = ("post_lead", "PUSH_PATH")
# Where each door name may appear inside crm_push.
NAMED_IN = {"post_lead": (DOOR,), "PUSH_PATH": (DOOR, PATH_FORMATTER)}
# push_one's keyword that skips item 13 for make push-test's company (05 §B.1).
SYNTHETIC_KWARG = "synthetic"
# The lead path as one token — a bare path, one relative to the client's
# base_url ("api/leads/in/agent" resolves to the same door), a URL, a
# template, a concatenation; the 404 alert sentence that quotes it for the
# owner (05 §B.1) is prose and does not match.
PUSH_LITERAL = re.compile(r"\S*leads/in(?:/\S*)?")


@dataclass(frozen=True, slots=True)
class Module:
    rel: str
    dotted: str
    tree: ast.Module


def _package() -> list[Module]:
    return [Module(rel(p), dotted_name(p), tree_of(p)) for p in modules()]


def _push_path_definition(tree: ast.Module) -> set[int]:
    """The literal of ``PUSH_PATH = "/api/leads/in/{source}"`` at module level
    (annotated ``PUSH_PATH: Final = …`` too)."""
    ids: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "PUSH_PATH" for t in targets):
            assert node.value is not None
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
                if not (at_door and where(ref) in NAMED_IN[name]):
                    found.append(f"{m.rel}:{where(ref)}: names {name}")
        defining = _push_path_definition(m.tree) if at_door else set()
        for text in texts(m.tree):
            if id(text.node) in defining:
                continue
            if PUSH_LITERAL.fullmatch(text.value.strip()):
                found.append(f"{m.rel}:{where(text.node)}: spells the lead path")

        # Who reaches the door and the path.
        if at_door:
            for function in functions(m.tree):
                if function.name in (DOOR, PATH_FORMATTER) and not reached_only_from(
                    m.tree, function, lambda f: f.name == ENTRY
                ):
                    found.append(f"{m.rel}:{function.name}: reached from outside {ENTRY}")
        elif CRM_PUSH_MODULE in imported_modules(m.tree, module=m.dotted):
            for name in (DOOR, PATH_FORMATTER):
                refs = references(m.tree, name)
                if refs:
                    found.append(f"{m.rel}:{where(refs[0])}: reaches crm_push.{name}")
        # Only make push-test may ask push_one to skip an item.
        if m.rel != PUSH_TEST:
            for node in ast.walk(m.tree):
                if isinstance(node, ast.Call) and any(
                    k.arg == SYNTHETIC_KWARG for k in node.keywords
                ):
                    found.append(f"{m.rel}:{where(node)}: passes {SYNTHETIC_KWARG}=")
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
    assert path.exists()
    assert entry_shape(tree_of(path)) == []


# --- the behavioural half (Postgres + the fake ERP) ------------------------------------

BASE = "https://erp.test"
# Every state but 'approved', with the columns its CHECK or trigger demands.
REFUSED_STATES = (
    "found",
    "enriching",
    "scored",
    "review",
    "rejected",
    "pushed",
    "suppressed",
    "excluded",
    "hold",
)


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def door(session, monkeypatch) -> AsyncIterator[tuple[FakeErp, ErpClient]]:
    monkeypatch.setattr(settings, "crm_base_url", BASE)
    async with gate_fixtures.erp_ready(session, monkeypatch):
        fake = FakeErp(KEY, clock=lambda: NOW + timedelta(minutes=1))
        yield fake, ErpClient(BASE, KEY, transport=fake.transport(), sleep=_no_sleep)


async def _lead(session, state: str, nn: int) -> LeadCandidate:
    """A candidate that would pass every push check but its state: B2B, a
    fresh phone, a cited stated volume and its origin."""
    owner = await gate_fixtures.firm(session)
    lead = await gate_fixtures.contact(session, owner, gate_fixtures.phone(nn))
    ev = await gate_fixtures.evidence(session, "telegram_monitor")
    ev.excerpt = "Yiwu dan 5 kub mato kerak, optom olamiz."
    session.add(
        Signal(
            company_id=owner.id,
            evidence_id=ev.id,
            key=scoring.STATED_3PLUS,
            value=Decimal(5),
            quote="5 kub mato kerak",
            observed_at=NOW - timedelta(days=2),
            source_key="telegram_monitor",
        )
    )
    # The origin too: push_one re-scores an approved candidate (04 §7 item
    # 13), and 30 + 20 + recency 10 + mobile 6 is hot.
    session.add(
        Signal(
            company_id=owner.id,
            evidence_id=ev.id,
            key="cn_origin",
            quote="Yiwu",
            observed_at=NOW - timedelta(days=2),
            source_key="telegram_monitor",
        )
    )
    # As though approved once: nothing clears approved_by when a 4xx or the
    # 48-hour rule sends an approved candidate back to review, so the state
    # alone must refuse.
    fields: dict[str, Any] = {
        "approved_by": "owner:1",
        "approved_at": NOW - timedelta(minutes=5),
    }
    if state == "suppressed":
        hit = await gate_fixtures.row(session, "dnc", "phone_tail", "900000099")
        fields["suppressed_by"] = hit.id
    if state == "excluded":
        fields["exclude_reason"] = EXCLUDE_REASONS[0]
    if state == "hold":
        fields["hold_until"] = NOW + timedelta(days=30)
    if state in ("found", "enriching"):
        fields["score"] = None
    found = await gate_fixtures.candidate(
        session, owner, state=state, lead=lead, **fields
    )
    await session.commit()
    return found


async def _push(cid, erp: ErpClient, *, now=NOW, repush_of=None):
    async with SessionLocal() as s:
        return await crm_push.push_one(s, cid, now=now, erp=erp, repush_of=repush_of)


@pytest.mark.parametrize("state", REFUSED_STATES)
async def test_every_state_but_approved_is_refused(session, door, state):
    fake, erp = door
    cand = await _lead(session, state, 61)
    result = await _push(cand.id, erp)
    assert (result.status, result.codes) == ("refused", ["not_approved"])
    assert fake.posted == [] and fake.requests == []
    assert await session.scalar(sa.select(sa.func.count(PushLog.id))) == 0
    again = await session.get(LeadCandidate, cand.id, populate_existing=True)
    assert again is not None and again.state == state


async def test_the_approved_control_is_posted(session, door):
    fake, erp = door
    cand = await _lead(session, "approved", 61)
    result = await _push(cand.id, erp)
    assert (result.status, result.codes) == ("sent", [])
    assert [source for source, _, _ in fake.posted] == ["agent"]


async def test_approved_without_approved_by_is_refused_by_the_database(session):
    owner = await gate_fixtures.firm(session)
    with pytest.raises(sa.exc.IntegrityError, match="ck_lead_candidates_approved"):
        await gate_fixtures.candidate(session, owner, approved_by=None)
    await session.rollback()


async def test_a_repush_reads_pushed_only_for_its_own_external_id(session, door):
    fake, erp = door
    cand = await _lead(session, "approved", 61)
    first = await _push(cand.id, erp)
    assert first.status == "sent"
    pushed = await session.get(LeadCandidate, cand.id, populate_existing=True)
    assert pushed is not None and pushed.state == "pushed"
    # A delivered row naming this candidate under another id (a company it
    # was merged from): the candidate's 'pushed' is not that push's, so the
    # re-push is refused.
    other = await gate_fixtures.firm(session)
    foreign = await gate_fixtures.push(
        session,
        pushed,
        sent_at=NOW - timedelta(days=40),
        phone_tail=gate_fixtures.tail(62),
        company_id=other.id,
    )
    foreign.payload = {"external_id": foreign.external_id}
    await session.commit()
    refused = await _push(
        cand.id, erp, now=NOW + timedelta(hours=49), repush_of=foreign.id
    )
    assert refused.codes == ["not_approved"]
    assert len(fake.posted) == 1
    # Its own delivered row 49 h later is re-sent once.
    later = NOW + timedelta(hours=49)
    await gate_fixtures.set_config(
        session, "suppression", gate_fixtures.fresh_config(later)
    )
    await session.commit()
    own = await _push(cand.id, erp, now=later, repush_of=first.push_log_id)
    assert (own.status, own.attempt) == ("sent", 2)
    assert len(fake.posted) == 2
    assert fake.posted[1][2]["external_id"] == pushed.external_id


# --- the detector ---------------------------------------------------------------------

GOOD_CRM_PUSH = """
import sqlalchemy as sa
PUSH_PATH = "/api/leads/in/{source}"
ALERT = "ERP kaliti yoki manba notoʻgʻri: /api/leads/in/{source} 404 qaytardi."

def endpoint_for(source):
    return PUSH_PATH.format(source=source)

async def _post(erp, source, payload):
    r = await erp.post_lead(endpoint_for(source), payload)
    return r.status_code

async def _attempt(erp, payload):
    return await with_backoff(lambda: _post(erp, "agent", payload))

async def push_one(session, candidate_id, *, now, erp, synthetic=False):
    row = (await session.execute(
        sa.select(Candidate).where(Candidate.id == candidate_id).with_for_update()
    )).scalar_one()
    if row.state != "approved":
        return Refusal("not_approved")
    await claim(session, endpoint=endpoint_for("agent"))
    return await _attempt(erp, payload)
"""

PUSH_TEST_TOOL = """
from clientscrapper.services import crm_push

async def main(session, cid, erp, now):
    return await crm_push.push_one(session, cid, now=now, erp=erp, synthetic=True)
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
        f"{CRM_PUSH}:_post: reached from outside push_one",
        f"{CRM_PUSH}:endpoint_for: reached from outside push_one",
    ]
    # The annotated definition is the definition; a second spelling is not.
    annotated = GOOD_CRM_PUSH.replace(
        'PUSH_PATH = "/api/leads/in/{source}"',
        'PUSH_PATH: Final = "/api/leads/in/{source}"\nOTHER = "/api/leads/in/x"',
    )
    assert found({CRM_PUSH: annotated}) == [f"{CRM_PUSH}:<module>: spells the lead path"]
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
    # Relative to httpx's base_url, or built from pieces, it is the same door.
    reviews = "services/reviews.py"
    relative = (
        "async def p(erp, j):\n    await erp.client.post('api/leads/in/agent', json=j)\n"
    )
    assert found({reviews: relative}) == [f"{reviews}:p: spells the lead path"]
    pieces = "async def p(c, s):\n    await c.post('api/leads' + '/in/' + s)\n"
    assert found({reviews: pieces}) == [f"{reviews}:p: spells the lead path"]
    assert found({reviews: "U = 'leads/in'\n"}) == [
        f"{reviews}:<module>: spells the lead path"
    ]
    # No exemption any more: push_test reaching _post is a door like any other
    # (the zero-uuid shortcut of T-04 is gone), and so is anyone holding the
    # path formatter.
    zero_uuid = (
        "from clientscrapper.services import crm_push\n"
        "EXTERNAL_ID = 'cs:00000000-0000-0000-0000-000000000000:{month}'\n"
        "async def main(erp):\n"
        "    return await crm_push._post(erp, 'agent', payload(EXTERNAL_ID))\n"
    )
    assert found({CRM_PUSH: GOOD_CRM_PUSH, PUSH_TEST: zero_uuid}) == [
        f"{PUSH_TEST}:main: reaches crm_push._post"
    ]
    path = (
        "from clientscrapper.services import crm_push\n"
        "async def go(c):\n    await c.post(crm_push.endpoint_for('agent'))\n"
    )
    assert found({CRM_PUSH: GOOD_CRM_PUSH, "bot/main.py": path}) == [
        "bot/main.py:go: reaches crm_push.endpoint_for"
    ]
    # Only push_test may pass synthetic=; the worker may not.
    assert found({CRM_PUSH: GOOD_CRM_PUSH, "worker/main.py": PUSH_TEST_TOOL}) == [
        "worker/main.py:main: passes synthetic="
    ]
    # post_lead outside _post inside crm_push itself, or the formatter used
    # from outside push_one.
    elsewhere = (
        GOOD_CRM_PUSH.replace(
            "    return PUSH_PATH.format(source=source)",
            "    return erp_post_lead_free(PUSH_PATH.format(source=source))",
        )
        + "\nasync def leak(erp):\n"
        + "    return await erp.post_lead(endpoint_for('x'), {})\n"
    )
    assert found({CRM_PUSH: elsewhere}) == [
        f"{CRM_PUSH}:endpoint_for: reached from outside push_one",
        f"{CRM_PUSH}:leak: names post_lead",
    ]
    # push_one without its lock or without the state check.
    unlocked = ast.parse(GOOD_CRM_PUSH.replace(".with_for_update()", ""))
    assert entry_shape(unlocked) == ["push_one takes no FOR UPDATE lock"]
    unchecked = ast.parse(GOOD_CRM_PUSH.replace('row.state != "approved"', "row is None"))
    assert entry_shape(unchecked) == ["push_one never compares the state with 'approved'"]
    assert entry_shape(ast.parse("async def push_one():\n    pass\n")) == [
        "defines no _post"
    ]
