"""F11: connections are opened at import and bound to the importing
thread (stream 3.1 removes the markers)."""

import os
import pathlib
import subprocess
import sys
import threading

import pytest

F11 = "F11: import-time, thread-bound SQLite connections; stream 3.1 (connections) removes this marker"


@pytest.mark.xfail(strict=True, reason=F11)
def test_query_from_worker_thread(api, library):
    library.add_book("Synthetic Book")
    outcome = {}

    def worker():
        try:
            outcome["books"] = len(list(api.list_books()))
        except Exception as e:  # reported below, not through threading.excepthook
            outcome["error"] = e

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert outcome == {"books": 1}


@pytest.mark.xfail(strict=True, reason=F11)
def test_import_without_a_library(tmp_path):
    """Importing the package must not need Apple Books data."""
    import py_apple_books

    tree = pathlib.Path(py_apple_books.__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
    env.update(HOME=str(tmp_path), PYTHONPATH=os.pathsep.join(filter(None, [str(tree), env.get("PYTHONPATH")])))
    proc = subprocess.run([sys.executable, "-c", "import py_apple_books"], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
