"""1.10 read semantics against the synthetic library.

- F04: one reading-status rule, finished first; the three status lists
  partition the books in the library.
- F07: book lists leave out Store series items the user doesn't own
  (series containers, and Series-source volumes without the redownload
  flag). get_book_by_id still resolves them; get_book_content refuses
  them unless they have a local file.
- F25: annotation lists leave out deleted rows and type-0 tombstones
  unless ``include_deleted``; ``book.annotations`` agrees.
- F62: recently-read order and 'Last Read' use the later of the
  last-opened and last-engaged dates.
"""

import datetime as dt
import warnings

import pytest

from py_apple_books import api as api_module
from py_apple_books.exceptions import BookNotDownloadedError, NotInLibraryError
from py_apple_books.models import Annotation, Book, ReadingStatus
from py_apple_books.testing import STORE_SERIES, core_data_time, write_epub

UTC = dt.timezone.utc


def day(n: int) -> dt.datetime:
    return dt.datetime(2026, 9, n, 12, 0, tzinfo=UTC)


def cfi(chapter_id: str, step: int) -> str:
    return f"epubcfi(/6/{step}[{chapter_id}]!/4/4,/1:0,/1:21)"


def engaged(when) -> dict:
    return {"ZLASTENGAGEDDATE": core_data_time(when)}


def ids(rows) -> set:
    return {row.id for row in rows}


def order(rows) -> list:
    return [row.id for row in rows]


@pytest.fixture
def books(library, tmp_path):
    """Owned books covering every status corner, plus Store series rows."""
    add = library.add_book
    epub = write_epub(tmp_path / "Owned Series Volume.epub", "Owned Series Volume")
    rows = {
        "finished_zero": add("Finished At Zero", finished=True, progress=0.0,
                             last_opened=day(5), genre="History"),
        "finished_43": add("Finished At 43", finished=True, progress=0.43,
                           last_opened=day(20), genre="History"),
        # ZISFINISHED is NULL on unfinished rows (the fixture default).
        "reading": add("Reading Now", progress=0.5, last_opened=day(10),
                       raw=engaged(day(25)), genre="Fiction"),
        "null_progress": add("No Progress Recorded", progress=None),
        "zero": add("Explicitly Unfinished", progress=0.0, raw={"ZISFINISHED": 0}),
        "null_source": add("No Data Source", data_source=None, progress=0.2,
                           last_opened=day(12), genre="Fiction"),
        # Store series rows: only the one with the redownload flag is owned.
        "unowned_volume": add("Store Volume", data_source=STORE_SERIES, can_redownload=0,
                              state=5, progress=0.01, last_opened=day(28), genre="Fantasy"),
        "owned_volume": add("Owned Series Volume", data_source=STORE_SERIES, can_redownload=1,
                            progress=0.3, last_opened=day(15), path=epub, genre="Fantasy"),
        "null_redownload_volume": add("Store Volume Unknown", data_source=STORE_SERIES,
                                      raw={"ZCANREDOWNLOAD": None}, state=5,
                                      last_opened=day(27)),
        "container": add("Series Stack", data_source=STORE_SERIES, can_redownload=1,
                         content_type=5, state=5, last_opened=day(26), genre="Fantasy"),
    }
    return {key: row["id"] for key, row in rows.items()}


OWNED = {"finished_zero", "finished_43", "reading", "null_progress", "zero", "null_source",
         "owned_volume"}
STORE = {"unowned_volume", "null_redownload_volume", "container"}


def pick(books, keys) -> set:
    return {books[k] for k in keys}


