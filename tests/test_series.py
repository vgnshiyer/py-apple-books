"""``PyAppleBooks.get_series`` and ``list_series`` (provisional), and the
``Series``/``SeriesVolume`` types. Synthetic libraries only
(``FixtureLibrary.add_series``)."""

import copy
import datetime as dt
import pickle
import threading

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import BookNotFoundError, InvalidArgumentError
from py_apple_books.models import ReadingStatus, Series, SeriesVolume
from py_apple_books.testing import STORE_SERIES, UBIQUITY

UTC = dt.timezone.utc


class Lib:
    """A FixtureLibrary (``fx``) and a PyAppleBooks over it (``api``)."""

    def __init__(self, fx):
        self.fx = fx
        self.api = PyAppleBooks(data_dir=fx.data_dir)

    def refresh(self) -> None:
        self.api._PyAppleBooks__library.invalidate_schema()

    def drop(self, *columns: str) -> None:
        """Take columns away (renamed: some are indexed, which DROP refuses)."""
        for column in columns:
            self.fx.execute("library", f"ALTER TABLE ZBKLIBRARYASSET RENAME COLUMN {column} TO {column}_GONE")
        self.refresh()

    def set(self, book_id: int, **columns) -> None:
        assignments = ", ".join(f"{name} = ?" for name in columns)
        self.fx.execute("library", f"UPDATE ZBKLIBRARYASSET SET {assignments} WHERE Z_PK = ?",
                        (*columns.values(), book_id))


@pytest.fixture
def lib(make_library):
    made = Lib(make_library())
    yield made
    made.api.close()


def series_s(lib, **kwargs):
    """The design's fixture: an ordered 'Series S' with v1 (opened, 1 %,
    a purchase date, not redownloadable), v2 (unowned) and v3 (2.5)."""
    purchased = 700000000.0
    made = lib.fx.add_series("Series S", [
        dict(sequence=1, label="Book 1", progress=0.01, can_redownload=0, raw={"ZPURCHASEDATE": purchased}),
        dict(sequence=2, label="Book 2"),
        dict(sequence=2.5, label="Book 2.5"),
    ], **kwargs)
    return made["container"]["id"], [v["id"] for v in made["volumes"]], made


def ids(series: Series) -> list:
    return [v.ids for v in series.volumes]


