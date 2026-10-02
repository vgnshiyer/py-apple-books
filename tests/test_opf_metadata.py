"""The private OPF reader (``py_apple_books._opf``) and the metadata types.

Bundles are synthetic (``write_epub_bundle``, or a ``container.xml``
and package document written directly for odd shapes). iCloud
placeholders are faked by patching ``_icloud.lstat`` / ``_icloud.stat``,
the guard's patch points.
"""

import errno
import logging
import os
import pathlib
import pickle
import stat
import threading
import time
import tracemalloc
from types import SimpleNamespace

import pytest

from py_apple_books import _icloud, _opf
from py_apple_books.content import clear_content_cache
from py_apple_books.models import BookMetadata, MetadataFileState
from py_apple_books.testing import write_epub_bundle

OPF_NS = 'xmlns="http://www.idpf.org/2007/opf" xmlns:opf="http://www.idpf.org/2007/opf"'
DC_NS = 'xmlns:dc="http://purl.org/dc/elements/1.1/"'
CONTAINER = ('<?xml version="1.0"?>\n<container version="1.0" '
             'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
             '<rootfile full-path="{path}" media-type="application/oebps-package+xml"/>'
             '</rootfiles></container>')


def opf(metadata: str = "", manifest: str = "", *, version: str = "3.0", head: str = "") -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n{head}<package {OPF_NS} version="{version}">'
            f'<metadata {DC_NS}>{metadata}</metadata><manifest>{manifest}</manifest>'
            f'<spine/></package>')


def bundle(root: pathlib.Path, package, *, rel: str = "OEBPS/content.opf", container=None,
           name: str = "book.epub") -> pathlib.Path:
    """A bundle with a container.xml naming ``rel`` and ``package`` (str
    or bytes) written there; ``container`` replaces container.xml."""
    path = root / name
    (path / "META-INF").mkdir(parents=True, exist_ok=True)
    (path / "META-INF" / "container.xml").write_text(
        container if container is not None else CONTAINER.format(path=rel), encoding="utf-8")
    if package is not None:
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(package, bytes):
            target.write_bytes(package)
        else:
            target.write_text(package, encoding="utf-8")
    return path


def read(path) -> _opf.OpfResult:
    return _opf.read_metadata(path)


def fields(path) -> _opf.OpfFields:
    result = read(path)
    assert result.state == _opf.READ, result
    return result.fields


def meta(tmp_path, metadata: str, manifest: str = "", **kwargs) -> _opf.OpfFields:
    return fields(bundle(tmp_path, opf(metadata, manifest, **kwargs)))


@pytest.fixture
def reads(monkeypatch):
    """Every ``read_local`` call ``_opf`` makes, as ``rel`` names."""
    calls = []
    real = _icloud.read_local

    def spy(root, rel, **kwargs):
        calls.append(rel)
        return real(root, rel, **kwargs)

    monkeypatch.setattr(_icloud, "read_local", spy)
    return calls


@pytest.fixture
def opens(monkeypatch):
    """Every ``os.open`` of a file (not a folder), as the name passed."""
    calls = []
    real = os.open

    def spy(path, flags, *args, **kwargs):
        if not flags & getattr(os, "O_DIRECTORY", 0):
            calls.append(os.fspath(path))
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    return calls


def fake(st, **changes):
    values = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def patch_stat(monkeypatch):
    """``patch(name, **changes)``: ``_icloud.lstat``/``stat`` of a path or
    of a ``dir_fd`` name ending in ``name`` report ``changes``. Returns
    the list of names stat'ed."""
    real_lstat, real_stat = os.lstat, os.stat
    rules = {}
    seen = []

    def apply(path, st):
        text = os.fspath(path)
        seen.append(text)
        for name, changes in rules.items():
            if text == name or text.endswith("/" + name):
                return fake(st, **changes)
        return st

    monkeypatch.setattr(_icloud, "lstat", lambda path, *, dir_fd=None: apply(path, real_lstat(path, dir_fd=dir_fd)))
    monkeypatch.setattr(_icloud, "stat", lambda path: apply(path, real_stat(path)))

    def patch(name, **changes):
        rules[name] = changes
        return seen

    return patch


