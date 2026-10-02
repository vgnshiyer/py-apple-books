"""The process-wide book index cache (1.11, stream 2.1): what list_chapters
and the spine API read, when the cache is used, when it is invalidated,
its bounds, and its behaviour under threads.

Every bundle is synthetic (tests/_epub_shapes.py, write_epub_bundle).
"""

from __future__ import annotations

import gc
import os
import shutil
import sqlite3
import threading
import time
import tracemalloc

import pytest

from py_apple_books import _epub_index
from py_apple_books import content as content_module
from py_apple_books.content import BookContent, clear_content_cache
from py_apple_books.exceptions import AppleBooksError
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
        assert [c.title for c in BookContent(bundle).list_chapters()] == expected
        assert _epub_index._verified_index(bundle) is None

    def test_an_index_failure_never_fails_list_chapters(self, tmp_path, monkeypatch):
        bundle = _epub_shapes.plain(tmp_path)

        def broken(root, root_st):
            raise RuntimeError("index bug")

        monkeypatch.setattr(_epub_index, "_build_index", broken)
        chapters = BookContent(bundle).list_chapters()
        assert [c.title for c in chapters] == ["One", "Two", "Three"]
        assert [c.spine_index for c in chapters] == [None, None, None]


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
        cache = small_cache(max_weight=100)
        bundle = _epub_shapes.plain(tmp_path)
        assert len(BookContent(bundle).list_spine_items()) == 3
        assert cache.stats() == {"weight": 0}
        assert len(BookContent(bundle).list_chapters()) == 3
        assert len(builds) == 2

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

    def test_a_failed_build_lets_waiters_build(self, tmp_path, monkeypatch):
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
        time.sleep(0.05)
        gate.set()
        t1.join(10)
        t2.join(10)
        assert len(errors) == 1 and len(results) == 1 and len(results[0]) == 3

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
