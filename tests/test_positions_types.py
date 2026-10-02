"""``py_apple_books.positions``: the value types of the 1.11 reading
position, annotation location and context, and spoiler-safe reading APIs.

Pure value-type tests: no library, no book files. The APIs that return
these types test them in use.
"""

import ast
import copy
import dataclasses
import enum
import json
import os
import pathlib
import pickle
import random
import sys
from datetime import datetime, timezone

import pytest

import py_apple_books
from py_apple_books import exceptions as ex
from py_apple_books import positions as P
from py_apple_books.content import Chapter
from py_apple_books.models.location import Location
from py_apple_books.utils import snap_window
from tests import _fs_audit

NAMES = [
    "ChapterMatch", "ResolvedLocation", "UnavailableReason", "PositionSource",
    "ReadingPosition", "TextMatch", "AnnotationContext", "TextPosition",
    "BoundarySource", "BoundaryPrecision", "BoundaryWarning", "ReadBoundary",
    "ResolvedBoundary",
]

CHAPTER = Chapter(id="c3", title="Three", href="OEBPS/c3.xhtml", fragment="", order=3, depth=0)
WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def roundtrips(value):
    """``value`` survives pickling (every protocol), ``copy.copy`` and
    ``copy.deepcopy`` equal, and hashes like its copies."""
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        back = pickle.loads(pickle.dumps(value, protocol))
        assert back == value and type(back) is type(value)
        assert hash(back) == hash(value)
    for clone in (copy.copy(value), copy.deepcopy(value)):
        assert clone == value and hash(clone) == hash(value)
    return True


def field_names(cls):
    return [f.name for f in dataclasses.fields(cls)]


# -- the module -----------------------------------------------------------------


def test_public_names():
    assert P.__all__ == NAMES
    for name in NAMES:
        assert getattr(P, name).__module__ == "py_apple_books.positions"


def test_not_in_the_top_level_namespace():
    """Imported from their home module (like ``Chapter``), not from
    ``py_apple_books``."""
    for name in NAMES:
        assert name not in py_apple_books.__all__
        assert not hasattr(py_apple_books, name)


def _imports(nodes):
    for node in nodes:
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module


def test_module_imports_only_the_stdlib_and_exceptions():
    """Pure: at import, only the stdlib and the exceptions module. The
    book-content module (``Chapter``) and the models (``Location``) are
    imported for type checking only, so neither can form an import cycle
    with this module and it never pulls in ebooklib or bs4."""
    tree = ast.parse(pathlib.Path(P.__file__).read_text(encoding="utf-8"))
    top, typing_only = [], []
    for node in tree.body:
        if isinstance(node, ast.If) and getattr(node.test, "id", None) == "TYPE_CHECKING":
            typing_only.extend(_imports(node.body))
        else:
            top.extend(_imports([node]))
    package = [m for m in top if m.split(".")[0] == "py_apple_books"]
    assert package == ["py_apple_books.exceptions"]
    for module in top:
        root = module.split(".")[0]
        assert root == "py_apple_books" or root in sys.stdlib_module_names, module
    assert "py_apple_books.content" in typing_only
    # Anywhere in the module (functions included): never ebooklib or bs4,
    # and the content module for type checking only.
    every = list(_imports(ast.walk(tree)))
    assert not {"ebooklib", "bs4"} & {m.split(".")[0] for m in every}
    assert every.count("py_apple_books.content") == typing_only.count("py_apple_books.content")


# -- enums ------------------------------------------------------------------------

