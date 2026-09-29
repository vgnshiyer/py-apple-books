"""Guard rails for writing to the Apple Books library database.

Feature-agnostic safety utilities shared by any write path (today:
:mod:`py_apple_books.collection_writer`):

* :func:`books_is_running` / :func:`ensure_books_not_running` — Books
  caches library rows in memory and uses Core Data optimistic locking,
  so edits made while the app runs can be overwritten or ignored. Fails
  closed: if it can't tell whether Books is running, writes refuse.
* :func:`backup_library` — timestamped, WAL-inclusive backup via the
  SQLite backup API. A bare file copy of a live WAL database misses
  un-checkpointed data and can itself be corrupt; the backup API is the
  documented-safe route.
* :func:`restore_library` — one-command restore of a backup over the
  live database (with the WAL/SHM sidecars removed so SQLite doesn't
  replay stale journal pages over the restored file).
* :func:`validate_table_columns` — exact check (column names and
  declared types) for the tables the writer inserts into. Core Data
  never declares NOT NULL, so a new mandatory attribute only shows up
  as a column this version doesn't know.
* :func:`validate_table_schema` — presence check for the tables the
  writer only reads.
* :func:`check_model_hashes` — compares Core Data's per-entity model
  version hashes against the ones this version was verified with;
  :data:`MODEL_CHECK_ENV` selects whether a mismatch warns, refuses or
  is ignored.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional

from py_apple_books.db.metadata import (  # noqa: F401  (re-exported)
    StoreMetadata,
    read_only_uri,
    read_store_metadata,
)
from py_apple_books.exceptions import (
    BooksAppRunningError,
    SchemaValidationError,
    WriteError,
)

logger = logging.getLogger(__name__)

#: Default location for pre-write backups.
BACKUP_DIR = Path.home() / ".py_apple_books" / "backups"

#: How many backups to retain per database (oldest pruned first).
BACKUP_KEEP = 10

#: Reuse the newest backup instead of taking another when it's younger
#: than this many seconds. Protects the pre-batch restore point: a
#: burst of writes (e.g. "add 30 books to a collection") would
#: otherwise rotate away the one backup that predates the whole batch.
BACKUP_MIN_INTERVAL = 300.0

#: Leftover ``.part`` files are only pruned once older than this many
#: seconds. A younger one may be another process's backup still in
#: progress; deleting it would make that process's write abort.
BACKUP_PART_STALE_AFTER = 600.0

#: Environment variable choosing what the writer does about schema
#: drift it can't vouch for: ``warn`` (the default: log an unknown Core
#: Data model hash and write), ``enforce`` (refuse on an unknown hash)
#: or ``off`` (skip the hash check, and allow unknown *nullable* columns
#: in the tables the writer inserts into, leaving them empty). Missing
#: or retyped columns refuse in every mode.
MODEL_CHECK_ENV = "APPLE_BOOKS_MODEL_CHECK"
MODEL_CHECK_DEFAULT = "warn"
_MODEL_CHECK_MODES = ("warn", "enforce", "off")

_REPORT_HINT = (
    "Please report your schema: python -m py_apple_books.testing.dump_schema "
    "--out ~/Desktop/apple-books-schema"
)


def books_is_running() -> bool:
    """True if the Apple Books app itself is currently running.

    Only the app process matters: its helper daemons (BKAgentService,
    bookassetd) stay resident permanently and holding writes for them
    would mean never writing at all — transient lock contention with
    the daemons is handled by the write transaction's busy timeout
    instead.

    Fails closed, raising :class:`WriteError` when it can't tell: off
    macOS (a Docker/Linux process can't see the host's Books.app, so a
    process check there would silently pass) or when ``pgrep`` is
    missing, hangs, or errors. Deliberately not
    :class:`BooksAppRunningError` — "quit Books" would be wrong advice.
    """
    if sys.platform != "darwin":
        raise WriteError(
            "Collection writes are only supported when running directly "
            "on macOS (not in Docker/Linux), because Apple Books can't be "
            "verified closed."
        )
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Books"], capture_output=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WriteError(
            f"Could not verify Apple Books is closed; refusing to write. ({e})"
        )
    # pgrep: 0 = matched, 1 = no match, 2+ = usage/internal error.
    if result.returncode not in (0, 1):
        raise WriteError(
            "Could not verify Apple Books is closed; refusing to write. "
            f"(pgrep exited with status {result.returncode})"
        )
    return result.returncode == 0


def ensure_books_not_running() -> None:
    """Raise :class:`BooksAppRunningError` if Books is open.

    Propagates :class:`WriteError` from :func:`books_is_running` when
    that can't be determined.
    """
    if books_is_running():
        raise BooksAppRunningError(
            "Apple Books is running. Quit the Books app (Cmd-Q) before "
            "modifying the library, then retry. Books caches library rows "
            "in memory, so edits made while it runs may be overwritten or "
            "not appear until relaunch."
        )


def backup_library(
    db_path: Path,
    backup_dir: Optional[Path] = None,
    keep: int = BACKUP_KEEP,
    min_interval: float = 0.0,
) -> Path:
    """Take a timestamped backup of ``db_path`` and prune old ones.

    Uses the SQLite online backup API so the copy includes all
    committed data even when the source is in WAL mode with a pending
    checkpoint. The copy lands under a ``.part`` name and is renamed
    only on success, so a failed backup can never masquerade as a
    valid one. Returns the backup file's path.

    :param min_interval: If the newest existing backup is younger than
        this many seconds, reuse it instead of taking another. Zero
        (the default) always takes a fresh backup.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir) if backup_dir else BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(backup_dir.glob(f"{db_path.stem}-*.sqlite"))
    if min_interval > 0 and existing:
        newest = existing[-1]
        if time.time() - newest.stat().st_mtime < min_interval:
            return newest

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = backup_dir / f"{db_path.stem}-{stamp}.sqlite"
    part = dest.with_name(dest.name + ".part")

    try:
        src = sqlite3.connect(read_only_uri(db_path), uri=True)
    except sqlite3.Error as e:
        raise WriteError(f"Backup failed, aborting write: {e}")
    try:
        dst = sqlite3.connect(part)
        try:
            src.backup(dst)
        finally:
            dst.close()
        part.replace(dest)
    except (sqlite3.Error, OSError) as e:
        part.unlink(missing_ok=True)
        raise WriteError(f"Backup failed, aborting write: {e}")
    finally:
        src.close()

    # Prune: completed backups beyond the retention count, plus any
    # stray .part files a crashed run may have left behind. Only stale
    # ones — a fresh .part may be another process's backup in flight —
    # and files can vanish underneath us as that process finishes.
    now = time.time()
    for stray in backup_dir.glob(f"{db_path.stem}-*.sqlite.part"):
        try:
            if now - stray.stat().st_mtime > BACKUP_PART_STALE_AFTER:
                stray.unlink()
        except FileNotFoundError:
            pass
    backups = sorted(backup_dir.glob(f"{db_path.stem}-*.sqlite"))
    for old in backups[:-keep]:
        old.unlink(missing_ok=True)

    return dest


