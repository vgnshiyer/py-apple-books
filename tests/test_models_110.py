"""1.10 model data against the synthetic library: the newly mapped
columns (MAP-IDS, F63, F62, F53) and the properties built on them
(F04 reading status, F07 series flags, F18 deep links).

Books and annotations are fetched by id, which stays unscoped through
1.10, so Store-series rows and tombstones resolve too. Nothing here
depends on listing order, relation loading or how often a query runs.
"""

import datetime as dt

import pytest

from py_apple_books.models import Annotation, AnnotationType, Book, ReadingStatus
from py_apple_books.models.book import (
    CONTENT_TYPE_SERIES_CONTAINER,
    SERIES_DATA_SOURCE,
    STATE_CLOUD_ONLY,
)
from py_apple_books.testing import ANNOTATION_KINDS, STORE_SERIES, core_data_time

UTC = dt.timezone.utc


def day(n: int) -> dt.datetime:
    return dt.datetime(2020, 1, n, 12, 0, tzinfo=UTC)


def cfi(chapter_id: str, step: int, start: int = 0) -> str:
    return f"epubcfi(/6/{step}[{chapter_id}]!/4/4,/1:{start},/1:{start + 21})"


def engaged(when) -> dict:
    return {"ZLASTENGAGEDDATE": core_data_time(when)}


# ---------------------------------------------------------------------------
# Enums and constants
# ---------------------------------------------------------------------------


class TestEnums:
    def test_reading_status_formats_as_its_value(self):
        assert str(ReadingStatus.FINISHED) == "finished"
        assert f"{ReadingStatus.IN_PROGRESS}" == "in_progress"
        assert "{}".format(ReadingStatus.UNSTARTED) == "unstarted"
        assert f"{ReadingStatus.UNSTARTED:>10}" == " unstarted"

    def test_reading_status_is_a_str(self):
        assert ReadingStatus.FINISHED == "finished"
        assert ReadingStatus("in_progress") is ReadingStatus.IN_PROGRESS
        assert [s.value for s in ReadingStatus] == ["finished", "in_progress", "unstarted"]

    def test_annotation_type_matches_the_fixture_kinds(self):
        assert AnnotationType.TOMBSTONE == ANNOTATION_KINDS["tombstone"][0] == 0
        assert AnnotationType.BOOKMARK == ANNOTATION_KINDS["bookmark"][0] == 1
        assert AnnotationType.HIGHLIGHT == ANNOTATION_KINDS["highlight"][0] == ANNOTATION_KINDS["note"][0] == 2
        assert AnnotationType.READING_POSITION == ANNOTATION_KINDS["reading_position"][0] == 3

    def test_constants(self):
        assert SERIES_DATA_SOURCE == STORE_SERIES
        assert (CONTENT_TYPE_SERIES_CONTAINER, STATE_CLOUD_ONLY) == (5, 3)


# ---------------------------------------------------------------------------
# Book
# ---------------------------------------------------------------------------


class TestBookFields:
    def test_new_columns_are_read(self, api, library):
        row = library.add_book("Store Volume", data_source=STORE_SERIES, state=5,
                               raw={"ZSTOREID": "1234567890", **engaged(day(10))})
        book = api.get_book_by_id(row["id"])
        assert (book.store_id, book.data_source, book.can_redownload, book.state) == (
            "1234567890", STORE_SERIES, 0, 5)
        assert isinstance(book.last_engaged_date, dt.datetime)

    def test_owned_row_defaults(self, api, library):
        book = api.get_book_by_id(library.add_book()["id"])
        assert (book.store_id, book.can_redownload, book.state, book.last_engaged_date) == (None, 1, 1, None)

    def test_deep_link(self, api, library):
        row = library.add_book()
        assert api.get_book_by_id(row["id"]).deep_link == f"ibooks://assetid/{row['asset_id']}"


class TestReadingStatus:
    @pytest.mark.parametrize("kwargs, expected", [
        (dict(finished=True, progress=0.0), ReadingStatus.FINISHED),   # finished at 0%
        (dict(finished=True, progress=0.43), ReadingStatus.FINISHED),  # finished wins
        (dict(finished=True, progress=1.0), ReadingStatus.FINISHED),
        (dict(progress=0.5), ReadingStatus.IN_PROGRESS),
        (dict(progress=None), ReadingStatus.UNSTARTED),                 # NULL progress
        (dict(progress=0.0), ReadingStatus.UNSTARTED),
        # ZISFINISHED 0 rather than NULL
        (dict(progress=0.0, raw={"ZISFINISHED": 0}), ReadingStatus.UNSTARTED),
        (dict(progress=0.3, raw={"ZISFINISHED": 0}), ReadingStatus.IN_PROGRESS),
    ])
    def test_status(self, api, library, kwargs, expected):
        book = api.get_book_by_id(library.add_book(**kwargs)["id"])
        assert book.reading_status is expected


class TestLastReadDate:
    def test_engaged_after_opened(self, api, library):
        """Engaged after the last open: the book stayed open, so
        ZLASTOPENDATE alone is stale."""
        row = library.add_book(progress=0.3, last_opened=day(3), raw=engaged(day(10)))
        book = api.get_book_by_id(row["id"])
        assert book.last_engaged_date - book.last_opened_date == dt.timedelta(days=7)
        assert book.last_read_date == book.last_engaged_date

    def test_opened_after_engaged(self, api, library):
        row = library.add_book(progress=0.3, last_opened=day(10), raw=engaged(day(3)))
        book = api.get_book_by_id(row["id"])
        assert book.last_read_date == book.last_opened_date > book.last_engaged_date

    def test_engaged_null(self, api, library):
        book = api.get_book_by_id(library.add_book(last_opened=day(10))["id"])
        assert book.last_engaged_date is None
        assert book.last_read_date == book.last_opened_date is not None

    def test_opened_null(self, api, library):
        book = api.get_book_by_id(library.add_book(raw=engaged(day(3)))["id"])
        assert book.last_opened_date is None
        assert book.last_read_date == book.last_engaged_date is not None

    def test_both_null(self, api, library):
        assert api.get_book_by_id(library.add_book()["id"]).last_read_date is None


