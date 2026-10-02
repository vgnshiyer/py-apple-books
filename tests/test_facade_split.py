"""The 1.11 facade split (stream 0.2, R5): ``PyAppleBooks`` inherits
one private mixin per theme from ``py_apple_books/_api`` so new methods
can be written in parallel, with no change to any 1.10 method.

- Every 1.10 method stays in api.py; mixins only add new names.
- Every public method, inherited from a mixin or not, is bound once
  (``_bind_library``) and reads the instance's library.
- The annotation scopes are defined once, in models/annotation.py; the
  scope helpers live in ``_api/_common.py`` and api.py re-imports them.
- The new shared helpers in ``_api/_common.py``: ``_book_arg`` (R7),
  ``strict_limit``/``strict_offset`` (R8), the bookmark row helpers
  (R17) and ``_books_by_asset``.
"""

import copy
import datetime as dt
import inspect
import pickle
from decimal import Decimal
from fractions import Fraction

import pytest

from py_apple_books import PyAppleBooks
from py_apple_books import api as api_module
from py_apple_books._api import _common
from py_apple_books._api.book_info import _BookInfoAPI
from py_apple_books._api.engagement import _EngagementAPI
from py_apple_books._api.metadata import _MetadataAPI
from py_apple_books._api.pdf import _PdfAPI
from py_apple_books._api.positions import _PositionsAPI
from py_apple_books._api.reading import _ReadingAPI
from py_apple_books._api.search import _SearchAPI
from py_apple_books.db import LibraryDB, use_library
from py_apple_books.exceptions import BookNotFoundError, InvalidArgumentError, UnsupportedSchemaError
from py_apple_books.models import Annotation, Book
from py_apple_books.models import annotation as annotation_module
from py_apple_books.testing import STORE_SERIES

MIXINS = (_PositionsAPI, _ReadingAPI, _SearchAPI, _MetadataAPI, _EngagementAPI, _BookInfoAPI, _PdfAPI)

# The public names of PyAppleBooks in 1.10.0, all defined in api.py.
# New methods go into the mixins, so this set never changes.
PUBLIC_110 = frozenset({
    "add_book_to_collection", "close", "count_annotations", "count_books_by_status",
    "create_collection", "delete_collection", "get_annotation_by_id",
    "get_annotation_surrounding_text", "get_annotations_by_color", "get_annotations_by_date_range",
    "get_book_by_id", "get_book_by_title", "get_book_content", "get_books_by_genre",
    "get_books_in_progress", "get_collection_by_id", "get_collection_by_title",
    "get_current_reading_chapter", "get_current_reading_location", "get_finished_books",
    "get_library_stats", "get_recently_read_books", "get_unstarted_books", "list_annotations",
    "list_books", "list_collections", "query_deadline", "remove_book_from_collection",
    "rename_collection", "search_annotation_by_highlighted_text", "search_annotation_by_note",
    "search_annotation_by_text", "store_info",
})

# What a class body defines besides its methods (3.13+ adds the last two).
CLASS_DUNDERS = {"__module__", "__qualname__", "__doc__", "__dict__", "__weakref__",
                 "__firstlineno__", "__static_attributes__"}

# The code of the wrapper _bind_library installs (every wrapper shares it).
WRAPPER_CODE = api_module._in_library(lambda self: None).__code__


def public_names(cls) -> set:
    return {name for name in dir(cls) if not name.startswith("_")}


def own_public(cls) -> set:
    return {name for name in vars(cls) if not name.startswith("_")}


def defining_class(name: str) -> type:
    return next(klass for klass in PyAppleBooks.__mro__ if name in vars(klass))


