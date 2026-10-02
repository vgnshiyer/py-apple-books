"""Tests for the short-selection classifier: text.selection_core and
text.is_short_selection (a highlighted word or short phrase, as opposed
to a passage)."""

import os
import random
import sqlite3
import time
import unicodedata

import pytest

from py_apple_books import text as T
from py_apple_books.text import is_short_selection, selection_core

ITERATIONS = int(os.environ.get("APPLE_BOOKS_FUZZ_ITERATIONS", "300"))
NFD = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731

SHORT = [
    "ephemeral",
    "Ephemeral,",
    "“ubiquitous.”",
    "in medias res",
    "well-known",
    "don’t",
    "naïve",
    NFD("naïve"),
    "\u00adword\u200b",
    "\u200bword\u00ad",
    "鬱",                    # one CJK ideograph
    "สวัสดี",  # a Thai word
    "一期一会",  # a four-character idiom
    b"word",
    bytearray(b"two words"),
    "\ud800word",               # lone surrogate: replaced, then trimmed
    "x" * 30,
    " " * 60 + "word",          # 64 code points: inside the raw gate
    "(parenthetical)",
    "a cat",
]

NOT_SHORT = [
    "これはとても長い日本語の文章です",
    "1984",
    "a",
    "e.g.",
    "one two three four",
    "x" * 31,
    " " * 70 + "word",          # raw gate: over 64 before trimming
    " " * 61 + "word",
    "This is a sentence. Another.",
    "Wait: what?",
    "semi;colon",
    "wait…what",
    "本文。完",  # CJK full stop inside
    "一二三四五六七八九",  # 9 CJK characters
    None,
    "",
    "   ",
    "\u00ad\u200b",
    "!!!",
    "42 7",
    "\U0001F600",
    12,
    3.5,
]


@pytest.mark.parametrize("text", SHORT, ids=repr)
def test_short(text):
    assert is_short_selection(text) is True


@pytest.mark.parametrize("text", NOT_SHORT, ids=repr)
def test_not_short(text):
    assert is_short_selection(text) is False


def test_unicode_version_dependent_case():
    # U+31350 (CJK Extension H) is unassigned in Unicode 13.0-14.0
    # (Python 3.10, 3.11) and a letter from Unicode 15.0 (Python 3.12+).
    version = tuple(int(x) for x in unicodedata.unidata_version.split("."))
    assert is_short_selection(chr(0x31350)) is (version >= (15, 0, 0))


def test_thresholds_are_the_documented_values():
    assert (T._SHORT_MAX_RAW, T._SHORT_MAX_CHARS, T._SHORT_MAX_WORDS, T._SHORT_MAX_NO_SPACE) \
        == (64, 30, 3, 8)
    doc = is_short_selection.__doc__
    assert "(64, 30, 3 words, 8)" in doc


def test_no_space_scripts_use_the_no_space_limit():
    assert is_short_selection("一" * 8) is True
    assert is_short_selection("一" * 9) is False
    assert is_short_selection("ก" * 8) is True        # Thai
    assert is_short_selection("あ" * 9) is False       # Hiragana
    assert is_short_selection("\U00020000" * 8) is True   # Extension B


def test_letters_and_words():
    assert is_short_selection("ab cd ef") is True
    assert is_short_selection("ab cd ef gh") is False
    assert is_short_selection("x 1") is False      # one letter
    assert is_short_selection("  spaced\n\n out  ") is True


# --- selection_core ------------------------------------------------------------------


@pytest.mark.parametrize("raw, core", [
    ("“Ephemeral,”", "Ephemeral"),
    ("  in\n medias\t\tres. ", "in medias res"),
    ("\u00ad\u200bword\u2060\ufeff", "word"),
    (NFD("naïve"), "naïve"),
    ("(¿qué?)", "qué"),
    ("$100", "100"),
    ("e.g.", "e.g"),
    ("mid\u00addle", "mid\u00addle"),     # inner characters are kept
    (b"word", "word"),
    ("\ud800word", "word"),
    (None, ""),
    ("", ""),
    ("...", ""),
    (42, "42"),
])
def test_selection_core(raw, core):
    assert selection_core(raw) == core


def test_selection_core_failed_str_is_empty():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no text")

    assert selection_core(Unprintable()) == ""
    assert is_short_selection(Unprintable()) is False


def test_conversion_is_shared_with_fold_for_match():
    for value in (b"Caf\xc3\xa9", bytearray(b"x y"), "\ud800a", 7, None):
        coerced = T._coerce_text(value)
        assert selection_core(value) == selection_core(coerced or "")


# --- never raises ---------------------------------------------------------------------


class _Weird:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_fuzz_never_raises():
    rnd = random.Random(23)
    pool = ["a", "Z", " ", "\n", ".", "’", "\u00ad", "\u200b", "\u0301", "一", "ก",
            "\ud800", "\udfff", "\U0001F600", "�", "1", "-", "。", "\u00a0"]
    for _ in range(ITERATIONS):
        s = "".join(rnd.choice(pool) for _ in range(rnd.randint(0, 80)))
        for value in (s, s.encode("utf-8", "surrogatepass"), _Weird(s),
                      _Weird(ValueError("x")), rnd.randint(-10**6, 10**6)):
            assert isinstance(is_short_selection(value), bool)
            assert isinstance(selection_core(value), str)


def test_huge_int_and_odd_objects():
    assert is_short_selection(10 ** 5000) is False
    assert selection_core(10 ** 5000) == ""
    assert is_short_selection(object()) is False   # '<object object at ...>'
    assert is_short_selection(["word"]) is True    # str(): "['word']"


# --- cost and the SQL prefilter ------------------------------------------------------


def test_long_texts_are_rejected_before_any_normalisation():
    texts = ["x" * 600 + " y" for _ in range(50000)]
    start = time.perf_counter()
    assert not any(is_short_selection(t) for t in texts)
    assert time.perf_counter() - start < 0.1


def test_sql_length_prefilter_keeps_every_short_selection():
    # A caller may prefilter rows with SQL length(col) <= 64. SQLite's
    # length() never exceeds Python's len() of the decoded cell (it stops
    # at NUL and skips stray continuation bytes, which decode to U+FFFD),
    # so the prefilter drops no row the classifier accepts.
    rnd = random.Random(31)
    # Edge padding (spaces and bytes that decode to U+FFFD, trimmed by
    # selection_core), a word, then maybe a NUL or a stray byte.
    padding = [b" ", b"\x80", b"\xff", b"\xe2\x80", b"\x80\x80"]
    rows = []
    for _ in range(2000):
        raw = b""
        target = rnd.randint(50, 70)
        while len(raw) < target:
            raw += rnd.choice(padding)
        raw += rnd.choice([b"word", b"two words", b"\xc3\xa9t\xc3\xa9"])
        raw += rnd.choice([b"", b"\x00", b"\x00\x00 tail", b"\xff", b"\x80", b" "])
        rows.append(raw)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, col TEXT)")
    for i, raw in enumerate(rows):
        db.execute("INSERT INTO t VALUES (?, CAST(? AS TEXT))", (i, raw))
    accepted = 0
    for blob, kept in db.execute("SELECT CAST(col AS BLOB), length(col) <= 64 FROM t"):
        decoded = bytes(blob).decode("utf-8", "replace")
        if is_short_selection(decoded):
            accepted += 1
            assert kept == 1, blob
    assert accepted > 300
