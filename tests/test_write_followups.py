"""Write-path follow-ups to the 1.10 facade.

- Backups per store: ``write_safety`` keeps the current user's library's
  backups in ``BACKUP_DIR`` and any other store's in a folder of its own,
  for its own defaults (``list_backups``, ``restore_library``,
  ``backup_library``, direct ``collection_writer`` calls) as for the
  facade, and tells a store's series apart by the whole backup name.
- Restoring over a damaged library: without ``db_path`` the target is
  the canonical store file even if it can't be read, for ``force=True``
  or ``snapshot=False`` (and ``list_backups()``); several candidate
  stores still refuse.
- ``WriteSession`` opens the store ``mode=rw`` (never creates it), maps
  lock and read-only failures at ``BEGIN``, in the transaction and at
  ``COMMIT`` to typed errors, and checks for Books again once it holds
  the write lock.

Synthetic libraries only; Books.app is never checked for real.
"""

import hashlib
import os
import shutil
import sqlite3

import pytest

from py_apple_books import PyAppleBooks, api, collection_writer, write_safety
from py_apple_books.collection_writer import WriteSession
from py_apple_books.db import client
from py_apple_books.db.metadata import read_only_uri
from py_apple_books.exceptions import (
    AmbiguousStoreError,
    BackupValidationError,
    BooksAppRunningError,
    LibraryBusyError,
    SchemaValidationError,
    WriteError,
)
from py_apple_books.testing import FixtureLibrary

LOCATION_VARS = ("APPLE_BOOKS_DATA_DIR", "APPLE_BOOKS_LIBRARY_DB", "APPLE_BOOKS_ANNOTATION_DB")
SAME_UUID = {"BKLibrary": "11111111-2222-3333-4444-555555555555"}


@pytest.fixture(autouse=True)
def _guards(monkeypatch, tmp_path):
    """Books is closed (as far as the guard can tell); backups land in
    tmp_path."""
    monkeypatch.setattr(write_safety, "books_is_running", lambda: False)
    monkeypatch.setattr(write_safety, "BACKUP_DIR", tmp_path / "backups")


@pytest.fixture
def clean_env(monkeypatch, fresh_default_library):
    for var in LOCATION_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def redirect(monkeypatch, var, value):
    """Point location variable ``var`` at ``value`` for a new default library."""
    monkeypatch.setenv(var, str(value))
    client._reset_default_library()


def titles(path) -> list:
    conn = sqlite3.connect(read_only_uri(path), uri=True)
    try:
        return [t for (t,) in conn.execute(
            "SELECT ZTITLE FROM ZBKCOLLECTION WHERE ZDELETEDFLAG = 0 ORDER BY Z_PK")]
    finally:
        conn.close()