ENUMS = {
    P.ChapterMatch: {
        "FILE": "file", "ANCHOR": "anchor", "PRECEDING": "preceding",
        "FRONT_MATTER": "front_matter", "SECTION_UNKNOWN": "section_unknown",
    },
    P.UnavailableReason: {
        "NO_LOCATION": "no_location", "NO_HIGHLIGHT_TEXT": "no_highlight_text",
        "ORPHANED": "orphaned", "NOT_OWNED": "not_owned",
        "NOT_DOWNLOADED": "not_downloaded", "DRM": "drm", "NOT_EPUB": "not_epub",
        "UNREADABLE": "unreadable", "CHAPTER_NOT_FOUND": "chapter_not_found",
        "EMPTY_CHAPTER": "empty_chapter", "HIGHLIGHT_NOT_FOUND": "highlight_not_found",
    },
    P.PositionSource: {"BOOKMARK": "bookmark", "RECENT_ANNOTATION": "recent_annotation"},
    P.TextMatch: {
        "EXACT": "exact", "WHITESPACE": "whitespace", "INVISIBLE": "invisible",
        "FOLDED": "folded",
    },
    P.BoundarySource: {
        "READING_POSITION": "reading_position", "RECENT_HIGHLIGHT": "recent_highlight",
        "PROGRESS": "progress", "FURTHEST": "furthest", "NONE": "none",
    },
    P.BoundaryPrecision: {
        "EXACT": "exact", "APPROXIMATE": "approximate", "SPINE_ITEM": "spine_item",
        "START": "start",
    },
    P.BoundaryWarning: {
        "BOOKMARK_UNRESOLVED": "bookmark_unresolved",
        "BOOKMARK_NONLINEAR": "bookmark_nonlinear",
        "BOOKMARK_TOC_PAGE": "bookmark_toc_page",
        "BOOKMARK_INDEX_MISMATCH": "bookmark_index_mismatch",
        "MULTIPLE_BOOKMARKS": "multiple_bookmarks",
        "HIGHLIGHT_UNRESOLVED": "highlight_unresolved",
        "SCHEMA_MISSING_COLUMNS": "schema_missing_columns",
        "ANNOTATIONS_UNAVAILABLE": "annotations_unavailable",
    },
}


@pytest.mark.parametrize("cls", list(ENUMS), ids=lambda c: c.__name__)
def test_enum_members_and_values(cls):
    assert issubclass(cls, str) and issubclass(cls, enum.Enum)
    assert {m.name: m.value for m in cls} == ENUMS[cls]


@pytest.mark.parametrize("cls", list(ENUMS), ids=lambda c: c.__name__)
def test_enum_members_read_as_their_values(cls):
    for name, value in ENUMS[cls].items():
        member = cls[name]
        assert cls(value) is member
        assert member == value and str(member) == value
        assert f"{member}" == value and "%s" % member == value
        assert json.dumps(member) == json.dumps(value)
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            assert pickle.loads(pickle.dumps(member, protocol)) is member
        assert copy.copy(member) is member and copy.deepcopy(member) is member


def test_context_error_reasons_are_unavailable_reasons():
    """``ContextUnavailableError``'s reason constants are the matching
    ``UnavailableReason`` values."""
    for name in ("NO_LOCATION", "NO_HIGHLIGHT_TEXT", "ORPHANED", "EMPTY_CHAPTER",
                 "HIGHLIGHT_NOT_FOUND"):
        assert getattr(ex.ContextUnavailableError, name) == P.UnavailableReason[name].value


# -- UnavailableReason.of -----------------------------------------------------------

R = P.UnavailableReason
CUE = ex.ContextUnavailableError


class _MyDrm(ex.DRMProtectedError):
    pass


class _MyContext(ex.ContextUnavailableError):
    pass


class _ContextOfMissingBook(ex.ContextUnavailableError, ex.BookNotDownloadedError):
    pass


class _UnownedAndDrm(ex.NotInLibraryError, ex.DRMProtectedError):
    pass


class _DrmAndNotEpub(ex.DRMProtectedError, ex.NotEpubError):
    pass


class _BadStr(str):
    def __hash__(self):
        raise RuntimeError("no hash")

    def __eq__(self, other):
        raise RuntimeError("no eq")