@pytest.fixture
def annotations(library, books):
    """Live, deleted and system annotation rows on the "reading" book."""
    reading = library.execute("library", "SELECT ZASSETID FROM ZBKLIBRARYASSET WHERE Z_PK = ?",
                              (books["reading"],))[0][0]
    add = library.add_annotation
    return {
        "live": add(reading, "a live synthetic highlight", note="live note", created=day(20),
                    location=cfi("chap1", 4)),
        "deleted": add(reading, "a deleted synthetic highlight", note="deleted note",
                       deleted=True, created=day(21), location=cfi("chap1", 4)),
        "tombstone": add(None, None, kind="tombstone"),
        "position": add(reading, None, kind="reading_position", created=day(22),
                        location=cfi("chap1", 4)),
        "bookmark": add(reading, None, kind="bookmark", created=day(23)),
        "null_flag": add(reading, "a synthetic highlight with no deleted flag", created=day(24),
                         raw={"ZANNOTATIONDELETED": None}),
        "orphan": add("ORPHANASSET0000000000000000000000", "an orphan synthetic highlight",
                      created=day(19)),
    }


LIVE = {"live", "bookmark", "null_flag", "orphan"}


class TestStatusPartition:
    def test_lists_partition_the_library(self, api, books):
        in_progress = ids(api.get_books_in_progress())
        finished = ids(api.get_finished_books())
        unstarted = ids(api.get_unstarted_books())
        assert not (in_progress & finished or in_progress & unstarted or finished & unstarted)
        assert in_progress | finished | unstarted == ids(api.list_books())
        assert finished == pick(books, {"finished_zero", "finished_43"})
        assert in_progress == pick(books, {"reading", "null_source", "owned_volume"})
        assert unstarted == pick(books, {"null_progress", "zero"})

    def test_reading_status_agrees_per_book(self, api, books):
        lists = {
            ReadingStatus.FINISHED: ids(api.get_finished_books()),
            ReadingStatus.IN_PROGRESS: ids(api.get_books_in_progress()),
            ReadingStatus.UNSTARTED: ids(api.get_unstarted_books()),
        }
        for book in api.list_books():
            assert book.id in lists[book.reading_status], book.title

    def test_finished_at_zero_is_not_unstarted(self, api, books):
        assert books["finished_zero"] in ids(api.get_finished_books())
        assert books["finished_zero"] not in ids(api.get_unstarted_books())
        assert books["finished_43"] not in ids(api.get_books_in_progress())

    def test_null_progress_is_unstarted(self, api, books):
        assert books["null_progress"] in ids(api.get_unstarted_books())
        assert books["zero"] in ids(api.get_unstarted_books())

    def test_limit_order_and_offset(self, api, books):
        got = order(api.get_books_in_progress(limit=2, order_by="-last_opened_date"))
        assert got == [books["owned_volume"], books["null_source"]]
        assert order(api.get_finished_books(order_by="title", offset=1)) == [books["finished_zero"]]

    def test_currently_reading_resource_shape(self, api, books):
        """MCP 0.8.2's resource: the most recently opened unfinished book,
        never a Store series item or a finished book."""
        got = order(api.get_books_in_progress(limit=1, order_by="-last_opened_date"))
        assert got == [books["owned_volume"]]


