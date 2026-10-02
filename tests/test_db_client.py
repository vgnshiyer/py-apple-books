"""The connection layer (F11, G2.4): LibraryDB discovery errors, store
freshness, the pool, deadlines, and the AppleBooksDBClient compatibility
surface."""

import datetime
import os
import pathlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import warnings

import pytest

import py_apple_books
from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, query_deadline, use_library
from py_apple_books.db import client
from py_apple_books.db.client import ANNOTATION_RETRY, IDENTITY_RECHECK, AppleBooksDBClient
from py_apple_books.exceptions import (
    AnnotationStoreNotFoundError,
    AppleBooksError,
    DBConnectionError,
    DBQueryError,
    LibraryAccessDeniedError,
    LibraryNotFoundError,
    QueryTimeoutError,
)
from py_apple_books.testing.fixture import SCHEMAS_DIR, build_store
from py_apple_books.text import fold_for_match

COUNT_BOOKS = "SELECT count(*) FROM ZBKLIBRARYASSET"
TITLES = "SELECT ZTITLE FROM ZBKLIBRARYASSET ORDER BY Z_PK"
HIGHLIGHTS = "SELECT ZANNOTATIONSELECTEDTEXT FROM anno_db.ZAEANNOTATION ORDER BY Z_PK"
# Never ends on its own.
RUNAWAY = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r"

needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")

# Run in another process: whether some process holds a lock on SQLite's
# SHARED byte range of the file. A WAL reader holds it while it is open,
# which keeps others from checkpointing and deleting the -wal under it.
SHARED_RANGE_PROBE = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR)\n"
    "try:\n"
    "    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 510, 0x40000002)\n"
    "    print('free')\n"
    "except OSError:\n"
    "    print('held')\n"
)


