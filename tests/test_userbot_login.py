"""``make userbot-login`` (06 §2; 07 T-13): the session string goes to the
operator's terminal and nowhere else — no file, no log record.

The owner's first install (2026-11-12): the code «arrived neither by SMS nor in
the app», and the tool could not say where it went. Why is not verified: a
number typed without +998 (Telegram reads «90…» as +90, Turkey) is one
plausible cause; a code delivered to the app's «Telegram» chat, perhaps under
another account there, and SMS that Telegram withholds from third-party
clients are likelier ones. The login is the tool's own now — the number must
carry its country code, the screen says where Telegram sent the code and what
to do when it does not come, «qayta» re-sends it only when Telegram offers
another way and its wait is over, a code sent to the login e-mail signs in
through auth.signIn's e-mail field, wrong codes and passwords are counted, and
every refusal from Telegram is one Uzbek sentence and exit 1, never a
traceback.

The fake client below is Telegram as the login sees it, scripted per test; the
SentCode values and the errors are Telethon's own classes from the pinned
version, so a name the tool relies on that Telethon does not have fails here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pty
import select
import signal
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors
from telethon.tl.functions.auth import ResendCodeRequest, SignInRequest
from telethon.tl.types import auth

from clientscrapper.config import settings
from clientscrapper.tools import userbot_login
from clientscrapper.tools.userbot_login import (
    APP_ACCOUNTS,
    CODE_EXPIRED,
    CODE_PROMPT,
    CODE_PROMPT_RESEND,
    CONNECTION_LOST,
    DO_NOT_SHARE,
    EMPTY_PASSWORD,
    FOREIGN_REFUSED,
    LATE_SIGN_IN,
    NO_ACCOUNT,
    NO_CONNECTION,
    NO_COUNTRY_CODE,
    NO_OTHER_WAY,
    NOT_A_CODE,
    NOT_A_PHONE,
    NOT_A_WORD,
    OFFICIAL_APP_FIRST,
    PASSWORD_PROMPT,
    PHONE_PROMPT,
    RESEND_UNAVAILABLE,
    RESENDING,
    SENT_APP,
    SENT_SMS,
    SMS_MAY_NOT_COME,
    STOPPED,
    UPDATE_APP,
    WORD_PROMPT,
    WORD_PROMPT_RESEND,
    WRONG_CODE,
    WRONG_PASSWORD,
)

FAKE_SESSION = "1" + "N" * 40  # visibly fake, far shorter than a real one
PHONE = "+998930000011"  # synthetic (07 §1.0), not the prompt's example
DIGITS = PHONE.lstrip("+")
MASKED = "+99893*****11"
ROOT = Path(__file__).resolve().parents[1]
LOGIN_DEADLINE_SECONDS = 20


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


class Clock:
    """The clock «qayta» is timed by (userbot_login._now), moved by the
    script."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Later:
    """Typed ``answer`` after ``seconds`` more on ``clock``."""

    clock: Clock
    seconds: float
    answer: Any


def script(answers: Iterable[Any]) -> tuple[Callable[[str], str], list[str]]:
    """A terminal that types ``answers`` in order; the prompts it was shown."""
    pending = list(answers)
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        if not pending:
            raise Exhausted(prompt)
        answer = pending.pop(0)
        if isinstance(answer, Later):
            answer.clock.t += answer.seconds
            answer = answer.answer
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return read, prompts


def _outcome(value: Any) -> Any:
    if isinstance(value, BaseException):
        raise value
    return value


ME = SimpleNamespace(first_name="Namuna", id=5100000001)


