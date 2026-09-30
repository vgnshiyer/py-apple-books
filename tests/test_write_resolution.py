"""Which library store the collection writes go to (G3.2, R17).

Writes go to the store the reads resolve, found strictly: the default
library (``PyAppleBooks()``, ``collection_writer._default_db_path``)
honours ``APPLE_BOOKS_LIBRARY_DB`` and then ``APPLE_BOOKS_DATA_DIR``; a
``PyAppleBooks`` given a store writes that store whatever the
environment says. Several candidate stores and no canonical one, a
canonical one that fails validation, a lone copy, or a store other than
the one being read raise ``AmbiguousStoreError`` instead of a guess.
An instance's writes back up into a folder of their own per store.

Synthetic libraries only; the Books.app check is switched off.
"""

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from py_apple_books import PyAppleBooks, collection_writer, write_safety
from py_apple_books.db import client, use_library
from py_apple_books.exceptions import AmbiguousStoreError, DBConnectionError, WriteError

LOCATION_VARS = ("APPLE_BOOKS_DATA_DIR", "APPLE_BOOKS_LIBRARY_DB", "APPLE_BOOKS_ANNOTATION_DB")


@pytest.fixture(autouse=True)
def _guards_off(monkeypatch, tmp_path):
    """No Books.app check; backups land in tmp_path."""
    monkeypatch.setattr(collection_writer, "ensure_books_not_running", lambda: None)
    monkeypatch.setattr(write_safety, "BACKUP_DIR", tmp_path / "backups")


@pytest.fixture
def clean_env(monkeypatch, fresh_default_library):
    for var in LOCATION_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def make(make_library, name: str):
    """A library whose one book and one collection are named ``name``."""
    lib = make_library()
    lib.book = lib.add_book(f"{name} book")
    lib.shelf = lib.add_collection(f"{name} shelf")
    return lib


def titles(lib) -> list:
    return [t for (t,) in lib.execute("library", "SELECT ZTITLE FROM ZBKCOLLECTION WHERE ZDELETEDFLAG = 0 "
                                                 "ORDER BY Z_PK")]