def _shared_lock_held(path):
    proc = subprocess.run([sys.executable, "-c", SHARED_RANGE_PROBE, str(path)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout.strip() == "held"


# -- import and discovery -------------------------------------------------------


def test_import_and_first_query_without_a_library(tmp_path):
    tree = pathlib.Path(py_apple_books.__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
    env.update(HOME=str(tmp_path), PYTHONPATH=os.pathsep.join(filter(None, [str(tree), env.get("PYTHONPATH")])))
    code = (
        "import py_apple_books, py_apple_books.content\n"
        "from py_apple_books import PyAppleBooks\n"
        "from py_apple_books.exceptions import AppleBooksError, DBConnectionError, LibraryNotFoundError\n"
        "api = PyAppleBooks()\n"
        "try:\n"
        "    list(api.list_books())\n"
        "except LibraryNotFoundError as e:\n"
        "    assert isinstance(e, DBConnectionError) and isinstance(e, AppleBooksError)\n"
        "    print(e)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.startswith("No Apple Books library store found.")
    assert str(tmp_path) not in proc.stdout


def test_no_io_before_first_use(tmp_path):
    db = LibraryDB(data_dir=tmp_path / "nowhere")
    AppleBooksDBClient(db)
    with pytest.raises(LibraryNotFoundError) as exc:
        db.execute("SELECT 1")
    assert isinstance(exc.value, DBConnectionError) and isinstance(exc.value, AppleBooksError)


@needs_permissions
def test_access_denied(lib_db):
    folder = lib_db.fixture.library_path.parent
    folder.chmod(0)
    try:
        with pytest.raises(LibraryAccessDeniedError, match="Full Disk Access") as exc:
            lib_db.execute(COUNT_BOOKS)
    finally:
        folder.chmod(0o755)
    assert isinstance(exc.value, DBConnectionError)
    assert str(folder) not in str(exc.value)
    assert lib_db.execute(COUNT_BOOKS) == [(0,)]  # not cached


@needs_permissions
def test_access_denied_to_an_explicit_store(lib_db):
    path = lib_db.fixture.library_path
    path.chmod(0)
    try:
        with pytest.raises(LibraryAccessDeniedError) as exc:
            LibraryDB(library_db=path).execute(COUNT_BOOKS)
    finally:
        path.chmod(0o644)
    assert exc.value.path == path


@pytest.mark.parametrize("failure", [pytest.param("unreadable", marks=needs_permissions), "removed"])
def test_failed_connect_keeps_this_process_locks(make_library, failure):
    """A connect that fails on one store doesn't open() and close() the
    store files to find out why: closing a descriptor drops every POSIX
    lock this process holds on the file, including the pool's."""
    lib = make_library(journal_mode="WAL")
    lib.add_book("Synthetic Book")
    db = LibraryDB(data_dir=lib.data_dir)
    with db.connection() as held:
        assert held.execute(COUNT_BOOKS).fetchone() == (1,)
        assert _shared_lock_held(lib.library_path)
        # The next statement needs a second connection; its ATTACH fails.
        if failure == "unreadable":
            lib.annotation_path.chmod(0)
            try:
                with pytest.raises(LibraryAccessDeniedError) as exc:
                    db.execute(COUNT_BOOKS)
            finally:
                lib.annotation_path.chmod(0o644)
            assert exc.value.path == lib.annotation_path
        else:
            lib.annotation_path.rename(lib.root / "moved.sqlite")
            assert db.execute(COUNT_BOOKS) == [(1,)]  # resolved again, without annotations
            assert db.paths().annotations is None
        assert _shared_lock_held(lib.library_path)
    db.close()


def test_store_removed_after_its_path_was_resolved(lib_db):
    assert lib_db.paths().library == lib_db.fixture.library_path
    lib_db.fixture.library_path.unlink()
    with pytest.raises(LibraryNotFoundError, match="No Apple Books library store found"):
        lib_db.execute(COUNT_BOOKS)


def test_missing_annotation_store(make_library):
    """Books and collections work; annotation queries raise until the
    store shows up, which is noticed ANNOTATION_RETRY seconds later."""
    lib = make_library()
    shutil.rmtree(lib.annotation_path.parent)
    book = lib.add_book("Synthetic Book")
    db = LibraryDB(data_dir=lib.data_dir)
    now = [1000.0]
    db._clock = lambda: now[0]
    api = PyAppleBooks()
    with use_library(db):
        assert [b.title for b in api.list_books()] == ["Synthetic Book"]
        assert list(api.list_collections()) == []
        with pytest.raises(AnnotationStoreNotFoundError, match="No Apple Books annotation store found"):
            list(api.list_annotations())
    assert not db.has_annotations()

    build_store(SCHEMAS_DIR / lib.schema / "AEAnnotation.sql", lib.annotation_path)
    lib.add_annotation(book, "a synthetic highlight")
    now[0] += ANNOTATION_RETRY - 1
    with pytest.raises(AnnotationStoreNotFoundError):
        db.execute(HIGHLIGHTS)
    now[0] += 1
    with use_library(db):
        assert [a.selected_text for a in api.list_annotations()] == ["a synthetic highlight"]
    assert db.has_annotations()
    db.close()


@pytest.mark.parametrize("store", ["library", "annotations"])
def test_replaced_store_is_seen_on_the_next_call(lib_db, make_library, store):
    """Seen by the next statement once IDENTITY_RECHECK has passed since
    the files were last checked."""
    now = [1000.0]
    lib_db._clock = lambda: now[0]
    old = lib_db.fixture
    old.add_annotation(old.add_book("Old"), "old")
    assert (lib_db.execute(TITLES), lib_db.execute(HIGHLIGHTS)) == ([("Old",)], [("old",)])

    new = make_library()
    new.add_annotation(new.add_book("New"), "new")
    if store == "library":
        os.replace(new.library_path, old.library_path)
        expected = ([("New",)], [("old",)])
    else:
        os.replace(new.annotation_path, old.annotation_path)
        expected = ([("Old",)], [("new",)])
    now[0] += 2 * IDENTITY_RECHECK
    assert (lib_db.execute(TITLES), lib_db.execute(HIGHLIGHTS)) == expected


def test_wal_writes_are_seen_and_checkpoints_not_blocked(make_library):
    lib = make_library(journal_mode="WAL")
    db = LibraryDB(data_dir=lib.data_dir)
    assert db.execute(COUNT_BOOKS) == [(0,)]
    lib.add_book("Written in place")
    assert db.execute(COUNT_BOOKS) == [(1,)]

    writer = sqlite3.connect(lib.library_path, timeout=0)
    try:
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0  # not busy
    finally:
        writer.close()
    assert os.path.getsize(f"{lib.library_path}-wal") == 0
    assert db.execute(COUNT_BOOKS) == [(1,)]
    db.close()


# -- pool -----------------------------------------------------------------------


def test_pool_bounds(lib_db, monkeypatch):
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, max_idle=2, max_connections=3)
    lock = threading.Lock()
    state = {"out": 0, "peak_out": 0, "open": 0, "peak_open": 0}

    def track(key, delta):
        with lock:
            state[key] += delta
            state[f"peak_{key}"] = max(state[f"peak_{key}"], state[key])

    real_connect, real_close = db._connect, client._close_quietly

    def connect(*args):
        conn = real_connect(*args)
        track("open", 1)
        return conn

    def close(conn):
        track("open", -1)
        real_close(conn)

    db._connect = connect
    monkeypatch.setattr(client, "_close_quietly", close)

    def worker():
        for _ in range(5):
            with db.connection() as conn:
                track("out", 1)
                assert conn.execute("SELECT 1").fetchone() == (1,)
                time.sleep(0.01)
                track("out", -1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak_out"] == 3
    assert state["peak_open"] <= 3
    assert state["open"] == len(db._idle) <= 2
    db.close()
    assert state["open"] == 0


def test_waiting_for_a_connection_counts_against_the_deadline(lib_db):
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, max_connections=1, query_timeout=0.1)
    with db.connection():
        with pytest.raises(QueryTimeoutError, match=r"Timed out waiting for a database connection \(limit 0\.1 s\)"):
            db.execute("SELECT 1")
    assert db.execute("SELECT 1") == [(1,)]
    db.close()


def test_transaction_left_open_is_rolled_back(lib_db):
    with lib_db.connection() as conn:
        conn.execute("BEGIN")
        conn.execute(COUNT_BOOKS)
    with lib_db.connection() as again:
        assert again is conn and not again.in_transaction


def test_connection_is_returned_without_a_progress_handler(lib_db):
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, query_timeout=None, max_idle=1)
    with db.connection() as conn:
        conn.set_progress_handler(lambda: 1, 1)  # would stop every statement
    assert db.execute("SELECT 1") == [(1,)]
    db.close()


def test_dropping_a_library_closes_its_connections(lib_db):
    """Without this, sqlite3 connections wait for the cyclic garbage
    collector, keeping their files open."""
    db = LibraryDB(data_dir=lib_db.fixture.data_dir)
    assert db.execute(COUNT_BOOKS) == [(0,)]
    conn = db._idle[0].conn
    del db
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")

    with LibraryDB(data_dir=lib_db.fixture.data_dir) as db:
        assert db.execute(COUNT_BOOKS) == [(0,)]
        conn = db._idle[0].conn
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert db.execute(COUNT_BOOKS) == [(0,)]  # still usable
    db.close()


def test_close_keeps_the_library_usable(lib_db):
    assert lib_db.execute(COUNT_BOOKS) == [(0,)]
    [pooled] = lib_db._idle
    lib_db.close()
    assert lib_db._idle == []
    with pytest.raises(sqlite3.ProgrammingError):
        pooled.conn.execute("SELECT 1")
    lib_db.fixture.add_book("Synthetic Book")
    assert lib_db.execute(COUNT_BOOKS) == [(1,)]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_fork_child(lib_db):
    """The child makes its own connections; the parent's stay unused."""
    lib_db.fixture.add_book("Synthetic Book")
    assert lib_db.execute(COUNT_BOOKS) == [(1,)]
    [parents] = lib_db._idle
    read_end, write_end = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork() with other threads alive
        pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        status = 1
        try:
            # A deadlock in the child ends it (SIGALRM's default action)
            # and fails the test, instead of hanging the suite.
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(20)
            os.close(read_end)
            with use_library(lib_db):
                result = (lib_db.execute(COUNT_BOOKS), [b.title for b in PyAppleBooks().list_books()])
            own = [p.conn is not parents.conn and p.pid == os.getpid() for p in lib_db._idle]
            result += (own, [p is parents for p in lib_db._inherited])
            os.write(write_end, repr(result).encode())
            status = 0
        finally:
            os._exit(status)
    os.close(write_end)
    with os.fdopen(read_end, "rb") as pipe:
        output = pipe.read()
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert output == repr(([(1,)], ["Synthetic Book"], [True], [True])).encode()
    assert lib_db.execute(COUNT_BOOKS) == [(1,)]
    assert lib_db._idle == [parents]


def test_fork_bookkeeping(lib_db):
    """What a (grand)child does on first use, simulated by changing the
    recorded pid."""
    assert lib_db.execute(COUNT_BOOKS) == [(0,)]
    [first] = lib_db._idle
    lib_db._pid = -1  # a child
    lib_db.invalidate_schema()
    assert lib_db._inherited == [first] and lib_db._idle == []
    assert lib_db.execute(COUNT_BOOKS) == [(0,)]
    [second] = lib_db._idle
    lib_db._pid = -2  # a grandchild keeps both generations referenced
    assert lib_db.execute(COUNT_BOOKS) == [(0,)]
    assert lib_db._inherited == [first, second] and lib_db._idle[0] not in (first, second)
    for pooled in lib_db._inherited:
        pooled.conn.close()


# -- deadlines and errors -----------------------------------------------------


def test_timeout_interrupts_a_runaway_query(lib_db):
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, query_timeout=0.2)
    start = time.monotonic()
    with pytest.raises(QueryTimeoutError, match=r"Query took too long and was stopped \(limit 0\.2 s\)") as exc:
        db.execute(RUNAWAY)
    assert time.monotonic() - start < 1
    assert exc.value.timeout == 0.2 and isinstance(exc.value, DBQueryError)
    assert db.execute("SELECT 1") == [(1,)]
    db.close()


def test_query_deadline(lib_db):
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, query_timeout=None)
    start = time.monotonic()
    with query_deadline(0.2), query_deadline(60):  # the inner one can't extend it
        with pytest.raises(QueryTimeoutError, match=r"limit 0\.2 s"):
            db.execute(RUNAWAY)
    assert time.monotonic() - start < 1
    with query_deadline(None):
        assert db.execute("SELECT 1") == [(1,)]
    db.close()


