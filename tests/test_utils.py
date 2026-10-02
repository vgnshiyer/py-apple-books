"""Tests for py_apple_books.utils: date conversion and snippet windows."""

import pytest

from py_apple_books.utils import APPLE_EPOCH_OFFSET, apple_timestamp_to_datetime, snap_window

TEXT = "alpha beta gamma delta epsilon"


def window(word: str, before: int, after: int, text: str = TEXT) -> str:
    return snap_window(text, text.index(word), len(word), before, after)


class TestAppleTimestamp:
    def test_none_is_none(self):
        assert apple_timestamp_to_datetime(None) is None

    @pytest.mark.parametrize("raw", [0, 0.0, "0"])
    def test_core_data_epoch_is_2001_utc(self, raw):
        """Regression for the Apple-epoch fix: 0 is 2001-01-01T00:00Z,
        whatever zone the result is expressed in."""
        assert apple_timestamp_to_datetime(raw).timestamp() == APPLE_EPOCH_OFFSET == 978307200

    def test_fractional_seconds_are_kept(self):
        assert apple_timestamp_to_datetime(1.5).timestamp() == pytest.approx(978307201.5)


class TestSnapWindow:
    def test_whole_text_when_window_covers_it(self):
        assert window("gamma", 100, 100) == TEXT

    def test_snaps_to_word_boundaries(self):
        # The raw window "ta gamma de" would cut two words; both cuts move
        # outward to the nearest space inside the window.
        assert window("gamma", 3, 3) == "…gamma…"

    def test_no_prefix_ellipsis_at_start(self):
        assert window("alpha", 10, 3) == "alpha…"

    def test_no_suffix_ellipsis_at_end(self):
        assert window("epsilon", 4, 10) == "…epsilon"

    def test_zero_context(self):
        assert window("gamma", 0, 0) == "…gamma…"

    def test_never_cuts_mid_word_when_a_space_exists(self):
        got = window("gamma", 8, 8)
        assert got == "…beta gamma delta…"
        for word in got.strip("…").split():
            assert word in TEXT.split()

    def test_text_without_spaces_keeps_the_raw_window(self):
        assert snap_window("abcdefghij", 4, 2, 2, 2) == "…cdefgh…"

    def test_collapses_interior_whitespace(self):
        text = "one\n\ntwo   three\tfour"
        assert snap_window(text, text.index("two"), 3, 50, 50) == "one two three four"

    def test_empty_text(self):
        assert snap_window("", 0, 0, 5, 5) == ""


# ---------------------------------------------------------------------------
# Extraction core: _parse, _walk, _anchor_index (1.11)
# ---------------------------------------------------------------------------
#
# extract_chapter_text's byte-for-byte equivalence with 1.10 is pinned in
# test_extract_equivalence.py; these tests pin the pieces' own contracts,
# which the 1.11 section and chapter spans build on.


def _soup(markup: bytes):
    from py_apple_books.utils import _parse

    return _parse(markup)


class TestParse:
    def test_drops_script_style_and_head_with_their_contents(self):
        soup = _soup(
            b"<html><head><title>T</title></head><body>"
            b"<script>s()</script><p>a</p><style>p{}</style></body></html>"
        )
        assert str(soup) == "<html><body><p>a</p></body></html>"

    def test_inserts_nothing(self):
        # 1.10 inserted "\n" strings around every block-level tag here.
        assert str(_soup(b"<div><p>a</p><p>b</p></div>")) == "<div><p>a</p><p>b</p></div>"


