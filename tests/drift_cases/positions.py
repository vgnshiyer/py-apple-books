"""Drift cases for stream 3.1 (positions): ``get_reading_position``,
``get_annotation_locations`` and ``get_annotation_context``.

``seed`` adds a reading-position row (CFI, fractions, page data) and a
located highlight to the finished book, and a located highlight to the
fresh book (for an inferred position), so no book row is added and the
reading book's annotations are untouched (other drift tests count
them). The seeded books have no file, so the chapter is never resolved:
the cases compare the database part of each result and the reason the
book can't be read, which every schema shape must give alike.
"""

import datetime as dt

from py_apple_books.testing import page_location_blob
from tests.drift_cases import outcome

UTC = dt.timezone.utc
POSITION_CFI = "epubcfi(/6/8[c3]!/4/2/1:5)"
HIGHLIGHT_CFI = "epubcfi(/6/4[c1]!/4/6,/1:0,/1:9)"


def seed(lib, rows: dict) -> dict:
    def asset(key):
        return lib.execute("library", "SELECT ZASSETID FROM ZBKLIBRARYASSET WHERE Z_PK = ?", (rows[key],))[0][0]

    done, fresh = asset("done"), asset("fresh")
    when = dt.datetime(2026, 9, 3, 12, tzinfo=UTC)
    return {
        "pos_bookmark": lib.add_annotation(done, None, kind="reading_position", location=POSITION_CFI,
                                           user_data=page_location_blob(4, ordinal=3), position_fraction=0.25,
                                           furthest_fraction=0.5, created=when, modified=when),
        "pos_highlight": lib.add_annotation(done, "located text", location=HIGHLIGHT_CFI, created=when),
        "pos_inferred": lib.add_annotation(fresh, "fresh text", location=HIGHLIGHT_CFI, created=when),
    }


CASES = {
    "get_reading_position": lambda api, rows: outcome(lambda: api.get_reading_position(rows["done"])),
    "get_reading_position(inferred)": lambda api, rows: outcome(lambda: api.get_reading_position(rows["fresh"])),
    "get_reading_position(none)": lambda api, rows: outcome(lambda: api.get_reading_position(rows["reading"])),
    "get_reading_position(database only)": lambda api, rows: outcome(
        lambda: api.get_reading_position(api.get_book_by_id(rows["done"]), resolve_chapter=False, infer=False)),
    "get_annotation_locations": lambda api, rows: api.get_annotation_locations(
        list(api.list_annotations(include_deleted=True, order_by="id"))),
    "get_annotation_context": lambda api, rows: outcome(lambda: api.get_annotation_context(rows["pos_highlight"])),
    "get_annotation_context(no location)": lambda api, rows: outcome(
        lambda: api.get_annotation_context(rows["done_highlight"])),
}