class LoginClient:
    """TelegramClient as the login uses it. ``sends``: what each
    send_code_request answers; ``resends``: what each raw ResendCodeRequest
    answers; ``codes``: what each code sign-in answers, Telethon's sign_in or
    the raw e-mail SignInRequest (None = signed in); ``passwords``: what each
    2FA sign_in answers. A value that is an exception is raised. ``hang``:
    the calls that never answer."""

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
        hang: Iterable[str] = (),
    ) -> None:
        self.session = SimpleNamespace(save=self._save)
        self.calls: list[Any] = []
        self.sends = list(sends) or [sent_code()]
        self.resends = list(resends)
        self.codes = list(codes)
        self.passwords = list(passwords)
        self.hang = set(hang)
        self.me: Any = ME
        self.before_send: Callable[[], None] | None = None
        self.before_get_me: Callable[[], None] | None = None
        self.get_me_error: BaseException | None = None
        self.connect_error: BaseException | None = None
        self.disconnect_error: BaseException | None = None

    def _save(self) -> str:
        self.calls.append("save")
        return FAKE_SESSION

    async def _maybe_hang(self, what: str) -> None:
        if what in self.hang:
            await asyncio.sleep(3600)

    async def connect(self) -> None:
        self.calls.append("connect")
        if self.connect_error:
            raise self.connect_error

    async def start(self, **prompts: Any) -> None:  # Telethon's, never used
        self.calls.append("start")

    async def send_code_request(self, phone: str) -> Any:
        if self.before_send:
            self.before_send()
        self.calls.append(("send_code_request", phone))
        await self._maybe_hang("send_code_request")
        return _outcome(self.sends.pop(0))

    async def __call__(self, request: Any) -> Any:
        if isinstance(request, ResendCodeRequest):
            self.calls.append(
                ("ResendCodeRequest", request.phone_number, request.phone_code_hash)
            )
            await self._maybe_hang("ResendCodeRequest")
            return _outcome(self.resends.pop(0))
        if isinstance(request, SignInRequest):
            verification = request.email_verification
            self.calls.append(
                (
                    "SignInRequest",
                    request.phone_number,
                    request.phone_code_hash,
                    request.phone_code,
                    type(verification).__name__,
                    getattr(verification, "code", None),
                )
            )
            await self._maybe_hang("SignInRequest")
            return _outcome(self.codes.pop(0))
        raise AssertionError(f"unexpected raw request {type(request).__name__}")

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
            await self._maybe_hang("password")
            return _outcome(self.passwords.pop(0))
        self.calls.append(("sign_in", phone, code, phone_code_hash))
        await self._maybe_hang("sign_in")
        return _outcome(self.codes.pop(0))

    async def get_me(self) -> Any:
        if self.before_get_me:
            self.before_get_me()
        self.calls.append("get_me")
        await self._maybe_hang("get_me")
        if self.get_me_error:
            raise self.get_me_error
        return self.me

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
    # The test's own deadline: a bound the tool lost is a failure here, not a
    # test that hangs for the hour a fake call sleeps.
    ok = await asyncio.wait_for(
        userbot_login._login(lambda *_a: client, read=read, read_password=read_password),
        LOGIN_DEADLINE_SECONDS,
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


async def test_a_hanging_get_me_is_bounded_and_keeps_the_session(capsys, monkeypatch):
    monkeypatch.setattr(userbot_login, "REQUEST_TIMEOUT_SECONDS", 0.01)
    ok, _ = await login(LoginClient(hang={"get_me"}))
    assert ok
    out = capsys.readouterr().out
    assert f"TELETHON_SESSION={FAKE_SESSION}" in out
    assert "akkaunt nomini oʻqib boʻlmadi: TimeoutError" in out


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
        ("+998.90.123.45.67", "+998901234567"),
        # The international «00» prefix is a «+».
        ("00998901234567", "+998901234567"),
        ("00 998 90 123 45 67", "+998901234567"),
    ],
)
def test_a_number_with_its_country_code_is_taken(typed, phone):
    read, prompts = script([typed])  # a refusal asks again: Exhausted, at once
    assert userbot_login.ask_phone(read) == phone
    assert prompts == [PHONE_PROMPT]


@pytest.mark.parametrize(
    "typed",
    [
        "901234567",
        "90 123 45 67",
        "0901234567",
        # The plus put on, the 998 left off: the trap walked back into.
        "+90 123 45 67",
        "+901234567",
        "+93 123-45-67",
        "+33 123 45 67",
        "00901234567",
    ],
)
def test_a_number_without_its_country_code_is_refused_naming_the_trap(typed, capsys):
    """«901234567» is +90… to Telegram: another country's number, not the
    reader's (whether that is what happened on 2026-11-12 is not verified)."""
    answers = iter([typed, "+998901234567"])
    assert userbot_login.ask_phone(lambda _prompt: next(answers)) == "+998901234567"
    out = capsys.readouterr().out
    assert out.count(NO_COUNTRY_CODE) == 1
    assert "Turkiya" in out and "+90" in out and "+998901234567" in out
    # Said as a reading of the number, not as a fact about the code.
    assert "boshqa raqamga yuboradi" not in NO_COUNTRY_CODE


def test_a_number_of_another_country_is_taken_only_after_ha(capsys):
    prompts: list[str] = []
    answers = iter(["+7 916 123-45-67", "yoʻq", "+7 916 123-45-67", " Ha "])

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    assert userbot_login.ask_phone(read) == "+79161234567"
    assert prompts[0] == prompts[2] == PHONE_PROMPT
    assert "+79161****67" in prompts[1] and "Oʻzbekiston raqami emas" in prompts[1]
    assert prompts[3] == prompts[1]
    assert capsys.readouterr().out.count(FOREIGN_REFUSED) == 1


async def test_a_foreign_number_without_ha_never_reaches_telegram():
    client = LoginClient()
    with pytest.raises(Exhausted):
        await userbot_login._login(
            lambda *_a: client,
            read=script(["+90 532 123 45 67", "yoq"])[0],
            read_password=script([])[0],
        )
    assert not client.named("send_code_request")


def test_a_bot_token_at_the_phone_prompt_is_refused_and_asked_again(capsys):
    """Telethon signs in as a bot when the phone prompt gets a token; the
    reader must be the SIM account, so the token is refused."""
    answers = iter(["123456789:AAAAfakefakefakefake", "abc", "+998901234567"])
    assert userbot_login.ask_phone(lambda _prompt: next(answers)) == "+998901234567"
    assert capsys.readouterr().out.count("Bu telefon raqami emas") == 2


