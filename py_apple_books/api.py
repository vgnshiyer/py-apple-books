import functools
import hashlib
import inspect
import os
import pathlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from py_apple_books import collection_writer, write_safety
from py_apple_books.content import BookContent, Chapter
from py_apple_books.db.clause import Q
from py_apple_books.db.client import (
    USE_DEFAULT,
    LibraryDB,
    _store,
    current_library,
    default_data_dir,
    default_library,
    query_deadline as _query_deadline,
    use_library,
)
from py_apple_books.exceptions import (
    AnnotationNotFoundError,
    AppleBooksError,
    BookNotDownloadedError,
    BookNotFoundError,
    CollectionNotFoundError,
    DBError,
    DRMProtectedError,
    InvalidChoiceError,
    NotInLibraryError,
)
from py_apple_books.models import (
    Annotation,
    AnnotationColor,
    AnnotationType,
    Book,
    Collection,
    ReadingStatus,
)
from py_apple_books.models.book import CONTENT_TYPE_SERIES_CONTAINER, SERIES_DATA_SOURCE
from py_apple_books.models.manager import ModelIterable, normalize_limit, normalize_offset
from py_apple_books.utils import APPLE_EPOCH_OFFSET, snap_window


# Apple Books' ``ZANNOTATIONTYPE`` value for the automatic "current reading
# position" bookmark (see :class:`AnnotationType`: 0 is a deletion
# tombstone, 1 a user bookmark, 2 a highlight, with or without a note);
# one per book, updated as the user reads, with empty selected_text/note
# and a zero-width CFI range.
#
# User-facing annotation queries (``list_annotations``, search, date-range,
# color) silently exclude these rows — they aren't user-created annotations
# and showing them as empty-text entries is confusing. For direct access to
# the bookmark itself, use :meth:`PyAppleBooks.get_current_reading_location`.
_ANNOTATION_TYPE_READING_BOOKMARK = int(AnnotationType.READING_POSITION)

# Scope of the user-facing annotation queries. Live: not soft-deleted
# (ZANNOTATIONDELETED, NULL-safe), not a type-0 deletion tombstone and
# not the reading-position row. ``include_deleted=True`` gives the
# pre-1.10 set (everything but the reading-position row). Book.annotations
# uses a copy of _LIVE_ANNOTATIONS (models can't import this module).
_LIVE_ANNOTATIONS = {
    "type__gt": int(AnnotationType.TOMBSTONE),
    "type__ne": _ANNOTATION_TYPE_READING_BOOKMARK,
    "is_deleted__isnot": 1,
}
_ALL_ANNOTATIONS = {"type__ne": _ANNOTATION_TYPE_READING_BOOKMARK}


def _id_text(value) -> str:
    """``value`` for an error message. An int too long for ``str()``
    (``sys.get_int_max_str_digits()``) is a valid, if absurd, id."""
    try:
        return str(value)
    except ValueError:
        return "<an integer too long to print>"


def _annotation_scope(include_deleted: bool) -> dict:
    """Filter keywords for the user-facing annotation queries."""
    return dict(_ALL_ANNOTATIONS if include_deleted else _LIVE_ANNOTATIONS)


def _owned_books_filter() -> dict:
    """Filter keywords for the books in the user's library.

    Leaves out Apple Books Store series rows the user doesn't own: series
    containers (``ZCONTENTTYPE`` 5), and Series-source volumes without
    the redownload (ownership) flag. NULL-safe, so a row with no data
    source or content type stays in. A predicate whose column the store
    lacks is dropped, which shows those rows as 1.9.1 did: hiding a row
    needs all the evidence. Same rule as :attr:`Book.is_store_series_item`.
    """
    scope = {}
    if Book.manager.has_fields("content_type"):
        scope["content_type__isnot"] = CONTENT_TYPE_SERIES_CONTAINER
    if Book.manager.has_fields("data_source", "can_redownload"):
        scope["where"] = Q(data_source__isnot=SERIES_DATA_SOURCE) | Q(can_redownload=1)
    return scope


