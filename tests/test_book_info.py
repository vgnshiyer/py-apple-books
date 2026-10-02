"""get_cached_book_info: titles and authors of removed books from Books'
AEBookInfo caches (stream 2.6).

Synthetic caches only: ``FixtureLibrary.add_book_info_cache`` (built from
the committed ``AEBookInfo.sql``) and the open-rule cases shared with
``dump_schema`` (``tests/_bookinfo_cases.py``). Most tests need no
library store: the caches are found from the library's ``Documents``
folder alone.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import logging
import os
import pathlib
import pickle
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import types
import warnings
from urllib.parse import unquote

import pytest

import py_apple_books
from py_apple_books import PyAppleBooks, book_info
from py_apple_books.book_info import BOOK_INFO_RECHECK, CachedBookInfo
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import InvalidArgumentError
from py_apple_books.testing import FixtureLibrary
from tests import _bookinfo_cases as cases
from tests import _fs_audit

V0, V7, V10 = "v20250715-26.0", "v20250715-26.7", "v20250715-26.10"


def cache_name(version: str) -> str:
    return f"AEBookInfo-{version}.sqlite"


@pytest.fixture
def home(tmp_path):
    """A Books container with no stores: the caches are all a lookup needs."""
    return FixtureLibrary(tmp_path / "home")


@pytest.fixture
def reader(home):
    """A ``PyAppleBooks`` whose library is ``home``'s (closed afterwards)."""
    api = PyAppleBooks(home.data_dir)
    yield api
    api.close()


@pytest.fixture
def three(home):
    """Caches of Books 26.0, 26.7 (rollback journal) and 26.10."""
    home.add_book_info_cache([
        {"asset_id": "A", "title": "A in 26.0", "author": "Author 0"},
        {"asset_id": "B", "title": "Only B", "author": "B Author", "language": "en",
         "publisher": "B Press", "year": "1999"},
        {"asset_id": "T", "title": "T titled in 26.0", "author": "Old T author"},
    ], version=V0)
    home.add_book_info_cache([
        {"asset_id": "A", "title": "A in 26.7"},
        {"asset_id": "C", "title": "C in 26.7"},
    ], version=V7, journal_mode="DELETE")
    home.add_book_info_cache([
        {"asset_id": "A", "title": "A in 26.10"},
        {"asset_id": "T", "author": "New T author"},  # no title: an older file's title wins
    ], version=V10)
    return home


class Spy:
    """Records the cache files ``book_info`` connects to and the
    statements it runs on them."""

    def __init__(self, monkeypatch):
        self.uris, self.statements, self.connections = [], [], []
        real_connect, real_execute = book_info._connect, book_info._execute

        def connect(uri, busy):
            self.uris.append(uri)
            con = real_connect(uri, busy)
            self.connections.append(con)
            return con

        def execute(con, sql, params=()):
            self.statements.append((sql, list(params)))
            return real_execute(con, sql, params)

        monkeypatch.setattr(book_info, "_connect", connect)
        monkeypatch.setattr(book_info, "_execute", execute)

    def files(self) -> list:
        return [unquote(uri[len("file:"):].split("?", 1)[0]).rsplit("/", 1)[1] for uri in self.uris]

    def selects(self) -> list:
        return [params for sql, params in self.statements if sql.startswith("SELECT")]

    def reset(self) -> None:
        self.uris.clear()
        self.statements.clear()


@pytest.fixture
def spy(monkeypatch):
    return Spy(monkeypatch)


def index_of(api: PyAppleBooks):
    """The book-info index the instance's library holds (None if none)."""
    return api._PyAppleBooks__library._derived.get("book_info")


def touch_folder_events(folder, run):
    """The audit events ``run()`` causes under ``folder``."""
    with _fs_audit.record() as rec:
        result = run()
    return result, rec.under(str(pathlib.Path(folder).resolve()))


# -- the result type --------------------------------------------------------------


def test_cached_book_info_type():
    info = CachedBookInfo("A", "Title", None)
    assert [f.name for f in dataclasses.fields(CachedBookInfo)] == [
        "asset_id", "title", "author", "language", "publisher", "year", "source"]
    assert (info.language, info.publisher, info.year, info.source) == (None, None, None, "")
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.title = "x"
    assert hash(info) == hash(CachedBookInfo("A", "Title", None))
    assert pickle.loads(pickle.dumps(info)) == info
    assert BOOK_INFO_RECHECK == 30.0
    assert book_info.__all__ == ["CachedBookInfo", "BOOK_INFO_RECHECK"]
    # R6: imported from its home module; the package's top level is unchanged.
    assert "CachedBookInfo" not in py_apple_books.__all__


# -- resolution and order ---------------------------------------------------------


def test_newest_cache_wins_in_natural_order(three, reader):
    got = reader.get_cached_book_info(["A", "B", "C", "T", "Z"])
    assert list(got) == ["A", "B", "C", "T"]
    # 26.10 is newer than 26.7 (natural order, not string order).
    assert got["A"] == CachedBookInfo("A", "A in 26.10", None, source=cache_name(V10))
    assert got["B"] == CachedBookInfo("B", "Only B", "B Author", "en", "B Press", "1999", cache_name(V0))
    assert got["C"].source == cache_name(V7)
    # The newest row has only an author: the older file's title wins, as a whole row.
    assert got["T"] == CachedBookInfo("T", "T titled in 26.0", "Old T author", source=cache_name(V0))


def test_author_only_everywhere_is_returned(home, reader):
    home.add_book_info_cache([{"asset_id": "U", "author": "Old author"}], version=V0)
    home.add_book_info_cache([{"asset_id": "U", "author": "New author"}], version=V7)
    assert reader.get_cached_book_info("U") == {
        "U": CachedBookInfo("U", None, "New author", source=cache_name(V7))}


def test_rows_within_one_file(home, reader):
    home.add_book_info_cache([
        {"asset_id": "K", "title": "older row"},
        {"asset_id": "K", "title": "newer row"},
        {"asset_id": "K", "author": "newest row, no title"},
        {"asset_id": "K"},  # nothing at all
        {"asset_id": "N", "author": "older author"},
        {"asset_id": "N", "author": "newer author"},
    ])
    got = reader.get_cached_book_info(["K", "N"])
    assert got["K"].title == "newer row" and got["K"].author is None
    assert got["N"].author == "newer author"


def test_deleted_flag_is_not_a_filter(home, reader):
    home.add_book_info_cache([{"asset_id": "R", "title": "Removed", "deleted": 1}])
    assert reader.get_cached_book_info("R")["R"].title == "Removed"


# -- mapping ------------------------------------------------------------------------


