"""Text helpers: match folding, passage location, Unicode cleanup and
break points.

Pure functions on strings: no I/O, and no imports from the rest of the
package (the models and the database client import this module).

Apple Books keeps highlights and titles with typographic punctuation
(’ “ ” – —), line breaks, non-breaking spaces and accents, while
queries are usually typed in plain ASCII. :func:`fold_for_match` maps
both sides to one form so they compare equal; :func:`finditer_folded`
and :func:`find_folded` apply the same fold to find a query inside a
longer text and report where it is in the original. Folded text is only
ever used for matching, never shown. :func:`selection_core` output, on
the other hand, is meant to be shown: it is a highlight trimmed to its
word or phrase, and :func:`is_short_selection` tells a highlighted word
or short phrase from a passage.

:func:`normalize_unicode` removes the invisible characters that break
exact matching (soft hyphens, zero-width spaces, BOMs) and composes
accents. :func:`snap_break` picks where to end a page of text so that a
line, a word or a user-perceived character is not cut in two.
"""

import bisect
import operator
import re
import threading
import unicodedata
from typing import Dict, Iterator, List, Optional, Pattern, Tuple

__all__ = [
    "fold_for_match",
    "finditer_folded",
    "find_folded",
    "normalize_unicode",
    "snap_break",
    "selection_core",
    "is_short_selection",
]

_WHITESPACE = re.compile(r"\s+")

# Applied after NFKD, which has already turned NBSP and the other
# compatibility spaces into ' ', '…' into '...' and ligatures into
# their letters.
_MAP = {}
for _ch in "’‘‚‛′‵‹›ʼ＇":
    _MAP[ord(_ch)] = "'"
for _ch in "“”„‟″‶«»＂":
    _MAP[ord(_ch)] = '"'
for _ch in "‐‑‒–—―−﹘﹣－":
    _MAP[ord(_ch)] = "-"
# Soft hyphen, zero-width space / non-joiner / joiner, word joiner, BOM.
for _cp in (0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF):
    _MAP[_cp] = None
# Combining diacritical marks (Latin accents, split off by NFKD). Marks
# of other scripts (e.g. the kana voicing mark in が) are kept, so NFC
# recomposes those characters unchanged.
for _cp in range(0x0300, 0x0370):
    _MAP[_cp] = None
del _ch, _cp


def _coerce_text(s) -> Optional[str]:
    """The input rules of :func:`fold_for_match`, shared by the other
    helpers that accept any object.

    ``None`` → ``None``; ``bytes``/``bytearray`` are decoded as UTF-8
    (invalid bytes become U+FFFD); any other non-str goes through
    ``str()``, and ``None`` is returned if that fails. Lone surrogates
    are replaced (by U+FFFD), so the result always encodes as UTF-8.
    Replacement can only lengthen a string, never shorten it.
    """
    if s is None:
        return None
    if not isinstance(s, str):
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", "replace")
        else:
            try:
                s = str(s)
            except Exception:
                # e.g. an int beyond sys.get_int_max_str_digits(): no
                # text to match, and None matches nothing in SQL.
                return None
    if not s.isascii():
        try:
            s.encode("utf-8")
        except UnicodeEncodeError:
            s = s.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    return s


