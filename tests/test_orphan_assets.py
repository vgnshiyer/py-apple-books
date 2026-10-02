"""``LibraryStats.orphan_assets`` (1.11): the per-asset breakdown of
``orphan_annotations``. Synthetic data only."""

import dataclasses

from py_apple_books.api import LibraryStats


def test_breakdown(api, library, sql_trace):
    book = library.add_book("Kept")
    for _ in range(2):
        library.add_annotation(book, "kept")
    for _ in range(3):
        library.add_annotation("REMOVED-A", "gone a")
    library.add_annotation("REMOVED-B", "gone b")
    library.add_annotation("REMOVED-B", "deleted", deleted=True)
    library.add_annotation(None, None, kind="tombstone")
    library.add_annotation("REMOVED-C", None, kind="reading_position")
    before = len(sql_trace)
    stats = api.get_library_stats()
    assert len(sql_trace) - before == 5
    assert stats.orphan_assets == (("REMOVED-A", 3), ("REMOVED-B", 1))
    assert sum(n for _, n in stats.orphan_assets) == stats.orphan_annotations == 4
    assert stats.annotations_per_book == ((book["id"], "Kept", 2),)


def test_empty_library(api):
    stats = api.get_library_stats()
    assert stats.orphan_assets == ()
    assert stats == LibraryStats(0, 0, 0, 0, 0, 0, ())


def test_ties_and_mixed_key_types(api, library):
    library.add_annotation("B-ASSET", "b")
    library.add_annotation("A-ASSET", "a")
    library.add_annotation("", "empty")
    library.add_annotation("Z-ASSET", "z1")
    library.add_annotation("Z-ASSET", "z2")
    pk = library.add_annotation("BLOB", "blob")
    library.execute("annotations", "UPDATE ZAEANNOTATION SET ZANNOTATIONASSETID = CAST(? AS BLOB) "
                    "WHERE Z_PK = ?", ("BYTES", pk))
    pk = library.add_annotation("NULL", "null")
    library.execute("annotations", "UPDATE ZAEANNOTATION SET ZANNOTATIONASSETID = NULL WHERE Z_PK = ?", (pk,))
    stats = api.get_library_stats()
    assert stats.orphan_assets == (("Z-ASSET", 2), (b"BYTES", 1), ("", 1), ("A-ASSET", 1), ("B-ASSET", 1),
                                   (None, 1))
    assert sum(n for _, n in stats.orphan_assets) == stats.orphan_annotations == 7


def test_field_is_last_with_a_default():
    fields = dataclasses.fields(LibraryStats)
    assert fields[-1].name == "orphan_assets" and fields[-1].default == ()
    assert [f.name for f in fields[:-1]] == [
        "total_books", "finished_books", "in_progress_books", "unstarted_books", "total_annotations",
        "orphan_annotations", "annotations_per_book"]
    assert LibraryStats(1, 1, 0, 0, 0, 0).orphan_assets == ()