OF_TABLE = [
    # ContextUnavailableError: its reason, as a constant, a member or any member value.
    (CUE("m", CUE.NO_LOCATION), R.NO_LOCATION),
    (CUE("m", CUE.NO_HIGHLIGHT_TEXT, 7), R.NO_HIGHLIGHT_TEXT),
    (CUE("m", reason=CUE.ORPHANED), R.ORPHANED),
    (CUE("m", CUE.EMPTY_CHAPTER), R.EMPTY_CHAPTER),
    (CUE("m", CUE.HIGHLIGHT_NOT_FOUND), R.HIGHLIGHT_NOT_FOUND),
    (CUE("m", R.EMPTY_CHAPTER), R.EMPTY_CHAPTER),
    (CUE("m", "drm"), R.DRM),
    (_MyContext("m", CUE.ORPHANED), R.ORPHANED),
    # ... without a usable reason: the rules for other errors.
    (CUE("m"), R.UNREADABLE),
    (CUE("m", "not_a_reason"), R.UNREADABLE),
    (CUE("m", "NO_LOCATION"), R.UNREADABLE),   # a name, not a value
    (CUE("m", 3), R.UNREADABLE),
    (CUE("m", _BadStr("no_location")), R.UNREADABLE),
    (_ContextOfMissingBook("m", CUE.ORPHANED), R.ORPHANED),
    (_ContextOfMissingBook("m"), R.NOT_DOWNLOADED),
    # Book-level errors; a subclass before its base.
    (ex.NotInLibraryError("m"), R.NOT_OWNED),
    (ex.BookNotDownloadedError("m"), R.NOT_DOWNLOADED),
    (ex.DRMProtectedError("m"), R.DRM),
    (_MyDrm("m"), R.DRM),
    (ex.NotEpubError("m"), R.NOT_EPUB),
    (ex.ChapterNotFoundError("m"), R.CHAPTER_NOT_FOUND),
    (_UnownedAndDrm("m"), R.NOT_OWNED),
    (_DrmAndNotEpub("m"), R.DRM),
    # Database errors are not reasons about the book.
    (ex.DBError("m"), None),
    (ex.DBConnectionError("m"), None),
    (ex.LibraryNotFoundError("m"), None),
    (ex.AnnotationStoreNotFoundError("m"), None),
    (ex.LibraryAccessDeniedError("m"), None),
    (ex.DBQueryError("m"), None),
    (ex.UnsupportedSchemaError("m", table="T", column="C"), None),
    (ex.QueryTimeoutError("m", timeout=1.0), None),
    (ex.AmbiguousStoreError("m"), None),
    # Any other library error.
    (ex.AppleBooksError("m"), R.UNREADABLE),
    (ex.UnsafeEpubEntryError("m", entry="x"), R.UNREADABLE),
    (ex.WriteError("m"), R.UNREADABLE),
    # Not a library error, or not an exception at all.
    (IndexError("m"), None),   # get_book_content's unknown-id error
    (ValueError("m"), None),
    (OSError(35, "m"), None),
    (KeyboardInterrupt(), None),
    (None, None),
    ("drm", None),
    (R.DRM, None),
    (ex.DRMProtectedError, None),   # the class, not an instance
]


@pytest.mark.parametrize("exc,expected", OF_TABLE, ids=lambda v: type(v).__name__)
def test_of(exc, expected):
    assert P.UnavailableReason.of(exc) is expected


def test_of_after_pickling():
    e = pickle.loads(pickle.dumps(CUE("m", CUE.HIGHLIGHT_NOT_FOUND, annotation_id=4)))
    assert P.UnavailableReason.of(e) is R.HIGHLIGHT_NOT_FOUND


def test_of_a_raised_and_caught_error():
    try:
        raise ex.NotInLibraryError("m")
    except ex.BookNotDownloadedError as caught:   # a 1.10 handler
        assert P.UnavailableReason.of(caught) is R.NOT_OWNED


# -- TextPosition ---------------------------------------------------------------------

MAX_SPINE = 999_999_999
MAX_OFFSET = 999_999_999_999


class _Int(int):
    pass


class _Num(enum.IntEnum):
    THREE = 3


def test_text_position_fields():
    assert field_names(P.TextPosition) == ["spine_index", "offset"]
    assert P.TextPosition(4) == P.TextPosition(4, 0) == P.TextPosition(spine_index=4, offset=0)
    p = P.TextPosition(2, 17)
    assert (p.spine_index, p.offset) == (2, 17)
    assert repr(p) == "TextPosition(spine_index=2, offset=17)"


@pytest.mark.parametrize("args", [(0, 0), (MAX_SPINE, 0), (0, MAX_OFFSET), (MAX_SPINE, MAX_OFFSET)])
def test_text_position_limits(args):
    p = P.TextPosition(*args)
    assert (p.spine_index, p.offset) == args
    assert P.TextPosition.parse(str(p)) == p


def test_text_position_int_subclasses_are_stored_as_ints():
    p = P.TextPosition(_Int(3), _Num.THREE)
    assert type(p.spine_index) is int and type(p.offset) is int
    assert p == P.TextPosition(3, 3) and str(p) == "3:3"


BAD_FIELDS = [
    (-987654, 0), (0, -987654), (True, 0), (0, False), (0, 1.5), (1.0, 0), ("SECRET", 0),
    (None, 0), (0, None), (MAX_SPINE + 1, 0), (0, MAX_OFFSET + 1), (10 ** 30 + 987654, 0),
    (b"7", 0), (0, [1]),
]


@pytest.mark.parametrize("args", BAD_FIELDS, ids=repr)
def test_text_position_validation(args):
    with pytest.raises(ex.InvalidArgumentError) as info:
        P.TextPosition(*args)
    assert isinstance(info.value, ValueError)
    message = str(info.value)
    assert message.startswith("TextPosition.")
    for value in args:
        if value not in (0, None, True, False):
            assert repr(value) not in message and str(value) not in message


