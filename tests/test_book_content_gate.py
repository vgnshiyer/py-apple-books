"""``PyAppleBooks.get_book_content``: the 1.11 iCloud gates (R2) and the
``Book`` argument.

Gate order: unknown id (bare ``IndexError``), unowned Store series row,
no path; then, new in 1.11, ``ZSTATE`` 3 (before any file access), the
book file or bundle being an iCloud placeholder or having a stub next to
it ("stored in iCloud", 1.10's message), anything inside the bundle being
a placeholder (the partial-download message), all before ``du``; then
1.10's ``is_downloaded`` and DRM checks.

Placeholders are faked by wrapping ``os.lstat``/``os.stat`` (what the
gates and ``Path.resolve`` call at run time).
"""

import errno
import os
import pathlib
import pickle
import subprocess
import threading
from types import SimpleNamespace

import pytest

from py_apple_books import PyAppleBooks, _icloud
from py_apple_books.content import BookContent
from py_apple_books.exceptions import (
    AppleBooksError,
    BookNotDownloadedError,
    DRMProtectedError,
    NotInLibraryError,
)
from py_apple_books.models import Book
from py_apple_books.testing import STORE_SERIES, write_epub
from tests import _fs_audit

STORED_IN_ICLOUD = ("{title} is stored in iCloud and has not been downloaded to this Mac. "
                    "Open it in Apple Books to trigger a download, then try again.")
NOT_DOWNLOADED = ("{title} has not been downloaded to this Mac. Open it in Apple Books "
                  "to download a local copy, then try again.")


def _dataless(st):
    fields = {name: getattr(st, name) for name in dir(st) if name.startswith("st_")}
    fields["st_flags"] = getattr(st, "st_flags", 0) | _icloud.SF_DATALESS
    return SimpleNamespace(**fields)


class _Files:
    """Records every stat of a path and fakes placeholders: an ``lstat``
    of a marked path, or a ``stat`` of anything that resolves to one."""

    def __init__(self):
        self.marked = set()
        self.stats = []
        self._quiet = threading.local()

    def quiet(self):
        """Neither record nor fake (for the fixture's own realpath calls)."""
        files = self

        class Quiet:
            def __enter__(self):
                files._quiet.on = True

            def __exit__(self, *exc):
                files._quiet.on = False

        return Quiet()

    def is_quiet(self) -> bool:
        return getattr(self._quiet, "on", False)

    def mark(self, *paths):
        """Make ``paths`` placeholders and start recording afresh."""
        with self.quiet():
            self.marked.update(os.path.realpath(p) for p in paths)
        self.stats.clear()

    def resolved(self, path, follow: bool) -> str:
        with self.quiet():
            if follow:
                return os.path.realpath(path)
            head, name = os.path.split(os.path.abspath(path))
            return os.path.join(os.path.realpath(head), name)

    def touched(self, root) -> list:
        stats = list(self.stats)
        prefix = self.resolved(root, True)
        stub = f".{os.path.basename(prefix)}.icloud"
        return [p for p in stats
                if p == prefix or p.startswith(prefix + os.sep) or os.path.basename(p) == stub]


@pytest.fixture
def files(monkeypatch):
    fake = _Files()
    real = {"lstat": os.lstat, "stat": os.stat}

    def wrap(name):
        def call(path, *args, dir_fd=None, **kwargs):
            st = real[name](path, *args, dir_fd=dir_fd, **kwargs)
            if fake.is_quiet() or dir_fd is not None or not isinstance(path, (str, os.PathLike)):
                return st
            where = fake.resolved(path, follow=name == "stat")
            fake.stats.append(where)
            return _dataless(st) if where in fake.marked else st
        return call

    monkeypatch.setattr(os, "lstat", wrap("lstat"))
    monkeypatch.setattr(os, "stat", wrap("stat"))
    return fake


@pytest.fixture
def du(monkeypatch):
    """Every ``du`` run (``subprocess.run`` in content)."""
    runs = []
    real = subprocess.run

    def run(args, *a, **k):
        runs.append(list(args))
        return real(args, *a, **k)

    monkeypatch.setattr("py_apple_books.content.subprocess.run", run)
    return runs


