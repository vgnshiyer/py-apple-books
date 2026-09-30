"""Aggregates (F02, R6): ``count()``, ``exists()``, ``count_by()`` and
``Manager.count`` apply the same predicates as the lists, so a count
always equals the length of the list it counts.
"""

import datetime as dt
from collections import Counter

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import use_library
from py_apple_books.models import Annotation, Book, Collection
from py_apple_books.testing import STORE_SERIES

UTC = dt.timezone.utc


@pytest.fixture
def db(lib_db):
    """Owned books in each status, Store series rows, collections (one
    deleted), and live, deleted, tombstone, position and orphan annotations."""
    lib = lib_db.fixture
    books = []
    for i in range(9):
        books.append(lib.add_book(f"Book {i}", genre=("Fiction", "History", "Science")[i % 3],
                                  progress=(0.0, 0.3, 1.0)[i % 3], finished=i % 3 == 2,
                                  last_opened=None if i % 3 == 0 else dt.datetime(2026, 1, 1 + i, tzinfo=UTC)))
    books.append(lib.add_book("Series Volume", data_source=STORE_SERIES, progress=0.5))
    books.append(lib.add_book("Series", data_source=STORE_SERIES, content_type=5))
    shelves = [lib.add_collection(title, deleted=title == "Gone") for title in ("Shelf", "Other", "Gone")]
    for i, book in enumerate(books):
        lib.add_to_collection(shelves[i % 3], book)
    colors = ["yellow", "green", "blue", "pink", "purple"]
    for i in range(60):
        book = books[i % len(books)]
        lib.add_annotation(book, f"highlight {i} text", color=colors[i % 5],
                           kind="note" if i % 7 == 0 else "highlight", note="a note" if i % 7 == 0 else None,
                           deleted=i % 11 == 0, created=dt.datetime(2026, 1 + i % 9, 1, tzinfo=UTC))
    for book in books[:4]:
        lib.add_annotation(book, None, kind="reading_position")
        lib.add_annotation(book, None, kind="bookmark")
    lib.add_annotation(None, None, kind="tombstone")
    for i in range(3):
        lib.add_annotation("GONE-ASSET", f"orphan {i}")
    lib_db.books, lib_db.shelves = books, shelves
    with use_library(lib_db):
        yield lib_db


def facade_lists(api: PyAppleBooks) -> dict:
    """Every facade method that returns a ModelIterable, as a function of
    limit/offset/order_by keywords."""
    after = dt.datetime(2026, 3, 1)
    return {
        "list_collections": lambda **kw: api.list_collections(**kw),
        "get_collection_by_title": lambda **kw: api.get_collection_by_title("e", **kw),
        "list_books": lambda **kw: api.list_books(**kw),
        "list_books(all)": lambda **kw: api.list_books(include_store_series=True, **kw),
        "get_book_by_title": lambda **kw: api.get_book_by_title("series", include_store_series=True, **kw),
        "get_books_by_genre": lambda **kw: api.get_books_by_genre("i", **kw),
        "list_annotations": lambda **kw: api.list_annotations(**kw),
        "list_annotations(deleted)": lambda **kw: api.list_annotations(include_deleted=True, **kw),
        "get_annotations_by_color": lambda **kw: api.get_annotations_by_color("green", **kw),
        "search_highlighted": lambda **kw: api.search_annotation_by_highlighted_text("1", **kw),
        "search_note": lambda **kw: api.search_annotation_by_note("note", **kw),
        "date_range": lambda **kw: api.get_annotations_by_date_range(after=after, **kw),
        "in_progress": lambda **kw: api.get_books_in_progress(**kw),
        "finished": lambda **kw: api.get_finished_books(**kw),
        "unstarted": lambda **kw: api.get_unstarted_books(**kw),
        "recently_read": lambda **kw: api.get_recently_read_books(**kw),
        "recently_read(opened)": lambda **kw: api.get_recently_read_books(
            **{"order_by": "-last_opened_date", **kw}),
    }


SHAPES = [{}, {"limit": 3}, {"limit": 2, "offset": 1}, {"offset": 4}, {"limit": 1000},
          {"order_by": "-id"}]


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: ",".join(f"{k}={v}" for k, v in s.items()) or "plain")
def test_count_equals_list_length(db, shape):
    api = PyAppleBooks()
    counted = 0
    for name, call in facade_lists(api).items():
        n = call(**shape).count()
        assert n == len(list(call(**shape))) == len(call(**shape)), name
        assert call(**shape).exists() == (n > 0), name
        counted += n
    assert counted > 0


def test_relation_counts_equal_list_lengths(db):
    api = PyAppleBooks()
    for book in api.list_books(include_store_series=True):
        assert book.annotations.count() == len(list(book.annotations))
        assert book.collections.count() == len(list(book.collections))
    for collection in api.list_collections():
        assert collection.books.count() == len(list(collection.books)) > 0
    total = sum(b.annotations.count() for b in Book.manager.all())
    orphans = sum(1 for a in api.list_annotations() if a.book is None)
    assert total + orphans == api.list_annotations().count()


