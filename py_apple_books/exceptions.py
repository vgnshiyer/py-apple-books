"""Exceptions raised by py_apple_books.

Hierarchy (1.10; the classes marked 1.11 are new)::

    AppleBooksError
    ├── NotFoundError                      (also LookupError)
    │   ├── BookNotFoundError              (also WriteError, IndexError)
    │   ├── CollectionNotFoundError        (also WriteError, IndexError)
    │   ├── AnnotationNotFoundError        (also IndexError)
    │   └── ChapterNotFoundError
    ├── InvalidArgumentError               (also ValueError)
    │   └── InvalidChoiceError             (also KeyError)
    │       └── UnknownFieldError
    ├── BookNotDownloadedError
    │   └── NotInLibraryError
    ├── DRMProtectedError
    ├── UnsafeEpubEntryError
    ├── NotEpubError                       (1.11)
    ├── ContextUnavailableError            (1.11)
    ├── DBError
    │   ├── DBConnectionError
    │   │   ├── LibraryNotFoundError
    │   │   │   └── AnnotationStoreNotFoundError
    │   │   └── LibraryAccessDeniedError
    │   └── DBQueryError
    │       ├── UnsupportedSchemaError
    │       └── QueryTimeoutError
    └── WriteError
        ├── BooksAppRunningError
        ├── SchemaValidationError
        ├── SystemCollectionError
        ├── BackupValidationError
        ├── LibraryBusyError               (also sqlite3.OperationalError)
        └── AmbiguousStoreError            (also DBConnectionError)

The extra built-in bases keep pre-1.10 ``except`` clauses working:
1.9 and earlier signalled not-found with a bare ``IndexError``, a bad
choice with a bare ``KeyError`` and a busy library with a raw
``sqlite3.OperationalError``. The ``WriteError`` base on the book and
collection not-found errors is likewise kept. Two 1.x notes:

* The not-found classes for books, collections and annotations are
  also ``IndexError``, so ``except IndexError`` handlers written for
  1.9 still catch them.
* ``PyAppleBooks.get_book_content`` raises a bare ``IndexError`` (not
  an ``AppleBooksError``) for an unknown book id. apple-books-mcp
  0.8.2 and earlier catch ``AppleBooksError`` before ``IndexError``
  around it and would report a missing book as unreadable.

``DBError`` and its subclasses are defined here and re-exported by
:mod:`py_apple_books.db.exceptions` (the same class objects). Before
1.10 ``DBError`` derived from ``Exception`` directly.
"""

import sqlite3


class AppleBooksError(Exception):
    """Base exception for py_apple_books."""


class WriteError(AppleBooksError):
    """Base exception for write operations against the Books library."""


# -- not found ----------------------------------------------------------------


class NotFoundError(AppleBooksError, LookupError):
    """Base for "the thing you asked for doesn't exist" errors."""


class BookNotFoundError(NotFoundError, WriteError, IndexError):
    """Raised when the target book doesn't exist in the library.

    Also an :class:`IndexError` (historical read APIs signalled
    not-found via a bare ``IndexError`` from ``[0]`` indexing) and a
    :class:`WriteError` (before 1.10 only the writer raised it). Both
    extra bases are kept for 1.x.
    """


class CollectionNotFoundError(NotFoundError, WriteError, IndexError):
    """Raised when the target collection doesn't exist (or is deleted).

    Same backward-compatible bases as :class:`BookNotFoundError`.
    """


class AnnotationNotFoundError(NotFoundError, IndexError):
    """Raised when an annotation id doesn't exist.

    Also an :class:`IndexError`, which 1.9 raised bare.
    """


class ChapterNotFoundError(NotFoundError):
    """Raised when no ToC chapter or spine entry of a book matches the
    requested chapter id."""


# -- bad input ----------------------------------------------------------------


class InvalidArgumentError(AppleBooksError, ValueError):
    """A caller-supplied argument is invalid. The message says what's
    accepted."""


