"""Titles and authors of books removed from the library, from Apple
Books' own caches (new in 1.11).

Apple Books keeps what it parsed from each book (title, author,
language, publisher, year) in caches next to the library,
``<container>/Data/Library/Caches/AEEpubInfoSource/AEBookInfo-<version>.sqlite``,
one per Books version, keyed by the book's asset id. It keeps the rows
of books since removed from the library, so highlights whose book is
gone (``annotation.book is None``) can still be named:
:meth:`PyAppleBooks.get_cached_book_info
<py_apple_books.PyAppleBooks.get_cached_book_info>`.

Best effort: macOS may purge the caches at any time, each Books version
writes its own, and a value is what Books cached when it last parsed
the book. :attr:`CachedBookInfo.source` names the cache file a value
came from.

Public: :class:`CachedBookInfo` and :data:`BOOK_INFO_RECHECK`. Importing
this module does no I/O.
"""

from __future__ import annotations

import collections
import logging
import math
import os
import pathlib
import re
import sqlite3
import stat as _stat
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import quote

from py_apple_books import _icloud
from py_apple_books.exceptions import InvalidArgumentError

__all__ = ["CachedBookInfo", "BOOK_INFO_RECHECK"]

logger = logging.getLogger("py_apple_books.book_info")

#: Minimum seconds between two looks at the cache folder: within that
#: time, a call answers from what earlier calls of the same library read
#: and opens only the files it still needs. After it, the folder is
#: listed and its files stat'ed again, and only changed files are read
#: again. Read at call time.
BOOK_INFO_RECHECK = 30.0


@dataclass(frozen=True)
class CachedBookInfo:
    """One book's details as Apple Books cached them when it last parsed
    the book.

    Each text field is None when the cache has no value (or an empty
    one). ``year`` is the cached publication year as text (``'2001'``).
    ``source`` is the bare name of the cache file the values come from
    (``'AEBookInfo-v20250715-26.0.sqlite'``, never a folder), which names
    the Books version that wrote it: the values are cache-derived.
    """

    asset_id: str
    title: Optional[str]
    author: Optional[str]
    language: Optional[str] = None
    publisher: Optional[str] = None
    year: Optional[str] = None
    source: str = ""


# -- constants -----------------------------------------------------------------

# Relative to the container's Data folder, the parent of Documents.
_FOLDER = pathlib.PurePosixPath("Library/Caches/AEEpubInfoSource")
_NAME = re.compile(r"AEBookInfo-[A-Za-z0-9._-]{1,100}\.sqlite")
# A folder that is, or resolves into, one of these is never read.
# Casefolded: APFS is case-insensitive by default.
_CLOUD_PARTS = frozenset(p.casefold() for p in ("Mobile Documents", "com~apple~CloudDocs", "CloudStorage"))
# The newest this many cache files (natural order of their names) are read.
_MAX_FILES = 32
# Ids remembered per cache file (least recently used dropped).
_MEMO_IDS = 1024
# Ids bound per statement.
_CHUNK = 500
# A cached value longer than this (in bytes) reads as None: a damaged or
# crafted cell is neither returned nor remembered whole.
_MAX_VALUE_BYTES = 4096
# Seconds: per call, per file, and waiting for a lock Books holds.
_CALL_BUDGET = 2.0
_FILE_BUDGET = 1.0
_BUSY_WAIT = 0.25
_PROGRESS_OPCODES = 1000

_TABLE = "ZAEBOOKINFO"
_KEY = "ZDATABASEKEY"
_TITLE, _AUTHOR = "ZBOOKTITLE", "ZBOOKAUTHOR"
# In CachedBookInfo field order (title, author, language, publisher, year).
_COLUMNS = (_TITLE, _AUTHOR, "ZBOOKLANGUAGE", "ZPUBLISHERNAME", "ZPUBLISHERYEAR")

# How a cache file is opened (the open rule; see _open_mode).
_JOURNAL = "journal"              # rollback journal: mode=ro
_WAL = "wal"                      # WAL, -wal and -shm present: mode=ro
_WAL_IMMUTABLE = "wal_immutable"  # WAL without them: mode=ro&immutable=1
_SQLITE_MAGIC = b"SQLite format 3\x00"
_HEADER_BYTES = 100
_O_HEADER = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_CLOEXEC", 0))


