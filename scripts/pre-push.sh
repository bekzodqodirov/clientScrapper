#!/usr/bin/env bash
# Each gate prints its own marker and the chain stops at the first red one (fullprompt #738/#803:
# `cmd | tail` reports tail's exit code, so nothing here is piped).
set -euo pipefail
# A hook in a linked worktree (.wt/*) is handed GIT_DIR, and every gate would
# inherit it: the suite's scratch-repo git would then commit into the
# repository being pushed. Clear what git itself clears before entering
# another repository; the hook runs at the worktree's root, so git still
# finds this one from the directory.
unset $(git rev-parse --local-env-vars)
# Besides that line, three differ from 07 §2.2. Two passed a red gate: set -e ignores a
# failure anywhere in an && / || list but its last command, so [2/7]'s
# `ruff check . && ruff format --check .` went on after a failing ruff check
# (two commands now) and [6/7]'s `{ … && alembic check; } || echo` ran the echo
# instead (an if now). [7/7] could fail a green one: under pipefail an
# early-exiting grep -q can turn git's SIGPIPE into a false "missing trailer"
# (a here-string now, not a pipe).
echo "[1/7] secrets";     python scripts/secret_scan.py
echo "[2/7] ruff";        ruff check .; ruff format --check .
echo "[3/7] mypy";        mypy
echo "[4/7] guards";      pytest -q -p no:cacheprovider tests/test_no_telegram_writes.py tests/test_collector_imports.py \
                           tests/test_push_requires_approved.py tests/test_paid_calls_via_costs.py tests/test_env_example.py \
                           tests/test_uz_text.py tests/test_bot_callbacks.py tests/test_forbidden_strings.py
echo "[5/7] suite";       CS_REQUIRE_DB="${CS_REQUIRE_DB:-0}" pytest -q -p no:cacheprovider -x
echo "[6/7] migrations";  if [ "${CS_REQUIRE_DB:-0}" = "1" ]; then alembic check; else echo "  (no database: alembic check skipped — CI runs it)"; fi
echo "[7/7] trailers";    grep -q '^Co-Authored-By:' <<<"$(git log -1 --format=%B)" || { echo "missing Co-Authored-By trailer"; exit 1; }
echo "pre-push: green"
