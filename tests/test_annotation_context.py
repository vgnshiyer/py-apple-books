"""``PyAppleBooks.get_annotation_context`` (stream 3.1, positions F40 and
chapter-text F29-locate): an annotation's highlight with the text around
it, in parts, found by tiers, never the chapter opening in its place."""

from __future__ import annotations

import pickle
import random
import tracemalloc

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books._api.positions import _locate_highlight, _snap_parts
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import (
    AnnotationNotFoundError,
    AppleBooksError,
    BookNotDownloadedError,
    ChapterNotFoundError,
    ContextUnavailableError,
    DRMProtectedError,
    InvalidArgumentError,
    NotEpubError,
    NotInLibraryError,
)
from py_apple_books.positions import AnnotationContext, ChapterMatch, TextMatch, UnavailableReason
from py_apple_books.testing import write_epub_bundle
from py_apple_books.testing.fixture import STORE_SERIES
from py_apple_books.utils import snap_window
from tests import _epub_shapes, _fs_audit
from tests._positions_helpers import cfi, gutenberg_body, icloud, p  # noqa: F401

LONG = " ".join(f"word{i}" for i in range(200))


@pytest.fixture
def book(library, tmp_path):
    bundle = write_epub_bundle(
        tmp_path / "Ctx.epub",
        [("c1", p(LONG, "The quick brown fox jumps over the lazy dog.", LONG)),
         ("c2", p("He said: don’t stop. Café au lait, NAÏVE reader.",
                  "Soft hy­phen­ated word here.", "A line\nbreak inside.")),
         ("c3", p("yes, I said.", "Then he said yes to it, and left.", "yes")),
         ("img", '<img src="pic.png" alt=""/>'),
         ("body", gutenberg_body()),
         ("pic", b"\x89PNG", {"raw": True, "href": "pic.png", "media_type": "image/png"}),
         ("notes", p("A note outside the spine."), {"in_spine": False})],
        toc=[("One", "c1.xhtml"), ("Two", "c2.xhtml"), ("Three", "c3.xhtml"), ("I", "body.xhtml#ch1"),
             ("II", "body.xhtml#ch2"), ("III", "body.xhtml#ch3")])
    return library.add_book("Context Book", path=str(bundle))


def add(library, book, text, location, **kwargs):
    return library.add_annotation(book, text, location=location, **kwargs)


