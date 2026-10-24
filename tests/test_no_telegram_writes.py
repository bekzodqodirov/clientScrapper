# VENDORED_FROM dailyJournal@6b1f67f tests/test_userbot.py
"""No code path writes to Telegram as the reader, or reaches anyone at all.

07 §1.2 and 06 §5.3. MIYA's userbot guard with its sender deleted: there is no
sender in v1, so ``SENDER_WRITE_FUNCTIONS`` is empty and the exemption branch
is gone — a write call may appear nowhere in the reader, called or kept as a
bound method. Two raw requests are allowed, each in one named function (the
joiner and the ``about`` reader), and history is read only where the owner
asked for it. The reader is userbot/, every module importing Telethon and
every module importing the reader; outside it, a module handed the client
still may not use Telethon's write methods.

The aiogram side and the other channels are fenced here too: the owner bot
sends through one wrapper, ``bot/deliver.py::to_person``, which checks the
owner/manager allowlist; nothing else in the package may call a Bot API send,
import a mail/SMS/telephony client, or name a messenger API host.

Source-shape tests: they parse the package and fail on a shape, not on
behaviour. Each detector has a self-test on planted sources, because a guard
that cannot see its shape is green for ever.
"""

from __future__ import annotations

import ast
import asyncio
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from clientscrapper.config import settings
from clientscrapper.userbot import main as userbot
from tests.ast_tools import (
    CLASSIFYING,
    PACKAGE,
    ROOT,
    bound_name,
    called,
    dotted_name,
    dynamic_names,
    functions,
    imported_modules,
    imports,
    is_under,
    module_aliases,
    modules,
    owners,
    parents,
    reached_only_from,
    references,
    rel,
    resolve_attribute,
    texts,
    tree_of,
)

USERBOT_PACKAGE = PACKAGE / "userbot"

# MIYA's set, unchanged (07 §1.2). Everywhere in the reader and in every
# module that imports Telethon none of these may be called: the account must
# never leave a trace in a chat it reads.
WRITE_CALLS = {
    "send_message",
    "send_file",
    "send_read_acknowledge",
    "edit_message",
    "delete_messages",
    "forward_messages",
    "action",
    "mark_read",
    "reply",
    "respond",
    "edit",
    "delete",
    "forward_to",
    "pin_message",
    "unpin_message",
    "send_reaction",
    "click",
    "upload_file",
    # Draft.send / Draft.set_message, Message.pin / unpin, and the dialog and
    # participant writes.
    "send",
    "set_message",
    "pin",
    "unpin",
    "delete_dialog",
    "kick_participant",
    "edit_permissions",
    "edit_admin",
    "edit_folder",
}
# Raw MTProto requests that write (or show the account as active), for the
# record. The guard is stricter: any raw request name (a capitalised name
# ending in ``Request``) named anywhere — called, imported, aliased, an
# attribute, or a string handed to getattr — fails it unless RAW_ALLOWED
# names that exact request in that exact function.
RAW_REQUEST = re.compile(r"[A-Z]\w*Request")
RAW_WRITE_REQUESTS = {
    "SendMessageRequest",
    "SendMediaRequest",
    "SendMultiMediaRequest",
    "ForwardMessagesRequest",
    "EditMessageRequest",
    "DeleteMessagesRequest",
    "DeleteHistoryRequest",
    "ReadHistoryRequest",
    "ReadMessageContentsRequest",
    "ReadMentionsRequest",
    "ReadReactionsRequest",
    "SetTypingRequest",
    "SaveDraftRequest",
    "SendReactionRequest",
    "UpdatePinnedMessageRequest",
    "GetBotCallbackAnswerRequest",
    "ImportContactsRequest",
    "AddContactRequest",
    "JoinChannelRequest",
    "ImportChatInviteRequest",
    "UpdateStatusRequest",
    "UploadFileRequest",
    "SaveFilePartRequest",
    "SendInlineBotResultRequest",
    "StartBotRequest",
    "SendVoteRequest",
    "SendScreenshotNotificationRequest",
    "SendEncryptedRequest",
    "SendQuickReplyMessagesRequest",
    "SendPaidReactionRequest",
    "LeaveChannelRequest",
    "DeleteChatUserRequest",
}
# The only raw requests there are, each in one function (07 §1.2): the joiner
# (≤ 3 joins a day, 03 §2.3) and the channel/user ``about`` reader (06 §2).
RAW_ALLOWED: dict[tuple[str, str], set[str]] = {
    ("userbot/joiner.py", "join_one"): {"JoinChannelRequest", "GetFullChannelRequest"},
    ("userbot/about.py", "read_about"): {"GetFullChannelRequest", "GetFullUserRequest"},
}
# Joining is one function, reached from one scheduler entry whose cap test
# runs on a fake clock (T-13).
JOIN_REQUESTS = {"JoinChannelRequest", "ImportChatInviteRequest"}
JOIN_SITE = ("userbot/joiner.py", "join_one")
JOIN_CALLER = ("userbot/joiner.py", "join_daily")