def digest(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def backup_titles(path) -> list:
    conn = sqlite3.connect(path)
    try:
        return [t for (t,) in conn.execute("SELECT ZTITLE FROM ZBKCOLLECTION ORDER BY Z_PK")]
    finally:
        conn.close()


def two_generations(lib):
    """Replace ``lib``'s canonical library store by two future
    generations of it, neither of which can be told to be the live one."""
    folder = lib.library_path.parent
    newer = folder / "BKLibrary-3-1.sqlite"
    lib.library_path.rename(newer)
    shutil.copyfile(newer, folder / "BKLibrary-2-1.sqlite")
    return folder


# -- the default library --------------------------------------------------------


def test_default_db_path_is_the_home_store(make_library, clean_env):
    home = make(make_library, "home")
    clean_env.setenv("HOME", str(home.root))
    assert collection_writer._default_db_path() == home.library_path


def test_default_db_path_honours_the_data_dir_variable(make_library, clean_env):
    home, data = make(make_library, "home"), make(make_library, "data")
    clean_env.setenv("HOME", str(home.root))
    clean_env.setenv("APPLE_BOOKS_DATA_DIR", str(data.data_dir))
    assert collection_writer._default_db_path() == data.library_path


def test_library_db_variable_beats_the_data_dir_variable(make_library, clean_env):
    data, file = make(make_library, "data"), make(make_library, "file")
    clean_env.setenv("APPLE_BOOKS_DATA_DIR", str(data.data_dir))
    clean_env.setenv("APPLE_BOOKS_LIBRARY_DB", str(file.library_path))
    assert collection_writer._default_db_path() == file.library_path
    new_id = collection_writer.create_collection("Written", backup=False)
    assert titles(file) == ["file shelf", "Written"] and titles(data) == ["data shelf"]
    # The default instance reads back what it wrote.
    assert PyAppleBooks().get_collection_by_id(new_id).title == "Written"


def test_default_instance_writes_the_home_store(make_library, clean_env):
    home, other = make(make_library, "home"), make(make_library, "other")
    clean_env.setenv("HOME", str(home.root))
    api = PyAppleBooks()
    assert api._write_path() is None
    created = api.create_collection("Written", backup=False)
    assert created.title == "Written" and titles(home) == ["home shelf", "Written"]
    assert titles(other) == ["other shelf"]


def test_default_instance_passes_no_db_path(monkeypatch):
    """MCP 0.8.2's calls reach the writer exactly as in 1.9.1."""
    calls = []
    for name in ("create_collection", "rename_collection", "delete_collection",
                 "add_book_to_collection", "remove_book_from_collection"):
        monkeypatch.setattr(collection_writer, name,
                            lambda *args, _name=name, **kwargs: calls.append((_name, args, kwargs)))
    monkeypatch.setattr(PyAppleBooks, "get_collection_by_id", lambda self, cid: cid)
    api = PyAppleBooks()
    api.create_collection("t", "d")
    api.rename_collection(1, "u", backup=False)
    api.delete_collection(1)
    api.add_book_to_collection(1, 2)
    api.remove_book_from_collection(1, 2, backup=False)
    assert [kwargs for _, _, kwargs in calls] == [
        {"backup": True}, {"backup": False}, {"backup": True}, {"backup": True}, {"backup": False}]
    assert [args for _, args, _ in calls] == [("t", "d"), (1, "u"), (1,), (1, 2), (1, 2)]


def test_two_future_generations_refuse_default_writes(make_library, clean_env):
    lib = make(make_library, "lib")
    folder = two_generations(lib)
    clean_env.setenv("APPLE_BOOKS_DATA_DIR", str(lib.data_dir))
    before = {p.name: digest(p) for p in folder.iterdir()}
    with pytest.raises(AmbiguousStoreError) as exc:
        PyAppleBooks().create_collection("Nope", backup=False)
    assert isinstance(exc.value, WriteError) and isinstance(exc.value, DBConnectionError)
    assert "BKLibrary-2-1.sqlite" in str(exc.value) and "BKLibrary-3-1.sqlite" in str(exc.value)
    with pytest.raises(AmbiguousStoreError):
        collection_writer.create_collection("Nope", backup=False)
    with pytest.raises(AmbiguousStoreError):
        write_safety.restore_library(folder / "BKLibrary-2-1.sqlite")
    assert {p.name: digest(p) for p in folder.iterdir()} == before
    # Reads still pick one (the most recently written), with a warning.
    assert [c.title for c in PyAppleBooks().list_collections()] == ["lib shelf"]


# -- a library of the instance's own ------------------------------------------------


@pytest.fixture
def decoyed(make_library, clean_env):
    """``(mine, decoy)``: the environment points every location variable
    at the decoy's stores."""
    mine, decoy = make(make_library, "mine"), make(make_library, "decoy")
    clean_env.setenv("HOME", str(decoy.root))
    clean_env.setenv("APPLE_BOOKS_DATA_DIR", str(decoy.data_dir))
    clean_env.setenv("APPLE_BOOKS_LIBRARY_DB", str(decoy.library_path))
    clean_env.setenv("APPLE_BOOKS_ANNOTATION_DB", str(decoy.annotation_path))
    return mine, decoy


def test_write_path_is_the_instance_store(decoyed):
    mine, decoy = decoyed
    api = PyAppleBooks(data_dir=mine.data_dir)
    files = PyAppleBooks(library_db=mine.library_path)
    try:
        assert api._write_path() == mine.library_path
        assert files._write_path() == mine.library_path
        assert PyAppleBooks()._write_path() is None
        assert PyAppleBooks(query_timeout=5)._write_path() == decoy.library_path
        assert api._write_kwargs(backup=True) == {
            "backup": True, "db_path": mine.library_path, "backup_dir": api.store_info().backup_dir}
    finally:
        api.close()
        files.close()


def test_data_dir_instance_writes_its_store(decoyed):
    mine, decoy = decoyed
    decoy_files = {p: digest(p) for p in (decoy.library_path, decoy.annotation_path)}
    api = PyAppleBooks(data_dir=mine.data_dir)
    try:
        created = api.create_collection("Created", backup=False)
        assert titles(mine) == ["mine shelf", "Created"]
        assert api.rename_collection(created.id, "Renamed", backup=False).title == "Renamed"
        assert api.add_book_to_collection(created.id, mine.book["id"], backup=False) is True
        assert [b.title for b in api.get_collection_by_id(created.id).books] == ["mine book"]
        assert api.remove_book_from_collection(created.id, mine.book["id"], backup=False) is True
        api.delete_collection(created.id, backup=False)
        assert titles(mine) == ["mine shelf"]
        assert mine.execute("library", "SELECT ZDELETEDFLAG FROM ZBKCOLLECTION WHERE Z_PK = ?",
                            (created.id,)) == [(1,)]
    finally:
        api.close()
    assert {p: digest(p) for p in decoy_files} == decoy_files


def test_backup_is_of_the_instance_store(decoyed, tmp_path):
    mine, decoy = decoyed
    api = PyAppleBooks(data_dir=mine.data_dir)
    try:
        api.create_collection("Backed Up")
        backup_dir = api.store_info().backup_dir
    finally:
        api.close()
    assert backup_dir.parent == tmp_path / "backups" / "libraries"
    # Both stores have the canonical name; the backup's rows tell them apart.
    [backup] = write_safety.list_backups(mine.library_path, backup_dir)
    assert backup_titles(backup) == ["mine shelf"]
    # None where the default library's backups go.
    assert write_safety.list_backups(mine.library_path, tmp_path / "backups") == []


@pytest.mark.parametrize("instance_first", [True, False])
def test_default_and_instance_backups_stay_apart(make_library, clean_env, instance_first):
    """Two writes within BACKUP_MIN_INTERVAL, one to the default library and
    one to an instance's store of the same name: each store's restore point
    is a backup of that store, not a reuse of the other's."""
    home, mine = make(make_library, "home"), make(make_library, "mine")
    clean_env.setenv("HOME", str(home.root))
    default, api = PyAppleBooks(), PyAppleBooks(data_dir=mine.data_dir)
    try:
        writes = [lambda: api.create_collection("mine write"),
                  lambda: default.create_collection("home write")]
        for write in writes if instance_first else writes[::-1]:
            write()
        backup_dir = api.store_info().backup_dir
    finally:
        api.close()
    [home_backup] = write_safety.list_backups()
    assert backup_titles(home_backup) == ["home shelf"]
    [mine_backup] = write_safety.list_backups(mine.library_path, backup_dir)
    assert backup_titles(mine_backup) == ["mine shelf"]
    assert titles(home) == ["home shelf", "home write"] and titles(mine) == ["mine shelf", "mine write"]


def test_instance_writes_leave_the_default_backups(make_library, clean_env, monkeypatch):
    home, mine = make(make_library, "home"), make(make_library, "mine")
    clean_env.setenv("HOME", str(home.root))
    PyAppleBooks().create_collection("home write")
    [home_backup] = write_safety.list_backups()
    monkeypatch.setattr(write_safety, "BACKUP_MIN_INTERVAL", 0.0)
    api = PyAppleBooks(data_dir=mine.data_dir)
    try:
        for i in range(write_safety.BACKUP_KEEP + 1):
            api.create_collection(f"mine {i}")
        mine_backups = write_safety.list_backups(mine.library_path, api.store_info().backup_dir)
    finally:
        api.close()
    assert len(mine_backups) == write_safety.BACKUP_KEEP
    assert write_safety.list_backups() == [home_backup] and backup_titles(home_backup) == ["home shelf"]


def test_ambiguous_instance_store_refuses_writes(make_library, clean_env):
    lib = make(make_library, "lib")
    folder = two_generations(lib)
    before = {p.name: digest(p) for p in folder.iterdir()}
    api = PyAppleBooks(data_dir=lib.data_dir)
    try:
        with pytest.raises(AmbiguousStoreError):
            api.create_collection("Nope", backup=False)
        with pytest.raises(AmbiguousStoreError):
            api.add_book_to_collection(lib.shelf["id"], lib.book["id"], backup=False)
    finally:
        api.close()
    assert {p.name: digest(p) for p in folder.iterdir()} == before


def test_default_instance_in_use_library_writes_that_library(make_library, clean_env):
    """Reads in a use_library() block go to its library; so do writes."""
    home, other = make(make_library, "home"), make(make_library, "other")
    clean_env.setenv("HOME", str(home.root))
    api = PyAppleBooks()
    mine = PyAppleBooks(data_dir=other.data_dir)
    try:
        with use_library(mine._PyAppleBooks__library):
            assert api._write_path() == other.library_path
            assert api.create_collection("Here", backup=False).title == "Here"
    finally:
        mine.close()
    assert titles(other) == ["other shelf", "Here"] and titles(home) == ["home shelf"]


# -- a store other than the live one ------------------------------------------------

COPY = "BKLibrary-1-091020131601 copy.sqlite"


@pytest.fixture
def copy_beside(make_library, clean_env):
    """A library with a stale Finder copy of its store next to it; the
    live store has a collection (pk 2) the copy lacks."""
    lib = make(make_library, "live")
    copy = lib.library_path.parent / COPY
    shutil.copyfile(lib.library_path, copy)
    conn = sqlite3.connect(copy)
    try:
        conn.execute("UPDATE ZBKCOLLECTION SET ZTITLE = 'stale shelf'")
        conn.commit()
    finally:
        conn.close()
    lib.second = lib.add_collection("live second")
    return lib, copy


def fail_validation(monkeypatch, path):
    """Make ``path`` fail store validation, as a store locked past the
    discovery busy timeout does."""
    real = client._is_store
    monkeypatch.setattr(client, "_is_store",
                        lambda p, entity: False if Path(p) == path else real(p, entity))


@pytest.mark.parametrize("default", [False, True])
def test_canonical_store_failing_validation_refuses_writes(copy_beside, clean_env, monkeypatch, default):
    """Reads settled on the canonical store; when it can't be validated at
    write time, strict discovery would pick the copy. The write refuses
    before opening anything, so no collection of the live store comes
    back as the one created."""
    lib, copy = copy_beside
    clean_env.setenv("HOME", str(lib.root))
    api = PyAppleBooks() if default else PyAppleBooks(data_dir=lib.data_dir)
    try:
        assert [c.title for c in api.list_collections()] == ["live shelf", "live second"]
        before = {p.name: digest(p) for p in lib.library_path.parent.iterdir()}
        fail_validation(monkeypatch, lib.library_path)
        with pytest.raises(AmbiguousStoreError, match="can't be read right now") as exc:
            api.create_collection("New")
        assert COPY in str(exc.value)
        with pytest.raises(AmbiguousStoreError):
            api.add_book_to_collection(lib.second["id"], lib.book["id"])
        if default:
            with pytest.raises(AmbiguousStoreError):
                collection_writer.create_collection("New", backup=False)
            with pytest.raises(AmbiguousStoreError):
                write_safety.restore_library(copy)
    finally:
        api.close()
    assert {p.name: digest(p) for p in lib.library_path.parent.iterdir()} == before
    assert not (write_safety.BACKUP_DIR).exists()


def test_reads_fallen_back_to_a_copy_refuse_writes(copy_beside, monkeypatch):
    """The canonical store failed validation when first read, so reads
    use the copy; writes still refuse it."""
    lib, copy = copy_beside
    fail_validation(monkeypatch, lib.library_path)
    api = PyAppleBooks(data_dir=lib.data_dir)
    try:
        assert [c.title for c in api.list_collections()] == ["stale shelf"]
        with pytest.raises(AmbiguousStoreError, match="can't be read right now"):
            api.create_collection("New", backup=False)
    finally:
        api.close()
    assert titles(lib) == ["live shelf", "live second"]


@pytest.mark.parametrize("name", [COPY, "BKLibrary-1-091020131601.old.sqlite",
                                  "BKLibrary-1-091020131601-20260101-120000-000000.sqlite"])
def test_lone_copy_refuses_writes(make_library, clean_env, name):
    """No canonical store, only a copy, a '.old' file or a backup: reads
    use it, writes refuse (Apple Books doesn't read it)."""
    lib = make(make_library, "lib")
    store = lib.library_path.rename(lib.library_path.parent / name)
    before = digest(store)
    api = PyAppleBooks(data_dir=lib.data_dir)
    try:
        assert [c.title for c in api.list_collections()] == ["lib shelf"]
        with pytest.raises(AmbiguousStoreError, match="isn't named like"):
            api.create_collection("Nope", backup=False)
    finally:
        api.close()
    clean_env.setenv("APPLE_BOOKS_DATA_DIR", str(lib.data_dir))
    with pytest.raises(AmbiguousStoreError, match="isn't named like"):
        collection_writer._default_db_path()
    assert digest(store) == before


def test_single_future_generation_takes_writes(make_library, clean_env):
    lib = make(make_library, "lib")
    newer = lib.library_path.rename(lib.library_path.parent / "BKLibrary-2-1.sqlite")
    api = PyAppleBooks(data_dir=lib.data_dir)
    try:
        assert api._write_path() == newer
        assert api.create_collection("Here", backup=False).title == "Here"
        assert [c.title for c in api.list_collections()] == ["lib shelf", "Here"]
    finally:
        api.close()


def test_store_other_than_the_one_read_refuses_writes(make_library, clean_env):
    """The location changed after the reads resolved it: the write would
    go to a store other than the one the reads (and the collection the
    write returns) come from."""
    one, two = make(make_library, "one"), make(make_library, "two")
    clean_env.setenv("APPLE_BOOKS_LIBRARY_DB", str(one.library_path))
    api = PyAppleBooks()
    assert [c.title for c in api.list_collections()] == ["one shelf"]
    clean_env.setenv("APPLE_BOOKS_LIBRARY_DB", str(two.library_path))
    with pytest.raises(AmbiguousStoreError, match="isn't the file being read"):
        api.create_collection("Nope", backup=False)
    assert titles(one) == ["one shelf"] and titles(two) == ["two shelf"]


def test_explicit_missing_store_is_not_found(tmp_path):
    api = PyAppleBooks(library_db=tmp_path / "missing.sqlite")
    with pytest.raises(DBConnectionError, match="No Apple Books library store found"):
        api.create_collection("Nope", backup=False)
    assert not os.path.exists(tmp_path / "missing.sqlite")
