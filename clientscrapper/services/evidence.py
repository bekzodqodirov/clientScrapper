"""The evidence store (02 §2.3, §3): immutable fetched artefacts.

One row per (url, bytes): a re-fetch with identical bytes is a no-op
(``ux_evidence_url_sha``), changed bytes are a new row. The raw bytes go to
``EVIDENCE_DIR/<yy>/<sha256>.gz`` (``yy`` = the Tashkent year of
``fetched_at``, the sha of the bytes *before* gzip, level 6), written to a
``.tmp`` and ``os.replace``d so a crash never leaves half a file. One file per
distinct body: two URLs with the same bytes are two rows and one file. A body
over 2 MB is not stored (``raw_path`` NULL, ``meta.truncated``); its row
still carries ``url``, ``fetched_at``, ``bytes`` and ``excerpt``, which is all
a card or an ERP note quotes. The file goes at ``expires_at`` (180 d; 30 d for
``maps_google``, the Maps terms), the row stays.

A file is written only for a row this transaction inserted, and only once the
transaction commits (``after_commit``); a rollback — of the whole transaction
or of the savepoint the row was stored in — drops the write. So no file ever
exists that no row points to, which retention (R1 selects rows) could never
find. The opposite failure, a crash between the commit and the write, leaves a
row whose file is missing: harmless (``open_raw`` answers None, the excerpt
still quotes), and healed by the next store of the same bytes.

Only the runner writes here (03 §1.1: "the runner, not the collector").
"""

from __future__ import annotations

import gzip
import hashlib
import html
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from clientscrapper.config import settings
from clientscrapper.db.models import Evidence
from clientscrapper.services.text import storable

RAW_CAP: Final = 2 * 1024 * 1024
URL_MAX: Final = 2048
EXCERPT_MAX: Final = 600
TITLE_MAX: Final = 300
GZIP_LEVEL: Final = 6
DEFAULT_RETENTION: Final = timedelta(days=180)
# Google Maps Platform terms: no caching beyond 30 days (02 §2.5, R4b).
RETENTION: Final = {"maps_google": timedelta(days=30)}
TASHKENT: Final = ZoneInfo("Asia/Tashkent")
_PENDING: Final = "evidence_pending_raw"

log = logging.getLogger(__name__)

_SCRIPT = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.I | re.S)


def evidence_root() -> Path:
    return Path(settings.evidence_dir)


def raw_path_for(sha256: str, fetched_at: datetime) -> str:
    """``<yy>/<sha256>.gz`` — relative to EVIDENCE_DIR, as ``raw_path`` stores it."""
    return f"{fetched_at.astimezone(TASHKENT):%y}/{sha256}.gz"


def expires_for(source_key: str, fetched_at: datetime) -> datetime:
    return fetched_at + RETENTION.get(source_key, DEFAULT_RETENTION)


def clip(text: str, limit: int) -> str:
    """At most ``limit`` characters, cut at a space when one is near the end;
    what a ``text`` column accepts (``text.storable``: no NUL)."""
    text = storable(text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space >= limit * 0.8 else cut).rstrip()


def text_of(body: bytes, content_type: str) -> str:
    """Readable text of a body: HTML without scripts, styles and tags, entities
    decoded (``&#0;`` too: ``storable`` takes the NUL out), whitespace
    collapsed; anything else decoded as UTF-8."""
    text = body.decode("utf-8", errors="replace")
    if "html" in content_type or text.lstrip()[:1] == "<":
        text = html.unescape(_TAG.sub(" ", _SCRIPT.sub(" ", text)))
    return _SPACE.sub(" ", storable(text)).strip()


def html_title(body: bytes, content_type: str) -> str | None:
    if "html" not in content_type:
        return None
    match = _TITLE.search(body.decode("utf-8", errors="replace"))
    if match is None:
        return None
    title = _SPACE.sub(" ", html.unescape(_TAG.sub(" ", match.group(1)))).strip()
    return clip(title, TITLE_MAX) or None


def lang_code(lang: str | None) -> str | None:
    """``uz-Latn`` → ``uz``: the column is char(2)."""
    return lang[:2].lower() if lang else None


def write_raw(path: Path, body: bytes) -> None:
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Two processes may store the same bytes at once: each its own tmp name.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(gzip.compress(body, compresslevel=GZIP_LEVEL, mtime=0))
    os.replace(tmp, path)


# --- files follow the transaction -------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PendingRaw:
    path: Path
    body: bytes
    transaction: SessionTransaction  # the innermost one the row was stored in


def _pending(session: Session) -> list[_PendingRaw]:
    pending: list[_PendingRaw] = session.info.setdefault(_PENDING, [])
    return pending


def _queue_raw(session: AsyncSession, path: Path, body: bytes) -> None:
    sync = session.sync_session
    transaction = sync.get_nested_transaction() or sync.get_transaction()
    assert transaction is not None  # called right after a statement ran
    _pending(sync).append(_PendingRaw(path, body, transaction))


def _within(transaction: SessionTransaction | None, outer: SessionTransaction) -> bool:
    while transaction is not None:
        if transaction is outer:
            return True
        transaction = transaction.parent
    return False


@event.listens_for(Session, "after_commit")
def _write_after_commit(session: Session) -> None:
    # Also fired when a savepoint is released; the files wait for the root.
    if session.in_nested_transaction() or not session.info.get(_PENDING):
        return
    for item in session.info.pop(_PENDING):
        try:
            write_raw(item.path, item.body)
        except OSError:
            log.warning("evidence file not written", extra={"raw": item.path.name})


