"""``get_chapter`` keeps 1.10's text (1.11, stream 3.2).

``BookContent.get_chapter(chapter_id)`` and ``get_chapter(chapter_id,
span='file')`` must return exactly what 1.10 returned, through 1.10's id
resolution, for every chapter id, order string and manifest id of every
fixture shape. The oracle below is 1.10.0's ``get_chapter`` with its two
helpers, copied (``py_apple_books/content.py`` at tag v1.10.0, commit
0fb2285), run on its own ebooklib read of the bundle with 1.10.0's
extractor (``tests/_legacy_110_extract.py``). Only the chapter list comes
from the candidate: its equality with 1.10's is pinned by the book-index
tests (stream 2.1). Error messages are compared as 1.11 words them (names
quoted by ``_messages.quote_name``, which is ``repr`` for short names).

Every bundle is synthetic.
"""

from __future__ import annotations

import pathlib
from typing import List, Optional, Set

import pytest
from ebooklib import epub

from py_apple_books import content as content_module
from py_apple_books.content import BookContent, Chapter
from py_apple_books.exceptions import AppleBooksError, ChapterNotFoundError
from tests import _epub_shapes, _span_shapes
from tests._legacy_110_extract import extract_chapter_text as legacy_extract

ALL_SHAPES = {**_epub_shapes.SHAPES, **_span_shapes.SHAPES}


class _Legacy110:
    """1.10.0's ``get_chapter`` on a bundle (see the module docstring)."""

    def __init__(self, path: pathlib.Path, chapters: List[Chapter]) -> None:
        self.path = path
        self.chapters = chapters
        self.book = epub.read_epub(str(path))
        self.opf_dir = content_module._opf_dir_from_container(path)

    def list_chapters(self) -> List[Chapter]:
        return list(self.chapters)

    def _to_opf_relative(self, bundle_relative: str) -> str:
        if not bundle_relative:
            return bundle_relative
        opf_dir_str = str(self.opf_dir)
        if opf_dir_str in ("", "."):
            return bundle_relative
        prefix = f"{opf_dir_str}/"
        if bundle_relative.startswith(prefix):
            return bundle_relative[len(prefix):]
        return bundle_relative

    # -- verbatim from 1.10.0 (self._require_epub() calls dropped: every
    # -- shape is an EPUB; messages as 1.11 quotes names) ------------------

    def get_chapter(self, chapter_id: str) -> str:
        wanted_id = str(chapter_id)

        # Path 1: match a ToC chapter (enables fragment scoping).
        chapters = self.list_chapters()
        match: Optional[Chapter] = None
        for ch in chapters:
            if ch.id == wanted_id or str(ch.order) == wanted_id:
                match = ch
                break

        if match is not None:
            html_bytes = self._read_chapter_bytes(match.href)
            # Other navPoint fragments in the same file become stop
            # anchors so sibling sections don't bleed into one another.
            stop_anchors: Set[str] = {
                ch.fragment
                for ch in chapters
                if ch.href == match.href
                and ch.fragment
                and ch.fragment != match.fragment
            }
            return legacy_extract(
                html_bytes,
                start_anchor=match.fragment or None,
                stop_anchors=stop_anchors,
            )

        # Path 2: fall back to raw spine — works for sub-sections that
        # aren't in the ToC. ebooklib's manifest knows every spine item.
        return self._spine_item_text(wanted_id)

    def _spine_item_text(self, item_id: str) -> str:
        book = self.book
        item = book.get_item_with_id(item_id)
        if item is None:
            raise ChapterNotFoundError(
                f"No chapter or spine entry with id {item_id!r} in this "
                f"book. Pass an id from the book's table of contents, or "
                f"a chapter's 1-based order (e.g. \"5\")."
            )
        try:
            html_bytes = item.get_content()
        except Exception as e:
            raise AppleBooksError(
                f"Could not read spine entry {item_id!r}: {e}"
            ) from e
        return legacy_extract(
            html_bytes,
            start_anchor=None,
            stop_anchors=set(),
        )

    def _read_chapter_bytes(self, href: str) -> bytes:
        book = self.book
        opf_relative = self._to_opf_relative(href)
        item = book.get_item_with_href(opf_relative)
        if item is None:
            # ebooklib's lookup is exact-match; try a scan.
            for candidate in book.get_items():
                if candidate.file_name == opf_relative:
                    item = candidate
                    break
        if item is not None:
            try:
                return item.get_content()
            except Exception:
                pass  # fall through to disk read

        try:
            return content_module._safe_bundle_path(self.path, href).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            raise AppleBooksError(
                f"Chapter file {href!r} is declared in the EPUB "
                f"manifest but missing on disk."
            ) from None
        except OSError as e:
            raise AppleBooksError(
                f"Could not read chapter file {href!r}: {e.strerror}"
            ) from e


