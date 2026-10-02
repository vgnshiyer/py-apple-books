"""Ranked annotation search (1.11, ``PyAppleBooks.search_annotations``):
the query builder, ranking and tiers, paging, scope, the superset
guarantee, freshness, schema drift and non-English text.

Synthetic text only. Each test reads a library of its own (the index is
per ``LibraryDB``)."""

import dataclasses
import datetime as dt
import os
import random
import shutil
import threading
import time

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books import search
from py_apple_books.exceptions import (
    AnnotationStoreNotFoundError,
    BookNotFoundError,
    DBQueryError,
    InvalidArgumentError,
)
from py_apple_books.search import AnnotationHit, MatchMethod, MAX_QUERY_LENGTH

FTS = MatchMethod.FTS
SUBSTRING = MatchMethod.SUBSTRING


@pytest.fixture
def lib(make_library):
    return make_library()


@pytest.fixture
def ranked(lib):
    """A PyAppleBooks reading ``lib``, closed after the test."""
    api = PyAppleBooks(data_dir=lib.data_dir)
    yield api
    api.close()


@pytest.fixture
def clock(monkeypatch):
    """The index's clock, moved by hand: ``clock[0] += seconds``."""
    now = [1000.0]
    monkeypatch.setattr(search, "_clock", lambda: now[0])
    return now


def ids(hits) -> list:
    return [h.annotation.id for h in hits]


def index_of(api) -> search.AnnotationIndex:
    return api._PyAppleBooks__library._derived[search._INDEX_KEY]


def rep(text):
    """``raw=`` for an annotation whose surrounding text is ``text``."""
    return {"ZANNOTATIONREPRESENTATIVETEXT": text}


def bump(lib, pk, **columns):
    """Change annotation ``pk`` as Core Data does: the columns, Z_OPT + 1."""
    sets = ", ".join([f"{c} = ?" for c in columns] + ["Z_OPT = Z_OPT + 1"])
    lib.execute("annotations", f"UPDATE ZAEANNOTATION SET {sets} WHERE Z_PK = ?", (*columns.values(), pk))


# -- public types --------------------------------------------------------------


