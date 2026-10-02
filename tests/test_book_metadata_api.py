"""``PyAppleBooks.get_book_metadata``: gates, merge, inputs, drift.

Every library and bundle is synthetic. The bundles are real folders in
``tmp_path`` read with the real ``is_downloaded`` and file flags, except
where a test fakes an iCloud placeholder by patching ``_icloud.lstat`` /
``_icloud.stat``.
"""

import datetime as dt
import logging
import os
import pathlib
import subprocess
import threading
import time

import pytest

from py_apple_books import PyAppleBooks, _icloud
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import BookNotFoundError
from py_apple_books.models import BookMetadata, MetadataFileState
from py_apple_books.testing import write_epub, write_epub_bundle
from tests import _fs_audit

UTC = dt.timezone.utc
S = MetadataFileState
FULL = ('<dc:publisher>Synthetic Press</dc:publisher><dc:date>2001-02-03</dc:date>'
        '<dc:identifier opf:scheme="ISBN">978-0-306-40615-7</dc:identifier>'
        '<dc:subject>Fiction</dc:subject><dc:subject>Logic</dc:subject>'
        '<dc:description>From the book.</dc:description>'
        '<meta name="calibre:series" content="Cycle"/><meta name="calibre:series_index" content="2"/>')


@pytest.fixture
def lib(lib_db):
    """``lib.fixture`` (a FixtureLibrary) and ``lib.api`` over it."""
    lib_db.api = PyAppleBooks(data_dir=lib_db.fixture.data_dir)
    yield lib_db
    lib_db.api.close()


def refresh(lib) -> None:
    """Make ``lib.api`` see a changed schema at once."""
    lib.api._PyAppleBooks__library.invalidate_schema()


def epub(root: pathlib.Path, name: str = "book.epub", metadata: str = FULL, **kwargs) -> pathlib.Path:
    return write_epub_bundle(root / name, [("c1", "<p>Text.</p>")], metadata_xml=metadata, **kwargs)


def add(lib, path=None, **kwargs) -> int:
    return lib.fixture.add_book("Synthetic Book", path=path, **kwargs)["id"]


def set_columns(lib, book_id: int, **columns) -> None:
    assignments = ", ".join(f"{name} = ?" for name in columns)
    lib.fixture.execute("library", f"UPDATE ZBKLIBRARYASSET SET {assignments} WHERE Z_PK = ?",
                        (*columns.values(), book_id))


