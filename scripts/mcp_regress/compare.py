"""compare.py BASE_A.json BASE_B.json CANDIDATE.json [DETAIL_DIR]
           [--through STAGE] [--home SNAPSHOT_HOME] [--rules expected.py]

Reports, per tool, how many calls changed between baseline and candidate,
ignoring calls whose output already differs between the two baseline runs
(nondeterministic). Then checks the changes against the rules in
expected.py for every item up to --through STAGE (default final):

  UNEXPECTED     changed keys no active item allows
  NEW EXCEPTION  baseline A answered, the candidate raised (even if allowed)
  MISSING        keys of baseline A absent from the candidate

and prints per-item counts of allowed changes. Exits 1 if any of the three
lists is non-empty, else 0. Prints keys and counts only, never output text.
DETAIL_DIR gets unified diffs, which hold private library text: keep it
outside the repo.
"""
import argparse
import difflib
import importlib.util
import json
import pathlib
import sys
from collections import defaultdict

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harness import private_output  # noqa: E402


def load_rules(path):
    spec = importlib.util.spec_from_file_location("mcp_regress_expected", path)
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    return rules


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_a")
    parser.add_argument("base_b")
    parser.add_argument("candidate")
    parser.add_argument("detail_dir", nargs="?")
    parser.add_argument("--through", default="final",
                        help="last stage whose expected changes apply (a stage or stream alias)")
    parser.add_argument("--home", help="snapshot HOME the runs used; required unless baseline")
    parser.add_argument("--rules", default=str(HERE / "expected.py"),
                        help="rules module (default: the sibling expected.py)")
    args = parser.parse_args(argv)
    args.rules_module = load_rules(args.rules)
    try:
        args.stage = args.rules_module.resolve_stage(args.through)
    except ValueError as e:
        parser.error(str(e))
    if args.stage != "baseline" and not args.home:
        parser.error(f"--home is required with --through {args.through}")
    return args


def main(argv=None):
    args = parse_args(argv)
    rules = args.rules_module
    a, b, c = (json.load(open(p)) for p in (args.base_a, args.base_b, args.candidate))
    detail = private_output(args.detail_dir) if args.detail_dir else None
    if detail:
        detail.mkdir(parents=True, exist_ok=True)

    flaky = {k for k in a["out"] if a["out"][k] != b["out"].get(k)}
    missing = sorted(k for k in a["out"] if k not in c["out"])
    extra = [k for k in c["out"] if k not in a["out"]]
    changed = [k for k, before in a["out"].items()
               if k not in flaky and k in c["out"] and c["out"][k] != before]
    new_exc = sorted(k for k, before in a["out"].items()
                     if k in c["out"] and not before.startswith("EXCEPTION")
                     and c["out"][k].startswith("EXCEPTION"))
    sets = rules.book_sets(args.home) if args.stage != "baseline" else {}
    allowed = rules.allowed_keys(changed, args.stage, sets)
    by_item = rules.allowed_by_item(changed, args.stage, sets)
    unexpected = [k for k in changed if k not in allowed]

    per_tool = defaultdict(lambda: {"calls": 0, "changed": 0, "exc_before": 0, "exc_after": 0,
                                    "t_before": 0.0, "t_after": 0.0,
                                    "chars_before": 0, "chars_after": 0})
    for key, before in a["out"].items():
        tool = key.split("(", 1)[0]
        after = c["out"].get(key, "<MISSING>")
        row = per_tool[tool]
        row["calls"] += 1
        row["t_before"] += min(a["timings"].get(key, 0), b["timings"].get(key, 0))
        row["t_after"] += c["timings"].get(key, 0)
        row["chars_before"] += len(before)
        row["chars_after"] += len(after)
        row["exc_before"] += before.startswith("EXCEPTION")
        row["exc_after"] += after.startswith("EXCEPTION")
    for n, key in enumerate(changed, 1):
        tool = key.split("(", 1)[0]
        per_tool[tool]["changed"] += 1
        if detail:
            items = sorted(i for i, keys in by_item.items() if key in keys)
            verdict = f"allowed by {', '.join(items)}" if items else "UNEXPECTED"
            diff = difflib.unified_diff(a["out"][key].splitlines(), c["out"][key].splitlines(),
                                        "baseline", "candidate", n=1, lineterm="")
            (detail / f"{n:04d}_{tool}.diff").write_text(f"{key}\n{verdict}\n" + "\n".join(diff) + "\n")

    print(f"baseline lib:  {a['lib']}\ncandidate lib: {c['lib']}")
    print(f"through: {args.through} (stage {args.stage}); rules: {args.rules}")
    print(f"{len(a['out'])} calls; {len(flaky)} nondeterministic between baseline runs (ignored); "
          f"{len(changed)} changed, {len(changed) - len(unexpected)} allowed, "
          f"{len(unexpected)} unexpected\n")
    print(f"{'tool':32} {'calls':>5} {'chg':>4} {'exc 0->1':>9} "
          f"{'time before->after (s)':>24} {'chars before->after':>24}")
    for tool, r in sorted(per_tool.items()):
        print(f"{tool:32} {r['calls']:5d} {r['changed']:4d} {r['exc_before']:4d}->{r['exc_after']:<4d} "
              f"{r['t_before']:11.1f} -> {r['t_after']:<10.1f} {r['chars_before']:11d} -> {r['chars_after']:<11d}")
    if flaky:
        print("\nnondeterministic:", sorted({k.split('(', 1)[0] for k in flaky}))
    if extra:
        print(f"\nnote: {len(extra)} candidate keys not in baseline A (different ARGS.json?)")

    print("\nallowed changes per item:")
    for item, keys in by_item.items():
        print(f"  {item:6} {rules.ITEMS[item]['stage']:10} {len(keys):5d}")
    if not by_item:
        print("  (none: no items apply at this stage)")
    for title, keys in (("UNEXPECTED", unexpected), ("NEW EXCEPTION", new_exc), ("MISSING", missing)):
        print(f"\n{title}: {len(keys)}")
        for key in keys:
            print(f"  {key}")
    return 1 if unexpected or new_exc or missing else 0


if __name__ == "__main__":
    sys.exit(main())