def restore_library(backup_path: Path, db_path: Path) -> None:
    """Restore a backup over the live library database.

    Restores *through SQLite* — the backup API in reverse — rather
    than copying files. A filesystem copy plus sidecar deletion is
    documented-unsafe while any connection holds the database open,
    and Books' helper daemons (plus this package's own read
    connections) always do: their stale WAL handles would silently
    replay pre-restore pages over the copied file. The backup API
    takes proper locks, resets the WAL consistently, and other
    connections simply see the restored content on their next read.

    Still refuses while Books.app itself is running, since it caches
    rows in memory far above the SQLite layer.
    """
    backup_path = Path(backup_path)
    db_path = Path(db_path)
    if not backup_path.exists():
        raise WriteError(f"Backup file not found: {backup_path}")

    ensure_books_not_running()

    try:
        src = sqlite3.connect(read_only_uri(backup_path), uri=True)
    except sqlite3.Error as e:
        raise WriteError(f"Restore failed: {e}")
    try:
        dst = sqlite3.connect(db_path, timeout=5.0)
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as e:
        raise WriteError(f"Restore failed: {e}")
    finally:
        src.close()


# ---------------------------------------------------------------------------
# Schema and model checks
# ---------------------------------------------------------------------------


def resolve_model_check(mode: Optional[str] = None) -> str:
    """The effective model-check mode: ``mode`` if given, else the
    :data:`MODEL_CHECK_ENV` environment variable, else
    :data:`MODEL_CHECK_DEFAULT`.

    Case-insensitive. An unrecognised value logs a warning and counts
    as the default, ``'warn'``.
    """
    if mode is None:
        mode = os.environ.get(MODEL_CHECK_ENV, "")
    value = str(mode).strip().lower()
    if not value:
        return MODEL_CHECK_DEFAULT
    if value in _MODEL_CHECK_MODES:
        return value
    logger.warning(
        "Unrecognised model check mode %r (expected warn, enforce or off; "
        "set via %s or model_check=); using %r.",
        mode, MODEL_CHECK_ENV, MODEL_CHECK_DEFAULT,
    )
    return MODEL_CHECK_DEFAULT


