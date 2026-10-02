"""``get_vocabulary`` and ``VocabularyEntry`` (1.11, Tier B). Synthetic
words only."""

import datetime as dt
import sqlite3
import time

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.engagement import VocabularyEntry
from py_apple_books.exceptions import BookNotFoundError, InvalidArgumentError, InvalidChoiceError
from py_apple_books.text import is_short_selection
from py_apple_books.utils import APPLE_EPOCH_OFFSET
from tests import engagement_helpers


def local(*args) -> float:
    return dt.datetime(*args).timestamp() - APPLE_EPOCH_OFFSET


def terms(entries) -> list:
    return [e.term for e in entries]


@pytest.fixture
def words(library):
    one, two = library.add_book("One"), library.add_book("Two")
    r = {
        "one": one, "two": two,
        "e1": library.add_annotation(one, "Ephemeral,", created=local(2026, 1, 5),
                                     raw={"ZANNOTATIONREPRESENTATIVETEXT": "Ephemeral,"}),
        "e2": library.add_annotation(two, "ephemeral", kind="underline", note="fleeting",
                                     created=local(2026, 3, 1),
                                     raw={"ZANNOTATIONREPRESENTATIVETEXT": "Joy is ephemeral, they said."}),
        "e3": library.add_annotation(one, "EPHEMERAL", created=local(2026, 2, 1),
                                     raw={"ZANNOTATIONREPRESENTATIVETEXT": "A different sentence."}),
        "laconic": library.add_annotation(one, "laconic", kind="note", note="brief", created=local(2025, 6, 1)),
        "medias": library.add_annotation(two, "in medias res", created=local(2026, 4, 1)),
        "orphan": library.add_annotation("GONE-ASSET", "liminal", created=local(2024, 1, 1)),
        "passage": library.add_annotation(one, "A whole sentence that is not a word.", created=local(2026, 5, 1)),
        "deleted": library.add_annotation(one, "sonder", deleted=True, created=local(2026, 5, 2)),
        "undated": library.add_annotation(two, "petrichor", created=None),
        "bookmark": library.add_annotation(one, None, kind="bookmark"),
        "tombstone": library.add_annotation(None, None, kind="tombstone"),
    }
    return r


def test_grouping(api, words):
    entries = {e.key: e for e in api.get_vocabulary()}
    assert set(entries) == {"ephemeral", "laconic", "in medias res", "liminal", "petrichor"}
    eph = entries["ephemeral"]
    assert eph.count == 3 and eph.term == "ephemeral"   # the newest highlight's text
    assert [a.id for a in eph.annotations] == [words["e2"], words["e3"], words["e1"]]
    assert eph.notes == ("fleeting",)
    assert set(eph.asset_ids) == {words["one"]["asset_id"], words["two"]["asset_id"]}
    assert eph.asset_ids[0] == words["two"]["asset_id"]
    assert eph.first_highlighted == dt.datetime(2026, 1, 5) and eph.last_highlighted == dt.datetime(2026, 3, 1)
    assert entries["petrichor"].first_highlighted is None and entries["petrichor"].last_highlighted is None


def test_context(api, words):
    entries = {e.key: e for e in api.get_vocabulary()}
    assert entries["ephemeral"].context == "Joy is ephemeral, they said."
    assert entries["laconic"].context is None          # representative text equals the word
    assert entries["in medias res"].context is None


def test_context_skips_text_without_the_word(api, library):
    book = library.add_book("B")
    library.add_annotation(book, "word", created=local(2026, 1, 2),
                           raw={"ZANNOTATIONREPRESENTATIVETEXT": "Nothing related here."})
    library.add_annotation(book, "Word", created=local(2026, 1, 1),
                           raw={"ZANNOTATIONREPRESENTATIVETEXT": "The last word on it."})
    (entry,) = api.get_vocabulary()
    assert entry.context == "The last word on it."


def test_entry_type(api, words):
    entry = api.get_vocabulary()[0]
    assert isinstance(entry, VocabularyEntry)
    with pytest.raises(TypeError):
        hash(entry)
    with pytest.raises(AttributeError):
        entry.term = "x"


def test_filters(api, words):
    assert terms(api.get_vocabulary(order_by="term", underline_only=True)) == ["ephemeral"]
    assert terms(api.get_vocabulary(order_by="term", book_id=words["two"]["id"])) == [
        "ephemeral", "in medias res", "petrichor"]
    book = api.get_book_by_id(words["one"]["id"])
    assert [e.count for e in api.get_vocabulary(order_by="term", book_id=book)] == [2, 1]
    assert terms(api.get_vocabulary(order_by="term", after=dt.date(2026, 1, 1),
                                    before=dt.date(2026, 3, 31))) == ["ephemeral"]
    assert terms(api.get_vocabulary(order_by="term", before=dt.datetime(2025, 6, 1))) == ["laconic", "liminal"]
    with pytest.raises(BookNotFoundError):
        api.get_vocabulary(book_id=12345)


