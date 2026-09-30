"""Tests for the exception hierarchy (py_apple_books.exceptions).

1.10 extends the tree only by adding classes and bases, so every
``except`` clause written against 1.9 (including apple-books-mcp
0.8.2's) keeps catching what it caught before.
"""

import ast
import pathlib
import pickle
import sqlite3
from types import SimpleNamespace

import pytest

import py_apple_books.exceptions as ex
from py_apple_books.api import PyAppleBooks
from py_apple_books.db import exceptions as db_ex

# Every class with its direct bases. Pins the shape of the tree and
# proves each MRO is constructible (a bad one fails at import).
TREE = [
    (ex.AppleBooksError, (Exception,)),
    (ex.NotFoundError, (ex.AppleBooksError, LookupError)),
    (ex.BookNotFoundError, (ex.NotFoundError, ex.WriteError, IndexError)),
    (ex.CollectionNotFoundError, (ex.NotFoundError, ex.WriteError, IndexError)),
    (ex.AnnotationNotFoundError, (ex.NotFoundError, IndexError)),
    (ex.ChapterNotFoundError, (ex.NotFoundError,)),
    (ex.InvalidArgumentError, (ex.AppleBooksError, ValueError)),
    (ex.InvalidChoiceError, (ex.InvalidArgumentError, KeyError)),
    (ex.UnknownFieldError, (ex.InvalidChoiceError,)),
    (ex.BookNotDownloadedError, (ex.AppleBooksError,)),
    (ex.NotInLibraryError, (ex.BookNotDownloadedError,)),
    (ex.DRMProtectedError, (ex.AppleBooksError,)),
    (ex.UnsafeEpubEntryError, (ex.AppleBooksError,)),
    (ex.DBError, (ex.AppleBooksError,)),
    (ex.DBConnectionError, (ex.DBError,)),
    (ex.LibraryNotFoundError, (ex.DBConnectionError,)),
    (ex.AnnotationStoreNotFoundError, (ex.LibraryNotFoundError,)),
    (ex.LibraryAccessDeniedError, (ex.DBConnectionError,)),
    (ex.DBQueryError, (ex.DBError,)),
    (ex.UnsupportedSchemaError, (ex.DBQueryError,)),
    (ex.QueryTimeoutError, (ex.DBQueryError,)),
    (ex.WriteError, (ex.AppleBooksError,)),
    (ex.BooksAppRunningError, (ex.WriteError,)),
    (ex.SchemaValidationError, (ex.WriteError,)),
    (ex.SystemCollectionError, (ex.WriteError,)),
    (ex.BackupValidationError, (ex.WriteError,)),
    (ex.LibraryBusyError, (ex.WriteError, sqlite3.OperationalError)),
    (ex.AmbiguousStoreError, (ex.WriteError, ex.DBConnectionError)),
]

# Bases that pre-1.10 code may catch by, per class.
LEGACY = [
    (ex.BookNotFoundError, (IndexError, LookupError, ex.WriteError)),
    (ex.CollectionNotFoundError, (IndexError, LookupError, ex.WriteError)),
    (ex.AnnotationNotFoundError, (IndexError, LookupError)),
    (ex.InvalidChoiceError, (KeyError, ValueError, LookupError)),
    (ex.UnknownFieldError, (KeyError, ValueError, ex.InvalidChoiceError)),
    (ex.InvalidArgumentError, (ValueError,)),
    (ex.NotInLibraryError, (ex.BookNotDownloadedError,)),
    (ex.LibraryBusyError, (sqlite3.OperationalError, sqlite3.DatabaseError, sqlite3.Error)),
    (ex.AmbiguousStoreError, (ex.WriteError, ex.DBConnectionError, ex.DBError)),
    (ex.LibraryNotFoundError, (ex.DBConnectionError, ex.DBError)),
    (ex.AnnotationStoreNotFoundError, (ex.DBConnectionError, ex.DBError)),
    (ex.LibraryAccessDeniedError, (ex.DBConnectionError, ex.DBError)),
    (ex.UnsupportedSchemaError, (ex.DBQueryError, ex.DBError)),
    (ex.QueryTimeoutError, (ex.DBQueryError, ex.DBError)),
]


def _class_id(value):
    return value.__name__ if isinstance(value, type) else None


