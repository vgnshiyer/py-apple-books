"""LibraryDB.schema(): keys, cache refresh, errors and deadline (G3.1
support, R24)."""

import functools
import logging
import os
import sqlite3
import time

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.db import client
from py_apple_books.db.client import SCHEMA_RECHECK
from py_apple_books.exceptions import LibraryNotFoundError, QueryTimeoutError


def _columns(path) -> dict:
    conn = sqlite3.connect(path)
    try:
        tables = [n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {t: frozenset(r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')) for t in tables}
    finally:
        conn.close()


@pytest.fixture
def clock(lib_db):
    now = [1000.0]
    lib_db._clock = lambda: now[0]
    return now


def test_schema_keys(lib_db):
    lib = lib_db.fixture
    schema = lib_db.schema()
    expected = _columns(lib.library_path)
    expected.update((f"anno_db.{t}", cols) for t, cols in _columns(lib.annotation_path).items())
    assert schema == expected
    assert "ZTITLE" in schema["ZBKLIBRARYASSET"]
    assert "ZANNOTATIONSELECTEDTEXT" in schema["anno_db.ZAEANNOTATION"]
    assert "ZAEANNOTATION" not in schema
    assert lib_db.schema() is schema


def test_schema_without_annotation_store(make_library):
    lib = make_library()
    lib.annotation_path.unlink()
    db = LibraryDB(data_dir=lib.data_dir)
    assert db.schema() == _columns(lib.library_path)
    db.close()


def test_junk_canonical_file(make_library, caplog):
    """Random bytes where the library should be: the fallback picks the
    file and every query reports it as not a library."""
    lib = make_library()
    lib.library_path.write_bytes(os.urandom(16384))
    db = LibraryDB(data_dir=lib.data_dir)
    with caplog.at_level(logging.WARNING, logger="py_apple_books.db"):
        assert db.paths().library == lib.library_path
    assert "falling back" in caplog.text
    for call in (lambda: db.execute("SELECT count(*) FROM ZBKLIBRARYASSET"), db.schema):
        with pytest.raises(LibraryNotFoundError, match="not a SQLite database") as exc:
            call()
        assert not isinstance(exc.value, sqlite3.Error)
    with use_library(db), pytest.raises(LibraryNotFoundError):
        list(PyAppleBooks().list_books())
    with pytest.raises(LibraryNotFoundError):
        db.library_path(strict=True)
    db.close()


def test_new_column_after_recheck(lib_db, clock):
    lib = lib_db.fixture
    first = lib_db.schema()
    assert "ZNEWCOLUMN" not in first["ZBKLIBRARYASSET"]
    lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET ADD COLUMN ZNEWCOLUMN INTEGER")

    clock[0] += SCHEMA_RECHECK / 2
    assert lib_db.schema() is first
    clock[0] += SCHEMA_RECHECK
    assert "ZNEWCOLUMN" in lib_db.schema()["ZBKLIBRARYASSET"]


def test_unchanged_schema_is_kept_after_recheck(lib_db, clock):
    first = lib_db.schema()
    lib_db.fixture.add_book("Rows don't change the schema")
    clock[0] += SCHEMA_RECHECK * 3
    assert lib_db.schema() is first


def test_invalidate_schema(lib_db, clock):
    lib = lib_db.fixture
    assert "ZOTHER" not in lib_db.schema()["anno_db.ZAEANNOTATION"]
    lib.execute("annotations", "ALTER TABLE ZAEANNOTATION ADD COLUMN ZOTHER TEXT")
    assert "ZOTHER" not in lib_db.schema()["anno_db.ZAEANNOTATION"]
    lib_db.invalidate_schema()
    assert "ZOTHER" in lib_db.schema()["anno_db.ZAEANNOTATION"]


def test_replaced_store_rebuilds_the_schema(lib_db, make_library, clock):
    lib = lib_db.fixture
    assert "ZEXTRA" not in lib_db.schema()["ZBKLIBRARYASSET"]
    other = make_library()
    other.execute("library", "ALTER TABLE ZBKLIBRARYASSET ADD COLUMN ZEXTRA TEXT")
    os.replace(other.library_path, lib.library_path)
    clock[0] += SCHEMA_RECHECK
    assert "ZEXTRA" in lib_db.schema()["ZBKLIBRARYASSET"]


class _SlowConnection(sqlite3.Connection):
    """Sleeps in the progress handler, so any statement run under a
    deadline, a PRAGMA included, overruns it."""

    def set_progress_handler(self, handler, n):
        if handler is None:
            return super().set_progress_handler(None, n)

        def slow():
            time.sleep(0.005)
            return handler()

        return super().set_progress_handler(slow, 1)


def test_schema_honours_the_deadline(make_library, monkeypatch):
    lib = make_library()
    monkeypatch.setattr(client.sqlite3, "connect",
                        functools.partial(sqlite3.connect, factory=_SlowConnection))
    db = LibraryDB(data_dir=lib.data_dir, query_timeout=0.05)
    start = time.monotonic()
    with pytest.raises(QueryTimeoutError, match=r"limit 0\.05 s"):
        db.schema()
    assert time.monotonic() - start < 1
    db.close()
    monkeypatch.undo()
    assert "ZBKLIBRARYASSET" in db.schema()
    db.close()
