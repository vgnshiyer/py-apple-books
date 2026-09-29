"""ModelIterable and ModelManager (F50, R23): evaluate-once results,
slicing, first/exists, ``only=``, the pre-1.10 constructor, and the
manager's replaceable compiler.

Each test reads its own ``FixtureLibrary`` through a ``LibraryDB``
under ``use_library``; statements are counted at ``LibraryDB.execute``.
"""

import pytest

from py_apple_books.db import AppleBooksDBClient, LibraryDB, QueryCompiler, use_library
from py_apple_books.exceptions import LibraryNotFoundError, UnknownFieldError
from py_apple_books.models import Annotation, Book, Collection
from py_apple_books.models.manager import ModelIterable


@pytest.fixture
def db(lib_db):
    lib = lib_db.fixture
    # Titles run against primary-key order, so a title order is visibly
    # not storage order.
    lib.books = [lib.add_book(f"Book {9 - i}", genre=("Fiction", "History")[i % 2],
                              progress=(0.0, 0.5)[i % 2]) for i in range(10)]
    with use_library(lib_db):
        yield lib_db


def ids(rows) -> list:
    return [row.id for row in rows]


@pytest.fixture
def from_db_calls(monkeypatch):
    calls = []
    original = Book.from_db.__func__

    def spy(cls, row, db=None):
        calls.append(row[0])
        return original(cls, row, db=db)

    monkeypatch.setattr(Book, "from_db", classmethod(spy))
    return calls


class TestEvaluateOnce:
    def test_one_statement_for_every_use(self, db, sql_trace):
        books = Book.manager.all()
        assert len(books) == 10 and bool(books)
        first = list(books)
        assert list(books) == first and [b for b in books] == first
        assert books[0] is first[0] and books[-1] is first[-1]
        assert books[2:5] == first[2:5] and books[::3] == first[::3]
        assert books.count() == 10 and books.exists() and books.first() is first[0]
        assert len(sql_trace) == 1

    def test_len_builds_no_models(self, db, sql_trace, from_db_calls):
        books = Book.manager.all()
        assert len(books) == 10 and bool(books)
        assert from_db_calls == []
        list(books)
        assert len(from_db_calls) == 10 and len(sql_trace) == 1

    def test_models_are_built_once(self, db, from_db_calls):
        books = Book.manager.all()
        list(books), list(books), books[3]
        assert len(from_db_calls) == 10

    def test_each_call_is_a_new_query(self, db, sql_trace):
        books = Book.manager.all()
        list(books)
        db.fixture.add_book("Late Book")
        assert len(list(books)) == 10  # evaluated: a snapshot
        assert len(list(Book.manager.all())) == 11
        assert len(sql_trace) == 2

    def test_empty(self, db):
        none = Book.manager.filter(title="nope")
        assert len(none) == 0 and not none and list(none) == []
        with pytest.raises(IndexError):
            none[0]
        with pytest.raises(IndexError):
            Book.manager.filter(title="nope")[0]


