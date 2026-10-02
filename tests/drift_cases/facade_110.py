"""Drift cases for the 1.10 facade methods (every method that existed
in 1.10.0). The first entries are ``test_schema_drift.everything()`` as
it was in 1.10; the counts, stats and content calls were added in 1.11.
"""

import datetime as dt

from tests.drift_cases import outcome


def ids(rows) -> list:
    return sorted(row.id for row in rows)


def books(result) -> list:
    return [(b.id, b.title, ids(b.annotations), ids(b.collections)) for b in result]


def annotations(result) -> list:
    return [(a.id, a.selected_text, getattr(a.book, "id", None)) for a in result]


AFTER = dt.datetime(2026, 1, 1)

CASES = {
    "list_collections": lambda api, rows: [(c.id, ids(c.books)) for c in api.list_collections()],
    "get_collection_by_id": lambda api, rows: ids(api.get_collection_by_id(rows["shelf"]).books),
    "get_collection_by_title": lambda api, rows: [c.id for c in api.get_collection_by_title("shel")],
    "list_books": lambda api, rows: books(api.list_books()),
    "list_books(all)": lambda api, rows: books(api.list_books(include_store_series=True)),
    "get_book_by_id": lambda api, rows: books([api.get_book_by_id(rows["reading"])]),
    "get_book_by_title": lambda api, rows: books(api.get_book_by_title("book")),
    "get_books_by_genre": lambda api, rows: books(api.get_books_by_genre("fic", limit=10)),
    "list_annotations": lambda api, rows: annotations(api.list_annotations(order_by="-creation_date")),
    "list_annotations(deleted)": lambda api, rows: annotations(api.list_annotations(include_deleted=True)),
    "get_annotation_by_id": lambda api, rows: annotations([api.get_annotation_by_id(rows["note"])]),
    "get_annotations_by_color": lambda api, rows: annotations(api.get_annotations_by_color("yellow")),
    "search_annotation_by_highlighted_text": lambda api, rows: annotations(
        api.search_annotation_by_highlighted_text("highlight")),
    "search_annotation_by_note": lambda api, rows: annotations(api.search_annotation_by_note("note")),
    "search_annotation_by_text": lambda api, rows: annotations(api.search_annotation_by_text("text")),
    "get_annotations_by_date_range": lambda api, rows: annotations(
        api.get_annotations_by_date_range(after=AFTER)),
    "get_books_in_progress": lambda api, rows: books(api.get_books_in_progress(order_by="-last_opened_date")),
    "get_finished_books": lambda api, rows: books(api.get_finished_books()),
    "get_unstarted_books": lambda api, rows: books(api.get_unstarted_books()),
    "get_recently_read_books": lambda api, rows: books(api.get_recently_read_books(limit=10)),
    "get_recently_read_books(opened)": lambda api, rows: books(
        api.get_recently_read_books(order_by="-last_opened_date")),
    "get_current_reading_location": lambda api, rows: getattr(
        api.get_current_reading_location(rows["reading"]), "id", None),
    "get_annotation_surrounding_text": lambda api, rows: api.get_annotation_surrounding_text(rows["highlight"]),
    # Added in 1.11: the 1.10 read methods everything() didn't call.
    "count_books_by_status": lambda api, rows: {str(k): n for k, n in api.count_books_by_status().items()},
    "count_annotations": lambda api, rows: (api.count_annotations(), api.count_annotations(rows["reading"])),
    "get_library_stats": lambda api, rows: api.get_library_stats(),
    # The seeded books have no file: the same refusal on every schema.
    "get_book_content": lambda api, rows: outcome(lambda: api.get_book_content(rows["reading"])),
    "get_current_reading_chapter": lambda api, rows: outcome(
        lambda: getattr(api.get_current_reading_chapter(rows["reading"]), "id", None)),
}

EXEMPT = {
    "close": "runs no query",
    "query_deadline": "runs no query",
    "store_info": "reports the drift itself (missing_columns); see test_store_info",
    "create_collection": "a write; see test_collection_writer.test_schema_drift_aborts",
    "rename_collection": "a write; see test_collection_writer.test_schema_drift_aborts",
    "delete_collection": "a write; see test_collection_writer.test_schema_drift_aborts",
    "add_book_to_collection": "a write; see test_collection_writer.test_schema_drift_aborts",
    "remove_book_from_collection": "a write; see test_collection_writer.test_schema_drift_aborts",
}
