"""Synthetic Apple Books stores built from the committed schema fixtures.

A :class:`FixtureLibrary` is a fake HOME holding the two Core Data
stores Apple Books keeps under
``~/Library/Containers/com.apple.iBooksX/Data/Documents``: ``BKLibrary``
(books and collections) and ``AEAnnotation`` (highlights, notes,
bookmarks). The stores are created from ``schemas/<version>/*.sql``,
which hold Apple's DDL and Core Data bookkeeping but no library rows,
and are then filled with synthetic rows through the ``add_*`` helpers.

Rows follow the conventions observed on a real library (macOS 26.7,
Books 8.5): primary keys come from ``Z_PRIMARYKEY.Z_MAX``, flag columns
that are 0 on every real row default to 0 rather than NULL, and
identifiers are derived deterministically from the row's primary key so
two identically seeded libraries are identical.

Beside the stores, in the same container, a FixtureLibrary can also
write Books' preferences plist (:meth:`FixtureLibrary.write_prefs`) and
its per-book info caches (:meth:`FixtureLibrary.add_book_info_cache`).
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
import sqlite3
import struct
import uuid
from collections import abc
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Union

SCHEMAS_DIR = pathlib.Path(__file__).parent / "schemas"
DOCUMENTS = pathlib.PurePosixPath("Library/Containers/com.apple.iBooksX/Data/Documents")
# Files next to Documents, in the same container (``<container>/Data``).
PREFS_PLIST = pathlib.PurePosixPath("Library/Preferences/com.apple.iBooksX.plist")
BOOK_INFO_DIR = pathlib.PurePosixPath("Library/Caches/AEEpubInfoSource")

# Seconds between the Unix epoch and the Core Data epoch (2001-01-01 UTC).
_APPLE_EPOCH_OFFSET = 978307200
_ID_NAMESPACE = uuid.UUID("5f0c7c4e-2d0b-4c7e-9a52-6a1f3b0e8d11")

#: Core Data seconds of ``NSDate.distantPast`` (0000-12-30 00:00 UTC), the
#: year-0 date Books writes into its preferences plist. Python's
#: ``datetime`` can't hold it, so plain ``plistlib.loads`` fails on such a
#: file. Pass it (or ``float('nan')``) as a date to :meth:`FixtureLibrary.write_prefs`.
YEAR_ZERO = -63114076800.0
_YEAR_ZERO_XML = "0000-12-30T00:00:00Z"

# Store ids (Apple's numeric adam ids) given to series rows that don't
# name one: a fixed base plus the row's primary key.
_STORE_ID_BASE = 1900000000

# ZDATASOURCEIDENTIFIER values seen on real rows.
UBIQUITY = "com.apple.ibooks.datasource.ubiquity"
STORE_SERIES = "com.apple.ibooks.BKLibraryDataSourceSeries"

# kind -> (ZANNOTATIONTYPE, column overrides). Observed on macOS 26.7 /
# Books 8.5: 0 is a deletion tombstone (no asset, no dates, no location),
# 1 a user bookmark, 2 a highlight (a note is a type-2 row with a note
# body; an underline is style 0 with ZANNOTATIONISUNDERLINE 1), 3 the
# automatic reading-position bookmark.
ANNOTATION_KINDS: Dict[str, tuple] = {
    "highlight": (2, {}),
    "note": (2, {}),
    "underline": (2, {"ZANNOTATIONSTYLE": 0, "ZANNOTATIONISUNDERLINE": 1}),
    "bookmark": (1, {"ZANNOTATIONSTYLE": 0}),
    "reading_position": (3, {"ZANNOTATIONSTYLE": 0}),
    "tombstone": (0, {
        "ZANNOTATIONSTYLE": 0, "ZANNOTATIONDELETED": 1, "ZANNOTATIONASSETID": "",
        "ZANNOTATIONCREATIONDATE": None, "ZANNOTATIONLOCATION": None,
        "ZPLLOCATIONRANGESTART": None, "ZPLLOCATIONRANGEEND": None,
        "ZPLABSOLUTEPHYSICALLOCATION": None,
    }),
}

# Highlight colour -> ZANNOTATIONSTYLE.
COLORS = {"green": 1, "blue": 2, "yellow": 3, "pink": 4, "purple": 5}

# Built-in collections: ZCOLLECTIONID -> title, as Books creates them.
SYSTEM_COLLECTIONS = {
    "All_Collection_ID": "Library",
    "AudioBooks_Collection_ID": "Audiobooks",
    "Books_Collection_ID": "Books",
    "Downloaded_Collection_ID": "Downloaded",
    "Finished_Collection_ID": "Finished",
    "Pdfs_Collection_ID": "PDFs",
    "Samples_Collection_ID": "My Samples",
    "Want_To_Read_Collection_ID": "Want to Read",
}
# (ZSORTKEY, ZSORTMODE) of the built-ins on the reference library.
_SYSTEM_COLLECTION_ORDER = {
    "All_Collection_ID": (0, 6),
    "Books_Collection_ID": (-1, 6),
    "Want_To_Read_Collection_ID": (-2, 6),
    "Pdfs_Collection_ID": (-3, 6),
    "AudioBooks_Collection_ID": (-4, 6),
    "Downloaded_Collection_ID": (-6, 6),
    "Finished_Collection_ID": (-7, 8),
    "Samples_Collection_ID": (-8, 6),
}

_CREATOR = "com~apple~iBooks"  # ZANNOTATIONCREATORIDENTIFIER on every real row
_COLLECTION_MODIFIED = 780000000.0  # a fixed Core Data timestamp (2025-09)
_STORES = {"library": "BKLibrary", "annotations": "AEAnnotation"}

_SCHEMA_NAME = re.compile(r"^macos-([\d.]+)-(\w+)_books-([\d.]+)-(\d+)$")


def _version_key(name: str) -> tuple:
    """Sort key for schema directory names: numeric macOS, then Books version."""
    m = _SCHEMA_NAME.match(name)
    if not m:
        return ((), (), 0, name)
    macos, _, books, books_build = m.groups()
    ints = lambda v: tuple(int(p) for p in v.split(".") if p)
    return (ints(macos), ints(books), int(books_build), name)


def available_schemas() -> List[str]:
    """Committed schema fixtures, oldest first (numeric version order)."""
    if not SCHEMAS_DIR.is_dir():
        return []
    names = [
        p.name for p in SCHEMAS_DIR.iterdir()
        if (p / "meta.json").is_file()
        and (p / "BKLibrary.sql").is_file()
        and (p / "AEAnnotation.sql").is_file()
    ]
    return sorted(names, key=_version_key)


# The newest fixture. None only if the schema files weren't installed.
DEFAULT_SCHEMA = (available_schemas() or [None])[-1]


def core_data_time(value: Union[_dt.datetime, float, int, None]) -> Optional[float]:
    """Convert a datetime (naive means UTC) to Core Data seconds.

    Numbers are taken to be Core Data seconds already and pass through,
    as does None.
    """
    if value is None or isinstance(value, (int, float)):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.timestamp() - _APPLE_EPOCH_OFFSET


def page_location_blob(page_offset: int, ordinal: int = 0) -> bytes:
    """A ``ZPLUSERDATA`` value as Books writes it on a bookmark row.

    A binary property list of Books' ``BKPageLocation``: ``pageOffset``
    (the 0-based page of a PDF) and ``super.ordinal`` (the spine item of
    an EPUB bookmark without a CFI). Pass it to
    :meth:`FixtureLibrary.add_annotation` as ``user_data``. Values are
    written as given, so out-of-range ones can be tested.
    """
    import plistlib  # on first use, not at import

    return plistlib.dumps({"class": "BKPageLocation", "pageOffset": page_offset,
                           "super": {"class": "BKLocation", "ordinal": ordinal}},
                          fmt=plistlib.FMT_BINARY)


def _plist_values(value) -> Iterable[Any]:
    """Every value in a plist-shaped object, containers included."""
    yield value
    if isinstance(value, abc.Mapping):
        for item in value.values():
            yield from _plist_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _plist_values(item)


def _fraction_text(value, name: str) -> Optional[str]:
    """A position fraction as the text Books stores it (a str verbatim)."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a float, a str or None, not {type(value).__name__}")
    return repr(float(value))


