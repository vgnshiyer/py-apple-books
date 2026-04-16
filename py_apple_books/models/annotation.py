from dataclasses import dataclass
from py_apple_books.models.base import Model
from py_apple_books.utils import apple_timestamp_to_datetime
from datetime import datetime
from enum import Enum


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

    # Location
    chapter: str
    location: str

    # Color
    color: str = None

    def __post_init__(self):
        """
        Converts the creation_date and modification_date from timestamp to datetime.
        """
        self.creation_date = apple_timestamp_to_datetime(self.creation_date)
        self.modification_date = apple_timestamp_to_datetime(self.modification_date)

        if self.style in AnnotationColor._value2member_map_:
            self.color = AnnotationColor(self.style).name

    def __str__(self):
        return f"ID: {self.id}\nRepresentative text: {self.representative_text}\nSelected text: {self.selected_text}\nNote: {self.note}"
