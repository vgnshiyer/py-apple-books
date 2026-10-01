"""scripts/mcp_regress on synthetic runs: 1.10.0 baselines allow no
change, the MCP versions must match, 0.9.0 gets its extra calls.
Skipped where scripts/ isn't shipped (the sdist, a copied test tree)."""

import importlib
import json
import pathlib
import sys
import types

import pytest

HERE = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "mcp_regress"
pytestmark = pytest.mark.skipif(not (HERE / "compare.py").is_file(),
                                reason="scripts/mcp_regress not present")


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    for name in ("harness", "compare", "expected"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    mods = types.SimpleNamespace(**{n: importlib.import_module(n) for n in ("harness", "compare", "expected")})
    yield mods
    for name in ("harness", "compare", "expected", "mcp_regress_expected"):
        sys.modules.pop(name, None)


def write_runs(tmp_path, outs, **info):
    paths = []
    for name, out in zip(("a", "b", "c"), outs):
        run = {"lib": f"/{name}", "out": out, "timings": {k: 0.0 for k in out}}
        run.update(info.get(name, info.get("all", {})))
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(run))
        paths.append(str(path))
    return paths


BASE = {'search_annotations({"limit": 200, "text": "the"})': "one",
        'list_all_books({})': "books"}
CHANGED = dict(BASE, **{'search_annotations({"limit": 200, "text": "the"})': "two"})
V110 = {"mcp_version": "0.9.0", "lib_release": "1.10.0", "lib_version": "1.10.0"}


def test_no_items_are_active_against_110(tools):
    assert tools.expected.active_items("final", "1.10.0") == []
    assert len(tools.expected.active_items("final", "1.9.1")) == len(tools.expected.ITEMS)
    assert tools.expected.active_items("query", "1.9.1") == ["F08", "G4.1", "F28"]
    assert all(rule["release"] == "1.10.0" for rule in tools.expected.ITEMS.values())


def test_identical_runs_pass_against_110(tools, tmp_path):
    paths = write_runs(tmp_path, [BASE, BASE, BASE],
                       a=V110, b=V110, c={"mcp_version": "0.9.0", "lib_release": None})
    assert tools.compare.main(paths) == 0  # no --home needed: no per-book rule is active


def test_a_change_is_unexpected_against_110_but_allowed_against_191(tools, tmp_path, capsys):
    paths = write_runs(tmp_path, [BASE, BASE, CHANGED],
                       a=V110, b=V110, c={"mcp_version": "0.9.0", "lib_release": None})
    assert tools.compare.main(paths) == 1
    assert "UNEXPECTED: 1" in capsys.readouterr().out
    # The same change against 1.9.1 baselines is the 1.10 search rework.
    legacy = write_runs(tmp_path, [BASE, BASE, CHANGED])  # runs without the new keys
    assert tools.compare.main(legacy + ["--through", "query"]) == 0
    assert tools.compare.main(legacy) == 2  # final needs --home for the per-book rules


def test_runs_must_share_the_mcp_version_and_a_known_baseline(tools, tmp_path):
    mixed = write_runs(tmp_path, [BASE, BASE, BASE],
                       a=V110, b=V110, c={"mcp_version": "0.8.2", "lib_release": None})
    assert tools.compare.main(mixed) == 2
    dev = dict(V110, lib_release=None)
    unknown = write_runs(tmp_path, [BASE, BASE, BASE], a=dev, b=dev, c=dev)
    assert tools.compare.main(unknown) == 2
    assert tools.compare.main(unknown + ["--baseline-release", "1.10.0"]) == 0


def test_090_gets_its_new_calls(tools):
    args = dict(book_ids=[1, 2], annotated=[1], collections=[3], context_ids=[4],
                describe_ids=[4], chapter_calls=[[1, "c1"], [1, "c2"], [2, "x"]],
                revisit_title="t", colors=["yellow"])
    old = list(tools.harness.calls(args, "0.8.2"))
    new = list(tools.harness.calls(args, "0.9.0"))
    assert new[:len(old)] == old
    added = new[len(old):]
    assert ("search_books", {"query": "the"}) in added
    assert [kw for tool, kw in added if tool == "get_chapter_content"] == [{"book_id": 1}, {"book_id": 2}]
    assert all(tool != "search_books" for tool, _ in old)


def test_release_of_compares_sources(tools, tmp_path, monkeypatch):
    pkg = tmp_path / "py_apple_books"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("__version__ = '9.9.9'\n")
    lib = types.SimpleNamespace(__file__=str(pkg / "__init__.py"), __version__="9.9.9")
    monkeypatch.setitem(tools.harness.RELEASES, "9.9.9", tools.harness.source_digest(lib))
    assert tools.harness.release_of(lib) == "9.9.9"
    assert tools.harness.release_problem(lib) is None
    (pkg / "extra.py").write_text("")
    assert tools.harness.release_of(lib) is None
    assert "differ" in tools.harness.release_problem(lib)
    assert set(tools.harness.RELEASES) >= {"1.9.1", "1.10.0"}
