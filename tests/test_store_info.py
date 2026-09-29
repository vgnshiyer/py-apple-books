"""PyAppleBooks.store_info(): which stores an instance reads, the other
store files next to them, and the mapped columns the store lacks."""

import os
import shutil
import sqlite3

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.api import StoreInfo
from py_apple_books.exceptions import LibraryAccessDeniedError, LibraryNotFoundError
from py_apple_books.models import Annotation

NOTHING_MISSING = {"Book": [], "Annotation": [], "Collection": []}


@pytest.fixture
def lib(make_library):
    return make_library()


def info_of(lib, **kwargs) -> StoreInfo:
    api = PyAppleBooks(data_dir=lib.data_dir, **kwargs)
    try:
        return api.store_info()
    finally:
        api.close()


def test_normal_library(lib, monkeypatch):
    monkeypatch.delenv("APPLE_BOOKS_QUERY_TIMEOUT", raising=False)
    info = info_of(lib)
    assert info == StoreInfo(
        library_path=lib.library_path,
        annotation_path=lib.annotation_path,
        candidates={"library": [lib.library_path], "annotations": [lib.annotation_path]},
        missing_columns=NOTHING_MISSING,
        sqlite_version=sqlite3.sqlite_version,
        query_timeout=30.0,
    )
    assert info_of(lib, query_timeout=None).query_timeout is None
    assert info_of(lib, query_timeout=2).query_timeout == 2.0
    with pytest.raises(AttributeError):
        info.library_path = None


def test_default_instance(library):
    info = PyAppleBooks().store_info()
    assert (info.library_path, info.annotation_path) == (library.library_path, library.annotation_path)
    assert info.missing_columns == NOTHING_MISSING


def test_missing_annotation_store(lib):
    shutil.rmtree(lib.annotation_path.parent)
    lib.add_book("Synthetic Book")
    api = PyAppleBooks(data_dir=lib.data_dir)
    try:
        info = api.store_info()
        assert [b.title for b in api.list_books()] == ["Synthetic Book"]
    finally:
        api.close()
    assert info.library_path == lib.library_path and info.annotation_path is None
    assert info.candidates == {"library": [lib.library_path], "annotations": []}
    # Every annotation field is missing; books and collections are whole.
    assert info.missing_columns == {**NOTHING_MISSING,
                                    "Annotation": list(Annotation._get_mappings("Annotation"))}


def test_dropped_optional_column_is_listed(lib):
    lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZLASTENGAGEDDATE")
    lib.execute("annotations", "ALTER TABLE ZAEANNOTATION DROP COLUMN ZFUTUREPROOFING5")
    lib.execute("library", "ALTER TABLE ZBKCOLLECTION DROP COLUMN ZDETAILS")
    assert info_of(lib).missing_columns == {
        "Book": ["last_engaged_date"], "Annotation": ["chapter"], "Collection": ["details"]}


def test_candidates_list_decoys(lib):
    folder = lib.library_path.parent
    decoys = [folder / "BKLibrary-1-091020131601 copy.sqlite", folder / "BKLibrary-0-000000000000.sqlite",
              folder / "notes.sqlite"]
    for decoy in decoys:
        shutil.copyfile(lib.library_path, decoy)
    (folder / "readme.txt").write_text("not a store")
    info = info_of(lib)
    assert info.library_path == lib.library_path
    assert info.candidates["library"] == sorted([lib.library_path, *decoys])
    assert info.candidates["annotations"] == [lib.annotation_path]


def test_explicit_store_files(lib, make_library):
    other = make_library()
    api = PyAppleBooks(library_db=lib.library_path, annotation_db=other.annotation_path)
    try:
        info = api.store_info()
    finally:
        api.close()
    assert (info.library_path, info.annotation_path) == (lib.library_path, other.annotation_path)
    assert info.candidates == {"library": [lib.library_path], "annotations": [other.annotation_path]}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_errors_are_typed(tmp_path, lib):
    with pytest.raises(LibraryNotFoundError, match="No Apple Books library store found"):
        PyAppleBooks(data_dir=tmp_path / "nowhere").store_info()
    folder = lib.library_path.parent
    folder.chmod(0)
    try:
        with pytest.raises(LibraryAccessDeniedError):
            info_of(lib)
    finally:
        folder.chmod(0o755)


def test_store_info_reads_only(lib, sql_trace):
    before = {p: p.read_bytes() for p in (lib.library_path, lib.annotation_path)}
    info_of(lib)
    assert sql_trace == []
    assert {p: p.read_bytes() for p in before} == before
