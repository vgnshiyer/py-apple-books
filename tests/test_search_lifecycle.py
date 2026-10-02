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

    def fill(self, db, pending, *args):
        if pauses and not pauses[0].gate.is_set():
            pauses[0].entered.set()
            assert pauses[0].gate.wait(timeout=20)
        return real_fill(self, db, pending, *args)

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


def test_a_retired_generation_is_closed_by_its_last_user(lib, db, monkeypatch):
    """A rebuild retires the generation a running search holds: it stays
    open while that search runs, and the search closes it as it leaves."""
    now = [1000.0]
    monkeypatch.setattr(search, "_clock", lambda: now[0])
    assert len(ranked(db, limit=None)) == 200
    index = index_of(db)
    old = index._ready
    entered, gate = threading.Event(), threading.Event()
    real = search._query

    def query(conn, *args):
        if conn is old.conn and not gate.is_set():
            entered.set()
            assert gate.wait(20)
        return real(conn, *args)

    monkeypatch.setattr(search, "_query", query)
    paused, result = in_thread(lambda: len(ranked(db, limit=None)))
    try:
        assert entered.wait(10)
        lib.add_annotation(lib.add_book("Growing"), f"one more {QUERY}")
        now[0] += search._RECHECK * 2
        assert len(ranked(db, limit=None)) == 201  # the rebuild retires the paused search's generation
        assert index.builds == 2 and index._ready is not old
        assert old.retired and old.users == 1 and not is_closed(old.conn)
    finally:
        gate.set()
        paused.join(30)
    assert result == [("ok", 200)]
    assert old.users == 0 and is_closed(old.conn)
    assert not is_closed(index._ready.conn)


def test_close_never_waits_for_a_build(db, paused_build):
    """close() returns at once; the paused builder then leaves the
    discarded index (closing its database as the last user) and the
    call completes on the library's new one."""
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
    assert index.builds == 0 and index.rows_fetched == 0  # it moved on, not built
    new = index_of(db)
    assert new is not index and new.builds == 1
    assert len(ranked(db, limit=None)) == 200 and index_of(db) is new


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


def test_a_build_on_a_discarded_index_stops_at_the_next_chunk(lib, monkeypatch):
    """close() during a build: the builder (not on its last attempt)
    reads no chunk after the one in flight, and the call completes on
    the library's new index."""
    monkeypatch.setattr(search, "_CHUNK", 20)
    db = LibraryDB(data_dir=lib.data_dir, query_timeout=None)
    entered, gate = threading.Event(), threading.Event()
    real_run = search._run

    def run_insert(conn, fn, deadline, limit):
        out = real_run(conn, fn, deadline, limit)
        if getattr(fn, "__name__", "") == "insert" and not entered.is_set():
            entered.set()  # one chunk in: pause the first build
            assert gate.wait(20)
        return out

    monkeypatch.setattr(search, "_run", run_insert)
    try:
        builder, built = in_thread(lambda: len(ranked(db, limit=None)))
        assert entered.wait(10)
        index = index_of(db)
        conn = index._pending.conn
        db.close()
        gate.set()
        builder.join(30)
        assert built == [("ok", 200)]
        assert index.rows_fetched == search._CHUNK and index.builds == 0
        assert is_closed(conn) and index._pending is None
        assert index_of(db) is not index and index_of(db).builds == 1
    finally:
        gate.set()
        db.close()


