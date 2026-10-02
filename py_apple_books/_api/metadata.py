"""The :class:`~py_apple_books.PyAppleBooks` mixin for book metadata (library first, then the
book's OPF) and series.

- ``get_book_metadata``: :class:`~py_apple_books.models.BookMetadata`,
  library values first, completed from the book's package document
  (:mod:`py_apple_books._opf`).
- ``get_series``, ``list_series``: Apple Books Store series from the
  library database alone (provisional).

See ``py_apple_books._api`` for the rules mixin code follows.
"""

import datetime as _dt
import os
import stat as _stat
from typing import Callable, Dict, List, Optional, Tuple

from py_apple_books import _icloud, _opf
from py_apple_books._api._common import _book_arg, strict_limit, strict_offset
from py_apple_books.db.clause import Q, Subquery, Where, WhereGroup
from py_apple_books.models.book import (
    CONTENT_TYPE_SERIES_CONTAINER,
    SERIES_DATA_SOURCE,
    STATE_CLOUD_ONLY,
    Book,
    ReadingStatus,
)
from py_apple_books.models.book_metadata import BookMetadata, MetadataFileState
from py_apple_books.models.series import Series, SeriesVolume, _sequence_key
from py_apple_books.text import fold_for_match

_STATES = {
    _opf.READ: MetadataFileState.READ,
    _opf.NOT_DOWNLOADED: MetadataFileState.NOT_DOWNLOADED,
    _opf.UNREADABLE: MetadataFileState.UNREADABLE,
}


