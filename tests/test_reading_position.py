"""``PyAppleBooks.get_reading_position`` (stream 3.1, positions F16 and
the F17 page tier): where the reader is, from the reading-position row,
its page data, or the newest located annotation."""

from __future__ import annotations

import datetime as dt

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import BookNotFoundError, UnsupportedSchemaError
from py_apple_books.models.location import Location
from py_apple_books.positions import ChapterMatch, PositionSource, ReadingPosition, UnavailableReason
from py_apple_books.testing import page_location_blob, write_epub_bundle
from tests import _epub_shapes, _fs_audit
from tests._positions_helpers import cfi, icloud, p, split_book  # noqa: F401

UTC = dt.timezone.utc
BOOKMARK, RECENT = PositionSource.BOOKMARK, PositionSource.RECENT_ANNOTATION
R = UnavailableReason


def at(day: int) -> dt.datetime:
    return dt.datetime(2026, 9, day, 12, tzinfo=UTC)


def local(day: int) -> dt.datetime:
    """``at(day)`` as the library reads it back (naive local time)."""
    return at(day).astimezone().replace(tzinfo=None)


@pytest.fixture
def split(library, tmp_path):
    return library.add_book("Split", path=str(split_book(tmp_path)))


@pytest.fixture
def seven(library, tmp_path):
    """A book of seven spine files, one ToC entry each."""
    files = [(f"c{i}", p(f"text {i}.")) for i in range(7)]
    bundle = write_epub_bundle(tmp_path / "Seven.epub", files,
                               toc=[(f"Chapter {i}", f"c{i}.xhtml") for i in range(7)])
    return library.add_book("Seven", path=str(bundle))


@pytest.fixture
def pdf(library, tmp_path):
    def make(**kwargs):
        path = tmp_path / "Doc.pdf"
        path.write_bytes(b"%PDF-1.4")
        return library.add_book("Pdf", path=str(path), content_type=3, **kwargs)
    return make


class TestBookmark:
    def test_cfi(self, api, library, split):
        row = library.add_annotation(split, None, kind="reading_position", location=cfi(0, "s0", "/4/8/1:3"),
                                     position_fraction=0.4, furthest_fraction=0.6, created=at(1), modified=at(2))
        pos = api.get_reading_position(split["id"])
        assert pos == ReadingPosition(
            book_id=split["id"], source=BOOKMARK, annotation_id=row, updated=local(2),
            location=Location(cfi(0, "s0", "/4/8/1:3")), spine_index=0, item_id="s0",
            chapter=pos.chapter, match=ChapterMatch.ANCHOR, total_chapters=3, unavailable=None,
            fraction=0.4, furthest_fraction=0.6)
        assert pos.chapter.title == "B"

    def test_newest_live_row_is_authoritative(self, api, library, split):
        library.add_annotation(split, None, kind="reading_position", location=cfi(2, "s2"), modified=at(5))
        newest = library.add_annotation(split, None, kind="reading_position", location=cfi(1, "s1"), modified=at(9))
        library.add_annotation(split, None, kind="reading_position", location=cfi(0, "s0"), modified=at(20),
                               deleted=True)
        pos = api.get_reading_position(split["id"])
        assert (pos.annotation_id, pos.spine_index, pos.chapter.title, pos.match) == (
            newest, 1, "B", ChapterMatch.PRECEDING)

    def test_resolve_chapter_false_touches_no_file(self, api, library, split, icloud):
        library.add_annotation(split, None, kind="reading_position", location=cfi(2, "wrong-hint", "/4/2"))
        path = api.get_book_by_id(split["id"]).path
        icloud.mark()
        with _fs_audit.record() as rec:
            pos = api.get_reading_position(split["id"], resolve_chapter=False)
        assert (pos.spine_index, pos.item_id, pos.chapter, pos.match, pos.total_chapters, pos.unavailable) == (
            2, "wrong-hint", None, None, None, None)
        assert icloud.touched(path) == [] and rec.under(path) == []
        # Resolved, the file comes from the spine step (the hint names no item).
        resolved = api.get_reading_position(split["id"])
        assert (resolved.spine_index, resolved.item_id, resolved.chapter.title) == (2, "s2", "C")

    def test_furthest_before_the_position_is_dropped(self, api, library, split):
        library.add_annotation(split, None, kind="reading_position", location=cfi(0, "s0"),
                               position_fraction=0.7, furthest_fraction=0.5)
        pos = api.get_reading_position(split["id"])
        assert (pos.fraction, pos.furthest_fraction) == (0.7, None)

    def test_furthest_without_a_position(self, api, library, split):
        library.add_annotation(split, None, kind="reading_position", location=cfi(0, "s0"),
                               furthest_fraction=0.5)
        assert api.get_reading_position(split["id"]).furthest_fraction == 0.5

    def test_unusable_fractions_read_as_none(self, api, library, split):
        library.add_annotation(split, None, kind="reading_position", location=cfi(0, "s0"),
                               position_fraction="garbage", furthest_fraction="nan")
        pos = api.get_reading_position(split["id"])
        assert (pos.fraction, pos.furthest_fraction) == (None, None)


