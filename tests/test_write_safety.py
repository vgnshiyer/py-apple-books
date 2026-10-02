"""Tests for the write-path guard rails in :mod:`py_apple_books.write_safety`.

Backups run against scratch databases in ``tmp_path``; the Books-running
guard runs with ``sys.platform`` and ``subprocess.run`` mocked — never
against the user's actual library.
"""

import contextlib
import datetime
import fcntl
import os
import sqlite3
import stat
import subprocess
import sys
import time

import pytest

from py_apple_books import write_safety
from py_apple_books.collection_writer import create_collection
from py_apple_books.exceptions import BooksAppRunningError, LibraryBusyError, WriteError
from py_apple_books.testing import FixtureLibrary


@pytest.fixture
def scratch_db(tmp_path):
    """Minimal library-shaped DB; the guards under test never read it."""
    db = tmp_path / "BKLibrary-test.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ZBKCOLLECTION (Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR)")
    conn.execute("INSERT INTO ZBKCOLLECTION VALUES (1, 'Books')")
    conn.commit()
    conn.close()
    return db


def _make_part(backup_dir, db, age):
    """A stray ``.part`` for ``db`` whose mtime is ``age`` seconds ago."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    part = backup_dir / f"{db.stem}-20250101-000000-000000.sqlite.part"
    part.write_bytes(b"backup in progress")
    stamp = time.time() - age
    os.utime(part, (stamp, stamp))
    return part


def _fake_pgrep(returncode=None, raises=None, calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, b"", b"")
    return run


# ---------------------------------------------------------------------------
# backup_library: .part pruning
# ---------------------------------------------------------------------------


def test_backup_keeps_fresh_foreign_part(scratch_db, tmp_path):
    """A young .part may be another process's backup still in flight;
    deleting it would abort that process's write."""
    backup_dir = tmp_path / "backups"
    foreign = _make_part(backup_dir, scratch_db, age=5)
    dest = write_safety.backup_library(scratch_db, backup_dir)
    assert foreign.exists()
    assert dest.exists()


def test_backup_prunes_stale_part(scratch_db, tmp_path):
    backup_dir = tmp_path / "backups"
    stale = _make_part(
        backup_dir, scratch_db, age=write_safety.BACKUP_PART_STALE_AFTER + 60
    )
    dest = write_safety.backup_library(scratch_db, backup_dir)
    assert not stale.exists()
    assert dest.exists()


def test_backup_tolerates_part_vanishing(scratch_db, tmp_path):
    """A .part whose owner renames it away between our glob and stat
    must not fail our backup. A dangling symlink reproduces that:
    it globs but stat() raises FileNotFoundError."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    gone = backup_dir / f"{scratch_db.stem}-20250101-000000-000000.sqlite.part"
    gone.symlink_to(backup_dir / "renamed-away")
    dest = write_safety.backup_library(scratch_db, backup_dir)
    assert dest.exists()


# ---------------------------------------------------------------------------
# books_is_running: fails closed
# ---------------------------------------------------------------------------


def test_non_darwin_refuses(monkeypatch):
    """In Docker/Linux the host's Books.app is invisible, so any process
    check would pass. Refuse — and not with BooksAppRunningError, whose
    "quit Books" advice would be wrong."""
    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", _fake_pgrep(1, calls=calls))
    with pytest.raises(WriteError, match="only supported when running directly on macOS") as exc:
        write_safety.ensure_books_not_running()
    assert not isinstance(exc.value, BooksAppRunningError)
    assert calls == []


def test_missing_pgrep_refuses(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    missing = FileNotFoundError(2, "No such file or directory", "pgrep")
    monkeypatch.setattr(subprocess, "run", _fake_pgrep(raises=missing))
    with pytest.raises(WriteError, match="Could not verify Apple Books is closed") as exc:
        write_safety.ensure_books_not_running()
    assert not isinstance(exc.value, BooksAppRunningError)


def test_pgrep_timeout_refuses(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    hung = subprocess.TimeoutExpired(["pgrep", "-x", "Books"], 5)
    monkeypatch.setattr(subprocess, "run", _fake_pgrep(raises=hung, calls=calls))
    with pytest.raises(WriteError, match="Could not verify Apple Books is closed") as exc:
        write_safety.ensure_books_not_running()
    assert not isinstance(exc.value, BooksAppRunningError)
    assert calls[0][1]["timeout"] == 5


def test_pgrep_error_status_refuses(monkeypatch):
    """pgrep exits 2/3 on usage/internal errors — that's 'unknown', not
    'Books is closed'."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", _fake_pgrep(3))
    with pytest.raises(WriteError, match="Could not verify Apple Books is closed"):
        write_safety.books_is_running()


