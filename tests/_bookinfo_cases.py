"""Synthetic AEBookInfo caches in every state the header-based open rule
tells apart (stream 0.3), for every reader of these caches.

Books keeps per-book info caches (``AEBookInfo-<version>.sqlite``) in
``<container>/Data/Library/Caches/AEEpubInfoSource``. Two readers open
them by the same rule: ``py_apple_books.testing.dump_schema`` (stdlib
only, so it carries its own copy) and the library's removed-books
reader. Both test modules parametrize over :data:`CASES` and compare the
mode their rule picks with :attr:`Case.mode`, which pins the two copies
to one rule:

- rollback journal (DELETE, PERSIST, TRUNCATE; locked by another
  process or with a hot journal too): ``journal``, opened ``mode=ro``;
- rollback journal whose ``-journal`` is not a local regular file (a
  symlink): refused (:data:`REFUSED`), never opened, because SQLite
  would read it;
- WAL with a local regular ``-wal`` and ``-shm``, whether a connection
  has the cache open or not: ``wal``, ``mode=ro``. The read writes
  ``-shm`` in place (SQLite's read marks; a rebuild of the index when no
  connection has the cache open, after a crash or in a copy); every other
  file stays as it was;
- WAL without both (closed cleanly, one missing, one a symlink):
  ``wal_immutable``, ``mode=ro&immutable=1``.

The caches are built from the committed ``AEBookInfo.sql`` and hold
synthetic rows only: every text column reads ``SECRET-<column>-<n>``, so
a test can check that no row reached its output.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, ContextManager, Iterator, Optional

from py_apple_books.testing.fixture import DEFAULT_SCHEMA, SCHEMAS_DIR

CACHE_NAME = "AEBookInfo-v20250715-26.7.sqlite"
TABLE = "ZAEBOOKINFO"
JOURNAL, WAL, WAL_IMMUTABLE = "journal", "wal", "wal_immutable"
REFUSED = None  # the rule refuses the file before SQLite opens it
SECRET = "SECRET-"


def ddl(schema: str = DEFAULT_SCHEMA) -> str:
    """The committed AEBookInfo DDL of ``schema``."""
    return (SCHEMAS_DIR / schema / "AEBookInfo.sql").read_text(encoding="utf-8")


def fill(con: sqlite3.Connection, rows: int = 2) -> None:
    """Insert ``rows`` synthetic rows: text columns ``SECRET-<column>-<n>``."""
    columns = [(r[1], (r[2] or "").upper()) for r in con.execute(f"PRAGMA table_info({TABLE})")
               if r[1] != "Z_PK"]

    def value(name: str, kind: str, n: int):
        if "CHAR" in kind or "TEXT" in kind:
            return f"{SECRET}{name}-{n}"
        if "BLOB" in kind:
            return f"{SECRET}{name}-{n}".encode()
        return n

    names = ", ".join(name for name, _ in columns)
    marks = ", ".join("?" for _ in columns)
    con.execute("BEGIN")
    con.executemany(f"INSERT INTO {TABLE} ({names}) VALUES ({marks})",
                    [[value(name, kind, n) for name, kind in columns] for n in range(1, rows + 1)])
    con.execute("COMMIT")


def create(path: pathlib.Path, journal_mode: str = "DELETE", rows: int = 2) -> sqlite3.Connection:
    """A cache at ``path`` in ``journal_mode`` holding ``rows`` synthetic
    rows; returns its open connection (the caller closes it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None)
    con.execute(f"PRAGMA journal_mode={journal_mode}")
    con.executescript(ddl())
    fill(con, rows)
    return con


def sidecars(path: pathlib.Path) -> list:
    return sorted(p.name for p in path.parent.iterdir() if p.name.startswith(path.name + "-"))


# -- builders: each yields the cache path and undoes its locks on exit ------

@contextlib.contextmanager
def _closed(folder: pathlib.Path, journal_mode: str) -> Iterator[pathlib.Path]:
    path = folder / CACHE_NAME
    create(path, journal_mode).close()
    yield path


@contextlib.contextmanager
def _persist(folder: pathlib.Path) -> Iterator[pathlib.Path]:
    path = folder / CACHE_NAME
    con = create(path, "PERSIST")
    fill(con, 1)  # a write leaves the (zeroed) -journal behind
    con.close()
    assert sidecars(path) == [f"{CACHE_NAME}-journal"]
    yield path


_HOLD_LOCK = """
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], isolation_level=None)
con.execute("BEGIN EXCLUSIVE")
print("locked", flush=True)
sys.stdin.read()
con.execute("ROLLBACK")
con.close()
"""


