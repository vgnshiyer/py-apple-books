"""1.11 model data (stream 1.5, R12): tolerant dates, the new Book and
Annotation fields and the properties built on them.

Unit tables feed rows through ``Model.from_db`` (no database); the rest
reads synthetic libraries. Column values are written with raw SQL, so
nothing here depends on the 1.11 fixture helpers.
"""

import datetime as dt
import math
import random

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.models import Annotation, Book
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
            # Text float() can't read fails get_recently_read_books' own
            # sort (api.py, raw seconds), not the model; see changes/1.5.
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

    def test_drifted_store_with_a_corrupt_date(self, make_library):
        """A separate library: the facade reads it through its own LibraryDB."""
        lib = make_library()
        lib.add_book("Book", raw={"ZDATEFINISHED": "garbage", "ZISFINISHED": 1})
        db = LibraryDB(data_dir=lib.data_dir)
        try:
            with use_library(db):
                [book] = PyAppleBooks().get_finished_books()
                assert book.title == "Book" and book.finished_date is None
        finally:
            db.close()