@pytest.mark.parametrize("returncode, running", [(0, True), (1, False)])
def test_pgrep_status(monkeypatch, returncode, running):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", _fake_pgrep(returncode))
    assert write_safety.books_is_running() is running


def test_books_running_raises_books_app_running_error(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", _fake_pgrep(0))
    with pytest.raises(BooksAppRunningError):
        write_safety.ensure_books_not_running()


@pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS pgrep")
def test_real_pgrep_answers():
    """The real pgrep only reports matched / not matched (read-only
    process-table check; never touches the library)."""
    assert write_safety.books_is_running() in (True, False)


def test_write_refused_off_macos_before_backup(scratch_db, tmp_path, monkeypatch):
    """The guard runs first: a refused write takes no backup and leaves
    the database untouched."""
    monkeypatch.setattr(sys, "platform", "linux")
    backup_dir = tmp_path / "backups"
    with pytest.raises(WriteError, match="only supported when running directly on macOS"):
        create_collection("X", db_path=scratch_db, backup=True, backup_dir=backup_dir)
    assert not backup_dir.exists()
    conn = sqlite3.connect(scratch_db)
    assert conn.execute("SELECT COUNT(*) FROM ZBKCOLLECTION").fetchone()[0] == 1
    conn.close()


# ---------------------------------------------------------------------------
# Backup files and folders (1.11): private modes, exclusive names
# ---------------------------------------------------------------------------


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_new_backup_is_0600_and_new_folders_0700(scratch_db, tmp_path, umask_022):
    top = tmp_path / "a"
    backup_dir = top / "b" / "c"
    dest = write_safety.backup_library(scratch_db, backup_dir)
    assert _mode(dest) == 0o600
    for folder in (top, top / "b", backup_dir):
        assert _mode(folder) == 0o700, folder


def test_existing_folder_keeps_its_mode(scratch_db, tmp_path, umask_022):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o755)
    os.chmod(backup_dir, 0o755)
    dest = write_safety.backup_library(scratch_db, backup_dir)
    assert _mode(backup_dir) == 0o755
    assert _mode(dest) == 0o600


def test_default_backup_folders_are_private(scratch_db, tmp_path, monkeypatch, umask_022):
    """``~/.py_apple_books/backups/libraries/<key>``: every folder the
    first backup creates is 0700."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(write_safety, "BACKUP_DIR", home / ".py_apple_books" / "backups")
    dest = write_safety.backup_library(scratch_db)
    assert dest.parent == write_safety._own_backup_dir(scratch_db)
    folder = dest.parent
    while folder != home:
        assert _mode(folder) == 0o700, folder
        folder = folder.parent
    assert _mode(dest) == 0o600


def test_pre_restore_snapshot_is_0600(scratch_db, tmp_path, monkeypatch, umask_022):
    monkeypatch.setattr(write_safety, "books_is_running", lambda: False)
    backup = write_safety.backup_library(scratch_db, tmp_path / "b")
    snap = write_safety.restore_library(
        backup, scratch_db, force=True, backup_dir=tmp_path / "snaps" / "here"
    )
    assert snap.name.endswith(f"{write_safety.SNAPSHOT_SUFFIX}.sqlite")
    assert _mode(snap) == 0o600
    assert _mode(tmp_path / "snaps") == 0o700


def test_backup_dir_in_the_way_raises_as_before(scratch_db, tmp_path):
    """A file where the folder should be fails as Path.mkdir did."""
    blocker = tmp_path / "backups"
    blocker.write_bytes(b"not a folder")
    with pytest.raises(FileExistsError):
        write_safety.backup_library(scratch_db, blocker)
    with pytest.raises(OSError):
        write_safety.backup_library(scratch_db, blocker / "inside")


class _FixedClock(datetime.datetime):
    """``datetime`` whose ``now()`` never moves."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5, 600000)


