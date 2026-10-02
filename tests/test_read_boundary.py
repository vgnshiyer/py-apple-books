"""The read boundary (1.11): ``PyAppleBooks.get_read_boundary``,
``BookContent.resolve_boundary`` and ``BookContent.position_at_percent``.

Every library is a ``FixtureLibrary`` and every book a synthetic bundle
from ``write_epub_bundle``.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import datetime as dt
import os
import pickle
import threading
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace

import pytest

from py_apple_books import PyAppleBooks, _icloud
from py_apple_books import content as content_module
from py_apple_books.content import BookContent
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import (
    BookNotDownloadedError,
    BookNotFoundError,
    InvalidArgumentError,
    InvalidChoiceError,
    NotEpubError,
    UnsupportedSchemaError,
)
from py_apple_books.models.location import Location
from py_apple_books.positions import (
    BoundaryPrecision,
    BoundarySource,
    BoundaryWarning,
    ReadBoundary,
    ResolvedBoundary,
    TextPosition,
)
from py_apple_books.testing import write_epub_bundle
from tests import _fs_audit

UTC = dt.timezone.utc
P = BoundarySource
W = BoundaryWarning


def cfi(index: int, item_id: str = None, tail: str = "!/4/2/1:0") -> str:
    """A CFI into spine item ``index`` (step ``/6/2(index+1)``), with an
    id assertion when ``item_id`` is given."""
    hint = f"[{item_id}]" if item_id else ""
    return f"epubcfi(/6/{2 * index + 2}{hint}{tail})"


def at(day: int) -> dt.datetime:
    return dt.datetime(2026, 9, day, 12, tzinfo=UTC)


def book_bundle(dest):
    """The spine used by most tests:

    0 nav (the navigation document, a ToC page), 1 c1, 2 c2,
    3 notes (non-linear), 4 c3, 5 c4. Each chapter's text is 100
    characters of one letter, the nav's is the ToC titles."""
    files = [
        ("nav", None, {"properties": "nav"}),
        ("c1", "<p>" + "a" * 100 + "</p>"),
        ("c2", "<p>" + "b" * 100 + "</p>"),
        ("notes", "<p>" + "n" * 100 + "</p>", {"linear": False}),
        ("c3", "<p>" + "c" * 100 + "</p>"),
        ("c4", "<p>" + "d" * 100 + "</p>"),
    ]
    toc = [("One", "c1.xhtml"), ("Two", "c2.xhtml"), ("Three", "c3.xhtml"), ("Four", "c4.xhtml")]
    return write_epub_bundle(dest / "Boundary.epub", files, toc)


def boundary(source, *, bookmark=None, highlight=None, progress=None, high_water=None,
             book_id=None, warnings=()):
    return ReadBoundary(book_id, "position" if source != P.FURTHEST else "furthest", source,
                        Location(bookmark) if bookmark else None,
                        Location(highlight) if highlight else None,
                        progress, high_water, None, warnings)


@contextlib.contextmanager
def facade(lib):
    """A ``PyAppleBooks`` over ``lib``, closed after the block."""
    api = PyAppleBooks(data_dir=lib.data_dir)
    try:
        yield api
    finally:
        api.close()


@pytest.fixture
def lib_api(make_library):
    """``(FixtureLibrary, PyAppleBooks)`` over a library of its own."""
    lib = make_library()
    api = PyAppleBooks(data_dir=lib.data_dir)
    yield lib, api
    api.close()


# ---------------------------------------------------------------------------
# get_read_boundary: arguments
# ---------------------------------------------------------------------------


