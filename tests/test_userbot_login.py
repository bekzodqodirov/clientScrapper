"""``make userbot-login`` (06 §2; 07 T-13): the session string goes to the
operator's terminal and nowhere else — no file, no log record.

The owner's first install (2026-11-12): the code «arrived neither by SMS nor in
the app». The login is the tool's own now — the number must carry its country
code (a bare «90…» is +90, Turkey), the screen says where Telegram sent the
code, «qayta» re-sends it, wrong codes and passwords are counted, and every
refusal from Telegram is one Uzbek sentence and exit 1, never a traceback.

The fake client below is Telegram as the login sees it, scripted per test; the
SentCode values and the errors are Telethon's own classes from the pinned
version, so a name the tool relies on that Telethon does not have fails here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors
from telethon.tl.types import auth

from clientscrapper.config import settings
from clientscrapper.tools import userbot_login
from clientscrapper.tools.userbot_login import (
    CODE_EXPIRED,
    CODE_PROMPT,
    CONNECTION_LOST,
    EMPTY_PASSWORD,
    NO_COUNTRY_CODE,
    NOT_A_CODE,
    PASSWORD_PROMPT,
    PHONE_PROMPT,
    RESENDING,
    SEND_CODE_UNAVAILABLE,
    STOPPED,
    WORD_PROMPT,
    WRONG_CODE,
    WRONG_PASSWORD,
)

FAKE_SESSION = "1" + "N" * 40  # visibly fake, far shorter than a real one
PHONE = "+998930000011"  # synthetic (07 §1.0), not the prompt's example
DIGITS = PHONE.lstrip("+")
MASKED = "+99893*****11"


def sent_code(
    kind: Any = None,
    phone_code_hash: str = "h1",
    next_type: Any = None,
    timeout: int | None = None,
) -> auth.SentCode:
    return auth.SentCode(
        type=kind if kind is not None else auth.SentCodeTypeApp(length=5),
        phone_code_hash=phone_code_hash,
        next_type=next_type,
        timeout=timeout,
    )


class Exhausted(BaseException):
    """The tool asked once more than the test scripted. A BaseException, so
    the login's catch-all cannot turn it into a sentence and a green test."""


def script(answers: Iterable[str]) -> tuple[Callable[[str], str], list[str]]:
    """A terminal that types ``answers`` in order; the prompts it was shown."""
    pending = list(answers)
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        if not pending:
            raise Exhausted(prompt)
        answer = pending.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return read, prompts


def _outcome(value: Any) -> Any:
    if isinstance(value, BaseException):
        raise value
    return value


class LoginClient:
    """TelegramClient as the login uses it. ``sends``: what each
    send_code_request answers; ``resends``: what each raw ResendCodeRequest
    answers; ``codes`` / ``passwords``: what each sign_in answers (None =
    signed in). A value that is an exception is raised."""

    def __init__(
        self,
        session: Any = None,
        api_id: Any = None,
        api_hash: Any = None,
        *,
        sends: Iterable[Any] = (),
        resends: Iterable[Any] = (),
        codes: Iterable[Any] = (None,),
        passwords: Iterable[Any] = (),
    ) -> None:
        self.session = SimpleNamespace(save=self._save)
        self.calls: list[Any] = []
        self.sends = list(sends) or [sent_code()]
        self.resends = list(resends)
        self.codes = list(codes)
        self.passwords = list(passwords)
        self.before_send: Callable[[], None] | None = None
        self.before_get_me: Callable[[], None] | None = None
        self.get_me_error: BaseException | None = None
        self.disconnect_error: BaseException | None = None

    def _save(self) -> str:
        self.calls.append("save")
        return FAKE_SESSION

    async def connect(self) -> None:
        self.calls.append("connect")

    async def start(self, **prompts: Any) -> None:  # Telethon's, never used
        self.calls.append("start")

    async def send_code_request(self, phone: str) -> Any:
        if self.before_send:
            self.before_send()
        self.calls.append(("send_code_request", phone))
        return _outcome(self.sends.pop(0))

    async def __call__(self, request: Any) -> Any:
        self.calls.append(
            (type(request).__name__, request.phone_number, request.phone_code_hash)
        )
        return _outcome(self.resends.pop(0))

    async def sign_in(
        self,
        phone: str | None = None,
        code: str | None = None,
        *,
        password: str | None = None,
        phone_code_hash: str | None = None,
    ) -> Any:
        if password is not None:
            self.calls.append(("password", password))
            return _outcome(self.passwords.pop(0))
        self.calls.append(("sign_in", phone, code, phone_code_hash))
        return _outcome(self.codes.pop(0))

    async def get_me(self) -> Any:
        if self.before_get_me:
            self.before_get_me()
        self.calls.append("get_me")
        if self.get_me_error:
            raise self.get_me_error
        return SimpleNamespace(first_name="Namuna", id=5100000001)

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        if self.disconnect_error:
            raise self.disconnect_error

    def named(self, name: str) -> list[Any]:
        return [c for c in self.calls if isinstance(c, tuple) and c[0] == name]


