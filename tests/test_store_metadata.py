"""Tests for the Core Data metadata reader (py_apple_books.db.metadata).

Stores are synthetic SQLite files with a ``Z_METADATA`` table shaped
like Apple Books' (binary plist, per-entity model hashes).
"""

import ast
import base64
import os
import pathlib
import plistlib
import sqlite3
import threading
import time
import types

import pytest

from py_apple_books.db import metadata
from py_apple_books.db.metadata import StoreMetadata, read_only_uri, read_store_metadata

STORE_UUID = "00000000-1111-2222-3333-444444444444"
HASHES = {
    "BKLibraryAsset": b"\x01" * 32,
    "BKCollection": b"\x02" * 32,
    "BKCollectionMember": b"\x03" * 32,
}


def _plist(hashes=HASHES, **extra) -> bytes:
    return plistlib.dumps(
        {
            "NSStoreModelVersionHashes": hashes,
            "NSPersistenceFrameworkVersion": 1526,
            "NSStoreType": "SQLite",
            **extra,
        },
        fmt=plistlib.FMT_BINARY,
    )


def _make_store(path, plist=None, store_uuid=STORE_UUID, journal_mode="DELETE"):
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA journal_mode={journal_mode}")
    conn.execute(
        "CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, "
        "Z_UUID VARCHAR(255), Z_PLIST BLOB)"
    )
    conn.execute(
        "INSERT INTO Z_METADATA VALUES (1, ?, ?)",
        (store_uuid, _plist() if plist is None else plist),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def store(tmp_path):
    return _make_store(tmp_path / "BKLibrary-1-1.sqlite")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_reads_uuid_hashes_and_entities(store):
    meta = read_store_metadata(store)
    assert meta.uuid == STORE_UUID
    assert meta.model_hashes == {
        name: base64.b64encode(value).decode("ascii") for name, value in HASHES.items()
    }
    assert meta.entities == frozenset(HASHES)
    assert meta.framework_version == "1526"


def test_path_forms_and_connection_agree(store):
    conn = sqlite3.connect(read_only_uri(store), uri=True)
    try:
        from_conn = read_store_metadata(conn)
        conn.execute("SELECT 1")  # left open for the caller
    finally:
        conn.close()
    assert read_store_metadata(str(store)) == read_store_metadata(store) == from_conn


def test_metadata_is_frozen_and_hashable(store):
    meta = read_store_metadata(store)
    with pytest.raises(AttributeError):
        meta.uuid = "other"
    assert hash(meta) == hash(read_store_metadata(store))


def test_reads_a_wal_store(tmp_path):
    path = _make_store(tmp_path / "wal.sqlite", journal_mode="WAL")
    assert read_store_metadata(path).uuid == STORE_UUID


def test_null_uuid_and_missing_framework_version(tmp_path):
    plist = plistlib.dumps({"NSStoreModelVersionHashes": HASHES}, fmt=plistlib.FMT_BINARY)
    meta = read_store_metadata(_make_store(tmp_path / "s.sqlite", plist=plist, store_uuid=None))
    assert (meta.uuid, meta.framework_version) == (None, None)
    assert meta.entities == frozenset(HASHES)


def test_xml_plist(tmp_path):
    plist = plistlib.dumps({"NSStoreModelVersionHashes": HASHES}, fmt=plistlib.FMT_XML)
    meta = read_store_metadata(_make_store(tmp_path / "s.sqlite", plist=plist))
    assert meta.entities == frozenset(HASHES)


# ---------------------------------------------------------------------------
# Nothing to read -> None
# ---------------------------------------------------------------------------


def test_no_metadata_table(tmp_path):
    path = tmp_path / "plain.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    assert read_store_metadata(path) is None


def test_empty_metadata_table(store):
    conn = sqlite3.connect(store)
    conn.execute("DELETE FROM Z_METADATA")
    conn.commit()
    conn.close()
    assert read_store_metadata(store) is None


def test_junk_file(tmp_path):
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"not a database" * 100)
    assert read_store_metadata(junk) is None


def test_missing_file_and_directory(tmp_path):
    assert read_store_metadata(tmp_path / "absent.sqlite") is None
    assert read_store_metadata(tmp_path) is None


def test_fifo_does_not_hang(tmp_path):
    # sqlite3.connect() itself would block on a FIFO until a writer
    # appeared.
    fifo = tmp_path / "BKLibrary-1-1.sqlite"
    os.mkfifo(fifo)
    result = []
    reader = threading.Thread(target=lambda: result.append(read_store_metadata(fifo)), daemon=True)
    reader.start()
    reader.join(5)
    assert result == [None]


