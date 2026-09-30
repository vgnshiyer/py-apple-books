"""Guard rails for writing to the Apple Books library database.

Feature-agnostic safety utilities shared by any write path (today:
:mod:`py_apple_books.collection_writer`):

* :func:`books_is_running` / :func:`ensure_books_not_running` — Books
  caches library rows in memory and uses Core Data optimistic locking,
  so edits made while the app runs can be overwritten or ignored. Fails
  closed: if it can't tell whether Books is running, writes refuse.
* :func:`backup_library` / :func:`list_backups` — timestamped,
  WAL-inclusive backups via the SQLite backup API. A bare file copy of
  a live WAL database misses un-checkpointed data and can itself be
  corrupt; the backup API is the documented-safe route. The current
  user's library backs up into :data:`BACKUP_DIR`, any other store into
  a folder of its own under it.
* :func:`verify_backup` / :func:`restore_library` — restore a backup
  over the live database *through SQLite* (the backup API in reverse,
  never a file copy), after checking that the backup is intact, is a
  backup of this store and was written by the same Core Data model,
  and after snapshotting the current state so the restore itself can
  be undone. A restore replaces the whole database, not just
  collections.
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

import hashlib
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional

from py_apple_books.db.client import _store, default_data_dir
from py_apple_books.db.metadata import (  # noqa: F401  (re-exported)
    StoreMetadata,
    read_only_uri,
    read_store_metadata,
)
from py_apple_books.exceptions import (
    AmbiguousStoreError,
    AppleBooksError,
    BackupValidationError,
    BooksAppRunningError,
    SchemaValidationError,
    WriteError,
)

logger = logging.getLogger(__name__)

#: Default location for pre-write backups of the current user's library
#: (the store in the Apple Books container). Any other store's backups
#: go to a folder of their own under ``libraries/`` in it.
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

#: Name suffix of the snapshot :func:`restore_library` takes before
#: overwriting the library. Such a snapshot is never reused as a
#: pre-write backup: it holds the state from *before* the restore.
SNAPSHOT_SUFFIX = "-pre-restore"

# A backup of the store ``<stem>.sqlite`` is named
# ``<stem>-<stamp>[<SNAPSHOT_SUFFIX>].sqlite``. Its series is matched on
# the whole name: a glob ``<stem>-*`` also matches the backups of a store
# whose name extends the stem (``BKLibrary-1`` and
# ``BKLibrary-1-091020131601``).
_STAMP_FORMAT = "%Y%m%d-%H%M%S-%f"
_STAMP_PATTERN = r"[0-9]{8}-[0-9]{6}-[0-9]{6}"

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


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


def _is_home_store(store_file: Optional[Path], data_dir: Optional[Path]) -> bool:
    """Whether the library store looked up as ``(store file, data dir)``
    (as :meth:`LibraryDB._source <py_apple_books.db.LibraryDB._source>`
    gives them; a None data dir is the default one) is the current
    user's: one named like Apple Books' stores in the ``BKLibrary``
    folder of the Apple Books container. Told without finding the store:
    a store found in a folder is only written if it has such a name
    (``collection_writer._store_for_writes``)."""
    store = _store("library")
    home = default_data_dir() / store.subdir
    if store_file is None:
        folder = home if data_dir is None else Path(data_dir) / store.subdir
    elif store.generation.fullmatch(Path(store_file).name):
        folder = Path(store_file).parent
    else:
        return False
    if folder == home:
        return True
    try:
        return os.path.samefile(folder, home)
    except OSError:
        return False


def _writes_home_store(db) -> bool:
    """Whether the library store ``db`` (a
    :class:`~py_apple_books.db.LibraryDB`) writes is the current user's
    (see :func:`_is_home_store`), however ``db`` names it (no argument,
    the location variables or its own)."""
    return _is_home_store(*db._source("library"))


def _own_backup_dir(path) -> Path:
    """A folder of its own, under :data:`BACKUP_DIR` (read at call
    time), for the backups of the library store ``path``."""
    key = hashlib.sha256(os.fsencode(Path(path).resolve())).hexdigest()[:16]
    return BACKUP_DIR / "libraries" / key


def _backup_dir_for(db, path) -> Path:
    """The folder the pre-write backups of ``db``'s library store
    ``path`` go to.

    :data:`BACKUP_DIR` for the current user's store (see
    :func:`_writes_home_store`), where ``PyAppleBooks()`` has always put
    them; for any other store, a folder of its own under it. Backups are
    told apart by the store's file name, which every copy of a library
    shares, so a folder per store keeps one library's backups from being
    reused or pruned as another's, whether the copy is read through an
    instance or through the location variables.
    """
    return BACKUP_DIR if _writes_home_store(db) else _own_backup_dir(path)


def _store_backup_dir(db_path) -> Path:
    """:func:`_backup_dir_for` the library store file ``db_path``: the
    folder its backups go to when no ``backup_dir`` is given."""
    db_path = Path(db_path)
    return BACKUP_DIR if _is_home_store(db_path, None) else _own_backup_dir(db_path)


def _backup_dir(backup_dir: Optional[Path], db_path: Path) -> Path:
    """``backup_dir``, else the folder ``db_path``'s backups go to
    (:func:`_store_backup_dir`; :data:`BACKUP_DIR` is read at call
    time)."""
    return Path(backup_dir) if backup_dir else _store_backup_dir(db_path)


def _series(db_path: Path, backup_dir: Path, extension: str = ".sqlite") -> list[Path]:
    """The files of ``db_path``'s backup series in ``backup_dir`` whose
    names end in ``extension``, by name (the timestamped names sort
    chronologically)."""
    pattern = re.compile(
        f"{re.escape(Path(db_path).stem)}-{_STAMP_PATTERN}"
        f"(?:{re.escape(SNAPSHOT_SUFFIX)})?{re.escape(extension)}"
    )
    try:
        names = os.listdir(backup_dir)
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(Path(backup_dir) / name for name in names if pattern.fullmatch(name))


def _backups_for(db_path: Path, backup_dir: Path) -> list[Path]:
    """Completed backups of ``db_path`` in ``backup_dir``, oldest first."""
    return _series(db_path, backup_dir)


def _take_backup(db_path: Path, backup_dir: Path, *, suffix: str = "") -> Path:
    """Write one fresh backup of ``db_path``; no reuse, no pruning.

    The copy lands under a ``.part`` name and is renamed only on
    success, so a failed backup can never masquerade as a valid one.
    ``suffix`` is ``''`` or :data:`SNAPSHOT_SUFFIX`, the names a
    store's backup series is matched by.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise WriteError(f"Backup failed, aborting write: {e}") from e
    stamp = datetime.now().strftime(_STAMP_FORMAT)
    dest = backup_dir / f"{db_path.stem}-{stamp}{suffix}.sqlite"
    part = dest.with_name(dest.name + ".part")

    try:
        src = sqlite3.connect(read_only_uri(db_path), uri=True)
    except sqlite3.Error as e:
        raise WriteError(f"Backup failed, aborting write: {e}") from e
    try:
        dst = sqlite3.connect(part)
        try:
            src.backup(dst)
        finally:
            dst.close()
        part.replace(dest)
    except (sqlite3.Error, OSError) as e:
        part.unlink(missing_ok=True)
        raise WriteError(f"Backup failed, aborting write: {e}") from e
    finally:
        src.close()
    return dest