def test_text_position_is_frozen():
    p = P.TextPosition(1, 2)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.offset = 3


def test_text_position_order():
    a, b, c, d = (P.TextPosition(0, 50), P.TextPosition(1, 0), P.TextPosition(1, 9),
                  P.TextPosition(10, 0))
    assert a < b < c < d and d > c >= c > b > a
    assert sorted([d, b, a, c]) == [a, b, c, d]
    assert max([b, d, a]) is d and min([b, d, a]) is a
    # Numeric, not text, order: 10 comes after 9.
    assert P.TextPosition(9, 0) < P.TextPosition(10, 0)
    assert P.TextPosition(1, 9) < P.TextPosition(1, 10)


def test_text_position_compares_only_with_text_positions():
    p = P.TextPosition(1, 2)
    assert p != (1, 2) and p != "1:2"
    with pytest.raises(TypeError):
        p < (1, 3)
    with pytest.raises(TypeError):
        p < "1:3"


def test_text_position_hash_and_copies():
    p = P.TextPosition(5, 6)
    assert hash(p) == hash(P.TextPosition(5, 6))
    assert {p: 1}[P.TextPosition(5, 6)] == 1
    assert len({p, P.TextPosition(5, 6), P.TextPosition(6, 5)}) == 2
    assert roundtrips(p)


def test_text_position_str_and_parse():
    assert str(P.TextPosition(12, 345)) == "12:345"
    assert P.TextPosition.parse("12:345") == P.TextPosition(12, 345)
    assert P.TextPosition.parse("0:0") == P.TextPosition(0, 0)
    assert P.TextPosition.parse("007:0010") == P.TextPosition(7, 10)   # leading zeros
    assert P.TextPosition.parse("9" * 9 + ":" + "9" * 12) == P.TextPosition(MAX_SPINE, MAX_OFFSET)


class _Str(str):
    pass


def test_parse_takes_a_str_subclass():
    assert P.TextPosition.parse(_Str("3:4")) == P.TextPosition(3, 4)


def test_parse_round_trips():
    rng = random.Random(1107)
    for _ in range(int(os.environ.get("APPLE_BOOKS_FUZZ_ITERATIONS", "2000"))):
        spine = rng.choice([0, 1, rng.randrange(10 ** rng.randint(1, 9))])
        offset = rng.choice([0, 1, rng.randrange(10 ** rng.randint(1, 12))])
        p = P.TextPosition(spine, offset)
        text = str(p)
        assert P.TextPosition.parse(text) == p
        assert str(P.TextPosition.parse(text)) == text


BAD_TEXT = [
    "", ":", "1", "1:", ":1", "-1:0", "1:-0", "+1:2", "1:+2", "1:2:3", "1::2", " 1:2", "1:2 ",
    "1:2\n", "\t1:2", "1 :2", "1: 2", "1_0:2", "1,0:2", "0x1:2", "1.0:2", "1e3:2", "1：2",
    "١:٢",                 # Arabic-Indic digits
    "１:２",                 # fullwidth digits
    "१:2",                      # a Devanagari digit
    "1234567890:0",                  # 10 digits of spine index
    "0:1234567890123",               # 13 digits of offset
    "9" * 10_000 + ":0", "0:" + "9" * 10_000, "1:2" * 5000,
    "SECRET:1", "1:SECRET",
]


@pytest.mark.parametrize("text", BAD_TEXT, ids=lambda t: repr(t[:20]))
def test_parse_rejects(text):
    with pytest.raises(ex.InvalidArgumentError) as info:
        P.TextPosition.parse(text)
    # One fixed sentence, whatever the input: the input is never quoted.
    assert str(info.value) == P._PARSE_MESSAGE
    assert "SECRET" not in str(info.value)


@pytest.mark.parametrize("value", [None, 12, 1.5, b"1:2", bytearray(b"1:2"), ["1:2"],
                                   P.TextPosition(1, 2)], ids=repr)
def test_parse_takes_only_text(value):
    with pytest.raises(ex.InvalidArgumentError) as info:
        P.TextPosition.parse(value)
    assert str(info.value) == P._PARSE_MESSAGE


def test_parse_checks_the_length_before_matching(monkeypatch):
    """An oversized input is refused without running the pattern on it."""
    class Refuse:
        def fullmatch(self, text):
            raise AssertionError("matched an oversized input")

    monkeypatch.setattr(P, "_POSITION", Refuse())
    with pytest.raises(ex.InvalidArgumentError):
        P.TextPosition.parse("1" * 1_000_000 + ":1")