def fold_for_match(s) -> Optional[str]:
    """Fold ``s`` so typographic variants of the same text compare equal.

    Pure, deterministic and never raises. Rules:

    * ``None`` → ``None``; ``bytes`` are decoded as UTF-8 (invalid
      bytes become U+FFFD); any other non-str goes through ``str()``
      (``None`` if that fails).
    * Lone surrogates become U+FFFD, so the result can always be
      encoded as UTF-8 (and bound as an SQLite parameter).
    * ASCII text: every whitespace run becomes one space, then
      ``lower()``.
    * Anything else, in order: ``casefold`` (ß → ss), NFKD (ﬁ → fi,
      … → ..., NBSP → space), ``casefold`` again (NFKD can produce
      capitals: ℃ → °C, 𝐀 → A), curly quotes and primes → ``'``/``"``,
      dashes and minus signs → ``-``, soft hyphen, zero-width
      characters and U+0300–U+036F combining accents dropped (Gödel →
      godel), NFC, then every whitespace run becomes one space.

    Leading and trailing whitespace is collapsed, never stripped.
    Folding is idempotent: ``fold_for_match(fold_for_match(s)) ==
    fold_for_match(s)``.

    Stability: this is public API, and every search of the library
    applies it to both the query and the searched text. It stays
    deterministic, idempotent and never raising. A minor release may only
    make it fold more (treat more variants as equal); anything else is a
    major-release change.
    """
    s = _coerce_text(s)
    if s is None:
        return None
    if s.isascii():
        return _WHITESPACE.sub(" ", s).lower()
    s = unicodedata.normalize("NFKD", s.casefold()).casefold().translate(_MAP)
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", s))


# ---------------------------------------------------------------------------
# Folded search: fold_for_match applied character by character, with the
# way back to the original offsets.

# Per-character fold of the Basic Multilingual Plane, derived from
# fold_for_match on first use (so the two cannot drift): code point → fold
# of that one character, for the characters whose fold differs from the
# character itself. Built once, then never mutated (safe to share across
# threads, free-threaded builds included). Characters above U+FFFF are
# rare in book text and are folded per call instead.
_FOLD_LOCK = threading.Lock()
_FOLD_TABLES: Optional[Tuple[Dict[int, str], Pattern, frozenset]] = None

# ASCII whitespace other than ' ' (\t \n \v \f \r and U+001C-U+001F), which
# folds to ' ' like every whitespace character.
_ASCII_SPACES = {cp: " " for cp in range(0x80) if chr(cp).isspace() and cp != 0x20}
_SPACE_RUN = re.compile(" {2,}")


def _fold_tables() -> Tuple[Dict[int, str], Pattern, frozenset]:
    """``(table, irregular, zero_marks)``, built on first use.

    * ``table``: code point → fold, for the BMP characters whose fold is
      not the character itself.
    * ``irregular``: matches one character whose fold is not exactly one
      character long (dropped, or expanded like ß → ss), or any character
      above U+FFFF (folded per call).
    * ``zero_marks``: combining marks whose fold is empty (the Latin
      accents); a match is extended over them, so it ends on a whole
      character.
    """
    global _FOLD_TABLES
    tables = _FOLD_TABLES
    if tables is None:
        with _FOLD_LOCK:
            tables = _FOLD_TABLES
            if tables is None:
                tables = _FOLD_TABLES = _build_fold_tables()
    return tables


def _build_fold_tables() -> Tuple[Dict[int, str], Pattern, frozenset]:
    table: Dict[int, str] = {}
    irregular: List[int] = []
    zero_marks = set()
    for cp in range(0x10000):
        ch = chr(cp)
        # Only these can fold to something else: fold_for_match is
        # casefold, NFKD (identity without a decomposition, except Hangul
        # syllables, which NFC recomposes), _MAP and the whitespace rule;
        # lone surrogates become U+FFFD. tests/test_text_folded.py checks
        # the shortcut against fold_for_match for every BMP code point.
        if not (cp < 0x80 or 0xD800 <= cp <= 0xDFFF or cp in _MAP or ch.isspace()
                or unicodedata.decomposition(ch) or ch.casefold() != ch):
            continue
        folded = fold_for_match(ch)
        if folded == ch:
            continue
        table[cp] = folded
        if len(folded) != 1:
            irregular.append(cp)
            if not folded and unicodedata.category(ch).startswith("M"):
                zero_marks.add(cp)
    pattern = re.compile("[" + _char_class(irregular) + "\U00010000-\U0010FFFF]")
    return table, pattern, frozenset(zero_marks)


def _char_class(cps: List[int]) -> str:
    """Regex character-class body for sorted code points, as ranges."""
    parts = []
    i = 0
    while i < len(cps):
        j = i
        while j + 1 < len(cps) and cps[j + 1] == cps[j] + 1:
            j += 1
        first, last = re.escape(chr(cps[i])), re.escape(chr(cps[j]))
        parts.append(first if i == j else f"{first}-{last}")
        i = j + 1
    return "".join(parts)