@pytest.mark.parametrize("pooled", [True, False])
def test_lock_waits_end_at_the_deadline(make_library, pooled):
    """Waiting for another connection's lock (rollback journal) ends at
    the deadline, on a pooled connection or while connecting."""
    lib = make_library()
    if pooled:
        db = LibraryDB(data_dir=lib.data_dir, query_timeout=0.3)
        assert db.execute(COUNT_BOOKS) == [(0,)]
    else:  # explicit files: no discovery
        db = LibraryDB(library_db=lib.library_path, annotation_db=lib.annotation_path,
                       query_timeout=0.3)
    writer = sqlite3.connect(lib.library_path, isolation_level=None, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        with pytest.raises(QueryTimeoutError, match=r"Timed out waiting for a locked database \(limit 0\.3 s\)"):
            db.execute(COUNT_BOOKS)
        assert time.monotonic() - start < 1.5
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert db.execute(COUNT_BOOKS) == [(0,)]
    db.close()


def test_a_sooner_deadline_shortens_the_lock_wait(make_library):
    lib = make_library()
    db = LibraryDB(data_dir=lib.data_dir, query_timeout=None, max_idle=1)
    assert db.execute(COUNT_BOOKS) == [(0,)]
    writer = sqlite3.connect(lib.library_path, isolation_level=None, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        with query_deadline(0.3), pytest.raises(QueryTimeoutError, match=r"locked database \(limit 0\.3 s\)"):
            db.execute(COUNT_BOOKS)
        assert time.monotonic() - start < 1.5
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert db.execute(COUNT_BOOKS) == [(0,)]
    assert db._idle[0].busy == client._BUSY_TIMEOUT  # back to the usual wait
    db.close()


def test_progress_handler_is_cleared(lib_db):
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, query_timeout=0.05, max_idle=1)
    assert db.execute("SELECT 1") == [(1,)]
    time.sleep(0.1)  # that statement's deadline has passed
    with db.connection() as conn:  # the same connection, used without a deadline
        sql = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 200000) SELECT count(*) FROM r"
        assert conn.execute(sql).fetchone() == (200000,)
    db.close()


