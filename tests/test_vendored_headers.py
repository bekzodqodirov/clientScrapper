"""Every file copied from MIYA says where it came from (07 §1.2, memo §4).

The skeleton is copied, not imported. A ``# VENDORED_FROM dailyJournal@<sha>
<path>`` header on line 1–3 is what lets a later sync diff each file against a
MIYA checkout. ``VENDORED`` mirrors the AS-IS/ADAPT rows of
``docs/kod-audit-miya.md`` §1 under the names the spec gives them; a file is
checked once it exists, and ``REQUIRED_NOW`` lists the ones the built tasks
must already have, so the guard can never pass by finding nothing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.git_env import isolated_env, tree_files

ROOT = Path(__file__).resolve().parents[1]
MIYA_SHA = "6b1f67f"
HEADER = re.compile(
    r"^# VENDORED_FROM dailyJournal@(?P<sha>[0-9a-f]{7,40}) (?P<path>\S+)$"
)

# target in this repo -> source in MIYA (dailyJournal@6b1f67f).
VENDORED: dict[str, str] = {
    # Settings, logging, db plumbing, migrations
    "clientscrapper/config.py": "miya/config.py",
    "clientscrapper/logs.py": "miya/logs.py",
    "clientscrapper/db/__init__.py": "miya/db/__init__.py",
    "clientscrapper/db/base.py": "miya/db/base.py",
    "clientscrapper/db/session.py": "miya/db/session.py",
    "alembic/env.py": "alembic/env.py",
    "alembic.ini": "alembic.ini",
    # Docker / Make
    "Dockerfile": "Dockerfile",
    "docker-compose.yml": "docker-compose.yml",
    "Makefile": "Makefile",
    # Processes
    "clientscrapper/worker/main.py": "miya/worker/main.py",
    "clientscrapper/bot/main.py": "miya/bot/main.py",
    "clientscrapper/userbot/main.py": "miya/userbot/main.py",
    "clientscrapper/services/lifecycle.py": "miya/userbot/main.py",
    # Services (later tasks)
    "clientscrapper/services/retry.py": "miya/services/retry.py",
    "clientscrapper/services/extraction.py": "miya/services/extraction.py",
    "clientscrapper/services/model_jobs.py": "miya/services/batch.py",
    "clientscrapper/services/usage.py": "miya/services/usage.py",
    "clientscrapper/services/costs.py": "miya/jarvis/costs.py",
    "clientscrapper/services/companies.py": "miya/services/people.py",
    "clientscrapper/services/text.py": "miya/services/text.py",
    # FIRST_NAMES is new; HONORIFICS is people.py's _HONORIFICS (04 §1.6).
    "clientscrapper/services/names.py": "miya/services/people.py",
    "clientscrapper/services/client_import.py": "miya/services/client_import.py",
    "clientscrapper/services/documents.py": "miya/services/documents.py",
    "clientscrapper/services/chats.py": "miya/services/chats.py",
    "clientscrapper/services/reviews.py": "miya/services/approvals.py",
    "clientscrapper/services/health.py": "miya/services/health.py",
    "clientscrapper/services/backup.py": "miya/services/backup.py",
    "clientscrapper/services/restore_drill.py": "miya/services/restore_drill.py",
    "clientscrapper/services/sheets.py": "miya/services/sheets.py",
    "clientscrapper/services/sheets_sync.py": "miya/services/sheets_sync.py",
    "clientscrapper/services/safety.py": "miya/services/safety.py",
    "clientscrapper/services/purge.py": "miya/services/purge.py",
    # Owner bot (handlers.py is split per 06 §1.1, replies.py becomes texts.py)
    "clientscrapper/bot/formatting.py": "miya/bot/formatting.py",
    "clientscrapper/bot/keyboards.py": "miya/bot/keyboards.py",
    "clientscrapper/bot/notices.py": "miya/bot/notices.py",
    "clientscrapper/bot/texts.py": "miya/bot/replies.py",
    "clientscrapper/bot/handlers/review.py": "miya/bot/handlers.py",
    "clientscrapper/bot/handlers/sources.py": "miya/bot/handlers.py",
    "clientscrapper/bot/handlers/chats.py": "miya/bot/handlers.py",
    "clientscrapper/bot/handlers/status.py": "miya/bot/handlers.py",
    "clientscrapper/bot/handlers/admin.py": "miya/bot/handlers.py",
    "clientscrapper/bot/handlers/manager.py": "miya/bot/handlers.py",
    # Tools
    "clientscrapper/tools/doctor.py": "miya/tools/doctor.py",
    "clientscrapper/tools/userbot_login.py": "miya/tools/userbot_login.py",
    "clientscrapper/tools/import_clients.py": "miya/tools/import_clients.py",
    "clientscrapper/tools/restore.py": "miya/tools/restore.py",
    # Tests copied with the code so behaviour stays pinned
    "tests/conftest.py": "tests/conftest.py",
    "tests/test_env_example.py": "tests/test_env_example.py",
    "tests/test_logs.py": "tests/test_logs.py",
    "tests/test_no_telegram_writes.py": "tests/test_userbot.py",
    "tests/test_retry.py": "tests/test_retry.py",
    "tests/test_health_beat.py": "tests/test_health.py",
    "tests/test_forbidden_strings.py": "tests/test_owner_aliases.py",
    "tests/timetravel.py": "tests/timetravel.py",
    # MIYA's compose-env CI step, moved into a script (07 §2.1 docker job)
    "scripts/check_compose_env.py": ".github/workflows/python-ci.yml",
}
# Not listed although the memo says AS-IS: services/codes.py (02 §7 — suppression
# by GS code is not in the v1 key set, so it is not ported).

# Built by the tasks done so far: these must exist now.
REQUIRED_NOW = (
    "clientscrapper/config.py",
    "clientscrapper/logs.py",
    "clientscrapper/db/__init__.py",
    "clientscrapper/db/base.py",
    "clientscrapper/db/session.py",
    "alembic/env.py",
    "alembic.ini",
    "Dockerfile",
    "docker-compose.yml",
    "Makefile",
    "clientscrapper/worker/main.py",
    "clientscrapper/bot/main.py",
    "clientscrapper/userbot/main.py",
    "clientscrapper/services/lifecycle.py",
    "clientscrapper/tools/doctor.py",
    "clientscrapper/services/text.py",
    "clientscrapper/services/names.py",
    "clientscrapper/services/retry.py",
    "clientscrapper/services/health.py",
    "tests/conftest.py",
    "tests/test_env_example.py",
    "tests/test_logs.py",
    "tests/test_retry.py",
    "tests/test_health_beat.py",
    # T-06
    "clientscrapper/services/usage.py",
    "clientscrapper/services/costs.py",
    # T-04
    "tests/test_no_telegram_writes.py",
    "tests/test_forbidden_strings.py",
    "tests/timetravel.py",
    "scripts/check_compose_env.py",
    # T-08
    "clientscrapper/services/backup.py",
    "clientscrapper/services/restore_drill.py",
    "clientscrapper/tools/restore.py",
    # T-17
    "clientscrapper/services/companies.py",
    # T-13
    "clientscrapper/services/chats.py",
    "clientscrapper/tools/userbot_login.py",
    # T-11
    "clientscrapper/services/client_import.py",
    "clientscrapper/tools/import_clients.py",
    # T-15
    "clientscrapper/services/extraction.py",
)

SCANNED_SUFFIXES = {".py", ".yml", ".yaml", ".ini", ".toml", ".cfg", ".sh", ".mako"}
SCANNED_NAMES = {"Dockerfile", "Makefile"}


def _header(path: Path) -> re.Match[str] | None:
    head = path.read_text(encoding="utf-8").splitlines()[:3]
    for line in head:
        if match := HEADER.match(line):
            return match
    return None


def _scanned_files(root: Path = ROOT) -> list[Path]:
    # git's own list (tracked + untracked not ignored), never a bare rglob: run
    # from the main checkout, that would read every task worktree under .wt/.
    return [
        path
        for path in tree_files(root)
        if path.suffix in SCANNED_SUFFIXES or path.name in SCANNED_NAMES
    ]


def test_the_built_tasks_files_exist():
    assert [rel for rel in REQUIRED_NOW if not (ROOT / rel).is_file()] == []
    assert set(REQUIRED_NOW) <= set(VENDORED)


def test_every_vendored_file_names_its_miya_source_on_line_1_to_3():
    wrong = {}
    for rel, source in VENDORED.items():
        path = ROOT / rel
        if not path.is_file():
            continue
        match = _header(path)
        if match is None or match["sha"] != MIYA_SHA or match["path"] != source:
            wrong[rel] = match.group(0) if match else "no header"
    assert wrong == {}


def test_every_header_in_the_tree_is_a_listed_file():
    # The reverse direction: a header nobody listed is vendoring nobody tracks.
    unlisted = [
        str(path.relative_to(ROOT))
        for path in _scanned_files()
        if _header(path) is not None and str(path.relative_to(ROOT)) not in VENDORED
    ]
    assert unlisted == []


def test_the_scan_stays_inside_its_own_checkout(tmp_path):
    """A worktree under .wt/ (excluded in .git/info/exclude) or any ignored
    directory is another checkout's business: its headers are not ours."""

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            env=isolated_env(),
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    header = "# VENDORED_FROM dailyJournal@6b1f67f miya/config.py\n"
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "tracked.py").write_text(header, encoding="utf-8")
    (tmp_path / "pkg" / "new.py").write_text(header, encoding="utf-8")
    git("add", "pkg/tracked.py")
    for ignored in (".wt/t09/pkg/other.py", "data/stray.py"):
        (tmp_path / ignored).parent.mkdir(parents=True)
        (tmp_path / ignored).write_text(header, encoding="utf-8")
    (tmp_path / ".gitignore").write_text("data/\n", encoding="utf-8")
    (tmp_path / ".git" / "info").mkdir(exist_ok=True)
    (tmp_path / ".git" / "info" / "exclude").write_text(".wt/\n", encoding="utf-8")

    scanned = sorted(p.relative_to(tmp_path).as_posix() for p in _scanned_files(tmp_path))
    assert scanned == ["pkg/new.py", "pkg/tracked.py"]
