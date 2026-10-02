"""``LibraryDB._derived_cache``: the slot for per-library objects built
from the stores (the 1.11 annotation search index and book-info memo),
dropped by ``close()`` and by a fork."""

import os
import threading
import time
import warnings

import pytest

from py_apple_books.db import LibraryDB, client

COUNT_BOOKS = "SELECT count(*) FROM ZBKLIBRARYASSET"


class Derived:
    """A well-behaved derived object: O(1) to make, queries later under
    its own lock, never blocks in ``discard()``."""

    made = 0

    def __init__(self):
        type(self).made += 1
        self._state = threading.Lock()
        self._dead = False
        self.discarded = 0
        self.queries = 0

    @property
    def dead(self) -> bool:
        return self._dead

    def discard(self) -> None:
        self.discarded += 1
        self._dead = True

    def count_books(self, db):
        with self._state:
            self.queries += 1
            return db.execute(COUNT_BOOKS)[0][0]


@pytest.fixture(autouse=True)
def _reset_count():
    Derived.made = 0


def _lock_is_free_elsewhere(db) -> bool:
    """Whether another thread can take ``db._lock`` right now."""
    got = []

    def probe():
        if db._lock.acquire(timeout=0.5):
            db._lock.release()
            got.append(True)

    t = threading.Thread(target=probe)
    t.start()
    t.join(timeout=5)
    return got == [True]


def test_one_object_per_key_until_dead(tmp_path):
    db = LibraryDB(data_dir=tmp_path / "nowhere")  # no I/O: no store needed
    first = db._derived_cache("index", Derived)
    assert db._derived_cache("index", Derived) is first
    other = db._derived_cache("memo", Derived)
    assert other is not first and Derived.made == 2
    first._dead = True
    second = db._derived_cache("index", Derived)
    assert second is not first and db._derived_cache("index", Derived) is second
    assert first.discarded == 0  # a dead object is replaced, not discarded again
    assert db._derived_cache("memo", Derived) is other


def test_factory_runs_under_the_lock_and_only_there(tmp_path):
    db = LibraryDB(data_dir=tmp_path / "nowhere")
    seen = []

    def factory():
        seen.append(_lock_is_free_elsewhere(db))
        return Derived()

    db._derived_cache("index", factory)
    assert seen == [False]
    assert _lock_is_free_elsewhere(db)


def test_a_failing_factory_stores_nothing(tmp_path):
    db = LibraryDB(data_dir=tmp_path / "nowhere")

    def factory():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        db._derived_cache("index", factory)
    assert db._derived == {}
    assert isinstance(db._derived_cache("index", Derived), Derived)


def test_concurrent_first_use_makes_one_object(tmp_path):
    db = LibraryDB(data_dir=tmp_path / "nowhere")
    barrier = threading.Barrier(16)
    got, errors = [], []

    def factory():
        time.sleep(0.01)  # widen the window a racing lookup would hit
        return Derived()

    def worker():
        try:
            barrier.wait(timeout=30)
            got.append(db._derived_cache("index", factory))
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors and len(got) == 16
    assert Derived.made == 1 and all(obj is got[0] for obj in got)


class Guarded(Derived):
    """``dead`` read under the object's own lock, as a cache that must be
    safe on free-threaded builds may well do."""

    @property
    def dead(self) -> bool:
        with self._state:
            return self._dead


def test_dead_is_read_outside_the_lock(tmp_path):
    db = LibraryDB(data_dir=tmp_path / "nowhere")
    seen = []

    class Watched(Derived):
        @property
        def dead(self):
            seen.append(_lock_is_free_elsewhere(db))
            return self._dead

    obj = db._derived_cache("index", Watched)
    assert db._derived_cache("index", Watched) is obj
    assert seen == [True]