class TestContext:
    def test_parts_and_window(self, api, library, book):
        aid = add(library, book, "jumps over", cfi(0, "c1", "/4/4/1:20"))
        ctx = api.get_annotation_context(aid, 10, 9)
        assert isinstance(ctx, AnnotationContext)
        # Snapped to spaces as 1.10's window is: partial words are dropped.
        assert (ctx.before, ctx.highlight, ctx.after) == ("fox ", "jumps over", " the")
        assert (ctx.clipped_start, ctx.clipped_end) == (True, True)
        assert (ctx.annotation_id, ctx.book_id, ctx.item_id, ctx.spine_index) == (aid, book["id"], "c1", 0)
        assert (ctx.chapter.title, ctx.match) == ("One", ChapterMatch.FILE)
        assert (ctx.text_match, ctx.occurrences, ctx.disambiguated) == (TextMatch.EXACT, 1, False)
        assert str(ctx) == ctx.text == "…fox jumps over the…"
        assert str(ctx) == api.get_annotation_surrounding_text(aid, 10, 9)

    def test_defaults_match_the_wrapper(self, api, library, book):
        aid = add(library, book, "The quick brown fox", cfi(0, "c1"))
        assert str(api.get_annotation_context(aid)) == api.get_annotation_surrounding_text(aid)

    def test_chapter_in_a_multi_entry_file(self, api, library, book):
        aid = add(library, book, "two two", cfi(4, "body", "/4/10,/1:0,/1:7"))
        ctx = api.get_annotation_context(aid, 0, 0)
        assert (ctx.chapter.title, ctx.match, ctx.highlight) == ("II", ChapterMatch.ANCHOR, "two two")

    def test_whole_file_not_the_section(self, api, library, book):
        """The text is the whole spine file, so a highlight before its
        ToC entry's anchor is still found (``get_chapter`` would start
        after it)."""
        aid = add(library, book, "Title page", cfi(4, "body", "/4/2/1:0"))
        ctx = api.get_annotation_context(aid, 0, 0)
        # Before the file's first anchor: the section of the file before.
        assert (ctx.highlight, ctx.match, ctx.chapter.title) == ("Title page", ChapterMatch.PRECEDING, "Three")

    def test_annotation_object(self, api, library, book, sql_trace):
        aid = add(library, book, "lazy dog", cfi(0, "c1"))
        ann = api.get_annotation_by_id(aid)
        del sql_trace[:]
        ctx = api.get_annotation_context(ann, 5, 5)
        assert ctx.highlight == "lazy dog"
        assert not [s for s, _ in sql_trace if "ZAEANNOTATION" in s]

    def test_annotation_from_another_library_is_read_again(self, api, library, book, make_library):
        aid = add(library, book, "lazy dog", cfi(0, "c1"))
        other = make_library()
        other.add_annotation("ELSEWHERE", "other text", location=cfi(0, "c1"))
        db = LibraryDB(data_dir=other.data_dir)
        try:
            with use_library(db):
                foreign = PyAppleBooks().get_annotation_by_id(aid)
        finally:
            db.close()
        assert foreign.selected_text == "other text"
        assert api.get_annotation_context(foreign, 0, 0).highlight == "lazy dog"

    def test_hint_naming_a_document_outside_the_spine(self, api, library, book):
        aid = add(library, book, "note outside", "epubcfi(/6/40[notes]!/4/2/1:2)")
        ctx = api.get_annotation_context(aid, 2, 0)
        assert (ctx.item_id, ctx.spine_index, ctx.chapter, ctx.match, ctx.highlight) == (
            "notes", None, None, None, "note outside")
        assert str(ctx) == api.get_annotation_surrounding_text(aid, 2, 0)

    def test_hint_outside_the_spine_with_a_step_in_range(self, api, library, book):
        # The step names c1 (spine 0) but the hint a non-spine document:
        # read the hint's document, as 1.10 does; the chapter is where the
        # step places the location.
        aid = add(library, book, "note outside", "epubcfi(/6/2[notes]!/4/2/1:2)")
        ctx = api.get_annotation_context(aid, 2, 0)
        assert (ctx.item_id, ctx.spine_index, ctx.highlight) == ("notes", None, "note outside")
        assert (ctx.chapter.title, ctx.match) == ("One", ChapterMatch.FILE)
        assert str(ctx) == api.get_annotation_surrounding_text(aid, 2, 0)

    def test_non_text_hint_outside_the_spine_falls_back_to_the_step(self, api, library, tmp_path):
        bundle = write_epub_bundle(
            tmp_path / "Pic.epub",
            [("c1", p("The quick brown fox.")),
             ("pic", b"\x89PNG", {"raw": True, "href": "pic.png", "media_type": "image/png", "in_spine": False})],
            toc=[("One", "c1.xhtml")])
        aid = add(library, library.add_book("Pic", path=str(bundle)), "quick brown", "epubcfi(/6/2[pic]!/4/2/1:4)")
        ctx = api.get_annotation_context(aid, 0, 0)
        assert (ctx.item_id, ctx.spine_index, ctx.highlight, ctx.chapter.title) == ("c1", 0, "quick brown", "One")


