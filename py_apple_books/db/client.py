"""Read-only access to the Apple Books stores.

Nothing here touches the filesystem at import. A :class:`LibraryDB`
finds its two stores on first use (:func:`locate_store`) and hands out
pooled, read-only connections that any thread may use, one thread at a
time: a connection is checked out for one statement and then returned.
Checkouts re-check that the store files are still the ones the
connections opened, so a replaced store is picked up by the next call.

The model managers run their SQL through :class:`AppleBooksDBClient`,
which uses the library of the enclosing :func:`use_library` block, or
else the process-wide :func:`default_library`.
"""

import collections
import contextlib
import contextvars
import logging
import math
import numbers
import os
import re
import sqlite3
import stat
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Callable, List, NamedTuple, Optional, Sequence, Tuple, TypeVar

from py_apple_books.db.metadata import read_only_uri, read_store_metadata
from py_apple_books.db.query import adapt_params
from py_apple_books.exceptions import (
    AmbiguousStoreError,
    AnnotationStoreNotFoundError,
    AppleBooksError,
    DBConnectionError,
    DBError,
    DBQueryError,
    InvalidArgumentError,
    LibraryAccessDeniedError,
    LibraryNotFoundError,
    QueryTimeoutError,
)
from py_apple_books.text import fold_for_match

logger = logging.getLogger("py_apple_books.db")

_T = TypeVar("_T")

#: Seconds a read query may run before it is stopped, unless
#: ``APPLE_BOOKS_QUERY_TIMEOUT`` or ``LibraryDB(query_timeout=...)`` says
#: otherwise.
DEFAULT_QUERY_TIMEOUT = 30.0
#: Seconds a store other than a valid canonical file is used before
#: discovery runs again: the unvalidated fallback, or another store
#: chosen while the canonical file was missing, locked or invalid.
FALLBACK_TTL = 30.0
#: Seconds between lookups of a missing annotation store.
ANNOTATION_RETRY = 30.0
#: Seconds :meth:`LibraryDB.schema` trusts its cache before re-reading
#: ``PRAGMA schema_version``.
SCHEMA_RECHECK = 2.0
#: Seconds the store files are trusted to be the ones the pooled
#: connections opened before they are stat()ed again. Two stat() calls
#: per statement would add about 10% to the 1.9-style ORM's thousands of
#: statements per call.
IDENTITY_RECHECK = 0.02
# Seconds SQLite waits for a lock another process holds (sqlite3's
# default); less when the statement's deadline is sooner.
_BUSY_TIMEOUT = 5.0
# A pooled connection's wait is changed only when it is off by more.
_BUSY_SLACK = 0.05
# The wait of the Core Data metadata reads that validate a store (as in
# read_store_metadata).
_DISCOVERY_BUSY_TIMEOUT = 2.0

ENV_DATA_DIR = "APPLE_BOOKS_DATA_DIR"
ENV_LIBRARY_DB = "APPLE_BOOKS_LIBRARY_DB"
ENV_ANNOTATION_DB = "APPLE_BOOKS_ANNOTATION_DB"
ENV_QUERY_TIMEOUT = "APPLE_BOOKS_QUERY_TIMEOUT"

#: Schema name the annotation store is attached under.
ANNOTATION_SCHEMA = "anno_db"
DOCUMENTS = "Library/Containers/com.apple.iBooksX/Data/Documents"

# The progress handler runs every this many SQLite VM instructions.
_PROGRESS_OPCODES = 1000

LIBRARY_NOT_FOUND = (
    "No Apple Books library store found. Open Apple Books at least once on this Mac "
    "(or set APPLE_BOOKS_DATA_DIR)."
)
ANNOTATIONS_NOT_FOUND = (
    "No Apple Books annotation store found. Books and collections are available; "
    "highlights and notes are not. Open a book in Apple Books once to create it."
)
ACCESS_DENIED = (
    "macOS denied access to the Apple Books library. Grant Full Disk Access (or App Data "
    "access) to the app running this program in System Settings > Privacy & Security, "
    "then restart it."
)
NOT_A_DATABASE = (
    "An Apple Books store file is not a SQLite database; it is not an Apple Books library."
)
_COMPAT_WARNING = (
    "AppleBooksDBClient.conn is deprecated and will be removed in 2.0; "
    "use LibraryDB.open_connection()"
)