def check_model_hashes(
    conn: sqlite3.Connection,
    known: Mapping[str, Iterable[str]],
    mode: Optional[str] = None,
) -> dict:
    """Compare the store's Core Data model hashes with verified ones.

    ``known`` maps each entity to check to the base64
    ``NSStoreModelVersionHashes`` values it was verified with; other
    entities are never checked. A store without readable ``Z_METADATA``
    counts as unknown.

    On an unknown hash, ``mode`` (resolved by :func:`resolve_model_check`)
    decides: ``'warn'`` logs one warning naming each entity's hash and
    the Core Data framework version, ``'enforce'`` raises
    :class:`SchemaValidationError` with the same details, and ``'off'``
    skips the check without reading anything.

    :return: ``{entity: observed hash or None}`` for the entities in
        ``known``; ``{}`` when the check is off.
    """
    mode = resolve_model_check(mode)
    if mode == "off":
        return {}
    meta = read_store_metadata(conn)
    observed = {
        entity: (meta.model_hashes.get(entity) if meta else None) for entity in known
    }
    unknown = sorted(
        entity for entity, value in observed.items() if value not in known[entity]
    )
    if not unknown:
        return observed

    detail = ", ".join(f"{entity}={observed[entity] or 'unavailable'}" for entity in unknown)
    framework = (meta.framework_version if meta else None) or "unknown"
    message = (
        "The library's Core Data model differs from the one this version of "
        f"py-apple-books was verified against ({detail}; "
        f"NSPersistenceFrameworkVersion {framework})."
    )
    if mode == "enforce":
        raise SchemaValidationError(
            f"{message} Writes are refused because {MODEL_CHECK_ENV}=enforce. "
            f"Check for a newer py-apple-books; to write anyway, set "
            f"{MODEL_CHECK_ENV}=warn. {_REPORT_HINT}"
        )
    logger.warning(
        "%s Writing anyway (%s=warn; set it to enforce to refuse). %s",
        message, MODEL_CHECK_ENV, _REPORT_HINT,
    )
    return observed


def validate_table_columns(
    conn: sqlite3.Connection,
    table: str,
    expected: Mapping[str, str],
    *,
    allow_extra_nullable: bool = False,
) -> None:
    """Exact check: ``table``'s columns must be ``expected``.

    ``expected`` maps column name to declared type; names and types
    compare case-insensitively. Every attribute Core Data adds to an
    entity becomes a column, and a type change rewrites the declared
    type, so this catches the drift that would make the writer's
    INSERTs incomplete. A missing or retyped column always raises
    :class:`SchemaValidationError`. So does an unknown extra column,
    unless ``allow_extra_nullable``: then an extra column that is
    neither NOT NULL nor part of the primary key is allowed (the
    writer leaves it NULL) and one warning names the allowed columns.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise SchemaValidationError(
            f"Table {table} not found in the library database — "
            "the schema has changed and writes are not safe."
        )

    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
    actual = {row[1].upper(): row for row in rows}
    want = {name.upper(): (name, (decl or "").upper()) for name, decl in expected.items()}

    missing = [name for key, (name, _) in want.items() if key not in actual]
    retyped = [
        f"{name} {decl}->{(actual[key][2] or '').upper()}"
        for key, (name, decl) in want.items()
        if key in actual and (actual[key][2] or "").upper() != decl
    ]
    unknown, tolerated = [], []
    for key, row in actual.items():
        if key in want:
            continue
        if allow_extra_nullable and not row[3] and not row[5]:
            tolerated.append(row[1])
        elif row[3] or row[5]:
            unknown.append(f"{row[1]} ({'PRIMARY KEY' if row[5] else 'NOT NULL'})")
        else:
            unknown.append(row[1])

    if unknown or missing or retyped:
        parts = []
        if unknown:
            parts.append(f"unknown column(s) {unknown}")
        if missing:
            parts.append(f"missing {missing}")
        if retyped:
            parts.append(f"retyped {retyped}")
        raise SchemaValidationError(
            f"Table {table} doesn't match the Apple Books schema this version "
            f"was verified against ({'; '.join(parts)}) — writes are not safe. "
            "Check for a newer py-apple-books. If Apple Books only added new "
            f"optional columns, you can set {MODEL_CHECK_ENV}=off to allow "
            "writes that leave them empty (missing or changed columns still "
            f"refuse). {_REPORT_HINT}"
        )
    if tolerated:
        logger.warning(
            "Table %s has column(s) %s this version of py-apple-books wasn't "
            "verified against; writing anyway because %s=off, leaving them "
            "empty (NULL).",
            table, tolerated, MODEL_CHECK_ENV,
        )


def validate_table_schema(
    conn: sqlite3.Connection,
    table: str,
    required_columns: Iterable[str],
    writable_columns: Optional[Iterable[str]] = None,
) -> None:
    """Presence check: abort if ``table`` is missing or lacks any of
    ``required_columns``. Extra columns are fine.

    With ``writable_columns``, also abort on a NOT NULL column outside
    it. Core Data never declares NOT NULL, so that branch can't detect
    Core Data model drift; use :func:`validate_table_columns` for the
    tables the writer inserts into.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise SchemaValidationError(
            f"Table {table} not found in the library database — "
            "the schema has changed and writes are not safe."
        )

    names = {row[1] for row in rows}
    missing = set(required_columns) - names
    if missing:
        raise SchemaValidationError(
            f"Table {table} is missing expected column(s) {sorted(missing)} — "
            "the schema has changed and writes are not safe."
        )

    if writable_columns is None:
        return
    not_null = {row[1] for row in rows if row[3]}
    unknown_required = not_null - set(writable_columns)
    if unknown_required:
        raise SchemaValidationError(
            f"Table {table} has NOT NULL column(s) {sorted(unknown_required)} "
            "this version doesn't know how to populate — writes are not safe. "
            "Update py-apple-books."
        )
