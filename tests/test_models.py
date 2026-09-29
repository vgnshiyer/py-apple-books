"""Tests for the ORM model layer (py_apple_books.models).

Rows are built in memory and fed through ``Model.from_db`` with
relation loading stubbed out, so nothing here executes a query against
the Apple Books databases.
"""

import configparser
import datetime as dt
import pathlib

import pytest

from py_apple_books.models import base
from py_apple_books.models.annotation import Annotation
from py_apple_books.models.base import _load_mappings
from py_apple_books.models.book import Book
from py_apple_books.models.location import Location
from py_apple_books.utils import apple_timestamp_to_datetime

MAPPINGS_PATH = pathlib.Path(base.__file__).parent / "mappings.ini"


def _row(model, **values) -> list:
    """A DB row for ``model`` in mappings.ini column order; unspecified
    columns are NULL."""
    return [values.get(field) for field in model._get_mappings(model.__name__)]


def _book_row(**values) -> list:
    return _row(Book, **{"id": 1, "asset_id": "ASSET", "title": "Title", **values})


def _annotation_row(**values) -> list:
    return _row(Annotation, **{"id": 1, "asset_id": "ASSET", "type": 2, **values})


@pytest.fixture
def no_relations(monkeypatch):
    """Skip relation loading in ``from_db`` (it would query the DB)."""
    for model in (Book, Annotation):
        monkeypatch.setattr(model.manager, "handle_relations", lambda obj: None)


# ---------------------------------------------------------------------------
# mappings.ini memoization
# ---------------------------------------------------------------------------


class TestMappings:
    def test_ini_parsed_once_across_many_rows(self, monkeypatch, no_relations):
        """Regression: pre-1.9.1 re-read mappings.ini on every
        ``_get_mappings`` call — ~13 parses per annotation row, which
        was ~99% of the time spent listing a large library."""
        _load_mappings.cache_clear()
        reads = []
        original_read = configparser.ConfigParser.read

        def counting_read(self, *args, **kwargs):
            reads.append(args)
            return original_read(self, *args, **kwargs)

        monkeypatch.setattr(configparser.ConfigParser, "read", counting_read)

        for i in range(1000):
            Book.from_db(_book_row(id=i))
            Annotation.from_db(_row(Annotation, id=i, asset_id="ASSET"))
            # Manager helpers resolve fields through the same mappings.
            Annotation.manager.filter(asset_id="ASSET", type__ne=3, order_by="-creation_date")

        assert len(reads) == 1

    def test_matches_fresh_parse_in_order(self):
        """``from_db`` zips mapping keys against SELECTed columns, so
        key order must match the file exactly."""
        config = configparser.ConfigParser()
        config.read(MAPPINGS_PATH)
        for section in config.sections():
            assert list(Book._get_mappings(section).items()) == list(config.items(section))

    def test_returns_fresh_copy(self):
        mappings = Book._get_mappings("Book")
        mappings["title"] = "MUTATED"
        mappings["bogus"] = "ZBOGUS"

        again = Book._get_mappings("Book")
        assert again["title"] == "ZTITLE"
        assert "bogus" not in again

    def test_keys_subset(self):
        subset = Book._get_mappings("Book", keys=["title", "author"])
        assert subset == {"title": "ZTITLE", "author": "ZAUTHOR"}
        subset["title"] = "MUTATED"
        assert Book._get_mappings("Book")["title"] == "ZTITLE"


# ---------------------------------------------------------------------------
# Book.author: Apple's unknown-author placeholder
# ---------------------------------------------------------------------------

# How Books stores "no author" in ZAUTHOR; Books.app shows "Unknown Author".
UNKNOWN_AUTHOR = "\ue83aUnknownAuthor"


def _is_private_use(ch: str) -> bool:
    return "\ue000" <= ch <= "\uf8ff"


class TestUnknownAuthor:
    def test_placeholder_becomes_none(self, no_relations):
        """Regression: the raw placeholder leaked into every listing,
        bypassing callers' ``book.author or "Unknown Author"`` fallbacks."""
        assert Book.from_db(_book_row(author=UNKNOWN_AUTHOR)).author is None

    @pytest.mark.parametrize("glyph", ["\ue000", "\uf8ff"])
    def test_whole_private_use_range_is_recognized(self, no_relations, glyph):
        assert Book.from_db(_book_row(author=glyph + "UnknownAuthor")).author is None

    @pytest.mark.parametrize("author", [
        "Jane Austen",
        "村上春樹",            # CJK sits outside the Private Use Area
        "\uf8ff Education",    # PUA glyph (Apple logo) before a real name
        "UnknownAuthor",       # no glyph, so not Apple's placeholder
        "",
        None,
    ])
    def test_other_authors_untouched(self, no_relations, author):
        assert Book.from_db(_book_row(author=author)).author == author

    def test_str_renders_missing_author(self, no_relations):
        text = str(Book.from_db(_book_row(author=UNKNOWN_AUTHOR)))
        assert "\nAuthor: Unknown Author\n" in text
        assert not any(_is_private_use(ch) for ch in text)

    def test_str_renders_real_author(self, no_relations):
        assert "\nAuthor: Jane Austen\n" in str(Book.from_db(_book_row(author="Jane Austen")))