def test_dead_may_wait_for_an_object_that_is_querying(lib_db):
    """One thread holds the object's lock and queries, which takes the
    library's lock; another looks the object up, holding the library's
    lock first, and reads ``dead``, which waits for the object's lock.
    Neither waits for the other: ``dead`` is read after the library's
    lock is released."""
    lib_db.fixture.add_book("Synthetic Book")
    # Not lib_db: a deadlocked library would hang the fixture's close().
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, query_timeout=20)
    obj = db._derived_cache("index", Guarded)
    inside = threading.Event()
    counts, found, errors = [], [], []

    def query():
        try:
            with obj._state:
                inside.set()
                time.sleep(0.2)  # the lookup reaches dead meanwhile
                counts.append(db.execute(COUNT_BOOKS)[0][0])
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    def lookup():
        try:
            found.append(db._derived_cache("index", Guarded))
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    querier = threading.Thread(target=query, daemon=True)
    querier.start()
    assert inside.wait(timeout=10)
    looker = threading.Thread(target=lookup, daemon=True)
    with db._lock:  # the lookup takes the library's lock before the query does
        looker.start()
        time.sleep(0.05)
    querier.join(timeout=10)
    looker.join(timeout=10)
    assert not querier.is_alive() and not looker.is_alive(), "deadlock"
    assert not errors and counts == [1] and found == [obj]
    db.close()


def test_a_dead_object_is_replaced_once(tmp_path):
    """Threads that all find the same dead object make one replacement."""
    db = LibraryDB(data_dir=tmp_path / "nowhere")
    old = db._derived_cache("index", Guarded)
    old._dead = True
    barrier = threading.Barrier(16)
    got, errors, made = [], [], []

    def factory():
        time.sleep(0.01)  # widen the window a racing replacement would hit
        made.append(Guarded())
        return made[-1]

    def worker():
        try:
            barrier.wait(timeout=30)
            got.append(db._derived_cache("index", factory))
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors and len(got) == 16
    assert made == [got[0]] and got[0] is not old and all(obj is got[0] for obj in got)
    assert db._derived == {"index": got[0]}


def test_close_discards_outside_the_lock(lib_db):
    seen = []

    class Watched(Derived):
        def discard(self):
            seen.append(_lock_is_free_elsewhere(lib_db))
            super().discard()

    class Failing(Derived):
        def discard(self):
            raise RuntimeError("boom")

    objs = [lib_db._derived_cache(key, cls) for key, cls in (("a", Watched), ("b", Failing), ("c", Derived))]
    assert lib_db.execute(COUNT_BOOKS) == [(0,)]
    lib_db.close()  # a failing discard() doesn't stop it
    assert seen == [True] and objs[0].discarded == 1 and objs[2].discarded == 1
    assert lib_db._derived == {} and lib_db._idle == []
    fresh = lib_db._derived_cache("a", Derived)
    assert fresh is not objs[0]
    lib_db.close()
    assert fresh.discarded == 1 and objs[0].discarded == 1  # each discarded once


def test_close_never_blocks_on_a_busy_object(lib_db):
    """``close()`` returns while another thread is using a derived
    object; and even a ``discard()`` that blocks (against the contract)
    holds up only that ``close()`` call, not the library."""
    lib_db.fixture.add_book("Synthetic Book")
    busy = lib_db._derived_cache("index", Derived)
    in_use, done = threading.Event(), threading.Event()

    def use():
        with busy._state:  # a long query on the object
            in_use.set()
            done.wait(timeout=30)

    user = threading.Thread(target=use)
    user.start()
    assert in_use.wait(timeout=10)
    try:
        start = time.monotonic()
        lib_db.close()
        assert time.monotonic() - start < 1
        assert busy.dead and busy.discarded == 1
    finally:
        done.set()
        user.join(timeout=10)

    release = threading.Event()

    class Stuck(Derived):
        def discard(self):
            release.wait(timeout=30)
            super().discard()

    stuck = lib_db._derived_cache("index", Stuck)
    closer = threading.Thread(target=lib_db.close)
    closer.start()
    try:
        deadline = time.monotonic() + 10
        while lib_db._derived and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lib_db._derived == {}  # popped; close() now waits in discard()
        assert lib_db.execute(COUNT_BOOKS) == [(1,)]
        assert lib_db._derived_cache("index", Derived).count_books(lib_db) == 1
        assert closer.is_alive()
    finally:
        release.set()
        closer.join(timeout=10)
    assert stuck.discarded == 1


