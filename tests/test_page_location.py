"""PageLocation (F17): the page or spine position Apple Books keeps in a
bookmark row's ``ZPLUSERDATA`` property list.

Pure value-object tests: blobs are built here with ``plistlib`` in the
shape Books writes (``BKPageLocation`` with a ``BKLocation`` super).
"""

import copy
import dataclasses
import os
import pickle
import plistlib
import random
import struct

import pytest

from py_apple_books import models
from py_apple_books.models import PageLocation
from py_apple_books.models.location import MAX_PAGE_INDEX, MAX_PAGE_LOCATION_BYTES


def blob(page_offset=0, ordinal=0, *, fmt=plistlib.FMT_BINARY, drop=(), **extra) -> bytes:
    """A ZPLUSERDATA blob as Books writes it (``drop`` leaves keys out)."""
    data = {"class": "BKPageLocation", "pageOffset": page_offset,
            "super": {"class": "BKLocation", "ordinal": ordinal}, **extra}
    for key in drop:
        data.pop(key)
    return plistlib.dumps(data, fmt=fmt)


def archived() -> bytes:
    """An NSKeyedArchiver-shaped property list."""
    return plistlib.dumps({
        "$archiver": "NSKeyedArchiver", "$version": 100000,
        "$top": {"root": plistlib.UID(1)},
        "$objects": ["$null", {"pageOffset": 3, "$class": plistlib.UID(2)},
                     {"$classname": "BKPageLocation", "$classes": ["BKPageLocation", "NSObject"]}],
    }, fmt=plistlib.FMT_BINARY)


def huge_object_count() -> bytes:
    """A binary plist whose trailer claims 2**62 objects."""
    body = b"bplist00" + b"\x10\x05"  # one int object
    offsets = struct.pack(">B", 8)
    trailer = struct.pack(">6xBBQQQ", 1, 1, 2 ** 62, 0, len(body))
    return body + offsets + trailer


class TestFromPlist:
    def test_a_pdf_bookmark(self):
        loc = PageLocation.from_plist(blob(211))
        assert loc == PageLocation(ordinal=0, page_offset=211)
        assert loc.page == 212

    def test_an_epub_bookmark(self):
        loc = PageLocation.from_plist(blob(0, ordinal=9))
        assert (loc.ordinal, loc.page_offset, loc.page) == (9, 0, 1)

    def test_bytes_like_inputs(self):
        data = blob(12)
        for value in (bytearray(data), memoryview(data)):
            assert PageLocation.from_plist(value) == PageLocation(0, 12)

    def test_xml_plist_is_accepted(self):
        assert PageLocation.from_plist(blob(27, fmt=plistlib.FMT_XML)) == PageLocation(0, 27)

    def test_without_super_the_ordinal_is_0(self):
        assert PageLocation.from_plist(blob(17, drop=("super",))) == PageLocation(0, 17)

    def test_without_page_offset(self):
        loc = PageLocation.from_plist(blob(ordinal=3, drop=("pageOffset",)))
        assert loc == PageLocation(ordinal=3, page_offset=None) and loc.page is None

    def test_class_names_are_not_required(self):
        assert PageLocation.from_plist(plistlib.dumps({"pageOffset": 7})) == PageLocation(0, 7)

    def test_bounds(self):
        assert PageLocation.from_plist(blob(MAX_PAGE_INDEX)).page_offset == MAX_PAGE_INDEX
        assert PageLocation.from_plist(blob(0, ordinal=MAX_PAGE_INDEX)).ordinal == MAX_PAGE_INDEX
        assert PageLocation.from_plist(blob(MAX_PAGE_INDEX + 1)) is None
        assert PageLocation.from_plist(blob(0, ordinal=MAX_PAGE_INDEX + 1)) is None

    def test_size_limit(self):
        xml = blob(9, fmt=plistlib.FMT_XML)
        at_limit = xml + b" " * (MAX_PAGE_LOCATION_BYTES - len(xml))
        assert len(at_limit) == MAX_PAGE_LOCATION_BYTES == 64 * 1024
        assert PageLocation.from_plist(at_limit) == PageLocation(0, 9)
        assert PageLocation.from_plist(at_limit + b" ") is None
        assert PageLocation.from_plist(blob(9, padding=b"x" * 70 * 1024)) is None

    @pytest.mark.parametrize("data", [
        b"", b"\0" * 200, b"garbage", b"bplist00", b"bplist00" + b"\xff" * 40,
        b"<?xml version='1.0'?><plist><dict><key>pageOffset</key>",
        archived(), huge_object_count(),
        plistlib.dumps([1, 2, 3]), plistlib.dumps("pageOffset"), plistlib.dumps(211),
        plistlib.dumps({}), plistlib.dumps({"class": "BKPageLocation"}),
        blob(-1), blob(True), blob(10 ** 8), blob(1.5), blob("211"),
        blob(0, ordinal=-1), blob(0, ordinal=True), blob(0, ordinal="6"), blob(0, ordinal=2.0),
        blob(super="BKLocation"), blob(super=[0]),
        b"<?xml version='1.0'?><!DOCTYPE plist [<!ENTITY a 'aaaa'>]><plist><dict>"
        b"<key>pageOffset</key><integer>1</integer></dict></plist>",
    ], ids=repr)
    def test_refused(self, data):
        assert PageLocation.from_plist(data) is None

    @pytest.mark.parametrize("value", [None, "bplist00", 211, 1.5, [blob(1)], object()], ids=repr)
    def test_non_bytes(self, value):
        assert PageLocation.from_plist(value) is None

    def test_deeply_nested(self):
        deep = 5000
        xml = (b"<?xml version='1.0'?><plist>" + b"<array>" * deep + b"</array>" * deep + b"</plist>")
        assert PageLocation.from_plist(xml) is None

    def test_random_bytes_never_raise(self):
        rng = random.Random(17)
        base = blob(211)
        for i in range(int(os.environ.get("APPLE_BOOKS_FUZZ_ITERATIONS", "2000"))):
            if i % 2:
                data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 200)))
                if i % 4 == 1:
                    data = b"bplist00" + data
            else:  # a valid blob with a few bytes changed
                data = bytearray(base)
                for _ in range(rng.randrange(1, 6)):
                    data[rng.randrange(len(data))] = rng.randrange(256)
                data = bytes(data)
            result = PageLocation.from_plist(data)
            assert result is None or isinstance(result, PageLocation)


class TestValueObject:
    def test_exported_from_models(self):
        assert models.PageLocation is PageLocation and "PageLocation" in models.__all__

    def test_fields(self):
        assert [f.name for f in dataclasses.fields(PageLocation)] == ["ordinal", "page_offset"]

    def test_frozen_hashable_and_copyable(self):
        loc = PageLocation(2, 41)
        with pytest.raises(dataclasses.FrozenInstanceError):
            loc.page_offset = 1
        assert hash(loc) == hash(PageLocation(2, 41)) and loc != PageLocation(2, 40)
        for clone in (pickle.loads(pickle.dumps(loc)), copy.copy(loc), copy.deepcopy(loc)):
            assert clone == loc and clone.page == 42

    def test_repr(self):
        assert repr(PageLocation(0, 211)) == "PageLocation(ordinal=0, page_offset=211)"