def _char_fold(ch: str) -> str:
    """The fold of the single character ``ch`` used by the folded search:
    ``fold_for_match(ch)`` (whitespace folds to one space)."""
    cp = ord(ch)
    if cp > 0xFFFF:
        return fold_for_match(ch)
    return _fold_tables()[0].get(cp, ch)


def _translate(text: str) -> Tuple[str, Dict[int, str], List[int]]:
    """``text`` folded character by character, before space runs are
    collapsed: ``(folded, table, irregular)``, where ``irregular`` lists
    the offsets of the characters whose fold is not one character long."""
    table, irregular_re, _ = _fold_tables()
    if text.isascii():
        return text.lower().translate(_ASCII_SPACES), table, []
    candidates = [m.start() for m in irregular_re.finditer(text)]
    astral: Dict[int, str] = {}
    for i in candidates:
        cp = ord(text[i])
        if cp > 0xFFFF and cp not in astral:
            astral[cp] = fold_for_match(text[i])
    if astral:
        table = {**table, **{cp: f for cp, f in astral.items() if f != chr(cp)}}
    irregular = [i for i in candidates if len(table.get(ord(text[i]), "x")) != 1]
    return text.translate(table), table, irregular


def _per_char_fold(s: str) -> str:
    """``s`` folded character by character, space runs collapsed (the
    form both sides of :func:`finditer_folded` are compared in)."""
    return _SPACE_RUN.sub(" ", _translate(s)[0])


class _FoldMap:
    """``text`` folded character by character, with the way back.

    ``folded`` is the per-character fold of ``text`` with every space run
    collapsed to one space. :meth:`span` maps a match ``folded[a:b]``
    (which never starts or ends with a space) back to offsets of
    ``text``.
    """

    __slots__ = ("text", "folded", "_g_offs", "_o_idx", "_lens", "_run_fs", "_run_cum",
                 "_zero_marks")

    def __init__(self, text: str):
        g, table, irregular = _translate(text)
        self.text = text
        self._zero_marks = _fold_tables()[2]
        # Irregular characters, in order: their offset in g, their offset
        # in text and the length of their fold. Every other character
        # folds to exactly one character.
        g_offs, o_idx, lens = [], [], []
        shift = 0
        for i in irregular:
            n = len(table[ord(text[i])])
            g_offs.append(i + shift)
            o_idx.append(i)
            lens.append(n)
            shift += n - 1
        self._g_offs, self._o_idx, self._lens = g_offs, o_idx, lens
        # Space runs collapsed: the offset in folded of each kept space,
        # and the number of spaces dropped up to and including that run.
        run_fs, run_cum = [], []
        dropped = 0
        for m in _SPACE_RUN.finditer(g):
            run_fs.append(m.start() - dropped)
            dropped += m.end() - m.start() - 1
            run_cum.append(dropped)
        self._run_fs, self._run_cum = run_fs, run_cum
        self.folded = _SPACE_RUN.sub(" ", g) if run_fs else g

    def _source(self, f: int) -> int:
        """Offset in ``text`` of the character whose fold produced
        ``folded[f]`` (never a collapsed space)."""
        k = bisect.bisect_left(self._run_fs, f)
        g = f + (self._run_cum[k - 1] if k else 0)
        j = bisect.bisect_right(self._g_offs, g) - 1
        if j < 0:
            return g
        off, n = self._g_offs[j], self._lens[j]
        if g < off + n:
            return self._o_idx[j]
        return self._o_idx[j] + 1 + (g - off - n)

    def span(self, a: int, b: int) -> Tuple[int, int]:
        """``text`` offsets of the whole characters that fold to
        ``folded[a:b]``, plus any combining accents that follow."""
        start = self._source(a)
        end = self._source(b - 1) + 1
        text, marks = self.text, self._zero_marks
        while end < len(text) and ord(text[end]) in marks:
            end += 1
        return start, end


