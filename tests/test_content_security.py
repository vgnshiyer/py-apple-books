"""Security tests for py_apple_books.content: EPUB bundle containment.

Apple Books stores imported EPUBs as unzipped directories, and ebooklib's
directory reader opens ``os.path.join(root, name)`` for every manifest
item. A crafted book could therefore read any local file (absolute or
``../`` hrefs, symlinks) or block forever (FIFOs, ``/dev/stdin``). These
tests build such bundles by hand — the fixture factory in ``conftest.py``
can't produce them — and check that every read stays inside the bundle,
fails fast, and reports errors without absolute paths.

1.11 adds: no read downloads an evicted iCloud file (placeholders are
faked by patching ``os.lstat``/``os.stat``), the guarded per-file read of
new APIs (``_read_entry_bytes``), and short messages for long names.
"""

from __future__ import annotations

import errno
import os
import pathlib
import threading
import zipfile
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import pytest
from ebooklib import epub

from py_apple_books import _icloud
from py_apple_books import content as content_module
from py_apple_books.content import (
    BookContent,
    _ContainedEpubReader,
    _opf_dir_from_container,
    _parse_ncx_bytes,
    _read_entry_bytes,
    _safe_bundle_path,
)
from py_apple_books.exceptions import (
    AppleBooksError,
    BookNotDownloadedError,
    ChapterNotFoundError,
    NotEpubError,
    UnsafeEpubEntryError,
)
from tests import _fs_audit


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

    def test_in_bundle_symlinked_directory_is_followed(self, lib):
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/RealText/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        (bundle / "OEBPS" / "Text").symlink_to("RealText")
        assert "Legit text of One." in BookContent(bundle).get_chapter("ch1")

    def test_directories_are_resolved_once_per_load(self, lib, monkeypatch):
        images = [(f"img{i}", f"Images/{i}.png", "image/png", "") for i in range(50)]
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM, *images],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
                **{f"OEBPS/Images/{i}.png": "png" for i in range(50)},
            },
            spine=["ch1"],
        )
        calls = []
        real_resolve = pathlib.Path.resolve
        monkeypatch.setattr(
            pathlib.Path,
            "resolve",
            lambda self, *a, **k: calls.append(self) or real_resolve(self, *a, **k),
        )
        reader = _ContainedEpubReader(str(bundle), {"ignore_ncx": False})
        reader.load()
        # The bundle root plus one per directory (META-INF, OEBPS,
        # OEBPS/Text, OEBPS/Images), not one per file.
        assert len(calls) <= 6

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

    def test_symlinked_directory_to_outside_fails_to_load(self, lib):
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[
                _NAV_ITEM,
                _CH1_ITEM,
                ("appx", "Extra/canary.txt", "text/plain", ""),
            ],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        (bundle / "OEBPS" / "Extra").symlink_to(lib / "outside")
        with pytest.raises(UnsafeEpubEntryError, match="outside the book bundle"):
            BookContent(bundle).list_chapters()

    @pytest.mark.parametrize("via", ["file", "directory"])
    def test_symlink_loop_dotdot_escape_is_refused(self, lib, via):
        # Before Python 3.13, non-strict resolve() stops at a symlink
        # loop and returns the rest unresolved: "loop/../out" came back
        # as ".../OEBPS/out" with "out" still a live link to outside.
        href = "appx.txt" if via == "file" else "Extra/canary.txt"
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM, ("appx", href, "text/plain", "")],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One"), (href, "Notes")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        oebps = bundle / "OEBPS"
        (oebps / "loop").symlink_to("loop")
        if via == "file":
            (oebps / "out").symlink_to(lib / "outside" / "canary.txt")
            (oebps / "appx.txt").symlink_to("loop/../out")
        else:
            (oebps / "out").symlink_to(lib / "outside")
            (oebps / "Extra").symlink_to("loop/../out")
        content = BookContent(bundle)
        with pytest.raises(UnsafeEpubEntryError) as exc:
            content.list_chapters()
        assert CANARY not in str(exc.value)
        with pytest.raises(UnsafeEpubEntryError):
            content.get_chapter("appx")

    def test_directory_swapped_mid_load_is_not_trusted(self, lib):
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
                "OEBPS/Images/1.png": "png",
            },
            spine=["ch1"],
        )
        (lib / "outside" / "2.png").write_text(CANARY)
        reader = _ContainedEpubReader(str(bundle), {"ignore_ncx": False})
        assert reader.read_file("OEBPS/Images/1.png") == b"png"
        images = bundle / "OEBPS" / "Images"
        images.rename(images.with_name("Images.real"))
        images.symlink_to(lib / "outside")
        with pytest.raises(UnsafeEpubEntryError, match="outside the book bundle"):
            reader.read_file("OEBPS/Images/2.png")

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

    @pytest.mark.parametrize("toc", ["nav", "ncx"])
    def test_absolute_toc_href_with_opf_in_subdir_is_not_advertised(
        self, lib, toc
    ):
        # With the OPF in OEBPS/, prefixing "/abs" gives "OEBPS//abs",
        # which normalizes to an in-bundle-looking "OEBPS/abs".
        absolute = str(lib / "outside" / "canary.txt")
        links = [("Text/ch1.xhtml", "One"), (absolute, "Notes")]
        if toc == "nav":
            manifest = [_NAV_ITEM, _CH1_ITEM]
            files = {"OEBPS/nav.xhtml": _nav(links)}
            spine_toc = None
        else:
            manifest = [("ncx", "toc.ncx", "application/x-dtbncx+xml", ""), _CH1_ITEM]
            files = {
                "OEBPS/toc.ncx": _ncx(
                    [(f"np{i}", t, h) for i, (h, t) in enumerate(links, 1)]
                )
            }
            spine_toc = "ncx"
        files["OEBPS/Text/ch1.xhtml"] = _CHAPTER.format(title="One")
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=manifest,
            files=files,
            spine=["ch1"],
            spine_toc=spine_toc,
        )
        chapters = BookContent(bundle).list_chapters()
        assert [(c.title, c.href) for c in chapters] == [
            ("One", "OEBPS/Text/ch1.xhtml")
        ]

    def test_nul_byte_href_is_refused_as_apple_books_error(self, lib):
        # "%00" unquotes to a NUL byte, which makes resolve() raise
        # ValueError; it must surface like any other unreadable entry.
        bundle = _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM],
            files={
                "OEBPS/nav.xhtml": _nav(
                    [("Text/ch1.xhtml", "One"), ("Text/x.xhtml%00", "Bad")]
                ),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )
        content = BookContent(bundle)
        assert [c.id for c in content.list_chapters()] == ["ch1", "2"]
        with pytest.raises(UnsafeEpubEntryError):
            content.get_chapter("2")

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

    @pytest.mark.parametrize("encoding", ["Shift_JIS", "bogus"])
    def test_undecodable_container_falls_back_to_bundle_root(self, lib, encoding):
        bundle = _bundle_path(lib)
        (bundle / "META-INF").mkdir(parents=True)
        (bundle / "META-INF" / "container.xml").write_text(
            _CONTAINER.format(opf="OEBPS/content.opf").replace(
                '<?xml version="1.0"?>',
                f'<?xml version="1.0" encoding="{encoding}"?>',
            )
        )
        assert _opf_dir_from_container(bundle) == pathlib.PurePosixPath()


