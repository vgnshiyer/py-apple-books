"""Tests for search-matching text folding (py_apple_books.text)."""

import sqlite3
import unicodedata

import pytest

from py_apple_books.text import fold_for_match

NFD_GODEL = unicodedata.normalize("NFD", "Gödel")

# Pairs that must fold to the same string: what users type vs. what
# Apple Books stores.
EQUAL = [
    ("don't", "don’t"),
    ("a\nb", "a b"),
    ("a b", "a b"),  # NBSP
    ("a–b", "a-b"),
    ("wait…", "wait..."),
    ("Straße", "strasse"),
    ("Gödel", "GODEL"),
    (NFD_GODEL, "godel"),
    ("Gödel", NFD_GODEL),
    ("ﬁnd", "find"),
    ("“quoted”", '"quoted"'),
    ("em—dash", "em-dash"),
    ("minus −1", "minus -1"),
    ("soft­hyphen", "softhyphen"),
    ("zero​width", "zerowidth"),
    ("﻿bom", "bom"),
    ("naïve café", "NAIVE CAFE"),
    ("line\r\n\tbreaks", "line breaks"),
    ("37℃", "37°c"),
    ("𝐁𝐨𝐥𝐝", "bold"),
]


@pytest.mark.parametrize("a, b", EQUAL)
def test_fold_equal(a, b):
    assert fold_for_match(a) == fold_for_match(b)


def test_dotted_capital_i_contains_i():
    assert "i" in fold_for_match("İ")
    assert "istanbul" in fold_for_match("İstanbul")


def test_non_latin_marks_are_kept():
    # The kana voicing mark is outside U+0300-U+036F: が stays が.
    assert fold_for_match("が") != fold_for_match("か")
    assert fold_for_match("が") == "が"
    # NFC recomposes Hangul after NFKD split it into jamo.
    assert fold_for_match("한") == "한"
    assert fold_for_match("한") not in fold_for_match("하")
    assert fold_for_match("하") not in fold_for_match("한")


def test_whitespace_is_collapsed_not_stripped():
    assert fold_for_match("  a \n\n b  ") == " a b "
    assert fold_for_match(" é ") == " e "


def test_ascii_fast_path():
    assert fold_for_match("Hello,\tWORLD") == "hello, world"


def test_none_and_non_str():
    assert fold_for_match(None) is None
    assert fold_for_match(42) == "42"
    assert fold_for_match(1.5) == "1.5"
    assert fold_for_match("Gödel".encode()) == "godel"
    assert fold_for_match(bytearray(b"ABC")) == "abc"
    assert fold_for_match(b"bad \xff byte") == "bad � byte"


def test_unconvertible_input_folds_to_none():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no text")

    assert fold_for_match(Unprintable()) is None


@pytest.mark.parametrize("text", [a for pair in EQUAL for a in pair] + [
    "İ", "が", "한", "  x  ", "ϒ", "ℌ", "Ⅻ", "㎒", "½", "ǅ", "ﬀ", "ΐ",
])
def test_idempotent(text):
    once = fold_for_match(text)
    assert fold_for_match(once) == once


def test_idempotent_on_every_code_point():
    # Compatibility decompositions can yield capitals (℃ -> °C, ℍ -> H);
    # the second casefold keeps the fold a fixed point.
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        once = fold_for_match(chr(cp))
        assert fold_for_match(once) == once, hex(cp)


SURROGATES = [chr(0xD800), chr(0xDFFF), "a" + chr(0xD800) + "b", "Gödel" + chr(0xDC80)]


@pytest.mark.parametrize("text", SURROGATES, ids=["D800", "DFFF", "embedded", "non-ascii"])
def test_lone_surrogates_are_replaced(text):
    folded = fold_for_match(text)
    folded.encode("utf-8")
    assert "�" in folded
    assert fold_for_match(folded) == folded
    row = sqlite3.connect(":memory:").execute("select ?", (folded,)).fetchone()
    assert row == (folded,)


def test_surrogate_neighbours_still_fold():
    assert fold_for_match("A" + chr(0xD800) + "–B") == "a���-b"


def test_never_raises_on_all_code_points():
    everything = "".join(map(chr, range(0x110000)))
    folded = fold_for_match(everything)
    folded.encode("utf-8")
    assert fold_for_match(folded) == folded
