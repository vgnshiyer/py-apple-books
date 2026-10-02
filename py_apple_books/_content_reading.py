"""The :class:`~py_apple_books.content.BookContent` mixin for spoiler-safe
reading (private, 1.11).

Empty until its streams add the planned methods:

- ``search(query, ...)``: search the book's text up to a boundary;
- ``resolve_boundary(boundary, *, precision='item')``: place a
  ``ReadBoundary`` in the book's text;
- ``position_at_percent(percent)``;
- later, exact in-chapter positions (``positions_of``, ``position_of``,
  ``toc_positions``, ``chapter_at``, ``toc_before``).

They read the book's text through ``BookContent.iter_spine_text`` and
``BookContent.get_spine_item_text``. The rules of
:mod:`py_apple_books._content_resolve` apply here too: import
:mod:`py_apple_books.content` inside methods only, keep no state of the
mixin's own, and do no I/O at import.
"""


class _ReadingMixin:
    """Private mixin of :class:`~py_apple_books.content.BookContent`."""

    __slots__ = ()
