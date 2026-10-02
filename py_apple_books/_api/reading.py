"""The :class:`~py_apple_books.PyAppleBooks` mixin for spoiler-safe reading (the read boundary,
new in 1.11).

- ``get_read_boundary``: how far a book has been read, from the library
  database alone. ``BookContent.resolve_boundary`` places the result in
  the book's text (``py_apple_books._content_reading``).

See ``py_apple_books._api`` for the rules mixin code follows.
"""

import math
from typing import List, Optional, Sequence, Tuple

from py_apple_books._api._common import (
    _book_arg,
    _id_text,
    _reading_bookmarks,
    _recent_located_annotations,
)
from py_apple_books.db.client import current_library
from py_apple_books.exceptions import (
    AnnotationStoreNotFoundError,
    BookNotFoundError,
    InvalidChoiceError,
    UnsupportedSchemaError,
)
from py_apple_books.models.annotation import Annotation
from py_apple_books.models.book import Book
from py_apple_books.models.location import Location
from py_apple_books.positions import BoundarySource, BoundaryWarning, ReadBoundary

#: The values ``basis`` takes, in the order error messages list them.
_BASES = ("position", "furthest")

#: Live reading-position rows compared for the earliest (R17); Apple
#: Books normally keeps one per book.
_BOOKMARK_ROWS = 5
#: Located annotations read to find the newest usable one.
_HIGHLIGHT_ROWS = 5

# Annotation columns without which a tier can't be read at all.
_ANNOTATION_FIELDS = ("asset_id", "type", "is_deleted", "location")

# Book fields the boundary is placed by: a Book from this library without
# one (an ``only=`` read, or NULL in the store) is read again, once.
# ``is_finished`` is information only, and often NULL.
_BOOK_FIELDS = ("asset_id", "reading_progress", "high_water_progress")


def _basis(value) -> str:
    """``basis``, checked before any query: ``'position'`` or
    ``'furthest'``, in any case.

    :raises InvalidChoiceError: anything else (the message doesn't
        repeat the value).
    """
    if isinstance(value, str):
        folded = value.lower()
        if folded in _BASES:
            return folded
    raise InvalidChoiceError("basis must be 'position' or 'furthest'.", value=value, valid=list(_BASES))