class TestOwnedScope:
    def test_list_books_is_the_owned_set(self, api, books):
        assert ids(api.list_books()) == pick(books, OWNED)
        assert ids(api.list_books(include_store_series=True)) == set(books.values())

    def test_owned_set_is_not_is_store_series_item(self, api, books):
        """The SQL scope and the model property apply the same rule."""
        every = api.list_books(include_store_series=True)
        assert ids(api.list_books()) == {b.id for b in every if not b.is_store_series_item}

    def test_title_search(self, api, books):
        assert ids(api.get_book_by_title("store volume")) == set()
        assert ids(api.get_book_by_title("series")) == {books["owned_volume"]}
        assert ids(api.get_book_by_title("store volume", include_store_series=True)) == pick(
            books, {"unowned_volume", "null_redownload_volume"})
        assert ids(api.get_book_by_title("", limit=2, order_by="title")) == pick(
            books, {"zero", "finished_43"})

    def test_genre(self, api, books):
        assert ids(api.get_books_by_genre("Fantasy")) == {books["owned_volume"]}
        assert ids(api.get_books_by_genre("Fantasy", include_store_series=True)) == pick(
            books, {"owned_volume", "unowned_volume", "container"})

    def test_recently_read(self, api, books):
        got = order(api.get_recently_read_books(limit=None))
        assert not set(got) & pick(books, STORE)
        assert books["unowned_volume"] not in order(api.get_recently_read_books(limit=1))
        legacy = order(api.get_recently_read_books(limit=None, order_by="-last_opened_date"))
        assert not set(legacy) & pick(books, STORE)

    def test_get_book_by_id_resolves_every_row(self, api, books):
        for key, book_id in books.items():
            assert api.get_book_by_id(book_id).id == book_id, key
        assert api.get_book_by_id(books["container"]).is_series_container

    @pytest.mark.parametrize("key", ["unowned_volume", "null_redownload_volume", "container"])
    def test_content_of_store_item_is_not_in_library(self, api, books, key):
        with pytest.raises(NotInLibraryError) as exc:
            api.get_book_content(books[key])
        assert "isn't in your library" in str(exc.value)
        # MCP 0.8.2 catches BookNotDownloadedError ("Book not available: ...").
        assert isinstance(exc.value, BookNotDownloadedError)

    def test_owned_series_volume_opens(self, api, books):
        content = api.get_book_content(books["owned_volume"])
        assert [c.title for c in content.list_chapters()] == ["Chapter 1", "Chapter 2"]

    def test_store_row_with_a_local_file_opens(self, api, library, tmp_path):
        """Never refuse a row that has a local file."""
        epub = write_epub(tmp_path / "Local Store Volume.epub", "Local Store Volume")
        row = library.add_book("Local Store Volume", data_source=STORE_SERIES, can_redownload=0,
                               path=epub)
        book = api.get_book_by_id(row["id"])
        assert book.is_store_series_item
        assert api.get_book_content(row["id"]).list_chapters()

    def test_null_data_source_and_content_type_are_owned(self, api, library):
        row = library.add_book("Legacy Row", data_source=None, raw={"ZCONTENTTYPE": None})
        assert row["id"] in ids(api.list_books())
        assert row["id"] in ids(api.get_unstarted_books())

    @pytest.mark.parametrize("missing", [{"content_type"}, {"data_source"}, {"can_redownload"}])
    def test_scope_degrades_when_a_column_is_missing(self, api, books, monkeypatch, missing):
        """A predicate needs its columns; without them the rows it would
        hide are shown, as in 1.9.1 (hiding needs all the evidence)."""
        monkeypatch.setattr(Book.manager, "has_fields",
                            lambda *fields: not missing & set(fields))
        listed = ids(api.list_books())
        if missing == {"content_type"}:
            assert books["container"] in listed
            assert books["unowned_volume"] not in listed
        else:
            assert books["unowned_volume"] in listed
            assert books["null_redownload_volume"] in listed
            assert books["container"] not in listed

    def test_scope_sql(self, api, books, sql_trace):
        list(api.list_books())
        sql, params = [(s, p) for s, p in sql_trace if "FROM ZBKLIBRARYASSET" in s][-1]
        assert sql.endswith("WHERE ZCONTENTTYPE IS NOT ? "
                            "AND (ZDATASOURCEIDENTIFIER IS NOT ? OR ZCANREDOWNLOAD = ?)")
        assert tuple(params) == (5, STORE_SERIES, 1)


