"""Chapter spans (private, 1.11): the text of a table-of-contents entry
through the files that follow it in reading order, for
``BookContent.get_chapter(chapter_id, span='section' | 'chapter')``.

``span='file'`` is 1.10's text: an entry's own file, cut where another
entry of the same file begins. The span modes follow the book's reading
order instead:

* The **reading order** is the package document's spine, its entries
  marked ``linear="no"`` and the entries naming no manifest item left
  out, each file at its first place only (a file the spine lists again
  is not read again, so repeated entries can't multiply the work). It
  is built once per :class:`~py_apple_books.content.BookContent`
  (:func:`build_plan`) from the book the instance has loaded; spans read
  the items ebooklib loaded with it, never a file.
* Every table-of-contents entry whose file is in the reading order
  **begins** at a place in it: its file, at the element its fragment
  names (an ``id``, else an ``<a name>``), or at the start of the file
  when it has no fragment. An element that comes before any text of
  its file counts as the start of the file. Entries that begin at the
  same place (the same element, or the start of a file) are ordered by
  their table-of-contents order.
* A span starts where its entry begins and ends where the next entry
  begins (``'section'``: an entry of any depth; ``'chapter'``: one whose
  depth is at most the entry's own), or at the end of the reading
  order. An entry followed at the same place by another (a part heading
  whose first chapter starts in the same file, say) has no text of its
  own in ``'section'`` mode: its span is empty.
* A fragment that names no element in its file leaves the entry's
  place in that file unknown. As a start, the span starts at the start
  of the file, and of the other entries in that file only those later
  in the table of contents can end it. As an end, such an entry ends a
  span at the start of its file when that file comes after the span's
  first file, and does not end it inside the span's first file.
* A manifest id that isn't a table-of-contents id starts at the start of
  its file and ends at the next entry of any depth after that, in
  either mode (it has no depth of its own); entries that begin at the
  very start of the file don't end it.

The text of each file in the span is extracted as 1.10 extracts one
file (:func:`py_apple_books.utils._walk`, whitespace tidied per file),
and the files' texts are joined with a blank line.
"""

from __future__ import annotations

import posixpath
from typing import Any, Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from bs4 import Tag

from py_apple_books.exceptions import InvalidChoiceError
from py_apple_books.utils import _TEXT_STRING_TYPES, _anchor_index, _parse, _walk, normalize_whitespace

#: The values ``get_chapter``'s ``span`` takes.
SPANS: Tuple[str, ...] = ("file", "section", "chapter")


def check_span(span: Any) -> str:
    """``span``, checked: one of :data:`SPANS`.

    :raises InvalidChoiceError: anything else (case matters).
    """
    if isinstance(span, str) and span in SPANS:
        return span
    shown = span if isinstance(span, str) and len(span) <= 40 else type(span).__name__
    raise InvalidChoiceError(f"Unknown span {shown!r}. Valid spans: {', '.join(SPANS)}.",
                             value=span, valid=SPANS)


def is_order(chapter_id: str, count: int) -> Optional[int]:
    """The order ``chapter_id`` names, when it is a chapter's 1-based
    order written in ASCII digits without leading zeros (as
    :attr:`Chapter.order` prints) and at most ``count``; else None."""
    if not (chapter_id.isascii() and chapter_id.isdigit()) or len(chapter_id) > 9:
        return None
    n = int(chapter_id)
    if str(n) != chapter_id or not 1 <= n <= count:
        return None
    return n


def norm_href(href: str) -> Optional[str]:
    """A bundle-relative href as the reading order keys it; None for an
    empty one (an entry that names no file)."""
    if not href:
        return None
    return posixpath.normpath(href)


class Step(NamedTuple):
    """One file of the reading order."""

    href: str
    item: Any
    has_text: bool


class Plan(NamedTuple):
    """What spans need from a loaded book, built once per instance.

    :param items: manifest id -> the ebooklib item with that id (the
        first, as ``EpubBook.get_item_with_id`` finds it).
    :param steps: the reading order (see the module docstring).
    :param step_of_href: normalized href -> its index in ``steps``.
    :param step_of_id: manifest id -> the index in ``steps`` of its first
        linear spine entry.
    """

    items: Mapping[str, Any]
    steps: Tuple[Step, ...]
    step_of_href: Mapping[str, int]
    step_of_id: Mapping[str, int]


def build_plan(items: Iterable[Any], spine: Iterable[Tuple[Optional[str], bool]],
               rel: Callable[[str], str], has_text: Callable[[Any, str], bool]) -> Plan:
    """The :class:`Plan` of a loaded book.

    :param items: the book's ebooklib items, in manifest order.
    :param spine: ``(idref, linear)`` per spine element, in order
        (``idref`` None for an element that names none).
    :param rel: OPF-relative file name -> bundle-relative href.
    :param has_text: ``(item, href)`` -> whether its text can be
        extracted (a text media type, inside the bundle).
    """
    by_id: Dict[str, Any] = {}
    for item in items:
        item_id = item.get_id()
        if isinstance(item_id, str):
            by_id.setdefault(item_id, item)
    steps: List[Step] = []
    step_of_href: Dict[str, int] = {}
    step_of_id: Dict[str, int] = {}
    for idref, linear in spine:
        if not linear or not idref:
            continue
        item = by_id.get(idref)
        name = getattr(item, "file_name", None) if item is not None else None
        href = norm_href(rel(name)) if name else None
        if href is None:
            continue  # a broken entry
        index = step_of_href.get(href)
        if index is None:
            index = step_of_href[href] = len(steps)
            steps.append(Step(href, item, bool(has_text(item, href))))
        step_of_id.setdefault(idref, index)
    return Plan(by_id, tuple(steps), step_of_href, step_of_id)


