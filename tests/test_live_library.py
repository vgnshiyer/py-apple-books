"""Opt-in smoke test against your own Apple Books library.

Skipped unless ``APPLE_BOOKS_LIVE_TESTS=1``. Runs every read method with
``limit=5`` in a subprocess that sees your real HOME (the rest of the
suite never does) and prints only counts and exception type names:
never titles, text, ids or paths. The library is opened read-only, and
book content is only read from a bundle directory Books marks
downloaded whose every directory and file, and every folder above it,
is on disk (tests/_live_gate.py checks with lstat first, never listing
or looking inside an evicted folder); single-file books such as PDFs
are skipped. The subprocess also turns off downloads of evicted files
for itself, so a read the gate missed fails instead of downloading; on
macOS, if it can't, it reads no book content and fails. The run never
triggers a download.

The gate's unit tests below always run, and so does the script itself
on a synthetic library.
"""

import os
import subprocess
import sys
import types

import pytest

from tests import _bootstrap, _live_gate
from tests._live_gate import SF_DATALESS, UF_COMPRESSED, skip_reason

live = pytest.mark.skipif(os.environ.get("APPLE_BOOKS_LIVE_TESTS") != "1",
                          reason="set APPLE_BOOKS_LIVE_TESTS=1 to run against your own library")

_SCRIPT = r'''
from tests._live_gate import disable_materialization, skip_reason

policy_on = disable_materialization()  # before anything can read a book file
print(f"download of evicted files turned off: {policy_on}")

import collections
import datetime as dt
import sys

from py_apple_books import PyAppleBooks
from py_apple_books.exceptions import BookNotDownloadedError, DRMProtectedError

api = PyAppleBooks()
failures = []


def count(result):
    if result is None:
        return 0
    if isinstance(result, str):
        return len(result)
    if hasattr(result, "list_chapters"):
        return len(result.list_chapters())
    return len(list(result)) if hasattr(result, "__iter__") else 1


def run(label, call):
    try:
        print(f"{label}: {count(call())}")
    except (IndexError, BookNotDownloadedError, DRMProtectedError) as e:
        print(f"{label}: {type(e).__name__}")
    except Exception as e:  # the type name only: messages can quote titles
        print(f"{label}: UNEXPECTED {type(e).__name__}")
        failures.append(label)


run("list_collections", lambda: api.list_collections(limit=5))
run("get_collection_by_title", lambda: api.get_collection_by_title("a"))
run("list_books", lambda: api.list_books(limit=5))
run("get_book_by_title", lambda: api.get_book_by_title("a"))
run("get_books_by_genre", lambda: api.get_books_by_genre("a", limit=5))
run("list_annotations", lambda: api.list_annotations(limit=5, order_by="-creation_date"))
run("get_annotations_by_color", lambda: api.get_annotations_by_color("yellow", limit=5))
run("search_annotation_by_highlighted_text", lambda: api.search_annotation_by_highlighted_text("the", limit=5))
run("search_annotation_by_note", lambda: api.search_annotation_by_note("the", limit=5))
run("search_annotation_by_text", lambda: api.search_annotation_by_text("the", limit=5))
run("get_annotations_by_date_range", lambda: api.get_annotations_by_date_range(after=dt.datetime(2000, 1, 1), limit=5))
run("get_books_in_progress", lambda: api.get_books_in_progress(limit=5))
run("get_books_in_progress[resource]", lambda: api.get_books_in_progress(limit=1, order_by="-last_opened_date"))
run("get_finished_books", lambda: api.get_finished_books(limit=5))
run("get_unstarted_books", lambda: api.get_unstarted_books(limit=5))
recent = list(api.get_recently_read_books(limit=5))
print(f"get_recently_read_books: {len(recent)}")
for collection in list(api.list_collections(limit=5))[:1]:
    run("get_collection_by_id", lambda: api.get_collection_by_id(collection.id).books)
for annotation in list(api.list_annotations(limit=1)):
    run("get_annotation_by_id", lambda: [api.get_annotation_by_id(annotation.id).book])
for book in recent[:1]:
    run("get_book_by_id", lambda: api.get_book_by_id(book.id).annotations)
    run("get_book_by_id.collections", lambda: api.get_book_by_id(book.id).collections)
reasons = collections.Counter(skip_reason(b.path, b.state) for b in recent)
local = [b for b in recent if skip_reason(b.path, b.state) is None][:1]
print(f"fully local recent books: {reasons.pop(None, 0)}; skipped: {dict(sorted(reasons.items()))}")
if sys.platform == "darwin" and not policy_on:
    # No backstop for a read the gate missed: read no book file at all.
    print("book content: not read (downloads could not be turned off)")
    failures.append("download policy")
    local = []
for book in local:
    run("get_book_content", lambda: api.get_book_content(book.id))
    run("get_current_reading_location", lambda: [api.get_current_reading_location(book.id)])
    run("get_current_reading_chapter", lambda: [api.get_current_reading_chapter(book.id)])
    for annotation in list(book.annotations)[:1]:
        run("get_annotation_surrounding_text",
            lambda: api.get_annotation_surrounding_text(annotation.id, chars_before=50, chars_after=50))
print(f"unexpected errors: {len(failures)}")
raise SystemExit(1 if failures else 0)
'''