# -- types ----------------------------------------------------------------------


class TestTypes:
    def test_states(self):
        assert [s.value for s in MetadataFileState] == [
            "read", "not_requested", "no_file", "not_epub", "not_downloaded", "unreadable"]
        assert str(MetadataFileState.NOT_EPUB) == "not_epub"
        assert MetadataFileState("read") is MetadataFileState.READ

    def test_shared_reason_strings(self):
        positions = pytest.importorskip("py_apple_books.positions")
        reasons = {r.value for r in positions.UnavailableReason}
        assert {"not_epub", "not_downloaded", "unreadable"} <= reasons

    def test_frozen_hashable_picklable(self):
        meta_ = BookMetadata(book_id=1, language="en", subjects=("A",), file_state=MetadataFileState.READ,
                             book_file_fields=frozenset({"language"}))
        with pytest.raises(AttributeError):
            meta_.language = "fr"
        assert hash(meta_) == hash(BookMetadata(**{f: getattr(meta_, f) for f in meta_.__dataclass_fields__}))
        assert pickle.loads(pickle.dumps(meta_)) == meta_

    def test_equal_and_hash_stable_across_threads(self, tmp_path):
        path = bundle(tmp_path, opf('<meta name="calibre:series" content="S"/>'
                                    '<meta name="calibre:series_index" content="2"/>'))
        results = []

        def work():
            for _ in range(20):
                results.append(read(path))

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len({r for r in results}) == 1 and len({hash(r) for r in results}) == 1


# -- parsing --------------------------------------------------------------------


class TestOpf3:
    def test_every_field(self, tmp_path):
        metadata = (
            '<dc:publisher> Synthetic \n Press </dc:publisher><dc:date>2019-03-04</dc:date>'
            '<dc:identifier id="isbn">urn:isbn:978-0-306-40615-7</dc:identifier>'
            '<dc:subject>Philosophy</dc:subject><dc:subject>Logic</dc:subject><dc:subject>Mathematics</dc:subject>'
            '<dc:description>&lt;p&gt;A &lt;b&gt;synthetic&lt;/b&gt; book &amp;amp; more.&lt;/p&gt;</dc:description>')
        path = write_epub_bundle(tmp_path / "b.epub", [("c1", "<p>x</p>")], metadata_xml=metadata,
                                 extra_items=[("cov", "img/cover.jpg", "image/jpeg", b"\xff", "cover-image")])
        got = fields(path)
        assert got == _opf.OpfFields(
            language="en", publisher="Synthetic Press", published="2019-03-04", year=2019,
            isbn="9780306406157", subjects=("Philosophy", "Logic", "Mathematics"),
            description="A synthetic book & more.", cover_href="OEBPS/img/cover.jpg")

    def test_opf_at_the_bundle_root_and_percent_escapes(self, tmp_path):
        path = write_epub_bundle(tmp_path / "b.epub", [("c1", "<p>x</p>")], opf_dir="",
                                 extra_items=[("cov", "a%20b.png", "image/png", b"\x89", "cover-image")])
        assert fields(path).cover_href == "a b.png"

    def test_subjects_mode_stops_at_the_metadata(self, tmp_path):
        path = bundle(tmp_path, opf('<dc:subject>A</dc:subject><dc:publisher>P</dc:publisher>',
                                    '<item id="x" href="x" media-type="image/png" properties="cover-image"/>'
                                    '<<<not xml'))
        assert read(path).state == _opf.UNREADABLE  # 'full' reads the manifest
        clear_content_cache()
        assert _opf.read_subjects(path) == ("a",)


