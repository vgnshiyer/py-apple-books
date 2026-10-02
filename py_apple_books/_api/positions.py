"""The :class:`~py_apple_books.PyAppleBooks` mixin for where the reader is (reading positions,
annotation locations, annotation context), new in 1.11.

- ``get_reading_position``: where the reader is in a book (EPUB CFI and
  chapter, PDF page), from Apple Books' reading-position row, else
  inferred from the newest located annotation.
- ``get_annotation_locations``: the spine file and chapter of many
  annotations, each book read once.
- ``get_annotation_context``: an annotation's highlight with the text
  around it, as a structured :class:`~py_apple_books.positions.AnnotationContext`.

They supersede ``get_current_reading_location``,
``get_current_reading_chapter`` and ``get_annotation_surrounding_text``,
which are unchanged.

Book files are read through the book index (``_epub_index``) after its
gate, never with ``du`` or a walk of the bundle. See
``py_apple_books._api`` for the rules mixin code follows.
"""

import re
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple, Union

from py_apple_books._api._common import (
    _book_arg,
    _id_text,
    _reading_bookmarks,
    _recent_located_annotations,
    _strict_int,
)
from py_apple_books.db.client import current_library
from py_apple_books.exceptions import (
    AnnotationNotFoundError,
    AppleBooksError,
    BookNotDownloadedError,
    ContextUnavailableError,
    DBError,
    InvalidArgumentError,
    NotInLibraryError,
)
from py_apple_books.models.annotation import Annotation
from py_apple_books.models.book import STATE_CLOUD_ONLY, Book
from py_apple_books.positions import (
    AnnotationContext,
    PositionSource,
    ReadingPosition,
    ResolvedLocation,
    TextMatch,
    UnavailableReason,
)

# -- reading positions ----------------------------------------------------------

# The columns get_reading_position reads (type: the fraction columns and
# the page data are read only on bookmark rows).
_BOOKMARK_FIELDS = ("id", "asset_id", "type", "is_deleted", "location", "location_data",
                    "position_fraction", "furthest_fraction", "modification_date")
_RECENT_FIELDS = ("id", "asset_id", "type", "is_deleted", "location", "creation_date")
# How many recent located annotations are looked at for a usable CFI.
_RECENT_LIMIT = 5
# A page count is estimated from page / fraction only when that is this
# close to a whole number.
_PAGE_COUNT_RESIDUAL = 0.05


def _page_count(book: Book, page: Optional[int], fraction: Optional[float]) -> Tuple[Optional[int], bool]:
    """``(page_count, estimated)`` of a PDF: the count Books records when
    it is more than 1; else ``round(page / fraction)`` when that is within
    :data:`_PAGE_COUNT_RESIDUAL` of a whole number at least ``page``."""
    recorded = getattr(book, "page_count", None)
    if isinstance(recorded, int) and not isinstance(recorded, bool) and recorded > 1:
        return recorded, False
    if page is None or not fraction or fraction <= 0:
        return None, False
    estimate = page / fraction
    count = round(estimate)
    if count >= page and abs(estimate - count) <= _PAGE_COUNT_RESIDUAL:
        return count, True
    return None, False


# -- annotation context ---------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")


def _snap_parts(text: str, match_pos: int, match_len: int, chars_before: int,
                chars_after: int) -> Tuple[str, str, str, bool, bool]:
    """``utils.snap_window`` split in parts: ``(before, highlight, after,
    clipped_start, clipped_end)`` such that ``'…' * clipped_start +
    before + highlight + after + '…' * clipped_end`` is exactly
    ``snap_window(text, match_pos, match_len, chars_before,
    chars_after)``. Each part is a piece of the window with its
    whitespace collapsed; a whitespace run around a boundary goes to the
    part before it."""
    # The window, as snap_window computes it.
    raw_start = max(0, match_pos - chars_before)
    raw_end = min(len(text), match_pos + match_len + chars_after)
    start = raw_start
    if start > 0:
        sp = text.find(" ", raw_start, match_pos)
        if sp != -1:
            start = sp + 1
    end = raw_end
    if end < len(text):
        sp = text.rfind(" ", match_pos + match_len, raw_end)
        if sp != -1:
            end = sp
    window = text[start:end]
    full = _WHITESPACE.sub(" ", window)
    snippet = full.strip()
    lead = len(full) - len(full.lstrip())

    def at(offset: int) -> int:
        # Where window[offset] lands in snippet: collapsing the prefix
        # gives a prefix of the collapsed window.
        cut = len(_WHITESPACE.sub(" ", window[:max(0, min(offset, len(window)))])) - lead
        return max(0, min(cut, len(snippet)))

    a, b = at(match_pos - start), at(match_pos + match_len - start)
    return snippet[:a], snippet[a:b], snippet[b:], start > 0, end < len(text)