class TestTiers:
    @pytest.mark.parametrize("text, tier, highlight", [
        ("NAÏVE reader", TextMatch.EXACT, "NAÏVE reader"),
        ("A line break", TextMatch.WHITESPACE, "A line break"),
        ("Soft hyphenated word", TextMatch.INVISIBLE, "Soft hy­phen­ated word"),
        ("don't stop", TextMatch.FOLDED, "don’t stop"),
        ("cafe au lait", TextMatch.FOLDED, "Café au lait"),
        ("naive READER", TextMatch.FOLDED, "NAÏVE reader"),
    ])
    def test_tier(self, api, library, book, text, tier, highlight):
        aid = add(library, book, text, cfi(1, "c2"))
        ctx = api.get_annotation_context(aid, 0, 0)
        assert (ctx.text_match, ctx.highlight) == (tier, highlight)

    def test_exact_means_the_first_whitespace_match_is_verbatim(self):
        # No separate exact pass: the first whitespace-flexible occurrence
        # is taken (as 1.10's window), and it isn't verbatim.
        text = "A line\nbreak first. Then A line break again."
        found = _locate_highlight(text, "A line break", None)
        assert (found.start, found.text_match, found.occurrences) == (0, TextMatch.WHITESPACE, 2)
        assert _locate_highlight("Then A line break.", "A line break", None).text_match is TextMatch.EXACT

    def test_wrapper_stays_frozen_on_curly_quotes(self, api, library, book):
        """1.10's wrapper matches whitespace only: a straight apostrophe
        doesn't find a curly one there, and still doesn't (R10)."""
        aid = add(library, book, "don't stop", cfi(1, "c2"))
        assert api.get_annotation_surrounding_text(aid) == ""
        assert api.get_annotation_context(aid).highlight == "don’t stop"

    def test_occurrences_and_first(self, api, library, book):
        aid = add(library, book, "yes", cfi(2, "c3"))
        ctx = api.get_annotation_context(aid, 0, 7)
        assert (ctx.occurrences, ctx.disambiguated, ctx.highlight, ctx.after) == (3, False, "yes", ", I")
        assert str(ctx) == api.get_annotation_surrounding_text(aid, 0, 7) == "yes, I…"

    def test_disambiguated_by_the_representative_text(self, api, library, book):
        aid = add(library, book, "yes", cfi(2, "c3"), raw={"ZANNOTATIONREPRESENTATIVETEXT": "he said yes to it"})
        ctx = api.get_annotation_context(aid, 9, 7)
        assert (ctx.occurrences, ctx.disambiguated) == (3, True)
        assert (ctx.before, ctx.highlight, ctx.after) == ("he said ", "yes", " to")
        # The wrapper (1.10) takes the first occurrence.
        assert api.get_annotation_surrounding_text(aid, 9, 7) == "yes, I…"

    def test_representative_occurring_twice_does_not_disambiguate(self):
        found = _locate_highlight("a yes b. a yes b. yes", "yes", "a yes b.")
        assert (found.start, found.disambiguated, found.occurrences) == (2, False, 3)

    def test_representative_text_alone(self, api, library, book):
        aid = add(library, book, None, cfi(0, "c1"), raw={"ZANNOTATIONREPRESENTATIVETEXT": " lazy dog. "})
        assert api.get_annotation_context(aid, 0, 0).highlight == "lazy dog."

    def test_memory_does_not_grow_with_the_occurrences(self):
        """Matches are counted, not kept: a file full of a one-letter
        highlight (a million occurrences, at each tier) costs at most a
        copy or two of the text (the invisible and folded tiers), never
        a span per occurrence."""
        n = 1_000_000
        # Bytes per occurrence allowed: 0 (the text is searched in
        # place), the cleaned copy, the fold of the text. A kept span
        # would be over 60.
        for text, selected, tier, per in (("a " * n, "a", TextMatch.EXACT, 0),
                                          ("a \u00ad" * n, "a\u00ad", TextMatch.INVISIBLE, 4),
                                          ("A " * n, "a", TextMatch.FOLDED, 6)):
            tracemalloc.start()
            try:
                found = _locate_highlight(text, selected, "a a a")
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            assert (found.start, found.text_match, found.occurrences) == (0, tier, n)
            assert peak < per * n + 2 * 1024 * 1024, (tier, peak)

    def test_disambiguation_scans_without_a_list(self):
        text = "yes " * 100_000 + "so he said yes to it. " + "yes " * 100_000
        tracemalloc.start()
        try:
            found = _locate_highlight(text, "yes", "he said yes to it")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert (found.start, found.occurrences, found.disambiguated) == (400_011, 200_001, True)
        assert peak < 1024 * 1024

    def test_invisible_tier_is_find_passage(self):
        """The streaming invisible tier gives ``text._find_passage``'s
        spans, in order."""
        from py_apple_books._api.positions import _invisible_spans
        from py_apple_books.text import _find_passage

        rng = random.Random(1611)
        pieces = ["a", "b", "ab", " ", "\n", "\u00ad", "\u200b", "\ufeff", "a b", "é"]
        for _ in range(20_000):
            text = "".join(rng.choice(pieces) for _ in range(rng.randrange(0, 30)))
            passage = "".join(rng.choice(pieces) for _ in range(rng.randrange(0, 6)))
            assert list(_invisible_spans(text, passage)) == _find_passage(text, passage), (text, passage)

    def test_never_the_chapter_opening(self, api, library, book):
        aid = add(library, book, "words that are nowhere", cfi(0, "c1"))
        with pytest.raises(ContextUnavailableError) as exc:
            api.get_annotation_context(aid)
        assert exc.value.reason == ContextUnavailableError.HIGHLIGHT_NOT_FOUND
        assert api.get_annotation_surrounding_text(aid) == ""


