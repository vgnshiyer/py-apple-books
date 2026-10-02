"""Drift cases for stream 2.3 (search): ``search_annotations`` (ranked,
on the private index) and ``search_books`` (every word in the title or
the author). Hits are projected to ids, tiers and methods (scores are
floats of the ranking function and ``AnnotationHit`` compares by
identity)."""


def hits(result) -> list:
    return [(h.annotation.id, h.matched_all, str(h.method), getattr(h.annotation.book, "id", None))
            for h in result]


def books(result) -> list:
    return sorted((b.id, b.title) for b in result)


CASES = {
    "search_annotations": lambda api, rows: hits(api.search_annotations("highlight", limit=None)),
    "search_annotations(book, deleted)": lambda api, rows: hits(api.search_annotations(
        "noted highlight deleted", book_id=rows["reading"], include_deleted=True)),
    "search_annotations(require_all)": lambda api, rows: hits(api.search_annotations(
        "done highlight", require_all=True)),
    "search_books": lambda api, rows: books(api.search_books("book")),
    "search_books(all)": lambda api, rows: books(api.search_books("vol", include_store_series=True)),
}
