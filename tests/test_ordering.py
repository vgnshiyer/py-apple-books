"""``order_by`` (1.10): comma-separated and list forms, and a primary-key
tie-break."""

import datetime as dt

import pytest

from py_apple_books.exceptions import UnknownFieldError
from py_apple_books.models import Annotation, Book

UTC = dt.timezone.utc


def day(n: int) -> dt.datetime:
    return dt.datetime(2026, 9, n, 12, 0, tzinfo=UTC)


def ids(rows) -> list:
    return [row.id for row in rows]


def last_sql(sql_trace, table: str) -> str:
    return [sql for sql, _ in sql_trace if f"FROM {table}" in sql][-1]


class TestForms:
    @pytest.fixture
    def books(self, library):
        return [library.add_book(title, genre=genre)["id"] for title, genre in [
            ("B", "History"), ("A", "Fiction"), ("C", "History"), ("A", "History"), ("B", None),
            ("A", "History")]]

    @pytest.mark.parametrize("order_by", ["genre,-title", " genre , -title ", ["genre", "-title"],
                                          ("genre", "-title"), ["genre,-title"]])
    def test_multiple_terms(self, books, sql_trace, order_by):
        got = Book.manager.all(order_by=order_by)
        rows = [(b.genre, b.title, b.id) for b in got]
        assert "ORDER BY ZGENRE ASC, ZTITLE DESC, Z_PK ASC" in last_sql(sql_trace, "ZBKLIBRARYASSET")
        # NULL sorts first; the two (History, A) rows are ordered by id.
        expected = sorted(rows, key=lambda r: (r[0] is not None, r[0] or "", [-ord(c) for c in r[1]], r[2]))
        assert rows == expected

    def test_tie_break_only_when_the_key_is_not_already_there(self, books, sql_trace):
        list(Book.manager.all(order_by="-id"))
        assert last_sql(sql_trace, "ZBKLIBRARYASSET").endswith("ORDER BY Z_PK DESC")
        list(Book.manager.all(order_by="title"))
        assert last_sql(sql_trace, "ZBKLIBRARYASSET").endswith("ORDER BY ZTITLE ASC, Z_PK ASC")

    def test_ties_come_back_by_id(self, api, books):
        titles = [(b.title, b.id) for b in api.list_books(order_by="title")]
        assert titles == sorted(titles)

    def test_mcp_currently_reading_shape(self, api, library, sql_trace):
        older = library.add_book("Older", progress=0.2, last_opened=day(1))["id"]
        newer = library.add_book("Newer", progress=0.3, last_opened=day(2))["id"]
        assert ids(api.get_books_in_progress(limit=1, order_by="-last_opened_date")) == [newer]
        assert ids(api.get_recently_read_books(limit=10)) == [newer, older]
        assert "ORDER BY ZLASTOPENDATE DESC, Z_PK ASC" in last_sql(sql_trace, "ZBKLIBRARYASSET")

    @pytest.mark.parametrize("order_by", ["nope", "-nope", "title,nope", ["title", "-nope"], "+title",
                                          "ZTITLE", "title; DROP TABLE x"])
    def test_unknown_field(self, api, books, order_by):
        with pytest.raises(UnknownFieldError) as exc:
            api.list_books(order_by=order_by)
        assert isinstance(exc.value, KeyError)
        with pytest.raises(UnknownFieldError):
            Annotation.manager.filter(type__ne=3, order_by=order_by)

    @pytest.mark.parametrize("order_by", [None, "", [], ","])
    def test_no_order(self, books, sql_trace, order_by):
        assert len(list(Book.manager.all(order_by=order_by))) == len(books)
        assert "ORDER BY" not in last_sql(sql_trace, "ZBKLIBRARYASSET")
