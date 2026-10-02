from dataclasses import dataclass
from enum import Enum, IntEnum
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from py_apple_books.models.base import Model
from py_apple_books.models.location import Location
from py_apple_books.utils import apple_timestamp_to_datetime

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
        self.creation_date = apple_timestamp_to_datetime(self.creation_date)
        self.modification_date = apple_timestamp_to_datetime(self.modification_date)

        if self.style in AnnotationColor._value2member_map_:
            self.color = AnnotationColor(self.style).name

        # DB yields location as a str; wrap into the value object. Guard
        # against double-wrapping when tests construct Annotation
        # directly with a Location.
        if isinstance(self.location, str):
            self.location = Location(self.location) if self.location else None

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
