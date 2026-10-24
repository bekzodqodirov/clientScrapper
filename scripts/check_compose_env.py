# VENDORED_FROM dailyJournal@6b1f67f .github/workflows/python-ci.yml
"""Compose must read ``.env.example`` the way a fresh server's ``.env`` reads.

    cp .env.example .env
    docker compose config --format json > compose.json
    python3 scripts/check_compose_env.py compose.json

MIYA's ``compose-env`` job, moved into a file (07 §2.1 docker job). A
trailing ``# comment`` on a ``KEY=`` line once became the value itself and
crashed every container on the first ``make up``; and a credential that
reached the example would ship to every clone. Checked on Compose's own
reading of the file (variables expanded, ``env_file`` merged):

* no environment value of any service looks like a comment;
* every credential the example carries is blank, except POSTGRES_PASSWORD,
  which must be the ``CHANGE_ME`` placeholder ``make doctor`` refuses
  (``${POSTGRES_PASSWORD:?required}`` treats blank as unset, 06 §3.4);
* the services are db, init, worker, userbot, bot (searxng only under its
  profile), the app services carry ``TZ=Asia/Tashkent``, and nothing is
  published beyond Postgres on 127.0.0.1 (06 §3.1).

No third-party dependency: the docker job runs it with the runner's python3.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SERVICES = {"db", "init", "worker", "userbot", "bot"}
APP_SERVICES = ("worker", "userbot", "bot")
TIMEZONE = "Asia/Tashkent"
PASSWORD_PLACEHOLDER = "CHANGE_ME"
# Names that hold a credential; their example value must be blank.
CREDENTIAL = re.compile(r"(?:_KEY|_TOKEN|_SESSION|_HASH|_SECRET|_PASSWORD)$")
CREDENTIAL_EXCEPTIONS = {"POSTGRES_PASSWORD"}


def problems(config: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    services: Mapping[str, Any] = config.get("services") or {}
    if set(services) != SERVICES:
        found.append(f"services are {sorted(services)}, expected {sorted(SERVICES)}")
    for name, service in sorted(services.items()):
        env: Mapping[str, Any] = service.get("environment") or {}
        for key, value in sorted(env.items()):
            text = "" if value is None else str(value)
            if text.lstrip().startswith("#"):
                found.append(f"{name}.{key}: the value looks like a comment")
            if CREDENTIAL.search(key) and key not in CREDENTIAL_EXCEPTIONS and text:
                found.append(
                    f"{name}.{key}: a credential in .env.example (must be blank)"
                )
        password = env.get("POSTGRES_PASSWORD")
        if password is not None and password != PASSWORD_PLACEHOLDER:
            found.append(
                f"{name}.POSTGRES_PASSWORD: not the {PASSWORD_PLACEHOLDER} placeholder"
            )
        for port in service.get("ports") or []:
            host_ip = port.get("host_ip") if isinstance(port, Mapping) else None
            if host_ip != "127.0.0.1":
                found.append(f"{name}: publishes {port} beyond 127.0.0.1")
    for name in APP_SERVICES:
        env = (services.get(name) or {}).get("environment") or {}
        if env.get("TZ") != TIMEZONE:
            found.append(f"{name}.TZ: {env.get('TZ')!r}, expected {TIMEZONE}")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: check_compose_env.py compose.json", file=sys.stderr)
        return 2
    config = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    found = problems(config)
    for line in found:
        print(line)
    if found:
        return 1
    print("compose env ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