def _custom_cache(folder: pathlib.Path, name: str, ddl: str, rows: list) -> pathlib.Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    con = sqlite3.connect(path)
    try:
        con.executescript(ddl)
        con.executemany("INSERT INTO ZAEBOOKINFO VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        con.commit()
    finally:
        con.close()
    return path


def test_values_are_text_or_none(home, reader):
    # Numbers in a column without text affinity read as text; integral
    # ones as digits. Blobs and empty or blank text read as None.
    _custom_cache(home.book_info_dir, cache_name("v1"),
                  "CREATE TABLE ZAEBOOKINFO (Z_PK INTEGER PRIMARY KEY, ZDATABASEKEY VARCHAR, "
                  "ZBOOKTITLE, ZBOOKAUTHOR, ZBOOKLANGUAGE, ZPUBLISHERNAME, ZPUBLISHERYEAR)",
                  [(1, "I", "Int year", "", "  ", None, 2001),
                   (2, "F", "Float year", b"blob author", "fr", "P", 2001.0),
                   (3, "H", 42, "Half year", None, None, 2001.5),
                   (4, "E", "", "   ", "en", "Publisher only", "2001"),
                   (5, "S", " Spaced ", None, None, None, None)])
    got = reader.get_cached_book_info(["I", "F", "H", "E", "S"])
    assert got["I"] == CachedBookInfo("I", "Int year", None, None, None, "2001", cache_name("v1"))
    assert got["F"] == CachedBookInfo("F", "Float year", None, "fr", "P", "2001", cache_name("v1"))
    assert (got["H"].title, got["H"].year) == ("42", "2001.5")
    assert "E" not in got  # neither title nor author
    assert got["S"].title == " Spaced "  # kept as cached


def test_oversized_values_read_as_none(home, reader):
    big = 4097
    home.add_book_info_cache([
        {"asset_id": "L", "title": "x" * big, "author": "Kept author", "publisher": "\x00" + "p" * big},
        {"asset_id": "M", "title": "y" * 4096},  # at the cap: kept
        {"asset_id": "N", "title": "\x00" + "z" * big},  # a NUL doesn't hide the length
        {"asset_id": "O", "title": "é" * 2049, "author": "a"},  # bytes, not characters
    ])
    got = reader.get_cached_book_info(["L", "M", "N", "O"])
    assert got["L"] == CachedBookInfo("L", None, "Kept author", source=cache_name(V7))
    assert got["M"].title == "y" * 4096
    assert "N" not in got
    assert got["O"].title is None
    memo = next(iter(index_of(reader)._memos.values()))
    assert all(len(value or "") <= 4096 for row in memo.rows.values() if row for value in row)


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 31), reason="trusted_schema needs SQLite 3.31")
def test_the_cache_schema_is_not_trusted(home, reader):
    # A cache whose ZAEBOOKINFO is a view over a virtual table SQLite
    # doesn't deem harmless (here one that would put the cache's own path
    # in the title) is skipped, not run.
    folder = home.book_info_dir
    folder.mkdir(parents=True)
    con = sqlite3.connect(folder / cache_name("v1"))
    try:
        con.execute("CREATE VIEW ZAEBOOKINFO AS SELECT 'A' AS ZDATABASEKEY, file AS ZBOOKTITLE, "
                    "NULL AS ZBOOKAUTHOR FROM pragma_database_list")
    finally:
        con.close()
    home.add_book_info_cache([{"asset_id": "A", "title": "older"}], version="v0")
    assert reader.get_cached_book_info("A")["A"].title == "older"


def test_year_written_as_an_integer_reads_as_text(home, reader):
    home.add_book_info_cache([{"asset_id": "Y", "title": "Y", "year": 2001}])
    assert reader.get_cached_book_info("Y")["Y"].year == "2001"


def test_keys_in_first_occurrence_order(three, reader):
    got = reader.get_cached_book_info(["C", "Z", "A", "C", None, "", "B", "A"])
    assert list(got) == ["C", "A", "B"]


def test_invalid_utf8_reads_with_replacement(home, reader):
    path = home.add_book_info_cache([{"asset_id": "Q", "title": "placeholder", "author": "x"}])
    con = sqlite3.connect(path)
    try:
        con.execute("UPDATE ZAEBOOKINFO SET ZBOOKTITLE = CAST(X'4142FF43' AS TEXT)")
        con.commit()
    finally:
        con.close()
    assert reader.get_cached_book_info("Q")["Q"].title == "AB�C"


# -- arguments ----------------------------------------------------------------------


def test_one_str_and_iterables(three, reader):
    assert list(reader.get_cached_book_info("A")) == ["A"]
    assert list(reader.get_cached_book_info(i for i in ("B", None, "A"))) == ["B", "A"]
    assert list(reader.get_cached_book_info(("C",))) == ["C"]
    assert list(reader.get_cached_book_info({"A": 1})) == ["A"]


class _StrSub(str):
    pass


def test_str_subclass_items(three, reader):
    got = reader.get_cached_book_info([_StrSub("A")])
    assert list(got) == ["A"] and type(next(iter(got))) is str


def test_nothing_to_look_up_does_no_io(three, reader, spy):
    for empty in ("", [], [None, ""], iter(())):
        result, events = touch_folder_events(three.book_info_dir, lambda: reader.get_cached_book_info(empty))
        assert result == {} and events == []
    assert spy.uris == []


class _AnnotationLike:
    asset_id = "A"


@pytest.mark.parametrize("bad", [5, [5], b"abc", bytearray(b"abc"), [b"abc"], [object()], _AnnotationLike(),
                                 ["A", 7.5], [["A"]]], ids=repr)
def test_bad_arguments_raise_without_the_value(three, reader, spy, bad):
    with pytest.raises(InvalidArgumentError) as info:
        reader.get_cached_book_info(bad)
    message = str(info.value)
    assert "str" in message
    for leak in ("abc", "5", "7.5", "object at", "'A'"):
        assert leak not in message
    assert spy.uris == []  # checked before any read


def test_an_unbindable_id_does_not_hide_the_others(three, reader):
    assert list(reader.get_cached_book_info(["\ud800", "A"])) == ["A"]


# -- the open rule (cases shared with dump_schema) ---------------------------------


@pytest.mark.parametrize("case", cases.CASES, ids=str)
def test_open_rule(case, home, spy):
    folder = home.book_info_dir
    folder.mkdir(parents=True)
    key, title = f"{cases.SECRET}ZDATABASEKEY-1", f"{cases.SECRET}ZBOOKTITLE-1"
    with case.make(folder) as path:
        if case.mode is cases.REFUSED:
            with pytest.raises(book_info._Refused):
                book_info._open_mode(str(path))
        else:
            assert book_info._open_mode(str(path)) == case.mode
        before, before_bytes = cases.listing(folder), cases.contents(folder)
        api = PyAppleBooks(home.data_dir)
        try:
            got = api.get_cached_book_info([key, "absent"])
        finally:
            api.close()
        after, after_bytes = cases.listing(folder), cases.contents(folder)
    if case.mode is cases.REFUSED:
        assert spy.uris == []
    else:
        assert len(spy.uris) == 1 and spy.uris[0].endswith(
            "?mode=ro&immutable=1" if case.mode == cases.WAL_IMMUTABLE else "?mode=ro")
    committed = {key: CachedBookInfo(key, title, f"{cases.SECRET}ZBOOKAUTHOR-1",
                                     f"{cases.SECRET}ZBOOKLANGUAGE-1", f"{cases.SECRET}ZPUBLISHERNAME-1",
                                     f"{cases.SECRET}ZPUBLISHERYEAR-1", cases.CACHE_NAME)}
    if case.readable is True and case.mode is not cases.REFUSED:
        assert got == committed
    elif case.readable is False:
        assert got == {}
    else:  # the SQLite build decides (a hot journal): skipped, or the committed row only
        assert got in ({}, committed)
    # Nothing created, removed or changed; only SQLite's shared-memory WAL
    # index may be written in place by a read-only WAL read.
    if case.mode == cases.WAL:
        shm = f"{cases.CACHE_NAME}-shm"
        assert after.keys() == before.keys() and after[shm][0] == before[shm][0]
        for files in (before, after, before_bytes, after_bytes):
            del files[shm]
    assert after == before
    assert after_bytes == before_bytes


def test_open_rule_constants_match_the_cases():
    assert (book_info._JOURNAL, book_info._WAL, book_info._WAL_IMMUTABLE) == (
        cases.JOURNAL, cases.WAL, cases.WAL_IMMUTABLE)


def test_a_closed_wal_cache_is_read_immutable_and_left_as_is(three, reader, spy):
    folder = three.book_info_dir
    before = cases.listing(folder)
    reader.get_cached_book_info(["Z"])  # absent: every file is read
    assert sorted(spy.files()) == sorted(cache_name(v) for v in (V0, V7, V10))
    for uri, name in zip(spy.uris, spy.files()):
        assert uri.endswith("?mode=ro" if name == cache_name(V7) else "?mode=ro&immutable=1")
    assert cases.listing(folder) == before  # no -wal, -shm or -journal made


def test_no_connection_stays_open(three, reader, spy):
    reader.get_cached_book_info(["Z"])
    assert spy.connections
    for con in spy.connections:
        with pytest.raises(sqlite3.ProgrammingError):
            con.execute("SELECT 1")