class Start(NamedTuple):
    """Where a span starts: a step of the reading order, the fragment
    (``""`` for the start of the file), and the entry's table-of-contents
    order and depth (None for a manifest id: no entry at the start of
    the file ends the span, and every later entry does)."""

    step: int
    fragment: str
    order: Optional[int]
    depth: Optional[int]


def _find(ids: Mapping[str, Any], names: Mapping[str, Any], fragment: str) -> Any:
    """The element a fragment names (an ``id``, else an ``<a name>``), or
    None for no fragment or none found (the start of the file)."""
    if not fragment:
        return None
    el = ids.get(fragment)
    return el if el is not None else names.get(fragment)


def _leading_elements(soup: Any) -> Set[int]:
    """The ``id()`` of every element of a parsed file that begins before
    its first text (in ``<body>``, or the whole document without one):
    an entry anchored at one of them begins, like an entry without a
    fragment, at the start of the file's text."""
    top = soup.body if soup.body is not None else soup
    found: Set[int] = set()
    for node in top.descendants:
        if isinstance(node, Tag):
            found.add(id(node))
        elif type(node) in _TEXT_STRING_TYPES and node.strip():
            break
    return found


def _ends_at_start(entry: Any, start: Start) -> bool:
    """For an entry that begins exactly where the span starts: whether it
    ends the span there (it comes later in the table of contents)."""
    return start.order is not None and entry.order > start.order


def span_text(plan: Plan, chapters: Sequence[Any], start: Start, mode: str,
              read: Callable[[Step], bytes]) -> str:
    """The text of the span from ``start`` (see the module docstring).

    :param chapters: the book's ``list_chapters()``.
    :param mode: ``'section'`` or ``'chapter'``.
    :param read: the bytes of a step's item (each step is read at most
        once).
    """
    # The entries that can end the span, by the step they begin in.
    ends: Dict[int, List[Any]] = {}
    for c in chapters:
        if c.order == start.order:
            continue
        if mode == "chapter" and start.depth is not None and c.depth > start.depth:
            continue
        href = norm_href(c.href)
        step = plan.step_of_href.get(href) if href is not None else None
        if step is not None and step >= start.step:
            ends.setdefault(step, []).append(c)

    pieces: List[str] = []

    def finish() -> str:
        return "\n\n".join(p for p in pieces if p)

    # The start file: from the start's element (or the file's start) on.
    here = ends.get(start.step, ())
    step = plan.steps[start.step]
    if not step.has_text:
        # Nothing in it can be found: only entries without a fragment
        # have a known place (its start).
        if not start.fragment and any(not c.fragment and _ends_at_start(c, start) for c in here):
            return ""
    else:
        soup = _parse(read(step))
        ids, names = _anchor_index(soup) if (start.fragment or any(c.fragment for c in here)) else ({}, {})
        start_el = _find(ids, names, start.fragment)
        # A start whose fragment names nothing begins at the start of the
        # file, at a place in it that is not known.
        unknown = bool(start.fragment) and start_el is None
        top_els = _leading_elements(soup) if (start.fragment or any(c.fragment for c in here)) else set()
        start_top = start_el is None or id(start_el) in top_els
        stop_ids = set()
        for c in here:
            el = _find(ids, names, c.fragment) if c.fragment else None
            if c.fragment and el is None:
                continue  # its place in this file is not known
            if unknown:
                # Only entries later in the table of contents, at a known
                # element, can end it.
                if el is not None and c.order > start.order:
                    stop_ids.add(id(el))
                continue
            c_top = el is None or id(el) in top_els
            if start_top and c_top or el is start_el:
                # The same place (no text between them): the entry later in
                # the table of contents begins there, the other is empty.
                if _ends_at_start(c, start):
                    return ""
                continue
            if c_top:
                continue  # before the start
            # Elements before the start are never reached by the walk.
            stop_ids.add(id(el))
        top = soup if start_el is not None else (soup.body if soup.body is not None else soup)
        text, stopped = _walk(top, start_el, (lambda tag: id(tag) in stop_ids) if stop_ids else None)
        pieces.append(normalize_whitespace(text))
        if stopped:
            return finish()

    # The files after it, up to the first entry that ends the span.
    for index in range(start.step + 1, len(plan.steps)):
        step = plan.steps[index]
        here = ends.get(index, ())
        if here and (not step.has_text or any(not c.fragment for c in here)):
            break  # an entry begins at the start of this file
        if not step.has_text:
            continue
        soup = _parse(read(step))
        stop_ids = set()
        if here:
            ids, names = _anchor_index(soup)
            found = [_find(ids, names, c.fragment) for c in here]
            if any(el is None for el in found):
                break  # an entry whose element is missing begins at the start
            stop_ids = {id(el) for el in found}
        top = soup.body if soup.body is not None else soup
        text, stopped = _walk(top, None, (lambda tag: id(tag) in stop_ids) if stop_ids else None)
        pieces.append(normalize_whitespace(text))
        if stopped:
            break
    return finish()
