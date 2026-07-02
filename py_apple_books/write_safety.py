"""Guard rails for writing to the Apple Books library database.

Feature-agnostic safety utilities shared by any write path (today:
:mod:`py_apple_books.collection_writer`):

* :func:`books_is_running` / :func:`ensure_books_not_running` — Books
  caches library rows in memory and uses Core Data optimistic locking,
  so edits made while the app runs can be overwritten or ignored.
* :func:`backup_library` — timestamped, WAL-inclusive backup via the
  SQLite backup API. A bare file copy of a live WAL database misses
  un-checkpointed data and can itself be corrupt; the backup API is the
  documented-safe route.
* :func:`restore_library` — one-command restore of a backup over the
  live database (with the WAL/SHM sidecars removed so SQLite doesn't
  replay stale journal pages over the restored file).
* :func:`validate_table_schema` — abort-don't-guess check that a table
  still looks the way the writer expects, so a macOS schema change
  fails loudly instead of writing wrong rows.
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from py_apple_books.db.client import _read_only_uri
from py_apple_books.exceptions import (
    BooksAppRunningError,
    SchemaValidationError,
    WriteError,
)

#: Default location for pre-write backups.
BACKUP_DIR = Path.home() / ".py_apple_books" / "backups"

#: How many backups to retain per database (oldest pruned first).
BACKUP_KEEP = 10

#: Reuse the newest backup instead of taking another when it's younger
#: than this many seconds. Protects the pre-batch restore point: a
#: burst of writes (e.g. "add 30 books to a collection") would
#: otherwise rotate away the one backup that predates the whole batch.
BACKUP_MIN_INTERVAL = 300.0


def books_is_running() -> bool:
    """True if the Apple Books app itself is currently running.

    Only the app process matters: its helper daemons (BKAgentService,
    bookassetd) stay resident permanently and holding writes for them
    would mean never writing at all — transient lock contention with
    the daemons is handled by the write transaction's busy timeout
    instead.
    """
    result = subprocess.run(["pgrep", "-x", "Books"], capture_output=True)
    return result.returncode == 0


def ensure_books_not_running() -> None:
    """Raise :class:`BooksAppRunningError` if Books is open."""
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
        src = sqlite3.connect(_read_only_uri(db_path), uri=True)
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
    # stray .part files a crashed run may have left behind.
    for stray in backup_dir.glob(f"{db_path.stem}-*.sqlite.part"):
        stray.unlink(missing_ok=True)
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
        src = sqlite3.connect(_read_only_uri(backup_path), uri=True)
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


def validate_table_schema(
    conn: sqlite3.Connection,
    table: str,
    required_columns: set[str],
    writable_columns: set[str],
) -> None:
    """Abort if ``table`` no longer matches what the writer maintains.

    Two checks:

    * every column the writer populates must still exist;
    * every NOT NULL column must be one the writer knows how to fill —
      a new required column added by a macOS update means our INSERTs
      would produce rows Books considers invalid, so fail loudly.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise SchemaValidationError(
            f"Table {table} not found in the library database — "
            "the schema has changed and writes are not safe."
        )

    names = {row[1] for row in rows}
    missing = required_columns - names
    if missing:
        raise SchemaValidationError(
            f"Table {table} is missing expected column(s) {sorted(missing)} — "
            "the schema has changed and writes are not safe."
        )

    not_null = {row[1] for row in rows if row[3]}
    unknown_required = not_null - writable_columns
    if unknown_required:
        raise SchemaValidationError(
            f"Table {table} has NOT NULL column(s) {sorted(unknown_required)} "
            "this version doesn't know how to populate — writes are not safe. "
            "Update py-apple-books."
        )