def test_errors_are_typed(lib_db):
    def boom(conn):
        raise RuntimeError("boom")

    with pytest.raises(DBQueryError, match="Unexpected error while executing query") as exc:
        lib_db.execute("SELECT ?", [datetime.datetime(2000, 1, 1)])
    assert isinstance(exc.value.__cause__, TypeError)
    with pytest.raises(DBQueryError, match="Unexpected error while executing query: boom"):
        lib_db._run(boom)
    with pytest.raises(DBQueryError, match="^Error executing query: no such table"):
        lib_db.execute("SELECT * FROM no_such_table")
    with pytest.raises(DBQueryError, match="^Error executing query"):
        lib_db.execute("SELECT ?", ())
    with pytest.raises(DBQueryError, match="readonly|read-only"):
        lib_db.execute("DELETE FROM ZBKLIBRARYASSET")


def test_huge_int_is_bound_as_text(lib_db):
    assert lib_db.execute("SELECT ?, typeof(?)", [2**63, 2**63]) == [(str(2**63), "text")]


# -- AppleBooksDBClient -------------------------------------------------------


def test_client_uses_the_current_library(lib_db):
    lib_db.fixture.add_book("Synthetic Book")
    client_ = AppleBooksDBClient()
    with use_library(lib_db):
        assert client_.execute(TITLES) == [("Synthetic Book",)]
    assert AppleBooksDBClient(lib_db).execute(TITLES) == [("Synthetic Book",)]