class TestWalk:
    def walk(self, markup: bytes, **kw):
        from py_apple_books.utils import _walk

        soup = _soup(markup)
        return _walk(soup, **kw)

    def test_block_tags_add_a_newline_on_entry_and_exit(self):
        assert self.walk(b"<p>a</p>b<div>c</div>") == ("\na\nb\nc\n", False)

    def test_inline_tags_add_nothing(self):
        assert self.walk(b"<span>a</span><em>b</em>") == ("ab", False)

    def test_br_and_hr_are_block_level(self):
        assert self.walk(b"a<br/>b<hr>c") == ("a\n\nb\n\nc", False)

    def test_top_itself_adds_no_newline(self):
        from py_apple_books.utils import _walk

        soup = _soup(b"<div><p>a</p></div>")
        assert _walk(soup.div) == ("\na\n", False)

    def test_only_text_and_cdata_count(self):
        markup = (
            b"<!DOCTYPE html><p>a<!-- c --><?pi x?><![CDATA[d]]>"
            b"<ruby>k<rp>(</rp><rt>r</rt><rp>)</rp></ruby>"
            b"<template>t</template>b</p>"
        )
        assert self.walk(markup) == ("\nadkb\n", False)

    def test_start_el_collects_from_it_on(self):
        from py_apple_books.utils import _walk

        soup = _soup(b"<p>skip</p><div><p id='s'>x</p>y</div>z")
        start = soup.find(id="s")
        # No newline for entering the start element; one for leaving it
        # and for leaving its block-level ancestors.
        assert _walk(soup, start) == ("x\ny\nz", False)

    def test_start_el_outside_top_collects_nothing(self):
        from py_apple_books.utils import _walk

        soup = _soup(b"<div id='a'>a</div><div id='b'>b</div>")
        assert _walk(soup.find(id="a"), soup.find(id="b")) == ("", False)

    def test_stop_ends_before_the_tag(self):
        from py_apple_books.utils import _walk

        soup = _soup(b"<p id='a'>one</p><p id='b'>two</p><p id='c'>three</p>")
        text, stopped = _walk(soup, soup.find(id="a"), lambda t: t.get("id") == "c")
        assert (text, stopped) == ("one\n\ntwo\n\n", True)

    def test_is_stop_sees_only_tags_reached_while_collecting(self):
        from py_apple_books.utils import _walk

        soup = _soup(b"<p id='x'>0</p><p id='s'><b id='in'>1</b></p><i id='after'>2</i>")
        seen = []

        def is_stop(tag):
            seen.append(tag.get("id"))
            return False

        assert _walk(soup, soup.find(id="s"), is_stop) == ("1\n2", False)
        assert seen == ["in", "after"]

    def test_stop_without_start(self):
        from py_apple_books.utils import _walk

        soup = _soup(b"<p>a</p><h2 id='n'>next</h2>")
        assert _walk(soup, None, lambda t: t.name == "h2") == ("\na\n\n", True)

    def test_does_not_mutate_the_tree(self):
        from py_apple_books.utils import _walk

        soup = _soup(b"<div><p id='a'>x</p><p>y</p></div>z")
        before = str(soup)
        _walk(soup)
        _walk(soup, soup.find(id="a"), lambda t: False)
        assert str(soup) == before

    def test_returns_a_plain_str(self):
        text, _ = self.walk(b"<p>a</p>")
        assert type(text) is str
        text, _ = self.walk(b"a")
        assert type(text) is str

    def test_deep_nesting_is_not_recursive(self):
        depth = 5000
        markup = b"<div>" * depth + b"x" + b"</div>" * depth
        text, stopped = self.walk(markup)
        assert text.strip() == "x" and not stopped


class TestAnchorIndex:
    MARKUP = (
        b"<body><h2 id='c1'>One</h2><a name='old'></a><p id='c1'>dup</p>"
        b"<p name='notanchor'>x</p><a id='both' name='n2'></a>"
        b"<span id='old'>id wins</span><a name='old'>second</a></body>"
    )

    def test_ids_map_to_the_element_find_returns(self):
        from py_apple_books.utils import _anchor_index

        soup = _soup(self.MARKUP)
        ids, _ = _anchor_index(soup)
        assert set(ids) == {"c1", "both", "old"}
        for value, el in ids.items():
            assert el is soup.find(id=value)

    def test_names_only_from_a_elements_first_wins(self):
        from py_apple_books.utils import _anchor_index

        soup = _soup(self.MARKUP)
        _, names = _anchor_index(soup)
        assert set(names) == {"old", "n2"}
        assert names["old"] is soup.find("a", attrs={"name": "old"})
        assert names["n2"].get("id") == "both"

    def test_ids_and_names_are_separate(self):
        from py_apple_books.utils import _anchor_index

        ids, names = _anchor_index(_soup(self.MARKUP))
        # A fragment resolves in ids first: here an id and an <a name>
        # share a value on different elements.
        assert ids["old"].name == "span" and names["old"].name == "a"

    def test_skipped_tags_are_gone(self):
        from py_apple_books.utils import _anchor_index

        ids, _ = _anchor_index(_soup(b"<head><title id='t'>x</title></head><p id='p'>y</p>"))
        assert set(ids) == {"p"}


class TestTextInWindow:
    def test_thin_wrapper_over_walk(self):
        from py_apple_books.utils import _text_in_window, extract_chapter_text

        markup = b"<body><h2 id='a'>A</h2><p>one</p><h2 id='b'>B</h2><p>two</p></body>"
        soup = _soup(markup)
        assert _text_in_window(soup, soup.find(id="a"), {"b"}) == "A\n\none"
        assert _text_in_window(soup, soup.find(id="a"), {"b"}) == extract_chapter_text(
            markup, "a", {"b"}
        )


def test_extract_chapter_text_inserts_no_nodes(monkeypatch):
    """The 1.11 core reads the tree without changing it."""
    from bs4.element import PageElement, Tag

    from py_apple_books.utils import extract_chapter_text

    def refuse(*args, **kwargs):
        raise AssertionError("the extraction core must not modify the tree")

    monkeypatch.setattr(PageElement, "insert_before", refuse)
    monkeypatch.setattr(PageElement, "insert_after", refuse)
    monkeypatch.setattr(Tag, "insert", refuse)
    monkeypatch.setattr(Tag, "append", refuse)
    markup = b"<body><h2 id='a'>A</h2><p>one</p><h2 id='b'>B</h2><p>two</p></body>"
    assert extract_chapter_text(markup) == "A\n\none\n\nB\n\ntwo"
    assert extract_chapter_text(markup, "a", {"b"}) == "A\n\none"
