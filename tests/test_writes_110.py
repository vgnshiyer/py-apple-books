"""1.10 write-path hardening: the exact pre-write schema check and its
escape hatch (F37), lock contention (LibraryBusyError) and restore
hardening (F45).

Synthetic libraries only (the ``fixture_lib`` of
``test_collection_writer``); Books.app is never checked for real.
"""

import base64
import logging
import os
import plistlib
import sqlite3
import sys
import time

import pytest

from py_apple_books import collection_writer, write_safety
from py_apple_books.collection_writer import (
    WriteSession,
    add_book_to_collection,
    create_collection,
)
from py_apple_books.exceptions import (
    BackupValidationError,
    BooksAppRunningError,
    LibraryBusyError,
    SchemaValidationError,
    WriteError,
)

from tests.test_collection_writer import (  # noqa: F401  (fixtures)
    COLLECTION_ROWS,
    SHELF_A,
    OTHER_BOOK,
    _default_backup_dir,
    _q,
    _session_kwargs,
    fixture_db,
    fixture_lib,
)

UNKNOWN_HASH = base64.b64encode(b"\x01" * 32).decode("ascii")
LOGGER = "py_apple_books.write_safety"


@pytest.fixture(autouse=True)
def _no_model_check_env(monkeypatch):
    monkeypatch.delenv(write_safety.MODEL_CHECK_ENV, raising=False)


def _counts(db):
    """Rows in the tables a write touches, plus the key counters."""
    return (
        _q(db, "SELECT COUNT(*) FROM ZBKCOLLECTION")[0][0],
        _q(db, "SELECT COUNT(*) FROM ZBKCOLLECTIONMEMBER")[0][0],
        _q(db, "SELECT Z_NAME, Z_MAX FROM Z_PRIMARYKEY ORDER BY Z_ENT"),
    )


def _alter(db, *statements):
    conn = sqlite3.connect(db)
    try:
        for sql in statements:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _set_model_hash(db, entity, value):
    """Rewrite one entity's hash in the store's Z_METADATA plist."""
    conn = sqlite3.connect(db)
    try:
        blob = conn.execute("SELECT Z_PLIST FROM Z_METADATA").fetchone()[0]
        plist = plistlib.loads(blob)
        plist["NSStoreModelVersionHashes"][entity] = base64.b64decode(value)
        conn.execute(
            "UPDATE Z_METADATA SET Z_PLIST = ?",
            (plistlib.dumps(plist, fmt=plistlib.FMT_BINARY),),
        )
        conn.commit()
    finally:
        conn.close()


def _warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING]


# Each drifted table is exercised by the write that inserts into it:
# (table, write, the table and key of the row that write inserts).
def _create(db, **kwargs):
    return create_collection("Drift", **_session_kwargs(db), **kwargs)


def _add(db, **kwargs):
    return add_book_to_collection(SHELF_A, OTHER_BOOK, **_session_kwargs(db), **kwargs)


DRIFT_CASES = [
    pytest.param("ZBKCOLLECTION", _create, id="collection"),
    pytest.param("ZBKCOLLECTIONMEMBER", _add, id="member"),
]


def _inserted_value(db, table, column):
    if table == "ZBKCOLLECTION":
        sql = f"SELECT {column} FROM ZBKCOLLECTION WHERE ZTITLE = 'Drift'"
    else:
        sql = f"SELECT {column} FROM ZBKCOLLECTIONMEMBER WHERE ZCOLLECTION = {SHELF_A}"
    rows = _q(db, sql)
    assert len(rows) == 1
    return rows[0][0]


# ---------------------------------------------------------------------------
# F37: exact schema check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table, write", DRIFT_CASES)
def test_nullable_column_drift_aborts(fixture_db, table, write):
    # The shape Core Data gives a new attribute: nullable, no default.
    # 1.9 let this through.
    _alter(fixture_db, f"ALTER TABLE {table} ADD COLUMN ZNEWATTRIBUTE INTEGER")
    before = _counts(fixture_db)
    with pytest.raises(SchemaValidationError, match="ZNEWATTRIBUTE") as info:
        write(fixture_db)
    message = str(info.value)
    assert f"Table {table} doesn't match" in message
    assert "APPLE_BOOKS_MODEL_CHECK=off" in message
    assert "python -m py_apple_books.testing.dump_schema" in message
    assert _counts(fixture_db) == before


