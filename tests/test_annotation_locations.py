"""``PyAppleBooks.get_annotation_locations`` (stream 3.1, positions F43):
the spine file and chapter of many annotations, every book looked up in
one query and gated once."""

from __future__ import annotations

import pytest

from py_apple_books import _epub_index
from py_apple_books._api import positions as positions_api
from py_apple_books._content_resolve import _Boundaries
from py_apple_books.content import BookContent
from py_apple_books.exceptions import AppleBooksError, DBError, InvalidArgumentError
from py_apple_books.positions import ChapterMatch, ResolvedLocation, UnavailableReason
from py_apple_books.testing.fixture import STORE_SERIES
from tests import _epub_shapes, _fs_audit
from tests._positions_helpers import cfi, icloud, parses, split_book  # noqa: F401

R = UnavailableReason


def _book_queries(trace):
    return [sql for sql, _ in trace if "ZBKLIBRARYASSET" in sql]


@pytest.fixture
def seeded(library, api, tmp_path):
    split = library.add_book("Split", path=str(split_book(tmp_path)))
    plain = library.add_book("Plain", path=str(_epub_shapes.plain(tmp_path)))
    rows = {
        "a": library.add_annotation(split, "a text.", location=cfi(0, "s0", "/4/4,/1:0,/1:7")),
        "b": library.add_annotation(split, "b text.", location=cfi(0, "s0", "/4/8,/1:0,/1:7")),
        "b2": library.add_annotation(split, "b continues.", location=cfi(1, "s1", "/4/2,/1:0,/1:5")),
        "c": library.add_annotation(split, "c text.", location=cfi(2, "s2", "/4/6,/1:0,/1:5")),
        "two": library.add_annotation(plain, "Beta", location=cfi(1, "ch2", "/4/4,/1:0,/1:4")),
        "nowhere": library.add_annotation(plain, "x", location=cfi(7, "gone")),
        "noloc": library.add_annotation(plain, "no location"),
        "bookmark": library.add_annotation(plain, None, kind="bookmark", location=cfi(2, "ch3")),
        "orphan": library.add_annotation("GONE-ASSET", "orphan", location=cfi(0, "x")),
    }
    return {"split": split, "plain": plain, **rows}


def _by_id(api, *ids):
    return [api.get_annotation_by_id(i) for i in ids]


class TestResults:
    def test_each_annotation(self, api, seeded):
        anns = list(api.list_annotations(order_by="id"))
        found = api.get_annotation_locations(anns)
        assert list(found) == [a.id for a in anns]

        def short(key):
            r = found[seeded[key]]
            return (r.chapter.title if r.chapter else None, r.match, r.spine_index, r.item_id, r.unavailable)

        assert short("a") == ("A", ChapterMatch.ANCHOR, 0, "s0", None)
        assert short("b") == ("B", ChapterMatch.ANCHOR, 0, "s0", None)
        assert short("b2") == ("B", ChapterMatch.PRECEDING, 1, "s1", None)
        assert short("c") == ("C", ChapterMatch.FILE, 2, "s2", None)
        assert short("two") == ("Two", ChapterMatch.FILE, 1, "ch2", None)
        assert short("bookmark") == ("Three", ChapterMatch.FILE, 2, "ch3", None)
        assert short("nowhere") == (None, None, 7, "gone", R.NO_LOCATION)
        assert short("noloc") == (None, None, None, None, R.NO_LOCATION)
        assert short("orphan") == (None, None, 0, "x", R.ORPHANED)

    def test_same_as_book_content_resolve(self, api, seeded):
        anns = _by_id(api, seeded["a"], seeded["b2"], seeded["c"])
        content = BookContent(api.get_book_by_id(seeded["split"]["id"]).path)
        found = api.get_annotation_locations(anns)
        assert [found[a.id] for a in anns] == [content.resolve(a.location) for a in anns]

    def test_one_entry_per_annotation_in_order(self, api, seeded):
        a, c = _by_id(api, seeded["a"], seeded["c"])
        again = api.get_annotation_by_id(seeded["a"])
        found = api.get_annotation_locations(iter([c, a, again, c]))
        assert list(found) == [c.id, a.id]

    def test_empty(self, api, sql_trace):
        assert api.get_annotation_locations([]) == {}
        assert api.get_annotation_locations(iter(())) == {}
        assert sql_trace == []

    @pytest.mark.parametrize("bad", [[1], ["epubcfi(/6/2)"], [None]])
    def test_items_must_be_annotations(self, api, bad):
        with pytest.raises(InvalidArgumentError):
            api.get_annotation_locations(bad)

    def test_a_single_annotation_is_refused(self, api, seeded):
        with pytest.raises(InvalidArgumentError):
            api.get_annotation_locations(api.get_annotation_by_id(seeded["a"]))


