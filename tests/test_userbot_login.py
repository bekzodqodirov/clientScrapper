"""``make userbot-login`` (06 §2; 07 T-13): the session string goes to the
operator's terminal and nowhere else — no file, no log record."""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace

from clientscrapper.config import settings
from clientscrapper.tools import userbot_login

FAKE_SESSION = "1" + "N" * 40  # visibly fake, far shorter than a real one


class LoginClient:
    def __init__(self, session, api_id, api_hash) -> None:
        self.session = SimpleNamespace(save=lambda: FAKE_SESSION)
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def get_me(self):
        return SimpleNamespace(first_name="Namuna", id=5100000001)

    async def disconnect(self) -> None:
        self.calls.append("disconnect")


async def test_the_session_is_printed_once_and_written_nowhere(
    capsys, caplog, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.DEBUG)
    await userbot_login._login(LoginClient)
    out = capsys.readouterr().out
    assert out.count(f"TELETHON_SESSION={FAKE_SESSION}") == 1
    assert "Logged in as: Namuna" in out
    assert os.listdir(tmp_path) == []
    assert all(FAKE_SESSION not in record.getMessage() for record in caplog.records)


def test_the_line_is_the_env_key():
    assert userbot_login.session_line("abc") == "TELETHON_SESSION=abc"


def test_without_the_app_credentials_it_stops_before_asking_anything(capsys, monkeypatch):
    monkeypatch.setattr(settings, "telethon_api_id", None)
    monkeypatch.setattr(settings, "telethon_api_hash", "")
    assert userbot_login.main() == 1
    assert "TELETHON_API_ID and TELETHON_API_HASH" in capsys.readouterr().err