class InvalidChoiceError(InvalidArgumentError, KeyError):
    """A value that must be one of a fixed set isn't (a highlight
    color, an ``order_by`` field).

    Also a :class:`KeyError`, which 1.9 raised bare. ``value`` is the
    rejected value and ``valid`` the accepted ones.
    """

    # KeyError.__str__ repr-quotes its message; keep the plain text.
    __str__ = Exception.__str__

    def __init__(self, message: str, value=None, valid=()):
        super().__init__(message)
        self.value = value
        self.valid = tuple(valid or ())


class UnknownFieldError(InvalidChoiceError):
    """A model field name (in a filter, ``order_by`` or ``only``) that
    the model doesn't have. ``model`` is the model class (or its name)
    and ``field`` the unknown name; ``valid`` lists the model's fields.
    """

    def __init__(self, model, field: str, valid):
        valid = tuple(valid or ())
        if isinstance(model, str):
            name = model
        else:
            name = getattr(model, "__name__", type(model).__name__)
        message = f"{name} has no field '{field}'."
        if valid:
            message += f" Valid fields: {', '.join(map(str, valid))}."
        super().__init__(message, value=field, valid=valid)
        self.model = model
        self.field = field

    def __reduce__(self):
        # The default rebuilds from ``args`` (the message alone), which
        # doesn't match this signature.
        args = (self.model, self.field, self.valid)
        return (type(self), args, self.__dict__)


# -- content ------------------------------------------------------------------


class BookNotDownloadedError(AppleBooksError):
    """Raised when a book's file is an iCloud placeholder that hasn't been
    downloaded to local disk yet. The user needs to open the book in Apple
    Books (or otherwise trigger a download) before its content can be read.
    """


class NotInLibraryError(BookNotDownloadedError):
    """Raised when a book row is an Apple Books Store series item that
    isn't in the user's library (an unowned volume or a series
    container), so there is no book file to read.

    A :class:`BookNotDownloadedError` so existing handlers still
    report the book as unreadable.
    """


class DRMProtectedError(AppleBooksError):
    """Raised when a book is DRM-protected and its content cannot be read.
    Typically an Apple Books Store purchase (FairPlay); occasionally an
    imported EPUB with Adobe DRM or a ``META-INF/encryption.xml`` that
    encrypts more than its fonts.
    """


class UnsafeEpubEntryError(AppleBooksError):
    """Raised when a file inside an EPUB bundle resolves outside the
    bundle (absolute or ``../`` href, symlink), isn't a regular file
    (FIFO, device node), or is implausibly large. Reading it could expose
    unrelated local files or block forever, so the read is refused — a
    crafted book fails to load instead.

    ``entry`` (1.11) is the entry name as the book wrote it, in full; the
    message shortens a name over 80 characters. None when unknown.
    """

    def __init__(self, *args, entry=None):
        super().__init__(*args)
        self.entry = entry


class NotEpubError(AppleBooksError):
    """Raised by an EPUB-only :class:`~py_apple_books.content.BookContent`
    method (chapter listing and reading) when the book is not an EPUB
    bundle directory: a PDF, a zipped ``.epub`` or anything else. New in
    1.11; 1.10 raised a plain :class:`AppleBooksError` with the same
    message, which still catches it.
    """


class ContextUnavailableError(AppleBooksError):
    """Raised when the text around an annotation can't be shown for a
    reason of the annotation itself (the book is readable). ``reason``
    is one of the class constants below (the matching
    ``py_apple_books.positions.UnavailableReason`` values);
    ``annotation_id`` the annotation's id, when known.
    """

    NO_LOCATION = "no_location"
    NO_HIGHLIGHT_TEXT = "no_highlight_text"
    ORPHANED = "orphaned"
    EMPTY_CHAPTER = "empty_chapter"
    HIGHLIGHT_NOT_FOUND = "highlight_not_found"

    def __init__(self, message: str, reason: str = None, annotation_id=None):
        super().__init__(message)
        self.reason = reason
        self.annotation_id = annotation_id

    def __reduce__(self):
        message = self.args[0] if self.args else ""
        return (type(self), (message, self.reason, self.annotation_id), self.__dict__)


