"""Binding safety (R21): every read query binds its values through
``db.query.adapt_params``, and any failure inside execute is a
``DBQueryError``.

sqlite3 can't bind an int outside the 64-bit range (``OverflowError``)
or a str with a lone surrogate (``UnicodeEncodeError``). 1.9.1 wrote
values into the SQL text, where SQLite read an out-of-range int as a
REAL that matched nothing; binding keeps that result.
"""

import datetime
import sqlite3
import sys
import warnings
from decimal import Decimal
from fractions import Fraction

import pytest

from py_apple_books.db import adapt_params
from py_apple_books.db.exceptions import DBError, DBQueryError
from py_apple_books.models import Annotation, Book

REPLACEMENT = chr(0xFFFD)


class Index:
    """An integer type sqlite3 can't bind, like numpy.int64."""

    def __init__(self, value):
        self.value = value

    def __index__(self):
        return self.value


def variable_limit() -> int:
    """SQLite's bound-variable limit (a compile-time setting)."""
    con = sqlite3.connect(":memory:")
    try:
        if hasattr(con, "getlimit"):  # Python 3.11+
            return con.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        for limit in (999, 32766, 250000):  # the usual builds
            try:
                con.execute(f"SELECT 1 WHERE 1 IN ({', '.join('?' * (limit + 1))})", [0] * (limit + 1))
            except sqlite3.OperationalError as e:
                if "too many SQL variables" in str(e):
                    return limit
                raise
        pytest.skip("unknown bound-variable limit")
    finally:
        con.close()


class TestAdaptParams:
    def test_int64_bounds(self):
        assert adapt_params([2**63 - 1, -2**63]) == (2**63 - 1, -2**63)
        assert adapt_params([2**63, 10**20, -2**63 - 1]) == (
            str(2**63), str(10**20), str(-2**63 - 1))

    @pytest.mark.skipif(not hasattr(sys, "get_int_max_str_digits"), reason="no int/str digit limit")
    def test_int_too_long_for_str(self):
        """str() raises ValueError beyond sys.get_int_max_str_digits();
        the text SQLite reads as the REAL +-infinity a long literal was."""
        huge = 10**(sys.get_int_max_str_digits() + 1)
        assert adapt_params([huge, -huge, Decimal("1E+5000")]) == ("9e999", "-9e999", "9e999")

    @pytest.mark.parametrize("value", [datetime.date(2000, 1, 1), datetime.datetime(2000, 1, 1)])
    def test_date_is_a_type_error(self, value):
        """sqlite3's deprecated default adapter would bind ISO text, which
        compares greater than every Core Data timestamp."""
        with pytest.raises(TypeError, match="Core Data seconds"):
            adapt_params([1, value])

    def test_bool_becomes_int(self):
        got = adapt_params([True, False])
        assert got == (1, 0) and all(type(v) is int for v in got)

    def test_lone_surrogate_becomes_replacement_character(self):
        got = adapt_params(["a" + chr(0xD800) + "b", chr(0xDFFF)])
        for value in got:
            value.encode("utf-8")
            assert REPLACEMENT in value
        assert got[0].startswith("a") and got[0].endswith("b")
        assert got[1].strip(REPLACEMENT) == ""

    def test_other_values_pass_through(self):
        values = [None, 1.5, "don’t", b"\x00", 7]
        assert adapt_params(values) == tuple(values)
        assert adapt_params(()) == ()

    def test_numbers_sqlite3_cannot_bind(self):
        """Pre-1.10 wrote str(value) into the SQL: Decimal('1') and an
        __index__ type such as numpy.int64 matched id 1."""
        got = adapt_params([Index(1), Index(2**64), Decimal("1"), Decimal("2.50"), Fraction(1, 4),
                            Decimal("1E+30")])
        assert got == (1, str(2**64), 1, 2.5, 0.25, str(10**30))
        assert [type(v) for v in got] == [int, str, int, float, float, str]
        nan = Decimal("NaN")
        assert adapt_params([nan, 1j])[0] is nan  # left for sqlite3 to reject

    def test_client_binds_through_it(self):
        client = Book.manager.compiler.client
        [(huge, flag, text)] = client.execute("SELECT ?, ?, ?", [10**20, True, "x" + chr(0xD800)])
        assert (huge, flag) == (str(10**20), 1)
        assert text.startswith("x" + REPLACEMENT)


