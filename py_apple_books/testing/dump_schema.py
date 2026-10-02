"""Dump a privacy-safe schema fixture from an Apple Books library.

usage: python -m py_apple_books.testing.dump_schema --out DIR [--data-dir DIR] [--compare] [--census]

Writes ``DIR/macos-<version>-<build>_books-<version>-<build>/`` holding
``BKLibrary.sql``, ``AEAnnotation.sql`` and ``meta.json``: the stores'
DDL, ``Z_PRIMARYKEY`` with ``Z_MAX`` reset to 0, ``Z_METADATA`` with a
fresh ``Z_UUID`` and only schema-level plist keys, and an empty
``Z_MODELCACHE``. No library rows, titles, paths or account ids are
written, and the output is checked for that before it is saved.

When Books keeps per-book info caches next to the library
(``<container>/Data/Library/Caches/AEEpubInfoSource/AEBookInfo-*.sqlite``,
found from ``--data-dir`` when it is a ``Documents`` folder), the dump
also writes ``AEBookInfo.sql``: the ``CREATE TABLE``/``CREATE INDEX``
statements of the newest cache's ``ZAEBOOKINFO`` table and nothing
else, and ``meta.json`` gains ``"book_info": {"file", "columns"}``. The
cache is opened by its header, without creating or changing a file
beside it: rollback journal or WAL with both sidecars read-only, WAL
without them read-only and immutable. A missing or unreadable cache is
reported on stderr and skipped; the stores are dumped regardless.

``--compare`` prints column and entity-hash differences against the
nearest committed fixture. ``--census`` prints (and never saves) a
count-only table of book rows by data source, redownload flag, content
type and state, which shows how the 1.10 owned-books rule treats a
library. The stores are opened read-only and read inside one short
transaction.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import plistlib
import re
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections import Counter
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from .fixture import DOCUMENTS, SCHEMAS_DIR, STORE_SERIES, _version_key, available_schemas

STORES = ("BKLibrary", "AEAnnotation")

# Z_PLIST keys that describe the schema; everything else is dropped.
PLIST_KEYS = {
    "NSStoreType",
    "NSPersistenceFrameworkVersion",
    "NSPersistenceMaximumFrameworkVersion",
    "NSStoreModelVersionHashes",
    "NSStoreModelVersionHashesVersion",
    "NSStoreModelVersionHashesDigest",
    "NSStoreModelVersionChecksumKey",
    "NSStoreModelVersionIdentifiers",
    "_NSAutoVacuumLevel",
    "BKDatabase-Metadata",
}
# Keys kept inside BKDatabase-Metadata (migration and bootstrap flags are dropped).
BK_META_KEYS = {"BKLibraryVersion_Key", "Annotations-Update-Version"}

# Tables that may hold rows in a fixture.
BOOKKEEPING = ("Z_PRIMARYKEY", "Z_METADATA")

CENSUS_COLUMNS = ("ZDATASOURCEIDENTIFIER", "ZCANREDOWNLOAD", "ZCONTENTTYPE", "ZSTATE")
CENSUS_HEADERS = ("data_source", "can_redownload", "content_type", "state")
_RULE_COLUMNS = {"ZDATASOURCEIDENTIFIER", "ZCANREDOWNLOAD", "ZCONTENTTYPE"}
_APPLE_IDENTIFIER = re.compile(r"^com\.apple\.[A-Za-z0-9._-]+$")

# Books' per-book info caches (titles of books no longer in the library).
# Only the schema of their one entity table is dumped.
BOOK_INFO = "AEBookInfo"
BOOK_INFO_TABLE = "ZAEBOOKINFO"
# Relative to the container's Data folder, the parent of Documents.
BOOK_INFO_DIR = pathlib.PurePosixPath("Library/Caches/AEEpubInfoSource")
BOOK_INFO_NAME = re.compile(r"AEBookInfo-[A-Za-z0-9._-]{1,100}\.sqlite")
# How a cache file is opened, chosen from its header and sidecars.
OPEN_JOURNAL = "journal"              # rollback journal: mode=ro
OPEN_WAL = "wal"                      # WAL, -wal and -shm present: mode=ro
OPEN_WAL_IMMUTABLE = "wal_immutable"  # WAL without them: mode=ro&immutable=1
_SQLITE_MAGIC = b"SQLite format 3\x00"
_HEADER_BYTES = 100
_BUSY_TIMEOUT = 0.25
# Hard-coded: stat.SF_DATALESS is missing before Python 3.13.
_SF_DATALESS = 0x40000000
_UF_COMPRESSED = 0x20
# A cache folder that resolves into one of these is never read.
_CLOUD_PARTS = ("Mobile Documents", "com~apple~CloudDocs", "CloudStorage")


class DumpError(Exception):
    """A store can't be read, or the dump failed its privacy self-check."""


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _read_only(path: pathlib.Path) -> sqlite3.Connection:
    # mode=ro, never immutable=1: Books may be writing, and immutable
    # would skip locking and could read a torn page.
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, isolation_level=None)