def test_parse_message_names_the_format():
    message = P._PARSE_MESSAGE
    assert "'N:M'" in message and "'12:345'" in message


# -- ResolvedLocation, ReadingPosition, AnnotationContext ------------------------------


def test_resolved_location():
    assert field_names(P.ResolvedLocation) == [
        "chapter", "match", "spine_index", "item_id", "unavailable"]
    loc = P.ResolvedLocation(CHAPTER, P.ChapterMatch.ANCHOR, 2, "c3")
    assert loc.unavailable is None
    assert (loc.chapter, loc.match, loc.spine_index, loc.item_id) == (
        CHAPTER, P.ChapterMatch.ANCHOR, 2, "c3")
    gone = P.ResolvedLocation(None, None, 4, "c5", P.UnavailableReason.DRM)
    assert gone.unavailable is P.UnavailableReason.DRM
    assert loc != gone
    assert roundtrips(loc) and roundtrips(gone)
    with pytest.raises(dataclasses.FrozenInstanceError):
        loc.item_id = "x"


READING_POSITION_FIELDS = [
    "book_id", "source", "annotation_id", "updated", "location", "spine_index", "item_id",
    "chapter", "match", "total_chapters", "unavailable", "fraction", "furthest_fraction",
    "page", "page_count", "page_count_estimated",
]


def test_reading_position_fields_and_defaults():
    assert field_names(P.ReadingPosition) == READING_POSITION_FIELDS
    pos = P.ReadingPosition(7, P.PositionSource.BOOKMARK, 70, WHEN,
                            Location("epubcfi(/6/8[c3]!/4/2/1:0)"), 3, "c3")
    assert (pos.chapter, pos.match, pos.total_chapters, pos.unavailable) == (None,) * 4
    assert (pos.fraction, pos.furthest_fraction, pos.page, pos.page_count) == (None,) * 4
    assert pos.page_count_estimated is False


def test_reading_position_values_and_copies():
    full = P.ReadingPosition(
        book_id=7, source=P.PositionSource.RECENT_ANNOTATION, annotation_id=71, updated=WHEN,
        location=Location("epubcfi(/6/8[c3]!/4/2/1:0)"), spine_index=3, item_id="c3",
        chapter=CHAPTER, match=P.ChapterMatch.FILE, total_chapters=12,
        unavailable=None, fraction=0.25, furthest_fraction=0.5, page=None, page_count=None)
    pdf = P.ReadingPosition(8, P.PositionSource.BOOKMARK, 80, None, None, None, None,
                            unavailable=P.UnavailableReason.NOT_EPUB, fraction=0.59,
                            furthest_fraction=0.6, page=286, page_count=485,
                            page_count_estimated=True)
    assert roundtrips(full) and roundtrips(pdf)
    assert full != pdf
    with pytest.raises(dataclasses.FrozenInstanceError):
        full.page = 3


ANNOTATION_CONTEXT_FIELDS = [
    "annotation_id", "book_id", "item_id", "spine_index", "chapter", "match", "before",
    "highlight", "after", "clipped_start", "clipped_end", "text_match", "occurrences",
    "disambiguated",
]


def _context(before="He said ", highlight="yes", after=" twice.", start=False, end=False, **kw):
    return P.AnnotationContext(5, 7, "c3", 2, CHAPTER, P.ChapterMatch.FILE, before, highlight,
                               after, start, end, **kw)


def test_annotation_context_fields_and_defaults():
    assert field_names(P.AnnotationContext) == ANNOTATION_CONTEXT_FIELDS
    ctx = _context()
    assert ctx.text_match is P.TextMatch.EXACT
    assert (ctx.occurrences, ctx.disambiguated) == (1, False)
    other = _context(text_match=P.TextMatch.FOLDED, occurrences=2, disambiguated=True)
    assert (other.text_match, other.occurrences, other.disambiguated) == (
        P.TextMatch.FOLDED, 2, True)
    assert roundtrips(ctx) and roundtrips(other)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.before = ""


@pytest.mark.parametrize("start,end,expected", [
    (False, False, "He said yes twice."),
    (True, False, "…He said yes twice."),
    (False, True, "He said yes twice.…"),
    (True, True, "…He said yes twice.…"),
])
def test_annotation_context_text(start, end, expected):
    ctx = _context(start=start, end=end)
    assert ctx.text == expected and str(ctx) == expected