def test_a_wait_for_a_discarded_index_moves_on(db, monkeypatch):
    """Callers waiting for the build lock or the check lock of an index
    that close() discards stop waiting and use the library's new index,
    while the holder of the lock is still busy."""
    now = [1000.0]
    monkeypatch.setattr(search, "_clock", lambda: now[0])
    assert len(ranked(db, limit=None)) == 200
    first = index_of(db)
    db.close()
    # The build lock: a builder of the next index is paused holding it.
    index = db._derived_cache(search._INDEX_KEY, search.AnnotationIndex)
    assert index is not first
    entered, gate = threading.Event(), threading.Event()
    real_fill = search.AnnotationIndex._fill

    def fill(self, db, pending, *args):
        if self is index:
            entered.set()
            assert gate.wait(20)
        return real_fill(self, db, pending, *args)

    monkeypatch.setattr(search.AnnotationIndex, "_fill", fill)
    try:
        builder, built = in_thread(lambda: len(ranked(db, limit=None)))
        assert entered.wait(10)
        waiter, waited = in_thread(lambda: len(ranked(db, limit=None)))
        deadline = time.monotonic() + 10
        while index._inside < 2:  # the waiter is inside, at the build lock
            assert time.monotonic() < deadline
            time.sleep(0.005)
        time.sleep(0.1)
        db.close()
        waiter.join(10)
        assert waited == [("ok", 200)] and index._build_lock.locked()  # the builder is still paused
    finally:
        gate.set()
    builder.join(30)
    assert built == [("ok", 200)] and index.builds == 0
    # The check lock: a fingerprint of the current index is due, and
    # another thread holds the lock.
    current = index_of(db)
    assert current is not index and current.builds == 1
    now[0] += search._RECHECK * 2
    with current._check_lock:
        waiter, waited = in_thread(lambda: len(ranked(db, limit=None)))
        deadline = time.monotonic() + 10
        while current._inside < 1:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        time.sleep(0.1)
        db.close()
        waiter.join(10)
        assert waited == [("ok", 200)]
    assert current.builds == 1 and index_of(db) is not current


def test_closes_during_cold_searches_stack_no_builds(lib, monkeypatch):
    """16 threads search while close() runs 40 times, 20 ms apart, during
    builds: every call returns every hit; a build by a call that can
    move on (not its last attempt) inserts at most the chunk in flight
    once its index is discarded; one build at a time per index; and the
    last attempts share one index until an index completes a build while
    current (once the closes stop, or if one is slower than a build).
    So at most two full builds run at once (the current index's and the
    shared one's), plus at most one chunk in flight on each discarded
    index. Before, each close() during a build added one more
    concurrent full build (5-9 here), and at 20,000 annotations the
    calls waiting behind them hit the 30 s timeout.

    Apart from the 30 s timeout, the bounds asserted hold however the
    threads are scheduled. The number of fills at once does not (on a
    busy machine more discarded indexes are still finishing their
    chunk): reported, not asserted."""
    lib.populate(books=10, annotations_per_book=100)  # 1,000 more rows
    expected = len(PyAppleBooks(data_dir=lib.data_dir).search_annotations(QUERY, limit=None))
    assert expected == 1200
    monkeypatch.setattr(search, "_CHUNK", 50)
    db = LibraryDB(data_dir=lib.data_dir)  # the default 30 s timeout
    local = threading.local()
    lock = threading.Lock()
    fills = []  # [index, if_discarded, inserts once the index was discarded]
    running = {}  # index -> fills running on it
    most_per_index, most = [0], [0]
    ends = [0]  # builds completed while current: each ends the shared index's run
    movable_started = threading.Event()
    real_search, real_fill, real_run, real_end = (search.AnnotationIndex.search, search.AnnotationIndex._fill,
                                                  search._run, search._end_finisher)

    def index_search(self, db, plan, **kwargs):
        local.if_discarded = kwargs.get("if_discarded", False)
        return real_search(self, db, plan, **kwargs)

    def fill(self, db, pending, *args):
        record = [self, local.if_discarded, 0]
        with lock:
            fills.append(record)
            running[self] = running.get(self, 0) + 1
            most_per_index[0] = max(most_per_index[0], running[self])
            most[0] = max(most[0], sum(running.values()))
        if not record[1]:
            movable_started.set()
        local.fill = record
        try:
            return real_fill(self, db, pending, *args)
        finally:
            local.fill = None
            with lock:
                running[self] -= 1

    def run_insert(conn, fn, deadline, limit):
        record = getattr(local, "fill", None)
        if record is not None and getattr(fn, "__name__", "") == "insert":
            if record[0]._dead:
                record[2] += 1
            time.sleep(0.005)  # a build spans several close() calls
        return real_run(conn, fn, deadline, limit)

    def end_finisher(of):
        if of is db:
            with lock:
                ends[0] += 1
        return real_end(of)

    monkeypatch.setattr(search.AnnotationIndex, "search", index_search)
    monkeypatch.setattr(search.AnnotationIndex, "_fill", fill)
    monkeypatch.setattr(search, "_run", run_insert)
    monkeypatch.setattr(search, "_end_finisher", end_finisher)
    stop = threading.Event()

    def loop():
        counts = []
        while not stop.is_set():
            counts.append(len(ranked(db, limit=None)))
        return counts

    workers = [in_thread(loop) for _ in range(16)]
    try:
        # A build by a call that can move on is under way before the
        # first close(), however slowly the threads start.
        assert movable_started.wait(20)
        for _ in range(40):
            time.sleep(0.02)
            db.close()
        stop.set()
        for thread, _ in workers:
            thread.join(60)
    finally:
        stop.set()
        db.close()
    results = [result for _, result in workers]
    assert all(r and r[0][0] == "ok" and r[0][1] and set(r[0][1]) == {expected} for r in results), [
        r and (r[0][0], r[0][0] == "error" and type(r[0][1]).__name__) for r in results]
    movable = [record for record in fills if not record[1]]
    assert movable and max(record[2] for record in movable) <= 1, [record[2] for record in movable]
    assert most_per_index[0] == 1
    # A last attempt registers a new shared index only after a build
    # completed while current ended the previous one's run.
    shared = {id(record[0]) for record in fills if record[1]}
    assert len(shared) <= 1 + ends[0], (len(shared), ends[0], most[0])


