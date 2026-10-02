"""Where a reader is in a book, and how far they have read (new in 1.11).

The value types the reading-position, annotation-location, annotation
context and spoiler-safe reading APIs return. Import them from here,
like :class:`~py_apple_books.content.Chapter` from
:mod:`py_apple_books.content`; they are not in the top-level
``py_apple_books`` namespace.

Where am I:

* :class:`ChapterMatch`: how a location was placed in the table of
  contents.
* :class:`ResolvedLocation`: a location's chapter and spine file
  (``BookContent.resolve``, ``PyAppleBooks.get_annotation_locations``).
* :class:`UnavailableReason`: why a chapter, a position or a context
  couldn't be given, with :meth:`UnavailableReason.of` to map an error.
* :class:`PositionSource` and :class:`ReadingPosition`
  (``PyAppleBooks.get_reading_position``).
* :class:`TextMatch` and :class:`AnnotationContext`
  (``PyAppleBooks.get_annotation_context``).

How far have I read (spoiler-safe reading):

* :class:`TextPosition`: a point in a book's text, and the cursor format
  of the book-text APIs (``"N:M"``).
* :class:`BoundarySource`, :class:`BoundaryPrecision` and
  :class:`BoundaryWarning`.
* :class:`ReadBoundary` (``PyAppleBooks.get_read_boundary``, from the
  database only) and :class:`ResolvedBoundary`
  (``BookContent.resolve_boundary``, a :class:`TextPosition` in the
  book's text).

Every type is a frozen (immutable, hashable, picklable) dataclass or a
``str`` enum whose ``str()`` is its value. This module does no I/O and
imports neither the book-content module nor the models at import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional, Tuple

from py_apple_books.exceptions import (
    AppleBooksError,
    BookNotDownloadedError,
    ChapterNotFoundError,
    ContextUnavailableError,
    DBError,
    DRMProtectedError,
    InvalidArgumentError,
    NotEpubError,
    NotInLibraryError,
)

if TYPE_CHECKING:
    from py_apple_books.content import Chapter
    from py_apple_books.models.location import Location

__all__ = [
    "ChapterMatch",
    "ResolvedLocation",
    "UnavailableReason",
    "PositionSource",
    "ReadingPosition",
    "TextMatch",
    "AnnotationContext",
    "TextPosition",
    "BoundarySource",
    "BoundaryPrecision",
    "BoundaryWarning",
    "ReadBoundary",
    "ResolvedBoundary",
]


# -- where am I -----------------------------------------------------------------


class ChapterMatch(str, Enum):
    """How a location was placed in the table of contents.

    Every ToC entry whose file is in the spine starts a section: at the
    start of its file when the file holds only that entry (or the entry
    has no fragment, or its anchor isn't in the file), else at its
    anchor. A location belongs to the last section starting at or
    before it.
    """

    #: The location's spine file holds exactly one ToC entry, which is
    #: the chapter, whatever its fragment.
    FILE = "file"
    #: The file holds several ToC entries; the chapter is the last one
    #: starting at or before the location.
    ANCHOR = "anchor"
    #: The file holds no ToC entry, or the location is before every entry
    #: starting in it; the chapter is the last entry starting earlier in
    #: reading order.
    PRECEDING = "preceding"
    #: No entry starts before the location; there is no chapter.
    FRONT_MATTER = "front_matter"
    #: The file holds several ToC entries, but where they start couldn't
    #: be compared with the location (the file is unreadable, not
    #: downloaded, too large or unparseable, or this comparison is turned
    #: off); there is no chapter, and the spine file is still known. Also
    #: given for a location that would be ``PRECEDING`` when the file
    #: before it holds several entries whose order in that file can't be
    #: read, so the last of them isn't known.
    SECTION_UNKNOWN = "section_unknown"

    def __str__(self) -> str:
        return self.value


class UnavailableReason(str, Enum):
    """Why a chapter, a reading position's chapter or an annotation's
    context couldn't be given.

    The values are stable strings; other 1.11 types that report a reason
    for a book reuse them.
    """

    #: The annotation records no usable location (no CFI, or one that
    #: points into no spine file).
    NO_LOCATION = "no_location"
    #: The annotation has no text to find (a bookmark).
    NO_HIGHLIGHT_TEXT = "no_highlight_text"
    #: The annotation's book is no longer in the library.
    ORPHANED = "orphaned"
    #: The book is an Apple Books Store series volume or series that
    #: isn't in the library, so there is no file.
    NOT_OWNED = "not_owned"
    #: The book, or part of it, is stored only in iCloud, or it has no
    #: file on this Mac.
    NOT_DOWNLOADED = "not_downloaded"
    #: The book is DRM-protected.
    DRM = "drm"
    #: The book isn't an EPUB (a PDF, for example).
    NOT_EPUB = "not_epub"
    #: The book's files couldn't be read (a malformed or unsafe EPUB, or
    #: a read error).
    UNREADABLE = "unreadable"
    #: The location's file isn't in the book.
    CHAPTER_NOT_FOUND = "chapter_not_found"
    #: The location's file has no text (images only, for example).
    EMPTY_CHAPTER = "empty_chapter"
    #: The highlighted text wasn't found in its file.
    HIGHLIGHT_NOT_FOUND = "highlight_not_found"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def of(cls, exc: Any) -> Optional["UnavailableReason"]:
        """The reason an error of the library stands for, or None.

        Checked in this order (a subclass is matched before its base):

        * :class:`~py_apple_books.exceptions.ContextUnavailableError`:
          its ``reason`` (an :class:`UnavailableReason` or its value).
          When that is missing or unknown, the rules below apply, so a
          plain ``ContextUnavailableError`` gives ``UNREADABLE``.
        * :class:`~py_apple_books.exceptions.NotInLibraryError`:
          ``NOT_OWNED`` (before its base, ``BookNotDownloadedError``).
        * :class:`~py_apple_books.exceptions.BookNotDownloadedError`:
          ``NOT_DOWNLOADED``.
        * :class:`~py_apple_books.exceptions.DRMProtectedError`: ``DRM``.
        * :class:`~py_apple_books.exceptions.NotEpubError`: ``NOT_EPUB``.
        * :class:`~py_apple_books.exceptions.ChapterNotFoundError`:
          ``CHAPTER_NOT_FOUND``.
        * :class:`~py_apple_books.exceptions.DBError` (and its
          subclasses): None; a database failure isn't a reason about the
          book.
        * Any other :class:`~py_apple_books.exceptions.AppleBooksError`:
          ``UNREADABLE``.
        * Anything else (another exception, or not an exception): None.

        Meant for the errors the book-content and context APIs raise.
        Never raises.
        """
        if isinstance(exc, ContextUnavailableError):
            reason = cls._lookup(getattr(exc, "reason", None))
            if reason is not None:
                return reason
        if isinstance(exc, NotInLibraryError):
            return cls.NOT_OWNED
        if isinstance(exc, BookNotDownloadedError):
            return cls.NOT_DOWNLOADED
        if isinstance(exc, DRMProtectedError):
            return cls.DRM
        if isinstance(exc, NotEpubError):
            return cls.NOT_EPUB
        if isinstance(exc, ChapterNotFoundError):
            return cls.CHAPTER_NOT_FOUND
        if isinstance(exc, DBError):
            return None
        if isinstance(exc, AppleBooksError):
            return cls.UNREADABLE
        return None

    @classmethod
    def _lookup(cls, value: Any) -> Optional["UnavailableReason"]:
        """The member whose value is ``value`` (a member or its string),
        or None."""
        if not isinstance(value, str):
            return None
        try:
            return cls(value)
        except Exception:  # noqa: BLE001 - an unknown value (or a str subclass that misbehaves)
            return None


@dataclass(frozen=True)
class ResolvedLocation:
    """Where a location sits in a book: its spine file and its chapter.

    :attr chapter: The ToC entry the location belongs to (see
        :class:`ChapterMatch`); None for ``FRONT_MATTER`` and
        ``SECTION_UNKNOWN``, and when :attr:`unavailable` is set.
    :attr match: How the chapter was chosen; None only when
        :attr:`unavailable` is set.
    :attr spine_index: The 0-based spine position of the location's
        file, or None when unknown.
    :attr item_id: That file's manifest id, or None when unknown. Read
        the whole file with ``BookContent.get_spine_item_text(item_id)``;
        ``get_chapter(item_id)`` can return only part of it.
    :attr unavailable: Why no chapter could be given (the book can't be
        read, or the annotation has no location), or None.
    """

    chapter: Optional[Chapter]
    match: Optional[ChapterMatch]
    spine_index: Optional[int]
    item_id: Optional[str]
    unavailable: Optional[UnavailableReason] = None


class PositionSource(str, Enum):
    """Where a :class:`ReadingPosition` comes from."""

    #: Apple Books' reading-position row for the book (a bookmark row,
    #: also where its page data comes from).
    BOOKMARK = "bookmark"
    #: Inferred from the newest highlight or bookmark with a location,
    #: because the reading-position row has none.
    RECENT_ANNOTATION = "recent_annotation"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ReadingPosition:
    """Where the reader is in a book.

    :attr book_id: The book's id.
    :attr source: Where the position comes from; ``RECENT_ANNOTATION``
        positions are inferred.
    :attr annotation_id: The id of the annotation row the position was
        read from.
    :attr updated: When that row was last changed (a bookmark) or
        created (an inferred position), or None.
    :attr location: The position's CFI, or None (a position by page).
    :attr spine_index: The 0-based spine position of the position's
        file, or None.
    :attr item_id: That file's manifest id, or None. Read the whole file
        with ``BookContent.get_spine_item_text(item_id)``.
    :attr chapter: The ToC entry the position belongs to, or None (not
        asked for, not found, or see :attr:`match` and
        :attr:`unavailable`).
    :attr match: How the chapter was chosen, or None.
    :attr total_chapters: The number of ToC entries, or None.
    :attr unavailable: Why the chapter couldn't be given when it was
        asked for, or None. The position itself is still given.
    :attr fraction: Where the reader is, as a 0..1 fraction of the book,
        as Books records it on the reading-position row, or None.
    :attr furthest_fraction: The furthest point read, as a 0..1
        fraction, when Books records one at or past :attr:`fraction`;
        else None.
    :attr page: The 1-based page (PDFs), or None.
    :attr page_count: The number of pages, or None when unknown.
    :attr page_count_estimated: Whether :attr:`page_count` was estimated
        from :attr:`page` and :attr:`fraction` rather than recorded.
    """

    book_id: int
    source: PositionSource
    annotation_id: Any
    updated: Optional[datetime]
    location: Optional[Location]
    spine_index: Optional[int]
    item_id: Optional[str]
    chapter: Optional[Chapter] = None
    match: Optional[ChapterMatch] = None
    total_chapters: Optional[int] = None
    unavailable: Optional[UnavailableReason] = None
    fraction: Optional[float] = None
    furthest_fraction: Optional[float] = None
    page: Optional[int] = None
    page_count: Optional[int] = None
    page_count_estimated: bool = False


class TextMatch(str, Enum):
    """How an annotation's text was found in its file, from the strictest
    comparison to the most lenient."""

    #: Character for character: the occurrence found with whitespace
    #: runs compared as one space is verbatim the text (an earlier
    #: occurrence differing only in whitespace is still the one taken,
    #: and is ``WHITESPACE``).
    EXACT = "exact"
    #: With whitespace runs compared as one space.
    WHITESPACE = "whitespace"
    #: Also ignoring soft hyphens, zero-width spaces and byte-order marks.
    INVISIBLE = "invisible"
    #: With the fold the library's searches use (case, accents, quote and
    #: dash style; see ``py_apple_books.text.fold_for_match``).
    FOLDED = "folded"

    def __str__(self) -> str:
        return self.value


_ELLIPSIS = "…"


@dataclass(frozen=True)
class AnnotationContext:
    """An annotation's highlighted text with the text around it.

    :attr annotation_id: The annotation's id.
    :attr book_id: Its book's id.
    :attr item_id: The manifest id of the spine file the text comes from.
    :attr spine_index: That file's 0-based spine position, or None.
    :attr chapter: The ToC entry the annotation belongs to, or None (see
        :attr:`match`).
    :attr match: How the chapter was chosen, or None.
    :attr before: The text before the highlight, whitespace collapsed.
    :attr highlight: The highlighted text as it appears in the book,
        whitespace collapsed.
    :attr after: The text after the highlight, whitespace collapsed.
    :attr clipped_start: Whether the file has text before :attr:`before`.
    :attr clipped_end: Whether the file has text after :attr:`after`.
    :attr text_match: How the highlight was found in the file.
    :attr occurrences: How many times it occurs there.
    :attr disambiguated: Whether, of several occurrences, one was chosen
        with the annotation's surrounding text rather than taken first.

    ``str()`` (and :attr:`text`) gives the passage as one line, with an
    ellipsis (``…``) where it was clipped.
    """

    annotation_id: Any
    book_id: Any
    item_id: Optional[str]
    spine_index: Optional[int]
    chapter: Optional[Chapter]
    match: Optional[ChapterMatch]
    before: str
    highlight: str
    after: str
    clipped_start: bool
    clipped_end: bool
    text_match: TextMatch = TextMatch.EXACT
    occurrences: int = 1
    disambiguated: bool = False

    @property
    def text(self) -> str:
        """``before + highlight + after``, with an ellipsis (``…``) in
        front when :attr:`clipped_start` and at the end when
        :attr:`clipped_end`."""
        prefix = _ELLIPSIS if self.clipped_start else ""
        suffix = _ELLIPSIS if self.clipped_end else ""
        return f"{prefix}{self.before}{self.highlight}{self.after}{suffix}"

    def __str__(self) -> str:
        return self.text


# -- how far have I read ------------------------------------------------------------

# Digits ``TextPosition.parse`` accepts, and so the largest values a
# TextPosition holds (every TextPosition's ``str()`` parses back).
_MAX_SPINE_DIGITS = 9
_MAX_OFFSET_DIGITS = 12
_MAX_SPINE_INDEX = 10 ** _MAX_SPINE_DIGITS - 1
_MAX_OFFSET = 10 ** _MAX_OFFSET_DIGITS - 1
_MAX_TEXT = _MAX_SPINE_DIGITS + 1 + _MAX_OFFSET_DIGITS
# ASCII digits only (``\d`` would take other scripts' digits too).
_POSITION = re.compile(r"([0-9]{1,%d}):([0-9]{1,%d})" % (_MAX_SPINE_DIGITS, _MAX_OFFSET_DIGITS))

_PARSE_MESSAGE = (
    "A text position is written 'N:M': the spine index (up to "
    f"{_MAX_SPINE_DIGITS} digits) and the offset in that item (up to "
    f"{_MAX_OFFSET_DIGITS} digits), in ASCII digits, such as '12:345'."
)


def _whole_number(value: Any, name: str, maximum: int) -> int:
    """``value`` as a plain int, if it is an int (not a bool) in
    ``0..maximum``; else :class:`InvalidArgumentError` (the message names
    the field and the range, never the value)."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum:
        return int(value)
    raise InvalidArgumentError(
        f"TextPosition.{name} must be a whole number from 0 to {maximum}.")


@dataclass(frozen=True, order=True)
class TextPosition:
    """A point in a book's text: :attr:`offset` characters (code points)
    into the text of spine item :attr:`spine_index`, as
    ``BookContent.get_spine_item_text(spine_index)`` returns it.

    :attr spine_index: The 0-based position among the book's spine items
        (the ``<itemref>`` elements of its package document, linear or
        not), so it is the same number as ``Location.spine_index``.
    :attr offset: Characters into that item's text; 0 is its start.

    Positions order in reading order. ``str()`` writes one as ``"N:M"``
    and :meth:`parse` reads it back: the cursor format of the book-text
    APIs. A position is valid for one book file and one version of the
    library's text extraction; it is not an offset into
    ``get_chapter`` text.

    Raises :class:`~py_apple_books.exceptions.InvalidArgumentError` (a
    ``ValueError``) unless both fields are ints (not bools) from 0, the
    spine index at most 999,999,999 and the offset at most
    999,999,999,999.
    """

    spine_index: int
    offset: int = 0

    def __post_init__(self) -> None:
        # frozen=True: store the plain-int values with object.__setattr__.
        object.__setattr__(
            self, "spine_index", _whole_number(self.spine_index, "spine_index", _MAX_SPINE_INDEX))
        object.__setattr__(self, "offset", _whole_number(self.offset, "offset", _MAX_OFFSET))

    def __str__(self) -> str:
        return f"{self.spine_index}:{self.offset}"

    @classmethod
    def parse(cls, text: str) -> "TextPosition":
        """The position ``text`` writes as ``"N:M"`` (the inverse of
        ``str()``).

        Accepts exactly two runs of ASCII digits joined by one colon: the
        spine index (up to 9 digits) and the offset (up to 12), with no
        sign, space or other character. Anything else raises
        :class:`~py_apple_books.exceptions.InvalidArgumentError`, whose
        message doesn't repeat the input.
        """
        if isinstance(text, str) and len(text) <= _MAX_TEXT:
            m = _POSITION.fullmatch(text)
            if m:
                return cls(int(m.group(1)), int(m.group(2)))
        raise InvalidArgumentError(_PARSE_MESSAGE)


class BoundarySource(str, Enum):
    """What a :class:`ReadBoundary` (or a :class:`ResolvedBoundary`) is
    placed by."""

    #: Apple Books' reading-position bookmark.
    READING_POSITION = "reading_position"
    #: The newest highlight (or other annotation with a location), as no
    #: reading-position bookmark is usable.
    RECENT_HIGHLIGHT = "recent_highlight"
    #: The book's reading progress (percent), as no location is usable.
    PROGRESS = "progress"
    #: The furthest point read (asked for with ``basis='furthest'``), which
    #: is past the reading position.
    FURTHEST = "furthest"
    #: Nothing: nothing is known to have been read.
    NONE = "none"

    def __str__(self) -> str:
        return self.value


class BoundaryPrecision(str, Enum):
    """How exactly a :class:`ResolvedBoundary` is placed. A boundary is
    never later than the point it stands for."""

    #: At the location's character, or just before the whitespace in
    #: front of it.
    EXACT = "exact"
    #: In the right spine item, possibly earlier than the location.
    APPROXIMATE = "approximate"
    #: At the start of the location's spine item.
    SPINE_ITEM = "spine_item"
    #: At the start of the book.
    START = "start"

    def __str__(self) -> str:
        return self.value


class BoundaryWarning(str, Enum):
    """Something worth knowing about how a boundary was placed."""

    #: The bookmark's file couldn't be found in the book.
    BOOKMARK_UNRESOLVED = "bookmark_unresolved"
    #: The bookmark is in a non-linear item (notes, for example), so a
    #: fallback was used.
    BOOKMARK_NONLINEAR = "bookmark_nonlinear"
    #: The bookmark is on a table-of-contents page, so a fallback was used.
    BOOKMARK_TOC_PAGE = "bookmark_toc_page"
    #: The bookmark's file id and its spine step name different items; the
    #: earlier one was used.
    BOOKMARK_INDEX_MISMATCH = "bookmark_index_mismatch"
    #: The book has several reading-position bookmarks; the earliest was
    #: used.
    MULTIPLE_BOOKMARKS = "multiple_bookmarks"
    #: The highlight's file couldn't be found in the book.
    HIGHLIGHT_UNRESOLVED = "highlight_unresolved"
    #: The library database lacks a column used to place the boundary, so
    #: a source was skipped or chosen in a simpler way.
    SCHEMA_MISSING_COLUMNS = "schema_missing_columns"
    #: The annotation store is missing, so no bookmark or highlight was
    #: used.
    ANNOTATIONS_UNAVAILABLE = "annotations_unavailable"

    def __str__(self) -> str:
        return self.value


# Placeholder for "not in the instance's __dict__".
_UNSET = object()


def _start_key(location: Location) -> Optional[Tuple[int, ...]]:
    """``location.sort_key``, also for a :class:`Location` unpickled from
    a version that didn't store it."""
    key = location.__dict__.get("sort_key", _UNSET)
    if key is _UNSET:
        from py_apple_books.models.location import _cfi_sort_key

        key = _cfi_sort_key(location.cfi)
    return key


def _spine_key(key: Optional[Tuple[int, ...]]) -> Optional[Tuple[int, ...]]:
    """``key`` if it is a sort key that starts with a spine step (so it
    can be compared with another such key), else None."""
    from py_apple_books.models.location import _spine_index as from_key

    return key if from_key(key) is not None else None


def _spine_index(location: Location) -> Optional[int]:
    """``location.spine_index``, also for a :class:`Location` unpickled
    from a version that didn't store it."""
    index = location.__dict__.get("spine_index", _UNSET)
    if index is _UNSET:
        from py_apple_books.models.location import _spine_index as from_key

        index = from_key(_start_key(location))
    return index


def _warnings(value: Any) -> Tuple[BoundaryWarning, ...]:
    """``value`` as a tuple; a single string (one warning without its
    tuple) is refused rather than split into characters."""
    if isinstance(value, tuple):
        return value
    if isinstance(value, (str, bytes)):
        raise InvalidArgumentError("warnings must be a tuple of BoundaryWarning values.")
    return tuple(value)


@dataclass(frozen=True)
class ReadBoundary:
    """How far a reader has read a book, from the library database alone
    (no file is read).

    :attr book_id: The book's id.
    :attr basis: ``'position'`` (where the reader is) or ``'furthest'``
        (the furthest point read).
    :attr source: What the boundary is placed by.
    :attr bookmark: The reading-position bookmark's location, or None.
    :attr highlight: The location of the newest highlight (or other
        annotation with a location), or None.
    :attr progress: The book's reading progress, in percent, or None.
    :attr high_water: The furthest point read, in percent, or None.
    :attr is_finished: Whether the book is marked finished, or None.
        Informational: it doesn't move the boundary.
    :attr warnings: What is worth knowing about how the boundary was
        placed (a tuple; another iterable given is stored as one).

    Place it in the book's text with ``BookContent.resolve_boundary``;
    :meth:`includes` decides from locations alone.
    """

    book_id: int
    basis: str
    source: BoundarySource
    bookmark: Optional[Location]
    highlight: Optional[Location]
    progress: Optional[float]
    high_water: Optional[float]
    is_finished: Optional[bool]
    warnings: Tuple[BoundaryWarning, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", _warnings(self.warnings))

    @property
    def location(self) -> Optional[Location]:
        """The location the boundary is placed by: :attr:`bookmark` for
        ``READING_POSITION`` and ``FURTHEST`` (the furthest point is at or
        after it), :attr:`highlight` for ``RECENT_HIGHLIGHT``; None for
        ``PROGRESS`` and ``NONE``."""
        if self.source in (BoundarySource.READING_POSITION, BoundarySource.FURTHEST):
            return self.bookmark
        if self.source == BoundarySource.RECENT_HIGHLIGHT:
            return self.highlight
        return None

    @property
    def spine_index(self) -> Optional[int]:
        """:attr:`location`'s spine index, or None."""
        location = self.location
        return None if location is None else _spine_index(location)

    def includes(self, location: Optional[Location]) -> bool:
        """Whether ``location`` is known to have been read, decided from
        CFIs alone (no file is read).

        * ``READING_POSITION`` and ``FURTHEST``: ``location`` starts
          before the bookmark (the bookmark's own point is not included).
        * ``RECENT_HIGHLIGHT``: ``location`` starts at or before the end
          of the highlight (the highlight itself is included).
        * ``PROGRESS`` and ``NONE``, a missing bookmark or highlight, and
          a location (or bookmark or highlight) whose CFI doesn't start
          with a spine step (``spine_index`` None; ``location`` None):
          False. Place those with ``BookContent.resolve_boundary``
          instead.

        Locations are compared in document order by their ``sort_key``
        (the spine step first); a file id that disagrees with the spine
        step is only noticed when the boundary is resolved in the book.
        Raises :class:`~py_apple_books.exceptions.InvalidArgumentError`
        for anything but a :class:`~py_apple_books.models.location.Location`
        or None (pass ``annotation.location``, not the annotation).
        """
        from py_apple_books.models.location import Location

        if location is None:
            return False
        if not isinstance(location, Location):
            raise InvalidArgumentError(
                "includes() takes a Location (such as Annotation.location) or None.")
        key = _spine_key(_start_key(location))
        if key is None:
            return False
        if self.source in (BoundarySource.READING_POSITION, BoundarySource.FURTHEST):
            if self.bookmark is None:
                return False
            limit = _spine_key(_start_key(self.bookmark))
            return limit is not None and key < limit
        if self.source == BoundarySource.RECENT_HIGHLIGHT:
            if self.highlight is None:
                return False
            limit = _spine_key(self.highlight.end_sort_key)
            return limit is not None and key <= limit
        return False


@dataclass(frozen=True)
class ResolvedBoundary:
    """A :class:`ReadBoundary` placed in a book's text: the text before
    :attr:`position` has been read.

    :attr book_id: The book's id, or None when unknown.
    :attr boundary: The boundary that was resolved.
    :attr position: Where the read text ends (exclusive).
    :attr precision: How exactly :attr:`position` is placed; never later
        than the point it stands for.
    :attr source: What :attr:`position` is placed by, after any fallback
        (it can differ from ``boundary.source``).
    :attr warnings: The boundary's warnings and those from placing it (a
        tuple; another iterable given is stored as one).
    """

    book_id: Optional[int]
    boundary: ReadBoundary
    position: TextPosition
    precision: BoundaryPrecision
    source: BoundarySource
    warnings: Tuple[BoundaryWarning, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", _warnings(self.warnings))