class _MetadataAPI:
    """Private mixin of :class:`~py_apple_books.PyAppleBooks`."""

    def get_book_metadata(self, book_id, *, read_files: bool = True) -> BookMetadata:
        """A book's metadata: what the library records, completed from the
        book's own package document (OPF) for what it doesn't (1.11).

        Library values come first: the language Books records, the
        Store release date (else the year) for ``published``/``year``,
        the store's description, and its genre as the first subject.
        The package document adds the rest (publisher, ISBN, further
        subjects, the cover's path, a calibre or EPUB 3 series) and fills
        any gap; ``book_file_fields`` names the fields it filled. Every
        text value is untrusted (the book file decides it).

        Only ``META-INF/container.xml`` and the package document are read,
        and only from an unzipped EPUB that is on this Mac: nothing is
        downloaded from iCloud. ``file_state`` says what happened (see
        :class:`~py_apple_books.models.MetadataFileState`); a book stored
        only in iCloud is ``not_downloaded`` until it is opened in Apple
        Books. The checks, in order: a file recorded (``no_file``), an
        ``.epub`` path (``not_epub``), Books not recording the book as
        stored only in iCloud (``ZSTATE`` 3), all three without touching
        the disk; the bundle being a local folder with no iCloud stub
        next to it; every folder and file in it being on this Mac (each
        folder checked before it is listed), as
        :meth:`get_book_content` checks; then
        :func:`~py_apple_books.content.is_downloaded`; then, again,
        every folder and file on the way to the package document. DRM
        doesn't matter: package documents are never encrypted. Results
        read from the files are cached while they are unchanged.

        :param book_id: the book's id, or a :class:`Book` (used as is
            when read from this library, with its ``path`` and ``state``
            when ``read_files``). A ``Book`` read with ``only=`` that
            left out the language, ``release_date``, ``year``,
            ``description`` or ``genre`` is taken as the library having
            none: the file's values fill them and are named in
            ``book_file_fields``. Pass the id (or a fully read ``Book``)
            for the library's own values.
        :param read_files: False to use the library's values only
            (``file_state`` ``not_requested``; no file access).
        :raises BookNotFoundError: no book has that id (an
            :class:`IndexError`).
        :raises DBError: the library couldn't be read.
        """
        book = _book_arg(book_id, needs=("path", "state") if read_files else (), get_book=self.get_book_by_id)
        if read_files:
            state, fields = _book_file_metadata(book)
        else:
            state, fields = MetadataFileState.NOT_REQUESTED, None
        return _merge(book, state, fields)

    def get_series(self, book_id) -> Optional[Series]:
        """The Apple Books Store series a book belongs to, with every
        volume the library knows, owned or not (provisional, 1.11).

        ``book_id`` may be a volume, a copy of a volume in your library
        that only shares its Store id, or the series container; an id or
        a :class:`Book` (used as is when read from this library, unless
        it has none of the series and Store id fields, as a ``Book``
        read with ``only=`` may: then it is read again once). None when
        Books records no series for the book. Reads the library database
        only (at most 4 statements, 3 given a ``Book``), never a book
        file. ``Series.volumes`` lists the *known* volumes, not the
        length of the series.

        :raises BookNotFoundError: no book has that id (an
            :class:`IndexError`).
        :raises DBError: the library couldn't be read.
        """
        book = _book_arg(book_id, get_book=self.get_book_by_id)
        columns = _SeriesColumns()
        if not columns.any_series:
            return None
        if book is book_id and book.series_id is None and book.series_container_id is None \
                and book.store_id is None:
            # A Book used as is (R7) may come from an only= read that left
            # every series column out; _book_arg's needs= rereads when any
            # field is None, which most volumes are, so check "all" here.
            book = self.get_book_by_id(book.id)
        anchor = book
        if book.series_id is None and book.series_container_id is None and not columns.is_container(book):
            if not columns.store_id or not book.store_id:
                return None
            anchor = _anchor_row(book.store_id, columns)
            if anchor is None:
                return None
        if columns.is_container(anchor):
            container_id, series_id = anchor.id, anchor.series_id or anchor.store_id
        else:
            container_id, series_id = anchor.series_container_id, anchor.series_id
        rows = _series_rows(container_id, series_id, columns)
        wanted = {anchor.id, book.id}
        for series in _group(rows, columns):
            if (series.container is not None and series.container.id in wanted) or any(
                    wanted & set(volume.ids) for volume in series.volumes):
                return series
        return None

    def list_series(self, *, started_only: bool = False, limit=None, offset=None) -> List[Series]:
        """Every Store series the library knows (provisional, 1.11): one
        per series container, plus one per series id whose volumes have
        no container. Ordered by title (compared like searches compare
        text; untitled last), then series id.

        A list (not a :class:`ModelIterable`): ``offset`` and ``limit``
        apply after ordering, in Python. ``started_only`` keeps the series
        with a volume in progress or finished. Reads the library database
        only, in at most 2 statements; ``[]`` on a Books version without
        the series columns.

        :param limit: None for all, else at least 1.
        :param offset: None or at least 0.
        :raises InvalidArgumentError: a bad ``limit`` or ``offset``.
        :raises DBError: the library couldn't be read.
        """
        limit = strict_limit(limit)
        offset = strict_offset(offset) or 0
        columns = _SeriesColumns()
        if not columns.any_series:
            return []
        where = None
        for present, clause in ((columns.series_id, Q(series_id__isnull=False)),
                                (columns.container_id, Q(series_container_id__isnull=False)),
                                (columns.content_type, Q(content_type=CONTENT_TYPE_SERIES_CONTAINER))):
            if present:
                where = clause if where is None else where | clause
        if columns.store_id:
            tagged = [Where(columns.column(name), None, "IS NOT")
                      for name, present in (("series_id", columns.series_id),
                                            ("series_container_id", columns.container_id)) if present]
            where = where | Q(store_id__in=Subquery(
                columns.table, columns.column("store_id"),
                [WhereGroup(tagged, "OR") if len(tagged) > 1 else tagged[0]]))
        rows = list(Book.manager.filter(where=where, order_by="id"))
        found = _group(rows, columns)
        if started_only:
            found = [s for s in found if any(v.reading_status != ReadingStatus.UNSTARTED for v in s.volumes)]
        found.sort(key=_series_order)
        return found[offset:] if limit is None else found[offset:offset + limit]