async def login(
    client: LoginClient,
    answers: Iterable[Any] = (PHONE, "12345"),
    passwords: Iterable[Any] = (),
) -> tuple[bool, list[str]]:
    """Run the login against ``client``; the client is disconnected exactly
    once, last, whatever ended it."""
    read, prompts = script(answers)
    read_password, password_prompts = script(passwords)
    ok = await userbot_login._login(
        lambda *_a: client, read=read, read_password=read_password
    )
    assert client.calls.count("disconnect") == 1
    assert client.calls[-1] == "disconnect"
    return ok, prompts + password_prompts


def _no_traceback(captured: Any) -> None:
    assert "Traceback" not in captured.out + captured.err


# --- the session string -----------------------------------------------------------


async def test_the_session_is_printed_once_and_written_nowhere(
    capsys, caplog, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.DEBUG)
    ok, _ = await login(LoginClient())
    assert ok
    out = capsys.readouterr().out
    assert out.count(f"TELETHON_SESSION={FAKE_SESSION}") == 1
    assert "Kirildi: Namuna" in out
    assert os.listdir(tmp_path) == []
    assert all(FAKE_SESSION not in record.getMessage() for record in caplog.records)


async def test_the_session_is_printed_before_get_me(capsys):
    """Signing in minted an authorised session: its string goes out before
    the cosmetic get_me, which may fail or hang."""
    client = LoginClient()
    seen: list[str] = []
    client.before_get_me = lambda: seen.append(capsys.readouterr().out)
    ok, _ = await login(client)
    assert ok
    assert f"TELETHON_SESSION={FAKE_SESSION}" in seen[0]
    assert client.calls.index("save") < client.calls.index("get_me")
    assert "Kirildi: Namuna" in capsys.readouterr().out


async def test_a_failing_get_me_keeps_the_session(capsys):
    client = LoginClient()
    client.get_me_error = ConnectionError("gone")
    ok, _ = await login(client)
    assert ok
    out = capsys.readouterr().out
    assert f"TELETHON_SESSION={FAKE_SESSION}" in out
    assert "akkaunt nomini oʻqib boʻlmadi: ConnectionError" in out


def test_the_line_is_the_env_key():
    assert userbot_login.session_line("abc") == "TELETHON_SESSION=abc"


def test_without_the_app_credentials_it_stops_before_asking_anything(capsys, monkeypatch):
    monkeypatch.setattr(settings, "telethon_api_id", None)
    monkeypatch.setattr(settings, "telethon_api_hash", "")
    assert userbot_login.main() == 1
    err = capsys.readouterr().err
    assert "TELETHON_API_ID va TELETHON_API_HASH" in err and "my.telegram.org" in err


async def test_the_prompts_are_the_tools_own():
    """Never Telethon's start() and its English prompts, whose phone prompt
    says «(or bot token)» right after the owner has made a bot (the first
    docker dry run), and which never said where the code went (2026-11-12)."""
    client = LoginClient()
    ok, prompts = await login(client)
    assert ok
    assert "start" not in client.calls
    assert prompts == [PHONE_PROMPT, CODE_PROMPT]


# --- the phone number -------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "phone"),
    [
        ("+998901234567", "+998901234567"),
        ("998 90 123-45-67", "+998901234567"),
        ("+998(90)1234567", "+998901234567"),
        ("998901234567", "+998901234567"),
        ("  +998 90 123 45 67 ", "+998901234567"),
        # A reader SIM from elsewhere, with its own country code.
        ("+7 916 123-45-67", "+79161234567"),
    ],
)
def test_a_number_with_its_country_code_is_taken(typed, phone):
    assert userbot_login.ask_phone(lambda _prompt: typed) == phone


