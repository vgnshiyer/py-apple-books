"""1.11 model data (stream 1.5, R12): tolerant dates, the new Book and
Annotation fields and the properties built on them.

Unit tables feed rows through ``Model.from_db`` (no database); the rest
reads synthetic libraries, with the new columns set through the
fixture's ``raw=`` overrides, so nothing here depends on the 1.11
fixture helpers.
"""

import copy
import dataclasses
import datetime as dt
import math
import pathlib
import pickle
import plistlib
import random
import threading

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books import text as text_module
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.models import Annotation, Book, PageLocation
from py_apple_books.models.book import CONTENT_TYPE_SERIES_CONTAINER
from py_apple_books.testing import STORE_SERIES
from py_apple_books.utils import _apple_datetime_or_none, apple_timestamp_to_datetime

# NSDate.distantPast in Core Data seconds: 0000-12-30 in the proleptic
# Gregorian calendar, before datetime.min, so never a datetime.
DISTANT_PAST = -63114076800.0

# Values a Core Data date column can hold that no datetime represents.
CORRUPT_DATES = [
    "NaN", "nan", float("nan"), float("inf"), "-inf", "1e999", 1e12, -1e12,
    DISTANT_PAST, DISTANT_PAST - 1, "2019-01-01", "", "  ", b"\xff\xfe", 10 ** 400,
]

BOOK_DATES = {
    "creation_date": "ZCREATIONDATE",
    "finished_date": "ZDATEFINISHED",
    "last_opened_date": "ZLASTOPENDATE",
    "purchased_date": "ZPURCHASEDATE",
    "last_engaged_date": "ZLASTENGAGEDDATE",
}
ANNOTATION_DATES = {
    "creation_date": "ZANNOTATIONCREATIONDATE",
    "modification_date": "ZANNOTATIONMODIFICATIONDATE",
}


def _row(model, **values) -> list:
    """A DB row for ``model`` in mappings.ini order; other columns NULL."""
    return [values.get(field) for field in model._get_mappings(model.__name__)]


def book_row(**values) -> list:
    return _row(Book, **{"id": 1, "asset_id": "ASSET", "title": "Title", **values})


def annotation_row(**values) -> list:
    return _row(Annotation, **{"id": 1, "asset_id": "ASSET", "type": 2, **values})


# ---------------------------------------------------------------------------
# X-TOLERANT-DATES
# ---------------------------------------------------------------------------


class TestTolerantDateHelper:
    def test_distant_past_is_not_a_datetime(self):
        """1.10 raised ValueError for it in every time zone."""
        with pytest.raises(ValueError):
            apple_timestamp_to_datetime(DISTANT_PAST)
        assert _apple_datetime_or_none(DISTANT_PAST) is None

    def test_distant_future_is_kept(self):
        """A date 1.10 converted keeps converting (4001-01-01)."""
        far = 63113904000.0
        assert _apple_datetime_or_none(far) == apple_timestamp_to_datetime(far)
        assert _apple_datetime_or_none(far).year in (4000, 4001)

    def test_none_and_datetimes_pass_through(self):
        when = dt.datetime(2020, 1, 2, 3, 4, 5)
        assert _apple_datetime_or_none(None) is None
        assert _apple_datetime_or_none(when) is when
        aware = when.replace(tzinfo=dt.timezone.utc)
        assert _apple_datetime_or_none(aware) is aware

    @pytest.mark.parametrize("raw", CORRUPT_DATES + [object(), [], {}, dt.date(2020, 1, 1)],
                             ids=repr)
    def test_corrupt_values_read_as_none(self, raw):
        assert _apple_datetime_or_none(raw) is None

    @pytest.mark.parametrize("raw", [0, 0.0, 1.5, -1.5, 86400, 600000000, 780000000.25,
                                     "600000000", "780000000.5", b"700000000",
                                     DISTANT_PAST + 4 * 86400, 2e11])
    def test_valid_values_match_the_strict_conversion(self, raw):
        assert _apple_datetime_or_none(raw) == apple_timestamp_to_datetime(raw)

    def test_random_valid_values_match(self):
        rng = random.Random(111)
        for _ in range(2000):
            raw = rng.uniform(-3e9, 3e9)
            assert _apple_datetime_or_none(raw) == apple_timestamp_to_datetime(raw)

    def test_platform_failures_read_as_none(self, monkeypatch):
        import py_apple_books.utils as utils

        for error in (OverflowError, OSError, ValueError):
            class Failing(dt.datetime):
                @classmethod
                def fromtimestamp(cls, *args, **kwargs):
                    raise error("refused")

            monkeypatch.setattr(utils, "datetime", Failing)
            assert utils._apple_datetime_or_none(1.0) is None

    def test_idempotent(self):
        once = _apple_datetime_or_none(700000000.0)
        assert _apple_datetime_or_none(once) is once