# ---------------------------------------------------------------------------
# The DRM gate reads encryption.xml before the book loads
# ---------------------------------------------------------------------------


_FONT_ONLY_ENCRYPTION = (
    '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
    'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
    '<enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
    '<enc:CipherData><enc:CipherReference URI="OEBPS/f.otf"/></enc:CipherData>'
    "</enc:EncryptedData></encryption>"
)


class TestDrmGate:
    """``is_drm_protected`` runs on every ``get_book_content`` call, for
    library-wide MCP tools too, so reading ``encryption.xml`` must stay
    in the bundle, never block, and fail closed."""

    def _bundle(self, lib) -> pathlib.Path:
        return _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            },
            spine=["ch1"],
        )

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
    def test_fifo_encryption_xml_fails_closed_fast(self, lib):
        fifo = self._bundle(lib) / "META-INF" / "encryption.xml"
        os.mkfifo(fifo)
        try:
            content = BookContent(fifo.parent.parent)
            assert _call_with_timeout(lambda: content.is_drm_protected) is True
        finally:
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass

    @pytest.mark.parametrize("where", ["dev-zero", "outside"])
    def test_symlinked_encryption_xml_fails_closed_fast(self, lib, where):
        bundle = self._bundle(lib)
        if where == "dev-zero":
            target = pathlib.Path("/dev/zero")
        else:
            # Font-only, so reading it would wrongly clear the book.
            target = lib / "outside" / "encryption.xml"
            target.write_text(_FONT_ONLY_ENCRYPTION)
        (bundle / "META-INF" / "encryption.xml").symlink_to(target)
        content = BookContent(bundle)
        assert _call_with_timeout(lambda: content.is_drm_protected) is True

    def test_oversized_encryption_xml_is_not_parsed(self, lib, monkeypatch):
        bundle = self._bundle(lib)
        (bundle / "META-INF" / "encryption.xml").write_text(_FONT_ONLY_ENCRYPTION)
        assert BookContent(bundle).is_drm_protected is False
        monkeypatch.setattr(content_module, "_MAX_ENCRYPTION_XML_BYTES", 10)
        assert BookContent(bundle).is_drm_protected is True


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


