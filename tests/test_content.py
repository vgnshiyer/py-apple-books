"""Tests for py_apple_books.content.

Split into four layers:

* **Pure-function tests** for helpers that don't touch the filesystem or
  ebooklib (NCX parsing, text extraction, whitespace normalization).
* **is_downloaded tests** using real temp files and monkey-patched stat
  results to simulate iCloud placeholders without needing actual
  iCloud-synced files.
* **BookContent tests** using the generated EPUB fixtures from
  ``conftest.py``.
* **Facade tests** for the content-backed :class:`PyAppleBooks` methods
  (DRM error wording, annotation context), with the database lookups
  stubbed out.

Bundle-containment (security) tests live in ``test_content_security.py``.
"""

from __future__ import annotations

import os
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.content import (
    BookContent,
    Chapter,
    _parse_ncx_bytes,
    is_downloaded,
)
from py_apple_books.exceptions import AppleBooksError, DRMProtectedError
from py_apple_books.models.location import Location
from py_apple_books.utils import extract_chapter_text, normalize_whitespace


# ---------------------------------------------------------------------------
# _normalize_whitespace
# ---------------------------------------------------------------------------


class TestNormalizeWhitespace:
    def test_empty_string(self):
        assert normalize_whitespace("") == ""

    def test_whitespace_only_string(self):
        assert normalize_whitespace("  \t\n  ") == ""

    def test_collapses_runs_of_spaces(self):
        assert normalize_whitespace("hello    world") == "hello world"

    def test_collapses_tabs(self):
        assert normalize_whitespace("hello\t\tworld") == "hello world"

    def test_preserves_single_paragraph_break(self):
        assert normalize_whitespace("para 1\n\npara 2") == "para 1\n\npara 2"

    def test_collapses_multiple_blank_lines(self):
        assert (
            normalize_whitespace("para 1\n\n\n\n\npara 2") == "para 1\n\npara 2"
        )

    def test_strips_leading_and_trailing(self):
        assert normalize_whitespace("\n\n  hello world  \n\n") == "hello world"


# ---------------------------------------------------------------------------
# _parse_ncx_bytes
# ---------------------------------------------------------------------------


def _ncx(body: str) -> bytes:
    """Wrap an NCX navMap body into a full NCX XML document."""
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        b"<navMap>\n"
        + body.encode("utf-8")
        + b"\n</navMap>\n</ncx>\n"
    )


