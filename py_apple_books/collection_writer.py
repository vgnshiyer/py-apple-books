"""Write operations for Apple Books collections.

Apple exposes no automation surface for collections (no AppleScript
dictionary, no Shortcuts action), so the only path is writing directly
to the library's Core Data SQLite store. That demands discipline; every
operation here runs inside a :class:`WriteSession` that:

1. refuses while the Books app is running,
2. takes a WAL-inclusive backup (on by default),
3. wraps all statements in one ``BEGIN IMMEDIATE`` transaction (a
   write lock held elsewhere past :data:`BUSY_TIMEOUT` raises
   :class:`LibraryBusyError`),
4. inside that transaction, validates the schema and aborts on drift:
   the collection and membership tables must have exactly the verified
   columns and types, the tables it reads must have the columns it
   reads, and the Core Data model hashes are compared with the
   verified ones (``APPLE_BOOKS_MODEL_CHECK``; see
   :mod:`py_apple_books.write_safety`), and
5. maintains Core Data's bookkeeping invariants — primary keys are
   allocated through ``Z_PRIMARYKEY`` (skipping this breaks Books' own
   next insert), ``Z_ENT``/``Z_OPT`` are set the way Books sets them,
   timestamps use the Core Data epoch, and sort keys follow Books'
   multiples-of-10000 convention.

Scope is deliberately narrow: user-created collections can be created,
renamed, and (soft-)deleted; membership can be edited on user
collections plus "Want to Read" — the one built-in collection whose
membership the user edits in the app. The auto-managed built-ins
(Books, PDFs, Library, Downloaded, …) are refused outright.

Known limitation, by design: Books tracks iCloud sync state in a
separate versions table these writes don't touch, so with collection
sync enabled an edit may not propagate to other devices and could be
reverted by a cloud re-sync. Callers should surface that caveat.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

from py_apple_books.db.client import AppleBooksDBClient, _store, default_data_dir, find_sqlite_file
from py_apple_books.exceptions import (
    AmbiguousStoreError,
    BookNotFoundError,
    CollectionNotFoundError,
    LibraryBusyError,
    LibraryNotFoundError,
    SchemaValidationError,
    SystemCollectionError,
    WriteError,
)
from py_apple_books.utils import APPLE_EPOCH_OFFSET
from py_apple_books.write_safety import (
    backup_library,
    check_model_hashes,
    ensure_books_not_running,
    resolve_model_check,
    validate_table_columns,
    validate_table_schema,
)

_COLLECTION_TABLE = "ZBKCOLLECTION"
_MEMBER_TABLE = "ZBKCOLLECTIONMEMBER"
_ASSET_TABLE = "ZBKLIBRARYASSET"
_PK_TABLE = "Z_PRIMARYKEY"

_COLLECTION_ENTITY = "BKCollection"
_MEMBER_ENTITY = "BKCollectionMember"

#: SQLite busy timeout, in seconds, for taking the write lock. If
#: another program holds it longer, the write gives up with
#: :class:`LibraryBusyError` and changes nothing.
BUSY_TIMEOUT = 5.0

#: Books assigns sidebar / in-collection order in multiples of 10000.
SORT_KEY_STEP = 10000

#: ZSORTMODE value observed on every user collection.
_DEFAULT_SORT_MODE = 6

#: Sentinel ``ZCOLLECTIONID`` values of Apple's built-in collections.
#: User-created collections carry an uppercase UUID instead.
SYSTEM_COLLECTION_IDS = frozenset(
    {
        "All_Collection_ID",
        "AudioBooks_Collection_ID",
        "Books_Collection_ID",
        "Downloaded_Collection_ID",
        "Finished_Collection_ID",
        "Pdfs_Collection_ID",
        "Samples_Collection_ID",
        "Want_To_Read_Collection_ID",
    }
)

#: Built-in collections whose *membership* the user edits in the app UI.
MEMBERSHIP_EDITABLE_SYSTEM_IDS = frozenset({"Want_To_Read_Collection_ID"})

# Exact schema (column -> declared type, as ``PRAGMA table_info``
# reports it) of the tables the writer inserts into, verified on macOS
# 26.7 / Books 8.5. Every INSERT populates every one of these columns;
# any other column is an attribute the writer would leave NULL, so the
# live tables must match exactly before any write.
_COLLECTION_SCHEMA = {
    "Z_PK": "INTEGER", "Z_ENT": "INTEGER", "Z_OPT": "INTEGER",
    "ZDELETEDFLAG": "INTEGER", "ZHIDDEN": "INTEGER", "ZPLACEHOLDER": "INTEGER",
    "ZSORTKEY": "INTEGER", "ZSORTMODE": "INTEGER", "ZVIEWMODE": "INTEGER",
    "ZLASTMODIFICATION": "TIMESTAMP", "ZLOCALMODDATE": "TIMESTAMP",
    "ZCOLLECTIONID": "VARCHAR", "ZDETAILS": "VARCHAR", "ZTITLE": "VARCHAR",
}
_MEMBER_SCHEMA = {
    "Z_PK": "INTEGER", "Z_ENT": "INTEGER", "Z_OPT": "INTEGER",
    "ZSORTKEY": "INTEGER", "ZASSET": "INTEGER", "ZCOLLECTION": "INTEGER",
    "ZLOCALMODDATE": "TIMESTAMP", "ZASSETID": "VARCHAR",
    "ZTEMPORARYASSETID": "VARCHAR",
}
_COLLECTION_COLUMNS = frozenset(_COLLECTION_SCHEMA)
_MEMBER_COLUMNS = frozenset(_MEMBER_SCHEMA)

# Tables the writer only reads (or bumps a counter in): the columns it
# uses must exist; other columns are fine.
_ASSET_READ_COLUMNS = frozenset({"Z_PK", "ZASSETID"})
_PK_COLUMNS = frozenset({"Z_ENT", "Z_NAME", "Z_MAX"})

#: Core Data ``NSStoreModelVersionHashes`` (base64) the writer was
#: verified against, for the entities it inserts into or updates.
#: BKLibraryAsset isn't pinned: it changes with most Books releases and
#: the writer only reads Z_PK and ZASSETID from it. Observed on macOS
#: 26.7 / Books 8.5 (NSPersistenceFrameworkVersion 1526).
_VERIFIED_MODEL_HASHES = {
    _COLLECTION_ENTITY: frozenset({"SNZFrt9vtP7OHwxpdgQvjG0aDQCjcaPKMvV4xi2f4wY="}),
    _MEMBER_ENTITY: frozenset({"iyiO3gHrQVAI21IxwV8Cp2jsmVbWVAixNI4/dJrLPnc="}),
}


def _cd_now() -> float:
    """Current time as a Core Data timestamp (seconds since 2001-01-01)."""
    return time.time() - APPLE_EPOCH_OFFSET


def _store_for_writes(db, *, unreadable_ok: bool = False) -> Path:
    """The library store file the writes to ``db`` (a
    :class:`~py_apple_books.db.LibraryDB`) go to.

    Found strictly (:meth:`LibraryDB.library_path`), and only if it is
    the file ``db``'s reads use. A store found in a folder, not given as
    a file, must be the canonical one or, if there is no canonical file,
    a single store with Apple's generation-stamped name: a canonical file
    that fails validation for a moment (busy, say) doesn't send the write
    to a copy, a ``.old`` file or a backup next to it, nor is it reported
    as missing. Otherwise :class:`AmbiguousStoreError`, before anything is
    opened for writing.

    ``unreadable_ok`` (restoring over a damaged library, listing its
    backups): a canonical file that is present but fails validation is
    the store, unless strict discovery found several other candidates.
    """
    store = _store("library")
    folder = f"{store.subdir}/"
    store_file, data_dir = db._source("library")
    unreadable = (f"The Apple Books library store {store.canonical} in {folder} can't be read "
                  "right now (busy or damaged)")
    try:
        path = db.library_path(strict=True)
    except LibraryNotFoundError:
        # Strict discovery treats a canonical file that fails validation
        # as absent: with nothing else there, "no store found".
        directory = (data_dir if data_dir is not None else default_data_dir()) / store.subdir
        canonical = directory / store.canonical
        if store_file is None and os.path.lexists(canonical):
            if unreadable_ok and os.path.isfile(canonical):
                return canonical
            raise AmbiguousStoreError(
                f"{unreadable}. Nothing was changed; try again in a moment.") from None
        raise
    if store_file is None and path.name != store.canonical:
        canonical = path.parent / store.canonical
        if os.path.lexists(canonical):
            if unreadable_ok and os.path.isfile(canonical):
                return canonical
            raise AmbiguousStoreError(
                f"{unreadable}, so the write won't go to {path.name} instead. "
                "Nothing was changed; try again in a moment.")
        if not store.generation.fullmatch(path.name):
            raise AmbiguousStoreError(
                f"The only library store in {folder} is {path.name}, which isn't named like "
                "the store Apple Books uses (a copy or a backup?); refusing to write to it.")
    read = db.paths().library
    try:
        same = os.path.samefile(path, read)
    except OSError:
        same = False
    if not same:
        raise AmbiguousStoreError(
            f"The library store found for this write ({path.name}) isn't the file being read "
            f"({read.name}): the store's location changed while in use. Nothing was changed.")
    return path


def _default_db_path(*, unreadable_ok: bool = False) -> Path:
    """The default library's store file (:func:`default_library`, which
    honours ``APPLE_BOOKS_LIBRARY_DB`` and then ``APPLE_BOOKS_DATA_DIR``),
    found strictly (see :func:`_store_for_writes`): several candidate
    stores raise :class:`AmbiguousStoreError` instead of a guess."""
    from py_apple_books.db.client import default_library
    return _store_for_writes(default_library(), unreadable_ok=unreadable_ok)


class WriteSession:
    """One guarded transaction against the Books library database.

    Context manager: guards run on ``__enter__``, the transaction
    commits on clean exit and rolls back on any exception.

    :param db_path: The library database to write. None means the
        default library's (``PyAppleBooks()``); a ``PyAppleBooks`` with
        a library of its own passes that library's store.
    :param backup: Take a pre-write backup. On by default; only tests
        should turn this off.
    :param backup_dir: Override the backup directory. By default the
        current user's library backs up into
        :data:`~py_apple_books.write_safety.BACKUP_DIR` and any other
        store into a folder of its own under it (see
        :func:`~py_apple_books.write_safety.backup_library`).
    :param require_books_closed: Refuse when Books.app is running. On
        by default; only tests against fixture databases turn this off.
    :param model_check: ``'warn'``, ``'enforce'`` or ``'off'``: what an
        unverified Core Data model hash does, and (``'off'``) whether
        unknown nullable columns are allowed. None reads
        ``APPLE_BOOKS_MODEL_CHECK`` (default ``'warn'``); see
        :func:`py_apple_books.write_safety.resolve_model_check`.

    After ``__enter__``, ``model_hashes`` holds the store's model hashes
    for the verified entities (``{}`` when the model check is off).
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        backup: bool = True,
        backup_dir: Optional[Path] = None,
        require_books_closed: bool = True,
        model_check: Optional[str] = None,
    ):
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self.backup = backup
        self.backup_dir = backup_dir
        self.require_books_closed = require_books_closed
        self.model_check = model_check
        self.backup_path: Optional[Path] = None
        self.model_hashes: dict = {}
        self.conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "WriteSession":
        if self.require_books_closed:
            ensure_books_not_running()
        if self.backup:
            from py_apple_books.write_safety import BACKUP_MIN_INTERVAL
            self.backup_path = backup_library(
                self.db_path, self.backup_dir, min_interval=BACKUP_MIN_INTERVAL
            )

        # isolation_level=None -> autocommit off our hands; we control
        # the transaction explicitly. timeout is SQLite's busy timeout:
        # either we get the write lock promptly or we abort.
        self.conn = sqlite3.connect(
            self.db_path, timeout=BUSY_TIMEOUT, isolation_level=None
        )
        try:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as e:
                message = str(e).lower()
                if "locked" in message or "busy" in message:
                    raise LibraryBusyError(
                        "The Books library database is busy (another program "
                        "is writing to it); nothing was changed. Try again in "
                        "a moment.",
                        cause=e,
                    ) from e
                raise
            # Validate under the write lock, so the schema can't change
            # between the check and the write.
            self._validate_schema()
        except BaseException:
            try:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # closing the connection rolls back as well
            finally:
                self.conn.close()
                self.conn = None
            raise
        return self

    def _validate_schema(self) -> None:
        conn = self.conn
        mode = resolve_model_check(self.model_check)
        relaxed = mode == "off"
        validate_table_columns(
            conn, _COLLECTION_TABLE, _COLLECTION_SCHEMA, allow_extra_nullable=relaxed
        )
        validate_table_columns(
            conn, _MEMBER_TABLE, _MEMBER_SCHEMA, allow_extra_nullable=relaxed
        )
        validate_table_schema(conn, _ASSET_TABLE, _ASSET_READ_COLUMNS)
        validate_table_schema(conn, _PK_TABLE, _PK_COLUMNS)

        entities = (_COLLECTION_ENTITY, _MEMBER_ENTITY)
        present = {
            row[0] for row in conn.execute(
                f"SELECT Z_NAME FROM {_PK_TABLE} WHERE Z_NAME IN (?, ?)", entities
            )
        }
        missing = [entity for entity in entities if entity not in present]
        if missing:
            raise SchemaValidationError(
                f"Z_PRIMARYKEY has no entry for entity(ies) {missing} — "
                "the schema has changed and writes are not safe."
            )

        self.model_hashes = check_model_hashes(conn, _VERIFIED_MODEL_HASHES, mode=mode)

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        finally:
            self.conn.close()
            self.conn = None