# ---------------------------------------------------------------------------
# iCloud placeholders: no read downloads an evicted file (1.11)
# ---------------------------------------------------------------------------
#
# A real placeholder can't be made here, so ``os.lstat``/``os.stat`` are
# wrapped to report SF_DATALESS for chosen paths (``_icloud`` and
# ``Path.resolve`` call them at run time), and to record every path
# stat'ed. The I/O policy calls are replaced by a recorder.


def _dataless_copy(st):
    fields = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
    fields["st_flags"] = getattr(st, "st_flags", 0) | _icloud.SF_DATALESS
    return SimpleNamespace(**fields)


class _FakeICloud:
    def __init__(self):
        self.marked = set()
        self.stats: List[str] = []

    def mark(self, *paths):
        """Make ``paths`` placeholders, and start recording afresh (so
        stats made while building the bundle don't count)."""
        for path in paths:
            self.marked.add(os.fspath(path))
            self.marked.add(os.path.realpath(path))
        self.stats.clear()

    def inside(self, folder) -> List[str]:
        """The paths stat'ed strictly inside ``folder``. (A snapshot:
        realpath() itself stats.)"""
        stats = list(self.stats)
        prefix = os.path.realpath(folder) + os.sep
        return [p for p in stats if os.path.realpath(p).startswith(prefix)]


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
        assert scope == _icloud.IOPOL_SCOPE_THREAD
        return values.get(threading.get_ident(), 0)

    def set_(kind, scope, value):
        assert scope == _icloud.IOPOL_SCOPE_THREAD
        values[threading.get_ident()] = value
        return 0

    monkeypatch.setattr(_icloud, "_policy_loaded", True)
    monkeypatch.setattr(_icloud, "_policy_fns", (get, set_))
    return SimpleNamespace(
        off=lambda: values.get(threading.get_ident(), 0) == _icloud.IOPOL_MATERIALIZE_DATALESS_FILES_OFF,
        value=lambda: values.get(threading.get_ident(), 0),
    )


@pytest.fixture
def reads(monkeypatch, policy):
    """Every ``Path.read_bytes`` call as ``(path, policy was off)``."""
    seen = []
    real = pathlib.Path.read_bytes

    def read_bytes(self):
        seen.append((os.fspath(self), policy.off()))
        return real(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)
    return seen


def _icloud_bundle(lib) -> pathlib.Path:
    return _write_bundle(
        _bundle_path(lib),
        manifest=[_NAV_ITEM, _CH1_ITEM, ("ch2", "Text/ch2.xhtml", _XHTML, "")],
        files={
            "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One"), ("Text/ch2.xhtml", "Two")]),
            "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
            "OEBPS/Text/ch2.xhtml": _CHAPTER.format(title="Two"),
            "OEBPS/Text/extra.xhtml": _CHAPTER.format(title="Extra"),
        },
        spine=["ch1", "ch2"],
    )


def _assert_partial(exc: BookNotDownloadedError) -> None:
    assert type(exc) is BookNotDownloadedError
    assert str(exc) == _icloud.PARTIAL_DOWNLOAD_MESSAGE
    assert "/" not in str(exc) and "xhtml" not in str(exc)
    # Raised fresh or ``from None``: no OSError (and its path) attached.
    assert exc.__cause__ is None
    assert exc.__context__ is None or exc.__suppress_context__