class TestRead:
    def test_real_bundle_through_the_real_gates(self, lib, tmp_path):
        """No patching: the bundle's folders report 0 blocks on APFS and
        must still read (directories are judged by SF_DATALESS only)."""
        book = add(lib, epub(tmp_path))
        got = lib.api.get_book_metadata(book)
        assert got == BookMetadata(
            book_id=book, language="en", publisher="Synthetic Press", published="2001-02-03", year=2001,
            isbn="9780306406157", subjects=("Fiction", "Logic"), description="From the book.",
            series_title="Cycle", series_sequence=2.0, file_state=S.READ,
            book_file_fields=frozenset({"language", "publisher", "published", "year", "isbn", "subjects",
                                        "description", "series_title", "series_sequence"}))

    def test_library_values_first(self, lib, tmp_path):
        book = add(lib, epub(tmp_path), genre="fiction")
        set_columns(lib, book, ZLANGUAGE="fr_ca", ZBOOKDESCRIPTION="<p>From the &amp; store.</p>", ZYEAR="1999")
        got = lib.api.get_book_metadata(book)
        assert (got.language, got.description, got.published, got.year, got.subjects) == (
            "fr-CA", "From the & store.", "1999", 1999, ("fiction", "Logic"))
        assert got.book_file_fields == {"publisher", "isbn", "subjects", "series_title", "series_sequence"}

    def test_book_fills_the_gaps(self, lib, tmp_path):
        book = add(lib, epub(tmp_path, metadata="<dc:subject>History</dc:subject>"), genre="Fiction")
        set_columns(lib, book, ZBOOKDESCRIPTION="  ")
        got = lib.api.get_book_metadata(book)
        assert got.subjects == ("Fiction", "History") and got.description is None
        assert got.book_file_fields == {"language", "subjects"}

    def test_genre_is_kept_as_books_records_it(self, lib, tmp_path):
        path = epub(tmp_path, metadata="<dc:subject>www.example.com</dc:subject><dc:subject>B</dc:subject>")
        got = lib.api.get_book_metadata(add(lib, path, genre=" www.example.org "))
        assert got.subjects == ("www.example.org", "B")
        assert lib.api.get_book_metadata(add(lib, path, genre="WWW.EXAMPLE.COM")).subjects == (
            "WWW.EXAMPLE.COM", "B")

    def test_genre_only_is_not_from_the_file(self, lib, tmp_path):
        book = add(lib, epub(tmp_path, metadata="<dc:subject>FICTION</dc:subject>"), genre="Fiction")
        got = lib.api.get_book_metadata(book)
        assert got.subjects == ("Fiction",) and "subjects" not in got.book_file_fields

    def test_release_date_is_the_utc_day(self, lib, tmp_path, monkeypatch):
        book = add(lib, epub(tmp_path))
        midnight_utc = dt.datetime(2019, 1, 1, tzinfo=UTC).timestamp() - 978307200
        set_columns(lib, book, ZRELEASEDATE=midnight_utc, ZYEAR="1990")
        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        try:
            got = lib.api.get_book_metadata(book)
            assert lib.api.get_book_by_id(book).release_date.day == 31  # local: the day before
        finally:
            monkeypatch.undo()
            time.tzset()
        assert (got.published, got.year) == ("2019-01-01", 2019)
        assert not {"published", "year"} & got.book_file_fields

    def test_implausible_library_year_falls_through(self, lib, tmp_path):
        book = add(lib, epub(tmp_path))
        set_columns(lib, book, ZYEAR="0101")
        got = lib.api.get_book_metadata(book)
        assert (got.published, got.year) == ("2001-02-03", 2001)

    def test_cover_href(self, lib, tmp_path):
        path = epub(tmp_path, extra_items=[("cov", "images/c.jpg", "image/jpeg", b"\xff", "cover-image")])
        assert lib.api.get_book_metadata(add(lib, path)).cover_href == "OEBPS/images/c.jpg"

    def test_fairplay_bundle_is_read(self, lib, tmp_path):
        path = epub(tmp_path)
        (path / "META-INF" / "sinf.xml").write_text("<sinf/>")
        assert lib.api.get_book_metadata(add(lib, path)).file_state is S.READ

    def test_write_epub_bundle(self, lib, tmp_path):
        path = write_epub(tmp_path / "demo.epub", "Demo")
        got = lib.api.get_book_metadata(add(lib, path))
        assert got.file_state is S.READ and got.language == "en"

    def test_only_the_two_files_are_opened(self, lib, tmp_path):
        path = epub(tmp_path)
        book = add(lib, path)
        with _fs_audit.record() as rec:
            assert lib.api.get_book_metadata(book).file_state is S.READ
        opened = [os.fsdecode(e.args[0]) for e in rec.of("open") if isinstance(e.args[0], (str, bytes))]
        names = {os.path.basename(name.rstrip("/")) for name in opened}
        assert opened and names <= {path.name, "META-INF", "container.xml", "OEBPS", "content.opf"}, names
        assert not rec.under(path, "os.scandir", "os.listdir")
        # is_downloaded's du is the one process (owner decision Q3 (b)).
        assert all("du" in (e.path or "du") for e in rec.of(*_fs_audit.PROCESS_EVENTS))


