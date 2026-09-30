"""Smoke test: every public read method runs against every schema fixture.

Each method is called on an empty store and on the demo library
(``seed_demo``), and every returned model has its relations traversed,
so every read statement the library can issue is executed at least
once. The only exceptions allowed are the documented outcomes: not
found (an ``IndexError``) and the content errors for books without a
readable file. A schema that lacks a mapped column fails here with
'no such column'.

The session library covers the default schema in-process. Other schema
fixtures run in a subprocess with HOME pointing at a ``make_library``
root, because py_apple_books <= 1.9 binds its connections at import.
"""

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

import pytest

from py_apple_books.testing import available_schemas, seed_demo

# (label, call) pairs; ``ids`` holds the seeded ids, or ids of rows that
# don't exist when the store is empty.
READ_CALLS = [
    ("list_collections", lambda api, ids: api.list_collections(limit=None)),
    ("list_collections[limit]", lambda api, ids: api.list_collections(limit=2, order_by="title")),
    ("get_collection_by_id", lambda api, ids: api.get_collection_by_id(str(ids["collection"]))),
    ("get_collection_by_title", lambda api, ids: api.get_collection_by_title("Shelf")),
    ("list_books", lambda api, ids: api.list_books(limit=None)),
    ("list_books[order]", lambda api, ids: api.list_books(limit=3, order_by="-creation_date")),
    ("get_book_by_id", lambda api, ids: api.get_book_by_id(str(ids["book"]))),
    ("get_book_by_title", lambda api, ids: api.get_book_by_title("Book")),
    ("get_books_by_genre", lambda api, ids: api.get_books_by_genre("Fiction", limit=None)),
    ("list_annotations", lambda api, ids: api.list_annotations()),
    ("list_annotations[recent]", lambda api, ids: api.list_annotations(limit=10, order_by="-creation_date")),
    ("get_annotation_by_id", lambda api, ids: api.get_annotation_by_id(str(ids["annotation"]))),
    ("get_annotations_by_color", lambda api, ids: api.get_annotations_by_color("yellow", limit=None)),
    ("search_annotation_by_highlighted_text",
     lambda api, ids: api.search_annotation_by_highlighted_text("synthetic")),
    ("search_annotation_by_note", lambda api, ids: api.search_annotation_by_note("note", limit=None)),
    ("search_annotation_by_text", lambda api, ids: api.search_annotation_by_text("synthetic", limit=5)),
    ("get_annotations_by_date_range", lambda api, ids: api.get_annotations_by_date_range(
        after=dt.datetime(2026, 9, 1), before=dt.datetime(2026, 9, 30), limit=None)),
    ("get_books_in_progress", lambda api, ids: api.get_books_in_progress(limit=None)),
    ("get_books_in_progress[resource]",
     lambda api, ids: api.get_books_in_progress(limit=1, order_by="-last_opened_date")),
    ("get_finished_books", lambda api, ids: api.get_finished_books(limit=None)),
    ("get_unstarted_books", lambda api, ids: api.get_unstarted_books(limit=None)),
    ("get_recently_read_books", lambda api, ids: api.get_recently_read_books(limit=10)),
    ("get_book_content", lambda api, ids: api.get_book_content(ids["book"])),
    ("get_book_content[no_file]", lambda api, ids: api.get_book_content(ids["no_file"])),
    ("get_book_content[drm]", lambda api, ids: api.get_book_content(ids["drm"])),
    ("get_current_reading_location", lambda api, ids: api.get_current_reading_location(ids["book"])),
    ("get_current_reading_chapter", lambda api, ids: api.get_current_reading_chapter(ids["book"])),
    ("get_annotation_surrounding_text", lambda api, ids: api.get_annotation_surrounding_text(
        ids["annotation"], chars_before=100, chars_after=100)),
]

READ_METHODS_191 = {
    "list_collections", "get_collection_by_id", "get_collection_by_title", "list_books",
    "get_book_by_id", "get_book_by_title", "get_books_by_genre", "list_annotations",
    "get_annotation_by_id", "get_annotations_by_color", "search_annotation_by_highlighted_text",
    "search_annotation_by_note", "search_annotation_by_text", "get_annotations_by_date_range",
    "get_books_in_progress", "get_finished_books", "get_unstarted_books", "get_recently_read_books",
    "get_book_content", "get_current_reading_location", "get_current_reading_chapter",
    "get_annotation_surrounding_text",
}