def _allocate_pk(cur: sqlite3.Cursor, entity_name: str, table: str) -> tuple[int, int]:
    """Reserve the next primary key for a Core Data entity.

    Returns ``(z_ent, new_pk)``. Core Data allocates keys from
    ``Z_PRIMARYKEY.Z_MAX``; failing to advance it makes Books' next
    native insert collide. Defensively takes ``max(Z_MAX, MAX(Z_PK))``
    in case a previous tool broke the invariant.
    """
    row = cur.execute(
        f"SELECT Z_ENT, Z_MAX FROM {_PK_TABLE} WHERE Z_NAME = ?", (entity_name,)
    ).fetchone()
    if row is None:
        raise SchemaValidationError(
            f"Z_PRIMARYKEY has no entry for entity {entity_name!r} — "
            "the schema has changed and writes are not safe."
        )
    z_ent, z_max = row
    actual_max = cur.execute(f"SELECT MAX(Z_PK) FROM {table}").fetchone()[0] or 0
    new_pk = max(z_max or 0, actual_max) + 1
    cur.execute(
        f"UPDATE {_PK_TABLE} SET Z_MAX = ? WHERE Z_NAME = ?", (new_pk, entity_name)
    )
    return z_ent, new_pk


def _fetch_collection(cur: sqlite3.Cursor, collection_id) -> tuple:
    row = cur.execute(
        f"SELECT Z_PK, ZCOLLECTIONID, ZTITLE, ZDELETEDFLAG "
        f"FROM {_COLLECTION_TABLE} WHERE Z_PK = ?",
        (collection_id,),
    ).fetchone()
    if row is None:
        raise CollectionNotFoundError(f"No collection with id {collection_id}.")
    pk, sentinel, title, deleted = row
    if deleted:
        raise CollectionNotFoundError(
            f"Collection {collection_id} ({title!r}) has been deleted."
        )
    return pk, sentinel, title