class TestOpf2:
    def test_scheme_events_cover(self, tmp_path):
        got = meta(tmp_path,
                   '<dc:identifier opf:scheme="ISBN">0-306-40615-2</dc:identifier>'
                   '<dc:date opf:event="modification">2024-01-01</dc:date>'
                   '<dc:date opf:event="creation">2001</dc:date>'
                   '<dc:date opf:event="publication">2010-05</dc:date>'
                   '<meta name="cover" content="cover-id"/>',
                   '<item id="page" href="cover.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="cover-id" href="images/c.jpg" media-type="image/jpeg"/>',
                   version="2.0")
        assert (got.isbn, got.published, got.year, got.cover_href) == (
            "0306406152", "2010-05", 2010, "OEBPS/images/c.jpg")

    def test_event_preference(self, tmp_path):
        undated = meta(tmp_path, '<dc:date opf:event="creation">2001</dc:date><dc:date>2003</dc:date>')
        assert undated.published == "2003"
        clear_content_cache()
        only_modified = meta(tmp_path, '<dc:date opf:event="modification">2024-01-01</dc:date>')
        assert only_modified.published is None and only_modified.year is None

    def test_cover_meta_pointing_at_a_page_is_ignored(self, tmp_path):
        got = meta(tmp_path, '<meta name="cover" content="p"/>',
                   '<item id="p" href="cover.xhtml" media-type="application/xhtml+xml"/>')
        assert got.cover_href is None

    def test_cover_leaving_the_bundle_or_absolute_is_dropped(self, tmp_path):
        for href in ("../../x.jpg", "/etc/x.jpg", "http://example.invalid/x.jpg", "file:///x.jpg"):
            clear_content_cache()
            got = meta(tmp_path, "", f'<item id="c" href="{href}" media-type="image/jpeg" '
                                     f'properties="cover-image"/>')
            assert got.cover_href is None, href

    def test_opf1_wrapper_and_capitalised_names(self, tmp_path):
        package = ('<?xml version="1.0"?><package unique-identifier="id"><metadata>'
                   '<dc-metadata xmlns:dc="http://purl.org/dc/elements/1.0/">'
                   '<dc:Title>T</dc:Title><dc:Language>fr</dc:Language><dc:Subject>Roman</dc:Subject>'
                   '<dc:Publisher>Éditions</dc:Publisher></dc-metadata>'
                   '<x-metadata><meta name="cover" content="c"/></x-metadata></metadata>'
                   '<manifest><item id="c" href="c.gif" media-type="image/gif"/></manifest></package>')
        got = fields(bundle(tmp_path, package))
        assert (got.language, got.subjects, got.publisher, got.cover_href) == (
            "fr", ("Roman",), "Éditions", "OEBPS/c.gif")

    def test_external_dtd_only_doctype_parses_offline(self, tmp_path, monkeypatch):
        import socket

        def no_network(*args, **kwargs):
            raise AssertionError("network used")

        monkeypatch.setattr(socket, "create_connection", no_network)
        head = '<!DOCTYPE package PUBLIC "+//ISBN 0-9673008-1-9//DTD OEB 1.2 Package//EN" ' \
               '"http://openebook.org/dtds/oeb-1.2/oebpkg12.dtd">'
        got = meta(tmp_path, '<dc:language>de</dc:language><dc:subject>A&nbsp;B</dc:subject>', head=head)
        assert got.language == "de" and got.subjects == ("AB",)