def test_last_attempts_share_one_index(lib, db):
    """The index a call's last attempt uses: the library's current one,
    registered, until an index of the library completes a build while
    not discarded; meanwhile every last attempt gets the registered one,
    even once it is discarded."""
    first = db._derived_cache(search._INDEX_KEY, search.AnnotationIndex)
    assert search._finisher(db, first) is first
    db.close()
    second = db._derived_cache(search._INDEX_KEY, search.AnnotationIndex)
    assert second is not first and first.dead
    assert search._finisher(db, second) is first
    # The last attempt finishes on it: one build, of the discarded index.
    assert len(first.search(db, search._plan(QUERY), if_discarded=True)) == 200
    assert first.builds == 1 and search._finisher(db, second) is first
    assert len(ranked(db, limit=None)) == 200  # second builds, not discarded
    assert second.builds == 1 and db not in search._finishers
    assert search._finisher(db, second) is second
    other = LibraryDB(data_dir=lib.data_dir, query_timeout=None)
    try:
        current = other._derived_cache(search._INDEX_KEY, search.AnnotationIndex)
        assert search._finisher(other, current) is current  # per library
    finally:
        other.close()


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


def test_book_files_and_icloud_drive_are_never_touched(make_library):
    """Books whose bundles are under iCloud Drive (one evicted: ZSTATE 3
    and an .icloud stub): both searches, book_id given as a Book, and the
    hits' books, all under block(), which refuses the bundles and
    anything under Mobile Documents."""
    lib = make_library()
    docs = lib.root / "Library" / "Mobile Documents" / "iCloud~com~apple~iBooks" / "Documents"
    bundles = []
    for i in range(3):
        bundle = docs / f"Bundle{i}.epub"
        (bundle / "OEBPS").mkdir(parents=True)
        (bundle / "OEBPS" / "chapter.xhtml").write_text("<html/>")
        bundles.append(bundle)
    (docs / ".Bundle2.epub.icloud").write_text("stub")
    books = [lib.add_book(f"Lantern {i}", "Quill Author", path=bundle, state=3 if i == 2 else 1)
             for i, bundle in enumerate(bundles)]
    for book in books:
        for j in range(5):
            lib.add_annotation(book, f"lantern passage {j}", note="lantern note")
    db = LibraryDB(data_dir=lib.data_dir)
    try:
        with use_library(db):
            api = PyAppleBooks()
            with _fs_audit.block(_fs_audit.Policy.for_library(lib.root, books=bundles)) as rec:
                hits = api.search_annotations("lantern passage", limit=None)
                evicted = api.get_book_by_id(books[2]["id"])
                mine = api.search_annotations("lantern", book_id=evicted, limit=None)
                found = list(api.search_books("lantern quill"))
                seen = [(hit.annotation.book.title, hit.annotation.book.path) for hit in hits + mine]
    finally:
        db.close()
    assert len(hits) == 15 and len(mine) == 5 and len(found) == 3
    assert all(title and path for title, path in seen)
    assert rec.refused == []
    paths = {e.path for e in rec.of(*_fs_audit.PATH_EVENTS)}
    assert paths and not any("mobile documents" in path.casefold() for path in paths)
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