def _percent(value) -> Optional[float]:
    """A stored progress percent, or None unless it is a finite number
    above 0 (``Book`` already reads 0 and NULL as None)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


def _usable(location) -> bool:
    """Whether ``location`` is a CFI into the spine (its sort key starts
    with a spine step), so it can be compared and placed."""
    return isinstance(location, Location) and location.spine_index is not None


def _plain_id(book_id) -> int:
    """The id ``book_id`` names, without reading the store (used only
    when the store can't be read as books)."""
    if isinstance(book_id, Book):
        return book_id.id
    if isinstance(book_id, int) and not isinstance(book_id, bool):
        return int(book_id)
    if isinstance(book_id, str) and book_id.strip().isdigit():
        return int(book_id.strip())
    raise BookNotFoundError(f"No book with id {_id_text(book_id)}.")


def _boundary_locations(asset_id: Optional[str]
                        ) -> Tuple[Sequence[Location], Optional[Location], bool, bool]:
    """``(bookmarks, highlight, missing_columns, unavailable)`` for
    :meth:`get_read_boundary`: the usable locations of the book's
    live reading-position rows (several only if the book has
    several), the newest usable highlight location (or None), whether
    a column the tiers read is missing, and whether there is no
    annotation store. Two statements at most; never raises
    :class:`UnsupportedSchemaError`."""
    if not current_library().has_annotations():
        return (), None, False, True
    manager = Annotation.manager
    if not manager.has_fields(*_ANNOTATION_FIELDS):
        return (), None, True, False
    missing = not manager.has_fields("modification_date", "creation_date")
    try:
        rows = _reading_bookmarks(asset_id, limit=_BOOKMARK_ROWS, only=("id", "location"))
        bookmarks = [row.location for row in rows if _usable(row.location)]
        rows = _recent_located_annotations(asset_id, limit=_HIGHLIGHT_ROWS, only=("id", "location"))
        highlight = next((row.location for row in rows if _usable(row.location)), None)
    except AnnotationStoreNotFoundError:
        return (), None, missing, True
    except UnsupportedSchemaError:
        # A column went missing after the schema was read.
        return (), None, True, False
    return bookmarks, highlight, missing, False


class _ReadingAPI:
    """Private mixin of :class:`~py_apple_books.PyAppleBooks`."""

    def get_read_boundary(self, book_id, *, basis: str = "position") -> ReadBoundary:
        """How far a book has been read, from the library database alone:
        the point before which the reader has seen the text, for
        spoiler-safe search and reading (1.11).

        No file is read: the result names locations and percents. Place
        it in the book's text with
        :meth:`BookContent.resolve_boundary
        <py_apple_books.content.BookContent.resolve_boundary>`, then pass
        the resolved boundary as ``until`` to ``BookContent.search`` or
        ``BookContent.iter_spine_text``; or test a highlight's location
        with :meth:`ReadBoundary.includes
        <py_apple_books.positions.ReadBoundary.includes>`.

        The boundary is placed by the first of these that the library
        has (:attr:`~py_apple_books.positions.ReadBoundary.source`):

        1. ``READING_POSITION``: Apple Books' reading-position bookmark.
           If the book has several live ones, the earliest in the book is
           used (``MULTIPLE_BOOKMARKS``), so the boundary is never past
           any of them. This differs on purpose from
           ``get_reading_position``, which reports the newest.
        2. ``RECENT_HIGHLIGHT``: the newest highlight, note or bookmark
           whose location points into the book's text.
        3. ``PROGRESS``: the book's reading progress (percent).
        4. ``NONE``: nothing is known to have been read.

        With ``basis='furthest'``, a furthest point read
        (``Book.high_water_progress``) past the reading progress gives
        ``FURTHEST`` instead: the boundary is then the later of the
        reading position and that point, for readers who jumped back.

        Every source found is kept on the result (``bookmark``,
        ``highlight``, ``progress``, ``high_water``), so placing it can
        fall back when the preferred one can't be placed in the book.

        Works on any store schema: a column it reads that the store
        lacks skips the tiers that need it and adds
        ``SCHEMA_MISSING_COLUMNS`` (it never raises
        :class:`~py_apple_books.exceptions.UnsupportedSchemaError`); with
        no annotation store, the bookmark and highlight are skipped and
        ``ANNOTATIONS_UNAVAILABLE`` is added. At most three statements.

        :param book_id: The book: an id, or a :class:`Book`.
        :param basis: ``'position'`` (default) or ``'furthest'``; any
            case.
        :raises InvalidChoiceError: an unknown ``basis`` (checked before
            any query).
        :raises BookNotFoundError: no book has that id.
        """
        basis = _basis(basis)
        warnings: List[BoundaryWarning] = []
        try:
            book = _book_arg(book_id, needs=_BOOK_FIELDS, get_book=self.get_book_by_id)
        except UnsupportedSchemaError:
            # The store's book table lacks a required column: no book can
            # be read, so nothing is known to have been read.
            return ReadBoundary(_plain_id(book_id), basis, BoundarySource.NONE, None, None, None,
                                None, None, (BoundaryWarning.SCHEMA_MISSING_COLUMNS,))

        progress = _percent(book.reading_progress)
        high_water = _percent(book.high_water_progress)
        is_finished = None if book.is_finished is None else bool(book.is_finished)
        missing = not Book.manager.has_fields("reading_progress")
        if basis == "furthest" and not Book.manager.has_fields("high_water_progress"):
            missing = True

        bookmarks, highlight, missing_annotations, unavailable = _boundary_locations(book.asset_id)
        if missing or missing_annotations:
            warnings.append(BoundaryWarning.SCHEMA_MISSING_COLUMNS)
        if unavailable:
            warnings.append(BoundaryWarning.ANNOTATIONS_UNAVAILABLE)
        bookmark = None
        if bookmarks:
            bookmark = min(bookmarks, key=lambda location: location.sort_key)
            if len(bookmarks) > 1:
                warnings.append(BoundaryWarning.MULTIPLE_BOOKMARKS)

        if basis == "furthest" and high_water is not None and (progress is None or high_water > progress):
            source = BoundarySource.FURTHEST
        elif bookmark is not None:
            source = BoundarySource.READING_POSITION
        elif highlight is not None:
            source = BoundarySource.RECENT_HIGHLIGHT
        elif progress is not None:
            source = BoundarySource.PROGRESS
        else:
            source = BoundarySource.NONE
        return ReadBoundary(book.id, basis, source, bookmark, highlight, progress, high_water,
                            is_finished, tuple(warnings))