class TestPages:
    def test_pdf_page_count_estimated(self, api, library, pdf):
        book = pdf()
        row = library.add_annotation(book, None, kind="reading_position", user_data=page_location_blob(40),
                                     position_fraction=41 / 120)
        pos = api.get_reading_position(book["id"])
        assert (pos.source, pos.annotation_id, pos.location, pos.spine_index, pos.item_id) == (
            BOOKMARK, row, None, None, None)
        assert (pos.page, pos.page_count, pos.page_count_estimated) == (41, 120, True)
        assert (pos.chapter, pos.match, pos.unavailable) == (None, None, R.NOT_EPUB)

    def test_pdf_page_count_recorded(self, api, library, pdf):
        book = pdf(raw={"ZPAGECOUNT": 120})
        library.add_annotation(book, None, kind="reading_position", user_data=page_location_blob(40),
                               position_fraction=0.5)
        pos = api.get_reading_position(book["id"])
        assert (pos.page, pos.page_count, pos.page_count_estimated) == (41, 120, False)

    @pytest.mark.parametrize("fraction", [41 / 120.3, 41 / 119.6, None, 0.0])
    def test_no_estimate_off_a_whole_number(self, api, library, pdf, fraction):
        book = pdf()
        library.add_annotation(book, None, kind="reading_position", user_data=page_location_blob(40),
                               position_fraction=fraction)
        pos = api.get_reading_position(book["id"])
        assert (pos.page, pos.page_count, pos.page_count_estimated) == (41, None, False)

    def test_estimate_is_never_below_the_page(self, api, library, pdf):
        book = pdf()
        library.add_annotation(book, None, kind="reading_position", user_data=page_location_blob(9),
                               position_fraction=1.0)
        pos = api.get_reading_position(book["id"])
        assert (pos.page, pos.page_count) == (10, 10)

    def test_pdf_without_page_data_has_no_inferred_position(self, api, library, pdf):
        book = pdf()
        library.add_annotation(book, "a highlight", location=cfi(0, "x"))
        assert api.get_reading_position(book["id"]) is None
        library.add_annotation(book, None, kind="reading_position", position_fraction=0.25)
        pos = api.get_reading_position(book["id"])
        assert (pos.source, pos.page, pos.fraction, pos.unavailable) == (BOOKMARK, None, 0.25, R.NOT_EPUB)

    def test_epub_ordinal(self, api, library, seven):
        library.add_annotation(seven, None, kind="reading_position", user_data=page_location_blob(0, ordinal=6))
        pos = api.get_reading_position(seven["id"])
        assert (pos.source, pos.location, pos.spine_index, pos.item_id, pos.chapter.title, pos.match) == (
            BOOKMARK, None, 6, "c6", "Chapter 6", ChapterMatch.FILE)
        assert (pos.page, pos.page_count) == (None, None)
        bare = api.get_reading_position(seven["id"], resolve_chapter=False)
        assert (bare.spine_index, bare.item_id, bare.chapter) == (6, None, None)

    def test_epub_ordinal_past_the_spine(self, api, library, seven):
        library.add_annotation(seven, None, kind="reading_position", user_data=page_location_blob(0, ordinal=40))
        pos = api.get_reading_position(seven["id"])
        assert (pos.spine_index, pos.chapter, pos.unavailable, pos.total_chapters) == (
            40, None, R.NO_LOCATION, 7)

    def test_epub_page_data_without_an_ordinal_is_not_the_book_start(self, api, library, seven):
        # A missing super.ordinal reads as 0, which isn't data: the page
        # tier is skipped and the position inferred (or not given).
        import plistlib

        blob = plistlib.dumps({"class": "BKPageLocation", "pageOffset": 7}, fmt=plistlib.FMT_BINARY)
        highlight = library.add_annotation(seven, "text 2.", location=cfi(2, "c2", "/4/2,/1:0,/1:4"))
        library.add_annotation(seven, None, kind="reading_position", user_data=blob)
        pos = api.get_reading_position(seven["id"])
        assert (pos.source, pos.annotation_id, pos.spine_index, pos.chapter.title) == (
            RECENT, highlight, 2, "Chapter 2")
        assert api.get_reading_position(seven["id"], infer=False) is None

    def test_epub_ordinal_zero_when_recorded(self, api, library, seven):
        library.add_annotation(seven, None, kind="reading_position", user_data=page_location_blob(7, ordinal=0))
        pos = api.get_reading_position(seven["id"])
        assert (pos.source, pos.spine_index, pos.chapter.title, pos.page) == (BOOKMARK, 0, "Chapter 0", None)

    def test_decode_reports_whether_the_ordinal_was_recorded(self):
        import plistlib

        from py_apple_books.models.location import PageLocation, _decode_page_location

        bare = plistlib.dumps({"pageOffset": 7}, fmt=plistlib.FMT_BINARY)
        assert _decode_page_location(bare) == (PageLocation(ordinal=0, page_offset=7), False)
        assert _decode_page_location(page_location_blob(7, ordinal=3)) == (
            PageLocation(ordinal=3, page_offset=7), True)
        assert _decode_page_location(b"junk") is None
        assert PageLocation.from_plist(bare) == PageLocation(ordinal=0, page_offset=7)

    def test_cfi_wins_over_page_data(self, api, library, seven):
        library.add_annotation(seven, None, kind="reading_position", location=cfi(2, "c2"),
                               user_data=page_location_blob(0, ordinal=6))
        assert api.get_reading_position(seven["id"]).spine_index == 2


