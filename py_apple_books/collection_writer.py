"""Write operations for Apple Books collections.

Apple exposes no automation surface for collections (no AppleScript
dictionary, no Shortcuts action), so the only path is writing directly
to the library's Core Data SQLite store. That demands discipline; every
operation here runs inside a :class:`WriteSession` that:

1. refuses while the Books app is running,
2. takes a WAL-inclusive backup (on by default),
3. validates the schema and aborts on drift,
4. wraps all statements in one ``BEGIN IMMEDIATE`` transaction, and
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

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

from py_apple_books.db.client import AppleBooksDBClient, find_sqlite_file
from py_apple_books.exceptions import (
    BookNotFoundError,
    CollectionNotFoundError,
    SystemCollectionError,
    WriteError,
)
from py_apple_books.utils import APPLE_EPOCH_OFFSET
from py_apple_books.write_safety import (
    backup_library,
    ensure_books_not_running,
    validate_table_schema,
)

_COLLECTION_TABLE = "ZBKCOLLECTION"
_MEMBER_TABLE = "ZBKCOLLECTIONMEMBER"
_ASSET_TABLE = "ZBKLIBRARYASSET"
_PK_TABLE = "Z_PRIMARYKEY"

_COLLECTION_ENTITY = "BKCollection"
_MEMBER_ENTITY = "BKCollectionMember"

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

# Columns each write populates / requires. Used both to build INSERTs
# and to validate the live schema before any write.
_COLLECTION_COLUMNS = {
    "Z_PK", "Z_ENT", "Z_OPT", "ZDELETEDFLAG", "ZHIDDEN", "ZPLACEHOLDER",
    "ZSORTKEY", "ZSORTMODE", "ZVIEWMODE", "ZLASTMODIFICATION",
    "ZLOCALMODDATE", "ZCOLLECTIONID", "ZDETAILS", "ZTITLE",
}
_MEMBER_COLUMNS = {
    "Z_PK", "Z_ENT", "Z_OPT", "ZSORTKEY", "ZASSET", "ZCOLLECTION",
    "ZLOCALMODDATE", "ZASSETID", "ZTEMPORARYASSETID",
}


def _cd_now() -> float:
    """Current time as a Core Data timestamp (seconds since 2001-01-01)."""
    return time.time() - APPLE_EPOCH_OFFSET


def _default_db_path() -> Path:
    return find_sqlite_file(AppleBooksDBClient.book_lib_db[1])


class WriteSession:
    """One guarded transaction against the Books library database.

    Context manager: guards run on ``__enter__``, the transaction
    commits on clean exit and rolls back on any exception.

    :param db_path: Override the library database path (tests point
        this at a fixture copy; production leaves it None).
    :param backup: Take a pre-write backup. On by default; only tests
        should turn this off.
    :param backup_dir: Override the backup directory.
    :param require_books_closed: Refuse when Books.app is running. On
        by default; only tests against fixture databases turn this off.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        backup: bool = True,
        backup_dir: Optional[Path] = None,
        require_books_closed: bool = True,
    ):
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self.backup = backup
        self.backup_dir = backup_dir
        self.require_books_closed = require_books_closed
        self.backup_path: Optional[Path] = None
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
        self.conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        try:
            validate_table_schema(
                self.conn, _COLLECTION_TABLE,
                required_columns=_COLLECTION_COLUMNS,
                writable_columns=_COLLECTION_COLUMNS,
            )
            validate_table_schema(
                self.conn, _MEMBER_TABLE,
                required_columns=_MEMBER_COLUMNS,
                writable_columns=_MEMBER_COLUMNS,
            )
            self.conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            self.conn.close()
            self.conn = None
            raise
        return self

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
        raise WriteError(
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
