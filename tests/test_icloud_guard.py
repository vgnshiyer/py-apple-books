"""Tests for py_apple_books._icloud: the guard that keeps every read from
downloading an evicted iCloud file.

A real placeholder can't be made in a test, so placeholders are faked by
patching ``_icloud.lstat`` / ``_icloud.stat`` (the module's patch points)
to report ``SF_DATALESS`` for chosen paths, and the I/O policy calls by
replacing the looked-up libSystem functions. One test also checks the
real thread policy on macOS (it touches no file).
"""

from __future__ import annotations

import errno
import os
import pathlib
import stat
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from py_apple_books import _icloud
from py_apple_books.exceptions import BookNotDownloadedError
from py_apple_books.testing import write_epub
from tests import _fs_audit

ICLOUD = _icloud.FileState


def fake_stat(real=None, **changes):
    """A stat-like object: ``real``'s fields (or a regular file's), with
    ``changes`` applied."""
    fields = {"st_mode": stat.S_IFREG | 0o644, "st_size": 10, "st_blocks": 8,
              "st_flags": 0, "st_dev": 1, "st_ino": 1}
    if real is not None:
        for name in list(fields):
            fields[name] = getattr(real, name, fields[name])
    fields.update(changes)
    return SimpleNamespace(**fields)


def dataless(real):
    return fake_stat(real, st_flags=_icloud.flags(real) | _icloud.SF_DATALESS)


@pytest.fixture
def mark_dataless(monkeypatch):
    """``mark(*paths)``: ``_icloud.lstat``/``stat`` report each path as an
    iCloud placeholder (by path, or by name for ``dir_fd`` lookups).
    Returns the list of every ``lstat``/``stat`` call made."""
    real_lstat, real_stat = os.lstat, os.stat
    marked = set()
    calls = []

    def lstat(path, *, dir_fd=None):
        calls.append(("lstat", os.fspath(path), dir_fd))
        st = real_lstat(path, dir_fd=dir_fd)
        return dataless(st) if os.fspath(path) in marked else st

    def stat_(path):
        calls.append(("stat", os.fspath(path), None))
        st = real_stat(path)
        return dataless(st) if os.fspath(path) in marked else st

    monkeypatch.setattr(_icloud, "lstat", lstat)
    monkeypatch.setattr(_icloud, "stat", stat_)

    def mark(*paths):
        marked.update(os.fspath(p) for p in paths)
        return calls

    return mark


