"""Chapter spans (1.11): ``BookContent.get_chapter(chapter_id, span=...)``.

``span='section'`` and ``span='chapter'`` follow the reading order across
files up to the next table-of-contents entry (of any depth, or of the
entry's depth or shallower); ``span='file'`` (the default) is 1.10's
text, pinned against 1.10 in ``tests/test_chapter_compat.py``.

Every bundle is synthetic (tests/_span_shapes.py, tests/_epub_shapes.py).
"""

from __future__ import annotations

import pickle
import threading
import time

import pytest

from py_apple_books import _spans
from py_apple_books import content as content_module
from py_apple_books.content import BookContent
from py_apple_books.exceptions import (
    ChapterNotFoundError,
    InvalidArgumentError,
    InvalidChoiceError,
)
from py_apple_books.models.location import Location
from py_apple_books.testing import write_epub_bundle
from tests import _epub_shapes, _fs_audit, _span_shapes

SPANS = ("section", "chapter")


def _p(*paragraphs: str) -> str:
    return "".join(f"<p>{p}</p>" for p in paragraphs)


def _texts(content: BookContent, chapter_id: str):
    return tuple(content.get_chapter(chapter_id, span=s) for s in ("file", "section", "chapter"))


# -- the span argument ----------------------------------------------------------


@pytest.mark.parametrize("span", ["File", "page", "", " section", None, 1, b"file", ["file"]])
def test_bad_span_raises_before_any_io(tmp_path, span):
    missing = tmp_path / "Nowhere.epub"
    with _fs_audit.record() as rec:
        with pytest.raises(InvalidChoiceError) as info:
            BookContent(missing).get_chapter("1", span=span)
    assert not rec.of(*_fs_audit.PATH_EVENTS)
    err = info.value
    assert isinstance(err, InvalidArgumentError) and isinstance(err, KeyError)
    assert err.value == span
    assert err.valid == ("file", "section", "chapter")
    assert str(err).startswith("Unknown span ")
    assert "file, section, chapter" in str(err)


def test_bad_span_message_does_not_echo_long_values(tmp_path):
    with pytest.raises(InvalidChoiceError) as info:
        BookContent(tmp_path / "x.epub").get_chapter("1", span="s" * 500)
    assert "s" * 41 not in str(info.value)


def test_span_and_normalize_are_keyword_only(tmp_path):
    content = BookContent(_epub_shapes.plain(tmp_path))
    with pytest.raises(TypeError):
        content.get_chapter("ch1", "section")  # type: ignore[misc]


def test_span_checked_before_the_epub_check(tmp_path):
    pdf = tmp_path / "Doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(InvalidChoiceError):
        BookContent(pdf).get_chapter("1", span="nope")


# -- spans across files -----------------------------------------------------------


def test_multi_file_chapter(tmp_path):
    content = BookContent(_epub_shapes.subfile(tmp_path))
    assert _texts(content, "ch1") == (
        "first file text.",
        "first file text.\n\nsubfile text.",
        "first file text.\n\nsubfile text.",
    )
    assert _texts(content, "ch2") == ("second chapter text.",) * 3
    # A file the ToC doesn't list, by manifest id: to the next entry.
    assert content.get_chapter("ch1_sub01", span="section") == "subfile text."


def test_calibre_split_and_the_documented_cfi_collision(tmp_path):
    content = BookContent(_epub_shapes.calibre_split(tmp_path))
    ids = [(c.id, c.order) for c in content.list_chapters()]
    assert ids == [("1", 1), ("2", 2), ("index_split_001", 3)]
    assert content.get_chapter("2") == "Part B\n\nb text begins."
    for span in SPANS:
        assert content.get_chapter("2", span=span) == "Part B\n\nb text begins.\n\nb text continues."
        assert content.get_chapter("1", span=span) == "Part A\n\na text."
    # A reading position in B's second file: its CFI names the file, whose
    # manifest id is chapter C's table-of-contents id.
    where = Location("epubcfi(/6/4[index_split_001]!/4/2/1:0)")
    assert where.chapter_id == "index_split_001" and where.spine_index == 1
    for span in SPANS:
        assert content.get_chapter(where.chapter_id, span=span) == "Part C\n\nc text."
    # The documented ways round it: the file's own text, or the order.
    assert content.get_spine_item_text(where.spine_index) == "b text continues.\n\nPart C\n\nc text."
    assert content.get_spine_item_text(where.chapter_id) == "b text continues.\n\nPart C\n\nc text."


