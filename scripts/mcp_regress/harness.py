"""Before/after regression harness for the MCP 0.8.2 read tools (read-only).

  discover [--force] ARGS.json   pick deterministic call arguments (run on 1.9.1)
  run ARGS.json OUT.json         call every read tool, save {call_key: output}

Run with HOME pointing at a read-only snapshot of the Books stores (see
README.md). Which py_apple_books is used is decided by the interpreter and
PYTHONPATH; ``run`` prints its path. Never calls write tools.

``discover`` refuses anything but py_apple_books 1.9.1 unless ``--force``:
the ids must come from the unscoped 1.9.1 view, so rows a later version
hides (Store series items, deleted annotations) are still exercised.

ARGS.json and OUT.json hold private library data (ids, titles, highlight
text). Write them under a scratch directory and never commit them; both
commands refuse a path inside a git working tree.
"""
import argparse
import json
import pathlib
import re
import sys
import time

MAX_CONTEXT_ANNOTATIONS = 200
DISCOVER_VERSION = "1.9.1"

# ``s`` (apple_books_mcp.server) and ``py_apple_books`` are imported in
# __main__ after the version check: importing the server builds its
# PyAppleBooks(), which a wrong or stub library can't do.


def private_output(path):
    """Return ``path`` resolved, exiting 2 if it lies inside a git working tree."""
    p = pathlib.Path(path).resolve()
    for d in (p, *p.parents):
        if (d / ".git").exists():
            print(f"refusing to write private output inside the git working tree {d}; "
                  "use a scratch directory", file=sys.stderr)
            sys.exit(2)
    return p


def text_of(result):
    if hasattr(result, "text"):
        return result.text
    if isinstance(result, list):
        return "\n".join(text_of(r) for r in result)
    if hasattr(result, "content"):
        return text_of(result.content)
    return str(result)


def require_discover_version(lib, force):
    version = getattr(lib, "__version__", None)
    if version == DISCOVER_VERSION:
        return
    msg = (f"discover needs py_apple_books {DISCOVER_VERSION} (the unscoped view), "
           f"got {version!r} from {lib.__file__}")
    if not force:
        print(f"{msg}; pass --force to use it anyway", file=sys.stderr)
        sys.exit(2)
    print(f"warning: {msg} (--force)", file=sys.stderr)


def discover(path):
    lib = s.apple_books
    books = sorted(lib.list_books(), key=lambda b: int(b.id))
    book_ids = [int(b.id) for b in books]
    annotated = []
    for b in books:
        if len(b.annotations):
            annotated.append(int(b.id))
    collections = sorted(int(c.id) for c in lib.list_collections())
    annos = sorted(lib.list_annotations(), key=lambda a: int(a.id))
    hinted = [int(a.id) for a in annos if getattr(a, "location", None) and a.location.chapter_id]
    step = max(1, len(hinted) // MAX_CONTEXT_ANNOTATIONS)
    context_ids = hinted[::step][:MAX_CONTEXT_ANNOTATIONS]
    describe_ids = [int(a.id) for a in annos[:: max(1, len(annos) // 60)]][:60]
    chapter_calls = []
    for bid in book_ids:
        out = text_of(s.list_book_chapters(bid))
        ids = [m.group(1) for m in re.finditer(r"\(id=([^)]+)\)\s*$", out, re.M)]
        for cid in (ids[:1] + ids[len(ids) // 2: len(ids) // 2 + 1]):
            chapter_calls.append([bid, cid])
    revisit_title = next((b.title for b in books if int(b.id) in annotated and b.title), "a")
    args = dict(book_ids=book_ids, annotated=annotated, collections=collections,
                context_ids=context_ids, describe_ids=describe_ids,
                chapter_calls=chapter_calls, revisit_title=revisit_title,
                colors=["yellow", "green", "blue", "pink", "purple", "underline"])
    json.dump(args, open(path, "w"))
    print(f"books={len(book_ids)} annotated={len(annotated)} collections={len(collections)} "
          f"context={len(context_ids)} (of {len(hinted)} hinted) chapter_calls={len(chapter_calls)}")


def calls(a):
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


def run(args_path, out_path):
    a = json.load(open(args_path))
    out, timings = {}, {}
    print("py_apple_books from", py_apple_books.__file__, file=sys.stderr)
    for name, kwargs in calls(a):
        key = f"{name}({json.dumps(kwargs, sort_keys=True)})"
        t = time.perf_counter()
        try:
            out[key] = text_of(getattr(s, name)(**kwargs))
        except Exception as e:  # recorded, compared like any other output
            out[key] = f"EXCEPTION {type(e).__name__}: {e}"
        timings[key] = round(time.perf_counter() - t, 3)
    json.dump({"lib": py_apple_books.__file__, "out": out, "timings": timings}, open(out_path, "w"))
    print(f"{len(out)} calls, {sum(timings.values()):.1f}s total", file=sys.stderr)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="write ARGS.json from the 1.9.1 library")
    d.add_argument("args_json")
    d.add_argument("--force", action="store_true",
                   help=f"run on a py_apple_books other than {DISCOVER_VERSION}")
    r = sub.add_parser("run", help="call every read tool with ARGS.json, write OUT.json")
    r.add_argument("args_json")
    r.add_argument("out_json")
    return parser.parse_args(argv)


if __name__ == "__main__":
    cli = parse_args()
    import py_apple_books
    if cli.cmd == "discover":
        require_discover_version(py_apple_books, cli.force)
        target = private_output(cli.args_json)
        from apple_books_mcp import server as s
        discover(target)
    else:
        target = private_output(cli.out_json)
        from apple_books_mcp import server as s
        run(cli.args_json, target)
