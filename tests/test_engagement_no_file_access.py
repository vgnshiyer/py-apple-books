"""The engagement methods never touch a book file, iCloud Drive or another
process (stream 2.5).

A fresh interpreter installs the suite's audit hook (``tests/_fs_audit.py``)
before importing py_apple_books, builds a synthetic library whose book
lives under the fixture's ``Library/Mobile Documents`` (a real EPUB
bundle), and runs every engagement method under ``block()``: reading
the bundle, anything in iCloud Drive, any other container file (but
Books' preferences plist) or starting a process raises
``PermissionError`` and is recorded. Expected: nothing refused, no
error. The control, ``get_book_content`` on that book, must be refused,
which shows the hook is armed.

Stat-family calls raise no audit event, yet a lookup inside an evicted
iCloud folder can download that folder's listing, so the script also
wraps ``os.stat``, ``os.lstat``, ``os.access`` (``os.path.exists`` and
friends go through them), pathlib's accessor on 3.10 and the
``_icloud`` stat patch points, and records every path they see while
the methods run: none may be in the bundle or under ``Library/Mobile
Documents``. The control must stat the bundle, which shows the wrappers
are armed.
"""

import json
import os
import pathlib
import subprocess
import sys
import textwrap

from py_apple_books._api.engagement import _EngagementAPI

TESTS = pathlib.Path(__file__).resolve().parent