class TestTypes:
    def test_module_exports(self):
        assert search.__all__ == ["AnnotationHit", "MatchMethod", "fts5_available", "MAX_QUERY_LENGTH"]
        assert MAX_QUERY_LENGTH == 10_000

    def test_match_method_is_a_str_enum(self):
        assert MatchMethod.FTS == "fts" and MatchMethod.SUBSTRING == "substring"
        assert str(MatchMethod.FTS) == "fts" and f"{MatchMethod.SUBSTRING}" == "substring"
        assert MatchMethod("fts") is MatchMethod.FTS

    def test_hits_compare_by_identity(self, lib, ranked):
        book = lib.add_book("B")
        lib.add_annotation(book, "a zebra crossing")
        first, = ranked.search_annotations("zebra")
        second, = ranked.search_annotations("zebra")
        assert first == first and first != second
        assert len({first, second}) == 2
        assert dataclasses.astuple(first)[1:] == dataclasses.astuple(second)[1:]
        with pytest.raises(dataclasses.FrozenInstanceError):
            first.score = 0.0
        assert isinstance(first, AnnotationHit) and isinstance(first.method, MatchMethod)
        assert isinstance(first.score, float) and first.matched_all is True

    def test_fts5_is_probed_once(self, monkeypatch):
        monkeypatch.setattr(search, "_fts5", None)
        calls = []
        real = search._probe_fts5
        monkeypatch.setattr(search, "_probe_fts5", lambda: calls.append(1) or real())
        assert search.fts5_available() is search.fts5_available()
        assert calls == [1]

    def test_fts5_is_probed_once_across_threads(self, monkeypatch):
        """16 first calls at once: one probe, and every thread gets its
        result."""
        monkeypatch.setattr(search, "_fts5", None)
        calls = []
        real = search._probe_fts5

        def slow_probe():
            calls.append(1)
            time.sleep(0.2)  # every thread arrives while it runs
            return real()

        monkeypatch.setattr(search, "_probe_fts5", slow_probe)
        barrier = threading.Barrier(16)
        results = []

        def first_call():
            barrier.wait(10)
            results.append(search.fts5_available())

        threads = [threading.Thread(target=first_call, daemon=True) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert calls == [1] and len(results) == 16 and len(set(results)) == 1
        assert search.fts5_available() is results[0] and calls == [1]


# -- the query builder -----------------------------------------------------------

SPECIAL_QUERIES = [
    "decision-making", "don't", "don’t", "what about habits?", "NOT habits", "AND", "OR",
    "NEAR(a b)", "a NEAR b", "habit*", "*", "col:x", "sel:habit", "^start", "+plus", "-minus",
    "(paren", "paren)", "{brace}", '"balanced quotes"', 'unbalanced "quote', '"', '""', "'",
    "a\x00b", "\x00", "lone \ud800 surrogate", "\u0301\u0301", "e\u0301", "", "   ", "\u200b",
    "´", "a ¨", "!!!", "...", "…", "?", "%", "_", "100%", "c++", "名前", "名前 name",
    "مدرسة", "किताब", "a'b'c", "'quoted'", "x" * 300, "and or not near",
]


def _match_database(rows=("a habit of decision making", "don't wait", "名前 は 太郎")):
    conn, fts = search._new_database()
    conn.executemany("INSERT INTO ann (rowid, sel, note, rep, asset, live) VALUES (?, ?, '', ?, 'A', 1)",
                     [(i, search.fold_for_match(t), search.fold_for_match(t)) for i, t in enumerate(rows, 1)])
    return conn, fts


@pytest.mark.parametrize("query", SPECIAL_QUERIES)
def test_special_queries_run_every_tier_without_error(query):
    conn, fts = _match_database()
    try:
        plan = search._plan(query)
        if plan is None:
            return
        for require_all in (False, True):
            search._query(conn, fts, plan, None, False, require_all)
            search._query(conn, fts, plan, "A", True, require_all)
        if plan.terms:
            for connector in (" AND ", " OR "):
                conn.execute("SELECT rowid FROM ann WHERE ann MATCH ?", [connector.join(plan.terms)]).fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("query, items, substring, needle", [
    ("Decision-Making", ("decision", "making"), False, "decision-making"),
    ("don’t wait", ("don't", "wait"), False, "don't wait"),
    ("NOT habits", ("habits",), False, "not habits"),
    ("the of", ("the of",), False, "the of"),
    ("the", ("the",), False, "the"),
    ('"decision making" habit', ("decision making", "habit"), False, '"decision making" habit'),
    ('"single" word', ("single", "word"), False, '"single" word'),
    ("col:x", ("col", "x"), False, "col:x"),
    ("habit habit HABIT", ("habit",), False, "habit habit habit"),
    ("  spaced   words  ", ("spaced", "words"), False, "spaced words"),
    ("名前", ("名前",), True, None),
    ("名前 name", ("名前", "name"), True, "名前 name"),
    ("학교", ("학교",), True, None),
    ("...", ("...",), True, "..."),
    ("?", ("?",), True, None),
    ("ab", ("ab",), False, None),
    # 3 characters only with a space: the needle keeps it, as the
    # substring search's does.
    (" ab", ("ab",), False, " ab"),
    ("ab ", ("ab",), False, "ab "),
    (" a ", ("a",), False, " a "),
    ("\u00a0ab", ("ab",), False, " ab"),
    (" \u0301ab", ("ab",), False, " ab"),
    (" a", ("a",), False, None),
])
def test_plan(query, items, substring, needle):
    plan = search._plan(query)
    assert (plan.items, plan.substring, plan.needle) == (items, substring, needle)
    if not substring:
        assert plan.terms == tuple('"' + i.replace('"', '""') + '"' for i in items)


@pytest.mark.parametrize("query", ["", "   ", "\u200b", "\u0301", "´", "\u200b \u0301"])
def test_nothing_to_search_for(query):
    assert search._plan(query) is None


def test_terms_are_quoted_fts5_strings():
    plan = search._plan('say "hi" NOT col:x OR "a ""b"" c')
    assert all(t.startswith('"') and t.endswith('"') for t in plan.terms)


def test_planning_stops_at_32_distinct_terms():
    plan = search._plan(" ".join(f"word{i}" for i in range(100)))
    assert len(plan.items) == 32 and plan.items[0] == "word0"


def test_query_length():
    assert search._plan("a " * (MAX_QUERY_LENGTH // 2)) is not None
    with pytest.raises(InvalidArgumentError) as e:
        search._plan("q" * (MAX_QUERY_LENGTH + 1))
    assert "qqq" not in str(e.value) and str(MAX_QUERY_LENGTH) in str(e.value)
    # A non-str query is measured as text, after str().
    with pytest.raises(InvalidArgumentError):
        search._plan(_Long())


class _Long:
    def __str__(self):
        return "z" * (MAX_QUERY_LENGTH + 1)


def test_a_long_query_plans_fast():
    queries = ["x" * MAX_QUERY_LENGTH, ("word " * 2000)[:MAX_QUERY_LENGTH],
               ("名前" * 5000)[:MAX_QUERY_LENGTH], ("-.,;" * 2500)[:MAX_QUERY_LENGTH]]
    for query in queries:
        best = min(_timed(lambda q=query: search._plan(q)) for _ in range(3))
        assert best < 0.05, (query[:10], best)


def _timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


FUZZ_ALPHABET = (
    list("abcdefghij XYZ 0123 '\"-*:^+(){}[]<>!?.,;%_\\/") + ["NOT", "AND", "OR", "NEAR", "col:"]
    + ["’", "“", "”", "—", "…", "ﬁ", "ß", "é", "e\u0301", "\u0301", "\u200b", "\u00a0", "\x00", "\ud800"]
    + ["名", "前", "は", "학", "교", "ก", "ມ", "ក", "မ", "م", "د", "क", "\u093f", "\u0902", "\u0903", "\ue000"]
)


def test_query_builder_fuzz():
    """Random queries over operators, quotes and many scripts: every tier
    runs without an SQLite error (seeded; APPLE_BOOKS_FUZZ_ITERATIONS)."""
    iterations = int(os.environ.get("APPLE_BOOKS_FUZZ_ITERATIONS", "300"))
    rng = random.Random(2301)
    conn, fts = _match_database(["".join(rng.choice(FUZZ_ALPHABET) for _ in range(40)) for _ in range(20)])
    failures = []
    try:
        for _ in range(iterations):
            query = "".join(rng.choice(FUZZ_ALPHABET) for _ in range(rng.randint(0, 12)))
            plan = search._plan(query)
            if plan is None:
                continue
            try:
                search._query(conn, fts, plan, None, False, False)
                if plan.terms:
                    conn.execute("SELECT rowid FROM ann WHERE ann MATCH ?", [" AND ".join(plan.terms)]).fetchall()
            except Exception as e:  # noqa: BLE001 (collected)
                failures.append((ascii(query), type(e).__name__))
    finally:
        conn.close()
    assert failures == []


# -- ranking and tiers -------------------------------------------------------------


@pytest.fixture
def filler(lib):
    """Annotations without the test words, so that bm25's inverse
    document frequency is positive (a word in half the rows scores ~0)."""
    lib.populate(books=1, annotations_per_book=40)
    return lib.add_book("Ranking Book")


class TestRanking:
    def test_highlight_outranks_note_outranks_surrounding_text(self, lib, ranked, filler):
        in_rep = lib.add_annotation(filler, "plain words here", raw=rep("plain words here zebra"))
        in_note = lib.add_annotation(filler, "plain words here", note="zebra", raw=rep("plain words here"))
        in_sel = lib.add_annotation(filler, "plain zebra here", raw=rep("plain words here"))
        hits = ranked.search_annotations("zebra")
        assert ids(hits) == [in_sel, in_note, in_rep]
        assert hits[0].score > hits[1].score > hits[2].score > 0
        assert all(h.method is FTS and h.matched_all for h in hits)

    def test_tiers(self, lib, ranked, filler):
        both = lib.add_annotation(filler, "decision making is hard")
        stemmed = lib.add_annotation(filler, "she makes decisions")
        inside = lib.add_annotation(filler, "indecision making")
        partial = lib.add_annotation(filler, "making bread")
        hits = ranked.search_annotations("decision making", limit=None)
        assert set(ids(hits)[:2]) == {both, stemmed} and ids(hits)[2:] == [inside, partial]
        assert [(h.matched_all, h.method) for h in hits] == [
            (True, FTS), (True, FTS), (True, SUBSTRING), (False, FTS)]
        assert [h.matched_all for h in hits] == sorted((h.matched_all for h in hits), reverse=True)

    def test_require_all_drops_partial_hits(self, lib, ranked, filler):
        both = lib.add_annotation(filler, "decision making")
        lib.add_annotation(filler, "making bread")
        lib.add_annotation(filler, "a decision")
        hits = ranked.search_annotations("decision making", require_all=True, limit=None)
        assert ids(hits) == [both] and hits[0].matched_all
        assert len(ranked.search_annotations("decision making", limit=None)) == 3

    def test_ties_go_to_the_higher_id(self, lib, ranked, filler):
        rows = [lib.add_annotation(filler, "same zebra text") for _ in range(3)]
        assert ids(ranked.search_annotations("zebra")) == rows[::-1]

    def test_fallback_finds_words_inside_longer_words(self, lib, ranked, filler):
        """Tier 4, only when nothing else matched: an attached article."""
        row = lib.add_annotation(filler, "ذهبت إلى المدرسة")
        affix = lib.add_annotation(filler, "unrelated substringword")
        hits = ranked.search_annotations("مدرسة")
        assert ids(hits) == [row] and hits[0].method is SUBSTRING and hits[0].matched_all
        hits = ranked.search_annotations("string word")
        assert ids(hits) == [affix] and hits[0].method is SUBSTRING and hits[0].matched_all
        # A word of 1-2 characters never triggers the fallback.
        assert ranked.search_annotations("ub") == []

    def test_fallback_needs_every_word(self, lib, ranked, filler):
        lib.add_annotation(filler, "prefixalpha prefixbeta")
        only_one = lib.add_annotation(filler, "prefixalpha")
        assert ids(ranked.search_annotations("alpha beta", limit=None)) != [only_one]
        assert len(ranked.search_annotations("alpha beta", limit=None)) == 1

    def test_fallback_and_short_words(self, lib, ranked, filler):
        """The fallback first needs every word, short ones included
        (matched_all); then, unless require_all, only the words of 3 or
        more characters (not matched_all)."""
        partial = lib.add_annotation(filler, "prefixalpha nothing else")
        both = lib.add_annotation(filler, "prefixalpha xzzx")
        hits = ranked.search_annotations("alpha zz", limit=None)
        assert [(h.annotation.id, h.matched_all, h.method) for h in hits] == [
            (both, True, SUBSTRING), (partial, False, SUBSTRING)]
        hits = ranked.search_annotations("alpha zz", limit=None, require_all=True)
        assert [(h.annotation.id, h.matched_all) for h in hits] == [(both, True)]
        assert ids(ranked.search_annotations("alpha zzz", limit=None, require_all=True)) == []

    @pytest.mark.parametrize("require_all", [False, True])
    def test_the_fallback_runs_only_when_nothing_else_matched(self, lib, ranked, filler, require_all):
        """Rows only the fallback finds (every word inside a longer word)
        are left out as soon as tiers 1-3 find anything."""
        every_word = lib.add_annotation(filler, "alpha beta token")  # tier 1
        lib.add_annotation(filler, "prefixalpha prefixbeta")
        whole_query = lib.add_annotation(filler, "xgamma deltax")  # tier 2 only
        lib.add_annotation(filler, "prefixgamma prefixdelta")
        some_words = lib.add_annotation(filler, "kappa token")  # tier 3 only
        inside = lib.add_annotation(filler, "prefixkappa prefixomega")
        hits = ranked.search_annotations("alpha beta", limit=None, require_all=require_all)
        assert [(h.annotation.id, h.matched_all, h.method) for h in hits] == [(every_word, True, FTS)]
        hits = ranked.search_annotations("gamma delta", limit=None, require_all=require_all)
        assert [(h.annotation.id, h.matched_all, h.method) for h in hits] == [(whole_query, True, SUBSTRING)]
        hits = ranked.search_annotations("kappa omega", limit=None, require_all=require_all)
        if require_all:  # tier 3 is skipped: nothing matched, so the fallback runs
            assert [(h.annotation.id, h.matched_all, h.method) for h in hits] == [(inside, True, SUBSTRING)]
        else:
            assert [(h.annotation.id, h.matched_all, h.method) for h in hits] == [(some_words, False, FTS)]

    def test_stopwords_are_left_out(self, lib, ranked, filler):
        row = lib.add_annotation(filler, "habits shape us")
        assert ids(ranked.search_annotations("what about the habits")) == [row]

    def test_a_phrase(self, lib, ranked, filler):
        phrase = lib.add_annotation(filler, "good decision making")
        lib.add_annotation(filler, "making a good decision")
        hits = ranked.search_annotations('"decision making"', limit=None)
        assert ids(hits) == [phrase]


class TestPaging:
    @pytest.fixture
    def many(self, lib, filler):
        return [lib.add_annotation(filler, f"zebra {'zebra ' * (i % 4)}number {i}") for i in range(23)]

    def test_pages_concatenate_to_the_whole_list(self, ranked, many):
        everything = ids(ranked.search_annotations("zebra", limit=None))
        assert sorted(everything) == sorted(many)
        pages = []
        for offset in range(0, 30, 4):
            pages += ids(ranked.search_annotations("zebra", limit=4, offset=offset))
        assert pages == everything
        assert ids(ranked.search_annotations("zebra", offset=0)) == everything[:20]  # default limit 20
        assert ranked.search_annotations("zebra", offset=100) == []

    def test_a_page_ranks_only_the_hits_up_to_its_end(self, ranked, many, monkeypatch):
        sizes = []
        real = search._query

        def query(*args):
            hits = real(*args)
            sizes.append(len(hits))
            return hits

        monkeypatch.setattr(search, "_query", query)
        assert len(ranked.search_annotations("zebra", limit=4, offset=2)) == 4
        assert len(ranked.search_annotations("zebra", limit=None)) == 23
        assert sizes == [6, 23]

    @pytest.mark.parametrize("fts", [True, False])
    def test_pages_across_tiers(self, monkeypatch, lib, ranked, filler, fts):
        if not fts:
            monkeypatch.setattr(search, "_fts5", False)
        for i in range(5):
            lib.add_annotation(filler, f"decision making {i}")
        for i in range(4):
            lib.add_annotation(filler, f"indecision making {i}")
        for i in range(6):
            lib.add_annotation(filler, f"making bread {i}")
        for require_all in (False, True):
            everything = ids(ranked.search_annotations("decision making", limit=None, require_all=require_all))
            assert len(everything) == (9 if require_all else 15)
            for size in (1, 2, 3, 4, 7, 16):
                pages = []
                for offset in range(0, 17, size):
                    pages += ids(ranked.search_annotations("decision making", limit=size, offset=offset,
                                                           require_all=require_all))
                assert pages == everything, (require_all, size)

    def test_deleted_rows_ranked_first_keep_pages_whole(self, lib, ranked, filler, monkeypatch):
        """Rows deleted in Apple Books (known to the index) that would rank
        first are left out by the index itself: pages stay consistent and
        are never refilled from all the hits."""
        gone = [lib.add_annotation(filler, "zebra zebra zebra zebra", deleted=True) for _ in range(5)]
        live = [lib.add_annotation(filler, f"zebra plain row {i}") for i in range(7)]
        wants = []
        real = search._query
        monkeypatch.setattr(search, "_query", lambda *args: wants.append(args[-1]) or real(*args))
        everything = ids(ranked.search_annotations("zebra", limit=None))
        assert sorted(everything) == sorted(live)
        pages = []
        for offset in range(0, 9, 3):
            pages += ids(ranked.search_annotations("zebra", limit=3, offset=offset))
        assert pages == everything
        with_deleted = ids(ranked.search_annotations("zebra", limit=None, include_deleted=True))
        assert set(with_deleted[:5]) == set(gone)
        assert wants == [None, 3, 6, 9, None] and index_of(ranked).builds == 1

    def test_rows_deleted_since_the_check_beyond_the_ranked_hits(self, lib, ranked, clock):
        """More hits gone from the store than a page ranked: the page is
        filled from all the hits."""
        book = lib.add_book("B")
        rows = [lib.add_annotation(book, "alpha") for _ in range(6)]
        assert ids(ranked.search_annotations("alpha", limit=2)) == [rows[5], rows[4]]
        for pk in rows[3:]:
            lib.execute("annotations", "DELETE FROM ZAEANNOTATION WHERE Z_PK = ?", (pk,))
        assert ids(ranked.search_annotations("alpha", limit=2)) == [rows[2], rows[1]]
        assert index_of(ranked).builds == 1  # until the next check
        clock[0] += search._RECHECK
        assert ids(ranked.search_annotations("alpha", limit=2)) == [rows[2], rows[1]]
        assert index_of(ranked).builds == 2

    def test_a_row_deleted_since_the_check_with_an_offset(self, lib, ranked, clock):
        book = lib.add_book("B")
        rows = [lib.add_annotation(book, "alpha") for _ in range(6)]
        assert ids(ranked.search_annotations("alpha", limit=2, offset=1)) == [rows[4], rows[3]]
        lib.execute("annotations", "DELETE FROM ZAEANNOTATION WHERE Z_PK = ?", (rows[4],))
        assert ids(ranked.search_annotations("alpha", limit=2, offset=1)) == [rows[3], rows[2]]

    @pytest.mark.parametrize("limit", [0, -1, True, False, 1.5, "5", [1]])
    def test_bad_limits(self, ranked, many, limit):
        with pytest.raises(InvalidArgumentError):
            ranked.search_annotations("zebra", limit=limit)

    @pytest.mark.parametrize("offset", [-1, True, 0.5, "1"])
    def test_bad_offsets(self, ranked, many, offset):
        with pytest.raises(InvalidArgumentError):
            ranked.search_annotations("zebra", offset=offset)

    def test_huge_and_integral_limits(self, ranked, many):
        assert len(ranked.search_annotations("zebra", limit=2 ** 64)) == 23
        assert len(ranked.search_annotations("zebra", limit=5.0, offset=2.0)) == 5


class TestScope:
    def test_deleted_only_with_include_deleted(self, lib, ranked, filler):
        live = lib.add_annotation(filler, "zebra live")
        gone = lib.add_annotation(filler, "zebra gone", deleted=True)
        assert ids(ranked.search_annotations("zebra")) == [live]
        assert sorted(ids(ranked.search_annotations("zebra", include_deleted=True))) == [live, gone]

    def test_reading_positions_and_tombstones_never(self, lib, ranked, filler):
        live = lib.add_annotation(filler, "zebra")
        lib.add_annotation(filler, None, kind="reading_position",
                           raw={"ZANNOTATIONSELECTEDTEXT": "zebra", "ZANNOTATIONNOTE": "zebra"})
        lib.add_annotation(None, None, kind="tombstone")
        for deleted in (False, True):
            assert ids(ranked.search_annotations("zebra", include_deleted=deleted)) == [live]

    def test_bookmarks_and_orphans_are_included(self, lib, ranked, filler):
        mark = lib.add_annotation(filler, None, kind="bookmark", raw={"ZANNOTATIONNOTE": "zebra"})
        orphan = lib.add_annotation("NO-SUCH-ASSET", "zebra orphan")
        hits = ranked.search_annotations("zebra")
        assert sorted(ids(hits)) == [mark, orphan]
        assert {h.annotation.id: h.annotation.book for h in hits}[orphan] is None

    def test_book_id(self, lib, ranked, filler):
        other = lib.add_book("Other")
        mine = lib.add_annotation(filler, "zebra mine")
        lib.add_annotation(other, "zebra theirs")
        assert ids(ranked.search_annotations("zebra", book_id=filler["id"])) == [mine]
        assert ids(ranked.search_annotations("zebra", book_id=str(filler["id"]))) == [mine]
        book = ranked.get_book_by_id(filler["id"])
        assert ids(ranked.search_annotations("zebra", book_id=book)) == [mine]
        with pytest.raises(BookNotFoundError):
            ranked.search_annotations("zebra", book_id=999999)
        with pytest.raises(BookNotFoundError):
            ranked.search_annotations("", book_id=999999)  # checked even for an empty query

    def test_a_book_without_an_asset_id_has_no_hits(self, lib, ranked, filler):
        lib.add_annotation(filler, "zebra")
        book = lib.add_book("No asset id")
        lib.execute("library", "UPDATE ZBKLIBRARYASSET SET ZASSETID = NULL WHERE Z_PK = ?", (book["id"],))
        assert ranked.search_annotations("zebra", book_id=book["id"]) == []
        assert ranked.search_annotations("", book_id=book["id"]) == []

    def test_book_id_with_deleted(self, lib, ranked, filler):
        gone = lib.add_annotation(filler, "zebra gone", deleted=True)
        assert ranked.search_annotations("zebra", book_id=filler["id"]) == []
        assert ids(ranked.search_annotations("zebra", book_id=filler["id"], include_deleted=True)) == [gone]

    def test_hits_read_the_instance_library(self, lib, ranked, filler, library):
        library.add_book("default library book")
        row = lib.add_annotation(filler, "zebra")
        hit, = ranked.search_annotations("zebra")
        assert hit.annotation.id == row and hit.annotation.book.title == "Ranking Book"

    def test_nothing_to_search_for(self, ranked, lib, filler):
        lib.add_annotation(filler, "zebra")
        for query in ("", "   ", "\u200b", "\u0301"):
            assert ranked.search_annotations(query) == []

    def test_no_annotation_store(self, make_library):
        lib = make_library()
        book = lib.add_book("B")
        no_asset = lib.add_book("No asset id")
        lib.execute("library", "UPDATE ZBKLIBRARYASSET SET ZASSETID = NULL WHERE Z_PK = ?", (no_asset["id"],))
        shutil.rmtree(lib.annotation_path.parent)
        api = PyAppleBooks(data_dir=lib.data_dir)
        try:
            # Whatever the query and the book, as search_annotation_by_text.
            for query in ("zebra", "", "   ", "\u200b"):
                with pytest.raises(AnnotationStoreNotFoundError):
                    api.search_annotations(query)
                with pytest.raises(AnnotationStoreNotFoundError):
                    api.search_annotation_by_text(query)
                for book_id in (book["id"], no_asset["id"]):
                    with pytest.raises(AnnotationStoreNotFoundError):
                        api.search_annotations(query, book_id=book_id)
            with pytest.raises(BookNotFoundError):  # the book is checked first
                api.search_annotations("", book_id=999999)
        finally:
            api.close()

    def test_query_is_converted_like_the_substring_search(self, lib, ranked, filler):
        row = lib.add_annotation(filler, "in 2026 we")
        assert ids(ranked.search_annotations(2026)) == [row]
        assert [a.id for a in ranked.search_annotation_by_text(2026)] == [row]


# -- superset of search_annotation_by_text -------------------------------------------

WORDS = ["habit", "habits", "decision", "making", "indecision", "Görel", "GÖREL", "don’t", "don't",
         "ﬁnd", "find", "Straße", "strasse", "naïve", "co-operate", "e-mail", "—", "…", "...",
         "memory", "memories", "the", "of", "a", "learning", "x", "50%", "under_score", "“quoted”",
         "名前", "학교에", "café", "cafe\u0301", "\u00a0", "line\nbreak", "tab\tbed"]


def _sentence(rng) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 9)))


