"""``BookContent.resolve`` (stream 3.1, positions F16/F43): a location's
spine file and the ToC entry it belongs to, from the book index and,
for files holding several entries, their anchors."""

from __future__ import annotations

import os
import pathlib
import time

import pytest

from py_apple_books import _content_resolve, _epub_index
from py_apple_books.content import BookContent
from py_apple_books.exceptions import (
    AppleBooksError,
    BookNotDownloadedError,
    DRMProtectedError,
    InvalidArgumentError,
    NotEpubError,
)
from py_apple_books.models.location import Location
from py_apple_books.positions import ChapterMatch, ResolvedLocation
from py_apple_books.testing import write_epub_bundle
from tests import _epub_shapes, _fs_audit
from tests._positions_helpers import cfi, gutenberg_body, icloud, p, parses, split_book  # noqa: F401

FILE, ANCHOR, PRECEDING = ChapterMatch.FILE, ChapterMatch.ANCHOR, ChapterMatch.PRECEDING
FRONT, UNKNOWN = ChapterMatch.FRONT_MATTER, ChapterMatch.SECTION_UNKNOWN


def title(resolved):
    return None if resolved.chapter is None else resolved.chapter.title


def placed(content, location):
    r = content.resolve(location)
    return (title(r), r.match)


@pytest.fixture
def gutenberg(tmp_path):
    return BookContent(write_epub_bundle(
        tmp_path / "Gut.epub", [("front", p("Front matter.")), ("body", gutenberg_body()),
                                ("license", p("License text."))],
        toc=[("I", "body.xhtml#ch1"), ("II", "body.xhtml#ch2"), ("III", "body.xhtml#ch3")]))


# ---------------------------------------------------------------------------
# Placing in a file holding several entries
# ---------------------------------------------------------------------------