@pytest.fixture
def scandir_spy(monkeypatch):
    seen = []
    real = os.scandir

    def spy(path="."):
        seen.append(os.fspath(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", spy)
    return seen


@pytest.fixture
def fake_policy(monkeypatch):
    """Replace the libSystem policy functions with recorders. The fake
    keeps one value per (scope, thread)."""
    values = {}
    calls = []

    def key(scope):
        return (scope, threading.get_ident() if scope == _icloud.IOPOL_SCOPE_THREAD else None)

    def get(kind, scope):
        assert kind == _icloud.IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES
        calls.append(("get", scope))
        return values.get(key(scope), 0)

    def set_(kind, scope, value):
        assert kind == _icloud.IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES
        calls.append(("set", scope, value))
        values[key(scope)] = value
        return 0

    state = SimpleNamespace(get=get, set=set_, calls=calls, values=values,
                            thread_value=lambda: values.get(key(_icloud.IOPOL_SCOPE_THREAD), 0))
    monkeypatch.setattr(_icloud, "_policy_loaded", True)
    monkeypatch.setattr(_icloud, "_policy_fns", (get, set_))
    return state


@pytest.fixture
def unloaded_policy(monkeypatch):
    """Start with the policy functions not looked up yet (restored after)."""
    monkeypatch.setattr(_icloud, "_policy_loaded", False)
    monkeypatch.setattr(_icloud, "_policy_fns", None)


@pytest.fixture
def bundle(tmp_path):
    return write_epub(tmp_path / "Book.epub", "Book")


# ---------------------------------------------------------------------------
# Constants and stat helpers
# ---------------------------------------------------------------------------


def test_constants():
    assert _icloud.SF_DATALESS == 0x40000000
    assert _icloud.UF_COMPRESSED == 0x20
    if hasattr(stat, "SF_DATALESS"):
        assert stat.SF_DATALESS == _icloud.SF_DATALESS
    if hasattr(stat, "UF_COMPRESSED"):
        assert stat.UF_COMPRESSED == _icloud.UF_COMPRESSED
    assert _icloud.PARTIAL_DOWNLOAD_MESSAGE == (
        "Part of this book is stored only in iCloud. Open it in Apple Books "
        "to download it, then try again.")
    assert "/" not in _icloud.PARTIAL_DOWNLOAD_MESSAGE


DIR = stat.S_IFDIR | 0o755
REG = stat.S_IFREG | 0o644


@pytest.mark.parametrize("st, expected", [
    pytest.param(fake_stat(st_flags=_icloud.SF_DATALESS), True, id="flag-on-file"),
    pytest.param(fake_stat(st_mode=DIR, st_blocks=0, st_flags=_icloud.SF_DATALESS), True, id="flag-on-dir"),
    pytest.param(fake_stat(st_size=100, st_blocks=0), True, id="file-size-without-blocks"),
    pytest.param(fake_stat(st_size=100, st_blocks=0, st_flags=_icloud.UF_COMPRESSED), False,
                 id="compressed-file-without-blocks"),
    pytest.param(fake_stat(st_mode=DIR, st_size=96, st_blocks=0), False, id="dir-without-blocks-or-flag"),
    pytest.param(fake_stat(st_size=0, st_blocks=0), False, id="empty-file"),
    pytest.param(fake_stat(st_size=100, st_blocks=8), False, id="local-file"),
    pytest.param(fake_stat(st_mode=stat.S_IFLNK | 0o755, st_size=10, st_blocks=0), False,
                 id="symlink-without-blocks"),
])
def test_is_dataless(st, expected):
    assert _icloud.is_dataless(st) is expected


def test_stat_without_flags_linux_shape():
    linux = SimpleNamespace(st_mode=REG, st_size=100, st_blocks=8)
    assert _icloud.flags(linux) == 0
    assert _icloud.is_dataless(linux) is False
    assert _icloud.is_dataless(SimpleNamespace(st_mode=REG, st_size=100, st_blocks=0)) is True
    assert _icloud.is_dataless(SimpleNamespace(st_mode=REG, st_size=100)) is False


def test_real_bundle_is_not_dataless(bundle):
    seen = 0
    for folder, dirs, files in os.walk(bundle):
        for name in [*dirs, *files, None]:
            path = os.path.join(folder, name) if name else folder
            assert not _icloud.is_dataless(os.lstat(path)), path
            seen += 1
    assert seen > 3


def test_icloud_stub(tmp_path):
    book = tmp_path / "Book.epub"
    assert _icloud.icloud_stub(book) is False
    (tmp_path / ".Book.epub.icloud").write_bytes(b"stub")
    assert _icloud.icloud_stub(book) is True
    assert _icloud.icloud_stub(str(book) + "/") is True
    assert _icloud.icloud_stub(tmp_path / "Other.pdf") is False


def test_not_downloaded_error():
    for code in (errno.EDEADLK, errno.ETIMEDOUT):
        err = _icloud.not_downloaded_error(OSError(code, os.strerror(code), "/Users/x/secret.xhtml"))
        assert isinstance(err, BookNotDownloadedError)
        assert str(err) == _icloud.PARTIAL_DOWNLOAD_MESSAGE
    assert _icloud.not_downloaded_error(FileNotFoundError(errno.ENOENT, "gone")) is None
    assert _icloud.not_downloaded_error(OSError("no errno")) is None
    assert _icloud.not_downloaded_error(ValueError("x")) is None


# ---------------------------------------------------------------------------
# no_materialize
# ---------------------------------------------------------------------------


class TestNoMaterialize:
    def test_off_inside_and_restored_after(self, fake_policy):
        fake_policy.values[(_icloud.IOPOL_SCOPE_THREAD, threading.get_ident())] = 2
        with _icloud.no_materialize():
            assert fake_policy.thread_value() == _icloud.IOPOL_MATERIALIZE_DATALESS_FILES_OFF
        assert fake_policy.thread_value() == 2
        assert ("set", _icloud.IOPOL_SCOPE_THREAD, 1) in fake_policy.calls
        assert not any(c[0] == "set" and c[1] == _icloud.IOPOL_SCOPE_PROCESS for c in fake_policy.calls)

    def test_restored_after_an_exception(self, fake_policy):
        with pytest.raises(KeyError):
            with _icloud.no_materialize():
                assert fake_policy.thread_value() == 1
                raise KeyError("boom")
        assert fake_policy.thread_value() == 0

    def test_nests(self, fake_policy):
        with _icloud.no_materialize():
            with _icloud.no_materialize():
                assert fake_policy.thread_value() == 1
            assert fake_policy.thread_value() == 1
        assert fake_policy.thread_value() == 0

    def test_other_threads_unaffected(self, fake_policy):
        seen = []
        with _icloud.no_materialize():
            t = threading.Thread(target=lambda: seen.append(fake_policy.thread_value()))
            t.start()
            t.join()
            assert fake_policy.thread_value() == 1
        assert seen == [0]

    def test_unreadable_policy_is_left_alone(self, fake_policy, monkeypatch):
        sets = []
        monkeypatch.setattr(_icloud, "_policy_fns", (lambda k, s: -1, lambda *a: sets.append(a) or 0))
        with _icloud.no_materialize():
            pass
        assert sets == []

    def test_failed_set_is_not_restored(self, monkeypatch):
        sets = []

        def set_(*args):
            sets.append(args)
            return -1

        monkeypatch.setattr(_icloud, "_policy_loaded", True)
        monkeypatch.setattr(_icloud, "_policy_fns", (lambda k, s: 0, set_))
        with _icloud.no_materialize():
            pass
        assert len(sets) == 1  # the attempt only, no restore

    def test_raising_functions_are_a_no_op(self, monkeypatch):
        def boom(*args):
            raise OSError("no")

        monkeypatch.setattr(_icloud, "_policy_loaded", True)
        monkeypatch.setattr(_icloud, "_policy_fns", (boom, boom))
        ran = []
        with _icloud.no_materialize():
            ran.append(1)
        assert ran == [1]

    def test_lookup_happens_once_under_concurrent_first_use(self, unloaded_policy, monkeypatch):
        loads = []

        def load():
            loads.append(threading.get_ident())
            time.sleep(0.05)
            return None

        monkeypatch.setattr(_icloud, "_load_policy_functions", load)
        barrier = threading.Barrier(16)
        errors = []

        def use():
            try:
                barrier.wait()
                with _icloud.no_materialize():
                    pass
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=use) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(loads) == 1

    def test_off_darwin_is_a_no_op_without_ctypes(self, unloaded_policy, monkeypatch):
        monkeypatch.setattr(_icloud, "sys", SimpleNamespace(platform="linux"))
        with _fs_audit.record() as rec:
            with _icloud.no_materialize():
                pass
        assert _icloud._policy_fns is None and _icloud._policy_loaded
        assert rec.of("ctypes.dlopen") == []

    def test_library_load_failure_is_a_no_op(self, unloaded_policy, monkeypatch):
        import ctypes

        def fail(*args, **kwargs):
            raise OSError("no libSystem")

        monkeypatch.setattr(ctypes, "CDLL", fail)
        with _icloud.no_materialize():
            pass
        assert _icloud._policy_fns is None
        assert _icloud.disable_materialization_for_process() is False

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS I/O policy")
    def test_real_thread_policy(self, unloaded_policy):
        fns = _icloud._policy_functions()
        assert fns is not None
        get, _ = fns
        thread = _icloud.IOPOL_SCOPE_THREAD
        kind = _icloud.IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES
        before = get(kind, thread)
        other = []
        with _icloud.no_materialize():
            assert get(kind, thread) == _icloud.IOPOL_MATERIALIZE_DATALESS_FILES_OFF
            t = threading.Thread(target=lambda: other.append(get(kind, thread)))
            t.start()
            t.join()
        assert get(kind, thread) == before
        assert other == [before]


def test_disable_materialization_for_process(fake_policy, monkeypatch):
    assert _icloud.disable_materialization_for_process() is True
    assert fake_policy.values[(_icloud.IOPOL_SCOPE_PROCESS, None)] == 1
    monkeypatch.setattr(_icloud, "_policy_fns", (fake_policy.get, lambda *a: -1))
    assert _icloud.disable_materialization_for_process() is False
    monkeypatch.setattr(_icloud, "_policy_fns", None)
    assert _icloud.disable_materialization_for_process() is False


# ---------------------------------------------------------------------------
# walk_bundle_local / local_file_state
# ---------------------------------------------------------------------------


class TestWalkBundleLocal:
    def test_local_bundle(self, bundle, fake_policy):
        assert _icloud.walk_bundle_local(bundle) is ICLOUD.LOCAL
        assert ("set", _icloud.IOPOL_SCOPE_THREAD, 1) in fake_policy.calls

    def test_missing(self, tmp_path):
        assert _icloud.walk_bundle_local(tmp_path / "gone.epub") is ICLOUD.MISSING

    def test_stub_inside(self, bundle):
        (bundle / "OEBPS" / ".chap1.xhtml.icloud").write_bytes(b"")
        assert _icloud.walk_bundle_local(bundle) is ICLOUD.ICLOUD_STUB

    def test_stub_next_to_root(self, tmp_path):
        (tmp_path / ".Gone.epub.icloud").write_bytes(b"")
        assert _icloud.walk_bundle_local(tmp_path / "Gone.epub") is ICLOUD.ICLOUD_STUB

    def test_dataless_folder_is_never_listed(self, bundle, mark_dataless, scandir_spy):
        epub_dir = bundle / "OEBPS"
        mark_dataless(epub_dir)
        assert _icloud.walk_bundle_local(bundle) is ICLOUD.DATALESS
        assert os.fspath(epub_dir) not in scandir_spy
        assert os.fspath(bundle) in scandir_spy

    def test_dataless_root_is_never_listed(self, bundle, mark_dataless, scandir_spy):
        mark_dataless(bundle)
        assert _icloud.walk_bundle_local(bundle) is ICLOUD.DATALESS
        assert scandir_spy == []

    def test_dataless_file(self, bundle, mark_dataless):
        mark_dataless(bundle / "META-INF" / "container.xml")
        assert _icloud.walk_bundle_local(bundle) is ICLOUD.DATALESS

    def test_listing_that_would_download_counts_as_dataless(self, bundle, monkeypatch):
        def scandir(path="."):
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")

        monkeypatch.setattr(os, "scandir", scandir)
        assert _icloud.walk_bundle_local(bundle) is ICLOUD.DATALESS

    def test_unlistable_folder_is_skipped(self, bundle, monkeypatch):
        real = os.scandir

        def scandir(path="."):
            if os.fspath(path).endswith("META-INF"):
                raise PermissionError(errno.EACCES, "Permission denied")
            return real(path)

        monkeypatch.setattr(os, "scandir", scandir)
        assert _icloud.walk_bundle_local(bundle) is ICLOUD.LOCAL

    def test_symlinks_are_not_followed(self, bundle, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / ".x.icloud").write_bytes(b"")
        (bundle / "OEBPS" / "linked").symlink_to(outside)
        assert _icloud.walk_bundle_local(bundle) is ICLOUD.LOCAL

    def test_single_file_root(self, tmp_path, mark_dataless):
        pdf = tmp_path / "Book.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" * 100)
        assert _icloud.walk_bundle_local(pdf) is ICLOUD.LOCAL
        mark_dataless(pdf)
        assert _icloud.walk_bundle_local(pdf) is ICLOUD.DATALESS