class TestParseNcxBytes:
    def test_empty_bytes_returns_empty(self):
        assert _parse_ncx_bytes(b"", pathlib.PurePosixPath("OEBPS")) == []

    def test_malformed_xml_returns_empty(self):
        assert _parse_ncx_bytes(b"<not valid", pathlib.PurePosixPath("")) == []

    @pytest.mark.parametrize("encoding", ["Shift_JIS", "bogus"])
    def test_undecodable_declared_encoding_returns_empty(self, encoding):
        # expat raises ValueError / LookupError here, not ParseError.
        xml = _ncx("").replace(b"UTF-8", encoding.encode())
        assert _parse_ncx_bytes(xml, pathlib.PurePosixPath("OEBPS")) == []

    def test_missing_navmap_returns_empty(self):
        xml = (
            b'<?xml version="1.0"?>\n'
            b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"></ncx>'
        )
        assert _parse_ncx_bytes(xml, pathlib.PurePosixPath("OEBPS")) == []

    def test_single_navpoint_flat(self):
        ncx = _ncx(
            '<navPoint id="np-1" playOrder="1">'
            "  <navLabel><text>Chapter One</text></navLabel>"
            '  <content src="ch1.xhtml"/>'
            "</navPoint>"
        )
        chapters = _parse_ncx_bytes(ncx, pathlib.PurePosixPath("OEBPS"))
        assert len(chapters) == 1
        assert chapters[0].title == "Chapter One"
        assert chapters[0].href == "OEBPS/ch1.xhtml"
        assert chapters[0].fragment == ""
        assert chapters[0].order == 1
        assert chapters[0].depth == 0
        assert chapters[0].id == "np-1"

    def test_fragment_is_split_from_href(self):
        ncx = _ncx(
            '<navPoint id="np-1" playOrder="1">'
            "  <navLabel><text>Chapter One</text></navLabel>"
            '  <content src="ch1.xhtml#section-a"/>'
            "</navPoint>"
        )
        chapters = _parse_ncx_bytes(ncx, pathlib.PurePosixPath(""))
        assert chapters[0].href == "ch1.xhtml"
        assert chapters[0].fragment == "section-a"

    def test_nested_navpoints_preserve_depth(self):
        ncx = _ncx(
            '<navPoint id="np-1"><navLabel><text>Part I</text></navLabel>'
            '  <content src="p1.xhtml"/>'
            '  <navPoint id="np-2"><navLabel><text>Ch 1</text></navLabel>'
            '    <content src="p1.xhtml#ch1"/>'
            "  </navPoint>"
            "</navPoint>"
            '<navPoint id="np-3"><navLabel><text>Part II</text></navLabel>'
            '  <content src="p2.xhtml"/>'
            "</navPoint>"
        )
        chapters = _parse_ncx_bytes(ncx, pathlib.PurePosixPath(""))
        titles = [(c.title, c.depth, c.order) for c in chapters]
        assert titles == [
            ("Part I", 0, 1),
            ("Ch 1", 1, 2),
            ("Part II", 0, 3),
        ]

    def test_duplicate_navpoint_ids_fall_back_to_order(self):
        # Real-world NCX quirk: 4-Hour Workweek's NCX has 14 navPoints
        # all with id="bm1".
        ncx = _ncx(
            '<navPoint id="bm1"><navLabel><text>First</text></navLabel>'
            '  <content src="a.xhtml"/></navPoint>'
            '<navPoint id="bm1"><navLabel><text>Second</text></navLabel>'
            '  <content src="b.xhtml"/></navPoint>'
        )
        chapters = _parse_ncx_bytes(ncx, pathlib.PurePosixPath(""))
        # Dup id collapses to numeric ``order`` so Chapter.id stays unique.
        assert [c.id for c in chapters] == ["1", "2"]

    def test_unique_ids_preserved_when_no_collision(self):
        ncx = _ncx(
            '<navPoint id="alpha"><navLabel><text>A</text></navLabel>'
            '  <content src="a.xhtml"/></navPoint>'
            '<navPoint id="beta"><navLabel><text>B</text></navLabel>'
            '  <content src="b.xhtml"/></navPoint>'
        )
        chapters = _parse_ncx_bytes(ncx, pathlib.PurePosixPath(""))
        assert [c.id for c in chapters] == ["alpha", "beta"]

    def test_url_encoded_src_is_decoded(self):
        # Some EPUBs percent-encode file names, e.g. part%21one_split_002.html
        ncx = _ncx(
            '<navPoint id="np-1"><navLabel><text>Ch</text></navLabel>'
            '  <content src="dir%2Fweird%21file.xhtml"/></navPoint>'
        )
        chapters = _parse_ncx_bytes(ncx, pathlib.PurePosixPath(""))
        assert chapters[0].href == "dir/weird!file.xhtml"

    def test_missing_label_or_content_skipped(self):
        ncx = _ncx(
            "<navPoint><content src=\"a.xhtml\"/></navPoint>"
            '<navPoint><navLabel><text>OK</text></navLabel>'
            '  <content src="b.xhtml"/></navPoint>'
        )
        chapters = _parse_ncx_bytes(ncx, pathlib.PurePosixPath(""))
        assert [c.title for c in chapters] == ["OK"]


# ---------------------------------------------------------------------------
# _extract_chapter_text
# ---------------------------------------------------------------------------


