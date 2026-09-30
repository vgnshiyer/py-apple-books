"""F27 raise sites in the facade and BookContent (1.10).

Each typed error keeps the built-in base 1.9.1 raised bare, so
``except IndexError`` / ``except KeyError`` handlers (apple-books-mcp
0.8.2 has ten) still catch it. ``get_book_content`` keeps raising a bare
``IndexError`` for an unknown id: MCP 0.8.2 catches ``AppleBooksError``
before ``IndexError`` in its chapter tools.
"""

import pickle
import sys

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.content import BookContent
from py_apple_books.exceptions import (
    AnnotationNotFoundError,
    AppleBooksError,
    BookNotFoundError,
    ChapterNotFoundError,
    DBError,
    DBQueryError,
    InvalidArgumentError,
    InvalidChoiceError,
    NotFoundError,
    NotInLibraryError,
)
from py_apple_books.models import Annotation
from py_apple_books.testing import STORE_SERIES, write_epub

MISSING = 999999
HUGE = 10**20
COLORS = ("green", "blue", "yellow", "pink", "purple")


def cfi(chapter_id: str, step: int) -> str:
    return f"epubcfi(/6/{step}[{chapter_id}]!/4/4,/1:0,/1:21)"


@pytest.fixture
def epub(tmp_path):
    return write_epub(tmp_path / "Synthetic Book.epub", "Synthetic Book")


@pytest.fixture
def book(library, epub):
    return library.add_book("Synthetic Book", progress=0.4, path=epub)


class TestBookNotFound:
    @pytest.mark.parametrize("book_id", [MISSING, str(MISSING), HUGE])
    def test_get_book_by_id(self, api, library, book_id):
        with pytest.raises(BookNotFoundError) as exc:
            api.get_book_by_id(book_id)
        err = exc.value
        assert isinstance(err, IndexError) and isinstance(err, LookupError)
        assert isinstance(err, NotFoundError) and isinstance(err, AppleBooksError)
        assert str(err) == f"No book with id {book_id}."
        assert err.__cause__ is None and err.__suppress_context__

    @pytest.mark.skipif(not hasattr(sys, "get_int_max_str_digits"), reason="no int/str digit limit")
    def test_int_too_long_for_str(self, api, library):
        huge = 10 ** (sys.get_int_max_str_digits() + 1)
        with pytest.raises(BookNotFoundError, match="too long"):
            api.get_book_by_id(huge)
        with pytest.raises(AnnotationNotFoundError, match="too long"):
            api.get_annotation_by_id(huge)

    def test_found(self, api, book):
        assert api.get_book_by_id(book["id"]).title == "Synthetic Book"

    def test_pickles(self, api, library):
        with pytest.raises(BookNotFoundError) as exc:
            api.get_book_by_id(MISSING)
        again = pickle.loads(pickle.dumps(exc.value))
        assert type(again) is BookNotFoundError and str(again) == str(exc.value)


class TestAnnotationNotFound:
    @pytest.mark.parametrize("annotation_id", [MISSING, str(MISSING), HUGE, -HUGE])
    def test_get_annotation_by_id(self, api, library, annotation_id):
        with pytest.raises(AnnotationNotFoundError) as exc:
            api.get_annotation_by_id(annotation_id)
        err = exc.value
        assert isinstance(err, IndexError) and isinstance(err, AppleBooksError)
        assert str(err) == f"No annotation with id {annotation_id}."

    def test_found_even_if_deleted(self, api, library, book):
        aid = library.add_annotation(book, "gone", deleted=True)
        assert api.get_annotation_by_id(aid).is_deleted == 1


class TestColor:
    def test_unknown_color(self, api, library):
        with pytest.raises(InvalidChoiceError) as exc:
            api.get_annotations_by_color("orange")
        err = exc.value
        assert str(err) == ("Unknown highlight color 'orange'. "
                            "Valid colors: green, blue, yellow, pink, purple.")
        assert err.value == "orange" and err.valid == COLORS
        assert isinstance(err, InvalidArgumentError) and isinstance(err, ValueError)
        assert isinstance(err, AppleBooksError)

    def test_caught_by_except_keyerror(self, api, library):
        """1.9.1 raised ``KeyError: 'ORANGE'``."""
        try:
            api.get_annotations_by_color("orange")
        except KeyError as e:
            caught = e
        assert isinstance(caught, InvalidChoiceError)

    @pytest.mark.parametrize("color", ["", "Orange", "yellow ", "underline", "__class__"])
    def test_other_bad_values(self, api, library, color):
        with pytest.raises(InvalidChoiceError):
            api.get_annotations_by_color(color)

    def test_none_is_still_an_attribute_error(self, api, library):
        with pytest.raises(AttributeError):
            api.get_annotations_by_color(None)

    @pytest.mark.parametrize("color", ["YELLOW", "Yellow", "yellow"])
    def test_any_case_is_accepted(self, api, library, book, color):
        aid = library.add_annotation(book, "a synthetic highlight")
        assert [a.id for a in api.get_annotations_by_color(color)] == [aid]