class _UseDefault:
    """Type of :data:`USE_DEFAULT`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "USE_DEFAULT"

    def __reduce__(self):
        return "USE_DEFAULT"


#: ``query_timeout`` default: ``APPLE_BOOKS_QUERY_TIMEOUT`` if set, else
#: :data:`DEFAULT_QUERY_TIMEOUT`.
USE_DEFAULT = _UseDefault()


# -- store discovery ----------------------------------------------------------


class _Store(NamedTuple):
    subdir: str
    canonical: str
    generation: "re.Pattern[str]"
    prefix: str
    entity: str
    missing_error: type
    missing_message: str


_STORES = {
    "library": _Store(
        "BKLibrary", "BKLibrary-1-091020131601.sqlite",
        re.compile(r"BKLibrary-[0-9]+-[0-9]+[.]sqlite"), "BKLibrary", "BKLibraryAsset",
        LibraryNotFoundError, LIBRARY_NOT_FOUND),
    "annotations": _Store(
        "AEAnnotation", "AEAnnotation_v10312011_1727_local.sqlite",
        re.compile(r"AEAnnotation_v[0-9]+_[0-9]+_local[.]sqlite"), "AEAnnotation", "AEAnnotation",
        AnnotationStoreNotFoundError, ANNOTATIONS_NOT_FOUND),
}


def _store(kind: str) -> _Store:
    try:
        return _STORES[kind]
    except (KeyError, TypeError):
        raise InvalidArgumentError(f"kind must be 'library' or 'annotations', not {kind!r}") from None


def default_data_dir() -> Path:
    """Apple Books' Documents folder in the current user's container."""
    return Path.home() / DOCUMENTS


def _as_path(value) -> Optional[Path]:
    return None if value is None else Path(os.fspath(value)).expanduser()


def _is_regular(entry: os.DirEntry) -> bool:
    try:
        return entry.is_file()
    except OSError:
        return False


def _list_stores(directory: Path) -> List[str]:
    """Sorted names of the regular ``*.sqlite`` files in ``directory``."""
    with os.scandir(directory) as entries:
        return sorted(e.name for e in entries if e.name.endswith(".sqlite") and _is_regular(e))


def _listing_error(store: _Store, e: OSError) -> DBConnectionError:
    # strerror, not str(e), which would name the absolute path.
    return DBConnectionError(f"Error reading the Apple Books {store.subdir} folder: {e.strerror or type(e).__name__}")


def _is_store(path: Path, entity: str) -> bool:
    """Whether ``path`` is a Core Data store whose model has ``entity``.

    Store files are only ever opened through SQLite. Closing a
    descriptor drops every POSIX lock this process holds on the file,
    including those of connections still reading it, and SQLite keeps
    its descriptors open while such locks are held. (Hence the
    connection handed to ``read_store_metadata``: given a path it fails
    to open, that function probes it with ``open()``.) Only regular
    files are opened: SQLite would block forever on a FIFO.
    """
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return False
    except PermissionError as e:
        raise LibraryAccessDeniedError(ACCESS_DENIED, path=path) from e
    except (OSError, ValueError):
        return False
    try:
        conn = sqlite3.connect(read_only_uri(path), uri=True, timeout=_DISCOVERY_BUSY_TIMEOUT)
    except sqlite3.Error as e:
        if _access_denied(path):
            raise LibraryAccessDeniedError(ACCESS_DENIED, path=path) from e
        return False
    try:
        meta = read_store_metadata(conn)
    finally:
        conn.close()
    return meta is not None and entity in meta.entities


def _last_write(path: Path) -> float:
    """Latest mtime of the store and its ``-wal``."""
    times = [0.0]
    for p in (path, Path(f"{path}-wal")):
        try:
            times.append(p.stat().st_mtime)
        except OSError:
            pass
    return max(times)


def _locate(kind: str, data_dir=None, strict: bool = False) -> Tuple[Path, bool]:
    """:func:`locate_store`, plus whether the store is settled: the
    canonical file, validated.

    ``False`` means the canonical file was missing, locked or invalid,
    and another store or the unvalidated fallback was chosen. That may
    be passing (a store being replaced, or written), so :class:`LibraryDB`
    re-runs discovery after :data:`FALLBACK_TTL`.
    """
    store = _store(kind)
    base = _as_path(data_dir) if data_dir is not None else default_data_dir()
    directory = base / store.subdir
    try:
        names = _list_stores(directory)
    except (FileNotFoundError, NotADirectoryError):
        raise store.missing_error(store.missing_message, path=directory) from None
    except PermissionError as e:
        raise LibraryAccessDeniedError(ACCESS_DENIED, path=directory) from e
    except OSError as e:
        raise _listing_error(store, e) from e

    # Validation reads Z_METADATA through SQLite, and reports a file the
    # OS won't let us read, without open()ing it (see _is_store).
    if store.canonical in names and _is_store(directory / store.canonical, store.entity):
        return directory / store.canonical, True

    valid = [name for name in names
             if name != store.canonical and name.startswith(store.prefix)
             and _is_store(directory / name, store.entity)]
    candidates = [name for name in valid if store.generation.fullmatch(name)] or valid
    if len(candidates) == 1:
        return directory / candidates[0], False
    if candidates:
        if strict:
            raise AmbiguousStoreError(
                f"Several Apple Books {kind} stores found in {store.subdir}/ "
                f"({', '.join(candidates)}); refusing to guess which one Apple Books uses.")
        chosen = max((directory / name for name in candidates), key=_last_write)
        logger.warning("Several Apple Books %s stores found in %s/ (%s); using the most "
                       "recently written one, %s.", kind, store.subdir, ", ".join(candidates),
                       chosen.name)
        return chosen, False

    if names and not strict:
        name = store.canonical if store.canonical in names else names[0]
        logger.warning("No valid Apple Books %s store found in %s/ (%s); falling back to %s.",
                       kind, store.subdir, ", ".join(names), name)
        return directory / name, False
    raise store.missing_error(store.missing_message, path=directory)


def locate_store(kind: str, data_dir=None, *, strict: bool = False) -> Path:
    """Find the live Apple Books store of ``kind`` under ``data_dir``.

    :param kind: ``'library'`` (``BKLibrary/``) or ``'annotations'``
        (``AEAnnotation/``).
    :param data_dir: the Documents folder holding those directories;
        ``None`` means the current user's Apple Books container. Never
        read from the environment here (see :class:`LibraryDB`).
    :param strict: refuse to guess (for writes).

    The canonical file name is used if it is a Core Data store with the
    kind's entity. Otherwise the ``BKLibrary*.sqlite`` /
    ``AEAnnotation*.sqlite`` files that pass that check are candidates,
    preferring names of Apple's ``<name>-<generation>-<stamp>`` form: one
    is used; of several the most recently written (store or ``-wal``) is
    used with a warning, or :class:`AmbiguousStoreError` is raised if
    ``strict``. If none passes, a non-strict lookup falls back, with a
    warning, to the canonical file or else the first ``*.sqlite`` by name
    (as 1.9 did); a strict one raises :class:`LibraryNotFoundError`.

    :raises LibraryNotFoundError: no directory or no store
        (:class:`AnnotationStoreNotFoundError` for annotations).
    :raises LibraryAccessDeniedError: macOS refused access.
    """
    return _locate(kind, data_dir, strict)[0]


def _require_file(path: Path, kind: str) -> Path:
    """``path`` if it is a regular file, else the kind's not-found error."""
    store = _store(kind)
    try:
        regular = stat.S_ISREG(os.stat(path).st_mode)
    except PermissionError as e:
        raise LibraryAccessDeniedError(ACCESS_DENIED, path=path) from e
    except (OSError, ValueError):
        regular = False
    if not regular:
        raise store.missing_error(store.missing_message, path=path)
    return path


