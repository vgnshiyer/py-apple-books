"""The :class:`~py_apple_books.content.BookContent` mixin for spoiler-safe
reading (private, 1.11).

- ``search(query, ...)``: search the book's text, up to a boundary;
- ``resolve_boundary(boundary, *, precision='item')``: place a
  ``ReadBoundary`` in the book's text;
- ``position_at_percent(percent)``: where a share of the book's text
  starts;
- later, exact in-chapter positions (``positions_of``, ``position_of``,
  ``toc_positions``, ``chapter_at``, ``toc_before``).

They read the book's text through ``BookContent.iter_spine_text`` and
``BookContent.get_spine_item_text``. The rules of
:mod:`py_apple_books._content_resolve` apply here too: import
:mod:`py_apple_books.content` inside methods only, and do no I/O at
import. The mixin's memos are created on first use in the instance's
``__dict__`` (``BookContent`` pickles and copies only the attributes it
names, so they are dropped like its own runtime state); each holds small
immutable values, and every read or write of one is a single dict
operation, safe from several threads.
"""

import decimal
import math
import numbers
import operator
from bisect import bisect_right
from typing import Any, Dict, List, Optional, Tuple

from py_apple_books.exceptions import InvalidArgumentError, InvalidChoiceError
from py_apple_books.models.location import Location
from py_apple_books.positions import (
    BoundaryPrecision,
    BoundarySource,
    BoundaryWarning,
    ReadBoundary,
    ResolvedBoundary,
    TextPosition,
)
from py_apple_books.positions import _spine_index as _location_spine_index
from py_apple_books._messages import ELLIPSIS
from py_apple_books.text import _coerce_text, _fold_query, _iter_folded

#: Longest query :meth:`_ReadingMixin.search` accepts, in characters after
#: folding.
MAX_FOLDED_QUERY = 1000

#: Longest query accepted before folding, so that folding never costs more
#: than a bounded amount: generous, as runs of whitespace and of
#: characters that fold to nothing (soft hyphens, zero-width spaces,
#: combining marks) make a long query fold short.
_MAX_RAW_QUERY = 64 * MAX_FOLDED_QUERY

_QUERY_TOO_LONG = (f"The search query is too long: at most {MAX_FOLDED_QUERY} characters after "
                   f"folding and {_MAX_RAW_QUERY} before.")

#: Most characters a snippet keeps from each end of a long match (one
#: lengthened by characters that fold to nothing); the middle becomes an
#: ellipsis.
_MATCH_KEEP = 2000

#: The precisions ``resolve_boundary`` accepts today; ``'exact'`` comes
#: with exact in-chapter positions.
_PRECISIONS = ("item",)

#: Most resolved boundaries one instance remembers (each is small).
_RESOLVED_MEMO_SIZE = 256

# Attribute names of the memos in the instance's __dict__.
_RESOLVED_MEMO = "_reading_resolved_memo"
_LINEAR_MEMO = "_reading_linear_memo"

_START = TextPosition(0, 0)


def _context_size(value: Any, name: str) -> int:
    """A snippet context size: an int (not a bool) from 0 to
    ``MAX_SNIPPET_CONTEXT``."""
    from py_apple_books.content import MAX_SNIPPET_CONTEXT

    if not isinstance(value, bool):
        try:
            size = operator.index(value)
        except TypeError:
            size = None
        if size is not None and 0 <= size <= MAX_SNIPPET_CONTEXT:
            return size
    raise InvalidArgumentError(f"{name} must be a whole number from 0 to {MAX_SNIPPET_CONTEXT}.")


def _page_size(limit: Any) -> int:
    """Hits per page: ``limit`` (an integer of at least 1, R8), at most
    ``MAX_SEARCH_HITS``; None gives ``MAX_SEARCH_HITS``."""
    from py_apple_books._api._common import strict_limit
    from py_apple_books.content import MAX_SEARCH_HITS

    size = strict_limit(limit)
    return MAX_SEARCH_HITS if size is None else min(size, MAX_SEARCH_HITS)


