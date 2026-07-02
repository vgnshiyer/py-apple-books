"""Tests for collection write operations.

Every test runs against a scratch fixture database that replicates the
live ``BKLibrary`` schema (verbatim CREATE statements from a real
macOS library) — never against the user's actual library. Sessions are
opened with ``require_books_closed=False`` and ``backup=False`` except
where those guards are the thing under test.
"""

import sqlite3
import uuid

import pytest

from py_apple_books import collection_writer, write_safety
from py_apple_books.collection_writer import (
    SORT_KEY_STEP,
    add_book_to_collection,
    create_collection,
    delete_collection,
    remove_book_from_collection,
    rename_collection,
)
from py_apple_books.exceptions import (
    BookNotFoundError,
    BooksAppRunningError,
    CollectionNotFoundError,
    SchemaValidationError,
    SystemCollectionError,
    WriteError,
)

# Core Data epoch reference so tests can sanity-check timestamps.
CD_2025 = 750000000  # ~ late 2024 in Core Data seconds; new stamps must exceed this


@pytest.fixture
def fixture_db(tmp_path):
    """Scratch library DB mirroring the real schema + representative rows."""
    db = tmp_path / "BKLibrary-test.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ZBKCOLLECTION ( Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER,
            Z_OPT INTEGER, ZDELETEDFLAG INTEGER, ZHIDDEN INTEGER,
            ZPLACEHOLDER INTEGER, ZSORTKEY INTEGER, ZSORTMODE INTEGER,
            ZVIEWMODE INTEGER, ZLASTMODIFICATION TIMESTAMP,
            ZLOCALMODDATE TIMESTAMP, ZCOLLECTIONID VARCHAR, ZDETAILS VARCHAR,
            ZTITLE VARCHAR );
        CREATE TABLE ZBKCOLLECTIONMEMBER ( Z_PK INTEGER PRIMARY KEY,
            Z_ENT INTEGER, Z_OPT INTEGER, ZSORTKEY INTEGER, ZASSET INTEGER,
            ZCOLLECTION INTEGER, ZLOCALMODDATE TIMESTAMP, ZASSETID VARCHAR,
            ZTEMPORARYASSETID VARCHAR );
        CREATE TABLE ZBKLIBRARYASSET ( Z_PK INTEGER PRIMARY KEY,
            Z_ENT INTEGER, Z_OPT INTEGER, ZASSETID VARCHAR, ZTITLE VARCHAR );
        CREATE TABLE Z_PRIMARYKEY ( Z_ENT INTEGER PRIMARY KEY,
            Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER );

        INSERT INTO Z_PRIMARYKEY VALUES (2, 'BKCollection', 0, 15);
        INSERT INTO Z_PRIMARYKEY VALUES (3, 'BKCollectionMember', 0, 548);
        INSERT INTO Z_PRIMARYKEY VALUES (5, 'BKLibraryAsset', 0, 245);

        -- System collections (subset)
        INSERT INTO ZBKCOLLECTION VALUES (1, 2, 5, 0, 0, 0, -2, 6, NULL,
            758012697.6, 788638310.6, 'Want_To_Read_Collection_ID', NULL, 'Want to Read');
        INSERT INTO ZBKCOLLECTION VALUES (3, 2, 4, 0, 0, 0, -3, 6, NULL,
            758012697.6, 788638310.6, 'Books_Collection_ID', NULL, 'Books');

        -- User collections
        INSERT INTO ZBKCOLLECTION VALUES (9, 2, 4, 0, 0, 0, 60000, 6, NULL,
            758012702.9, 780299623.6, '7BCF5B83-A6FA-4B5F-B9E5-C85BAF7647C5', NULL, 'Finance');
        INSERT INTO ZBKCOLLECTION VALUES (15, 2, 2, 0, 0, 0, 10000, 6, NULL,
            758012702.9, 714777249.7, 'F2D26108-C5D3-444D-8775-1BFA253F670A', NULL, 'Tech');

        -- Books
        INSERT INTO ZBKLIBRARYASSET VALUES (151, 5, 1, '28AEDF62F12B289C88BD6659BD6E50CC', 'DDIA');
        INSERT INTO ZBKLIBRARYASSET VALUES (191, 5, 1, '0BAEAACD05D85FAAABCB1A69B77FA9F7', 'Biz21');
        INSERT INTO ZBKLIBRARYASSET VALUES (200, 5, 1, NULL, 'NoAssetId');

        -- Existing membership: DDIA in Tech
        INSERT INTO ZBKCOLLECTIONMEMBER VALUES (126, 3, 3, 10000, 151, 15,
            780299623.6, '28AEDF62F12B289C88BD6659BD6E50CC', NULL);
        """
    )
    conn.commit()
    conn.close()
    return db


def _q(db, sql, params=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _session_kwargs(db):
    return dict(db_path=db, backup=False, require_books_closed=False)


# ---------------------------------------------------------------------------
# create_collection
# ---------------------------------------------------------------------------


def test_create_collection_row_and_bookkeeping(fixture_db):
    new_id = create_collection("Philosophy", **_session_kwargs(fixture_db))
    assert new_id == 16  # Z_MAX was 15

    row = _q(fixture_db, "SELECT * FROM ZBKCOLLECTION WHERE Z_PK = ?", (new_id,))[0]
    (pk, z_ent, z_opt, deleted, hidden, placeholder, sortkey, sortmode,
     viewmode, lastmod, localmod, coll_id, details, title) = row
    assert z_ent == 2
    assert z_opt == 1
    assert (deleted, hidden, placeholder) == (0, 0, 0)
    assert sortmode == 6
    assert viewmode is None
    assert title == "Philosophy"
    assert details is None
    # sidebar slot after highest user sortkey (60000)
    assert sortkey == 60000 + SORT_KEY_STEP
    # uppercase UUID
    assert coll_id == coll_id.upper()
    uuid.UUID(coll_id)  # parses
    # Core Data timestamps, recent
    assert lastmod > CD_2025 and localmod > CD_2025

    # Z_PRIMARYKEY advanced
    z_max = _q(fixture_db, "SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME = 'BKCollection'")[0][0]
    assert z_max == 16


def test_create_collection_rejects_empty_title(fixture_db):
    with pytest.raises(WriteError):
        create_collection("   ", **_session_kwargs(fixture_db))


def test_create_collection_defensive_pk_when_zmax_stale(fixture_db):
    # Simulate a broken invariant: Z_MAX lower than actual MAX(Z_PK)
    conn = sqlite3.connect(fixture_db)
    conn.execute("UPDATE Z_PRIMARYKEY SET Z_MAX = 3 WHERE Z_NAME = 'BKCollection'")
    conn.commit()
    conn.close()

    new_id = create_collection("Recovery", **_session_kwargs(fixture_db))
    assert new_id == 16  # max(3, 15) + 1, not 4


# ---------------------------------------------------------------------------
# rename_collection
# ---------------------------------------------------------------------------


def test_rename_collection(fixture_db):
    before_opt = _q(fixture_db, "SELECT Z_OPT FROM ZBKCOLLECTION WHERE Z_PK = 9")[0][0]
    rename_collection(9, "Money", **_session_kwargs(fixture_db))
    title, z_opt = _q(
        fixture_db, "SELECT ZTITLE, Z_OPT FROM ZBKCOLLECTION WHERE Z_PK = 9"
    )[0]
    assert title == "Money"
    assert z_opt == before_opt + 1


def test_rename_system_collection_refused(fixture_db):
    with pytest.raises(SystemCollectionError):
        rename_collection(1, "Nope", **_session_kwargs(fixture_db))  # Want to Read
    with pytest.raises(SystemCollectionError):
        rename_collection(3, "Nope", **_session_kwargs(fixture_db))  # Books


def test_rename_missing_collection(fixture_db):
    with pytest.raises(CollectionNotFoundError):
        rename_collection(999, "X", **_session_kwargs(fixture_db))


# ---------------------------------------------------------------------------
# delete_collection
# ---------------------------------------------------------------------------


def test_delete_collection_soft_deletes_and_clears_members(fixture_db):
    delete_collection(15, **_session_kwargs(fixture_db))  # Tech, has 1 member
    deleted = _q(fixture_db, "SELECT ZDELETEDFLAG FROM ZBKCOLLECTION WHERE Z_PK = 15")[0][0]
    assert deleted == 1
    members = _q(fixture_db, "SELECT * FROM ZBKCOLLECTIONMEMBER WHERE ZCOLLECTION = 15")
    assert members == []
    # the book itself is untouched
    assert _q(fixture_db, "SELECT COUNT(*) FROM ZBKLIBRARYASSET WHERE Z_PK = 151")[0][0] == 1


def test_delete_system_collection_refused(fixture_db):
    with pytest.raises(SystemCollectionError):
        delete_collection(1, **_session_kwargs(fixture_db))


def test_deleted_collection_not_addressable(fixture_db):
    delete_collection(15, **_session_kwargs(fixture_db))
    with pytest.raises(CollectionNotFoundError):
        rename_collection(15, "Back", **_session_kwargs(fixture_db))


# ---------------------------------------------------------------------------
# add_book_to_collection
# ---------------------------------------------------------------------------


def test_add_book_creates_member_row(fixture_db):
    changed = add_book_to_collection(9, 191, **_session_kwargs(fixture_db))
    assert changed is True

    row = _q(
        fixture_db,
        "SELECT Z_PK, Z_ENT, Z_OPT, ZSORTKEY, ZASSET, ZCOLLECTION, ZASSETID, "
        "ZTEMPORARYASSETID FROM ZBKCOLLECTIONMEMBER WHERE ZCOLLECTION = 9",
    )[0]
    pk, z_ent, z_opt, sortkey, asset, coll, asset_id, temp_id = row
    assert pk == 549  # member Z_MAX was 548
    assert z_ent == 3
    assert z_opt == 1
    assert sortkey == SORT_KEY_STEP  # first member
    assert asset == 191
    assert asset_id == "0BAEAACD05D85FAAABCB1A69B77FA9F7"
    assert temp_id is None

    z_max = _q(fixture_db, "SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME = 'BKCollectionMember'")[0][0]
    assert z_max == 549


def test_add_book_appends_after_existing_members(fixture_db):
    add_book_to_collection(15, 191, **_session_kwargs(fixture_db))
    sortkey = _q(
        fixture_db,
        "SELECT ZSORTKEY FROM ZBKCOLLECTIONMEMBER WHERE ZCOLLECTION = 15 AND ZASSET = 191",
    )[0][0]
    assert sortkey == 10000 + SORT_KEY_STEP  # after DDIA's 10000


def test_add_book_touches_parent_collection(fixture_db):
    before = _q(fixture_db, "SELECT Z_OPT, ZLOCALMODDATE FROM ZBKCOLLECTION WHERE Z_PK = 9")[0]
    add_book_to_collection(9, 191, **_session_kwargs(fixture_db))
    after = _q(fixture_db, "SELECT Z_OPT, ZLOCALMODDATE FROM ZBKCOLLECTION WHERE Z_PK = 9")[0]
    assert after[0] == before[0] + 1
    assert after[1] > before[1]


def test_add_book_duplicate_is_noop(fixture_db):
    assert add_book_to_collection(9, 191, **_session_kwargs(fixture_db)) is True
    assert add_book_to_collection(9, 191, **_session_kwargs(fixture_db)) is False
    count = _q(
        fixture_db,
        "SELECT COUNT(*) FROM ZBKCOLLECTIONMEMBER WHERE ZCOLLECTION = 9",
    )[0][0]
    assert count == 1


def test_add_book_to_want_to_read_allowed(fixture_db):
    assert add_book_to_collection(1, 151, **_session_kwargs(fixture_db)) is True


def test_add_book_to_managed_system_collection_refused(fixture_db):
    with pytest.raises(SystemCollectionError):
        add_book_to_collection(3, 151, **_session_kwargs(fixture_db))  # Books


def test_add_unknown_book(fixture_db):
    with pytest.raises(BookNotFoundError):
        add_book_to_collection(9, 999, **_session_kwargs(fixture_db))


def test_add_book_without_asset_id_refused(fixture_db):
    with pytest.raises(WriteError):
        add_book_to_collection(9, 200, **_session_kwargs(fixture_db))


# ---------------------------------------------------------------------------
# remove_book_from_collection
# ---------------------------------------------------------------------------


def test_remove_book(fixture_db):
    assert remove_book_from_collection(15, 151, **_session_kwargs(fixture_db)) is True
    members = _q(fixture_db, "SELECT * FROM ZBKCOLLECTIONMEMBER WHERE ZCOLLECTION = 15")
    assert members == []


def test_remove_book_not_in_collection_is_noop(fixture_db):
    assert remove_book_from_collection(9, 151, **_session_kwargs(fixture_db)) is False


def test_remove_from_managed_system_collection_refused(fixture_db):
    with pytest.raises(SystemCollectionError):
        remove_book_from_collection(3, 151, **_session_kwargs(fixture_db))


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_books_running_guard(fixture_db, monkeypatch):
    monkeypatch.setattr(write_safety, "books_is_running", lambda: True)
    with pytest.raises(BooksAppRunningError):
        create_collection("X", db_path=fixture_db, backup=False)


def test_unknown_sentinel_fails_closed(fixture_db):
    """A built-in collection added by a future macOS (sentinel we've
    never seen, negative sort key) must be refused, not treated as
    user-created. The guard fails closed: only UUID-shaped ids are
    editable."""
    conn = sqlite3.connect(fixture_db)
    conn.execute(
        "INSERT INTO ZBKCOLLECTION VALUES (14, 2, 1, 0, 0, 0, -9, 6, NULL, "
        "758012697.6, 788638310.6, 'Hidden_Collection_ID', NULL, 'Hidden')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(SystemCollectionError):
        rename_collection(14, "Nope", **_session_kwargs(fixture_db))
    with pytest.raises(SystemCollectionError):
        delete_collection(14, **_session_kwargs(fixture_db))
    with pytest.raises(SystemCollectionError):
        add_book_to_collection(14, 151, **_session_kwargs(fixture_db))


def test_schema_drift_aborts(fixture_db, tmp_path):
    # Simulate a macOS update adding a NOT NULL column we can't populate.
    conn = sqlite3.connect(fixture_db)
    conn.execute(
        "ALTER TABLE ZBKCOLLECTION ADD COLUMN ZNEWREQUIRED INTEGER NOT NULL DEFAULT 0"
    )
    conn.commit()
    conn.close()
    with pytest.raises(SchemaValidationError):
        create_collection("X", **_session_kwargs(fixture_db))
    # Nothing was written
    count = _q(fixture_db, "SELECT COUNT(*) FROM ZBKCOLLECTION")[0][0]
    assert count == 4


def test_missing_table_aborts(tmp_path):
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    with pytest.raises(SchemaValidationError):
        create_collection("X", db_path=empty, backup=False, require_books_closed=False)


def test_backup_taken_before_write(fixture_db, tmp_path):
    backup_dir = tmp_path / "backups"
    new_id = create_collection(
        "Backed", db_path=fixture_db, backup=True,
        backup_dir=backup_dir, require_books_closed=False,
    )
    backups = list(backup_dir.glob("BKLibrary-test-*.sqlite"))
    assert len(backups) == 1
    # The backup predates the write: it must NOT contain the new row.
    rows = _q(backups[0], "SELECT COUNT(*) FROM ZBKCOLLECTION WHERE Z_PK = ?", (new_id,))
    assert rows[0][0] == 0


def test_backup_pruning(fixture_db, tmp_path):
    backup_dir = tmp_path / "backups"
    for i in range(7):
        write_safety.backup_library(fixture_db, backup_dir, keep=5)
    assert len(list(backup_dir.glob("*.sqlite"))) == 5


def test_backup_min_interval_reuses_recent(fixture_db, tmp_path):
    """Within the interval, the newest backup is reused — preserving
    the pre-batch restore point during a burst of writes."""
    backup_dir = tmp_path / "backups"
    first = write_safety.backup_library(fixture_db, backup_dir, min_interval=300)
    second = write_safety.backup_library(fixture_db, backup_dir, min_interval=300)
    assert first == second
    assert len(list(backup_dir.glob("*.sqlite"))) == 1
    # interval=0 always takes a fresh one
    third = write_safety.backup_library(fixture_db, backup_dir, min_interval=0)
    assert third != first


def test_failed_backup_leaves_no_artifact(tmp_path):
    """A backup that fails must not leave a .part or bogus .sqlite
    behind to masquerade as a valid restore point."""
    backup_dir = tmp_path / "backups"
    missing = tmp_path / "does-not-exist.sqlite"
    with pytest.raises(WriteError):
        write_safety.backup_library(missing, backup_dir)
    assert list(backup_dir.glob("*")) == []


def test_failed_op_rolls_back_everything(fixture_db):
    """A failing statement mid-operation must leave no partial writes —
    including the Z_PRIMARYKEY bump."""
    z_max_before = _q(
        fixture_db, "SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME = 'BKCollectionMember'"
    )[0][0]
    # Book 200 has no asset id -> raises AFTER collection fetch but the
    # session context must roll back any bookkeeping.
    with pytest.raises(WriteError):
        add_book_to_collection(9, 200, **_session_kwargs(fixture_db))
    z_max_after = _q(
        fixture_db, "SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME = 'BKCollectionMember'"
    )[0][0]
    assert z_max_after == z_max_before
    assert _q(fixture_db, "SELECT COUNT(*) FROM ZBKCOLLECTIONMEMBER WHERE ZCOLLECTION = 9")[0][0] == 0


# ---------------------------------------------------------------------------
# restore_library
# ---------------------------------------------------------------------------


def test_restore_library(fixture_db, tmp_path, monkeypatch):
    monkeypatch.setattr(write_safety, "books_is_running", lambda: False)
    backup = write_safety.backup_library(fixture_db, tmp_path / "b")
    create_collection("Ephemeral", **_session_kwargs(fixture_db))
    assert _q(fixture_db, "SELECT COUNT(*) FROM ZBKCOLLECTION")[0][0] == 5

    write_safety.restore_library(backup, fixture_db)
    assert _q(fixture_db, "SELECT COUNT(*) FROM ZBKCOLLECTION")[0][0] == 4


def test_restore_survives_open_reader(fixture_db, tmp_path, monkeypatch):
    """Regression for the review's critical finding: a filesystem-copy
    restore is silently undone by connections that stay open across it
    (Books' daemons; this package's own import-time read connections).
    The SQLite-level restore must hold up even with a reader open, and
    that reader must see the restored state on its next query."""
    monkeypatch.setattr(write_safety, "books_is_running", lambda: False)
    # Put the fixture in WAL mode like the real library.
    conn = sqlite3.connect(fixture_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()

    reader = sqlite3.connect(fixture_db)  # stays open across the restore
    assert reader.execute("SELECT COUNT(*) FROM ZBKCOLLECTION").fetchone()[0] == 4

    backup = write_safety.backup_library(fixture_db, tmp_path / "b")
    create_collection("Ephemeral", **_session_kwargs(fixture_db))
    write_safety.restore_library(backup, fixture_db)

    # The still-open reader sees the restored state, not the stale one.
    assert reader.execute("SELECT COUNT(*) FROM ZBKCOLLECTION").fetchone()[0] == 4
    reader.close()
    # And a fresh connection agrees + the file is intact.
    fresh = sqlite3.connect(fixture_db)
    assert fresh.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert fresh.execute("SELECT COUNT(*) FROM ZBKCOLLECTION").fetchone()[0] == 4
    fresh.close()