class TestStructure:
    def test_bases_are_the_mixins(self):
        assert PyAppleBooks.__bases__ == MIXINS == api_module._MIXINS
        assert PyAppleBooks.__mro__ == (PyAppleBooks, *MIXINS, object)
        for mixin in MIXINS:
            assert mixin.__module__.startswith("py_apple_books._api.")
            assert mixin.__name__.startswith("_")

    def test_mixins_hold_methods_only(self):
        for mixin in MIXINS:
            assert "__init__" not in vars(mixin) and "__slots__" not in vars(mixin), mixin
            assert "__init_subclass__" not in vars(mixin), mixin
            for name, attr in vars(mixin).items():
                if name in CLASS_DUNDERS:
                    continue
                assert inspect.isfunction(attr) or isinstance(attr, (staticmethod, classmethod, property)), \
                    (mixin.__name__, name)

    def test_1_10_methods_stay_in_api(self):
        assert own_public(PyAppleBooks) == PUBLIC_110

    def test_mixins_add_new_names_only(self):
        seen = set(PUBLIC_110)
        for mixin in MIXINS:
            names = own_public(mixin)
            assert not names & seen, (mixin.__name__, names & seen)
            seen |= names

    def test_public_names_are_1_10_plus_mixin_methods(self):
        added = set().union(*(own_public(mixin) for mixin in MIXINS))
        assert public_names(PyAppleBooks) == PUBLIC_110 | added

    def test_init_subclass_unchanged(self):
        assert "__init_subclass__" in vars(PyAppleBooks)
        assert PyAppleBooks.__init_subclass__.__func__ is vars(PyAppleBooks)["__init_subclass__"].__func__


class TestBinding:
    @pytest.mark.parametrize("name", sorted(public_names(PyAppleBooks)))
    def test_every_public_method_is_bound_once(self, name):
        """Inherited or not: the function the class defines is the
        _in_library wrapper around the original, with its name,
        docstring and signature."""
        attr = vars(defining_class(name))[name]
        assert inspect.isfunction(attr), name
        assert attr.__code__ is WRAPPER_CODE, name
        original = attr.__wrapped__
        assert original.__code__ is not WRAPPER_CODE, f"{name} is wrapped twice"
        assert (attr.__name__, attr.__qualname__, attr.__doc__) == (
            original.__name__, original.__qualname__, original.__doc__)
        assert inspect.signature(attr) == inspect.signature(original)
        assert getattr(PyAppleBooks, name) is attr

    def test_signatures_are_the_originals(self):
        assert list(inspect.signature(PyAppleBooks.get_finished_books).parameters) == [
            "self", "limit", "order_by", "offset", "finished_after", "finished_before"]

    def test_a_bound_mixin_method_reads_the_instance_library(self, library, make_library):
        """The mechanism api.py applies to every mixin: a mixin's method,
        bound with _bind_library and inherited by a facade, runs with the
        instance's library current."""
        library.add_book("default book")
        other = make_library()
        other.add_book("other book")

        class ProbeAPI:
            def probe_titles(self):
                return [b.title for b in Book.manager.all()]

            def _private(self):
                return [b.title for b in Book.manager.all()]

        api_module._bind_library(ProbeAPI)
        assert ProbeAPI.probe_titles.__code__ is WRAPPER_CODE
        assert not hasattr(ProbeAPI._private, "__wrapped__")

        class Facade(PyAppleBooks, ProbeAPI):
            pass

        own = Facade(data_dir=other.data_dir)
        try:
            assert own.probe_titles() == ["other book"]
            assert own.list_books()[0].title == "other book"
        finally:
            own.close()
        assert Facade().probe_titles() == ["default book"]
        # Facade's own vars are bound by __init_subclass__; the
        # inherited method was not wrapped again.
        assert "probe_titles" not in vars(Facade)


def test_every_drift_case_reads_the_instance_library(library, make_library):
    """At run time: every registered facade call (the drift cases cover
    every public method) gives the same result on PyAppleBooks(data_dir)
    as on PyAppleBooks() inside use_library, while the default library
    holds other rows. An unbound method would read the default one."""
    from tests import drift_cases
    from tests.test_schema_drift import seed

    library.add_book("A default library book")
    lib = make_library()
    rows = seed(lib)
    db = LibraryDB(data_dir=lib.data_dir)
    try:
        with use_library(db):
            expected = drift_cases.everything(PyAppleBooks(), rows)
    finally:
        db.close()
    own = PyAppleBooks(data_dir=lib.data_dir)
    try:
        assert drift_cases.everything(own, rows) == expected
    finally:
        own.close()
    # The control: the default library gives other results.
    default = PyAppleBooks()
    assert {label: drift_cases.outcome(lambda: case(default, rows))
            for label, case in drift_cases.cases().items()} != expected


