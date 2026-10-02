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
  a folder of its own under it. Backups and restores lock the backup
  folder (threads and processes alike), so a burst of concurrent writes
  shares one backup and never prunes another's restore point.
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

import contextlib
import errno
import hashlib
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - not on macOS or Linux
    fcntl = None

from py_apple_books.db import client as _client
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
    LibraryBusyError,
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

#: How long, in seconds, a backup or restore waits for another one
#: working in the same backup folder (in this process or another) before
#: giving up with :class:`LibraryBusyError`. Read at call time; an
#: enclosing :func:`py_apple_books.db.query_deadline` that ends sooner
#: shortens the wait.
BACKUP_LOCK_TIMEOUT = 15.0

_LOCK_BUSY_MESSAGE = (
    "Another write is backing up or restoring this library; nothing was "
    "changed. Try again in a moment."
)

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
# Backup folder lock
# ---------------------------------------------------------------------------
#
# One lock per backup folder for threads and processes alike: a thread
# lock from the registry below, then an exclusive flock(2) on the folder
# itself (a descriptor opened on the directory, so no lock file is ever
# created). A flock belongs to the open file description, which a forked
# child shares: if the child kept its copy, the lock would outlive this
# process's release. So every folder descriptor is recorded, and the
# fork hooks close the child's copies (never LOCK_UN, which would
# release this process's lock as well) and give the child a fresh
# registry. Where flock isn't available (no fcntl, a folder that can't
# be opened, a file system without flock) the thread lock alone
# serializes this process.
#
# A folder is identified by its device and inode, read from the
# descriptor the flock is taken on: one folder can have many spellings
# (a symlink, another letter case on a case-insensitive volume, a
# firmlink such as /System/Volumes/Data/...), and two descriptors on it
# in one process would conflict with each other. Only a folder that
# can't be looked at is identified by its real path. Folders are locked
# in the order of these keys, the same in every process, so two callers
# can't deadlock.
#
# The lock never takes an SQLite or LibraryDB lock.


class _FolderLock:
    """Registry entry for one backup folder: its thread lock, the thread
    holding it, and how many holds hold or wait for it (the entry is
    dropped when the last one leaves, so the registry only has the
    folders in use)."""

    __slots__ = ("lock", "owner", "users")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.owner: Optional[int] = None
        self.users = 0


# Guards the registry and the set of recorded folder descriptors. Held
# for dict and set operations, around closing a recorded descriptor, so
# that "close, then forget" is atomic for a fork (the before-fork hook
# takes it), and, only after a fork raced an open, around opening one
# again (see _open_folder). A fork, or another folder's lock, waits
# while it is held: closing a directory descriptor doesn't wait on the
# disk, but releasing a flock on a network volume may take a round trip.
_registry_guard = threading.Lock()
_registry: dict = {}
_held_fds: set = set()
# Bumped in a forked child: a hold taken before the fork releases
# nothing there (its descriptor is already closed, its lock replaced).
_generation = 0
# Bumped after every fork, in the parent and the child: a folder opened
# while it changed may have a copy in a child that isn't recorded.
_forks = 0


def _before_fork() -> None:
    _registry_guard.acquire()


def _after_fork_in_parent() -> None:
    global _forks
    _forks += 1
    _registry_guard.release()


def _after_fork_in_child() -> None:
    global _registry_guard, _registry, _held_fds, _generation, _forks
    held = _held_fds
    _registry_guard = threading.Lock()
    _registry = {}
    _held_fds = set()
    _generation += 1
    _forks += 1
    for fd in held:
        try:
            os.close(fd)
        except OSError:
            pass


# Registered once per module object: a reload re-runs this code in the
# same namespace, and a second before-fork hook would wait on the lock
# the first one took.
if hasattr(os, "register_at_fork") and not globals().get("_fork_hooks_registered"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_in_parent,
        after_in_child=_after_fork_in_child,
    )
    _fork_hooks_registered = True