class TestAnchors:
    @pytest.mark.parametrize("path, expected", [
        ("/4/2/1:0", (None, FRONT)),          # the title page heading, before every anchor
        ("/4/4/1:0", ("I", ANCHOR)),          # on the anchor element itself
        ("/4/4", ("I", ANCHOR)),
        ("/4/6/1:3", ("I", ANCHOR)),
        ("/4/8/1:0", ("I", ANCHOR)),          # text before a#ch2 inside its h2
        ("/4/8/2", ("II", ANCHOR)),           # the anchor itself
        ("/4/8/3:1", ("II", ANCHOR)),
        ("/4/10/1:2", ("II", ANCHOR)),
        ("/4/12/1:0", ("III", ANCHOR)),
        ("/4/14/1:0", ("III", ANCHOR)),
        ("/4/99/1:0", ("III", ANCHOR)),       # past the last element
        ("/4", (None, FRONT)),                # the body element: before its children
        ("/2/2/1:0", (None, FRONT)),          # in the head
        ("", (None, FRONT)),                  # the file as a whole: its start
    ])
    def test_gutenberg(self, gutenberg, path, expected):
        resolved = gutenberg.resolve(cfi(1, "body", path))
        assert (title(resolved), resolved.match) == expected
        assert (resolved.spine_index, resolved.item_id, resolved.unavailable) == (1, "body", None)

    def test_before_every_entry_of_the_first_file_with_entries_is_front_matter(self, gutenberg):
        assert placed(gutenberg, cfi(0, "front")) == (None, FRONT)
        assert placed(gutenberg, 0) == (None, FRONT)

    def test_a_file_after_them_takes_the_last_by_anchor(self, gutenberg):
        assert placed(gutenberg, cfi(2, "license")) == ("III", PRECEDING)
        assert placed(gutenberg, 2) == ("III", PRECEDING)

    def test_range_cfi_is_placed_where_it_begins(self, gutenberg):
        # Starts in I's paragraph, ends in II's.
        assert placed(gutenberg, "epubcfi(/6/4[body]!/4,/6/1:2,/10/1:3)") == ("I", ANCHOR)

    def test_id_assertions_rebase_element_steps(self, gutenberg):
        # The step numbers are wrong for this parser; the assertions are right.
        assert placed(gutenberg, cfi(1, "body", "/4/40[ch3]/1:0")) == ("III", ANCHOR)
        assert placed(gutenberg, cfi(1, "body", "/4/2[ch2]/1:0")) == ("II", ANCHOR)
        # The deepest assertion naming an anchor of the file wins.
        assert placed(gutenberg, cfi(1, "body", "/4[ch3]/2[ch1]/1:0")) == ("I", ANCHOR)
        # Assertions that name no anchor (or an odd step) are ignored.
        assert placed(gutenberg, cfi(1, "body", "/4/2[nothing]/1:0")) == (None, FRONT)
        assert placed(gutenberg, cfi(1, "body", "/4/1[ch3]")) == (None, FRONT)

    def test_out_of_order_toc_is_placed_by_document_order(self, tmp_path):
        book = BookContent(split_book(tmp_path, toc=[("B", "s0.xhtml#b"), ("A", "s0.xhtml#a"),
                                                     ("C", "s2.xhtml#c")]))
        assert placed(book, cfi(0, "s0", "/4/4/1:0")) == ("A", ANCHOR)
        assert placed(book, cfi(0, "s0", "/4/8/1:0")) == ("B", ANCHOR)
        # The section before s1 is the one starting last in s0: B, whatever the ToC order.
        assert placed(book, cfi(1, "s1")) == ("B", PRECEDING)

    def test_entries_without_fragment_start_at_the_top(self, tmp_path):
        """A part heading and its first chapter both link the file: the
        location belongs to the later entry; an anchor missing from the
        file starts at the top too."""
        book = BookContent(write_epub_bundle(
            tmp_path / "Parts.epub",
            [("c1", '<h1>Part One</h1><h2>Chapter 1</h2>' + p("text") + '<h2 id="two">Chapter 2</h2>'
              + p("more"))],
            toc=[("Part One", "c1.xhtml", [("Chapter 1", "c1.xhtml#missing"),
                                           ("Chapter 2", "c1.xhtml#two")])]))
        assert placed(book, cfi(0, "c1", "/4/2/1:0")) == ("Chapter 1", ANCHOR)
        assert placed(book, cfi(0, "c1", "/4/6/1:0")) == ("Chapter 1", ANCHOR)
        assert placed(book, cfi(0, "c1", "/4/8/1:0")) == ("Chapter 2", ANCHOR)

    def test_a_name_anchors(self, tmp_path):
        book = BookContent(write_epub_bundle(
            tmp_path / "Names.epub",
            [("c1", '<p><a name="one">One</a></p>' + p("x") + '<p><a name="two">Two</a></p>' + p("y"))],
            toc=[("One", "c1.xhtml#one"), ("Two", "c1.xhtml#two")]))
        assert placed(book, cfi(0, "c1", "/4/4/1:0")) == ("One", ANCHOR)
        assert placed(book, cfi(0, "c1", "/4/8/1:0")) == ("Two", ANCHOR)

    def test_malformed_content_path_in_a_multi_entry_file_is_unknown(self, gutenberg):
        resolved = gutenberg.resolve("epubcfi(/6/4[body]!/4/x/1:0)")
        assert (resolved.chapter, resolved.match, resolved.spine_index) == (None, UNKNOWN, 1)

    def test_unclosed_spine_assertion_is_unknown_not_the_file_start(self, gutenberg):
        # The unclosed '[' must not swallow the '!' and leave "the whole
        # spine item", which would place it at the top of the file.
        resolved = gutenberg.resolve("epubcfi(/6/4[body!/4/10/1:0)")
        assert (resolved.chapter, resolved.match, resolved.spine_index) == (None, UNKNOWN, 1)