def _book_scope(include_store_series: bool) -> dict:
    """Filter keywords for a book list: all rows if ``include_store_series``,
    else the owned ones."""
    return {} if include_store_series else _owned_books_filter()


# One reading-status rule, finished first: FINISHED is ZISFINISHED = 1
# whatever the progress; IN_PROGRESS is not finished with
# ZREADINGPROGRESS (a 0-1 fraction) above 0; UNSTARTED is not finished
# with progress 0 or NULL. NULL-safe, so over the owned books the three
# sets are disjoint and cover list_books(); Book.reading_status applies
# the same rule to one book.
_STATUS_FILTERS = {
    ReadingStatus.FINISHED: {"is_finished": 1},
    ReadingStatus.IN_PROGRESS: {"is_finished__isnot": 1, "reading_progress__gt": 0},
    ReadingStatus.UNSTARTED: {"is_finished__isnot": 1, "reading_progress__not_gt": 0},
}

# Default order of the colour and annotation text searches: newest
# first, so a ``limit`` keeps the most recent matches. Pass
# ``order_by=None`` for storage order (the pre-1.10 default).
_SEARCH_ORDER = "-creation_date"

# get_recently_read_books orders by Book.last_read_date, newest first by
# default. It isn't a column, so these two orders sort in Python; the
# value says whether the order is descending.
_RECENCY_ORDERS = {"-last_read_date": True, "last_read_date": False}

# The models store_info() checks the mapped columns of.
_MODELS = (Book, Annotation, Collection)


def _writes_home_store(db: LibraryDB) -> bool:
    """Whether the library store ``db`` writes is the current user's:
    one named like Apple Books' stores in the ``BKLibrary`` folder of
    the Apple Books container, however ``db`` names it (no argument, the
    location variables or its own). Told from where ``db`` looks for the
    store, without finding it: a store found in a folder is only written
    if it has such a name (``collection_writer._store_for_writes``)."""
    store = _store("library")
    store_file, data_dir = db._source("library")
    home = default_data_dir() / store.subdir
    if store_file is None:
        folder = home if data_dir is None else data_dir / store.subdir
    elif store.generation.fullmatch(store_file.name):
        folder = store_file.parent
    else:
        return False
    if folder == home:
        return True
    try:
        return os.path.samefile(folder, home)
    except OSError:
        return False


def _own_backup_dir(path) -> pathlib.Path:
    """A folder of its own, under :data:`write_safety.BACKUP_DIR` (read
    at call time), for the backups of the library store ``path``."""
    key = hashlib.sha256(os.fsencode(pathlib.Path(path).resolve())).hexdigest()[:16]
    return write_safety.BACKUP_DIR / "libraries" / key


def _backup_dir_for(db: LibraryDB, path) -> pathlib.Path:
    """The folder the pre-write backups of ``db``'s library store
    ``path`` go to.

    :data:`write_safety.BACKUP_DIR` for the current user's store (see
    :func:`_writes_home_store`), where ``PyAppleBooks()`` has always put
    them; for any other store, a folder of its own under it. Backups are
    told apart by the store's file name, which every copy of a library
    shares, so a folder per store keeps one library's backups from being
    reused or pruned as another's, whether the copy is read through an
    instance or through the location variables.
    """
    return write_safety.BACKUP_DIR if _writes_home_store(db) else _own_backup_dir(path)


@dataclass(frozen=True)
class LibraryStats:
    """Counts over the library, from :meth:`PyAppleBooks.get_library_stats`.

    The book counts cover the books :meth:`PyAppleBooks.list_books`
    returns; the three status counts partition ``total_books`` as the
    three status lists do. The annotation counts cover the annotations
    :meth:`PyAppleBooks.list_annotations` returns; ``orphan_annotations``
    are those whose book isn't in the store (``annotation.book`` is
    None). ``annotations_per_book`` holds ``(book id, title, count)``
    for every book with annotations, most annotated first (ties by id);
    the title is None for a book without one.
    """

    total_books: int
    finished_books: int
    in_progress_books: int
    unstarted_books: int
    total_annotations: int
    orphan_annotations: int
    annotations_per_book: Tuple[Tuple[int, Optional[str], int], ...] = ()