class TestInferred:
    def test_newest_located_annotation(self, api, library, split):
        library.add_annotation(split, "old", location=cfi(0, "s0", "/4/4/1:0"), created=at(1))
        newest = library.add_annotation(split, None, kind="bookmark", location=cfi(2, "s2", "/4/6"), created=at(5))
        library.add_annotation(split, "no cfi", created=at(9))
        library.add_annotation(split, "not a spine cfi", location="epubcfi(/4/2!/4)", created=at(9))
        library.add_annotation(split, "deleted", location=cfi(1, "s1"), created=at(9), deleted=True)
        pos = api.get_reading_position(split["id"])
        assert (pos.source, pos.annotation_id, pos.updated, pos.spine_index, pos.chapter.title) == (
            RECENT, newest, local(5), 2, "C")
        assert (pos.fraction, pos.furthest_fraction) == (None, None)

    def test_row_without_location_keeps_its_fractions(self, api, library, split):
        library.add_annotation(split, None, kind="reading_position", position_fraction=0.3, furthest_fraction=0.4)
        library.add_annotation(split, "hl", location=cfi(1, "s1"), created=at(3))
        pos = api.get_reading_position(split["id"])
        assert (pos.source, pos.spine_index, pos.fraction, pos.furthest_fraction) == (RECENT, 1, 0.3, 0.4)

    def test_infer_false(self, api, library, split):
        library.add_annotation(split, "hl", location=cfi(1, "s1"))
        assert api.get_reading_position(split["id"], infer=False) is None
        library.add_annotation(split, None, kind="reading_position", position_fraction=0.3)
        pos = api.get_reading_position(split["id"], infer=False)
        assert (pos.source, pos.location, pos.spine_index, pos.fraction, pos.unavailable) == (
            BOOKMARK, None, None, 0.3, R.NO_LOCATION)

    def test_a_usable_cfi_among_the_newest_five(self, api, library, split):
        target = library.add_annotation(split, "hl", location=cfi(1, "s1"), created=at(1))
        for day in range(2, 6):
            library.add_annotation(split, "x", location="epubcfi(/6/0!/4/2)", created=at(day))
        assert api.get_reading_position(split["id"]).annotation_id == target
        library.add_annotation(split, "x", location="epubcfi(/6/0!/4/4)", created=at(7))
        assert api.get_reading_position(split["id"]) is None

    def test_nothing(self, api, library, split):
        assert api.get_reading_position(split["id"]) is None
        empty = library.add_book("No asset", asset_id="")
        assert api.get_reading_position(empty["id"]) is None


