"""Tests for the ORM model layer (py_apple_books.models).

Rows are built in memory and fed through ``Model.from_db`` with
relation loading stubbed out, so nothing here executes a query against
the Apple Books databases.
"""

import configparser
import pathlib

import pytest

from py_apple_books.models import base
from py_apple_books.models.annotation import Annotation
from py_apple_books.models.base import _load_mappings
from py_apple_books.models.book import Book

MAPPINGS_PATH = pathlib.Path(base.__file__).parent / "mappings.ini"


def _row(model, **values) -> list:
    """A DB row for ``model`` in mappings.ini column order; unspecified
    columns are NULL."""
    return [values.get(field) for field in model._get_mappings(model.__name__)]


def _book_row(**values) -> list:
    return _row(Book, **{"id": 1, "asset_id": "ASSET", "title": "Title", **values})


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