class TestBasis:
    @pytest.mark.parametrize("value, expected", [
        ("position", "position"), ("Position", "position"), ("FURTHEST", "furthest"),
        ("furthest", "furthest"),
    ])
    def test_any_case(self, lib_api, value, expected):
        lib, api = lib_api
        book = lib.add_book("B")
        assert api.get_read_boundary(book["id"], basis=value).basis == expected

    @pytest.mark.parametrize("value", [None, 1, b"position", "pos", " position", "position ", "",
                                       "furthest_point", ["position"]])
    def test_refused_before_any_query(self, lib_api, sql_trace, value):
        lib, api = lib_api
        with pytest.raises(InvalidChoiceError) as exc:
            api.get_read_boundary(10 ** 6, basis=value)
        assert exc.value.valid == ("position", "furthest") and exc.value.value is value
        assert str(exc.value) == "basis must be 'position' or 'furthest'."
        assert isinstance(exc.value, InvalidArgumentError) and isinstance(exc.value, KeyError)
        assert sql_trace == []

    def test_basis_is_keyword_only(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B")
        with pytest.raises(TypeError):
            api.get_read_boundary(book["id"], "furthest")


class TestBookArgument:
    def test_unknown_id(self, lib_api):
        lib, api = lib_api
        with pytest.raises(BookNotFoundError):
            api.get_read_boundary(999)

    def test_book_id_forms(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B", progress=0.2)
        by_id = api.get_read_boundary(book["id"])
        assert api.get_read_boundary(str(book["id"])) == by_id
        assert api.get_read_boundary(api.get_book_by_id(book["id"])) == by_id
        assert by_id.book_id == book["id"] and type(by_id.book_id) is int

    def test_a_book_from_another_library_is_reread_here(self, lib_api, make_library):
        lib, api = lib_api
        other = make_library()
        mine = lib.add_book("Mine", progress=0.5)
        theirs = other.add_book("Theirs", progress=0.1)
        assert mine["id"] == theirs["id"]
        with facade(other) as other_api:
            foreign = other_api.get_book_by_id(theirs["id"])
        assert api.get_read_boundary(foreign).progress == 50.0


# ---------------------------------------------------------------------------
# get_read_boundary: tiers
# ---------------------------------------------------------------------------


class TestTiers:
    def test_reading_position(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B", progress=0.3, raw={"ZBOOKHIGHWATERMARKPROGRESS": 0.45})
        lib.add_annotation(book, None, kind="reading_position", location=cfi(2, "c2"), created=at(1))
        lib.add_annotation(book, "hl", location=cfi(4, "c3", ",/1:0,/1:5"), created=at(2))
        b = api.get_read_boundary(book["id"])
        assert b.source == P.READING_POSITION and b.basis == "position"
        assert b.bookmark == Location(cfi(2, "c2")) and b.highlight == Location(cfi(4, "c3", ",/1:0,/1:5"))
        assert b.location == b.bookmark and b.spine_index == 2
        assert (b.progress, b.high_water, b.warnings) == (30.0, 45.0, ())
        assert b.is_finished in (None, False)

    def test_several_bookmarks_give_the_earliest(self, lib_api):
        """The newest bookmark (what get_reading_position reports) is
        later in the book; the boundary takes the earliest."""
        lib, api = lib_api
        book = lib.add_book("B")
        lib.add_annotation(book, None, kind="reading_position", location=cfi(2, "c2", "!/4/8/1:0"),
                           created=at(1), modified=at(2))
        lib.add_annotation(book, None, kind="reading_position", location=cfi(2, "c2", "!/4/4/1:7"),
                           created=at(1), modified=at(1))
        lib.add_annotation(book, None, kind="reading_position", location=cfi(5, "c4"),
                           created=at(1), modified=at(3))
        b = api.get_read_boundary(book["id"])
        assert b.source == P.READING_POSITION and b.bookmark.cfi == cfi(2, "c2", "!/4/4/1:7")
        assert b.warnings == (W.MULTIPLE_BOOKMARKS,)
        assert api.get_read_boundary(book["id"]).includes(Location(cfi(2, "c2", "!/4/2/1:0")))

    def test_unusable_and_deleted_bookmarks_are_skipped(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B")
        lib.add_annotation(book, None, kind="reading_position", location=None, created=at(1))
        lib.add_annotation(book, None, kind="reading_position", location=cfi(0), deleted=True, created=at(1))
        lib.add_annotation(book, None, kind="reading_position", location="epubcfi(/4/2)", created=at(1))
        lib.add_annotation(book, "hl", location=cfi(3), created=at(1))
        b = api.get_read_boundary(book["id"])
        # No live type-3 row with a CFI into the spine: no bookmark, and
        # nothing to choose from, so no MULTIPLE_BOOKMARKS.
        assert b.source == P.RECENT_HIGHLIGHT and b.bookmark is None
        assert b.highlight == Location(cfi(3)) and b.spine_index == 3
        assert b.warnings == ()

    def test_recent_highlight_is_the_newest_located_one(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B", progress=0.9)
        lib.add_annotation(book, "old", location=cfi(5), created=at(1))
        lib.add_annotation(book, "new", location=cfi(2), created=at(5))
        lib.add_annotation(book, "gone", location=cfi(4), created=at(9), deleted=True)
        lib.add_annotation(book, "nowhere", location=None, created=at(9))
        lib.add_annotation(book, "not a spine cfi", location="epubcfi(/4/2)", created=at(9))
        lib.add_annotation(book, None, kind="bookmark", location=cfi(1), created=at(4))
        b = api.get_read_boundary(book["id"])
        assert b.source == P.RECENT_HIGHLIGHT and b.highlight == Location(cfi(2))
        assert b.progress == 90.0

    def test_a_bookmark_counts_as_a_located_annotation(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B")
        lib.add_annotation(book, None, kind="bookmark", location=cfi(1), created=at(4))
        assert api.get_read_boundary(book["id"]).highlight == Location(cfi(1))

    def test_progress(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B", progress=0.25)
        b = api.get_read_boundary(book["id"])
        assert (b.source, b.progress, b.bookmark, b.highlight, b.location) == (P.PROGRESS, 25.0, None, None, None)

    def test_none(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B", progress=0.0)
        b = api.get_read_boundary(book["id"])
        assert (b.source, b.progress, b.high_water, b.warnings) == (P.NONE, None, None, ())

    def test_finished_is_information_only(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B", finished=True, progress=0.0)
        b = api.get_read_boundary(book["id"])
        assert b.is_finished is True and b.source == P.NONE

    def test_other_books_rows_are_ignored(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B")
        other = lib.add_book("Other")
        lib.add_annotation(other, None, kind="reading_position", location=cfi(1), created=at(1))
        lib.add_annotation(other, "hl", location=cfi(1), created=at(1))
        assert api.get_read_boundary(book["id"]).source == P.NONE


class TestFurthest:
    def test_furthest_when_past_the_progress(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B", progress=0.2, raw={"ZBOOKHIGHWATERMARKPROGRESS": 0.6})
        lib.add_annotation(book, None, kind="reading_position", location=cfi(1), created=at(1))
        position = api.get_read_boundary(book["id"])
        furthest = api.get_read_boundary(book["id"], basis="furthest")
        assert position.source == P.READING_POSITION
        assert furthest.source == P.FURTHEST and furthest.basis == "furthest"
        assert (furthest.bookmark, furthest.progress, furthest.high_water) == (Location(cfi(1)), 20.0, 60.0)
        assert furthest.location == furthest.bookmark

    @pytest.mark.parametrize("high_water", [0.2, 0.1, None])
    def test_not_past_the_progress(self, lib_api, high_water):
        lib, api = lib_api
        book = lib.add_book("B", progress=0.2, raw={"ZBOOKHIGHWATERMARKPROGRESS": high_water})
        assert api.get_read_boundary(book["id"], basis="furthest").source == P.PROGRESS

    def test_furthest_without_progress(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B", progress=0.0, raw={"ZBOOKHIGHWATERMARKPROGRESS": 0.3})
        b = api.get_read_boundary(book["id"], basis="furthest")
        assert (b.source, b.progress, b.high_water) == (P.FURTHEST, None, 30.0)
        assert api.get_read_boundary(book["id"]).source == P.NONE


# ---------------------------------------------------------------------------
# get_read_boundary: database only
# ---------------------------------------------------------------------------


def _full_book(lib, bundle=None):
    book = lib.add_book("B", progress=0.3, path=bundle, raw={"ZBOOKHIGHWATERMARKPROGRESS": 0.6})
    lib.add_annotation(book, None, kind="reading_position", location=cfi(1, "c1"), created=at(1))
    lib.add_annotation(book, None, kind="reading_position", location=cfi(2, "c2"), created=at(1))
    lib.add_annotation(book, "hl", location=cfi(4, "c3"), created=at(2))
    return book


class TestDatabaseOnly:
    def test_at_most_three_statements(self, lib_api, sql_trace):
        lib, api = lib_api
        book = _full_book(lib)
        sql_trace.clear()
        api.get_read_boundary(book["id"], basis="furthest")
        assert len(sql_trace) <= 3
        loaded = api.get_book_by_id(book["id"])
        sql_trace.clear()
        api.get_read_boundary(loaded)
        assert len(sql_trace) <= 2

    def test_a_book_without_progress_is_reread_once(self, lib_api, sql_trace):
        lib, api = lib_api
        book = lib.add_book("B", progress=0.0)
        loaded = api.get_book_by_id(book["id"])
        sql_trace.clear()
        assert api.get_read_boundary(loaded).source == P.NONE
        assert len(sql_trace) <= 3

    def test_no_book_file_is_touched(self, lib_api, tmp_path, monkeypatch):
        lib, api = lib_api
        bundle = book_bundle(tmp_path)
        book = _full_book(lib, bundle)

        def refuse(*args, **kwargs):
            raise AssertionError("get_read_boundary reached the book's files")

        monkeypatch.setattr(BookContent, "__init__", refuse)
        monkeypatch.setattr(content_module, "is_downloaded", refuse)
        with _fs_audit.record() as rec:
            for basis in ("position", "furthest"):
                assert api.get_read_boundary(book["id"], basis=basis).bookmark is not None
        assert not rec.under(bundle) and not rec.of(*_fs_audit.PROCESS_EVENTS)


# ---------------------------------------------------------------------------
# get_read_boundary: other schemas
# ---------------------------------------------------------------------------


def _rename(lib, table, column):
    store = "annotations" if table == "ZAEANNOTATION" else "library"
    lib.execute(store, f"ALTER TABLE {table} RENAME COLUMN {column} TO {column}_GONE")


class TestDrift:
    """The seven columns the boundary reads, each missing in turn: the
    tiers that need it are skipped, SCHEMA_MISSING_COLUMNS says so, and
    nothing raises."""

    @pytest.mark.parametrize("table, column, basis, source", [
        ("ZAEANNOTATION", "ZANNOTATIONTYPE", "position", P.PROGRESS),
        ("ZAEANNOTATION", "ZANNOTATIONDELETED", "position", P.PROGRESS),
        ("ZAEANNOTATION", "ZANNOTATIONLOCATION", "position", P.PROGRESS),
        ("ZAEANNOTATION", "ZANNOTATIONMODIFICATIONDATE", "position", P.READING_POSITION),
        ("ZAEANNOTATION", "ZANNOTATIONCREATIONDATE", "position", P.READING_POSITION),
        ("ZBKLIBRARYASSET", "ZREADINGPROGRESS", "position", P.READING_POSITION),
        ("ZBKLIBRARYASSET", "ZBOOKHIGHWATERMARKPROGRESS", "furthest", P.READING_POSITION),
    ])
    def test_each_column(self, make_library, table, column, basis, source):
        lib = make_library()
        book = _full_book(lib)
        full = LibraryDB(data_dir=lib.data_dir)
        try:
            with use_library(full):
                before = PyAppleBooks().get_read_boundary(book["id"], basis=basis)
        finally:
            full.close()
        assert W.SCHEMA_MISSING_COLUMNS not in before.warnings
        _rename(lib, table, column)
        with facade(lib) as api:
            after = api.get_read_boundary(book["id"], basis=basis)
        assert after.source == source
        assert W.SCHEMA_MISSING_COLUMNS in after.warnings
        if source == P.READING_POSITION:
            assert after.bookmark == before.bookmark
        else:
            assert after.bookmark is None and after.highlight is None

    def test_missing_high_water_is_not_reported_for_position(self, make_library):
        lib = make_library()
        book = _full_book(lib)
        _rename(lib, "ZBKLIBRARYASSET", "ZBOOKHIGHWATERMARKPROGRESS")
        with facade(lib) as api:
            b = api.get_read_boundary(book["id"])
        assert b.high_water is None and W.SCHEMA_MISSING_COLUMNS not in b.warnings

    def test_progress_then_none(self, make_library):
        lib = make_library()
        book = lib.add_book("B", progress=0.4)
        _rename(lib, "ZBKLIBRARYASSET", "ZREADINGPROGRESS")
        with facade(lib) as api:
            b = api.get_read_boundary(book["id"])
        assert (b.source, b.progress, b.warnings) == (P.NONE, None, (W.SCHEMA_MISSING_COLUMNS,))

    def test_annotation_asset_id_missing(self, make_library):
        lib = make_library()
        book = _full_book(lib)
        _rename(lib, "ZAEANNOTATION", "ZANNOTATIONASSETID")
        with facade(lib) as api:
            b = api.get_read_boundary(book["id"])
        assert (b.source, b.progress) == (P.PROGRESS, 30.0)
        assert b.warnings == (W.SCHEMA_MISSING_COLUMNS,)

    def test_book_asset_id_missing(self, make_library):
        lib = make_library()
        book = _full_book(lib)
        _rename(lib, "ZBKLIBRARYASSET", "ZASSETID")
        with facade(lib) as api:
            with pytest.raises(UnsupportedSchemaError):
                api.get_book_by_id(book["id"])
            b = api.get_read_boundary(book["id"])
        assert (b.book_id, b.source, b.warnings) == (book["id"], P.NONE, (W.SCHEMA_MISSING_COLUMNS,))

    def test_several_columns_at_once(self, make_library):
        lib = make_library()
        book = _full_book(lib)
        for table, column in (("ZAEANNOTATION", "ZANNOTATIONLOCATION"),
                              ("ZBKLIBRARYASSET", "ZREADINGPROGRESS"),
                              ("ZBKLIBRARYASSET", "ZBOOKHIGHWATERMARKPROGRESS")):
            _rename(lib, table, column)
        with facade(lib) as api:
            for basis in ("position", "furthest"):
                b = api.get_read_boundary(book["id"], basis=basis)
                assert (b.source, b.warnings) == (P.NONE, (W.SCHEMA_MISSING_COLUMNS,))

    def test_missing_annotation_store(self, make_library):
        lib = make_library()
        book = _full_book(lib)
        lib.annotation_path.unlink()
        with facade(lib) as api:
            b = api.get_read_boundary(book["id"], basis="furthest")
            assert (b.source, b.bookmark, b.highlight) == (P.FURTHEST, None, None)
            b = api.get_read_boundary(book["id"])
        assert (b.source, b.progress, b.warnings) == (P.PROGRESS, 30.0, (W.ANNOTATIONS_UNAVAILABLE,))


# ---------------------------------------------------------------------------
# ReadBoundary.includes, on boundaries from the library
# ---------------------------------------------------------------------------


class TestIncludes:
    def test_reading_position(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B")
        lib.add_annotation(book, None, kind="reading_position", location=cfi(2, "c2", "!/4/6/1:10"),
                           created=at(1))
        b = api.get_read_boundary(book["id"])
        assert b.includes(Location(cfi(1, "c1")))
        assert b.includes(Location(cfi(2, "c2", "!/4/6/1:9")))
        assert not b.includes(Location(cfi(2, "c2", "!/4/6/1:10")))
        assert not b.includes(Location(cfi(3)))
        assert not b.includes(None) and not b.includes(Location("epubcfi(/4/2)"))
        with pytest.raises(InvalidArgumentError):
            b.includes("epubcfi(/6/2!/4)")

    def test_recent_highlight_includes_itself(self, lib_api):
        lib, api = lib_api
        book = lib.add_book("B")
        lib.add_annotation(book, "hl", location=cfi(2, "c2", "!/4/6,/1:3,/1:9"), created=at(1))
        b = api.get_read_boundary(book["id"])
        assert b.source == P.RECENT_HIGHLIGHT
        assert b.includes(b.highlight) and b.includes(Location(cfi(2, "c2", "!/4/6/1:9")))
        assert not b.includes(Location(cfi(2, "c2", "!/4/6/1:10")))

    def test_progress_and_none_include_nothing(self, lib_api):
        lib, api = lib_api
        for progress in (0.5, 0.0):
            book = lib.add_book("B", progress=progress)
            assert not api.get_read_boundary(book["id"]).includes(Location(cfi(0)))


# ---------------------------------------------------------------------------
# position_at_percent
# ---------------------------------------------------------------------------


def percent_bundle(dest):
    """Linear text 1,000 characters: c1 100 (index 0), c2 300 (2), c3 600
    (4); a non-linear note (1) and an image (3) that don't count."""
    return write_epub_bundle(dest / "Percent.epub", [
        ("c1", "<p>" + "a" * 100 + "</p>"),
        ("notes", "<p>" + "n" * 5000 + "</p>", {"linear": False}),
        ("c2", "<p>" + "b" * 300 + "</p>"),
        ("pic", b"\x89PNG", {"raw": True, "href": "p.png", "media_type": "image/png"}),
        ("c3", "<p>" + "c" * 600 + "</p>"),
    ], [("One", "c1.xhtml")])


class TestPositionAtPercent:
    @pytest.mark.parametrize("percent, index", [
        (0, 0), (0.0, 0), (9.99, 0), (10, 2), (10.0, 2), (25, 2), (39.99, 2), (40, 4), (75, 4),
        (99.999, 4), (100, 4), (100.5, 4), (1e300, 4), (float("inf"), 4), (-5, 0),
        (float("-inf"), 0), (Decimal("10"), 2), (Fraction(399, 10), 2), (True + 0, 0),
    ])
    def test_table(self, tmp_path, percent, index):
        content = BookContent(percent_bundle(tmp_path))
        assert [len(content.get_spine_item_text(i)) for i in (0, 2, 4)] == [100, 300, 600]
        assert content.position_at_percent(percent) == TextPosition(index, 0)

    @pytest.mark.parametrize("percent", [None, True, False, float("nan"), "50", b"5", [50], complex(1, 0),
                                         Decimal("NaN")])
    def test_refused(self, tmp_path, percent):
        with pytest.raises(InvalidArgumentError, match="percent must be a number from 0 to 100"):
            BookContent(percent_bundle(tmp_path)).position_at_percent(percent)

    def test_refused_before_reading(self, tmp_path):
        with pytest.raises(InvalidArgumentError):
            BookContent(tmp_path / "Missing.epub").position_at_percent(float("nan"))
        with pytest.raises(NotEpubError):
            BookContent(tmp_path / "Missing.epub").position_at_percent(50)

    def test_toc_pages_count(self, tmp_path):
        bundle = write_epub_bundle(tmp_path / "Nav.epub", [
            ("nav", None, {"properties": "nav"}), ("c1", "<p>" + "a" * 1000 + "</p>")], [("One", "c1.xhtml")])
        content = BookContent(bundle)
        assert content.list_spine_items()[0].is_toc_page
        assert content.position_at_percent(0) == TextPosition(0, 0)
        assert content.position_at_percent(5) == TextPosition(1, 0)

    def test_no_linear_text(self, tmp_path):
        bundle = write_epub_bundle(tmp_path / "Empty.epub", [
            ("pic", b"\x89PNG", {"raw": True, "href": "p.png", "media_type": "image/png"}),
            ("notes", "<p>note</p>", {"linear": False})])
        assert BookContent(bundle).position_at_percent(50) == TextPosition(0, 0)

    def test_lengths_are_measured_once(self, tmp_path, monkeypatch):
        content = BookContent(percent_bundle(tmp_path))
        calls = []
        original = BookContent.get_spine_item_text

        def counted(self, item, **kwargs):
            calls.append(item)
            return original(self, item, **kwargs)

        monkeypatch.setattr(BookContent, "get_spine_item_text", counted)
        results = [content.position_at_percent(p) for p in range(0, 101, 5)]
        assert sorted(calls) == [0, 2, 4]
        assert results[0] == TextPosition(0, 0) and results[-1] == TextPosition(4, 0)
        # A new index (here: the cache was cleared) measures again.
        content_module.clear_content_cache()
        content.position_at_percent(50)
        assert sorted(calls) == [0, 0, 2, 2, 4, 4]


# ---------------------------------------------------------------------------
# resolve_boundary
# ---------------------------------------------------------------------------


@pytest.fixture
def content(tmp_path):
    return BookContent(book_bundle(tmp_path))


def placed(resolved: ResolvedBoundary):
    return resolved.position, resolved.precision, resolved.source, resolved.warnings


class TestResolveTiers:
    def test_reading_position_is_its_item_start(self, content):
        r = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(2, "c2", "!/4/2/1:57")))
        assert placed(r) == (TextPosition(2, 0), BoundaryPrecision.SPINE_ITEM, P.READING_POSITION, ())
        assert r.book_id is None and r.boundary.bookmark.cfi == cfi(2, "c2", "!/4/2/1:57")

    def test_a_bookmark_without_an_id_assertion(self, content):
        r = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(4)))
        assert placed(r) == (TextPosition(4, 0), BoundaryPrecision.SPINE_ITEM, P.READING_POSITION, ())

    @pytest.mark.parametrize("bookmark, index", [
        (cfi(1, "c3"), 1),    # the step names c1 (1), the id c3 (4): the step is earlier
        (cfi(5, "c2"), 2),    # the step names c4 (5), the id c2 (2): the id is earlier
        (cfi(19, "c2"), 2),   # the step names nothing, the id c2
    ])
    def test_index_mismatch_uses_the_earlier(self, content, bookmark, index):
        r = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=bookmark))
        assert placed(r) == (TextPosition(index, 0), BoundaryPrecision.SPINE_ITEM, P.READING_POSITION,
                             (W.BOOKMARK_INDEX_MISMATCH,))

    def test_an_id_outside_the_spine_leaves_the_step(self, content):
        r = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(4, "ncx")))
        assert placed(r) == (TextPosition(4, 0), BoundaryPrecision.SPINE_ITEM, P.READING_POSITION, ())

    def test_unresolved_bookmark_falls_back_to_the_highlight(self, content):
        r = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(19, "zz"), highlight=cfi(5, "c4"),
                                              progress=10.0))
        assert placed(r) == (TextPosition(5, 0), BoundaryPrecision.SPINE_ITEM, P.RECENT_HIGHLIGHT,
                             (W.BOOKMARK_UNRESOLVED,))

    def test_nonlinear_bookmark_falls_back_but_never_past_its_item(self, content):
        # The highlight (c4, 5) is past the notes (3): the notes' start.
        r = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(3, "notes"),
                                              highlight=cfi(5, "c4")))
        assert placed(r) == (TextPosition(3, 0), BoundaryPrecision.SPINE_ITEM, P.READING_POSITION,
                             (W.BOOKMARK_NONLINEAR,))
        # The highlight (c1, 1) is before them: the highlight's item.
        r = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(3, "notes"),
                                              highlight=cfi(1, "c1")))
        assert placed(r) == (TextPosition(1, 0), BoundaryPrecision.SPINE_ITEM, P.RECENT_HIGHLIGHT,
                             (W.BOOKMARK_NONLINEAR,))

    def test_nonlinear_bookmark_then_progress_then_start(self, content):
        progress = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(3, "notes"), progress=30.0))
        assert placed(progress) == (content.position_at_percent(30.0), BoundaryPrecision.SPINE_ITEM,
                                    P.PROGRESS, (W.BOOKMARK_NONLINEAR,))
        assert progress.position < TextPosition(3, 0)
        start = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(3, "notes")))
        assert placed(start) == (TextPosition(0, 0), BoundaryPrecision.START, P.NONE, (W.BOOKMARK_NONLINEAR,))
        late = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(3, "notes"), progress=95.0))
        assert placed(late) == (TextPosition(3, 0), BoundaryPrecision.SPINE_ITEM, P.READING_POSITION,
                                (W.BOOKMARK_NONLINEAR,))

    def test_toc_page_bookmark(self, tmp_path):
        bundle = write_epub_bundle(tmp_path / "Guide.epub", [
            ("c1", "<p>" + "a" * 100 + "</p>"), ("contents", "<p>One Two Three</p>"),
            ("c2", "<p>" + "b" * 100 + "</p>"), ("c3", "<p>" + "c" * 100 + "</p>")],
            [("One", "c1.xhtml"), ("Two", "c2.xhtml")], guide=(("toc", "Contents", "contents.xhtml"),))
        content = BookContent(bundle)
        assert content.list_spine_items()[1].is_toc_page
        r = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(1, "contents"),
                                              highlight=cfi(0, "c1")))
        assert placed(r) == (TextPosition(0, 0), BoundaryPrecision.SPINE_ITEM, P.RECENT_HIGHLIGHT,
                             (W.BOOKMARK_TOC_PAGE,))
        r = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(1, "contents"),
                                              highlight=cfi(3, "c3")))
        assert placed(r) == (TextPosition(1, 0), BoundaryPrecision.SPINE_ITEM, P.READING_POSITION,
                             (W.BOOKMARK_TOC_PAGE,))

    def test_nav_document_bookmark(self, content):
        r = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(0, "nav"), progress=80.0))
        assert placed(r) == (TextPosition(0, 0), BoundaryPrecision.SPINE_ITEM, P.READING_POSITION,
                             (W.BOOKMARK_TOC_PAGE,))

    def test_recent_highlight(self, content):
        r = content.resolve_boundary(boundary(P.RECENT_HIGHLIGHT, highlight=cfi(4, "c3", "!/4/2,/1:5,/1:9")))
        assert placed(r) == (TextPosition(4, 0), BoundaryPrecision.SPINE_ITEM, P.RECENT_HIGHLIGHT, ())

    def test_recent_highlight_ignores_a_bookmark(self, content):
        """Only tiers at or below the boundary's source are tried."""
        r = content.resolve_boundary(boundary(P.RECENT_HIGHLIGHT, bookmark=cfi(5), highlight=cfi(1)))
        assert r.position == TextPosition(1, 0)
        r = content.resolve_boundary(boundary(P.PROGRESS, bookmark=cfi(5), highlight=cfi(5), progress=10.0))
        assert r.source == P.PROGRESS

    def test_unresolved_highlight(self, content):
        r = content.resolve_boundary(boundary(P.RECENT_HIGHLIGHT, highlight=cfi(30, "zz"), progress=50.0))
        assert placed(r) == (content.position_at_percent(50.0), BoundaryPrecision.SPINE_ITEM, P.PROGRESS,
                             (W.HIGHLIGHT_UNRESOLVED,))

    def test_highlight_in_the_notes(self, content):
        r = content.resolve_boundary(boundary(P.RECENT_HIGHLIGHT, highlight=cfi(3, "notes"), progress=95.0))
        assert placed(r) == (TextPosition(3, 0), BoundaryPrecision.SPINE_ITEM, P.RECENT_HIGHLIGHT, ())
        r = content.resolve_boundary(boundary(P.RECENT_HIGHLIGHT, highlight=cfi(3, "notes")))
        assert placed(r) == (TextPosition(0, 0), BoundaryPrecision.START, P.NONE, ())

    def test_progress(self, content):
        r = content.resolve_boundary(boundary(P.PROGRESS, progress=60.0))
        assert placed(r) == (content.position_at_percent(60.0), BoundaryPrecision.SPINE_ITEM, P.PROGRESS, ())

    def test_none(self, content):
        r = content.resolve_boundary(boundary(P.NONE, progress=None))
        assert placed(r) == (TextPosition(0, 0), BoundaryPrecision.START, P.NONE, ())

    @pytest.mark.parametrize("progress", [None, 0.0, -3.0, float("nan"), float("inf"), True, "50"])
    def test_unusable_progress_gives_the_start(self, content, progress):
        r = content.resolve_boundary(boundary(P.PROGRESS, progress=progress))
        assert placed(r)[:3] == (TextPosition(0, 0), BoundaryPrecision.START, P.NONE)

    def test_furthest_is_the_later(self, content):
        late = content.resolve_boundary(boundary(P.FURTHEST, bookmark=cfi(1, "c1"), progress=5.0,
                                                 high_water=90.0))
        assert placed(late) == (content.position_at_percent(90.0), BoundaryPrecision.SPINE_ITEM, P.FURTHEST, ())
        assert late.position > TextPosition(1, 0)
        early = content.resolve_boundary(boundary(P.FURTHEST, bookmark=cfi(5, "c4"), progress=5.0,
                                                  high_water=30.0))
        assert placed(early) == (TextPosition(5, 0), BoundaryPrecision.SPINE_ITEM, P.READING_POSITION, ())
        alone = content.resolve_boundary(boundary(P.FURTHEST, high_water=90.0))
        assert placed(alone)[2] == P.FURTHEST

    def test_warnings_of_the_boundary_come_first(self, content):
        r = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(3, "notes"),
                                              warnings=(W.MULTIPLE_BOOKMARKS, W.SCHEMA_MISSING_COLUMNS)))
        assert r.warnings == (W.MULTIPLE_BOOKMARKS, W.SCHEMA_MISSING_COLUMNS, W.BOOKMARK_NONLINEAR)


