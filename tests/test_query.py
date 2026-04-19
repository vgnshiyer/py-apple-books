"""Tests for the db.query / db.clause layer.

These test the SQL-building primitives in isolation — no database
connection, no filesystem. The :class:`~py_apple_books.db.clause.Where`
renderer and :class:`~py_apple_books.db.query.Query.select` compose
every ``Annotation.manager.filter(...)`` call, so a bug here cascades
through the whole API.
"""

from py_apple_books.db.clause import Where
from py_apple_books.db.query import Query


class TestWhere:
    def test_equality_default_operator(self):
        assert str(Where("field", 5)) == "field = 5"

    def test_string_value_is_quoted(self):
        assert str(Where("name", "foo")) == "name = 'foo'"

    def test_not_equal_operator(self):
        """Regression: ``__ne`` is the key mechanism used to exclude
        Apple Books' auto-tracked reading-bookmark annotations from
        user-facing queries."""
        assert str(Where("type", 3, operator="!=")) == "type != 3"

    def test_greater_than(self):
        assert str(Where("progress", 0, operator=">")) == "progress > 0"

    def test_in_with_list(self):
        assert str(Where("id", [1, 2, 3], operator="IN")) == "id IN (1, 2, 3)"

    def test_is_null(self):
        """Regression for pre-v1.7.1: ``IS NULL`` must emit the SQL
        keyword, not a quoted string literal."""
        assert str(Where("col", "NULL", operator="IS")) == "col IS NULL"
        assert str(Where("col", None, operator="IS")) == "col IS NULL"
        assert str(Where("col", "NULL", operator="IS NOT")) == "col IS NOT NULL"


class TestSelect:
    def test_plain_select(self):
        q = Query.select("t")
        assert q == "SELECT * FROM t"

    def test_fields_as_list(self):
        q = Query.select("t", fields=["a", "b"])
        assert q == "SELECT a, b FROM t"

    def test_where_and_by_default(self):
        q = Query.select("t", where=[Where("a", 1), Where("b", 2)])
        assert q == "SELECT * FROM t WHERE a = 1 AND b = 2"

    def test_where_or_when_requested(self):
        q = Query.select("t", where=[Where("a", 1), Where("b", 2)], use_or=True)
        assert q == "SELECT * FROM t WHERE a = 1 OR b = 2"

    def test_limit_and_order_by(self):
        q = Query.select("t", order_by="col DESC", limit=10)
        assert q == "SELECT * FROM t ORDER BY col DESC LIMIT 10"

    def test_bookmark_exclusion_renders_correctly(self):
        """The exact shape of the WHERE clause produced for a
        user-annotation facade call like
        ``Annotation.manager.filter(type__ne=3, limit=10)``."""
        q = Query.select(
            "anno_db.ZAEANNOTATION",
            where=[Where("ZANNOTATIONTYPE", 3, operator="!=")],
            limit=10,
        )
        assert q == (
            "SELECT * FROM anno_db.ZAEANNOTATION "
            "WHERE ZANNOTATIONTYPE != 3 "
            "LIMIT 10"
        )
