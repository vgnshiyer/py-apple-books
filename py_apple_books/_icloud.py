"""Never download an evicted iCloud file by reading it (private).

Books keeps imported books in iCloud Drive
(``~/Library/Mobile Documents/iCloud~com~apple~iBooks/Documents``).
macOS may evict any file or directory there, leaving a *dataless*
placeholder: ``stat`` still works, but reading the file, or listing or
looking up a name inside the directory, makes macOS download it first.
A library that only reads must never cause that.

Four layers, all here:

* the database: Books marks a book it keeps only in iCloud with
  ``ZSTATE`` 3 (checked by ``PyAppleBooks.get_book_content``);
* ``lstat`` gates: :func:`is_dataless` on every directory before a name
  inside it is looked up, and on every entry before it is read
  (:func:`walk_bundle_local`, :func:`read_local`, and the book readers
  in :mod:`py_apple_books.content`);
* a backstop, :func:`no_materialize`: while it is active, the calling
  thread's I/O policy turns a read of a dataless file into an
  ``EDEADLK`` error instead of a download (:func:`not_downloaded_error`
  maps it to :class:`BookNotDownloadedError`);
* :func:`read_local`: a strict ``openat`` reader for small metadata
  files.

Also the registry of caches derived from book files
(:func:`register_file_cache`, :func:`clear_file_caches`).

Standard library only, and nothing happens at import: ``ctypes`` is
loaded on the first :func:`no_materialize`.
"""

import contextlib
import enum
import errno
import os
import stat as _stat
import sys
import threading
from typing import Callable, Iterator, List, Optional, Tuple

from py_apple_books.exceptions import BookNotDownloadedError

# ``stat.SF_DATALESS`` only exists on Python 3.13+.
SF_DATALESS = 0x40000000
UF_COMPRESSED = 0x20

# <sys/resource.h>
IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES = 3
IOPOL_SCOPE_PROCESS = 0
IOPOL_SCOPE_THREAD = 1
IOPOL_MATERIALIZE_DATALESS_FILES_OFF = 1

# What a read of a dataless file fails with while materialization is off
# (EDEADLK), or when a download it started didn't finish (ETIMEDOUT).
MATERIALIZE_ERRNOS = frozenset({errno.EDEADLK, errno.ETIMEDOUT})

PARTIAL_DOWNLOAD_MESSAGE = (
    "Part of this book is stored only in iCloud. Open it in Apple Books "
    "to download it, then try again."
)

_LIBSYSTEM = "/usr/lib/libSystem.B.dylib"


# ---------------------------------------------------------------------------
# stat (the patch points for tests)
# ---------------------------------------------------------------------------


def lstat(path, *, dir_fd: Optional[int] = None) -> os.stat_result:
    """``os.lstat``. Every gate here and in :mod:`py_apple_books.content`
    stats through this function (or :func:`stat`), so a test can mark
    any entry dataless by patching it."""
    return os.lstat(path, dir_fd=dir_fd)


def stat(path) -> os.stat_result:
    """``os.stat`` (follows symlinks); see :func:`lstat`."""
    return os.stat(path)


def flags(st) -> int:
    """``st_flags`` of a stat result; 0 where the platform has none
    (Linux)."""
    return getattr(st, "st_flags", 0) or 0


def is_dataless(st) -> bool:
    """True if a stat result is an iCloud placeholder.

    Anything with ``SF_DATALESS`` set; and a regular file whose size is
    not backed by any block, unless it is compressed (APFS compressed
    files report 0 blocks). Directories are judged by the flag alone:
    APFS always reports 0 blocks for a directory.
    """
    fl = flags(st)
    if fl & SF_DATALESS:
        return True
    return (
        _stat.S_ISREG(st.st_mode)
        and st.st_size > 0
        and getattr(st, "st_blocks", 1) == 0
        and not fl & UF_COMPRESSED
    )


def icloud_stub(path) -> bool:
    """True if ``path`` has an iCloud stub next to it (``.<name>.icloud``,
    what older macOS versions leave in place of an evicted file)."""
    head, name = os.path.split(os.fspath(path).rstrip(os.sep))
    if not name:
        return False
    return os.path.lexists(os.path.join(head, f".{name}.icloud"))


def _is_stub_name(name: str) -> bool:
    return len(name) > len("..icloud") and name.startswith(".") and name.endswith(".icloud")


