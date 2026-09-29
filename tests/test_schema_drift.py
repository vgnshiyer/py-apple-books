"""Schema drift (G3.1): queries compile against the store's columns.

A mapped column the store lacks reads as None unless its field is
required (``REQUIRED_FIELDS``); filtering or sorting on a missing column,
or a missing required column, raises ``UnsupportedSchemaError`` for that
model only. Each test drifts its own ``FixtureLibrary`` with ``ALTER
TABLE`` before reading it through a new ``LibraryDB``.
"""

import datetime as dt
import json
import pathlib
import sqlite3

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import (
    AnnotationStoreNotFoundError,
    AppleBooksError,
    DBQueryError,
    LibraryNotFoundError,
    UnsupportedSchemaError,
)
from py_apple_books.models import Annotation, Book, Collection
from py_apple_books.models.manager import REQUIRED_FIELDS
from py_apple_books.testing import STORE_SERIES

PARTIAL_2023 = pathlib.Path(__file__).parent / "fixtures" / "partial" / "2023-03_obsidian-ibook-plugin.json"
UTC = dt.timezone.utc


def seed(lib) -> dict:
    """Books (owned, Store series), a collection and annotations."""
    reading = lib.add_book("Reading Book", genre="Fiction", progress=0.4,
                           last_opened=dt.datetime(2026, 9, 1, tzinfo=UTC))
    done = lib.add_book("Done Book", genre="History", finished=True, progress=1.0,
                        last_opened=dt.datetime(2026, 8, 1, tzinfo=UTC))
    fresh = lib.add_book("Fresh Book", genre="Fiction")
    volume = lib.add_book("Series Volume", data_source=STORE_SERIES)
    owned_volume = lib.add_book("Owned Volume", data_source=STORE_SERIES, can_redownload=1)
    container = lib.add_book("Series", data_source=STORE_SERIES, content_type=5)
    shelf = lib.add_collection("Shelf")
    for book in (reading, done, container):
        lib.add_to_collection(shelf, book)
    lib.add_collection("Gone", deleted=True)
    created = dt.datetime(2026, 9, 2, tzinfo=UTC)
    rows = {
        "highlight": lib.add_annotation(reading, "a highlight", created=created, chapter="Ch 1"),
        "note": lib.add_annotation(reading, "noted text", kind="note", note="my note", color="green",
                                   created=created),
        "done_highlight": lib.add_annotation(done, "done highlight", color="blue", created=created),
        "orphan": lib.add_annotation("GONE-ASSET", "orphan highlight", created=created),
        "deleted": lib.add_annotation(reading, "deleted", deleted=True, created=created),
        "position": lib.add_annotation(reading, None, kind="reading_position", created=created),
    }
    return {"reading": reading["id"], "done": done["id"], "fresh": fresh["id"], "volume": volume["id"],
            "owned_volume": owned_volume["id"], "container": container["id"], "shelf": shelf["id"],
            **rows}


@pytest.fixture
def drifted(make_library):
    """``drifted(*alters)``: a seeded library with ``(store, sql)`` ALTERs
    applied, read through a new LibraryDB (``db.rows`` holds the ids)."""
    dbs = []

    def _make(*alters):
        lib = make_library()
        rows = seed(lib)
        for store, sql in alters:
            lib.execute(store, sql)
        db = LibraryDB(data_dir=lib.data_dir)
        db.fixture, db.rows = lib, rows
        dbs.append(db)
        return db

    yield _make
    for db in dbs:
        db.close()


def rename(table: str, column: str):
    store = "annotations" if table == "ZAEANNOTATION" else "library"
    return store, f"ALTER TABLE {table} RENAME COLUMN {column} TO {column}_GONE"


def drop(table: str, column: str):
    store = "annotations" if table == "ZAEANNOTATION" else "library"
    return store, f"ALTER TABLE {table} DROP COLUMN {column}"


def ids(rows) -> list:
    return sorted(row.id for row in rows)


