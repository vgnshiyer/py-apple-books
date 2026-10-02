"""The spine API of BookContent (1.11): list_spine_items, get_spine_item_text,
iter_spine_text, the content dataclasses, Chapter.spine_index and the
BookContent mixins.

Every bundle is synthetic (tests/_epub_shapes.py, write_epub_bundle).
"""

from __future__ import annotations

import copy
import dataclasses
import pickle
import threading

import pytest

from py_apple_books import content as content_module
from py_apple_books._content_reading import _ReadingMixin
from py_apple_books._content_resolve import _ResolveMixin
from py_apple_books.content import (
    MAX_SEARCH_HITS,
    MAX_SNIPPET_CONTEXT,
    BookContent,
    Chapter,
    SpineItem,
    SpineText,
    TextHit,
    TextSearchResult,
)
from py_apple_books.exceptions import (
    AppleBooksError,
    BookNotDownloadedError,
    ChapterNotFoundError,
    InvalidArgumentError,
    NotEpubError,
    UnsafeEpubEntryError,
)
from py_apple_books.models.location import Location
from py_apple_books.positions import (
    BoundaryPrecision,
    BoundarySource,
    ReadBoundary,
    ResolvedBoundary,
    TextPosition,
)
from py_apple_books.testing import write_epub, write_epub_bundle
from tests import _epub_shapes
from tests._epub_shapes import SHAPES


@pytest.fixture
def shape(tmp_path, request):
    return SHAPES[request.param](tmp_path)


def _full(path) -> BookContent:
    """An instance that has read the whole book (1.10's way)."""
    content = BookContent(path)
    content._load_book()
    return content


# ---------------------------------------------------------------------------
# Chapter.spine_index
# ---------------------------------------------------------------------------


class TestChapterSpineIndex:
    def test_compatible_with_1_10_chapters(self):
        old = Chapter(id="c1", title="One", href="OEBPS/c1.xhtml", fragment="", order=1, depth=0)
        new = dataclasses.replace(old, spine_index=4)
        assert old.spine_index is None and new.spine_index == 4
        assert old == new and hash(old) == hash(new)
        assert repr(old) == repr(new) == (
            "Chapter(id='c1', title='One', href='OEBPS/c1.xhtml', fragment='', order=1, depth=0)")
        assert [f.name for f in dataclasses.fields(Chapter)][-1] == "spine_index"
        assert dataclasses.asdict(new)["spine_index"] == 4
        assert dataclasses.astuple(new) == ("c1", "One", "OEBPS/c1.xhtml", "", 1, 0, 4)
        assert pickle.loads(pickle.dumps(new)).spine_index == 4
        assert copy.deepcopy(new).spine_index == 4

    def test_a_1_10_pickle_reads_as_none(self):
        chapter = Chapter(id="c1", title="One", href="h", fragment="", order=1, depth=0)
        # A 1.10 instance pickles without the field in its state.
        state = {k: v for k, v in chapter.__dict__.items() if k != "spine_index"}
        clone = Chapter.__new__(Chapter)
        clone.__dict__.update(state)
        assert clone.spine_index is None and clone == chapter

    @pytest.mark.parametrize("shape", ["plain", "subfile", "gutenberg", "calibre_split",
                                       "nav_in_spine", "toc_pages"], indirect=True)
    def test_list_chapters_sets_it(self, shape):
        content = BookContent(shape)
        items = {i.href: i.index for i in content.list_spine_items()}
        for chapter in content.list_chapters():
            assert chapter.spine_index == items[chapter.href]

    def test_first_occurrence_and_none(self, tmp_path):
        bundle = write_epub_bundle(
            tmp_path / "Repeat.epub", [("a", "<p>a</p>"), ("b", "<p>b</p>")],
            toc=[("A", "a.xhtml"), ("Gone", "missing.xhtml")],
            spine_xml='<itemref idref="b"/><itemref idref="a"/><itemref idref="a"/>')
        chapters = BookContent(bundle).list_chapters()
        assert [(c.title, c.spine_index) for c in chapters] == [("A", 1), ("Gone", None)]

    def test_phantom_sections_have_none(self, tmp_path):
        chapters = BookContent(_epub_shapes.phantom_sections(tmp_path)).list_chapters()
        assert [(c.title, c.spine_index) for c in chapters] == [
            ("Section 1", 0), ("Section 2", None), ("Section 3", 1), ("Section 4", None),
            ("Section 5", 2)]


