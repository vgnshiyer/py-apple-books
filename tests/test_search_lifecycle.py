"""The ranked-search index's life: lock order and connection limits,
deadlines, resumable builds, ``close()`` during searches, garbage
collection, fork, error privacy, and which files it touches.

Synthetic libraries only (``FixtureLibrary.populate``)."""

import contextlib
import gc
import hashlib
import os
import signal
import sqlite3
import threading
import time
import traceback
import warnings
import weakref
import zipfile

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books import search
from py_apple_books.db import LibraryDB, query_deadline, use_library
from py_apple_books.exceptions import DBQueryError, QueryTimeoutError
from tests import _fs_audit

# Every populated highlight reads "synthetic highlight <n> about the
# theme of book <m>".
QUERY = "theme"


@pytest.fixture
def lib(make_library):
    lib = make_library()
    lib.populate(books=4, annotations_per_book=50)
    return lib


@pytest.fixture
def db(lib):
    db = LibraryDB(data_dir=lib.data_dir, query_timeout=None)
    yield db
    db.close()


def run(db, fn, *args, **kwargs):
    """``fn(PyAppleBooks(), ...)`` reading ``db``."""
    with use_library(db):
        return fn(PyAppleBooks(), *args, **kwargs)


def ranked(db, query=QUERY, **kwargs):
    return run(db, lambda api: api.search_annotations(query, **kwargs))


def index_of(db) -> search.AnnotationIndex:
    return db._derived[search._INDEX_KEY]


def is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


@pytest.fixture
def paused_build(monkeypatch):
    """``paused_build()``: the next build stops before reading the store
    (holding the build lock) until ``.release()``; ``.entered`` is set
    once it waits."""

    class Pause:
        def __init__(self):
            self.entered, self.gate = threading.Event(), threading.Event()

        def release(self):
            self.gate.set()

    pauses = []
    real_fill = search.AnnotationIndex._fill

    def fill(self, db, pending):
        if pauses and not pauses[0].gate.is_set():
            pauses[0].entered.set()
            assert pauses[0].gate.wait(timeout=20)
        return real_fill(self, db, pending)

    monkeypatch.setattr(search.AnnotationIndex, "_fill", fill)

    def make():
        pauses.append(Pause())
        return pauses[0]

    yield make
    for pause in pauses:
        pause.release()


def in_thread(fn):
    """Start ``fn()`` in a thread; return ``(thread, result)`` where
    ``result`` gets ``('ok', value)`` or ``('error', exception)``."""
    result = []

    def target():
        try:
            result.append(("ok", fn()))
        except BaseException as e:  # noqa: BLE001 (reported to the test)
            result.append(("error", e))

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


# -- connections and lock order ------------------------------------------------------


def test_no_deadlock_with_one_connection(lib):
    """A cold search (pooled reads under the build lock) and a thread
    listing books share one connection."""
    db = LibraryDB(data_dir=lib.data_dir, max_connections=1, query_timeout=None)
    try:
        searcher, found = in_thread(lambda: len(ranked(db, limit=None)))

        def list_books():
            for _ in range(200):
                list(run(db, lambda api: api.list_books(limit=5)))
            return 200

        lister, listed = in_thread(list_books)
        searcher.join(30)
        lister.join(30)
        assert not searcher.is_alive() and not lister.is_alive()
        assert found == [("ok", 200)] and listed == [("ok", 200)]
    finally:
        db.close()


def test_searches_share_one_build(db):
    threads = [in_thread(lambda: len(ranked(db, limit=None))) for _ in range(8)]
    for thread, _ in threads:
        thread.join(30)
    assert [result for _, result in threads] == [[("ok", 200)]] * 8
    assert index_of(db).builds == 1


def test_threads_while_rows_are_inserted(lib, db, monkeypatch):
    """Every search fingerprints the store (no recheck interval) while
    rows are added: no error, and no more builds than changes + 1."""
    monkeypatch.setattr(search, "_RECHECK", 0.0)
    book = lib.add_book("Growing")
    stop = threading.Event()

    def loop():
        n = 0
        while not stop.is_set():
            ranked(db, "growing", limit=None)
            n += 1
        return n

    workers = [in_thread(loop) for _ in range(8)]
    for i in range(10):
        lib.add_annotation(book, f"growing row {i}")
        time.sleep(0.02)
    stop.set()
    for thread, _ in workers:
        thread.join(30)
    assert all(result and result[0][0] == "ok" for _, result in workers), [r for _, r in workers]
    assert len(ranked(db, "growing", limit=None)) == 10
    assert index_of(db).builds <= 10 + 1