# ---------------------------------------------------------------------------
# Book.page_count: ZPAGECOUNT placeholders
# ---------------------------------------------------------------------------


class TestPageCount:
    @pytest.mark.parametrize("raw", [None, 0, 1])
    def test_placeholder_counts_become_none(self, no_relations, raw):
        """Regression: Apple leaves ZPAGECOUNT at 0 or 1 for most
        imported books, so callers printed "Pages: 1"."""
        assert Book.from_db(_book_row(page_count=raw)).page_count is None

    @pytest.mark.parametrize("raw", [2, 336])
    def test_real_counts_kept(self, no_relations, raw):
        assert Book.from_db(_book_row(page_count=raw)).page_count == raw


# ---------------------------------------------------------------------------
# 1.10 columns: appended to mappings.ini, defaulted on the dataclasses
# ---------------------------------------------------------------------------

# The 1.9.1 [Book] and [Annotation] keys, in file order. ``from_db`` is
# positional, so these must stay first with the 1.10 keys after them.
BOOK_KEYS_191 = [
    "id", "asset_id", "title", "author", "description", "genre", "content_type",
    "page_count", "path", "filesize", "is_finished", "reading_progress", "duration",
    "creation_date", "finished_date", "last_opened_date", "purchased_date",
    "is_explicit", "is_locked", "is_ephemeral", "is_hidden", "is_sample",
    "is_store_audiobook", "rating",
]
BOOK_KEYS_110 = ["store_id", "data_source", "can_redownload", "state", "last_engaged_date"]
ANNOTATION_KEYS_191 = [
    "id", "asset_id", "is_deleted", "is_underline", "style", "type", "creation_date",
    "modification_date", "selected_text", "representative_text", "note", "location",
    "chapter",
]
ANNOTATION_KEYS_110 = ["uuid", "position"]


class TestNewFields:
    @pytest.mark.parametrize("model, old, new", [
        (Book, BOOK_KEYS_191, BOOK_KEYS_110),
        (Annotation, ANNOTATION_KEYS_191, ANNOTATION_KEYS_110),
    ])
    def test_new_keys_are_appended(self, model, old, new):
        keys = list(model._get_mappings(model.__name__))
        assert keys[:len(old) + len(new)] == old + new

    def test_purchaser_account_id_is_not_mapped(self):
        """ZPURCHASEDDSID identifies an Apple account; never read it."""
        for columns in _load_mappings().values():
            assert "ZPURCHASEDDSID" not in columns.values()

    def test_from_db_fills_book_fields(self, no_relations):
        book = Book.from_db(_book_row(
            store_id="1234567890", data_source="com.apple.ibooks.BKLibraryDataSourceSeries",
            can_redownload=0, state=5, last_engaged_date=86400.0))
        assert (book.store_id, book.data_source, book.can_redownload, book.state) == (
            "1234567890", "com.apple.ibooks.BKLibraryDataSourceSeries", 0, 5)
        assert book.last_engaged_date == apple_timestamp_to_datetime(86400.0)

    def test_from_db_null_book_fields(self, no_relations):
        book = Book.from_db(_book_row())
        assert [getattr(book, key) for key in BOOK_KEYS_110] == [None] * len(BOOK_KEYS_110)

    def test_from_db_fills_annotation_fields(self, no_relations):
        annotation = Annotation.from_db(_annotation_row(
            uuid="6F1C2B7A-0000-4000-8000-000000000001", position=3,
            location="epubcfi(/6/8[c3]!/4/2/1,:0,:5)"))
        assert annotation.uuid == "6F1C2B7A-0000-4000-8000-000000000001"
        assert annotation.position == 3 == annotation.location.spine_index

    def test_last_engaged_date_accepts_a_datetime(self):
        when = dt.datetime(2020, 1, 2, 3, 4)
        book = Book(*[None] * len(BOOK_KEYS_191), last_engaged_date=when)
        assert book.last_engaged_date == when

    def test_positional_construction_without_new_fields(self):
        """Code written against 1.9.1 builds models positionally; the new
        fields are defaulted and last, so that keeps working."""
        book = Book(*[None] * len(BOOK_KEYS_191))
        assert [getattr(book, key) for key in BOOK_KEYS_110] == [None] * len(BOOK_KEYS_110)
        assert book.deep_link is None and book.last_read_date is None
        # id, asset_id, is_deleted, dates, texts, is_underline, style, type,
        # chapter, location, color: the 1.9.1 dataclass fields.
        annotation = Annotation(7, "ASSET", 0, None, None, "r", "s", None, 0, 3, 2, None,
                                Location("epubcfi(/6/4!/4/2/1:0)"), None)
        assert (annotation.uuid, annotation.position) == (None, None)
        assert annotation.deep_link == "ibooks://assetid/ASSET#epubcfi(/6/4!/4/2/1:0)"