@pytest.fixture
def corpus(lib, ranked):
    rng = random.Random(77)
    books = [lib.add_book(f"Book {i}") for i in range(3)]
    texts = []
    for _ in range(120):
        sel = _sentence(rng)
        note = _sentence(rng) if rng.random() < 0.3 else None
        surrounding = f"{_sentence(rng)} {sel} {_sentence(rng)}" if rng.random() < 0.7 else None
        lib.add_annotation(rng.choice(books + ["ORPHAN"]), sel, note=note, deleted=rng.random() < 0.1,
                           raw=rep(surrounding))
        texts += [t for t in (sel, note, surrounding) if t]
    return texts


# Queries of 3 folded characters only with a space at an end.
EDGE_QUERIES = [" a ", " x ", " of", "of ", " ha", "it ", "ng ", "fe ", " e-", " co", "\u2028x ", " ...",
                " \u2014 ", "fe\u0301 ", "\u00a0of"]


def test_superset_of_the_substring_search(ranked, corpus):
    """For random fragments (cut mid-word, often at a space), frequent
    words and queries with a space at an end, ranked results include
    every annotation search_annotation_by_text finds: every query of 3
    or more folded characters, spaces included."""
    rng = random.Random(5)
    queries = [w for w in WORDS if len(search.fold_for_match(w)) >= 3]
    for _ in range(80):
        text = rng.choice(corpus)
        start = rng.randrange(len(text))
        queries.append(text[start:start + rng.randint(3, 20)])
    queries += ["decision making", "habit decision", "the habit", "of the"] + EDGE_QUERIES
    checked = violations = 0
    found_with_spaces = 0
    for query in queries:
        if len(search.fold_for_match(query)) < 3:
            continue
        for deleted in (False, True):
            want = {a.id for a in ranked.search_annotation_by_text(query, include_deleted=deleted)}
            got = set(ids(ranked.search_annotations(query, limit=None, include_deleted=deleted)))
            checked += 1
            violations += not want <= got
            found_with_spaces += bool(want) and len(search.fold_for_match(query).strip()) < 3
    assert checked > 100 and violations == 0
    assert found_with_spaces >= 10  # the edge queries do find rows