class TestResolveArguments:
    @pytest.mark.parametrize("precision", ["exact", "EXACT", "approximate", "spine_item", "", None, 1,
                                           BoundaryPrecision.EXACT])
    def test_precision_refused_before_reading(self, tmp_path, precision):
        with pytest.raises(InvalidChoiceError) as exc:
            BookContent(tmp_path / "Missing.epub").resolve_boundary(boundary(P.NONE), precision=precision)
        assert exc.value.valid == ("item",) and str(exc.value) == "precision must be 'item'."

    def test_item_in_any_case(self, content):
        b = boundary(P.READING_POSITION, bookmark=cfi(2))
        assert content.resolve_boundary(b, precision="ITEM") == content.resolve_boundary(b)

    @pytest.mark.parametrize("value", [None, "epubcfi(/6/4!)", TextPosition(1, 0), Location(cfi(1))])
    def test_not_a_boundary(self, content, value):
        with pytest.raises(InvalidArgumentError, match="boundary must be a ReadBoundary"):
            content.resolve_boundary(value)

    def test_a_resolved_boundary_is_not_a_boundary(self, content):
        resolved = content.resolve_boundary(boundary(P.NONE))
        with pytest.raises(InvalidArgumentError):
            content.resolve_boundary(resolved)

    def test_another_books_boundary_is_refused(self, tmp_path):
        bundle = book_bundle(tmp_path)
        mine = BookContent(bundle, book_id=5)
        with pytest.raises(InvalidArgumentError, match="another book"):
            mine.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(2), book_id=6))
        r = mine.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(2), book_id=5))
        assert r.book_id == 5 and r.position == TextPosition(2, 0)
        # A path-only instance can't tell, and places it as is.
        assert BookContent(bundle).resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(2), book_id=6)
                                                    ).position == TextPosition(2, 0)

    def test_not_an_epub(self, tmp_path):
        pdf = tmp_path / "Paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        with pytest.raises(NotEpubError):
            BookContent(pdf).resolve_boundary(boundary(P.NONE))


