# syntax=docker/dockerfile:1
# VENDORED_FROM dailyJournal@6b1f67f Dockerfile
#
# Base image: the tag is pinned here and its digest recorded below; refresh
# the digest monthly (06 §3.2). python:3.12-slim on 2026-10-24 (index):
#   sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
FROM python:3.12-slim

# PIP_DEFAULT_TIMEOUT: pip's default 15-second socket timeout gives up on a
# slow link in the middle of a wheel. PIP_RETRIES then makes a dropped
# connection resume rather than fail the whole build.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tashkent \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

WORKDIR /app

# tzdata: Asia/Tashkent. age: encrypts the nightly dump before it touches the
# disk (06 §4.4). postgresql-client: pg_dump for that backup.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata ca-certificates curl age postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# pg_dump refuses to dump from a *newer* server, so the client major must be
# >= the postgres:16 container's. Debian's own client is whatever the base
# image's release shipped, so try PGDG for an exactly matching
# postgresql-client-16.
#
# Deliberately non-fatal: apt-get update dies on an unreachable or
# non-existent suite, and a build that fails here would take down the entire
# deployment to fix a backup. The Debian client installed above stays as the
# fallback, and a version mismatch surfaces loudly — pg_dump's own error is
# forwarded to the owner by the backup job.
RUN set -eu; \
    key=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    . /etc/os-release; \
    if install -d /usr/share/postgresql-common/pgdg \
        && curl -fsSL --retry 3 https://www.postgresql.org/media/keys/ACCC4CF8.asc -o "$key" \
        && echo "deb [signed-by=$key] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
            > /etc/apt/sources.list.d/pgdg.list \
        && apt-get update \
        && apt-get install -y --no-install-recommends postgresql-client-16; then \
        echo "pg_dump: using postgresql-client-16 from PGDG"; \
    else \
        echo "pg_dump: PGDG unavailable, keeping the distribution client" >&2; \
        rm -f /etc/apt/sources.list.d/pgdg.list; \
    fi; \
    rm -rf /var/lib/apt/lists/*; \
    pg_dump --version

COPY requirements.txt ./

# The cache mount is what makes a slow connection survivable: a build that
# dies half way keeps every wheel it already fetched, so the retry resumes
# instead of starting over. It is also why PIP_NO_CACHE_DIR is not set.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY clientscrapper ./clientscrapper

# Run as a non-root user; the app never needs to write to its own code.
RUN useradd --create-home --uid 10001 clientscrapper \
    && mkdir -p /data/evidence /data/backups /data/imports \
    && chown -R clientscrapper:clientscrapper /data
USER clientscrapper

CMD ["python", "-m", "clientscrapper.worker.main"]
