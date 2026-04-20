"""EPUB CFI location — a pure value object.

An Apple Books annotation stores its position as an EPUB Canonical
Fragment Identifier, e.g.::

    epubcfi(/6/8[item5]!/4/2[pgepubid00005]/18/1,:629,:691)

The :class:`Location` wraps a CFI and eagerly pulls out the only two
pieces of information that are usefully derivable from the string
alone — everything else requires the book itself:

* :attr:`chapter_id` — the manifest item id of the spine entry this
  location points at, taken from the last bracket hint after ``/6/N``.
  ``None`` if the CFI carries no bracket hint (bracket hints are
  optional per the EPUB CFI spec, so this is an optimistic parse;
  non-matches are acceptable).
* :attr:`char_range` — the ``(start, end)`` offsets from a
  ``,:start,:end`` range suffix. ``None`` when the CFI doesn't encode
  one. These offsets index into the leaf XHTML text node, not the
  extracted plain text, so they're rarely directly useful to callers
  — they're kept for completeness.

Nothing here depends on a :class:`~py_apple_books.content.BookContent`
or on the rest of the library. Resolving ``chapter_id`` to an actual
chapter, or extracting text around a location, is done at the facade
layer (see :meth:`PyAppleBooks.get_annotation_surrounding_text`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Tuple


# ``epubcfi(...)`` wrapper. The body can contain any character; we
# don't try to validate CFI syntax beyond extracting the two pieces we
# care about.
_CFI_WRAPPER = re.compile(r"^\s*epubcfi\((.+)\)\s*$")

# Trailing ``,:start,:end`` suffix that encodes a character range.
_CHAR_RANGE = re.compile(r",:(\d+),:(\d+)\s*$")

# Bracketed manifest-id hints — ``/6/26[id134]`` etc. We use a simple
# findall and pick the last hit in the spine path, because by EPUB CFI
# convention the immediately-following ``[id]`` after the spine step
# is the manifest item id of that spine entry.
_BRACKET_HINT = re.compile(r"\[([^\]]+)\]")


def _parse_cfi(
    cfi: str,
) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    """Optimistic CFI parse. Returns ``(chapter_id, char_range)``.

    Any component we can't confidently extract comes back as ``None``.
    Not a validator — we never raise on malformed input.
    """
    if not cfi:
        return None, None
    m = _CFI_WRAPPER.match(cfi)
    if not m:
        return None, None
    body = m.group(1)

    # Spine path is everything before the ``!``; content path follows.
    spine_path, _, _ = body.partition("!")
    brackets = _BRACKET_HINT.findall(spine_path)
    chapter_id = brackets[-1] if brackets else None

    cr_match = _CHAR_RANGE.search(body)
    char_range = (
        (int(cr_match.group(1)), int(cr_match.group(2))) if cr_match else None
    )

    return chapter_id, char_range


@dataclass(frozen=True)
class Location:
    """Parsed representation of an EPUB CFI.

    :attr cfi: The raw ``epubcfi(...)`` string. Empty string when no
        location was recorded for the annotation.
    :attr chapter_id: Manifest item id of the chapter this location
        points at, parsed optimistically from the CFI's bracket hint.
        ``None`` when the CFI carries no hint. Callers that need to
        fetch the chapter's text should guard on this and skip when
        absent.
    :attr char_range: Character offsets ``(start, end)`` within the
        leaf XHTML text node when the CFI encodes a range. Note: this
        is not the offset in the extracted plain text; it's where the
        highlight lives in the raw source. Kept for diagnostics; not
        used for resolution.
    """

    cfi: str
    chapter_id: Optional[str] = field(init=False)
    char_range: Optional[Tuple[int, int]] = field(init=False)

    def __post_init__(self) -> None:
        chapter_id, char_range = _parse_cfi(self.cfi)
        # frozen=True requires object.__setattr__ to populate derived fields.
        object.__setattr__(self, "chapter_id", chapter_id)
        object.__setattr__(self, "char_range", char_range)

    def __bool__(self) -> bool:
        return bool(self.cfi)

    def __str__(self) -> str:
        """The raw CFI string — lets ``str(location)`` round-trip with
        anyone expecting the underlying representation."""
        return self.cfi
