"""Tests for py_apple_books.text: search-matching folding, the shared
input conversion, normalize_unicode and passage location.

The folded search, snap_break and the short-selection classifier have
their own files (test_text_folded.py, test_snap_break.py,
test_short_selection.py).
"""

import pathlib
import re
import sqlite3
import time
import unicodedata

import pytest

from py_apple_books import text as text_module
from py_apple_books.text import _find_passage, _passage_pattern, fold_for_match, normalize_unicode

NFD_GODEL = unicodedata.normalize("NFD", "Gödel")

# Pairs that must fold to the same string: what users type vs. what
# Apple Books stores.
EQUAL = [
    ("don't", "don’t"),
    ("a\nb", "a b"),
    ("a\u00a0b", "a b"),  # NBSP
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
    ("soft\u00adhyphen", "softhyphen"),
    ("zero\u200bwidth", "zerowidth"),
    ("\ufeffbom", "bom"),
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
    assert fold_for_match("\u2003é\u2003") == " e "


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


# --- 1.11: the public text module ------------------------------------------

def test_public_names():
    assert text_module.__all__ == [
        "fold_for_match", "finditer_folded", "find_folded", "normalize_unicode",
        "snap_break", "selection_core", "is_short_selection",
    ]
    for name in text_module.__all__:
        assert callable(getattr(text_module, name))


def test_text_module_imports_nothing_from_the_package():
    source = pathlib.Path(text_module.__file__).read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, flags=re.M)
    assert imports and not [m for m in imports if m.startswith(("py_apple_books", "."))]


# The README's "Searching" section, example by example.
README_EXAMPLES = [
    ("ß", "ss"),
    ("Godel", "Gödel"),
    ("don't", "don’t"),
    ("-", "–"),
    ("-", "—"),
    ("find", "ﬁnd"),
    ("...", "…"),
    ("softhyphen", "soft\u00adhyphen"),
    ("zerowidth", "zero\u200bwidth"),
    ("a highlight", "a\n  highlight"),
]


@pytest.mark.parametrize("query, stored", README_EXAMPLES)
def test_readme_searching_examples(query, stored):
    assert fold_for_match(query) in fold_for_match(stored)


def test_readme_other_scripts_keep_their_marks():
    assert fold_for_match("が") != fold_for_match("か")


# Inputs of every kind: the shared conversion must give fold_for_match
# exactly what it did on its own in 1.10.
ODD_INPUTS = [None, "", "abc", "Gödel", b"G\xc3\xb6del", b"bad \xff", bytearray(b"x"), 0, -1.5,
              chr(0xD800), "a" + chr(0xDFFF), ["list"], ("tuple",), object]


@pytest.mark.parametrize("value", ODD_INPUTS, ids=repr)
def test_coerce_matches_fold_input_rules(value):
    coerced = text_module._coerce_text(value)
    if value is None:
        assert coerced is None
        return
    assert isinstance(coerced, str)
    coerced.encode("utf-8")
    assert fold_for_match(value) == fold_for_match(coerced)


def test_coerce_failed_str_is_none():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no text")

    assert text_module._coerce_text(Unprintable()) is None


# --- normalize_unicode ---------------------------------------------------------


@pytest.mark.parametrize("raw, want", [
    ("Donau\u00addampf\u200bschiff\ufeff", "Donaudampfschiff"),
    ("e\u00ad\u0301", "é"),  # deleted first, so the accent composes
    (unicodedata.normalize("NFD", "Tiếng Việt"), "Tiếng Việt"),
    ("\ufeffBOM first", "BOM first"),
    ("plain ascii  text\n", "plain ascii  text\n"),
    ("", ""),
])
def test_normalize_unicode_table(raw, want):
    assert normalize_unicode(raw) == want
    assert unicodedata.is_normalized("NFC", normalize_unicode(raw))


def test_normalize_unicode_keeps_joiners_controls_and_spaces():
    family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
    kept = family + " \u200c \u200f \u200e \u00a0 \u202f \u3000 \u2060"
    assert normalize_unicode(kept) == kept
    # NFC, not NFKC: compatibility characters are left alone.
    assert normalize_unicode("ﬁ ① Ａ") == "ﬁ ① Ａ"


def test_normalize_unicode_none_ascii_and_types():
    assert normalize_unicode(None) is None
    s = "already ascii"
    assert normalize_unicode(s) is s
    with pytest.raises(TypeError):
        normalize_unicode(b"bytes")


@pytest.mark.parametrize("raw", [
    "Donau\u00addampf", "e\u00ad\u0301\u00ad", unicodedata.normalize("NFD", "한국어 Việt"),
    "\u200b\u200b", "a\ufeff\u0308b", "ᄀ\u00adᅡ",
])
def test_normalize_unicode_idempotent(raw):
    once = normalize_unicode(raw)
    assert normalize_unicode(once) == once
    assert not set(once) & {"\u00ad", "\u200b", "\ufeff"}


# --- _find_passage ---------------------------------------------------------------


def legacy_first_match(text, anchor):
    """1.10's get_annotation_surrounding_text lookup, vendored."""
    pattern = r"\s+".join(re.escape(word) for word in anchor.split())
    m = re.search(pattern, text)
    return None if m is None else m.span()


