"""The :class:`~py_apple_books.content.BookContent` mixin for placing
locations in a book (private, 1.11).

- ``resolve(location)``: a location's chapter and spine file, from the
  book's index (:mod:`py_apple_books._epub_index`).

The facade's reading-position and annotation-location methods
(``py_apple_books._api.positions``) use the same functions with the
index a book gate returned: :func:`_target_of`, :class:`_Boundaries`
and :func:`_resolve`.

How a location is placed (the rules :class:`~py_apple_books.positions.ChapterMatch`
documents):

- Its spine file: the item its CFI's bracket hint names when that is a
  manifest id in the spine (the spine step's item when it names the same
  id, else the first such item), else the item at its spine step. None
  when neither names an item of this spine.
- Every ToC entry whose file is in the spine starts a section at
  ``(spine index of its file, start path, ToC order)``. The start path
  is ``()`` (the start of the file) when the file holds only that
  entry, the entry has no fragment, or the fragment's anchor isn't in
  the file; else the anchor's element path (its CFI steps,
  ``_epub_index._anchor_table``).
- The location belongs to the last section starting at or before it.
  Its own path is the CFI's content path, re-based on the deepest step
  whose id assertion names an anchor of the file (that anchor's path,
  then the steps after it).
- A file whose entries' start paths would hold more than
  :data:`_MAX_START_STEPS` steps together (a crafted file of many deeply
  nested ToC anchors) is treated like one whose anchors can't be read:
  ``SECTION_UNKNOWN``. Each file's starts are built once per
  :class:`_Boundaries` and sorted, so placing a location costs a binary
  search.

Reads: nothing but the index and, for a file holding several ToC
entries with fragments (the location's, or the file before it), that
file's anchor table, cached process-wide by the index cache. Nothing
new is cached here.

Rules for code here, so the mixin and :mod:`py_apple_books.content`
don't import each other at import time:

- import :mod:`py_apple_books.content` (and :mod:`py_apple_books._epub_index`)
  inside methods, never at module level;
- no state of its own: no ``__init__``, no class attributes that hold
  data. Per-instance memos go in ``BookContent._init_runtime_state``, so
  they are dropped by pickling and copying like the others;
- no I/O at import.
"""

from __future__ import annotations

import bisect
import posixpath
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, NamedTuple, Optional, Tuple, Union

from py_apple_books.exceptions import InvalidArgumentError
from py_apple_books.positions import ChapterMatch, ResolvedLocation

if TYPE_CHECKING:
    from py_apple_books.content import Chapter
    from py_apple_books.models.location import Location

#: Whether locations in a file holding several ToC entries are placed by
#: comparing their CFI with the entries' anchors (``ChapterMatch.ANCHOR``).
#: Off, every such location gets ``SECTION_UNKNOWN`` (no chapter, spine
#: file still known): the cut line of owner question Q4, for a release
#: whose real-library accuracy check fails. Read on each call.
_ANCHOR_MATCHING = True

# A content path: CFI steps (ints) from the root element of the file.
_Path = Tuple[int, ...]

#: The most element steps the start paths of one file's ToC entries may
#: hold together (the sum of their anchors' depths). Over it, locations in
#: that file are ``SECTION_UNKNOWN``, never "file start": this bounds the
#: time and memory of placing a location in a crafted file whose many ToC
#: anchors are all deeply nested (each path is built in steps proportional
#: to its depth). A real book's file stays far below it.
_MAX_START_STEPS = 1_000_000


class _Target(NamedTuple):
    """What :func:`_resolve` needs from a location.

    :attr spine_step: the spine index its spine step names (for an int
        location, that int), or None.
    :attr hint: the manifest id of its bracket hint, or None.
    :attr steps: its content path (``models.location._content_path``):
        ``()`` for the start of the file, None when unknown (a
        malformed content path).
    """

    spine_step: Optional[int]
    hint: Optional[str]
    steps: Optional[Tuple[Tuple[int, Optional[str]], ...]]


_NOWHERE = _Target(None, None, ())