def test_digits_of_another_script_are_not_a_number(capsys):
    """Telethon passes «٩٩٨…» on unchanged; only ASCII digits are taken."""
    answers = iter(["+٩٩٨٩٠١٢٣٤٥٦٧", "+998901234567"])
    assert userbot_login.ask_phone(lambda _prompt: next(answers)) == "+998901234567"
    assert NOT_A_PHONE in capsys.readouterr().out


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
        (
            auth.SentCodeTypeFirebaseSms(length=5),
            ["faqat rasmiy ilova orqali", "SMS kelmaydi"],
        ),
        (auth.SentCodeTypeSmsWord(beginning="o"), ["soʻz koʻrinishida", "(«o…»"]),
        (auth.SentCodeTypeSmsPhrase(), ["ibora koʻrinishida"]),
        (auth.SentCodeTypeCall(length=5), ["qoʻngʻirogʻi bilan aytiladi"]),
        (
            auth.SentCodeTypeFlashCall(pattern="*"),
            ["qisqa qoʻngʻiroq", "raqamning oʻzi", "+ siz"],
        ),
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
    assert all(word in lines[0] for word in words), lines


def test_the_flash_call_is_never_told_to_be_the_last_digits():
    """For a flash call the code is the whole calling number (api/auth); «the
    last digits» is the missed-call rule."""
    (line,) = userbot_login.delivery(sent_code(auth.SentCodeTypeFlashCall(pattern="*")))
    assert "oxirgi" not in line


def test_an_app_code_says_to_switch_to_the_reader_account():
    """The official app may hold several accounts; the code is in the reader
    account's «Telegram» chat, not the one on the screen."""
    assert userbot_login.delivery(sent_code()) == [SENT_APP, APP_ACCOUNTS]
    assert "oʻquvchi akkauntga oʻt" in APP_ACCOUNTS


@pytest.mark.parametrize(
    "kind",
    [
        auth.SentCodeTypeSms(length=5),
        auth.SentCodeTypeSmsWord(),
        auth.SentCodeTypeSmsPhrase(),
        auth.SentCodeTypeFirebaseSms(length=5),
    ],
    ids=lambda v: type(v).__name__,
)
def test_an_sms_code_says_it_may_not_come_and_what_then(kind):
    """api/auth: in some conditions only official apps get the SMS; logged in
    there, the next code comes to the app."""
    lines = userbot_login.delivery(sent_code(kind))
    assert lines.count(OFFICIAL_APP_FIRST) == 1
    assert "rasmiy Telegram ilovasida" in OFFICIAL_APP_FIRST
    if type(kind).__name__ != "SentCodeTypeFirebaseSms":
        assert lines.count(SMS_MAY_NOT_COME) == 1


async def test_the_app_sentence_reaches_the_screen(capsys):
    """The code goes to the «Telegram» chat of the app where the number is
    logged in, not to SMS — and is typed by hand, never sent on."""
    ok, _ = await login(LoginClient(sends=[sent_code(auth.SentCodeTypeApp(length=5))]))
    assert ok
    out = capsys.readouterr().out
    assert SENT_APP in out and APP_ACCOUNTS in out
    assert out.count(DO_NOT_SHARE) == 1
    assert "bekor qiladi" in DO_NOT_SHARE


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
            lines = userbot_login.delivery(sent)
            assert f"({name})" not in lines[0], name
        elif name.startswith("CodeType"):
            assert name in userbot_login.NEXT_WAYS, name


def _hint(lines: list[str]) -> str:
    (hint,) = [line for line in lines if line.startswith("Kod kelmasa")]
    return hint


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
    lines = userbot_login.delivery(sent_code(next_type=next_type, timeout=60))
    assert lines[:2] == [SENT_APP, APP_ACCOUNTS]
    assert _hint(lines) == (
        f"Kod kelmasa, 60 soniyadan keyin «qayta» deb yoz — Telegram uni {way} "
        "qayta yuboradi."
    )


def test_no_next_way_no_hint_and_no_wait_no_seconds():
    assert userbot_login.delivery(sent_code()) == [SENT_APP, APP_ACCOUNTS]
    hint = _hint(userbot_login.delivery(sent_code(next_type=auth.CodeTypeSms())))
    assert "soniyadan" not in hint and "«qayta»" in hint


