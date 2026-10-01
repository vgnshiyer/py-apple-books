"""Helpers shared by the facade (``api.py``) and its mixins.

The scope helpers moved here from ``api.py`` in 1.11 (``api.py``
re-imports them under the same names).

Private: nothing here is public API.
"""

from py_apple_books.db.clause import Q
from py_apple_books.models.annotation import _ALL_ANNOTATIONS, _LIVE_ANNOTATIONS
from py_apple_books.models.book import CONTENT_TYPE_SERIES_CONTAINER, SERIES_DATA_SOURCE, Book


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