class TestTolerantModelDates:
    @pytest.mark.parametrize("field", list(BOOK_DATES))
    @pytest.mark.parametrize("raw", ["NaN", float("inf"), 1e12, DISTANT_PAST, "2019-01-01"], ids=repr)
    def test_book_from_db(self, field, raw):
        book = Book.from_db(book_row(**{field: raw}))
        assert getattr(book, field) is None

    @pytest.mark.parametrize("field", list(ANNOTATION_DATES))
    @pytest.mark.parametrize("raw", ["NaN", float("inf"), 1e12, DISTANT_PAST, "2019-01-01"], ids=repr)
    def test_annotation_from_db(self, field, raw):
        annotation = Annotation.from_db(annotation_row(**{field: raw}))
        assert getattr(annotation, field) is None

    def test_valid_dates_are_unchanged(self):
        values = {field: 600000000.0 + n for n, field in enumerate(BOOK_DATES)}
        book = Book.from_db(book_row(**values))
        assert {f: getattr(book, f) for f in BOOK_DATES} == {
            f: apple_timestamp_to_datetime(v) for f, v in values.items()}
        annotation = Annotation.from_db(annotation_row(creation_date=1.0, modification_date=2.0))
        assert (annotation.creation_date, annotation.modification_date) == (
            apple_timestamp_to_datetime(1.0), apple_timestamp_to_datetime(2.0))

    def test_datetimes_are_kept(self):
        """Building a model from another one's fields keeps its dates
        (1.10 raised TypeError for every field but last_engaged_date)."""
        when = dt.datetime(2020, 5, 6, 7, 8, 9)
        book = Book.from_db(book_row(**{field: when for field in BOOK_DATES}))
        assert all(getattr(book, field) is when for field in BOOK_DATES)
        annotation = Annotation.from_db(annotation_row(creation_date=when, modification_date=when))
        assert annotation.creation_date is annotation.modification_date is when

    @pytest.mark.parametrize("raw", ["NaN", "Infinity", 1e12, DISTANT_PAST, "2019-01-01"], ids=repr)
    def test_every_list_still_works(self, api, library, raw):
        """One corrupt date in any column no longer fails every list."""
        good = library.add_book("Good Book", progress=0.5, last_opened=700000000.0)
        bad = library.add_book("Bad Book", progress=0.5, finished=False,
                               raw={column: raw for column in BOOK_DATES.values()})
        library.add_annotation(good, "a good highlight", created=700000000.0)
        library.add_annotation(bad, "a bad highlight",
                               raw={column: raw for column in ANNOTATION_DATES.values()})
        books = {b.title: b for b in api.list_books()}
        assert set(books) == {"Good Book", "Bad Book"}
        assert all(getattr(books["Bad Book"], field) is None for field in BOOK_DATES)
        assert books["Good Book"].last_opened_date == apple_timestamp_to_datetime(700000000.0)
        assert {b.title for b in api.get_books_in_progress()} == {"Good Book", "Bad Book"}
        if not isinstance(raw, str) or raw in ("NaN", "Infinity"):
            # Text float() can't read still fails get_recently_read_books:
            # api.py sorts the raw seconds itself, outside the models.
            assert {b.title for b in api.get_recently_read_books(limit=10)} >= {"Good Book"}
        annotations = {a.selected_text: a for a in api.list_annotations()}
        assert set(annotations) == {"a good highlight", "a bad highlight"}
        assert annotations["a bad highlight"].creation_date is None
        assert annotations["a bad highlight"].modification_date is None
        assert api.get_book_by_id(bad["id"]).title == "Bad Book"
        assert [a.selected_text for a in api.get_book_by_id(bad["id"]).annotations] == ["a bad highlight"]
        stats = api.get_library_stats()
        assert stats.total_books == 2 and stats.total_annotations == 2
        assert math.isclose(api.get_book_by_id(good["id"]).reading_progress, 50.0)

    def test_another_library_with_a_corrupt_date(self, make_library):
        """A library of its own, read through its own LibraryDB."""
        lib = make_library()
        lib.add_book("Book", raw={"ZDATEFINISHED": "garbage", "ZISFINISHED": 1})
        db = LibraryDB(data_dir=lib.data_dir)
        try:
            with use_library(db):
                [book] = PyAppleBooks().get_finished_books()
                assert book.title == "Book" and book.finished_date is None
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Book: metadata, series and high-water fields (F55, F49, F22-high-water)
# ---------------------------------------------------------------------------

