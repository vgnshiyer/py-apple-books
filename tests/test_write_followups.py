"""Write-path follow-ups to the 1.10 facade.

- Backups per store: ``write_safety`` keeps the current user's library's
  backups in ``BACKUP_DIR`` and any other store's in a folder of its own,
  for its own defaults (``list_backups``, ``restore_library``,
  ``backup_library``, direct ``collection_writer`` calls) as for the
  facade, and tells a store's series apart by the whole backup name.

Synthetic libraries only; Books.app is never checked for real.
"""

import os
import shutil
import sqlite3

import pytest

from py_apple_books import PyAppleBooks, api, collection_writer, write_safety
from py_apple_books.db import client
from py_apple_books.db.metadata import read_only_uri
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


def library(path, title, **kwargs) -> FixtureLibrary:
    lib = FixtureLibrary.create(path, **kwargs)
    lib.add_collection(title)
    return lib


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