def _target_of(location: Any) -> _Target:
    """The :class:`_Target` of a ``resolve()`` argument: a
    :class:`~py_apple_books.models.location.Location`, a CFI string or
    an int spine index (the start of that item). A negative int, or a
    CFI with neither a spine step nor a bracket hint, gives a target
    that names no item; a CFI whose content path is malformed keeps its
    spine step and hint, with ``steps`` None.

    :raises InvalidArgumentError: anything else (a bool included).
    """
    from py_apple_books.models.location import Location, _content_path
    from py_apple_books.positions import _spine_index

    if isinstance(location, bool):
        raise InvalidArgumentError(
            "location must be a Location, a CFI string or a spine index (int), not a bool.")
    if isinstance(location, int):
        return _Target(int(location), None, ()) if location >= 0 else _NOWHERE
    if isinstance(location, str):
        location = Location(location)
    if not isinstance(location, Location):
        raise InvalidArgumentError(
            f"location must be a Location, a CFI string or a spine index (int), "
            f"not {type(location).__name__}.")
    hint = getattr(location, "chapter_id", None)
    return _Target(_spine_index(location), hint if isinstance(hint, str) else None,
                   _content_path(location.cfi))


def _usable(location: Any) -> bool:
    """Whether a location (an annotation's) can name a spine file: a
    :class:`Location` whose CFI starts with a spine step or carries a
    bracket hint."""
    from py_apple_books.models.location import Location
    from py_apple_books.positions import _spine_index

    if not isinstance(location, Location) or not location:
        return False
    return _spine_index(location) is not None or bool(getattr(location, "chapter_id", None))


def _norm(href: Optional[str]) -> Optional[str]:
    return posixpath.normpath(href) if href else None


class _Start(NamedTuple):
    path: _Path
    order: int
    chapter: "Chapter"


class _Sections(NamedTuple):
    """Where the ToC entries of one file start, sorted by ``(path,
    order)``, with their paths alone (for bisecting) and the file's anchor
    table (None when no entry has a fragment, so none was read)."""

    starts: List[_Start]
    paths: List[_Path]
    table: Optional[Mapping[str, _Path]]