class TestUnknownSections:
    @pytest.fixture
    def book(self, tmp_path):
        return BookContent(split_book(tmp_path))

    def test_cut_line_flag(self, book, monkeypatch, parses):
        monkeypatch.setattr(_content_resolve, "_ANCHOR_MATCHING", False)
        assert placed(book, cfi(0, "s0", "/4/8/1:0")) == (None, UNKNOWN)
        assert placed(book, cfi(2, "s2", "/4/2/1:0")) == ("C", FILE)  # one entry: no anchor needed
        assert parses["anchors"] == 0
        # The section before a file with no entry is ordered among s0's own
        # anchors, not compared with the CFI: it is still given.
        assert placed(book, cfi(1, "s1")) == ("B", PRECEDING)

    def test_flag_is_read_on_each_call(self, book, monkeypatch):
        assert placed(book, cfi(0, "s0", "/4/8/1:0")) == ("B", ANCHOR)
        monkeypatch.setattr(_content_resolve, "_ANCHOR_MATCHING", False)
        assert placed(book, cfi(0, "s0", "/4/8/1:0")) == (None, UNKNOWN)

    def test_oversized_file(self, book, monkeypatch):
        monkeypatch.setattr(_epub_index, "MAX_ANCHOR_BYTES", 64)
        resolved = book.resolve(cfi(0, "s0", "/4/8/1:0"))
        assert (resolved.chapter, resolved.match, resolved.spine_index, resolved.item_id) == (None, UNKNOWN, 0, "s0")
        # The section before s1 needs s0's anchors too: unknown, never s0's first entry.
        assert placed(book, cfi(1, "s1")) == (None, UNKNOWN)

    def test_unreadable_file(self, book, monkeypatch):
        def fail(root, href):
            raise AppleBooksError("Could not read EPUB entry 's0.xhtml': [Errno 5] Input/output error")

        monkeypatch.setattr(_epub_index, "_anchor_table_for", fail)
        assert placed(book, cfi(0, "s0", "/4/8/1:0")) == (None, UNKNOWN)

    def test_unparseable_file(self, book, monkeypatch):
        def fail(raw):
            raise RecursionError("too deep")

        monkeypatch.setattr(_epub_index, "_anchor_table", fail)
        assert placed(book, cfi(0, "s0", "/4/8/1:0")) == (None, UNKNOWN)

    def test_placeholder_file_is_never_opened(self, book, icloud):
        chapter = book.path / "OEBPS" / "s0.xhtml"
        book.list_spine_items()
        icloud.mark(chapter)
        with _fs_audit.record() as rec:
            assert placed(book, cfi(0, "s0", "/4/8/1:0")) == (None, UNKNOWN)
            assert placed(book, cfi(2, "s2", "/4/6/1:0")) == ("C", FILE)
        assert rec.under(chapter, "open") == []

    def test_file_outside_the_bundle(self, tmp_path):
        """A symlinked chapter leading out of the bundle is refused, so
        its anchors are unknown."""
        bundle = split_book(tmp_path)
        outside = tmp_path / "outside.xhtml"
        target = bundle / "OEBPS" / "s0.xhtml"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        os.symlink(outside, target)
        with _fs_audit.block(_fs_audit.Policy(deny=(str(outside),))) as rec:
            assert placed(BookContent(bundle), cfi(0, "s0", "/4/8/1:0")) == (None, UNKNOWN)
        # Neither the link nor what it points to is opened.
        assert rec.under(outside, "open") == [] and rec.under(target, "open") == []
        assert rec.refused == []


# ---------------------------------------------------------------------------
# The file a location names
# ---------------------------------------------------------------------------


