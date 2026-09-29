"""Read-path tests: every ``PyAppleBooks`` read method runs its real SQL
against a synthetic library built from the committed schema fixture.

Calls use the exact shapes apple-books-mcp 0.8.2 makes (keyword
``limit=``, string ids from ``describe_*``, ``order_by`` on the
currently-reading resource), and assertions are limited to behaviour
that must survive 1.10: the seeded rows are all owned books and live
annotations, unordered results are compared as id sets, and storage
order is never assumed. Known 1.9.1 defects the 1.10 fixes change are
pinned separately under ``tests/known/``.
"""

import datetime as dt

import pytest

from py_apple_books.exceptions import (
    AppleBooksError,
    BookNotDownloadedError,
    CollectionNotFoundError,
    DRMProtectedError,
)
from py_apple_books.testing import write_epub

UTC = dt.timezone.utc
HUGE_IDS = [2**63, 10**20]


def day(n: int) -> dt.datetime:
    return dt.datetime(2026, 9, n, 12, 0, tzinfo=UTC)


def ids(rows) -> set:
    return {row.id for row in rows}


def cfi(chapter_id: str, step: int, *, point: bool = False) -> str:
    if point:
        return f"epubcfi(/6/{step}[{chapter_id}]!/4/4/1:0)"
    return f"epubcfi(/6/{step}[{chapter_id}]!/4/4,/1:0,/1:21)"


@pytest.fixture
def seeded(library, tmp_path):
    """Three owned books, two collections (plus a deleted one) and live
    annotations; the in-progress book has a readable EPUB."""
    epub = write_epub(tmp_path / "Synthetic Book.epub", "Synthetic Book")
    reading = library.add_book("Synthetic Book", genre="Fiction", progress=0.42,
                               last_opened=day(20), created=day(1), path=epub)
    done = library.add_book("Second Book", "Second Author", genre="Science Fiction",
                            finished=True, progress=1.0, last_opened=day(10), created=day(2))
    new = library.add_book("Unopened Book", genre="History", created=day(3))

    shelf = library.add_collection("Shelf")
    library.add_to_collection(shelf, reading)
    library.add_to_collection(shelf, done)
    empty = library.add_collection("Empty Shelf")
    gone = library.add_collection("Gone", deleted=True)

    return {
        "reading": reading["id"], "done": done["id"], "new": new["id"],
        "shelf": shelf["id"], "empty": empty["id"], "gone": gone["id"],
        "highlight": library.add_annotation(
            reading, "a synthetic highlight", created=day(5), location=cfi("chap1", 4)),
        "note": library.add_annotation(
            reading, "Opening words.", kind="note", note="beta note", color="green",
            created=day(10), location=cfi("chap2", 6)),
        "blue": library.add_annotation(
            reading, "Closing words.", color="blue", created=day(15), location=cfi("chap1", 4)),
        "towel": library.add_annotation(done, "towel day", color="purple", created=day(12)),
        "position": library.add_annotation(
            reading, None, kind="reading_position", created=day(25),
            location=cfi("chap1", 4, point=True)),
    }


class TestCollections:
    def test_empty_library(self, api):
        assert list(api.list_collections(limit=None)) == []

    def test_list_excludes_deleted(self, api, seeded):
        assert ids(api.list_collections(limit=None)) == {seeded["shelf"], seeded["empty"]}
        assert len(list(api.list_collections(limit=1))) == 1

    @pytest.mark.parametrize("as_str", [False, True])
    def test_get_by_id(self, api, seeded, as_str):
        cid = str(seeded["shelf"]) if as_str else seeded["shelf"]
        collection = api.get_collection_by_id(cid)
        assert collection.title == "Shelf"
        assert ids(collection.books) == {seeded["reading"], seeded["done"]}
        assert list(api.get_collection_by_id(seeded["empty"]).books) == []

    @pytest.mark.parametrize("key", ["gone", None])
    def test_missing_or_deleted_raises(self, api, seeded, key):
        cid = seeded[key] if key else 9999
        with pytest.raises(CollectionNotFoundError) as exc:
            api.get_collection_by_id(str(cid))
        assert isinstance(exc.value, IndexError)  # MCP 0.8.2 catches IndexError

    def test_get_by_title(self, api, seeded):
        assert ids(api.get_collection_by_title("Shel")) == {seeded["shelf"], seeded["empty"]}
        assert ids(api.get_collection_by_title("Empty")) == {seeded["empty"]}
        assert list(api.get_collection_by_title("Gone")) == []


