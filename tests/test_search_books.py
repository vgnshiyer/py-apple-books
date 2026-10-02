"""``PyAppleBooks.search_books`` (1.11): the books whose title or author
contains every word of a query, folded, in one SQL statement; a superset
of apple-books-mcp 0.9's ``search_books`` filter for every query."""

import random
import re
import unicodedata
from typing import Optional

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.exceptions import InvalidArgumentError
from py_apple_books.testing import STORE_SERIES
from py_apple_books.text import fold_for_match

# -- the oracle: apple-books-mcp 0.9.0's filter (apple_books_mcp/utils.py
# _fold, _search_needle, _book_matches over list_books()), copied --------

_FOLD_WHITESPACE = re.compile(r"\s+")
_FOLD_MAP: dict = {}
for _ch in "’‘‚‛′‵‹›ʼ＇":
    _FOLD_MAP[ord(_ch)] = "'"
for _ch in "“”„‟″‶«»＂":
    _FOLD_MAP[ord(_ch)] = '"'
for _ch in "‐‑‒–—―−﹘﹣－":
    _FOLD_MAP[ord(_ch)] = "-"
for _cp in (0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF):
    _FOLD_MAP[_cp] = None
for _cp in range(0x0300, 0x0370):
    _FOLD_MAP[_cp] = None
del _ch, _cp


def _fold(text) -> Optional[str]:
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if text.isascii():
        return _FOLD_WHITESPACE.sub(" ", text).lower()
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    text = unicodedata.normalize("NFKD", text.casefold()).casefold().translate(_FOLD_MAP)
    return _FOLD_WHITESPACE.sub(" ", unicodedata.normalize("NFC", text))


def _search_needle(query) -> Optional[str]:
    needle = _fold(query)
    if needle is None or (str(query).strip() and not needle.strip()):
        return None
    return needle


def _book_matches(book, needle: str) -> bool:
    for value in (getattr(book, "title", None), getattr(book, "author", None)):
        folded = _fold(value)
        if folded is not None and needle in folded:
            return True
    return False


def mcp09(api, query) -> list:
    """The ids apple-books-mcp 0.9's search_books lists for ``query``."""
    needle = _search_needle(query)
    if needle is None:
        return []
    return sorted({b.id for b in api.list_books() if _book_matches(b, needle)})


# -- fixtures --------------------------------------------------------------------

BOOKS = [
    ("The Lantern and me", "Q. R. Ostrander"),
    ("x y", "Anon"),
    ("Görel, Esker, Bakh", "Dorian Halvorsen"),
    ("Don’t Wait", "Some—One"),
    ("ﬁnding Strasse", "Straße Author"),
    ("Lantern", None),
    ("50% Off", "C++ Guy"),
    ("under_score", "A. Writer"),
    ("Lantern Tales", "Another Ostrander"),
    (None, "Untitled Author"),
    ("Naïve Café", "Zoë Q"),
]


@pytest.fixture
def lib(make_library):
    lib = make_library()
    lib.ids = {title: lib.add_book(title, author)["id"] for title, author in BOOKS}
    lib.series = lib.add_book("Series Lantern Volume", "Q. R. Ostrander", data_source=STORE_SERIES)["id"]
    lib.container = lib.add_book("Lantern Series", "Q. R. Ostrander", data_source=STORE_SERIES,
                                 content_type=5)["id"]
    return lib


@pytest.fixture
def books(lib):
    api = PyAppleBooks(data_dir=lib.data_dir)
    yield api
    api.close()


def found(api, query, **kwargs) -> list:
    return sorted(b.id for b in api.search_books(query, **kwargs))


def titles(api, query, **kwargs) -> list:
    return sorted(str(b.title) for b in api.search_books(query, **kwargs))


# -- matching --------------------------------------------------------------------------


class TestMatching:
    def test_title_author_and_both(self, books):
        assert titles(books, "wait") == ["Don’t Wait"]
        assert titles(books, "halvorsen") == ["Görel, Esker, Bakh"]
        assert titles(books, "lantern ostrander") == ["Lantern Tales", "The Lantern and me"]
        assert titles(books, "ostrander lantern") == titles(books, "lantern ostrander")
        assert titles(books, "lantern tales ostrander") == ["Lantern Tales"]
        assert titles(books, "lantern nobody") == []

    def test_folded(self, books):
        assert titles(books, "GOREL bakh") == ["Görel, Esker, Bakh"]
        assert titles(books, "don't") == ["Don’t Wait"]
        assert titles(books, "some-one") == ["Don’t Wait"]
        assert titles(books, "finding") == ["ﬁnding Strasse"]
        assert titles(books, "STRASSE author") == ["ﬁnding Strasse"]
        assert titles(books, "naive cafe zoe") == ["Naïve Café"]

    def test_percent_and_underscore_match_themselves(self, books):
        assert titles(books, "50%") == ["50% Off"]
        assert titles(books, "%") == ["50% Off"]
        assert titles(books, "_") == ["under_score"]
        assert titles(books, "c++") == ["50% Off"]

    def test_a_word_may_be_part_of_a_longer_one(self, books):
        assert titles(books, "lant ostr") == ["Lantern Tales", "The Lantern and me"]

    def test_empty_whitespace_and_folded_away(self, lib, books):
        everything = sorted(lib.ids.values())
        assert found(books, "") == everything  # every book with a title or an author
        # Whitespace: every book whose title or author has a space.
        spaced = sorted(lib.ids[t] for t, a in BOOKS if " " in (t or "") or " " in (a or ""))
        assert found(books, "   ") == spaced and len(spaced) < len(everything)
        for query in ("\u200b", "\u0301", "´"):
            assert found(books, query) == []

    def test_at_most_32_words(self, lib, books):
        long_title = " ".join(f"w{i:02d}" for i in range(32))
        book = lib.add_book(long_title, "Long Author")["id"]
        query = " ".join(f"w{i:02d}" for i in range(40))  # w32..w39 aren't in the title
        assert found(books, query) == [book]
        assert found(books, "w99 " + long_title) == []  # the first 32 words count

    def test_non_str_queries(self, lib, books):
        assert found(books, 50) == [lib.ids["50% Off"]]
        assert found(books, None) == found(books, "none")