class TestUnavailable:
    def test_reasons(self, api, library, tmp_path, icloud):
        cloud = library.add_book("Cloud")
        library.add_annotation(cloud, None, kind="reading_position", location=cfi(1, "c2"))
        pos = api.get_reading_position(cloud["id"])
        assert (pos.spine_index, pos.item_id, pos.unavailable, pos.total_chapters) == (1, "c2", R.NOT_DOWNLOADED, None)

        bundle = _epub_shapes.plain(tmp_path)
        (bundle / "META-INF" / "sinf.xml").write_text("<sinf/>")
        drm = library.add_book("Drm", path=str(bundle))
        library.add_annotation(drm, None, kind="reading_position", location=cfi(1, "ch2"))
        assert api.get_reading_position(drm["id"]).unavailable == R.DRM

        evicted = _epub_shapes.ncx_only(tmp_path)
        icloud.mark(evicted / "OEBPS")
        book = library.add_book("Evicted", path=str(evicted), state=3)
        library.add_annotation(book, None, kind="reading_position", location=cfi(0, "a"))
        icloud.mark()
        assert api.get_reading_position(book["id"]).unavailable == R.NOT_DOWNLOADED
        assert icloud.touched(evicted) == []

    @pytest.mark.parametrize("dataless", ["bundle", "opf"])
    def test_downloaded_state_with_a_cloud_only_part_opens_nothing_there(
            self, api, library, tmp_path, icloud, dataless):
        # ZSTATE 1 (the database says downloaded), but the bundle, or its
        # package file, is an iCloud placeholder: the gate refuses before
        # anything there is opened.
        from tests.conftest import FIXTURE_HOME

        bundle = split_book(tmp_path)
        book = library.add_book("Evicted", path=str(bundle), state=1)
        library.add_annotation(book, None, kind="reading_position", location=cfi(2, "s2", "/4/6"))
        marked = bundle if dataless == "bundle" else bundle / "OEBPS" / "content.opf"
        icloud.mark(marked)
        policy = _fs_audit.Policy.for_library(FIXTURE_HOME, books=[marked])
        with _fs_audit.block(policy) as rec:
            pos = api.get_reading_position(book["id"])
        assert (pos.spine_index, pos.item_id, pos.unavailable, pos.chapter) == (2, "s2", R.NOT_DOWNLOADED, None)
        assert rec.under(marked, "open") == [] and rec.refused == []

    def test_location_naming_no_file(self, api, library, split):
        library.add_annotation(split, None, kind="reading_position", location=cfi(9, "gone"))
        pos = api.get_reading_position(split["id"])
        assert (pos.spine_index, pos.item_id, pos.chapter, pos.unavailable, pos.total_chapters) == (
            9, "gone", None, R.NO_LOCATION, 3)

    def test_unknown_book(self, api):
        with pytest.raises(BookNotFoundError):
            api.get_reading_position(424242)


class TestQueries:
    def _count(self, sql_trace, call):
        del sql_trace[:]
        call()
        return len(sql_trace)

    def test_budget(self, api, library, pdf, split, sql_trace):
        # 500 newer annotations without a CFI, then the inference.
        library.add_annotation(split, "hl", location=cfi(1, "s1"), created=at(1))
        for i in range(500):
            library.add_annotation(split, f"n{i}", created=at(2))
        assert self._count(sql_trace, lambda: api.get_reading_position(split["id"])) <= 3
        book = api.get_book_by_id(split["id"])
        assert self._count(sql_trace, lambda: api.get_reading_position(book)) <= 2
        # A PDF with 2,000 annotations: one query for the row, no inference.
        doc = pdf()
        library.add_annotation(doc, None, kind="reading_position", user_data=page_location_blob(3))
        for i in range(2000):
            library.add_annotation(doc, f"p{i}", created=at(3))
        assert self._count(sql_trace, lambda: api.get_reading_position(doc["id"])) <= 2

    @pytest.mark.parametrize("resolve_chapter", [True, False])
    def test_budget_for_a_book_with_no_file(self, api, library, sql_trace, resolve_chapter):
        # No path (not on this Mac): the Book is read again, so three.
        row = library.add_book("Elsewhere", state=3)
        library.add_annotation(row, "hl", location=cfi(0, "x"), created=at(1))
        book = api.get_book_by_id(row["id"])
        assert book.path is None
        assert self._count(sql_trace, lambda: api.get_reading_position(
            book, resolve_chapter=resolve_chapter)) <= 3

    def test_a_book_from_another_library_is_read_again(self, api, library, split, make_library):
        library.add_annotation(split, None, kind="reading_position", location=cfi(2, "s2"))
        other = make_library()
        other.add_book("Elsewhere")
        db = LibraryDB(data_dir=other.data_dir)
        try:
            with use_library(db):
                foreign = PyAppleBooks().get_book_by_id(1)
        finally:
            db.close()
        assert foreign.id == split["id"] and foreign.title == "Elsewhere"
        assert api.get_reading_position(foreign).chapter.title == "C"

    def test_a_partial_book_is_read_again(self, api, library, split):
        library.add_annotation(split, None, kind="reading_position", location=cfi(2, "s2"))
        from py_apple_books.models.book import Book

        partial = Book.manager.filter(id=split["id"], only=["id", "title"])[0]
        assert partial.path is None
        assert api.get_reading_position(partial).chapter.title == "C"


