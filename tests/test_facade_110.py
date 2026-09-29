"""The 1.10 facade: libraries of its own (F11, G2.4), query deadlines,
recency provenance (F62) and the aggregates (F02, R6).

- ``PyAppleBooks(data_dir=...)`` (or ``library_db``/``annotation_db``/
  ``query_timeout``) reads a library of its own; its results and their
  relations keep reading it after the call. ``PyAppleBooks()`` reads
  the shared default library, as before.
- ``count_books_by_status()``, ``count_annotations()`` and
  ``get_library_stats()`` count in SQL with the predicates of the lists
  they count, so each count equals the length of its list.
"""

import datetime as dt
import itertools
import os
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from py_apple_books import LibraryStats, PyAppleBooks
from py_apple_books import api as api_module
from py_apple_books.db import LibraryDB, default_library, use_library
from py_apple_books.exceptions import (
    BookNotFoundError,
    InvalidArgumentError,
    LibraryNotFoundError,
    QueryTimeoutError,
)
from py_apple_books.models import Annotation, Book, ReadingStatus
from py_apple_books.testing import STORE_SERIES, core_data_time, seed_demo

UTC = dt.timezone.utc
SLOW = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"


def day(n: int) -> dt.datetime:
    return dt.datetime(2026, 9, n, 12, 0, tzinfo=UTC)


def engaged(when) -> dict:
    return {"ZLASTENGAGEDDATE": core_data_time(when)}


def seed(lib, name: str) -> dict:
    """A book, three highlights and a collection, all named after ``name``."""
    book = lib.add_book(f"{name} book", progress=0.5, last_opened=day(3))
    for i in range(3):
        lib.add_annotation(book, f"{name} highlight {i}")
    shelf = lib.add_collection(f"{name} shelf")
    lib.add_to_collection(shelf, book)
    return {"book": book["id"], "shelf": shelf["id"]}


@pytest.fixture
def other(library, make_library):
    """A PyAppleBooks over a library of its own ('other'), while the
    default (session) library holds different rows ('default')."""
    seed(library, "default")
    lib = make_library()
    ids = seed(lib, "other")
    api = PyAppleBooks(data_dir=lib.data_dir)
    api.fixture, api.ids = lib, ids
    yield api
    api.close()


# -- construction -------------------------------------------------------------