class TestSlicing:
    @pytest.mark.parametrize("order_by", [None, "title", "-genre,title"])
    @pytest.mark.parametrize("s", [slice(2, 5), slice(4, None), slice(None, 3), slice(-2, None),
                                   slice(None, None, 2), slice(3, 3), slice(7, 2), slice(8, 50),
                                   slice(None, -3), slice(1, None, 1)])
    def test_slice_equals_the_list_slice(self, db, order_by, s):
        # Unordered, the list is in storage order, which is primary-key
        # order here, like an unordered slice.
        everything = list(Book.manager.all(order_by=order_by))
        assert ids(Book.manager.all(order_by=order_by)[s]) == ids(everything)[s]

    def test_unevaluated_slice_is_one_query(self, db, sql_trace):
        books = Book.manager.all()
        page = books[2:5]
        assert isinstance(page, list) and ids(page) == [b["id"] for b in db.fixture.books[2:5]]
        assert len(sql_trace) == 1
        sql, params = sql_trace[0]
        assert sql.endswith("ORDER BY Z_PK ASC LIMIT ? OFFSET ?") and params[-2:] == (3, 2)
        assert repr(books) == "<ModelIterable[Book]: unevaluated>"

    def test_ordered_slice_keeps_its_order(self, db, sql_trace):
        page = Book.manager.all(order_by="title")[0:3]
        assert [b.title for b in page] == ["Book 0", "Book 1", "Book 2"]
        assert "ORDER BY ZTITLE ASC, Z_PK ASC LIMIT ?" in sql_trace[0][0]

    def test_open_slice_without_limit(self, db, sql_trace):
        assert len(Book.manager.all()[3:]) == 7
        sql, params = sql_trace[0]
        assert sql.endswith("LIMIT ? OFFSET ?") and params[-2:] == (-1, 3)

    def test_negative_or_stepped_slice_reads_everything(self, db, sql_trace):
        assert ids(Book.manager.all()[-2:]) == [b["id"] for b in db.fixture.books[-2:]]
        assert ids(Book.manager.all()[::4]) == [b["id"] for b in db.fixture.books[::4]]
        assert all("LIMIT" not in sql for sql, _ in sql_trace)

    def test_slice_composes_with_limit_and_offset(self, db):
        everything = ids(Book.manager.all(order_by="id"))
        assert ids(Book.manager.all(limit=6)[2:]) == everything[2:6]
        assert ids(Book.manager.all(limit=6)[1:3]) == everything[1:3]
        assert ids(Book.manager.all(limit=6)[4:9]) == everything[4:6]
        assert ids(Book.manager.all(offset=3)[1:3]) == everything[4:6]
        assert ids(Book.manager.all(offset=3, limit=4)[2:10]) == everything[5:7]
        assert Book.manager.all(limit=2)[5:] == []

    def test_huge_bounds(self, db):
        assert len(Book.manager.all()[0:10**20]) == 10
        assert Book.manager.all()[10**20:] == []

    def test_slice_bounds_must_be_integers(self, db):
        with pytest.raises(TypeError):
            Book.manager.all()["a":]

    @pytest.mark.parametrize("size", [1, 3, 4, 10])
    def test_paging_a_collection_and_a_book(self, db, size):
        """Consecutive unordered slices page through a relation without
        gaps or repeats (R23), each page one query on a new iterable."""
        lib = db.fixture
        shelf = lib.add_collection("Shelf")
        for book in reversed(lib.books):
            lib.add_to_collection(shelf, book)
        book = lib.books[0]
        for i in range(11):
            lib.add_annotation(book, f"highlight {i}", created=1000.0 - i)
        collection = Collection.manager.filter(id=shelf["id"])[0]
        book_obj = Book.manager.filter(id=book["id"])[0]
        for relation, expected in ((lambda: collection.books, len(lib.books)),
                                   (lambda: book_obj.annotations, 11)):
            pages = [relation()[start:start + size] for start in range(0, expected + size, size)]
            got = [row.id for page in pages for row in page]
            assert len(got) == len(set(got)) == expected
            assert got == sorted(row.id for row in relation())

    def test_paging_matches_offset(self, db):
        for start in range(0, 12, 4):
            assert ids(Book.manager.all()[start:start + 4]) == ids(Book.manager.all(offset=start, limit=4))


class TestFirstAndExists:
    def test_first_is_the_lowest_primary_key(self, db, sql_trace):
        assert Book.manager.all().first().id == db.fixture.books[0]["id"]
        assert Book.manager.all(order_by="title").first().title == "Book 0"
        assert Book.manager.filter(genre="History").first().id == db.fixture.books[1]["id"]
        assert Book.manager.filter(title="nope").first() is None
        assert len(sql_trace) == 4 and all("LIMIT ?" in sql for sql, _ in sql_trace)

    def test_exists(self, db, sql_trace):
        books = Book.manager.all()
        assert books.exists() and not Book.manager.filter(title="nope").exists()
        assert all(sql.startswith("SELECT 1 FROM") and "LIMIT ?" in sql for sql, _ in sql_trace)
        assert repr(books) == "<ModelIterable[Book]: unevaluated>"

    def test_evaluated_answers_from_its_rows(self, db, sql_trace):
        books = Book.manager.filter(genre="Fiction")
        list(books)
        assert books.first().genre == "Fiction" and books.exists() and books.count() == 5
        assert len(sql_trace) == 1


class TestOnly:
    @pytest.mark.parametrize("only", [["title"], ["ZTITLE"], ["title", "ztitle"], "title"])
    def test_only_reads_named_fields(self, db, sql_trace, only):
        books = list(Book.manager.all(only=only, order_by="id"))
        book = books[0]
        assert (book.title, book.id, book.asset_id) == ("Book 9", db.fixture.books[0]["id"],
                                                        db.fixture.books[0]["asset_id"])
        assert book.author is None and book.genre is None
        sql = sql_trace[0][0]
        assert sql.startswith("SELECT Z_PK, ZASSETID, ZTITLE, NULL, NULL,")

    def test_only_with_filter(self, db):
        [book] = Book.manager.filter(only=["genre", "ZAUTHOR"], title="Book 9")
        assert (book.genre, book.author, book.title) == ("Fiction", "Test Author", None)

    @pytest.mark.parametrize("only", [["nope"], ["ZNOPE"], ["title", "ZBOGUS"]])
    def test_unknown_names_raise(self, db, only):
        with pytest.raises(UnknownFieldError) as exc:
            Book.manager.all(only=only)
        assert isinstance(exc.value, KeyError) and "title" in exc.value.valid


