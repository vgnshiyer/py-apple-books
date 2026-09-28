"""Security tests for py_apple_books.content: EPUB bundle containment.

Apple Books stores imported EPUBs as unzipped directories, and ebooklib's
directory reader opens ``os.path.join(root, name)`` for every manifest
item. A crafted book could therefore read any local file (absolute or
``../`` hrefs, symlinks) or block forever (FIFOs, ``/dev/stdin``). These
tests build such bundles by hand — the fixture factory in ``conftest.py``
can't produce them — and check that every read stays inside the bundle,
fails fast, and reports errors without absolute paths.
"""

from __future__ import annotations

import os
import pathlib
import threading
import zipfile
from typing import Dict, List, Optional, Tuple

import pytest
from ebooklib import epub

from py_apple_books import content as content_module
from py_apple_books.content import (
    BookContent,
    _ContainedEpubReader,
    _opf_dir_from_container,
    _parse_ncx_bytes,
)
from py_apple_books.exceptions import AppleBooksError, UnsafeEpubEntryError


CANARY = "CANARY-SECRET-aws_secret_access_key"

_CONTAINER = (
    '<?xml version="1.0"?>\n'
    '<container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="{opf}" '
    'media-type="application/oebps-package+xml"/></rootfiles>'
    "</container>"
)

_OPF = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
    'unique-identifier="id">'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<dc:identifier id="id">crafted</dc:identifier>'
    "<dc:title>Crafted</dc:title><dc:language>en</dc:language>"
    "</metadata>"
    "<manifest>{items}</manifest>"
    "<spine{toc}>{itemrefs}</spine>"
    "</package>"
)

_CHAPTER = (
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c</title>'
    "</head><body><h1>{title}</h1><p>Legit text of {title}.</p></body></html>"
)

_NAV = (
    '<html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>nav</title>'
    '</head><body><nav epub:type="toc"><ol>{links}</ol></nav></body></html>'
)

_NCX = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
    "<head/><docTitle><text>Crafted</text></docTitle><navMap>{points}</navMap>"
    "</ncx>"
)


def _nav(links: List[Tuple[str, str]]) -> str:
    return _NAV.format(
        links="".join(f'<li><a href="{h}">{t}</a></li>' for h, t in links)
    )


def _ncx(points: List[Tuple[str, str, str]]) -> str:
    return _NCX.format(
        points="".join(
            f'<navPoint id="{i}"><navLabel><text>{t}</text></navLabel>'
            f'<content src="{s}"/></navPoint>'
            for i, t, s in points
        )
    )


def _write_bundle(
    bundle: pathlib.Path,
    manifest: List[Tuple[str, str, str, str]],
    files: Dict[str, str],
    spine: List[str],
    spine_toc: Optional[str] = None,
    opf_path: str = "OEBPS/content.opf",
) -> pathlib.Path:
    """Write an unzipped EPUB bundle by hand.

    :param manifest: ``(id, href, media_type, properties)`` tuples, with
        hrefs relative to the OPF — written verbatim, so they can be
        absolute or climb out with ``../``.
    :param files: bundle-relative path -> text content.
    :param spine: manifest ids in reading order.
    """
    (bundle / "META-INF").mkdir(parents=True, exist_ok=True)
    (bundle / "META-INF" / "container.xml").write_text(
        _CONTAINER.format(opf=opf_path)
    )
    items = "".join(
        f'<item id="{i}" href="{h}" media-type="{m}"'
        + (f' properties="{p}"' if p else "")
        + "/>"
        for i, h, m, p in manifest
    )
    itemrefs = "".join(f'<itemref idref="{i}"/>' for i in spine)
    toc = f' toc="{spine_toc}"' if spine_toc else ""
    opf = bundle / opf_path
    opf.parent.mkdir(parents=True, exist_ok=True)
    opf.write_text(_OPF.format(items=items, toc=toc, itemrefs=itemrefs))
    for rel, text in files.items():
        f = bundle / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return bundle


_XHTML = "application/xhtml+xml"
_NAV_ITEM = ("nav", "nav.xhtml", _XHTML, "nav")
_CH1_ITEM = ("ch1", "Text/ch1.xhtml", _XHTML, "")