def test_construction_does_no_io(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("I/O during construction")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    monkeypatch.setattr(os, "scandir", refuse)
    default = PyAppleBooks()
    missing = PyAppleBooks(data_dir=tmp_path / "nowhere")
    files = PyAppleBooks(library_db=tmp_path / "a.sqlite", annotation_db=tmp_path / "b.sqlite",
                         query_timeout=3)
    assert default._db is None
    assert isinstance(missing._db, LibraryDB) and isinstance(files._db, LibraryDB)
    assert files._db.query_timeout == 3.0
    monkeypatch.undo()
    with pytest.raises(LibraryNotFoundError):
        list(missing.list_books())


def test_any_argument_makes_a_library_of_its_own(tmp_path):
    assert PyAppleBooks()._db is None
    assert PyAppleBooks(query_timeout=None)._db.query_timeout is None
    assert PyAppleBooks(tmp_path)._db is not PyAppleBooks(tmp_path)._db
    with pytest.raises(InvalidArgumentError):
        PyAppleBooks(query_timeout=-1)


def test_public_methods_are_wrapped_with_their_signatures():
    import inspect

    for name, attr in vars(PyAppleBooks).items():
        if not name.startswith("_") and inspect.isfunction(attr):
            assert attr.__wrapped__.__name__ == name, name
    assert list(inspect.signature(PyAppleBooks.list_books).parameters) == [
        "self", "limit", "order_by", "offset", "include_store_series"]
    assert "Get a book and its annotations" in PyAppleBooks.get_book_by_id.__doc__


def test_subclass_without_init_call_reads_the_default_library(library):
    """1.9 had no __init__; a subclass that doesn't call it still works."""
    seed(library, "default")

    class Mine(PyAppleBooks):
        def __init__(self):
            self.mine = True

    assert [b.title for b in Mine().list_books()] == ["default book"]


def test_subclass_methods_read_the_instance_library(other):
    """A subclass's own public methods are wrapped as well: the models
    they read directly come from the instance's library."""

    class Mine(PyAppleBooks):
        def my_titles(self):
            return [b.title for b in Book.manager.all()]

        def list_books(self, *args, **kwargs):
            return list(super().list_books(*args, **kwargs))

        def _helper(self):
            return [b.title for b in Book.manager.all()]

    mine = Mine(data_dir=other.fixture.data_dir)
    try:
        assert mine.my_titles() == ["other book"]
        assert [b.title for b in mine.list_books()] == ["other book"]
        assert Mine.my_titles.__wrapped__.__name__ == "my_titles"
        assert not hasattr(Mine._helper, "__wrapped__")
    finally:
        mine.close()
    assert Mine().my_titles() == ["default book"]


# -- the library of an instance ---------------------------------------------


def test_data_dir_instance_reads_its_library(other):
    books = other.list_books()
    annotations = other.list_annotations()
    collection = other.get_collection_by_id(other.ids["shelf"])
    # Evaluated after the calls returned, outside any use_library block.
    assert [b.title for b in books] == ["other book"]
    [book] = list(other.list_books())
    assert [a.selected_text for a in book.annotations] == [f"other highlight {i}" for i in range(3)]
    assert {a.book.title for a in annotations} == {"other book"}
    assert [b.title for b in collection.books] == ["other book"]
    assert [c.title for c in book.collections] == ["other shelf"]
    assert other.count_annotations() == 3 and other.count_annotations(other.ids["book"]) == 3
    assert [a.selected_text for a in other.search_annotation_by_text("highlight 1")] == ["other highlight 1"]
    # The default instance still reads the default library.
    assert [b.title for b in PyAppleBooks().list_books()] == ["default book"]
    assert [b.title for b in Book.manager.all()] == ["default book"]


def test_explicit_store_files(other, make_library):
    lib = other.fixture
    api = PyAppleBooks(library_db=lib.library_path, annotation_db=lib.annotation_path)
    try:
        assert [a.book.title for a in api.list_annotations()] == ["other book"] * 3
    finally:
        api.close()


def test_default_instance_follows_use_library(other):
    """PyAppleBooks() reads the current library, as in wave 3."""
    with use_library(other._db):
        assert [b.title for b in PyAppleBooks().list_books()] == ["other book"]
    assert [b.title for b in PyAppleBooks().list_books()] == ["default book"]


def test_an_instance_ignores_an_enclosing_use_library(other, lib_db):
    with use_library(lib_db):
        assert [b.title for b in other.list_books()] == ["other book"]


def test_instances_in_threads_keep_their_results_apart(other):
    default = PyAppleBooks()
    apis = [(default, "default"), (other, "other")]

    def work(i):
        api, name = apis[i % 2]
        books = api.list_books()
        annotations = api.list_annotations(order_by="id")
        time.sleep(0.001)
        return (name, [b.title for b in books], [a.book.title for a in annotations],
                [a.selected_text for b in books for a in b.annotations])

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(work, range(64)))
    for name, titles, annotated, texts in results:
        assert titles == [f"{name} book"] and annotated == [f"{name} book"] * 3
        assert texts == [f"{name} highlight {i}" for i in range(3)]


def test_close_then_a_call_reopens(other):
    list(other.list_books())
    assert other._db._idle
    other.close()
    assert other._db._idle == [] and other._db._paths is None
    assert [b.title for b in other.list_books()] == ["other book"]
    assert other._db._idle


def test_close_of_the_default_instance_closes_the_default_library(library):
    seed(library, "default")
    api = PyAppleBooks()
    list(api.list_books())
    assert default_library()._idle
    api.close()
    assert default_library()._idle == []
    assert [b.title for b in api.list_books()] == ["default book"]


# -- deadlines ----------------------------------------------------------------