def test_superset_with_a_space_at_an_end(lib, ranked, filler):
    """' qz' has 3 folded characters but only 2 without its space: the
    substring search finds ' qz' inside 'xx qzwerty' (no word 'qz')."""
    row = lib.add_annotation(filler, "xx qzwerty yy")
    tail = lib.add_annotation(filler, "zzqz cd")
    for query, expected in ((" qz", [row]), ("qz ", [tail]), (" qzw", [row]), ("\u00a0qz", [row])):
        assert [a.id for a in ranked.search_annotation_by_text(query)] == expected
        hits = ranked.search_annotations(query, limit=None)
        assert ids(hits) == expected and hits[0].method is SUBSTRING and hits[0].matched_all


# -- freshness ---------------------------------------------------------------------


class TestFreshness:
    def test_an_insert_is_found_after_the_recheck(self, lib, ranked, clock):
        book = lib.add_book("B")
        first = lib.add_annotation(book, "alpha one")
        assert ids(ranked.search_annotations("alpha")) == [first]
        second = lib.add_annotation(book, "alpha two")
        clock[0] += search._RECHECK / 2
        assert ids(ranked.search_annotations("alpha")) == [first]  # not checked yet
        clock[0] += search._RECHECK
        assert sorted(ids(ranked.search_annotations("alpha"))) == [first, second]
        assert index_of(ranked).builds == 2

    def test_an_unchanged_store_is_not_rebuilt(self, lib, ranked, clock):
        lib.add_annotation(lib.add_book("B"), "alpha")
        for _ in range(3):
            ranked.search_annotations("alpha")
            clock[0] += search._RECHECK * 2
        assert index_of(ranked).builds == 1

    def test_a_soft_delete_disappears(self, lib, ranked, clock):
        book = lib.add_book("B")
        kept, gone = lib.add_annotation(book, "alpha kept"), lib.add_annotation(book, "alpha gone")
        assert sorted(ids(ranked.search_annotations("alpha"))) == [kept, gone]
        bump(lib, gone, ZANNOTATIONDELETED=1)
        clock[0] += search._RECHECK * 2
        assert ids(ranked.search_annotations("alpha")) == [kept]
        assert sorted(ids(ranked.search_annotations("alpha", include_deleted=True))) == [kept, gone]

    def test_a_text_edit_is_found(self, lib, ranked, clock):
        row = lib.add_annotation(lib.add_book("B"), "alpha")
        ranked.search_annotations("alpha")
        bump(lib, row, ZANNOTATIONSELECTEDTEXT="omega", ZANNOTATIONREPRESENTATIVETEXT="omega",
             ZANNOTATIONMODIFICATIONDATE=800000000.0)
        clock[0] += search._RECHECK * 2
        assert ids(ranked.search_annotations("omega")) == [row]
        assert ranked.search_annotations("alpha") == []

    def test_a_reading_position_update_does_not_rebuild(self, lib, ranked, clock):
        book = lib.add_book("B")
        lib.add_annotation(book, "alpha")
        position = lib.add_annotation(book, None, kind="reading_position", location="epubcfi(/6/2!/4/2)")
        ranked.search_annotations("alpha")
        for page in range(4, 12, 2):
            bump(lib, position, ZANNOTATIONLOCATION=f"epubcfi(/6/{page}!/4/2)",
                 ZANNOTATIONMODIFICATIONDATE=800000000.0 + page)
            clock[0] += search._RECHECK * 2
            ranked.search_annotations("alpha")
        assert index_of(ranked).builds == 1

    def test_a_replaced_store_rebuilds(self, lib, ranked, clock, tmp_path):
        lib.add_annotation(lib.add_book("B"), "alpha")
        ranked.search_annotations("alpha")
        copy = tmp_path / "copy.sqlite"
        shutil.copyfile(lib.annotation_path, copy)
        os.replace(copy, lib.annotation_path)  # same rows, a new file
        time.sleep(0.05)  # the library notices a new file within 20 ms
        clock[0] += search._RECHECK * 2
        assert len(ranked.search_annotations("alpha")) == 1
        assert index_of(ranked).builds == 2

    def test_a_row_deleted_before_the_recheck_is_skipped_and_the_page_filled(self, lib, ranked, clock):
        book = lib.add_book("B")
        rows = [lib.add_annotation(book, "alpha") for _ in range(4)]
        assert ids(ranked.search_annotations("alpha", limit=2)) == rows[:1:-1]
        lib.execute("annotations", "DELETE FROM ZAEANNOTATION WHERE Z_PK = ?", (rows[3],))
        index = index_of(ranked)
        for _ in range(2):
            assert ids(ranked.search_annotations("alpha", limit=2)) == [rows[2], rows[1]]
            assert index.builds == 1  # the index catches up at its next check
        clock[0] += search._RECHECK
        assert ids(ranked.search_annotations("alpha", limit=2)) == [rows[2], rows[1]]
        assert index.builds == 2

    def test_deletions_rebuild_at_most_once_per_recheck(self, lib, ranked, clock, monkeypatch):
        """Hits deleted since the check are skipped without fingerprinting
        the store again before ``_RECHECK`` has passed, however many."""
        book = lib.add_book("B")
        rows = [lib.add_annotation(book, "alpha") for _ in range(12)]
        prints = []
        real = search.AnnotationIndex._fingerprint
        monkeypatch.setattr(search.AnnotationIndex, "_fingerprint",
                            lambda self, db: prints.append(1) or real(self, db))
        assert len(ranked.search_annotations("alpha", limit=2)) == 2
        index = index_of(ranked)
        for second in range(3):
            for step in range(3):
                lib.execute("annotations", "DELETE FROM ZAEANNOTATION WHERE Z_PK = ?", (rows.pop(),))
                assert ids(ranked.search_annotations("alpha", limit=2)) == rows[:-3:-1]
                clock[0] += search._RECHECK / 4
            assert (len(prints), index.builds) == (second + 1, second + 1)
            clock[0] += search._RECHECK / 4

    def test_a_failed_fingerprint_keeps_the_index_under_a_ttl(self, lib, ranked, clock, monkeypatch):
        """A transient failure ('database is locked' while Apple Books
        writes) neither rebuilds the index nor, once over, rebuilds it
        again; one that lasts past the TTL does."""
        lib.add_annotation(lib.add_book("B"), "alpha")
        assert len(ranked.search_annotations("alpha")) == 1
        index, db = index_of(ranked), ranked._PyAppleBooks__library
        real, failing = db.execute, [True]

        def execute(sql, *args, **kwargs):
            if failing[0] and "total(" in sql:
                raise DBQueryError("Database query failed.")
            return real(sql, *args, **kwargs)

        monkeypatch.setattr(db, "execute", execute)
        clock[0] += search._RECHECK * 2
        assert len(ranked.search_annotations("alpha")) == 1
        assert index.builds == 1 and index._ready.ttl_at == clock[0] + search._TTL_WITHOUT_ZOPT
        failing[0] = False
        clock[0] += search._RECHECK * 2
        assert len(ranked.search_annotations("alpha")) == 1
        assert index.builds == 1 and index._ready.ttl_at is None  # verified again: no TTL
        failing[0] = True
        clock[0] += search._RECHECK * 2
        first_failure = clock[0]
        ranked.search_annotations("alpha")
        clock[0] += search._TTL_WITHOUT_ZOPT / 2
        ranked.search_annotations("alpha")
        assert index.builds == 1 and index._ready.ttl_at == first_failure + search._TTL_WITHOUT_ZOPT
        clock[0] += search._TTL_WITHOUT_ZOPT / 2
        assert len(ranked.search_annotations("alpha")) == 1
        assert index.builds == 2

    def test_a_build_never_goes_back_to_an_older_fingerprint(self, lib, ranked, clock):
        """Replays what two threads can interleave: one holding an older
        fingerprint reaches _build after the newer index (or the newer
        build in progress) exists, and gets that one."""
        book = lib.add_book("B")
        rows = [lib.add_annotation(book, "alpha one")]
        ranked.search_annotations("alpha")
        index, db = index_of(ranked), ranked._PyAppleBooks__library
        old_key, old_ttl = index._fingerprint(db)
        old_at = clock[0]
        rows.append(lib.add_annotation(book, "alpha two"))
        clock[0] += 1
        new_key, new_ttl = index._fingerprint(db)
        assert new_key != old_key
        newer = index._build(db, new_key, new_ttl, clock[0])
        assert newer.key == new_key and index.builds == 2
        assert index._build(db, old_key, old_ttl, old_at) is newer and index.builds == 2
        # A build in progress from a newer fingerprint is continued, not
        # restarted for the older one.
        rows.append(lib.add_annotation(book, "alpha three"))
        clock[0] += 1
        newest_key, _ = index._fingerprint(db)
        conn, fts = search._new_database()
        with index._state:
            retired = index._retire(index._ready)
            index._ready = None
            index._pending = search._Pending(conn, fts, newest_key, clock[0], None)
        search._close_quietly(retired)
        gen = index._build(db, new_key, new_ttl, clock[0] - 0.5)
        assert gen.key == newest_key and index.builds == 3
        assert sorted(ids(ranked.search_annotations("alpha"))) == rows

    def test_a_ttl_without_z_opt(self, lib, ranked, clock):
        lib.add_annotation(lib.add_book("B"), "alpha")
        lib.execute("annotations", "ALTER TABLE ZAEANNOTATION DROP COLUMN Z_OPT")
        assert len(ranked.search_annotations("alpha")) == 1
        index = index_of(ranked)
        assert index._ready.ttl_at == pytest.approx(clock[0] + search._TTL_WITHOUT_ZOPT)
        clock[0] += search._RECHECK * 2
        ranked.search_annotations("alpha")
        assert index.builds == 1
        clock[0] += search._TTL_WITHOUT_ZOPT
        ranked.search_annotations("alpha")
        assert index.builds == 2


