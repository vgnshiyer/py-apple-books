"""Tests for text.snap_break and its grapheme-cluster approximation.

Grapheme vectors (tests/fixtures/grapheme_vectors.json) hold, for each
sample text below, the extended grapheme cluster boundaries computed by
the third-party ``regex`` module's ``\\X`` (UAX #29) at a pinned version.
Samples use code points assigned in Unicode 13.0 (Python 3.10's
database) unless tagged ``min_unidata``; tagged ones are skipped on older
Pythons. To regenerate after editing SAMPLES (``regex`` stays a
test-only tool, not a dependency):

    uv run --no-project -p 3.13 --with regex==2026.9.10 \\
        python tests/test_snap_break.py --regenerate
"""

import json
import pathlib
import random
import sys
import unicodedata

import pytest

from py_apple_books import text as T
from py_apple_books.text import snap_break

VECTORS = pathlib.Path(__file__).parent / "fixtures" / "grapheme_vectors.json"
REGEX_VERSION = "2026.9.10"

NFD = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731

# (note, text, min_unidata or None)
SAMPLES = [
    ("devanagari words and conjuncts", "नमस्ते दुनिया क्षत्रिय हिन्दी", None),
    ("devanagari conjunct run", "क्षत्रियक्षत्रिय", None),
    ("devanagari virama then independent vowel", "क्अ", None),
    ("devanagari ZWJ inside a conjunct", "क्\u200dष", None),
    ("bengali", "বাংলা ভাষা ক্ষ", None),
    ("gujarati", "ગુજરાતી ક્ષ", None),
    ("oriya", "ଓଡ଼ିଆ କ୍ଷ", None),
    ("telugu", "తెలుగు క్ష", None),
    ("malayalam", "മലയാളം ക്ഷ", None),
    ("tamil (virama does not link)", "தமிழ் மொழி க்ஷ", None),
    ("kannada (virama does not link)", "ಕನ್ನಡ ಕ್ಷ", None),
    ("sinhala with ZWJ after virama", "ශ්\u200dරී ලංකා", None),
    ("khmer coeng", "ខ្មែរ ក្ស", None),
    ("myanmar, a vowel sign that does not extend", "မြန်မာ ကာ က်္ခ", None),
    ("thai with SARA AM", "ภาษาไทยที่สวยงาม กำ", None),
    ("hebrew points", "בְּרֵאשִׁית בָּרָא אֱלֹהִים", None),
    ("arabic marks and ligature", "ﷺ عَرَبِيّ", None),
    ("arabic prepended number sign", "\u0600١٢ x", None),
    ("vietnamese decomposed", NFD("Tiếng Việt rất đẹp"), None),
    ("hangul decomposed", NFD("한국어텍스트"), None),
    ("hangul syllable plus jamo", "각각ᆨ가ᅡᄀ가", None),
    ("emoji ZWJ family, modifier, flags", "a👨\u200d👩\u200d👧\u200d👦b👍🏽c🇯🇵🇺🇸d", None),
    ("odd regional indicators", "🇯🇵🇺x", None),
    ("rainbow flag", "🏳\ufe0f\u200d🌈 flag", None),
    ("eye in speech bubble", "👁\ufe0f\u200d🗨\ufe0f!", None),
    ("tag sequence flag", "🏴\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F.", None),
    ("keycaps", "1\ufe0f\u20e3 #\ufe0f\u20e3", None),
    ("lone modifier and ZWJ after a letter", "a🏽 a\u200db \u200d👍", None),
    ("kana voicing marks, halfwidth too", "か\u3099 ｶﾞｷﾞ", None),
    ("CR LF and controls", "x\r\ny\rz\n\u0301", None),
    ("soft hyphen, ZWSP, BOM, word joiner", "soft\u00adhyphen zero\u200bwidth \ufeffbom a\u2060b", None),
    ("combining marks on Latin", "e\u0301\u0327x\u20dd\u0300", None),
    ("CJK and punctuation", "第一章。本文！", None),
    ("CJK extension H", "\U00031350\U00031351\u3099", "15.0.0"),
    ("kawi conjunct", "\U00011F12\U00011F42\U00011F12", "15.0.0"),
]


