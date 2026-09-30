# MCP real-library regression tooling

Checks that a py-apple-books candidate keeps apple-books-mcp 0.8.2 working on
a real library: every read tool is called with the same arguments on the
released 1.9.1 and on the candidate, and each changed output must be allowed
by a rule in `expected.py`. These scripts use only the standard library and
are not part of the wheel.

| File | Purpose |
|---|---|
| `harness.py` | `discover` picks call arguments on 1.9.1; `run` calls every MCP 0.8.2 read tool and saves the outputs and timings. |
| `compare.py` | Compares a candidate run with two baseline runs against the rules. |
| `expected.py` | Which outputs each audit item may change, and from which stage. |
| `odd_args.py` | Probes huge ids and limits, lone surrogates and similar arguments, and compares result classes. |

## Privacy

Everything these scripts write holds private library data: `args.json` has ids
and a book title, run outputs and `DETAIL_DIR` diffs have highlight text. Keep
all of it in a scratch directory outside the repository. The scripts refuse
to write inside a git working tree.

`compare.py` prints keys and counts, never output text. Keys still carry
library ids, chapter ids (which can contain title words or ISBNs) and one book
title. The per-tool table counts calls and characters, so it gives the size of
the library. `odd_args.py` stores result classes, but its outputs also hold
the `lib` path, and `len=<n>` is the length of private output.

For PR descriptions, the CHANGELOG and release notes, report only:
- the `compare.py` exit status;
- the UNEXPECTED, NEW EXCEPTION and MISSING totals;
- the `odd_args.py` verdict counts (`same`, `FIXED`, `REGRESSION ...`).

Per-tool or per-item counts go in only if the owner opts in. Keys, `lib` paths
and `len=` values never go in.

Never point `HOME` at the real home: the runs read a snapshot, and every
command refuses the real home or a `HOME` without a `BKLibrary` folder.
Content tools still read book files at the paths the store records, since
those are not part of the snapshot.

## Procedure

Every command runs from the repository root, with `$S` a scratch directory.

**1. Snapshot.** Make a read-only copy of the two stores with the SQLite
backup API, in rollback-journal mode, in a HOME-shaped directory:

```sh
DOCS=Library/Containers/com.apple.iBooksX/Data/Documents
for store in BKLibrary/BKLibrary-1-091020131601.sqlite \
             AEAnnotation/AEAnnotation_v10312011_1727_local.sqlite; do
  mkdir -p "$S/home/$DOCS/${store%/*}"
  sqlite3 "file:$HOME/$DOCS/$store?mode=ro" ".backup '$S/home/$DOCS/$store'"
  sqlite3 "$S/home/$DOCS/$store" "PRAGMA journal_mode=DELETE" >/dev/null
done
```

Library data drifts while you use Books, so take a fresh snapshot and a fresh
baseline pair for each check. Never reuse baselines from another snapshot.

**2. Environments.** You need two venvs with apple-books-mcp 0.8.2. The
baseline venv also has py-apple-books 1.9.1 from PyPI. The candidate venv has
the candidate installed, either as a wheel or editable from the checkout.
Use the same Python minor version for both. Each run prints the
`py_apple_books` it imported, so check that it is the tree you mean to test:
an editable install can shadow `PYTHONPATH`.

**3. Discover on 1.9.1.**

```sh
HOME=$S/home $S/base/bin/python scripts/mcp_regress/harness.py discover $S/args.json
```

The ids must come from the unscoped 1.9.1 view, so rows that later versions
hide (Store series items, deleted annotations) still get called. Always run
`discover` with the baseline venv: development trees report `__version__`
`1.9.1` until the release bump.

`discover` exits 2 unless the `py_apple_books` it imports is the 1.9.1
release. The version must be `1.9.1`, and a digest of the package's `.py` and
`.ini` files must match the release. So a development tree is refused however
it is installed (editable, wheel or `PYTHONPATH`). `discover` prints the
`py_apple_books` it imported. `--force` overrides the check.

**4. Baselines A and B, then the candidate.**

```sh
HOME=$S/home $S/base/bin/python scripts/mcp_regress/harness.py run $S/args.json $S/base-A.json
HOME=$S/home $S/base/bin/python scripts/mcp_regress/harness.py run $S/args.json $S/base-B.json
HOME=$S/home $S/cand/bin/python scripts/mcp_regress/harness.py run $S/args.json $S/cand.json
```

A key is `tool(json-args)` with sorted arguments. Calls whose outputs differ
between A and B are nondeterministic and are ignored.

**5. Compare.**

```sh
python3 scripts/mcp_regress/compare.py $S/base-A.json $S/base-B.json $S/cand.json $S/diffs \
    --through <stage> --home $S/home
```

- `--through` takes a stage from `expected.STAGES` (`baseline`, `query`,
  `semantics`, `final`) or a stream alias from `expected.ALIASES`. Items apply
  cumulatively: `semantics` includes the `query` items. The default is
  `final`.
- `--home` is the snapshot the runs used. The per-book rules (F25, F62, F07)
  are resolved from it. It is required unless the stage is `baseline`.
- `--rules` loads another rules file; the default is the sibling `expected.py`.
- Run it under the same `TZ` as the harness, because the F62 rule compares
  local dates.

The report prints the per-tool table and per-item counts of allowed changes,
followed by three lists:
- `UNEXPECTED`: changed keys that no active item allows;
- `NEW EXCEPTION`: keys where baseline A answered and the candidate raised,
  reported even if allowed;
- `MISSING`: baseline keys absent from the candidate.

It exits 1 if any list is non-empty, and 0 otherwise. `$S/diffs` gets one
unified diff per changed key. Each diff is headed by its key and by the items
that allow it, or by `UNEXPECTED`.

The stage is the latest one already in the branch under test, counting the
branch's own changes. The table assumes the planned merge order, where writes
and models-data land before query-layer. If query-layer is already in your
base, use `query`.

| Stream | `--through` |
|---|---|
| Wave 1, writes, models-data (output-neutral by themselves) | `baseline` |
| query-layer, connections | `query` |
| read-semantics, orm, facade | `semantics` |
| release gate | `final` |

**6. Odd arguments.**

```sh
HOME=$S/home $S/base/bin/python scripts/mcp_regress/odd_args.py run $S/odd-base.json
HOME=$S/home $S/cand/bin/python scripts/mcp_regress/odd_args.py run $S/odd-cand.json
python3 scripts/mcp_regress/odd_args.py compare $S/odd-base.json $S/odd-cand.json
```

Each call is stored as `EXC <type>`, `OK notfound` (a short one-line
not-found answer) or `OK <md5[:8]> len=<n>`.
`compare` prints a transition table. It exits 1 on `OK` -> `EXC`, on
`OK notfound` -> anything else, or on a missing call. `EXC` -> `OK` is
reported as `FIXED`. Result classes can depend on the Python version: on
1.9.1, a NUL title search is `EXC DBError` on 3.10 but `EXC DBQueryError` on
3.13. Compare runs made with the same Python minor version.

## Changing the rules

Rules may allow more than actually changes. Allowed means "not a regression
if it changes", never "must change". Every rule belongs to an `ITEMS` entry
named by its audit item id and assigned to the stage whose stream lands it.
Per-book rules go through a named set in `book_sets()` as a query on the
snapshot, so `expected.py` never contains ids, titles or counts.
