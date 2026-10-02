"""Concurrent backups and restores share one backup folder lock (1.11).

``backup_library`` runs its reuse check, backup and pruning, and
``restore_library`` its check, snapshot, restore and pruning, under a
lock on the backup folder: a thread lock per folder plus an exclusive
``flock`` on the folder itself, so threads and processes alike see one
backup per burst and never prune each other's restore points.

Everything runs on synthetic databases under ``tmp_path``; the process
tests run ``tests/_backup_worker.py``.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from py_apple_books import write_safety
from py_apple_books.collection_writer import create_collection
from py_apple_books.db import query_deadline
from py_apple_books.exceptions import LibraryBusyError, WriteError
from py_apple_books.testing import FixtureLibrary

WORKER = pathlib.Path(__file__).with_name("_backup_worker.py")
BUSY = (
    "Another write is backing up or restoring this library; nothing was "
    "changed. Try again in a moment."
)
SEEDED = 10
SLOW = 0.25


@pytest.fixture
def db(tmp_path):
    """A small SQLite store named like a Books library."""
    path = tmp_path / "BKLibrary-1-091020131601.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ZBKCOLLECTION (Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR)")
    conn.execute("INSERT INTO ZBKCOLLECTION VALUES (1, 'Books')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def backups(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    return folder


def _seed(db, folder, count=SEEDED):
    """``count`` old backups of ``db`` in ``folder`` (a day apart, in 2020)."""
    seeded = []
    for i in range(count):
        path = folder / f"{db.stem}-202001{i + 1:02d}-000000-000000.sqlite"
        path.write_bytes(b"old backup")
        os.utime(path, (1e9 + i, 1e9 + i))
        seeded.append(path)
    return seeded


@pytest.fixture
def slow_take(monkeypatch):
    """Slow every backup down, so concurrent callers overlap."""
    take = write_safety._take_backup

    def slow(*args, **kwargs):
        time.sleep(SLOW)
        return take(*args, **kwargs)

    monkeypatch.setattr(write_safety, "_take_backup", slow)


def _burst(fn, n=8):
    """Run ``fn()`` in ``n`` threads released together; their results
    (or exceptions), in no particular order."""
    barrier = threading.Barrier(n)
    out = []
    lock = threading.Lock()

    def run():
        barrier.wait()
        try:
            result = fn()
        except BaseException as e:  # noqa: BLE001 - reported to the test
            result = e
        with lock:
            out.append(result)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads)
    return out


def _backup_burst(db, folder, n=8):
    return _burst(lambda: write_safety.backup_library(db, folder, min_interval=300), n)


def _hold_flock(folder):
    """Hold ``folder``'s flock on a descriptor of our own, as another
    process would (a new open file description)."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _flock_free(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


@pytest.fixture(autouse=True)
def _no_held_locks():
    """Every test leaves no folder locked."""
    yield
    assert not write_safety._held_fds
    assert all(state.owner is None for state in write_safety._registry.values())


# -- bursts ---------------------------------------------------------------------


def test_thread_burst_takes_one_backup_and_prunes_one(db, backups, slow_take):
    seeded = _seed(db, backups)
    results = _backup_burst(db, backups)
    assert not [r for r in results if isinstance(r, BaseException)]
    assert len(set(results)) == 1
    [taken] = set(results)
    series = write_safety._backups_for(db, backups)
    assert len(series) == write_safety.BACKUP_KEEP
    assert series[-1] == taken
    assert series[:-1] == seeded[1:]  # 9 of the 10 old backups kept
    assert sorted(os.listdir(backups)) == sorted(p.name for p in series)


def test_burst_without_the_lock_races(db, backups, slow_take, monkeypatch):
    """Mutation check: with the lock replaced by a no-op, the same burst
    takes several backups and prunes old restore points it shouldn't,
    so the test above really depends on the lock."""
    monkeypatch.setattr(
        write_safety, "_backup_folder_lock", lambda *dirs, **kw: contextlib.nullcontext()
    )
    seeded = _seed(db, backups)
    results = _backup_burst(db, backups)
    assert not [r for r in results if isinstance(r, BaseException)]
    kept = [p for p in seeded if p.exists()]
    assert len(set(results)) > 1
    assert len(kept) < SEEDED - 1