class TestScopes:
    def test_scopes_are_defined_once(self):
        assert api_module._LIVE_ANNOTATIONS is annotation_module._LIVE_ANNOTATIONS
        assert api_module._ALL_ANNOTATIONS is annotation_module._ALL_ANNOTATIONS
        assert annotation_module._LIVE_ANNOTATIONS == {"type__gt": 0, "type__ne": 3, "is_deleted__isnot": 1}
        assert annotation_module._ALL_ANNOTATIONS == {"type__ne": 3}
        for scope in (annotation_module._LIVE_ANNOTATIONS, annotation_module._ALL_ANNOTATIONS):
            assert all(type(value) is int for value in scope.values())

    def test_book_annotations_use_the_live_scope(self):
        relation = vars(Book)["annotations"]
        assert relation.extra_filters == annotation_module._LIVE_ANNOTATIONS
        assert list(relation.extra_filters) == list(annotation_module._LIVE_ANNOTATIONS)

    @pytest.mark.parametrize("name", ["_annotation_scope", "_owned_books_filter", "_book_scope", "_id_text"])
    def test_moved_helpers_are_reimported(self, name):
        assert getattr(api_module, name) is getattr(_common, name)

    def test_scope_helper_returns_a_copy(self):
        scope = _common._annotation_scope(False)
        assert scope == annotation_module._LIVE_ANNOTATIONS
        assert scope is not annotation_module._LIVE_ANNOTATIONS
        assert _common._annotation_scope(True) is not annotation_module._ALL_ANNOTATIONS


# -- the new shared helpers ---------------------------------------------------

UTC = dt.timezone.utc
INT64_MAX = 2**63 - 1


def day(n: int) -> dt.datetime:
    return dt.datetime(2026, 9, n, 12, 0, tzinfo=UTC)


def cfi(step: int) -> str:
    """A CFI into spine item ``step`` (a /6/ path)."""
    return f"epubcfi(/6/{step}[c{step}]!/4/2,/1:0,/1:5)"


def statements(trace, table: str) -> list:
    return [sql for sql, _ in trace if f"FROM {table}" in sql or f"FROM anno_db.{table}" in sql]


class Index:
    """An integer type that isn't an int, like numpy.int64."""

    def __init__(self, value):
        self.value = value

    def __index__(self):
        return self.value


@pytest.fixture
def two_libraries(library, make_library):
    """The default (session) library and another one, each with a book
    of id 1; only the other has id 2."""
    mine = library.add_book("default book", path="/nowhere/default.epub")
    lib = make_library()
    theirs = lib.add_book("other book")
    extra = lib.add_book("other only")
    api = PyAppleBooks(data_dir=lib.data_dir)
    assert mine["id"] == theirs["id"] == 1 and extra["id"] == 2
    yield api
    api.close()


class TestBookArg:
    def test_an_id_is_looked_up(self, api, two_libraries, sql_trace):
        book = _common._book_arg(1)
        assert isinstance(book, Book) and book.title == "default book"
        assert _common._book_arg("1").id == 1
        assert _common._book_arg(Index(1)).id == 1
        assert len(statements(sql_trace, "ZBKLIBRARYASSET")) == 3

    def test_an_unknown_id_raises(self, api, two_libraries):
        with pytest.raises(BookNotFoundError, match="No book with id 2"):
            _common._book_arg(2)
        with pytest.raises(BookNotFoundError):
            _common._book_arg(None)

    def test_a_book_from_this_library_is_used_as_is(self, api, two_libraries, sql_trace):
        book = api.get_book_by_id(1)
        before = len(sql_trace)
        assert _common._book_arg(book) is book
        assert _common._book_arg(book, needs=("path", "title")) is book
        assert len(sql_trace) == before

    def test_a_book_from_another_library_is_reread_here(self, api, two_libraries, sql_trace):
        theirs = two_libraries.get_book_by_id(1)
        assert theirs.title == "other book"
        before = len(sql_trace)
        mine = _common._book_arg(theirs)
        assert (mine.id, mine.title) == (1, "default book")
        assert len(sql_trace) == before + 1
        with pytest.raises(BookNotFoundError):
            _common._book_arg(two_libraries.get_book_by_id(2))
        # Inside the other library (as in its own facade methods) it is
        # the current one, so its book is used as is.
        with use_library(two_libraries._PyAppleBooks__library):
            assert _common._book_arg(theirs) is theirs
            assert _common._book_arg(mine).title == "other book"

    def test_a_book_without_a_library_is_reread(self, api, two_libraries):
        book = api.get_book_by_id(1)
        for detached in (pickle.loads(pickle.dumps(book)), copy.copy(book)):
            found = _common._book_arg(detached)
            assert found is not detached and found.title == "default book"

    def test_a_needed_field_left_out_by_only_is_reread_once(self, api, two_libraries, sql_trace):
        [partial] = Book.manager.all(only=["id", "asset_id", "title"])
        assert partial.path is None
        before = len(sql_trace)
        assert _common._book_arg(partial) is partial
        full = _common._book_arg(partial, needs=("path",))
        assert full is not partial and str(full.path) == "/nowhere/default.epub"
        assert len(sql_trace) == before + 1

    def test_a_field_null_in_the_store_is_reread_once_and_returned(self, library, sql_trace):
        library.add_book("no file", path=None)
        [book] = Book.manager.all()
        before = len(sql_trace)
        again = _common._book_arg(book, needs=("path",))
        assert again is not book and again.id == book.id and again.path is None
        assert len(sql_trace) == before + 1

    def test_get_book_does_the_lookups(self, api, two_libraries):
        calls = []

        def stub(book_id):
            calls.append(book_id)
            return "stubbed"

        assert _common._book_arg(7, get_book=stub) == "stubbed"
        assert _common._book_arg(two_libraries.get_book_by_id(2), get_book=stub) == "stubbed"
        mine = api.get_book_by_id(1)
        assert _common._book_arg(mine, get_book=stub) is mine
        assert calls == [7, 2]

    def test_get_book_by_id_is_unchanged(self, api, two_libraries):
        assert api.get_book_by_id(1).title == "default book"
        with pytest.raises(BookNotFoundError, match=r"^No book with id 2\.$") as exc:
            api.get_book_by_id(2)
        assert isinstance(exc.value, IndexError) and exc.value.__suppress_context__