def test_one_fingerprint_per_interval_under_concurrency(lib, db, monkeypatch):
    """16 searches arriving together once a check is due: one of them
    fingerprints the store and the others use its result, whether the
    store is unchanged or changed (then one rebuild)."""
    now = [1000.0]
    monkeypatch.setattr(search, "_clock", lambda: now[0])
    assert len(ranked(db, limit=None)) == 200
    index = index_of(db)
    real, calls = search.AnnotationIndex._fingerprint, []

    def slow(self, db):
        calls.append(1)
        time.sleep(0.05)  # every thread arrives while it runs
        return real(self, db)

    monkeypatch.setattr(search.AnnotationIndex, "_fingerprint", slow)
    book = lib.add_book("Growing")
    for check, change in enumerate((False, True, False), start=1):
        if change:
            lib.add_annotation(book, f"one more {QUERY}")
        now[0] += search._RECHECK * 2
        barrier = threading.Barrier(16)
        threads = [in_thread(lambda: (barrier.wait(10), len(ranked(db, limit=None)))[1]) for _ in range(16)]
        for thread, _ in threads:
            thread.join(30)
        expected = 201 if check > 1 else 200
        assert [result for _, result in threads] == [[("ok", expected)]] * 16
        assert len(calls) == check
    assert index.builds == 2 and not index._check_lock.locked()


# -- deadlines -----------------------------------------------------------------


def test_a_wait_for_another_build_ends_at_the_query_deadline(db, paused_build):
    pause = paused_build()
    builder, built = in_thread(lambda: len(ranked(db, limit=None)))
    assert pause.entered.wait(10)
    start = time.monotonic()
    with use_library(db), query_deadline(0.1):
        with pytest.raises(QueryTimeoutError) as e:
            PyAppleBooks().search_annotations("secretquery")
    waited = time.monotonic() - start
    assert 0.09 <= waited < 2.0 and e.value.timeout == 0.1
    assert "secretquery" not in str(e.value) and "search index" in str(e.value)
    pause.release()
    builder.join(30)
    assert built == [("ok", 200)]


def test_a_wait_for_another_build_ends_at_the_query_timeout(lib, paused_build):
    db = LibraryDB(data_dir=lib.data_dir, query_timeout=0.05)
    try:
        pause = paused_build()
        builder, built = in_thread(lambda: len(ranked(db, limit=None)))
        assert pause.entered.wait(10)
        start = time.monotonic()
        with pytest.raises(QueryTimeoutError) as e:
            ranked(db)
        assert time.monotonic() - start < 2.0 and e.value.timeout == 0.05
        pause.release()
        builder.join(30)
        assert built == [("ok", 200)]
    finally:
        db.close()


@pytest.mark.parametrize("how", ["query_timeout", "query_deadline"])
def test_a_timeout_beyond_the_lock_maximum(lib, how):
    """A valid timeout longer than threading.TIMEOUT_MAX: the lock waits
    are clamped as the pool's are (no OverflowError)."""
    huge = 1e12
    assert huge > threading.TIMEOUT_MAX
    db = LibraryDB(data_dir=lib.data_dir, query_timeout=huge if how == "query_timeout" else None)
    try:
        scope = query_deadline(huge) if how == "query_deadline" else contextlib.nullcontext()
        with use_library(db), scope:
            api = PyAppleBooks()
            assert len(api.search_annotations(QUERY, limit=None)) == 200  # cold: the build lock
            assert len(api.search_annotations(QUERY, limit=None)) == 200  # warm: the index lock
    finally:
        db.close()


def test_an_interrupted_search_releases_its_pin_and_lock(db, monkeypatch):
    monkeypatch.setattr(search, "_RECHECK", 3600.0)
    assert len(ranked(db, limit=None)) == 200
    index = index_of(db)
    real_query = search._query

    def endless(conn, *args):
        conn.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                     "SELECT count(*) FROM c").fetchall()

    monkeypatch.setattr(search, "_query", endless)
    start = time.monotonic()
    with use_library(db), query_deadline(0.05):
        with pytest.raises(QueryTimeoutError) as e:
            PyAppleBooks().search_annotations(QUERY)
    assert time.monotonic() - start < 2.0
    assert str(e.value) == "Query took too long and was stopped (limit 0.05 s)." and e.value.timeout == 0.05
    assert e.value.__cause__ is None and e.value.__context__ is None
    gen = index._ready
    assert gen.users == 0 and not gen.lock.locked() and not index._build_lock.locked()
    monkeypatch.setattr(search, "_query", real_query)
    assert len(ranked(db, limit=None)) == 200 and index.builds == 1