def test_gutenberg_section_equals_file(tmp_path):
    content = BookContent(_epub_shapes.gutenberg(tmp_path))
    for chapter in content.list_chapters():
        for chapter_id in (chapter.id, str(chapter.order)):
            file_text = content.get_chapter(chapter_id)
            assert file_text
            assert content.get_chapter(chapter_id, span="section") == file_text
            assert content.get_chapter(chapter_id, span="chapter") == file_text


def test_gutenberg_split_over_files_continues_to_the_next_entry(tmp_path):
    body1 = '<h2 id="c1">I</h2><p>one</p><h2 id="c2">II</h2><p>two begins</p>'
    body2 = "<p>two ends</p>" + '<h2 id="c3">III</h2><p>three</p>'
    content = BookContent(write_epub_bundle(
        tmp_path / "G2.epub", [("f1", body1), ("f2", body2)],
        toc=[("I", "f1.xhtml#c1"), ("II", "f1.xhtml#c2"), ("III", "f2.xhtml#c3")]))
    assert content.get_chapter("2") == "II\n\ntwo begins"
    assert content.get_chapter("2", span="section") == "II\n\ntwo begins\n\ntwo ends"
    assert content.get_chapter("1", span="section") == "I\n\none"
    assert content.get_chapter("3", span="section") == "III\n\nthree"


def test_nested_toc_section_vs_chapter(tmp_path):
    content = BookContent(_span_shapes.nested(tmp_path))
    titles = [(c.order, c.title, c.depth) for c in content.list_chapters()]
    assert titles == [(1, "Part One", 0), (2, "Chapter 1", 1), (3, "S1", 2), (4, "S2", 2),
                      (5, "Chapter 2", 1), (6, "Part Two", 0), (7, "Chapter 3", 1)]
    sec = {str(n): content.get_chapter(str(n), span="section") for n in range(1, 8)}
    chap = {str(n): content.get_chapter(str(n), span="chapter") for n in range(1, 8)}
    assert sec == {
        "1": "Part One\n\npart one intro.",
        "2": "Chapter 1\n\nch1 text.",
        "3": "S1\n\ns1 text.\n\nch1 continued.",
        "4": "S2\n\ns2 text.",
        "5": "Chapter 2\n\nch2 text.",
        "6": "Part Two",
        "7": "Chapter 3\n\nch3 text.",
    }
    assert chap["1"] == ("Part One\n\npart one intro.\n\nChapter 1\n\nch1 text.\n\nS1\n\ns1 text."
                         "\n\nch1 continued.\n\nS2\n\ns2 text.\n\nChapter 2\n\nch2 text.")
    assert chap["2"] == "Chapter 1\n\nch1 text.\n\nS1\n\ns1 text.\n\nch1 continued.\n\nS2\n\ns2 text."
    assert chap["3"] == sec["3"] and chap["4"] == sec["4"] and chap["5"] == sec["5"]
    assert chap["6"] == "Part Two\n\nChapter 3\n\nch3 text."
    assert chap["7"] == sec["7"]
    # By chapter id: the first entry with that id.
    assert content.get_chapter("ch1b", span="chapter") == sec["4"]
    assert content.get_chapter("part2", span="chapter") == chap["6"]