def test_count_is_one_statement_and_leaves_the_iterable_unevaluated(db, sql_trace):
    annotations = PyAppleBooks().list_annotations(order_by="-creation_date")
    assert annotations.count() == len(list(PyAppleBooks().list_annotations()))
    before = len(sql_trace)
    assert annotations.count() > 0 and repr(annotations) == "<ModelIterable[Annotation]: unevaluated>"
    assert len(sql_trace) == before + 1
    sql, params = sql_trace[-1]
    assert sql.startswith("SELECT COUNT(*) FROM (SELECT 1 FROM anno_db.ZAEANNOTATION WHERE ")
    assert params == (0, 3, 1)
    n = len(annotations)
    assert annotations.count() == n and len(sql_trace) == before + 2  # the len(), then cached


def test_count_by(db, sql_trace):
    api = PyAppleBooks()
    annotations = list(api.list_annotations())
    before = len(sql_trace)
    assert api.list_annotations().count_by("asset_id") == Counter(a.asset_id for a in annotations)
    assert len(sql_trace) == before + 1 and "GROUP BY _k" in sql_trace[-1][0]
    assert api.list_annotations(limit=7, order_by="-id").count_by("style") == Counter(
        a.style for a in api.list_annotations(limit=7, order_by="-id"))
    assert Book.manager.all().count_by("genre") == Counter(b.genre for b in Book.manager.all())
    evaluated = api.list_books()
    list(evaluated)
    assert evaluated.count_by("genre") == Counter(b.genre for b in evaluated)
    with pytest.raises(KeyError):
        Book.manager.all().count_by("nope")


@pytest.mark.parametrize("field", ["last_opened_date", "reading_progress", "author", "genre"])
def test_count_by_keys_are_raw_values_on_every_path(db, field):
    """The recency orders return a pre-1.10 callable iterable; its
    ``count_by`` groups the raw rows as the SQL does, not model values."""
    api = PyAppleBooks()
    in_python = api.get_recently_read_books(limit=None)
    in_sql = api.get_recently_read_books(limit=None, order_by="-last_opened_date")
    assert in_python.count_by(field) == in_sql.count_by(field)
    list(in_python)
    assert in_python.count_by(field) == in_sql.count_by(field)


def test_count_by_counts_what_the_rows_hold(db):
    """``count_by`` groups the values the iterable's rows hold: a field
    ``only=`` leaves out counts as None, as it reads on the models, with
    or without a limit (which groups the rows read, see test_manager)."""
    for make in (lambda: Book.manager.all(only=["title"]),
                 lambda: Book.manager.all(only=["title"], limit=4),
                 lambda: Book.manager.all(only=["title"], limit=4, order_by="-title"),
                 lambda: Book.manager.all(only=["title"], offset=2)):
        assert make().count_by("genre") == {None: len(make())}
        for field in ("title", "asset_id"):  # listed, and required
            assert make().count_by(field) == Counter(getattr(b, field) for b in make())


def test_counts_leave_out_the_order(db, sql_trace):
    """A count doesn't depend on the order, so its SQL has none; its
    columns are still checked."""
    assert Book.manager.all(order_by="-title").count() == 11
    assert Book.manager.all(order_by="title", offset=9).exists()
    assert not Book.manager.all(offset=11).exists()
    assert Book.manager.all(order_by="title", offset=3, limit=5).count() == 5
    assert Book.manager.all(order_by="title").count_by("genre") == Counter(
        b.genre for b in Book.manager.all())
    assert not any("ORDER BY" in sql for sql, _ in sql_trace[:-1])
    # With a limit or offset, the order picks the rows that are grouped.
    limited = Book.manager.all(order_by="-title", limit=4)
    assert limited.count_by("title") == Counter(b.title for b in Book.manager.all(order_by="-title", limit=4))
    assert "ORDER BY ZTITLE DESC" in sql_trace[-2][0]


def test_manager_count(db, sql_trace):
    assert Book.manager.count() == len(list(Book.manager.all())) == 11
    assert Book.manager.count(genre="Fiction") == 3
    assert Annotation.manager.count(type=3) == 4
    assert Collection.manager.count(is_deleted=0) == 2
    assert Book.manager.count(limit=2, offset=10) == 1
    before = len(sql_trace)
    Book.manager.count(title__search="book")
    assert len(sql_trace) == before + 1


def test_has_fields_runs_no_statement(db, sql_trace):
    list(Book.manager.all())
    before = len(sql_trace)
    assert Book.manager.has_fields("title", "genre") and Annotation.manager.has_fields("note")
    assert Collection.manager.has_fields("is_deleted") and Book.manager.has_fields()
    assert len(sql_trace) == before


def test_has_fields_on_a_cold_library(lib_db, sql_trace):
    with use_library(lib_db):
        assert Book.manager.has_fields("title") and Annotation.manager.has_fields("uuid")
    assert sql_trace == []