class TestExtractChapterText:
    def test_simple_paragraph(self):
        html = b"<html><body><p>Hello world</p></body></html>"
        assert extract_chapter_text(html, None, set()) == "Hello world"

    def test_multiple_paragraphs_get_paragraph_breaks(self):
        html = b"<html><body><p>First</p><p>Second</p></body></html>"
        assert extract_chapter_text(html, None, set()) == "First\n\nSecond"

    def test_script_and_style_are_stripped(self):
        html = (
            b"<html><head><title>ignore</title></head>"
            b"<body>"
            b"<script>var x = 1;</script>"
            b"<style>p { color: red; }</style>"
            b"<p>Visible text</p>"
            b"</body></html>"
        )
        out = extract_chapter_text(html, None, set())
        assert "Visible text" in out
        assert "var x" not in out
        assert "color: red" not in out
        assert "ignore" not in out

    def test_inline_tags_stay_inline(self):
        html = b"<html><body><p>foo <em>bar</em> baz</p></body></html>"
        assert extract_chapter_text(html, None, set()) == "foo bar baz"

    def test_xml_declaration_handled(self):
        html = (
            b'<?xml version="1.0"?>\n'
            b'<html xmlns="http://www.w3.org/1999/xhtml">'
            b"<head><title>T</title></head>"
            b"<body><p>Hi</p></body></html>"
        )
        assert extract_chapter_text(html, None, set()) == "Hi"

    def test_void_elements_do_not_suppress_body(self):
        # Regression for the <meta>/<link> bug that suppressed all body
        # content because void tags were being tracked as skip-depth
        # containers.
        html = (
            b"<html><head>"
            b'<meta charset="utf-8"/>'
            b'<link rel="stylesheet" href="x.css"/>'
            b'<meta name="x" content="y"/>'
            b"</head><body>"
            b"<p>Real content</p>"
            b"</body></html>"
        )
        assert "Real content" in extract_chapter_text(html, None, set())

    def test_start_anchor_on_empty_a_inside_paragraph(self):
        # Regression for "The Goal" / Introduction. The anchor is an
        # empty <a id="p4"/> nested inside a <p>; collecting only the
        # sibling chain of the <a> would return nothing. The
        # document-order walk picks up subsequent siblings of the
        # containing element.
        html = (
            b"<html><body>"
            b'<p class="heading"><a id="p4"/>1</p>'
            b"<p>The real opening paragraph of the introduction.</p>"
            b"<p>And a follow-up paragraph too.</p>"
            b"</body></html>"
        )
        out = extract_chapter_text(html, "p4", set())
        assert "real opening paragraph" in out
        assert "follow-up paragraph" in out

    def test_fragment_scoping_stops_at_sibling_anchor(self):
        # Project-Gutenberg layout: multiple sections share one file,
        # each anchored by a hidden <a>. Requesting section B's text
        # should not include section A's or section C's content.
        html = (
            b"<html><body>"
            b'<a id="A"/><p>Text in section A</p>'
            b'<a id="B"/><p>Text in section B</p>'
            b'<a id="C"/><p>Text in section C</p>'
            b"</body></html>"
        )
        out = extract_chapter_text(html, "B", {"A", "C"})
        assert "section B" in out
        assert "section A" not in out
        assert "section C" not in out

    def test_missing_start_anchor_falls_back_to_whole_document(self):
        html = b"<html><body><p>Hello</p></body></html>"
        out = extract_chapter_text(html, "nonexistent", set())
        assert out == "Hello"

    def test_image_only_body_returns_empty(self):
        # A real pattern: a part title page ("Part 1 Introduction") is
        # rendered as a single <img>. We can't extract text from that —
        # an empty return is correct.
        html = (
            b"<html><body><div>"
            b'<img alt="Part 1" src="img.jpg"/>'
            b"</div></body></html>"
        )
        assert extract_chapter_text(html, None, set()) == ""

    def test_fragment_scoping_drops_comments(self):
        # Regression: the fragment path kept HTML comments (bs4 Comment
        # subclasses NavigableString) while the whole-file path dropped
        # them — leaking commented-out markup into 29 real chapters.
        html = (
            b"<html><body>"
            b'<a id="A"/><p>Text <!-- editor note --> in A</p>'
            b"<?pi instruction?>"
            b'<a id="B"/><p>Text in B</p>'
            b"</body></html>"
        )
        out = extract_chapter_text(html, "A", {"B"})
        assert out == "Text in A"
        assert "editor note" not in out
        assert "instruction" not in out

    def test_fragment_scoping_drops_ruby_annotations(self):
        # Furigana in <rt>/<rp> are dropped by get_text() on the
        # whole-file path; the fragment path must agree.
        body = (
            "<p><ruby>漢<rp>(</rp><rt>かん</rt><rp>)</rp></ruby>"
            "<ruby>字<rp>(</rp><rt>じ</rt><rp>)</rp></ruby>を読む。</p>"
        )
        whole = extract_chapter_text(
            f"<html><body>{body}</body></html>".encode(), None, set()
        )
        fragment = extract_chapter_text(
            f'<html><body><a id="s"/>{body}</body></html>'.encode(), "s", set()
        )
        assert whole == fragment == "漢字を読む。"

    def test_fragment_and_whole_file_paths_agree(self):
        body = (
            "<p>One <![CDATA[cdata]]> two</p><!-- hidden -->"
            "<template><p>tpl</p></template><p>three</p>"
        )
        whole = extract_chapter_text(
            f"<html><body>{body}</body></html>".encode(), None, set()
        )
        fragment = extract_chapter_text(
            f'<html><body><a id="s"/>{body}</body></html>'.encode(), "s", set()
        )
        assert fragment == whole
        assert "cdata" in whole
        assert "hidden" not in whole and "tpl" not in whole


