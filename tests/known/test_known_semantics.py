"""Read-semantics defects on the demo library (fixed in 1.10 by stream 3.2).

- F25: deleted highlights and empty tombstones were listed.
- F04: the reading-status lists overlapped and left books out.
- F07: unowned Store-series rows were listed as books.
"""

import pytest

from py_apple_books.testing import seed_demo


@pytest.fixture
def demo(library, tmp_path):
    return seed_demo(library, tmp_path)


def ids(rows) -> set:
    return {row.id for row in rows}


def test_deleted_and_tombstone_annotations_not_listed(api, demo):
    listed = ids(api.list_annotations())
    assert demo["annotations"]["highlight"] in listed
    assert demo["annotations"]["deleted"] not in listed
    assert demo["annotations"]["tombstone"] not in listed


def test_finished_book_is_not_in_progress(api, demo):
    assert demo["books"]["finished"]["id"] not in ids(api.get_books_in_progress())


def test_status_lists_partition_the_library(api, demo):
    in_progress = ids(api.get_books_in_progress())
    finished = ids(api.get_finished_books())
    unstarted = ids(api.get_unstarted_books())
    assert not (in_progress & finished or in_progress & unstarted or finished & unstarted)
    assert in_progress | finished | unstarted == ids(api.list_books())


def test_unowned_series_row_not_listed(api, demo):
    listed = ids(api.list_books())
    assert demo["books"]["series_stack"]["id"] not in listed
    assert demo["books"]["owned_series"]["id"] in listed
