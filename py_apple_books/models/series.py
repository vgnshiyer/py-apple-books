"""Apple Books Store series, as the library records them. See
:meth:`py_apple_books.PyAppleBooks.get_series` and
:meth:`~py_apple_books.PyAppleBooks.list_series`.

New in 1.11, **provisional**: the shape may change in 1.12, once a
library with bought Store series books has been checked. Import the
types from :mod:`py_apple_books.models`.
"""

import operator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Tuple

from py_apple_books.models.book import ReadingStatus

if TYPE_CHECKING:
    from py_apple_books.models.book import Book


def _row_id(book_id) -> Optional[int]:
    """An int row id from an int, a string of ASCII digits or a
    :class:`Book`; None for anything else (a bool, or other Unicode
    digits such as '²' or '٣', which no library id is written with)."""
    book_id = getattr(book_id, "id", book_id)
    if isinstance(book_id, bool):
        return None
    try:
        return operator.index(book_id)
    except TypeError:
        pass
    if isinstance(book_id, str):
        text = book_id.strip()
        if text.isascii() and text.isdigit():
            return int(text)
    return None


@dataclass(frozen=True)
class SeriesVolume:
    """One volume of a :class:`Series` (provisional).

    Books lists a Store series volume as its own row (Series data
    source); a copy of it in your library may be a second row with the
    same Store id. ``ids`` holds every row that stands for the volume.
    Not hashable: it holds a :class:`Book`, which isn't.
    """

    #: The row in your library (the lowest id) if there is one, else the
    #: Store series row.
    book: "Book"
    #: Every row id that stands for this volume, ascending.
    ids: Tuple[int, ...]
    #: The volume's number in the series (2.0, 2.5, ...), when known.
    sequence: Optional[float]
    #: How Books labels it ("Book 2"), when known.
    label: Optional[str]
    #: Whether :meth:`~py_apple_books.PyAppleBooks.list_books` lists ``book``.
    in_library: bool

    __hash__ = None  # type: ignore[assignment]  # holds Book models

    @property
    def reading_status(self) -> ReadingStatus:
        """``book.reading_status``: a volume you opened but don't own shows
        as in progress."""
        return self.book.reading_status


def _sequence_key(volume: SeriesVolume):
    """Order of volumes: by sequence (unknown last), then book id."""
    return (volume.sequence is None, volume.sequence or 0.0, volume.book.id)


@dataclass(frozen=True)
class Series:
    """A Store series Books knows about, with the volumes it records
    (provisional).

    ``volumes`` are the *known* volumes (rows Books keeps), in sequence
    order (unknown last), then by book id: never the length of the
    series. Not hashable: it holds :class:`Book` models.
    """

    #: The series container's title; None when no container row exists.
    title: Optional[str]
    #: The series' Store id.
    series_id: Optional[str]
    #: The series container row (``Book.is_series_container``), if any.
    container: Optional["Book"]
    #: Whether the series is read in order (from the container).
    is_ordered: Optional[bool]
    volumes: Tuple[SeriesVolume, ...]

    __hash__ = None  # type: ignore[assignment]  # holds Book models

    def volume_for(self, book_id) -> Optional[SeriesVolume]:
        """The volume a row id (or a :class:`Book`) stands for: the Store
        series row's id or the id of a copy in your library."""
        row = _row_id(book_id)
        if row is None:
            return None
        return next((v for v in self.volumes if row in v.ids), None)

    def next_after(self, book_id) -> Optional[SeriesVolume]:
        """The first known volume with a higher sequence than the one
        ``book_id`` stands for. None when the series isn't read in order
        (``is_ordered`` False), the volume has no sequence or isn't in
        this series, or it is the last known volume."""
        if self.is_ordered is False:
            return None
        volume = self.volume_for(book_id)
        if volume is None or volume.sequence is None:
            return None
        later = [v for v in self.volumes if v.sequence is not None and v.sequence > volume.sequence]
        return min(later, key=_sequence_key) if later else None

    @property
    def current(self) -> Optional[SeriesVolume]:
        """The volume in progress with the highest sequence (unknown
        lowest); on a tie, the one read most recently. None when no
        volume is in progress."""
        reading = [v for v in self.volumes if v.reading_status == ReadingStatus.IN_PROGRESS]
        if not reading:
            return None
        return max(reading, key=lambda v: (
            v.sequence is not None, v.sequence or 0.0,
            v.book.last_read_date is not None, v.book.last_read_date or 0, -v.book.id))

    @property
    def up_next(self) -> Optional[SeriesVolume]:
        """The volume after the highest-sequence volume you have started
        or finished. None when none is started, when the series isn't
        read in order, or when that volume is the last known one."""
        if self.is_ordered is False:
            return None
        started = [v for v in self.volumes
                   if v.sequence is not None and v.reading_status != ReadingStatus.UNSTARTED]
        if not started:
            return None
        furthest = max(started, key=lambda v: (v.sequence, -v.book.id))
        return self.next_after(furthest.ids[0])
