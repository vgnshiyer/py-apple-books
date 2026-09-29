import pathlib
import re
from datetime import datetime
from typing import Optional
from py_apple_books import collection_writer
from py_apple_books.content import BookContent, Chapter
from py_apple_books.db.clause import Q
from py_apple_books.exceptions import (
    AppleBooksError,
    BookNotDownloadedError,
    CollectionNotFoundError,
    DBError,
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

# Default order of the colour and annotation text searches: newest
# first, so a ``limit`` keeps the most recent matches. Pass
# ``order_by=None`` for storage order (the pre-1.10 default).
_SEARCH_ORDER = "-creation_date"


class PyAppleBooks:
    """Facade class for accessing Apple Books data."""

    # -- collection actions --
    #
    # Deleted collections are soft-deleted tombstones (ZDELETEDFLAG=1)
    # kept for iCloud sync — user-facing reads exclude them.

    def list_collections(self, limit: int = None, order_by: str = None, *,
                         offset: int = None) -> ModelIterable:
        """List all collections (excluding deleted ones)."""
        return Collection.manager.filter(is_deleted=0, limit=limit, order_by=order_by, offset=offset)

    def get_collection_by_id(self, collection_id: str) -> Collection:
        """Get a collection and its books.

        Raises :class:`CollectionNotFoundError` (an ``IndexError``
        subclass, so legacy handlers keep working) when the id doesn't
        exist or points at a deleted collection.
        """
        try:
            return Collection.manager.filter(id=collection_id, is_deleted=0)[0]
        except IndexError:
            raise CollectionNotFoundError(f"No collection with id {collection_id}.")

    def get_collection_by_title(self, title: str, *, limit: int = None, order_by: str = None,
                                offset: int = None) -> ModelIterable:
        """Get the collections whose title contains ``title``, ignoring
        case, accents and quote/dash style (deleted ones excluded)."""
        return Collection.manager.filter(title__search=title, is_deleted=0,
                                         limit=limit, order_by=order_by, offset=offset)

    # -- collection write actions --
    #
    # These modify the Apple Books library database directly (Apple
    # exposes no automation API for collections). Every call refuses
    # while Books.app is running, takes a WAL-inclusive backup by
    # default, validates the schema, and runs in a single transaction.
    # See py_apple_books.collection_writer for the invariants
    # maintained and the iCloud-sync caveat.

    def create_collection(self, title: str, details: str = None, backup: bool = True) -> Collection:
        """Create a user collection and return it."""
        new_id = collection_writer.create_collection(title, details, backup=backup)
        return self.get_collection_by_id(new_id)

    def rename_collection(self, collection_id, new_title: str, backup: bool = True) -> Collection:
        """Rename a user-created collection and return it refreshed."""
        collection_writer.rename_collection(collection_id, new_title, backup=backup)
        return self.get_collection_by_id(collection_id)

    def delete_collection(self, collection_id, backup: bool = True) -> None:
        """Delete a user-created collection (soft-delete; books are untouched)."""
        collection_writer.delete_collection(collection_id, backup=backup)

    def add_book_to_collection(self, collection_id, book_id, backup: bool = True) -> bool:
        """Add a book to a collection. Returns False if it was already there."""
        return collection_writer.add_book_to_collection(collection_id, book_id, backup=backup)

    def remove_book_from_collection(self, collection_id, book_id, backup: bool = True) -> bool:
        """Remove a book from a collection. Returns False if it wasn't in it."""
        return collection_writer.remove_book_from_collection(collection_id, book_id, backup=backup)

    # -- book actions --
    def list_books(self, limit: int = None, order_by: str = None, *,
                   offset: int = None) -> ModelIterable:
        """List all books."""
        return Book.manager.all(limit=limit, order_by=order_by, offset=offset)

    def get_book_by_id(self, book_id: str) -> Book:
        """Get a book and its annotations."""
        return Book.manager.filter(id=book_id)[0]

    def get_book_by_title(self, title: str, *, limit: int = None, order_by: str = None,
                          offset: int = None) -> ModelIterable:
        """Get the books whose title contains ``title``, ignoring case,
        accents and quote/dash style."""
        return Book.manager.filter(title__search=title, limit=limit, order_by=order_by, offset=offset)

    def get_books_by_genre(self, genre: str, limit: int = None, order_by: str = None, *,
                           offset: int = None) -> ModelIterable:
        """Get books whose genre contains the given string, ignoring case,
        accents and quote/dash style."""
        return Book.manager.filter(genre__search=genre, limit=limit, order_by=order_by, offset=offset)

    # -- annotation actions --
    #
    # All user-facing annotation queries filter out Apple Books' auto-tracked
    # reading-position bookmarks (``type = 3``). These are system entries with
    # empty text, not user-created highlights or notes — callers that want
    # them specifically should use :meth:`get_current_reading_location`.

    def list_annotations(self, limit: int = None, order_by: str = None, *,
                         offset: int = None) -> ModelIterable:
        """List all user-created annotations (highlights and notes).

        Excludes Apple Books' auto-tracked reading-position bookmarks.
        """
        return Annotation.manager.filter(
            type__ne=_ANNOTATION_TYPE_READING_BOOKMARK,
            limit=limit,
            order_by=order_by,
            offset=offset,
        )

    def get_annotation_by_id(self, annotation_id: str) -> Annotation:
        """Get an annotation by id (returns bookmarks too — use when the
        caller has already obtained the id from a specific API)."""
        return Annotation.manager.filter(id=annotation_id)[0]

    def get_annotations_by_color(self, color: str, limit: int = None, order_by: str = _SEARCH_ORDER, *,
                                 offset: int = None) -> ModelIterable:
        """Get user highlights by color, newest first by default."""
        style = AnnotationColor[color.upper()].value
        # The color filter (style in 1..5) already excludes bookmarks
        # (style = 0); the explicit type filter is a belt-and-suspenders
        # guard against future style reuse.
        return Annotation.manager.filter(
            style=style,
            type__ne=_ANNOTATION_TYPE_READING_BOOKMARK,
            limit=limit,
            order_by=order_by,
            offset=offset,
        )

    # The text searches ignore case, accents and quote/dash/whitespace
    # style on both sides (py_apple_books.text.fold_for_match): "don't"
    # finds "Don’t", "Godel" finds "Gödel". % and _ match themselves.

    def search_annotation_by_highlighted_text(self, text: str, limit: int = None,
                                              order_by: str = _SEARCH_ORDER, *,
                                              offset: int = None) -> ModelIterable:
        """Search user annotations by highlighted text, newest first by default."""
        return Annotation.manager.filter(
            selected_text__search=text,
            type__ne=_ANNOTATION_TYPE_READING_BOOKMARK,
            limit=limit,
            order_by=order_by,
            offset=offset,
        )

    def search_annotation_by_note(self, note: str, limit: int = None, order_by: str = _SEARCH_ORDER, *,
                                  offset: int = None) -> ModelIterable:
        """Search user annotations by note, newest first by default."""
        return Annotation.manager.filter(
            note__search=note,
            type__ne=_ANNOTATION_TYPE_READING_BOOKMARK,
            limit=limit,
            order_by=order_by,
            offset=offset,
        )

    def search_annotation_by_text(self, text: str, limit: int = None, order_by: str = _SEARCH_ORDER, *,
                                  offset: int = None):
        """Search user annotations whose highlighted text, surrounding
        text or note contains the given text, newest first by default.

        Returns a list (not a :class:`ModelIterable`), as before 1.10.
        """
        matches = Annotation.manager.filter(
            where=Q(selected_text__search=text) | Q(representative_text__search=text) | Q(note__search=text),
            type__ne=_ANNOTATION_TYPE_READING_BOOKMARK,
            limit=limit,
            order_by=order_by,
            offset=offset,
        )
        # iter() first: list() on the ModelIterable itself would also call
        # its __len__, which runs the query a second time.
        return list(iter(matches))

    def get_annotations_by_date_range(self, after: datetime = None, before: datetime = None,
                                       limit: int = None, order_by: str = None, *,
                                       offset: int = None) -> ModelIterable:
        """Get user annotations within a date range.

        Args:
            after: Only include annotations created after this datetime.
            before: Only include annotations created before this datetime.
            limit: Maximum number of results.
            order_by: Field to sort by (prefix with - for descending).
            offset: Number of results to skip.
        """
        kwargs = {"type__ne": _ANNOTATION_TYPE_READING_BOOKMARK}
        if after:
            kwargs["creation_date__gte"] = after.timestamp() - APPLE_EPOCH_OFFSET
        if before:
            kwargs["creation_date__lte"] = before.timestamp() - APPLE_EPOCH_OFFSET
        return Annotation.manager.filter(**kwargs, limit=limit, order_by=order_by, offset=offset)

    # -- reading progress actions --
    def get_books_in_progress(self, limit: int = None, order_by: str = None, *,
                              offset: int = None) -> ModelIterable:
        """Get books that are currently being read (progress > 0% and < 100%)."""
        return Book.manager.filter(reading_progress__gt=0, 
                                  reading_progress__lt=100, 
                                  limit=limit, 
                                  order_by=order_by,
                                  offset=offset)

    def get_finished_books(self, limit: int = None, order_by: str = None, *,
                           offset: int = None) -> ModelIterable:
        """Get books that are marked as finished."""
        return Book.manager.filter(is_finished=True, limit=limit, order_by=order_by, offset=offset)

    def get_unstarted_books(self, limit: int = None, order_by: str = None, *,
                            offset: int = None) -> ModelIterable:
        """Get books that haven't been started (progress = 0% or None)."""
        return Book.manager.filter(reading_progress__lte=0, limit=limit, order_by=order_by, offset=offset)

    def get_recently_read_books(self, limit: int = 10, order_by: str = "-last_opened_date", *,
                                offset: int = None) -> ModelIterable:
        """Get recently opened books, ordered by last opened date."""
        return Book.manager.filter(last_opened_date__isnull=False, limit=limit, order_by=order_by,
                                   offset=offset)

    # -- content actions --
    def get_book_content(self, book_id: int) -> BookContent:
        """Return a :class:`BookContent` handle for reading a book's full text.

        Performs three pre-checks before returning:

        1. The book's file is recorded in the library (``ZPATH`` is set).
        2. The file is locally downloaded, not an iCloud placeholder.
        3. The file is not DRM-protected: no FairPlay ``sinf.xml``, no
           Adobe ``rights.xml``, and no ``META-INF/encryption.xml`` that
           encrypts more than fonts (see
           :attr:`BookContent.is_drm_protected`).

        :raises BookNotDownloadedError: if the book has no local file
            (``path`` is None) or exists only as an iCloud placeholder. The
            fix in both cases is to open the book in Apple Books to trigger
            a download.
        :raises DRMProtectedError: if the book is DRM-protected — usually
            a FairPlay-protected, non-sample Apple Books Store purchase;
            occasionally an encrypted imported EPUB. Its chapters are
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
            # Only sinf.xml proves a FairPlay Store purchase; anything
            # else is an imported EPUB carrying its own DRM.
            if content._drm_evidence() == "sinf.xml":
                raise DRMProtectedError(
                    f"'{book.title}' is a DRM-protected Apple Books Store "
                    f"purchase (FairPlay). Its text content cannot be read "
                    f"directly; only imported EPUBs and PDFs are readable."
                )
            raise DRMProtectedError(
                f"'{book.title}' is an encrypted EPUB (DRM). Its text "
                f"content cannot be read directly; only DRM-free EPUBs "
                f"are readable."
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
        go through :meth:`get_annotation_surrounding_text`, which reads
        the whole spine file the CFI names, sub-sections included).

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

        Pulls the annotation's CFI from its :class:`Location`, extracts
        the text of the whole spine file the CFI names (not the
        fragment-scoped :meth:`BookContent.get_chapter` text, which can
        start after the highlight), finds the annotation's selected text
        in it — tolerating whitespace differences such as the line
        breaks Apple Books keeps in ``selected_text`` — and returns a
        snippet of ``chars_before`` characters before and
        ``chars_after`` after, snapped to whitespace so the window never
        starts or ends mid-word.

        Degrades gracefully (returns ``""``) in any of these cases:

        * the annotation has no CFI or the CFI carries no bracket hint
          (so there's no chapter to fetch);
        * the book isn't available locally (iCloud placeholder,
          never-downloaded);
        * the book is DRM-protected;
        * the spine entry isn't readable for any reason;
        * the annotation has no text, or its text can't be located in
          the chapter. (The chapter opening is never returned in its
          place.)

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

        anchor = (
            (annotation.selected_text or "").strip()
            or (annotation.representative_text or "").strip()
        )
        if not anchor:
            return ""

        try:
            content = self.get_book_content(book.id)
            chapter_text = content._spine_item_text(
                annotation.location.chapter_id
            )
        except DBError:
            # An AppleBooksError since 1.10, but a database failure isn't
            # an unreadable book: let it propagate, as it did before.
            raise
        except AppleBooksError:
            return ""

        # Extraction collapses whitespace, so match the anchor's words
        # separated by any whitespace run rather than verbatim.
        pattern = r"\s+".join(re.escape(word) for word in anchor.split())
        match = re.search(pattern, chapter_text)
        if match is None:
            return ""
        return snap_window(
            chapter_text,
            match.start(),
            match.end() - match.start(),
            chars_before,
            chars_after,
        )
