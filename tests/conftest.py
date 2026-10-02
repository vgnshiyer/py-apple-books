"""Shared pytest fixtures.

The whole suite runs against a synthetic Apple Books library: at import
time, before any test module imports py_apple_books, this file removes
the developer's APPLE_BOOKS_* settings, builds both stores from the
committed schema fixture in a temporary HOME and points HOME (and
APPLE_BOOKS_DATA_DIR) at it. The default library resolves from those.
Tests never open the real library and don't need one.

Also includes a helper to build unzipped EPUB bundle directories in a
tmp path. Apple Books stores EPUBs unzipped on disk, so fixture EPUBs
are written as directories rather than ``.zip`` files — ``BookContent``
only supports that layout.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import pathlib
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from typing import List, Optional

import pytest
from ebooklib import epub

from tests import _bootstrap

_bootstrap.isolate_environment()
FIXTURE_HOME = pathlib.Path(tempfile.mkdtemp(prefix="pab-home-"))
FIXTURE_SCHEMA = _bootstrap.build_home(FIXTURE_HOME)
os.environ["HOME"] = str(FIXTURE_HOME)
os.environ["APPLE_BOOKS_DATA_DIR"] = str(FIXTURE_HOME / _bootstrap.DOCUMENTS)


def pytest_unconfigure(config):
    shutil.rmtree(FIXTURE_HOME, ignore_errors=True)


# Apple Books files next to its Documents folder, in the same container
# (``<container>/Data``): the preferences plist (reading goals) and the
# per-book info caches (removed books).
PREFS_PLIST = "Library/Preferences/com.apple.iBooksX.plist"
BOOK_INFO_CACHES = "Library/Caches/AEEpubInfoSource"
# Modules (added in 1.11) whose ``default_*_path()``/``default_*_dir()``
# functions name such files; checked too once they exist. Every such
# function that takes no required argument is called; one may return
# None (no default here: nothing to read), which the guard skips.
_DEFAULT_LOCATION_MODULES = ("py_apple_books._prefs", "py_apple_books.book_info")


def _library_locations():
    """``[(label, path)]`` for every place a default library would read."""
    from py_apple_books.db.client import AppleBooksDBClient, default_data_dir, default_library

    lib_dir = getattr(AppleBooksDBClient, "book_lib_db", (None, None))[1]
    paths = default_library().paths()
    found = [("Path.home()", pathlib.Path.home()), ("AppleBooksDBClient.book_lib_db", lib_dir),
             ("default_library().paths().library", paths.library),
             ("default_library().paths().annotations", paths.annotations)]
    # The container files, derived from HOME and from the data dir the
    # way the library derives its stores.
    containers = {("HOME", pathlib.Path.home() / _bootstrap.DOCUMENTS),
                  ("default_data_dir()", default_data_dir())}
    if os.environ.get("APPLE_BOOKS_DATA_DIR"):
        containers.add(("APPLE_BOOKS_DATA_DIR", pathlib.Path(os.environ["APPLE_BOOKS_DATA_DIR"])))
    for source, docs in sorted(containers, key=str):
        for rel in (PREFS_PLIST, BOOK_INFO_CACHES):
            found.append((f"{rel} from {source}", pathlib.Path(docs).parent / rel))
    for name in _DEFAULT_LOCATION_MODULES:
        if importlib.util.find_spec(name) is None:
            continue
        module = importlib.import_module(name)
        for attr in sorted(vars(module)):
            fn = getattr(module, attr)
            if (attr.startswith("default_") and attr.endswith(("_path", "_dir"))
                    and callable(fn) and not isinstance(fn, type) and _no_required_args(fn)):
                location = fn()
                if location is not None:  # None: no default location, nothing read
                    found.append((f"{name}.{attr}()", location))
    return found


def _no_required_args(fn) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return all(p.default is not p.empty or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params)


@pytest.fixture(scope="session", autouse=True)
def _fixture_home_guard():
    """Stop the run if the library would resolve outside the fixture HOME."""
    root = FIXTURE_HOME.resolve()
    for label, path in _library_locations():
        if path is None or not pathlib.Path(path).resolve().is_relative_to(root):
            pytest.exit(f"{label} is {path}, outside the fixture HOME {root}; refusing to run "
                        f"against a real library", returncode=3)


def _content_cache_clearer():
    """``py_apple_books.content.clear_content_cache`` (1.11 on), or None."""
    from py_apple_books import content

    return getattr(content, "clear_content_cache", None)


@pytest.fixture(autouse=True)
def _content_cache_reset():
    """Every test starts and ends with no cached book-file data, so a test
    that rewrites a bundle can't see another test's index."""
    clear = _content_cache_clearer()
    if clear is not None:
        clear()
    yield
    if clear is not None:
        clear()


def _library_env() -> dict:
    return {k: v for k, v in os.environ.items() if k.startswith(_bootstrap.ENV_PREFIX) or k == "HOME"}


@pytest.fixture(autouse=True)
def _library_env_guard(monkeypatch):
    """Undo a test's changes to HOME and APPLE_BOOKS_* and drop the
    default library it resolved from them.

    Depends on ``monkeypatch`` so it runs before monkeypatch's own undo
    and still sees the test's changes.
    """
    from py_apple_books.db import client

    before = _library_env()
    yield
    if _library_env() != before:
        for key in set(_library_env()) - set(before):
            del os.environ[key]
        os.environ.update(before)
        client._reset_default_library()


@pytest.fixture
def fresh_default_library():
    """Start and end the test with a new default library, so it resolves
    from the environment the test sets up."""
    from py_apple_books.db import client

    client._reset_default_library()
    yield
    client._reset_default_library()


@pytest.fixture
def library():
    """The session's synthetic library, emptied before and after each test.

    The default library (``PyAppleBooks()``, the model managers) reads
    it; tests seed exactly the rows they assert on.
    """
    from py_apple_books.testing import FixtureLibrary

    lib = FixtureLibrary(FIXTURE_HOME, FIXTURE_SCHEMA)
    lib.reset()
    yield lib
    lib.reset()


@pytest.fixture
def api(library):
    """A ``PyAppleBooks`` reading the (empty) session library."""
    from py_apple_books import PyAppleBooks

    return PyAppleBooks()


@pytest.fixture
def make_library(tmp_path):
    """Factory for independent libraries under ``tmp_path``.

    ``make_library(schema=None, journal_mode='DELETE')`` returns a new
    ``FixtureLibrary`` in its own root. Read it with
    ``LibraryDB(data_dir=lib.data_dir)``, inside ``use_library(db)`` for
    the models and ``PyAppleBooks()`` (see ``lib_db``).
    """
    from py_apple_books.testing import FixtureLibrary

    count = {"n": 0}

    def _make(schema: Optional[str] = None, journal_mode: str = "DELETE"):
        count["n"] += 1
        return FixtureLibrary.create(tmp_path / f"home-{count['n']}",
                                     schema or FIXTURE_SCHEMA, journal_mode=journal_mode)

    return _make


@pytest.fixture
def lib_db(make_library):
    """A ``LibraryDB`` over a new ``FixtureLibrary`` (``lib_db.fixture``),
    closed after the test."""
    from py_apple_books.db import LibraryDB

    lib = make_library()
    db = LibraryDB(data_dir=lib.data_dir)
    db.fixture = lib
    yield db
    db.close()


@pytest.fixture
def sql_trace(monkeypatch):
    """Record every statement the read path executes as ``(sql, params)``.

    Hooks ``LibraryDB.execute`` at class level, so every library and
    manager is covered.
    """
    from py_apple_books.db import LibraryDB

    original = LibraryDB.execute
    calls = []

    def traced(self, sql, params=()):
        calls.append((sql, tuple(params or ())))
        return original(self, sql, params)

    monkeypatch.setattr(LibraryDB, "execute", traced)
    yield calls


@dataclass
class _BuiltEpub:
    """An unzipped EPUB bundle plus extra metadata for tests to assert against."""

    path: pathlib.Path
    chapter_hrefs: List[str]


def _unzip_epub_to_dir(zipped: pathlib.Path, dir_path: pathlib.Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zipped) as z:
        z.extractall(dir_path)


def _write_book_as_unzipped_epub(
    book: epub.EpubBook, dir_path: pathlib.Path, tmp: pathlib.Path
) -> pathlib.Path:
    """Write ``book`` via ebooklib to a zip, then extract into ``dir_path``."""
    zipped = tmp / "tmp.epub"
    epub.write_epub(str(zipped), book)
    _unzip_epub_to_dir(zipped, dir_path)
    return dir_path


@pytest.fixture
def epub_factory(tmp_path):
    """Factory fixture that builds a minimal unzipped EPUB on demand.

    Accepts customization via keyword arguments so individual tests can
    shape their own fixtures (nested ToC, shared-file siblings, etc.).
    """

    counter = {"n": 0}

    def _build(
        chapter_titles: Optional[List[str]] = None,
        title: str = "Test Book",
        author: str = "Test Author",
    ) -> _BuiltEpub:
        counter["n"] += 1
        book = epub.EpubBook()
        book.set_identifier(f"test-{counter['n']}")
        book.set_title(title)
        book.set_language("en")
        book.add_author(author)

        chapter_titles = chapter_titles or ["Chapter 1", "Chapter 2", "Chapter 3"]
        chapters = []
        hrefs = []
        for i, ct in enumerate(chapter_titles, start=1):
            href = f"chap{i}.xhtml"
            c = epub.EpubHtml(title=ct, file_name=href, lang="en")
            c.content = (
                f"<html><body>"
                f"<h1>{ct}</h1>"
                f"<p>Body paragraph for {ct}. It contains meaningful text.</p>"
                f"<p>A second paragraph with more words for testing.</p>"
                f"</body></html>"
            )
            book.add_item(c)
            chapters.append(c)
            hrefs.append(href)

        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = tuple(
            epub.Link(c.file_name, c.title, f"link{i}")
            for i, c in enumerate(chapters, start=1)
        )
        book.spine = ["nav"] + chapters

        dir_path = tmp_path / f"book-{counter['n']}.epub"
        _write_book_as_unzipped_epub(book, dir_path, tmp_path)
        return _BuiltEpub(path=dir_path, chapter_hrefs=hrefs)

    return _build


@pytest.fixture
def simple_epub(epub_factory):
    """A default 3-chapter EPUB bundle."""
    return epub_factory()
