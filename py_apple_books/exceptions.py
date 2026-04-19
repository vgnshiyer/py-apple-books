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
