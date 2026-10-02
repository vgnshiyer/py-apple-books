"""``BookContent.search`` (1.11): folded search of a book's text, up to a
boundary, a page at a time.

Every bundle is synthetic (``write_epub_bundle``).
"""

from __future__ import annotations

import pytest

from py_apple_books.content import (
    MAX_SEARCH_HITS,
    MAX_SNIPPET_CONTEXT,
    BookContent,
    TextHit,
    TextSearchResult,
)
from py_apple_books.exceptions import InvalidArgumentError, NotEpubError
from py_apple_books.positions import (
    BoundaryPrecision,
    BoundarySource,
    ReadBoundary,
    ResolvedBoundary,
    TextPosition,
)
from py_apple_books.testing import write_epub_bundle
from py_apple_books.text import fold_for_match

SENTINEL = "Zanzibar"


def bundle(dest, files, toc=(), **kwargs):
    return write_epub_bundle(dest / "Search.epub", files, toc or [("One", f"{files[0][0]}.xhtml")], **kwargs)


def texts(content: BookContent, hits):
    """The matched text of each hit, from the item's text."""
    return [content.get_spine_item_text(h.start.spine_index)[h.start.offset:h.end.offset] for h in hits]


@pytest.fixture
def story(tmp_path):
    """Five chapters; the sentinel appears late in c2 and in c3 and c4."""
    return BookContent(bundle(tmp_path, [
        ("c1", "<p>Café owners met at the CAFE. The cafe closed.</p>"),
        ("c2", f"<p>It was a quiet morning in the town.</p><p>Later, {SENTINEL} was named the culprit.</p>"),
        ("c3", f"<p>Everyone talked about {SENTINEL}.</p>"),
        ("c4", f"<p>{SENTINEL} left the café at dawn.</p>"),
        ("c5", "<p>The end.</p>"),
    ], [("One", "c1.xhtml"), ("Two", "c2.xhtml"), ("Three", "c3.xhtml"), ("Four", "c4.xhtml"),
        ("Five", "c5.xhtml")]))


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


class TestExamples:
    def test_folded_matching(self, story):
        result = story.search("cafe")
        assert isinstance(result, TextSearchResult) and all(isinstance(h, TextHit) for h in result.hits)
        assert texts(story, result.hits) == ["Café", "CAFE", "cafe", "café"]
        assert [h.start.spine_index for h in result.hits] == [0, 0, 0, 3]
        assert [h.item_id for h in result.hits] == ["c1", "c1", "c1", "c4"]
        assert result.next_start is None and not result.truncated
        assert (result.total, result.withheld_in_item, result.withheld_later) == (None, None, None)

    @pytest.mark.parametrize("query, found", [
        ("CAFÉ", 4), ("  cafe  ", 4), ("café owners", 1), ("cafe\nowners", 1), ("the culprit", 1),
        ("ZANZIBAR", 3), ("the", 6),
    ])
    def test_queries(self, story, query, found):
        assert len(story.search(query, limit=None).hits) == found

    def test_typographic_variants(self, tmp_path):
        content = BookContent(bundle(tmp_path, [("c1", "<p>Don’t stop—the “oﬃce” is open…</p>")]))
        for query, matched in (("don't", "Don’t"), ('"office"', "“oﬃce”"), ("stop-the", "stop—the"),
                               ("open...", "open…")):
            assert texts(content, content.search(query).hits) == [matched], query

    def test_positions_are_item_text_offsets(self, story):
        for hit in story.search("zanzibar").hits:
            text = story.get_spine_item_text(hit.item_id)
            assert fold_for_match(text[hit.start.offset:hit.end.offset]) == "zanzibar"
            assert hit.start.spine_index == hit.end.spine_index

    def test_snippets_are_one_line(self, story):
        [hit] = story.search("culprit").hits
        assert hit.snippet == f"It was a quiet morning in the town. Later, {SENTINEL} was named the culprit."
        [hit] = story.search("culprit", chars_before=0, chars_after=0).hits
        assert hit.snippet == "culprit"
        [hit] = story.search("culprit", chars_before=7, chars_after=1).hits
        assert hit.snippet == "ed the culprit."
        assert "\n" not in story.search("town", chars_after=40).hits[0].snippet

    def test_a_long_match_is_shortened_in_its_snippet(self, tmp_path):
        """Characters that fold to nothing can make a match any length:
        its snippet keeps 2,000 characters from each end, joined by an
        ellipsis."""
        shy = "\u00ad"
        content = BookContent(bundle(tmp_path, [
            ("c1", f"<p>pre x{shy * 3998}y post</p>"),
            ("c2", f"<p>pre x{shy * 3999}y post</p>"),
            ("c3", f"<p>pre x{shy * 200_000}y post</p>"),
        ], [("One", "c1.xhtml"), ("Two", "c2.xhtml"), ("Three", "c3.xhtml")]))
        whole, cut, huge = content.search("xy", chars_before=4, chars_after=5).hits
        assert whole.snippet == f"pre x{shy * 3998}y post"
        assert cut.snippet == f"pre x{shy * 1999} … {shy * 1999}y post"
        assert huge.snippet == cut.snippet
        assert huge.end.offset - huge.start.offset == 200_002
        assert [h.snippet for h in content.search("xy", chars_before=0, chars_after=0).hits] == [
            f"x{shy * 3998}y", f"x{shy * 1999} … {shy * 1999}y", f"x{shy * 1999} … {shy * 1999}y"]

    def test_snippets_stay_in_their_item(self, story):
        [hit] = story.search("the end", chars_before=MAX_SNIPPET_CONTEXT, chars_after=MAX_SNIPPET_CONTEXT).hits
        assert hit.snippet == "The end."

    def test_query_conversion(self, tmp_path):
        content = BookContent(bundle(tmp_path, [("c1", "<p>Room 42, floor 3.5, code b'x'.</p>")]))
        assert texts(content, content.search(42).hits) == ["42"]
        assert texts(content, content.search(3.5).hits) == ["3.5"]
        assert texts(content, content.search(b"ROOM").hits) == ["Room"]

    def test_not_an_epub(self, tmp_path):
        pdf = tmp_path / "Paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        with pytest.raises(NotEpubError):
            BookContent(pdf).search("x")


