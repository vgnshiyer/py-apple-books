"""Fair hand-off of the pool's connection slots (F-G8).

Threads waiting for a connection are served in the order they came: a
thread that returns one can't take it back ahead of them, however
tightly it loops. A waiter that gives up (its deadline passes, or it is
interrupted) leaves the queue without taking a slot with it.
"""

import os
import random
import signal
import sqlite3
import threading
import time
import warnings

import pytest

from py_apple_books.db import LibraryDB, query_deadline
from py_apple_books.db.client import IDENTITY_RECHECK, _Slots
from py_apple_books.exceptions import QueryTimeoutError

COUNT_BOOKS = "SELECT count(*) FROM ZBKLIBRARYASSET"
TITLES = "SELECT ZTITLE FROM ZBKLIBRARYASSET ORDER BY Z_PK"
WAIT_MESSAGE = "Timed out waiting for a database connection"


def _wait_for_waiters(slots, count, timeout=10.0):
    """Block until ``count`` threads are queued on ``slots``."""
    end = time.monotonic() + timeout
    while len(slots._waiters) < count:
        assert time.monotonic() < end, f"{len(slots._waiters)} of {count} threads queued"
        time.sleep(0.001)


def _assert_all_free(slots, size):
    assert (slots._free, len(slots._waiters)) == (size, 0)


def _start(target, *args):
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread


def _join(*threads):
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()


# -- the slots ------------------------------------------------------------------


def test_waiters_are_served_in_arrival_order():
    slots = _Slots(1)
    assert slots.acquire()
    order = []

    def waiter(n):
        assert slots.acquire(timeout=10)
        order.append(n)
        slots.release()

    threads = []
    for n in range(6):
        threads.append(_start(waiter, n))
        _wait_for_waiters(slots, n + 1)
    slots.release()
    _join(*threads)
    assert order == list(range(6))
    _assert_all_free(slots, 1)


def test_a_released_slot_goes_to_the_waiter_not_back_to_the_releaser():
    slots = _Slots(1)
    assert slots.acquire()
    holding, done = threading.Event(), threading.Event()

    def waiter():
        assert slots.acquire(timeout=10)
        holding.set()
        done.wait(10)
        slots.release()

    thread = _start(waiter)
    _wait_for_waiters(slots, 1)
    slots.release()
    assert not slots.acquire(timeout=0)  # handed over, even before the waiter runs
    assert holding.wait(10)
    done.set()
    _join(thread)
    _assert_all_free(slots, 1)


def test_a_timed_out_waiter_leaves_the_queue():
    slots = _Slots(2)
    assert slots.acquire() and slots.acquire()
    start = time.monotonic()
    assert not slots.acquire(timeout=0.05)
    assert time.monotonic() - start >= 0.04
    assert not slots.acquire(timeout=0)
    assert len(slots._waiters) == 0
    slots.release()
    slots.release()
    _assert_all_free(slots, 2)
    with pytest.raises(ValueError):
        slots.release()


def test_a_slot_handed_over_as_the_wait_ends_is_passed_on():
    """The waiter's wait times out, and before it can leave the queue a
    releasing thread hands it the slot: it passes the slot to the next
    waiter instead of keeping or losing it."""
    slots = _Slots(1)
    assert slots.acquire()
    results = {}

    def first():
        results["first"] = slots.acquire(timeout=0.01)

    def second():
        results["second"] = slots.acquire(timeout=10)

    threads = [_start(first)]
    _wait_for_waiters(slots, 1)
    threads.append(_start(second))
    _wait_for_waiters(slots, 2)
    with slots._lock:  # what release() does, once the first wait has timed out
        time.sleep(0.5)
        slots._hand_off()
    _join(*threads)
    assert results == {"first": False, "second": True}
    slots.release()
    _assert_all_free(slots, 1)


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs signal.setitimer")
def test_an_interrupted_waiter_leaves_the_queue():
    """As when Ctrl-C stops a thread waiting for a connection."""
    slots = _Slots(1)
    assert slots.acquire()

    class Interrupted(Exception):
        pass

    def interrupt(signum, frame):
        raise Interrupted

    previous = signal.signal(signal.SIGALRM, interrupt)
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.05)
        with pytest.raises(Interrupted):
            slots.acquire()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert len(slots._waiters) == 0
    slots.release()
    _assert_all_free(slots, 1)


# -- the pool -------------------------------------------------------------------


def test_a_returned_connection_goes_to_the_waiting_thread(lib_db):
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, max_connections=1)
    order = []

    def waiter():
        with db.connection(time.monotonic() + 10):
            order.append("waiter")

    with db.connection():
        thread = _start(waiter)
        _wait_for_waiters(db._slots, 1)
    with db.connection(time.monotonic() + 10):  # at once, but after the waiter
        order.append("releaser")
    _join(thread)
    assert order == ["waiter", "releaser"]
    db.close()