def build_store(sql_file, dest, journal_mode: str = "DELETE") -> None:
    """Create a SQLite store at ``dest`` from a schema ``.sql`` file.

    ``DELETE`` (rollback journal) is the default because Apple's system
    SQLite refuses to open a WAL store read-only when its ``-wal`` and
    ``-shm`` files are missing.
    """
    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(dest)
    try:
        con.execute(f"PRAGMA journal_mode={journal_mode}")
        con.executescript(pathlib.Path(sql_file).read_text(encoding="utf-8"))
        con.commit()
    finally:
        con.close()


class FixtureLibrary:
    """A synthetic Apple Books library rooted at ``root`` (a fake HOME).

    ``FixtureLibrary(root)`` only describes the paths; use
    :meth:`create` to build the stores. Both stores keep Apple's file
    names, so code that looks the library up the way py_apple_books
    does finds them.
    """

    def __init__(self, root, schema: str = DEFAULT_SCHEMA):
        if schema is None:
            raise FileNotFoundError(f"no schema fixtures installed under {SCHEMAS_DIR}")
        self.root = pathlib.Path(root)
        self.schema = schema
        self.meta = json.loads((SCHEMAS_DIR / schema / "meta.json").read_text(encoding="utf-8"))
        self.data_dir = self.root / DOCUMENTS
        stores = self.meta["stores"]
        self.library_path = self.data_dir / "BKLibrary" / stores["BKLibrary"]["file"]
        self.annotation_path = self.data_dir / "AEAnnotation" / stores["AEAnnotation"]["file"]

    def __repr__(self) -> str:
        return f"FixtureLibrary(root={str(self.root)!r}, schema={self.schema!r})"

    @property
    def prefs_path(self) -> pathlib.Path:
        """Where Books keeps its preferences plist, beside ``Documents``:
        ``<root>/Library/Containers/com.apple.iBooksX/Data/Library/Preferences/com.apple.iBooksX.plist``.
        Written by :meth:`write_prefs`."""
        return self.data_dir.parent / PREFS_PLIST

    @property
    def book_info_dir(self) -> pathlib.Path:
        """Where Books keeps its per-book info caches, beside ``Documents``:
        ``<root>/Library/Containers/com.apple.iBooksX/Data/Library/Caches/AEEpubInfoSource``.
        Written by :meth:`add_book_info_cache`."""
        return self.data_dir.parent / BOOK_INFO_DIR

    @classmethod
    def create(cls, root, schema: str = DEFAULT_SCHEMA, journal_mode: str = "DELETE",
               store_uuids: Optional[Mapping[str, str]] = None) -> "FixtureLibrary":
        """Build both stores under ``root`` from ``schema``.

        Each store gets a fresh ``Z_METADATA.Z_UUID`` (Core Data's store
        identity) unless ``store_uuids`` maps its name (``'BKLibrary'``,
        ``'AEAnnotation'``) to a fixed one. The model version hashes in
        ``Z_PLIST`` are Apple's and are kept as-is.
        """
        lib = cls(root, schema)
        for store, path in (("BKLibrary", lib.library_path), ("AEAnnotation", lib.annotation_path)):
            build_store(SCHEMAS_DIR / schema / f"{store}.sql", path, journal_mode)
            store_uuid = (store_uuids or {}).get(store) or str(uuid.uuid4()).upper()
            con = sqlite3.connect(path, isolation_level=None)
            try:
                con.execute("UPDATE Z_METADATA SET Z_UUID = ?", (store_uuid,))
            finally:
                con.close()
        return lib

    # -- plumbing ---------------------------------------------------------

    def _path(self, store: str) -> pathlib.Path:
        if store not in _STORES:
            raise ValueError(f"store must be 'library' or 'annotations', not {store!r}")
        return self.library_path if store == "library" else self.annotation_path

    @staticmethod
    def _identifier(entity: str, pk: int, purpose: str) -> uuid.UUID:
        return uuid.uuid5(_ID_NAMESPACE, f"{entity}:{pk}:{purpose}")

    def _insert_many(self, store: str, table: str, entity: str, count: int, make_row) -> List[int]:
        """Insert ``count`` rows built by ``make_row(pk)`` in one transaction.

        Primary keys are allocated from ``Z_PRIMARYKEY.Z_MAX`` exactly as
        Core Data does, and ``Z_MAX`` is advanced past them.
        """
        con = sqlite3.connect(self._path(store), isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            ent, zmax = con.execute(
                "SELECT Z_ENT, Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME = ?", (entity,)).fetchone()
            pks = list(range(zmax + 1, zmax + 1 + count))
            rows = [{"Z_PK": pk, "Z_ENT": ent, "Z_OPT": 1, **make_row(pk)} for pk in pks]
            if rows:
                cols = list(rows[0])
                con.executemany(
                    f"INSERT INTO {table} ({', '.join(cols)}) "
                    f"VALUES ({', '.join(':' + c for c in cols)})",
                    rows,
                )
                con.execute("UPDATE Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_NAME = ?", (pks[-1], entity))
            con.execute("COMMIT")
            return pks
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def _insert(self, store: str, table: str, entity: str, make_row) -> int:
        return self._insert_many(store, table, entity, 1, make_row)[0]

    def execute(self, store: str, sql: str, params=()) -> list:
        """Run raw SQL against ``'library'`` or ``'annotations'`` (autocommit).

        For edge cases the helpers don't cover: odd column values,
        ``ALTER TABLE`` drift, row counts.
        """
        con = sqlite3.connect(self._path(store), isolation_level=None)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def reset(self) -> None:
        """Delete every row except Core Data bookkeeping and set ``Z_MAX`` to 0.

        Works in place, so connections already open on the stores (as
        py_apple_books <= 1.9 keeps them from import time) stay valid.
        """
        for store in _STORES:
            con = sqlite3.connect(self._path(store), isolation_level=None)
            try:
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                    "AND name NOT IN ('Z_PRIMARYKEY', 'Z_METADATA', 'Z_MODELCACHE')")]
                con.execute("BEGIN IMMEDIATE")
                for table in tables:
                    con.execute(f'DELETE FROM "{table}"')
                con.execute("UPDATE Z_PRIMARYKEY SET Z_MAX = 0")
                con.execute("COMMIT")
            finally:
                con.close()

    # -- books --------------------------------------------------------------

    def _book_row(self, pk: int, title, author, *, asset_id=None, path=None, genre=None,
                  progress=0.0, finished=False, last_opened=None, created=0.0, content_type=1,
                  data_source=UBIQUITY, can_redownload=None, state=1, finished_date=None,
                  last_engaged=None, columns=None, raw=None) -> dict:
        if can_redownload is None:
            # Unowned Store-series rows carry 0; every owned row carries 1.
            can_redownload = 0 if data_source == STORE_SERIES else 1
        # Columns added in 1.11 are only named when set, so a row built
        # with the defaults is exactly a 1.10 row (and inserts into a store
        # whose schema lacks them).
        optional = {}
        if finished_date is not None:
            optional["ZDATEFINISHED"] = core_data_time(finished_date)
        if last_engaged is not None:
            optional["ZLASTENGAGEDDATE"] = core_data_time(last_engaged)
        return {
            "ZASSETID": asset_id or self._identifier("BKLibraryAsset", pk, "asset").hex.upper(),
            "ZASSETGUID": str(self._identifier("BKLibraryAsset", pk, "guid")).upper(),
            "ZTITLE": title, "ZSORTTITLE": title, "ZAUTHOR": author, "ZSORTAUTHOR": author,
            "ZGENRE": genre, "ZCONTENTTYPE": content_type,
            "ZPATH": str(path) if path is not None else None,
            "ZREADINGPROGRESS": progress, "ZBOOKHIGHWATERMARKPROGRESS": progress,
            # NULL (not 0) when unfinished, as on real rows.
            "ZISFINISHED": 1 if finished else None, "ZFINISHEDDATEKIND": int(bool(finished)),
            "ZLASTOPENDATE": core_data_time(last_opened), "ZCREATIONDATE": core_data_time(created),
            "ZDATASOURCEIDENTIFIER": data_source, "ZCANREDOWNLOAD": can_redownload,
            "ZSTATE": state, "ZCOMBINEDSTATE": state,
            # 0, not NULL, on every row of the reference library.
            "ZISEPHEMERAL": 0, "ZISHIDDEN": 0, "ZISLOCKED": 0, "ZISSAMPLE": 0, "ZISPROOF": 0,
            "ZISDEVELOPMENT": 0, "ZRATING": 0, "ZDURATION": 0.0, "ZPAGECOUNT": 0, "ZSORTKEY": 0,
            "ZFILESIZE": 0, "ZGENERATION": 1, "ZDESKTOPSUPPORTLEVEL": 0,
            "ZDIDWARNABOUTDESKTOPSUPPORT": 0, "ZHASRACSUPPORT": 0, "ZTASTE": 0,
            "ZTASTESYNCEDTOSTORE": 0, "ZMAPPEDASSETCONTENTTYPE": 0, "ZVERSIONNUMBER": 0.0,
            **optional,
            **(columns or {}),
            **(raw or {}),
        }

    def add_book(self, title: str = "Synthetic Book", author: Optional[str] = "Test Author", *,
                 asset_id: Optional[str] = None, path=None, genre: Optional[str] = None,
                 progress: Optional[float] = 0.0, finished: bool = False, last_opened=None,
                 created=0.0, content_type: int = 1, data_source: Optional[str] = UBIQUITY,
                 can_redownload: Optional[int] = None, state: int = 1, finished_date=None,
                 last_engaged=None, raw: Optional[Mapping[str, Any]] = None) -> dict:
        """Insert a ``ZBKLIBRARYASSET`` row and return ``{'id', 'asset_id'}``.

        ``progress`` is a 0..1 fraction, as Books stores it. Dates accept
        a datetime (naive means UTC) or Core Data seconds.
        ``finished_date`` and ``last_engaged`` set ``ZDATEFINISHED`` and
        ``ZLASTENGAGEDDATE`` (``Book.finished_date``,
        ``Book.last_engaged_date``); a finish date doesn't mark the book
        finished (``finished=True`` does), so stray dates can be tested.
        ``can_redownload`` defaults to 1, or to 0 for Store-series rows
        (``data_source=STORE_SERIES``), matching real data. ``raw``
        overrides or adds columns verbatim.
        """
        fields = dict(asset_id=asset_id, path=path, genre=genre, progress=progress,
                      finished=finished, last_opened=last_opened, created=created,
                      content_type=content_type, data_source=data_source,
                      can_redownload=can_redownload, state=state, finished_date=finished_date,
                      last_engaged=last_engaged, raw=raw)
        row = self._insert_book(title, author, fields)
        return {"id": row["Z_PK"], "asset_id": row["ZASSETID"]}

    def _insert_book(self, title, author, fields: Mapping[str, Any],
                     columns: Optional[Callable[[int], Mapping[str, Any]]] = None) -> dict:
        """Insert one book row and return it (with ``Z_PK``); ``columns(pk)``
        adds columns, applied before ``raw``."""
        rows = {}

        def make_row(pk):
            extra = columns(pk) if columns is not None else None
            rows[pk] = self._book_row(pk, title, author, columns=extra, **fields)
            return rows[pk]

        pk = self._insert("library", "ZBKLIBRARYASSET", "BKLibraryAsset", make_row)
        return {"Z_PK": pk, **rows[pk]}

    # -- series -----------------------------------------------------------

    _VOLUME_KEYS = frozenset({"title", "sequence", "label", "store_id"})
    _BOOK_KEYS = frozenset({"author", "asset_id", "path", "genre", "progress", "finished",
                            "last_opened", "created", "content_type", "data_source",
                            "can_redownload", "state", "finished_date", "last_engaged", "raw"})

    def add_series(self, title: str, volumes: Sequence[Mapping[str, Any]], *, ordered: bool = True,
                   store_id: Optional[str] = None) -> dict:
        """Insert a Store series: a container row and its volumes.

        Books lists a Store series as a container (``ZCONTENTTYPE`` 5,
        Series data source, ``ZSERIESID`` equal to its own ``ZSTOREID``,
        ``ZSERIESISORDERED`` from ``ordered``) and one Series-source row
        per volume, linked to it by both ``ZSERIESCONTAINER`` (the
        container's row id) and ``ZSERIESID``. ``store_id`` is the
        container's (and so the series') Store id; by default a numeric
        id is derived from the row id.

        Each volume is a mapping with optional ``title`` (default
        ``'<title> <n>'``), ``sequence`` (``ZSEQUENCENUMBER``, a number,
        or a str written verbatim), ``label`` (``ZSEQUENCEDISPLAYNAME``,
        e.g. ``'Book 2'``) and ``store_id`` (default derived from the row
        id), plus any :meth:`add_book` keyword. Volumes default to
        unowned (``can_redownload`` 0), like the Store volumes Books lists
        for a series you've started; ``can_redownload=1`` makes one the
        library lists as owned. ``state`` defaults to 5 for unowned rows
        and the container, 1 for owned ones. A volume's ``raw`` is applied
        last, so it can unlink or garble any series column.

        Returns ``{'container': {'id', 'asset_id', 'store_id'}, 'volumes':
        [{'id', 'asset_id', 'store_id'}, ...]}`` (each ``store_id`` as
        stored, after ``raw``).
        """
        specs = []
        for i, volume in enumerate(volumes):
            unknown = sorted(set(volume) - self._VOLUME_KEYS - self._BOOK_KEYS)
            if unknown:
                raise ValueError(f"volumes[{i}]: unknown keys {unknown}")
            specs.append(dict(volume))

        def ids(row) -> dict:
            return {"id": row["Z_PK"], "asset_id": row["ZASSETID"], "store_id": row.get("ZSTOREID")}

        def own_store_id(pk: int, given: Optional[str]) -> str:
            return given if given is not None else str(_STORE_ID_BASE + pk)

        def container_columns(pk):
            sid = own_store_id(pk, store_id)
            return {"ZSTOREID": sid, "ZSERIESID": sid, "ZSERIESISORDERED": int(bool(ordered))}

        container_row = self._insert_book(title, "Test Author", dict(
            content_type=5, data_source=STORE_SERIES, state=5), container_columns)
        container = ids(container_row)
        series_id = container_row["ZSERIESID"]

        made = []
        for n, spec in enumerate(specs, start=1):
            fields = {k: v for k, v in spec.items() if k in self._BOOK_KEYS and k != "author"}
            fields.setdefault("data_source", STORE_SERIES)
            if fields.get("can_redownload") is None:
                fields["can_redownload"] = 0 if fields["data_source"] == STORE_SERIES else 1
            fields.setdefault("state", 1 if fields["can_redownload"] else 5)

            def volume_columns(pk, spec=spec):
                return {"ZSTOREID": own_store_id(pk, spec.get("store_id")), "ZSERIESID": series_id,
                        "ZSERIESCONTAINER": container["id"], "ZSEQUENCENUMBER": spec.get("sequence"),
                        "ZSEQUENCEDISPLAYNAME": spec.get("label")}

            made.append(ids(self._insert_book(spec.get("title", f"{title} {n}"),
                                              spec.get("author", "Test Author"), fields, volume_columns)))
        return {"container": container, "volumes": made}

    # -- collections ------------------------------------------------------

    def add_collection(self, title: str, *, collection_id: Optional[str] = None,
                       deleted: bool = False, hidden: bool = False, sort_key: Optional[int] = None,
                       raw: Optional[Mapping[str, Any]] = None) -> dict:
        """Insert a ``ZBKCOLLECTION`` row and return ``{'id', 'collection_id'}``.

        ``deleted=True`` makes the soft-deleted tombstone Books keeps for
        iCloud sync. User collections get upper-case UUID ids, like the
        ones Books creates.
        """
        ids = {}

        def make_row(pk):
            ids[pk] = collection_id or str(self._identifier("BKCollection", pk, "id")).upper()
            return {
                "ZDELETEDFLAG": int(deleted), "ZHIDDEN": int(hidden), "ZPLACEHOLDER": 0,
                "ZSORTKEY": sort_key if sort_key is not None else 10000 * pk,
                "ZSORTMODE": 6, "ZLASTMODIFICATION": _COLLECTION_MODIFIED,
                "ZLOCALMODDATE": _COLLECTION_MODIFIED, "ZCOLLECTIONID": ids[pk], "ZTITLE": title,
                **(raw or {}),
            }

        pk = self._insert("library", "ZBKCOLLECTION", "BKCollection", make_row)
        return {"id": pk, "collection_id": ids[pk]}

    def add_to_collection(self, collection: Mapping, book: Mapping, *,
                          raw: Optional[Mapping[str, Any]] = None) -> int:
        """Add ``book`` (from :meth:`add_book`) to ``collection``; returns the member Z_PK."""
        return self._insert("library", "ZBKCOLLECTIONMEMBER", "BKCollectionMember", lambda pk: {
            "ZCOLLECTION": collection["id"], "ZASSET": book["id"], "ZASSETID": book["asset_id"],
            "ZSORTKEY": 10000, **(raw or {})})

    def seed_system_collections(self) -> Dict[str, dict]:
        """Insert Books' 8 built-in collections; returns ``{collection_id: row}``."""
        out = {}
        for cid, title in SYSTEM_COLLECTIONS.items():
            sort_key, sort_mode = _SYSTEM_COLLECTION_ORDER[cid]
            out[cid] = self.add_collection(title, collection_id=cid, sort_key=sort_key,
                                           raw={"ZSORTMODE": sort_mode})
        return out

    # -- annotations ------------------------------------------------------

    def _annotation_row(self, pk: int, book, text, *, kind="highlight", note=None, color="yellow",
                        deleted=False, created=0.0, modified=None, location=None, chapter=None,
                        range_start=0, user_data=None, position_fraction=None,
                        furthest_fraction=None, raw=None) -> dict:
        if kind not in ANNOTATION_KINDS:
            raise ValueError(f"kind must be one of {sorted(ANNOTATION_KINDS)}, not {kind!r}")
        if color not in COLORS:
            raise ValueError(f"color must be one of {sorted(COLORS)}, not {color!r}")
        ztype, overrides = ANNOTATION_KINDS[kind]
        if isinstance(book, Mapping):
            asset_id = book["asset_id"]
        else:
            asset_id = book if book is not None else ""
        is_text = ztype == 2
        if kind == "note" and note is None:
            note = "a synthetic note"
        if kind == "tombstone":
            modification = core_data_time(modified)
        else:
            modification = core_data_time(modified if modified is not None else created)
        return {
            "ZANNOTATIONASSETID": asset_id,
            "ZANNOTATIONTYPE": ztype, "ZANNOTATIONDELETED": int(bool(deleted)),
            "ZANNOTATIONISUNDERLINE": 0, "ZANNOTATIONSTYLE": COLORS[color] if is_text else 0,
            "ZANNOTATIONSELECTEDTEXT": text if is_text else None,
            "ZANNOTATIONREPRESENTATIVETEXT": text if is_text else None,
            "ZANNOTATIONNOTE": note,
            "ZANNOTATIONCREATIONDATE": core_data_time(created),
            "ZANNOTATIONMODIFICATIONDATE": modification,
            "ZANNOTATIONLOCATION": location, "ZFUTUREPROOFING5": chapter,
            "ZANNOTATIONUUID": str(self._identifier("AEAnnotation", pk, "uuid")).upper(),
            "ZANNOTATIONCREATORIDENTIFIER": _CREATOR,
            "ZPLLOCATIONRANGESTART": range_start, "ZPLLOCATIONRANGEEND": 0,
            "ZPLABSOLUTEPHYSICALLOCATION": 0,
            **overrides, **self._position_columns(user_data, position_fraction, furthest_fraction),
            **(raw or {}),
        }

    @staticmethod
    def _position_columns(user_data, position_fraction, furthest_fraction) -> dict:
        # Only named when set, so a default row is exactly a 1.10 row.
        columns = {}
        if user_data is not None:
            if not isinstance(user_data, (bytes, bytearray, memoryview)):
                raise TypeError(f"user_data must be bytes or None, not {type(user_data).__name__}")
            columns["ZPLUSERDATA"] = bytes(user_data)
        for column, name, value in (("ZFUTUREPROOFING10", "position_fraction", position_fraction),
                                    ("ZFUTUREPROOFING8", "furthest_fraction", furthest_fraction)):
            if value is not None:
                columns[column] = _fraction_text(value, name)
        return columns

    def add_annotation(self, book: Union[Mapping, str, None], text: Optional[str] = "a synthetic highlight", *,
                       kind: str = "highlight", note: Optional[str] = None, color: str = "yellow",
                       deleted: bool = False, created=0.0, modified=None, location: Optional[str] = None,
                       chapter: Optional[str] = None, range_start: Optional[int] = 0,
                       user_data: Optional[bytes] = None,
                       position_fraction: Union[float, str, None] = None,
                       furthest_fraction: Union[float, str, None] = None,
                       raw: Optional[Mapping[str, Any]] = None) -> int:
        """Insert a ``ZAEANNOTATION`` row and return its Z_PK.

        ``book`` is a row from :meth:`add_book`, a bare asset id (an id
        no book has makes an orphan), or None. ``kind`` is one of
        :data:`ANNOTATION_KINDS`: highlight and note are type 2 (a note
        carries a note body), underline is type 2 with style 0 and the
        underline flag, bookmark type 1, reading_position type 3, and
        tombstone type 0 with no asset, dates or location. ``text`` only
        applies to type-2 kinds. ``location`` is the EPUB CFI.

        For every kind, ``user_data`` sets ``ZPLUSERDATA`` (Books' own
        position record, e.g. :func:`page_location_blob`), and
        ``position_fraction`` / ``furthest_fraction`` set
        ``ZFUTUREPROOFING10`` / ``ZFUTUREPROOFING8`` (where the reader is
        and the furthest point read, 0..1). Books stores the fractions as
        text: a number is written as its shortest ``repr`` (``0.5`` as
        ``'0.5'``), a str verbatim (so garbage can be tested).
        """
        fields = dict(kind=kind, note=note, color=color, deleted=deleted, created=created,
                      modified=modified, location=location, chapter=chapter,
                      range_start=range_start, user_data=user_data,
                      position_fraction=position_fraction, furthest_fraction=furthest_fraction,
                      raw=raw)
        # Validate before opening a write transaction.
        self._annotation_row(0, book, text, **fields)
        return self._insert("annotations", "ZAEANNOTATION", "AEAnnotation",
                            lambda pk: self._annotation_row(pk, book, text, **fields))

    # -- Books' preferences plist -------------------------------------------

    def write_prefs(self, *, books_goal: Any = 3, books_goal_set=None, daily_goal_seconds: Any = 300.0,
                    daily_goal_set=None, current_streak: Any = 0,
                    finished: Optional[Mapping[str, Any]] = None,
                    extra: Optional[Mapping[str, Any]] = None, year_zero_date: bool = True,
                    fmt: str = "binary") -> pathlib.Path:
        """Write a synthetic Books preferences plist to :attr:`prefs_path`
        and return the path.

        The keys Books uses for reading goals:

        - ``ReadingGoals.BooksFinished``: ``{'goal': books_goal, 'date':
          books_goal_set}``, the yearly books goal and when it was set;
        - ``ReadingGoals.StreakDay``: ``{'goal': daily_goal_seconds,
          'date': daily_goal_set}``, the daily reading goal in seconds;
        - ``ReadingHistory.CurrentStreak``: ``current_streak``;
        - ``BKFinishedAssetsCache``: ``finished``, asset id -> finish date;
        - ``BKMostRecentPurchaseDateKey``: a year-0 date
          (:data:`YEAR_ZERO`) when ``year_zero_date``. Books writes one,
          and it makes plain ``plistlib.loads`` reject the whole file.

        The default goals (3 books a year, 300 seconds a day) are
        arbitrary synthetic values. A None ``books_goal``,
        ``daily_goal_seconds``, ``current_streak`` or ``finished`` leaves
        its key out. Values are written as given, so wrong types can be
        tested (``books_goal='3'``). ``extra`` adds or replaces top-level
        keys, last; its values are plain plist values.

        Dates (``books_goal_set``, ``daily_goal_set``, the values of
        ``finished``) are datetimes (naive means UTC), dates (midnight
        UTC) or Core Data seconds (int or float), written as dates
        exactly, so :data:`YEAR_ZERO`, ``float('nan')`` and other values
        ``datetime`` can't hold give the unreadable dates found in real
        files. A ``*_set`` of None is a fixed date in January 2026.

        ``fmt`` is ``'binary'`` (what Books writes) or ``'xml'``; an XML
        plist can hold no unreadable date but :data:`YEAR_ZERO`.
        """
        import plistlib  # on first use, not at import

        if fmt not in ("binary", "xml"):
            raise ValueError(f"fmt must be 'binary' or 'xml', not {fmt!r}")
        epoch = _dt.datetime(2001, 1, 1)
        patches = []  # (placeholder datetime, Core Data seconds to write instead)

        def date(value):
            if isinstance(value, _dt.datetime):
                return value.astimezone(_dt.timezone.utc).replace(tzinfo=None) if value.tzinfo else value
            if isinstance(value, _dt.date):
                return _dt.datetime(value.year, value.month, value.day)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"a date must be a datetime or Core Data seconds, not {type(value).__name__}")
            seconds = float(value)
            if fmt == "xml" and seconds != YEAR_ZERO:
                try:
                    return epoch + _dt.timedelta(seconds=seconds)
                except (OverflowError, ValueError):
                    raise ValueError(f"an XML plist can't hold the date {seconds!r}; "
                                     f"use fmt='binary'") from None
            # A unique placeholder date, swapped for the raw value once
            # encoded (a collision with a date in the plist is refused).
            n = len(patches) + 1
            placeholder = (epoch + _dt.timedelta(microseconds=n) if fmt == "binary"
                           else _dt.datetime(1, 1, 1) + _dt.timedelta(seconds=n))
            patches.append((placeholder, seconds))
            return placeholder

        doc: Dict[str, Any] = {}
        if books_goal is not None:
            doc["ReadingGoals.BooksFinished"] = {
                "goal": books_goal,
                "date": date(_dt.datetime(2026, 1, 2, 9, 0) if books_goal_set is None else books_goal_set)}
        if daily_goal_seconds is not None:
            doc["ReadingGoals.StreakDay"] = {
                "goal": daily_goal_seconds,
                "date": date(_dt.datetime(2026, 1, 3, 9, 0) if daily_goal_set is None else daily_goal_set)}
        if current_streak is not None:
            doc["ReadingHistory.CurrentStreak"] = current_streak
        if finished is not None:
            doc["BKFinishedAssetsCache"] = {asset: date(when) for asset, when in finished.items()}
        if year_zero_date:
            doc["BKMostRecentPurchaseDateKey"] = date(YEAR_ZERO)
        doc.update(extra or {})

        dates = [v for v in _plist_values(doc) if isinstance(v, _dt.datetime)]
        if any(dates.count(placeholder) != 1 for placeholder, _ in patches):
            raise ValueError("a date in the plist collides with write_prefs' placeholders; use another date")
        if fmt == "binary":
            data = plistlib.dumps(doc, fmt=plistlib.FMT_BINARY)
            for placeholder, seconds in patches:
                encoded = b"\x33" + struct.pack(">d", (placeholder - epoch).total_seconds())
                if data.count(encoded) != 1:  # pragma: no cover - guarded above
                    raise ValueError("could not place a raw date in the plist")
                data = data.replace(encoded, b"\x33" + struct.pack(">d", seconds))
        else:
            data = plistlib.dumps(doc, fmt=plistlib.FMT_XML)
            for placeholder, _ in patches:
                p = placeholder  # plistlib's own format (strftime's %Y varies below 1000)
                encoded = (f"<date>{p.year:04d}-{p.month:02d}-{p.day:02d}T"
                           f"{p.hour:02d}:{p.minute:02d}:{p.second:02d}Z</date>").encode()
                if data.count(encoded) != 1:  # pragma: no cover - guarded above
                    raise ValueError("could not place a raw date in the plist")
                data = data.replace(encoded, f"<date>{_YEAR_ZERO_XML}</date>".encode())
        path = self.prefs_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    # -- Books' per-book info caches ------------------------------------------

    # add_book_info_cache row keys -> ZAEBOOKINFO columns.
    BOOK_INFO_COLUMNS = {"asset_id": "ZDATABASEKEY", "title": "ZBOOKTITLE", "author": "ZBOOKAUTHOR",
                         "language": "ZBOOKLANGUAGE", "publisher": "ZPUBLISHERNAME",
                         "year": "ZPUBLISHERYEAR", "deleted": "ZDELETEDFLAG"}
    _JOURNAL_MODES = frozenset({"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"})

    def _book_info_sql(self) -> pathlib.Path:
        """This schema's ``AEBookInfo.sql``, else the newest fixture's."""
        for name in [self.schema, *reversed(available_schemas())]:
            path = SCHEMAS_DIR / name / "AEBookInfo.sql"
            if path.is_file():
                return path
        raise FileNotFoundError(f"no schema fixture under {SCHEMAS_DIR} has AEBookInfo.sql")

    def add_book_info_cache(self, rows: Iterable[Mapping[str, Any]], *, version: str = "v20250715-26.7",
                            columns: Optional[Sequence[str]] = None,
                            journal_mode: str = "WAL") -> pathlib.Path:
        """Write a Books per-book info cache and return its path,
        ``book_info_dir / f'AEBookInfo-{version}.sqlite'``.

        Books keeps what it parsed from each book (title, author,
        language, publisher...) in these caches, keyed by the book's asset
        id, and keeps rows for books since removed from the library. The
        ``ZAEBOOKINFO`` table and its indexes come from the schema
        fixture's ``AEBookInfo.sql`` (the newest fixture that has one if
        this library's doesn't).

        Each row is a mapping with optional keys ``asset_id``
        (``ZDATABASEKEY``), ``title``, ``author``, ``language``,
        ``publisher``, ``year`` (``ZPUBLISHERYEAR``, a text column) and
        ``deleted`` (``ZDELETEDFLAG``, default 0), plus ``raw`` (column ->
        value, verbatim, applied last). Values are written as given. Rows
        get ``Z_PK`` 1, 2, ... in order, so later rows are newer.

        ``columns`` keeps only the named columns (and ``Z_PK``) and the
        indexes on them, to simulate drift; values for dropped columns
        are ignored. ``journal_mode='WAL'`` (what Books uses) leaves the
        file closed cleanly, with no ``-wal`` or ``-shm``, like a cache
        Books isn't using; any other SQLite journal mode works too.

        Raises ValueError for an unknown row key, column name or journal
        mode, and FileExistsError if the file exists (a test changes an
        existing cache with ``sqlite3`` directly).
        """
        if not isinstance(version, str) or not version or "/" in version or "\x00" in version:
            raise ValueError("version must be a non-empty file-name part")
        mode = str(journal_mode).upper()
        if mode not in self._JOURNAL_MODES:
            raise ValueError(f"journal_mode must be one of {sorted(self._JOURNAL_MODES)}, not {journal_mode!r}")
        ddl = self._book_info_sql().read_text(encoding="utf-8")
        schema = sqlite3.connect(":memory:")
        try:
            schema.executescript(ddl)
            table = [(r[1], r[2], r[5]) for r in schema.execute("PRAGMA table_info(ZAEBOOKINFO)")]
            names = [name for name, _, _ in table]
            indexes = [(name, sql, [r[2] for r in schema.execute(f'PRAGMA index_info("{name}")')])
                       for name, sql in schema.execute(
                           "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
                           "AND tbl_name = 'ZAEBOOKINFO' AND sql IS NOT NULL ORDER BY rowid")]
        finally:
            schema.close()
        if columns is not None:
            if isinstance(columns, str):
                raise ValueError("columns must be a sequence of column names, not a str")
            unknown = sorted(set(columns) - set(names))
            if unknown:
                raise ValueError(f"no such ZAEBOOKINFO columns: {unknown}")
            keep = {"Z_PK", *columns}
            defs = ", ".join(f"{name} {kind}{' PRIMARY KEY' if pk else ''}".rstrip()
                             for name, kind, pk in table if name in keep)
            ddl = f"CREATE TABLE ZAEBOOKINFO ( {defs} );\n" + "".join(
                f"{sql};\n" for _, sql, cols in indexes if set(cols) <= keep)
            names = [name for name in names if name in keep]

        prepared = []
        for i, row in enumerate(rows):
            unknown = sorted(set(row) - set(self.BOOK_INFO_COLUMNS) - {"raw"})
            if unknown:
                raise ValueError(f"rows[{i}]: unknown keys {unknown}")
            raw = dict(row.get("raw") or {})
            bad = sorted(set(raw) - {name for name, _, _ in table})
            if bad:
                raise ValueError(f"rows[{i}]: no such ZAEBOOKINFO columns: {bad}")
            values = {"Z_PK": i + 1, "Z_ENT": 1, "Z_OPT": 1, "ZDELETEDFLAG": 0}
            values.update({self.BOOK_INFO_COLUMNS[k]: v for k, v in row.items() if k != "raw"})
            values.update(raw)
            prepared.append({k: v for k, v in values.items() if k in names})

        folder = self.book_info_dir
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"AEBookInfo-{version}.sqlite"
        if path.exists():
            raise FileExistsError(f"{path.name} already exists")
        con = sqlite3.connect(path, isolation_level=None)
        try:
            con.execute(f"PRAGMA journal_mode={mode}")
            con.executescript(ddl)
            con.execute("BEGIN")
            for values in prepared:
                cols = list(values)
                con.execute(f"INSERT INTO ZAEBOOKINFO ({', '.join(cols)}) "
                            f"VALUES ({', '.join('?' for _ in cols)})", [values[c] for c in cols])
            con.execute("COMMIT")
        finally:
            con.close()
        return path

    # -- bulk -------------------------------------------------------------

    def populate(self, books: int = 10, annotations_per_book: int = 10) -> dict:
        """Bulk-insert ``books`` books with ``annotations_per_book`` highlights each.

        Uses one ``executemany`` per store, so it scales to hundreds of
        thousands of annotations. Content is deterministic: books cycle
        through unstarted, in-progress and finished; annotations cycle
        through the five colours with ascending creation dates. Returns
        ``{'books': [{'id', 'asset_id'}, ...], 'annotations': count}``.
        """
        colors = list(COLORS)
        book_rows = {}

        def make_book(pk):
            n = pk
            status = n % 3  # 0 unstarted, 1 in progress, 2 finished
            book_rows[pk] = self._book_row(
                pk, f"Populated Book {n}", f"Author {n % 7}",
                genre=("Fiction", "History", "Science")[n % 3],
                progress=(0.0, 0.5, 1.0)[status], finished=status == 2,
                last_opened=None if status == 0 else 700000000.0 + n * 3600,
                created=600000000.0 + n * 86400)
            return book_rows[pk]

        pks = self._insert_many("library", "ZBKLIBRARYASSET", "BKLibraryAsset", books, make_book)
        made = [{"id": pk, "asset_id": book_rows[pk]["ZASSETID"]} for pk in pks]
        count = len(made) * annotations_per_book

        def make_annotation(pk):
            book = made[(pk - 1) % len(made)]
            return self._annotation_row(
                pk, book, f"synthetic highlight {pk} about the theme of book {book['id']}",
                color=colors[pk % len(colors)], created=700000000.0 + pk * 60,
                range_start=pk)

        if count:
            self._insert_many("annotations", "ZAEANNOTATION", "AEAnnotation", count, make_annotation)
        return {"books": made, "annotations": count}
