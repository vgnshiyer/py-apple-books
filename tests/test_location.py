"""Tests for py_apple_books.models.location.

Pure value-object tests — no BookContent, no EPUB fixture, no
filesystem. Location's whole job is to parse two fields out of a CFI
string; resolving chapters or text from a location is the facade's
responsibility, tested elsewhere.
"""

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
