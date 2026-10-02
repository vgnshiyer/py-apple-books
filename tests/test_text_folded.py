"""Tests for the folded search helpers: text.finditer_folded and
text.find_folded (fold_for_match applied character by character, with
the matches reported as offsets into the original text)."""

import os
import random
import threading
import time
import types
import unicodedata

import pytest

from py_apple_books import text as T
from py_apple_books.text import find_folded, finditer_folded, fold_for_match

NFD = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731


def spans(text, query):
    return list(finditer_folded(text, query))


def found(text, query):
    return [text[a:b] for a, b in finditer_folded(text, query)]


# --- examples --------------------------------------------------------------------

EXAMPLES = [
    ("Le café.", "CAFE", ["café"]),
    (NFD("Le café."), "cafe", [NFD("café")]),
    ("Le café.", NFD("CAFÉ"), ["café"]),
    ("I don’t know", "don't", ["don’t"]),
    ("Die Straße", "strasse", ["Straße"]),
    ("the oﬃce…", "office...", ["oﬃce…"]),
    ("Second — para", "second - para", ["Second — para"]),
    ("Donau\u00addampf", "Donaudampf", ["Donau\u00addampf"]),
    ("É or e\u0301", "e", ["É", "e\u0301"]),
    ("non\u00a0breaking", "non breaking", ["non\u00a0breaking"]),
    ("line\nbreak and\r\n\ttabs", "line break and tabs", ["line\nbreak and\r\n\ttabs"]),
    ("zero\u200bwidth", "zerowidth", ["zero\u200bwidth"]),
    ("“quoted”", '"quoted"', ["“quoted”"]),
    ("small﹣form", "small-form", ["small﹣form"]),
    ("𝐁𝐨𝐥𝐝 type", "bold", ["𝐁𝐨𝐥𝐝"]),
    ("x🄐y", "(a)", ["🄐"]),
    ("İstanbul", "istanbul", ["İstanbul"]),
    ("ΣΟΦΟΣ σοφος", "σοφοσ", ["ΣΟΦΟΣ", "σοφος"]),
    ("ﬁnd ﬁnd", "FIND", ["ﬁnd", "ﬁnd"]),
]


@pytest.mark.parametrize("text, query, want", EXAMPLES)
def test_examples(text, query, want):
    assert found(text, query) == want


def test_spans_index_the_original_text():
    text = "a  Straße\u00ad und\n\nCAFE\u0301 ok"
    assert spans(text, "strasse und cafe") == [(3, 21)]
    assert text[3:21] == "Straße\u00ad und\n\nCAFE\u0301"


def test_partial_character_matches_widen_to_the_whole_character():
    # s and the ss of ß: one character, reported once.
    assert spans("ßs", "s") == [(0, 1), (1, 2)]
    assert spans("Maße", "as") == [(1, 3)]
    assert spans("…", ".") == [(0, 1)]
    assert spans("aﬃb", "fib") == [(1, 3)]


def test_match_extends_over_following_accents_only():
    text = "cafe\u0301\u0327 au"
    assert spans(text, "cafe") == [(0, 6)]
    # A soft hyphen after the match is not part of it.
    assert spans("Donau\u00ad", "donau") == [(0, 5)]
    # Marks that fold to themselves are text of their own.
    assert spans("か\u3099", "か") == [(0, 1)]


def test_matches_are_non_overlapping_and_in_order():
    assert spans("aaaa", "aa") == [(0, 2), (2, 4)]
    assert spans("yes, yes and yes", "YES") == [(0, 3), (5, 8), (13, 16)]


def test_decomposed_hangul_is_matched_as_decomposed():
    # Documented exception: the fold is per character, so NFC does not
    # join jamo; a decomposed query finds decomposed text.
    syllable, jamo = "한", NFD("한")
    assert spans(jamo, jamo) == [(0, 3)]
    assert spans(syllable, syllable) == [(0, 1)]
    assert spans(jamo, syllable) == []


def test_lone_surrogates_match_like_fold_for_match():
    text = "a\ud800b"
    assert fold_for_match(text) == "a���b"
    assert spans(text, "�") == [(1, 2)]
    assert spans(text, "a\ud800b") == [(0, 3)]


@pytest.mark.parametrize("query", ["", "   ", "\n\t", "\u00ad\u00ad", "\u200b \ufeff", "\u0301", None])
def test_query_that_folds_to_nothing_finds_nothing(query):
    assert spans("anything at all \u00ad\u0301", query) == []
    assert find_folded("anything", query) is None


def test_query_conversion_and_edge_whitespace():
    assert spans("say none", None) == []
    assert spans("say None", "None") == [(4, 8)]
    assert spans("Gödel", "godel".encode()) == [(0, 5)]
    assert spans("Gödel's", "  GÖDEL\n") == [(0, 5)]
    assert spans("1.5 and 15", 1.5) == [(0, 3)]


def test_text_must_be_str():
    for bad in (None, b"bytes", 12):
        with pytest.raises(TypeError):
            finditer_folded(bad, "x")
        with pytest.raises(TypeError):
            find_folded(bad, "x")


def test_iteration_is_lazy():
    it = finditer_folded("abc abc", "b")
    assert isinstance(it, types.GeneratorType)
    assert next(it) == (1, 2)
    assert next(it) == (5, 6)
    assert next(it, None) is None


def test_empty_text():
    assert spans("", "x") == []
    assert find_folded("", "x") is None


# --- find_folded --------------------------------------------------------------------


