"""``PyAppleBooks.get_books_by_subject`` (Tier B): ``get_books_by_genre``
plus the subjects in the books' package documents. Synthetic libraries
and bundles only."""

import os
import pathlib
import random
import threading
import time

import pytest

from py_apple_books import PyAppleBooks, _icloud, _opf
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import InvalidArgumentError, QueryTimeoutError
from py_apple_books.testing import STORE_SERIES, write_epub_bundle

CONTAINER = ('<?xml version="1.0"?><container version="1.0" '
             'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
             '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
             '</rootfiles></container>')


def minimal_bundle(path: pathlib.Path, subjects) -> pathlib.Path:
    """container.xml and a package document only."""
    (path / "META-INF").mkdir(parents=True)
    (path / "META-INF" / "container.xml").write_text(CONTAINER)
    dc = "".join(f"<dc:subject>{s}</dc:subject>" for s in subjects)
    (path / "content.opf").write_text(
        '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
        f'xmlns:dc="http://purl.org/dc/elements/1.1/">{dc}</metadata><manifest/></package>')
    return path


class Lib:
    def __init__(self, fx, root: pathlib.Path):
        self.fx = fx
        self.root = root
        self.api = PyAppleBooks(data_dir=fx.data_dir)
        self.n = 0

    def book(self, title="Book", *, genre=None, subjects=None, path=None, **kwargs) -> int:
        """A book; with ``subjects`` (a list), an EPUB bundle carrying them."""
        self.n += 1
        if subjects is not None:
            path = write_epub_bundle(self.root / f"b{self.n}.epub", [("c1", "<p>x</p>")],
                                     metadata_xml="".join(f"<dc:subject>{s}</dc:subject>" for s in subjects))
        return self.fx.add_book(title, genre=genre, path=path, **kwargs)["id"]

    def ids(self, *args, **kwargs) -> list:
        return [b.id for b in self.api.get_books_by_subject(*args, **kwargs)]

    def genre_ids(self, *args, **kwargs) -> list:
        return [b.id for b in self.api.get_books_by_genre(*args, **kwargs)]


@pytest.fixture
def lib(make_library, tmp_path):
    made = Lib(make_library(), tmp_path)
    yield made
    made.api.close()


@pytest.fixture
def mixed(lib):
    """Genre only, file only, both, neither, and a book without a file."""
    ids = {
        "genre": lib.book(genre="Philosophie"),
        "file": lib.book(subjects=["Gödel, Escher", "Logic and Proof"]),
        "both": lib.book(genre="Philosophy of Mind", subjects=["Philosophy of mind"]),
        "neither": lib.book(genre="Cooking", subjects=["Baking"]),
        "nofile": lib.book(genre="History"),
        "empty": lib.book(subjects=[]),
    }
    return ids


class TestMatching:
    def test_genre_file_and_both_once(self, lib, mixed):
        assert lib.ids("philosoph") == [mixed["genre"], mixed["both"]]
        assert lib.ids("PHILOSOPHIE") == [mixed["genre"]]
        assert lib.ids("godel") == [mixed["file"]]
        assert lib.ids("proof") == [mixed["file"]]
        assert lib.ids("of mind") == [mixed["both"]]
        assert lib.ids("nothing") == []

    def test_empty_needle(self, lib, mixed):
        assert lib.ids("", read_files=False) == lib.genre_ids("") == [
            mixed["genre"], mixed["both"], mixed["neither"], mixed["nofile"]]
        assert lib.ids("") == sorted([*lib.genre_ids(""), mixed["file"]])

    def test_whitespace_needle(self, lib, mixed):
        assert lib.ids(" ", read_files=False) == lib.genre_ids(" ") == [mixed["both"]]
        assert lib.ids(" ") == [mixed["file"], mixed["both"]]

    @pytest.mark.parametrize("needle", ["́", "​", "­"])
    def test_needle_folding_to_nothing(self, lib, mixed, needle):
        assert lib.ids(needle) == lib.genre_ids(needle) == []

    def test_non_text_needles_follow_the_genre_search(self, lib):
        lib.book(genre="None of these")
        lib.book(genre="Volume 5")
        for needle in (None, 5):
            assert lib.ids(needle) == lib.genre_ids(needle)

    def test_superset_of_the_genre_search(self, lib, mixed):
        words = ["phil", "o", "of", "", " ", "x", "Cook", "his", "ГÖ", "mind "]
        for word in words:
            assert set(lib.genre_ids(word)) <= set(lib.ids(word)), word


