"""Importing py_apple_books, or any of its modules, does no I/O (R19).

A fresh interpreter (``-I -B``: no cwd, no PYTHON* settings, no
bytecode writes) installs the suite's audit hook and imports every
module of the package, one at a time, recording each import.
Expected: reading any file in the package's own
folder (its modules and package data), and, elsewhere on the import
path, only module files (source, bytecode, extension modules: the
interpreter's ``importlib.machinery.all_suffixes()``) and directory
listings. Anything else is not: no other file (another distribution's
data files included), no write, no SQLite connection, no process, no
``ctypes.dlopen``. ctypes, plistlib, FTS5 probes and the like belong in
first use, not at import.

The modules are found in the package's source, so a new module is
checked without being listed anywhere. The ``tests/import_no_io_*.txt``
lists are optional: a stream may name the modules it adds there (each
listed name must be a module of the package), and ``!name`` keeps a
module from being imported at all (one only ever run as a file). One
list per stream, named ``tests/import_no_io_<stream>.txt`` with the
stream number's dot written as an underscore (``import_no_io_2_1.txt``
for stream 2.1); the glob also reads other spellings.
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

TESTS = pathlib.Path(__file__).resolve().parent
PACKAGE = "py_apple_books"

_SCRIPT = r'''
import importlib, importlib.machinery, importlib.util, json, os, sys

spec = importlib.util.spec_from_file_location("_fs_audit", sys.argv[1])
audit = sys.modules["_fs_audit"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
audit.install()
if sys.argv[2]:
    sys.path.insert(0, sys.argv[2])
pkg = importlib.util.find_spec("py_apple_books")
package = sorted({os.path.realpath(p) for p in pkg.submodule_search_locations})
roots = sorted({os.path.realpath(p) for p in [*sys.path, *package] if p and os.path.isdir(p)})
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
out = {"roots": roots, "package": package, "suffixes": importlib.machinery.all_suffixes(),
       "file": pkg.origin, "modules": {}}
for name in sys.argv[3:]:
    with audit.record() as rec:
        importlib.import_module(name)
    events = []
    for e in rec.events:
        write = False
        if e.event == "open" and len(e.args) >= 3:
            mode, flags = e.args[1], e.args[2]
            write = any(c in (mode or "") for c in "wax+") or bool((flags or 0) & WRITE)
        events.append([e.event, e.path, write])
    out["modules"][name] = events
print(json.dumps(out))
'''


def listed():
    """``(listed, never_imported)`` module names from every list file."""
    imported, skipped = [], []
    for path in sorted(TESTS.glob("import_no_io_*.txt")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith("!"):
                skipped.append(line[1:].strip())
            elif line:
                imported.append(line)
    return imported, skipped


def package_modules():
    """Every module in the package's source, found without importing it."""
    spec = importlib.util.find_spec(PACKAGE)
    root = pathlib.Path(list(spec.submodule_search_locations)[0])
    names = set()
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root.parent).with_suffix("")
        parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
        names.add(".".join(parts))
    return names


def modules_to_import():
    """Every module of the package and every listed one, less the
    ``!name`` ones; the package itself comes first (sorted)."""
    imported, skipped = listed()
    return sorted((package_modules() | set(imported)) - set(skipped))


def _under(path, roots):
    return any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def test_every_module_is_imported():
    modules = modules_to_import()
    _, skipped = listed()
    assert modules[0] == PACKAGE
    assert {"py_apple_books.api", "py_apple_books.db.client", "py_apple_books.models.book"} <= set(modules)
    assert set(modules) == package_modules() - set(skipped)


def test_lists_name_real_modules():
    imported, skipped = listed()
    unknown = sorted(set(imported + skipped) - package_modules())
    assert not unknown, f"listed but not in the package: {unknown}"
    assert len(imported) == len(set(imported)), "a module is listed twice"
    both = sorted(set(imported) & set(skipped))
    assert not both, f"listed both to import and to skip: {both}"


def import_events(modules, extra_path=""):
    """Import ``modules`` in a fresh interpreter; return its report."""
    # -I ignores PYTHON* variables and leaves the cwd off sys.path; -B
    # writes no bytecode, which would show up as writes.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    proc = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _SCRIPT, str(TESTS / "_fs_audit.py"), str(extra_path),
         *modules],
        env=env, cwd=TESTS, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def problems_in(report):
    roots, package = report["roots"], report["package"]
    module_suffixes = tuple(report["suffixes"])
    problems = []
    for name, events in report["modules"].items():
        for event, path, write in events:
            if event == "open" and path is not None:
                if write:
                    why = "a write"
                elif _under(path, package):
                    continue  # the package's own modules and data
                elif not _under(path, roots):
                    why = "outside the import path"
                elif path.endswith(module_suffixes):
                    continue  # another module being imported
                else:
                    why = "not a module file"
            elif event in ("os.listdir", "os.scandir") and path is not None:
                if _under(path, roots):
                    continue  # the import system looking for modules
                why = "outside the import path"
            elif event == "open":
                continue  # an already-open fd: its open was checked
            else:
                why = "not allowed at import"
            problems.append(f"{name}: {event} {path} ({why})")
    return problems


def test_importing_the_package_does_no_io():
    modules = modules_to_import()
    report = import_events(modules)
    assert set(report["modules"]) == set(modules)
    problems = problems_in(report)
    assert not problems, "import-time I/O:\n" + "\n".join(problems)


def test_import_time_io_is_caught(tmp_path):
    """The check itself: a module that reads a file outside the import
    path, reads a data file of another distribution on it, lists a
    folder outside it, writes, connects to SQLite and starts a process
    at import."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("x")
    code = tmp_path / "code"
    (code / "other_dist").mkdir(parents=True)
    (code / "other_dist" / "data.json").write_text("{}")
    (code / "quiet_helper.py").write_text("VALUE = 1\n")
    (code / "noisy_module.py").write_text(
        "import os, sqlite3, subprocess, sys\n"
        "import quiet_helper\n"
        f"open({str(outside / 'data.txt')!r}).read()\n"
        f"open({str(code / 'other_dist' / 'data.json')!r}).read()\n"
        f"os.listdir({str(outside)!r})\n"
        f"open({str(outside / 'new.txt')!r}, 'w').close()\n"
        "sqlite3.connect(':memory:').close()\n"
        "subprocess.run([sys.executable, '-c', 'pass'])\n")
    report = import_events(["noisy_module"], extra_path=code)
    problems = problems_in(report)
    kinds = sorted({p.split(" ", 2)[1] for p in problems})
    assert kinds == ["open", "os.listdir", "sqlite3.connect", "subprocess.Popen"], problems
    assert any("(a write)" in p for p in problems)
    assert any("data.txt (outside the import path)" in p for p in problems)
    assert any("data.json (not a module file)" in p for p in problems)
    # Importing another module from the import path is not a problem.
    helper = os.path.realpath(code / "quiet_helper.py")
    assert any(path == helper for _, path, _ in report["modules"]["noisy_module"])
    assert not any("quiet_helper" in p for p in problems)