def test_annotation_context_text_with_empty_parts():
    assert _context(before="", after="").text == "yes"
    assert _context(before="", highlight="", after="", start=True, end=True).text == "……"


def test_annotation_context_text_matches_snap_window():
    """The text reads as 1.10's ``snap_window`` output does for the same
    window: the same ellipsis, at the clipped ends."""
    rng = random.Random(4011)
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]
    checked = 0
    for _ in range(500):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(3, 40)))
        target = f"MARK{rng.randint(0, 99)}"
        cut = rng.randint(0, len(text))
        cut = text.rfind(" ", 0, cut) + 1 if " " in text[:cut] else 0
        text = f"{text[:cut]}{target} {text[cut:]}".strip()
        pos = text.index(target)
        window = snap_window(text, pos, len(target), rng.randint(0, 60), rng.randint(0, 60))
        core = window.removeprefix("…").removesuffix("…")
        if core.count(target) != 1:
            continue
        before, _, after = core.partition(target)
        ctx = _context(before, target, after, window.startswith("…"),
                       window.endswith("…"))
        assert str(ctx) == window
        checked += 1
    assert checked > 400


# -- ReadBoundary ---------------------------------------------------------------------

# A bookmark in paragraph 5 of spine item 3 (/6/8), 20 characters in.
BOOKMARK = Location("epubcfi(/6/8[c3]!/4/10/1:20)")
# A highlight over characters 4..30 of paragraph 6 of the same item.
HIGHLIGHT = Location("epubcfi(/6/8[c3]!/4/12,/1:4,/1:30)")

S = P.BoundarySource
W = P.BoundaryWarning


def _boundary(source, bookmark=BOOKMARK, highlight=HIGHLIGHT, warnings=(), basis="position"):
    return P.ReadBoundary(7, basis, source, bookmark, highlight, 41.5, 55.0, False, warnings)


def test_read_boundary_fields():
    assert field_names(P.ReadBoundary) == [
        "book_id", "basis", "source", "bookmark", "highlight", "progress", "high_water",
        "is_finished", "warnings"]
    # No defaults: every field is given.
    with pytest.raises(TypeError):
        P.ReadBoundary(7, "position", S.NONE, None, None, None, None, None)


def test_read_boundary_values_and_copies():
    b = _boundary(S.READING_POSITION, warnings=(W.MULTIPLE_BOOKMARKS,))
    assert (b.book_id, b.basis, b.source, b.bookmark, b.highlight) == (
        7, "position", S.READING_POSITION, BOOKMARK, HIGHLIGHT)
    assert (b.progress, b.high_water, b.is_finished, b.warnings) == (
        41.5, 55.0, False, (W.MULTIPLE_BOOKMARKS,))
    assert roundtrips(b)
    assert {b: "memo"}[_boundary(S.READING_POSITION, warnings=(W.MULTIPLE_BOOKMARKS,))] == "memo"
    assert b != _boundary(S.READING_POSITION)
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.source = S.NONE


def test_read_boundary_warnings_are_a_tuple():
    given = (W.SCHEMA_MISSING_COLUMNS,)
    assert _boundary(S.NONE, warnings=given).warnings is given
    assert _boundary(S.NONE, warnings=[W.SCHEMA_MISSING_COLUMNS]).warnings == given
    assert _boundary(S.NONE, warnings=(w for w in given)).warnings == given
    assert _boundary(S.NONE, warnings=[]).warnings == ()
    assert hash(_boundary(S.NONE, warnings=[W.MULTIPLE_BOOKMARKS]))


@pytest.mark.parametrize("warnings", [W.MULTIPLE_BOOKMARKS, "multiple_bookmarks", b"x"])
def test_read_boundary_refuses_one_unwrapped_warning(warnings):
    with pytest.raises(ex.InvalidArgumentError):
        _boundary(S.NONE, warnings=warnings)


@pytest.mark.parametrize("source,location,spine_index", [
    (S.READING_POSITION, BOOKMARK, 3),
    (S.FURTHEST, BOOKMARK, 3),
    (S.RECENT_HIGHLIGHT, HIGHLIGHT, 3),
    (S.PROGRESS, None, None),
    (S.NONE, None, None),
    ("reading_position", BOOKMARK, 3),   # the value of a member reads the same
])
def test_read_boundary_location(source, location, spine_index):
    b = _boundary(source)
    assert b.location == location and b.spine_index == spine_index