@pytest.fixture
def slow_queries(monkeypatch):
    """Every statement run through LibraryDB.execute becomes a recursive
    CTE that never ends."""
    original = LibraryDB.execute
    monkeypatch.setattr(LibraryDB, "execute", lambda self, sql, params=(): original(self, SLOW, ()))


@pytest.mark.parametrize("make", [PyAppleBooks, "other"])
def test_query_deadline_stops_a_slow_query(other, slow_queries, make):
    api = other if make == "other" else make()
    start = time.monotonic()
    with api.query_deadline(0.05):
        with pytest.raises(QueryTimeoutError, match="limit 0.05 s"):
            api.count_annotations()
    assert time.monotonic() - start < 5


def test_query_timeout_of_an_instance(other, slow_queries):
    api = PyAppleBooks(data_dir=other.fixture.data_dir, query_timeout=0.05)
    try:
        with pytest.raises(QueryTimeoutError, match="limit 0.05 s"):
            list(api.list_books())
    finally:
        api.close()


def test_query_deadline_none_changes_nothing(other):
    with other.query_deadline(None):
        assert other.count_annotations() == 3
    with pytest.raises(InvalidArgumentError):
        with other.query_deadline(-1):
            pass


# -- recency ------------------------------------------------------------------


def wave3_recency(order_by="-last_read_date", limit=10, offset=None) -> list:
    """Wave 3's get_recently_read_books ids: the raw rows sorted by the
    later of the two raw dates (NULL oldest), ties by id."""
    descending = order_by == "-last_read_date"
    rows = Book.manager.filter(last_opened_date__isnull=False, **api_module._owned_books_filter()).run_query()
    keys = list(Book._get_mappings("Book"))
    i_open, i_engaged, i_id = (keys.index(k) for k in ("last_opened_date", "last_engaged_date", "id"))

    def read_at(row):
        return max(float("-inf") if row[i] is None else float(row[i]) for i in (i_open, i_engaged))

    rows = sorted(rows, key=lambda row: (-read_at(row) if descending else read_at(row), row[i_id]))
    start = offset or 0
    return [row[i_id] for row in (rows[start:] if limit is None else rows[start:start + limit])]


@pytest.fixture
def recency_library(library, tmp_path):
    """The demo library plus books whose engaged date is later than their
    opened date, ties, and Store series rows."""
    seed_demo(library, tmp_path)
    add = library.add_book
    add("Engaged Later", progress=0.3, last_opened=day(2), raw=engaged(day(29)))
    add("Engaged Earlier", progress=0.3, last_opened=day(21), raw=engaged(day(4)))
    add("Tie A", last_opened=day(6))
    add("Tie B", last_opened=day(6))
    add("Tie C", last_opened=day(1), raw=engaged(day(6)))
    add("Opened Once", last_opened=day(9))
    add("Never Opened", raw=engaged(day(28)))
    add("Store Volume", data_source=STORE_SERIES, can_redownload=0, last_opened=day(30))
    return library


@pytest.mark.parametrize("order_by", ["-last_read_date", "last_read_date"])
def test_recency_is_that_of_wave_3(api, recency_library, order_by):
    for limit, offset in itertools.product([None, 1, 3, 10, 100], [None, 0, 2, 100]):
        got = [b.id for b in api.get_recently_read_books(limit=limit, order_by=order_by, offset=offset)]
        assert got == wave3_recency(order_by, limit, offset), (limit, offset)
    assert len(api.get_recently_read_books(limit=None)) > 10


def test_recency_models_carry_the_instance_library(other):
    other.fixture.add_book("other second", last_opened=day(4))
    recent = other.get_recently_read_books()
    assert [b.title for b in recent] == ["other second", "other book"]
    assert all(b.__dict__["_ab_db"] is other._db for b in recent)
    assert [a.selected_text for a in recent[1].annotations] == [f"other highlight {i}" for i in range(3)]
    assert recent.count() == len(recent) == 2
    # run_query() (a 1.9.1 attribute) returns the rows, in order.
    assert [Book.from_db(row).title for row in recent.run_query()] == ["other second", "other book"]