# -- parity with apple-books-mcp 0.9 --------------------------------------------------

EXACT = ["lantern", "gorel", "don't", "-one", "finding", "strasse", "50%", "c++", "_", "%", "author",
         "", "   ", "\u200b", "´", "x", "e", "ostrander"]
SUPERSET = ["lantern \u200b", "lantern \u0301", "lantern ´", "x ¨", " lantern", "lantern ostrander", "gorel bakh",
            "the lantern", "esker, bakh"]


class TestParityWithMcp09:
    @pytest.mark.parametrize("query", EXACT)
    def test_exact(self, books, query):
        assert found(books, query) == mcp09(books, query)

    @pytest.mark.parametrize("query", SUPERSET)
    def test_superset(self, books, query):
        assert set(found(books, query)) >= set(mcp09(books, query))

    def test_the_counterexamples_of_splitting_before_folding(self, books):
        """Splitting the raw query and folding each word made these match
        nothing, where 0.9 matches."""
        for query in ("lantern \u200b", "lantern \u0301", "lantern ´", "x ¨"):
            assert mcp09(books, query), query
            assert set(found(books, query)) >= set(mcp09(books, query)), query
        assert found(books, "x ¨") == mcp09(books, "x ¨")

    def test_seeded_needles(self, lib, books):
        """Every word of the titles and authors (exact), and random
        windows of 2-3 words and of characters (superset)."""
        texts = [t for pair in BOOKS for t in pair if t]
        words = {w for t in texts for w in fold_for_match(t).split(" ") if w}
        for word in sorted(words):
            assert found(books, word) == mcp09(books, word), word
        rng = random.Random(23)
        for _ in range(60):
            text = rng.choice(texts)
            parts = text.split(" ")
            start = rng.randrange(len(parts))
            window = " ".join(parts[start:start + rng.randint(2, 3)])
            cut = rng.randrange(len(text))
            chars = text[cut:cut + rng.randint(1, 8)]
            for query in (window, chars):
                assert set(found(books, query)) >= set(mcp09(books, query)), query


# -- scope, paging, statements, drift --------------------------------------------------


class TestScope:
    def test_store_series_rows(self, lib, books):
        assert lib.series not in found(books, "lantern")
        assert lib.series not in found(books, "series lantern volume ostrander")
        assert found(books, "series lantern volume", include_store_series=True) == [lib.series]
        assert lib.container in found(books, "lantern", include_store_series=True)

    def test_paging_follows_the_id_order(self, lib, books):
        everything = found(books, "")
        result = books.search_books("")
        assert result.count() == len(result) == len(everything)
        assert [b.id for b in result[0:4]] == everything[:4]
        assert [b.id for b in result[4:8]] == everything[4:8]
        assert [b.id for b in books.search_books("", limit=3, offset=3)] == everything[3:6]
        assert [b.id for b in books.search_books("lantern", order_by="-id")] == sorted(
            found(books, "lantern"), reverse=True)

    @pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": -1}, {"limit": True}, {"limit": "5"},
                                        {"limit": 1.5}, {"offset": -1}, {"offset": True}])
    def test_bad_limits(self, books, kwargs):
        with pytest.raises(InvalidArgumentError):
            books.search_books("lantern", **kwargs)

    def test_good_limits(self, books):
        assert len(books.search_books("", limit=2 ** 64)) == len(books.search_books(""))
        assert len(books.search_books("", limit=2.0, offset=1.0)) == 2


def test_one_statement_with_the_query_in_parameters_only(books, sql_trace):
    hits = list(books.search_books("lantern ostrander"))
    assert len(hits) == 2 and len(sql_trace) == 1
    sql, params = sql_trace[0]
    assert "lantern" not in sql.lower() and "ostrander" not in sql.lower()
    assert {"lantern", "ostrander"} <= set(params)
    del sql_trace[:]
    assert books.search_books("lantern").count() == 3 and len(sql_trace) == 1


def test_without_an_author_column_titles_only(lib, books):
    lib.execute("library", "ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZAUTHOR")
    books.close()  # read the schema again
    assert titles(books, "ostrander") == []
    assert titles(books, "lantern tales") == ["Lantern Tales"]
    assert found(books, "") == sorted(i for t, i in lib.ids.items() if t is not None)
