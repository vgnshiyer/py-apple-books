"""F08: SQL built by string interpolation (fixed in 1.10 by stream 2.1).

A search containing an apostrophe failed with a syntax error, and ``%``
and ``_`` acted as LIKE wildcards instead of matching themselves.
"""

import pytest


@pytest.fixture
def seeded(library):
    return {
        "apostrophe": library.add_book("Don't Panic"),
        "percent": library.add_book("100% Proof"),
        "plain": library.add_book("Plain Title"),
    }


def test_title_search_with_apostrophe_finds_the_book(api, seeded):
    assert {b.id for b in api.get_book_by_title("Don't")} == {seeded["apostrophe"]["id"]}


def test_annotation_search_with_apostrophe_finds_the_row(api, library, seeded):
    anno = library.add_annotation(seeded["apostrophe"], "Don't panic.")
    assert anno in {a.id for a in api.search_annotation_by_text("Don't", limit=None)}


def test_percent_is_literal(api, seeded):
    assert {b.id for b in api.get_book_by_title("%")} == {seeded["percent"]["id"]}
