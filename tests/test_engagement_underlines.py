"""``get_underlines`` (1.11): type-2 rows Books marks as underlined.
Synthetic data only."""

import datetime as dt
import inspect

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import InvalidArgumentError, InvalidChoiceError, UnsupportedSchemaError
from py_apple_books.testing import seed_demo

UTC = dt.timezone.utc


@pytest.fixture
def demo(library, tmp_path):
    return seed_demo(library, tmp_path)


@pytest.fixture
def underlines(library):
    book = library.add_book("Underlined")
    rows = {f"u{i}": library.add_annotation(book, f"word{i}", kind="underline",
                                            created=dt.datetime(2026, 1, 1 + i, tzinfo=UTC))
            for i in range(5)}
    rows["gone"] = library.add_annotation(book, "gone", kind="underline", deleted=True,
                                          created=dt.datetime(2026, 2, 1, tzinfo=UTC))
    rows["highlight"] = library.add_annotation(book, "a yellow highlight")
    rows["bookmark"] = library.add_annotation(book, None, kind="bookmark")
    rows["position"] = library.add_annotation(book, None, kind="reading_position")
    return rows


def test_demo_underline_only(api, demo):
    found = list(api.get_underlines())
    assert [a.id for a in found] == [demo["annotations"]["underline"]]
    assert found[0].is_underline and found[0].type == 2


def test_bookmarks_and_positions_are_not_underlines(api, underlines):
    ids = {a.id for a in api.get_underlines()}
    assert ids == {underlines[f"u{i}"] for i in range(5)}


def test_the_flag_decides_when_the_column_exists(api, library):
    # With ZANNOTATIONISUNDERLINE present, the flag alone decides: a
    # flagged row with a color style is an underline, and an unflagged
    # style-0 row is not (style 0 is only the fallback without the flag).
    book = library.add_book("Flagged")
    flagged = library.add_annotation(book, "flagged", raw={"ZANNOTATIONISUNDERLINE": 1, "ZANNOTATIONSTYLE": 3})
    library.add_annotation(book, "style zero", raw={"ZANNOTATIONISUNDERLINE": 0, "ZANNOTATIONSTYLE": 0})
    assert [a.id for a in api.get_underlines()] == [flagged]


def test_deleted_with_include_deleted(api, underlines):
    assert underlines["gone"] in {a.id for a in api.get_underlines(include_deleted=True)}
    assert underlines["gone"] not in {a.id for a in api.get_underlines()}


def test_newest_first_and_storage_order(api, underlines):
    assert [a.id for a in api.get_underlines()] == [underlines[f"u{i}"] for i in reversed(range(5))]
    assert sorted(a.id for a in api.get_underlines(order_by=None)) == [underlines[f"u{i}"] for i in range(5)]
    assert [a.id for a in api.get_underlines(order_by="creation_date")] == [underlines[f"u{i}"] for i in range(5)]


def test_count_and_pages(api, underlines):
    result = api.get_underlines()
    assert result.count() == len(result) == 5
    pages = [a.id for o in (0, 2, 4) for a in api.get_underlines(2, offset=o)]
    assert pages == [a.id for a in api.get_underlines()]


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": True}, {"limit": "2"}, {"offset": -1}])
def test_strict_limits(api, kwargs):
    with pytest.raises(InvalidArgumentError):
        api.get_underlines(**kwargs)


def test_signature():
    params = inspect.signature(PyAppleBooks.get_underlines).parameters
    assert list(params) == ["self", "limit", "order_by", "offset", "include_deleted"]
    assert params["order_by"].default == "-creation_date"
    assert params["offset"].kind is inspect.Parameter.KEYWORD_ONLY


def test_color_message_is_unchanged(api):
    with pytest.raises(InvalidChoiceError) as exc:
        api.get_annotations_by_color("underline")
    assert str(exc.value) == ("Unknown highlight color 'underline'. Valid colors: green, blue, yellow, "
                              "pink, purple.")


class TestDrift:
    def seeded(self, make_library, *drops):
        lib = make_library()
        book = lib.add_book("B")
        ids = [lib.add_annotation(book, "u", kind="underline"), lib.add_annotation(book, "h"),
               lib.add_annotation(book, None, kind="bookmark")]
        for column in drops:
            lib.execute("annotations", f"ALTER TABLE ZAEANNOTATION DROP COLUMN {column}")
        return lib, ids

    def test_style_zero_without_the_flag(self, make_library):
        lib, ids = self.seeded(make_library, "ZANNOTATIONISUNDERLINE")
        with LibraryDB(data_dir=lib.data_dir) as db, use_library(db):
            assert [a.id for a in PyAppleBooks().get_underlines()] == [ids[0]]

    def test_neither_column(self, make_library):
        lib, _ = self.seeded(make_library, "ZANNOTATIONISUNDERLINE", "ZANNOTATIONSTYLE")
        with LibraryDB(data_dir=lib.data_dir) as db, use_library(db):
            with pytest.raises(UnsupportedSchemaError, match="ZANNOTATIONSTYLE"):
                list(PyAppleBooks().get_underlines())

    def test_default_order_needs_the_creation_date(self, make_library):
        lib, ids = self.seeded(make_library, "ZANNOTATIONCREATIONDATE")
        with LibraryDB(data_dir=lib.data_dir) as db, use_library(db):
            api = PyAppleBooks()
            with pytest.raises(UnsupportedSchemaError, match="ZANNOTATIONCREATIONDATE"):
                list(api.get_underlines())
            assert [a.id for a in api.get_underlines(order_by="id")] == [ids[0]]