def _access_denied(path) -> bool:
    """Whether the OS refuses to let this process read ``path``, which
    SQLite failed to open.

    Asked without opening the file (see :func:`_is_store`): ``stat()``
    and ``access()`` it, and list its folder, which is what macOS
    privacy protection refuses.
    """
    try:
        os.stat(path)
        if not os.access(path, os.R_OK):
            return True
        with os.scandir(os.path.dirname(os.fspath(path)) or os.curdir):
            pass
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return False


def _busy_wait(deadline: Optional[float]) -> float:
    """Seconds SQLite may wait for a lock, given the statement's deadline."""
    if deadline is None:
        return _BUSY_TIMEOUT
    return min(_BUSY_TIMEOUT, max(0.0, deadline - time.monotonic()))


def _gave_up_on_lock(e, deadline: Optional[float]) -> bool:
    """Whether ``e`` is SQLite giving up on a lock because the deadline
    came (see :func:`_busy_wait`)."""
    return (deadline is not None and isinstance(e, sqlite3.OperationalError)
            and "locked" in str(e) and time.monotonic() >= deadline - _BUSY_SLACK)


def _identity(path: Optional[Path]):
    """``(st_dev, st_ino)`` of ``path``; None for no path or no file."""
    if path is None:
        return None
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st.st_dev, st.st_ino


# -- connections --------------------------------------------------------------


def _register_functions(conn: sqlite3.Connection) -> None:
    """Register the SQL functions read queries use.

    ``abk_fold(value)`` is :func:`py_apple_books.text.fold_for_match`,
    for the ``__search`` lookup, which passes the column as a BLOB (it
    is decoded as UTF-8, invalid bytes becoming U+FFFD). Name, argument
    count and function are passed positionally: the keyword forms are
    deprecated since Python 3.13.
    """
    try:
        conn.create_function('abk_fold', 1, fold_for_match, deterministic=True)
    except sqlite3.NotSupportedError:
        # SQLite older than 3.8.3 has no deterministic flag.
        conn.create_function('abk_fold', 1, fold_for_match)


def _timeout_from_env() -> Optional[float]:
    """The query timeout ``APPLE_BOOKS_QUERY_TIMEOUT`` asks for.

    Unset: :data:`DEFAULT_QUERY_TIMEOUT`. ``0``, ``none`` or ``off``: no
    timeout. A positive number: that many seconds. Anything else is
    logged and ignored.
    """
    raw = os.environ.get(ENV_QUERY_TIMEOUT)
    if raw is None:
        return DEFAULT_QUERY_TIMEOUT
    value = raw.strip().lower()
    if value in ("0", "none", "off"):
        return None
    try:
        seconds = float(value)
    except ValueError:
        seconds = math.nan
    if seconds == 0:
        return None
    if seconds > 0:
        return seconds
    logger.warning("Ignoring %s=%r: expected seconds, or 0, 'none' or 'off' for no limit. "
                   "Using %g s.", ENV_QUERY_TIMEOUT, raw, DEFAULT_QUERY_TIMEOUT)
    return DEFAULT_QUERY_TIMEOUT


def _timeout_seconds(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, numbers.Real) or not value >= 0:
        raise InvalidArgumentError(
            f"query_timeout must be a number of seconds >= 0 or None, not {value!r}")
    return float(value) or None


class StorePaths(NamedTuple):
    """The two store files a :class:`LibraryDB` reads."""

    library: Path
    #: None when there is no annotation store.
    annotations: Optional[Path]


class _Pooled:
    """A pooled connection and what it was opened on."""

    __slots__ = ("conn", "paths", "identity", "pid", "generation", "busy")

    def __init__(self, conn: sqlite3.Connection, paths: StorePaths, identity: tuple,
                 pid: int, generation: int, busy: float):
        self.conn = conn
        self.paths = paths
        #: ``_identity`` of both files, taken just before they were opened.
        self.identity = identity
        self.pid = pid
        self.generation = generation
        #: Seconds SQLite waits for a lock on this connection.
        self.busy = busy


class _Slots:
    """The pool's connection slots: a semaphore that serves waiters in
    the order they came.

    A released slot goes straight to the longest waiting thread, so the
    thread that released it can't take it back before that one wakes up
    (``threading.Semaphore`` lets it, starving waiters under load).
    """

    __slots__ = ("_lock", "_size", "_free", "_waiters")

    def __init__(self, size: int):
        self._lock = threading.Lock()
        self._size = self._free = size
        # One locked lock per waiting thread, oldest first; a slot is
        # handed over by releasing it.
        self._waiters: collections.deque = collections.deque()

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """Take a slot, waiting at most ``timeout`` seconds (None: as long
        as it takes). Whether one was taken."""
        with self._lock:
            if self._free and not self._waiters:
                self._free -= 1
                return True
            if timeout is not None and timeout <= 0:
                return False
            waiter = threading.Lock()
            waiter.acquire()
            self._waiters.append(waiter)
        got = False
        try:
            got = waiter.acquire(timeout=-1 if timeout is None else min(timeout, threading.TIMEOUT_MAX))
        finally:
            if not got:
                with self._lock:
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        # Handed a slot as the wait ended (or was
                        # interrupted): pass it on.
                        self._hand_off()
        return got

    def release(self) -> None:
        with self._lock:
            self._hand_off()

    def _hand_off(self) -> None:
        """Give a slot to the longest waiting thread, or free it. Called
        with the lock held."""
        if self._waiters:
            self._waiters.popleft().release()
        elif self._free < self._size:
            self._free += 1
        else:
            raise ValueError("connection slot released too many times")


class _Schema(NamedTuple):
    #: (identity, schema_version of each attached store)
    key: tuple
    tables: dict
    checked: float


# ``(deadline, limit)``: time.monotonic() value and the seconds it was
# set from, for messages.
_deadline: contextvars.ContextVar = contextvars.ContextVar("py_apple_books_deadline", default=None)


