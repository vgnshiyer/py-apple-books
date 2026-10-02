"""A book's descriptive metadata: what the library records, completed
from the book's own package document (OPF). See
:meth:`py_apple_books.PyAppleBooks.get_book_metadata`.

New in 1.11. Import the types from :mod:`py_apple_books.models`.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Optional, Tuple


class MetadataFileState(str, Enum):
    """Whether the book's own file was read for a :class:`BookMetadata`.

    The values that name the same condition as
    :class:`py_apple_books.positions.UnavailableReason` use the same
    string (``not_epub``, ``not_downloaded``, ``unreadable``).
    """

    #: The package document was read (fields can still be None).
    READ = "read"
    #: ``read_files=False``: only the library's values.
    NOT_REQUESTED = "not_requested"
    #: The library records no file for the book.
    NO_FILE = "no_file"
    #: Not an unzipped EPUB bundle (a PDF, a zipped ``.epub`` file, ...).
    NOT_EPUB = "not_epub"
    #: Stored only in iCloud, or partly evicted: nothing was read (and
    #: nothing downloaded). Open the book in Apple Books to download it.
    NOT_DOWNLOADED = "not_downloaded"
    #: The bundle or its package document is missing, unsafe (a symlink,
    #: a path leaving the bundle), too large or malformed, or couldn't
    #: be read.
    UNREADABLE = "unreadable"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class BookMetadata:
    """A book's metadata, library values first, completed from the book's
    package document (OPF). Frozen, hashable and picklable.

    Every text value comes from the library or the book file, so treat
    it as untrusted text when showing it to a model or a user.
    ``book_file_fields`` names the fields whose value came from the book
    file.
    """

    book_id: Optional[int]
    #: BCP 47 language tag, normalised (``'en-US'``; ``'und'`` reads as None).
    language: Optional[str] = None
    publisher: Optional[str] = None
    #: Publication date as ``'YYYY'``, ``'YYYY-MM'`` or ``'YYYY-MM-DD'``.
    published: Optional[str] = None
    year: Optional[int] = None
    #: ISBN, checksum-verified, without hyphens; an ISBN-13 when the book
    #: has one, else an ISBN-10 (which may end in ``X``).
    isbn: Optional[str] = None
    #: The library's genre first, then the book's subjects, without
    #: duplicates (compared like searches compare text); at most 30.
    subjects: Tuple[str, ...] = ()
    #: Plain text, at most 16,000 characters.
    description: Optional[str] = None
    #: The cover image's path inside the bundle, as the package document
    #: names it. Checked lexically only: never opened.
    cover_href: Optional[str] = None
    #: Series named in the package document (calibre's ``series`` or an
    #: EPUB 3 ``belongs-to-collection``); not the Apple Books Store series
    #: (see :meth:`~py_apple_books.PyAppleBooks.get_series`).
    series_title: Optional[str] = None
    series_sequence: Optional[float] = None
    file_state: MetadataFileState = MetadataFileState.NOT_REQUESTED
    #: Names of the fields whose value came from the book file.
    book_file_fields: FrozenSet[str] = field(default_factory=frozenset)
