"""The process-wide book index cache (1.11, stream 2.1): what list_chapters
and the spine API read, when the cache is used, when it is invalidated,
its bounds, and its behaviour under threads.

Every bundle is synthetic (tests/_epub_shapes.py, write_epub_bundle).
"""

from __future__ import annotations

import gc
import os
import pickle
import shutil
import signal
import sqlite3
import threading
import time
import tracemalloc
import warnings

import pytest

from py_apple_books import _epub_index
from py_apple_books import content as content_module
from py_apple_books.content import BookContent, clear_content_cache
from py_apple_books.exceptions import AppleBooksError, DRMProtectedError, UnsafeEpubEntryError
from py_apple_books.testing import write_epub_bundle
from tests import _epub_shapes
from tests._epub_shapes import SHAPES


@pytest.fixture
def builds(monkeypatch):
    """Every index build (an index-only read of a book), as bundle names."""
    seen = []
    real = _epub_index._build_index

    def build(root, root_st):
        seen.append(root.name)
        return real(root, root_st)

    monkeypatch.setattr(_epub_index, "_build_index", build)
    return seen


@pytest.fixture
def full_loads(monkeypatch):
    """Every full (1.10) load of a book, as bundle names."""
    seen = []
    real = content_module._ContainedEpubReader.load

    def load(self):
        if type(self) is content_module._ContainedEpubReader:
            seen.append(os.path.basename(self.file_name))
        return real(self)

    monkeypatch.setattr(content_module._ContainedEpubReader, "load", load)
    return seen


@pytest.fixture
def small_cache(monkeypatch):
    """Swap in a fresh cache; ``small_cache(max_weight, max_entries)``
    returns it."""
    def make(max_weight=_epub_index.MAX_CACHE_BYTES, max_entries=_epub_index.MAX_CACHE_ENTRIES):
        cache = _epub_index._IndexCache(max_weight, max_entries)
        monkeypatch.setattr(_epub_index, "_CACHE", cache)
        return cache
    return make


class _Begins:
    """Every ``_IndexCache.begin``: ``(kind, builder)`` pairs, and
    :meth:`wait_for` to wait until some number of waiters joined."""

    def __init__(self):
        self.calls = []
        self._cond = threading.Condition()

    def record(self, ident, builder):
        with self._cond:
            self.calls.append((ident[0], builder))
            self._cond.notify_all()

    def waiters(self):
        return sum(1 for _, builder in self.calls if not builder)

    def wait_for(self, waiters, timeout=10):
        with self._cond:
            return self._cond.wait_for(lambda: self.waiters() >= waiters, timeout)


@pytest.fixture
def begins(monkeypatch):
    spy = _Begins()
    real = _epub_index._IndexCache.begin

    def begin(self, ident):
        result = real(self, ident)
        spy.record(ident, result[0])
        return result

    monkeypatch.setattr(_epub_index._IndexCache, "begin", begin)
    return spy


def _run_threads(count, target, timeout=30):
    """Run ``target(n)`` in ``count`` threads started together; returns
    ``(results, errors)``."""
    barrier = threading.Barrier(count)
    results, errors = [], []

    def work(n):
        try:
            barrier.wait()
            results.append(target(n))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    assert not any(t.is_alive() for t in threads)
    return results, errors


def _touch_ns(path, delta_ns=1):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + delta_ns))


# ---------------------------------------------------------------------------
# What is read, and when the cache answers
# ---------------------------------------------------------------------------