@pytest.mark.parametrize("plist", [
    b"garbage",
    b"bplist00" + b"\xff" * 40,
    b"<?xml version='1.0'?><plist><dict><key>a</key>",
    None,
    plistlib.dumps(["not", "a", "dict"], fmt=plistlib.FMT_BINARY),
    plistlib.dumps({"NSPersistenceFrameworkVersion": 1526}, fmt=plistlib.FMT_BINARY),
    plistlib.dumps({"NSStoreModelVersionHashes": "not a dict"}, fmt=plistlib.FMT_BINARY),
], ids=["garbage", "bad-binary", "bad-xml", "null", "array", "no-hashes", "hashes-not-dict"])
def test_unusable_plist(tmp_path, plist):
    conn = sqlite3.connect(tmp_path / "s.sqlite")
    conn.execute("CREATE TABLE Z_METADATA (Z_VERSION INTEGER, Z_UUID VARCHAR, Z_PLIST BLOB)")
    conn.execute("INSERT INTO Z_METADATA VALUES (1, ?, ?)", (STORE_UUID, plist))
    conn.commit()
    conn.close()
    assert read_store_metadata(tmp_path / "s.sqlite") is None


def test_closed_connection():
    conn = sqlite3.connect(":memory:")
    conn.close()
    assert read_store_metadata(conn) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_permission_error_propagates(store):
    store.chmod(0)
    try:
        with pytest.raises(PermissionError):
            read_store_metadata(store)
    finally:
        store.chmod(0o644)


# ---------------------------------------------------------------------------
# read_only_uri
# ---------------------------------------------------------------------------


def test_read_only_uri_quotes_special_characters():
    assert read_only_uri("/tmp/a b#c?d%e.sqlite") == "file:/tmp/a%20b%23c%3Fd%25e.sqlite?mode=ro"
    assert read_only_uri(pathlib.Path("/x/y.sqlite")) == "file:/x/y.sqlite?mode=ro"


def test_reads_through_a_path_with_special_characters(tmp_path):
    folder = tmp_path / "My Books #1"
    folder.mkdir()
    assert read_store_metadata(_make_store(folder / "s.sqlite")).uuid == STORE_UUID


def test_read_only_uri_cannot_write(store):
    conn = sqlite3.connect(read_only_uri(store), uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM Z_METADATA")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Busy retry
# ---------------------------------------------------------------------------


class _ExclusiveLock:
    """Holds an EXCLUSIVE lock on a rollback-journal store from another
    thread until ``release()``."""

    def __init__(self, path):
        self._path = path
        self._locked = threading.Event()
        self._release = threading.Event()
        self._released = threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self):
        conn = sqlite3.connect(self._path, isolation_level=None)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            self._locked.set()
            self._release.wait(5)
            conn.execute("COMMIT")
        finally:
            conn.close()
            self._released.set()

    def __enter__(self):
        self._thread.start()
        assert self._locked.wait(5)
        return self

    def release(self):
        self._release.set()
        assert self._released.wait(5)

    def __exit__(self, *exc):
        self._release.set()
        self._thread.join(5)


class _Sleeps(list):
    """The reader's retry pauses; ``hooks`` run at the start of each."""

    def __init__(self):
        super().__init__()
        self.hooks = []


@pytest.fixture
def sleeps(monkeypatch):
    calls = _Sleeps()
    real_sleep = time.sleep

    def sleep(seconds):
        calls.append(seconds)
        for hook in calls.hooks:
            hook()
        real_sleep(seconds)

    monkeypatch.setattr(metadata, "time", types.SimpleNamespace(sleep=sleep))
    return calls


def test_busy_read_is_retried_once(store, sleeps):
    with _ExclusiveLock(store) as lock:
        # The first read waits busy_timeout and fails busy; another
        # thread drops the lock during the pause, so the retry succeeds.
        sleeps.hooks.append(lock.release)
        meta = read_store_metadata(store, busy_timeout=0.05)
    assert meta is not None and meta.uuid == STORE_UUID
    assert sleeps == [metadata.BUSY_RETRY_DELAY] == [0.1]


def test_still_busy_after_retry_returns_none(store, sleeps):
    with _ExclusiveLock(store):
        assert read_store_metadata(store, busy_timeout=0.05) is None
    assert sleeps == [0.1]
    assert read_store_metadata(store) is not None


def test_busy_retry_on_a_caller_connection(store, sleeps):
    conn = sqlite3.connect(read_only_uri(store), uri=True, timeout=0.05)
    try:
        with _ExclusiveLock(store) as lock:
            sleeps.hooks.append(lock.release)
            assert read_store_metadata(conn).uuid == STORE_UUID
    finally:
        conn.close()
    assert sleeps == [0.1]


def test_other_errors_are_not_retried(tmp_path, sleeps):
    path = tmp_path / "plain.sqlite"
    sqlite3.connect(path).close()
    assert read_store_metadata(path) is None
    assert sleeps == []


# ---------------------------------------------------------------------------
# Module boundaries
# ---------------------------------------------------------------------------


def test_does_not_import_the_db_client():
    tree = ast.parse(pathlib.Path(metadata.__file__).read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module)
    assert not any(m.startswith("py_apple_books") for m in modules), modules


def test_store_metadata_fields():
    meta = StoreMetadata(uuid="u", model_hashes={"A": "x"}, framework_version=None)
    assert meta.entities == frozenset({"A"})