class TestFile:
    @pytest.fixture
    def plain(self, tmp_path):
        return BookContent(_epub_shapes.plain(tmp_path))

    def test_hint_wins_over_the_spine_step(self, plain):
        resolved = plain.resolve(cfi(1, "ch1"))
        assert (resolved.spine_index, resolved.item_id, title(resolved), resolved.match) == (0, "ch1", "One", FILE)

    def test_hint_not_in_the_spine_falls_back_to_the_step(self, plain):
        for hint in ("nav", "ncx", "nonexistent"):
            resolved = plain.resolve(cfi(1, hint))
            assert (resolved.spine_index, resolved.item_id, title(resolved)) == (1, "ch2", "Two")

    def test_hint_without_a_spine_step(self, plain):
        resolved = plain.resolve("epubcfi([ch3]!/4/2/1:0)")
        assert (resolved.spine_index, title(resolved)) == (2, "Three")

    @pytest.mark.parametrize("location", [
        cfi(3), cfi(3, "nonexistent"), "epubcfi(/6/0!/4/2)", "epubcfi(/4/2!/4/2)", "junk", "", -1, 3, 10 ** 30,
        Location(""), Location("epubcfi(/8/2!/4)"),
    ])
    def test_not_in_this_spine(self, plain, location):
        assert plain.resolve(location) is None

    @pytest.mark.parametrize("location", [True, False, 1.0, None, b"epubcfi(/6/2)", object(), ["epubcfi(/6/2)"]])
    def test_wrong_types(self, plain, location):
        with pytest.raises(InvalidArgumentError):
            plain.resolve(location)

    def test_type_is_checked_before_any_io(self, tmp_path):
        with _fs_audit.record() as rec:
            with pytest.raises(InvalidArgumentError):
                BookContent(tmp_path / "Missing.epub").resolve(True)
        assert rec.events == []

    def test_str_location_and_int(self, plain):
        text = cfi(2, "ch3", "/4/4/1:2")
        assert plain.resolve(text) == plain.resolve(Location(text)) == ResolvedLocation(
            plain.list_chapters()[2], FILE, 2, "ch3")
        assert plain.resolve(2) == ResolvedLocation(plain.list_chapters()[2], FILE, 2, "ch3")

    def test_location_unpickled_from_1_10(self, plain):
        loc = Location(cfi(1, "ch2"))
        for name in ("spine_index", "sort_key"):
            del loc.__dict__[name]
        assert title(plain.resolve(loc)) == "Two"

    def test_repeated_itemref(self, tmp_path):
        book = BookContent(write_epub_bundle(
            tmp_path / "Repeat.epub", [("c1", p("one")), ("c2", p("two")), ("c3", p("three"))],
            toc=[("One", "c1.xhtml"), ("Three", "c3.xhtml")],
            spine_xml='<itemref idref="c1"/><itemref idref="c2"/><itemref idref="c1"/><itemref idref="c3"/>'))
        assert [title(book.resolve(i)) for i in range(4)] == ["One", "One", "One", "Three"]
        assert [book.resolve(i).match for i in range(4)] == [FILE, PRECEDING, FILE, FILE]
        # A hint naming the repeated file goes to the step when it names that file.
        assert book.resolve(cfi(2, "c1")).spine_index == 2
        assert book.resolve(cfi(1, "c1")).spine_index == 0

    def test_repeated_multi_entry_file_before_its_anchors(self, tmp_path):
        """A file holding several entries, listed again later in the
        spine: a location there before its first anchor belongs to the
        section before the file's first place in the spine (where its
        sections start), not to the file just before the repeat."""
        multi = '<h1>Top</h1>' + p("x.") + '<h1 id="a">A</h1>' + p("a.") + '<h1 id="b">B</h1>' + p("b.")
        book = BookContent(write_epub_bundle(
            tmp_path / "Again.epub", [("f", p("front")), ("m", multi), ("e", p("end"))],
            toc=[("F", "f.xhtml"), ("A", "m.xhtml#a"), ("B", "m.xhtml#b"), ("E", "e.xhtml")],
            spine_xml='<itemref idref="f"/><itemref idref="m"/><itemref idref="e"/><itemref idref="m"/>'))
        assert placed(book, cfi(1, "m", "/4/2/1:0")) == ("F", PRECEDING)
        assert placed(book, cfi(3, "m", "/4/2/1:0")) == ("F", PRECEDING)
        assert placed(book, cfi(3, "m", "/4/8/1:0")) == ("A", ANCHOR)

    def test_broken_and_binary_spine_items(self, tmp_path):
        book = BookContent(_epub_shapes.mixed_types(tmp_path))
        assert placed(book, 3) == ("Page", PRECEDING)
        assert book.resolve(3).item_id == "pic"
        broken = BookContent(write_epub_bundle(
            tmp_path / "Broken.epub", [("c1", p("one")), ("c2", p("two"))], toc=[("One", "c1.xhtml")],
            spine_xml='<itemref idref="c1"/><itemref/><itemref idref="gone"/><itemref idref="c2"/>'))
        assert [(r.item_id, title(r), r.match) for r in map(broken.resolve, range(4))] == [
            ("c1", "One", FILE), (None, "One", PRECEDING), ("gone", "One", PRECEDING), ("c2", "One", PRECEDING)]