def test_entries_at_the_same_place(tmp_path):
    content = BookContent(_span_shapes.same_place(tmp_path))
    assert [(c.title, c.fragment, c.depth) for c in content.list_chapters()] == [
        ("Part I", "", 0), ("Chapter 1", "", 1), ("Part II", "", 0), ("Chapter 2", "c2", 1)]
    # A part whose first chapter begins where it does has no text of its own.
    assert content.get_chapter("1", span="section") == ""
    assert content.get_chapter("1", span="chapter") == "Part I\n\nfirst chapter text."
    assert content.get_chapter("2", span="section") == "Part I\n\nfirst chapter text."
    assert content.get_chapter("3", span="section") == ""
    assert content.get_chapter("3", span="chapter") == "Chapter 2\n\nsecond chapter text."
    assert content.get_chapter("4", span="section") == "Chapter 2\n\nsecond chapter text."
    # 1.10's text is unchanged.
    assert content.get_chapter("1") == "Part I\n\nfirst chapter text."


def test_nonlinear_and_broken_spine_entries_are_skipped(tmp_path):
    content = BookContent(_span_shapes.nonlinear(tmp_path))
    for span in SPANS:
        # notes (linear="no"), the comment, the processing instruction, the
        # missing and idref-less entries are skipped; c1 is read once.
        assert content.get_chapter("c1", span=span) == "One\n\nc1 text.\n\nc1b text."
        assert content.get_chapter("1", span=span) == "One\n\nc1 text.\n\nc1b text."
        # An entry in a non-linear file gives its 'file' text.
        assert content.get_chapter("notes", span=span) == "note text."
        assert content.get_chapter("c2", span=span) == "Two\n\nc2 text."
        # Manifest ids: a linear file spans to the next entry; another text
        # file gives its whole text.
        assert content.get_chapter("c1b", span=span) == "c1b text."
        assert content.get_chapter("extra", span=span) == "not in the spine."


def test_image_only_entries_give_empty_text(tmp_path):
    content = BookContent(_span_shapes.images(tmp_path))
    for span in SPANS:
        assert content.get_chapter("cover", span=span) == ""
        assert content.get_chapter("plate", span=span) == ""   # a ToC entry on an image
        assert content.get_chapter("c1", span=span) == "One\n\none text."


def test_binary_manifest_ids_raise(tmp_path):
    content = BookContent(_epub_shapes.mixed_types(tmp_path))
    for span in SPANS:
        for item_id in ("pic", "css", "font", "cover", "ncx"):
            with pytest.raises(ChapterNotFoundError) as info:
                content.get_chapter(item_id, span=span)
            assert str(info.value) == f"No text document with id '{item_id}' in this book."
        # An SVG and an HTML page are text.
        assert content.get_chapter("art", span=span) == "Vector words"


def test_unknown_id_keeps_the_110_message(tmp_path):
    content = BookContent(_epub_shapes.plain(tmp_path))
    with pytest.raises(ChapterNotFoundError) as file_error:
        content.get_chapter("nope")
    for span in SPANS:
        with pytest.raises(ChapterNotFoundError) as span_error:
            content.get_chapter("nope", span=span)
        assert str(span_error.value) == str(file_error.value)
    assert "Pass an id from the book's table of contents" in str(file_error.value)


def test_out_of_order_toc_follows_the_reading_order(tmp_path):
    content = BookContent(_span_shapes.out_of_order(tmp_path))
    assert [(c.order, c.title) for c in content.list_chapters()] == [
        (1, "C"), (2, "Y"), (3, "A"), (4, "X")]
    for span in SPANS:
        assert content.get_chapter("1", span=span) == "c text."
        assert content.get_chapter("3", span=span) == "a text."
        assert content.get_chapter("4", span=span) == "X\n\nx text."
        assert content.get_chapter("2", span=span) == "Y\n\ny text."