class TestQueriesAndReads:
    def test_books_in_one_query_each_gated_once(self, api, seeded, sql_trace, monkeypatch):
        anns = list(api.list_annotations())
        gated = []
        real = _epub_index._gate_book
        monkeypatch.setattr(_epub_index, "_gate_book", lambda book: gated.append(book.id) or real(book))
        del sql_trace[:]
        api.get_annotation_locations(anns)
        assert len(_book_queries(sql_trace)) == 1
        assert sorted(gated) == sorted([seeded["split"]["id"], seeded["plain"]["id"]])

    def test_many_assets_read_every_book_in_one_query(self, api, seeded, sql_trace, monkeypatch):
        monkeypatch.setattr(positions_api, "_IN_LIST_MAX", 1)
        anns = list(api.list_annotations())
        expected = api.get_annotation_locations(anns)
        monkeypatch.setattr(positions_api, "_IN_LIST_MAX", 500)
        del sql_trace[:]
        assert api.get_annotation_locations(anns) == expected
        monkeypatch.setattr(positions_api, "_IN_LIST_MAX", 1)
        del sql_trace[:]
        assert api.get_annotation_locations(anns) == expected
        assert len(_book_queries(sql_trace)) == 1

    def test_no_book_query_without_located_annotations(self, api, seeded, sql_trace):
        anns = _by_id(api, seeded["noloc"])
        del sql_trace[:]
        assert api.get_annotation_locations(anns)[seeded["noloc"]].unavailable == R.NO_LOCATION
        assert _book_queries(sql_trace) == []

    def test_warm_call_parses_nothing_and_runs_nothing(self, api, seeded, parses, monkeypatch):
        anns = list(api.list_annotations())
        first = api.get_annotation_locations(anns)
        assert parses == {"index": 2, "anchors": 1}  # s0's anchors once, for a, b and b2
        monkeypatch.setattr("py_apple_books.content.subprocess.run", lambda *a, **k: pytest.fail("du ran"))
        with _fs_audit.record() as rec:
            assert api.get_annotation_locations(anns) == first
        assert parses == {"index": 2, "anchors": 1}
        assert rec.of(*_fs_audit.PROCESS_EVENTS) == [] and rec.of("os.scandir", "os.listdir") == []
        assert [e for e in rec.of("open") if ".epub" in (e.path or "")] == []


class TestBookReasons:
    def _reasons(self, api, library, book, *locations):
        ids = [library.add_annotation(book, f"t{i}", location=loc) for i, loc in enumerate(locations)]
        found = api.get_annotation_locations(_by_id(api, *ids))
        return [(found[i].unavailable, found[i].spine_index, found[i].item_id, found[i].chapter, found[i].match)
                for i in ids]

    def test_not_downloaded_keeps_the_cfi_data(self, api, library):
        book = library.add_book("Cloud")
        assert self._reasons(api, library, book, cfi(3, "c4"), cfi(0)) == [
            (R.NOT_DOWNLOADED, 3, "c4", None, None), (R.NOT_DOWNLOADED, 0, None, None, None)]

    def test_cloud_only_touches_no_file(self, api, library, tmp_path, icloud):
        bundle = _epub_shapes.plain(tmp_path)
        book = library.add_book("Cloud", path=str(bundle), state=3)
        icloud.mark()
        with _fs_audit.record() as rec:
            assert self._reasons(api, library, book, cfi(1, "ch2"))[0][0] == R.NOT_DOWNLOADED
        assert icloud.touched(bundle) == [] and rec.under(str(bundle)) == []

    def test_not_owned(self, api, library):
        book = library.add_book("Volume", data_source=STORE_SERIES)
        assert self._reasons(api, library, book, cfi(1, "ch2"))[0][0] == R.NOT_OWNED

    def test_pdf(self, api, library, tmp_path):
        pdf = tmp_path / "B.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        book = library.add_book("Pdf", path=str(pdf), content_type=3)
        assert self._reasons(api, library, book, cfi(1, "ch2"))[0][0] == R.NOT_EPUB

    def test_drm(self, api, library, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        (bundle / "META-INF" / "rights.xml").write_text("<rights/>")
        book = library.add_book("Drm", path=str(bundle))
        assert self._reasons(api, library, book, cfi(1, "ch2"), cfi(0, "ch1"))[1][0] == R.DRM

    def test_placeholder_package(self, api, library, tmp_path, icloud):
        bundle = _epub_shapes.plain(tmp_path)
        icloud.mark(bundle / "OEBPS")
        book = library.add_book("Evicted", path=str(bundle))
        assert self._reasons(api, library, book, cfi(1, "ch2"))[0][0] == R.NOT_DOWNLOADED

    def test_missing_bundle(self, api, library, tmp_path):
        book = library.add_book("Gone", path=str(tmp_path / "Gone.epub"))
        assert self._reasons(api, library, book, cfi(1, "ch2"))[0][0] == R.NOT_DOWNLOADED

    def test_unreadable_package(self, api, library, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        (bundle / "OEBPS" / "content.opf").write_text("<package")
        book = library.add_book("Broken", path=str(bundle))
        assert self._reasons(api, library, book, cfi(1, "ch2"))[0][0] == R.UNREADABLE

    def test_resolution_failure_is_a_reason(self, api, library, tmp_path, monkeypatch):
        book = library.add_book("Plain", path=str(_epub_shapes.plain(tmp_path)))

        def fail(self, target):
            raise AppleBooksError("boom")

        monkeypatch.setattr(_Boundaries, "resolve", fail)
        assert self._reasons(api, library, book, cfi(1, "ch2"))[0][:3] == (R.UNREADABLE, 1, "ch2")

    def test_database_errors_propagate(self, api, library, tmp_path, monkeypatch):
        book = library.add_book("Plain", path=str(_epub_shapes.plain(tmp_path)))
        ann = api.get_annotation_by_id(library.add_annotation(book, "x", location=cfi(1, "ch2")))

        def fail(self, target):
            raise DBError("database is locked")

        monkeypatch.setattr(_Boundaries, "resolve", fail)
        with pytest.raises(DBError):
            api.get_annotation_locations([ann])

    def test_results_are_resolved_locations(self, api, library, tmp_path):
        book = library.add_book("Plain", path=str(_epub_shapes.plain(tmp_path)))
        ann = api.get_annotation_by_id(library.add_annotation(book, "x", location=cfi(1, "ch2")))
        (result,) = api.get_annotation_locations([ann]).values()
        assert isinstance(result, ResolvedLocation) and result.unavailable is None
