from dataclasses import dataclass
from enum import Enum
from py_apple_books.models.base import Model
from py_apple_books.models.annotation import _LIVE_ANNOTATIONS, Annotation
from py_apple_books.models.relations import OneToMany
from py_apple_books.utils import _apple_datetime_or_none
import math
import os
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
# ZCONTENTTYPE of a PDF (1 is an EPUB).
CONTENT_TYPE_PDF = 3

# The 1.11 fields' conversions. Each is tolerant (a value of the wrong
# type or shape reads as None rather than failing the list the row is in)
# and idempotent (a value it already produced is kept), so a Book built
# from another one's fields keeps them; high_water_progress excepted,
# which is converted to a percent like reading_progress is.
_YEAR_TEXT = re.compile(r"[0-9]{4}")
_FLAGS = {0: False, 1: True, "0": False, "1": True}


def _text_or_none(value) -> Optional[str]:
    """A str with something other than whitespace in it, else None."""
    return value if isinstance(value, str) and value.strip() else None


def _year_or_none(value) -> Optional[int]:
    """ZYEAR (text): an int for a 4-digit year text or an int, in 1..9999."""
    if isinstance(value, str) and _YEAR_TEXT.fullmatch(value.strip()):
        value = int(value.strip())
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 9999:
        return value
    return None


def _series_id_or_none(value) -> Optional[str]:
    """ZSERIESID: a Store id as text; an int becomes its str."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _text_or_none(value)


def _int_or_none(value) -> Optional[int]:
    """An integer column: an int, an integral float or an integer text."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _finite_float_or_none(value) -> Optional[float]:
    """A finite float (a number or a number's text), else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _flag_or_none(value) -> Optional[bool]:
    """A bool from a bool, 0/1 or '0'/'1', else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        return _FLAGS.get(value)
    return None


def _percent_or_none(value) -> Optional[float]:
    """A 0..1 fraction as a percent, like ``reading_progress``: 0, NULL
    and anything that isn't a finite number give None."""
    fraction = _finite_float_or_none(value)
    return fraction * 100 if fraction else None


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

    # Added in 1.11, defaulted and last for the same reason. Each reads
    # as None where the store has no such column (store_info() lists
    # them) or the value doesn't convert. ZACCOUNTID (an Apple account
    # id) is deliberately not mapped.
    # ZLANGUAGE: the language Books records for the book, as stored
    # (e.g. 'en', 'fr_CA').
    language: Optional[str] = None
    # ZYEAR (text): the publication year; an int in 1..9999.
    year: Optional[int] = None
    # ZRELEASEDATE: the Store release date (naive local time, like the
    # other dates); for an upcoming series volume, the expected date.
    release_date: Optional[datetime] = None
    # ZSERIESID: the Store id of the series a volume belongs to.
    series_id: Optional[str] = None
    # ZSERIESCONTAINER: the id (Z_PK) of the series container row
    # (is_series_container) a volume belongs to.
    series_container_id: Optional[int] = None
    # ZSEQUENCENUMBER: the volume's number in its series (2.0, 2.5, ...).
    series_sequence: Optional[float] = None
    # ZSEQUENCEDISPLAYNAME: the volume's label in its series ('Book 2').
    series_label: Optional[str] = None
    # ZSERIESISORDERED: whether the series is read in order (set on
    # series containers).
    series_is_ordered: Optional[bool] = None
    # ZBOOKHIGHWATERMARKPROGRESS: the furthest point Books recorded, as a
    # percent like reading_progress (0 or NULL give None). Information
    # only: it may be ahead of where the reader is now.
    high_water_progress: Optional[float] = None

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
        # Tolerant (1.11): a corrupt Core Data date reads as None instead
        # of failing every list the row is in; a datetime is kept.
        self.creation_date = _apple_datetime_or_none(self.creation_date)
        self.finished_date = _apple_datetime_or_none(self.finished_date)
        self.last_opened_date = _apple_datetime_or_none(self.last_opened_date)
        self.purchased_date = _apple_datetime_or_none(self.purchased_date)
        self.last_engaged_date = _apple_datetime_or_none(self.last_engaged_date)
        self.language = _text_or_none(self.language)
        self.year = _year_or_none(self.year)
        self.release_date = _apple_datetime_or_none(self.release_date)
        self.series_id = _series_id_or_none(self.series_id)
        self.series_container_id = _int_or_none(self.series_container_id)
        self.series_sequence = _finite_float_or_none(self.series_sequence)
        self.series_label = _text_or_none(self.series_label)
        self.series_is_ordered = _flag_or_none(self.series_is_ordered)
        self.high_water_progress = _percent_or_none(self.high_water_progress)
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
    def is_pdf(self) -> bool:
        """Whether the book is a PDF: ZCONTENTTYPE 3, or a path ending in
        ``.pdf`` (any case).

        Decided from the library row alone, with no file access; unlike
        :attr:`BookContent.is_pdf <py_apple_books.content.BookContent.is_pdf>`,
        which looks at the file itself.
        """
        if self.content_type == CONTENT_TYPE_PDF:
            return True
        try:
            path = os.fspath(self.path)
        except TypeError:  # None, or not a path
            return False
        suffix = b".pdf" if isinstance(path, bytes) else ".pdf"
        return path.lower().endswith(suffix)

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
