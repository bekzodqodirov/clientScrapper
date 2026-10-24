"""scripts/check_compose_env.py: what the CI docker job refuses (07 §2.1)."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys

import pytest

from tests.ast_tools import ROOT

SCRIPT = ROOT / "scripts" / "check_compose_env.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_compose_env", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_compose_env"] = module
    spec.loader.exec_module(module)
    return module


checker = _load()

APP_ENV = {
    "TZ": "Asia/Tashkent",
    "POSTGRES_PASSWORD": "CHANGE_ME",
    "DATABASE_URL": "postgresql+psycopg://clientscrapper:CHANGE_ME@db:5432/clientscrapper",
    "ANTHROPIC_API_KEY": "",
    "ASSISTANT_BOT_TOKEN": "",
    "TELETHON_SESSION": "",
    "TELETHON_API_HASH": "",
    "CRM_AGENT_KEY": "",
    "CRM_BASE_URL": "https://erp.example.uz",
}
GOOD = {
    "services": {
        "db": {
            "environment": {"POSTGRES_PASSWORD": "CHANGE_ME", "TZ": "Asia/Tashkent"},
            "ports": [{"host_ip": "127.0.0.1", "target": 5432, "published": "5433"}],
        },
        "init": {"environment": dict(APP_ENV, TZ=None)},
        "worker": {"environment": dict(APP_ENV)},
        "userbot": {"environment": dict(APP_ENV)},
        "bot": {"environment": dict(APP_ENV)},
    }
}


def _with(path: list[str], value: object) -> dict:
    config = copy.deepcopy(GOOD)
    node = config
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return config


def test_the_good_shape_passes():
    assert checker.problems(GOOD) == []


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (
            ["services", "worker", "environment", "HH_USER_AGENT"],
            "# clientScrapper/1.0 (email)",
            "worker.HH_USER_AGENT: the value looks like a comment",
        ),
        (
            ["services", "bot", "environment", "ASSISTANT_BOT_TOKEN"],
            "not-blank",
            "bot.ASSISTANT_BOT_TOKEN: a credential in .env.example (must be blank)",
        ),
        (
            ["services", "db", "environment", "POSTGRES_PASSWORD"],
            "hunter2",
            "db.POSTGRES_PASSWORD: not the CHANGE_ME placeholder",
        ),
        (
            ["services", "userbot", "environment", "TZ"],
            "UTC",
            "userbot.TZ: 'UTC', expected Asia/Tashkent",
        ),
        (
            ["services", "db", "ports"],
            [{"host_ip": "0.0.0.0", "target": 5432, "published": "5433"}],
            "db: publishes {'host_ip': '0.0.0.0', 'target': 5432, 'published': '5433'} "
            "beyond 127.0.0.1",
        ),
        (
            ["services", "api"],
            {"environment": {}},
            "services are ['api', 'bot', 'db', 'init', 'userbot', 'worker'], "
            "expected ['bot', 'db', 'init', 'userbot', 'worker']",
        ),
    ],
)
def test_each_problem_is_named(path, value, expected):
    assert checker.problems(_with(path, value)) == [expected]


def test_the_cli_exit_codes(tmp_path, capsys):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(GOOD))
    bad.write_text(json.dumps(_with(["services", "bot", "environment", "TZ"], "UTC")))
    assert checker.main([str(good)]) == 0
    assert checker.main([str(bad)]) == 1
    assert checker.main([]) == 2
    assert "bot.TZ" in capsys.readouterr().out


def test_compose_reads_env_example_cleanly(tmp_path):
    """The docker job's own steps, when the Compose CLI is here (it needs no
    daemon to print the configuration)."""
    if shutil.which("docker") is None:
        pytest.skip("no docker CLI (CI's docker job runs these steps)")
    shutil.copy(ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env")
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 and "is not a docker command" in result.stderr:
        pytest.skip("docker CLI without the compose plugin")
    assert result.returncode == 0, result.stderr
    assert checker.problems(json.loads(result.stdout)) == []