def _fold_query(query) -> str:
    q = _coerce_text(query)
    if not q:
        return ""
    return _per_char_fold(q).strip(" ")


def _iter_folded(text: str, q: str) -> Iterator[Tuple[int, int]]:
    if not q or not text:
        return
    fm = _FoldMap(text)
    folded, n = fm.folded, len(q)
    last = 0
    i = folded.find(q)
    while i != -1:
        start, end = fm.span(i, i + n)
        # Two matches inside one character's fold (s in ß → ss) widen to
        # the same character: report it once.
        if start >= last:
            yield start, end
            last = end
        i = folded.find(q, i + n)


def finditer_folded(text: str, query) -> Iterator[Tuple[int, int]]:
    """Find ``query`` in ``text`` the way the library's searches match:
    folded (:func:`fold_for_match`), with the matches given as offsets
    into ``text``.

    Yields non-overlapping ``(start, end)`` spans of ``text``, in order,
    such that the fold of ``text[start:end]`` contains the folded query.
    Both sides are folded one character at a time with
    :func:`fold_for_match`, and every whitespace run becomes one space;
    this equals ``fold_for_match`` of the whole string except where NFC
    would combine characters outside the Latin accents (for example
    decomposed Hangul syllables). So ``CAFE`` finds ``café`` (composed or
    not), ``don't`` finds ``don’t``, ``strasse`` finds ``Straße``,
    ``office...`` finds ``oﬃce…`` and a space finds a line break.

    A match that starts or ends inside the fold of one character (an
    ``s`` of ``ß``) is widened to the whole character, and a match is
    extended over the combining accents that follow it, so a span never
    ends inside a character. The query is converted like
    :func:`fold_for_match`'s input (``None`` and ``bytes`` included), and
    its leading and trailing whitespace is ignored; a query that folds to
    nothing (empty, whitespace only, or only characters the fold drops)
    yields nothing.

    ``text`` must be a ``str`` (``TypeError`` otherwise). The fold of
    ``text`` is computed when iteration starts (linear in its length) and
    is not cached.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be a str, not {type(text).__name__}")
    return _iter_folded(text, _fold_query(query))


def find_folded(text: str, query, start: int = 0,
                end: Optional[int] = None) -> Optional[Tuple[int, int]]:
    """The first match of :func:`finditer_folded` in ``text[start:end]``,
    as ``(start, end)`` offsets into ``text``, or ``None``.

    ``start`` and ``end`` are slice bounds (negative values count from
    the end, out-of-range values are clipped); only the slice is folded.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be a str, not {type(text).__name__}")
    lo, hi, _ = slice(start, end).indices(len(text))
    if hi <= lo:
        return None
    window = text if (lo, hi) == (0, len(text)) else text[lo:hi]
    for a, b in _iter_folded(window, _fold_query(query)):
        return a + lo, b + lo
    return None


# ---------------------------------------------------------------------------
# Unicode cleanup and passage location

# Characters that break exact matching and carry no text: soft hyphen,
# zero-width space, BOM (zero-width no-break space).
_INVISIBLE = "­​﻿"
_DELETE_INVISIBLE = dict.fromkeys(map(ord, _INVISIBLE))
_INVISIBLE_RE = re.compile("[" + _INVISIBLE + "]")


def normalize_unicode(s: Optional[str]) -> Optional[str]:
    """``s`` without soft hyphens (U+00AD), zero-width spaces (U+200B) and
    BOMs (U+FEFF), then in NFC (decomposed accents composed).

    Keeps everything else: ZWJ and ZWNJ (emoji, Indic and Persian
    spelling), bidi controls, NBSP, U+202F and U+3000; whitespace is not
    touched. ``None`` → ``None``; ASCII text is returned as is. Pure and
    idempotent. Deleting before composing matters: a soft hyphen between
    a letter and its accent would otherwise keep them apart.
    """
    if s is None:
        return None
    if not isinstance(s, str):
        raise TypeError(f"s must be a str or None, not {type(s).__name__}")
    if s.isascii():
        return s
    return unicodedata.normalize("NFC", s.translate(_DELETE_INVISIBLE))