# Reading history is about *unsolicited* reading: the reader lives on
# events.NewMessage and trawls backwards only where the owner asked — the
# backfill of a chat he switched on, and the about reader.
HISTORY_CALLS = {"get_messages", "iter_messages"}
HISTORY_ALLOWED = {
    ("userbot/backfill.py", "backfill_chat"),
    ("userbot/about.py", "read_about"),
}

# --- the aiogram side (06 §5.3) ------------------------------------------------

# Bot API methods that put something into a chat. They may be called only in
# bot/deliver.py, by to_person or a helper reached only from it, which checks
# that the chat is the owner's or an allow-listed manager's.
BOT_SEND_CALLS = {
    "send_message",
    "send_photo",
    "send_document",
    "send_video",
    "send_audio",
    "send_voice",
    "send_animation",
    "send_sticker",
    "send_media_group",
    "send_location",
    "send_venue",
    "send_contact",
    "send_poll",
    "send_dice",
    "send_video_note",
    "send_chat_action",
    "send_paid_media",
    "send_invoice",
    "send_game",
    "copy_message",
    "copy_messages",
    "forward_message",
    "forward_messages",
    "edit_message_text",
    "edit_message_caption",
    "edit_message_media",
    "edit_message_reply_markup",
}
DELIVER = "bot/deliver.py"
DELIVER_ROOT = "to_person"
# The same sends as aiogram method objects (bot(SendMessage(chat_id=…))),
# reachable without importing aiogram.methods by name (``import aiogram``
# then ``aiogram.methods.SendMessage``): refused outside deliver.py, by name.
BOT_METHOD_OBJECTS = frozenset(
    "".join(part.title() for part in name.split("_")) for name in BOT_SEND_CALLS
)
# Message/callback shortcuts answer into the chat the update came from. Inside
# bot/ the routers admit only the owner and the managers (06 §1.1); anywhere
# else a module holding aiogram objects has no business answering anyone.
REPLY_SHORTCUTS = re.compile(r"(answer|reply)(_\w+)?|edit_text|edit_caption|send_copy")

# --- other channels ---------------------------------------------------------------

# Mail, SMS, telephony and second Telegram clients: none may be imported.
FORBIDDEN_IMPORTS = (
    "smtplib",
    "aiosmtplib",
    "yagmail",
    "twilio",
    "vonage",
    "nexmo",
    "plivo",
    "sendgrid",
    "mailgun",
    "messagebird",
    "africastalking",
    "telesign",
    "sinch",
    "pywhatkit",
    "yowsup",
    "pyrogram",
    "telebot",
    "telegram",
    "tdjson",
)
# The same as distributions in requirements*.txt (06 §5.3: "no SMS, e-mail or
# telephony dependency exists").
FORBIDDEN_DISTRIBUTIONS = {
    "aiosmtplib",
    "yagmail",
    "twilio",
    "vonage",
    "nexmo",
    "plivo",
    "sendgrid",
    "mailgun",
    "messagebird",
    "africastalking",
    "telesign",
    "sinch",
    "pywhatkit",
    "yowsup",
    "yowsup2",
    "pyrogram",
    "kurigram",
    "tdjson",
    "python-telegram-bot",
    "pytelegrambotapi",
    "telegram-send",
}
# Hosts and schemes that only a sender would name, as a URL (a bare host in a
# classification table, such as normalise.LINK_HUB_HOSTS, sends nothing). The
# Bot API host belongs to aiogram, never to our own HTTP; graph.facebook.com
# is read for Business Discovery, its /messages edges (Messenger, WhatsApp,
# Instagram DMs) never.
_SENDER_HOSTS = (
    r"api\.telegram\.org",
    r"api\.twilio\.com",
    r"(?:rest|api)\.nexmo\.com",
    r"api\.vonage\.com",
    r"api\.sendgrid\.com",
    r"api\.mailgun\.net",
    r"(?:[\w-]+\.)*eskiz\.uz",
    r"(?:[\w-]+\.)*playmobile\.uz",
    r"api\.whatsapp\.com",
    r"wa\.me",
)
_HOST = "(?:" + "|".join(_SENDER_HOSTS) + ")"
OUTBOUND_TARGETS = re.compile(
    rf"https?://{_HOST}|(?<![\w.-]){_HOST}/"
    r"|graph\.facebook\.com/\S*messages"
    r"|\bsmtps?://|\bsmtp\.[a-z0-9-]+\.[a-z]+|\bsendmail\b",
    re.IGNORECASE,
)
# The same hosts bare, as a constant a URL is built from at run time
# (``f"https://{TG_HOST}/bot{token}/sendMessage"``). A bare host is allowed
# where it classifies — compared, or listed in a table — and nowhere else.
SENDER_HOST = re.compile(_HOST, re.IGNORECASE)
REQUIREMENTS = (ROOT / "requirements.txt", ROOT / "requirements-dev.txt")


# --- MIYA's helpers (tests/test_userbot.py) -------------------------------------