BOOK_FIELDS_111 = {
    "language": "ZLANGUAGE",
    "year": "ZYEAR",
    "release_date": "ZRELEASEDATE",
    "series_id": "ZSERIESID",
    "series_container_id": "ZSERIESCONTAINER",
    "series_sequence": "ZSEQUENCENUMBER",
    "series_label": "ZSEQUENCEDISPLAYNAME",
    "series_is_ordered": "ZSERIESISORDERED",
    "high_water_progress": "ZBOOKHIGHWATERMARKPROGRESS",
}


class TestBookFieldConversions:
    def test_mapping(self):
        mapping = Book._get_mappings("Book")
        assert list(mapping)[-len(BOOK_FIELDS_111):] == list(BOOK_FIELDS_111)
        assert {f: mapping[f] for f in BOOK_FIELDS_111} == BOOK_FIELDS_111
        assert "ZACCOUNTID" not in mapping.values()

    def test_all_null(self):
        book = Book.from_db(book_row())
        assert {f: getattr(book, f) for f in BOOK_FIELDS_111} == dict.fromkeys(BOOK_FIELDS_111)

    @pytest.mark.parametrize("raw, expected", [
        ("en", "en"), ("fr_CA", "fr_CA"), ("", None), ("   ", None), (5, None), (b"en", None),
    ], ids=repr)
    def test_language(self, raw, expected):
        assert Book.from_db(book_row(language=raw)).language == expected

    @pytest.mark.parametrize("raw, expected", [
        ("2019", 2019), (" 2019 ", 2019), ("0042", 42), (2019, 2019), (1, 1), (9999, 9999),
        ("n/a", None), ("", None), ("19", None), ("20190", None), ("2019-01-01", None),
        ("0000", None), (0, None), (10000, None), (-5, None), (True, None), (2019.0, None),
        ("२०१९", None), ("２０１９", None),
    ], ids=repr)
    def test_year(self, raw, expected):
        assert Book.from_db(book_row(year=raw)).year == expected

    def test_release_date(self):
        book = Book.from_db(book_row(release_date=600000000.0))
        assert book.release_date == apple_timestamp_to_datetime(600000000.0)
        for raw in (DISTANT_PAST, 1e12, "NaN", "2019-01-01"):
            assert Book.from_db(book_row(release_date=raw)).release_date is None

    @pytest.mark.parametrize("field, raw, expected", [
        ("series_id", "1234567890", "1234567890"),
        ("series_id", 1234567890, "1234567890"),
        ("series_id", "", None),
        ("series_id", True, None),
        ("series_id", 1.5, None),
        ("series_container_id", 42, 42),
        ("series_container_id", "42", 42),
        ("series_container_id", 42.0, 42),
        ("series_container_id", 42.5, None),
        ("series_container_id", "abc", None),
        ("series_container_id", True, None),
        ("series_container_id", float("nan"), None),
        ("series_sequence", 2, 2.0),
        ("series_sequence", "2", 2.0),
        ("series_sequence", 2.5, 2.5),
        ("series_sequence", "nan", None),
        ("series_sequence", "inf", None),
        ("series_sequence", "1e999", None),
        ("series_sequence", "x", None),
        ("series_sequence", True, None),
        ("series_label", "Book 2", "Book 2"),
        ("series_label", "", None),
        ("series_label", 2, None),
        ("series_is_ordered", 1, True),
        ("series_is_ordered", 0, False),
        ("series_is_ordered", "1", True),
        ("series_is_ordered", "0", False),
        ("series_is_ordered", True, True),
        ("series_is_ordered", False, False),
        ("series_is_ordered", 2, None),
        ("series_is_ordered", "maybe", None),
        ("series_is_ordered", 1.0, None),
    ], ids=repr)
    def test_series(self, field, raw, expected):
        value = getattr(Book.from_db(book_row(**{field: raw})), field)
        assert value == expected and type(value) is type(expected)

    @pytest.mark.parametrize("raw, expected", [
        (0.87, 87.0), ("0.87", 87.0), (1, 100.0), (1.0, 100.0), (0.0001, 0.01),
        (0, None), (0.0, None), (None, None), ("", None), ("x", None), (float("nan"), None),
        (float("inf"), None), (True, None),
    ], ids=repr)
    def test_high_water_progress(self, raw, expected):
        value = Book.from_db(book_row(high_water_progress=raw)).high_water_progress
        assert value == pytest.approx(expected) if expected is not None else value is None

    def test_high_water_like_reading_progress(self):
        """Same unit and arithmetic as reading_progress."""
        for raw in (0.1, 0.333, 0.87, 1.0):
            book = Book.from_db(book_row(reading_progress=raw, high_water_progress=raw))
            assert book.high_water_progress == book.reading_progress

    def test_conversions_are_idempotent(self):
        """A Book built from another one's fields keeps the 1.11 fields
        and the dates (reading_progress and duration are converted again,
        as in 1.10)."""
        book = Book.from_db(book_row(
            language="en", year="2019", release_date=600000000.0, series_id=123,
            series_container_id="7", series_sequence="2.5", series_label="Book 2",
            series_is_ordered="1", creation_date=1.0, finished_date=2.0, last_opened_date=3.0,
            purchased_date=4.0, last_engaged_date=5.0))
        fields = {f.name: getattr(book, f.name) for f in dataclasses.fields(Book)}
        clone = Book(**fields)
        keep = [f for f in BOOK_FIELDS_111 if f != "high_water_progress"] + list(BOOK_DATES)
        assert {f: getattr(clone, f) for f in keep} == {f: getattr(book, f) for f in keep}
        assert (clone.language, clone.year, clone.series_id, clone.series_container_id,
                clone.series_sequence, clone.series_is_ordered) == ("en", 2019, "123", 7, 2.5, True)

    def test_positional_construction_without_the_new_fields(self):
        book = Book(*[None] * 24)
        assert {f: getattr(book, f) for f in BOOK_FIELDS_111} == dict.fromkeys(BOOK_FIELDS_111)
        assert not book.is_pdf

    def test_pickle_and_copy(self):
        book = Book.from_db(book_row(language="en", year="2019", series_sequence=2,
                                     high_water_progress=0.5, release_date=600000000.0))
        for clone in (pickle.loads(pickle.dumps(book)), copy.deepcopy(book), copy.copy(book)):
            assert clone == book
            assert (clone.year, clone.series_sequence, clone.high_water_progress) == (2019, 2.0, 50.0)

    def test_repr_shows_the_new_fields(self):
        text = repr(Book.from_db(book_row(language="en", series_label="Book 2")))
        assert "language='en'" in text and "series_label='Book 2'" in text
        assert text.endswith("high_water_progress=None)")


