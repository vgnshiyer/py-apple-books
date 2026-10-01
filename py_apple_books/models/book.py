from dataclasses import dataclass
from enum import Enum
from py_apple_books.models.base import Model
from py_apple_books.models.annotation import _LIVE_ANNOTATIONS, Annotation
from py_apple_books.models.relations import OneToMany
from py_apple_books.utils import apple_timestamp_to_datetime
import pathlib
import re
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from py_apple_books.models.collection import Collection
    from py_apple_books.models.manager import ModelIterable

# Apple Books stores a missing author as a Private Use Area glyph plus a
# localization key (U+E83A + 'UnknownAuthor'), which Books.app renders as
# "Unknown Author". Matched narrowly so a real name that merely starts
# with a PUA glyph is left alone.
_UNKNOWN_AUTHOR_PLACEHOLDER = re.compile(r"[\uE000-\uF8FF][A-Za-z]+")

# ZDATASOURCEIDENTIFIER of the rows Apple Books adds for Store series it
# knows about: series containers and the volumes of a series. Owned
# books normally come from other data sources (ubiquity, purchases, ...);
# see Book.is_store_series_item for Series rows that are owned.
SERIES_DATA_SOURCE = "com.apple.ibooks.BKLibraryDataSourceSeries"
# ZCONTENTTYPE of a Store series container (the "stack", not a volume).
CONTENT_TYPE_SERIES_CONTAINER = 5
# ZSTATE Books.app records for an asset that is in iCloud only.
STATE_CLOUD_ONLY = 3


class ReadingStatus(str, Enum):
    """Where a book stands; see :attr:`Book.reading_status`."""
    FINISHED = "finished"
    IN_PROGRESS = "in_progress"
    UNSTARTED = "unstarted"

    def __str__(self) -> str:
        return self.value