def unidata() -> tuple:
    return tuple(int(x) for x in unicodedata.unidata_version.split("."))


def load_vectors():
    if not VECTORS.is_file():  # only while regenerating; test_vectors_match_the_samples fails
        return []
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def applicable(vector) -> bool:
    need = vector.get("min_unidata")
    return need is None or unidata() >= tuple(int(x) for x in need.split("."))


def vector_params():
    out = []
    for v in load_vectors():
        marks = [] if applicable(v) else [pytest.mark.skip(reason=f"needs Unicode {v['min_unidata']}")]
        out.append(pytest.param(v, id=v["note"], marks=marks))
    return out


# --- the committed vectors -----------------------------------------------------------


def test_vectors_match_the_samples():
    vectors = load_vectors()
    assert [(v["note"], v["text"], v.get("min_unidata")) for v in vectors] == SAMPLES
    for v in vectors:
        assert v["boundaries"][0] == 0 and v["boundaries"][-1] == len(v["text"])
        assert v["regex"] == REGEX_VERSION


def test_untagged_vectors_use_assigned_code_points():
    # Run on Python 3.10 (Unicode 13.0) this checks the 13.0 restriction.
    for v in load_vectors():
        if v.get("min_unidata") is None:
            assert all(unicodedata.category(ch) != "Cn" for ch in v["text"]), v["note"]


@pytest.mark.parametrize("vector", vector_params())
def test_grapheme_boundaries_match_uax29(vector):
    text, want = vector["text"], set(vector["boundaries"])
    got = {i for i in range(len(text) + 1) if T._is_grapheme_boundary(text, i)}
    assert got == want


@pytest.mark.parametrize("vector", vector_params())
def test_snap_break_on_vectors(vector):
    text, bounds = vector["text"], set(vector["boundaries"])
    for pos in range(1, len(text)):
        for floor in (0, pos // 2, pos - 1):
            b = snap_break(text, pos, lookback=len(text), floor=floor)
            in_window = [x for x in bounds if floor < x <= pos]
            assert floor < b <= pos
            assert b in bounds or (b == pos and not in_window), (vector["note"], pos, floor, b)


def test_cross_check_with_regex_module():
    regex = pytest.importorskip("regex")
    rnd = random.Random(4)
    alphabet = [ch for v in load_vectors() if applicable(v) for ch in v["text"]]
    for _ in range(500):
        text = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 30)))
        want = {m.end() for m in regex.finditer(r"\X", text)} | {0}
        for pos in range(1, len(text)):
            b = snap_break(text, pos, lookback=len(text))
            assert b in want or not [x for x in want if 0 < x <= pos], (ascii(text), pos, b)


# --- preference order and window ---------------------------------------------------


def test_newline_beats_whitespace_and_sentence_end():
    text = "first line\nsecond line. third"
    assert snap_break(text, len(text) - 2) == len("first line\n")
    assert snap_break("ab\ncd ef", 7, floor=3) == 6  # the newline is at the floor


def test_whitespace_or_sentence_end_beats_the_grapheme_fallback():
    assert snap_break("alpha beta", 8) == 6
    assert snap_break("第一章。本文本文", 7) == 4
    assert snap_break("Wow!Then", 6) == 4
    assert snap_break("ab\u3000cd", 4) == 3


def test_last_candidate_wins():
    text = "one two three four"
    assert snap_break(text, 16) == len("one two three ")
    assert snap_break(text, 13) == len("one two ")


def test_no_break_spaces_are_skipped():
    for nbsp in ("\u00a0", "\u2007", "\u202f"):
        text = f"aa bb{nbsp}cc"
        assert snap_break(text, 7) == 3


def test_a_break_is_never_before_an_extender():
    text = "word \u0301tail and more"
    # The space is followed by its accent, so it is no break point: the
    # last grapheme boundary wins instead.
    assert snap_break(text, 8) == 8
    assert snap_break(text, 5) == 4
    assert snap_break("x\r\ny", 2) == 1  # CR LF stays whole