def everything(api: PyAppleBooks, rows: dict) -> dict:
    """Every facade read method, as the MCP calls them, with each
    result's relations traversed: ``{name: comparable result}``."""
    def books(result):
        return [(b.id, b.title, ids(b.annotations), ids(b.collections)) for b in result]

    def annotations(result):
        return [(a.id, a.selected_text, getattr(a.book, "id", None)) for a in result]

    after = dt.datetime(2026, 1, 1)
    return {
        "list_collections": [(c.id, ids(c.books)) for c in api.list_collections()],
        "get_collection_by_id": ids(api.get_collection_by_id(rows["shelf"]).books),
        "get_collection_by_title": [c.id for c in api.get_collection_by_title("shel")],
        "list_books": books(api.list_books()),
        "list_books(all)": books(api.list_books(include_store_series=True)),
        "get_book_by_id": books([api.get_book_by_id(rows["reading"])]),
        "get_book_by_title": books(api.get_book_by_title("book")),
        "get_books_by_genre": books(api.get_books_by_genre("fic", limit=10)),
        "list_annotations": annotations(api.list_annotations(order_by="-creation_date")),
        "list_annotations(deleted)": annotations(api.list_annotations(include_deleted=True)),
        "get_annotation_by_id": annotations([api.get_annotation_by_id(rows["note"])]),
        "get_annotations_by_color": annotations(api.get_annotations_by_color("yellow")),
        "search_highlighted": annotations(api.search_annotation_by_highlighted_text("highlight")),
        "search_note": annotations(api.search_annotation_by_note("note")),
        "search_text": annotations(api.search_annotation_by_text("text")),
        "date_range": annotations(api.get_annotations_by_date_range(after=after)),
        "in_progress": books(api.get_books_in_progress(order_by="-last_opened_date")),
        "finished": books(api.get_finished_books()),
        "unstarted": books(api.get_unstarted_books()),
        "recently_read": books(api.get_recently_read_books(limit=10)),
        "recently_read(opened)": books(api.get_recently_read_books(order_by="-last_opened_date")),
        "reading_location": getattr(api.get_current_reading_location(rows["reading"]), "id", None),
        "surrounding_text": api.get_annotation_surrounding_text(rows["highlight"]),
    }


class TestOptionalColumn:
    @pytest.mark.parametrize("table, column, model, field", [
        ("ZBKLIBRARYASSET", "ZRATING", Book, "rating"),
        ("ZBKLIBRARYASSET", "ZLASTENGAGEDDATE", Book, "last_engaged_date"),
        ("ZAEANNOTATION", "ZFUTUREPROOFING5", Annotation, "chapter"),
        ("ZBKCOLLECTION", "ZDETAILS", Collection, "details"),
    ])
    def test_reads_as_none_and_rejects_filters(self, drifted, table, column, model, field):
        full = drifted()
        db = drifted(rename(table, column))
        with use_library(full):
            expected = ids(model.manager.all())
        with use_library(db):
            rows = list(model.manager.all())
            assert ids(rows) == expected and all(getattr(r, field) is None for r in rows)
            assert model.manager.all().count_by(field) == {None: len(expected)}
            assert not model.manager.has_fields(field) and model.manager.has_fields("id")
            for query in (model.manager.filter(**{field: 1}),
                          model.manager.filter(**{f"{field}__isnull": True}),
                          model.manager.all(order_by=f"-{field}")):
                for use in (list, len, lambda q: q.count(), lambda q: q.exists(), lambda q: q[0:1]):
                    with pytest.raises(UnsupportedSchemaError) as exc:
                        use(query)
                    assert (exc.value.table, exc.value.column) == (table, column)
                    assert f"no column {column} in {table} (needed for {model.__name__}.{field})" \
                        in str(exc.value)
                    assert isinstance(exc.value, DBQueryError) and isinstance(exc.value, AppleBooksError)

    def test_facade_is_unchanged_apart_from_the_field(self, drifted):
        full = drifted()
        db = drifted(rename("ZBKLIBRARYASSET", "ZRATING"), rename("ZAEANNOTATION", "ZFUTUREPROOFING5"),
                     rename("ZBKCOLLECTION", "ZDETAILS"))
        with use_library(full):
            expected = everything(PyAppleBooks(), full.rows)
        with use_library(db):
            assert everything(PyAppleBooks(), db.rows) == expected
            book = PyAppleBooks().get_book_by_id(db.rows["reading"])
            assert book.rating is None and len(list(book.annotations)) == 2

    def test_selects_null_in_its_place(self, drifted, sql_trace):
        db = drifted(rename("ZBKLIBRARYASSET", "ZRATING"))
        with use_library(db):
            list(Book.manager.all())
        assert "ZISSTOREAUDIOBOOK, NULL, ZSTOREID" in sql_trace[-1][0]


