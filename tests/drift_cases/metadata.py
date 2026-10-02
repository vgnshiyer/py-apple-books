"""Drift cases for stream 2.4 (metadata): ``get_book_metadata``,
``get_series`` and ``list_series``.

``seed`` links the seeded Store series rows (``container``, ``volume``,
``owned_volume``) into one series by setting their series columns, so
no row is added (other drift tests count the seeded rows). The seeded
books have no file, so ``get_book_metadata`` reads the library only.
Series results are projected to what every schema shape keeps: the
2023 shape has no sequence or is-ordered column, which changes the
volume order and ``next_after``, so the cases compare titles and
sorted ids; ``tests/test_series.py`` checks the order on each shape.
"""

from tests.drift_cases import outcome

SERIES_ID = "1900000901"


def seed(lib, rows: dict) -> None:
    lib.execute("library", "UPDATE ZBKLIBRARYASSET SET ZSTOREID = ?, ZSERIESID = ?, ZSERIESISORDERED = 1 "
                "WHERE Z_PK = ?", (SERIES_ID, SERIES_ID, rows["container"]))
    for n, key in enumerate(("volume", "owned_volume"), start=1):
        lib.execute("library", "UPDATE ZBKLIBRARYASSET SET ZSTOREID = ?, ZSERIESID = ?, ZSERIESCONTAINER = ?, "
                    "ZSEQUENCENUMBER = ?, ZSEQUENCEDISPLAYNAME = ? WHERE Z_PK = ?",
                    (f"19000009{n:02d}9", SERIES_ID, rows["container"], n, f"Book {n}", rows[key]))
    return None


def metadata(result) -> tuple:
    return (str(result.file_state), result.language, result.published, result.year, result.description,
            result.subjects, sorted(result.book_file_fields))


def series(result):
    if result is None:
        return None
    return (result.title, result.series_id, getattr(result.container, "id", None),
            sorted((v.ids, v.in_library, v.label) for v in result.volumes))


CASES = {
    "get_book_metadata": lambda api, rows: outcome(lambda: metadata(api.get_book_metadata(rows["reading"]))),
    "get_book_metadata(library only)": lambda api, rows: metadata(
        api.get_book_metadata(api.get_book_by_id(rows["done"]), read_files=False)),
    "get_series": lambda api, rows: series(api.get_series(rows["volume"])),
    "get_series(container)": lambda api, rows: series(api.get_series(rows["container"])),
    "get_series(none)": lambda api, rows: series(api.get_series(rows["reading"])),
    "list_series": lambda api, rows: [series(s) for s in api.list_series()],
    "list_series(started)": lambda api, rows: [series(s) for s in api.list_series(started_only=True, limit=5)],
}