class TestStrictLimit:
    @pytest.mark.parametrize("value, expected", [
        (None, None), (1, 1), (20, 20), (Index(3), 3), (5.0, 5), (Decimal("5"), 5),
        (Fraction(10, 2), 5), (INT64_MAX, INT64_MAX), (2**64, INT64_MAX), (10**30, INT64_MAX),
    ])
    def test_accepted(self, value, expected):
        got = _common.strict_limit(value)
        assert got == expected and (got is None or type(got) is int)

    @pytest.mark.parametrize("value, message", [
        (0, "at least 1"), (-1, "at least 1"), (-(10**30), "at least 1"), (Index(0), "at least 1"),
        (True, "not a bool"), (False, "not a bool"),
        ("5", "not str"), (b"5", "not bytes"), ([5], "not list"), (object(), "not object"),
        (5.5, "whole number"), (float("nan"), "whole number"), (float("inf"), "whole number"),
        (Decimal("NaN"), "whole number"), (Decimal("2.5"), "whole number"), (1j, "whole number"),
    ])
    def test_refused(self, value, message):
        with pytest.raises(InvalidArgumentError, match=message) as exc:
            _common.strict_limit(value)
        assert str(exc.value).startswith("limit must be")
        assert exc.value.__context__ is None or isinstance(exc.value.__context__, TypeError)

    def test_messages_never_echo_the_value(self):
        for value in ("secret-string-123", 123456.5, -987654):
            with pytest.raises(InvalidArgumentError) as exc:
                _common.strict_limit(value)
            assert str(abs(value) if isinstance(value, (int, float)) else value) not in str(exc.value)

    def test_name_is_used_in_messages(self):
        with pytest.raises(InvalidArgumentError, match=r"^sample_size must be at least 1"):
            _common.strict_limit(0, name="sample_size")

    def test_no_deprecation_path(self, recwarn):
        with pytest.raises(InvalidArgumentError):
            _common.strict_limit(0)
        assert not recwarn.list


class TestStrictOffset:
    @pytest.mark.parametrize("value, expected", [
        (None, None), (0, 0), (3, 3), (Index(2), 2), (4.0, 4), (Decimal("0"), 0), (2**70, INT64_MAX),
    ])
    def test_accepted(self, value, expected):
        assert _common.strict_offset(value) == expected

    @pytest.mark.parametrize("value, message", [
        (-1, "0 or more"), (True, "not a bool"), ("1", "not str"), (1.5, "whole number"),
        (float("nan"), "whole number"),
    ])
    def test_refused(self, value, message):
        with pytest.raises(InvalidArgumentError, match=message) as exc:
            _common.strict_offset(value)
        assert str(exc.value).startswith("offset must be")


@pytest.fixture
def two_books(library):
    return library.add_book("Reading Book"), library.add_book("Other Book")