def _calls_by_function(source: str) -> dict[str | None, set[str]]:
    """Attribute calls in each function, by the innermost function that
    makes them (a nested helper is its own name); None is module level."""
    tree = ast.parse(source)
    owner: dict[int, str | None] = {}
    # ast.walk is breadth first: an enclosing function comes before the
    # functions nested in it, so the innermost one is written last.
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                owner[id(child)] = node.name

    found: dict[str | None, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            found.setdefault(owner.get(id(node)), set()).add(node.func.attr)
        # partial(client.get_messages, …) is a call too, made later.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "partial"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
        ):
            found.setdefault(owner.get(id(node)), set()).add(node.args[0].attr)
    return found


def _raw_requests_by_function(source: str) -> dict[str | None, set[str]]:
    """Raw requests named in each function (innermost), or at module level
    (None): a call, a bare name, an attribute or an import alias. Every raw
    TL request counts, not only the known writes (RAW_REQUEST)."""
    tree = ast.parse(source)
    owner: dict[int, str | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                owner[id(child)] = node.name
    found: dict[str | None, set[str]] = {}
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.alias):
            names += [node.name.rsplit(".", 1)[-1], node.asname or ""]
        # getattr(functions.messages, "SendMessageRequest") names one too.
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.append(node.value)
        hits = {name for name in names if name and RAW_REQUEST.fullmatch(name)}
        if hits:
            found.setdefault(owner.get(id(node)), set()).update(hits)
    return found


# A write needs no call of its own: a bound method kept for later (``send =
# client.send_message``), handed over (``call=client.send_file``) or looked
# up by name (``getattr(client, "send_message")``) writes once it is called.
# Telethon's Message.action is data — the service-message payload a reader
# reads to skip joins — so it counts only when called, like the one-word
# names the package-wide fence below shares with other libraries.
DATA_ATTRIBUTES = frozenset({"action"})
LOOKUPS = frozenset({"getattr", "methodcaller", "attrgetter", "__getattribute__"})


def _is_lookup(func: ast.expr) -> bool:
    return (isinstance(func, ast.Name) and func.id in LOOKUPS) or (
        isinstance(func, ast.Attribute) and func.attr in LOOKUPS
    )


def _method_refs_by_function(
    source: str, names: Iterable[str], called_only: Iterable[str] = DATA_ATTRIBUTES
) -> dict[str | None, set[str]]:
    """Methods of ``names`` named without a direct call, by the innermost
    function: an attribute load that is not the called function, and a
    string a getattr-like lookup is given. A ``called_only`` name counts only
    when the plain name it is bound to is called in the same function, or
    when its lookup is called straight away."""
    tree = ast.parse(source)
    wanted, data = set(names), set(called_only)
    owner: dict[int, str | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                owner[id(child)] = node.name
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    callees = {id(n.func) for n in calls}
    called_names = {
        (owner.get(id(n)), n.func.id) for n in calls if isinstance(n.func, ast.Name)
    }
    bound_to: dict[int, set[str]] = {}  # an attribute load → the names it is bound to
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.Attribute):
            plain = {t.id for t in targets if isinstance(t, ast.Name)}
            bound_to.setdefault(id(value), set()).update(plain)

    found: dict[str | None, set[str]] = {}

    def add(node: ast.AST, name: str) -> None:
        found.setdefault(owner.get(id(node)), set()).add(name)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in wanted
            and id(node) not in callees
        ):
            function = owner.get(id(node))
            later = {(function, name) for name in bound_to.get(id(node), set())}
            if node.attr not in data or later & called_names:
                add(node, node.attr)
        elif isinstance(node, ast.Call) and _is_lookup(node.func):
            for arg in node.args:
                looked_up = isinstance(arg, ast.Constant) and arg.value in wanted
                if looked_up and (arg.value not in data or id(node) in callees):
                    add(node, str(arg.value))
    return found


def _uses_by_function(
    source: str, names: Iterable[str], called_only: Iterable[str] = DATA_ATTRIBUTES
) -> dict[str | None, set[str]]:
    """Calls and method references of ``names``, by the innermost function
    (only the functions that name one)."""
    wanted = set(names)
    found: dict[str | None, set[str]] = {}
    for by in (
        _calls_by_function(source),
        _method_refs_by_function(source, wanted, called_only),
    ):
        for function, named in by.items():
            if named & wanted:
                found.setdefault(function, set()).update(named & wanted)
    return found