class TestScope:
    def test_store_series_rows(self, lib):
        unowned = lib.book(genre="Saga", data_source=STORE_SERIES)
        owned = lib.book(genre="Saga")
        assert lib.ids("saga") == [owned]
        assert lib.ids("saga", include_store_series=True) == [unowned, owned]
        assert lib.ids("saga", include_store_series=True) == lib.genre_ids("saga", include_store_series=True)

    def test_cloud_only_and_pdf_are_never_touched(self, lib, monkeypatch):
        cloud = lib.book(subjects=["Hidden"], state=3)
        pdf_path = lib.root / "doc.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        lib.book(path=pdf_path)
        shown = lib.book(subjects=["Hidden"])
        cloud_path = lib.api.get_book_by_id(cloud).path
        stats = []
        real_lstat, real_stat = os.lstat, os.stat
        monkeypatch.setattr(_icloud, "lstat", lambda p, *, dir_fd=None: stats.append(os.fspath(p)) or real_lstat(
            p, dir_fd=dir_fd))
        monkeypatch.setattr(_icloud, "stat", lambda p: stats.append(os.fspath(p)) or real_stat(p))
        assert lib.ids("hidden") == [shown]
        assert str(cloud_path) not in stats and str(pdf_path) not in stats

    def test_dataless_package_document_is_skipped_unread(self, lib, monkeypatch):
        evicted = lib.book(subjects=["Evicted"])
        real_lstat = os.lstat
        opened = []
        real_open = os.open
        monkeypatch.setattr(_icloud, "lstat", lambda p, *, dir_fd=None: _blocks0(real_lstat(p, dir_fd=dir_fd))
                            if os.fspath(p) == "content.opf" else real_lstat(p, dir_fd=dir_fd))
        monkeypatch.setattr(os, "open", lambda p, *a, **k: opened.append(os.fspath(p)) or real_open(p, *a, **k))
        assert lib.ids("evicted") == []
        assert "content.opf" not in opened and evicted

    def test_is_downloaded_is_not_called(self, lib, monkeypatch):
        from py_apple_books import content

        lib.book(subjects=["Local"])
        monkeypatch.setattr(content, "is_downloaded", lambda path: pytest.fail("is_downloaded called"))
        assert len(lib.ids("local")) == 1


def _blocks0(st):
    from types import SimpleNamespace

    values = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
    values["st_blocks"] = 0
    return SimpleNamespace(**values)


class TestPaging:
    @pytest.fixture
    def many(self, lib):
        rnd = random.Random(7)
        made = []
        for i in range(30):
            if i % 3 == 0:
                made.append(lib.book(f"T{rnd.randint(0, 9)}", genre="Topic"))
            else:
                made.append(lib.book(f"T{rnd.randint(0, 9)}", subjects=["Topic"] if i % 3 == 1 else ["Other"]))
        return made

    @pytest.mark.parametrize("order_by", [None, "title", "-title"])
    def test_pages(self, lib, many, order_by):
        whole = lib.ids("topic", order_by=order_by)
        assert len(whole) == 20
        pages = [lib.ids("topic", order_by=order_by, offset=start, limit=7) for start in range(0, 21, 7)]
        assert sum(pages, []) == whole
        if order_by is None:
            assert whole == sorted(whole)

    def test_count_and_slice_run_one_statement(self, lib, many, sql_trace):
        list(lib.api.list_books())  # schema read
        sql_trace.clear()
        result = lib.api.get_books_by_subject("topic", order_by="title")
        assert result.count() == 20 and len(result[5:10]) == 5 and len(list(result)) == 20
        assert len(sql_trace) == 1
        assert result.count_by("genre") == {"Topic": 10, None: 10}

    @pytest.mark.parametrize("needle", ["topic", "", " ", "t", "x"])
    @pytest.mark.parametrize("order_by", [None, "title", "-id"])
    def test_without_files_is_the_genre_search(self, lib, many, needle, order_by):
        assert lib.ids(needle, order_by=order_by, read_files=False) == lib.genre_ids(needle, order_by=order_by)
        assert lib.ids(needle, 3, order_by, offset=2, read_files=False) == lib.genre_ids(
            needle, 3, order_by, offset=2)

    @pytest.mark.parametrize("kwargs", [dict(limit=0), dict(limit=-1), dict(limit=True), dict(offset=-1),
                                        dict(limit=1.5)])
    @pytest.mark.parametrize("read_files", [True, False])
    def test_strict_limits(self, lib, kwargs, read_files):
        with pytest.raises(InvalidArgumentError):
            lib.api.get_books_by_subject("x", read_files=read_files, **kwargs)

    def test_unknown_order_field(self, lib):
        from py_apple_books.exceptions import UnknownFieldError

        with pytest.raises(UnknownFieldError):
            lib.api.get_books_by_subject("x", order_by="nope")