# -- arguments -----------------------------------------------------------------

_ARGUMENT_TYPES = "a str or an iterable of str (None and '' items are skipped)"


def _asset_id_list(asset_ids) -> List[str]:
    """``asset_ids`` as a list of distinct non-empty ids, in first-occurrence
    order. Raises :class:`InvalidArgumentError` naming types, never values."""
    if isinstance(asset_ids, str):
        items: Iterable = (asset_ids,)
    elif isinstance(asset_ids, (bytes, bytearray, memoryview)):
        raise InvalidArgumentError(f"asset_ids must be {_ARGUMENT_TYPES}, not {type(asset_ids).__name__}")
    else:
        try:
            items = iter(asset_ids)
        except TypeError:
            raise InvalidArgumentError(
                f"asset_ids must be {_ARGUMENT_TYPES}, not {type(asset_ids).__name__}") from None
    seen: Dict[str, None] = {}
    for item in items:
        if item is None:
            continue
        if not isinstance(item, str):
            raise InvalidArgumentError(
                f"asset_ids must be {_ARGUMENT_TYPES}; got an item of type {type(item).__name__}")
        if item:
            seen.setdefault(item if type(item) is str else str.__str__(item))
    return list(seen)


def _bindable(asset_id: str) -> bool:
    # A str with lone surrogates can't be bound (and can't equal any
    # cached key); leaving it out keeps it from failing the other ids.
    try:
        asset_id.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# -- the cache folder ----------------------------------------------------------

def _cache_folder(db) -> Optional[str]:
    """The absolute path of the AEBookInfo cache folder of the Books
    container holding ``db``'s library, derived without I/O; None when
    the library isn't in Books' container layout.

    A library store given as a file counts when it sits in a
    ``Documents/BKLibrary`` folder; otherwise the library's data folder
    (the default one if none was given) must be named ``Documents``.
    """
    from py_apple_books.db.client import default_data_dir  # no import cycle at module import

    try:
        store_file, data_dir = db._source("library")
        if store_file is not None:
            store_file = pathlib.Path(store_file)
            if store_file.parent.name != "BKLibrary":
                return None
            documents = store_file.parent.parent
        else:
            documents = pathlib.Path(data_dir) if data_dir is not None else default_data_dir()
        if documents.name != "Documents":
            return None
        return os.path.abspath(documents.parent / _FOLDER)
    except (RuntimeError, KeyError, OSError, ValueError, TypeError):
        # No home directory to derive the default from, or an unusable path.
        return None


def _in_cloud(path: str) -> bool:
    return any(part.casefold() in _CLOUD_PARTS for part in pathlib.PurePath(path).parts)


def _natural_key(name: str) -> tuple:
    # "26.10" after "26.7". re.split with a group alternates text and
    # digit runs, so the parts compared at one index have the same type.
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)), name


def _list_folder(folder: str) -> List[str]:
    """The names of the newest cache files in ``folder``, newest first;
    ``[]`` when there is no folder or it must not be read (it is, or
    resolves into, iCloud Drive or a cloud-storage folder; it is a
    symlink, not a folder, or evicted to iCloud). Raises ``OSError`` when
    it can't be looked up or listed for another reason (transient)."""
    if _in_cloud(folder):
        return []
    try:
        st = _icloud.lstat(folder)
    except (FileNotFoundError, NotADirectoryError):
        return []
    if not _stat.S_ISDIR(st.st_mode) or _icloud.is_dataless(st):
        return []
    if _in_cloud(os.path.realpath(folder)):
        return []
    names = [name for name in os.listdir(folder) if _NAME.fullmatch(name)]
    names.sort(key=_natural_key, reverse=True)
    return names[:_MAX_FILES]


# -- one cache file --------------------------------------------------------------

class _Refused(Exception):
    """The open rule refuses the file (message-free, private)."""


class _Drifted(Exception):
    """The file has no usable ZAEBOOKINFO table (message-free, private)."""


class _Changed(Exception):
    """An immutable read saw the file change (message-free, private)."""


def _lstat_or_none(path: str) -> Optional[os.stat_result]:
    """``lstat``, or None when nothing is there; other errors raise."""
    try:
        return _icloud.lstat(path)
    except FileNotFoundError:
        return None