# -- database -----------------------------------------------------------------
# Single-base subclasses only: DBError already derives from
# AppleBooksError, and naming it again as a second base is an MRO error.


class DBError(AppleBooksError):
    """Base for SQLite-level failures on the read path."""


class DBConnectionError(DBError):
    """The library database couldn't be found or opened."""


class LibraryNotFoundError(DBConnectionError):
    """No usable Apple Books library store was found (no store file,
    or the file isn't an Apple Books store). ``path`` is the file or
    directory looked at, when known."""

    def __init__(self, *args, path=None):
        super().__init__(*args)
        self.path = path


class AnnotationStoreNotFoundError(LibraryNotFoundError):
    """The library store exists but the annotation store doesn't
    (Apple Books creates it when a book is first opened). Books and
    collections are readable; highlights and notes are not."""


class LibraryAccessDeniedError(DBConnectionError):
    """macOS refused access to the Apple Books data (privacy
    protection: the running app lacks Full Disk Access or App Data
    access). ``path`` is the file or directory refused, when known."""

    def __init__(self, *args, path=None):
        super().__init__(*args)
        self.path = path


class DBQueryError(DBError):
    """A read query failed."""


class UnsupportedSchemaError(DBQueryError):
    """The store lacks a column (or table) a query needs, e.g. on an
    older or newer macOS than the mapping was written for. ``table``
    and ``column`` name it, when known."""

    def __init__(self, *args, table=None, column=None):
        super().__init__(*args)
        self.table = table
        self.column = column


class QueryTimeoutError(DBQueryError):
    """A read query ran past its time limit and was stopped.
    ``timeout`` is the limit in seconds, when known."""

    def __init__(self, *args, timeout=None):
        super().__init__(*args)
        self.timeout = timeout


# -- writes -------------------------------------------------------------------


class BooksAppRunningError(WriteError):
    """Raised when a write is attempted while the Books app is running.
    Books caches library rows in memory and uses optimistic locking, so
    edits made underneath it can be overwritten or ignored — the app must
    be quit first.
    """


class SchemaValidationError(WriteError):
    """Raised when the library database's schema doesn't match what the
    writer knows how to maintain (e.g. after a macOS update changed the
    Core Data model). Writes abort rather than guess.
    """


class SystemCollectionError(WriteError):
    """Raised on an attempt to modify one of Apple Books' built-in
    collections (Books, PDFs, Finished, …). Only user-created collections
    can be renamed or deleted; only 'Want to Read' among the built-ins
    accepts membership edits.
    """


class BackupValidationError(WriteError):
    """Raised when a backup fails a pre-restore check. Nothing was
    changed. ``reason`` is one of the class constants below."""

    NOT_A_DATABASE = "not_a_database"
    INTEGRITY = "integrity"
    NOT_CORE_DATA = "not_core_data"
    WRONG_STORE = "wrong_store"
    MODEL_MISMATCH = "model_mismatch"
    LIVE_UNREADABLE = "live_unreadable"
    SAME_FILE = "same_file"

    def __init__(self, message: str, reason: str = None):
        super().__init__(message)
        self.reason = reason


class LibraryBusyError(WriteError, sqlite3.OperationalError):
    """The write lock couldn't be taken within the busy timeout
    (another program is writing to the library). Nothing was changed;
    retry shortly.

    Also a :class:`sqlite3.OperationalError`, which 1.9 raised raw.
    ``sqlite_errorcode`` and ``sqlite_errorname`` are copied from
    ``cause`` when given (Python 3.11+ sets them on sqlite3 errors).
    """

    def __init__(self, message: str, cause: BaseException = None):
        super().__init__(message)
        self.sqlite_errorcode = getattr(cause, "sqlite_errorcode", None)
        self.sqlite_errorname = getattr(cause, "sqlite_errorname", None)


class AmbiguousStoreError(WriteError, DBConnectionError):
    """Several candidate store files were found and a write can't tell
    which one Apple Books uses, so it refuses rather than guess."""