def test_an_sms_resend_promise_carries_the_sms_caveat_once():
    """«qayta … SMS bilan» promises an SMS a third-party client may never get;
    after an app code the app is where to look, so no official-app advice."""
    sms = userbot_login.delivery(
        sent_code(auth.SentCodeTypeCall(length=5), next_type=auth.CodeTypeSms())
    )
    assert sms.count(SMS_MAY_NOT_COME) == 1 and sms.count(OFFICIAL_APP_FIRST) == 1
    twice = userbot_login.delivery(
        sent_code(auth.SentCodeTypeSms(length=5), next_type=auth.CodeTypeSms())
    )
    assert twice.count(SMS_MAY_NOT_COME) == 1 and twice.count(OFFICIAL_APP_FIRST) == 1
    app = userbot_login.delivery(sent_code(next_type=auth.CodeTypeSms()))
    assert OFFICIAL_APP_FIRST not in app


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
    ok, prompts = await login(client, answers=[PHONE, word, "12345"])
    assert ok
    assert prompts == [PHONE_PROMPT, CODE_PROMPT_RESEND, CODE_PROMPT]
    assert client.named("ResendCodeRequest") == [("ResendCodeRequest", DIGITS, "h1")]
    assert client.named("sign_in") == [("sign_in", PHONE, "12345", "h2")]
    out = capsys.readouterr().out
    assert RESENDING in out and SENT_SMS in out


async def test_qayta_is_not_offered_and_asks_nothing_without_another_way(capsys):
    """No next_type: Telegram said there is no other way. «qayta» then makes
    no request; the code already sent still signs in."""
    client = LoginClient()
    ok, prompts = await login(client, answers=[PHONE, "qayta", "12345"])
    assert ok
    assert prompts == [PHONE_PROMPT, CODE_PROMPT, CODE_PROMPT]
    assert "qayta" not in CODE_PROMPT
    assert not client.named("ResendCodeRequest")
    assert len(client.named("send_code_request")) == 1
    assert NO_OTHER_WAY in capsys.readouterr().out
    assert client.named("sign_in") == [("sign_in", PHONE, "12345", "h1")]


