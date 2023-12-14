from dataclasses import dataclass
from py_apple_books.models.highlight import Highlight
from py_apple_books.models.underline import Underline
from datetime import datetime
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
    highlights: list[Highlight]
    underlines: list[Underline]

    def __post_init__(self):
        self.creation_date = datetime.fromtimestamp(float(self.creation_date) / 1000)
        self.finished_date = datetime.fromtimestamp(float(self.finished_date) / 1000)
        self.last_opened_date = datetime.fromtimestamp(float(self.last_opened_date) / 1000)
        self.purchased_date = datetime.fromtimestamp(float(self.purchased_date) / 1000)
        self.duration = float(self.duration) / 1000
        self.reading_progress = float(self.reading_progress) * 100