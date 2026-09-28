"""Tests for the write-path guard rails in :mod:`py_apple_books.write_safety`.

Backups run against scratch databases in ``tmp_path``; the Books-running
guard runs with ``sys.platform`` and ``subprocess.run`` mocked — never
against the user's actual library.
"""

import os
import sqlite3
import subprocess
import sys
import time

import pytest

from py_apple_books import write_safety
from py_apple_books.collection_writer import create_collection
from py_apple_books.exceptions import BooksAppRunningError, WriteError


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
