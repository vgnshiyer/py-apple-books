# MCP real-library regression tooling

Checks that a py-apple-books candidate keeps apple-books-mcp 0.8.2 and 0.9.0
working on a real library: every read tool is called with the same arguments
on a released baseline and on the candidate, and each changed output must be
allowed by a rule in `expected.py`. These scripts use only the standard
library and are not part of the wheel.

| File | Purpose |
|---|---|
| `harness.py` | `discover` picks call arguments on a released baseline; `run` calls every read tool of the installed MCP (0.8.2 or 0.9.0) and saves the outputs, timings and versions. |
| `compare.py` | Compares a candidate run with two baseline runs against the rules. |
| `expected.py` | Which outputs each audit item may change, in which release, and from which stage. |
| `odd_args.py` | Probes huge ids and limits, lone surrogates and similar arguments, and compares result classes. |

Baselines: for 1.11 and later, the released 1.10.0 for both MCP versions;
the rules then allow no change at all (every 1.10 item is already in the
baseline, and 1.11 adds none), so on a fully local library the candidate
must match the baseline exactly. The released 1.9.1 stays available as a
baseline for 0.8.2, with the 1.10 items active.

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
those are not part of the snapshot. So, on macOS, `harness.py` and
`odd_args.py run` first turn off downloads of evicted iCloud files for their
own process (`setiopolicy_np`, process scope; inherited by `du`): a book
file that is only in iCloud makes its tool fail instead of downloading, on
the baseline and the candidate alike. They print whether the policy is on,
and exit 2 if macOS refuses it (`--allow-downloads` runs anyway).
To keep book files out entirely, null `ZPATH` in the snapshot's
`ZBKLIBRARYASSET` first and run a DB-only pass.

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

**2. Environments.** Per MCP version, you need two venvs with that
apple-books-mcp (`0.8.2` or `0.9.0`). The baseline venv also has the baseline
py-apple-books from PyPI (`1.10.0`; or `1.9.1` with MCP 0.8.2). The candidate
venv has the candidate installed, either as a wheel or editable from the
checkout. Use the same Python minor version for both. Each run prints the
`py_apple_books` it imported, so check that it is the tree you mean to test:
an editable install can shadow `PYTHONPATH`.

**3. Discover on the baseline.**

```sh
HOME=$S/home $S/base/bin/python scripts/mcp_regress/harness.py discover $S/args.json
```

The ids come from the unscoped view (on 1.10.0, `include_store_series=True`
and `include_deleted=True`), so rows that later versions hide (Store series
items, deleted annotations) still get called. Always run `discover` with a
baseline venv: development trees report the last release's `__version__`
until the release bump. One `args.json` serves both MCP versions.

`discover` exits 2 unless the `py_apple_books` it imports is the 1.9.1 or
1.10.0 release: the version must be one of those, and a digest of the
package's `.py` and `.ini` files must match that release. So a development
tree is refused however it is installed (editable, wheel or `PYTHONPATH`).
`discover` prints the `py_apple_books` it imported. `--force` overrides the
check.

**4. Baselines A and B, then the candidate.**

```sh
HOME=$S/home $S/base/bin/python scripts/mcp_regress/harness.py run --mcp-version 0.9.0 $S/args.json $S/base-A.json
HOME=$S/home $S/base/bin/python scripts/mcp_regress/harness.py run --mcp-version 0.9.0 $S/args.json $S/base-B.json
HOME=$S/home $S/cand/bin/python scripts/mcp_regress/harness.py run --mcp-version 0.9.0 $S/args.json $S/cand.json
```

A key is `tool(json-args)` with sorted arguments. Calls whose outputs differ
between A and B are nondeterministic and are ignored. `--mcp-version` makes
the run exit 2 unless the venv has that apple-books-mcp; without it, the
installed version is used and recorded. Against 0.9.0 the run adds calls for
its new tool and arguments: `search_books` with and without `limit`/`offset`,
`chapter_id` "current", `chars_before`/`chars_after`, and every argument 0.9.0
added on every tool that has it (`offset` on each paged tool, `order_by`
"oldest" on each tool with an `order_by`); `tests/test_mcp_regress.py` checks
that none is left out. Each run records the MCP version, the `py_apple_books`
version and, if its sources match one, the release it is.

**5. Compare.**

```sh
python3 scripts/mcp_regress/compare.py $S/base-A.json $S/base-B.json $S/cand.json $S/diffs \
    --through <stage> --home $S/home
```

- The three runs must use one MCP version, and the two baselines one known
  release (recorded by `run`; runs made before it recorded them count as
  1.9.1 with MCP 0.8.2). Otherwise it exits 2. `--baseline-release`
  names the release when the runs can't.
- An item applies only if the baseline predates its `release`. Against
  1.10.0 none does, so every change is UNEXPECTED.
- `--through` takes a stage from `expected.STAGES` (`baseline`, `query`,
  `semantics`, `final`) or a stream alias from `expected.ALIASES`. Items apply
  cumulatively: `semantics` includes the `query` items. The default is
  `final`.
- `--home` is the snapshot the runs used. The per-book rules (F25, F62, F07)
  are resolved from it. It is required when one of them is active (a 1.9.1
  baseline past the `query` stage).
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

With a 1.10.0 baseline the stage doesn't matter: use the default. With a
1.9.1 baseline (checking a 1.10-era branch), the stage is the latest one
already in the branch under test, counting the branch's own changes. The
table assumes 1.10's planned merge order, where writes and models-data land
before query-layer. If query-layer is already in your base, use `query`.

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
not-found answer) or `OK <md5[:8]> len=<n>`. Against 0.9.0 the run adds odd
values for its new arguments: huge and negative `offset`, an unknown
`order_by`, `search_books` with a lone surrogate or NUL, and a huge offset
into the current chapter. None of the calls names a real id.
`compare` prints a transition table. It exits 1 on `OK` -> `EXC`, on
`OK notfound` -> anything else, or on a missing call, and 2 if the runs used
different MCP versions (0.9.0 reports not-found as an error, `EXC
ToolError`, where 0.8.2 answers `OK notfound`). `EXC` -> `OK` is reported as
`FIXED`. Result classes can depend on the Python version: on 1.9.1, a NUL
title search is `EXC DBError` on 3.10 but `EXC DBQueryError` on 3.13. Compare
runs made with the same Python minor version.

## Changing the rules

Rules may allow more than actually changes. Allowed means "not a regression
if it changes", never "must change". Every rule belongs to an `ITEMS` entry
named by its audit item id, with the release that ships it and the stage
whose stream lands it. 1.11 adds no items: a change it makes to MCP output on
a fully local library is a bug.
Per-book rules go through a named set in `book_sets()` as a query on the
snapshot, so `expected.py` never contains ids, titles or counts.