def _ensure_editable(sentinel: Optional[str], title, *, membership: bool) -> None:
    """Refuse writes against anything but user-created collections.

    Fails CLOSED: a collection is editable only if its
    ``ZCOLLECTIONID`` parses as a UUID — the shape every user-created
    collection has. Built-in sentinels (``*_Collection_ID``), NULL
    ids, and any future sentinel Apple introduces are all refused,
    with one explicit exception: ``membership=True`` allows 'Want to
    Read', whose membership the user edits in the app UI.
    """
    if membership and sentinel in MEMBERSHIP_EDITABLE_SYSTEM_IDS:
        return
    try:
        uuid.UUID(sentinel)
    except (TypeError, ValueError, AttributeError):
        raise SystemCollectionError(
            f"{title!r} is not a user-created collection (built-in or "
            "unrecognized) and cannot be modified." + (
                "" if membership
                else " Only user-created collections can be renamed or deleted."
            )
        )


def _touch_collection(cur: sqlite3.Cursor, pk: int, now: float) -> None:
    """Bump the collection row the way Books does when it changes
    (observed: membership edits update the parent's ZLOCALMODDATE)."""
    cur.execute(
        f"UPDATE {_COLLECTION_TABLE} SET Z_OPT = Z_OPT + 1, "
        f"ZLASTMODIFICATION = ?, ZLOCALMODDATE = ? WHERE Z_PK = ?",
        (now, now, pk),
    )