class TestIsbn:
    @pytest.mark.parametrize("identifiers, expected", [
        ([("urn:isbn:9780306406157", None, None)], "9780306406157"),
        ([("978-0-306-40615-8", "ISBN", None)], None),  # checksum
        ([("urn:uuid:12345678-1234-1234-1234-123456789abc", None, None)], None),
        ([("B00ABCDEFG", "AMAZON", None)], None),
        ([("0306406152", "AMAZON", None)], None),  # an ASIN, even if it checks out
        ([("calibre:1234", None, None)], None),
        ([("1234", "calibre", None)], None),
        ([("0306406152", None, None)], None),  # a bare 10-digit number says nothing
        ([("0-306-40615-2", "ISBN", None)], "0306406152"),
        ([("isbn:080442957X", None, None)], "080442957X"),
        ([("0306406152", "ISBN", None), ("9780306406157", "ISBN", None)], "9780306406157"),
        ([("9780306406157", None, None)], "9780306406157"),  # a bare EAN-13 978/979
        ([("9770306406155", None, None)], None),  # a bare EAN-13 that isn't an ISBN
        ([("9780306406157", None, "15")], "9780306406157"),
        ([("0306406152", None, "02")], "0306406152"),
        ([("9780306406157", None, "01")], None),  # refined as another identifier type
    ])
    def test_pick(self, identifiers, expected):
        assert _opf.pick_isbn(identifiers) == expected

    def test_onix_refinement_in_a_package(self, tmp_path):
        got = meta(tmp_path, '<dc:identifier id="pub">0306406152</dc:identifier>'
                             '<meta refines="#pub" property="identifier-type" scheme="onix:codelist5">02</meta>')
        assert got.isbn == "0306406152"


class TestDatesAndLanguage:
    @pytest.mark.parametrize("text, expected", [
        ("2019", ("2019", 2019)),
        ("2019-03", ("2019-03", 2019)),
        ("2019-03-04T10:00:00+05:00", ("2019-03-04", 2019)),
        ("2019-02-30", ("2019-02", 2019)),
        ("2019-13-01", ("2019", 2019)),
        ("0101-01-01T00:00:00+00:00", None),  # calibre's "no date"
        ("1449", None),
        ("n/a", None),
        ("20190304", None),
        ("", None),
    ])
    def test_parse_date(self, text, expected):
        assert _opf.parse_date(text) == expected

    def test_far_future_year(self):
        assert _opf.parse_date(str(_opf.latest_plausible_year() + 1)) is None
        assert _opf.parse_date(str(_opf.latest_plausible_year()))[1] == _opf.latest_plausible_year()

    @pytest.mark.parametrize("text, expected", [
        ("en", "en"), ("en_us", "en-US"), ("EN-gb", "en-GB"), ("fr_ca", "fr-CA"), ("zh-hant-tw", "zh-Hant-TW"),
        ("es-419", "es-419"), (" de ", "de"), ("und", None), ("zxx", None), ("English", None), ("", None),
        ("e", None), (None, None), (3, None), ("en-", None), ("x" * 70, None),
    ])
    def test_language(self, text, expected):
        assert _opf.normalize_language(text) == expected

    def test_language_from_the_package(self, tmp_path):
        path = write_epub_bundle(tmp_path / "b.epub", [("c1", "<p>x</p>")], language=None,
                                 metadata_xml="<dc:language>und</dc:language><dc:language>en_us</dc:language>")
        assert fields(path).language == "en-US"


class TestSubjectsAndDescription:
    def test_subjects_cleanup(self):
        assert _opf.clean_subjects(["Philosophie", "philosophie", "PHILOSOPHIE ", "Gödel", "godel",
                                    "www.example.com", "https://x.example/a", "example.org",
                                    "  ", None, 3, "A\x00 \n  B"]) == ("Philosophie", "Gödel", "A B")

    def test_urls_kept_on_request(self):
        assert _opf.clean_subjects(["www.example.com", "A"], drop_urls=False) == ("www.example.com", "A")

    def test_subject_caps(self):
        many = [f"Subject {i}" for i in range(40)]
        assert _opf.clean_subjects(many) == tuple(many[:30])
        assert _opf.clean_subjects(["x" * 500]) == ("x" * 120,)

    def test_description_cleanup(self):
        assert _opf.clean_description("&lt;p&gt;One&lt;/p&gt;&lt;p&gt;Two&lt;/p&gt;") == "One\nTwo"
        assert _opf.clean_description("<p>a\x01b<script>x()</script></p>  c") == "ab\nc"
        assert _opf.clean_description("Tom & Jerry < 3") == "Tom & Jerry < 3"
        assert _opf.clean_description("  \n ") is None and _opf.clean_description(None) is None

    def test_description_cut_on_a_word(self):
        text = _opf.clean_description(("word " * 5000).strip())
        assert len(text) <= _opf.DESCRIPTION_MAX and text.endswith("word…")


