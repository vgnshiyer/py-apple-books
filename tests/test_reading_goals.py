"""``get_reading_goals`` and the hardened preferences reader
(``py_apple_books._prefs``; 1.11, Tier B, owner question Q6 (a)).
Synthetic preference files only."""

import datetime as dt
import logging
import os
import plistlib
import shutil
import struct
import threading
import time
import types

import pytest

from py_apple_books import PyAppleBooks, _prefs
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.engagement import ReadingGoals
from py_apple_books.exceptions import InvalidArgumentError
from py_apple_books.testing import YEAR_ZERO
from tests import _fs_audit

UTC = dt.timezone.utc


def local(value: dt.datetime) -> dt.datetime:
    """A UTC datetime as naive local time (how the reader returns dates)."""
    return value.replace(tzinfo=UTC).astimezone().replace(tzinfo=None)


@pytest.fixture
def lib(make_library):
    return make_library()


def goals_of(lib, **kwargs):
    with LibraryDB(data_dir=lib.data_dir) as db, use_library(db):
        return PyAppleBooks().get_reading_goals(**kwargs)


class TestRead:
    def test_defaults(self, lib):
        path = lib.write_prefs(finished={"ASSET-B": dt.datetime(2026, 6, 20, 12), "ASSET-A": dt.datetime(2026, 2, 1)})
        with pytest.raises(Exception):
            plistlib.loads(path.read_bytes())   # Books' year-0 date
        goals, reason = _prefs._read_goals(path)
        assert reason == "ok"
        assert goals.books_per_year == 3 and goals.daily_goal_seconds == 300.0 and goals.daily_goal_minutes == 5.0
        assert goals.apple_current_streak == 0
        assert goals.books_goal_set == local(dt.datetime(2026, 1, 2, 9))
        assert goals.daily_goal_set == local(dt.datetime(2026, 1, 3, 9))
        assert goals.finished_assets == (("ASSET-A", local(dt.datetime(2026, 2, 1))),
                                         ("ASSET-B", local(dt.datetime(2026, 6, 20, 12))))
        assert goals.books_finished_in(2026) == 2 and goals.books_finished_in(2025) == 0
        assert abs((goals.modified - dt.datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()) < 1
        assert goals_of(lib) == goals

    def test_xml(self, lib):
        path = lib.write_prefs(fmt="xml", books_goal=12)
        goals, reason = _prefs._read_goals(path)
        assert reason == "ok" and goals.books_per_year == 12

    def test_missing_keys_and_wrong_types(self, lib):
        path = lib.write_prefs(books_goal="8", daily_goal_seconds=None, current_streak=None, finished=None,
                               extra={"ReadingHistory.CurrentStreak": {"n": 1}})
        goals, reason = _prefs._read_goals(path)
        assert reason == "ok"
        assert goals == ReadingGoals(books_goal_set=goals.books_goal_set, modified=goals.modified)
        assert goals.books_goal_set is not None
        path = lib.write_prefs(books_goal=True, daily_goal_seconds=float("nan"), current_streak=-1,
                               extra={"BKFinishedAssetsCache": ["not", "a dict"]})
        goals, _ = _prefs._read_goals(path)
        assert (goals.books_per_year, goals.daily_goal_seconds, goals.apple_current_streak,
                goals.finished_assets) == (None, None, None, ())

    def test_unreadable_dates_are_none(self, lib):
        path = lib.write_prefs(books_goal_set=float("nan"), daily_goal_set=YEAR_ZERO,
                               finished={"OK": dt.datetime(2026, 3, 1), "NAN": float("nan"), "ZERO": YEAR_ZERO,
                                         "FAR": 1e300})
        goals, reason = _prefs._read_goals(path)
        assert reason == "ok"
        assert goals.books_goal_set is None and goals.daily_goal_set is None
        assert goals.books_per_year == 3
        assert goals.finished_assets == (("OK", local(dt.datetime(2026, 3, 1))), ("FAR", None), ("NAN", None),
                                         ("ZERO", None))
        assert goals.books_finished_in(2026) == 1

    def test_books_finished_in_needs_an_int(self):
        with pytest.raises(InvalidArgumentError):
            ReadingGoals().books_finished_in("2026")
        with pytest.raises(InvalidArgumentError):
            ReadingGoals().books_finished_in(True)


class TestReasons:
    def test_no_path(self):
        assert _prefs._read_goals(None) == (None, "no_path")

    def test_missing(self, tmp_path):
        assert _prefs._read_goals(tmp_path / "nope.plist") == (None, "missing")
        assert _prefs._read_goals(tmp_path / "nope" / "x.plist") == (None, "missing")

    def test_directory_fifo_symlink(self, lib, tmp_path):
        real = lib.write_prefs()
        assert _prefs._read_goals(tmp_path) == (None, "not_regular")
        link = tmp_path / "link.plist"
        link.symlink_to(real)
        assert _prefs._read_goals(link) == (None, "not_regular")
        fifo = tmp_path / "fifo.plist"
        os.mkfifo(fifo)
        start = time.monotonic()
        assert _prefs._read_goals(fifo) == (None, "not_regular")
        assert time.monotonic() - start < 1

    def test_too_large(self, tmp_path):
        big = tmp_path / "big.plist"
        big.write_bytes(b"\x00" * (9 * 1024 * 1024))   # written, not sparse (a sparse file looks dataless)
        assert _prefs._read_goals(big) == (None, "too_large")

    @pytest.mark.parametrize("path", [
        "/home/someone/Library/Mobile Documents/com~apple~CloudDocs/com.apple.iBooksX.plist",
        "/home/someone/library/MOBILE DOCUMENTS/x.plist",
        "/home/someone/Library/CloudStorage/Provider/x.plist",
        "relative/Mobile Documents/x.plist",
    ])
    def test_icloud_path_is_never_touched(self, monkeypatch, path):
        def refuse(*args, **kwargs):
            raise AssertionError("the file system was called")

        for name in ("lstat", "stat", "open", "fstat"):
            monkeypatch.setattr(os, name, refuse)
        assert _prefs._read_goals(path) == (None, "icloud_path")

    @pytest.mark.parametrize("data", [
        b"", b"bplist00", b"garbage" * 10, b"bplist00" + b"\x00" * 40,
        b"bplist00\xd0\x08" + struct.pack(">6xBBQQQ", 1, 1, 1, 0, 9),           # an empty dict: well formed
        b"bplist00\x08" + struct.pack(">6xBBQQQ", 1, 1, 1, 0, 1000),              # table out of bounds
        b"bplist00\x08" + struct.pack(">6xBBQQQ", 3, 1, 1, 0, 9),                 # bad offset size
        b"<?xml version='1.0'?><plist><dict><key>a</key><date>junk</date></dict></plist>",
    ])
    def test_unparseable(self, tmp_path, data):
        path = tmp_path / "p.plist"
        path.write_bytes(data)
        goals, reason = _prefs._read_goals(path)
        if data.startswith(b"bplist00\xd0"):
            # A well-formed empty dict (the offset table points at 0xd0).
            assert reason == "ok" and goals == ReadingGoals(modified=goals.modified)
        else:
            assert (goals, reason) == (None, "unparseable")

    def test_truncated(self, lib, tmp_path):
        data = lib.write_prefs().read_bytes()
        path = tmp_path / "p.plist"
        path.write_bytes(data[:-5])
        assert _prefs._read_goals(path) == (None, "unparseable")

    def test_deep_nesting(self, tmp_path):
        count = 20_000
        objects = [b"\xa1" + (i + 1).to_bytes(2, "big") for i in range(count)] + [b"\xa0"]
        body, offsets = b"bplist00", []
        for obj in objects:
            offsets.append(len(body))
            body += obj
        table = len(body)
        body += b"".join(o.to_bytes(4, "big") for o in offsets)
        body += struct.pack(">6xBBQQQ", 4, 2, len(objects), 0, table)
        path = tmp_path / "deep.plist"
        path.write_bytes(body)
        assert _prefs._read_goals(path) == (None, "unparseable")

    def test_not_a_dict(self, tmp_path):
        cyclic = b"bplist00" + b"\xa1\x00\x00"
        table = len(cyclic)
        cyclic += (8).to_bytes(1, "big") + struct.pack(">6xBBQQQ", 1, 2, 1, 0, table)
        path = tmp_path / "cyc.plist"
        path.write_bytes(cyclic)
        assert _prefs._read_goals(path) == (None, "not_dict")
        path.write_bytes(plistlib.dumps([1, 2], fmt=plistlib.FMT_BINARY))
        assert _prefs._read_goals(path) == (None, "not_dict")


def flagged(st, flags):
    """A stat result like ``st`` with ``st_flags`` set."""
    fields = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
    fields["st_flags"] = flags
    return types.SimpleNamespace(**fields)


class TestDataless:
    def test_dataless_by_lstat_is_never_opened(self, lib, monkeypatch):
        path = lib.write_prefs()
        real_lstat = os.lstat
        monkeypatch.setattr(os, "lstat", lambda p, **k: flagged(real_lstat(p, **k), 0x40000000))
        monkeypatch.setattr(os, "open", lambda *a, **k: pytest.fail("opened"))
        assert _prefs._read_goals(path) == (None, "dataless")

    def test_dataless_by_fstat_is_never_read(self, lib, monkeypatch):
        path = lib.write_prefs()
        real_fstat = os.fstat
        monkeypatch.setattr(os, "fstat", lambda fd: flagged(real_fstat(fd), 0x40000000))
        monkeypatch.setattr(os, "read", lambda *a: pytest.fail("read"))
        assert _prefs._read_goals(path) == (None, "dataless")

    def test_another_file_after_open(self, lib, monkeypatch):
        path = lib.write_prefs()
        real_fstat = os.fstat

        def swapped(fd):
            st = flagged(real_fstat(fd), 0)
            st.st_ino += 1
            return st

        monkeypatch.setattr(os, "fstat", swapped)
        monkeypatch.setattr(os, "read", lambda *a: pytest.fail("read"))
        assert _prefs._read_goals(path) == (None, "not_regular")

    def test_reads_run_with_materialization_off(self, lib, monkeypatch):
        """Every file call of the reader runs with the thread's
        materialization policy off, restored afterwards (a fake policy
        records the value at each call)."""
        from py_apple_books import _icloud

        path = lib.write_prefs()
        state = {"value": 0}

        def get(kind, scope):
            return state["value"]

        def set_(kind, scope, value):
            state["value"] = value
            return 0

        monkeypatch.setattr(_icloud, "_policy_loaded", True)
        monkeypatch.setattr(_icloud, "_policy_fns", (get, set_))
        seen = []
        for name in ("lstat", "open", "fstat", "read"):
            real = getattr(os, name)

            def spy(*args, _real=real, _name=name, **kwargs):
                seen.append((_name, state["value"]))
                return _real(*args, **kwargs)

            monkeypatch.setattr(os, name, spy)
        assert _prefs._read_goals(path)[1] == "ok"
        assert {name for name, _ in seen} == {"lstat", "open", "fstat", "read"}
        assert all(value == _icloud.IOPOL_MATERIALIZE_DATALESS_FILES_OFF for _, value in seen)
        assert state["value"] == 0


class TestPath:
    def test_from_the_data_dir(self, lib):
        lib.write_prefs(books_goal=7)
        assert goals_of(lib).books_per_year == 7
        assert _prefs.prefs_path_for(None, lib.data_dir) == lib.prefs_path

    def test_case_variants(self, lib, tmp_path):
        lib.write_prefs(books_goal=5)
        docs = tmp_path / "x" / "documents"
        assert _prefs.prefs_path_for(None, docs) == docs.parent / _prefs.PREFS_FILE
        store = tmp_path / "x" / "DOCUMENTS" / "bklibrary" / "BKLibrary-1.sqlite"
        assert _prefs.prefs_path_for(store, None) == store.parent.parent.parent / _prefs.PREFS_FILE

    def test_from_the_library_file(self, lib):
        lib.write_prefs(books_goal=9)
        with LibraryDB(library_db=lib.library_path, annotation_db=lib.annotation_path) as db, use_library(db):
            assert PyAppleBooks().get_reading_goals().books_per_year == 9

    def test_a_copied_store_has_no_preferences(self, lib, tmp_path):
        lib.write_prefs()
        copy = tmp_path / "copy.sqlite"
        shutil.copy(lib.library_path, copy)
        assert _prefs.prefs_path_for(copy, lib.data_dir) is None
        with LibraryDB(library_db=copy, annotation_db=lib.annotation_path) as db, use_library(db):
            assert PyAppleBooks().get_reading_goals() is None

    def test_a_data_dir_outside_a_container(self, tmp_path):
        assert _prefs.prefs_path_for(None, tmp_path / "Books") is None
        api = PyAppleBooks(data_dir=tmp_path / "Books")
        try:
            assert api.get_reading_goals() is None   # no LibraryNotFoundError: SQLite isn't opened
        finally:
            api.close()

    def test_prefs_path_overrides(self, lib, tmp_path):
        lib.write_prefs(books_goal=4)
        other = tmp_path / "other.plist"
        shutil.copy(lib.prefs_path, other)
        lib.write_prefs(books_goal=6)
        assert goals_of(lib, prefs_path=other).books_per_year == 4
        assert goals_of(lib, prefs_path=str(other)).books_per_year == 4
        assert goals_of(lib, prefs_path=os.fsencode(other)).books_per_year == 4
        with pytest.raises(InvalidArgumentError, match="prefs_path must be a path"):
            goals_of(lib, prefs_path=5)

    def test_default_library(self, library):
        from tests.conftest import FIXTURE_HOME

        assert _prefs.default_prefs_path().resolve().is_relative_to(FIXTURE_HOME.resolve())
        assert PyAppleBooks().get_reading_goals() is None   # the session library has no preferences file
        library.write_prefs(books_goal=11)
        try:
            assert PyAppleBooks().get_reading_goals().books_per_year == 11
        finally:
            library.prefs_path.unlink()

    def test_no_database_no_process(self, lib):
        lib.write_prefs()
        api = PyAppleBooks(data_dir=lib.data_dir)
        try:
            with _fs_audit.record() as rec:
                assert api.get_reading_goals() is not None
            assert not rec.of("sqlite3.connect", "subprocess.Popen", "os.posix_spawn", "os.scandir", "os.listdir")
            assert rec.paths("open") == [str(lib.prefs_path.resolve())]
        finally:
            api.close()


def test_logs_only_the_reason(lib, caplog):
    path = lib.write_prefs(finished={"SECRET-ASSET": dt.datetime(2026, 1, 1)})
    with caplog.at_level(logging.DEBUG):
        _prefs._read_goals(path)
        _prefs._read_goals(path.parent / "missing.plist")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "reading goals: ok" in text and "reading goals: missing" in text
    assert "SECRET" not in text and str(path.parent) not in text and "/" not in text


def test_threads(lib):
    path = lib.write_prefs(finished={"A": dt.datetime(2026, 1, 1)})
    expected = _prefs._read_goals(path)
    results = []

    def run():
        results.append(_prefs._read_goals(path))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [expected] * 8


def test_frozen_type():
    goals = ReadingGoals(books_per_year=3)
    with pytest.raises(AttributeError):
        goals.books_per_year = 4
    assert hash(goals) == hash(ReadingGoals(books_per_year=3))
    assert ReadingGoals().daily_goal_minutes is None
