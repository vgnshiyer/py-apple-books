"""Which stores a LibraryDB reads (R17) and APPLE_BOOKS_QUERY_TIMEOUT.

Each library is seeded with a book and a highlight named after it, so a
query shows which store it read.
"""

import logging

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import (
    DEFAULT_QUERY_TIMEOUT, USE_DEFAULT, LibraryDB, StorePaths, default_library, use_library,
)
from py_apple_books.exceptions import AnnotationStoreNotFoundError, InvalidArgumentError, LibraryNotFoundError

LOCATION_VARS = ("APPLE_BOOKS_DATA_DIR", "APPLE_BOOKS_LIBRARY_DB", "APPLE_BOOKS_ANNOTATION_DB")


@pytest.fixture
def libs(make_library):
    """Three libraries, marked 'first', 'second' and 'third'."""
    made = []
    for name in ("first", "second", "third"):
        lib = make_library()
        lib.add_annotation(lib.add_book(name), name)
        made.append(lib)
    return made


@pytest.fixture
def clean_env(monkeypatch):
    for var in LOCATION_VARS + ("APPLE_BOOKS_QUERY_TIMEOUT",):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _marks(db):
    """(book titles, highlight texts) read through ``db``."""
    books = {title for (title,) in db.execute("SELECT ZTITLE FROM ZBKLIBRARYASSET")}
    notes = {text for (text,) in db.execute("SELECT ZANNOTATIONSELECTEDTEXT FROM anno_db.ZAEANNOTATION")}
    return books, notes


def test_default_library_precedence(libs, clean_env, fresh_default_library):
    """APPLE_BOOKS_*_DB beats APPLE_BOOKS_DATA_DIR, which beats HOME."""
    home, data, files = libs
    clean_env.setenv("HOME", str(home.root))
    assert LibraryDB().paths() == StorePaths(home.library_path, home.annotation_path)

    clean_env.setenv("APPLE_BOOKS_DATA_DIR", str(data.data_dir))
    assert LibraryDB().paths() == StorePaths(data.library_path, data.annotation_path)

    clean_env.setenv("APPLE_BOOKS_LIBRARY_DB", str(files.library_path))
    assert LibraryDB().paths() == StorePaths(files.library_path, data.annotation_path)
    assert LibraryDB().library_path(strict=True) == files.library_path

    clean_env.setenv("APPLE_BOOKS_ANNOTATION_DB", str(files.annotation_path))
    db = default_library()
    assert db.paths() == StorePaths(files.library_path, files.annotation_path)
    assert _marks(db) == ({"third"}, {"third"})
    assert [b.title for b in PyAppleBooks().list_books()] == ["third"]


def test_data_dir_ignores_location_env(libs, clean_env):
    """An explicit data_dir reads both of its stores, whatever the
    environment points at."""
    mine, decoy, other = libs
    clean_env.setenv("APPLE_BOOKS_LIBRARY_DB", str(decoy.library_path))
    clean_env.setenv("APPLE_BOOKS_ANNOTATION_DB", str(decoy.annotation_path))
    clean_env.setenv("APPLE_BOOKS_DATA_DIR", str(other.data_dir))
    db = LibraryDB(data_dir=mine.data_dir)
    assert db.paths() == StorePaths(mine.library_path, mine.annotation_path)
    assert _marks(db) == ({"first"}, {"first"})
    assert db.library_path(strict=True) == mine.library_path
    with use_library(db):
        api = PyAppleBooks()
        assert [b.title for b in api.list_books()] == ["first"]
        assert [a.selected_text for a in api.list_annotations()] == ["first"]
    db.close()


def test_library_db_takes_annotations_from_the_default_dir(libs, clean_env):
    home, mine, decoy = libs
    clean_env.setenv("HOME", str(home.root))
    clean_env.setenv("APPLE_BOOKS_ANNOTATION_DB", str(decoy.annotation_path))
    clean_env.setenv("APPLE_BOOKS_DATA_DIR", str(decoy.data_dir))

    db = LibraryDB(library_db=mine.library_path)
    assert db.paths() == StorePaths(mine.library_path, home.annotation_path)
    assert _marks(db) == ({"second"}, {"first"})
    assert db.library_path(strict=True) == mine.library_path
    db.close()

    db = LibraryDB(annotation_db=mine.annotation_path)
    assert db.paths() == StorePaths(home.library_path, mine.annotation_path)

    db = LibraryDB(mine.data_dir, library_db=decoy.library_path)
    assert db.paths() == StorePaths(decoy.library_path, mine.annotation_path)