class TestLegacyConstructor:
    def test_callable_returning_rows(self, db, sql_trace):
        rows = Book.manager.all(order_by="id").run_query()
        calls = []

        def run():
            calls.append(1)
            return rows

        books = ModelIterable(run, Book)
        assert len(books) == 10 and bool(books)
        assert ids(books) == [row[0] for row in rows]
        assert books[1].id == rows[1][0] and ids(books[2:4]) == [rows[2][0], rows[3][0]]
        assert books.count() == 10 and books.exists() and books.first().id == rows[0][0]
        assert books.count_by("genre") == {"Fiction": 5, "History": 5}
        assert books.run_query() is rows
        assert len(calls) == 2  # evaluated once, plus the explicit run_query()
        assert repr(books) == "<ModelIterable[Book]: 10 rows>"
        assert len(sql_trace) == 1  # the run_query() above

    def test_keyword_form(self, db):
        books = ModelIterable(callable=lambda: [], model_class=Book)
        assert list(books) == [] and books.first() is None and not books.exists()

    def test_run_query_is_uncached(self, db, sql_trace):
        books = Book.manager.all()
        assert books.run_query() == books.run_query()
        assert repr(books) == "<ModelIterable[Book]: unevaluated>" and len(sql_trace) == 2

    def test_from_objects(self, db, sql_trace):
        objs = list(Book.manager.all())
        books = ModelIterable._from_objects(Book, reversed(objs))
        assert ids(books) == ids(objs)[::-1] and len(books) == 10 and books.count() == 10
        assert books[0] is objs[-1] and books.first() is objs[-1]
        assert len(sql_trace) == 1


def test_repr(db):
    books = Book.manager.all()
    assert repr(books) == "<ModelIterable[Book]: unevaluated>"
    len(books)
    assert repr(books) == "<ModelIterable[Book]: 10 rows>"


class RecordingCompiler(QueryCompiler):
    def __init__(self):
        super().__init__(AppleBooksDBClient())
        self.calls = []

    def execute(self, query, params=()):
        self.calls.append((query, tuple(params)))
        return super().execute(query, params)


def test_reassigned_compiler_sees_every_model_query(db, monkeypatch, sql_trace):
    """``manager.compiler`` is a plain attribute (1.9.1); a replacement
    gets every model statement, with its parameters."""
    compilers = {}
    for model in (Book, Annotation, Collection):
        compilers[model] = RecordingCompiler()
        monkeypatch.setattr(model.manager, "compiler", compilers[model])
    lib = db.fixture
    shelf = lib.add_collection("Shelf")
    lib.add_to_collection(shelf, lib.books[0])
    for i in range(3):
        lib.add_annotation(lib.books[i % 2], f"highlight {i}")

    books = Book.manager.filter(genre="Fiction")
    books.count(), books.exists(), books.first(), books[1:2], books.count_by("genre"), list(books)
    annotations = list(Annotation.manager.all())
    [a.book for a in annotations]
    book = Book.manager.filter(id=lib.books[0]["id"])[0]
    list(book.annotations), list(book.collections)
    list(Collection.manager.filter(id=shelf["id"])[0].books)
    Book.manager.count(genre="History")

    recorded = [call for c in compilers.values() for call in c.calls]
    assert sorted(recorded) == sorted(sql_trace) and len(recorded) == 14
    assert ("SELECT COUNT(*) FROM (SELECT 1 FROM ZBKLIBRARYASSET WHERE ZGENRE = ?)", ("Fiction",)) \
        in compilers[Book].calls
    assert all(sql.startswith("SELECT") for sql, _ in recorded)


def test_manager_calls_do_no_io(monkeypatch, tmp_path):
    """Building an iterable reads nothing: a library that doesn't exist
    only fails when the query runs."""
    db = LibraryDB(data_dir=tmp_path / "nothing-here")
    with use_library(db):
        books = Book.manager.filter(title__search="x", order_by="-title", limit=3, offset=1)
        annotations = Annotation.manager.all(only=["note"])
    assert repr(books) == "<ModelIterable[Book]: unevaluated>"
    with pytest.raises(LibraryNotFoundError):
        list(books)
    assert repr(annotations) == "<ModelIterable[Annotation]: unevaluated>"