def _prune_backups(
    db_path: Path, backup_dir: Path, keep: int, protect: Iterable[Path] = ()
) -> None:
    """Drop completed backups beyond the newest ``keep``, plus stray
    ``.part`` files a crashed run may have left behind.

    Only stale ``.part`` files: a fresh one may be another process's
    backup in flight, and files can vanish underneath us as that
    process finishes. A backup in ``protect`` is never removed.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    now = time.time()
    for stray in _series(db_path, backup_dir, ".sqlite.part"):
        try:
            if now - stray.stat().st_mtime > BACKUP_PART_STALE_AFTER:
                stray.unlink()
        except FileNotFoundError:
            pass
    protected = {Path(p).resolve() for p in protect if p}
    for old in _backups_for(db_path, backup_dir)[:-keep]:
        if old.resolve() in protected:
            continue
        old.unlink(missing_ok=True)
        # Opening a WAL-mode backup leaves -wal/-shm sidecars behind.
        for sidecar in ("-wal", "-shm"):
            old.with_name(old.name + sidecar).unlink(missing_ok=True)


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

    :param backup_dir: Where the backup goes. By default
        :data:`BACKUP_DIR` for the current user's library (the store in
        the Apple Books container), and a folder of its own under it
        for any other store.
    :param min_interval: If the newest existing backup is younger than
        this many seconds, reuse it instead of taking another. Zero
        (the default) always takes a fresh backup. A pre-restore
        snapshot is never reused.
    """
    db_path = Path(db_path)
    backup_dir = _backup_dir(backup_dir, db_path)
    backup_dir.mkdir(parents=True, exist_ok=True)

    existing = _backups_for(db_path, backup_dir)
    if min_interval > 0 and existing:
        newest = existing[-1]
        if (
            not newest.stem.endswith(SNAPSHOT_SUFFIX)
            and time.time() - newest.stat().st_mtime < min_interval
        ):
            return newest

    dest = _take_backup(db_path, backup_dir)
    _prune_backups(db_path, backup_dir, keep)
    return dest