def _passage_pattern(passage: str) -> Optional[str]:
    """1.10's highlight pattern: the passage's words, regex-escaped,
    joined by ``\\s+`` (any whitespace run between words)."""
    tokens = passage.split()
    return r"\s+".join(map(re.escape, tokens)) if tokens else None


def _find_passage(text: str, passage: str) -> List[Tuple[int, int]]:
    """Every non-overlapping match of ``passage`` in ``text``, left to
    right, as ``(start, end)`` offsets into ``text``.

    A whitespace run in the passage matches any whitespace run, and soft
    hyphens, zero-width spaces and BOMs are ignored on both sides;
    everything else is exact (case, punctuation, accents; regex
    metacharacters are literal). ``[]`` when the passage has no visible
    characters, or either argument is not a non-empty str. Pure, never
    raises.

    Without those three characters in ``text`` this is 1.10's match
    (same pattern, so the first span is what 1.10 found). With them, the
    same pattern runs on a copy without them and the spans are mapped
    back: no larger pattern, and linear for typical input.
    """
    if not isinstance(text, str) or not isinstance(passage, str) or not text or not passage:
        return []
    pattern = _passage_pattern(passage.translate(_DELETE_INVISIBLE))
    if pattern is None:
        return []
    try:
        if _INVISIBLE_RE.search(text) is None:
            return [m.span() for m in re.finditer(pattern, text)]
        # Offset in the cleaned copy of each deleted character's
        # successor, ascending: a cleaned offset c is c plus the number
        # of deletions at or before it in the original.
        marks = [m.start() - k for k, m in enumerate(_INVISIBLE_RE.finditer(text))]
        clean = text.translate(_DELETE_INVISIBLE)
        spans = []
        for m in re.finditer(pattern, clean):
            start = m.start() + bisect.bisect_right(marks, m.start())
            last = m.end() - 1
            spans.append((start, last + bisect.bisect_right(marks, last) + 1))
        return spans
    except (re.error, OverflowError, RecursionError):
        return []


# ---------------------------------------------------------------------------
# Break points: snap_break and a stdlib approximation of extended grapheme
# cluster boundaries (Unicode Standard Annex #29).

# Whitespace that does not allow a line break there.
_NO_BREAK_SPACES = "   "
# A breaking whitespace character, or the end of a CJK (or ! ?) sentence.
_BREAK_AFTER = re.compile("[^\\S" + _NO_BREAK_SPACES + "]|[。！？．!?]")

_ZWJ = 0x200D
# Format characters (Cf) that are not grapheme Controls: ZWNJ (extends),
# ZWJ, the prepended concatenation marks (Prepend) and the tag characters
# (Extend). Every other Cc, Cf, Zl and Zp character is a Control.
_PREPEND = frozenset([*range(0x0600, 0x0606), 0x06DD, 0x070F, 0x0890, 0x0891, 0x08E2,
                      0x0D4E, 0x110BD, 0x110CD, 0x111C2, 0x111C3, 0x1193F, 0x11941,
                      *range(0x11A84, 0x11A8A), 0x11D46, 0x11F02])
# Extending characters outside the mark categories: ZWNJ, the halfwidth
# kana voicing marks, emoji skin-tone modifiers and tags; plus the two
# letters that are spacing marks (Thai and Lao SARA AM).
_EXTEND_OTHER = frozenset([0x200C, 0x0E33, 0x0EB3, 0xFF9E, 0xFF9F, *range(0x1F3FB, 0x1F400),
                           *range(0xE0020, 0xE0080)])
# Marks that do not extend the previous character (Grapheme_Cluster_Break
# Other): Myanmar, Tai Tham, Tai Viet and Ahom vowel signs.
_MARK_NOT_EXTEND = frozenset([0x102B, 0x102C, 0x1038, 0x1062, 0x1063, 0x1064,
                              *range(0x1067, 0x106E), 0x1083, *range(0x1087, 0x108D), 0x108F,
                              0x109A, 0x109B, 0x109C, 0x1A61, 0x1A63, 0x1A64, 0xAA7B, 0xAA7D,
                              0x11720, 0x11721])
