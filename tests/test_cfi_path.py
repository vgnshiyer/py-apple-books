"""The CFI content-path parser (``models.location._content_path``, 1.11):
the steps after the first ``!``, with id assertions, for placing a
location in its spine file."""

import random
import time

import pytest

from py_apple_books.models.location import Location, _cfi_sort_key, _content_path


@pytest.mark.parametrize("cfi, steps", [
    # A highlight (range form): parent + start; offsets dropped.
    ("epubcfi(/6/10[item7]!/4/2[pgepubid00007]/16/1,:412,:478)",
     ((4, None), (2, "pgepubid00007"), (16, None), (1, None))),
    ("epubcfi(/6/4!/4/10,/1:0,/3:5)", ((4, None), (10, None), (1, None))),
    ("epubcfi(/6/4!,/4/2/1:0,/4/2/1:5)", ((4, None), (2, None), (1, None))),
    # A point (reading position).
    ("epubcfi(/6/14[chap05]!/4/2/1:0)", ((4, None), (2, None), (1, None))),
    ("epubcfi(/6/14[chap05]!/4/2/1)", ((4, None), (2, None), (1, None))),
    # Temporal and spatial offsets end the path, like character offsets.
    ("epubcfi(/6/4!/4/2~12.5)", ((4, None), (2, None))),
    ("epubcfi(/6/4!/4/2@10:20)", ((4, None), (2, None))),
    ("epubcfi(/6/4!/4/2:3[yes,;s=b])", ((4, None), (2, None))),
    # A second '!' (into an embedded document) is dropped with what follows.
    ("epubcfi(/6/4!/4/2!/4/6)", ((4, None), (2, None))),
    # Assertions: parameters dropped, escapes undone, empty ones ignored.
    ("epubcfi(/6/4!/4[body;x=y]/2)", ((4, "body"), (2, None))),
    ("epubcfi(/6/4!/4[a^]b^;c^^d]/2)", ((4, "a]b;c^d"), (2, None))),
    ("epubcfi(/6/4!/4[]/2)", ((4, None), (2, None))),
    # Commas and '!' inside an assertion don't split the CFI.
    ("epubcfi(/6/4!/4[x^,y]/2[p!q]/1:0)", ((4, "x,y"), (2, "p!q"), (1, None))),
    # Whitespace around the wrapper is tolerated, as by Location.
    ("  epubcfi(/6/4!/4/2)  ", ((4, None), (2, None))),
])
def test_steps(cfi, steps):
    assert _content_path(cfi) == steps


@pytest.mark.parametrize("cfi", ["epubcfi(/6/4[chap])", "epubcfi(/6/4)", "epubcfi(/6/4!)"])
def test_no_content_path_is_the_start_of_the_file(cfi):
    assert _content_path(cfi) == ()


@pytest.mark.parametrize("cfi", [
    None, "", "junk", "epubcfi()", "epubcfi(/6/4!/4/x)", "epubcfi(/6/4!/4[abc)", "epubcfi(/6/4!4/2)",
    "epubcfi(/6/4!/4//2)", "epubcfi(/6/4!/4/2 /3)", "epubcfi(/6/4!/4/1234567890)", "epubcfi(/6/4!/-4)",
    "epubcfi(/6/4!/٤)", 42, b"epubcfi(/6/4!/4)",
])
def test_malformed_is_none(cfi):
    assert _content_path(cfi) is None


def test_nine_digit_steps_are_accepted():
    assert _content_path("epubcfi(/6/4!/4/999999999)") == ((4, None), (999999999, None))


def test_agrees_with_the_sort_key():
    """The content path is the part of ``sort_key`` after the spine
    steps, without offsets, for well-formed CFIs."""
    for cfi in ["epubcfi(/6/10[item7]!/4/2[x]/16/1,:412,:478)", "epubcfi(/6/4!/4/10,/1:0,/3:5)",
                "epubcfi(/6/14!/4/2/1:0)"]:
        key = _cfi_sort_key(cfi)
        steps = tuple(s for s, _ in _content_path(cfi))
        assert key[2:2 + len(steps)] == steps


def test_never_raises_on_noise():
    rng = random.Random(1110)
    alphabet = "/!,:[]^;~@0123456789abc() é"
    for _ in range(3000):
        body = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        result = _content_path(f"epubcfi({body})")
        assert result is None or isinstance(result, tuple)


def test_linear_time_on_hostile_input():
    """Long runs of brackets, escapes and steps parse in linear time."""
    for body in ("/6/4!" + "[" * 200_000, "/6/4!/4[" + "^" * 200_001 + "]",
                 "/6/4!" + "/2" * 100_000, "/6/4!" + ",[" * 100_000):
        start = time.perf_counter()
        _content_path(f"epubcfi({body})")
        assert time.perf_counter() - start < 1.0


def test_location_is_unchanged():
    """The parser is a function of its own; Location's fields and
    equality are as in 1.10."""
    loc = Location("epubcfi(/6/10[item7]!/4/2/16/1,:412,:478)")
    assert (loc.chapter_id, loc.char_range, loc.spine_index) == ("item7", (412, 478), 4)
    assert not hasattr(loc, "content_path")
