"""Books' preferences plist: the reading goals (private; 1.11).

Books keeps its reading goals only in its preferences file,
``<container>/Data/Library/Preferences/com.apple.iBooksX.plist``, a
binary property list cfprefsd writes (never under iCloud Drive). Plain
``plistlib.loads`` rejects the whole file, because Books stores a
year-0 date in it. This module reads it defensively and keeps four keys:

- the path is derived from the library's configured source, without
  opening SQLite (:func:`library_prefs_path`);
- a path under iCloud Drive (``Mobile Documents``) or a File Provider
  folder (``CloudStorage``) is refused before any file system call, so
  a placeholder folder is never even looked up in;
- the file must be a regular local file: ``lstat`` first, then an
  ``O_NOFOLLOW | O_NONBLOCK`` open and an ``fstat`` that must show the
  same file, not an iCloud placeholder, at most :data:`MAX_BYTES`; all
  under :func:`_icloud.no_materialize`, so a missed placeholder fails
  instead of downloading;
- dates a ``datetime`` can't hold (Books' year 0, NaN) are patched to a
  sentinel before parsing and read back as None;
- any problem gives None and a reason code; only the reason code is
  logged (DEBUG), never a path or a value.

A custom ``prefs_path`` is checked by name only: a symlinked parent
folder that leads into iCloud Drive is not detected (``O_NOFOLLOW``
protects the last component only).

Nothing here does I/O at import; ``plistlib`` is imported on first use.
"""

import logging
import math
import os
import re
import stat
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from py_apple_books import _icloud
from py_apple_books.db.client import default_data_dir, default_library
from py_apple_books.engagement import ReadingGoals

logger = logging.getLogger(__name__)

#: Where Books keeps its preferences, relative to the container's ``Data``
#: folder (the parent of ``Documents``).
PREFS_FILE = Path("Library", "Preferences", "com.apple.iBooksX.plist")
#: The largest preferences file read.
MAX_BYTES = 8 * 1024 * 1024

# Reason codes of _read_goals.
OK = "ok"
NO_PATH = "no_path"
ICLOUD_PATH = "icloud_path"
MISSING = "missing"
NOT_REGULAR = "not_regular"
DATALESS = "dataless"
TOO_LARGE = "too_large"
UNREADABLE = "unreadable"
UNPARSEABLE = "unparseable"
NOT_DICT = "not_dict"

# Folder names (case-insensitive) whose contents may be cloud placeholders.
_CLOUD_FOLDERS = frozenset({"mobile documents", "cloudstorage"})

# struct codes of the binary plist integer sizes (big-endian).
_OFFSET_CODES = {1: "B", 2: "H", 4: "L", 8: "Q"}

_O_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0))
_CHUNK = 64 * 1024

# Binary plist dates are seconds since 2001-01-01 UTC; plistlib turns
# them into datetimes, which fails outside these bounds.
_EPOCH = datetime(2001, 1, 1)
_DATE_MIN = (datetime(1, 1, 2) - _EPOCH).total_seconds()
_DATE_MAX = (datetime(9999, 12, 30) - _EPOCH).total_seconds()
# What an unreadable date is patched to: 2000-01-01 00:00:00.5 UTC, a
# valid date Books never writes (it writes whole seconds and later
# dates), read back as None.
_SENTINEL_SECONDS = -31622399.5
_SENTINEL = _EPOCH + timedelta(seconds=_SENTINEL_SECONDS)
# The same for XML plists (whole seconds only): 0001-01-01T00:00:00Z.
_XML_SENTINEL = datetime(1, 1, 1)
_XML_DATE = re.compile(rb"<date>([^<]*)</date>")
# plistlib's own XML date pattern.
_XML_DATE_VALUE = re.compile(
    rb"\s*(\d\d\d\d)(?:-(\d\d)(?:-(\d\d)(?:T(\d\d)(?::(\d\d)(?::(\d\d))?)?)?)?)?Z\s*")

_BOOKS_GOAL = "ReadingGoals.BooksFinished"
_DAILY_GOAL = "ReadingGoals.StreakDay"
_STREAK = "ReadingHistory.CurrentStreak"
_FINISHED = "BKFinishedAssetsCache"