def default_data_dir() -> pathlib.Path:
    env = os.environ.get("APPLE_BOOKS_DATA_DIR")
    return pathlib.Path(env) if env else pathlib.Path.home() / DOCUMENTS


def find_store(data_dir: pathlib.Path, store: str) -> pathlib.Path:
    """The store file Books uses: its canonical name if present, else the only
    or newest ``*.sqlite`` in ``data_dir/store``."""
    folder = pathlib.Path(data_dir) / store
    canonical = {
        json.loads((SCHEMAS_DIR / s / "meta.json").read_text(encoding="utf-8"))["stores"][store]["file"]
        for s in available_schemas()
    }
    for name in sorted(canonical):
        if (folder / name).is_file():
            return folder / name
    candidates = sorted(folder.glob("*.sqlite")) if folder.is_dir() else []
    if not candidates:
        raise DumpError(f"No {store} store (*.sqlite) in {folder}. Open Books once, or pass --data-dir.")
    if len(candidates) > 1:
        print(f"note: several {store} stores; using the newest, {max(candidates, key=_mtime).name}",
              file=sys.stderr)
    return max(candidates, key=_mtime)


def _mtime(path: pathlib.Path) -> float:
    return path.stat().st_mtime


def system_versions() -> Dict[str, str]:
    """macOS version and build, and the Books app version and build."""
    versions = {"macos": platform.mac_ver()[0] or "unknown", "macos_build": "unknown",
                "books": "unknown", "books_build": "unknown"}
    if sys.platform == "darwin":
        try:
            build = subprocess.run(["sw_vers", "-buildVersion"], capture_output=True,
                                   text=True, timeout=10).stdout.strip()
            versions["macos_build"] = build or "unknown"
        except (OSError, subprocess.SubprocessError):
            pass
        for app in ("/System/Applications/Books.app", "/Applications/Books.app"):
            try:
                info = plistlib.loads((pathlib.Path(app) / "Contents/Info.plist").read_bytes())
            except (OSError, plistlib.InvalidFileException):
                continue
            versions["books"] = str(info.get("CFBundleShortVersionString", "unknown"))
            versions["books_build"] = str(info.get("CFBundleVersion", "unknown"))
            break
    return {k: re.sub(r"[^A-Za-z0-9.]", "", v) or "unknown" for k, v in versions.items()}


def fixture_name(versions: Dict[str, str]) -> str:
    return (f"macos-{versions['macos']}-{versions['macos_build']}"
            f"_books-{versions['books']}-{versions['books_build']}")


# -- dump -------------------------------------------------------------------

def _clean_plist(blob: bytes) -> dict:
    meta = {k: v for k, v in plistlib.loads(blob).items() if k in PLIST_KEYS}
    if isinstance(meta.get("BKDatabase-Metadata"), dict):
        meta["BKDatabase-Metadata"] = {
            k: v for k, v in meta["BKDatabase-Metadata"].items() if k in BK_META_KEYS}
    return meta