_HOLD = """
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], isolation_level=None)
for statement in sys.argv[2:]:
    con.execute(statement)
print("holding", flush=True)
sys.stdin.read()
con.execute("ROLLBACK")
con.close()
"""


@contextlib.contextmanager
def held(path, *statements):
    """Another process runs ``statements`` on ``path`` and keeps its
    transaction open, as Books would."""
    holder = subprocess.Popen([sys.executable, "-I", "-c", _HOLD, str(path), *statements],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "holding"
        yield
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
        holder.stdout.close()


def _set_title(path, key, title):
    con = sqlite3.connect(path)
    try:
        con.execute("UPDATE ZAEBOOKINFO SET ZBOOKTITLE = ? WHERE ZDATABASEKEY = ?", (title, key))
        con.commit()
    finally:
        con.close()


def test_locked_cache_keeps_its_last_rows(home, reader, monkeypatch):
    path = home.add_book_info_cache([{"asset_id": "K", "title": "v1"}], journal_mode="DELETE")
    assert reader.get_cached_book_info("K")["K"].title == "v1"
    _set_title(path, "K", "v2")
    monkeypatch.setattr(book_info, "BOOK_INFO_RECHECK", 0.0)
    with held(path, "BEGIN EXCLUSIVE"):
        start = time.monotonic()
        got = reader.get_cached_book_info("K")
        assert time.monotonic() - start < 1.0  # its busy wait (0.25 s), not the holder's
    assert got["K"].title == "v1"  # the last good row
    assert reader.get_cached_book_info("K")["K"].title == "v2"  # read again once released


def test_locked_cache_without_earlier_rows_is_skipped(home, reader):
    path = home.add_book_info_cache([{"asset_id": "K", "title": "v1"}], journal_mode="DELETE")
    with held(path, "BEGIN EXCLUSIVE"):
        assert reader.get_cached_book_info("K") == {}
    assert reader.get_cached_book_info("K")["K"].title == "v1"  # not remembered as absent


def test_uncommitted_titles_are_never_returned(home, reader):
    path = home.add_book_info_cache([{"asset_id": "K", "title": "committed"}], journal_mode="DELETE")
    with held(path, "BEGIN IMMEDIATE", "UPDATE ZAEBOOKINFO SET ZBOOKTITLE = 'uncommitted'"):
        assert reader.get_cached_book_info("K")["K"].title == "committed"


def test_a_spilled_uncommitted_write_is_never_returned(home, reader):
    # The writer's page cache overflows, so its change reaches the file
    # before commit: SQLite's locks keep readers away (the file is
    # skipped) rather than show it.
    path = home.add_book_info_cache([{"asset_id": f"K{i}", "title": "committed"} for i in range(200)],
                                    journal_mode="DELETE")
    with held(path, "PRAGMA cache_size=1", "BEGIN IMMEDIATE",
              "UPDATE ZAEBOOKINFO SET ZBOOKTITLE = 'uncommitted' || hex(randomblob(500))"):
        got = reader.get_cached_book_info([f"K{i}" for i in range(200)])
    assert all(info.title == "committed" for info in got.values())


def test_a_hot_journal_never_shows_uncommitted_titles(home, reader):
    # A copy of a cache and its journal taken while a writer had spilled
    # retitled pages into the file: the journal is hot. A read-only open
    # can't roll it back, so the file is skipped (or, on a SQLite build
    # that reads past it, only committed titles are seen); nothing is
    # written.
    folder = home.book_info_dir
    work = home.root / "work"
    source = home.add_book_info_cache([{"asset_id": f"K{i}", "title": "committed"} for i in range(400)],
                                      journal_mode="DELETE")
    staged = work / source.name
    work.mkdir()
    os.replace(source, staged)
    writer = sqlite3.connect(staged, isolation_level=None)
    try:
        writer.execute("PRAGMA cache_size=1")
        writer.execute("BEGIN")
        writer.execute("UPDATE ZAEBOOKINFO SET ZBOOKTITLE = 'uncommitted' || hex(randomblob(800))")
        assert os.path.exists(f"{staged}-journal")
        for side in ("", "-journal"):
            shutil.copyfile(f"{staged}{side}", f"{source}{side}")
        writer.execute("ROLLBACK")
    finally:
        writer.close()
    before, before_bytes = cases.listing(folder), cases.contents(folder)
    got = reader.get_cached_book_info([f"K{i}" for i in range(400)])
    assert all(info.title == "committed" for info in got.values())
    assert cases.listing(folder) == before and cases.contents(folder) == before_bytes


def test_immutable_read_is_rechecked(home, reader, monkeypatch):
    path = home.add_book_info_cache([{"asset_id": "K", "title": "v1"}])  # WAL, closed: immutable
    assert reader.get_cached_book_info("K")["K"].title == "v1"
    _set_title(path, "K", "v2")
    monkeypatch.setattr(book_info, "BOOK_INFO_RECHECK", 0.0)
    real = book_info._files_signature
    calls = iter(range(1000))
    monkeypatch.setattr(book_info, "_files_signature", lambda p: (real(p), next(calls)))
    assert reader.get_cached_book_info("K")["K"].title == "v1"  # the changing read was dropped
    monkeypatch.setattr(book_info, "_files_signature", real)
    assert reader.get_cached_book_info("K")["K"].title == "v2"


def test_immutable_recheck_sees_a_sidecar_appear(home, reader, monkeypatch):
    path = home.add_book_info_cache([{"asset_id": "K", "title": "v1"}])
    real_read = book_info._execute

    def execute(con, sql, params=()):
        rows = real_read(con, sql, params)
        if sql.startswith("SELECT"):
            pathlib.Path(f"{path}-wal").write_bytes(b"")  # Books opened the cache meanwhile
        return rows

    monkeypatch.setattr(book_info, "_execute", execute)
    assert reader.get_cached_book_info("K") == {}


def test_books_closing_a_wal_cache_during_the_read(home, reader, monkeypatch, caplog):
    # The open rule saw the -wal and -shm of a cache Books has open; Books
    # closes it (removing both) before the read starts, so the read-only
    # open creates them again. That read is dropped and logged, the files
    # are left (by then they may be Books' own), and the next call reads
    # the cache.
    folder = home.book_info_dir
    path = folder / cases.CACHE_NAME
    books = cases.create(path, "WAL")
    books.execute("PRAGMA wal_autocheckpoint=0")
    key = f"{cases.SECRET}ZDATABASEKEY-1"
    real_connect = book_info._connect

    def connect(uri, busy):
        if books is not None:
            books.close()  # checkpoints and removes -wal and -shm
            assert cases.sidecars(path) == []
        return real_connect(uri, busy)

    monkeypatch.setattr(book_info, "_connect", connect)
    with caplog.at_level(logging.DEBUG, logger="py_apple_books"):
        assert reader.get_cached_book_info(key) == {}
    assert cases.sidecars(path) == [f"{cases.CACHE_NAME}-shm", f"{cases.CACHE_NAME}-wal"]
    assert [r.getMessage() for r in caplog.records] == [
        f"AEBookInfo cache {cases.CACHE_NAME} skipped: SidecarsChanged"]
    assert cases.CACHE_NAME not in index_of(reader)._memos  # nothing remembered
    books = None
    assert reader.get_cached_book_info(key)[key].title == f"{cases.SECRET}ZBOOKTITLE-1"


# -- per-id queries and the memo -------------------------------------------------


def test_only_requested_ids_are_bound(three, reader, spy):
    ids = [f"id{i}" for i in range(1200)] + ["A"]
    reader.get_cached_book_info(ids)
    selects = spy.selects()
    assert selects and all(1 <= len(params) <= 500 for params in selects)
    assert all(set(params) <= set(ids) for params in selects)
    # The newest file binds every id once, in chunks.
    first = [p for p in selects[:3]]
    assert [len(p) for p in first] == [500, 500, 201] and sum(first, []) == ids


def test_older_files_are_not_opened_once_every_id_has_a_title(three, reader, spy):
    reader.get_cached_book_info("A")
    assert spy.files() == [cache_name(V10)]
    spy.reset()
    reader.get_cached_book_info(["A", "C"])  # C is in 26.7
    assert spy.files() == [cache_name(V10), cache_name(V7)]
    # Only C was asked of 26.10: A was remembered.
    assert spy.selects()[0] == ["C"]


def test_a_second_call_opens_nothing(three, reader, spy):
    first = reader.get_cached_book_info(["A", "B", "C", "T", "Z"])
    spy.reset()
    second, events = touch_folder_events(three.book_info_dir,
                                         lambda: reader.get_cached_book_info(["A", "B", "C", "T", "Z"]))
    assert second == first and spy.uris == [] and events == []


def test_only_changed_files_are_read_after_the_recheck(three, reader, spy, monkeypatch):
    reader.get_cached_book_info(["Z"])
    assert len(spy.uris) == 3
    spy.reset()
    con = sqlite3.connect(three.book_info_dir / cache_name(V7))
    con.execute("INSERT INTO ZAEBOOKINFO (ZDATABASEKEY, ZBOOKTITLE) VALUES ('Z', 'Z now in 26.7')")
    con.commit()
    con.close()
    assert reader.get_cached_book_info(["Z"]) == {}  # within BOOK_INFO_RECHECK: not looked at
    assert spy.uris == []
    monkeypatch.setattr(book_info, "BOOK_INFO_RECHECK", 0.0)  # read at call time
    assert reader.get_cached_book_info(["Z"])["Z"].title == "Z now in 26.7"
    assert spy.files() == [cache_name(V7)]


_BOOKS = """
import sqlite3, sys
con = sqlite3.connect(sys.argv[1], isolation_level=None)
con.execute("PRAGMA wal_autocheckpoint=0")
con.execute("SELECT count(*) FROM ZAEBOOKINFO").fetchall()
print("open", flush=True)
for statement in sys.stdin:
    con.execute(statement)
    print("done", flush=True)
con.close()
"""


@contextlib.contextmanager
def books_has_open(path):
    """Another process has the WAL-mode cache ``path`` open, as Books
    would, and never checkpoints it; ``write(sql)`` runs a statement
    there (its change lands in the ``-wal`` only)."""
    holder = subprocess.Popen([sys.executable, "-I", "-c", _BOOKS, str(path)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def write(sql):
        holder.stdin.write(sql + "\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "done"

    try:
        assert holder.stdout.readline().strip() == "open"
        yield write
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
        holder.stdout.close()


def test_a_write_to_an_open_wal_cache_is_seen_after_the_recheck(home, reader, spy, monkeypatch):
    # Books keeps its newest cache open in WAL mode: a new row lands in
    # the -wal, and the main file's size and mtime stay as they were
    # until a checkpoint. The -wal's change is what makes the file read
    # again.
    path = home.book_info_dir / cases.CACHE_NAME
    cases.create(path, "WAL").close()
    with books_has_open(path) as write:
        assert book_info._open_mode(str(path)) == cases.WAL
        assert reader.get_cached_book_info("NEW") == {}
        main = path.stat()
        write("INSERT INTO ZAEBOOKINFO (ZDATABASEKEY, ZBOOKTITLE) VALUES ('NEW', 'new title')")
        assert (path.stat().st_size, path.stat().st_mtime_ns) == (main.st_size, main.st_mtime_ns)
        spy.reset()
        assert reader.get_cached_book_info("NEW") == {}  # within BOOK_INFO_RECHECK: not looked at
        assert spy.uris == []
        monkeypatch.setattr(book_info, "BOOK_INFO_RECHECK", 0.0)
        assert reader.get_cached_book_info("NEW")["NEW"].title == "new title"
        assert spy.files() == [cases.CACHE_NAME] and spy.uris[0].endswith("?mode=ro")


def test_recheck_interval_is_read_at_call_time(three, reader, spy, monkeypatch):
    reader.get_cached_book_info(["A"])
    index = index_of(reader)
    index._checked -= 31.0  # as if BOOK_INFO_RECHECK passed
    with _fs_audit.record() as rec:
        reader.get_cached_book_info(["A"])
    assert rec.of("os.listdir")
    monkeypatch.setattr(book_info, "BOOK_INFO_RECHECK", 3600.0)
    index._checked -= 31.0
    with _fs_audit.record() as rec:
        reader.get_cached_book_info(["A"])
    assert not rec.of("os.listdir")


def test_a_new_id_within_the_interval_reads_without_listing(three, reader, spy):
    # The memo can't answer B, so the files are read; the folder was
    # looked at moments ago, so it is not listed again.
    reader.get_cached_book_info("A")
    spy.reset()
    with _fs_audit.record() as rec:
        got = reader.get_cached_book_info(["A", "B"])
    assert got["B"].title == "Only B"
    assert spy.selects() and all(params == ["B"] for params in spy.selects())
    assert not rec.of("os.listdir", "os.scandir")


def test_purged_file_rows_disappear(three, reader, monkeypatch):
    assert "B" in reader.get_cached_book_info("B")
    (three.book_info_dir / cache_name(V0)).unlink()
    monkeypatch.setattr(book_info, "BOOK_INFO_RECHECK", 0.0)
    assert reader.get_cached_book_info(["B", "A"]) == {
        "A": CachedBookInfo("A", "A in 26.10", None, source=cache_name(V10))}
    assert cache_name(V0) not in index_of(reader)._memos


def test_purged_folder_rows_disappear(three, reader, monkeypatch):
    assert reader.get_cached_book_info("A")
    for path in three.book_info_dir.iterdir():
        path.unlink()
    three.book_info_dir.rmdir()
    monkeypatch.setattr(book_info, "BOOK_INFO_RECHECK", 0.0)
    assert reader.get_cached_book_info("A") == {}
    assert index_of(reader)._memos == {}


def test_a_file_purged_between_listing_and_reading(three, reader, spy):
    index = book_info._BookInfoIndex(str(three.book_info_dir))
    index._check_folder()
    (three.book_info_dir / cache_name(V10)).unlink()
    found = index.lookup(["A"], time.monotonic() + 2)
    assert found["A"][0] == cache_name(V7)
    assert cache_name(V10) not in spy.files()


def test_memo_is_bounded_per_file(three, reader):
    ids = [f"m{i}" for i in range(3000)] + ["A"]
    got = reader.get_cached_book_info(ids)
    assert list(got) == ["A"]
    memos = index_of(reader)._memos
    assert memos and all(len(memo.rows) <= book_info._MEMO_IDS for memo in memos.values())


def test_a_read_remembers_the_last_ids_it_was_asked(home, reader, monkeypatch):
    monkeypatch.setattr(book_info, "_MEMO_IDS", 4)
    home.add_book_info_cache([{"asset_id": f"k{i}", "title": f"t{i}"} for i in range(10)])
    ids = [f"k{i}" for i in range(10)] + ["absent"]
    assert len(reader.get_cached_book_info(ids)) == 10
    (memo,) = index_of(reader)._memos.values()
    assert list(memo.rows) == ids[-4:] and memo.rows["absent"] is None


def test_memo_is_bounded_across_calls(home, reader, monkeypatch):
    # Each call that brings new ids adds them; the oldest are dropped, so
    # a long-running process holds at most _MEMO_IDS ids per file.
    monkeypatch.setattr(book_info, "_MEMO_IDS", 4)
    home.add_book_info_cache([{"asset_id": f"k{i}", "title": f"t{i}"} for i in range(10)])
    reader.get_cached_book_info(["k0", "k1", "k2"])
    reader.get_cached_book_info(["k3", "k4", "k5"])
    (memo,) = index_of(reader)._memos.values()
    assert list(memo.rows) == ["k2", "k3", "k4", "k5"]
    reader.get_cached_book_info(["k6", "absent"])
    assert list(memo.rows) == ["k4", "k5", "k6", "absent"]


def test_a_memo_hit_makes_an_id_recently_used(home, reader, monkeypatch):
    monkeypatch.setattr(book_info, "_MEMO_IDS", 3)
    home.add_book_info_cache([{"asset_id": f"k{i}", "title": f"t{i}"} for i in range(10)])
    reader.get_cached_book_info(["k0", "k1", "k2"])
    (memo,) = index_of(reader)._memos.values()
    reader.get_cached_book_info("k0")  # answered by the memo
    assert list(memo.rows) == ["k1", "k2", "k0"]
    reader.get_cached_book_info("k3")  # read: the least recently used (k1) goes
    assert list(memo.rows) == ["k2", "k0", "k3"]


def test_many_ids_all_found(home, reader):
    rows = [{"asset_id": f"k{i:04d}", "title": f"t{i}"} for i in range(1500)]
    home.add_book_info_cache(rows)
    got = reader.get_cached_book_info([r["asset_id"] for r in rows])
    assert len(got) == 1500 and got["k1499"].title == "t1499"


# -- budget ------------------------------------------------------------------------


def test_a_locked_file_within_a_query_deadline(three, reader):
    newest = three.book_info_dir / "AEBookInfo-v20250715-26.11.sqlite"
    newest_lib = FixtureLibrary(three.root)
    newest_lib.add_book_info_cache([{"asset_id": "A", "title": "A in 26.11"}], version="v20250715-26.11",
                                   journal_mode="DELETE")
    with held(newest, "BEGIN EXCLUSIVE"):
        # The locked file costs its busy wait (0.25 s); the rest of the
        # deadline reads the other files.
        start = time.monotonic()
        with reader.query_deadline(0.6):
            got = reader.get_cached_book_info(["A", "B"])
        elapsed = time.monotonic() - start
        assert elapsed < 0.9
        assert got["A"].source == cache_name(V10) and got["B"].title == "Only B"
        # A tighter deadline is kept too: the call returns by then, with
        # whatever it read (here nothing new: the wait used it up).
        start = time.monotonic()
        with reader.query_deadline(0.2):
            reader.get_cached_book_info(["A", "B", "C"])
        assert time.monotonic() - start < 0.5


def test_locked_files_cost_their_busy_wait_each(home, reader):
    paths = [home.add_book_info_cache([{"asset_id": "A", "title": f"in {v}"}], version=v, journal_mode="DELETE")
             for v in ("v1", "v2", "v3")]
    with contextlib.ExitStack() as stack:
        for path in paths:
            stack.enter_context(held(path, "BEGIN EXCLUSIVE"))
        start = time.monotonic()
        assert reader.get_cached_book_info("A") == {}
        elapsed = time.monotonic() - start
    assert elapsed < 2.1


def test_the_call_budget_caps_the_waits(home, reader, monkeypatch):
    monkeypatch.setattr(book_info, "_CALL_BUDGET", 0.4)
    paths = [home.add_book_info_cache([{"asset_id": "A", "title": f"in {v}"}], version=v, journal_mode="DELETE")
             for v in ("v1", "v2", "v3")]
    with contextlib.ExitStack() as stack:
        for path in paths:
            stack.enter_context(held(path, "BEGIN EXCLUSIVE"))
        start = time.monotonic()
        reader.get_cached_book_info("A")
        assert time.monotonic() - start < 0.8


def test_query_timeout_caps_the_call(home, monkeypatch):
    # A busy wait of 1 s would alone take the call past the bound; only
    # the library's query_timeout (0.1 s) keeps it under.
    monkeypatch.setattr(book_info, "_BUSY_WAIT", 1.0)
    path = home.add_book_info_cache([{"asset_id": "A", "title": "x"}], journal_mode="DELETE")
    api = PyAppleBooks(home.data_dir, query_timeout=0.1)
    try:
        with held(path, "BEGIN EXCLUSIVE"):
            start = time.monotonic()
            assert api.get_cached_book_info("A") == {}
            assert time.monotonic() - start < 0.5
    finally:
        api.close()


def test_an_expired_deadline_reads_nothing_and_never_raises(three, reader, spy):
    with reader.query_deadline(0):
        result, events = touch_folder_events(three.book_info_dir, lambda: reader.get_cached_book_info("A"))
    assert result == {} and events == [] and spy.uris == []
    assert reader.get_cached_book_info("A")["A"].title == "A in 26.10"
    with reader.query_deadline(0):  # what is remembered is still answered
        assert reader.get_cached_book_info("A")["A"].title == "A in 26.10"


def test_a_slow_statement_is_interrupted_at_the_file_budget(home, reader, monkeypatch):
    home.add_book_info_cache([{"asset_id": "A", "title": "x"}])
    monkeypatch.setattr(book_info, "_FILE_BUDGET", 0.2)
    real = book_info._execute

    def slow(con, sql, params=()):
        if sql.startswith("SELECT"):
            # A statement that runs past the budget: the progress handler stops it.
            return con.execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) "
                               "SELECT count(*) FROM n").fetchall()
        return real(con, sql, params)

    monkeypatch.setattr(book_info, "_execute", slow)
    start = time.monotonic()
    assert reader.get_cached_book_info("A") == {}
    assert time.monotonic() - start < 1.0


# -- lifetime and threads -----------------------------------------------------------


def test_close_drops_the_memo(three, reader, spy):
    reader.get_cached_book_info("A")
    index = index_of(reader)
    assert isinstance(index, book_info._BookInfoIndex) and not index.dead
    reader.close()
    assert index.dead and index_of(reader) is None
    spy.reset()
    assert reader.get_cached_book_info("A")["A"].title == "A in 26.10"
    assert spy.files() == [cache_name(V10)]


@pytest.mark.parametrize("name", ["a?mode=rwc&x=", "b#frag", "c%41d", "d é space", "e&immutable=0"])
def test_uri_syntax_in_the_folder_name_stays_a_path(tmp_path, spy, name):
    # Percent-encoding keeps a folder name from adding URI parameters
    # (mode=rwc, immutable) or cutting the path short.
    lib = FixtureLibrary(tmp_path / name)
    closed = lib.add_book_info_cache([{"asset_id": "A", "title": "wal"}], version=V0)
    journal = lib.add_book_info_cache([{"asset_id": "B", "title": "journal"}], version=V7, journal_mode="DELETE")
    before = cases.listing(lib.book_info_dir)
    api = PyAppleBooks(lib.data_dir)
    try:
        got = api.get_cached_book_info(["A", "B"])
    finally:
        api.close()
    assert {k: v.title for k, v in got.items()} == {"A": "wal", "B": "journal"}
    uris = {unquote(uri[len("file:"):].split("?", 1)[0]): uri.split("?", 1)[1] for uri in spy.uris}
    assert uris == {str(journal): "mode=ro", str(closed): "mode=ro&immutable=1"}
    assert cases.listing(lib.book_info_dir) == before


def test_the_index_holds_no_library(three, reader):
    reader.get_cached_book_info("A")
    index = index_of(reader)
    assert not any(isinstance(value, LibraryDB) for value in vars(index).values())
    assert index.folder == str(three.book_info_dir)


def test_threads_share_one_read_per_file(three, reader, spy):
    ids = ["A", "B", "C", "T", "Z"]
    barrier = threading.Barrier(8)
    results, errors = [], []

    def work():
        try:
            barrier.wait(timeout=10)
            results.append(reader.get_cached_book_info(ids))
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors and len(results) == 8
    assert all(r == results[0] for r in results) and list(results[0]) == ["A", "B", "C", "T"]
    assert sorted(spy.files()) == sorted(cache_name(v) for v in (V0, V7, V10))  # each opened once


def test_a_waiting_thread_answers_from_the_memo(three, reader, monkeypatch):
    assert reader.get_cached_book_info("A")
    index = index_of(reader)
    with index._build:  # another thread is reading the files
        start = time.monotonic()
        with reader.query_deadline(0.2):
            got = reader.get_cached_book_info(["A", "B"])
        assert time.monotonic() - start < 1.0
    assert list(got) == ["A"]  # B was never read: not known yet


def test_remembered_ids_are_answered_without_waiting(three, reader, spy, monkeypatch):
    first = reader.get_cached_book_info(["A", "B", "C", "T", "Z"])
    index = index_of(reader)
    spy.reset()
    with index._build:  # another thread is reading the files, for up to 2 s
        start = time.monotonic()
        assert reader.get_cached_book_info(["T", "A", "Z"]) == {k: first[k] for k in ("T", "A")}
        assert time.monotonic() - start < 0.5
        # Once the folder check is due, the lookup waits for its turn.
        monkeypatch.setattr(book_info, "BOOK_INFO_RECHECK", 0.0)
        start = time.monotonic()
        with reader.query_deadline(0.3):
            assert reader.get_cached_book_info("A") == {"A": first["A"]}
        assert time.monotonic() - start >= 0.25
    assert spy.uris == []


def test_two_libraries_on_one_folder(three):
    one, two = PyAppleBooks(three.data_dir), PyAppleBooks(three.data_dir)
    try:
        results = []
        threads = [threading.Thread(target=lambda api=api: results.append(api.get_cached_book_info(["A", "B"])))
                   for api in (one, two, one, two)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert len(results) == 4 and all(r == results[0] for r in results) and len(results[0]) == 2
    finally:
        one.close()
        two.close()


def test_libraries_never_read_cache_files_at_once(three, monkeypatch):
    # Opening a cache's header (os.open/os.close) drops every POSIX lock
    # the process holds on it, a read lock of another library's open
    # connection included: one library's read must keep the others' out
    # of every cache file until its connection is closed.
    one, two = PyAppleBooks(three.data_dir), PyAppleBooks(three.data_dir)
    entered, release = threading.Event(), threading.Event()
    opened_while_held = []
    real_execute, real_open_mode = book_info._execute, book_info._open_mode

    def execute(con, sql, params=()):
        if threading.current_thread().name == "one" and sql.startswith("SELECT"):
            entered.set()
            assert release.wait(timeout=20)
        return real_execute(con, sql, params)

    def open_mode(path):
        if threading.current_thread().name != "one" and entered.is_set() and not release.is_set():
            opened_while_held.append(os.path.basename(path))
        return real_open_mode(path)

    monkeypatch.setattr(book_info, "_execute", execute)
    monkeypatch.setattr(book_info, "_open_mode", open_mode)
    worker = threading.Thread(target=lambda: one.get_cached_book_info("A"), name="one")
    worker.start()
    try:
        assert entered.wait(timeout=10)  # library one has a cache open, mid-read
        start = time.monotonic()
        with two.query_deadline(0.3):
            assert two.get_cached_book_info("A") == {}  # waited for its turn, read nothing
        assert time.monotonic() - start < 1.5
        assert opened_while_held == []
    finally:
        release.set()
        worker.join(timeout=30)
    try:
        assert two.get_cached_book_info("A")["A"].title == "A in 26.10"  # nothing was remembered
    finally:
        one.close()
        two.close()


def _child_time_limit(seconds: int = 20) -> None:
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(seconds)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_fork_while_another_thread_is_between_statements(three, reader, monkeypatch):
    # The reader is paused in Python between two statements, holding the
    # index's build lock and the process's cache-read lock, never inside
    # SQLite: a fork while a thread is inside SQLite can leave SQLite's
    # own mutexes locked in the child (LibraryDB's fork rule), which no
    # lock of this module can undo.
    entered, release = threading.Event(), threading.Event()
    real = book_info._execute

    def execute(con, sql, params=()):
        if threading.current_thread().name == "reader" and sql.startswith("SELECT"):
            entered.set()
            assert release.wait(timeout=20)
        return real(con, sql, params)

    monkeypatch.setattr(book_info, "_execute", execute)
    worker = threading.Thread(target=lambda: reader.get_cached_book_info("A"), name="reader")
    worker.start()
    try:
        assert entered.wait(timeout=10)  # it holds the index's build lock and the read lock
        read_end, write_end = os.pipe()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # fork() with other threads alive
            pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            status = 1
            try:
                _child_time_limit()
                os.close(read_end)
                got = reader.get_cached_book_info(["A", "B"])
                fresh = index_of(reader) is not None
                os.write(write_end, repr((sorted(got), got["A"].title, fresh)).encode())
                status = 0
            finally:
                os._exit(status)
        os.close(write_end)
        with os.fdopen(read_end, "rb") as pipe:
            output = pipe.read()
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        assert output == repr((["A", "B"], "A in 26.10", True)).encode()
    finally:
        release.set()
        worker.join(timeout=30)


# -- folder guards -------------------------------------------------------------------


def _container_at(base: pathlib.Path) -> FixtureLibrary:
    lib = FixtureLibrary(base)
    lib.add_book_info_cache([{"asset_id": "A", "title": "Hidden"}])
    return lib


def _assert_not_read(data_dir, spy):
    api = PyAppleBooks(data_dir)
    try:
        with _fs_audit.record() as rec:
            assert api.get_cached_book_info("A") == {}
    finally:
        api.close()
    assert not rec.of("os.listdir", "os.scandir") and spy.uris == []


@pytest.mark.parametrize("cloud", ["Mobile Documents", "com~apple~CloudDocs", "CloudStorage", "mobile documents"])
def test_folder_in_cloud_storage_is_never_listed(tmp_path, spy, cloud):
    lib = _container_at(tmp_path / "Library" / cloud / "x")
    _assert_not_read(lib.data_dir, spy)


def test_folder_resolving_into_cloud_storage_is_never_listed(tmp_path, spy):
    real = _container_at(tmp_path / "Library" / "Mobile Documents" / "x")
    link = tmp_path / "link"
    link.symlink_to(real.root, target_is_directory=True)
    _assert_not_read(FixtureLibrary(link).data_dir, spy)


def test_symlinked_cache_folder_is_never_listed(tmp_path, spy):
    elsewhere = _container_at(tmp_path / "elsewhere")
    home = FixtureLibrary(tmp_path / "home")
    home.book_info_dir.parent.mkdir(parents=True)
    home.book_info_dir.symlink_to(elsewhere.book_info_dir, target_is_directory=True)
    _assert_not_read(home.data_dir, spy)


def _fake_stat(st, **changes):
    fields = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
    fields.update(changes)
    return types.SimpleNamespace(**fields)


def _patch_lstat(monkeypatch, target: pathlib.Path, **changes):
    real = book_info._icloud.lstat
    target = str(target)

    def lstat(path, *, dir_fd=None):
        st = real(path, dir_fd=dir_fd)
        return _fake_stat(st, **changes) if os.fspath(path) == target else st

    monkeypatch.setattr(book_info._icloud, "lstat", lstat)


def test_evicted_folder_is_never_listed(home, spy, monkeypatch):
    home.add_book_info_cache([{"asset_id": "A", "title": "x"}])
    _patch_lstat(monkeypatch, home.book_info_dir, st_flags=0x40000000)
    _assert_not_read(home.data_dir, spy)


@pytest.mark.parametrize("changes", [{"st_flags": 0x40000000}, {"st_blocks": 0}], ids=["flag", "no_blocks"])
def test_evicted_file_is_never_opened(home, reader, spy, monkeypatch, changes):
    home.add_book_info_cache([{"asset_id": "A", "title": "older"}], version=V0)
    evicted = home.add_book_info_cache([{"asset_id": "A", "title": "newer"}], version=V7)
    _patch_lstat(monkeypatch, evicted, **changes)
    with _fs_audit.record() as rec:
        got = reader.get_cached_book_info("A")
    assert got["A"].title == "older"
    assert not [e for e in rec.of("open", "sqlite3.connect") if e.path == str(evicted.resolve())]
    assert spy.files() == [cache_name(V0)]


def test_compressed_file_without_blocks_is_read(home, reader, monkeypatch):
    path = home.add_book_info_cache([{"asset_id": "A", "title": "compressed"}])
    _patch_lstat(monkeypatch, path, st_blocks=0, st_flags=0x20)
    assert reader.get_cached_book_info("A")["A"].title == "compressed"


def test_reads_run_with_materialization_off(home, reader, monkeypatch):
    home.add_book_info_cache([{"asset_id": "A", "title": "x"}])
    depth = {"now": 0, "seen": []}

    @contextlib.contextmanager
    def no_materialize():
        depth["now"] += 1
        try:
            yield
        finally:
            depth["now"] -= 1

    real_connect, real_list = book_info._connect, book_info._list_folder
    monkeypatch.setattr(book_info._icloud, "no_materialize", no_materialize)
    monkeypatch.setattr(book_info, "_connect",
                        lambda uri, busy: depth["seen"].append(depth["now"]) or real_connect(uri, busy))
    monkeypatch.setattr(book_info, "_list_folder", lambda f: depth["seen"].append(depth["now"]) or real_list(f))
    assert reader.get_cached_book_info("A")
    assert depth["seen"] == [1, 1]


_CASE = {case.name: case for case in cases.CASES}


@pytest.mark.parametrize("case, side, mode", [
    ("wal_live", "-wal", cases.WAL_IMMUTABLE),  # opens no sidecar
    ("wal_live", "-shm", cases.WAL_IMMUTABLE),
    ("wal_persist_journal", "-journal", cases.REFUSED),  # SQLite would read a byte of it
    ("persist_journal", "-journal", cases.REFUSED),
    ("journal_empty_wal", "-wal", cases.REFUSED),  # SQLite would open it
], ids=lambda v: v if isinstance(v, str) else "refused")
def test_evicted_sidecars(home, monkeypatch, case, side, mode):
    folder = home.book_info_dir
    folder.mkdir(parents=True)
    with _CASE[case].make(folder) as path:
        assert book_info._open_mode(str(path)) == _CASE[case].mode  # as found
        _patch_lstat(monkeypatch, f"{path}{side}", st_flags=book_info._icloud.SF_DATALESS)
        if mode is cases.REFUSED:
            with pytest.raises(book_info._Refused):
                book_info._open_mode(str(path))
        else:
            assert book_info._open_mode(str(path)) == mode


def _fail_header_read(monkeypatch, name):
    real = os.open

    def fake(path, *args, **kwargs):
        if os.fspath(path).endswith("/" + name):
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")  # a dataless file
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake)


def _fail_connect(monkeypatch, name):
    real = book_info._connect

    def fake(uri, busy):
        if name in uri:
            raise sqlite3.OperationalError("disk I/O error")
        return real(uri, busy)

    monkeypatch.setattr(book_info, "_connect", fake)


def _fail_select(monkeypatch, name):
    real = book_info._execute

    def fake(con, sql, params=()):
        if sql.startswith("SELECT") and any(row[2].endswith("/" + name) for row in con.execute(
                "PRAGMA database_list")):
            raise sqlite3.OperationalError("disk I/O error")
        return real(con, sql, params)

    monkeypatch.setattr(book_info, "_execute", fake)


@pytest.mark.parametrize("fail", [_fail_header_read, _fail_connect, _fail_select],
                         ids=["header_edeadlk", "connect_io_error", "select_io_error"])
def test_a_failed_read_is_skipped_and_tried_again(home, reader, monkeypatch, fail):
    # The backstop under the lstat gates: a read that would download
    # (EDEADLK) or fails is skipped without raising, and nothing about the
    # file is remembered, so the next call reads it.
    home.add_book_info_cache([{"asset_id": "A", "title": "older"}], version=V0)
    home.add_book_info_cache([{"asset_id": "A", "title": "newer"}], version=V7)
    with monkeypatch.context() as patch:
        fail(patch, cache_name(V7))
        assert reader.get_cached_book_info("A")["A"].title == "older"
    assert cache_name(V7) not in index_of(reader)._memos
    assert reader.get_cached_book_info("A")["A"].title == "newer"


@pytest.mark.skipif(sys.platform != "darwin", reason="the I/O policy is macOS's")
def test_reads_run_with_the_thread_materialization_policy_off(home, reader, monkeypatch):
    icloud = book_info._icloud
    functions = icloud._policy_functions()
    if functions is None:
        pytest.skip("getiopolicy_np is not available")
    get = functions[0]

    def policy():
        return get(icloud.IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, icloud.IOPOL_SCOPE_THREAD)

    home.add_book_info_cache([{"asset_id": "A", "title": "x"}])
    seen = []
    real_list, real_open, real_connect = book_info._list_folder, os.open, book_info._connect
    monkeypatch.setattr(book_info, "_list_folder", lambda f: seen.append(("list", policy())) or real_list(f))
    monkeypatch.setattr(os, "open", lambda p, *a, **k: (seen.append(("open", policy()))
                                                        if "AEBookInfo-" in os.fspath(p) else None)
                        or real_open(p, *a, **k))
    monkeypatch.setattr(book_info, "_connect",
                        lambda uri, busy: seen.append(("connect", policy())) or real_connect(uri, busy))
    before = policy()
    assert reader.get_cached_book_info("A")["A"].title == "x"
    off = icloud.IOPOL_MATERIALIZE_DATALESS_FILES_OFF
    assert seen == [("list", off), ("open", off), ("connect", off)]
    assert policy() == before  # restored


# -- folder derivation ----------------------------------------------------------------


def test_folder_from_data_dir(three, reader):
    reader.get_cached_book_info("A")
    assert index_of(reader).folder == str(three.book_info_dir)


def test_folder_from_a_library_store_in_the_container(make_library):
    lib = make_library()
    lib.add_book_info_cache([{"asset_id": "A", "title": "x"}])
    api = PyAppleBooks(library_db=lib.library_path)
    try:
        assert api.get_cached_book_info("A")["A"].title == "x"
        assert index_of(api).folder == str(lib.book_info_dir)
    finally:
        api.close()


def test_library_store_outside_the_container_layout(make_library, tmp_path):
    lib = make_library()
    lib.add_book_info_cache([{"asset_id": "A", "title": "x"}])
    flat = tmp_path / "flat"
    flat.mkdir()
    store = flat / lib.library_path.name
    store.write_bytes(lib.library_path.read_bytes())
    api = PyAppleBooks(library_db=store)
    try:
        assert api.get_cached_book_info("A") == {}
    finally:
        api.close()


def test_data_dir_not_named_documents(tmp_path, spy):
    lib = _container_at(tmp_path / "home")
    renamed = lib.data_dir.parent / "Elsewhere"
    renamed.mkdir()
    _assert_not_read(renamed, spy)


def test_default_library_reads_its_container(tmp_path, monkeypatch, fresh_default_library):
    lib = _container_at(tmp_path / "fakehome")
    monkeypatch.setenv("HOME", str(lib.root))
    monkeypatch.delenv("APPLE_BOOKS_DATA_DIR", raising=False)
    assert PyAppleBooks().get_cached_book_info("A")["A"].title == "Hidden"


def test_data_dir_variable(tmp_path, monkeypatch, fresh_default_library):
    lib = _container_at(tmp_path / "other")
    monkeypatch.setenv("APPLE_BOOKS_DATA_DIR", str(lib.data_dir))
    assert PyAppleBooks().get_cached_book_info("A")["A"].title == "Hidden"


@pytest.mark.parametrize("form", ["default", "query_timeout", "annotation_db", "library_db_variable",
                                  "data_dir_variable"])
def test_every_constructor_form_finds_the_container(tmp_path, monkeypatch, fresh_default_library, form):
    """No form needs a store: the fake HOME holds the caches only."""
    lib = _container_at(tmp_path / "fakehome")
    monkeypatch.setenv("HOME", str(lib.root))
    for name in ("APPLE_BOOKS_DATA_DIR", "APPLE_BOOKS_LIBRARY_DB", "APPLE_BOOKS_ANNOTATION_DB"):
        monkeypatch.delenv(name, raising=False)
    if form == "library_db_variable":
        monkeypatch.setenv("APPLE_BOOKS_LIBRARY_DB", str(lib.library_path))
    elif form == "data_dir_variable":
        monkeypatch.setenv("APPLE_BOOKS_DATA_DIR", str(lib.data_dir))
    api = {"query_timeout": lambda: PyAppleBooks(query_timeout=5),
           # The library store is then found in the default container.
           "annotation_db": lambda: PyAppleBooks(annotation_db=tmp_path / "elsewhere.sqlite"),
           }.get(form, PyAppleBooks)()
    try:
        assert api.get_cached_book_info("A")["A"].title == "Hidden"
    finally:
        api.close()


def test_a_changed_folder_replaces_the_index(tmp_path, monkeypatch, fresh_default_library):
    first, second = _container_at(tmp_path / "one"), FixtureLibrary(tmp_path / "two")
    second.add_book_info_cache([{"asset_id": "A", "title": "Second"}])
    monkeypatch.setenv("APPLE_BOOKS_DATA_DIR", str(first.data_dir))
    api = PyAppleBooks()
    assert api.get_cached_book_info("A")["A"].title == "Hidden"
    monkeypatch.setenv("APPLE_BOOKS_DATA_DIR", str(second.data_dir))
    assert api.get_cached_book_info("A")["A"].title == "Second"


def test_no_store_is_needed_or_looked_up(home, spy):
    home.add_book_info_cache([{"asset_id": "A", "title": "x"}])
    api = PyAppleBooks(home.data_dir)  # no BKLibrary or AEAnnotation store at all
    try:
        with _fs_audit.record() as rec:
            assert api.get_cached_book_info("A")["A"].title == "x"
        assert not [e for e in rec.of("os.listdir", "os.scandir", "sqlite3.connect")
                    if "AEEpubInfoSource" not in (e.path or "")]
    finally:
        api.close()


def test_missing_folder_and_home(tmp_path, spy):
    api = PyAppleBooks(FixtureLibrary(tmp_path / "nothing").data_dir)
    try:
        assert api.get_cached_book_info("A") == {}
    finally:
        api.close()


def test_a_path_the_os_refuses_gives_nothing(tmp_path, spy):
    api = PyAppleBooks(tmp_path / "a\x00b" / "Documents")
    try:
        assert api.get_cached_book_info("A") == {}
    finally:
        api.close()
    assert spy.uris == []


def test_folder_derivation_never_raises(monkeypatch):
    class Source:
        def __init__(self, result):
            self.result = result

        def _source(self, kind):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    assert book_info._cache_folder(Source(RuntimeError("no home"))) is None
    assert book_info._cache_folder(Source((None, pathlib.Path("/x/Documents")))) == \
        "/x/Library/Caches/AEEpubInfoSource"
    assert book_info._cache_folder(Source((pathlib.Path("/x/Documents/BKLibrary/s.sqlite"), None))) == \
        "/x/Library/Caches/AEEpubInfoSource"
    assert book_info._cache_folder(Source((pathlib.Path("/x/Documents/s.sqlite"), None))) is None
    from py_apple_books.db import client
    monkeypatch.setattr(client, "default_data_dir", lambda: (_ for _ in ()).throw(RuntimeError("no home")))
    assert book_info._cache_folder(Source((None, None))) is None


# -- drift -----------------------------------------------------------------------------


def test_drifted_and_foreign_files_are_skipped(home, reader, spy):
    folder = home.book_info_dir
    home.add_book_info_cache([{"asset_id": "A", "title": "Good", "author": "x"}], version="v1")
    home.add_book_info_cache([{"asset_id": "A", "title": "no key"}], version="v2",
                             columns=["ZBOOKTITLE", "ZBOOKAUTHOR"])
    home.add_book_info_cache([{"asset_id": "A", "language": "en"}], version="v3",
                             columns=["ZDATABASEKEY", "ZBOOKLANGUAGE"])
    con = sqlite3.connect(folder / cache_name("v4"))
    con.execute("CREATE TABLE OTHER (x)")
    con.commit()
    con.close()
    (folder / cache_name("v5")).write_bytes(b"not a database" * 20)
    (folder / cache_name("v6")).write_bytes(b"")
    (folder / f"{cache_name('v7')}.bak").write_bytes((folder / cache_name("v1")).read_bytes())
    (folder / cache_name("v8")).mkdir()
    (folder / "AEBookInfo-a b.sqlite").write_bytes(b"")
    got = reader.get_cached_book_info("A")
    assert got == {"A": CachedBookInfo("A", "Good", "x", source=cache_name("v1"))}
    # Files the open rule refuses are never connected to; the rest once.
    assert sorted(spy.files()) == [cache_name(v) for v in ("v1", "v2", "v3", "v4")]
    spy.reset()
    assert reader.get_cached_book_info("A") == got and spy.uris == []


def test_a_cache_without_the_author_column(home, reader):
    home.add_book_info_cache([{"asset_id": "A", "title": "Title only"}],
                             columns=["ZDATABASEKEY", "ZBOOKTITLE"])
    assert reader.get_cached_book_info("A") == {"A": CachedBookInfo("A", "Title only", None, source=cache_name(V7))}


def test_a_cache_without_z_pk_order(home, reader):
    _custom_cache(home.book_info_dir, cache_name("v1"),
                  "CREATE TABLE ZAEBOOKINFO (ZID INTEGER, ZDATABASEKEY VARCHAR, ZBOOKTITLE VARCHAR, "
                  "ZBOOKAUTHOR VARCHAR, ZBOOKLANGUAGE VARCHAR, ZPUBLISHERNAME VARCHAR, ZPUBLISHERYEAR VARCHAR)",
                  [(1, "A", "only", None, None, None, None)])
    assert reader.get_cached_book_info("A")["A"].title == "only"


def test_only_the_newest_32_files_are_read(home, reader, spy):
    for n in range(1, 35):
        home.add_book_info_cache([{"asset_id": f"only{n}", "title": f"in {n}"}], version=f"v{n}")
    got = reader.get_cached_book_info([f"only{n}" for n in range(1, 35)])
    assert sorted(got, key=lambda k: int(k[4:])) == [f"only{n}" for n in range(3, 35)]
    assert len(spy.uris) == 32


def test_a_refused_file_is_tried_again_after_the_recheck(home, reader, spy, monkeypatch):
    path = home.book_info_dir / cache_name("v9")
    home.book_info_dir.mkdir(parents=True)
    path.write_bytes(b"not yet" * 20)
    assert reader.get_cached_book_info("A") == {}
    home.add_book_info_cache([{"asset_id": "A", "title": "late"}], version="v1")
    path.unlink()
    staged = home.add_book_info_cache([{"asset_id": "A", "title": "now valid"}], version="v9-staged")
    os.replace(staged, path)
    assert reader.get_cached_book_info("A") == {}  # nothing looked at within the interval
    monkeypatch.setattr(book_info, "BOOK_INFO_RECHECK", 0.0)
    assert reader.get_cached_book_info("A")["A"].title == "now valid"


# -- privacy ---------------------------------------------------------------------------


def test_debug_log_names_files_only(three, reader, caplog, monkeypatch):
    folder = three.book_info_dir
    (folder / cache_name("v99")).write_bytes(b"x" * 200)
    three.add_book_info_cache([{"asset_id": "A", "title": "SECRET TITLE"}], version="v98",
                              columns=["ZBOOKTITLE"])
    locked = three.add_book_info_cache([{"asset_id": "SECRET-ID", "title": "SECRET LOCKED"}], version="v97",
                                       journal_mode="DELETE")
    with caplog.at_level(logging.DEBUG, logger="py_apple_books"):
        with held(locked, "BEGIN EXCLUSIVE"):
            reader.get_cached_book_info(["A", "SECRET-ID", "B"])
    text = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    assert caplog.records  # the skips were logged
    for secret in ("SECRET", "A in 26", "Only B", "B Author", str(folder), str(three.root), "/"):
        assert secret not in text


# -- drift registry and facade -------------------------------------------------------


def test_the_method_runs_in_the_instance_library(three, library):
    db = LibraryDB(data_dir=three.data_dir)
    try:
        with use_library(db):
            inside = PyAppleBooks().get_cached_book_info("A")
    finally:
        db.close()
    assert inside["A"].title == "A in 26.10"
    assert PyAppleBooks().get_cached_book_info("A") == {}  # the session library has no cache
