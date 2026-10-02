"""Annotation search at release-gate sizes: ranked search
(``search_annotations``, 1.11) at 50,000 annotations and 1.10's folded
search (``search_annotation_by_*``) at 200,000.

Each runs at a small size in every suite; ``APPLE_BOOKS_SLOW_TESTS=1``
adds the gate size and holds both to strict time budgets (soft
otherwise: CI machines vary). Synthetic text only:
``FixtureLibrary.populate`` rows, plus a note on some of them.
"""

import sqlite3
import time

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.text import fold_for_match
from tests._bootstrap import SLOW


@pytest.fixture
def library(make_library):
    return make_library()


@pytest.fixture
def api(library):
    made = PyAppleBooks(data_dir=library.data_dir)
    yield made
    made.close()


def populated(library, count: int, books: int) -> None:
    """``count`` highlights over ``books`` books; every 97th also gets a note."""
    library.populate(books=books, annotations_per_book=count // books)
    library.execute("annotations", "UPDATE ZAEANNOTATION SET ZANNOTATIONNOTE = 'a synthetic note on row ' || Z_PK "
                                   "WHERE Z_PK % 97 = 0")


def best_of(fn, runs: int = 3):
    """``(result of the last run, fastest wall time)``."""
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - start)
    return result, min(times)


def folded_rows(library) -> list:
    """``(pk, folded selected text, folded surrounding text, folded note)`` of every row."""
    con = sqlite3.connect(library.annotation_path)
    try:
        rows = con.execute("SELECT Z_PK, ZANNOTATIONSELECTEDTEXT, ZANNOTATIONREPRESENTATIVETEXT, "
                           "ZANNOTATIONNOTE FROM ZAEANNOTATION").fetchall()
    finally:
        con.close()
    return [(pk, *(fold_for_match(v) if v else "" for v in values)) for pk, *values in rows]


# -- ranked search -----------------------------------------------------------------

RANKED_QUERIES = ["theme", "theme of book 17", "THÉME", "synthetic highlight 4242", "about the", "zzz-no-match",
                  '"theme of"', "highlight -theme", "theme*", "NEAR(book theme)", "synthetic note on row 970"]


def ids(hits) -> list:
    return [h.annotation.id for h in hits]


@pytest.mark.parametrize("count", [10_000] + ([50_000] if SLOW else []))
def test_ranked_search_at_scale(library, api, count):
    """Every result of search_annotation_by_text is a ranked result,
    once; limit and offset are slices of limit=None; the first call
    (index build included) and the warm limit=20 queries keep within
    budget."""
    populated(library, count, books=100)
    start = time.perf_counter()
    api.search_annotations("theme", limit=20)
    first = time.perf_counter() - start
    worst = 0.0
    for query in RANKED_QUERIES:
        everything = ids(api.search_annotations(query, limit=None))
        assert len(everything) == len(set(everything)), query
        assert {a.id for a in api.search_annotation_by_text(query)} <= set(everything), query
        top, warm = best_of(lambda q=query: ids(api.search_annotations(q, limit=20)))
        assert top == everything[:20], query
        assert ids(api.search_annotations(query, limit=20, offset=20)) == everything[20:40], query
        worst = max(worst, warm)
    # Strict budgets: about 2.5x what G1 measured at load average 17
    # (first call 1.03 s, warm limit=20 at most 0.19 s at 50k).
    first_budget, warm_budget = ((1.0, 0.1) if count == 10_000 else (2.5, 0.5)) if SLOW else (10.0, 3.0)
    assert first < first_budget, f"{count}: first call {first:.3f} s"
    assert worst < warm_budget, f"{count}: warm limit=20 {worst:.3f} s"


# -- 1.10's folded search ------------------------------------------------------------

FOLDED_QUERIES = [("text", "theme of book 7"), ("text", "THÉME OF BOOK 3"), ("text", "highlight 9999"),
                  ("text", "zzz-no-match"), ("text", "about the"), ("text", "%"), ("text", "_"),
                  ("highlighted", "synthetic highlight 4242"), ("note", "note on row 97"), ("note", "anything")]


@pytest.mark.parametrize("count", [10_000] + ([200_000] if SLOW else []))
def test_folded_search_at_scale(library, api, count):
    """search_annotation_by_text, _by_highlighted_text and _by_note find
    exactly the rows whose folded fields contain the folded query
    (LIKE's % and _ are literal), newest first, and page with limit and
    offset, within budget."""
    populated(library, count, books=count // 1000)
    rows = folded_rows(library)
    assert len(rows) == count
    search = {"text": api.search_annotation_by_text, "highlighted": api.search_annotation_by_highlighted_text,
              "note": api.search_annotation_by_note}
    fields = {"text": (1, 2, 3), "highlighted": (1,), "note": (3,)}
    slowest = 0.0
    for kind, query in FOLDED_QUERIES:
        needle = fold_for_match(query)
        expected = {row[0] for row in rows if any(needle in row[i] for i in fields[kind])}
        found, best = best_of(lambda f=search[kind], q=query: [a.id for a in f(q)])
        assert len(found) == len(set(found)) and set(found) == expected, (kind, query)
        slowest = max(slowest, best)
    # Newest first: populate dates rows by primary key.
    everything = [a.id for a in api.search_annotation_by_text("synthetic")]
    assert everything == sorted(everything, reverse=True) and len(everything) == count
    page = [a.id for a in api.search_annotation_by_text("synthetic", limit=20, offset=1000)]
    assert page == everything[1000:1020]
    # Strict budgets: about 2.5x the 2.3-2.6 s G1 measured for the
    # broadest query (every row) at 200k, at load average 17.
    budget = (0.3 if count == 10_000 else 6.0) if SLOW else 30.0
    assert slowest < budget, f"{count}: slowest query {slowest:.3f} s"
