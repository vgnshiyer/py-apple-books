"""Point the test run at a synthetic Apple Books library before py_apple_books loads.

py_apple_books finds its stores lazily, but some paths are still fixed
from ``Path.home()`` at import (the writer's default library directory,
the backup directory), so the fake HOME must exist before anything
imports the package. This module therefore never imports py_apple_books:
it finds the schema SQL through importlib's finder (which doesn't
execute the package) and builds the stores with plain sqlite3.
``conftest.py`` calls it at import time. Importing this module also
installs the suite's audit hook (``tests/_fs_audit.py``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import sqlite3
import uuid
from typing import Dict, Optional

from tests import _fs_audit

# The shared audit hook (tests/_fs_audit.py) goes in before anything can
# import py_apple_books, so record() and block() see every event of the
# package, including those of its first import.
_fs_audit.install()

DOCUMENTS = "Library/Containers/com.apple.iBooksX/Data/Documents"
ENV_PREFIX = "APPLE_BOOKS_"

# Test-control switches (they select tests, not a library), restored
# after every other APPLE_BOOKS_* variable is removed.
TEST_CONTROL_VARS = ("APPLE_BOOKS_LIVE_TESTS", "APPLE_BOOKS_FUZZ_ITERATIONS", "APPLE_BOOKS_SLOW_TESTS")

# APPLE_BOOKS_SLOW_TESTS: run the scale tests at release-gate sizes and
# hold the timing tests to their strict budgets (soft by default: CI
# machines vary).
SLOW = os.environ.get("APPLE_BOOKS_SLOW_TESTS", "") not in ("", "0")

# The developer's environment as it was before isolate_environment().
REAL_HOME: Optional[str] = os.environ.get("HOME")
POPPED_ENV: Dict[str, str] = {}

_SCHEMA_NAME = re.compile(r"^macos-([\d.]+)-(\w+)_books-([\d.]+)-(\d+)$")


def isolate_environment() -> None:
    """Remove every APPLE_BOOKS_* variable so a developer's own settings
    can't redirect the suite to a real library; keep the test switches."""
    for key in [k for k in os.environ if k.startswith(ENV_PREFIX)]:
        POPPED_ENV[key] = os.environ.pop(key)
    for key in TEST_CONTROL_VARS:
        if key in POPPED_ENV:
            os.environ[key] = POPPED_ENV[key]


def schemas_dir() -> pathlib.Path:
    spec = importlib.util.find_spec("py_apple_books")
    return pathlib.Path(list(spec.submodule_search_locations)[0]) / "testing" / "schemas"


def _version_key(name: str) -> tuple:
    # Same order as py_apple_books.testing.available_schemas().
    m = _SCHEMA_NAME.match(name)
    if not m:
        return ((), (), 0, name)
    macos, _, books, books_build = m.groups()
    ints = lambda v: tuple(int(p) for p in v.split(".") if p)
    return (ints(macos), ints(books), int(books_build), name)


def default_schema() -> str:
    names = [p.name for p in schemas_dir().iterdir()
             if all((p / f).is_file() for f in ("meta.json", "BKLibrary.sql", "AEAnnotation.sql"))]
    return sorted(names, key=_version_key)[-1]


def build_home(root: pathlib.Path, schema: Optional[str] = None) -> str:
    """Create both stores under ``root`` (a fake HOME); return the schema used."""
    sdir = schemas_dir()
    schema = schema or default_schema()
    meta = json.loads((sdir / schema / "meta.json").read_text())
    for store in ("BKLibrary", "AEAnnotation"):
        dest = pathlib.Path(root) / DOCUMENTS / store / meta["stores"][store]["file"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(dest)
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.executescript((sdir / schema / f"{store}.sql").read_text())
            con.execute("UPDATE Z_METADATA SET Z_UUID = ?", (str(uuid.uuid4()).upper(),))
            con.commit()
        finally:
            con.close()
    return schema