# Rows that don't exist: every id-taking call is a not-found.
MISSING_IDS = {"book": 999999, "no_file": 999999, "drm": 999999, "collection": 999999, "annotation": 999999}
# On the demo library these calls end in the documented content errors.
SEEDED_ERRORS = {"get_book_content[no_file]": "not_downloaded", "get_book_content[drm]": "drm"}


def _touch(result) -> int:
    """Materialize ``result`` and traverse each model's relations once;
    return a size."""
    if result is None:
        return 0
    if isinstance(result, str):
        return len(result)
    if hasattr(result, "list_chapters"):  # BookContent
        return len(result.list_chapters())
    if not hasattr(result, "__iter__"):
        result = [result]
    rows = list(result)
    for row in rows:
        for relation in ("annotations", "collections", "books"):
            if hasattr(row, relation):
                list(getattr(row, relation))
        if hasattr(row, "asset_id") and hasattr(row, "book"):  # Annotation
            getattr(row, "book")
    return len(rows)


def smoke(api, ids) -> dict:
    """Run READ_CALLS; return ``{label: outcome}`` where an outcome is
    ``'ok'``, ``'not_found'``, ``'not_downloaded'``, ``'drm'`` or
    ``'unexpected: <exception>'``."""
    from py_apple_books.exceptions import BookNotDownloadedError, DRMProtectedError

    outcomes = {}
    for label, call in READ_CALLS:
        try:
            _touch(call(api, ids))
            outcomes[label] = "ok"
        except BookNotDownloadedError:
            outcomes[label] = "not_downloaded"
        except DRMProtectedError:
            outcomes[label] = "drm"
        except IndexError:
            outcomes[label] = "not_found"
        except Exception as e:
            outcomes[label] = f"unexpected: {type(e).__name__}: {e}"
    return outcomes


def demo_ids(demo) -> dict:
    return {
        "book": demo["books"]["synthetic"]["id"],
        "no_file": demo["books"]["finished"]["id"],
        "drm": demo["books"]["drm"]["id"],
        "collection": demo["collections"]["shelf"]["id"],
        "annotation": demo["annotations"]["highlight"],
    }


def check(empty: dict, seeded: dict) -> None:
    for outcomes in (empty, seeded):
        assert set(outcomes) == {label for label, _ in READ_CALLS}
        assert not any("no such column" in o or "no such table" in o for o in outcomes.values()), outcomes
    assert {k: v for k, v in empty.items() if v not in ("ok", "not_found")} == {}
    assert {k: v for k, v in seeded.items() if v != "ok"} == SEEDED_ERRORS


def test_calls_cover_every_191_read_method():
    from py_apple_books import PyAppleBooks

    called = {label.split("[")[0] for label, _ in READ_CALLS}
    assert called == READ_METHODS_191
    assert all(callable(getattr(PyAppleBooks, name)) for name in called)


def test_default_schema_in_process(api, library, tmp_path):
    empty = smoke(api, MISSING_IDS)
    seeded = smoke(api, demo_ids(seed_demo(library, tmp_path)))
    check(empty, seeded)


_SUBPROCESS = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("_read_smoke", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from py_apple_books import PyAppleBooks
print(json.dumps(module.smoke(PyAppleBooks(), json.loads(sys.argv[2]))))
"""


def _smoke_in_subprocess(lib, ids) -> dict:
    import py_apple_books

    tree = pathlib.Path(py_apple_books.__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
    env.update(HOME=str(lib.root), APPLE_BOOKS_DATA_DIR=str(lib.data_dir), TZ="UTC",
               PYTHONPATH=os.pathsep.join(filter(None, [str(tree), env.get("PYTHONPATH")])))
    proc = subprocess.run([sys.executable, "-c", _SUBPROCESS, __file__, json.dumps(ids)],
                          env=env, cwd=lib.root, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("schema", available_schemas())
def test_every_schema_in_subprocess(make_library, schema):
    lib = make_library(schema)
    empty = _smoke_in_subprocess(lib, MISSING_IDS)
    seeded = _smoke_in_subprocess(lib, demo_ids(seed_demo(lib, lib.root)))
    check(empty, seeded)
