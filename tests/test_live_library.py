"""Opt-in smoke test against your own Apple Books library.

Skipped unless ``APPLE_BOOKS_LIVE_TESTS=1``. Runs every read method with
``limit=5`` in a subprocess that sees your real HOME (the rest of the
suite never does) and prints only counts and exception type names:
never titles, text, ids or paths. The library is opened read-only, and
book content is only read from a bundle that is fully on disk (not an
iCloud placeholder), so the run never triggers a download.
"""

import os
import subprocess
import sys

import pytest

from tests import _bootstrap

pytestmark = pytest.mark.skipif(os.environ.get("APPLE_BOOKS_LIVE_TESTS") != "1",
                                reason="set APPLE_BOOKS_LIVE_TESTS=1 to run against your own library")

_SCRIPT = r'''
import datetime as dt
import os
import pathlib

from py_apple_books import PyAppleBooks
from py_apple_books.exceptions import BookNotDownloadedError, DRMProtectedError

SF_DATALESS = 0x40000000  # set on files whose data is only in iCloud
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


def fully_local(path):
    p = pathlib.Path(path or "")
    try:
        return p.is_dir() and not any(os.lstat(f).st_flags & SF_DATALESS
                                      for f in (p, p / "META-INF" / "container.xml"))
    except (OSError, AttributeError):
        return False


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
local = [b for b in recent if fully_local(b.path)][:1]
print(f"fully local recent books: {len(local)}")
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