class TestICloudPlaceholders:
    def test_dataless_chapter_is_never_read(self, lib, icloud, reads):
        bundle = _icloud_bundle(lib)
        chapter = bundle / "OEBPS" / "Text" / "ch2.xhtml"
        icloud.mark(chapter)
        with _fs_audit.record() as rec:
            with pytest.raises(BookNotDownloadedError) as exc:
                BookContent(bundle).list_chapters()
        _assert_partial(exc.value)
        assert os.path.realpath(chapter) not in [os.path.realpath(p) for p, _ in reads]
        assert rec.under(chapter, "open") == []

    def test_dataless_folder_is_never_looked_into(self, lib, icloud, reads):
        bundle = _icloud_bundle(lib)
        oebps = bundle / "OEBPS"
        icloud.mark(oebps)
        with _fs_audit.record() as rec:
            with pytest.raises(BookNotDownloadedError) as exc:
                BookContent(bundle).list_chapters()
        _assert_partial(exc.value)
        assert icloud.inside(oebps) == []
        assert rec.under(oebps) == []
        assert [p for p, _ in reads if "OEBPS" in p] == []

    def test_dataless_root(self, lib, icloud):
        bundle = _icloud_bundle(lib)
        icloud.mark(bundle)
        with pytest.raises(BookNotDownloadedError) as exc:
            BookContent(bundle).list_chapters()
        _assert_partial(exc.value)
        assert icloud.inside(bundle) == []

    def test_dataless_meta_inf_fails_the_drm_gate_closed(self, lib, icloud):
        bundle = _icloud_bundle(lib)
        icloud.mark(bundle / "META-INF")
        content = BookContent(bundle)
        assert content.is_drm_protected is True
        assert content._drm_evidence() == "encryption.xml"
        assert icloud.inside(bundle / "META-INF") == []
        with pytest.raises(BookNotDownloadedError):
            content.list_chapters()

    def test_dataless_encryption_xml_is_not_read(self, lib, icloud, reads):
        bundle = _icloud_bundle(lib)
        enc = bundle / "META-INF" / "encryption.xml"
        enc.write_text(_FONT_ONLY_ENCRYPTION)
        assert BookContent(bundle).is_drm_protected is False
        reads.clear()
        icloud.mark(enc)
        assert BookContent(bundle).is_drm_protected is True
        assert [p for p, _ in reads if p.endswith("encryption.xml")] == []

    def test_dataless_chapter_on_the_disk_fallback(self, lib, icloud, reads):
        bundle = _icloud_bundle(lib)
        content = BookContent(bundle)
        content.list_chapters()
        icloud.mark(bundle / "OEBPS" / "Text" / "extra.xhtml")
        reads.clear()
        with pytest.raises(BookNotDownloadedError) as exc:
            content._read_chapter_bytes("OEBPS/Text/extra.xhtml")
        _assert_partial(exc.value)
        assert reads == []

    def test_dataless_container_is_not_parsed_as_empty(self, lib, icloud):
        bundle = _icloud_bundle(lib)
        icloud.mark(bundle / "META-INF" / "container.xml")
        with pytest.raises(BookNotDownloadedError):
            _opf_dir_from_container(bundle)

    @pytest.mark.parametrize("code", [errno.EDEADLK, errno.ETIMEDOUT])
    def test_read_that_would_download_fails_with_the_fixed_message(self, lib, monkeypatch, code):
        bundle = _icloud_bundle(lib)
        real = pathlib.Path.read_bytes

        def read_bytes(self):
            if self.name in ("ch2.xhtml", "extra.xhtml", "container.xml"):
                raise OSError(code, os.strerror(code), os.fspath(self))
            return real(self)

        monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)
        for call in (
            lambda: BookContent(bundle).list_chapters(),
            lambda: BookContent(bundle).get_chapter("ch1"),
            lambda: _opf_dir_from_container(bundle),
        ):
            with pytest.raises(BookNotDownloadedError) as exc:
                call()
            _assert_partial(exc.value)
            assert "Could not read" not in str(exc.value)

    @pytest.mark.parametrize("code", [errno.EDEADLK, errno.ETIMEDOUT])
    def test_read_that_would_download_on_the_disk_fallback(self, lib, monkeypatch, code):
        bundle = _icloud_bundle(lib)
        content = BookContent(bundle)
        content.list_chapters()
        real = pathlib.Path.read_bytes

        def read_bytes(self):
            if self.name == "extra.xhtml":
                raise OSError(code, os.strerror(code), os.fspath(self))
            return real(self)

        monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)
        with pytest.raises(BookNotDownloadedError) as exc:
            content._read_chapter_bytes("OEBPS/Text/extra.xhtml")
        _assert_partial(exc.value)

    def test_spine_item_read_that_would_download(self, lib, monkeypatch):
        content = BookContent(_icloud_bundle(lib))
        item = content._load_book().get_item_with_id("ch1")

        def get_content(*args, **kwargs):
            raise OSError(errno.EDEADLK, "Resource deadlock avoided", "/x/y.xhtml")

        monkeypatch.setattr(item, "get_content", get_content)
        with pytest.raises(BookNotDownloadedError) as exc:
            content._spine_item_text("ch1")
        _assert_partial(exc.value)

    def test_encryption_xml_read_that_would_download_fails_closed(self, lib, monkeypatch):
        bundle = _icloud_bundle(lib)
        (bundle / "META-INF" / "encryption.xml").write_text(_FONT_ONLY_ENCRYPTION)
        real = pathlib.Path.read_bytes

        def read_bytes(self):
            if self.name == "encryption.xml":
                raise OSError(errno.EDEADLK, "Resource deadlock avoided")
            return real(self)

        monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)
        assert BookContent(bundle).is_drm_protected is True

    def test_every_legacy_read_runs_with_downloads_off(self, lib, reads, policy):
        bundle = _icloud_bundle(lib)
        (bundle / "META-INF" / "encryption.xml").write_text(_FONT_ONLY_ENCRYPTION)
        before = policy.value()
        content = BookContent(bundle)
        assert content.is_drm_protected is False
        assert content.list_chapters()
        assert "Legit text of Two." in content.get_chapter("ch2")
        assert "Legit text of One." in content._spine_item_text("ch1")
        content._read_chapter_bytes("OEBPS/Text/extra.xhtml")
        _opf_dir_from_container(bundle)
        assert len(reads) >= 7
        assert all(off for _, off in reads), [p for p, off in reads if not off]
        assert policy.value() == before

    def test_policy_is_restored_after_a_failed_read(self, lib, policy):
        bundle = _icloud_bundle(lib)
        (bundle / "OEBPS" / "Text" / "ch2.xhtml").unlink()
        before = policy.value()
        with pytest.raises(AppleBooksError):
            BookContent(bundle).list_chapters()
        assert policy.value() == before

    def test_stat_results_without_flags(self, lib, monkeypatch):
        # Linux stat results have no st_flags.
        class NoFlags:
            def __init__(self, st):
                self._st = st

            def __getattr__(self, name):
                if name == "st_flags":
                    raise AttributeError(name)
                return getattr(self._st, name)

        real_lstat, real_stat = _icloud.lstat, _icloud.stat
        monkeypatch.setattr(_icloud, "lstat", lambda p, **kw: NoFlags(real_lstat(p, **kw)))
        monkeypatch.setattr(_icloud, "stat", lambda p: NoFlags(real_stat(p)))
        bundle = _icloud_bundle(lib)
        content = BookContent(bundle)
        assert [c.id for c in content.list_chapters()] == ["ch1", "ch2"]
        assert "Legit text of One." in content.get_chapter("ch1")
        assert content.is_drm_protected is False
        assert _icloud.walk_bundle_local(bundle) is _icloud.FileState.LOCAL

    def test_container_is_read_once(self, lib, monkeypatch, reads):
        bundle = _icloud_bundle(lib)

        def no_reread(root):
            raise AssertionError("container.xml read again")

        monkeypatch.setattr(content_module, "_opf_dir_from_container", no_reread)
        content = BookContent(bundle)
        chapters = content.list_chapters()
        assert [c.href for c in chapters] == ["OEBPS/Text/ch1.xhtml", "OEBPS/Text/ch2.xhtml"]
        assert content._opf_dir() == pathlib.PurePosixPath("OEBPS")
        # Once, by the load itself.
        assert len([p for p, _ in reads if p.endswith("container.xml")]) == 1