class TestHits:
    @pytest.mark.parametrize("name", sorted(SHAPES))
    def test_index_chapters_equal_the_full_load(self, tmp_path, name):
        bundle = SHAPES[name](tmp_path)
        full = BookContent(bundle)
        expected = full._chapters_from_loaded_book(full._load_book())
        index = _epub_index._build_index(bundle, os.stat(bundle))
        assert index.chapters == tuple(expected)
        assert [c.title for c in index.chapters] == [c.title for c in expected]
        assert index.stable and index.keyed_files

    def test_index_reads_only_its_files(self, tmp_path, monkeypatch):
        bundle = _epub_shapes.gutenberg(tmp_path)  # with a 300 KB image
        reads = []
        real = content_module._read_entry

        def spy(root, href, max_bytes):
            reads.append(href)
            return real(root, href, max_bytes)

        monkeypatch.setattr(content_module, "_read_entry", spy)
        index = _epub_index._build_index(bundle, os.stat(bundle))
        assert sorted(reads) == ["META-INF/container.xml", "OEBPS/content.opf", "OEBPS/nav.xhtml",
                                 "OEBPS/toc.ncx"]
        assert index.keyed_files == ("META-INF/container.xml", "OEBPS/content.opf",
                                     "OEBPS/nav.xhtml", "OEBPS/toc.ncx")
        total = sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file())
        assert index.bytes_read < 10_000 < total

    def test_index_keeps_only_what_text_needs(self, tmp_path):
        bundle = _epub_shapes.mixed_types(tmp_path)
        index = _epub_index._build_index(bundle, os.stat(bundle))
        assert sorted(item.get_id() for item in index.book.items) == ["art", "c1", "nav", "page"]
        assert index.book.toc == [] and index.book.pages == []
        # The manifest map still names every item.
        assert set(index.manifest) == {"art", "c1", "cover", "css", "font", "nav", "ncx", "page", "pic"}
        assert index.manifest["pic"] == ("OEBPS/pic.png", "image/png", "OEBPS/pic.png")

    def test_weight_tracks_measured_memory(self, tmp_path):
        bundles = [write_epub_bundle(tmp_path / f"m{i}.epub",
                                     [(f"c{j}", f"<p>text {j}</p>") for j in range(60)],
                                     toc=[(f"Chapter {j}", f"c{j}.xhtml") for j in range(60)])
                   for i in range(5)]
        _epub_index._build_index(bundles[0], os.stat(bundles[0]))
        gc.collect()
        tracemalloc.start()
        try:
            kept = [_epub_index._build_index(b, os.stat(b)) for b in bundles]
            gc.collect()
            used = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        estimated = sum(index.weight for index in kept)
        assert 0.5 * estimated <= used <= 1.25 * estimated

    @pytest.mark.parametrize("shape", ["metadata", "names", "astral"])
    def test_weight_covers_long_package_strings(self, tmp_path, shape):
        """Strings from the package document are kept by what they cost,
        whatever their length: the package metadata isn't kept at all,
        and the names and paths that are kept (ids, hrefs, properties)
        are weighed by their size, so a crafted package can't pin memory
        the weight doesn't show."""
        big = 2_000_000
        files = [("c1", "<p>one</p>"), ("c2", "<p>two</p>")]
        kwargs = {}
        if shape == "metadata":
            kwargs["metadata_xml"] = "".join(
                f"<dc:description>{chr(97 + i) * big}</dc:description>" for i in range(3))
            kwargs["title"] = "T" * big
        else:
            mark = "\U0001F600" if shape == "astral" else ""
            files[1] = ("c2", "<p>two</p>", {"properties": mark + "p" * big})
            kwargs["extra_items"] = [("x" * big, mark + "h" * big + ".xhtml",
                                      "application/xhtml+xml", None)]
        bundle = write_epub_bundle(tmp_path / "Long.epub", files, toc=[("One", "c1.xhtml")], **kwargs)
        _epub_index._build_index(_epub_shapes.plain(tmp_path / "warm"), os.stat(tmp_path / "warm"))
        gc.collect()
        tracemalloc.start()
        try:
            index = _epub_index._build_index(bundle, os.stat(bundle))
            gc.collect()
            used = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        assert index.book.metadata == {} and index.book.guide == []
        assert index.book.title == "" and index.book.language == "en"
        assert used <= index.weight <= 4 * used + 100_000, (used, index.weight)
        # Text extraction doesn't need what was dropped.
        assert BookContent(bundle).get_spine_item_text("c2") == "two"

    def test_get_chapter_flattens_the_toc_once(self, tmp_path, monkeypatch):
        content = BookContent(_epub_shapes.plain(tmp_path))
        calls = []
        real = BookContent._chapters_from_ebooklib_toc

        def spy(self, book):
            calls.append(1)
            return real(self, book)

        monkeypatch.setattr(BookContent, "_chapters_from_ebooklib_toc", spy)
        for _ in range(5):
            for chapter in content.list_chapters():
                content.get_chapter(chapter.id)
        assert calls == [1]

    def test_two_instances_one_parse(self, tmp_path, builds, full_loads):
        bundle = _epub_shapes.plain(tmp_path)
        first = BookContent(bundle).list_chapters()
        assert (builds, full_loads) == (["Plain.epub"], ["Plain.epub"])
        second = BookContent(bundle)
        assert second.list_chapters() == first
        assert second._book is None
        assert all(a is b for a, b in zip(first, second.list_chapters()))
        assert (builds, full_loads) == (["Plain.epub"], ["Plain.epub"])

    def test_warm_pass_parses_nothing(self, tmp_path, builds, full_loads, monkeypatch):
        bundles = [SHAPES[name](tmp_path) for name in sorted(SHAPES)]
        for bundle in bundles:
            content = BookContent(bundle)
            content.list_chapters()
            content.list_spine_items()
        builds.clear()
        full_loads.clear()
        monkeypatch.setattr("py_apple_books.content.subprocess.run",
                            lambda *a, **k: pytest.fail("du ran"))
        for bundle in bundles:
            content = BookContent(bundle)
            assert content.list_chapters()
            spine = content.list_spine_items()
            content.get_spine_item_text(next(i.index for i in spine if i.readable))
        assert builds == [] and full_loads == []

    def test_returns_a_new_list_each_call(self, tmp_path):
        content = BookContent(_epub_shapes.plain(tmp_path))
        first = content.list_chapters()
        first.append("junk")
        assert len(content.list_chapters()) == 3

    def test_chapters_carry_spine_indexes_on_every_path(self, tmp_path):
        bundle = _epub_shapes.subfile(tmp_path)
        cold = BookContent(bundle).list_chapters()
        warm = BookContent(bundle).list_chapters()
        assert [c.spine_index for c in cold] == [c.spine_index for c in warm] == [0, 2]

    def test_new_api_first_then_legacy(self, tmp_path, builds, full_loads):
        bundle = _epub_shapes.plain(tmp_path)
        BookContent(bundle).list_spine_items()
        assert (builds, full_loads) == (["Plain.epub"], [])
        # The index-only read isn't trusted for list_chapters until it has
        # matched a full load once.
        expected = BookContent(bundle).list_chapters()
        assert (builds, full_loads) == (["Plain.epub"], ["Plain.epub"])
        assert BookContent(bundle).list_chapters() == expected
        assert (builds, full_loads) == (["Plain.epub"], ["Plain.epub"])

    def test_legacy_errors_are_1_10_errors_after_an_index_only_read(self, tmp_path):
        # A missing manifest item doesn't stop the index, but a full load
        # (1.10's list_chapters) still fails on it.
        bundle = _epub_shapes.gutenberg(tmp_path)
        (bundle / "OEBPS" / "big.png").unlink()
        assert BookContent(bundle).list_spine_items()
        with pytest.raises(AppleBooksError, match="Could not read EPUB entry 'OEBPS/big.png'"):
            BookContent(bundle).list_chapters()

    def test_an_index_that_disagrees_is_not_used(self, tmp_path, monkeypatch):
        bundle = _epub_shapes.plain(tmp_path)
        real = _epub_index._build_index

        def wrong(root, root_st):
            index = real(root, root_st)
            return _epub_index.replace(index, chapters=index.chapters[:1])

        monkeypatch.setattr(_epub_index, "_build_index", wrong)
        expected = ["One", "Two", "Three"]
        assert [c.title for c in BookContent(bundle).list_chapters()] == expected
        chapters = BookContent(bundle).list_chapters()
        assert [c.title for c in chapters] == expected
        assert [c.spine_index for c in chapters] == [0, 1, 2]
        assert _epub_index._verified_index(bundle) is None

    def test_an_index_failure_never_fails_list_chapters(self, tmp_path, monkeypatch):
        bundle = _epub_shapes.plain(tmp_path)

        def broken(root, root_st):
            raise RuntimeError("index bug")

        monkeypatch.setattr(_epub_index, "_build_index", broken)
        chapters = BookContent(bundle).list_chapters()
        assert [c.title for c in chapters] == ["One", "Two", "Three"]
        # spine_index still comes from the package document the full load read.
        assert [c.spine_index for c in chapters] == [0, 1, 2]

    @pytest.mark.parametrize("name", sorted(SHAPES))
    def test_spine_indexes_without_the_index_match_the_index(self, tmp_path, monkeypatch, name):
        bundle = SHAPES[name](tmp_path)
        expected = [c.spine_index for c in _epub_index._build_index(bundle, os.stat(bundle)).chapters]
        monkeypatch.setattr(_epub_index, "_index_matching", lambda path, chapters: None)
        assert [c.spine_index for c in BookContent(bundle).list_chapters()] == expected

    def test_an_unpickled_instance_without_the_index_has_no_spine_indexes(self, tmp_path, monkeypatch):
        content = BookContent(_epub_shapes.plain(tmp_path))
        content._load_book()
        clone = pickle.loads(pickle.dumps(content))
        monkeypatch.setattr(_epub_index, "_index_matching", lambda path, chapters: None)
        chapters = clone.list_chapters()
        assert [c.title for c in chapters] == ["One", "Two", "Three"]
        assert [c.spine_index for c in chapters] == [None, None, None]  # as documented


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