def test_conn_and_cursor_compat(lib_db):
    lib = lib_db.fixture
    lib.add_annotation(lib.add_book("Synthetic Book"), "a synthetic highlight")
    client_ = AppleBooksDBClient(lib_db)
    with pytest.warns(DeprecationWarning, match=r"AppleBooksDBClient\.conn is deprecated"):
        conn = client_.conn
    assert conn.execute(HIGHLIGHTS).fetchall() == [("a synthetic highlight",)]
    assert conn.execute("SELECT abk_fold(?)", ["Ｃafé’s"]).fetchone() == (fold_for_match("Ｃafé’s"),)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM ZBKLIBRARYASSET")
    with pytest.warns(DeprecationWarning, match=r"AppleBooksDBClient\.conn is deprecated"):
        cursor = client_.cursor
    assert cursor.connection is conn
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert client_.conn is conn and client_.cursor is cursor

    outcome = {}

    def worker():
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError as e:
            outcome["error"] = e

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert "error" in outcome  # like 1.9's, bound to the thread that opened it

    client_.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert client_.execute(COUNT_BOOKS) == [(1,)]  # the pool is untouched
    client_.close()  # nothing left to close


def test_assigned_cursor_runs_the_queries(lib_db):
    """1.9 ran queries on ``self.cursor``; an assigned one still does."""

    class FailingCursor:
        def execute(self, *args):
            raise RuntimeError("boom")

    client_ = AppleBooksDBClient(lib_db)
    client_.cursor = FailingCursor()
    with pytest.raises(DBQueryError, match="Unexpected error while executing query: boom"):
        client_.execute(COUNT_BOOKS)
    client_.close()
    assert client_.execute(COUNT_BOOKS) == [(0,)]
