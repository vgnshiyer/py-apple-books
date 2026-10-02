"""The :class:`~py_apple_books.PyAppleBooks` mixin for engagement: underlines, highlight
sampling, highlights made on this day in earlier years, highlighted
words, highlight activity and streaks, and reading goals (1.11).

Every method here reads the library and annotation databases only (no
book file, no iCloud folder, no subprocess), except
``get_reading_goals``, which reads Books' preferences file instead
(``py_apple_books._prefs``). Dates follow the rules in
:mod:`py_apple_books.engagement`; ``limit`` and ``offset`` the 1.11
rules (``_common.strict_limit``/``strict_offset``).

See ``py_apple_books._api`` for the rules mixin code follows.
"""

import operator
import os
from typing import FrozenSet, Iterable, List, Optional

from py_apple_books import _prefs
from py_apple_books import engagement as _eng
from py_apple_books._api._common import (
    _annotation_scope,
    _book_arg,
    _books_by_asset,
    strict_limit,
    strict_offset,
)
from py_apple_books.db.clause import Q, Subquery, Where
from py_apple_books.db.client import ANNOTATIONS_NOT_FOUND, current_library
from py_apple_books.exceptions import AnnotationStoreNotFoundError, InvalidArgumentError
from py_apple_books.models.annotation import Annotation, AnnotationType
from py_apple_books.models.book import Book
from py_apple_books.models.manager import ModelIterable
from py_apple_books.text import _SHORT_MAX_RAW, _coerce_text, is_short_selection
from py_apple_books.utils import APPLE_EPOCH_OFFSET, _apple_datetime_or_none

_HIGHLIGHT = int(AnnotationType.HIGHLIGHT)

# Picks fetched by id in one statement at most; more are found by reading
# the candidate scope again (an IN list this long would near SQLite's
# parameter limit on older versions).
_FETCH_BY_ID_MAX = 500

# The columns sample_highlights ranks from (one narrow query; the
# filters do the rest in SQL).
_SAMPLE_FIELDS = ("id", "uuid", "asset_id", "note", "selected_text")


def _require_annotation_store() -> None:
    """Raise :class:`AnnotationStoreNotFoundError`, as the annotation
    lists do, when the library has no annotation store."""
    if not current_library().has_annotations():
        raise AnnotationStoreNotFoundError(ANNOTATIONS_NOT_FOUND)


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


def _asset_of(api, book_id) -> Optional[str]:
    """The asset id of the book ``book_id`` names (R7); '' for a book
    without one, which has no annotations."""
    book = _book_arg(book_id, needs=("asset_id",), get_book=api.get_book_by_id)
    return book.asset_id or ""


def _iterable(name: str, value) -> list:
    """``value`` as a list of items, for an ``exclude_*`` argument: any
    iterable but a string; None is no items."""
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)):
        raise InvalidArgumentError(f"{name} must be an iterable of items, not a single {type(value).__name__}.")
    try:
        return list(value)
    except TypeError:
        raise InvalidArgumentError(f"{name} must be an iterable, not {type(value).__name__}.") from None


def _excluded_ids(values) -> FrozenSet[int]:
    """``exclude_ids`` as ints: ints (not bools) and strings of one."""
    ids = set()
    for item in _iterable("exclude_ids", values):
        if isinstance(item, bool):
            raise InvalidArgumentError("exclude_ids items must be annotation ids, not bool.")
        if isinstance(item, str):
            try:
                ids.add(int(item))
                continue
            except ValueError:
                raise InvalidArgumentError("exclude_ids items must be annotation ids (ints or "
                                           "strings of digits).") from None
        try:
            ids.add(operator.index(item))
        except TypeError:
            raise InvalidArgumentError(f"exclude_ids items must be annotation ids, "
                                       f"not {type(item).__name__}.") from None
    return frozenset(ids)