def dump_store(path: pathlib.Path) -> Tuple[str, dict]:
    """Return ``(sql, meta)`` for one store: schema-only SQL and its meta.json entry."""
    con = _read_only(path)
    try:
        con.execute("BEGIN")  # one consistent snapshot, released right after
        objects = con.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
        entities = con.execute("SELECT Z_ENT, Z_NAME, Z_SUPER FROM Z_PRIMARYKEY ORDER BY Z_ENT").fetchall()
        version, _, plist = con.execute("SELECT Z_VERSION, Z_UUID, Z_PLIST FROM Z_METADATA").fetchone()
        columns = {name: len(con.execute(f"PRAGMA table_info({_ident(name)})").fetchall())
                   for kind, name, _ in objects if kind == "table"}
        con.execute("COMMIT")
    finally:
        con.close()

    rank = {"table": 0, "index": 1}
    objects.sort(key=lambda o: (rank.get(o[0], 2), o[1]))
    meta = _clean_plist(plist)
    lines = [
        "-- Schema-only Apple Books store fixture. Generated by py_apple_books.testing.dump_schema.",
        "-- Contains no library rows. Z_MAX reset to 0; Z_UUID regenerated; Z_MODELCACHE left empty.",
    ]
    lines += [sql.strip() + ";" for _, _, sql in objects]
    lines += [f"INSERT INTO Z_PRIMARYKEY (Z_ENT, Z_NAME, Z_SUPER, Z_MAX) "
              f"VALUES ({int(ent)}, {_sql_string(name)}, {int(sup)}, 0);" for ent, name, sup in entities]
    blob = plistlib.dumps(meta, fmt=plistlib.FMT_BINARY, sort_keys=True).hex().upper()
    lines.append(f"INSERT INTO Z_METADATA (Z_VERSION, Z_UUID, Z_PLIST) "
                 f"VALUES ({int(version)}, {_sql_string(str(uuid.uuid4()).upper())}, X'{blob}');")
    info = {
        "file": path.name,
        "entities": {k: v.hex() for k, v in sorted(meta.get("NSStoreModelVersionHashes", {}).items())},
        "framework_version": meta.get("NSPersistenceFrameworkVersion"),
        "tables": dict(sorted(columns.items())),
    }
    return "\n".join(lines) + "\n", info


def self_check(sql: str, meta_text: str = "") -> None:
    """Raise :class:`DumpError` unless ``sql`` is schema-only and path-free."""
    for text in (sql, meta_text):
        if "/Users/" in text:
            raise DumpError("self-check: output contains a /Users/ path")
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(sql)
        tables = [r[0] for r in mem.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        filled = {t: mem.execute(f"SELECT count(*) FROM {_ident(t)}").fetchone()[0]
                  for t in tables if t not in BOOKKEEPING}
        if any(filled.values()):
            raise DumpError(f"self-check: rows outside bookkeeping tables: {filled}")
        if "Z_PRIMARYKEY" in tables and any(r[0] != 0 for r in mem.execute("SELECT Z_MAX FROM Z_PRIMARYKEY")):
            raise DumpError("self-check: Z_MAX is not 0")
        if "Z_METADATA" in tables:
            for (blob,) in mem.execute("SELECT Z_PLIST FROM Z_METADATA"):
                if blob is not None and "/Users/" in repr(plistlib.loads(blob)):
                    raise DumpError("self-check: Z_PLIST contains a /Users/ path")
    finally:
        mem.close()


def table_columns(sql: str) -> Dict[str, Dict[str, str]]:
    """``{table: {column: declared type}}`` of a schema script."""
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(sql)
        tables = [r[0] for r in mem.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {t: {r[1]: r[2] for r in mem.execute(f"PRAGMA table_info({_ident(t)})")} for t in tables}
    finally:
        mem.close()


# -- book info caches -------------------------------------------------------

def _lstat(path) -> Optional[os.stat_result]:
    """``os.lstat``, or None when nothing is there. Patched by the tests."""
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_dataless(st) -> bool:
    """Evicted to iCloud: SF_DATALESS, or for a regular file a size with no
    blocks unless compressed (directories always report zero blocks)."""
    flags = getattr(st, "st_flags", 0)
    if flags & _SF_DATALESS:
        return True
    return (stat.S_ISREG(st.st_mode) and st.st_size > 0 and getattr(st, "st_blocks", 1) == 0
            and not flags & _UF_COMPRESSED)


def _local_regular(st) -> bool:
    return st is not None and stat.S_ISREG(st.st_mode) and not _is_dataless(st)


def _os_error(e: OSError) -> str:
    return f"[Errno {e.errno}] {e.strerror}"  # never the path


def book_info_dir(data_dir) -> Optional[pathlib.Path]:
    """Books' AEBookInfo cache folder beside ``data_dir`` (Books'
    ``Documents`` folder), or None when ``data_dir`` isn't named
    ``Documents``: only Books' own container layout is followed."""
    data_dir = pathlib.Path(data_dir)
    if data_dir.name != "Documents":
        return None
    return data_dir.parent / BOOK_INFO_DIR


def _natural_key(name: str) -> tuple:
    # "26.10" after "26.7". re.split with a group alternates text and digit
    # runs, so the parts compared at one index always have the same type.
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)), name


