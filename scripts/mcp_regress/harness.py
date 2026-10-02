"""Before/after regression harness for the MCP read tools (read-only).

  discover [--force] ARGS.json   pick deterministic call arguments (a released baseline only)
  run [--mcp-version V] ARGS.json OUT.json
                                 call every read tool, save {call_key: output}

Run with HOME pointing at a read-only snapshot of the Books stores (see
README.md); both commands refuse the real home. Which py_apple_books
and apple-books-mcp are used is decided by the interpreter and
PYTHONPATH; both commands print them. Never calls write tools.
apple-books-mcp 0.8.2 and 0.9.0 are supported; 0.9.0 adds calls for
its new tool and arguments (each argument it added, on every tool that
has it, is passed by at least one call).

On macOS both commands first turn off downloads of evicted iCloud files
for their own process, so a book file that is only in iCloud makes its
tool fail rather than download (on the baseline and the candidate
alike). If macOS refuses, they exit 2 unless ``--allow-downloads``.

``discover`` refuses anything but a released baseline (py_apple_books
1.9.1 or 1.10.0, compared byte for byte with the release sources,
since development trees report the last release's version until the
bump) unless ``--force``. The ids come from the unscoped view (Store
series items and deleted annotations included), so rows a version
hides are still exercised. Run ``discover`` with a baseline venv.

ARGS.json and OUT.json hold private library data (ids, titles, highlight
text). Write them under a scratch directory and never commit them; both
commands refuse a path inside a git working tree.
"""
import argparse
import hashlib
import inspect
import json
import os
import pathlib
import pwd
import re
import sys
import time

MAX_CONTEXT_ANNOTATIONS = 200
MAX_CURRENT_CHAPTER_BOOKS = 60
MAX_SHORT_CONTEXT_ANNOTATIONS = 10
# source_digest() of the releases discover accepts and compare can name
# as a baseline (each PyPI wheel and its tag agree).
RELEASES = {
    "1.9.1": "99e7f4f8ec51d45ee8929f13d91797236935cfdc49681c4e065121b21245f701",
    "1.10.0": "e061bd1f64b30870850825e93862d4a9e6db05da6282e2f90eb191590b89b0af",
}
DISCOVER_VERSION = "1.9.1"  # kept for scripts that import it
DISCOVER_DIGEST = RELEASES[DISCOVER_VERSION]
MCP_VERSIONS = ("0.8.2", "0.9.0")
BOOKS_DOCUMENTS = pathlib.Path("Library/Containers/com.apple.iBooksX/Data/Documents")

# ``s`` (apple_books_mcp.server) and ``py_apple_books`` are imported in
# __main__ after the version and HOME checks: importing the server builds
# its PyAppleBooks(), which a wrong or stub library can't do.


def private_output(path):
    """Return ``path`` resolved, exiting 2 if it lies inside a git working tree."""
    p = pathlib.Path(path).resolve()
    for d in (p, *p.parents):
        if (d / ".git").exists():
            print(f"refusing to write private output inside the git working tree {d}; "
                  "use a scratch directory", file=sys.stderr)
            sys.exit(2)
    return p


def require_snapshot_home():
    """Exit 2 unless HOME is a snapshot: not the real home, and holding a BKLibrary folder."""
    home = os.environ.get("HOME")
    real = pwd.getpwuid(os.getuid()).pw_dir
    if not home or pathlib.Path(home).resolve() == pathlib.Path(real).resolve():
        print("HOME must point at a snapshot made with snap.py, not the real home", file=sys.stderr)
        sys.exit(2)
    if not (pathlib.Path(home) / BOOKS_DOCUMENTS / "BKLibrary").is_dir():
        print(f"HOME has no {BOOKS_DOCUMENTS}/BKLibrary; is it a snapshot?", file=sys.stderr)
        sys.exit(2)


def text_of(result):
    if hasattr(result, "text"):
        return result.text
    if isinstance(result, list):
        return "\n".join(text_of(r) for r in result)
    if hasattr(result, "content"):
        return text_of(result.content)
    return str(result)


def source_digest(lib):
    """Return a sha256 over the package's .py and .ini files (paths and bytes)."""
    root = pathlib.Path(lib.__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.suffix in (".py", ".ini")):
        data = f.read_bytes()
        h.update(f"{f.relative_to(root).as_posix()}\0{len(data)}\0".encode())
        h.update(data)
    return h.hexdigest()


