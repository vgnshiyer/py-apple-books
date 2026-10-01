#!/usr/bin/env python3
"""Golden-output check: drive a released apple-books-mcp against this library.

usage:
  python tests/mcp_compat/run.py --mcp-version 0.8.2 --check              # uvx, Python 3.12
  python tests/mcp_compat/run.py --mcp-version 0.8.2 --uvx-python 3.14 --lib dist/py_apple_books-*.whl --check
  python tests/mcp_compat/run.py --mcp-version 0.8.2 --python .venv/bin/python --lib . --update
  python tests/mcp_compat/run.py --mcp-version latest
  python tests/mcp_compat/run.py --mcp-version 0.9.0 --lib dist/py_apple_books-*.whl --lib-extra pdf --check

Seeds the demo library (``py_apple_books.testing.seed_demo``) into a
temporary HOME, starts the MCP server over stdio and calls every
non-write tool from the argument table below, plus the
``apple-books://currently-reading`` resource. Each call's text is
written to ``golden/apple-books-mcp-<version>/NN_<tool>[_k].txt`` under
an ``isError`` header, with the temporary root shown as ``<ROOT>``.

Two ways to start the server:
  uvx (default)  uvx --python <PYVER> --from apple-books-mcp==<v> --with <lib> apple-books-mcp
  --python PY    PY -m apple_books_mcp, with PYTHONPATH=<lib> (PY must have that MCP version)

``--lib`` is the py_apple_books under test: this checkout by default, or
a built wheel; ``--lib-extra NAME`` installs it with that optional extra
(uvx only). ``--check`` exits 1 on any difference from the goldens;
``--update`` rewrites them (review the diff: every change to MCP output
must be an intended one). ``--mcp-version latest`` has no goldens and
fails only on an isError from a probe not marked as an expected error,
a 'Traceback', 'no such column', or a tool missing from the argument
table.

A probe with ``since`` is made for that apple-books-mcp version and
later (a new tool or argument); older pinned versions skip it, so their
goldens don't change. New probes go at the end of the table, since
golden files are numbered by table position.

The server never gets APPLE_BOOKS_MCP_ENABLE_WRITES, and write tools
aren't called. Only the standard library is needed to run this script;
the seeding code is loaded from this checkout without importing
py_apple_books itself.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import os
import pathlib
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from typing import Dict, List, NamedTuple, Optional, Tuple

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
GOLDEN_DIR = HERE / "golden"

WRITE_TOOLS = {
    "create_collection", "rename_collection", "delete_collection",
    "add_book_to_collection", "remove_book_from_collection",
}
RESOURCE = "apple-books://currently-reading"
MISSING = 999999
HUGE = 99999999999999999999  # > 2**63: not representable as an SQLite integer
PROTOCOL_VERSION = "2025-03-26"


class Probe(NamedTuple):
    args: dict
    # isError is an acceptable outcome (only matters for --mcp-version latest).
    expected_error: bool = False
    # Seconds to wait for the response; None means --call-timeout.
    timeout: Optional[float] = None
    # The oldest apple-books-mcp version the probe is made for (None: every
    # version). A pinned older version skips it, so its goldens don't move.
    since: Optional[str] = None


def version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def applies(probe: Probe, mcp_version: str) -> bool:
    return (probe.since is None or mcp_version == "latest"
            or version_key(mcp_version) >= version_key(probe.since))


def probe_table(demo: dict) -> List[Tuple[str, List[Probe]]]:
    """``[(tool, [Probe, ...]), ...]`` for every read tool, in golden order.

    Golden files are numbered by table position, so new entries go at
    the end: inserting one would rename every later file of every
    version's goldens.
    """
    books, annos, colls = demo["books"], demo["annotations"], demo["collections"]
    book = {k: v["id"] for k, v in books.items()}
    reading, shelf, deleted = book["synthetic"], colls["shelf"]["id"], colls["deleted"]["id"]
    content_books = [reading, MISSING, HUGE, book["drm"], book["finished"], book["series_stack"], book["owned_series"]]

    def failing(args: dict, **kwargs) -> Probe:
        # A not-found or not-readable answer: plain text up to 0.8.x, an
        # error result from 0.9.0 on. Either is expected.
        return Probe(args, expected_error=True, **kwargs)

    def v090(args: dict, **kwargs) -> Probe:
        return Probe(args, since="0.9.0", **kwargs)

    return [
        ("list_all_collections", [Probe({}), Probe({"limit": 2})]),
        ("get_collection_books", [
            Probe({"collection_id": str(shelf)}), failing({"collection_id": str(MISSING)}),
            failing({"collection_id": str(deleted)})]),
        ("describe_collection", [
            Probe({"collection_id": str(shelf)}), failing({"collection_id": str(MISSING)}),
            failing({"collection_id": str(deleted)})]),
        ("search_collections_by_title", [Probe({"title": "Shelf"}), Probe({"title": "nothing like it"})]),
        ("list_all_books", [
            Probe({}), Probe({"limit": 2}),
            # 1.9.1 renders LIMIT 10^20 literally: an SQLite error (R22).
            Probe({"limit": HUGE}, expected_error=True)]),
        ("describe_book", [
            Probe({"book_id": str(reading)}), Probe({"book_id": str(book["finished_zero"])}),
            Probe({"book_id": str(book["series_stack"])}), failing({"book_id": str(MISSING)}),
            failing({"book_id": str(HUGE)})]),
        ("search_books_by_title", [
            Probe({"title": "Synthetic"}),
            Probe({"title": "Don't"}, expected_error=True),  # F08: a syntax error before 1.10
            Probe({"title": "%"}), Probe({"title": "_"}), Probe({"title": "Series"})]),
        ("get_books_by_genre", [Probe({"genre": "Fiction"}), Probe({"genre": "Fantasy", "limit": 1})]),
        ("get_books_in_progress", [Probe({}), Probe({"limit": 1})]),
        ("get_finished_books", [Probe({})]),
        ("get_unstarted_books", [Probe({})]),
        ("get_recently_read_books", [Probe({}), Probe({"limit": 2})]),
        ("list_all_annotations", [Probe({}), Probe({"limit": 3})]),
        ("list_annotations", [Probe({"book_id": reading}), Probe({"book_id": book["finished"]}),
                              failing({"book_id": MISSING}), failing({"book_id": HUGE})]),
        ("get_highlights_by_color", [
            Probe({"color": "yellow"}), Probe({"color": "purple"}), Probe({"color": "blue", "limit": 1}),
            Probe({"color": "orange"}, expected_error=True)]),  # not a highlight colour
        ("search_notes", [Probe({"note": "synthetic"}), Probe({"note": "%"})]),
        ("search_annotations", [
            Probe({"text": "synthetic"}), Probe({"text": "synthetic", "limit": 1}),
            Probe({"text": "don't"}, expected_error=True),  # F08
            Probe({"text": "100%"}), Probe({"text": "snake_case"}), Probe({"text": "_"}),
            # A lone surrogate (sent JSON-escaped): the transport may reject it
            # before the tool runs; whatever happens is recorded.
            Probe({"text": "x" + chr(0xD800)}, expected_error=True, timeout=10.0)]),
        ("recent_annotations", [Probe({}), Probe({"limit": 2})]),
        ("describe_annotation", [
            *[Probe({"annotation_id": str(a)}) for a in (
                annos["highlight"], annos["note"], annos["deleted"], annos["tombstone"], annos["orphan"])],
            failing({"annotation_id": str(MISSING)})]),
        ("get_annotation_context", [
            *[Probe({"annotation_id": a}) for a in (
                annos["highlight"], annos["apostrophe"], annos["curly"], annos["deleted"])],
            *[failing({"annotation_id": a}) for a in (annos["orphan"], annos["no_file"], MISSING, HUGE)]]),
        ("get_annotations_by_date_range", [
            Probe({"after": "2026-09-01", "before": "2026-09-30"}), Probe({"after": "2026-09-20"}),
            Probe({"before": "2026-09-18", "limit": 2})]),
        ("list_book_chapters", [Probe({"book_id": reading}),
                                *[failing({"book_id": b}) for b in content_books[1:]]]),
        ("get_chapter_content", [
            Probe({"book_id": reading, "chapter_id": "chap1"}), Probe({"book_id": reading, "chapter_id": "2"}),
            Probe({"book_id": reading, "chapter_id": "chap2", "offset": 10, "max_chars": 20}),
            failing({"book_id": reading, "chapter_id": "Chapter 5"}),
            *[failing({"book_id": b, "chapter_id": "chap1"}) for b in content_books[1:]]]),
        ("get_current_reading_position", [
            Probe({"book_id": reading}), failing({"book_id": MISSING}), failing({"book_id": HUGE}),
            *[Probe({"book_id": b}) for b in content_books[3:]]]),
        ("get_library_stats", [Probe({})]),
        # -- apple-books-mcp 0.9.0 on: a new tool and new arguments --
        ("search_books", [
            v090({"query": "synthetic"}), v090({"query": "second author"}),
            v090({"query": "don't"}), v090({"query": "DON\u2019T PANIC"}),
            v090({"query": "store"}), v090({"query": "series"}),
            v090({"query": "test author book"}), v090({"query": "nothing like it"}),
            v090({"query": "%"}), v090({"query": " "}),
            v090({"query": "book", "limit": 1, "offset": 1})]),
        # chapter_id defaults to "current": the chapter being read.
        ("get_chapter_content", [
            v090({"book_id": reading}),
            v090({"book_id": reading, "chapter_id": "current", "offset": 10, "max_chars": 20}),
            *[v090({"book_id": b, "chapter_id": "current"}, expected_error=True) for b in content_books[1:]]]),
        ("list_all_books", [v090({"limit": 2, "offset": 2}), v090({"offset": 99})]),
        ("list_all_annotations", [v090({"limit": 2, "offset": 2})]),
        ("list_annotations", [v090({"book_id": reading, "limit": 2, "offset": 2})]),
        ("search_annotations", [
            v090({"text": "synthetic", "limit": 1, "offset": 1}),
            v090({"text": "synthetic", "order_by": "oldest"})]),
        ("get_annotations_by_date_range", [v090({"after": "2026-09-01", "order_by": "oldest"})]),
        ("get_annotation_context", [
            v090({"annotation_id": annos["highlight"], "chars_before": 5, "chars_after": 5})]),
    ]


# -- seeding ------------------------------------------------------------------

def load_testing():
    """Import py_apple_books/testing from this checkout without running
    py_apple_books/__init__.py (which, up to 1.9, opens the library)."""
    tdir = REPO / "py_apple_books" / "testing"
    spec = importlib.util.spec_from_file_location(
        "_pab_testing", tdir / "__init__.py", submodule_search_locations=[str(tdir)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# -- server -------------------------------------------------------------------

class Server:
    """A newline-delimited JSON-RPC client for an MCP server on stdio."""

    def __init__(self, cmd: List[str], env: Dict[str, str], cwd: pathlib.Path):
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, cwd=cwd)
        self.lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.stderr: List[bytes] = []
        self.next_id = 0
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines.put), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, self.stderr.append), daemon=True).start()

    @staticmethod
    def _pump(stream, sink) -> None:
        for line in iter(stream.readline, b""):
            sink(line)
        sink(None)

    def _send(self, message: dict) -> None:
        # ensure_ascii keeps a lone surrogate as a \\ud800 escape on the wire.
        self.proc.stdin.write((json.dumps(message) + "\n").encode("ascii"))
        self.proc.stdin.flush()

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method: str, params: dict, timeout: float) -> Optional[dict]:
        """The response, or None if none arrives within ``timeout``.

        Requests are sent one at a time, so an error message without an
        id (the transport rejecting what we sent) belongs to this one.
        Raises EOFError if the server exits.
        """
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if line is None:
                raise EOFError("the MCP server exited")
            try:
                message = json.loads(line.decode("utf-8", "backslashreplace"))
            except ValueError:
                continue  # not protocol output
            if not isinstance(message, dict) or "method" in message:
                continue  # a notification or a request from the server
            if message.get("id") == self.next_id or ("error" in message and message.get("id") is None):
                return message

    def close(self, grace: float = 5.0) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def stderr_tail(self, lines: int = 30) -> str:
        text = b"".join(line for line in self.stderr if line).decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])


def _uv_dir(kind: str) -> Optional[str]:
    """uv's cache or Python install dir, resolved before HOME is replaced."""
    env_var = {"cache": "UV_CACHE_DIR", "python": "UV_PYTHON_INSTALL_DIR"}[kind]
    if os.environ.get(env_var):
        return os.environ[env_var]
    try:
        out = subprocess.run(["uv", kind, "dir"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def server_env(root: pathlib.Path, data_dir: pathlib.Path) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPLE_BOOKS_")}
    env.update(HOME=str(root), TZ="UTC", APPLE_BOOKS_DATA_DIR=str(data_dir), PYTHONDONTWRITEBYTECODE="1")
    for var in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(var, None)
    return env


def _lib_path(lib: pathlib.Path, workdir: pathlib.Path) -> pathlib.Path:
    """A directory to put on PYTHONPATH: ``lib`` itself, or a wheel unpacked."""
    if lib.is_file() and lib.suffix == ".whl":
        target = workdir / "lib"
        with zipfile.ZipFile(lib) as wheel:
            wheel.extractall(target)
        return target
    return lib


def local_server(args, env: Dict[str, str], workdir: pathlib.Path) -> List[str]:
    lib = _lib_path(args.lib, workdir)
    env["PYTHONPATH"] = str(lib)
    probe = ("import importlib.metadata as m, py_apple_books as p, sys\n"
             "print(m.version('apple-books-mcp')); print(p.__file__)")
    out = subprocess.run([args.python, "-c", probe], env=env, cwd=workdir, capture_output=True,
                         text=True, timeout=120)
    if out.returncode != 0:
        raise SystemExit(f"error: {args.python} can't import apple_books_mcp and py_apple_books:\n{out.stderr[-2000:]}")
    version, pab_file = out.stdout.split()
    if args.mcp_version != "latest" and version != args.mcp_version:
        raise SystemExit(f"error: {args.python} has apple-books-mcp {version}, not {args.mcp_version}")
    if not pathlib.Path(pab_file).resolve().is_relative_to(lib.resolve()):
        raise SystemExit(f"error: {args.python} imports py_apple_books from {pab_file}, not from {lib} "
                         f"(an editable install shadows PYTHONPATH?)")
    print(f"server: {args.python} -m apple_books_mcp (apple-books-mcp {version}); "
          f"py_apple_books from {lib}", file=sys.stderr)
    return [args.python, "-m", "apple_books_mcp"]


def uvx_server(args, env: Dict[str, str]) -> List[str]:
    if shutil.which("uvx") is None:
        raise SystemExit("error: uvx not found; install uv, or pass --python PY to use a local environment")
    for kind, var in (("cache", "UV_CACHE_DIR"), ("python", "UV_PYTHON_INSTALL_DIR")):
        path = _uv_dir(kind)
        if path:
            env[var] = path
    source = "apple-books-mcp@latest" if args.mcp_version == "latest" else f"apple-books-mcp=={args.mcp_version}"
    lib = str(args.lib)
    if args.lib_extra:
        # A direct reference, so the extra's dependencies are installed too.
        lib = f"py-apple-books[{','.join(args.lib_extra)}] @ {args.lib.as_uri()}"
    cmd = ["uvx", "--python", args.uvx_python, "--from", source, "--with", lib,
           "--refresh-package", "py-apple-books", "apple-books-mcp"]
    print("server: " + " ".join(cmd), file=sys.stderr)
    return cmd


# -- running and rendering ----------------------------------------------------

class Outcome(NamedTuple):
    status: str  # 'ok', 'isError', 'jsonrpc-error' or 'no-response'
    text: str


def outcome_of(response: Optional[dict]) -> Outcome:
    if response is None:
        return Outcome("no-response", "")
    if "error" in response:
        error = response["error"] or {}
        return Outcome("jsonrpc-error", f"{error.get('code')}: {error.get('message', '')}")
    result = response.get("result") or {}
    if "contents" in result:  # resources/read
        return Outcome("ok", "\n".join(c.get("text", f"[{c.get('mimeType')}]") for c in result["contents"]))
    parts = [c.get("text", "") if c.get("type") == "text" else f"[{c.get('type')} content]"
             for c in result.get("content", [])]
    return Outcome("isError" if result.get("isError") else "ok", "\n".join(parts))


def normalize(text: str, roots: List[str]) -> str:
    for root in roots:
        text = text.replace(root, "<ROOT>")
    # Lone surrogates can't be written as UTF-8; keep them visible as escapes.
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def render(tool: str, args: dict, outcome: Outcome, roots: List[str]) -> str:
    if outcome.status in ("ok", "isError"):
        status = f"isError={outcome.status == 'isError'}"
    else:
        status = outcome.status
    return f"# {tool} {json.dumps(args, sort_keys=True)} {status}\n{normalize(outcome.text, roots)}\n"


def run_calls(args) -> Tuple[Dict[str, str], List[str]]:
    """Seed, start the server, make every call; ``({file: text}, problems)``."""
    testing = load_testing()
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="mcp-compat-"))
    try:
        root = workdir / "home"
        lib = testing.FixtureLibrary.create(root)
        demo = testing.seed_demo(lib, root)
        roots = sorted({str(root), os.path.realpath(root)}, key=len, reverse=True)
        env = server_env(root, lib.data_dir)
        cmd = local_server(args, env, workdir) if args.python else uvx_server(args, env)
        server = Server(cmd, env, cwd=root)
        try:
            return _drive(server, probe_table(demo), roots, args)
        except EOFError as e:
            raise SystemExit(f"error: {e}. Server stderr:\n{server.stderr_tail()}")
        finally:
            server.close()
    finally:
        if args.keep:
            print(f"kept {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def _drive(server: Server, table, roots: List[str], args) -> Tuple[Dict[str, str], List[str]]:
    init = server.request("initialize", {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "py-apple-books-mcp-compat", "version": "1"},
    }, timeout=args.startup_timeout)
    if init is None or "error" in init:
        raise SystemExit(f"error: initialize failed: {init}. Server stderr:\n{server.stderr_tail()}")
    server.notify("notifications/initialized")
    listed = server.request("tools/list", {}, timeout=args.call_timeout)
    tools = {t["name"] for t in ((listed or {}).get("result") or {}).get("tools", [])}
    if not tools:
        raise SystemExit(f"error: tools/list failed: {listed}")

    files = {"00_tools.txt": "# tools/list (names)\n" + "\n".join(sorted(tools)) + "\n"}
    problems = [f"tool {name!r} is not in the argument table"
                for name in sorted(tools - WRITE_TOOLS - {tool for tool, _ in table})]
    for n, (tool, probes) in enumerate(table, start=1):
        probes = [probe for probe in probes if applies(probe, args.mcp_version)]
        if not probes:
            continue  # made for newer versions only
        if tool not in tools:
            print(f"note: {tool} is not offered by this server version; skipped", file=sys.stderr)
            continue
        for k, probe in enumerate(probes, start=1):
            name = f"{n:02d}_{tool}" + (f"_{k}" if len(probes) > 1 else "") + ".txt"
            response = server.request("tools/call", {"name": tool, "arguments": probe.args},
                                      timeout=probe.timeout or args.call_timeout)
            outcome = outcome_of(response)
            files[name] = render(tool, probe.args, outcome, roots)
            if outcome.status == "isError" and not probe.expected_error:
                problems.append(f"{name}: isError from a probe not marked expected_error")
            for marker in ("Traceback", "no such column"):
                if marker in outcome.text:
                    problems.append(f"{name}: output contains {marker!r}")
    outcome = outcome_of(server.request("resources/read", {"uri": RESOURCE}, timeout=args.call_timeout))
    files["99_resource_currently_reading.txt"] = render("resources/read", {"uri": RESOURCE}, outcome, roots)
    if outcome.status != "ok":
        problems.append(f"resources/read {RESOURCE}: {outcome.status}")
    return files, problems