def _outcome(call):
    try:
        return ("text", call())
    except AppleBooksError as e:
        return (type(e).__name__, str(e))


def _ids(content: BookContent) -> List[str]:
    """Every chapter id, order string and manifest id, plus ids that name
    nothing, without repeats."""
    chapters = content.list_chapters()
    book = content._load_book()
    ids = [c.id for c in chapters] + [str(c.order) for c in chapters]
    ids += [item.get_id() for item in book.get_items()]
    ids += ["nope", "", "0", "01", str(len(chapters) + 1)]
    return list(dict.fromkeys(ids))


@pytest.mark.filterwarnings("ignore::bs4.XMLParsedAsHTMLWarning")
@pytest.mark.parametrize("name", sorted(ALL_SHAPES))
def test_default_and_file_span_equal_110(tmp_path, name):
    bundle = ALL_SHAPES[name](tmp_path)
    content = BookContent(bundle)
    legacy = _Legacy110(bundle, BookContent(bundle).list_chapters())
    checked = 0
    for chapter_id in _ids(content):
        expected = _outcome(lambda: legacy.get_chapter(chapter_id))
        assert _outcome(lambda: content.get_chapter(chapter_id)) == expected, chapter_id
        assert _outcome(lambda: content.get_chapter(chapter_id, span="file")) == expected, chapter_id
        assert _outcome(lambda: content.get_chapter(chapter_id, span="file", normalize_unicode=False)) \
            == expected, chapter_id
        # A fresh instance (book not loaded yet) gives the same.
        assert _outcome(lambda: BookContent(bundle).get_chapter(chapter_id)) == expected, chapter_id
        checked += 1
    assert checked >= 5


@pytest.mark.filterwarnings("ignore::bs4.XMLParsedAsHTMLWarning")
@pytest.mark.parametrize("name", sorted(ALL_SHAPES))
def test_span_calls_leave_file_text_unchanged(tmp_path, name):
    """Span calls on an instance don't change what later default calls
    return (no state shared between the modes)."""
    bundle = ALL_SHAPES[name](tmp_path)
    content = BookContent(bundle)
    ids = _ids(content)
    before = {i: _outcome(lambda: content.get_chapter(i)) for i in ids}
    for chapter_id in ids:
        for span in ("section", "chapter"):
            _outcome(lambda: content.get_chapter(chapter_id, span=span))
    assert {i: _outcome(lambda: content.get_chapter(i)) for i in ids} == before


@pytest.mark.parametrize("name", sorted(ALL_SHAPES))
def test_span_modes_return_text_or_chapter_errors(tmp_path, name):
    """Every id gives text or ChapterNotFoundError in the span modes (the
    ids 1.10 couldn't read, such as phantom spine sections, give text)."""
    bundle = ALL_SHAPES[name](tmp_path)
    content = BookContent(bundle)
    for chapter_id in _ids(content):
        for span in ("section", "chapter"):
            try:
                text = content.get_chapter(chapter_id, span=span)
            except ChapterNotFoundError:
                continue
            assert isinstance(text, str)
            assert text == text.strip()
            assert "\n\n\n" not in text


def test_signature():
    import inspect

    sig = inspect.signature(BookContent.get_chapter)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["self", "chapter_id", "span", "normalize_unicode"]
    assert params[1].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params[2].kind is inspect.Parameter.KEYWORD_ONLY and params[2].default == "file"
    assert params[3].kind is inspect.Parameter.KEYWORD_ONLY and params[3].default is False