# -- schema drift ------------------------------------------------------------------


class TestDrift:
    @pytest.mark.parametrize("column, still, gone", [
        ("ZANNOTATIONNOTE", ["sel", "rep"], ["note"]),
        ("ZANNOTATIONREPRESENTATIVETEXT", ["sel", "note"], ["rep"]),
    ])
    def test_a_missing_text_column(self, lib, ranked, column, still, gone):
        book = lib.add_book("B")
        found = {"sel": lib.add_annotation(book, "selword", raw=rep("plain")),
                 "note": lib.add_annotation(book, "plain", note="noteword", raw=rep("plain")),
                 "rep": lib.add_annotation(book, "plain", raw=rep("repword"))}
        lib.execute("annotations", f"ALTER TABLE ZAEANNOTATION DROP COLUMN {column}")
        for name in still:
            assert ids(ranked.search_annotations(f"{name}word")) == [found[name]]
        for name in gone:
            assert ranked.search_annotations(f"{name}word") == []

    def test_z_opt_dropped_between_two_searches(self, lib, ranked, clock):
        lib.add_annotation(lib.add_book("B"), "alpha")
        ranked.search_annotations("alpha")
        index = index_of(ranked)
        assert index._ready.ttl_at is None
        lib.execute("annotations", "ALTER TABLE ZAEANNOTATION DROP COLUMN Z_OPT")
        clock[0] += search._RECHECK * 2
        # The cached schema still names Z_OPT: that fingerprint fails, and
        # the index is kept under a TTL.
        assert len(ranked.search_annotations("alpha")) == 1  # no error
        assert index._ready.ttl_at == clock[0] + search._TTL_WITHOUT_ZOPT and index.builds == 1
        # The next one reads the schema again: a new key, one rebuild.
        clock[0] += search._RECHECK * 2
        assert len(ranked.search_annotations("alpha")) == 1
        assert index._ready.ttl_at == clock[0] + search._TTL_WITHOUT_ZOPT and index.builds == 2
        clock[0] += search._RECHECK * 2
        ranked.search_annotations("alpha")
        assert index.builds == 2

    def test_a_missing_type_column_raises_as_the_substring_search_does(self, lib, ranked):
        lib.add_annotation(lib.add_book("B"), "alpha")
        lib.execute("annotations", "ALTER TABLE ZAEANNOTATION DROP COLUMN ZANNOTATIONTYPE")
        with pytest.raises(DBQueryError) as substring_error:
            ranked.search_annotation_by_text("alpha")
        with pytest.raises(type(substring_error.value)):
            ranked.search_annotations("alpha")