def test_grapheme_fallback():
    assert snap_break("abcdef", 4) == 4
    flags = "\U0001f1ef\U0001f1f5\U0001f1fa\U0001f1f8\U0001f1eb\U0001f1f7"
    assert snap_break(flags, 3) == 2
    assert snap_break(flags, 5) == 4
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467xyz"
    assert snap_break(family, 4) == 4      # no boundary in (0, 4]: a hard cut
    assert snap_break(family, 4, floor=1) == 4
    assert snap_break(family, 5) == 5      # after the whole family
    assert snap_break(family, 6) == 6
    decomposed = NFD("ếế")
    assert snap_break(decomposed, 2) == 2 and snap_break(decomposed, 4) == 3


def test_lookback_limits_the_window():
    text = "a " + "b" * 50
    assert snap_break(text, 40, lookback=45) == 2
    assert snap_break(text, 40, lookback=10) == 40
    assert snap_break(text, 40, lookback=0) == 40
    assert snap_break(text, 40, lookback=-5) == 40


def test_cjk_truncation_is_a_hard_cut_not_the_title():
    assert snap_break("第一章 " + "本" * 279, 180, lookback=45) == 180


def test_ends_and_floor():
    assert snap_break("abc", 3) == 3
    assert snap_break("abc", 99) == 3
    assert snap_break("", 0) == 0
    assert snap_break("", 5) == 0
    assert snap_break("abc def", 0) == 0
    assert snap_break("abc def", -3) == 0
    assert snap_break("abc def", 2, floor=2) == 2
    assert snap_break("abc def", 2, floor=5) == 2
    assert snap_break("abc def", 5, floor=4) == 5    # only (4, 5] is open
    assert snap_break("ab cd", 4, floor=-10) == 3     # negative floor counts as 0


def test_argument_types():
    with pytest.raises(TypeError):
        snap_break(None, 1)
    with pytest.raises(TypeError):
        snap_break(b"abc", 1)
    with pytest.raises(TypeError):
        snap_break("abc", 1.5)
    with pytest.raises(TypeError):
        snap_break("abc", 1, lookback="3")
    with pytest.raises(TypeError):
        snap_break("abc", 1, floor=None)
    assert snap_break("abc def", True) == 1


# --- paging ------------------------------------------------------------------------


@pytest.mark.parametrize("max_chars", [1, 2, 5, 20, 299, 300, 1000])
def test_paging_moves_forward_and_covers_the_text_once(max_chars):
    rnd = random.Random(5)
    alphabet = ["a", "b", " ", "\n", "é", NFD("é"), "\U0001F1EF\U0001F1F5", "क्ष", "。", "\u00a0",
                "👍🏽", "\u00ad", "ก", "ำ", "\r\n", "\u200d", "가"]
    for _ in range(150):
        text = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 300)))
        offset, pages = 0, []
        while offset < len(text):
            end = snap_break(text, offset + max_chars, floor=offset)
            assert offset < end <= min(offset + max_chars, len(text))
            pages.append(text[offset:end])
            offset = end
        assert "".join(pages) == text


def test_long_mark_runs_and_huge_lookback_stay_cheap():
    import time

    zalgo = "a" + "\u0301" * 50000
    start = time.perf_counter()
    assert snap_break(zalgo, 40000, lookback=50000) == 40000  # no boundary: hard cut
    words = "word " * 200000
    assert snap_break(words, 999_999, lookback=10**9) == 999_995
    assert time.perf_counter() - start < 2


# --- regeneration ------------------------------------------------------------------


def _regenerate():  # pragma: no cover - maintenance tool
    import regex

    if regex.__version__ != REGEX_VERSION:
        raise SystemExit(f"need regex=={REGEX_VERSION}, found {regex.__version__}")
    out = []
    for note, text, need in SAMPLES:
        vector = {"note": note, "text": text,
                  "boundaries": sorted({0} | {m.end() for m in regex.finditer(r"\X", text)}),
                  "regex": regex.__version__}
        if need:
            vector["min_unidata"] = need
        out.append(vector)
    VECTORS.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(out)} vectors to {VECTORS}")


if __name__ == "__main__":  # pragma: no cover
    if sys.argv[1:] != ["--regenerate"]:
        raise SystemExit(__doc__)
    _regenerate()
