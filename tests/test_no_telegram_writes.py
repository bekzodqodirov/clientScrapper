# VENDORED_FROM dailyJournal@6b1f67f tests/test_userbot.py
"""No code path writes to Telegram as the reader, or reaches anyone at all.

07 §1.2 and 06 §5.3. MIYA's userbot guard with its sender deleted: there is no
sender in v1, so ``SENDER_WRITE_FUNCTIONS`` is empty and the exemption branch
is gone — a write call may appear nowhere in the reader. Two raw requests are
allowed, each in one named function (the joiner and the ``about`` reader), and
history is read only where the owner asked for it.

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
from pathlib import Path

import pytest

from clientscrapper.config import settings
from clientscrapper.userbot import main as userbot
from tests.ast_tools import (
    PACKAGE,
    ROOT,
    called,
    functions,
    imported_modules,
    imports,
    is_under,
    modules,
    owners,
    reached_only_from,
    references,
    rel,
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


def _scanned() -> list[Path]:
    """Every module of clientscrapper/userbot (recursively) and every module
    of the package that imports Telethon."""
    paths = set(modules(USERBOT_PACKAGE))
    paths |= {p for p in modules() if _imports_telethon(p)}
    return sorted(paths)


SCANNED = _scanned()


def _offences(path_rel: str, source: str) -> list[str]:
    """What a reader module does that it must not."""
    offending = []
    for function, calls in _calls_by_function(source).items():
        writes = calls & WRITE_CALLS
        if writes:
            offending.append(f"{function or '<module>'}: {sorted(writes)}")
    for function, named in _raw_requests_by_function(source).items():
        allowed = RAW_ALLOWED.get((path_rel, function or ""), set())
        if named - allowed:
            offending.append(f"{function or '<module>'}: {sorted(named - allowed)}")
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
    for function, calls in _calls_by_function(path.read_text(encoding="utf-8")).items():
        offending = calls & HISTORY_CALLS
        if not offending or (rel(path), function) in HISTORY_ALLOWED:
            continue
        pytest.fail(
            f"{rel(path)}:{function or '<module>'} calls {sorted(offending)} — the "
            "reader follows live events and must not pull history on its own. "
            f"Only {sorted(HISTORY_ALLOWED)} may, to read what the owner asked for."
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


def _channel_offences(tree: ast.Module, module: str = "") -> list[str]:
    names = imported_modules(tree, module=module)
    roots = {is_under(name, FORBIDDEN_IMPORTS) for name in names}
    offending = [f"imports {root}" for root in sorted(r for r in roots if r)]
    for text in texts(tree):
        if hit := OUTBOUND_TARGETS.search(text.value):
            offending.append(f"line {text.node.lineno}: names {hit.group(0)!r}")
    return offending


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
    offending = {}
    for path in modules():
        found = _channel_offences(tree_of(path))
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
    assert _requirement_names("twilio==9.0.0  # sms\nTelethon==1.44.0\n-r x.txt\n") == {
        "twilio",
        "telethon",
    }