# ---------------------------------------------------------------------------
# is_downloaded
# ---------------------------------------------------------------------------


class TestIsDownloaded:
    def test_nonexistent_path_returns_false(self, tmp_path):
        assert is_downloaded(tmp_path / "does-not-exist") is False

    def test_regular_file_with_content_returns_true(self, tmp_path):
        f = tmp_path / "book.pdf"
        f.write_bytes(b"x" * 4096)
        assert is_downloaded(f) is True

    def test_directory_with_content_returns_true(self, tmp_path):
        d = tmp_path / "book.epub"
        d.mkdir()
        # >4KB so it clears the placeholder threshold.
        (d / "data").write_bytes(b"x" * 8192)
        assert is_downloaded(d) is True

    def test_placeholder_file_mocked_stat_blocks_zero(self, tmp_path):
        """Single-file iCloud placeholders: ``st_blocks == 0`` with
        ``st_size > 0``. We can't fabricate that on a real filesystem,
        so we monkey-patch :meth:`pathlib.Path.stat` to return a fake
        stat-result with zeroed blocks."""
        f = tmp_path / "book.pdf"
        f.write_bytes(b"real bytes")

        real_stat = f.stat()
        fake = os.stat_result(
            (
                real_stat.st_mode,
                real_stat.st_ino,
                real_stat.st_dev,
                real_stat.st_nlink,
                real_stat.st_uid,
                real_stat.st_gid,
                1024 * 1024,  # non-zero logical size
                real_stat.st_atime,
                real_stat.st_mtime,
                real_stat.st_ctime,
            )
        )
        # Inject st_blocks=0 via a custom object so the attribute works
        # even if the platform's stat_result doesn't accept block override.
        class _FakeStat:
            def __init__(self, inner, size, blocks):
                self._inner = inner
                self.st_size = size
                self.st_blocks = blocks

            def __getattr__(self, name):
                return getattr(self._inner, name)

        fake_stat = _FakeStat(real_stat, 1024 * 1024, 0)

        with patch.object(pathlib.Path, "stat", return_value=fake_stat):
            assert is_downloaded(f) is False

    def test_placeholder_epub_mocked_du_empty(self, tmp_path):
        """Bundle-directory iCloud placeholders report 0 KB via ``du -sk``
        even when the directory appears materialized. We simulate by
        intercepting the subprocess call."""
        d = tmp_path / "book.epub"
        d.mkdir()
        (d / "file").write_bytes(b"x" * 100)  # not enough to matter

        import subprocess as _sp
        fake_run = _sp.CompletedProcess(
            args=["du", "-sk", str(d)], returncode=0, stdout="0\t" + str(d) + "\n"
        )
        with patch("py_apple_books.content.subprocess.run", return_value=fake_run):
            assert is_downloaded(d) is False

    def test_du_failure_fails_open(self, tmp_path):
        """When ``du`` can't be invoked, we err on the side of letting
        the read proceed (fail open) rather than incorrectly reporting
        the book as a placeholder."""
        d = tmp_path / "book.epub"
        d.mkdir()
        (d / "file").write_bytes(b"x" * 8192)

        with patch(
            "py_apple_books.content.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            assert is_downloaded(d) is True


# ---------------------------------------------------------------------------
# BookContent — integration tests using the generated EPUB fixture
# ---------------------------------------------------------------------------


class TestBookContentProperties:
    def test_is_epub_for_epub_directory(self, simple_epub):
        content = BookContent(simple_epub.path)
        assert content.is_epub is True
        assert content.is_pdf is False

    def test_is_pdf_for_pdf_file(self, tmp_path):
        f = tmp_path / "book.pdf"
        f.write_bytes(b"%PDF-1.4\n")
        content = BookContent(f)
        assert content.is_pdf is True
        assert content.is_epub is False

    def test_is_drm_protected_false_when_no_encryption_xml(self, simple_epub):
        assert BookContent(simple_epub.path).is_drm_protected is False

    def test_is_drm_protected_true_when_encryption_xml_encrypts_content(
        self, simple_epub
    ):
        _write_encryption_xml(
            simple_epub.path,
            [("http://www.w3.org/2001/04/xmlenc#aes128-cbc", "EPUB/chap1.xhtml")],
        )
        assert BookContent(simple_epub.path).is_drm_protected is True

    def test_is_drm_protected_false_for_empty_encryption_xml(self, simple_epub):
        # Well-formed but lists no encrypted resources: nothing is
        # hidden, so the book stays readable. (Before 1.9.1 the mere
        # presence of the file counted as DRM.)
        meta_inf = simple_epub.path / "META-INF"
        (meta_inf / "encryption.xml").write_text(
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"/>'
        )
        assert BookContent(simple_epub.path).is_drm_protected is False

    @pytest.mark.parametrize(
        "algorithm",
        ["http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"],
        ids=["idpf", "adobe"],
    )
    def test_is_drm_protected_false_for_font_obfuscation_only(
        self, simple_epub, algorithm
    ):
        _write_encryption_xml(
            simple_epub.path,
            [(algorithm, "EPUB/fonts/a.otf"), (algorithm, "EPUB/fonts/b.TTF")],
        )
        content = BookContent(simple_epub.path)
        assert content.is_drm_protected is False
        # ...and the text really is readable.
        assert "Body paragraph for Chapter 1" in content.get_chapter("1")

    def test_is_drm_protected_true_when_any_entry_is_not_obfuscation(
        self, simple_epub
    ):
        _write_encryption_xml(
            simple_epub.path,
            [
                ("http://www.idpf.org/2008/embedding", "EPUB/fonts/a.otf"),
                ("http://www.w3.org/2001/04/xmlenc#aes256-cbc", "EPUB/chap2.xhtml"),
            ],
        )
        assert BookContent(simple_epub.path).is_drm_protected is True

    def test_is_drm_protected_true_when_algorithm_missing(self, simple_epub):
        _write_encryption_xml(simple_epub.path, [(None, "EPUB/chap1.xhtml")])
        assert BookContent(simple_epub.path).is_drm_protected is True

    def test_is_drm_protected_true_for_malformed_encryption_xml(self, simple_epub):
        (simple_epub.path / "META-INF" / "encryption.xml").write_text(
            "<encryption><EncryptedData"
        )
        assert BookContent(simple_epub.path).is_drm_protected is True

    @pytest.mark.parametrize("encoding", ["Shift_JIS", "bogus"])
    def test_is_drm_protected_true_for_undecodable_encryption_xml(
        self, simple_epub, encoding
    ):
        # Font obfuscation only, but in an encoding expat refuses with
        # ValueError / LookupError: fail closed rather than raise.
        _write_encryption_xml(
            simple_epub.path,
            [("http://www.idpf.org/2008/embedding", "EPUB/f.otf")],
            encoding=encoding,
        )
        assert BookContent(simple_epub.path).is_drm_protected is True

    @pytest.mark.parametrize(
        "root",
        [
            "<encryption>",
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">',
        ],
        ids=["no-namespace", "container-namespace"],
    )
    def test_is_drm_protected_true_without_xmlenc_namespace(
        self, simple_epub, root
    ):
        (simple_epub.path / "META-INF" / "encryption.xml").write_text(
            root
            + "<EncryptedData><EncryptionMethod "
            'Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
            '<CipherData><CipherReference URI="EPUB/chap1.xhtml"/></CipherData>'
            "</EncryptedData></encryption>"
        )
        assert BookContent(simple_epub.path).is_drm_protected is True

    @pytest.mark.parametrize("name", ["sinf.xml", "rights.xml"])
    def test_is_drm_protected_true_for_license_files(self, simple_epub, name):
        (simple_epub.path / "META-INF" / name).write_text("<x/>")
        content = BookContent(simple_epub.path)
        assert content.is_drm_protected is True
        assert content._drm_evidence() == name

    def test_is_drm_protected_false_for_pdf(self, tmp_path):
        f = tmp_path / "book.pdf"
        f.write_bytes(b"%PDF-1.4\n")
        assert BookContent(f).is_drm_protected is False


def _write_encryption_xml(
    bundle: pathlib.Path, entries, encoding: str = "utf-8"
) -> None:
    """Write ``META-INF/encryption.xml`` with one ``EncryptedData`` per
    ``(algorithm, uri)`` entry; ``algorithm=None`` omits the
    ``EncryptionMethod``. ``encoding`` only goes into the XML
    declaration (the content is ASCII)."""
    blocks = []
    for algorithm, uri in entries:
        method = (
            f'<enc:EncryptionMethod Algorithm="{algorithm}"/>' if algorithm else ""
        )
        blocks.append(
            f"<enc:EncryptedData>{method}<enc:CipherData>"
            f'<enc:CipherReference URI="{uri}"/></enc:CipherData>'
            f"</enc:EncryptedData>"
        )
    (bundle / "META-INF" / "encryption.xml").write_text(
        f'<?xml version="1.0" encoding="{encoding}"?>\n'
        '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        'xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
        + "".join(blocks)
        + "</encryption>"
    )


class TestGetBookContentDrmMessage:
    """``PyAppleBooks.get_book_content`` words its DRM error from the
    evidence: FairPlay only when ``sinf.xml`` proves a Store purchase."""

    def _api_for(self, bundle):
        api = PyAppleBooks()
        api.get_book_by_id = lambda book_id: SimpleNamespace(
            title="Some Book", path=str(bundle)
        )
        return api

    def test_fairplay_message_needs_sinf(self, simple_epub):
        (simple_epub.path / "META-INF" / "sinf.xml").write_text("<x/>")
        with patch.object(BookContent, "is_downloaded", new=True):
            with pytest.raises(DRMProtectedError, match=r"\(FairPlay\)"):
                self._api_for(simple_epub.path).get_book_content(1)

    def test_other_drm_is_an_encrypted_epub(self, simple_epub):
        _write_encryption_xml(
            simple_epub.path,
            [("http://www.w3.org/2001/04/xmlenc#aes128-cbc", "EPUB/chap1.xhtml")],
        )
        with patch.object(BookContent, "is_downloaded", new=True):
            with pytest.raises(DRMProtectedError) as exc:
                self._api_for(simple_epub.path).get_book_content(1)
        assert "encrypted EPUB (DRM)" in str(exc.value)
        assert "FairPlay" not in str(exc.value)

    @pytest.mark.parametrize("encoding", ["Shift_JIS", "bogus"])
    def test_undecodable_encryption_xml_is_refused_not_raised(
        self, simple_epub, encoding
    ):
        _write_encryption_xml(
            simple_epub.path,
            [("http://www.idpf.org/2008/embedding", "EPUB/f.otf")],
            encoding=encoding,
        )
        with patch.object(BookContent, "is_downloaded", new=True):
            with pytest.raises(DRMProtectedError, match="encrypted EPUB"):
                self._api_for(simple_epub.path).get_book_content(1)

    def test_font_obfuscated_book_is_returned(self, simple_epub):
        _write_encryption_xml(
            simple_epub.path, [("http://www.idpf.org/2008/embedding", "EPUB/f.otf")]
        )
        with patch.object(BookContent, "is_downloaded", new=True):
            content = self._api_for(simple_epub.path).get_book_content(1)
        assert content.list_chapters()


class TestListChapters:
    def test_basic_three_chapter_epub(self, simple_epub):
        chapters = BookContent(simple_epub.path).list_chapters()
        assert [c.title for c in chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
        assert [c.order for c in chapters] == [1, 2, 3]
        assert all(c.depth == 0 for c in chapters)

    def test_hrefs_point_to_real_files(self, simple_epub):
        content = BookContent(simple_epub.path)
        for ch in content.list_chapters():
            assert (content.path / ch.href).exists(), (
                f"chapter href {ch.href!r} does not resolve to a real file"
            )

    def test_chapter_ids_are_unique(self, simple_epub):
        chapters = BookContent(simple_epub.path).list_chapters()
        ids = [c.id for c in chapters]
        assert len(set(ids)) == len(ids)

    def test_raises_for_non_epub(self, tmp_path):
        f = tmp_path / "book.pdf"
        f.write_bytes(b"%PDF-1.4\n")
        with pytest.raises(AppleBooksError):
            BookContent(f).list_chapters()

    def test_custom_chapter_titles(self, epub_factory):
        built = epub_factory(
            chapter_titles=["Prologue", "Chapter Alpha", "Epilogue"]
        )
        titles = [c.title for c in BookContent(built.path).list_chapters()]
        assert titles == ["Prologue", "Chapter Alpha", "Epilogue"]


class TestGetChapter:
    def test_by_chapter_id(self, simple_epub):
        content = BookContent(simple_epub.path)
        chapters = content.list_chapters()
        text = content.get_chapter(chapters[0].id)
        assert "Chapter 1" in text
        assert "Body paragraph for Chapter 1" in text

    def test_by_order_as_string(self, simple_epub):
        """The id parameter also accepts ``str(order)`` as a fallback
        lookup for callers that only have a position."""
        content = BookContent(simple_epub.path)
        text = content.get_chapter("2")
        assert "Chapter 2" in text

    def test_paragraph_breaks_preserved(self, simple_epub):
        content = BookContent(simple_epub.path)
        text = content.get_chapter(content.list_chapters()[0].id)
        # Two <p> tags should produce a paragraph break.
        assert "\n\n" in text

    def test_raises_on_unknown_chapter(self, simple_epub):
        with pytest.raises(AppleBooksError):
            BookContent(simple_epub.path).get_chapter("nonexistent")

    def test_raises_for_non_epub(self, tmp_path):
        f = tmp_path / "book.pdf"
        f.write_bytes(b"%PDF-1.4\n")
        with pytest.raises(AppleBooksError):
            BookContent(f).get_chapter("any")

    def test_accepts_any_spine_entry_id(self, simple_epub):
        """``get_chapter`` must also accept manifest ids that aren't in
        the ToC — e.g. sub-section spine entries. The generated fixture
        doesn't have those, so we verify the general contract: every
        manifest item id in the spine is fetchable."""
        content = BookContent(simple_epub.path)
        book = content._load_book()
        spine_ids = [e[0] for e in book.spine if isinstance(e, tuple)]
        # Every spine id should be fetchable (no raise).
        for sid in spine_ids:
            text = content.get_chapter(sid)
            assert isinstance(text, str)


# ---------------------------------------------------------------------------
# PyAppleBooks.get_annotation_surrounding_text
# ---------------------------------------------------------------------------


_ANCHORED_CHAPTER = (
    "<html><body>"
    "<p>Opening paragraph of the file.</p>"
    "<p>The reader highlighted this sentence\n    which wraps across lines.</p>"
    "<p>Closing words of the chapter.</p>"
    '<h2 id="next">Next Chapter Heading</h2>'
    "</body></html>"
)


@pytest.fixture
def anchored_epub(epub_factory):
    """Calibre-split layout: chapter 1's only ToC entry points at an
    anchor at the *end* of its file, so fragment-scoped
    :meth:`BookContent.get_chapter` text starts after everything the
    reader highlighted."""
    built = epub_factory()
    root = built.path / "EPUB"
    (root / "chap1.xhtml").write_text(_ANCHORED_CHAPTER)
    nav = root / "nav.xhtml"
    nav.write_text(
        nav.read_text().replace('href="chap1.xhtml"', 'href="chap1.xhtml#next"')
    )
    return built


class TestAnnotationSurroundingText:
    def _api(self, bundle, selected_text, representative_text=None):
        content = BookContent(bundle)
        item_id = content._load_book().get_item_with_href("chap1.xhtml").get_id()
        annotation = SimpleNamespace(
            location=Location(f"epubcfi(/6/4[{item_id}]!/4/4/1,:4,:30)"),
            selected_text=selected_text,
            representative_text=representative_text,
            book=SimpleNamespace(id=1),
        )
        api = PyAppleBooks()
        api.get_annotation_by_id = lambda annotation_id: annotation
        api.get_book_content = lambda book_id: BookContent(bundle)
        return api, content, item_id

    def test_finds_highlight_before_fragment_anchor(self, anchored_epub):
        # Stored selected_text keeps the EPUB's line breaks; extraction
        # collapses them, so an exact find() would miss.
        selected = "highlighted this sentence\n    which wraps across lines."
        api, content, item_id = self._api(anchored_epub.path, selected)
        assert content.get_chapter(item_id) == "Next Chapter Heading"

        window = api.get_annotation_surrounding_text(1)
        assert "highlighted this sentence which wraps across lines." in window
        assert "Opening paragraph" in window
        # MCP 0.8.1 wraps the highlight with the same token regex.
        pattern = r"\s+".join(re.escape(t) for t in selected.split())
        assert re.search(pattern, window)

    def test_window_is_snapped_around_the_highlight(self, anchored_epub):
        api, _, _ = self._api(anchored_epub.path, "Closing words")
        window = api.get_annotation_surrounding_text(1, chars_before=10, chars_after=10)
        assert window.startswith("…") and window.endswith("…")
        assert "Closing words" in window
        assert "Opening paragraph" not in window

    def test_miss_returns_empty_not_chapter_opening(self, anchored_epub):
        api, _, _ = self._api(anchored_epub.path, "words that are not in the book")
        assert api.get_annotation_surrounding_text(1) == ""

    def test_annotation_without_text_returns_empty(self, anchored_epub):
        api, _, _ = self._api(anchored_epub.path, "", representative_text="  ")
        assert api.get_annotation_surrounding_text(1) == ""

    def test_falls_back_to_representative_text(self, anchored_epub):
        api, _, _ = self._api(
            anchored_epub.path, None, representative_text="Opening paragraph"
        )
        assert "Opening paragraph" in api.get_annotation_surrounding_text(1)

    def test_regex_metacharacters_are_literal(self, anchored_epub):
        api, _, _ = self._api(anchored_epub.path, "lines.) (Closing")
        assert api.get_annotation_surrounding_text(1) == ""


# ---------------------------------------------------------------------------
# Chapter dataclass sanity
# ---------------------------------------------------------------------------


class TestChapterDataclass:
    def test_frozen(self):
        ch = Chapter(id="a", title="A", href="a.xhtml", fragment="", order=1, depth=0)
        with pytest.raises(AttributeError):
            ch.title = "B"  # type: ignore[misc]

    def test_equality(self):
        a = Chapter(id="a", title="A", href="a.xhtml", fragment="", order=1, depth=0)
        b = Chapter(id="a", title="A", href="a.xhtml", fragment="", order=1, depth=0)
        assert a == b