def test_named_anchors(tmp_path):
    content = BookContent(_span_shapes.named_anchors(tmp_path))
    # 1.10 matches ids only: without one, the whole file.
    whole = content.get_chapter("1")
    assert whole.startswith("One") and whole.endswith("three text.")
    for span in SPANS:
        assert content.get_chapter("1", span=span) == "One\n\none text."
        # An id wins over an <a name> of the same value earlier in the file.
        assert content.get_chapter("2", span=span) == "Two\n\ntwo text.\n\ndecoy"
        assert content.get_chapter("3", span=span) == "Three\n\nthree text."


def test_missing_fragments_as_start_and_stop(tmp_path):
    content = BookContent(_span_shapes.missing_fragments(tmp_path))
    assert [(c.order, c.title) for c in content.list_chapters()] == [
        (1, "One"), (2, "Ghost"), (3, "B"), (4, "Two"), (5, "Three")]
    for span in SPANS:
        # Ghost's place in m1 is unknown: it doesn't end One.
        assert content.get_chapter("1", span=span) == "One\n\none text."
        # Ghost starts at the start of m1; only entries after it in the ToC end it.
        assert content.get_chapter("2", span=span) == "One\n\none text."
        # Two's fragment names nothing in m2: it ends B at the start of m2.
        assert content.get_chapter("3", span=span) == "B\n\nb text."
        assert content.get_chapter("4", span=span) == "two text."
        assert content.get_chapter("5", span=span) == "Three\n\nthree text."
    # 1.10: a missing start gives the whole file.
    assert content.get_chapter("2") == "One\n\none text.\n\nB\n\nb text."


def test_order_ids_win_in_span_modes(tmp_path):
    content = BookContent(_span_shapes.digit_ids(tmp_path))
    assert [(c.id, c.order) for c in content.list_chapters()] == [("3", 1), ("1", 2), ("2", 3)]
    # 1.10: the first entry whose id or order matches.
    assert content.get_chapter("3") == "first."
    assert content.get_chapter("1") == "first."
    assert content.get_chapter("2") == "second."
    for span in SPANS:
        assert content.get_chapter("3", span=span) == "third."
        assert content.get_chapter("1", span=span) == "first."
        assert content.get_chapter("2", span=span) == "second."


def test_order_strings_must_be_canonical_and_in_range(tmp_path):
    content = BookContent(_epub_shapes.plain(tmp_path))
    assert content.get_chapter("2", span="section") == "Two\n\nBeta text two."
    assert content.get_chapter(2, span="section") == "Two\n\nBeta text two."
    for odd in ("02", "0", "4", "+2", " 2", "２", "9" * 5000):
        with pytest.raises(ChapterNotFoundError):
            content.get_chapter(odd, span="section")


def test_spine_comments_and_processing_instructions(tmp_path):
    content = BookContent(_epub_shapes.phantom_sections(tmp_path))
    # 1.10 lists a 'Section N' for the comment and the instruction; they
    # name no file, so spans give no text for them.
    assert [c.href for c in content.list_chapters()][1::2] == ["", ""]
    for span in SPANS:
        assert content.get_chapter("1", span=span) == "section one."
        assert content.get_chapter("2", span=span) == ""
        assert content.get_chapter("3", span=span) == "section two."
        assert content.get_chapter("4", span=span) == ""
        assert content.get_chapter("5", span=span) == "section three."


def test_entry_without_a_file_gives_empty_text(tmp_path):
    content = BookContent(write_epub_bundle(
        tmp_path / "Heading.epub", [("p1", "<h1>Part</h1>"), ("c1", "<p>one</p>")],
        toc=[("Part", None, [("One", "c1.xhtml")]), ("Two", "p1.xhtml")]))
    assert content.list_chapters()[0].href == ""
    for span in SPANS:
        assert content.get_chapter("1", span=span) == ""
        assert content.get_chapter("c1", span=span) == "one"
    # "Two" comes first in the reading order; "One" (deeper) doesn't end its chapter.
    assert content.get_chapter("p1", span="section") == "Part"
    assert content.get_chapter("p1", span="chapter") == "Part\n\none"


