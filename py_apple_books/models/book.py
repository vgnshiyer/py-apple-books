from py_apple_books.models.annotation import Annotation
from py_apple_books.models.highlight import Highlight
from py_apple_books.models.underline import Underline

from dataclasses import dataclass, field
from datetime import datetime
from typing import List
import pathlib


@dataclass
class Book:
    # Identifiers
    id: str
    asset_id: str

    # Basic book information
    title: str
    author: str
    description: str
    genre: str
    content_type: str
    page_count: int

    # File information
    path: pathlib.Path
    filesize: int

    # Reading progress
    is_finished: bool
    reading_progress: float
    duration: float

    # Dates
    creation_date: datetime
    finished_date: datetime
    last_opened_date: datetime
    purchased_date: datetime

    # Flags
    is_explicit: bool
    is_locked: bool
    is_ephemeral: bool
    is_hidden: bool
    is_sample: bool
    is_store_audiobook: bool

    # User interactions
    rating: int
    highlights: List[Highlight] = field(default_factory=list)
    underlines: List[Underline] = field(default_factory=list)
    other_annotations: List[Annotation] = field(default_factory=list)

    def __post_init__(self):
        """
        Converts the creation_date, finished_date, last_opened_date, and purchased_date from timestamp to datetime.
        """
        self.creation_date = datetime.fromtimestamp(float(self.creation_date) / 1000) if self.creation_date else None
        self.finished_date = datetime.fromtimestamp(float(self.finished_date) / 1000) if self.finished_date else None
        self.last_opened_date = datetime.fromtimestamp(float(self.last_opened_date) / 1000) if self.last_opened_date else None
        self.purchased_date = datetime.fromtimestamp(float(self.purchased_date) / 1000) if self.purchased_date else None
        self.duration = float(self.duration) / 1000 if self.duration else None
        self.reading_progress = float(self.reading_progress) * 100 if self.reading_progress else None

    def __hash__(self) -> int:
        """
        Returns a hash value for the Book object.

        Returns:
            int: hash value for the Book object
        """
        return hash(self.id)

    def add_annotation(self, annotation: Annotation) -> None:
        """
        Adds an annotation to the book.

        Args:
            annotation (Annotation): annotation to add
        """
        if isinstance(annotation, Highlight):
            self.highlights.append(annotation)
        elif isinstance(annotation, Underline):
            self.underlines.append(annotation)
        else:
            self.other_annotations.append(annotation)