def find_book_info(data_dir) -> Optional[pathlib.Path]:
    """The newest AEBookInfo cache file for ``data_dir`` (natural order of
    the names, which carry Books' version), or None if there is none.

    Raises :class:`DumpError` when the folder isn't a local folder (a
    symlink, a file, evicted to iCloud) or resolves into iCloud Drive or
    a cloud-storage folder.
    """
    folder = book_info_dir(data_dir)
    if folder is None:
        return None
    st = _lstat(folder)
    if st is None:
        return None
    if not stat.S_ISDIR(st.st_mode) or _is_dataless(st):
        raise DumpError(f"the {BOOK_INFO} cache folder is not a local folder")
    if any(part in _CLOUD_PARTS for part in pathlib.Path(os.path.realpath(folder)).parts):
        raise DumpError(f"the {BOOK_INFO} cache folder is in iCloud Drive or cloud storage")
    try:
        names = [n for n in os.listdir(folder) if BOOK_INFO_NAME.fullmatch(n)]
    except OSError as e:
        raise DumpError(f"the {BOOK_INFO} cache folder can't be listed: {_os_error(e)}") from None
    return folder / max(names, key=_natural_key) if names else None


def book_info_open_mode(path) -> str:
    """How an AEBookInfo cache file is opened: one of ``OPEN_*``.

    Chosen from the file's 100-byte header (read with ``O_NOFOLLOW``) and
    its sidecars, so that reading never creates, changes or waits on a
    file Books owns beyond SQLite's own read locks:

    - rollback journal: ``mode=ro`` (``OPEN_JOURNAL``), which takes the
      shared lock and honours a hot journal;
    - WAL whose ``-wal`` and ``-shm`` are both local regular files:
      ``mode=ro`` (``OPEN_WAL``), which sees the committed WAL content;
    - WAL without them (closed cleanly, or one is missing):
      ``mode=ro&immutable=1`` (``OPEN_WAL_IMMUTABLE``), because a read-only
      open would fail or create the sidecars. The file is re-checked after
      the read.

    The same rule as py_apple_books' own cache reader; the probe cases in
    ``tests/_bookinfo_cases.py`` pin both. Raises :class:`DumpError` unless
    ``path`` is a local regular SQLite file.
    """
    path = pathlib.Path(path)
    st = _lstat(path)
    if not _local_regular(st) or st.st_size < _HEADER_BYTES:
        raise DumpError(f"{path.name} is not a local SQLite file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    head = b""
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(opened.st_mode):
                raise DumpError(f"{path.name} changed while being opened")
            while len(head) < _HEADER_BYTES:
                chunk = os.read(fd, _HEADER_BYTES - len(head))
                if not chunk:
                    break
                head += chunk
        finally:
            os.close(fd)
    except OSError as e:
        raise DumpError(f"{path.name} can't be read: {_os_error(e)}") from None
    if len(head) < _HEADER_BYTES or not head.startswith(_SQLITE_MAGIC):
        raise DumpError(f"{path.name} is not a SQLite file")
    # Bytes 18 and 19: file format write and read versions, 2 for WAL.
    if head[18] != 2 and head[19] != 2:
        return OPEN_JOURNAL
    sidecars = (_lstat(f"{path}-wal"), _lstat(f"{path}-shm"))
    return OPEN_WAL if all(_local_regular(s) for s in sidecars) else OPEN_WAL_IMMUTABLE


def _signature(path: pathlib.Path) -> tuple:
    out = []
    for side in ("", "-wal", "-shm"):
        st = _lstat(f"{path}{side}")
        out.append(None if st is None else
                   (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, stat.S_IFMT(st.st_mode)))
    return tuple(out)


def dump_book_info(path) -> Tuple[str, dict]:
    """Return ``(sql, meta)`` for one AEBookInfo cache: the ``CREATE TABLE``
    and ``CREATE INDEX`` statements of ``ZAEBOOKINFO`` and meta.json's
    ``book_info`` entry (bare file name, column count). Reads no row."""
    path = pathlib.Path(path)
    mode = book_info_open_mode(path)
    before = _signature(path)
    extra = "&immutable=1" if mode == OPEN_WAL_IMMUTABLE else ""
    try:
        con = sqlite3.connect(f"file:{quote(str(path))}?mode=ro{extra}", uri=True,
                              isolation_level=None, timeout=_BUSY_TIMEOUT)
    except sqlite3.Error as e:
        raise DumpError(f"{path.name} can't be read: {e}") from None
    try:
        con.execute("BEGIN")  # one consistent snapshot, released right after
        objects = con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'index') "
            "AND tbl_name = ? COLLATE NOCASE AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'",
            (BOOK_INFO_TABLE,)).fetchall()
        columns = len(con.execute(f"PRAGMA table_info({_ident(BOOK_INFO_TABLE)})").fetchall())
        con.execute("COMMIT")
    except sqlite3.Error as e:
        raise DumpError(f"{path.name} can't be read: {e}") from None
    finally:
        con.close()
    if mode == OPEN_WAL_IMMUTABLE and _signature(path) != before:
        raise DumpError(f"{path.name} changed while being read; try again")
    if not any(kind == "table" for kind, _, _ in objects):
        raise DumpError(f"{path.name} has no {BOOK_INFO_TABLE} table")
    objects.sort(key=lambda o: (o[0] != "table", o[1]))
    lines = [
        f"-- Schema-only Apple Books {BOOK_INFO} cache fixture. Generated by py_apple_books.testing.dump_schema.",
        f"-- {BOOK_INFO_TABLE} and its indexes only. Contains no rows.",
    ]
    lines += [sql.strip() + ";" for _, _, sql in objects]
    return "\n".join(lines) + "\n", {"file": path.name, "columns": columns}