# ---------------------------------------------------------------------------
# Paging and limits
# ---------------------------------------------------------------------------


def page_through(content, query, limit, **kwargs):
    hits, start, pages = [], None, 0
    while True:
        pages += 1
        assert pages < 10_000
        result = content.search(query, start=start, limit=limit, **kwargs)
        assert len(result.hits) <= limit
        hits.extend(result.hits)
        if result.next_start is None:
            return hits, pages
        assert result.truncated and result.next_start == content.search(
            query, start=start, limit=limit + 1, **kwargs).hits[limit].start
        start = result.next_start


class TestPaging:
    @pytest.mark.parametrize("limit", [1, 2, 3, 7])
    def test_pages_cover_every_hit_once(self, story, limit):
        everything = story.search("the", limit=None).hits
        hits, pages = page_through(story, "the", limit)
        assert hits == list(everything)
        assert pages == -(-len(everything) // limit)

    def test_start_mid_item(self, story):
        [first, second, third, *_] = story.search("cafe").hits
        assert story.search("cafe", start=second.start).hits[0] == second
        assert story.search("cafe", start=TextPosition(0, second.start.offset + 1)).hits[0] == third
        # A match that starts before start is not on the page, even if it
        # ends after it.
        assert story.search("cafe", start=TextPosition(0, first.start.offset + 1)).hits[0] == second

    def test_start_past_the_end(self, story):
        assert story.search("the", start=TextPosition(99, 0)) == TextSearchResult(())

    def test_default_limit(self, story):
        assert len(story.search("the").hits) == 6
        many = BookContent(bundle(story.path.parent / "m", [("c1", "<p>" + "ab " * 50 + "</p>")]))
        result = many.search("ab")
        assert len(result.hits) == 20 and result.next_start == TextPosition(0, 60)

    def test_none_and_large_limits_are_capped(self, tmp_path):
        content = BookContent(bundle(tmp_path, [("c1", "<p>" + "x " * (MAX_SEARCH_HITS + 5) + "</p>")]))
        for limit in (None, MAX_SEARCH_HITS + 1, 10 ** 30):
            result = content.search("x", limit=limit, count_total=True)
            assert len(result.hits) == MAX_SEARCH_HITS and result.total == MAX_SEARCH_HITS + 5
            assert result.next_start == TextPosition(0, 2 * MAX_SEARCH_HITS)

    @pytest.mark.parametrize("limit", [0, -1, True, False, 1.5, "3", [3]])
    def test_bad_limits(self, story, limit):
        with pytest.raises(InvalidArgumentError, match="limit"):
            story.search("the", limit=limit)

    def test_integral_limits(self, story):
        assert len(story.search("the", limit=2.0).hits) == 2

    @pytest.mark.parametrize("name", ["chars_before", "chars_after"])
    @pytest.mark.parametrize("value", [-1, MAX_SNIPPET_CONTEXT + 1, True, 1.0, "5", None])
    def test_bad_context_sizes(self, story, name, value):
        with pytest.raises(InvalidArgumentError, match=name):
            story.search("the", **{name: value})

    @pytest.mark.parametrize("value", [0, MAX_SNIPPET_CONTEXT])
    def test_context_size_bounds(self, story, value):
        assert story.search("the", chars_before=value, chars_after=value).hits

    @pytest.mark.parametrize("start", [(0, 1), "0:1", 0, ResolvedBoundary])
    def test_bad_start(self, story, start):
        with pytest.raises(InvalidArgumentError, match="start"):
            story.search("the", start=start)

    def test_count_total(self, story):
        result = story.search("the", limit=2, count_total=True)
        assert len(result.hits) == 2 and result.total == 6
        later = story.search("the", start=result.next_start, limit=2, count_total=True)
        assert later.total == 4


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class TestQueries:
    @pytest.mark.parametrize("query", ["", "   ", "\n\t", None, "­", "​́", b""])
    def test_a_query_folding_to_nothing_finds_nothing(self, story, query):
        assert story.search(query) == TextSearchResult(())
        assert story.search(query, count_total=True, count_withheld=True) == TextSearchResult((), None, 0, 0, 0)

    def test_an_empty_query_reads_nothing(self, tmp_path):
        assert BookContent(tmp_path / "Missing.epub").search("  ") == TextSearchResult(())

    def test_query_length_is_measured_after_folding(self, story):
        assert story.search("x" * 1000).hits == ()
        assert story.search(" " + "x" * 1000 + " ").hits == ()
        assert story.search("­" * 5000) == TextSearchResult(())
        for query in ("x" * 1001, "ß" * 501, "ﬃ" * 334):
            with pytest.raises(InvalidArgumentError) as exc:
                story.search(query)
            assert "1000" in str(exc.value) and query[:5] not in str(exc.value)

    def test_raw_length_is_bounded_before_folding(self, story, monkeypatch):
        """Folding costs time and memory with the input's length: a query
        over 64,000 characters is refused before it is folded, even one
        that would fold short."""
        from py_apple_books import _content_reading

        assert story.search("\u00ad" * 63996 + "town").hits != ()
        folded = []
        real = _content_reading._fold_query
        monkeypatch.setattr(_content_reading, "_fold_query", lambda q: folded.append(len(q)) or real(q))
        for query in ("x" * 64001, " " * 64000 + "x", "\u00ad" * 64001, "\ufb01" * 2_000_000, b"x" * 64001):
            with pytest.raises(InvalidArgumentError) as exc:
                story.search(query)
            assert "64000" in str(exc.value) and "xxxxx" not in str(exc.value)
        assert folded == []

    def test_arguments_are_checked_before_reading(self, tmp_path):
        missing = BookContent(tmp_path / "Missing.epub")
        for kwargs in ({"limit": 0}, {"chars_before": -1}, {"chars_after": 10 ** 6}, {"start": "0:0"},
                       {"until": "0:0"}):
            with pytest.raises(InvalidArgumentError):
                missing.search("x", **kwargs)
        with pytest.raises(InvalidArgumentError):
            missing.search("x" * 2000)
        with pytest.raises(NotEpubError):
            missing.search("x")


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_sentinel_after_a_mid_item_boundary(self, story):
        """The sentinel is past the boundary in its item (c2) and in two
        later chapters: no hit, no snippet shows it, and the withheld
        counts are 1 and 2."""
        c2 = story.get_spine_item_text(1)
        until = TextPosition(1, c2.index(SENTINEL) - 3)
        result = story.search(SENTINEL, until=until, count_total=True, count_withheld=True)
        assert result == TextSearchResult((), None, 0, 1, 2)
        for query in ("the", "town", "later", "a"):
            for hit in story.search(query, until=until, limit=None,
                                    chars_after=MAX_SNIPPET_CONTEXT).hits:
                assert SENTINEL.lower() not in hit.snippet.lower()
                assert hit.end <= until

    def test_snippets_stop_at_the_boundary(self, story):
        c2 = story.get_spine_item_text(1)
        until = TextPosition(1, c2.index("Later"))
        [hit] = story.search("town", until=until, chars_after=MAX_SNIPPET_CONTEXT).hits
        assert hit.snippet == "It was a quiet morning in the town."
        prefix = " ".join(c2[:until.offset].split())
        assert hit.snippet in prefix

    def test_no_match_runs_across_the_boundary(self, story):
        c2 = story.get_spine_item_text(1)
        cut = c2.index(SENTINEL) + 3
        result = story.search(SENTINEL, until=TextPosition(1, cut), count_withheld=True)
        assert result.hits == () and (result.withheld_in_item, result.withheld_later) == (1, 2)
        result = story.search(SENTINEL, until=TextPosition(1, cut + len(SENTINEL)), count_withheld=True)
        assert len(result.hits) == 1 and (result.withheld_in_item, result.withheld_later) == (0, 2)

    def test_boundary_at_an_item_start(self, story):
        result = story.search(SENTINEL, until=TextPosition(2, 0), count_withheld=True, count_total=True)
        assert len(result.hits) == 1 and result.total == 1
        assert (result.withheld_in_item, result.withheld_later) == (1, 1)

    def test_paging_never_passes_the_boundary(self, story):
        until = TextPosition(3, 0)
        hits, _ = page_through(story, "the", 1, until=until)
        assert hits == list(story.search("the", until=until, limit=None).hits)
        assert all(h.end <= until for h in hits)

    def test_withheld_without_a_boundary(self, story):
        result = story.search(SENTINEL, count_withheld=True)
        assert len(result.hits) == 3 and (result.withheld_in_item, result.withheld_later) == (0, 0)

    def test_withheld_counts_follow_the_scope(self, tmp_path):
        content = BookContent(bundle(tmp_path, [
            ("c1", "<p>start</p>"),
            ("notes", f"<p>{SENTINEL} note</p>", {"linear": False}),
            ("c2", f"<p>{SENTINEL}</p>"),
        ]))
        until = TextPosition(1, 0)
        assert content.search(SENTINEL, until=until, count_withheld=True) == TextSearchResult((), None, None, 0, 1)
        assert content.search(SENTINEL, until=until, count_withheld=True, include_nonlinear=True
                              ) == TextSearchResult((), None, None, 1, 1)

    def test_a_resolved_boundary(self, tmp_path, story):
        rb = ReadBoundary(3, "position", BoundarySource.PROGRESS, None, None, 50.0, None, None, ())
        resolved = ResolvedBoundary(3, rb, TextPosition(2, 0), BoundaryPrecision.SPINE_ITEM,
                                    BoundarySource.PROGRESS, ())
        assert story.search(SENTINEL, until=resolved) == story.search(SENTINEL, until=TextPosition(2, 0))
        mine = BookContent(story.path, book_id=3)
        assert len(mine.search(SENTINEL, until=resolved).hits) == 1
        other = BookContent(story.path, book_id=4)
        with pytest.raises(InvalidArgumentError, match="another book"):
            other.search(SENTINEL, until=resolved)
        with pytest.raises(InvalidArgumentError, match="resolve_boundary"):
            mine.search(SENTINEL, until=rb)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestScope:
    @pytest.fixture
    def shaped(self, tmp_path):
        """A linear navigation document in the spine, a non-linear note,
        an image and a guide ToC page; each mentions the sentinel."""
        return BookContent(write_epub_bundle(tmp_path / "Shaped.epub", [
            ("nav", None, {"properties": "nav"}),
            ("contents", f"<p>Contents: {SENTINEL}</p>"),
            ("c1", f"<p>Chapter one: {SENTINEL}.</p>"),
            ("notes", f"<p>Note: {SENTINEL}.</p>", {"linear": False}),
            ("pic", b"\x89PNG", {"raw": True, "href": "p.png", "media_type": "image/png"}),
            ("c2", "<p>Chapter two.</p>"),
        ], [(SENTINEL, "c1.xhtml"), ("Two", "c2.xhtml")], guide=(("toc", "Contents", "contents.xhtml"),)))

    def test_linear_nav_document_is_never_hit_by_default(self, shaped):
        spine = shaped.list_spine_items()
        assert spine[0].linear and spine[0].is_toc_page and spine[1].is_toc_page
        assert [h.item_id for h in shaped.search(SENTINEL).hits] == ["c1"]

    def test_including_toc_pages_and_nonlinear_items(self, shaped):
        assert [h.item_id for h in shaped.search(SENTINEL, include_toc_pages=True).hits] == [
            "nav", "contents", "c1"]
        assert [h.item_id for h in shaped.search(SENTINEL, include_nonlinear=True).hits] == ["c1", "notes"]
        assert [h.item_id for h in shaped.search(SENTINEL, include_nonlinear=True,
                                                 include_toc_pages=True).hits] == [
            "nav", "contents", "c1", "notes"]

    def test_unreadable_items_are_skipped(self, shaped):
        assert [h.item_id for h in shaped.search("chapter", limit=None).hits] == ["c1", "c2"]
