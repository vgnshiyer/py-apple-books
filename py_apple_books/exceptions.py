"""Exceptions raised by py_apple_books."""


class AppleBooksError(Exception):
    """Base exception for py_apple_books."""


class BookNotDownloadedError(AppleBooksError):
    """Raised when a book's file is an iCloud placeholder that hasn't been
    downloaded to local disk yet. The user needs to open the book in Apple
    Books (or otherwise trigger a download) before its content can be read.
    """


class DRMProtectedError(AppleBooksError):
    """Raised when a book is DRM-protected and its content cannot be read.
    Typically an Apple Books Store purchase; occasionally an imported EPUB
    with ``META-INF/encryption.xml``.
    """


class WriteError(AppleBooksError):
    """Base exception for write operations against the Books library."""


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


class CollectionNotFoundError(WriteError, IndexError):
    """Raised when the target collection doesn't exist (or is deleted).

    Also subclasses :class:`IndexError` because historical read APIs
    signaled not-found via bare ``IndexError`` from ``[0]`` indexing —
    existing ``except IndexError`` handlers keep working.
    """


class BookNotFoundError(WriteError, IndexError):
    """Raised when the target book doesn't exist in the library.

    Subclasses :class:`IndexError` for the same backward-compatibility
    reason as :class:`CollectionNotFoundError`.
    """