class TestSeries:
    def test_calibre(self, tmp_path):
        got = meta(tmp_path, '<meta name="calibre:series" content=" S "/>'
                             '<meta name="calibre:series_index" content="2.0"/>')
        assert (got.series_title, got.series_sequence) == ("S", 2.0)

    def test_epub3_collection(self, tmp_path):
        got = meta(tmp_path, '<meta property="belongs-to-collection" id="c">Set</meta>'
                             '<meta refines="#c" property="collection-type">set</meta>'
                             '<meta property="belongs-to-collection" id="s">X</meta>'
                             '<meta refines="#s" property="collection-type">series</meta>'
                             '<meta refines="#s" property="group-position">3</meta>')
        assert (got.series_title, got.series_sequence) == ("X", 3.0)

    def test_untyped_collection(self, tmp_path):
        got = meta(tmp_path, '<meta property="belongs-to-collection">Y</meta>')
        assert (got.series_title, got.series_sequence) == ("Y", None)

    def test_index_without_a_name(self, tmp_path):
        got = meta(tmp_path, '<meta name="calibre:series_index" content="1"/>')
        assert (got.series_title, got.series_sequence) == (None, None)

    @pytest.mark.parametrize("index", ["nan", "inf", "1e999", "x", ""])
    def test_bad_index(self, tmp_path, index):
        got = meta(tmp_path, f'<meta name="calibre:series" content="S"/>'
                             f'<meta name="calibre:series_index" content="{index}"/>')
        assert (got.series_title, got.series_sequence) == ("S", None)


# -- refusals and limits ----------------------------------------------------------


class TestLimits:
    def test_internal_subset_entity_bomb_utf16(self, tmp_path):
        bomb = ('<?xml version="1.0" encoding="UTF-16"?>\n<!DOCTYPE package [<!ENTITY a "aaaaaaaaaa">'
                + "".join(f'<!ENTITY a{i} "&a{i - 1 if i else ""};&a{i - 1 if i else ""};">' for i in range(30))
                + ']><package><metadata><dc:subject xmlns:dc="x">&a29;</dc:subject></metadata></package>')
        path = bundle(tmp_path, bomb.encode("utf-16"))
        started = time.process_time()  # CPU time: no expansion work (wall time varies with load)
        assert read(path).state == _opf.UNREADABLE
        assert time.process_time() - started < 0.5

    def test_tiny_elements(self, tmp_path):
        body = "<x/>" * (2 * 1024 * 1024)  # 8 MiB
        path = bundle(tmp_path, opf(body))
        tracemalloc.start()
        started = time.process_time()
        try:
            assert read(path).state == _opf.UNREADABLE
            elapsed = time.process_time() - started
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert elapsed < 1 and peak < 20 * 1024 * 1024

    def test_element_cap_inside_the_metadata(self, tmp_path):
        path = bundle(tmp_path, opf("<x/>" * (_opf.MAX_ELEMENTS + 10)))
        assert read(path).state == _opf.UNREADABLE

    def test_markup_inside_the_description_counts(self, tmp_path):
        path = bundle(tmp_path, opf("<dc:description>" + "<p>w</p>" * _opf.MAX_ELEMENTS + "</dc:description>"))
        assert read(path).state == _opf.UNREADABLE

    def test_depth_cap(self, tmp_path):
        path = bundle(tmp_path, opf("<x>" * 70 + "</x>" * 70))
        assert read(path).state == _opf.UNREADABLE

    def test_large_description(self, tmp_path):
        path = bundle(tmp_path, opf("<dc:description>" + "&lt;p&gt;word word word&lt;/p&gt;" * 100_000
                                    + "</dc:description>"))
        assert path.joinpath("OEBPS/content.opf").stat().st_size < _opf.OPF_MAX_BYTES
        started = time.process_time()
        got = fields(path)
        assert time.process_time() - started < 1
        assert len(got.description) <= _opf.DESCRIPTION_MAX

    def test_over_the_read_budget(self, tmp_path):
        path = bundle(tmp_path, opf("<dc:subject>A</dc:subject>", "<item/>" * 700_000))
        assert read(path).state == _opf.UNREADABLE

    def test_huge_manifest_after_the_metadata(self, tmp_path):
        manifest = ('<item id="c" href="c.png" media-type="image/png" properties="cover-image"/>'
                    + "<item/>" * 200_000)
        got = fields(bundle(tmp_path, opf("<dc:subject>A</dc:subject>", manifest)))
        assert got.subjects == ("A",) and got.cover_href == "OEBPS/c.png"