class TestIsPdf:
    @pytest.mark.parametrize("content_type, path, expected", [
        (3, None, True),
        (3, "/x/book.epub", True),
        (1, pathlib.Path("/x/Paper.PDF"), True),
        (1, "/x/paper.pdf", True),
        (None, b"/x/paper.Pdf", True),
        (1, "/x/book.epub", False),
        (1, None, False),
        (None, None, False),
        (1, "/x/pdf", False),
        (1, "/x/book.pdf.epub", False),
        (1, 42, False),
        (5, None, False),
    ], ids=repr)
    def test_table(self, content_type, path, expected):
        book = Book.from_db(book_row(content_type=content_type, path=path))
        assert book.is_pdf is expected

    def test_constant(self):
        from py_apple_books.models.book import CONTENT_TYPE_PDF

        assert CONTENT_TYPE_PDF == 3

    def test_no_file_access(self, monkeypatch, tmp_path):
        """A database-only test: no stat, no open."""
        import os

        def refuse(*args, **kwargs):
            raise AssertionError("file access")

        book = Book.from_db(book_row(content_type=1, path=str(tmp_path / "missing.pdf")))
        monkeypatch.setattr(os, "stat", refuse)
        monkeypatch.setattr(os, "lstat", refuse)
        assert book.is_pdf is True