# Polling interval while another process holds a folder's flock: from
# 20 ms, growing to 50 ms.
_POLL_FIRST = 0.02
_POLL_STEP = 0.01
_POLL_MAX = 0.05


def _errno_name(e: OSError) -> str:
    return errno.errorcode.get(e.errno, str(e.errno))


def _lock_deadline(timeout: Optional[float]) -> float:
    """The ``time.monotonic()`` value a lock wait ends at: ``timeout``
    seconds from now (None: :data:`BACKUP_LOCK_TIMEOUT`, read now; None
    there too: no limit), or the end of the enclosing
    :func:`~py_apple_books.db.query_deadline` if that is sooner."""
    limit = BACKUP_LOCK_TIMEOUT if timeout is None else timeout
    now = time.monotonic()
    if limit is None:
        at = math.inf
    else:
        limit = float(limit)
        at = now + limit if limit > 0 else now  # negative or NaN: no wait
    context = _client._deadline.get()
    if context is not None and context[0] < at:
        at = context[0]
    return at


def _acquire_thread_lock(lock: threading.Lock, deadline: float) -> bool:
    """Take ``lock`` by ``deadline``; past it, one attempt without
    waiting."""
    remaining = deadline - time.monotonic()
    if remaining > 0:
        return lock.acquire(timeout=min(remaining, threading.TIMEOUT_MAX))
    return lock.acquire(blocking=False)


_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_folder(path: str) -> Optional[int]:
    """A descriptor on the folder ``path``, recorded so that a forked
    child closes its copy; None without fcntl or when the folder can't
    be opened (the reason is logged at DEBUG, without the path).

    The folder is opened outside the registry guard, so a slow open (a
    stale network mount, say) holds up neither forks nor other folders'
    locks. A fork during that open may have left the child an
    unrecorded copy, which would keep a flock taken on it alive; then
    the descriptor is closed unused and the folder opened again under
    the guard.
    """
    if fcntl is None:
        logger.debug("Backup folder lock: no fcntl here; using a thread lock only.")
        return None
    with _registry_guard:
        forks = _forks
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError as e:
        logger.debug(
            "Backup folder lock: can't open the folder (%s); using a thread lock only.",
            _errno_name(e),
        )
        return None
    with _registry_guard:
        if forks == _forks:
            _held_fds.add(fd)
            return fd
    os.close(fd)
    with _registry_guard:
        try:
            fd = os.open(path, _OPEN_FLAGS)
        except OSError as e:
            logger.debug(
                "Backup folder lock: can't open the folder (%s); using a thread lock only.",
                _errno_name(e),
            )
            return None
        _held_fds.add(fd)
        return fd


def _folder_key(path: str, fd: Optional[int]) -> tuple:
    """What identifies the folder ``path`` (open as ``fd``, or None) in
    every thread and process, however it is spelled: its device and
    inode, or, for a folder that can't be looked at (missing, say), its
    real path."""
    try:
        st = os.fstat(fd) if fd is not None else os.stat(path)
    except OSError:
        return ("path", os.path.realpath(path))
    return ("inode", st.st_dev, st.st_ino)


def _close_folder_fd(fd: Optional[int], generation: int) -> None:
    """Close (and so unlock) a folder descriptor this process recorded,
    unless a fork has happened since (the child closed it already, and
    the number may name another file by now)."""
    if fd is None:
        return
    with _registry_guard:
        if generation != _generation or fd not in _held_fds:
            return
        _held_fds.discard(fd)
        try:
            os.close(fd)
        except OSError:
            pass


def _flock_fd(fd: int, deadline: float) -> bool:
    """An exclusive flock on the folder descriptor ``fd``, polled until
    ``deadline`` after at least one attempt: True once taken, False
    where the file system doesn't support flock (logged at DEBUG).

    :raises LibraryBusyError: another process held it past the deadline.
    """
    pause = _POLL_FIRST
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                # ENOTSUP, EOPNOTSUPP, ENOLCK (network file systems) and
                # the like: no flock on this folder.
                logger.debug(
                    "Backup folder lock: flock is unavailable (%s); using a "
                    "thread lock only.",
                    _errno_name(e),
                )
                return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LibraryBusyError(_LOCK_BUSY_MESSAGE)
        time.sleep(min(pause, remaining))
        pause = min(pause + _POLL_STEP, _POLL_MAX)


