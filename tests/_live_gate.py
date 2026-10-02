"""Which books the opt-in live test may open: only those wholly on disk.

Apple Books keeps books in iCloud Drive, and macOS evicts files it can
download again: the entry stays, flagged SF_DATALESS, and reading it (or
listing an evicted directory) downloads it. The live test must never do
that, so before it opens a book it ``lstat``s (which never downloads)
every folder above the bundle from "/" down, then walks the whole
bundle, listing a directory only after its own ``lstat`` showed it is
local, and skips the book at the first entry that is not. It opens
only bundle directories: a single-file book (a PDF, a packed ``.epub``)
is skipped, since reading it is not what the live test checks.

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
MAX_LINKS = 32  # symlinked folders followed above a bundle

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


def _local_ancestors(path: str) -> Optional[str]:
    """Check every folder above ``path`` from "/" down, without ever
    looking up a name inside a folder not yet shown to be local.

    Returns None when they are all local, else a fixed reason. A
    symlinked folder is followed by hand (its target is checked the
    same way): macOS's ``/var`` and ``/tmp`` are links.
    """
    parts = [p for p in os.path.dirname(os.path.abspath(path)).split(os.sep) if p]
    cur, links = os.sep, 0
    while parts:
        name = parts.pop(0)
        if name in (".", ""):
            continue
        if name == "..":
            cur = os.path.dirname(cur)
            continue
        nxt = os.path.join(cur, name)
        if os.path.lexists(os.path.join(cur, f".{name}.icloud")):
            return "icloud stub"
        st = os.lstat(nxt)
        if is_dataless(st):
            return "dataless"
        if stat.S_ISLNK(st.st_mode):
            links += 1
            if links > MAX_LINKS:
                return "symlink"
            target = os.readlink(nxt)
            parts = [p for p in target.split(os.sep) if p] + parts
            if os.path.isabs(target):
                cur = os.sep
            continue
        if not stat.S_ISDIR(st.st_mode):
            return "missing"  # a file where a folder should be: the path can't exist
        cur = nxt
    return None


def skip_reason(path, state) -> Optional[str]:
    """Why the book at ``path`` must not be opened, or None if Books marks
    it downloaded (ZSTATE 1), it is a bundle directory, and every folder
    above it and every directory and file in it is on disk.

    Reasons are fixed words (never a path): ``state``, ``no path``,
    ``missing``, ``dataless``, ``icloud stub``, ``not a bundle`` (a
    single file such as a PDF), ``symlink``, ``special file``,
    ``unreadable``.
    """
    if state != STATE_LOCAL:
        return "state"
    if not path:
        return "no path"
    root = os.path.abspath(os.fspath(path)).rstrip(os.sep) or os.sep
    try:
        reason = _local_ancestors(root)
        if reason:
            return reason
        # The parent is local now, so looking names up in it is safe.
        parent, name = os.path.split(root)
        if os.path.lexists(os.path.join(parent, f".{name}.icloud")):
            return "icloud stub"
        st = os.lstat(root)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if is_dataless(st):
        return "dataless"
    if stat.S_ISREG(st.st_mode):
        return "not a bundle"
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
