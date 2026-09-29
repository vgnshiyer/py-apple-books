"""Tests for the db.query / db.clause layer.

These test the SQL-building primitives in isolation (only the NULL-safety
test runs SQL, on an in-memory database). Since 1.10,
:meth:`~py_apple_books.db.clause.Where.to_sql` and
:meth:`~py_apple_books.db.query.Query.compile` compose every
``Annotation.manager.filter(...)`` call, so a bug here cascades through
the whole API. ``str(Where)`` and ``Query.select`` keep their 1.9.1
literal form for backward compatibility.
"""

import sqlite3

import pytest

from py_apple_books.db.clause import OPERATORS, Not, Q, Subquery, Where, WhereGroup, escape_like
from py_apple_books.db.query import CompiledQuery, Query
from py_apple_books.models import Book
from py_apple_books.text import fold_for_match

COMPARISONS = ["=", "!=", "<>", "<", "<=", ">", ">=", "LIKE", "NOT LIKE", "GLOB", "NOT GLOB"]


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


class TestLegacyStr:
    """``str()`` keeps the 1.9.1 literal form (debug only, never executed);
    the one change is that embedded single quotes are doubled."""

    def test_quotes_are_doubled(self):
        assert str(Where("name", "O'Brien")) == "name = 'O''Brien'"
        assert str(Where("id", ["a'b", 2], operator="IN")) == "id IN ('a''b', 2)"
        assert Query.select("t", where=[Where("a", "it's")]) == "SELECT * FROM t WHERE a = 'it''s'"

    def test_unknown_operator_constructs_and_renders(self):
        clause = Where("x", 1, "BETWEEN")
        assert str(clause) == "x BETWEEN 1"
        with pytest.raises(ValueError, match="__gte/__lte"):
            clause.to_sql()

    def test_in_with_string_renders_raw_but_does_not_compile(self):
        clause = Where("x", "a,b", "IN")
        assert str(clause) == "x IN (a,b)"
        with pytest.raises(TypeError, match="pass a list or a Subquery"):
            clause.to_sql()
        with pytest.raises(TypeError):
            Where("x", b"a,b", "NOT IN").to_sql()

    def test_debug_str_of_new_clauses(self):
        sub = Subquery("m", "a", [Where("c", "it's")])
        assert str(sub) == "SELECT a FROM m WHERE c = 'it''s'"
        assert str(Where("x", sub, "IN")) == "x IN (SELECT a FROM m WHERE c = 'it''s')"
        group = WhereGroup([Where("a", 1), Not(Where("b", None, "IS"))], "OR")
        assert str(group) == "(a = 1 OR (b IS NULL) IS NOT 1)"
        assert Query.select("t", where=[Where("a", 1), group]) == (
            "SELECT * FROM t WHERE a = 1 AND (a = 1 OR (b IS NULL) IS NOT 1)")
        assert str(WhereGroup([])) == "1"