class TestReasons:
    def _raised(self, api, aid):
        with pytest.raises(ContextUnavailableError) as exc:
            api.get_annotation_context(aid)
        return exc.value

    @pytest.mark.parametrize("kind", ["no_cfi", "no_file", "not_spine"])
    def test_no_location(self, api, library, book, kind):
        location = {"no_cfi": None, "no_file": cfi(30, "gone"), "not_spine": "epubcfi(/4/2!/4)"}[kind]
        aid = add(library, book, "lazy dog", location)
        err = self._raised(api, aid)
        assert (err.reason, err.annotation_id) == ("no_location", aid)

    def test_reasons_and_messages(self, api, library, book, tmp_path):
        cases = {
            "no_highlight_text": library.add_annotation(book, None, kind="bookmark", location=cfi(0, "c1")),
            "orphaned": library.add_annotation("GONE-ASSET", "x", location=cfi(0, "c1")),
            "empty_chapter": add(library, book, "anything", cfi(3, "img")),
            "highlight_not_found": add(library, book, "absent words", cfi(0, "c1")),
        }
        for reason, aid in cases.items():
            err = self._raised(api, aid)
            assert (err.reason, err.annotation_id) == (reason, aid)
            assert UnavailableReason.of(err) == UnavailableReason(reason)
            message = str(err)
            assert message and "/" not in message and str(tmp_path) not in message and "Context Book" not in message
            copy = pickle.loads(pickle.dumps(err))
            assert (str(copy), copy.reason, copy.annotation_id) == (message, reason, aid)

    def test_annotation_reasons_come_before_the_book(self, api, library, tmp_path, icloud):
        cloud = library.add_book("Cloud", path=str(_epub_shapes.plain(tmp_path)), state=3)
        aid = library.add_annotation(cloud, None, kind="bookmark", location=cfi(0, "ch1"))
        assert self._raised(api, aid).reason == "no_highlight_text"


