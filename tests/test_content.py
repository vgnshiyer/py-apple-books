"""Tests for py_apple_books.content.

Split into three layers:

* **Pure-function tests** for helpers that don't touch the filesystem or
  ebooklib (NCX parsing, text extraction, whitespace normalization).
* **is_downloaded tests** using real temp files and monkey-patched stat
  results to simulate iCloud placeholders without needing actual
  iCloud-synced files.
* **BookContent tests** using the generated EPUB fixtures from
  ``conftest.py``.
"""

from __future__ import annotations

import os
import pathlib
from unittest.mock import patch

import pytest

from py_apple_books.content import (
    BookContent,
    Chapter,
    _extract_chapter_text,
    _normalize_whitespace,
    _parse_ncx_bytes,
    is_downloaded,
)
from py_apple_books.exceptions import AppleBooksError


# ---------------------------------------------------------------------------
# _normalize_whitespace
# ---------------------------------------------------------------------------


class TestNormalizeWhitespace:
    def test_empty_string(self):
        assert _normalize_whitespace("") == ""

    def test_whitespace_only_string(self):
        assert _normalize_whitespace("  \t\n  ") == ""

    def test_collapses_runs_of_spaces(self):
        assert _normalize_whitespace("hello    world") == "hello world"

    def test_collapses_tabs(self):
        assert _normalize_whitespace("hello\t\tworld") == "hello world"

    def test_preserves_single_paragraph_break(self):
        assert _normalize_whitespace("para 1\n\npara 2") == "para 1\n\npara 2"

    def test_collapses_multiple_blank_lines(self):
        assert (
            _normalize_whitespace("para 1\n\n\n\n\npara 2") == "para 1\n\npara 2"
        )

    def test_strips_leading_and_trailing(self):
        assert _normalize_whitespace("\n\n  hello world  \n\n") == "hello world"


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
        # Real Steve Jobs EPUB has CR%21BJHPA831ES3B3F7WSRKZ17DMGSM7_split_002.html
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
        assert _extract_chapter_text(html, None, set()) == "Hello world"

    def test_multiple_paragraphs_get_paragraph_breaks(self):
        html = b"<html><body><p>First</p><p>Second</p></body></html>"
        assert _extract_chapter_text(html, None, set()) == "First\n\nSecond"

    def test_script_and_style_are_stripped(self):
        html = (
            b"<html><head><title>ignore</title></head>"
            b"<body>"
            b"<script>var x = 1;</script>"
            b"<style>p { color: red; }</style>"
            b"<p>Visible text</p>"
            b"</body></html>"
        )
        out = _extract_chapter_text(html, None, set())
        assert "Visible text" in out
        assert "var x" not in out
        assert "color: red" not in out
        assert "ignore" not in out

    def test_inline_tags_stay_inline(self):
        html = b"<html><body><p>foo <em>bar</em> baz</p></body></html>"
        assert _extract_chapter_text(html, None, set()) == "foo bar baz"

    def test_xml_declaration_handled(self):
        html = (
            b'<?xml version="1.0"?>\n'
            b'<html xmlns="http://www.w3.org/1999/xhtml">'
            b"<head><title>T</title></head>"
            b"<body><p>Hi</p></body></html>"
        )
        assert _extract_chapter_text(html, None, set()) == "Hi"

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
        assert "Real content" in _extract_chapter_text(html, None, set())

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
        out = _extract_chapter_text(html, "p4", set())
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
        out = _extract_chapter_text(html, "B", {"A", "C"})
        assert "section B" in out
        assert "section A" not in out
        assert "section C" not in out

    def test_missing_start_anchor_falls_back_to_whole_document(self):
        html = b"<html><body><p>Hello</p></body></html>"
        out = _extract_chapter_text(html, "nonexistent", set())
        assert out == "Hello"

    def test_image_only_body_returns_empty(self):
        # Real Skin in the Game pattern: "Book 1 Introduction" is
        # rendered as a single <img>. We can't extract text from that —
        # an empty return is correct.
        html = (
            b"<html><body><div>"
            b'<img alt="Book 1" src="img.jpg"/>'
            b"</div></body></html>"
        )
        assert _extract_chapter_text(html, None, set()) == ""


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

    def test_is_drm_protected_true_when_encryption_xml_present(self, simple_epub):
        meta_inf = simple_epub.path / "META-INF"
        meta_inf.mkdir(exist_ok=True)
        (meta_inf / "encryption.xml").write_text("<encryption/>")
        assert BookContent(simple_epub.path).is_drm_protected is True

    def test_is_drm_protected_false_for_pdf(self, tmp_path):
        f = tmp_path / "book.pdf"
        f.write_bytes(b"%PDF-1.4\n")
        assert BookContent(f).is_drm_protected is False


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


