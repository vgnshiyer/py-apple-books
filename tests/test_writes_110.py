"""1.10 write-path hardening: the exact pre-write schema check and its
escape hatch (F37), and lock contention (LibraryBusyError).

Synthetic libraries only (the ``fixture_lib`` of
``test_collection_writer``); Books.app is never checked for real.
"""

import base64
import logging
import plistlib
import sqlite3
import sys

import pytest

from py_apple_books import collection_writer, write_safety
from py_apple_books.collection_writer import (
    WriteSession,
    add_book_to_collection,
    create_collection,
)
from py_apple_books.exceptions import (
    LibraryBusyError,
    SchemaValidationError,
    WriteError,
)

from tests.test_collection_writer import (  # noqa: F401  (fixtures)
    COLLECTION_ROWS,
    FINANCE,
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
    return add_book_to_collection(FINANCE, OTHER_BOOK, **_session_kwargs(db), **kwargs)


DRIFT_CASES = [
    pytest.param("ZBKCOLLECTION", _create, id="collection"),
    pytest.param("ZBKCOLLECTIONMEMBER", _add, id="member"),
]


def _inserted_value(db, table, column):
    if table == "ZBKCOLLECTION":
        sql = f"SELECT {column} FROM ZBKCOLLECTION WHERE ZTITLE = 'Drift'"
    else:
        sql = f"SELECT {column} FROM ZBKCOLLECTIONMEMBER WHERE ZCOLLECTION = {FINANCE}"
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