@pytest.mark.parametrize("via", ["kwarg", "env"])
@pytest.mark.parametrize("table, write", DRIFT_CASES)
def test_model_check_off_allows_nullable_column(fixture_db, table, write, via,
                                                monkeypatch, caplog):
    _alter(fixture_db, f"ALTER TABLE {table} ADD COLUMN ZNEWATTRIBUTE INTEGER")
    kwargs = {}
    if via == "env":
        monkeypatch.setenv(write_safety.MODEL_CHECK_ENV, "OFF")  # case-insensitive
    else:
        kwargs["model_check"] = "off"
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        write(fixture_db, **kwargs)
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "ZNEWATTRIBUTE" in warnings[0].getMessage()
    assert table in warnings[0].getMessage()
    assert _inserted_value(fixture_db, table, "ZNEWATTRIBUTE") is None


@pytest.mark.parametrize("mode", [None, "off"])
def test_not_null_extra_column_aborts_even_when_off(fixture_db, mode):
    _alter(fixture_db,
           "ALTER TABLE ZBKCOLLECTION ADD COLUMN ZNEWREQUIRED INTEGER NOT NULL DEFAULT 0")
    before = _counts(fixture_db)
    with pytest.raises(SchemaValidationError, match="ZNEWREQUIRED"):
        _create(fixture_db, model_check=mode)
    assert _counts(fixture_db) == before


@pytest.mark.parametrize("mode", [None, "off"])
def test_retyped_column_aborts_even_when_off(fixture_db, mode):
    _alter(
        fixture_db,
        "ALTER TABLE ZBKCOLLECTIONMEMBER RENAME TO OLD_MEMBER",
        "CREATE TABLE ZBKCOLLECTIONMEMBER ( Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, "
        "Z_OPT INTEGER, ZSORTKEY FLOAT, ZASSET INTEGER, ZCOLLECTION INTEGER, "
        "ZLOCALMODDATE TIMESTAMP, ZASSETID VARCHAR, ZTEMPORARYASSETID VARCHAR )",
        "INSERT INTO ZBKCOLLECTIONMEMBER SELECT * FROM OLD_MEMBER",
        "DROP TABLE OLD_MEMBER",
    )
    before = _counts(fixture_db)
    with pytest.raises(SchemaValidationError, match="ZSORTKEY INTEGER->FLOAT"):
        _add(fixture_db, model_check=mode)
    assert _counts(fixture_db) == before


@pytest.mark.parametrize("mode", [None, "off"])
def test_dropped_column_aborts_even_when_off(fixture_db, mode):
    _alter(fixture_db, "ALTER TABLE ZBKCOLLECTION DROP COLUMN ZDETAILS")
    before = _counts(fixture_db)
    with pytest.raises(SchemaValidationError, match=r"missing \['ZDETAILS'\]"):
        _create(fixture_db, model_check=mode)
    assert _counts(fixture_db) == before


def test_read_table_column_dropped_aborts(fixture_db):
    _alter(
        fixture_db,
        "DROP INDEX Z_BKLibraryAsset_byAssetIDIndex",
        "ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZASSETID",
    )
    before = _counts(fixture_db)
    with pytest.raises(SchemaValidationError, match="ZBKLIBRARYASSET.*ZASSETID"):
        _add(fixture_db, model_check="off")
    assert _counts(fixture_db) == before


def test_missing_primary_key_entity_aborts(fixture_db):
    _alter(fixture_db, "DELETE FROM Z_PRIMARYKEY WHERE Z_NAME = 'BKCollectionMember'")
    before = _counts(fixture_db)
    with pytest.raises(SchemaValidationError, match="BKCollectionMember"):
        _create(fixture_db)
    assert _counts(fixture_db) == before