# -- get_book_metadata ----------------------------------------------------------


def _file_path(raw) -> Optional[str]:
    """A recorded path as text, or None for no file (None, empty, not a
    path). A path stored as bytes (a BLOB) is decoded as the file
    system does, so it is read like the same path stored as text."""
    try:
        path = os.fsdecode(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return path or None


def _is_epub_path(path) -> bool:
    if isinstance(path, bytes):
        return path.rstrip(b"/").lower().endswith(b".epub")
    return path.rstrip("/").lower().endswith(".epub")


def _bundle_state(path) -> Optional[MetadataFileState]:
    """The state of the bundle folder itself, from ``lstat`` (and
    ``stat`` through a symlink), or None if it is a local folder.

    A bundle that isn't on this Mac, with or without an iCloud stub next
    to it, is ``not_downloaded``, as :meth:`get_book_content` reports it
    and as plan rule R7 lines the reasons up (ZSTATE 3, dataless, stub
    and missing); any other failure to look it up is ``unreadable``."""
    with _icloud.no_materialize():
        try:
            if _icloud.icloud_stub(path):
                return MetadataFileState.NOT_DOWNLOADED
            st = _icloud.lstat(path)
            if _stat.S_ISLNK(st.st_mode):
                st = _icloud.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return MetadataFileState.NOT_DOWNLOADED
        except (OSError, ValueError) as e:
            if _icloud.is_materialize_error(e):
                return MetadataFileState.NOT_DOWNLOADED
            return MetadataFileState.UNREADABLE
    if _icloud.is_dataless(st):
        return MetadataFileState.NOT_DOWNLOADED
    if not _stat.S_ISDIR(st.st_mode):
        return MetadataFileState.NOT_EPUB
    return None


def _book_file_metadata(book) -> Tuple[MetadataFileState, Optional[_opf.OpfFields]]:
    """Gate, then read the book's package document (see
    :meth:`_MetadataAPI.get_book_metadata` for the order)."""
    path = _file_path(getattr(book, "path", None))
    if path is None:
        return MetadataFileState.NO_FILE, None
    if not _is_epub_path(path):
        return MetadataFileState.NOT_EPUB, None
    if getattr(book, "state", None) == STATE_CLOUD_ONLY:
        return MetadataFileState.NOT_DOWNLOADED, None
    refused = _bundle_state(path)
    if refused is not None:
        return refused, None
    # The bundle-level checks get_book_content runs before du (R2, owner
    # decision Q1 (a)): every folder and file in the bundle on this Mac,
    # each folder checked before it is listed, so du (which lists every
    # folder) never reaches an iCloud placeholder folder, and a partly
    # evicted book is not_downloaded here as it is for the content APIs.
    if _icloud.walk_bundle_local(path) in (_icloud.FileState.DATALESS, _icloud.FileState.ICLOUD_STUB):
        return MetadataFileState.NOT_DOWNLOADED, None
    # Owner decision Q3 (b): a single-book read keeps 1.10's
    # is_downloaded check after the bundle-level ones.
    from py_apple_books import content

    if not content.is_downloaded(path):
        return MetadataFileState.NOT_DOWNLOADED, None
    result = _opf.read_metadata(path)
    return _STATES[result.state], result.fields


def _release_published(release_date) -> Optional[Tuple[str, int]]:
    """``('YYYY-MM-DD', year)`` of a release date's UTC calendar day."""
    if not isinstance(release_date, _dt.datetime):
        return None
    try:
        day = _dt.datetime.fromtimestamp(release_date.timestamp(), _dt.timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None
    if not _opf.plausible_year(day.year):
        return None
    return day.isoformat(), day.year


def _merge(book, state: MetadataFileState, fields: Optional[_opf.OpfFields]) -> BookMetadata:
    """The library's values first, then the package document's."""
    opf = fields or _opf.OpfFields()
    from_file = set()

    language = _opf.normalize_language(getattr(book, "language", None))
    if language is None and opf.language is not None:
        language = opf.language
        from_file.add("language")

    dated = _release_published(getattr(book, "release_date", None))
    if dated is None:
        year = getattr(book, "year", None)
        if _opf.plausible_year(year):
            dated = (f"{year:04d}", year)
    if dated is None and opf.year is not None:
        dated = (opf.published, opf.year)
        from_file.update(("published", "year"))
    published, year = dated or (None, None)

    description = _opf.clean_description(getattr(book, "description", None))
    if description is None and opf.description is not None:
        description = opf.description
        from_file.add("description")

    # The genre as Books records it (whitespace and length cleaned
    # only); the book's subjects were cleaned when read.
    genre = _opf.clean_subjects([getattr(book, "genre", None)], drop_urls=False)
    subjects = _opf.clean_subjects([*genre, *opf.subjects], drop_urls=False)
    if len(subjects) > len(genre):
        from_file.add("subjects")

    file_only = {name: getattr(opf, name) for name in
                 ("publisher", "isbn", "cover_href", "series_title", "series_sequence")}
    from_file.update(name for name, value in file_only.items() if value is not None)
    return BookMetadata(
        book_id=getattr(book, "id", None), language=language, published=published, year=year,
        subjects=subjects, description=description, file_state=state,
        book_file_fields=frozenset(from_file), **file_only)


# -- series ---------------------------------------------------------------------


class _SeriesColumns:
    """Which series and ownership columns the current library's store
    has (from its cached schema: no statement)."""

    def __init__(self):
        has = Book.manager.has_fields
        self.series_id = has("series_id")
        self.container_id = has("series_container_id")
        self.store_id = has("store_id")
        self.content_type = has("content_type")
        self.owned_rule = has("data_source", "can_redownload")
        self.any_series = self.series_id or self.container_id
        self.table = Book.manager.table_name

    @staticmethod
    def column(field: str) -> str:
        return Book.manager._get_db_field(field)

    def is_container(self, book) -> bool:
        """A series container row: content type 5, or, on a store without
        the content type column, a row whose Store id is its own series
        id."""
        if self.content_type:
            return book.content_type == CONTENT_TYPE_SERIES_CONTAINER
        return book.series_id is not None and book.store_id == book.series_id

    def listed(self, book) -> bool:
        """Whether :meth:`list_books` lists ``book``: the rule of
        ``_api._common._owned_books_filter`` in Python, dropping a
        predicate whose column the store lacks, as the query does (Store
        series containers and Series-source rows without the redownload
        flag are left out)."""
        if self.content_type and book.content_type == CONTENT_TYPE_SERIES_CONTAINER:
            return False
        if self.owned_rule and book.data_source == SERIES_DATA_SOURCE and book.can_redownload != 1:
            return False
        return True


def _anchor_row(store_id: str, columns: _SeriesColumns) -> Optional[Book]:
    """The lowest-id series-tagged row with Store id ``store_id`` (one
    statement): what an owned copy that carries only the Store id stands
    for."""
    where = None
    for present, clause in ((columns.series_id, Q(series_id__isnull=False)),
                            (columns.container_id, Q(series_container_id__isnull=False))):
        if present:
            where = clause if where is None else where | clause
    return next(iter(Book.manager.filter(store_id=store_id, where=where, order_by="id", limit=1)), None)


def _series_rows(container_id: Optional[int], series_id: Optional[str],
                 columns: _SeriesColumns) -> List[Book]:
    """One statement: the container, the rows linked to it or to the
    series id, and the rows sharing a Store id with one of them. The
    parameter count doesn't depend on the series' size."""
    clauses = []
    links = []
    if container_id is not None:
        clauses.append(Q(id=container_id))
        if columns.container_id:
            clauses.append(Q(series_container_id=container_id))
            links.append(Where(columns.column("series_container_id"), container_id))
    if series_id is not None:
        if columns.series_id:
            clauses.append(Q(series_id=series_id))
            links.append(Where(columns.column("series_id"), series_id))
        if columns.store_id:
            clauses.append(Q(store_id=series_id))
    if columns.store_id and links:
        clauses.append(Q(store_id__in=Subquery(
            columns.table, columns.column("store_id"),
            [WhereGroup(links, "OR") if len(links) > 1 else links[0]])))
    if not clauses:
        return []
    where = clauses[0]
    for clause in clauses[1:]:
        where = where | clause
    return list(Book.manager.filter(where=where, order_by="id"))


def _group(rows: List[Book], columns: _SeriesColumns) -> List[Series]:
    """Group series rows into :class:`Series`: containers, the volumes
    linked to them (by container id, else by series id), container-less
    groups by series id, and copies by Store id."""
    unique: Dict[int, Book] = {}
    for row in rows:
        unique.setdefault(row.id, row)
    rows = [unique[i] for i in sorted(unique)]
    containers = {row.id: row for row in rows if columns.is_container(row)}
    by_series_id: Dict[str, Book] = {}
    for container in containers.values():
        key = container.series_id or container.store_id
        if key:
            by_series_id.setdefault(key, container)

    groups: Dict[tuple, dict] = {}

    def group(key: tuple, container: Optional[Book], series_id: Optional[str]) -> dict:
        return groups.setdefault(key, {"container": container, "series_id": series_id, "rows": []})

    for container in containers.values():
        group(("container", container.id), container, container.series_id or container.store_id)
    tagged_store: Dict[str, tuple] = {}
    for row in rows:
        if row.id in containers or (row.series_id is None and row.series_container_id is None):
            continue
        container = containers.get(row.series_container_id)
        if container is None and row.series_id is not None:
            container = by_series_id.get(row.series_id)
        if container is not None:
            key = ("container", container.id)
            group(key, container, container.series_id or container.store_id)
        elif row.series_id is not None:
            key = ("series", row.series_id)
            group(key, None, row.series_id)
        else:
            key = ("dangling", row.series_container_id)
            group(key, None, None)
        groups[key]["rows"].append(row)
        if row.store_id:
            tagged_store.setdefault(row.store_id, key)
    for row in rows:
        if row.id in containers or row.series_id is not None or row.series_container_id is not None:
            continue
        key = tagged_store.get(row.store_id) if row.store_id else None
        if key is not None:
            groups[key]["rows"].append(row)
    return [_series(g["container"], g["series_id"], g["rows"], columns.listed) for g in groups.values()]


def _series(container: Optional[Book], series_id: Optional[str], rows: List[Book],
            listed: Callable[[Book], bool]) -> Series:
    by_volume: Dict[tuple, List[Book]] = {}
    for row in rows:
        by_volume.setdefault(("store", row.store_id) if row.store_id else ("row", row.id), []).append(row)
    volumes = []
    for members in by_volume.values():
        members.sort(key=lambda b: b.id)
        tagged = [m for m in members if m.series_id is not None or m.series_container_id is not None]
        owned = [m for m in members if listed(m)]
        book = owned[0] if owned else (tagged or members)[0]
        sequence = next((m.series_sequence for m in tagged + members if m.series_sequence is not None), None)
        label = next((m.series_label for m in tagged + members if m.series_label is not None), None)
        volumes.append(SeriesVolume(book=book, ids=tuple(m.id for m in members), sequence=sequence,
                                    label=label, in_library=listed(book)))
    volumes.sort(key=_sequence_key)
    return Series(title=container.title if container is not None else None, series_id=series_id,
                  container=container,
                  is_ordered=container.series_is_ordered if container is not None else None,
                  volumes=tuple(volumes))


def _series_order(series: Series):
    title = fold_for_match(series.title) if series.title is not None else None
    first = series.container.id if series.container is not None else min(
        (i for v in series.volumes for i in v.ids), default=0)
    return (title is None, title or "", series.series_id is None, series.series_id or "", first)