class TestWhereToSql:
    @pytest.mark.parametrize("op", COMPARISONS)
    def test_comparisons_bind_the_value(self, op):
        assert Where("col", "x' OR 1=1 --", op).to_sql() == (f"col {op} ?", ["x' OR 1=1 --"])

    def test_every_operator_compiles(self):
        for op in OPERATORS:
            value = [1] if "IN" in op.split() else 1
            sql, params = Where("col", value, op).to_sql()
            assert "?" in sql and params

    def test_operator_is_stripped_and_upper_cased(self):
        clause = Where("col", "a*", " not glob ")
        assert clause.operator == " not glob "
        assert clause.to_sql() == ("col NOT GLOB ?", ["a*"])

    @pytest.mark.parametrize("value", [[1, "a'b", 3], (1, "a'b", 3), frozenset({1})])
    def test_in_binds_each_item(self, value):
        sql, params = Where("id", value, "IN").to_sql()
        assert sql == f"id IN ({', '.join('?' * len(value))})"
        assert sorted(map(str, params)) == sorted(map(str, value))

    def test_empty_in_list(self):
        assert Where("id", [], "IN").to_sql() == ("id IN ()", [])
        assert Where("id", [], "NOT IN").to_sql() == ("id NOT IN ()", [])

    def test_in_subquery(self):
        sub = Subquery("ZBKCOLLECTIONMEMBER", "ZASSETID", [Where("ZCOLLECTION", 7)])
        assert sub.table == "ZBKCOLLECTIONMEMBER"
        assert sub.needed_columns() == {"ZASSETID", "ZCOLLECTION"}
        assert Where("ZASSETID", sub, "IN").to_sql() == (
            "ZASSETID IN (SELECT ZASSETID FROM ZBKCOLLECTIONMEMBER WHERE ZCOLLECTION = ?)", [7])
        assert Subquery("t", "c").to_sql() == ("SELECT c FROM t", [])
        inner = Query.compile("t", ["c"], [Where("d", 1)])
        assert Where("x", inner, "NOT IN").to_sql() == ("x NOT IN (SELECT c FROM t WHERE d = ?)", [1])

    def test_is_null_versus_is_value(self):
        assert Where("col", None, "IS").to_sql() == ("col IS NULL", [])
        assert Where("col", "NULL", "IS NOT").to_sql() == ("col IS NOT NULL", [])
        assert Where("col", 1, "IS").to_sql() == ("col IS ?", [1])
        assert Where("col", 1, "IS NOT").to_sql() == ("col IS NOT ?", [1])

    def test_contains_escapes_like_wildcards(self):
        assert escape_like("50%_a\\b") == "50\\%\\_a\\\\b"
        assert Where("col", "50%_a\\b'", "CONTAINS").to_sql() == (
            "col LIKE ? ESCAPE '\\'", ["%50\\%\\_a\\\\b'%"])

    def test_search_folds_the_needle(self):
        assert Where("col", "Don’t  PANIC", "SEARCH").to_sql() == (
            "instr(abk_fold(col), ?) > 0", [fold_for_match("Don’t  PANIC")])
        assert Where("col", "Don’t", "SEARCH").to_sql()[1] == ["don't"]

    def test_columns(self):
        assert Where("a", 1).columns() == {"a"}
        assert WhereGroup([Where("a", 1), Not(Where("b", 2))], "OR").columns() == {"a", "b"}


class TestGroups:
    def test_group_and_negation(self):
        group = WhereGroup([Where("a", 1), Where("b", 2)], "OR")
        assert group.to_sql() == ("(a = ? OR b = ?)", [1, 2])
        assert WhereGroup([Where("a", 1)], negated=True).to_sql() == ("(a = ?) IS NOT 1", [1])
        assert Not(Where("a", 0, ">")).to_sql() == ("(a > ?) IS NOT 1", [0])

    def test_empty_group(self):
        assert WhereGroup([]).to_sql() == ("1", [])
        assert WhereGroup([], negated=True).to_sql() == ("0", [])

    def test_q_precedence(self):
        """``a & (b | c)`` keeps its grouping in SQL."""
        q = Q(title="a") & (Q(genre="b") | Q(author="c"))
        sql, params = q.resolve(Book.manager).to_sql()
        assert sql == "(ZTITLE = ? AND (ZGENRE = ? OR ZAUTHOR = ?))"
        assert params == ["a", "b", "c"]
        compiled = Query.compile("ZBKLIBRARYASSET", ["Z_PK"], [q.resolve(Book.manager)])
        assert compiled.sql.endswith("WHERE (ZTITLE = ? AND (ZGENRE = ? OR ZAUTHOR = ?))")

    def test_q_flattens_and_negates(self):
        sql, params = (Q(title="a") | Q(genre="b") | Q(author="c")).resolve(Book.manager).to_sql()
        assert sql == "(ZTITLE = ? OR ZGENRE = ? OR ZAUTHOR = ?)"
        assert Q(title="a", genre="b").resolve(Book.manager).to_sql() == (
            "(ZTITLE = ? AND ZGENRE = ?)", ["a", "b"])
        assert Q(title="a").resolve(Book.manager).to_sql() == ("ZTITLE = ?", ["a"])
        assert (~Q(title="a")).resolve(Book.manager).to_sql() == ("(ZTITLE = ?) IS NOT 1", ["a"])
        assert (~(Q(title="a") | Q(genre="b"))).resolve(Book.manager).to_sql() == (
            "(ZTITLE = ? OR ZGENRE = ?) IS NOT 1", ["a", "b"])
        assert (Q() | Q(title="a")).resolve(Book.manager).to_sql() == ("ZTITLE = ?", ["a"])
        assert Q().resolve(Book.manager).to_sql() == ("1", [])

    def test_negation_is_null_safe(self):
        """``Not``/``~Q`` keep rows where the inner clause is NULL, unlike
        SQL ``NOT``."""
        con = sqlite3.connect(":memory:")
        con.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, x);"
                          "INSERT INTO t (id, x) VALUES (1, 1), (2, 0), (3, NULL)")
        run = lambda q: {r[0] for r in con.execute(q.sql, q.params)}
        assert run(Query.compile("t", ["id"], [Not(Where("x", 0, ">"))])) == {2, 3}
        assert {r[0] for r in con.execute("SELECT id FROM t WHERE NOT (x > 0)")} == {2}
        negated_or = WhereGroup([Where("x", 1), Where("id", 2)], "OR", negated=True)
        assert run(Query.compile("t", ["id"], [negated_or])) == {3}