# ---------------------------------------------------------------------------
# _read_entry_bytes: the per-file read of new content APIs (1.11)
# ---------------------------------------------------------------------------


class TestReadEntryBytes:
    def test_reads_the_entry(self, lib, policy, monkeypatch):
        bundle = _icloud_bundle(lib)
        expected = (bundle / "OEBPS" / "Text" / "ch1.xhtml").read_bytes()
        offs = []
        real_read = os.read

        def read(fd, n):
            offs.append(policy.off())
            return real_read(fd, n)

        with monkeypatch.context() as m:
            m.setattr(os, "read", read)
            data = _read_entry_bytes(bundle, "OEBPS/Text/ch1.xhtml", 1 << 20)
        assert data == expected
        assert offs and all(offs)
        assert _read_entry_bytes(str(bundle), "OEBPS/Text/../Text/ch1.xhtml", len(expected)) == expected
        assert _read_entry_bytes(bundle, "OEBPS/Text/ch1.xhtml", len(expected)) == expected

    def test_containment(self, lib):
        bundle = _icloud_bundle(lib)
        (bundle / "OEBPS" / "out.txt").symlink_to(lib / "outside" / "canary.txt")
        (bundle / "OEBPS" / "in.xhtml").symlink_to("Text/ch1.xhtml")
        for href in ("../../../outside/canary.txt", str(lib / "outside" / "canary.txt"),
                     "OEBPS/out.txt", "OEBPS/Text", "/dev/zero"):
            with pytest.raises(UnsafeEpubEntryError) as exc:
                _read_entry_bytes(bundle, href, 1 << 20)
            assert CANARY not in str(exc.value)
            assert exc.value.entry == href
        # A symlink inside the bundle is followed, as for 1.10's reads.
        assert b"Legit text of One." in _read_entry_bytes(bundle, "OEBPS/in.xhtml", 1 << 20)
        with pytest.raises(FileNotFoundError):
            _read_entry_bytes(bundle, "OEBPS/Text/gone.xhtml", 1 << 20)

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
    def test_fifo_fails_fast(self, lib):
        bundle = _icloud_bundle(lib)
        os.mkfifo(bundle / "OEBPS" / "pipe.xhtml")
        with pytest.raises(UnsafeEpubEntryError, match="not a regular file"):
            _call_with_timeout(lambda: _read_entry_bytes(bundle, "OEBPS/pipe.xhtml", 1 << 20), 1.0)

    def test_size_limits(self, lib, monkeypatch):
        bundle = _icloud_bundle(lib)
        size = (bundle / "OEBPS" / "Text" / "ch1.xhtml").stat().st_size
        with pytest.raises(UnsafeEpubEntryError, match=f"larger than {size - 1} bytes"):
            _read_entry_bytes(bundle, "OEBPS/Text/ch1.xhtml", size - 1)
        monkeypatch.setattr(content_module, "_MAX_ENTRY_BYTES", 4)
        with pytest.raises(UnsafeEpubEntryError, match="larger than 4 bytes"):
            _read_entry_bytes(bundle, "OEBPS/Text/ch1.xhtml", 1 << 20)

    def test_entry_growing_past_the_limit(self, lib, monkeypatch):
        bundle = _icloud_bundle(lib)
        real_fstat = os.fstat

        def fstat(fd):
            return SimpleNamespace(**{**{n: getattr(real_fstat(fd), n) for n in dir(real_fstat(fd))
                                         if n.startswith("st_")}, "st_size": 8})

        with monkeypatch.context() as m:
            m.setattr(os, "fstat", fstat)
            with pytest.raises(UnsafeEpubEntryError, match="larger than 16 bytes"):
                _read_entry_bytes(bundle, "OEBPS/Text/ch1.xhtml", 16)

    def test_dataless_entry_and_folder(self, lib, icloud, monkeypatch):
        bundle = _icloud_bundle(lib)
        opened = []
        real_open = os.open
        monkeypatch.setattr(os, "open", lambda p, *a, **k: opened.append(os.fspath(p)) or real_open(p, *a, **k))
        icloud.mark(bundle / "OEBPS" / "Text" / "ch1.xhtml")
        with pytest.raises(BookNotDownloadedError) as exc:
            _read_entry_bytes(bundle, "OEBPS/Text/ch1.xhtml", 1 << 20)
        _assert_partial(exc.value)
        assert opened == []
        icloud.mark(bundle / "OEBPS" / "Text")
        with pytest.raises(BookNotDownloadedError):
            _read_entry_bytes(bundle, "OEBPS/Text/ch2.xhtml", 1 << 20)
        assert icloud.inside(bundle / "OEBPS" / "Text") == []

    def test_read_that_would_download(self, lib, monkeypatch):
        bundle = _icloud_bundle(lib)

        def read(fd, n):
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")

        with monkeypatch.context() as m:
            m.setattr(os, "read", read)
            with pytest.raises(BookNotDownloadedError) as exc:
                _read_entry_bytes(bundle, "OEBPS/Text/ch1.xhtml", 1 << 20)
        _assert_partial(exc.value)


