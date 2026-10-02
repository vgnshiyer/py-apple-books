"""Schema drift (G3.1): queries compile against the store's columns.

A mapped column the store lacks reads as None unless its field is
required (``REQUIRED_FIELDS``); filtering or sorting on a missing column,
or a missing required column, raises ``UnsupportedSchemaError`` for that
model only. Each test drifts its own ``FixtureLibrary`` with ``ALTER
TABLE`` before reading it through a new ``LibraryDB``.

The facade calls compared under drift are the drift cases in
``tests/drift_cases`` (one module per stream); every public facade
method needs a case or an exemption there.
"""

import datetime as dt
import json
import pathlib
import sqlite3

import pytest

from py_apple_books import PyAppleBooks
from tests import drift_cases
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
    base = {"reading": reading["id"], "done": done["id"], "fresh": fresh["id"], "volume": volume["id"],
            "owned_volume": owned_volume["id"], "container": container["id"], "shelf": shelf["id"],
            **rows}
    # Rows the streams' drift case modules add for their own cases.
    return drift_cases.seed(lib, base)


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
    """Every facade method, as the MCP calls it, with each result's
    relations traversed: ``{case label: comparable result}`` over the
    registered drift cases (``tests/drift_cases``)."""
    return drift_cases.everything(api, rows)


def public_methods() -> set:
    return {name for name in dir(PyAppleBooks)
            if not name.startswith("_") and callable(getattr(PyAppleBooks, name))}


class TestRegistry:
    def test_every_public_method_has_a_drift_case(self):
        """A stream adding a facade method adds its drift case (or an
        exemption with a reason) in tests/drift_cases/<stream>.py."""
        covered, exempt = set(drift_cases.covered()), set(drift_cases.exempt())
        missing = public_methods() - covered - exempt
        assert not missing, f"public PyAppleBooks methods without a drift case: {sorted(missing)}"

    def test_a_mixin_method_without_a_case_is_reported(self, monkeypatch):
        from py_apple_books._api.positions import _PositionsAPI

        monkeypatch.setattr(_PositionsAPI, "probe_without_case", lambda self: None, raising=False)
        missing = public_methods() - set(drift_cases.covered()) - set(drift_cases.exempt())
        assert missing == {"probe_without_case"}

    def test_cases_and_exemptions_name_public_methods(self):
        covered, exempt = set(drift_cases.covered()), set(drift_cases.exempt())
        assert covered <= public_methods(), sorted(covered - public_methods())
        assert exempt <= public_methods(), sorted(exempt - public_methods())
        assert not covered & exempt, sorted(covered & exempt)
        assert all(reason.strip() for _, reason in drift_cases.exempt().values())

    def test_the_registry_finds_the_modules(self):
        names = [module.__name__ for module in drift_cases.modules()]
        assert "tests.drift_cases.facade_110" in names and names == sorted(names)
        assert drift_cases.method_of("list_books(all)") == "list_books"

    def test_duplicate_labels_and_rows_are_refused(self, monkeypatch):
        from types import SimpleNamespace

        one = SimpleNamespace(__name__="one", CASES={"list_books": None}, seed=lambda lib, rows: {"x": 1})
        two = SimpleNamespace(__name__="two", CASES={"list_books": None}, EXEMPT={"close": "r"},
                              seed=lambda lib, rows: {"x": 2})
        monkeypatch.setattr(drift_cases, "modules", lambda: [one, two])
        with pytest.raises(ValueError, match="defined twice"):
            drift_cases.cases()
        with pytest.raises(ValueError, match="redefines rows"):
            drift_cases.seed(None, {})
        assert drift_cases.exempt() == {"close": ("two", "r")}

    def test_outcome(self):
        assert drift_cases.outcome(lambda: 5) == 5
        assert drift_cases.outcome(lambda: {}["k"]) == ("raised", "KeyError", "'k'")


class TestOptionalColumn:
    @pytest.mark.parametrize("table, column, model, field", [
        ("ZBKLIBRARYASSET", "ZRATING", Book, "rating"),
        ("ZBKLIBRARYASSET", "ZLASTENGAGEDDATE", Book, "last_engaged_date"),
        # 1.11
        ("ZBKLIBRARYASSET", "ZLANGUAGE", Book, "language"),
        ("ZBKLIBRARYASSET", "ZYEAR", Book, "year"),
        ("ZBKLIBRARYASSET", "ZRELEASEDATE", Book, "release_date"),
        ("ZBKLIBRARYASSET", "ZSERIESID", Book, "series_id"),
        ("ZBKLIBRARYASSET", "ZSERIESCONTAINER", Book, "series_container_id"),
        ("ZBKLIBRARYASSET", "ZSEQUENCENUMBER", Book, "series_sequence"),
        ("ZBKLIBRARYASSET", "ZSEQUENCEDISPLAYNAME", Book, "series_label"),
        ("ZBKLIBRARYASSET", "ZSERIESISORDERED", Book, "series_is_ordered"),
        ("ZBKLIBRARYASSET", "ZBOOKHIGHWATERMARKPROGRESS", Book, "high_water_progress"),
        ("ZAEANNOTATION", "ZFUTUREPROOFING5", Annotation, "chapter"),
        ("ZAEANNOTATION", "ZPLUSERDATA", Annotation, "location_data"),
        ("ZAEANNOTATION", "ZFUTUREPROOFING10", Annotation, "position_fraction"),
        ("ZAEANNOTATION", "ZFUTUREPROOFING8", Annotation, "furthest_fraction"),
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
                with pytest.raises(LibraryNotFoundError) as exc:
                    list(Book.manager.all())
                # One error: the retry runs outside the first one's handler.
                assert exc.value.__context__ is None


class TestMissingStoresAndTables:
    @pytest.fixture
    def invalidations(self, monkeypatch):
        calls = []
        original = LibraryDB.invalidate_schema

        def counted(self):
            calls.append(self)
            original(self)

        monkeypatch.setattr(LibraryDB, "invalidate_schema", counted)
        return calls

    def test_a_missing_store_is_not_retried(self, tmp_path, invalidations):
        """Store discovery (or a file that isn't a database) doesn't
        depend on the cached schema, so it isn't looked for twice."""
        garbage = tmp_path / "garbage.sqlite"
        garbage.write_bytes(b"not a database" * 100)
        for db in (LibraryDB(data_dir=tmp_path / "nothing-here"),
                   LibraryDB(library_db=garbage, annotation_db=garbage)):
            with use_library(db), pytest.raises(LibraryNotFoundError) as exc:
                list(Book.manager.all())
            assert not isinstance(exc.value.__context__, LibraryNotFoundError)
            db.close()
        assert invalidations == []

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
                with pytest.raises(LibraryNotFoundError, match=f"has no {table} table") as exc:
                    list(model.manager.all())
                assert exc.value.__context__ is None
            assert not Book.manager.has_fields("id")
        db.close()
