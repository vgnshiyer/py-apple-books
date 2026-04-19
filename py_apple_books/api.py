import pathlib
from datetime import datetime
from typing import Optional
from py_apple_books.content import BookContent, Chapter
from py_apple_books.exceptions import BookNotDownloadedError, DRMProtectedError
from py_apple_books.models import Book, Collection, Annotation, AnnotationColor
from py_apple_books.models.manager import ModelIterable
from py_apple_books.utils import APPLE_EPOCH_OFFSET


# Apple Books' ``ZANNOTATIONTYPE`` value for the automatic "current reading
# position" bookmark. Distinct from highlights (1) and notes (2); one per
# book, updated as the user reads, with empty selected_text/note and a
# zero-width CFI range.
_ANNOTATION_TYPE_READING_BOOKMARK = 3


class PyAppleBooks:
    """Facade class for accessing Apple Books data."""

    # -- collection actions --
    def list_collections(self, limit: int = None, order_by: str = None) -> ModelIterable:
        """List all collections."""
        return Collection.manager.all(limit=limit, order_by=order_by)

    def get_collection_by_id(self, collection_id: str) -> Collection:
        """Get a collection and its books."""
        return Collection.manager.filter(id=collection_id)[0]

    def get_collection_by_title(self, title: str) -> ModelIterable:
        """Get a collection and its books."""
        return Collection.manager.filter(title__contains=title)

    # -- book actions --
    def list_books(self, limit: int = None, order_by: str = None) -> ModelIterable:
        """List all books."""
        return Book.manager.all(limit=limit, order_by=order_by)

    def get_book_by_id(self, book_id: str) -> Book:
        """Get a book and its annotations."""
        return Book.manager.filter(id=book_id)[0]

    def get_book_by_title(self, title: str) -> ModelIterable:
        """Get a book by title."""
        return Book.manager.filter(title__contains=title)

    def get_books_by_genre(self, genre: str, limit: int = None, order_by: str = None) -> ModelIterable:
        """Get books whose genre contains the given string (case-sensitive)."""
        return Book.manager.filter(genre__contains=genre, limit=limit, order_by=order_by)

    # -- annotation actions --
    def list_annotations(self, limit: int = None, order_by: str = None) -> ModelIterable:
        """List all annotations."""
        return Annotation.manager.all(limit=limit, order_by=order_by)

    def get_annotation_by_id(self, annotation_id: str) -> Annotation:
        """Get an annotation by id."""
        return Annotation.manager.filter(id=annotation_id)[0]

    def get_annotations_by_color(self, color: str, limit: int = None, order_by: str = None) -> ModelIterable:
        """Get annotations by color."""
        style = AnnotationColor[color.upper()].value
        return Annotation.manager.filter(style=style, limit=limit, order_by=order_by)

    def search_annotation_by_highlighted_text(self, text: str,
                                              limit: int = None, order_by: str = None) -> ModelIterable:
        """Search for annotations by highlighted text."""
        return Annotation.manager.filter(selected_text__contains=text, limit=limit, order_by=order_by)

    def search_annotation_by_note(self, note: str, limit: int = None, order_by: str = None) -> ModelIterable:
        """Search for annotations by note."""
        return Annotation.manager.filter(note__contains=note, limit=limit, order_by=order_by)

    def search_annotation_by_text(self, text: str, limit: int = None, order_by: str = None) -> ModelIterable:
        """Search for annotations by any text that contains the given text."""
        return Annotation.manager.filter(selected_text__contains=text,
                                         representative_text__contains=text,
                                         note__contains=text,
                                         use_or=True,
                                         limit=limit,
                                         order_by=order_by)

    def get_annotations_by_date_range(self, after: datetime = None, before: datetime = None,
                                       limit: int = None, order_by: str = None) -> ModelIterable:
        """Get annotations within a date range.

        Args:
            after: Only include annotations created after this datetime.
            before: Only include annotations created before this datetime.
            limit: Maximum number of results.
            order_by: Field to sort by (prefix with - for descending).
        """
        kwargs = {}
        if after:
            kwargs["creation_date__gte"] = after.timestamp() - APPLE_EPOCH_OFFSET
        if before:
            kwargs["creation_date__lte"] = before.timestamp() - APPLE_EPOCH_OFFSET
        if limit:
            kwargs["limit"] = limit
        if order_by:
            kwargs["order_by"] = order_by
        return Annotation.manager.filter(**kwargs) if kwargs else Annotation.manager.all()

    # -- reading progress actions --
    def get_books_in_progress(self, limit: int = None, order_by: str = None) -> ModelIterable:
        """Get books that are currently being read (progress > 0% and < 100%)."""
        return Book.manager.filter(reading_progress__gt=0, 
                                  reading_progress__lt=100, 
                                  limit=limit, 
                                  order_by=order_by)

    def get_finished_books(self, limit: int = None, order_by: str = None) -> ModelIterable:
        """Get books that are marked as finished."""
        return Book.manager.filter(is_finished=True, limit=limit, order_by=order_by)

    def get_unstarted_books(self, limit: int = None, order_by: str = None) -> ModelIterable:
        """Get books that haven't been started (progress = 0% or None)."""
        return Book.manager.filter(reading_progress__lte=0, limit=limit, order_by=order_by)

    def get_recently_read_books(self, limit: int = 10, order_by: str = "-last_opened_date") -> ModelIterable:
        """Get recently opened books, ordered by last opened date."""
        return Book.manager.filter(last_opened_date__isnull=False, limit=limit, order_by=order_by)

    # -- content actions --
    def get_book_content(self, book_id: int) -> BookContent:
        """Return a :class:`BookContent` handle for reading a book's full text.

        Performs three pre-checks before returning:

        1. The book's file is recorded in the library (``ZPATH`` is set).
        2. The file is locally downloaded, not an iCloud placeholder.
        3. The file is not DRM-protected (no ``META-INF/encryption.xml``).

        :raises BookNotDownloadedError: if the book has no local file
            (``path`` is None) or exists only as an iCloud placeholder. The
            fix in both cases is to open the book in Apple Books to trigger
            a download.
        :raises DRMProtectedError: if the book is FairPlay-protected — a
            non-sample Apple Books Store purchase. Its chapters are
            readable only through the Apple Books reader.
        """
        book = self.get_book_by_id(book_id)

        if not book.path:
            raise BookNotDownloadedError(
                f"'{book.title}' has not been downloaded to this Mac. "
                f"Open it in Apple Books to download a local copy, then "
                f"try again."
            )

        content = BookContent(pathlib.Path(book.path))

        if not content.is_downloaded:
            raise BookNotDownloadedError(
                f"'{book.title}' is stored in iCloud and has not been "
                f"downloaded to this Mac. Open it in Apple Books to trigger "
                f"a download, then try again."
            )

        if content.is_drm_protected:
            raise DRMProtectedError(
                f"'{book.title}' is a DRM-protected Apple Books Store "
                f"purchase (FairPlay). Its text content cannot be read "
                f"directly; only imported EPUBs and PDFs are readable."
            )

        return content

    def get_current_reading_location(self, book_id: int) -> Optional[Annotation]:
        """Return the auto-tracked 'current reading position' bookmark, or
        None if none exists.

        Apple Books silently creates and updates one bookmark-style
        annotation per book as the user reads — it's how the reader
        restores your place when you reopen a book. These annotations
        have ``ZANNOTATIONTYPE = 3``, empty selected text and note, and
        a zero-width CFI range in :attr:`Annotation.location`.

        Callers that also want the chapter resolved from the CFI should
        use :meth:`get_current_reading_chapter` instead, which does both
        lookups in one call.
        """
        book = self.get_book_by_id(book_id)
        if not book.asset_id:
            return None
        results = list(
            Annotation.manager.filter(
                asset_id=book.asset_id,
                type=_ANNOTATION_TYPE_READING_BOOKMARK,
                is_deleted=False,
                limit=1,
            )
        )
        return results[0] if results else None

    def get_current_reading_chapter(self, book_id: int) -> Optional[Chapter]:
        """Return the :class:`Chapter` the user was last reading, or None.

        Combines :meth:`get_current_reading_location` with
        :meth:`BookContent.chapter_at_cfi`:

        1. Look up the book's auto-bookmark annotation.
        2. Parse its CFI to identify the current spine item.
        3. Return the corresponding :class:`Chapter` entry.

        :raises BookNotDownloadedError: if the book isn't available
            locally (same preconditions as :meth:`get_book_content`).
        :raises DRMProtectedError: if the book is DRM-protected.
        """
        bookmark = self.get_current_reading_location(book_id)
        if bookmark is None or not bookmark.location:
            return None
        content = self.get_book_content(book_id)
        return content.chapter_at_cfi(bookmark.location)