class _Found(NamedTuple):
    start: int
    end: int
    text_match: TextMatch
    occurrences: int
    disambiguated: bool


def _occurrences(text: str, passage: str) -> Tuple[Optional[TextMatch], List[Tuple[int, int]]]:
    """Every match of ``passage`` in ``text`` at the strictest tier that
    finds one (R18): 1.10's whitespace-flexible pattern (its matches that
    are character for character the passage report ``EXACT``), then
    ignoring invisible characters, then folded. ``(None, [])`` when no
    tier finds it."""
    from py_apple_books.text import _find_passage, _passage_pattern, finditer_folded

    pattern = _passage_pattern(passage)
    if pattern is None:
        return None, []
    try:
        spans = [m.span() for m in re.finditer(pattern, text)]
    except (re.error, OverflowError, RecursionError):
        spans = []
    if spans:
        return TextMatch.WHITESPACE, spans
    spans = _find_passage(text, passage)
    if spans:
        return TextMatch.INVISIBLE, spans
    spans = list(finditer_folded(text, passage))
    if spans:
        return TextMatch.FOLDED, spans
    return None, []


def _locate_highlight(text: str, selected: Optional[str], representative: Optional[str]) -> Optional[_Found]:
    """Where an annotation's text is in its file's ``text``: the
    selected text (else the representative text, as 1.10), at the first
    tier that finds it (see :func:`_occurrences`). Of several
    occurrences the first is taken, unless the representative text (the
    passage around the selection) occurs exactly once and holds one of
    them: then that one (``disambiguated``). None when it isn't found."""
    selected = (selected or "").strip()
    representative = (representative or "").strip()
    passage = selected or representative
    if not passage:
        return None
    tier, spans = _occurrences(text, passage)
    if tier is None:
        return None
    chosen, disambiguated = spans[0], False
    if len(spans) > 1 and selected and len(representative) > len(selected):
        _, around = _occurrences(text, representative)
        if len(around) == 1:
            lo, hi = around[0]
            inside = [s for s in spans if lo <= s[0] and s[1] <= hi]
            if inside:
                chosen, disambiguated = inside[0], True
    if tier is TextMatch.WHITESPACE and text[chosen[0]:chosen[1]] == passage:
        tier = TextMatch.EXACT
    return _Found(chosen[0], chosen[1], tier, len(spans), disambiguated)


# Fixed, path-free messages for ContextUnavailableError (annotation-level
# reasons; the book is readable or wasn't needed).
_CONTEXT_MESSAGES = {
    ContextUnavailableError.NO_LOCATION: "This annotation records no location in its book.",
    ContextUnavailableError.NO_HIGHLIGHT_TEXT: "This annotation has no highlighted text (it is a bookmark).",
    ContextUnavailableError.ORPHANED: "This annotation's book is no longer in the library.",
    ContextUnavailableError.EMPTY_CHAPTER: "The part of the book this annotation is in has no text.",
    ContextUnavailableError.HIGHLIGHT_NOT_FOUND: (
        "The highlighted text wasn't found in the part of the book the annotation is in."),
}


def _unavailable(reason: str, annotation_id: Any) -> ContextUnavailableError:
    return ContextUnavailableError(_CONTEXT_MESSAGES[reason], reason=reason, annotation_id=annotation_id)


def _size(value: Any, name: str) -> int:
    """A context size: a whole number from 0 (bools refused)."""
    try:
        size = _strict_int(value, name)
    except InvalidArgumentError:
        raise InvalidArgumentError(f"{name} must be a whole number from 0.") from None
    if size < 0:
        raise InvalidArgumentError(f"{name} must be a whole number from 0.")
    return size


# -- book gates ---------------------------------------------------------------------


def _gate(book: Any) -> Any:
    """``_epub_index._gate_book(book)``: why the book can't be read, or
    its index."""
    from py_apple_books import _epub_index

    return _epub_index._gate_book(book)