class LibraryDB:
    """One Apple Books library: its two stores and a pool of read-only
    connections to them.

    :param data_dir: the Apple Books Documents folder (holding
        ``BKLibrary/`` and ``AEAnnotation/``).
    :param library_db: the library store file.
    :param annotation_db: the annotation store file.
    :param query_timeout: seconds a statement may run before it is
        stopped with :class:`QueryTimeoutError`; ``None`` or ``0`` for no
        limit. :data:`USE_DEFAULT` reads ``APPLE_BOOKS_QUERY_TIMEOUT``
        (default :data:`DEFAULT_QUERY_TIMEOUT`).
    :param max_idle: idle connections kept open.
    :param max_connections: connections open at once; beyond that,
        callers wait for one (within their deadline), first come first
        served.

    Where the stores are: a store given as a file is used as is. The
    rest are found by :func:`locate_store` in ``data_dir``, or in the
    current user's container if it is None. When none of the three
    arguments is given, the ``APPLE_BOOKS_LIBRARY_DB``,
    ``APPLE_BOOKS_ANNOTATION_DB`` and ``APPLE_BOOKS_DATA_DIR`` environment
    variables stand in for them; otherwise they are ignored.

    Construction does no I/O. The stores are found on first use, and
    found again when a store file is replaced (noticed within
    :data:`IDENTITY_RECHECK` seconds). A missing annotation store isn't
    an error: book and collection queries work, and it is looked for
    again every :data:`ANNOTATION_RETRY` seconds.

    Thread-safe. A connection is used by one thread at a time. Close a
    library you create when done with it (:meth:`close`, or ``with``).
    The models read from it keep it alive, and using their relations
    after :meth:`close` opens its connections again. A library dropped
    with its models closes its idle connections too, but only in the
    process that opened them.

    A forked child makes its own connections. Fork only while no other
    thread is running a query: SQLite's internal locks are copied in
    whatever state they were in.
    """

    def __init__(self, data_dir=None, *, library_db=None, annotation_db=None,
                 query_timeout=USE_DEFAULT, max_idle: int = 4, max_connections: int = 8):
        if max_connections < 1 or max_idle < 0:
            raise InvalidArgumentError("max_connections must be >= 1 and max_idle >= 0")
        self._explicit = any(x is not None for x in (data_dir, library_db, annotation_db))
        self._data_dir = _as_path(data_dir)
        self._library_db = _as_path(library_db)
        self._annotation_db = _as_path(annotation_db)
        if query_timeout is USE_DEFAULT:
            query_timeout = _timeout_from_env()
        #: Seconds per statement, or None for no limit.
        self.query_timeout = _timeout_seconds(query_timeout)
        self.max_idle = max_idle
        self.max_connections = max_connections
        self._clock = time.monotonic
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._slots = _Slots(max_connections)
        self._idle: List[_Pooled] = []
        # Idle connections a forked child got from its parents: never
        # used, and kept referenced so garbage collection doesn't close
        # them.
        self._inherited: List[_Pooled] = []
        # Bumped whenever pooled connections must not be reused.
        self._generation = 0
        self._paths: Optional[StorePaths] = None
        # Clock time the cached paths expire (a store is not settled; see _locate).
        self._paths_expire: Optional[float] = None
        # Clock time of the last failed annotation store lookup.
        self._annotations_attempt: Optional[float] = None
        self._schema: Optional[_Schema] = None
        # (identity, clock time) of the last stat() that found the store
        # files unchanged.
        self._verified: Tuple[Optional[tuple], float] = (None, 0.0)

    def __repr__(self) -> str:
        if not self._explicit:
            return "LibraryDB()"
        args = [f"{name}={str(value)!r}" for name, value in (
            ("data_dir", self._data_dir), ("library_db", self._library_db),
            ("annotation_db", self._annotation_db)) if value is not None]
        return f"LibraryDB({', '.join(args)})"

    def __enter__(self) -> "LibraryDB":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __del__(self):
        # Close the idle connections now: left alone they wait for the
        # cyclic garbage collector (sqlite3's statement cache refers back
        # to its connection), and SQLite keeps the files of connections
        # closed then open for as long as another connection of this
        # process holds a lock on them.
        try:
            if self._pid == os.getpid():
                for pooled in self._idle:
                    pooled.conn.close()
        except Exception:
            pass

    # -- store paths ------------------------------------------------------

    def _source(self, kind: str) -> Tuple[Optional[Path], Optional[Path]]:
        """``(store file, data dir)`` for ``kind`` by precedence (R17);
        either may be None (a None data dir is the default one)."""
        if self._explicit:
            return (self._library_db if kind == "library" else self._annotation_db), self._data_dir
        env_file = os.environ.get(ENV_LIBRARY_DB if kind == "library" else ENV_ANNOTATION_DB)
        env_dir = os.environ.get(ENV_DATA_DIR)
        return _as_path(env_file or None), _as_path(env_dir or None)

    def _find(self, kind: str, strict: bool = False) -> Tuple[Path, bool]:
        store_file, data_dir = self._source(kind)
        if store_file is not None:
            return _require_file(store_file, kind), True
        return _locate(kind, data_dir, strict)

    def _resolve(self, now: float) -> Tuple[StorePaths, Optional[float]]:
        library, library_settled = self._find("library")
        try:
            annotations, annotations_settled = self._find("annotations")
            self._annotations_attempt = None
        except AnnotationStoreNotFoundError:
            annotations, annotations_settled = None, True
            self._annotations_attempt = now
        expire = None if library_settled and annotations_settled else now + FALLBACK_TTL
        return StorePaths(library, annotations), expire

    def paths(self) -> StorePaths:
        """The store files, found on first use.

        :raises LibraryNotFoundError: no library store.
        :raises LibraryAccessDeniedError: macOS refused access.
        """
        self._check_fork()
        with self._lock:
            now = self._clock()
            if self._paths is not None and (self._paths_expire is None or now < self._paths_expire):
                return self._paths
            self._paths, self._paths_expire = self._resolve(now)
            return self._paths

    def library_path(self, strict: bool = False) -> Path:
        """The library store file.

        ``strict=True`` (for writes) resolves it again and raises
        :class:`AmbiguousStoreError` or :class:`LibraryNotFoundError`
        instead of guessing.
        """
        if not strict:
            return self.paths().library
        return self._find("library", strict=True)[0]

    def has_annotations(self) -> bool:
        """Whether there is an annotation store.

        A missing one is looked for again once :data:`ANNOTATION_RETRY`
        seconds have passed since the last attempt.
        """
        paths = self.paths()
        if paths.annotations is not None:
            return True
        with self._lock:
            paths = self.paths()
            if paths.annotations is not None:
                return True
            now = self._clock()
            last = self._annotations_attempt
            if last is not None and now - last < ANNOTATION_RETRY:
                return False
            try:
                annotations, settled = self._find("annotations")
            except AnnotationStoreNotFoundError:
                self._annotations_attempt = now
                return False
            self._annotations_attempt = None
            self._paths = paths._replace(annotations=annotations)
            if not settled:
                expire = now + FALLBACK_TTL
                self._paths_expire = expire if self._paths_expire is None else min(self._paths_expire, expire)
            # New connections attach the store; the cached schema has no
            # anno_db tables.
            self._generation += 1
            self._schema = None
            return True

    def candidates(self, kind: str) -> List[Path]:
        """Every ``*.sqlite`` file in the directory ``kind`` is looked up
        in (the given store file's directory, if there is one)."""
        store = _store(kind)
        store_file, data_dir = self._source(kind)
        if store_file is not None:
            directory = store_file.parent
        else:
            directory = (data_dir if data_dir is not None else default_data_dir()) / store.subdir
        try:
            return [directory / name for name in _list_stores(directory)]
        except (FileNotFoundError, NotADirectoryError):
            return []
        except PermissionError as e:
            raise LibraryAccessDeniedError(ACCESS_DENIED, path=directory) from e
        except OSError as e:
            raise _listing_error(store, e) from e

    # -- connections ------------------------------------------------------

    def _check_fork(self) -> None:
        pid = os.getpid()
        if pid != self._pid:
            # A forked child: the parent's connections and locks aren't
            # ours. Set the connections aside unused and unclosed.
            self._pid = pid
            self._inherited = self._inherited + self._idle
            self._idle = []
            self._lock = threading.RLock()
            self._slots = _Slots(self.max_connections)
            self._generation += 1

    def _connect(self, paths: StorePaths, check_same_thread: bool,
                 busy: float = _BUSY_TIMEOUT) -> sqlite3.Connection:
        """A configured read-only connection to ``paths``, waiting at most
        ``busy`` seconds for a lock."""
        try:
            conn = sqlite3.connect(read_only_uri(paths.library), uri=True, timeout=busy,
                                   check_same_thread=check_same_thread)
        except sqlite3.Error as e:
            raise self._connect_error(e, paths.library, "library") from e
        try:
            conn.execute("PRAGMA query_only=1")
            if paths.annotations is not None:
                # The URI is bound, so the store opens read-only too.
                conn.execute(f"ATTACH DATABASE ? AS {ANNOTATION_SCHEMA}",
                             (read_only_uri(paths.annotations),))
            _register_functions(conn)
        except sqlite3.Error as e:
            conn.close()
            # Of these statements only the ATTACH opens a file.
            if paths.annotations is not None:
                raise self._connect_error(e, paths.annotations, "annotations") from e
            raise self._connect_error(e, paths.library, "library") from e
        return conn

    @staticmethod
    def _connect_error(e: sqlite3.Error, path: Path, kind: str) -> DBError:
        """The error for SQLite failing to open (or attach) ``path``.

        The file is never opened here: other connections of this process
        may hold locks on it (see :func:`_is_store`).
        """
        message = str(e)
        if "not a database" in message:
            return LibraryNotFoundError(NOT_A_DATABASE)
        if "unable to open" in message:
            if _access_denied(path):
                return LibraryAccessDeniedError(ACCESS_DENIED, path=path)
            if _identity(path) is None:
                store = _store(kind)
                return store.missing_error(store.missing_message, path=path)
            # An ATTACH's message goes on to name the file.
            message = "unable to open database file"
        return DBConnectionError(f"Error connecting to database: {message}")

    def _connect_current(self, check_same_thread: bool, busy: float):
        """Connect to the resolved stores: ``(connection, paths, identity,
        generation)``.

        A store file gone since the paths were resolved (removed, or
        halfway through a replacement) makes them resolve again, and the
        connection is tried once more.
        """
        for last in (False, True):
            with self._lock:
                paths = self.paths()
                generation = self._generation
            # Identity before opening: if a file is replaced in between, the
            # connection looks stale at its next checkout rather than fresh.
            identity = (_identity(paths.library), _identity(paths.annotations))
            try:
                return self._connect(paths, check_same_thread, busy), paths, identity, generation
            except LibraryNotFoundError as e:
                if e.path is None:
                    raise  # not a database
                with self._lock:
                    if self._paths == paths:
                        self._paths = self._paths_expire = None
                if last:
                    raise

    def _open(self, deadline: Optional[float] = None) -> _Pooled:
        busy = _busy_wait(deadline)
        conn, paths, identity, generation = self._connect_current(False, busy)
        return _Pooled(conn, paths, identity, os.getpid(), generation, busy)

    def open_connection(self) -> sqlite3.Connection:
        """A new read-only connection to this library, owned by the
        caller (close it).

        Configured like the pooled ones (``query_only``, the annotation
        store attached as ``anno_db``, ``abk_fold`` registered) but bound
        to the calling thread and not subject to the query timeout.
        """
        self.has_annotations()
        return self._connect_current(True, _BUSY_TIMEOUT)[0]

    def _fresh(self, pooled: _Pooled) -> bool:
        """Whether an idle connection may be reused: its store files are
        still the ones at the paths (checked at most every
        :data:`IDENTITY_RECHECK` seconds). A connection whose files were
        replaced or removed also drops the cached paths and schema.
        Called with the lock held."""
        if pooled.generation != self._generation or pooled.paths != self._paths:
            return False
        now = self._clock()
        verified, at = self._verified
        if pooled.identity == verified and now - at < IDENTITY_RECHECK:
            return True
        identity = (_identity(pooled.paths.library), _identity(pooled.paths.annotations))
        if identity == pooled.identity and identity[0] is not None:
            self._verified = (identity, now)
            return True
        self._paths = self._paths_expire = None
        self._schema = None
        self._verified = (None, 0.0)
        return False

    def _take_idle(self) -> Optional[_Pooled]:
        stale = []
        with self._lock:
            while self._idle:
                pooled = self._idle.pop()
                if self._fresh(pooled):
                    break
                stale.append(pooled)
            else:
                pooled = None
        for old in stale:
            _close_quietly(old.conn)
        return pooled

    def _give_back(self, pooled: _Pooled) -> None:
        if pooled.pid != os.getpid():
            return  # checked out before a fork; the parent owns it
        keep = True
        try:
            if pooled.conn.in_transaction:
                pooled.conn.rollback()
        except sqlite3.Error:
            keep = False
        with self._lock:
            if keep and pooled.generation == self._generation and len(self._idle) < self.max_idle:
                self._idle.append(pooled)
                return
        _close_quietly(pooled.conn)

    def _acquire(self, deadline: Optional[float], limit: Optional[float]):
        """Check out ``(pooled connection, slots)``; give both to
        :meth:`_release`. (Not a context manager: this runs for every
        statement.)"""
        self._check_fork()
        slots = self._slots
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not slots.acquire(timeout=timeout):
            if limit is None:
                limit = timeout
            raise QueryTimeoutError(
                f"Timed out waiting for a database connection (limit {limit:g} s).", timeout=limit)
        try:
            return self._take_idle() or self._open(deadline), slots
        except BaseException:
            slots.release()
            raise

    @staticmethod
    def _limit_busy_wait(pooled: _Pooled, deadline: Optional[float]) -> None:
        """Make SQLite wait for a lock no longer than ``deadline`` allows."""
        busy = _busy_wait(deadline)
        if abs(busy - pooled.busy) > _BUSY_SLACK:
            pooled.conn.execute(f"PRAGMA busy_timeout = {round(busy * 1000)}")
            pooled.busy = busy

    def _release(self, pooled: _Pooled, slots: _Slots) -> None:
        try:
            self._give_back(pooled)
        finally:
            slots.release()

    @contextlib.contextmanager
    def connection(self, deadline: Optional[float] = None):
        """Check out a pooled connection for the ``with`` block.

        :param deadline: ``time.monotonic()`` value by which a connection
            must be free; :class:`QueryTimeoutError` otherwise. None waits
            as long as it takes. SQLite then waits no longer than that
            for a lock another process holds; statements aren't stopped.

        The connection goes back to the pool, and to other threads,
        after the block, so leave it as you found it:

        - Don't keep it, or a cursor, after the block. Fetch all rows or
          close each cursor inside it: an unfinished ``SELECT`` keeps a
          read lock on the store, which can hold up Apple Books' writes.
        - Don't detach ``anno_db``, change PRAGMAs or register functions.

        An open transaction is rolled back and a progress handler
        removed when it is returned.
        """
        self.paths()  # an expired fallback is resolved again first
        pooled, slots = self._acquire(deadline, None)
        try:
            self._limit_busy_wait(pooled, deadline)
            yield pooled.conn
        finally:
            with contextlib.suppress(sqlite3.Error):
                pooled.conn.set_progress_handler(None, 0)
            self._release(pooled, slots)

    def close(self) -> None:
        """Close the idle connections and forget the store paths and
        schema. Connections checked out now are closed when returned.
        The library stays usable; it reconnects on the next query."""
        self._check_fork()
        with self._lock:
            idle, self._idle = self._idle, []
            self._paths = self._paths_expire = self._annotations_attempt = None
            self._schema = None
            self._verified = (None, 0.0)
            self._generation += 1
        for pooled in idle:
            _close_quietly(pooled.conn)

    # -- execution --------------------------------------------------------

    def _statement_deadline(self) -> Tuple[Optional[float], Optional[float]]:
        """``(deadline, limit)`` for a statement starting now."""
        best = None
        if self.query_timeout is not None:
            best = (time.monotonic() + self.query_timeout, self.query_timeout)
        context = _deadline.get()
        if context is not None and (best is None or context[0] < best[0]):
            best = context
        if best is None or math.isinf(best[0]):
            return None, None
        return best

    def _run_pooled(self, fn: Callable[[_Pooled], _T]) -> _T:
        """Run ``fn`` on a checked-out connection within the deadline and
        turn every failure into a typed :class:`DBError`."""
        deadline, limit = self._statement_deadline()
        pooled = None
        try:
            if self.paths().annotations is None:
                self.has_annotations()
            pooled, slots = self._acquire(deadline, limit)
            try:
                conn = pooled.conn
                if deadline is not None:
                    conn.set_progress_handler(lambda: time.monotonic() > deadline, _PROGRESS_OPCODES)
                try:
                    if pooled.busy != _BUSY_TIMEOUT or (
                            deadline is not None and deadline - time.monotonic() < _BUSY_TIMEOUT):
                        self._limit_busy_wait(pooled, deadline)
                    return fn(pooled)
                finally:
                    if deadline is not None:
                        conn.set_progress_handler(None, 0)
            finally:
                self._release(pooled, slots)
        except AppleBooksError as e:
            if isinstance(e, DBConnectionError) and _gave_up_on_lock(e.__cause__, deadline):
                raise QueryTimeoutError(
                    f"Timed out waiting for a locked database (limit {limit:g} s).", timeout=limit) from e
            raise
        except sqlite3.OperationalError as e:
            message = str(e)
            if "interrupted" in message and deadline is not None:
                raise QueryTimeoutError(
                    f"Query took too long and was stopped (limit {limit:g} s).", timeout=limit) from e
            if _gave_up_on_lock(e, deadline):
                raise QueryTimeoutError(
                    f"Timed out waiting for a locked database (limit {limit:g} s).", timeout=limit) from e
            if (f"{ANNOTATION_SCHEMA}." in message or f"database {ANNOTATION_SCHEMA}" in message) \
                    and pooled is not None and pooled.paths.annotations is None:
                raise AnnotationStoreNotFoundError(ANNOTATIONS_NOT_FOUND) from e
            raise DBQueryError(f"Error executing query: {e}") from e
        except sqlite3.DatabaseError as e:
            if "not a database" in str(e):
                raise LibraryNotFoundError(NOT_A_DATABASE) from e
            raise DBQueryError(f"Error executing query: {e}") from e
        except sqlite3.Error as e:
            raise DBQueryError(f"Error executing query: {e}") from e
        except Exception as e:
            raise DBQueryError(f"Unexpected error while executing query: {e}") from e

    def _run(self, fn: Callable[[sqlite3.Connection], _T]) -> _T:
        return self._run_pooled(lambda pooled: fn(pooled.conn))

    def execute(self, sql: str, params: Sequence[Any] = ()) -> list:
        """Run one read statement and return all rows.

        ``params`` are bound through
        :func:`~py_apple_books.db.query.adapt_params`.

        :raises QueryTimeoutError: the statement ran past the deadline.
        :raises DBQueryError: any other failure of the statement.
        :raises DBConnectionError: the library can't be found or opened
            (:class:`LibraryNotFoundError`,
            :class:`AnnotationStoreNotFoundError` for an annotation query
            without an annotation store, :class:`LibraryAccessDeniedError`).
        """
        params = () if params is None else params
        return self._run(lambda conn: conn.execute(sql, adapt_params(params)).fetchall())

    # -- schema -----------------------------------------------------------

    def _schema_key(self, pooled: _Pooled) -> tuple:
        versions = [pooled.conn.execute("PRAGMA main.schema_version").fetchone()[0]]
        if pooled.paths.annotations is not None:
            versions.append(
                pooled.conn.execute(f"PRAGMA {ANNOTATION_SCHEMA}.schema_version").fetchone()[0])
        return pooled.identity, tuple(versions)

    def _read_schema(self, pooled: _Pooled) -> Tuple[tuple, dict]:
        # The versions are read first, so a change made meanwhile shows
        # up as a new version at the next check.
        key = self._schema_key(pooled)
        schemas = [("main", "")]
        if pooled.paths.annotations is not None:
            schemas.append((ANNOTATION_SCHEMA, f"{ANNOTATION_SCHEMA}."))
        tables = {}
        for schema, prefix in schemas:
            columns = {}
            rows = pooled.conn.execute(
                f"SELECT m.name, p.name FROM {schema}.sqlite_master AS m "
                f"JOIN pragma_table_info(m.name, '{schema}') AS p WHERE m.type = 'table'")
            for table, column in rows:
                columns.setdefault(table, set()).add(column)
            tables.update((prefix + table, frozenset(cols)) for table, cols in columns.items())
        return key, tables

    def schema(self) -> dict:
        """``{table: frozenset(columns)}`` of both stores.

        Library tables by bare name (``'ZBKLIBRARYASSET'``), annotation
        store tables as ``'anno_db.<table>'``. Cached; the cache is
        checked against the files and their ``PRAGMA schema_version``
        at most every :data:`SCHEMA_RECHECK` seconds. Errors and the
        deadline are as for :meth:`execute`.
        """
        cached = self._schema
        now = self._clock()
        if cached is not None:
            if now - cached.checked < SCHEMA_RECHECK:
                return cached.tables
            if self._run_pooled(self._schema_key) == cached.key:
                with self._lock:
                    if self._schema is cached:
                        self._schema = cached._replace(checked=now)
                return cached.tables
        key, tables = self._run_pooled(self._read_schema)
        with self._lock:
            self._schema = _Schema(key, tables, now)
        return tables

    def invalidate_schema(self) -> None:
        """Make the next :meth:`schema` call read the schema again."""
        self._check_fork()
        with self._lock:
            self._schema = None