def test_recency_count_by_groups_raw_values(api, recency_library):
    in_python = api.get_recently_read_books(limit=None)
    in_sql = api.get_recently_read_books(limit=None, order_by="-last_opened_date")
    for field in ("last_opened_date", "reading_progress", "title"):
        assert in_python.count_by(field) == in_sql.count_by(field)


# -- aggregates ---------------------------------------------------------------


@pytest.fixture
def stats_library(library):
    """Owned books in every status corner, Store series rows (one with
    annotations), a duplicate asset id, and live, deleted, tombstone,
    reading-position, bookmark, orphan and asset-less annotations."""
    add = library.add_book
    books = {
        "finished_zero": add("Finished At Zero", finished=True, progress=0.0),
        "finished": add("Finished", finished=True, progress=1.0),
        "reading": add("Reading", progress=0.5),
        "null_progress": add("No Progress", progress=None),
        "zero": add("Unfinished", progress=0.0, raw={"ZISFINISHED": 0}),
        "null_source": add("No Source", data_source=None, progress=0.2),
        "owned_volume": add("Owned Volume", data_source=STORE_SERIES, can_redownload=1, progress=0.3),
        "unowned_volume": add("Store Volume", data_source=STORE_SERIES, can_redownload=0, progress=0.1),
        "null_redownload": add("Store Unknown", data_source=STORE_SERIES, raw={"ZCANREDOWNLOAD": None}),
        "container": add("Series Stack", data_source=STORE_SERIES, content_type=5),
    }
    # A second row with the reading book's asset id: annotation.book is
    # the lower id, so the stats count its annotations there.
    books["duplicate"] = add("Duplicate", asset_id=books["reading"]["asset_id"], progress=0.9)
    note = library.add_annotation
    counts = {"reading": 5, "finished": 3, "owned_volume": 2, "unowned_volume": 2, "container": 1,
              "null_source": 3}
    for key, n in counts.items():
        for i in range(n):
            note(books[key], f"{key} highlight {i}", created=day(1 + i))
    note(books["reading"], "deleted", deleted=True)
    note(books["finished"], None, kind="reading_position")
    note(books["finished"], None, kind="bookmark")
    note(None, None, kind="tombstone")
    for i in range(4):
        note("GONE-ASSET", f"orphan {i}")
    note(None, "asset-less highlight", raw={"ZANNOTATIONASSETID": None})
    note(None, "empty asset id highlight")
    return books


SCOPE_COLUMNS = ("content_type", "data_source", "can_redownload")
MISSING = [set(c) for n in range(len(SCOPE_COLUMNS) + 1) for c in itertools.combinations(SCOPE_COLUMNS, n)]


def expected_per_book(api) -> tuple:
    counter = Counter((a.book.id, a.book.title) for a in api.list_annotations() if a.book is not None)
    return tuple(sorted(((bid, title, n) for (bid, title), n in counter.items()),
                        key=lambda entry: (-entry[2], entry[0])))


@pytest.mark.parametrize("missing", MISSING, ids=lambda m: "+".join(sorted(m)) or "all-columns")
def test_counts_equal_list_lengths(api, stats_library, monkeypatch, missing):
    """For every combination of the owned-scope columns the store may
    lack (R8), each count equals the length of the list it counts."""
    if missing:
        monkeypatch.setattr(Book.manager, "has_fields", lambda *fields: not missing & set(fields))
    counts = api.count_books_by_status()
    lists = {ReadingStatus.FINISHED: api.get_finished_books(),
             ReadingStatus.IN_PROGRESS: api.get_books_in_progress(),
             ReadingStatus.UNSTARTED: api.get_unstarted_books()}
    assert counts == {status: len(list(books)) for status, books in lists.items()}
    stats = api.get_library_stats()
    total = len(list(api.list_books()))
    assert stats.total_books == sum(counts.values()) == total
    assert (stats.finished_books, stats.in_progress_books, stats.unstarted_books) == (
        counts[ReadingStatus.FINISHED], counts[ReadingStatus.IN_PROGRESS], counts[ReadingStatus.UNSTARTED])
    ids = [{b.id for b in books} for books in lists.values()]
    assert set.union(*ids) == {b.id for b in api.list_books()} and sum(map(len, ids)) == total
    assert stats.total_annotations == api.count_annotations() == len(list(api.list_annotations()))