async def test_qayta_before_the_wait_is_over_asks_nothing(capsys, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(userbot_login, "_now", clock)
    client = LoginClient(
        sends=[sent_code(next_type=auth.CodeTypeSms(), timeout=60)],
        resends=[sent_code(auth.SentCodeTypeSms(length=5), phone_code_hash="h2")],
    )
    answers = [PHONE, Later(clock, 10, "qayta"), Later(clock, 51, "qayta"), "12345"]
    ok, _ = await login(client, answers=answers)
    assert ok
    out = capsys.readouterr().out
    assert "yana 50 soniya kut" in out
    assert client.named("ResendCodeRequest") == [("ResendCodeRequest", DIGITS, "h1")]
    assert client.named("sign_in") == [("sign_in", PHONE, "12345", "h2")]


async def test_a_refused_resend_keeps_the_code_already_sent(capsys):
    """SEND_CODE_UNAVAILABLE on a resend: the code sent first may still be
    valid — one sentence, keep asking for it, never another request."""
    client = LoginClient(
        sends=[sent_code(next_type=auth.CodeTypeSms())],
        resends=[errors.SendCodeUnavailableError(request=None)],
    )
    ok, prompts = await login(client, answers=[PHONE, "qayta", "qayta", "12345"])
    assert ok
    assert prompts == [PHONE_PROMPT, CODE_PROMPT_RESEND, CODE_PROMPT, CODE_PROMPT]
    assert len(client.named("ResendCodeRequest")) == 1
    assert client.named("sign_in") == [("sign_in", PHONE, "12345", "h1")]
    captured = capsys.readouterr()
    assert RESEND_UNAVAILABLE in captured.out and NO_OTHER_WAY in captured.out
    assert "TELETHON_SESSION=" in captured.out
    _no_traceback(captured)


async def test_a_flood_wait_on_a_resend_keeps_the_code_and_times_the_next(
    capsys, monkeypatch
):
    clock = Clock()
    monkeypatch.setattr(userbot_login, "_now", clock)
    client = LoginClient(
        sends=[sent_code(next_type=auth.CodeTypeSms())],
        resends=[errors.FloodWaitError(request=None, capture=300)],
    )
    answers = [PHONE, "qayta", Later(clock, 60, "qayta"), "12345"]
    ok, _ = await login(client, answers=answers)
    assert ok
    out = capsys.readouterr().out
    assert "5 daqiqa kutishni soʻradi" in out and "yana 240 soniya kut" in out
    assert len(client.named("ResendCodeRequest")) == 1


async def test_an_expired_code_says_qayta_and_qayta_asks_for_a_new_one(capsys):
    """sign_in drops Telethon's copy of an expired hash; «qayta» then asks
    for a fresh code, not a resend of the dead one — offered even when there
    was no other way."""
    client = LoginClient(
        sends=[sent_code(phone_code_hash="h1"), sent_code(phone_code_hash="h3")],
        codes=[errors.PhoneCodeExpiredError(request=None), None],
    )
    ok, prompts = await login(client, answers=[PHONE, "11111", "qayta", "22222"])
    assert ok
    assert prompts == [PHONE_PROMPT, CODE_PROMPT, CODE_PROMPT_RESEND, CODE_PROMPT]
    assert not client.named("ResendCodeRequest")
    assert len(client.named("send_code_request")) == 2
    assert client.named("sign_in")[-1] == ("sign_in", PHONE, "22222", "h3")
    out = capsys.readouterr().out
    assert CODE_EXPIRED in out
    assert "chatga yuborilgan kodni" in CODE_EXPIRED


async def test_after_a_fresh_code_qayta_is_a_resend_again():
    """The fresh code after an expired one is a code like any other: the next
    «qayta» re-sends it, not another fresh auth.sendCode."""
    client = LoginClient(
        sends=[
            sent_code(phone_code_hash="h1"),
            sent_code(phone_code_hash="h3", next_type=auth.CodeTypeSms()),
        ],
        resends=[sent_code(auth.SentCodeTypeSms(length=5), phone_code_hash="h5")],
        codes=[errors.PhoneCodeExpiredError(request=None), None],
    )
    ok, _ = await login(client, answers=[PHONE, "11111", "qayta", "qayta", "22222"])
    assert ok
    assert len(client.named("send_code_request")) == 2
    assert client.named("ResendCodeRequest") == [("ResendCodeRequest", DIGITS, "h3")]
    assert client.named("sign_in")[-1] == ("sign_in", PHONE, "22222", "h5")


async def test_a_resend_on_an_expired_hash_asks_for_a_new_code():
    client = LoginClient(
        sends=[
            sent_code(phone_code_hash="h1", next_type=auth.CodeTypeSms()),
            sent_code(phone_code_hash="h4"),
        ],
        resends=[errors.PhoneCodeExpiredError(request=None)],
    )
    ok, _ = await login(client, answers=[PHONE, "qayta", "12345"])
    assert ok
    assert client.named("sign_in") == [("sign_in", PHONE, "12345", "h4")]


async def test_a_resend_that_switches_to_a_word_code_switches_the_prompt():
    client = LoginClient(
        sends=[sent_code(next_type=auth.CodeTypeSms())],
        resends=[
            sent_code(auth.SentCodeTypeSmsWord(beginning="o"), phone_code_hash="h2")
        ],
    )
    ok, prompts = await login(client, answers=[PHONE, "qayta", "olma"])
    assert ok
    assert prompts == [PHONE_PROMPT, CODE_PROMPT_RESEND, WORD_PROMPT]
    assert client.named("sign_in") == [("sign_in", PHONE, "olma", "h2")]


async def test_a_hanging_resend_is_bounded(capsys, monkeypatch):
    monkeypatch.setattr(userbot_login, "REQUEST_TIMEOUT_SECONDS", 0.01)
    client = LoginClient(sends=[sent_code(next_type=auth.CodeTypeSms())])
    client.hang = {"ResendCodeRequest"}
    ok, _ = await login(client, answers=[PHONE, "qayta"])
    assert ok is False
    assert CONNECTION_LOST in capsys.readouterr().err


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
    never counts as a wrong code); «12 345» and «12-345» are 12345."""
    client = LoginClient(codes=[None])
    ok, _ = await login(client, answers=[PHONE, "abc", "", "kod 12345", "12 345"])
    assert ok
    assert client.named("sign_in") == [("sign_in", PHONE, "12345", "h1")]
    assert capsys.readouterr().out.count(NOT_A_CODE) == 3
    dashed = LoginClient()
    assert (await login(dashed, answers=[PHONE, "12-345"]))[0]
    assert dashed.named("sign_in") == [("sign_in", PHONE, "12345", "h1")]


async def test_a_code_sent_as_a_word_is_taken_as_typed(capsys):
    """An empty line at the word prompt never reaches Telegram, and is told
    to type the word — not «digits only»."""
    client = LoginClient(sends=[sent_code(auth.SentCodeTypeSmsWord(beginning="o"))])
    ok, prompts = await login(client, answers=[PHONE, "", "olma"])
    assert ok
    assert prompts == [PHONE_PROMPT, WORD_PROMPT, WORD_PROMPT]
    assert client.named("sign_in") == [("sign_in", PHONE, "olma", "h1")]
    out = capsys.readouterr().out
    assert NOT_A_WORD in out and NOT_A_CODE not in out
    assert "qayta" not in WORD_PROMPT and "qayta" in WORD_PROMPT_RESEND


async def test_a_flash_call_code_is_the_calling_number_without_its_plus():
    client = LoginClient(sends=[sent_code(auth.SentCodeTypeFlashCall(pattern="*"))])
    ok, _ = await login(client, answers=[PHONE, "+998 71 200-00-00"])
    assert ok
    assert client.named("sign_in") == [("sign_in", PHONE, "998712000000", "h1")]


async def test_a_hanging_sign_in_is_bounded(capsys, monkeypatch):
    monkeypatch.setattr(userbot_login, "SIGN_IN_TIMEOUT_SECONDS", 0.01)
    client = LoginClient(hang={"sign_in"})
    client.me = None  # the late check: not logged in
    ok, _ = await login(client)
    assert ok is False
    assert CONNECTION_LOST in capsys.readouterr().err


def test_a_sign_in_is_given_time_for_telethons_own_flood_sleeps():
    """sign_in is up to five requests, each of which Telethon may sleep
    through a 60 s flood wait for."""
    assert userbot_login.SIGN_IN_TIMEOUT_SECONDS >= 5 * 60
    assert userbot_login.REQUEST_TIMEOUT_SECONDS > 60


# --- a sign-in whose answer never came ----------------------------------------------


@pytest.mark.parametrize(
    "lost", [TimeoutError(), ConnectionError("Connection reset")], ids=type
)
async def test_a_late_sign_in_that_went_through_still_prints_the_session(lost, capsys):
    """The bound ran out (or the line broke) after auth.signIn was sent; the
    account is in all the same: its session is printed, never left an orphan."""
    client = LoginClient(codes=[lost])
    ok, _ = await login(client)
    assert ok
    out = capsys.readouterr().out
    assert LATE_SIGN_IN in out
    assert out.count(f"TELETHON_SESSION={FAKE_SESSION}") == 1


async def test_a_late_sign_in_that_did_not_go_through_is_a_lost_line(capsys):
    client = LoginClient(codes=[TimeoutError()])
    client.me = None
    ok, _ = await login(client)
    assert ok is False
    captured = capsys.readouterr()
    assert CONNECTION_LOST in captured.err
    assert "TELETHON_SESSION=" not in captured.out and LATE_SIGN_IN not in captured.out


async def test_no_late_check_before_any_sign_in_was_sent(capsys, monkeypatch):
    monkeypatch.setattr(userbot_login, "REQUEST_TIMEOUT_SECONDS", 0.01)
    client = LoginClient(hang={"send_code_request"})
    ok, _ = await login(client, answers=[PHONE])
    assert ok is False
    assert "get_me" not in client.calls
    assert CONNECTION_LOST in capsys.readouterr().err


# --- a code sent to the login e-mail -------------------------------------------------

EMAIL = auth.SentCodeTypeEmailCode(email_pattern="b***@example.com", length=6)


async def test_an_email_code_signs_in_through_email_verification(capsys):
    """auth.signIn takes an e-mail code in email_verification; phone_code is
    documented as the code from the phone (Telethon's sign_in fills only
    that one)."""
    client = LoginClient(sends=[sent_code(EMAIL, phone_code_hash="h1")])
    ok, _ = await login(client, answers=[PHONE, "123 456"])
    assert ok
    assert not client.named("sign_in")
    assert client.named("SignInRequest") == [
        ("SignInRequest", DIGITS, "h1", None, "EmailVerificationCode", "123456")
    ]
    assert f"TELETHON_SESSION={FAKE_SESSION}" in capsys.readouterr().out


async def test_an_email_code_for_a_number_with_no_account_is_never_a_sign_up(capsys):
    client = LoginClient(
        sends=[sent_code(EMAIL)], codes=[auth.AuthorizationSignUpRequired()]
    )
    ok, _ = await login(client, answers=[PHONE, "123456"])
    assert ok is False
    assert NO_ACCOUNT in capsys.readouterr().err


async def test_an_email_code_with_2fa_asks_the_password(capsys):
    client = LoginClient(
        sends=[sent_code(EMAIL)],
        codes=[errors.SessionPasswordNeededError(request=None)],
        passwords=[None],
    )
    ok, _ = await login(client, answers=[PHONE, "123456"], passwords=["parol-namuna"])
    assert ok
    assert client.named("password") == [("password", "parol-namuna")]


@pytest.mark.parametrize(
    "wrong",
    [errors.PhoneCodeInvalidError(request=None), errors.CodeInvalidError(request=None)],
    ids=type,
)
async def test_a_wrong_email_code_counts_as_a_wrong_code(wrong, capsys):
    client = LoginClient(sends=[sent_code(EMAIL)], codes=[wrong, None])
    ok, _ = await login(client, answers=[PHONE, "111111", "222222"])
    assert ok
    assert WRONG_CODE in capsys.readouterr().out


async def test_an_expired_email_code_says_qayta(capsys):
    client = LoginClient(
        sends=[sent_code(EMAIL), sent_code(EMAIL, phone_code_hash="h2")],
        codes=[errors.EmailVerifyExpiredError(request=None), None],
    )
    ok, _ = await login(client, answers=[PHONE, "111111", "qayta", "222222"])
    assert ok
    assert CODE_EXPIRED in capsys.readouterr().out
    assert client.named("SignInRequest")[-1][2] == "h2"


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
    typed = ["parol-birinchi", "parol-ikkinchi", "parol-uchinchi"]
    ok, _ = await login(client, passwords=typed)
    assert ok is False
    assert len(client.named("password")) == 3
    captured = capsys.readouterr()
    assert captured.out.count(WRONG_PASSWORD) == 2
    assert "2FA paroli 3 marta notoʻgʻri" in captured.err
    assert "TELETHON_SESSION=" not in captured.out
    # A wrong password is never echoed back.
    assert not any(word in captured.out + captured.err for word in typed)


async def test_a_hanging_password_sign_in_is_bounded(capsys, monkeypatch):
    monkeypatch.setattr(userbot_login, "SIGN_IN_TIMEOUT_SECONDS", 0.01)
    client = LoginClient(codes=[errors.SessionPasswordNeededError(request=None)])
    client.hang = {"password"}
    client.me = None
    ok, _ = await login(client, passwords=["parol-namuna"])
    assert ok is False
    assert CONNECTION_LOST in capsys.readouterr().err


# --- Telegram's refusals -------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "words"),
    [
        (errors.FloodWaitError(request=None, capture=45), ["45 soniya"]),
        (errors.FloodWaitError(request=None, capture=119), ["119 soniya"]),
        (errors.FloodWaitError(request=None, capture=120), ["2 daqiqa"]),
        (errors.FloodWaitError(request=None, capture=3600), ["60 daqiqa"]),
        (errors.FloodWaitError(request=None, capture=86400), ["24 soat"]),
        (errors.PhoneNumberFloodError(request=None), ["juda koʻp kod soʻraldi"]),
        (errors.PhoneNumberInvalidError(request=None), ["qabul qilmadi", "+998"]),
        (errors.PhoneNumberBannedError(request=None), ["bloklangan", "zaxira"]),
        (errors.PhoneNumberUnoccupiedError(request=None), ["akkaunti yoʻq"]),
        (errors.PhoneNumberAppSignupForbiddenError(request=None), ["akkaunti yoʻq"]),
        (errors.ApiIdInvalidError(request=None), ["TELETHON_API_ID", "my.telegram.org"]),
        (errors.ApiIdPublishedFloodError(request=None), ["eʼlon qilingan"]),
        (errors.SendCodeUnavailableError(request=None), ["hamma yoʻllarini"]),
        (ConnectionError("Connection to Telegram failed 5 time(s)"), [CONNECTION_LOST]),
        (asyncio.IncompleteReadError(b"", 8), [CONNECTION_LOST]),
        (OSError("Network is unreachable"), [CONNECTION_LOST]),
        (
            RuntimeError("logged in right after sending the code"),
            [
                "Kutilmagan xato (RuntimeError)",
                "(logged in right after sending the code)",
            ],
        ),
        (
            errors.RPCError(None, "SOMETHING_NEW", 400),
            ["Telegram kutilmagan javob berdi (RPCError: SOMETHING_NEW)"],
        ),
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


async def test_a_local_error_is_not_blamed_on_telegram_and_masks_numbers(capsys):
    """Telethon's own AttributeError (auth.SentCodePaymentRequired has no
    .type) is not Telegram's answer; its text goes on a second line for the
    developer, a number in it masked."""
    error = AttributeError(f"'SentCodePaymentRequired' has no attribute 'type' {DIGITS}")
    ok, _ = await login(LoginClient(sends=[error]), answers=[PHONE])
    assert ok is False
    err = capsys.readouterr().err
    head, detail = err.strip().splitlines()
    assert head.startswith("Kutilmagan xato (AttributeError)")
    assert "Telegram" not in head
    assert "'SentCodePaymentRequired' has no attribute 'type' <raqam>" in detail
    assert DIGITS not in err


def test_an_error_text_is_clipped_to_one_short_line():
    detail = userbot_login._detail(ValueError("a\nb " + "x" * 500))
    assert "\n" not in detail and len(detail) == userbot_login.DETAIL_MAX


async def test_an_update_app_refusal_says_to_tell_the_developer(capsys):
    """UPDATE_APP_TO_LOGIN: waiting does not help, a newer client does."""
    client = LoginClient(sends=[errors.UpdateAppToLoginError(request=None)])
    ok, _ = await login(client, answers=[PHONE])
    assert ok is False
    captured = capsys.readouterr()
    assert UPDATE_APP in captured.err and "dasturchiga" in captured.err
    _no_traceback(captured)


async def test_an_account_without_telegram_is_never_signed_up(capsys):
    """sign_in raises PhoneNumberUnoccupied for a number with no account;
    the tool says to open it in the official app and stops."""
    client = LoginClient(codes=[errors.PhoneNumberUnoccupiedError(request=None)])
    ok, _ = await login(client)
    assert ok is False
    assert NO_ACCOUNT in capsys.readouterr().err


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
    client.get_me_error = lost  # disconnected: the late check cannot ask either
    ok, _ = await login(client)
    assert ok is False
    captured = capsys.readouterr()
    assert CONNECTION_LOST in captured.err
    _no_traceback(captured)


async def test_a_request_that_never_answers_is_bounded(capsys, monkeypatch):
    monkeypatch.setattr(userbot_login, "REQUEST_TIMEOUT_SECONDS", 0.01)
    ok, _ = await login(LoginClient(hang={"send_code_request"}), answers=[PHONE])
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


def test_main_holds_its_own_sigint_handler_while_it_runs(monkeypatch):
    """asyncio.run installs a handler that only cancels the main task when
    SIGINT has the default one, and a prompt blocked in input() then waits
    for a second Ctrl+C. main() keeps a handler of its own in place while it
    runs, and puts the previous one back."""
    seen: list[Any] = []

    async def peek() -> bool:
        seen.append(signal.getsignal(signal.SIGINT))
        return True

    monkeypatch.setattr(settings, "telethon_api_id", 1)
    monkeypatch.setattr(settings, "telethon_api_hash", "x")
    monkeypatch.setattr(userbot_login, "_login", peek)
    before = signal.getsignal(signal.SIGINT)
    assert userbot_login.main() == 0
    assert seen[0] is userbot_login._interrupt
    assert seen[0] is not signal.default_int_handler
    assert signal.getsignal(signal.SIGINT) is before
    with pytest.raises(KeyboardInterrupt):
        userbot_login._interrupt(signal.SIGINT, None)


CTRL_C_CHILD = """
import asyncio, sys
from types import SimpleNamespace
from telethon.tl.types import auth
from clientscrapper.config import settings
from clientscrapper.tools import userbot_login as u

