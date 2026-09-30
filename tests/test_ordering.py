"""``order_by`` (1.10): comma-separated and list forms, a primary-key
tie-break, and the newest-first default of the colour and annotation text
searches (F28)."""

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


@pytest.fixture
def annotations(library):
    """Yellow highlights whose ids run opposite to their dates, plus a tie."""
    book = library.add_book("Synthetic Book")
    old = library.add_annotation(book, "the oldest words", note="the note", created=day(1))
    new = library.add_annotation(book, "the newest words", note="the note", created=day(9))
    mid_a = library.add_annotation(book, "the middle words", note="the note", created=day(5))
    mid_b = library.add_annotation(book, "the middle words again", note="the note", created=day(5))
    library.add_annotation(book, None, kind="reading_position", created=day(20))
    return {"old": old, "new": new, "mid_a": mid_a, "mid_b": mid_b}


class TestSearchDefaults:
    SEARCHES = [
        lambda api, **kw: api.get_annotations_by_color("yellow", **kw),
        lambda api, **kw: api.search_annotation_by_highlighted_text("words", **kw),
        lambda api, **kw: api.search_annotation_by_note("note", **kw),
        lambda api, **kw: api.search_annotation_by_text("the", **kw),
    ]

    @pytest.mark.parametrize("search", SEARCHES)
    def test_newest_first_by_default(self, api, annotations, search):
        a = annotations
        assert ids(search(api)) == [a["new"], a["mid_a"], a["mid_b"], a["old"]]
        assert ids(search(api, limit=2)) == [a["new"], a["mid_a"]]

    @pytest.mark.parametrize("search", SEARCHES)
    def test_explicit_none_keeps_storage_order(self, api, annotations, search, sql_trace):
        assert set(ids(search(api, order_by=None))) == set(annotations.values())
        assert "ORDER BY" not in last_sql(sql_trace, "anno_db.ZAEANNOTATION")

    @pytest.mark.parametrize("search", SEARCHES)
    def test_default_order_sql(self, api, annotations, search, sql_trace):
        list(search(api, limit=3))
        assert "ORDER BY ZANNOTATIONCREATIONDATE DESC, Z_PK ASC LIMIT ?" in last_sql(
            sql_trace, "anno_db.ZAEANNOTATION")

    def test_date_range_and_list_keep_storage_order(self, api, annotations, sql_trace):
        list(api.get_annotations_by_date_range(after=dt.datetime(2026, 1, 1), limit=2))
        assert "ORDER BY" not in last_sql(sql_trace, "anno_db.ZAEANNOTATION")
        list(api.list_annotations(limit=2))
        assert "ORDER BY" not in last_sql(sql_trace, "anno_db.ZAEANNOTATION")

    def test_recent_annotations_shape(self, api, annotations):
        """MCP's ``list_annotations(limit=, order_by='-creation_date')``."""
        a = annotations
        assert ids(api.list_annotations(limit=3, order_by="-creation_date")) == [a["new"], a["mid_a"], a["mid_b"]]


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
        # The default order (last_read_date) sorts in Python since 1.10;
        # '-last_opened_date' is the 1.9 order, in SQL.
        assert ids(api.get_recently_read_books(limit=10, order_by="-last_opened_date")) == [newer, older]
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