# ---------------------------------------------------------------------------
# The materialization backstop
# ---------------------------------------------------------------------------


_policy_lock = threading.Lock()
_policy_loaded = False
_policy_fns: Optional[Tuple[Callable, Callable]] = None


def _load_policy_functions() -> Optional[Tuple[Callable, Callable]]:
    """``(getiopolicy_np, setiopolicy_np)`` from libSystem, or None off
    macOS or on any failure."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes

        libc = ctypes.CDLL(_LIBSYSTEM, use_errno=True)
        get, set_ = libc.getiopolicy_np, libc.setiopolicy_np
        get.argtypes = [ctypes.c_int, ctypes.c_int]
        get.restype = ctypes.c_int
        set_.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        set_.restype = ctypes.c_int
        return get, set_
    except Exception:  # noqa: BLE001 (no backstop is the documented fallback)
        return None


def _policy_functions() -> Optional[Tuple[Callable, Callable]]:
    """The policy functions, looked up once (under a lock) on first use."""
    global _policy_loaded, _policy_fns
    if not _policy_loaded:
        with _policy_lock:
            if not _policy_loaded:
                _policy_fns = _load_policy_functions()
                _policy_loaded = True
    return _policy_fns


@contextlib.contextmanager
def no_materialize() -> Iterator[None]:
    """Within the block, a read by this thread of a dataless file fails
    with ``EDEADLK`` instead of downloading it.

    Sets the thread's ``IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES``
    policy to off and restores the value it had in ``finally``, so
    blocks nest and other threads are unaffected. A silent no-op off
    macOS, or if the policy can't be read or set: the ``lstat`` gates
    stay the primary defence. Don't hold it across a ``yield``.
    """
    fns = _policy_functions()
    if fns is None:
        yield
        return
    get, set_ = fns
    try:
        previous = get(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_THREAD)
        changed = previous >= 0 and set_(
            IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_THREAD,
            IOPOL_MATERIALIZE_DATALESS_FILES_OFF) == 0
    except Exception:  # noqa: BLE001 (see the docstring)
        changed = False
    try:
        yield
    finally:
        if changed:
            try:
                set_(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_THREAD, previous)
            except Exception:  # noqa: BLE001
                pass


def disable_materialization_for_process() -> bool:
    """Turn materialization off for the whole process (and the processes
    it starts). For short-lived worker processes only: the library's
    own process never calls it. True if the policy is now off."""
    fns = _policy_functions()
    if fns is None:
        return False
    get, set_ = fns
    try:
        if set_(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_PROCESS,
                IOPOL_MATERIALIZE_DATALESS_FILES_OFF) != 0:
            return False
        return get(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
                   IOPOL_SCOPE_PROCESS) == IOPOL_MATERIALIZE_DATALESS_FILES_OFF
    except Exception:  # noqa: BLE001
        return False


def is_materialize_error(exc: BaseException) -> bool:
    """True if ``exc`` is the ``OSError`` a read of a dataless file fails
    with (see :data:`MATERIALIZE_ERRNOS`)."""
    return isinstance(exc, OSError) and exc.errno in MATERIALIZE_ERRNOS


def not_downloaded_error(exc: BaseException) -> Optional[BookNotDownloadedError]:
    """The :class:`BookNotDownloadedError` to raise (``from None``) for
    ``exc``, if it is a read of a dataless file; else None."""
    if is_materialize_error(exc):
        return BookNotDownloadedError(PARTIAL_DOWNLOAD_MESSAGE)
    return None


# ---------------------------------------------------------------------------
# File and bundle state
# ---------------------------------------------------------------------------


class FileState(str, enum.Enum):
    """Whether a book file or bundle is on this Mac."""

    LOCAL = "local"
    DATALESS = "dataless"
    ICLOUD_STUB = "icloud_stub"
    MISSING = "missing"
    NOT_REGULAR = "not_regular"


def local_file_state(path) -> FileState:
    """The state of a single book file (a PDF), from ``lstat`` alone.

    Its folder is checked first, so nothing is looked up inside a
    dataless folder. A symlink, a folder or a special file is
    ``NOT_REGULAR``. Runs under :func:`no_materialize`.
    """
    path = os.fspath(path)
    with no_materialize():
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if is_dataless(lstat(parent)):
                return FileState.DATALESS
            if icloud_stub(path):
                return FileState.ICLOUD_STUB
            st = lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return FileState.MISSING
        except OSError as e:
            if is_materialize_error(e):
                return FileState.DATALESS
            return FileState.MISSING
    if is_dataless(st):
        return FileState.DATALESS
    if not _stat.S_ISREG(st.st_mode):
        return FileState.NOT_REGULAR
    return FileState.LOCAL


def walk_bundle_local(root) -> FileState:
    """Whether every file and folder of a book bundle is on this Mac.

    An ``lstat``-first walk: a folder is listed only after its own
    ``lstat`` shows it is not dataless, and the walk stops at the first
    dataless entry (``DATALESS``) or iCloud stub (``ICLOUD_STUB``).
    Symlinks are not followed (a contained link's target is walked in
    its own place). ``root`` itself may be a single file. ``MISSING``
    if ``root`` doesn't exist; folders that can't be listed for another
    reason (permissions) are skipped, so the read itself reports them.
    Runs under :func:`no_materialize`.
    """
    root = os.fspath(root)
    with no_materialize():
        try:
            if icloud_stub(root):
                return FileState.ICLOUD_STUB
            st = lstat(root)
            if _stat.S_ISLNK(st.st_mode):
                st = stat(root)
        except (FileNotFoundError, NotADirectoryError):
            return FileState.MISSING
        except OSError as e:
            return FileState.DATALESS if is_materialize_error(e) else FileState.MISSING
        if is_dataless(st):
            return FileState.DATALESS
        if not _stat.S_ISDIR(st.st_mode):
            return FileState.LOCAL
        return _walk_directories(root)


def _walk_directories(root: str) -> FileState:
    pending: List[str] = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as it:
                entries = [entry.name for entry in it]
        except OSError as e:
            if is_materialize_error(e):
                return FileState.DATALESS
            continue
        for name in entries:
            if _is_stub_name(name):
                return FileState.ICLOUD_STUB
            path = os.path.join(directory, name)
            try:
                st = lstat(path)
            except OSError as e:
                if is_materialize_error(e):
                    return FileState.DATALESS
                continue
            if is_dataless(st):
                return FileState.DATALESS
            if _stat.S_ISDIR(st.st_mode):
                pending.append(path)
    return FileState.LOCAL


# ---------------------------------------------------------------------------
# Strict reader for small metadata files
# ---------------------------------------------------------------------------


class _LocalReadError(Exception):
    """Base of :func:`read_local`'s refusals. Carries no message: the
    caller maps the class to its own wording or reason code."""


class _NotLocal(_LocalReadError):
    """Part of the path is an iCloud placeholder (or a read of it would
    have downloaded it)."""


class _Missing(_LocalReadError):
    """The file, or a folder on the way to it, doesn't exist."""