def _close_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass


# -- the library in use -------------------------------------------------------

_default_lock = threading.Lock()
_default_db: Optional[LibraryDB] = None
_active_db: contextvars.ContextVar = contextvars.ContextVar("py_apple_books_library", default=None)


def default_library() -> LibraryDB:
    """The process-wide :class:`LibraryDB()` (created on first call,
    without I/O)."""
    global _default_db
    db = _default_db
    if db is None:
        with _default_lock:
            if _default_db is None:
                _default_db = LibraryDB()
            db = _default_db
    return db


def _reset_default_library() -> None:
    """For tests: close and forget the default library, so the next
    :func:`default_library` call reads the environment again."""
    global _default_db
    with _default_lock:
        db, _default_db = _default_db, None
    if db is not None:
        db.close()


def current_library() -> LibraryDB:
    """The library of the innermost :func:`use_library` block, else
    :func:`default_library`."""
    db = _active_db.get()
    return default_library() if db is None else db


@contextlib.contextmanager
def use_library(db: Optional[LibraryDB]):
    """Make ``db`` the :func:`current_library` inside the block (in this
    thread or task, and in the ones that copy its context, such as
    ``anyio.to_thread``). ``None`` leaves the current library as it is.
    Yields the current library."""
    if db is None:
        yield current_library()
        return
    token = _active_db.set(db)
    try:
        yield db
    finally:
        _active_db.reset(token)