class TestGetSeries:
    def test_the_design_fixture(self, lib):
        container, (v1, v2, v3), _ = series_s(lib)
        got = lib.api.get_series(v1)
        assert (got.title, got.is_ordered, got.container.id) == ("Series S", True, container)
        assert ids(got) == [(v1,), (v2,), (v3,)]
        assert [(v.sequence, v.label, v.in_library) for v in got.volumes] == [
            (1.0, "Book 1", False), (2.0, "Book 2", False), (2.5, "Book 2.5", False)]
        assert got.current.ids == (v1,) and got.up_next.ids == (v2,)
        assert got.next_after(v3) is None and got.next_after(v1).ids == (v2,)
        assert got.volumes[0].reading_status is ReadingStatus.IN_PROGRESS
        assert got.series_id == got.container.store_id

    def test_every_member_gives_the_same_series(self, lib):
        container, (v1, v2, v3), _ = series_s(lib)
        expected = lib.api.get_series(v1)
        for book in (container, v2, v3, str(v3), lib.api.get_book_by_id(v2)):
            assert lib.api.get_series(book) == expected

    def test_outside_any_series(self, lib):
        series_s(lib)
        assert lib.api.get_series(lib.fx.add_book("Alone")["id"]) is None

    def test_unknown_id(self, lib):
        with pytest.raises(BookNotFoundError):
            lib.api.get_series(999)

    def test_owned_copy_with_only_a_store_id(self, lib):
        container, (v1, v2, v3), made = series_s(lib)
        copy_ = lib.fx.add_book("My copy", data_source=UBIQUITY,
                                raw={"ZSTOREID": made["volumes"][1]["store_id"]})["id"]
        got = lib.api.get_series(copy_)
        assert got == lib.api.get_series(v2) == lib.api.get_series(v1)
        volume = got.volume_for(copy_)
        assert volume is got.volume_for(v2) and volume.ids == (v2, copy_)
        assert volume.book.id == copy_ and volume.in_library and volume.sequence == 2.0
        assert volume.label == "Book 2"
        assert got.next_after(copy_).ids == (v3,)
        assert copy_ in {b.id for b in lib.api.list_books()}

    def test_a_copy_of_nothing(self, lib):
        series_s(lib)
        stray = lib.fx.add_book("Stray", raw={"ZSTOREID": "555"})["id"]
        assert lib.api.get_series(stray) is None

    def test_redownloadable_volume_is_in_the_library(self, lib):
        made = lib.fx.add_series("Owned", [dict(sequence=1, can_redownload=1), dict(sequence=2)])
        got = lib.api.get_series(made["volumes"][0]["id"])
        assert [v.in_library for v in got.volumes] == [True, False]

    def test_unordered(self, lib):
        _, (v1, v2, v3), _ = series_s(lib, ordered=False)
        got = lib.api.get_series(v2)
        assert got.is_ordered is False and ids(got) == [(v1,), (v2,), (v3,)]
        assert got.up_next is None and got.next_after(v1) is None
        assert got.current.ids == (v1,)

    def test_linked_by_series_id_only(self, lib):
        container, (v1, v2, v3), _ = series_s(lib)
        for v in (v1, v2, v3):
            lib.set(v, ZSERIESCONTAINER=None)
        got = lib.api.get_series(v2)
        assert got.container.id == container and ids(got) == [(v1,), (v2,), (v3,)]
        assert lib.api.get_series(container) == got

    def test_dangling_container(self, lib):
        container, (v1, v2, v3), _ = series_s(lib)
        lib.fx.execute("library", "DELETE FROM ZBKLIBRARYASSET WHERE Z_PK = ?", (container,))
        got = lib.api.get_series(v1)
        assert got.container is None and got.title is None and got.is_ordered is None
        assert ids(got) == [(v1,), (v2,), (v3,)] and got.series_id is not None
        assert got.up_next.ids == (v2,)  # order unknown, not refused

    @pytest.mark.parametrize("value, expected", [
        ("nan", None), ("inf", None), ("1e999", None), ("x", None), ("2", 2.0)])
    def test_sequence_garbage(self, lib, value, expected):
        _, (v1, v2, v3), _ = series_s(lib)
        lib.set(v1, ZSEQUENCENUMBER=value)
        got = lib.api.get_series(v1)
        assert got.volume_for(v1).sequence == expected
        if expected is None:
            assert got.volumes[-1].ids == (v1,) and got.next_after(v1) is None

    def test_other_garbage(self, lib):
        container, (v1, v2, v3), _ = series_s(lib)
        lib.set(container, ZSERIESISORDERED="maybe")
        lib.set(v3, ZSERIESCONTAINER="abc")
        got = lib.api.get_series(v1)
        assert got.is_ordered is None and ids(got) == [(v1,), (v2,), (v3,)]

    def test_container_without_volumes(self, lib):
        made = lib.fx.add_series("Empty", [])
        got = lib.api.get_series(made["container"]["id"])
        assert got.title == "Empty" and got.volumes == () and got.current is None and got.up_next is None


class TestSeriesMethods:
    def test_volume_for_accepts_ids_text_and_books(self, lib):
        _, (v1, v2, v3), _ = series_s(lib)
        got = lib.api.get_series(v1)
        book = lib.api.get_book_by_id(v2)
        assert got.volume_for(book).ids == got.volume_for(str(v2)).ids == (v2,)
        assert got.volume_for(True) is None and got.volume_for(None) is None and got.volume_for(999) is None
        assert got.next_after(999) is None

    def test_current_prefers_the_highest_then_the_latest(self, lib):
        made = lib.fx.add_series("Two open", [
            dict(sequence=1, progress=0.5, last_opened=dt.datetime(2026, 9, 1, tzinfo=UTC)),
            dict(sequence=3, progress=0.2, last_opened=dt.datetime(2026, 1, 1, tzinfo=UTC)),
            dict(sequence=3, progress=0.1, last_opened=dt.datetime(2026, 5, 1, tzinfo=UTC)),
        ])
        got = lib.api.get_series(made["volumes"][0]["id"])
        assert got.current.ids == (made["volumes"][2]["id"],)

    def test_up_next_after_a_finished_volume(self, lib):
        made = lib.fx.add_series("Read", [dict(sequence=1, finished=True), dict(sequence=2), dict(sequence=3)])
        got = lib.api.get_series(made["volumes"][1]["id"])
        assert got.current is None and got.up_next.ids == (made["volumes"][1]["id"],)

    def test_not_hashable_but_picklable(self, lib):
        _, (v1, _, _), _ = series_s(lib)
        got = lib.api.get_series(v1)
        with pytest.raises(TypeError):
            hash(got)
        with pytest.raises(TypeError):
            hash(got.volumes[0])
        assert pickle.loads(pickle.dumps(got)) == got
        assert copy.deepcopy(got) == got
        with pytest.raises(AttributeError):
            got.title = "x"
        assert isinstance(got.volumes[0], SeriesVolume)