def _named(db, when, suffix=""):
    return f"{db.stem}-{when.strftime(write_safety._STAMP_FORMAT)}{suffix}.sqlite"


def test_taken_names_move_on_to_the_next_timestamp(scratch_db, tmp_path, monkeypatch):
    """The ``.part`` is created exclusively: a name whose ``.part`` or
    finished backup exists, or with a symlink in the way, is never
    reused or written through; the next microsecond's is."""
    monkeypatch.setattr(write_safety, "datetime", _FixedClock)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    t0 = _FixedClock.now()
    us = datetime.timedelta(microseconds=1)
    foreign_part = backup_dir / (_named(scratch_db, t0) + ".part")
    foreign_part.write_bytes(b"someone else's backup in flight")
    finished = backup_dir / _named(scratch_db, t0 + us)
    finished.write_bytes(b"someone else's finished backup")
    target = tmp_path / "outside"
    (backup_dir / (_named(scratch_db, t0 + 2 * us) + ".part")).symlink_to(target)

    dest = write_safety._take_backup(scratch_db, backup_dir)
    assert dest.name == _named(scratch_db, t0 + 3 * us)
    assert foreign_part.read_bytes() == b"someone else's backup in flight"
    assert finished.read_bytes() == b"someone else's finished backup"
    assert not target.exists()
    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT ZTITLE FROM ZBKCOLLECTION").fetchall() == [("Books",)]
    conn.close()


def test_no_free_name_gives_up_after_100_tries(scratch_db, tmp_path, monkeypatch):
    monkeypatch.setattr(write_safety, "datetime", _FixedClock)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    t0 = _FixedClock.now()
    for i in range(100):
        when = t0 + datetime.timedelta(microseconds=i)
        (backup_dir / (_named(scratch_db, when) + ".part")).write_bytes(b"x")
    before = sorted(os.listdir(backup_dir))
    with pytest.raises(WriteError, match="no free backup file name"):
        write_safety._take_backup(scratch_db, backup_dir)
    assert sorted(os.listdir(backup_dir)) == before
    # The 101st name is free.
    (backup_dir / (_named(scratch_db, t0) + ".part")).unlink()
    assert write_safety._take_backup(scratch_db, backup_dir).name == _named(scratch_db, t0)


def test_part_created_exclusively_without_following_links(scratch_db, tmp_path, monkeypatch):
    opened = []
    real_open = os.open

    def spy(path, flags, *args, **kwargs):
        if str(path).endswith(".part"):
            opened.append((flags, args))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(write_safety.os, "open", spy)
    write_safety.backup_library(scratch_db, tmp_path / "backups")
    [(flags, args)] = opened
    for flag in (os.O_CREAT, os.O_EXCL, os.O_WRONLY, os.O_NOFOLLOW):
        assert flags & flag
    assert args == (0o600,)


# ---------------------------------------------------------------------------
# No artifacts
# ---------------------------------------------------------------------------


def test_unreadable_source_leaves_nothing(tmp_path):
    """The source is opened first: a store that can't be read leaves no
    ``.part`` and creates no folder."""
    missing = tmp_path / "BKLibrary-gone.sqlite"
    backup_dir = tmp_path / "fresh" / "backups"
    with pytest.raises(WriteError, match="Backup failed"):
        write_safety._take_backup(missing, backup_dir)
    assert not (tmp_path / "fresh").exists()