def test_toc_entry_outside_the_spine_gives_its_file_text(tmp_path):
    content = BookContent(write_epub_bundle(
        tmp_path / "Outside.epub",
        [("c1", "<p>one</p>"), ("app", '<p>pre</p><h2 id="a1">A1</h2><p>a1</p><h2 id="a2">A2</h2><p>a2</p>',
                                {"in_spine": False})],
        toc=[("One", "c1.xhtml"), ("A1", "app.xhtml#a1"), ("A2", "app.xhtml#a2")]))
    for span in SPANS:
        assert content.get_chapter("2", span=span) == content.get_chapter("2") == "A1\n\na1"
        assert content.get_chapter("3", span=span) == "A2\n\na2"
        assert content.get_chapter("1", span=span) == "one"


def test_repeated_files_are_read_once(tmp_path, monkeypatch):
    content = BookContent(_span_shapes.nonlinear(tmp_path))
    reads = []
    real = BookContent._span_item_bytes

    def counting(self, step):
        reads.append(step.href)
        return real(self, step)

    monkeypatch.setattr(BookContent, "_span_item_bytes", counting)
    assert content.get_chapter("c1", span="section") == "One\n\nc1 text.\n\nc1b text."
    assert sorted(reads) == ["OEBPS/c1.xhtml", "OEBPS/c1b.xhtml"]


def test_a_thousand_repeated_itemrefs(tmp_path, monkeypatch):
    spine = ('<itemref idref="r1"/>' + '<itemref idref="r2"/><itemref idref="r1"/>' * 500
             + '<itemref idref="r3"/>' + '<itemref idref="r2"/>' * 1000)
    content = BookContent(write_epub_bundle(
        tmp_path / "Repeat.epub",
        [("r1", "<h1>One</h1>" + _p("r1 " * 2000)), ("r2", _p("r2 " * 2000)), ("r3", _p("three"))],
        toc=[("One", "r1.xhtml"), ("Three", "r3.xhtml")], spine_xml=spine))
    reads = []
    real = BookContent._span_item_bytes
    monkeypatch.setattr(BookContent, "_span_item_bytes",
                        lambda self, step: reads.append(step.href) or real(self, step))
    started = time.perf_counter()
    text = content.get_chapter("1", span="chapter")
    elapsed = time.perf_counter() - started
    assert text.startswith("One\n\nr1 ") and text.endswith("r2")
    assert text.count("r2 ") == 1999
    assert len(reads) == 2
    assert elapsed < 2.0
    assert content.get_chapter("2", span="section") == "three"


def _nav_bundle(dest, nav: str, opf_dir: str):
    sub = "text/" if opf_dir == "" else ""
    return write_epub_bundle(
        dest / f"Nav-{nav}-{opf_dir.replace('/', '_') or 'root'}.epub",
        [("c1", "<h1>One</h1>" + _p("c1 text."), {"href": f"{sub}c1.xhtml"}),
         ("c1b", _p("c1b text."), {"href": f"{sub}c1b.xhtml"}),
         ("c2", "<h1>Two</h1>" + _p("c2 text.") + '<h2 id="s">S</h2>' + _p("s text."),
          {"href": f"{sub}c2.xhtml"})],
        toc=[("One", f"{sub}c1.xhtml"), ("Two", f"{sub}c2.xhtml", [("S", f"{sub}c2.xhtml#s")])],
        nav=nav, opf_dir=opf_dir)


@pytest.mark.parametrize("opf_dir", ["OEBPS", "", "a/b"])
@pytest.mark.parametrize("nav", ["both", "nav", "ncx", "ncx-undeclared"])
def test_nav_variants_and_package_folders(tmp_path, nav, opf_dir):
    content = BookContent(_nav_bundle(tmp_path, nav, opf_dir))
    assert [c.order for c in content.list_chapters()] == [1, 2, 3]
    assert content.get_chapter("1", span="section") == "One\n\nc1 text.\n\nc1b text."
    assert content.get_chapter("2", span="section") == "Two\n\nc2 text."
    assert content.get_chapter("2", span="chapter") == "Two\n\nc2 text.\n\nS\n\ns text."
    assert content.get_chapter("3", span="section") == "S\n\ns text."
    assert content.get_chapter("c1b", span="section") == "c1b text."


