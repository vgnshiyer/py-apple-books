"""Text folding for search matching.

Apple Books keeps highlights and titles with typographic punctuation
(’ “ ” – —), line breaks, non-breaking spaces and accents, while
queries are usually typed in plain ASCII. :func:`fold_for_match` maps
both sides to one form so they compare equal. The folded text is only
ever used for matching, never shown.
"""

import re
import unicodedata
from typing import Optional

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


def fold_for_match(s) -> Optional[str]:
    """Fold ``s`` so typographic variants of the same text compare equal.

    Pure, deterministic and never raises. Rules:

    * ``None`` → ``None``; ``bytes`` are decoded as UTF-8 (invalid
      bytes become U+FFFD); any other non-str goes through ``str()``.
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
    if s.isascii():
        return _WHITESPACE.sub(" ", s).lower()
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        s = s.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    s = unicodedata.normalize("NFKD", s.casefold()).casefold().translate(_MAP)
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", s))
