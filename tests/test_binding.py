"""Binding safety (R21): every read query binds its values through
``db.query.adapt_params``, and any failure inside execute is a
``DBQueryError``.

sqlite3 can't bind an int outside the 64-bit range (``OverflowError``)
or a str with a lone surrogate (``UnicodeEncodeError``). 1.9.1 wrote
values into the SQL text, where SQLite read an out-of-range int as a
REAL that matched nothing; binding keeps that result.
"""

import pytest

from py_apple_books.db import adapt_params
from py_apple_books.db.exceptions import DBError, DBQueryError
from py_apple_books.models import Book

REPLACEMENT = chr(0xFFFD)


class TestAdaptParams:
    def test_int64_bounds(self):
        assert adapt_params([2**63 - 1, -2**63]) == (2**63 - 1, -2**63)
        assert adapt_params([2**63, 10**20, -2**63 - 1]) == (
            str(2**63), str(10**20), str(-2**63 - 1))

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

    def test_huge_int_in_a_list(self, library, book):
        """The out-of-range item matches nothing; the others still match."""
        assert [b.id for b in Book.manager.filter(id__in=[book["id"], 10**20])] == [book["id"]]


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