class TestBookFieldsFromTheLibrary:
    def test_columns_are_read(self, api, library):
        container = library.add_book("A Series", data_source=STORE_SERIES,
                                     content_type=CONTENT_TYPE_SERIES_CONTAINER,
                                     raw={"ZSERIESISORDERED": 1, "ZSTOREID": "900"})
        volume = library.add_book("Volume Two", progress=0.25, raw={
            "ZLANGUAGE": "en", "ZYEAR": "2019", "ZRELEASEDATE": 600000000.0, "ZSERIESID": "900",
            "ZSERIESCONTAINER": container["id"], "ZSEQUENCENUMBER": 2, "ZSEQUENCEDISPLAYNAME": "Book 2",
            "ZBOOKHIGHWATERMARKPROGRESS": 0.87})
        book = api.get_book_by_id(volume["id"])
        assert (book.language, book.year, book.series_id, book.series_container_id,
                book.series_sequence, book.series_label) == ("en", 2019, "900", container["id"], 2.0,
                                                             "Book 2")
        assert book.release_date == apple_timestamp_to_datetime(600000000.0)
        assert book.high_water_progress == pytest.approx(87.0)
        assert book.reading_progress == pytest.approx(25.0)
        assert api.get_book_by_id(container["id"]).series_is_ordered is True

    def test_fixture_high_water_follows_progress(self, api, library):
        """FixtureLibrary writes ZBOOKHIGHWATERMARKPROGRESS = progress."""
        started = library.add_book("Started", progress=0.4)
        fresh = library.add_book("Fresh", progress=0.0)
        unknown = library.add_book("Unknown", progress=0.4, raw={"ZBOOKHIGHWATERMARKPROGRESS": None})
        assert api.get_book_by_id(started["id"]).high_water_progress == pytest.approx(40.0)
        assert api.get_book_by_id(fresh["id"]).high_water_progress is None
        assert api.get_book_by_id(unknown["id"]).high_water_progress is None

    def test_filterable(self, library):
        library.add_book("Other")
        one = library.add_book("In series", raw={"ZSERIESID": "900", "ZSEQUENCENUMBER": 1})
        two = library.add_book("Also", raw={"ZSERIESID": "900", "ZSEQUENCENUMBER": 2})
        assert [b.id for b in Book.manager.filter(series_id="900", order_by="-series_sequence")] == [
            two["id"], one["id"]]
        assert Book.manager.filter(high_water_progress__gt=0.5).count() == 0

    def test_garbage_never_fails_a_list(self, api, library):
        library.add_book("Clean", progress=0.5)
        # Text columns turn numbers into text, so their garbage is a blob.
        library.add_book("Garbage", progress=0.5, raw={
            "ZLANGUAGE": b"en", "ZYEAR": "n/a", "ZRELEASEDATE": "NaN", "ZSERIESID": b"\x00",
            "ZSERIESCONTAINER": "abc", "ZSEQUENCENUMBER": "x", "ZSEQUENCEDISPLAYNAME": b"\xff",
            "ZSERIESISORDERED": "maybe", "ZBOOKHIGHWATERMARKPROGRESS": "lots"})
        books = {b.title: b for b in api.list_books()}
        assert set(books) == {"Clean", "Garbage"}
        assert {f: getattr(books["Garbage"], f) for f in BOOK_FIELDS_111} == dict.fromkeys(BOOK_FIELDS_111)
        assert {b.title for b in api.get_books_in_progress()} == {"Clean", "Garbage"}
        assert api.get_library_stats().in_progress_books == 2


