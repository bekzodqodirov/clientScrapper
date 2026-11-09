"""``make userbot-login`` (06 §2; 07 T-13): the session string goes to the
operator's terminal and nowhere else — no file, no log record."""

from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest

from clientscrapper.config import settings
from clientscrapper.tools import userbot_login

FAKE_SESSION = "1" + "N" * 40  # visibly fake, far shorter than a real one


class LoginClient:
    def __init__(self, session, api_id, api_hash) -> None:
        self.session = SimpleNamespace(save=lambda: FAKE_SESSION)
        self.calls: list[str] = []

    async def connect(self) -> None:
        self.calls.append("connect")

    async def start(self, **prompts) -> None:
        self.calls.append("start")
        self.prompts = prompts

    async def get_me(self):
        return SimpleNamespace(first_name="Namuna", id=5100000001)

    async def disconnect(self) -> None:
        self.calls.append("disconnect")


async def test_the_session_is_printed_once_and_written_nowhere(
    capsys, caplog, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.DEBUG)
    assert await userbot_login._login(LoginClient)
    out = capsys.readouterr().out
    assert out.count(f"TELETHON_SESSION={FAKE_SESSION}") == 1
    assert "Kirildi: Namuna" in out
    assert os.listdir(tmp_path) == []
    assert all(FAKE_SESSION not in record.getMessage() for record in caplog.records)


def test_the_line_is_the_env_key():
    assert userbot_login.session_line("abc") == "TELETHON_SESSION=abc"


def test_without_the_app_credentials_it_stops_before_asking_anything(capsys, monkeypatch):
    monkeypatch.setattr(settings, "telethon_api_id", None)
    monkeypatch.setattr(settings, "telethon_api_hash", "")
    assert userbot_login.main() == 1
    err = capsys.readouterr().err
    assert "TELETHON_API_ID va TELETHON_API_HASH" in err and "my.telegram.org" in err


def test_the_prompts_are_the_tools_own():
    """Never Telethon's English defaults, whose phone prompt says «(or bot
    token)» right after the owner has made a bot (the first docker dry run)."""
    client = LoginClient(None, 1, "x")
    asyncio.run(userbot_login._login(lambda *a: client))
    assert client.calls[:2] == ["connect", "start"]
    assert client.prompts["phone"] is userbot_login.ask_phone
    assert {"code_callback", "password"} <= set(client.prompts)


@pytest.mark.parametrize(
    "phone", ["+998901234567", "998 90 123-45-67", "+998(90)1234567"]
)
def test_a_phone_number_is_taken(phone):
    assert userbot_login.ask_phone(lambda _prompt: phone) == phone


def test_a_bot_token_at_the_phone_prompt_is_refused_and_asked_again(capsys):
    """Telethon signs in as a bot when the phone prompt gets a token; the
    reader must be the SIM account, so the token is refused."""
    answers = iter(["123456789:AAAAfakefakefakefake", "abc", "+998901234567"])
    assert userbot_login.ask_phone(lambda _prompt: next(answers)) == "+998901234567"
    assert capsys.readouterr().out.count("Bu telefon raqami emas") == 2


class SilentClient(LoginClient):
    async def connect(self) -> None:
        await asyncio.sleep(3600)


def test_no_route_to_telegram_ends_in_words_not_silence(capsys, monkeypatch):
    """Without a route to Telegram the login waited in silence (the first
    docker dry run): the connect is bounded and says what to check."""
    monkeypatch.setattr(userbot_login, "CONNECT_TIMEOUT_SECONDS", 0.01)
    client = SilentClient(None, 1, "x")
    assert asyncio.run(userbot_login._login(lambda *a: client)) is False
    assert "start" not in client.calls and "disconnect" in client.calls
    assert "Telegramga" in capsys.readouterr().err