# ---------------------------------------------------------------------------
# Long names in messages (1.11): shortened, never longer than 300
# ---------------------------------------------------------------------------


# 5,000-character entry names that reach a real file: "./" padding is
# normalized away on disk but kept in the name the book wrote.
_PAD = "./" * 2470


def _long(rel: str) -> str:
    head, _, tail = rel.rpartition("/")
    return f"{head}/{_PAD}{tail}" if head else f"{_PAD}{tail}"


def _assert_short(exc: BaseException, cls) -> None:
    assert type(exc) is cls, type(exc)
    message = str(exc)
    assert len(message) < 300, len(message)
    assert "…" in message
    assert _PAD not in message


class TestLongNames:
    def _bundle(self, lib, href: str, extra: Optional[Dict[str, str]] = None) -> pathlib.Path:
        assert len(href) >= 4900
        return _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM, ("appx", href, "text/plain", "")],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
                **(extra or {}),
            },
            spine=["ch1"],
        )

    def test_escaping_href(self, lib):
        href = "../" * 1650 + "outside/canary.txt"
        with pytest.raises(UnsafeEpubEntryError) as exc:
            BookContent(self._bundle(lib, href)).list_chapters()
        _assert_short(exc.value, UnsafeEpubEntryError)
        assert exc.value.entry == "OEBPS/" + href
        assert "../../" in str(exc.value) and "canary.txt'" in str(exc.value)

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
    def test_fifo(self, lib):
        bundle = self._bundle(lib, _long("appx.txt"))
        os.mkfifo(bundle / "OEBPS" / "appx.txt")
        with pytest.raises(UnsafeEpubEntryError) as exc:
            _call_with_timeout(BookContent(bundle).list_chapters)
        _assert_short(exc.value, UnsafeEpubEntryError)
        assert exc.value.entry == "OEBPS/" + _long("appx.txt")
        assert "not a regular file" in str(exc.value)

    def test_oversized_sparse_file(self, lib):
        bundle = self._bundle(lib, _long("appx.txt"))
        with open(bundle / "OEBPS" / "appx.txt", "wb") as f:
            f.seek(content_module._MAX_ENTRY_BYTES)
            f.write(b"x")  # sparse, with one block at the end
        with pytest.raises(UnsafeEpubEntryError) as exc:
            BookContent(bundle).list_chapters()
        _assert_short(exc.value, UnsafeEpubEntryError)
        assert "larger than 256 MiB" in str(exc.value)
        with pytest.raises(UnsafeEpubEntryError) as exc:
            _safe_bundle_path(bundle, "OEBPS/" + _long("appx.txt"))
        _assert_short(exc.value, UnsafeEpubEntryError)

    def test_unreadable_file(self, lib):
        if os.geteuid() == 0:
            pytest.skip("root reads anything")
        bundle = self._bundle(lib, _long("appx.txt"), {"OEBPS/appx.txt": "x"})
        target = bundle / "OEBPS" / "appx.txt"
        target.chmod(0)
        try:
            with pytest.raises(AppleBooksError) as exc:
                BookContent(bundle).list_chapters()
            _assert_short(exc.value, AppleBooksError)
            assert "Permission denied" in str(exc.value)
            content = BookContent(bundle)
            with pytest.raises(AppleBooksError) as exc:
                content._read_chapter_bytes("OEBPS/" + _long("appx.txt"))
        finally:
            target.chmod(0o644)

    def test_missing_entry(self, lib):
        with pytest.raises(AppleBooksError) as exc:
            BookContent(self._bundle(lib, _long("gone.txt"))).list_chapters()
        _assert_short(exc.value, AppleBooksError)

    def test_missing_chapter_file(self, lib):
        bundle = _icloud_bundle(lib)
        content = BookContent(bundle)
        content.list_chapters()
        with pytest.raises(AppleBooksError) as exc:
            content._read_chapter_bytes("OEBPS/Text/" + _PAD + "gone.xhtml")
        _assert_short(exc.value, AppleBooksError)
        assert "missing on disk" in str(exc.value)

    def test_symlink_loop(self, lib):
        bundle = self._bundle(lib, _long("loop/x.txt"))
        (bundle / "OEBPS" / "loop").symlink_to("loop")
        with pytest.raises(UnsafeEpubEntryError) as exc:
            BookContent(bundle).list_chapters()
        _assert_short(exc.value, UnsafeEpubEntryError)
        assert "can't be resolved" in str(exc.value)

    def test_unknown_spine_entry(self, lib):
        content = BookContent(_icloud_bundle(lib))
        for call in (lambda: content.get_chapter("x" * 5000), lambda: content._spine_item_text("x" * 5000)):
            with pytest.raises(ChapterNotFoundError) as exc:
                call()
            _assert_long_free(exc.value)

    def test_unreadable_spine_entry(self, lib, monkeypatch):
        content = BookContent(_icloud_bundle(lib))
        item = content._load_book().get_item_with_id("ch1")
        home = os.path.expanduser("~")

        def get_content(*args, **kwargs):
            raise RuntimeError(f"bad bytes in {home}/" + "y" * 5000)

        monkeypatch.setattr(item, "get_content", get_content)
        with pytest.raises(AppleBooksError) as exc:
            content._spine_item_text("ch1")
        _assert_long_free(exc.value)
        assert home not in str(exc.value)
        assert str(exc.value).startswith("Could not read spine entry 'ch1': bad bytes in ~/")

    def test_long_bundle_and_file_names(self, lib):
        name = "B" * 240 + ".epub"
        bundle = _write_bundle(lib / "a" / name, manifest=[], files={}, spine=[])
        (bundle / "OEBPS" / "content.opf").write_text("<package")
        with pytest.raises(AppleBooksError) as exc:
            BookContent(bundle).list_chapters()
        _assert_long_free(exc.value)
        assert str(exc.value).startswith("Could not read EPUB '" + "B" * 60 + "…")
        other = lib / "a" / ("N" * 240 + ".txt")
        other.write_text("x")
        with pytest.raises(NotEpubError) as exc:
            BookContent(other).list_chapters()
        _assert_long_free(exc.value)


