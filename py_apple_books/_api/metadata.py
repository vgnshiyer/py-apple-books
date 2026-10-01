"""The :class:`~py_apple_books.PyAppleBooks` mixin for book metadata (library first, then the
book's OPF) and series.

Empty until its stream adds the planned methods:

- ``get_book_metadata``
- ``get_series``
- ``list_series``
- ``get_books_by_subject``

See ``py_apple_books._api`` for the rules mixin code follows.
"""


class _MetadataAPI:
    """Private mixin of :class:`~py_apple_books.PyAppleBooks`."""