# -- non-English text (G4.2) ------------------------------------------------------------


MULTILINGUAL = [
    # (stored text, query, hits, method of the hit)
    ("私の名前は太郎です", "名前", 1, SUBSTRING),
    ("她是一个好姑娘", "姑娘", 1, SUBSTRING),
    ("학교에 갔다", "학교", 1, SUBSTRING),
    ("ذهبت إلى المدرسة", "مدرسة", 1, SUBSTRING),
    ("Don’t wait", "don't", 1, FTS),
    ("don't stop", "don’t", 1, FTS),
    ("to deﬁne terms", "define", 1, FTS),
    ("to define terms", "deﬁne", 1, FTS),
    ("Die Straße ist lang", "strasse", 1, FTS),
    ("die strasse", "Straße", 1, FTS),
    ("wait… what", "...", 1, SUBSTRING),
    ("and so on...", "…", 1, SUBSTRING),
    ("ばかな話", "はか", 0, None),
    ("ばかな話", "ばか", 1, SUBSTRING),
    ("Kurt Görel", "GÖREL", 1, FTS),
    ("Kurt Görel", "Go\u0308rel", 1, FTS),
    ("Kurt Gorel", "Görel", 1, FTS),
    ("मैं किताब पढ़ता हूँ", "किताब", 1, FTS),
    ("книги на полке", "книги", 1, FTS),
]