# -- the path -----------------------------------------------------------------


def prefs_path_for(library_file: Optional[Path], data_dir: Optional[Path]) -> Optional[Path]:
    """The preferences file of a library given as a store file and/or a
    Documents folder (``LibraryDB._source('library')``), without I/O.

    A library store file sitting in ``Documents/BKLibrary`` (names
    compared case-insensitively) gives ``<Documents parent>/`` +
    :data:`PREFS_FILE`; any other store file gives None (a copied store
    has no preferences next to it). Otherwise the data folder (default:
    the current user's container) must be named ``Documents``.
    """
    if library_file is not None:
        store = Path(os.path.abspath(library_file))
        folder, documents = store.parent, store.parent.parent
        if folder.name.casefold() == "bklibrary" and documents.name.casefold() == "documents":
            return documents.parent / PREFS_FILE
        return None
    documents = Path(os.path.abspath(data_dir if data_dir is not None else default_data_dir()))
    if documents.name.casefold() == "documents":
        return documents.parent / PREFS_FILE
    return None


def library_prefs_path(db) -> Optional[Path]:
    """The preferences file of the library ``db`` reads (its explicit
    arguments, else the ``APPLE_BOOKS_*`` variables, else the current
    user's container); None when it can't be told. No I/O."""
    return prefs_path_for(*db._source("library"))


def default_prefs_path() -> Optional[Path]:
    """The preferences file of the default library (``PyAppleBooks()``)."""
    return library_prefs_path(default_library())


def _in_cloud_folder(path: str) -> bool:
    return any(part.casefold() in _CLOUD_FOLDERS for part in Path(os.path.abspath(path)).parts)


# -- reading ------------------------------------------------------------------


def _read_bytes(path: str) -> Tuple[Optional[bytes], str, Optional[float]]:
    """``(data, reason, mtime)``: the file's bytes, or None and why."""
    with _icloud.no_materialize():
        try:
            st = _icloud.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None, MISSING, None
        except (OSError, ValueError) as e:
            return None, (DATALESS if _icloud.is_materialize_error(e) else UNREADABLE), None
        if not stat.S_ISREG(st.st_mode):
            return None, NOT_REGULAR, None
        if _icloud.is_dataless(st):
            return None, DATALESS, None
        if st.st_size > MAX_BYTES:
            return None, TOO_LARGE, None
        try:
            fd = os.open(path, _O_FLAGS)
        except OSError as e:
            if _icloud.is_materialize_error(e):
                return None, DATALESS, None
            # ELOOP: the name became a symlink since the lstat.
            return None, (MISSING if isinstance(e, FileNotFoundError) else
                          NOT_REGULAR if e.errno == getattr(os, "ELOOP", None) else UNREADABLE), None
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(opened.st_mode):
                return None, NOT_REGULAR, None
            if _icloud.is_dataless(opened):
                return None, DATALESS, None
            if opened.st_size > MAX_BYTES:
                return None, TOO_LARGE, None
            chunks, size = [], 0
            while size <= MAX_BYTES:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > MAX_BYTES:
                return None, TOO_LARGE, None
            return b"".join(chunks), OK, opened.st_mtime
        except OSError as e:
            return None, (DATALESS if _icloud.is_materialize_error(e) else UNREADABLE), None
        finally:
            os.close(fd)


def _patch_binary_dates(data: bytes) -> bytes:
    """``data`` (a binary plist) with every date object a datetime can't
    hold set to the sentinel. Raises ValueError for a malformed trailer
    (the parser would refuse the file anyway).

    The offset table is unpacked in one call and each distinct offset is
    looked at once, so a crafted table repeating one entry costs about
    what plistlib's own read of the table does."""
    if len(data) < 40:
        raise ValueError("truncated")
    offset_size, ref_size, count, _top, table = struct.unpack(">6xBBQQQ", data[-32:])
    if (offset_size not in _OFFSET_CODES or ref_size not in _OFFSET_CODES or count > len(data)
            or table + count * offset_size > len(data) - 32):
        raise ValueError("bad trailer")
    offsets = set(struct.unpack_from(f">{count}{_OFFSET_CODES[offset_size]}", data, table))
    patched = bytearray(data)
    for offset in offsets:
        if offset + 9 <= table and data[offset] == 0x33:
            (seconds,) = struct.unpack(">d", data[offset + 1:offset + 9])
            if not (math.isfinite(seconds) and _DATE_MIN <= seconds <= _DATE_MAX):
                patched[offset + 1:offset + 9] = struct.pack(">d", _SENTINEL_SECONDS)
    return bytes(patched)