def create_collection(
    title: str,
    details: Optional[str] = None,
    **session_kwargs,
) -> int:
    """Create a user collection; returns its new id (``Z_PK``)."""
    title = (title or "").strip()
    if not title:
        raise WriteError("Collection title must be a non-empty string.")

    with WriteSession(**session_kwargs) as session:
        cur = session.conn.cursor()
        z_ent, new_pk = _allocate_pk(cur, _COLLECTION_ENTITY, _COLLECTION_TABLE)

        # Next sidebar slot after the highest user collection.
        max_sort = cur.execute(
            f"SELECT MAX(ZSORTKEY) FROM {_COLLECTION_TABLE} WHERE ZSORTKEY > 0"
        ).fetchone()[0]
        sort_key = (max_sort or 0) + SORT_KEY_STEP

        now = _cd_now()
        cur.execute(
            f"INSERT INTO {_COLLECTION_TABLE} "
            "(Z_PK, Z_ENT, Z_OPT, ZDELETEDFLAG, ZHIDDEN, ZPLACEHOLDER, "
            " ZSORTKEY, ZSORTMODE, ZVIEWMODE, ZLASTMODIFICATION, "
            " ZLOCALMODDATE, ZCOLLECTIONID, ZDETAILS, ZTITLE) "
            "VALUES (?, ?, 1, 0, 0, 0, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (
                new_pk, z_ent, sort_key, _DEFAULT_SORT_MODE,
                now, now, str(uuid.uuid4()).upper(), details, title,
            ),
        )
        return new_pk