class TestFailSoft:
    @pytest.mark.parametrize("make", [
        lambda root: bundle(root, opf("<dc:subject>A</dc:subject><broken")),
        lambda root: bundle(root, opf("<dc:subject>A</dc:subject>", "<item id='a' id='b'/>")),
        lambda root: bundle(root, "<<<not xml"),
        lambda root: bundle(root, "<html><body/></html>"),  # not a package document
        lambda root: bundle(root, None),  # no package document
        lambda root: bundle(root, opf(), rel="../x.opf"),
        lambda root: bundle(root, opf(), container="<container/>"),  # no rootfile
        lambda root: bundle(root, opf(), container="<container"),
        lambda root: root / "missing.epub",
    ])
    def test_unreadable(self, tmp_path, make, caplog):
        caplog.set_level(logging.DEBUG)
        path = make(tmp_path)
        assert read(path) == _opf.OpfResult(_opf.UNREADABLE)
        assert _opf.read_subjects(path) is None
        assert not caplog.records

    def test_missing_container(self, tmp_path):
        path = tmp_path / "b.epub"
        (path / "OEBPS").mkdir(parents=True)
        assert read(path).state == _opf.UNREADABLE

    def test_symlinked_package_document_even_inside(self, tmp_path):
        path = bundle(tmp_path, opf("<dc:subject>A</dc:subject>"), rel="OEBPS/real.opf")
        (path / "OEBPS" / "content.opf").symlink_to("real.opf")
        (path / "META-INF" / "container.xml").write_text(CONTAINER.format(path="OEBPS/content.opf"))
        assert read(path).state == _opf.UNREADABLE

    def test_symlinked_folder(self, tmp_path):
        path = bundle(tmp_path, opf(), rel="real/content.opf")
        (path / "OEBPS").symlink_to("real")
        (path / "META-INF" / "container.xml").write_text(CONTAINER.format(path="OEBPS/content.opf"))
        assert read(path).state == _opf.UNREADABLE

    def test_fifo(self, tmp_path):
        path = bundle(tmp_path, None)
        (path / "OEBPS").mkdir()
        os.mkfifo(path / "OEBPS" / "content.opf")
        started = time.perf_counter()
        assert read(path).state == _opf.UNREADABLE
        # Wall time: a blocking open would wait for a writer forever.
        assert time.perf_counter() - started < 5

    def test_unknown_encoding(self, tmp_path):
        package = opf("<dc:subject>A</dc:subject>").replace('encoding="UTF-8"', 'encoding="x-nonexistent"')
        assert read(bundle(tmp_path, package)).state == _opf.UNREADABLE


