"""docker-compose.yml as 06 §3.1 states it (and 06 §5.6: no open port).

String and YAML checks: no Docker daemon is needed to see that a port opened
to the network, an app service lost its memory cap, its .env, its log
rotation or its wait on ``init``, or that a service appeared that §3.1 does
not name. «Healthy» for the app services is «running, and a fresh heartbeat»
(06 §4.1): since T-08 each app container's healthcheck is
``python -m clientscrapper.tools.healthcheck <service>``, which asks the
health job's own rule, so ``make ps`` shows them healthy (the go-live
checklist, 06 §7) exactly when the owner's alert would call them alive;
``db``'s ``pg_isready`` is the one ``init`` and every app service wait on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP_SERVICES = ("worker", "userbot", "bot")
SERVICES = {"db", "init", *APP_SERVICES, "searxng"}


def _services() -> dict[str, dict[str, Any]]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services: dict[str, dict[str, Any]] = compose["services"]
    return services


def test_the_service_set_is_06_s():
    assert set(_services()) == SERVICES


def test_compose_publishes_nothing_but_localhost():
    services = _services()
    published = {
        name: [str(port) for port in service.get("ports", [])]
        for name, service in services.items()
    }
    open_ports = {
        name: ports
        for name, ports in published.items()
        if any(not port.startswith("127.0.0.1:") for port in ports)
    }
    assert open_ports == {}
    assert published["db"] == ["127.0.0.1:${DB_PORT:-5433}:5432"]
    assert [name for name, ports in published.items() if ports] == ["db"]


def test_every_app_service_is_capped_configured_restarted_and_rotated():
    services = _services()
    for name in APP_SERVICES:
        service = services[name]
        assert service.get("mem_limit"), name
        assert service.get("cpus"), name
        assert service["env_file"] == ".env", name
        assert service["restart"] == "unless-stopped", name
        assert service["depends_on"] == {
            "db": {"condition": "service_healthy"},
            "init": {"condition": "service_completed_successfully"},
        }, name
        assert service["command"] == ["python", "-m", f"clientscrapper.{name}.main"]
        assert service["volumes"] == ["./data:/data", "./secrets:/app/secrets:ro"]


def test_every_service_rotates_its_logs():
    rotated = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
    assert {name: s.get("logging") for name, s in _services().items()} == dict.fromkeys(
        SERVICES, rotated
    )


def test_the_database_is_capped_waited_on_and_kept():
    db = _services()["db"]
    assert db["image"] == "postgres:16"
    assert db["restart"] == "unless-stopped"
    assert db["mem_limit"] and db["oom_score_adj"] < 0
    assert "pg_isready" in " ".join(db["healthcheck"]["test"])
    assert db["volumes"] == ["db_data:/var/lib/postgresql/data"]


def test_init_migrates_once_as_the_app_user():
    init = _services()["init"]
    assert init["restart"] == "no"
    assert init["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert "alembic upgrade head" in init["command"][-1]
    assert "su -s /bin/sh clientscrapper" in init["command"][-1]


def test_searxng_is_optional_and_unpublished():
    searxng = _services()["searxng"]
    assert searxng["profiles"] == ["searxng"]
    assert "ports" not in searxng and searxng.get("mem_limit")


def test_every_app_container_is_healthy_by_its_heartbeat():
    services = _services()
    beat = {"interval": "60s", "timeout": "20s", "retries": 3, "start_period": "120s"}
    for name in APP_SERVICES:
        check = services[name]["healthcheck"]
        assert check["test"] == [
            "CMD",
            "python",
            "-m",
            "clientscrapper.tools.healthcheck",
            name,
        ], name
        assert {k: v for k, v in check.items() if k != "test"} == beat, name
    # No other service but the database carries one: init runs once and the
    # optional searxng is nobody's dependency.
    assert sorted(n for n, s in services.items() if "healthcheck" in s) == [
        "bot",
        "db",
        "userbot",
        "worker",
    ]


def test_the_healthcheck_names_a_component_the_tool_knows():
    from clientscrapper.tools import healthcheck

    assert set(APP_SERVICES) == set(healthcheck.COMPONENTS)