def _leave_registry(key: tuple, state: _FolderLock, generation: int) -> None:
    """One hold of ``key`` is over: drop its registry entry if no other
    hold holds or waits for it (nothing to do after a fork: the
    registry is the child's own)."""
    with _registry_guard:
        if generation != _generation:
            return
        state.users -= 1
        if state.users <= 0 and _registry.get(key) is state:
            del _registry[key]


@contextlib.contextmanager
def _hold_folder(key: tuple, fd: Optional[int], generation: int, deadline: float):
    """Hold the backup lock of the folder ``key`` (see
    :func:`_folder_key`) for the block: its thread lock, then a flock on
    ``fd`` (None: the thread lock only). Takes over ``fd``: it is closed
    when the hold ends, fails, or nests in this thread's own hold of the
    folder, which it then shares."""
    me = threading.get_ident()
    with _registry_guard:
        state = _registry.get(key)
        if state is None:
            state = _registry[key] = _FolderLock()
        state.users += 1
    try:
        if state.owner == me:
            _close_folder_fd(fd, generation)
            fd = None
            yield
            return
        if not _acquire_thread_lock(state.lock, deadline):
            raise LibraryBusyError(_LOCK_BUSY_MESSAGE)
        try:
            if fd is not None and not _flock_fd(fd, deadline):
                _close_folder_fd(fd, generation)
                fd = None
            state.owner = me
            try:
                yield
            finally:
                state.owner = None
                # Unlock the folder before the thread lock: a thread
                # waiting for it then finds the flock free.
                _close_folder_fd(fd, generation)
                fd = None
        finally:
            if generation == _generation:
                state.lock.release()
    finally:
        _close_folder_fd(fd, generation)
        _leave_registry(key, state, generation)


@contextlib.contextmanager
def _backup_folder_lock(*dirs, timeout: Optional[float] = None):
    """Hold the backup lock of every folder in ``dirs`` for the block,
    against other threads and processes (each folder once, however it is
    spelled, taken in an order every process agrees on, so two callers
    can't deadlock; see :func:`_folder_key`).

    Waits for all of them together up to ``timeout`` seconds (None:
    :data:`BACKUP_LOCK_TIMEOUT`, read now), or until the enclosing
    :func:`~py_apple_books.db.query_deadline` ends if that is sooner,
    but always tries each lock once: with the time already up, an
    uncontended lock is still taken. A folder that doesn't exist (or
    can't be opened, or doesn't support flock) gets the thread lock
    only.

    :raises LibraryBusyError: the wait ran out; nothing is held then.
    """
    deadline = _lock_deadline(timeout)
    with _registry_guard:
        generation = _generation
    pending: dict = {}  # key -> descriptor not yet taken over by a hold
    try:
        for d in dirs:
            path = os.fspath(d)
            fd = _open_folder(path)
            key = _folder_key(path, fd)
            if key in pending:
                _close_folder_fd(fd, generation)
            else:
                pending[key] = fd
        with contextlib.ExitStack() as stack:
            for key in sorted(pending):
                fd = pending.pop(key)
                stack.enter_context(_hold_folder(key, fd, generation, deadline))
            yield
    finally:
        for fd in pending.values():
            _close_folder_fd(fd, generation)


