"""The 1.11 facade split (stream 0.2, R5): ``PyAppleBooks`` inherits
one private mixin per theme from ``py_apple_books/_api`` so new methods
can be written in parallel, with no change to any 1.10 method.

- Every 1.10 method stays in api.py; mixins only add new names.
- Every public method, inherited from a mixin or not, is bound once
  (``_bind_library``) and reads the instance's library.
- The annotation scopes are defined once, in models/annotation.py; the
  scope helpers live in ``_api/_common.py`` and api.py re-imports them.
"""

import inspect

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
from py_apple_books.models import Book
from py_apple_books.models import annotation as annotation_module

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
            "self", "limit", "order_by", "offset"]

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