def rename_collection(collection_id, new_title: str, **session_kwargs) -> None:
    """Rename a user-created collection."""
    new_title = (new_title or "").strip()
    if not new_title:
        raise WriteError("Collection title must be a non-empty string.")

    with WriteSession(**session_kwargs) as session:
        cur = session.conn.cursor()
        pk, sentinel, title = _fetch_collection(cur, collection_id)
        _ensure_editable(sentinel, title, membership=False)
        now = _cd_now()
        cur.execute(
            f"UPDATE {_COLLECTION_TABLE} SET ZTITLE = ?, Z_OPT = Z_OPT + 1, "
            f"ZLASTMODIFICATION = ?, ZLOCALMODDATE = ? WHERE Z_PK = ?",
            (new_title, now, now, pk),
        )


def delete_collection(collection_id, **session_kwargs) -> None:
    """Delete a user-created collection.

    Follows Books' own semantics: the collection row is soft-deleted
    (``ZDELETEDFLAG = 1`` — kept as an iCloud tombstone) and its
    membership rows are hard-deleted (Books hard-deletes members;
    verified by primary-key gaps in the live table). Books themselves
    are untouched.
    """
    with WriteSession(**session_kwargs) as session:
        cur = session.conn.cursor()
        pk, sentinel, title = _fetch_collection(cur, collection_id)
        _ensure_editable(sentinel, title, membership=False)
        now = _cd_now()
        cur.execute(
            f"UPDATE {_COLLECTION_TABLE} SET ZDELETEDFLAG = 1, Z_OPT = Z_OPT + 1, "
            f"ZLASTMODIFICATION = ?, ZLOCALMODDATE = ? WHERE Z_PK = ?",
            (now, now, pk),
        )
        cur.execute(f"DELETE FROM {_MEMBER_TABLE} WHERE ZCOLLECTION = ?", (pk,))


