"""``offset`` paging (F03, R22, R23).

Any query with an ``offset`` (0 included) and no ``order_by`` is ordered
by primary key, and ordered queries get a primary-key tie-break, so
consecutive pages reassemble the full result without duplicates or gaps,
also when the sort key has ties and NULLs. A ``limit`` alone keeps
storage order, as before 1.10.
"""

import warnings
from decimal import Decimal
from fractions import Fraction

import pytest

from py_apple_books.db.clause import Subquery, Where
from py_apple_books.exceptions import InvalidArgumentError
from py_apple_books.models import Annotation, Book


def ids(rows) -> list:
    return [row.id for row in rows]


def pages(fetch, size: int, total: int) -> list:
    """Concatenate pages of ``size`` until one past the end."""
    out = []
    for i in range(total // size + 2):
        page = ids(fetch(limit=size, offset=i * size))
        assert len(page) <= size
        out.extend(page)
    return out


def last_query(sql_trace, table: str) -> tuple:
    """The last ``(sql, params)`` that selected from ``table`` (relation
    loading runs other statements after it)."""
    return [(sql, params) for sql, params in sql_trace if f"FROM {table} " in sql + " "][-1]


@pytest.fixture
def seeded(library):
    """3 books with 12 highlights each; creation dates have ties and NULLs."""
    made = library.populate(books=3, annotations_per_book=12)
    library.execute("annotations", "UPDATE ZAEANNOTATION SET ZANNOTATIONCREATIONDATE = NULL WHERE Z_PK % 4 = 0")
    library.execute("annotations", "UPDATE ZAEANNOTATION SET ZANNOTATIONCREATIONDATE = 700000000 WHERE Z_PK % 4 = 1")
    shelf = library.add_collection("Shelf")
    for book in made["books"][:2]:
        library.add_to_collection(shelf, book)
    for n in range(7):
        library.add_to_collection(shelf, library.add_book(f"Shelved {n}", genre=None if n % 2 else "Fiction"))
    return {"books": made["books"], "annotations": made["annotations"], "shelf": shelf["id"]}


class TestReassembly:
    @pytest.mark.parametrize("order_by", [None, "creation_date", "-creation_date", "style,-creation_date"])
    @pytest.mark.parametrize("size", [1, 5, 36])
    def test_list_annotations(self, api, seeded, order_by, size):
        full = ids(api.list_annotations(order_by=order_by, offset=0))
        assert len(full) == seeded["annotations"]
        got = pages(lambda **kw: api.list_annotations(order_by=order_by, **kw), size, len(full))
        assert got == full
        assert len(set(got)) == len(got)

    @pytest.mark.parametrize("size", [1, 5])
    def test_book_annotations_shape(self, api, seeded, size):
        asset = seeded["books"][1]["asset_id"]
        fetch = lambda **kw: Annotation.manager.filter(asset_id=asset, type__ne=3, **kw)
        full = ids(fetch(offset=0))
        assert set(full) == {a.id for a in api.get_book_by_id(seeded["books"][1]["id"]).annotations}
        assert pages(fetch, size, len(full)) == full

    @pytest.mark.parametrize("order_by", [None, "genre", "-genre,title"])
    @pytest.mark.parametrize("size", [1, 2, 4])
    def test_collection_books_shape(self, api, seeded, order_by, size):
        members = Subquery("ZBKCOLLECTIONMEMBER", "ZASSETID", [Where("ZCOLLECTION", seeded["shelf"])])
        fetch = lambda **kw: Book.manager.filter(asset_id__in=members, order_by=order_by, **kw)
        full = ids(fetch(offset=0))
        assert set(full) == {b.id for b in api.get_collection_by_id(seeded["shelf"]).books}
        assert pages(fetch, size, len(full)) == full

    def test_every_paged_facade(self, api, seeded):
        for method in (api.list_books, api.list_collections, api.get_books_in_progress,
                       api.get_finished_books, api.get_unstarted_books, api.get_recently_read_books):
            full = ids(method(limit=None, offset=0))
            assert pages(method, 2, len(full)) == full, method.__name__
        search = lambda **kw: api.search_annotation_by_text("synthetic", **kw)
        full = ids(search(limit=None, offset=0))
        assert len(full) == seeded["annotations"]
        assert pages(search, 7, len(full)) == full
        dated = lambda **kw: api.get_annotations_by_date_range(**kw)
        assert pages(dated, 10, seeded["annotations"]) == ids(dated(offset=0))


class TestSql:
    def test_offset_zero_orders_by_primary_key(self, api, seeded, sql_trace):
        list(api.list_annotations(offset=0))
        sql, _ = last_query(sql_trace, "anno_db.ZAEANNOTATION")
        assert sql.endswith("WHERE ZANNOTATIONTYPE > ? AND ZANNOTATIONTYPE != ? "
                            "AND ZANNOTATIONDELETED IS NOT ? ORDER BY Z_PK ASC")
        list(api.list_annotations(limit=3, offset=0))
        sql, _ = last_query(sql_trace, "anno_db.ZAEANNOTATION")
        assert sql.endswith("ORDER BY Z_PK ASC LIMIT ?")
        list(api.list_books(offset=4))
        sql, params = last_query(sql_trace, "ZBKLIBRARYASSET")
        assert sql.endswith("FROM ZBKLIBRARYASSET WHERE ZCONTENTTYPE IS NOT ? AND "
                            "(ZDATASOURCEIDENTIFIER IS NOT ? OR ZCANREDOWNLOAD = ?) "
                            "ORDER BY Z_PK ASC LIMIT ? OFFSET ?")
        assert tuple(params)[-2:] == (-1, 4)

    def test_limit_alone_keeps_storage_order(self, api, seeded, sql_trace):
        list(api.list_annotations(limit=3))
        sql, _ = last_query(sql_trace, "anno_db.ZAEANNOTATION")
        assert "ORDER BY" not in sql and sql.endswith("LIMIT ?")


class TestArguments:
    @pytest.mark.parametrize("offset", [-1, -(10**20)])
    def test_negative_offset(self, api, seeded, offset):
        with pytest.raises(InvalidArgumentError, match="offset must be >= 0") as exc:
            api.list_books(offset=offset)
        assert isinstance(exc.value, ValueError)

    @pytest.mark.parametrize("offset", ["1", 1.5, object()])
    def test_offset_must_be_an_integer(self, api, seeded, offset):
        with pytest.raises(InvalidArgumentError):
            api.list_books(offset=offset)

    @pytest.mark.parametrize("call", [
        lambda api, limit: api.list_books(limit=limit),
        lambda api, limit: Book.manager.all(limit=limit),
        lambda api, limit: Book.manager.filter(genre__isnull=False, limit=limit),
        lambda api, limit: api.search_annotation_by_text("synthetic", limit=limit),
        lambda api, limit: api.get_annotations_by_date_range(limit=limit),
    ])
    @pytest.mark.parametrize("limit", [0, -1])
    def test_limit_zero_or_negative_means_all_rows(self, api, seeded, call, limit):
        expected = len(call(api, None))
        with pytest.warns(DeprecationWarning, match="limit <= 0") as record:
            got = call(api, limit)
        assert len(got) == expected > 0
        assert [w.filename for w in record] == [__file__]

    @pytest.mark.parametrize("limit,expected", [("5", 5), (" 2 ", 2), (2.0, 2), (True, 1), (10**20, 10),
                                                (Decimal(3), 3), (Decimal("4.0"), 4), (Fraction(6, 2), 3)])
    def test_limit_forms(self, api, seeded, limit, expected):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert len(list(api.list_books(limit=limit))) == expected

    @pytest.mark.parametrize("limit", ["abc", "5.0", 2.5, float("nan"), float("inf"), object(), [5],
                                       Decimal("2.5"), Decimal("NaN"), 3j])
    def test_bad_limit(self, api, seeded, limit):
        with pytest.raises(InvalidArgumentError) as exc:
            api.list_books(limit=limit)
        assert isinstance(exc.value, ValueError)
