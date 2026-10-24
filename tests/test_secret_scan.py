"""scripts/secret_scan.py (07 §2.2): every rule sees its shape, the tree is clean.

The planted values are built at run time, so this file never holds a string
the scanner would refuse.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ast_tools import ROOT
from tests.git_env import isolated_env

SCRIPT = ROOT / "scripts" / "secret_scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("secret_scan", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["secret_scan"] = module
    spec.loader.exec_module(module)
    return module


scanner = _load()

PLANTED = {
    "anthropic key": "sk-" + "ant-" + "api03-" + "A1b2C3d4" * 4,
    "telegram bot token": "1234567" + "89:" + "AAH" + "x" * 32,
    "telethon session": "1" + "BAAz" * 90,
    "google api key": "AI" + "za" + "S" * 35,
    "apify token": "apify" + "_api_" + "Q" * 30,
    "brave key": "BS" + "A" + "k" * 25,
    "age secret key": "AGE-SECRET" + "-KEY-1" + "Q" * 58,
}


# Split so this file holds no number the scanner would refuse.
REAL = "+998 91 "


def rules(path: str, text: str) -> list[str]:
    return [f.rule for f in scanner.scan_text(path, text)]


@pytest.mark.parametrize(("rule", "value"), PLANTED.items(), ids=list(PLANTED))
def test_each_credential_shape_is_refused_and_never_printed(rule, value):
    findings = scanner.scan_text("clientscrapper/x.py", f'KEY = "{value}"\n')
    assert [f.rule for f in findings] == [rule]
    assert [str(f) for f in findings] == [f"clientscrapper/x.py:1: {rule}"]
    assert value not in str(findings[0])


# Split so this file holds no token the scanner would refuse.
BOT_ID = "71234" + "56789"
BOT_SECRET = "AAH" + "x" * 31


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        (f"https://api.telegram.org/bot{BOT_ID}:{BOT_SECRET}Q/getMe", True),
        (f"TOKEN={BOT_ID}:{BOT_SECRET}-", True),  # a secret ending in "-"
        (f"'{BOT_ID}:{BOT_SECRET}_'", True),
        (f"TOKEN={BOT_ID}:{BOT_SECRET}Q", True),
        (f"id 1{BOT_ID}:{BOT_SECRET}Q", False),  # 11 digits are no bot id
        (f"TOKEN={BOT_ID}:{BOT_SECRET}QQ", False),  # 36 characters are no secret
    ],
    ids=["url", "dash", "underscore", "plain", "eleven-digits", "too-long"],
)
def test_a_bot_token_in_a_url_or_ending_in_a_dash(text, flagged):
    assert (rules("services/x.py", text) == ["telegram bot token"]) is flagged


def test_a_session_inside_a_longer_base64_blob_is_not_a_session():
    blob = "data:image/png;base64,iVBOR" + ("w0KGgo+" * 10) + "1" + "A" * 320
    assert rules("tests/fixtures/x/page.html", blob) == []
    assert rules("x.txt", "TELETHON_SESSION=" + PLANTED["telethon session"]) == [
        "telethon session"
    ]


@pytest.mark.parametrize(
    ("text", "path", "flagged"),
    [
        (REAL + "234 56 78", "docs/x.md", True),
        (REAL.replace(" ", "") + "2345678", "tests/test_x.py", True),
        (REAL + "234-56-78", "clientscrapper/x.py", True),  # dashes are spaces too
        (REAL + "234 56 78", "tests/fixtures/goldenpages/list.html", False),
        ("+998 90 000 00 42", "tests/test_x.py", False),  # the synthetic range
        ("+998 93 000 00 09", "tests/test_x.py", False),  # same shape, other code
        ("+998 71 200 00 17", "tests/test_x.py", False),  # 04 §1.1 landline shape
        ("+998 90 111 11 11", "tests/test_x.py", False),  # a placeholder
        ("+998 90 123 45 67", "docs/spec/04-processing.md", False),  # the spec's example
        ("+998 90 " + "765 43 21", "docs/x.md", True),
    ],
)
def test_phone_numbers_outside_the_synthetic_range(text, path, flagged):
    assert bool(rules(path, f"tel: {text}")) is flagged


def test_an_age_recipient_only_on_a_comment_of_env_example():
    recipient = "age" + "1" + "q" * 58
    assert rules(".env.example", f"# e.g. BACKUP_AGE_RECIPIENT={recipient}") == []
    assert rules(".env.example", f"BACKUP_AGE_RECIPIENT={recipient}") == [
        "age recipient outside .env.example"
    ]
    assert rules("docs/x.md", recipient) == ["age recipient outside .env.example"]


def test_the_cli_names_file_and_line_and_exits_1(tmp_path, capsys):
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    assert scanner.main(["--root", str(tmp_path)]) == 0
    (tmp_path / "leak.py").write_text(f'\n\nK = "{PLANTED["anthropic key"]}"\n')
    (tmp_path / "blob.bin").write_bytes(b"\0" + PLANTED["brave key"].encode())
    assert scanner.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "leak.py:3: anthropic key" in out
    assert PLANTED["anthropic key"] not in out
    assert "blob.bin" not in out  # binary files are skipped


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, env=isolated_env(), check=True, capture_output=True
    )


def test_the_cli_reads_git_tracked_and_new_files_not_ignored(tmp_path):
    def git(*args: str) -> None:
        _git(tmp_path, *args)

    git("init", "-q")
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".env").write_text(f"KEY={PLANTED['apify token']}\n")
    (tmp_path / "new.md").write_text(f"{PLANTED['google api key']}\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        env=isolated_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["new.md:1: google api key"]


def test_the_repository_is_clean():
    assert [str(f) for f in scanner.scan(ROOT)] == []


def test_paths_are_relative_to_the_root(tmp_path):
    target = tmp_path / "a" / "b.txt"
    target.parent.mkdir()
    target.write_text(PLANTED["brave key"])
    [finding] = scanner.scan(tmp_path, [str(target)])
    assert finding.path == "a/b.txt"
    assert isinstance(finding.line, int) and Path(finding.path).name == "b.txt"


def test_git_env_drops_every_variable_git_calls_repository_local():
    listed = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        env=isolated_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "GIT_DIR" in listed
    assert set(listed) <= scanner.REPO_LOCAL_ENV


def test_an_inherited_git_dir_does_not_change_what_is_scanned(tmp_path, monkeypatch):
    """The hook of a linked worktree exports GIT_DIR; --root DIR still lists
    DIR's own index. A file git tracks there despite .gitignore (added with
    -f) is listed only by DIR's index, so reading another one misses it."""
    victim = tmp_path / "victim"
    victim.mkdir()
    _git(victim, "init", "-q")
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("*.log\n")
    (root / "kept.log").write_text(f"{PLANTED['apify token']}\n")
    _git(root, "add", "-f", "kept.log")
    monkeypatch.setenv("GIT_DIR", str(victim / ".git"))
    assert [str(f) for f in scanner.scan(root)] == ["kept.log:1: apify token"]
