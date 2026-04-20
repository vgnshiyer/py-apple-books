from dataclasses import dataclass
from enum import Enum
from datetime import datetime
from typing import Optional

from py_apple_books.models.base import Model
from py_apple_books.models.location import Location
from py_apple_books.utils import apple_timestamp_to_datetime


class AnnotationColor(Enum):
    GREEN = 1
    BLUE = 2
    YELLOW = 3
    PINK = 4
    PURPLE = 5


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
    # ZANNOTATIONTYPE disambiguates what kind of annotation this is:
    #   1 = highlight (with selected text)
    #   2 = user note (text + note body)
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

    def __str__(self):
        return f"ID: {self.id}\nRepresentative text: {self.representative_text}\nSelected text: {self.selected_text}\nNote: {self.note}"