@contextlib.contextmanager
def query_deadline(seconds: Optional[float]):
    """Stop every query in the block that is still running ``seconds``
    from now, with :class:`QueryTimeoutError`.

    An enclosing, sooner deadline still applies, as does each library's
    ``query_timeout``. Covers work started in this thread or task and in
    those that copy its context, such as ``anyio.to_thread.run_sync``.
    ``None`` changes nothing.
    """
    if seconds is None:
        yield
        return
    if isinstance(seconds, bool) or not isinstance(seconds, numbers.Real) or not seconds >= 0:
        raise InvalidArgumentError(f"seconds must be a number >= 0 or None, not {seconds!r}")
    seconds = float(seconds)
    at = time.monotonic() + seconds
    current = _deadline.get()
    if current is not None and current[0] <= at:
        yield
        return
    token = _deadline.set((at, seconds))
    try:
        yield
    finally:
        _deadline.reset(token)


# -- pre-1.10 names ------------------------------------------------------------


def find_sqlite_file(directory: Path) -> Path:
    """The store file in ``directory`` (pre-1.10; use :func:`locate_store`).

    For a ``BKLibrary`` or ``AEAnnotation`` directory this is
    ``locate_store(kind, directory.parent)``. Any other directory gives
    its first ``*.sqlite`` by name, as before.
    """
    directory = Path(directory)
    for kind, store in _STORES.items():
        if directory.name == store.subdir:
            return locate_store(kind, directory.parent)
    try:
        return sorted(directory.glob("*.sqlite"))[0]
    except IndexError:
        raise DBConnectionError(
            f"No sqlite files found in {directory}. Please open Apple Books at least once."
        )