class _Unsafe(_LocalReadError):
    """A bad name (absolute, ``..``, NUL), a symlink, a special file, or
    a file swapped during the read."""


class _TooLarge(_LocalReadError):
    """Larger than ``max_bytes``."""


class _IOFailed(_LocalReadError):
    """Any other I/O error (permissions, ...)."""


_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_CHUNK = 64 * 1024


def _parts(rel) -> List[str]:
    """The components of a bundle-relative name, refusing anything that
    could leave the bundle."""
    if not isinstance(rel, str) or not rel or "\0" in rel or rel.startswith("/"):
        raise _Unsafe()
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise _Unsafe()
    return parts


def _os_error(e: OSError) -> _LocalReadError:
    if is_materialize_error(e):
        return _NotLocal()
    if isinstance(e, (FileNotFoundError, NotADirectoryError)):
        return _Missing()
    if e.errno == errno.ELOOP:
        return _Unsafe()
    return _IOFailed()


def _same_file(a, b) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _open_parent(root, parts: List[str]) -> int:
    """An fd of the folder holding ``parts[-1]``, every folder on the way
    checked not to be dataless before anything inside it is looked up,
    and none of them a symlink (the root may be one)."""
    st = stat(root)
    if is_dataless(st):
        raise _NotLocal()
    if not _stat.S_ISDIR(st.st_mode):
        raise _Missing()
    fd = os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        if not _same_file(os.fstat(fd), st):
            raise _Unsafe()
        for name in parts[:-1]:
            st = lstat(name, dir_fd=fd)
            if is_dataless(st):
                raise _NotLocal()
            if _stat.S_ISLNK(st.st_mode):
                raise _Unsafe()
            if not _stat.S_ISDIR(st.st_mode):
                raise _Missing()
            child = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            if not _same_file(os.fstat(fd), st):
                raise _Unsafe()
        return fd
    except BaseException:
        os.close(fd)
        raise