class TestLocalFileState:
    def test_states(self, tmp_path):
        pdf = tmp_path / "Book.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        assert _icloud.local_file_state(pdf) is ICLOUD.LOCAL
        assert _icloud.local_file_state(tmp_path / "Gone.pdf") is ICLOUD.MISSING
        (tmp_path / ".Stub.pdf.icloud").write_bytes(b"")
        assert _icloud.local_file_state(tmp_path / "Stub.pdf") is ICLOUD.ICLOUD_STUB
        assert _icloud.local_file_state(tmp_path) is ICLOUD.NOT_REGULAR
        (tmp_path / "link.pdf").symlink_to(pdf)
        assert _icloud.local_file_state(tmp_path / "link.pdf") is ICLOUD.NOT_REGULAR

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
    def test_fifo(self, tmp_path):
        os.mkfifo(tmp_path / "pipe.pdf")
        assert _icloud.local_file_state(tmp_path / "pipe.pdf") is ICLOUD.NOT_REGULAR

    def test_dataless_file_and_folder(self, tmp_path, mark_dataless):
        folder = tmp_path / "Books"
        folder.mkdir()
        pdf = folder / "Book.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        calls = mark_dataless(pdf)
        assert _icloud.local_file_state(pdf) is ICLOUD.DATALESS
        calls.clear()
        mark_dataless(folder)
        assert _icloud.local_file_state(pdf) is ICLOUD.DATALESS
        assert [c for c in calls if c[1] == os.fspath(pdf)] == []