# -- iCloud placeholders ----------------------------------------------------------


class TestPlaceholders:
    def test_dataless_folder_is_never_looked_into(self, tmp_path, patch_stat, opens):
        path = bundle(tmp_path, opf("<dc:subject>A</dc:subject>"))
        seen = patch_stat("META-INF", st_flags=_icloud.SF_DATALESS)
        assert read(path) == _opf.OpfResult(_opf.NOT_DOWNLOADED)
        assert not any(name.endswith("container.xml") for name in seen) and not opens

    def test_dataless_package_document_is_never_opened(self, tmp_path, patch_stat, opens):
        path = bundle(tmp_path, opf("<dc:subject>A</dc:subject>"))
        patch_stat("content.opf", st_blocks=0)
        assert read(path).state == _opf.NOT_DOWNLOADED
        assert "content.opf" not in opens
        assert _opf.read_subjects(path) is None

    def test_compressed_file_is_read(self, tmp_path, patch_stat):
        path = bundle(tmp_path, opf("<dc:subject>A</dc:subject>"))
        patch_stat("content.opf", st_blocks=0, st_flags=_icloud.UF_COMPRESSED)
        assert fields(path).subjects == ("A",)

    def test_folders_with_no_blocks_are_local(self, tmp_path, patch_stat):
        # APFS reports 0 blocks for every folder.
        path = bundle(tmp_path, opf("<dc:subject>A</dc:subject>"))
        for name in ("META-INF", "OEBPS", path.name):
            patch_stat(name, st_blocks=0, st_size=160)
        assert fields(path).subjects == ("A",)

    def test_edeadlk_is_not_downloaded(self, tmp_path, monkeypatch):
        path = bundle(tmp_path, opf())

        def deadlock(*args, **kwargs):
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")

        monkeypatch.setattr(os, "read", deadlock)
        assert read(path).state == _opf.NOT_DOWNLOADED


# -- caches ------------------------------------------------------------------------