@pytest.fixture
def lib(tmp_path):
    """A library dir with a canary file outside the book bundle.

    The bundle sits two levels below ``tmp_path`` (as real books sit at
    a fixed depth under the iCloud Books folder), so
    ``../../../outside/canary.txt`` from ``OEBPS/`` reaches the canary.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "canary.txt").write_text(CANARY)
    return tmp_path


def _bundle_path(lib: pathlib.Path) -> pathlib.Path:
    return lib / "a" / "b" / "Book.epub"


def _call_with_timeout(fn, timeout: float = 5.0):
    """Run ``fn`` in a daemon thread so a regression that blocks on a
    FIFO or ``/dev/stdin`` fails the test instead of hanging the run.
    Returns ``fn``'s result or re-raises its exception."""
    outcome: dict = {}

    def run():
        try:
            outcome["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — re-raised below
            outcome["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        pytest.fail(f"call still blocked after {timeout}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _assert_path_free(message: str, lib: pathlib.Path) -> None:
    assert str(lib) not in message
    assert str(lib.resolve()) not in message
    assert str(pathlib.Path.home()) not in message


# ---------------------------------------------------------------------------
# Legitimate bundles keep working
# ---------------------------------------------------------------------------


class TestLegitBundle:
    def test_in_bundle_dotdot_nav_href_still_resolves(self, lib):
        # nav.xhtml lives in OEBPS/nav/ and links "../Text/ch1.xhtml" —
        # a ".." that stays inside the bundle (seen in real books).
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[("nav", "nav/nav.xhtml", _XHTML, "nav"), _CH1_ITEM],
            files={
                "OEBPS/nav/nav.xhtml": _nav([("../Text/ch1.xhtml", "One")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        content = BookContent(bundle)
        chapters = content.list_chapters()
        assert [(c.id, c.title, c.href) for c in chapters] == [
            ("ch1", "One", "OEBPS/Text/ch1.xhtml")
        ]
        assert "Legit text of One." in content.get_chapter("ch1")

    def test_in_bundle_symlink_is_followed(self, lib):
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/Text/real.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        (bundle / "OEBPS" / "Text" / "ch1.xhtml").symlink_to("real.xhtml")
        assert "Legit text of One." in BookContent(bundle).get_chapter("ch1")

    def test_ncx_dot_prefixed_file_name_is_not_mangled(self, lib):
        # Regression for lstrip("./"), which turned ".ch1.xhtml" into
        # "ch1.xhtml".
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[
                ("ncx", "toc.ncx", "application/x-dtbncx+xml", ""),
                ("ch1", ".ch1.xhtml", _XHTML, ""),
            ],
            files={
                "OEBPS/toc.ncx": _ncx([("np1", "One", ".ch1.xhtml")]),
                "OEBPS/.ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        content = BookContent(bundle)
        chapters = content.list_chapters()
        assert [c.href for c in chapters] == ["OEBPS/.ch1.xhtml"]
        assert "Legit text of One." in content.get_chapter("np1")


# ---------------------------------------------------------------------------
# Escapes are refused
# ---------------------------------------------------------------------------


class TestManifestEscapes:
    @pytest.mark.parametrize(
        "href_for",
        [
            pytest.param(lambda lib: str(lib / "outside" / "canary.txt"), id="absolute"),
            pytest.param(lambda lib: "../../../outside/canary.txt", id="dotdot"),
            pytest.param(lambda lib: "/dev/stdin", id="dev-stdin"),
            pytest.param(lambda lib: "/dev/zero", id="dev-zero"),
        ],
    )
    def test_escaping_manifest_item_fails_to_load(self, lib, href_for):
        href = href_for(lib)
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[
                _NAV_ITEM,
                _CH1_ITEM,
                ("appx", href, "text/plain", ""),
            ],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One"), (href, "Notes")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        content = BookContent(bundle)
        with pytest.raises(UnsafeEpubEntryError) as exc:
            _call_with_timeout(content.list_chapters)
        assert CANARY not in str(exc.value)
        if not href.startswith("/"):
            # (An absolute href is echoed as written by the book.)
            _assert_path_free(str(exc.value), lib)
        with pytest.raises(UnsafeEpubEntryError):
            _call_with_timeout(lambda: content.get_chapter("appx"))

    def test_symlink_to_outside_fails_to_load(self, lib):
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM, ("appx", "appx.txt", "text/plain", "")],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        (bundle / "OEBPS" / "appx.txt").symlink_to(lib / "outside" / "canary.txt")
        with pytest.raises(UnsafeEpubEntryError, match="outside the book bundle"):
            BookContent(bundle).list_chapters()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
    def test_fifo_entry_fails_fast(self, lib):
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM, ("appx", "appx.txt", "text/plain", "")],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        fifo = bundle / "OEBPS" / "appx.txt"
        os.mkfifo(fifo)
        try:
            with pytest.raises(UnsafeEpubEntryError, match="not a regular file"):
                _call_with_timeout(BookContent(bundle).list_chapters)
        finally:
            # If a regression left a reader blocked in open(), give it a
            # writer so the daemon thread can finish.
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass

    def test_oversized_entry_is_refused(self, lib, monkeypatch):
        monkeypatch.setattr(content_module, "_MAX_ENTRY_BYTES", 10)
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        with pytest.raises(UnsafeEpubEntryError, match="larger than"):
            BookContent(bundle).list_chapters()


class TestTocEscapes:
    def test_dotdot_nav_href_is_not_advertised_or_read(self, lib):
        escape = "../../../outside/canary.txt"
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One"), (escape, "Notes")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        content = BookContent(bundle)
        chapters = content.list_chapters()
        assert [c.title for c in chapters] == ["One"]
        with pytest.raises(AppleBooksError):
            content.get_chapter("2")
        # The disk fallback refuses the href even if asked directly.
        with pytest.raises(UnsafeEpubEntryError):
            content._read_chapter_bytes(f"OEBPS/{escape}")

    def test_dotdot_ncx_src_in_subdir_is_not_advertised_or_read(self, lib):
        # No nav and no <spine toc>, so the media-type NCX fallback runs;
        # the NCX sits in OEBPS/toc/, so its srcs resolve from there.
        escape = "../../../../outside/canary.txt"
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[
                ("ncx", "toc/toc.ncx", "application/x-dtbncx+xml", ""),
                _CH1_ITEM,
            ],
            files={
                "OEBPS/toc/toc.ncx": _ncx(
                    [("np1", "One", "../Text/ch1.xhtml"), ("np2", "Notes", escape)]
                ),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        content = BookContent(bundle)
        chapters = content.list_chapters()
        assert [(c.id, c.href) for c in chapters] == [("np1", "OEBPS/Text/ch1.xhtml")]
        assert "Legit text of One." in content.get_chapter("np1")
        with pytest.raises(AppleBooksError):
            content.get_chapter("np2")
        with pytest.raises(UnsafeEpubEntryError):
            content._read_chapter_bytes("OEBPS/toc/" + escape)

    def test_parse_ncx_drops_escaping_and_absolute_srcs(self):
        ncx = _ncx(
            [
                ("a", "Up", "../../../x.xhtml"),
                ("b", "Abs", "/etc/passwd"),
                ("c", "Ok", "../Text/c.xhtml"),
                ("d", "Root", "../../d.xhtml"),
            ]
        ).encode()
        chapters = _parse_ncx_bytes(ncx, pathlib.PurePosixPath("OEBPS/toc"))
        assert [(c.id, c.href, c.order) for c in chapters] == [
            ("c", "OEBPS/Text/c.xhtml", 1),
            ("d", "d.xhtml", 2),
        ]


class TestContainerEscapes:
    def test_rootfile_outside_bundle_is_refused(self, lib):
        # A valid OPF waiting outside the bundle must not be loaded.
        outside_book = lib / "outside" / "OEBPS"
        _write_bundle(
            lib / "outside",
            manifest=[_NAV_ITEM, ("appx", "../canary.txt", "text/plain", "")],
            files={"OEBPS/nav.xhtml": _nav([("../canary.txt", "Notes")])},
            spine=["appx"],
        )
        assert (outside_book / "content.opf").exists()
        bundle = _bundle_path(lib)
        _write_bundle(
            bundle,
            manifest=[],
            files={},
            spine=[],
            opf_path="OEBPS/content.opf",
        )
        (bundle / "META-INF" / "container.xml").write_text(
            _CONTAINER.format(opf="../../../outside/OEBPS/content.opf")
        )
        assert _opf_dir_from_container(bundle) == pathlib.PurePosixPath()
        with pytest.raises(UnsafeEpubEntryError):
            BookContent(bundle).list_chapters()

    def test_container_symlink_outside_bundle_is_ignored(self, lib):
        (lib / "outside" / "container.xml").write_text(
            _CONTAINER.format(opf="OEBPS/content.opf")
        )
        bundle = _bundle_path(lib)
        (bundle / "META-INF").mkdir(parents=True)
        (bundle / "META-INF" / "container.xml").symlink_to(
            lib / "outside" / "container.xml"
        )
        assert _opf_dir_from_container(bundle) == pathlib.PurePosixPath()
        with pytest.raises(UnsafeEpubEntryError):
            BookContent(bundle).list_chapters()


# ---------------------------------------------------------------------------
# Zipped EPUBs
# ---------------------------------------------------------------------------


class TestZippedEpub:
    def _zipped(self, tmp_path) -> pathlib.Path:
        book = epub.EpubBook()
        book.set_identifier("zipped")
        book.set_title("Zipped")
        book.set_language("en")
        ch = epub.EpubHtml(title="One", file_name="ch1.xhtml", lang="en")
        ch.content = "<html><body><p>Zipped text.</p></body></html>"
        book.add_item(ch)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (epub.Link("ch1.xhtml", "One", "one"),)
        book.spine = ["nav", ch]
        path = tmp_path / "Zipped.epub"
        epub.write_epub(str(path), book)
        assert zipfile.is_zipfile(path)
        return path

    def test_contained_reader_delegates_to_zip_members(self, tmp_path):
        # Zip members are looked up by name inside the archive, so the
        # directory containment check doesn't apply.
        reader = _ContainedEpubReader(str(self._zipped(tmp_path)))
        book = reader.load()
        assert any(i.file_name == "ch1.xhtml" for i in book.get_items())

    def test_book_content_refuses_zip_without_touching_disk_fallbacks(self, tmp_path):
        path = self._zipped(tmp_path)
        content = BookContent(path)
        with pytest.raises(AppleBooksError) as exc:
            content.list_chapters()
        assert str(exc.value) == (
            "'Zipped.epub' is not an EPUB bundle directory; chapter "
            "listing/reading is only supported for EPUB books."
        )
        assert _opf_dir_from_container(path) == pathlib.PurePosixPath()


# ---------------------------------------------------------------------------
# Error messages carry no absolute paths
# ---------------------------------------------------------------------------


class TestPathFreeMessages:
    def test_pdf_message(self, lib):
        pdf = lib / "a" / "Some Book.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"%PDF-1.4\n")
        for call in (BookContent(pdf).list_chapters, lambda: BookContent(pdf).get_chapter("1")):
            with pytest.raises(AppleBooksError) as exc:
                call()
            assert str(exc.value) == (
                "This book is a PDF; chapter listing/reading is only "
                "supported for EPUB books."
            )

    def test_missing_manifest_file_message(self, lib):
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM],
            files={"OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")])},
            spine=["ch1"],
        )
        with pytest.raises(AppleBooksError) as exc:
            BookContent(bundle).list_chapters()
        assert "'OEBPS/Text/ch1.xhtml'" in str(exc.value)
        _assert_path_free(str(exc.value), lib)

    def test_unparseable_opf_message(self, lib):
        bundle = _write_bundle(
            _bundle_path(lib), manifest=[], files={}, spine=[]
        )
        (bundle / "OEBPS" / "content.opf").write_text("<package")
        with pytest.raises(AppleBooksError) as exc:
            BookContent(bundle).list_chapters()
        assert "'Book.epub'" in str(exc.value)
        _assert_path_free(str(exc.value), lib)

    def test_missing_chapter_file_message(self, lib):
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        content = BookContent(bundle)
        content.list_chapters()
        with pytest.raises(AppleBooksError) as exc:
            content._read_chapter_bytes("OEBPS/Text/gone.xhtml")
        assert "missing on disk" in str(exc.value)
        _assert_path_free(str(exc.value), lib)