def _instance(cls):
    if cls is ex.UnknownFieldError:
        return cls("Book", "bogus", ["title", "author"])
    return cls("message")


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls, bases", TREE, ids=_class_id)
def test_direct_bases(cls, bases):
    assert cls.__bases__ == bases
    assert cls.__module__ == "py_apple_books.exceptions"
    assert issubclass(cls, ex.AppleBooksError)
    assert cls.__mro__[-3:] == (Exception, BaseException, object)


@pytest.mark.parametrize("cls, legacy", LEGACY, ids=_class_id)
def test_legacy_bases(cls, legacy):
    for base in legacy:
        assert issubclass(cls, base), base
    with pytest.raises(legacy[0]):
        raise _instance(cls)


def test_tree_covers_every_exported_class():
    classes = {
        obj for obj in vars(ex).values()
        if isinstance(obj, type) and issubclass(obj, BaseException)
        and obj.__module__ == ex.__name__
    }
    assert classes == {cls for cls, _ in TREE}


@pytest.mark.parametrize("name", [
    # Everything apple-books-mcp 0.8.2 imports from py_apple_books.exceptions.
    "AppleBooksError", "BookNotDownloadedError", "BookNotFoundError",
    "BooksAppRunningError", "CollectionNotFoundError", "DRMProtectedError",
    "SchemaValidationError", "SystemCollectionError", "WriteError",
])
def test_names_used_by_mcp_still_exist(name):
    assert isinstance(getattr(ex, name), type)


def test_not_found_is_still_index_error_and_write_error():
    # apple-books-mcp 0.8.2 relies on both.
    for cls in (ex.BookNotFoundError, ex.CollectionNotFoundError):
        with pytest.raises(IndexError):
            raise cls("gone")
        with pytest.raises(ex.WriteError):
            raise cls("gone")


def test_db_exceptions_are_the_same_objects():
    assert db_ex.DBError is ex.DBError
    assert db_ex.DBConnectionError is ex.DBConnectionError
    assert db_ex.DBQueryError is ex.DBQueryError
    assert sorted(db_ex.__all__) == ["DBConnectionError", "DBError", "DBQueryError"]
    with pytest.raises(ex.AppleBooksError):
        raise db_ex.DBQueryError("Error executing query: boom")