# ---------------------------------------------------------------------------
# Invariants and 1.10 parity over the fixture shapes
# ---------------------------------------------------------------------------


def _paths():
    return ["", "/4/2/1:0", "/4/6/1:0", "/4/10", "/4/99"]


@pytest.mark.parametrize("shape", sorted(_epub_shapes.SHAPES))
def test_invariants(tmp_path, shape):
    book = BookContent(_epub_shapes.SHAPES[shape](tmp_path))
    spine = book.list_spine_items()
    chapters = book.list_chapters()
    for item in spine:
        by_file = [c for c in chapters if c.spine_index is not None and item.href
                   and os.path.normpath(c.href) == os.path.normpath(item.href)]
        for path in _paths():
            resolved = book.resolve(cfi(item.index, item.item_id, path))
            assert (resolved.spine_index, resolved.item_id, resolved.unavailable) == (
                item.index, item.item_id, None)
            assert isinstance(resolved.match, ChapterMatch)
            assert (resolved.chapter is None) == (resolved.match in (FRONT, UNKNOWN))
            if resolved.chapter is not None:
                assert resolved.chapter in chapters and resolved.chapter.spine_index <= item.index
            if resolved.match == FILE:
                assert by_file == [resolved.chapter]
            elif resolved.match in (ANCHOR, UNKNOWN):
                assert len(by_file) > 1
            elif resolved.match == PRECEDING:
                assert resolved.chapter not in by_file


@pytest.mark.parametrize("shape", sorted(_epub_shapes.SHAPES))
def test_parity_with_1_10_current_chapter(tmp_path, shape):
    """Where 1.10's ``get_current_reading_chapter`` found a chapter (the
    ToC entry whose id is the CFI's hint), ``resolve`` finds the same one,
    as a FILE match."""
    book = BookContent(_epub_shapes.SHAPES[shape](tmp_path))
    chapters = book.list_chapters()
    checked = 0
    for item in book.list_spine_items():
        if item.item_id is None:
            continue
        oracle = next((c for c in chapters if c.id == item.item_id), None)
        if oracle is None:
            continue
        resolved = book.resolve(cfi(item.index, item.item_id, "/4/2/1:0"))
        assert (resolved.chapter, resolved.match) == (oracle, FILE)
        checked += 1
    if shape not in ("gutenberg", "calibre_split", "ncx_fallback"):
        assert checked


# ---------------------------------------------------------------------------
# Reads, caching, gate
# ---------------------------------------------------------------------------