def _imports_telethon(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name.split(".")[0] == "telethon" for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == (
            "telethon"
        ):
            return True
    # importlib.import_module("telethon…") is an import as well.
    return any(is_under(imp.module, ["telethon"]) for imp in imports(tree))


def _imports_the_reader(tree: ast.Module, module: str) -> bool:
    names = imported_modules(tree, module=module)
    return any(is_under(name, ["clientscrapper.userbot"]) for name in names)


def _scanned() -> list[Path]:
    """Every module of clientscrapper/userbot (recursively), every module of
    the package that imports Telethon, and every module that imports the
    reader (it holds the reader's objects)."""
    paths = set(modules(USERBOT_PACKAGE))
    paths |= {
        p
        for p in modules()
        if _imports_telethon(p) or _imports_the_reader(tree_of(p), dotted_name(p))
    }
    return sorted(paths)


SCANNED = _scanned()


def _offences(path_rel: str, source: str) -> list[str]:
    """What a reader module does that it must not. A name built at run time
    (``getattr(client, verb)``, ``vars(m)["SendMessage" + "Request"]``,
    ``import_module(name)``) is refused whatever it would name: no rule here
    can read it, so the reader fails closed."""
    offending = []
    for function, writes in _uses_by_function(source, WRITE_CALLS).items():
        offending.append(f"{function or '<module>'}: {sorted(writes)}")
    for function, named in _raw_requests_by_function(source).items():
        allowed = RAW_ALLOWED.get((path_rel, function or ""), set())
        if named - allowed:
            offending.append(f"{function or '<module>'}: {sorted(named - allowed)}")
    tree = ast.parse(source)
    owner = owners(tree)
    for node, what in dynamic_names(tree):
        function = owner.get(id(node))
        offending.append(f"{function.name if function else '<module>'}: {what}")
    return offending


# --- the reader ---------------------------------------------------------------


def test_the_scan_reaches_every_telethon_module():
    names = {rel(p) for p in SCANNED}
    assert {"userbot/__init__.py", "userbot/main.py"} <= names
    # A second, textual reading of "imports telethon": if _imports_telethon
    # ever stopped seeing an import, the module would silently leave the scan.
    textual = re.compile(
        r"^\s*(?:import|from)\s+telethon\b|import_module\(\s*['\"]telethon", re.MULTILINE
    )
    by_text = {rel(p) for p in modules() if textual.search(p.read_text(encoding="utf-8"))}
    assert by_text <= names


@pytest.mark.parametrize("path", SCANNED, ids=rel)
def test_the_reader_never_writes_to_telegram(path):
    """No write call anywhere in the reader; raw requests only where
    RAW_ALLOWED names them (there is no sender in v1)."""
    offending = _offences(rel(path), path.read_text(encoding="utf-8"))
    assert not offending, f"{rel(path)} writes to Telegram: {offending}"


def test_joins_happen_in_join_one_reached_only_from_join_daily():
    """03 §2.3: ≤ 3 joins a day, in one function. A join request named
    anywhere else in the package, or join_one reached from anything but the
    daily job, fails."""
    sites = set()
    for path in modules():
        for function, named in _raw_requests_by_function(
            path.read_text(encoding="utf-8")
        ).items():
            if named & JOIN_REQUESTS:
                joins = sorted(named & JOIN_REQUESTS)
                sites.add((rel(path), function or "<module>", *joins))
    assert sites <= {(*JOIN_SITE, "JoinChannelRequest")}
    callers = set()
    for path in modules():
        tree = tree_of(path)
        owner = owners(tree)
        for ref in references(tree, JOIN_SITE[1]):
            caller = owner.get(id(ref))
            callers.add((rel(path), caller.name if caller else "<module>"))
    assert callers <= {JOIN_CALLER}
    joiner = PACKAGE / JOIN_SITE[0]
    if joiner.exists():
        defined = {f.name for f in functions(tree_of(joiner))}
        assert {JOIN_SITE[1], JOIN_CALLER[1]} <= defined
        assert callers == {JOIN_CALLER}


@pytest.mark.parametrize("path", SCANNED, ids=rel)
def test_history_is_only_read_where_the_owner_asked_for_it(path):
    history = _uses_by_function(
        path.read_text(encoding="utf-8"), HISTORY_CALLS, called_only=()
    )
    for function, offending in history.items():
        if not offending or (rel(path), function) in HISTORY_ALLOWED:
            continue
        pytest.fail(
            f"{rel(path)}:{function or '<module>'} calls {sorted(offending)} — the "
            "reader follows live events and must not pull history on its own. "
            f"Only {sorted(HISTORY_ALLOWED)} may, to read what the owner asked for."
        )


# Outside the scan a module may still be handed the reader's client (T-13's
# services/chats.py takes one). Telethon's write methods are refused there
# too, except the words other libraries use every day (httpx's client.send,
# a session's delete, edit, Message.action), and the one-word ones count only
# when called. bot/ is aiogram's: its fence is the Bot API one below.
PACKAGE_WRITES = frozenset(WRITE_CALLS - {"send", "edit", "delete", "action"})
PACKAGE_CALLED_ONLY = frozenset(n for n in PACKAGE_WRITES if "_" not in n)


def _package_write_offences(source: str) -> list[str]:
    uses = _uses_by_function(source, PACKAGE_WRITES, called_only=PACKAGE_CALLED_ONLY)
    return sorted(f"{f or '<module>'}: {sorted(names)}" for f, names in uses.items())


def test_no_module_writes_with_a_client_it_was_handed():
    scanned = set(SCANNED)
    offending = {}
    for path in modules():
        if path in scanned or rel(path).startswith("bot/"):
            continue
        found = _package_write_offences(path.read_text(encoding="utf-8"))
        if found:
            offending[rel(path)] = found
    assert offending == {}, (
        "a Telethon write outside the reader is still the reader writing "
        f"(06 §5.3): {offending}"
    )


def test_the_write_detector_catches_each_shape():
    source = (
        "async def outer(client):\n"
        "    async def inner():\n"
        "        await client.send_message(1, 'x')\n"
        "    await client.get_dialogs()\n"
        "from telethon.tl.functions.messages import ReadHistoryRequest as R\n"
        "async def raw(client):\n"
        "    await client(functions.messages.SetTypingRequest(1, None))\n"
        "async def aliased(client):\n"
        "    request = SendMessageRequest\n"
        "async def looked_up(functions):\n"
        "    return getattr(functions, 'SendMediaRequest')\n"
    )
    calls = _calls_by_function(source)
    # The nested helper's call is its own, not the enclosing function's.
    assert calls["inner"] == {"send_message"}
    assert calls["outer"] == {"get_dialogs"}
    raw = _raw_requests_by_function(source)
    assert raw == {
        None: {"ReadHistoryRequest"},
        "raw": {"SetTypingRequest"},
        "aliased": {"SendMessageRequest"},
        "looked_up": {"SendMediaRequest"},
    }
    # Any raw request, not only the listed writes: a raw history read too.
    unlisted = "async def trawl(client):\n    await client(GetHistoryRequest(1))\n"
    assert _raw_requests_by_function(unlisted) == {"trawl": {"GetHistoryRequest"}}
    assert all(RAW_REQUEST.fullmatch(name) for name in RAW_WRITE_REQUESTS)
    drafts = (
        "async def d(draft, message):\n    await draft.send()\n    await message.pin()\n"
    )
    assert _calls_by_function(drafts)["d"] & WRITE_CALLS == {"send", "pin"}
    # The allow-list is per function: the joiner's request elsewhere in the
    # same file, or a second request inside join_one, is an offence.
    joiner = (
        "async def join_one(client, chat):\n"
        "    await client(JoinChannelRequest(chat))\n"
        "async def join_all(client, chats):\n"
        "    for chat in chats:\n"
        "        await client(JoinChannelRequest(chat))\n"
    )
    assert _offences("userbot/joiner.py", joiner) == ["join_all: ['JoinChannelRequest']"]
    sneaky = (
        "async def join_one(client, c):\n    await client(SendMessageRequest(c, 'x'))\n"
    )
    assert _offences("userbot/joiner.py", sneaky) == ["join_one: ['SendMessageRequest']"]
    dynamic = "import importlib\nT = importlib.import_module('telethon.sync')\n"
    assert any(is_under(i.module, ["telethon"]) for i in imports(ast.parse(dynamic)))
    # A write method kept, handed over or looked up writes when called.
    kept = (
        "async def kept(client):\n"
        "    send = client.send_message\n"
        "    await send('me', 'x')\n"
        "async def handed(client, run):\n"
        "    await run(call=client.send_file)\n"
        "async def looked_up(client):\n"
        "    await getattr(client, 'send_read_acknowledge')(1)\n"
        "async def typing(client):\n"
        "    act = client.action\n"
        "    await act(1, 'typing')\n"
        "def skip(message):\n"
        "    act = message.action\n"
        "    return message.action is None or getattr(message, 'action', None) or act\n"
    )
    assert sorted(_offences("userbot/main.py", kept)) == [
        "handed: ['send_file']",
        "kept: ['send_message']",
        "looked_up: ['send_read_acknowledge']",
        "typing: ['action']",
    ]
    # A name built at run time is refused, whatever it would name (the seam
    # review's probe_t2 / probe_t4 shapes).
    built = (
        "import importlib\n"
        "from telethon.tl import functions as m\n"
        "async def greet(client, verb):\n"
        "    await getattr(client, verb)('me', 'salom')\n"
        "async def raw(client):\n"
        "    await client(vars(m)['SendMessage' + 'Request']('me', 'salom'))\n"
        "async def loaded(name):\n"
        "    return importlib.import_module(name)\n"
    )
    assert sorted(_offences("userbot/probe.py", built)) == [
        "greet: getattr(…, <built name>)",
        "loaded: import_module(<built name>)",
        "raw: namespace[<built name>]",
    ]
    history = "async def trawl(client):\n    it = client.iter_messages\n    return it\n"
    assert _uses_by_function(history, HISTORY_CALLS, called_only=()) == {
        "trawl": {"iter_messages"}
    }
    # Handed the client, a module outside the scan may not write with it; the
    # everyday words of other libraries stay theirs.
    chats = (
        "async def ack(client, chat, message, path):\n"
        "    await client.send_read_acknowledge(chat)\n"
        "    await client.send_file(chat, path)\n"
        "    await message.reply('salom')\n"
        "    reply = message.reply\n"
        "async def store(session, http, row, request):\n"
        "    await session.delete(row)\n"
        "    await http.send(request)\n"
        "    return row.reply\n"
    )
    assert _package_write_offences(chats) == [
        "ack: ['reply', 'send_file', 'send_read_acknowledge']"
    ]
    # A module importing the reader holds its objects: it joins the scan.
    services = "clientscrapper.services.chats"
    assert _imports_the_reader(ast.parse("from ..userbot import about\n"), services)
    assert _imports_the_reader(ast.parse("import clientscrapper.userbot.x\n"), services)
    assert not _imports_the_reader(ast.parse("from ..db import models\n"), services)


def test_the_kill_switch_idles_and_never_builds_a_client(monkeypatch):
    """USERBOT_ENABLED=false must idle without touching Telethon at all.

    Idle, not exit: the container is ``restart: unless-stopped``, so a clean
    exit would be restarted in a tight loop for ever. Whatever the reader
    calls to idle (the skeleton's idle_until_stopped, T-13's _idle_forever)
    is replaced by a recorder, and asserting it ran is the point — a stub
    that merely let run() return would pass either way."""
    monkeypatch.setattr(settings, "userbot_enabled", False)

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("TelegramClient was constructed with the switch off")

    idled = []

    async def _idle():
        idled.append(True)

    monkeypatch.setattr(userbot, "TelegramClient", _explode, raising=False)
    monkeypatch.setattr(userbot, "_idle_forever", _idle, raising=False)
    monkeypatch.setattr(userbot, "idle_until_stopped", _idle, raising=False)
    monkeypatch.setattr(userbot, "setup_logging", lambda *a, **k: None, raising=False)
    asyncio.run(userbot.run())
    assert idled, "the kill switch must idle the process, not let it exit"


@pytest.mark.skipif(
    not hasattr(userbot, "TelegramClient"),
    reason="T-13 builds the Telethon reader; the T-01 skeleton never logs in",
)
def test_a_missing_session_fails_loudly_naming_the_key(monkeypatch):
    monkeypatch.setattr(settings, "userbot_enabled", True)
    monkeypatch.setattr(settings, "telethon_session", "")
    monkeypatch.setattr(userbot, "setup_logging", lambda *a, **k: None, raising=False)
    with pytest.raises(SystemExit) as exit_:
        asyncio.run(userbot.run())
    assert "TELETHON_SESSION" in str(exit_.value.code)


# --- the owner bot and every other channel ----------------------------------------


def _bot_send_sites(tree: ast.Module) -> list[tuple[ast.Call, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for target in called(node):
                if isinstance(target, ast.Attribute) and target.attr in BOT_SEND_CALLS:
                    found.append((node, target.attr))
    return found


def _send_offences(path_rel: str, tree: ast.Module, module: str = "") -> list[str]:
    """Bot API sends outside to_person, aiogram method objects outside
    deliver.py, and reply shortcuts in an aiogram module outside bot/."""
    owner = owners(tree)
    offending = []
    for call, name in _bot_send_sites(tree):
        function = owner.get(id(call))
        allowed = (
            path_rel == DELIVER
            and function is not None
            and reached_only_from(tree, function, lambda f: f.name == DELIVER_ROOT)
        )
        if not allowed:
            where = function.name if function else "<module>"
            offending.append(f"{where}: {name}")
    imported = imported_modules(tree, module=module)
    if path_rel != DELIVER and any(is_under(m, ["aiogram.methods"]) for m in imported):
        offending.append("<module>: imports aiogram.methods")
    if path_rel != DELIVER:
        offending += _method_object_offences(tree, module)
    holds_aiogram = any(is_under(m, ["aiogram"]) for m in imported)
    if holds_aiogram and not path_rel.startswith("bot/"):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and REPLY_SHORTCUTS.fullmatch(node.func.attr)
            ):
                function = owner.get(id(node))
                where = function.name if function else "<module>"
                offending.append(f"{where}: {node.func.attr}")
    return sorted(offending)


def _method_object_offences(tree: ast.Module, module: str) -> list[str]:
    """aiogram method objects: any chain through ``aiogram.methods`` and any
    sending method object named or looked up by its class name."""
    aliases = module_aliases(tree, module=module)
    parent = parents(tree)
    owner = owners(tree)
    found = []

    def where(node: ast.AST) -> str:
        function = owner.get(id(node))
        return function.name if function else "<module>"

    for node in ast.walk(tree):
        up = parent.get(id(node))
        inner = isinstance(up, ast.Attribute) and up.value is node  # mid-chain
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and not inner
        ):
            resolved = resolve_attribute(node, aliases)
            dotted = ".".join(resolved) if resolved else ""
            if is_under(dotted, ["aiogram.methods"]):
                found.append(f"{where(node)}: {dotted}")
                continue
        if isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in BOT_METHOD_OBJECTS:
                found.append(f"{where(node)}: {name}")
        if isinstance(node, ast.Call) and _is_lookup(node.func):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value in BOT_METHOD_OBJECTS:
                    found.append(f"{where(node)}: {arg.value}")
    return found


def _top_level_assigns(tree: ast.Module) -> list[tuple[list[str], ast.expr]]:
    owner = owners(tree)
    found = []
    for node in ast.walk(tree):
        if id(node) in owner:
            continue
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            found.append((targets, node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                found.append(([node.target.id], node.value))
    return found


def sender_host_names(trees: Mapping[str, ast.Module]) -> dict[str, set[str]]:
    """Per module (dotted name), the names bound at module or class level to
    a bare sender host: a literal, another such name, or one imported from a
    module of ours that binds it. A fixpoint over all the modules."""
    bound: dict[str, set[str]] = {m: set() for m in trees}
    changed = True
    while changed:
        changed = False
        for module, tree in trees.items():
            mine = bound[module]
            aliases = module_aliases(tree, module=module)
            for targets, value in _top_level_assigns(tree):
                host = (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and SENDER_HOST.fullmatch(value.value.strip()) is not None
                )
                alias = bound_name(value, mine, aliases, bound)
                if set(targets) - mine and (host or alias):
                    mine.update(targets)
                    changed = True
            for imp in imports(tree, module=module):
                exported = bound.get(imp.module, set())
                if imp.name in exported and imp.alias not in mine:
                    mine.add(imp.alias)
                    changed = True
    return bound


def _host_offences(
    tree: ast.Module, module: str, bound: Mapping[str, set[str]]
) -> list[str]:
    mine = bound.get(module, set())
    aliases = module_aliases(tree, module=module)
    parent = parents(tree)
    defining: set[int] = set()  # the definitions of the host names themselves
    for targets, value in _top_level_assigns(tree):
        if set(targets) & mine:
            defining.update(id(n) for n in ast.walk(value))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        name = bound_name(node, mine, aliases, bound)
        if name is None or id(node) in defining or not isinstance(node, ast.expr):
            continue
        if not isinstance(parent.get(id(node)), CLASSIFYING):
            found.append((node.lineno, name))
    return [f"line {line}: builds on {name}" for line, name in sorted(found)]


def _channel_offences(
    tree: ast.Module, module: str = "", hosts: Mapping[str, set[str]] | None = None
) -> list[str]:
    names = imported_modules(tree, module=module)
    roots = {is_under(name, FORBIDDEN_IMPORTS) for name in names}
    offending = [f"imports {root}" for root in sorted(r for r in roots if r)]
    for text in texts(tree):
        if hit := OUTBOUND_TARGETS.search(text.value):
            offending.append(f"line {text.node.lineno}: names {hit.group(0)!r}")
    if hosts is None:
        hosts = sender_host_names({module: tree})
    return offending + _host_offences(tree, module, hosts)


def _requirement_names(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[\s\[<>=!~;]", line, maxsplit=1)[0]
        names.add(name.lower().replace("_", "-"))
    return names


def test_bot_api_sends_only_through_deliver_to_person():
    offending = {}
    for path in modules():
        found = _send_offences(rel(path), tree_of(path))
        if found:
            offending[rel(path)] = found
    assert offending == {}, (
        "a Bot API send outside bot/deliver.py::to_person can reach a chat that "
        f"is not the owner's or a manager's (06 §5.3): {offending}"
    )


def test_to_person_checks_the_owner_and_manager_allowlist():
    deliver = PACKAGE / DELIVER
    if not deliver.exists():
        # Until T-20 there is no wrapper, and the test above holds the package
        # to sending nothing at all.
        assert not any(_bot_send_sites(tree_of(p)) for p in modules())
        pytest.skip("bot/deliver.py arrives with T-20; nothing may send until then")
    tree = tree_of(deliver)
    [to_person] = [f for f in functions(tree) if f.name == DELIVER_ROOT]
    named = {n.attr for n in ast.walk(to_person) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(to_person) if isinstance(n, ast.Name)
    }
    assert {"owner_telegram_id", "manager_telegram_ids"} <= named
    assert any(isinstance(n, ast.Compare) for n in ast.walk(to_person))


def test_nothing_imports_or_names_a_mail_sms_or_messenger_route():
    trees = {dotted_name(p): tree_of(p) for p in modules()}
    hosts = sender_host_names(trees)
    offending = {}
    for path in modules():
        found = _channel_offences(tree_of(path), dotted_name(path), hosts)
        if found:
            offending[rel(path)] = found
    assert offending == {}


def test_no_sms_email_or_telephony_dependency():
    names = set().union(*(_requirement_names(p.read_text()) for p in REQUIREMENTS))
    assert {"telethon", "aiogram"} <= names  # the reading is the right one
    assert names & FORBIDDEN_DISTRIBUTIONS == set()


def test_the_outbound_detectors_catch_each_shape():
    def tree(source: str) -> ast.Module:
        return ast.parse(source)

    sender = (
        "from aiogram import Bot\n"
        "async def alert(bot: Bot, chat):\n"
        "    await bot.send_message(chat, 'x')\n"
        "async def later(bot):\n"
        "    return partial(bot.copy_message, 1, 2, 3)\n"
    )
    assert _send_offences("services/health.py", tree(sender)) == [
        "alert: send_message",
        "later: copy_message",
    ]
    # In deliver.py: the wrapper and a helper reached only from it may send;
    # a second door into the helper may not.
    deliver = (
        "async def to_person(bot, telegram_id, text):\n"
        "    if telegram_id not in allowed():\n"
        "        raise NotAllowed\n"
        "    await _send(bot, telegram_id, text)\n"
        "async def _send(bot, chat, text):\n"
        "    await bot.send_message(chat, text)\n"
    )
    assert _send_offences(DELIVER, tree(deliver)) == []
    leaky = deliver + "async def broadcast(bot, chat):\n    await _send(bot, chat, 'x')\n"
    assert _send_offences(DELIVER, tree(leaky)) == ["_send: send_message"]
    methods = "from aiogram.methods import SendMessage\n"
    assert _send_offences("bot/handlers/review.py", tree(methods)) == [
        "<module>: imports aiogram.methods"
    ]
    # The method objects reached without importing aiogram.methods by name.
    through_package = (
        "import aiogram\n"
        "import aiogram.types as t\n"
        "async def alert(bot, chat_id):\n"
        "    await bot(aiogram.methods.SendMessage(chat_id=chat_id, text='x'))\n"
        "async def later(bot, m, chat_id):\n"
        "    await bot(getattr(m, 'CopyMessage')(chat_id=chat_id))\n"
        "async def kept(bot, ns):\n"
        "    cls = ns.EditMessageText\n"
        "    return t.Message\n"
    )
    assert _send_offences("services/alerts.py", tree(through_package)) == [
        "alert: aiogram.methods.SendMessage",
        "kept: EditMessageText",
        "later: CopyMessage",
    ]
    assert _send_offences(DELIVER, tree(through_package)) == []
    shortcut = (
        "from aiogram.types import Message\n"
        "async def nudge(message: Message):\n"
        "    await message.answer('x')\n"
    )
    assert _send_offences("worker/main.py", tree(shortcut)) == ["nudge: answer"]
    assert _send_offences("bot/handlers/review.py", tree(shortcut)) == []

    channels = (
        "import smtplib\n"
        "from twilio.rest import Client\n"
        "URL = 'https://api.telegram.org/bot{token}/sendMessage'\n"
        "def wa(n):\n"
        "    return f'https://graph.facebook.com/v21.0/{n}/messages'\n"
        "def doc():\n"
        "    '''Never https://api.telegram.org directly.'''\n"
    )
    # An f-string is one template, and the docstring is prose.
    assert _channel_offences(tree(channels)) == [
        "imports smtplib",
        "imports twilio",
        "line 3: names 'https://api.telegram.org'",
        "line 5: names 'graph.facebook.com/v21.0/{}/messages'",
    ]
    discovery = "U = 'https://graph.facebook.com/v21.0/123?fields=business_discovery'\n"
    assert _channel_offences(tree(discovery)) == []
    # A host in a lookup table is no route; the same host as a URL is.
    table = "HUBS = frozenset({'wa.me', 'api.whatsapp.com'})\n"
    assert _channel_offences(tree(table)) == []
    links = (
        "A = 'https://wa.me/998900000001'\n"
        "B = 'wa.me/998900000002'\n"
        "C = 'smtp.example.uz'\n"
    )
    assert _channel_offences(tree(links)) == [
        "line 1: names 'https://wa.me'",
        "line 2: names 'wa.me/'",
        "line 3: names 'smtp.example.uz'",
    ]
    # A bare host is a table entry until a URL is built on it.
    built = (
        "TG_HOST = 'api.telegram.org'\n"
        "WA: str = 'wa.me'\n"
        "HUBS = frozenset({TG_HOST, WA})\n"
        "async def notify(http, token, host):\n"
        "    if host == TG_HOST or host in (WA, 'x'):\n"
        "        return None\n"
        "    return await http.post(f'https://{TG_HOST}/bot{token}/sendMessage')\n"
        "BASE = 'https://' + WA\n"
    )
    assert _channel_offences(tree(built)) == [
        "line 7: builds on TG_HOST",
        "line 8: builds on WA",
    ]
    # ... and across modules: imported by name or read through the module.
    hosts_module = "clientscrapper.services.hosts"
    trees = {
        hosts_module: tree("TG = 'api.telegram.org'\nALIAS = TG\n"),
        "clientscrapper.services.notify": tree(
            "from clientscrapper.services.hosts import ALIAS as H\n"
            "from clientscrapper.services import hosts\n"
            "import httpx\n"
            "def url(t):\n"
            "    return f'https://{H}/bot{t}/getMe', 'https://%s/x' % hosts.TG\n"
            "def fine(u):\n"
            "    return httpx.URL(u).host == hosts.TG\n"
        ),
    }
    hosts = sender_host_names(trees)
    assert hosts[hosts_module] == {"TG", "ALIAS"}
    notify = "clientscrapper.services.notify"
    assert _channel_offences(trees[notify], notify, hosts) == [
        "line 5: builds on H",
        "line 5: builds on TG",
    ]
    assert _requirement_names("twilio==9.0.0  # sms\nTelethon==1.44.0\n-r x.txt\n") == {
        "twilio",
        "telethon",
    }


def test_the_method_object_names_are_aiogram_s():
    """BOT_METHOD_OBJECTS is derived from BOT_SEND_CALLS; each derived class
    name must be a real aiogram method, or the fence guards a typo."""
    import aiogram.methods

    assert set(BOT_METHOD_OBJECTS) <= set(dir(aiogram.methods))
    assert {"SendMessage", "CopyMessage", "EditMessageText"} <= BOT_METHOD_OBJECTS