def test_exceptions_module_imports_only_sqlite3():
    tree = ast.parse(pathlib.Path(ex.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {"sqlite3"}


# ---------------------------------------------------------------------------
# Class details
# ---------------------------------------------------------------------------


def test_invalid_choice_error():
    assert str(ex.InvalidChoiceError("x")) == "x"
    e = ex.InvalidChoiceError("Unknown color 'orange'.", value="orange", valid=["green", "blue"])
    assert str(e) == "Unknown color 'orange'."  # not KeyError's repr-quoting
    assert e.value == "orange"
    assert e.valid == ("green", "blue")
    assert ex.InvalidChoiceError("x").valid == ()
    assert ex.InvalidChoiceError("x").value is None


def test_unknown_field_error_message():
    class Book:
        pass

    e = ex.UnknownFieldError(Book, "bogus", ["author", "title"])
    assert str(e) == "Book has no field 'bogus'. Valid fields: author, title."
    assert (e.model, e.field, e.value, e.valid) == (Book, "bogus", "bogus", ("author", "title"))
    assert str(ex.UnknownFieldError("Annotation", "x", ("id",))) == (
        "Annotation has no field 'x'. Valid fields: id."
    )
    with pytest.raises(KeyError):
        raise e


def test_backup_validation_error():
    reasons = {
        "NOT_A_DATABASE": "not_a_database",
        "INTEGRITY": "integrity",
        "NOT_CORE_DATA": "not_core_data",
        "WRONG_STORE": "wrong_store",
        "MODEL_MISMATCH": "model_mismatch",
        "LIVE_UNREADABLE": "live_unreadable",
        "SAME_FILE": "same_file",
    }
    for name, value in reasons.items():
        assert getattr(ex.BackupValidationError, name) == value
    e = ex.BackupValidationError("wrong store", ex.BackupValidationError.WRONG_STORE)
    assert (str(e), e.reason) == ("wrong store", "wrong_store")
    assert ex.BackupValidationError("x").reason is None


def test_library_busy_error_copies_sqlite_codes():
    cause = sqlite3.OperationalError("database is locked")
    cause.sqlite_errorcode = 5  # set by Python 3.11+ itself
    cause.sqlite_errorname = "SQLITE_BUSY"
    e = ex.LibraryBusyError("busy", cause)
    assert str(e) == "busy"
    assert (e.sqlite_errorcode, e.sqlite_errorname) == (5, "SQLITE_BUSY")
    bare = ex.LibraryBusyError("busy")
    assert (bare.sqlite_errorcode, bare.sqlite_errorname) == (None, None)


def test_library_busy_error_from_a_real_lock(tmp_path):
    db = tmp_path / "busy.sqlite"
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN EXCLUSIVE")
    writer = sqlite3.connect(db, timeout=0, isolation_level=None)
    try:
        with pytest.raises(sqlite3.OperationalError) as info:
            writer.execute("BEGIN IMMEDIATE")
        e = ex.LibraryBusyError("busy", info.value)
        assert e.sqlite_errorcode == getattr(info.value, "sqlite_errorcode", None)
        assert e.sqlite_errorname == getattr(info.value, "sqlite_errorname", None)
    finally:
        writer.close()
        holder.execute("ROLLBACK")
        holder.close()


def test_optional_attributes():
    assert ex.LibraryNotFoundError("x").path is None
    assert ex.LibraryNotFoundError("x", path="/p").path == "/p"
    assert ex.AnnotationStoreNotFoundError("x", path="/a").path == "/a"
    assert ex.LibraryAccessDeniedError("x").path is None
    assert ex.LibraryAccessDeniedError("x", path="/p").path == "/p"
    e = ex.UnsupportedSchemaError("x", table="ZBKLIBRARYASSET", column="ZTITLE")
    assert (e.table, e.column) == ("ZBKLIBRARYASSET", "ZTITLE")
    assert ex.UnsupportedSchemaError("x").table is None
    assert ex.QueryTimeoutError("x", timeout=30.0).timeout == 30.0
    assert ex.QueryTimeoutError("x").timeout is None
    assert str(ex.QueryTimeoutError("Query took too long")) == "Query took too long"


@pytest.mark.parametrize("cls", [cls for cls, _ in TREE], ids=_class_id)
def test_pickle_round_trip(cls):
    e = _instance(cls)
    copy = pickle.loads(pickle.dumps(e))
    assert type(copy) is cls
    assert str(copy) == str(e)
    assert vars(copy) == vars(e)


@pytest.mark.parametrize("e", [
    ex.LibraryNotFoundError("x", path="/p"),
    ex.LibraryAccessDeniedError("x", path="/p"),
    ex.UnsupportedSchemaError("x", table="T", column="C"),
    ex.QueryTimeoutError("x", timeout=2.5),
    ex.InvalidChoiceError("x", value="v", valid=["a"]),
    ex.BackupValidationError("x", ex.BackupValidationError.INTEGRITY),
], ids=lambda e: type(e).__name__)
def test_pickle_keeps_attributes(e):
    copy = pickle.loads(pickle.dumps(e))
    assert (type(copy), str(copy), vars(copy)) == (type(e), str(e), vars(e))


# ---------------------------------------------------------------------------
# PyAppleBooks.get_annotation_surrounding_text keeps raising DB errors
# ---------------------------------------------------------------------------


@pytest.fixture
def annotation_in_readable_chapter(monkeypatch):
    annotation = SimpleNamespace(
        location=SimpleNamespace(chapter_id="chapter-1"),
        book=SimpleNamespace(id=7),
        selected_text="a highlighted passage",
        representative_text=None,
    )
    monkeypatch.setattr(PyAppleBooks, "get_annotation_by_id", lambda self, aid: annotation)

    def book_content_raising(error):
        def get_book_content(self, book_id):
            raise error
        monkeypatch.setattr(PyAppleBooks, "get_book_content", get_book_content)

    return book_content_raising


@pytest.mark.parametrize("error", [
    ex.DBQueryError("Error executing query: disk I/O error"),
    ex.DBConnectionError("Error connecting to database"),
    ex.LibraryNotFoundError("No Apple Books library store found."),
])
def test_surrounding_text_propagates_db_errors(annotation_in_readable_chapter, error):
    annotation_in_readable_chapter(error)
    with pytest.raises(type(error)):
        PyAppleBooks().get_annotation_surrounding_text(1)


@pytest.mark.parametrize("error", [
    ex.BookNotDownloadedError("not downloaded"),
    ex.DRMProtectedError("drm"),
    ex.ChapterNotFoundError("no such chapter"),
    ex.AppleBooksError("unreadable"),
])
def test_surrounding_text_degrades_on_unreadable_books(annotation_in_readable_chapter, error):
    annotation_in_readable_chapter(error)
    assert PyAppleBooks().get_annotation_surrounding_text(1) == ""