class TestListSeries:
    def make(self, lib):
        a = lib.fx.add_series("beta", [dict(sequence=1), dict(sequence=2)])
        b = lib.fx.add_series("Álpha", [dict(sequence=1, progress=0.3)])
        loose = lib.fx.add_book("Loose", data_source=STORE_SERIES, raw={"ZSERIESID": "777", "ZSTOREID": "778"})
        return a, b, loose

    def test_order_and_grouping(self, lib):
        a, b, loose = self.make(lib)
        got = lib.api.list_series()
        assert [s.title for s in got] == ["Álpha", "beta", None]
        assert got[2].series_id == "777" and got[2].container is None and ids(got[2]) == [(loose["id"],)]
        assert got[1] == lib.api.get_series(a["volumes"][0]["id"])

    def test_started_only(self, lib):
        self.make(lib)
        assert [s.title for s in lib.api.list_series(started_only=True)] == ["Álpha"]

    def test_paging(self, lib):
        self.make(lib)
        assert [s.title for s in lib.api.list_series(limit=1, offset=1)] == ["beta"]
        assert [s.title for s in lib.api.list_series(offset=2)] == [None]
        assert lib.api.list_series(offset=5) == []

    @pytest.mark.parametrize("kwargs", [dict(limit=0), dict(limit=-1), dict(limit=True), dict(limit="1"),
                                        dict(offset=-1), dict(offset=1.5)])
    def test_strict_limits(self, lib, kwargs):
        with pytest.raises(InvalidArgumentError):
            lib.api.list_series(**kwargs)

    def test_copies_join_their_volume(self, lib):
        a, _, _ = self.make(lib)
        copy_ = lib.fx.add_book("Copy", raw={"ZSTOREID": a["volumes"][1]["store_id"]})["id"]
        beta = lib.api.list_series()[1]
        assert beta.volumes[1].ids == (a["volumes"][1]["id"], copy_) and beta.volumes[1].in_library

    def test_empty(self, lib):
        lib.fx.add_book("No series")
        assert lib.api.list_series() == []


class TestStatements:
    def test_get_series_by_id(self, lib, sql_trace):
        _, (v1, _, _), _ = series_s(lib)
        list(lib.api.list_books(include_store_series=True))  # schema read
        sql_trace.clear()
        lib.api.get_series(v1)
        assert len(sql_trace) <= 4

    def test_get_series_by_book(self, lib, sql_trace):
        _, (v1, _, _), made = series_s(lib)
        copy_ = lib.fx.add_book("Copy", raw={"ZSTOREID": made["volumes"][0]["store_id"]})["id"]
        for book in (lib.api.get_book_by_id(v1), lib.api.get_book_by_id(copy_)):
            sql_trace.clear()
            lib.api.get_series(book)
            assert len(sql_trace) <= 3

    def test_list_series(self, lib, sql_trace):
        series_s(lib)
        list(lib.api.list_books(include_store_series=True))
        sql_trace.clear()
        lib.api.list_series(started_only=True)
        assert len(sql_trace) <= 2

    def test_parameters_do_not_grow_with_the_series(self, lib, sql_trace):
        small = lib.fx.add_series("Small", [dict(sequence=i) for i in range(2)])
        large = lib.fx.add_series("Large", [dict(sequence=i) for i in range(200)])
        counts = []
        for made in (small, large):
            sql_trace.clear()
            got = lib.api.get_series(made["volumes"][0]["id"])
            counts.append([len(params) for _, params in sql_trace])
            assert len(got.volumes) == len(made["volumes"])
        assert counts[0] == counts[1]