class TestRequiredColumn:
    @pytest.mark.parametrize("table, column, broken", [
        ("ZBKLIBRARYASSET", "ZASSETID", Book),
        ("ZBKLIBRARYASSET", "Z_PK", Book),
        ("ZAEANNOTATION", "ZANNOTATIONASSETID", Annotation),
        ("ZBKCOLLECTION", "Z_PK", Collection),
    ])
    def test_breaks_only_its_model(self, drifted, table, column, broken):
        db = drifted(rename(table, column))
        api = PyAppleBooks()
        with use_library(db):
            for model in (Book, Annotation, Collection):
                if model is broken:
                    with pytest.raises(UnsupportedSchemaError, match=f"no column {column} in {table}"):
                        list(model.manager.all())
                    with pytest.raises(UnsupportedSchemaError):
                        model.manager.count()
                else:
                    assert list(model.manager.all())
            if broken is not Book:
                assert api.list_books() and api.get_book_by_id(db.rows["reading"]).title == "Reading Book"
            if broken is not Annotation:
                assert list(api.list_annotations())
            if broken is not Collection:
                assert list(api.list_collections())

    def test_relations_into_the_broken_model_raise(self, drifted):
        db = drifted(rename("ZAEANNOTATION", "ZANNOTATIONASSETID"))
        with use_library(db):
            book = PyAppleBooks().get_book_by_id(db.rows["reading"])
            with pytest.raises(UnsupportedSchemaError):
                list(book.annotations)
        db = drifted(rename("ZBKLIBRARYASSET", "ZASSETID"))
        with use_library(db):
            annotations = list(PyAppleBooks().list_annotations())
            with pytest.raises(UnsupportedSchemaError):
                annotations[0].book
            with pytest.raises(UnsupportedSchemaError):
                list(PyAppleBooks().get_collection_by_id(db.rows["shelf"]).books)

    def test_registry(self):
        assert REQUIRED_FIELDS == {"Book": ("id", "asset_id"), "Annotation": ("id", "asset_id"),
                                   "Collection": ("id",)}
        for model in (Book, Annotation, Collection):
            assert model.manager.required_fields == REQUIRED_FIELDS[model.__name__]
            assert set(model.manager.required_fields) <= set(model._get_mappings(model.__name__))


class TestMemberTable:
    @pytest.mark.parametrize("column", ["ZCOLLECTION", "ZASSETID"])
    def test_breaks_only_collection_book_access(self, drifted, column):
        full = drifted()
        db = drifted(rename("ZBKCOLLECTIONMEMBER", column))
        api = PyAppleBooks()
        with use_library(full):
            books = ids(api.list_books())
            annotations = ids(api.list_annotations())
        with use_library(db):
            collection = api.get_collection_by_id(db.rows["shelf"])
            with pytest.raises(UnsupportedSchemaError, match=f"no column {column} in ZBKCOLLECTIONMEMBER"):
                list(collection.books)
            book = api.get_book_by_id(db.rows["reading"])
            with pytest.raises(UnsupportedSchemaError) as exc:
                len(book.collections)
            assert (exc.value.table, exc.value.column) == ("ZBKCOLLECTIONMEMBER", column)
            assert ids(api.list_books()) == books and ids(api.list_annotations()) == annotations
            assert [c.title for c in api.list_collections()] == ["Shelf"]
            assert all(a.book is None or a.book.title for a in api.list_annotations())
            assert ids(book.annotations)

    def test_missing_member_table(self, drifted):
        db = drifted(("library", "DROP TABLE ZBKCOLLECTIONMEMBER"))
        with use_library(db):
            collection = PyAppleBooks().get_collection_by_id(db.rows["shelf"])
            with pytest.raises(UnsupportedSchemaError, match="no ZBKCOLLECTIONMEMBER table"):
                list(collection.books)
            assert list(PyAppleBooks().list_books())