class TestBookContent:
    @pytest.mark.parametrize("book_id", [MISSING, str(MISSING), HUGE])
    def test_unknown_id_is_a_bare_indexerror(self, api, library, book_id):
        with pytest.raises(IndexError) as exc:
            api.get_book_content(book_id)
        assert type(exc.value) is IndexError
        assert not isinstance(exc.value, AppleBooksError)
        assert str(exc.value) == f"No book with id {book_id}."

    def test_mcp_0_8_2_except_order(self, api, library):
        """The chapter tools' order: AppleBooksError first, then IndexError."""
        try:
            api.get_book_content(MISSING)
        except AppleBooksError:
            branch = "unreadable"
        except IndexError:
            branch = "not found"
        assert branch == "not found"

    def test_not_in_library(self, api, library):
        row = library.add_book("Store Volume", data_source=STORE_SERIES, can_redownload=0, state=5)
        with pytest.raises(NotInLibraryError) as exc:
            api.get_book_content(row["id"])
        assert str(exc.value) == (
            "'Store Volume' is an Apple Books Store series item that isn't in your library "
            "(an unowned volume or a series container), so there is no book file to read.")

    def test_stubbed_book_without_the_property(self, api, epub):
        """Callers may stub get_book_by_id with a plain object."""
        from types import SimpleNamespace

        api.get_book_by_id = lambda book_id: SimpleNamespace(title="Stub", path=str(epub))
        assert api.get_book_content(1).list_chapters()


class TestChapterNotFound:
    EXPECTED = ("No chapter or spine entry with id 'Chapter 5' in this book. Pass an id "
                "from the book's table of contents, or a chapter's 1-based order (e.g. \"5\").")

    def test_get_chapter(self, epub):
        with pytest.raises(ChapterNotFoundError) as exc:
            BookContent(epub).get_chapter("Chapter 5")
        err = exc.value
        assert str(err) == self.EXPECTED
        assert "list_chapters" not in str(err)
        assert isinstance(err, NotFoundError) and isinstance(err, AppleBooksError)
        # Not an IndexError: MCP 0.8.2 reports it as "Could not read chapter".
        assert not isinstance(err, IndexError)

    def test_spine_item_text(self, epub):
        with pytest.raises(ChapterNotFoundError) as exc:
            BookContent(epub)._spine_item_text("Chapter 5")
        assert str(exc.value) == self.EXPECTED

    def test_order_out_of_range(self, epub):
        with pytest.raises(ChapterNotFoundError, match="'99'"):
            BookContent(epub).get_chapter("99")

    def test_found(self, epub):
        assert "Chapter 2" in BookContent(epub).get_chapter("2")
        assert "Chapter 1" in BookContent(epub).get_chapter("chap1")


class TestSurroundingText:
    def test_unknown_id_is_empty(self, api, library):
        assert api.get_annotation_surrounding_text(MISSING) == ""
        assert api.get_annotation_surrounding_text(HUGE) == ""

    def test_found(self, api, library, book):
        aid = library.add_annotation(book, "a synthetic highlight", location=cfi("chap1", 4))
        assert "a synthetic highlight" in api.get_annotation_surrounding_text(aid, 20, 20)

    def test_unknown_chapter_is_empty(self, api, library, book):
        aid = library.add_annotation(book, "a synthetic highlight", location=cfi("nochapter", 4))
        assert api.get_annotation_surrounding_text(aid) == ""

    def test_store_item_is_empty(self, api, library):
        row = library.add_book("Store Volume", data_source=STORE_SERIES, can_redownload=0)
        aid = library.add_annotation(row, "a synthetic highlight", location=cfi("chap1", 4))
        assert api.get_annotation_surrounding_text(aid) == ""

    def test_db_error_on_lookup_propagates(self, api, library, monkeypatch):
        def failing(*args, **kwargs):
            raise DBQueryError("Error executing query: disk I/O error")

        monkeypatch.setattr(Annotation.manager, "filter", failing)
        with pytest.raises(DBError):
            api.get_annotation_surrounding_text(1)

    def test_db_error_on_content_propagates(self, api, library, book, monkeypatch):
        aid = library.add_annotation(book, "a synthetic highlight", location=cfi("chap1", 4))

        def failing(self, book_id):
            raise DBQueryError("Error executing query: disk I/O error")

        monkeypatch.setattr(PyAppleBooks, "get_book_content", failing)
        with pytest.raises(DBQueryError):
            api.get_annotation_surrounding_text(aid)


class TestInvalidArguments:
    """Arguments the facade validates before querying (R22)."""

    def test_negative_offset(self, api, library):
        with pytest.raises(InvalidArgumentError):
            api.get_recently_read_books(offset=-1)
        with pytest.raises(InvalidArgumentError):
            api.list_books(offset=-1)

    def test_bad_limit(self, api, library):
        with pytest.raises(InvalidArgumentError):
            api.get_recently_read_books(limit="ten")