class TestStoreInfo:
    def test_missing_columns_lists_the_new_fields(self, make_library):
        lib = make_library()
        lib.add_book("Book", progress=0.5, raw={"ZLANGUAGE": "en", "ZYEAR": "2019"})
        for column in BOOK_FIELDS_111.values():
            # Renamed rather than dropped: two of them are indexed.
            lib.execute("library", f"ALTER TABLE ZBKLIBRARYASSET RENAME COLUMN {column} TO {column}_GONE")
        api = PyAppleBooks(data_dir=lib.data_dir)
        try:
            info = api.store_info()
            assert info.missing_columns["Book"] == list(BOOK_FIELDS_111)
            [book] = api.list_books()
            assert {f: getattr(book, f) for f in BOOK_FIELDS_111} == dict.fromkeys(BOOK_FIELDS_111)
        finally:
            api.close()

    def test_nothing_missing_on_the_full_schema(self, make_library):
        api = PyAppleBooks(data_dir=make_library().data_dir)
        try:
            assert api.store_info().missing_columns == {"Book": [], "Annotation": [], "Collection": []}
        finally:
            api.close()



# ---------------------------------------------------------------------------
# Annotation: page location and fractions (F17), short selections (F23)
# ---------------------------------------------------------------------------

ANNOTATION_FIELDS_111 = {
    "location_data": "ZPLUSERDATA",
    "position_fraction": "ZFUTUREPROOFING10",
    "furthest_fraction": "ZFUTUREPROOFING8",
}
BOOKMARK, HIGHLIGHT, POSITION = 1, 2, 3


def page_blob(page_offset: int, ordinal: int = 0) -> bytes:
    """A ZPLUSERDATA blob in the shape Books writes."""
    return plistlib.dumps({"class": "BKPageLocation", "pageOffset": page_offset,
                           "super": {"class": "BKLocation", "ordinal": ordinal}},
                          fmt=plistlib.FMT_BINARY)


class TestAnnotationFieldConversions:
    def test_mapping(self):
        mapping = Annotation._get_mappings("Annotation")
        keys = list(mapping)
        assert keys[keys.index("position") + 1:] == list(ANNOTATION_FIELDS_111)
        assert {f: mapping[f] for f in ANNOTATION_FIELDS_111} == ANNOTATION_FIELDS_111

    def test_fields_are_last_and_defaulted(self):
        fields = dataclasses.fields(Annotation)
        assert [f.name for f in fields][-3:] == list(ANNOTATION_FIELDS_111)
        assert all(f.default is None for f in fields[-3:])
        assert [f.repr for f in fields][-3:] == [False, True, True]

    @pytest.mark.parametrize("raw, expected", [
        ("0.4375", 0.4375), ("0", 0.0), ("1", 1.0), ("1.0", 1.0), (".25", 0.25),
        (0.25, 0.25), (1, 1.0), (0, 0.0), ("1.0000001", 1.0), (1 + 1e-6, 1.0), ("-0", 0.0),
        (None, None), ("", None), (" ", None), ("abc", None), ("nan", None), ("inf", None),
        ("-0.1", None), (-1e-9, None), ("1.000002", None), (2, None), (True, None), (b"0.5", None),
    ], ids=repr)
    @pytest.mark.parametrize("kind", [BOOKMARK, POSITION])
    def test_fractions(self, kind, raw, expected):
        annotation = Annotation.from_db(annotation_row(
            type=kind, position_fraction=raw, furthest_fraction=raw))
        for value in (annotation.position_fraction, annotation.furthest_fraction):
            assert value == expected and type(value) is type(expected)

    @pytest.mark.parametrize("kind", [0, HIGHLIGHT, None, 4])
    def test_fractions_only_on_bookmark_rows(self, kind):
        """Types 1 and 3 only: other rows don't use the columns, so a
        value there is not a position."""
        blob = page_blob(211)
        annotation = Annotation.from_db(annotation_row(
            type=kind, position_fraction="0.5", furthest_fraction="0.6", location_data=blob))
        assert (annotation.position_fraction, annotation.furthest_fraction) == (None, None)
        assert annotation.page_location is None
        assert annotation.location_data == blob

    def test_location_data_is_kept_raw(self):
        for raw in (b"", b"garbage", page_blob(12), "text", None):
            assert Annotation.from_db(annotation_row(type=POSITION, location_data=raw)).location_data == raw

    def test_repr(self):
        annotation = Annotation.from_db(annotation_row(
            type=POSITION, location_data=page_blob(211), position_fraction="0.5", furthest_fraction="0.75"))
        text = repr(annotation)
        assert "location_data" not in text and "bplist" not in text
        assert text.endswith("position_fraction=0.5, furthest_fraction=0.75)")

    def test_round_trip(self):
        annotation = Annotation.from_db(annotation_row(
            type=POSITION, location_data=page_blob(211), position_fraction="0.5",
            furthest_fraction="1.0000001", creation_date=1.0, modification_date=2.0))
        fields = {f.name: getattr(annotation, f.name) for f in dataclasses.fields(Annotation)}
        clone = Annotation(**fields)
        assert clone == annotation and clone.page_location == annotation.page_location
        assert (clone.position_fraction, clone.furthest_fraction) == (0.5, 1.0)