class TestBooks:
    def test_empty_library(self, api):
        assert list(api.list_books(limit=None)) == []
        assert list(api.get_books_in_progress(limit=None)) == []
        assert list(api.get_recently_read_books(limit=10)) == []

    def test_list_books(self, api, seeded):
        assert ids(api.list_books(limit=None)) == {seeded["reading"], seeded["done"], seeded["new"]}
        assert len(list(api.list_books(limit=2))) == 2

    @pytest.mark.parametrize("as_str", [False, True])
    def test_get_book_by_id(self, api, seeded, as_str):
        bid = str(seeded["reading"]) if as_str else seeded["reading"]
        book = api.get_book_by_id(bid)
        assert book.id == seeded["reading"]
        assert (book.title, book.author, book.genre) == ("Synthetic Book", "Test Author", "Fiction")
        assert book.reading_progress == pytest.approx(42.0)
        assert not book.is_finished
        assert book.progress_status == "In Progress (42.0%)"
        # Compared as instants: the datetime's zone isn't part of the contract.
        assert book.last_opened_date.timestamp() == day(20).timestamp()
        assert f"Last Read: {book.last_opened_date:%Y-%m-%d}" in book.format_progress_summary()
        assert api.get_book_by_id(seeded["done"]).is_finished

    @pytest.mark.parametrize("bid", [9999, "9999"] + HUGE_IDS)
    def test_get_book_by_id_missing_raises_indexerror(self, api, seeded, bid):
        with pytest.raises(IndexError):
            api.get_book_by_id(bid)

    def test_get_book_by_title(self, api, seeded):
        assert ids(api.get_book_by_title("Synth")) == {seeded["reading"]}
        assert ids(api.get_book_by_title("Book")) == {seeded["reading"], seeded["done"], seeded["new"]}
        assert list(api.get_book_by_title("no such title")) == []

    def test_get_books_by_genre(self, api, seeded):
        assert ids(api.get_books_by_genre("Fiction", limit=None)) == {seeded["reading"], seeded["done"]}
        assert len(list(api.get_books_by_genre("Fiction", limit=1))) == 1
        assert list(api.get_books_by_genre("Poetry", limit=None)) == []

    def test_in_progress(self, api, seeded):
        in_progress = ids(api.get_books_in_progress(limit=None))
        assert seeded["reading"] in in_progress
        assert seeded["new"] not in in_progress

    def test_currently_reading_resource_shape(self, api, seeded):
        """The MCP ``apple-books://currently-reading`` resource call."""
        books = list(api.get_books_in_progress(limit=1, order_by="-last_opened_date"))
        assert [b.id for b in books] == [seeded["reading"]]

    def test_finished_and_unstarted(self, api, seeded):
        assert ids(api.get_finished_books(limit=None)) == {seeded["done"]}
        assert ids(api.get_unstarted_books(limit=None)) == {seeded["new"]}

    def test_recently_read(self, api, seeded):
        """Ordered by last-opened date, newest first; never-opened books
        are left out."""
        assert [b.id for b in api.get_recently_read_books(limit=10)] == [seeded["reading"], seeded["done"]]
        assert [b.id for b in api.get_recently_read_books(limit=1)] == [seeded["reading"]]


class TestAnnotations:
    def test_empty_library(self, api):
        assert list(api.list_annotations()) == []
        assert api.search_annotation_by_text("anything", limit=None) == []

    def test_list_excludes_reading_position(self, api, seeded):
        expected = {seeded["highlight"], seeded["note"], seeded["blue"], seeded["towel"]}
        assert ids(api.list_annotations()) == expected
        assert seeded["position"] not in ids(api.list_annotations(limit=None, order_by="-creation_date"))

    def test_recent_annotations_shape(self, api, seeded):
        """``list_annotations(limit=, order_by='-creation_date')``: newest first."""
        got = [a.id for a in api.list_annotations(limit=2, order_by="-creation_date")]
        assert got == [seeded["blue"], seeded["towel"]]

    def test_by_color(self, api, seeded):
        assert ids(api.get_annotations_by_color("yellow", limit=None)) == {seeded["highlight"]}
        assert ids(api.get_annotations_by_color("green", limit=5)) == {seeded["note"]}
        assert ids(api.get_annotations_by_color("purple", limit=None)) == {seeded["towel"]}
        assert list(api.get_annotations_by_color("pink", limit=None)) == []

    def test_search_highlighted_text(self, api, seeded):
        assert ids(api.search_annotation_by_highlighted_text("synthetic")) == {seeded["highlight"]}

    def test_search_note(self, api, seeded):
        assert ids(api.search_annotation_by_note("beta", limit=None)) == {seeded["note"]}
        assert list(api.search_annotation_by_note("nothing", limit=None)) == []

    def test_search_text_returns_list(self, api, seeded):
        got = api.search_annotation_by_text("words", limit=None)
        assert isinstance(got, list)
        assert ids(got) == {seeded["note"], seeded["blue"]}
        assert ids(api.search_annotation_by_text("beta", limit=None)) == {seeded["note"]}  # note body
        limited = api.search_annotation_by_text("words", limit=1)
        assert isinstance(limited, list) and len(limited) == 1
        assert ids(limited) <= {seeded["note"], seeded["blue"]}

    def test_date_range(self, api, seeded):
        """MCP passes naive dates parsed from YYYY-MM-DD. The bounds are
        more than a day from the seeded rows, so the result is the same
        in every local zone."""
        after, before = dt.datetime(2026, 9, 7), dt.datetime(2026, 9, 14)
        got = api.get_annotations_by_date_range(after=after, before=before, limit=None)
        assert ids(got) == {seeded["note"], seeded["towel"]}
        assert len(list(api.get_annotations_by_date_range(after=after, before=before, limit=1))) == 1
        assert ids(api.get_annotations_by_date_range(after=None, before=None, limit=None)) == ids(api.list_annotations())

    @pytest.mark.parametrize("as_str", [False, True])
    def test_get_annotation_by_id(self, api, seeded, as_str):
        aid = str(seeded["highlight"]) if as_str else seeded["highlight"]
        anno = api.get_annotation_by_id(aid)
        assert anno.id == seeded["highlight"]
        assert anno.selected_text == "a synthetic highlight"
        assert anno.color == "YELLOW"
        assert anno.location.chapter_id == "chap1"
        assert api.get_annotation_by_id(seeded["note"]).note == "beta note"

    @pytest.mark.parametrize("aid", [9999, "9999", 10**20])
    def test_get_annotation_by_id_missing_raises_indexerror(self, api, seeded, aid):
        with pytest.raises(IndexError):
            api.get_annotation_by_id(aid)