def test_normalize_unicode(tmp_path):
    content = BookContent(_span_shapes.unicode_text(tmp_path))
    raw = content.get_chapter("u1")
    assert "­" in raw and "​" in raw and "é" in raw
    assert content.get_chapter("u1", normalize_unicode=True) == "coop era\n\ncafé"
    assert content.get_chapter("u1", span="file", normalize_unicode=True) == "coop era\n\ncafé"
    assert content.get_chapter("u1", span="section", normalize_unicode=True) == "coop era\n\ncafé\n\nnext."
    # The default form is untouched by a normalized call.
    assert content.get_chapter("u1") == raw
    assert content.get_chapter("u1", normalize_unicode=False) == raw


def test_spans_open_no_file_once_the_book_is_loaded(tmp_path):
    bundle = _span_shapes.nested(tmp_path)
    content = BookContent(bundle)
    first = content.get_chapter("1", span="chapter")
    with _fs_audit.record() as rec:
        for n in range(1, 8):
            for span in SPANS:
                content.get_chapter(str(n), span=span)
        assert content.get_chapter("1", span="chapter") == first
    assert not rec.of("open", "os.listdir", "os.scandir")
    assert not rec.of(*_fs_audit.PROCESS_EVENTS)


def test_the_plan_is_built_once_by_eight_threads(tmp_path, monkeypatch):
    content = BookContent(_span_shapes.nested(tmp_path))
    loads, plans = [], []
    real_read, real_plan = BookContent._read_book, _spans.build_plan

    def read_book(self):
        loads.append(1)
        time.sleep(0.05)
        return real_read(self)

    def build_plan(*args):
        plans.append(1)
        time.sleep(0.05)
        return real_plan(*args)

    monkeypatch.setattr(BookContent, "_read_book", read_book)
    monkeypatch.setattr(content_module._spans, "build_plan", build_plan)
    barrier = threading.Barrier(8)
    results, errors = [], []

    def work():
        try:
            barrier.wait()
            results.append(content.get_chapter("2", span="chapter"))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert len(results) == 8 and len(set(results)) == 1
    assert len(loads) == 1 and len(plans) == 1


def test_unpickled_instance_with_its_book(tmp_path):
    content = BookContent(_span_shapes.nested(tmp_path))
    expected = {(n, s): content.get_chapter(str(n), span=s) for n in range(1, 8) for s in SPANS}
    clone = pickle.loads(pickle.dumps(content))
    assert clone._book is not None and clone._package is None
    assert {(n, s): clone.get_chapter(str(n), span=s) for n in range(1, 8) for s in SPANS} == expected


def test_a_reread_book_gets_a_new_plan(tmp_path):
    bundle = _epub_shapes.plain(tmp_path)
    first = BookContent(bundle)
    assert first.get_chapter("1", span="section") == "One\n\nAlpha text one.\n\nmore"
    write_epub_bundle(bundle, [("ch1", _p("changed.")), ("ch2", _p("two."))],
                      toc=[("One", "ch1.xhtml"), ("Two", "ch2.xhtml")])
    content_module.clear_content_cache()
    # The instance keeps the book it read; a new one reads the new files.
    assert first.get_chapter("1", span="section") == "One\n\nAlpha text one.\n\nmore"
    assert BookContent(bundle).get_chapter("1", span="section") == "changed."


def test_not_an_epub(tmp_path):
    pdf = tmp_path / "Doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    from py_apple_books.exceptions import NotEpubError
    for span in ("file", *SPANS):
        with pytest.raises(NotEpubError):
            BookContent(pdf).get_chapter("1", span=span)
