from dataclasses import dataclass
from py_apple_books.models.base import Model
from py_apple_books.models.annotation import Annotation
from py_apple_books.models.relations import OneToMany
from py_apple_books.utils import apple_timestamp_to_datetime
import pathlib
from datetime import datetime


@dataclass
class Book(Model):
    """
    Represents a book in the Apple Books library.
    """
    id: int
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

    # Relations
    #
    # ``book.annotations`` returns only user-created annotations (highlights
    # and notes). Apple Books stores its auto-tracked reading-position
    # bookmark as an annotation with ``type = 3``; we filter that out here
    # so callers asking "what did the user annotate?" get the expected set.
    # For direct access to the bookmark, use
    # :meth:`PyAppleBooks.get_current_reading_location`.
    annotations = OneToMany(
        related_model=Annotation,
        related_name='book',
        foreign_key='asset_id',
        extra_filters={'type__ne': 3},
    )

    def __post_init__(self):
        self.creation_date = apple_timestamp_to_datetime(self.creation_date)
        self.finished_date = apple_timestamp_to_datetime(self.finished_date)
        self.last_opened_date = apple_timestamp_to_datetime(self.last_opened_date)
        self.purchased_date = apple_timestamp_to_datetime(self.purchased_date)
        self.duration = float(self.duration) / 1000 if self.duration else None
        self.reading_progress = float(self.reading_progress) * 100 if self.reading_progress else None

    def __str__(self):
        return f"ID: {self.id}\nTitle: {self.title}\nAuthor: {self.author}\nDescription: {self.description}"

    @property
    def progress_status(self) -> str:
        """Get a human-readable reading progress status."""
        if self.is_finished:
            return "Finished"
        elif self.reading_progress is None or self.reading_progress == 0:
            return "Not Started"
        elif self.reading_progress >= 100:
            return "Completed"
        else:
            return f"In Progress ({self.reading_progress:.1f}%)"

    def format_progress_summary(self) -> str:
        """Get a formatted summary of reading progress."""
        status = self.progress_status
        last_read = "Never" if self.last_opened_date is None else self.last_opened_date.strftime("%Y-%m-%d")
        
        summary = f"Progress: {status}"
        if self.reading_progress and self.reading_progress > 0:
            summary += f" | Last Read: {last_read}"
        if self.duration:
            hours = self.duration / 3600  # Convert seconds to hours
            summary += f" | Time Spent: {hours:.1f}h"
        
        return summary