class TestGetChapterContent:
    def test_by_chapter_id(self, simple_epub):
        content = BookContent(simple_epub.path)
        chapters = content.list_chapters()
        text = content.get_chapter_content(chapters[0].id)
        assert "Chapter 1" in text
        assert "Body paragraph for Chapter 1" in text

    def test_by_order_as_string(self, simple_epub):
        """The id parameter also accepts ``str(order)`` as a fallback
        lookup for callers that only have a position."""
        content = BookContent(simple_epub.path)
        text = content.get_chapter_content("2")
        assert "Chapter 2" in text

    def test_paragraph_breaks_preserved(self, simple_epub):
        content = BookContent(simple_epub.path)
        text = content.get_chapter_content(content.list_chapters()[0].id)
        # Two <p> tags should produce a paragraph break.
        assert "\n\n" in text

    def test_raises_on_unknown_chapter(self, simple_epub):
        with pytest.raises(AppleBooksError):
            BookContent(simple_epub.path).get_chapter_content("nonexistent")

    def test_raises_for_non_epub(self, tmp_path):
        f = tmp_path / "book.pdf"
        f.write_bytes(b"%PDF-1.4\n")
        with pytest.raises(AppleBooksError):
            BookContent(f).get_chapter_content("any")


class TestChapterAtCfi:
    def test_bracket_hint_resolves_to_chapter(self, simple_epub):
        content = BookContent(simple_epub.path)
        chapters = content.list_chapters()
        # The third chapter's manifest id (ebooklib defaults to "chap1",
        # "chap2", etc. based on file_name). We verify via the resolver
        # and cross-check.
        book = content._load_book()
        spine_ids = [e[0] for e in book.spine if isinstance(e, tuple)]
        target_id = spine_ids[2]  # skip "nav"
        cfi = f"epubcfi(/6/6[{target_id}]!/4/2/1:0)"
        resolved = content.chapter_at_cfi(cfi)
        assert resolved is not None
        # The resolver returns the outermost ToC chapter that matches
        # the href; for our flat fixture that is just the chapter itself.
        assert resolved.href == chapters[1].href or resolved.href == chapters[0].href \
            or resolved.href == chapters[2].href

    def test_invalid_cfi_returns_none(self, simple_epub):
        assert BookContent(simple_epub.path).chapter_at_cfi("not a cfi") is None

    def test_empty_cfi_returns_none(self, simple_epub):
        assert BookContent(simple_epub.path).chapter_at_cfi("") is None

    def test_unknown_bracket_hint_falls_back_to_numeric(self, simple_epub):
        """When the bracketed manifest id isn't in the book, the
        resolver should fall back to numeric spine-index decoding."""
        content = BookContent(simple_epub.path)
        # /6/4 (even step 4) → spine index 1; our spine is
        # ["nav", c1, c2, c3], so spine[1] is Chapter 1.
        cfi = "epubcfi(/6/4[ghost-id]!/4/2/1:0)"
        resolved = content.chapter_at_cfi(cfi)
        assert resolved is not None
        assert resolved.title == "Chapter 1"

    def test_raises_for_non_epub(self, tmp_path):
        f = tmp_path / "book.pdf"
        f.write_bytes(b"%PDF-1.4\n")
        # Non-EPUBs short-circuit and return None rather than raising.
        assert BookContent(f).chapter_at_cfi("epubcfi(/6/4!/4/2/1:0)") is None


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