def release_of(lib):
    """The release ``lib`` is (a RELEASES key), or None.

    The version alone can't tell: development trees report the last
    release's version until the bump, whatever way they are installed. So
    the imported sources must also match the release byte for byte.
    """
    version = getattr(lib, "__version__", None)
    if version in RELEASES and source_digest(lib) == RELEASES[version]:
        return version
    return None


def release_problem(lib):
    """Return why ``lib`` is not a released baseline, or None."""
    version = getattr(lib, "__version__", None)
    if version not in RELEASES:
        return f"it reports version {version!r}"
    if release_of(lib) is None:
        return f"its sources differ from the {version} release (a development tree?)"
    return None


def require_discover_version(lib, force):
    problem = release_problem(lib)
    if problem is None:
        return
    names = " or ".join(RELEASES)
    msg = (f"discover needs a released py_apple_books ({names}), but {problem}; "
           f"imported from {lib.__file__}. Use a baseline venv with the release from PyPI")
    if not force:
        print(f"{msg}; pass --force to use it anyway", file=sys.stderr)
        sys.exit(2)
    print(f"warning: {msg} (--force)", file=sys.stderr)


def mcp_version():
    """The installed apple-books-mcp version, or None."""
    from importlib import metadata
    try:
        return metadata.version("apple-books-mcp")
    except metadata.PackageNotFoundError:
        return None


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


def require_mcp_version(wanted):
    """Exit 2 unless the installed apple-books-mcp is ``wanted`` (if given)
    and one this harness knows; return its version."""
    have = mcp_version()
    if wanted and have != wanted:
        print(f"--mcp-version {wanted}, but this interpreter has apple-books-mcp {have}",
              file=sys.stderr)
        sys.exit(2)
    if have not in MCP_VERSIONS:
        print(f"apple-books-mcp {have} is not one of {', '.join(MCP_VERSIONS)}", file=sys.stderr)
        sys.exit(2)
    return have


def disable_materialization():
    """Turn off downloads of evicted (dataless) iCloud files for this
    process and its children: reading one then fails with EDEADLK. True
    if the policy is on; False off macOS or on any failure."""
    if sys.platform != "darwin":
        return False
    try:
        import ctypes

        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        # IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_PROCESS,
        # IOPOL_MATERIALIZE_DATALESS_FILES_OFF
        if libc.setiopolicy_np(3, 0, 1) != 0:
            return False
        return libc.getiopolicy_np(3, 0) == 1
    except (OSError, AttributeError):
        return False


def require_download_policy(allow_downloads):
    """Turn downloads of evicted files off (see disable_materialization).
    On macOS, exit 2 if that fails, unless ``allow_downloads``: the runs
    read book files at the paths the snapshot records."""
    on = disable_materialization()
    print(f"download of evicted iCloud files turned off: {on}", file=sys.stderr)
    if sys.platform == "darwin" and not on and not allow_downloads:
        print("could not turn off downloads of evicted iCloud files, so a book file that is "
              "only in iCloud would be downloaded; pass --allow-downloads to run anyway",
              file=sys.stderr)
        sys.exit(2)
    return on


def _unscoped(method, **scope):
    """``method(**scope)``, or ``method()`` on a release whose method has
    no such keywords (1.9.1 lists every row anyway). Decided from the
    signature, not by catching TypeError, so an error raised inside the
    method is never mistaken for an old release."""
    params = inspect.signature(method).parameters
    if any(p.kind is p.VAR_KEYWORD for p in params.values()) or set(scope) <= set(params):
        return method(**scope)
    print(f"note: {method.__name__} has no {', '.join(sorted(set(scope) - set(params)))}; "
          f"using its default view", file=sys.stderr)
    return method()


