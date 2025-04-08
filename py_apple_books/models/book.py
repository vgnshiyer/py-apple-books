from dataclasses import dataclass
from py_apple_books.models.base import Model
from py_apple_books.models.annotation import Annotation
from py_apple_books.models.relations import OneToMany
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
    annotations = OneToMany(
        related_model=Annotation,
        related_name='book',
        foreign_key='asset_id'
    )

    def __post_init__(self):
        self.creation_date = datetime.fromtimestamp(float(self.creation_date) / 1000) if self.creation_date else None
        self.finished_date = datetime.fromtimestamp(float(self.finished_date) / 1000) if self.finished_date else None
        self.last_opened_date = datetime.fromtimestamp(float(self.last_opened_date) / 1000) \
            if self.last_opened_date else None
        self.purchased_date = datetime.fromtimestamp(float(self.purchased_date) / 1000) if self.purchased_date else None
        self.duration = float(self.duration) / 1000 if self.duration else None
        self.reading_progress = float(self.reading_progress) * 100 if self.reading_progress else None

    def __str__(self):
        return f"ID: {self.id}\nTitle: {self.title}\nAuthor: {self.author}\nDescription: {self.description}"