def _patch_xml_dates(data: bytes) -> bytes:
    """``data`` (an XML plist) with every date a datetime can't hold set
    to the XML sentinel."""
    def fix(match):
        value = _XML_DATE_VALUE.fullmatch(match.group(1))
        if value is None:
            return match.group(0)  # not a date plistlib reads: it refuses the file
        parts = [int(p) for p in value.groups() if p is not None]
        try:
            datetime(*(parts + [1, 1][:max(0, 3 - len(parts))]))
        except ValueError:
            return b"<date>0001-01-01T00:00:00Z</date>"
        return match.group(0)

    return _XML_DATE.sub(fix, data)


def _parse(data: bytes):
    import plistlib  # on first use

    data = _patch_binary_dates(data) if data.startswith(b"bplist00") else _patch_xml_dates(data)
    return plistlib.loads(data)


# -- values -------------------------------------------------------------------


def _date(value) -> Optional[datetime]:
    """A plist date as naive local time (like the model dates); None for
    anything else, a patched date, or one out of local range."""
    if not isinstance(value, datetime):
        return None
    utc = value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if utc in (_SENTINEL, _XML_SENTINEL):
        return None
    try:
        return utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _count(value) -> Optional[int]:
    """A non-negative int (not a bool), else None."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _seconds(value) -> Optional[float]:
    """A finite, non-negative number of seconds (not a bool), else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return number
    return None


def _goal(doc: dict, key: str) -> Tuple[object, Optional[datetime]]:
    """``(goal, date set)`` of a ``ReadingGoals.*`` entry."""
    entry = doc.get(key)
    if not isinstance(entry, dict):
        return None, None
    return entry.get("goal"), _date(entry.get("date"))


def _goals(doc: dict, mtime: Optional[float]) -> ReadingGoals:
    books, books_set = _goal(doc, _BOOKS_GOAL)
    daily, daily_set = _goal(doc, _DAILY_GOAL)
    finished = doc.get(_FINISHED)
    entries = []
    if isinstance(finished, dict):
        entries = [(asset, _date(when)) for asset, when in finished.items() if isinstance(asset, str)]
    dated = sorted(((a, w) for a, w in entries if w is not None), key=lambda e: (e[1], e[0]))
    undated = sorted((a, w) for a, w in entries if w is None)
    try:
        modified = None if mtime is None else datetime.fromtimestamp(mtime)
    except (OverflowError, OSError, ValueError):
        modified = None
    return ReadingGoals(
        books_per_year=_count(books),
        books_goal_set=books_set,
        daily_goal_seconds=_seconds(daily),
        daily_goal_set=daily_set,
        apple_current_streak=_count(doc.get(_STREAK)),
        finished_assets=tuple(dated + undated),
        modified=modified,
    )


def _read_goals(path) -> Tuple[Optional[ReadingGoals], str]:
    """``(ReadingGoals or None, reason code)`` for the preferences file
    at ``path`` (None: no path). Never raises for I/O or parse problems;
    logs only the reason code."""
    goals, reason = None, NO_PATH
    if path is not None:
        name = os.fsdecode(os.fspath(path))
        if _in_cloud_folder(name):
            reason = ICLOUD_PATH
        else:
            data, reason, mtime = _read_bytes(name)
            if data is not None:
                try:
                    doc = _parse(data)
                except Exception:  # noqa: BLE001 - any malformed file reads as unparseable
                    doc, reason = None, UNPARSEABLE
                if reason == OK:
                    if isinstance(doc, dict):
                        goals = _goals(doc, mtime)
                    else:
                        reason = NOT_DICT
    logger.debug("reading goals: %s", reason)
    return goals, reason