class TestSeriesAndCloudFlags:
    def test_series_container(self, api, library):
        container = library.add_book("Series", data_source=STORE_SERIES,
                                     content_type=CONTENT_TYPE_SERIES_CONTAINER, state=6)
        volume = library.add_book("Volume", data_source=STORE_SERIES, state=5)
        owned = library.add_book("Owned")
        assert api.get_book_by_id(container["id"]).is_series_container
        assert not api.get_book_by_id(volume["id"]).is_series_container
        assert not api.get_book_by_id(owned["id"]).is_series_container

    @pytest.mark.parametrize("kwargs, expected", [
        (dict(data_source=STORE_SERIES, can_redownload=0, state=5), True),
        (dict(data_source=STORE_SERIES, can_redownload=1), False),  # owned volume
        (dict(data_source=STORE_SERIES, raw={"ZCANREDOWNLOAD": None}), True),
        (dict(data_source=STORE_SERIES, can_redownload=1, content_type=CONTENT_TYPE_SERIES_CONTAINER), True),
        (dict(), False),  # ubiquity
        (dict(data_source=None), False),
    ], ids=["series-0", "series-1", "series-null", "container-1", "ubiquity", "no-source"])
    def test_store_series_item(self, api, library, kwargs, expected):
        book = api.get_book_by_id(library.add_book(**kwargs)["id"])
        assert book.is_store_series_item is expected

    @pytest.mark.parametrize("state, expected", [(3, True), (1, False), (5, False), (None, False)])
    def test_cloud_only(self, api, library, state, expected):
        row = library.add_book(raw={"ZSTATE": state})
        assert api.get_book_by_id(row["id"]).is_cloud_only is expected

    def test_new_fields_are_filterable(self, library):
        library.add_book("Local")
        cloud = library.add_book("In iCloud", state=STATE_CLOUD_ONLY)
        assert [b.id for b in Book.manager.filter(state=STATE_CLOUD_ONLY)] == [cloud["id"]]


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------


class TestAnnotationFields:
    def test_uuid(self, api, library):
        book = library.add_book()
        pks = [library.add_annotation(book, f"highlight {i}") for i in range(3)]
        stored = dict(library.execute("annotations", "SELECT Z_PK, ZANNOTATIONUUID FROM ZAEANNOTATION"))
        uuids = [api.get_annotation_by_id(pk).uuid for pk in pks]
        assert uuids == [stored[pk] for pk in pks]
        assert len(set(uuids)) == 3 and all(uuids)

    @pytest.mark.parametrize("kind", ["highlight", "note", "bookmark"])
    def test_position_is_the_spine_index(self, api, library, kind):
        book = library.add_book()
        pk = library.add_annotation(book, "text", kind=kind, location=cfi("c3", 8), range_start=3)
        annotation = api.get_annotation_by_id(pk)
        assert annotation.position == annotation.location.spine_index == 3

    def test_deep_link_with_location(self, api, library):
        book = library.add_book()
        location = cfi("c3", 8, start=629)
        annotation = api.get_annotation_by_id(library.add_annotation(book, "text", location=location))
        assert annotation.deep_link == f"ibooks://assetid/{book['asset_id']}#{location}"
        assert annotation.deep_link.startswith(api.get_book_by_id(book["id"]).deep_link + "#")

    def test_deep_link_without_location(self, api, library):
        book = library.add_book()
        annotation = api.get_annotation_by_id(library.add_annotation(book, "text", location=None))
        assert annotation.deep_link == f"ibooks://assetid/{book['asset_id']}"

    def test_tombstone_has_no_deep_link(self, api, library):
        annotation = api.get_annotation_by_id(library.add_annotation(None, kind="tombstone"))
        assert annotation.type == AnnotationType.TOMBSTONE
        assert annotation.deep_link is None

    def test_orphan_deep_link_needs_no_book(self, api, library, sql_trace):
        """Built from the row alone: no book lookup, so a highlight whose
        book is gone still gets a link."""
        location = cfi("c1", 4)
        annotation = api.get_annotation_by_id(
            library.add_annotation("ASSET-OF-A-REMOVED-BOOK", "text", location=location))
        sql_trace.clear()
        assert annotation.deep_link == f"ibooks://assetid/ASSET-OF-A-REMOVED-BOOK#{location}"
        assert sql_trace == []

    def test_order_by_position(self, library):
        book = library.add_book()
        other = library.add_book()
        # Created in the opposite order to their place in the book.
        for n, (step, start) in enumerate([(12, 0), (4, 50), (8, 0), (4, 10)]):
            library.add_annotation(book, f"highlight {n}", created=day(n + 1),
                                   location=cfi(f"c{step}", step, start), range_start=step // 2 - 1)
        library.add_annotation(other, "elsewhere", range_start=0)

        rows = list(Annotation.manager.filter(asset_id=book["asset_id"], order_by="position"))
        assert [a.position for a in rows] == [1, 1, 3, 5]
        assert all(a.position == a.location.spine_index for a in rows)
        # Ties on position break by the CFI.
        rows.sort(key=lambda a: (a.position, a.location.sort_key))
        assert [a.location.sort_key[-1] for a in rows] == [10, 50, 0, 0]

        descending = list(Annotation.manager.filter(asset_id=book["asset_id"], order_by="-position"))
        assert [a.position for a in descending] == [5, 3, 1, 1]
