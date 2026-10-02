"""Drift cases for stream 3.3 (reading boundary): ``get_read_boundary``
on the seeded "reading" book, with both bases. The seed gives its
reading-position row and its highlight CFIs into the spine and its
furthest point a value past its progress (in place: the seeded book and
row lists, which other tests assert on, are unchanged).

The columns the boundary reads are in every store schema the suite
drifts to (the 2023 shape included), so the result is the same on all;
``tests/test_read_boundary.py`` drifts those columns themselves."""

BOOKMARK = "epubcfi(/6/6[c2]!/4/2/1:0)"
HIGHLIGHT = "epubcfi(/6/8[c3]!/4/2,/1:0,/1:9)"


def seed(lib, rows: dict) -> dict:
    for row, cfi in ((rows["position"], BOOKMARK), (rows["highlight"], HIGHLIGHT)):
        lib.execute("annotations", "UPDATE ZAEANNOTATION SET ZANNOTATIONLOCATION = ? WHERE Z_PK = ?", (cfi, row))
    lib.execute("library", "UPDATE ZBKLIBRARYASSET SET ZBOOKHIGHWATERMARKPROGRESS = 0.6 WHERE Z_PK = ?",
                (rows["reading"],))
    return None


def boundary(result) -> tuple:
    """A ReadBoundary as plain values."""
    return (result.book_id, result.basis, str(result.source),
            result.bookmark.cfi if result.bookmark else None,
            result.highlight.cfi if result.highlight else None,
            result.progress, result.high_water, result.is_finished,
            tuple(str(w) for w in result.warnings))


CASES = {
    "get_read_boundary": lambda api, rows: boundary(api.get_read_boundary(rows["reading"])),
    "get_read_boundary(furthest)": lambda api, rows: boundary(
        api.get_read_boundary(rows["reading"], basis="furthest")),
}