def list_backups(
    db_path: Optional[Path] = None, backup_dir: Optional[Path] = None
) -> list[Path]:
    """Backups of the library database, newest first.

    ``db_path`` defaults to the Books library (the store the location
    variables name, else the current user's; its canonical file when
    present, even if damaged). ``backup_dir`` defaults to where that
    store's backups go (see :func:`backup_library`). The first entry is
    the restore point for the most recent write (writes within
    :data:`BACKUP_MIN_INTERVAL` of each other share the backup taken
    before the first of them) or, right after a restore, the snapshot
    that undoes it. Returns ``[]`` if the directory doesn't exist.

    :raises AmbiguousStoreError: no ``db_path``, and the Books library
        can't be told for sure (several candidate stores).
    """
    db_path = Path(db_path) if db_path else _default_library_path(unreadable_ok=True)
    backup_dir = _backup_dir(backup_dir, db_path)
    if not backup_dir.is_dir():
        return []
    return _backups_for(db_path, backup_dir)[::-1]


def _default_library_path(*, unreadable_ok: bool = False) -> Path:
    """The Books library's store (``collection_writer._default_db_path``);
    with ``unreadable_ok``, its canonical file when that is present but
    fails validation (busy or damaged)."""
    # Imported lazily: collection_writer imports this module.
    from py_apple_books.collection_writer import _default_db_path

    return _default_db_path(unreadable_ok=unreadable_ok)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def verify_backup(backup_path: Path, db_path: Path) -> None:
    """Check that ``backup_path`` can safely replace ``db_path``.

    Raises :class:`BackupValidationError` (see its ``reason``) unless
    the backup opens as an SQLite database, passes ``PRAGMA
    quick_check``, has Core Data metadata with the same store UUID as
    the live library (a backup of *this* store, not the annotations
    store or another Mac's library), and records the same Core Data
    model hashes. The live library is only read.
    """
    backup_path = Path(backup_path)
    db_path = Path(db_path)
    error = BackupValidationError
    name = backup_path.name

    try:
        src = sqlite3.connect(read_only_uri(backup_path), uri=True)
    except sqlite3.Error as e:
        raise error(f"Backup {name} can't be opened: {e}", error.NOT_A_DATABASE) from e
    try:
        try:
            rows = src.execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError as e:
            raise error(
                f"{name} is not a readable SQLite database: {e}", error.NOT_A_DATABASE
            ) from e
        result = [row[0] for row in rows]
        if result != ["ok"]:
            raise error(
                f"Backup {name} failed its integrity check ({'; '.join(map(str, result[:3]))}).",
                error.INTEGRITY,
            )
        backup_meta = read_store_metadata(src)
    finally:
        src.close()
    if backup_meta is None or not backup_meta.uuid:
        raise error(
            f"{name} is not a Core Data store (no usable Z_METADATA), so it "
            "isn't a backup of the Books library.",
            error.NOT_CORE_DATA,
        )

    live_meta = read_store_metadata(db_path)
    if live_meta is None or not live_meta.uuid:
        raise error(
            f"Can't read the live library's store identity ({db_path.name}) to "
            "check the backup against. If you are restoring over a damaged "
            "library, call restore_library(..., force=True), with "
            "db_path=<the library's store file> if it isn't the default library.",
            error.LIVE_UNREADABLE,
        )

    if backup_meta.uuid != live_meta.uuid:
        raise error(
            f"{name} is a backup of a different store (store UUID "
            f"{backup_meta.uuid}; the live library is {live_meta.uuid}), such "
            "as the annotations store or another library.",
            error.WRONG_STORE,
        )

    if backup_meta.model_hashes != live_meta.model_hashes:
        changed = sorted(
            entity
            for entity in set(backup_meta.model_hashes) | set(live_meta.model_hashes)
            if backup_meta.model_hashes.get(entity) != live_meta.model_hashes.get(entity)
        )
        raise error(
            f"{name} was written by a different Apple Books data model "
            f"(entities that differ: {', '.join(changed)}), probably before a "
            "macOS or Books update. Restoring it could leave Books unable to "
            "open the library. Pass force=True only if you know it is safe.",
            error.MODEL_MISMATCH,
        )