class TestDeletedAnnotations:
    @staticmethod
    def rows(annotations, keys) -> set:
        return {annotations[k] for k in keys}

    def test_list(self, api, annotations):
        assert ids(api.list_annotations()) == self.rows(annotations, LIVE)
        pre_110 = LIVE | {"deleted", "tombstone"}
        assert ids(api.list_annotations(include_deleted=True)) == self.rows(annotations, pre_110)

    def test_color(self, api, annotations):
        assert ids(api.get_annotations_by_color("yellow")) == self.rows(
            annotations, {"live", "null_flag", "orphan"})
        assert annotations["deleted"] in ids(api.get_annotations_by_color("yellow", include_deleted=True))

    def test_searches(self, api, annotations):
        live = self.rows(annotations, {"live", "null_flag", "orphan"})
        assert ids(api.search_annotation_by_highlighted_text("synthetic")) == live
        assert ids(api.search_annotation_by_text("synthetic")) == live
        assert ids(api.search_annotation_by_note("note")) == {annotations["live"]}
        assert ids(api.search_annotation_by_note("note", include_deleted=True)) == self.rows(
            annotations, {"live", "deleted"})
        with_deleted = api.search_annotation_by_text("synthetic", include_deleted=True)
        assert isinstance(with_deleted, list)
        assert ids(with_deleted) == live | {annotations["deleted"]}

    def test_text_search_is_one_statement(self, api, annotations, sql_trace):
        api.search_annotation_by_text("synthetic", limit=2)
        searches = [(sql, params) for sql, params in sql_trace if "abk_fold" in sql]
        assert len(searches) == 1
        sql, params = searches[0]
        assert ("WHERE ZANNOTATIONTYPE > ? AND ZANNOTATIONTYPE != ? AND ZANNOTATIONDELETED IS NOT ? "
                "AND (instr(") in sql
        assert tuple(params)[:3] == (0, 3, 1)

    def test_date_range(self, api, annotations):
        after = dt.datetime(2026, 9, 1)
        assert ids(api.get_annotations_by_date_range(after=after)) == self.rows(
            annotations, {"live", "bookmark", "null_flag", "orphan"})
        assert annotations["deleted"] in ids(
            api.get_annotations_by_date_range(after=after, include_deleted=True))
        # The tombstone has no date: only an unbounded range includes it.
        assert annotations["tombstone"] in ids(api.get_annotations_by_date_range(include_deleted=True))
        assert annotations["tombstone"] not in ids(api.get_annotations_by_date_range())

    def test_book_annotations_are_live_only(self, api, books, annotations):
        book = api.get_book_by_id(books["reading"])
        assert ids(book.annotations) == self.rows(annotations, {"live", "bookmark", "null_flag"})

    def test_get_annotation_by_id_is_unfiltered(self, api, annotations):
        assert api.get_annotation_by_id(annotations["deleted"]).is_deleted == 1
        assert api.get_annotation_by_id(annotations["tombstone"]).type == 0
        assert api.get_annotation_by_id(annotations["position"]).type == 3

    def test_relation_filter_matches_the_facade_scope(self):
        relation = next(r for r in Book.relations if r["name"] == "annotations")
        assert relation["extra_filters"] == api_module._LIVE_ANNOTATIONS
        assert api_module._annotation_scope(False) == api_module._LIVE_ANNOTATIONS
        assert api_module._annotation_scope(True) == {"type__ne": 3}

    def test_scope_is_a_copy(self):
        scope = api_module._annotation_scope(False)
        scope["creation_date__gte"] = 0
        assert "creation_date__gte" not in api_module._LIVE_ANNOTATIONS


