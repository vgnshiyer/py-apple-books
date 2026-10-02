from dataclasses import dataclass, field
from enum import Enum, IntEnum
from datetime import datetime
import math
from typing import TYPE_CHECKING, Optional

from py_apple_books.models.base import Model
from py_apple_books.models.location import Location, PageLocation
from py_apple_books.text import is_short_selection as _is_short_selection
from py_apple_books.utils import _apple_datetime_or_none

if TYPE_CHECKING:
    from py_apple_books.models.book import Book


class AnnotationColor(Enum):
    GREEN = 1
    BLUE = 2
    YELLOW = 3
    PINK = 4
    PURPLE = 5


class AnnotationType(IntEnum):
    """``ZANNOTATIONTYPE`` values, as observed (Apple doesn't document them)."""
    TOMBSTONE = 0         # deletion marker: no asset id, text, dates or location
    BOOKMARK = 1          # user bookmark
    HIGHLIGHT = 2         # every highlight; a note is a highlight with a note body
    READING_POSITION = 3  # Books' automatic "current reading position" row


# Scope of the user-facing annotation queries (the facade's lists and
# searches, Book.annotations, and the 1.11 search index), defined once
# here so every reader of it uses the same rows. Live: not soft-deleted
# (ZANNOTATIONDELETED, NULL-safe), not a type-0 deletion tombstone and
# not the reading-position row. ``include_deleted=True`` gives the
# pre-1.10 set (everything but the reading-position row). Shared
# objects: copy before adding keys (``api._annotation_scope`` does).
_LIVE_ANNOTATIONS = {
    "type__gt": int(AnnotationType.TOMBSTONE),
    "type__ne": int(AnnotationType.READING_POSITION),
    "is_deleted__isnot": 1,
}
_ALL_ANNOTATIONS = {"type__ne": int(AnnotationType.READING_POSITION)}

# The rows that record a position rather than a passage: user bookmarks
# and the reading-position row. Only they use ZPLUSERDATA and the two
# fraction columns (a tuple: compared with ==, so any type value works).
_BOOKMARK_TYPES = (int(AnnotationType.BOOKMARK), int(AnnotationType.READING_POSITION))
# A fraction at most this far above 1 is rounding and reads as 1.0.
_FRACTION_SLACK = 1e-6