def self_check_book_info(sql: str) -> None:
    """Raise :class:`DumpError` unless ``sql`` only creates ``ZAEBOOKINFO``
    and its indexes, holds no row and is path-free."""
    if "/Users/" in sql:
        raise DumpError(f"self-check: {BOOK_INFO}.sql contains a /Users/ path")
    mem = sqlite3.connect(":memory:")
    try:
        try:
            mem.executescript(sql)
        except sqlite3.Error as e:
            raise DumpError(f"self-check: {BOOK_INFO}.sql does not run: {e}") from None
        objects = mem.execute("SELECT type, name, tbl_name FROM sqlite_master").fetchall()
        tables = [name for kind, name, _ in objects if kind == "table"]
        if len(tables) != 1 or tables[0].upper() != BOOK_INFO_TABLE or any(
                kind not in ("table", "index") or tbl.upper() != BOOK_INFO_TABLE for kind, _, tbl in objects):
            raise DumpError(f"self-check: {BOOK_INFO}.sql creates more than {BOOK_INFO_TABLE} and its indexes")
        if mem.execute(f"SELECT count(*) FROM {_ident(tables[0])}").fetchone()[0]:
            raise DumpError(f"self-check: {BOOK_INFO}.sql holds rows")
    finally:
        mem.close()


def _book_info_or_none(data_dir: pathlib.Path) -> Optional[Tuple[str, dict]]:
    """The newest cache's dump, self-checked, or None with a note on stderr."""
    try:
        path = find_book_info(data_dir)
        if path is None:
            print(f"note: no {BOOK_INFO} cache found; {BOOK_INFO}.sql not written", file=sys.stderr)
            return None
        sql, info = dump_book_info(path)
        self_check_book_info(sql)
    except DumpError as e:
        print(f"note: {BOOK_INFO}.sql not written: {e}", file=sys.stderr)
        return None
    except OSError as e:  # an lstat on the way failed: not a missing file
        print(f"note: {BOOK_INFO}.sql not written: {_os_error(e)}", file=sys.stderr)
        return None
    return sql, info


# -- compare ----------------------------------------------------------------

def nearest_schema(name: str) -> str:
    """The committed fixture ``name`` is compared with: itself if committed,
    else the newest one not newer than it, else the oldest."""
    schemas = available_schemas()
    if name in schemas:
        return name
    older = [s for s in schemas if _version_key(s) <= _version_key(name)]
    return older[-1] if older else schemas[0]


def _column_diffs(old: Dict[str, Dict[str, str]], new: Dict[str, Dict[str, str]]) -> List[str]:
    diffs = []
    for table in sorted(old.keys() - new.keys()):
        diffs.append(f"table removed: {table}")
    for table in sorted(new.keys() - old.keys()):
        diffs.append(f"table added: {table} ({len(new[table])} columns)")
    for table in sorted(old.keys() & new.keys()):
        for col in sorted(old[table].keys() - new[table].keys()):
            diffs.append(f"{table}: column removed: {col}")
        for col in sorted(new[table].keys() - old[table].keys()):
            diffs.append(f"{table}: column added: {col} {new[table][col]}")
        for col in sorted(old[table].keys() & new[table].keys()):
            if old[table][col] != new[table][col]:
                diffs.append(f"{table}: column {col} retyped {old[table][col]} -> {new[table][col]}")
    return diffs