@pytest.mark.parametrize("text, query, count, method", MULTILINGUAL)
def test_multilingual(lib, ranked, text, query, count, method):
    book = lib.add_book("B")
    lib.add_annotation(book, "unrelated filler text")
    lib.add_annotation(book, text)
    hits = ranked.search_annotations(query)
    assert len(hits) == count
    if count:
        assert hits[0].method is method and hits[0].matched_all


def test_routing():
    assert search._plan("名前 name").substring
    assert search._plan("ทดสอบ").substring and search._plan("ពាក្យ").substring
    assert search._plan("မြန်မာ").substring and search._plan("ພາສາ").substring
    assert search._plan("?!").substring
    assert not search._plan("naïve café").substring


# -- without FTS5 ------------------------------------------------------------------------


def test_without_fts5(monkeypatch, lib, ranked):
    monkeypatch.setattr(search, "_fts5", False)
    assert search.fts5_available() is False
    book = lib.add_book("B")
    both = lib.add_annotation(book, "decision making")
    partial = lib.add_annotation(book, "making bread")
    hits = ranked.search_annotations("decision making", limit=None)
    assert index_of(ranked)._ready.fts is None
    assert ids(hits) == [both, partial]
    assert [(h.matched_all, h.method) for h in hits] == [(True, SUBSTRING), (False, SUBSTRING)]


def test_dates_are_untouched(lib, ranked):
    created = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
    lib.add_annotation(lib.add_book("B"), "alpha", created=created)
    hit, = ranked.search_annotations("alpha")
    assert hit.annotation.creation_date == ranked.list_annotations()[0].creation_date