# ---------------------------------------------------------------------------
# list_spine_items
# ---------------------------------------------------------------------------


class TestListSpineItems:
    def test_probe_matches_cfi_spine_steps(self, tmp_path):
        # nav, comment, idref-less itemref, PI, linear=' NO ': five items,
        # numbered as the /6/N steps of a CFI count them.
        bundle = write_epub_bundle(
            tmp_path / "Probe.epub",
            [("c1", "<p>one</p>"), ("c2", "<p>two</p>"), ("c3", "<p>three</p>")],
            toc=[("One", "c1.xhtml")],
            spine_xml=('<itemref idref="nav"/><itemref idref="c1"/><!-- <itemref idref="old"/> -->'
                       '<itemref/><itemref idref="c2"/><?pi x?><itemref idref="c3" linear=" NO "/>'),
            guide=[("toc", "Contents", "nav.xhtml")])
        items = BookContent(bundle).list_spine_items()
        assert [(i.index, i.item_id, i.linear, i.readable, i.is_toc_page) for i in items] == [
            (0, "nav", True, True, True),
            (1, "c1", True, True, False),
            (2, None, True, False, False),
            (3, "c2", True, True, False),
            (4, "c3", False, True, False),
        ]
        for cfi, item_id in [("epubcfi(/6/4[c1]!/4/2/1:0)", "c1"), ("epubcfi(/6/8[c2]!/4/2/1:0)", "c2"),
                             ("epubcfi(/6/10[c3]!/4/2/1:0)", "c3"), ("epubcfi(/6/2[nav]!/4)", "nav")]:
            assert items[Location(cfi).spine_index].item_id == item_id
        assert items[2].href is None and items[2].media_type is None and items[2].toc_orders == ()

    def test_unknown_idref_and_non_itemref(self, tmp_path):
        bundle = write_epub_bundle(
            tmp_path / "Odd.epub", [("c1", "<p>one</p>")], toc=[("One", "c1.xhtml")],
            spine_xml='<itemref idref="ghost"/><reference idref="c1"/><itemref idref="c1"/>')
        items = BookContent(bundle).list_spine_items()
        assert [(i.index, i.item_id, i.href, i.readable) for i in items] == [
            (0, "ghost", None, False), (1, None, None, False), (2, "c1", "OEBPS/c1.xhtml", True)]

    def test_toc_pages_from_nav_guide_and_landmarks(self, tmp_path):
        items = BookContent(_epub_shapes.toc_pages(tmp_path)).list_spine_items()
        assert [(i.item_id, i.is_toc_page, i.linear) for i in items] == [
            ("navdoc", True, True), ("contents", True, True), ("printed", True, True),
            ("c1", False, True), ("notes", False, False), ("c2", False, True)]

    def test_toc_orders_on_a_gutenberg_file(self, tmp_path):
        content = BookContent(_epub_shapes.gutenberg(tmp_path))
        (item,) = content.list_spine_items()
        assert item.toc_orders == (1, 2, 3)
        assert [c.order for c in content.list_chapters()] == [1, 2, 3]

    @pytest.mark.parametrize("media_type, readable", [
        ("IMAGE/PNG", False), ("Image/Jpeg", False), ("text/CSS", False),
        (" text/css ; charset=utf-8", False), ("image/svg+xml; charset=utf-8", True),
        ("IMAGE/SVG+XML", True), ("Application/XHTML+XML", True), ("text/html;q=1", True),
        (None, True), ("", True),
    ])
    def test_media_types_ignore_case_and_parameters(self, media_type, readable):
        assert content_module._is_text_media_type(media_type) is readable

    def test_media_types_and_readable(self, tmp_path):
        items = BookContent(_epub_shapes.mixed_types(tmp_path)).list_spine_items()
        assert [(i.item_id, i.media_type, i.readable) for i in items] == [
            ("c1", "application/xhtml+xml", True), ("page", "text/html", True),
            ("art", "image/svg+xml", True), ("pic", "image/png", False)]

    def test_hrefs_are_bundle_relative(self, tmp_path):
        items = BookContent(_epub_shapes.root_opf(tmp_path)).list_spine_items()
        assert [i.href for i in items] == ["text/c1.xhtml", "text/c2.xhtml"]

    def test_href_outside_the_bundle_is_not_readable(self, tmp_path):
        bundle = write_epub_bundle(
            tmp_path / "Escape.epub", [("c1", "<p>one</p>")], toc=[("One", "c1.xhtml")],
            extra_items=[("out", "../../outside.xhtml", "application/xhtml+xml", None)],
            spine_xml='<itemref idref="c1"/><itemref idref="out"/>')
        items = BookContent(bundle).list_spine_items()
        assert (items[1].item_id, items[1].readable) == ("out", False)
        with pytest.raises(ChapterNotFoundError):
            BookContent(bundle).get_spine_item_text(1)

    def test_new_list_each_call_shared_items(self, tmp_path):
        content = BookContent(_epub_shapes.plain(tmp_path))
        first, second = content.list_spine_items(), content.list_spine_items()
        assert first == second and first is not second
        assert all(a is b for a, b in zip(first, second))
        with pytest.raises(dataclasses.FrozenInstanceError):
            first[0].index = 9

    def test_not_epub(self, tmp_path):
        pdf = tmp_path / "Book.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        for call in (BookContent(pdf).list_spine_items, lambda: BookContent(pdf).get_spine_item_text(0),
                     lambda: BookContent(pdf).iter_spine_text()):
            with pytest.raises(NotEpubError, match="This book is a PDF"):
                call()


