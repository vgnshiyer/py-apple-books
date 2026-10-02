"""Drift cases for stream 1.1 (content safety): ``get_book_content`` with
a ``Book`` argument (new in 1.11). The seeded books have no file, so the
call fails the same way on every schema, whether or not the store has the
columns the iCloud gate reads (``ZSTATE``)."""

from tests.drift_cases import outcome

CASES = {
    "get_book_content(book)": lambda api, rows: outcome(
        lambda: api.get_book_content(api.get_book_by_id(rows["reading"]))),
}
