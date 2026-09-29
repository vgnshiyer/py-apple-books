"""Store discovery (G3.2): locate_store and find_sqlite_file.

Decoys are copies of a fixture store (which carry Apple's Core Data
metadata, so they validate) or files that aren't stores.
"""

import logging
import os
import shutil
import time

import pytest

from py_apple_books.db import LibraryDB, locate_store
from py_apple_books.db.client import FALLBACK_TTL, find_sqlite_file
from py_apple_books.exceptions import (
    AmbiguousStoreError,
    AnnotationStoreNotFoundError,
    DBConnectionError,
    LibraryAccessDeniedError,
    LibraryNotFoundError,
)

CANONICAL = "BKLibrary-1-091020131601.sqlite"
COPY = "BKLibrary-1-091020131601 copy.sqlite"

needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


@pytest.fixture
def lib(make_library):
    return make_library()


def _copy(src, dest, mtime=None):
    shutil.copyfile(src, dest)
    if mtime is not None:
        os.utime(dest, (mtime, mtime))
    return dest


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "py_apple_books.db" and r.levelno == logging.WARNING]


def test_canonical_only(lib):
    assert locate_store("library", lib.data_dir) == lib.library_path
    assert locate_store("annotations", lib.data_dir) == lib.annotation_path
    assert locate_store("library", lib.data_dir, strict=True) == lib.library_path


def test_default_data_dir_is_under_home(lib, monkeypatch):
    monkeypatch.setenv("HOME", str(lib.root))
    assert locate_store("library") == lib.library_path


def test_data_dir_argument_ignores_the_environment(lib, make_library, monkeypatch):
    other = make_library()
    monkeypatch.setenv("APPLE_BOOKS_DATA_DIR", str(other.data_dir))
    monkeypatch.setenv("HOME", str(other.root))
    assert locate_store("library", lib.data_dir) == lib.library_path


def test_canonical_beats_decoys(lib, caplog):
    folder = lib.library_path.parent
    newer = time.time() + 3600
    for name in (COPY, "BKLibrary-1-091020131601.old.sqlite",
                 "BKLibrary-1-091020131601-20260101-120000-000000.sqlite",
                 "BKLibrary-0-000000000000.sqlite"):
        _copy(lib.library_path, folder / name, mtime=newer)
    with caplog.at_level(logging.WARNING):
        assert locate_store("library", lib.data_dir) == lib.library_path
        assert locate_store("library", lib.data_dir, strict=True) == lib.library_path
    assert _warnings(caplog) == []


def test_lone_future_generation(lib):
    folder = lib.library_path.parent
    future = folder / "BKLibrary-2-202601011200.sqlite"
    lib.library_path.rename(future)
    # A valid copy that isn't named like a generation is not preferred.
    _copy(future, folder / "BKLibrary-2-202601011200 copy.sqlite", mtime=time.time() + 3600)
    assert locate_store("library", lib.data_dir) == future
    assert locate_store("library", lib.data_dir, strict=True) == future


def test_two_future_generations(lib, caplog):
    folder = lib.library_path.parent
    older, newer = folder / "BKLibrary-2-1.sqlite", folder / "BKLibrary-3-1.sqlite"
    lib.library_path.rename(newer)
    _copy(newer, older)
    now = time.time()
    os.utime(older, (now + 100, now + 100))
    os.utime(newer, (now, now))
    # The -wal counts as a write to its store.
    (folder / "BKLibrary-3-1.sqlite-wal").touch()
    os.utime(folder / "BKLibrary-3-1.sqlite-wal", (now + 200, now + 200))
    with caplog.at_level(logging.WARNING):
        assert locate_store("library", lib.data_dir) == newer
    [message] = _warnings(caplog)
    assert "BKLibrary-2-1.sqlite" in message and "BKLibrary-3-1.sqlite" in message

    with pytest.raises(AmbiguousStoreError) as exc:
        locate_store("library", lib.data_dir, strict=True)
    assert "BKLibrary-2-1.sqlite" in str(exc.value) and "BKLibrary-3-1.sqlite" in str(exc.value)
    assert str(folder) not in str(exc.value)
    assert isinstance(exc.value, DBConnectionError)


def test_zero_byte_stub_falls_back(tmp_path, caplog):
    folder = tmp_path / "BKLibrary"
    folder.mkdir()
    (folder / "BKLibrary-stub.sqlite").touch()
    with caplog.at_level(logging.WARNING):
        assert locate_store("library", tmp_path) == folder / "BKLibrary-stub.sqlite"
    [message] = _warnings(caplog)
    assert "falling back to BKLibrary-stub.sqlite" in message
    with pytest.raises(LibraryNotFoundError):
        locate_store("library", tmp_path, strict=True)


def test_invalid_canonical_next_to_a_valid_copy(lib):
    """The copy is chosen: it is the only store that validates."""
    copy = _copy(lib.library_path, lib.library_path.parent / COPY)
    lib.library_path.write_bytes(b"")
    assert locate_store("library", lib.data_dir) == copy
    assert locate_store("library", lib.data_dir, strict=True) == copy