# ---------------------------------------------------------------------------
# get_spine_item_text
# ---------------------------------------------------------------------------


class TestGetSpineItemText:
    @pytest.mark.parametrize("shape", sorted(SHAPES), indirect=True)
    def test_oracle_equals_1_10_spine_item_text(self, shape):
        # By id and by index, from the index (no full load) and from an
        # instance that has read the whole book: always 1.10's text.
        full = _full(shape)
        fresh = BookContent(shape)
        items = fresh.list_spine_items()
        assert fresh._book is None
        for item in items:
            if not item.readable:
                continue
            expected = full._spine_item_text(item.item_id)
            assert fresh.get_spine_item_text(item.item_id) == expected
            assert BookContent(shape).get_spine_item_text(item.index) == expected
            assert full.get_spine_item_text(item.index) == expected
        assert fresh._book is None

    def test_reads_one_file_per_item(self, tmp_path, monkeypatch):
        bundle = _epub_shapes.plain(tmp_path)
        content = BookContent(bundle)
        content.list_spine_items()
        reads = []
        real = content_module._read_entry

        def spy(root, href, max_bytes):
            reads.append(href)
            return real(root, href, max_bytes)

        monkeypatch.setattr(content_module, "_read_entry", spy)
        assert "Beta text two." in content.get_spine_item_text("ch2")
        assert reads == ["OEBPS/ch2.xhtml"]
        # Memoized per instance (the default form).
        assert content.get_spine_item_text(1) == content.get_spine_item_text("ch2")
        assert reads == ["OEBPS/ch2.xhtml"]

    def test_uses_the_loaded_book(self, tmp_path, monkeypatch):
        content = _full(_epub_shapes.plain(tmp_path))
        monkeypatch.setattr(content_module, "_read_entry_bytes",
                            lambda *a: pytest.fail("read again"))
        assert "Alpha text one." in content.get_spine_item_text("ch1")

    @pytest.mark.parametrize("item", [True, False, -1, 3, 99])
    def test_bad_index(self, tmp_path, item):
        content = BookContent(_epub_shapes.plain(tmp_path))
        with pytest.raises(ChapterNotFoundError) as exc:
            content.get_spine_item_text(item)
        assert repr(item) in str(exc.value) and "/" not in str(exc.value)

    @pytest.mark.parametrize("sign", [1, -1])
    @pytest.mark.parametrize("digits", [12, 4000, 5000])  # 5000: past str()'s 4,300-digit limit
    def test_huge_index(self, tmp_path, sign, digits):
        item = sign * 10**digits
        content = BookContent(_epub_shapes.plain(tmp_path))
        with pytest.raises(ChapterNotFoundError) as exc:
            content.get_spine_item_text(item)
        assert str(exc.value) == "No spine entry at that index in this book."

    def test_an_int_subclass_index(self, tmp_path):
        import enum

        class Nth(enum.IntEnum):
            SECOND = 1
            FAR = 7

        content = BookContent(_epub_shapes.plain(tmp_path))
        assert content.get_spine_item_text(Nth.SECOND) == content.get_spine_item_text(1)
        with pytest.raises(ChapterNotFoundError, match="^No spine entry at index 7 in this book.$"):
            content.get_spine_item_text(Nth.FAR)

    @pytest.mark.parametrize("item", [1.0, None, b"ch1", ["ch1"]])
    def test_bad_type(self, tmp_path, item):
        with pytest.raises(InvalidArgumentError):
            BookContent(_epub_shapes.plain(tmp_path)).get_spine_item_text(item)

    @pytest.mark.parametrize("item", ["pic", "css", "font", "cover", "ncx", 3, "nope"])
    def test_binary_or_unknown_is_refused(self, tmp_path, item):
        content = BookContent(_epub_shapes.mixed_types(tmp_path))
        with pytest.raises(ChapterNotFoundError) as exc:
            content.get_spine_item_text(item)
        message = str(exc.value)
        assert "/" not in message and "order" not in message
        assert message.startswith("No text document")

    def test_svg_and_html_are_read(self, tmp_path):
        content = BookContent(_epub_shapes.mixed_types(tmp_path))
        assert content.get_spine_item_text("art") == "Vector words"
        assert content.get_spine_item_text("page") == "Plain html page."

    def test_manifest_item_outside_the_spine(self, tmp_path):
        bundle = write_epub_bundle(
            tmp_path / "Extra.epub", [("c1", "<p>one</p>"), ("aside", "<p>Aside text.</p>", {"in_spine": False})],
            toc=[("One", "c1.xhtml")])
        assert BookContent(bundle).get_spine_item_text("aside") == "Aside text."

    def test_normalize_unicode(self, tmp_path):
        bundle = write_epub_bundle(
            tmp_path / "Uni.epub", [("c1", "<p>Café co­op ​word﻿ end</p>")],
            toc=[("One", "c1.xhtml")])
        content = BookContent(bundle)
        raw = content.get_spine_item_text("c1")
        assert raw == "Café co­op ​word﻿ end"
        assert content.get_spine_item_text("c1", normalize_unicode=True) == "Café coop word end"
        assert content.get_spine_item_text(0) == raw  # the memo keeps the default form
        normalized = content.get_spine_item_text(0, normalize_unicode=True)
        assert normalized != raw
        # Normalized first, on a new instance: the memo still keeps the
        # default form, so later default calls keep TextPosition offsets.
        fresh = BookContent(bundle)
        assert fresh.get_spine_item_text("c1", normalize_unicode=True) == normalized
        assert fresh.get_spine_item_text("c1") == raw
        assert fresh.get_spine_item_text(0) == raw

    def test_missing_file(self, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        (bundle / "OEBPS" / "ch2.xhtml").unlink()
        content = BookContent(bundle)
        assert len(content.list_spine_items()) == 3  # the index doesn't need it
        with pytest.raises(AppleBooksError) as exc:
            content.get_spine_item_text("ch2")
        assert str(exc.value).startswith("Could not read spine entry 'ch2'")
        assert str(tmp_path) not in str(exc.value)

    def test_symlink_out_of_the_bundle(self, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        outside = tmp_path / "secret.xhtml"
        outside.write_text("<p>secret</p>")
        target = bundle / "OEBPS" / "ch2.xhtml"
        target.unlink()
        target.symlink_to(outside)
        with pytest.raises(UnsafeEpubEntryError):
            BookContent(bundle).get_spine_item_text("ch2")

    def test_lock_is_not_held_while_extracting(self, tmp_path, monkeypatch):
        content = BookContent(_epub_shapes.plain(tmp_path))
        held = []
        real = content_module.extract_chapter_text

        def extract(*args, **kwargs):
            held.append(content._lock.locked())
            return real(*args, **kwargs)

        monkeypatch.setattr(content_module, "extract_chapter_text", extract)
        content.get_spine_item_text(0)
        assert held == [False]


# ---------------------------------------------------------------------------
# iter_spine_text
# ---------------------------------------------------------------------------


def _paged(content, page_size, until=None, **scope):
    """Read the scope page by page, ``page_size`` characters at a time,
    resuming each page at the position the last one stopped; returns the
    ``(index, offset, text)`` pieces in order."""
    pieces, start, rounds = [], None, 0
    while True:
        rounds += 1
        assert rounds < 100_000, "paging doesn't terminate"
        budget, resume = page_size, None
        for chunk in content.iter_spine_text(start=start, until=until, **scope):
            if not chunk.readable:
                continue
            take = chunk.text[:budget]
            pieces.append((chunk.index, chunk.offset, take))
            budget -= len(take)
            if budget == 0:
                resume = TextPosition(chunk.index, chunk.offset + len(take))
                break
        if resume is None:
            return pieces
        start = resume


def _whole(pieces):
    """{index: text} joined from the pieces, checking there is no gap and
    no overlap inside an item."""
    out = {}
    for index, offset, text in pieces:
        assert len(out.get(index, "")) == offset, (index, offset)
        out[index] = out.get(index, "") + text
    return out


class TestIterSpineText:
    def test_default_scope(self, tmp_path):
        content = BookContent(_epub_shapes.toc_pages(tmp_path))
        chunks = list(content.iter_spine_text())
        assert [c.item_id for c in chunks] == ["c1", "c2"]
        assert all(c.complete and c.offset == 0 and c.length == len(c.text) for c in chunks)
        assert chunks[0].text == content.get_spine_item_text("c1")
        assert chunks[0].position == TextPosition(3, 0) and chunks[0].end == TextPosition(3, len(chunks[0].text))

    def test_including_nonlinear_and_toc_pages(self, tmp_path):
        content = BookContent(_epub_shapes.toc_pages(tmp_path))
        assert [c.item_id for c in content.iter_spine_text(include_nonlinear=True)] == ["c1", "notes", "c2"]
        assert [c.item_id for c in content.iter_spine_text(include_toc_pages=True)] == [
            "navdoc", "contents", "printed", "c1", "c2"]
        everything = list(content.iter_spine_text(include_nonlinear=True, include_toc_pages=True))
        assert [c.index for c in everything] == [0, 1, 2, 3, 4, 5]
        assert everything[0].is_toc_page and not everything[4].linear

    def test_unreadable_items_are_yielded_once(self, tmp_path):
        bundle = write_epub_bundle(
            tmp_path / "Holes.epub",
            [("c1", "<p>one</p>"), ("pic", b"\x89PNG", {"raw": True, "href": "p.png", "media_type": "image/png"}),
             ("c2", "<p>two</p>"), ("c3", "<p>three</p>")],
            toc=[("One", "c1.xhtml")],
            spine_xml='<itemref idref="c1"/><itemref idref="pic"/><itemref/><itemref idref="c2"/>'
                      '<itemref idref="c3"/>')
        (bundle / "OEBPS" / "c2.xhtml").unlink()
        content = BookContent(bundle)
        chunks = list(content.iter_spine_text())
        assert [(c.index, c.readable, c.text) for c in chunks] == [
            (0, True, "one"), (1, False, ""), (2, False, ""), (3, False, ""), (4, True, "three")]
        assert not chunks[1].complete and chunks[1].length == 0
        # Continuing from an unreadable item's end moves past it.
        assert chunks[1].position == TextPosition(1, 0)
        assert chunks[1].end == TextPosition(2, 0)
        assert chunks[0].end == TextPosition(0, 3)

        # One chunk per page, each page resumed at the last chunk's end:
        # terminates and reports every item exactly once.
        seen, start = [], None
        for _ in range(20):
            chunk = next(content.iter_spine_text(start=start), None)
            if chunk is None:
                break
            seen.append((chunk.index, chunk.readable))
            start = chunk.end
        else:
            raise AssertionError(f"paging by chunk.end doesn't terminate: {seen}")
        assert seen == [(0, True), (1, False), (2, False), (3, False), (4, True)]

        def got(**kwargs):
            return [(c.index, c.readable) for c in content.iter_spine_text(**kwargs)]

        # An unreadable item at `until` (offset 0) is out of scope.
        assert got(until=TextPosition(1, 0)) == [(0, True)]
        assert got(until=TextPosition(3, 0)) == [(0, True), (1, False), (2, False)]
        assert got(until=TextPosition(3, 2)) == [(0, True), (1, False), (2, False), (3, False)]
        # Resuming inside an unreadable item doesn't report it again.
        assert got(start=TextPosition(1, 3)) == [(2, False), (3, False), (4, True)]
        assert got(start=TextPosition(3, 0)) == [(3, False), (4, True)]
        assert got(start=TextPosition(3, 1)) == [(4, True)]
        # start == until, inside or at the start of an unreadable item: nothing.
        assert got(start=TextPosition(1, 0), until=TextPosition(1, 0)) == []
        assert got(start=TextPosition(3, 0), until=TextPosition(3, 0)) == []
        assert got(start=TextPosition(3, 2), until=TextPosition(3, 2)) == []

    def test_dataless_item_raises(self, tmp_path, monkeypatch):
        bundle = _epub_shapes.plain(tmp_path)
        content = BookContent(bundle)
        it = content.iter_spine_text()
        assert next(it).item_id == "ch1"

        def refuse(*args):
            raise BookNotDownloadedError("Part of this book is stored only in iCloud.")

        monkeypatch.setattr(content_module, "_read_entry_bytes", refuse)
        with pytest.raises(BookNotDownloadedError):
            next(it)

    def test_start_and_until(self, tmp_path):
        content = BookContent(_epub_shapes.plain(tmp_path))
        texts = [content.get_spine_item_text(i) for i in range(3)]
        got = [(c.index, c.offset, c.text) for c in content.iter_spine_text(
            start=TextPosition(0, 4), until=TextPosition(2, 5))]
        assert got == [(0, 4, texts[0][4:]), (1, 0, texts[1]), (2, 0, texts[2][:5])]
        # until at an item's start: nothing of that item.
        assert [c.index for c in content.iter_spine_text(until=TextPosition(1, 0))] == [0]
        # until past the end of an item / of the book: everything.
        assert [c.text for c in content.iter_spine_text(until=TextPosition(0, 10**6))] == [texts[0]]
        assert [c.index for c in content.iter_spine_text(until=TextPosition(99, 0))] == [0, 1, 2]
        # start after until, start past the end: nothing.
        assert list(content.iter_spine_text(start=TextPosition(2, 0), until=TextPosition(1, 3))) == []
        assert list(content.iter_spine_text(start=TextPosition(1, 3), until=TextPosition(1, 3))) == []
        assert list(content.iter_spine_text(start=TextPosition(0, 10**6), until=TextPosition(1, 0))) == []
        assert list(content.iter_spine_text(start=TextPosition(50, 0))) == []

    @pytest.mark.parametrize("page_size", [1, 7, 100, 10_000])
    @pytest.mark.parametrize("until", [None, TextPosition(2, 0), TextPosition(2, 9), TextPosition(9, 0)])
    def test_paging_has_no_gap_no_overlap_and_ends(self, tmp_path, page_size, until):
        content = BookContent(_epub_shapes.toc_pages(tmp_path))
        expected = {c.index: c.text for c in content.iter_spine_text(
            until=until, include_nonlinear=True, include_toc_pages=True)}
        pieces = _paged(content, page_size, until=until, include_nonlinear=True, include_toc_pages=True)
        assert _whole(pieces) == expected
        assert all(len(text) <= page_size for _, _, text in pieces)

    def test_resolved_boundary(self, tmp_path):
        content = BookContent(_epub_shapes.plain(tmp_path), book_id=7)
        boundary = ReadBoundary(7, "position", BoundarySource.PROGRESS, None, None, 40.0, None, None, ())
        resolved = ResolvedBoundary(7, boundary, TextPosition(1, 0), BoundaryPrecision.SPINE_ITEM,
                                    BoundarySource.PROGRESS, ())
        assert [c.index for c in content.iter_spine_text(until=resolved)] == [0]
        other = dataclasses.replace(resolved, book_id=8)
        with pytest.raises(InvalidArgumentError):
            content.iter_spine_text(until=other)
        with pytest.raises(InvalidArgumentError):
            content.iter_spine_text(until=dataclasses.replace(
                resolved, book_id=None, boundary=dataclasses.replace(boundary, book_id=8)))
        # An instance that doesn't know its book can't tell: accepted.
        assert [c.index for c in BookContent(content.path).iter_spine_text(until=other)] == [0]
        with pytest.raises(InvalidArgumentError, match="resolve_boundary"):
            content.iter_spine_text(until=boundary)

    @pytest.mark.parametrize("kwargs", [{"start": "0:1"}, {"start": 3}, {"until": "1:0"}, {"until": 2}])
    def test_bad_arguments_raise_from_the_call(self, tmp_path, kwargs):
        with pytest.raises(InvalidArgumentError):
            BookContent(_epub_shapes.plain(tmp_path)).iter_spine_text(**kwargs)

    def test_a_suspended_generator_blocks_nobody(self, tmp_path):
        content = BookContent(_epub_shapes.plain(tmp_path))
        it = content.iter_spine_text()
        next(it)
        assert not content._lock.locked()
        done = []
        worker = threading.Thread(target=lambda: done.append(content.get_spine_item_text(2)))
        worker.start()
        worker.join(5)
        assert done and "Gamma" in done[0]
        assert next(it).index == 1


# ---------------------------------------------------------------------------
# Content dataclasses, constants, mixins
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_spine_text_properties(self):
        part = SpineText(3, "c", "OEBPS/c.xhtml", True, False, 5, "hello", 20)
        assert part.position == TextPosition(3, 5) and part.end == TextPosition(3, 10)
        assert not part.complete
        assert SpineText(3, "c", None, True, False, 0, "abc", 3).complete
        assert pickle.loads(pickle.dumps(part)) == part

    def test_search_result_shapes(self):
        hit = TextHit(TextPosition(1, 2), TextPosition(1, 5), "c1", "…a hit…")
        assert TextSearchResult((hit,)).truncated is False
        assert TextSearchResult((hit,), next_start=TextPosition(1, 5)).truncated is True
        assert (MAX_SEARCH_HITS, MAX_SNIPPET_CONTEXT) == (1000, 2000)
        for value in (hit, TextSearchResult(()), SpineItem(0, "a", "a", None, True, False, True)):
            assert copy.copy(value) == value and hash(value) == hash(copy.deepcopy(value))

    def test_public_names(self):
        for name in ("SpineItem", "SpineText", "TextHit", "TextSearchResult", "MAX_SEARCH_HITS",
                     "MAX_SNIPPET_CONTEXT", "clear_content_cache", "Chapter", "BookContent"):
            assert hasattr(content_module, name)


class TestMixins:
    def test_book_content_bases(self):
        assert BookContent.__bases__ == (_ResolveMixin, _ReadingMixin)
        for mixin in (_ResolveMixin, _ReadingMixin):
            assert "__init__" not in vars(mixin)
            assert vars(mixin).get("__slots__") == ()
        public = {n for n in vars(_ResolveMixin) if not n.startswith("_")}
        assert public == {"resolve"}
        # Stays the 2.1 stub until stream 3.3 fills it.
        assert [n for n in vars(_ReadingMixin) if not n.startswith("__")] == []

    def test_pickling_is_unchanged(self, tmp_path):
        content = BookContent(write_epub(tmp_path / "B.epub", "B"), book_id=2)
        content.get_spine_item_text(1)
        state = content.__getstate__()
        assert sorted(state) == ["_book", "_book_id", "_opf_dir_cache", "path"]
        clone = pickle.loads(pickle.dumps(content))
        assert clone._spine_text_memo == {} and clone._chapters_memo is None
        assert clone.get_spine_item_text(1) == content.get_spine_item_text(1)


class TestDuplicateIds:
    """A manifest that repeats an id: the text is that of the first item
    with the id, as 1.10's lookup finds it, loaded book or index."""

    @pytest.mark.parametrize("binary_first", [True, False])
    def test_first_item_decides(self, tmp_path, binary_first):
        doc = ("dup", "doc.xhtml", "application/xhtml+xml", "<html><body><p>Doc text.</p></body></html>")
        img = ("dup", "img.png", "image/png", b"\x89PNG")
        bundle = write_epub_bundle(tmp_path / "Dup.epub", [("c1", "<p>one</p>")], toc=[("One", "c1.xhtml")])
        opf = bundle / "OEBPS" / "content.opf"
        first, second = (img, doc) if binary_first else (doc, img)
        items = "".join(f'<item id="{i}" href="{h}" media-type="{m}"/>' for i, h, m, _ in (first, second))
        opf.write_text(opf.read_text().replace("</manifest>", items + "</manifest>"))
        for _, href, _, data in (first, second):
            (bundle / "OEBPS" / href).write_bytes(data if isinstance(data, bytes) else data.encode())
        for content in (BookContent(bundle), _full(bundle)):
            if binary_first:
                with pytest.raises(ChapterNotFoundError):
                    content.get_spine_item_text("dup")
            else:
                assert content.get_spine_item_text("dup") == "Doc text."