# The methods the script calls, by name; every public method of the
# engagement mixin must be one of them.
_SCRIPT = textwrap.dedent(r'''
    import datetime as dt, importlib.util, json, pathlib, sys, traceback

    spec = importlib.util.spec_from_file_location("_fs_audit", sys.argv[1])
    audit = sys.modules["_fs_audit"] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    audit.install()
    assert "py_apple_books" not in sys.modules

    import os, pathlib as _pathlib

    # Stat-family calls (no audit event): record their paths while
    # stat_seen is set. Installed before py_apple_books is imported, so
    # a module-level alias would be wrapped too.
    stat_seen = None

    def recording(original):
        def wrapper(path, *args, **kwargs):
            if stat_seen is not None and not isinstance(path, int):
                name = os.fsdecode(os.fspath(path))
                if kwargs.get("dir_fd") is not None:
                    name = "<dir_fd>/" + name   # can't be resolved: never expected here
                stat_seen.append(os.path.abspath(name) if not name.startswith("<") else name)
            return original(path, *args, **kwargs)
        return wrapper

    for _name in ("stat", "lstat", "access"):
        setattr(os, _name, recording(getattr(os, _name)))
    _accessor = getattr(_pathlib, "_NormalAccessor", None)   # 3.10: bound at import
    if _accessor is not None:
        for _name in ("stat", "lstat"):
            if hasattr(_accessor, _name):
                setattr(_accessor, _name, staticmethod(getattr(os, _name)))

    from py_apple_books import PyAppleBooks
    from py_apple_books import _icloud
    from py_apple_books.testing import FixtureLibrary, write_epub_bundle

    for _name in ("stat", "lstat"):
        setattr(_icloud, _name, recording(getattr(_icloud, _name)))

    home = pathlib.Path(sys.argv[2])
    UTC = dt.timezone.utc

    def noon(*ymd):
        # Local noon, so each row's local day is the same in every zone.
        return dt.datetime(*ymd, 12).astimezone()

    lib = FixtureLibrary.create(home)
    bundle = write_epub_bundle(
        home / "Library" / "Mobile Documents" / "iCloud~com~apple~iBooks" / "Documents" / "Cloud Book.epub",
        [("ch1", "<p>A first chapter.</p>")])
    book = lib.add_book("Cloud Book", path=bundle, finished=True,
                        finished_date=dt.datetime(2026, 3, 1, tzinfo=UTC))
    for i in range(4):
        lib.add_annotation(book, f"a passage long enough to be sampled, number {i}.",
                           note="a note" if i % 2 else None, created=noon(2025, 10, 1))
    lib.add_annotation(book, "Ephemeral,", kind="underline", created=noon(2024, 10, 1))
    lib.add_annotation(book, "laconic", kind="note", note="brief", created=noon(2026, 9, 1))
    lib.add_annotation("GONE-ASSET", "an orphan passage long enough to count.",
                       created=noon(2023, 10, 1))
    lib.write_prefs(finished={book["asset_id"]: dt.datetime(2026, 3, 1)})

    day = dt.date(2026, 10, 1)
    calls = {
        "get_underlines": lambda api: len(api.get_underlines()),
        "get_highlights_on_this_day": lambda api: [len(api.get_highlights_on_this_day(day)),
                                                   len(api.get_highlights_on_this_day(day, include_orphans=False))],
        "sample_highlights": lambda api: [len(api.sample_highlights(limit=None, on=day)),
                                          [a.book.title for a in api.sample_highlights(on=day, book_id=book["id"])]],
        "get_finished_books": lambda api: len(api.get_finished_books(finished_after=dt.date(2026, 1, 1))),
        "get_annotations_by_date_range": lambda api: len(api.get_annotations_by_date_range(
            dt.date(2025, 1, 1), dt.date(2026, 12, 31))),
        "get_library_stats": lambda api: len(api.get_library_stats().orphan_assets),
    }
    extra = {name: getattr(PyAppleBooks, name) for name in sys.argv[3:]}
    for name in extra:
        calls.setdefault(name, None)

    policy = audit.Policy.for_library(home, books=[bundle], allow=[lib.prefs_path])
    out = {"results": {}, "errors": {}, "refused": [], "uncalled": []}
    api = PyAppleBooks(data_dir=lib.data_dir)
    mobile = os.path.join(os.path.realpath(home), "library", "mobile documents").casefold()
    roots = [os.path.realpath(bundle).casefold(), mobile]

    def inside(path):
        p = os.path.realpath(path).casefold() if not path.startswith("<") else path
        return path.startswith("<") or any(p == r or p.startswith(r + os.sep) for r in roots)

    stat_seen = []
    with audit.block(policy, all_threads=True) as rec:
        for name, call in calls.items():
            if call is None:
                out["uncalled"].append(name)
                continue
            try:
                out["results"][name] = call(api)
            except Exception as e:
                out["errors"][name] = "".join(traceback.format_exception_only(type(e), e))
    seen, stat_seen = stat_seen, None
    out["refused"] = [[ev.event, reason] for ev, reason in rec.refused]
    out["stat_calls"] = len(seen)
    out["stat_inside"] = sorted({p for p in seen if inside(p)})

    stat_seen = []
    with audit.block(policy, all_threads=True) as control:
        try:
            api.get_book_content(book["id"])
            out["control"] = "read"
        except Exception as e:
            out["control"] = type(e).__name__
    seen, stat_seen = stat_seen, None
    out["control_refused"] = len(control.refused)
    out["control_stat_inside"] = len([p for p in seen if inside(p)])
    api.close()
    print(json.dumps(out))
''')


def test_engagement_methods_touch_no_book_file(tmp_path):
    methods = sorted(name for name in vars(_EngagementAPI) if not name.startswith("_"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
    env["HOME"] = str(tmp_path)
    done = subprocess.run(
        [sys.executable, "-I", "-c", _SCRIPT, str(TESTS / "_fs_audit.py"), str(tmp_path / "home"), *methods],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr[-3000:]
    out = json.loads(done.stdout)
    assert out["uncalled"] == [], f"engagement methods the script doesn't call: {out['uncalled']}"
    assert out["errors"] == {}
    assert out["refused"] == []
    results = out["results"]
    assert results["get_underlines"] == 1
    assert results["get_highlights_on_this_day"] == [6, 5]
    assert results["sample_highlights"][0] == 4 and set(results["sample_highlights"][1]) == {"Cloud Book"}
    assert results["get_finished_books"] == 1 and results["get_library_stats"] == 1
    # No stat-family lookup in the bundle or iCloud Drive (the methods
    # do stat other paths, such as the store folders).
    assert out["stat_calls"] > 0
    assert out["stat_inside"] == []
    # The control: reading the book is refused, and it stats the bundle.
    assert out["control_refused"] > 0 and out["control"] != "read"
    assert out["control_stat_inside"] > 0