class TestResolveMemo:
    def test_remembered_per_boundary(self, content, monkeypatch):
        b = boundary(P.PROGRESS, progress=40.0)
        first = content.resolve_boundary(b)
        calls = []
        monkeypatch.setattr(BookContent, "_resolve_item", lambda self, *a: calls.append(a))
        assert content.resolve_boundary(b) is first
        assert content.resolve_boundary(dataclasses.replace(b)) is first
        assert content.resolve_boundary(b, precision="Item") is first
        assert calls == []
        content.resolve_boundary(dataclasses.replace(b, progress=41.0))
        assert len(calls) == 1

    def test_cleared_cache_resolves_again(self, content):
        b = boundary(P.READING_POSITION, bookmark=cfi(2))
        first = content.resolve_boundary(b)
        content_module.clear_content_cache()
        again = content.resolve_boundary(b)
        assert again == first

    def test_an_unhashable_boundary_is_resolved_without_the_memo(self, content):
        b = boundary(P.PROGRESS, progress=40.0)
        object.__setattr__(b, "warnings", [])  # bypassing __post_init__
        assert content.resolve_boundary(b).position == content.position_at_percent(40.0)

    def test_memo_is_bounded(self, content):
        for i in range(600):
            content.resolve_boundary(boundary(P.PROGRESS, progress=1.0 + i / 10))
        assert len(content.__dict__["_reading_resolved_memo"]) <= 256