@dataclass(frozen=True)
class StoreInfo:
    """Which Apple Books stores a :class:`PyAppleBooks` reads, from
    :meth:`PyAppleBooks.store_info`. Not hashable: it holds dicts."""

    #: The library store (books and collections).
    library_path: pathlib.Path
    #: The annotation store; None when there is none.
    annotation_path: Optional[pathlib.Path]
    #: Every ``*.sqlite`` file in the folders the stores are looked up in,
    #: as ``{'library': [...], 'annotations': [...]}``.
    candidates: Dict[str, List[pathlib.Path]]
    #: ``{model name: [fields]}``: the mapped fields whose column the store
    #: lacks (read as None). Every field of a model whose table is missing.
    missing_columns: Dict[str, List[str]]
    #: The version of the SQLite library in use.
    sqlite_version: str
    #: Seconds a query may run, or None for no limit.
    query_timeout: Optional[float]
    #: Where collection writes back the library store up to: pass it as
    #: ``backup_dir`` to :func:`~py_apple_books.write_safety.list_backups`
    #: and :func:`~py_apple_books.write_safety.restore_library` (with
    #: ``db_path=library_path``). ``write_safety.BACKUP_DIR`` for the
    #: current user's store (in the Apple Books container); a folder of
    #: its own, under it, for any other store (one named by ``data_dir``,
    #: ``library_db`` or the location variables).
    backup_dir: pathlib.Path