class TestReadingBookmarks:
    @pytest.fixture
    def rows(self, library, two_books):
        book, other = two_books
        add = library.add_annotation
        return {
            "old": add(book, None, kind="reading_position", modified=day(2), location=cfi(4)),
            "new": add(book, None, kind="reading_position", modified=day(5), location=cfi(6)),
            "tie_low": add(book, None, kind="reading_position", modified=day(3), location=cfi(8)),
            "tie_high": add(book, None, kind="reading_position", modified=day(3), location=cfi(10)),
            "null_flag": add(book, None, kind="reading_position", modified=day(4),
                             raw={"ZANNOTATIONDELETED": None}),
            "no_date": add(book, None, kind="reading_position",
                           raw={"ZANNOTATIONMODIFICATIONDATE": None}),
            # Not reading positions of this book:
            "deleted": add(book, None, kind="reading_position", modified=day(9), deleted=True),
            "other_book": add(other, None, kind="reading_position", modified=day(9)),
            "highlight": add(book, "a highlight", modified=day(9), location=cfi(4)),
            "bookmark": add(book, None, kind="bookmark", modified=day(9), location=cfi(4)),
            "tombstone": add(None, None, kind="tombstone"),
        }

    def test_live_rows_newest_modified_first_then_higher_id(self, two_books, rows, sql_trace):
        result = _common._reading_bookmarks(two_books[0]["asset_id"])
        assert [a.id for a in result] == [rows[k] for k in (
            "new", "null_flag", "tie_high", "tie_low", "old", "no_date")]
        assert all(a.type == 3 for a in result)
        [sql] = statements(sql_trace, "ZAEANNOTATION")
        assert sql.endswith("ORDER BY ZANNOTATIONMODIFICATIONDATE DESC, Z_PK DESC")

    def test_limit_and_only(self, two_books, rows):
        [newest] = _common._reading_bookmarks(two_books[0]["asset_id"], limit=1, only=["location"])
        assert newest.id == rows["new"] and newest.location.cfi == cfi(6)
        assert newest.modification_date is None and newest.type is None

    def test_no_asset_id_runs_nothing(self, rows, sql_trace):
        for asset in (None, ""):
            result = _common._reading_bookmarks(asset, limit=1)
            assert list(result) == [] and len(result) == 0 and result.count() == 0
        assert statements(sql_trace, "ZAEANNOTATION") == []

    def test_unknown_asset(self, rows):
        assert list(_common._reading_bookmarks("NO-SUCH-ASSET")) == []

    def test_storage_order_without_the_modification_date(self, make_library, sql_trace):
        lib = make_library()
        book = lib.add_book("Book")
        ids = [lib.add_annotation(book, None, kind="reading_position", modified=day(n)) for n in (3, 1, 2)]
        lib.execute("annotations", "ALTER TABLE ZAEANNOTATION RENAME COLUMN ZANNOTATIONMODIFICATIONDATE TO GONE")
        db = LibraryDB(data_dir=lib.data_dir)
        try:
            with use_library(db):
                assert not Annotation.manager.has_fields("modification_date")
                assert sorted(a.id for a in _common._reading_bookmarks(book["asset_id"])) == ids
                assert len(list(_common._reading_bookmarks(book["asset_id"], limit=1))) == 1
        finally:
            db.close()
        assert not any("ORDER BY" in sql for sql in statements(sql_trace, "ZAEANNOTATION"))

    def test_a_missing_deleted_flag_raises(self, make_library):
        lib = make_library()
        book = lib.add_book("Book")
        lib.add_annotation(book, None, kind="reading_position")
        lib.execute("annotations", "ALTER TABLE ZAEANNOTATION RENAME COLUMN ZANNOTATIONDELETED TO GONE")
        db = LibraryDB(data_dir=lib.data_dir)
        try:
            with use_library(db), pytest.raises(UnsupportedSchemaError, match="ZANNOTATIONDELETED"):
                list(_common._reading_bookmarks(book["asset_id"]))
        finally:
            db.close()