def _local_regular(st: Optional[os.stat_result]) -> bool:
    return st is not None and _stat.S_ISREG(st.st_mode) and not _icloud.is_dataless(st)


def _identity(st: Optional[os.stat_result]) -> Optional[tuple]:
    if st is None:
        return None
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, _stat.S_IFMT(st.st_mode)


def _signature(path: str) -> tuple:
    """What a file's remembered rows are valid for: the identity of the
    file, its ``-wal`` and its ``-journal`` (``-shm`` is left out: a
    read-only WAL reader writes its read marks there)."""
    return tuple(_identity(_lstat_or_none(path + side)) for side in ("", "-wal", "-journal"))


def _files_signature(path: str) -> tuple:
    """The identity of the file and of every sidecar: an immutable read
    is valid only if none of them changed during it."""
    return tuple(_identity(_lstat_or_none(path + side)) for side in ("", "-wal", "-shm", "-journal"))


def _check_journal(path: str) -> None:
    # A mode=ro open checks for a hot rollback journal in every journal
    # mode: it opens a -journal that is there and reads its first byte.
    journal = _lstat_or_none(path + "-journal")
    if journal is not None and not _local_regular(journal):
        raise _Refused


def _open_mode(path: str) -> str:
    """How the cache file ``path`` is opened: ``_JOURNAL``, ``_WAL`` or
    ``_WAL_IMMUTABLE``; raises :class:`_Refused` if it must not be.

    The rule of ``py_apple_books.testing.dump_schema.book_info_open_mode``
    (the cases in ``tests/_bookinfo_cases.py`` pin both), chosen from the
    file's 100-byte header (read with ``O_NOFOLLOW``) and its sidecars so
    that a read never writes the cache, its journal or its WAL, never
    waits on Books beyond SQLite's own read locks, and creates no file:

    - rollback journal: ``mode=ro``, which takes SQLite's shared lock and
      refuses a hot journal. A ``-journal`` must be a local regular file;
      a ``-wal`` must be absent or empty (SQLite would open any other,
      whatever the header says, and create ``-shm``);
    - WAL whose ``-wal`` and ``-shm`` are local regular files: ``mode=ro``
      (sees the committed WAL content; SQLite writes its read marks into
      ``-shm``); a ``-journal`` must be a local regular file;
    - WAL without them (closed cleanly, one missing, or not a local
      regular file): ``mode=ro&immutable=1``, which opens no sidecar.

    The file must be a local regular file of at least 100 bytes with the
    SQLite magic. ``os.open``/``os.close`` drops every POSIX lock this
    process holds on the file, so this runs under ``_io_lock`` with no
    connection of this module open on it.
    """
    st = _lstat_or_none(path)
    if not _local_regular(st) or st.st_size < _HEADER_BYTES:
        raise _Refused
    head = b""
    fd = os.open(path, _O_HEADER)
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino) or not _stat.S_ISREG(opened.st_mode)
                or _icloud.is_dataless(opened)):
            raise _Refused
        while len(head) < _HEADER_BYTES:
            chunk = os.read(fd, _HEADER_BYTES - len(head))
            if not chunk:
                break
            head += chunk
    finally:
        os.close(fd)
    if len(head) < _HEADER_BYTES or not head.startswith(_SQLITE_MAGIC):
        raise _Refused
    # Bytes 18 and 19: the file format write and read versions, 2 for WAL.
    if head[18] != 2 and head[19] != 2:
        wal = _lstat_or_none(path + "-wal")
        if wal is not None and not (_local_regular(wal) and wal.st_size == 0):
            raise _Refused
        _check_journal(path)
        return _JOURNAL
    if not all(_local_regular(_lstat_or_none(path + side)) for side in ("-wal", "-shm")):
        return _WAL_IMMUTABLE
    _check_journal(path)
    return _WAL


class _Row(NamedTuple):
    """One cached book, in :class:`CachedBookInfo` field order."""

    title: Optional[str]
    author: Optional[str]
    language: Optional[str]
    publisher: Optional[str]
    year: Optional[str]


def _text(value) -> Optional[str]:
    """A cached value as text: '' (or only whitespace) and blobs read as
    None, integral numbers as digits (``2001`` and ``2001.0`` → '2001')."""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return str(int(value)) if value.is_integer() else repr(value)
    return None