@pytest.fixture
def scandirs(monkeypatch):
    seen = []
    real = os.scandir

    def spy(path="."):
        seen.append(os.path.realpath(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", spy)
    return seen


@pytest.fixture
def bundle(tmp_path):
    return write_epub(tmp_path / "Synthetic Book.epub", "Synthetic Book")


@pytest.fixture
def book(library, bundle):
    return library.add_book("Synthetic Book", path=bundle)


def _raises(call, cls=BookNotDownloadedError) -> str:
    with pytest.raises(cls) as exc:
        call()
    assert type(exc.value) is cls
    return str(exc.value)


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


class TestGates:
    def test_local_book(self, api, book, bundle, du):
        content = api.get_book_content(book["id"])
        assert isinstance(content, BookContent)
        assert content.path == bundle and content.book_id == book["id"]
        assert content.list_chapters()
        assert len(du) == 1  # 1.10's is_downloaded still runs

    def test_cloud_only_state_touches_no_file(self, api, library, bundle, files, du, scandirs):
        row = library.add_book("In iCloud", path=bundle, state=_icloud_state())
        files.mark()
        with _fs_audit.record() as rec:
            message = _raises(lambda: api.get_book_content(row["id"]))
        assert message == STORED_IN_ICLOUD.format(title="'In iCloud'")
        assert files.touched(bundle) == []
        assert rec.under(bundle) == [] and rec.of(*_fs_audit.PROCESS_EVENTS) == []
        assert du == [] and scandirs == []

    def test_cloud_only_without_a_path_keeps_the_1_10_message(self, api, library):
        row = library.add_book("Never Here", path=None, state=_icloud_state())
        assert _raises(lambda: api.get_book_content(row["id"])) == NOT_DOWNLOADED.format(
            title="'Never Here'")

    def test_dataless_root_is_refused_before_du(self, api, book, bundle, files, du, scandirs):
        files.mark(bundle)
        message = _raises(lambda: api.get_book_content(book["id"]))
        assert message == STORED_IN_ICLOUD.format(title="'Synthetic Book'")
        assert du == [] and scandirs == []

    def test_icloud_stub_next_to_the_root(self, api, library, tmp_path, du):
        bundle = write_epub(tmp_path / "Stubbed.epub", "Stubbed")
        (tmp_path / ".Stubbed.epub.icloud").write_bytes(b"")
        row = library.add_book("Stubbed", path=bundle)
        assert _raises(lambda: api.get_book_content(row["id"])) == STORED_IN_ICLOUD.format(
            title="'Stubbed'")
        assert du == []

    def test_missing_bundle_keeps_the_1_10_message(self, api, library, tmp_path):
        row = library.add_book("Gone", path=tmp_path / "Gone.epub")
        assert _raises(lambda: api.get_book_content(row["id"])) == STORED_IN_ICLOUD.format(
            title="'Gone'")

    def test_dataless_file_deep_in_the_bundle(self, api, book, bundle, files, du):
        files.mark(bundle / "OEBPS" / "chap2.xhtml")
        message = _raises(lambda: api.get_book_content(book["id"]))
        assert message == _icloud.PARTIAL_DOWNLOAD_MESSAGE
        assert du == []

    def test_dataless_folder_is_never_listed(self, api, book, bundle, files, du, scandirs):
        oebps = os.path.realpath(bundle / "OEBPS")
        files.mark(oebps)
        assert _raises(lambda: api.get_book_content(book["id"])) == _icloud.PARTIAL_DOWNLOAD_MESSAGE
        assert oebps not in scandirs
        assert files.touched(oebps) == [oebps]  # its own lstat, nothing inside
        assert du == []

    def test_stub_inside_the_bundle(self, api, book, bundle, du):
        (bundle / "OEBPS" / ".chap1.xhtml.icloud").write_bytes(b"")
        assert _raises(lambda: api.get_book_content(book["id"])) == _icloud.PARTIAL_DOWNLOAD_MESSAGE
        assert du == []

    def test_gates_run_with_downloads_off(self, api, book, bundle, monkeypatch):
        values = {}

        def get(kind, scope):
            return values.get(threading.get_ident(), 0)

        def set_(kind, scope, value):
            values[threading.get_ident()] = value
            return 0

        monkeypatch.setattr(_icloud, "_policy_loaded", True)
        monkeypatch.setattr(_icloud, "_policy_fns", (get, set_))
        offs = []
        real = _icloud.lstat

        def lstat(path, **kwargs):
            if os.path.realpath(path).startswith(os.path.realpath(bundle)):
                offs.append(values.get(threading.get_ident(), 0))
            return real(path, **kwargs)

        monkeypatch.setattr(_icloud, "lstat", lstat)
        api.get_book_content(book["id"])
        assert offs and set(offs) == {_icloud.IOPOL_MATERIALIZE_DATALESS_FILES_OFF}
        assert values.get(threading.get_ident(), 0) == 0

    def test_lookup_that_would_download(self, api, book, monkeypatch, du):
        def deadlock(path, **kwargs):
            raise OSError(errno.EDEADLK, os.strerror(errno.EDEADLK))

        monkeypatch.setattr(_icloud, "lstat", deadlock)
        assert _raises(lambda: api.get_book_content(book["id"])) == STORED_IN_ICLOUD.format(
            title="'Synthetic Book'")
        assert du == []

    def test_unreadable_root_is_left_to_the_1_10_checks(self, api, book, monkeypatch):
        def denied(path, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(_icloud, "lstat", denied)
        assert api.get_book_content(book["id"]).list_chapters()

    def test_stat_results_without_flags(self, api, book, monkeypatch):
        class NoFlags:
            def __init__(self, st):
                self._st = st

            def __getattr__(self, name):
                if name == "st_flags":
                    raise AttributeError(name)
                return getattr(self._st, name)

        real_lstat, real_stat = _icloud.lstat, _icloud.stat
        monkeypatch.setattr(_icloud, "lstat", lambda p, **kw: NoFlags(real_lstat(p, **kw)))
        monkeypatch.setattr(_icloud, "stat", lambda p: NoFlags(real_stat(p)))
        assert api.get_book_content(book["id"]).list_chapters()

    def test_single_file_book(self, api, library, tmp_path, files, du):
        pdf = tmp_path / "Paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 5000)
        row = library.add_book("Paper", path=pdf)
        content = api.get_book_content(row["id"])
        assert content.is_pdf and du == []
        files.mark(pdf)
        assert _raises(lambda: api.get_book_content(row["id"])) == STORED_IN_ICLOUD.format(
            title="'Paper'")

    def test_symlinked_root_to_a_placeholder(self, api, library, bundle, tmp_path, files, du):
        # (1.10 already refused a symlinked bundle: du doesn't follow it.)
        link = tmp_path / "Linked.epub"
        link.symlink_to(bundle)
        row = library.add_book("Linked", path=link)
        files.mark(bundle)
        assert _raises(lambda: api.get_book_content(row["id"])) == STORED_IN_ICLOUD.format(
            title="'Linked'")
        assert du == []

    def test_drm_still_checked_after_the_gates(self, api, book, bundle):
        (bundle / "META-INF" / "sinf.xml").write_text("<x/>")
        assert "(FairPlay)" in _raises(lambda: api.get_book_content(book["id"]), DRMProtectedError)

    def test_not_in_library_comes_first(self, api, library):
        row = library.add_book("Store Volume", data_source=STORE_SERIES, can_redownload=0,
                               state=_icloud_state())
        _raises(lambda: api.get_book_content(row["id"]), NotInLibraryError)


def _icloud_state() -> int:
    from py_apple_books.models.book import STATE_CLOUD_ONLY

    return STATE_CLOUD_ONLY


# ---------------------------------------------------------------------------
# Titles in the messages
# ---------------------------------------------------------------------------


class TestTitles:
    @pytest.mark.parametrize("title", ["T", "Ender's Game", "x" * 80])
    def test_titles_up_to_80_characters_are_unchanged(self, api, library, tmp_path, title):
        row = library.add_book(title, path=None)
        assert _raises(lambda: api.get_book_content(row["id"])) == NOT_DOWNLOADED.format(
            title=f"'{title}'")
        row = library.add_book(title, path=tmp_path / "gone.epub")
        assert _raises(lambda: api.get_book_content(row["id"])) == STORED_IN_ICLOUD.format(
            title=f"'{title}'")

    def test_long_titles_are_shortened(self, api, library, bundle, tmp_path):
        title = "A" * 60 + "m" * 5000 + "Z" * 20
        short = "'" + "A" * 60 + "…" + "Z" * 20 + "'"
        rows = [
            (library.add_book(title, path=None), BookNotDownloadedError),
            (library.add_book(title, path=tmp_path / "gone.epub"), BookNotDownloadedError),
            (library.add_book(title, path=bundle, state=_icloud_state()), BookNotDownloadedError),
            (library.add_book(title, data_source=STORE_SERIES, can_redownload=0), NotInLibraryError),
        ]
        for row, cls in rows:
            message = _raises(lambda: api.get_book_content(row["id"]), cls)
            assert message.startswith(short) and len(message) < 300
        drm = write_epub(tmp_path / "Locked.epub", "Locked")
        (drm / "META-INF" / "sinf.xml").write_text("<x/>")
        row = library.add_book(title, path=drm)
        message = _raises(lambda: api.get_book_content(row["id"]), DRMProtectedError)
        assert message.startswith(short) and len(message) < 300


# ---------------------------------------------------------------------------
# A Book argument
# ---------------------------------------------------------------------------


class TestBookArgument:
    def test_book_from_this_library_runs_no_sql(self, api, book, sql_trace):
        found = api.get_book_by_id(book["id"])
        sql_trace.clear()
        content = api.get_book_content(found)
        assert sql_trace == []
        assert content.book_id == book["id"] and content.list_chapters()

    def test_book_without_path_is_read_again_once(self, api, book, bundle, sql_trace):
        partial = list(Book.manager.filter(id=book["id"], only=["id", "title"]))[0]
        assert partial.path is None
        sql_trace.clear()
        content = api.get_book_content(partial)
        assert len(sql_trace) == 1
        assert content.path == bundle

    def test_book_really_without_a_file(self, api, library, sql_trace):
        row = library.add_book("No File", path=None)
        found = api.get_book_by_id(row["id"])
        sql_trace.clear()
        assert _raises(lambda: api.get_book_content(found)) == NOT_DOWNLOADED.format(
            title="'No File'")
        assert len(sql_trace) == 1

    def test_cloud_only_book_touches_no_file(self, api, library, bundle, files, du):
        row = library.add_book("Cloud", path=bundle, state=_icloud_state())
        found = api.get_book_by_id(row["id"])
        files.mark()
        _raises(lambda: api.get_book_content(found))
        assert files.touched(bundle) == [] and du == []

    def test_book_from_another_library_is_resolved_here(self, api, library, bundle, make_library, tmp_path):
        other_lib = make_library()
        elsewhere = write_epub(tmp_path / "Elsewhere.epub", "Elsewhere")
        other_row = other_lib.add_book("Elsewhere", path=elsewhere)
        here = library.add_book("Here", path=bundle)
        assert other_row["id"] == here["id"]
        other_api = PyAppleBooks(data_dir=other_lib.data_dir)
        try:
            foreign = other_api.get_book_by_id(other_row["id"])
            assert foreign.path == os.fspath(elsewhere) or pathlib.Path(foreign.path) == elsewhere
            content = api.get_book_content(foreign)
            assert content.path == bundle and content.book_id == here["id"]
        finally:
            other_api.close()

    def test_book_unknown_here_is_a_bare_index_error(self, api, library, make_library, tmp_path):
        other_lib = make_library()
        other_row = other_lib.add_book("Only There", path=None)
        other_api = PyAppleBooks(data_dir=other_lib.data_dir)
        try:
            foreign = other_api.get_book_by_id(other_row["id"])
        finally:
            other_api.close()
        with pytest.raises(IndexError) as exc:
            api.get_book_content(foreign)
        assert type(exc.value) is IndexError and not isinstance(exc.value, AppleBooksError)

    def test_overridden_lookup_is_honoured(self, api, book, bundle):
        # An unpickled Book belongs to no library: it is looked up again,
        # through the instance's (here overridden) get_book_by_id.
        detached = pickle.loads(pickle.dumps(api.get_book_by_id(book["id"])))
        stub = SimpleNamespace(id=book["id"], title="Stub", path=os.fspath(bundle))
        looked_up = []
        api.get_book_by_id = lambda book_id: looked_up.append(book_id) or stub
        content = api.get_book_content(detached)
        assert looked_up == [book["id"]]
        assert content.book_id == book["id"] and content.path == bundle

    def test_ids_keep_working(self, api, book):
        assert api.get_book_content(book["id"]).book_id == book["id"]
        assert api.get_book_content(str(book["id"])).book_id == book["id"]