# ---------------------------------------------------------------------------
# An evicted item (iCloud placeholder)
# ---------------------------------------------------------------------------


@pytest.fixture
def evict(monkeypatch):
    """``evict(path)`` makes ``path`` look like an iCloud placeholder
    (``SF_DATALESS``) to ``os.lstat``/``os.stat``, as in
    tests/test_content_gate.py: a real one can't be made here."""
    marked = set()

    def wrap(real):
        def call(path, *args, dir_fd=None, **kwargs):
            st = real(path, *args, dir_fd=dir_fd, **kwargs)
            if dir_fd is None and isinstance(path, (str, os.PathLike)) and os.fspath(path) in marked:
                fields = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
                fields["st_flags"] = getattr(st, "st_flags", 0) | _icloud.SF_DATALESS
                return SimpleNamespace(**fields)
            return st
        return call

    monkeypatch.setattr(os, "lstat", wrap(os.lstat))
    monkeypatch.setattr(os, "stat", wrap(os.stat))

    def mark(bundle, name):
        path = next(os.path.join(root, name) for root, _dirs, files in os.walk(bundle) if name in files)
        marked.update((path, os.path.realpath(path)))
        return path

    return mark


class TestEvictedItem:
    """A later item evicted to iCloud: what reads it refuses with
    ``BookNotDownloadedError`` and never opens it (so never downloads
    it); what doesn't need it never touches it."""

    def test_position_at_percent_refuses(self, tmp_path, evict):
        content = BookContent(book_bundle(tmp_path))
        evicted = evict(content.path, "c3.xhtml")
        with _fs_audit.record() as rec:
            with pytest.raises(BookNotDownloadedError):
                content.position_at_percent(50)
            with pytest.raises(BookNotDownloadedError):
                content.position_at_percent(5)  # the table needs every linear item
        assert rec.under(evicted) == []
        assert "_reading_linear_memo" not in content.__dict__

    @pytest.mark.parametrize("b", [
        boundary(P.PROGRESS, progress=40.0),
        boundary(P.FURTHEST, bookmark=cfi(1, "c1"), progress=10.0, high_water=90.0),
        boundary(P.READING_POSITION, bookmark=cfi(3, "notes"), progress=40.0),
    ], ids=["progress", "furthest", "nonlinear-bookmark-to-progress"])
    def test_resolve_refuses(self, tmp_path, evict, b):
        content = BookContent(book_bundle(tmp_path))
        evicted = evict(content.path, "c3.xhtml")
        with _fs_audit.record() as rec:
            with pytest.raises(BookNotDownloadedError):
                content.resolve_boundary(b)
        assert rec.under(evicted) == []
        assert not content.__dict__.get("_reading_resolved_memo")

    @pytest.mark.parametrize("b, index", [
        (boundary(P.READING_POSITION, bookmark=cfi(2, "c2"), progress=90.0), 2),
        (boundary(P.RECENT_HIGHLIGHT, highlight=cfi(5, "c4"), progress=10.0), 5),
        (boundary(P.NONE), 0),
    ], ids=["bookmark", "highlight", "none"])
    def test_an_item_placement_reads_no_text(self, tmp_path, evict, monkeypatch, b, index):
        content = BookContent(book_bundle(tmp_path))
        content.list_spine_items()
        evicted = evict(content.path, "c3.xhtml")

        def refuse(self, *args, **kwargs):
            raise AssertionError("an item placement reads no item text")

        monkeypatch.setattr(BookContent, "get_spine_item_text", refuse)
        with _fs_audit.record() as rec:
            assert content.resolve_boundary(b).position == TextPosition(index, 0)
        assert rec.under(evicted) == []

    def test_search_up_to_the_boundary(self, tmp_path, evict):
        content = BookContent(book_bundle(tmp_path))
        evicted = evict(content.path, "c3.xhtml")
        read = content.resolve_boundary(boundary(P.READING_POSITION, bookmark=cfi(4, "c3")))
        with _fs_audit.record() as rec:
            assert len(content.search("b", until=read, limit=None, count_total=True).hits) == 100
            with pytest.raises(BookNotDownloadedError):
                content.search("b", until=read, count_withheld=True)
        assert rec.under(evicted) == []