class Fake:
    def __init__(self, *a):
        self.session = SimpleNamespace(save=lambda: "X")
    async def connect(self):
        pass
    async def send_code_request(self, phone):
        return auth.SentCode(type=auth.SentCodeTypeApp(length=5), phone_code_hash="h")
    async def sign_in(self, *a, **k):
        return None
    async def get_me(self):
        return SimpleNamespace(first_name="N", id=1)
    async def disconnect(self):
        await asyncio.sleep(0.05)
        print("[disconnected]", flush=True)

settings.telethon_api_id = 1
settings.telethon_api_hash = "x"
login = u._login
u._login = lambda: login(lambda *a: Fake())
sys.exit(u.main())
"""


def _read_until(fd: int, buf: bytearray, needle: bytes, seconds: float) -> None:
    end = time.monotonic() + seconds
    while needle not in buf and time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk


def test_one_ctrl_c_at_the_code_prompt_stops_the_tool():
    """A real Ctrl+C through a terminal (the review, 2026-11-12: under
    asyncio's own handler one Ctrl+C at the code prompt did nothing)."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    pid, fd = pty.fork()
    if pid == 0:  # the child: the tool on a terminal of its own
        try:
            os.chdir(ROOT)
            os.execve(sys.executable, [sys.executable, "-c", CTRL_C_CHILD], env)
        finally:
            os._exit(127)
    buf = bytearray()
    status: int | None = None
    try:
        _read_until(fd, buf, b"+998 bilan", 60)
        os.write(fd, PHONE.encode() + b"\r")
        _read_until(fd, buf, b"yuborgan kod", 30)
        assert b"yuborgan kod" in buf, buf.decode(errors="replace")
        time.sleep(0.3)
        os.write(fd, b"\x03")  # one Ctrl+C
        end = time.monotonic() + 15
        while time.monotonic() < end:
            _read_until(fd, buf, b"\x00never", 0.2)
            done, raw = os.waitpid(pid, os.WNOHANG)
            if done:
                status = raw
                break
        text = buf.decode(errors="replace")
        assert status is not None, f"still running after one Ctrl+C:\n{text}"
        assert os.waitstatus_to_exitcode(status) == 1, text
        assert "[disconnected]" in text and STOPPED in text, text
        assert "Traceback" not in text, text
    finally:
        if status is None:  # never reaped: still running, or never got this far
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)