def columns_after_2023() -> list:
    """ZBKLIBRARYASSET columns of the full fixture that the 2023 DDL lacks."""
    old = set(json.loads(PARTIAL_2023.read_text())["tables"]["ZBKLIBRARYASSET"])
    from py_apple_books.testing.fixture import DEFAULT_SCHEMA, SCHEMAS_DIR
    con = sqlite3.connect(":memory:")
    try:
        con.executescript((SCHEMAS_DIR / DEFAULT_SCHEMA / "BKLibrary.sql").read_text())
        return [r[1] for r in con.execute("PRAGMA table_info(ZBKLIBRARYASSET)") if r[1] not in old]
    finally:
        con.close()


def test_2023_shape_passes_every_facade_method(drifted):
    newer = columns_after_2023()
    assert len(newer) == 16
    full = drifted()
    db = drifted(*(drop("ZBKLIBRARYASSET", column) for column in newer))
    with use_library(full):
        expected = everything(PyAppleBooks(), full.rows)
    with use_library(db):
        assert everything(PyAppleBooks(), db.rows) == expected


class TestOwnedScopeDegrades:
    """R8: a scope predicate whose column is missing is dropped, which
    shows the rows it would hide (hiding needs all the evidence)."""

    def listed(self, db) -> set:
        with use_library(db):
            return set(ids(PyAppleBooks().list_books()))

    def test_full_schema(self, drifted):
        db = drifted()
        r = db.rows
        assert self.listed(db) == {r["reading"], r["done"], r["fresh"], r["owned_volume"]}

    @pytest.mark.parametrize("column", ["ZDATASOURCEIDENTIFIER", "ZCANREDOWNLOAD"])
    def test_series_predicate_needs_both_columns(self, drifted, column):
        db = drifted(drop("ZBKLIBRARYASSET", column))
        r = db.rows
        # Every row but the container, which the ZCONTENTTYPE predicate hides.
        assert self.listed(db) == {r["reading"], r["done"], r["fresh"], r["volume"], r["owned_volume"]}
        with use_library(db):
            assert ids(PyAppleBooks().list_books(include_store_series=True)) == sorted(
                v for k, v in r.items() if k in {"reading", "done", "fresh", "volume", "owned_volume",
                                                  "container"})

    def test_without_a_container_every_row_is_listed(self, drifted):
        db = drifted(("library", "DELETE FROM ZBKLIBRARYASSET WHERE ZCONTENTTYPE = 5"),
                     drop("ZBKLIBRARYASSET", "ZDATASOURCEIDENTIFIER"))
        with use_library(db):
            assert ids(PyAppleBooks().list_books()) == ids(Book.manager.all())

    def test_container_predicate_needs_content_type(self, drifted):
        db = drifted(drop("ZBKLIBRARYASSET", "ZCONTENTTYPE"))
        r = db.rows
        # The Series predicate still applies; it hides the container too.
        assert self.listed(db) == {r["reading"], r["done"], r["fresh"], r["owned_volume"]}