def test_failed_copy_leaves_no_part(scratch_db, tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    real_connect = sqlite3.connect

    class Failing:
        def __init__(self, conn):
            self._conn = conn

        def backup(self, target, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            self._conn.close()

    def connect(database, *args, **kwargs):
        conn = real_connect(database, *args, **kwargs)
        return Failing(conn) if kwargs.get("uri") else conn

    monkeypatch.setattr(write_safety.sqlite3, "connect", connect)
    with pytest.raises(WriteError, match="disk I/O error"):
        write_safety.backup_library(scratch_db, backup_dir)
    assert os.listdir(backup_dir) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_part_that_cannot_be_created_fails_cleanly(scratch_db, tmp_path):
    """A folder the backup can't be written into fails with WriteError
    (as 1.10 did), leaving nothing behind."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    os.chmod(backup_dir, 0o500)
    try:
        with pytest.raises(WriteError, match="Backup failed, aborting write"):
            write_safety.backup_library(scratch_db, backup_dir)
    finally:
        os.chmod(backup_dir, 0o700)
    assert os.listdir(backup_dir) == []


def test_reuse_check_survives_a_vanished_backup(scratch_db, tmp_path, monkeypatch):
    """The newest backup pruned between listing and the age check (by a
    1.10 writer, which doesn't take the lock) means a fresh backup, not
    a crash."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    ghost = backup_dir / f"{scratch_db.stem}-20990101-000000-000000.sqlite"
    real = write_safety._backups_for
    calls = []

    def listing(db_path, folder):
        calls.append(1)
        found = real(db_path, folder)
        return found + [ghost] if len(calls) == 1 else found

    monkeypatch.setattr(write_safety, "_backups_for", listing)
    dest = write_safety.backup_library(scratch_db, backup_dir, min_interval=300)
    assert dest != ghost
    assert dest.exists()
    assert not write_safety._younger_than(ghost, 300)


def test_backups_leave_only_backups(scratch_db, tmp_path):
    backup_dir = tmp_path / "backups"
    for _ in range(3):
        write_safety.backup_library(scratch_db, backup_dir)
    names = sorted(os.listdir(backup_dir))
    assert len(names) == 3
    assert all(n.startswith(scratch_db.stem + "-") and n.endswith(".sqlite") for n in names)


# ---------------------------------------------------------------------------
# restore_library under the folder lock
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_spy(monkeypatch):
    """Record the order of restore_library's steps."""
    events = []
    monkeypatch.setattr(
        write_safety, "ensure_books_not_running", lambda: events.append(("books_check",))
    )
    lock = write_safety._backup_folder_lock

    @contextlib.contextmanager
    def spy_lock(*dirs, **kwargs):
        with lock(*dirs, **kwargs):
            events.append(("lock", tuple(sorted({os.path.realpath(d) for d in dirs}))))
            try:
                yield
            finally:
                events.append(("unlock",))

    monkeypatch.setattr(write_safety, "_backup_folder_lock", spy_lock)
    for name in ("verify_backup", "_take_backup", "_prune_backups"):
        real = getattr(write_safety, name)

        def wrapped(*args, _real=real, _name=name, **kwargs):
            events.append((_name,))
            return _real(*args, **kwargs)

        monkeypatch.setattr(write_safety, name, wrapped)
    return events


def test_restore_order_lock_after_books_check_and_before_verify(tmp_path, restore_spy):
    """The lock is taken after the Books check and before the backup
    check, on the snapshot folder and the backup's own folder (each by
    real path, in order), and held until the pruning is done."""
    lib = FixtureLibrary.create(tmp_path / "home")
    elsewhere = tmp_path / "kept-backups"
    backup = write_safety._take_backup(lib.library_path, elsewhere)
    snaps = tmp_path / "snaps"
    restore_spy.clear()
    snap = write_safety.restore_library(backup, lib.library_path, backup_dir=snaps)
    names = [e[0] for e in restore_spy]
    assert names == [
        "books_check", "lock", "verify_backup", "_take_backup", "_prune_backups", "unlock",
    ]
    [lock_event] = [e for e in restore_spy if e[0] == "lock"]
    assert lock_event[1] == tuple(sorted({os.path.realpath(snaps), os.path.realpath(elsewhere)}))
    assert snap.parent == snaps


def test_restore_force_order(scratch_db, tmp_path, restore_spy):
    backup_dir = tmp_path / "backups"
    backup = write_safety._take_backup(scratch_db, backup_dir)
    restore_spy.clear()
    write_safety.restore_library(backup, scratch_db, force=True, backup_dir=backup_dir)
    assert [e[0] for e in restore_spy] == [
        "books_check", "lock", "_take_backup", "_prune_backups", "unlock",
    ]
    [lock_event] = [e for e in restore_spy if e[0] == "lock"]
    assert lock_event[1] == (os.path.realpath(backup_dir),)  # one folder, once


def test_restore_refused_by_books_takes_no_lock_and_creates_nothing(
    scratch_db, tmp_path, monkeypatch
):
    monkeypatch.setattr(write_safety, "books_is_running", lambda: True)
    locked = []
    monkeypatch.setattr(
        write_safety, "_backup_folder_lock",
        lambda *dirs, **kw: locked.append(dirs) or contextlib.nullcontext(),
    )
    backup = write_safety._take_backup(scratch_db, tmp_path / "b")
    with pytest.raises(BooksAppRunningError):
        write_safety.restore_library(backup, scratch_db, backup_dir=tmp_path / "snaps")
    assert locked == []
    assert not (tmp_path / "snaps").exists()


def test_restore_without_snapshot_creates_no_folder(scratch_db, tmp_path, monkeypatch):
    monkeypatch.setattr(write_safety, "books_is_running", lambda: False)
    backup = write_safety._take_backup(scratch_db, tmp_path / "b")
    missing = tmp_path / "never-made"
    assert write_safety.restore_library(
        backup, scratch_db, force=True, snapshot=False, backup_dir=missing
    ) is None
    assert not missing.exists()


def test_restore_rechecks_the_backup_under_the_lock(scratch_db, tmp_path, monkeypatch):
    """A backup pruned (by a writer that doesn't take the lock) between
    the first look and the lock is reported as missing, before any
    snapshot is taken."""
    monkeypatch.setattr(write_safety, "books_is_running", lambda: False)
    backup = write_safety._take_backup(scratch_db, tmp_path / "b")
    lock = write_safety._backup_folder_lock

    @contextlib.contextmanager
    def pruning_lock(*dirs, **kwargs):
        backup.unlink()
        with lock(*dirs, **kwargs):
            yield

    monkeypatch.setattr(write_safety, "_backup_folder_lock", pruning_lock)
    snaps = tmp_path / "snaps"
    with pytest.raises(WriteError, match="Backup file not found"):
        write_safety.restore_library(backup, scratch_db, force=True, backup_dir=snaps)
    assert os.listdir(snaps) == []


def test_restore_busy_changes_nothing(scratch_db, tmp_path, monkeypatch):
    monkeypatch.setattr(write_safety, "books_is_running", lambda: False)
    monkeypatch.setattr(write_safety, "BACKUP_LOCK_TIMEOUT", 0.1)
    backup_dir = tmp_path / "b"
    backup = write_safety._take_backup(scratch_db, backup_dir)
    before = sorted(os.listdir(backup_dir))
    fd = os.open(backup_dir, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(LibraryBusyError):
            write_safety.restore_library(backup, scratch_db, force=True, backup_dir=backup_dir)
    finally:
        os.close(fd)
    assert sorted(os.listdir(backup_dir)) == before