class TestDeadline:
    def test_one_deadline_for_the_scan(self, lib, monkeypatch):
        for i in range(50):
            lib.fx.add_book(f"B{i}", path=lib.root / f"missing-{i}.epub")
        clock = [1000.0]
        checks = []
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])

        def slow(path):
            checks.append(path)
            clock[0] += 1.0
            return None

        monkeypatch.setattr(_opf, "read_subjects", slow)
        with lib.api.query_deadline(5):
            with pytest.raises(QueryTimeoutError) as exc:
                lib.api.get_books_by_subject("x")
        assert len(checks) <= 6 and exc.value.timeout == 5
        assert "missing" not in str(exc.value) and str(lib.root) not in str(exc.value)

    def test_zero_deadline_matches_the_genre_search(self, lib):
        lib.book(genre="G")

        def outcome(call):
            try:
                return [b.id for b in call()]
            except QueryTimeoutError:
                return "timeout"

        with lib.api.query_deadline(0):
            assert outcome(lambda: lib.api.get_books_by_subject("g")) == outcome(
                lambda: lib.api.get_books_by_genre("g"))

    def test_connection_is_released_before_file_reads(self, lib, monkeypatch):
        lib.book(subjects=["S"])
        db = LibraryDB(data_dir=lib.fx.data_dir, max_connections=1, query_timeout=10)
        started, release = threading.Event(), threading.Event()
        real = _opf.read_subjects

        def blocking(path):
            started.set()
            assert release.wait(10)
            return real(path)

        monkeypatch.setattr(_opf, "read_subjects", blocking)
        results = {}

        def scan():
            with use_library(db):
                results["scan"] = [b.id for b in PyAppleBooks().get_books_by_subject("s")]

        t = threading.Thread(target=scan)
        t.start()
        try:
            assert started.wait(10)
            with use_library(db):
                results["list"] = [b.id for b in PyAppleBooks().list_books()]
        finally:
            release.set()
            t.join(10)
            db.close()
        assert results == {"list": [1], "scan": [1]}


class TestScale:
    def test_two_thousand_bundles(self, lib, monkeypatch, tmp_path):
        paths = []
        for i in range(2000):
            path = minimal_bundle(tmp_path / f"s{i}.epub", [f"Subject {i % 7}", "Common"])
            paths.append(path)
        lib.fx._insert_many("library", "ZBKLIBRARYASSET", "BKLibraryAsset", 2000,
                            lambda pk: lib.fx._book_row(pk, f"Book {pk}", "Author", path=paths[pk - 1]))
        reads = []
        real = _icloud.read_local
        monkeypatch.setattr(_icloud, "read_local", lambda root, rel, **kw: reads.append(rel) or real(root, rel, **kw))
        first = lib.ids("subject 3")
        assert len(first) == len([i for i in range(2000) if i % 7 == 3])
        assert reads.count("content.opf") == 2000
        reads.clear()
        # CPU time, not wall time: the bound is on the work the warm scan
        # does (two gated stats per bundle, no read), which load on the
        # machine doesn't change.
        started = time.process_time()
        assert lib.ids("subject 3") == first
        assert reads == [] and time.process_time() - started < 3
        monkeypatch.setattr(_opf._subject_index, "max_entries", 100)
        monkeypatch.setattr(_opf._metadata_cache, "max_entries", 100)
        from py_apple_books.content import clear_content_cache

        clear_content_cache()
        assert lib.ids("common") == list(range(1, 2001))
        assert lib.ids("subject 3") == first


def test_threads(lib, mixed):
    expected = lib.ids("phil")
    errors, barrier = [], threading.Barrier(8)

    def work():
        barrier.wait()
        for _ in range(10):
            if lib.ids("phil") != expected:
                errors.append(1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