def test_read_boundary_location_without_one():
    assert _boundary(S.READING_POSITION, bookmark=None).location is None
    assert _boundary(S.READING_POSITION, bookmark=None).spine_index is None
    assert _boundary(S.RECENT_HIGHLIGHT, highlight=None).spine_index is None
    assert _boundary(S.READING_POSITION, bookmark=Location("")).spine_index is None


def L(cfi):
    return Location(f"epubcfi({cfi})")


INCLUDES = [
    # Before the bookmark: read; at or after it: not.
    (S.READING_POSITION, L("/6/8[c3]!/4/8/1:100"), True),       # an earlier paragraph
    (S.READING_POSITION, L("/6/8[c3]!/4/10/1:19"), True),       # same text, earlier offset
    (S.READING_POSITION, L("/6/8[c3]!/4/10/1:20"), False),      # the bookmark's own point
    (S.READING_POSITION, L("/6/8[elsewhere]!/4/10/1:20"), False),   # same point, other hint
    (S.READING_POSITION, L("/6/8[c3]!/4/10/1:21"), False),      # same text, later offset
    (S.READING_POSITION, L("/6/8[c3]!/4/10"), True),            # the paragraph starts before
    (S.READING_POSITION, L("/6/8[c3]!/4/10,/1:10,/1:40"), True),   # a range starting before
    (S.READING_POSITION, L("/6/8[c3]!/4/10,/1:20,/1:40"), False),  # a range starting at it
    (S.READING_POSITION, L("/6/6[c2]!/4/200/1:0"), True),       # an earlier spine item
    (S.READING_POSITION, L("/6/10[c4]!/4/2/1:0"), False),       # a later spine item
    (S.READING_POSITION, L("/6/80!/4/2/1:0"), False),           # numeric order: 80 > 8
    (S.READING_POSITION, L("/4/2/1:0"), False),                 # no spine step
    (S.READING_POSITION, Location(""), False),
    (S.READING_POSITION, Location("not a cfi"), False),
    (S.READING_POSITION, None, False),
    # FURTHEST is placed by the bookmark too.
    (S.FURTHEST, L("/6/8[c3]!/4/10/1:19"), True),
    (S.FURTHEST, L("/6/8[c3]!/4/10/1:20"), False),
    (S.FURTHEST, L("/6/10[c4]!/4/2/1:0"), False),
    # Up to the end of the highlight: read, the highlight included.
    (S.RECENT_HIGHLIGHT, HIGHLIGHT, True),
    (S.RECENT_HIGHLIGHT, L("/6/8[c3]!/4/12/1:4"), True),        # its start
    (S.RECENT_HIGHLIGHT, L("/6/8[c3]!/4/12/1:10"), True),       # inside it
    (S.RECENT_HIGHLIGHT, L("/6/8[c3]!/4/12/1:30"), True),       # its end
    (S.RECENT_HIGHLIGHT, L("/6/8[c3]!/4/12/1:31"), False),      # after it
    (S.RECENT_HIGHLIGHT, L("/6/8[c3]!/4/12,/1:20,/1:90"), True),   # a range starting inside
    (S.RECENT_HIGHLIGHT, L("/6/8[c3]!/4/14/1:0"), False),       # the next paragraph
    (S.RECENT_HIGHLIGHT, L("/6/8[c3]!/4/10/1:500"), True),      # the paragraph before
    (S.RECENT_HIGHLIGHT, L("/6/10[c4]!/4/2/1:0"), False),
    (S.RECENT_HIGHLIGHT, L("/4/2/1:0"), False),
    (S.RECENT_HIGHLIGHT, None, False),
    # Placed by percent or by nothing: never decided from a location.
    (S.PROGRESS, L("/6/2!/4/2/1:0"), False),
    (S.NONE, L("/6/2!/4/2/1:0"), False),
    # A member's value reads as the member.
    ("reading_position", L("/6/8[c3]!/4/10/1:19"), True),
    ("recent_highlight", L("/6/8[c3]!/4/12/1:30"), True),
]


@pytest.mark.parametrize("source,location,expected", INCLUDES,
                         ids=lambda v: str(v) if v is not None else "None")
def test_includes(source, location, expected):
    assert _boundary(source).includes(location) is expected


def test_includes_a_point_highlight():
    """A highlight without a range ends where it starts."""
    point = L("/6/8[c3]!/4/12/1:4")
    b = _boundary(S.RECENT_HIGHLIGHT, highlight=point)
    assert b.includes(point) and b.includes(L("/6/8[c3]!/4/12/1:3"))
    assert not b.includes(L("/6/8[c3]!/4/12/1:5"))