def test_missing_store_file(libs, clean_env, tmp_path):
    home = libs[0]
    missing = tmp_path / "nope.sqlite"
    with pytest.raises(LibraryNotFoundError, match="No Apple Books library store found") as exc:
        LibraryDB(library_db=missing).paths()
    assert exc.value.path == missing
    assert str(tmp_path) not in str(exc.value)

    clean_env.setenv("APPLE_BOOKS_LIBRARY_DB", str(tmp_path))  # a directory, not a store
    with pytest.raises(LibraryNotFoundError):
        LibraryDB().paths()

    # A missing annotation store only means no highlights.
    db = LibraryDB(home.data_dir, annotation_db=missing)
    assert db.paths() == StorePaths(home.library_path, None)
    assert not db.has_annotations()
    assert db.execute("SELECT ZTITLE FROM ZBKLIBRARYASSET") == [("first",)]
    with pytest.raises(AnnotationStoreNotFoundError):
        db.execute("SELECT count(*) FROM anno_db.ZAEANNOTATION")
    db.close()


def test_failures_are_not_cached(libs, clean_env, tmp_path):
    home = libs[0]
    clean_env.setenv("APPLE_BOOKS_DATA_DIR", str(tmp_path))
    db = LibraryDB()
    with pytest.raises(LibraryNotFoundError):
        db.paths()
    clean_env.setenv("APPLE_BOOKS_DATA_DIR", str(home.data_dir))
    assert db.paths().library == home.library_path


def test_candidates(libs, clean_env):
    home, mine, _ = libs
    decoy = mine.library_path.parent / "BKLibrary-1-091020131601 copy.sqlite"
    decoy.write_bytes(mine.library_path.read_bytes())
    clean_env.setenv("HOME", str(home.root))
    clean_env.setenv("APPLE_BOOKS_DATA_DIR", str(mine.data_dir))

    assert LibraryDB().candidates("library") == [decoy, mine.library_path]
    assert LibraryDB(home.data_dir).candidates("library") == [home.library_path]
    assert LibraryDB(library_db=mine.library_path).candidates("library") == [decoy, mine.library_path]
    assert LibraryDB(library_db=mine.library_path).candidates("annotations") == [home.annotation_path]
    assert LibraryDB(home.root / "nowhere").candidates("library") == []


@pytest.mark.parametrize("raw, expected", [
    (None, DEFAULT_QUERY_TIMEOUT), ("2.5", 2.5), (" 10 ", 10.0), ("1e-06", 1e-06),
    ("0", None), ("0.0", None), ("none", None), ("OFF", None),
])
def test_query_timeout_env(clean_env, raw, expected):
    if raw is not None:
        clean_env.setenv("APPLE_BOOKS_QUERY_TIMEOUT", raw)
    assert LibraryDB().query_timeout == expected
    # Unlike the location variables, it also applies to explicit libraries.
    assert LibraryDB(data_dir="/nonexistent").query_timeout == expected


@pytest.mark.parametrize("raw", ["", "abc", "-1", "nan", "30s"])
def test_invalid_query_timeout_env(clean_env, caplog, raw):
    clean_env.setenv("APPLE_BOOKS_QUERY_TIMEOUT", raw)
    with caplog.at_level(logging.WARNING, logger="py_apple_books.db"):
        assert LibraryDB().query_timeout == DEFAULT_QUERY_TIMEOUT
    assert "APPLE_BOOKS_QUERY_TIMEOUT" in caplog.text


def test_query_timeout_argument(clean_env):
    clean_env.setenv("APPLE_BOOKS_QUERY_TIMEOUT", "5")
    assert LibraryDB(query_timeout=USE_DEFAULT).query_timeout == 5.0
    assert LibraryDB(query_timeout=1).query_timeout == 1.0
    assert LibraryDB(query_timeout=None).query_timeout is None
    assert LibraryDB(query_timeout=0).query_timeout is None
    assert repr(USE_DEFAULT) == "USE_DEFAULT"
    for bad in (-1, "5", float("nan"), True):
        with pytest.raises(InvalidArgumentError):
            LibraryDB(query_timeout=bad)