def _folded_query(query: Any) -> str:
    """The folded query :meth:`_ReadingMixin.search` matches: converted
    like :func:`~py_apple_books.text.fold_for_match`'s input (None finds
    nothing, bytes are decoded as UTF-8, ``str()`` for other objects),
    folded, without leading or trailing whitespace.

    :raises InvalidArgumentError: longer than :data:`_MAX_RAW_QUERY`
        characters once converted (checked before folding, which costs
        time and memory in proportion), or than :data:`MAX_FOLDED_QUERY`
        after folding. The message never repeats the query.
    """
    text = _coerce_text(query)
    if text is not None and len(text) > _MAX_RAW_QUERY:
        raise InvalidArgumentError(_QUERY_TOO_LONG)
    folded = _fold_query(text)
    if len(folded) > MAX_FOLDED_QUERY:
        raise InvalidArgumentError(_QUERY_TOO_LONG)
    return folded


def _snippet(text: str, start: int, end: int, before: int, after: int) -> str:
    """The match ``text[start:end]`` with up to ``before``/``after``
    characters around it, on one line (every whitespace run one space,
    none at the ends). ``text`` is already cut at the boundary, so the
    snippet never reaches past it.

    A match longer than ``2 * _MATCH_KEEP`` characters (only characters
    that fold to nothing make one that long) keeps ``_MATCH_KEEP`` from
    each end, joined by an ellipsis, so a snippet holds at most about
    ``before + after + 2 * _MATCH_KEEP`` characters.
    """
    lo, hi = max(0, start - before), min(len(text), end + after)
    if end - start <= 2 * _MATCH_KEEP:
        return " ".join(text[lo:hi].split())
    head = " ".join(text[lo:start + _MATCH_KEEP].split())
    tail = " ".join(text[end - _MATCH_KEEP:hi].split())
    return f"{head} {ELLIPSIS} {tail}"


def _percent_value(percent: Any) -> float:
    """``percent`` as a float clamped to 0..100.

    :raises InvalidArgumentError: None, a bool, NaN, or anything that
        isn't a real number.
    """
    value = _real(percent)
    if value is None or math.isnan(value):
        raise InvalidArgumentError("percent must be a number from 0 to 100.")
    return min(100.0, max(0.0, value))


