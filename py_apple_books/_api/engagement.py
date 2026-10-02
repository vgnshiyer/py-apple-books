"""The :class:`~py_apple_books.PyAppleBooks` mixin for engagement: underlines and
highlights made on this day in earlier years (1.11).

Every method here reads the library and annotation databases only (no
book file, no iCloud folder, no subprocess). Dates follow the rules in
:mod:`py_apple_books.engagement`; ``limit`` and ``offset`` the 1.11
rules (``_common.strict_limit``/``strict_offset``).

See ``py_apple_books._api`` for the rules mixin code follows.
"""

from py_apple_books import engagement as _eng
from py_apple_books._api._common import _annotation_scope, strict_limit, strict_offset
from py_apple_books.db.clause import Subquery, Where
from py_apple_books.models.annotation import Annotation, AnnotationType
from py_apple_books.models.book import Book
from py_apple_books.models.manager import ModelIterable
from py_apple_books.utils import APPLE_EPOCH_OFFSET

_HIGHLIGHT = int(AnnotationType.HIGHLIGHT)


def _in_library() -> Subquery:
    """The asset ids of every book row (``annotation.book`` is not None
    exactly for these), for ``asset_id__in``."""
    return Subquery(Book.manager.table_name, Book.manager._get_db_field("asset_id"))


def _underline_filter() -> dict:
    """Filter keywords for underlines among type-2 rows: Books' underline
    flag, or style 0 on a store without it (a store without either
    raises ``UnsupportedSchemaError`` when the query runs)."""
    if Annotation.manager.has_fields("is_underline"):
        return {"is_underline": 1}
    return {"style": 0}


class _EngagementAPI:
    """Private mixin of :class:`~py_apple_books.PyAppleBooks`."""

    def get_underlines(self, limit: int = None, order_by: str = "-creation_date", *,
                       offset: int = None, include_deleted: bool = False) -> ModelIterable:
        """Get your underlines, newest first by default (1.11).

        Underlines are highlights (type 2) Books marks as underlined;
        bookmarks and the reading-position row, which also have no color
        (style 0), are never returned. On a Books version without the
        underline flag, the type-2 rows with style 0 are returned
        instead. Scope, paging and order work as in
        :meth:`get_annotations_by_color`; deleted underlines come back
        with ``include_deleted``.

        :raises InvalidArgumentError: ``limit`` below 1, or a bad
            ``offset``.
        :raises UnsupportedSchemaError: (when the result is read) the
            store has neither the underline flag nor the style column.
        """
        limit, offset = strict_limit(limit), strict_offset(offset)
        return Annotation.manager.filter(
            type=_HIGHLIGHT,
            **_underline_filter(),
            **_annotation_scope(include_deleted),
            limit=limit,
            order_by=order_by,
            offset=offset,
        )

    def get_highlights_on_this_day(self, on=None, limit: int = None, order_by: str = "-creation_date", *,
                                   offset: int = None, include_deleted: bool = False,
                                   include_orphans: bool = True) -> ModelIterable:
        """Get the highlights you made on this calendar day in earlier
        years, newest first by default (1.11).

        ``on`` is the day (default today; see
        :mod:`py_apple_books.engagement` for how a datetime is read).
        Returned: highlights and notes (type 2) whose local creation date
        has ``on``'s month and day and falls before ``on`` (so the day
        itself is left out, and 29 February matches earlier 29 Februaries
        only). Bookmarks and reading positions are not returned, unlike
        :meth:`get_annotations_by_date_range`. ``include_orphans=False``
        leaves out highlights of books no longer in the library;
        ``include_deleted`` brings deleted ones back. The result is
        counted, sliced and grouped in SQL.

        :raises InvalidArgumentError: a bad ``on``, ``limit`` or ``offset``.
        :raises UnsupportedSchemaError: (when the result is read) the
            store has no annotation creation date.
        """
        day = _eng._local_day(on)
        limit, offset = strict_limit(limit), strict_offset(offset)
        column = Annotation.manager._get_db_field("creation_date")
        filters = _annotation_scope(include_deleted)
        filters["type"] = _HIGHLIGHT
        # Schema-checked, so a store without the column raises
        # UnsupportedSchemaError before the expression below runs.
        filters["creation_date__lt"] = _eng._day_start(day)
        if not include_orphans:
            filters["asset_id__in"] = _in_library()
        same_day = Where(f"strftime('%m-%d', {column} + {APPLE_EPOCH_OFFSET}, 'unixepoch', 'localtime')",
                         f"{day.month:02d}-{day.day:02d}")
        return Annotation.manager.filter(**filters, where=same_day,
                                         limit=limit, order_by=order_by, offset=offset)
