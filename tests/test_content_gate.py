"""The gate of the 1.11 content APIs (stream 2.1): _book_gate / _path_gate
and the BookContent methods that run it. Nothing is downloaded, nothing
is read that isn't needed, every read goes through the guarded per-file
primitive, and the answer doesn't depend on whether the index is cached.

A real iCloud placeholder can't be made here, so ``os.lstat``/``os.stat``
are wrapped to report SF_DATALESS for chosen paths (as in
tests/test_content_security.py), and to record every path stat'ed.
"""

from __future__ import annotations

import errno
import os
import pathlib
import threading
from types import SimpleNamespace
from typing import Dict, List

import pytest

from py_apple_books import _epub_index, _icloud
from py_apple_books import content as content_module
from py_apple_books.content import BookContent
from py_apple_books.exceptions import (
    AppleBooksError,
    BookNotDownloadedError,
    DRMProtectedError,
    NotEpubError,
)
from py_apple_books.positions import UnavailableReason
from tests import _epub_shapes, _fs_audit

FONT_ONLY = """<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
    xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
  <enc:CipherData><enc:CipherReference URI="OEBPS/f.otf"/></enc:CipherData></enc:EncryptedData>
</encryption>"""
CONTENT_ENCRYPTED = FONT_ONLY.replace("http://www.idpf.org/2008/embedding",
                                      "http://www.w3.org/2001/04/xmlenc#aes256-cbc")


def _dataless_copy(st):
    fields = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
    fields["st_flags"] = getattr(st, "st_flags", 0) | _icloud.SF_DATALESS
    return SimpleNamespace(**fields)


class _FakeICloud:
    def __init__(self):
        self.marked = set()
        self.stats: List[str] = []

    def mark(self, *paths):
        for path in paths:
            self.marked.add(os.fspath(path))
            self.marked.add(os.path.realpath(path))
        self.stats.clear()

    def inside(self, folder) -> List[str]:
        stats = list(self.stats)
        prefix = os.path.realpath(folder) + os.sep
        return [p for p in stats if os.path.realpath(p).startswith(prefix)]

    def touched(self, path) -> List[str]:
        stats = list(self.stats)
        real = os.path.realpath(path)
        return [p for p in stats if os.path.realpath(p) == real or os.path.realpath(p).startswith(real + os.sep)]


@pytest.fixture
def icloud(monkeypatch):
    fake = _FakeICloud()
    real_lstat, real_stat = os.lstat, os.stat

    def wrap(real):
        def call(path, *args, dir_fd=None, **kwargs):
            if dir_fd is None and isinstance(path, (str, bytes, os.PathLike)):
                fake.stats.append(os.fsdecode(path))
            st = real(path, *args, dir_fd=dir_fd, **kwargs)
            if dir_fd is None and isinstance(path, (str, os.PathLike)) and os.fspath(path) in fake.marked:
                return _dataless_copy(st)
            return st
        return call

    monkeypatch.setattr(os, "lstat", wrap(real_lstat))
    monkeypatch.setattr(os, "stat", wrap(real_stat))
    return fake


@pytest.fixture
def policy(monkeypatch):
    """Recorder for the thread I/O policy: ``policy.off()`` is True while
    downloads are turned off for the calling thread."""
    values: Dict[int, int] = {}

    def get(kind, scope):
        return values.get(threading.get_ident(), 0)

    def set_(kind, scope, value):
        values[threading.get_ident()] = value
        return 0

    monkeypatch.setattr(_icloud, "_policy_loaded", True)
    monkeypatch.setattr(_icloud, "_policy_fns", (get, set_))
    return SimpleNamespace(off=lambda: values.get(threading.get_ident(), 0) == 1)


@pytest.fixture
def reads(monkeypatch):
    """Every read through content._read_entry (the guarded primitive),
    as bundle-relative names."""
    seen = []
    real = content_module._read_entry

    def spy(root, href, max_bytes):
        seen.append(href)
        return real(root, href, max_bytes)

    monkeypatch.setattr(content_module, "_read_entry", spy)
    return seen