def test_fallback_prefers_the_canonical_name(lib):
    """Nothing validates: the canonical file, not the copy sorting first."""
    (lib.library_path.parent / COPY).write_bytes(b"")
    lib.library_path.write_bytes(b"")
    assert locate_store("library", lib.data_dir) == lib.library_path


def test_non_store_files_are_ignored(lib):
    folder = lib.library_path.parent
    os.mkfifo(folder / "BKLibrary-0-fifo.sqlite")  # would block an open()
    (folder / "BKLibrary-9-9.sqlite-wal").touch()
    (folder / "notes.txt").touch()
    assert locate_store("library", lib.data_dir, strict=True) == lib.library_path
    lib.library_path.unlink()
    with pytest.raises(LibraryNotFoundError):
        locate_store("library", lib.data_dir)


def test_fallback_expires(lib):
    """A LibraryDB re-runs discovery FALLBACK_TTL after a fallback."""
    folder = lib.library_path.parent
    valid = _copy(lib.library_path, lib.root / "saved.sqlite")
    lib.library_path.write_bytes(b"")
    db = LibraryDB(data_dir=lib.data_dir)
    now = [1000.0]
    db._clock = lambda: now[0]
    assert db.paths().library == lib.library_path

    future = _copy(valid, folder / "BKLibrary-2-1.sqlite")
    now[0] += FALLBACK_TTL - 1
    assert db.paths().library == lib.library_path
    now[0] += 1
    assert db.paths().library == future
    db.close()


def test_fallback_store_is_replaced_in_the_pool(lib):
    """Connections to a fallback store aren't reused once it expires."""
    folder = lib.library_path.parent
    valid = _copy(lib.library_path, lib.root / "saved.sqlite")
    lib.library_path.write_bytes(b"")
    db = LibraryDB(data_dir=lib.data_dir)
    now = [1000.0]
    db._clock = lambda: now[0]
    assert db.execute("SELECT count(*) FROM sqlite_master") == [(0,)]

    _copy(valid, folder / "BKLibrary-2-1.sqlite")
    now[0] += FALLBACK_TTL
    assert db.execute("SELECT count(*) FROM ZBKLIBRARYASSET") == [(0,)]
    db.close()


def test_validated_stores_are_not_resolved_again(lib):
    db = LibraryDB(data_dir=lib.data_dir)
    now = [1000.0]
    db._clock = lambda: now[0]
    assert db.paths().library == lib.library_path
    _copy(lib.library_path, lib.library_path.parent / "BKLibrary-2-1.sqlite")
    now[0] += 100 * FALLBACK_TTL
    assert db.paths().library == lib.library_path


def test_missing_directory(tmp_path):
    with pytest.raises(LibraryNotFoundError, match="No Apple Books library store found") as exc:
        locate_store("library", tmp_path)
    assert exc.value.path == tmp_path / "BKLibrary"
    assert str(tmp_path) not in str(exc.value)
    with pytest.raises(AnnotationStoreNotFoundError, match="No Apple Books annotation store found"):
        locate_store("annotations", tmp_path)


def test_empty_directory_or_a_file_in_its_place(tmp_path):
    (tmp_path / "BKLibrary").mkdir()
    with pytest.raises(LibraryNotFoundError):
        locate_store("library", tmp_path)
    (tmp_path / "AEAnnotation").touch()
    with pytest.raises(AnnotationStoreNotFoundError):
        locate_store("annotations", tmp_path)


def test_unknown_kind(tmp_path):
    with pytest.raises(ValueError, match="'library' or 'annotations'"):
        locate_store("series", tmp_path)


@needs_permissions
def test_directory_access_denied(lib):
    folder = lib.library_path.parent
    folder.chmod(0)
    try:
        with pytest.raises(LibraryAccessDeniedError, match="Full Disk Access") as exc:
            locate_store("library", lib.data_dir)
    finally:
        folder.chmod(0o755)
    assert exc.value.path == folder
    assert str(folder) not in str(exc.value)


@needs_permissions
def test_store_file_access_denied(lib):
    lib.library_path.chmod(0)
    try:
        with pytest.raises(LibraryAccessDeniedError) as exc:
            locate_store("library", lib.data_dir)
    finally:
        lib.library_path.chmod(0o644)
    assert exc.value.path == lib.library_path


def test_find_sqlite_file_delegates(lib, tmp_path):
    _copy(lib.library_path, lib.library_path.parent / COPY)  # sorts first
    assert find_sqlite_file(lib.library_path.parent) == lib.library_path
    assert find_sqlite_file(lib.annotation_path.parent) == lib.annotation_path

    other = tmp_path / "Other"
    other.mkdir()
    with pytest.raises(DBConnectionError, match="No sqlite files found"):
        find_sqlite_file(other)
    (other / "b.sqlite").touch()
    (other / "a.sqlite").touch()
    assert find_sqlite_file(other) == other / "a.sqlite"