# Viramas that link two consonants into one cluster (Indic conjuncts, rule
# GB9c), and the blocks whose letters they link.
_LINKERS = frozenset([0x094D, 0x09CD, 0x0ACD, 0x0B4D, 0x0C4D, 0x0D4D, 0x1039, 0x17D2, 0x1A60,
                      0x1B44, 0xA9C0, 0x10A3F, 0x11133, 0x1193E, 0x11A47, 0x11F42])
_LINKER_BLOCKS = frozenset(cp & ~0x7F for cp in _LINKERS)
# Blocks laid out like Devanagari: independent vowels at offsets 0x04-0x14.
_ISCII_BLOCKS = frozenset([0x0900, 0x0980, 0x0A80, 0x0B00, 0x0C00, 0x0D00])
# Extended_Pictographic outside the symbol categories, roughly (emoji).
_PICTOGRAPHIC_OTHER = frozenset([0x203C, 0x2049, 0x2194, 0x2195, 0x2196, 0x2197, 0x2198,
                                 0x2199, 0x21A9, 0x21AA, 0x2934, 0x2935, 0x3030, 0x303D])


def _category(cp: int) -> str:
    return unicodedata.category(chr(cp))


def _is_control(cp: int) -> bool:
    cat = _category(cp)
    if cat in ("Cc", "Zl", "Zp"):
        return True
    return cat == "Cf" and cp not in (0x200C, _ZWJ) and cp not in _PREPEND \
        and not 0xE0020 <= cp <= 0xE007F


def _is_extend(cp: int) -> bool:
    """Grapheme Extend or SpacingMark (no boundary before it)."""
    if cp in _EXTEND_OTHER:
        return True
    return _category(cp).startswith("M") and cp not in _MARK_NOT_EXTEND


def _hangul(cp: int) -> Optional[str]:
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97C:
        return "L"
    if 0x1160 <= cp <= 0x11A7 or 0xD7B0 <= cp <= 0xD7C6:
        return "V"
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return "T"
    if 0xAC00 <= cp <= 0xD7A3:
        return "LV" if (cp - 0xAC00) % 28 == 0 else "LVT"
    return None


def _is_consonant(cp: int) -> bool:
    """A letter a linking virama joins (InCB=Consonant, approximately)."""
    block = cp & ~0x7F
    if block not in _LINKER_BLOCKS or _category(cp) != "Lo":
        return False
    return block not in _ISCII_BLOCKS or (cp & 0x7F) > 0x14


def _is_pictographic(cp: int) -> bool:
    return (0x1F000 <= cp <= 0x1FFFD or cp in _PICTOGRAPHIC_OTHER
            or _category(cp) == "So")


def _linked(text: str, i: int) -> bool:
    """Rule GB9c: consonant, linking virama (with marks or ZWJ around
    it), then the consonant at ``i``."""
    if not _is_consonant(ord(text[i])):
        return False
    j, linker = i - 1, False
    while j >= 0 and i - j <= 32:
        cp = ord(text[j])
        if cp in _LINKERS:
            linker = True
        elif not (cp == _ZWJ or _is_extend(cp)):
            break
        j -= 1
    return linker and j >= 0 and _is_consonant(ord(text[j]))


def _emoji_zwj(text: str, i: int) -> bool:
    """Rule GB11: pictograph, extenders, ZWJ (at ``i - 1``), then the
    pictograph at ``i``."""
    if not _is_pictographic(ord(text[i])):
        return False
    j = i - 2
    while j >= 0 and i - j <= 32 and _is_extend(ord(text[j])):
        j -= 1
    return j >= 0 and _is_pictographic(ord(text[j]))