@dataclass
class Book(Model):
    """
    Represents a book in the Apple Books library.
    """
    id: int
    asset_id: str

    # Basic book information
    title: str
    # None when Apple Books has no author (see _UNKNOWN_AUTHOR_PLACEHOLDER).
    author: Optional[str]
    description: str
    genre: str
    content_type: str
    # None when unknown: Apple leaves ZPAGECOUNT at a placeholder 0 or 1
    # for most imported books; only counts above 1 are real.
    page_count: Optional[int]

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

    # Added in 1.10. Defaulted and last, so code that builds a Book
    # positionally keeps working. ZPURCHASEDDSID (an Apple account id)
    # is deliberately not mapped.
    # ZSTOREID: the Apple Books Store id; set on Store rows only.
    store_id: Optional[str] = None
    # ZDATASOURCEIDENTIFIER: where Books got the row (see SERIES_DATA_SOURCE).
    data_source: Optional[str] = None
    # ZCANREDOWNLOAD: 1 on every owned row observed; 0 on unowned series rows.
    can_redownload: Optional[int] = None
    # ZSTATE: Books' cached download state (1 local, 3 iCloud only, ...).
    state: Optional[int] = None
    # ZLASTENGAGEDDATE: advances with reading activity, unlike
    # ZLASTOPENDATE; NULL on many books. See last_read_date.
    last_engaged_date: Optional[datetime] = None

    # Relations
    #
    # ``book.annotations`` returns only live user-created annotations
    # (highlights, notes and bookmarks). Apple Books stores its
    # auto-tracked reading-position bookmark as an annotation with
    # ``type = 3``, and keeps annotations deleted in Books
    # (``is_deleted``) and type-0 tombstones for iCloud sync; we filter
    # those out here so callers asking "what did the user annotate?" get
    # the expected set. The same filter as the annotation queries in
    # :class:`PyAppleBooks`: both read ``annotation._LIVE_ANNOTATIONS``
    # (the relation keeps its own dict of it). For direct access to the
    # bookmark, use :meth:`PyAppleBooks.get_current_reading_location`.
    annotations = OneToMany(
        related_model=Annotation,
        related_name='book',
        foreign_key='asset_id',
        extra_filters=_LIVE_ANNOTATIONS,
    )

    if TYPE_CHECKING:
        # For type checkers only: ModelBase installs the relation (the
        # reverse of Collection.books) when Collection is defined. A
        # class-level annotation would make it a dataclass field.
        @property
        def collections(self) -> ModelIterable[Collection]:
            """The collections the book is in, deleted ones included."""

    def __post_init__(self):
        self.creation_date = apple_timestamp_to_datetime(self.creation_date)
        self.finished_date = apple_timestamp_to_datetime(self.finished_date)
        self.last_opened_date = apple_timestamp_to_datetime(self.last_opened_date)
        self.purchased_date = apple_timestamp_to_datetime(self.purchased_date)
        if not isinstance(self.last_engaged_date, datetime):
            self.last_engaged_date = apple_timestamp_to_datetime(self.last_engaged_date)
        self.duration = float(self.duration) / 1000 if self.duration else None
        self.reading_progress = float(self.reading_progress) * 100 if self.reading_progress else None
        if self.author and _UNKNOWN_AUTHOR_PLACEHOLDER.fullmatch(self.author):
            self.author = None
        self.page_count = self.page_count if self.page_count and self.page_count > 1 else None

    def __str__(self):
        return f"ID: {self.id}\nTitle: {self.title}\nAuthor: {self.author or 'Unknown Author'}\nDescription: {self.description}"

    @property
    def last_read_date(self) -> Optional[datetime]:
        """The later of :attr:`last_opened_date` and :attr:`last_engaged_date`,
        ignoring None. ZLASTOPENDATE alone goes stale while a book stays
        open; ZLASTENGAGEDDATE alone is NULL on many books."""
        dates = [d for d in (self.last_opened_date, self.last_engaged_date) if d is not None]
        return max(dates) if dates else None

    @property
    def reading_status(self) -> ReadingStatus:
        """Finished if Books marked it finished (whatever the progress),
        else in progress if any progress is recorded, else unstarted."""
        if self.is_finished == 1:
            return ReadingStatus.FINISHED
        if (self.reading_progress or 0) > 0:
            return ReadingStatus.IN_PROGRESS
        return ReadingStatus.UNSTARTED

    @property
    def is_series_container(self) -> bool:
        """A Store series container row (ZCONTENTTYPE 5), not a book."""
        return self.content_type == CONTENT_TYPE_SERIES_CONTAINER

    @property
    def is_store_series_item(self) -> bool:
        """An Apple Books Store series entry you don't own (series
        container or unowned volume); Series-source rows with
        can_redownload=1 are treated as owned. A NULL can_redownload
        counts as unowned."""
        return self.is_series_container or (
            self.data_source == SERIES_DATA_SOURCE and self.can_redownload != 1
        )

    @property
    def is_cloud_only(self) -> bool:
        """Whether Books records the book as in iCloud only (ZSTATE 3).

        This is Apple's cached view and can be stale; the file system is
        authoritative (see :func:`py_apple_books.content.is_downloaded`).
        """
        return self.state == STATE_CLOUD_ONLY

    @property
    def deep_link(self) -> Optional[str]:
        """``ibooks://assetid/<asset_id>``, which opens the book in Books.app;
        None without an asset id."""
        return f"ibooks://assetid/{self.asset_id}" if self.asset_id else None

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
        last_read_date = self.last_read_date
        last_read = "Never" if last_read_date is None else last_read_date.strftime("%Y-%m-%d")
        
        summary = f"Progress: {status}"
        if self.reading_progress and self.reading_progress > 0:
            summary += f" | Last Read: {last_read}"
        if self.duration:
            hours = self.duration / 3600  # Convert seconds to hours
            summary += f" | Time Spent: {hours:.1f}h"
        
        return summary