def compare(files: Dict[str, str], golden: pathlib.Path) -> List[str]:
    """Unified diffs between ``files`` and the golden directory."""
    have = {p.name: p.read_text(encoding="utf-8") for p in golden.glob("*.txt")} if golden.is_dir() else {}
    diffs = []
    for name in sorted(have.keys() | files.keys()):
        old, new = have.get(name), files.get(name)
        if old == new:
            continue
        diffs.append("".join(difflib.unified_diff(
            (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
            f"golden/{name}" if old is not None else "/dev/null",
            f"actual/{name}" if new is not None else "/dev/null")))
    return diffs


def extra_name(value: str) -> str:
    """An extra name as PEP 508 allows it (argparse type)."""
    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", value):
        raise argparse.ArgumentTypeError(f"not an extra name: {value!r}")
    return value


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mcp-version", required=True, help="apple-books-mcp version, e.g. 0.8.2, or 'latest'")
    how = parser.add_mutually_exclusive_group()
    how.add_argument("--python", help="run PY -m apple_books_mcp with PYTHONPATH=<lib> instead of uvx")
    how.add_argument("--uvx-python", default="3.12", help="Python version for uvx (default 3.12)")
    parser.add_argument("--lib", type=pathlib.Path, default=REPO,
                        help="py_apple_books under test: a source tree or a wheel (default: this checkout)")
    parser.add_argument("--lib-extra", action="append", metavar="NAME", type=extra_name,
                        help="install --lib with this optional extra (uvx only; repeatable)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 if the output differs from the goldens")
    mode.add_argument("--update", action="store_true", help="rewrite the goldens")
    parser.add_argument("--out", type=pathlib.Path, help="also write the outputs to this directory")
    parser.add_argument("--keep", action="store_true", help="keep the temporary HOME")
    parser.add_argument("--startup-timeout", type=float, default=300.0)
    parser.add_argument("--call-timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    args.lib = args.lib.resolve()
    if args.lib_extra and args.python:
        parser.error("--lib-extra needs uvx; with --python, install the extra's dependencies into PY")
    pinned = args.mcp_version != "latest"
    if args.update and not pinned:
        parser.error("--update needs a pinned --mcp-version")

    files, problems = run_calls(args)
    for name, text in sorted(files.items()):
        header = text.split("\n", 1)[0]
        digest = hashlib.sha1(text.encode()).hexdigest()[:8]
        print(f"{name:48s} {header.rsplit(' ', 1)[-1]:15s} {len(text):6d} {digest}")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (args.out / name).write_text(text, encoding="utf-8")

    golden = GOLDEN_DIR / f"apple-books-mcp-{args.mcp_version}"
    if args.update:
        golden.mkdir(parents=True, exist_ok=True)
        for old in golden.glob("*.txt"):
            old.unlink()
        for name, text in files.items():
            (golden / name).write_text(text, encoding="utf-8")
        print(f"updated {len(files)} goldens in {golden.relative_to(REPO)}")
    if pinned and args.check:
        diffs = compare(files, golden)
        for diff in diffs:
            print(diff)
        print(f"{len(diffs)} of {len(files)} outputs differ from {golden.relative_to(REPO)}")
        if diffs:
            return 1
    for problem in problems:
        print(f"problem: {problem}")
    if not pinned and problems:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