# --- no route to Telegram ------------------------------------------------------------


class SilentClient(LoginClient):
    async def connect(self) -> None:
        await asyncio.sleep(3600)


def test_no_route_to_telegram_ends_in_words_not_silence(capsys, monkeypatch):
    """Without a route to Telegram the login waited in silence (the first
    docker dry run): the connect is bounded and says what to check. The test's
    own deadline makes a missing bound a failure, not a hang."""
    monkeypatch.setattr(userbot_login, "CONNECT_TIMEOUT_SECONDS", 0.01)
    client = SilentClient()

    async def run() -> bool:
        return await asyncio.wait_for(userbot_login._login(lambda *a: client), 5)

    assert asyncio.run(run()) is False
    assert not client.named("send_code_request") and "disconnect" in client.calls
    assert "Telegramga" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error",
    [
        # What an HTTP proxy or a block page on the route makes of the
        # auth-key read (Telethon catches only IOError and timeouts there).
        asyncio.IncompleteReadError(b"HTTP/1.1 403", 8),
        ConnectionRefusedError(111, "Connect call failed"),
    ],
    ids=type,
)
async def test_a_connect_answered_by_something_else_is_no_connection(error, capsys):
    client = LoginClient()
    client.connect_error = error
    ok, prompts = await login(client, answers=[])
    assert ok is False and prompts == []
    captured = capsys.readouterr()
    assert NO_CONNECTION.format(seconds=userbot_login.CONNECT_TIMEOUT_SECONDS) in (
        captured.err
    )
    _no_traceback(captured)


async def test_anything_else_from_connect_is_words_not_a_traceback(capsys):
    client = LoginClient()
    client.connect_error = RuntimeError("auth key generation failed")
    ok, _ = await login(client, answers=[])
    assert ok is False
    captured = capsys.readouterr()
    assert "Kutilmagan xato (RuntimeError)" in captured.err
    _no_traceback(captured)
