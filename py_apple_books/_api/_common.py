"""Helpers shared by the facade (``api.py``) and its mixins.

The scope helpers moved here from ``api.py`` in 1.11 (``api.py``
re-imports them under the same names). The rest is new in 1.11 and used
only by new methods; no 1.10 method changes behaviour:

- :func:`_book_arg`: the one rule for a ``book_id`` argument that may
  be an id or a :class:`Book`.
- :func:`strict_limit`, :func:`strict_offset`: argument checks for new
  methods (no deprecated "``limit <= 0`` means all" path).
- :func:`_reading_bookmarks`, :func:`_recent_located_annotations`: the
  annotation rows reading positions are read from.
- :func:`_books_by_asset`: books by asset id, as ``annotation.book``
  finds them.

Private: nothing here is public API.
"""

import numbers
import operator
from typing import Callable, Dict, Iterable, Optional, Sequence

from py_apple_books.db.clause import Q
from py_apple_books.db.client import current_library
from py_apple_books.exceptions import BookNotFoundError, InvalidArgumentError
from py_apple_books.models.annotation import (
    _ALL_ANNOTATIONS,
    _LIVE_ANNOTATIONS,
    Annotation,
    AnnotationType,
)
from py_apple_books.models.book import CONTENT_TYPE_SERIES_CONTAINER, SERIES_DATA_SOURCE, Book
# _INT64_MAX: the largest value SQLite binds as an integer; larger
# limits and offsets are clamped to it, as the manager does.
from py_apple_books.models.manager import _INT64_MAX, ModelIterable


# -- moved from api.py (1.10 behaviour, unchanged) ----------------------------


def _id_text(value) -> str:
    """``value`` for an error message. An int too long for ``str()``
    (``sys.get_int_max_str_digits()``) is a valid, if absurd, id."""
    try:
        return str(value)
    except ValueError:
        return "<an integer too long to print>"


def _annotation_scope(include_deleted: bool) -> dict:
    """Filter keywords for the user-facing annotation queries (a new
    dict each call, so callers may add keys)."""
    return dict(_ALL_ANNOTATIONS if include_deleted else _LIVE_ANNOTATIONS)


def _owned_books_filter() -> dict:
    """Filter keywords for the books in the user's library.

    Leaves out Apple Books Store series rows the user doesn't own: series
    containers (``ZCONTENTTYPE`` 5), and Series-source volumes without
    the redownload (ownership) flag. NULL-safe, so a row with no data
    source or content type stays in. A predicate whose column the store
    lacks is dropped, which shows those rows as 1.9.1 did: hiding a row
    needs all the evidence. Same rule as :attr:`Book.is_store_series_item`.
    """
    scope = {}
    if Book.manager.has_fields("content_type"):
        scope["content_type__isnot"] = CONTENT_TYPE_SERIES_CONTAINER
    if Book.manager.has_fields("data_source", "can_redownload"):
        scope["where"] = Q(data_source__isnot=SERIES_DATA_SOURCE) | Q(can_redownload=1)
    return scope


def _book_scope(include_store_series: bool) -> dict:
    """Filter keywords for a book list: all rows if ``include_store_series``,
    else the owned ones."""
    return {} if include_store_series else _owned_books_filter()


def _book_by_id(book_id) -> Book:
    """The book with id ``book_id`` in the current library, Store series
    rows included (the body of :meth:`PyAppleBooks.get_book_by_id`).

    :raises BookNotFoundError: no book has that id.
    """
    try:
        return Book.manager.filter(id=book_id)[0]
    except IndexError:
        raise BookNotFoundError(f"No book with id {_id_text(book_id)}.") from None


# -- book arguments (R7) ------------------------------------------------------


def _book_arg(book_id, *, needs: Iterable[str] = (),
              get_book: Optional[Callable[[object], Book]] = None) -> Book:
    """The :class:`Book` a new facade method's ``book_id`` argument names.

    New methods take ``book_id`` as an id (``int`` or ``str``, as
    :meth:`PyAppleBooks.get_book_by_id` accepts) or a ``Book``:

    - an id is looked up (one statement), raising
      :class:`BookNotFoundError` when no book has it;
    - a ``Book`` read from another library (another ``LibraryDB``, or
      none: built directly or unpickled) is looked up again by its id in
      the current library, which is the instance's inside a facade
      method;
    - a ``Book`` read from the current library is used as is (no
      statement), unless a field named in ``needs`` is None on it (for
      example one an ``only=`` read left out): then it is read again
      once, by id. The reread book is returned even if the field is
      still None (NULL in the store).

    ``get_book(id)`` does the lookups; the default is
    :func:`_book_by_id`. Pass ``self.get_book_by_id`` to honour a
    subclass's (or a test's) override.
    """
    lookup = _book_by_id if get_book is None else get_book
    if not isinstance(book_id, Book):
        return lookup(book_id)
    book = book_id
    if book.__dict__.get("_ab_db") is not current_library():
        return lookup(book.id)
    if any(getattr(book, field) is None for field in needs):
        return lookup(book.id)
    return book


# -- limits and offsets of new methods (R8) -----------------------------------