def _run_script(home, *, settings=None, prelude="", timeout=600):
    """Run ``_SCRIPT`` (after ``prelude``) in a subprocess whose HOME is
    ``home``, with no APPLE_BOOKS_* settings but ``settings``."""
    import py_apple_books

    tree = os.path.dirname(os.path.dirname(os.path.abspath(py_apple_books.__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
    env.update(settings or {})
    env.update(HOME=str(home), PYTHONPATH=os.pathsep.join(filter(None, [tree, env.get("PYTHONPATH")])))
    return subprocess.run([sys.executable, "-c", prelude + _SCRIPT], env=env, cwd=tree,
                          capture_output=True, text=True, timeout=timeout)


@live
def test_every_read_method_on_the_real_library():
    # The developer's own APPLE_BOOKS_* settings, popped for the suite.
    proc = _run_script(_bootstrap.REAL_HOME or "", settings=_bootstrap.POPPED_ENV)
    print(proc.stdout)
    # Only the exception type from stderr: a message could quote a title.
    last = (proc.stderr.strip().splitlines() or [""])[-1].split(":")[0]
    assert proc.returncode == 0, f"live run failed: {last or 'see the counts above'}"


# Prepended to _SCRIPT by the synthetic runs: for each watched book
# ({name: path}), counts the files opened at or under its path (the
# gate's own lstat/scandir walk opens nothing) and prints "opened NAME: N"
# at exit.
_WATCH = r'''
import atexit as _atexit, collections as _collections, os as _os, sys as _sys
_WATCHED = {watched!r}
_opened = _collections.Counter()


def _watch_hook(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        path = _os.path.abspath(_os.fsdecode(args[0]))
        for name, w in _WATCHED.items():
            if path == w or path.startswith(w + _os.sep):
                _opened[name] += 1


_sys.addaudithook(_watch_hook)


def _report():
    for name in sorted(_WATCHED):
        print(f"opened {{name}}: {{_opened[name]}}")


_atexit.register(_report)
'''


def test_live_script_on_a_synthetic_library(tmp_path, epub_factory):
    """The whole live script, on a synthetic HOME: single-file books (a
    PDF, a packed .epub) are skipped and never opened, the newest bundle
    is read, and nothing is unexpected."""
    import datetime as dt

    from py_apple_books.testing import FixtureLibrary

    from tests import conftest

    lib = FixtureLibrary.create(tmp_path / "home", conftest.FIXTURE_SCHEMA)
    pdf = tmp_path / "books" / "Synthetic.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
    packed = tmp_path / "books" / "Packed.epub"
    packed.write_bytes(b"PK\x05\x06" + bytes(18))  # an empty zip archive
    bundle = epub_factory().path
    opened = dt.datetime(2026, 1, 2)
    lib.add_book("A PDF", path=pdf, content_type=3, last_opened=opened)
    lib.add_book("A packed EPUB", path=packed, last_opened=opened - dt.timedelta(days=1))
    lib.add_book("A bundle", path=bundle, last_opened=opened - dt.timedelta(days=2))
    lib.add_book("In iCloud", path=tmp_path / "books" / "Gone.epub", state=3,
                 last_opened=opened - dt.timedelta(days=3))

    watched = {"pdf": str(pdf), "packed": str(packed), "bundle": str(bundle)}
    proc = _run_script(lib.root, prelude=_WATCH.format(watched=watched), timeout=120)
    out = proc.stdout.splitlines()
    assert proc.returncode == 0, proc.stdout + proc.stderr[-3000:]
    assert "fully local recent books: 1; skipped: {'not a bundle': 2, 'state': 1}" in out
    assert [line for line in out if line.startswith("get_book_content:")] == ["get_book_content: 3"]
    assert "opened pdf: 0" in out and "opened packed: 0" in out
    assert "opened bundle: 0" not in out  # the watch does see the bundle being read
    assert "unexpected errors: 0" in out
    assert not [line for line in out if "UNEXPECTED" in line]


@pytest.mark.skipif(sys.platform != "darwin", reason="the download policy exists on macOS only")
def test_live_script_reads_no_book_without_the_download_policy(tmp_path, epub_factory):
    """If macOS refuses to turn downloads off, the script opens no book
    file (there would be no backstop) and fails."""
    from py_apple_books.testing import FixtureLibrary

    from tests import conftest

    lib = FixtureLibrary.create(tmp_path / "home", conftest.FIXTURE_SCHEMA)
    bundle = epub_factory().path
    lib.add_book("A bundle", path=bundle, last_opened=1.0)
    prelude = ("import tests._live_gate as _gate\n"
               "_gate.disable_materialization = lambda: False\n"
               + _WATCH.format(watched={"bundle": str(bundle)}))
    proc = _run_script(lib.root, prelude=prelude, timeout=120)
    out = proc.stdout.splitlines()
    assert proc.returncode == 1, proc.stdout + proc.stderr[-3000:]
    assert "download of evicted files turned off: False" in out
    assert "fully local recent books: 1; skipped: {}" in out
    assert "book content: not read (downloads could not be turned off)" in out
    assert not [line for line in out if line.startswith("get_book_content")]
    assert "opened bundle: 0" in out


# -- the gate, on synthetic bundles with a patched os.lstat ------------------

def _bundle(root):
    book = root / "Book.epub"
    (book / "META-INF").mkdir(parents=True)
    (book / "OEBPS" / "Text").mkdir(parents=True)
    (book / "mimetype").write_text("application/epub+zip")
    (book / "META-INF" / "container.xml").write_text("<container/>")
    (book / "OEBPS" / "content.opf").write_text("<package/>")
    (book / "OEBPS" / "Text" / "ch1.xhtml").write_text("<html/>")
    return book


@pytest.fixture
def evict(monkeypatch):
    """``evict(path, how)`` makes os.lstat report ``path`` evicted; also
    records every os.scandir call in ``evict.listed`` and every os.lstat
    call in ``evict.stated``."""
    marked = {}
    real_lstat, real_scandir = os.lstat, os.scandir

    def fake_lstat(path, *args, **kwargs):
        fake.stated.append(os.fspath(path))
        st = real_lstat(path, *args, **kwargs)
        how = marked.get(os.fspath(path))
        if how is None:
            return st
        flags = getattr(st, "st_flags", 0)
        size, blocks = st.st_size, getattr(st, "st_blocks", 0)
        if how == "flag":
            flags |= SF_DATALESS
        elif how == "no blocks":
            size, blocks = max(size, 1), 0
        elif how == "compressed":
            size, blocks, flags = max(size, 1), 0, flags | UF_COMPRESSED
        return types.SimpleNamespace(st_mode=st.st_mode, st_size=size, st_blocks=blocks, st_flags=flags)

    def fake_scandir(path):
        fake.listed.append(os.fspath(path))
        return real_scandir(path)

    def fake(path, how="flag"):
        marked[os.fspath(path)] = how

    fake.listed = []
    fake.stated = []
    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(os, "scandir", fake_scandir)
    return fake


def test_gate_accepts_a_wholly_local_bundle(tmp_path, evict):
    book = _bundle(tmp_path)
    assert skip_reason(book, 1) is None
    assert sorted(evict.listed) == sorted(str(p) for p in (
        book, book / "META-INF", book / "OEBPS", book / "OEBPS" / "Text"))
    assert skip_reason(str(book), 1) is None


def test_gate_skips_by_state_without_touching_the_files(tmp_path, evict):
    book = _bundle(tmp_path)
    for state in (3, 0, None, 5):
        assert skip_reason(book, state) == "state"
    assert skip_reason(None, 1) == "no path"
    assert skip_reason(tmp_path / "gone.epub", 1) == "missing"
    assert evict.listed == []


def test_gate_never_lists_an_evicted_directory(tmp_path, evict):
    book = _bundle(tmp_path)
    evict(book)
    assert skip_reason(book, 1) == "dataless"
    assert evict.listed == []
    evict.listed.clear()
    other = _bundle(tmp_path / "second")
    evict(other / "OEBPS")
    assert skip_reason(other, 1) == "dataless"
    assert str(other / "OEBPS") not in evict.listed
    assert not any(p.startswith(str(other / "OEBPS") + os.sep) for p in evict.listed)


def test_gate_skips_an_evicted_file_anywhere(tmp_path, evict):
    book = _bundle(tmp_path)
    evict(book / "OEBPS" / "Text" / "ch1.xhtml")
    assert skip_reason(book, 1) == "dataless"
    book2 = _bundle(tmp_path / "b2")
    evict(book2 / "META-INF" / "container.xml", "no blocks")  # no flag, but no data either
    assert skip_reason(book2, 1) == "dataless"
    book3 = _bundle(tmp_path / "b3")
    evict(book3 / "mimetype", "compressed")  # APFS-compressed: local
    assert skip_reason(book3, 1) is None


def test_gate_skips_stubs_links_and_special_files(tmp_path, evict):
    book = _bundle(tmp_path)
    (book / "OEBPS" / ".ch2.xhtml.icloud").write_text("")
    assert skip_reason(book, 1) == "icloud stub"
    linked = _bundle(tmp_path / "linked")
    (linked / "OEBPS" / "outside").symlink_to(tmp_path)
    assert skip_reason(linked, 1) == "symlink"
    stubbed = _bundle(tmp_path / "stubbed")
    (stubbed.parent / f".{stubbed.name}.icloud").write_text("")
    assert skip_reason(stubbed, 1) == "icloud stub"
    fifo = tmp_path / "fifo.epub"
    os.mkfifo(fifo)
    assert skip_reason(fifo, 1) == "special file"


def test_gate_skips_single_file_books(tmp_path, evict):
    """Only bundle directories are opened: a PDF or a packed .epub is
    one file, skipped whether or not it is on disk."""
    pdf = tmp_path / "Book.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    packed = tmp_path / "Packed.epub"
    packed.write_bytes(b"PK\x05\x06" + bytes(18))
    assert skip_reason(pdf, 1) == "not a bundle"
    assert skip_reason(packed, 1) == "not a bundle"
    assert evict.listed == []
    evict(pdf)
    assert skip_reason(pdf, 1) == "dataless"


def test_gate_checks_every_folder_above_the_bundle(tmp_path, evict):
    """An evicted parent folder is caught by its own lstat: nothing
    inside it is looked up or listed."""
    parent = tmp_path / "iCloud" / "Books"
    book = _bundle(parent)
    assert skip_reason(book, 1) is None
    evict(tmp_path / "iCloud")
    evict.listed.clear()
    evict.stated.clear()
    assert skip_reason(book, 1) == "dataless"
    inside = str(tmp_path / "iCloud") + os.sep
    assert not [p for p in evict.stated + evict.listed if p.startswith(inside)]
    assert skip_reason(book / "OEBPS", 1) == "dataless"


def test_gate_finds_a_stub_or_an_evicted_folder_through_a_link(tmp_path, evict):
    real = tmp_path / "real"
    book = _bundle(real)
    (tmp_path / "link").symlink_to(real)
    via_link = tmp_path / "link" / book.name
    assert skip_reason(via_link, 1) is None
    (tmp_path / ".real.icloud").write_text("")
    assert skip_reason(via_link, 1) == "icloud stub"
    (tmp_path / ".real.icloud").unlink()
    evict(real)
    assert skip_reason(via_link, 1) == "dataless"
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    assert skip_reason(tmp_path / "loop" / "Book.epub", 1) == "symlink"


def test_gate_without_st_flags(tmp_path, monkeypatch):
    """Linux's stat has no st_flags: only the size/blocks rule applies."""
    real = os.lstat

    def linux_lstat(path, *args, **kwargs):
        st = real(path, *args, **kwargs)
        return types.SimpleNamespace(st_mode=st.st_mode, st_size=st.st_size, st_blocks=st.st_blocks)

    monkeypatch.setattr(os, "lstat", linux_lstat)
    assert skip_reason(_bundle(tmp_path), 1) is None


def test_live_script_uses_the_gate():
    assert "skip_reason(b.path, b.state)" in _SCRIPT
    assert _SCRIPT.index("disable_materialization()") < _SCRIPT.index("from py_apple_books")
    assert callable(_live_gate.disable_materialization)


def test_download_policy_turns_off_in_a_subprocess():
    """The live subprocess's backstop: macOS accepts the process-wide
    policy (and reports it back); elsewhere it is a no-op."""
    tree = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("from tests._live_gate import disable_materialization as off\n"
            "import subprocess, sys\n"
            "print(off())\n")
    proc = subprocess.run([sys.executable, "-c", code], cwd=tree, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.split() == [str(sys.platform == "darwin")]
