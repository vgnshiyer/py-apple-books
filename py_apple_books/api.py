import pathlib
from datetime import datetime
from typing import Optional
from py_apple_books.content import BookContent, Chapter
from py_apple_books.exceptions import (
    AppleBooksError,
    BookNotDownloadedError,
    DRMProtectedError,
)
from py_apple_books.models import Book, Collection, Annotation, AnnotationColor
from py_apple_books.models.manager import ModelIterable
from py_apple_books.utils import APPLE_EPOCH_OFFSET, snap_window


# Apple Books' ``ZANNOTATIONTYPE`` value for the automatic "current reading
# position" bookmark. Distinct from highlights (1) and notes (2); one per
# book, updated as the user reads, with empty selected_text/note and a
# zero-width CFI range.
#
# User-facing annotation queries (``list_annotations``, search, date-range,
# color) silently exclude these rows — they aren't user-created annotations
# and showing them as empty-text entries is confusing. For direct access to
# the bookmark itself, use :meth:`PyAppleBooks.get_current_reading_location`.
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
    #
    # All user-facing annotation queries filter out Apple Books' auto-tracked
    # reading-position bookmarks (``type = 3``). These are system entries with
    # empty text, not user-created highlights or notes — callers that want
    # them specifically should use :meth:`get_current_reading_location`.

    def list_annotations(self, limit: int = None, order_by: str = None) -> ModelIterable:
        """List all user-created annotations (highlights and notes).

        Excludes Apple Books' auto-tracked reading-position bookmarks.
        """
        return Annotation.manager.filter(
            type__ne=_ANNOTATION_TYPE_READING_BOOKMARK,
            limit=limit,
            order_by=order_by,
        )

    def get_annotation_by_id(self, annotation_id: str) -> Annotation:
        """Get an annotation by id (returns bookmarks too — use when the
        caller has already obtained the id from a specific API)."""
        return Annotation.manager.filter(id=annotation_id)[0]

    def get_annotations_by_color(self, color: str, limit: int = None, order_by: str = None) -> ModelIterable:
        """Get user highlights by color."""
        style = AnnotationColor[color.upper()].value
        # The color filter (style in 1..5) already excludes bookmarks
        # (style = 0); the explicit type filter is a belt-and-suspenders
        # guard against future style reuse.
        return Annotation.manager.filter(
            style=style,
            type__ne=_ANNOTATION_TYPE_READING_BOOKMARK,
            limit=limit,
            order_by=order_by,
        )

    def search_annotation_by_highlighted_text(self, text: str,
                                              limit: int = None, order_by: str = None) -> ModelIterable:
        """Search user annotations by highlighted text."""
        return Annotation.manager.filter(
            selected_text__contains=text,
            type__ne=_ANNOTATION_TYPE_READING_BOOKMARK,
            limit=limit,
            order_by=order_by,
        )

    def search_annotation_by_note(self, note: str, limit: int = None, order_by: str = None) -> ModelIterable:
        """Search user annotations by note."""
        return Annotation.manager.filter(
            note__contains=note,
            type__ne=_ANNOTATION_TYPE_READING_BOOKMARK,
            limit=limit,
            order_by=order_by,
        )

    def search_annotation_by_text(self, text: str, limit: int = None, order_by: str = None):
        """Search user annotations by any text that contains the given text.

        The OR across ``selected_text`` / ``representative_text`` / ``note``
        can't cleanly combine with an AND on ``type`` in the current
        manager (``use_or`` applies to every WHERE clause uniformly),
        so we over-fetch from the manager and strip bookmarks in
        Python. The over-fetch factor compensates for the filter loss
        when the caller supplies a limit.

        Returns a list (not a :class:`ModelIterable`) to reflect the
        post-processing step.
        """
        fetch_limit = (limit * 2) if limit else None
        raw = list(Annotation.manager.filter(
            selected_text__contains=text,
            representative_text__contains=text,
            note__contains=text,
            use_or=True,
            limit=fetch_limit,
            order_by=order_by,
        ))
        filtered = [
            a for a in raw
            if getattr(a, "type", None) != _ANNOTATION_TYPE_READING_BOOKMARK
        ]
        return filtered[:limit] if limit else filtered

    def get_annotations_by_date_range(self, after: datetime = None, before: datetime = None,
                                       limit: int = None, order_by: str = None) -> ModelIterable:
        """Get user annotations within a date range.

        Args:
            after: Only include annotations created after this datetime.
            before: Only include annotations created before this datetime.
            limit: Maximum number of results.
            order_by: Field to sort by (prefix with - for descending).
        """
        kwargs = {"type__ne": _ANNOTATION_TYPE_READING_BOOKMARK}
        if after:
            kwargs["creation_date__gte"] = after.timestamp() - APPLE_EPOCH_OFFSET
        if before:
            kwargs["creation_date__lte"] = before.timestamp() - APPLE_EPOCH_OFFSET
        if limit:
            kwargs["limit"] = limit
        if order_by:
            kwargs["order_by"] = order_by
        return Annotation.manager.filter(**kwargs)

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

        Looks up the book's auto-bookmark annotation, pulls
        :attr:`Location.chapter_id` off the parsed CFI, and matches it
        against :meth:`BookContent.list_chapters`. Returns None when
        the book has no bookmark, the CFI lacks a bracket hint, or the
        hinted spine entry isn't a ToC chapter (only the current-reading
        surface cares about ToC-level resolution; generic content reads
        go through :meth:`get_annotation_surrounding_text` which handles
        sub-sections via :meth:`BookContent.get_chapter`).

        :raises BookNotDownloadedError: if the book isn't available
            locally (same preconditions as :meth:`get_book_content`).
        :raises DRMProtectedError: if the book is DRM-protected.
        """
        bookmark = self.get_current_reading_location(book_id)
        if bookmark is None or not bookmark.location or not bookmark.location.chapter_id:
            return None
        content = self.get_book_content(book_id)
        target_id = bookmark.location.chapter_id
        for ch in content.list_chapters():
            if ch.id == target_id:
                return ch
        return None

    def get_annotation_surrounding_text(
        self,
        annotation_id: int,
        chars_before: int = 300,
        chars_after: int = 300,
    ) -> str:
        """Return a text window around an annotation's highlight.

        Pulls the annotation's CFI from its :class:`Location`, fetches
        the chapter (or sub-section) text via
        :meth:`BookContent.get_chapter`, finds the annotation's
        selected text inside the chapter, and returns a snippet of
        ``chars_before`` characters before and ``chars_after`` after,
        snapped to whitespace so the window never starts or ends
        mid-word.

        Degrades gracefully (returns ``""``) in any of these cases:

        * the annotation has no CFI or the CFI carries no bracket hint
          (so there's no chapter to fetch);
        * the book isn't available locally (iCloud placeholder,
          never-downloaded);
        * the book is DRM-protected;
        * the spine entry isn't readable for any reason;
        * the anchor text can't be located in the chapter.

        :param annotation_id: Annotation id from any of the annotation
            facade methods (``list_annotations``, ``recent_annotations``,
            etc.) or from
            :meth:`get_current_reading_location`.
        :param chars_before: Characters of context to include before
            the annotation's anchor text.
        :param chars_after: Characters of context after.
        """
        try:
            annotation = self.get_annotation_by_id(annotation_id)
        except IndexError:
            return ""

        if not annotation.location or not annotation.location.chapter_id:
            return ""

        book = getattr(annotation, "book", None)
        if book is None:
            return ""

        try:
            content = self.get_book_content(book.id)
            chapter_text = content.get_chapter(annotation.location.chapter_id)
        except AppleBooksError:
            return ""

        anchor = (
            (annotation.selected_text or "").strip()
            or (annotation.representative_text or "").strip()
            or None
        )

        if not chapter_text:
            return ""

        if anchor:
            idx = chapter_text.find(anchor)
            if idx >= 0:
                return snap_window(
                    chapter_text, idx, len(anchor), chars_before, chars_after
                )

        # Fallback: opening of the chapter if anchor isn't findable.
        end = min(len(chapter_text), chars_before + chars_after)
        return snap_window(chapter_text, 0, 0, 0, end)