def _gate_error(book: Any, gated: Any) -> AppleBooksError:
    """The error a refused book raises from ``get_annotation_context``:
    the gate's own (a ``BookContent`` method's), or, for a refusal from
    the library row alone, the error and message
    ``PyAppleBooks.get_book_content`` gives."""
    if gated.error is not None:
        return gated.error
    from py_apple_books import _epub_index, api

    reason = gated.reason
    if reason == UnavailableReason.NOT_OWNED:
        return NotInLibraryError(
            f"{api._quoted_title(book)} is an Apple Books Store series item that "
            f"isn't in your library (an unowned volume or a series "
            f"container), so there is no book file to read.")
    if reason == UnavailableReason.NOT_EPUB:
        import pathlib

        return _epub_index._not_epub(pathlib.Path(book.path)).error
    if getattr(book, "state", None) == STATE_CLOUD_ONLY and book.path:
        return BookNotDownloadedError(api._stored_in_icloud_message(book))
    return BookNotDownloadedError(
        f"{api._quoted_title(book)} has not been downloaded to this Mac. "
        f"Open it in Apple Books to download a local copy, then try again.")


def _resolve_in(boundaries: Any, location: Any) -> Tuple[Optional[ResolvedLocation], Optional[UnavailableReason]]:
    """``(resolved, None)``, or ``(None, reason)`` when resolving failed
    (``NO_LOCATION`` when the location names no file of the spine).
    Database errors propagate."""
    from py_apple_books._content_resolve import _Target, _target_of

    try:
        resolved = boundaries.resolve(location if isinstance(location, _Target) else _target_of(location))
    except DBError:
        raise
    except Exception as e:  # noqa: BLE001 - reported per book, as the reason
        return None, UnavailableReason.of(e) or UnavailableReason.UNREADABLE
    if resolved is None:
        return None, UnavailableReason.NO_LOCATION
    return resolved, None