class TestRelations:
    def test_book_annotations_exclude_reading_position(self, api, seeded):
        book = api.get_book_by_id(seeded["reading"])
        assert ids(book.annotations) == {seeded["highlight"], seeded["note"], seeded["blue"]}
        assert list(api.get_book_by_id(seeded["new"]).annotations) == []

    def test_book_collections(self, api, seeded):
        assert ids(api.get_book_by_id(seeded["reading"]).collections) == {seeded["shelf"]}
        assert list(api.get_book_by_id(seeded["new"]).collections) == []

    def test_annotation_book(self, api, seeded):
        assert api.get_annotation_by_id(seeded["towel"]).book.id == seeded["done"]

    def test_orphan_annotation_has_no_book(self, api, library, seeded):
        orphan = library.add_annotation("ORPHANASSET", "orphan highlight", created=day(7))
        assert api.get_annotation_by_id(orphan).book is None


class TestReadingPosition:
    def test_current_reading_location(self, api, seeded):
        bookmark = api.get_current_reading_location(seeded["reading"])
        assert bookmark.id == seeded["position"]
        assert bookmark.location.chapter_id == "chap1"
        assert api.get_current_reading_location(seeded["new"]) is None

    def test_current_reading_chapter(self, api, seeded):
        chapter = api.get_current_reading_chapter(seeded["reading"])
        assert (chapter.id, chapter.title) == ("chap1", "Chapter 1")
        assert api.get_current_reading_chapter(seeded["new"]) is None

    def test_annotation_surrounding_text(self, api, seeded):
        window = api.get_annotation_surrounding_text(seeded["highlight"], chars_before=40, chars_after=40)
        assert "a synthetic highlight" in window
        assert api.get_annotation_surrounding_text(seeded["towel"], chars_before=40, chars_after=40) == ""
        assert api.get_annotation_surrounding_text(9999) == ""


class TestContent:
    def test_readable_epub(self, api, library, simple_epub):
        book = library.add_book("Test Book", path=simple_epub.path)
        content = api.get_book_content(book["id"])
        assert [c.title for c in content.list_chapters()] == ["Chapter 1", "Chapter 2", "Chapter 3"]

    def test_no_path_raises_not_downloaded(self, api, library):
        book = library.add_book("Cloud Book")
        with pytest.raises(BookNotDownloadedError):
            api.get_book_content(book["id"])

    def test_fairplay_raises_drm(self, api, library, simple_epub):
        (simple_epub.path / "META-INF" / "sinf.xml").write_text("<sinf/>")
        book = library.add_book("Store Book", path=simple_epub.path)
        with pytest.raises(DRMProtectedError):
            api.get_book_content(book["id"])

    def test_unknown_book_is_bare_indexerror(self, api, library):
        """MCP 0.8.2's chapter tools catch AppleBooksError before
        IndexError, so an unknown id must not be an AppleBooksError."""
        with pytest.raises(IndexError) as exc:
            api.get_book_content(9999)
        assert not isinstance(exc.value, AppleBooksError)