def _excluded_uuids(values) -> FrozenSet[str]:
    """``exclude_uuids`` in upper case (uuids compare case-insensitively)."""
    uuids = set()
    for item in _iterable("exclude_uuids", values):
        if not isinstance(item, str):
            raise InvalidArgumentError(f"exclude_uuids items must be strings, not {type(item).__name__}.")
        uuids.add(item.upper())
    return frozenset(uuids)


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
            store has neither the underline flag nor the style column,
            or lacks a column ``order_by`` needs (the default order
            needs the annotation creation date).
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

    def sample_highlights(self, limit: Optional[int] = 5, *, offset: Optional[int] = None, on=None,
                          seed: Optional[str] = None, book_id=None, after=None, before=None,
                          exclude_ids: Optional[Iterable] = (), exclude_uuids: Optional[Iterable[str]] = (),
                          exclude_short: bool = True, include_orphans: bool = False) -> ModelIterable:
        """A varied, repeatable sample of your highlights for one day
        (1.11): the same day and ``seed`` give the same picks, and a new
        day gives new ones. Resurfacing, not spaced repetition: nothing
        is stored, so pass what you've already shown in ``exclude_ids``
        or ``exclude_uuids``.

        Candidates are live highlights and notes (type 2) with text,
        made before the end of ``on``'s day (default today; rows without
        a creation date count), and inside ``after``/``before`` if given
        (:mod:`py_apple_books.engagement` has the date rules). Left out:
        highlights of books no longer in the library unless
        ``include_orphans``; words and short phrases
        (:attr:`Annotation.is_short_selection`) unless ``exclude_short``
        is False; ``exclude_ids`` (ints or strings of digits) and
        ``exclude_uuids`` (any case; None for either excludes nothing).

        They are ranked by :data:`py_apple_books.engagement.SAMPLE_ALGORITHM`
        (a highlight with a note counts twice) and, without ``book_id``,
        taken round-robin across books, so the first picks come from
        different books. ``offset`` and ``limit`` (default 5; None for
        every candidate, which reads the whole library) slice that order.

        Returns a :class:`ModelIterable` in sample order: ``count()``,
        slices and ``count_by()`` work on the picks, and their books load
        in one query.

        :param book_id: only this book's highlights: an id or a
            :class:`Book`.
        :raises InvalidArgumentError: a bad ``limit``, ``offset``, ``on``,
            ``after``, ``before`` or exclusion, or a ``seed`` that isn't a
            string.
        :raises BookNotFoundError: no book has id ``book_id``.
        :raises UnsupportedSchemaError: ``after`` or ``before`` on a store
            without the annotation creation date.
        """
        limit, offset = strict_limit(limit), strict_offset(offset)
        day = _eng._local_day(on)
        if seed is not None and not isinstance(seed, str):
            raise InvalidArgumentError(f"seed must be a string or None, not {type(seed).__name__}.")
        window = _eng._window_filters("creation_date", after, before)
        skip_ids = _excluded_ids(exclude_ids)
        skip_uuids = _excluded_uuids(exclude_uuids)
        asset = None if book_id is None else _asset_of(self, book_id)

        _require_annotation_store()
        if asset == "" or not Annotation.manager.has_fields("selected_text"):
            # No text to sample (documented): an empty result.
            return ModelIterable(lambda: [], Annotation)

        filters = {"type": _HIGHLIGHT, "is_deleted__isnot": 1, "selected_text__isnull": False, **window}
        if asset is not None:
            filters["asset_id"] = asset
        if not include_orphans:
            filters["asset_id__in"] = _in_library()
        where = None
        if Annotation.manager.has_fields("creation_date"):
            # Made before the end of the day; rows without a date count.
            where = Q(creation_date__lt=_eng._day_end(day)) | Q(creation_date__isnull=True)

        keys = list(Annotation._get_mappings("Annotation"))
        i_id, i_uuid, i_asset, i_note, i_text = (keys.index(k) for k in (
            "id", "uuid", "asset_id", "note", "selected_text"))
        candidates = []
        prefix = _eng._sample_prefix(day, seed)
        for row in Annotation.manager.filter(**filters, where=where, only=list(_SAMPLE_FIELDS)).run_query():
            text = _coerce_text(row[i_text])
            if not text or not text.strip():
                continue
            pk = row[i_id]
            if pk in skip_ids:
                continue
            ident = _eng._sample_ident(row[i_uuid], pk)
            if skip_uuids and ident in skip_uuids:
                continue
            if exclude_short and is_short_selection(text):
                continue
            note = _coerce_text(row[i_note])
            candidates.append((_eng._sample_key(day, seed, ident, bool(note and note.strip()), prefix=prefix),
                               pk, row[i_asset]))

        ranked = _eng._rank(candidates)
        if asset is None:
            ranked = _eng._round_robin(ranked)
        start = offset or 0
        picked = ranked[start:] if limit is None else ranked[start:start + limit]
        rows = _full_rows([pk for _, pk, _ in picked], filters, where)
        # Rows are read here, in the instance's library, which the
        # iterable (and the picks' relations) then read too.
        return ModelIterable(lambda: rows, Annotation)

    def get_vocabulary(self, limit: int = None, order_by: str = "-last_highlighted", *,
                       offset: int = None, book_id=None, after=None, before=None,
                       underline_only: bool = False) -> List[_eng.VocabularyEntry]:
        """The words and short phrases you highlighted, one
        :class:`~py_apple_books.engagement.VocabularyEntry` each (1.11).

        Highlights and notes (type 2, not deleted) whose text is a short
        selection (:attr:`Annotation.is_short_selection`) are grouped by
        their folded word or phrase, so ``'Ephemeral,'`` and
        ``'ephemeral'`` are one entry; there is no stemming. Highlights of
        books no longer in the library are included. ``book_id`` (an id
        or a :class:`Book`) keeps one book's, ``after``/``before`` a
        creation-date window (:mod:`py_apple_books.engagement` has the
        date rules), and ``underline_only`` underlines
        (:meth:`get_underlines`).

        ``order_by`` is ``'last_highlighted'``, ``'first_highlighted'``,
        ``'term'`` (folded) or ``'count'``, each optionally prefixed with
        ``'-'`` for descending; ties go by term, and entries without a
        dated highlight come last. ``offset`` and ``limit`` page over the
        entries. Returns a list built from one query, so for a total,
        call with ``limit=None`` and page yourself. On a Books version
        without the selected-text column, the list is empty.

        :raises InvalidArgumentError: a bad ``limit``, ``offset``,
            ``after`` or ``before``.
        :raises InvalidChoiceError: an unknown ``order_by``.
        :raises BookNotFoundError: no book has id ``book_id``.
        :raises UnsupportedSchemaError: ``underline_only`` on a store
            with neither the underline flag nor the style column, or
            ``after`` or ``before`` on a store without the annotation
            creation date.
        """
        limit, offset = strict_limit(limit), strict_offset(offset)
        field, descending = _eng._vocabulary_order(order_by)
        window = _eng._window_filters("creation_date", after, before)
        asset = None if book_id is None else _asset_of(self, book_id)

        _require_annotation_store()
        if asset == "" or not Annotation.manager.has_fields("selected_text"):
            return []
        filters = {"type": _HIGHLIGHT, "is_deleted__isnot": 1, "selected_text__isnull": False, **window}
        if asset is not None:
            filters["asset_id"] = asset
        if underline_only:
            filters.update(_underline_filter())
        column = Annotation.manager._get_db_field("selected_text")
        # Exact: is_short_selection refuses anything longer than
        # _SHORT_MAX_RAW code points, and SQLite's length() of the value
        # as text never counts more characters than Python's len() of the
        # decoded text (it stops at a NUL; invalid bytes count at most as
        # Python's U+FFFD do; the cast reads a BLOB cell as text too).
        short_enough = Where(f"length(CAST({column} AS TEXT))", _SHORT_MAX_RAW, operator="<=")
        i_text = list(Annotation._get_mappings("Annotation")).index("selected_text")
        members = [row for row in Annotation.manager.filter(**filters, where=short_enough).run_query()
                   if is_short_selection(row[i_text])]
        # One iterable over the members' rows, so their books load in one
        # query (and read the instance's library).
        entries = _eng._vocabulary(ModelIterable(lambda: members, Annotation))
        entries = _eng._sort_vocabulary(entries, field, descending)
        start = offset or 0
        return entries[start:] if limit is None else entries[start:start + limit]

    def get_highlight_activity(self, *, after=None, before=None, book_id=None,
                               granularity: Optional[str] = "month") -> _eng.HighlightActivity:
        """How much you highlighted, in total and per period (1.11).

        Counts highlights and notes (type 2, not deleted) with a creation
        date in the ``after``/``before`` window (all time by default;
        :mod:`py_apple_books.engagement` has the date rules), books no
        longer in the library included. ``book_id`` (an id or a
        :class:`Book`) keeps one book's. ``granularity`` groups them by
        local ``'day'``, ``'week'`` (ISO, from Monday), ``'month'`` or
        ``'year'`` (None: no periods). See
        :class:`~py_apple_books.engagement.HighlightActivity` for the
        fields and how they add up.

        This measures highlighting, not reading time, which Books
        doesn't record in a form the library can read. For a window with
        a bound, ``highlights`` equals the number of type-2 rows
        :meth:`get_annotations_by_date_range` returns for it whose
        ``creation_date`` is a date (a stored value too far out of range
        to read as one is not counted here).

        :raises InvalidArgumentError: a bad ``after`` or ``before``.
        :raises InvalidChoiceError: an unknown ``granularity``.
        :raises BookNotFoundError: no book has id ``book_id``.
        :raises UnsupportedSchemaError: the store has no annotation
            creation date.
        """
        granularity = _eng._granularity(granularity)
        window = _eng._window_filters("creation_date", after, before)
        asset = None if book_id is None else _asset_of(self, book_id)
        _require_annotation_store()
        rows = []
        if asset != "":
            filters = {"type": _HIGHLIGHT, "is_deleted__isnot": 1, "creation_date__isnull": False, **window}
            if asset is not None:
                filters["asset_id"] = asset
            keys = list(Annotation._get_mappings("Annotation"))
            i_asset, i_note, i_created = (keys.index(k) for k in ("asset_id", "note", "creation_date"))
            for row in Annotation.manager.filter(**filters, only=["id", "asset_id", "note", "creation_date"]
                                                 ).run_query():
                created = _apple_datetime_or_none(row[i_created])
                if created is None:
                    continue
                note = _coerce_text(row[i_note])
                rows.append((row[i_asset], created, bool(note and note.strip())))
        return _eng._activity(rows, _books_by_asset(), after=after, before=before, granularity=granularity)

    def get_highlight_streaks(self, *, on=None) -> _eng.HighlightStreaks:
        """Runs of consecutive local days on which you highlighted, up to
        ``on`` (default today) (1.11).

        Any highlight or note (type 2, not deleted) counts, books no
        longer in the library included. The current streak is the run
        ending on ``on``, or ending the day before if you haven't
        highlighted yet on ``on``. See
        :class:`~py_apple_books.engagement.HighlightStreaks`.

        This measures highlighting, not reading: a day read without a
        highlight breaks a streak.

        :raises InvalidArgumentError: a bad ``on``.
        :raises UnsupportedSchemaError: the store has no annotation
            creation date.
        """
        day = _eng._local_day(on)
        _require_annotation_store()
        i_created = list(Annotation._get_mappings("Annotation")).index("creation_date")
        days = set()
        for row in Annotation.manager.filter(type=_HIGHLIGHT, is_deleted__isnot=1,
                                             creation_date__lt=_eng._day_end(day),
                                             only=["id", "creation_date"]).run_query():
            created = _apple_datetime_or_none(row[i_created])
            if created is not None:
                days.add(created.date())
        return _eng._streaks(days, day)

    def get_reading_goals(self, *, prefs_path=None) -> Optional[_eng.ReadingGoals]:
        """Books' reading goals, as Books last saved them to disk (1.11):
        the yearly books goal, the daily reading goal, Books' current
        streak and its list of books finished towards the goal, as a
        :class:`~py_apple_books.engagement.ReadingGoals`.

        Read from Books' preferences file, next to the library's
        ``Documents`` folder (``prefs_path`` names another file). Never
        writes, starts no process, opens no database and keeps nothing.
        Books may hold newer values in memory while it runs
        (``ReadingGoals.modified`` says when the file was written).

        Returns None when there is no such file or it can't be read
        safely: the library isn't in an Apple Books container, the file
        is missing or not downloaded, its path leads into an iCloud
        Drive or other cloud folder (by name or through a symlinked
        folder), it isn't a regular file, it is over 8 MiB,
        or it isn't a readable property list. A single key missing or
        in an unexpected form leaves just that field None.

        :raises InvalidArgumentError: ``prefs_path`` isn't a path.
        """
        if prefs_path is None:
            path = _prefs.library_prefs_path(current_library())
        else:
            # os.fspath also rejects a PathLike whose __fspath__ returns
            # neither str nor bytes.
            try:
                path = os.fspath(prefs_path)
            except TypeError:
                path = None
            if path is None:
                raise InvalidArgumentError(f"prefs_path must be a path or None, not {type(prefs_path).__name__}.")
        return _prefs._read_goals(path)[0]


def _full_rows(ids: List[int], filters: dict, where) -> list:
    """Every column of the annotations ``ids``, in that order. A row
    gone since the candidates were read is left out."""
    if not ids:
        return []
    if len(ids) <= _FETCH_BY_ID_MAX:
        found = Annotation.manager.filter(id__in=ids).run_query()
    else:
        found = Annotation.manager.filter(**filters, where=where).run_query()
    i_id = list(Annotation._get_mappings("Annotation")).index("id")
    by_id = {row[i_id]: row for row in found}
    return [by_id[pk] for pk in ids if pk in by_id]
