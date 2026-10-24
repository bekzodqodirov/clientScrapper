"""services/evidence (02 §2.3, §3): immutable rows, one gzip file per body.

Postgres-backed; files go to a tmp EVIDENCE_DIR and are written when the
transaction commits. Synthetic pages only.
"""

from __future__ import annotations

import gzip
import hashlib
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from clientscrapper.db.models import Evidence
from clientscrapper.services import evidence
from tests.fakes import T0, TASHKENT

URL = "https://www.namuna.example.uz/katalog/1"
BODY = (
    b"<html><head><title>Namuna  Savdo &amp; Co</title><script>var x=1;</script></head>"
    b"<body><h1>Namuna Savdo MChJ</h1><p>Optom mebel, Toshkent</p></body></html>"
)


async def test_store_writes_one_row_and_one_gzip_file(session, tmp_path):
    row = await evidence.store(
        session,
        source_key="directories_goldenpages",
        url=URL,
        body=BODY,
        content_type="text/html; charset=utf-8",
        fetched_at=T0,
        meta={"etag": '"v1"', "status": 200},
        root=tmp_path,
    )
    sha = hashlib.sha256(BODY).hexdigest()
    assert (row.sha256, row.bytes, row.content_type) == (sha, len(BODY), "text/html")
    assert row.raw_path == f"26/{sha}.gz"
    assert row.title == "Namuna Savdo & Co"
    assert row.excerpt == "Namuna Savdo & Co Namuna Savdo MChJ Optom mebel, Toshkent"
    assert row.expires_at == T0 + timedelta(days=180)
    assert row.meta == {"etag": '"v1"', "status": 200}
    stored = tmp_path / "26" / f"{sha}.gz"
    assert not stored.exists()  # the file follows the commit
    await session.commit()
    assert gzip.decompress(stored.read_bytes()) == BODY
    assert not list(tmp_path.rglob("*.tmp"))
    assert evidence.open_raw(row, root=tmp_path) == BODY


async def test_the_same_bytes_at_the_same_url_are_a_no_op(session, tmp_path):
    kwargs = {
        "source_key": "directories_goldenpages",
        "url": URL,
        "body": BODY,
        "content_type": "text/html",
        "root": tmp_path,
    }
    first = await evidence.store(session, fetched_at=T0, **kwargs)
    again = await evidence.store(session, fetched_at=T0 + timedelta(hours=1), **kwargs)
    assert again.id == first.id
    assert again.fetched_at == T0  # immutable: the first fetch stays
    count = await session.scalar(sa.select(sa.func.count()).select_from(Evidence))
    assert count == 1


async def test_the_same_bytes_at_two_urls_are_two_rows_and_one_file(session, tmp_path):
    a = await evidence.store(
        session,
        source_key="directories",
        url=URL,
        body=BODY,
        content_type="text/html",
        fetched_at=T0,
        root=tmp_path,
    )
    b = await evidence.store(
        session,
        source_key="directories",
        url=URL + "?page=1",
        body=BODY,
        content_type="text/html",
        fetched_at=T0,
        root=tmp_path,
    )
    assert a.id != b.id and a.raw_path == b.raw_path
    await session.commit()
    assert len(list(tmp_path.rglob("*.gz"))) == 1


async def test_a_body_over_two_megabytes_keeps_its_row_but_not_its_file(
    session, tmp_path
):
    big = b"<html>" + b"x" * (evidence.RAW_CAP + 1) + b"</html>"
    row = await evidence.store(
        session,
        source_key="directories",
        url=URL,
        body=big,
        content_type="text/html",
        fetched_at=T0,
        excerpt="Namuna katta sahifa",
        root=tmp_path,
    )
    assert row.raw_path is None
    assert row.meta["truncated"] is True
    assert (row.bytes, row.excerpt) == (len(big), "Namuna katta sahifa")
    await session.commit()
    assert list(tmp_path.rglob("*.gz")) == []
    assert evidence.open_raw(row, root=tmp_path) is None


async def test_places_evidence_expires_in_30_days(session, tmp_path):
    row = await evidence.store(
        session,
        source_key="maps_google",
        url="https://places.googleapis.com/v1/places/x",
        body=b'{"id":"x"}',
        content_type="application/json",
        fetched_at=T0,
        root=tmp_path,
    )
    assert row.expires_at == T0 + timedelta(days=30)
    assert row.title is None


async def test_the_excerpt_is_clipped_to_600(session, tmp_path):
    words = " ".join(["Namuna"] * 200)
    row = await evidence.store(
        session,
        source_key="directories",
        url=URL,
        body=words.encode(),
        content_type="text/plain",
        fetched_at=T0,
        root=tmp_path,
    )
    assert len(row.excerpt) <= 600
    assert row.excerpt.endswith("Namuna")


async def test_a_url_over_2048_is_refused(session, tmp_path):
    with pytest.raises(ValueError):
        await evidence.store(
            session,
            source_key="directories",
            url="https://a.example.uz/" + "x" * 2048,
            body=b"x",
            content_type="text/plain",
            fetched_at=T0,
            root=tmp_path,
        )