class TestLiveDrift:
    def test_no_such_column_is_retried_with_a_fresh_schema(self, lib_db, sql_trace):
        """A column dropped after the schema was cached: the query fails
        with 'no such column', the schema is read again, and the retry
        reads the field as None."""
        lib = lib_db.fixture
        lib.add_book("Rated")
        lib_db._clock = lambda: 1000.0  # the cached schema never expires by age
        with use_library(lib_db):
            assert [b.rating for b in Book.manager.all()] == [0]
            lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZRATING")
            before = len(sql_trace)
            assert [b.rating for b in Book.manager.all()] == [None]
            assert Book.manager.all().count() == 1
            with pytest.raises(UnsupportedSchemaError):
                list(Book.manager.filter(rating=0))
        failed, retried = sql_trace[before:before + 2]
        assert "ZRATING" in failed[0] and "ZRATING" not in retried[0]
        assert len(sql_trace) == before + 3

    def test_a_new_column_is_seen(self, lib_db):
        lib = lib_db.fixture
        lib.add_book("Book")
        with use_library(lib_db):
            assert Book.manager.has_fields("title")
            lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET RENAME COLUMN ZRATING TO ZRATING_GONE")
            lib_db.invalidate_schema()
            assert not Book.manager.has_fields("rating")
            assert [b.rating for b in Book.manager.all()] == [None]

    def test_a_column_added_back_is_seen_at_once(self, lib_db):
        """A cached schema that lacks a column doesn't fail a query on
        it: the schema is read again before raising."""
        lib = lib_db.fixture
        lib.add_book("Book")
        lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET RENAME COLUMN ZRATING TO ZRATING_GONE")
        lib_db._clock = lambda: 1000.0  # the cached schema never expires by age
        with use_library(lib_db):
            assert not Book.manager.has_fields("rating")
            lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET RENAME COLUMN ZRATING_GONE TO ZRATING")
            assert [b.rating for b in Book.manager.filter(rating=0)] == [0]
            assert Book.manager.has_fields("rating")

    def test_an_annotation_store_found_later_is_read_at_once(self, make_library, monkeypatch):
        """The annotation store appears after the schema was cached
        without it: the first annotation query reads it."""
        from py_apple_books.db import client

        monkeypatch.setattr(client, "ANNOTATION_RETRY", 0.0)
        lib = make_library()
        book = lib.add_book("Book")
        lib.add_annotation(book, "a highlight")
        aside = lib.annotation_path.with_name("aside")
        lib.annotation_path.rename(aside)
        db = LibraryDB(data_dir=lib.data_dir)
        db._clock = lambda: 1000.0
        with use_library(db):
            assert [b.title for b in Book.manager.all()] == ["Book"]
            assert not db.has_annotations()
            aside.rename(lib.annotation_path)
            assert [a.selected_text for a in Annotation.manager.all()] == ["a highlight"]
        db.close()

    @pytest.mark.parametrize("replacement", ["no asset table", "0-byte"])
    def test_a_replaced_store_raises_a_typed_error_at_once(self, lib_db, replacement):
        """The store replaced by a non-Books file after its schema was
        cached: 'no such table' reads the schema again, so the first
        query already raises LibraryNotFoundError, not DBQueryError."""
        lib = lib_db.fixture
        lib.add_book("Book")
        clock = [1000.0]
        lib_db._clock = lambda: clock[0]
        with use_library(lib_db):
            assert len(Book.manager.all()) == 1
            new = lib.library_path.with_name("new")
            if replacement == "0-byte":
                new.touch()
            else:
                sqlite3.connect(new).execute("CREATE TABLE t (x)").connection.close()
            new.replace(lib.library_path)
            clock[0] += 1  # past the file identity recheck, not the schema's
            for _ in range(2):
                with pytest.raises(LibraryNotFoundError):
                    list(Book.manager.all())


class TestMissingStoresAndTables:
    def test_no_annotation_store(self, make_library):
        lib = make_library()
        book = lib.add_book("Book")
        lib.annotation_path.unlink()
        db = LibraryDB(data_dir=lib.data_dir)
        with use_library(db):
            assert [b.title for b in Book.manager.all()] == ["Book"]
            [obj] = Book.manager.all()
            for use in (list, len, lambda q: q.count(), lambda q: q.first()):
                with pytest.raises(AnnotationStoreNotFoundError):
                    use(obj.annotations)
            assert not Annotation.manager.has_fields("id")
            assert Book.manager.count() == 1
        db.close()
        assert book

    def test_store_without_tables(self, tmp_path):
        empty = tmp_path / "empty.sqlite"
        sqlite3.connect(empty).execute("CREATE TABLE t (x)").connection.close()
        db = LibraryDB(library_db=empty, annotation_db=empty)
        with use_library(db):
            for model, table in ((Book, "ZBKLIBRARYASSET"), (Collection, "ZBKCOLLECTION"),
                                 (Annotation, "ZAEANNOTATION")):
                with pytest.raises(LibraryNotFoundError, match=f"has no {table} table"):
                    list(model.manager.all())
            assert not Book.manager.has_fields("id")
        db.close()