@pytest.mark.parametrize("source,kwargs", [
    (S.READING_POSITION, {"bookmark": None}),
    (S.READING_POSITION, {"bookmark": Location("")}),
    (S.READING_POSITION, {"bookmark": L("/4/2/1:9")}),       # no spine step
    (S.FURTHEST, {"bookmark": None}),                          # the highlight isn't used
    (S.RECENT_HIGHLIGHT, {"highlight": None}),
    (S.RECENT_HIGHLIGHT, {"highlight": Location("")}),
    (S.RECENT_HIGHLIGHT, {"highlight": L("/4/2,/1:0,/1:9")}),
])
def test_includes_without_a_usable_location(source, kwargs):
    b = _boundary(source, **kwargs)
    for location in (L("/6/2!/4/2/1:0"), L("/6/8[c3]!/4/2/1:0"), BOOKMARK, HIGHLIGHT):
        assert b.includes(location) is False


@pytest.mark.parametrize("value", ["epubcfi(/6/2!/4/2/1:0)", ("/6/2",), 3, object()],
                         ids=lambda v: type(v).__name__)
def test_includes_takes_a_location(value):
    with pytest.raises(ex.InvalidArgumentError) as info:
        _boundary(S.READING_POSITION).includes(value)
    assert "epubcfi" not in str(info.value)


def _as_unpickled_from_an_old_version(location):
    """A Location as unpickled from a version that didn't store the
    derived keys."""
    clone = copy.copy(location)
    del clone.__dict__["sort_key"]
    del clone.__dict__["spine_index"]
    return clone


def test_includes_and_spine_index_with_locations_from_old_pickles():
    old_bookmark = _as_unpickled_from_an_old_version(BOOKMARK)
    old_highlight = _as_unpickled_from_an_old_version(HIGHLIGHT)
    by_bookmark = _boundary(S.READING_POSITION, bookmark=old_bookmark)
    by_highlight = _boundary(S.RECENT_HIGHLIGHT, highlight=old_highlight)
    assert by_bookmark.spine_index == 3 and by_highlight.spine_index == 3
    earlier = _as_unpickled_from_an_old_version(L("/6/8[c3]!/4/10/1:19"))
    later = _as_unpickled_from_an_old_version(L("/6/8[c3]!/4/12/1:31"))
    assert by_bookmark.includes(earlier) and not by_bookmark.includes(later)
    assert by_highlight.includes(earlier) and not by_highlight.includes(later)


def test_includes_reads_nothing():
    b = _boundary(S.READING_POSITION)
    with _fs_audit.record() as rec:
        for _ in range(3):
            b.includes(L("/6/8[c3]!/4/10/1:19"))
            b.includes(_as_unpickled_from_an_old_version(L("/6/8[c3]!/4/10/1:19")))
            _ = b.spine_index
    assert not rec.events


# -- ResolvedBoundary -------------------------------------------------------------------


def test_resolved_boundary():
    assert field_names(P.ResolvedBoundary) == [
        "book_id", "boundary", "position", "precision", "source", "warnings"]
    boundary = _boundary(S.READING_POSITION, warnings=(W.MULTIPLE_BOOKMARKS,))
    resolved = P.ResolvedBoundary(7, boundary, P.TextPosition(3, 0), P.BoundaryPrecision.SPINE_ITEM,
                                  S.READING_POSITION, [W.MULTIPLE_BOOKMARKS, W.BOOKMARK_TOC_PAGE])
    assert resolved.warnings == (W.MULTIPLE_BOOKMARKS, W.BOOKMARK_TOC_PAGE)
    assert resolved.boundary is boundary and resolved.position == P.TextPosition(3, 0)
    start = P.ResolvedBoundary(None, _boundary(S.NONE), P.TextPosition(0), P.BoundaryPrecision.START,
                               S.NONE, ())
    assert start.book_id is None and start.warnings == ()
    assert roundtrips(resolved) and roundtrips(start)
    assert {resolved: 1, start: 2}[copy.deepcopy(resolved)] == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.position = P.TextPosition(0)
    with pytest.raises(ex.InvalidArgumentError):
        P.ResolvedBoundary(7, boundary, P.TextPosition(3), P.BoundaryPrecision.START, S.NONE,
                           W.BOOKMARK_TOC_PAGE)
    with pytest.raises(TypeError):   # every field is given
        P.ResolvedBoundary(7, boundary, P.TextPosition(3), P.BoundaryPrecision.START, S.NONE)