class _Boundaries:
    """The section boundaries of one book index, built once per call
    (or per book, for a batch): ToC entries grouped by their file, and
    the files holding entries by spine index. Holds anchor tables read
    during the call, so a batch reads each one once."""

    __slots__ = ("index", "root", "by_file", "starts_at", "files_at", "_tables", "_sections",
                 "_anchor_paths", "_spine_ids")

    def __init__(self, index: Any, root: Any) -> None:
        self.index = index
        self.root = root
        by_file: Dict[str, List["Chapter"]] = {}
        files_at: Dict[int, str] = {}
        for chapter in index.chapters:
            if chapter.spine_index is None or not chapter.href:
                continue
            file = posixpath.normpath(chapter.href)
            by_file.setdefault(file, []).append(chapter)
            files_at.setdefault(chapter.spine_index, file)
        self.by_file: Dict[str, Tuple["Chapter", ...]] = {f: tuple(c) for f, c in by_file.items()}
        self.starts_at: List[int] = sorted(files_at)
        self.files_at = files_at
        self._tables: Dict[str, Optional[Mapping[str, _Path]]] = {}
        self._sections: Dict[str, Optional[_Sections]] = {}
        self._anchor_paths: Dict[Tuple[str, str], _Path] = {}
        self._spine_ids: Optional[Dict[str, int]] = None

    # -- the file ---------------------------------------------------------------

    def spine_position(self, target: _Target) -> Optional[int]:
        """The spine index of the target's file, or None (see the module
        docstring)."""
        spine = self.index.spine
        step = target.spine_step
        in_range = step is not None and 0 <= step < len(spine)
        if target.hint is not None:
            if in_range and spine[step].item_id == target.hint:
                return step
            if self._spine_ids is None:
                ids: Dict[str, int] = {}
                for item in spine:
                    if item.item_id is not None:
                        ids.setdefault(item.item_id, item.index)
                self._spine_ids = ids
            found = self._spine_ids.get(target.hint)
            if found is not None:
                return found
        return step if in_range else None

    # -- anchors ----------------------------------------------------------------

    def table(self, file: str) -> Optional[Mapping[str, _Path]]:
        """The anchor table of ``file`` (bundle-relative), or None when it
        can't be had: too large, unreadable, not downloaded, unparseable.
        Read once per instance."""
        if file in self._tables:
            return self._tables[file]
        from py_apple_books import _epub_index

        try:
            found = _epub_index._anchor_table_for(self.root, file)
        except Exception:  # noqa: BLE001 - any failure: the section is unknown, never "file start"
            found = None
        self._tables[file] = found
        return found

    def sections(self, file: str) -> Optional[_Sections]:
        """Where each ToC entry of ``file`` starts (see :class:`_Sections`);
        None when an entry has a fragment and the file's anchor table
        can't be had, or when the entries' start paths would hold more
        than :data:`_MAX_START_STEPS` steps. Built once per instance, so
        a batch builds each file's once."""
        if file in self._sections:
            return self._sections[file]
        found = self._build_sections(file)
        self._sections[file] = found
        return found

    def _build_sections(self, file: str) -> Optional[_Sections]:
        entries = self.by_file.get(file, ())
        table = None
        if any(c.fragment for c in entries):
            table = self.table(file)
            if table is None:
                return None
        starts = []
        budget = _MAX_START_STEPS
        for c in entries:
            path: _Path = ()
            if table is not None and c.fragment and c.fragment in table:
                path = table[c.fragment]
                budget -= len(path)
                if budget < 0:
                    return None
            starts.append(_Start(path, c.order, c))
        starts.sort(key=lambda s: (s.path, s.order))
        return _Sections(starts, [s.path for s in starts], table)

    def anchor_path(self, file: str, table: Mapping[str, _Path], anchor: str) -> _Path:
        """``table[anchor]`` for ``file``'s table, kept for the instance
        (a batch re-bases many locations on the same anchors)."""
        key = (file, anchor)
        path = self._anchor_paths.get(key)
        if path is None:
            path = self._anchor_paths[key] = tuple(table[anchor])
        return path

    # -- placing ----------------------------------------------------------------

    def place(self, position: int,
              steps: Optional[Tuple[Tuple[int, Optional[str]], ...]]
              ) -> Tuple[Optional["Chapter"], ChapterMatch]:
        """The chapter and match of a location at spine index
        ``position`` with content path ``steps``."""
        item = self.index.spine[position]
        file = _norm(item.href)
        entries = self.by_file.get(file, ()) if file else ()
        if not entries:
            return self.preceding(position)
        if len(entries) == 1:
            return entries[0], ChapterMatch.FILE
        if not _ANCHOR_MATCHING:
            return None, ChapterMatch.SECTION_UNKNOWN
        sections = self.sections(file)
        if sections is None:
            return None, ChapterMatch.SECTION_UNKNOWN
        if steps is None:
            # Where the location is in its file is unknown: only a file
            # whose entries all start at its top can be placed (the last
            # of them in ToC order).
            if sections.paths[-1] == ():
                return sections.starts[-1].chapter, ChapterMatch.ANCHOR
            return None, ChapterMatch.SECTION_UNKNOWN
        path = self.rebased(file, steps, sections.table)
        # The last entry starting at or before the location: of those
        # starting at the same place, the last in ToC order.
        at = bisect.bisect_right(sections.paths, path)
        if at:
            return sections.starts[at - 1].chapter, ChapterMatch.ANCHOR
        # Before every entry of its file: the section before the file (its
        # first place in the spine, where its sections start, for a file
        # the spine lists more than once).
        return self.preceding(self.index.first_spine_index.get(file, position))

    def rebased(self, file: str, steps: Tuple[Tuple[int, Optional[str]], ...],
                table: Optional[Mapping[str, _Path]]) -> _Path:
        """The integer path of ``steps``, re-based on the deepest even
        step whose id assertion names an anchor in ``file``'s ``table``:
        that anchor's path, then the steps after it (so a CFI whose
        element counting differs from the parser's still lands on its
        element)."""
        if table:
            for k in range(len(steps) - 1, -1, -1):
                step, ident = steps[k]
                if ident is not None and step % 2 == 0 and ident in table:
                    return self.anchor_path(file, table, ident) + tuple(s for s, _ in steps[k + 1:])
        return tuple(s for s, _ in steps)

    def preceding(self, file_index: int) -> Tuple[Optional["Chapter"], ChapterMatch]:
        """The last section starting in a file before spine index
        ``file_index`` (``PRECEDING``), ``FRONT_MATTER`` when there is
        none, or ``SECTION_UNKNOWN`` when that file holds several entries
        whose order in the file can't be read."""
        at = bisect.bisect_left(self.starts_at, file_index) - 1
        if at < 0:
            return None, ChapterMatch.FRONT_MATTER
        file = self.files_at[self.starts_at[at]]
        entries = self.by_file[file]
        if len(entries) == 1:
            return entries[0], ChapterMatch.PRECEDING
        sections = self.sections(file)
        if sections is None:
            return None, ChapterMatch.SECTION_UNKNOWN
        return sections.starts[-1].chapter, ChapterMatch.PRECEDING

    def resolve(self, target: _Target) -> Optional[ResolvedLocation]:
        """The :class:`ResolvedLocation` of ``target``, or None when it
        names no item of this spine."""
        position = self.spine_position(target)
        if position is None:
            return None
        chapter, match = self.place(position, target.steps)
        return ResolvedLocation(chapter, match, position, self.index.spine[position].item_id)