def discover(path):
    print(f"py_apple_books {py_apple_books.__version__} from {py_apple_books.__file__}",
          file=sys.stderr)
    lib = s.apple_books
    books = sorted(_unscoped(lib.list_books, include_store_series=True), key=lambda b: int(b.id))
    book_ids = [int(b.id) for b in books]
    annos = sorted(_unscoped(lib.list_annotations, include_deleted=True), key=lambda a: int(a.id))
    anno_assets = {a.asset_id for a in annos}
    annotated = []
    for b in books:
        # 1.10 hides deleted rows from Book.annotations; their books still count.
        if len(b.annotations) or b.asset_id in anno_assets:
            annotated.append(int(b.id))
    collections = sorted(int(c.id) for c in lib.list_collections())
    hinted = [int(a.id) for a in annos if getattr(a, "location", None) and a.location.chapter_id]
    step = max(1, len(hinted) // MAX_CONTEXT_ANNOTATIONS)
    context_ids = hinted[::step][:MAX_CONTEXT_ANNOTATIONS]
    describe_ids = [int(a.id) for a in annos[:: max(1, len(annos) // 60)]][:60]
    chapter_calls = []
    for bid in book_ids:
        try:
            out = text_of(s.list_book_chapters(bid))
        except Exception:  # 0.9.0 raises for a book it can't read
            continue
        ids = [m.group(1) for m in re.finditer(r"\(id=([^)]+)\)\s*$", out, re.M)]
        for cid in (ids[:1] + ids[len(ids) // 2: len(ids) // 2 + 1]):
            chapter_calls.append([bid, cid])
    revisit_title = next((b.title for b in books if int(b.id) in annotated and b.title), "a")
    args = dict(book_ids=book_ids, annotated=annotated, collections=collections,
                context_ids=context_ids, describe_ids=describe_ids,
                chapter_calls=chapter_calls, revisit_title=revisit_title,
                colors=["yellow", "green", "blue", "pink", "purple", "underline"],
                discovered_with={"py_apple_books": release_of(py_apple_books),
                                 "apple_books_mcp": mcp_version()})
    json.dump(args, open(path, "w"))
    print(f"books={len(book_ids)} annotated={len(annotated)} collections={len(collections)} "
          f"context={len(context_ids)} (of {len(hinted)} hinted) chapter_calls={len(chapter_calls)}")


def calls(a, version="0.8.2"):
    """``(tool, kwargs)`` for every call; apple-books-mcp ``version``
    0.9.0 and later get the calls for its new tool and arguments too."""
    yield "list_all_collections", {}
    for cid in a["collections"]:
        yield "get_collection_books", {"collection_id": str(cid)}
        yield "describe_collection", {"collection_id": str(cid)}
    yield "search_collections_by_title", {"title": "a"}
    yield "list_all_books", {}
    for bid in a["book_ids"]:
        yield "describe_book", {"book_id": str(bid)}
        yield "list_book_chapters", {"book_id": bid}
        yield "get_current_reading_position", {"book_id": bid}
    for bid in a["annotated"]:
        yield "list_annotations", {"book_id": bid}
    for bid, cid in a["chapter_calls"]:
        yield "get_chapter_content", {"book_id": bid, "chapter_id": cid}
    yield "search_books_by_title", {"title": "the"}
    yield "get_books_by_genre", {"genre": "Fiction"}
    yield "get_books_in_progress", {}
    yield "get_finished_books", {}
    yield "get_unstarted_books", {}
    yield "get_recently_read_books", {}
    yield "list_all_annotations", {"limit": 500}
    yield "list_all_annotations", {}
    for color in a["colors"]:
        yield "get_highlights_by_color", {"color": color, "limit": 200}
    yield "search_notes", {"note": "the"}
    yield "search_annotations", {"text": "the", "limit": 200}
    yield "search_annotations", {"text": "don't", "limit": 20}
    yield "recent_annotations", {"limit": 50}
    for aid in a["describe_ids"]:
        yield "describe_annotation", {"annotation_id": str(aid)}
    for aid in a["context_ids"]:
        yield "get_annotation_context", {"annotation_id": aid}
    yield "get_annotations_by_date_range", {"after": "2025-01-01"}
    yield "get_library_stats", {}
    yield "currently_reading_resource", {}
    yield "weekly_digest", {"days": 7}
    yield "library_snapshot", {}
    yield "revisit_book", {"book_title": a["revisit_title"]}
    if version_key(version) < version_key("0.9.0"):
        return
    for query in ("the", "a", "don't"):
        yield "search_books", {"query": query}
    # chapter_id defaults to "current": the chapter being read.
    readable = list(dict.fromkeys(bid for bid, _ in a["chapter_calls"]))
    step = max(1, len(readable) // MAX_CURRENT_CHAPTER_BOOKS)
    for bid in readable[::step][:MAX_CURRENT_CHAPTER_BOOKS]:
        yield "get_chapter_content", {"book_id": bid}
    yield "search_books", {"query": "the", "limit": 5, "offset": 5}
    yield "list_all_books", {"limit": 50, "offset": 50}
    yield "list_all_annotations", {"limit": 100, "offset": 100}
    for bid in a["annotated"][:1]:
        yield "list_annotations", {"book_id": bid, "limit": 5, "offset": 5}
    yield "search_annotations", {"text": "the", "limit": 50, "offset": 50}
    yield "search_annotations", {"text": "the", "limit": 50, "order_by": "oldest"}
    yield "get_annotations_by_date_range", {"after": "2025-01-01", "order_by": "oldest"}
    ctx = a["context_ids"]
    for aid in ctx[:: max(1, len(ctx) // MAX_SHORT_CONTEXT_ANNOTATIONS)][:MAX_SHORT_CONTEXT_ANNOTATIONS]:
        yield "get_annotation_context", {"annotation_id": aid, "chars_before": 50, "chars_after": 50}
    # offset on the other paged tools, and order_by "oldest" on every tool
    # that has it: each reaches its own library query. offset=1, so a small
    # library still gets a page (rows, not only the past-the-end count).
    yield "list_all_collections", {"limit": 5, "offset": 1}
    yield "search_books_by_title", {"title": "the", "limit": 5, "offset": 1}
    yield "get_books_by_genre", {"genre": "Fiction", "limit": 5, "offset": 1}
    for tool in ("get_books_in_progress", "get_finished_books", "get_unstarted_books",
                 "get_recently_read_books"):
        yield tool, {"limit": 5, "offset": 1}
    yield "recent_annotations", {"limit": 20, "offset": 1}
    for color in a["colors"]:
        yield "get_highlights_by_color", {"color": color, "limit": 50, "order_by": "oldest"}
    for color in a["colors"][:1]:
        yield "get_highlights_by_color", {"color": color, "limit": 20, "offset": 1}
    yield "search_notes", {"note": "the", "limit": 50, "order_by": "oldest"}
    yield "search_notes", {"note": "the", "limit": 20, "offset": 1}
    yield "get_annotations_by_date_range", {"after": "2025-01-01", "limit": 50, "offset": 1}


def run(args_path, out_path, version):
    a = json.load(open(args_path))
    out, timings = {}, {}
    print(f"py_apple_books from {py_apple_books.__file__}; apple-books-mcp {version}",
          file=sys.stderr)
    for name, kwargs in calls(a, version):
        key = f"{name}({json.dumps(kwargs, sort_keys=True)})"
        t = time.perf_counter()
        try:
            out[key] = text_of(getattr(s, name)(**kwargs))
        except Exception as e:  # recorded, compared like any other output
            out[key] = f"EXCEPTION {type(e).__name__}: {e}"
        timings[key] = round(time.perf_counter() - t, 3)
    json.dump({"lib": py_apple_books.__file__, "lib_version": py_apple_books.__version__,
               "lib_release": release_of(py_apple_books), "mcp_version": version,
               "out": out, "timings": timings}, open(out_path, "w"))
    print(f"{len(out)} calls, {sum(timings.values()):.1f}s total", file=sys.stderr)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="write ARGS.json from a released baseline library")
    d.add_argument("args_json")
    d.add_argument("--force", action="store_true",
                   help=f"accept a py_apple_books other than the {' or '.join(RELEASES)} releases")
    r = sub.add_parser("run", help="call every read tool with ARGS.json, write OUT.json")
    r.add_argument("args_json")
    r.add_argument("out_json")
    r.add_argument("--mcp-version", choices=MCP_VERSIONS,
                   help="exit 2 unless this is the installed apple-books-mcp")
    for p in (d, r):
        p.add_argument("--allow-downloads", action="store_true",
                       help="run even if macOS refuses to turn off downloads of evicted "
                            "iCloud files")
    return parser.parse_args(argv)


if __name__ == "__main__":
    cli = parse_args()
    require_download_policy(cli.allow_downloads)
    import py_apple_books
    if cli.cmd == "discover":
        require_discover_version(py_apple_books, cli.force)
    version = require_mcp_version(getattr(cli, "mcp_version", None))
    require_snapshot_home()
    target = private_output(cli.args_json if cli.cmd == "discover" else cli.out_json)
    from apple_books_mcp import server as s
    if cli.cmd == "discover":
        discover(target)
    else:
        run(cli.args_json, target, version)