# ---------------------------------------------------------------------------
# read_local / stat_local
# ---------------------------------------------------------------------------


CONTAINER = "META-INF/container.xml"


class TestReadLocal:
    def test_reads_the_file(self, bundle, fake_policy):
        expected = (bundle / CONTAINER).read_bytes()
        assert _icloud.read_local(bundle, CONTAINER, max_bytes=1 << 20) == expected
        assert ("set", _icloud.IOPOL_SCOPE_THREAD, 1) in fake_policy.calls
        chunks = []
        total = _icloud.read_local(bundle, CONTAINER, max_bytes=len(expected), sink=chunks.append)
        assert total == len(expected) and b"".join(chunks) == expected
        st = _icloud.stat_local(bundle, "./META-INF//container.xml")
        assert st.st_size == len(expected) and stat.S_ISREG(st.st_mode)

    def test_dataless_folder_is_never_looked_into(self, bundle, mark_dataless):
        calls = mark_dataless("META-INF")
        with pytest.raises(_icloud._NotLocal) as exc:
            _icloud.read_local(bundle, CONTAINER, max_bytes=1 << 20)
        assert exc.value.args == ()
        assert [c for c in calls if "container.xml" in c[1]] == []
        with pytest.raises(_icloud._NotLocal):
            _icloud.stat_local(bundle, CONTAINER)

    def test_dataless_file_is_never_opened(self, bundle, mark_dataless, monkeypatch):
        mark_dataless("container.xml")
        opened = []
        real_open = os.open

        def spy(path, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", spy)
        with pytest.raises(_icloud._NotLocal):
            _icloud.read_local(bundle, CONTAINER, max_bytes=1 << 20)
        assert "container.xml" not in opened

    def test_dataless_root(self, bundle, mark_dataless):
        mark_dataless(bundle)
        with pytest.raises(_icloud._NotLocal):
            _icloud.read_local(bundle, CONTAINER, max_bytes=1 << 20)

    def test_symlinks_are_refused(self, bundle, tmp_path):
        (tmp_path / "outside.xml").write_bytes(b"<x/>")
        (bundle / "META-INF" / "link.xml").symlink_to(tmp_path / "outside.xml")
        (bundle / "INFO").symlink_to(bundle / "META-INF")
        (bundle / "META-INF" / "inside.xml").symlink_to(bundle / CONTAINER)
        for rel in ("META-INF/link.xml", "INFO/container.xml", "META-INF/inside.xml"):
            with pytest.raises(_icloud._Unsafe):
                _icloud.read_local(bundle, rel, max_bytes=1 << 20)

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
    def test_fifo_returns_fast(self, bundle):
        os.mkfifo(bundle / "META-INF" / "pipe.xml")
        start = time.perf_counter()
        with pytest.raises(_icloud._Unsafe):
            _icloud.read_local(bundle, "META-INF/pipe.xml", max_bytes=1 << 20)
        assert time.perf_counter() - start < 0.05

    @pytest.mark.parametrize("rel", ["../x", "META-INF/../../x", "/etc/hosts", "a\0b", "", ".", "//"])
    def test_bad_names(self, bundle, rel):
        with pytest.raises(_icloud._Unsafe):
            _icloud.read_local(bundle, rel, max_bytes=1 << 20)
        with pytest.raises(_icloud._Unsafe):
            _icloud.stat_local(bundle, rel)

    def test_missing(self, bundle, tmp_path):
        for rel in ("META-INF/gone.xml", "GONE/container.xml", "mimetype/x"):
            with pytest.raises(_icloud._Missing):
                _icloud.read_local(bundle, rel, max_bytes=1 << 20)
        with pytest.raises(_icloud._Missing):
            _icloud.read_local(tmp_path / "nowhere", CONTAINER, max_bytes=1 << 20)

    def test_too_large_by_size(self, bundle):
        size = (bundle / CONTAINER).stat().st_size
        with pytest.raises(_icloud._TooLarge):
            _icloud.read_local(bundle, CONTAINER, max_bytes=size - 1)

    def test_file_growing_past_the_limit(self, bundle, monkeypatch):
        real = (bundle / CONTAINER).read_bytes()
        real_lstat = os.lstat

        def lstat(path, *, dir_fd=None):
            st = real_lstat(path, dir_fd=dir_fd)
            if os.fspath(path) == "container.xml":
                return fake_stat(st, st_size=4)  # stat'ed small; reads bigger
            return st

        monkeypatch.setattr(_icloud, "lstat", lstat)
        seen = []
        with pytest.raises(_icloud._TooLarge):
            _icloud.read_local(bundle, CONTAINER, max_bytes=8, sink=seen.append)
        assert sum(map(len, seen)) <= 8
        assert len(real) > 8

    def test_swapped_file_is_refused(self, bundle, monkeypatch):
        real_lstat = os.lstat

        def lstat(path, *, dir_fd=None):
            st = real_lstat(path, dir_fd=dir_fd)
            if os.fspath(path) == "container.xml":
                return fake_stat(st, st_ino=st.st_ino + 1)
            return st

        monkeypatch.setattr(_icloud, "lstat", lstat)
        with pytest.raises(_icloud._Unsafe):
            _icloud.read_local(bundle, CONTAINER, max_bytes=1 << 20)

    def test_read_that_would_download_is_not_local(self, bundle, monkeypatch):
        def read(fd, n):
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")

        with monkeypatch.context() as m:
            m.setattr(os, "read", read)
            with pytest.raises(_icloud._NotLocal):
                _icloud.read_local(bundle, CONTAINER, max_bytes=1 << 20)

    def test_unreadable_is_io_failed(self, bundle):
        if os.geteuid() == 0:
            pytest.skip("root reads anything")
        target = bundle / CONTAINER
        target.chmod(0)
        try:
            with pytest.raises(_icloud._IOFailed):
                _icloud.read_local(bundle, CONTAINER, max_bytes=1 << 20)
        finally:
            target.chmod(0o644)

    def test_no_fd_leaks(self, bundle):
        def open_fds():
            return len(os.listdir("/dev/fd"))

        before = open_fds()
        for rel in (CONTAINER, "META-INF/gone.xml", "GONE/x", "mimetype/x"):
            try:
                _icloud.read_local(bundle, rel, max_bytes=1 << 20)
            except _icloud._LocalReadError:
                pass
        assert open_fds() == before


# ---------------------------------------------------------------------------
# Cache registry
# ---------------------------------------------------------------------------


class TestFileCaches:
    @pytest.fixture(autouse=True)
    def _empty_registry(self, monkeypatch):
        monkeypatch.setattr(_icloud, "_cache_clearers", [])

    def test_register_and_clear(self):
        calls = []

        def clear():
            calls.append(1)

        _icloud.register_file_cache(clear)
        _icloud.register_file_cache(clear)
        _icloud.clear_file_caches()
        assert calls == [1]

    def test_every_clearer_runs_and_the_first_error_is_raised(self):
        calls = []

        def bad():
            calls.append("bad")
            raise RuntimeError("first")

        def worse():
            calls.append("worse")
            raise KeyError("second")

        def good():
            calls.append("good")

        for fn in (bad, worse, good):
            _icloud.register_file_cache(fn)
        with pytest.raises(RuntimeError, match="first"):
            _icloud.clear_file_caches()
        assert calls == ["bad", "worse", "good"]

    def test_concurrent_register_and_clear(self):
        counts = []
        barrier = threading.Barrier(8)

        def work(i):
            barrier.wait()
            for j in range(50):
                _icloud.register_file_cache(lambda i=i, j=j: counts.append((i, j)))
                _icloud.clear_file_caches()

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(_icloud._cache_clearers) == 400
        before = len(counts)
        _icloud.clear_file_caches()
        assert len(counts) - before == 400