class TestCache:
    def test_second_read_comes_from_the_cache(self, tmp_path, reads):
        path = bundle(tmp_path, opf("<dc:subject>A</dc:subject>"))
        first = read(path)
        assert read(path) == first and len(reads) == 2  # container.xml and the OPF, once

    def test_a_changed_package_document_is_read_again(self, tmp_path, reads):
        path = bundle(tmp_path, opf("<dc:subject>A</dc:subject>"))
        assert fields(path).subjects == ("A",)
        target = path / "OEBPS" / "content.opf"
        target.write_text(opf("<dc:subject>B</dc:subject>"))
        os.utime(target, ns=(1, target.stat().st_mtime_ns + 1_000_000))
        assert fields(path).subjects == ("B",) and len(reads) == 4

    def test_placeholder_then_local_again(self, tmp_path, patch_stat):
        path = bundle(tmp_path, opf("<dc:subject>A</dc:subject>"))
        assert read(path).state == _opf.READ
        patch_stat("container.xml", st_flags=_icloud.SF_DATALESS)
        assert read(path).state == _opf.NOT_DOWNLOADED
        patch_stat("container.xml")  # flag cleared; inode, mtime and size unchanged
        assert read(path).state == _opf.READ

    def test_io_errors_are_not_cached(self, tmp_path, monkeypatch, reads):
        path = bundle(tmp_path, opf("<dc:subject>A</dc:subject>"))
        real = _icloud.read_local
        failed = []

        def once(root, rel, **kwargs):
            if not failed:
                failed.append(rel)
                raise _icloud._IOFailed()
            return real(root, rel, **kwargs)

        monkeypatch.setattr(_icloud, "read_local", once)
        assert read(path) == _opf.OpfResult(_opf.UNREADABLE)
        assert read(path).state == _opf.READ

    def test_malformed_is_read_once_while_unchanged(self, tmp_path, reads):
        path = bundle(tmp_path, "<package><broken")
        assert read(path).state == _opf.UNREADABLE
        assert read(path).state == _opf.UNREADABLE
        assert reads == [_opf.CONTAINER, "OEBPS/content.opf"]

    def test_container_decided_result_is_cached(self, tmp_path, reads):
        path = bundle(tmp_path, None, container="<container/>")
        assert read(path).state == read(path).state == _opf.UNREADABLE
        assert reads == [_opf.CONTAINER]

    def test_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_opf._metadata_cache, "max_entries", 4)
        monkeypatch.setattr(_opf._subject_index, "max_entries", 4)
        for i in range(10):
            path = bundle(tmp_path, opf(f"<dc:subject>S{i}</dc:subject>"), name=f"b{i}.epub")
            read(path)
            _opf.read_subjects(path)
        assert len(_opf._metadata_cache) == 4 and len(_opf._subject_index) == 4

    def test_weight_bound(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_opf._metadata_cache, "max_weight", 100_000)
        for i in range(10):
            read(bundle(tmp_path, opf("<dc:description>" + "w " * 7000 + "</dc:description>"),
                        name=f"b{i}.epub"))
        assert 0 < _opf._metadata_cache.weight <= 100_000 and len(_opf._metadata_cache) < 10

    def test_oversized_entry_is_returned_not_stored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_opf._metadata_cache, "max_weight", 10)
        assert read(bundle(tmp_path, opf("<dc:subject>A</dc:subject>"))).state == _opf.READ
        assert len(_opf._metadata_cache) == 0

    def test_clear_content_cache_empties_both(self, tmp_path, reads):
        path = bundle(tmp_path, opf("<dc:subject>A</dc:subject>"))
        read(path)
        _opf.read_subjects(path)
        assert len(_opf._metadata_cache) == 1 and len(_opf._subject_index) == 1
        clear_content_cache()
        assert len(_opf._metadata_cache) == 0 and len(_opf._subject_index) == 0

    def test_subjects_from_the_metadata_cache(self, tmp_path, reads):
        path = bundle(tmp_path, opf("<dc:subject>Gödel</dc:subject>"))
        read(path)
        assert _opf.read_subjects(path) == ("godel",) and len(reads) == 2

    def test_threads(self, tmp_path):
        paths = [bundle(tmp_path, opf(f"<dc:subject>S{i}</dc:subject>"), name=f"b{i}.epub") for i in range(3)]
        expected = [read(p) for p in paths]
        clear_content_cache()
        errors, barrier = [], threading.Barrier(8)

        def work():
            barrier.wait()
            for _ in range(30):
                for p, want in zip(paths, expected):
                    if read(p) != want or _opf.read_subjects(p) != want.fields.subjects and \
                            _opf.read_subjects(p) != tuple(s.lower() for s in want.fields.subjects):
                        errors.append(p)
                clear_content_cache()

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# -- privacy ------------------------------------------------------------------------


class TestPrivacy:
    TITLE = "A Very Private Title"

    def test_nothing_logged_or_printed(self, tmp_path, caplog, capsys, recwarn):
        caplog.set_level(logging.DEBUG)
        rel = f"OEBPS/{self.TITLE}.opf"
        for package in (opf(f"<dc:description>{self.TITLE}</dc:description>"), "<broken " + self.TITLE):
            path = bundle(tmp_path, package, rel=rel, name=f"{self.TITLE}.epub")
            read(path)
            _opf.read_subjects(path)
            clear_content_cache()
        out, err = capsys.readouterr()
        assert not caplog.records and not out and not err and not list(recwarn)

    def test_opf_module_has_no_logger(self):
        assert not any(isinstance(v, logging.Logger) for v in vars(_opf).values())
        assert "logging" not in vars(_opf)


def test_stat_identity_needs_no_mtime():
    st = SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mode=stat.S_IFREG)
    assert _opf._identity(st) == (1, 2, None, 3)
