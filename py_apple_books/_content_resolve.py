"""The :class:`~py_apple_books.content.BookContent` mixin for placing
locations in a book (private, 1.11).

Empty until its stream adds the planned method:

- ``resolve(location)``: a location's chapter and spine file, from the
  book's index (:mod:`py_apple_books._epub_index`).

Rules for code here, so the mixin and :mod:`py_apple_books.content`
don't import each other at import time:

- import :mod:`py_apple_books.content` (and :mod:`py_apple_books._epub_index`)
  inside methods, never at module level;
- no state of its own: no ``__init__``, no class attributes that hold
  data. Per-instance memos go in ``BookContent._init_runtime_state``, so
  they are dropped by pickling and copying like the others;
- no I/O at import.
"""


class _ResolveMixin:
    """Private mixin of :class:`~py_apple_books.content.BookContent`."""

    __slots__ = ()