class TestSchema:
    def test_renamed_location_column_gives_none(self, api, library, split):
        library.add_annotation(split, None, kind="reading_position", location=cfi(2, "s2"))
        library.execute("annotations", "ALTER TABLE ZAEANNOTATION RENAME COLUMN ZANNOTATIONLOCATION TO X_GONE")
        try:
            PyAppleBooks().store_info()
            from py_apple_books.db import default_library
            default_library().invalidate_schema()
            assert api.get_reading_position(split["id"]) is None
        finally:
            library.execute("annotations", "ALTER TABLE ZAEANNOTATION RENAME COLUMN X_GONE TO ZANNOTATIONLOCATION")
            default_library().invalidate_schema()

    @pytest.mark.parametrize("column", ["ZPLUSERDATA", "ZFUTUREPROOFING10", "ZFUTUREPROOFING8",
                                        "ZANNOTATIONMODIFICATIONDATE", "ZANNOTATIONCREATIONDATE"])
    def test_optional_columns(self, make_library, tmp_path, column):
        lib = make_library()
        book = lib.add_book("Split", path=str(split_book(tmp_path)))
        lib.add_annotation(book, None, kind="reading_position", location=cfi(2, "s2"), position_fraction=0.5)
        lib.add_annotation(book, "hl", location=cfi(1, "s1"))
        lib.execute("annotations", f"ALTER TABLE ZAEANNOTATION RENAME COLUMN {column} TO {column}_GONE")
        db = LibraryDB(data_dir=lib.data_dir)
        try:
            with use_library(db):
                pos = PyAppleBooks().get_reading_position(book["id"])
        finally:
            db.close()
        assert (pos.source, pos.chapter.title) == (BOOKMARK, "C")
        assert pos.fraction == (None if column == "ZFUTUREPROOFING10" else 0.5)

    def test_missing_annotation_store_gives_none(self, make_library):
        lib = make_library()
        book = lib.add_book("B")
        lib.annotation_path.unlink()
        db = LibraryDB(data_dir=lib.data_dir)
        try:
            with use_library(db):
                assert PyAppleBooks().get_reading_position(book["id"]) is None
        finally:
            db.close()

    def test_other_schema_errors_propagate(self, make_library):
        lib = make_library()
        book = lib.add_book("B")
        lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET RENAME COLUMN ZASSETID TO ZASSETID_GONE")
        db = LibraryDB(data_dir=lib.data_dir)
        try:
            with use_library(db), pytest.raises(UnsupportedSchemaError):
                PyAppleBooks().get_reading_position(book["id"])
        finally:
            db.close()


@pytest.mark.parametrize("shape", sorted(_epub_shapes.SHAPES))
def test_parity_with_1_10_current_reading_chapter(api, library, tmp_path, shape):
    """Where 1.10's get_current_reading_chapter (unchanged) names a
    chapter, the reading position names the same one."""
    bundle = _epub_shapes.SHAPES[shape](tmp_path)
    book = library.add_book(shape, path=str(bundle))
    from py_apple_books.content import BookContent

    spine = BookContent(bundle).list_spine_items()
    checked = 0
    for item in spine:
        if item.item_id is None:
            continue
        library.execute("annotations", "DELETE FROM ZAEANNOTATION")
        library.add_annotation(book, None, kind="reading_position", location=cfi(item.index, item.item_id))
        legacy = api.get_current_reading_chapter(book["id"])
        pos = api.get_reading_position(book["id"])
        assert pos.spine_index == item.index and pos.unavailable is None
        if legacy is not None:
            assert (pos.chapter, pos.match) == (legacy, ChapterMatch.FILE)
            checked += 1
    if shape not in ("gutenberg", "calibre_split", "ncx_fallback"):
        assert checked