#: Pre-1.10 name of :func:`py_apple_books.db.metadata.read_only_uri`.
_read_only_uri = read_only_uri


class DBClient:
    """Base class of the pre-1.10 read clients."""

    def _get_sqlite_file(self, path: Path) -> Path:
        return find_sqlite_file(path)

    def _get_cursor(self, paths: list[tuple[str, Path]]):
        _, first_path = paths[0]
        try:
            # uri=True also enables URI interpretation for the ATTACH
            # statements below, so the attached databases inherit
            # read-only mode.
            conn = sqlite3.connect(_read_only_uri(self._get_sqlite_file(first_path)), uri=True)
            _register_functions(conn)
            cursor = conn.cursor()
            for db_name, path in paths[1:]:
                # The URI is bound; db_name is a class constant.
                cursor.execute("ATTACH DATABASE ? AS " + db_name,
                               (_read_only_uri(self._get_sqlite_file(path)),))
            self.conn = conn
            return cursor
        except DBConnectionError:
            raise
        except sqlite3.Error as e:
            raise DBConnectionError(f"Error connecting to database: {e}")
        except Exception as e:
            raise DBError(f"Unexpected error while connecting to database: {e}")

    def execute(self, *args, **kwargs):
        raise NotImplementedError

    def close(self):
        # __dict__, not getattr: a subclass may compute these lazily.
        for name in ("cursor", "conn"):
            resource = self.__dict__.get(name)
            if resource is not None:
                resource.close()