def test_a_thread_in_a_tight_loop_does_not_starve_a_waiter(lib_db):
    """Each of this thread's statements waits for at most the statement
    the looping thread is running and the one it has just started; a
    semaphore that lets a releasing thread take its slot straight back
    lets the looping thread run many more."""
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, max_connections=1, query_timeout=10)
    stop, done = threading.Event(), [0]

    def loop():
        while not stop.is_set():
            db.execute("SELECT 1")
            done[0] += 1

    # This thread's first statement does one-time per-thread setup before
    # it joins the queue; measure the steady state.
    assert db.execute("SELECT 1") == [(1,)]
    thread = _start(loop)
    try:
        while done[0] < 100:
            time.sleep(0.001)
        overtaken = []
        for _ in range(50):
            before = done[0]
            assert db.execute("SELECT 1") == [(1,)]
            overtaken.append(done[0] - before)
    finally:
        stop.set()
        _join(thread)
    assert max(overtaken) <= 2, overtaken
    _assert_all_free(db._slots, 1)
    db.close()


def test_waiters_that_time_out_do_not_leak_slots(lib_db):
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, max_connections=2, query_timeout=0.05)
    errors = []

    def waiter():
        try:
            db.execute("SELECT 1")
        except QueryTimeoutError as e:
            errors.append(str(e))

    with db.connection(), db.connection():
        threads = [_start(waiter) for _ in range(6)]
        _join(*threads)
    assert len(errors) == 6 and all(WAIT_MESSAGE in e for e in errors)
    _assert_all_free(db._slots, 2)
    deadline = time.monotonic() + 1
    with db.connection(deadline) as one, db.connection(deadline) as two:
        assert one.execute("SELECT 1").fetchone() == two.execute("SELECT 1").fetchone() == (1,)
    db.close()


def test_expiring_waiters_racing_hand_offs_keep_the_pool_whole(lib_db):
    """Many threads on a small pool, many of their deadlines too short:
    every statement returns its rows or times out, and afterwards every
    slot is free again."""
    lib_db.fixture.add_book("Synthetic Book")
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, max_connections=2, max_idle=1)
    counts = {"ok": 0, "timeout": 0}
    lock, errors = threading.Lock(), []

    def worker(seed):
        rnd = random.Random(seed)
        for _ in range(60):
            try:
                with query_deadline(rnd.choice([0.0002, 0.001, 0.003, 5])):
                    assert db.execute(COUNT_BOOKS) == [(1,)]
                outcome = "ok"
            except QueryTimeoutError:
                outcome = "timeout"
            except BaseException as e:  # reported below
                errors.append(e)
                return
            with lock:
                counts[outcome] += 1

    threads = [_start(worker, seed) for seed in range(16)]
    _join(*threads)
    assert errors == []
    assert counts["ok"] and counts["timeout"] and sum(counts.values()) == 16 * 60
    _assert_all_free(db._slots, 2)
    assert len(db._idle) <= 1
    db.close()


def test_close_while_threads_wait(lib_db):
    """Connections returned after close() are closed; the waiters get new ones."""
    lib_db.fixture.add_book("Synthetic Book")
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, max_connections=1)
    results = []

    def waiter():
        results.append(db.execute(COUNT_BOOKS))

    with db.connection() as conn:
        threads = [_start(waiter) for _ in range(3)]
        _wait_for_waiters(db._slots, 3)
        db.close()
    _join(*threads)
    assert results == [[(1,)]] * 3
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    _assert_all_free(db._slots, 1)
    db.close()


def test_store_replaced_while_threads_wait(lib_db, make_library):
    now = [1000.0]
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, max_connections=1)
    db._clock = lambda: now[0]
    lib_db.fixture.add_book("Old")
    assert db.execute(TITLES) == [("Old",)]
    results = []

    def waiter():
        results.append(db.execute(TITLES))

    with db.connection():
        thread = _start(waiter)
        _wait_for_waiters(db._slots, 1)
        new = make_library()
        new.add_book("New")
        os.replace(new.library_path, lib_db.fixture.library_path)
        now[0] += 2 * IDENTITY_RECHECK
    _join(thread)
    assert results == [[("New",)]]
    db.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_fork_while_threads_wait(lib_db):
    """The child has its own slots: not held by the parent's checkouts
    or owed to the parent's waiters."""
    db = LibraryDB(data_dir=lib_db.fixture.data_dir, max_connections=1)
    lib_db.fixture.add_book("Synthetic Book")
    results = []

    def waiter():
        results.append(db.execute(COUNT_BOOKS))

    with db.connection():
        thread = _start(waiter)
        _wait_for_waiters(db._slots, 1)
        read_end, write_end = os.pipe()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # fork() with other threads alive
            pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            status = 1
            try:
                os.close(read_end)
                result = [db.execute(COUNT_BOOKS) for _ in range(3)]
                os.write(write_end, repr((result, db._slots._free)).encode())
                status = 0
            finally:
                os._exit(status)
        os.close(write_end)
        with os.fdopen(read_end, "rb") as pipe:
            output = pipe.read()
        _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert output == repr(([[(1,)]] * 3, 1)).encode()
    _join(thread)
    assert results == [[(1,)]]
    _assert_all_free(db._slots, 1)
    db.close()
