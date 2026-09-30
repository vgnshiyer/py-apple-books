"""F02: relations loaded eagerly, one query per row (fixed by stream 4.1, orm)."""


def test_listing_annotations_with_books_is_a_few_statements(api, library, sql_trace):
    library.populate(books=5, annotations_per_book=20)
    annotations = list(api.list_annotations(limit=100))
    books = [a.book for a in annotations]
    assert len(annotations) == 100 and all(b is not None for b in books)
    assert len(sql_trace) <= 3, f"{len(sql_trace)} statements"