class TestPageLocationProperty:
    @pytest.mark.parametrize("kind", [BOOKMARK, POSITION])
    def test_decoded(self, kind):
        annotation = Annotation.from_db(annotation_row(type=kind, location_data=page_blob(211)))
        assert annotation.page_location == PageLocation(ordinal=0, page_offset=211)
        assert annotation.page_location.page == 212

    def test_epub_bookmark_ordinal(self):
        annotation = Annotation.from_db(annotation_row(type=POSITION, location_data=page_blob(0, 9)))
        assert annotation.page_location == PageLocation(ordinal=9, page_offset=0)

    @pytest.mark.parametrize("raw", [None, b"", b"garbage", "text", 5, page_blob(-1)], ids=repr)
    def test_none(self, raw):
        assert Annotation.from_db(annotation_row(type=POSITION, location_data=raw)).page_location is None

    def test_lazy_and_cached(self, monkeypatch):
        calls = []
        original = PageLocation.from_plist.__func__

        def counting(cls, data):
            calls.append(data)
            return original(cls, data)

        monkeypatch.setattr(PageLocation, "from_plist", classmethod(counting))
        annotation = Annotation.from_db(annotation_row(type=POSITION, location_data=page_blob(3)))
        assert calls == []
        first = annotation.page_location
        assert annotation.page_location is first and len(calls) == 1
        # Not a field: equality and the pickled state are unchanged.
        assert annotation == Annotation.from_db(annotation_row(type=POSITION, location_data=page_blob(3)))
        clone = pickle.loads(pickle.dumps(annotation))
        assert clone == annotation and clone.page_location == first
        assert not [key for key in annotation.__getstate__() if "page_location" in key]

    def test_cache_follows_the_data(self):
        annotation = Annotation.from_db(annotation_row(type=POSITION, location_data=page_blob(3)))
        assert annotation.page_location.page_offset == 3
        annotation.location_data = page_blob(9)
        assert annotation.page_location.page_offset == 9
        annotation.type = HIGHLIGHT
        assert annotation.page_location is None

    def test_not_a_field(self):
        names = {f.name for f in dataclasses.fields(Annotation)}
        assert not {"page_location", "is_short_selection"} & names

    def test_threads(self):
        """One annotation read from many threads at once: every thread
        gets the same value, and nothing raises."""
        annotation = Annotation.from_db(annotation_row(type=POSITION, location_data=page_blob(7, 2)))
        barrier = threading.Barrier(16)
        results, errors = [], []

        def read():
            try:
                barrier.wait()
                for _ in range(200):
                    results.append(annotation.page_location)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        workers = [threading.Thread(target=read) for _ in range(16)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert errors == [] and len(results) == 16 * 200
        assert set(results) == {PageLocation(ordinal=2, page_offset=7)}


class TestIsShortSelection:
    @pytest.mark.parametrize("text, expected", [
        ("Ephemeral,", True), ("in medias res", True), ("“ubiquitous.”", True),
        ("A whole sentence that is clearly a passage rather than a word.", False),
        ("1984", False), ("a", False), (None, False), ("", False),
    ], ids=repr)
    def test_highlights(self, text, expected):
        annotation = Annotation.from_db(annotation_row(type=HIGHLIGHT, selected_text=text))
        assert annotation.is_short_selection is expected
        assert expected == text_module.is_short_selection(text)

    @pytest.mark.parametrize("kind", [BOOKMARK, POSITION])
    def test_bookmark_rows_are_never_short(self, kind):
        annotation = Annotation.from_db(annotation_row(type=kind, selected_text="Ephemeral"))
        assert annotation.is_short_selection is False

    def test_tombstone(self):
        assert Annotation.from_db(annotation_row(type=0, selected_text=None)).is_short_selection is False

    def test_seed_demo(self, api, library, tmp_path):
        from py_apple_books.testing import seed_demo

        seed_demo(library, tmp_path)
        rows = list(Annotation.manager.all())
        assert {a.type for a in rows} >= {POSITION, HIGHLIGHT}
        assert all(a.is_short_selection is False for a in rows if a.type in (BOOKMARK, POSITION))
        assert all(isinstance(a.is_short_selection, bool) for a in rows)


class TestAnnotationFieldsFromTheLibrary:
    def test_a_pdf_reading_position(self, api, library):
        book = library.add_book("Paper", content_type=3, path="/nonexistent/Paper.pdf")
        pk = library.add_annotation(book, None, kind="reading_position", raw={
            "ZPLUSERDATA": page_blob(211), "ZFUTUREPROOFING10": "0.4375",
            "ZFUTUREPROOFING8": "0.46875"})
        annotation = api.get_annotation_by_id(pk)
        assert annotation.page_location == PageLocation(0, 211) and annotation.page_location.page == 212
        assert (annotation.position_fraction, annotation.furthest_fraction) == (0.4375, 0.46875)
        assert api.get_book_by_id(book["id"]).is_pdf

    def test_highlights_ignore_the_columns(self, api, library):
        book = library.add_book()
        pk = library.add_annotation(book, "text", raw={
            "ZPLUSERDATA": page_blob(1), "ZFUTUREPROOFING10": "0.5", "ZFUTUREPROOFING8": "0.5"})
        annotation = api.get_annotation_by_id(pk)
        assert (annotation.position_fraction, annotation.furthest_fraction, annotation.page_location) == (
            None, None, None)
        assert annotation.location_data == page_blob(1)

    def test_fractions_are_text_in_sql(self, library):
        """Books stores them as text: SQL compares and sorts them as text."""
        book = library.add_book()
        for value in ("0.9", "0.10", "1"):
            library.add_annotation(book, None, kind="reading_position", raw={"ZFUTUREPROOFING8": value})
        rows = Annotation.manager.filter(type=POSITION, order_by="furthest_fraction")
        assert [a.furthest_fraction for a in rows] == [0.1, 0.9, 1.0]
        assert [a.furthest_fraction for a in Annotation.manager.filter(furthest_fraction__gt="0.5")] == [
            0.9, 1.0]

    def test_garbage_never_fails_a_list(self, api, library):
        book = library.add_book()
        library.add_annotation(book, "a highlight")
        library.add_annotation(book, None, kind="reading_position", raw={
            "ZPLUSERDATA": b"\xff" * 300, "ZFUTUREPROOFING10": "x", "ZFUTUREPROOFING8": b"\x00"})
        library.add_annotation(book, None, kind="bookmark", raw={
            "ZPLUSERDATA": b"bplist00" + b"\xff" * 40, "ZFUTUREPROOFING10": "NaN"})
        assert [a.selected_text for a in api.list_annotations()] == ["a highlight", None]
        rows = list(Annotation.manager.all())
        assert all(a.page_location is None and a.position_fraction is None for a in rows)
        assert api.get_current_reading_location(book["id"]) is not None

    def test_store_info_lists_missing_columns(self, make_library):
        lib = make_library()
        for column in ANNOTATION_FIELDS_111.values():
            lib.execute("annotations", f"ALTER TABLE ZAEANNOTATION RENAME COLUMN {column} TO {column}_GONE")
        api = PyAppleBooks(data_dir=lib.data_dir)
        try:
            assert api.store_info().missing_columns["Annotation"] == list(ANNOTATION_FIELDS_111)
        finally:
            api.close()