def _is_grapheme_boundary(text: str, i: int) -> bool:
    """Whether ``text[:i]`` ends on an extended grapheme cluster boundary.

    A stdlib approximation of UAX #29: CR LF, controls, Hangul syllable
    sequences, marks and other extenders, spacing marks, prepended
    characters, Indic conjuncts (virama between consonants), emoji ZWJ
    sequences and modifiers, tags, and regional-indicator pairs. It
    follows the running Python's Unicode database for categories.
    """
    if i <= 0 or i >= len(text):
        return True
    a, b = ord(text[i - 1]), ord(text[i])
    if a == 0x0D:
        return b != 0x0A                                      # GB3, GB4
    if a == 0x0A or _is_control(a):
        return True                                           # GB4
    if b in (0x0A, 0x0D) or _is_control(b):
        return True                                           # GB5
    ha, hb = _hangul(a), _hangul(b)
    if ha and hb and ((ha == "L" and hb != "T") or (ha in ("LV", "V") and hb in ("V", "T"))
                      or (ha in ("LVT", "T") and hb == "T")):
        return False                                          # GB6-GB8
    if b == _ZWJ or _is_extend(b):
        return False                                          # GB9, GB9a
    if a in _PREPEND:
        return False                                          # GB9b
    if _linked(text, i):
        return False                                          # GB9c
    if a == _ZWJ and _emoji_zwj(text, i):
        return False                                          # GB11
    if 0x1F1E6 <= a <= 0x1F1FF and 0x1F1E6 <= b <= 0x1F1FF:
        j = i - 1
        while j >= 0 and 0x1F1E6 <= ord(text[j]) <= 0x1F1FF:
            j -= 1
        return (i - 1 - j) % 2 == 0                           # GB12, GB13
    return True                                               # GB999