def digest(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def library(path, title, **kwargs) -> FixtureLibrary:
    lib = FixtureLibrary.create(path, **kwargs)
    lib.add_collection(title)
    return lib


def unlocked(path) -> bool:
    """Whether another connection can take the write lock at once."""
    conn = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


# -- backups per store ----------------------------------------------------------


@pytest.fixture
def home_and_copy(tmp_path, clean_env):
    """``(home, copy)``: HOME's library and a copy of it elsewhere with
    the same store UUID (as a Finder or snapshot copy has)."""
    home = library(tmp_path / "lib-home", "home shelf", store_uuids=SAME_UUID)
    copy = library(tmp_path / "lib-copy", "copy shelf", store_uuids=SAME_UUID)
    clean_env.setenv("HOME", str(home.root))
    return home, copy


def test_the_facade_uses_the_write_safety_rule():
    assert api._backup_dir_for is write_safety._backup_dir_for
    assert api._writes_home_store is write_safety._writes_home_store
    assert api._own_backup_dir is write_safety._own_backup_dir


@pytest.mark.parametrize("var", ["APPLE_BOOKS_DATA_DIR", "APPLE_BOOKS_LIBRARY_DB"])
def test_redirected_store_never_sees_or_restores_home_backups(home_and_copy, clean_env, tmp_path, var):
    """With a location variable naming another store, list_backups() and
    restore_library() without backup_dir work on that store's own backups,
    and direct writer calls back it up there. The APPLE_BOOKS_LIBRARY_DB
    store is named BKLibrary-1.sqlite, a prefix of the HOME store's name."""
    home, copy = home_and_copy
    PyAppleBooks().create_collection("home write")
    [home_backup] = write_safety.list_backups()
    assert home_backup.parent == write_safety.BACKUP_DIR

    if var == "APPLE_BOOKS_DATA_DIR":
        store = copy.library_path
        redirect(clean_env, var, copy.data_dir)
    else:
        store = tmp_path / "elsewhere" / "BKLibrary-1.sqlite"
        store.parent.mkdir()
        shutil.copyfile(copy.library_path, store)
        redirect(clean_env, var, store)
    assert write_safety.list_backups() == []
    assert write_safety.list_backups(store) == []

    collection_writer.create_collection("copy write")
    [copy_backup] = write_safety.list_backups()
    assert copy_backup.parent == write_safety._own_backup_dir(store)
    assert write_safety.list_backups(store) == [copy_backup]
    assert titles(copy_backup) == ["copy shelf"]

    snap = write_safety.restore_library(write_safety.list_backups()[0])
    assert snap.parent == copy_backup.parent
    assert titles(store) == ["copy shelf"]
    assert write_safety.list_backups() == [snap, copy_backup]
    # The HOME library and its backups are as they were.
    assert titles(home.library_path) == ["home shelf", "home write"]
    assert sorted(p for p in write_safety.BACKUP_DIR.iterdir() if p.is_file()) == [home_backup]
    assert titles(home_backup) == ["home shelf"]


def test_direct_writer_calls_back_up_by_store(home_and_copy):
    """Two writes within BACKUP_MIN_INTERVAL, given db_path and no
    backup_dir, to two stores of the same name: each gets a backup of its
    own, where list_backups(db_path) finds it."""
    home, copy = home_and_copy
    collection_writer.create_collection("copy write", db_path=copy.library_path)
    collection_writer.create_collection("home write", db_path=home.library_path)
    [copy_backup] = write_safety.list_backups(copy.library_path)
    [home_backup] = write_safety.list_backups(home.library_path)
    assert copy_backup.parent == write_safety._own_backup_dir(copy.library_path)
    assert home_backup.parent == write_safety.BACKUP_DIR
    assert titles(copy_backup) == ["copy shelf"] and titles(home_backup) == ["home shelf"]
    assert write_safety.list_backups() == [home_backup]


@pytest.fixture
def prefix_stores(tmp_path):
    """``(long, short)``: the canonical store and a copy named
    ``BKLibrary-1.sqlite``, whose stem is a prefix of the canonical one's."""
    long = library(tmp_path / "lib", "long shelf").library_path
    short = tmp_path / "short" / "BKLibrary-1.sqlite"
    short.parent.mkdir()
    shutil.copyfile(long, short)
    conn = sqlite3.connect(short)
    try:
        conn.execute("UPDATE ZBKCOLLECTION SET ZTITLE = 'short shelf'")
        conn.commit()
    finally:
        conn.close()
    return long, short


def test_prefix_stem_stores_reuse_their_own_backups(prefix_stores, tmp_path):
    long, short = prefix_stores
    shared = tmp_path / "shared"
    interval = write_safety.BACKUP_MIN_INTERVAL
    long_backup = write_safety.backup_library(long, shared, min_interval=interval)
    short_backup = write_safety.backup_library(short, shared, min_interval=interval)
    assert short_backup != long_backup and titles(short_backup) == ["short shelf"]
    assert write_safety.backup_library(short, shared, min_interval=interval) == short_backup
    assert write_safety.backup_library(long, shared, min_interval=interval) == long_backup
    assert write_safety.list_backups(short, shared) == [short_backup]
    assert write_safety.list_backups(long, shared) == [long_backup]
    # A pre-restore snapshot belongs to its store's series only.
    snap = write_safety.restore_library(short_backup, short, backup_dir=shared)
    assert write_safety.list_backups(short, shared) == [snap, short_backup]
    assert write_safety.list_backups(long, shared) == [long_backup]


def test_prefix_stem_stores_prune_their_own_backups(prefix_stores, tmp_path):
    long, short = prefix_stores
    shared = tmp_path / "shared"
    long_backup = write_safety.backup_library(long, shared)
    stale = shared / f"{long.stem}-20200101-000000-000000.sqlite.part"
    stale.write_bytes(b"")
    old = os.path.getmtime(stale) - write_safety.BACKUP_PART_STALE_AFTER - 60
    os.utime(stale, (old, old))
    for _ in range(write_safety.BACKUP_KEEP + 1):
        write_safety.backup_library(short, shared)
    assert len(write_safety.list_backups(short, shared)) == write_safety.BACKUP_KEEP
    assert write_safety.list_backups(long, shared) == [long_backup] and long_backup.exists()
    assert stale.exists()  # the long store's to prune
    write_safety.backup_library(long, shared)
    assert not stale.exists()


# -- restoring over a damaged library -----------------------------------------------


def damage_metadata(path):
    """Leave the store's Z_METADATA unreadable: the store fails
    validation, and verify_backup can't read its identity."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE Z_METADATA SET Z_PLIST = X'00', Z_UUID = NULL")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def damaged_home(tmp_path, clean_env):
    """``(lib, backup)``: HOME's library, backed up with one collection,
    then given a second and a damaged Z_METADATA."""
    lib = library(tmp_path / "home", "before")
    clean_env.setenv("HOME", str(lib.root))
    backup = write_safety.backup_library(lib.library_path)
    assert backup.parent == write_safety.BACKUP_DIR
    lib.add_collection("after")
    damage_metadata(lib.library_path)
    return lib, backup


def test_list_backups_of_a_damaged_library(damaged_home):
    _, backup = damaged_home
    assert write_safety.list_backups() == [backup]


def test_restore_over_a_damaged_library_needs_force(damaged_home):
    lib, backup = damaged_home
    before = digest(lib.library_path)
    with pytest.raises(AmbiguousStoreError, match="can't be read") as exc:
        write_safety.restore_library(backup)
    assert "force=True" in str(exc.value)
    assert digest(lib.library_path) == before
    assert write_safety.list_backups() == [backup]


def test_restore_over_a_damaged_library_with_force(damaged_home):
    lib, backup = damaged_home
    snap = write_safety.restore_library(backup, force=True)
    assert titles(lib.library_path) == ["before"]
    assert write_safety.read_store_metadata(lib.library_path).uuid
    assert snap.parent == write_safety.BACKUP_DIR
    assert write_safety.list_backups() == [snap, backup]
    assert titles(snap) == ["before", "after"]


def test_restore_over_a_damaged_library_without_snapshot(damaged_home):
    lib, backup = damaged_home
    # The target is found; the backup can't be checked against it.
    with pytest.raises(BackupValidationError) as exc:
        write_safety.restore_library(backup, snapshot=False)
    assert exc.value.reason == BackupValidationError.LIVE_UNREADABLE
    assert "force=True" in str(exc.value) and "db_path=" in str(exc.value)
    assert lib.library_path.name in str(exc.value)
    assert write_safety.restore_library(backup, force=True, snapshot=False) is None
    assert titles(lib.library_path) == ["before"]
    assert write_safety.list_backups() == [backup]


def test_restore_over_a_damaged_library_next_to_a_copy(damaged_home):
    """A damaged canonical store next to a copy: the restore goes to the
    canonical file, never the copy."""
    lib, backup = damaged_home
    copy = lib.library_path.with_name("BKLibrary-1-091020131601 copy.sqlite")
    shutil.copyfile(backup, copy)
    before = digest(copy)
    assert write_safety.list_backups() == [backup]
    with pytest.raises(AmbiguousStoreError, match="force=True"):
        write_safety.restore_library(backup)
    write_safety.restore_library(backup, force=True)
    assert titles(lib.library_path) == ["before"] and digest(copy) == before


@pytest.mark.parametrize("canonical", ["missing", "damaged"])
def test_several_candidate_stores_still_refuse(tmp_path, clean_env, canonical):
    lib = library(tmp_path / "home", "shelf")
    clean_env.setenv("HOME", str(lib.root))
    backup = write_safety.backup_library(lib.library_path)
    folder = lib.library_path.parent
    for name in ("BKLibrary-2-1.sqlite", "BKLibrary-3-1.sqlite"):
        shutil.copyfile(lib.library_path, folder / name)
    if canonical == "missing":
        lib.library_path.unlink()
    else:
        damage_metadata(lib.library_path)
    before = {p.name: digest(p) for p in folder.iterdir()}
    for call in (write_safety.list_backups,
                 lambda: write_safety.restore_library(backup),
                 lambda: write_safety.restore_library(backup, force=True),
                 lambda: write_safety.restore_library(backup, force=True, snapshot=False)):
        with pytest.raises(AmbiguousStoreError, match="Several"):
            call()
    assert {p.name: digest(p) for p in folder.iterdir()} == before


def test_live_unreadable_message_names_db_path(tmp_path):
    lib = library(tmp_path / "lib", "shelf")
    backup = write_safety.backup_library(lib.library_path, tmp_path / "b")
    damage_metadata(lib.library_path)
    with pytest.raises(BackupValidationError, match="db_path=") as exc:
        write_safety.verify_backup(backup, lib.library_path)
    assert exc.value.reason == BackupValidationError.LIVE_UNREADABLE
    assert "force=True" in str(exc.value)


# -- WriteSession ----------------------------------------------------------------


@pytest.fixture
def lib(tmp_path):
    return library(tmp_path / "lib", "shelf")


def counts(path):
    """The collection titles and the primary-key counters. Read through a
    connection that can write, so a WAL store keeps no -wal or -shm file
    after it closes."""
    conn = sqlite3.connect(path)
    try:
        return (conn.execute("SELECT ZTITLE FROM ZBKCOLLECTION ORDER BY Z_PK").fetchall(),
                conn.execute("SELECT Z_MAX FROM Z_PRIMARYKEY ORDER BY Z_ENT").fetchall())
    finally:
        conn.close()


@pytest.mark.parametrize("via", ["writer", "default"])
def test_store_gone_at_connect_is_not_created(lib, tmp_path, clean_env, monkeypatch, via):
    """The store is moved away between its backup and the connect: the
    write fails instead of creating an empty file under its name."""
    path = lib.library_path
    moved = path.with_name("moved-away.sqlite")
    real = collection_writer.backup_library

    def backup_then_move(*args, **kwargs):
        made = real(*args, **kwargs)
        path.rename(moved)
        return made

    monkeypatch.setattr(collection_writer, "backup_library", backup_then_move)
    clean_env.setenv("HOME", str(lib.root))
    with pytest.raises(WriteError, match="isn't there any more") as exc:
        if via == "writer":
            collection_writer.create_collection("Nope", db_path=path, backup_dir=tmp_path / "b")
        else:
            PyAppleBooks().create_collection("Nope")
    assert not isinstance(exc.value, SchemaValidationError)
    assert not os.path.lexists(path)
    assert titles(moved) == ["shelf"]


def test_missing_db_path_without_backup_is_not_created(tmp_path):
    missing = tmp_path / "BKLibrary-1-091020131601.sqlite"
    with pytest.raises(WriteError, match="isn't there any more"):
        collection_writer.create_collection("Nope", db_path=missing, backup=False)
    assert not os.path.lexists(missing)


def test_commit_blocked_by_a_reader_is_busy(tmp_path, monkeypatch):
    """A rollback-journal store with another connection's read transaction
    open: the commit can't take the exclusive lock."""
    lib = library(tmp_path / "lib", "shelf", journal_mode="DELETE")
    path = lib.library_path
    monkeypatch.setattr(collection_writer, "BUSY_TIMEOUT", 0.05)
    before = counts(path)
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM ZBKCOLLECTION").fetchone()
    session = WriteSession(path, backup=False)
    body_ran = False
    try:
        with pytest.raises(LibraryBusyError, match="nothing was changed") as exc:
            with session:
                collection_writer._allocate_pk(session.conn.cursor(), "BKCollection", "ZBKCOLLECTION")
                body_ran = True
        assert body_ran and session.conn is None
        assert isinstance(exc.value, sqlite3.OperationalError)
        assert isinstance(exc.value.__cause__, sqlite3.OperationalError)
        with pytest.raises(LibraryBusyError):
            collection_writer.create_collection("Busy", db_path=path, backup=False)
    finally:
        reader.execute("ROLLBACK")
        reader.close()
    assert counts(path) == before and unlocked(path)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permissions")
@pytest.mark.parametrize("journal, read_only", [("DELETE", "file"), ("WAL", "file"), ("WAL", "folder")])
def test_read_only_store_is_a_write_error(tmp_path, journal, read_only):
    """A store file SQLite can only open read-only refuses at the first
    statement that writes; a WAL store in a read-only folder at BEGIN."""
    lib = library(tmp_path / "lib", "shelf", journal_mode=journal)
    path = lib.library_path
    before = counts(path)
    target = path if read_only == "file" else path.parent
    mode = os.stat(target).st_mode
    os.chmod(target, 0o444 if read_only == "file" else 0o555)
    try:
        with pytest.raises(WriteError, match="readonly") as exc:
            collection_writer.create_collection("Nope", db_path=path, backup=False)
        assert not isinstance(exc.value, sqlite3.Error)
    finally:
        os.chmod(target, mode)
        # SQLite gives the -wal and -shm files it creates the store's mode.
        for sidecar in ("-wal", "-shm"):
            if os.path.exists(f"{path}{sidecar}"):
                os.chmod(f"{path}{sidecar}", 0o644)
    assert counts(path) == before and unlocked(path)


def test_books_opened_during_the_backup_rolls_back(lib, tmp_path, monkeypatch):
    path = lib.library_path
    answers = iter([False, True])
    monkeypatch.setattr(write_safety, "books_is_running", lambda: next(answers))
    before = counts(path)
    with pytest.raises(BooksAppRunningError):
        collection_writer.create_collection("Nope", db_path=path, backup_dir=tmp_path / "b")
    assert counts(path) == before and unlocked(path)
    assert len(write_safety.list_backups(path, tmp_path / "b")) == 1
    assert next(answers, None) is None  # checked twice


def test_books_is_not_checked_with_the_guard_off(lib, monkeypatch):
    calls = []
    monkeypatch.setattr(write_safety, "books_is_running", lambda: calls.append(1) or False)
    collection_writer.create_collection("Written", db_path=lib.library_path, backup=False,
                                        require_books_closed=False)
    assert calls == [] and titles(lib.library_path) == ["shelf", "Written"]