class TestInvalidation:
    def _opf(self, bundle):
        return bundle / "OEBPS" / "content.opf"

    def test_rewritten_package_document(self, tmp_path, builds):
        bundle = _epub_shapes.plain(tmp_path)
        assert len(BookContent(bundle).list_chapters()) == 3
        write_epub_bundle(bundle, [("ch1", "<p>a</p>"), ("ch2", "<p>b</p>")],
                          toc=[("Uno", "ch1.xhtml"), ("Dos", "ch2.xhtml")])
        assert [c.title for c in BookContent(bundle).list_chapters()] == ["Uno", "Dos"]
        assert [i.item_id for i in BookContent(bundle).list_spine_items()] == ["ch1", "ch2"]
        assert builds == ["Plain.epub", "Plain.epub"]

    def test_new_modification_time_in_nanoseconds(self, tmp_path, builds):
        bundle = _epub_shapes.plain(tmp_path)
        BookContent(bundle).list_spine_items()
        _touch_ns(self._opf(bundle))
        BookContent(bundle).list_spine_items()
        assert len(builds) == 2

    def test_edit_only_the_nav(self, tmp_path, builds):
        bundle = _epub_shapes.plain(tmp_path)
        assert BookContent(bundle).list_chapters()[0].title == "One"
        nav = bundle / "OEBPS" / "nav.xhtml"
        nav.write_text(nav.read_text().replace(">One<", ">First<"))
        assert BookContent(bundle).list_chapters()[0].title == "First"
        assert len(builds) == 2

    def test_edit_only_the_ncx(self, tmp_path, builds):
        bundle = _epub_shapes.ncx_only(tmp_path)
        assert BookContent(bundle).list_chapters()[0].title == "A"
        ncx = bundle / "OEBPS" / "toc.ncx"
        ncx.write_text(ncx.read_text().replace("<text>A</text>", "<text>Ay</text>"))
        assert BookContent(bundle).list_chapters()[0].title == "Ay"
        assert len(builds) == 2

    def test_edit_a_chapter_rebuilds_its_anchor_table_only(self, tmp_path, builds):
        bundle = _epub_shapes.gutenberg(tmp_path)
        BookContent(bundle).list_spine_items()
        before = _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml")
        assert set(before) == {"ch1", "ch2", "ch3"}
        assert _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml") is before
        body = bundle / "OEBPS" / "body.xhtml"
        body.write_text(body.read_text().replace('id="ch3"', 'id="ch3b"'))
        after = _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml")
        assert set(after) == {"ch1", "ch2", "ch3b"}
        BookContent(bundle).list_spine_items()
        assert builds == ["Gutenberg.epub"]
        assert _epub_index._CACHE.stats()["anchor"] == 2  # the old one ages out

    def test_bundle_replaced_by_rename(self, tmp_path, builds):
        bundle = _epub_shapes.plain(tmp_path)
        BookContent(bundle).list_chapters()
        staged = write_epub_bundle(tmp_path / "staged.epub", [("x1", "<p>x</p>")], toc=[("New", "x1.xhtml")])
        os.rename(bundle, tmp_path / "old.epub")
        os.rename(staged, bundle)
        assert [c.title for c in BookContent(bundle).list_chapters()] == ["New"]
        assert len(builds) == 2

    def test_removed_package_document(self, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        BookContent(bundle).list_chapters()
        self._opf(bundle).unlink()
        with pytest.raises(AppleBooksError) as exc:
            BookContent(bundle).list_chapters()
        # 1.10's error, from the full load.
        assert str(exc.value) == "Could not read EPUB entry 'OEBPS/content.opf': No such file or directory"
        with pytest.raises(AppleBooksError):
            BookContent(bundle).list_spine_items()

    def test_an_instance_keeps_what_it_read(self, tmp_path):
        bundle = _epub_shapes.plain(tmp_path)
        content = BookContent(bundle)
        chapters = content.list_chapters()
        write_epub_bundle(bundle, [("ch1", "<p>a</p>")], toc=[("Uno", "ch1.xhtml")])
        assert content.list_chapters() == chapters  # as 1.10's loaded book
        assert [c.title for c in BookContent(bundle).list_chapters()] == ["Uno"]

    def test_clear_content_cache_empties_every_kind(self, tmp_path, builds):
        bundle = _epub_shapes.gutenberg(tmp_path)
        (bundle / "META-INF" / "encryption.xml").write_text(
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"/>')
        BookContent(bundle).list_chapters()
        BookContent(bundle).list_spine_items()
        _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml")
        stats = _epub_index._CACHE.stats()
        assert stats["book"] == stats["anchor"] == stats["encryption"] == 1 and stats["weight"] > 0
        clear_content_cache()
        assert _epub_index._CACHE.stats() == {"weight": 0}
        BookContent(bundle).list_spine_items()
        assert len(builds) == 2


# ---------------------------------------------------------------------------
# Anchor tables
# ---------------------------------------------------------------------------


# Expected paths are the element steps of hand-written CFIs: from <html>,
# the n-th child element is step 2n; text, comments, the XML declaration
# and the doctype don't count.
_ANCHOR_DOC = b"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>T</title><meta charset="utf-8"/></head>
<body id="b">
  Some text <!-- a comment -->
  <section id="s1">
    <h1 id="h">Title</h1>
    <p>Para <a name="n1">one</a> and <span id="sp">x</span> tail</p>
    <p><a name="sp">same name as the span's id</a><a id="dup">first</a><em id="dup">second</em></p>
  </section>
  text between
  <section id="s2"><div><p id="deep">d</p></div><a name="n1">again</a></section>
</body>
</html>
"""

_ANCHOR_PATHS = {
    "b": (4,),                # /4: <body>, after <head> (/2)
    "s1": (4, 2),
    "h": (4, 2, 2),
    "n1": (4, 2, 4, 2),       # the first <a name="n1">; no id has that name
    "sp": (4, 2, 4, 4),       # the <span> id, not the later <a name="sp">
    "dup": (4, 2, 6, 4),      # the first element with that id
    "s2": (4, 4),
    "deep": (4, 4, 2, 2),
}


class TestAnchorTables:
    def test_paths_are_cfi_element_steps(self):
        table = _epub_index._anchor_table(_ANCHOR_DOC)
        assert dict(table) == _ANCHOR_PATHS
        assert table == _ANCHOR_PATHS and len(table) == len(_ANCHOR_PATHS)
        assert "nope" not in table and table.get("nope") is None
        with pytest.raises(KeyError):
            table["nope"]

    def test_ids_win_over_names_and_first_wins(self):
        table = _epub_index._anchor_table(
            b'<html><body><a name="x">1</a><p id="y">2</p><a name="y">3</a>'
            b'<a name="z">4</a><a name="z">5</a><p id="x">6</p></body></html>')
        # <body> is /2 (no <head>); its children /2 to /12.
        assert dict(table) == {"x": (2, 12), "y": (2, 4), "z": (2, 8)}

    def test_without_an_html_element(self):
        assert dict(_epub_index._anchor_table(b'<body><p id="a">x</p><div><i id="b"/></div></body>')) == {
            "a": (2, 2), "b": (2, 4, 2)}
        assert dict(_epub_index._anchor_table(b'<p id="a">x</p><p id="b">y</p>')) == {"a": (2,), "b": (4,)}
        assert dict(_epub_index._anchor_table(b"just text")) == {}

    def test_repr_has_no_document_text(self):
        assert repr(_epub_index._anchor_table(_ANCHOR_DOC)) == "<anchor table: 8 anchors>"

    def test_size_cap(self, monkeypatch):
        raw = b'<html><body><p id="a">x</p></body></html>'
        monkeypatch.setattr(_epub_index, "MAX_ANCHOR_BYTES", len(raw))
        assert dict(_epub_index._anchor_table(raw)) == {"a": (2, 2)}
        assert _epub_index._anchor_table(raw + b" ") is None

    def test_a_file_over_the_cap_is_not_read(self, tmp_path, monkeypatch):
        bundle = _epub_shapes.gutenberg(tmp_path)
        body = bundle / "OEBPS" / "body.xhtml"
        monkeypatch.setattr(_epub_index, "MAX_ANCHOR_BYTES", body.stat().st_size - 1)
        monkeypatch.setattr(content_module, "_read_entry", lambda *a: pytest.fail("read"))
        assert _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml") is None

    def test_deep_nesting_costs_linear_memory(self):
        # Paths are built on lookup: a table of 5,000 nested elements, each
        # with an id, keeps one (parent, step) pair per element, not 5,000
        # paths of up to 5,000 steps (about 100 MiB).
        depth = 5000
        raw = ("<html><body>" + "".join(f'<div id="d{i}">' for i in range(depth)) + "x"
               + "</div>" * depth + "</body></html>").encode()
        gc.collect()
        tracemalloc.start()
        try:
            table = _epub_index._anchor_table(raw)
            gc.collect()
            kept, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert len(table) == depth
        assert table["d0"] == (2, 2) and table[f"d{depth - 1}"] == (2,) + (2,) * depth
        assert kept < 2 * 2**20 and peak < 40 * 2**20, (kept, peak)
        assert table.weight < 2 * 2**20

    def test_a_table_too_heavy_to_keep_is_none_and_not_parsed_again(self, tmp_path, monkeypatch, small_cache):
        bundle = _epub_shapes.gutenberg(tmp_path)
        cache = small_cache(max_weight=300)  # the marker (256) fits, the table doesn't
        parses = []
        real = _epub_index._anchor_table

        def spy(raw):
            parses.append(1)
            return real(raw)

        monkeypatch.setattr(_epub_index, "_anchor_table", spy)
        assert _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml") is None
        assert _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml") is None
        assert parses == [1] and cache.stats() == {"anchor": 1, "weight": 256}

    def test_errors_name_the_entry_not_a_path(self, tmp_path):
        bundle = _epub_shapes.gutenberg(tmp_path)
        with pytest.raises(AppleBooksError) as exc:
            _epub_index._anchor_table_for(bundle, "OEBPS/missing.xhtml")
        assert not isinstance(exc.value, OSError)
        assert str(exc.value).startswith("Could not read EPUB entry 'OEBPS/missing.xhtml'")
        assert str(tmp_path) not in str(exc.value)
        with pytest.raises(UnsafeEpubEntryError):
            _epub_index._anchor_table_for(bundle, "../outside.xhtml")


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


class TestEviction:
    def test_entry_count(self, tmp_path, small_cache, builds):
        cache = small_cache(max_entries=2)
        a, b, c = (write_epub_bundle(tmp_path / f"{n}.epub", [("x", f"<p>{n}</p>")], toc=[(n, "x.xhtml")])
                   for n in "abc")
        for bundle in (a, b):
            BookContent(bundle).list_spine_items()
        BookContent(a).list_spine_items()  # a is now the most recently used
        BookContent(c).list_spine_items()  # evicts b
        assert cache.stats()["book"] == 2
        builds.clear()
        BookContent(a).list_spine_items()
        BookContent(c).list_spine_items()
        assert builds == []
        BookContent(b).list_spine_items()
        assert builds == ["b.epub"]

    def test_weight(self, tmp_path, small_cache):
        one = _epub_index._build_index(_epub_shapes.plain(tmp_path), os.stat(tmp_path / "Plain.epub"))
        cache = small_cache(max_weight=int(one.weight * 2.5))
        bundles = [write_epub_bundle(tmp_path / f"w{i}.epub", [("ch1", "<p>a</p>"), ("ch2", "<p>b</p>"),
                                                                  ("ch3", "<p>c</p>")],
                                     toc=[("One", "ch1.xhtml"), ("Two", "ch2.xhtml"), ("Three", "ch3.xhtml")])
                   for i in range(6)]
        for bundle in bundles:
            BookContent(bundle).list_spine_items()
            stats = cache.stats()
            assert stats["weight"] <= cache.max_weight
        assert 1 <= cache.stats()["book"] <= 3

    def test_an_oversize_entry_is_returned_not_stored(self, tmp_path, small_cache, builds):
        bundle = _epub_shapes.plain(tmp_path)
        small = _epub_index._anchor_table((bundle / "OEBPS" / "ch1.xhtml").read_bytes())
        index = _epub_index._build_index(bundle, os.stat(bundle))
        builds.clear()
        assert small.weight < 4 * small.weight < index.weight
        cache = small_cache(max_weight=2 * small.weight)
        # A small entry first: storing nothing for the oversize index must
        # not flush it.
        assert _epub_index._anchor_table_for(bundle, "OEBPS/ch1.xhtml") == small
        assert cache.stats() == {"anchor": 1, "weight": small.weight}
        assert len(BookContent(bundle).list_spine_items()) == 3
        assert cache.stats() == {"anchor": 1, "weight": small.weight}
        assert len(BookContent(bundle).list_chapters()) == 3
        assert cache.stats() == {"anchor": 1, "weight": small.weight}
        assert len(builds) == 2

    def test_an_instance_keeps_an_index_the_cache_cannot(self, tmp_path, small_cache, builds):
        bundle = write_epub_bundle(tmp_path / "Long.epub",
                                   [(f"c{i}", f"<p>text {i}</p>") for i in range(40)],
                                   toc=[(f"Chapter {i}", f"c{i}.xhtml") for i in range(40)])
        small_cache(max_weight=100)
        content = BookContent(bundle)
        assert len(list(content.iter_spine_text())) == 40
        assert content.get_spine_item_text(3) == "text 3"
        assert builds == ["Long.epub"]  # not one per item
        # The checks still run on every call.
        (bundle / "META-INF" / "rights.xml").write_text("<rights/>")
        with pytest.raises(DRMProtectedError):
            content.get_spine_item_text(4)
        (bundle / "META-INF" / "rights.xml").unlink()
        # A cleared cache, or a changed package document, reads it again.
        clear_content_cache()
        content.list_spine_items()
        assert len(builds) == 2
        _touch_ns(bundle / "OEBPS" / "content.opf")
        content.list_spine_items()
        content.list_spine_items()
        assert len(builds) == 3

    def test_insert_after_clear_is_dropped(self):
        cache = _epub_index._IndexCache()
        generation = cache.generation
        cache.clear()
        assert cache.insert(("book", 1, 2), "k", "v", 10, generation) is False
        assert cache.insert(("book", 1, 2), "k", "v", 10, cache.generation) is True


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


class TestThreads:
    def test_eight_threads_one_build(self, tmp_path, monkeypatch):
        bundle = _epub_shapes.plain(tmp_path)
        builds = []
        real = _epub_index._build_index

        def slow(root, root_st):
            builds.append(threading.get_ident())
            time.sleep(0.05)
            return real(root, root_st)

        monkeypatch.setattr(_epub_index, "_build_index", slow)
        barrier = threading.Barrier(8)
        results, errors = [], []

        def work():
            try:
                barrier.wait()
                results.append(BookContent(bundle).list_spine_items())
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == [] and len(results) == 8
        assert len(builds) == 1
        assert all(r == results[0] for r in results)

    def test_a_failed_build_lets_waiters_build(self, tmp_path, monkeypatch, begins):
        bundle = _epub_shapes.plain(tmp_path)
        calls = []
        real = _epub_index._build_index
        gate = threading.Event()

        def first_fails(root, root_st):
            calls.append(1)
            if len(calls) == 1:
                gate.wait(5)
                raise AppleBooksError("transient")
            return real(root, root_st)

        monkeypatch.setattr(_epub_index, "_build_index", first_fails)
        errors, results = [], []

        def first():
            try:
                BookContent(bundle).list_spine_items()
            except AppleBooksError as e:
                errors.append(e)

        def second():
            results.append(BookContent(bundle).list_spine_items())

        t1 = threading.Thread(target=first)
        t1.start()
        while not calls:
            time.sleep(0.001)
        t2 = threading.Thread(target=second)
        t2.start()
        assert begins.wait_for(1)  # t2 waits for t1's build
        gate.set()
        t1.join(10)
        t2.join(10)
        assert len(errors) == 1 and len(results) == 1 and len(results[0]) == 3
        assert begins.calls == [("book", True), ("book", False)]
        assert len(calls) == 2

    def test_waiters_share_a_value_the_cache_cannot_keep(self, tmp_path, monkeypatch, small_cache, begins):
        small_cache(max_weight=100)  # no index fits
        bundle = _epub_shapes.plain(tmp_path)
        builds = []
        real = _epub_index._build_index
        release = threading.Event()

        def slow(root, root_st):
            builds.append(1)
            release.wait(10)
            return real(root, root_st)

        monkeypatch.setattr(_epub_index, "_build_index", slow)
        threading.Thread(target=lambda: (begins.wait_for(7), release.set())).start()
        results, errors = _run_threads(8, lambda n: BookContent(bundle).list_spine_items())
        assert errors == [] and len(results) == 8 and all(r == results[0] for r in results)
        assert len(builds) == 1

    def test_a_clear_during_a_build_does_not_make_its_waiters_build(self, tmp_path, monkeypatch, begins):
        bundle = _epub_shapes.plain(tmp_path)
        builds = []
        real = _epub_index._build_index
        release = threading.Event()

        def slow(root, root_st):
            builds.append(1)
            release.wait(10)
            return real(root, root_st)

        monkeypatch.setattr(_epub_index, "_build_index", slow)

        def clear_then_release():
            begins.wait_for(7)
            clear_content_cache()
            release.set()

        threading.Thread(target=clear_then_release).start()
        results, errors = _run_threads(8, lambda n: BookContent(bundle).list_spine_items())
        assert errors == [] and len(results) == 8 and len(builds) == 1
        # Nothing from before the clear was stored; a new call builds.
        assert _epub_index._CACHE.stats() == {"weight": 0}
        BookContent(bundle).list_spine_items()
        assert len(builds) == 2

    def test_a_waiter_rebuilds_when_the_files_changed_meanwhile(self, tmp_path, monkeypatch, begins):
        bundle = _epub_shapes.plain(tmp_path)
        builds = []
        real = _epub_index._build_index
        built, release = threading.Event(), threading.Event()

        def slow(root, root_st):
            builds.append(1)
            index = real(root, root_st)
            if len(builds) == 1:
                built.set()
                release.wait(10)
            return index

        monkeypatch.setattr(_epub_index, "_build_index", slow)

        def edit_then_release():
            # The first build has read the files, the other thread waits.
            built.wait(10)
            begins.wait_for(1)
            nav = bundle / "OEBPS" / "nav.xhtml"
            nav.write_text(nav.read_text().replace(">One<", ">First<"))
            release.set()

        threading.Thread(target=edit_then_release).start()
        results, errors = _run_threads(2, lambda n: [c.toc_orders for c in BookContent(bundle).list_spine_items()])
        assert errors == [] and len(builds) == 2
        assert BookContent(bundle).list_chapters()[0].title == "First"

    def test_eight_threads_one_anchor_parse(self, tmp_path, monkeypatch):
        bundle = _epub_shapes.gutenberg(tmp_path)
        parses = []
        real = _epub_index._anchor_table

        def slow(raw):
            parses.append(1)
            time.sleep(0.05)
            return real(raw)

        monkeypatch.setattr(_epub_index, "_anchor_table", slow)
        results, errors = _run_threads(8, lambda n: _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml"))
        assert errors == [] and len(results) == 8
        assert len(parses) == 1 and all(r is results[0] for r in results)
        assert set(results[0]) == {"ch1", "ch2", "ch3"}

    def test_encryption_verdicts_never_flip_under_threads(self, tmp_path):
        fonts = _epub_shapes.plain(tmp_path)
        (fonts / "META-INF" / "encryption.xml").write_text(
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
            '<enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
            '<enc:CipherData><enc:CipherReference URI="OEBPS/font.otf"/></enc:CipherData>'
            '</enc:EncryptedData></encryption>')
        locked = write_epub_bundle(tmp_path / "Locked.epub", [("c1", "<p>x</p>")], toc=[("One", "c1.xhtml")])
        (locked / "META-INF" / "encryption.xml").write_text(
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
            '<enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/>'
            '<enc:CipherData><enc:CipherReference URI="OEBPS/c1.xhtml"/></enc:CipherData>'
            '</enc:EncryptedData></encryption>')
        assert _epub_index._gate_path(fonts).reason is None
        assert _epub_index._gate_path(locked).reason == _epub_index.UnavailableReason.DRM

        def work(n):
            seen = set()
            for i in range(60):
                if (n + i) % 13 == 0:
                    clear_content_cache()
                seen.add((_epub_index._gate_path(fonts).reason, _epub_index._gate_path(locked).reason))
            return seen

        results, errors = _run_threads(12, work)
        assert errors == [] and len(results) == 12
        assert set().union(*results) == {(None, _epub_index.UnavailableReason.DRM)}

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
    def test_a_fork_during_a_build(self, tmp_path, monkeypatch):
        """A child forked while a thread of its parent builds a book's
        index doesn't wait for that build (its thread wasn't copied)."""
        bundle = _epub_shapes.plain(tmp_path)
        started, release = threading.Event(), threading.Event()
        real = _epub_index._build_index

        def paused(root, root_st):
            if threading.current_thread() is not threading.main_thread():
                started.set()
                release.wait(20)
            return real(root, root_st)

        monkeypatch.setattr(_epub_index, "_build_index", paused)
        worker = threading.Thread(target=lambda: BookContent(bundle).list_spine_items())
        worker.start()
        try:
            assert started.wait(10)
            read_end, write_end = os.pipe()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)  # fork() with other threads alive
                pid = os.fork()
            if pid == 0:  # pragma: no cover - child
                status = 1
                try:
                    signal.signal(signal.SIGALRM, signal.SIG_DFL)
                    signal.alarm(10)  # a hang fails the test instead of the suite
                    os.close(read_end)
                    result = (len(BookContent(bundle).list_chapters()),
                              len(BookContent(bundle).list_spine_items()),
                              _epub_index._CACHE.stats().get("book"))
                    os.write(write_end, repr(result).encode())
                    status = 0
                finally:
                    os._exit(status)
            os.close(write_end)
            with os.fdopen(read_end, "rb") as pipe:
                output = pipe.read()
            _, status = os.waitpid(pid, 0)
        finally:
            release.set()
            worker.join(10)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, status
        assert output == repr((3, 3, 1)).encode()
        # The parent's own build finished and was stored as usual.
        assert _epub_index._CACHE.stats().get("book") == 1

    def test_after_fork_in_child_resets_the_cache_state(self):
        cache = _epub_index._IndexCache()
        generation = cache.generation
        cache.insert(("anchor", 1), "k", "v", 10, generation)
        builder, flight, _ = cache.begin(("book", 1, 2))
        assert builder
        cache._after_fork_in_child()
        assert cache.begin(("book", 1, 2))[0]  # nobody waits for the parent's build
        assert cache.insert(("book", 1, 2), "k", "v", 10, generation) is False
        assert cache.stats() == {"anchor": 1, "weight": 10}  # entries kept
        cache._lock.acquire()  # a fork while another thread held the lock
        cache._after_fork_in_child()
        assert not cache._lock.locked() and cache.stats() == {"weight": 0}

    def test_clear_during_a_build_leaves_the_cache_empty(self, tmp_path, monkeypatch):
        bundle = _epub_shapes.plain(tmp_path)
        started, release = threading.Event(), threading.Event()
        real = _epub_index._build_index

        def paused(root, root_st):
            index = real(root, root_st)
            started.set()
            release.wait(5)
            return index

        monkeypatch.setattr(_epub_index, "_build_index", paused)
        worker = threading.Thread(target=lambda: BookContent(bundle).list_spine_items())
        worker.start()
        assert started.wait(5)
        clear_content_cache()
        release.set()
        worker.join(10)
        assert _epub_index._CACHE.stats() == {"weight": 0}

    def test_a_call_after_a_clear_does_not_take_an_older_build(self, tmp_path, monkeypatch, begins):
        # An anchor table has no files to re-check, so a caller after
        # clear_content_cache() must build its own instead of waiting for
        # (and taking) the value of a build that began before the clear.
        bundle = _epub_shapes.gutenberg(tmp_path)
        started, release = threading.Event(), threading.Event()
        parses = []
        real = _epub_index._anchor_table

        def paused(raw):
            parses.append(1)
            if len(parses) == 1:
                started.set()
                release.wait(5)
            return real(raw)

        monkeypatch.setattr(_epub_index, "_anchor_table", paused)
        worker = threading.Thread(target=lambda: _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml"))
        worker.start()
        try:
            assert started.wait(5)
            clear_content_cache()
            table = _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml")
        finally:
            release.set()
            worker.join(10)
        assert begins.calls == [("anchor", True), ("anchor", True)]
        assert len(parses) == 2 and set(table) == {"ch1", "ch2", "ch3"}
        # Only the build after the clear is kept.
        assert _epub_index._CACHE.stats() == {"anchor": 1, "weight": table.weight}
        assert _epub_index._anchor_table_for(bundle, "OEBPS/body.xhtml") is table

    def test_many_threads_many_calls(self, tmp_path):
        bundles = [SHAPES[name](tmp_path) for name in ("plain", "gutenberg", "toc_pages")]
        expected = {}
        for bundle in bundles:
            content = BookContent(bundle)
            expected[bundle] = (content.list_chapters(), content.list_spine_items(),
                                [c.text for c in content.iter_spine_text()])
        clear_content_cache()
        errors = []
        barrier = threading.Barrier(8)

        def work(seed):
            try:
                barrier.wait()
                for i in range(50):
                    bundle = bundles[(seed + i) % 3]
                    content = BookContent(bundle)
                    if i % 7 == 0:
                        clear_content_cache()
                    got = (content.list_chapters(), content.list_spine_items(),
                           [c.text for c in content.iter_spine_text()])
                    assert got == expected[bundle]
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert errors == []


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------


def test_synthetic_scale(make_library, tmp_path, small_cache, capsys):
    """5,000 library books, 300 of them with a bundle: every book through
    the gate, with a weight cap that forces eviction. Prints the memory
    the pass left allocated, and its peak (tracemalloc; reported, not
    asserted)."""
    from py_apple_books import PyAppleBooks

    lib = make_library()
    lib.populate(books=5000, annotations_per_book=10)
    bundles_dir = tmp_path / "bundles"
    con = sqlite3.connect(lib.library_path)
    try:
        ids = [r[0] for r in con.execute("SELECT Z_PK FROM ZBKLIBRARYASSET ORDER BY Z_PK")]
        assert len(ids) == 5000
        with_files = ids[::16][:300]
        for n, pk in enumerate(with_files):
            path = write_epub_bundle(bundles_dir / f"b{n}.epub",
                                     [("c1", f"<p>book {n} one</p>"), ("c2", f"<p>book {n} two</p>")],
                                     toc=[("One", "c1.xhtml"), ("Two", "c2.xhtml")])
            con.execute("UPDATE ZBKLIBRARYASSET SET ZPATH = ? WHERE Z_PK = ?", (str(path), pk))
        con.commit()
    finally:
        con.close()
    cache = small_cache(max_weight=200_000)
    api = PyAppleBooks(data_dir=lib.data_dir)
    books = list(api.list_books(limit=None))
    reasons = {}
    tracemalloc.start()
    started = time.perf_counter()
    for book in books:
        reason, key = _epub_index._book_gate(book)
        reasons[str(reason)] = reasons.get(str(reason), 0) + 1
        assert cache.stats()["weight"] <= cache.max_weight
        if reason is None:
            assert key is not None
    elapsed = time.perf_counter() - started
    left, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert reasons.get("None") == 300 and sum(reasons.values()) == 5000, reasons
    assert 0 < cache.stats()["book"] < 300  # evicted down to the cap
    with capsys.disabled():
        print(f"\n[scale] 5000 books, 300 bundles: {elapsed:.2f}s (traced), allocated "
              f"{left / 2**20:.1f} MiB after, peak {peak / 2**20:.1f} MiB, cache {cache.stats()}")
    shutil.rmtree(bundles_dir)