def compare(dumped: Dict[str, Tuple[str, dict]], against: str,
            book_info: Optional[Tuple[str, dict]] = None) -> List[str]:
    """Column and entity-hash differences between a dump and a committed
    fixture; ``book_info`` (a :func:`dump_book_info` result) adds the
    AEBookInfo columns when the fixture has an ``AEBookInfo.sql``."""
    ref_meta = json.loads((SCHEMAS_DIR / against / "meta.json").read_text(encoding="utf-8"))
    lines = [f"Comparing with committed fixture {against}:"]
    column_diffs = hash_diffs = 0
    for store in STORES:
        sql, info = dumped[store]
        new = table_columns(sql)
        old = table_columns((SCHEMAS_DIR / against / f"{store}.sql").read_text(encoding="utf-8"))
        diffs = _column_diffs(old, new)
        old_hashes = ref_meta["stores"][store]["entities"]
        new_hashes = info["entities"]
        hashes = []
        for entity in sorted(old_hashes.keys() | new_hashes.keys()):
            before, after = old_hashes.get(entity), new_hashes.get(entity)
            if before != after:
                hashes.append(f"entity {entity}: {(before or 'absent')[:16]} -> {(after or 'absent')[:16]}")
        column_diffs += len(diffs)
        hash_diffs += len(hashes)
        if not diffs and not hashes:
            lines.append(f"  {store}: 0 column diffs; entity hashes identical")
        else:
            lines += [f"  {store}: {d}" for d in diffs + hashes]
    ref_book_info = SCHEMAS_DIR / against / f"{BOOK_INFO}.sql"
    if book_info is not None and ref_book_info.is_file():
        diffs = _column_diffs(table_columns(ref_book_info.read_text(encoding="utf-8")),
                              table_columns(book_info[0]))
        column_diffs += len(diffs)
        lines += [f"  {BOOK_INFO}: {d}" for d in diffs] or [f"  {BOOK_INFO}: 0 column diffs"]
    elif book_info is not None:
        lines.append(f"  {BOOK_INFO}: not in the committed fixture; not compared")
    elif ref_book_info.is_file():
        lines.append(f"  {BOOK_INFO}: no cache dumped; not compared")
    lines.append(f"compare: {column_diffs} column diffs, {hash_diffs} entity-hash diffs")
    return lines


# -- census -----------------------------------------------------------------

def _census_label(column: str, value) -> str:
    # Only Apple's own constants and integers are ever printed.
    if column == "ZDATASOURCEIDENTIFIER":
        return value if isinstance(value, str) and _APPLE_IDENTIFIER.match(value) else "<other>"
    if value is None:
        return "NULL"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return "<other>"


def _census_sort_key(labels: Tuple[str, ...]) -> tuple:
    return tuple((0, int(v), "") if re.fullmatch(r"-?\d+", v) else (1, 0, v) for v in labels)