def _boundary(transaction: SessionTransaction) -> SessionTransaction:
    """What a rollback of ``transaction`` undoes in the database: everything
    since the nearest savepoint or the root. A failed flush rolls back its own
    internal subtransaction, and the savepoint around it is then only closed,
    so the subtransaction is all this event ever names."""
    current: SessionTransaction | None = transaction
    while current is not None and not (current.nested or current.parent is None):
        current = current.parent
    return current or transaction


@event.listens_for(Session, "after_soft_rollback")
def _drop_after_rollback(session: Session, previous: SessionTransaction) -> None:
    pending = session.info.get(_PENDING)
    if pending:
        undone = _boundary(previous)
        session.info[_PENDING] = [
            item for item in pending if not _within(item.transaction, undone)
        ]


def pending_raw(session: AsyncSession) -> list[Path]:
    """The files the next commit of ``session`` writes (for tests and doctor)."""
    return [item.path for item in session.sync_session.info.get(_PENDING, [])]


async def find(session: AsyncSession, url: str, sha256: str) -> Evidence | None:
    stmt = sa.select(Evidence).where(
        sa.func.md5(Evidence.url) == sa.func.md5(url),
        Evidence.url == url,
        Evidence.sha256 == sha256,
    )
    return (await session.scalars(stmt)).first()


async def latest_stored(session: AsyncSession, url: str) -> Evidence | None:
    """The newest row for ``url`` whose file is still on disk — the copy a
    conditional GET validates against."""
    stmt = (
        sa.select(Evidence)
        .where(
            sa.func.md5(Evidence.url) == sa.func.md5(url),
            Evidence.url == url,
            Evidence.raw_path.is_not(None),
        )
        .order_by(Evidence.fetched_at.desc(), Evidence.id.desc())
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


async def store(
    session: AsyncSession,
    *,
    source_key: str,
    url: str,
    body: bytes,
    content_type: str,
    fetched_at: datetime,
    source_ref: str | None = None,
    excerpt: str | None = None,
    title: str | None = None,
    lang: str | None = None,
    published_at: datetime | None = None,
    collector_run_id: int | None = None,
    meta: dict[str, Any] | None = None,
    root: Path | None = None,
) -> Evidence:
    """The evidence row for these bytes at this URL — the existing one when the
    same bytes were stored before (nothing is rewritten: evidence is
    immutable), else a new row and, under 2 MB, its file, written when the
    transaction commits (see the module docstring)."""
    if len(url) > URL_MAX:
        raise ValueError("evidence url longer than 2048 characters")
    sha = hashlib.sha256(body).hexdigest()
    facts = dict(meta or {})
    stored_raw = len(body) <= RAW_CAP
    if not stored_raw or facts.get("truncated"):
        facts["truncated"] = True
    raw_path = raw_path_for(sha, fetched_at) if stored_raw else None
    kind = (content_type or "text/html").split(";")[0].strip().lower() or "text/html"
    quote = clip(excerpt if excerpt else text_of(body, kind), EXCERPT_MAX)
    heading = clip(title, TITLE_MAX) if title else html_title(body, kind)
    stmt = (
        pg_insert(Evidence)
        .values(
            source_key=source_key,
            collector_run_id=collector_run_id,
            url=url,
            source_ref=storable(source_ref) if source_ref is not None else None,
            fetched_at=fetched_at,
            published_at=published_at,
            sha256=sha,
            content_type=kind,
            bytes=len(body),
            raw_path=raw_path,
            excerpt=quote,
            title=heading or None,
            lang=lang_code(lang),
            expires_at=expires_for(source_key, fetched_at),
            meta=facts,
        )
        .on_conflict_do_nothing(
            index_elements=[sa.func.md5(Evidence.url), Evidence.sha256]
        )
        .returning(Evidence)
    )
    row = (await session.scalars(stmt)).first()
    if row is None:
        row = await find(session, url, sha)
        assert row is not None  # the conflict was this very (url, sha)
        keep_raw(session, row, body, root=root)
    elif raw_path is not None:
        _queue_raw(session, (root or evidence_root()) / raw_path, body)
    return row


def keep_raw(
    session: AsyncSession, evidence: Evidence, body: bytes, *, root: Path | None = None
) -> bool:
    """Heal a row whose file is missing (a crash between its commit and its
    write) when ``body`` is its bytes; the write waits for the commit. A
    purged row (``raw_path`` NULL) stays purged. True when a write was queued."""
    if evidence.raw_path is None or hashlib.sha256(body).hexdigest() != evidence.sha256:
        return False
    path = (root or evidence_root()) / evidence.raw_path
    if path.is_file():
        return False
    _queue_raw(session, path, body)
    return True


def open_raw(evidence: Evidence, *, root: Path | None = None) -> bytes | None:
    """The stored bytes, or None when the file was never kept or is gone."""
    if evidence.raw_path is None:
        return None
    path = (root or evidence_root()) / evidence.raw_path
    try:
        return gzip.decompress(path.read_bytes())
    except (OSError, EOFError, gzip.BadGzipFile):
        return None


async def purge_raw(
    session: AsyncSession, evidence: Evidence, *, now: datetime, root: Path | None = None
) -> bool:
    """Retention R1 for one row: the row loses its file reference; the file is
    unlinked only when no other row still points at the same bytes. True when
    a file was removed."""
    if evidence.raw_path is None:
        return False
    shared = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Evidence)
        .where(
            Evidence.raw_path == evidence.raw_path,
            Evidence.id != evidence.id,
        )
    )
    removed = False
    if not shared:
        path = (root or evidence_root()) / evidence.raw_path
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            removed = False
    evidence.raw_path = None
    evidence.raw_deleted_at = now
    await session.flush()
    return removed