def _real(value: Any) -> Optional[float]:
    """``value`` as a float if it is a real number (an int, float,
    Fraction, Decimal or other ``numbers.Real``, not a bool); NaN for one
    that has no float value; else None."""
    if isinstance(value, bool) or not isinstance(value, (numbers.Real, decimal.Decimal)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return math.nan


def _precision(value: Any) -> str:
    """``precision``, checked: ``'item'`` (any case).

    :raises InvalidChoiceError: anything else, ``'exact'`` included
        until exact in-chapter positions ship.
    """
    if isinstance(value, str) and value.lower() in _PRECISIONS:
        return value.lower()
    raise InvalidChoiceError("precision must be 'item'.", value=value, valid=list(_PRECISIONS))


def _item_of(location: Any, spine) -> Tuple[Optional[int], bool]:
    """``(spine index, mismatch)`` of the spine item ``location`` is in.

    The CFI names the item twice: by its spine step (``/6/N``) and by
    the manifest id in brackets after it. When they agree (the item at
    the step has that id), that item. When they disagree and both name
    an item in this spine, the earlier one (``mismatch`` True), so the
    boundary is never later than either. Otherwise whichever names an
    item; None when neither does.
    """
    if not isinstance(location, Location):
        return None, False
    step = _location_spine_index(location)
    if step is not None and not 0 <= step < len(spine):
        step = None
    idref = location.chapter_id
    if idref is not None:
        if step is not None and spine[step].item_id == idref:
            return step, False
        named = next((entry.index for entry in spine if entry.item_id == idref), None)
        if named is not None:
            if step is None:
                return named, _location_spine_index(location) is not None
            return min(named, step), True
    return step, False


class _ReadingMixin:
    """Private mixin of :class:`~py_apple_books.content.BookContent`."""

    __slots__ = ()

    # -- search ---------------------------------------------------------------

    def search(
        self,
        query: Any,
        *,
        start: Optional[TextPosition] = None,
        until: Any = None,
        limit: Optional[int] = 20,
        chars_before: int = 80,
        chars_after: int = 80,
        count_total: bool = False,
        count_withheld: bool = False,
        include_nonlinear: bool = False,
        include_toc_pages: bool = False,
    ):
        """Search the book's text, in reading order, up to a boundary
        (1.11).

        Matching is folded (:func:`py_apple_books.text.finditer_folded`):
        case, accents, typographic quotes and dashes, ligatures and
        whitespace runs don't matter. The scope is that of
        :meth:`iter_spine_text`: from ``start`` up to ``until``
        (exclusive), linear items only unless asked otherwise. Text at or
        after ``until`` is cut off before matching, so no match runs
        across it and no snippet shows anything past it.

        Results come a page at a time: pass :attr:`TextSearchResult.next_start`
        back as ``start`` (with the same other arguments) for the next
        page.

        :param query: The text to find. Converted like
            :func:`~py_apple_books.text.fold_for_match`'s input (None
            finds nothing, bytes are decoded as UTF-8, ``str()`` for
            numbers and other objects); leading and trailing whitespace
            is ignored. A query that folds to nothing finds nothing.
        :param start: Where to start (default: the start of the book). A
            match is on the page if it starts at or after ``start``.
        :param until: Where the readable text ends: a
            :class:`~py_apple_books.positions.TextPosition` or a
            :class:`~py_apple_books.positions.ResolvedBoundary` (from
            :meth:`resolve_boundary`); None searches to the end of the
            book.
        :param limit: Most hits on this page (default 20); None gives
            ``MAX_SEARCH_HITS``, which is also the most any page holds.
        :param chars_before: Characters of context before each match in
            its snippet, 0 to ``MAX_SNIPPET_CONTEXT`` (default 80). A
            match made very long by characters that fold to nothing (over
            4,000 characters) keeps 2,000 from each end in the snippet,
            joined by an ellipsis.
        :param chars_after: The same, after the match.
        :param count_total: Also count every match in scope (from
            ``start`` to ``until``), in :attr:`TextSearchResult.total`:
            reads the whole scope.
        :param count_withheld: Also count the matches past ``until``,
            in :attr:`TextSearchResult.withheld_in_item` (the rest of
            ``until``'s own item) and :attr:`TextSearchResult.withheld_later`
            (later items in scope): reads the rest of the book, but no
            text past ``until`` is returned. Both 0 without ``until``.
        :param include_nonlinear: Also search items marked
            ``linear="no"`` (notes, for example).
        :param include_toc_pages: Also search table-of-contents pages.
        :returns: A :class:`~py_apple_books.content.TextSearchResult`.
        :raises InvalidArgumentError: a query over 1,000 characters
            after folding or 64,000 before (the message doesn't repeat
            it), a bad ``limit``, ``chars_before`` or ``chars_after``, a
            bad ``start`` or ``until``, or a boundary resolved for
            another book. Checked before the book is read.
        :raises NotEpubError, BookNotDownloadedError,
            DRMProtectedError, AppleBooksError: as for
            :meth:`list_spine_items`.
        """
        from py_apple_books.content import TextSearchResult

        folded = _folded_query(query)
        page = _page_size(limit)
        before = _context_size(chars_before, "chars_before")
        after = _context_size(chars_after, "chars_after")
        if start is not None and not isinstance(start, TextPosition):
            raise InvalidArgumentError("start must be a TextPosition or None.")
        stop = self._until_position(until)
        include_nonlinear, include_toc_pages = bool(include_nonlinear), bool(include_toc_pages)
        if not folded:
            zero = 0 if count_withheld else None
            return TextSearchResult((), None, 0 if count_total else None, zero, zero)

        first = start or _START
        hits: List[Any] = []
        next_start: Optional[TextPosition] = None
        total = 0
        for chunk in self.iter_spine_text(start=TextPosition(first.spine_index, 0), until=stop,
                                          include_nonlinear=include_nonlinear,
                                          include_toc_pages=include_toc_pages):
            if not chunk.readable:
                continue
            floor = first.offset if chunk.index == first.spine_index else 0
            for lo, hi in _iter_folded(chunk.text, folded):
                if lo < floor:
                    continue
                total += 1
                if len(hits) < page:
                    hits.append(self._hit(chunk, lo, hi, before, after))
                elif next_start is None:
                    next_start = TextPosition(chunk.index, lo)
                    if not count_total:
                        break
            if next_start is not None and not count_total:
                break

        in_item = later = None
        if count_withheld:
            in_item, later = self._withheld(folded, stop, include_nonlinear, include_toc_pages)
        return TextSearchResult(tuple(hits), next_start, total if count_total else None, in_item, later)

    @staticmethod
    def _hit(chunk, lo: int, hi: int, before: int, after: int):
        from py_apple_books.content import TextHit

        return TextHit(TextPosition(chunk.index, lo), TextPosition(chunk.index, hi), chunk.item_id,
                       _snippet(chunk.text, lo, hi, before, after))

    def _withheld(self, folded: str, stop: Optional[TextPosition], include_nonlinear: bool,
                  include_toc_pages: bool) -> Tuple[int, int]:
        """``(in the boundary's item, in later items)``: the matches past
        ``stop`` in scope, counted but never returned."""
        if stop is None:
            return 0, 0

        def count(text: str) -> int:
            return sum(1 for _ in _iter_folded(text, folded))

        in_item = later = 0
        for chunk in self.iter_spine_text(start=TextPosition(stop.spine_index, 0),
                                          include_nonlinear=include_nonlinear,
                                          include_toc_pages=include_toc_pages):
            if not chunk.readable:
                continue
            if chunk.index == stop.spine_index:
                # Matches of the whole item less those of the part before
                # the boundary: the ones that end past it (straddling
                # included).
                in_item = max(0, count(chunk.text) - count(chunk.text[:stop.offset]))
            else:
                later += count(chunk.text)
        return in_item, later

    # -- percents ---------------------------------------------------------------

    def position_at_percent(self, percent: Any) -> TextPosition:
        """Where the given share of the book's text lies: the start of the
        linear spine item that holds that share of the linear text (1.11).

        The share is measured over the text of every linear spine item
        (table-of-contents pages included; items marked
        ``linear="no"`` and items without text left out), so ``0`` gives
        the start of the first item with text and ``100`` the start of
        the last. Being an item's start, the result is never later than
        the point the percent stands for, as long as the items' text is
        in proportion to what Apple Books counts.

        The first call reads every linear item's text (as
        :meth:`get_spine_item_text`); their lengths are remembered by the
        instance, so later calls only check the book's index.

        :param percent: A number from 0 to 100, such as
            :attr:`ReadBoundary.progress
            <py_apple_books.positions.ReadBoundary.progress>`; values
            outside are clamped.
        :returns: A :class:`~py_apple_books.positions.TextPosition` at an
            item's start; ``TextPosition(0, 0)`` for a book without
            linear text.
        :raises InvalidArgumentError: None, a bool, NaN, or anything that
            isn't a real number.
        :raises NotEpubError, BookNotDownloadedError,
            DRMProtectedError, AppleBooksError: as for
            :meth:`list_spine_items`.
        """
        value = _percent_value(percent)
        starts, indexes, total = self._linear_table()
        if not total:
            return _START
        target = value / 100.0 * total
        i = min(max(bisect_right(starts, target) - 1, 0), len(indexes) - 1)
        return TextPosition(indexes[i], 0)

    def _linear_table(self) -> Tuple[Tuple[int, ...], Tuple[int, ...], int]:
        """``(starts, indexes, total)`` over the linear items with text:
        each one's start in the linear text, its spine index, and the
        length of all of it. Remembered per index of the book (a book
        whose files changed is measured again)."""
        spine = self._gated_index().spine
        memo = self.__dict__.get(_LINEAR_MEMO)
        if memo is not None and memo[0] is spine:
            return memo[1]
        starts: List[int] = []
        indexes: List[int] = []
        total = 0
        for chunk in self.iter_spine_text(include_toc_pages=True):
            if chunk.readable and chunk.length:
                starts.append(total)
                indexes.append(chunk.index)
                total += chunk.length
        table = (tuple(starts), tuple(indexes), total)
        self.__dict__[_LINEAR_MEMO] = (spine, table)
        return table

    # -- boundaries -------------------------------------------------------------

    def resolve_boundary(self, boundary: ReadBoundary, *, precision: str = "item") -> ResolvedBoundary:
        """Place a :class:`~py_apple_books.positions.ReadBoundary` (from
        ``PyAppleBooks.get_read_boundary``) in this book's text (1.11).

        The text before the result's
        :attr:`~py_apple_books.positions.ResolvedBoundary.position` has
        been read; pass the result as ``until`` to :meth:`search` or
        :meth:`iter_spine_text`. The position is never later than the
        point the boundary stands for.

        ``precision='item'`` (the default, and the only precision
        today) places the boundary at the start of the spine item it
        falls in:

        * ``READING_POSITION``: the start of the bookmark's item. A
          bookmark whose spine step and file id disagree uses the earlier
          item (``BOOKMARK_INDEX_MISMATCH``). A bookmark on an auxiliary
          item (marked non-linear, ``BOOKMARK_NONLINEAR``, or a
          table-of-contents page, ``BOOKMARK_TOC_PAGE``) or one not found
          in the book (``BOOKMARK_UNRESOLVED``) falls back to the
          highlight, then the progress, then the start of the book; after
          an auxiliary item, never later than that item's start.
        * ``RECENT_HIGHLIGHT``: the start of the highlight's item (one
          not found, ``HIGHLIGHT_UNRESOLVED``, or on an auxiliary item
          falls back to the progress, then the start of the book; after
          an auxiliary item, never later than that item's start). A
          highlight on an auxiliary item adds no warning in this release,
          so a result placed there looks like a placement at a linear
          highlight.
        * ``PROGRESS``: :meth:`position_at_percent` of the progress.
        * ``FURTHEST``: the later of the reading position (placed as
          above) and :meth:`position_at_percent` of the furthest point
          read.
        * ``NONE``: the start of the book.

        The result's ``source`` names what placed it after any fallback,
        and its ``warnings`` add those from placing it to the
        boundary's. Results are remembered by the instance.

        :param boundary: A :class:`~py_apple_books.positions.ReadBoundary`.
        :param precision: ``'item'`` (any case).
        :raises InvalidChoiceError: any other ``precision``
            (``'exact'`` included, until exact in-chapter positions
            ship).
        :raises InvalidArgumentError: ``boundary`` isn't a
            ``ReadBoundary``, or is for another book than this
            instance's :attr:`book_id` (an instance made from a path
            alone can't tell, and places it as is).
        :raises NotEpubError, BookNotDownloadedError,
            DRMProtectedError, AppleBooksError: as for
            :meth:`list_spine_items`.
        """
        precision = _precision(precision)
        if not isinstance(boundary, ReadBoundary):
            raise InvalidArgumentError(
                "boundary must be a ReadBoundary (from PyAppleBooks.get_read_boundary).")
        mine = self._book_id
        if mine is not None and boundary.book_id is not None and boundary.book_id != mine:
            raise InvalidArgumentError("This boundary is for another book.")
        spine = self._gated_index().spine
        memo: Dict[Any, Any] = self.__dict__.setdefault(_RESOLVED_MEMO, {})
        key = (boundary, precision)
        try:
            held = memo.get(key)
        except TypeError:  # a hand-made boundary holding something unhashable
            key = held = None
        if held is not None and held[0] is spine:
            return held[1]
        resolved = self._resolve_item(boundary, spine)
        if key is not None:
            if len(memo) >= _RESOLVED_MEMO_SIZE:
                memo.clear()
            memo[key] = (spine, resolved)
        return resolved

    def _resolve_item(self, boundary: ReadBoundary, spine) -> ResolvedBoundary:
        warnings: List[BoundaryWarning] = list(boundary.warnings)
        position, precision, source = self._position_tier(boundary, spine, warnings)
        if boundary.source == BoundarySource.FURTHEST:
            high_water = _usable_percent(boundary.high_water)
            if high_water is not None:
                furthest = self.position_at_percent(high_water)
                if furthest > position:
                    position, precision, source = furthest, BoundaryPrecision.SPINE_ITEM, BoundarySource.FURTHEST
        return ResolvedBoundary(self._book_id, boundary, position, precision, source,
                                tuple(dict.fromkeys(warnings)))

    def _position_tier(self, boundary: ReadBoundary, spine, warnings: List[BoundaryWarning]
                       ) -> Tuple[TextPosition, BoundaryPrecision, BoundarySource]:
        """The reading position's place: the bookmark, else the
        highlight, else the progress, else the start, each tried only if
        the boundary's source is at or above it."""
        source = boundary.source
        cap: Optional[Tuple[TextPosition, BoundarySource]] = None
        tiers = {
            BoundarySource.FURTHEST: 0,
            BoundarySource.READING_POSITION: 0,
            BoundarySource.RECENT_HIGHLIGHT: 1,
            BoundarySource.PROGRESS: 2,
        }
        tier = tiers.get(source, 3)
        placed: Optional[Tuple[TextPosition, BoundaryPrecision, BoundarySource]] = None
        if tier <= 0 and boundary.bookmark is not None:
            index, mismatch = _item_of(boundary.bookmark, spine)
            if mismatch:
                warnings.append(BoundaryWarning.BOOKMARK_INDEX_MISMATCH)
            if index is None:
                warnings.append(BoundaryWarning.BOOKMARK_UNRESOLVED)
            elif not spine[index].linear:
                warnings.append(BoundaryWarning.BOOKMARK_NONLINEAR)
                cap = (TextPosition(index, 0), BoundarySource.READING_POSITION)
            elif spine[index].is_toc_page:
                warnings.append(BoundaryWarning.BOOKMARK_TOC_PAGE)
                cap = (TextPosition(index, 0), BoundarySource.READING_POSITION)
            else:
                placed = (TextPosition(index, 0), BoundaryPrecision.SPINE_ITEM,
                          BoundarySource.READING_POSITION)
        if placed is None and tier <= 1 and boundary.highlight is not None:
            index, _ = _item_of(boundary.highlight, spine)
            if index is None:
                warnings.append(BoundaryWarning.HIGHLIGHT_UNRESOLVED)
            elif not spine[index].linear or spine[index].is_toc_page:
                here = (TextPosition(index, 0), BoundarySource.RECENT_HIGHLIGHT)
                if cap is None or here[0] < cap[0]:
                    cap = here
            else:
                placed = (TextPosition(index, 0), BoundaryPrecision.SPINE_ITEM,
                          BoundarySource.RECENT_HIGHLIGHT)
        if placed is None and tier <= 2:
            progress = _usable_percent(boundary.progress)
            if progress is not None:
                placed = (self.position_at_percent(progress), BoundaryPrecision.SPINE_ITEM,
                          BoundarySource.PROGRESS)
        if placed is None:
            placed = (_START, BoundaryPrecision.START, BoundarySource.NONE)
        if cap is not None and cap[0] < placed[0]:
            placed = (cap[0], BoundaryPrecision.SPINE_ITEM, cap[1])
        return placed


def _usable_percent(value: Any) -> Optional[float]:
    """A boundary's percent, if it is a finite number above 0."""
    value = _real(value)
    return value if value is not None and math.isfinite(value) and value > 0 else None