class TestThreadsAndPickling:
    def test_eight_threads(self, tmp_path):
        content = BookContent(book_bundle(tmp_path))
        reference = BookContent(content.path)
        boundaries = [boundary(P.READING_POSITION, bookmark=cfi(i % 6), highlight=cfi(2), progress=float(i))
                      for i in range(12)] + [boundary(P.FURTHEST, bookmark=cfi(1), high_water=90.0)]
        expected = [reference.resolve_boundary(b) for b in boundaries]
        percents = [reference.position_at_percent(p) for p in range(0, 101, 10)]
        searches = reference.search("aaa", limit=3)
        barrier = threading.Barrier(8)
        errors, results = [], []

        def work():
            try:
                barrier.wait()
                for _ in range(5):
                    got = ([content.resolve_boundary(b) for b in boundaries],
                           [content.position_at_percent(p) for p in range(0, 101, 10)],
                           content.search("aaa", limit=3))
                    results.append(got)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(results) == 40 and all(r == (expected, percents, searches) for r in results)

    def test_pickling(self, tmp_path, lib_api):
        lib, api = lib_api
        bundle = book_bundle(tmp_path)
        book = _full_book(lib, bundle)
        b = api.get_read_boundary(book["id"])
        content = api.get_book_content(book["id"])
        resolved = content.resolve_boundary(b)
        content.position_at_percent(50)
        for value in (b, resolved):
            clone = pickle.loads(pickle.dumps(value))
            assert clone == value and hash(clone) == hash(value)
        assert sorted(content.__getstate__()) == ["_book", "_book_id", "_opf_dir_cache", "path"]
        clone = pickle.loads(pickle.dumps(content))
        assert "_reading_resolved_memo" not in clone.__dict__ and "_reading_linear_memo" not in clone.__dict__
        assert clone.book_id == book["id"] and clone.resolve_boundary(b) == resolved
        assert copy.copy(content).resolve_boundary(b) == resolved