class _PositionsAPI:
    """Private mixin of :class:`~py_apple_books.PyAppleBooks`."""

    def get_reading_position(self, book_id: Union[int, str, Book], *, resolve_chapter: bool = True,
                             infer: bool = True) -> Optional[ReadingPosition]:
        """Where the reader is in a book (1.11).

        Read from Apple Books' reading-position row for the book (the
        bookmark Books keeps up to date as you read):

        1. its CFI, when it has one into the book's spine
           (``source=BOOKMARK``, with :attr:`~ReadingPosition.location`);
        2. else its page data: for a PDF the page (``page``), for an EPUB
           the spine item (``spine_index``; the position is the start of
           that item);
        3. else, with ``infer`` and for a book that isn't a PDF, the
           location of the newest highlight or bookmark with a CFI into
           the spine (``source=RECENT_ANNOTATION``, an inference: the
           reader was there when they made it);
        4. else, when the row records how far the reader is, a position
           with only :attr:`~ReadingPosition.fraction`.

        :attr:`~ReadingPosition.fraction` and
        :attr:`~ReadingPosition.furthest_fraction` come from the
        reading-position row whichever tier placed the position. For a
        PDF, :attr:`~ReadingPosition.page_count` is the count Books
        records, else estimated as ``page / fraction`` when that is
        within 0.05 of a whole number (``page_count_estimated``).

        :param book_id: The book's id, or a :class:`Book` (used as is
            when read from this library with its asset id, path and
            content type; else read again by id).
        :param resolve_chapter: Also place the position in the book's
            table of contents (:attr:`~ReadingPosition.chapter`,
            :attr:`~ReadingPosition.match`,
            :attr:`~ReadingPosition.total_chapters`). This reads the
            book's index (not its text) after the same checks
            ``get_book_content`` makes, without downloading anything or
            running ``du``; when the book can't be read (not
            downloaded, DRM, a PDF: ``not_epub``) or the position names
            no file of it, the position is still returned, with the
            reason in :attr:`~ReadingPosition.unavailable`. False: no
            file is touched (``spine_index`` and ``item_id`` are then
            those the CFI records).
        :param infer: Fall back to the newest located annotation (tier 3).
        :return: The position, or None when nothing records one, or the
            annotation store lacks the columns positions are read from.
            At most three queries (two when given a :class:`Book`).
        :raises BookNotFoundError: no book has that id.
        :raises AnnotationStoreNotFoundError: there is no annotation
            store.
        """
        needs = ("asset_id", "path", "content_type") + (("state",) if resolve_chapter else ())
        book = _book_arg(book_id, needs=needs, get_book=self.get_book_by_id)
        if not Annotation.manager.has_fields("asset_id", "type", "location", "is_deleted"):
            return None
        asset_id = book.asset_id
        if not asset_id:
            return None
        rows = list(_reading_bookmarks(asset_id, limit=1, only=_BOOKMARK_FIELDS))
        row = rows[0] if rows else None
        is_pdf = bool(book.is_pdf)

        fraction = furthest = None
        page_location = None
        if row is not None:
            fraction = row.position_fraction
            if row.furthest_fraction is not None and (fraction is None or row.furthest_fraction >= fraction):
                furthest = row.furthest_fraction
            page_location = row.page_location
        page = page_location.page if (is_pdf and page_location is not None) else None

        from py_apple_books._content_resolve import _Target, _target_of, _usable

        source = annotation = updated = location = target = None
        spine_index = item_id = None
        if row is not None and _usable(row.location):
            source, annotation, updated, location = PositionSource.BOOKMARK, row, row.modification_date, row.location
        elif row is not None and page_location is not None and (page is not None or not is_pdf):
            source, annotation, updated = PositionSource.BOOKMARK, row, row.modification_date
            if not is_pdf:
                spine_index = page_location.ordinal
                target = _Target(spine_index, None, ())
        elif infer and not is_pdf:
            for recent in _recent_located_annotations(asset_id, limit=_RECENT_LIMIT, only=_RECENT_FIELDS):
                if _usable(recent.location):
                    source, annotation, updated = PositionSource.RECENT_ANNOTATION, recent, recent.creation_date
                    location = recent.location
                    break
        if source is None and row is not None and fraction is not None:
            source, annotation, updated = PositionSource.BOOKMARK, row, row.modification_date
        if source is None:
            return None
        if location is not None:
            from py_apple_books.positions import _spine_index

            spine_index = _spine_index(location)
            item_id = location.chapter_id
            target = _target_of(location)

        page_count, estimated = _page_count(book, page, fraction) if is_pdf else (None, False)
        chapter = match = total = unavailable = None
        if resolve_chapter:
            gated = _gate(book)
            if gated.reason is not None:
                unavailable = gated.reason
            else:
                from py_apple_books._content_resolve import _Boundaries

                total = len(gated.index.chapters)
                if target is None:
                    unavailable = UnavailableReason.NO_LOCATION
                else:
                    resolved, unavailable = _resolve_in(_Boundaries(gated.index, book.path), target)
                    if resolved is not None:
                        chapter, match = resolved.chapter, resolved.match
                        spine_index, item_id = resolved.spine_index, resolved.item_id
        return ReadingPosition(
            book_id=book.id,
            source=source,
            annotation_id=annotation.id,
            updated=updated,
            location=location,
            spine_index=spine_index,
            item_id=item_id,
            chapter=chapter,
            match=match,
            total_chapters=total,
            unavailable=unavailable,
            fraction=fraction,
            furthest_fraction=furthest,
            page=page,
            page_count=page_count,
            page_count_estimated=estimated,
        )

    def get_annotation_locations(self, annotations: Iterable[Annotation]) -> Dict[Any, ResolvedLocation]:
        """The spine file and table-of-contents entry of each annotation
        (1.11), keyed by ``annotation.id``, in the order given (one entry
        per id).

        Each book is looked up once (one query for all of them) and its
        index read once, after the same checks ``get_book_content`` makes
        (without downloading anything or running ``du``); an annotation
        is placed like ``BookContent.resolve(annotation.location)``.
        When an annotation can't be placed, its entry has no chapter and
        says why in :attr:`~py_apple_books.positions.ResolvedLocation.unavailable`:

        * ``no_location``: no CFI, or one naming no file of its book;
        * ``orphaned``: its book is no longer in the library;
        * the book's reason for every annotation of a book that can't be
          read (``not_downloaded``, ``not_owned``, ``drm``, ``not_epub``,
          ``unreadable``); ``spine_index`` and ``item_id`` are then those
          the CFI records.

        Nothing is cached here beyond the book index and anchor tables
        the content APIs share, so a repeat call reads no book file.

        :param annotations: :class:`Annotation` objects (as the list and
            search methods return them), from this library.
        :raises InvalidArgumentError: an item isn't an :class:`Annotation`.
        :raises DBError: the library store can't be read (book-level
            failures never raise).
        """
        from py_apple_books._content_resolve import _Boundaries, _usable
        from py_apple_books.positions import _spine_index

        if isinstance(annotations, Annotation):
            raise InvalidArgumentError("annotations must be an iterable of Annotation objects, not one.")
        distinct: Dict[Any, Annotation] = {}
        for annotation in annotations:
            if not isinstance(annotation, Annotation):
                raise InvalidArgumentError(
                    f"annotations must hold Annotation objects, not {type(annotation).__name__}.")
            distinct.setdefault(annotation.id, annotation)

        located = {key: a for key, a in distinct.items() if _usable(a.location)}
        books = _books_for(a.asset_id for a in located.values())
        per_book: Dict[Any, Any] = {}
        results: Dict[Any, ResolvedLocation] = {}
        for key, annotation in distinct.items():
            location = annotation.location
            if key not in located:
                results[key] = ResolvedLocation(None, None, None, None, UnavailableReason.NO_LOCATION)
                continue
            spine_index, hint = _spine_index(location), location.chapter_id
            book = books.get(annotation.asset_id)
            if book is None:
                results[key] = ResolvedLocation(None, None, spine_index, hint, UnavailableReason.ORPHANED)
                continue
            state = per_book.get(book.id)
            if state is None:
                gated = _gate(book)
                state = per_book[book.id] = (
                    gated.reason if gated.reason is not None else _Boundaries(gated.index, book.path))
            if isinstance(state, UnavailableReason):
                results[key] = ResolvedLocation(None, None, spine_index, hint, state)
                continue
            resolved, reason = _resolve_in(state, location)
            if resolved is None:
                resolved = ResolvedLocation(None, None, spine_index, hint, reason)
            results[key] = resolved
        return results

    def get_annotation_context(self, annotation_id: Union[int, str, Annotation], chars_before: int = 300,
                               chars_after: int = 300) -> AnnotationContext:
        """An annotation's highlight with the text around it, in parts
        (1.11).

        The text is that of the whole spine file the annotation's CFI
        names (``BookContent.get_spine_item_text``), read after the same
        checks ``get_book_content`` makes (without downloading anything
        or running ``du``). The highlight is found there by its selected
        text (else its representative text), first as 1.10 does (with
        any whitespace between words), then also ignoring soft hyphens,
        zero-width spaces and BOMs, then folded (case, accents, quote and
        dash style); :attr:`~AnnotationContext.text_match` says which.
        Of several occurrences, the one inside the annotation's
        representative text is taken when that passage occurs once
        (:attr:`~AnnotationContext.disambiguated`), else the first.
        ``before``, ``highlight`` and ``after`` are cut exactly as
        :meth:`get_annotation_surrounding_text` cuts its window: about
        ``chars_before`` and ``chars_after`` characters, snapped to
        spaces, whitespace collapsed; ``str()`` of the result is that
        window, ellipses included. The chapter is placed as by
        ``BookContent.resolve``. The opening of the file is never given
        in place of a highlight that isn't found.

        :param annotation_id: The annotation's id, or an
            :class:`Annotation` (read again by id unless it comes from
            this library).
        :param chars_before: Characters of context before the highlight
            (a whole number from 0).
        :param chars_after: Characters of context after it.
        :raises InvalidArgumentError: a size that is negative, not a
            whole number, or a bool; or a bool id.
        :raises AnnotationNotFoundError: no annotation has that id.
        :raises ContextUnavailableError: the annotation has no location
            (``reason='no_location'``), no text (``'no_highlight_text'``),
            no book in the library (``'orphaned'``), or its file has no
            text (``'empty_chapter'``) or doesn't hold it
            (``'highlight_not_found'``).
        :raises NotInLibraryError: the book is a Store series item you
            don't own.
        :raises BookNotDownloadedError: the book, or the part of it
            needed, is stored only in iCloud, or has no file here.
        :raises DRMProtectedError: the book is DRM-protected.
        :raises NotEpubError: the book isn't an EPUB.
        :raises ChapterNotFoundError: the annotation's file isn't a text
            document of the book.
        :raises AppleBooksError: the book can't be read.
        """
        before_size = _size(chars_before, "chars_before")
        after_size = _size(chars_after, "chars_after")
        annotation = _annotation_arg(annotation_id)
        key = annotation.id
        location = annotation.location

        from py_apple_books._content_resolve import _Boundaries, _target_of, _usable

        if not _usable(location):
            raise _unavailable(ContextUnavailableError.NO_LOCATION, key)
        selected = annotation.selected_text
        representative = annotation.representative_text
        if not ((selected or "").strip() or (representative or "").strip()):
            raise _unavailable(ContextUnavailableError.NO_HIGHLIGHT_TEXT, key)
        book = annotation.book
        if book is None:
            raise _unavailable(ContextUnavailableError.ORPHANED, key)
        gated = _gate(book)
        if gated.reason is not None:
            raise _gate_error(book, gated)

        index = gated.index
        boundaries = _Boundaries(index, book.path)
        target = _target_of(location)
        resolved = boundaries.resolve(target)
        if resolved is not None:
            item_id, spine_index = resolved.item_id, resolved.spine_index
            chapter, match = resolved.chapter, resolved.match
            if item_id is None or not index.spine[spine_index].readable:
                from py_apple_books.exceptions import ChapterNotFoundError

                raise ChapterNotFoundError(
                    f"No text document at spine index {int(spine_index)} in this book.")
        elif location.chapter_id and location.chapter_id in index.manifest:
            # A hint naming a manifest document outside the spine: read
            # as 1.10 read it (its whole file).
            item_id, spine_index, chapter, match = location.chapter_id, None, None, None
        else:
            raise _unavailable(ContextUnavailableError.NO_LOCATION, key)

        from py_apple_books._messages import quote_name
        from py_apple_books.content import BookContent

        content = BookContent(book.path, book_id=book.id)
        text = content._read_spine_text(
            index, item_id, f"No text document with id {quote_name(item_id)} in this book.")
        if not text.strip():
            raise _unavailable(ContextUnavailableError.EMPTY_CHAPTER, key)
        found = _locate_highlight(text, selected, representative)
        if found is None:
            raise _unavailable(ContextUnavailableError.HIGHLIGHT_NOT_FOUND, key)
        before, highlight, after, clipped_start, clipped_end = _snap_parts(
            text, found.start, found.end - found.start, before_size, after_size)
        return AnnotationContext(
            annotation_id=key,
            book_id=book.id,
            item_id=item_id,
            spine_index=spine_index,
            chapter=chapter,
            match=match,
            before=before,
            highlight=highlight,
            after=after,
            clipped_start=clipped_start,
            clipped_end=clipped_end,
            text_match=found.text_match,
            occurrences=found.occurrences,
            disambiguated=found.disambiguated,
        )