@pytest.mark.parametrize("typed", ["901234567", "90 123 45 67", "0901234567"])
def test_a_number_without_its_country_code_is_refused_naming_the_trap(typed, capsys):
    """«901234567» is +90… to Telegram: the code goes to a Turkish number."""
    answers = iter([typed, "+998901234567"])
    assert userbot_login.ask_phone(lambda _prompt: next(answers)) == "+998901234567"
    out = capsys.readouterr().out
    assert out.count(NO_COUNTRY_CODE) == 1
    assert "Turkiya" in out and "(+90)" in out and "+998901234567" in out


def test_a_bot_token_at_the_phone_prompt_is_refused_and_asked_again(capsys):
    """Telethon signs in as a bot when the phone prompt gets a token; the
    reader must be the SIM account, so the token is refused."""
    answers = iter(["123456789:AAAAfakefakefakefake", "abc", "+998901234567"])
    assert userbot_login.ask_phone(lambda _prompt: next(answers)) == "+998901234567"
    assert capsys.readouterr().out.count("Bu telefon raqami emas") == 2


@pytest.mark.parametrize("typed", ["+99890123456", "99890123456", "+9989012345678"])
def test_an_uzbek_number_of_the_wrong_length_is_refused(typed, capsys):
    answers = iter([typed, "+998901234567"])
    assert userbot_login.ask_phone(lambda _prompt: next(answers)) == "+998901234567"
    assert "+998 dan keyin 9 ta raqam" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("phone", "masked"),
    [("+998901234567", "+99890*****67"), ("+79161234567", "+79161****67")],
)
def test_the_number_is_masked_but_its_country_shows(phone, masked):
    assert userbot_login.mask_phone(phone) == masked


async def test_the_masked_number_is_shown_before_the_code_is_asked_for(capsys):
    client = LoginClient()
    seen: list[str] = []
    client.before_send = lambda: seen.append(capsys.readouterr().out)
    ok, _ = await login(client)
    assert ok
    assert MASKED in seen[0]
    assert client.named("send_code_request") == [("send_code_request", PHONE)]
    out = seen[0] + capsys.readouterr().out
    assert PHONE not in out and DIGITS not in out


# --- where the code went ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "words"),
    [
        (auth.SentCodeTypeApp(length=5), ["Telegram ilovasiga", "«Telegram» nomli chat"]),
        (auth.SentCodeTypeSms(length=5), ["SMS bilan yuborildi"]),
        (auth.SentCodeTypeFirebaseSms(length=5), ["SMS bilan yuborildi"]),
        (auth.SentCodeTypeSmsWord(beginning="o"), ["soʻz koʻrinishida", "(«o…»"]),
        (auth.SentCodeTypeSmsPhrase(), ["ibora koʻrinishida"]),
        (auth.SentCodeTypeCall(length=5), ["qoʻngʻirogʻi bilan aytiladi"]),
        (auth.SentCodeTypeFlashCall(pattern="*"), ["qisqa qoʻngʻiroq"]),
        (
            auth.SentCodeTypeMissedCall(prefix="+99871", length=4),
            ["oxirgi 4 ta raqami", "+99871…"],
        ),
        (
            auth.SentCodeTypeEmailCode(email_pattern="b***@example.com", length=6),
            ["e-pochtasiga", "b***@example.com"],
        ),
        (
            auth.SentCodeTypeFragmentSms(url="https://fragment.com/number/1", length=5),
            ["Fragment", "https://fragment.com/number/1"],
        ),
    ],
    ids=lambda v: type(v).__name__ if not isinstance(v, list) else "",
)
def test_each_way_telegram_sends_the_code_has_its_sentence(kind, words):
    lines = userbot_login.delivery(sent_code(kind))
    assert len(lines) == 1
    assert all(word in lines[0] for word in words), lines


async def test_the_app_sentence_reaches_the_screen(capsys):
    """The owner's case: the code goes to the «Telegram» chat of the app where
    the number is logged in, not to SMS."""
    ok, _ = await login(LoginClient(sends=[sent_code(auth.SentCodeTypeApp(length=5))]))
    assert ok
    assert userbot_login.SENT_APP in capsys.readouterr().out


def test_an_unknown_way_is_named_by_its_class():
    class SentCodeTypeSomethingNew:
        pass

    (line,) = userbot_login.delivery(SimpleNamespace(type=SentCodeTypeSomethingNew()))
    assert "(SentCodeTypeSomethingNew)" in line


