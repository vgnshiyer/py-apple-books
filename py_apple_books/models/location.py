"""EPUB CFI location — a pure value object.

An Apple Books annotation stores its position as an EPUB Canonical
Fragment Identifier, e.g.::

    epubcfi(/6/10[item7]!/4/2[pgepubid00007]/16/1,:412,:478)

The :class:`Location` wraps a CFI and eagerly pulls out the pieces of
information that are usefully derivable from the string alone —
everything else requires the book itself:

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
* :attr:`spine_index` — the 0-based spine position of the chapter,
  from the ``/6/N`` step (``N // 2 - 1``). Apple stores the same number
  in ``ZPLLOCATIONRANGESTART`` (``Annotation.position``) for
  highlights and bookmarks.
* :attr:`sort_key` — a tuple of the CFI's step and offset integers;
  sorting by it puts locations in document order.

:class:`PageLocation` is the other position Apple Books records: the
page (PDFs) or spine item (EPUB bookmarks without a CFI) of a bookmark
row, kept as a small property list in ``ZPLUSERDATA``.

Nothing here depends on a :class:`~py_apple_books.content.BookContent`
or on the rest of the library. Resolving ``chapter_id`` to an actual
chapter, or extracting text around a location, is done at the facade
layer (see :meth:`PyAppleBooks.get_annotation_surrounding_text`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Placeholder for "not in the instance's __dict__".
_UNSET = object()


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

# ``[...]`` assertions (id hints, text assertions), including ``^``
# escapes inside them; dropped before collecting the sort key. An
# unclosed ``[`` matches through to the end with group 1 empty and is
# kept as is: no later ``[`` could close either, and consuming the tail
# in one match keeps the scan linear on runs of unbalanced brackets.
_ASSERTION = re.compile(r"\[(?:\^.?|[^\]^])*(\]|\Z)")

# A step ``/N`` or a character offset ``:N``.
_STEP_OR_OFFSET = re.compile(r"/(\d+)|:(\d+)")


def _cfi_sort_key(cfi: str, end: bool = False) -> Optional[Tuple[int, ...]]:
    """Document-order sort key: the integers of the CFI's ``/N`` steps and
    ``:N`` offsets, in order.

    For a range CFI (``epubcfi(parent,start,end)``) the key covers the
    parent path followed by the start, so a range sorts where it begins;
    with ``end=True``, the parent path followed by the end (the start
    when the CFI has no end). ``None`` for anything that isn't an
    ``epubcfi(...)`` with steps.
    """
    if not cfi:
        return None
    m = _CFI_WRAPPER.match(cfi)
    if not m:
        return None
    body = _ASSERTION.sub(lambda hit: "" if hit.group(1) else hit.group(0), m.group(1))
    # Assertions are gone, so every remaining comma is top-level.
    parent, _, rest = body.partition(",")
    start, _, tail = rest.partition(",")
    stop = tail.partition(",")[0]
    path = parent + (stop if end and stop else start)
    try:
        ints = [int(a or b) for a, b in _STEP_OR_OFFSET.findall(path)]
    except ValueError:  # more digits than int() accepts from a string
        return None
    return tuple(ints) or None


def _spine_index(sort_key: Optional[Tuple[int, ...]]) -> Optional[int]:
    """0-based spine position from the leading ``/6/N`` steps (the spine
    is the package document's third child element, and its itemrefs
    sit at even steps from 2). ``None`` when the CFI doesn't start
    that way."""
    if not sort_key or len(sort_key) < 2 or sort_key[0] != 6 or sort_key[1] < 2:
        return None
    return sort_key[1] // 2 - 1


# -- the content path (1.11, private) ------------------------------------------

#: One step of a CFI's content path: the step's integer (even: a child
#: element, ``2 * (n + 1)`` for the n-th; odd: the text between child
#: elements) and the step's id assertion (``/4[chap01]``), or None.
_Step = Tuple[int, Optional[str]]

# Digits accepted in one step; a longer number is malformed (no document
# has a billion children, and int() of a huge string is slow).
_MAX_STEP_DIGITS = 9


def _top_level(body: str, separators: str) -> Optional[List[str]]:
    """``body`` split at the characters of ``separators`` that are
    outside ``[...]`` assertions (where ``^`` escapes the next
    character). None when a ``[`` is never closed (a malformed CFI,
    whose separators can't be told apart)."""
    parts: List[str] = []
    start = 0
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "[":
            i += 1
            while i < n and body[i] != "]":
                i += 2 if body[i] == "^" else 1
            if i >= n:
                return None
        elif ch in separators:
            parts.append(body[start:i])
            start = i + 1
        i += 1
    parts.append(body[start:])
    return parts


def _assertion_id(raw: str) -> Optional[str]:
    """The id of an element step's assertion text (between the
    brackets): up to the first unescaped ``;`` (parameters dropped),
    with ``^`` escapes undone; None when empty."""
    out: List[str] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "^" and i + 1 < n:
            out.append(raw[i + 1])
            i += 2
            continue
        if ch == ";":
            break
        out.append(ch)
        i += 1
    return "".join(out) or None


def _content_path(cfi: str) -> Optional[Tuple[_Step, ...]]:
    """The steps of a CFI's content path: where in its spine file the
    location is, for :meth:`BookContent.resolve
    <py_apple_books.content.BookContent.resolve>` (and the exact
    positions of later versions).

    The content path is what follows the first ``!`` (the spine path,
    before it, names the file). For a range CFI
    (``epubcfi(parent,start,end)``) it is the parent's content path
    followed by the start's steps, so a range is placed where it begins.
    Character offsets (``:N``), temporal (``~N``) and spatial (``@x:y``)
    offsets are dropped (they end a path), and so is anything after a
    second ``!`` (a reference into another document). Each step keeps
    its id assertion (``/2[chap01]``: ``(2, 'chap01')``; text-location
    parameters after ``;`` dropped, ``^`` escapes undone).

    Returns ``()`` for a CFI that points at a spine item as a whole (no
    ``!``), and None for anything that isn't an ``epubcfi(...)`` or whose
    content path is malformed (a step without digits, or with more than
    nine, an unclosed assertion anywhere in the CFI, a stray character). Never raises; time
    is linear in the length of the CFI.
    """
    if not isinstance(cfi, str) or not cfi:
        return None
    m = _CFI_WRAPPER.match(cfi)
    if not m:
        return None
    ranges = _top_level(m.group(1), ",")
    if ranges is None:
        return None  # an unclosed assertion, in the spine path included
    path = ranges[0] + (ranges[1] if len(ranges) > 1 else "")
    documents = _top_level(path, "!")
    if documents is None:
        return None
    if len(documents) < 2:
        return ()
    content = documents[1]
    steps: List[_Step] = []
    i, n = 0, len(content)
    while i < n:
        ch = content[i]
        if ch in ":~@":
            break  # an offset ends the path
        if ch != "/":
            return None
        j = i + 1
        while j < n and "0" <= content[j] <= "9":
            j += 1
        digits = j - i - 1
        if digits == 0 or digits > _MAX_STEP_DIGITS:
            return None
        step = int(content[i + 1:j])
        ident: Optional[str] = None
        if j < n and content[j] == "[":
            k = j + 1
            while k < n and content[k] != "]":
                k += 2 if content[k] == "^" else 1
            if k >= n:
                return None  # unclosed assertion
            ident = _assertion_id(content[j + 1:k])
            j = k + 1
        steps.append((step, ident))
        i = j
    return tuple(steps)


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
    :attr spine_index: 0-based spine position of the chapter, from the
        ``/6/N`` step as ``N // 2 - 1``. ``None`` when the CFI has no
        such step.
    :attr sort_key: Integers of the CFI's steps and character offsets
        (bracket assertions dropped; for a range, the parent path then
        the start). Tuples compare in document order, so sorting by
        ``sort_key`` gives reading order. ``None`` for anything that
        isn't an ``epubcfi(...)``; handle those before sorting.

    ``spine_index`` and ``sort_key`` are derived from ``cfi`` and are left
    out of equality, hashing and ``repr``.
    """

    cfi: str
    chapter_id: Optional[str] = field(init=False)
    char_range: Optional[Tuple[int, int]] = field(init=False)
    spine_index: Optional[int] = field(init=False, compare=False, repr=False)
    sort_key: Optional[Tuple[int, ...]] = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        chapter_id, char_range = _parse_cfi(self.cfi)
        # frozen=True requires object.__setattr__ to populate derived fields.
        object.__setattr__(self, "chapter_id", chapter_id)
        object.__setattr__(self, "char_range", char_range)
        sort_key = _cfi_sort_key(self.cfi)
        object.__setattr__(self, "sort_key", sort_key)
        object.__setattr__(self, "spine_index", _spine_index(sort_key))

    @property
    def end_sort_key(self) -> Optional[Tuple[int, ...]]:
        """The document-order key of where the location ends: for a range
        CFI, its parent path followed by its end (so a highlight's range
        lies between ``sort_key`` and ``end_sort_key``); for any other
        CFI, :attr:`sort_key`. None when :attr:`sort_key` is None.

        Computed on each access rather than stored, so a
        :class:`Location` pickled by 1.10 loads and has it too.
        """
        start = self.__dict__.get("sort_key", _UNSET)
        if start is _UNSET:  # unpickled from a version without sort_key
            start = _cfi_sort_key(self.cfi)
        if start is None:
            return None
        return _cfi_sort_key(self.cfi, end=True)

    def __bool__(self) -> bool:
        return bool(self.cfi)

    def __str__(self) -> str:
        """The raw CFI string — lets ``str(location)`` round-trip with
        anyone expecting the underlying representation."""
        return self.cfi


# -- page locations -----------------------------------------------------------

#: The largest ``ZPLUSERDATA`` blob :meth:`PageLocation.from_plist` decodes
#: (a page location is a few small values).
MAX_PAGE_LOCATION_BYTES = 64 * 1024
#: The largest page offset or ordinal :meth:`PageLocation.from_plist` accepts.
MAX_PAGE_INDEX = 10_000_000


def _page_index(value) -> bool:
    """Whether ``value`` is a plausible page offset or ordinal: an int
    (not a bool) in ``0..MAX_PAGE_INDEX``."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_PAGE_INDEX


@dataclass(frozen=True)
class PageLocation:
    """A position by page, as Apple Books records it on bookmark and
    reading-position rows (``Annotation.location_data``, a property list
    of class ``BKPageLocation``).

    :attr ordinal: the 0-based spine index (``super.ordinal``); 0 for a
        PDF, whose position is :attr:`page_offset`.
    :attr page_offset: the 0-based page (PDFs), or None when the
        property list has none.

    Read it from an annotation with :attr:`Annotation.page_location`, or
    from raw bytes with :meth:`from_plist`.
    """

    ordinal: int
    page_offset: Optional[int]

    @property
    def page(self) -> Optional[int]:
        """The 1-based page number (``page_offset + 1``), or None."""
        return None if self.page_offset is None else self.page_offset + 1

    @classmethod
    def from_plist(cls, data) -> Optional["PageLocation"]:
        """The page location a ``ZPLUSERDATA`` blob holds, or None.

        Accepts Apple's binary property list (or the XML form) of a
        dictionary with ``pageOffset`` and/or ``super: {ordinal}``; a
        missing ``super`` gives ordinal 0. Never raises: None for
        anything but bytes, for empty data or data over
        :data:`MAX_PAGE_LOCATION_BYTES`, for data that isn't a property
        list, for an ``NSKeyedArchiver`` archive, for a top level that
        isn't a dictionary (or has neither key), and for a page offset or
        ordinal that isn't an int in ``0..MAX_PAGE_INDEX``.
        """
        found = _decode_page_location(data)
        if found is None:
            return None
        location = found[0]
        return location if cls is PageLocation else cls(ordinal=location.ordinal,
                                                         page_offset=location.page_offset)


def _decode_page_location(data) -> Optional[Tuple["PageLocation", bool]]:
    """``(PageLocation.from_plist(data), whether the data records
    super.ordinal)``, or None (1.11, private): a missing ordinal reads as
    0, which for an EPUB is not data (the facade's reading position takes
    the spine item from it only when it was recorded)."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    try:
        size = memoryview(data).nbytes
    except (TypeError, ValueError):
        return None
    if not size or size > MAX_PAGE_LOCATION_BYTES:
        return None
    import plistlib  # imported on first use, not when the package is imported

    try:
        obj = plistlib.loads(bytes(data))
    except Exception:  # noqa: BLE001 - any malformed input means "no page location"
        return None
    if not isinstance(obj, dict) or "$archiver" in obj or "$objects" in obj:
        return None
    parent = obj.get("super")
    if parent is not None and not isinstance(parent, dict):
        return None
    has_offset = "pageOffset" in obj
    has_ordinal = parent is not None and "ordinal" in parent
    if not (has_offset or has_ordinal):
        return None
    page_offset = obj["pageOffset"] if has_offset else None
    ordinal = parent["ordinal"] if has_ordinal else 0
    if (has_offset and not _page_index(page_offset)) or not _page_index(ordinal):
        return None
    return PageLocation(ordinal=ordinal, page_offset=page_offset), has_ordinal