def snap_break(text: str, pos: int, *, lookback: int = 300, floor: int = 0) -> int:
    """Where to end ``text[:b]`` near ``pos`` without cutting a line, a
    word or a character: an offset ``b`` with ``floor < b <= pos``.

    * ``pos >= len(text)``: ``len(text)``. ``pos <= floor`` (a negative
      ``floor`` counts as 0): ``max(pos, 0)``.
    * Otherwise it looks at the candidates ``b`` in the window
      ``(max(floor, pos - lookback), pos]`` and returns, by preference:

      1. the offset after the last ``\\n``;
      2. else after the last breaking whitespace (not NBSP, U+2007 or
         U+202F) or sentence end (``。！？．!?``) that is not followed by
         a mark or other extender of its character;
      3. else the last extended grapheme cluster boundary, so no
         accented letter, emoji or flag is split;
      4. else ``pos`` (a hard cut).

    So ``floor < b <= pos`` whenever ``floor < pos < len(text)``: paging
    with ``end = snap_break(text, offset + size, floor=offset)`` always
    moves forward and covers the text exactly once.

    The grapheme rule approximates Unicode's extended grapheme clusters
    (UAX #29) with the standard library only: CR LF, controls, combining
    and spacing marks, ZWJ emoji sequences, skin-tone modifiers and tags,
    regional-indicator (flag) pairs, Hangul syllable sequences,
    prepended characters and virama conjuncts. It follows the running
    Python's Unicode database. Pure; raises ``TypeError`` only for
    arguments of the wrong type.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be a str, not {type(text).__name__}")
    pos, lookback, floor = operator.index(pos), operator.index(lookback), operator.index(floor)
    n = len(text)
    if pos >= n:
        return n
    floor = max(floor, 0)
    if pos <= floor:
        return max(pos, 0)
    lo = max(floor, pos - max(lookback, 0))      # candidates: lo < b <= pos
    k = text.rfind("\n", lo, pos)
    if k != -1:
        return k + 1
    ends = [m.end() for m in _BREAK_AFTER.finditer(text, lo, pos)]
    for b in reversed(ends):
        if _is_grapheme_boundary(text, b):
            return b
    for b in range(pos, lo, -1):
        if _is_grapheme_boundary(text, b):
            return b
    return pos


# ---------------------------------------------------------------------------
# Short selections: a highlighted word or phrase rather than a passage.

# Private thresholds (documented in is_short_selection; changing one needs
# a changelog entry).
_SHORT_MAX_RAW = 64       # code points, before any normalisation
_SHORT_MAX_CHARS = 30     # code points of the core
_SHORT_MAX_WORDS = 3      # space-separated tokens
_SHORT_MAX_NO_SPACE = 8   # code points of a core in a script without spaces

# Trimmed from the edges of a selection besides whitespace and the
# punctuation, symbol and separator categories.
_EDGE_INVISIBLE = frozenset("­​‌‍⁠﻿")
_SENTENCE_MARK = re.compile("[.!?;:…。！？；：]")
# Scripts written without spaces between words: kana, CJK ideographs
# (unified, compatibility, extensions B-H), Thai, Lao, Myanmar, Khmer.
_NO_SPACE_SCRIPT = re.compile(
    "[぀-ヿ㐀-䶿一-鿿豈-﫿฀-໿"
    "က-႟ក-៿\U00020000-\U000323af]")


def _is_edge(ch: str) -> bool:
    return (ch.isspace() or ch in _EDGE_INVISIBLE
            or unicodedata.category(ch)[0] in "PSZ")


def selection_core(text) -> str:
    """A highlight trimmed to its word or phrase, for display.

    The input is converted like :func:`fold_for_match`'s (``None`` and a
    failed ``str()`` give ``''``; ``bytes`` are decoded as UTF-8; lone
    surrogates become U+FFFD), put in NFC, stripped of leading and
    trailing whitespace, punctuation, symbols, zero-width characters and
    soft hyphens, and every inner whitespace run becomes one space:
    ``'“Ephemeral,”'`` → ``'Ephemeral'``. Never raises.
    """
    s = _coerce_text(text)
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    i, j = 0, len(s)
    while i < j and _is_edge(s[i]):
        i += 1
    while j > i and _is_edge(s[j - 1]):
        j -= 1
    return _WHITESPACE.sub(" ", s[i:j])


def is_short_selection(text) -> bool:
    """Whether a highlight is a word or a short phrase (likely looked up
    or collected) rather than a passage. A documented heuristic, applied
    in order:

    1. the text (converted as by :func:`selection_core`) is longer than
       64 code points: ``False``;
    2. its core (:func:`selection_core`) is empty or longer than 30 code
       points: ``False``;
    3. the core contains sentence punctuation (``. ! ? ; : …`` or
       ``。！？；：``): ``False``;
    4. the core has no letter (``str.isalpha``): ``False``;
    5. the core contains a character of a script written without spaces
       (kana, CJK ideographs, Thai, Lao, Myanmar, Khmer): ``True`` if it
       is at most 8 code points long;
    6. otherwise ``True`` if it has at least 2 letters and at most 3
       space-separated words.

    ``'Ephemeral,'``, ``'“ubiquitous.”'`` and ``'in medias res'`` are
    short; ``'1984'``, ``'a'``, ``'e.g.'`` and a sentence are not. The
    thresholds (64, 30, 3 words, 8) may change in a minor release, with a
    changelog entry. Letters and scripts follow the running Python's
    Unicode database, so a character assigned in a newer Unicode version
    (U+31350, a CJK ideograph of Unicode 15.0) is a letter on Python 3.12
    and later but not on 3.10. Never raises.
    """
    if isinstance(text, str) and len(text) > _SHORT_MAX_RAW:
        return False             # conversion never shortens a str
    s = _coerce_text(text)
    if not s or len(s) > _SHORT_MAX_RAW:
        return False
    core = selection_core(s)
    if not core or len(core) > _SHORT_MAX_CHARS:
        return False
    if _SENTENCE_MARK.search(core):
        return False
    letters = sum(1 for c in core if c.isalpha())
    if not letters:
        return False
    if _NO_SPACE_SCRIPT.search(core):
        return len(core) <= _SHORT_MAX_NO_SPACE
    return letters >= 2 and core.count(" ") + 1 <= _SHORT_MAX_WORDS