def test_every_way_the_pinned_telethon_knows_has_its_own_sentence():
    """A Telethon upgrade that adds a SentCodeType or a CodeType fails here
    until it has words of its own."""
    for name in dir(auth):
        cls = getattr(auth, name)
        if name.startswith("SentCodeType"):
            sent = SimpleNamespace(type=object.__new__(cls))
            if name == "SentCodeTypeSetUpEmailRequired":
                with pytest.raises(userbot_login.Stop):
                    userbot_login.delivery(sent)
                continue
            (line,) = userbot_login.delivery(sent)
            assert f"({name})" not in line, name
        elif name.startswith("CodeType"):
            assert name in userbot_login.NEXT_WAYS, name


@pytest.mark.parametrize(
    ("next_type", "way"),
    [
        (auth.CodeTypeSms(), "SMS bilan"),
        (auth.CodeTypeCall(), "telefon qoʻngʻirogʻi bilan"),
        (auth.CodeTypeFlashCall(), "qisqa qoʻngʻiroq bilan"),
        (auth.CodeTypeMissedCall(), "qoʻngʻiroq bilan"),
        (auth.CodeTypeFragmentSms(), "Fragment orqali"),
    ],
    ids=lambda v: type(v).__name__ if not isinstance(v, str) else "",
)
def test_the_next_way_and_its_wait_are_said(next_type, way):
    first, hint = userbot_login.delivery(sent_code(next_type=next_type, timeout=60))
    assert first == userbot_login.SENT_APP
    assert hint == (
        f"Kod kelmasa, 60 soniyadan keyin «qayta» deb yoz — Telegram uni {way} "
        "qayta yuboradi."
    )


def test_no_next_way_no_hint_and_no_wait_no_seconds():
    assert userbot_login.delivery(sent_code()) == [userbot_login.SENT_APP]
    (_, hint) = userbot_login.delivery(sent_code(next_type=auth.CodeTypeSms()))
    assert "soniyadan" not in hint and "«qayta»" in hint


async def test_a_demand_for_a_login_email_stops_before_any_code_is_asked(capsys):
    client = LoginClient(sends=[sent_code(auth.SentCodeTypeSetUpEmailRequired())])
    ok, prompts = await login(client, answers=[PHONE])
    assert ok is False
    assert prompts == [PHONE_PROMPT]
    assert not client.named("sign_in")
    captured = capsys.readouterr()
    assert userbot_login.SETUP_EMAIL_REQUIRED in captured.err
    assert "rasmiy Telegram ilovasida" in captured.err
    _no_traceback(captured)


# --- «qayta» ------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["qayta", "Qayta", " QAYTA "])
async def test_qayta_resends_and_the_new_hash_signs_in(word, capsys):
    client = LoginClient(
        sends=[sent_code(phone_code_hash="h1", next_type=auth.CodeTypeSms())],
        resends=[sent_code(auth.SentCodeTypeSms(length=5), phone_code_hash="h2")],
    )
    ok, _ = await login(client, answers=[PHONE, word, "12345"])
    assert ok
    assert client.named("ResendCodeRequest") == [("ResendCodeRequest", DIGITS, "h1")]
    assert client.named("sign_in") == [("sign_in", PHONE, "12345", "h2")]
    out = capsys.readouterr().out
    assert RESENDING in out and userbot_login.SENT_SMS in out


async def test_an_expired_code_says_qayta_and_qayta_asks_for_a_new_one(capsys):
    """sign_in drops Telethon's copy of an expired hash; «qayta» then asks
    for a fresh code, not a resend of the dead one."""
    client = LoginClient(
        sends=[sent_code(phone_code_hash="h1"), sent_code(phone_code_hash="h3")],
        codes=[errors.PhoneCodeExpiredError(request=None), None],
    )
    ok, _ = await login(client, answers=[PHONE, "11111", "qayta", "22222"])
    assert ok
    assert not client.named("ResendCodeRequest")
    assert len(client.named("send_code_request")) == 2
    assert client.named("sign_in")[-1] == ("sign_in", PHONE, "22222", "h3")
    assert CODE_EXPIRED in capsys.readouterr().out


async def test_a_resend_on_an_expired_hash_asks_for_a_new_code():
    client = LoginClient(
        sends=[sent_code(phone_code_hash="h1"), sent_code(phone_code_hash="h4")],
        resends=[errors.PhoneCodeExpiredError(request=None)],
    )
    ok, _ = await login(client, answers=[PHONE, "qayta", "12345"])
    assert ok
    assert client.named("sign_in") == [("sign_in", PHONE, "12345", "h4")]


async def test_no_way_left_to_resend_stops_in_words(capsys):
    client = LoginClient(resends=[errors.SendCodeUnavailableError(request=None)])
    ok, _ = await login(client, answers=[PHONE, "qayta"])
    assert ok is False
    captured = capsys.readouterr()
    assert SEND_CODE_UNAVAILABLE in captured.err
    _no_traceback(captured)


