"""Tests for py_apple_books.models.location.

Pure value-object tests — no BookContent, no EPUB fixture, no
filesystem. Location's whole job is to parse a few fields out of a CFI
string; resolving chapters or text from a location is the facade's
responsibility, tested elsewhere.
"""

import dataclasses
import random

import pytest

from py_apple_books.models.location import Location


class TestCfiParsing:
    def test_empty_cfi_is_falsy_and_yields_no_fields(self):
        loc = Location("")
        assert not loc
        assert loc.chapter_id is None
        assert loc.char_range is None

    def test_malformed_cfi_yields_no_fields(self):
        loc = Location("not a cfi at all")
        assert loc.chapter_id is None
        assert loc.char_range is None

    def test_str_returns_raw_cfi(self):
        cfi = "epubcfi(/6/8[item5]!/4/2/1:0)"
        assert str(Location(cfi)) == cfi

    def test_truthy_when_cfi_is_non_empty(self):
        assert bool(Location("epubcfi(/6/8[item5]!/4/2/1:0)"))

    def test_chapter_id_from_spine_bracket(self):
        loc = Location("epubcfi(/6/26[id134]!/4/2/1,:0,:1)")
        assert loc.chapter_id == "id134"

    def test_chapter_id_none_when_no_bracket_hint(self):
        # The EPUB CFI spec allows hint-less paths like ``/6/4/6``.
        loc = Location("epubcfi(/6/4/6!/4/2/1:0)")
        assert loc.chapter_id is None

    def test_chapter_id_prefers_last_bracket_in_spine_path(self):
        # Multiple bracket hints in the spine path — the last one is
        # the manifest item id per EPUB CFI convention.
        loc = Location("epubcfi(/6/26[wrapper]/3[id134]!/4/2/1:0)")
        assert loc.chapter_id == "id134"

    def test_char_range_parsed(self):
        loc = Location(
            "epubcfi(/6/8[item5]!/4/2[pgepubid00005]/18/1,:629,:691)"
        )
        assert loc.char_range == (629, 691)

    def test_char_range_none_when_not_encoded(self):
        loc = Location("epubcfi(/6/8[item5]!/4/2/1:0)")
        assert loc.char_range is None

    def test_real_world_cfis(self):
        """Spot-check a few CFIs lifted from a real Apple Books library
        so parser regressions can't slip through."""
        cases = [
            (
                "epubcfi(/6/26[id134]!/4[text]/2[fm02]/2/2[calibre_pb_0]/2/2/1,:0,:1)",
                {"chapter_id": "id134", "char_range": (0, 1)},
            ),
            (
                "epubcfi(/6/14[x9780062457738-5]!/4[x9780062457738-5]/2[_idContainer008]/266/1,:4,:10)",
                {"chapter_id": "x9780062457738-5", "char_range": (4, 10)},
            ),
            (
                "epubcfi(/6/20[chapter003]!/4/2/2[hd-chapter003]/3,:0,:1)",
                {"chapter_id": "chapter003", "char_range": (0, 1)},
            ),
        ]
        for cfi, expected in cases:
            loc = Location(cfi)
            for attr, want in expected.items():
                got = getattr(loc, attr)
                assert got == want, (
                    f"{cfi!r}: {attr}={got!r}, expected {want!r}"
                )

    def test_frozen_dataclass(self):
        import pytest

        loc = Location("epubcfi(/6/8[item5]!/4/2/1:0)")
        with pytest.raises(Exception):  # FrozenInstanceError subclass
            loc.cfi = "other"  # type: ignore[misc]

    def test_fields_populated_eagerly(self):
        """After construction, ``chapter_id`` and ``char_range`` should
        already be set — no lazy resolution."""
        loc = Location("epubcfi(/6/26[id134]!/4/2/1,:5,:10)")
        # Access without triggering any further parse.
        assert loc.chapter_id == "id134"
        assert loc.char_range == (5, 10)


