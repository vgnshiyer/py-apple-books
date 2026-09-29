"""Manager lookups against real rows, including NULLs (1.10).

``__isnot`` renders ``IS NOT ?`` and ``__not_<lookup>`` renders
``(<clause>) IS NOT 1``, so both keep rows whose column is NULL, which
``__ne`` and SQL ``NOT`` drop.
"""

import os
import pathlib
import subprocess
import sys

import pytest

from py_apple_books.db.clause import Q, Subquery, Where
from py_apple_books.exceptions import InvalidArgumentError, UnknownFieldError
from py_apple_books.models import Annotation, Book, Collection


def ids(rows) -> set:
    return {row.id for row in rows}


@pytest.fixture
def books(library):
    """Unfinished (ZISFINISHED NULL) and finished books; progress NULL, 0
    and 0.5."""
    return {
        "null": library.add_book("Null Progress", progress=None)["id"],
        "zero": library.add_book("Zero Progress", progress=0.0)["id"],
        "half": library.add_book("Half Read", progress=0.5, genre="History")["id"],
        "done": library.add_book("Done", progress=1.0, finished=True, genre="Fiction")["id"],
    }


class TestNullSafeNegation:
    def test_isnot_keeps_null_rows(self, books):
        assert ids(Book.manager.filter(is_finished__isnot=1)) == {books["null"], books["zero"], books["half"]}
        assert ids(Book.manager.filter(is_finished__ne=1)) == set()  # NULL != 1 is NULL

    def test_not_gt_keeps_null_and_zero(self, books):
        assert ids(Book.manager.filter(reading_progress__not_gt=0)) == {books["null"], books["zero"]}
        assert ids(Book.manager.filter(reading_progress__lte=0)) == {books["zero"]}

    def test_not_in_versus_notin(self, books):
        assert ids(Book.manager.filter(genre__not_in=["History"])) == {books["null"], books["zero"], books["done"]}
        assert ids(Book.manager.filter(genre__notin=["History"])) == {books["done"]}

    def test_not_q_keeps_null_rows(self, books):
        assert ids(Book.manager.filter(where=~Q(genre="History"))) == {books["null"], books["zero"], books["done"]}

    def test_is_and_isnull(self, books):
        assert ids(Book.manager.filter(is_finished__is=None)) == {books["null"], books["zero"], books["half"]}
        assert ids(Book.manager.filter(is_finished__is=1)) == {books["done"]}
        assert ids(Book.manager.filter(reading_progress__isnull=True)) == {books["null"]}
        assert ids(Book.manager.filter(reading_progress__isnull=False)) == {
            books["zero"], books["half"], books["done"]}