class TestReads:
    def test_warm_resolve_parses_nothing(self, tmp_path, parses):
        bundle = split_book(tmp_path)
        assert placed(BookContent(bundle), cfi(0, "s0", "/4/8/1:0")) == ("B", ANCHOR)
        assert parses == {"index": 1, "anchors": 1}
        with _fs_audit.record() as rec:
            fresh = BookContent(bundle)
            assert placed(fresh, cfi(0, "s0", "/4/2/1:0")) == ("A", ANCHOR)
            assert placed(fresh, cfi(1, "s1")) == ("B", PRECEDING)
        assert parses == {"index": 1, "anchors": 1}
        assert rec.of("open") == [] and rec.of(*_fs_audit.PROCESS_EVENTS) == []

    def test_single_entry_files_read_no_chapter_file(self, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        with _fs_audit.record() as rec:
            BookContent(bundle).resolve(cfi(1, "ch2", "/4/2/1:0"))
        opened = {pathlib.PurePath(e.path).name for e in rec.of("open")}
        assert not opened & {"ch1.xhtml", "ch2.xhtml", "ch3.xhtml"}

    def test_no_du_and_no_walk(self, tmp_path, monkeypatch):
        from py_apple_books import _icloud

        monkeypatch.setattr("py_apple_books.content.subprocess.run", lambda *a, **k: pytest.fail("du ran"))
        monkeypatch.setattr(_icloud, "walk_bundle_local", lambda root: pytest.fail("walked"))
        with _fs_audit.record() as rec:
            BookContent(split_book(tmp_path)).resolve(cfi(1, "s1"))
        assert rec.of("os.scandir", "os.listdir") == []

    def test_gate_errors(self, tmp_path, icloud):
        bundle = _epub_shapes.plain(tmp_path)
        (bundle / "META-INF" / "sinf.xml").write_text("<sinf/>")
        with pytest.raises(DRMProtectedError):
            BookContent(bundle).resolve(0)
        pdf = tmp_path / "Book.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        with pytest.raises(NotEpubError):
            BookContent(pdf).resolve(0)
        other = _epub_shapes.ncx_only(tmp_path)
        icloud.mark(other / "OEBPS")
        with _fs_audit.record() as rec:
            with pytest.raises(BookNotDownloadedError):
                BookContent(other).resolve(0)
        # Refused before anything under the dataless folder is opened or
        # listed (R1, R2).
        assert rec.under(other / "OEBPS", "open", "os.scandir", "os.listdir") == []


def test_deep_nesting_is_recursion_safe(tmp_path):
    depth = 10_000
    body = "<div>" * depth + '<p id="deep">deep</p>' + "</div>" * depth + '<h2 id="end">End</h2>' + p("tail")
    book = BookContent(write_epub_bundle(
        tmp_path / "Deep.epub", [("c1", '<h1 id="top">Top</h1>' + body)],
        toc=[("Top", "c1.xhtml#top"), ("Deep", "c1.xhtml#deep"), ("End", "c1.xhtml#end")]))
    deep = "/4" + "/4" + "/2" * (depth - 1) + "/2/1:0"
    assert placed(book, cfi(0, "c1", deep)) == ("Deep", ANCHOR)
    assert placed(book, cfi(0, "c1", "/4/4[deep]/1:0")) == ("Deep", ANCHOR)
    assert placed(book, cfi(0, "c1", "/4/6/1:0")) == ("End", ANCHOR)


def test_two_megabytes_six_thousand_ids_cold_under_a_second(tmp_path):
    sections = "".join(f'<h2 id="s{i}">Section {i}</h2><p>{"word " * 60}</p>' for i in range(6000))
    toc = [(f"Section {i}", f"c1.xhtml#s{i}") for i in range(0, 6000, 10)]
    bundle = write_epub_bundle(tmp_path / "Big.epub", [("c1", sections)], toc=toc)
    assert (bundle / "OEBPS" / "c1.xhtml").stat().st_size > 2_000_000
    start = time.perf_counter()
    resolved = BookContent(bundle).resolve(cfi(0, "c1", f"/4/{2 * (2 * 4321 + 1)}/1:0"))
    elapsed = time.perf_counter() - start
    assert (title(resolved), resolved.match) == ("Section 4320", ANCHOR)
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# Bounded work on crafted files (many deeply nested ToC anchors)
# ---------------------------------------------------------------------------


def _nested_anchors_book(tmp_path, depth, anchors):
    """One file: a heading, then ``anchors`` spans inside ``depth``
    nested divs, every one of them a ToC entry (start paths of about
    ``depth * anchors`` steps together)."""
    body = ('<h1 id="top">Top</h1>' + "<div>" * depth
            + "".join(f'<span id="a{i}">x</span>' for i in range(anchors)) + "</div>" * depth)
    toc = [("Top", "c1.xhtml#top")] + [(f"S{i}", f"c1.xhtml#a{i}") for i in range(anchors)]
    return write_epub_bundle(tmp_path / "Nested.epub", [("c1", body)], toc=toc)


class TestBoundedStarts:
    def test_over_the_budget_is_section_unknown_fast_and_small(self, tmp_path):
        import tracemalloc

        bundle = _nested_anchors_book(tmp_path, 8000, 8000)
        location = cfi(0, "c1", "/4/4/2/1:0")
        # CPU time, not wall time: the bound is on the work the budget
        # allows, and a loaded CI runner shouldn't decide it. Walking all
        # 8,000 x 8,000 start steps would take far longer than this.
        start = time.process_time()
        resolved = BookContent(bundle).resolve(location)
        elapsed = time.process_time() - start
        assert (resolved.chapter, resolved.match, resolved.spine_index) == (None, UNKNOWN, 0)
        assert elapsed < 2.0
        tracemalloc.start()
        try:
            BookContent(bundle).resolve(location)  # the anchor table is cached: the starts alone
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak < 64 * 1024 * 1024

    def test_the_budget_is_the_sum_of_the_start_paths(self, gutenberg, monkeypatch):
        # Start paths: #ch1 (4, 4), #ch2 (4, 8, 2), #ch3 (4, 12): 7 steps.
        location = cfi(1, "body", "/4/10/1:2")
        monkeypatch.setattr(_content_resolve, "_MAX_START_STEPS", 7)
        assert placed(gutenberg, location) == ("II", ANCHOR)
        monkeypatch.setattr(_content_resolve, "_MAX_START_STEPS", 6)
        assert placed(gutenberg, location) == (None, UNKNOWN)
        # Never "file start", and the file after it can't take its last entry.
        assert placed(gutenberg, cfi(2, "license")) == (None, UNKNOWN)

    def test_a_batch_builds_each_files_starts_once(self, library, api, tmp_path, monkeypatch):
        sections = "".join(f'<h2 id="s{i}">S{i}</h2><p>{"word " * 5}</p>' for i in range(6000))
        bundle = write_epub_bundle(tmp_path / "Big.epub", [("c1", sections)],
                                   toc=[(f"S{i}", f"c1.xhtml#s{i}") for i in range(6000)])
        book = library.add_book("Big", path=str(bundle))
        for i in range(1000):
            n = i * 7 % 6000
            library.add_annotation(book, f"w{i}", location=cfi(0, "c1", f"/4/{2 * (2 * n + 2)}/1:0"))
        builds = []
        real = _content_resolve._Boundaries._build_sections

        def counted(self, file):
            builds.append(file)
            return real(self, file)

        monkeypatch.setattr(_content_resolve._Boundaries, "_build_sections", counted)
        annotations = list(api.list_annotations(order_by="id"))
        api.get_annotation_locations(annotations)  # cold: parses the file once
        builds.clear()
        start = time.perf_counter()
        found = api.get_annotation_locations(annotations)
        elapsed = time.perf_counter() - start
        assert builds == ["OEBPS/c1.xhtml"]
        assert elapsed < 1.0
        placed_at = [found[a.id].chapter.title for a in annotations]
        assert placed_at == [f"S{i * 7 % 6000}" for i in range(1000)]

    def test_a_batch_in_a_crafted_file_is_bounded(self, library, api, tmp_path):
        book = library.add_book("Nested", path=str(_nested_anchors_book(tmp_path, 8000, 8000)))
        for i in range(1000):
            library.add_annotation(book, f"w{i}", location=cfi(0, "c1", "/4/4/2/1:0"))
        annotations = list(api.list_annotations(order_by="id"))
        start = time.perf_counter()
        found = api.get_annotation_locations(annotations)
        elapsed = time.perf_counter() - start
        assert {(r.chapter, r.match) for r in found.values()} == {(None, UNKNOWN)}
        assert elapsed < 1.5  # cold: includes parsing the file