def _assert_long_free(exc: BaseException) -> None:
    message = str(exc)
    assert len(message) < 300, len(message)
    assert "…" in message


# ---------------------------------------------------------------------------
# Names of up to 80 characters: messages exactly as 1.10 wrote them
# ---------------------------------------------------------------------------


# With "OEBPS/" in front, the longest is exactly 80 characters.
_SHORT_NAMES = ["appx.txt", "it's.txt", "n" * 70 + ".txt"]


class TestShortNamesUnchanged:
    """The 1.10 message for each site, built with 1.10's formatting
    (``{name!r}``, ``'{title}'``), for names of at most 80 characters."""

    def _bundle(self, lib, href, extra=None):
        return _write_bundle(
            _bundle_path(lib),
            manifest=[_NAV_ITEM, _CH1_ITEM, ("appx", href, "text/plain", "")],
            files={
                "OEBPS/nav.xhtml": _nav([("Text/ch1.xhtml", "One")]),
                "OEBPS/Text/ch1.xhtml": _CHAPTER.format(title="One"),
                **(extra or {}),
            },
            spine=["ch1"],
        )

    @pytest.mark.parametrize("name", _SHORT_NAMES)
    def test_entry_sites(self, lib, name, monkeypatch):
        entry = f"OEBPS/{name}"
        bundle = self._bundle(lib, name)
        # Missing.
        with pytest.raises(AppleBooksError) as exc:
            BookContent(bundle).list_chapters()
        assert str(exc.value) == f"Could not read EPUB entry {entry!r}: No such file or directory"
        # Not a regular file.
        (bundle / "OEBPS" / name).mkdir()
        with pytest.raises(UnsafeEpubEntryError) as exc:
            BookContent(bundle).list_chapters()
        assert str(exc.value) == f"EPUB entry {entry!r} is not a regular file."
        with pytest.raises(UnsafeEpubEntryError) as exc:
            _safe_bundle_path(bundle, entry)
        assert str(exc.value) == f"EPUB entry {entry!r} is not a regular file."
        (bundle / "OEBPS" / name).rmdir()
        # Too large.
        (bundle / "OEBPS" / name).write_text("x" * 20)
        monkeypatch.setattr(content_module, "_MAX_ENTRY_BYTES", 10 * 1024 * 1024)
        with open(bundle / "OEBPS" / name, "ab") as f:
            f.truncate(11 * 1024 * 1024)
        with pytest.raises(UnsafeEpubEntryError) as exc:
            BookContent(bundle).list_chapters()
        assert str(exc.value) == f"EPUB entry {entry!r} is larger than 10 MiB."
        with pytest.raises(UnsafeEpubEntryError) as exc:
            _safe_bundle_path(bundle, entry)
        assert str(exc.value) == f"EPUB entry {entry!r} is larger than 10 MiB."
        # Outside the bundle.
        (bundle / "OEBPS" / name).unlink()
        (bundle / "OEBPS" / name).symlink_to(lib / "outside" / "canary.txt")
        with pytest.raises(UnsafeEpubEntryError) as exc:
            BookContent(bundle).list_chapters()
        assert str(exc.value) == f"EPUB entry {entry!r} points outside the book bundle."
        assert exc.value.entry == entry
        # Can't be resolved.
        (bundle / "OEBPS" / name).unlink()
        (bundle / "OEBPS" / name).symlink_to(name)
        with pytest.raises(UnsafeEpubEntryError) as exc:
            BookContent(bundle).list_chapters()
        assert str(exc.value) == f"EPUB entry {entry!r} can't be resolved inside the book bundle."

    @pytest.mark.parametrize("name", _SHORT_NAMES)
    def test_chapter_and_spine_sites(self, lib, name):
        bundle = self._bundle(lib, "Text/ch1.xhtml")
        content = BookContent(bundle)
        content.list_chapters()
        href = f"OEBPS/{name}"  # 80 characters at most
        with pytest.raises(AppleBooksError) as exc:
            content._read_chapter_bytes(href)
        assert str(exc.value) == (
            f"Chapter file {href!r} is declared in the EPUB manifest but missing on disk.")
        with pytest.raises(ChapterNotFoundError) as exc:
            content.get_chapter(name)
        assert str(exc.value) == (
            f"No chapter or spine entry with id {name!r} in this book. Pass an id from "
            f"the book's table of contents, or a chapter's 1-based order (e.g. \"5\").")

    @pytest.mark.parametrize("stem", ["Book", "it's", "b" * 70])
    def test_book_name_sites(self, lib, stem):
        bundle = _write_bundle(lib / "a" / f"{stem}.epub", manifest=[], files={}, spine=[])
        (bundle / "OEBPS" / "content.opf").write_text("<package")
        with pytest.raises(AppleBooksError) as exc:
            BookContent(bundle).list_chapters()
        assert str(exc.value).startswith(f"Could not read EPUB '{stem}.epub': ")
        other = lib / "a" / f"{stem}.txt"
        other.write_text("x")
        with pytest.raises(NotEpubError) as exc:
            BookContent(other).list_chapters()
        assert str(exc.value) == (
            f"'{stem}.txt' is not an EPUB bundle directory; chapter "
            f"listing/reading is only supported for EPUB books.")