@contextlib.contextmanager
def _locked(folder: pathlib.Path) -> Iterator[pathlib.Path]:
    # The lock is held by another process, as Books holds it. A lock held
    # by a connection in this process would not model Books: POSIX locks
    # belong to the process, and the open rule's own os.open/os.close of
    # the file would drop it.
    path = folder / CACHE_NAME
    create(path).close()
    holder = subprocess.Popen([sys.executable, "-I", "-c", _HOLD_LOCK, str(path)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        yield path
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
        holder.stdout.close()


@contextlib.contextmanager
def _journal_symlinked(folder: pathlib.Path) -> Iterator[pathlib.Path]:
    path = folder / CACHE_NAME
    create(path, "PERSIST").close()
    assert sidecars(path) == [f"{CACHE_NAME}-journal"]
    elsewhere = folder / "elsewhere"
    elsewhere.mkdir()
    os.replace(f"{path}-journal", elsewhere / "journal")
    os.symlink(elsewhere / "journal", f"{path}-journal")
    yield path


@contextlib.contextmanager
def _hot_journal(folder: pathlib.Path) -> Iterator[pathlib.Path]:
    # A copy of a cache and its journal taken while a writer had spilled
    # pages into the file: the journal is hot and needs a rollback.
    work = folder / "work"
    work.mkdir()
    source = work / CACHE_NAME
    create(source, rows=20).close()
    writer = sqlite3.connect(source, isolation_level=None)
    text = next(r[1] for r in writer.execute(f"PRAGMA table_info({TABLE})") if "CHAR" in (r[2] or "").upper())
    writer.execute("PRAGMA cache_size=1")
    writer.execute("BEGIN")
    writer.execute(f"UPDATE {TABLE} SET {text} = {text} || hex(randomblob(800))")
    assert os.path.exists(f"{source}-journal")
    path = folder / CACHE_NAME
    shutil.copyfile(source, path)
    shutil.copyfile(f"{source}-journal", f"{path}-journal")
    writer.execute("ROLLBACK")
    writer.close()
    shutil.rmtree(work)
    yield path


@contextlib.contextmanager
def _wal_live(folder: pathlib.Path) -> Iterator[pathlib.Path]:
    path = folder / CACHE_NAME
    con = create(path, "WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    fill(con, 1)  # committed, in the -wal only
    assert sidecars(path) == [f"{CACHE_NAME}-shm", f"{CACHE_NAME}-wal"]
    try:
        yield path
    finally:
        con.close()


@contextlib.contextmanager
def _wal_dormant(folder: pathlib.Path) -> Iterator[pathlib.Path]:
    # Both sidecars and no connection: what a crash leaves, or a copy of
    # a cache Books has open. The committed rows are in the -wal only.
    work = folder / "work"
    work.mkdir()
    source = work / CACHE_NAME
    con = create(source, "WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    fill(con, 1)
    path = folder / CACHE_NAME
    for side in ("", "-wal", "-shm"):
        shutil.copyfile(f"{source}{side}", f"{path}{side}")
    con.close()
    shutil.rmtree(work)
    assert sidecars(path) == [f"{CACHE_NAME}-shm", f"{CACHE_NAME}-wal"]
    yield path


@contextlib.contextmanager
def _wal_one_sidecar(folder: pathlib.Path, keep: str) -> Iterator[pathlib.Path]:
    path = folder / CACHE_NAME
    con = create(path, "WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    fill(con, 1)
    saved = folder / "saved"
    shutil.copyfile(f"{path}{keep}", saved)
    con.close()  # checkpoints and removes both sidecars
    os.replace(saved, f"{path}{keep}")
    assert sidecars(path) == [f"{CACHE_NAME}{keep}"]
    yield path


@contextlib.contextmanager
def _wal_symlinked_sidecar(folder: pathlib.Path) -> Iterator[pathlib.Path]:
    path = folder / CACHE_NAME
    con = create(path, "WAL")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the table is in the main file
    elsewhere = folder / "elsewhere"
    elsewhere.mkdir()
    os.replace(f"{path}-wal", elsewhere / "wal")
    os.symlink(elsewhere / "wal", f"{path}-wal")
    try:
        yield path
    finally:
        os.unlink(f"{path}-wal")
        os.replace(elsewhere / "wal", f"{path}-wal")
        con.close()


@dataclass(frozen=True)
class Case:
    """One cache state. ``mode`` is the open mode the rule must pick
    (:data:`REFUSED`: none, the file is refused); ``readable`` whether its
    schema can be read in that mode (None: the SQLite build decides, e.g.
    a hot journal on a read-only open)."""

    name: str
    mode: Optional[str]
    make: Callable[[pathlib.Path], ContextManager[pathlib.Path]]
    readable: Optional[bool] = True

    def __str__(self) -> str:
        return self.name


CASES = [
    Case("delete", JOURNAL, lambda d: _closed(d, "DELETE")),
    Case("truncate", JOURNAL, lambda d: _closed(d, "TRUNCATE")),
    Case("persist_journal", JOURNAL, _persist),
    Case("delete_locked", JOURNAL, _locked, readable=False),
    Case("hot_journal", JOURNAL, _hot_journal, readable=None),
    Case("journal_symlinked", REFUSED, _journal_symlinked, readable=False),
    Case("wal_live", WAL, _wal_live),
    Case("wal_dormant_sidecars", WAL, _wal_dormant),
    Case("wal_closed", WAL_IMMUTABLE, lambda d: _closed(d, "WAL")),
    Case("wal_without_shm", WAL_IMMUTABLE, lambda d: _wal_one_sidecar(d, "-wal")),
    Case("wal_without_wal", WAL_IMMUTABLE, lambda d: _wal_one_sidecar(d, "-shm")),
    Case("wal_symlinked_sidecar", WAL_IMMUTABLE, _wal_symlinked_sidecar),
]


def listing(folder: pathlib.Path) -> dict:
    """``{name: (size, mtime_ns)}`` of ``folder``, symlinks not followed."""
    out = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            st = entry.stat(follow_symlinks=False)
            out[entry.name] = (st.st_size, st.st_mtime_ns)
    return out


def contents(folder: pathlib.Path) -> dict:
    """``{name: sha256}`` of the regular files in ``folder`` (symlinks
    and directories skipped)."""
    out = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.is_file(follow_symlinks=False):
                out[entry.name] = hashlib.sha256(pathlib.Path(entry.path).read_bytes()).hexdigest()
    return out