def test_a_factory_whose_object_queries_later_does_not_deadlock(lib_db):
    """One connection: lookups don't need it, and the objects' queries
    run outside the library's lock, also from inside connection()."""
    lib_db.fixture.add_book("Synthetic Book")
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, max_connections=1, query_timeout=20)
    errors = []

    def worker(n):
        try:
            for i in range(20):
                obj = db._derived_cache(f"index-{i % 3}", Derived)
                assert obj.count_books(db) == 1
                if n == 0 and i % 5 == 0:
                    with db.connection():
                        db._derived_cache("index-0", Derived)
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    try:
        assert not any(t.is_alive() for t in threads), "deadlock"
        assert not errors
        assert Derived.made == 3
    finally:
        db.close()


def test_fork_bookkeeping(lib_db):
    """What a child does on first use, simulated by changing the
    recorded pid: the parent's objects are set aside, never discarded."""
    parents = lib_db._derived_cache("index", Derived)
    lib_db._pid = -1  # a child
    mine = lib_db._derived_cache("index", Derived)
    assert mine is not parents and lib_db._derived_inherited == [parents]
    lib_db._pid = -2  # a grandchild keeps both referenced
    lib_db.close()
    assert lib_db._derived_inherited == [parents, mine]
    assert parents.discarded == 0 and mine.discarded == 0 and not parents.dead


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_fork_child(lib_db):
    lib_db.fixture.add_book("Synthetic Book")
    parents = lib_db._derived_cache("index", Derived)
    assert parents.count_books(lib_db) == 1
    read_end, write_end = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # fork() with other threads alive
        pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        status = 1
        try:
            os.close(read_end)
            mine = lib_db._derived_cache("index", Derived)
            result = (mine is not parents, mine.count_books(lib_db),
                      [obj is parents for obj in lib_db._derived_inherited])
            lib_db.close()
            result += (parents.discarded, mine.discarded)
            os.write(write_end, repr(result).encode())
            status = 0
        finally:
            os._exit(status)
    os.close(write_end)
    with os.fdopen(read_end, "rb") as pipe:
        output = pipe.read()
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert output == repr((True, 1, [True], 0, 1)).encode()
    assert lib_db._derived_cache("index", Derived) is parents and parents.discarded == 0


class _PausingDict(dict):
    """A dict whose ``values()`` waits for ``proceed``, after setting
    ``entered``: the moment a child's first thread is setting the
    parent's objects aside."""

    def __init__(self, data, entered, proceed):
        super().__init__(data)
        self.entered, self.proceed = entered, proceed

    def values(self):
        self.entered.set()
        assert self.proceed.wait(timeout=10)
        return super().values()


def test_a_childs_threads_wait_while_the_parents_objects_are_set_aside(tmp_path):
    """Two threads of a child use the library first at the same time
    (simulated by changing the recorded pid): the second waits until the
    first has set the parent's objects aside, so it never gets one."""
    db = LibraryDB(data_dir=tmp_path / "nowhere")
    parents = db._derived_cache("index", Derived)
    entered, proceed = threading.Event(), threading.Event()
    db._derived = _PausingDict(db._derived, entered, proceed)
    db._pid = -1  # a child
    got = []

    def use():
        got.append(db._derived_cache("index", Derived))

    first, second = threading.Thread(target=use), threading.Thread(target=use)
    first.start()
    try:
        assert entered.wait(timeout=10)  # first is moving the parent's objects
        second.start()
        second.join(timeout=0.2)
        assert second.is_alive() and got == []
    finally:
        proceed.set()
        first.join(timeout=10)
        if second.ident is not None:
            second.join(timeout=10)
    assert len(got) == 2 and got[0] is got[1] and got[0] is not parents
    assert db._derived_inherited == [parents] and db._pid == os.getpid()


@pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="needs os.fork")
def test_a_child_gets_a_new_fork_lock(lib_db):
    """A fork copies the lock a child's first use takes in whatever state
    it is in (held, here); the child replaces it."""
    lib_db.fixture.add_book("Synthetic Book")
    parents = lib_db._derived_cache("index", Derived)
    with client._fork_lock:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # fork() with other threads alive
            pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            status = 1
            try:
                if client._fork_lock.acquire(timeout=5):
                    client._fork_lock.release()
                    mine = lib_db._derived_cache("index", Derived)
                    if mine is not parents and mine.count_books(lib_db) == 1:
                        status = 0
                else:
                    status = 2
            finally:
                os._exit(status)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
