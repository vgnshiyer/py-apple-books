"""Which books the opt-in live test may open: only those wholly on disk.

Apple Books keeps books in iCloud Drive, and macOS evicts files it can
download again: the entry stays, flagged SF_DATALESS, and reading it (or
listing an evicted directory) downloads it. The live test must never do
that, so before it opens a book it walks the whole bundle with ``lstat``
(which never downloads), lists a directory only after its own ``lstat``
showed it is local, and skips the book at the first entry that is not.

Also here: the process-wide "don't download" I/O policy, so a read the
gate missed fails (EDEADLK) instead of downloading. It is meant for the
short-lived live-test subprocess only.

Stdlib only; no py_apple_books import.
"""

from __future__ import annotations

import os
import stat
import sys
from typing import Optional

SF_DATALESS = 0x40000000  # stat.SF_DATALESS is missing before Python 3.13
UF_COMPRESSED = 0x20
STATE_LOCAL = 1  # ZSTATE: 1 on this Mac, 3 iCloud only

# setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_PROCESS,
#                IOPOL_MATERIALIZE_DATALESS_FILES_OFF)
IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES = 3
IOPOL_SCOPE_PROCESS = 0
IOPOL_MATERIALIZE_DATALESS_FILES_OFF = 1


def is_dataless(st) -> bool:
    """Whether an ``lstat`` result is an evicted (cloud-only) entry.

    SF_DATALESS for anything; for regular files also "has a size but no
    blocks" unless the file is compressed. Directories are judged by the
    flag alone: APFS directories always report zero blocks.
    """
    flags = getattr(st, "st_flags", 0)  # Linux has no st_flags
    if flags & SF_DATALESS:
        return True
    return (stat.S_ISREG(st.st_mode) and st.st_size > 0 and getattr(st, "st_blocks", 1) == 0
            and not flags & UF_COMPRESSED)


def _icloud_stub(path: str) -> bool:
    parent, name = os.path.split(path.rstrip(os.sep))
    return os.path.lexists(os.path.join(parent, f".{name}.icloud"))


def skip_reason(path, state) -> Optional[str]:
    """Why the book at ``path`` must not be opened, or None if Books marks
    it downloaded (ZSTATE 1) and every directory and file of it is on disk.

    Reasons are fixed words (never a path): ``state``, ``no path``,
    ``missing``, ``dataless``, ``icloud stub``, ``symlink``, ``special
    file``, ``unreadable``.
    """
    if state != STATE_LOCAL:
        return "state"
    if not path:
        return "no path"
    root = os.fspath(path)
    try:
        if _icloud_stub(root):
            return "icloud stub"
        st = os.lstat(root)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if is_dataless(st):
        return "dataless"
    if stat.S_ISREG(st.st_mode):
        return None  # a single file (a PDF): its lstat was enough
    if not stat.S_ISDIR(st.st_mode):
        return "symlink" if stat.S_ISLNK(st.st_mode) else "special file"
    pending = [root]  # directories whose own lstat showed them local
    try:
        while pending:
            with os.scandir(pending.pop()) as entries:
                names = [(e.name, e.path) for e in entries]
            for name, entry in names:
                if name.startswith(".") and name.endswith(".icloud"):
                    return "icloud stub"
                st = os.lstat(entry)
                if is_dataless(st):
                    return "dataless"
                if stat.S_ISDIR(st.st_mode):
                    pending.append(entry)
                elif stat.S_ISLNK(st.st_mode):
                    return "symlink"
                elif not stat.S_ISREG(st.st_mode):
                    return "special file"
    except OSError:
        return "unreadable"
    return None


def disable_materialization() -> bool:
    """Turn off downloads of evicted files for this whole process (and
    the processes it starts). True if the policy is now on; False off
    macOS or on any failure. For a short-lived test subprocess only."""
    if sys.platform != "darwin":
        return False
    try:
        import ctypes

        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        if libc.setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_PROCESS,
                               IOPOL_MATERIALIZE_DATALESS_FILES_OFF) != 0:
            return False
        return (libc.getiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_PROCESS)
                == IOPOL_MATERIALIZE_DATALESS_FILES_OFF)
    except (OSError, AttributeError):
        return False
