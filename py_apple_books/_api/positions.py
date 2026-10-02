"""The :class:`~py_apple_books.PyAppleBooks` mixin for where the reader is (reading positions,
annotation locations, annotation context).

Empty until its stream adds the planned methods:

- ``get_reading_position``
- ``get_annotation_locations``
- ``get_annotation_context``

See ``py_apple_books._api`` for the rules mixin code follows.
"""


class _PositionsAPI:
    """Private mixin of :class:`~py_apple_books.PyAppleBooks`."""