# --- the code -----------------------------------------------------------------------


async def test_three_wrong_codes_stop_the_login(capsys):
    client = LoginClient(codes=[errors.PhoneCodeInvalidError(request=None)] * 3)
    ok, _ = await login(client, answers=[PHONE, "11111", "22222", "33333"])
    assert ok is False
    assert len(client.named("sign_in")) == 3
    assert "save" not in client.calls
    captured = capsys.readouterr()
    assert captured.out.count(WRONG_CODE) == 2
    assert "Kod 3 marta notoʻgʻri" in captured.err
    assert "TELETHON_SESSION=" not in captured.out


async def test_a_wrong_code_then_the_right_one_signs_in(capsys):
    client = LoginClient(codes=[errors.PhoneCodeInvalidError(request=None), None])
    ok, _ = await login(client, answers=[PHONE, "11111", "22222"])
    assert ok
    assert WRONG_CODE in capsys.readouterr().out


async def test_a_code_is_digits_and_spaces_go(capsys):
    """A word, an empty line or a pasted sentence never reaches Telegram (and
    never counts as a wrong code); «12 345» is 12345."""
    client = LoginClient()
    ok, _ = await login(client, answers=[PHONE, "abc", "", "kod 12345", "12 345"])
    assert ok
    assert client.named("sign_in") == [("sign_in", PHONE, "12345", "h1")]
    assert capsys.readouterr().out.count(NOT_A_CODE) == 3


async def test_a_code_sent_as_a_word_is_taken_as_typed():
    client = LoginClient(sends=[sent_code(auth.SentCodeTypeSmsWord(beginning="o"))])
    ok, prompts = await login(client, answers=[PHONE, "olma"])
    assert ok
    assert prompts == [PHONE_PROMPT, WORD_PROMPT]
    assert client.named("sign_in") == [("sign_in", PHONE, "olma", "h1")]


# --- 2FA ----------------------------------------------------------------------------


async def test_the_two_step_password_signs_in(capsys):
    client = LoginClient(
        codes=[errors.SessionPasswordNeededError(request=None)], passwords=[None]
    )
    ok, prompts = await login(client, passwords=["", "parol-namuna"])
    assert ok
    assert prompts[-2:] == [PASSWORD_PROMPT, PASSWORD_PROMPT]
    assert client.named("password") == [("password", "parol-namuna")]
    out = capsys.readouterr().out
    assert EMPTY_PASSWORD in out and "parol-namuna" not in out
    assert f"TELETHON_SESSION={FAKE_SESSION}" in out


async def test_three_wrong_passwords_stop_the_login(capsys):
    client = LoginClient(
        codes=[errors.SessionPasswordNeededError(request=None)],
        passwords=[errors.PasswordHashInvalidError(request=None)] * 3,
    )
    ok, _ = await login(client, passwords=["a", "b", "c"])
    assert ok is False
    assert len(client.named("password")) == 3
    captured = capsys.readouterr()
    assert captured.out.count(WRONG_PASSWORD) == 2
    assert "2FA paroli 3 marta notoʻgʻri" in captured.err
    assert "TELETHON_SESSION=" not in captured.out


# --- Telegram's refusals -------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "words"),
    [
        (errors.FloodWaitError(request=None, capture=45), ["45 soniya"]),
        (errors.FloodWaitError(request=None, capture=3600), ["60 daqiqa"]),
        (errors.FloodWaitError(request=None, capture=86400), ["24 soat"]),
        (errors.PhoneNumberFloodError(request=None), ["juda koʻp kod soʻraldi"]),
        (errors.PhoneNumberInvalidError(request=None), ["qabul qilmadi", "+998"]),
        (errors.PhoneNumberBannedError(request=None), ["bloklangan", "zaxira"]),
        (errors.PhoneNumberUnoccupiedError(request=None), ["akkaunti yoʻq"]),
        (errors.PhoneNumberAppSignupForbiddenError(request=None), ["akkaunti yoʻq"]),
        (errors.ApiIdInvalidError(request=None), ["TELETHON_API_ID", "my.telegram.org"]),
        (errors.ApiIdPublishedFloodError(request=None), ["eʼlon qilingan"]),
        (ConnectionError("Connection to Telegram failed 5 time(s)"), [CONNECTION_LOST]),
        (asyncio.IncompleteReadError(b"", 8), [CONNECTION_LOST]),
        (OSError("Network is unreachable"), [CONNECTION_LOST]),
        (RuntimeError("logged in right after sending the code"), ["(RuntimeError)"]),
        (errors.RPCError(None, "SOMETHING_NEW", 400), ["(RPCError: SOMETHING_NEW)"]),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else "",
)
async def test_a_refusal_when_the_code_is_asked_for_is_one_sentence(error, words, capsys):
    client = LoginClient(sends=[error])
    ok, _ = await login(client, answers=[PHONE])
    assert ok is False
    captured = capsys.readouterr()
    assert all(word in captured.err for word in words), captured.err
    assert "make userbot-login" in captured.err
    _no_traceback(captured)