def test_find_passage_whitespace_and_invisibles():
    t = "a Donau\u00addampf\nschiff b x\u200b y Donaudampf schiff"
    assert _find_passage(t, "Donaudampf schiff") == [(2, 20), (28, 45)]
    assert t[2:20] == "Donau\u00addampf\nschiff"
    assert _find_passage(t, "x y") == [(23, 27)]
    assert t[23:27] == "x\u200b y"


def test_find_passage_invisible_only_in_the_passage():
    assert _find_passage("Donaudampf", "Donau\u00addampf") == [(0, 10)]
    assert _find_passage("one two", "one\ufeff two") == [(0, 7)]


def test_find_passage_invisible_at_the_start_of_the_text():
    assert _find_passage("\u200bab", "ab") == [(1, 3)]
    assert _find_passage("\u00ad\u00adab\u00ad", "ab") == [(2, 4)]


def test_find_passage_line_breaks_and_spaces():
    assert _find_passage("one\n\ntwo", "one two") == [(0, 8)]
    assert _find_passage("one two", "one\n two") == [(0, 7)]
    assert _find_passage("onetwo", "one two") == []


def test_find_passage_is_literal_and_exact():
    assert _find_passage("a.b a+b", "a+b") == [(4, 7)]
    assert _find_passage("(x)[y]", "(x)[y]") == [(0, 6)]
    assert _find_passage("Café", "cafe") == []
    assert _find_passage("don’t", "don't") == []


def test_find_passage_non_overlapping_repeats():
    assert _find_passage("yes yes yes", "yes") == [(0, 3), (4, 7), (8, 11)]
    assert _find_passage("aaaa", "aa") == [(0, 2), (2, 4)]


@pytest.mark.parametrize("passage", ["", "   ", "\u200b \u00ad", "\ufeff"])
def test_find_passage_nothing_visible(passage):
    assert _find_passage("some text", passage) == []


@pytest.mark.parametrize("text, passage", [(None, "a"), ("a", None), (b"a", "a"), ("", "a")])
def test_find_passage_bad_input_is_empty(text, passage):
    assert _find_passage(text, passage) == []


def test_find_passage_offsets_slice_the_original():
    t = "x\u00ady  z\ufeff w\u200b\u200bq x\u00ady"
    for start, end in _find_passage(t, "xy"):
        assert normalize_unicode(t[start:end]) == "xy"
    assert _find_passage(t, "xy") == [(0, 3), (13, 16)]


@pytest.mark.parametrize("text, anchor", [
    ("He said yes.\nLater she said yes.", "said yes."),
    ("a  b c", "b c"),
    ("tab\there", "tab here"),
    ("nothing", "absent"),
])
def test_find_passage_first_span_is_1_10_match(text, anchor):
    spans = _find_passage(text, anchor)
    assert (spans[0] if spans else None) == legacy_first_match(text, anchor)


def test_passage_pattern_is_1_10_pattern():
    for passage in ("one  two\nthree", "a+b (c)", "x", "   ", "ünï ¢ødé"):
        tokens = passage.split()
        legacy = r"\s+".join(re.escape(word) for word in tokens) if tokens else None
        assert _passage_pattern(passage) == legacy


def test_find_passage_nbsp_runs_stay_fast():
    t = ("x" + "\u00a0" * 3000) * 200 + "\u00ad"
    start = time.perf_counter()
    assert _find_passage(t, "x y") == []
    assert time.perf_counter() - start < 0.5


def test_find_passage_long_passage_in_long_text_stays_fast():
    words = " ".join(f"w{i}" for i in range(2500))  # ~14k characters
    text = "filler " * 20000 + words.replace(" w5", " \u00adw5") + " tail"
    start = time.perf_counter()
    assert len(_find_passage(text, words)) == 1
    assert time.perf_counter() - start < 0.5


def fold_1_10(s):
    """fold_for_match as released in 1.10.0, vendored."""
    if s is None:
        return None
    if not isinstance(s, str):
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", "replace")
        else:
            try:
                s = str(s)
            except Exception:
                return None
    if s.isascii():
        return text_module._WHITESPACE.sub(" ", s).lower()
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        s = s.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    s = unicodedata.normalize("NFKD", s.casefold()).casefold().translate(text_module._MAP)
    return text_module._WHITESPACE.sub(" ", unicodedata.normalize("NFC", s))


def test_fold_for_match_output_is_unchanged_from_1_10():
    import random

    rnd = random.Random(19)
    pool = [chr(cp) for cp in (0x41, 0x61, 0x20, 0x0A, 0xA0, 0xAD, 0xDF, 0xE9, 0x130, 0x301, 0x2019,
                                0x2014, 0x2026, 0xFB01, 0xD800, 0xDC00, 0x3042, 0x1F600, 0x1D400)]
    values = list(ODD_INPUTS) + [b"\xff\xfe", 10 ** 5000]
    for _ in range(3000):
        values.append("".join(rnd.choice(pool) for _ in range(rnd.randint(0, 12))))
    for value in values:
        assert fold_for_match(value) == fold_1_10(value), ascii(value)