def _row(values: Sequence) -> Optional[_Row]:
    row = _Row(*(_text(v) for v in values))
    return row if row.title is not None or row.author is not None else None


def _decode_lenient(value: bytes) -> str:
    return value.decode("utf-8", "replace")


def _connect(uri: str, busy: float) -> sqlite3.Connection:
    """A read-only connection to one cache (``uri``) that waits at most
    ``busy`` seconds for a lock Books holds. The tests' patch point."""
    return sqlite3.connect(uri, uri=True, timeout=busy, isolation_level=None, check_same_thread=False)


def _execute(con: sqlite3.Connection, sql: str, params: Sequence = ()) -> list:
    """``con.execute(sql, params).fetchall()``. The tests' patch point."""
    return con.execute(sql, params).fetchall()


def _capped(column: str) -> str:
    # length() of a blob counts every byte (of text, only those before a
    # NUL), so the cast makes the cap hold for any value.
    return f"CASE WHEN length(CAST({column} AS BLOB)) <= {_MAX_VALUE_BYTES} THEN {column} END"


def _select(con: sqlite3.Connection, have: set, ids: Sequence[str]) -> Dict[str, _Row]:
    """The best row per id: the newest row with a title, else the newest
    with an author (newest by ``Z_PK``). A value over
    :data:`_MAX_VALUE_BYTES` reads as NULL."""
    columns = ", ".join(_capped(c) if c in have else "NULL" for c in _COLUMNS)
    order = " ORDER BY Z_PK DESC" if "Z_PK" in have else ""
    wanted = set(ids)
    found: Dict[str, _Row] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = list(ids[start:start + _CHUNK])
        marks = ", ".join("?" for _ in chunk)
        sql = f"SELECT {_KEY}, {columns} FROM {_TABLE} WHERE {_KEY} IN ({marks}){order}"
        for key, *values in _execute(con, sql, chunk):
            if not isinstance(key, str) or key not in wanted:
                continue
            row = _row(values)
            if row is None:
                continue
            best = found.get(key)
            if best is None or (best.title is None and row.title is not None):
                found[key] = row
    return found


def _read_cache(path: str, ids: Sequence[str], deadline: float) -> Dict[str, Optional[_Row]]:
    """``{id: row or None}`` for ``ids`` from the cache file ``path``.

    Raises :class:`_Refused` (open rule), :class:`_Drifted` (no usable
    table), :class:`_Changed` (an immutable read saw the file change),
    ``sqlite3.Error`` (locked, interrupted at ``deadline``, unreadable) or
    ``OSError``.
    """
    mode = _open_mode(path)
    before = _files_signature(path) if mode == _WAL_IMMUTABLE else None
    uri = f"file:{quote(path)}?mode=ro" + ("&immutable=1" if mode == _WAL_IMMUTABLE else "")
    con = _connect(uri, max(0.0, min(_BUSY_WAIT, deadline - time.monotonic())))
    try:
        con.text_factory = _decode_lenient
        con.set_progress_handler(lambda: time.monotonic() > deadline, _PROGRESS_OPCODES)
        # The file is not ours: functions its schema uses (in a view or a
        # trigger) must be harmless ones. A no-op before SQLite 3.31.
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("PRAGMA query_only=1")
        con.execute("BEGIN")  # one consistent snapshot for every statement
        have = {str(r[1]).upper() for r in _execute(con, f"PRAGMA table_info({_TABLE})")}
        if _KEY not in have or not {_TITLE, _AUTHOR} & have:
            raise _Drifted
        found = _select(con, have, ids)
        con.execute("COMMIT")
    finally:
        con.close()
    # A read-only (not immutable) read is one consistent snapshot. An
    # immutable one is not protected by any lock: if Books wrote the file
    # meanwhile, the rows may be torn.
    if before is not None and _files_signature(path) != before:
        raise _Changed
    return {i: found.get(i) for i in ids}


# -- the per-library index ---------------------------------------------------------

# Serializes this process's reads of cache files: opening a file's header
# with os.open and closing it drops every POSIX lock the process holds on
# that file, including the read lock of a connection another thread has
# open on it (SQLite tracks only its own descriptors). Taken with a
# timeout, after an index's build lock and never while holding its
# state lock.
_io_lock = threading.Lock()


