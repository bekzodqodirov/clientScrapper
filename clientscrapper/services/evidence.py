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
still quotes), and healed by the next store of the same bytes. Deleting goes
the other way round — rows first, their files after the commit — so a stop in
between leaves files no row names; R1's ``sweep_orphans`` finds those on disk.

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
import stat
from collections.abc import Collection, Iterable
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
# Raw files are the app user's alone (06 §5.6).
RAW_MODE: Final = 0o600
DEFAULT_RETENTION: Final = timedelta(days=180)
# Google Maps Platform terms: no caching beyond 30 days (02 §2.5, R4b).
RETENTION: Final = {"maps_google": timedelta(days=30)}
TASHKENT: Final = ZoneInfo("Asia/Tashkent")
_PENDING: Final = "evidence_pending_raw"
# A write lives for milliseconds between its ``.tmp`` and ``os.replace``: a
# ``.tmp`` this old is a crash mid-write, and retention sweeps it (02 §3, T-31).
TMP_SUFFIX: Final = ".tmp"
TMP_GRACE: Final = timedelta(hours=1)
# A stored file as ``raw_path`` names it (``raw_path_for``); the orphan sweep
# touches nothing of any other shape. Its rows are read this many at a time.
_RAW_PATH: Final = re.compile(r"\d{2}/[0-9a-f]{64}\.gz")
ORPHAN_PAGE: Final = 500

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
    # Owner-only from its first byte, whatever the umask: a raw page can hold
    # phones and names, and ./data is a bind mount every host user could
    # otherwise read (the backups are 0600 for the same reason).
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, RAW_MODE)
    try:
        with os.fdopen(fd, "wb") as sink:
            sink.write(gzip.compress(body, compresslevel=GZIP_LEVEL, mtime=0))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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


# --- retention and purge (02 §3, §4; 06 §5.4) ----------------------------------------


def _inside(base: Path, rel: str) -> Path | None:
    """``base / rel`` when it stays under ``base`` (``raw_path`` is written by
    this module alone; the guard keeps an unlink from ever leaving the root)."""
    path = base / rel
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return None
    return path


def raw_size(evidence: Evidence, *, root: Path | None = None) -> int:
    """The bytes the row's file takes on disk; 0 when it has none."""
    if evidence.raw_path is None:
        return 0
    path = _inside(root or evidence_root(), evidence.raw_path)
    try:
        return path.stat().st_size if path is not None else 0
    except OSError:
        return 0


def stored_bytes(*, root: Path | None = None) -> int:
    """What the evidence directory takes on disk: every regular file under it
    (files, a crash's ``.tmp`` included)."""
    base = root or evidence_root()
    total = 0
    for dirpath, _, names in os.walk(base):
        for name in names:
            try:
                found = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(found.st_mode):
                total += found.st_size
    return total


def sweep_tmp(
    *, now: datetime, root: Path | None = None, grace: timedelta = TMP_GRACE
) -> int:
    """Unlink every ``*.tmp`` under the evidence root older than ``grace``:
    what a crash between ``write_raw``'s write and its ``os.replace`` left
    (02 §3). A write in flight is younger and stays. Returns the count."""
    base = root or evidence_root()
    if not base.is_dir():
        return 0
    cutoff = now.timestamp() - grace.total_seconds()
    removed = 0
    for path in base.rglob(f"*{TMP_SUFFIX}"):
        try:
            found = path.lstat()
            if not stat.S_ISREG(found.st_mode) or found.st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            log.warning("evidence tmp not removed", extra={"raw": path.name})
    return removed


def _stored_files(base: Path, cutoff: float) -> list[str]:
    """Every ``<yy>/<sha256>.gz`` under ``base`` (``raw_path``'s own shape —
    nothing else the directory may hold) that is a regular file older than
    ``cutoff``, as ``raw_path`` would name it."""
    found: list[str] = []
    for path in base.glob("*/*.gz"):
        rel = f"{path.parent.name}/{path.name}"
        if not _RAW_PATH.fullmatch(rel):
            continue
        try:
            st = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
            found.append(rel)
    return sorted(found)


async def sweep_orphans(
    session: AsyncSession,
    *,
    now: datetime,
    root: Path | None = None,
    grace: timedelta = TMP_GRACE,
) -> int:
    """Unlink every stored file no evidence row names any more (retention R1,
    02 §3: «no file exists that no row points to»). A purge or R2a deletes
    its rows, commits, and only then unlinks their files; a process stopped
    in between, or an unlink the disk refused, leaves the file behind with no
    row to find it by — this is how it still goes (a legal request's page
    included). A file is written only after its row commits, so a file older
    than ``grace`` that no committed row names is an orphan; the rows are read
    in pages by ``sha256`` (indexed) right before each page is unlinked.
    Returns the count."""
    base = root or evidence_root()
    if not base.is_dir():
        return 0
    files = _stored_files(base, now.timestamp() - grace.total_seconds())
    removed = 0
    for start in range(0, len(files), ORPHAN_PAGE):
        page = files[start : start + ORPHAN_PAGE]
        shas = sorted({rel.split("/")[1].removesuffix(".gz") for rel in page})
        named = set(
            await session.scalars(
                sa.select(Evidence.raw_path).where(
                    Evidence.sha256.in_(shas), Evidence.raw_path.is_not(None)
                )
            )
        )
        removed += unlink_raw([rel for rel in page if rel not in named], root=base)
    return removed


async def drop_rows(session: AsyncSession, ids: Collection[int]) -> list[str]:
    """Delete these evidence rows (retention R2a, a purge) in the caller's
    transaction; their observations and signals go with them (CASCADE). The
    answer is the ``raw_path`` values they held that no remaining row holds:
    the files to unlink once the transaction commits (02 §3: «deletion only
    when no row with raw_path IS NOT NULL shares» the file) — a rollback then
    leaves every file a row still names."""
    if not ids:
        return []
    held = await session.scalars(
        sa.delete(Evidence)
        .where(Evidence.id.in_(sorted(ids)))
        .returning(Evidence.raw_path)
    )
    paths = sorted({p for p in held if p is not None})
    if not paths:
        return []
    still = set(
        await session.scalars(
            sa.select(Evidence.raw_path).where(Evidence.raw_path.in_(paths)).distinct()
        )
    )
    return [p for p in paths if p not in still]


def unlink_raw(paths: Iterable[str], *, root: Path | None = None) -> int:
    """Unlink files ``drop_rows`` named (after the commit); a file already
    gone is not an error. Returns how many were removed."""
    base = root or evidence_root()
    removed = 0
    for rel in paths:
        path = _inside(base, rel)
        if path is None:
            continue
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            log.warning("evidence file not removed", extra={"raw": path.name})
    return removed