def _default_restore_target(force: bool, snapshot: bool) -> Path:
    """:func:`restore_library`'s default ``db_path``.

    Restoring over a damaged library (``force``, or no ``snapshot``)
    takes the canonical store file even if it can't be read. Otherwise
    that store is refused, and the refusal says how to restore over it.
    """
    damaged_ok = force or not snapshot
    try:
        return _default_library_path(unreadable_ok=damaged_ok)
    except AmbiguousStoreError as e:
        refused = e
    if not damaged_ok:
        try:
            _default_library_path(unreadable_ok=True)
        except AppleBooksError:
            pass
        else:
            # The canonical store is there but can't be read.
            raise AmbiguousStoreError(
                f"{refused} To restore over it if it is damaged, call "
                "restore_library(..., force=True)."
            ) from refused
    raise refused


def restore_library(
    backup_path: Path,
    db_path: Optional[Path] = None,
    *,
    force: bool = False,
    snapshot: bool = True,
    backup_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Restore a backup over the live library database.

    The whole database is replaced: collections, and also every book
    added and all reading progress recorded since the backup was taken.

    Restores *through SQLite* — the backup API in reverse — rather
    than copying files. A filesystem copy plus sidecar deletion is
    documented-unsafe while any connection holds the database open,
    and Books' helper daemons (plus this package's own read
    connections) always do: their stale WAL handles would silently
    replay pre-restore pages over the copied file. The backup API
    takes proper locks, resets the WAL consistently, and other
    connections simply see the restored content on their next read.

    Before overwriting anything it refuses while Books.app is running
    (Books caches rows in memory far above the SQLite layer), runs
    :func:`verify_backup` unless ``force``, and snapshots the current
    library, so the restore itself can be undone by restoring the
    returned snapshot.

    :param db_path: The library database to overwrite; defaults to the
        Books library (the store the location variables name, else the
        current user's), found as for a write. With ``force`` or without
        ``snapshot`` that is its canonical file when present, even if it
        can't be read; otherwise a store that can't be read is refused.
    :param force: Skip :func:`verify_backup`, e.g. to restore over a
        damaged library or across a Books data-model change.
    :param snapshot: Snapshot the current library first. Turn it off
        only when the live file can't be read at all.
    :param backup_dir: Where the snapshot goes; defaults to where the
        library's backups go (see :func:`backup_library`).
    :return: The snapshot's path, or None with ``snapshot=False``.
    :raises BackupValidationError: a pre-restore check failed; nothing
        was changed.
    :raises BooksAppRunningError: Books is running; nothing was changed.
    :raises AmbiguousStoreError: no ``db_path``, and the Books library
        can't be told for sure (several candidate stores) or, with
        ``snapshot`` and without ``force``, can't be read; nothing was
        changed.
    :raises WriteError: the backup is missing, the snapshot failed
        (nothing was changed), or the restore itself failed.
    """
    backup_path = Path(backup_path)
    db_path = Path(db_path) if db_path else _default_restore_target(force, snapshot)
    if not backup_path.exists():
        raise WriteError(f"Backup file not found: {backup_path}")
    if db_path.exists() and os.path.samefile(backup_path, db_path):
        raise BackupValidationError(
            f"{backup_path} is the live library itself, not a backup of it.",
            BackupValidationError.SAME_FILE,
        )

    ensure_books_not_running()

    if not force:
        verify_backup(backup_path, db_path)

    backup_dir = _backup_dir(backup_dir, db_path)
    snap = None
    if snapshot:
        try:
            snap = _take_backup(db_path, backup_dir, suffix=SNAPSHOT_SUFFIX)
        except WriteError as e:
            raise WriteError(
                f"Pre-restore snapshot failed, nothing restored: {e.__cause__ or e}"
            ) from e
    undo = f" (pre-restore snapshot: {snap})" if snap else ""

    try:
        src = sqlite3.connect(read_only_uri(backup_path), uri=True)
    except sqlite3.Error as e:
        raise WriteError(f"Restore failed: {e}{undo}") from e
    try:
        dst = sqlite3.connect(db_path, timeout=5.0)
        try:
            src.backup(dst)
            result = [row[0] for row in dst.execute("PRAGMA quick_check").fetchall()]
        finally:
            dst.close()
    except sqlite3.Error as e:
        raise WriteError(f"Restore failed: {e}{undo}") from e
    finally:
        src.close()
    if result != ["ok"]:
        message = (
            "The restored library failed its integrity check "
            f"({'; '.join(map(str, result[:3]))})."
        )
        if snap:
            message += f" Restore the pre-restore snapshot {snap} to go back."
        raise WriteError(message)

    # The restore has happened; a failed cleanup mustn't report otherwise.
    try:
        _prune_backups(db_path, backup_dir, BACKUP_KEEP, protect=(backup_path, snap))
    except OSError as e:
        logger.warning("Restored, but pruning old backups in %s failed: %s", backup_dir, e)
    return snap


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