class TestRecency:
    def test_engaged_date_counts(self, api, books):
        """"Reading Now" was opened on the 10th but engaged on the 25th."""
        got = order(api.get_recently_read_books(limit=3))
        assert got == [books["reading"], books["finished_43"], books["owned_volume"]]

    def test_last_opened_date_gives_the_1_9_order(self, api, books):
        got = order(api.get_recently_read_books(limit=3, order_by="-last_opened_date"))
        assert got == [books["finished_43"], books["owned_volume"], books["null_source"]]

    def test_ascending_and_storage_order(self, api, books):
        opened = pick(books, OWNED) - pick(books, {"null_progress", "zero"})
        oldest = order(api.get_recently_read_books(limit=None, order_by="last_read_date"))
        assert oldest == list(reversed(order(api.get_recently_read_books(limit=None))))
        assert set(oldest) == opened
        assert ids(api.get_recently_read_books(limit=None, order_by=None)) == opened

    def test_offset_and_limit_apply_after_the_sort(self, api, books):
        full = order(api.get_recently_read_books(limit=None))
        assert order(api.get_recently_read_books(limit=2, offset=1)) == full[1:3]
        assert order(api.get_recently_read_books(limit=None, offset=3)) == full[3:]
        assert order(api.get_recently_read_books(offset=10**30)) == []

    def test_ties_by_id(self, api, library):
        first = library.add_book("Tie A", last_opened=day(3))["id"]
        second = library.add_book("Tie B", last_opened=day(3))["id"]
        assert order(api.get_recently_read_books()) == [first, second]
        assert order(api.get_recently_read_books(order_by="last_read_date")) == [first, second]

    def test_limit_zero_means_all_with_a_warning(self, api, books):
        with pytest.warns(DeprecationWarning, match="limit <= 0"):
            got = order(api.get_recently_read_books(limit=0))
        assert got == order(api.get_recently_read_books(limit=None))

    def test_negative_offset_is_rejected(self, api, books):
        with pytest.raises(ValueError):
            api.get_recently_read_books(offset=-1)

    def test_runs_one_query_and_evaluates_once(self, api, books, sql_trace):
        recent = api.get_recently_read_books(limit=None)
        before = len([s for s, _ in sql_trace if "FROM ZBKLIBRARYASSET" in s])
        assert len(list(recent)) == len(recent)
        after = len([s for s, _ in sql_trace if "FROM ZBKLIBRARYASSET" in s])
        assert before == 1 and after == before

    def test_progress_summary_shows_last_read_date(self, api, books):
        book = api.get_book_by_id(books["reading"])
        assert book.last_read_date == book.last_engaged_date
        summary = book.format_progress_summary()
        assert f"Last Read: {book.last_engaged_date:%Y-%m-%d}" in summary
        assert f"{book.last_opened_date:%Y-%m-%d}" not in summary

    def test_progress_summary_without_engaged_date(self, api, books):
        book = api.get_book_by_id(books["null_source"])
        assert book.last_engaged_date is None
        assert f"Last Read: {book.last_opened_date:%Y-%m-%d}" in book.format_progress_summary()


class TestLookups:
    """NULL-safe predicates end to end: a NULL column is 'not x'."""

    def test_isnot_keeps_null(self, library):
        null = library.add_book("Null Flag")["id"]  # ZISFINISHED NULL
        done = library.add_book("Done", finished=True)["id"]
        assert ids(Book.manager.filter(is_finished__isnot=1)) == {null}
        assert ids(Book.manager.filter(is_finished__ne=1)) == set()  # plain != drops NULL
        assert ids(Book.manager.filter(is_finished=1)) == {done}

    def test_not_gt_keeps_null(self, library):
        none = library.add_book("None", progress=None)["id"]
        zero = library.add_book("Zero", progress=0.0)["id"]
        some = library.add_book("Some", progress=0.1)["id"]
        assert ids(Book.manager.filter(reading_progress__not_gt=0)) == {none, zero}
        assert ids(Book.manager.filter(reading_progress__lte=0)) == {zero}
        assert ids(Book.manager.filter(reading_progress__gt=0)) == {some}

    def test_deleted_flag_null_is_live(self, library):
        book = library.add_book("Book")
        null = library.add_annotation(book, "x", raw={"ZANNOTATIONDELETED": None})
        library.add_annotation(book, "y", deleted=True)
        assert ids(Annotation.manager.filter(is_deleted__isnot=1)) == {null}

    def test_status_filters_warn_nothing(self, api, books):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            for method in (api.get_books_in_progress, api.get_finished_books, api.get_unstarted_books,
                           api.list_books, api.get_recently_read_books):
                list(method())