def test_a_build_continues_across_timed_out_calls(lib, monkeypatch):
    """Calls with a deadline shorter than the build each add chunks; the
    build completes once, and no row is read twice except the chunk a
    timed-out call was reading."""
    monkeypatch.setattr(search, "_CHUNK", 20)
    real_run = search._run

    def slow_insert(conn, fn, deadline, limit):
        if getattr(fn, "__name__", "") == "insert":
            time.sleep(0.01)
        return real_run(conn, fn, deadline, limit)

    monkeypatch.setattr(search, "_run", slow_insert)
    db = LibraryDB(data_dir=lib.data_dir, query_timeout=None)
    try:
        attempts = 0
        while True:
            attempts += 1
            assert attempts < 200
            try:
                with use_library(db), query_deadline(0.03):
                    hits = PyAppleBooks().search_annotations(QUERY, limit=None)
                break
            except QueryTimeoutError:
                continue
        index = index_of(db)
        assert attempts > 2 and len(hits) == 200 and index.builds == 1
        assert 200 <= index.rows_fetched <= 200 + search._CHUNK * (attempts - 1)
    finally:
        db.close()


# -- close() ---------------------------------------------------------------------


def test_close_during_searches(lib, monkeypatch):
    """8 threads search while close() runs 500 times: every call returns
    without an error, and every private database is closed at the end."""
    made = []
    real_new = search._new_database

    def new_database():
        conn, fts = real_new()
        made.append(conn)
        return conn, fts

    monkeypatch.setattr(search, "_new_database", new_database)
    api = PyAppleBooks(data_dir=lib.data_dir)
    stop = threading.Event()

    def loop():
        n = 0
        while not stop.is_set():
            n += len(api.search_annotations(QUERY, limit=5))
        return n

    workers = [in_thread(loop) for _ in range(8)]
    for _ in range(500):
        api.close()
        time.sleep(0.001)
    stop.set()
    for thread, _ in workers:
        thread.join(30)
    api.close()
    results = [result for _, result in workers]
    assert all(r and r[0][0] == "ok" and r[0][1] > 0 for r in results), results
    assert made and all(is_closed(conn) for conn in made)


def test_close_never_waits_for_or_aborts_a_build(db, paused_build):
    pause = paused_build()
    builder, built = in_thread(lambda: len(ranked(db, limit=None)))
    assert pause.entered.wait(10)
    index = index_of(db)
    start = time.monotonic()
    db.close()
    assert time.monotonic() - start < 0.5 and index.dead
    assert index._pending is not None and not is_closed(index._pending.conn)
    conn = index._pending.conn
    pause.release()
    builder.join(30)
    assert built == [("ok", 200)]  # the running search finished
    assert is_closed(conn)  # closed by its last user
    assert search._INDEX_KEY not in db._derived
    assert len(ranked(db, limit=None)) == 200 and index_of(db) is not index


def test_a_discarded_index_takes_no_new_search(db, monkeypatch):
    """A search holding the index from before close() moves to the
    library's new one rather than build the closed one again."""
    assert len(ranked(db, limit=None)) == 200
    index = index_of(db)
    db.close()
    with pytest.raises(search._Dead):
        index.search(db, search._plan(QUERY))
    assert index.builds == 1 and index._ready is None
    real, handed = db._derived_cache, []

    def derived_cache(key, factory):
        if not handed:  # the first lookup returns the closed index
            handed.append(index)
            return index
        return real(key, factory)

    monkeypatch.setattr(db, "_derived_cache", derived_cache)
    assert len(ranked(db, limit=None)) == 200
    assert handed and index_of(db) is not index and index.builds == 1
    # The last attempt of a call searches a closed index rather than fail.
    assert len(index.search(db, search._plan(QUERY), if_discarded=True)) == 200
    assert index.builds == 2 and index._ready is None and index._inside == 0


def test_a_dropped_index_closes_its_database(db):
    ranked(db)
    index = index_of(db)
    conn = index._ready.conn
    db._derived.clear()  # as if dropped without close()
    del index
    gc.collect()
    assert is_closed(conn)


def test_no_reference_to_the_library(lib):
    """With the cyclic collector off, dropping the library frees it, its
    index, and the index's database: nothing refers back to it."""
    gc.collect()
    gc.disable()
    try:
        db = LibraryDB(data_dir=lib.data_dir)
        assert ranked(db, "nothingmatchesthis") == []
        index = index_of(db)
        conn = index._ready.conn
        reachable = [index, *gc.get_referents(index)]
        for obj in list(reachable):
            reachable += gc.get_referents(obj)
        assert not any(isinstance(obj, LibraryDB) for obj in reachable)
        db_ref, index_ref = weakref.ref(db), weakref.ref(index)
        del db, index, reachable, obj
        assert db_ref() is None and index_ref() is None
        assert is_closed(conn)
    finally:
        gc.enable()


# -- fork --------------------------------------------------------------------------