def census(data_dir: pathlib.Path) -> List[str]:
    """Count-only census of ``ZBKLIBRARYASSET``; see the module docstring."""
    path = find_store(data_dir, "BKLibrary")
    con = _read_only(path)
    try:
        con.execute("BEGIN")
        have = {r[1] for r in con.execute("PRAGMA table_info(ZBKLIBRARYASSET)")}
        if not have:
            raise DumpError("ZBKLIBRARYASSET not found in the BKLibrary store")
        present = [c for c in CENSUS_COLUMNS if c in have]
        total = con.execute("SELECT count(*) FROM ZBKLIBRARYASSET").fetchone()[0]
        rows = con.execute(f"SELECT {', '.join(present)} FROM ZBKLIBRARYASSET").fetchall() if present else []
        rules = None
        if _RULE_COLUMNS <= have:
            hidden = con.execute(
                "SELECT count(*) FROM ZBKLIBRARYASSET WHERE ZCONTENTTYPE = 5 "
                "OR (ZDATASOURCEIDENTIFIER = ? AND ZCANREDOWNLOAD IS NOT 1)", (STORE_SERIES,)).fetchone()[0]
            kept = con.execute(
                "SELECT count(*) FROM ZBKLIBRARYASSET WHERE ZDATASOURCEIDENTIFIER = ? "
                "AND ZCANREDOWNLOAD = 1 AND ZCONTENTTYPE IS NOT 5", (STORE_SERIES,)).fetchone()[0]
            rules = (hidden, kept)
        con.execute("COMMIT")
    finally:
        con.close()

    counts = Counter()
    for row in rows:
        values = dict(zip(present, row))
        counts[tuple(_census_label(c, values[c]) if c in values else "-" for c in CENSUS_COLUMNS)] += 1
    table = [CENSUS_HEADERS + ("rows",)]
    table += [labels + (str(n),) for labels, n in sorted(counts.items(), key=lambda kv: _census_sort_key(kv[0]))]
    widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
    lines = [f"Census of ZBKLIBRARYASSET: {total} rows (counts only; no titles, ids or paths)"]
    lines += ["  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in table]
    for column in CENSUS_COLUMNS:
        if column not in have:
            lines.append(f"column {column} is missing")
    if rules is None:
        lines.append("rule lines skipped: the 1.10 owned-books rule needs "
                     + ", ".join(sorted(_RULE_COLUMNS)))
    else:
        lines.append(f"hidden_by_1.10_rule = {rules[0]}")
        lines.append(f"series_rows_kept_because_redownloadable = {rules[1]}")
    return lines


# -- command line -----------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m py_apple_books.testing.dump_schema",
        description="Dump a schema-only, privacy-safe fixture of the Apple Books stores.")
    parser.add_argument("--out", type=pathlib.Path,
                        help="directory to write <macos>_<books>/{BKLibrary.sql,AEAnnotation.sql,meta.json} "
                             "(and AEBookInfo.sql when Books has an info cache) into")
    parser.add_argument("--data-dir", type=pathlib.Path, default=None,
                        help="Books' Documents directory (default: $APPLE_BOOKS_DATA_DIR or "
                             "~/Library/Containers/com.apple.iBooksX/Data/Documents)")
    parser.add_argument("--compare", action="store_true",
                        help="print column and entity-hash differences against the nearest committed fixture")
    parser.add_argument("--census", action="store_true",
                        help="print a count-only census of book rows (never written to --out)")
    args = parser.parse_args(argv)
    if args.out is None and (args.compare or not args.census):
        parser.error("--out is required to dump or --compare (only --census runs without it)")
    data_dir = args.data_dir or default_data_dir()

    try:
        if args.out is not None:
            versions = system_versions()
            name = fixture_name(versions)
            dumped = {store: dump_store(find_store(data_dir, store)) for store in STORES}
            book_info = _book_info_or_none(data_dir)
            meta = {**versions, "stores": {store: info for store, (_, info) in dumped.items()}}
            if book_info is not None:
                meta["book_info"] = book_info[1]
            meta_text = json.dumps(meta, indent=2, sort_keys=True) + "\n"
            for store, (sql, _) in dumped.items():
                self_check(sql, meta_text)
            out = args.out / name
            out.mkdir(parents=True, exist_ok=True)
            # Progress goes to stderr; stdout carries only the reports.
            for store, (sql, info) in dumped.items():
                (out / f"{store}.sql").write_text(sql, encoding="utf-8")
                print(f"{store}.sql: {len(sql.encode())} bytes, {len(info['tables'])} tables",
                      file=sys.stderr)
            if book_info is not None:
                (out / f"{BOOK_INFO}.sql").write_text(book_info[0], encoding="utf-8")
                print(f"{BOOK_INFO}.sql: {len(book_info[0].encode())} bytes, "
                      f"{book_info[1]['columns']} columns", file=sys.stderr)
            elif (out / f"{BOOK_INFO}.sql").exists():
                print(f"note: {name}/{BOOK_INFO}.sql is left from an earlier dump; meta.json no "
                      f"longer describes it", file=sys.stderr)
            (out / "meta.json").write_text(meta_text, encoding="utf-8")
            print(f"wrote {out}", file=sys.stderr)
            if args.compare:
                print("\n".join(compare(dumped, nearest_schema(name), book_info)))
        if args.census:
            print("\n".join(census(data_dir)))
    except DumpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(f"error: could not read the Books stores in {data_dir}: {e}. If this is a permissions "
              f"error, grant your terminal Full Disk Access.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