def add_book_to_collection(collection_id, book_id, **session_kwargs) -> bool:
    """Add a book to a collection. Idempotent: returns True if a
    membership row was created, False if the book was already there."""
    with WriteSession(**session_kwargs) as session:
        cur = session.conn.cursor()
        pk, sentinel, title = _fetch_collection(cur, collection_id)
        _ensure_editable(sentinel, title, membership=True)

        book_row = cur.execute(
            f"SELECT Z_PK, ZASSETID FROM {_ASSET_TABLE} WHERE Z_PK = ?",
            (book_id,),
        ).fetchone()
        if book_row is None:
            raise BookNotFoundError(f"No book with id {book_id}.")
        asset_pk, asset_id = book_row
        if asset_id is None:
            raise WriteError(
                f"Book {book_id} has no asset id — cannot create a "
                "sync-stable membership row."
            )

        duplicate = cur.execute(
            f"SELECT 1 FROM {_MEMBER_TABLE} WHERE ZCOLLECTION = ? AND ZASSETID = ?",
            (pk, asset_id),
        ).fetchone()
        if duplicate:
            return False

        z_ent, member_pk = _allocate_pk(cur, _MEMBER_ENTITY, _MEMBER_TABLE)
        max_sort = cur.execute(
            f"SELECT MAX(ZSORTKEY) FROM {_MEMBER_TABLE} WHERE ZCOLLECTION = ?",
            (pk,),
        ).fetchone()[0]
        sort_key = (max_sort or 0) + SORT_KEY_STEP

        now = _cd_now()
        cur.execute(
            f"INSERT INTO {_MEMBER_TABLE} "
            "(Z_PK, Z_ENT, Z_OPT, ZSORTKEY, ZASSET, ZCOLLECTION, "
            " ZLOCALMODDATE, ZASSETID, ZTEMPORARYASSETID) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, ?, NULL)",
            (member_pk, z_ent, sort_key, asset_pk, pk, now, asset_id),
        )
        _touch_collection(cur, pk, now)
        return True


def remove_book_from_collection(collection_id, book_id, **session_kwargs) -> bool:
    """Remove a book from a collection. Idempotent: returns True if a
    membership row was deleted, False if the book wasn't in it."""
    with WriteSession(**session_kwargs) as session:
        cur = session.conn.cursor()
        pk, sentinel, title = _fetch_collection(cur, collection_id)
        _ensure_editable(sentinel, title, membership=True)

        book_row = cur.execute(
            f"SELECT ZASSETID FROM {_ASSET_TABLE} WHERE Z_PK = ?",
            (book_id,),
        ).fetchone()
        if book_row is None:
            raise BookNotFoundError(f"No book with id {book_id}.")
        (asset_id,) = book_row

        cur.execute(
            f"DELETE FROM {_MEMBER_TABLE} WHERE ZCOLLECTION = ? AND ZASSETID = ?",
            (pk, asset_id),
        )
        changed = cur.rowcount > 0
        if changed:
            _touch_collection(cur, pk, _cd_now())
        return changed