async def test_purge_raw_keeps_a_file_another_row_still_uses(session, tmp_path):
    a = await evidence.store(
        session,
        source_key="directories",
        url=URL,
        body=BODY,
        content_type="text/html",
        fetched_at=T0,
        root=tmp_path,
    )
    b = await evidence.store(
        session,
        source_key="directories",
        url=URL + "?p=2",
        body=BODY,
        content_type="text/html",
        fetched_at=T0,
        root=tmp_path,
    )
    await session.commit()
    path = tmp_path / a.raw_path
    later = T0 + timedelta(days=181)
    assert await evidence.purge_raw(session, a, now=later, root=tmp_path) is False
    assert path.is_file()
    assert (a.raw_path, a.raw_deleted_at) == (None, later)
    assert await evidence.purge_raw(session, b, now=later, root=tmp_path) is True
    assert not path.exists()


async def test_latest_stored_skips_rows_whose_file_is_gone(session, tmp_path):
    old = await evidence.store(
        session,
        source_key="directories",
        url=URL,
        body=b"<html>v1</html>",
        content_type="text/html",
        fetched_at=T0,
        root=tmp_path,
    )
    new = await evidence.store(
        session,
        source_key="directories",
        url=URL,
        body=b"<html>v2</html>",
        content_type="text/html",
        fetched_at=T0 + timedelta(days=1),
        root=tmp_path,
    )
    assert (await evidence.latest_stored(session, URL)).id == new.id
    await evidence.purge_raw(session, new, now=T0 + timedelta(days=2), root=tmp_path)
    assert (await evidence.latest_stored(session, URL)).id == old.id


# --- no file that no row points to (02 §4 R1 selects rows) ----------------------------


def files(root) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


async def test_a_rolled_back_store_leaves_no_file(session, tmp_path):
    await evidence.store(
        session,
        source_key="directories",
        url=URL,
        body=BODY,
        content_type="text/html",
        fetched_at=T0,
        root=tmp_path,
    )
    await session.rollback()
    assert files(tmp_path) == []


async def test_a_refused_savepoint_drops_only_its_own_file(session, tmp_path):
    kept = await evidence.store(
        session,
        source_key="directories",
        url=URL,
        body=BODY,
        content_type="text/html",
        fetched_at=T0,
        root=tmp_path,
    )
    with pytest.raises(sa.exc.DataError):
        async with session.begin_nested():
            dropped = await evidence.store(
                session,
                source_key="directories",
                url=URL + "?p=2",
                body=b"<html>other</html>",
                content_type="text/html",
                fetched_at=T0,
                root=tmp_path,
            )
            assert dropped.raw_path is not None
            # Something later in the same savepoint is refused (the runner's
            # observation rows): the row goes, so must its file.
            await session.execute(sa.text("SELECT 1 / 0"))
    await session.commit()
    assert files(tmp_path) == [kept.raw_path]
    count = await session.scalar(sa.select(sa.func.count()).select_from(Evidence))
    assert count == 1


async def test_the_same_bytes_in_a_new_tashkent_year_write_no_second_file(
    session, tmp_path
):
    kwargs = {
        "source_key": "directories",
        "url": URL,
        "body": BODY,
        "content_type": "text/html",
        "root": tmp_path,
    }
    first = await evidence.store(
        session, fetched_at=datetime(2026, 12, 31, 23, tzinfo=TASHKENT), **kwargs
    )
    again = await evidence.store(
        session, fetched_at=datetime(2027, 1, 1, 9, tzinfo=TASHKENT), **kwargs
    )
    await session.commit()
    assert again.id == first.id
    assert files(tmp_path) == [first.raw_path] == [f"26/{first.sha256}.gz"]


async def test_the_same_bytes_after_a_purge_stay_purged(session, tmp_path):
    kwargs = {
        "source_key": "directories",
        "url": URL,
        "body": BODY,
        "content_type": "text/html",
        "root": tmp_path,
    }
    row = await evidence.store(session, fetched_at=T0, **kwargs)
    await session.commit()
    await evidence.purge_raw(session, row, now=T0 + timedelta(days=181), root=tmp_path)
    await session.commit()
    again = await evidence.store(session, fetched_at=T0 + timedelta(days=182), **kwargs)
    await session.commit()
    assert (again.id, again.raw_path) == (row.id, None)
    assert files(tmp_path) == []


async def test_a_row_whose_file_went_missing_is_healed_by_the_same_bytes(
    session, tmp_path
):
    kwargs = {
        "source_key": "directories",
        "url": URL,
        "body": BODY,
        "content_type": "text/html",
        "root": tmp_path,
    }
    row = await evidence.store(session, fetched_at=T0, **kwargs)
    await session.commit()
    (tmp_path / row.raw_path).unlink()  # a crash between the commit and the write
    assert (
        evidence.keep_raw(session, row, b"<html>other bytes</html>", root=tmp_path)
        is False
    )
    await evidence.store(session, fetched_at=T0 + timedelta(hours=1), **kwargs)
    await session.commit()
    assert evidence.open_raw(row, root=tmp_path) == BODY