def _new_io_lock() -> None:
    # A fork copies the lock in whatever state a parent's thread left it.
    global _io_lock
    _io_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_new_io_lock)

# _FileMemo.unusable values.
_DRIFTED = "drifted"  # kept while the file is unchanged
_REFUSED = "refused"  # retried at the next look at the folder


class _FileMemo:
    """What one cache file gave, valid for the file's ``sig``: ``rows``
    maps id -> row (None: no usable row), least recently used first."""

    __slots__ = ("sig", "rows", "unusable")

    def __init__(self, sig: tuple, unusable: Optional[str] = None):
        self.sig = sig
        self.rows: "collections.OrderedDict[str, Optional[_Row]]" = collections.OrderedDict()
        self.unusable = unusable


class _BookInfoIndex:
    """The memo of one cache folder for one :class:`LibraryDB`
    (``db._derived_cache('book_info', ...)``, so ``close()`` and a fork
    drop it). Holds no reference to the library and no open connection.

    ``_build`` serializes the folder checks and file reads of lookups;
    ``_lock`` guards the fields and is held only around reads and writes
    of them (never during I/O).
    """

    def __init__(self, folder: str):
        self.folder = folder
        self._dead = False
        self._build = threading.Lock()
        self._lock = threading.Lock()
        self._names: Tuple[str, ...] = ()
        self._sigs: Dict[str, Optional[tuple]] = {}
        self._checked: Optional[float] = None
        self._memos: Dict[str, _FileMemo] = {}

    @property
    def dead(self) -> bool:
        return self._dead

    def discard(self) -> None:
        self._dead = True

    # -- lookups ----------------------------------------------------------------

    def lookup(self, ids: Sequence[str], deadline: float) -> Dict[str, Tuple[str, _Row]]:
        """``{id: (file name, row)}`` for the ids found, reading what the
        memo lacks until ``deadline``. Never raises for cache problems."""
        wait = deadline - time.monotonic()
        if not self._build.acquire(timeout=max(0.0, wait)):
            # Another thread is reading the files: answer from what is
            # known, without I/O.
            return self._resolve(ids, None)
        try:
            if time.monotonic() < deadline:
                self._check_folder()
            return self._resolve(ids, deadline)
        finally:
            self._build.release()

    def _check_folder(self) -> None:
        """List the folder and stat its files, at most every
        :data:`BOOK_INFO_RECHECK` seconds. Under ``_build``."""
        now = time.monotonic()
        with self._lock:
            checked = self._checked
        if checked is not None and now - checked < BOOK_INFO_RECHECK:
            return
        try:
            with _icloud.no_materialize():
                names = _list_folder(self.folder)
                sigs = {}
                for name in names:
                    try:
                        sigs[name] = _signature(os.path.join(self.folder, name))
                    except OSError as e:
                        _debug(name, e)
                        sigs[name] = None  # stat'ed again when needed
        except (OSError, ValueError) as e:
            # Transient (or a path the OS refuses, such as one holding a
            # NUL): keep what is known; look again at the next call.
            logger.debug("AEBookInfo cache folder not listed: %s", type(e).__name__)
            return
        with self._lock:
            self._names = tuple(name for name in names if not (sigs[name] and sigs[name][0] is None))
            self._sigs = sigs
            self._memos = {name: memo for name, memo in self._memos.items()
                           if name in sigs and memo.unusable != _REFUSED}
            self._checked = now

    def _resolve(self, ids: Sequence[str], deadline: Optional[float]) -> Dict[str, Tuple[str, _Row]]:
        """Merge the files' rows, newest file first: for each id the newest
        file with a title wins, else the newest with an author. Reads
        files (``deadline`` not None, under ``_build``) only for ids that
        still lack a title and the file's memo doesn't answer."""
        with self._lock:
            names = self._names
        need = [i for i in ids if _bindable(i)]
        found: Dict[str, Tuple[str, _Row]] = {}
        for name in names:
            if not need:
                break
            rows = self._file_rows(name, need, deadline)
            for i, row in rows.items():
                if row is None:
                    continue
                best = found.get(i)
                if best is None or (best[1].title is None and row.title is not None):
                    found[i] = (name, row)
            need = [i for i in need if not (i in found and found[i][1].title is not None)]
        return found

    def _file_rows(self, name: str, ids: List[str], deadline: Optional[float]) -> Dict[str, Optional[_Row]]:
        """The rows of one file for ``ids``: from its memo while the file
        is unchanged, else read (when ``deadline`` allows); on a failed
        read, the last good rows."""
        rows: Dict[str, Optional[_Row]] = {}
        want: List[str] = []
        with self._lock:
            memo = self._memos.get(name)
            fresh = memo is not None and memo.sig == self._sigs.get(name)
            if fresh and memo.unusable:
                return {}
            for i in ids:
                if fresh and i in memo.rows:
                    rows[i] = memo.rows[i]
                    memo.rows.move_to_end(i)
                else:
                    want.append(i)
        if not want:
            return rows
        got = self._read(name, want, deadline) if deadline is not None else None
        if got is None:
            # Not read (no time left, locked, changing, unreadable): the
            # last good rows, possibly of an older version of the file.
            with self._lock:
                memo = self._memos.get(name)
                if memo is not None and not memo.unusable:
                    rows.update((i, memo.rows[i]) for i in want if i in memo.rows)
            return rows
        rows.update(got)
        return rows

    def _read(self, name: str, ids: List[str], deadline: float) -> Optional[Dict[str, Optional[_Row]]]:
        """Read ``ids`` from one file and remember the rows; None on a
        transient failure (nothing new remembered)."""
        start = time.monotonic()
        if start >= deadline:
            return None
        file_deadline = min(deadline, start + _FILE_BUDGET)
        path = os.path.join(self.folder, name)
        lock = _io_lock
        if not lock.acquire(timeout=max(0.0, file_deadline - start)):
            return None
        try:
            with _icloud.no_materialize():
                sig = _signature(path)
                if sig[0] is None:  # purged since the folder was listed
                    with self._lock:
                        self._memos.pop(name, None)
                        self._sigs[name] = sig
                    return {}
                try:
                    got = _read_cache(path, ids, file_deadline)
                except (_Refused, _Drifted) as e:
                    with self._lock:
                        self._memos[name] = _FileMemo(sig, _REFUSED if isinstance(e, _Refused) else _DRIFTED)
                        self._sigs[name] = sig
                    _debug(name, e)
                    return {}
        except (sqlite3.Error, OSError, ValueError, _Changed) as e:
            _debug(name, e)
            return None
        finally:
            lock.release()
        with self._lock:
            memo = self._memos.get(name)
            if memo is None or memo.sig != sig or memo.unusable:
                memo = self._memos[name] = _FileMemo(sig)
            for i, row in got.items():
                memo.rows[i] = row
                memo.rows.move_to_end(i)
            while len(memo.rows) > _MEMO_IDS:
                memo.rows.popitem(last=False)
            self._sigs[name] = sig
        return got