class AppleBooksDBClient(DBClient):
    """Read client of the model managers: runs queries on ``db``, or on
    :func:`current_library` at execution time if ``db`` is None.

    No I/O on construction. Write access goes through
    :mod:`py_apple_books.collection_writer`, which opens its own
    short-lived read-write connection with guard rails (backup,
    Books-running check, schema validation, single transaction).
    """

    # Informational: where the default library's stores live when no
    # environment variable says otherwise (computed at import).
    book_lib_db = ("lib_db", Path.home() / DOCUMENTS / "BKLibrary")
    anno_db = ("anno_db", Path.home() / DOCUMENTS / "AEAnnotation")

    def __init__(self, db: Optional[LibraryDB] = None):
        self._db = db
        self._conn: Optional[sqlite3.Connection] = None
        self._cursor = None
        # The cursor the .cursor getter made, to tell it from one assigned.
        self._own_cursor = None

    def _library(self) -> LibraryDB:
        return current_library() if self._db is None else self._db

    def execute(self, query: str, params=()) -> list:
        """Run ``query`` with ``params`` bound (see
        :func:`~py_apple_books.db.query.adapt_params`) and return all rows."""
        if self._cursor is not None and self._cursor is not self._own_cursor:
            return self._execute_on(self._cursor, query, params)
        return self._library().execute(query, params)

    @staticmethod
    def _execute_on(cursor, query: str, params) -> list:
        # 1.9 ran every query on self.cursor; code that assigned one
        # (tests injecting failures) still gets it used.
        try:
            cursor.execute(query, adapt_params(params or ()))
            return cursor.fetchall()
        except AppleBooksError:
            raise
        except sqlite3.Error as e:
            raise DBQueryError(f"Error executing query: {e}") from e
        except Exception as e:
            raise DBQueryError(f"Unexpected error while executing query: {e}") from e

    def _compat_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._library().open_connection()
        return self._conn

    @property
    def conn(self) -> sqlite3.Connection:
        """Deprecated: a read-only connection of this client's own
        (opened on first access, closed by :meth:`close`)."""
        if self._conn is None:
            warnings.warn(_COMPAT_WARNING, DeprecationWarning, stacklevel=2)
        return self._compat_conn()

    @conn.setter
    def conn(self, value) -> None:
        self._conn = value

    @property
    def cursor(self):
        """Deprecated: a cursor on :attr:`conn`."""
        if self._cursor is None:
            warnings.warn(_COMPAT_WARNING, DeprecationWarning, stacklevel=2)
            self._cursor = self._own_cursor = self._compat_conn().cursor()
        return self._cursor

    @cursor.setter
    def cursor(self, value) -> None:
        self._cursor = value

    def close(self):
        """Close the connection opened for :attr:`conn` / :attr:`cursor`,
        if any. The library's connection pool stays open
        (``PyAppleBooks.close()`` closes it)."""
        cursor, conn = self._cursor, self._conn
        self._cursor = self._own_cursor = self._conn = None
        if cursor is not None:
            with contextlib.suppress(Exception):
                cursor.close()
        if conn is not None:
            conn.close()