class TestStates:
    def test_no_file(self, lib):
        got = lib.api.get_book_metadata(add(lib, None, genre="Poetry"))
        assert got.file_state is S.NO_FILE and got.subjects == ("Poetry",)

    @pytest.mark.parametrize("name", ["book.pdf", "folder"])
    def test_not_epub_by_name(self, lib, tmp_path, name):
        path = tmp_path / name
        path.mkdir()
        assert lib.api.get_book_metadata(add(lib, path)).file_state is S.NOT_EPUB

    def test_zipped_epub_file(self, lib, tmp_path):
        path = tmp_path / "book.epub"
        path.write_bytes(b"PK\x03\x04")
        assert lib.api.get_book_metadata(add(lib, path)).file_state is S.NOT_EPUB

    def test_cloud_only_touches_nothing(self, lib, tmp_path, monkeypatch):
        path = epub(tmp_path)
        book = add(lib, path, state=3)
        stats = []
        monkeypatch.setattr(_icloud, "lstat", lambda p, *, dir_fd=None: stats.append(p) or os.lstat(p))
        monkeypatch.setattr(_icloud, "stat", lambda p: stats.append(p) or os.stat(p))
        with _fs_audit.record() as rec:
            got = lib.api.get_book_metadata(book)
        assert got.file_state is S.NOT_DOWNLOADED and got.language is None
        assert not stats and not rec.under(path) and not rec.of(*_fs_audit.PROCESS_EVENTS)

    def test_not_requested_touches_nothing(self, lib, tmp_path):
        path = epub(tmp_path)
        book = add(lib, path, genre="Fiction")
        with _fs_audit.record() as rec:
            got = lib.api.get_book_metadata(book, read_files=False)
        assert got == BookMetadata(book_id=book, subjects=("Fiction",), file_state=S.NOT_REQUESTED)
        assert not rec.under(path) and not rec.of(*_fs_audit.PROCESS_EVENTS)

    def test_is_downloaded_false(self, lib, tmp_path, monkeypatch):
        from py_apple_books import content

        monkeypatch.setattr(content, "is_downloaded", lambda path: False)
        assert lib.api.get_book_metadata(add(lib, epub(tmp_path))).file_state is S.NOT_DOWNLOADED

    def test_stub_next_to_a_missing_bundle(self, lib, tmp_path):
        (tmp_path / ".book.epub.icloud").write_bytes(b"stub")
        assert lib.api.get_book_metadata(add(lib, tmp_path / "book.epub")).file_state is S.NOT_DOWNLOADED

    def test_missing_bundle(self, lib, tmp_path):
        assert lib.api.get_book_metadata(add(lib, tmp_path / "gone.epub")).file_state is S.UNREADABLE

    def test_dataless_bundle(self, lib, tmp_path, monkeypatch):
        path = epub(tmp_path)
        book = add(lib, path)
        looked_up = []
        real = os.lstat

        def lstat(p, *, dir_fd=None):
            looked_up.append(os.fspath(p))
            st = real(p, dir_fd=dir_fd)
            if os.fspath(p) == str(path):
                return _fake(st, st_flags=_icloud.SF_DATALESS)
            return st

        monkeypatch.setattr(_icloud, "lstat", lstat)
        with _fs_audit.record() as rec:
            assert lib.api.get_book_metadata(book).file_state is S.NOT_DOWNLOADED
        assert looked_up == [str(path)] and not rec.of(*_fs_audit.PROCESS_EVENTS)

    def test_dataless_meta_inf(self, lib, tmp_path, monkeypatch):
        book = add(lib, epub(tmp_path))
        seen = []
        real = os.lstat

        def lstat(p, *, dir_fd=None):
            seen.append(os.fspath(p))
            st = real(p, dir_fd=dir_fd)
            return _fake(st, st_flags=_icloud.SF_DATALESS) if os.fspath(p) == "META-INF" else st

        monkeypatch.setattr(_icloud, "lstat", lstat)
        assert lib.api.get_book_metadata(book).file_state is S.NOT_DOWNLOADED
        assert "container.xml" not in seen

    def test_dataless_package_document(self, lib, tmp_path, monkeypatch):
        book = add(lib, epub(tmp_path))
        real = os.lstat
        monkeypatch.setattr(_icloud, "lstat", lambda p, *, dir_fd=None: (
            _fake(real(p, dir_fd=dir_fd), st_blocks=0) if os.fspath(p) == "content.opf"
            else real(p, dir_fd=dir_fd)))
        assert lib.api.get_book_metadata(book).file_state is S.NOT_DOWNLOADED

    def test_unreadable_package_document(self, lib, tmp_path):
        path = epub(tmp_path)
        (path / "OEBPS" / "content.opf").write_text("<package><broken")
        got = lib.api.get_book_metadata(add(lib, path, genre="G"))
        assert got.file_state is S.UNREADABLE and got.subjects == ("G",) and not got.book_file_fields

    def test_not_downloaded_is_not_cached(self, lib, tmp_path, monkeypatch):
        from py_apple_books import content

        book = add(lib, epub(tmp_path))
        monkeypatch.setattr(content, "is_downloaded", lambda path: False)
        assert lib.api.get_book_metadata(book).file_state is S.NOT_DOWNLOADED
        monkeypatch.undo()
        assert lib.api.get_book_metadata(book).file_state is S.READ


def _fake(st, **changes):
    from types import SimpleNamespace

    values = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
    values.update(changes)
    return SimpleNamespace(**values)


