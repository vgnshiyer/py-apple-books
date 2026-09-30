"""The read path from worker threads and tasks (F11).

Until wave 5 binds libraries to ``PyAppleBooks`` instances, a
``LibraryDB`` other than the default is used through ``use_library``,
around both the call and the iteration of its result.
"""

import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, query_deadline, use_library
from py_apple_books.exceptions import QueryTimeoutError

RUNAWAY = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r"

CALLS = [
    lambda api: [b.title for b in api.list_books()],
    lambda api: [(a.id, a.book.title) for a in api.list_annotations()],
    lambda api: [(c.title, [b.id for b in c.books]) for c in api.list_collections()],
    lambda api: api.get_book_by_id(2).title,
    lambda api: [a.id for a in api.search_annotation_by_text("highlight 1")],
    lambda api: [b.title for b in api.get_books_in_progress()],
    lambda api: [a.id for a in api.get_annotations_by_color("green")],
    lambda api: [b.id for b in api.get_book_by_title("book 1")],
    lambda api: [len(list(b.annotations)) for b in api.get_finished_books()],
]


@pytest.fixture
def seeded(lib_db):
    lib = lib_db.fixture
    shelf = lib.add_collection("Shelf")
    for i in range(12):
        book = lib.add_book(f"Book {i}", f"Author {i % 3}", progress=(0.0, 0.5, 1.0)[i % 3],
                            finished=i % 3 == 2)
        if i % 2:
            lib.add_to_collection(shelf, book)
        for j in range(3):
            lib.add_annotation(book, f"highlight {i} {j}", color=("yellow", "green")[j % 2],
                               created=1000.0 * (i + j))
    return lib_db


def _run_calls(db, start, count=20):
    api = PyAppleBooks()
    with use_library(db):
        return [CALLS[(start + i) % len(CALLS)](api) for i in range(count)]


def test_threads_match_single_threaded(seeded):
    expected = {t: _run_calls(seeded, t) for t in range(16)}
    results, errors = {}, []

    def worker(t):
        try:
            results[t] = _run_calls(seeded, t)
        except BaseException as e:  # reported below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert results == expected
    assert len(seeded._idle) <= seeded.max_idle


def test_more_threads_than_connections(seeded):
    """32 threads on a pool of 2 (F-G8): the waits are shared fairly, so
    no call runs out of time, and every slot is free afterwards."""
    db = LibraryDB(data_dir=seeded.fixture.data_dir, max_connections=2, max_idle=1)
    expected = {t: _run_calls(seeded, t) for t in range(32)}
    results, errors = {}, []
    barrier = threading.Barrier(32)

    def worker(t):
        try:
            barrier.wait()
            results[t] = _run_calls(db, t)
        except BaseException as e:  # reported below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert results == expected
    assert (db._slots._free, len(db._slots._waiters)) == (2, 0)
    assert len(db._idle) <= 1
    db.close()


def test_default_library_from_threads(api, library):
    library.add_book("Synthetic Book")
    with ThreadPoolExecutor(4) as pool:
        titles = list(pool.map(lambda _: [b.title for b in api.list_books()], range(8)))
    assert titles == [["Synthetic Book"]] * 8


def test_anyio_to_thread(seeded):
    anyio = pytest.importorskip("anyio")

    async def main():
        with use_library(seeded):
            return await anyio.to_thread.run_sync(_run_calls, None, 0, len(CALLS))

    # _run_calls(None, ...) keeps the library the task context set.
    assert anyio.run(main) == _run_calls(seeded, 0, len(CALLS))


def test_query_deadline_reaches_anyio_workers(seeded):
    anyio = pytest.importorskip("anyio")
    db = LibraryDB(data_dir=seeded.fixture.data_dir, query_timeout=None)

    async def main():
        with query_deadline(0.2):
            return await anyio.to_thread.run_sync(db.execute, RUNAWAY)

    start = time.monotonic()
    with pytest.raises(QueryTimeoutError, match=r"limit 0\.2 s"):
        anyio.run(main)
    assert time.monotonic() - start < 2
    db.close()


def test_query_deadline_follows_a_copied_context(seeded):
    db = LibraryDB(data_dir=seeded.fixture.data_dir, query_timeout=None)
    with query_deadline(0.2):
        context = contextvars.copy_context()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(context.run, db.execute, RUNAWAY)
        with pytest.raises(QueryTimeoutError):
            future.result(timeout=5)
    db.close()