class TestEndToEnd:
    @pytest.fixture
    def book(self, library):
        return library.add_book("Synthetic Book")

    @pytest.mark.parametrize("bid", [2**63, 10**20, -2**63 - 1, str(10**20)])
    def test_huge_book_id_is_not_found(self, api, book, bid):
        with pytest.raises(IndexError):
            api.get_book_by_id(bid)

    def test_huge_annotation_id_is_not_found(self, api, library, book):
        library.add_annotation(book, "a synthetic highlight")
        with pytest.raises(IndexError):
            api.get_annotation_by_id(10**20)

    def test_huge_limit_means_all_rows(self, api, library, book):
        library.add_book("Second Book")
        assert len(list(api.list_books(limit=10**20))) == 2
        assert len(list(api.list_books(limit=2**63))) == 2
        assert len(list(api.list_books(limit=2**63 - 1))) == 2

    def test_huge_offset_is_past_the_end(self, api, book):
        assert list(api.list_books(offset=10**20)) == []
        assert list(api.list_books(limit=1, offset=2**63 - 1)) == []

    def test_lone_surrogate_search_finds_nothing(self, api, library, book):
        library.add_annotation(book, "a synthetic highlight")
        assert api.search_annotation_by_text("a" + chr(0xD800)) == []
        assert list(api.get_book_by_title(chr(0xDFFF))) == []

    @pytest.mark.parametrize("make_id", [Index, Decimal, lambda pk: Decimal(f"{pk}.0")])
    def test_id_of_a_number_type_sqlite3_cannot_bind(self, api, library, book, make_id):
        assert api.get_book_by_id(make_id(book["id"])).id == book["id"]
        assert [b.id for b in Book.manager.filter(id__in=[make_id(book["id"]), Index(10**20)])] == [book["id"]]
        library.add_annotation(book, "a synthetic highlight")
        annotation = api.list_annotations()[0]
        assert api.get_annotation_by_id(make_id(annotation.id)).id == annotation.id

    def test_huge_int_in_a_list(self, library, book):
        """The out-of-range item matches nothing; the others still match."""
        assert [b.id for b in Book.manager.filter(id__in=[book["id"], 10**20])] == [book["id"]]

    @pytest.mark.skipif(not hasattr(sys, "get_int_max_str_digits"), reason="no int/str digit limit")
    def test_int_too_long_for_str(self, api, library, book):
        """Not found (1.9.1 raised ValueError from str()); it compares as
        infinity, like the shorter out-of-range ints."""
        huge = 10**(sys.get_int_max_str_digits() + 1)
        with pytest.raises(IndexError):
            api.get_book_by_id(huge)
        with pytest.raises(IndexError):
            api.get_annotation_by_id(-huge)
        assert [b.id for b in Book.manager.filter(id__lt=huge)] == [book["id"]]
        assert list(Book.manager.filter(id__gt=huge)) == []
        assert list(api.get_book_by_title(huge)) == []

    @pytest.mark.parametrize("value", [datetime.date(2000, 1, 1), datetime.datetime(2000, 1, 1)])
    def test_date_filter_is_a_db_query_error(self, library, book, value):
        """A date or datetime is rejected, not bound as ISO text that
        compares greater than every timestamp (1.9.1 wrote it into the
        SQL: a syntax error for a datetime, arithmetic for a date)."""
        library.add_annotation(book, "a synthetic highlight", created=1000.0)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # no sqlite3 default-adapter DeprecationWarning
            with pytest.raises(DBQueryError, match="Core Data seconds") as exc:
                list(Annotation.manager.filter(creation_date__lte=value))
        assert isinstance(exc.value.__cause__, TypeError)

    def test_in_list_beyond_the_variable_limit(self, book):
        """__in binds one parameter per item: a list longer than SQLite's
        bound-variable limit is a DBQueryError (pass a Subquery instead)."""
        limit = variable_limit()
        with pytest.raises(DBQueryError, match="too many SQL variables"):
            list(Book.manager.filter(id__in=list(range(limit + 1))))
        assert [b.id for b in Book.manager.filter(id__in=list(range(limit)))] == [book["id"]]


def test_non_sqlite_error_in_execute_is_db_query_error(api, library, monkeypatch):
    library.add_book("Synthetic Book")
    client = Book.manager.compiler.client

    class FailingCursor:
        def execute(self, *args):
            raise RuntimeError("boom")

    monkeypatch.setattr(client, "cursor", FailingCursor())
    with pytest.raises(DBQueryError, match="Unexpected error while executing query: boom") as exc:
        list(api.list_books())
    assert isinstance(exc.value, DBError)  # what 1.9.1 raised here
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_sqlite_error_is_db_query_error():
    client = Book.manager.compiler.client
    with pytest.raises(DBQueryError, match="Error executing query"):
        client.execute("SELECT * FROM no_such_table")
    with pytest.raises(DBQueryError, match="Error executing query"):
        client.execute("SELECT ?", ())