# ---------------------------------------------------------------------------
# End to end, on a library
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_boundary_resolved_and_searched(self, tmp_path, lib_api):
        lib, api = lib_api
        files = [
            ("c1", "<p>The ship left the harbour.</p>"),
            ("c2", "<p>The ship met a storm. The captain was calm.</p>"),
            ("c3", "<p>The captain is the traitor, said the ship's cook.</p>"),
            ("c4", "<p>The ship sank. The traitor escaped.</p>"),
        ]
        bundle = write_epub_bundle(tmp_path / "Ship.epub", files,
                                   [("One", "c1.xhtml"), ("Two", "c2.xhtml"), ("Three", "c3.xhtml"),
                                    ("Four", "c4.xhtml")])
        book = lib.add_book("Ship", path=bundle, progress=0.4)
        lib.add_annotation(book, None, kind="reading_position", location=cfi(2, "c3", "!/4/2/1:4"),
                           created=at(1))
        b = api.get_read_boundary(book["id"])
        content = api.get_book_content(book["id"])
        resolved = content.resolve_boundary(b)
        assert (resolved.book_id, resolved.position, resolved.source) == (book["id"], TextPosition(2, 0),
                                                                          P.READING_POSITION)
        result = content.search("traitor", until=resolved, count_withheld=True, count_total=True)
        assert (result.hits, result.total, result.withheld_in_item, result.withheld_later) == ((), 0, 1, 1)
        ships = content.search("ship", until=resolved)
        assert [h.start.spine_index for h in ships.hits] == [0, 1]
        assert not any("traitor" in h.snippet for h in ships.hits)
        other = lib.add_book("Other")
        with pytest.raises(InvalidArgumentError, match="another book"):
            content.resolve_boundary(api.get_read_boundary(other["id"]))