def _leaf_stat(fd: int, name: str, max_bytes: Optional[int]) -> os.stat_result:
    st = lstat(name, dir_fd=fd)
    if is_dataless(st):
        raise _NotLocal()
    if not _stat.S_ISREG(st.st_mode):
        raise _Unsafe()
    if max_bytes is not None and st.st_size > max_bytes:
        raise _TooLarge()
    return st


def stat_local(root, rel) -> os.stat_result:
    """The ``lstat`` of bundle entry ``rel`` under ``root``, through the
    same checks as :func:`read_local`, without opening it.

    :raises _NotLocal, _Missing, _Unsafe, _IOFailed: see :func:`read_local`.
    """
    parts = _parts(rel)
    with no_materialize():
        try:
            fd = _open_parent(root, parts)
            try:
                return _leaf_stat(fd, parts[-1], None)
            finally:
                os.close(fd)
        except OSError as e:
            raise _os_error(e) from None


def read_local(root, rel, *, max_bytes: int, sink: Optional[Callable[[bytes], object]] = None):
    """Read the small file ``rel`` (``/``-separated) inside the folder
    ``root``, strictly: only if it and every folder on the way are on
    this Mac, nothing is a symlink below ``root``, and it is a regular
    file of at most ``max_bytes``.

    Each folder is ``fstatat``-ed (no symlink follow) and checked not to
    be dataless before the next name is looked up in it, then opened
    ``O_NOFOLLOW``; the file is opened ``O_NOFOLLOW | O_NONBLOCK`` (a
    FIFO swapped in can't block) and re-checked with ``fstat`` to be the
    file that was checked. At most ``max_bytes + 1`` bytes are read, so
    a file that grows past the limit is refused rather than read. Runs
    under :func:`no_materialize`.

    :param sink: if given, called with each chunk (never more than
        ``max_bytes`` bytes in total), and the total is returned;
        otherwise the bytes are returned.
    :raises _NotLocal: a dataless folder or file on the way.
    :raises _Missing: it doesn't exist.
    :raises _Unsafe: a bad name, a symlink, a special file, or a file
        swapped during the read.
    :raises _TooLarge: over ``max_bytes``.
    :raises _IOFailed: any other I/O error.
    """
    parts = _parts(rel)
    chunks: List[bytes] = []
    total = 0
    with no_materialize():
        try:
            fd = _open_parent(root, parts)
            try:
                st = _leaf_stat(fd, parts[-1], max_bytes)
                leaf = os.open(parts[-1], os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=fd)
            finally:
                os.close(fd)
            try:
                opened = os.fstat(leaf)
                if not _same_file(opened, st) or not _stat.S_ISREG(opened.st_mode):
                    raise _Unsafe()
                if is_dataless(opened):
                    raise _NotLocal()
                while True:
                    chunk = os.read(leaf, min(_CHUNK, max_bytes + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise _TooLarge()
                    if sink is None:
                        chunks.append(chunk)
                    else:
                        sink(chunk)
            finally:
                os.close(leaf)
        except OSError as e:
            raise _os_error(e) from None
    return b"".join(chunks) if sink is None else total


# ---------------------------------------------------------------------------
# Caches derived from book files
# ---------------------------------------------------------------------------


_caches_lock = threading.Lock()
_cache_clearers: List[Callable[[], None]] = []


def register_file_cache(clear_fn: Callable[[], None]) -> None:
    """Have :func:`clear_file_caches` call ``clear_fn``. For modules that
    cache anything read from book files; call at import (no I/O).
    Registering the same function again does nothing."""
    with _caches_lock:
        if clear_fn not in _cache_clearers:
            _cache_clearers.append(clear_fn)


def clear_file_caches() -> None:
    """Call every registered clear function (each one even if another
    raises; the first error is re-raised afterwards)."""
    with _caches_lock:
        clearers = list(_cache_clearers)
    first: Optional[BaseException] = None
    for clear in clearers:
        try:
            clear()
        except Exception as e:  # noqa: BLE001 (re-raised below)
            if first is None:
                first = e
    if first is not None:
        raise first