# -- lookups --------------------------------------------------------------------------

# Distinct asset ids looked up with one IN list; more, and every book is
# read in one query instead (SQLite's smallest limit on bound parameters
# is 999).
_IN_LIST_MAX = 500
_GATE_FIELDS = ("id", "asset_id", "title", "path", "state", "content_type", "data_source", "can_redownload")


def _books_for(asset_ids: Iterable[Optional[str]]) -> Dict[str, Book]:
    """``{asset id: Book}`` for the given asset ids, as
    ``annotation.book`` finds them (Store series items included, the
    lowest id for an asset id two rows share). One query, or none when
    there are no asset ids."""
    from py_apple_books._api._common import _books_by_asset

    wanted = list(dict.fromkeys(a for a in asset_ids if a))
    if not wanted:
        return {}
    if len(wanted) > _IN_LIST_MAX:
        return _books_by_asset(only=_GATE_FIELDS)
    books: Dict[str, Book] = {}
    for book in Book.manager.filter(asset_id__in=wanted, order_by="id"):
        books.setdefault(book.asset_id, book)
    return books


def _annotation_arg(annotation_id: Any) -> Annotation:
    """The annotation ``get_annotation_context`` was given: an id (looked
    up), or an :class:`Annotation` (used as is when it comes from this
    library, else looked up by its id)."""
    if isinstance(annotation_id, bool):
        raise InvalidArgumentError("annotation_id must be an annotation id or an Annotation, not a bool.")
    if isinstance(annotation_id, Annotation):
        if annotation_id.__dict__.get("_ab_db") is current_library():
            return annotation_id
        annotation_id = annotation_id.id
    try:
        return Annotation.manager.filter(id=annotation_id)[0]
    except IndexError:
        raise AnnotationNotFoundError(f"No annotation with id {_id_text(annotation_id)}.") from None