class TestInputs:
    def test_a_book_from_this_library_costs_no_statement(self, lib, tmp_path, sql_trace):
        book_id = add(lib, epub(tmp_path))
        book = lib.api.get_book_by_id(book_id)
        sql_trace.clear()
        assert lib.api.get_book_metadata(book).file_state is S.READ
        assert sql_trace == []

    def test_a_book_without_its_path_is_read_again(self, lib, tmp_path, sql_trace):
        book = lib.api.get_book_by_id(add(lib, epub(tmp_path)))
        book.path = None  # as if an only= read had left it out
        sql_trace.clear()
        assert lib.api.get_book_metadata(book).file_state is S.READ and len(sql_trace) == 1

    def test_a_book_from_another_library_is_resolved_here(self, lib, make_library, tmp_path):
        other = make_library()
        other_book_id = other.add_book("Other", genre="Elsewhere")["id"]
        here = add(lib, epub(tmp_path), genre="Here")
        assert here == other_book_id
        with LibraryDB(data_dir=other.data_dir) as db, use_library(db):
            foreign = PyAppleBooks().get_book_by_id(other_book_id)
        got = lib.api.get_book_metadata(foreign)
        assert got.subjects[0] == "Here" and got.file_state is S.READ

    def test_ids_as_text(self, lib, tmp_path):
        book = add(lib, epub(tmp_path))
        assert lib.api.get_book_metadata(str(book)) == lib.api.get_book_metadata(book)

    def test_unknown_id(self, lib):
        with pytest.raises(BookNotFoundError):
            lib.api.get_book_metadata(999)
        with pytest.raises(IndexError):
            lib.api.get_book_metadata(999)

    def test_threads(self, lib, tmp_path):
        book = add(lib, epub(tmp_path))
        expected = lib.api.get_book_metadata(book)
        from py_apple_books.content import clear_content_cache

        clear_content_cache()
        results, barrier = [], threading.Barrier(8)

        def work():
            barrier.wait()
            for _ in range(5):
                results.append(lib.api.get_book_metadata(book))

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 40 and set(results) == {expected}


class TestDrift:
    def test_without_the_language_year_and_release_date_columns(self, lib, tmp_path):
        book = add(lib, epub(tmp_path, metadata="<dc:date>2005</dc:date>"))
        set_columns(lib, book, ZLANGUAGE="de", ZYEAR="1999")
        for column in ("ZLANGUAGE", "ZYEAR", "ZRELEASEDATE"):
            lib.fixture.execute("library", f"ALTER TABLE ZBKLIBRARYASSET DROP COLUMN {column}")
        refresh(lib)
        assert {"language", "year", "release_date"} <= set(lib.api.store_info().missing_columns["Book"])
        got = lib.api.get_book_metadata(book)
        assert (got.language, got.published, got.year, got.file_state) == ("en", "2005", 2005, S.READ)

    def test_without_the_state_column(self, lib, tmp_path):
        book = add(lib, epub(tmp_path))
        lib.fixture.execute("library", "ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZSTATE")
        refresh(lib)
        assert lib.api.get_book_metadata(book).file_state is S.READ

    @pytest.mark.parametrize("value, expected", [
        (lambda path: os.fsencode(path), S.READ),  # a BLOB: read like the text
        (lambda path: b"", S.NO_FILE),
        (lambda path: str(path) + "\0x.epub", S.UNREADABLE),
    ])
    def test_odd_path_values(self, lib, tmp_path, value, expected):
        """File problems are states, never exceptions, whatever ZPATH
        holds."""
        book = add(lib, epub(tmp_path))
        set_columns(lib, book, ZPATH=value(epub(tmp_path, name="other.epub")))
        assert lib.api.get_book_metadata(book).file_state is expected


class TestPrivacy:
    TITLE = "An Unusually Private Title"

    def test_nothing_logged(self, lib, tmp_path, caplog, capsys):
        caplog.set_level(logging.DEBUG)
        path = epub(tmp_path, name=f"{self.TITLE}.epub", metadata=f"<dc:description>{self.TITLE}</dc:description>")
        (path / "META-INF" / "container.xml").write_text(
            '<container><rootfiles><rootfile full-path="OEBPS/' + self.TITLE
            + '.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        for book in (add(lib, path), add(lib, tmp_path / f"{self.TITLE} gone.epub")):
            assert lib.api.get_book_metadata(book).file_state is S.UNREADABLE
        out, err = capsys.readouterr()
        text = " ".join(r.getMessage() for r in caplog.records) + out + err
        assert self.TITLE not in text and str(tmp_path) not in text


def test_du_runs_only_after_the_bundle_checks(lib, tmp_path, monkeypatch):
    calls = []
    real = subprocess.run

    def spy(*args, **kwargs):
        calls.append(args[0][0] if args and args[0] else None)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    lib.api.get_book_metadata(add(lib, tmp_path / "gone.epub"))
    lib.api.get_book_metadata(add(lib, tmp_path / "x.pdf"))
    assert calls == []
    lib.api.get_book_metadata(add(lib, epub(tmp_path)))
    assert calls == ["du"]