def test_library_stats(api, stats_library, sql_trace):
    before = len(sql_trace)
    stats = api.get_library_stats()
    assert len(sql_trace) - before <= 5
    assert isinstance(stats, LibraryStats)
    annotations = list(api.list_annotations())
    assert stats.total_annotations == len(annotations) == 23
    assert stats.orphan_annotations == sum(1 for a in annotations if a.book is None) == 6
    assert stats.annotations_per_book == expected_per_book(api)
    by_id = {bid: n for bid, _, n in stats.annotations_per_book}
    books = {key: row["id"] for key, row in stats_library.items()}
    assert by_id[books["reading"]] == 5 and books["duplicate"] not in by_id
    assert by_id[books["container"]] == 1  # Store series rows resolve, as annotation.book does
    assert (stats.total_books, stats.finished_books, stats.in_progress_books, stats.unstarted_books) == (
        8, 2, 4, 2)
    with pytest.raises(AttributeError):
        stats.total_books = 0


def test_library_stats_of_an_empty_library(api, library, sql_trace):
    assert api.get_library_stats() == LibraryStats(0, 0, 0, 0, 0, 0, ())
    assert len(sql_trace) <= 5


def test_count_annotations_of_each_book(api, stats_library):
    for row in stats_library.values():
        book = api.get_book_by_id(row["id"])
        assert api.count_annotations(row["id"]) == len(list(book.annotations))
    assert api.count_annotations(stats_library["reading"]["id"]) == 5
    with pytest.raises(BookNotFoundError) as exc:
        api.count_annotations(10**6)
    assert isinstance(exc.value, IndexError)


def test_count_books_by_status_keys(api, stats_library):
    counts = api.count_books_by_status()
    assert list(counts) == [ReadingStatus.FINISHED, ReadingStatus.IN_PROGRESS, ReadingStatus.UNSTARTED]
    assert counts["finished"] == counts[ReadingStatus.FINISHED] == 2
    assert counts["in_progress"] == 4 and counts["unstarted"] == 2


def test_count_books_by_status_is_three_statements(api, stats_library, sql_trace):
    api.count_books_by_status()
    before = len(sql_trace)
    api.count_books_by_status()
    assert len(sql_trace) - before == 3
    assert all(sql.startswith("SELECT COUNT(*) FROM") for sql, _ in sql_trace[before:])


def test_statement_counts_on_a_large_library(api, library, sql_trace):
    library.populate(books=100, annotations_per_book=100)
    annotations = list(api.list_annotations())
    titles = {a.book.title for a in annotations}
    assert len(annotations) == 10_000 and len(titles) == 100
    assert len(sql_trace) <= 3, sql_trace
    before = len(sql_trace)
    stats = api.get_library_stats()
    assert len(sql_trace) - before <= 5
    assert stats.total_annotations == 10_000 and stats.orphan_annotations == 0
    assert {n for _, _, n in stats.annotations_per_book} == {100}
    assert [bid for bid, _, _ in stats.annotations_per_book] == sorted(b.id for b in Book.manager.all())
    assert stats.total_books == 100 == stats.finished_books + stats.in_progress_books + stats.unstarted_books


# -- typing -------------------------------------------------------------------


def test_typed_reverse_relations_are_not_fields():
    """Annotation.book and Book.collections are declared for type
    checkers only (``if TYPE_CHECKING``); at run time they are the
    relations ModelBase installs, not dataclass fields."""
    import dataclasses

    from py_apple_books.models.relations import ReverseManyToMany, ReverseToOne

    assert "book" not in {f.name for f in dataclasses.fields(Annotation)}
    assert "collections" not in {f.name for f in dataclasses.fields(Book)}
    assert isinstance(Annotation.__dict__["book"], ReverseToOne)
    assert isinstance(Book.__dict__["collections"], ReverseManyToMany)