async def test_an_account_without_telegram_is_never_signed_up(capsys):
    """sign_in raises PhoneNumberUnoccupied for a number with no account;
    the tool says to open it in the official app and stops."""
    client = LoginClient(codes=[errors.PhoneNumberUnoccupiedError(request=None)])
    ok, _ = await login(client)
    assert ok is False
    assert userbot_login.NO_ACCOUNT in capsys.readouterr().err


async def test_a_password_flood_is_one_sentence(capsys):
    client = LoginClient(
        codes=[errors.SessionPasswordNeededError(request=None)],
        passwords=[errors.PhonePasswordFloodError(request=None)],
    )
    ok, _ = await login(client, passwords=["a"])
    assert ok is False
    captured = capsys.readouterr()
    assert userbot_login.PASSWORD_FLOOD in captured.err
    _no_traceback(captured)


async def test_a_connection_lost_mid_flow_is_one_sentence(capsys):
    lost = ConnectionError("Cannot send requests while disconnected")
    client = LoginClient(codes=[lost])
    ok, _ = await login(client)
    assert ok is False
    captured = capsys.readouterr()
    assert CONNECTION_LOST in captured.err
    _no_traceback(captured)


class HangingClient(LoginClient):
    async def send_code_request(self, phone: str) -> Any:
        self.calls.append(("send_code_request", phone))
        await asyncio.sleep(3600)


async def test_a_request_that_never_answers_is_bounded(capsys, monkeypatch):
    monkeypatch.setattr(userbot_login, "REQUEST_TIMEOUT_SECONDS", 0.01)
    ok, _ = await login(HangingClient(), answers=[PHONE])
    assert ok is False
    assert CONNECTION_LOST in capsys.readouterr().err


async def test_the_end_of_input_at_the_code_prompt_is_a_stop(capsys):
    ok, _ = await login(LoginClient(), answers=[PHONE, EOFError()])
    assert ok is False
    captured = capsys.readouterr()
    assert STOPPED in captured.err
    _no_traceback(captured)


async def test_a_failing_disconnect_does_not_bury_the_result(capsys):
    client = LoginClient()
    client.disconnect_error = ConnectionError("already gone")
    ok, _ = await login(client)
    assert ok
    assert f"TELETHON_SESSION={FAKE_SESSION}" in capsys.readouterr().out


def test_a_refusal_ends_in_exit_1(monkeypatch):
    async def refused() -> bool:
        return False

    monkeypatch.setattr(settings, "telethon_api_id", 1)
    monkeypatch.setattr(settings, "telethon_api_hash", "x")
    monkeypatch.setattr(userbot_login, "_login", refused)
    assert userbot_login.main() == 1


def test_ctrl_c_ends_in_words_and_exit_1(capsys, monkeypatch):
    async def interrupted() -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(settings, "telethon_api_id", 1)
    monkeypatch.setattr(settings, "telethon_api_hash", "x")
    monkeypatch.setattr(userbot_login, "_login", interrupted)
    assert userbot_login.main() == 1
    captured = capsys.readouterr()
    assert STOPPED in captured.err
    _no_traceback(captured)


# --- no route to Telegram ------------------------------------------------------------


class SilentClient(LoginClient):
    async def connect(self) -> None:
        await asyncio.sleep(3600)


def test_no_route_to_telegram_ends_in_words_not_silence(capsys, monkeypatch):
    """Without a route to Telegram the login waited in silence (the first
    docker dry run): the connect is bounded and says what to check."""
    monkeypatch.setattr(userbot_login, "CONNECT_TIMEOUT_SECONDS", 0.01)
    client = SilentClient()
    assert asyncio.run(userbot_login._login(lambda *a: client)) is False
    assert not client.named("send_code_request") and "disconnect" in client.calls
    assert "Telegramga" in capsys.readouterr().err