def test_find_folded_first_match_and_bounds():
    text = "Café, café, CAFÉ"
    assert find_folded(text, "cafe") == (0, 4)
    assert find_folded(text, "cafe", 1) == (6, 10)
    assert find_folded(text, "cafe", 1, 9) is None
    assert find_folded(text, "cafe", 11) == (12, 16)
    assert find_folded(text, "cafe", -4) == (12, 16)
    assert find_folded(text, "cafe", 0, -12) == (0, 4)
    assert find_folded(text, "cafe", 100) is None
    assert find_folded(text, "cafe", 5, 2) is None
    assert find_folded(text, "tea") is None


def test_find_folded_stays_inside_the_slice():
    text = "cafe\u0301 cafe\u0301"
    # The accent after the end bound is outside the slice.
    assert find_folded(text, "cafe", 0, 4) == (0, 4)
    assert find_folded(text, "cafe", 0, 5) == (0, 5)
    # A match straddling a bound is not in the slice.
    assert find_folded("one two", "one two", 1) is None
    assert find_folded("Straße", "strasse", 0, 5) is None


def test_find_folded_equals_first_of_finditer():
    rnd = random.Random(3)
    pool = "ab ßé’'-—\u00ad\u0301ﬁ…"
    for _ in range(300):
        text = "".join(rnd.choice(pool) for _ in range(rnd.randint(0, 25)))
        query = "".join(rnd.choice(pool) for _ in range(rnd.randint(1, 4)))
        first = next(iter(finditer_folded(text, query)), None)
        assert find_folded(text, query) == first


# --- the per-character table is fold_for_match ----------------------------------------


def test_table_equals_fold_for_match_on_every_bmp_code_point():
    # Pins finditer_folded's table (derived lazily, with a shortcut for
    # characters that cannot fold) to fold_for_match itself.
    bad = [hex(cp) for cp in range(0x10000) if T._char_fold(chr(cp)) != fold_for_match(chr(cp))]
    assert bad == []


@pytest.mark.parametrize("ch", ["\U0001D400", "\U0001F110", "\U0002F800", "\U00010400",
                                "\U0001F600", "\U00020000", "\U0001E900", "\U0010FFFF"])
def test_astral_characters_fold_with_fold_for_match(ch):
    assert T._char_fold(ch) == fold_for_match(ch)
    folded = T._char_fold(ch)
    if folded.strip():
        assert spans("<" + ch + ">", folded) == [(1, 2)]


def test_whitespace_folds_to_one_space():
    for cp in range(0x10000):
        if chr(cp).isspace():
            assert T._char_fold(chr(cp)) == " ", hex(cp)


# Character mix of the fuzz below: quotes, dashes, NBSP, soft hyphen,
# ZWSP, ß/ẞ, ligatures, ellipsis, combining accents, İ/ı and Σ/σ/ς.
POOL = list("abcdeEfsSit '’\"“-—– \u00a0\u00ad\u200béÉèßẞﬁﬀ…\u0301\u0308ñÑøØæŒœ\n\t.,İıΣσς")
ZERO_MARKS = {"\u0301", "\u0308"}
# Strings per fuzz test; APPLE_BOOKS_FUZZ_ITERATIONS raises it (e.g. 20000).
ITERATIONS = int(os.environ.get("APPLE_BOOKS_FUZZ_ITERATIONS", "2000"))


def test_per_character_fold_equals_fold_for_match_on_latin_text():
    rnd = random.Random(11)
    for _ in range(ITERATIONS):
        s = "".join(rnd.choice(POOL) for _ in range(rnd.randint(0, 30)))
        assert T._per_char_fold(s) == fold_for_match(s), ascii(s)


def test_fuzz_soundness_and_recall():
    rnd = random.Random(7)
    cases = 0
    start = time.perf_counter()
    for _ in range(ITERATIONS):
        hay = "".join(rnd.choice(POOL) for _ in range(rnd.randint(5, 40)))
        a = rnd.randint(0, len(hay) - 1)
        b = rnd.randint(a + 1, len(hay))
        needle = hay[a:b]
        q = T._fold_query(needle)
        hits = spans(hay, needle)
        if not q:
            assert hits == []
            continue
        cases += 1
        # Recall: a substring of the text is always found.
        assert hits, (ascii(hay), ascii(needle))
        last = 0
        for s, e in hits:
            # Sound: the fold of the span contains the folded query.
            assert q in T._per_char_fold(hay[s:e]), (ascii(hay), ascii(needle), s, e)
            # In order, non-overlapping, whole characters.
            assert last <= s < e <= len(hay)
            assert hay[s] not in ZERO_MARKS
            assert e == len(hay) or hay[e] not in ZERO_MARKS
            last = e
    assert cases > ITERATIONS * 3 // 4
    assert time.perf_counter() - start < 2 * max(1, ITERATIONS / 2000)


# --- shared state and cost ---------------------------------------------------------


def test_table_is_built_once_under_concurrent_first_use(monkeypatch):
    monkeypatch.setattr(T, "_FOLD_TABLES", None)
    builds = []
    real = T._build_fold_tables

    def counting():
        builds.append(1)
        time.sleep(0.05)  # widen the race window
        return real()

    monkeypatch.setattr(T, "_build_fold_tables", counting)
    barrier = threading.Barrier(8)
    results, errors = [], []

    def worker():
        try:
            barrier.wait()
            results.append(spans("Le café, la Straße", "strasse"))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(builds) == 1
    assert results == [[(12, 18)]] * 8


def test_dense_text_folds_quickly():
    base = ("Alpha beta gamma. The first   paragraph has café and don’t.\n\n"
            "Second — para here, Straße, oﬃce…\n\n")
    big = base * 12000  # ~1.1M characters, many irregular folds
    T._fold_tables()
    start = time.perf_counter()
    hits = spans(big, "DON'T")
    elapsed = time.perf_counter() - start
    assert len(hits) == 12000
    assert big[hits[-1][0]:hits[-1][1]] == "don’t"
    assert elapsed < 3