@pytest.fixture
def no_du(monkeypatch):
    monkeypatch.setattr("py_apple_books.content.subprocess.run",
                        lambda *a, **k: pytest.fail("du ran"))


def _gate(path):
    return _epub_index._gate_path(path)


# ---------------------------------------------------------------------------
# Step 1: the database alone
# ---------------------------------------------------------------------------


class TestBookGate:
    def _book(self, api, library, **kwargs):
        row = library.add_book("Gated", **kwargs)
        return api.get_book_by_id(row["id"])

    def test_readable_book(self, api, library, tmp_path, no_du):
        bundle = _epub_shapes.plain(tmp_path)
        book = self._book(api, library, path=bundle)
        reason, key = _epub_index._book_gate(book)
        assert reason is None
        index = _epub_index._cached_index(key)
        assert index is not None and len(index.chapters) == 3
        assert _epub_index._cached_index(("book", 1)) is None

    def test_cloud_only_touches_no_file(self, api, library, tmp_path, icloud):
        bundle = _epub_shapes.plain(tmp_path)
        book = self._book(api, library, path=bundle, state=3)
        icloud.mark()
        with _fs_audit.record() as rec:
            assert _epub_index._book_gate(book) == (UnavailableReason.NOT_DOWNLOADED, None)
        assert rec.events == [] and icloud.touched(bundle) == []

    def test_no_path_and_unowned(self, api, library, icloud):
        book = self._book(api, library, path=None)
        icloud.mark()
        assert _epub_index._book_gate(book) == (UnavailableReason.NOT_DOWNLOADED, None)
        unowned = SimpleNamespace(path=None, state=1, is_store_series_item=True)
        assert _epub_index._book_gate(unowned) == (UnavailableReason.NOT_OWNED, None)
        assert icloud.stats == []

    def test_not_epub_by_suffix(self, api, library, tmp_path, icloud):
        pdf = tmp_path / "Book.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        book = self._book(api, library, path=pdf)
        icloud.mark()
        assert _epub_index._book_gate(book) == (UnavailableReason.NOT_EPUB, None)
        assert icloud.touched(pdf) == []

    def test_path_gate_two_tuple(self, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        reason, key = _epub_index._path_gate(bundle)
        assert reason is None and key == _gate(bundle).index.key


# ---------------------------------------------------------------------------
# Steps 2-6: the files
# ---------------------------------------------------------------------------


class TestPathGate:
    def test_missing_bundle(self, tmp_path):
        gated = _gate(tmp_path / "Gone.epub")
        assert gated.reason == UnavailableReason.NOT_DOWNLOADED
        # BookContent methods report it as 1.10's list_chapters does.
        with pytest.raises(NotEpubError, match="'Gone.epub' is not an EPUB bundle directory"):
            BookContent(tmp_path / "Gone.epub").list_spine_items()

    def test_not_a_folder(self, tmp_path):
        fake = tmp_path / "File.epub"
        fake.write_bytes(b"PK\x03\x04")
        assert _gate(fake).reason == UnavailableReason.NOT_EPUB
        with pytest.raises(NotEpubError):
            BookContent(fake).get_spine_item_text(0)

    def test_dataless_root(self, tmp_path, icloud):
        bundle = _epub_shapes.plain(tmp_path)
        icloud.mark(bundle)
        gated = _gate(bundle)
        assert gated.reason == UnavailableReason.NOT_DOWNLOADED
        assert str(gated.error) == _icloud.PARTIAL_DOWNLOAD_MESSAGE
        assert icloud.inside(bundle) == []

    def test_icloud_stub(self, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        (tmp_path / ".Plain.epub.icloud").write_bytes(b"")
        assert _gate(bundle).reason == UnavailableReason.NOT_DOWNLOADED
        with pytest.raises(BookNotDownloadedError):
            BookContent(bundle).list_spine_items()

    @pytest.mark.parametrize("warm", [False, True])
    def test_dataless_folder_is_never_looked_into(self, tmp_path, icloud, warm):
        bundle = _epub_shapes.plain(tmp_path)
        if warm:
            assert _gate(bundle).reason is None
        oebps = bundle / "OEBPS"
        icloud.mark(oebps)
        with _fs_audit.record() as rec:
            gated = _gate(bundle)
        assert gated.reason == UnavailableReason.NOT_DOWNLOADED
        assert icloud.inside(oebps) == []
        assert rec.under(oebps) == []
        with pytest.raises(BookNotDownloadedError) as exc:
            BookContent(bundle).list_spine_items()
        assert str(exc.value) == _icloud.PARTIAL_DOWNLOAD_MESSAGE

    @pytest.mark.parametrize("warm", [False, True])
    def test_dataless_meta_inf(self, tmp_path, icloud, warm):
        bundle = _epub_shapes.plain(tmp_path)
        if warm:
            assert _gate(bundle).reason is None
        icloud.mark(bundle / "META-INF")
        assert _gate(bundle).reason == UnavailableReason.NOT_DOWNLOADED
        assert icloud.inside(bundle / "META-INF") == []

    @pytest.mark.parametrize("warm", [False, True])
    def test_dataless_nav(self, tmp_path, icloud, warm):
        bundle = _epub_shapes.plain(tmp_path)
        if warm:
            assert _gate(bundle).reason is None
        nav = bundle / "OEBPS" / "nav.xhtml"
        icloud.mark(nav)
        with _fs_audit.record() as rec:
            assert _gate(bundle).reason == UnavailableReason.NOT_DOWNLOADED
        assert rec.under(nav, "open") == []

    def test_dataless_chapter_is_never_opened(self, tmp_path, icloud):
        bundle = _epub_shapes.plain(tmp_path)
        chapter = bundle / "OEBPS" / "ch2.xhtml"
        icloud.mark(chapter)
        content = BookContent(bundle)
        with _fs_audit.record() as rec:
            assert len(content.list_spine_items()) == 3  # the index never touches it
            assert "Alpha" in content.get_spine_item_text("ch1")
            assert icloud.touched(chapter) == []
            with pytest.raises(BookNotDownloadedError) as exc:
                content.get_spine_item_text("ch2")
        assert str(exc.value) == _icloud.PARTIAL_DOWNLOAD_MESSAGE
        assert exc.value.__cause__ is None
        assert rec.under(chapter, "open") == []

    @pytest.mark.parametrize("code", [errno.EDEADLK, errno.ETIMEDOUT])
    def test_read_that_would_download(self, tmp_path, monkeypatch, code):
        bundle = _epub_shapes.plain(tmp_path)
        content = BookContent(bundle)
        content.list_spine_items()
        real = os.read

        def read(fd, n):
            raise OSError(code, os.strerror(code))

        monkeypatch.setattr(os, "read", read)
        try:
            with pytest.raises(BookNotDownloadedError) as exc:
                content.get_spine_item_text("ch2")
        finally:
            monkeypatch.setattr(os, "read", real)
        assert str(exc.value) == _icloud.PARTIAL_DOWNLOAD_MESSAGE

    def test_no_du_and_no_walk(self, tmp_path, monkeypatch, no_du):
        bundle = _epub_shapes.plain(tmp_path)
        monkeypatch.setattr(_icloud, "walk_bundle_local", lambda root: pytest.fail("walked"))
        with _fs_audit.record() as rec:
            content = BookContent(bundle)
            content.list_spine_items()
            list(content.iter_spine_text())
        assert rec.of("os.scandir", "os.listdir") == []
        assert rec.of(*_fs_audit.PROCESS_EVENTS) == []

    def test_runs_with_downloads_off(self, tmp_path, monkeypatch, policy):
        bundle = _epub_shapes.plain(tmp_path)
        seen = []
        real_lstat, real_stat = _icloud.lstat, _icloud.stat
        monkeypatch.setattr(_icloud, "lstat", lambda p, **kw: seen.append(policy.off()) or real_lstat(p, **kw))
        monkeypatch.setattr(_icloud, "stat", lambda p: seen.append(policy.off()) or real_stat(p))
        reads_off = []
        real_read = os.read
        monkeypatch.setattr(os, "read", lambda fd, n: reads_off.append(policy.off()) or real_read(fd, n))
        content = BookContent(bundle)
        content.list_spine_items()
        content.get_spine_item_text(1)
        monkeypatch.setattr(os, "read", real_read)
        assert seen and all(seen)
        assert reads_off and all(reads_off)
        assert not policy.off()

    def test_unreadable_package(self, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        (bundle / "META-INF" / "container.xml").write_text("<container><rootfiles/></container>")
        gated = _gate(bundle)
        assert gated.reason == UnavailableReason.UNREADABLE
        with pytest.raises(AppleBooksError) as exc:
            BookContent(bundle).list_spine_items()
        assert str(tmp_path) not in str(exc.value)

    def test_same_answer_cold_and_warm(self, tmp_path, icloud):
        # A DRM-protected book whose nav is also a placeholder: DRM, cached
        # index or not.
        bundle = _epub_shapes.plain(tmp_path)
        cold, warm = bundle, _epub_shapes.subfile(tmp_path)
        assert _gate(warm).reason is None  # cached
        for book in (cold, warm):
            (book / "META-INF" / "sinf.xml").write_text("<sinf/>")
        icloud.mark(cold / "OEBPS" / "nav.xhtml", warm / "OEBPS" / "nav.xhtml")
        assert _gate(cold).reason == _gate(warm).reason == UnavailableReason.DRM
        for book in (cold, warm):
            (book / "META-INF" / "sinf.xml").unlink()
        assert _gate(cold).reason == _gate(warm).reason == UnavailableReason.NOT_DOWNLOADED


# ---------------------------------------------------------------------------
# DRM
# ---------------------------------------------------------------------------


class TestDrm:
    @pytest.mark.parametrize("name,message", [("sinf.xml", "(FairPlay)"), ("rights.xml", "encrypted EPUB")])
    def test_marker_on_a_warm_hit(self, tmp_path, name, message):
        bundle = _epub_shapes.plain(tmp_path)
        # Warmed through 1.10's path, which doesn't check DRM.
        assert BookContent(bundle).list_chapters()
        assert BookContent(bundle).list_spine_items()
        (bundle / "META-INF" / name).write_text("<x/>")
        assert _gate(bundle).reason == UnavailableReason.DRM
        for call in (lambda c: c.list_spine_items(), lambda c: c.get_spine_item_text(0),
                     lambda c: c.iter_spine_text()):
            with pytest.raises(DRMProtectedError) as exc:
                call(BookContent(bundle))
            assert message in str(exc.value) and "/" not in str(exc.value)
        # 1.10's list_chapters is unchanged: it never checked DRM.
        assert len(BookContent(bundle).list_chapters()) == 3

    def test_encryption_xml(self, tmp_path, reads):
        bundle = _epub_shapes.plain(tmp_path)
        enc = bundle / "META-INF" / "encryption.xml"
        enc.write_text(FONT_ONLY)
        assert _gate(bundle).reason is None
        assert reads.count("META-INF/encryption.xml") == 1
        assert _gate(bundle).reason is None
        assert reads.count("META-INF/encryption.xml") == 1  # the verdict is cached
        enc.write_text(CONTENT_ENCRYPTED)
        assert _gate(bundle).reason == UnavailableReason.DRM
        assert BookContent(bundle).is_drm_protected is True

    @pytest.mark.parametrize("data", [b"<not xml", b"x" * (2 * 1024 * 1024)])
    def test_bad_encryption_xml_fails_closed(self, tmp_path, data):
        bundle = _epub_shapes.plain(tmp_path)
        (bundle / "META-INF" / "encryption.xml").write_bytes(data)
        assert _gate(bundle).reason == UnavailableReason.DRM
        assert BookContent(bundle).is_drm_protected is True

    def test_unreadable_encryption_xml_is_not_cached(self, tmp_path, monkeypatch):
        bundle = _epub_shapes.plain(tmp_path)
        (bundle / "META-INF" / "encryption.xml").write_text(FONT_ONLY)
        real = content_module._read_entry

        def fail_once(root, href, max_bytes):
            if href.endswith("encryption.xml") and not calls:
                calls.append(1)
                raise PermissionError(errno.EACCES, "Permission denied")
            return real(root, href, max_bytes)

        calls = []
        monkeypatch.setattr(content_module, "_read_entry", fail_once)
        assert _gate(bundle).reason == UnavailableReason.DRM
        assert _gate(bundle).reason is None

    def test_dataless_encryption_xml_is_not_downloaded(self, tmp_path, icloud, reads):
        bundle = _epub_shapes.plain(tmp_path)
        enc = bundle / "META-INF" / "encryption.xml"
        enc.write_text(FONT_ONLY)
        icloud.mark(enc)
        assert _gate(bundle).reason == UnavailableReason.NOT_DOWNLOADED
        assert "META-INF/encryption.xml" not in reads


# ---------------------------------------------------------------------------
# No ungated reads
# ---------------------------------------------------------------------------


def test_every_open_goes_through_the_guarded_read(tmp_path, reads):
    """Every file opened by the new APIs on a cold cache is one
    content._read_entry read, and only the index's files or a file whose
    text (or anchors) was asked for."""
    bundle = _epub_shapes.gutenberg(tmp_path)
    (bundle / "META-INF" / "encryption.xml").write_text(FONT_ONLY)
    content = BookContent(bundle)
    with _fs_audit.record() as rec:
        content.list_spine_items()
        content.get_spine_item_text("body")
        list(content.iter_spine_text())
        _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml")
    # (Opens outside the bundle are the interpreter importing modules.)
    opened = sorted(os.path.relpath(e.path, os.path.realpath(bundle)) for e in rec.under(bundle, "open"))
    assert opened == sorted(reads)
    assert sorted(set(reads)) == ["META-INF/container.xml", "META-INF/encryption.xml",
                                  "OEBPS/body.xhtml", "OEBPS/content.opf", "OEBPS/nav.xhtml",
                                  "OEBPS/toc.ncx"]
    assert reads.count("OEBPS/body.xhtml") == 2  # its text once (memoized), its anchors once
    assert "OEBPS/big.png" not in reads
    assert rec.of("os.scandir", "os.listdir") == []


def test_audit_of_a_warm_pass(tmp_path, reads):
    bundle = _epub_shapes.plain(tmp_path)
    BookContent(bundle).list_chapters()  # the full load, once: then the index serves it
    BookContent(bundle).list_spine_items()
    reads.clear()
    with _fs_audit.record() as rec:
        BookContent(bundle).list_spine_items()
        BookContent(bundle).list_chapters()
    assert reads == [] and rec.under(bundle) == []


def test_bundle_symlink(tmp_path):
    bundle = _epub_shapes.plain(tmp_path / "real")
    link = tmp_path / "Linked.epub"
    link.symlink_to(bundle)
    assert [i.item_id for i in BookContent(link).list_spine_items()] == ["ch1", "ch2", "ch3"]
    assert _gate(link).key == _gate(bundle).key
    assert BookContent(link).get_spine_item_text(0) == BookContent(bundle).get_spine_item_text(0)


def test_messages_are_path_free(tmp_path):
    long_name = "x" * 200
    bundle = _epub_shapes.plain(tmp_path)
    target = bundle.parent / f"{long_name}.epub"
    bundle.rename(target)
    (target / "OEBPS" / "content.opf").write_text("<package")  # malformed
    for call in (lambda: BookContent(target).list_spine_items(),
                 lambda: BookContent(target).get_spine_item_text(0)):
        with pytest.raises(AppleBooksError) as exc:
            call()
        message = str(exc.value)
        assert str(tmp_path) not in message and len(message) < 300 and pathlib.Path.home().name not in message


def _deep_ncx(depth):
    """An NCX whose navPoints nest ``depth`` deep (about 50 bytes a level)."""
    point = ('<navPoint id="p{0}" playOrder="{0}"><navLabel><text>L</text></navLabel>'
             '<content src="c1.xhtml"/>')
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            '<head/><docTitle><text>T</text></docTitle><navMap>'
            + "".join(point.format(i) for i in range(1, depth + 1))
            + "</navPoint>" * depth + "</navMap></ncx>")


def test_failure_after_the_load_is_unreadable(tmp_path):
    """A failure while the index is computed from the files read (here a
    RecursionError: an NCX found by media type, nested deeper than the
    recursion limit) is reported like any unreadable book: UNREADABLE from
    the gate, AppleBooksError from the methods, never a bare exception."""
    from py_apple_books.testing.epub import write_epub_bundle

    bundle = write_epub_bundle(tmp_path / "Deep.epub", [("c1", "<p>one</p>")],
                               toc=[("One", "c1.xhtml")], nav="ncx-undeclared")
    (bundle / "OEBPS" / "toc.ncx").write_text(_deep_ncx(1200))
    book = SimpleNamespace(path=bundle, state=1, is_store_series_item=False)
    for _ in range(2):  # nothing is cached: the same answer every time
        assert _epub_index._book_gate(book) == (UnavailableReason.UNREADABLE, None)
        assert _gate(bundle).reason == UnavailableReason.UNREADABLE
    content = BookContent(bundle)
    for call in (content.list_spine_items, lambda: content.get_spine_item_text(0),
                 lambda: list(content.iter_spine_text())):
        with pytest.raises(AppleBooksError, match="Could not read EPUB 'Deep.epub'") as exc:
            call()
        assert type(exc.value) is AppleBooksError
        assert isinstance(exc.value.__cause__, RecursionError)


class TestPerFileStubs:
    """Files evicted the older way: gone, with a ``.<name>.icloud`` stub
    next to them, inside the bundle (a stub next to the bundle itself is
    covered above). The 1.11 paths report them as not downloaded."""

    @staticmethod
    def _evict(path):
        path.unlink()
        (path.parent / f".{path.name}.icloud").write_bytes(b"")

    @staticmethod
    def _book(bundle):
        return SimpleNamespace(path=bundle, state=1, is_store_series_item=False)

    def test_chapter(self, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        self._evict(bundle / "OEBPS" / "ch2.xhtml")
        content = BookContent(bundle)
        assert len(content.list_spine_items()) == 3  # the index doesn't need it
        assert _epub_index._book_gate(self._book(bundle))[0] is None
        for item in ("ch2", 1):
            with pytest.raises(BookNotDownloadedError):
                content.get_spine_item_text(item)
        it = content.iter_spine_text()
        assert next(it).item_id == "ch1"
        with pytest.raises(BookNotDownloadedError):
            next(it)
        # Gone with no stub: unreadable, as before.
        (bundle / "OEBPS" / ".ch2.xhtml.icloud").unlink()
        with pytest.raises(AppleBooksError) as exc:
            BookContent(bundle).get_spine_item_text("ch2")
        assert not isinstance(exc.value, BookNotDownloadedError)

    @pytest.mark.parametrize("name", ["content.opf", "nav.xhtml", "toc.ncx"])
    @pytest.mark.parametrize("warm", [False, True])
    def test_package_and_navigation_files(self, tmp_path, name, warm):
        bundle = _epub_shapes.plain(tmp_path)
        if warm:
            assert _epub_index._book_gate(self._book(bundle))[0] is None
        self._evict(bundle / "OEBPS" / name)
        assert _epub_index._book_gate(self._book(bundle)) == (UnavailableReason.NOT_DOWNLOADED, None)
        with pytest.raises(BookNotDownloadedError):
            BookContent(bundle).list_spine_items()