class TestSortKeyAndSpineIndex:
    # In document order. Numeric, not string, order matters: /6/10 comes
    # after /6/8, and offset :9 before :10.
    DOCUMENT_ORDER = [
        "epubcfi(/6/2[cover]!/4/2/1:0)",
        "epubcfi(/6/8[c3]!/4/2[p1]/18/1,:9,:12)",
        "epubcfi(/6/8[c3]!/4/2[p1]/18/1,:10,:691)",
        "epubcfi(/6/8[c3]!/4/2[p1]/18/1,:700,:710)",
        "epubcfi(/6/8[c3]!/4/2[p1]/20/1,:1,:5)",
        "epubcfi(/6/8[c3]!/4/2[p1]/20/3:0)",
        "epubcfi(/6/10[c4]!/4/2/2/1,:0,:5)",
        "epubcfi(/6/26[c12]!/4[body]/2[s1]/2/2[pb0]/2/2/1,:0,:1)",
    ]

    def test_sort_key_gives_document_order(self):
        shuffled = self.DOCUMENT_ORDER[:]
        random.Random(1).shuffle(shuffled)
        ordered = sorted((Location(c) for c in shuffled), key=lambda loc: loc.sort_key)
        assert [loc.cfi for loc in ordered] == self.DOCUMENT_ORDER

    def test_sort_key_collects_steps_then_start_offset(self):
        loc = Location("epubcfi(/6/8[c3]!/4/2[p1]/18/1,:629,:691)")
        assert loc.sort_key == (6, 8, 4, 2, 18, 1, 629)

    def test_range_sorts_where_it_starts(self):
        """A range CFI keys on its parent path plus its start, so it equals
        the point CFI at that start whatever the range's end."""
        point = Location("epubcfi(/6/4[chap1]!/4/4/1:0)")
        range_ = Location("epubcfi(/6/4[chap1]!/4/4,/1:0,/1:21)")
        assert range_.sort_key == point.sort_key == (6, 4, 4, 4, 1, 0)

    def test_assertions_are_ignored(self):
        """Bracket assertions may hold commas, colons, slashes and
        ^-escaped brackets; none of it is a step or offset."""
        plain = Location("epubcfi(/6/4!/4/10/1,:3,:9)")
        asserted = Location("epubcfi(/6/4[ch^]1]!/4/10[a,b:7/2]/1,:3[x,y],:9)")
        assert asserted.sort_key == plain.sort_key == (6, 4, 4, 10, 1, 3)
        assert asserted.spine_index == 1

    @pytest.mark.parametrize("cfi, spine_index", [
        ("epubcfi(/6/2[cover]!/4/2/1:0)", 0),
        ("epubcfi(/6/8[c3]!/4/2/1:0)", 3),
        ("epubcfi(/6/10[c4]!/4/2/2/1,:0,:5)", 4),
        ("epubcfi(/6/10!/4/2/2/1,:0,:5)", 4),  # no bracket hint needed
        ("epubcfi(/6/26[c12]!/4/2/1,:0,:1)", 12),
    ])
    def test_spine_index(self, cfi, spine_index):
        assert Location(cfi).spine_index == spine_index

    @pytest.mark.parametrize("cfi", [
        "epubcfi(/4/2/1:0)",   # not under the spine (/6)
        "epubcfi(/6)",         # no spine item step
        "epubcfi(/6/0!/4/2)",  # no itemref at step 0
    ])
    def test_spine_index_none_without_spine_step(self, cfi):
        assert Location(cfi).spine_index is None

    @pytest.mark.parametrize("cfi", ["", "x", "not a cfi at all", "epubcfi()", "epubcfi([id])"])
    def test_non_cfi_gives_none(self, cfi):
        loc = Location(cfi)
        assert loc.sort_key is None
        assert loc.spine_index is None

    def test_unclosed_bracket_is_not_an_assertion(self):
        """A ``[`` with no closing ``]`` (a ``^]`` doesn't close) is kept,
        so the steps after it still count."""
        assert Location("epubcfi(/6/4[c1!/4/2/1:0)").sort_key == (6, 4, 4, 2, 1, 0)
        assert Location("epubcfi(/6/4[c1]!/4[x^]/2/1:0)").sort_key == (6, 4, 4, 2, 1, 0)
        # A long run of them is scanned once, not once per bracket.
        assert Location("epubcfi(/6/4!/4" + "[" * 20000 + "/2:0)").sort_key == (6, 4, 4, 2, 0)

    def test_oversized_step_does_not_raise(self):
        """int() may refuse a long digit string (sys.get_int_max_str_digits);
        the key is then None rather than an exception."""
        loc = Location("epubcfi(/6/" + "9" * 5000 + "!/4/2/1:0)")
        assert loc.sort_key is None or loc.sort_key[1] > 10 ** 4000

    def test_equality_hash_and_repr_unchanged(self):
        """The derived fields stay out of ==, hash() and repr()."""
        cfi = "epubcfi(/6/8[c3]!/4/2[p1]/18/1,:629,:691)"
        a, b = Location(cfi), Location(cfi)
        assert a == b and hash(a) == hash(b)
        assert a != Location("epubcfi(/6/8[c3]!/4/2[p1]/18/1,:629,:692)")
        assert repr(a) == f"Location(cfi={cfi!r}, chapter_id='c3', char_range=(629, 691))"
        assert [f.name for f in dataclasses.fields(Location) if f.compare] == ["cfi", "chapter_id", "char_range"]
        assert [f.name for f in dataclasses.fields(Location) if f.init] == ["cfi"]