class TestLookups:
    def test_in_list_and_subquery(self, library, books):
        assert ids(Book.manager.filter(id__in=[books["zero"], books["done"], 999])) == {books["zero"], books["done"]}
        assert list(Book.manager.filter(id__in=[])) == []
        shelf = library.add_collection("Shelf")
        library.add_to_collection(shelf, {"id": books["half"], "asset_id": Book.manager.filter(
            id=books["half"])[0].asset_id})
        members = Subquery("ZBKCOLLECTIONMEMBER", "ZASSETID", [Where("ZCOLLECTION", shelf["id"])])
        assert ids(Book.manager.filter(asset_id__in=members)) == {books["half"]}

    def test_contains_is_literal(self, library):
        wild = library.add_book("100% snake_case")["id"]
        other = library.add_book("1000 snakescase")["id"]
        assert ids(Book.manager.filter(title__contains="0%")) == {wild}
        assert ids(Book.manager.filter(title__contains="e_c")) == {wild}
        assert ids(Book.manager.filter(title__contains="SNAKE")) == {wild, other}

    def test_contains_with_nul(self, library):
        """NUL is an ordinary character on both sides (LIKE stops reading at
        it, so '\\x00' matched every row and 'abc\\x00q' meant 'abc')."""
        nul = library.add_book("abc\x00xyz")["id"]
        plain = library.add_book("abc plain")["id"]
        assert ids(Book.manager.filter(title__contains="\x00")) == {nul}
        assert ids(Book.manager.filter(title__contains="abc\x00q")) == set()
        assert ids(Book.manager.filter(title__contains="xyz")) == {nul}
        assert ids(Book.manager.filter(title__contains="abc")) == {nul, plain}
        assert ids(Book.manager.filter(title__not_contains="\x00")) == {plain}

    def test_search_folds_both_sides(self, library):
        book = library.add_book("Gödel’s  Proof")["id"]
        for needle in ("godel's proof", "GÖDEL", "’s p", "s" + chr(0xA0) + "proof"):
            assert ids(Book.manager.filter(title__search=needle)) == {book}, needle
        assert list(Book.manager.filter(title__search="%")) == []

    def test_search_survives_a_row_that_is_not_utf8(self, api, library):
        """abk_fold gets the column's bytes, so a row with invalid UTF-8
        doesn't fail searches that don't return it."""
        good = library.add_book("Alpha")["id"]
        bad = library.add_book("Bad")["id"]
        library.execute("library", "UPDATE ZBKLIBRARYASSET SET ZTITLE = CAST(x'42616420ff' AS TEXT) "
                                   "WHERE Z_PK = ?", (bad,))
        assert ids(api.get_book_by_title("alpha")) == {good}
        assert ids(Book.manager.filter(title__search="ALPHA", genre__isnull=True)) == {good}
        assert ids(Book.manager.filter(title__contains="alpha")) == {good}

    def test_where_is_anded_with_the_keywords(self, books):
        either = Q(genre="History") | Q(genre="Fiction")
        assert ids(Book.manager.filter(where=either)) == {books["half"], books["done"]}
        assert ids(Book.manager.filter(where=either, is_finished=1)) == {books["done"]}
        # use_or joins the keywords; where= is still ANDed with them.
        got = Book.manager.filter(where=Q(genre="History"), use_or=True, id=books["half"], title="Done")
        assert ids(got) == {books["half"]}
        assert ids(Book.manager.filter(id=books["half"], title="Done", use_or=True)) == {books["half"], books["done"]}

    def test_where_rejects_other_types(self, books):
        with pytest.raises(TypeError):
            Book.manager.filter(where="ZGENRE = 'History'")

    def test_relations_use_a_subquery(self, api, library, sql_trace):
        book = library.add_book("Member")
        shelf = library.add_collection("Shelf")
        library.add_to_collection(shelf, book)
        collection = api.get_collection_by_id(shelf["id"])
        assert ids(collection.books) == {book["id"]}
        assert ids(api.get_book_by_id(book["id"]).collections) == {shelf["id"]}
        member_sql = [(sql, params) for sql, params in sql_trace if "ZBKCOLLECTIONMEMBER" in sql]
        assert member_sql and all("IN (SELECT" in sql and len(params) == 1 for sql, params in member_sql)

    def test_get_related_ids_binds(self, library):
        book = library.add_book("Member")
        shelf = library.add_collection("It's a shelf")
        library.add_to_collection(shelf, book)
        collection = Collection.manager.filter(id=shelf["id"])[0]
        relation = next(r for r in Collection.relations if r["name"] == "books")
        assert Collection.manager.get_related_ids(collection, relation) == [book["asset_id"]]


class TestUnknownFields:
    @pytest.mark.parametrize("call", [
        lambda: Book.manager.filter(nope=1),
        lambda: Book.manager.filter(nope__gt=1),
        lambda: Book.manager.filter(title__not_nope=1),
        lambda: Book.manager.filter(where=Q(nope=1)),
        lambda: Book.manager.all(order_by="-nope"),
        lambda: Book.manager.has_fields("title", "nope"),
    ])
    def test_unknown_field(self, library, call):
        with pytest.raises(UnknownFieldError) as exc:
            call()
        assert isinstance(exc.value, KeyError)
        assert isinstance(exc.value, InvalidArgumentError)
        assert exc.value.field.endswith("nope") and "title" in exc.value.valid


class TestHasFields:
    def test_mapped_columns_exist(self, library, sql_trace):
        assert Book.manager.has_fields()
        assert Book.manager.has_fields("id", "title", "genre")
        assert Annotation.manager.has_fields("note", "selected_text")
        assert Collection.manager.has_fields("is_deleted")
        assert "PRAGMA anno_db.table_info(ZAEANNOTATION)" in [sql for sql, _ in sql_trace]

    def test_dropped_column(self, make_library):
        """In a store without ZGENRE / ZANNOTATIONNOTE (read by a fresh
        process, since the library binds at import before 1.10)."""
        import py_apple_books

        lib = make_library()
        lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZGENRE")
        lib.execute("annotations", "ALTER TABLE ZAEANNOTATION DROP COLUMN ZANNOTATIONNOTE")
        tree = pathlib.Path(py_apple_books.__file__).resolve().parent.parent
        env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
        env.update(HOME=str(lib.root), PYTHONPATH=os.pathsep.join(filter(None, [str(tree), env.get("PYTHONPATH")])))
        code = ("from py_apple_books.models import Annotation, Book\n"
                "print(Book.manager.has_fields('title'), Book.manager.has_fields('title', 'genre'),\n"
                "      Annotation.manager.has_fields('selected_text'), Annotation.manager.has_fields('note'))\n")
        proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=lib.root,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert proc.stdout.split() == ["True", "False", "True", "False"]