def test_validation_runs_under_the_write_lock(fixture_db, monkeypatch):
    """Validation happens after BEGIN IMMEDIATE: with the lock held
    elsewhere, a drifted store reports busy, not drift."""
    _alter(fixture_db, "ALTER TABLE ZBKCOLLECTION ADD COLUMN ZNEWATTRIBUTE INTEGER")
    monkeypatch.setattr(collection_writer, "BUSY_TIMEOUT", 0.05)
    holder = sqlite3.connect(fixture_db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(LibraryBusyError):
            _create(fixture_db)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_failed_validation_releases_the_lock(fixture_db):
    _alter(fixture_db, "ALTER TABLE ZBKCOLLECTION ADD COLUMN ZNEWATTRIBUTE INTEGER")
    with pytest.raises(SchemaValidationError):
        _create(fixture_db)
    other = sqlite3.connect(fixture_db, timeout=0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")  # would raise 'database is locked'
        other.execute("ROLLBACK")
    finally:
        other.close()


def test_validate_table_columns_is_case_insensitive(fixture_db):
    conn = sqlite3.connect(fixture_db)
    try:
        expected = {name.lower(): decl.lower()
                    for name, decl in collection_writer._COLLECTION_SCHEMA.items()}
        write_safety.validate_table_columns(conn, "zbkcollection", expected)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# F37: Core Data model hashes
# ---------------------------------------------------------------------------


def test_unknown_model_hash_warns_and_writes_by_default(fixture_db, caplog):
    _set_model_hash(fixture_db, "BKCollection", UNKNOWN_HASH)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        new_id = _create(fixture_db)
    assert new_id == 16
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert f"BKCollection={UNKNOWN_HASH}" in message
    assert "NSPersistenceFrameworkVersion 1526" in message
    assert "BKCollectionMember" not in message  # its hash is the verified one


def test_known_model_hashes_are_silent(fixture_db, caplog):
    with caplog.at_level(logging.DEBUG, logger="py_apple_books"):
        with WriteSession(**_session_kwargs(fixture_db)) as session:
            pass
    assert caplog.records == []
    assert session.model_hashes == {
        entity: next(iter(hashes))
        for entity, hashes in collection_writer._VERIFIED_MODEL_HASHES.items()
    }


@pytest.mark.parametrize("via", ["kwarg", "env"])
def test_enforce_refuses_unknown_member_hash(fixture_db, monkeypatch, via):
    _set_model_hash(fixture_db, "BKCollectionMember", UNKNOWN_HASH)
    kwargs = {}
    if via == "env":
        monkeypatch.setenv(write_safety.MODEL_CHECK_ENV, "enforce")
    else:
        kwargs["model_check"] = "enforce"
    before = _counts(fixture_db)
    with pytest.raises(SchemaValidationError) as info:
        _create(fixture_db, **kwargs)
    message = str(info.value)
    assert f"BKCollectionMember={UNKNOWN_HASH}" in message
    assert "APPLE_BOOKS_MODEL_CHECK=warn" in message  # how to override
    assert _counts(fixture_db) == before


def test_unpinned_entity_hash_is_never_checked(fixture_db):
    _set_model_hash(fixture_db, "BKLibraryAsset", UNKNOWN_HASH)
    assert _create(fixture_db, model_check="enforce") == 16


def test_missing_metadata_counts_as_unknown(fixture_db):
    _alter(fixture_db, "DELETE FROM Z_METADATA")
    with pytest.raises(SchemaValidationError, match="BKCollection=unavailable"):
        _create(fixture_db, model_check="enforce")


def test_model_check_off_skips_hashes(fixture_db, caplog):
    _set_model_hash(fixture_db, "BKCollection", UNKNOWN_HASH)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        with WriteSession(**_session_kwargs(fixture_db), model_check="off") as session:
            pass
    assert _warnings(caplog) == []
    assert session.model_hashes == {}


def test_kwarg_overrides_env(fixture_db, monkeypatch):
    monkeypatch.setenv(write_safety.MODEL_CHECK_ENV, "enforce")
    _set_model_hash(fixture_db, "BKCollection", UNKNOWN_HASH)
    assert _create(fixture_db, model_check="warn") == 16


@pytest.mark.parametrize("value, expected", [
    (None, "warn"), ("", "warn"), ("warn", "warn"), ("Enforce", "enforce"), (" off ", "off"),
])
def test_resolve_model_check(monkeypatch, value, expected, caplog):
    if value is not None:
        monkeypatch.setenv(write_safety.MODEL_CHECK_ENV, value)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert write_safety.resolve_model_check() == expected
        assert write_safety.resolve_model_check(value) == expected
    assert _warnings(caplog) == []


def test_resolve_model_check_invalid_value_warns(monkeypatch, caplog):
    monkeypatch.setenv(write_safety.MODEL_CHECK_ENV, "disabled")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert write_safety.resolve_model_check() == "warn"
    assert "disabled" in caplog.text


# ---------------------------------------------------------------------------
# LibraryBusyError
# ---------------------------------------------------------------------------


def test_busy_library_raises_library_busy_error(fixture_db, monkeypatch):
    monkeypatch.setattr(collection_writer, "BUSY_TIMEOUT", 0.05)
    holder = sqlite3.connect(fixture_db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        before = _counts(fixture_db)
        with pytest.raises(LibraryBusyError) as info:
            _create(fixture_db)
        assert "busy" in str(info.value) and "nothing was changed" in str(info.value)
        assert isinstance(info.value.__cause__, sqlite3.OperationalError)
        if sys.version_info >= (3, 11):
            assert info.value.sqlite_errorname == "SQLITE_BUSY"
        # 1.9 handlers caught the raw sqlite3 error; 1.10 ones catch WriteError.
        for handler in (sqlite3.OperationalError, WriteError):
            try:
                _create(fixture_db)
            except handler:
                pass
            else:
                pytest.fail(f"not caught by except {handler.__name__}")
        assert _counts(fixture_db) == before
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# ---------------------------------------------------------------------------
# F45: restore hardening
# ---------------------------------------------------------------------------


@pytest.fixture
def restorable(fixture_lib, tmp_path, monkeypatch):
    """A library with one backup, then one more collection written."""
    monkeypatch.setattr(write_safety, "books_is_running", lambda: False)
    db = fixture_lib.library_path
    bdir = tmp_path / "b"
    backup = write_safety.backup_library(db, bdir)
    create_collection("After backup", **_session_kwargs(db))
    return db, backup, bdir


def _collections(db):
    return _q(db, "SELECT COUNT(*) FROM ZBKCOLLECTION")[0][0]


def test_verify_backup_accepts_own_backup(restorable):
    db, backup, _ = restorable
    write_safety.verify_backup(backup, db)


def test_restore_returns_snapshot_that_undoes_it(restorable):
    db, backup, bdir = restorable
    snap = write_safety.restore_library(backup, db, backup_dir=bdir)
    assert snap.parent == bdir and snap != backup
    assert _collections(db) == COLLECTION_ROWS
    assert _collections(snap) == COLLECTION_ROWS + 1
    assert write_safety.list_backups(db, bdir)[0] == snap
    write_safety.restore_library(snap, db, backup_dir=bdir)
    assert _collections(db) == COLLECTION_ROWS + 1


def test_restore_without_snapshot_returns_none(restorable):
    db, backup, bdir = restorable
    assert write_safety.restore_library(backup, db, snapshot=False, backup_dir=bdir) is None
    assert write_safety.list_backups(db, bdir) == [backup]
    assert _collections(db) == COLLECTION_ROWS


def test_restore_other_store_refused(restorable, fixture_lib):
    """The classic mistake: the annotations store (same folder layout,
    different store UUID) restored over the library."""
    db, _, bdir = restorable
    with pytest.raises(BackupValidationError) as info:
        write_safety.restore_library(fixture_lib.annotation_path, db, backup_dir=bdir)
    assert info.value.reason == BackupValidationError.WRONG_STORE
    live_uuid = write_safety.read_store_metadata(db).uuid
    other_uuid = write_safety.read_store_metadata(fixture_lib.annotation_path).uuid
    assert live_uuid in str(info.value) and other_uuid in str(info.value)
    assert _collections(db) == COLLECTION_ROWS + 1
    assert len(write_safety.list_backups(db, bdir)) == 1  # no snapshot taken


def test_restore_non_database_refused(restorable, tmp_path):
    db, _, bdir = restorable
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises(BackupValidationError) as info:
        write_safety.restore_library(junk, db, backup_dir=bdir)
    assert info.value.reason == BackupValidationError.NOT_A_DATABASE
    assert _collections(db) == COLLECTION_ROWS + 1


def test_restore_damaged_backup_refused(restorable):
    db, backup, bdir = restorable
    # Header field 36 is the freelist page count; the file has no free
    # pages, so any other value fails PRAGMA quick_check.
    data = bytearray(backup.read_bytes())
    data[36:40] = (3).to_bytes(4, "big")
    backup.write_bytes(bytes(data))
    with pytest.raises(BackupValidationError, match="integrity check") as info:
        write_safety.restore_library(backup, db, backup_dir=bdir)
    assert info.value.reason == BackupValidationError.INTEGRITY
    assert _collections(db) == COLLECTION_ROWS + 1


def test_restore_non_core_data_file_refused(restorable, tmp_path):
    db, _, bdir = restorable
    plain = tmp_path / "plain.sqlite"
    conn = sqlite3.connect(plain)
    conn.execute("CREATE TABLE ZBKCOLLECTION (Z_PK INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    with pytest.raises(BackupValidationError) as info:
        write_safety.restore_library(plain, db, backup_dir=bdir)
    assert info.value.reason == BackupValidationError.NOT_CORE_DATA


def test_restore_model_mismatch_refused_unless_forced(restorable):
    db, backup, bdir = restorable
    _set_model_hash(backup, "BKCollection", UNKNOWN_HASH)
    with pytest.raises(BackupValidationError) as info:
        write_safety.restore_library(backup, db, backup_dir=bdir)
    assert info.value.reason == BackupValidationError.MODEL_MISMATCH
    assert "BKCollection" in str(info.value)
    assert _collections(db) == COLLECTION_ROWS + 1
    write_safety.restore_library(backup, db, force=True, backup_dir=bdir)
    assert _collections(db) == COLLECTION_ROWS


def test_restore_unreadable_live_identity_needs_force(restorable):
    db, backup, bdir = restorable
    _alter(db, "DELETE FROM Z_METADATA")
    with pytest.raises(BackupValidationError, match="force=True") as info:
        write_safety.restore_library(backup, db, backup_dir=bdir)
    assert info.value.reason == BackupValidationError.LIVE_UNREADABLE
    write_safety.restore_library(backup, db, force=True, backup_dir=bdir)
    assert _collections(db) == COLLECTION_ROWS


def test_restore_same_file_refused(restorable):
    db, _, bdir = restorable
    with pytest.raises(BackupValidationError) as info:
        write_safety.restore_library(db, db, backup_dir=bdir)
    assert info.value.reason == BackupValidationError.SAME_FILE


def test_restore_missing_backup(restorable, tmp_path):
    db, _, bdir = restorable
    with pytest.raises(WriteError, match="Backup file not found"):
        write_safety.restore_library(tmp_path / "gone.sqlite", db, backup_dir=bdir)


def test_restore_snapshot_failure_restores_nothing(restorable, tmp_path):
    db, backup, _ = restorable
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("")
    with pytest.raises(WriteError, match="Pre-restore snapshot failed, nothing restored"):
        write_safety.restore_library(backup, db, backup_dir=not_a_dir)
    assert _collections(db) == COLLECTION_ROWS + 1


def test_restore_at_retention_limit_keeps_source_and_snapshot(restorable):
    db, _, bdir = restorable
    for _ in range(write_safety.BACKUP_KEEP - 1):
        write_safety.backup_library(db, bdir)
    backups = write_safety.list_backups(db, bdir)
    assert len(backups) == write_safety.BACKUP_KEEP
    oldest = backups[-1]
    snap = write_safety.restore_library(oldest, db, backup_dir=bdir)
    assert oldest.exists() and snap.exists()
    assert len(write_safety.list_backups(db, bdir)) == write_safety.BACKUP_KEEP + 1


def test_restore_refused_while_books_runs(restorable, monkeypatch):
    db, backup, bdir = restorable
    monkeypatch.setattr(write_safety, "books_is_running", lambda: True)
    with pytest.raises(BooksAppRunningError):
        write_safety.restore_library(backup, db, backup_dir=bdir)
    assert _collections(db) == COLLECTION_ROWS + 1
    assert write_safety.list_backups(db, bdir) == [backup]


def test_restore_defaults_to_the_books_library(restorable, monkeypatch):
    db, backup, bdir = restorable
    monkeypatch.setattr(collection_writer, "_default_db_path", lambda **kwargs: db)
    snap = write_safety.restore_library(backup, backup_dir=bdir)
    assert _collections(db) == COLLECTION_ROWS
    assert write_safety.list_backups(backup_dir=bdir) == [snap, backup]


def test_snapshot_is_not_reused_as_pre_write_backup(restorable, monkeypatch):
    """Within BACKUP_MIN_INTERVAL the newest backup is normally reused,
    but a pre-restore snapshot holds the state *before* the restore;
    the next write needs a backup of the restored state."""
    db, backup, bdir = restorable
    snap = write_safety.restore_library(backup, db, backup_dir=bdir)
    new_id = create_collection("After restore", db_path=db, backup_dir=bdir,
                               require_books_closed=False)
    newest = write_safety.list_backups(db, bdir)[0]
    assert newest not in (snap, backup)
    assert _collections(newest) == COLLECTION_ROWS
    assert _q(newest, "SELECT COUNT(*) FROM ZBKCOLLECTION WHERE Z_PK = ?", (new_id,))[0][0] == 0


def test_list_backups_newest_first(fixture_db, tmp_path):
    bdir = tmp_path / "b"
    first = write_safety.backup_library(fixture_db, bdir)
    second = write_safety.backup_library(fixture_db, bdir)
    assert write_safety.list_backups(fixture_db, bdir) == [second, first]
    assert write_safety.list_backups(fixture_db, tmp_path / "missing") == []


def test_list_backups_default_dir_is_read_at_call_time(fixture_lib, tmp_path, monkeypatch):
    """The current user's store backs up into BACKUP_DIR, any other store
    into a folder of its own under it; BACKUP_DIR is read at call time."""
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setattr(write_safety, "BACKUP_DIR", elsewhere)
    db = fixture_lib.library_path
    made = write_safety.backup_library(db)
    assert made.parent.parent == elsewhere / "libraries"
    assert write_safety.list_backups(db) == [made]
    monkeypatch.setenv("HOME", str(fixture_lib.root))
    home_made = write_safety.backup_library(db)
    assert home_made.parent == elsewhere
    assert write_safety.list_backups(db) == [home_made]


def test_prune_removes_sidecars_and_spares_protected(fixture_db, tmp_path):
    bdir = tmp_path / "b"
    made = [write_safety._take_backup(fixture_db, bdir) for _ in range(4)]
    for path in made:
        for sidecar in ("-wal", "-shm"):
            path.with_name(path.name + sidecar).write_bytes(b"")
    stale = bdir / f"{fixture_db.stem}-20200101-000000-000000.sqlite.part"
    stale.write_bytes(b"")
    old = time.time() - write_safety.BACKUP_PART_STALE_AFTER - 60
    os.utime(stale, (old, old))

    write_safety._prune_backups(fixture_db, bdir, keep=2, protect=(made[0],))
    assert [p.exists() for p in made] == [True, False, True, True]
    assert not made[1].with_name(made[1].name + "-wal").exists()
    assert not made[1].with_name(made[1].name + "-shm").exists()
    assert made[0].with_name(made[0].name + "-wal").exists()
    assert not stale.exists()
