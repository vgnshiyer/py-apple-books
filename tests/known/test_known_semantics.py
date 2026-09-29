"""Read-semantics defects on the demo library (stream 3.2 removes the markers).

- F25: deleted highlights and empty tombstones are listed.
- F04: the reading-status lists overlap and leave books out.
- F07: unowned Store-series rows are listed as books.
"""

import pytest

from py_apple_books.testing import seed_demo

F25 = "F25: deleted annotations and tombstones are listed; stream 3.2 (read-semantics) removes this marker"
F04 = "F04: reading-status lists overlap; stream 3.2 (read-semantics) removes this marker"
F07 = "F07: unowned Store-series rows are listed; stream 3.2 (read-semantics) removes this marker"


@pytest.fixture
def demo(library, tmp_path):
    return seed_demo(library, tmp_path)


def ids(rows) -> set:
    return {row.id for row in rows}


@pytest.mark.xfail(strict=True, reason=F25)
def test_deleted_and_tombstone_annotations_not_listed(api, demo):
    listed = ids(api.list_annotations())
    assert demo["annotations"]["highlight"] in listed
    assert demo["annotations"]["deleted"] not in listed
    assert demo["annotations"]["tombstone"] not in listed


@pytest.mark.xfail(strict=True, reason=F04)
def test_finished_book_is_not_in_progress(api, demo):
    assert demo["books"]["finished"]["id"] not in ids(api.get_books_in_progress())


@pytest.mark.xfail(strict=True, reason=F04)
def test_status_lists_partition_the_library(api, demo):
    in_progress = ids(api.get_books_in_progress())
    finished = ids(api.get_finished_books())
    unstarted = ids(api.get_unstarted_books())
    assert not (in_progress & finished or in_progress & unstarted or finished & unstarted)
    assert in_progress | finished | unstarted == ids(api.list_books())


@pytest.mark.xfail(strict=True, reason=F07)
def test_unowned_series_row_not_listed(api, demo):
    listed = ids(api.list_books())
    assert demo["books"]["series_stack"]["id"] not in listed
    assert demo["books"]["owned_series"]["id"] in listed