class TestLookups:
    def test_negated_lookup_rendering(self):
        to_sql = lambda key, value: Book.manager._lookup_to_where(key, value).to_sql()
        assert to_sql("is_finished__isnot", 1) == ("ZISFINISHED IS NOT ?", [1])
        assert to_sql("is_finished__is", None) == ("ZISFINISHED IS NULL", [])
        assert to_sql("reading_progress__not_gt", 0) == ("(ZREADINGPROGRESS > ?) IS NOT 1", [0])
        assert to_sql("id__not_in", [1, 2]) == ("(Z_PK IN (?, ?)) IS NOT 1", [1, 2])
        assert to_sql("id__notin", [1, 2]) == ("Z_PK NOT IN (?, ?)", [1, 2])
        assert to_sql("title__not_search", "x") == ("(instr(abk_fold(ZTITLE), ?) > 0) IS NOT 1", ["x"])
        assert to_sql("path__isnull", True) == ("ZPATH IS NULL", [])
        assert to_sql("path__isnull", False) == ("ZPATH IS NOT NULL", [])
        assert to_sql("title", "a") == to_sql("title__exact", "a") == ("ZTITLE = ?", ["a"])


class TestCompile:
    def test_plain_and_where(self):
        assert Query.compile("t") == CompiledQuery("SELECT * FROM t", ())
        q = Query.compile("t", ["a", "b"], [Where("a", 1), Where("b", "x'y")])
        assert q == ("SELECT a, b FROM t WHERE a = ? AND b = ?", (1, "x'y"))
        assert Query.compile("t", where=[Where("a", 1), Where("b", 2)], use_or=True).sql == (
            "SELECT * FROM t WHERE a = ? OR b = ?")
        assert Query.compile("t", where=Where("a", 1)).sql == "SELECT * FROM t WHERE a = ?"

    def test_order_by(self):
        assert Query.compile("t", order_by=["a DESC", "Z_PK ASC"]).sql == (
            "SELECT * FROM t ORDER BY a DESC, Z_PK ASC")
        assert Query.compile("t", order_by="a DESC").sql == "SELECT * FROM t ORDER BY a DESC"

    def test_limit_and_offset_forms(self):
        assert Query.compile("t", limit=5) == ("SELECT * FROM t LIMIT ?", (5,))
        assert Query.compile("t", offset=10) == ("SELECT * FROM t LIMIT ? OFFSET ?", (-1, 10))
        assert Query.compile("t", limit=5, offset=10) == ("SELECT * FROM t LIMIT ? OFFSET ?", (5, 10))
        assert Query.compile("t", offset=0) == ("SELECT * FROM t", ())
        assert Query.compile("t", limit=None, offset=None) == ("SELECT * FROM t", ())

    def test_count_and_to_sql(self):
        inner = Query.compile("t", ["a"], [Where("a", 1)], limit=3)
        assert Query.count(inner) == ("SELECT COUNT(*) FROM (SELECT a FROM t WHERE a = ? LIMIT ?)", (1, 3))
        assert inner.to_sql() == (inner.sql, [1, 3])
