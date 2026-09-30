"""Lazy relations (F02): batched ``Annotation.book``, fresh to-many
iterables, the library a model was read from, pickling.

Statements are counted at ``LibraryDB.execute`` (``sql_trace``).
"""

import asyncio
import copy
import gc
import pickle
import sqlite3
import sys
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.models import Annotation, Book, Collection
from py_apple_books.models.manager import ModelIterable
from py_apple_books.models.relations import (
    BATCH_SIZE, ManyToMany, OneToMany, ReverseManyToMany, ReverseToOne,
)


@pytest.fixture
def db(lib_db):
    with use_library(lib_db):
        yield lib_db


def ids(rows) -> list:
    return [row.id for row in rows]


@pytest.fixture
def fast_switching():
    """Switch threads as often as possible, to widen race windows."""
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(interval)


class TestAnnotationBook:
    def test_one_statement_for_a_whole_result(self, db, sql_trace):
        made = db.fixture.populate(books=20, annotations_per_book=100)
        annotations = list(Annotation.manager.all())
        assert len(annotations) == 2000 and len(sql_trace) == 1
        books = [a.book for a in annotations]
        assert len(sql_trace) == 2
        assert [b.asset_id for b in books] == [a.asset_id for a in annotations]
        by_asset = {row["asset_id"]: row["id"] for row in made["books"]}
        assert all(b.id == by_asset[a.asset_id] for a, b in zip(annotations, books))
        # One Book per asset, shared by its annotations.
        assert len({id(b) for b in books}) == 20
        assert annotations[0].book is annotations[20].book
        # Resolved: cached on each model, and the sibling list released.
        assert all("book" in a.__dict__ and "_ab_siblings" not in a.__dict__ for a in annotations)
        [a.book for a in annotations]
        assert len(sql_trace) == 2
        sql, params = sql_trace[1]
        assert "ZASSETID IN (" in sql and sql.endswith("ORDER BY Z_PK ASC") and len(params) == 20

    def test_more_keys_than_sqlite_can_bind(self, db, sql_trace):
        """Keys are bound in batches, so no result can hit SQLite's limit
        on bound parameters (32,766 on current builds, 999 on old ones)."""
        count = 40_500
        made = db.fixture.populate(books=count, annotations_per_book=1)
        annotations = list(Annotation.manager.all())
        assert len(annotations) == count
        books = [a.book for a in annotations]
        assert [b.id for b in books] == [row["id"] for row in made["books"]]
        batches = sql_trace[1:]
        assert len(batches) == -(-count // BATCH_SIZE)
        assert max(len(params) for _, params in batches) == BATCH_SIZE

    def test_first_row_wins_for_a_duplicate_asset_id(self, db):
        lib = db.fixture
        first = lib.add_book("First", asset_id="DUPLICATE")
        lib.add_book("Second", asset_id="DUPLICATE")
        for _ in range(2):
            lib.add_annotation("DUPLICATE", "a synthetic highlight")
        assert {a.book.id for a in Annotation.manager.all()} == {first["id"]}
        [single] = Annotation.manager.all(limit=1)
        assert single.book.id == first["id"]

    def test_orphan_and_null_key(self, db, sql_trace):
        lib = db.fixture
        book = lib.add_book("Kept")
        kept = lib.add_annotation(book, "kept")
        orphan = lib.add_annotation("GONE-ASSET", "orphaned")
        null = lib.add_annotation(book, "no asset", raw={"ZANNOTATIONASSETID": None})

        for aid in (orphan, null):
            [annotation] = Annotation.manager.filter(id=aid)
            before = len(sql_trace)
            assert annotation.book is None
            assert len(sql_trace) - before == (1 if aid == orphan else 0)

        by_id = {a.id: a for a in Annotation.manager.all()}
        before = len(sql_trace)
        assert by_id[null].book is None and len(sql_trace) == before  # no query for a NULL key
        assert by_id[orphan].book is None and by_id[kept].book.id == book["id"]
        assert len(sql_trace) == before + 1
        _, params = sql_trace[-1]
        assert sorted(params) == sorted([book["asset_id"], "GONE-ASSET"])

    def test_a_null_key_releases_the_sibling_list(self, db):
        lib = db.fixture
        book = lib.add_book("Kept")
        lib.add_annotation(book, "kept")
        null = lib.add_annotation(book, "no asset", raw={"ZANNOTATIONASSETID": None})
        annotations = {a.id: a for a in Annotation.manager.all()}
        assert annotations[null].book is None
        assert "_ab_siblings" not in annotations[null].__dict__

    def test_getattr_default_still_works(self, db):
        """apple-books-mcp 0.8.2 reads ``getattr(anno, 'book', None)``."""
        book = db.fixture.add_book("Kept")
        db.fixture.add_annotation(book, "kept")
        db.fixture.add_annotation("GONE-ASSET", "orphaned")
        assert [getattr(a, "book", None) is None for a in Annotation.manager.all()] == [False, True]

    def test_threads_sharing_one_result(self, db, fast_switching):
        """Threads reading ``.book`` across one result at once each get
        the right books, and every sibling list is released."""
        db.fixture.populate(books=300, annotations_per_book=4)
        expected = [a.book.id for a in list(Annotation.manager.all())]
        for _ in range(3):
            annotations = list(Annotation.manager.all())
            start = threading.Barrier(8)

            def work(i):
                start.wait()
                order = annotations if i % 2 else annotations[::-1]
                return {a.id: a.book.id for a in order}

            with ThreadPoolExecutor(8) as pool:
                got = list(pool.map(work, range(8)))
            assert all([g[a.id] for a in annotations] == expected for g in got)
            assert all("_ab_siblings" not in a.__dict__ for a in annotations)

    def test_errors_propagate(self, db, monkeypatch):
        db.fixture.add_annotation(db.fixture.add_book("Kept"), "kept")
        [annotation] = Annotation.manager.all()
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(Book.manager, "_relation_iterable", boom)
        with pytest.raises(RuntimeError, match="boom"):
            annotation.book
        assert "book" not in annotation.__dict__
        monkeypatch.undo()
        assert annotation.book.title == "Kept"


class TestToMany:
    @pytest.fixture
    def book(self, db):
        lib = db.fixture
        row = lib.add_book("Annotated")
        lib.rows = {
            "live": lib.add_annotation(row, "live highlight"),
            "bookmark": lib.add_annotation(row, None, kind="bookmark"),
            "deleted": lib.add_annotation(row, "deleted highlight", deleted=True),
            "tombstone": lib.add_annotation(row, None, kind="tombstone"),
            "position": lib.add_annotation(row, None, kind="reading_position"),
        }
        return Book.manager.filter(id=row["id"])[0]

    def test_each_access_is_a_new_live_only_iterable(self, db, book, sql_trace):
        first, second = book.annotations, book.annotations
        assert isinstance(first, ModelIterable) and first is not second
        assert not sql_trace  # nothing runs until the iterable is used
        rows = db.fixture.rows
        assert sorted(ids(first)) == sorted([rows["live"], rows["bookmark"]])
        added = db.fixture.add_annotation(book.asset_id, "added")
        assert len(first) == 2 and len(book.annotations) == 3
        assert added in ids(book.annotations)
        assert "annotations" not in book.__dict__

    def test_filters_are_those_of_the_facade(self, db, book):
        assert Book.annotations.extra_filters == {"type__gt": 0, "type__ne": 3, "is_deleted__isnot": 1}
        api = PyAppleBooks()
        assert sorted(ids(book.annotations)) == sorted(
            a.id for a in api.list_annotations() if a.asset_id == book.asset_id)

    def test_collection_books_in_the_pre_110_order(self, db, sql_trace):
        """The rows (and their order) of 1.9.1's literal IN list over the
        member rows' asset ids."""
        lib = db.fixture
        books = [lib.add_book(f"Book {i}") for i in range(8)]
        shelf = lib.add_collection("Shelf")
        for i in (5, 2, 7, 0, 3):
            lib.add_to_collection(shelf, books[i])
        other = lib.add_collection("Other")
        lib.add_to_collection(other, books[1])
        collection = Collection.manager.filter(id=shelf["id"])[0]

        con = sqlite3.connect(lib.library_path)
        try:
            members = [r[0] for r in con.execute(
                f"SELECT ZASSETID FROM ZBKCOLLECTIONMEMBER WHERE ZCOLLECTION = {shelf['id']}")]
            literal = ", ".join(f"'{asset}'" for asset in members)
            expected = [r[0] for r in con.execute(
                f"SELECT Z_PK FROM ZBKLIBRARYASSET WHERE ZASSETID IN ({literal})")]
        finally:
            con.close()
        assert ids(collection.books) == expected and len(expected) == 5
        member_sql = [sql for sql, _ in sql_trace if "ZBKCOLLECTIONMEMBER" in sql]
        assert member_sql == ["SELECT Z_PK, ZASSETID, ZTITLE, ZAUTHOR, ZBOOKDESCRIPTION, ZGENRE, "
                              "ZCONTENTTYPE, ZPAGECOUNT, ZPATH, ZFILESIZE, ZISFINISHED, ZREADINGPROGRESS, "
                              "ZDURATION, ZCREATIONDATE, ZDATEFINISHED, ZLASTOPENDATE, ZPURCHASEDATE, "
                              "ZISEXPLICIT, ZISLOCKED, ZISEPHEMERAL, ZISHIDDEN, ZISSAMPLE, "
                              "ZISSTOREAUDIOBOOK, ZRATING, ZSTOREID, ZDATASOURCEIDENTIFIER, "
                              "ZCANREDOWNLOAD, ZSTATE, ZLASTENGAGEDDATE FROM ZBKLIBRARYASSET WHERE "
                              "ZASSETID IN (SELECT ZASSETID FROM ZBKCOLLECTIONMEMBER WHERE ZCOLLECTION = ?)"]

    def test_book_collections(self, db):
        lib = db.fixture
        row = lib.add_book("Shelved")
        shelves = [lib.add_collection(title, deleted=title == "Gone") for title in ("A", "B", "Gone")]
        for shelf in shelves:
            lib.add_to_collection(shelf, row)
        lib.add_collection("Unrelated")
        book = Book.manager.filter(id=row["id"])[0]
        # Unfiltered, as in 1.9.1: a deleted collection is listed too.
        assert sorted(ids(book.collections)) == sorted(s["id"] for s in shelves)
        assert book.collections is not book.collections

    def test_class_access_returns_the_relation(self):
        assert isinstance(Book.annotations, OneToMany) and Book.annotations.name == "annotations"
        assert isinstance(Collection.books, ManyToMany)
        assert isinstance(Annotation.book, ReverseToOne) and Annotation.book.name == "book"
        assert isinstance(Book.collections, ReverseManyToMany)
        assert Annotation.book.foreign_key == "asset_id" and Annotation.book.related_model is Book


def test_handle_relations_loads_eagerly(db, sql_trace):
    lib = db.fixture
    row = lib.add_book("Annotated")
    lib.add_annotation(row, "live")
    shelf = lib.add_collection("Shelf")
    lib.add_to_collection(shelf, row)
    book = Book.manager.filter(id=row["id"])[0]
    Book.manager.handle_relations(book)
    assert isinstance(book.__dict__["annotations"], ModelIterable)
    assert ids(book.annotations) == ids(book.annotations)  # the same, stored iterable
    assert ids(book.__dict__["collections"]) == [shelf["id"]]
    annotation = Annotation.manager.all()[0]
    before = len(sql_trace)
    Annotation.manager.handle_relations(annotation)
    assert annotation.__dict__["book"].id == row["id"] and len(sql_trace) == before + 1


class TestCopies:
    def test_pickle_and_deepcopy(self, db):
        lib = db.fixture
        row = lib.add_book("Annotated")
        for i in range(3):
            lib.add_annotation(row, f"highlight {i}")
        annotations = list(Annotation.manager.all())
        annotations[0].book
        book = Book.manager.filter(id=row["id"])[0]
        Book.manager.handle_relations(book)
        for obj in (annotations[0], annotations[1], book):
            for clone in (pickle.loads(pickle.dumps(obj)), copy.deepcopy(obj), copy.copy(obj)):
                assert clone == obj and clone is not obj
                assert not [key for key in clone.__dict__ if key.startswith("_ab_")]
        clone = pickle.loads(pickle.dumps(annotations[0]))
        assert clone.book == annotations[0].book  # the resolved book is data
        assert len(pickle.loads(pickle.dumps(book)).annotations) == 3  # reloads
        assert pickle.loads(pickle.dumps(annotations[2])).book.id == row["id"]

    def test_copies_while_another_thread_loads_a_sibling(self, db, fast_switching):
        """Loading ``.book`` writes into every sibling's ``__dict__``;
        pickling one of them meanwhile (``__getstate__``, which copy and
        deepcopy use too) doesn't see it change size. Before the fix,
        about one trial in four failed."""
        db.fixture.populate(books=300, annotations_per_book=4)
        for _ in range(30):
            annotations = list(Annotation.manager.all())
            start = threading.Barrier(2)

            def load():
                start.wait()
                annotations[-1].book

            loader = threading.Thread(target=load)
            loader.start()
            start.wait()
            pickled = [pickle.dumps(a) for a in annotations]
            loader.join()
            assert [pickle.loads(p).id for p in pickled] == ids(annotations)

    def test_models_built_directly(self, db):
        """A model not read from a library resolves in the current one."""
        book = db.fixture.add_book("Kept")
        # The 1.9.1 dataclass fields, positionally.
        built = Annotation(7, book["asset_id"], 0, None, None, "r", "s", None, 0, 3, 2, None, None)
        assert built.book.id == book["id"]
        assert [b.id for b in Book(*[None] * 24).annotations] == []


class TestLibraryOfTheModel:
    @pytest.fixture
    def other(self, make_library, library):
        """A data_dir library, while the default one holds different rows."""
        default_book = library.add_book("Default Book", last_opened=700000000.0)
        library.add_annotation(default_book, "default highlight")
        lib = make_library()
        row = lib.add_book("Other Book", last_opened=700000000.0)
        for i in range(3):
            lib.add_annotation(row, f"other highlight {i}")
        shelf = lib.add_collection("Other Shelf")
        lib.add_to_collection(shelf, row)
        other = LibraryDB(data_dir=lib.data_dir)
        yield other
        other.close()

    def test_relations_read_the_store_their_model_came_from(self, other):
        with use_library(other):
            [book] = Book.manager.all()
            annotations = list(Annotation.manager.all())
            [collection] = Collection.manager.all()
        # Outside use_library: the default library would give other rows.
        assert [a.selected_text for a in book.annotations] == [f"other highlight {i}" for i in range(3)]
        assert all(a.book.title == "Other Book" for a in annotations)
        assert [b.title for b in collection.books] == ["Other Book"]
        assert [c.title for c in book.collections] == ["Other Shelf"]
        assert [b.title for b in Book.manager.all()] == ["Default Book"]

    @pytest.mark.parametrize("order_by", ["-last_read_date", "last_read_date", "-last_opened_date", None])
    def test_recently_read_books(self, other, order_by):
        """The recency orders sort in Python and return a pre-1.10
        callable iterable: its models read their library too."""
        with use_library(other):
            [book] = PyAppleBooks().get_recently_read_books(order_by=order_by)
        assert book.__dict__["_ab_db"] is other
        assert [a.selected_text for a in book.annotations] == [f"other highlight {i}" for i in range(3)]
        assert [c.title for c in book.collections] == ["Other Shelf"]

    def test_callable_iterable_with_a_pre_110_from_db(self, other, monkeypatch):
        original = Book.from_db
        monkeypatch.setattr(Book, "from_db", classmethod(lambda cls, row: original(row)))
        with use_library(other):
            rows = Book.manager.all().run_query()
            [book] = ModelIterable(lambda: rows, Book)
        assert book.__dict__["_ab_db"] is other
        assert [a.selected_text for a in book.annotations] == [f"other highlight {i}" for i in range(3)]

    def test_manager_results_with_a_pre_110_from_db(self, other, monkeypatch):
        """An override without ``db=`` still builds manager results; the
        library is recorded on the models afterwards."""
        original = Book.from_db
        monkeypatch.setattr(Book, "from_db", classmethod(lambda cls, row: original(row)))
        with use_library(other):
            [book] = Book.manager.all()
            assert Book.manager.all(limit=1).first().id == book.id
        assert book.__dict__["_ab_db"] is other
        assert [a.selected_text for a in book.annotations] == [f"other highlight {i}" for i in range(3)]

    def test_an_unclosed_library_is_freed_with_its_results(self, make_library):
        """Book and collection results hold no reference cycle, so a
        dropped LibraryDB (and its idle connections) goes as soon as its
        models do, without the cyclic garbage collector."""
        lib = make_library()
        for i in range(3):
            lib.add_book(f"Book {i}")
            lib.add_collection(f"Shelf {i}")
        db = LibraryDB(data_dir=lib.data_dir)
        library = weakref.ref(db)
        gc.disable()
        try:
            with use_library(db):
                books, collections = list(Book.manager.all()), list(Collection.manager.all())
            assert books[0].__dict__["_ab_db"] is db and len(collections) == 3
            del db, books, collections
            assert library() is None
        finally:
            gc.enable()

    def test_from_worker_threads(self, other):
        with use_library(other):
            books = list(Book.manager.all())
            annotations = list(Annotation.manager.all())

        def work(i):
            if i % 2:
                return [a.id for a in books[0].annotations]
            return annotations[i % 3].book.title

        with ThreadPoolExecutor(8) as pool:
            got = list(pool.map(work, range(32)))
        assert got[0] == "Other Book" and got[1] == [a.id for a in annotations]

    def test_asyncio_to_thread(self, other):
        async def main():
            with use_library(other):
                books = await asyncio.to_thread(lambda: list(Book.manager.all()))
                count = await asyncio.to_thread(lambda: books[0].annotations.count())
            return books, count, await asyncio.to_thread(lambda: len(books[0].annotations))

        books, count, length = asyncio.run(main())
        assert [b.title for b in books] == ["Other Book"] and count == length == 3


def test_listing_scales_linearly(db):
    """The 1.9.1 N+1 path took ~2 statements per annotation; this is
    3 statements whatever the size (timing is only a loose guard)."""
    db.fixture.populate(books=50, annotations_per_book=200)
    start = time.perf_counter()
    annotations = list(PyAppleBooks().list_annotations())
    titles = {a.book.title for a in annotations}
    assert len(annotations) == 10_000 and len(titles) == 50
    assert time.perf_counter() - start < 10