def test_write_session_burst_shares_one_backup(tmp_path, slow_take):
    lib = FixtureLibrary.create(tmp_path / "home")
    lib.seed_system_collections()
    folder = tmp_path / "backups"
    names = [f"Shelf {i}" for i in range(8)]
    pending = list(names)
    lock = threading.Lock()

    def write():
        with lock:
            name = pending.pop()
        return create_collection(
            name, db_path=lib.library_path, backup=True, backup_dir=folder,
            require_books_closed=False,
        )

    results = _burst(write)
    assert not [r for r in results if isinstance(r, BaseException)]
    assert len(write_safety._backups_for(lib.library_path, folder)) == 1
    conn = sqlite3.connect(lib.library_path)
    try:
        titles = {row[0] for row in conn.execute("SELECT ZTITLE FROM ZBKCOLLECTION")}
    finally:
        conn.close()
    assert set(names) <= titles


def _start_workers(args, n):
    procs = [
        subprocess.Popen(
            [sys.executable, str(WORKER), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=os.environ.copy(),
        )
        for _ in range(n)
    ]
    return procs


def test_process_burst_takes_one_backup(db, backups):
    seeded = _seed(db, backups)
    procs = _start_workers(["burst", str(db), str(backups), str(SLOW)], 4)
    try:
        for p in procs:
            assert p.stdout.readline().strip() == "ready", p.stderr.read()
        for p in procs:
            p.stdin.write("go\n")
            p.stdin.flush()
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            assert p.returncode == 0, err
            outs.append(out.strip())
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
    assert len(set(outs)) == 1, outs
    [name] = set(outs)
    series = write_safety._backups_for(db, backups)
    assert [p.name for p in series] == [p.name for p in seeded[1:]] + [name]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_fork_child_with_held_lock(tmp_path):
    """A child forked while the lock is held drops its copy of the
    folder descriptor (so the lock ends when the parent releases it,
    even with the child still alive), starts with a fresh registry, and
    releases nothing of its own when it leaves the inherited hold."""
    held, other = tmp_path / "held", tmp_path / "other"
    held.mkdir()
    other.mkdir()
    proc = subprocess.run(
        [sys.executable, str(WORKER), "fork", str(held), str(other)],
        capture_output=True, text=True, timeout=60, env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["child"] == {
        "inherited_fd_open": False,
        "registry_fresh": True,
        "lock_taken": False,
        "other_fd": report["child"]["other_fd"],
        "other_fd_is_inherited_number": True,
        "other_fd_open_after_leaving": True,
        "other_fd_held_after_leaving": True,
        "other_locked_after_leaving": True,
        "other_free_after_release": True,
    }
    assert report["parent"] == {
        "child_alive": True,
        "free_after_release": True,
        "parent_fds": [],
        "relocked": True,
    }


# -- waiting: timeout and query_deadline ----------------------------------------


def test_timeout_raises_library_busy_and_changes_nothing(db, backups, monkeypatch):
    monkeypatch.setattr(write_safety, "BACKUP_LOCK_TIMEOUT", 0.3)
    before = sorted(os.listdir(backups))
    fd = _hold_flock(backups)
    try:
        start = time.monotonic()
        with pytest.raises(LibraryBusyError) as exc:
            write_safety.backup_library(db, backups)
        waited = time.monotonic() - start
    finally:
        os.close(fd)
    assert str(exc.value) == BUSY
    assert isinstance(exc.value, WriteError)
    assert isinstance(exc.value, sqlite3.OperationalError)
    assert exc.value.sqlite_errorcode is None
    assert 0.3 <= waited < 3
    assert sorted(os.listdir(backups)) == before


def test_timeout_is_read_at_call_time(backups, monkeypatch):
    fd = _hold_flock(backups)
    try:
        monkeypatch.setattr(write_safety, "BACKUP_LOCK_TIMEOUT", 0.05)
        start = time.monotonic()
        with pytest.raises(LibraryBusyError):
            with write_safety._backup_folder_lock(backups):
                pass
        assert time.monotonic() - start < 2
    finally:
        os.close(fd)


def test_query_deadline_caps_the_wait(db, backups):
    assert write_safety.BACKUP_LOCK_TIMEOUT == 15.0
    fd = _hold_flock(backups)
    try:
        start = time.monotonic()
        with query_deadline(0.3), pytest.raises(LibraryBusyError):
            write_safety.backup_library(db, backups)
        waited = time.monotonic() - start
    finally:
        os.close(fd)
    assert 0.3 <= waited < 3


def test_explicit_timeout_beats_the_default(backups):
    fd = _hold_flock(backups)
    try:
        start = time.monotonic()
        with pytest.raises(LibraryBusyError):
            with write_safety._backup_folder_lock(backups, timeout=0.1):
                pass
        assert time.monotonic() - start < 2
    finally:
        os.close(fd)


def test_wait_for_another_thread_times_out(backups, monkeypatch):
    monkeypatch.setattr(write_safety, "BACKUP_LOCK_TIMEOUT", 0.2)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with write_safety._backup_folder_lock(backups):
            holding.set()
            release.wait(30)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holding.wait(30)
        with pytest.raises(LibraryBusyError, match="Another write is backing up"):
            with write_safety._backup_folder_lock(backups):
                pass
    finally:
        release.set()
        t.join(30)


def test_waiter_gets_the_lock_once_released(db, backups):
    fd = _hold_flock(backups)
    timer = threading.Timer(0.3, os.close, (fd,))
    timer.start()
    try:
        start = time.monotonic()
        dest = write_safety.backup_library(db, backups)
        assert time.monotonic() - start >= 0.25
    finally:
        timer.join()
    assert dest.exists()


def test_no_timeout_waits_until_released(db, backups, monkeypatch):
    """``BACKUP_LOCK_TIMEOUT = None``: no limit of its own, so the write
    waits for the holder however long it takes."""
    monkeypatch.setattr(write_safety, "BACKUP_LOCK_TIMEOUT", None)
    assert write_safety._lock_deadline(None) == float("inf")
    fd = _hold_flock(backups)
    timer = threading.Timer(0.3, os.close, (fd,))
    timer.start()
    try:
        start = time.monotonic()
        dest = write_safety.backup_library(db, backups)
        assert time.monotonic() - start >= 0.25
    finally:
        timer.join()
    assert dest.exists()


def test_no_timeout_still_capped_by_query_deadline(backups, monkeypatch):
    monkeypatch.setattr(write_safety, "BACKUP_LOCK_TIMEOUT", None)
    fd = _hold_flock(backups)
    try:
        start = time.monotonic()
        with query_deadline(0.2), pytest.raises(LibraryBusyError):
            with write_safety._backup_folder_lock(backups):
                pass
        assert time.monotonic() - start < 3
    finally:
        os.close(fd)


@pytest.mark.parametrize("how", ["deadline", "timeout", "zero"])
def test_expired_deadline_still_takes_an_uncontended_lock(db, backups, monkeypatch, how):
    """With no time left, each lock is still tried once."""
    if how == "deadline":
        with query_deadline(0):
            time.sleep(0.01)
            dest = write_safety.backup_library(db, backups)
    elif how == "timeout":
        monkeypatch.setattr(write_safety, "BACKUP_LOCK_TIMEOUT", -1)
        dest = write_safety.backup_library(db, backups)
    else:
        monkeypatch.setattr(write_safety, "BACKUP_LOCK_TIMEOUT", 0)
        dest = write_safety.backup_library(db, backups)
    assert dest.exists()


def test_expired_deadline_contended_fails_at_once(backups):
    fd = _hold_flock(backups)
    try:
        start = time.monotonic()
        with query_deadline(0), pytest.raises(LibraryBusyError):
            with write_safety._backup_folder_lock(backups):
                pass
        assert time.monotonic() - start < 1
    finally:
        os.close(fd)


# -- the lock itself -------------------------------------------------------------


def test_lock_creates_no_file_and_is_released(backups):
    before = sorted(os.listdir(backups))
    with write_safety._backup_folder_lock(backups):
        assert sorted(os.listdir(backups)) == before
        assert not _flock_free(backups)
        assert len(write_safety._held_fds) == 1
    assert sorted(os.listdir(backups)) == before
    assert _flock_free(backups)
    assert not write_safety._held_fds


def test_lock_released_on_error(backups):
    with pytest.raises(RuntimeError):
        with write_safety._backup_folder_lock(backups):
            raise RuntimeError("boom")
    assert _flock_free(backups)
    with write_safety._backup_folder_lock(backups, timeout=0):
        pass


def test_nested_hold_in_one_thread_shares_the_outer_one(tmp_path, backups):
    alias = tmp_path / "alias"
    alias.symlink_to(backups)
    with write_safety._backup_folder_lock(backups):
        with write_safety._backup_folder_lock(alias, timeout=0):
            assert len(write_safety._held_fds) == 1
        # The inner hold released nothing.
        assert not _flock_free(backups)
    assert _flock_free(backups)


def test_folders_locked_once_each_in_real_path_order(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (tmp_path / "z-link").symlink_to(a)
    order = []
    hold = write_safety._hold_folder

    @contextlib.contextmanager
    def spy(path, deadline):
        order.append(path)
        with hold(path, deadline):
            yield

    monkeypatch.setattr(write_safety, "_hold_folder", spy)
    with write_safety._backup_folder_lock(b, tmp_path / "z-link", a):
        pass
    assert order == sorted({os.path.realpath(a), os.path.realpath(b)})


def test_opposite_orders_do_not_deadlock(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()

    turns = iter([(a, b), (b, a)] * 4)
    lock = threading.Lock()

    def run():
        with lock:
            first, second = next(turns)
        for _ in range(50):
            with write_safety._backup_folder_lock(first, second):
                pass

    results = _burst(run, 8)
    assert not [r for r in results if isinstance(r, BaseException)]


def test_restore_waits_for_a_backup_in_flight(db, backups, monkeypatch, slow_take):
    """A restore into the same folder waits for a backup in flight (and
    the other way round): nothing interleaves."""
    monkeypatch.setattr(write_safety, "books_is_running", lambda: False)
    first = write_safety.backup_library(db, backups)
    in_backup = threading.Event()
    take = write_safety._take_backup  # the slowed one

    def flagged(*args, **kwargs):
        in_backup.set()
        return take(*args, **kwargs)

    monkeypatch.setattr(write_safety, "_take_backup", flagged)
    spans = {}

    def timed(name, fn):
        def run():
            start = time.monotonic()
            fn()
            spans[name] = (start, time.monotonic())
        return run

    backup = threading.Thread(target=timed("backup", lambda: write_safety.backup_library(db, backups)))
    restore = threading.Thread(target=timed(
        "restore", lambda: write_safety.restore_library(first, db, force=True, backup_dir=backups)
    ))
    backup.start()
    assert in_backup.wait(30)  # the backup holds the folder lock now
    restore.start()
    backup.join(30)
    restore.join(30)
    assert spans["restore"][1] - spans["backup"][1] >= SLOW * 0.9  # its own snapshot after


# -- fallbacks: a thread lock only -----------------------------------------------


def test_without_fcntl_threads_still_share_one_backup(db, backups, slow_take, monkeypatch, caplog):
    monkeypatch.setattr(write_safety, "fcntl", None)
    _seed(db, backups)
    with caplog.at_level(logging.DEBUG, logger=write_safety.__name__):
        results = _backup_burst(db, backups)
    assert len(set(results)) == 1
    assert "no fcntl" in caplog.text


class _NoFlock:
    """``fcntl`` stand-in whose flock fails with ``code``."""

    LOCK_EX = fcntl.LOCK_EX
    LOCK_NB = fcntl.LOCK_NB

    def __init__(self, code):
        self.code = code
        self.calls = 0

    def flock(self, fd, op):
        self.calls += 1
        raise OSError(self.code, os.strerror(self.code))


@pytest.mark.parametrize("code", sorted({errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOLCK}))
def test_unsupported_flock_falls_back_to_the_thread_lock(
    db, backups, slow_take, monkeypatch, caplog, code
):
    fake = _NoFlock(code)
    monkeypatch.setattr(write_safety, "fcntl", fake)
    _seed(db, backups)
    with caplog.at_level(logging.DEBUG, logger=write_safety.__name__):
        results = _backup_burst(db, backups)
    assert len(set(results)) == 1
    assert fake.calls == 8  # one attempt each, no polling
    assert errno.errorcode[code] in caplog.text
    assert str(backups) not in caplog.text


def test_folder_that_cannot_be_opened_gets_the_thread_lock(tmp_path, caplog):
    missing = tmp_path / "missing"
    with caplog.at_level(logging.DEBUG, logger=write_safety.__name__):
        with write_safety._backup_folder_lock(missing):
            assert not write_safety._held_fds
    assert "ENOENT" in caplog.text
    assert "missing" not in caplog.text
    assert not missing.exists()


def test_thread_lock_alone_still_times_out(backups, monkeypatch):
    monkeypatch.setattr(write_safety, "fcntl", None)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with write_safety._backup_folder_lock(backups):
            holding.set()
            release.wait(30)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holding.wait(30)
        with pytest.raises(LibraryBusyError):
            with write_safety._backup_folder_lock(backups, timeout=0.1):
                pass
    finally:
        release.set()
        t.join(30)