@pytest.mark.parametrize("order_by, expected", [
    ("-last_highlighted", ["in medias res", "ephemeral", "laconic", "liminal", "petrichor"]),
    ("last_highlighted", ["liminal", "laconic", "ephemeral", "in medias res", "petrichor"]),
    ("first_highlighted", ["liminal", "laconic", "ephemeral", "in medias res", "petrichor"]),
    ("-first_highlighted", ["in medias res", "ephemeral", "laconic", "liminal", "petrichor"]),
    ("term", ["ephemeral", "in medias res", "laconic", "liminal", "petrichor"]),
    ("-term", ["petrichor", "liminal", "laconic", "in medias res", "ephemeral"]),
    ("-count", ["ephemeral", "in medias res", "laconic", "liminal", "petrichor"]),
    ("count", ["in medias res", "laconic", "liminal", "petrichor", "ephemeral"]),
])
def test_orders(api, words, order_by, expected):
    assert [e.key for e in api.get_vocabulary(order_by=order_by)] == expected


def test_paging(api, words):
    full = [e.key for e in api.get_vocabulary(order_by="term")]
    assert [e.key for e in api.get_vocabulary(2, "term")] == full[:2]
    assert [e.key for e in api.get_vocabulary(2, "term", offset=4)] == full[4:]
    assert api.get_vocabulary(offset=50) == []


@pytest.mark.parametrize("kwargs, error", [
    ({"order_by": "count desc"}, InvalidChoiceError), ({"order_by": None}, InvalidChoiceError),
    ({"order_by": "-creation_date"}, InvalidChoiceError), ({"limit": 0}, InvalidArgumentError),
    ({"offset": -1}, InvalidArgumentError), ({"after": "2026"}, InvalidArgumentError),
])
def test_validation(api, kwargs, error):
    with pytest.raises(error):
        api.get_vocabulary(**kwargs)


def test_empty_library(api):
    assert api.get_vocabulary() == []


def test_books_load_in_one_query(api, words, sql_trace):
    entries = api.get_vocabulary()
    before = len(sql_trace)
    titles = {a.book.title if a.book else None for e in entries for a in e.annotations}
    assert titles == {"One", "Two", None} and len(sql_trace) - before == 1


def test_prefilter_is_exact(make_library):
    """Every text the classifier accepts passes `length(...) <= 64`, for
    texts around the gate, with NUL and invalid UTF-8 (its own library:
    invalid UTF-8 makes a library read text leniently for good)."""
    library = make_library()
    book = library.add_book("Edges")
    texts = []
    for n in range(58, 72):
        texts += [" " * (n - 4) + "word", "w" + "é" * (n - 1), "ab" + "\x00" * (n - 2),
                  "x" * min(n, 30) + " " * max(0, n - 30)]
    for text in texts:
        library.add_annotation(book, text, created=local(2026, 1, 1))
    # The last: 50 characters in 70 bytes (as a BLOB, length() counts bytes).
    raws = [b"ab\xffcd", b"\xe2\x82word", b" " * 62 + b"\xff\xfe", b"\xc3" * 70,
            "\u00e9".encode() * 20 + b" " * 30]
    # Each raw value twice: as a TEXT cell (invalid UTF-8) and as a BLOB.
    cells = [(pk, raw, as_text) for raw in raws for as_text in (True, False)
             for pk in [library.add_annotation(book, "placeholder", created=local(2026, 1, 1))]]
    con = sqlite3.connect(library.annotation_path)
    for pk, raw, as_text in cells:
        cast = "CAST(? AS TEXT)" if as_text else "?"
        con.execute(f"UPDATE ZAEANNOTATION SET ZANNOTATIONSELECTEDTEXT = {cast} WHERE Z_PK = ?", (raw, pk))
    con.commit()
    rows = con.execute("SELECT Z_PK, CAST(ZANNOTATIONSELECTEDTEXT AS BLOB) FROM ZAEANNOTATION").fetchall()
    con.close()
    accepted = {pk for pk, raw in rows if is_short_selection(raw)}
    assert accepted, "the case table should have short selections"
    with LibraryDB(data_dir=library.data_dir) as db, use_library(db):
        found = {a.id for e in PyAppleBooks().get_vocabulary() for a in e.annotations}
    assert found == accepted


def test_without_selected_text(make_library, sql_trace):
    lib = make_library()
    lib.add_annotation(lib.add_book("B"), "word")
    lib.execute("annotations", "ALTER TABLE ZAEANNOTATION DROP COLUMN ZANNOTATIONSELECTEDTEXT")
    with LibraryDB(data_dir=lib.data_dir) as db, use_library(db):
        api = PyAppleBooks()
        assert api.get_vocabulary() == []
        before = len(sql_trace)
        assert api.get_vocabulary() == []
        assert len(sql_trace) == before   # no query, no schema re-read


@pytest.mark.parametrize("count", [10_000] + ([50_000] if engagement_helpers.SLOW else []))
def test_time(api, library, count):
    library.populate(books=100, annotations_per_book=count // 100)
    engagement_helpers.realistic_texts(library)
    texts = library.execute("annotations", "SELECT ZANNOTATIONSELECTEDTEXT FROM ZAEANNOTATION")
    shorts = sum(is_short_selection(t) for (t,) in texts)
    api.get_vocabulary(limit=1)
    start = time.perf_counter()
    entries = api.get_vocabulary()
    elapsed = time.perf_counter() - start
    assert sum(e.count for e in entries) == shorts
    budget = (0.25 if count == 10_000 else 1.0) if engagement_helpers.SLOW else 3.0
    assert elapsed < budget, f"{count}: {elapsed:.3f} s"
