"""Odd-argument probe of the MCP 0.8.2 tools (read-only).

  run OUT.json                 call each tool with an odd argument, save result classes
  compare BASE.json CAND.json  print the transition table; exit 1 on a regression

``run`` imports apple_books_mcp.server in-process with the current
interpreter, so the py_apple_books it imports is the one under test; HOME
must be a snapshot (see README.md). Arguments are huge or out-of-int64
ids and limits, lone surrogates, NUL and an unknown colour.

Only the class of each result is stored:
  EXC <ExceptionType>   the tool raised (MCP reports isError)
  OK notfound           a short one-line text that starts with 'No ' or says
                        'No book found' / 'not found'
  OK <md5[:8]> len=<n>  any other text

compare exits 1 on OK -> EXC, on 'OK notfound' -> anything else, or on a
call missing from CAND; EXC -> OK is reported as FIXED.
"""
import argparse
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harness import private_output, require_snapshot_home, text_of  # noqa: E402

HUGE = 99999999999999999999
SURROGATE = chr(0xD800)

CALLS = [
    ("describe_book", {"book_id": str(HUGE)}),
    *(("list_annotations", {"book_id": n}) for n in (HUGE, 2**63 - 1, 2**63, -2**63 - 1, -1)),
    ("list_book_chapters", {"book_id": HUGE}),
    ("get_current_reading_position", {"book_id": HUGE}),
    ("get_annotation_context", {"annotation_id": HUGE}),
    ("describe_annotation", {"annotation_id": str(HUGE)}),
    ("get_collection_books", {"collection_id": "1e3"}),
    ("get_collection_books", {"collection_id": str(HUGE)}),
    ("list_all_books", {"limit": HUGE}),
    ("list_all_books", {"limit": -HUGE}),
    ("search_annotations", {"text": "x", "limit": -1}),
    ("search_annotations", {"text": SURROGATE}),
    ("search_notes", {"note": "a" + SURROGATE + "b"}),
    ("search_books_by_title", {"title": SURROGATE}),
    ("search_books_by_title", {"title": chr(0)}),
    ("get_highlights_by_color", {"color": "orange"}),
    ("get_chapter_content", {"book_id": HUGE, "chapter_id": "1"}),
]
# A not-found answer is one short line; longer texts (e.g. every match for
# 'x') may quote highlights that happen to say 'not found' or a title
# starting with 'No '.
NOTFOUND_MAX_LEN = 200


def key_of(name, kwargs):
    return f"{name}({json.dumps(kwargs, sort_keys=True)})"


def classify(text):
    short = len(text) <= NOTFOUND_MAX_LEN and "\n" not in text
    if short and (text.startswith("No ") or "No book found" in text or "not found" in text):
        return "OK notfound"
    digest = hashlib.md5(text.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"OK {digest} len={len(text)}"


def run(out_path):
    require_snapshot_home()
    target = private_output(out_path)
    import py_apple_books
    from apple_books_mcp import server as s
    print(f"py_apple_books {py_apple_books.__version__} from {py_apple_books.__file__}",
          file=sys.stderr)
    out = {}
    for name, kwargs in CALLS:
        try:
            result = classify(text_of(getattr(s, name)(**kwargs)))
        except Exception as e:  # the class is the result
            result = f"EXC {type(e).__name__}"
        out[key_of(name, kwargs)] = result
        print(f"{key_of(name, kwargs):72} {result}")
    json.dump({"lib": py_apple_books.__file__, "version": py_apple_books.__version__,
               "python": sys.version.split()[0], "out": out}, open(target, "w"), indent=1)


def transition(before, after):
    """Return (verdict, is_regression) for one call's class change."""
    if after is None:
        return "MISSING", True
    if before == after:
        return "same", False
    if before.startswith("OK") and after.startswith("EXC"):
        return "REGRESSION ok->exc", True
    if before == "OK notfound":
        return "REGRESSION notfound->found", True
    if before.startswith("EXC") and after.startswith("OK"):
        return "FIXED", False
    return "changed", False


def compare(base_path, cand_path):
    base, cand = (json.load(open(p)) for p in (base_path, cand_path))
    print(f"base: py_apple_books {base.get('version')} ({base.get('python')})  "
          f"cand: py_apple_books {cand.get('version')} ({cand.get('python')})\n")
    print(f"{'call':72} {'base':22} {'cand':22} verdict")
    counts, failed = {}, False
    for key, before in base["out"].items():
        after = cand["out"].get(key)
        verdict, bad = transition(before, after)
        failed |= bad
        counts[verdict] = counts.get(verdict, 0) + 1
        print(f"{key:72} {before:22} {str(after):22} {verdict}")
    extra = [k for k in cand["out"] if k not in base["out"]]
    if extra:
        print(f"\nnote: {len(extra)} calls only in CAND: {extra}")
    print("\n" + ", ".join(f"{v}: {n}" for v, n in sorted(counts.items())))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="probe the installed library, write OUT.json")
    r.add_argument("out_json")
    c = sub.add_parser("compare", help="compare two run outputs")
    c.add_argument("base_json")
    c.add_argument("cand_json")
    args = parser.parse_args(argv)
    if args.cmd == "run":
        run(args.out_json)
        return 0
    return compare(args.base_json, args.cand_json)


if __name__ == "__main__":
    sys.exit(main())