def _make_dirs(path) -> None:
    """Create the folder ``path`` and any missing parents, like
    ``mkdir -p``, each new folder with mode 0700 (as the umask allows):
    backups hold the whole library. Existing folders keep their mode.

    The path is used as given, as :meth:`pathlib.Path.mkdir` does: the
    OS resolves it, so ``link/../backups`` lands where ``link`` points.

    :raises OSError: as :meth:`pathlib.Path.mkdir` does, e.g.
        :class:`FileExistsError` for a file in the way.
    """
    path = Path(path)
    try:
        os.mkdir(path, 0o700)
    except FileNotFoundError:
        if path.parent == path:
            raise
        _make_dirs(path.parent)
        try:
            os.mkdir(path, 0o700)
        except OSError:
            if not path.is_dir():
                raise
    except OSError:
        if not path.is_dir():
            raise


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


#: How many backup names :func:`_take_backup` tries (the timestamp moved
#: on by a microsecond each time) before giving up.
_NAME_TRIES = 100

_PART_FLAGS = (
    os.O_CREAT | os.O_EXCL | os.O_WRONLY
    | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)


def _claim_part(backup_dir: Path, stem: str, suffix: str) -> tuple:
    """``(dest, part)`` for a new backup of the store ``<stem>.sqlite``:
    ``part`` (``dest`` plus ``.part``) created empty, exclusively (never
    an existing file, never through a symlink), mode 0600; ``dest`` free.
    On a name in use, the next microsecond's timestamp is tried.

    :raises OSError: creating ``part`` failed for another reason.
    :raises WriteError: no free name in :data:`_NAME_TRIES` tries.
    """
    when = datetime.now()
    for _ in range(_NAME_TRIES):
        dest = backup_dir / f"{stem}-{when.strftime(_STAMP_FORMAT)}{suffix}.sqlite"
        part = dest.with_name(dest.name + ".part")
        if not os.path.lexists(dest):
            try:
                fd = os.open(part, _PART_FLAGS, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                return dest, part
        when = max(datetime.now(), when + timedelta(microseconds=1))
    raise WriteError("Backup failed, aborting write: no free backup file name.")


def _take_backup(db_path: Path, backup_dir: Path, *, suffix: str = "") -> Path:
    """Write one fresh backup of ``db_path``; no reuse, no pruning.

    The source is opened first, so a store that can't be read leaves
    nothing behind. The copy lands under a ``.part`` name, created
    exclusively with mode 0600 (another timestamp if the name is taken),
    and is renamed only on success, so a failed backup can never
    masquerade as a valid one. A missing ``backup_dir`` is created
    (new folders 0700). ``suffix`` is ``''`` or :data:`SNAPSHOT_SUFFIX`,
    the names a store's backup series is matched by.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    try:
        src = sqlite3.connect(read_only_uri(db_path), uri=True)
    except sqlite3.Error as e:
        raise WriteError(f"Backup failed, aborting write: {e}") from e
    try:
        try:
            _make_dirs(backup_dir)
            dest, part = _claim_part(backup_dir, db_path.stem, suffix)
        except OSError as e:
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


def _file_identity(path) -> object:
    """What tells the file ``path`` apart however it is spelled (through
    a symlink, in another letter case, through a firmlink): its device
    and inode, or its resolved path if it can't be looked at."""
    try:
        st = os.stat(path)
    except OSError:
        return Path(path).resolve()
    return (st.st_dev, st.st_ino)


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
    protected = {_file_identity(p) for p in protect if p}
    for old in _backups_for(db_path, backup_dir)[:-keep]:
        if _file_identity(old) in protected:
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
    valid one. Returns the backup file's path. New backups are created
    with mode 0600, new folders with 0700.

    The reuse check, the backup and the pruning run under a lock on the
    backup folder, shared with every other backup and restore there, in
    any thread or process using this version: a burst of concurrent
    writes takes one backup, and no backup prunes another's restore
    point. The lock is a ``flock`` on the folder itself (no lock file);
    where the file system doesn't support it, only this process's
    threads are serialized.

    :param backup_dir: Where the backup goes. By default
        :data:`BACKUP_DIR` for the current user's library (the store in
        the Apple Books container), and a folder of its own under it
        for any other store.
    :param min_interval: If the newest existing backup is younger than
        this many seconds, reuse it instead of taking another. Zero
        (the default) always takes a fresh backup. A pre-restore
        snapshot is never reused.
    :raises LibraryBusyError: another backup or restore held the folder
        for :data:`BACKUP_LOCK_TIMEOUT` seconds (or until the enclosing
        :func:`~py_apple_books.db.query_deadline`); nothing was changed.
    :raises WriteError: the backup failed; nothing was changed.
    """
    db_path = Path(db_path)
    backup_dir = _backup_dir(backup_dir, db_path)
    _make_dirs(backup_dir)

    with _backup_folder_lock(backup_dir):
        existing = _backups_for(db_path, backup_dir)
        if min_interval > 0 and existing:
            newest = existing[-1]
            if not newest.stem.endswith(SNAPSHOT_SUFFIX) and _younger_than(newest, min_interval):
                return newest

        dest = _take_backup(db_path, backup_dir)
        _prune_backups(db_path, backup_dir, keep)
    return dest


def _younger_than(path: Path, seconds: float) -> bool:
    """Whether the file ``path`` was modified less than ``seconds`` ago
    (False if it is gone: an older version's writer, which doesn't take
    the folder lock, may have pruned it)."""
    try:
        return time.time() - path.stat().st_mtime < seconds
    except FileNotFoundError:
        return False


def list_backups(
    db_path: Optional[Path] = None, backup_dir: Optional[Path] = None
) -> list[Path]:
    """Backups of the library database, newest first.

    ``db_path`` defaults to the Books library (the store the location
    variables name, else the current user's; its canonical file when
    present, even if damaged). ``backup_dir`` defaults to where that
    store's backups go (see :func:`backup_library`). The first entry is
    the restore point for the most recent write (a write reuses the
    newest backup while it is younger than :data:`BACKUP_MIN_INTERVAL`,
    so a burst of writes shares the backup taken before its first
    write) or, right after a restore, the snapshot that undoes it. Returns ``[]`` if the directory doesn't exist.

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

    From the check onwards it holds the backup folder lock (see
    :func:`backup_library`) of the folder the snapshot goes to and of
    the backup's own folder (as given and, for a symlink, where the file
    it points to is), so no concurrent write using this version can
    prune the backup being restored, or take its pre-write backup
    halfway through the restore. A missing snapshot folder is created
    only with ``snapshot``, and before the checks, since the lock needs
    it: a restore refused after that (or one that waited too long for
    the lock) may leave that empty folder behind, but changes nothing
    else.

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
        was changed (but see above for the snapshot folder).
    :raises BooksAppRunningError: Books is running; nothing was changed.
    :raises LibraryBusyError: another backup or restore held a backup
        folder for :data:`BACKUP_LOCK_TIMEOUT` seconds (or until the
        enclosing :func:`~py_apple_books.db.query_deadline`); nothing
        was changed (but see above for the snapshot folder).
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

    backup_dir = _backup_dir(backup_dir, db_path)
    if snapshot:
        # The folder is locked by its descriptor, so it must exist first.
        try:
            _make_dirs(backup_dir)
        except OSError as e:
            raise WriteError(f"Pre-restore snapshot failed, nothing restored: {e}") from e
    # The backup's folder both as given and where the file really is
    # (they differ when backup_path is a symlink): a write in either may
    # prune it.
    with _backup_folder_lock(
        backup_dir, backup_path.parent, Path(os.path.realpath(backup_path)).parent
    ):
        return _restore_locked(backup_path, db_path, backup_dir, force, snapshot)


def _restore_locked(
    backup_path: Path, db_path: Path, backup_dir: Path, force: bool, snapshot: bool
) -> Optional[Path]:
    """:func:`restore_library` from the backup check on, under the lock
    of ``backup_dir`` and of the backup's folder."""
    # A write that didn't wait for the lock (an older version) may have
    # pruned it since it was looked at.
    if not backup_path.exists():
        raise WriteError(f"Backup file not found: {backup_path}")
    if not force:
        verify_backup(backup_path, db_path)

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