class TestBookErrors:
    def _error(self, api, library, book, location=None):
        aid = add(library, book, "Alpha", location or cfi(0, "ch1"))
        with pytest.raises(AppleBooksError) as exc:
            api.get_annotation_context(aid)
        # 1.10's handlers catch it; UnavailableReason maps it.
        assert UnavailableReason.of(exc.value) is not None
        return exc.value

    def test_no_file(self, api, library):
        err = self._error(api, library, library.add_book("Never Downloaded"))
        assert type(err) is BookNotDownloadedError and "has not been downloaded" in str(err)

    def test_cloud_only_touches_no_file(self, api, library, tmp_path, icloud):
        bundle = _epub_shapes.plain(tmp_path)
        cloud = library.add_book("Cloud", path=str(bundle), state=3)
        icloud.mark()
        err = self._error(api, library, cloud)
        assert type(err) is BookNotDownloadedError and "stored in iCloud" in str(err)
        assert icloud.touched(bundle) == []

    def test_not_owned(self, api, library):
        err = self._error(api, library, library.add_book("Volume", data_source=STORE_SERIES))
        assert isinstance(err, NotInLibraryError)

    def test_drm(self, api, library, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        (bundle / "META-INF" / "sinf.xml").write_text("<sinf/>")
        assert isinstance(self._error(api, library, library.add_book("Drm", path=str(bundle))), DRMProtectedError)

    def test_pdf(self, api, library, tmp_path):
        pdf = tmp_path / "Doc.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        err = self._error(api, library, library.add_book("Pdf", path=str(pdf), content_type=3))
        assert isinstance(err, NotEpubError) and "PDF" in str(err)

    def test_placeholder_file(self, api, library, tmp_path, icloud):
        bundle = _epub_shapes.plain(tmp_path)
        plain = library.add_book("Plain", path=str(bundle))
        icloud.mark(bundle / "OEBPS" / "ch1.xhtml")
        with _fs_audit.record() as rec:
            err = self._error(api, library, plain)
        assert isinstance(err, BookNotDownloadedError)
        assert rec.under(bundle / "OEBPS" / "ch1.xhtml", "open") == []

    def test_binary_spine_item(self, api, library, book):
        err = self._error(api, library, book, cfi(5, "pic"))
        assert isinstance(err, ChapterNotFoundError) and UnavailableReason.of(err) == UnavailableReason.CHAPTER_NOT_FOUND

    def test_reads_only_the_annotation_file(self, api, library, book, monkeypatch):
        path = api.get_book_by_id(book["id"]).path
        aid = add(library, book, "lazy dog", cfi(0, "c1"))
        monkeypatch.setattr("py_apple_books.content.subprocess.run", lambda *a, **k: pytest.fail("du ran"))
        api.get_annotation_context(aid)
        with _fs_audit.record() as rec:
            api.get_annotation_context(aid)
        opened = [e.path for e in rec.of("open") if e.path and e.path.startswith(path)]
        assert opened == [f"{path}/OEBPS/c1.xhtml"]
        assert rec.of("os.scandir", "os.listdir") == [] and rec.of(*_fs_audit.PROCESS_EVENTS) == []


class TestArguments:
    @pytest.mark.parametrize("size", [-1, 1.5, True, "300", None])
    def test_bad_sizes(self, api, library, book, size, sql_trace):
        aid = add(library, book, "lazy dog", cfi(0, "c1"))
        del sql_trace[:]
        for args in ((aid, size, 10), (aid, 10, size)):
            with pytest.raises(InvalidArgumentError):
                api.get_annotation_context(*args)
        assert sql_trace == []

    def test_integral_sizes(self, api, library, book):
        aid = add(library, book, "lazy dog", cfi(0, "c1"))
        assert api.get_annotation_context(aid, 10.0, 0) == api.get_annotation_context(aid, 10, 0)

    def test_unknown_id(self, api):
        with pytest.raises(AnnotationNotFoundError):
            api.get_annotation_context(424242)

    def test_bool_id(self, api):
        with pytest.raises(InvalidArgumentError):
            api.get_annotation_context(True)


# ---------------------------------------------------------------------------
# The window and the 1.10 wrapper
# ---------------------------------------------------------------------------

_SPACES = [" ", "  ", "\n", "\n\n", "\t", " ", " ", "\x1c", " \n "]


def _random_text(rng: random.Random) -> str:
    words = ["a", "bb", "ccc", "word", "x.", "—", "é", "longerword"]
    pieces = []
    for _ in range(rng.randrange(0, 60)):
        pieces.append(rng.choice(words))
        pieces.append(rng.choice(_SPACES))
    if rng.random() < 0.3:
        pieces.insert(0, rng.choice(_SPACES))
    return "".join(pieces)


def test_snap_parts_is_snap_window():
    rng = random.Random(311)
    for _ in range(20_000):
        text = _random_text(rng)
        pos = rng.randrange(0, len(text) + 1)
        length = rng.randrange(0, len(text) - pos + 1)
        before, after = rng.randrange(0, 40), rng.randrange(0, 40)
        b, h, a, cs, ce = _snap_parts(text, pos, length, before, after)
        expected = snap_window(text, pos, length, before, after)
        assert ("…" if cs else "") + b + h + a + ("…" if ce else "") == expected


def test_str_matches_the_wrapper_whenever_it_finds_the_text(api, library, tmp_path):
    """The 1.10 wrapper's window, for every highlight it finds, is
    ``str()`` of the context (unless the context chose another of
    several occurrences by the representative text)."""
    rng = random.Random(1110)
    paragraphs = []
    for _ in range(30):
        paragraphs.append(" ".join(rng.choice(["alpha", "beta", "gamma", "delta", "it’s", "don't", "Café",
                                               "x­y", "end."]) for _ in range(rng.randrange(3, 25))))
    bundle = write_epub_bundle(tmp_path / "Oracle.epub", [("c1", p(*paragraphs))], toc=[("One", "c1.xhtml")])
    book = library.add_book("Oracle", path=str(bundle))
    words = " ".join(paragraphs).split()
    checked = 0
    for i in range(150):
        start = rng.randrange(0, len(words))
        selected = " ".join(words[start:start + rng.randrange(1, 6)])
        if rng.random() < 0.2:
            selected = selected.replace("it’s", "it's")
        aid = library.add_annotation(book, selected, location=cfi(0, "c1"))
        before, after = rng.randrange(0, 200), rng.randrange(0, 200)
        legacy = api.get_annotation_surrounding_text(aid, before, after)
        try:
            ctx = api.get_annotation_context(aid, before, after)
        except ContextUnavailableError:
            assert legacy == ""
            continue
        if legacy:
            assert not ctx.disambiguated
            assert str(ctx) == legacy
            checked += 1
    assert checked > 50


class TestUnsafeFiles:
    def test_spine_file_symlinked_outside_the_bundle_is_refused_unopened(self, api, library, tmp_path):
        from py_apple_books.exceptions import UnsafeEpubEntryError

        bundle = write_epub_bundle(tmp_path / "Sym.epub", [("c1", p("alpha beta gamma")), ("c2", p("delta"))],
                                   toc=[("One", "c1.xhtml"), ("Two", "c2.xhtml")])
        outside = tmp_path / "outside.xhtml"
        outside.write_text("<html><body><p>alpha beta gamma outside</p></body></html>")
        target = bundle / "OEBPS" / "c1.xhtml"
        target.unlink()
        target.symlink_to(outside)
        aid = add(library, library.add_book("Sym", path=str(bundle)), "alpha beta", cfi(0, "c1"))
        with _fs_audit.block(_fs_audit.Policy(deny=(str(outside),))) as rec:
            with pytest.raises(UnsafeEpubEntryError) as caught:
                api.get_annotation_context(aid)
        assert rec.under(outside, "open") == [] and rec.refused == []
        assert str(tmp_path) not in str(caught.value) and "outside.xhtml" not in str(caught.value)