class TestRecentLocatedAnnotations:
    @pytest.fixture
    def rows(self, library, two_books):
        book, other = two_books
        add = library.add_annotation
        return {
            "first": add(book, "first", created=day(1), location=cfi(4)),
            "bookmark": add(book, None, kind="bookmark", created=day(3), location=cfi(6)),
            "tie_low": add(book, "tie a", created=day(2), location=cfi(8)),
            "tie_high": add(book, "tie b", created=day(2), location=cfi(8)),
            "null_flag": add(book, "no flag", created=day(4), location=cfi(10),
                             raw={"ZANNOTATIONDELETED": None}),
            "note": add(book, "noted", kind="note", created=day(5), location=cfi(12)),
            # Left out:
            "deleted": add(book, "deleted", deleted=True, created=day(9), location=cfi(14)),
            "no_location": add(book, "no location", created=day(9)),
            "not_spine": add(book, "not spine", created=day(9), location="epubcfi(/2/4!/4/2,:0,:3)"),
            "sixty": add(book, "sixty", created=day(9), location="epubcfi(/60/2!/4/2,:0,:3)"),
            "short": add(book, "short", created=day(9), location="epubcfi(/6"),
            "position": add(book, None, kind="reading_position", created=day(9), location=cfi(16)),
            "other_book": add(other, "other", created=day(9), location=cfi(4)),
            "tombstone": add(None, None, kind="tombstone"),
        }

    def test_live_located_rows_newest_created_first_then_higher_id(self, two_books, rows, sql_trace):
        result = _common._recent_located_annotations(two_books[0]["asset_id"])
        assert [a.id for a in result] == [rows[k] for k in (
            "note", "null_flag", "bookmark", "tie_high", "tie_low", "first")]
        assert all(a.location.cfi.startswith("epubcfi(/6/") for a in result)
        [sql] = statements(sql_trace, "ZAEANNOTATION")
        assert "ZANNOTATIONLOCATION >= ? AND ZANNOTATIONLOCATION < ?" in sql
        assert sql.endswith("ORDER BY ZANNOTATIONCREATIONDATE DESC, Z_PK DESC")

    def test_limit_and_only(self, two_books, rows):
        got = _common._recent_located_annotations(two_books[0]["asset_id"], limit=2,
                                                  only=["location", "creation_date"])
        assert [a.id for a in got] == [rows["note"], rows["null_flag"]]
        assert all(a.selected_text is None and a.creation_date is not None for a in got)

    def test_no_asset_id_runs_nothing(self, rows, sql_trace):
        assert list(_common._recent_located_annotations(None, limit=5)) == []
        assert statements(sql_trace, "ZAEANNOTATION") == []

    def test_storage_order_without_the_creation_date(self, make_library, sql_trace):
        lib = make_library()
        book = lib.add_book("Book")
        ids = [lib.add_annotation(book, f"h{n}", created=day(n), location=cfi(4)) for n in (3, 1, 2)]
        lib.execute("annotations", "ALTER TABLE ZAEANNOTATION RENAME COLUMN ZANNOTATIONCREATIONDATE TO GONE")
        db = LibraryDB(data_dir=lib.data_dir)
        try:
            with use_library(db):
                assert sorted(a.id for a in _common._recent_located_annotations(book["asset_id"])) == ids
        finally:
            db.close()
        assert not any("ORDER BY" in sql for sql in statements(sql_trace, "ZAEANNOTATION"))


class TestBooksByAsset:
    def test_every_row_lowest_id_per_asset(self, library, sql_trace):
        first = library.add_book("First")
        library.add_book("Same Asset", asset_id=first["asset_id"])
        series = library.add_book("Series", data_source=STORE_SERIES, content_type=5)
        library.add_book("No Asset", raw={"ZASSETID": None})
        books = _common._books_by_asset()
        assert {asset: (b.id, b.title) for asset, b in books.items()} == {
            first["asset_id"]: (first["id"], "First"), series["asset_id"]: (series["id"], "Series")}
        assert all(b.path is None and b.author is None for b in books.values())
        [sql] = statements(sql_trace, "ZBKLIBRARYASSET")
        assert sql.endswith("ORDER BY Z_PK ASC")

    def test_only(self, library):
        book = library.add_book("Book", path="/nowhere/book.epub")
        [found] = _common._books_by_asset(only=("id", "asset_id", "path")).values()
        assert (found.id, str(found.path), found.title) == (book["id"], "/nowhere/book.epub", None)

    def test_library_stats_reads_it(self, api, library, monkeypatch):
        book = library.add_book("Book")
        library.add_annotation(book, "a highlight")
        library.add_annotation("GONE-ASSET", "an orphan")
        calls = []
        real = _common._books_by_asset

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real(*args, **kwargs)

        monkeypatch.setattr(api_module, "_books_by_asset", spy)
        stats = api.get_library_stats()
        assert calls == [((), {})]
        assert (stats.total_annotations, stats.orphan_annotations) == (2, 1)
        assert stats.annotations_per_book == ((book["id"], "Book", 1),)
