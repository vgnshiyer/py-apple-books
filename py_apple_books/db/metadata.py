"""Core Data store metadata (``Z_METADATA``).

Both Apple Books stores are Core Data SQLite stores. Their one-row
``Z_METADATA`` table holds the store UUID and a plist recording, among
other things, a version hash for every entity in the model the store
was written with. That identifies a store (library vs. annotations, this
library vs. a copy of another) and detects model changes after a macOS
or Books update.

Deliberately free of the rest of the package (no ``db.client``), so
store discovery, the connection layer and the writer can all use it.
"""

import base64
import os
import plistlib
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from typing import Mapping, Optional, Union
from urllib.parse import quote

#: Pause before the one retry of a metadata read that hit a lock.
BUSY_RETRY_DELAY = 0.1

_QUERY = "SELECT Z_UUID, Z_PLIST FROM Z_METADATA LIMIT 1"


def read_only_uri(path) -> str:
    """SQLite URI that opens ``path`` read-only.

    ``quote`` keeps the URI valid when the path contains characters
    special to URIs (space, ``#``, ``?``, ``%``).
    """
    return f"file:{quote(str(path))}?mode=ro"


@dataclass(frozen=True)
class StoreMetadata:
    """What Core Data records about a store in ``Z_METADATA``."""

    #: ``Z_UUID``: the store's identity, stable across backups.
    uuid: Optional[str]
    #: ``NSStoreModelVersionHashes``: entity name -> base64 hash.
    model_hashes: Mapping[str, str] = field(hash=False)
    #: ``NSPersistenceFrameworkVersion``, e.g. ``'1526'``.
    framework_version: Optional[str]

    @property
    def entities(self) -> frozenset:
        """Names of the Core Data entities in the store's model."""
        return frozenset(self.model_hashes)


def read_store_metadata(
    source: Union[sqlite3.Connection, "os.PathLike[str]", str],
    *,
    busy_timeout: float = 2.0,
) -> Optional[StoreMetadata]:
    """Read a store's Core Data metadata.

    :param source: an open connection (used as is and left open), or
        the path of a store file (opened read-only and closed again).
    :param busy_timeout: seconds to wait for a lock when opening a
        path. A read that still fails with 'database is locked' / busy
        is retried once after :data:`BUSY_RETRY_DELAY`.
    :returns: the metadata, or ``None`` when there is none to read: no
        such regular file, not an SQLite database, no ``Z_METADATA``
        table or row, an unparseable plist or one without model hashes,
        any other SQLite error, or still locked after the retry.
    :raises PermissionError: if the OS refuses to open the file (e.g.
        macOS privacy protection), so callers can report access denied
        rather than "not a store".
    """
    try:
        if isinstance(source, sqlite3.Connection):
            row = _fetch_row(source)
        elif not _is_regular_file(source):
            return None
        else:
            conn = sqlite3.connect(
                read_only_uri(source), uri=True, timeout=busy_timeout
            )
            try:
                row = _fetch_row(conn)
            finally:
                conn.close()
    except sqlite3.Error as e:
        opening_path = not isinstance(source, sqlite3.Connection)
        if opening_path and "unable to open" in str(e):
            _raise_if_access_denied(source)
        return None
    return _parse(row)


def _fetch_row(conn: sqlite3.Connection):
    try:
        return conn.execute(_QUERY).fetchone()
    except sqlite3.OperationalError as e:
        message = str(e).lower()
        if "locked" not in message and "busy" not in message:
            raise
    time.sleep(BUSY_RETRY_DELAY)
    return conn.execute(_QUERY).fetchone()


def _is_regular_file(path) -> bool:
    # SQLite would block forever opening a FIFO. stat() rather than
    # open(): closing any descriptor of a file drops every POSIX lock
    # this process holds on it, including other SQLite connections'.
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except PermissionError:
        raise
    except (OSError, TypeError, ValueError):
        return False


def _raise_if_access_denied(path) -> None:
    # SQLite reports a refused open as a generic "unable to open
    # database file"; the OS error says why. Only called after SQLite
    # failed to open the file, so no connection holds locks through it.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except PermissionError:
        raise
    except (OSError, TypeError, ValueError):
        return
    os.close(fd)


def _parse(row) -> Optional[StoreMetadata]:
    if row is None:
        return None
    store_uuid, blob = row
    try:
        plist = plistlib.loads(blob)
    except Exception:
        # InvalidFileException (a ValueError), ExpatError, TypeError, ...
        return None
    if not isinstance(plist, dict):
        return None
    raw = plist.get("NSStoreModelVersionHashes")
    if not isinstance(raw, dict):
        return None
    hashes = {
        str(entity): base64.b64encode(value).decode("ascii")
        for entity, value in raw.items()
        if isinstance(value, (bytes, bytearray))
    }
    version = plist.get("NSPersistenceFrameworkVersion")
    return StoreMetadata(
        uuid=store_uuid if isinstance(store_uuid, str) else None,
        model_hashes=hashes,
        framework_version=str(version) if version is not None else None,
    )