class PyAppleBooks:
    """Facade class for accessing Apple Books data.

    ``PyAppleBooks()`` reads the current user's library: the stores
    ``APPLE_BOOKS_LIBRARY_DB``, ``APPLE_BOOKS_ANNOTATION_DB`` and
    ``APPLE_BOOKS_DATA_DIR`` name, else those in the Apple Books
    container. It shares one pool of connections with every other
    argument-less instance.

    :param data_dir: the Apple Books Documents folder to read (holding
        ``BKLibrary/`` and ``AEAnnotation/``).
    :param library_db: the library store file.
    :param annotation_db: the annotation store file.
    :param query_timeout: seconds a query may run before it is stopped
        with :class:`QueryTimeoutError`; None or 0 for no limit. The
        default reads ``APPLE_BOOKS_QUERY_TIMEOUT`` (else 30 s).

    Given any argument, the instance has a library of its own
    (:class:`~py_apple_books.db.LibraryDB`); :meth:`close` it when done.
    Its results, and their relations, keep reading that library after
    the call returns. Given a store (``data_dir``, ``library_db`` or
    ``annotation_db``), it ignores the location variables; a store not
    given as a file is found in ``data_dir``, else in the Apple Books
    container. Collection writes go to the library store the instance
    reads. They back up the current user's store into the usual backup
    folder, and any other store into a folder of its own
    (``store_info().backup_dir``). Construction does no I/O.

    A subclass's public methods read the instance's library too.
    """

    # The library an instance reads; None: the shared default one (and,
    # inside a use_library() block, that block's library). Name-mangled
    # (_PyAppleBooks__library): 1.9 had no instance attributes, so a
    # subclass may use any other name for its own.
    __library: Optional[LibraryDB] = None

    def __init__(self, data_dir=None, *, library_db=None, annotation_db=None,
                 query_timeout=USE_DEFAULT):
        if (data_dir is not None or library_db is not None or annotation_db is not None
                or query_timeout is not USE_DEFAULT):
            self.__library = LibraryDB(data_dir, library_db=library_db, annotation_db=annotation_db,
                                       query_timeout=query_timeout)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _bind_library(cls)

    def close(self) -> None:
        """Close the idle connections of the library this instance reads
        (the shared default one for ``PyAppleBooks()``). It stays usable:
        the next call connects again."""
        (self.__library or default_library()).close()

    def query_deadline(self, seconds: Optional[float]):
        """A context manager that stops every query in its block still
        running ``seconds`` from now, with :class:`QueryTimeoutError`
        (see :func:`py_apple_books.db.query_deadline`). None changes
        nothing."""
        return _query_deadline(seconds)

    def store_info(self) -> StoreInfo:
        """Which stores this instance reads, the other store files next
        to them, the mapped columns this Apple Books version lacks, and
        where collection writes back the library up to.

        Reads only (the stores' schema).

        :raises LibraryNotFoundError: no library store.
        :raises LibraryAccessDeniedError: macOS refused access.
        """
        db = current_library()
        has_annotations = db.has_annotations()
        paths = db.paths()
        schema = db.schema()
        missing = {}
        for model in _MODELS:
            have = {column.upper() for column in schema.get(model.manager.table_name, ())}
            missing[model.__name__] = [field for field, column in model._get_mappings(model.__name__).items()
                                       if column.upper() not in have]
        return StoreInfo(
            library_path=paths.library,
            annotation_path=paths.annotations if has_annotations else None,
            candidates={kind: db.candidates(kind) for kind in ("library", "annotations")},
            missing_columns=missing,
            sqlite_version=sqlite3.sqlite_version,
            query_timeout=db.query_timeout,
            backup_dir=_backup_dir_for(db, paths.library),
        )

    def _write_path(self) -> Optional[pathlib.Path]:
        """The library store the collection writes go to: the one this
        instance reads, resolved strictly (no guessing between stores;
        see ``collection_writer._store_for_writes``). None for the default
        library, which the writer resolves itself, the same way
        (``collection_writer._default_db_path``)."""
        db = self.__library if self.__library is not None else current_library()
        if db is default_library():
            return None
        return collection_writer._store_for_writes(db)

    def _write_kwargs(self, **kwargs) -> dict:
        """``kwargs`` for a collection_writer call, plus the store to
        write (``db_path``) if this instance's library isn't the default
        one, and its backup folder (``backup_dir``) if it isn't the
        current user's store (:func:`_backup_dir_for`). ``PyAppleBooks()``
        on the current user's store passes neither, as 1.9.1 did."""
        path = self._write_path()
        if path is not None:
            kwargs["db_path"] = path
        db = self.__library if self.__library is not None else current_library()
        if not _writes_home_store(db):
            # The default library's store is the one the writer will find.
            kwargs["backup_dir"] = _own_backup_dir(path or collection_writer._default_db_path())
        return kwargs

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
    #
    # They write the library store the instance reads. If that can't be
    # told for sure (several candidate stores and no canonical one, a
    # canonical one that can't be read, only a copy, or a store other than
    # the one being read), they raise AmbiguousStoreError rather than guess.

    def create_collection(self, title: str, details: str = None, backup: bool = True) -> Collection:
        """Create a user collection and return it."""
        new_id = collection_writer.create_collection(title, details, **self._write_kwargs(backup=backup))
        return self.get_collection_by_id(new_id)

    def rename_collection(self, collection_id, new_title: str, backup: bool = True) -> Collection:
        """Rename a user-created collection and return it refreshed."""
        collection_writer.rename_collection(collection_id, new_title, **self._write_kwargs(backup=backup))
        return self.get_collection_by_id(collection_id)

    def delete_collection(self, collection_id, backup: bool = True) -> None:
        """Delete a user-created collection (soft-delete; books are untouched)."""
        collection_writer.delete_collection(collection_id, **self._write_kwargs(backup=backup))

    def add_book_to_collection(self, collection_id, book_id, backup: bool = True) -> bool:
        """Add a book to a collection. Returns False if it was already there."""
        return collection_writer.add_book_to_collection(collection_id, book_id,
                                                        **self._write_kwargs(backup=backup))

    def remove_book_from_collection(self, collection_id, book_id, backup: bool = True) -> bool:
        """Remove a book from a collection. Returns False if it wasn't in it."""
        return collection_writer.remove_book_from_collection(collection_id, book_id,
                                                             **self._write_kwargs(backup=backup))

    # -- book actions --
    #
    # The book lists and searches return the books in the user's library.
    # Apple Books also keeps rows for Store series it knows about: series
    # containers and volumes the user doesn't own. Those are left out
    # unless ``include_store_series=True`` (the status and recency lists
    # always leave them out). get_book_by_id and the relations resolve
    # every row.

    def list_books(self, limit: int = None, order_by: str = None, *,
                   offset: int = None, include_store_series: bool = False) -> ModelIterable:
        """List the books in the library (Store series items you don't own
        are left out unless ``include_store_series``)."""
        return Book.manager.filter(**_book_scope(include_store_series),
                                   limit=limit, order_by=order_by, offset=offset)

    def get_book_by_id(self, book_id: str) -> Book:
        """Get a book and its annotations. Resolves every row, Store
        series items included.

        :raises BookNotFoundError: no book has that id. An
            :class:`IndexError` subclass, so pre-1.10 handlers still work.
        """
        try:
            return Book.manager.filter(id=book_id)[0]
        except IndexError:
            raise BookNotFoundError(f"No book with id {_id_text(book_id)}.") from None

    def get_book_by_title(self, title: str, *, limit: int = None, order_by: str = None,
                          offset: int = None, include_store_series: bool = False) -> ModelIterable:
        """Get the books whose title contains ``title``, ignoring case,
        accents and quote/dash style (Store series items you don't own
        are left out unless ``include_store_series``)."""
        return Book.manager.filter(title__search=title, **_book_scope(include_store_series),
                                   limit=limit, order_by=order_by, offset=offset)

    def get_books_by_genre(self, genre: str, limit: int = None, order_by: str = None, *,
                           offset: int = None, include_store_series: bool = False) -> ModelIterable:
        """Get books whose genre contains the given string, ignoring case,
        accents and quote/dash style (Store series items you don't own
        are left out unless ``include_store_series``)."""
        return Book.manager.filter(genre__search=genre, **_book_scope(include_store_series),
                                   limit=limit, order_by=order_by, offset=offset)

    # -- annotation actions --
    #
    # The user-facing annotation queries return live user annotations
    # (highlights, notes and bookmarks). They leave out Apple Books'
    # auto-tracked reading-position rows (``type = 3``; see
    # :meth:`get_current_reading_location`), highlights deleted in Books
    # (``is_deleted``, kept for iCloud sync) and type-0 deletion
    # tombstones. ``include_deleted=True`` brings the deleted rows and
    # tombstones back, as before 1.10. get_annotation_by_id is unfiltered.

    def list_annotations(self, limit: int = None, order_by: str = None, *,
                         offset: int = None, include_deleted: bool = False) -> ModelIterable:
        """List all user-created annotations (highlights, notes and
        bookmarks).

        Excludes Apple Books' auto-tracked reading-position bookmarks,
        and deleted annotations unless ``include_deleted``.
        """
        return Annotation.manager.filter(
            **_annotation_scope(include_deleted),
            limit=limit,
            order_by=order_by,
            offset=offset,
        )

    def get_annotation_by_id(self, annotation_id: str) -> Annotation:
        """Get an annotation by id, whatever it is: unlike the list and
        search methods this can return a deleted annotation
        (``is_deleted``), a type-0 tombstone or a reading-position
        bookmark. Use it when the caller already has the id from a
        specific API.

        :raises AnnotationNotFoundError: no annotation has that id. An
            :class:`IndexError` subclass, so pre-1.10 handlers still work.
        """
        try:
            return Annotation.manager.filter(id=annotation_id)[0]
        except IndexError:
            raise AnnotationNotFoundError(f"No annotation with id {_id_text(annotation_id)}.") from None

    def get_annotations_by_color(self, color: str, limit: int = None, order_by: str = _SEARCH_ORDER, *,
                                 offset: int = None, include_deleted: bool = False) -> ModelIterable:
        """Get user highlights by color, newest first by default.

        :raises InvalidChoiceError: ``color`` isn't one of green, blue,
            yellow, pink or purple. A :class:`KeyError` subclass, as 1.9
            raised a bare ``KeyError``.
        """
        try:
            style = AnnotationColor[color.upper()].value
        except KeyError:
            valid = [c.name.lower() for c in AnnotationColor]
            raise InvalidChoiceError(
                f"Unknown highlight color {color!r}. Valid colors: {', '.join(valid)}.",
                value=color, valid=valid,
            ) from None
        # The color filter (style in 1..5) already excludes bookmarks
        # (style = 0); the explicit type filter is a belt-and-suspenders
        # guard against future style reuse.
        return Annotation.manager.filter(
            style=style,
            **_annotation_scope(include_deleted),
            limit=limit,
            order_by=order_by,
            offset=offset,
        )

    # The text searches ignore case, accents and quote/dash/whitespace
    # style on both sides (py_apple_books.text.fold_for_match): "don't"
    # finds "Don’t", "Godel" finds "Gödel". % and _ match themselves.

    def search_annotation_by_highlighted_text(self, text: str, limit: int = None,
                                              order_by: str = _SEARCH_ORDER, *,
                                              offset: int = None,
                                              include_deleted: bool = False) -> ModelIterable:
        """Search user annotations by highlighted text, newest first by default."""
        return Annotation.manager.filter(
            selected_text__search=text,
            **_annotation_scope(include_deleted),
            limit=limit,
            order_by=order_by,
            offset=offset,
        )

    def search_annotation_by_note(self, note: str, limit: int = None, order_by: str = _SEARCH_ORDER, *,
                                  offset: int = None, include_deleted: bool = False) -> ModelIterable:
        """Search user annotations by note, newest first by default."""
        return Annotation.manager.filter(
            note__search=note,
            **_annotation_scope(include_deleted),
            limit=limit,
            order_by=order_by,
            offset=offset,
        )

    def search_annotation_by_text(self, text: str, limit: int = None, order_by: str = _SEARCH_ORDER, *,
                                  offset: int = None, include_deleted: bool = False):
        """Search user annotations whose highlighted text, surrounding
        text or note contains the given text, newest first by default.

        Returns a list (not a :class:`ModelIterable`), as before 1.10.
        """
        matches = Annotation.manager.filter(
            where=Q(selected_text__search=text) | Q(representative_text__search=text) | Q(note__search=text),
            **_annotation_scope(include_deleted),
            limit=limit,
            order_by=order_by,
            offset=offset,
        )
        return list(matches)

    def get_annotations_by_date_range(self, after: datetime = None, before: datetime = None,
                                       limit: int = None, order_by: str = None, *,
                                       offset: int = None, include_deleted: bool = False) -> ModelIterable:
        """Get user annotations within a date range.

        Args:
            after: Only include annotations created after this datetime.
            before: Only include annotations created before this datetime.
            limit: Maximum number of results.
            order_by: Field to sort by (prefix with - for descending).
            offset: Number of results to skip.
            include_deleted: Also return annotations deleted in Apple Books.
        """
        kwargs = _annotation_scope(include_deleted)
        if after:
            kwargs["creation_date__gte"] = after.timestamp() - APPLE_EPOCH_OFFSET
        if before:
            kwargs["creation_date__lte"] = before.timestamp() - APPLE_EPOCH_OFFSET
        return Annotation.manager.filter(**kwargs, limit=limit, order_by=order_by, offset=offset)

    # -- reading progress actions --
    #
    # One rule, finished first (see _STATUS_FILTERS): a book marked
    # finished is finished whatever its progress; otherwise it is in
    # progress above 0% and unstarted at 0% or no progress. Over the books
    # in the library the three lists don't overlap and together equal
    # list_books(). Store series items you don't own are always left out.

    def get_books_in_progress(self, limit: int = None, order_by: str = None, *,
                              offset: int = None) -> ModelIterable:
        """Get books being read: not marked finished, progress above 0%.
        A finished book is never in progress, whatever its progress."""
        return Book.manager.filter(**_owned_books_filter(), **_STATUS_FILTERS[ReadingStatus.IN_PROGRESS],
                                   limit=limit, order_by=order_by, offset=offset)

    def get_finished_books(self, limit: int = None, order_by: str = None, *,
                           offset: int = None) -> ModelIterable:
        """Get books marked as finished, whatever their progress (a
        finished book is in neither of the other two lists)."""
        return Book.manager.filter(**_owned_books_filter(), **_STATUS_FILTERS[ReadingStatus.FINISHED],
                                   limit=limit, order_by=order_by, offset=offset)

    def get_unstarted_books(self, limit: int = None, order_by: str = None, *,
                            offset: int = None) -> ModelIterable:
        """Get books not started: not marked finished, and progress 0% or
        none. A finished book is never unstarted, even at 0%."""
        return Book.manager.filter(**_owned_books_filter(), **_STATUS_FILTERS[ReadingStatus.UNSTARTED],
                                   limit=limit, order_by=order_by, offset=offset)

    def get_recently_read_books(self, limit: int = 10, order_by: str = "-last_read_date", *,
                                offset: int = None) -> ModelIterable:
        """Get recently read books, newest first.

        The default order is :attr:`Book.last_read_date`, the later of the
        last-opened and last-engaged dates (ZLASTOPENDATE alone goes stale
        while a book stays open); ``'last_read_date'`` is oldest first.
        Both sort in Python, ties by id, and apply ``offset`` and
        ``limit`` after sorting. Any other ``order_by`` sorts in SQL:
        ``'-last_opened_date'`` is the pre-1.10 default, and None is
        storage order. Books never opened, and Store series items you
        don't own, are left out.
        """
        if not isinstance(order_by, str) or order_by not in _RECENCY_ORDERS:
            return Book.manager.filter(last_opened_date__isnull=False, **_owned_books_filter(),
                                       limit=limit, order_by=order_by, offset=offset)
        descending = _RECENCY_ORDERS[order_by]
        limit = normalize_limit(limit)
        start = normalize_offset(offset) or 0
        base = Book.manager.filter(last_opened_date__isnull=False, **_owned_books_filter())
        rows = base.run_query()
        keys = list(Book._get_mappings("Book"))
        i_open, i_engaged, i_id = (keys.index(k) for k in ("last_opened_date", "last_engaged_date", "id"))

        def read_at(row) -> float:
            # Raw Core Data seconds; NULL sorts as the oldest.
            return max(float("-inf") if row[i] is None else float(row[i]) for i in (i_open, i_engaged))

        rows = sorted(rows, key=lambda row: (-read_at(row) if descending else read_at(row), row[i_id]))
        sliced = rows[start:] if limit is None else rows[start:start + limit]
        # The iterable holds the library current here (this instance's),
        # which its books and their relations read; run_query() returns
        # the rows and count_by() groups their raw values, as on the
        # SQL-ordered results.
        return ModelIterable(lambda: sliced, Book)

    # -- counts --
    #
    # Counted in SQL with the predicates of the lists they count, so a
    # count always equals the length of its list.

    def count_books_by_status(self) -> Dict[ReadingStatus, int]:
        """The number of books in each reading status: the lengths of
        :meth:`get_finished_books`, :meth:`get_books_in_progress` and
        :meth:`get_unstarted_books`, which add up to :meth:`list_books`.
        Keys are :class:`ReadingStatus` members, which also match their
        string values (``counts['finished']``)."""
        return {status: Book.manager.count(**_owned_books_filter(), **filters)
                for status, filters in _STATUS_FILTERS.items()}

    def count_annotations(self, book_id=None) -> int:
        """The number of annotations :meth:`list_annotations` returns, or
        with ``book_id`` that book's (``len(book.annotations)``).

        :raises BookNotFoundError: no book has that id. An
            :class:`IndexError` subclass.
        """
        if book_id is None:
            return Annotation.manager.count(**_LIVE_ANNOTATIONS)
        return self.get_book_by_id(book_id).annotations.count()

    def get_library_stats(self) -> LibraryStats:
        """Book and annotation counts for the whole library, in five
        statements (see :class:`LibraryStats`)."""
        counts = self.count_books_by_status()
        per_asset = Annotation.manager.filter(**_LIVE_ANNOTATIONS).count_by("asset_id")
        # Every book row, as annotation.book finds them: Store series
        # items included, and the lowest id for an asset id two rows share.
        books: Dict[str, Book] = {}
        for book in Book.manager.all(only=["id", "asset_id", "title"], order_by="id"):
            if book.asset_id is not None:
                books.setdefault(book.asset_id, book)
        per_book = sorted(((books[asset].id, books[asset].title, n)
                           for asset, n in per_asset.items() if asset in books),
                          key=lambda entry: (-entry[2], entry[0]))
        return LibraryStats(
            total_books=sum(counts.values()),
            finished_books=counts[ReadingStatus.FINISHED],
            in_progress_books=counts[ReadingStatus.IN_PROGRESS],
            unstarted_books=counts[ReadingStatus.UNSTARTED],
            total_annotations=sum(per_asset.values()),
            orphan_annotations=sum(n for asset, n in per_asset.items() if asset not in books),
            annotations_per_book=tuple(per_book),
        )

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
        :raises NotInLibraryError: (a :class:`BookNotDownloadedError`)
            if the row is an Apple Books Store series item you don't own
            (:attr:`Book.is_store_series_item`) and has no local file:
            there is nothing to download.
        :raises DRMProtectedError: if the book is DRM-protected — usually
            a FairPlay-protected, non-sample Apple Books Store purchase;
            occasionally an encrypted imported EPUB. Its chapters are
            readable only through the Apple Books reader.
        :raises IndexError: no book has that id. A bare ``IndexError``,
            not an :class:`AppleBooksError`, for 1.x compatibility.
        """
        try:
            book = self.get_book_by_id(book_id)
        except BookNotFoundError as e:
            # 1.x compatibility: this has always raised a bare IndexError
            # for an unknown id, and apple-books-mcp <= 0.8.2 catches
            # AppleBooksError before IndexError around it (its chapter
            # tools), so a BookNotFoundError would report a missing book
            # as unreadable. 2.0 raises BookNotFoundError.
            raise IndexError(str(e)) from None

        # getattr: callers (and tests) may stub get_book_by_id with a
        # plain object. A row with a local file is always tried.
        if getattr(book, "is_store_series_item", False) and not getattr(book, "path", None):
            raise NotInLibraryError(
                f"'{book.title}' is an Apple Books Store series item that "
                f"isn't in your library (an unowned volume or a series "
                f"container), so there is no book file to read."
            )

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


def _in_library(method):
    """``method``, run with its instance's library as the current one
    (``PyAppleBooks.__library``, mangled)."""
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with use_library(self._PyAppleBooks__library):
            return method(self, *args, **kwargs)
    return call


def _bind_library(cls) -> None:
    """Make every public method of ``cls`` read its instance's library
    (:func:`use_library`; the current one for ``PyAppleBooks()``). Results
    keep reading it after the call: models and iterables hold the library
    they were read from."""
    for name, attr in list(vars(cls).items()):
        if not name.startswith("_") and inspect.isfunction(attr):
            setattr(cls, name, _in_library(attr))


_bind_library(PyAppleBooks)