def _child_time_limit(seconds: int = 20) -> None:
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(seconds)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_fork(db):
    assert len(ranked(db, limit=None)) == 200
    parents = index_of(db)
    conn = parents._ready.conn
    read_end, write_end = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork() with other threads alive
        pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        status = 1
        try:
            _child_time_limit()
            os.close(read_end)
            hits = len(ranked(db, limit=None))
            mine = index_of(db)
            result = (hits, mine is not parents, [obj is parents for obj in db._derived_inherited],
                      mine.builds, parents.dead)
            db.close()
            os.write(write_end, repr(result).encode())
            status = 0
        finally:
            os._exit(status)
    os.close(write_end)
    with os.fdopen(read_end, "rb") as pipe:
        output = pipe.read()
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert output == repr((200, True, [True], 1, True)).encode()
    # The parent's index is untouched and still works.
    assert index_of(db) is parents and not parents.dead and not is_closed(conn)
    assert len(ranked(db, limit=None)) == 200 and parents.builds == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_fork_while_fts5_is_being_probed(monkeypatch):
    """A child forked while a thread holds the FTS5 probe's lock gets a
    lock of its own, rather than waiting forever in fts5_available()."""
    monkeypatch.setattr(search, "_fts5", None)
    held = search._fts5_lock
    held.acquire()  # as a thread inside the first probe holds it
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            status = 1
            try:
                _child_time_limit(10)
                status = 0 if search.fts5_available() in (True, False) else 1
            finally:
                os._exit(status)
        _, status = os.waitpid(pid, 0)
    finally:
        held.release()
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, status
    assert search._fts5_lock is held  # the parent's own lock is untouched


def test_another_process_never_uses_the_index(db):
    ranked(db)
    parents = index_of(db)
    parents._pid = -1  # as a forked child sees it
    assert parents.dead
    with pytest.raises(search._Dead):
        parents.search(db, search._plan(QUERY))


# -- privacy -------------------------------------------------------------------------


def test_an_index_error_carries_no_sqlite_message(db, monkeypatch):
    """FTS5 quotes a bad term in its message: an unquoted 'secretword:x'
    would read 'no such column: secretword'."""
    monkeypatch.setattr(search, "_quote", lambda item: "secretword:" + item)
    with pytest.raises(DBQueryError) as e:
        ranked(db, "habit")
    assert type(e.value) is DBQueryError and str(e.value) == "Ranked annotation search failed."
    assert e.value.__cause__ is None and e.value.__context__ is None
    assert "secretword" not in "".join(traceback.format_exception(e.value))


def test_the_index_stays_in_memory(db):
    ranked(db)
    conn = index_of(db)._ready.conn
    assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
    # The pragma reads back 2 even where SQLite was built to ignore it
    # (SQLITE_TEMP_STORE=0: temporary data always in files).
    assert "TEMP_STORE=0" not in [row[0] for row in conn.execute("PRAGMA compile_options")]
    assert [row[2] for row in conn.execute("PRAGMA database_list")] == [""]


def _snapshot(root):
    found = {}
    for folder, _, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            st = os.stat(path)
            with open(path, "rb") as f:
                found[path] = (st.st_size, st.st_mtime_ns, hashlib.sha256(f.read()).hexdigest())
    return found


def test_searches_change_no_file(lib, db):
    before = _snapshot(lib.root)
    ranked(db, limit=None)
    ranked(db, "theme book", require_all=True)
    run(db, lambda api: list(api.search_books("populated")))
    assert _snapshot(lib.root) == before


def test_only_the_stores_and_memory_are_opened(lib):
    """Under the audit hook: every file, listing and SQLite open during
    both searches (and reading the hits' fields and books) is a store
    file, a store folder, or ':memory:'."""
    warm = LibraryDB(data_dir=lib.data_dir)
    try:
        ranked(warm)  # loads the package's own data files once
    finally:
        warm.close()
    db = LibraryDB(data_dir=lib.data_dir)
    try:
        with _fs_audit.record() as rec:
            hits = ranked(db, limit=None)
            books = run(db, lambda api: list(api.search_books("populated book")))
            touched = [(h.annotation.selected_text, getattr(h.annotation.book, "title", None)) for h in hits]
        assert len(hits) == 200 and len(books) == 4 and len(touched) == 200
    finally:
        db.close()
    stores = {os.path.realpath(p) for p in (lib.library_path, lib.annotation_path,
                                            lib.library_path.parent, lib.annotation_path.parent)}
    events = rec.of(*_fs_audit.PATH_EVENTS)
    assert events, "the hook saw nothing"
    assert {e.path for e in events} <= stores | {":memory:"}, {e.path for e in events} - stores
    assert ":memory:" in {e.path for e in events}
    assert not rec.of(*_fs_audit.PROCESS_EVENTS)


def test_the_audit_control(tmp_path):
    """The hook sees what the test above rules out."""
    archive = tmp_path / "x.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a", "b")
    with _fs_audit.record() as rec:
        zipfile.ZipFile(archive).close()
        os.listdir(tmp_path)
    assert {os.path.realpath(archive), os.path.realpath(tmp_path)} <= set(rec.paths())
