"""Opt-in smoke test against your own Apple Books library.

Skipped unless ``APPLE_BOOKS_LIVE_TESTS=1``. Runs every read method with
``limit=5`` in a subprocess that sees your real HOME (the rest of the
suite never does) and prints only counts and exception type names:
never titles, text, ids or paths. The library is opened read-only, and
book content is only read from a book Books marks downloaded whose
every directory and file is on disk (tests/_live_gate.py walks it with
lstat first, never listing an evicted directory). The subprocess also
turns off downloads of evicted files for itself, so a read the gate
missed fails instead of downloading. The run never triggers a download.

The gate's unit tests below always run.
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


@live
def test_every_read_method_on_the_real_library():
    import py_apple_books

    tree = os.path.dirname(os.path.dirname(os.path.abspath(py_apple_books.__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
    env.update(_bootstrap.POPPED_ENV)  # the developer's own APPLE_BOOKS_* settings
    env.update(HOME=_bootstrap.REAL_HOME or "", PYTHONPATH=os.pathsep.join(filter(None, [tree, env.get("PYTHONPATH")])))
    proc = subprocess.run([sys.executable, "-c", _SCRIPT], env=env, cwd=tree,
                          capture_output=True, text=True, timeout=600)
    print(proc.stdout)
    # Only the exception type from stderr: a message could quote a title.
    last = (proc.stderr.strip().splitlines() or [""])[-1].split(":")[0]
    assert proc.returncode == 0, f"live run failed: {last or 'see the counts above'}"


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
    records every os.scandir call in ``evict.listed``."""
    marked = {}
    real_lstat, real_scandir = os.lstat, os.scandir

    def fake_lstat(path, *args, **kwargs):
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


def test_gate_skips_stubs_links_and_single_evicted_files(tmp_path, evict):
    book = _bundle(tmp_path)
    (book / "OEBPS" / ".ch2.xhtml.icloud").write_text("")
    assert skip_reason(book, 1) == "icloud stub"
    linked = _bundle(tmp_path / "linked")
    (linked / "OEBPS" / "outside").symlink_to(tmp_path)
    assert skip_reason(linked, 1) == "symlink"
    stubbed = _bundle(tmp_path / "stubbed")
    (stubbed.parent / f".{stubbed.name}.icloud").write_text("")
    assert skip_reason(stubbed, 1) == "icloud stub"
    pdf = tmp_path / "Book.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    assert skip_reason(pdf, 1) is None
    evict(pdf)
    assert skip_reason(pdf, 1) == "dataless"


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