def _debug(name: str, e: BaseException) -> None:
    # The bare file name and the exception class only: never a path, a
    # message (SQLite's may quote the schema) or a value.
    logger.debug("AEBookInfo cache %s skipped: %s", name, type(e).__name__.lstrip("_"))


def _index_for(db, folder: str) -> _BookInfoIndex:
    """``db``'s index for ``folder``. One index per library: a library
    whose folder changed (its environment changed) gets a new one."""
    for _ in range(3):
        index = db._derived_cache("book_info", lambda: _BookInfoIndex(folder))
        if index.folder == folder:
            return index
        index.discard()
    return _BookInfoIndex(folder)  # the folder keeps changing: not remembered


def _lookup(db, ids: Sequence[str]) -> Dict[str, CachedBookInfo]:
    """:meth:`PyAppleBooks.get_cached_book_info` for validated ``ids``."""
    deadline = time.monotonic() + _CALL_BUDGET
    cap, _ = db._statement_deadline()
    if cap is not None:
        deadline = min(deadline, cap)
    folder = _cache_folder(db)
    if folder is None or not ids:
        return {}
    found = _index_for(db, folder).lookup(ids, deadline)
    out: Dict[str, CachedBookInfo] = {}
    for i in ids:
        hit = found.get(i)
        if hit is not None:
            name, row = hit
            out[i] = CachedBookInfo(i, *row, source=name)
    return out