def _strict_int(value, name: str) -> int:
    """``value`` as an int: an int (not a bool), an ``__index__`` type
    such as numpy.int64, or an integral float, Decimal or Fraction.
    Messages name the argument and the type, never the value."""
    if isinstance(value, bool):
        raise InvalidArgumentError(f"{name} must be an integer or None, not a bool.")
    try:
        return operator.index(value)
    except TypeError:
        pass
    if isinstance(value, numbers.Number):
        try:
            as_int = int(value)
        except (TypeError, ValueError, OverflowError):
            as_int = None
        if as_int is not None and as_int == value:
            return as_int
        raise InvalidArgumentError(f"{name} must be a whole number or None.")
    raise InvalidArgumentError(f"{name} must be an integer or None, not {type(value).__name__}.")


def strict_limit(limit, *, name: str = "limit") -> Optional[int]:
    """Check a new method's ``limit`` (R8): None (all, unless the method
    documents a default), else an integer >= 1. Values above 2**63 - 1
    are clamped to it.

    Unlike 1.10's ``normalize_limit`` there is no deprecated "``<= 0``
    means all" path, and bools and strings are refused.

    :raises InvalidArgumentError: a bool, a non-number, a non-integral
        number, or a value below 1.
    """
    if limit is None:
        return None
    value = _strict_int(limit, name)
    if value < 1:
        raise InvalidArgumentError(f"{name} must be at least 1, or None for no limit.")
    return min(value, _INT64_MAX)


def strict_offset(offset, *, name: str = "offset") -> Optional[int]:
    """Check a new method's ``offset`` (R8): None, else an integer >= 0.
    Values above 2**63 - 1 are clamped to it.

    :raises InvalidArgumentError: a bool, a non-number, a non-integral
        number, or a negative value.
    """
    if offset is None:
        return None
    value = _strict_int(offset, name)
    if value < 0:
        raise InvalidArgumentError(f"{name} must be 0 or more, or None.")
    return min(value, _INT64_MAX)


# -- annotation rows for reading positions (R17) ------------------------------

# A CFI into the spine (/6 is the package's spine element): the range
# ['epubcfi(/6/', 'epubcfi(/60') holds exactly the strings with that
# prefix, as '/' sorts just before '0'. Plain >=/< comparisons, so no
# new lookup and no LIKE escaping.
_SPINE_CFI_FROM = "epubcfi(/6/"
_SPINE_CFI_TO = "epubcfi(/60"


def _none() -> ModelIterable:
    """An empty, evaluated result of annotations (no statement)."""
    return ModelIterable._from_objects(Annotation, (), db=current_library())


def _reading_bookmarks(asset_id: Optional[str], *, limit: Optional[int] = None,
                       only: Optional[Sequence[str]] = None) -> ModelIterable:
    """The reading-position rows (type 3) of ``asset_id`` that are live
    (``is_deleted`` is not 1, so NULL counts as live): newest
    ``modification_date`` first (None last), then higher id. On a store
    without the modification date column, storage order.

    One statement, or none when ``asset_id`` is None or empty. ``limit``
    and ``only`` are passed to the manager. Callers that take one row
    (``get_reading_position``: the newest is authoritative) and callers
    that compare several (``get_read_boundary``: the earliest CFI) read
    the same rows; Apple Books normally keeps one per book.

    :raises UnsupportedSchemaError: the store lacks the type, deleted
        flag or asset id column (check ``has_fields`` first to degrade).
    """
    if not asset_id:
        return _none()
    order = ("-modification_date", "-id") if Annotation.manager.has_fields("modification_date") else None
    return Annotation.manager.filter(
        asset_id=asset_id,
        type=int(AnnotationType.READING_POSITION),
        is_deleted__isnot=1,
        only=only, limit=limit, order_by=order,
    )


def _recent_located_annotations(asset_id: Optional[str], *, limit: Optional[int] = None,
                                only: Optional[Sequence[str]] = None) -> ModelIterable:
    """The live bookmarks and highlights (types 1 and 2; ``is_deleted``
    not 1, NULL counting as live) of ``asset_id`` whose location is a
    CFI into the spine (starts ``epubcfi(/6/``): newest
    ``creation_date`` first, then higher id. On a store without the
    creation date column, storage order.

    One statement, or none when ``asset_id`` is None or empty. ``limit``
    and ``only`` are passed to the manager.

    :raises UnsupportedSchemaError: the store lacks the type, deleted
        flag, location or asset id column.
    """
    if not asset_id:
        return _none()
    order = ("-creation_date", "-id") if Annotation.manager.has_fields("creation_date") else None
    return Annotation.manager.filter(
        asset_id=asset_id,
        type__in=[int(AnnotationType.BOOKMARK), int(AnnotationType.HIGHLIGHT)],
        is_deleted__isnot=1,
        location__gte=_SPINE_CFI_FROM,
        location__lt=_SPINE_CFI_TO,
        only=only, limit=limit, order_by=order,
    )


# -- books by asset id --------------------------------------------------------


def _books_by_asset(only: Sequence[str] = ("id", "asset_id", "title")) -> Dict[str, Book]:
    """``{asset id: Book}`` over every book row, as ``annotation.book``
    finds them: Store series items included, and the lowest id for an
    asset id two rows share. Rows without an asset id are left out.
    One statement; ``only`` names the fields read (the others are None).
    """
    books: Dict[str, Book] = {}
    for book in Book.manager.all(only=list(only), order_by="id"):
        if book.asset_id is not None:
            books.setdefault(book.asset_id, book)
    return books