class TestOwnershipRule:
    """``SeriesVolume.in_library`` follows ``list_books()``, whichever
    ownership columns the store has (the rule of
    ``_api._common._owned_books_filter``, dropped predicate by predicate)."""

    @pytest.mark.parametrize("drop", [(), ("ZCANREDOWNLOAD",), ("ZDATASOURCEIDENTIFIER",), ("ZCONTENTTYPE",)])
    def test_in_library_matches_list_books(self, lib, drop):
        _, (v1, v2, v3), made = series_s(lib)
        lib.fx.add_book("Copy", raw={"ZSTOREID": made["volumes"][2]["store_id"]})
        owned = lib.fx.add_series("Owned", [dict(sequence=1, can_redownload=1), dict(sequence=2)])
        lib.drop(*drop)
        listed = {b.id for b in lib.api.list_books()}
        volumes = [v for s in lib.api.list_series() for v in s.volumes]
        assert len(volumes) == 5 and owned
        for volume in volumes:
            assert volume.in_library == (volume.book.id in listed)


class TestDrift:
    def test_2023_shape(self, lib):
        _, (v1, v2, v3), _ = series_s(lib)
        lib.set(v1, ZSEQUENCENUMBER=9)  # would sort it last
        lib.drop("ZSEQUENCENUMBER", "ZSERIESISORDERED")
        got = lib.api.get_series(v2)
        assert ids(got) == [(v1,), (v2,), (v3,)] and [v.label for v in got.volumes] == [
            "Book 1", "Book 2", "Book 2.5"]
        assert got.is_ordered is None and got.up_next is None and got.next_after(v1) is None
        assert got.current.ids == (v1,)

    def test_without_series_columns(self, lib):
        _, (v1, _, _), _ = series_s(lib)
        lib.drop("ZSERIESID", "ZSERIESCONTAINER")
        assert lib.api.get_series(v1) is None and lib.api.list_series() == []

    def test_without_store_ids(self, lib):
        container, (v1, v2, v3), _ = series_s(lib)
        lib.drop("ZSTOREID")
        got = lib.api.get_series(v3)
        assert got.container.id == container and ids(got) == [(v1,), (v2,), (v3,)]

    def test_without_content_type(self, lib):
        container, (v1, v2, v3), _ = series_s(lib)
        lib.drop("ZCONTENTTYPE")
        got = lib.api.get_series(v1)
        assert got.container.id == container and ids(got) == [(v1,), (v2,), (v3,)]
        assert lib.api.get_series(container) == got


class TestBinding:
    def test_worker_thread_reads_the_instance_library(self, lib):
        _, (v1, _, _), _ = series_s(lib)
        expected = lib.api.get_series(v1)
        got = []
        t = threading.Thread(target=lambda: got.append(lib.api.get_series(v1)))
        t.start()
        t.join()
        assert got == [expected]

    def test_a_book_from_another_library_is_resolved_here(self, lib, make_library):
        _, (v1, _, _), _ = series_s(lib)
        other = make_library()
        for i in range(v1):
            other.add_book(f"Other {i}")
        with LibraryDB(data_dir=other.data_dir) as db, use_library(db):
            foreign = PyAppleBooks().get_book_by_id(v1)
        assert foreign.series_id is None
        assert lib.api.get_series(foreign) == lib.api.get_series(v1)


def test_lists_are_unchanged_by_series_columns(make_library):
    """list_books, the status lists and the stats ignore the series
    columns: the same rows with and without them."""
    results = []
    for linked in (True, False):
        lib = Lib(make_library())
        made = lib.fx.add_series("S", [dict(sequence=1, progress=0.4, can_redownload=1), dict(sequence=2)])
        lib.fx.add_book("Plain", progress=0.2)
        if not linked:
            lib.fx.execute("library", "UPDATE ZBKLIBRARYASSET SET ZSERIESID = NULL, ZSERIESCONTAINER = NULL, "
                                      "ZSEQUENCENUMBER = NULL, ZSERIESISORDERED = NULL")
        api = lib.api
        results.append((
            [b.id for b in api.list_books()], [b.id for b in api.get_books_in_progress()],
            [b.id for b in api.get_unstarted_books()], [b.id for b in api.get_finished_books()],
            api.count_books_by_status(), api.get_library_stats(), made["container"]["id"]))
        api.close()
    assert results[0] == results[1]