def _resolve(index: Any, root: Any, target: _Target) -> Optional[ResolvedLocation]:
    """:meth:`_ResolveMixin.resolve` on a book's index (gated by the
    caller) and bundle path."""
    return _Boundaries(index, root).resolve(target)


class _ResolveMixin:
    """Private mixin of :class:`~py_apple_books.content.BookContent`."""

    __slots__ = ()

    def resolve(self, location: Union["Location", str, int]) -> Optional[ResolvedLocation]:
        """Where ``location`` is in this book: its spine file and the
        table-of-contents entry it belongs to (1.11).

        :param location: A :class:`~py_apple_books.models.location.Location`
            (such as ``Annotation.location``), a CFI string, or an int: a
            0-based spine index, standing for the start of that item (a
            bookmark recorded by spine item, without a CFI).
        :return: A :class:`~py_apple_books.positions.ResolvedLocation`:
            the chapter (the last ToC entry starting at or before the
            location; see :class:`~py_apple_books.positions.ChapterMatch`
            for how it was chosen), the spine index and the manifest id
            of the location's file. None when the location names no item
            of this book's spine: a negative index, or a CFI whose spine
            step and bracket hint name none (a CFI that isn't one
            included). A CFI whose path inside the file is malformed
            still gives its file, with the match ``SECTION_UNKNOWN`` when
            the file holds several entries. Never raises for a malformed
            CFI.

        The file is the one the CFI's bracket hint names, when that is a
        manifest id in the spine; else the one at its spine step. A
        location in a file holding several ToC entries is compared with
        where each entry's anchor is in the file (``ANCHOR``); when that
        can't be done (the file is too large, unreadable or not
        downloaded), the match is ``SECTION_UNKNOWN`` and the chapter
        None, never the first entry of the file.

        Reads the book's index (container, package and navigation files,
        cached process-wide) and, only for a file holding several ToC
        entries with fragments, that file's anchors (cached too); no
        chapter text.

        :raises InvalidArgumentError: ``location`` is none of the above
            (a bool included).
        :raises NotEpubError: the book is not an EPUB bundle.
        :raises BookNotDownloadedError: the book's package or navigation
            files are stored only in iCloud (nothing is downloaded).
        :raises DRMProtectedError: the book is DRM-protected.
        :raises AppleBooksError: the book's package can't be read.
        """
        target = _target_of(location)
        index = self._gated_index()  # type: ignore[attr-defined]
        return _resolve(index, self.path, target)  # type: ignore[attr-defined]
