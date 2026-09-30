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