def _fraction_or_none(value) -> Optional[float]:
    """A 0..1 fraction from a fraction column (text, or a number), else
    None: not finite, below 0, or more than ``_FRACTION_SLACK`` above 1."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or number > 1 + _FRACTION_SLACK:
        return None
    return min(number, 1.0) + 0.0  # + 0.0: -0.0 reads as 0.0


@dataclass
class Annotation(Model):
    """
    Represents an annotation in the Apple Books library.
    """
    # Identifiers
    id: str
    asset_id: str

    # Status
    is_deleted: bool

    # Dates
    creation_date: datetime
    modification_date: datetime

    # Annotation details
    representative_text: str
    selected_text: str
    note: str
    is_underline: bool
    style: int
    # ZANNOTATIONTYPE disambiguates what kind of annotation this is
    # (see :class:`AnnotationType`):
    #   0 = deletion tombstone (no asset id, text, dates or location)
    #   1 = user bookmark
    #   2 = highlight, with selected text; a note is a type-2 row with
    #       a note body, and an underline is style 0 with is_underline set
    #   3 = automatic "current reading position" bookmark (zero-width,
    #       no selected text, one per book, updated as the user reads)
    type: int

    # Location
    # ``chapter`` is Apple's ZFUTUREPROOFING5 field. It's sparsely
    # populated (mostly NULL) — use ``self.chapter(content)`` to resolve
    # a real chapter title from the CFI instead.
    chapter: str
    # ``location`` holds the annotation's EPUB CFI as a
    # :class:`Location` value object, so callers can call
    # ``self.location.chapter(content)`` /
    # ``self.location.surrounding_text(content)`` without knowing CFI
    # syntax. Populated from the DB as a string and upgraded to a
    # :class:`Location` in :meth:`__post_init__`.
    location: Optional[Location]

    # Color
    color: str = None

    # Added in 1.10. Defaulted and last, so code that builds an
    # Annotation directly keeps working.
    # ZANNOTATIONUUID: stable across devices and re-syncs.
    uuid: Optional[str] = None
    # ZPLLOCATIONRANGESTART: the spine index of the CFI's chapter for
    # highlights and bookmarks (equal to ``location.spine_index``), so
    # ``order_by='position'`` sorts by book order. Meaningless on
    # reading-position rows (type 3).
    position: Optional[int] = None

    # Added in 1.11, defaulted and last for the same reason. Read on
    # bookmark rows (types 1 and 3) only: the fractions are None on any
    # other row, and on a row read with ``only=`` fields that leave out
    # ``type``. None where the store has no such column.
    # ZPLUSERDATA: Books' own record of the row's position, kept as the
    # raw bytes (a small property list; see page_location). Left out of
    # repr.
    location_data: Optional[bytes] = field(default=None, repr=False)
    # ZFUTUREPROOFING10: where the reader is, as a 0..1 fraction of the
    # book (for a PDF, exactly (page_offset + 1) / pages).
    position_fraction: Optional[float] = None
    # ZFUTUREPROOFING8: the furthest point read, as a 0..1 fraction.
    # Books stores both fractions as text, so a filter or order_by on
    # them compares text in SQL ('0.10' < '0.9'); compare the floats in
    # Python instead.
    furthest_fraction: Optional[float] = None

    if TYPE_CHECKING:
        # For type checkers only: ModelBase installs the relation (the
        # reverse of Book.annotations) when Book is defined. A class-level
        # annotation would make it a dataclass field.
        @property
        def book(self) -> Optional[Book]:
            """The annotated book, or None if it isn't in the library."""

    def __post_init__(self):
        """
        Converts the creation_date and modification_date from timestamp to datetime,
        and wraps the raw CFI string in a Location.
        """
        # Tolerant (1.11): a corrupt date reads as None; a datetime is kept.
        self.creation_date = _apple_datetime_or_none(self.creation_date)
        self.modification_date = _apple_datetime_or_none(self.modification_date)

        if self.style in AnnotationColor._value2member_map_:
            self.color = AnnotationColor(self.style).name

        # DB yields location as a str; wrap into the value object. Guard
        # against double-wrapping when tests construct Annotation
        # directly with a Location.
        if isinstance(self.location, str):
            self.location = Location(self.location) if self.location else None

        if self.type in _BOOKMARK_TYPES:
            self.position_fraction = _fraction_or_none(self.position_fraction)
            self.furthest_fraction = _fraction_or_none(self.furthest_fraction)
        else:
            self.position_fraction = self.furthest_fraction = None

    @property
    def page_location(self) -> Optional[PageLocation]:
        """The page or spine position Books records in :attr:`location_data`
        on a bookmark row (types 1 and 3): for a PDF the page, for an EPUB
        bookmark without a CFI the spine item (``ordinal``). None on other
        rows and when the data doesn't decode
        (:meth:`PageLocation.from_plist`).

        Decoded on first access and kept for as long as
        :attr:`location_data` is the same object.
        """
        data = self.location_data
        if data is None or self.type not in _BOOKMARK_TYPES:
            return None
        cached = self.__dict__.get("_ab_page_location")
        if cached is not None and cached[0] is data:
            return cached[1]
        result = PageLocation.from_plist(data)
        # An _ab_ key: left out of pickles and copies (Model.__getstate__).
        self.__dict__["_ab_page_location"] = (data, result)
        return result

    @property
    def is_short_selection(self) -> bool:
        """Whether the highlight is a word or a short phrase (likely looked
        up or collected) rather than a passage: see
        :func:`py_apple_books.text.is_short_selection`. False for
        bookmarks and the reading-position row."""
        if self.type in _BOOKMARK_TYPES:
            return False
        return _is_short_selection(self.selected_text)

    @property
    def deep_link(self) -> Optional[str]:
        """``ibooks://assetid/<asset_id>#<cfi>``, or the book's link
        (:attr:`Book.deep_link`) when there's no location. None without an
        asset id, e.g. on tombstones.

        Built from this row alone; the book isn't looked up, so orphans
        get a link too. The CFI is appended as-is, without percent-encoding.
        The ``#<cfi>`` fragment follows the Obsidian Apple Books plugin;
        whether Books.app jumps to it is unverified.
        """
        if not self.asset_id:
            return None
        link = f"ibooks://assetid/{self.asset_id}"
        return f"{link}#{self.location.cfi}" if self.location else link

    def __str__(self):
        return f"ID: {self.id}\nRepresentative text: {self.representative_text}\nSelected text: {self.selected_text}\nNote: {self.note}"
